from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.verify_continuity_manifest import (
    ContinuityManifestError,
    verify_manifest,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_root(rows: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: str(item["path"])):
        digest.update(
            f"{row['path']}\0{row['bytes']}\0{row['sha256']}\n".encode("utf-8")
        )
    return digest.hexdigest()


def _init_repo(root: Path, *paths: str) -> None:
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "--", *paths], check=True)


def _manifest(root: Path, authority: Path) -> dict[str, object]:
    local = root / "source.txt"
    rows: list[dict[str, object]] = [
        {"path": "source.txt", "bytes": local.stat().st_size, "sha256": _sha(local)}
    ]
    return {
        "schema": "peerbridge-continuity-manifest/v1",
        "claims": {
            "automatic_provider_reply_ready": True,
            "local_alpha_acceptance_ready": True,
            "local_alpha_release_ready": True,
            "strict_package_gate_ready": True,
            "operator_physical_acceptance_ready": True,
            "tests_collected": 2,
            "tests_failed": 0,
            "tests_passed": 1,
            "tests_skipped": 1,
        },
        "peerbridge": {
            "release_profile": "local-alpha",
            "manifest_excludes_self": "manifest.json",
            "payload_root_algorithm": "sha256(path\\0bytes\\0sha256\\n for sorted entries)",
            "tracked_payload_file_count": len(rows),
            "tracked_payload_root_sha256": _payload_root(rows),
            "source_files": rows,
            "documentation": [],
        },
        "external_authorities": [
            {
                "path": str(authority.resolve()),
                "bytes": authority.stat().st_size,
                "sha256": _sha(authority),
            }
        ],
    }


def test_continuity_manifest_passes_without_writes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    local = root / "source.txt"
    local.write_text("local\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "manifest.json")
    manifest_path.write_text(
        json.dumps(_manifest(root, authority), sort_keys=True), encoding="utf-8"
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (local, authority, manifest_path)
    }

    result = verify_manifest(manifest_path, root)

    assert result["status"] == "PASS"
    assert result["verified_file_count"] == 2
    assert before == {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (local, authority, manifest_path)
    }


def test_continuity_manifest_detects_drift(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    local = root / "source.txt"
    local.write_text("local\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "manifest.json")
    manifest_path.write_text(
        json.dumps(_manifest(root, authority), sort_keys=True), encoding="utf-8"
    )
    local.write_text("drift\n", encoding="utf-8")

    with pytest.raises(ContinuityManifestError, match="drift"):
        verify_manifest(manifest_path, root)


def test_continuity_manifest_rejects_relative_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    manifest = {
        "schema": "peerbridge-continuity-manifest/v1",
        "peerbridge": {
            "source_files": [
                {
                    "path": "../outside.txt",
                    "bytes": outside.stat().st_size,
                    "sha256": _sha(outside),
                }
            ],
            "documentation": [],
        },
        "external_authorities": [],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ContinuityManifestError, match="escapes project root"):
        verify_manifest(manifest_path, root)


def test_continuity_manifest_rejects_omitted_tracked_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.txt").write_text("local\n", encoding="utf-8")
    (root / "omitted.txt").write_text("must be bound\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "omitted.txt", "manifest.json")
    manifest_path.write_text(json.dumps(_manifest(root, authority)), encoding="utf-8")

    with pytest.raises(ContinuityManifestError, match="tracked inventory mismatch"):
        verify_manifest(manifest_path, root)


def test_continuity_manifest_rejects_duplicate_tracked_entry(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.txt").write_text("local\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "manifest.json")
    manifest = _manifest(root, authority)
    manifest["peerbridge"]["documentation"].append(
        dict(manifest["peerbridge"]["source_files"][0])
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ContinuityManifestError, match="duplicate tracked entry"):
        verify_manifest(manifest_path, root)


@pytest.mark.parametrize("field", ["tracked_payload_file_count", "tracked_payload_root_sha256"])
def test_continuity_manifest_rejects_declared_inventory_drift(
    tmp_path: Path, field: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.txt").write_text("local\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "manifest.json")
    manifest = _manifest(root, authority)
    manifest["peerbridge"][field] = 99 if field.endswith("count") else "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ContinuityManifestError, match="count mismatch|aggregate root drift"):
        verify_manifest(manifest_path, root)


def test_local_alpha_release_profile_rejects_false_release_claim(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.txt").write_text("local\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "manifest.json")
    manifest = _manifest(root, authority)
    manifest["claims"]["local_alpha_release_ready"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ContinuityManifestError, match="local_alpha_release_ready"):
        verify_manifest(manifest_path, root, release_profile="local-alpha")
