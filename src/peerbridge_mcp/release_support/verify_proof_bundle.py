"""Trusted installed verifier for a portable PeerBridge Proof Bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any


BUNDLE_SCHEMA = "peerbridge-proof-bundle/v1"
MANIFEST_SCHEMA = "peerbridge-proof-bundle-manifest/v1"
MANIFEST_NAME = "MANIFEST.json"
PROOF_NAME = "proof-bundle.json"
MARKDOWN_NAME = "PROOF_BUNDLE.md"
VERIFIER_NAME = "verify_proof_bundle.py"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
WINDOWS_ABSOLUTE = re.compile(
    r"(?i)(?:^|[\s(\[{'\"])(?:[A-Z]:[\\/]|\\\\[^\\/\s]+[\\/])"
)
POSIX_ABSOLUTE = re.compile(
    r"(?<![:/A-Za-z0-9_.-])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
)
TOKEN_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:sk-|pk-|rk-|ghp_|github_pat_|xox[a-z]-|AIza)"
    r"[A-Za-z0-9_.\-]{10,}|\bAKIA[0-9A-Z]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|authorization|password|secret|token)"
    r"\b[\"']?\s*[:=]\s*[\"']?(?P<value>[^\r\n,;}&\"']+)"
)
MAX_BUNDLE_FILES = 205
MAX_BUNDLE_FILE_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_TOTAL_BYTES = 160 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


class VerificationError(ValueError):
    """A Proof Bundle is malformed or no longer matches its manifest."""


def _is_link_or_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _observed_bundle_files(root: Path) -> set[str]:
    """List regular files without traversing links or Windows reparse points."""

    observed: set[str] = set()
    total_bytes = 0
    pending = [(root, PurePosixPath())]
    while pending:
        directory, relative_directory = pending.pop()
        try:
            directory_info = directory.lstat()
        except OSError as exc:
            raise VerificationError("Proof Bundle directory became unreadable") from exc
        if _is_link_or_reparse(directory_info):
            raise VerificationError("Proof Bundle contains a link or reparse point")
        if not stat.S_ISDIR(directory_info.st_mode):
            raise VerificationError("Proof Bundle contains a non-directory path")
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    relative = relative_directory / entry.name
                    if _is_link_or_reparse(info):
                        raise VerificationError(
                            "Proof Bundle contains a link or reparse point"
                        )
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((Path(entry.path), relative))
                    elif stat.S_ISREG(info.st_mode):
                        observed.add(relative.as_posix())
                        if len(observed) > MAX_BUNDLE_FILES + 1:
                            raise VerificationError(
                                "Proof Bundle contains too many filesystem entries"
                            )
                        if info.st_size > MAX_BUNDLE_FILE_BYTES:
                            raise VerificationError(
                                "Proof Bundle file exceeds the byte limit"
                            )
                        total_bytes += info.st_size
                        if total_bytes > MAX_BUNDLE_TOTAL_BYTES:
                            raise VerificationError(
                                "Proof Bundle exceeds the aggregate byte limit"
                            )
                    else:
                        raise VerificationError(
                            "Proof Bundle contains a non-regular filesystem entry"
                        )
        except OSError as exc:
            raise VerificationError("Proof Bundle directory became unreadable") from exc
    return observed


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bounded_bytes(path: Path, limit: int, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise VerificationError(f"unreadable {label}: {path.name}") from exc
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise VerificationError(f"{label} is not a regular file: {path.name}")
    if before.st_size > limit:
        raise VerificationError(f"{label} exceeds the byte limit: {path.name}")
    try:
        with path.open("rb") as handle:
            payload = handle.read(limit + 1)
    except OSError as exc:
        raise VerificationError(f"unreadable {label}: {path.name}") from exc
    if len(payload) > limit or len(payload) != before.st_size:
        raise VerificationError(f"{label} changed or exceeds its byte limit: {path.name}")
    return payload


def _sha256_file(path: Path, expected_bytes: int) -> str:
    try:
        before = path.lstat()
    except OSError as exc:
        raise VerificationError(f"bundle file became unreadable: {path.name}") from exc
    if (
        _is_link_or_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size != expected_bytes
        or expected_bytes > MAX_BUNDLE_FILE_BYTES
    ):
        raise VerificationError(
            f"Proof Bundle file differs from manifest: {path.name} (byte count)"
        )
    digest = hashlib.sha256()
    observed_bytes = 0
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
                observed_bytes += len(block)
                if observed_bytes > expected_bytes:
                    raise VerificationError(f"bundle file changed while hashing: {path.name}")
                digest.update(block)
    except OSError as exc:
        raise VerificationError(f"bundle file became unreadable: {path.name}") from exc
    if observed_bytes != expected_bytes:
        raise VerificationError(f"bundle file changed while hashing: {path.name}")
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_bounded_bytes(path, MAX_JSON_BYTES, "JSON").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"unreadable JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"JSON root is not an object: {path.name}")
    return value


def _contains_absolute_path(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_absolute_path(key) or _contains_absolute_path(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_absolute_path(item) for item in value)
    if not isinstance(value, str):
        return False
    return bool(WINDOWS_ABSOLUTE.search(value) or POSIX_ABSOLUTE.search(value))


def _contains_credential_bytes(payload: bytes) -> bool:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return bool(
            re.search(
                rb"(?i)(?:sk-|pk-|rk-|ghp_|github_pat_|xox[a-z]-|AIza)"
                rb"[A-Za-z0-9_.\-]{10,}|AKIA[0-9A-Z]{16}|"
                rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                payload,
            )
        )
    if TOKEN_PATTERN.search(text):
        return True
    for match in ASSIGNMENT_PATTERN.finditer(text):
        candidate = match.group("value").strip()
        if len(candidate) >= 12 and candidate.lower() not in {
            "not included",
            "not recorded",
        }:
            return True
    return False


def _relative_file(root: Path, value: Any) -> Path:
    text = str(value or "")
    if not text or "\\" in text:
        raise VerificationError("bundle paths must be non-empty POSIX-relative paths")
    relative = PurePosixPath(text)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise VerificationError("bundle path escapes its root")
    candidate = root.joinpath(*relative.parts)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise VerificationError(f"bundle file is missing: {text}") from exc
        if _is_link_or_reparse(info):
            raise VerificationError("bundle contains a link or reparse point")
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise VerificationError("bundle path escapes its root") from exc
    if not candidate.is_file():
        raise VerificationError(f"bundle file is missing: {text}")
    return candidate


def _event_envelope(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "event_id",
            "scope",
            "actor",
            "event_type",
            "task_id",
            "payload_sha256",
            "created_utc",
            "prev_chain_sha256",
        )
    }


def _trust_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "scope",
            "record_id",
            "task_id",
            "actor",
            "stage",
            "statement",
            "source_bindings",
            "related_record_ids",
            "created_utc",
        )
    }


def _source_keys(item: dict[str, Any]) -> set[tuple[str, int, str]]:
    rows = item.get("source_bindings")
    if not isinstance(rows, list):
        raise VerificationError("trust source bindings are invalid")
    result: set[tuple[str, int, str]] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
            raise VerificationError("trust source binding shape is invalid")
        path = str(row["path"])
        if path.startswith("/") or "\\" in path or ".." in PurePosixPath(path).parts:
            raise VerificationError("trust source path is not project-relative")
        size = row["bytes"]
        digest = str(row["sha256"])
        if not isinstance(size, int) or size < 0 or not SHA256.fullmatch(digest):
            raise VerificationError("trust source binding is invalid")
        result.add((path, size, digest))
    if len(result) != len(rows):
        raise VerificationError("trust source bindings contain duplicates")
    return result


def _verify_semantics(proof: dict[str, Any], manifest_paths: set[str]) -> None:
    if proof.get("schema") != BUNDLE_SCHEMA:
        raise VerificationError("unsupported Proof Bundle schema")
    expected = str(proof.get("bundle_sha256") or "")
    unsigned = dict(proof)
    unsigned.pop("bundle_sha256", None)
    if not SHA256.fullmatch(expected) or _canonical_sha256(unsigned) != expected:
        raise VerificationError("Proof Bundle SHA-256 mismatch")
    if _contains_absolute_path(proof):
        raise VerificationError("Proof Bundle contains an absolute path")
    if _contains_credential_bytes(
        json.dumps(proof, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ):
        raise VerificationError("Proof Bundle contains credential-like data")

    safety = proof.get("safety")
    if safety != {
        "absolute_paths_included": False,
        "cloud_collaboration_enabled": False,
        "credentials_included": False,
        "raw_event_payloads_included": False,
        "terminal_history_included": False,
        "origin_authenticated": False,
        "verification_scope": "structural_consistency_only",
    }:
        raise VerificationError("Proof Bundle safety declaration is invalid")

    evidence = proof.get("evidence_files")
    if not isinstance(evidence, list) or not evidence:
        raise VerificationError("Proof Bundle has no portable evidence")
    evidence_by_project: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {
            "project_path",
            "bundle_path",
            "bytes",
            "sha256",
        }:
            raise VerificationError("portable evidence entry shape is invalid")
        project_path = str(item["project_path"])
        bundle_path = str(item["bundle_path"])
        if project_path in evidence_by_project:
            raise VerificationError("portable evidence project path is duplicated")
        if not bundle_path.startswith("evidence/") or bundle_path not in manifest_paths:
            raise VerificationError("portable evidence is absent from the manifest")
        if item["bytes"] < 0 or not SHA256.fullmatch(str(item["sha256"])):
            raise VerificationError("portable evidence binding is invalid")
        evidence_by_project[project_path] = item

    records = proof.get("trust_records")
    if not isinstance(records, list) or not records:
        raise VerificationError("Proof Bundle has no trust records")
    by_id: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict):
            raise VerificationError("trust record is invalid")
        record_id = str(item.get("record_id") or "")
        if not record_id or record_id in by_id:
            raise VerificationError("trust record id is empty or duplicated")
        if _canonical_sha256(_trust_payload(item)) != item.get("trust_sha256"):
            raise VerificationError("trust record SHA-256 mismatch")
        for project_path, size, digest in _source_keys(item):
            bound = evidence_by_project.get(project_path)
            if bound is None or (bound["bytes"], bound["sha256"]) != (size, digest):
                raise VerificationError("trust source is not bound to portable evidence")
        by_id[record_id] = item
    for item in records:
        related = item.get("related_record_ids")
        if not isinstance(related, list) or any(str(value) not in by_id for value in related):
            raise VerificationError("trust record references an absent related record")

    required = {"test", "proof", "review", "decision", "completion"}
    if not required.issubset({str(item.get("stage")) for item in records}):
        raise VerificationError("Proof Bundle lacks a required trust stage")
    decisions = [item for item in records if item.get("stage") == "decision"]
    if any(
        item.get("actor") != "human-operator" or not _source_keys(item)
        for item in decisions
    ):
        raise VerificationError(
            "Proof Bundle decision must be human-authored and source-bound"
        )
    completions = [item for item in records if item.get("stage") == "completion"]
    for completion in completions:
        related = [by_id[str(value)] for value in completion["related_record_ids"]]
        related_stages = {str(item.get("stage")) for item in related}
        if not {"test", "proof", "review", "decision"}.issubset(related_stages):
            raise VerificationError("completion is not bound to all required trust stages")
        source_sets = [
            _source_keys(item)
            for item in related
            if item.get("stage") in {"test", "proof", "review", "decision"}
        ]
        completion_sources = _source_keys(completion)
        if not completion_sources or any(item != completion_sources for item in source_sets):
            raise VerificationError("completion records do not bind one exact source state")

    audit = proof.get("audit_chain")
    if not isinstance(audit, dict):
        raise VerificationError("audit-chain position is absent")
    if audit.get("verified_at_capture") is not True:
        raise VerificationError("audit chain was not verified at capture")
    if audit.get("raw_payloads_included") is not False:
        raise VerificationError("raw audit payloads must be absent")
    positions = audit.get("record_positions")
    if not isinstance(positions, list) or len(positions) != len(records):
        raise VerificationError("audit-chain positions do not cover every trust record")
    previous_sequence = 0
    seen_positions: set[str] = set()
    for position in positions:
        if not isinstance(position, dict):
            raise VerificationError("audit-chain position is invalid")
        record_id = str(position.get("record_id") or "")
        record = by_id.get(record_id)
        if record is None or record_id in seen_positions:
            raise VerificationError("audit-chain position record is absent or duplicated")
        seen_positions.add(record_id)
        sequence = position.get("sequence")
        if not isinstance(sequence, int) or sequence <= previous_sequence:
            raise VerificationError("audit-chain positions are not strictly ordered")
        previous_sequence = sequence
        if _canonical_sha256(_event_envelope(position)) != position.get("chain_sha256"):
            raise VerificationError("audit event chain SHA-256 mismatch")
        if (
            position.get("scope") != record.get("scope")
            or position.get("task_id") != record.get("task_id")
            or position.get("actor") != record.get("actor")
            or position.get("event_type") != f"trust.{record.get('stage')}"
        ):
            raise VerificationError("audit event identity differs from its trust record")
    if audit.get("event_count", 0) < previous_sequence:
        raise VerificationError("audit-chain event count precedes a bound position")
    if not SHA256.fullmatch(str(audit.get("head_chain_sha256") or "")):
        raise VerificationError("audit-chain head is invalid")

    identities = proof.get("identities")
    expected_identities = sorted(
        {str(proof.get("exported_by") or ""), *(str(item["actor"]) for item in records)}
    )
    if identities != expected_identities or not all(expected_identities):
        raise VerificationError("Proof Bundle identities are incomplete")


def verify_bundle(bundle_root: Path) -> dict[str, Any]:
    errors: list[str] = []
    bundle_sha256 = ""
    files_verified = 0
    try:
        supplied_root = Path(bundle_root).absolute()
        try:
            supplied_info = supplied_root.lstat()
        except OSError as exc:
            raise VerificationError("Proof Bundle root is not a directory") from exc
        if _is_link_or_reparse(supplied_info):
            raise VerificationError("Proof Bundle root is a link or reparse point")
        root = supplied_root.resolve()
        if not root.is_dir():
            raise VerificationError("Proof Bundle root is not a directory")
        observed = _observed_bundle_files(root)
        manifest = _read_json(root / MANIFEST_NAME)
        if manifest.get("schema") != MANIFEST_SCHEMA:
            raise VerificationError("unsupported Proof Bundle manifest schema")
        expected_manifest_sha = str(manifest.get("manifest_sha256") or "")
        unsigned_manifest = dict(manifest)
        unsigned_manifest.pop("manifest_sha256", None)
        if (
            not SHA256.fullmatch(expected_manifest_sha)
            or _canonical_sha256(unsigned_manifest) != expected_manifest_sha
        ):
            raise VerificationError("Proof Bundle manifest SHA-256 mismatch")
        if manifest.get("manifest_excludes_self") != MANIFEST_NAME:
            raise VerificationError("Proof Bundle manifest self-exclusion is invalid")
        if manifest.get("safety") != {
            "create_only": True,
            "relative_paths_only": True,
            "trusted_external_verifier_required": True,
            "origin_authenticated": False,
        }:
            raise VerificationError("Proof Bundle manifest safety declaration is invalid")
        if _contains_absolute_path(manifest):
            raise VerificationError("Proof Bundle manifest contains an absolute path")
        rows = manifest.get("files")
        if not isinstance(rows, list) or not rows or len(rows) > MAX_BUNDLE_FILES:
            raise VerificationError("Proof Bundle manifest file list is invalid")
        by_path: dict[str, dict[str, Any]] = {}
        declared_total_bytes = 0
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
                raise VerificationError("Proof Bundle manifest entry shape is invalid")
            name = str(row["path"])
            if name == MANIFEST_NAME or name in by_path:
                raise VerificationError("Proof Bundle manifest path is duplicated")
            if type(row["bytes"]) is not int or row["bytes"] < 0:
                raise VerificationError("Proof Bundle manifest size is invalid")
            if row["bytes"] > MAX_BUNDLE_FILE_BYTES:
                raise VerificationError("Proof Bundle manifest file exceeds the byte limit")
            declared_total_bytes += row["bytes"]
            if declared_total_bytes > MAX_BUNDLE_TOTAL_BYTES:
                raise VerificationError("Proof Bundle manifest exceeds the aggregate byte limit")
            if not SHA256.fullmatch(str(row["sha256"])):
                raise VerificationError("Proof Bundle manifest digest is invalid")
            by_path[name] = row
        required = {PROOF_NAME, MARKDOWN_NAME}
        if not required.issubset(by_path):
            raise VerificationError("Proof Bundle manifest lacks a required file")

        if observed != set(by_path) | {MANIFEST_NAME}:
            raise VerificationError("Proof Bundle contains an unlisted or missing file")
        for name, row in by_path.items():
            path = _relative_file(root, name)
            if _sha256_file(path, row["bytes"]) != row["sha256"]:
                raise VerificationError(f"Proof Bundle file differs from manifest: {name}")
            files_verified += 1

        proof = _read_json(root / PROOF_NAME)
        bundle_sha256 = str(proof.get("bundle_sha256") or "")
        if manifest.get("bundle_sha256") != bundle_sha256:
            raise VerificationError("manifest and Proof Bundle digests differ")
        _verify_semantics(proof, set(by_path))
        for name in {MARKDOWN_NAME, *(str(item["bundle_path"]) for item in proof["evidence_files"])}:
            payload = _read_bounded_bytes(
                _relative_file(root, name), MAX_BUNDLE_FILE_BYTES, "bundle content"
            )
            if _contains_credential_bytes(payload):
                raise VerificationError(f"credential-like data found in {name}")
            try:
                text = payload.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = ""
            if text and _contains_absolute_path(text):
                raise VerificationError(f"absolute path found in {name}")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}:{exc}")
    return {
        "valid": not errors,
        "bundle_sha256": bundle_sha256,
        "files_verified": files_verified,
        "errors": errors,
        "writes_performed": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundle",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    args = parser.parse_args(argv)
    result = verify_bundle(args.bundle)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
