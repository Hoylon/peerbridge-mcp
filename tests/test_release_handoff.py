from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.capture_release_handoff import (
    HandoffError,
    build_checkpoint,
    verify_chain,
    write_checkpoint,
)


def _repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "PeerBridge Tests"], cwd=root, check=True)
    (root / ".gitignore").write_text(".peerbridge-artifacts/\n", encoding="utf-8")
    for relative in (
        "ROADMAP.md",
        "docs/ALPHA_5_2_REQUIREMENTS.md",
        "docs/DESKTOP_FEATURE_GAP_REGISTER_20260815.md",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)


def test_handoff_chain_binds_authority_backup_and_previous_checkpoint(tmp_path: Path) -> None:
    _repo(tmp_path)
    backup = tmp_path / ".peerbridge-artifacts" / "backup.bundle"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"verified-backup")
    first_path = tmp_path / ".peerbridge-artifacts" / "handoff" / "001.json"
    first = build_checkpoint(
        tmp_path,
        release="v0.1.0-alpha.5.2",
        package_version="0.1.0a5.post2",
        phase="requirements",
        next_phase="managed-runtime",
        backup=backup,
    )
    written = write_checkpoint(first_path, first)
    assert written["chain_index"] == 1
    assert first["git"]["clean"] is True
    assert len(first["authority_bindings"]) == 3
    assert first["backup_binding"]["path"].endswith("backup.bundle")

    (tmp_path / "ROADMAP.md").write_text("# changed\n", encoding="utf-8")
    second_path = tmp_path / ".peerbridge-artifacts" / "handoff" / "002.json"
    second = build_checkpoint(
        tmp_path,
        release="v0.1.0-alpha.5.2",
        package_version="0.1.0a5.post2",
        phase="managed-runtime",
        next_phase="cockpit-ui",
        previous=first_path,
        backup=backup,
    )
    write_checkpoint(second_path, second)
    assert second["chain_index"] == 2
    assert second["git"]["clean"] is False
    assert verify_chain(tmp_path, second_path)["checkpoint_count"] == 2


def test_handoff_output_is_create_only(tmp_path: Path) -> None:
    _repo(tmp_path)
    target = tmp_path / "checkpoint.json"
    payload = build_checkpoint(
        tmp_path,
        release="v0.1.0-alpha.5.2",
        package_version="0.1.0a5.post2",
        phase="requirements",
        next_phase="managed-runtime",
    )
    write_checkpoint(target, payload)
    with pytest.raises(HandoffError, match="create-only"):
        write_checkpoint(target, payload)


def test_handoff_verification_rejects_tampering(tmp_path: Path) -> None:
    _repo(tmp_path)
    target = tmp_path / "checkpoint.json"
    payload = build_checkpoint(
        tmp_path,
        release="v0.1.0-alpha.5.2",
        package_version="0.1.0a5.post2",
        phase="requirements",
        next_phase="managed-runtime",
    )
    write_checkpoint(target, payload)
    tampered = json.loads(target.read_text(encoding="utf-8"))
    tampered["phase"] = "published"
    target.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(HandoffError, match="SHA-256 mismatch"):
        verify_chain(tmp_path, target)
