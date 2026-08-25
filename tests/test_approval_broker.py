from __future__ import annotations

import threading

import pytest
import peerbridge_mcp.approval_broker as broker_module

from peerbridge_mcp.approval_broker import ApprovalBroker


def _request(broker: ApprovalBroker, result: list[str]) -> None:
    result.append(
        broker.request(
            provider_request_id="provider-request-one",
            action_kind="command-execution",
            title="Run unit tests",
            detail="pytest -q",
            timeout_seconds=5,
        )
    )


def test_approval_required_pauses_and_resumes_exact_request() -> None:
    changes = []
    broker = ApprovalBroker(
        session_id="session-one",
        adapter_id="codex-app-server",
        mode="approval-required",
        on_change=changes.append,
    )
    result: list[str] = []
    worker = threading.Thread(target=_request, args=(broker, result))
    worker.start()
    for _ in range(100):
        pending = broker.snapshot()["pending"]
        if pending:
            break
        worker.join(timeout=0.01)
    else:
        raise AssertionError("approval request was not published")

    approval_id = pending[0]["approval_id"]
    broker.resolve(str(approval_id), "allow-once")
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result == ["allow-once"]
    assert broker.snapshot()["pending_count"] == 0
    assert broker.snapshot()["history"][0]["state"] == "allowed"
    assert changes[0].state == "pending"
    assert changes[-1].state == "allowed"


def test_agent_delegated_only_auto_approves_routine_actions() -> None:
    broker = ApprovalBroker(
        session_id="session-two",
        adapter_id="grok-native-acp",
        mode="agent-delegated",
    )

    assert broker.request(
        provider_request_id="routine-one",
        action_kind="read-file",
        title="Read README",
        detail="README.md",
        risk="routine",
    ) == "allow-once"
    assert broker.snapshot()["pending_count"] == 0


def test_allow_session_is_applied_by_provider_not_reused_from_provider_title() -> None:
    broker = ApprovalBroker(
        session_id="session-provider-grant",
        adapter_id="grok-native-acp",
        mode="agent-delegated",
    )
    first: list[str] = []
    worker = threading.Thread(
        target=lambda: first.append(
            broker.request(
                provider_request_id="provider-grant-one",
                action_kind="shell",
                title="Run command",
                detail="first command",
                risk="high",
                timeout_seconds=5,
            )
        )
    )
    worker.start()
    for _ in range(100):
        pending = broker.snapshot()["pending"]
        if pending:
            break
        worker.join(timeout=0.01)
    else:
        raise AssertionError("first approval request was not published")
    broker.resolve(str(pending[0]["approval_id"]), "allow-session")
    worker.join(timeout=5)
    assert first == ["allow-session"]

    second: list[str] = []
    worker = threading.Thread(
        target=lambda: second.append(
            broker.request(
                provider_request_id="provider-grant-two",
                action_kind="shell",
                title="Run command",
                detail="different command",
                risk="high",
                timeout_seconds=5,
            )
        )
    )
    worker.start()
    for _ in range(100):
        pending = broker.snapshot()["pending"]
        if pending:
            break
        worker.join(timeout=0.01)
    else:
        raise AssertionError("second approval request was incorrectly auto-approved")
    broker.resolve(str(pending[0]["approval_id"]), "deny")
    worker.join(timeout=5)
    assert second == ["deny"]


def test_full_access_does_not_create_pending_prompts() -> None:
    broker = ApprovalBroker(
        session_id="session-three",
        adapter_id="kimi-native-acp",
        mode="full-access",
    )

    assert broker.request(
        provider_request_id="full-one",
        action_kind="shell",
        title="Run build",
        detail="npm run build",
        risk="high",
    ) == "allow-once"
    assert broker.snapshot()["pending_count"] == 0


def test_broker_redacts_secret_shaped_details() -> None:
    broker = ApprovalBroker(
        session_id="session-four",
        adapter_id="codex-app-server",
        mode="approval-required",
    )
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(
            broker.request(
                provider_request_id="secret-one",
                action_kind="network",
                title="Call service",
                detail=(
                    "Authorization: "
                    + " ".join(("Bearer", "sk-test-" + "secret-value-1234567890"))
                ),
                timeout_seconds=5,
            )
        )
    )
    worker.start()
    for _ in range(100):
        pending = broker.snapshot()["pending"]
        if pending:
            break
        worker.join(timeout=0.01)
    else:
        raise AssertionError("approval request was not published")
    assert "sk-test" not in pending[0]["detail"]
    broker.resolve(str(pending[0]["approval_id"]), "deny")
    worker.join(timeout=5)
    assert result == ["deny"]


def test_duplicate_provider_request_and_invalid_resolution_fail_closed() -> None:
    broker = ApprovalBroker(
        session_id="session-five",
        adapter_id="codex-app-server",
        mode="approval-required",
    )

    with pytest.raises(ValueError, match="provider approval request"):
        broker.request(
            provider_request_id="bad request",
            action_kind="shell",
            title="Run",
            detail="command",
        )
    with pytest.raises(KeyError, match="not pending"):
        broker.resolve("approval-missing", "deny")


def test_pending_approval_limit_rejects_provider_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_module, "MAX_PENDING_APPROVALS", 1)
    broker = ApprovalBroker(
        session_id="session-limit",
        adapter_id="claude-stream-json",
        mode="approval-required",
    )
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(
            broker.request(
                provider_request_id="limit-one",
                action_kind="write",
                title="First write",
                detail="first",
                timeout_seconds=5,
            )
        )
    )
    worker.start()
    for _ in range(100):
        pending = broker.snapshot()["pending"]
        if pending:
            break
        worker.join(timeout=0.01)
    else:
        raise AssertionError("first approval request was not published")

    with pytest.raises(ValueError, match="pending approval limit"):
        broker.request(
            provider_request_id="limit-two",
            action_kind="write",
            title="Second write",
            detail="second",
            timeout_seconds=1,
        )

    broker.resolve(str(pending[0]["approval_id"]), "deny")
    worker.join(timeout=5)
    assert result == ["deny"]
