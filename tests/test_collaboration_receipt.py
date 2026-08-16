from __future__ import annotations

import json
from pathlib import Path

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.collaboration_receipt import (
    CHILD_RECEIPT_VERIFIERS,
    RECEIPT_SCHEMA_V1,
    RECEIPT_SCHEMA_V2,
    RECEIPT_SCHEMA_V3,
    capture_collaboration_receipt,
    verify_collaboration_receipt,
)
from peerbridge_mcp.bridge import stable_sha256


def _build_chain(tmp_path: Path) -> tuple[Path, list[dict], dict[str, Path]]:
    db = tmp_path / "bridge.sqlite3"
    scope = "chain-test"
    task_id = "chain-task"
    codex = Bridge(tmp_path, db, "codex", scope, session_id="codex-session")
    claude = Bridge(
        tmp_path,
        db,
        "claude",
        scope,
        session_id="claude-session",
        client_name="claude-acp",
        provider_id="relay:test",
        model_id="sonnet",
        route_class="relay",
    )
    grok = Bridge(
        tmp_path,
        db,
        "grok",
        scope,
        session_id="grok-session",
        client_name="grok-acp",
        provider_id="official:grok",
        model_id="grok-test",
        route_class="official",
    )
    operator = Bridge(
        tmp_path,
        db,
        "human-operator",
        scope,
        session_id="operator-session",
    )
    operator.upsert_route_profile(
        {
            "route_id": "claude-route",
            "agent_id": "claude",
            "provider_id": "relay:test",
            "model_id": "sonnet",
            "route_class": "relay",
        }
    )
    operator.upsert_route_profile(
        {
            "route_id": "grok-route",
            "agent_id": "grok",
            "provider_id": "official:grok",
            "model_id": "grok-test",
            "route_class": "official",
        }
    )
    first = codex.send_message(
        {
            "recipient": "claude",
            "task_id": task_id,
            "subject": "STEP_1",
            "body": "one",
            "route_profile_id": "claude-route",
        }
    )
    claude.ack_message({"message_id": first["message_id"], "agent_id": "claude"})
    second = claude.send_message(
        {
            "recipient": "grok",
            "task_id": task_id,
            "subject": "STEP_2",
            "body": "two",
            "route_profile_id": "grok-route",
        }
    )
    grok.ack_message({"message_id": second["message_id"], "agent_id": "grok"})
    third = grok.send_message(
        {
            "recipient": "codex",
            "task_id": task_id,
            "subject": "STEP_3",
            "body": "three",
            "reply_to": second["message_id"],
        }
    )
    codex.ack_message({"message_id": third["message_id"], "agent_id": "codex"})
    evidence = tmp_path / "acp-evidence.ndjson"
    evidence.write_text('{"safe":"evidence"}\n', encoding="utf-8")
    expected = [
        {
            "sender": "codex",
            "recipient": "claude",
            "subject": "STEP_1",
            "body": "one",
            "route": {
                "requested_provider_id": "relay:test",
                "requested_model_id": "sonnet",
                "requested_reasoning_mode": None,
                "requested_route_class": "relay",
            },
        },
        {
            "sender": "claude",
            "recipient": "grok",
            "subject": "STEP_2",
            "body": "two",
            "route": {
                "requested_provider_id": "official:grok",
                "requested_model_id": "grok-test",
                "requested_reasoning_mode": None,
                "requested_route_class": "official",
            },
        },
        {
            "sender": "grok",
            "recipient": "codex",
            "subject": "STEP_3",
            "body": "three",
            "reply_to_step": 1,
        },
    ]
    return db, expected, {"provider_transcript": evidence}


def test_collaboration_receipt_binds_chain_and_allows_append_only_events(
    tmp_path: Path,
) -> None:
    db, expected, evidence = _build_chain(tmp_path)
    receipt = capture_collaboration_receipt(
        db_path=db,
        scope="chain-test",
        task_id="chain-task",
        expected_chain=expected,
        evidence_paths=evidence,
        receipt_schema=RECEIPT_SCHEMA_V2,
    )
    assert receipt["schema"] == RECEIPT_SCHEMA_V2
    assert receipt["room_id"] == "lobby"
    assert {item["content_hash_contract"] for item in receipt["messages"]} == {
        "room-bound-v2"
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    before = {item: (item.stat().st_size, item.stat().st_mtime_ns) for item in (db, path)}
    assert verify_collaboration_receipt(path)["valid"] is True
    after = {item: (item.stat().st_size, item.stat().st_mtime_ns) for item in before}
    assert before == after
    Bridge(tmp_path, db, "later", "chain-test").send_message(
        {
            "recipient": "nobody",
            "task_id": "later-task",
            "subject": "LATER",
            "body": "append-only",
        }
    )
    assert verify_collaboration_receipt(path)["valid"] is True


def test_legacy_v1_receipt_contract_remains_verifiable(tmp_path: Path) -> None:
    db, expected, evidence = _build_chain(tmp_path)
    receipt = capture_collaboration_receipt(
        db_path=db,
        scope="chain-test",
        task_id="chain-task",
        expected_chain=expected,
        evidence_paths=evidence,
        receipt_schema=RECEIPT_SCHEMA_V1,
    )
    assert receipt["schema"] == RECEIPT_SCHEMA_V1
    assert "room_id" not in receipt
    assert all("content_hash_contract" not in item for item in receipt["messages"])
    path = tmp_path / "legacy-receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert verify_collaboration_receipt(path)["valid"] is True


def test_v2_receipt_rejects_cross_room_task_chain(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    scope = "room-chain"
    sender = Bridge(tmp_path, db, "human", scope)
    sender.create_room({"room_id": "alpha", "name": "Alpha"})
    sender.create_room({"room_id": "beta", "name": "Beta"})
    sender.join_room({"room_id": "alpha", "agent_id": "peer"})
    sender.join_room({"room_id": "beta", "agent_id": "peer"})
    peer = Bridge(tmp_path, db, "peer", scope)
    first = sender.send_message(
        {
            "room_id": "alpha",
            "recipient": "peer",
            "task_id": "shared-task",
            "subject": "ALPHA",
            "body": "one",
        }
    )
    peer.ack_message({"message_id": first["message_id"]})
    second = sender.send_message(
        {
            "room_id": "beta",
            "recipient": "peer",
            "task_id": "shared-task",
            "subject": "BETA",
            "body": "two",
        }
    )
    peer.ack_message({"message_id": second["message_id"]})
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text('{"safe":true}\n', encoding="utf-8")
    try:
        capture_collaboration_receipt(
            db_path=db,
            scope=scope,
            task_id="shared-task",
            expected_chain=[
                {
                    "room_id": "alpha",
                    "sender": "human",
                    "recipient": "peer",
                    "subject": "ALPHA",
                    "body": "one",
                },
                {
                    "room_id": "beta",
                    "sender": "human",
                    "recipient": "peer",
                    "subject": "BETA",
                    "body": "two",
                },
            ],
            evidence_paths={"provider": evidence_path},
        )
    except RuntimeError as exc:
        assert "one room" in str(exc)
    else:
        raise AssertionError("cross-room chain was accepted")


def test_collaboration_receipt_fails_closed_after_evidence_drift(tmp_path: Path) -> None:
    db, expected, evidence = _build_chain(tmp_path)
    receipt = capture_collaboration_receipt(
        db_path=db,
        scope="chain-test",
        task_id="chain-task",
        expected_chain=expected,
        evidence_paths=evidence,
        receipt_schema=RECEIPT_SCHEMA_V2,
    )
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    evidence["provider_transcript"].write_text("drift\n", encoding="utf-8")
    verified = verify_collaboration_receipt(path)
    assert verified["valid"] is False
    assert any("evidence" in error for error in verified["errors"])


def test_collaboration_receipt_rejects_secret_bearing_evidence(tmp_path: Path) -> None:
    db, expected, evidence = _build_chain(tmp_path)
    evidence["provider_transcript"].write_text(
        "Bear" + "er " + "abcdefghijklmnopqrstuvwxyz123456\n", encoding="utf-8"
    )
    try:
        capture_collaboration_receipt(
            db_path=db,
            scope="chain-test",
            task_id="chain-task",
            expected_chain=expected,
            evidence_paths=evidence,
            receipt_schema=RECEIPT_SCHEMA_V2,
        )
    except RuntimeError as exc:
        assert "credential" in str(exc)
    else:
        raise AssertionError("credential-bearing evidence was accepted")


def _child_receipts(tmp_path: Path) -> dict[str, Path]:
    identities = {
        "codex": {
            "client_name": "codex-cli",
            "provider_id": "openai:test",
            "model_id": "codex-test",
            "reasoning_mode": None,
            "route_class": None,
        },
        "claude": {
            "client_name": "claude-acp",
            "provider_id": "relay:test",
            "model_id": "sonnet",
            "reasoning_mode": None,
            "route_class": "relay",
        },
        "grok": {
            "client_name": "grok-acp",
            "provider_id": "official:grok",
            "model_id": "grok-test",
            "reasoning_mode": None,
            "route_class": "official",
        },
    }
    paths: dict[str, Path] = {}
    for agent_id, identity in identities.items():
        receipt = {
            "schema": "test.child-receipt.v1",
            "bridge": {
                "scope": "chain-test",
                "agent_id": agent_id,
                "runtime_identity": identity,
            },
        }
        receipt["receipt_sha256"] = stable_sha256(receipt)
        path = tmp_path / f"{agent_id}-child.json"
        path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        paths[agent_id] = path
    return paths


def _install_test_child_verifier(monkeypatch) -> None:
    def verify(path: Path) -> dict:
        value = json.loads(path.read_text(encoding="utf-8"))
        unsigned = dict(value)
        receipt_sha = unsigned.pop("receipt_sha256", "")
        valid = stable_sha256(unsigned) == receipt_sha
        return {
            "valid": valid,
            "receipt_sha256": receipt_sha,
            "errors": [] if valid else ["receipt_sha256"],
            "writes_performed": 0,
        }

    monkeypatch.setitem(CHILD_RECEIPT_VERIFIERS, "test.child-receipt.v1", verify)


def test_v3_requires_verified_child_receipt_for_every_participant(
    tmp_path: Path, monkeypatch
) -> None:
    _install_test_child_verifier(monkeypatch)
    db, expected, evidence = _build_chain(tmp_path)
    children = _child_receipts(tmp_path)
    receipt = capture_collaboration_receipt(
        db_path=db,
        scope="chain-test",
        task_id="chain-task",
        expected_chain=expected,
        evidence_paths=evidence,
        child_receipt_paths=children,
        receipt_schema=RECEIPT_SCHEMA_V3,
    )
    assert receipt["schema"] == RECEIPT_SCHEMA_V3
    assert receipt["all_participants_independently_verified"] is True
    assert receipt["participant_agents"] == ["claude", "codex", "grok"]
    assert set(receipt["child_receipts"]) == set(children)
    receipt_path = tmp_path / "v3-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (db, receipt_path, *children.values())
    }
    assert verify_collaboration_receipt(receipt_path)["valid"] is True
    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns) for path in before
    }
    assert before == after


def test_v3_rejects_missing_or_duplicate_participant_child(
    tmp_path: Path, monkeypatch
) -> None:
    _install_test_child_verifier(monkeypatch)
    db, expected, evidence = _build_chain(tmp_path)
    children = _child_receipts(tmp_path)
    children.pop("codex")
    try:
        capture_collaboration_receipt(
            db_path=db,
            scope="chain-test",
            task_id="chain-task",
            expected_chain=expected,
            evidence_paths=evidence,
            child_receipt_paths=children,
            receipt_schema=RECEIPT_SCHEMA_V3,
        )
    except RuntimeError as exc:
        assert "participant mismatch" in str(exc)
    else:
        raise AssertionError("v3 accepted an incomplete participant receipt set")


def test_v3_rejects_child_route_identity_mismatch(tmp_path: Path, monkeypatch) -> None:
    _install_test_child_verifier(monkeypatch)
    db, expected, evidence = _build_chain(tmp_path)
    children = _child_receipts(tmp_path)
    claude = json.loads(children["claude"].read_text(encoding="utf-8"))
    claude["bridge"]["runtime_identity"]["model_id"] = "wrong-model"
    claude.pop("receipt_sha256")
    claude["receipt_sha256"] = stable_sha256(claude)
    children["claude"].write_text(json.dumps(claude), encoding="utf-8")
    try:
        capture_collaboration_receipt(
            db_path=db,
            scope="chain-test",
            task_id="chain-task",
            expected_chain=expected,
            evidence_paths=evidence,
            child_receipt_paths=children,
            receipt_schema=RECEIPT_SCHEMA_V3,
        )
    except RuntimeError as exc:
        assert "runtime identity" in str(exc)
    else:
        raise AssertionError("v3 accepted a mismatched route identity")


def test_v3_fails_after_child_receipt_drift(tmp_path: Path, monkeypatch) -> None:
    _install_test_child_verifier(monkeypatch)
    db, expected, evidence = _build_chain(tmp_path)
    children = _child_receipts(tmp_path)
    receipt = capture_collaboration_receipt(
        db_path=db,
        scope="chain-test",
        task_id="chain-task",
        expected_chain=expected,
        evidence_paths=evidence,
        child_receipt_paths=children,
        receipt_schema=RECEIPT_SCHEMA_V3,
    )
    receipt_path = tmp_path / "v3-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    children["grok"].write_text("{}", encoding="utf-8")
    verified = verify_collaboration_receipt(receipt_path)
    assert verified["valid"] is False
    assert any("child receipt" in error for error in verified["errors"])
