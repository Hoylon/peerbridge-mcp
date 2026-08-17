"""Verify that a retained Windows portable artifact matches its source receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any


SCHEMA = "peerbridge.windows-portable-provenance.v1"
SHA256 = frozenset("0123456789abcdef")


class ProvenanceError(ValueError):
    """A retained portable artifact is not bound to the expected source."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _one(paths: list[Path], label: str) -> Path:
    if len(paths) != 1:
        raise ProvenanceError(f"expected exactly one {label}, found {len(paths)}")
    return paths[0]


def _git(project_root: Path, revision: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", revision],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ProvenanceError(f"cannot resolve Git revision {revision}") from exc
    return result.stdout.strip().lower()


def verify(
    artifact_root: Path,
    project_root: Path,
    expected_commit: str,
    expected_archive_name: str | None = None,
    expected_archive_sha256: str | None = None,
) -> dict[str, Any]:
    root = artifact_root.resolve()
    source = project_root.resolve()
    commit = expected_commit.strip().lower()
    if len(commit) != 40 or any(character not in SHA256 for character in commit):
        raise ProvenanceError("expected commit must be a full 40-character Git SHA")

    receipt_path = _one(list(root.rglob("*.provenance.json")), "provenance receipt")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError("portable provenance receipt is not valid UTF-8 JSON") from exc
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ProvenanceError("portable provenance receipt schema is invalid")
    if receipt.get("source_commit") != commit:
        raise ProvenanceError("portable source commit differs from the release commit")
    if receipt.get("source_tree") != _git(source, "HEAD^{tree}"):
        raise ProvenanceError("portable source tree differs from the checked-out release tree")
    if _git(source, "HEAD") != commit:
        raise ProvenanceError("checked-out release commit differs from the expected commit")
    if receipt.get("source_dirty") is not False:
        raise ProvenanceError("portable artifact was built from a dirty source tree")
    if receipt.get("packager_sha256") != _sha256(
        source / "scripts" / "package_windows_portable.ps1"
    ):
        raise ProvenanceError("portable packager differs from the release source")

    archive_name = str(receipt.get("archive_name") or "")
    sbom_name = str(receipt.get("sbom_name") or "")
    license_manifest_name = str(receipt.get("runtime_license_manifest_name") or "")
    if Path(archive_name).name != archive_name or not archive_name.endswith(".zip"):
        raise ProvenanceError("portable archive name is invalid")
    if expected_archive_name is not None and archive_name != expected_archive_name:
        raise ProvenanceError("portable archive name differs from the expected release asset")
    if expected_archive_sha256 is not None:
        normalized_expected_sha = expected_archive_sha256.strip().lower()
        if len(normalized_expected_sha) != 64 or any(
            character not in SHA256 for character in normalized_expected_sha
        ):
            raise ProvenanceError("expected portable archive SHA-256 is invalid")
        if receipt.get("archive_sha256") != normalized_expected_sha:
            raise ProvenanceError(
                "portable archive SHA-256 differs from the expected release digest"
            )
    if Path(sbom_name).name != sbom_name or sbom_name != "SBOM.spdx.json":
        raise ProvenanceError("portable SBOM name is invalid")
    if (
        Path(license_manifest_name).name != license_manifest_name
        or license_manifest_name != "THIRD_PARTY_LICENSES_MANIFEST.json"
    ):
        raise ProvenanceError("portable runtime-license manifest name is invalid")
    archive = _one(list(root.rglob(archive_name)), "portable archive")
    sbom = _one(list(root.rglob(sbom_name)), "portable SBOM")
    if archive.stat().st_size != int(receipt.get("archive_bytes") or -1):
        raise ProvenanceError("portable archive byte count differs from its receipt")
    if _sha256(archive) != receipt.get("archive_sha256"):
        raise ProvenanceError("portable archive SHA-256 differs from its receipt")
    sbom_sha256 = _sha256(sbom)
    if sbom_sha256 != receipt.get("sbom_sha256"):
        raise ProvenanceError("portable SBOM SHA-256 differs from its receipt")
    try:
        with zipfile.ZipFile(archive) as package:
            candidates = [
                name
                for name in package.namelist()
                if name == sbom_name or name.endswith(f"/{sbom_name}")
            ]
            member = _one([Path(name) for name in candidates], "packaged SBOM member")
            packaged_sbom = package.read(member.as_posix())
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ProvenanceError("portable archive cannot provide its bound SBOM") from exc
    if hashlib.sha256(packaged_sbom).hexdigest() != sbom_sha256:
        raise ProvenanceError("packaged SBOM differs from the retained SBOM")

    license_manifest = _one(
        list(root.rglob(license_manifest_name)), "runtime-license manifest"
    )
    license_manifest_sha256 = _sha256(license_manifest)
    if license_manifest_sha256 != receipt.get("runtime_license_manifest_sha256"):
        raise ProvenanceError("runtime-license manifest differs from its receipt")
    try:
        with zipfile.ZipFile(archive) as package:
            license_candidates = [
                name
                for name in package.namelist()
                if name.endswith("/THIRD_PARTY_LICENSES/LICENSES_MANIFEST.json")
            ]
            license_member = _one(
                [Path(name) for name in license_candidates],
                "packaged runtime-license manifest member",
            )
            packaged_license_manifest = package.read(license_member.as_posix())
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ProvenanceError(
            "portable archive cannot provide its runtime-license manifest"
        ) from exc
    if hashlib.sha256(packaged_license_manifest).hexdigest() != license_manifest_sha256:
        raise ProvenanceError(
            "packaged runtime-license manifest differs from the retained manifest"
        )

    return {
        "status": "PASS",
        "schema": SCHEMA,
        "source_commit": commit,
        "source_tree": receipt["source_tree"],
        "archive": archive.name,
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": receipt["archive_sha256"],
        "sbom_sha256": sbom_sha256,
        "runtime_license_manifest_sha256": license_manifest_sha256,
        "receipt": receipt_path.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-archive-name")
    parser.add_argument("--expected-archive-sha256")
    args = parser.parse_args(argv)
    try:
        if bool(args.expected_archive_name) != bool(args.expected_archive_sha256):
            raise ProvenanceError(
                "expected archive name and SHA-256 must be supplied together"
            )
        result = verify(
            args.artifact_root,
            args.project_root,
            args.expected_commit,
            args.expected_archive_name,
            args.expected_archive_sha256,
        )
    except ProvenanceError as exc:
        print(f"PORTABLE_PROVENANCE_FAIL {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
