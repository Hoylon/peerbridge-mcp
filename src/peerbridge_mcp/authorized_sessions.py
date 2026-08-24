"""Explicit, source-bound adapters for observable external Agent sessions."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any

from .bridge import (
    CONTROL_ROOM_WORKFLOW_ID,
    HUMAN_OPERATOR_ID,
    Bridge,
    stable_sha256,
    utc_now,
)
from .secret_scan import contains_secret, redact_secrets
from .session_contract import normalize_session_contract, session_panel_id


AUTHORIZED_SOURCE_TYPES = frozenset(
    {"authorized-desktop", "authorized-terminal"}
)
AUTHORIZED_SESSION_STATES = frozenset(
    {"detected", "running", "waiting", "completed", "stopped", "failed"}
)
AUTHORIZED_EVENT_STREAMS = frozenset({"system", "stdout", "stderr"})
AUTHORIZED_EVENT_KINDS = frozenset(
    {"system", "terminal", "activity", "answer", "error"}
)
TERMINAL_SESSION_STATES = frozenset({"completed", "stopped", "failed"})
MAX_AUTHORIZED_SESSIONS = 64
MAX_AUTHORIZED_EVENTS = 1_000
MAX_AUTHORIZED_RESPONSE_EVENTS = 512
MAX_AUTHORIZED_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_EVENT_TEXT_CHARS = 20_000
MAX_EVENT_SUMMARY_CHARS = 1_000
DEFAULT_ADAPTER_TTL_SECONDS = 90
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
INTERNAL_CONTROL_AGENTS = frozenset(
    {
        HUMAN_OPERATOR_ID,
        CONTROL_ROOM_WORKFLOW_ID,
        "control-room-migrator",
        "mailbox-supervisor",
    }
)


class AuthorizedSessionError(ValueError):
    """An external adapter claim is unsafe, ambiguous, or corrupt."""


def _identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text) or contains_secret(text):
        raise AuthorizedSessionError(f"{label} is invalid")
    return text


def _optional_identifier(value: Any, label: str) -> str | None:
    text = str(value or "").strip()
    return _identifier(text, label) if text else None


def _safe_text(
    value: Any,
    label: str,
    *,
    limit: int,
    required: bool = False,
) -> tuple[str | None, bool]:
    raw = str(value or "").strip()
    if required and not raw:
        raise AuthorizedSessionError(f"{label} is required")
    if len(raw) > limit:
        raise AuthorizedSessionError(f"{label} exceeds {limit} characters")
    if not raw:
        return None, False
    redacted = redact_secrets(raw)
    return redacted, redacted != raw


def _session_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "scope",
            "source_type",
            "source_session_id",
            "source_conversation_id",
            "adapter_id",
            "owner_agent_id",
            "owner_bridge_session_id",
            "room_id",
            "room_session_id",
            "display_name",
            "client_name",
            "client_version",
            "requested_route",
            "observed_route",
            "observed_route_source",
            "model_id",
            "model_source",
            "role_id",
            "role_label",
            "state",
            "supports_events",
            "created_utc",
            "started_utc",
            "ended_utc",
            "last_seen_utc",
            "last_seen_epoch",
            "latest_sequence",
        )
    }


def _event_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "scope",
            "source_type",
            "source_session_id",
            "sequence",
            "adapter_event_id",
            "created_utc",
            "stream",
            "kind",
            "text",
            "summary",
            "state_after",
            "secret_redacted",
        )
    }


def _external_contract(
    row: Mapping[str, Any],
    events: list[dict[str, Any]],
    *,
    first_retained_sequence: int,
    stale: bool,
) -> dict[str, Any]:
    source_type = str(row["source_type"])
    supports_events = bool(row["supports_events"])
    persisted_state = str(row["state"])
    state = (
        "unavailable"
        if stale and persisted_state not in TERMINAL_SESSION_STATES
        else persisted_state
    )
    first_sequence = max(0, int(first_retained_sequence))
    latest_sequence = int(row["latest_sequence"])
    outcome_status = (
        persisted_state if persisted_state in TERMINAL_SESSION_STATES else "unavailable"
    )
    result = {
        "session_id": session_panel_id(source_type, str(row["source_session_id"])),
        "source_type": source_type,
        "source_session_id": str(row["source_session_id"]),
        "source_conversation_id": str(row["source_conversation_id"]),
        "adapter_id": str(row["adapter_id"]),
        "owner_agent_id": str(row["owner_agent_id"]),
        "owner_bridge_session_id": str(row["owner_bridge_session_id"]),
        "room_id": row["room_id"],
        "room_session_id": row["room_session_id"],
        "agent_id": str(row["owner_agent_id"]),
        "display_name": str(row["display_name"]),
        "client_name": str(row["client_name"]),
        "client_version": row["client_version"],
        "role": str(row["role_id"]),
        "role_label": row["role_label"],
        "working_directory": None,
        "state": state,
        "adapter_state": persisted_state,
        "return_code": None,
        "started_utc": str(row["started_utc"]),
        "ended_utc": row["ended_utc"],
        "input_submitted": False,
        "requested_route": row["requested_route"],
        "observed_route": row["observed_route"],
        "observed_route_source": row["observed_route_source"],
        "model_id": row["model_id"],
        "model_source": row["model_source"],
        "usage": {"status": "unavailable", "source": None},
        "usage_capture_bounded": True,
        "usage_capture_truncated": first_sequence > 1,
        "terminal_outcome": {
            "status": outcome_status,
            "process_status": "unavailable",
            "exit_code": None,
            "provider_status": "unavailable",
            "provider_reason": None,
            "source": "authorized-adapter",
        },
        "execution_mode": "external-observe",
        "governance_binding_id": None,
        "capture_mode": (
            "authorized-adapter-events" if supports_events else "presence-only"
        ),
        "reasoning_contract": "observable-output-only",
        "input_owner": (
            "external-desktop" if source_type == "authorized-desktop" else "external-terminal"
        ),
        "capabilities": {
            "detectable": True,
            "mirrorable": supports_events,
            "input_capable": False,
            "context_resumable": False,
            "terminal_controllable": False,
            "model_route_only": False,
        },
        "first_retained_sequence": first_sequence,
        "latest_sequence": latest_sequence,
        "events": events,
    }
    return normalize_session_contract(result)


def _presence_contract(row: Mapping[str, Any]) -> dict[str, Any]:
    client_name = str(row["client_name"] or "PeerBridge MCP client")
    lowered = client_name.casefold()
    source_type = (
        "authorized-desktop"
        if any(
            marker in lowered
            for marker in ("desktop", "vscode", "cursor", "windsurf", "zed")
        )
        else "authorized-terminal"
    )
    source_session_id = str(row["session_id"])
    result = {
        "session_id": session_panel_id(source_type, source_session_id),
        "source_type": source_type,
        "source_session_id": source_session_id,
        "source_conversation_id": source_session_id,
        "adapter_id": "peerbridge-mcp-presence",
        "owner_agent_id": str(row["agent_id"]),
        "owner_bridge_session_id": source_session_id,
        "room_id": None,
        "room_session_id": None,
        "agent_id": str(row["agent_id"]),
        "display_name": str(row["agent_id"]),
        "client_name": client_name,
        "client_version": None,
        "role": "equal-participant",
        "role_label": None,
        "working_directory": None,
        "state": "detected",
        "return_code": None,
        "started_utc": str(row["last_seen_utc"]),
        "ended_utc": None,
        "input_submitted": False,
        "requested_route": row["provider_id"],
        "observed_route": None,
        "observed_route_source": None,
        "model_id": row["model_id"],
        "model_source": "peerbridge-mcp-presence" if row["model_id"] else None,
        "usage": {"status": "unavailable", "source": None},
        "usage_capture_bounded": True,
        "usage_capture_truncated": False,
        "terminal_outcome": {
            "status": "unavailable",
            "process_status": "unavailable",
            "exit_code": None,
            "provider_status": "unavailable",
            "provider_reason": None,
            "source": "peerbridge-mcp-presence",
        },
        "execution_mode": "external-detected",
        "governance_binding_id": None,
        "capture_mode": "peerbridge-mcp-presence",
        "reasoning_contract": "no-output-captured",
        "input_owner": "external-surface",
        "capabilities": {
            "detectable": True,
            "mirrorable": False,
            "input_capable": False,
            "context_resumable": False,
            "terminal_controllable": False,
            "model_route_only": False,
        },
        "first_retained_sequence": 0,
        "latest_sequence": 0,
        "events": [],
    }
    return normalize_session_contract(result)


class AuthorizedSessionRegistry:
    """Persist only events an external MCP adapter explicitly publishes."""

    def __init__(
        self,
        bridge: Bridge,
        *,
        adapter_ttl_seconds: int = DEFAULT_ADAPTER_TTL_SECONDS,
        max_retained_sessions: int = MAX_AUTHORIZED_SESSIONS,
    ) -> None:
        self.bridge = bridge
        self.scope = bridge.scope
        self.adapter_ttl_seconds = max(30, min(int(adapter_ttl_seconds), 3_600))
        self.max_retained_sessions = max(
            1, min(int(max_retained_sessions), MAX_AUTHORIZED_SESSIONS)
        )

    @staticmethod
    def _require_source_type(value: Any) -> str:
        source_type = str(value or "").strip().lower()
        if source_type not in AUTHORIZED_SOURCE_TYPES:
            raise AuthorizedSessionError("source type is not an authorized external source")
        return source_type

    @staticmethod
    def _require_state(value: Any, *, default: str) -> str:
        state = str(value or default).strip().lower()
        if state not in AUTHORIZED_SESSION_STATES:
            raise AuthorizedSessionError("external session state is invalid")
        return state

    def _verified_session_row(
        self, connection: Any, source_type: str, source_session_id: str
    ) -> Any:
        row = connection.execute(
            """SELECT * FROM authorized_sessions
                 WHERE scope=? AND source_type=? AND source_session_id=?""",
            (self.scope, source_type, source_session_id),
        ).fetchone()
        if row is None:
            raise AuthorizedSessionError("authorized session does not exist")
        if stable_sha256(_session_payload(row)) != str(row["session_sha256"]):
            raise AuthorizedSessionError("authorized session SHA-256 does not match")
        return row

    def _require_adapter_owner(self, row: Mapping[str, Any]) -> None:
        if (
            str(row["owner_agent_id"]) != self.bridge.agent_id
            or str(row["owner_bridge_session_id"]) != self.bridge.session_id
        ):
            raise AuthorizedSessionError(
                "only the bound external adapter session may publish or close this session"
            )

    def _prune_for_new_session(self, connection: Any, *, now_epoch: float) -> None:
        retained = int(
            connection.execute(
                "SELECT COUNT(*) FROM authorized_sessions WHERE scope=?",
                (self.scope,),
            ).fetchone()[0]
        )
        required = retained - self.max_retained_sessions + 1
        if required <= 0:
            return
        rows = connection.execute(
            """SELECT * FROM authorized_sessions
                 WHERE scope=?
                   AND (state IN ('completed', 'stopped', 'failed')
                        OR last_seen_epoch<?)
                 ORDER BY last_seen_epoch, source_type, source_session_id
                 LIMIT ?""",
            (
                self.scope,
                now_epoch - self.adapter_ttl_seconds,
                required,
            ),
        ).fetchall()
        if len(rows) != required:
            raise AuthorizedSessionError(
                "authorized session retention limit reached while all retained sessions are live"
            )
        for row in rows:
            if stable_sha256(_session_payload(row)) != str(row["session_sha256"]):
                raise AuthorizedSessionError(
                    "authorized session SHA-256 does not match"
                )
        for row in rows:
            source_type = str(row["source_type"])
            source_session_id = str(row["source_session_id"])
            connection.execute(
                """DELETE FROM authorized_session_events
                     WHERE scope=? AND source_type=? AND source_session_id=?""",
                (self.scope, source_type, source_session_id),
            )
            connection.execute(
                """DELETE FROM authorized_sessions
                     WHERE scope=? AND source_type=? AND source_session_id=?""",
                (self.scope, source_type, source_session_id),
            )
            self.bridge._event(
                connection,
                "cockpit.authorized_session.retention_pruned",
                {
                    "source_type": source_type,
                    "source_session_sha256": stable_sha256(source_session_id),
                    "terminal": str(row["state"]) in TERMINAL_SESSION_STATES,
                    "stale": float(row["last_seen_epoch"])
                    < (now_epoch - self.adapter_ttl_seconds),
                },
            )

    def _room_binding(
        self, connection: Any, room_id: str | None
    ) -> tuple[str | None, str | None, str, str | None]:
        if room_id is None:
            return None, None, "equal-participant", None
        room_id = _identifier(room_id, "room id")
        row = connection.execute(
            """SELECT rm.room_session_id, rmr.role_id, rmr.role_label
                 FROM room_memberships rm
                 LEFT JOIN room_member_roles rmr
                   ON rmr.scope=rm.scope AND rmr.room_id=rm.room_id
                  AND rmr.agent_id=rm.agent_id
                WHERE rm.scope=? AND rm.room_id=? AND rm.agent_id=?
                  AND rm.status='active'""",
            (self.scope, room_id, self.bridge.agent_id),
        ).fetchone()
        if row is None:
            raise AuthorizedSessionError(
                "external adapter must be an active member of the linked room"
            )
        return (
            room_id,
            str(row["room_session_id"]),
            str(row["role_id"] or "equal-participant"),
            str(row["role_label"]) if row["role_label"] else None,
        )

    def connect(self, args: Mapping[str, Any]) -> dict[str, Any]:
        source_type = self._require_source_type(args.get("source_type"))
        source_session_id = _identifier(
            args.get("source_session_id"), "source session id"
        )
        source_conversation_id = _identifier(
            args.get("source_conversation_id"), "source conversation id"
        )
        adapter_id = _identifier(args.get("adapter_id"), "adapter id")
        if not isinstance(args.get("supports_events"), bool):
            raise AuthorizedSessionError("supports_events must be explicit")
        supports_events = bool(args["supports_events"])
        state = self._require_state(args.get("state"), default="detected")
        display_name, _ = _safe_text(
            args.get("display_name"), "display name", limit=160, required=True
        )
        client_name, _ = _safe_text(
            args.get("client_name"), "client name", limit=160, required=True
        )
        client_version, _ = _safe_text(
            args.get("client_version"), "client version", limit=80
        )
        requested_route, _ = _safe_text(
            args.get("requested_route"), "requested route", limit=200
        )
        observed_route, _ = _safe_text(
            args.get("observed_route"), "observed route", limit=200
        )
        observed_route_source, _ = _safe_text(
            args.get("observed_route_source"),
            "observed route source",
            limit=200,
        )
        model_id, _ = _safe_text(args.get("model_id"), "model id", limit=200)
        model_source, _ = _safe_text(
            args.get("model_source"), "model source", limit=200
        )
        now_utc = utc_now()
        now_epoch = time.time()
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            room_id, room_session_id, role_id, role_label = self._room_binding(
                connection,
                _optional_identifier(args.get("room_id"), "room id"),
            )
            if room_session_id is not None:
                conflicting = connection.execute(
                    """SELECT source_type, source_session_id
                         FROM authorized_sessions
                        WHERE scope=? AND room_session_id=?
                          AND NOT (source_type=? AND source_session_id=?)
                          AND state NOT IN ('completed', 'stopped', 'failed')
                          AND last_seen_epoch>=?
                        LIMIT 1""",
                    (
                        self.scope,
                        room_session_id,
                        source_type,
                        source_session_id,
                        now_epoch - self.adapter_ttl_seconds,
                    ),
                ).fetchone()
                if conflicting is not None:
                    raise AuthorizedSessionError(
                        "room member already has another live observable adapter"
                    )
            existing = connection.execute(
                """SELECT * FROM authorized_sessions
                     WHERE scope=? AND source_type=? AND source_session_id=?""",
                (self.scope, source_type, source_session_id),
            ).fetchone()
            if existing is None:
                self._prune_for_new_session(connection, now_epoch=now_epoch)
            created_utc = now_utc
            started_utc = now_utc
            latest_sequence = 0
            if existing is not None:
                if stable_sha256(_session_payload(existing)) != str(
                    existing["session_sha256"]
                ):
                    raise AuthorizedSessionError(
                        "authorized session SHA-256 does not match"
                    )
                existing_binding = (
                    str(existing["source_conversation_id"]),
                    str(existing["room_id"]) if existing["room_id"] else None,
                    (
                        str(existing["room_session_id"])
                        if existing["room_session_id"]
                        else None
                    ),
                )
                requested_binding = (
                    source_conversation_id,
                    room_id,
                    room_session_id,
                )
                if existing_binding != requested_binding:
                    raise AuthorizedSessionError(
                        "authorized session identity binding cannot change; "
                        "use a new source session id"
                    )
                same_owner = (
                    str(existing["owner_agent_id"]) == self.bridge.agent_id
                    and str(existing["owner_bridge_session_id"])
                    == self.bridge.session_id
                )
                stale = float(existing["last_seen_epoch"]) < (
                    now_epoch - self.adapter_ttl_seconds
                )
                terminal = str(existing["state"]) in TERMINAL_SESSION_STATES
                if not same_owner and (
                    str(existing["owner_agent_id"]) != self.bridge.agent_id
                    or not (stale or terminal)
                ):
                    raise AuthorizedSessionError(
                        "authorized session is still bound to another live adapter"
                    )
                created_utc = str(existing["created_utc"])
                started_utc = str(existing["started_utc"])
                latest_sequence = int(existing["latest_sequence"])
            ended_utc = now_utc if state in TERMINAL_SESSION_STATES else None
            payload = {
                "scope": self.scope,
                "source_type": source_type,
                "source_session_id": source_session_id,
                "source_conversation_id": source_conversation_id,
                "adapter_id": adapter_id,
                "owner_agent_id": self.bridge.agent_id,
                "owner_bridge_session_id": self.bridge.session_id,
                "room_id": room_id,
                "room_session_id": room_session_id,
                "display_name": display_name,
                "client_name": client_name,
                "client_version": client_version,
                "requested_route": requested_route,
                "observed_route": observed_route,
                "observed_route_source": observed_route_source,
                "model_id": model_id,
                "model_source": model_source,
                "role_id": role_id,
                "role_label": role_label,
                "state": state,
                "supports_events": int(supports_events),
                "created_utc": created_utc,
                "started_utc": started_utc,
                "ended_utc": ended_utc,
                "last_seen_utc": now_utc,
                "last_seen_epoch": now_epoch,
                "latest_sequence": latest_sequence,
            }
            connection.execute(
                """INSERT INTO authorized_sessions(
                       scope, source_type, source_session_id,
                       source_conversation_id, adapter_id, owner_agent_id,
                       owner_bridge_session_id, room_id, room_session_id,
                       display_name, client_name, client_version, requested_route,
                       observed_route, observed_route_source, model_id, model_source,
                       role_id, role_label, state, supports_events, created_utc,
                       started_utc, ended_utc, last_seen_utc, last_seen_epoch,
                       latest_sequence, session_sha256
                   ) VALUES (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?
                   )
                   ON CONFLICT(scope, source_type, source_session_id) DO UPDATE SET
                       source_conversation_id=excluded.source_conversation_id,
                       adapter_id=excluded.adapter_id,
                       owner_agent_id=excluded.owner_agent_id,
                       owner_bridge_session_id=excluded.owner_bridge_session_id,
                       room_id=excluded.room_id,
                       room_session_id=excluded.room_session_id,
                       display_name=excluded.display_name,
                       client_name=excluded.client_name,
                       client_version=excluded.client_version,
                       requested_route=excluded.requested_route,
                       observed_route=excluded.observed_route,
                       observed_route_source=excluded.observed_route_source,
                       model_id=excluded.model_id,
                       model_source=excluded.model_source,
                       role_id=excluded.role_id,
                       role_label=excluded.role_label,
                       state=excluded.state,
                       supports_events=excluded.supports_events,
                       ended_utc=excluded.ended_utc,
                       last_seen_utc=excluded.last_seen_utc,
                       last_seen_epoch=excluded.last_seen_epoch,
                       latest_sequence=excluded.latest_sequence,
                       session_sha256=excluded.session_sha256""",
                (*payload.values(), stable_sha256(payload)),
            )
            self.bridge._event(
                connection,
                "cockpit.authorized_session.connected",
                {
                    "source_type": source_type,
                    "source_session_sha256": stable_sha256(source_session_id),
                    "source_conversation_sha256": stable_sha256(
                        source_conversation_id
                    ),
                    "adapter_id": adapter_id,
                    "room_session_id": room_session_id,
                    "session_sha256": stable_sha256(payload),
                    "supports_events": supports_events,
                },
            )
        return self.get(source_type, source_session_id)

    def publish_event(self, args: Mapping[str, Any]) -> dict[str, Any]:
        source_type = self._require_source_type(args.get("source_type"))
        source_session_id = _identifier(
            args.get("source_session_id"), "source session id"
        )
        adapter_event_id = _identifier(args.get("event_id"), "event id")
        stream = str(args.get("stream") or "system").strip().lower()
        kind = str(args.get("kind") or "activity").strip().lower()
        if stream not in AUTHORIZED_EVENT_STREAMS:
            raise AuthorizedSessionError("external event stream is invalid")
        if kind not in AUTHORIZED_EVENT_KINDS:
            raise AuthorizedSessionError("external event kind is invalid")
        text, text_redacted = _safe_text(
            args.get("text"),
            "event text",
            limit=MAX_EVENT_TEXT_CHARS,
            required=True,
        )
        summary, summary_redacted = _safe_text(
            args.get("summary"),
            "event summary",
            limit=MAX_EVENT_SUMMARY_CHARS,
        )
        state_after = self._require_state(args.get("state"), default="running")
        now_utc = utc_now()
        now_epoch = time.time()
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._verified_session_row(
                connection, source_type, source_session_id
            )
            self._require_adapter_owner(row)
            if not bool(row["supports_events"]):
                raise AuthorizedSessionError(
                    "this adapter did not authorize observable event mirroring"
                )
            existing_event = connection.execute(
                """SELECT * FROM authorized_session_events
                     WHERE scope=? AND source_type=? AND source_session_id=?
                       AND adapter_event_id=?""",
                (
                    self.scope,
                    source_type,
                    source_session_id,
                    adapter_event_id,
                ),
            ).fetchone()
            if existing_event is not None:
                expected = (
                    stream,
                    kind,
                    text,
                    summary,
                    state_after,
                )
                observed = tuple(
                    existing_event[key]
                    for key in ("stream", "kind", "text", "summary", "state_after")
                )
                if observed != expected:
                    raise AuthorizedSessionError(
                        "event id was already used for different observable content"
                    )
                return {
                    "source_type": source_type,
                    "source_session_id": source_session_id,
                    "event_id": adapter_event_id,
                    "sequence": int(existing_event["sequence"]),
                    "event_sha256": str(existing_event["event_sha256"]),
                    "idempotent_replay": True,
                }
            sequence = int(row["latest_sequence"]) + 1
            redacted = int(text_redacted or summary_redacted)
            event_payload = {
                "scope": self.scope,
                "source_type": source_type,
                "source_session_id": source_session_id,
                "sequence": sequence,
                "adapter_event_id": adapter_event_id,
                "created_utc": now_utc,
                "stream": stream,
                "kind": kind,
                "text": text,
                "summary": summary,
                "state_after": state_after,
                "secret_redacted": redacted,
            }
            event_sha = stable_sha256(event_payload)
            connection.execute(
                """INSERT INTO authorized_session_events(
                       scope, source_type, source_session_id, sequence,
                       adapter_event_id, created_utc, stream, kind, text, summary,
                       state_after, secret_redacted, event_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*event_payload.values(), event_sha),
            )
            ended_utc = now_utc if state_after in TERMINAL_SESSION_STATES else None
            connection.execute(
                """UPDATE authorized_sessions
                      SET state=?, ended_utc=?, last_seen_utc=?, last_seen_epoch=?,
                          latest_sequence=?
                    WHERE scope=? AND source_type=? AND source_session_id=?""",
                (
                    state_after,
                    ended_utc,
                    now_utc,
                    now_epoch,
                    sequence,
                    self.scope,
                    source_type,
                    source_session_id,
                ),
            )
            cutoff = sequence - MAX_AUTHORIZED_EVENTS
            if cutoff > 0:
                connection.execute(
                    """DELETE FROM authorized_session_events
                         WHERE scope=? AND source_type=? AND source_session_id=?
                           AND sequence<=?""",
                    (self.scope, source_type, source_session_id, cutoff),
                )
            updated = connection.execute(
                """SELECT * FROM authorized_sessions
                     WHERE scope=? AND source_type=? AND source_session_id=?""",
                (self.scope, source_type, source_session_id),
            ).fetchone()
            assert updated is not None
            session_sha = stable_sha256(_session_payload(updated))
            connection.execute(
                """UPDATE authorized_sessions SET session_sha256=?
                     WHERE scope=? AND source_type=? AND source_session_id=?""",
                (session_sha, self.scope, source_type, source_session_id),
            )
            self.bridge._event(
                connection,
                "cockpit.authorized_session.event_published",
                {
                    "source_type": source_type,
                    "source_session_sha256": stable_sha256(source_session_id),
                    "event_sha256": event_sha,
                    "sequence": sequence,
                    "kind": kind,
                    "secret_redacted": bool(redacted),
                    "session_sha256": session_sha,
                },
            )
        return {
            "source_type": source_type,
            "source_session_id": source_session_id,
            "event_id": adapter_event_id,
            "sequence": sequence,
            "event_sha256": event_sha,
            "secret_redacted": bool(redacted),
            "idempotent_replay": False,
        }

    def close(self, args: Mapping[str, Any]) -> dict[str, Any]:
        source_type = self._require_source_type(args.get("source_type"))
        source_session_id = _identifier(
            args.get("source_session_id"), "source session id"
        )
        state = self._require_state(args.get("state"), default="stopped")
        if state not in TERMINAL_SESSION_STATES:
            raise AuthorizedSessionError("closing state must be terminal")
        now_utc = utc_now()
        now_epoch = time.time()
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._verified_session_row(
                connection, source_type, source_session_id
            )
            self._require_adapter_owner(row)
            connection.execute(
                """UPDATE authorized_sessions
                      SET state=?, ended_utc=?, last_seen_utc=?, last_seen_epoch=?
                    WHERE scope=? AND source_type=? AND source_session_id=?""",
                (
                    state,
                    now_utc,
                    now_utc,
                    now_epoch,
                    self.scope,
                    source_type,
                    source_session_id,
                ),
            )
            updated = connection.execute(
                """SELECT * FROM authorized_sessions
                     WHERE scope=? AND source_type=? AND source_session_id=?""",
                (self.scope, source_type, source_session_id),
            ).fetchone()
            assert updated is not None
            session_sha = stable_sha256(_session_payload(updated))
            connection.execute(
                """UPDATE authorized_sessions SET session_sha256=?
                     WHERE scope=? AND source_type=? AND source_session_id=?""",
                (session_sha, self.scope, source_type, source_session_id),
            )
            self.bridge._event(
                connection,
                "cockpit.authorized_session.closed",
                {
                    "source_type": source_type,
                    "source_session_sha256": stable_sha256(source_session_id),
                    "state": state,
                    "session_sha256": session_sha,
                },
            )
        return self.get(source_type, source_session_id)

    @staticmethod
    def _after_sequence(
        after_sequences: Mapping[str, int] | None,
        row: Mapping[str, Any],
    ) -> int:
        if not after_sequences:
            return 0
        panel_id = session_panel_id(
            str(row["source_type"]), str(row["source_session_id"])
        )
        try:
            after_sequence = int(after_sequences.get(panel_id, 0))
        except (TypeError, ValueError) as exc:
            raise AuthorizedSessionError(
                "authorized session event cursor is invalid"
            ) from exc
        if after_sequence < 0:
            raise AuthorizedSessionError(
                "authorized session event cursor is invalid"
            )
        return after_sequence

    def _events_for(
        self,
        connection: Any,
        row: Mapping[str, Any],
        *,
        after_sequence: int = 0,
        max_events: int = MAX_AUTHORIZED_RESPONSE_EVENTS,
        max_bytes: int = MAX_AUTHORIZED_RESPONSE_BYTES,
    ) -> tuple[list[dict[str, Any]], int, int]:
        first_value = connection.execute(
            """SELECT MIN(sequence) FROM authorized_session_events
                 WHERE scope=? AND source_type=? AND source_session_id=?""",
            (self.scope, row["source_type"], row["source_session_id"]),
        ).fetchone()[0]
        first_retained_sequence = int(first_value) if first_value is not None else 0
        effective_after = max(0, int(after_sequence))
        if (
            effective_after
            and first_retained_sequence
            and first_retained_sequence > effective_after + 1
        ):
            effective_after = 0
        event_rows = connection.execute(
            """SELECT * FROM authorized_session_events
                 WHERE scope=? AND source_type=? AND source_session_id=?
                   AND sequence>?
                 ORDER BY sequence""",
            (
                self.scope,
                row["source_type"],
                row["source_session_id"],
                effective_after,
            ),
        )
        events: list[dict[str, Any]] = []
        response_bytes = 0
        for event_row in event_rows:
            if stable_sha256(_event_payload(event_row)) != str(
                event_row["event_sha256"]
            ):
                raise AuthorizedSessionError(
                    "authorized session event SHA-256 does not match"
                )
            if len(events) >= max(0, int(max_events)):
                break
            event = {
                "sequence": int(event_row["sequence"]),
                "created_utc": str(event_row["created_utc"]),
                "stream": str(event_row["stream"]),
                "kind": str(event_row["kind"]),
                "text": str(event_row["text"]),
                "summary": event_row["summary"],
                "secret_redacted": bool(event_row["secret_redacted"]),
            }
            event_bytes = len(
                json.dumps(
                    event,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if response_bytes + event_bytes > max(0, int(max_bytes)):
                break
            events.append(event)
            response_bytes += event_bytes
        return events, first_retained_sequence, response_bytes

    def get(
        self,
        source_type: str,
        source_session_id: str,
        *,
        after_sequence: int = 0,
    ) -> dict[str, Any]:
        source_type = self._require_source_type(source_type)
        source_session_id = _identifier(source_session_id, "source session id")
        now_epoch = time.time()
        with self.bridge._connect() as connection:
            row = self._verified_session_row(
                connection, source_type, source_session_id
            )
            events, first_retained_sequence, _response_bytes = self._events_for(
                connection, row, after_sequence=after_sequence
            )
        stale = float(row["last_seen_epoch"]) < (
            now_epoch - self.adapter_ttl_seconds
        )
        return _external_contract(
            row,
            events,
            first_retained_sequence=first_retained_sequence,
            stale=stale,
        )

    def list_owned(
        self,
        *,
        limit: int = MAX_AUTHORIZED_SESSIONS,
        after_sequences: Mapping[str, int] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        limit = max(1, min(int(limit), MAX_AUTHORIZED_SESSIONS))
        now_epoch = time.time()
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM authorized_sessions
                     WHERE scope=? AND owner_agent_id=?
                     ORDER BY last_seen_epoch DESC, source_type, source_session_id
                     LIMIT ?""",
                (self.scope, self.bridge.agent_id, limit),
            ).fetchall()
            result = []
            remaining_events = MAX_AUTHORIZED_RESPONSE_EVENTS
            remaining_bytes = MAX_AUTHORIZED_RESPONSE_BYTES
            for index, row in enumerate(rows):
                if stable_sha256(_session_payload(row)) != str(row["session_sha256"]):
                    raise AuthorizedSessionError(
                        "authorized session SHA-256 does not match"
                    )
                remaining_sessions = len(rows) - index
                event_allowance = (
                    (remaining_events + remaining_sessions - 1) // remaining_sessions
                    if remaining_events > 0
                    else 0
                )
                byte_allowance = (
                    (remaining_bytes + remaining_sessions - 1) // remaining_sessions
                    if remaining_bytes > 0
                    else 0
                )
                events, first_retained_sequence, response_bytes = self._events_for(
                    connection,
                    row,
                    after_sequence=self._after_sequence(after_sequences, row),
                    max_events=event_allowance,
                    max_bytes=byte_allowance,
                )
                remaining_events -= len(events)
                remaining_bytes -= response_bytes
                result.append(
                    _external_contract(
                        row,
                        events,
                        first_retained_sequence=first_retained_sequence,
                        stale=float(row["last_seen_epoch"])
                        < (now_epoch - self.adapter_ttl_seconds),
                    )
                )
        return tuple(result)

    def list_for_control_room(
        self,
        *,
        include_detected: bool = True,
        limit: int = MAX_AUTHORIZED_SESSIONS,
        after_sequences: Mapping[str, int] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if self.bridge.agent_id not in {HUMAN_OPERATOR_ID, CONTROL_ROOM_WORKFLOW_ID}:
            raise AuthorizedSessionError(
                "only the local Control Room may aggregate external sessions"
            )
        limit = max(1, min(int(limit), MAX_AUTHORIZED_SESSIONS))
        now_epoch = time.time()
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM authorized_sessions
                     WHERE scope=?
                     ORDER BY last_seen_epoch DESC, source_type, source_session_id
                     LIMIT ?""",
                (self.scope, limit),
            ).fetchall()
            result: list[dict[str, Any]] = []
            registered_bridge_sessions: set[str] = set()
            remaining_events = MAX_AUTHORIZED_RESPONSE_EVENTS
            remaining_bytes = MAX_AUTHORIZED_RESPONSE_BYTES
            for index, row in enumerate(rows):
                if stable_sha256(_session_payload(row)) != str(row["session_sha256"]):
                    raise AuthorizedSessionError(
                        "authorized session SHA-256 does not match"
                    )
                registered_bridge_sessions.add(str(row["owner_bridge_session_id"]))
                remaining_sessions = len(rows) - index
                event_allowance = (
                    (remaining_events + remaining_sessions - 1) // remaining_sessions
                    if remaining_events > 0
                    else 0
                )
                byte_allowance = (
                    (remaining_bytes + remaining_sessions - 1) // remaining_sessions
                    if remaining_bytes > 0
                    else 0
                )
                events, first_retained_sequence, response_bytes = self._events_for(
                    connection,
                    row,
                    after_sequence=self._after_sequence(after_sequences, row),
                    max_events=event_allowance,
                    max_bytes=byte_allowance,
                )
                remaining_events -= len(events)
                remaining_bytes -= response_bytes
                result.append(
                    _external_contract(
                        row,
                        events,
                        first_retained_sequence=first_retained_sequence,
                        stale=float(row["last_seen_epoch"])
                        < (now_epoch - self.adapter_ttl_seconds),
                    )
                )
            if include_detected and len(result) < limit:
                presence_rows = connection.execute(
                    """SELECT agent_id, session_id, transport, client_name,
                              provider_id, model_id, reasoning_mode, route_class,
                              last_seen_utc, last_seen_epoch
                         FROM agent_presence
                        WHERE scope=? AND last_seen_epoch>=?
                        ORDER BY last_seen_epoch DESC, agent_id, session_id""",
                    (self.scope, now_epoch - self.bridge.presence_ttl_seconds),
                ).fetchall()
                for presence in presence_rows:
                    if len(result) >= limit:
                        break
                    agent_id = str(presence["agent_id"])
                    bridge_session_id = str(presence["session_id"])
                    if (
                        agent_id in INTERNAL_CONTROL_AGENTS
                        or agent_id.startswith("control-room-")
                        or bridge_session_id in registered_bridge_sessions
                    ):
                        continue
                    result.append(_presence_contract(presence))
        return tuple(result)


__all__ = [
    "AUTHORIZED_SOURCE_TYPES",
    "AuthorizedSessionError",
    "AuthorizedSessionRegistry",
    "MAX_AUTHORIZED_EVENTS",
    "MAX_AUTHORIZED_RESPONSE_BYTES",
    "MAX_AUTHORIZED_RESPONSE_EVENTS",
    "MAX_AUTHORIZED_SESSIONS",
]
