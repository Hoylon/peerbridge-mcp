"""Bounded native inference through a user-selected CC Switch provider.

CC Switch remains an intermediary evidence class.  This adapter never reads its
database or credentials: it resolves a redacted provider reference through the
public CLI, requires that provider to be selected, and launches the associated
native client with tools and session persistence disabled.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agent_install import find_trusted_executable, official_agent_spec
from .bridge import MAX_TEXT_CHARS, stable_sha256
from .child_environment import build_agent_child_environment
from .ccswitch import (
    CcSwitchError,
    CcSwitchRouteIdentity,
    list_providers,
    resolve_route_identity,
)
from .openai_compatible_runner import (
    ConfigurationError,
    CredentialUnavailableError,
    InferenceResult,
    ProviderHTTPError,
    ResourceUnavailableError,
    RouteMismatchError,
    RunCancelledError,
    RunnerConfig,
    provider_runtime_admission,
)
from .multimodal import (
    attachment_delivery_receipt,
    claude_native_content_blocks,
    extract_verified_attachments,
)
from .process_control import (
    attach_process_tree,
    process_group_popen_kwargs,
    release_process_tree,
    terminate_process_tree,
)
from .usage import normalize_provider_usage


REFERENCE_PREFIX = "CCSwitch:"
MAX_CAPTURE_BYTES = 4 * 1024 * 1024


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def resolve_reference(
    *, app: str, credential_target: str, route_class: str, model_id: str
) -> CcSwitchRouteIdentity:
    """Resolve an opaque database reference without exposing provider config."""
    reference = str(credential_target or "")
    if not reference.startswith(REFERENCE_PREFIX):
        raise CcSwitchError("invalid CC Switch provider reference")
    expected = reference[len(REFERENCE_PREFIX) :]
    if len(expected) != 32 or any(ch not in "0123456789abcdef" for ch in expected):
        raise CcSwitchError("invalid CC Switch provider reference")
    matches = []
    for provider in list_providers(app):
        digest = hashlib.sha256(
            f"{provider.app}:{provider.provider_id}".encode("utf-8")
        ).hexdigest()
        if digest[:32] == expected:
            matches.append(provider)
    if len(matches) != 1:
        raise CcSwitchError("CC Switch provider reference is missing or ambiguous")
    identity = resolve_route_identity(
        app=app,
        route_class=route_class,
        provider_id=matches[0].provider_id,
        model_id=model_id,
    )
    if not identity.current:
        raise CcSwitchError("CC Switch provider is not currently selected")
    return identity


def find_claude_cli() -> Path | None:
    return find_trusted_executable(official_agent_spec("claude-code"))


def _bounded_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stdin_text: str,
    timeout_seconds: float,
    max_capture_bytes: int = MAX_CAPTURE_BYTES,
    runtime_label: str = "CC Switch native client",
    cancel_event: threading.Event | None = None,
) -> tuple[int, bytes, bytes]:
    """Capture a child without allowing stdout/stderr to grow without bound."""
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **process_group_popen_kwargs(),
    )
    attach_process_tree(process)
    outputs: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    stdin_failed = threading.Event()
    stdin_errors: list[Exception] = []
    deadline = time.monotonic() + float(timeout_seconds)

    def drain(name: str, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                target = outputs[name]
                remaining = max_capture_bytes - len(target)
                if remaining <= 0 or len(chunk) > remaining:
                    target.extend(chunk[: max(0, remaining)])
                    overflow.set()
                    return
                target.extend(chunk)
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    def write_stdin() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(stdin_text.encode("utf-8"))
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            stdin_errors.append(exc)
            stdin_failed.set()
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()

    writer = threading.Thread(
        target=write_stdin,
        name=f"peerbridge-stdin-{process.pid}",
        daemon=True,
    )
    writer.start()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                terminate_process_tree(process)
                raise RunCancelledError(f"{runtime_label} was cancelled")
            if overflow.is_set():
                terminate_process_tree(process)
                raise ResourceUnavailableError(
                    f"{runtime_label} exceeded output budget"
                )
            if stdin_failed.is_set() and process.poll() is None:
                terminate_process_tree(process)
                raise ResourceUnavailableError(
                    f"{runtime_label} could not receive the bounded request"
                ) from stdin_errors[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process_tree(process)
                raise ResourceUnavailableError(f"{runtime_label} timed out")
            try:
                return_code = process.wait(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            terminate_process_tree(process)
        else:
            release_process_tree(process)
        with contextlib.suppress(OSError):
            if process.stdin is not None:
                process.stdin.close()
        writer.join(timeout=0.5)
        for thread in threads:
            thread.join(timeout=5)
    if overflow.is_set():
        raise ResourceUnavailableError(f"{runtime_label} exceeded output budget")
    return return_code, bytes(outputs["stdout"]), bytes(outputs["stderr"])


class CcSwitchRunner:
    """One-shot relay inference using only CC Switch's selected native client."""

    def __init__(
        self,
        config: RunnerConfig,
        *,
        credential_target: str,
        client_name: str | None,
        executable: Path | None = None,
        identity_resolver: Callable[..., CcSwitchRouteIdentity] = resolve_reference,
        process_runner: Callable[..., tuple[int, bytes, bytes]] = _bounded_process,
        runtime_admitted: bool = False,
    ) -> None:
        self.config = config
        self.credential_target = credential_target
        self.app = str(client_name or "").strip().lower()
        if self.app != "claude":
            raise ConfigurationError(
                "this CC Switch runtime supports the Claude native client only"
            )
        self.executable = Path(executable) if executable else find_claude_cli()
        if self.executable is None or not self.executable.is_file():
            raise ConfigurationError("Claude Code native client is not installed")
        self.identity_resolver = identity_resolver
        self.process_runner = process_runner
        self._runtime_admitted = bool(runtime_admitted)
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _identity(self) -> CcSwitchRouteIdentity:
        try:
            return self.identity_resolver(
                app=self.app,
                credential_target=self.credential_target,
                route_class=self.config.route_class,
                model_id=self.config.model,
            )
        except CcSwitchError as exc:
            message = str(exc)
            if "currently selected" in message:
                raise CredentialUnavailableError(message) from None
            if "advertise" in message:
                raise RouteMismatchError(message) from None
            raise ConfigurationError(message) from None

    def run(
        self,
        messages: list[dict[str, Any]],
        *,
        message_id: str | None = None,
    ) -> InferenceResult:
        with provider_runtime_admission(already_admitted=self._runtime_admitted):
            return self._run_unchecked(messages, message_id=message_id)

    def _run_unchecked(
        self,
        messages: list[dict[str, Any]],
        *,
        message_id: str | None = None,
    ) -> InferenceResult:
        identity = self._identity()
        conversation, attachments = extract_verified_attachments(
            self.config.project_root,
            messages,
        )
        system = "\n\n".join(
            str(item.get("content") or "")
            for item in conversation
            if item.get("role") == "system"
        ).strip()
        prompt = "\n\n".join(
            str(item.get("content") or "")
            for item in conversation
            if item.get("role") != "system"
        ).strip()
        if not prompt and not attachments:
            raise ConfigurationError("CC Switch inference prompt is empty")
        if not prompt:
            prompt = "Inspect the supplied PeerBridge attachment evidence and respond."
        runtime = self.config.project_root / ".peerbridge" / "runtime" / "ccswitch"
        runtime.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.executable),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--model",
            self.config.model,
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--disable-slash-commands",
            "--no-chrome",
        ]
        stdin_text = prompt
        if attachments:
            command.extend(("--input-format", "stream-json"))
            content_blocks = claude_native_content_blocks(prompt, attachments)
            event = {
                "type": "user",
                "message": {"role": "user", "content": content_blocks},
                "parent_tool_use_id": None,
            }
            stdin_text = json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
        if system:
            command.extend(("--system-prompt", system))
        return_code, stdout, _stderr = self.process_runner(
            command,
            cwd=runtime,
            environment=build_agent_child_environment(
                self.app,
                include_provider_credentials=False,
            ),
            stdin_text=stdin_text,
            timeout_seconds=self.config.timeout_seconds,
            cancel_event=self._cancel_event,
        )
        events: list[dict[str, Any]] = []
        try:
            for raw in stdout.decode("utf-8", errors="strict").splitlines():
                if raw.strip():
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError
                    events.append(value)
        except (UnicodeError, json.JSONDecodeError, ValueError):
            raise ProviderHTTPError(
                "CC Switch provider returned malformed event output",
                status_code=502,
                retryable=False,
            ) from None
        initial = next((event for event in events if event.get("type") == "system"), {})
        terminal = next(
            (event for event in reversed(events) if event.get("type") == "result"),
            None,
        )
        if return_code != 0 or terminal is None or terminal.get("is_error"):
            status = terminal.get("api_error_status") if terminal else None
            if status == 401:
                raise CredentialUnavailableError("CC Switch provider authentication failed")
            code = int(status) if isinstance(status, int) and not isinstance(status, bool) else 502
            raise ProviderHTTPError(
                "CC Switch provider request failed",
                status_code=code,
                retryable=code in {408, 429} or code >= 500,
            )
        observed_model = str(initial.get("model") or "").strip()
        if observed_model != self.config.model:
            raise RouteMismatchError("CC Switch native client model identity drifted")
        content = str(terminal.get("result") or "").strip()
        if not content:
            raise ProviderHTTPError(
                "CC Switch provider returned an empty response",
                status_code=502,
                retryable=False,
            )
        if len(content) > MAX_TEXT_CHARS:
            raise ResourceUnavailableError("CC Switch provider response exceeded bridge limit")
        final_identity = self._identity()
        if not secrets.compare_digest(
            final_identity.identity_sha256, identity.identity_sha256
        ):
            raise RouteMismatchError(
                "CC Switch provider selection changed during inference"
            )
        raw_usage = terminal.get("usage")
        if not isinstance(raw_usage, Mapping):
            model_usage = terminal.get("modelUsage")
            if isinstance(model_usage, Mapping):
                per_model = model_usage.get(observed_model)
                raw_usage = per_model if isinstance(per_model, Mapping) else model_usage
        usage = normalize_provider_usage(
            raw_usage if isinstance(raw_usage, Mapping) else None,
            source="cc-switch/native-client",
        )
        receipt: dict[str, Any] = {
            "schema": "peerbridge.ccswitch-inference-receipt.v1",
            "secret_backend": "cc-switch",
            "route_class": "relay",
            "route_profile_id": self.config.route_profile_id,
            "route_profile_sha256": self.config.route_profile_sha256,
            "app": self.app,
            "connection_id": self.config.connection_id,
            "provider_identity_sha256": identity.identity_sha256,
            "provider_selection_rechecked": True,
            "provider_selection_stable": True,
            "requested_model": self.config.model,
            "observed_model": observed_model,
            "message_id": message_id,
            "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
            "response_sha256": _sha256_bytes(content.encode("utf-8")),
            "response_chars": len(content),
            "event_count": len(events),
            "stdout_bytes": len(stdout),
            "executable_sha256": _sha256_bytes(self.executable.read_bytes()),
            "credential_values_read": False,
            "credential_values_recorded": False,
            "upstream_provider_identity_attested": False,
            "tool_calls": 0,
            "status": "completed",
            "usage": usage,
        }
        if attachments:
            receipt["attachment_delivery"] = attachment_delivery_receipt(
                provider_id=self.config.provider_id,
                protocol="claude-stream-json",
                delivery_mode="native_image_base64_and_bounded_text_inline",
                status="provider_request_completed",
                attachments=attachments,
            )
        receipt["receipt_sha256"] = stable_sha256(receipt)
        return InferenceResult(
            assistant_message={"role": "assistant", "content": content},
            receipt=receipt,
        )


__all__ = ["CcSwitchRunner", "find_claude_cli", "resolve_reference"]
