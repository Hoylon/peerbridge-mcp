from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.server import handle_request


def _tool_payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list)
    payload = json.loads(content[0]["text"])
    assert isinstance(payload, dict)
    return payload


def test_committed_fanout_survives_return_audit_failure_and_replays_once(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "bridge.sqlite3"
    session_id = "mcp-fanout-session"
    bridge = Bridge(
        tmp_path,
        database,
        "human-operator",
        "server-test",
        session_id=session_id,
    )
    bridge.create_room({"room_id": "team", "name": "Team"})
    bridge.upsert_route_profile(
        {
            "route_id": "worker-route",
            "agent_id": "worker",
            "provider_id": "relay-worker",
            "model_id": "worker-model",
            "route_class": "relay",
        }
    )
    bridge.join_room(
        {
            "room_id": "team",
            "agent_id": "worker",
            "route_profile_id": "worker-route",
        }
    )
    request = {
        "jsonrpc": "2.0",
        "id": "fanout-retry-1",
        "method": "tools/call",
        "params": {
            "name": "send_room_fanout",
                "arguments": {
                    "room_id": "team",
                    "task_id": "release-blocker",
                    "subject": "Run once",
                    "body": "A retried MCP request must not duplicate this delivery.",
                    "idempotency_key": "fanout-stable-key-1",
            },
        },
    }

    original_event = bridge._event
    injected_failures = 0

    def fail_returned_audit(connection, event_type, payload, task_id=None):
        nonlocal injected_failures
        if event_type == "tool.returned":
            injected_failures += 1
            raise sqlite3.OperationalError("injected tool.returned audit failure")
        return original_event(connection, event_type, payload, task_id)

    monkeypatch.setattr(bridge, "_event", fail_returned_audit)
    first_response = handle_request(bridge, request)
    assert first_response is not None and "error" not in first_response
    first_payload = _tool_payload(first_response)
    assert injected_failures == 1

    # Recreate the Bridge to prove replay is backed by the durable SQLite
    # receipt, not only by the same Python object's memory cache.
    retry_bridge = Bridge(
        tmp_path,
        database,
        "human-operator",
        "server-test",
        session_id="mcp-fanout-restarted-session",
    )
    retry_response = handle_request(retry_bridge, request)
    assert retry_response == first_response
    assert _tool_payload(retry_response)["fanout_id"] == first_payload["fanout_id"]

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mcp_mutation_receipts"
        ).fetchone()[0] == 1
        tool_events = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM events WHERE event_type LIKE 'tool.%' "
                "ORDER BY sequence"
            )
        ]
    assert tool_events == ["tool.called", "tool.called", "tool.returned"]
    assert retry_bridge.verify_audit_chain()["valid"] is True


def test_message_and_receipt_roll_back_together_before_retry(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "bridge.sqlite3"
    bridge = Bridge(
        tmp_path,
        database,
        "human-operator",
        "server-test",
        session_id="first-process",
    )
    request = {
        "jsonrpc": "2.0",
        "id": "message-crash-1",
        "method": "tools/call",
        "params": {
            "name": "send_message",
            "arguments": {
                "recipient": "worker",
                "task_id": "crash-safe",
                "subject": "Exactly once",
                "body": "The message and receipt share one transaction.",
                "idempotency_key": "message-stable-key-1",
            },
        },
    }
    original_store = bridge._store_mcp_mutation_receipt_locked

    def fail_receipt_store(connection, metadata, result):
        raise sqlite3.OperationalError("injected receipt write failure")

    monkeypatch.setattr(
        bridge, "_store_mcp_mutation_receipt_locked", fail_receipt_store
    )
    first = handle_request(bridge, request)
    assert first is not None and first["error"]["code"] == -32603
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM mcp_mutation_receipts"
        ).fetchone()[0] == 0

    monkeypatch.setattr(
        bridge, "_store_mcp_mutation_receipt_locked", original_store
    )
    retry_bridge = Bridge(
        tmp_path,
        database,
        "human-operator",
        "server-test",
        session_id="second-process",
    )
    retry = handle_request(retry_bridge, request)
    assert retry is not None and "error" not in retry
    replay = handle_request(retry_bridge, request)
    assert replay == retry
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mcp_mutation_receipts"
        ).fetchone()[0] == 1


def test_idempotency_key_reuse_with_different_arguments_is_rejected(
    tmp_path: Path,
) -> None:
    bridge = Bridge(tmp_path, tmp_path / "bridge.sqlite3", "human-operator", "server-test")
    base = {
        "jsonrpc": "2.0",
        "id": "same-key",
        "method": "tools/call",
        "params": {
            "name": "send_message",
            "arguments": {
                "recipient": "worker",
                "task_id": "conflict",
                "subject": "One",
                "body": "Original body.",
                "idempotency_key": "do-not-reuse",
            },
        },
    }
    assert "error" not in handle_request(bridge, base)
    conflicting = json.loads(json.dumps(base))
    conflicting["params"]["arguments"]["body"] = "Different body."
    response = handle_request(bridge, conflicting)
    payload = _tool_payload(response)
    assert "reused with different arguments" in payload["error"]
    with sqlite3.connect(bridge.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
