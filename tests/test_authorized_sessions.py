from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import peerbridge_mcp.authorized_sessions as authorized_sessions_module
from peerbridge_mcp.authorized_sessions import (
    AuthorizedSessionError,
    AuthorizedSessionRegistry,
    _session_payload,
)
from peerbridge_mcp.bridge import Bridge, SCHEMA_VERSION, stable_sha256
from peerbridge_mcp.server import handle_request


SCOPE = "authorized-session-test"


def _bridge(
    root: Path,
    agent_id: str,
    session_id: str,
    *,
    client_name: str | None = None,
) -> Bridge:
    return Bridge(
        root,
        root / ".peerbridge" / "authorized-sessions.sqlite3",
        agent_id,
        SCOPE,
        session_id=session_id,
        client_name=client_name,
    )


def _connected(
    registry: AuthorizedSessionRegistry,
    *,
    source_type: str = "authorized-desktop",
    source_session_id: str = "desktop-one",
    source_conversation_id: str = "conversation-one",
    room_id: str | None = None,
    supports_events: bool = True,
) -> dict[str, object]:
    args: dict[str, object] = {
        "source_type": source_type,
        "source_session_id": source_session_id,
        "source_conversation_id": source_conversation_id,
        "adapter_id": "test-adapter-v1",
        "display_name": "Review session",
        "client_name": "Codex Desktop",
        "supports_events": supports_events,
        "state": "running",
    }
    if room_id:
        args["room_id"] = room_id
    return registry.connect(args)


def _payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list) and content
    payload = json.loads(content[0]["text"])
    assert isinstance(payload, dict)
    return payload


def _call(
    bridge: Bridge, name: str, arguments: dict[str, object] | None = None
) -> dict[str, object]:
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": name,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert response is not None and "error" not in response
    return _payload(response)


def test_current_schema_adds_bounded_authorized_session_tables(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "human-operator", "human-session")
    with sqlite3.connect(bridge.db_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = str(
            connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
        )
    assert version == SCHEMA_VERSION
    assert int(SCHEMA_VERSION) >= 22
    assert {"authorized_sessions", "authorized_session_events"}.issubset(tables)


def test_authorized_adapter_binds_exact_room_session_and_redacts_before_storage(
    tmp_path: Path,
) -> None:
    human = _bridge(tmp_path, "human-operator", "human-session")
    human.create_room({"room_id": "review-room", "name": "Review Room"})
    joined = human.join_room(
        {
            "room_id": "review-room",
            "agent_id": "review-agent",
            "role_id": "reviewer",
        }
    )
    adapter = _bridge(
        tmp_path, "review-agent", "adapter-session", client_name="codex-desktop"
    )
    registry = AuthorizedSessionRegistry(adapter)
    connected = _connected(registry, room_id="review-room")

    assert connected["session_id"] == "authorized-desktop:desktop-one"
    assert connected["room_session_id"] == joined["room_session_id"]
    assert connected["role"] == "reviewer"
    assert connected["input_owner"] == "external-desktop"
    assert connected["capabilities"] == {
        "detectable": True,
        "mirrorable": True,
        "input_capable": False,
        "context_resumable": False,
        "terminal_controllable": False,
        "model_route_only": False,
    }

    published = registry.publish_event(
        {
            "source_type": "authorized-desktop",
            "source_session_id": "desktop-one",
            "event_id": "event-one",
            "stream": "stdout",
            "kind": "activity",
            "text": "Checked source. api_key=realistic-secret-value-123",
            "summary": "Observable progress",
            "state": "running",
        }
    )
    assert published["secret_redacted"] is True

    listed = AuthorizedSessionRegistry(human).list_for_control_room(
        include_detected=False
    )
    assert len(listed) == 1
    assert listed[0]["events"][0]["text"] == "Checked source. api_key=[REDACTED]"
    with sqlite3.connect(human.db_path) as connection:
        persisted = str(
            connection.execute(
                "SELECT text FROM authorized_session_events"
            ).fetchone()[0]
        )
    assert "realistic-secret-value-123" not in persisted


def test_adapter_event_ids_are_idempotent_and_retention_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authorized_sessions_module, "MAX_AUTHORIZED_EVENTS", 3)
    adapter = _bridge(tmp_path, "agent-one", "adapter-one")
    registry = AuthorizedSessionRegistry(adapter)
    _connected(registry)

    first = registry.publish_event(
        {
            "source_type": "authorized-desktop",
            "source_session_id": "desktop-one",
            "event_id": "event-1",
            "stream": "stdout",
            "kind": "activity",
            "text": "first",
        }
    )
    replay = registry.publish_event(
        {
            "source_type": "authorized-desktop",
            "source_session_id": "desktop-one",
            "event_id": "event-1",
            "stream": "stdout",
            "kind": "activity",
            "text": "first",
        }
    )
    assert replay["idempotent_replay"] is True
    assert replay["event_sha256"] == first["event_sha256"]
    with pytest.raises(AuthorizedSessionError, match="different observable content"):
        registry.publish_event(
            {
                "source_type": "authorized-desktop",
                "source_session_id": "desktop-one",
                "event_id": "event-1",
                "stream": "stdout",
                "kind": "activity",
                "text": "changed",
            }
        )

    for index in range(2, 6):
        registry.publish_event(
            {
                "source_type": "authorized-desktop",
                "source_session_id": "desktop-one",
                "event_id": f"event-{index}",
                "stream": "stdout",
                "kind": "activity",
                "text": f"event {index}",
            }
        )
    snapshot = registry.get("authorized-desktop", "desktop-one")
    assert [event["sequence"] for event in snapshot["events"]] == [3, 4, 5]
    assert snapshot["usage_capture_truncated"] is True


def test_event_reads_use_panel_sequence_cursors_and_recover_retention_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authorized_sessions_module, "MAX_AUTHORIZED_EVENTS", 3)
    adapter = _bridge(tmp_path, "agent-one", "adapter-one")
    registry = AuthorizedSessionRegistry(adapter)
    _connected(registry)
    for index in range(1, 4):
        registry.publish_event(
            {
                "source_type": "authorized-desktop",
                "source_session_id": "desktop-one",
                "event_id": f"event-{index}",
                "stream": "stdout",
                "kind": "activity",
                "text": f"event {index}",
            }
        )

    control = AuthorizedSessionRegistry(
        _bridge(tmp_path, "human-operator", "human-session")
    )
    panel_id = "authorized-desktop:desktop-one"
    incremental = control.list_for_control_room(
        include_detected=False, after_sequences={panel_id: 2}
    )[0]
    assert [event["sequence"] for event in incremental["events"]] == [3]
    assert incremental["first_retained_sequence"] == 1
    assert incremental["latest_sequence"] == 3
    assert control.list_for_control_room(
        include_detected=False, after_sequences={panel_id: 3}
    )[0]["events"] == []

    for index in range(4, 6):
        registry.publish_event(
            {
                "source_type": "authorized-desktop",
                "source_session_id": "desktop-one",
                "event_id": f"event-{index}",
                "stream": "stdout",
                "kind": "activity",
                "text": f"event {index}",
            }
        )
    recovered = control.list_for_control_room(
        include_detected=False, after_sequences={panel_id: 1}
    )[0]
    assert [event["sequence"] for event in recovered["events"]] == [3, 4, 5]
    assert recovered["first_retained_sequence"] == 3
    assert recovered["usage_capture_truncated"] is True


def test_event_list_response_has_aggregate_count_and_utf8_byte_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        authorized_sessions_module, "MAX_AUTHORIZED_RESPONSE_EVENTS", 3
    )
    monkeypatch.setattr(
        authorized_sessions_module, "MAX_AUTHORIZED_RESPONSE_BYTES", 650
    )
    adapter = _bridge(tmp_path, "agent-one", "adapter-one")
    registry = AuthorizedSessionRegistry(adapter)
    _connected(registry)
    for index in range(1, 6):
        registry.publish_event(
            {
                "source_type": "authorized-desktop",
                "source_session_id": "desktop-one",
                "event_id": f"event-{index}",
                "stream": "stdout",
                "kind": "activity",
                "text": f"event-{index}",
            }
        )

    control = AuthorizedSessionRegistry(
        _bridge(tmp_path, "human-operator", "human-session")
    )
    first = control.list_for_control_room(include_detected=False)[0]
    assert [event["sequence"] for event in first["events"]] == [1, 2, 3]
    assert sum(
        len(
            json.dumps(
                event, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        for event in first["events"]
    ) <= 650
    second = control.list_for_control_room(
        include_detected=False,
        after_sequences={"authorized-desktop:desktop-one": 3},
    )[0]
    assert [event["sequence"] for event in second["events"]] == [4, 5]


@pytest.mark.parametrize("modern", [False, True])
def test_observable_session_transport_response_respects_complete_utf8_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    modern: bool,
) -> None:
    response_limit = 12_000
    monkeypatch.setattr(
        authorized_sessions_module,
        "MAX_AUTHORIZED_RESPONSE_BYTES",
        response_limit,
    )
    owner = _bridge(tmp_path, "agent-one", "adapter-one")
    registry = AuthorizedSessionRegistry(owner)
    _connected(
        registry,
        source_type="authorized-terminal",
        source_session_id="terminal-one",
    )
    for index in range(1, 11):
        registry.publish_event(
            {
                "source_type": "authorized-terminal",
                "source_session_id": "terminal-one",
                "event_id": f"event-{index}",
                "stream": "stdout",
                "kind": "activity",
                "text": (f"event-{index}: \"多語系\\output\" " * 100),
            }
        )

    params: dict[str, object] = {
        "name": "list_own_observable_sessions",
        "arguments": {},
    }
    if modern:
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28"
        }
    response = handle_request(
        owner,
        {
            "jsonrpc": "2.0",
            "id": "bounded-observable-sessions",
            "method": "tools/call",
            "params": params,
        },
    )
    assert response is not None and "error" not in response
    serialized = (
        json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    assert len(serialized) <= response_limit

    result = response["result"]
    assert isinstance(result, dict)
    payload = json.loads(result["content"][0]["text"])
    if modern:
        assert payload == result["structuredContent"]
    returned_events = payload["sessions"][0]["events"]
    returned_sequences = [event["sequence"] for event in returned_events]
    assert returned_sequences == list(range(1, len(returned_sequences) + 1))
    assert 0 < len(returned_sequences) < 10
    assert payload["sessions"][0]["latest_sequence"] == 10

    continuation = _call(
        owner,
        "list_own_observable_sessions",
        {
            "after_sequences": {
                "authorized-terminal:terminal-one": returned_sequences[-1]
            }
        },
    )
    assert continuation["sessions"][0]["events"][0]["sequence"] == (
        returned_sequences[-1] + 1
    )


def test_event_list_count_budget_is_global_across_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        authorized_sessions_module, "MAX_AUTHORIZED_RESPONSE_EVENTS", 1
    )
    adapter = _bridge(tmp_path, "agent-one", "adapter-one")
    registry = AuthorizedSessionRegistry(adapter)
    for source_session_id in ("desktop-one", "desktop-two"):
        _connected(registry, source_session_id=source_session_id)
        registry.publish_event(
            {
                "source_type": "authorized-desktop",
                "source_session_id": source_session_id,
                "event_id": f"event-{source_session_id}",
                "stream": "stdout",
                "kind": "activity",
                "text": source_session_id,
            }
        )

    sessions = AuthorizedSessionRegistry(
        _bridge(tmp_path, "human-operator", "human-session")
    ).list_for_control_room(include_detected=False)
    assert sum(len(session["events"]) for session in sessions) == 1


def test_total_session_retention_prunes_terminal_history_with_events(
    tmp_path: Path,
) -> None:
    registry = AuthorizedSessionRegistry(
        _bridge(tmp_path, "agent-one", "adapter-one"),
        max_retained_sessions=2,
    )
    _connected(registry, source_session_id="desktop-one")
    registry.publish_event(
        {
            "source_type": "authorized-desktop",
            "source_session_id": "desktop-one",
            "event_id": "event-one",
            "stream": "stdout",
            "kind": "activity",
            "text": "terminal history",
        }
    )
    registry.close(
        {
            "source_type": "authorized-desktop",
            "source_session_id": "desktop-one",
            "state": "completed",
        }
    )
    _connected(registry, source_session_id="desktop-two")
    _connected(registry, source_session_id="desktop-three")

    with sqlite3.connect(registry.bridge.db_path) as connection:
        retained = {
            str(row[0])
            for row in connection.execute(
                "SELECT source_session_id FROM authorized_sessions WHERE scope=?",
                (SCOPE,),
            )
        }
        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM authorized_session_events WHERE scope=?",
                (SCOPE,),
            ).fetchone()[0]
        )
    assert retained == {"desktop-two", "desktop-three"}
    assert event_count == 0


def test_total_session_retention_fails_closed_when_every_session_is_live(
    tmp_path: Path,
) -> None:
    registry = AuthorizedSessionRegistry(
        _bridge(tmp_path, "agent-one", "adapter-one"),
        max_retained_sessions=2,
    )
    _connected(registry, source_session_id="desktop-one")
    _connected(registry, source_session_id="desktop-two")

    with pytest.raises(AuthorizedSessionError, match="all retained sessions are live"):
        _connected(registry, source_session_id="desktop-three")
    with sqlite3.connect(registry.bridge.db_path) as connection:
        retained = int(
            connection.execute(
                "SELECT COUNT(*) FROM authorized_sessions WHERE scope=?", (SCOPE,)
            ).fetchone()[0]
        )
    assert retained == 2


def test_live_adapter_owner_and_room_binding_cannot_be_stolen(
    tmp_path: Path,
) -> None:
    human = _bridge(tmp_path, "human-operator", "human-session")
    human.create_room({"room_id": "one-room", "name": "One Room"})
    human.join_room({"room_id": "one-room", "agent_id": "agent-one"})
    first = AuthorizedSessionRegistry(
        _bridge(tmp_path, "agent-one", "adapter-one")
    )
    _connected(first, room_id="one-room")

    second = AuthorizedSessionRegistry(
        _bridge(tmp_path, "agent-one", "adapter-two")
    )
    with pytest.raises(AuthorizedSessionError, match="another live.*adapter"):
        _connected(
            second,
            source_type="authorized-terminal",
            source_session_id="terminal-two",
            room_id="one-room",
        )
    with pytest.raises(AuthorizedSessionError, match="bound external adapter"):
        second.publish_event(
            {
                "source_type": "authorized-desktop",
                "source_session_id": "desktop-one",
                "event_id": "stolen-event",
                "stream": "stdout",
                "kind": "activity",
                "text": "must fail",
            }
        )


def test_presence_detection_never_claims_uncaptured_output_or_input(
    tmp_path: Path,
) -> None:
    human = _bridge(tmp_path, "human-operator", "human-session")
    desktop = _bridge(
        tmp_path, "codex-one", "codex-live", client_name="codex-desktop"
    )
    terminal = _bridge(
        tmp_path, "claude-one", "claude-live", client_name="claude-code"
    )
    desktop.touch_presence("stdio")
    terminal.touch_presence("stdio")

    sessions = AuthorizedSessionRegistry(human).list_for_control_room()
    by_agent = {session["agent_id"]: session for session in sessions}
    assert by_agent["codex-one"]["source_type"] == "authorized-desktop"
    assert by_agent["claude-one"]["source_type"] == "authorized-terminal"
    for session in by_agent.values():
        assert session["state"] == "detected"
        assert session["events"] == []
        assert session["reasoning_contract"] == "no-output-captured"
        assert session["capabilities"]["detectable"] is True
        assert session["capabilities"]["mirrorable"] is False
        assert session["capabilities"]["input_capable"] is False
        assert session["capabilities"]["terminal_controllable"] is False


def test_stale_same_agent_adapter_can_rebind_without_live_dual_writer(
    tmp_path: Path,
) -> None:
    first = AuthorizedSessionRegistry(
        _bridge(tmp_path, "agent-one", "adapter-one"), adapter_ttl_seconds=30
    )
    _connected(first)
    with first.bridge._connect() as connection:
        connection.execute(
            """UPDATE authorized_sessions
                  SET last_seen_epoch=?, session_sha256='pending'
                WHERE scope=? AND source_session_id='desktop-one'""",
            (time.time() - 60, SCOPE),
        )
        row = connection.execute(
            "SELECT * FROM authorized_sessions WHERE source_session_id='desktop-one'"
        ).fetchone()
        connection.execute(
            """UPDATE authorized_sessions SET session_sha256=?
                WHERE scope=? AND source_session_id='desktop-one'""",
            (
                stable_sha256(_session_payload(row)),
                SCOPE,
            ),
        )
    replacement = AuthorizedSessionRegistry(
        _bridge(tmp_path, "agent-one", "adapter-two"), adapter_ttl_seconds=30
    )
    rebound = _connected(replacement)
    assert rebound["owner_bridge_session_id"] == "adapter-two"


def test_authorized_session_id_cannot_be_reused_for_another_room_binding(
    tmp_path: Path,
) -> None:
    human = _bridge(tmp_path, "human-operator", "human-session")
    for room_id in ("room-a", "room-b"):
        human.create_room({"room_id": room_id, "name": room_id})
        human.join_room({"room_id": room_id, "agent_id": "agent-one"})
    registry = AuthorizedSessionRegistry(
        _bridge(tmp_path, "agent-one", "adapter-one")
    )
    _connected(registry, room_id="room-a")
    registry.publish_event(
        {
            "source_type": "authorized-desktop",
            "source_session_id": "desktop-one",
            "event_id": "room-a-answer",
            "stream": "stdout",
            "kind": "answer",
            "text": "Room A only",
        }
    )

    with pytest.raises(AuthorizedSessionError, match="binding cannot change"):
        _connected(registry, room_id="room-b")

    retained = registry.get("authorized-desktop", "desktop-one")
    assert retained["room_id"] == "room-a"
    assert [event["text"] for event in retained["events"]] == ["Room A only"]


def test_authorized_session_id_cannot_be_reused_for_another_conversation(
    tmp_path: Path,
) -> None:
    registry = AuthorizedSessionRegistry(
        _bridge(tmp_path, "agent-one", "adapter-one")
    )
    _connected(registry, source_conversation_id="conversation-one")

    with pytest.raises(AuthorizedSessionError, match="binding cannot change"):
        _connected(registry, source_conversation_id="conversation-two")

    retained = registry.get("authorized-desktop", "desktop-one")
    assert retained["source_conversation_id"] == "conversation-one"


def test_external_session_tools_publish_only_for_the_calling_identity(
    tmp_path: Path,
) -> None:
    owner = _bridge(tmp_path, "owner-agent", "owner-session")
    connected = _call(
        owner,
        "connect_observable_session",
        {
            "source_type": "authorized-terminal",
            "source_session_id": "terminal-one",
            "source_conversation_id": "conversation-one",
            "adapter_id": "terminal-adapter-v1",
            "display_name": "Claude terminal",
            "client_name": "Claude Code",
            "supports_events": True,
            "state": "running",
        },
    )
    assert connected["input_owner"] == "external-terminal"
    published = _call(
        owner,
        "publish_observable_session_event",
        {
            "source_type": "authorized-terminal",
            "source_session_id": "terminal-one",
            "event_id": "terminal-event-one",
            "stream": "stdout",
            "kind": "answer",
            "text": "Observable final answer",
            "state": "completed",
        },
    )
    assert published["sequence"] == 1
    own = _call(owner, "list_own_observable_sessions")
    assert own["count"] == 1
    incremental_own = _call(
        owner,
        "list_own_observable_sessions",
        {"after_sequences": {"authorized-terminal:terminal-one": 1}},
    )
    assert incremental_own["sessions"][0]["events"] == []

    unrelated = _bridge(tmp_path, "other-agent", "other-session")
    assert _call(unrelated, "list_own_observable_sessions")["count"] == 0
    rejected = _call(
        unrelated,
        "close_observable_session",
        {
            "source_type": "authorized-terminal",
            "source_session_id": "terminal-one",
            "state": "stopped",
        },
    )
    assert "bound external adapter" in str(rejected["error"])
