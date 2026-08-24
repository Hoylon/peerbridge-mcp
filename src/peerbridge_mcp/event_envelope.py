"""Local-only collaboration event envelope reserved for future encrypted sync."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .bridge import Bridge, ZERO_SHA256, stable_sha256
from .secret_scan import contains_secret


EVENT_ENVELOPE_SCHEMA = "peerbridge.local-event.v1"
SYNC_STATE = "disabled-alpha-5.2"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class EventEnvelopeError(RuntimeError):
    """An audit event cannot be projected into the local sync boundary safely."""


def validate_event_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema",
        "event_id",
        "scope",
        "sequence",
        "actor",
        "event_type",
        "task_id",
        "created_utc",
        "payload_sha256",
        "prev_chain_sha256",
        "chain_sha256",
        "authoritative_store",
        "sync_state",
        "transport",
        "encryption_required",
        "collaboration_channel",
    }
    if set(envelope) != expected:
        raise EventEnvelopeError("event envelope fields do not match schema v1")
    if envelope["schema"] != EVENT_ENVELOPE_SCHEMA:
        raise EventEnvelopeError("event envelope schema is unsupported")
    if envelope["authoritative_store"] != "local-sqlite":
        raise EventEnvelopeError("event envelope authority must remain local SQLite")
    if envelope["sync_state"] != SYNC_STATE or envelope["transport"] is not None:
        raise EventEnvelopeError("cloud collaboration must remain disabled in Alpha 5.2")
    if envelope["encryption_required"] is not True:
        raise EventEnvelopeError("future collaboration sync must require encryption")
    if envelope["collaboration_channel"] != "reserved-separate-from-feedback-announcements":
        raise EventEnvelopeError("collaboration channel boundary is invalid")
    for key in ("payload_sha256", "prev_chain_sha256", "chain_sha256"):
        if not SHA256.fullmatch(str(envelope[key] or "")):
            raise EventEnvelopeError(f"{key} is invalid")
    if contains_secret(json.dumps(envelope, sort_keys=True)):
        raise EventEnvelopeError("event envelope contains credential-like data")
    return dict(envelope)


def local_event_envelopes(
    bridge: Bridge,
    *,
    task_id: str | None = None,
    after_sequence: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Verify the complete local chain, then return a bounded metadata projection."""

    after_sequence = max(0, int(after_sequence))
    limit = max(1, min(int(limit), 2_000))
    with bridge._connect() as connection:
        rows = connection.execute(
            "SELECT * FROM events WHERE scope=? ORDER BY sequence",
            (bridge.scope,),
        ).fetchall()
    previous = ZERO_SHA256
    projected: list[dict[str, Any]] = []
    for row in rows:
        payload_bytes = str(row["payload_json"]).encode("utf-8")
        payload_sha = hashlib.sha256(payload_bytes).hexdigest()
        if payload_sha != row["payload_sha256"]:
            raise EventEnvelopeError("audit event payload SHA-256 does not match")
        chain_payload = {
            "event_id": row["event_id"],
            "scope": row["scope"],
            "actor": row["actor"],
            "event_type": row["event_type"],
            "task_id": row["task_id"],
            "payload_sha256": row["payload_sha256"],
            "created_utc": row["created_utc"],
            "prev_chain_sha256": row["prev_chain_sha256"],
        }
        if row["prev_chain_sha256"] != previous:
            raise EventEnvelopeError("audit event chain predecessor does not match")
        if stable_sha256(chain_payload) != row["chain_sha256"]:
            raise EventEnvelopeError("audit event chain SHA-256 does not match")
        previous = str(row["chain_sha256"])
        if int(row["sequence"]) <= after_sequence:
            continue
        if task_id is not None and str(row["task_id"] or "") != str(task_id):
            continue
        envelope = {
            "schema": EVENT_ENVELOPE_SCHEMA,
            "event_id": str(row["event_id"]),
            "scope": str(row["scope"]),
            "sequence": int(row["sequence"]),
            "actor": str(row["actor"]),
            "event_type": str(row["event_type"]),
            "task_id": str(row["task_id"]) if row["task_id"] else None,
            "created_utc": str(row["created_utc"]),
            "payload_sha256": str(row["payload_sha256"]),
            "prev_chain_sha256": str(row["prev_chain_sha256"]),
            "chain_sha256": str(row["chain_sha256"]),
            "authoritative_store": "local-sqlite",
            "sync_state": SYNC_STATE,
            "transport": None,
            "encryption_required": True,
            "collaboration_channel": "reserved-separate-from-feedback-announcements",
        }
        projected.append(validate_event_envelope(envelope))
    return projected[:limit]


__all__ = [
    "EVENT_ENVELOPE_SCHEMA",
    "EventEnvelopeError",
    "SYNC_STATE",
    "local_event_envelopes",
    "validate_event_envelope",
]
