from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.verify_portable_provenance import ProvenanceError, verify


PROJECT_ROOT = Path(__file__).parents[1]


def _git(revision: str) -> str:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", revision],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _fixture(root: Path) -> tuple[str, Path]:
    commit = _git("HEAD")
    sbom = root / "package" / "SBOM.spdx.json"
    sbom.parent.mkdir(parents=True)
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}', encoding="utf-8")
    license_manifest = root / "THIRD_PARTY_LICENSES_MANIFEST.json"
    license_manifest.write_text(
        '{"schema":"peerbridge.windows-runtime-licenses.v1"}', encoding="utf-8"
    )
    archive = root / "portable.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("package/SBOM.spdx.json", sbom.read_bytes())
        package.writestr(
            "package/THIRD_PARTY_LICENSES/LICENSES_MANIFEST.json",
            license_manifest.read_bytes(),
        )
        package.writestr("package/program.exe", b"fixture")
    receipt = {
        "schema": "peerbridge.windows-portable-provenance.v1",
        "version": "0.1.0a1",
        "source_commit": commit,
        "source_tree": _git("HEAD^{tree}"),
        "source_dirty": False,
        "archive_name": archive.name,
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "sbom_name": sbom.name,
        "sbom_sha256": hashlib.sha256(sbom.read_bytes()).hexdigest(),
        "runtime_license_manifest_name": license_manifest.name,
        "runtime_license_manifest_sha256": hashlib.sha256(
            license_manifest.read_bytes()
        ).hexdigest(),
        "packager_sha256": hashlib.sha256(
            (PROJECT_ROOT / "scripts" / "package_windows_portable.ps1").read_bytes()
        ).hexdigest(),
        "created_utc": "2026-08-16T00:00:00Z",
    }
    (root / "portable.provenance.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    return commit, archive


def test_portable_provenance_binds_retained_archive_and_source(tmp_path: Path) -> None:
    commit, _archive = _fixture(tmp_path)

    result = verify(tmp_path, PROJECT_ROOT, commit)

    assert result["status"] == "PASS"
    assert result["source_commit"] == commit


def test_portable_provenance_rejects_archive_tamper(tmp_path: Path) -> None:
    commit, archive = _fixture(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"tamper")

    with pytest.raises(ProvenanceError, match="byte count"):
        verify(tmp_path, PROJECT_ROOT, commit)


def test_packager_git_inventory_detects_untracked_pyinstaller_input(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "PeerBridge Test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    tracked = tmp_path / "README.md"
    tracked.write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    hook = tmp_path / "scripts" / "pyinstaller-hooks" / "hook-peerbridge.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("hiddenimports = []\n", encoding="utf-8")

    result = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert "?? scripts/pyinstaller-hooks/hook-peerbridge.py" in result.stdout
    packager = (PROJECT_ROOT / "scripts" / "package_windows_portable.ps1").read_text(
        encoding="utf-8"
    )
    assert "status --porcelain=v1 --untracked-files=all" in packager
