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
    runtime = b"fixture runtime"
    support_root = PROJECT_ROOT / "src" / "peerbridge_mcp" / "release_support"
    support_config = (support_root / "support.json").read_bytes()
    support_public_key = (
        support_root / "peerbridge-support-public.pub"
    ).read_bytes()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("package/SBOM.spdx.json", sbom.read_bytes())
        package.writestr(
            "package/THIRD_PARTY_LICENSES/LICENSES_MANIFEST.json",
            license_manifest.read_bytes(),
        )
        package.writestr("package/PeerBridgeControlRoom.exe", runtime)
        package.writestr(
            "package/_internal/peerbridge_mcp/release_support/support.json",
            support_config,
        )
        package.writestr(
            "package/_internal/peerbridge_mcp/release_support/"
            "peerbridge-support-public.pub",
            support_public_key,
        )
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
        "runtime_name": "PeerBridgeControlRoom.exe",
        "runtime_bytes": len(runtime),
        "runtime_sha256": hashlib.sha256(runtime).hexdigest(),
        "support_config_sha256": hashlib.sha256(support_config).hexdigest(),
        "support_public_key_sha256": hashlib.sha256(support_public_key).hexdigest(),
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
    commit, archive = _fixture(tmp_path)

    result = verify(
        tmp_path,
        PROJECT_ROOT,
        commit,
        archive.name,
        hashlib.sha256(archive.read_bytes()).hexdigest(),
    )

    assert result["status"] == "PASS"
    assert result["source_commit"] == commit
    assert result["runtime_sha256"] == hashlib.sha256(b"fixture runtime").hexdigest()
    assert result["support_public_key_sha256"] == hashlib.sha256(
        (
            PROJECT_ROOT
            / "src"
            / "peerbridge_mcp"
            / "release_support"
            / "peerbridge-support-public.pub"
        ).read_bytes()
    ).hexdigest()


def test_portable_provenance_rejects_different_expected_release_asset(
    tmp_path: Path,
) -> None:
    commit, archive = _fixture(tmp_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    with pytest.raises(ProvenanceError, match="expected release asset"):
        verify(tmp_path, PROJECT_ROOT, commit, "other.zip", digest)

    with pytest.raises(ProvenanceError, match="expected release digest"):
        verify(tmp_path, PROJECT_ROOT, commit, archive.name, "0" * 64)


def test_portable_provenance_rejects_archive_tamper(tmp_path: Path) -> None:
    commit, archive = _fixture(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"tamper")

    with pytest.raises(ProvenanceError, match="byte count"):
        verify(tmp_path, PROJECT_ROOT, commit)


def test_portable_provenance_rejects_runtime_identity_tamper(tmp_path: Path) -> None:
    commit, _archive = _fixture(tmp_path)
    receipt_path = tmp_path / "portable.provenance.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["runtime_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ProvenanceError, match="runtime differs"):
        verify(tmp_path, PROJECT_ROOT, commit)


def test_portable_provenance_rejects_support_key_identity_tamper(
    tmp_path: Path,
) -> None:
    commit, _archive = _fixture(tmp_path)
    receipt_path = tmp_path / "portable.provenance.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["support_public_key_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ProvenanceError, match="support public key differs"):
        verify(tmp_path, PROJECT_ROOT, commit)


def test_portable_provenance_rejects_extreme_zip_compression_ratio(
    tmp_path: Path,
) -> None:
    commit, archive = _fixture(tmp_path)
    with zipfile.ZipFile(archive, "a", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("package/compression-bomb.txt", b"x" * (1024 * 1024))
    receipt_path = tmp_path / "portable.provenance.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["archive_bytes"] = archive.stat().st_size
    receipt["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ProvenanceError, match="compression-ratio"):
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
