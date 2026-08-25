from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from peerbridge_mcp import mailbox_supervisor as supervisor_module
from peerbridge_mcp.attachments import stage_chat_attachment_payloads
from peerbridge_mcp.bridge import Bridge, stable_sha256
from peerbridge_mcp.credentials import credential_target
from peerbridge_mcp.mailbox_supervisor import (
    MailboxSupervisor,
    SupervisorAlreadyRunningError,
    SupervisorError,
    discover_runnable_routes,
)
from peerbridge_mcp.openai_compatible_runner import (
    InferenceResult,
    ProviderHTTPError,
    ResourceUnavailableError,
)
from peerbridge_mcp.multimodal import VERIFIED_ATTACHMENT_MESSAGE_KEY
from tests._image_fixtures import PNG


HEX_A = "a" * 64
HEX_B = "b" * 64


def successful_inference_result(
    config,
    *,
    message_id: str,
    content: str,
    usage: dict | None = None,
) -> InferenceResult:
    assert config.db_path is not None
    with sqlite3.connect(config.db_path) as connection:
        connection.row_factory = sqlite3.Row
        provider_connection = connection.execute(
            """SELECT * FROM provider_connections
               WHERE scope=? AND connection_id=? AND enabled=1""",
            (config.scope, config.connection_id),
        ).fetchone()
    assert provider_connection is not None
    assistant_message = {"role": "assistant", "content": content}
    secret_backend = str(provider_connection["secret_backend"])
    if secret_backend == "native-acp":
        receipt = {
            "schema": "peerbridge.acpx-inference-receipt.v1",
            "secret_backend": "native-acp",
            "route_profile_id": config.route_profile_id,
            "route_profile_sha256": config.route_profile_sha256,
            "route_class": config.route_class,
            "requested_provider_id": config.provider_id,
            "requested_model": config.model,
            "requested_reasoning_mode": config.reasoning_mode,
            "connection_id": config.connection_id,
            "message_id": message_id,
            "response_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    elif secret_backend == "cc-switch":
        receipt = {
            "schema": "peerbridge.ccswitch-inference-receipt.v1",
            "secret_backend": "cc-switch",
            "route_profile_id": config.route_profile_id,
            "route_profile_sha256": config.route_profile_sha256,
            "route_class": config.route_class,
            "requested_model": config.model,
            "connection_id": config.connection_id,
            "message_id": message_id,
            "response_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    else:
        receipt = {
            "schema": "peerbridge.openai-compatible-run.v1",
            "route": {
                "route_profile_id": config.route_profile_id,
                "route_profile_sha256": config.route_profile_sha256,
                "route_class": config.route_class,
                "provider_id": config.provider_id,
                "model_id": config.model,
                "response_model_id": config.response_model or config.model,
                "reasoning_mode": config.reasoning_mode,
                "connection_id": config.connection_id,
                "connection_sha256": provider_connection["connection_sha256"],
            },
            "room_id": config.room_id,
            "session_id": config.session_id,
            "message_id_sha256": stable_sha256(message_id),
            "output_message_sha256": stable_sha256(assistant_message),
        }
    if usage is not None:
        receipt["usage"] = usage
    receipt["receipt_sha256"] = stable_sha256(receipt)
    return InferenceResult(assistant_message=assistant_message, receipt=receipt)


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    (
        (401, "provider_authentication_required", False),
        (402, "provider_billing_required", False),
        (403, "provider_access_denied", False),
        (429, "provider_rate_limited_terminal", False),
        (503, "provider_http_retryable", True),
    ),
)
def test_provider_http_failure_codes_remain_actionable(
    status: int, expected: str, retryable: bool
) -> None:
    failure = ProviderHTTPError(
        "sanitized provider failure",
        status_code=status,
        retryable=retryable,
    )
    assert supervisor_module._failure_policy(failure) == (expected, retryable)


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
    inference_timeout_seconds: int | None = None,
) -> None:
    if backend == "windows-credential-manager":
        target = credential_target("test", connection_id)
        provider_id = connection_id
        route_class = "relay"
        client_name = "test-client"
    elif backend == "cc-switch":
        target = "ccswitch-reference"
        provider_id = connection_id
        route_class = "relay"
        client_name = "test-client"
    elif backend == "native-acp":
        target = "ACPX:codex"
        provider_id = "openai-official"
        route_class = "official"
        client_name = "codex"
    else:
        raise AssertionError(f"unsupported test backend: {backend}")
    bridge.upsert_provider_connection(
        {
            "connection_id": connection_id,
            "display_name": connection_id,
            "route_class": route_class,
            "provider_id": provider_id,
            "secret_backend": backend,
            "credential_target": target,
            "endpoint_sha256": HEX_A,
            "credential_fingerprint_sha256": HEX_B,
            "descriptor_schema": "peerbridge-provider-v2",
            "credential_version_sha256": HEX_A,
            "enabled": True,
        }
    )
    profile = {
        "route_id": route_id,
        "agent_id": agent_id,
        "client_name": client_name,
        "provider_id": provider_id,
        "model_id": model_id,
        "reasoning_mode": "high",
        "route_class": route_class,
        "enabled": True,
    }
    if inference_timeout_seconds is not None:
        profile["inference_timeout_seconds"] = inference_timeout_seconds
    bridge.upsert_route_profile(profile)


class SuccessfulRunner:
    configs = []

    def __init__(self, config) -> None:
        self.config = config
        self.__class__.configs.append(config)

    def run(self, messages, *, message_id=None) -> InferenceResult:
        assert messages[0]["role"] == "system"
        assert "hello supervisor" in messages[1]["content"]
        assert message_id
        return successful_inference_result(
            self.config,
            message_id=message_id,
            content="audited reply",
            usage={
                "schema": "peerbridge.inference-usage.v1",
                "status": "reported",
                "source": "test-runner",
                "input_tokens": 21,
                "output_tokens": 8,
                "total_tokens": 29,
                "cached_input_tokens": 5,
                "reasoning_tokens": 3,
                "reported_calls": 1,
                "total_calls": 1,
                "total_tokens_derived": False,
            },
        )


class AttachmentCapturingRunner:
    messages: list[list[dict[str, object]]] = []

    def __init__(self, config) -> None:
        self.config = config

    def run(self, messages, *, message_id=None) -> InferenceResult:
        assert message_id
        self.__class__.messages.append(messages)
        return successful_inference_result(
            self.config,
            message_id=message_id,
            content="attachment reviewed",
        )


class ContextCapturingRunner:
    messages_by_agent: dict[str, list[list[dict[str, object]]]] = {}
    lock = threading.Lock()

    def __init__(self, config) -> None:
        self.config = config

    def run(self, messages, *, message_id=None) -> InferenceResult:
        assert message_id
        with self.__class__.lock:
            self.__class__.messages_by_agent.setdefault(
                self.config.agent_id, []
            ).append(messages)
        return successful_inference_result(
            self.config,
            message_id=message_id,
            content=f"context reply from {self.config.agent_id}",
        )


class WrongReceiptSchemaRunner:
    def __init__(self, config) -> None:
        self.config = config

    def run(self, messages, *, message_id=None) -> InferenceResult:
        assert message_id
        assert self.config.db_path is not None
        with sqlite3.connect(self.config.db_path) as connection:
            connection.row_factory = sqlite3.Row
            provider_connection = connection.execute(
                """SELECT * FROM provider_connections
                   WHERE scope=? AND connection_id=? AND enabled=1""",
                (self.config.scope, self.config.connection_id),
            ).fetchone()
        assert provider_connection is not None
        assistant_message = {"role": "assistant", "content": "forged schema reply"}
        receipt = {
            "schema": "peerbridge.openai-compatible-run.v1",
            "route": {
                "route_profile_id": self.config.route_profile_id,
                "route_profile_sha256": self.config.route_profile_sha256,
                "route_class": self.config.route_class,
                "provider_id": self.config.provider_id,
                "model_id": self.config.model,
                "response_model_id": self.config.response_model or self.config.model,
                "reasoning_mode": self.config.reasoning_mode,
                "connection_id": self.config.connection_id,
                "connection_sha256": provider_connection["connection_sha256"],
            },
            "room_id": self.config.room_id,
            "session_id": self.config.session_id,
            "message_id_sha256": stable_sha256(message_id),
            "output_message_sha256": stable_sha256(assistant_message),
        }
        receipt["receipt_sha256"] = stable_sha256(receipt)
        return InferenceResult(assistant_message=assistant_message, receipt=receipt)


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
    assert SuccessfulRunner.configs[-1].timeout_seconds == 60.0
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
        usage = connection.execute(
            "SELECT usage_status, input_tokens, output_tokens, total_tokens, "
            "cached_input_tokens, reasoning_tokens, inference_receipt_sha256 "
            "FROM inference_usage WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()
    assert replies == [("audited reply", sent["message_id"])]
    assert dispatch[:2] == ("completed", 1)
    assert len(dispatch[2]) == 64
    assert usage[:6] == ("reported", 21, 8, 29, 5, 3)
    assert usage[6] == dispatch[2]
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


def test_ambiguous_resource_failure_is_terminal_and_not_replayed(
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
    repeated = supervisor.run_cycle()
    assert (failed.terminal_failures, failed.completed) == (1, 0)
    assert (repeated.claimed, repeated.completed) == (0, 0)
    with sqlite3.connect(human.db_path) as connection:
        state = connection.execute(
            "SELECT status, attempt_count FROM message_dispatches"
        ).fetchone()
        schedule = connection.execute(
            "SELECT attempt_count, not_before_epoch, error_code "
            "FROM message_dispatch_retry_schedules"
        ).fetchone()
    assert state == ("failed", 1)
    assert schedule is None
    supervisor.close()
    assert RetryThenSucceedRunner.attempts == 1


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


def test_native_acp_route_is_exactly_bound_and_uses_acpx_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from peerbridge_mcp.acpx_runner import AcpxRunner

    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    monkeypatch.setattr(
        "peerbridge_mcp.acpx_runner.native_acp_runtime_available",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        "peerbridge_mcp.acpx_runner.find_acpx",
        lambda: executable,
    )
    human = make_bridge(tmp_path, "human-operator")
    register_route(
        human,
        agent_id="codex-native",
        connection_id="acpx-codex",
        route_id="acpx-codex-luna",
        model_id="gpt-5.6-luna",
        backend="native-acp",
    )

    routes = discover_runnable_routes(human, credential_probe=lambda _route: True)

    assert len(routes) == 1
    route = routes[0]
    assert route.secret_backend == "native-acp"
    assert route.credential_target == "ACPX:codex"
    assert route.client_name == "codex"
    assert route.provider_id == "openai-official"
    assert route.route_class == "official"
    supervisor = MailboxSupervisor(tmp_path, human.db_path, "test")
    assert supervisor._credential_available(route) is True
    bridge = supervisor._bridge_for(route)
    runner = supervisor._runner_for(
        route, supervisor._runner_config(route, bridge, "lobby")
    )
    assert isinstance(runner, AcpxRunner)
    assert runner.config.timeout_seconds == 180.0
    supervisor.close()


def test_native_acp_route_rejects_openai_compatible_receipt_schema(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(
        human,
        agent_id="codex-native",
        connection_id="acpx-codex",
        route_id="acpx-codex-luna",
        model_id="gpt-5.6-luna",
        backend="native-acp",
    )
    sent = human.send_message(
        {
            "recipient": "codex-native",
            "task_id": "wrong-receipt-schema",
            "subject": "Reject backend schema mismatch",
            "body": "hello supervisor",
            "route_profile_id": "acpx-codex-luna",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=WrongReceiptSchemaRunner,
        credential_probe=lambda _route: True,
        max_attempts=1,
    )

    result = supervisor.run_cycle()

    assert (result.claimed, result.completed, result.terminal_failures) == (1, 0, 1)
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
        trusted_receipts = connection.execute(
            "SELECT COUNT(*) FROM trusted_inference_receipts WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()[0]
    assert dispatch == ("failed", 1, "unexpected_runtime_failure")
    assert replies == 0
    assert trusted_receipts == 0
    supervisor.close()


def test_explicit_route_timeout_overrides_the_relay_default(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(human, inference_timeout_seconds=180)

    routes = discover_runnable_routes(human, credential_probe=lambda _route: True)

    assert len(routes) == 1
    route = routes[0]
    assert route.inference_timeout_seconds == 180
    supervisor = MailboxSupervisor(tmp_path, human.db_path, "test")
    bridge = supervisor._bridge_for(route)
    config = supervisor._runner_config(route, bridge, "lobby")
    assert config.timeout_seconds == 180.0
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
        return successful_inference_result(
            self.config,
            message_id=str(message_id),
            content=f"{self.config.connection_id}:{self.config.model}",
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

    assert (
        result.runnable_routes,
        result.claimed,
        result.completed,
        result.terminal_failures,
    ) == (2, 2, 2, 0)
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
        return successful_inference_result(
            self.config,
            message_id=str(message_id),
            content=str(self.config.route_profile_id),
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

    assert (
        result.runnable_routes,
        result.claimed,
        result.completed,
        result.terminal_failures,
    ) == (2, 2, 2, 1)
    assert sorted(ProfileEchoRunner.seen) == [
        ("profile-a", profile_a["message_id"]),
        ("profile-b", profile_b["message_id"]),
    ]
    with sqlite3.connect(human.db_path) as connection:
        replies = connection.execute(
            "SELECT reply_to, body FROM messages WHERE reply_to IS NOT NULL"
        ).fetchall()
        ambiguous_dispatch = connection.execute(
            "SELECT status, error_code FROM message_dispatches WHERE message_id=?",
            (ambiguous["message_id"],),
        ).fetchone()
    assert dict(replies) == {
        profile_a["message_id"]: "profile-a",
        profile_b["message_id"]: "profile-b",
    }
    assert ambiguous_dispatch == ("failed", "route_runtime_ambiguous")
    supervisor.close()


def test_supervisor_terminally_records_missing_runtime_instead_of_hanging(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "missing-runtime",
            "subject": "Do not hang",
            "body": "A disabled provider connection must become visible terminal evidence.",
            "route_profile_id": "relay-one-model-a",
        }
    )
    with sqlite3.connect(human.db_path) as connection:
        connection.execute(
            "UPDATE provider_connections SET enabled=0 WHERE connection_id='relay-one'"
        )
    supervisor = MailboxSupervisor(tmp_path, human.db_path, "test")

    result = supervisor.run_cycle()

    assert (result.claimed, result.completed, result.terminal_failures) == (0, 0, 1)
    with sqlite3.connect(human.db_path) as connection:
        dispatch = connection.execute(
            "SELECT status, error_code FROM message_dispatches WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()
    assert dispatch == ("failed", "route_runtime_unavailable")
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


def test_room_context_persists_deduplicates_fanout_and_isolates_rooms(
    tmp_path: Path,
) -> None:
    ContextCapturingRunner.messages_by_agent = {}
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "team", "name": "Team"})
    for agent_id, connection_id, route_id, model_id in (
        ("grok-peer", "relay-grok", "grok-route", "grok-4.6"),
        ("kimi-peer", "relay-kimi", "kimi-route", "kimi-for-coding"),
    ):
        register_route(
            human,
            agent_id=agent_id,
            connection_id=connection_id,
            route_id=route_id,
            model_id=model_id,
        )
        human.join_room(
            {
                "room_id": "team",
                "agent_id": agent_id,
                "route_profile_id": route_id,
            }
        )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=ContextCapturingRunner,
        credential_probe=lambda _route: True,
    )

    for body in ("first room question", "second room question"):
        human.send_room_fanout(
            {
                "room_id": "team",
                "task_id": f"context-{body.split()[0]}",
                "subject": "Remember this room",
                "body": body,
            }
        )
        result = supervisor.run_cycle()
        assert (result.claimed, result.completed) == (2, 2)

    for agent_id in ("grok-peer", "kimi-peer"):
        first_prompt, second_prompt = ContextCapturingRunner.messages_by_agent[
            agent_id
        ]
        assert len(first_prompt) == 2
        assert len(second_prompt) == 5
        rendered = "\n".join(str(item["content"]) for item in second_prompt)
        assert rendered.count("first room question") == 1
        assert rendered.count("context reply from grok-peer") == 1
        assert rendered.count("context reply from kimi-peer") == 1
        assert "count=3" in str(second_prompt[0]["content"])
        assert "second room question" in str(second_prompt[-1]["content"])

    human.create_room({"room_id": "other", "name": "Other"})
    human.join_room(
        {
            "room_id": "other",
            "agent_id": "grok-peer",
            "route_profile_id": "grok-route",
        }
    )
    human.send_room_fanout(
        {
            "room_id": "other",
            "task_id": "context-other",
            "subject": "Isolated room",
            "body": "other room question",
        }
    )
    isolated = supervisor.run_cycle()
    assert (isolated.claimed, isolated.completed) == (1, 1)
    isolated_prompt = ContextCapturingRunner.messages_by_agent["grok-peer"][-1]
    assert len(isolated_prompt) == 2
    isolated_text = "\n".join(str(item["content"]) for item in isolated_prompt)
    assert "other room question" in isolated_text
    assert "first room question" not in isolated_text
    assert "second room question" not in isolated_text
    supervisor.close()


def test_direct_dispatch_reply_stays_private_from_other_room_agents(
    tmp_path: Path,
) -> None:
    ContextCapturingRunner.messages_by_agent = {}
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "private-context", "name": "Private Context"})
    for agent_id, connection_id, route_id in (
        ("alpha-peer", "relay-alpha", "alpha-route"),
        ("beta-peer", "relay-beta", "beta-route"),
    ):
        register_route(
            human,
            agent_id=agent_id,
            connection_id=connection_id,
            route_id=route_id,
            model_id=f"{agent_id}-model",
        )
        human.join_room(
            {
                "room_id": "private-context",
                "agent_id": agent_id,
                "route_profile_id": route_id,
            }
        )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=ContextCapturingRunner,
        credential_probe=lambda _route: True,
    )
    human.send_message(
        {
            "room_id": "private-context",
            "recipient": "alpha-peer",
            "route_profile_id": "alpha-route",
            "task_id": "private-alpha",
            "subject": "Private instruction",
            "body": "ALPHA-PRIVATE-ROOT",
        }
    )
    first = supervisor.run_cycle()
    assert (first.claimed, first.completed) == (1, 1)

    beta_root = human.send_message(
        {
            "room_id": "private-context",
            "recipient": "beta-peer",
            "route_profile_id": "beta-route",
            "task_id": "private-beta",
            "subject": "Separate instruction",
            "body": "BETA-PRIVATE-ROOT",
        }
    )
    beta = make_bridge(tmp_path, "beta-peer")
    context = beta.room_prompt_context(beta_root["message_id"])
    serialized = json.dumps(context)
    assert "ALPHA-PRIVATE-ROOT" not in serialized
    assert "context reply from alpha-peer" not in serialized
    assert context["receipt"]["history_message_count"] == 0
    supervisor.close()


def test_room_fanout_delivers_reverified_attachments_to_every_agent(
    tmp_path: Path,
) -> None:
    AttachmentCapturingRunner.messages = []
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "team", "name": "Team"})
    for agent_id, connection_id, route_id, model_id in (
        ("grok-peer", "relay-grok", "grok-route", "grok-4.6"),
        ("kimi-peer", "relay-kimi", "kimi-route", "kimi-for-coding"),
    ):
        register_route(
            human,
            agent_id=agent_id,
            connection_id=connection_id,
            route_id=route_id,
            model_id=model_id,
        )
        human.join_room(
            {
                "room_id": "team",
                "agent_id": agent_id,
                "route_profile_id": route_id,
            }
        )
    staged = stage_chat_attachment_payloads(
        tmp_path,
        (("chart.png", PNG), ("notes.txt", b"Room attachment notes.")),
    )
    human.send_room_fanout(
        {
            "room_id": "team",
            "task_id": "fanout-attachments",
            "subject": "Review attached evidence",
            "body": "hello supervisor",
            "artifact_paths": [item.relative_path for item in staged],
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=AttachmentCapturingRunner,
        credential_probe=lambda _route: True,
    )

    result = supervisor.run_cycle()

    assert (result.claimed, result.completed) == (2, 2)
    assert len(AttachmentCapturingRunner.messages) == 2
    for messages in AttachmentCapturingRunner.messages:
        metadata = messages[1][VERIFIED_ATTACHMENT_MESSAGE_KEY]
        assert isinstance(metadata, list)
        assert [item["kind"] for item in metadata] == ["image", "text"]
        assert all("absolute_path" not in item for item in metadata)
        assert str(tmp_path) not in json.dumps(metadata, sort_keys=True)
    supervisor.close()


def test_room_fanout_fails_closed_when_attachment_changes_before_dispatch(
    tmp_path: Path,
) -> None:
    AttachmentCapturingRunner.messages = []
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "team", "name": "Team"})
    register_route(
        human,
        agent_id="grok-peer",
        connection_id="relay-grok",
        route_id="grok-route",
        model_id="grok-4.6",
    )
    human.join_room(
        {
            "room_id": "team",
            "agent_id": "grok-peer",
            "route_profile_id": "grok-route",
        }
    )
    staged = stage_chat_attachment_payloads(tmp_path, (("chart.png", PNG),))
    human.send_room_fanout(
        {
            "room_id": "team",
            "task_id": "fanout-tamper",
            "subject": "Review attached evidence",
            "body": "hello supervisor",
            "artifact_paths": [staged[0].relative_path],
        }
    )
    target = tmp_path / staged[0].relative_path
    target.write_bytes(PNG[:-1] + bytes([PNG[-1] ^ 1]))
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=AttachmentCapturingRunner,
        credential_probe=lambda _route: True,
    )

    result = supervisor.run_cycle()

    assert (result.claimed, result.completed, result.terminal_failures) == (1, 0, 1)
    assert AttachmentCapturingRunner.messages == []
    with sqlite3.connect(human.db_path) as connection:
        dispatch = connection.execute(
            "SELECT status, error_code FROM message_dispatches",
        ).fetchone()
    assert dispatch == ("failed", "attachment_integrity_failed")
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

    def __init__(self, config) -> None:
        self.config = config
        self.__class__.constructions += 1

    def run(self, _messages, *, message_id=None) -> InferenceResult:
        self.__class__.attempts += 1
        return successful_inference_result(
            self.config,
            message_id=str(message_id),
            content="bounded reply",
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


class NeverReturningRunner(SuccessfulRunner):
    attempts: dict[str, int] = {}
    hang_agents: set[str] = set()
    recover_on_retry = False
    completed_before_release: set[str] = set()
    started = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    @classmethod
    def reset(cls, *hang_agents: str, recover_on_retry: bool = False) -> None:
        cls.attempts = {}
        cls.hang_agents = set(hang_agents)
        cls.recover_on_retry = recover_on_retry
        cls.completed_before_release = set()
        cls.started = threading.Event()
        cls.release = threading.Event()
        cls.returned = threading.Event()

    def run(self, messages, *, message_id=None) -> InferenceResult:
        agent_id = self.config.agent_id
        attempt = self.__class__.attempts.get(agent_id, 0) + 1
        self.__class__.attempts[agent_id] = attempt
        should_hang = agent_id in self.__class__.hang_agents and (
            not self.__class__.recover_on_retry or attempt == 1
        )
        if should_hang:
            self.__class__.started.set()
            self.__class__.release.wait()
            self.__class__.returned.set()
            return successful_inference_result(
                self.config,
                message_id=str(message_id),
                content="SENSITIVE_LATE_PROVIDER_RESPONSE",
            )
        result = super().run(messages, message_id=message_id)
        if not self.__class__.release.is_set():
            self.__class__.completed_before_release.add(agent_id)
        return result

    def cancel(self) -> None:
        self.__class__.release.set()


class CancellationIgnoringRunner(SuccessfulRunner):
    started = threading.Event()
    release = threading.Event()
    cancel_calls = 0

    @classmethod
    def reset(cls) -> None:
        cls.started = threading.Event()
        cls.release = threading.Event()
        cls.cancel_calls = 0

    def run(self, messages, *, message_id=None) -> InferenceResult:
        self.__class__.started.set()
        self.__class__.release.wait()
        return successful_inference_result(
            self.config,
            message_id=str(message_id),
            content="late reply",
        )

    def cancel(self) -> None:
        self.__class__.cancel_calls += 1


class NoCancelNeverReturningRunner(SuccessfulRunner):
    started = threading.Event()
    release = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.started = threading.Event()
        cls.release = threading.Event()

    def run(self, messages, *, message_id=None) -> InferenceResult:
        self.__class__.started.set()
        self.__class__.release.wait()
        return super().run(messages, message_id=message_id)


class BlockingCancelRunner(SuccessfulRunner):
    started = threading.Event()
    run_release = threading.Event()
    cancel_started = threading.Event()
    cancel_release = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.started = threading.Event()
        cls.run_release = threading.Event()
        cls.cancel_started = threading.Event()
        cls.cancel_release = threading.Event()

    def run(self, messages, *, message_id=None) -> InferenceResult:
        self.__class__.started.set()
        self.__class__.run_release.wait()
        return super().run(messages, message_id=message_id)

    def cancel(self) -> None:
        self.__class__.cancel_started.set()
        self.__class__.cancel_release.wait()


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


def test_completed_inference_survives_transient_lease_renewal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "renewal-error",
            "subject": "Commit completed work",
            "body": "hello supervisor",
            "route_profile_id": "relay-one-model-a",
        }
    )
    original_renew = Bridge.renew_message_dispatch
    failures = {"count": 0}

    def fail_one_renewal(self, args):
        if self.agent_id == "model-peer" and failures["count"] == 0:
            failures["count"] += 1
            raise RuntimeError("synthetic transient renewal failure")
        return original_renew(self, args)

    monkeypatch.setattr(Bridge, "renew_message_dispatch", fail_one_renewal)
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

    assert failures["count"] == 1
    assert (result.claimed, result.completed, result.retryable_failures) == (1, 1, 0)
    with sqlite3.connect(human.db_path) as connection:
        dispatch = connection.execute(
            "SELECT status, attempt_count FROM message_dispatches WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()
        replies = connection.execute(
            "SELECT body FROM messages WHERE reply_to=?", (sent["message_id"],)
        ).fetchall()
    assert dispatch == ("completed", 1)
    assert replies == [("audited reply",)]
    supervisor.close()


def test_completed_provider_call_is_never_retried_after_final_lease_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "completed-provider-final-renewal-failed",
            "subject": "Do not repeat a completed paid call",
            "body": "hello supervisor",
            "route_profile_id": "relay-one-model-a",
        }
    )

    def reject_renewal(self, args):
        raise RuntimeError("synthetic final renewal failure")

    monkeypatch.setattr(Bridge, "renew_message_dispatch", reject_renewal)
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=SuccessfulRunner,
        credential_probe=lambda _route: True,
        lease_seconds=30,
        lease_renew_interval_seconds=10,
    )
    calls_before = len(SuccessfulRunner.configs)

    first = supervisor.run_cycle()
    second = supervisor.run_cycle()

    assert (first.claimed, first.terminal_failures, first.retryable_failures) == (
        1,
        1,
        0,
    )
    assert second.claimed == 0
    assert len(SuccessfulRunner.configs) == calls_before + 1
    with sqlite3.connect(human.db_path) as connection:
        dispatch = connection.execute(
            "SELECT status, error_code, attempt_count FROM message_dispatches "
            "WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()
        replies = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE reply_to=?",
            (sent["message_id"],),
        ).fetchone()[0]
    assert dispatch == (
        "failed",
        "provider_completed_lease_renewal_failed",
        1,
    )
    assert replies == 0
    supervisor.close()


def test_runner_hard_deadline_is_terminal_when_provider_completion_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr("peerbridge_mcp.bridge.time.time", lambda: clock[0])
    NeverReturningRunner.reset("model-peer", recover_on_retry=True)
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "runner-deadline",
            "subject": "Bound provider runtime",
            "body": "hello supervisor PRIVATE_REQUEST_BODY",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=NeverReturningRunner,
        credential_probe=lambda _route: True,
        lease_seconds=30,
        lease_renew_interval_seconds=0.03,
        runner_hard_deadline_seconds=0.18,
        retry_backoff_base_seconds=1,
        retry_backoff_cap_seconds=1,
    )

    try:
        started = time.monotonic()
        first = supervisor.run_cycle()
        elapsed = time.monotonic() - started

        assert NeverReturningRunner.started.is_set()
        assert NeverReturningRunner.returned.is_set()
        assert elapsed < 1.0
        assert (
            first.claimed,
            first.completed,
            first.retryable_failures,
            first.terminal_failures,
        ) == (1, 0, 0, 1)
        with sqlite3.connect(human.db_path) as connection:
            dispatch = connection.execute(
                "SELECT status, claimed_session_id, lease_token_sha256, "
                "lease_expires_epoch, attempt_count, error_code "
                "FROM message_dispatches WHERE message_id=?",
                (sent["message_id"],),
            ).fetchone()
            schedule = connection.execute(
                "SELECT not_before_epoch, error_code "
                "FROM message_dispatch_retry_schedules WHERE message_id=?",
                (sent["message_id"],),
            ).fetchone()
            renewal_count = connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='message.dispatch_renewed'"
            ).fetchone()[0]
            failure_payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM events "
                    "WHERE event_type='message.dispatch_failed' ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0]
            )
        assert dispatch == (
            "failed",
            None,
            None,
            None,
            1,
            "provider_completion_ambiguous_hard_deadline",
        )
        assert schedule is None
        assert renewal_count >= 1
        assert failure_payload["error_code"] == (
            "provider_completion_ambiguous_hard_deadline"
        )
        serialized_evidence = json.dumps(failure_payload, sort_keys=True)
        assert "PRIVATE_REQUEST_BODY" not in serialized_evidence
        assert "SENSITIVE_LATE_PROVIDER_RESPONSE" not in serialized_evidence

        time.sleep(0.12)
        with sqlite3.connect(human.db_path) as connection:
            stopped_renewal_count = connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='message.dispatch_renewed'"
            ).fetchone()[0]
        assert stopped_renewal_count == renewal_count

        assert NeverReturningRunner.returned.wait(timeout=1.0)
        with sqlite3.connect(human.db_path) as connection:
            replies = connection.execute(
                "SELECT body FROM messages WHERE reply_to=?",
                (sent["message_id"],),
            ).fetchall()
            final_dispatch = connection.execute(
                "SELECT status, attempt_count, inference_receipt_sha256 "
                "FROM message_dispatches WHERE message_id=?",
                (sent["message_id"],),
            ).fetchone()
        assert replies == []
        assert final_dispatch == ("failed", 1, None)
        assert not any(
            thread.name.startswith("peerbridge-runner-") and thread.is_alive()
            for thread in threading.enumerate()
        )
    finally:
        NeverReturningRunner.release.set()
        supervisor.close()


def test_runner_that_ignores_cancel_fails_terminally_without_freezing_supervisor(
    tmp_path: Path,
) -> None:
    CancellationIgnoringRunner.reset()
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "cancel-incomplete",
            "subject": "Bound cancellation",
            "body": "do not freeze",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=CancellationIgnoringRunner,
        credential_probe=lambda _route: True,
        runner_hard_deadline_seconds=0.05,
        runner_cancel_grace_seconds=0.05,
    )

    try:
        started = time.monotonic()
        result = supervisor.run_cycle()
        elapsed = time.monotonic() - started

        assert CancellationIgnoringRunner.started.is_set()
        assert CancellationIgnoringRunner.cancel_calls == 1
        assert elapsed < 0.5
        assert (result.claimed, result.completed, result.terminal_failures) == (1, 0, 1)
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
        assert dispatch == ("failed", 1, "runner_cancellation_incomplete")
        assert replies == 0
        assert supervisor.run_cycle().claimed == 0
    finally:
        CancellationIgnoringRunner.release.set()
        supervisor.close()


def test_runner_without_cancel_hits_terminal_deadline_without_freezing(
    tmp_path: Path,
) -> None:
    NoCancelNeverReturningRunner.reset()
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "missing-cancel",
            "subject": "Bound a runner without cancellation",
            "body": "hello supervisor",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=NoCancelNeverReturningRunner,
        credential_probe=lambda _route: True,
        runner_hard_deadline_seconds=0.05,
        runner_cancel_grace_seconds=0.05,
    )

    try:
        started = time.monotonic()
        result = supervisor.run_cycle()
        elapsed = time.monotonic() - started

        assert NoCancelNeverReturningRunner.started.is_set()
        assert elapsed < 0.5
        assert (result.claimed, result.terminal_failures) == (1, 1)
        with sqlite3.connect(human.db_path) as connection:
            dispatch = connection.execute(
                "SELECT status, error_code FROM message_dispatches "
                "WHERE message_id=?",
                (sent["message_id"],),
            ).fetchone()
        assert dispatch == ("failed", "runner_cancellation_incomplete")
    finally:
        NoCancelNeverReturningRunner.release.set()
        supervisor.close()


def test_blocking_cancel_cannot_freeze_supervisor(tmp_path: Path) -> None:
    BlockingCancelRunner.reset()
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "blocking-cancel",
            "subject": "Bound a blocking cancel method",
            "body": "hello supervisor",
            "route_profile_id": "relay-one-model-a",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=BlockingCancelRunner,
        credential_probe=lambda _route: True,
        runner_hard_deadline_seconds=0.05,
        runner_cancel_grace_seconds=0.05,
    )

    try:
        started = time.monotonic()
        result = supervisor.run_cycle()
        elapsed = time.monotonic() - started

        assert BlockingCancelRunner.started.is_set()
        assert BlockingCancelRunner.cancel_started.is_set()
        # The blocking cancel callback is deliberately never released in this
        # scope. Returning within this generous CI bound proves the supervisor
        # did not join that callback while tolerating hosted-runner scheduling.
        assert elapsed < 2.0
        assert (result.claimed, result.terminal_failures) == (1, 1)
        with sqlite3.connect(human.db_path) as connection:
            dispatch = connection.execute(
                "SELECT status, error_code FROM message_dispatches "
                "WHERE message_id=?",
                (sent["message_id"],),
            ).fetchone()
        assert dispatch == ("failed", "runner_cancellation_incomplete")
    finally:
        BlockingCancelRunner.cancel_release.set()
        BlockingCancelRunner.run_release.set()
        supervisor.close()


def test_persistent_lease_renewal_failure_aborts_before_local_safety_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    NeverReturningRunner.reset("model-peer")
    human = make_bridge(tmp_path, "human-operator")
    register_route(human)
    sent = human.send_message(
        {
            "recipient": "model-peer",
            "task_id": "lease-renewal-abort",
            "subject": "Stop before lease ownership becomes uncertain",
            "body": "hello supervisor",
            "route_profile_id": "relay-one-model-a",
        }
    )

    def reject_renewal(self, args):
        raise RuntimeError("synthetic persistent renewal failure")

    monkeypatch.setattr(Bridge, "renew_message_dispatch", reject_renewal)
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=NeverReturningRunner,
        credential_probe=lambda _route: True,
        lease_renew_interval_seconds=0.02,
        runner_hard_deadline_seconds=5.0,
        runner_cancel_grace_seconds=0.1,
    )
    supervisor.lease_seconds = 0.2

    try:
        started = time.monotonic()
        result = supervisor.run_cycle()
        elapsed = time.monotonic() - started

        assert NeverReturningRunner.started.is_set()
        assert NeverReturningRunner.returned.is_set()
        assert elapsed < 1.0
        assert (result.claimed, result.retryable_failures, result.terminal_failures) == (
            1,
            0,
            1,
        )
        with sqlite3.connect(human.db_path) as connection:
            dispatch = connection.execute(
                "SELECT status, error_code FROM message_dispatches "
                "WHERE message_id=?",
                (sent["message_id"],),
            ).fetchone()
            replies = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE reply_to=?",
                (sent["message_id"],),
            ).fetchone()[0]
        assert dispatch == (
            "failed",
            "provider_completion_ambiguous_lease_loss",
        )
        assert replies == 0
    finally:
        NeverReturningRunner.release.set()
        supervisor.close()


def test_runner_hard_deadline_does_not_block_other_routes(tmp_path: Path) -> None:
    NeverReturningRunner.reset("hung-peer")
    human = make_bridge(tmp_path, "human-operator")
    register_route(
        human,
        agent_id="hung-peer",
        connection_id="hung-connection",
        route_id="hung-route",
    )
    register_route(
        human,
        agent_id="healthy-peer",
        connection_id="healthy-connection",
        route_id="healthy-route",
    )
    hung = human.send_message(
        {
            "recipient": "hung-peer",
            "task_id": "hung-route",
            "subject": "Never return",
            "body": "hello supervisor",
            "route_profile_id": "hung-route",
        }
    )
    healthy = human.send_message(
        {
            "recipient": "healthy-peer",
            "task_id": "healthy-route",
            "subject": "Continue independently",
            "body": "hello supervisor",
            "route_profile_id": "healthy-route",
        }
    )
    supervisor = MailboxSupervisor(
        tmp_path,
        human.db_path,
        "test",
        runner_factory=NeverReturningRunner,
        credential_probe=lambda _route: True,
        lease_renew_interval_seconds=0.03,
        # Give a loaded Windows runner enough scheduling headroom. The ordering
        # assertion below proves route isolation without relying on wall time.
        runner_hard_deadline_seconds=5.0,
        runner_cancel_grace_seconds=0.5,
    )

    try:
        result = supervisor.run_cycle()

        assert NeverReturningRunner.started.is_set()
        assert "healthy-peer" in NeverReturningRunner.completed_before_release
        assert (
            result.claimed,
            result.completed,
            result.retryable_failures,
            result.terminal_failures,
        ) == (2, 1, 0, 1)
        with sqlite3.connect(human.db_path) as connection:
            dispatches = dict(
                connection.execute(
                    "SELECT message_id, status FROM message_dispatches"
                ).fetchall()
            )
            replies = connection.execute(
                "SELECT reply_to, body FROM messages WHERE reply_to IS NOT NULL"
            ).fetchall()
        assert dispatches == {
            hung["message_id"]: "failed",
            healthy["message_id"]: "completed",
        }
        assert replies == [(healthy["message_id"], "audited reply")]
    finally:
        NeverReturningRunner.release.set()
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
        return successful_inference_result(
            self.config,
            message_id=str(message_id),
            content=(
                f"{self.config.agent_id} accepts the bounded proposal.\n"
                "PEERBRIDGE_SIGNAL: CONSENSUS"
            ),
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
