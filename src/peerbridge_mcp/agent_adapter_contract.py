"""Versioned vendor-neutral contracts for official coding Agent adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol


AGENT_ADAPTER_CONTRACT_VERSION = "peerbridge.agent-adapter.v1"
CAPABILITY_STATES = frozenset({"supported", "conditional", "unsupported"})
SAFE_ADAPTER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")


@dataclass(frozen=True)
class AdapterCapability:
    capability_id: str
    state: Literal["supported", "conditional", "unsupported"]
    evidence: str
    limitation: str | None = None

    def __post_init__(self) -> None:
        if not SAFE_ADAPTER_ID.fullmatch(self.capability_id):
            raise ValueError("invalid Agent adapter capability id")
        if self.state not in CAPABILITY_STATES:
            raise ValueError("invalid Agent adapter capability state")
        if not self.evidence.strip() or len(self.evidence) > 240:
            raise ValueError("invalid Agent adapter capability evidence")
        if self.limitation is not None and (
            not self.limitation.strip() or len(self.limitation) > 500
        ):
            raise ValueError("invalid Agent adapter capability limitation")

    def as_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "state": self.state,
            "evidence": self.evidence,
            "limitation": self.limitation,
        }


@dataclass(frozen=True)
class AgentAdapterDescriptor:
    adapter_id: str
    agent_id: str
    vendor: str
    transport: str
    provider_identity: str
    official: bool
    capabilities: tuple[AdapterCapability, ...]
    contract_version: str = AGENT_ADAPTER_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for value, label in (
            (self.adapter_id, "adapter id"),
            (self.agent_id, "Agent id"),
            (self.transport, "transport"),
            (self.provider_identity, "provider identity"),
        ):
            if not SAFE_ADAPTER_ID.fullmatch(value):
                raise ValueError(f"invalid {label}")
        if not self.vendor.strip() or len(self.vendor) > 120:
            raise ValueError("invalid Agent adapter vendor")
        if self.contract_version != AGENT_ADAPTER_CONTRACT_VERSION:
            raise ValueError("unsupported Agent adapter contract version")
        capability_ids = [row.capability_id for row in self.capabilities]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("Agent adapter capabilities contain duplicates")

    def capability(self, capability_id: str) -> AdapterCapability | None:
        return next(
            (
                row
                for row in self.capabilities
                if row.capability_id == capability_id
            ),
            None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "adapter_id": self.adapter_id,
            "agent_id": self.agent_id,
            "vendor": self.vendor,
            "transport": self.transport,
            "provider_identity": self.provider_identity,
            "official": self.official,
            "capabilities": [row.as_dict() for row in self.capabilities],
        }


class AgentSession(Protocol):
    session_id: str

    def start(self) -> None: ...

    def submit(self, text: str, *, attachments: Iterable[Any] = ()) -> None: ...

    def snapshot(self, *, after_sequence: int = 0) -> Mapping[str, Any]: ...

    def interrupt(self) -> None: ...

    def stop(self) -> None: ...


SessionFactory = Callable[..., AgentSession]


@dataclass(frozen=True)
class RegisteredAgentAdapter:
    descriptor: AgentAdapterDescriptor
    session_factory: SessionFactory
    preferred: bool = True


class AgentAdapterRegistry:
    """Immutable-by-default registry with exact Agent identity lookup."""

    def __init__(self, adapters: Iterable[RegisteredAgentAdapter] = ()) -> None:
        self._by_agent: dict[str, list[RegisteredAgentAdapter]] = {}
        self._by_adapter: dict[str, RegisteredAgentAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: RegisteredAgentAdapter) -> None:
        descriptor = adapter.descriptor
        if descriptor.adapter_id in self._by_adapter:
            raise ValueError("Agent adapter registry contains duplicate adapter identity")
        rows = self._by_agent.setdefault(descriptor.agent_id, [])
        if adapter.preferred and any(row.preferred for row in rows):
            raise ValueError("Agent adapter registry contains duplicate preferred adapter")
        rows.append(adapter)
        self._by_adapter[descriptor.adapter_id] = adapter

    def for_agent(
        self, agent_id: str, *, adapter_id: str | None = None
    ) -> RegisteredAgentAdapter:
        if adapter_id is not None:
            adapter = self._by_adapter.get(adapter_id)
            if adapter is None or adapter.descriptor.agent_id != agent_id:
                raise KeyError("Agent has no matching registered adapter")
            return adapter
        try:
            rows = self._by_agent[agent_id]
        except KeyError as exc:
            raise KeyError("Agent has no registered adapter") from exc
        preferred = next((row for row in rows if row.preferred), None)
        if preferred is None:
            raise KeyError("Agent has no preferred registered adapter")
        return preferred

    def adapters_for_agent(self, agent_id: str) -> tuple[RegisteredAgentAdapter, ...]:
        return tuple(self._by_agent.get(agent_id, ()))

    def descriptors(self) -> tuple[AgentAdapterDescriptor, ...]:
        return tuple(
            self._by_adapter[key].descriptor for key in sorted(self._by_adapter)
        )

    def capability_matrix(self) -> dict[str, dict[str, str]]:
        matrix: dict[str, dict[str, str]] = {}
        rank = {"unsupported": 0, "conditional": 1, "supported": 2}
        for descriptor in self.descriptors():
            agent = matrix.setdefault(descriptor.agent_id, {})
            for row in descriptor.capabilities:
                current = agent.get(row.capability_id)
                if current is None or rank[row.state] > rank[current]:
                    agent[row.capability_id] = row.state
        return matrix


__all__ = [
    "AGENT_ADAPTER_CONTRACT_VERSION",
    "AdapterCapability",
    "AgentAdapterDescriptor",
    "AgentAdapterRegistry",
    "AgentSession",
    "RegisteredAgentAdapter",
    "SessionFactory",
]
