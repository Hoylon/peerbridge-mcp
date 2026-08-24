"""Verify that a retained Windows portable artifact matches its source receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


SCHEMA = "peerbridge.windows-portable-provenance.v1"
SHA256 = frozenset("0123456789abcdef")
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 250
MAX_METADATA_MEMBER_BYTES = 16 * 1024 * 1024
SUPPORT_CONFIG_FIELDS = frozenset(
    {
        "endpoint",
        "endpoint_transport",
        "privacy_url",
        "public_key_path",
        "public_key_sha256",
        "recipient_label",
        "schema",
        "support_email",
    }
)


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


def _inspect_archive(package: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = package.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ProvenanceError("portable archive contains too many members")
    expanded = 0
    by_name: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        name = info.filename
        relative = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or name in by_name
        ):
            raise ProvenanceError("portable archive contains an unsafe or duplicate path")
        by_name[name] = info
        if info.is_dir():
            continue
        if info.file_size < 0 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ProvenanceError(f"portable archive member exceeds the size limit: {name}")
        expanded += info.file_size
        if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ProvenanceError("portable archive exceeds the expanded byte limit")
        if info.file_size and (
            info.compress_size <= 0
            or info.file_size > info.compress_size * MAX_ARCHIVE_COMPRESSION_RATIO
        ):
            raise ProvenanceError(
                f"portable archive member exceeds the compression-ratio limit: {name}"
            )
    return by_name


def _hash_zip_member(
    package: zipfile.ZipFile, info: zipfile.ZipInfo, byte_limit: int
) -> tuple[int, str]:
    if info.file_size > byte_limit:
        raise ProvenanceError(
            f"portable archive member exceeds its content limit: {info.filename}"
        )
    digest = hashlib.sha256()
    observed = 0
    with package.open(info, "r") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            observed += len(block)
            if observed > byte_limit or observed > info.file_size:
                raise ProvenanceError(
                    f"portable archive member changed while reading: {info.filename}"
                )
            digest.update(block)
    if observed != info.file_size:
        raise ProvenanceError(
            f"portable archive member byte count is invalid: {info.filename}"
        )
    return observed, digest.hexdigest()


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
    runtime_name = str(receipt.get("runtime_name") or "")
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
    if runtime_name != "PeerBridgeControlRoom.exe":
        raise ProvenanceError("portable runtime name is invalid")
    runtime_sha256 = str(receipt.get("runtime_sha256") or "")
    if len(runtime_sha256) != 64 or any(
        character not in SHA256 for character in runtime_sha256
    ):
        raise ProvenanceError("portable runtime SHA-256 is invalid")
    runtime_bytes = int(receipt.get("runtime_bytes") or -1)
    if runtime_bytes < 1:
        raise ProvenanceError("portable runtime byte count is invalid")
    support_config_sha256 = str(receipt.get("support_config_sha256") or "")
    support_public_key_sha256 = str(
        receipt.get("support_public_key_sha256") or ""
    )
    for value, label in (
        (support_config_sha256, "portable support configuration SHA-256"),
        (support_public_key_sha256, "portable support public-key SHA-256"),
    ):
        if len(value) != 64 or any(character not in SHA256 for character in value):
            raise ProvenanceError(f"{label} is invalid")
    if (
        Path(license_manifest_name).name != license_manifest_name
        or license_manifest_name != "THIRD_PARTY_LICENSES_MANIFEST.json"
    ):
        raise ProvenanceError("portable runtime-license manifest name is invalid")
    archive = _one(list(root.rglob(archive_name)), "portable archive")
    sbom = _one(list(root.rglob(sbom_name)), "portable SBOM")
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ProvenanceError("portable archive exceeds the compressed byte limit")
    if archive.stat().st_size != int(receipt.get("archive_bytes") or -1):
        raise ProvenanceError("portable archive byte count differs from its receipt")
    if _sha256(archive) != receipt.get("archive_sha256"):
        raise ProvenanceError("portable archive SHA-256 differs from its receipt")
    sbom_sha256 = _sha256(sbom)
    if sbom_sha256 != receipt.get("sbom_sha256"):
        raise ProvenanceError("portable SBOM SHA-256 differs from its receipt")
    try:
        with zipfile.ZipFile(archive) as package:
            members = _inspect_archive(package)
            candidates = [
                name
                for name in members
                if name == sbom_name or name.endswith(f"/{sbom_name}")
            ]
            member = _one([Path(name) for name in candidates], "packaged SBOM member")
            runtime_candidates = [
                name
                for name in members
                if name == runtime_name or name.endswith(f"/{runtime_name}")
            ]
            runtime_member = _one(
                [Path(name) for name in runtime_candidates],
                "packaged runtime member",
            )
            support_config_member = _one(
                [
                    Path(name)
                    for name in members
                    if name.endswith(
                        "/_internal/peerbridge_mcp/release_support/support.json"
                    )
                ],
                "packaged support configuration member",
            )
            support_public_key_member = _one(
                [
                    Path(name)
                    for name in members
                    if name.endswith(
                        "/_internal/peerbridge_mcp/release_support/"
                        "peerbridge-support-public.pub"
                    )
                ],
                "packaged support public-key member",
            )
            packaged_sbom_bytes, packaged_sbom_sha256 = _hash_zip_member(
                package, members[member.as_posix()], MAX_METADATA_MEMBER_BYTES
            )
            packaged_runtime_bytes, packaged_runtime_sha256 = _hash_zip_member(
                package, members[runtime_member.as_posix()], MAX_ARCHIVE_MEMBER_BYTES
            )
            _, packaged_support_config_sha256 = _hash_zip_member(
                package,
                members[support_config_member.as_posix()],
                MAX_METADATA_MEMBER_BYTES,
            )
            _, packaged_support_public_key_sha256 = _hash_zip_member(
                package,
                members[support_public_key_member.as_posix()],
                MAX_METADATA_MEMBER_BYTES,
            )
            support_config_bytes = package.read(
                members[support_config_member.as_posix()]
            )
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ProvenanceError("portable archive cannot provide its bound SBOM") from exc
    if packaged_sbom_bytes != sbom.stat().st_size or packaged_sbom_sha256 != sbom_sha256:
        raise ProvenanceError("packaged SBOM differs from the retained SBOM")
    if packaged_runtime_bytes != runtime_bytes:
        raise ProvenanceError("packaged runtime byte count differs from its receipt")
    if packaged_runtime_sha256 != runtime_sha256:
        raise ProvenanceError("packaged runtime differs from its receipt")
    if packaged_support_config_sha256 != support_config_sha256:
        raise ProvenanceError("packaged support configuration differs from its receipt")
    if packaged_support_public_key_sha256 != support_public_key_sha256:
        raise ProvenanceError("packaged support public key differs from its receipt")
    try:
        support_config = json.loads(support_config_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(
            "packaged support configuration is not valid UTF-8 JSON"
        ) from exc
    if (
        not isinstance(support_config, dict)
        or set(support_config) != SUPPORT_CONFIG_FIELDS
        or support_config.get("schema") != "peerbridge.feedback-config.v1"
        or support_config.get("endpoint_transport") != "json-base64-v1"
        or support_config.get("public_key_path")
        != "peerbridge-support-public.pub"
        or support_config.get("public_key_sha256") != support_public_key_sha256
        or support_config.get("support_email") is not None
    ):
        raise ProvenanceError(
            "packaged support configuration does not bind its public key"
        )
    try:
        support_endpoint = urlsplit(str(support_config.get("endpoint") or ""))
        endpoint_port = support_endpoint.port
    except ValueError as exc:
        raise ProvenanceError("packaged support endpoint is invalid") from exc
    if (
        support_endpoint.scheme != "https"
        or not support_endpoint.hostname
        or endpoint_port is not None
        or support_endpoint.username
        or support_endpoint.password
        or support_endpoint.query
        or support_endpoint.fragment
        or support_endpoint.path != "/v1/feedback"
    ):
        raise ProvenanceError("packaged support endpoint is invalid")

    license_manifest = _one(
        list(root.rglob(license_manifest_name)), "runtime-license manifest"
    )
    license_manifest_sha256 = _sha256(license_manifest)
    if license_manifest_sha256 != receipt.get("runtime_license_manifest_sha256"):
        raise ProvenanceError("runtime-license manifest differs from its receipt")
    try:
        with zipfile.ZipFile(archive) as package:
            members = _inspect_archive(package)
            license_candidates = [
                name
                for name in members
                if name.endswith("/THIRD_PARTY_LICENSES/LICENSES_MANIFEST.json")
            ]
            license_member = _one(
                [Path(name) for name in license_candidates],
                "packaged runtime-license manifest member",
            )
            _, packaged_license_manifest_sha256 = _hash_zip_member(
                package,
                members[license_member.as_posix()],
                MAX_METADATA_MEMBER_BYTES,
            )
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ProvenanceError(
            "portable archive cannot provide its runtime-license manifest"
        ) from exc
    if packaged_license_manifest_sha256 != license_manifest_sha256:
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
        "runtime_sha256": runtime_sha256,
        "support_config_sha256": support_config_sha256,
        "support_public_key_sha256": support_public_key_sha256,
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
