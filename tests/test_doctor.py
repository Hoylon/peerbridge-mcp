from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from peerbridge_mcp.bridge import SCHEMA_VERSION
from peerbridge_mcp.cli import main


_FIXED_MTIME_NS = 1_700_000_000_000_000_000


def _run(capsys, *args: str) -> tuple[int, dict]:
    return_code = main(list(args))
    captured = capsys.readouterr()
    assert captured.err == ""
    return return_code, json.loads(captured.out)


def _snapshot(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_mtime_ns


def _tree(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _legacy_database(root: Path) -> Path:
    state = root / ".peerbridge"
    state.mkdir(parents=True)
    database = state / "peerbridge.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', '1')"
        )
    os.utime(database, ns=(_FIXED_MTIME_NS, _FIXED_MTIME_NS))
    return database


def test_doctor_preserves_current_database_bytes_mtime_and_tree(
    tmp_path: Path, capsys
) -> None:
    return_code, _receipt = _run(
        capsys,
        "init",
        "--project-root",
        str(tmp_path),
        "--scope",
        "doctor-test",
    )
    assert return_code == 0
    database = tmp_path / ".peerbridge" / "peerbridge.sqlite3"
    os.utime(database, ns=(_FIXED_MTIME_NS, _FIXED_MTIME_NS))
    before = _snapshot(database)
    before_tree = _tree(tmp_path)

    return_code, result = _run(
        capsys,
        "doctor",
        "--project-root",
        str(tmp_path),
        "--scope",
        "doctor-test",
    )

    assert return_code == 0
    assert result["ok"] is True
    assert result["writes_performed"] == 0
    assert result["database"]["open_mode"] == "ro+immutable"
    assert result["database"]["query_only"] is True
    assert result["schema"] == {
        "expected_version": SCHEMA_VERSION,
        "observed_version": SCHEMA_VERSION,
        "status": "current",
    }
    assert result["audit"]["valid"] is True
    assert _snapshot(database) == before
    assert _tree(tmp_path) == before_tree


def test_doctor_missing_database_only_reports_init_guidance(
    tmp_path: Path, capsys
) -> None:
    before_tree = _tree(tmp_path)

    return_code, result = _run(
        capsys,
        "doctor",
        "--project-root",
        str(tmp_path),
        "--scope",
        "missing-test",
    )

    assert return_code == 1
    assert result["ok"] is False
    assert result["schema"]["status"] == "missing"
    assert result["guidance"][0]["action"] == "init"
    assert result["guidance"][0]["command"][:2] == ["peerbridge", "init"]
    assert not (tmp_path / ".peerbridge").exists()
    assert _tree(tmp_path) == before_tree


def test_doctor_custom_missing_database_guidance_targets_explicit_init(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "custom-state" / "peerbridge.sqlite3"

    return_code, result = _run(
        capsys,
        "doctor",
        "--project-root",
        str(tmp_path),
        "--db",
        str(database),
        "--scope",
        "custom-test",
    )

    assert return_code == 1
    command = result["guidance"][0]["command"]
    assert command[:2] == ["peerbridge", "init"]
    assert command[command.index("--db") + 1] == str(database)
    assert not database.parent.exists()

    return_code, receipt = _run(capsys, *command[1:])
    assert return_code == 0
    assert Path(receipt["database"]) == database
    assert database.is_file()


def test_doctor_old_schema_only_reports_migrate_and_preserves_database(
    tmp_path: Path, capsys
) -> None:
    database = _legacy_database(tmp_path)
    before = _snapshot(database)
    before_tree = _tree(tmp_path)

    return_code, result = _run(
        capsys,
        "doctor",
        "--project-root",
        str(tmp_path),
        "--scope",
        "legacy-test",
    )

    assert return_code == 1
    assert result["ok"] is False
    assert result["database"]["open_mode"] == "ro+immutable"
    assert result["database"]["query_only"] is True
    assert result["schema"]["status"] == "old"
    assert result["schema"]["observed_version"] == "1"
    assert result["guidance"][0]["action"] == "migrate"
    assert result["guidance"][0]["command"][:2] == ["peerbridge", "migrate"]
    assert _snapshot(database) == before
    assert _tree(tmp_path) == before_tree


def test_explicit_migrate_upgrades_legacy_database(tmp_path: Path, capsys) -> None:
    database = _legacy_database(tmp_path)
    before = _snapshot(database)

    return_code, result = _run(
        capsys,
        "migrate",
        "--project-root",
        str(tmp_path),
        "--db",
        str(database),
        "--scope",
        "legacy-test",
    )

    assert return_code == 0
    assert result["writes_performed"] is True
    assert result["schema_version"] == SCHEMA_VERSION
    assert _snapshot(database) != before
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        observed = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
    assert observed == SCHEMA_VERSION


def test_doctor_fails_closed_on_pending_wal_without_writing(
    tmp_path: Path, capsys
) -> None:
    return_code, _receipt = _run(
        capsys,
        "init",
        "--project-root",
        str(tmp_path),
        "--scope",
        "doctor-wal-test",
    )
    assert return_code == 0
    database = tmp_path / ".peerbridge" / "peerbridge.sqlite3"

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE doctor_wal_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO doctor_wal_probe(value) VALUES ('pending')")
        connection.commit()
        wal_path = database.with_name(f"{database.name}-wal")
        assert wal_path.stat().st_size > 0
        tracked = {
            path: _snapshot(path)
            for path in database.parent.iterdir()
            if path.is_file()
        }
        before_tree = _tree(tmp_path)

        return_code, result = _run(
            capsys,
            "doctor",
            "--project-root",
            str(tmp_path),
            "--scope",
            "doctor-wal-test",
        )

        assert return_code == 1
        assert result["ok"] is False
        assert result["writes_performed"] == 0
        assert result["schema"]["status"] == "busy_wal"
        assert result["guidance"][0]["action"] == "pause_writers_then_retry"
        assert {path: _snapshot(path) for path in tracked} == tracked
        assert _tree(tmp_path) == before_tree
    finally:
        connection.close()
