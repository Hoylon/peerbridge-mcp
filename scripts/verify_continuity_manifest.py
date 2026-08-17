from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


TEST_EVIDENCE_SCHEMA = "peerbridge.test-evidence.v1"
RELEASE_READY_CLAIMS = (
    "automatic_provider_reply_ready",
    "local_alpha_acceptance_ready",
    "local_alpha_release_ready",
    "strict_package_gate_ready",
    "operator_physical_acceptance_ready",
)


class ContinuityManifestError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _payload_root(rows: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item["path"]):
        digest.update(
            f"{row['path']}\0{row['bytes']}\0{row['sha256']}\n".encode("utf-8")
        )
    return digest.hexdigest()


def _git_tracked_paths(project_root: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "-z", "--cached"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContinuityManifestError(
            "project root must be a readable Git worktree"
        ) from exc
    paths = {
        raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for raw in result.stdout.split(b"\0")
        if raw
    }
    if not paths:
        raise ContinuityManifestError("Git tracked file set is empty")
    return paths


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContinuityManifestError(f"{label} must be an object")
    return value


def _tracked_row(project_root: Path, relative_path: str) -> dict[str, Any]:
    path = (project_root / relative_path).resolve()
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise ContinuityManifestError(
            f"tracked path escapes project root: {relative_path}"
        ) from exc
    if not path.is_file():
        raise ContinuityManifestError(f"tracked file is missing: {relative_path}")
    return {
        "bytes": path.stat().st_size,
        "path": relative_path,
        "sha256": _sha256(path),
    }


def _normalize_test_evidence(
    value: Mapping[str, Any], expected_payload_root: str
) -> dict[str, Any]:
    evidence = dict(value)
    expected_fields = {
        "schema",
        "status",
        "command",
        "created_at_utc",
        "tracked_payload_root_sha256",
        "tests_collected",
        "tests_failed",
        "tests_passed",
        "tests_skipped",
    }
    if set(evidence) != expected_fields:
        raise ContinuityManifestError("test evidence fields are incomplete or unsupported")
    if evidence.get("schema") != TEST_EVIDENCE_SCHEMA or evidence.get("status") != "PASS":
        raise ContinuityManifestError("test evidence must be a PASS v1 receipt")
    command = evidence.get("command")
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > 4096
        or "\x00" in command
    ):
        raise ContinuityManifestError("test evidence command is invalid")
    created_at = evidence.get("created_at_utc")
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise ContinuityManifestError("test evidence timestamp is invalid")
    try:
        datetime.fromisoformat(created_at[:-1] + "+00:00")
    except ValueError as exc:
        raise ContinuityManifestError("test evidence timestamp is invalid") from exc
    if evidence.get("tracked_payload_root_sha256") != expected_payload_root:
        raise ContinuityManifestError("test evidence payload root does not match live source")
    for field in (
        "tests_collected",
        "tests_failed",
        "tests_passed",
        "tests_skipped",
    ):
        count = evidence.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ContinuityManifestError(f"test evidence count is invalid: {field}")
    if evidence["tests_failed"] != 0 or evidence["tests_passed"] <= 0:
        raise ContinuityManifestError("test evidence must contain passing tests and zero failures")
    if evidence["tests_collected"] != (
        evidence["tests_passed"] + evidence["tests_skipped"]
    ):
        raise ContinuityManifestError("test evidence accounting is inconsistent")
    return evidence


def refresh_manifest(
    manifest_path: Path,
    project_root: Path,
    *,
    test_evidence: Mapping[str, Any],
    certify_release_claims: bool = False,
) -> dict[str, Any]:
    """Refresh the tracked payload snapshot after a successful test run.

    External authority bindings are intentionally left untouched. Updating those
    hashes requires a separate, explicit provenance decision.
    """

    manifest_path = Path(manifest_path).resolve()
    project_root = Path(project_root).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuityManifestError(f"manifest is unreadable: {exc}") from exc
    manifest = _require_mapping(manifest, "manifest")
    if manifest.get("schema") != "peerbridge-continuity-manifest/v1":
        raise ContinuityManifestError("unsupported continuity manifest schema")
    try:
        manifest_relative = manifest_path.relative_to(project_root).as_posix()
    except ValueError as exc:
        raise ContinuityManifestError("manifest must be inside the project root") from exc

    peerbridge = _require_mapping(manifest.get("peerbridge"), "peerbridge")
    if peerbridge.get("manifest_excludes_self") != manifest_relative:
        raise ContinuityManifestError(
            "manifest_excludes_self must equal the continuity manifest Git path"
        )
    tracked_paths = _git_tracked_paths(project_root)
    if manifest_relative not in tracked_paths:
        raise ContinuityManifestError("continuity manifest must be Git tracked")
    rows = [
        _tracked_row(project_root, relative_path)
        for relative_path in sorted(tracked_paths - {manifest_relative})
    ]
    source_rows = [row for row in rows if not row["path"].lower().endswith(".md")]
    documentation_rows = [row for row in rows if row["path"].lower().endswith(".md")]
    payload_root = _payload_root(rows)
    normalized_evidence = _normalize_test_evidence(test_evidence, payload_root)

    claims = _require_mapping(manifest.get("claims"), "claims")
    for claim in RELEASE_READY_CLAIMS:
        claims[claim] = bool(certify_release_claims)
    claims["remote_mobile_e2e_ready"] = False
    claims["release_certified_payload_root_sha256"] = (
        payload_root if certify_release_claims else None
    )
    claims["test_evidence"] = normalized_evidence
    for field in (
        "tests_collected",
        "tests_failed",
        "tests_passed",
        "tests_skipped",
    ):
        claims[field] = normalized_evidence[field]
    peerbridge["source_files"] = source_rows
    peerbridge["documentation"] = documentation_rows
    peerbridge["tracked_payload_file_count"] = len(rows)
    peerbridge["tracked_payload_root_sha256"] = payload_root
    manifest["generated_at_utc"] = datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=manifest_path.parent,
            prefix=f".{manifest_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        os.replace(temporary_path, manifest_path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ContinuityManifestError(f"manifest refresh failed: {exc}") from exc
    return manifest


def _entries(manifest: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    peerbridge = _require_mapping(manifest.get("peerbridge"), "peerbridge")
    for section in ("source_files", "documentation"):
        rows = peerbridge.get(section)
        if not isinstance(rows, list):
            raise ContinuityManifestError(f"peerbridge.{section} must be an array")
        for index, row in enumerate(rows):
            yield "relative", _require_mapping(row, f"peerbridge.{section}[{index}]")
    rows = manifest.get("external_authorities", [])
    if not isinstance(rows, list):
        raise ContinuityManifestError("external_authorities must be an array")
    for index, row in enumerate(rows):
        yield "absolute", _require_mapping(row, f"external_authorities[{index}]")


def _validate_release_profile(manifest: dict[str, Any], release_profile: str) -> None:
    peerbridge = _require_mapping(manifest.get("peerbridge"), "peerbridge")
    claims = _require_mapping(manifest.get("claims"), "claims")
    if peerbridge.get("release_profile") != release_profile:
        raise ContinuityManifestError(
            f"release profile mismatch: {peerbridge.get('release_profile')!r}"
        )
    if release_profile != "local-alpha":
        raise ContinuityManifestError(f"unsupported release profile: {release_profile}")

    for name in RELEASE_READY_CLAIMS:
        if claims.get(name) is not True:
            raise ContinuityManifestError(
                f"release claim must be exactly true for {release_profile}: {name}"
            )
    for name in (
        "tests_collected",
        "tests_failed",
        "tests_passed",
        "tests_skipped",
    ):
        if not isinstance(claims.get(name), int) or isinstance(claims.get(name), bool):
            raise ContinuityManifestError(f"release test claim must be an integer: {name}")
    if claims["tests_failed"] != 0:
        raise ContinuityManifestError("release test failures must be zero")
    if claims["tests_passed"] <= 0:
        raise ContinuityManifestError("release must bind at least one passing test")
    if claims["tests_collected"] != claims["tests_passed"] + claims["tests_skipped"]:
        raise ContinuityManifestError("release test accounting is inconsistent")
    payload_root = peerbridge.get("tracked_payload_root_sha256")
    if claims.get("release_certified_payload_root_sha256") != payload_root:
        raise ContinuityManifestError("release claims are not bound to the live payload root")
    evidence = _normalize_test_evidence(
        _require_mapping(claims.get("test_evidence"), "claims.test_evidence"),
        str(payload_root),
    )
    for field in (
        "tests_collected",
        "tests_failed",
        "tests_passed",
        "tests_skipped",
    ):
        if claims[field] != evidence[field]:
            raise ContinuityManifestError("release test claims differ from bound evidence")


def verify_manifest(
    manifest_path: Path,
    project_root: Path,
    *,
    release_profile: str | None = None,
) -> dict[str, Any]:
    manifest_path = Path(manifest_path).resolve()
    project_root = Path(project_root).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuityManifestError(f"manifest is unreadable: {exc}") from exc
    manifest = _require_mapping(manifest, "manifest")
    if manifest.get("schema") != "peerbridge-continuity-manifest/v1":
        raise ContinuityManifestError("unsupported continuity manifest schema")

    verified: list[dict[str, Any]] = []
    tracked_rows: list[dict[str, Any]] = []
    seen_relative_paths: set[str] = set()
    for path_kind, row in _entries(manifest):
        raw_path = row.get("path")
        expected_sha = row.get("sha256")
        expected_bytes = row.get("bytes")
        if not isinstance(raw_path, str) or not raw_path:
            raise ContinuityManifestError("entry path is missing")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            raise ContinuityManifestError(f"entry SHA-256 is invalid: {raw_path}")
        if not isinstance(expected_bytes, int) or expected_bytes < 0:
            raise ContinuityManifestError(f"entry byte count is invalid: {raw_path}")

        if path_kind == "relative":
            if "\\" in raw_path or raw_path.startswith("./"):
                raise ContinuityManifestError(
                    f"relative entry must use canonical Git path syntax: {raw_path}"
                )
            if raw_path in seen_relative_paths:
                raise ContinuityManifestError(f"duplicate tracked entry: {raw_path}")
            seen_relative_paths.add(raw_path)
            path = (project_root / raw_path).resolve()
            try:
                path.relative_to(project_root)
            except ValueError as exc:
                raise ContinuityManifestError(
                    f"relative entry escapes project root: {raw_path}"
                ) from exc
        else:
            path = Path(raw_path).resolve()
            if not path.is_absolute():
                raise ContinuityManifestError(
                    f"authority entry must be absolute: {raw_path}"
                )

        if not path.is_file():
            raise ContinuityManifestError(f"bound file is missing: {raw_path}")
        actual_bytes = path.stat().st_size
        actual_sha = _sha256(path)
        if actual_bytes != expected_bytes:
            raise ContinuityManifestError(
                f"byte count drift: {raw_path}: {actual_bytes} != {expected_bytes}"
            )
        if actual_sha != expected_sha.lower():
            raise ContinuityManifestError(f"SHA-256 drift: {raw_path}")
        verified.append(
            {"bytes": actual_bytes, "path": raw_path, "sha256": actual_sha}
        )
        if path_kind == "relative":
            tracked_rows.append(verified[-1])

    peerbridge = _require_mapping(manifest.get("peerbridge"), "peerbridge")
    excluded_path = peerbridge.get("manifest_excludes_self")
    try:
        actual_manifest_relative = manifest_path.relative_to(project_root).as_posix()
    except ValueError as exc:
        raise ContinuityManifestError("manifest must be inside the project root") from exc
    if excluded_path != actual_manifest_relative:
        raise ContinuityManifestError(
            "manifest_excludes_self must equal the continuity manifest Git path"
        )

    git_payload_paths = _git_tracked_paths(project_root) - {excluded_path}
    if seen_relative_paths != git_payload_paths:
        missing = sorted(git_payload_paths - seen_relative_paths)
        unexpected = sorted(seen_relative_paths - git_payload_paths)
        raise ContinuityManifestError(
            "tracked inventory mismatch: "
            f"missing={missing[:10]!r}, unexpected={unexpected[:10]!r}"
        )

    declared_count = peerbridge.get("tracked_payload_file_count")
    if declared_count != len(tracked_rows):
        raise ContinuityManifestError(
            f"tracked payload count mismatch: {declared_count!r} != {len(tracked_rows)}"
        )
    if peerbridge.get("payload_root_algorithm") != (
        "sha256(path\\0bytes\\0sha256\\n for sorted entries)"
    ):
        raise ContinuityManifestError("unsupported tracked payload root algorithm")
    actual_root = _payload_root(tracked_rows)
    if peerbridge.get("tracked_payload_root_sha256") != actual_root:
        raise ContinuityManifestError("tracked payload aggregate root drift")

    if release_profile is not None:
        _validate_release_profile(manifest, release_profile)

    return {
        "manifest_path": str(manifest_path),
        "schema": manifest["schema"],
        "status": "PASS",
        "tracked_payload_file_count": len(tracked_rows),
        "tracked_payload_root_sha256": actual_root,
        "verified_file_count": len(verified),
        "verified_files": verified,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify SHA-bound PeerBridge continuity inputs without writes."
    )
    parser.add_argument(
        "--manifest",
        default="docs/continuity-manifest.json",
        type=Path,
    )
    parser.add_argument("--project-root", default=".", type=Path)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Atomically refresh tracked payload hashes before verification.",
    )
    parser.add_argument(
        "--test-evidence",
        type=Path,
        help="Root-bound PASS test receipt required by --refresh.",
    )
    parser.add_argument(
        "--certify-release-claims",
        action="store_true",
        help="Explicitly re-certify release claims for the refreshed payload root.",
    )
    parser.add_argument(
        "--release-profile",
        choices=("local-alpha",),
        help="Require the named release profile and its release-ready claims.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.refresh:
            if args.test_evidence is None:
                raise ContinuityManifestError(
                    "--refresh requires --test-evidence"
                )
            try:
                test_evidence = json.loads(args.test_evidence.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ContinuityManifestError("test evidence is unreadable") from exc
            if not isinstance(test_evidence, dict):
                raise ContinuityManifestError("test evidence must be an object")
            refresh_manifest(
                args.manifest,
                args.project_root,
                test_evidence=test_evidence,
                certify_release_claims=args.certify_release_claims,
            )
        elif args.test_evidence is not None or args.certify_release_claims:
            raise ContinuityManifestError(
                "test evidence and release certification require --refresh"
            )
        result = verify_manifest(
            args.manifest,
            args.project_root,
            release_profile=args.release_profile,
        )
        if args.refresh:
            result["refresh"] = "APPLIED"
    except ContinuityManifestError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
