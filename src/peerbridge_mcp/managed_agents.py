"""Bounded local CLI sessions for the in-app Agent Cockpit.

Managed sessions capture only observable process output. They never claim access to
provider-side history or hidden reasoning, and they never persist command input.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from .agent_install import (
    ACPX_RUNTIME_SPEC,
    find_trusted_executable,
    official_agent_spec,
)
from .child_environment import build_agent_child_environment
from .process_control import (
    attach_process_tree,
    process_group_popen_kwargs,
    release_process_tree,
    terminate_process_tree,
    write_process_stdin_bounded,
)
from .secret_scan import contains_secret, redact_secrets
from .usage import aggregate_usage, normalize_provider_usage, unavailable_usage


MAX_SESSIONS = 8
MAX_RETAINED_SESSIONS = 32
MAX_EVENTS_PER_SESSION = 2_000
MAX_CAPTURE_CHARS = 1024 * 1024
MAX_RAW_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_INPUT_BYTES = 64 * 1024
# The reviewed WSL2/bubblewrap boundary uses explicit mount and namespace
# arguments instead of an opaque shell script. Keep the list bounded while
# allowing that auditable command shape.
MAX_ARGUMENTS = 128
MAX_ARGUMENT_CHARS = 4_096
MAX_ENVIRONMENT_ENTRIES = 128
MAX_ENVIRONMENT_VALUE_CHARS = 32_767
MAX_USAGE_RECORDS_PER_SESSION = 16
MAX_OUTPUT_DRAIN_SECONDS = 10.0
ACPX_OBSERVE_TIMEOUT_SECONDS = 180
ACPX_OBSERVE_PROFILES: Mapping[str, tuple[str, str]] = {
    "kimi-code": ("kimi", "kimi"),
    "grok": ("grok-build", "grok-build"),
}
ACPX_AGENT_IDS = frozenset(ACPX_OBSERVE_PROFILES)
ACPX_REQUEST_METHODS = frozenset(
    {"initialize", "authenticate", "session/new", "session/prompt"}
)
NON_CODEX_WRITE_UNAVAILABLE_REASON = "credential_broker_required"
TERMINAL_STATES = frozenset({"completed", "failed", "stopped"})
SESSION_STATES = frozenset(
    {"created", "running", "stopping", "completed", "failed", "stopped"}
)


class ManagedAgentError(RuntimeError):
    """A managed Agent session cannot be operated safely."""


class ManagedAgentUnavailableError(ManagedAgentError):
    """A reviewed launch profile is intentionally unavailable."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.reason_code = reason
        self.unavailable_reason = reason


def _non_codex_write_unavailable() -> ManagedAgentUnavailableError:
    return ManagedAgentUnavailableError(
        "isolated-write has no reviewed launch profile for this Agent; "
        "a provider credential broker is required",
        reason=NON_CODEX_WRITE_UNAVAILABLE_REASON,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_label(value: str, label: str, *, limit: int = 100) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > limit:
        raise ManagedAgentError(f"{label} is invalid")
    if any(ord(character) < 32 for character in normalized):
        raise ManagedAgentError(f"{label} contains a control character")
    if contains_secret(normalized):
        raise ManagedAgentError(f"{label} contains credential-like data")
    return normalized


@dataclass(frozen=True)
class ManagedAgentLaunch:
    session_id: str
    agent_id: str
    display_name: str
    role: str
    executable: Path
    arguments: tuple[str, ...]
    working_directory: Path
    requested_route: str | None = None
    execution_mode: Literal["observe", "isolated-write"] = "observe"
    permission_tier: Literal[
        "observe", "review", "edit", "full-development"
    ] = "observe"
    governance_binding_id: str | None = None
    isolation_boundary: str | None = None
    process_working_directory: Path | None = field(default=None, repr=False)
    input_mode: Literal["single"] = "single"
    environment: tuple[tuple[str, str], ...] | None = field(default=None, repr=False)

    def validated(self) -> "ManagedAgentLaunch":
        session_id = _safe_label(self.session_id, "session id")
        agent_id = _safe_label(self.agent_id, "agent id")
        display_name = _safe_label(self.display_name, "display name", limit=160)
        role = _safe_label(self.role, "role", limit=80)
        executable = self.executable.resolve()
        working_directory = self.working_directory.resolve()
        process_working_directory = (
            self.process_working_directory.resolve()
            if self.process_working_directory is not None
            else None
        )
        if not executable.is_file():
            raise ManagedAgentError("managed Agent executable is unavailable")
        if not working_directory.is_dir():
            raise ManagedAgentError("managed Agent working directory is unavailable")
        if (
            process_working_directory is not None
            and not process_working_directory.is_dir()
        ):
            raise ManagedAgentError("managed Agent launcher directory is unavailable")
        if len(self.arguments) > MAX_ARGUMENTS:
            raise ManagedAgentError("managed Agent command has too many arguments")
        arguments: list[str] = []
        for value in self.arguments:
            argument = str(value)
            if not argument or len(argument) > MAX_ARGUMENT_CHARS or "\x00" in argument:
                raise ManagedAgentError("managed Agent command argument is invalid")
            if contains_secret(argument):
                raise ManagedAgentError(
                    "managed Agent command argument contains credential-like data"
                )
            arguments.append(argument)
        requested_route = (
            _safe_label(self.requested_route, "requested route", limit=160)
            if self.requested_route
            else None
        )
        if self.execution_mode not in {"observe", "isolated-write"}:
            raise ManagedAgentError("managed Agent execution mode is invalid")
        if self.permission_tier not in {
            "observe",
            "review",
            "edit",
            "full-development",
        }:
            raise ManagedAgentError("managed Agent permission tier is invalid")
        if self.execution_mode == "observe" and self.permission_tier not in {
            "observe",
            "review",
        }:
            raise ManagedAgentError(
                "observe execution cannot use a write-enabled permission tier"
            )
        if self.execution_mode == "isolated-write" and self.permission_tier not in {
            "edit",
            "full-development",
        }:
            raise ManagedAgentError(
                "isolated-write execution requires a write-enabled permission tier"
            )
        governance_binding_id = (
            _safe_label(
                self.governance_binding_id,
                "governance binding id",
                limit=200,
            )
            if self.governance_binding_id
            else None
        )
        isolation_boundary = (
            _safe_label(
                self.isolation_boundary,
                "isolation boundary",
                limit=160,
            )
            if self.isolation_boundary
            else None
        )
        if self.execution_mode == "isolated-write" and not isolation_boundary:
            raise ManagedAgentError(
                "isolated-write execution requires an explicit isolation boundary"
            )
        if self.input_mode != "single":
            raise ManagedAgentError("managed Agent input mode is invalid")
        environment: tuple[tuple[str, str], ...] | None = None
        if self.environment is not None:
            if len(self.environment) > MAX_ENVIRONMENT_ENTRIES:
                raise ManagedAgentError("managed Agent environment is too large")
            selected: dict[str, str] = {}
            for raw_key, raw_value in self.environment:
                key = str(raw_key)
                value = str(raw_value)
                if (
                    not key
                    or "=" in key
                    or "\x00" in key
                    or "\x00" in value
                    or len(value) > MAX_ENVIRONMENT_VALUE_CHARS
                ):
                    raise ManagedAgentError("managed Agent environment is invalid")
                selected[key.upper() if os.name == "nt" else key] = value
            environment = tuple(sorted(selected.items(), key=lambda item: item[0].upper()))
        return ManagedAgentLaunch(
            session_id=session_id,
            agent_id=agent_id,
            display_name=display_name,
            role=role,
            executable=executable,
            arguments=tuple(arguments),
            working_directory=working_directory,
            requested_route=requested_route,
            execution_mode=self.execution_mode,
            permission_tier=self.permission_tier,
            governance_binding_id=governance_binding_id,
            isolation_boundary=isolation_boundary,
            process_working_directory=process_working_directory,
            input_mode=self.input_mode,
            environment=environment,
        )


@dataclass(frozen=True)
class ManagedAgentEvent:
    sequence: int
    created_utc: str
    stream: Literal["system", "stdout", "stderr"]
    kind: Literal["system", "terminal", "activity", "answer", "error"]
    text: str
    summary: str | None = None


def build_observe_launch(
    agent_id: str,
    *,
    session_id: str,
    role: str,
    working_directory: Path,
    requested_route: str | None = None,
    permission_tier: Literal["observe", "review"] = "observe",
    governance_binding_id: str | None = None,
) -> ManagedAgentLaunch:
    """Build one reviewed, stdin-driven, read-only/plan-mode CLI launch."""

    if permission_tier not in {"observe", "review"}:
        raise ManagedAgentError(
            "read-only managed launch only supports observe or review"
        )
    spec = official_agent_spec(agent_id)
    executable = find_trusted_executable(spec)
    if executable is None:
        raise ManagedAgentError("official Agent CLI is not installed in a trusted location")
    official_executable = executable
    if agent_id == "codex":
        environment_family = "codex"
        arguments_list = [
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            "read-only",
            "--ephemeral",
        ]
        if requested_route:
            arguments_list.extend(("--model", requested_route))
        arguments_list.append("-")
        arguments = tuple(arguments_list)
    elif agent_id == "claude-code":
        environment_family = "claude"
        arguments_list = [
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "plan",
            "--no-session-persistence",
        ]
        if requested_route:
            arguments_list.extend(("--model", requested_route))
        arguments = tuple(arguments_list)
    elif agent_id in ACPX_OBSERVE_PROFILES:
        acpx_agent, environment_family = ACPX_OBSERVE_PROFILES[agent_id]
        acpx_executable = find_trusted_executable(ACPX_RUNTIME_SPEC)
        if acpx_executable is None:
            raise ManagedAgentError(
                "ACPX interoperability runtime is not installed in a trusted location"
            )
        arguments_list = [
            "--cwd",
            str(working_directory.resolve()),
            "--auth-policy",
            "skip",
            "--deny-all",
            "--non-interactive-permissions",
            "fail",
            "--format",
            "json",
            "--json-strict",
            "--no-fs",
            "--no-terminal",
            "--max-turns",
            "1",
            "--prompt-retries",
            "0",
            "--timeout",
            str(ACPX_OBSERVE_TIMEOUT_SECONDS),
        ]
        if requested_route:
            arguments_list.extend(("--model", requested_route))
        arguments_list.extend((acpx_agent, "exec", "-f", "-"))
        arguments = tuple(arguments_list)
        executable = acpx_executable
        try:
            child_environment = build_agent_child_environment(
                environment_family,
                required_path_roots=(executable.parent, official_executable.parent),
                include_provider_credentials=False,
            )
        except ValueError as exc:
            raise ManagedAgentError(
                "ACPX managed Agent environment could not be isolated"
            ) from exc
    else:
        raise ManagedAgentError(
            "this official Agent does not yet have a reviewed managed launch profile"
        )
    if agent_id not in ACPX_OBSERVE_PROFILES:
        try:
            child_environment = build_agent_child_environment(
                environment_family,
                include_provider_credentials=False,
            )
        except ValueError as exc:
            raise ManagedAgentError(
                "managed Agent environment could not be isolated"
            ) from exc
    return ManagedAgentLaunch(
        session_id=session_id,
        agent_id=agent_id,
        display_name=spec.display_name,
        role=role,
        executable=executable,
        arguments=arguments,
        working_directory=working_directory,
        requested_route=requested_route,
        permission_tier=permission_tier,
        governance_binding_id=governance_binding_id,
        environment=tuple(child_environment.items()),
    ).validated()


def build_managed_launch(
    agent_id: str,
    *,
    session_id: str,
    role: str,
    working_directory: Path,
    execution_mode: Literal["observe", "isolated-write"],
    permission_tier: Literal[
        "observe", "review", "edit", "full-development"
    ] | None = None,
    requested_route: str | None = None,
    governance_binding_id: str | None = None,
    isolation_verified: bool = False,
) -> ManagedAgentLaunch:
    """Build one reviewed launch and fail closed before any write-enabled process."""

    if execution_mode == "observe":
        selected_permission = permission_tier or "observe"
        if selected_permission not in {"observe", "review"}:
            raise ManagedAgentError(
                "observe execution cannot use a write-enabled permission tier"
            )
        return build_observe_launch(
            agent_id,
            session_id=session_id,
            role=role,
            working_directory=working_directory,
            requested_route=requested_route,
            permission_tier=selected_permission,
            governance_binding_id=governance_binding_id,
        )
    if execution_mode != "isolated-write":
        raise ManagedAgentError("managed Agent execution mode is invalid")
    if agent_id != "codex":
        raise _non_codex_write_unavailable()
    if not isolation_verified or not governance_binding_id:
        raise ManagedAgentError(
            "isolated-write launch requires a verified governance binding"
        )
    if permission_tier not in {None, "edit", "full-development"}:
        raise ManagedAgentError(
            "isolated-write execution requires a write-enabled permission tier"
        )
    spec = official_agent_spec(agent_id)
    executable = find_trusted_executable(spec)
    if executable is None:
        raise ManagedAgentError("official Agent CLI is not installed in a trusted location")
    try:
        child_environment = build_agent_child_environment(
            "codex",
            include_provider_credentials=False,
        )
    except ValueError as exc:
        raise ManagedAgentError(
            "managed Agent environment could not be isolated"
        ) from exc
    return ManagedAgentLaunch(
        session_id=session_id,
        agent_id=agent_id,
        display_name=spec.display_name,
        role=role,
        executable=executable,
        arguments=tuple(
            [
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            *(["--model", requested_route] if requested_route else []),
            "-",
            ]
        ),
        working_directory=working_directory,
        requested_route=requested_route,
        execution_mode="isolated-write",
        permission_tier=permission_tier or "edit",
        governance_binding_id=governance_binding_id,
        isolation_boundary="codex-workspace-write-v1",
        environment=tuple(child_environment.items()),
    ).validated()


def build_wsl_write_launch(
    agent_id: str,
    *,
    session_id: str,
    role: str,
    working_directory: Path,
    permission_tier: Literal["edit", "full-development"],
    requested_route: str | None,
    governance_binding_id: str,
) -> ManagedAgentLaunch:
    """Fail closed until provider auth is brokered outside model-visible tools."""

    if agent_id == "codex":
        raise ManagedAgentError("Codex write launches do not use the WSL profile")
    raise _non_codex_write_unavailable()


def _explicit_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts) or None
    return None


def _observed_label(value: Any, *, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > limit
        or any(ord(character) < 32 for character in normalized)
        or normalized == "[REDACTED]"
        or contains_secret(normalized)
    ):
        return None
    return normalized


def _codex_usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None
    allowlisted = {
        "input_tokens": raw.get("input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "cached_input_tokens": raw.get("cached_input_tokens"),
        "reasoning_tokens": raw.get("reasoning_output_tokens"),
    }
    return normalize_provider_usage(allowlisted, source="codex-exec-jsonl")


def _claude_usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("usage")
    if isinstance(raw, Mapping):
        return normalize_provider_usage(raw, source="claude-stream-json")
    model_usage = payload.get("modelUsage")
    if not isinstance(model_usage, Mapping):
        return None
    records = [
        normalize_provider_usage(value, source="claude-stream-json:model-usage")
        for value in tuple(model_usage.values())[:MAX_USAGE_RECORDS_PER_SESSION]
        if isinstance(value, Mapping)
    ]
    if not records:
        return None
    return aggregate_usage(records, source="claude-stream-json:model-usage")


def _acpx_request_key(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    label = _observed_label(str(value), limit=80)
    return f"{type(value).__name__}:{label}" if label is not None else None


def _acpx_assistant_text(payload: Mapping[str, Any]) -> str | None:
    if payload.get("method") != "session/update":
        return None
    params = payload.get("params")
    update = params.get("update") if isinstance(params, Mapping) else None
    if not isinstance(update, Mapping) or update.get("sessionUpdate") != "agent_message_chunk":
        return None
    content = update.get("content")
    if not isinstance(content, Mapping) or content.get("type") != "text":
        return None
    return _explicit_text(content.get("text"))


def _acpx_usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        metadata = payload.get("_meta")
        raw = metadata.get("usage") if isinstance(metadata, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    return normalize_provider_usage(raw, source="acpx/acp-session-prompt")


def _acpx_model_evidence(
    payload: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    configured_model: str | None = None
    options = payload.get("configOptions")
    if isinstance(options, list):
        matches = [
            item
            for item in options
            if isinstance(item, Mapping) and item.get("id") == "model"
        ]
        if len(matches) == 1:
            configured_model = _observed_label(matches[0].get("currentValue"))
    models = payload.get("models")
    observed_route = (
        _observed_label(models.get("currentModelId"))
        if isinstance(models, Mapping)
        else None
    )
    model = configured_model or (
        observed_route.split("[", 1)[0] if observed_route is not None else None
    )
    return model, observed_route or model


def _classify_output(
    stream: Literal["stdout", "stderr"], text: str
) -> tuple[Literal["terminal", "activity", "answer", "error"], str | None]:
    if stream == "stderr":
        return "error", None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "terminal", None
    if not isinstance(payload, dict):
        return "terminal", None
    if payload.get("jsonrpc") == "2.0":
        assistant_text = _acpx_assistant_text(payload)
        if assistant_text:
            return "answer", redact_secrets(assistant_text)
        if isinstance(payload.get("error"), Mapping):
            return "error", "ACPX request failed"
        method = _observed_label(payload.get("method"), limit=80)
        return "activity", f"ACPX {method}" if method else "ACPX JSON-RPC event"
    event_type = str(payload.get("type") or "")
    if event_type == "result":
        summary = _explicit_text(payload.get("result"))
        return "answer", redact_secrets(summary) if summary else None
    item = payload.get("item")
    if event_type == "item.completed" and isinstance(item, dict):
        if item.get("type") == "agent_message":
            summary = _explicit_text(item.get("text"))
            return "answer", redact_secrets(summary) if summary else None
    if event_type == "assistant":
        message = payload.get("message")
        if isinstance(message, dict):
            summary = _explicit_text(message.get("content"))
            return "activity", redact_secrets(summary) if summary else None
    return "activity", event_type[:160] or None


class ManagedAgentSession:
    """Own one exact child process and a bounded, redacted event buffer."""

    def __init__(
        self,
        launch: ManagedAgentLaunch,
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.launch = launch.validated()
        self._popen = popen
        self._process: subprocess.Popen[bytes] | None = None
        self._state = "created"
        self._return_code: int | None = None
        self._events: deque[ManagedAgentEvent] = deque()
        self._captured_chars = 0
        self._raw_output_bytes = 0
        self._next_sequence = 1
        self._input_submitted = False
        self._started_utc: str | None = None
        self._ended_utc: str | None = None
        self._client_version: str | None = None
        self._observed_route: str | None = None
        self._observed_route_source: str | None = None
        self._model_id: str | None = None
        self._model_source: str | None = None
        self._usage_records: list[dict[str, Any]] = []
        self._usage_truncated = False
        self._provider_terminal_status: Literal["completed", "failed"] | None = None
        self._provider_terminal_reason: str | None = None
        self._provider_terminal_source: str | None = None
        self._acpx_request_methods: dict[str, str] = {}
        self._lock = threading.RLock()
        self._terminal = threading.Event()
        self._reader_threads: list[threading.Thread] = []

    @property
    def session_id(self) -> str:
        return self.launch.session_id

    def _append(
        self,
        stream: Literal["system", "stdout", "stderr"],
        kind: Literal["system", "terminal", "activity", "answer", "error"],
        text: str,
        summary: str | None = None,
    ) -> None:
        safe_text = redact_secrets(text).replace("\x00", "")
        safe_summary = redact_secrets(summary) if summary else None
        event = ManagedAgentEvent(
            sequence=self._next_sequence,
            created_utc=_utc_now(),
            stream=stream,
            kind=kind,
            text=safe_text,
            summary=safe_summary,
        )
        self._next_sequence += 1
        self._events.append(event)
        self._captured_chars += len(safe_text) + len(safe_summary or "")
        while (
            len(self._events) > MAX_EVENTS_PER_SESSION
            or self._captured_chars > MAX_CAPTURE_CHARS
        ):
            removed = self._events.popleft()
            self._captured_chars -= len(removed.text) + len(removed.summary or "")

    def start(self) -> None:
        with self._lock:
            if self._state != "created":
                raise ManagedAgentError("managed Agent session was already started")
            command = (str(self.launch.executable), *self.launch.arguments)
            environment_options = (
                {"env": dict(self.launch.environment)}
                if self.launch.environment is not None
                else {}
            )
            try:
                process = self._popen(
                    command,
                    cwd=(
                        self.launch.process_working_directory
                        or self.launch.working_directory
                    ),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    close_fds=True,
                    bufsize=0,
                    **environment_options,
                    **process_group_popen_kwargs(),
                )
                attach_process_tree(process)
            except OSError as exc:
                self._state = "failed"
                self._ended_utc = _utc_now()
                self._append("system", "error", "Managed Agent process could not start.")
                self._terminal.set()
                raise ManagedAgentError("managed Agent process could not start") from exc
            self._process = process
            self._state = "running"
            self._started_utc = _utc_now()
            self._append(
                "system",
                "system",
                "Managed session started for "
                f"{self.launch.display_name} in {self.launch.execution_mode} mode.",
            )
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                assert stream is not None
                reader = threading.Thread(
                    target=self._read_stream,
                    args=(name, stream),
                    name=f"peerbridge-{self.session_id}-{name}",
                    daemon=True,
                )
                self._reader_threads.append(reader)
                reader.start()
            threading.Thread(
                target=self._watch,
                name=f"peerbridge-{self.session_id}-watch",
                daemon=True,
            ).start()

    def _read_stream(
        self,
        name: Literal["stdout", "stderr"],
        stream: Any,
    ) -> None:
        while True:
            try:
                raw = stream.readline(MAX_LINE_BYTES + 1)
            except OSError:
                return
            if not raw:
                return
            with self._lock:
                self._raw_output_bytes += len(raw)
                exceeded = self._raw_output_bytes > MAX_RAW_OUTPUT_BYTES
                process = self._process
                if exceeded:
                    self._append(
                        "system",
                        "error",
                        "Managed output exceeded the cumulative raw byte limit.",
                    )
            if exceeded:
                if process is not None:
                    terminate_process_tree(process, wait_seconds=2)
                return
            if len(raw) > MAX_LINE_BYTES:
                while raw and not raw.endswith((b"\n", b"\r")):
                    try:
                        raw = stream.readline(MAX_LINE_BYTES + 1)
                    except OSError:
                        raw = b""
                with self._lock:
                    self._append(
                        "system",
                        "error",
                        f"{name} line exceeded the bounded capture limit and was omitted.",
                    )
                continue
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            safe_text = redact_secrets(text)
            kind, summary = _classify_output(name, safe_text)
            with self._lock:
                event_text = safe_text
                if name == "stdout":
                    try:
                        payload = json.loads(safe_text)
                    except json.JSONDecodeError:
                        payload = None
                    if isinstance(payload, dict):
                        self._observe_provider_event(payload)
                        if self.launch.agent_id in ACPX_AGENT_IDS:
                            event_text = self._acpx_event_text(payload, summary=summary)
                            if payload.get("jsonrpc") != "2.0":
                                kind = "error"
                                summary = "ACPX emitted an invalid JSON-RPC event"
                    elif self.launch.agent_id in ACPX_AGENT_IDS:
                        self._provider_terminal_status = "failed"
                        self._provider_terminal_reason = "malformed_jsonrpc"
                        self._provider_terminal_source = "acpx/json-rpc"
                        kind = "error"
                        summary = "ACPX emitted malformed JSON-RPC output"
                        event_text = "ACPX malformed stdout was omitted."
                self._append(name, kind, event_text, summary)

    def _record_usage(self, usage: dict[str, Any] | None) -> None:
        if usage is None:
            return
        if len(self._usage_records) >= MAX_USAGE_RECORDS_PER_SESSION:
            self._usage_truncated = True
            return
        self._usage_records.append(usage)

    def _acpx_event_method(
        self, payload: Mapping[str, Any], *, record: bool = False
    ) -> str | None:
        method = _observed_label(payload.get("method"), limit=80)
        request_key = _acpx_request_key(payload.get("id"))
        if method in ACPX_REQUEST_METHODS:
            if record and request_key is not None:
                existing = self._acpx_request_methods.get(request_key)
                if existing is not None and existing != method:
                    self._provider_terminal_status = "failed"
                    self._provider_terminal_reason = "ambiguous_request_id"
                    self._provider_terminal_source = "acpx/json-rpc"
                elif len(self._acpx_request_methods) < 16:
                    self._acpx_request_methods[request_key] = method
            return method
        return self._acpx_request_methods.get(request_key or "")

    def _acpx_event_text(
        self, payload: Mapping[str, Any], *, summary: str | None
    ) -> str:
        assistant_text = _acpx_assistant_text(payload)
        if assistant_text:
            return redact_secrets(assistant_text)
        method = self._acpx_event_method(payload)
        if isinstance(payload.get("error"), Mapping):
            return f"ACPX {method or 'JSON-RPC'} request failed."
        result = payload.get("result")
        if method == "session/prompt" and isinstance(result, Mapping):
            stop_reason = _observed_label(result.get("stopReason"), limit=80)
            if stop_reason == "end_turn":
                return "ACPX turn completed."
            return f"ACPX turn failed ({stop_reason or 'stop reason unavailable'})."
        labels = {
            "initialize": "ACPX Agent initialization event observed.",
            "authenticate": "ACPX authentication handshake event observed.",
            "session/new": "ACPX session creation event observed.",
            "session/prompt": "ACPX single-turn request event observed.",
        }
        if method in labels:
            return labels[method]
        if payload.get("method") == "session/update":
            return "ACPX non-answer session update observed; content omitted."
        return summary or "ACPX JSON-RPC event observed."

    def _observe_acpx_event(self, payload: Mapping[str, Any]) -> None:
        if payload.get("jsonrpc") != "2.0":
            self._provider_terminal_status = "failed"
            self._provider_terminal_reason = "invalid_jsonrpc"
            self._provider_terminal_source = "acpx/json-rpc"
            return
        method = self._acpx_event_method(payload, record=True)
        if isinstance(payload.get("error"), Mapping):
            self._provider_terminal_status = "failed"
            self._provider_terminal_reason = f"{method or 'jsonrpc'}.error"
            self._provider_terminal_source = "acpx/json-rpc"
            return
        result = payload.get("result")
        if not isinstance(result, Mapping):
            return
        if method == "initialize":
            agent_info = result.get("agentInfo")
            version = (
                _observed_label(agent_info.get("version"), limit=80)
                if isinstance(agent_info, Mapping)
                else None
            )
            if version is None:
                metadata = result.get("_meta")
                version = (
                    _observed_label(metadata.get("agentVersion"), limit=80)
                    if isinstance(metadata, Mapping)
                    else None
                )
            if version is not None:
                self._client_version = version
            return
        if method == "session/new":
            model, route = _acpx_model_evidence(result)
            if model is not None:
                self._model_id = model
                self._model_source = "acpx/acp-session-new"
            if route is not None:
                self._observed_route = route
                self._observed_route_source = "acpx/acp-session-new"
            return
        if method != "session/prompt":
            return
        self._record_usage(_acpx_usage(result))
        stop_reason = _observed_label(result.get("stopReason"), limit=80)
        if (
            self._provider_terminal_status == "failed"
            and self._provider_terminal_source == "acpx/json-rpc"
        ):
            return
        self._provider_terminal_status = (
            "completed" if stop_reason == "end_turn" else "failed"
        )
        self._provider_terminal_reason = stop_reason or "missing_stop_reason"
        self._provider_terminal_source = "acpx/acp-session-prompt"

    def _observe_provider_event(self, payload: Mapping[str, Any]) -> None:
        if self.launch.agent_id in ACPX_AGENT_IDS:
            self._observe_acpx_event(payload)
            return
        event_type = str(payload.get("type") or "")
        if self.launch.agent_id == "codex":
            if event_type == "turn.completed":
                self._record_usage(_codex_usage(payload))
                self._provider_terminal_status = "completed"
                self._provider_terminal_reason = "turn.completed"
                self._provider_terminal_source = "codex-exec-jsonl"
            elif event_type in {"turn.failed", "error"}:
                self._provider_terminal_status = "failed"
                self._provider_terminal_reason = event_type
                self._provider_terminal_source = "codex-exec-jsonl"
            return

        if self.launch.agent_id != "claude-code":
            return
        if event_type == "system" and payload.get("subtype") == "init":
            model = _observed_label(payload.get("model"))
            if model:
                self._model_id = model
                self._model_source = "claude-stream-json:system-init"
            version = _observed_label(payload.get("claude_code_version"), limit=80)
            if version:
                self._client_version = version
            return
        if event_type != "result":
            return
        self._record_usage(_claude_usage(payload))
        model_usage = payload.get("modelUsage")
        if self._model_id is None and isinstance(model_usage, Mapping):
            observed_models = [
                model
                for model in (_observed_label(value) for value in model_usage)
                if model is not None
            ]
            if len(observed_models) == 1:
                self._model_id = observed_models[0]
                self._model_source = "claude-stream-json:model-usage"
        successful = (
            payload.get("subtype") == "success"
            and payload.get("is_error") is False
            and payload.get("terminal_reason") == "completed"
        )
        self._provider_terminal_status = "completed" if successful else "failed"
        self._provider_terminal_reason = (
            _observed_label(payload.get("terminal_reason"), limit=80)
            or _observed_label(payload.get("subtype"), limit=80)
            or "result"
        )
        self._provider_terminal_source = "claude-stream-json"

    def _usage_snapshot(self) -> dict[str, Any]:
        if not self._usage_records:
            return unavailable_usage("managed-agent-event-not-reported")
        return aggregate_usage(
            self._usage_records,
            source=f"{self.launch.agent_id}-managed-session",
        )

    def _terminal_outcome(self) -> dict[str, Any]:
        process_status = self._state if self._state in TERMINAL_STATES else None
        provider_status = self._provider_terminal_status
        if process_status is None:
            status = "unavailable"
            source = "session-not-terminal"
        elif provider_status is None:
            status = process_status
            source = "managed-process-exit"
        elif provider_status == process_status:
            status = process_status
            source = "provider-event+managed-process-exit"
        else:
            status = "conflict"
            source = "provider-event+managed-process-exit"
        return {
            "status": status,
            "source": source,
            "process_status": process_status,
            "exit_code": self._return_code,
            "provider_status": provider_status,
            "provider_reason": self._provider_terminal_reason,
            "provider_source": self._provider_terminal_source,
        }

    def _watch(self) -> None:
        process = self._process
        assert process is not None
        return_code = process.wait()
        release_process_tree(process)
        drain_deadline = time.monotonic() + MAX_OUTPUT_DRAIN_SECONDS
        for reader in tuple(self._reader_threads):
            remaining = max(0.0, drain_deadline - time.monotonic())
            reader.join(timeout=remaining)
        readers_drained = all(not reader.is_alive() for reader in self._reader_threads)
        with self._lock:
            self._return_code = int(return_code)
            self._ended_utc = _utc_now()
            if self._state == "stopping":
                self._state = "stopped"
            elif return_code == 0:
                self._state = "completed"
            else:
                self._state = "failed"
            self._append(
                "system",
                "system" if return_code == 0 else "error",
                f"Managed session ended with exit code {return_code}.",
            )
            if not readers_drained:
                self._append(
                    "system",
                    "error",
                    "Managed output did not close within the bounded drain period.",
                )
            self._terminal.set()

    def submit(self, text: str) -> None:
        payload = str(text or "")
        encoded = payload.encode("utf-8")
        if not payload.strip() or len(encoded) > MAX_INPUT_BYTES or "\x00" in payload:
            raise ManagedAgentError("managed Agent input is invalid or too large")
        if contains_secret(payload):
            raise ManagedAgentError("managed Agent input contains credential-like data")
        with self._lock:
            if self._state != "running" or self._process is None:
                raise ManagedAgentError("managed Agent session is not running")
            if self._input_submitted:
                raise ManagedAgentError("single-input managed Agent already received work")
            process = self._process
            stdin = process.stdin
            if stdin is None or stdin.closed:
                raise ManagedAgentError("managed Agent input stream is unavailable")
            try:
                write_process_stdin_bounded(
                    process,
                    encoded + b"\n",
                    close_after=True,
                )
            except (OSError, RuntimeError, TimeoutError) as exc:
                raise ManagedAgentError("managed Agent input could not be delivered") from exc
            self._input_submitted = True
            self._append(
                "system",
                "system",
                f"Input delivered ({len(encoded)} UTF-8 bytes); content was not recorded.",
            )

    def interrupt(self) -> None:
        with self._lock:
            process = self._process
            if self._state != "running" or process is None or process.poll() is not None:
                raise ManagedAgentError("managed Agent session is not running")
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(process.pid, signal.SIGINT)
            except (OSError, ValueError) as exc:
                raise ManagedAgentError("managed Agent interrupt was unavailable") from exc
            self._append("system", "system", "Interrupt requested by the operator.")

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if self._state in TERMINAL_STATES:
                return
            if self._state == "created" or process is None:
                self._state = "stopped"
                self._ended_utc = _utc_now()
                self._terminal.set()
                return
            self._state = "stopping"
            self._append("system", "system", "Stop requested by the operator.")
        terminate_process_tree(process, wait_seconds=5)

    def wait(self, timeout: float = 30.0) -> bool:
        return self._terminal.wait(max(0.0, float(timeout)))

    def snapshot(self, *, after_sequence: int = 0) -> dict[str, Any]:
        with self._lock:
            if self._state not in SESSION_STATES:
                raise ManagedAgentError("managed Agent session state is invalid")
            events = [
                asdict(event)
                for event in self._events
                if event.sequence > int(after_sequence)
            ]
            return {
                "session_id": self.session_id,
                "agent_id": self.launch.agent_id,
                "display_name": self.launch.display_name,
                "client_name": self.launch.agent_id,
                "client_version": self._client_version,
                "role": self.launch.role,
                "working_directory": redact_secrets(
                    str(self.launch.working_directory)
                ),
                "state": self._state,
                "return_code": self._return_code,
                "started_utc": self._started_utc,
                "ended_utc": self._ended_utc,
                "input_submitted": self._input_submitted,
                "input_mode": self.launch.input_mode,
                "can_submit_input": (
                    self._state == "running"
                    and not self._input_submitted
                    and self.launch.input_mode == "single"
                ),
                "session_contract": {
                    "mode": "one_shot",
                    "input_transport": "stdin_once",
                    "additional_input_supported": False,
                    "resume_supported": False,
                    "process_terminal_after_turn": True,
                },
                "requested_route": self.launch.requested_route,
                "observed_route": self._observed_route,
                "observed_route_source": self._observed_route_source,
                "model_id": self._model_id,
                "model_source": self._model_source,
                "usage": self._usage_snapshot(),
                "usage_capture_bounded": True,
                "usage_capture_truncated": self._usage_truncated,
                "terminal_outcome": self._terminal_outcome(),
                "execution_mode": self.launch.execution_mode,
                "permission_tier": self.launch.permission_tier,
                "governance_binding_id": self.launch.governance_binding_id,
                "isolation_boundary": self.launch.isolation_boundary,
                "capture_mode": "managed-pipes",
                "reasoning_contract": "observable-output-only",
                "first_retained_sequence": (
                    self._events[0].sequence if self._events else self._next_sequence
                ),
                "latest_sequence": self._next_sequence - 1,
                "events": events,
            }


class ManagedAgentManager:
    """Own a bounded set of stable-identity managed sessions."""

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        max_retained_sessions: int = MAX_RETAINED_SESSIONS,
    ) -> None:
        if not 1 <= int(max_sessions) <= MAX_SESSIONS:
            raise ManagedAgentError("managed Agent session limit is invalid")
        if not int(max_sessions) <= int(max_retained_sessions) <= MAX_RETAINED_SESSIONS:
            raise ManagedAgentError("managed Agent retained session limit is invalid")
        self.max_sessions = int(max_sessions)
        self.max_retained_sessions = int(max_retained_sessions)
        self._sessions: dict[str, ManagedAgentSession] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _prune_terminal_sessions(self) -> None:
        for session_id, session in tuple(self._sessions.items()):
            if len(self._sessions) < self.max_retained_sessions:
                return
            if session.snapshot()["state"] in TERMINAL_STATES:
                del self._sessions[session_id]

    def prune_one_terminal(self) -> bool:
        with self._lock:
            for session_id, session in tuple(self._sessions.items()):
                if session.snapshot()["state"] in TERMINAL_STATES:
                    del self._sessions[session_id]
                    return True
        return False

    def counts(self) -> tuple[int, int]:
        with self._lock:
            sessions = tuple(self._sessions.values())
        active = sum(
            session.snapshot()["state"] not in TERMINAL_STATES
            for session in sessions
        )
        return active, len(sessions)

    def start(
        self,
        launch: ManagedAgentLaunch,
        *,
        input_text: str | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> ManagedAgentSession:
        validated = launch.validated()
        with self._lock:
            if self._closed:
                raise ManagedAgentError("managed Agent manager is closed")
            if validated.session_id in self._sessions:
                raise ManagedAgentError("managed Agent session id already exists")
            self._prune_terminal_sessions()
            if len(self._sessions) >= self.max_retained_sessions:
                raise ManagedAgentError("managed Agent retained session limit reached")
            active = sum(
                session.snapshot()["state"] not in TERMINAL_STATES
                for session in self._sessions.values()
            )
            if active >= self.max_sessions:
                raise ManagedAgentError("managed Agent session limit reached")
            session = ManagedAgentSession(validated, popen=popen)
            self._sessions[validated.session_id] = session
        try:
            session.start()
            if input_text is not None:
                session.submit(input_text)
        except Exception:
            with contextlib.suppress(ManagedAgentError):
                session.stop()
            raise
        return session

    def get(self, session_id: str) -> ManagedAgentSession:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError as exc:
                raise ManagedAgentError("managed Agent session does not exist") from exc

    def snapshots(
        self, *, after_sequences: Mapping[str, int] | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            sessions = tuple(self._sessions.values())
        positions = after_sequences or {}
        return [
            session.snapshot(after_sequence=max(0, int(positions.get(session.session_id, 0))))
            for session in sessions
        ]

    def stop_all(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
        for session in sessions:
            with contextlib.suppress(ManagedAgentError):
                session.stop()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop_all()


__all__ = [
    "MAX_RETAINED_SESSIONS",
    "NON_CODEX_WRITE_UNAVAILABLE_REASON",
    "ManagedAgentError",
    "ManagedAgentEvent",
    "ManagedAgentLaunch",
    "ManagedAgentManager",
    "ManagedAgentSession",
    "ManagedAgentUnavailableError",
    "build_managed_launch",
    "build_observe_launch",
    "build_wsl_write_launch",
]
