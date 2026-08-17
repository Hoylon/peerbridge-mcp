from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.verify_continuity_manifest import (
    ContinuityManifestError,
    refresh_manifest,
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


def _test_evidence(
    payload_root: str,
    *,
    tests_passed: int = 1,
    tests_skipped: int = 1,
) -> dict[str, object]:
    return {
        "schema": "peerbridge.test-evidence.v1",
        "status": "PASS",
        "command": "python -m pytest -q",
        "created_at_utc": "2026-08-18T00:00:00Z",
        "tracked_payload_root_sha256": payload_root,
        "tests_collected": tests_passed + tests_skipped,
        "tests_failed": 0,
        "tests_passed": tests_passed,
        "tests_skipped": tests_skipped,
    }


def _manifest(root: Path, authority: Path) -> dict[str, object]:
    local = root / "source.txt"
    rows: list[dict[str, object]] = [
        {"path": "source.txt", "bytes": local.stat().st_size, "sha256": _sha(local)}
    ]
    payload_root = _payload_root(rows)
    return {
        "schema": "peerbridge-continuity-manifest/v1",
        "claims": {
            "automatic_provider_reply_ready": True,
            "local_alpha_acceptance_ready": True,
            "local_alpha_release_ready": True,
            "strict_package_gate_ready": True,
            "operator_physical_acceptance_ready": True,
            "remote_mobile_e2e_ready": False,
            "release_certified_payload_root_sha256": payload_root,
            "test_evidence": _test_evidence(payload_root),
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
            "tracked_payload_root_sha256": payload_root,
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


def test_refresh_manifest_rebuilds_git_inventory_and_test_counts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("new source\n", encoding="utf-8")
    readme = root / "README.md"
    readme.write_text("# Documentation\n", encoding="utf-8")
    untracked = root / "private.tmp"
    untracked.write_text("not part of the release\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "README.md", "manifest.json")
    stale = _manifest(root, authority)
    stale["peerbridge"]["manifest_excludes_self"] = "manifest.json"
    manifest_path.write_text(json.dumps(stale), encoding="utf-8")
    rows = [
        {"path": path.name, "bytes": path.stat().st_size, "sha256": _sha(path)}
        for path in (readme, source)
    ]
    evidence = _test_evidence(_payload_root(rows), tests_passed=12, tests_skipped=2)

    refreshed = refresh_manifest(
        manifest_path,
        root,
        test_evidence=evidence,
        certify_release_claims=True,
    )
    verified = verify_manifest(manifest_path, root, release_profile="local-alpha")

    assert refreshed["claims"]["tests_collected"] == 14
    assert refreshed["claims"]["tests_passed"] == 12
    assert refreshed["claims"]["tests_skipped"] == 2
    assert [row["path"] for row in refreshed["peerbridge"]["source_files"]] == [
        "source.txt"
    ]
    assert [row["path"] for row in refreshed["peerbridge"]["documentation"]] == [
        "README.md"
    ]
    assert refreshed["external_authorities"] == stale["external_authorities"]
    assert refreshed["claims"]["test_evidence"] == evidence
    assert refreshed["claims"]["release_certified_payload_root_sha256"] == (
        refreshed["peerbridge"]["tracked_payload_root_sha256"]
    )
    assert "private.tmp" not in manifest_path.read_text(encoding="utf-8")
    assert verified["status"] == "PASS"


@pytest.mark.parametrize(
    ("tests_passed", "tests_skipped"),
    [(0, 0), (1, -1)],
)
def test_refresh_manifest_rejects_invalid_test_counts(
    tmp_path: Path,
    tests_passed: int,
    tests_skipped: int,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("source\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "manifest.json")
    manifest_path.write_text(json.dumps(_manifest(root, authority)), encoding="utf-8")
    payload_root = _payload_root(
        [{"path": "source.txt", "bytes": source.stat().st_size, "sha256": _sha(source)}]
    )

    with pytest.raises(ContinuityManifestError, match="test evidence"):
        refresh_manifest(
            manifest_path,
            root,
            test_evidence=_test_evidence(
                payload_root,
                tests_passed=tests_passed,
                tests_skipped=tests_skipped,
            ),
        )


def test_refresh_manifest_invalidates_stale_release_claims_without_certification(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("source\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "manifest.json")
    manifest_path.write_text(json.dumps(_manifest(root, authority)), encoding="utf-8")
    payload_root = _payload_root(
        [{"path": "source.txt", "bytes": source.stat().st_size, "sha256": _sha(source)}]
    )

    refreshed = refresh_manifest(
        manifest_path,
        root,
        test_evidence=_test_evidence(payload_root),
    )

    assert refreshed["claims"]["local_alpha_release_ready"] is False
    assert refreshed["claims"]["release_certified_payload_root_sha256"] is None
    with pytest.raises(ContinuityManifestError, match="release claim"):
        verify_manifest(manifest_path, root, release_profile="local-alpha")


def test_refresh_manifest_rejects_test_evidence_for_an_old_payload(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("source\n", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.write_text("{}\n", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _init_repo(root, "source.txt", "manifest.json")
    manifest_path.write_text(json.dumps(_manifest(root, authority)), encoding="utf-8")

    with pytest.raises(ContinuityManifestError, match="payload root"):
        refresh_manifest(
            manifest_path,
            root,
            test_evidence=_test_evidence("0" * 64),
            certify_release_claims=True,
        )
