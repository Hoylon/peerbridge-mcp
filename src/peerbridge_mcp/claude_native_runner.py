"""Official Claude Code transport backed by a WCM provider descriptor."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from pathlib import Path
from typing import Any, Mapping

from . import credentials
from .bridge import MAX_TEXT_CHARS, stable_sha256
from .ccswitch_runner import _bounded_process, find_claude_cli
from .child_environment import build_agent_child_environment
from .multimodal import (
    attachment_delivery_receipt,
    claude_native_content_blocks,
    extract_verified_attachments,
)
from .openai_compatible_runner import (
    ConfigurationError,
    CredentialUnavailableError,
    InferenceResult,
    ProviderHTTPError,
    ResourceUnavailableError,
    RouteMismatchError,
    RunnerConfig,
    provider_runtime_admission,
)
from .usage import normalize_provider_usage


RECEIPT_SCHEMA = "peerbridge.claude-native-wcm-inference-receipt.v1"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ClaudeNativeWcmRunner:
    """Run one bounded Claude Code turn without exposing WCM credentials."""

    def __init__(
        self,
        config: RunnerConfig,
        *,
        executable: Path | None = None,
        process_runner: Any = _bounded_process,
        runtime_admitted: bool = False,
    ) -> None:
        self.config = config
        self.executable = Path(executable) if executable else find_claude_cli()
        if self.executable is None or not self.executable.is_file():
            raise ConfigurationError("Claude Code native client is not installed")
        self.process_runner = process_runner
        self._runtime_admitted = bool(runtime_admitted)
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _access(self) -> credentials.ProviderAccess:
        try:
            return credentials.load_provider_access(
                scope=self.config.scope,
                connection_id=self.config.connection_id,
                route_class=self.config.route_class,
                provider_id=self.config.provider_id,
            )
        except credentials.CredentialStoreError:
            raise CredentialUnavailableError(
                "Claude native provider credential is unavailable"
            ) from None

    @staticmethod
    def _base_url(endpoint: str) -> str:
        value = str(endpoint).rstrip("/")
        return value[:-3] if value.endswith("/v1") else value

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
        message_id: str | None,
    ) -> InferenceResult:
        access = self._access()
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
            raise ConfigurationError("Claude native inference prompt is empty")
        if not prompt:
            prompt = "Inspect the supplied PeerBridge attachment evidence and respond."

        runtime = self.config.project_root / ".peerbridge" / "runtime" / "claude-native"
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
            stdin_text = json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": claude_native_content_blocks(prompt, attachments),
                    },
                    "parent_tool_use_id": None,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
        if system:
            command.extend(("--system-prompt", system))

        environment = build_agent_child_environment(
            "claude",
            include_provider_credentials=False,
        )
        environment["ANTHROPIC_BASE_URL"] = self._base_url(access.endpoint)
        environment["ANTHROPIC_API_KEY"] = access.api_key or ""
        environment.pop("ANTHROPIC_AUTH_TOKEN", None)
        return_code, stdout, _stderr = self.process_runner(
            command,
            cwd=runtime,
            environment=environment,
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
                "Claude native provider returned malformed event output",
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
                raise CredentialUnavailableError(
                    "Claude native provider authentication failed"
                )
            code = int(status) if isinstance(status, int) and not isinstance(status, bool) else 502
            raise ProviderHTTPError(
                "Claude native provider request failed",
                status_code=code,
                retryable=code in {408, 429} or code >= 500,
            )

        observed_model = str(initial.get("model") or "").strip()
        if observed_model != self.config.model:
            raise RouteMismatchError("Claude native model identity drifted")
        content = str(terminal.get("result") or "").strip()
        if not content:
            raise ProviderHTTPError(
                "Claude native provider returned an empty response",
                status_code=502,
                retryable=False,
            )
        if len(content) > MAX_TEXT_CHARS:
            raise ResourceUnavailableError(
                "Claude native provider response exceeded bridge limit"
            )

        final_access = self._access()
        if not (
            secrets.compare_digest(access.endpoint_sha256, final_access.endpoint_sha256)
            and secrets.compare_digest(
                access.credential_version_sha256,
                final_access.credential_version_sha256,
            )
        ):
            raise RouteMismatchError(
                "Claude native credential changed during inference"
            )

        raw_usage = terminal.get("usage")
        if not isinstance(raw_usage, Mapping):
            model_usage = terminal.get("modelUsage")
            if isinstance(model_usage, Mapping):
                per_model = model_usage.get(observed_model)
                raw_usage = per_model if isinstance(per_model, Mapping) else model_usage
        usage = normalize_provider_usage(
            raw_usage if isinstance(raw_usage, Mapping) else None,
            source="claude-native/wcm",
        )
        receipt: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "secret_backend": "windows-credential-manager",
            "route_class": self.config.route_class,
            "route_profile_id": self.config.route_profile_id,
            "route_profile_sha256": self.config.route_profile_sha256,
            "connection_id": self.config.connection_id,
            "endpoint_sha256": access.endpoint_sha256,
            "credential_version_sha256": access.credential_version_sha256,
            "requested_provider_id": self.config.provider_id,
            "requested_model": self.config.model,
            "observed_model": observed_model,
            "requested_reasoning_mode": self.config.reasoning_mode,
            "message_id": message_id,
            "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
            "response_sha256": _sha256_bytes(content.encode("utf-8")),
            "response_chars": len(content),
            "event_count": len(events),
            "stdout_bytes": len(stdout),
            "executable_sha256": _sha256_bytes(self.executable.read_bytes()),
            "credential_values_recorded": False,
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


__all__ = ["ClaudeNativeWcmRunner", "RECEIPT_SCHEMA"]
