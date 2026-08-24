"""Execute governed local workflow operations through managed Agent sessions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal

from .agent_install import find_trusted_executable, official_agent_spec
from .bridge import Bridge
from .execution_governance import ExecutionGovernance, GovernanceError
from .managed_agents import (
    MAX_INPUT_BYTES,
    TERMINAL_STATES,
    ManagedAgentError,
    ManagedAgentLaunch,
    ManagedAgentManager,
    ManagedAgentSession,
    build_managed_launch,
)
from .operation_queue import (
    RELEASE_GATE_RECEIPT_SCHEMA,
    RELEASE_GATE_TERMINAL_OUTCOME,
    RELEASE_REVIEW_VERDICT_MARKER,
    RELEASE_REVIEW_VERDICT_SCHEMA,
    WORKFLOW_TEMPLATES,
    DurableOperationQueue,
    OperationClaim,
    OperationQueueError,
)
from .secret_scan import redact_secrets


REVIEWED_AGENT_IDS = ("codex", "claude-code")
DEFAULT_LEASE_SECONDS = 30
DEFAULT_HEARTBEAT_SECONDS = 8.0
DEFAULT_POLL_SECONDS = 0.25


class ManagedWorkflowError(RuntimeError):
    """A queued workflow cannot run without weakening its governance contract."""


LaunchBuilder = Callable[..., ManagedAgentLaunch]


class ManagedWorkflowRunner:
    """Claim local operations and render their owned sessions in the Agent Cockpit."""

    def __init__(
        self,
        bridge: Bridge,
        manager: ManagedAgentManager,
        *,
        launch_builder: LaunchBuilder = build_managed_launch,
        available_agent_ids: tuple[str, ...] | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.bridge = bridge
        self.queue = DurableOperationQueue(bridge)
        self.governance = ExecutionGovernance(bridge)
        self.manager = manager
        self.launch_builder = launch_builder
        self._configured_agents = available_agent_ids
        self.lease_seconds = max(5, min(int(lease_seconds), 300))
        self.heartbeat_seconds = max(
            1.0, min(float(heartbeat_seconds), self.lease_seconds / 2)
        )
        self.poll_seconds = max(0.05, min(float(poll_seconds), 5.0))
        self.worker_id = f"control-room-{os.getpid()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_lock = threading.RLock()
        self._active_sessions: tuple[ManagedAgentSession, ...] = ()
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._stop.is_set():
            raise ManagedWorkflowError("managed workflow runner is closed")
        self._thread = threading.Thread(
            target=self._run,
            name="peerbridge-managed-workflows",
            daemon=True,
        )
        self._thread.start()

    def close(self, *, wait_seconds: float = 10.0) -> None:
        self._stop.set()
        with self._active_lock:
            sessions = self._active_sessions
        try:
            self._stop_sessions(sessions)
        except ManagedWorkflowError as exc:
            self.last_error = redact_secrets(str(exc))[:500]
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, min(float(wait_seconds), 30.0)))

    def _reviewed_agents(self) -> tuple[str, ...]:
        if self._configured_agents is not None:
            return self._configured_agents
        available = []
        for agent_id in REVIEWED_AGENT_IDS:
            spec = official_agent_spec(agent_id)
            if find_trusted_executable(spec) is not None:
                available.append(agent_id)
        return tuple(available)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.queue.materialize_due_schedules(limit=20)
                self.queue.reconcile()
                if not self._reviewed_agents():
                    self.last_error = "No reviewed managed Agent CLI is installed."
                    self._stop.wait(2.0)
                    continue
                claim = self.queue.claim(
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                    operation_class="managed",
                )
                if claim is None:
                    self.last_error = None
                    self._stop.wait(self.poll_seconds)
                    continue
                self._run_claim(claim)
            except (GovernanceError, ManagedAgentError, OperationQueueError, OSError) as exc:
                self.last_error = redact_secrets(str(exc))[:500]
                self._stop.wait(self.poll_seconds)
            except Exception as exc:
                self.last_error = redact_secrets(str(exc))[:500]
                self._stop.wait(1.0)

    def _project_directory(self, value: Any) -> Path:
        text = str(value or "").strip()
        candidate = Path(text)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ManagedWorkflowError(
                "workflow working directory must be project-relative"
            )
        resolved = (self.bridge.root / candidate).resolve()
        try:
            relative = resolved.relative_to(self.bridge.root).as_posix()
        except ValueError as exc:
            raise ManagedWorkflowError(
                "workflow working directory escapes the project"
            ) from exc
        if not resolved.is_dir():
            raise ManagedWorkflowError("workflow working directory is unavailable")
        if self.bridge._is_within_protected(relative):
            raise ManagedWorkflowError("workflow working directory is protected")
        return resolved

    @staticmethod
    def _session_id(
        operation_id: str, phase: str, index: int, attempt_count: int
    ) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:12]
        return f"workflow-{digest}-a{attempt_count}-{phase}-{index + 1}"

    @staticmethod
    def _prompt(
        operation: dict[str, Any], *, role: str, execution_mode: str
    ) -> str:
        task = str(operation["task_text"])
        mode_instruction = (
            "Edit only inside the assigned isolated worktree. Never apply changes to "
            "another checkout and never merge."
            if execution_mode == "isolated-write"
            else "Observe and report only; do not modify project files."
        )
        prompt = (
            "PeerBridge managed local workflow.\n"
            f"Workflow: {operation['workflow_id']}\n"
            f"Operation: {operation['operation_id']}\n"
            f"Role: {role}\n"
            f"Execution mode: {execution_mode}\n"
            "Report only observable evidence and a concise final answer. "
            f"Do not claim access to hidden reasoning. {mode_instruction}\n\n"
            f"Task:\n{task}"
        )
        if operation["workflow_id"] == "release-gate":
            fingerprint = str(operation["resource_key"]).removeprefix("release:")
            prompt += (
                "\n\nRelease Gate verdict contract: inspect the exact source and report "
                "all findings normally. Your final answer must end with exactly one "
                "unquoted line using this marker followed by one JSON object:\n"
                f'{RELEASE_REVIEW_VERDICT_MARKER} '
                '{"schema":"peerbridge.release-review-verdict.v1",'
                f'"decision":"approve_or_reject","source_fingerprint":"{fingerprint}",'
                f'"role":"{role}"}}\n'
                "Use decision approve only when no release-blocking finding remains; "
                "otherwise use reject. Do not publish or make a human decision."
            )
        if len(prompt.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ManagedWorkflowError("workflow task exceeds managed input capacity")
        return prompt

    def _launch_group(
        self,
        claim: OperationClaim,
        *,
        roles: tuple[str, ...],
        modes: tuple[Literal["observe", "isolated-write"], ...],
        working_directory: Path,
        governance_binding_id: str | None = None,
        phase: str,
        agent_offset: int = 0,
    ) -> tuple[ManagedAgentSession, ...]:
        if len(roles) != len(modes) or not roles:
            raise ManagedWorkflowError("workflow role plan is invalid")
        available = self._reviewed_agents()
        if not available:
            raise ManagedWorkflowError("no reviewed managed Agent CLI is installed")
        sessions: list[ManagedAgentSession] = []
        try:
            for index, (role, mode) in enumerate(zip(roles, modes, strict=True)):
                if mode == "isolated-write":
                    agent_id = "codex"
                    if agent_id not in available:
                        raise ManagedWorkflowError(
                            "Codex is required for the reviewed isolated-write profile"
                        )
                else:
                    agent_id = available[(index + agent_offset) % len(available)]
                launch = self.launch_builder(
                    agent_id,
                    session_id=self._session_id(
                        str(claim.operation["operation_id"]),
                        phase,
                        index,
                        int(claim.operation["attempt_count"]),
                    ),
                    role=role,
                    working_directory=working_directory,
                    execution_mode=mode,
                    governance_binding_id=governance_binding_id,
                    isolation_verified=(mode == "isolated-write"),
                )
                sessions.append(
                    self.manager.start(
                        launch,
                        input_text=self._prompt(
                            claim.operation,
                            role=role,
                            execution_mode=mode,
                        ),
                    )
                )
        except Exception:
            self._stop_sessions(tuple(sessions))
            raise
        with self._active_lock:
            self._active_sessions = tuple(sessions)
        return tuple(sessions)

    def _stop_sessions(self, sessions: tuple[ManagedAgentSession, ...]) -> None:
        pending = tuple(sessions)
        for _attempt in range(2):
            for session in pending:
                with contextlib.suppress(ManagedAgentError):
                    session.stop()
            pending = tuple(session for session in pending if not session.wait(5.0))
            if not pending:
                return
        self._stop.set()
        raise ManagedWorkflowError(
            "an owned managed Agent process tree did not stop; workflow dispatch halted"
        )

    def _clear_active(self, sessions: tuple[ManagedAgentSession, ...]) -> None:
        with self._active_lock:
            if self._active_sessions == sessions:
                self._active_sessions = ()

    def _reconcile_after_lost_lease(self) -> None:
        with contextlib.suppress(OperationQueueError):
            self.queue.reconcile()

    def _renew_claim(self, claim: OperationClaim, *, transition: str) -> bool:
        try:
            self.queue.heartbeat(
                str(claim.operation["operation_id"]),
                self.worker_id,
                claim.lease_token,
                lease_seconds=self.lease_seconds,
            )
        except OperationQueueError as exc:
            self.last_error = redact_secrets(str(exc))[:500]
            self._fail_claim(
                claim,
                error_class="queue-state",
                detail=(
                    "Managed workflow lost its live operation lease before "
                    f"{transition}."
                ),
                retry_after_seconds=0,
            )
            return False
        return True

    def _fail_claim(
        self,
        claim: OperationClaim,
        *,
        error_class: str,
        detail: str,
        retry_after_seconds: int = 15,
    ) -> None:
        try:
            self.queue.fail(
                str(claim.operation["operation_id"]),
                self.worker_id,
                claim.lease_token,
                error_class=error_class,
                detail=detail,
                retry_after_seconds=retry_after_seconds,
            )
        except OperationQueueError:
            self._reconcile_after_lost_lease()

    def _wait_group(
        self,
        claim: OperationClaim,
        sessions: tuple[ManagedAgentSession, ...],
    ) -> Literal["completed", "cancelled", "incomplete"]:
        operation_id = str(claim.operation["operation_id"])
        next_heartbeat = time.monotonic() + self.heartbeat_seconds
        try:
            while True:
                if self._stop.is_set():
                    self._stop_sessions(sessions)
                    self._fail_claim(
                        claim,
                        error_class="transient",
                        detail="Control Room closed; the managed workflow is retryable.",
                        retry_after_seconds=0,
                    )
                    return "incomplete"
                operation = self.queue.get_operation(operation_id)
                if operation["cancellation_requested"]:
                    self._stop_sessions(sessions)
                    try:
                        self.queue.acknowledge_cancel(
                            operation_id,
                            self.worker_id,
                            claim.lease_token,
                        )
                    except OperationQueueError:
                        self._reconcile_after_lost_lease()
                    return "cancelled"
                now_monotonic = time.monotonic()
                if now_monotonic >= next_heartbeat:
                    try:
                        self.queue.heartbeat(
                            operation_id,
                            self.worker_id,
                            claim.lease_token,
                            lease_seconds=self.lease_seconds,
                        )
                    except OperationQueueError:
                        self._stop_sessions(sessions)
                        self._reconcile_after_lost_lease()
                        return "incomplete"
                    next_heartbeat = now_monotonic + self.heartbeat_seconds
                snapshots = tuple(session.snapshot() for session in sessions)
                states = tuple(str(snapshot["state"]) for snapshot in snapshots)
                provider_statuses = tuple(
                    str(
                        (snapshot.get("terminal_outcome") or {}).get(
                            "provider_status"
                        )
                        or "unavailable"
                    )
                    for snapshot in snapshots
                )
                if any(state in {"failed", "stopped"} for state in states):
                    self._stop_sessions(sessions)
                    self._fail_claim(
                        claim,
                        error_class="provider",
                        detail=(
                            "A managed Agent session failed or stopped; no workflow "
                            "verdict was inferred."
                        ),
                    )
                    return "incomplete"
                if states and all(state == "completed" for state in states):
                    if any(
                        status != "completed" for status in provider_statuses
                    ):
                        self._fail_claim(
                            claim,
                            error_class="provider",
                            detail=(
                                "A managed Agent process exited successfully, but its "
                                "provider terminal outcome was not completed; no workflow "
                                "verdict was inferred."
                            ),
                        )
                        return "incomplete"
                    return "completed"
                self._stop.wait(self.poll_seconds)
        except BaseException:
            self._stop_sessions(sessions)
            raise
        finally:
            if all(
                session.snapshot()["state"] in TERMINAL_STATES for session in sessions
            ):
                self._clear_active(sessions)

    @staticmethod
    def _release_review_verdict(
        session: ManagedAgentSession,
        *,
        expected_fingerprint: str,
        expected_role: str,
    ) -> dict[str, str]:
        snapshot = session.snapshot()
        matches: list[tuple[dict[str, Any], str]] = []
        for event in snapshot.get("events") or ():
            if event.get("kind") != "answer":
                continue
            answer = str(event.get("summary") or "").strip()
            if not answer:
                continue
            for line in answer.splitlines():
                prefix = f"{RELEASE_REVIEW_VERDICT_MARKER} "
                if not line.startswith(prefix):
                    continue
                try:
                    payload = json.loads(line[len(prefix) :])
                except json.JSONDecodeError as exc:
                    raise ManagedWorkflowError(
                        "release reviewer returned a malformed verdict"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ManagedWorkflowError(
                        "release reviewer returned a malformed verdict"
                    )
                matches.append((payload, answer))
        if len(matches) != 1:
            raise ManagedWorkflowError(
                "release reviewer must return exactly one explicit verdict"
            )
        payload, answer = matches[0]
        if set(payload) != {"schema", "decision", "source_fingerprint", "role"}:
            raise ManagedWorkflowError("release reviewer verdict fields are invalid")
        decision = str(payload.get("decision") or "")
        if (
            payload.get("schema") != RELEASE_REVIEW_VERDICT_SCHEMA
            or decision not in {"approve", "reject"}
            or payload.get("source_fingerprint") != expected_fingerprint
            or payload.get("role") != expected_role
        ):
            raise ManagedWorkflowError(
                "release reviewer verdict does not match its source and role"
            )
        return {
            "agent_id": str(snapshot["agent_id"]),
            "session_id": str(snapshot["session_id"]),
            "role": expected_role,
            "decision": decision,
            "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        }

    def _binding_for_implement(
        self, operation: dict[str, Any]
    ) -> tuple[dict[str, Any], Path]:
        permission_id = str(operation.get("permission_decision_id") or "")
        if not permission_id:
            raise ManagedWorkflowError(
                "Implement + Review requires a human-approved isolated worktree binding"
            )
        binding = self.governance.execution_binding_for_permission(permission_id)
        if binding["state"] != "active":
            raise ManagedWorkflowError("isolated execution binding is not active")
        if binding["agent_id"] != "codex":
            raise ManagedWorkflowError(
                "isolated-write binding must identify the reviewed Codex writer"
            )
        worktree = Path(str(binding["worktree_path"])).resolve()
        repository = Path(str(binding["repository_root"])).resolve()
        requested = (self.bridge.root / str(operation["working_directory"])).resolve()
        if repository != self.bridge.root or requested != repository:
            raise ManagedWorkflowError(
                "workflow project does not match its isolated execution binding"
            )
        verified = self.governance.verify_execution_source(str(binding["binding_id"]))
        if verified["stale"]:
            raise ManagedWorkflowError("isolated execution binding is stale before launch")
        return binding, worktree

    def _run_implement_review(self, claim: OperationClaim) -> None:
        binding, worktree = self._binding_for_implement(claim.operation)
        binding_id = str(binding["binding_id"])
        writer = self._launch_group(
            claim,
            roles=("implementer",),
            modes=("isolated-write",),
            working_directory=worktree,
            governance_binding_id=binding_id,
            phase="write",
        )
        if self._wait_group(claim, writer) != "completed":
            return
        if not self._renew_claim(claim, transition="sealing writer output"):
            return
        self.governance.seal_execution(binding_id)
        if self.governance.verify_execution_source(binding_id)["stale"]:
            self._fail_claim(
                claim,
                error_class="source-drift",
                detail="Writer output changed while the execution binding was sealed.",
                retry_after_seconds=0,
            )
            return
        if not self._renew_claim(claim, transition="launching the same-source reviewer"):
            return
        reviewer = self._launch_group(
            claim,
            roles=("reviewer",),
            modes=("observe",),
            working_directory=worktree,
            governance_binding_id=binding_id,
            phase="review",
            agent_offset=1,
        )
        if self._wait_group(claim, reviewer) != "completed":
            return
        if self.governance.verify_execution_source(binding_id)["stale"]:
            self._fail_claim(
                claim,
                error_class="source-drift",
                detail="Reviewed source changed after the execution binding was sealed.",
                retry_after_seconds=0,
            )
            return
        if not self._renew_claim(claim, transition="recording final completion"):
            return
        self.queue.complete(
            str(claim.operation["operation_id"]),
            self.worker_id,
            claim.lease_token,
            outcome="managed_sessions_completed",
            detail=(
                "Isolated writer and same-source reviewer sessions completed; "
                "operator review is still required before patch application or merge."
            ),
        )

    def _run_observe_workflow(self, claim: OperationClaim) -> None:
        template = WORKFLOW_TEMPLATES[str(claim.operation["workflow_id"])]
        roles = tuple(str(value) for value in template["roles"])
        modes = tuple(str(value) for value in template["session_modes"])
        if any(mode != "observe" for mode in modes):
            raise ManagedWorkflowError("workflow requires an unsupported execution mode")
        if (
            claim.operation["workflow_id"] == "release-gate"
            and len(set(self._reviewed_agents())) < 2
        ):
            raise ManagedWorkflowError(
                "Release Gate requires two distinct reviewed Agent CLIs"
            )
        directory = self._project_directory(claim.operation["working_directory"])
        sessions = self._launch_group(
            claim,
            roles=roles,
            modes=tuple("observe" for _role in roles),
            working_directory=directory,
            phase="observe",
        )
        if self._wait_group(claim, sessions) != "completed":
            return
        if claim.operation["workflow_id"] == "release-gate":
            fingerprint = str(claim.operation["resource_key"]).removeprefix(
                "release:"
            )
            if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                raise ManagedWorkflowError("Release Gate source fingerprint is invalid")
            reviews = [
                self._release_review_verdict(
                    session,
                    expected_fingerprint=fingerprint,
                    expected_role=role,
                )
                for session, role in zip(sessions, roles, strict=True)
            ]
            if len({review["agent_id"] for review in reviews}) != 2:
                raise ManagedWorkflowError(
                    "Release Gate reviewers must use two distinct Agent CLIs"
                )
            if any(review["decision"] != "approve" for review in reviews):
                self._fail_claim(
                    claim,
                    error_class="review-blocked",
                    detail=(
                        "At least one independent Release Gate reviewer rejected the "
                        "exact source. Review the separate Cockpit answers."
                    ),
                    retry_after_seconds=0,
                )
                return
            if not self._renew_claim(claim, transition="recording Release Gate verdicts"):
                return
            receipt = {
                "schema": RELEASE_GATE_RECEIPT_SCHEMA,
                "operation_id": str(claim.operation["operation_id"]),
                "source_fingerprint": fingerprint,
                "reviews": reviews,
            }
            self.queue.complete(
                str(claim.operation["operation_id"]),
                self.worker_id,
                claim.lease_token,
                outcome=RELEASE_GATE_TERMINAL_OUTCOME,
                detail=json.dumps(
                    receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                ),
            )
            return
        if not self._renew_claim(claim, transition="recording final completion"):
            return
        self.queue.complete(
            str(claim.operation["operation_id"]),
            self.worker_id,
            claim.lease_token,
            outcome="managed_sessions_completed",
            detail=(
                "All managed observe sessions completed; their captured answers remain "
                "separate in the Agent Cockpit for operator judgment."
            ),
        )

    def _run_claim(self, claim: OperationClaim) -> None:
        try:
            workflow_id = str(claim.operation["workflow_id"])
            if workflow_id == "implement-review":
                self._run_implement_review(claim)
            else:
                self._run_observe_workflow(claim)
            self.last_error = None
        except OperationQueueError as exc:
            self.last_error = redact_secrets(str(exc))[:500]
            self._fail_claim(
                claim,
                error_class="queue-state",
                detail="Managed workflow queue transition failed closed.",
                retry_after_seconds=0,
            )
        except (
            GovernanceError,
            ManagedAgentError,
            ManagedWorkflowError,
            OSError,
        ) as exc:
            self.last_error = redact_secrets(str(exc))[:500]
            self._fail_claim(
                claim,
                error_class="configuration",
                detail="Managed workflow failed closed before a governed completion.",
                retry_after_seconds=0,
            )


__all__ = ["ManagedWorkflowError", "ManagedWorkflowRunner"]
