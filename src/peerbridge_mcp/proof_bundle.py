"""Create-only, portable Proof Bundles for completed PeerBridge tasks."""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .bridge import Bridge, stable_sha256, utc_now
from .secret_scan import (
    contains_secret,
    contains_secret_bytes,
    decode_text_bytes,
    source_text_contains_secret,
)
from .trust_timeline import TrustTimeline, TrustTimelineError


BUNDLE_SCHEMA = "peerbridge-proof-bundle/v1"
MANIFEST_SCHEMA = "peerbridge-proof-bundle-manifest/v1"
MANIFEST_NAME = "MANIFEST.json"
PROOF_NAME = "proof-bundle.json"
MARKDOWN_NAME = "PROOF_BUNDLE.md"
VERIFIER_NAME = "verify_proof_bundle.py"
PROOF_OUTPUT_ROOT = ".peerbridge-artifacts/proof-bundles"
MAX_EVIDENCE_FILES = 100
MAX_EVIDENCE_FILE_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 128 * 1024 * 1024
WINDOWS_ABSOLUTE = re.compile(
    r"(?i)(?:^|[\s(\[{'\"])(?:[A-Z]:[\\/]|\\\\[^\\/\s]+[\\/])"
)
POSIX_ABSOLUTE = re.compile(
    r"(?<![:/A-Za-z0-9_.-])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
)


class ProofBundleError(RuntimeError):
    """A Proof Bundle cannot be exported without weakening its trust claims."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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


def _verifier_bytes() -> bytes:
    resource = importlib.resources.files("peerbridge_mcp").joinpath(
        "release_support", "verify_proof_bundle.py"
    )
    try:
        return resource.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise ProofBundleError("standalone Proof Bundle verifier is unavailable") from exc


def _verifier_namespace() -> dict[str, Any]:
    source = _verifier_bytes()
    namespace: dict[str, Any] = {
        "__file__": VERIFIER_NAME,
        "__name__": "peerbridge_proof_bundle_verifier",
    }
    exec(compile(source, VERIFIER_NAME, "exec"), namespace)
    return namespace


def verify_proof_bundle(bundle_root: Path) -> dict[str, Any]:
    """Run the exact bundled verifier in-process without writing any files."""

    verify = _verifier_namespace().get("verify_bundle")
    if not callable(verify):
        raise ProofBundleError("standalone Proof Bundle verifier has no verify function")
    result = verify(Path(bundle_root))
    if not isinstance(result, dict):
        raise ProofBundleError("standalone Proof Bundle verifier returned invalid data")
    return result


def _read_stable(path: Path) -> bytes:
    before = path.stat()
    if before.st_size > MAX_EVIDENCE_FILE_BYTES:
        raise ProofBundleError("evidence file exceeds the portable size limit")
    payload = path.read_bytes()
    after = path.stat()
    if (
        (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        or len(payload) != after.st_size
    ):
        raise ProofBundleError("evidence changed while the Proof Bundle was captured")
    return payload


def _source_binding(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(item["path"]),
        "bytes": int(item["bytes"]),
        "sha256": str(item["sha256"]),
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


def _event_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sequence": int(row["sequence"]),
        "event_id": str(row["event_id"]),
        "scope": str(row["scope"]),
        "actor": str(row["actor"]),
        "event_type": str(row["event_type"]),
        "task_id": str(row["task_id"]),
        "payload_sha256": str(row["payload_sha256"]),
        "created_utc": str(row["created_utc"]),
        "prev_chain_sha256": str(row["prev_chain_sha256"]),
        "chain_sha256": str(row["chain_sha256"]),
    }


def _trust_records(bridge: Bridge, task_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        live = TrustTimeline(bridge).timeline(task_id)
    except TrustTimelineError as exc:
        raise ProofBundleError(str(exc)) from exc
    if not live:
        raise ProofBundleError("task has no Trust Timeline records")
    records: list[dict[str, Any]] = []
    for item in live:
        if bool(item.get("stale")):
            raise ProofBundleError("stale trust evidence cannot enter a Proof Bundle")
        record = {
            "scope": str(item["scope"]),
            "record_id": str(item["record_id"]),
            "task_id": str(item["task_id"]),
            "actor": str(item["actor"]),
            "stage": str(item["stage"]),
            "statement": str(item["statement"]),
            "source_bindings": [
                _source_binding(binding) for binding in item["source_bindings"]
            ],
            "related_record_ids": [str(value) for value in item["related_record_ids"]],
            "created_utc": str(item["created_utc"]),
            "trust_sha256": str(item["trust_sha256"]),
        }
        if stable_sha256(_trust_payload(record)) != record["trust_sha256"]:
            raise ProofBundleError("Trust Timeline record digest changed during export")
        records.append(record)

    required = {"test", "proof", "review", "decision", "completion"}
    if not required.issubset({record["stage"] for record in records}):
        raise ProofBundleError("task is missing a required Proof Bundle trust stage")
    decisions = [record for record in records if record["stage"] == "decision"]
    if any(
        record["actor"] != "human-operator" or not record["source_bindings"]
        for record in decisions
    ):
        raise ProofBundleError(
            "Proof Bundle decision must be human-authored and source-bound"
        )
    by_id = {record["record_id"]: record for record in records}
    for completion in (record for record in records if record["stage"] == "completion"):
        related = [by_id.get(record_id) for record_id in completion["related_record_ids"]]
        if None in related or not {"test", "proof", "review", "decision"}.issubset(
            {str(record["stage"]) for record in related if record is not None}
        ):
            raise ProofBundleError(
                "completion must reference test, proof, review, and human decision records"
            )
        exact_source_sets = [
            {
                (
                    str(binding["path"]),
                    int(binding["bytes"]),
                    str(binding["sha256"]),
                )
                for binding in record["source_bindings"]
            }
            for record in related
            if record is not None
            and record["stage"] in {"test", "proof", "review", "decision"}
        ]
        completion_sources = {
            (
                str(binding["path"]),
                int(binding["bytes"]),
                str(binding["sha256"]),
            )
            for binding in completion["source_bindings"]
        }
        if not completion_sources or any(
            source_set != completion_sources for source_set in exact_source_sets
        ):
            raise ProofBundleError(
                "completion records do not bind one exact source state"
            )

    audit = bridge.verify_audit_chain()
    if not audit["valid"]:
        raise ProofBundleError("audit chain is invalid")
    event_by_record: dict[str, sqlite3.Row] = {}
    with bridge._connect() as connection:
        rows = connection.execute(
            """SELECT * FROM events
                WHERE scope=? AND task_id=? AND event_type LIKE 'trust.%'
                ORDER BY sequence""",
            (bridge.scope, task_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError as exc:
                raise ProofBundleError("trust audit payload is unreadable") from exc
            record_id = str(payload.get("record_id") or "")
            if record_id in by_id:
                if record_id in event_by_record:
                    raise ProofBundleError("trust record has duplicate audit events")
                record = by_id[record_id]
                if (
                    payload.get("trust_sha256") != record["trust_sha256"]
                    or row["actor"] != record["actor"]
                    or row["event_type"] != f"trust.{record['stage']}"
                ):
                    raise ProofBundleError("trust record differs from its audit event")
                event_by_record[record_id] = row
    if set(event_by_record) != set(by_id):
        raise ProofBundleError("a trust record has no matching audit event")
    positions = [
        {"record_id": record_id, **_event_snapshot(row)}
        for record_id, row in sorted(
            event_by_record.items(), key=lambda item: int(item[1]["sequence"])
        )
    ]
    return records, positions


def _portable_evidence(
    bridge: Bridge, records: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    bindings: dict[str, dict[str, Any]] = {}
    for record in records:
        for item in record["source_bindings"]:
            path = str(item["path"])
            existing = bindings.get(path)
            if existing is not None and existing != item:
                raise ProofBundleError("one evidence path has conflicting source hashes")
            bindings[path] = item
    if not bindings:
        raise ProofBundleError("Proof Bundle has no source-bound evidence")
    if len(bindings) > MAX_EVIDENCE_FILES:
        raise ProofBundleError("Proof Bundle exceeds the evidence file limit")
    collision_keys: set[str] = set()
    files: dict[str, bytes] = {}
    evidence: list[dict[str, Any]] = []
    total = 0
    for project_path, binding in sorted(bindings.items()):
        collision = project_path.casefold()
        if collision in collision_keys:
            raise ProofBundleError("evidence paths collide on a portable filesystem")
        collision_keys.add(collision)
        resolved = bridge._resolve_path(project_path, must_exist=True)
        payload = _read_stable(resolved)
        digest = _sha256_bytes(payload)
        if len(payload) != binding["bytes"] or digest != binding["sha256"]:
            raise ProofBundleError("live evidence differs from its trust binding")
        text = decode_text_bytes(payload)
        if text is None:
            unsafe = contains_secret_bytes(payload)
        else:
            unsafe = source_text_contains_secret(text, resolved.suffix)
            if _contains_absolute_path(text):
                raise ProofBundleError("evidence contains a private absolute path")
        if unsafe:
            raise ProofBundleError("evidence contains credential-like data")
        total += len(payload)
        if total > MAX_EVIDENCE_TOTAL_BYTES:
            raise ProofBundleError("Proof Bundle exceeds the total evidence size limit")
        bundle_path = PurePosixPath("evidence", *PurePosixPath(project_path).parts).as_posix()
        files[bundle_path] = payload
        evidence.append(
            {
                "project_path": project_path,
                "bundle_path": bundle_path,
                "bytes": len(payload),
                "sha256": digest,
            }
        )
    return evidence, files


def _markdown(proof: dict[str, Any]) -> str:
    def safe(value: Any) -> str:
        text = " ".join(str(value).splitlines())
        for source, replacement in (
            ("\\", "\\\\"),
            ("`", "\\`"),
            ("*", "\\*"),
            ("_", "\\_"),
            ("[", "\\["),
            ("]", "\\]"),
            ("<", "&lt;"),
            (">", "&gt;"),
            ("|", "\\|"),
        ):
            text = text.replace(source, replacement)
        return text

    lines = [
        "# PeerBridge Proof Bundle",
        "",
        f"- Task: `{safe(proof['task_id'])}`",
        f"- Scope: `{safe(proof['scope'])}`",
        f"- Captured: `{safe(proof['created_utc'])}`",
        f"- Bundle SHA-256: `{proof['bundle_sha256']}`",
        f"- Audit head: `{proof['audit_chain']['head_chain_sha256']}`",
        "",
        "## Identities",
        "",
    ]
    lines.extend(f"- `{safe(identity)}`" for identity in proof["identities"])
    lines.extend(["", "## Trust Timeline", ""])
    for record in proof["trust_records"]:
        lines.extend(
            [
                f"### {safe(record['stage']).title()} - `{safe(record['record_id'])}`",
                "",
                f"Actor: `{safe(record['actor'])}`",
                "",
                safe(record["statement"]),
                "",
            ]
        )
        for source in record["source_bindings"]:
            lines.append(
                f"- `{safe(source['path'])}` `{source['sha256']}` ({source['bytes']} bytes)"
            )
        if record["source_bindings"]:
            lines.append("")
    lines.extend(
        [
            "## Verification",
            "",
            "Use a separately installed, trusted PeerBridge copy:",
            "`python -m peerbridge_mcp.proof_bundle verify .`",
            "",
            "The verifier performs no writes. This unsigned bundle proves structural",
            "consistency only; it does not authenticate the sender or origin.",
            "",
        ]
    )
    return "\n".join(lines)


def _manifest(files: dict[str, bytes], proof: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "created_utc": proof["created_utc"],
        "bundle_sha256": proof["bundle_sha256"],
        "manifest_excludes_self": MANIFEST_NAME,
        "files": [
            {"path": path, "bytes": len(payload), "sha256": _sha256_bytes(payload)}
            for path, payload in sorted(files.items())
        ],
        "safety": {
            "create_only": True,
            "relative_paths_only": True,
            "trusted_external_verifier_required": True,
            "origin_authenticated": False,
        },
    }
    body["manifest_sha256"] = stable_sha256(body)
    return body


def _write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def create_proof_bundle(
    bridge: Bridge, *, task_id: str, output_path: Path
) -> dict[str, Any]:
    """Create one portable directory and refuse stale, incomplete, or unsafe evidence."""

    task_id = str(task_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", task_id):
        raise ProofBundleError("task id is invalid")
    root = bridge.root.resolve()
    output = Path(output_path)
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    try:
        relative_output = output.relative_to(root).as_posix()
    except ValueError as exc:
        raise ProofBundleError("Proof Bundle output must remain inside the project root") from exc
    allowed_artifact_output = relative_output == PROOF_OUTPUT_ROOT or relative_output.startswith(
        PROOF_OUTPUT_ROOT + "/"
    )
    if bridge._is_protected(relative_output) and not allowed_artifact_output:
        raise ProofBundleError("Proof Bundle output is inside a protected path")
    if output.exists():
        raise ProofBundleError("Proof Bundle output is create-only and already exists")

    records, positions = _trust_records(bridge, task_id)
    evidence, evidence_files = _portable_evidence(bridge, records)
    audit = bridge.verify_audit_chain()
    stage_counts = {
        stage: sum(record["stage"] == stage for record in records)
        for stage in sorted({record["stage"] for record in records})
    }
    proof: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "created_utc": utc_now(),
        "scope": bridge.scope,
        "task_id": task_id,
        "exported_by": bridge.agent_id,
        "identities": sorted(
            {bridge.agent_id, *(record["actor"] for record in records)}
        ),
        "stage_counts": stage_counts,
        "trust_records": records,
        "evidence_files": evidence,
        "audit_chain": {
            "verified_at_capture": True,
            "event_count": int(audit["event_count"]),
            "head_chain_sha256": str(audit["head_chain_sha256"]),
            "record_positions": positions,
            "raw_payloads_included": False,
        },
        "safety": {
            "absolute_paths_included": False,
            "cloud_collaboration_enabled": False,
            "credentials_included": False,
            "raw_event_payloads_included": False,
            "terminal_history_included": False,
            "origin_authenticated": False,
            "verification_scope": "structural_consistency_only",
        },
    }
    proof["bundle_sha256"] = stable_sha256(proof)
    proof_bytes = (
        json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    markdown_bytes = _markdown(proof).encode("utf-8")
    if _contains_absolute_path(proof) or _contains_absolute_path(
        markdown_bytes.decode("utf-8")
    ):
        raise ProofBundleError("Proof Bundle metadata contains an absolute path")
    if contains_secret(proof_bytes.decode("utf-8")) or contains_secret(
        markdown_bytes.decode("utf-8")
    ):
        raise ProofBundleError("Proof Bundle metadata contains credential-like data")
    files = {
        PROOF_NAME: proof_bytes,
        MARKDOWN_NAME: markdown_bytes,
        **evidence_files,
    }
    manifest = _manifest(files, proof)
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    )
    moved = False
    try:
        for name, payload in sorted(files.items()):
            _write_file(staging.joinpath(*PurePosixPath(name).parts), payload)
        _write_file(staging / MANIFEST_NAME, manifest_bytes)
        verified = verify_proof_bundle(staging)
        if not verified.get("valid"):
            raise ProofBundleError(
                "created Proof Bundle did not pass its standalone verifier: "
                + "; ".join(str(item) for item in verified.get("errors", []))
            )
        if output.exists():
            raise ProofBundleError("Proof Bundle output became occupied during capture")
        staging.rename(output)
        moved = True
    finally:
        if not moved and staging.exists():
            shutil.rmtree(staging)
    return {
        "status": "CAPTURED",
        "path": str(output),
        "relative_path": relative_output,
        "task_id": task_id,
        "bundle_sha256": proof["bundle_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "evidence_file_count": len(evidence),
        "trust_record_count": len(records),
        "trusted_external_verifier_required": True,
        "origin_authenticated": False,
        "cloud_collaboration_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--root", type=Path, default=Path.cwd())
    capture.add_argument("--db", type=Path, required=True)
    capture.add_argument("--scope", required=True)
    capture.add_argument("--agent-id", required=True)
    capture.add_argument("--session-id", required=True)
    capture.add_argument("--task-id", required=True)
    capture.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_proof_bundle(args.bundle)
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("valid") else 1
        root = args.root.resolve()
        db_path = args.db if args.db.is_absolute() else root / args.db
        bridge = Bridge(
            root,
            db_path,
            args.agent_id,
            args.scope,
            session_id=args.session_id,
        )
        result = create_proof_bundle(
            bridge, task_id=args.task_id, output_path=args.output
        )
    except (OSError, ProofBundleError, sqlite3.Error) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
