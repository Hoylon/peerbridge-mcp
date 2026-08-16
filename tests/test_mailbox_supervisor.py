from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from peerbridge_mcp import mailbox_supervisor as supervisor_module
from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.credentials import credential_target
from peerbridge_mcp.mailbox_supervisor import (
    MailboxSupervisor,
    SupervisorAlreadyRunningError,
    SupervisorError,
    discover_runnable_routes,
)
from peerbridge_mcp.openai_compatible_runner import (
    InferenceResult,
    ResourceUnavailableError,
)


HEX_A = "a" * 64
HEX_B = "b" * 64


@pytest.fixture(autouse=True)
def deterministic_runtime_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def admitted():
        yield

    monkeypatch.setattr(supervisor_module, "provider_runtime_admission", admitted)


def make_bridge(root: Path, agent: str, *, session: str | None = None) -> Bridge:
    return Bridge(
        root,
        root / ".peerbridge" / "peerbridge.sqlite3",
        agent,
        "test",
        session_id=session,
    )


def register_route(
    bridge: Bridge,
    *,
    agent_id: str = "model-peer",
    connection_id: str = "relay-one",
    route_id: str = "relay-one-model-a",
    model_id: str = "model-a",
    backend: str = "windows-credential-manager",
) -> None:
    target = (
        credential_target("test", connection_id)
        if backend == "windows-credential-manager"
        else "ccswitch-reference"
    )
    bridge.upsert_provider_connection(
        {
            "connection_id": connection_id,
            "display_name": connection_id,
            "route_class": "relay",
            "provider_id": connection_id,
            "secret_backend": backend,
            "credential_target": target,
            "endpoint_sha256": HEX_A,
            "credential_fingerprint_sha256": HEX_B,
            "descriptor_schema": "peerbridge-provider-v2",
            "credential_version_sha256": HEX_A,
            "enabled": True,
        }
    )
    bridge.upsert_route_profile(
        {
            "route_id": route_id,
            "agent_id": agent_id,
            "client_name": "test-client",
            "provider_id": connection_id,
            "model_id": model_id,
            "reasoning_mode": "high",
            "route_class": "relay",
            "enabled": True,
        }
    )


class SuccessfulRunner:
    configs = []

    def __init__(self, config) -> None:
        self.config = config
        self.__class__.configs.append(config)

    def run(self, messages, *, message_id=None) -> InferenceResult:
        assert messages[0]["role"] == "system"
        assert "hello supervisor" in messages[1]["content"]
        assert message_id
        return InferenceResult(
            assistant_message={"role": "assistant", "content": "audited reply"},
            receipt={"receipt_sha256": HEX_B},
        )


def test_supervisor_completes_one_routed_message_exactly_once(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator", session="human")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "mailbox-e2e",
            "subject": "reply once",
            "body": "hello supervisor",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=SuccessfulRunner,
        credential_probe=lambda _route: True,
    )

    first = supervisor.run_cycle()
    second = supervisor.run_cycle()
    assert first.runnable_routes == 1
    assert (first.claimed, first.completed) == (1, 1)
    assert (second.claimed, second.completed) == (0, 0)
    assert SuccessfulRunner.configs[-1].agent_id == "model-peer"
    assert SuccessfulRunner.configs[-1].room_id == "lobby"
    assert SuccessfulRunner.configs[-1].route_profile_id == "relay-one-model-a"
    assert SuccessfulRunner.configs[-1].route_profile_sha256
    assert SuccessfulRunner.configs[-1].response_only_fallback_on_tool_error is True

    with sqlite3.connect(human.db_path) as connection:
        replies = connection.execute(
            "SELECT body, reply_to FROM messages WHERE sender='model-peer'"
        ).fetchall()
        dispatch = connection.execute(
            "SELECT status, attempt_count, inference_receipt_sha256 "
            "FROM message_dispatches WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()
    assert replies == [("audited reply", sent["message_id"])]
    assert dispatch == ("completed", 1, HEX_B)
    assert human.status()["message_dispatch_counts"] == {"completed": 1}
    supervisor.close()
    assert "model-peer" not in human.presence_snapshot()["online_agents"]


class RetryThenSucceedRunner(SuccessfulRunner):
    attempts = 0

    def run(self, messages, *, message_id=None) -> InferenceResult:
        self.__class__.attempts += 1
        if self.__class__.attempts == 1:
            raise ResourceUnavailableError("synthetic resource pressure")
        return super().run(messages, message_id=message_id)


def test_supervisor_retry_is_bounded_and_crash_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr("peerbridge_mcp.bridge.time.time", lambda: clock[0])
    RetryThenSucceedRunner.attempts = 0
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "retry-e2e",
            "subject": "retry safely",
            "body": "hello supervisor",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=RetryThenSucceedRunner,
        credential_probe=lambda _route: True,
        max_attempts=3,
    )
    failed = supervisor.run_cycle()
    cooling_down = supervisor.run_cycle()
    assert (failed.retryable_failures, failed.completed) == (1, 0)
    assert (cooling_down.claimed, cooling_down.completed) == (0, 0)
    with sqlite3.connect(human.db_path) as connection:
        state = connection.execute(
            "SELECT status, attempt_count FROM message_dispatches"
        ).fetchone()
        schedule = connection.execute(
            "SELECT attempt_count, not_before_epoch, error_code "
            "FROM message_dispatch_retry_schedules"
        ).fetchone()
    assert state == ("retryable", 1)
    assert schedule == (1, 1_015.0, "resource_unavailable")
    supervisor.close()

    clock[0] = 1_016.0
    recovered = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=RetryThenSucceedRunner,
        credential_probe=lambda _route: True,
        max_attempts=3,
    )
    completed = recovered.run_cycle()
    assert (completed.retryable_failures, completed.completed) == (0, 1)
    with sqlite3.connect(human.db_path) as connection:
        state = connection.execute(
            "SELECT status, attempt_count FROM message_dispatches"
        ).fetchone()
    assert state == ("completed", 2)
    recovered.close()


def test_ccswitch_route_is_discovered_but_requires_a_runtime_probe(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(
        human,
        agent_id="metadata-only-peer",
        connection_id="ccswitch-one",
        route_id="ccswitch-one-model-a",
        backend="cc-switch",
    )
    human.send_message(
        {
            "recipient": "metadata-only-peer",
            "task_id": "metadata-only",
            "subject": "must remain queued",
            "body": "hello supervisor",
            "route_profile_id": "ccswitch-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=SuccessfulRunner,
    )
    discovered = discover_runnable_routes(
        supervisor._control, credential_probe=lambda _route: True
    )
    assert len(discovered) == 1
    assert discovered[0].secret_backend == "cc-switch"
    assert discovered[0].credential_target == "ccswitch-reference"
    result = supervisor.run_cycle()
    assert result == result.__class__(0, 1, 0, 0, 1)
    with sqlite3.connect(human.db_path) as connection:
        dispatch = connection.execute(
            "SELECT status, attempt_count, error_code FROM message_dispatches"
        ).fetchone()
    assert dispatch == ("failed", 1, "credential_unavailable")
    supervisor.close()


def test_legacy_ccswitch_connection_without_provider_id_keeps_exact_route_binding(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(
        human,
        agent_id="legacy-peer",
        connection_id="ccswitch-legacy",
        route_id="ccswitch-legacy-model-a",
        backend="cc-switch",
    )
    with sqlite3.connect(human.db_path) as connection:
        connection.execute(
            "UPDATE provider_connections SET provider_id=NULL "
            "WHERE scope='test' AND connection_id='ccswitch-legacy'"
        )

    discovered = discover_runnable_routes(human)

    assert len(discovered) == 1
    assert discovered[0].connection_id == "ccswitch-legacy"
    assert discovered[0].provider_id == "ccswitch-legacy"
    assert discovered[0].route_id == "ccswitch-legacy-model-a"


def test_idle_supervisor_does_not_probe_saved_provider_routes(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    probes: list[str] = []
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=SuccessfulRunner,
        credential_probe=lambda route: probes.append(route.route_id) or True,
    )

    result = supervisor.run_cycle()

    assert result.runnable_routes == 0
    assert result.claimed == 0
    assert probes == []
    supervisor.close()


def test_supervisor_claims_identity_bound_message_without_route_profile(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "identity-bound",
            "subject": "Legacy direct route request",
            "body": "hello supervisor",
            "requested_provider_id": "relay-one",
            "requested_model_id": "model-a",
            "requested_reasoning_mode": "high",
            "requested_route_class": "relay",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=SuccessfulRunner,
        credential_probe=lambda _route: True,
    )

    result = supervisor.run_cycle()

    assert (result.runnable_routes, result.claimed, result.completed) == (1, 1, 1)
    with sqlite3.connect(human.db_path) as connection:
        dispatch = connection.execute(
            "SELECT status, attempt_count FROM message_dispatches WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()
    assert dispatch == ("completed", 1)
    supervisor.close()


class RouteEchoRunner:
    seen: list[tuple[str, str, str]] = []

    def __init__(self, config) -> None:
        self.config = config

    def run(self, messages, *, message_id=None) -> InferenceResult:
        self.__class__.seen.append(
            (self.config.connection_id, self.config.model, str(message_id))
        )
        return InferenceResult(
            assistant_message={
                "role": "assistant",
                "content": f"{self.config.connection_id}:{self.config.model}",
            },
            receipt={"receipt_sha256": HEX_B},
        )


def test_supervisor_keeps_same_agent_routes_exact_and_non_overlapping(
    tmp_path: Path,
) -> None:
    RouteEchoRunner.seen = []
    human = make_bridge(tmp_path, "human-operator")
    register_route(
        human,
        connection_id="relay-one",
        route_id="relay-one-model-a",
        model_id="model-a",
    )
    register_route(
        human,
        connection_id="relay-two",
        route_id="relay-two-model-b",
        model_id="model-b",
    )
    first = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "route-a",
            "subject": "Use A",
            "body": "First exact route.",
            "route_profile_id": "relay-one-model-a",
        }
    )
    second = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "route-b",
            "subject": "Use B",
            "body": "Second exact route.",
            "route_profile_id": "relay-two-model-b",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=RouteEchoRunner,
        credential_probe=lambda _route: True,
    )

    result = supervisor.run_cycle()

    assert (result.runnable_routes, result.claimed, result.completed) == (2, 2, 2)
    assert sorted(
        (connection, model) for connection, model, _ in RouteEchoRunner.seen
    ) == [
        ("relay-one", "model-a"),
        ("relay-two", "model-b"),
    ]
    with sqlite3.connect(human.db_path) as connection:
        replies = connection.execute(
            "SELECT reply_to, body FROM messages WHERE sender='model-peer' ORDER BY sequence"
        ).fetchall()
    assert dict(replies) == {
        first["message_id"]: "relay-one:model-a",
        second["message_id"]: "relay-two:model-b",
    }
    supervisor.close()


class ProfileEchoRunner:
    seen: list[tuple[str | None, str]] = []

    def __init__(self, config) -> None:
        self.config = config

    def run(self, _messages, *, message_id=None) -> InferenceResult:
        self.__class__.seen.append((self.config.route_profile_id, str(message_id)))
        return InferenceResult(
            assistant_message={
                "role": "assistant",
                "content": str(self.config.route_profile_id),
            },
            receipt={"receipt_sha256": HEX_B},
        )


def test_supervisor_claims_exact_message_and_profile_for_identical_routes(
    tmp_path: Path,
) -> None:
    ProfileEchoRunner.seen = []
    human = make_bridge(tmp_path, "human-operator")
    register_route(human, route_id="profile-a")
    register_route(human, route_id="profile-b")
    profile_b = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "profile-b-task",
            "subject": "Use profile B",
            "body": "Profile B must remain exact.",
            "route_profile_id": "profile-b",
        }
    )
    ambiguous = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "ambiguous-task",
            "subject": "Ambiguous direct route",
            "body": "Do not guess a saved profile.",
            "requested_provider_id": "relay-one",
            "requested_model_id": "model-a",
            "requested_reasoning_mode": "high",
            "requested_route_class": "relay",
        }
    )
    profile_a = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "profile-a-task",
            "subject": "Use profile A",
            "body": "Profile A must remain exact.",
            "route_profile_id": "profile-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=ProfileEchoRunner,
        credential_probe=lambda _route: True,
    )

    result = supervisor.run_cycle()

    assert (result.runnable_routes, result.claimed, result.completed) == (2, 2, 2)
    assert sorted(ProfileEchoRunner.seen) == [
        ("profile-a", profile_a["message_id"]),
        ("profile-b", profile_b["message_id"]),
    ]
    with sqlite3.connect(human.db_path) as connection:
        replies = connection.execute(
            "SELECT reply_to, body FROM messages WHERE reply_to IS NOT NULL"
        ).fetchall()
        ambiguous_dispatches = connection.execute(
            "SELECT COUNT(*) FROM message_dispatches WHERE message_id=?",
            (ambiguous["message_id"],),
        ).fetchone()[0]
    assert dict(replies) == {
        profile_a["message_id"]: "profile-a",
        profile_b["message_id"]: "profile-b",
    }
    assert ambiguous_dispatches == 0
    supervisor.close()


def test_supervisor_completes_room_fanout_once_per_agent(tmp_path: Path) -> None:
    SuccessfulRunner.configs = []
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "team", "name": "Team"})
    register_route(
        human,
        agent_id="grok-peer",
        connection_id="relay-grok",
        route_id="grok-route",
        model_id="grok-4.6",
    )
    register_route(
        human,
        agent_id="kimi-peer",
        connection_id="relay-kimi",
        route_id="kimi-route",
        model_id="kimi-for-coding",
    )
    human.join_room(
        {
            "room_id": "team",
            "agent_id": "grok-peer",
            "route_profile_id": "grok-route",
        }
    )
    human.join_room(
        {
            "room_id": "team",
            "agent_id": "kimi-peer",
            "route_profile_id": "kimi-route",
        }
    )
    sent = human.send_room_fanout(
        {
            "room_id": "team",
            "task_id": "fanout-e2e",
            "subject": "Reply once each",
            "body": "hello supervisor",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=SuccessfulRunner,
        credential_probe=lambda _route: True,
    )

    first = supervisor.run_cycle()
    second = supervisor.run_cycle()

    assert sent["fanout_count"] == 2
    assert (first.runnable_routes, first.claimed, first.completed) == (2, 2, 2)
    assert (second.claimed, second.completed) == (0, 0)
    with sqlite3.connect(human.db_path) as connection:
        replies = connection.execute(
            "SELECT sender, reply_to FROM messages WHERE reply_to IS NOT NULL "
            "ORDER BY sender"
        ).fetchall()
        dispatches = connection.execute(
            "SELECT agent_id, status, attempt_count FROM message_dispatches "
            "ORDER BY agent_id"
        ).fetchall()
    assert [row[0] for row in replies] == ["grok-peer", "kimi-peer"]
    assert dispatches == [
        ("grok-peer", "completed", 1),
        ("kimi-peer", "completed", 1),
    ]
    supervisor.close()


def test_supervisor_process_lock_precedes_bridge_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    real_bridge = supervisor_module.Bridge
    constructions: list[str] = []

    def tracked_bridge(*args, **kwargs):
        constructions.append(str(args[2]))
        return real_bridge(*args, **kwargs)

    monkeypatch.setattr(supervisor_module, "Bridge", tracked_bridge)
    supervisor = MailboxSupervisor(tmp_path, human.db_path, "test")
    try:
        with pytest.raises(
            SupervisorAlreadyRunningError,
            match="already owns this scope",
        ):
            MailboxSupervisor(tmp_path, human.db_path, "test")
        assert constructions == ["mailbox-supervisor"]
    finally:
        supervisor.close()

    with pytest.raises(SupervisorError, match="is closed"):
        supervisor.run_once()
    replacement = MailboxSupervisor(tmp_path, human.db_path, "test")
    assert constructions == ["mailbox-supervisor", "mailbox-supervisor"]
    replacement.close()


class CountingRunner:
    constructions = 0
    attempts = 0

    def __init__(self, _config) -> None:
        self.__class__.constructions += 1

    def run(self, _messages, *, message_id=None) -> InferenceResult:
        self.__class__.attempts += 1
        return InferenceResult(
            assistant_message={"role": "assistant", "content": "bounded reply"},
            receipt={"receipt_sha256": HEX_B},
        )


def test_runtime_capacity_miss_does_not_claim_or_attempt_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    CountingRunner.constructions = 0
    CountingRunner.attempts = 0
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "capacity-miss",
            "subject": "Wait for local capacity",
            "body": "Do not consume an attempt.",
            "route_profile_id": "relay-one-model-a",
        }
    )

    @contextmanager
    def unavailable():
        raise ResourceUnavailableError("synthetic local capacity miss")
        yield

    monkeypatch.setattr(supervisor_module, "provider_runtime_admission", unavailable)
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=CountingRunner,
        credential_probe=lambda _route: True,
    )

    result = supervisor.run_cycle()

    assert (result.runnable_routes, result.claimed, result.completed) == (1, 0, 0)
    assert (CountingRunner.constructions, CountingRunner.attempts) == (0, 0)
    with sqlite3.connect(human.db_path) as connection:
        dispatch_count = connection.execute(
            "SELECT COUNT(*) FROM message_dispatches WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()[0]
    assert dispatch_count == 0
    supervisor.close()


def test_supervisor_fails_closed_on_claim_handoff_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    CountingRunner.constructions = 0
    CountingRunner.attempts = 0
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "handoff-drift",
            "subject": "Verify handoff",
            "body": "Do not run after route drift.",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=CountingRunner,
        credential_probe=lambda _route: True,
    )
    route = discover_runnable_routes(supervisor._control)[0]
    bridge = supervisor._bridge_for(route)
    real_claim = bridge.claim_message_dispatch

    def drifted_claim(args):
        claim = real_claim(args)
        if claim["claimed"]:
            claim["message"]["route_profile_id"] = "different-profile"
        return claim

    monkeypatch.setattr(bridge, "claim_message_dispatch", drifted_claim)

    result = supervisor.run_cycle()

    assert (result.claimed, result.completed, result.terminal_failures) == (1, 0, 1)
    assert (CountingRunner.constructions, CountingRunner.attempts) == (0, 0)
    with sqlite3.connect(human.db_path) as connection:
        dispatch = connection.execute(
            "SELECT status, attempt_count, error_code FROM message_dispatches "
            "WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()
    assert dispatch == ("failed", 1, "route_handoff_mismatch")
    supervisor.close()


def test_parallel_limit_is_applied_before_claiming(tmp_path: Path) -> None:
    CountingRunner.constructions = 0
    CountingRunner.attempts = 0
    human = make_bridge(tmp_path, "human-operator")
    message_ids: list[str] = []
    for index in range(3):
        agent_id = f"peer-{index}"
        route_id = f"route-{index}"
        register_route(
            human,
            agent_id=agent_id,
            connection_id=f"relay-{index}",
            route_id=route_id,
            model_id=f"model-{index}",
        )
        sent = human.send_message(
            {
                "recipient": agent_id,
                "task_id": f"bounded-{index}",
                "subject": "Bound claims",
                "body": "One provider at a time.",
                "route_profile_id": route_id,
            }
        )
        message_ids.append(sent["message_id"])
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=CountingRunner,
        credential_probe=lambda _route: True,
        max_parallel_dispatches=1,
    )

    result = supervisor.run_cycle()

    assert (result.runnable_routes, result.claimed, result.completed) == (3, 1, 1)
    assert (CountingRunner.constructions, CountingRunner.attempts) == (1, 1)
    with sqlite3.connect(human.db_path) as connection:
        dispatches = connection.execute(
            "SELECT message_id, attempt_count FROM message_dispatches"
        ).fetchall()
    assert dispatches == [(message_ids[0], 1)]
    supervisor.close()


def test_supervisor_calls_one_atomic_reconciliation_hook_per_cycle(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    calls: list[tuple[str, dict[str, int]]] = []

    def reconcile(control: Bridge, args):
        calls.append((control.agent_id, dict(args)))
        return {"reconciled": 0}

    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        reconciliation_hook=reconcile,
        max_attempts=7,
    )

    result = supervisor.run_cycle()

    assert result.claimed == 0
    assert calls == [("mailbox-supervisor", {"max_attempts": 7, "limit": 500})]
    supervisor.close()


def test_run_forever_recovers_with_bounded_backoff_and_retains_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        cycle_error_backoff_base_seconds=0.5,
        cycle_error_backoff_cap_seconds=1.0,
    )
    cycles = 0
    sleeps: list[float] = []
    duplicate_rejected: list[bool] = []

    def failing_cycle():
        nonlocal cycles
        cycles += 1
        if cycles <= 3:
            raise RuntimeError(f"synthetic cycle failure {cycles}")
        raise KeyboardInterrupt

    def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        try:
            duplicate = MailboxSupervisor(tmp_path, human.db_path, "test")
        except SupervisorAlreadyRunningError:
            duplicate_rejected.append(True)
        else:
            duplicate_rejected.append(False)
            duplicate.close()

    monkeypatch.setattr(supervisor, "run_cycle", failing_cycle)
    monkeypatch.setattr(supervisor_module.time, "sleep", record_sleep)

    with pytest.raises(KeyboardInterrupt):
        supervisor.run_forever(poll_seconds=0.25)

    assert cycles == 4
    assert sleeps == [0.5, 1.0, 1.0]
    assert duplicate_rejected == [True, True, True]
    replacement = MailboxSupervisor(tmp_path, human.db_path, "test")
    replacement.close()


class EmptyResponseRunner:
    def __init__(self, _config) -> None:
        pass

    def run(self, _messages, *, message_id=None) -> InferenceResult:
        return InferenceResult(
            assistant_message={"role": "assistant", "content": "   "},
            receipt={"receipt_sha256": HEX_B},
        )


def test_supervisor_fails_empty_response_once_without_creating_a_reply(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "empty-response",
            "subject": "No empty reply",
            "body": "A blank model response must fail closed.",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=EmptyResponseRunner,
        credential_probe=lambda _route: True,
    )

    first = supervisor.run_cycle()
    second = supervisor.run_cycle()

    assert (first.claimed, first.terminal_failures) == (1, 1)
    assert (second.claimed, second.completed) == (0, 0)
    with sqlite3.connect(human.db_path) as connection:
        dispatch = connection.execute(
            "SELECT status, attempt_count, error_code FROM message_dispatches "
            "WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()
        replies = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE reply_to=?",
            (sent["message_id"],),
        ).fetchone()[0]
    assert dispatch == ("failed", 1, "inference_failed")
    assert replies == 0
    supervisor.close()


class SlowSuccessfulRunner(SuccessfulRunner):
    def run(self, messages, *, message_id=None) -> InferenceResult:
        time.sleep(0.18)
        return super().run(messages, message_id=message_id)


def test_supervisor_renews_long_inference_lease(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "slow-inference",
            "subject": "renew while running",
            "body": "hello supervisor",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=SlowSuccessfulRunner,
        credential_probe=lambda _route: True,
        lease_seconds=30,
        lease_renew_interval_seconds=0.05,
    )

    result = supervisor.run_cycle()

    assert (result.claimed, result.completed, result.terminal_failures) == (1, 1, 0)
    with sqlite3.connect(human.db_path) as connection:
        renewals = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='message.dispatch_renewed'"
        ).fetchone()[0]
        state = connection.execute(
            "SELECT status FROM message_dispatches WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()[0]
    assert renewals >= 2
    assert state == "completed"
    supervisor.close()


def test_supervisor_contains_unexpected_worker_future_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "worker-crash",
            "subject": "contain worker failure",
            "body": "hello supervisor",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=SuccessfulRunner,
        credential_probe=lambda _route: True,
    )

    def crash_before_worker_guard(_job):
        raise RuntimeError("synthetic future failure")

    monkeypatch.setattr(supervisor, "_execute_claimed", crash_before_worker_guard)
    result = supervisor.run_cycle()

    assert (result.claimed, result.completed, result.terminal_failures) == (1, 0, 1)
    with sqlite3.connect(human.db_path) as connection:
        state = connection.execute(
            "SELECT status, error_code FROM message_dispatches WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()
    assert state == ("failed", "unexpected_worker_failure")
    supervisor.close()


class ConsensusRunner:
    barrier: threading.Barrier | None = None

    def __init__(self, config) -> None:
        self.config = config

    def run(self, messages, *, message_id=None) -> InferenceResult:
        assert message_id
        assert "bounded parallel discussion round" in messages[0]["content"]
        if self.__class__.barrier is not None:
            self.__class__.barrier.wait(timeout=2.0)
        return InferenceResult(
            assistant_message={
                "role": "assistant",
                "content": (
                    f"{self.config.agent_id} accepts the bounded proposal.\n"
                    "PEERBRIDGE_SIGNAL: CONSENSUS"
                ),
            },
            receipt={"receipt_sha256": HEX_B},
        )


def test_supervisor_completes_parallel_discussion_and_stops_on_consensus(
    tmp_path: Path,
) -> None:
    ConsensusRunner.barrier = threading.Barrier(2)
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "consensus-room", "name": "Consensus Room"})
    for index, agent_id in enumerate(("peer-a", "peer-b"), start=1):
        register_route(
            human,
            agent_id=agent_id,
            connection_id=f"relay-{index}",
            route_id=f"route-{index}",
            model_id=f"model-{index}",
        )
        human.join_room(
            {
                "room_id": "consensus-room",
                "agent_id": agent_id,
                "route_profile_id": f"route-{index}",
            }
        )
    human.set_room_automation(
        {
            "room_id": "consensus-room",
            "mode": "discussion",
            "max_rounds": 4,
            "max_messages": 40,
            "stagnation_rounds": 2,
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "consensus-room",
            "task_id": "bounded-consensus",
            "subject": "Reach consensus",
            "body": "Review this proposal in parallel and stop when both peers agree.",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=ConsensusRunner,
        credential_probe=lambda _route: True,
    )

    first = supervisor.run_cycle()
    second = supervisor.run_cycle()

    assert posted["fanout_count"] == 2
    assert (first.claimed, first.completed, first.discussions_advanced) == (2, 2, 1)
    assert (second.claimed, second.completed, second.discussions_advanced) == (0, 0, 0)
    policy = human.get_room_automation({"room_id": "consensus-room"})
    assert policy["active_discussion"] is None
    with sqlite3.connect(human.db_path) as connection:
        discussion = connection.execute(
            "SELECT status, current_round, processed_round, stop_reason "
            "FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
        prompts = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE discussion_id=? "
            "AND discussion_role='prompt'",
            (posted["discussion_id"],),
        ).fetchone()[0]
    assert discussion == ("completed", 1, 1, "consensus")
    assert prompts == 2
    supervisor.close()


def test_unavailable_discussion_seat_fails_closed_instead_of_hanging(
    tmp_path: Path,
) -> None:
    ConsensusRunner.barrier = None
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "offline-room", "name": "Offline Room"})
    for index, agent_id in enumerate(("online-peer", "offline-peer"), start=1):
        register_route(
            human,
            agent_id=agent_id,
            connection_id=f"relay-{index}",
            route_id=f"route-{index}",
            model_id=f"model-{index}",
        )
        human.join_room(
            {
                "room_id": "offline-room",
                "agent_id": agent_id,
                "route_profile_id": f"route-{index}",
            }
        )
    human.set_room_automation(
        {
            "room_id": "offline-room",
            "mode": "discussion",
            "max_rounds": 4,
            "max_messages": 40,
            "stagnation_rounds": 2,
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "offline-room",
            "task_id": "offline-seat",
            "subject": "Do not hang",
            "body": "One configured peer is unavailable.",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=ConsensusRunner,
        credential_probe=lambda route: route.agent_id == "online-peer",
    )

    first = supervisor.run_cycle()
    second = supervisor.run_cycle()

    assert posted["fanout_count"] == 2
    assert first == first.__class__(1, 2, 1, 0, 1, 1)
    assert (second.claimed, second.discussions_advanced) == (0, 0)
    automation = human.get_room_automation({"room_id": "offline-room"})
    assert automation["active_discussion"]["status"] == "waiting_human"
    assert automation["active_discussion"]["stop_reason"] == "agent_dispatch_failed"
    with sqlite3.connect(human.db_path) as connection:
        discussion = connection.execute(
            "SELECT status, stop_reason FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
        dispatches = connection.execute(
            "SELECT agent_id, status, error_code FROM message_dispatches "
            "ORDER BY agent_id"
        ).fetchall()
    assert discussion == ("waiting_human", "agent_dispatch_failed")
    assert dispatches == [
        ("offline-peer", "failed", "credential_unavailable"),
        ("online-peer", "completed", None),
    ]
    supervisor.close()
    ConsensusRunner.barrier = None
