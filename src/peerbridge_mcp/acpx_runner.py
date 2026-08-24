"""Bounded response-only inference through the optional ACPX runtime.

ACPX is a community Agent Client Protocol runtime, not a model provider.  This
adapter invokes only reviewed built-in Agent identifiers, passes prompts through
stdin, disables filesystem and terminal capabilities, and records sanitized
identity evidence without reading the Agent's cached authentication material.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agent_install import (
    ACPX_RUNTIME_SPEC,
    find_trusted_executable,
    official_agent_spec,
)
from .bridge import (
    MAX_TEXT_CHARS,
    _reject_reparse_ancestry,
    stable_sha256,
)
from .ccswitch_runner import MAX_CAPTURE_BYTES, _bounded_process
from .child_environment import build_agent_child_environment
from .multimodal import (
    VerifiedAttachment,
    attachment_delivery_receipt,
    extract_verified_attachments,
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
_OFFICIAL_AGENT_IDS: Mapping[str, str] = {
    "codex": "codex",
    "claude": "claude-code",
    "kimi": "kimi-code",
    "grok-build": "grok",
}
_OFFICIAL_ROUTE_OVERRIDES: Mapping[str, frozenset[str]] = {
    "codex": frozenset({"OPENAI_BASE_URL"}),
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
        }
    ),
    "grok-build": frozenset({"GROK_BASE_URL", "XAI_BASE_URL"}),
    "kimi": frozenset({"KIMI_BASE_URL", "MOONSHOT_BASE_URL"}),
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def verify_acpx_inference_receipt(
    receipt: Mapping[str, Any] | Path,
) -> dict[str, Any]:
    """Verify one sanitized ACPX receipt without invoking an Agent or writing state."""

    if isinstance(receipt, Path):
        if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 512 * 1024:
            raise ValueError("ACPX receipt path is unsafe")
        value = json.loads(receipt.read_text(encoding="utf-8"))
    else:
        value = dict(receipt)
    if not isinstance(value, dict) or value.get("schema") != "peerbridge.acpx-inference-receipt.v1":
        raise ValueError("ACPX receipt schema is invalid")
    claimed = value.get("receipt_sha256")
    if not isinstance(claimed, str) or len(claimed) != 64:
        raise ValueError("ACPX receipt SHA-256 is missing")
    base = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if stable_sha256(base) != claimed:
        raise ValueError("ACPX receipt SHA-256 does not match")
    for field in (
        "prompt_sha256",
        "response_sha256",
        "executable_sha256",
        "mcp_config_sha256",
        "permission_policy_sha256",
    ):
        item = value.get(field)
        if not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item):
            raise ValueError(f"ACPX receipt {field} is invalid")
    if value.get("credential_values_read_by_peerbridge") is not False:
        raise ValueError("ACPX receipt indicates credential access")
    if value.get("credential_values_recorded") is not False:
        raise ValueError("ACPX receipt indicates credential persistence")
    if value.get("terminal_capability") is not False:
        raise ValueError("ACPX receipt terminal boundary is invalid")
    if value.get("mcp_tools_exposed") is not True:
        raise ValueError("ACPX receipt MCP tool exposure is invalid")
    if value.get("requested_route_class") != value.get("route_class"):
        raise ValueError("ACPX receipt requested route class binding is invalid")
    if value.get("observed_route_class") is not None:
        raise ValueError("ACPX receipt overclaims an observed route class")
    if value.get("provider_route_class") is not None:
        raise ValueError("ACPX receipt overclaims a provider route class")
    if value.get("provider_route_class_attested") is not False:
        raise ValueError("ACPX receipt route attestation is invalid")
    for field in (
        "mcp_tool_call_count",
        "mcp_allowed_tool_call_count",
        "mcp_unrecognized_tool_call_count",
        "mcp_tool_error_count",
        "response_chars",
    ):
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ValueError(f"ACPX receipt {field} is invalid")
    return {
        "valid": True,
        "writes_performed": 0,
        "receipt_sha256": claimed,
        "agent_id": value.get("agent_id"),
        "observed_agent_name": value.get("observed_agent_name"),
        "observed_agent_version": value.get("observed_agent_version"),
        "observed_model": value.get("observed_model"),
        "requested_route_class": value.get("requested_route_class"),
        "observed_route_class": value.get("observed_route_class"),
        "mcp_canonical_tools_called": value.get("mcp_canonical_tools_called"),
        "mcp_tool_call_count": value.get("mcp_tool_call_count"),
    }


def find_acpx() -> Path | None:
    return find_trusted_executable(ACPX_RUNTIME_SPEC)


def find_acpx_agent_runtime(agent: str) -> Path | None:
    official_id = _OFFICIAL_AGENT_IDS.get(str(agent or "").strip().lower())
    if official_id is None:
        return None
    return find_trusted_executable(official_agent_spec(official_id))


def _agent_from_reference(*, client_name: str | None, credential_target: str) -> str:
    agent = str(client_name or "").strip().lower()
    if agent not in SUPPORTED_AGENTS:
        raise ConfigurationError("unsupported ACPX built-in Agent identity")
    if credential_target != f"{REFERENCE_PREFIX}{agent}":
        raise RouteMismatchError("ACPX runtime reference does not match the route Agent")
    return agent


def native_acp_runtime_available(
    *, client_name: str | None, credential_target: str
) -> bool:
    try:
        agent = _agent_from_reference(
            client_name=client_name, credential_target=credential_target
        )
    except (ConfigurationError, RouteMismatchError):
        return False
    return find_acpx() is not None and find_acpx_agent_runtime(agent) is not None


def _requested_model(config: RunnerConfig, agent: str) -> str:
    model = str(config.model or "").strip()
    if not model:
        raise ConfigurationError("ACPX route model is empty")
    if agent == "codex" and ("[" in model or "]" in model):
        raise ConfigurationError(
            "ACPX Codex model and reasoning mode must be configured separately"
        )
    return model


def _mcp_tool_variants(allowed_tools: Sequence[str]) -> dict[str, str]:
    allowed = {str(tool).strip() for tool in allowed_tools if str(tool).strip()}
    return {
        variant: tool
        for tool in allowed
        for variant in (
            tool,
            f"peerbridge__{tool}",
            f"mcp__peerbridge__{tool}",
            f"mcp.peerbridge.{tool}",
        )
    }


def _prompt_text(messages: Sequence[Mapping[str, Any]]) -> str:
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


def _mcp_permission_policy(allowed_tools: Sequence[str]) -> tuple[str, str]:
    """Approve only the explicitly exposed PeerBridge MCP tools."""

    matchers: set[str] = set()
    for value in allowed_tools:
        tool = str(value or "").strip()
        if not tool:
            continue
        matchers.update(
            {
                tool,
                f"peerbridge__{tool}",
                f"mcp__peerbridge__{tool}",
                f"mcp.peerbridge.{tool}",
            }
        )
    payload = {
        "autoApprove": sorted(matchers),
        "defaultAction": "deny",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return encoded, _sha256_bytes(encoded.encode("utf-8"))


def _mcp_config_path(config: RunnerConfig, agent: str, runtime: Path) -> tuple[Path, str]:
    if config.identity_capability_path is None:
        raise ConfigurationError(
            "a pre-issued Agent identity capability is required for ACPX MCP"
        )
    identity_capability = Path(config.identity_capability_path)
    if not identity_capability.is_absolute() or not identity_capability.is_file():
        raise ConfigurationError("Agent identity capability is unavailable")
    args = [
        "-m",
        "peerbridge_mcp",
        "serve",
        "--project-root",
        str(config.project_root.resolve()),
        "--agent-id",
        config.agent_id,
        "--identity-capability",
        str(identity_capability.resolve()),
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


def _parse_cli_object(stdout: bytes) -> dict[str, Any]:
    try:
        value = json.loads(stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        raise ProviderHTTPError(
            "ACPX returned malformed lifecycle output",
            status_code=502,
            retryable=False,
        ) from None
    if not isinstance(value, dict):
        raise ProviderHTTPError(
            "ACPX returned malformed lifecycle output",
            status_code=502,
            retryable=False,
        )
    return value


def _record_value(record: Mapping[str, Any], camel: str, snake: str) -> Any:
    return record.get(camel) if camel in record else record.get(snake)


def _record_agent_messages(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_messages = record.get("messages")
    if not isinstance(raw_messages, list):
        raise ProviderHTTPError(
            "ACPX session record omitted messages",
            status_code=502,
            retryable=False,
        )
    messages: list[Mapping[str, Any]] = []
    for raw in raw_messages:
        if not isinstance(raw, Mapping):
            raise ProviderHTTPError(
                "ACPX session record contained a malformed message",
                status_code=502,
                retryable=False,
            )
        agent = raw.get("Agent")
        if isinstance(agent, Mapping):
            messages.append(agent)
    return messages


def _record_user_ids(record: Mapping[str, Any]) -> list[str]:
    raw_messages = record.get("messages")
    if not isinstance(raw_messages, list):
        return []
    values: list[str] = []
    for raw in raw_messages:
        if not isinstance(raw, Mapping):
            continue
        user = raw.get("User")
        if not isinstance(user, Mapping):
            continue
        value = str(user.get("id") or "").strip()
        if value:
            values.append(value)
    return values


def _record_assistant_text(agent_messages: Sequence[Mapping[str, Any]]) -> str:
    chunks: list[str] = []
    for message in agent_messages:
        content = message.get("content")
        if not isinstance(content, list):
            raise ProviderHTTPError(
                "ACPX session record contained malformed assistant content",
                status_code=502,
                retryable=False,
            )
        for item in content:
            if not isinstance(item, Mapping) or "Text" not in item:
                continue
            value = item.get("Text")
            if not isinstance(value, str):
                raise ProviderHTTPError(
                    "ACPX session record contained malformed assistant text",
                    status_code=502,
                    retryable=False,
                )
            chunks.append(value)
    answer = "".join(chunks).strip()
    if not answer:
        return ""
    if len(answer) > MAX_TEXT_CHARS:
        raise ResourceUnavailableError("ACPX response exceeded bridge limit")
    return answer


def _record_tool_result_audit(
    agent_messages: Sequence[Mapping[str, Any]], allowed_tools: Sequence[str]
) -> tuple[dict[str, Any], bool]:
    variants = _mcp_tool_variants(allowed_tools)
    seen_ids: set[str] = set()
    observed_names: list[str] = []
    canonical: list[str] = []
    unrecognized_mcp = 0
    auxiliary = 0
    errors = 0
    all_results_present = True
    for message in agent_messages:
        raw_results = message.get("tool_results")
        if raw_results is None:
            raw_results = {}
        if not isinstance(raw_results, Mapping):
            raise ProviderHTTPError(
                "ACPX session record contained malformed tool results",
                status_code=502,
                retryable=False,
            )
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            tool_use = item.get("ToolUse") if isinstance(item, Mapping) else None
            if not isinstance(tool_use, Mapping):
                continue
            call_id = str(tool_use.get("id") or "").strip()
            raw_name = str(tool_use.get("name") or "").strip()
            if not call_id or call_id in seen_ids:
                continue
            seen_ids.add(call_id)
            result = raw_results.get(call_id)
            if not isinstance(result, Mapping):
                all_results_present = False
                continue
            result_name = str(result.get("tool_name") or raw_name).strip()
            observed_names.append(result_name)
            if result.get("is_error") is True:
                errors += 1
            canonical_name = variants.get(result_name)
            if canonical_name is None:
                if result_name.startswith(
                    ("peerbridge__", "mcp__peerbridge__", "mcp.peerbridge.")
                ):
                    unrecognized_mcp += 1
                else:
                    auxiliary += 1
            else:
                canonical.append(canonical_name)
    return (
        {
            "agent_tool_call_event_count": len(seen_ids),
            "agent_auxiliary_tool_call_count": auxiliary,
            "mcp_tool_call_count": len(canonical) + unrecognized_mcp,
            "mcp_allowed_tool_call_count": len(canonical),
            "mcp_unrecognized_tool_call_count": unrecognized_mcp,
            "mcp_tool_error_count": errors,
            "mcp_canonical_tools_called": sorted(set(canonical)),
            "mcp_observed_tool_names_sha256": stable_sha256(observed_names),
        },
        all_results_present,
    )


def _record_config(record: Mapping[str, Any]) -> tuple[str, str | None, list[Any]]:
    acpx = record.get("acpx")
    if not isinstance(acpx, Mapping):
        raise ProviderHTTPError(
            "ACPX session record omitted runtime configuration",
            status_code=502,
            retryable=False,
        )
    model = str(acpx.get("current_model_id") or "").strip()
    desired = acpx.get("desired_config_options")
    reasoning = (
        str(desired.get("reasoning_effort") or "").strip()
        if isinstance(desired, Mapping)
        else ""
    )
    if not reasoning:
        options = acpx.get("config_options")
        if isinstance(options, list):
            matches = [
                item
                for item in options
                if isinstance(item, Mapping) and item.get("id") == "reasoning_effort"
            ]
            if len(matches) == 1:
                reasoning = str(
                    matches[0].get("currentValue")
                    or matches[0].get("current_value")
                    or ""
                ).strip()
    catalog = acpx.get("available_models")
    return model, reasoning or None, catalog if isinstance(catalog, list) else []


def _result_for_id(events: Sequence[Mapping[str, Any]], request_id: int) -> Mapping[str, Any]:
    for event in events:
        if event.get("id") == request_id and isinstance(event.get("result"), dict):
            return event["result"]
    raise ProviderHTTPError(
        "ACPX omitted a required ACP result", status_code=502, retryable=False
    )


def _result_for_method(
    events: Sequence[Mapping[str, Any]], method: str, fallback_id: int
) -> Mapping[str, Any]:
    request_ids = [
        event.get("id")
        for event in events
        if event.get("method") == method and event.get("id") is not None
    ]
    if len(request_ids) > 1:
        raise ProviderHTTPError(
            "ACPX returned ambiguous ACP request identifiers",
            status_code=502,
            retryable=False,
        )
    return _result_for_id(
        events, request_ids[0] if request_ids else fallback_id
    )


def _method_returned_error(
    events: Sequence[Mapping[str, Any]], method: str
) -> bool:
    request_ids = {
        event.get("id")
        for event in events
        if event.get("method") == method and event.get("id") is not None
    }
    return any(
        event.get("id") in request_ids and isinstance(event.get("error"), Mapping)
        for event in events
    )


def _returned_rate_limit(events: Sequence[Mapping[str, Any]]) -> bool:
    for event in events:
        error = event.get("error")
        if not isinstance(error, Mapping) or error.get("code") != -32003:
            continue
        if "rate limit" in str(error.get("message") or "").strip().lower():
            return True
    return False


def _returned_auth_required(events: Sequence[Mapping[str, Any]]) -> bool:
    """Recognize the ACP authentication-required code without reading its message."""
    return any(
        isinstance(event.get("error"), Mapping)
        and event["error"].get("code") == -32000
        for event in events
    )


def _observed_agent_identity(
    initialize: Mapping[str, Any], agent: str
) -> tuple[str, str]:
    agent_info = initialize.get("agentInfo")
    if isinstance(agent_info, Mapping):
        name = str(agent_info.get("name") or "").strip()
        version = str(agent_info.get("version") or "").strip()
        return name, version
    metadata = initialize.get("_meta")
    if (
        agent == "grok-build"
        and isinstance(metadata, Mapping)
        and metadata.get("grokShell") is True
    ):
        return "grok-build", str(metadata.get("agentVersion") or "").strip()
    return "", ""


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


def _mcp_tool_call_audit(
    events: Sequence[Mapping[str, Any]], allowed_tools: Sequence[str]
) -> dict[str, Any]:
    variants = _mcp_tool_variants(allowed_tools)
    seen_ids: set[str] = set()
    observed_names: list[str] = []
    canonical: list[str] = []
    unrecognized_mcp = 0
    auxiliary = 0
    for index, event in enumerate(events):
        params = event.get("params")
        update = params.get("update") if isinstance(params, Mapping) else None
        if not isinstance(update, Mapping) or update.get("sessionUpdate") != "tool_call":
            continue
        call_id = str(update.get("toolCallId") or f"event-{index}")
        if call_id in seen_ids:
            continue
        seen_ids.add(call_id)
        raw_input = update.get("rawInput")
        raw_name = (
            str(raw_input.get("tool_name") or "").strip()
            if isinstance(raw_input, Mapping)
            else ""
        )
        if not raw_name:
            raw_name = str(update.get("title") or "").strip()
        observed_names.append(raw_name)
        canonical_name = variants.get(raw_name)
        if canonical_name is None:
            if raw_name.startswith(
                ("peerbridge__", "mcp__peerbridge__", "mcp.peerbridge.")
            ):
                unrecognized_mcp += 1
            else:
                auxiliary += 1
        else:
            canonical.append(canonical_name)
    return {
        "agent_tool_call_event_count": len(seen_ids),
        "agent_auxiliary_tool_call_count": auxiliary,
        "mcp_tool_call_count": len(canonical) + unrecognized_mcp,
        "mcp_allowed_tool_call_count": len(canonical),
        "mcp_unrecognized_tool_call_count": unrecognized_mcp,
        "mcp_tool_error_count": 0,
        "mcp_canonical_tools_called": sorted(set(canonical)),
        "mcp_observed_tool_names_sha256": stable_sha256(observed_names),
    }


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
        conversation, attachments = extract_verified_attachments(
            self.config.project_root,
            messages,
        )
        if attachments:
            raise ConfigurationError(
                "legacy ACPX inference does not expose filesystem attachments; "
                "use a persistent ACPX session with provider-native content blocks"
            )
        prompt = _prompt_text(conversation)
        requested_model = _requested_model(self.config, self.agent)
        state_root = self.config.project_root / ".peerbridge"
        runtime = state_root / "runtime" / "acpx"
        _reject_reparse_ancestry(state_root, runtime, "ACPX runtime state")
        runtime.mkdir(parents=True, exist_ok=True)
        _reject_reparse_ancestry(state_root, runtime, "ACPX runtime state")
        mcp_config, mcp_config_sha = _mcp_config_path(
            self.config, self.agent, runtime
        )
        permission_policy, permission_policy_sha = _mcp_permission_policy(
            self.config.allowed_tools
        )
        if self.agent == "codex":
            return self._run_codex_persistent(
                prompt=prompt,
                message_id=message_id,
                requested_model=requested_model,
                runtime=runtime,
                mcp_config=mcp_config,
                mcp_config_sha=mcp_config_sha,
                permission_policy=permission_policy,
                permission_policy_sha=permission_policy_sha,
                attachments=(),
                attachment_cwd=None,
            )
        return self._run_one_shot(
            prompt=prompt,
            message_id=message_id,
            requested_model=requested_model,
            runtime=runtime,
            mcp_config=mcp_config,
            mcp_config_sha=mcp_config_sha,
            permission_policy=permission_policy,
            permission_policy_sha=permission_policy_sha,
            attachments=(),
            attachment_cwd=None,
        )

    def _child_environment(
        self,
    ) -> tuple[dict[str, str], Path | None, dict[str, Any]]:
        agent_runtime = find_acpx_agent_runtime(self.agent)
        override_names = _OFFICIAL_ROUTE_OVERRIDES[self.agent]
        environment = build_agent_child_environment(
            self.agent,
            required_path_roots=(agent_runtime.parent,) if agent_runtime else (),
            include_provider_credentials=False,
        )
        removed = sorted(
            override_names.intersection(str(name).upper() for name in os.environ)
        )
        for name in tuple(environment):
            normalized = name.upper()
            if normalized in override_names:
                removed.append(normalized)
                environment.pop(name, None)
        present = sorted(
            name.upper()
            for name in environment
            if name.upper() in override_names
        )
        removed = sorted(set(removed))
        if present:
            raise RouteMismatchError("ACPX child retained a provider selector")
        route_audit = {
            "requested_route_class": self.config.route_class,
            "observed_route_class": None,
            "provider_route_class": None,
            "provider_route_class_attested": False,
            "requested_provider_id": self.config.provider_id,
            "provider_environment_policy": "provider-credentials-stripped",
            "provider_override_names_removed": removed,
            "provider_override_names_present": present,
        }
        return environment, agent_runtime, route_audit

    def _raise_process_failure(self, stdout: bytes) -> None:
        try:
            events = _parse_events(stdout)
        except ProviderHTTPError:
            events = []
        if events and _method_returned_error(events, "authenticate"):
            raise CredentialUnavailableError("ACPX Agent authentication failed")
        if events and _returned_auth_required(events):
            raise CredentialUnavailableError(
                "ACPX Agent credential is unavailable or unsupported"
            )
        if events and _returned_rate_limit(events):
            raise ProviderHTTPError(
                "ACPX Agent rate limited",
                status_code=429,
                retryable=False,
            )
        raise ProviderHTTPError(
            "ACPX Agent execution failed",
            status_code=502,
            retryable=True,
        )

    def _receipt_base(
        self,
        *,
        prompt: str,
        answer: str,
        message_id: str | None,
        observed_agent: str,
        observed_agent_version: str,
        observed_model: str,
        observed_reasoning: str | None,
        mcp_config_sha: str,
        permission_policy_sha: str,
        usage: Mapping[str, Any],
        tool_call_audit: Mapping[str, Any],
        provider_route_audit: Mapping[str, Any],
        attachments: Sequence[VerifiedAttachment],
    ) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "schema": "peerbridge.acpx-inference-receipt.v1",
            "runtime": "acpx",
            "runtime_version": ACPX_VERSION,
            "secret_backend": "native-acp",
            "route_class": self.config.route_class,
            "route_class_source": "requested-route-profile-binding",
            "route_profile_id": self.config.route_profile_id,
            "route_profile_sha256": self.config.route_profile_sha256,
            "connection_id": self.config.connection_id,
            "agent_id": self.agent,
            "observed_agent_name": observed_agent,
            "observed_agent_version": observed_agent_version,
            "requested_model": self.config.model,
            "observed_model": observed_model,
            "requested_reasoning_mode": self.config.reasoning_mode,
            "observed_reasoning_mode": observed_reasoning,
            "message_id": message_id,
            "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
            "response_sha256": _sha256_bytes(answer.encode("utf-8")),
            "response_chars": len(answer),
            "executable_sha256": _sha256_bytes(self.executable.read_bytes()),
            "credential_values_read_by_peerbridge": False,
            "credential_values_recorded": False,
            "provider_credentials_forwarded_to_child": False,
            "agent_cached_auth_used": True,
            "filesystem_capability": bool(attachments),
            "filesystem_capability_scope": (
                "staged-chat-attachments-only" if attachments else "none"
            ),
            "terminal_capability": False,
            "mcp_tools_exposed": True,
            "mcp_allowed_tools_sha256": stable_sha256(self.config.allowed_tools),
            "mcp_config_sha256": mcp_config_sha,
            "permission_policy": "explicit-mcp-allowlist-default-deny",
            "permission_policy_sha256": permission_policy_sha,
            "usage": dict(usage),
            **dict(provider_route_audit),
            **dict(tool_call_audit),
        }
        if attachments:
            receipt["attachment_delivery"] = attachment_delivery_receipt(
                provider_id=self.config.provider_id,
                protocol="acpx-acp-filesystem",
                delivery_mode="verified_cwd_read_path",
                status="provider_turn_completed_with_read_capability",
                attachments=attachments,
            )
        return receipt

    def _run_one_shot(
        self,
        *,
        prompt: str,
        message_id: str | None,
        requested_model: str,
        runtime: Path,
        mcp_config: Path,
        mcp_config_sha: str,
        permission_policy: str,
        permission_policy_sha: str,
        attachments: Sequence[VerifiedAttachment],
        attachment_cwd: Path | None,
    ) -> InferenceResult:
        agent_cwd = (
            attachment_cwd.resolve(strict=True)
            if attachment_cwd is not None
            else self.config.project_root.resolve()
        )
        filesystem_args = ["--approve-reads"] if attachments else ["--no-fs"]
        command = [
            str(self.executable),
            "--cwd",
            str(agent_cwd),
            "--auth-policy",
            "skip",
            "--permission-policy",
            permission_policy,
            "--non-interactive-permissions",
            "fail",
            "--format",
            "json",
            "--json-strict",
            *filesystem_args,
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
        environment, _agent_runtime, provider_route_audit = (
            self._child_environment()
        )
        return_code, stdout, _stderr = self.process_runner(
            command,
            cwd=runtime,
            environment=environment,
            stdin_text=prompt,
            timeout_seconds=self.config.timeout_seconds,
            max_capture_bytes=MAX_CAPTURE_BYTES,
            runtime_label="ACPX runtime",
            cancel_event=self._cancel_event,
        )
        events = _parse_events(stdout)
        if return_code != 0:
            self._raise_process_failure(stdout)

        initialize = _result_for_method(events, "initialize", 0)
        session = _result_for_method(events, "session/new", 1)
        terminal = _result_for_method(events, "session/prompt", 2)
        observed_agent, observed_agent_version = _observed_agent_identity(
            initialize, self.agent
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
        tool_call_audit = _mcp_tool_call_audit(events, self.config.allowed_tools)
        receipt = self._receipt_base(
            prompt=prompt,
            answer=answer,
            message_id=message_id,
            observed_agent=observed_agent,
            observed_agent_version=observed_agent_version,
            observed_model=observed_model,
            observed_reasoning=observed_reasoning,
            mcp_config_sha=mcp_config_sha,
            permission_policy_sha=permission_policy_sha,
            usage=usage,
            tool_call_audit=tool_call_audit,
            provider_route_audit=provider_route_audit,
            attachments=attachments,
        )
        receipt.update(
            {
                "lifecycle_mode": "one-shot-exec-terminal",
                "session_soft_close_confirmed": True,
                "lifecycle_command_count": 1,
                "poll_count": 0,
                "event_count": len(events),
                "tool_event_count": _tool_event_count(events),
                "model_catalog_sha256": stable_sha256(catalog),
                "stdout_bytes": len(stdout),
            }
        )
        receipt["receipt_sha256"] = stable_sha256(receipt)
        return InferenceResult(
            assistant_message={"role": "assistant", "content": answer},
            receipt=receipt,
        )

    def _run_codex_persistent(
        self,
        *,
        prompt: str,
        message_id: str | None,
        requested_model: str,
        runtime: Path,
        mcp_config: Path,
        mcp_config_sha: str,
        permission_policy: str,
        permission_policy_sha: str,
        attachments: Sequence[VerifiedAttachment],
        attachment_cwd: Path | None,
    ) -> InferenceResult:
        session_name = f"peerbridge-codex-{uuid.uuid4().hex}"
        environment, agent_runtime, provider_route_audit = (
            self._child_environment()
        )
        agent_cwd = (
            attachment_cwd.resolve(strict=True)
            if attachment_cwd is not None
            else runtime.resolve()
        )
        filesystem_args = ["--approve-reads"] if attachments else ["--no-fs"]
        base = [
            str(self.executable),
            "--cwd",
            str(agent_cwd),
            "--auth-policy",
            "skip",
            "--permission-policy",
            permission_policy,
            "--non-interactive-permissions",
            "fail",
            "--format",
            "json",
            "--json-strict",
            *filesystem_args,
            "--no-terminal",
            "--mcp-config",
            str(mcp_config),
            "--max-turns",
            "1",
            "--prompt-retries",
            "0",
            "--timeout",
            str(max(1, int(self.config.timeout_seconds))),
        ]
        stdout_bytes = 0
        command_count = 0

        def invoke(parts: Sequence[str], *, stdin_text: str = "") -> dict[str, Any]:
            nonlocal stdout_bytes, command_count
            command_count += 1
            return_code, stdout, _stderr = self.process_runner(
                [*base, *parts],
                cwd=runtime,
                environment=environment,
                stdin_text=stdin_text,
                timeout_seconds=self.config.timeout_seconds,
                max_capture_bytes=MAX_CAPTURE_BYTES,
                runtime_label="ACPX Codex lifecycle",
                cancel_event=self._cancel_event,
            )
            stdout_bytes += len(stdout)
            if return_code != 0:
                self._raise_process_failure(stdout)
            return _parse_cli_object(stdout)

        def invoke_events(
            parts: Sequence[str], *, stdin_text: str = ""
        ) -> list[dict[str, Any]]:
            nonlocal stdout_bytes, command_count
            command_count += 1
            return_code, stdout, _stderr = self.process_runner(
                [*base, *parts],
                cwd=runtime,
                environment=environment,
                stdin_text=stdin_text,
                timeout_seconds=self.config.timeout_seconds,
                max_capture_bytes=MAX_CAPTURE_BYTES,
                runtime_label="ACPX Codex blocking prompt",
                cancel_event=self._cancel_event,
            )
            stdout_bytes += len(stdout)
            events = _parse_events(stdout)
            if return_code != 0:
                self._raise_process_failure(stdout)
            return events

        created = False
        primary_error: Exception | None = None
        close_error: Exception | None = None
        record: dict[str, Any] | None = None
        answer = ""
        raw_usage: Mapping[str, Any] | None = None
        tool_call_audit: Mapping[str, Any] = {}
        observed_model = ""
        observed_reasoning: str | None = None
        catalog: list[Any] = []
        poll_count = 0
        record_id = ""
        provisional_acp_session_id = ""
        effective_acp_session_id = ""
        prompt_events: list[dict[str, Any]] = []
        prompt_answer = ""
        prompt_tool_audit: Mapping[str, Any] = {}
        observed_agent = ""
        observed_agent_version = ""
        prompt_dispatched = False
        try:
            created_payload = invoke(
                ("codex", "sessions", "new", "--name", session_name)
            )
            if (
                created_payload.get("action") != "session_ensured"
                or created_payload.get("created") is not True
                or created_payload.get("name") != session_name
                or created_payload.get("replacedSessionId") is not None
            ):
                raise RouteMismatchError("ACPX Codex session creation drifted")
            record_id = str(created_payload.get("acpxRecordId") or "").strip()
            provisional_acp_session_id = str(
                created_payload.get("acpxSessionId") or ""
            ).strip()
            if not record_id or not provisional_acp_session_id:
                raise ProviderHTTPError(
                    "ACPX Codex session identity was incomplete",
                    status_code=502,
                    retryable=False,
                )
            if provisional_acp_session_id != record_id:
                raise RouteMismatchError(
                    "ACPX Codex provisional session identity drifted"
                )
            created = True

            model_payload = invoke(
                ("codex", "set", "--session", session_name, "model", requested_model)
            )
            if (
                model_payload.get("action") != "model_set"
                or model_payload.get("modelId") != requested_model
                or model_payload.get("acpxRecordId") != record_id
            ):
                raise RouteMismatchError("ACPX Codex model selection drifted")

            requested_reasoning = str(self.config.reasoning_mode or "").strip()
            if requested_reasoning:
                reasoning_payload = invoke(
                    (
                        "codex",
                        "set",
                        "--session",
                        session_name,
                        "reasoning_effort",
                        requested_reasoning,
                    )
                )
                if (
                    reasoning_payload.get("action") != "config_set"
                    or reasoning_payload.get("configId") != "reasoning_effort"
                    or reasoning_payload.get("value") != requested_reasoning
                    or reasoning_payload.get("acpxRecordId") != record_id
                ):
                    raise RouteMismatchError("ACPX Codex reasoning selection drifted")

            prompt_dispatched = True
            prompt_events = invoke_events(
                (
                    "codex",
                    "prompt",
                    "--session",
                    session_name,
                    "--file",
                    "-",
                ),
                stdin_text=prompt,
            )
            initialize = _result_for_method(prompt_events, "initialize", 0)
            terminal = _result_for_method(prompt_events, "session/prompt", 2)
            observed_agent, observed_agent_version = _observed_agent_identity(
                initialize, self.agent
            )
            if observed_agent not in _EXPECTED_AGENT_NAMES[self.agent]:
                raise RouteMismatchError("ACPX observed Agent identity drifted")
            if terminal.get("stopReason") != "end_turn":
                raise ProviderHTTPError(
                    "ACPX Codex Agent did not complete the turn",
                    status_code=502,
                    retryable=False,
                )
            prompt_answer = _assistant_text(prompt_events)
            prompt_tool_audit = _mcp_tool_call_audit(
                prompt_events, self.config.allowed_tools
            )

            deadline = time.monotonic() + max(1.0, float(self.config.timeout_seconds))
            while True:
                if self._cancel_event.is_set():
                    raise ResourceUnavailableError("ACPX Codex session was cancelled")
                poll_count += 1
                candidate = invoke(("codex", "sessions", "show", session_name))
                candidate_record_id = str(
                    _record_value(candidate, "acpxRecordId", "acpx_record_id") or ""
                ).strip()
                candidate_session_id = str(
                    _record_value(candidate, "acpSessionId", "acp_session_id") or ""
                ).strip()
                if (
                    candidate.get("name") != session_name
                    or candidate_record_id != record_id
                    or not candidate_session_id
                ):
                    raise RouteMismatchError("ACPX Codex session identity drifted")
                if not effective_acp_session_id:
                    effective_acp_session_id = candidate_session_id
                elif candidate_session_id != effective_acp_session_id:
                    raise RouteMismatchError("ACPX Codex session identity drifted")
                if candidate.get("closed") is True:
                    raise ProviderHTTPError(
                        "ACPX Codex session closed before turn completion",
                        status_code=502,
                        retryable=False,
                    )
                event_log = _record_value(candidate, "eventLog", "event_log")
                if isinstance(event_log, Mapping) and _record_value(
                    event_log, "lastWriteError", "last_write_error"
                ) is not None:
                    raise ProviderHTTPError(
                        "ACPX Codex session event log failed",
                        status_code=502,
                        retryable=False,
                    )
                last_request = str(
                    _record_value(candidate, "lastRequestId", "last_request_id") or ""
                ).strip()
                agent_messages = _record_agent_messages(candidate)
                candidate_answer = _record_assistant_text(agent_messages)
                candidate_audit, tool_results_complete = _record_tool_result_audit(
                    agent_messages, self.config.allowed_tools
                )
                request_usage = candidate.get("request_token_usage")
                user_ids = _record_user_ids(candidate)
                matched_user_ids = (
                    [value for value in user_ids if value in request_usage]
                    if isinstance(request_usage, Mapping)
                    else []
                )
                if (
                    last_request
                    and candidate_answer
                    and candidate_answer == prompt_answer
                    and tool_results_complete
                    and matched_user_ids
                    and candidate_audit.get("mcp_tool_call_count")
                    == prompt_tool_audit.get("mcp_tool_call_count")
                    and candidate_audit.get("mcp_allowed_tool_call_count")
                    == prompt_tool_audit.get("mcp_allowed_tool_call_count")
                    and candidate_audit.get("mcp_unrecognized_tool_call_count")
                    == prompt_tool_audit.get("mcp_unrecognized_tool_call_count")
                    and candidate_audit.get("mcp_canonical_tools_called")
                    == prompt_tool_audit.get("mcp_canonical_tools_called")
                ):
                    record = candidate
                    answer = candidate_answer
                    raw_usage = request_usage.get(matched_user_ids[-1])
                    tool_call_audit = candidate_audit
                    break
                if time.monotonic() >= deadline:
                    raise ProviderHTTPError(
                        "ACPX Codex session timed out",
                        status_code=504,
                        retryable=True,
                    )
                time.sleep(0.25)

            observed_model, observed_reasoning, catalog = _record_config(record)
            if observed_model != requested_model:
                raise RouteMismatchError("ACPX observed model identity drifted")
            if requested_reasoning and observed_reasoning != requested_reasoning:
                raise RouteMismatchError("ACPX observed reasoning mode drifted")
            agent_command = str(
                _record_value(record, "agentCommand", "agent_command") or ""
            ).strip()
            if "@agentclientprotocol/codex-acp" not in agent_command:
                raise RouteMismatchError("ACPX observed Agent identity drifted")
        except Exception as exc:
            primary_error = exc
        finally:
            if created:
                try:
                    closed_payload = invoke(
                        ("codex", "sessions", "close", session_name)
                    )
                    closed_session_id = str(
                        closed_payload.get("acpxSessionId") or ""
                    ).strip()
                    expected_session_id = (
                        effective_acp_session_id or provisional_acp_session_id
                    )
                    if (
                        closed_payload.get("action") != "session_closed"
                        or closed_payload.get("acpxRecordId") != record_id
                        or not closed_session_id
                        or closed_session_id != expected_session_id
                    ):
                        raise RouteMismatchError("ACPX Codex soft-close drifted")
                except Exception as exc:
                    close_error = exc

        if primary_error is not None:
            ambiguous_paid_work = prompt_dispatched and (
                isinstance(primary_error, (ResourceUnavailableError, RunCancelledError))
                or (
                    isinstance(primary_error, ProviderHTTPError)
                    and primary_error.retryable
                    and primary_error.status_code != 429
                )
            )
            if ambiguous_paid_work:
                status_code = (
                    primary_error.status_code
                    if isinstance(primary_error, ProviderHTTPError)
                    else 502
                )
                raise ProviderHTTPError(
                    "ACPX Codex paid turn outcome is ambiguous and cannot be replayed",
                    status_code=status_code,
                    retryable=False,
                ) from primary_error
            raise primary_error
        if close_error is not None:
            raise ProviderHTTPError(
                "ACPX Codex session soft-close failed",
                status_code=502,
                retryable=False,
            ) from close_error
        if (
            record is None
            or not answer
            or not isinstance(raw_usage, Mapping)
            or not effective_acp_session_id
        ):
            raise ProviderHTTPError(
                "ACPX Codex session completion evidence was incomplete",
                status_code=502,
                retryable=False,
            )

        normalized_raw_usage = dict(raw_usage)
        if "reasoning_tokens" not in normalized_raw_usage and isinstance(
            normalized_raw_usage.get("thought_tokens"), int
        ):
            normalized_raw_usage["reasoning_tokens"] = normalized_raw_usage[
                "thought_tokens"
            ]
        usage = normalize_provider_usage(
            normalized_raw_usage,
            source="acpx/codex-persistent-session",
        )
        agent_command = str(
            _record_value(record, "agentCommand", "agent_command") or ""
        ).strip()
        receipt = self._receipt_base(
            prompt=prompt,
            answer=answer,
            message_id=message_id,
            observed_agent=observed_agent,
            observed_agent_version=observed_agent_version,
            observed_model=observed_model,
            observed_reasoning=observed_reasoning,
            mcp_config_sha=mcp_config_sha,
            permission_policy_sha=permission_policy_sha,
            usage=usage,
            tool_call_audit=tool_call_audit,
            provider_route_audit=provider_route_audit,
            attachments=attachments,
        )
        receipt.update(
            {
                "lifecycle_mode": "persistent-session-blocking-prompt-poll-soft-close",
                "session_soft_close_confirmed": True,
                "lifecycle_command_count": command_count,
                "poll_count": poll_count,
                "session_name_sha256": _sha256_bytes(session_name.encode("utf-8")),
                "acpx_record_id_sha256": _sha256_bytes(record_id.encode("utf-8")),
                "provisional_acp_session_id_sha256": _sha256_bytes(
                    provisional_acp_session_id.encode("utf-8")
                ),
                "acp_session_id_sha256": _sha256_bytes(
                    effective_acp_session_id.encode("utf-8")
                ),
                "acp_session_transition_observed": (
                    provisional_acp_session_id != effective_acp_session_id
                ),
                "agent_command_sha256": _sha256_bytes(agent_command.encode("utf-8")),
                "agent_runtime_executable_sha256": (
                    _sha256_bytes(agent_runtime.read_bytes())
                    if agent_runtime is not None and agent_runtime.is_file()
                    else None
                ),
                "protocol_version": _record_value(
                    record, "protocolVersion", "protocol_version"
                ),
                "event_count": len(prompt_events),
                "record_last_seq": int(
                    _record_value(record, "lastSeq", "last_seq") or 0
                ),
                "tool_event_count": _tool_event_count(prompt_events),
                "stream_and_record_tool_audit_equal": True,
                "model_catalog_sha256": stable_sha256(catalog),
                "stdout_bytes": stdout_bytes,
            }
        )
        receipt["receipt_sha256"] = stable_sha256(receipt)
        return InferenceResult(
            assistant_message={"role": "assistant", "content": answer},
            receipt=receipt,
        )


__all__ = [
    "ACPX_VERSION",
    "AcpxRunner",
    "SUPPORTED_AGENTS",
    "find_acpx",
    "find_acpx_agent_runtime",
    "native_acp_runtime_available",
    "verify_acpx_inference_receipt",
]
