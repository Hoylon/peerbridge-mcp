"""Append-safe receipts for real multi-provider PeerBridge message chains."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .bridge import DEFAULT_ROOM_ID, sha256_bytes, stable_sha256, utc_now
from .secret_scan import contains_secret
from .acp_client_receipt import (
    RECEIPT_SCHEMA as ACP_CLIENT_RECEIPT_SCHEMA,
    verify_receipt as verify_acp_client_receipt,
)
from .claude_client_receipt import (
    RECEIPT_SCHEMA as CLAUDE_CLIENT_RECEIPT_SCHEMA,
    verify_receipt as verify_claude_client_receipt,
)
from .mcp_client_receipt import (
    RECEIPT_SCHEMA as MCP_CLIENT_RECEIPT_SCHEMA,
    verify_receipt as verify_mcp_client_receipt,
)
from .provider_receipt import (
    RECEIPT_SCHEMA as PROVIDER_RECEIPT_SCHEMA,
    ReceiptError,
    _event_snapshot,
    _file_sha256,
    _read_json,
    _readonly_connection,
    _verify_chain_prefix,
    verify_receipt as verify_provider_receipt,
)


RECEIPT_SCHEMA_V1 = "peerbridge.collaboration-chain-receipt.v1"
RECEIPT_SCHEMA_V2 = "peerbridge.collaboration-chain-receipt.v2"
RECEIPT_SCHEMA_V3 = "peerbridge.collaboration-chain-receipt.v3"
RECEIPT_SCHEMA = RECEIPT_SCHEMA_V3
SUPPORTED_RECEIPT_SCHEMAS = {
    RECEIPT_SCHEMA_V1,
    RECEIPT_SCHEMA_V2,
    RECEIPT_SCHEMA_V3,
}
CHILD_RECEIPT_VERIFIERS = {
    ACP_CLIENT_RECEIPT_SCHEMA: verify_acp_client_receipt,
    CLAUDE_CLIENT_RECEIPT_SCHEMA: verify_claude_client_receipt,
    MCP_CLIENT_RECEIPT_SCHEMA: verify_mcp_client_receipt,
    PROVIDER_RECEIPT_SCHEMA: verify_provider_receipt,
}


def _route_request(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any] | None:
    if not row["route_request_sha256"]:
        return None
    route = {
        "route_profile_id": row["route_profile_id"],
        "target_agent_id": row["recipient"],
        "requested_provider_id": row["requested_provider_id"],
        "requested_model_id": row["requested_model_id"],
        "requested_reasoning_mode": row["requested_reasoning_mode"],
        "route_request_sha256": row["route_request_sha256"],
    }
    keys = set(row.keys()) if isinstance(row, sqlite3.Row) else set(row)
    if "requested_route_class" in keys and row["requested_route_class"] is not None:
        route["requested_route_class"] = row["requested_route_class"]
    return route


def _message_snapshot(
    row: sqlite3.Row | dict[str, Any], receipt_schema: str
) -> dict[str, Any]:
    artifacts = json.loads(row["artifact_paths_json"])
    route = _route_request(row)
    legacy_content = {
        "message_id": row["message_id"],
        "scope": row["scope"],
        "task_id": row["task_id"],
        "sender": row["sender"],
        "recipient": row["recipient"],
        "subject": row["subject"],
        "body": row["body"],
        "priority": row["priority"],
        "reply_to": row["reply_to"],
        "artifact_paths": artifacts,
        "route_request": route,
        "created_utc": row["created_utc"],
    }
    keys = set(row.keys()) if isinstance(row, sqlite3.Row) else set(row)
    room_id = str(row["room_id"] or DEFAULT_ROOM_ID) if "room_id" in keys else DEFAULT_ROOM_ID
    room_bound_content = {
        "message_id": row["message_id"],
        "scope": row["scope"],
        "room_id": room_id,
        **{key: value for key, value in legacy_content.items() if key not in {"message_id", "scope"}},
    }
    discussion_bound_content = {
        **{
            key: value
            for key, value in room_bound_content.items()
            if key != "created_utc"
        },
        "discussion_id": row["discussion_id"] if "discussion_id" in keys else None,
        "discussion_round": (
            row["discussion_round"] if "discussion_round" in keys else None
        ),
        "discussion_role": row["discussion_role"] if "discussion_role" in keys else None,
        "created_utc": row["created_utc"],
    }
    visibility = str(row["visibility"] or "direct") if "visibility" in keys else "direct"
    room_visibility_content = {
        **{key: value for key, value in room_bound_content.items() if key != "created_utc"},
        "visibility": visibility,
        "created_utc": row["created_utc"],
    }
    discussion_visibility_content = {
        **{
            key: value
            for key, value in discussion_bound_content.items()
            if key != "created_utc"
        },
        "visibility": visibility,
        "created_utc": row["created_utc"],
    }
    observed_sha = str(row["content_sha256"])
    if stable_sha256(discussion_visibility_content) == observed_sha:
        hash_contract = "discussion-visibility-bound-v4"
    elif stable_sha256(room_visibility_content) == observed_sha:
        hash_contract = "room-visibility-bound-v4"
    elif stable_sha256(discussion_bound_content) == observed_sha:
        hash_contract = "discussion-bound-v3"
    elif stable_sha256(room_bound_content) == observed_sha:
        hash_contract = "room-bound-v2"
    elif stable_sha256(legacy_content) == observed_sha:
        hash_contract = "legacy-v1"
    else:
        raise ReceiptError(f"message {row['message_id']} content SHA mismatch")
    snapshot = {
        "sequence": row["sequence"],
        "message_id": row["message_id"],
        "sender": row["sender"],
        "recipient": row["recipient"],
        "subject": row["subject"],
        "body_sha256": sha256_bytes(row["body"].encode("utf-8")),
        "priority": row["priority"],
        "reply_to": row["reply_to"],
        "artifact_paths_sha256": stable_sha256(artifacts),
        "route_request": route,
        "created_utc": row["created_utc"],
        "acknowledged_utc": row["acknowledged_utc"],
        "content_sha256": row["content_sha256"],
    }
    if receipt_schema in {RECEIPT_SCHEMA_V2, RECEIPT_SCHEMA_V3}:
        snapshot["room_id"] = room_id
        snapshot["visibility"] = visibility
        snapshot["content_hash_contract"] = hash_contract
    return snapshot


def _message_receipt_snapshot(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": row["scope"],
        "message_id": row["message_id"],
        "agent_id": row["agent_id"],
        "acknowledged_utc": row["acknowledged_utc"],
    }


def _route_receipt_snapshot(
    row: sqlite3.Row | dict[str, Any], route_request_sha256: str
) -> dict[str, Any]:
    content = {
        "scope": row["scope"],
        "message_id": row["message_id"],
        "agent_id": row["agent_id"],
        "session_id": row["session_id"],
        "observed_provider_id": row["observed_provider_id"],
        "observed_model_id": row["observed_model_id"],
        "observed_reasoning_mode": row["observed_reasoning_mode"],
        "route_status": row["route_status"],
        "acknowledged_utc": row["acknowledged_utc"],
        "route_request_sha256": route_request_sha256,
    }
    keys = set(row.keys()) if isinstance(row, sqlite3.Row) else set(row)
    if "observed_route_class" in keys and row["observed_route_class"] is not None:
        content["observed_route_class"] = row["observed_route_class"]
    if stable_sha256(content) != row["receipt_sha256"]:
        raise ReceiptError(f"route receipt {row['message_id']} SHA mismatch")
    return {**content, "receipt_sha256": row["receipt_sha256"]}


def _safe_evidence(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReceiptError(f"evidence file is absent: {path}")
    data = path.read_bytes()
    texts = [data.decode("utf-8", errors="ignore")]
    if data.count(b"\0") > len(data) // 10:
        texts.append(data.decode("utf-16", errors="ignore"))
    if any(contains_secret(text) for text in texts):
        raise ReceiptError(f"evidence appears to contain a credential: {path}")
    return {
        "path": str(path.resolve()),
        "size_bytes": len(data),
        "sha256": _file_sha256(path),
    }


def _expected_snapshot(expected: dict[str, Any], receipt_schema: str) -> dict[str, Any]:
    required = ("sender", "recipient", "subject")
    missing = [key for key in required if key not in expected]
    if missing:
        raise ReceiptError(f"expected chain step lacks: {', '.join(missing)}")
    if "body" in expected:
        body_sha256 = sha256_bytes(str(expected["body"]).encode("utf-8"))
    else:
        body_sha256 = str(expected.get("body_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", body_sha256):
            raise ReceiptError("expected chain step requires body or body_sha256")
    route = expected.get("route")
    if route is not None and not isinstance(route, dict):
        raise ReceiptError("expected route must be an object")
    snapshot = {
        "sender": str(expected["sender"]),
        "recipient": str(expected["recipient"]),
        "subject": str(expected["subject"]),
        "body_sha256": body_sha256,
        "reply_to_step": expected.get("reply_to_step"),
        "route": route,
    }
    if receipt_schema in {RECEIPT_SCHEMA_V2, RECEIPT_SCHEMA_V3}:
        snapshot["room_id"] = str(expected.get("room_id") or DEFAULT_ROOM_ID)
    return snapshot


def _child_receipt_snapshot(path: Path) -> dict[str, Any]:
    """Verify one immutable child receipt and expose only coordination identity."""
    if not path.is_file():
        raise ReceiptError(f"child receipt is absent: {path}")
    child = _read_json(path)
    schema = str(child.get("schema") or "")
    verifier = CHILD_RECEIPT_VERIFIERS.get(schema)
    if verifier is None:
        raise ReceiptError(f"unsupported child receipt schema: {schema or '<missing>'}")
    verification = verifier(path)
    if verification.get("writes_performed") != 0:
        raise ReceiptError("child receipt verifier did not prove zero writes")
    if not verification.get("valid"):
        errors = ", ".join(str(item) for item in verification.get("errors") or [])
        raise ReceiptError(f"child receipt verification failed: {errors or 'unknown error'}")
    bridge = child.get("bridge") or {}
    runtime_identity = bridge.get("runtime_identity")
    if not isinstance(runtime_identity, dict):
        raise ReceiptError("child receipt lacks a runtime identity")
    agent_id = str(bridge.get("agent_id") or "")
    scope = str(bridge.get("scope") or "")
    child_sha = str(child.get("receipt_sha256") or "")
    if not agent_id or not scope or not re.fullmatch(r"[0-9a-f]{64}", child_sha):
        raise ReceiptError("child receipt lacks a bound agent, scope, or receipt SHA")
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "file_sha256": _file_sha256(path),
        "schema": schema,
        "receipt_sha256": child_sha,
        "agent_id": agent_id,
        "scope": scope,
        "runtime_identity": runtime_identity,
        "identity_strength": (
            "provider-acp-attested"
            if schema == PROVIDER_RECEIPT_SCHEMA
            else "direct-acp-client-observed"
            if schema == ACP_CLIENT_RECEIPT_SCHEMA
            else "native-claude-client-observed"
            if schema == CLAUDE_CLIENT_RECEIPT_SCHEMA
            else "mcp-client-observed"
        ),
        "verification_writes_performed": 0,
    }


def _verified_child_receipts(
    paths: dict[str, Path],
    *,
    participants: set[str],
    scope: str,
    route_receipts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    snapshots = {
        label: _child_receipt_snapshot(path)
        for label, path in sorted(paths.items())
    }
    agents = [item["agent_id"] for item in snapshots.values()]
    if len(agents) != len(set(agents)):
        raise ReceiptError("child receipts contain duplicate agent identities")
    if set(agents) != participants:
        missing = sorted(participants - set(agents))
        extra = sorted(set(agents) - participants)
        raise ReceiptError(
            "child receipt participant mismatch: "
            f"missing={missing or []}, extra={extra or []}"
        )
    by_agent = {item["agent_id"]: item for item in snapshots.values()}
    for item in snapshots.values():
        if item["scope"] != scope:
            raise ReceiptError(f"child receipt scope mismatch for {item['agent_id']}")
    for route in route_receipts:
        child_identity = by_agent[route["agent_id"]]["runtime_identity"]
        expected = {
            "provider_id": route["observed_provider_id"],
            "model_id": route["observed_model_id"],
            "reasoning_mode": route["observed_reasoning_mode"],
        }
        if route.get("observed_route_class") is not None:
            expected["route_class"] = route["observed_route_class"]
        observed = {key: child_identity.get(key) for key in expected}
        if observed != expected:
            raise ReceiptError(
                f"child runtime identity does not match route receipt for {route['agent_id']}"
            )
    return snapshots


def _assert_step(
    snapshot: dict[str, Any], expected: dict[str, Any], prior: list[dict[str, Any]]
) -> None:
    for key in ("sender", "recipient", "subject", "body_sha256"):
        if snapshot[key] != expected[key]:
            raise ReceiptError(f"message step mismatch: {key}")
    if "room_id" in expected and snapshot.get("room_id") != expected["room_id"]:
        raise ReceiptError("message step mismatch: room_id")
    reply_step = expected.get("reply_to_step")
    expected_reply = None if reply_step is None else prior[int(reply_step)]["message_id"]
    if snapshot["reply_to"] != expected_reply:
        raise ReceiptError("message step mismatch: reply_to")
    expected_route = expected.get("route")
    actual_route = snapshot.get("route_request")
    if expected_route is None:
        if actual_route is not None:
            raise ReceiptError("unexpected routed message")
        return
    if actual_route is None:
        raise ReceiptError("expected routed message has no route request")
    for key in (
        "requested_provider_id",
        "requested_model_id",
        "requested_reasoning_mode",
        "requested_route_class",
    ):
        if expected_route.get(key) != actual_route.get(key):
            raise ReceiptError(f"message route mismatch: {key}")


def _final_ack_event(
    connection: sqlite3.Connection, scope: str, message_id: str
) -> sqlite3.Row:
    rows = connection.execute(
        "SELECT * FROM events WHERE scope=? AND event_type='message.acknowledged' "
        "ORDER BY sequence DESC",
        (scope,),
    ).fetchall()
    for row in rows:
        if json.loads(row["payload_json"]).get("message_id") == message_id:
            return row
    raise ReceiptError("final message acknowledgement event is absent")


def capture_collaboration_receipt(
    *,
    db_path: Path,
    scope: str,
    task_id: str,
    expected_chain: list[dict[str, Any]],
    evidence_paths: dict[str, Path],
    child_receipt_paths: dict[str, Path] | None = None,
    receipt_schema: str = RECEIPT_SCHEMA,
    room_id: str | None = None,
) -> dict[str, Any]:
    if receipt_schema not in SUPPORTED_RECEIPT_SCHEMAS:
        raise ReceiptError(f"unsupported collaboration receipt schema: {receipt_schema}")
    expected = [_expected_snapshot(step, receipt_schema) for step in expected_chain]
    if len(expected) < 2:
        raise ReceiptError("a collaboration chain requires at least two messages")
    resolved_room_id: str | None = None
    if receipt_schema in {RECEIPT_SCHEMA_V2, RECEIPT_SCHEMA_V3}:
        expected_rooms = {str(step["room_id"]) for step in expected}
        if len(expected_rooms) != 1:
            raise ReceiptError("a collaboration chain must remain inside one room")
        resolved_room_id = next(iter(expected_rooms))
        if room_id is not None and str(room_id) != resolved_room_id:
            raise ReceiptError("receipt room_id does not match the expected chain")
    with _readonly_connection(db_path) as connection:
        if receipt_schema in {RECEIPT_SCHEMA_V2, RECEIPT_SCHEMA_V3}:
            rows = connection.execute(
                "SELECT * FROM messages WHERE scope=? AND room_id=? AND task_id=? "
                "ORDER BY sequence ASC",
                (scope, resolved_room_id, task_id),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM messages WHERE scope=? AND task_id=? ORDER BY sequence ASC",
                (scope, task_id),
            ).fetchall()
        if len(rows) != len(expected):
            raise ReceiptError(
                f"expected {len(expected)} task messages, found {len(rows)}"
            )
        messages: list[dict[str, Any]] = []
        message_receipts: list[dict[str, Any]] = []
        route_receipts: list[dict[str, Any]] = []
        for row, expected_step in zip(rows, expected, strict=True):
            snapshot = _message_snapshot(row, receipt_schema)
            _assert_step(snapshot, expected_step, messages)
            if not snapshot["acknowledged_utc"]:
                raise ReceiptError(f"message {snapshot['message_id']} is not acknowledged")
            receipt_rows = connection.execute(
                "SELECT * FROM message_receipts WHERE scope=? AND message_id=?",
                (scope, snapshot["message_id"]),
            ).fetchall()
            if len(receipt_rows) != 1:
                raise ReceiptError("message requires exactly one recipient receipt")
            message_receipt = _message_receipt_snapshot(receipt_rows[0])
            if message_receipt["agent_id"] != snapshot["recipient"]:
                raise ReceiptError("message receipt recipient mismatch")
            if message_receipt["acknowledged_utc"] != snapshot["acknowledged_utc"]:
                raise ReceiptError("message acknowledgement timestamp mismatch")
            route = snapshot["route_request"]
            route_rows = connection.execute(
                "SELECT * FROM message_route_receipts WHERE scope=? AND message_id=?",
                (scope, snapshot["message_id"]),
            ).fetchall()
            if route is None:
                if route_rows:
                    raise ReceiptError("unrouted message has a route receipt")
            else:
                if len(route_rows) != 1:
                    raise ReceiptError("routed message requires exactly one route receipt")
                route_receipt = _route_receipt_snapshot(
                    route_rows[0], route["route_request_sha256"]
                )
                if route_receipt["agent_id"] != snapshot["recipient"]:
                    raise ReceiptError("route receipt recipient mismatch")
                if route_receipt["route_status"] != "verified":
                    raise ReceiptError("route receipt is not verified")
                route_receipts.append(route_receipt)
            messages.append(snapshot)
            message_receipts.append(message_receipt)
        final_ack = _final_ack_event(connection, scope, messages[-1]["message_id"])
        chain_rows = _verify_chain_prefix(connection, scope, int(final_ack["sequence"]))
    evidence = {
        label: _safe_evidence(path)
        for label, path in sorted(evidence_paths.items())
    }
    participant_agents = {
        str(message[role])
        for message in messages
        for role in ("sender", "recipient")
    }
    child_receipts: dict[str, dict[str, Any]] = {}
    if receipt_schema == RECEIPT_SCHEMA_V3:
        if not child_receipt_paths:
            raise ReceiptError("v3 collaboration receipt requires one child receipt per agent")
        child_receipts = _verified_child_receipts(
            child_receipt_paths,
            participants=participant_agents,
            scope=scope,
            route_receipts=route_receipts,
        )
    elif child_receipt_paths:
        raise ReceiptError("child receipts require collaboration receipt schema v3")
    receipt: dict[str, Any] = {
        "schema": receipt_schema,
        "created_utc": utc_now(),
        "database_path": str(db_path.resolve()),
        "scope": scope,
        "task_id": task_id,
        "expected_chain": expected,
        "messages": messages,
        "message_receipts": message_receipts,
        "route_receipts": route_receipts,
        "final_ack_event": _event_snapshot(final_ack),
        "chain_prefix_event_count": len(chain_rows),
        "chain_prefix_head_sha256": final_ack["chain_sha256"],
        "evidence": evidence,
        "credential_contents_recorded": False,
    }
    if receipt_schema in {RECEIPT_SCHEMA_V2, RECEIPT_SCHEMA_V3}:
        receipt["room_id"] = resolved_room_id
    if receipt_schema == RECEIPT_SCHEMA_V3:
        receipt["participant_agents"] = sorted(participant_agents)
        receipt["child_receipts"] = child_receipts
        receipt["all_participants_independently_verified"] = True
    receipt["receipt_sha256"] = stable_sha256(receipt)
    return receipt


def verify_collaboration_receipt(receipt_path: Path) -> dict[str, Any]:
    receipt = _read_json(receipt_path)
    errors: list[str] = []
    expected_sha = str(receipt.get("receipt_sha256") or "")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    receipt_schema = str(receipt.get("schema") or "")
    if receipt_schema not in SUPPORTED_RECEIPT_SCHEMAS:
        errors.append("schema")
    if stable_sha256(unsigned) != expected_sha:
        errors.append("receipt_sha256")
    try:
        rebuilt = capture_collaboration_receipt(
            db_path=Path(receipt["database_path"]),
            scope=receipt["scope"],
            task_id=receipt["task_id"],
            expected_chain=receipt["expected_chain"],
            evidence_paths={
                label: Path(item["path"])
                for label, item in receipt["evidence"].items()
            },
            child_receipt_paths={
                label: Path(item["path"])
                for label, item in (receipt.get("child_receipts") or {}).items()
            },
            receipt_schema=receipt_schema,
            room_id=receipt.get("room_id"),
        )
        for key in (
            "database_path",
            "scope",
            "task_id",
            "expected_chain",
            "messages",
            "message_receipts",
            "route_receipts",
            "final_ack_event",
            "chain_prefix_event_count",
            "chain_prefix_head_sha256",
            "evidence",
            "credential_contents_recorded",
        ):
            if rebuilt.get(key) != receipt.get(key):
                errors.append(key)
        if receipt_schema in {RECEIPT_SCHEMA_V2, RECEIPT_SCHEMA_V3} and rebuilt.get("room_id") != receipt.get(
            "room_id"
        ):
            errors.append("room_id")
        if receipt_schema == RECEIPT_SCHEMA_V3:
            for key in (
                "participant_agents",
                "child_receipts",
                "all_participants_independently_verified",
            ):
                if rebuilt.get(key) != receipt.get(key):
                    errors.append(key)
    except (KeyError, OSError, ReceiptError, sqlite3.Error, ValueError) as exc:
        errors.append(f"evidence:{exc}")
    return {
        "valid": not errors,
        "receipt_sha256": expected_sha,
        "errors": errors,
        "writes_performed": 0,
    }


def capture_main() -> int:
    parser = argparse.ArgumentParser(description="Capture a collaboration-chain receipt.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-chain", type=Path, required=True)
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--child-receipt", action="append", default=[])
    parser.add_argument(
        "--schema",
        choices=sorted(SUPPORTED_RECEIPT_SCHEMAS),
        default=RECEIPT_SCHEMA,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite existing receipt: {args.output}")
    expected = json.loads(args.expected_chain.read_text(encoding="utf-8"))
    if not isinstance(expected, list):
        parser.error("expected-chain must contain a JSON array")
    evidence: dict[str, Path] = {}
    for item in args.evidence:
        label, separator, raw_path = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
            parser.error("evidence must use label=path")
        evidence[label] = Path(raw_path).resolve()
    child_receipts: dict[str, Path] = {}
    for item in args.child_receipt:
        label, separator, raw_path = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
            parser.error("child-receipt must use label=path")
        child_receipts[label] = Path(raw_path).resolve()
    receipt = capture_collaboration_receipt(
        db_path=args.db.resolve(),
        scope=args.scope,
        task_id=args.task_id,
        expected_chain=expected,
        evidence_paths=evidence,
        child_receipt_paths=child_receipts,
        receipt_schema=args.schema,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(
        json.dumps(
            {"status": "CAPTURED", **verify_collaboration_receipt(args.output)},
            sort_keys=True,
        )
    )
    return 0


def verify_main() -> int:
    parser = argparse.ArgumentParser(description="Verify a collaboration receipt.")
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    result = verify_collaboration_receipt(args.receipt.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(capture_main())
