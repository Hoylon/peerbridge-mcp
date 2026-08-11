from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from peerbridge_mcp.bridge import Bridge, BridgeError
from peerbridge_mcp.server import handle_request


def make_bridge(
    root: Path,
    agent: str,
    *,
    session: str | None = None,
    client_name: str | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
) -> Bridge:
    return Bridge(
        root,
        root / ".peerbridge" / "peerbridge.sqlite3",
        agent,
        "test-scope",
        session_id=session,
        client_name=client_name,
        provider_id=provider_id,
        model_id=model_id,
    )


def test_message_cursors_are_per_consumer_and_contiguous(tmp_path: Path) -> None:
    sender = make_bridge(tmp_path, "sender")
    bob = make_bridge(tmp_path, "bob")
    carol = make_bridge(tmp_path, "carol")

    first = sender.send_message(
        {"recipient": "bob", "task_id": "mail", "subject": "one", "body": "direct"}
    )
    second = sender.send_message(
        {"recipient": "*", "task_id": "mail", "subject": "two", "body": "broadcast"}
    )

    assert [m["message_id"] for m in bob.poll_messages({})["messages"]] == [
        first["message_id"],
        second["message_id"],
    ]
    out_of_order = bob.ack_message({"message_id": second["message_id"]})
    assert out_of_order["cursor"] == 0
    contiguous = bob.ack_message({"message_id": first["message_id"]})
    assert contiguous["cursor"] == second["sequence"]
    assert bob.poll_messages({})["messages"] == []

    carol_poll = carol.poll_messages({})
    assert [m["message_id"] for m in carol_poll["messages"]] == [second["message_id"]]
    assert carol.ack_message({"message_id": second["message_id"]})["cursor"] == second["sequence"]


def test_task_leases_block_overlapping_writers_and_allow_expired_recovery(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    first = make_bridge(tmp_path, "first")
    second = make_bridge(tmp_path, "second")
    claim = first.claim_task(
        {"task_id": "alpha", "summary": "first", "write_paths": ["src"], "lease_seconds": 30}
    )

    with pytest.raises(BridgeError, match="conflicts"):
        second.claim_task(
            {"task_id": "beta", "summary": "second", "write_paths": ["src/module.py"]}
        )

    with first._connect() as connection:
        connection.execute(
            "UPDATE tasks SET lease_expires_epoch=? WHERE scope=? AND task_id=?",
            (time.time() - 1, first.scope, "alpha"),
        )
    recovered = second.claim_task(
        {"task_id": "beta", "summary": "second", "write_paths": ["src/module.py"]}
    )
    assert recovered["claimed_by"] == "second"
    assert recovered["lease_token"] != claim["lease_token"]


def test_read_read_scopes_do_not_conflict_but_read_write_does(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    first = make_bridge(tmp_path, "first")
    second = make_bridge(tmp_path, "second")
    third = make_bridge(tmp_path, "third")
    first.claim_task({"task_id": "a", "summary": "read", "read_paths": ["docs"]})
    second.claim_task({"task_id": "b", "summary": "read", "read_paths": ["docs/guide.md"]})
    with pytest.raises(BridgeError, match="conflicts"):
        third.claim_task({"task_id": "c", "summary": "write", "write_paths": ["docs"]})


def test_proof_gate_rehashes_live_files_and_rejects_drift(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    target = source / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    agent = make_bridge(tmp_path, "agent")
    claim = agent.claim_task(
        {
            "task_id": "proof",
            "summary": "proof gate",
            "write_paths": ["src"],
            "approval_mode": "solo_allowed",
        }
    )
    agent.record_proof(
        {
            "task_id": "proof",
            "lease_token": claim["lease_token"],
            "change_summary": "changed module",
            "changed_paths": ["src/module.py"],
            "before_hashes": {},
            "tests": "pytest: pass",
        }
    )
    target.write_text("value = 2\n", encoding="utf-8")
    with pytest.raises(BridgeError, match="drift"):
        agent.complete_task({"task_id": "proof", "lease_token": claim["lease_token"]})


def test_presence_aware_review_requires_online_peer_then_completes(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    target = source / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    owner = make_bridge(tmp_path, "owner")
    peer = make_bridge(tmp_path, "peer")
    owner.touch_presence()
    peer.touch_presence()
    claim = owner.claim_task(
        {
            "task_id": "reviewed",
            "summary": "needs peer",
            "write_paths": ["src"],
            "approval_mode": "presence_aware",
            "required_peer": "peer",
        }
    )
    owner.record_proof(
        {
            "task_id": "reviewed",
            "lease_token": claim["lease_token"],
            "change_summary": "reviewed change",
            "changed_paths": ["src/module.py"],
            "tests": "pytest: pass",
        }
    )
    with pytest.raises(BridgeError, match="review gate"):
        owner.complete_task({"task_id": "reviewed", "lease_token": claim["lease_token"]})
    request = owner.request_review(
        {
            "task_id": "reviewed",
            "lease_token": claim["lease_token"],
            "recipient": "peer",
            "question": "Review the bounded change and test evidence.",
        }
    )
    review = peer.submit_review(
        {
            "request_id": request["request_id"],
            "verdict": "approved",
            "score": 95,
            "findings": "The file and test evidence are internally consistent.",
        }
    )
    completed = owner.complete_task(
        {"task_id": "reviewed", "lease_token": claim["lease_token"]}
    )
    assert completed["status"] == "complete"
    assert review["review_id"] in {
        item["review_id"] for item in completed["review"]["reviews"]
    }


def test_presence_aware_mode_allows_traced_solo_fallback(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("checked\n", encoding="utf-8")
    owner = make_bridge(tmp_path, "owner")
    owner.touch_presence()
    claim = owner.claim_task(
        {
            "task_id": "solo",
            "summary": "offline fallback",
            "approval_mode": "presence_aware",
            "required_peer": "offline-peer",
        }
    )
    owner.record_proof(
        {
            "task_id": "solo",
            "lease_token": claim["lease_token"],
            "change_summary": "read-only evidence",
            "tests": "manual verifier: pass",
            "evidence_paths": ["evidence.txt"],
        }
    )
    result = owner.complete_task({"task_id": "solo", "lease_token": claim["lease_token"]})
    assert result["review"]["policy_reason"] == "peer_offline_solo_fallback"


def test_protected_paths_and_secret_values_fail_closed(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "agent")
    with pytest.raises(BridgeError, match="protected"):
        bridge.claim_task(
            {"task_id": "git", "summary": "bad", "write_paths": [".git/config"]}
        )
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=not-for-bridge\n", encoding="utf-8")
    with pytest.raises(BridgeError, match="protected"):
        bridge.hash_artifact({"path": ".env"})
    with pytest.raises(BridgeError, match="credential"):
        bridge.send_message(
            {
                "recipient": "peer",
                "task_id": "secret",
                "subject": "bad",
                "body": "sk-" + "a" * 40,
            }
        )


def test_event_hash_chain_detects_payload_tampering(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "agent")
    bridge.send_message(
        {"recipient": "peer", "task_id": "audit", "subject": "hello", "body": "world"}
    )
    assert bridge.verify_audit_chain()["valid"] is True
    with bridge._connect() as connection:
        connection.execute(
            "UPDATE events SET payload_json=? WHERE sequence=(SELECT MIN(sequence) FROM events)",
            (json.dumps({"tampered": True}),),
        )
    result = bridge.verify_audit_chain()
    assert result["valid"] is False
    assert any(item["error"] == "payload_sha256" for item in result["errors"])


def test_mcp_initialize_list_and_tool_call(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "agent")
    initialized = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert initialized["result"]["serverInfo"]["name"] == "peerbridge-mcp"
    tools = handle_request(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {item["name"] for item in tools["result"]["tools"]}
    assert {"claim_task", "verify_audit_chain", "complete_task"}.issubset(names)
    called = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {}},
        },
    )
    payload = json.loads(called["result"]["content"][0]["text"])
    assert payload["network_listener"] is False


def test_modern_mcp_discovery_versioning_and_structured_results(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "modern-agent")
    meta = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }
    discovered = handle_request(
        bridge,
        {"jsonrpc": "2.0", "id": "d1", "method": "server/discover", "params": meta},
    )
    assert discovered["result"]["resultType"] == "complete"
    assert "2026-07-28" in discovered["result"]["supportedVersions"]

    listed = handle_request(
        bridge,
        {"jsonrpc": "2.0", "id": "l1", "method": "tools/list", "params": meta},
    )
    assert listed["result"]["resultType"] == "complete"
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in listed["result"]["tools"])

    called = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": "c1",
            "method": "tools/call",
            "params": {**meta, "name": "bridge_status", "arguments": {}},
        },
    )
    assert called["result"]["resultType"] == "complete"
    assert called["result"]["isError"] is False
    assert called["result"]["structuredContent"]["network_listener"] is False


def test_modern_mcp_errors_are_protocol_or_recoverable_tool_results(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "modern-agent")
    unsupported = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {
                "_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}
            },
        },
    )
    assert unsupported["error"]["code"] == -32022
    assert unsupported["error"]["data"]["requested"] == "1900-01-01"

    meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
    unknown = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {**meta, "name": "does_not_exist", "arguments": {}},
        },
    )
    assert unknown["error"]["code"] == -32602

    recoverable = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {**meta, "name": "ack_message", "arguments": {}},
        },
    )
    assert recoverable["result"]["isError"] is True
    assert recoverable["result"]["resultType"] == "complete"


def test_multi_agent_quorum_requires_configured_number_of_reviews(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    owner = make_bridge(tmp_path, "codex")
    peers = {name: make_bridge(tmp_path, name) for name in ("grok", "deepseek", "kimi")}
    owner.touch_presence()
    for peer in peers.values():
        peer.touch_presence()

    claim = owner.claim_task(
        {
            "task_id": "multi-agent",
            "summary": "Require two independent peers",
            "write_paths": ["module.py"],
            "approval_mode": "quorum_required",
            "required_peers": list(peers),
            "review_quorum": 2,
        }
    )
    owner.record_proof(
        {
            "task_id": "multi-agent",
            "lease_token": claim["lease_token"],
            "change_summary": "Bounded synthetic change",
            "changed_paths": ["module.py"],
            "tests": "synthetic check: pass",
        }
    )
    requests = {
        name: owner.request_review(
            {
                "task_id": "multi-agent",
                "lease_token": claim["lease_token"],
                "recipient": name,
                "question": "Review the synthetic proof independently.",
            }
        )
        for name in peers
    }
    peers["grok"].submit_review(
        {
            "request_id": requests["grok"]["request_id"],
            "verdict": "approved",
            "score": 90,
            "findings": "Synthetic evidence and live hash agree.",
        }
    )
    with pytest.raises(BridgeError, match="review gate"):
        owner.complete_task({"task_id": "multi-agent", "lease_token": claim["lease_token"]})

    peers["deepseek"].submit_review(
        {
            "request_id": requests["deepseek"]["request_id"],
            "verdict": "approved",
            "score": 92,
            "findings": "Independent synthetic review also passes.",
        }
    )
    completed = owner.complete_task(
        {"task_id": "multi-agent", "lease_token": claim["lease_token"]}
    )
    assert completed["review"]["policy_reason"] == "review_quorum_met"
    assert completed["review"]["review_quorum"] == 2
    assert completed["review"]["required_peers"] == ["deepseek", "grok", "kimi"]


def test_runtime_identity_distinguishes_official_and_relay_routes(tmp_path: Path) -> None:
    official = make_bridge(
        tmp_path,
        "grok-official-session",
        session="official-session",
        client_name="browser-adapter",
        provider_id="xai-official-web",
        model_id="grok",
    )
    relay = make_bridge(
        tmp_path,
        "grok-relay-session",
        session="relay-session",
        client_name="relay-coding-client",
        provider_id="relay:grok-official-channel",
        model_id="grok",
    )
    official.touch_presence()
    relay.touch_presence()

    sessions = {
        row["agent_id"]: row for row in official.presence_snapshot()["online_sessions"]
    }
    assert sessions["grok-official-session"]["provider_id"] == "xai-official-web"
    assert sessions["grok-relay-session"]["provider_id"] == "relay:grok-official-channel"
    assert official.status()["runtime_identity"] == {
        "client_name": "browser-adapter",
        "provider_id": "xai-official-web",
        "model_id": "grok",
    }


def test_schema_v1_database_migrates_additively_to_v3(tmp_path: Path) -> None:
    state = tmp_path / ".peerbridge"
    state.mkdir()
    db = state / "bridge.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '1');
            CREATE TABLE tasks (
                scope TEXT NOT NULL,
                task_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                owner TEXT,
                status TEXT NOT NULL,
                claimed_by TEXT,
                claimed_session_id TEXT,
                lease_token_sha256 TEXT,
                lease_expires_epoch REAL,
                claimed_utc TEXT,
                created_utc TEXT NOT NULL,
                updated_utc TEXT NOT NULL,
                task_sha256 TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                required_peer TEXT,
                PRIMARY KEY(scope, task_id)
            );
            """
        )
    Bridge(tmp_path, db, "migration-agent", "test")
    with sqlite3.connect(db) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        presence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_presence)")
        }
    assert version == "3"
    assert "review_quorum" in task_columns
    assert {"client_name", "provider_id", "model_id"}.issubset(presence_columns)


def test_schema_v2_presence_migrates_additively_to_v3(tmp_path: Path) -> None:
    state = tmp_path / ".peerbridge"
    state.mkdir()
    db = state / "bridge.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '2');
            CREATE TABLE agent_presence (
                scope TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                transport TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                last_seen_epoch REAL NOT NULL,
                PRIMARY KEY(scope, agent_id, session_id)
            );
            INSERT INTO agent_presence VALUES (
                'test', 'legacy-agent', 'legacy-session', 'stdio',
                '2026-08-11T00:00:00Z', 1786406400
            );
            """
        )

    bridge = Bridge(
        tmp_path,
        db,
        "migration-agent",
        "test",
        client_name="codex",
        provider_id="openai-official",
        model_id="gpt",
    )
    bridge.touch_presence()

    with sqlite3.connect(db) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        presence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_presence)")
        }
        legacy = connection.execute(
            """SELECT client_name, provider_id, model_id
               FROM agent_presence WHERE agent_id='legacy-agent'"""
        ).fetchone()
        current = connection.execute(
            """SELECT client_name, provider_id, model_id
               FROM agent_presence WHERE agent_id='migration-agent'"""
        ).fetchone()

    assert version == "3"
    assert {"client_name", "provider_id", "model_id"}.issubset(presence_columns)
    assert legacy == (None, None, None)
    assert current == ("codex", "openai-official", "gpt")
