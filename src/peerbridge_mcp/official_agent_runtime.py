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
import hashlib
import json
import os
import secrets
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from .agent_adapter_contract import AgentAdapterRegistry, RegisteredAgentAdapter
from .agent_adapters import (
    CLAUDE_ADAPTER,
    CODEX_ADAPTER,
    GROK_ADAPTER,
    KIMI_ADAPTER,
)
from .agent_install import ACPX_RUNTIME_SPEC, find_trusted_executable, official_agent_spec
from .attachments import (
    AttachmentError,
    StagedAttachment,
    stage_chat_attachment_payloads,
)
from .approval_broker import (
    APPROVAL_MODES,
    MAX_PENDING_APPROVALS,
    ApprovalBroker,
    ApprovalRecord,
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
MAX_APPROVAL_CALLBACK_BYTES = 64 * 1024


class _AcpApprovalCallbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, broker: ApprovalBroker, token: str) -> None:
        self.broker = broker
        self.token = token
        self._request_slots = threading.BoundedSemaphore(MAX_PENDING_APPROVALS)
        super().__init__(("127.0.0.1", 0), _AcpApprovalCallbackHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class _AcpApprovalCallbackHandler(BaseHTTPRequestHandler):
    server: _AcpApprovalCallbackServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _reply(self, status: HTTPStatus, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/approval":
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if not secrets.compare_digest(supplied, expected):
            self._reply(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 1 <= length <= MAX_APPROVAL_CALLBACK_BYTES:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid body"})
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        if not isinstance(request, Mapping):
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
            return
        encoded_request = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        request_hash = hashlib.sha256(encoded_request.encode("utf-8")).hexdigest()
        raw_kind = str(request.get("inferredKind") or "tool").lower()
        action_kind = "".join(
            character if character.isalnum() or character in "._-" else "-"
            for character in raw_kind
        )[:80] or "tool"
        risk: Literal["routine", "elevated", "high"] = (
            "routine"
            if raw_kind in {"read", "search", "think"}
            else "elevated"
            if raw_kind in {"edit", "fetch"}
            else "high"
        )
        title = str(
            request.get("title")
            or request.get("displayName")
            or request.get("toolName")
            or "Agent tool request"
        )
        try:
            decision = self.server.broker.request(
                provider_request_id=f"acp-{request_hash[:40]}",
                action_kind=action_kind,
                title=title,
                detail=encoded_request,
                risk=risk,
                timeout_seconds=600,
            )
        except (KeyError, ValueError):
            decision = "deny"
        outcome = {
            "allow-once": "allow_once",
            "allow-session": "allow_always",
            "deny": "reject_once",
        }.get(decision, "reject_once")
        self._reply(HTTPStatus.OK, {"outcome": outcome})


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
    adapter_descriptor = None
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
        approval_mode: Literal[
            "approval-required", "agent-delegated", "full-access"
        ] | None = None,
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
        default_approval_mode = (
            "full-access"
            if permission_tier == "full-development"
            else "agent-delegated"
            if permission_tier == "edit"
            else "approval-required"
        )
        selected_approval_mode = approval_mode or default_approval_mode
        if selected_approval_mode not in APPROVAL_MODES:
            raise ManagedAgentError("official runtime approval mode is invalid")
        if permission_tier in {"observe", "review"} and selected_approval_mode != "approval-required":
            raise ManagedAgentError("read-only runtime requires approval-required mode")
        if permission_tier == "edit" and selected_approval_mode not in {
            "approval-required",
            "agent-delegated",
        }:
            raise ManagedAgentError("standard runtime approval mode is invalid")
        if permission_tier == "full-development" and selected_approval_mode != "full-access":
            raise ManagedAgentError("full-development runtime requires full-access mode")
        self.approval_mode = selected_approval_mode
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
        adapter_id = (
            self.adapter_descriptor.adapter_id
            if self.adapter_descriptor is not None
            else self.agent_id
        )
        self._approval_broker = ApprovalBroker(
            session_id=self.session_id,
            adapter_id=adapter_id,
            mode=self.approval_mode,
            on_change=self._approval_changed,
        )

    def _approval_changed(self, record: ApprovalRecord) -> None:
        with self._lock:
            verb = "requested" if record.state == "pending" else record.state
            self._append(
                "system",
                "activity",
                f"Approval {verb}: {record.title}",
                summary=record.approval_id,
            )

    def resolve_approval(self, approval_id: str, decision: str) -> dict[str, object]:
        return self._approval_broker.resolve(approval_id, decision).as_dict()

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

    def _approvals_reviewer(self) -> str:
        return "auto_review" if self.approval_mode == "agent-delegated" else "user"

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
                "adapter": (
                    self.adapter_descriptor.as_dict()
                    if self.adapter_descriptor is not None
                    else None
                ),
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
                "approval_mode": self.approval_mode,
                "approval_broker": self._approval_broker.snapshot(),
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
    adapter_descriptor = CODEX_ADAPTER

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
                    "approvalsReviewer": self._approvals_reviewer(),
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
                with contextlib.suppress(ManagedAgentError):
                    self._handle_server_request(
                        response_id,
                        method,
                        params if isinstance(params, Mapping) else {},
                        generation,
                    )
                continue
            if isinstance(method, str) and isinstance(params, Mapping):
                self._handle_notification(method, params, generation)

    def _handle_server_request(
        self,
        response_id: int,
        method: str,
        params: Mapping[str, Any],
        generation: int,
    ) -> None:
        approval_methods = {
            "item/commandExecution/requestApproval": "command-execution",
            "item/fileChange/requestApproval": "file-change",
            "item/permissions/requestApproval": "permissions",
        }
        action_kind = approval_methods.get(method)
        if action_kind is None:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": response_id,
                    "error": {
                        "code": -32001,
                        "message": "PeerBridge does not implement this server request",
                    },
                }
            )
            return
        if action_kind == "command-execution":
            raw_command = params.get("command")
            if isinstance(raw_command, list):
                command = " ".join(str(value) for value in raw_command)
            else:
                command = str(raw_command or "")
            title = str(params.get("reason") or "Run command")
            detail = " | ".join(
                value
                for value in (
                    command,
                    f"cwd={params.get('cwd')}" if params.get("cwd") else "",
                )
                if value
            )
            risk: Literal["routine", "elevated", "high"] = "high"
        elif action_kind == "file-change":
            title = str(params.get("reason") or "Apply file changes")
            detail = json.dumps(
                params.get("changes") or params.get("grantRoot") or {},
                ensure_ascii=False,
                sort_keys=True,
            )
            risk = "elevated"
        else:
            title = str(params.get("reason") or "Grant additional permissions")
            detail = json.dumps(
                params.get("permissions") or {},
                ensure_ascii=False,
                sort_keys=True,
            )
            risk = "high"
        if self.permission_tier in {"observe", "review"}:
            decision = "deny"
        else:
            raw_available = params.get("availableDecisions")
            allow_session = not isinstance(raw_available, list) or (
                "acceptForSession" in raw_available
            )
            available = (
                ("allow-once", "allow-session", "deny")
                if allow_session
                else ("allow-once", "deny")
            )
            decision = self._approval_broker.request(
                provider_request_id=f"codex-{generation}-{response_id}",
                action_kind=action_kind,
                title=title,
                detail=detail,
                risk=risk,
                available_decisions=available,
            )
        if action_kind == "permissions":
            result: dict[str, Any] = {
                "scope": "session" if decision == "allow-session" else "turn",
                "permissions": (
                    dict(params.get("permissions") or {})
                    if decision in {"allow-once", "allow-session"}
                    else {}
                ),
            }
        else:
            result = {
                "decision": {
                    "allow-once": "accept",
                    "allow-session": "acceptForSession",
                    "deny": "decline",
                }[decision]
            }
        self._send({"jsonrpc": "2.0", "id": response_id, "result": result})

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
                    "approvalsReviewer": self._approvals_reviewer(),
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
        self._approval_broker.cancel_all()
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
    adapter_descriptor = CLAUDE_ADAPTER

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._claude_session_id = str(uuid.uuid4())
        self._generation = 0
        self._answer_fragments: list[str] = []
        self._claude_control_requests: dict[str, str] = {}
        self._claude_approval_slots = threading.BoundedSemaphore(
            MAX_PENDING_APPROVALS
        )

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
        interactive_approval = write_capable and self.approval_mode == "approval-required"
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
            "Read,Glob,Grep"
            if interactive_approval
            else available_tools
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
            else "manual"
            if interactive_approval
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
        if interactive_approval:
            arguments.extend(("--permission-prompt-tool", "stdio"))
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

    @staticmethod
    def _claude_session_permission_suggestions(
        request: Mapping[str, Any], tool_name: str
    ) -> tuple[dict[str, Any], ...]:
        accepted: list[dict[str, Any]] = []
        for row in request.get("permission_suggestions") or ():
            if not isinstance(row, Mapping):
                continue
            if row.get("destination") != "session":
                continue
            if row.get("type") != "addRules" or row.get("behavior") != "allow":
                continue
            raw_rules = row.get("rules")
            if not isinstance(raw_rules, list) or not 1 <= len(raw_rules) <= 4:
                continue
            rules: list[dict[str, str]] = []
            for rule in raw_rules:
                if not isinstance(rule, Mapping) or rule.get("toolName") != tool_name:
                    rules = []
                    break
                rule_content = rule.get("ruleContent")
                if (
                    not isinstance(rule_content, str)
                    or not rule_content.strip()
                    or rule_content.strip() == "*"
                    or len(rule_content.encode("utf-8")) > 1024
                ):
                    rules = []
                    break
                rules.append(
                    {"toolName": tool_name, "ruleContent": rule_content}
                )
            if not rules:
                continue
            candidate = {
                "type": "addRules",
                "destination": "session",
                "behavior": "allow",
                "rules": rules,
            }
            encoded = json.dumps(
                candidate,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) <= 8 * 1024:
                accepted.append(candidate)
        return tuple(accepted[:8])

    @staticmethod
    def _claude_permission_risk(tool_name: str) -> Literal["routine", "elevated", "high"]:
        normalized = tool_name.casefold()
        if normalized in {"read", "glob", "grep"}:
            return "routine"
        if normalized in {"edit", "write", "multiedit", "notebookedit"}:
            return "elevated"
        return "high"

    @staticmethod
    def _claude_provider_request_id(
        generation: int,
        request_id: str,
    ) -> str:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:40]
        return f"claude-{generation}-{digest}"

    def _send_claude_control_response(
        self,
        process: subprocess.Popen[bytes],
        generation: int,
        payload: Mapping[str, Any],
    ) -> None:
        wire = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(wire) > MAX_PROVIDER_FRAME_BYTES:
            raise ManagedAgentError("Claude control response exceeded the bounded limit")
        with self._lock:
            if (
                generation != self._generation
                or process is not self._process
                or self._state != "running"
            ):
                return
            try:
                write_process_stdin_bounded(process, wire)
            except (OSError, RuntimeError, TimeoutError) as exc:
                self._append(
                    "system",
                    "error",
                    "Claude interactive approval response could not be delivered.",
                )
                terminate_process_tree(process, wait_seconds=2)
                raise ManagedAgentError(
                    "Claude interactive approval response failed"
                ) from exc

    def _handle_claude_control_request(
        self,
        process: subprocess.Popen[bytes],
        generation: int,
        event: Mapping[str, Any],
    ) -> None:
        request_id = event.get("request_id")
        request = event.get("request")
        if not isinstance(request_id, str) or not request_id or not isinstance(request, Mapping):
            return
        if request.get("subtype") != "can_use_tool":
            self._send_claude_control_response(
                process,
                generation,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "error",
                        "request_id": request_id,
                        "error": "PeerBridge supports only Claude tool permission requests",
                    },
                },
            )
            return
        tool_name = str(request.get("tool_name") or "Agent tool")[:160]
        tool_input = request.get("input")
        if not isinstance(tool_input, Mapping):
            tool_input = {}
        suggestions = self._claude_session_permission_suggestions(request, tool_name)
        available = (
            ("allow-once", "allow-session", "deny")
            if suggestions
            else ("allow-once", "deny")
        )
        provider_request_id = self._claude_provider_request_id(generation, request_id)
        with self._lock:
            self._claude_control_requests[request_id] = provider_request_id
        detail = json.dumps(
            {
                "tool_name": tool_name,
                "input": dict(tool_input),
                "tool_use_id": request.get("tool_use_id"),
                "blocked_path": request.get("blocked_path"),
                "decision_reason": request.get("decision_reason"),
                "agent_id": request.get("agent_id"),
                "session_permission_suggestions": list(suggestions),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            decision = self._approval_broker.request(
                provider_request_id=provider_request_id,
                action_kind="claude-tool",
                title=str(
                    request.get("title")
                    or request.get("display_name")
                    or tool_name
                ),
                detail=detail,
                risk=self._claude_permission_risk(tool_name),
                available_decisions=available,
                timeout_seconds=600,
            )
            response_data: dict[str, Any]
            if decision in {"allow-once", "allow-session"}:
                response_data = {
                    "behavior": "allow",
                    "updatedInput": dict(tool_input),
                }
                if decision == "allow-session" and suggestions:
                    response_data["updatedPermissions"] = list(suggestions)
            else:
                response_data = {
                    "behavior": "deny",
                    "message": "The operator denied this tool request.",
                }
            self._send_claude_control_response(
                process,
                generation,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": response_data,
                    },
                },
            )
        finally:
            with self._lock:
                self._claude_control_requests.pop(request_id, None)

    def _cancel_claude_control_request(self, event: Mapping[str, Any]) -> None:
        request_id = event.get("request_id")
        if not isinstance(request_id, str):
            return
        with self._lock:
            provider_request_id = self._claude_control_requests.get(request_id)
        if provider_request_id:
            self._approval_broker.cancel_provider_request(provider_request_id)

    def _run_claude_control_request(
        self,
        process: subprocess.Popen[bytes],
        generation: int,
        event: Mapping[str, Any],
    ) -> None:
        try:
            self._handle_claude_control_request(process, generation, event)
        finally:
            self._claude_approval_slots.release()

    def _reject_claude_control_overload(
        self,
        process: subprocess.Popen[bytes],
        generation: int,
        event: Mapping[str, Any],
    ) -> None:
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        self._send_claude_control_response(
            process,
            generation,
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {
                        "behavior": "deny",
                        "message": "PeerBridge approval capacity is exhausted.",
                    },
                },
            },
        )

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
            event_type = event.get("type")
            if event_type == "control_request":
                if not self._claude_approval_slots.acquire(blocking=False):
                    self._reject_claude_control_overload(
                        process, generation, event
                    )
                    continue
                threading.Thread(
                    target=self._run_claude_control_request,
                    args=(process, generation, event),
                    name=f"peerbridge-{self.session_id}-claude-approval",
                    daemon=True,
                ).start()
                continue
            if event_type == "control_cancel_request":
                self._cancel_claude_control_request(event)
                continue
            with self._lock:
                if generation != self._generation:
                    continue
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
        self._approval_broker.cancel_all()
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
        approval_server: _AcpApprovalCallbackServer | None = None
        approval_thread: threading.Thread | None = None
        body: dict[str, object] = {
            "operation": request.get("operation"),
            "runtimeModulePath": str(runtime_module),
            "stateDir": str(state_dir),
            "cwd": str(self.working_directory),
            "agent": self.acpx_profile,
            "sessionKey": self._acpx_session_name,
            "permissionTier": self.permission_tier,
            "approvalMode": self.approval_mode,
            "timeoutMs": min(600_000, max(1_000, int(timeout * 1000))),
        }
        if self.requested_route:
            body["model"] = self.requested_route
        for key in ("requestId", "text", "attachments"):
            if key in request:
                body[key] = request[key]
        if request.get("operation") == "turn" and self.permission_tier != "full-development":
            approval_token = secrets.token_urlsafe(32)
            approval_server = _AcpApprovalCallbackServer(
                self._approval_broker, approval_token
            )
            approval_thread = threading.Thread(
                target=approval_server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name=f"peerbridge-{self.session_id}-approval-callback",
                daemon=True,
            )
            approval_thread.start()
            body["approvalEndpoint"] = (
                f"http://127.0.0.1:{approval_server.server_address[1]}/approval"
            )
            body["approvalToken"] = approval_token
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
        finally:
            if approval_server is not None:
                approval_server.shutdown()
                approval_server.server_close()
            if approval_thread is not None:
                approval_thread.join(timeout=2)

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
        self._approval_broker.cancel_all()
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
    adapter_descriptor = GROK_ADAPTER


class KimiAcpSession(_AcpNamedSession):
    """A durable official Kimi Code session over ACP."""

    agent_id = "kimi-code"
    display_name = "Moonshot Kimi Code"
    provider_family = "kimi-code"
    protocol = "acpx-kimi-named-session"
    acpx_profile = "kimi"
    official_agent_id = "kimi-code"
    provider_identity = "moonshot-official-kimi-code"
    adapter_descriptor = KIMI_ADAPTER


class HybridManagedAgentManager:
    """Route official Agents to native runtimes and retain one-shot fallback."""

    # Compatibility injection point for existing tests and local embedders.
    # Adapter identity still comes from AgentAdapterRegistry.
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
        adapter_registry: AgentAdapterRegistry | None = None,
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
        self._adapter_registry = adapter_registry or AgentAdapterRegistry(
            (
                RegisteredAgentAdapter(CODEX_ADAPTER, CodexAppServerSession),
                RegisteredAgentAdapter(CLAUDE_ADAPTER, ClaudeStreamSession),
                RegisteredAgentAdapter(GROK_ADAPTER, GrokAcpSession),
                RegisteredAgentAdapter(KIMI_ADAPTER, KimiAcpSession),
            )
        )
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
        approval_mode: Literal[
            "approval-required", "agent-delegated", "full-access"
        ] | None = None,
        governance_binding_id: str | None = None,
        input_text: str | None = None,
        project_root: Path | None = None,
        attachments: Iterable[StagedAttachment | Mapping[str, object]] = (),
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> _PersistentSessionBase:
        try:
            registered_adapter = self._adapter_registry.for_agent(agent_id)
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
            session_factory = self._SESSION_TYPES.get(
                agent_id, registered_adapter.session_factory
            )
            session = session_factory(
                session_id=session_id,
                role=role,
                working_directory=working_directory,
                requested_route=requested_route,
                permission_tier=permission_tier,
                approval_mode=approval_mode,
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
