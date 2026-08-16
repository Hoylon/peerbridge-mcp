"""Strictly read-only diagnostics for an existing PeerBridge SQLite database."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .bridge import (
    DEFAULT_PRESENCE_TTL_SECONDS,
    SCHEMA_VERSION,
    ZERO_SHA256,
    stable_sha256,
)


_SAFE_SCOPE = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _command(action: str, root: Path, db_path: Path, scope: str) -> list[str]:
    command = ["peerbridge", action, "--project-root", str(root), "--scope", scope]
    if action in {"init", "migrate", "doctor"}:
        command.extend(("--db", str(db_path)))
    return command


def _audit_not_checked(reason: str) -> dict[str, Any]:
    return {
        "valid": False,
        "checked": False,
        "event_count": 0,
        "head_chain_sha256": ZERO_SHA256,
        "errors": [],
        "not_checked_reason": reason,
        "writes_performed": 0,
    }


def _diagnostic(
    root: Path,
    db_path: Path,
    scope: str,
    *,
    schema_status: str,
    observed_version: str | None,
    reason: str,
    exists: bool,
    opened: bool,
    query_only: bool,
    guidance: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": False,
        "writes_performed": 0,
        "project_root": str(root),
        "database": {
            "path": str(db_path),
            "exists": exists,
            "open_mode": "ro+immutable" if opened else None,
            "query_only": query_only,
        },
        "schema": {
            "status": schema_status,
            "expected_version": SCHEMA_VERSION,
            "observed_version": observed_version,
        },
        "status": {
            "scope": scope,
            "database": str(db_path),
            "schema_version": observed_version,
        },
        "integrity": {"valid": False, "checked": False, "errors": []},
        "audit": _audit_not_checked(reason),
        "guidance": guidance,
    }


@contextlib.contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=3.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            raise sqlite3.OperationalError("SQLite query_only mode could not be enabled")
        yield connection
    finally:
        connection.close()


def _schema_version(connection: sqlite3.Connection) -> str | None:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "metadata" not in tables:
        return None
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    return str(row["value"]) if row is not None else None


def _integrity(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()]
    valid = rows == ["ok"]
    return {
        "valid": valid,
        "checked": True,
        "errors": [] if valid else rows,
    }


def _status(
    connection: sqlite3.Connection, db_path: Path, scope: str
) -> dict[str, Any]:
    task_counts = {
        row["status"]: row["n"]
        for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE scope=? GROUP BY status",
            (scope,),
        ).fetchall()
    }
    dispatch_counts = {
        row["status"]: row["n"]
        for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM message_dispatches "
            "WHERE scope=? GROUP BY status",
            (scope,),
        ).fetchall()
    }
    message_count = connection.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE scope=?", (scope,)
    ).fetchone()["n"]
    event_count = connection.execute(
        "SELECT COUNT(*) AS n FROM events WHERE scope=?", (scope,)
    ).fetchone()["n"]
    cutoff = time.time() - DEFAULT_PRESENCE_TTL_SECONDS
    presence_rows = connection.execute(
        """SELECT agent_id, session_id, transport, client_name, provider_id,
                  model_id, reasoning_mode, route_class, last_seen_utc
           FROM agent_presence
           WHERE scope=? AND last_seen_epoch>=?
           ORDER BY agent_id, last_seen_epoch DESC""",
        (scope, cutoff),
    ).fetchall()
    sessions = [dict(row) for row in presence_rows]
    return {
        "scope": scope,
        "agent_id": "doctor",
        "session_id": None,
        "runtime_identity": {
            "client_name": None,
            "provider_id": None,
            "model_id": None,
            "reasoning_mode": None,
            "route_class": None,
        },
        "transport": "local-read-only",
        "network_listener": False,
        "database": str(db_path),
        "message_count": message_count,
        "message_dispatch_counts": dispatch_counts,
        "task_counts": task_counts,
        "audit_event_count": event_count,
        "schema_version": SCHEMA_VERSION,
        "presence": {
            "online_agents": sorted({row["agent_id"] for row in presence_rows}),
            "online_sessions": sessions,
            "presence_ttl_seconds": DEFAULT_PRESENCE_TTL_SECONDS,
            "observed_utc": _utc_now(),
        },
    }


def _verify_audit_chain(
    connection: sqlite3.Connection, scope: str
) -> dict[str, Any]:
    rows = connection.execute(
        """SELECT sequence, event_id, scope, actor, event_type, task_id,
                  payload_json, created_utc, payload_sha256,
                  prev_chain_sha256, chain_sha256
           FROM events WHERE scope=? ORDER BY sequence ASC""",
        (scope,),
    ).fetchall()
    previous = ZERO_SHA256
    errors: list[dict[str, Any]] = []
    for row in rows:
        payload_sha = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
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
            errors.append(
                {"sequence": row["sequence"], "error": "prev_chain_sha256"}
            )
        if row["chain_sha256"] != chain_sha:
            errors.append({"sequence": row["sequence"], "error": "chain_sha256"})
        previous = row["chain_sha256"]
    return {
        "valid": not errors,
        "checked": True,
        "event_count": len(rows),
        "head_chain_sha256": previous,
        "errors": errors,
        "writes_performed": 0,
    }


def inspect_database(project_root: Path, db_path: Path, scope: str) -> dict[str, Any]:
    """Inspect one database without creating paths, migrating, or opening it writable."""
    root = Path(project_root).resolve()
    database = Path(db_path).resolve()
    normalized_scope = str(scope or "").strip()
    if not _SAFE_SCOPE.fullmatch(normalized_scope):
        raise ValueError(
            "scope must contain only letters, digits, dot, underscore, colon or dash"
        )
    if not root.is_dir():
        return _diagnostic(
            root,
            database,
            normalized_scope,
            schema_status="missing_project_root",
            observed_version=None,
            reason="project root does not exist",
            exists=False,
            opened=False,
            query_only=False,
            guidance=[
                {
                    "action": "init",
                    "message": "Create the project root and initialize PeerBridge explicitly.",
                    "command": _command("init", root, database, normalized_scope),
                }
            ],
        )
    if not database.is_file():
        return _diagnostic(
            root,
            database,
            normalized_scope,
            schema_status="missing",
            observed_version=None,
            reason="database does not exist",
            exists=False,
            opened=False,
            query_only=False,
            guidance=[
                {
                    "action": "init",
                    "message": "Initialize PeerBridge explicitly; doctor will not create it.",
                    "command": _command("init", root, database, normalized_scope),
                }
            ],
        )

    wal_path = database.with_name(f"{database.name}-wal")
    try:
        pending_wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
    except OSError as exc:
        return _diagnostic(
            root,
            database,
            normalized_scope,
            schema_status="busy_wal",
            observed_version=None,
            reason="database WAL state could not be inspected without writing",
            exists=True,
            opened=False,
            query_only=False,
            guidance=[
                {
                    "action": "pause_writers_then_retry",
                    "message": str(exc),
                    "command": _command("doctor", root, database, normalized_scope),
                }
            ],
        )
    if pending_wal_bytes > 0:
        return _diagnostic(
            root,
            database,
            normalized_scope,
            schema_status="busy_wal",
            observed_version=None,
            reason="database has an uncheckpointed WAL; immutable inspection would be stale",
            exists=True,
            opened=False,
            query_only=False,
            guidance=[
                {
                    "action": "pause_writers_then_retry",
                    "message": (
                        "Pause PeerBridge writers so SQLite can checkpoint, then run "
                        "the same doctor command again."
                    ),
                    "command": _command("doctor", root, database, normalized_scope),
                }
            ],
        )

    try:
        with _readonly_connection(database) as connection:
            observed_version = _schema_version(connection)
            if observed_version != SCHEMA_VERSION:
                try:
                    old_schema = int(observed_version or "0") < int(SCHEMA_VERSION)
                except ValueError:
                    old_schema = False
                if old_schema:
                    action = "migrate"
                    message = (
                        "Upgrade the existing database with the explicit migrate command."
                    )
                    command = _command(
                        "migrate", root, database, normalized_scope
                    )
                    schema_status = "old"
                else:
                    action = "install_compatible_release"
                    message = (
                        "Install a PeerBridge release compatible with this database schema."
                    )
                    command = []
                    schema_status = "unsupported"
                return _diagnostic(
                    root,
                    database,
                    normalized_scope,
                    schema_status=schema_status,
                    observed_version=observed_version,
                    reason=f"database schema is {schema_status}",
                    exists=True,
                    opened=True,
                    query_only=True,
                    guidance=[
                        {"action": action, "message": message, "command": command}
                    ],
                )
            integrity = _integrity(connection)
            status = _status(connection, database, normalized_scope)
            audit = _verify_audit_chain(connection, normalized_scope)
    except (OSError, sqlite3.Error, TypeError, UnicodeError, ValueError) as exc:
        return _diagnostic(
            root,
            database,
            normalized_scope,
            schema_status="invalid",
            observed_version=None,
            reason="database could not be inspected read-only",
            exists=True,
            opened=False,
            query_only=False,
            guidance=[
                {
                    "action": "restore_or_install_compatible_release",
                    "message": str(exc),
                    "command": [],
                }
            ],
        )

    return {
        "ok": bool(integrity["valid"] and audit["valid"]),
        "writes_performed": 0,
        "project_root": str(root),
        "database": {
            "path": str(database),
            "exists": True,
            "open_mode": "ro+immutable",
            "query_only": True,
        },
        "schema": {
            "status": "current",
            "expected_version": SCHEMA_VERSION,
            "observed_version": SCHEMA_VERSION,
        },
        "status": status,
        "integrity": integrity,
        "audit": audit,
        "guidance": [],
    }
