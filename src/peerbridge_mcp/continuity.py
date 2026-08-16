"""Bounded, secret-free continuity snapshots for long-running PeerBridge work."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SNAPSHOT_SCHEMA = "peerbridge-continuity-snapshot/v1"
MAX_CHANGED_PATHS = 200
MAX_RECEIPTS = 32
MAX_ROWS = 100
MAX_SNAPSHOT_BYTES = 512 * 1024
SENSITIVE_VALUE = re.compile(
    r"(?i)(?:sk-|ghp_|github_pat_|Bearer\s+)[A-Za-z0-9_.-]{12,}|AKIA[0-9A-Z]{16}"
)


class ContinuitySnapshotError(ValueError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _safe_git(project_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _database_snapshot(db_path: Path, scope: str) -> dict[str, Any]:
    if not db_path.is_file():
        raise ContinuitySnapshotError(f"database not found: {db_path}")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with contextlib.closing(
        sqlite3.connect(uri, uri=True, timeout=2.0)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "metadata",
            "messages",
            "message_dispatches",
            "rooms",
            "tasks",
            "route_profiles",
            "events",
        }
        missing = required - tables
        if missing:
            raise ContinuitySnapshotError(
                "database is missing tables: " + ", ".join(sorted(missing))
            )

        schema_row = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        counts = {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE scope=?', (scope,)
                ).fetchone()[0]
            )
            for table in (
                "messages",
                "message_dispatches",
                "rooms",
                "tasks",
                "route_profiles",
                "events",
            )
        }
        if "memories" in tables:
            counts["memories"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM memories WHERE scope=?", (scope,)
                ).fetchone()[0]
            )

        dispatches = {
            str(row["status"]): int(row["row_count"])
            for row in connection.execute(
                """SELECT status, COUNT(*) AS row_count
                     FROM message_dispatches WHERE scope=? GROUP BY status""",
                (scope,),
            )
        }
        tasks = [
            dict(row)
            for row in connection.execute(
                """SELECT task_id, status, owner, claimed_by, updated_utc,
                          approval_mode, task_sha256
                     FROM tasks WHERE scope=?
                     ORDER BY updated_utc DESC, task_id LIMIT ?""",
                (scope, MAX_ROWS),
            )
        ]
        rooms = [
            dict(row)
            for row in connection.execute(
                """SELECT room_id, name, archived, updated_utc, room_sha256
                     FROM rooms WHERE scope=?
                     ORDER BY updated_utc DESC, room_id LIMIT ?""",
                (scope, MAX_ROWS),
            )
        ]
        routes = [
            dict(row)
            for row in connection.execute(
                """SELECT route_id, agent_id, provider_id, model_id,
                          response_model_id, reasoning_mode, route_class,
                          enabled, profile_sha256
                     FROM route_profiles WHERE scope=?
                     ORDER BY route_id LIMIT ?""",
                (scope, MAX_ROWS),
            )
        ]
        message_highwater = connection.execute(
            """SELECT COALESCE(MAX(sequence), 0), MAX(created_utc)
                 FROM messages WHERE scope=?""",
            (scope,),
        ).fetchone()
        event_highwater = connection.execute(
            """SELECT COALESCE(MAX(sequence), 0), MAX(created_utc),
                      COALESCE(MAX(chain_sha256), '')
                 FROM events WHERE scope=?""",
            (scope,),
        ).fetchone()

    stat = db_path.stat()
    return {
        "path": str(db_path.resolve()),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "schema_version": str(schema_row[0]) if schema_row else None,
        "table_counts": counts,
        "dispatch_status_counts": dispatches,
        "message_highwater": {
            "sequence": int(message_highwater[0]),
            "created_utc": message_highwater[1],
        },
        "event_highwater": {
            "sequence": int(event_highwater[0]),
            "created_utc": event_highwater[1],
            "terminal_chain_sha256": event_highwater[2],
        },
        "tasks": tasks,
        "rooms": rooms,
        "routes": routes,
    }


def _receipt_bindings(project_root: Path) -> list[dict[str, Any]]:
    root = project_root / ".peerbridge" / "receipts"
    if not root.is_dir():
        return []
    files = sorted(
        (path for path in root.rglob("*.json") if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.as_posix()),
        reverse=True,
    )[:MAX_RECEIPTS]
    return [
        {
            "path": path.relative_to(project_root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in files
    ]


def build_snapshot(project_root: Path, db_path: Path, scope: str) -> dict[str, Any]:
    project_root = project_root.resolve()
    db_path = db_path.resolve()
    if not project_root.is_dir():
        raise ContinuitySnapshotError(f"project root not found: {project_root}")
    if not scope or len(scope) > 200:
        raise ContinuitySnapshotError("scope is invalid")

    status = _safe_git(project_root, "status", "--short")
    changed = [] if not status else [line[3:] for line in status.splitlines()]
    body: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "scope": scope,
        "project": {
            "root": str(project_root),
            "git_head": _safe_git(project_root, "rev-parse", "HEAD"),
            "changed_path_count": len(changed),
            "changed_paths": changed[:MAX_CHANGED_PATHS],
            "changed_paths_truncated": len(changed) > MAX_CHANGED_PATHS,
        },
        "database": _database_snapshot(db_path, scope),
        "receipt_bindings": _receipt_bindings(project_root),
        "continuity_contract": {
            "raw_chat_history_embedded": False,
            "message_bodies_embedded": False,
            "credentials_embedded": False,
            "live_state_must_be_recomputed_before_write": True,
        },
    }
    body["snapshot_sha256"] = _sha256_bytes(_canonical_bytes(body))
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if SENSITIVE_VALUE.search(encoded):
        raise ContinuitySnapshotError("snapshot contains a credential-like value")
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ContinuitySnapshotError("snapshot exceeds the bounded size limit")
    return body


def verify_snapshot(snapshot_path: Path) -> dict[str, Any]:
    snapshot_path = snapshot_path.resolve()
    try:
        raw = snapshot_path.read_bytes()
        snapshot = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContinuitySnapshotError(f"snapshot is unreadable: {exc}") from exc
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ContinuitySnapshotError("snapshot exceeds the bounded size limit")
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise ContinuitySnapshotError("unsupported continuity snapshot schema")
    expected = snapshot.get("snapshot_sha256")
    unsigned = dict(snapshot)
    unsigned.pop("snapshot_sha256", None)
    actual = _sha256_bytes(_canonical_bytes(unsigned))
    if expected != actual:
        raise ContinuitySnapshotError("snapshot SHA-256 mismatch")
    if SENSITIVE_VALUE.search(raw.decode("utf-8")):
        raise ContinuitySnapshotError("snapshot contains a credential-like value")
    contract = snapshot.get("continuity_contract")
    if not isinstance(contract, dict) or any(
        contract.get(key) is not False
        for key in (
            "raw_chat_history_embedded",
            "message_bodies_embedded",
            "credentials_embedded",
        )
    ):
        raise ContinuitySnapshotError("snapshot violates the bounded continuity contract")
    return {
        "status": "PASS",
        "path": str(snapshot_path),
        "bytes": len(raw),
        "snapshot_sha256": actual,
    }


def write_snapshot(output: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(output)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return verify_snapshot(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture or verify a bounded PeerBridge continuity snapshot."
    )
    parser.add_argument("command", choices=("capture", "verify"))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path)
    parser.add_argument("--scope", default="default")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.project_root.resolve()
    output = (
        args.output.resolve()
        if args.output
        else root / ".peerbridge" / "continuity" / "current.json"
    )
    try:
        if args.command == "capture":
            db = args.db.resolve() if args.db else root / ".peerbridge" / "peerbridge.sqlite3"
            result = write_snapshot(output, build_snapshot(root, db, args.scope))
        else:
            result = verify_snapshot(output)
    except ContinuitySnapshotError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
