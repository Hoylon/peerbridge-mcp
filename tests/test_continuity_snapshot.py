from __future__ import annotations

import json
from pathlib import Path

import pytest

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.continuity import (
    ContinuitySnapshotError,
    build_snapshot,
    verify_snapshot,
    write_snapshot,
)


def test_snapshot_is_bounded_and_omits_message_content(tmp_path: Path) -> None:
    bridge = Bridge(
        tmp_path,
        tmp_path / ".peerbridge" / "peerbridge.sqlite3",
        "sender",
        "test-scope",
    )
    secret_body = "private-message-body-that-must-never-enter-continuity"
    bridge.send_message(
        {
            "recipient": "peer",
            "task_id": "continuity",
            "subject": "private subject",
            "body": secret_body,
        }
    )

    snapshot = build_snapshot(tmp_path, bridge.db_path, "test-scope")
    encoded = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["database"]["table_counts"]["messages"] == 1
    assert snapshot["database"]["message_highwater"]["sequence"] == 1
    assert secret_body not in encoded
    assert "private subject" not in encoded
    assert snapshot["continuity_contract"]["raw_chat_history_embedded"] is False
    assert len(encoded.encode("utf-8")) < 512 * 1024


def test_snapshot_write_and_verify_are_stable_and_verify_only(tmp_path: Path) -> None:
    bridge = Bridge(
        tmp_path,
        tmp_path / ".peerbridge" / "peerbridge.sqlite3",
        "writer",
        "test-scope",
    )
    output = tmp_path / ".peerbridge" / "continuity" / "current.json"
    result = write_snapshot(
        output, build_snapshot(tmp_path, bridge.db_path, "test-scope")
    )
    before = (output.read_bytes(), output.stat().st_mtime_ns)

    verified = verify_snapshot(output)

    assert result["status"] == "PASS"
    assert verified["snapshot_sha256"] == result["snapshot_sha256"]
    assert before == (output.read_bytes(), output.stat().st_mtime_ns)


def test_snapshot_verifier_rejects_tampering(tmp_path: Path) -> None:
    bridge = Bridge(
        tmp_path,
        tmp_path / ".peerbridge" / "peerbridge.sqlite3",
        "writer",
        "test-scope",
    )
    output = tmp_path / "snapshot.json"
    write_snapshot(output, build_snapshot(tmp_path, bridge.db_path, "test-scope"))
    data = json.loads(output.read_text(encoding="utf-8"))
    data["scope"] = "tampered"
    output.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ContinuitySnapshotError, match="SHA-256 mismatch"):
        verify_snapshot(output)
