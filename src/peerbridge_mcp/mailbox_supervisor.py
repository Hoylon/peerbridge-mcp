"""Low-memory, crash-recoverable routed-message supervisor.

One supervisor process can service many saved routes.  A provider runtime is
created only while a routed message is being answered.  When a queued route's
credential backend cannot be resolved safely, the exact dispatch is failed
closed so bounded discussions cannot wait forever for an invisible participant.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .bridge import SAFE_ID, Bridge, MAX_TEXT_CHARS
from .openai_compatible_runner import (
    ConfigurationError,
    CredentialUnavailableError,
    InferenceResult,
    MCPTransportError,
    MaxToolRoundsError,
    OpenAICompatibleRunner,
    ProviderHTTPError,
    ResourceUnavailableError,
    RouteMismatchError,
    RunnerConfig,
    RunnerError,
    RunCancelledError,
    ToolCallError,
    provider_runtime_admission,
)
from .usage import usage_from_receipt


SUPERVISOR_CLIENT_NAME = "peerbridge-mailbox-supervisor"
SUPPORTED_SECRET_BACKENDS = {
    "windows-credential-manager",
    "cc-switch",
    "native-acp",
}
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_PARALLEL_DISPATCHES = 16
DEFAULT_RETRY_BACKOFF_BASE_SECONDS = 15
DEFAULT_RETRY_BACKOFF_CAP_SECONDS = 300
DEFAULT_CYCLE_ERROR_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_CYCLE_ERROR_BACKOFF_CAP_SECONDS = 30.0
DEFAULT_RUNNER_HARD_DEADLINE_SECONDS = 900.0
DEFAULT_RUNNER_CANCEL_GRACE_SECONDS = 5.0


class SupervisorError(RuntimeError):
    """A safe, non-secret supervisor error."""


class SupervisorAlreadyRunningError(SupervisorError):
    """Another process owns this scope's mailbox supervisor lock."""


class _RunnerHardDeadlineExceeded(SupervisorError):
    """The provider runner did not return before the supervisor deadline."""


class _RunnerCancellationIncomplete(SupervisorError):
    """The provider runner ignored cancellation past the bounded grace period."""


class _DispatchLeaseRenewalFailed(SupervisorError):
    """The active dispatch lease could not be confirmed before its safety margin."""


class Runner(Protocol):
    def run(
        self,
        messages: list[dict[str, str]],
        *,
        message_id: str | None = None,
    ) -> InferenceResult: ...

    def cancel(self) -> None: ...


RunnerFactory = Callable[[RunnerConfig], Runner]
CredentialProbe = Callable[["RouteRuntime"], bool]
ReconciliationHook = Callable[[Bridge, Mapping[str, Any]], Mapping[str, Any] | None]


@dataclass(frozen=True)
class RouteRuntime:
    route_id: str
    agent_id: str
    client_name: str | None
    connection_id: str
    provider_id: str
    model_id: str
    response_model_id: str | None
    reasoning_mode: str | None
    route_class: str
    profile_sha256: str
    connection_sha256: str
    credential_version_sha256: str
    secret_backend: str
    credential_target: str


@dataclass(frozen=True)
class CycleResult:
    runnable_routes: int
    claimed: int
    completed: int
    retryable_failures: int
    terminal_failures: int
    discussions_advanced: int = 0


@dataclass(frozen=True)
class _ClaimedDispatch:
    route: RouteRuntime
    bridge: Bridge
    claim: Mapping[str, Any]
    runtime_admission: contextlib.ExitStack


@dataclass(frozen=True)
class _DispatchOutcome:
    completed: bool = False
    retryable_failure: bool = False
    terminal_failure: bool = False


class _ProcessFileLock:
    """Cross-platform advisory lock whose ownership is released on process exit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            raise SupervisorAlreadyRunningError(
                "a mailbox supervisor already owns this scope"
            ) from None
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "_ProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _runtime_key(route: RouteRuntime) -> tuple[str, ...]:
    return (
        route.agent_id,
        route.route_id,
        route.connection_id,
        route.provider_id,
        route.model_id,
        route.response_model_id or "",
        route.reasoning_mode or "",
        route.route_class,
        route.profile_sha256,
        route.connection_sha256,
        route.credential_version_sha256,
        route.secret_backend,
        route.credential_target,
    )


def discover_runnable_routes(
    control: Bridge,
    *,
    credential_probe: CredentialProbe | None = None,
) -> tuple[RouteRuntime, ...]:
    """Resolve only exact route/connection pairs with a supported secret backend."""
    profiles = control.list_route_profiles({"enabled_only": True})["profiles"]
    connections = control.list_provider_connections({"enabled_only": True})[
        "connections"
    ]
    by_provider: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for connection in connections:
        connection_id = str(connection.get("connection_id") or "")
        provider_id = str(connection.get("provider_id") or "")
        route_class = str(connection.get("route_class") or "")
        # Schema v10 CC Switch references predate the explicit provider_id
        # column.  Their route profiles already used the redacted connection
        # id as provider identity, so retain that exact legacy binding without
        # reading or rewriting provider credentials.
        if (
            not provider_id
            and connection.get("secret_backend") == "cc-switch"
            and connection_id
        ):
            provider_id = connection_id
        if (
            connection.get("secret_backend") not in SUPPORTED_SECRET_BACKENDS
            or not provider_id
            or not route_class
        ):
            continue
        by_provider.setdefault((provider_id, route_class), []).append(connection)

    routes: list[RouteRuntime] = []
    for profile in profiles:
        provider_id = str(profile.get("provider_id") or "")
        model_id = str(profile.get("model_id") or "")
        route_class = str(profile.get("route_class") or "")
        matches = by_provider.get((provider_id, route_class), [])
        if not provider_id or not model_id or len(matches) != 1:
            continue
        connection = matches[0]
        route = RouteRuntime(
                route_id=str(profile["route_id"]),
                agent_id=str(profile["agent_id"]),
                client_name=profile.get("client_name"),
                connection_id=str(connection["connection_id"]),
                provider_id=provider_id,
                model_id=model_id,
                response_model_id=profile.get("response_model_id"),
                reasoning_mode=profile.get("reasoning_mode"),
                route_class=route_class,
                profile_sha256=str(profile["profile_sha256"]),
                connection_sha256=str(connection["connection_sha256"]),
                credential_version_sha256=str(
                    connection.get("credential_version_sha256") or ""
                ),
                secret_backend=str(connection.get("secret_backend") or ""),
                credential_target=str(connection.get("credential_target") or ""),
            )
        probe = credential_probe
        if probe is not None and not probe(route):
            continue
        routes.append(route)
    return tuple(sorted(set(routes), key=_runtime_key))


def _prompt_for(message: Mapping[str, Any], route: RouteRuntime) -> list[dict[str, str]]:
    discussion_instruction = ""
    if message.get("discussion_id"):
        discussion_instruction = (
            " This is one bounded parallel discussion round. Address peer evidence, "
            "add only material new information, and end with exactly one line: "
            "PEERBRIDGE_SIGNAL: CONTINUE, CONSENSUS, or BLOCKED."
        )
    return [
        {
            "role": "system",
            "content": (
                "You are an AI peer participating in an auditable local PeerBridge room. "
                "Answer the addressed message directly and concisely. Use a read-only MCP "
                "tool only when current PeerBridge state is necessary; ordinary analysis "
                "and review should answer directly without tools. Never reveal credentials, "
                "hidden reasoning, or private data, and do not claim to have changed files."
                + discussion_instruction
            ),
        },
        {
            "role": "user",
            "content": (
                f"Agent: {route.agent_id}\n"
                f"Room: {message['room_id']}\n"
                f"Task: {message['task_id']}\n"
                f"Subject: {message['subject']}\n\n"
                f"{message['body']}"
            ),
        },
    ]


def _reply_text(result: InferenceResult) -> str:
    value = result.content
    if not isinstance(value, str):
        raise SupervisorError("provider returned unsupported assistant content")
    value = value.strip()
    if not value:
        raise SupervisorError("provider returned an empty assistant response")
    if len(value) > MAX_TEXT_CHARS:
        raise SupervisorError("provider assistant response exceeded the bridge limit")
    return value


def _failure_policy(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ResourceUnavailableError):
        return "resource_unavailable", True
    if isinstance(exc, ProviderHTTPError):
        if exc.status_code == 401:
            return "provider_authentication_required", False
        if exc.status_code == 402:
            return "provider_billing_required", False
        if exc.status_code == 403:
            return "provider_access_denied", False
        if exc.status_code == 429:
            return "provider_rate_limited", True
        return "provider_http_retryable" if exc.retryable else "provider_http_failed", bool(
            exc.retryable
        )
    if isinstance(exc, CredentialUnavailableError):
        return "credential_unavailable", False
    if isinstance(exc, RouteMismatchError):
        return "route_mismatch", False
    if isinstance(exc, ConfigurationError):
        return "configuration_invalid", False
    if isinstance(exc, RunCancelledError):
        return "run_cancelled", True
    if isinstance(exc, MCPTransportError):
        return "mcp_transport_failed", True
    if isinstance(exc, (ToolCallError, MaxToolRoundsError)):
        return "tool_policy_failed", False
    if isinstance(exc, (RunnerError, SupervisorError)):
        return "inference_failed", False
    return "unexpected_runtime_failure", False


class MailboxSupervisor:
    """Route-aware orchestrator that does not keep provider processes resident."""

    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        scope: str,
        *,
        runner_factory: RunnerFactory | None = None,
        credential_probe: CredentialProbe | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = 5,
        max_parallel_dispatches: int = DEFAULT_MAX_PARALLEL_DISPATCHES,
        retry_backoff_base_seconds: int = DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
        retry_backoff_cap_seconds: int = DEFAULT_RETRY_BACKOFF_CAP_SECONDS,
        lease_renew_interval_seconds: float | None = None,
        reconciliation_hook: ReconciliationHook | None = None,
        cycle_error_backoff_base_seconds: float = DEFAULT_CYCLE_ERROR_BACKOFF_BASE_SECONDS,
        cycle_error_backoff_cap_seconds: float = DEFAULT_CYCLE_ERROR_BACKOFF_CAP_SECONDS,
        runner_hard_deadline_seconds: float = DEFAULT_RUNNER_HARD_DEADLINE_SECONDS,
        runner_cancel_grace_seconds: float = DEFAULT_RUNNER_CANCEL_GRACE_SECONDS,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.db_path = Path(db_path).resolve()
        self.scope = str(scope or "").strip()
        if not SAFE_ID.fullmatch(self.scope):
            raise SupervisorError("scope is not a safe identifier")
        self.lease_seconds = max(30, min(int(lease_seconds), 86_400))
        self.max_attempts = max(1, min(int(max_attempts), 100))
        self.max_parallel_dispatches = max(
            1, min(int(max_parallel_dispatches), 64)
        )
        self.retry_backoff_base_seconds = max(
            1, min(int(retry_backoff_base_seconds), 3_600)
        )
        self.retry_backoff_cap_seconds = max(
            self.retry_backoff_base_seconds,
            min(int(retry_backoff_cap_seconds), 86_400),
        )
        self.cycle_error_backoff_base_seconds = max(
            0.05, min(float(cycle_error_backoff_base_seconds), 300.0)
        )
        self.cycle_error_backoff_cap_seconds = max(
            self.cycle_error_backoff_base_seconds,
            min(float(cycle_error_backoff_cap_seconds), 300.0),
        )
        self.runner_hard_deadline_seconds = max(
            0.05, min(float(runner_hard_deadline_seconds), 86_400.0)
        )
        self.runner_cancel_grace_seconds = max(
            0.01, min(float(runner_cancel_grace_seconds), 60.0)
        )
        default_renew_interval = max(1.0, min(30.0, self.lease_seconds / 3.0))
        requested_renew_interval = (
            default_renew_interval
            if lease_renew_interval_seconds is None
            else float(lease_renew_interval_seconds)
        )
        self.lease_renew_interval_seconds = max(
            0.05,
            min(requested_renew_interval, max(0.05, self.lease_seconds / 2.0)),
        )
        self._runner_factory = runner_factory
        self._credential_probe = credential_probe
        self._reconciliation_hook = reconciliation_hook
        self._closed = False
        self._route_bridges: dict[tuple[str, ...], Bridge] = {}
        self._process_lock = _ProcessFileLock(self.lock_path)
        self._process_lock.acquire()
        try:
            self._control = Bridge(
                self.project_root,
                self.db_path,
                "mailbox-supervisor",
                self.scope,
                client_name=SUPERVISOR_CLIENT_NAME,
                discussion_coordinator=True,
            )
        except BaseException:
            self._process_lock.release()
            raise

    def _credential_available(self, route: RouteRuntime) -> bool:
        if self._credential_probe is not None:
            return bool(self._credential_probe(route))
        if route.secret_backend == "cc-switch":
            from .ccswitch_runner import resolve_reference

            try:
                resolve_reference(
                    app=str(route.client_name or ""),
                    credential_target=route.credential_target,
                    route_class=route.route_class,
                    model_id=route.model_id,
                )
            except Exception:
                return False
            return True
        if route.secret_backend == "native-acp":
            from .acpx_runner import REFERENCE_PREFIX, SUPPORTED_AGENTS, find_acpx

            client = str(route.client_name or "").strip().lower()
            return (
                client in SUPPORTED_AGENTS
                and route.credential_target == f"{REFERENCE_PREFIX}{client}"
                and find_acpx() is not None
            )

        from . import credentials

        try:
            credentials.load_provider_access(
                scope=self.scope,
                connection_id=route.connection_id,
                route_class=route.route_class,
                provider_id=route.provider_id,
            )
        except credentials.CredentialStoreError:
            return False
        return True

    def _runner_for(self, route: RouteRuntime, config: RunnerConfig) -> Runner:
        if self._runner_factory is not None:
            return self._runner_factory(config)
        if route.secret_backend == "cc-switch":
            from .ccswitch_runner import CcSwitchRunner

            return CcSwitchRunner(
                config,
                credential_target=route.credential_target,
                client_name=route.client_name,
                runtime_admitted=True,
            )
        if route.secret_backend == "native-acp":
            from .acpx_runner import AcpxRunner

            return AcpxRunner(
                config,
                credential_target=route.credential_target,
                client_name=route.client_name,
                runtime_admitted=True,
            )
        return OpenAICompatibleRunner(config, runtime_admitted=True)

    @property
    def lock_path(self) -> Path:
        return self.db_path.parent / f"mailbox-supervisor-{self.scope}.lock"

    def _bridge_for(self, route: RouteRuntime) -> Bridge:
        key = _runtime_key(route)
        bridge = self._route_bridges.get(key)
        if bridge is None:
            bridge = Bridge(
                self.project_root,
                self.db_path,
                route.agent_id,
                self.scope,
                session_id=f"supervisor-{uuid.uuid4().hex}",
                client_name=route.client_name or SUPERVISOR_CLIENT_NAME,
                provider_id=route.provider_id,
                model_id=route.model_id,
                reasoning_mode=route.reasoning_mode,
                route_class=route.route_class,
            )
            self._route_bridges[key] = bridge
        return bridge

    def _runner_config(
        self, route: RouteRuntime, bridge: Bridge, room_id: str
    ) -> RunnerConfig:
        return RunnerConfig(
            project_root=self.project_root,
            db_path=self.db_path,
            scope=self.scope,
            connection_id=route.connection_id,
            route_class=route.route_class,
            provider_id=route.provider_id,
            model=route.model_id,
            response_model=route.response_model_id,
            reasoning_mode=route.reasoning_mode,
            route_profile_id=route.route_id,
            route_profile_sha256=route.profile_sha256,
            room_id=room_id,
            session_id=bridge.session_id,
            agent_id=route.agent_id,
            response_only_fallback_on_tool_error=True,
        )

    def _execute_claimed(self, job: _ClaimedDispatch) -> _DispatchOutcome:
        route = job.route
        bridge = job.bridge
        claim = job.claim
        message = claim["message"]
        token = claim["lease_token"]
        renewal_stop = threading.Event()
        renewal_abort = threading.Event()
        renewal_failures: list[Exception] = []
        lease_expires_monotonic = [time.monotonic() + self.lease_seconds]
        lease_safety_margin = max(
            0.1,
            min(
                self.lease_seconds / 4.0,
                self.lease_renew_interval_seconds * 2.0,
            ),
        )

        def renew_once() -> bool:
            try:
                bridge.renew_message_dispatch(
                    {
                        "message_id": message["message_id"],
                        "lease_token": token,
                        "lease_seconds": self.lease_seconds,
                    }
                )
            except Exception as exc:
                renewal_failures[:] = [exc]
                return False
            renewal_failures.clear()
            lease_expires_monotonic[0] = time.monotonic() + self.lease_seconds
            return True

        def renew_lease() -> None:
            while not renewal_stop.wait(self.lease_renew_interval_seconds):
                if renew_once():
                    continue
                if time.monotonic() >= (
                    lease_expires_monotonic[0] - lease_safety_margin
                ):
                    renewal_abort.set()
                    return

        renewal_thread = threading.Thread(
            target=renew_lease,
            name=f"peerbridge-lease-{str(message['message_id'])[:12]}",
            daemon=True,
        )
        renewal_thread.start()
        try:
            runner = self._runner_for(
                route,
                self._runner_config(route, bridge, str(message["room_id"])),
            )
            result = self._run_with_hard_deadline(
                runner,
                _prompt_for(message, route),
                message_id=str(message["message_id"]),
                abort_event=renewal_abort,
            )
            renewal_stop.set()
            renewal_thread.join(timeout=2.0)
            final_renewed = False
            for attempt in range(3):
                if renew_once():
                    final_renewed = True
                    break
                if attempt < 2:
                    time.sleep(min(0.05, self.lease_renew_interval_seconds / 2.0))
            if not final_renewed:
                return self._fail_claimed(
                    job,
                    error_code="dispatch_lease_renewal_failed",
                    can_retry=True,
                )
            receipt_sha = str(result.receipt.get("receipt_sha256") or "")
            bridge.complete_message_dispatch(
                {
                    "message_id": message["message_id"],
                    "lease_token": token,
                    "body": _reply_text(result),
                    "inference_receipt_sha256": receipt_sha,
                    "inference_usage": usage_from_receipt(result.receipt),
                }
            )
            return _DispatchOutcome(completed=True)
        except _RunnerHardDeadlineExceeded:
            renewal_stop.set()
            renewal_thread.join(timeout=0.25)
            return self._fail_claimed(
                job,
                error_code="runner_hard_deadline_exceeded",
                can_retry=True,
            )
        except _RunnerCancellationIncomplete:
            renewal_stop.set()
            renewal_thread.join(timeout=0.25)
            return self._fail_claimed(
                job,
                error_code="runner_cancellation_incomplete",
                can_retry=False,
            )
        except _DispatchLeaseRenewalFailed:
            renewal_stop.set()
            renewal_thread.join(timeout=0.25)
            return self._fail_claimed(
                job,
                error_code="dispatch_lease_renewal_failed",
                can_retry=True,
            )
        except Exception as exc:
            error_code, can_retry = _failure_policy(exc)
            return self._fail_claimed(
                job,
                error_code=error_code,
                can_retry=can_retry,
            )
        finally:
            renewal_stop.set()
            renewal_thread.join(timeout=2.0)

    def _run_with_hard_deadline(
        self,
        runner: Runner,
        messages: list[dict[str, str]],
        *,
        message_id: str,
        abort_event: threading.Event | None = None,
    ) -> InferenceResult:
        cancel = getattr(runner, "cancel", None)
        completed = threading.Event()
        completed_at: list[float] = []
        results: list[InferenceResult] = []
        failures: list[BaseException] = []

        def invoke_runner() -> None:
            try:
                results.append(runner.run(messages, message_id=message_id))
            except BaseException as exc:
                failures.append(exc)
            finally:
                completed_at.append(time.monotonic())
                completed.set()

        deadline = time.monotonic() + self.runner_hard_deadline_seconds
        runner_thread = threading.Thread(
            target=invoke_runner,
            name=f"peerbridge-runner-{message_id[:12]}",
            daemon=True,
        )
        runner_thread.start()
        stopped_for_lease = False
        while not completed.is_set():
            if abort_event is not None and abort_event.is_set():
                stopped_for_lease = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            completed.wait(min(0.05, remaining))
        if not completed.is_set():
            cancel_completed = threading.Event()

            def request_cancel() -> None:
                try:
                    if callable(cancel):
                        cancel()
                finally:
                    cancel_completed.set()

            if callable(cancel):
                threading.Thread(
                    target=request_cancel,
                    name=f"peerbridge-cancel-{message_id[:12]}",
                    daemon=True,
                ).start()
            grace_deadline = time.monotonic() + self.runner_cancel_grace_seconds
            cancel_returned = (
                cancel_completed.wait(self.runner_cancel_grace_seconds)
                if callable(cancel)
                else False
            )
            remaining_grace = max(0.0, grace_deadline - time.monotonic())
            runner_returned = completed.wait(remaining_grace)
            if not (cancel_returned and runner_returned):
                raise _RunnerCancellationIncomplete
            runner_thread.join()
            if stopped_for_lease:
                raise _DispatchLeaseRenewalFailed
            raise _RunnerHardDeadlineExceeded
        runner_thread.join()
        if completed_at and completed_at[0] > deadline:
            raise _RunnerHardDeadlineExceeded
        if failures:
            raise failures[0]
        return results[0]

    def _fail_claimed(
        self,
        job: _ClaimedDispatch,
        *,
        error_code: str,
        can_retry: bool,
    ) -> _DispatchOutcome:
        claim = job.claim
        attempts = int(claim["dispatch"]["attempt_count"])
        can_retry = can_retry and attempts < self.max_attempts
        retry_after_seconds = min(
            self.retry_backoff_cap_seconds,
            self.retry_backoff_base_seconds * (2 ** max(0, attempts - 1)),
        )
        try:
            job.bridge.fail_message_dispatch(
                {
                    "message_id": claim["message"]["message_id"],
                    "lease_token": claim["lease_token"],
                    "error_code": error_code,
                    "retryable": can_retry,
                    "retry_after_seconds": retry_after_seconds,
                }
            )
        except Exception:
            # A lost/expired lease must not terminate the supervisor process or
            # create a reply outside the active lease transaction.
            return _DispatchOutcome(
                terminal_failure=True,
            )
        return _DispatchOutcome(
            retryable_failure=can_retry,
            terminal_failure=not can_retry,
        )

    def _reconcile_dispatches(
        self,
        route_runtime_observations: tuple[dict[str, Any], ...] = (),
    ) -> Mapping[str, Any] | None:
        """Invoke one Bridge-owned atomic reconciliation operation when available."""
        args = {"max_attempts": self.max_attempts, "limit": 500}
        if route_runtime_observations:
            args["route_runtime_observations"] = list(route_runtime_observations)
        if self._reconciliation_hook is not None:
            result = self._reconciliation_hook(self._control, args)
        else:
            reconcile = getattr(self._control, "reconcile_message_dispatches", None)
            if not callable(reconcile):
                return None
            result = reconcile(args)
        if result is not None and not isinstance(result, Mapping):
            raise SupervisorError("dispatch reconciliation returned an invalid result")
        return result

    def _pending_route_requests(self) -> tuple[dict[str, Any], ...]:
        """Return bounded unresolved root messages before probing providers.

        Provider discovery can involve an external CLI or Credential Manager.
        Performing it for every saved model on every idle poll is unnecessary
        and was the main source of idle background churn.
        """
        with self._control._connect() as connection:
            rows = connection.execute(
                """SELECT m.sequence, m.message_id, m.recipient,
                                      m.route_profile_id,
                                      m.requested_provider_id,
                                      m.requested_model_id,
                                      m.requested_reasoning_mode,
                                      m.requested_route_class,
                                      m.route_request_sha256
                       FROM messages m
                       LEFT JOIN message_receipts r
                         ON r.scope=m.scope AND r.message_id=m.message_id
                        AND r.agent_id=m.recipient
                       LEFT JOIN message_dispatches d
                         ON d.scope=m.scope AND d.message_id=m.message_id
                        AND d.agent_id=m.recipient
                       LEFT JOIN message_dispatch_retry_schedules s
                         ON s.scope=d.scope AND s.message_id=d.message_id
                        AND s.agent_id=d.agent_id AND s.attempt_count=d.attempt_count
                      WHERE m.scope=? AND m.recipient!='*'
                        AND m.sender!=m.recipient AND m.reply_to IS NULL
                        AND m.route_request_sha256 IS NOT NULL
                        AND r.message_id IS NULL
                        AND (d.status IS NULL OR d.status NOT IN ('completed', 'failed'))
                        AND (d.attempt_count IS NULL OR d.attempt_count<?)
                        AND (d.status IS NULL OR d.status!='retryable'
                             OR s.not_before_epoch IS NULL OR s.not_before_epoch<=?)
                        AND (m.discussion_id IS NULL OR EXISTS (
                            SELECT 1 FROM room_discussions rd
                             WHERE rd.scope=m.scope
                               AND rd.discussion_id=m.discussion_id
                               AND rd.status='active'
                        ))
                      ORDER BY m.sequence ASC LIMIT 500""",
                (self.scope, self.max_attempts, time.time()),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    @staticmethod
    def _route_matches_request(
        route: RouteRuntime, request: Mapping[str, Any]
    ) -> bool:
        if str(request.get("recipient") or "") != route.agent_id:
            return False
        comparisons = (
            (request.get("route_profile_id"), route.route_id),
            (request.get("requested_provider_id"), route.provider_id),
            (request.get("requested_model_id"), route.model_id),
            (request.get("requested_reasoning_mode"), route.reasoning_mode),
            (request.get("requested_route_class"), route.route_class),
        )
        return all(not expected or str(expected) == str(observed or "") for expected, observed in comparisons)

    @classmethod
    def _plan_route_requests(
        cls,
        routes: tuple[RouteRuntime, ...],
        pending_requests: tuple[dict[str, Any], ...],
    ) -> tuple[tuple[RouteRuntime, dict[str, Any]], ...]:
        """Assign at most one exact message to each unambiguous route runtime."""
        planned: list[tuple[RouteRuntime, dict[str, Any]]] = []
        assigned_routes: set[tuple[str, ...]] = set()
        for request in pending_requests:
            matches = tuple(
                route for route in routes if cls._route_matches_request(route, request)
            )
            if len(matches) != 1:
                # A direct identity request that maps to multiple saved profiles
                # has no defensible profile identity. Leave it for reconciliation.
                continue
            route = matches[0]
            key = _runtime_key(route)
            if key in assigned_routes:
                continue
            assigned_routes.add(key)
            planned.append((route, request))
        return tuple(planned)

    @classmethod
    def _terminal_route_runtime_observations(
        cls,
        routes: tuple[RouteRuntime, ...],
        pending_requests: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        observations: list[dict[str, Any]] = []
        for request in pending_requests:
            match_count = sum(
                1 for route in routes if cls._route_matches_request(route, request)
            )
            if match_count == 1:
                continue
            observations.append(
                {
                    "message_id": str(request["message_id"]),
                    "route_request_sha256": str(request["route_request_sha256"]),
                    "match_count": match_count,
                }
            )
        return tuple(observations)

    @classmethod
    def _claim_handoff_is_exact(
        cls,
        route: RouteRuntime,
        request: Mapping[str, Any],
        claim: Mapping[str, Any],
    ) -> bool:
        message = claim.get("message")
        if not isinstance(message, Mapping):
            return False
        if message.get("message_id") != request.get("message_id"):
            return False
        if message.get("recipient") != request.get("recipient"):
            return False
        request_fields = (
            "route_profile_id",
            "requested_provider_id",
            "requested_model_id",
            "requested_reasoning_mode",
            "requested_route_class",
            "route_request_sha256",
        )
        if any(message.get(key) != request.get(key) for key in request_fields):
            return False
        route_profile_id = request.get("route_profile_id")
        if route_profile_id is not None and str(route_profile_id) != route.route_id:
            return False
        route_request = message.get("route_request")
        if not isinstance(route_request, Mapping):
            return False
        expected_route_request = {
            "route_profile_id": request.get("route_profile_id"),
            "target_agent_id": request.get("recipient"),
            "requested_provider_id": request.get("requested_provider_id"),
            "requested_model_id": request.get("requested_model_id"),
            "requested_reasoning_mode": request.get("requested_reasoning_mode"),
            "requested_route_class": request.get("requested_route_class"),
            "route_request_sha256": request.get("route_request_sha256"),
        }
        if any(
            route_request.get(key) != value
            for key, value in expected_route_request.items()
        ):
            return False
        evaluation = message.get("route_evaluation")
        if not isinstance(evaluation, Mapping) or evaluation.get("status") != "verified":
            return False
        observed = evaluation.get("observed")
        expected_observed = {
            "agent_id": route.agent_id,
            "provider_id": route.provider_id,
            "model_id": route.model_id,
            "reasoning_mode": route.reasoning_mode,
            "route_class": route.route_class,
        }
        return (
            isinstance(observed, Mapping)
            and all(observed.get(key) == value for key, value in expected_observed.items())
            and cls._route_matches_request(route, message)
        )

    def _claim_for_route(
        self,
        route: RouteRuntime,
        request: Mapping[str, Any],
        *,
        advertise_presence: bool,
    ) -> tuple[Bridge, Mapping[str, Any] | None, bool]:
        bridge = self._bridge_for(route)
        if advertise_presence:
            bridge.touch_presence("mailbox-supervisor")
        claim_args: dict[str, Any] = {
            "message_id": request["message_id"],
            "require_route": True,
            "lease_seconds": self.lease_seconds,
            "max_attempts": self.max_attempts,
        }
        requested_profile = request.get("route_profile_id")
        if requested_profile is not None:
            claim_args["route_profile_id"] = requested_profile
        claim = bridge.claim_message_dispatch(claim_args)
        if not claim["claimed"]:
            return bridge, None, False
        if self._claim_handoff_is_exact(route, request, claim):
            return bridge, claim, False
        with contextlib.suppress(Exception):
            bridge.fail_message_dispatch(
                {
                    "message_id": claim["message"]["message_id"],
                    "lease_token": claim["lease_token"],
                    "error_code": "route_handoff_mismatch",
                    "retryable": False,
                }
            )
        return bridge, None, True

    def run_cycle(self) -> CycleResult:
        if self._closed:
            raise SupervisorError("mailbox supervisor is closed")
        pending_requests = self._pending_route_requests()
        discovered_routes = (
            discover_runnable_routes(self._control) if pending_requests else ()
        )
        route_runtime_observations = self._terminal_route_runtime_observations(
            discovered_routes, pending_requests
        )
        reconciliation = self._reconcile_dispatches(route_runtime_observations)
        reconciliation_terminal = int(
            (reconciliation or {}).get("count") or 0
        )
        pending_requests = self._pending_route_requests()
        planned = self._plan_route_requests(discovered_routes, pending_requests)
        routes: list[tuple[RouteRuntime, dict[str, Any]]] = []
        unavailable_routes: list[tuple[RouteRuntime, dict[str, Any]]] = []
        for route, request in planned:
            if self._credential_available(route):
                routes.append((route, request))
            else:
                unavailable_routes.append((route, request))

        active_keys = {_runtime_key(route) for route, _request in routes}
        for key, bridge in tuple(self._route_bridges.items()):
            if key not in active_keys:
                bridge.clear_presence()
                del self._route_bridges[key]

        claimed_count = 0
        terminal = reconciliation_terminal
        for route, request in unavailable_routes:
            bridge, claim, handoff_failed = self._claim_for_route(
                route, request, advertise_presence=False
            )
            if handoff_failed:
                claimed_count += 1
                terminal += 1
            elif claim is not None:
                message = claim["message"]
                bridge.fail_message_dispatch(
                    {
                        "message_id": message["message_id"],
                        "lease_token": claim["lease_token"],
                        "error_code": "credential_unavailable",
                        "retryable": False,
                    }
                )
                claimed_count += 1
                terminal += 1
            bridge.clear_presence()
            self._route_bridges.pop(_runtime_key(route), None)

        jobs: list[_ClaimedDispatch] = []
        for route, request in routes:
            if len(jobs) >= self.max_parallel_dispatches:
                break
            runtime_admission = contextlib.ExitStack()
            try:
                runtime_admission.enter_context(provider_runtime_admission())
            except ResourceUnavailableError:
                runtime_admission.close()
                break
            try:
                bridge, claim, handoff_failed = self._claim_for_route(
                    route, request, advertise_presence=True
                )
            except BaseException:
                runtime_admission.close()
                raise
            if handoff_failed:
                claimed_count += 1
                terminal += 1
                runtime_admission.close()
                continue
            if claim is None:
                runtime_admission.close()
                continue
            jobs.append(
                _ClaimedDispatch(
                    route=route,
                    bridge=bridge,
                    claim=claim,
                    runtime_admission=runtime_admission,
                )
            )
        claimed_count += len(jobs)

        completed = retryable = 0
        if jobs:
            workers = min(self.max_parallel_dispatches, len(jobs))
            try:
                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="peerbridge-dispatch",
                ) as executor:
                    future_jobs = {
                        executor.submit(self._execute_claimed, job): job for job in jobs
                    }
                    for future in as_completed(future_jobs):
                        job = future_jobs[future]
                        try:
                            outcome = future.result()
                        except Exception:
                            # One malformed provider runtime cannot kill the
                            # supervisor cycle or strand unrelated room agents.
                            outcome = self._fail_claimed(
                                job,
                                error_code="unexpected_worker_failure",
                                can_retry=False,
                            )
                        finally:
                            job.runtime_admission.close()
                        completed += int(outcome.completed)
                        retryable += int(outcome.retryable_failure)
                        terminal += int(outcome.terminal_failure)
            finally:
                for job in jobs:
                    job.runtime_admission.close()
        discussion_result = self._control.advance_discussions({"limit": 25})
        return CycleResult(
            len(routes),
            claimed_count,
            completed,
            retryable,
            terminal,
            int(discussion_result.get("count") or 0),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            for bridge in tuple(self._route_bridges.values()):
                with contextlib.suppress(Exception):
                    bridge.clear_presence()
            self._route_bridges.clear()
        finally:
            self._process_lock.release()

    def run_forever(self, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        if self._closed:
            raise SupervisorError("mailbox supervisor is closed")
        interval = max(0.25, min(float(poll_seconds), 300.0))
        consecutive_failures = 0
        try:
            while True:
                try:
                    self.run_cycle()
                except Exception:
                    consecutive_failures += 1
                    delay = min(
                        self.cycle_error_backoff_cap_seconds,
                        self.cycle_error_backoff_base_seconds
                        * (2 ** min(consecutive_failures - 1, 20)),
                    )
                else:
                    consecutive_failures = 0
                    delay = interval
                time.sleep(delay)
        finally:
            self.close()

    def run_once(self) -> CycleResult:
        """Run one cycle under the same single-supervisor ownership lock."""
        if self._closed:
            raise SupervisorError("mailbox supervisor is closed")
        try:
            return self.run_cycle()
        finally:
            self.close()


__all__ = [
    "CycleResult",
    "DEFAULT_CYCLE_ERROR_BACKOFF_BASE_SECONDS",
    "DEFAULT_CYCLE_ERROR_BACKOFF_CAP_SECONDS",
    "DEFAULT_MAX_PARALLEL_DISPATCHES",
    "MailboxSupervisor",
    "RouteRuntime",
    "SupervisorAlreadyRunningError",
    "SupervisorError",
    "discover_runnable_routes",
]
