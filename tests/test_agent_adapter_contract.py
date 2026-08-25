from __future__ import annotations

from dataclasses import replace

import pytest

from peerbridge_mcp.agent_adapter_contract import (
    AGENT_ADAPTER_CONTRACT_VERSION,
    AgentAdapterRegistry,
    RegisteredAgentAdapter,
)
from peerbridge_mcp.agent_adapters import (
    CLAUDE_ADAPTER,
    CODEX_ADAPTER,
    GROK_ADAPTER,
    KIMI_ADAPTER,
    official_agent_adapter_descriptors,
)


class _Session:
    pass


def test_official_adapter_descriptors_are_versioned_and_identity_distinct() -> None:
    descriptors = official_agent_adapter_descriptors()

    assert tuple(row.agent_id for row in descriptors) == (
        "codex",
        "claude-code",
        "grok",
        "kimi-code",
    )
    assert all(
        row.contract_version == AGENT_ADAPTER_CONTRACT_VERSION
        for row in descriptors
    )
    assert len({row.adapter_id for row in descriptors}) == 4
    assert len({row.provider_identity for row in descriptors}) == 4
    assert all(row.official for row in descriptors)


def test_capability_union_does_not_downgrade_other_agents() -> None:
    registry = AgentAdapterRegistry(
        RegisteredAgentAdapter(row, _Session)
        for row in official_agent_adapter_descriptors()
    )

    matrix = registry.capability_matrix()
    assert matrix["codex"]["interactive-approval"] == "supported"
    assert matrix["grok"]["interactive-approval"] == "supported"
    assert matrix["kimi-code"]["interactive-approval"] == "supported"
    assert matrix["claude-code"]["interactive-approval"] == "supported"
    assert matrix["claude-code"]["session-compact"] == "unsupported"
    assert matrix["codex"]["session-compact"] == "supported"


def test_registry_resolves_exact_agent_without_vendor_fallback() -> None:
    registry = AgentAdapterRegistry(
        (
            RegisteredAgentAdapter(CODEX_ADAPTER, _Session),
            RegisteredAgentAdapter(CLAUDE_ADAPTER, _Session),
            RegisteredAgentAdapter(GROK_ADAPTER, _Session),
            RegisteredAgentAdapter(KIMI_ADAPTER, _Session),
        )
    )

    assert registry.for_agent("grok").descriptor is GROK_ADAPTER
    with pytest.raises(KeyError, match="no registered adapter"):
        registry.for_agent("grok-relay")


def test_registry_allows_alternate_transport_but_rejects_identity_collisions() -> None:
    registry = AgentAdapterRegistry(
        (RegisteredAgentAdapter(CODEX_ADAPTER, _Session),)
    )
    alternate = replace(
        CODEX_ADAPTER,
        adapter_id="codex-acp-alternate",
        transport="agent-client-protocol",
    )
    registry.register(RegisteredAgentAdapter(alternate, _Session, preferred=False))

    assert len(registry.adapters_for_agent("codex")) == 2
    assert registry.for_agent("codex").descriptor is CODEX_ADAPTER
    assert registry.for_agent(
        "codex", adapter_id="codex-acp-alternate"
    ).descriptor is alternate

    with pytest.raises(ValueError, match="duplicate adapter"):
        registry.register(RegisteredAgentAdapter(CODEX_ADAPTER, _Session))
    second_preferred = replace(
        CODEX_ADAPTER,
        adapter_id="codex-second-preferred",
        transport="alternate-jsonrpc",
    )
    with pytest.raises(ValueError, match="duplicate preferred"):
        registry.register(RegisteredAgentAdapter(second_preferred, _Session))
