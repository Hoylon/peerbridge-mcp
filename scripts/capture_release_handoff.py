"""Create and verify bounded, secret-free release handoff checkpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from peerbridge_mcp.secret_scan import contains_secret


SCHEMA = "peerbridge-release-handoff/v1"
MAX_CHECKPOINT_BYTES = 256 * 1024
MAX_CHANGED_ENTRIES = 300
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DEFAULT_AUTHORITIES = (
    "ROADMAP.md",
    "docs/ALPHA_5_2_REQUIREMENTS.md",
    "docs/DESKTOP_FEATURE_GAP_REGISTER_20260815.md",
)


class HandoffError(ValueError):
    """A release checkpoint cannot be captured or verified safely."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _identifier(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not IDENTIFIER.fullmatch(normalized):
        raise HandoffError(f"{label} is invalid")
    return normalized


def _inside(root: Path, value: Path, label: str) -> Path:
    candidate = value if value.is_absolute() else root / value
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HandoffError(f"{label} escapes project root") from exc
    return resolved


def _binding(root: Path, value: Path, label: str) -> dict[str, Any]:
    path = _inside(root, value, label)
    if not path.is_file():
        raise HandoffError(f"{label} is not a file")
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HandoffError(f"git {' '.join(args)} failed") from exc
    return completed.stdout.strip()


def _git_state(root: Path) -> dict[str, Any]:
    raw_status = _git(root, "status", "--short", "--untracked-files=normal")
    entries = raw_status.splitlines() if raw_status else []
    encoded = raw_status.encode("utf-8")
    return {
        "head": _git(root, "rev-parse", "HEAD"),
        "branch": _git(root, "branch", "--show-current") or None,
        "status_entry_count": len(entries),
        "status_entries": entries[:MAX_CHANGED_ENTRIES],
        "status_entries_truncated": len(entries) > MAX_CHANGED_ENTRIES,
        "status_sha256": _sha256_bytes(encoded),
        "clean": not entries,
    }


def _read_checkpoint(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffError(f"checkpoint is unreadable: {exc}") from exc
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise HandoffError("checkpoint exceeds the bounded size limit")
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise HandoffError("unsupported checkpoint schema")
    expected = payload.get("checkpoint_sha256")
    unsigned = dict(payload)
    unsigned.pop("checkpoint_sha256", None)
    actual = _sha256_bytes(_canonical_bytes(unsigned))
    if expected != actual:
        raise HandoffError("checkpoint SHA-256 mismatch")
    if contains_secret(raw.decode("utf-8")):
        raise HandoffError("checkpoint contains credential-like data")
    return payload


def build_checkpoint(
    project_root: Path,
    *,
    release: str,
    package_version: str,
    phase: str,
    next_phase: str,
    authorities: Iterable[Path] = (),
    previous: Path | None = None,
    backup: Path | None = None,
    evidence: Iterable[Path] = (),
) -> dict[str, Any]:
    root = project_root.resolve()
    if not root.is_dir():
        raise HandoffError("project root is not a directory")
    release = _identifier(release, "release")
    package_version = _identifier(package_version, "package version")
    phase = _identifier(phase, "phase")
    next_phase = _identifier(next_phase, "next phase")

    authority_paths = tuple(authorities) or tuple(root / item for item in DEFAULT_AUTHORITIES)
    authority_bindings = [
        _binding(root, path, "authority") for path in authority_paths
    ]
    if len({item["path"] for item in authority_bindings}) != len(authority_bindings):
        raise HandoffError("authority bindings contain duplicates")

    previous_binding: dict[str, Any] | None = None
    chain_index = 1
    if previous is not None:
        previous_path = _inside(root, previous, "previous checkpoint")
        previous_payload = _read_checkpoint(previous_path)
        if previous_payload.get("release") != release:
            raise HandoffError("previous checkpoint belongs to another release")
        if previous_payload.get("package_version") != package_version:
            raise HandoffError("previous checkpoint package version differs")
        chain_index = int(previous_payload.get("chain_index") or 0) + 1
        previous_binding = _binding(root, previous_path, "previous checkpoint")

    evidence_bindings = [_binding(root, path, "evidence") for path in evidence]
    if len({item["path"] for item in evidence_bindings}) != len(evidence_bindings):
        raise HandoffError("evidence bindings contain duplicates")

    body: dict[str, Any] = {
        "schema": SCHEMA,
        "release": release,
        "package_version": package_version,
        "chain_index": chain_index,
        "phase": phase,
        "next_phase": next_phase,
        "git": _git_state(root),
        "authority_bindings": authority_bindings,
        "previous_checkpoint": previous_binding,
        "backup_binding": _binding(root, backup, "backup") if backup else None,
        "evidence_bindings": evidence_bindings,
        "claims": {
            "implementation_complete": False,
            "release_ready": False,
            "published": False,
        },
        "recovery_contract": {
            "recompute_live_git_before_write": True,
            "preserve_uncommitted_user_files": True,
            "raw_chat_or_session_embedded": False,
            "credentials_embedded": False,
            "old_artifacts_may_be_overwritten": False,
            "test_evidence_is_not_release_success": True,
        },
    }
    body["checkpoint_sha256"] = _sha256_bytes(_canonical_bytes(body))
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise HandoffError("checkpoint exceeds the bounded size limit")
    if contains_secret(encoded.decode("utf-8")):
        raise HandoffError("checkpoint contains credential-like data")
    return body


def write_checkpoint(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    output = path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise HandoffError("checkpoint output is create-only and already exists") from exc
    verified = _read_checkpoint(output)
    return {
        "status": "PASS",
        "path": str(output),
        "chain_index": verified["chain_index"],
        "checkpoint_sha256": verified["checkpoint_sha256"],
    }


def verify_chain(project_root: Path, latest: Path) -> dict[str, Any]:
    root = project_root.resolve()
    current = _inside(root, latest, "checkpoint")
    seen: set[Path] = set()
    expected_index: int | None = None
    release: str | None = None
    package_version: str | None = None
    count = 0
    while True:
        if current in seen:
            raise HandoffError("checkpoint chain contains a cycle")
        seen.add(current)
        payload = _read_checkpoint(current)
        if expected_index is None:
            expected_index = int(payload["chain_index"])
            release = str(payload["release"])
            package_version = str(payload["package_version"])
        if int(payload["chain_index"]) != expected_index:
            raise HandoffError("checkpoint chain index is discontinuous")
        if payload["release"] != release or payload["package_version"] != package_version:
            raise HandoffError("checkpoint chain release identity changed")
        count += 1
        previous = payload.get("previous_checkpoint")
        if previous is None:
            if expected_index != 1:
                raise HandoffError("checkpoint chain ends before index one")
            break
        if not isinstance(previous, dict):
            raise HandoffError("previous checkpoint binding is invalid")
        previous_path = _inside(
            root, Path(str(previous.get("path") or "")), "previous checkpoint"
        )
        binding = _binding(root, previous_path, "previous checkpoint")
        if binding != previous:
            raise HandoffError("previous checkpoint binding drifted")
        current = previous_path
        expected_index -= 1
    return {
        "status": "PASS",
        "latest": str(latest.resolve()),
        "release": release,
        "package_version": package_version,
        "checkpoint_count": count,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--project-root", type=Path, default=Path.cwd())
    capture.add_argument("--release", required=True)
    capture.add_argument("--package-version", required=True)
    capture.add_argument("--phase", required=True)
    capture.add_argument("--next-phase", required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--previous", type=Path)
    capture.add_argument("--backup", type=Path)
    capture.add_argument("--authority", type=Path, action="append", default=[])
    capture.add_argument("--evidence", type=Path, action="append", default=[])
    verify = subparsers.add_parser("verify")
    verify.add_argument("--project-root", type=Path, default=Path.cwd())
    verify.add_argument("--checkpoint", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "capture":
            root = args.project_root.resolve()
            payload = build_checkpoint(
                root,
                release=args.release,
                package_version=args.package_version,
                phase=args.phase,
                next_phase=args.next_phase,
                authorities=[_inside(root, item, "authority") for item in args.authority],
                previous=args.previous,
                backup=args.backup,
                evidence=args.evidence,
            )
            result = write_checkpoint(args.output, payload)
        else:
            result = verify_chain(args.project_root, args.checkpoint)
    except HandoffError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
