from __future__ import annotations

from pathlib import Path

import pytest

from peerbridge_mcp import resource_guard
from peerbridge_mcp.resource_guard import (
    MemoryPressureError,
    MemorySnapshot,
    ResourceGuardError,
    ResourcePolicy,
    RuntimeCapacityError,
    provider_runtime_slot,
    require_memory_headroom,
)


def policy(*, available_mib: int = 128, concurrency: int = 1) -> ResourcePolicy:
    return ResourcePolicy(
        min_available_bytes=available_mib * resource_guard.MIB,
        min_available_fraction=0.01,
        max_concurrent_runtimes=concurrency,
    )


def test_memory_pressure_gate_is_fail_closed_without_sensitive_details() -> None:
    observed = MemorySnapshot(
        total_physical_bytes=16 * 1024 * resource_guard.MIB,
        available_physical_bytes=64 * resource_guard.MIB,
    )
    with pytest.raises(MemoryPressureError, match="memory pressure") as caught:
        require_memory_headroom(snapshot=observed, policy=policy())
    assert "64" not in str(caught.value)


def test_memory_pressure_gate_accepts_safe_headroom() -> None:
    observed = MemorySnapshot(
        total_physical_bytes=16 * 1024 * resource_guard.MIB,
        available_physical_bytes=4 * 1024 * resource_guard.MIB,
    )
    assert require_memory_headroom(snapshot=observed, policy=policy()) is observed


def test_environment_policy_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEERBRIDGE_MAX_RUNTIME_CONCURRENCY", "0")
    with pytest.raises(ResourceGuardError, match="supported range"):
        ResourcePolicy.from_environment()


def test_runtime_slot_prevents_duplicate_provider_work_and_releases_after_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe = MemorySnapshot(
        total_physical_bytes=16 * 1024 * resource_guard.MIB,
        available_physical_bytes=8 * 1024 * resource_guard.MIB,
    )
    monkeypatch.setattr(resource_guard, "memory_snapshot", lambda: safe)
    selected = policy(concurrency=1)

    with provider_runtime_slot(policy=selected, slot_root=tmp_path):
        with pytest.raises(RuntimeCapacityError, match="slots are occupied"):
            with provider_runtime_slot(policy=selected, slot_root=tmp_path):
                pass

    with provider_runtime_slot(policy=selected, slot_root=tmp_path) as observed:
        assert observed == safe
