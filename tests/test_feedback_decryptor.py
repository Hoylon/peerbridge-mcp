from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="The maintainer decryptor is a Windows DPAPI contract"
)


PWSh = shutil.which("pwsh")
CASE_ID = "0123456789abcdef0123456789abcdef"


def _run_decryptor(project_root: Path, bundle: Path, private_store: Path) -> subprocess.CompletedProcess[str]:
    if PWSh is None:
        pytest.skip("PowerShell 7 is unavailable")
    return subprocess.run(
        [
            PWSh,
            "-NoProfile",
            "-File",
            str(project_root / "scripts" / "decrypt_feedback_bundle.ps1"),
            "-Bundle",
            str(bundle),
            "-PrivateStore",
            str(private_store),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _report() -> str:
    return json.dumps(
        {"schema": "peerbridge.feedback-report.v1", "case_id": CASE_ID}
    )


def _envelope() -> str:
    return json.dumps(
        {
            "schema": "peerbridge.feedback-secret-envelope.v1",
            "algorithm": "RSA-OAEP-SHA256+A256GCM",
            "case_id": CASE_ID,
        }
    )


def test_feedback_decryptor_rejects_duplicate_members_before_key_access(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(bundle, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", _report())
        archive.writestr("report.json", _report())
        archive.writestr("encrypted-credential.json", _envelope())

    result = _run_decryptor(project_root, bundle, tmp_path)

    assert result.returncode != 0
    assert "invalid or duplicate member" in result.stderr
    assert "private key is unavailable" not in result.stderr


def test_feedback_decryptor_rejects_unsafe_compression_ratio(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "ratio.zip"
    with zipfile.ZipFile(bundle, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", _report())
        archive.writestr("encrypted-credential.json", _envelope())
        archive.writestr("attachments/01.txt", b"0" * (1024 * 1024))

    result = _run_decryptor(project_root, bundle, tmp_path)

    assert result.returncode != 0
    assert "compression ratio is unsafe" in result.stderr
    assert "private key is unavailable" not in result.stderr


def test_feedback_decryptor_validates_structure_before_requesting_private_key(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "valid-structure.zip"
    with zipfile.ZipFile(bundle, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", _report())
        archive.writestr("encrypted-credential.json", _envelope())

    result = _run_decryptor(project_root, bundle, tmp_path)

    assert result.returncode != 0
    assert "protected maintainer private key is unavailable" in result.stderr
    assert "invalid or duplicate member" not in result.stderr


def test_feedback_decryptor_rejects_directory_before_key_access(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "not-a-bundle"
    bundle.mkdir()

    result = _run_decryptor(project_root, bundle, tmp_path)

    assert result.returncode != 0
    assert "must be a regular ZIP file" in result.stderr
    assert "private key is unavailable" not in result.stderr
