"""Bounded response-only inference through the optional ACPX runtime.

ACPX is a community Agent Client Protocol runtime, not a model provider.  This
adapter invokes only reviewed built-in Agent identifiers, passes prompts through
stdin, disables filesystem and terminal capabilities, and records sanitized
identity evidence without reading the Agent's cached authentication material.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agent_install import ACPX_RUNTIME_SPEC, find_trusted_executable
from .bridge import MAX_TEXT_CHARS, stable_sha256
from .ccswitch_runner import MAX_CAPTURE_BYTES, _bounded_process
from .child_environment import build_agent_child_environment
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


ACPX_VERSION = "0.13.0"
REFERENCE_PREFIX = "ACPX:"
SUPPORTED_AGENTS = frozenset({"codex", "claude", "kimi", "grok-build"})
_EXPECTED_AGENT_NAMES: Mapping[str, frozenset[str]] = {
    "codex": frozenset({"@agentclientprotocol/codex-acp"}),
    "claude": frozenset({"claude-agent-acp", "@zed-industries/claude-agent-acp"}),
    "kimi": frozenset({"kimi", "kimi-acp", "kimi-code"}),
    "grok-build": frozenset({"grok-build", "grok"}),
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def find_acpx() -> Path | None:
    return find_trusted_executable(ACPX_RUNTIME_SPEC)


def _agent_from_reference(*, client_name: str | None, credential_target: str) -> str:
    agent = str(client_name or "").strip().lower()
    if agent not in SUPPORTED_AGENTS:
        raise ConfigurationError("unsupported ACPX built-in Agent identity")
    if credential_target != f"{REFERENCE_PREFIX}{agent}":
        raise RouteMismatchError("ACPX runtime reference does not match the route Agent")
    return agent


def _requested_model(config: RunnerConfig, agent: str) -> str:
    model = str(config.model or "").strip()
    if not model:
        raise ConfigurationError("ACPX route model is empty")
    # ACPX 0.13.0 advertises bracketed Codex variants in ``models`` but its
    # --model validator accepts the base config-option value.  Reasoning is
    # independently verified from session/new below; it is never guessed.
    return model.split("[", 1)[0]


def _prompt_text(messages: Sequence[Mapping[str, str]]) -> str:
    system = "\n\n".join(
        str(item.get("content") or "")
        for item in messages
        if item.get("role") == "system"
    ).strip()
    user = "\n\n".join(
        str(item.get("content") or "")
        for item in messages
        if item.get("role") != "system"
    ).strip()
    if not user:
        raise ConfigurationError("ACPX inference prompt is empty")
    prompt = user if not system else f"Instructions:\n{system}\n\nRequest:\n{user}"
    if len(prompt) > MAX_TEXT_CHARS:
        raise ResourceUnavailableError("ACPX inference prompt exceeded bridge limit")
    return prompt


def _mcp_config_path(config: RunnerConfig, agent: str, runtime: Path) -> tuple[Path, str]:
    args = [
        "-m",
        "peerbridge_mcp",
        "serve",
        "--project-root",
        str(config.project_root.resolve()),
        "--agent-id",
        config.agent_id,
        "--scope",
        config.scope,
        "--session-id",
        config.session_id,
        "--client-name",
        f"acpx-{agent}",
        "--provider-id",
        config.provider_id,
        "--model-id",
        config.model.split("[", 1)[0],
        "--route-class",
        config.route_class,
    ]
    if config.reasoning_mode:
        args.extend(("--reasoning-mode", config.reasoning_mode))
    if config.db_path is not None:
        args.extend(("--db", str(Path(config.db_path).resolve())))
    for tool in config.allowed_tools:
        args.extend(("--allow-tool", tool))
    payload = {
        "mcpServers": [
            {
                "name": "peerbridge",
                "command": str(Path(sys.executable).resolve()),
                "args": args,
            }
        ]
    }
    content = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    digest = _sha256_bytes(content)
    path = runtime / f"mcp-{digest}.json"
    if path.exists():
        if path.read_bytes() != content:
            raise RouteMismatchError("ACPX MCP config content address drifted")
    else:
        try:
            with path.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            if path.read_bytes() != content:
                raise RouteMismatchError("ACPX MCP config creation raced") from None
    return path, digest


def _parse_events(stdout: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        for raw in stdout.decode("utf-8", errors="strict").splitlines():
            if not raw.strip():
                continue
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            events.append(value)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise ProviderHTTPError(
            "ACPX returned malformed ACP event output",
            status_code=502,
            retryable=False,
        ) from None
    if not events:
        raise ProviderHTTPError(
            "ACPX returned no ACP events", status_code=502, retryable=False
        )
    return events


def _result_for_id(events: Sequence[Mapping[str, Any]], request_id: int) -> Mapping[str, Any]:
    for event in events:
        if event.get("id") == request_id and isinstance(event.get("result"), dict):
            return event["result"]
    raise ProviderHTTPError(
        "ACPX omitted a required ACP result", status_code=502, retryable=False
    )


def _observed_config(session: Mapping[str, Any], key: str) -> str | None:
    options = session.get("configOptions")
    if not isinstance(options, list):
        return None
    matches = [
        item
        for item in options
        if isinstance(item, dict) and item.get("id") == key
    ]
    if len(matches) != 1:
        return None
    value = matches[0].get("currentValue")
    return str(value) if isinstance(value, str) and value else None


def _assistant_text(events: Sequence[Mapping[str, Any]]) -> str:
    chunks: list[str] = []
    for event in events:
        params = event.get("params")
        if not isinstance(params, dict):
            continue
        update = params.get("update")
        if not isinstance(update, dict) or update.get("sessionUpdate") != "agent_message_chunk":
            continue
        content = update.get("content")
        if not isinstance(content, dict) or content.get("type") != "text":
            continue
        value = content.get("text")
        if not isinstance(value, str):
            raise ProviderHTTPError(
                "ACPX returned malformed assistant content",
                status_code=502,
                retryable=False,
            )
        chunks.append(value)
    answer = "".join(chunks).strip()
    if not answer:
        raise ProviderHTTPError(
            "ACPX returned an empty response", status_code=502, retryable=False
        )
    if len(answer) > MAX_TEXT_CHARS:
        raise ResourceUnavailableError("ACPX response exceeded bridge limit")
    return answer


def _tool_event_count(events: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for event in events:
        params = event.get("params")
        update = params.get("update") if isinstance(params, dict) else None
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if isinstance(kind, str) and ("tool" in kind or "plan" in kind):
            total += 1
    return total


class AcpxRunner:
    """Execute one audited, response-only ACP turn through a built-in Agent."""

    def __init__(
        self,
        config: RunnerConfig,
        *,
        credential_target: str,
        client_name: str | None,
        executable: Path | None = None,
        process_runner: Callable[..., tuple[int, bytes, bytes]] = _bounded_process,
        runtime_admitted: bool = False,
    ) -> None:
        self.config = config
        self.agent = _agent_from_reference(
            client_name=client_name, credential_target=credential_target
        )
        self.executable = Path(executable) if executable else find_acpx()
        if self.executable is None or not self.executable.is_file():
            raise ConfigurationError("ACPX interoperability runtime is not installed")
        self.process_runner = process_runner
        self._runtime_admitted = bool(runtime_admitted)
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(
        self,
        messages: list[dict[str, str]],
        *,
        message_id: str | None = None,
    ) -> InferenceResult:
        with provider_runtime_admission(already_admitted=self._runtime_admitted):
            return self._run_unchecked(messages, message_id=message_id)

    def _run_unchecked(
        self,
        messages: list[dict[str, str]],
        *,
        message_id: str | None,
    ) -> InferenceResult:
        prompt = _prompt_text(messages)
        requested_model = _requested_model(self.config, self.agent)
        runtime = self.config.project_root / ".peerbridge" / "runtime" / "acpx"
        runtime.mkdir(parents=True, exist_ok=True)
        mcp_config, mcp_config_sha = _mcp_config_path(
            self.config, self.agent, runtime
        )
        command = [
            str(self.executable),
            "--cwd",
            str(self.config.project_root.resolve()),
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
            "--mcp-config",
            str(mcp_config),
            "--max-turns",
            "1",
            "--prompt-retries",
            "0",
            "--model",
            requested_model,
            "--timeout",
            str(max(1, int(self.config.timeout_seconds))),
            self.agent,
            "exec",
            "-f",
            "-",
        ]
        return_code, stdout, _stderr = self.process_runner(
            command,
            cwd=runtime,
            environment=build_agent_child_environment(self.agent),
            stdin_text=prompt,
            timeout_seconds=self.config.timeout_seconds,
            max_capture_bytes=MAX_CAPTURE_BYTES,
            runtime_label="ACPX runtime",
            cancel_event=self._cancel_event,
        )
        events = _parse_events(stdout)
        if return_code != 0:
            raise CredentialUnavailableError("ACPX Agent authentication or execution failed")

        initialize = _result_for_id(events, 0)
        session = _result_for_id(events, 1)
        terminal = _result_for_id(events, 2)
        agent_info = initialize.get("agentInfo")
        observed_agent = (
            str(agent_info.get("name") or "").strip()
            if isinstance(agent_info, dict)
            else ""
        )
        if observed_agent not in _EXPECTED_AGENT_NAMES[self.agent]:
            raise RouteMismatchError("ACPX observed Agent identity drifted")
        if terminal.get("stopReason") != "end_turn":
            raise ProviderHTTPError(
                "ACPX Agent did not complete the turn",
                status_code=502,
                retryable=False,
            )

        observed_model = _observed_config(session, "model")
        observed_reasoning = _observed_config(session, "reasoning_effort")
        if observed_model is None:
            models = session.get("models")
            observed_model = (
                str(models.get("currentModelId") or "").split("[", 1)[0]
                if isinstance(models, dict)
                else ""
            )
        if observed_model != self.config.model.split("[", 1)[0]:
            raise RouteMismatchError("ACPX observed model identity drifted")
        if (
            self.agent == "codex"
            and self.config.reasoning_mode
            and observed_reasoning != self.config.reasoning_mode
        ):
            raise RouteMismatchError("ACPX observed reasoning mode drifted")

        answer = _assistant_text(events)
        available_models = session.get("models")
        catalog = (
            available_models.get("availableModels", [])
            if isinstance(available_models, dict)
            else []
        )
        raw_usage = terminal.get("usage")
        if not isinstance(raw_usage, Mapping):
            metadata = terminal.get("_meta")
            raw_usage = metadata.get("usage") if isinstance(metadata, Mapping) else None
        usage = normalize_provider_usage(
            raw_usage if isinstance(raw_usage, Mapping) else None,
            source="acpx/acp-session-prompt",
        )
        receipt: dict[str, Any] = {
            "schema": "peerbridge.acpx-inference-receipt.v1",
            "runtime": "acpx",
            "runtime_version": ACPX_VERSION,
            "secret_backend": "native-acp",
            "route_class": self.config.route_class,
            "route_profile_id": self.config.route_profile_id,
            "route_profile_sha256": self.config.route_profile_sha256,
            "connection_id": self.config.connection_id,
            "agent_id": self.agent,
            "observed_agent_name": observed_agent,
            "observed_agent_version": (
                str(agent_info.get("version") or "")
                if isinstance(agent_info, dict)
                else ""
            ),
            "requested_model": self.config.model,
            "observed_model": observed_model,
            "requested_reasoning_mode": self.config.reasoning_mode,
            "observed_reasoning_mode": observed_reasoning,
            "message_id": message_id,
            "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
            "response_sha256": _sha256_bytes(answer.encode("utf-8")),
            "response_chars": len(answer),
            "event_count": len(events),
            "tool_event_count": _tool_event_count(events),
            "model_catalog_sha256": stable_sha256(catalog),
            "stdout_bytes": len(stdout),
            "executable_sha256": _sha256_bytes(self.executable.read_bytes()),
            "credential_values_read_by_peerbridge": False,
            "credential_values_recorded": False,
            "agent_cached_auth_used": True,
            "filesystem_capability": False,
            "terminal_capability": False,
            "mcp_tools_exposed": True,
            "mcp_allowed_tools_sha256": stable_sha256(self.config.allowed_tools),
            "mcp_config_sha256": mcp_config_sha,
            "usage": usage,
        }
        receipt["receipt_sha256"] = stable_sha256(receipt)
        return InferenceResult(
            assistant_message={"role": "assistant", "content": answer},
            receipt=receipt,
        )


__all__ = ["ACPX_VERSION", "AcpxRunner", "SUPPORTED_AGENTS", "find_acpx"]
