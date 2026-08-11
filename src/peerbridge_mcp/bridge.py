"""Auditable local coordination primitives used by the PeerBridge MCP server."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


ZERO_SHA256 = "0" * 64
DEFAULT_PRESENCE_TTL_SECONDS = 120
DEFAULT_LEASE_SECONDS = 900
MAX_LEASE_SECONDS = 86_400
MAX_TEXT_CHARS = 50_000
SCHEMA_VERSION = "3"

SECRET_VALUE = re.compile(
    r"(?i)(?:sk-|ghp_|github_pat_|Bearer\s+)[A-Za-z0-9_\-.]{16,}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
PATCH_DESTRUCTIVE = re.compile(
    r"(?im)^---\s+/dev/null|^\+\+\+\s+/dev/null|^diff --git\s+.*(?:\.git/|\.peerbridge/)"
)
SENSITIVE_PARTS = {
    ".env",
    ".aws",
    ".ssh",
    "credentials",
    "credentials.json",
    "secrets",
    "secret",
    "private_key",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


class BridgeError(Exception):
    """Expected error that is safe to return to an MCP client."""


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def _require_identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise BridgeError(
            f"{label} must contain only letters, digits, dot, underscore, colon or dash"
        )
    return text


def _optional_identifier(value: Any, label: str) -> str | None:
    text = str(value or "").strip()
    return _require_identifier(text, label) if text else None


def _require_text(value: Any, label: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "").strip()
    if not text:
        raise BridgeError(f"{label} is required")
    if len(text) > limit:
        raise BridgeError(f"{label} exceeds {limit} characters")
    if SECRET_VALUE.search(text):
        raise BridgeError(f"{label} appears to contain a credential or private key")
    return text


def _json_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BridgeError(f"{label} must be an array")
    return value


def _path_parts_are_sensitive(path: Path) -> bool:
    for part in path.parts:
        lowered = part.lower()
        if lowered in SENSITIVE_PARTS or Path(lowered).suffix in SENSITIVE_SUFFIXES:
            return True
    return False


class Bridge:
    """One local agent session sharing an append-only SQLite coordination store."""

    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        agent_id: str,
        scope: str = "default",
        *,
        session_id: str | None = None,
        client_name: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        presence_ttl_seconds: int = DEFAULT_PRESENCE_TTL_SECONDS,
        protected_paths: Iterable[str] = (),
    ) -> None:
        self.root = Path(project_root).resolve()
        if not self.root.is_dir():
            raise BridgeError(f"project root does not exist: {self.root}")
        self.db_path = Path(db_path).resolve()
        self.agent_id = _require_identifier(agent_id, "agent_id")
        self.scope = _require_identifier(scope, "scope")
        self.session_id = _require_identifier(
            session_id or uuid.uuid4().hex, "session_id"
        )
        self.client_name = _optional_identifier(client_name, "client_name")
        self.provider_id = _optional_identifier(provider_id, "provider_id")
        self.model_id = _optional_identifier(model_id, "model_id")
        self.presence_ttl_seconds = max(30, min(int(presence_ttl_seconds), 3600))
        self.state_root = self.db_path.parent
        self.draft_root = self.state_root / "drafts"
        defaults = [".git", ".peerbridge"]
        self.protected_paths = tuple(
            sorted({self._normalize_path(item) for item in [*defaults, *protected_paths]})
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.draft_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            for attempt in range(40):
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 39:
                        raise
                    time.sleep(0.25)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    reply_to TEXT,
                    artifact_paths_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    acknowledged_utc TEXT,
                    content_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_inbox
                    ON messages(scope, recipient, sequence);
                CREATE TABLE IF NOT EXISTS message_receipts (
                    scope TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    acknowledged_utc TEXT NOT NULL,
                    PRIMARY KEY(scope, message_id, agent_id),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id)
                );
                CREATE TABLE IF NOT EXISTS consumer_cursors (
                    scope TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    consumer TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    updated_utc TEXT NOT NULL,
                    PRIMARY KEY(scope, channel, consumer)
                );
                CREATE TABLE IF NOT EXISTS tasks (
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
                    review_quorum INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(scope, task_id)
                );
                CREATE TABLE IF NOT EXISTS task_required_peers (
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    PRIMARY KEY(scope, task_id, peer_id),
                    FOREIGN KEY(scope, task_id) REFERENCES tasks(scope, task_id)
                );
                CREATE TABLE IF NOT EXISTS task_paths (
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    access TEXT NOT NULL,
                    path_prefix TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    PRIMARY KEY(scope, task_id, access, path_prefix),
                    FOREIGN KEY(scope, task_id) REFERENCES tasks(scope, task_id)
                );
                CREATE TABLE IF NOT EXISTS work_updates (
                    update_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    artifact_paths_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    update_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_updates_scope_task_time
                    ON work_updates(scope, task_id, created_utc);
                CREATE TABLE IF NOT EXISTS peer_calls (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    requester TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    question TEXT NOT NULL,
                    artifact_paths_json TEXT NOT NULL,
                    request_utc TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_mode TEXT NOT NULL,
                    response TEXT,
                    response_artifact_paths_json TEXT,
                    response_utc TEXT,
                    response_sha256 TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_peer_calls_inbox
                    ON peer_calls(scope, recipient, status, sequence);
                CREATE TABLE IF NOT EXISTS peer_reviews (
                    review_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    findings TEXT NOT NULL,
                    artifact_paths_json TEXT NOT NULL,
                    review_utc TEXT NOT NULL,
                    review_sha256 TEXT NOT NULL,
                    UNIQUE(scope, request_id, reviewer),
                    FOREIGN KEY(request_id) REFERENCES peer_calls(request_id)
                );
                CREATE TABLE IF NOT EXISTS integration_records (
                    record_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    change_summary TEXT NOT NULL,
                    changed_paths_json TEXT NOT NULL,
                    before_hashes_json TEXT NOT NULL,
                    after_hashes_json TEXT NOT NULL,
                    tests TEXT NOT NULL,
                    evidence_paths_json TEXT NOT NULL,
                    approval_mode TEXT NOT NULL,
                    review_ids_json TEXT NOT NULL,
                    recorded_utc TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_integration_records_task_time
                    ON integration_records(scope, task_id, recorded_utc);
                CREATE TABLE IF NOT EXISTS agent_presence (
                    scope TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    client_name TEXT,
                    provider_id TEXT,
                    model_id TEXT,
                    last_seen_utc TEXT NOT NULL,
                    last_seen_epoch REAL NOT NULL,
                    PRIMARY KEY(scope, agent_id, session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_presence_scope_time
                    ON agent_presence(scope, last_seen_epoch);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    task_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    prev_chain_sha256 TEXT NOT NULL,
                    chain_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_scope_time
                    ON events(scope, sequence);
                """
            )
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row and row["value"] not in {"1", "2", SCHEMA_VERSION}:
                raise BridgeError(
                    f"unsupported database schema {row['value']}; expected {SCHEMA_VERSION}"
                )
            task_columns = {
                item["name"] for item in connection.execute("PRAGMA table_info(tasks)")
            }
            if "review_quorum" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN review_quorum INTEGER NOT NULL DEFAULT 1"
                )
            presence_columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(agent_presence)")
            }
            for column in ("client_name", "provider_id", "model_id"):
                if column not in presence_columns:
                    connection.execute(
                        f"ALTER TABLE agent_presence ADD COLUMN {column} TEXT"
                    )
            connection.execute(
                """INSERT OR IGNORE INTO task_required_peers(scope, task_id, peer_id)
                   SELECT scope, task_id, required_peer FROM tasks
                   WHERE required_peer IS NOT NULL AND required_peer != ''"""
            )
            connection.execute(
                """INSERT INTO metadata(key, value) VALUES ('schema_version', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (SCHEMA_VERSION,),
            )

    def _event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        payload: dict[str, Any],
        task_id: str | None = None,
    ) -> dict[str, str]:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT chain_sha256 FROM events WHERE scope=? ORDER BY sequence DESC LIMIT 1",
            (self.scope,),
        ).fetchone()
        prev_sha = previous["chain_sha256"] if previous else ZERO_SHA256
        event_id = uuid.uuid4().hex
        created = utc_now()
        full_payload = {
            **payload,
            "session_id": self.session_id,
            "runtime_identity": {
                "client_name": self.client_name,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
            },
        }
        payload_json = json.dumps(
            full_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        payload_sha = sha256_bytes(payload_json.encode("utf-8"))
        envelope = {
            "event_id": event_id,
            "scope": self.scope,
            "actor": self.agent_id,
            "event_type": event_type,
            "task_id": task_id,
            "payload_sha256": payload_sha,
            "created_utc": created,
            "prev_chain_sha256": prev_sha,
        }
        chain_sha = stable_sha256(envelope)
        connection.execute(
            """INSERT INTO events(
                event_id, scope, actor, event_type, task_id, payload_json,
                created_utc, payload_sha256, prev_chain_sha256, chain_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                self.scope,
                self.agent_id,
                event_type,
                task_id,
                payload_json,
                created,
                payload_sha,
                prev_sha,
                chain_sha,
            ),
        )
        return {"event_id": event_id, "chain_sha256": chain_sha}

    def _normalize_path(self, value: Any) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if not text:
            raise BridgeError("path must be a non-empty project-relative path")
        candidate = Path(text)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise BridgeError("path must remain inside the project root")
        resolved = (self.root / candidate).resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise BridgeError("path escapes the project root") from exc
        normalized = relative.as_posix()
        return "." if normalized in {"", "."} else normalized.rstrip("/")

    def _resolve_path(self, value: Any, *, must_exist: bool = False) -> Path:
        normalized = self._normalize_path(value)
        resolved = self.root if normalized == "." else self.root / normalized
        if must_exist and not resolved.is_file():
            raise BridgeError(f"artifact does not exist as a file: {normalized}")
        return resolved

    @staticmethod
    def _path_overlaps(left: str, right: str) -> bool:
        if left == "." or right == ".":
            return True
        return (
            left == right
            or left.startswith(right + "/")
            or right.startswith(left + "/")
        )

    @staticmethod
    def _path_within(path: str, prefix: str) -> bool:
        return prefix == "." or path == prefix or path.startswith(prefix + "/")

    def _is_protected(self, normalized: str) -> bool:
        path = Path(normalized)
        if _path_parts_are_sensitive(path):
            return True
        return any(self._path_overlaps(normalized, item) for item in self.protected_paths)

    def _clean_artifacts(self, values: Any) -> list[str]:
        clean: list[str] = []
        for value in _json_list(values, "artifact_paths"):
            normalized = self._normalize_path(value)
            if self._is_protected(normalized):
                raise BridgeError(f"protected or sensitive artifact is not exposed: {normalized}")
            resolved = self._resolve_path(normalized, must_exist=True)
            clean.append(resolved.relative_to(self.root).as_posix())
        return clean

    def _clean_task_paths(self, values: Any, access: str) -> list[str]:
        clean: list[str] = []
        for value in _json_list(values, f"{access}_paths"):
            normalized = self._normalize_path(value)
            if access == "write" and self._is_protected(normalized):
                raise BridgeError(f"write scope is protected or sensitive: {normalized}")
            clean.append(normalized)
        return sorted(set(clean))

    def touch_presence(self, transport: str = "stdio") -> None:
        now_utc = utc_now()
        now_epoch = time.time()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_presence(
                    scope, agent_id, session_id, transport, client_name, provider_id,
                    model_id, last_seen_utc, last_seen_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, agent_id, session_id) DO UPDATE SET
                    transport=excluded.transport,
                    client_name=excluded.client_name,
                    provider_id=excluded.provider_id,
                    model_id=excluded.model_id,
                    last_seen_utc=excluded.last_seen_utc,
                    last_seen_epoch=excluded.last_seen_epoch""",
                (
                    self.scope,
                    self.agent_id,
                    self.session_id,
                    transport,
                    self.client_name,
                    self.provider_id,
                    self.model_id,
                    now_utc,
                    now_epoch,
                ),
            )

    def clear_presence(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM agent_presence WHERE scope=? AND agent_id=? AND session_id=?",
                (self.scope, self.agent_id, self.session_id),
            )

    def presence_snapshot(self) -> dict[str, Any]:
        cutoff = time.time() - self.presence_ttl_seconds
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT agent_id, session_id, transport, client_name, provider_id,
                          model_id, last_seen_utc
                   FROM agent_presence
                   WHERE scope=? AND last_seen_epoch>=?
                   ORDER BY agent_id, last_seen_epoch DESC""",
                (self.scope, cutoff),
            ).fetchall()
        sessions = [dict(row) for row in rows]
        return {
            "online_agents": sorted({row["agent_id"] for row in rows}),
            "online_sessions": sessions,
            "presence_ttl_seconds": self.presence_ttl_seconds,
            "observed_utc": utc_now(),
        }

    def _expire_leases(self, connection: sqlite3.Connection) -> None:
        now_epoch = time.time()
        rows = connection.execute(
            """SELECT task_id, claimed_by FROM tasks
               WHERE scope=? AND status='claimed' AND lease_expires_epoch<=?""",
            (self.scope, now_epoch),
        ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE tasks SET status='open', claimed_by=NULL,
                   claimed_session_id=NULL, lease_token_sha256=NULL,
                   lease_expires_epoch=NULL, claimed_utc=NULL, updated_utc=?
                   WHERE scope=? AND task_id=?""",
                (utc_now(), self.scope, row["task_id"]),
            )
            self._event(
                connection,
                "task.lease_expired",
                {"previous_holder": row["claimed_by"]},
                row["task_id"],
            )

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        lease_token: Any,
    ) -> sqlite3.Row:
        token = str(lease_token or "")
        if not token:
            raise BridgeError("lease_token is required")
        row = connection.execute(
            "SELECT * FROM tasks WHERE scope=? AND task_id=?",
            (self.scope, task_id),
        ).fetchone()
        if row is None:
            raise BridgeError("task not found")
        if row["status"] != "claimed" or row["lease_expires_epoch"] is None:
            raise BridgeError("task has no active lease")
        if row["lease_expires_epoch"] <= time.time():
            raise BridgeError("task lease has expired")
        if row["claimed_by"] != self.agent_id:
            raise BridgeError(f"task is leased by {row['claimed_by']}")
        if not secrets.compare_digest(
            row["lease_token_sha256"], sha256_bytes(token.encode("utf-8"))
        ):
            raise BridgeError("lease token does not match")
        return row

    def status(self, _args: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks WHERE scope=? GROUP BY status",
                    (self.scope,),
                ).fetchall()
            }
            message_count = connection.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE scope=?", (self.scope,)
            ).fetchone()["n"]
            event_count = connection.execute(
                "SELECT COUNT(*) AS n FROM events WHERE scope=?", (self.scope,)
            ).fetchone()["n"]
        return {
            "scope": self.scope,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "runtime_identity": {
                "client_name": self.client_name,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
            },
            "transport": "stdio",
            "network_listener": False,
            "database": str(self.db_path),
            "message_count": message_count,
            "task_counts": counts,
            "audit_event_count": event_count,
            "schema_version": SCHEMA_VERSION,
            "presence": self.presence_snapshot(),
        }

    def send_message(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_recipient = str(args.get("recipient") or "").strip()
        recipient = "*" if raw_recipient == "*" else _require_identifier(raw_recipient, "recipient")
        task_id = _require_identifier(args.get("task_id"), "task_id")
        subject = _require_text(args.get("subject"), "subject", limit=500)
        body = _require_text(args.get("body"), "body")
        priority = str(args.get("priority", "normal")).strip().lower()
        if priority not in {"low", "normal", "high", "critical"}:
            raise BridgeError("priority must be low, normal, high or critical")
        reply_to = str(args.get("reply_to") or "").strip() or None
        artifacts = self._clean_artifacts(args.get("artifact_paths", []))
        message_id = uuid.uuid4().hex
        created = utc_now()
        content = {
            "message_id": message_id,
            "scope": self.scope,
            "task_id": task_id,
            "sender": self.agent_id,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "priority": priority,
            "reply_to": reply_to,
            "artifact_paths": artifacts,
            "created_utc": created,
        }
        content_sha = stable_sha256(content)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT INTO messages(
                    message_id, scope, task_id, sender, recipient, subject, body,
                    priority, reply_to, artifact_paths_json, created_utc,
                    acknowledged_utc, content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    message_id,
                    self.scope,
                    task_id,
                    self.agent_id,
                    recipient,
                    subject,
                    body,
                    priority,
                    reply_to,
                    json.dumps(artifacts, ensure_ascii=False),
                    created,
                    content_sha,
                ),
            )
            self._event(
                connection,
                "message.sent",
                {
                    "message_id": message_id,
                    "sequence": cursor.lastrowid,
                    "content_sha256": content_sha,
                },
                task_id,
            )
        return {
            "message_id": message_id,
            "sequence": cursor.lastrowid,
            "content_sha256": content_sha,
            "created_utc": created,
        }

    def _consumer_cursor(
        self, connection: sqlite3.Connection, channel: str, consumer: str
    ) -> int:
        row = connection.execute(
            """SELECT position FROM consumer_cursors
               WHERE scope=? AND channel=? AND consumer=?""",
            (self.scope, channel, consumer),
        ).fetchone()
        return int(row["position"]) if row else 0

    def poll_messages(self, args: dict[str, Any]) -> dict[str, Any]:
        consumer = _require_identifier(
            args.get("agent_id", self.agent_id), "agent_id"
        )
        limit = max(1, min(int(args.get("limit", 50)), 500))
        include_sent = bool(args.get("include_sent", False))
        with self._connect() as connection:
            stored_cursor = self._consumer_cursor(connection, "messages", consumer)
            cursor = int(args.get("after_cursor", stored_cursor))
            sender_clause = " OR m.sender=?" if include_sent else ""
            params: list[Any] = [
                consumer,
                self.scope,
                cursor,
                consumer,
            ]
            if include_sent:
                params.append(consumer)
            params.append(limit)
            rows = connection.execute(
                f"""SELECT m.*, CASE WHEN r.message_id IS NULL THEN 0 ELSE 1 END AS acknowledged
                    FROM messages m
                    LEFT JOIN message_receipts r
                      ON r.scope=m.scope AND r.message_id=m.message_id AND r.agent_id=?
                    WHERE m.scope=? AND m.sequence>?
                      AND (m.recipient=? OR m.recipient='*'{sender_clause})
                    ORDER BY m.sequence ASC LIMIT ?""",
                tuple(params),
            ).fetchall()
        messages = []
        for row in rows:
            item = dict(row)
            item["artifact_paths"] = json.loads(item.pop("artifact_paths_json"))
            item["acknowledged"] = bool(item["acknowledged"])
            messages.append(item)
        return {
            "messages": messages,
            "count": len(messages),
            "stored_cursor": stored_cursor,
            "requested_cursor": cursor,
            "next_cursor": messages[-1]["sequence"] if messages else cursor,
        }

    def ack_message(self, args: dict[str, Any]) -> dict[str, Any]:
        message_id = _require_identifier(args.get("message_id"), "message_id")
        consumer = _require_identifier(
            args.get("agent_id", self.agent_id), "agent_id"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT sequence, recipient FROM messages WHERE scope=? AND message_id=?",
                (self.scope, message_id),
            ).fetchone()
            if row is None or row["recipient"] not in {consumer, "*"}:
                raise BridgeError("message is not addressed to this consumer")
            acknowledged = utc_now()
            connection.execute(
                """INSERT OR IGNORE INTO message_receipts(
                    scope, message_id, agent_id, acknowledged_utc
                ) VALUES (?, ?, ?, ?)""",
                (self.scope, message_id, consumer, acknowledged),
            )
            if row["recipient"] != "*":
                connection.execute(
                    "UPDATE messages SET acknowledged_utc=COALESCE(acknowledged_utc, ?) WHERE message_id=?",
                    (acknowledged, message_id),
                )
            current = self._consumer_cursor(connection, "messages", consumer)
            eligible = connection.execute(
                """SELECT m.sequence,
                          CASE WHEN r.message_id IS NULL THEN 0 ELSE 1 END AS acknowledged
                   FROM messages m
                   LEFT JOIN message_receipts r
                     ON r.scope=m.scope AND r.message_id=m.message_id AND r.agent_id=?
                   WHERE m.scope=? AND m.sequence>? AND (m.recipient=? OR m.recipient='*')
                   ORDER BY m.sequence ASC""",
                (consumer, self.scope, current, consumer),
            ).fetchall()
            advanced = current
            for item in eligible:
                if not item["acknowledged"]:
                    break
                advanced = int(item["sequence"])
            connection.execute(
                """INSERT INTO consumer_cursors(scope, channel, consumer, position, updated_utc)
                   VALUES (?, 'messages', ?, ?, ?)
                   ON CONFLICT(scope, channel, consumer) DO UPDATE SET
                     position=excluded.position, updated_utc=excluded.updated_utc""",
                (self.scope, consumer, advanced, utc_now()),
            )
            self._event(
                connection,
                "message.acknowledged",
                {"message_id": message_id, "consumer": consumer, "cursor": advanced},
            )
        return {
            "message_id": message_id,
            "acknowledged": True,
            "consumer": consumer,
            "cursor": advanced,
        }

    def claim_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        summary = _require_text(args.get("summary"), "summary", limit=10_000)
        read_paths = self._clean_task_paths(args.get("read_paths", []), "read")
        write_paths = self._clean_task_paths(args.get("write_paths", []), "write")
        lease_seconds = max(
            30,
            min(int(args.get("lease_seconds", DEFAULT_LEASE_SECONDS)), MAX_LEASE_SECONDS),
        )
        approval_mode = str(args.get("approval_mode", "presence_aware")).lower()
        if approval_mode not in {
            "solo_allowed",
            "two_party_required",
            "presence_aware",
            "quorum_required",
        }:
            raise BridgeError(
                "approval_mode must be solo_allowed, two_party_required, "
                "presence_aware or quorum_required"
            )
        required_peer = str(args.get("required_peer") or "").strip() or None
        if required_peer:
            required_peer = _require_identifier(required_peer, "required_peer")
            if required_peer == self.agent_id:
                raise BridgeError("required_peer must be another agent")
        required_peers = []
        for item in _json_list(args.get("required_peers"), "required_peers"):
            peer = _require_identifier(item, "required_peers item")
            if peer == self.agent_id:
                raise BridgeError("required_peers must contain only other agents")
            if peer not in required_peers:
                required_peers.append(peer)
        if required_peer and required_peer not in required_peers:
            required_peers.append(required_peer)
        required_peers.sort()
        try:
            review_quorum = int(args.get("review_quorum", 1))
        except (TypeError, ValueError) as exc:
            raise BridgeError("review_quorum must be a positive integer") from exc
        if review_quorum < 1:
            raise BridgeError("review_quorum must be a positive integer")
        if required_peers and review_quorum > len(required_peers):
            raise BridgeError("review_quorum cannot exceed required_peers count")
        if approval_mode == "quorum_required" and not required_peers:
            raise BridgeError("quorum_required needs at least one required_peers entry")
        owner = str(args.get("owner") or "").strip() or None
        now = utc_now()
        now_epoch = time.time()
        expires = now_epoch + lease_seconds
        token = secrets.token_urlsafe(32)
        token_sha = sha256_bytes(token.encode("utf-8"))
        task_content = {
            "scope": self.scope,
            "task_id": task_id,
            "summary": summary,
            "owner": owner,
            "read_paths": read_paths,
            "write_paths": write_paths,
            "approval_mode": approval_mode,
            "required_peer": required_peer,
            "required_peers": required_peers,
            "review_quorum": review_quorum,
        }
        task_sha = stable_sha256(task_content)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_leases(connection)
            existing = connection.execute(
                "SELECT * FROM tasks WHERE scope=? AND task_id=?",
                (self.scope, task_id),
            ).fetchone()
            if existing and existing["status"] == "complete":
                raise BridgeError("task is already complete")
            if existing and existing["status"] == "claimed":
                raise BridgeError(
                    f"task already has an active lease held by {existing['claimed_by']}"
                )
            requested = [("read", item) for item in read_paths] + [
                ("write", item) for item in write_paths
            ]
            active = connection.execute(
                """SELECT t.task_id, t.claimed_by, p.access, p.path_prefix
                   FROM tasks t JOIN task_paths p
                     ON p.scope=t.scope AND p.task_id=t.task_id
                   WHERE t.scope=? AND t.status='claimed' AND t.lease_expires_epoch>?""",
                (self.scope, now_epoch),
            ).fetchall()
            conflicts = []
            for new_access, new_path in requested:
                for row in active:
                    if row["task_id"] == task_id:
                        continue
                    if new_access == "read" and row["access"] == "read":
                        continue
                    if self._path_overlaps(new_path, row["path_prefix"]):
                        conflicts.append(
                            {
                                "task_id": row["task_id"],
                                "claimed_by": row["claimed_by"],
                                "existing_access": row["access"],
                                "existing_path": row["path_prefix"],
                                "requested_access": new_access,
                                "requested_path": new_path,
                            }
                        )
            if conflicts:
                raise BridgeError(
                    "task path scope conflicts with an active lease: "
                    + json.dumps(conflicts, ensure_ascii=False, sort_keys=True)
                )
            if existing:
                connection.execute(
                    """UPDATE tasks SET summary=?, owner=?, status='claimed', claimed_by=?,
                       claimed_session_id=?, lease_token_sha256=?, lease_expires_epoch=?,
                       claimed_utc=?, updated_utc=?, task_sha256=?, approval_mode=?,
                       required_peer=?, review_quorum=? WHERE scope=? AND task_id=?""",
                    (
                        summary,
                        owner,
                        self.agent_id,
                        self.session_id,
                        token_sha,
                        expires,
                        now,
                        now,
                        task_sha,
                        approval_mode,
                        required_peer,
                        review_quorum,
                        self.scope,
                        task_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM task_paths WHERE scope=? AND task_id=?",
                    (self.scope, task_id),
                )
            else:
                connection.execute(
                    """INSERT INTO tasks(
                        scope, task_id, summary, owner, status, claimed_by,
                        claimed_session_id, lease_token_sha256, lease_expires_epoch,
                        claimed_utc, created_utc, updated_utc, task_sha256,
                        approval_mode, required_peer, review_quorum
                    ) VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.scope,
                        task_id,
                        summary,
                        owner,
                        self.agent_id,
                        self.session_id,
                        token_sha,
                        expires,
                        now,
                        now,
                        now,
                        task_sha,
                        approval_mode,
                        required_peer,
                        review_quorum,
                    ),
                )
            for access, path_prefix in requested:
                connection.execute(
                    "INSERT INTO task_paths VALUES (?, ?, ?, ?, ?)",
                    (self.scope, task_id, access, path_prefix, now),
                )
            connection.execute(
                "DELETE FROM task_required_peers WHERE scope=? AND task_id=?",
                (self.scope, task_id),
            )
            connection.executemany(
                "INSERT INTO task_required_peers(scope, task_id, peer_id) VALUES (?, ?, ?)",
                [(self.scope, task_id, peer) for peer in required_peers],
            )
            self._event(
                connection,
                "task.claimed",
                {
                    "task_sha256": task_sha,
                    "lease_seconds": lease_seconds,
                    "read_paths": read_paths,
                    "write_paths": write_paths,
                    "approval_mode": approval_mode,
                    "required_peer": required_peer,
                    "required_peers": required_peers,
                    "review_quorum": review_quorum,
                },
                task_id,
            )
        return {
            "task_id": task_id,
            "claimed_by": self.agent_id,
            "lease_token": token,
            "lease_expires_epoch": expires,
            "lease_seconds": lease_seconds,
            "read_paths": read_paths,
            "write_paths": write_paths,
            "task_sha256": task_sha,
            "approval_mode": approval_mode,
            "required_peers": required_peers,
            "review_quorum": review_quorum,
        }

    def renew_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        lease_seconds = max(
            30,
            min(int(args.get("lease_seconds", DEFAULT_LEASE_SECONDS)), MAX_LEASE_SECONDS),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, args.get("lease_token"))
            expires = time.time() + lease_seconds
            connection.execute(
                "UPDATE tasks SET lease_expires_epoch=?, updated_utc=? WHERE scope=? AND task_id=?",
                (expires, utc_now(), self.scope, task_id),
            )
            self._event(
                connection,
                "task.lease_renewed",
                {"lease_seconds": lease_seconds, "lease_expires_epoch": expires},
                task_id,
            )
        return {"task_id": task_id, "lease_expires_epoch": expires}

    def release_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        status = str(args.get("status", "open")).lower()
        if status not in {"open", "blocked"}:
            raise BridgeError("release status must be open or blocked")
        reason = str(args.get("reason") or "").strip()
        if reason and SECRET_VALUE.search(reason):
            raise BridgeError("reason appears to contain a credential")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, args.get("lease_token"))
            connection.execute(
                """UPDATE tasks SET status=?, claimed_by=NULL, claimed_session_id=NULL,
                   lease_token_sha256=NULL, lease_expires_epoch=NULL, claimed_utc=NULL,
                   updated_utc=? WHERE scope=? AND task_id=?""",
                (status, utc_now(), self.scope, task_id),
            )
            self._event(
                connection,
                "task.released",
                {"status": status, "reason_sha256": stable_sha256(reason)},
                task_id,
            )
        return {"task_id": task_id, "status": status, "released": True}

    def announce_work(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        summary = _require_text(args.get("summary"), "summary", limit=10_000)
        status = str(args.get("status", "working")).lower()
        if status not in {"working", "waiting", "review"}:
            raise BridgeError("status must be working, waiting or review")
        artifacts = self._clean_artifacts(args.get("artifact_paths", []))
        update_id = uuid.uuid4().hex
        created = utc_now()
        content = {
            "update_id": update_id,
            "scope": self.scope,
            "task_id": task_id,
            "agent_id": self.agent_id,
            "status": status,
            "summary": summary,
            "artifact_paths": artifacts,
            "created_utc": created,
        }
        update_sha = stable_sha256(content)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, args.get("lease_token"))
            connection.execute(
                "INSERT INTO work_updates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    update_id,
                    self.scope,
                    task_id,
                    self.agent_id,
                    status,
                    summary,
                    json.dumps(artifacts, ensure_ascii=False),
                    created,
                    update_sha,
                ),
            )
            connection.execute(
                "UPDATE tasks SET summary=?, updated_utc=? WHERE scope=? AND task_id=?",
                (summary, created, self.scope, task_id),
            )
            self._event(
                connection,
                "work.announced",
                {"update_id": update_id, "status": status, "update_sha256": update_sha},
                task_id,
            )
        return {
            "update_id": update_id,
            "task_id": task_id,
            "status": status,
            "update_sha256": update_sha,
        }

    def workboard(self, args: dict[str, Any]) -> dict[str, Any]:
        include_completed = bool(args.get("include_completed", False))
        limit = max(1, min(int(args.get("limit", 100)), 500))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_leases(connection)
            status_clause = "" if include_completed else "AND t.status!='complete'"
            rows = connection.execute(
                f"""SELECT t.* FROM tasks t WHERE t.scope=? {status_clause}
                    ORDER BY t.updated_utc DESC, t.task_id ASC LIMIT ?""",
                (self.scope, limit),
            ).fetchall()
            tasks = []
            for row in rows:
                paths = connection.execute(
                    "SELECT access, path_prefix FROM task_paths WHERE scope=? AND task_id=? ORDER BY access, path_prefix",
                    (self.scope, row["task_id"]),
                ).fetchall()
                latest = connection.execute(
                    """SELECT * FROM work_updates WHERE scope=? AND task_id=?
                       ORDER BY created_utc DESC, rowid DESC LIMIT 1""",
                    (self.scope, row["task_id"]),
                ).fetchone()
                item = dict(row)
                item.pop("lease_token_sha256", None)
                item["paths"] = [dict(path) for path in paths]
                item["latest_update"] = dict(latest) if latest else None
                if item["latest_update"]:
                    item["latest_update"]["artifact_paths"] = json.loads(
                        item["latest_update"].pop("artifact_paths_json")
                    )
                tasks.append(item)
        return {
            "scope": self.scope,
            "tasks": tasks,
            "count": len(tasks),
            "presence": self.presence_snapshot(),
        }

    def request_review(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        recipient = _require_identifier(args.get("recipient"), "recipient")
        if recipient == self.agent_id:
            raise BridgeError("review recipient must be another agent")
        question = _require_text(args.get("question"), "question", limit=20_000)
        artifacts = self._clean_artifacts(args.get("artifact_paths", []))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._require_lease(connection, task_id, args.get("lease_token"))
            request_id = uuid.uuid4().hex
            created = utc_now()
            content = {
                "request_id": request_id,
                "scope": self.scope,
                "task_id": task_id,
                "requester": self.agent_id,
                "recipient": recipient,
                "question": question,
                "artifact_paths": artifacts,
                "request_utc": created,
                "approval_mode": task["approval_mode"],
            }
            request_sha = stable_sha256(content)
            cursor = connection.execute(
                """INSERT INTO peer_calls(
                    request_id, scope, task_id, requester, recipient, question,
                    artifact_paths_json, request_utc, request_sha256, status,
                    approval_mode, response, response_artifact_paths_json,
                    response_utc, response_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL, NULL, NULL, NULL)""",
                (
                    request_id,
                    self.scope,
                    task_id,
                    self.agent_id,
                    recipient,
                    question,
                    json.dumps(artifacts, ensure_ascii=False),
                    created,
                    request_sha,
                    task["approval_mode"],
                ),
            )
            self._event(
                connection,
                "peer.requested",
                {
                    "request_id": request_id,
                    "sequence": cursor.lastrowid,
                    "request_sha256": request_sha,
                    "recipient": recipient,
                },
                task_id,
            )
        return {
            "request_id": request_id,
            "sequence": cursor.lastrowid,
            "request_sha256": request_sha,
            "status": "open",
        }

    def poll_reviews(self, args: dict[str, Any]) -> dict[str, Any]:
        agent = _require_identifier(args.get("agent_id", self.agent_id), "agent_id")
        after = max(0, int(args.get("after_cursor", 0)))
        limit = max(1, min(int(args.get("limit", 50)), 500))
        include_closed = bool(args.get("include_closed", False))
        status_clause = "" if include_closed else "AND status IN ('open', 'responded')"
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM peer_calls
                    WHERE scope=? AND sequence>? AND (recipient=? OR requester=?)
                    {status_clause} ORDER BY sequence ASC LIMIT ?""",
                (self.scope, after, agent, agent, limit),
            ).fetchall()
        calls = []
        for row in rows:
            item = dict(row)
            item["artifact_paths"] = json.loads(item.pop("artifact_paths_json"))
            item["response_artifact_paths"] = json.loads(
                item.pop("response_artifact_paths_json") or "[]"
            )
            calls.append(item)
        return {
            "calls": calls,
            "count": len(calls),
            "next_cursor": calls[-1]["sequence"] if calls else after,
        }

    def submit_review(self, args: dict[str, Any]) -> dict[str, Any]:
        request_id = _require_identifier(args.get("request_id"), "request_id")
        verdict = str(args.get("verdict", "")).lower()
        if verdict not in {"approved", "changes_requested", "blocked"}:
            raise BridgeError("verdict must be approved, changes_requested or blocked")
        try:
            score = int(args.get("score"))
        except (TypeError, ValueError) as exc:
            raise BridgeError("score must be an integer from 0 to 100") from exc
        if not 0 <= score <= 100:
            raise BridgeError("score must be an integer from 0 to 100")
        findings = _require_text(args.get("findings"), "findings")
        response_text = str(args.get("response") or "").strip()
        if response_text and SECRET_VALUE.search(response_text):
            raise BridgeError("response appears to contain a credential")
        artifacts = self._clean_artifacts(args.get("artifact_paths", []))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            call = connection.execute(
                "SELECT * FROM peer_calls WHERE scope=? AND request_id=?",
                (self.scope, request_id),
            ).fetchone()
            if call is None:
                raise BridgeError("review request not found")
            if call["recipient"] != self.agent_id:
                raise BridgeError("only the addressed peer may submit this review")
            review_id = uuid.uuid4().hex
            reviewed = utc_now()
            content = {
                "review_id": review_id,
                "scope": self.scope,
                "request_id": request_id,
                "task_id": call["task_id"],
                "reviewer": self.agent_id,
                "verdict": verdict,
                "score": score,
                "findings": findings,
                "artifact_paths": artifacts,
                "review_utc": reviewed,
            }
            review_sha = stable_sha256(content)
            try:
                connection.execute(
                    "INSERT INTO peer_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        review_id,
                        self.scope,
                        request_id,
                        call["task_id"],
                        self.agent_id,
                        verdict,
                        score,
                        findings,
                        json.dumps(artifacts, ensure_ascii=False),
                        reviewed,
                        review_sha,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BridgeError("this peer already reviewed the request") from exc
            if response_text:
                response_sha = stable_sha256(
                    {
                        "request_id": request_id,
                        "response": response_text,
                        "artifact_paths": artifacts,
                    }
                )
                connection.execute(
                    """UPDATE peer_calls SET status='responded', response=?,
                       response_artifact_paths_json=?, response_utc=?, response_sha256=?
                       WHERE scope=? AND request_id=?""",
                    (
                        response_text,
                        json.dumps(artifacts, ensure_ascii=False),
                        reviewed,
                        response_sha,
                        self.scope,
                        request_id,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE peer_calls SET status='responded', response_utc=? WHERE scope=? AND request_id=?",
                    (reviewed, self.scope, request_id),
                )
            self._event(
                connection,
                "peer.reviewed",
                {
                    "request_id": request_id,
                    "review_id": review_id,
                    "review_sha256": review_sha,
                    "verdict": verdict,
                },
                call["task_id"],
            )
        return {
            "request_id": request_id,
            "review_id": review_id,
            "review_sha256": review_sha,
            "verdict": verdict,
            "score": score,
        }

    def review_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        with self._connect() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE scope=? AND task_id=?",
                (self.scope, task_id),
            ).fetchone()
            if task is None:
                raise BridgeError("task not found")
            rows = connection.execute(
                """SELECT r.*, c.recipient, c.requester, c.status AS request_status
                   FROM peer_reviews r JOIN peer_calls c ON c.request_id=r.request_id
                   WHERE r.scope=? AND r.task_id=? ORDER BY r.review_utc ASC""",
                (self.scope, task_id),
            ).fetchall()
            required_peer_rows = connection.execute(
                """SELECT peer_id FROM task_required_peers
                   WHERE scope=? AND task_id=? ORDER BY peer_id""",
                (self.scope, task_id),
            ).fetchall()
        reviews = []
        for row in rows:
            item = dict(row)
            item["artifact_paths"] = json.loads(item.pop("artifact_paths_json"))
            reviews.append(item)
        presence = self.presence_snapshot()
        online = {
            agent
            for agent in presence["online_agents"]
            if agent not in {task["claimed_by"], "human-operator"}
        }
        approved = {row["reviewer"] for row in rows if row["verdict"] == "approved"}
        required_peer = task["required_peer"]
        required_peers = {row["peer_id"] for row in required_peer_rows}
        if required_peer:
            required_peers.add(required_peer)
        review_quorum = max(1, int(task["review_quorum"] or 1))
        mode = task["approval_mode"]
        if mode == "solo_allowed":
            ready = True
            reason = "solo_allowed"
        elif mode == "two_party_required":
            ready = required_peer in approved if required_peer else bool(approved)
            reason = "required_peer_approved" if ready else "peer_approval_required"
        elif mode == "quorum_required":
            eligible = required_peers or approved
            approved_count = len(eligible.intersection(approved))
            ready = approved_count >= review_quorum
            reason = "review_quorum_met" if ready else "review_quorum_pending"
        elif required_peers.intersection(online):
            eligible = required_peers.intersection(online)
            needed = min(review_quorum, len(eligible))
            ready = len(eligible.intersection(approved)) >= needed
            reason = "online_review_quorum_met" if ready else "online_review_quorum_pending"
        elif online:
            needed = min(review_quorum, len(online))
            ready = len(online.intersection(approved)) >= needed
            reason = "online_peer_quorum_met" if ready else "online_peer_quorum_pending"
        else:
            ready = True
            reason = "peer_offline_solo_fallback"
        return {
            "task_id": task_id,
            "approval_mode": mode,
            "required_peer": required_peer,
            "required_peers": sorted(required_peers),
            "review_quorum": review_quorum,
            "reviews": reviews,
            "approved_reviewers": sorted(approved),
            "online_peer_agents": sorted(online),
            "ready_for_completion": ready,
            "policy_reason": reason,
        }

    def read_artifact(self, args: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_path(args.get("path"))
        if self._is_protected(normalized):
            raise BridgeError("protected or sensitive files are not exposed")
        resolved = self._resolve_path(normalized, must_exist=True)
        max_bytes = max(1, min(int(args.get("max_bytes", 100_000)), 500_000))
        data = resolved.read_bytes()
        result: dict[str, Any] = {
            "path": normalized,
            "bytes": len(data),
            "sha256": sha256_bytes(data),
            "truncated": len(data) > max_bytes,
        }
        try:
            result["text"] = data[:max_bytes].decode("utf-8")
        except UnicodeDecodeError:
            result["binary"] = True
        return result

    def hash_artifact(self, args: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_path(args.get("path"))
        if self._is_protected(normalized):
            raise BridgeError("protected or sensitive files are not exposed")
        resolved = self._resolve_path(normalized, must_exist=True)
        data = resolved.read_bytes()
        return {"path": normalized, "bytes": len(data), "sha256": sha256_bytes(data)}

    def record_proof(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        summary = _require_text(args.get("change_summary"), "change_summary", limit=20_000)
        tests = _require_text(args.get("tests"), "tests", limit=20_000)
        changed_paths = [
            self._normalize_path(item)
            for item in _json_list(args.get("changed_paths", []), "changed_paths")
        ]
        if any(self._is_protected(path) for path in changed_paths):
            raise BridgeError("changed_paths contains a protected or sensitive target")
        evidence_paths = self._clean_artifacts(args.get("evidence_paths", []))
        if not changed_paths and not evidence_paths:
            raise BridgeError("proof needs at least one changed path or evidence artifact")
        before_hashes = args.get("before_hashes", {})
        if not isinstance(before_hashes, dict):
            raise BridgeError("before_hashes must be an object")
        review_ids = [
            _require_identifier(item, "review_id")
            for item in _json_list(args.get("review_ids", []), "review_ids")
        ]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._require_lease(connection, task_id, args.get("lease_token"))
            write_scopes = [
                row["path_prefix"]
                for row in connection.execute(
                    "SELECT path_prefix FROM task_paths WHERE scope=? AND task_id=? AND access='write'",
                    (self.scope, task_id),
                ).fetchall()
            ]
            for path in changed_paths:
                if not any(self._path_within(path, prefix) for prefix in write_scopes):
                    raise BridgeError(f"changed path is outside the task write scope: {path}")
            after_hashes: dict[str, str] = {}
            for path in changed_paths:
                resolved = self._resolve_path(path, must_exist=True)
                after_hashes[path] = sha256_bytes(resolved.read_bytes())
            evidence_hashes = {
                path: sha256_bytes(self._resolve_path(path, must_exist=True).read_bytes())
                for path in evidence_paths
            }
            record_id = uuid.uuid4().hex
            recorded = utc_now()
            content = {
                "record_id": record_id,
                "scope": self.scope,
                "task_id": task_id,
                "actor": self.agent_id,
                "change_summary": summary,
                "changed_paths": changed_paths,
                "before_hashes": before_hashes,
                "after_hashes": after_hashes,
                "tests": tests,
                "evidence_paths": evidence_paths,
                "evidence_hashes": evidence_hashes,
                "approval_mode": task["approval_mode"],
                "review_ids": review_ids,
                "recorded_utc": recorded,
            }
            record_sha = stable_sha256(content)
            connection.execute(
                """INSERT INTO integration_records(
                    record_id, scope, task_id, actor, change_summary,
                    changed_paths_json, before_hashes_json, after_hashes_json,
                    tests, evidence_paths_json, approval_mode, review_ids_json,
                    recorded_utc, record_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id,
                    self.scope,
                    task_id,
                    self.agent_id,
                    summary,
                    json.dumps(changed_paths, ensure_ascii=False),
                    json.dumps(before_hashes, ensure_ascii=False, sort_keys=True),
                    json.dumps(after_hashes, ensure_ascii=False, sort_keys=True),
                    tests,
                    json.dumps(
                        {"paths": evidence_paths, "hashes": evidence_hashes},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    task["approval_mode"],
                    json.dumps(review_ids, ensure_ascii=False),
                    recorded,
                    record_sha,
                ),
            )
            self._event(
                connection,
                "proof.recorded",
                {"record_id": record_id, "record_sha256": record_sha},
                task_id,
            )
        return {
            "record_id": record_id,
            "record_sha256": record_sha,
            "after_hashes": after_hashes,
            "evidence_hashes": evidence_hashes,
            "bridge_write_performed": False,
        }

    def change_log(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args.get("task_id") or "").strip()
        if task_id:
            task_id = _require_identifier(task_id, "task_id")
        limit = max(1, min(int(args.get("limit", 50)), 500))
        where = "AND task_id=?" if task_id else ""
        params: tuple[Any, ...] = (
            (self.scope, task_id, limit) if task_id else (self.scope, limit)
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM integration_records WHERE scope=? {where}
                    ORDER BY recorded_utc DESC, rowid DESC LIMIT ?""",
                params,
            ).fetchall()
        records = []
        for row in rows:
            item = dict(row)
            for source, target in (
                ("changed_paths_json", "changed_paths"),
                ("before_hashes_json", "before_hashes"),
                ("after_hashes_json", "after_hashes"),
                ("evidence_paths_json", "evidence"),
                ("review_ids_json", "review_ids"),
            ):
                item[target] = json.loads(item.pop(source))
            records.append(item)
        return {"records": records, "count": len(records)}

    def _proofs_are_live(
        self, connection: sqlite3.Connection, task_id: str
    ) -> tuple[bool, list[dict[str, Any]]]:
        rows = connection.execute(
            "SELECT * FROM integration_records WHERE scope=? AND task_id=? ORDER BY recorded_utc ASC",
            (self.scope, task_id),
        ).fetchall()
        checks = []
        for row in rows:
            after_hashes = json.loads(row["after_hashes_json"])
            evidence = json.loads(row["evidence_paths_json"])
            drift = []
            for path, expected in {
                **after_hashes,
                **evidence.get("hashes", {}),
            }.items():
                resolved = self._resolve_path(path)
                live = sha256_bytes(resolved.read_bytes()) if resolved.is_file() else None
                if live != expected:
                    drift.append({"path": path, "expected": expected, "live": live})
            checks.append(
                {
                    "record_id": row["record_id"],
                    "record_sha256": row["record_sha256"],
                    "tests": row["tests"],
                    "drift": drift,
                }
            )
        return bool(rows) and all(not item["drift"] and item["tests"] for item in checks), checks

    def complete_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, args.get("lease_token"))
            proof_ready, proof_checks = self._proofs_are_live(connection, task_id)
            if not proof_ready:
                raise BridgeError(
                    "task proof is missing or has live hash drift: "
                    + json.dumps(proof_checks, ensure_ascii=False, sort_keys=True)
                )
        review = self.review_summary({"task_id": task_id})
        if not review["ready_for_completion"]:
            raise BridgeError(
                f"review gate is not ready: {review['policy_reason']}"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, args.get("lease_token"))
            proof_ready, proof_checks = self._proofs_are_live(connection, task_id)
            if not proof_ready:
                raise BridgeError("proof changed while completing the task")
            completed = utc_now()
            connection.execute(
                """UPDATE tasks SET status='complete', claimed_by=NULL,
                   claimed_session_id=NULL, lease_token_sha256=NULL,
                   lease_expires_epoch=NULL, claimed_utc=NULL, updated_utc=?
                   WHERE scope=? AND task_id=?""",
                (completed, self.scope, task_id),
            )
            event = self._event(
                connection,
                "task.completed",
                {
                    "proof_records": [item["record_sha256"] for item in proof_checks],
                    "review_policy_reason": review["policy_reason"],
                    "approved_reviewers": review["approved_reviewers"],
                },
                task_id,
            )
        return {
            "task_id": task_id,
            "status": "complete",
            "completed_utc": completed,
            "proof_checks": proof_checks,
            "review": review,
            "completion_chain_sha256": event["chain_sha256"],
        }

    def _write_draft(self, task_id: str, filename: str, data: bytes) -> dict[str, Any]:
        safe_task = _require_identifier(task_id, "task_id").replace(":", "_")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
            raise BridgeError("draft filename contains unsupported characters")
        destination = self.draft_root / self.scope / safe_task / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{filename}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        return {
            "path": destination.relative_to(self.state_root).as_posix(),
            "bytes": len(data),
            "sha256": sha256_bytes(data),
        }

    def submit_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        plan = _require_text(args.get("plan"), "plan")
        with self._connect() as connection:
            self._require_lease(connection, task_id, args.get("lease_token"))
        draft = self._write_draft(task_id, "PLAN.md", plan.encode("utf-8"))
        with self._connect() as connection:
            self._event(connection, "plan.submitted", draft, task_id)
        return {"task_id": task_id, "draft": draft, "project_write_performed": False}

    def submit_patch(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        summary = _require_text(args.get("change_summary"), "change_summary")
        patch = _require_text(args.get("patch"), "patch", limit=500_000)
        if PATCH_DESTRUCTIVE.search(patch):
            raise BridgeError("destructive or repository-control patch is not accepted")
        targets = [
            self._normalize_path(item)
            for item in _json_list(args.get("target_paths"), "target_paths")
        ]
        if not targets:
            raise BridgeError("target_paths must be a non-empty array")
        if any(self._is_protected(path) for path in targets):
            raise BridgeError("patch targets a protected or sensitive path")
        with self._connect() as connection:
            task = self._require_lease(connection, task_id, args.get("lease_token"))
            write_scopes = [
                row["path_prefix"]
                for row in connection.execute(
                    "SELECT path_prefix FROM task_paths WHERE scope=? AND task_id=? AND access='write'",
                    (self.scope, task_id),
                ).fetchall()
            ]
            for target in targets:
                if not any(self._path_within(target, prefix) for prefix in write_scopes):
                    raise BridgeError(f"patch target is outside the task write scope: {target}")
        draft = self._write_draft(task_id, "PATCH.diff", patch.encode("utf-8"))
        metadata = {
            "task_id": task_id,
            "target_paths": targets,
            "change_summary": summary,
            "patch": draft,
            "approval_mode": task["approval_mode"],
            "project_write_performed": False,
        }
        meta = self._write_draft(
            task_id,
            "PATCH_METADATA.json",
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )
        with self._connect() as connection:
            self._event(
                connection,
                "patch.submitted",
                {"patch": draft, "metadata": meta, "target_paths": targets},
                task_id,
            )
        return {"task_id": task_id, "patch": draft, "metadata": meta, "project_write_performed": False}

    def verify_audit_chain(self, _args: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE scope=? ORDER BY sequence ASC",
                (self.scope,),
            ).fetchall()
        previous = ZERO_SHA256
        errors = []
        for row in rows:
            payload_sha = sha256_bytes(row["payload_json"].encode("utf-8"))
            envelope = {
                "event_id": row["event_id"],
                "scope": row["scope"],
                "actor": row["actor"],
                "event_type": row["event_type"],
                "task_id": row["task_id"],
                "payload_sha256": payload_sha,
                "created_utc": row["created_utc"],
                "prev_chain_sha256": previous,
            }
            chain_sha = stable_sha256(envelope)
            if row["payload_sha256"] != payload_sha:
                errors.append({"sequence": row["sequence"], "error": "payload_sha256"})
            if row["prev_chain_sha256"] != previous:
                errors.append({"sequence": row["sequence"], "error": "prev_chain_sha256"})
            if row["chain_sha256"] != chain_sha:
                errors.append({"sequence": row["sequence"], "error": "chain_sha256"})
            previous = row["chain_sha256"]
        return {
            "valid": not errors,
            "event_count": len(rows),
            "head_chain_sha256": previous,
            "errors": errors,
            "writes_performed": 0,
        }
