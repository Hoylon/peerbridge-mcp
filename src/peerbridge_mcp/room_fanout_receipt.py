"""Receipts for one-to-many room fanout and independently routed replies."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .bridge import sha256_bytes, stable_sha256, utc_now
from .secret_scan import contains_secret
from .collaboration_receipt import _message_snapshot, _route_receipt_snapshot
from .openai_compatible_runner import RECEIPT_SCHEMA as OPENAI_RUN_SCHEMA
from .provider_receipt import (
    ReceiptError,
    _event_snapshot,
    _file_sha256,
    _read_json,
    _readonly_connection,
    _verify_chain_prefix,
)


RECEIPT_SCHEMA_V1 = "peerbridge.room-fanout-receipt.v1"
RECEIPT_SCHEMA = "peerbridge.room-fanout-receipt.v2"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


def _dispatch_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": row["scope"],
        "message_id": row["message_id"],
        "agent_id": row["agent_id"],
        "status": row["status"],
        "claimed_session_id": row["claimed_session_id"],
        "lease_token_sha256": row["lease_token_sha256"],
        "lease_expires_epoch": row["lease_expires_epoch"],
        "attempt_count": int(row["attempt_count"]),
        "claimed_utc": row["claimed_utc"],
        "updated_utc": row["updated_utc"],
        "completed_utc": row["completed_utc"],
        "reply_message_id": row["reply_message_id"],
        "inference_receipt_sha256": row["inference_receipt_sha256"],
        "error_code": row["error_code"],
    }


def _dispatch_snapshot(
    row: sqlite3.Row | dict[str, Any], *, allow_terminal_failure: bool = False
) -> dict[str, Any]:
    payload = _dispatch_payload(row)
    if stable_sha256(payload) != row["dispatch_sha256"]:
        raise ReceiptError(f"dispatch {row['message_id']} SHA mismatch")
    if payload["attempt_count"] != 1:
        raise ReceiptError(f"dispatch {row['message_id']} was not attempted once")
    if payload["lease_expires_epoch"] is not None:
        raise ReceiptError(f"dispatch {row['message_id']} retained an active lease")
    if payload["status"] == "completed":
        if not payload["claimed_session_id"]:
            raise ReceiptError(
                f"dispatch {row['message_id']} lacks executor provenance"
            )
        if not SHA256.fullmatch(str(payload["lease_token_sha256"] or "")):
            raise ReceiptError(
                f"dispatch {row['message_id']} lacks lease provenance"
            )
        if payload["error_code"] is not None or payload["completed_utc"] is None:
            raise ReceiptError(f"dispatch {row['message_id']} retained failure state")
        if not payload["reply_message_id"]:
            raise ReceiptError(f"dispatch {row['message_id']} lacks a reply")
        inference_sha = str(payload["inference_receipt_sha256"] or "")
        if not SHA256.fullmatch(inference_sha):
            raise ReceiptError(
                f"dispatch {row['message_id']} lacks an inference receipt SHA"
            )
    elif payload["status"] == "failed" and allow_terminal_failure:
        if (
            payload["claimed_session_id"] is not None
            or payload["lease_token_sha256"] is not None
        ):
            raise ReceiptError(
                f"failed dispatch {row['message_id']} retained lease provenance"
            )
        if (
            payload["completed_utc"] is not None
            or payload["reply_message_id"] is not None
            or payload["inference_receipt_sha256"] is not None
        ):
            raise ReceiptError(
                f"failed dispatch {row['message_id']} retained success evidence"
            )
        if not ERROR_CODE.fullmatch(str(payload["error_code"] or "")):
            raise ReceiptError(
                f"failed dispatch {row['message_id']} lacks a safe error code"
            )
    else:
        raise ReceiptError(f"dispatch {row['message_id']} is not terminal")
    public = dict(payload)
    public.pop("lease_token_sha256", None)
    public["dispatch_sha256"] = row["dispatch_sha256"]
    return public


def _event_with_payload(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"event {row['sequence']} payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ReceiptError(f"event {row['sequence']} payload is not an object")
    if sha256_bytes(row["payload_json"].encode("utf-8")) != row["payload_sha256"]:
        raise ReceiptError(f"event {row['sequence']} payload SHA mismatch")
    return _event_snapshot(row), payload


def _find_event(
    rows: list[sqlite3.Row], event_type: str, message_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        if row["event_type"] != event_type:
            continue
        snapshot, payload = _event_with_payload(row)
        if message_id is None or payload.get("message_id") == message_id:
            matches.append((snapshot, payload))
    if len(matches) != 1:
        raise ReceiptError(
            f"expected exactly one {event_type} event for {message_id or 'task'}"
        )
    return matches[0]


def _capability_receipt(path: Path, *, route: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        raise ReceiptError(f"capability receipt is absent: {path}")
    receipt = _read_json(path)
    if receipt.get("schema") != OPENAI_RUN_SCHEMA:
        raise ReceiptError("capability receipt has an unsupported schema")
    unsigned = dict(receipt)
    expected_sha = str(unsigned.pop("receipt_sha256", ""))
    if not SHA256.fullmatch(expected_sha) or stable_sha256(unsigned) != expected_sha:
        raise ReceiptError("capability receipt SHA mismatch")
    if receipt.get("raw_content_recorded") is not False:
        raise ReceiptError("capability receipt records raw model content")
    if receipt.get("credential_contents_recorded") is not False:
        raise ReceiptError("capability receipt records credential contents")
    route_evidence = receipt.get("route")
    if not isinstance(route_evidence, dict):
        raise ReceiptError("capability receipt lacks route evidence")
    for key in ("provider_id", "model_id", "route_class"):
        if route_evidence.get(key) != route.get(key):
            raise ReceiptError(f"capability receipt route mismatch: {key}")
    mcp_methods = [
        str(item.get("method"))
        for item in receipt.get("mcp_calls", [])
        if isinstance(item, dict)
    ]
    tools = [
        str(item.get("name"))
        for item in receipt.get("tool_calls", [])
        if isinstance(item, dict)
    ]
    if "initialize" not in mcp_methods or "tools/list" not in mcp_methods:
        raise ReceiptError("capability receipt lacks MCP lifecycle calls")
    if "bridge_status" not in tools:
        raise ReceiptError("capability receipt lacks a real PeerBridge tool invocation")
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    if contains_secret(encoded):
        raise ReceiptError("capability receipt contains a credential-like value")
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "file_sha256": _file_sha256(path),
        "schema": OPENAI_RUN_SCHEMA,
        "receipt_sha256": expected_sha,
        "provider_id": route_evidence["provider_id"],
        "model_id": route_evidence["model_id"],
        "response_model_id": route_evidence.get("response_model_id"),
        "route_class": route_evidence["route_class"],
        "mcp_methods": sorted(set(mcp_methods)),
        "tool_names": sorted(set(tools)),
        "credential_contents_recorded": False,
        "raw_content_recorded": False,
        "evidence_role": "provider_mcp_capability_not_exact_dispatch_inference",
    }


def capture_room_fanout_receipt(
    *,
    db_path: Path,
    scope: str,
    task_id: str,
    room_id: str,
    capability_receipt_paths: dict[str, Path],
    receipt_schema: str = RECEIPT_SCHEMA,
) -> dict[str, Any]:
    if receipt_schema not in {RECEIPT_SCHEMA_V1, RECEIPT_SCHEMA}:
        raise ReceiptError("room fanout receipt schema is unsupported")
    allow_terminal_failure = receipt_schema == RECEIPT_SCHEMA
    with _readonly_connection(db_path) as connection:
        task_events = connection.execute(
            "SELECT * FROM events WHERE scope=? AND task_id=? ORDER BY sequence",
            (scope, task_id),
        ).fetchall()
        fanout_event, fanout_payload = _find_event(
            list(task_events), "message.room_fanout_sent"
        )
        if fanout_payload.get("room_id") != room_id:
            raise ReceiptError("fanout event room mismatch")
        recipients = fanout_payload.get("recipients")
        if not isinstance(recipients, list) or len(recipients) < 2:
            raise ReceiptError("fanout requires at least two routed recipients")
        if int(fanout_payload.get("recipient_count", -1)) != len(recipients):
            raise ReceiptError("fanout recipient count mismatch")
        agent_ids = [str(item.get("agent_id") or "") for item in recipients]
        if not all(agent_ids) or len(set(agent_ids)) != len(agent_ids):
            raise ReceiptError("fanout recipients are invalid or duplicated")
        originals: list[dict[str, Any]] = []
        replies: list[dict[str, Any]] = []
        dispatches: list[dict[str, Any]] = []
        route_receipts: list[dict[str, Any]] = []
        completed_events: list[dict[str, Any]] = []
        failed_events: list[dict[str, Any]] = []
        completed_agents: list[str] = []
        failed_agents: list[str] = []
        capability_receipts: dict[str, dict[str, Any]] = {}

        for expected in recipients:
            message_id = str(expected.get("message_id") or "")
            agent_id = str(expected["agent_id"])
            original = connection.execute(
                "SELECT * FROM messages WHERE scope=? AND message_id=?",
                (scope, message_id),
            ).fetchone()
            if original is None:
                raise ReceiptError(f"fanout message is absent: {message_id}")
            original_snapshot = _message_snapshot(original, "peerbridge.collaboration-chain-receipt.v3")
            if (
                original_snapshot["room_id"] != room_id
                or original["task_id"] != task_id
                or original_snapshot["recipient"] != agent_id
                or original_snapshot["reply_to"] is not None
            ):
                raise ReceiptError(f"fanout message binding mismatch: {message_id}")
            for key in ("message_id", "sequence", "content_sha256", "route_request_sha256"):
                observed = (
                    original_snapshot["route_request"]["route_request_sha256"]
                    if key == "route_request_sha256"
                    else original_snapshot[key]
                )
                if observed != expected.get(key):
                    raise ReceiptError(f"fanout event message mismatch: {key}")

            dispatch_row = connection.execute(
                """SELECT * FROM message_dispatches
                     WHERE scope=? AND message_id=? AND agent_id=?""",
                (scope, message_id, agent_id),
            ).fetchone()
            if dispatch_row is None:
                raise ReceiptError(f"dispatch is absent: {message_id}")
            dispatch = _dispatch_snapshot(
                dispatch_row, allow_terminal_failure=allow_terminal_failure
            )
            originals.append(original_snapshot)
            dispatches.append(dispatch)
            if dispatch["status"] == "failed":
                if receipt_schema == RECEIPT_SCHEMA_V1:
                    raise ReceiptError(f"dispatch {message_id} is not completed")
                route_row = connection.execute(
                    """SELECT * FROM message_route_receipts
                         WHERE scope=? AND message_id=? AND agent_id=?""",
                    (scope, message_id, agent_id),
                ).fetchone()
                if route_row is not None:
                    raise ReceiptError(
                        f"failed dispatch unexpectedly retained route evidence: {message_id}"
                    )
                failure_rows = connection.execute(
                    """SELECT * FROM events
                         WHERE scope=? AND event_type='message.dispatch_failed'
                           AND json_extract(payload_json, '$.message_id')=?
                         ORDER BY sequence""",
                    (scope, message_id),
                ).fetchall()
                failed_event, failed_payload = _find_event(
                    list(failure_rows), "message.dispatch_failed", message_id
                )
                if failed_event.get("task_id") not in {None, task_id}:
                    raise ReceiptError(f"dispatch failure task mismatch: {message_id}")
                expected_failed = {
                    "status": "failed",
                    "error_code": dispatch["error_code"],
                    "retry_after_seconds": None,
                    "retry_not_before_epoch": None,
                    "retry_schedule_sha256": None,
                    "dispatch_sha256": dispatch["dispatch_sha256"],
                }
                if any(
                    failed_payload.get(key) != value
                    for key, value in expected_failed.items()
                ):
                    raise ReceiptError(f"dispatch failure event mismatch: {message_id}")
                failed_events.append(failed_event)
                failed_agents.append(agent_id)
                continue
            reply = connection.execute(
                "SELECT * FROM messages WHERE scope=? AND message_id=?",
                (scope, dispatch["reply_message_id"]),
            ).fetchone()
            if reply is None:
                raise ReceiptError(f"dispatch reply is absent: {message_id}")
            reply_snapshot = _message_snapshot(reply, "peerbridge.collaboration-chain-receipt.v3")
            if (
                reply_snapshot["reply_to"] != message_id
                or reply_snapshot["sender"] != agent_id
                or reply_snapshot["recipient"] != original_snapshot["sender"]
                or reply_snapshot["room_id"] != room_id
                or reply["task_id"] != task_id
            ):
                raise ReceiptError(f"dispatch reply binding mismatch: {message_id}")

            route_row = connection.execute(
                """SELECT * FROM message_route_receipts
                     WHERE scope=? AND message_id=? AND agent_id=?""",
                (scope, message_id, agent_id),
            ).fetchone()
            if route_row is None:
                raise ReceiptError(f"route receipt is absent: {message_id}")
            route = _route_receipt_snapshot(
                route_row,
                original_snapshot["route_request"]["route_request_sha256"],
            )
            if route["route_status"] != "verified":
                raise ReceiptError(f"route was not verified: {message_id}")

            completed_event, completed_payload = _find_event(
                list(task_events), "message.dispatch_completed", message_id
            )
            expected_completed = {
                "reply_message_id": dispatch["reply_message_id"],
                "dispatch_sha256": dispatch["dispatch_sha256"],
                "inference_receipt_sha256": dispatch["inference_receipt_sha256"],
                "route_receipt_sha256": route["receipt_sha256"],
            }
            if any(
                completed_payload.get(key) != value
                for key, value in expected_completed.items()
            ):
                raise ReceiptError(f"dispatch completion event mismatch: {message_id}")

            capability_path = capability_receipt_paths.get(agent_id)
            if capability_path is not None:
                capability_route = {
                    "provider_id": route["observed_provider_id"],
                    "model_id": original_snapshot["route_request"]["requested_model_id"],
                    "route_class": route.get("observed_route_class"),
                }
                capability_receipts[agent_id] = _capability_receipt(
                    capability_path, route=capability_route
                )
            replies.append(reply_snapshot)
            route_receipts.append(route)
            completed_events.append(completed_event)
            completed_agents.append(agent_id)

        capability_agents = set(capability_receipt_paths)
        completed_agent_set = set(completed_agents)
        if receipt_schema == RECEIPT_SCHEMA_V1 and capability_agents != completed_agent_set:
            raise ReceiptError(
                "capability receipts must match the successfully completed recipients"
            )
        if receipt_schema == RECEIPT_SCHEMA and not capability_agents.issubset(
            completed_agent_set
        ):
            raise ReceiptError(
                "capability receipts may only bind successfully completed recipients"
            )
        if not completed_agents:
            raise ReceiptError("fanout produced no successful routed reply")
        reply_ids = [item["message_id"] for item in replies]
        if reply_ids:
            placeholders = ",".join("?" for _ in reply_ids)
            cascade_count = connection.execute(
                f"""SELECT COUNT(*) FROM message_dispatches
                      WHERE scope=? AND message_id IN ({placeholders})""",
                (scope, *reply_ids),
            ).fetchone()[0]
            if int(cascade_count) != 0:
                raise ReceiptError("a fanout reply triggered a reply cascade")

        reconstructed_fanout = {
            "fanout_id": fanout_payload["fanout_id"],
            "scope": scope,
            "room_id": room_id,
            "task_id": task_id,
            "sender": originals[0]["sender"],
            "recipients": recipients,
            "created_utc": originals[0]["created_utc"],
        }
        if any(item["sender"] != originals[0]["sender"] for item in originals):
            raise ReceiptError("fanout senders are inconsistent")
        if any(item["created_utc"] != originals[0]["created_utc"] for item in originals):
            raise ReceiptError("fanout timestamps are inconsistent")
        if stable_sha256(reconstructed_fanout) != fanout_payload.get("fanout_sha256"):
            raise ReceiptError("fanout SHA mismatch")

        terminal_events = sorted(
            [*completed_events, *failed_events], key=lambda item: int(item["sequence"])
        )
        through_sequence = max(int(item["sequence"]) for item in terminal_events)
        chain_rows = _verify_chain_prefix(connection, scope, through_sequence)

    receipt: dict[str, Any] = {
        "schema": receipt_schema,
        "created_utc": utc_now(),
        "database_path": str(db_path.resolve()),
        "scope": scope,
        "task_id": task_id,
        "room_id": room_id,
        "fanout_event": fanout_event,
        "fanout_sha256": fanout_payload["fanout_sha256"],
        "recipient_agents": sorted(agent_ids),
        "original_messages": originals,
        "reply_messages": replies,
        "dispatches": dispatches,
        "route_receipts": route_receipts,
        "dispatch_completed_events": completed_events,
        "provider_capability_receipts": capability_receipts,
        "chain_prefix_event_count": len(chain_rows),
        "chain_prefix_head_sha256": chain_rows[-1]["chain_sha256"],
        "claims": {
            "atomic_room_fanout_observed": True,
            "all_dispatches_completed_once": True,
            "all_routes_verified": True,
            "all_replies_room_and_parent_bound": True,
            "provider_mcp_capability_independently_bound": True,
            "exact_dispatch_inference_receipt_artifacts_available": False,
        },
        "raw_message_content_recorded": False,
        "credential_contents_recorded": False,
    }
    if receipt_schema == RECEIPT_SCHEMA:
        receipt["dispatch_failed_events"] = failed_events
        receipt["terminal_outcomes"] = [
            {
                "agent_id": dispatch["agent_id"],
                "message_id": dispatch["message_id"],
                "status": dispatch["status"],
                "error_code": dispatch["error_code"],
                "dispatch_sha256": dispatch["dispatch_sha256"],
            }
            for dispatch in dispatches
        ]
        receipt["completed_agents"] = sorted(completed_agents)
        receipt["failed_agents"] = sorted(failed_agents)
        receipt["mcp_capability_bound_agents"] = sorted(capability_receipts)
        receipt["claims"] = {
            "atomic_room_fanout_observed": True,
            "all_dispatches_terminal_once": True,
            "successful_routes_verified": True,
            "successful_replies_room_and_parent_bound": True,
            "terminal_failures_bound": True,
            "failed_seat_did_not_hang_fanout": True,
            "no_reply_cascade_observed": True,
            "provider_mcp_capability_independently_bound": (
                set(capability_receipts) == set(completed_agents)
            ),
            "dispatch_inference_receipt_hashes_bound": True,
            "exact_dispatch_inference_receipt_artifacts_available": False,
        }
    receipt["receipt_sha256"] = stable_sha256(receipt)
    return receipt


def verify_room_fanout_receipt(receipt_path: Path) -> dict[str, Any]:
    receipt = _read_json(receipt_path)
    errors: list[str] = []
    expected_sha = str(receipt.get("receipt_sha256") or "")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    receipt_schema = str(receipt.get("schema") or "")
    if receipt_schema not in {RECEIPT_SCHEMA_V1, RECEIPT_SCHEMA}:
        errors.append("schema")
    if stable_sha256(unsigned) != expected_sha:
        errors.append("receipt_sha256")
    try:
        rebuilt = capture_room_fanout_receipt(
            db_path=Path(receipt["database_path"]),
            scope=str(receipt["scope"]),
            task_id=str(receipt["task_id"]),
            room_id=str(receipt["room_id"]),
            receipt_schema=receipt_schema,
            capability_receipt_paths={
                agent: Path(item["path"])
                for agent, item in receipt["provider_capability_receipts"].items()
            },
        )
        ignored = {"created_utc", "receipt_sha256"}
        for key in sorted(set(receipt) | set(rebuilt) - ignored):
            if key not in ignored and receipt.get(key) != rebuilt.get(key):
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
    parser = argparse.ArgumentParser(description="Capture a room fanout receipt.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--capability-receipt", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite existing receipt: {args.output}")
    capability_paths: dict[str, Path] = {}
    for item in args.capability_receipt:
        agent, separator, raw_path = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+", agent):
            parser.error("capability-receipt must use agent=path")
        capability_paths[agent] = Path(raw_path).resolve()
    try:
        receipt = capture_room_fanout_receipt(
            db_path=args.db.resolve(),
            scope=args.scope,
            task_id=args.task_id,
            room_id=args.room_id,
            capability_receipt_paths=capability_paths,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(args.output)
        result = verify_room_fanout_receipt(args.output)
    except (OSError, ReceiptError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"status": "CAPTURED", **result}, sort_keys=True))
    return 0 if result["valid"] else 1


def verify_main() -> int:
    parser = argparse.ArgumentParser(description="Verify a room fanout receipt.")
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    result = verify_room_fanout_receipt(args.receipt.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(capture_main())
