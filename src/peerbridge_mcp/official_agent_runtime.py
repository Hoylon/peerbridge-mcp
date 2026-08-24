"""Persistent official Agent runtimes for the PeerBridge local workbench.

The adapters in this module intentionally keep provider identities and native
protocols separate:

* Codex uses the official ``app-server`` JSON-RPC transport.
* Claude Code uses its official bidirectional ``stream-json`` transport.
* Grok uses an ACPX named session backed by ``grok agent stdio``.
* Kimi Code uses an ACPX named session backed by its official ACP runtime.

No prompt, credential, or hidden model state is persisted by PeerBridge.  The
bounded event buffers contain only redacted observable output.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from .agent_install import ACPX_RUNTIME_SPEC, find_trusted_executable, official_agent_spec
from .attachments import (
    AttachmentError,
    StagedAttachment,
    stage_chat_attachment_payloads,
)
from .child_environment import build_agent_child_environment
from .managed_agents import (
    MAX_CAPTURE_CHARS,
    MAX_EVENTS_PER_SESSION,
    MAX_INPUT_BYTES,
    MAX_RETAINED_SESSIONS,
    MAX_SESSIONS,
    TERMINAL_STATES,
    ManagedAgentError,
    ManagedAgentEvent,
    ManagedAgentManager,
    build_wsl_write_launch,
)
from .process_control import (
    attach_process_tree,
    process_group_popen_kwargs,
    release_process_tree,
    terminate_process_tree,
    write_process_stdin_bounded,
)
from .multimodal import (
    VerifiedAttachment,
    VisionChallenge,
    acp_native_turn_payload,
    attachment_delivery_receipt,
    attachment_path_instruction,
    claude_native_content_blocks,
    create_vision_challenge,
    verify_staged_attachments,
    vision_verification_receipt,
)
from .openai_compatible_runner import ResourceUnavailableError, RunCancelledError
from .secret_scan import contains_secret, redact_secrets
from .usage import aggregate_usage, normalize_provider_usage, unavailable_usage


SESSION_STATES = frozenset(
    {"created", "running", "stopping", "completed", "failed", "stopped"}
)
OFFICIAL_PERSISTENT_AGENT_IDS = frozenset(
    {"codex", "claude-code", "grok", "kimi-code"}
)
REQUEST_TIMEOUT_SECONDS = 45.0
PERMISSION_TIERS = frozenset({"observe", "review", "edit", "full-development"})
WRITE_PERMISSION_TIERS = frozenset({"edit", "full-development"})
MAX_PROVIDER_FRAME_BYTES = 1024 * 1024
MAX_PROVIDER_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_ACP_CAPTURE_BYTES = 4 * 1024 * 1024


def _bounded_acp_process(*args: Any, **kwargs: Any) -> tuple[int, bytes, bytes]:
    """Load the shared runner lazily so lock helpers do not require a home directory."""

    from .ccswitch_runner import _bounded_process

    return _bounded_process(*args, **kwargs)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_text(text: str) -> tuple[str, bytes]:
    payload = str(text or "")
    encoded = payload.encode("utf-8")
    if not payload.strip() or len(encoded) > MAX_INPUT_BYTES or "\x00" in payload:
        raise ManagedAgentError("managed Agent input is invalid or too large")
    if contains_secret(payload):
        raise ManagedAgentError("managed Agent input contains credential-like data")
    return payload, encoded


def _validate_composed_text(
    user_text: str,
    composed_text: str,
    *,
    attachment_present: bool,
) -> tuple[str, bytes]:
    """Validate operator text while allowing a trusted attachment appendix."""

    raw = str(user_text or "")
    if raw.strip():
        _validate_text(raw)
    elif not attachment_present:
        _validate_text(raw)
    payload = str(composed_text or "")
    encoded = payload.encode("utf-8")
    if not payload.strip() or len(encoded) > MAX_INPUT_BYTES or "\x00" in payload:
        raise ManagedAgentError("managed Agent input is invalid or too large")
    return payload, encoded


def _safe_label(value: str, label: str, *, limit: int = 200) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > limit:
        raise ManagedAgentError(f"{label} is invalid")
    if any(ord(character) < 32 for character in normalized):
        raise ManagedAgentError(f"{label} contains a control character")
    if contains_secret(normalized):
        raise ManagedAgentError(f"{label} contains credential-like data")
    return normalized


def _command_for(executable: Path, *arguments: str) -> tuple[str, ...]:
    """Return a hidden-Popen-compatible command, including Windows shims."""

    if os.name == "nt" and executable.suffix.lower() in {".cmd", ".bat"}:
        command_processor = os.environ.get("COMSPEC") or "cmd.exe"
        return (
            command_processor,
            "/d",
            "/s",
            "/c",
            str(executable),
            *arguments,
        )
    return (str(executable), *arguments)


def _spawn_owned(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    popen: Callable[..., subprocess.Popen[bytes]],
) -> subprocess.Popen[bytes]:
    try:
        process = popen(
            command,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            bufsize=0,
            **process_group_popen_kwargs(),
        )
        attach_process_tree(process)
        return process
    except OSError as exc:
        raise ManagedAgentError("official Agent process could not start") from exc


class _PersistentSessionBase:
    """Shared bounded state and observable-event contract."""

    agent_id = ""
    display_name = ""
    provider_family = ""
    protocol = ""
    supports_verified_attachments = True

    def __init__(
        self,
        *,
        session_id: str,
        role: str,
        working_directory: Path,
        requested_route: str | None,
        permission_tier: Literal[
            "observe", "review", "edit", "full-development"
        ],
        governance_binding_id: str | None,
        project_root: Path | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.session_id = _safe_label(session_id, "session id")
        self.role = _safe_label(role, "role", limit=80)
        self.working_directory = Path(working_directory).resolve()
        if not self.working_directory.is_dir():
            raise ManagedAgentError("managed Agent working directory is unavailable")
        self.project_root = Path(project_root or self.working_directory).resolve()
        if not self.project_root.is_dir():
            raise ManagedAgentError("managed Agent project root is unavailable")
        if permission_tier not in PERMISSION_TIERS:
            raise ManagedAgentError("official persistent runtime permission tier is invalid")
        governed_write = permission_tier in WRITE_PERMISSION_TIERS
        if governed_write and not governance_binding_id:
            raise ManagedAgentError(
                "write-capable official runtime requires a governed worktree binding"
            )
        if not governed_write and governance_binding_id:
            raise ManagedAgentError(
                "read-only official runtime must not claim a write binding"
            )
        try:
            self.working_directory.relative_to(self.project_root)
        except ValueError as exc:
            if not governed_write or not governance_binding_id:
                raise ManagedAgentError(
                    "managed Agent working directory escapes the project root"
                ) from exc
        self.requested_route = (
            _safe_label(requested_route, "requested route") if requested_route else None
        )
        self.permission_tier = permission_tier
        self.governance_binding_id = (
            _safe_label(governance_binding_id, "governance binding id")
            if governance_binding_id
            else None
        )
        self._popen = popen
        self._process: subprocess.Popen[bytes] | None = None
        self._state = "created"
        self._return_code: int | None = None
        self._events: deque[ManagedAgentEvent] = deque()
        self._next_sequence = 1
        self._captured_chars = 0
        self._started_utc: str | None = None
        self._ended_utc: str | None = None
        self._client_version: str | None = None
        self._observed_route: str | None = None
        self._observed_route_source: str | None = None
        self._model_id: str | None = None
        self._model_source: str | None = None
        self._usage_records: list[dict[str, Any]] = []
        self._attachment_delivery_receipts: deque[dict[str, object]] = deque(
            maxlen=32
        )
        self._vision_verification_receipts: deque[dict[str, object]] = deque(
            maxlen=16
        )
        self._pending_vision_challenge: VisionChallenge | None = None
        self._busy = False
        self._turn_count = 0
        self._provider_output_bytes = 0
        self._provider_output_limit_exceeded = False
        self._lock = threading.RLock()
        self._terminal = threading.Event()

    def _append(
        self,
        stream: Literal["system", "stdout", "stderr"],
        kind: Literal["system", "terminal", "activity", "answer", "error"],
        text: str,
        summary: str | None = None,
    ) -> None:
        safe_text = redact_secrets(str(text or "")).replace("\x00", "")
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

    def _record_usage(self, usage: dict[str, Any]) -> None:
        if usage.get("status") != "unavailable":
            self._usage_records.append(usage)
            if len(self._usage_records) > 32:
                del self._usage_records[:-32]

    def _accept_provider_output(
        self,
        process: subprocess.Popen[bytes],
        byte_count: int,
        label: str,
    ) -> bool:
        """Account for raw provider bytes before parsing or retaining them."""

        count = max(0, int(byte_count))
        with self._lock:
            self._provider_output_bytes += count
            within_limit = self._provider_output_bytes <= MAX_PROVIDER_OUTPUT_BYTES
            if not within_limit and not self._provider_output_limit_exceeded:
                self._provider_output_limit_exceeded = True
                self._append(
                    "system",
                    "error",
                    f"{label} exceeded the bounded provider output budget.",
                )
        if not within_limit:
            terminate_process_tree(process, wait_seconds=2)
        return within_limit

    def _usage_snapshot(self) -> dict[str, Any]:
        if not self._usage_records:
            return unavailable_usage(f"{self.agent_id}-persistent-session-not-reported")
        return aggregate_usage(
            self._usage_records,
            source=f"{self.agent_id}-persistent-session",
        )

    def _terminal_outcome(self) -> dict[str, Any]:
        process_status = self._state if self._state in TERMINAL_STATES else None
        return {
            "status": process_status or "unavailable",
            "source": "owned-native-runtime" if process_status else "session-not-terminal",
            "process_status": process_status,
            "exit_code": self._return_code,
            "provider_status": None,
            "provider_reason": None,
            "provider_source": self.protocol,
        }

    def _contract(self) -> dict[str, Any]:
        raise NotImplementedError

    def _multimodal_capability(self) -> dict[str, object]:
        return {
            "attachment_input_supported": True,
            "image_input_supported": True,
            "image_input": "verified_path",
            "audio_input_supported": False,
            "audio_input": "unavailable",
            "text_file_input": "verified_path",
            "model_view_confirmation": self._vision_was_verified(),
            "semantic_image_verification": self._vision_verification_status(),
        }

    def _sandbox_name(self) -> str:
        return (
            "workspace-write"
            if self.permission_tier in WRITE_PERMISSION_TIERS
            else "read-only"
        )

    def _sandbox_policy(self) -> dict[str, object]:
        if self.permission_tier not in WRITE_PERMISSION_TIERS:
            return {"type": "readOnly", "networkAccess": False}
        return {
            "type": "workspaceWrite",
            "writableRoots": [str(self.working_directory)],
            "networkAccess": True,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }

    def _approval_policy(self) -> str:
        if self.permission_tier == "edit":
            return "on-request"
        return "never"

    def _developer_instructions(self) -> str:
        if self.permission_tier not in WRITE_PERMISSION_TIERS:
            return (
                "Operate read-only. Do not edit files, invoke paid tools, or expose "
                "credentials. Return observable conclusions only."
            )
        return (
            "Work only inside the PeerBridge-governed isolated Git worktree. Do not "
            "access credentials or unrelated paths. Keep changes scoped, run relevant "
            "checks, and leave merge or publication to the human operator."
        )

    def _vision_was_verified(self) -> bool:
        return any(
            receipt.get("model_view_confirmed") is True
            for receipt in self._vision_verification_receipts
        )

    def _vision_verification_status(self) -> str:
        if self._pending_vision_challenge is not None:
            return "semantic_image_pending"
        if not self._vision_verification_receipts:
            return "available"
        return str(self._vision_verification_receipts[-1].get("status") or "available")

    def _finalize_vision_probe(
        self,
        answer: str,
        *,
        failure_status: str = "semantic_image_failed",
    ) -> None:
        challenge = self._pending_vision_challenge
        if challenge is None:
            return
        capability = self._multimodal_capability()
        receipt = vision_verification_receipt(
            challenge=challenge,
            answer=answer,
            provider_id=self.agent_id,
            protocol=self.protocol,
            delivery_mode=str(capability.get("image_input") or "unknown"),
            provider_identity=self._observed_route,
            model_id=self._model_id or self.requested_route,
            client_version=self._client_version,
            failure_status=failure_status,
        )
        self._vision_verification_receipts.append(receipt)
        self._pending_vision_challenge = None
        self._append(
            "system",
            "system" if receipt["model_view_confirmed"] else "error",
            (
                "Semantic image verification passed."
                if receipt["model_view_confirmed"]
                else "Semantic image verification did not pass."
            ),
        )

    def _append_visible_answer(self, answer: str) -> None:
        """Keep one-use vision challenge answers out of retained session output."""

        if answer and self._pending_vision_challenge is None:
            self._append("stdout", "answer", answer)

    def start_vision_probe(self) -> str:
        """Send a one-use image-only answer challenge through this runtime."""

        with self._lock:
            if self._state != "running" or self._busy:
                raise ManagedAgentError("managed Agent cannot verify vision now")
            if self._pending_vision_challenge is not None:
                raise ManagedAgentError("a semantic image verification is already pending")
        challenge = create_vision_challenge()
        try:
            staged = stage_chat_attachment_payloads(
                self.project_root,
                (("peerbridge-vision-check.png", challenge.png),),
            )
        except AttachmentError as exc:
            raise ManagedAgentError(str(exc)) from exc
        with self._lock:
            self._pending_vision_challenge = challenge
        try:
            self.submit(challenge.prompt, attachments=staged)
        except Exception:
            with self._lock:
                self._finalize_vision_probe(
                    "", failure_status="semantic_image_delivery_failed"
                )
            raise
        return challenge.challenge_id

    def _verify_attachments(
        self,
        attachments: Iterable[StagedAttachment | Mapping[str, object]],
    ) -> tuple[VerifiedAttachment, ...]:
        try:
            return verify_staged_attachments(self.project_root, attachments)
        except AttachmentError as exc:
            raise ManagedAgentError(str(exc)) from exc

    def _record_attachment_delivery(
        self,
        *,
        delivery_mode: str,
        status: str,
        attachments: Iterable[VerifiedAttachment],
    ) -> None:
        rows = tuple(attachments)
        if not rows:
            return
        self._attachment_delivery_receipts.append(
            attachment_delivery_receipt(
                provider_id=self.agent_id,
                protocol=self.protocol,
                delivery_mode=delivery_mode,
                status=status,
                attachments=rows,
            )
        )

    def snapshot(self, *, after_sequence: int = 0) -> dict[str, Any]:
        with self._lock:
            if self._state not in SESSION_STATES:
                raise ManagedAgentError("managed Agent session state is invalid")
            contract = self._contract()
            events = [
                asdict(event)
                for event in self._events
                if event.sequence > max(0, int(after_sequence))
            ]
            return {
                "session_id": self.session_id,
                "agent_id": self.agent_id,
                "display_name": self.display_name,
                "client_name": self.agent_id,
                "client_version": self._client_version,
                "role": self.role,
                "working_directory": redact_secrets(str(self.working_directory)),
                "state": self._state,
                "return_code": self._return_code,
                "started_utc": self._started_utc,
                "ended_utc": self._ended_utc,
                "input_submitted": self._turn_count > 0,
                "input_mode": "persistent",
                "can_submit_input": self._state == "running" and not self._busy,
                "session_contract": contract,
                "requested_route": self.requested_route,
                "observed_route": self._observed_route,
                "observed_route_source": self._observed_route_source,
                "model_id": self._model_id,
                "model_source": self._model_source,
                "usage": self._usage_snapshot(),
                "usage_capture_bounded": True,
                "usage_capture_truncated": False,
                "provider_output_bytes": self._provider_output_bytes,
                "provider_output_limit_bytes": MAX_PROVIDER_OUTPUT_BYTES,
                "provider_output_limit_exceeded": self._provider_output_limit_exceeded,
                "terminal_outcome": self._terminal_outcome(),
                "execution_mode": self.permission_tier,
                "permission_tier": self.permission_tier,
                "governance_binding_id": self.governance_binding_id,
                "capture_mode": "managed-pipes",
                "reasoning_contract": "observable-output-only",
                "multimodal_capability": self._multimodal_capability(),
                "attachment_delivery_receipts": list(
                    self._attachment_delivery_receipts
                ),
                "vision_verification_receipts": list(
                    self._vision_verification_receipts
                ),
                "first_retained_sequence": (
                    self._events[0].sequence if self._events else self._next_sequence
                ),
                "latest_sequence": self._next_sequence - 1,
                "events": events,
            }

    def wait(self, timeout: float = 30.0) -> bool:
        return self._terminal.wait(max(0.0, float(timeout)))

    def compact(self) -> None:
        raise ManagedAgentError("native compact is unavailable for this Agent")

    def review(self, instructions: str | None = None) -> None:
        raise ManagedAgentError("native review is unavailable for this Agent")

    def fork(self) -> None:
        raise ManagedAgentError("native session fork is unavailable for this Agent")

    def resume(self) -> None:
        raise ManagedAgentError("native session resume is unavailable for this Agent")


class CodexAppServerSession(_PersistentSessionBase):
    """A durable Codex thread over the official app-server JSON-RPC API."""

    agent_id = "codex"
    display_name = "OpenAI Codex"
    provider_family = "codex"
    protocol = "codex-app-server-jsonrpc"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._request_id = 1
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._write_lock = threading.Lock()
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._answer_fragments: list[str] = []
        self._generation = 0

    def _contract(self) -> dict[str, Any]:
        return {
            "mode": "persistent",
            "protocol": self.protocol,
            "input_transport": "jsonrpc",
            "additional_input_supported": True,
            "resume_supported": True,
            "fork_supported": True,
            "compact_supported": True,
            "review_supported": True,
            "interrupt_supported": True,
            "process_terminal_after_turn": False,
            "provider_identity": "openai-official-codex",
        }

    def _multimodal_capability(self) -> dict[str, object]:
        return {
            "attachment_input_supported": True,
            "image_input_supported": True,
            "image_input": "native_local_image",
            "audio_input_supported": False,
            "audio_input": "unavailable",
            "text_file_input": "verified_path",
            "model_view_confirmation": self._vision_was_verified(),
            "semantic_image_verification": self._vision_verification_status(),
        }

    def start(self) -> None:
        with self._lock:
            if self._state != "created":
                raise ManagedAgentError("Codex app-server session was already started")
        self._launch(resume=False)

    def _launch(self, *, resume: bool) -> None:
        executable = find_trusted_executable(official_agent_spec("codex"))
        if executable is None:
            raise ManagedAgentError("official Codex CLI is unavailable")
        environment = _official_child_environment(
            "codex", required_path_roots=(executable.parent,)
        )
        process = _spawn_owned(
            _command_for(executable, "app-server", "--listen", "stdio://"),
            cwd=self.working_directory,
            environment=environment,
            popen=self._popen,
        )
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._process = process
            self._state = "running"
            self._return_code = None
            self._ended_utc = None
            self._started_utc = self._started_utc or _utc_now()
            self._terminal.clear()
        threading.Thread(
            target=self._read_stdout,
            args=(process, generation),
            name=f"peerbridge-{self.session_id}-codex-rpc",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(process, generation),
            name=f"peerbridge-{self.session_id}-codex-stderr",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._watch_process,
            args=(process, generation),
            name=f"peerbridge-{self.session_id}-codex-watch",
            daemon=True,
        ).start()
        self._request(
            "initialize",
            {
                "clientInfo": {"name": "PeerBridge", "version": "5.2"},
                "capabilities": {"experimentalApi": False},
            },
        )
        self._notify("initialized", {})
        if resume and self._thread_id:
            response = self._request("thread/resume", {"threadId": self._thread_id})
        else:
            response = self._request(
                "thread/start",
                {
                    "cwd": str(self.working_directory),
                    "model": self.requested_route,
                    "approvalPolicy": self._approval_policy(),
                    "approvalsReviewer": "user",
                    "sandbox": self._sandbox_name(),
                    "ephemeral": False,
                    "developerInstructions": self._developer_instructions(),
                },
            )
        thread = response.get("thread") if isinstance(response, Mapping) else None
        thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise ManagedAgentError("Codex app-server did not return a thread identity")
        with self._lock:
            self._thread_id = thread_id
            model = response.get("model") if isinstance(response, Mapping) else None
            if isinstance(model, str) and model:
                self._model_id = model
                self._model_source = "codex-app-server:thread"
            provider = (
                response.get("modelProvider") if isinstance(response, Mapping) else None
            )
            if isinstance(provider, str) and provider:
                self._observed_route = provider
                self._observed_route_source = "codex-app-server:thread"
            self._append(
                "system",
                "system",
                "Official Codex app-server thread is ready for multiple turns.",
            )

    def _send(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.stdin.closed:
            raise ManagedAgentError("Codex app-server input stream is unavailable")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ) + b"\n"
        try:
            with self._write_lock:
                write_process_stdin_bounded(process, encoded)
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise ManagedAgentError("Codex app-server request could not be delivered") from exc

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        with self._lock:
            request_id = self._request_id
            self._request_id += 1
            event = threading.Event()
            holder: dict[str, Any] = {}
            self._pending[request_id] = (event, holder)
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            if not event.wait(max(1.0, float(timeout))):
                raise ManagedAgentError(f"Codex app-server {method} timed out")
            if "error" in holder:
                raw_error = holder.get("error")
                detail = ""
                if isinstance(raw_error, Mapping):
                    code = raw_error.get("code")
                    message = redact_secrets(str(raw_error.get("message") or ""))
                    detail = f"code={code}; {message}"[:240]
                raise ManagedAgentError(
                    f"Codex app-server rejected {method}"
                    + (f": {detail}" if detail else "")
                )
            result = holder.get("result")
            return dict(result) if isinstance(result, Mapping) else {}
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _read_stdout(
        self, process: subprocess.Popen[bytes], generation: int
    ) -> None:
        stream = process.stdout
        if stream is None:
            return
        while True:
            raw = stream.readline(MAX_PROVIDER_FRAME_BYTES + 1)
            if not raw:
                return
            if not self._accept_provider_output(process, len(raw), "Codex"):
                return
            if len(raw) > MAX_PROVIDER_FRAME_BYTES:
                with self._lock:
                    self._append("system", "error", "Codex JSON-RPC frame exceeded limit.")
                terminate_process_tree(process, wait_seconds=2)
                return
            try:
                payload = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                with self._lock:
                    self._append("system", "error", "Codex emitted malformed JSON-RPC.")
                continue
            if not isinstance(payload, Mapping):
                continue
            response_id = payload.get("id")
            if isinstance(response_id, int) and (
                "result" in payload or "error" in payload
            ):
                with self._lock:
                    pending = self._pending.get(response_id)
                    if pending is not None:
                        _, holder = pending
                        if "error" in payload:
                            holder["error"] = payload.get("error")
                        else:
                            holder["result"] = payload.get("result")
                        pending[0].set()
                continue
            method = payload.get("method")
            params = payload.get("params")
            if isinstance(response_id, int) and isinstance(method, str):
                # PeerBridge has no hidden click-through path. Standard Agent mode
                # rejects an escalation; Full access is expected not to request one.
                denied = (
                    "PeerBridge standard Agent mode denied this escalation; "
                    "restart the managed session with Full access if it is intended"
                    if self.permission_tier == "edit"
                    else "PeerBridge read-only session denied the request"
                )
                with contextlib.suppress(ManagedAgentError):
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": response_id,
                            "error": {
                                "code": -32001,
                                "message": denied,
                            },
                        }
                    )
                continue
            if isinstance(method, str) and isinstance(params, Mapping):
                self._handle_notification(method, params, generation)

    def _handle_notification(
        self, method: str, params: Mapping[str, Any], generation: int
    ) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str):
                    self._answer_fragments.append(delta)
                return
            if method == "thread/tokenUsage/updated":
                raw_usage = params.get("tokenUsage")
                self._record_usage(
                    normalize_provider_usage(
                        raw_usage if isinstance(raw_usage, Mapping) else None,
                        source="codex-app-server:token-usage",
                    )
                )
                return
            if method == "turn/completed":
                answer = "".join(self._answer_fragments).strip()
                self._append_visible_answer(answer)
                self._finalize_vision_probe(answer)
                self._answer_fragments.clear()
                self._busy = False
                self._turn_id = None
                self._append("system", "terminal", "Codex turn completed.")
                return
            if method in {"turn/failed", "error"}:
                raw_error = params.get("error")
                error_info = (
                    str(raw_error.get("codexErrorInfo") or "")
                    if isinstance(raw_error, Mapping)
                    else ""
                )
                failure_labels = {
                    "usageLimitExceeded": "usage limit reached",
                    "rateLimitExceeded": "rate limited",
                    "contextWindowExceeded": "context window exceeded",
                }
                failure_label = failure_labels.get(error_info, "runtime error")
                self._finalize_vision_probe(
                    "", failure_status="semantic_image_runtime_failed"
                )
                self._busy = False
                self._turn_id = None
                self._append(
                    "system",
                    "error",
                    f"Codex turn failed ({failure_label}).",
                )

    def _read_stderr(
        self, process: subprocess.Popen[bytes], generation: int
    ) -> None:
        stream = process.stderr
        if stream is None:
            return
        while True:
            raw = stream.readline(MAX_PROVIDER_FRAME_BYTES + 1)
            if not raw:
                return
            if not self._accept_provider_output(process, len(raw), "Codex stderr"):
                return
            if len(raw) > MAX_PROVIDER_FRAME_BYTES:
                with self._lock:
                    self._append("system", "error", "Codex stderr frame exceeded limit.")
                terminate_process_tree(process, wait_seconds=2)
                return
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                with self._lock:
                    if generation == self._generation:
                        self._append("stderr", "activity", text)

    def _watch_process(
        self, process: subprocess.Popen[bytes], generation: int
    ) -> None:
        return_code = process.wait()
        release_process_tree(process)
        with self._lock:
            if generation != self._generation:
                return
            self._return_code = int(return_code)
            self._busy = False
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
                f"Codex app-server ended with exit code {return_code}.",
            )
            self._terminal.set()
            for event, holder in self._pending.values():
                holder["error"] = {"message": "app-server ended"}
                event.set()

    def submit(
        self,
        text: str,
        *,
        attachments: Iterable[StagedAttachment | Mapping[str, object]] = (),
    ) -> None:
        verified = self._verify_attachments(attachments)
        if any(item.kind == "audio" for item in verified):
            raise ManagedAgentError(
                "Codex app-server runtime does not advertise native audio input"
            )
        images = tuple(item for item in verified if item.kind == "image")
        path_attachments = tuple(item for item in verified if item.kind != "image")
        payload = str(text or "").strip()
        if not payload and verified:
            payload = "Inspect the supplied PeerBridge attachment evidence and respond to the task."
        path_appendix = attachment_path_instruction(
            path_attachments,
            working_directory=self.working_directory,
        )
        if path_appendix:
            payload = f"{payload.rstrip()}\n\n{path_appendix}"
        payload, encoded = _validate_composed_text(
            text,
            payload,
            attachment_present=bool(verified),
        )
        turn_input: list[dict[str, str]] = [{"type": "text", "text": payload}]
        turn_input.extend(
            {"type": "localImage", "path": str(item.absolute_path)}
            for item in images
        )
        with self._lock:
            if self._state != "running" or self._thread_id is None:
                raise ManagedAgentError("Codex app-server session is not running")
            if self._busy:
                raise ManagedAgentError("Codex app-server turn is already running")
            self._busy = True
            self._answer_fragments.clear()
            thread_id = self._thread_id
        try:
            response = self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": turn_input,
                    "approvalPolicy": self._approval_policy(),
                    "sandboxPolicy": self._sandbox_policy(),
                },
            )
            turn = response.get("turn") if isinstance(response, Mapping) else None
            turn_id = turn.get("id") if isinstance(turn, Mapping) else None
            with self._lock:
                self._turn_id = turn_id if isinstance(turn_id, str) else None
                self._turn_count += 1
                if images and path_attachments:
                    delivery_mode = "native_local_image_and_verified_path"
                elif images:
                    delivery_mode = "native_local_image"
                else:
                    delivery_mode = "verified_path"
                self._record_attachment_delivery(
                    delivery_mode=delivery_mode,
                    status="transport_accepted",
                    attachments=verified,
                )
                self._append(
                    "system",
                    "system",
                    f"Codex turn accepted ({len(encoded)} UTF-8 bytes); input not retained.",
                )
        except Exception:
            with self._lock:
                self._busy = False
            raise

    def interrupt(self) -> None:
        with self._lock:
            if not self._busy or not self._thread_id or not self._turn_id:
                raise ManagedAgentError("Codex turn is not running")
            params = {"threadId": self._thread_id, "turnId": self._turn_id}
        self._request("turn/interrupt", params)
        with self._lock:
            self._append("system", "system", "Codex turn interrupt requested.")

    def compact(self) -> None:
        with self._lock:
            if self._state != "running" or self._busy or not self._thread_id:
                raise ManagedAgentError("Codex thread cannot be compacted now")
            thread_id = self._thread_id
        self._request("thread/compact/start", {"threadId": thread_id})
        with self._lock:
            self._append("system", "system", "Codex native thread compaction started.")

    def review(self, instructions: str | None = None) -> None:
        with self._lock:
            if self._state != "running" or self._busy or not self._thread_id:
                raise ManagedAgentError("Codex review cannot start now")
            self._busy = True
            thread_id = self._thread_id
        target: dict[str, Any]
        if instructions and instructions.strip():
            payload, _ = _validate_text(instructions)
            target = {"type": "custom", "instructions": payload}
        else:
            target = {"type": "uncommittedChanges"}
        try:
            response = self._request(
                "review/start", {"threadId": thread_id, "target": target}
            )
            turn = response.get("turn") if isinstance(response, Mapping) else None
            turn_id = turn.get("id") if isinstance(turn, Mapping) else None
            with self._lock:
                self._turn_id = turn_id if isinstance(turn_id, str) else None
                self._turn_count += 1
                self._append("system", "system", "Codex native review started.")
        except Exception:
            with self._lock:
                self._busy = False
            raise

    def fork(self) -> None:
        with self._lock:
            if self._state != "running" or self._busy or not self._thread_id:
                raise ManagedAgentError("Codex thread cannot be forked now")
            thread_id = self._thread_id
        response = self._request("thread/fork", {"threadId": thread_id})
        thread = response.get("thread") if isinstance(response, Mapping) else None
        new_id = thread.get("id") if isinstance(thread, Mapping) else None
        if not isinstance(new_id, str) or not new_id:
            raise ManagedAgentError("Codex app-server did not return a fork identity")
        with self._lock:
            self._thread_id = new_id
            self._append("system", "system", "Codex thread forked; new branch selected.")

    def resume(self) -> None:
        with self._lock:
            if self._state not in TERMINAL_STATES or not self._thread_id:
                raise ManagedAgentError("Codex thread cannot be resumed now")
        self._launch(resume=True)

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if self._state in TERMINAL_STATES:
                return
            if process is None:
                self._state = "stopped"
                self._ended_utc = _utc_now()
                self._terminal.set()
                return
            self._state = "stopping"
            self._append("system", "system", "Codex app-server stop requested.")
        terminate_process_tree(process, wait_seconds=5)


class ClaudeStreamSession(_PersistentSessionBase):
    """A multi-turn Claude Code stream-json process."""

    agent_id = "claude-code"
    display_name = "Anthropic Claude Code"
    provider_family = "claude"
    protocol = "claude-stream-json"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._claude_session_id = str(uuid.uuid4())
        self._generation = 0
        self._answer_fragments: list[str] = []

    def _contract(self) -> dict[str, Any]:
        return {
            "mode": "persistent",
            "protocol": self.protocol,
            "input_transport": "ndjson",
            "additional_input_supported": True,
            "resume_supported": True,
            "fork_supported": True,
            "compact_supported": False,
            "review_supported": False,
            "interrupt_supported": True,
            "process_terminal_after_turn": False,
            "provider_identity": "anthropic-claude-code-cli",
        }

    def _multimodal_capability(self) -> dict[str, object]:
        return {
            "attachment_input_supported": True,
            "image_input_supported": True,
            "image_input": "native_image_base64",
            "audio_input_supported": False,
            "audio_input": "unavailable",
            "text_file_input": "bounded_verified_text_inline",
            "model_view_confirmation": self._vision_was_verified(),
            "semantic_image_verification": self._vision_verification_status(),
        }

    def start(self) -> None:
        with self._lock:
            if self._state != "created":
                raise ManagedAgentError("Claude stream session was already started")
        self._launch(mode="new")

    def _launch(self, *, mode: Literal["new", "resume", "fork"]) -> None:
        executable = find_trusted_executable(official_agent_spec("claude-code"))
        if executable is None:
            raise ManagedAgentError("official Claude Code CLI is unavailable")
        write_capable = self.permission_tier in WRITE_PERMISSION_TIERS
        available_tools = (
            "Read,Glob,Grep,Edit,Write,Bash,WebFetch,WebSearch"
            if self.permission_tier == "full-development"
            else (
                "Read,Glob,Grep,Edit,Write,WebFetch,WebSearch"
                if write_capable
                else "Read,Glob,Grep"
            )
        )
        allowed_tools = (
            available_tools
            if self.permission_tier == "full-development"
            else (
                "Read,Glob,Grep,Edit,Write,WebFetch,WebSearch"
                if write_capable
                else "Read,Glob,Grep"
            )
        )
        permission_mode = (
            "bypassPermissions"
            if self.permission_tier == "full-development"
            else ("acceptEdits" if write_capable else "plan")
        )
        arguments = [
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--replay-user-messages",
            "--include-partial-messages",
            "--verbose",
            "--permission-mode",
            permission_mode,
            "--tools",
            available_tools,
            "--allowedTools",
            allowed_tools,
        ]
        if self.permission_tier == "full-development":
            arguments.append("--allow-dangerously-skip-permissions")
        if self.requested_route:
            arguments.extend(("--model", self.requested_route))
        if mode == "new":
            arguments.extend(("--session-id", self._claude_session_id))
        else:
            arguments.extend(("--resume", self._claude_session_id))
            if mode == "fork":
                arguments.append("--fork-session")
        environment = _official_child_environment(
            self.provider_family, required_path_roots=(executable.parent,)
        )
        process = _spawn_owned(
            _command_for(executable, *arguments),
            cwd=self.working_directory,
            environment=environment,
            popen=self._popen,
        )
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._process = process
            self._state = "running"
            self._return_code = None
            self._ended_utc = None
            self._started_utc = self._started_utc or _utc_now()
            self._terminal.clear()
            self._busy = False
            self._append(
                "system",
                "system",
                "Official Claude Code stream-json session is ready for multiple turns.",
            )
        threading.Thread(
            target=self._read_stdout,
            args=(process, generation),
            name=f"peerbridge-{self.session_id}-claude-stream",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(process, generation),
            name=f"peerbridge-{self.session_id}-claude-stderr",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._watch_process,
            args=(process, generation),
            name=f"peerbridge-{self.session_id}-claude-watch",
            daemon=True,
        ).start()

    def _read_stdout(
        self, process: subprocess.Popen[bytes], generation: int
    ) -> None:
        stream = process.stdout
        if stream is None:
            return
        while True:
            raw = stream.readline(MAX_PROVIDER_FRAME_BYTES + 1)
            if not raw:
                return
            if not self._accept_provider_output(process, len(raw), "Claude"):
                return
            if len(raw) > MAX_PROVIDER_FRAME_BYTES:
                with self._lock:
                    self._append("system", "error", "Claude stream frame exceeded limit.")
                terminate_process_tree(process, wait_seconds=2)
                return
            try:
                event = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                with self._lock:
                    self._append("system", "error", "Claude emitted malformed stream JSON.")
                continue
            if not isinstance(event, Mapping):
                continue
            with self._lock:
                if generation != self._generation:
                    continue
                event_type = event.get("type")
                if event_type == "system" and event.get("subtype") == "init":
                    observed_id = event.get("session_id")
                    if isinstance(observed_id, str) and observed_id:
                        self._claude_session_id = observed_id
                    model = event.get("model")
                    if isinstance(model, str) and model:
                        self._model_id = model
                        self._model_source = "claude-stream-json:init"
                    version = event.get("claude_code_version")
                    if isinstance(version, str) and version:
                        self._client_version = version[:160]
                    self._observed_route = "claude-code-native-client"
                    self._observed_route_source = "claude-stream-json:init"
                    continue
                if event_type == "assistant":
                    message = event.get("message")
                    content = message.get("content") if isinstance(message, Mapping) else None
                    fragments: list[str] = []
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, Mapping) and item.get("type") == "text":
                                text = item.get("text")
                                if isinstance(text, str):
                                    fragments.append(text)
                    answer = "".join(fragments).strip()
                    if answer:
                        self._answer_fragments.append(answer)
                        self._append_visible_answer(answer)
                    continue
                if event_type == "result":
                    usage = event.get("usage")
                    self._record_usage(
                        normalize_provider_usage(
                            usage if isinstance(usage, Mapping) else None,
                            source="claude-stream-json:result",
                        )
                    )
                    answer = self._answer_fragments[-1] if self._answer_fragments else ""
                    self._finalize_vision_probe(
                        answer,
                        failure_status=(
                            "semantic_image_failed"
                            if event.get("is_error") is False
                            else "semantic_image_runtime_failed"
                        ),
                    )
                    self._answer_fragments.clear()
                    self._busy = False
                    self._append(
                        "system",
                        "terminal" if event.get("is_error") is False else "error",
                        "Claude turn completed."
                        if event.get("is_error") is False
                        else "Claude turn failed.",
                    )

    def _read_stderr(
        self, process: subprocess.Popen[bytes], generation: int
    ) -> None:
        stream = process.stderr
        if stream is None:
            return
        while True:
            raw = stream.readline(MAX_PROVIDER_FRAME_BYTES + 1)
            if not raw:
                return
            if not self._accept_provider_output(process, len(raw), "Claude stderr"):
                return
            if len(raw) > MAX_PROVIDER_FRAME_BYTES:
                with self._lock:
                    self._append("system", "error", "Claude stderr frame exceeded limit.")
                terminate_process_tree(process, wait_seconds=2)
                return
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                with self._lock:
                    if generation == self._generation:
                        self._append("stderr", "activity", text)

    def _watch_process(
        self, process: subprocess.Popen[bytes], generation: int
    ) -> None:
        return_code = process.wait()
        release_process_tree(process)
        with self._lock:
            if generation != self._generation:
                return
            self._return_code = int(return_code)
            self._busy = False
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
                f"Claude stream session ended with exit code {return_code}.",
            )
            self._terminal.set()

    def submit(
        self,
        text: str,
        *,
        attachments: Iterable[StagedAttachment | Mapping[str, object]] = (),
    ) -> None:
        verified = self._verify_attachments(attachments)
        payload = str(text or "").strip()
        if not payload and verified:
            payload = "Inspect the supplied PeerBridge attachment evidence and respond to the task."
        payload, encoded = _validate_composed_text(
            text,
            payload,
            attachment_present=bool(verified),
        )
        try:
            content_blocks = claude_native_content_blocks(payload, verified)
        except AttachmentError as exc:
            raise ManagedAgentError(str(exc)) from exc
        with self._lock:
            process = self._process
            if self._state != "running" or process is None or self._busy:
                raise ManagedAgentError("Claude stream session cannot accept input now")
            stdin = process.stdin
            if stdin is None or stdin.closed:
                raise ManagedAgentError("Claude stream input is unavailable")
            event = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": content_blocks,
                },
                "parent_tool_use_id": None,
                "session_id": self._claude_session_id,
            }
            wire = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ) + b"\n"
            try:
                write_process_stdin_bounded(process, wire)
            except (OSError, RuntimeError, TimeoutError) as exc:
                raise ManagedAgentError("Claude input could not be delivered") from exc
            self._busy = True
            self._answer_fragments.clear()
            self._turn_count += 1
            self._record_attachment_delivery(
                delivery_mode="native_image_base64_and_bounded_text_inline",
                status="transport_accepted",
                attachments=verified,
            )
            self._append(
                "system",
                "system",
                f"Claude turn accepted ({len(encoded)} UTF-8 bytes); input not retained.",
            )

    def interrupt(self) -> None:
        with self._lock:
            process = self._process
            if self._state != "running" or process is None:
                raise ManagedAgentError("Claude stream session is not running")
            self._state = "stopping"
            self._append("system", "system", "Claude turn interrupted; session can resume.")
        terminate_process_tree(process, wait_seconds=5)

    def resume(self) -> None:
        with self._lock:
            if self._state not in TERMINAL_STATES:
                raise ManagedAgentError("Claude session cannot be resumed now")
        self._launch(mode="resume")

    def fork(self) -> None:
        with self._lock:
            process = self._process
            if self._busy:
                raise ManagedAgentError("Claude session cannot be forked during a turn")
            if self._state == "running" and process is not None:
                self._state = "stopping"
            elif self._state not in TERMINAL_STATES:
                raise ManagedAgentError("Claude session cannot be forked now")
        if process is not None and process.poll() is None:
            terminate_process_tree(process, wait_seconds=5)
        self._launch(mode="fork")

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if self._state in TERMINAL_STATES:
                return
            if process is None:
                self._state = "stopped"
                self._ended_utc = _utc_now()
                self._terminal.set()
                return
            self._state = "stopping"
            self._append("system", "system", "Claude stream stop requested.")
        terminate_process_tree(process, wait_seconds=5)


class _AcpNamedSession(_PersistentSessionBase):
    """A durable ACPX named session with bounded per-turn runners."""

    acpx_profile = ""
    official_agent_id = ""
    provider_identity = ""
    image_input_mode = "native_acp_binary_content_block"
    audio_input_mode = "native_acp_audio_content_block"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._acpx_session_name = f"peerbridge-{self.session_id}"[:96]
        self._acp_prompt_capabilities: dict[str, bool] = {
            "image": False,
            "audio": False,
            "embeddedContext": False,
        }
        self._acp_turn_generation = 0
        self._acp_cancel_event: threading.Event | None = None
        self._acp_worker: threading.Thread | None = None
        self._acp_start_cancel_event: threading.Event | None = None

    def _contract(self) -> dict[str, Any]:
        return {
            "mode": "persistent",
            "protocol": self.protocol,
            "input_transport": "acpx-runtime-native-content",
            "additional_input_supported": True,
            "resume_supported": True,
            "fork_supported": False,
            "compact_supported": False,
            "review_supported": False,
            "interrupt_supported": True,
            "process_terminal_after_turn": False,
            "provider_identity": self.provider_identity,
        }

    def _multimodal_capability(self) -> dict[str, object]:
        image_supported = self._acp_prompt_capabilities.get("image") is True
        audio_supported = self._acp_prompt_capabilities.get("audio") is True
        return {
            "attachment_input_supported": True,
            "image_input_supported": image_supported,
            "image_input": self.image_input_mode if image_supported else "unavailable",
            "audio_input_supported": audio_supported,
            "audio_input": self.audio_input_mode if audio_supported else "unavailable",
            "text_file_input": "bounded_verified_text_inline",
            "model_view_confirmation": self._vision_was_verified(),
            "semantic_image_verification": (
                self._vision_verification_status()
                if image_supported
                else "unsupported_by_agent_runtime"
            ),
        }

    def _attachment_delivery_mode(
        self, attachments: Iterable[VerifiedAttachment]
    ) -> str:
        kinds = {item.kind for item in attachments}
        if "image" in kinds and "audio" in kinds:
            return "native_acp_image_audio_content_blocks"
        if "audio" in kinds:
            return self.audio_input_mode
        return self.image_input_mode

    def start_vision_probe(self) -> str:
        if self._acp_prompt_capabilities.get("image") is not True:
            raise ManagedAgentError(
                f"{self.display_name} ACP runtime does not advertise image input"
            )
        return super().start_vision_probe()

    def _runtime_bridge(
        self,
    ) -> tuple[Path, Path, Path, Path, dict[str, str]]:
        acpx = find_trusted_executable(ACPX_RUNTIME_SPEC)
        official_cli = find_trusted_executable(
            official_agent_spec(self.official_agent_id)
        )
        if acpx is None or official_cli is None:
            raise ManagedAgentError(
                f"official {self.display_name} and ACPX runtimes are required"
            )
        if os.name == "nt":
            program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            node = (program_files / "nodejs" / "node.exe").resolve()
            state_base = Path(
                os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
            )
        else:
            node_candidate = Path("/usr/bin/node")
            node = node_candidate.resolve()
            state_base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        helper = Path(__file__).with_name("acpx_runtime_bridge.mjs").resolve()
        runtime_module = (
            acpx.parent / "node_modules" / "acpx" / "dist" / "runtime.js"
        ).resolve()
        state_dir = (state_base / "PeerBridge" / "acpx-runtime").resolve()
        if not node.is_file() or not helper.is_file() or not runtime_module.is_file():
            raise ManagedAgentError(
                "ACPX native-content runtime dependencies are unavailable"
            )
        environment = _official_child_environment(
            self.provider_family,
            required_path_roots=(acpx.parent, official_cli.parent, node.parent),
        )
        return node, helper, runtime_module, state_dir, environment

    def _run_bridge(
        self,
        request: Mapping[str, object],
        *,
        timeout: float = 210.0,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, bytes, bytes]:
        node, helper, runtime_module, state_dir, environment = self._runtime_bridge()
        body: dict[str, object] = {
            "operation": request.get("operation"),
            "runtimeModulePath": str(runtime_module),
            "stateDir": str(state_dir),
            "cwd": str(self.working_directory),
            "agent": self.acpx_profile,
            "sessionKey": self._acpx_session_name,
            "permissionTier": self.permission_tier,
            "timeoutMs": min(600_000, max(1_000, int(timeout * 1000))),
        }
        if self.requested_route:
            body["model"] = self.requested_route
        for key in ("requestId", "text", "attachments"):
            if key in request:
                body[key] = request[key]
        stdin_data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            return _bounded_acp_process(
                _command_for(node, str(helper)),
                cwd=self.working_directory,
                environment=environment,
                stdin_text=stdin_data.decode("utf-8"),
                timeout_seconds=timeout,
                max_capture_bytes=MAX_ACP_CAPTURE_BYTES,
                runtime_label=f"{self.display_name} ACP native-content runtime",
                cancel_event=cancel_event,
            )
        except (ResourceUnavailableError, RunCancelledError) as exc:
            raise ManagedAgentError(str(exc)) from exc

    @staticmethod
    def _bridge_events(stdout: bytes) -> tuple[Mapping[str, object], ...]:
        events: list[Mapping[str, object]] = []
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ManagedAgentError("ACPX runtime returned invalid protocol data") from exc
            if not isinstance(event, Mapping) or not isinstance(event.get("type"), str):
                raise ManagedAgentError("ACPX runtime returned an invalid event")
            events.append(event)
        return tuple(events)

    @staticmethod
    def _normalized_acp_usage(
        raw: Mapping[str, object] | None,
        *,
        source: str,
    ) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        translated: dict[str, object] = {}
        for source_key, target_key in (
            ("inputTokens", "input_tokens"),
            ("outputTokens", "output_tokens"),
            ("totalTokens", "total_tokens"),
            ("thoughtTokens", "reasoning_tokens"),
        ):
            value = raw.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                translated[target_key] = value
        cached_values = [raw.get("cachedReadTokens"), raw.get("cachedWriteTokens")]
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in cached_values
        ):
            translated["cached_input_tokens"] = sum(cached_values)  # type: ignore[arg-type]
        if not translated:
            return None
        return normalize_provider_usage(translated, source=source)

    def start(self) -> None:
        start_cancel_event = threading.Event()
        with self._lock:
            if self._state != "created":
                raise ManagedAgentError(
                    f"{self.display_name} ACP session was already started"
                )
            self._acp_start_cancel_event = start_cancel_event
        try:
            code, stdout, stderr = self._run_bridge(
                {"operation": "ensure"},
                timeout=60.0,
                cancel_event=start_cancel_event,
            )
        finally:
            with self._lock:
                if self._acp_start_cancel_event is start_cancel_event:
                    self._acp_start_cancel_event = None
        events = self._bridge_events(stdout)
        if code != 0:
            failure = next(
                (event for event in reversed(events) if event.get("type") == "error"),
                None,
            )
            detail = (
                redact_secrets(str(failure.get("message") or ""))[:300]
                if isinstance(failure, Mapping)
                else ""
            )
            raise ManagedAgentError(
                f"{self.display_name} ACP native-content session could not be created"
                + (f": {detail}" if detail else "")
            )
        session_event = next(
            (event for event in events if event.get("type") == "session"),
            None,
        )
        if session_event is None or not any(
            event.get("type") == "done" and event.get("status") == "ready"
            for event in events
        ):
            raise ManagedAgentError("ACPX runtime did not confirm a ready session")
        status = session_event.get("status")
        status = status if isinstance(status, Mapping) else {}
        raw_prompt_capabilities = session_event.get("promptCapabilities")
        raw_prompt_capabilities = (
            raw_prompt_capabilities
            if isinstance(raw_prompt_capabilities, Mapping)
            else {}
        )
        observed_model = status.get("currentModelId")
        with self._lock:
            if self._state != "created" or start_cancel_event.is_set():
                raise ManagedAgentError(
                    f"{self.display_name} ACP session start was cancelled"
                )
            self._acp_prompt_capabilities = {
                key: raw_prompt_capabilities.get(key) is True
                for key in ("image", "audio", "embeddedContext")
            }
            self._state = "running"
            self._started_utc = _utc_now()
            self._terminal.clear()
            self._observed_route = f"{self.provider_identity}/{self.acpx_profile}"
            self._observed_route_source = "acpx-runtime:ensure"
            self._model_id = (
                str(observed_model)
                if isinstance(observed_model, str) and observed_model.strip()
                else self.requested_route
            )
            self._model_source = (
                "acpx-runtime-status"
                if isinstance(observed_model, str) and observed_model.strip()
                else ("operator-request" if self.requested_route else None)
            )
            self._append(
                "system",
                "system",
                f"Official {self.display_name} ACP native-content session is ready for multiple turns.",
            )
            if stderr.strip():
                self._append("stderr", "activity", stderr.decode("utf-8", "replace"))

    def submit(
        self,
        text: str,
        *,
        attachments: Iterable[StagedAttachment | Mapping[str, object]] = (),
    ) -> None:
        verified = self._verify_attachments(attachments)
        if any(row.kind == "image" for row in verified) and not self._acp_prompt_capabilities.get("image"):
            raise ManagedAgentError(
                f"{self.display_name} ACP runtime does not advertise image input"
            )
        if any(row.kind == "audio" for row in verified) and not self._acp_prompt_capabilities.get("audio"):
            raise ManagedAgentError(
                f"{self.display_name} ACP runtime does not advertise audio input"
            )
        payload = str(text or "").strip()
        if not payload and verified:
            payload = "Inspect the supplied PeerBridge attachment evidence and respond to the task."
        try:
            payload, native_attachments = acp_native_turn_payload(payload, verified)
        except AttachmentError as exc:
            raise ManagedAgentError(str(exc)) from exc
        payload, encoded = _validate_composed_text(
            text,
            payload,
            attachment_present=bool(verified),
        )
        with self._lock:
            if self._state != "running" or self._busy:
                raise ManagedAgentError(
                    f"{self.display_name} ACP session cannot accept input now"
                )
            self._acp_turn_generation += 1
            generation = self._acp_turn_generation
            cancel_event = threading.Event()
            self._busy = True
            self._turn_count += 1
            self._record_attachment_delivery(
                delivery_mode=self._attachment_delivery_mode(verified),
                status="native_content_prepared",
                attachments=verified,
            )
            self._append(
                "system",
                "system",
                f"{self.display_name} turn accepted ({len(encoded)} UTF-8 bytes); input not retained.",
            )

            worker: threading.Thread

            def run_turn() -> None:
                self._prompt_worker(
                    payload,
                    native_attachments,
                    verified,
                    generation,
                    cancel_event,
                    worker,
                )

            worker = threading.Thread(
                target=run_turn,
                args=(),
                name=f"peerbridge-{self.session_id}-acp-turn-{generation}",
                daemon=True,
            )
            self._acp_cancel_event = cancel_event
            self._acp_worker = worker
            try:
                worker.start()
            except Exception as exc:
                if self._owns_acp_turn(generation, cancel_event, worker):
                    self._finalize_vision_probe(
                        "", failure_status="semantic_image_runtime_failed"
                    )
                    self._busy = False
                    self._acp_cancel_event = None
                    self._acp_worker = None
                raise ManagedAgentError(
                    f"{self.display_name} ACP turn worker could not start"
                ) from exc

    def _owns_acp_turn(
        self,
        generation: int,
        cancel_event: threading.Event,
        worker: threading.Thread,
    ) -> bool:
        return (
            generation == self._acp_turn_generation
            and cancel_event is self._acp_cancel_event
            and worker is self._acp_worker
        )

    def _prompt_worker(
        self,
        payload: str,
        native_attachments: tuple[dict[str, str], ...],
        verified: tuple[VerifiedAttachment, ...],
        generation: int,
        cancel_event: threading.Event,
        worker: threading.Thread,
    ) -> None:
        code: int | None = None
        stderr = b""
        observed_model: str | None = None
        transport_accepted = False
        answer_fragments: list[str] = []
        usage_record: dict[str, Any] | None = None
        activity_messages: list[str] = []
        runtime_errors: list[str] = []
        failure_message: str | None = None
        unexpected_failure = False
        try:
            code, stdout, stderr = self._run_bridge(
                {
                    "operation": "turn",
                    "requestId": str(uuid.uuid4()),
                    "text": payload,
                    "attachments": native_attachments,
                },
                cancel_event=cancel_event,
            )
            for event in self._bridge_events(stdout):
                event_type = event.get("type")
                if event_type == "session":
                    status = event.get("status")
                    if isinstance(status, Mapping):
                        model_id = status.get("currentModelId")
                        if isinstance(model_id, str) and model_id.strip():
                            observed_model = model_id
                elif event_type == "transport":
                    transport_accepted = (
                        event.get("status") == "native_acp_content_submitted"
                    )
                elif event_type == "text_delta":
                    value = event.get("text")
                    if isinstance(value, str):
                        answer_fragments.append(value)
                elif event_type == "status":
                    normalized = self._normalized_acp_usage(
                        event.get("usage") if isinstance(event.get("usage"), Mapping) else None,
                        source=f"acpx-{self.official_agent_id}:runtime-turn",
                    )
                    if normalized is not None:
                        usage_record = normalized
                elif event_type == "tool_call":
                    title = str(event.get("title") or "ACP tool")
                    state = str(event.get("status") or "unknown")
                    kind = str(event.get("kind") or "tool")
                    activity_messages.append(
                        f"ACP activity: {title} [{kind}; {state}]"
                    )
                elif event_type == "error":
                    runtime_errors.append(
                        str(event.get("message") or "ACP runtime failed")
                    )
        except ManagedAgentError as exc:
            failure_message = str(exc)
        except Exception:
            unexpected_failure = True
        finally:
            with self._lock:
                if self._owns_acp_turn(generation, cancel_event, worker):
                    if cancel_event.is_set():
                        self._finalize_vision_probe(
                            "", failure_status="semantic_image_runtime_failed"
                        )
                        self._append(
                            "system",
                            "system",
                            f"{self.display_name} ACP turn cancelled.",
                        )
                    elif failure_message is not None:
                        self._finalize_vision_probe(
                            "", failure_status="semantic_image_runtime_failed"
                        )
                        self._append("system", "error", failure_message)
                    elif unexpected_failure or code is None:
                        self._finalize_vision_probe(
                            "", failure_status="semantic_image_runtime_failed"
                        )
                        self._append(
                            "system",
                            "error",
                            f"{self.display_name} ACP turn failed unexpectedly.",
                        )
                    else:
                        if observed_model is not None:
                            self._model_id = observed_model
                            self._model_source = "acpx-runtime-status"
                        for message in activity_messages:
                            self._append("stdout", "activity", message)
                        for message in runtime_errors:
                            self._append("system", "error", message)
                        if transport_accepted and verified:
                            self._record_attachment_delivery(
                                delivery_mode=self._attachment_delivery_mode(verified),
                                status="native_acp_content_submitted",
                                attachments=verified,
                            )
                        if usage_record is not None:
                            self._record_usage(usage_record)
                        answer = "".join(answer_fragments).strip()
                        self._append_visible_answer(answer)
                        self._finalize_vision_probe(
                            answer,
                            failure_status=(
                                "semantic_image_failed"
                                if code == 0
                                else "semantic_image_runtime_failed"
                            ),
                        )
                        if stderr.strip():
                            self._append(
                                "stderr",
                                "activity",
                                stderr.decode("utf-8", errors="replace"),
                            )
                        self._append(
                            "system",
                            "terminal" if code == 0 else "error",
                            f"{self.display_name} ACP turn completed."
                            if code == 0
                            else f"{self.display_name} ACP turn failed with exit code {code}.",
                        )

                    # Every owned transport exit closes its one-use challenge.
                    self._finalize_vision_probe(
                        "", failure_status="semantic_image_runtime_failed"
                    )
                    self._busy = False
                    self._process = None
                    self._acp_cancel_event = None
                    self._acp_worker = None
                    if self._state == "stopping":
                        self._state = "stopped"
                        self._ended_utc = _utc_now()
                        self._return_code = 0
                        self._terminal.set()

    def interrupt(self) -> None:
        with self._lock:
            cancel_event = self._acp_cancel_event
            worker = self._acp_worker
            if not self._busy or cancel_event is None or worker is None:
                raise ManagedAgentError(
                    f"{self.display_name} ACP turn is not running"
                )
            cancel_event.set()
            self._append(
                "system",
                "system",
                f"{self.display_name} ACP turn cancellation requested.",
            )

    def resume(self) -> None:
        with self._lock:
            if self._state not in TERMINAL_STATES:
                raise ManagedAgentError(
                    f"{self.display_name} ACP session cannot be resumed now"
                )
            self._state = "created"
            self._ended_utc = None
        self.start()

    def stop(self) -> None:
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            start_cancel_event = self._acp_start_cancel_event
            cancel_event = self._acp_cancel_event
            self._state = "stopping"
            self._append(
                "system", "system", f"{self.display_name} ACP session stop requested."
            )
            if start_cancel_event is not None:
                start_cancel_event.set()
            if self._busy:
                if cancel_event is not None:
                    cancel_event.set()
                return
            self._finalize_vision_probe(
                "", failure_status="semantic_image_runtime_failed"
            )
            self._state = "stopped"
            self._busy = False
            self._ended_utc = _utc_now()
            self._return_code = 0
            self._terminal.set()


class GrokAcpSession(_AcpNamedSession):
    """A durable official Grok Build session over ACP."""

    agent_id = "grok"
    display_name = "xAI Grok Build"
    provider_family = "grok-build"
    protocol = "acpx-grok-named-session"
    acpx_profile = "grok-build"
    official_agent_id = "grok"
    provider_identity = "xai-official-grok-build"


class KimiAcpSession(_AcpNamedSession):
    """A durable official Kimi Code session over ACP."""

    agent_id = "kimi-code"
    display_name = "Moonshot Kimi Code"
    provider_family = "kimi-code"
    protocol = "acpx-kimi-named-session"
    acpx_profile = "kimi"
    official_agent_id = "kimi-code"
    provider_identity = "moonshot-official-kimi-code"


class HybridManagedAgentManager:
    """Route official Agents to native runtimes and retain one-shot fallback."""

    _SESSION_TYPES: Mapping[str, type[_PersistentSessionBase]] = {
        "codex": CodexAppServerSession,
        "claude-code": ClaudeStreamSession,
        "grok": GrokAcpSession,
        "kimi-code": KimiAcpSession,
    }

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        max_retained_sessions: int = MAX_RETAINED_SESSIONS,
        fallback: ManagedAgentManager | None = None,
        wsl_write_builder: Callable[..., Any] | None = build_wsl_write_launch,
    ) -> None:
        if not 1 <= int(max_sessions) <= MAX_SESSIONS:
            raise ManagedAgentError("managed Agent session limit is invalid")
        if not int(max_sessions) <= int(max_retained_sessions) <= MAX_RETAINED_SESSIONS:
            raise ManagedAgentError("managed Agent retained session limit is invalid")
        self.max_sessions = int(max_sessions)
        self.max_retained_sessions = int(max_retained_sessions)
        self._sessions: dict[str, _PersistentSessionBase] = {}
        self._fallback = fallback or ManagedAgentManager(
            max_sessions=max_sessions,
            max_retained_sessions=max_retained_sessions,
        )
        self._wsl_write_builder = wsl_write_builder
        self._closed = False
        self._lock = threading.RLock()

    def _prune_one_native_terminal(self) -> bool:
        for session_id, session in tuple(self._sessions.items()):
            if session.snapshot()["state"] in TERMINAL_STATES:
                del self._sessions[session_id]
                return True
        return False

    def _combined_counts(self) -> tuple[int, int, set[str]]:
        fallback = self._fallback.snapshots()
        native = tuple(self._sessions.values())
        active = sum(
            session.snapshot()["state"] not in TERMINAL_STATES
            for session in native
        ) + sum(row.get("state") not in TERMINAL_STATES for row in fallback)
        identities = {session.session_id for session in native} | {
            str(row.get("session_id") or "") for row in fallback
        }
        return active, len(native) + len(fallback), identities

    def _prune_for_capacity(self) -> None:
        while True:
            _active, retained, _identities = self._combined_counts()
            if retained < self.max_retained_sessions:
                return
            if self._prune_one_native_terminal():
                continue
            if self._fallback.prune_one_terminal():
                continue
            return

    def start_official(
        self,
        *,
        agent_id: str,
        session_id: str,
        role: str,
        working_directory: Path,
        requested_route: str | None = None,
        permission_tier: Literal[
            "observe", "review", "edit", "full-development"
        ] = "observe",
        governance_binding_id: str | None = None,
        input_text: str | None = None,
        project_root: Path | None = None,
        attachments: Iterable[StagedAttachment | Mapping[str, object]] = (),
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> _PersistentSessionBase:
        try:
            session_type = self._SESSION_TYPES[agent_id]
        except KeyError as exc:
            raise ManagedAgentError("Agent has no official persistent runtime") from exc
        with self._lock:
            if self._closed:
                raise ManagedAgentError("managed Agent manager is closed")
            self._prune_for_capacity()
            active, retained, identities = self._combined_counts()
            if session_id in identities:
                raise ManagedAgentError("managed Agent session id already exists")
            if retained >= self.max_retained_sessions:
                raise ManagedAgentError("managed Agent retained session limit reached")
            if active >= self.max_sessions:
                raise ManagedAgentError("managed Agent session limit reached")
            session = session_type(
                session_id=session_id,
                role=role,
                working_directory=working_directory,
                requested_route=requested_route,
                permission_tier=permission_tier,
                governance_binding_id=governance_binding_id,
                project_root=project_root,
                popen=popen,
            )
            self._sessions[session_id] = session
        try:
            session.start()
            attachment_rows = tuple(attachments)
            if input_text is not None or attachment_rows:
                session.submit(input_text or "", attachments=attachment_rows)
        except Exception:
            with contextlib.suppress(ManagedAgentError):
                session.stop()
            raise
        return session

    def start(self, *args: Any, **kwargs: Any) -> Any:
        launch = args[0] if args else kwargs.get("launch")
        session_id = str(getattr(launch, "session_id", "") or "")
        with self._lock:
            if self._closed:
                raise ManagedAgentError("managed Agent manager is closed")
            self._prune_for_capacity()
            active, retained, identities = self._combined_counts()
            if session_id and session_id in identities:
                raise ManagedAgentError("managed Agent session id already exists")
            if retained >= self.max_retained_sessions:
                raise ManagedAgentError("managed Agent retained session limit reached")
            if active >= self.max_sessions:
                raise ManagedAgentError("managed Agent session limit reached")
            return self._fallback.start(*args, **kwargs)

    def get(self, session_id: str) -> Any:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            return session
        return self._fallback.get(session_id)

    def snapshots(
        self, *, after_sequences: Mapping[str, int] | None = None
    ) -> list[dict[str, Any]]:
        positions = after_sequences or {}
        with self._lock:
            native = tuple(self._sessions.values())
        snapshots = [
            session.snapshot(
                after_sequence=max(0, int(positions.get(session.session_id, 0)))
            )
            for session in native
        ]
        snapshots.extend(self._fallback.snapshots(after_sequences=positions))
        return snapshots

    def stop_all(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
        for session in sessions:
            with contextlib.suppress(ManagedAgentError):
                session.stop()
        self._fallback.stop_all()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop_all()
        self._fallback.close()


__all__ = [
    "ClaudeStreamSession",
    "CodexAppServerSession",
    "GrokAcpSession",
    "HybridManagedAgentManager",
    "KimiAcpSession",
    "OFFICIAL_PERSISTENT_AGENT_IDS",
]

_OFFICIAL_ROUTE_OVERRIDE_KEYS: dict[str, tuple[str, ...]] = {
    "codex": ("OPENAI_BASE_URL",),
    "claude": ("ANTHROPIC_BASE_URL",),
    "grok-build": ("GROK_BASE_URL", "XAI_BASE_URL"),
    "kimi-code": ("KIMI_BASE_URL", "MOONSHOT_BASE_URL"),
}


def _official_child_environment(
    provider_family: str, *, required_path_roots: tuple[Path, ...]
) -> dict[str, str]:
    environment = build_agent_child_environment(
        provider_family,
        required_path_roots=required_path_roots,
        include_provider_credentials=False,
    )
    for key in _OFFICIAL_ROUTE_OVERRIDE_KEYS.get(provider_family, ()):
        environment.pop(key, None)
    return environment
