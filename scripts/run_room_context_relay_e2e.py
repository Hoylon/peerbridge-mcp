"""Run a two-turn same-room memory check through one explicit relay route."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from peerbridge_mcp.bridge import Bridge, stable_sha256, utc_now
from peerbridge_mcp.mailbox_supervisor import MailboxSupervisor


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_rows(
    db_path: Path, scope: str, route_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        profile = connection.execute(
            """SELECT * FROM route_profiles
                 WHERE scope=? AND route_id=? AND enabled=1""",
            (scope, route_id),
        ).fetchone()
        if profile is None:
            raise RuntimeError("requested relay route is unavailable")
        connections = connection.execute(
            """SELECT * FROM provider_connections
                 WHERE scope=? AND provider_id=? AND route_class=? AND enabled=1""",
            (scope, profile["provider_id"], profile["route_class"]),
        ).fetchall()
    if len(connections) != 1:
        raise RuntimeError("requested relay route has no unique provider connection")
    return dict(profile), dict(connections[0])


def _provider_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "connection_id",
            "display_name",
            "route_class",
            "provider_id",
            "secret_backend",
            "credential_target",
            "endpoint_sha256",
            "credential_fingerprint_sha256",
            "descriptor_schema",
            "credential_version_sha256",
        )
    } | {"enabled": True}


def _profile_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "route_id",
            "agent_id",
            "client_name",
            "provider_id",
            "model_id",
            "response_model_id",
            "inference_timeout_seconds",
            "reasoning_mode",
            "route_class",
        )
    } | {"enabled": True}


def _reply(db_path: Path, reply_to: str) -> dict[str, Any]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT message_id, sender, body, content_sha256
                 FROM messages WHERE reply_to=? ORDER BY sequence""",
            (reply_to,),
        ).fetchall()
    if len(rows) != 1:
        raise RuntimeError("expected exactly one relay reply")
    return dict(rows[0])


def _write_exclusive(path: Path, value: dict[str, Any]) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run(args: argparse.Namespace) -> dict[str, Any]:
    project_root = args.project_root.resolve()
    source_db = args.source_db.resolve()
    source_sha_before = _sha256(source_db)
    profile, provider = _source_rows(source_db, args.scope, args.route_id)
    if profile["route_class"] != "relay":
        raise RuntimeError("this check requires an explicitly selected relay route")
    if "grok" not in str(profile["model_id"] or "").casefold():
        raise RuntimeError("this check is bound to a Grok relay model")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    db_path = output / "peerbridge.sqlite3"
    human = Bridge(
        project_root,
        db_path,
        "human-operator",
        args.scope,
        session_id="room-context-e2e-human",
    )
    human.upsert_provider_connection(_provider_payload(provider))
    registered_profile = human.upsert_route_profile(_profile_payload(profile))
    human.create_room({"room_id": args.room_id, "name": "Room context relay E2E"})
    human.join_room(
        {
            "room_id": args.room_id,
            "agent_id": profile["agent_id"],
            "route_profile_id": profile["route_id"],
        }
    )

    phrase = "PB-CONTEXT-8Q7M-2026"
    supervisor = MailboxSupervisor(
        project_root,
        db_path,
        args.scope,
        max_parallel_dispatches=1,
    )
    try:
        first = human.send_room_fanout(
            {
                "room_id": args.room_id,
                "task_id": "room-context-relay-e2e-turn-1",
                "subject": "Same-room context turn one",
                "body": (
                    "Store this synthetic continuity token for my next message: "
                    f"{phrase}. Reply only ACK_STORED."
                ),
            }
        )
        first_cycle = supervisor.run_cycle()
        first_message_id = str(first["recipients"][0]["message_id"])
        first_reply = _reply(db_path, first_message_id)

        second = human.send_room_fanout(
            {
                "room_id": args.room_id,
                "task_id": "room-context-relay-e2e-turn-2",
                "subject": "Same-room context turn two",
                "body": (
                    "What exact synthetic continuity token did I give you in my "
                    "immediately previous message in this room? Reply only with the token."
                ),
            }
        )
        second_message_id = str(second["recipients"][0]["message_id"])
        route_bridge = Bridge(
            project_root,
            db_path,
            str(profile["agent_id"]),
            args.scope,
            session_id="room-context-e2e-probe",
            client_name=profile["client_name"],
            provider_id=profile["provider_id"],
            model_id=profile["model_id"],
            reasoning_mode=profile["reasoning_mode"],
            route_class=profile["route_class"],
        )
        context = route_bridge.room_prompt_context(second_message_id)
        second_cycle = supervisor.run_cycle()
        second_reply = _reply(db_path, second_message_id)
    finally:
        supervisor.close()

    remembered = phrase in str(second_reply["body"])
    source_sha_after = _sha256(source_db)
    receipt: dict[str, Any] = {
        "schema": "peerbridge.room-context-relay-e2e.v1",
        "created_utc": utc_now(),
        "scope": args.scope,
        "room_id": args.room_id,
        "agent_id": profile["agent_id"],
        "route_profile_id": profile["route_id"],
        "route_profile_sha256": registered_profile["profile_sha256"],
        "provider_id": profile["provider_id"],
        "model_id": profile["model_id"],
        "route_class": profile["route_class"],
        "source_db_sha256_before": source_sha_before,
        "source_db_sha256_after": source_sha_after,
        "source_db_zero_write": source_sha_before == source_sha_after,
        "first_message_id": first_message_id,
        "first_reply_content_sha256": first_reply["content_sha256"],
        "second_message_id": second_message_id,
        "second_reply_content_sha256": second_reply["content_sha256"],
        "continuity_phrase_sha256": hashlib.sha256(phrase.encode()).hexdigest(),
        "context_receipt": context["receipt"],
        "first_cycle": {
            "claimed": first_cycle.claimed,
            "completed": first_cycle.completed,
            "terminal_failures": first_cycle.terminal_failures,
        },
        "second_cycle": {
            "claimed": second_cycle.claimed,
            "completed": second_cycle.completed,
            "terminal_failures": second_cycle.terminal_failures,
        },
        "remembered_previous_turn": remembered,
        "status": "PASS" if remembered else "FAIL",
    }
    receipt["receipt_sha256"] = stable_sha256(receipt)
    _write_exclusive(output / "ROOM_CONTEXT_RELAY_E2E_RECEIPT.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--room-id", default="alpha52-context-relay-e2e")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = run(args)
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
