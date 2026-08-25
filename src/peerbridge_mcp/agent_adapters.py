"""PeerBridge capability descriptors for each official Agent transport."""

from __future__ import annotations

from .agent_adapter_contract import AdapterCapability, AgentAdapterDescriptor


def _supported(capability_id: str, evidence: str) -> AdapterCapability:
    return AdapterCapability(capability_id, "supported", evidence)


def _conditional(
    capability_id: str, evidence: str, limitation: str
) -> AdapterCapability:
    return AdapterCapability(capability_id, "conditional", evidence, limitation)


def _unsupported(capability_id: str, evidence: str) -> AdapterCapability:
    return AdapterCapability(capability_id, "unsupported", evidence)


CODEX_ADAPTER = AgentAdapterDescriptor(
    adapter_id="codex-app-server",
    agent_id="codex",
    vendor="OpenAI",
    transport="app-server-jsonrpc",
    provider_identity="openai-official-codex",
    official=True,
    capabilities=(
        _supported("model-selection", "app-server thread and turn model fields"),
        _supported("reasoning-selection", "app-server turn effort field"),
        _supported("persistent-session", "thread start and resume"),
        _supported("session-resume", "thread/resume"),
        _supported("session-fork", "thread/fork"),
        _supported("session-compact", "thread/compact/start"),
        _supported("native-review", "review/start"),
        _supported("image-input", "turn/start localImage"),
        _supported("observable-events", "app-server item and turn events"),
        _supported("token-usage", "thread/tokenUsage/updated"),
        _supported(
            "interactive-approval",
            "item commandExecution/fileChange requestApproval",
        ),
        _supported("history-import", "official app-server thread listing"),
    ),
)


CLAUDE_ADAPTER = AgentAdapterDescriptor(
    adapter_id="claude-stream-json",
    agent_id="claude-code",
    vendor="Anthropic",
    transport="stream-json-control",
    provider_identity="anthropic-claude-code-cli",
    official=True,
    capabilities=(
        _supported("model-selection", "Claude Code --model"),
        _conditional(
            "reasoning-selection",
            "model aliases and provider settings",
            "not every Claude Code model exposes a separate effort control",
        ),
        _supported("persistent-session", "stream-json session id"),
        _supported("session-resume", "Claude Code --resume"),
        _supported("session-fork", "Claude Code --fork-session"),
        _unsupported("session-compact", "stream-json host has no stable compact RPC"),
        _unsupported("native-review", "stream-json host has no stable review RPC"),
        _supported("image-input", "native image content blocks"),
        _supported("observable-events", "stream-json events"),
        _supported("token-usage", "result and stream usage fields"),
        _supported(
            "interactive-approval",
            "can_use_tool stdio control_request/control_response channel",
        ),
        _supported("history-import", "official Claude session JSONL format"),
    ),
)


def _acp_descriptor(
    *, adapter_id: str, agent_id: str, vendor: str, provider_identity: str
) -> AgentAdapterDescriptor:
    return AgentAdapterDescriptor(
        adapter_id=adapter_id,
        agent_id=agent_id,
        vendor=vendor,
        transport="agent-client-protocol",
        provider_identity=provider_identity,
        official=True,
        capabilities=(
            _supported("model-selection", "ACP session config option"),
            _supported("reasoning-selection", "ACP thought-level config option when advertised"),
            _supported("persistent-session", "ACP session/new and session/load"),
            _supported("session-resume", "ACPX named session ensure/load"),
            _unsupported("session-fork", "current PeerBridge ACP profile has no fork mapping"),
            _unsupported("session-compact", "current PeerBridge ACP profile has no compact mapping"),
            _unsupported("native-review", "current PeerBridge ACP profile has no review mapping"),
            _conditional(
                "image-input",
                "ACP promptCapabilities.image",
                "available only when the live Agent advertises image input",
            ),
            _supported("observable-events", "ACP session/update and tool_call events"),
            _supported("token-usage", "ACP session status usage when advertised"),
            _supported("interactive-approval", "ACP session/request_permission"),
            _conditional(
                "history-import",
                "ACP session history when the Agent exports it",
                "empty or non-exportable native sessions remain metadata only",
            ),
        ),
    )


GROK_ADAPTER = _acp_descriptor(
    adapter_id="grok-native-acp",
    agent_id="grok",
    vendor="xAI",
    provider_identity="xai-official-grok-build",
)

KIMI_ADAPTER = _acp_descriptor(
    adapter_id="kimi-native-acp",
    agent_id="kimi-code",
    vendor="Moonshot AI",
    provider_identity="moonshot-official-kimi-code",
)

OFFICIAL_AGENT_ADAPTER_DESCRIPTORS = (
    CODEX_ADAPTER,
    CLAUDE_ADAPTER,
    GROK_ADAPTER,
    KIMI_ADAPTER,
)


def official_agent_adapter_descriptors() -> tuple[AgentAdapterDescriptor, ...]:
    return OFFICIAL_AGENT_ADAPTER_DESCRIPTORS


__all__ = [
    "CLAUDE_ADAPTER",
    "CODEX_ADAPTER",
    "GROK_ADAPTER",
    "KIMI_ADAPTER",
    "OFFICIAL_AGENT_ADAPTER_DESCRIPTORS",
    "official_agent_adapter_descriptors",
]
