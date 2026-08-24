"""Source-bound acceptance receipt for the three principal official Agent paths."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .acpx_runner import verify_acpx_inference_receipt
from .bridge import (
    stable_sha256,
    trusted_inference_receipt_payload,
    utc_now,
)
from .claude_client_receipt import verify_receipt as verify_claude_receipt


ACCEPTANCE_SCHEMA = "peerbridge.official-integration-acceptance.v1"
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class OfficialIntegrationAcceptanceError(ValueError):
    """One child receipt or source binding cannot support acceptance."""


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _relative_file(root: Path, path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise OfficialIntegrationAcceptanceError("acceptance input escapes project root") from exc
    if _is_reparse(path) or not path.is_file():
        raise OfficialIntegrationAcceptanceError("acceptance input is not a regular file")
    size = path.stat().st_size
    if size < 1 or size > MAX_SOURCE_FILE_BYTES:
        raise OfficialIntegrationAcceptanceError("acceptance input size is invalid")
    return {
        "path": relative.as_posix(),
        "bytes": size,
        "sha256": _sha256(path),
    }


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )
    if completed.returncode != 0:
        raise OfficialIntegrationAcceptanceError("Git source state is unavailable")
    return completed.stdout


def source_manifest(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    raw_paths = _git(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ).split("\0")
    records: list[dict[str, Any]] = []
    for raw in sorted(path for path in raw_paths if path):
        path = root / raw
        if not path.is_file():
            continue
        records.append(_relative_file(root, path))
    if not records:
        raise OfficialIntegrationAcceptanceError("source manifest is empty")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    head = _git(root, "rev-parse", "HEAD").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise OfficialIntegrationAcceptanceError("Git HEAD is invalid")
    return {
        "head_commit": head,
        "dirty_entry_count": len([line for line in status.splitlines() if line]),
        "status_sha256": stable_sha256(status.splitlines()),
        "file_count": len(records),
        "files": records,
        "tree_sha256": stable_sha256(records),
    }


def _load_json(path: Path) -> dict[str, Any]:
    if _is_reparse(path) or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise OfficialIntegrationAcceptanceError("child receipt path is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialIntegrationAcceptanceError("child receipt is invalid JSON") from exc
    if not isinstance(value, dict):
        raise OfficialIntegrationAcceptanceError("child receipt must be an object")
    return value


def _trusted_acpx_binding(
    root: Path,
    receipt: Mapping[str, Any],
    receipt_sha256: str,
) -> dict[str, Any]:
    database = root / ".peerbridge" / "peerbridge.sqlite3"
    if not database.is_file() or _is_reparse(database):
        raise OfficialIntegrationAcceptanceError(
            "official ACPX receipt lacks a trusted dispatch database"
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM trusted_inference_receipts "
            "WHERE inference_receipt_sha256=? AND receipt_schema=?",
            (receipt_sha256, "peerbridge.acpx-inference-receipt.v1"),
        ).fetchall()
    except sqlite3.Error as exc:
        raise OfficialIntegrationAcceptanceError(
            "trusted ACPX dispatch evidence is unavailable"
        ) from exc
    finally:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()
    if len(rows) != 1:
        raise OfficialIntegrationAcceptanceError(
            "official ACPX receipt is not uniquely bound to a trusted dispatch"
        )
    row = rows[0]
    if stable_sha256(trusted_inference_receipt_payload(row)) != row["binding_sha256"]:
        raise OfficialIntegrationAcceptanceError(
            "trusted ACPX dispatch binding SHA-256 does not match"
        )
    for stored, claimed in (
        (row["message_id"], receipt.get("message_id")),
        (row["route_profile_id"], receipt.get("route_profile_id")),
        (row["route_profile_sha256"], receipt.get("route_profile_sha256")),
        (row["provider_id"], receipt.get("requested_provider_id")),
        (row["model_id"], receipt.get("requested_model")),
        (row["route_class"], receipt.get("route_class")),
    ):
        if stored != claimed:
            raise OfficialIntegrationAcceptanceError(
                "official ACPX receipt does not match its trusted dispatch"
            )
    return {
        "scope": row["scope"],
        "message_id": row["message_id"],
        "agent_id": row["agent_id"],
        "attempt_count": int(row["attempt_count"]),
        "binding_sha256": row["binding_sha256"],
    }


def _acpx_child(root: Path, path: Path, expected_agent: str) -> dict[str, Any]:
    receipt = _load_json(path)
    verified = verify_acpx_inference_receipt(receipt)
    if verified.get("observed_agent_name") != expected_agent:
        raise OfficialIntegrationAcceptanceError("official ACPX Agent identity drifted")
    if verified.get("route_class") != "official":
        raise OfficialIntegrationAcceptanceError("official ACPX route class drifted")
    if verified.get("mcp_canonical_tools_called") != ["bridge_status"]:
        raise OfficialIntegrationAcceptanceError("official ACPX tool evidence is incomplete")
    if verified.get("mcp_tool_call_count") != 1:
        raise OfficialIntegrationAcceptanceError("official ACPX tool count is invalid")
    if int(receipt.get("response_chars") or 0) < 1:
        raise OfficialIntegrationAcceptanceError("official ACPX inference output is missing")
    trusted_dispatch = _trusted_acpx_binding(
        root, receipt, str(verified["receipt_sha256"])
    )
    return {
        **_relative_file(root, path),
        "receipt_sha256": verified["receipt_sha256"],
        "observed_agent": verified["observed_agent_name"],
        "observed_version": verified["observed_agent_version"],
        "observed_model": verified["observed_model"],
        "tool": "bridge_status",
        "real_inference": True,
        "zero_write_verify": True,
        "trusted_dispatch": trusted_dispatch,
    }


def _claude_child(root: Path, path: Path) -> dict[str, Any]:
    receipt = _load_json(path)
    verified = verify_claude_receipt(path)
    if not verified.get("valid") or verified.get("writes_performed") != 0:
        raise OfficialIntegrationAcceptanceError("official Claude receipt verification failed")
    transcript = receipt.get("transcript")
    if not isinstance(transcript, Mapping):
        raise OfficialIntegrationAcceptanceError("official Claude transcript binding is missing")
    if transcript.get("real_model_inference_observed") is not True:
        raise OfficialIntegrationAcceptanceError("official Claude inference evidence is missing")
    if transcript.get("mcp_tool_invocation_observed") is not True:
        raise OfficialIntegrationAcceptanceError("official Claude MCP evidence is missing")
    if transcript.get("tool") != "bridge_status":
        raise OfficialIntegrationAcceptanceError("official Claude tool identity drifted")
    return {
        **_relative_file(root, path),
        "receipt_sha256": verified["receipt_sha256"],
        "observed_agent": "claude-code-native",
        "observed_version": transcript.get("claude_code_version"),
        "observed_model": transcript.get("observed_model_id"),
        "tool": "bridge_status",
        "real_inference": True,
        "zero_write_verify": True,
    }


def _acceptance_base(
    *,
    project_root: Path,
    codex_receipt: Path,
    claude_receipt: Path,
    grok_receipt: Path,
) -> dict[str, Any]:
    root = project_root.resolve()
    children = {
        "codex": _acpx_child(
            root, codex_receipt.resolve(), "@agentclientprotocol/codex-acp"
        ),
        "claude": _claude_child(root, claude_receipt.resolve()),
        "grok": _acpx_child(root, grok_receipt.resolve(), "grok-build"),
    }
    return {
        "schema": ACCEPTANCE_SCHEMA,
        "created_utc": utc_now(),
        "status": "PASS",
        "source": source_manifest(root),
        "children": children,
        "invariants": {
            "official_paths": ["codex", "claude", "grok"],
            "real_inference_count": 3,
            "peerbridge_mcp_tool_invocation_count": 3,
            "credential_contents_recorded": False,
            "hidden_reasoning_recorded": False,
            "writes_during_verification": 0,
        },
    }


def capture_acceptance(
    *,
    project_root: Path,
    codex_receipt: Path,
    claude_receipt: Path,
    grok_receipt: Path,
    output: Path,
) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError("official integration acceptance already exists")
    base = _acceptance_base(
        project_root=project_root,
        codex_receipt=codex_receipt,
        claude_receipt=claude_receipt,
        grok_receipt=grok_receipt,
    )
    value = {**base, "acceptance_sha256": stable_sha256(base)}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    return value


def verify_acceptance(output: Path, project_root: Path) -> dict[str, Any]:
    value = _load_json(output)
    claimed = value.get("acceptance_sha256")
    base = {key: item for key, item in value.items() if key != "acceptance_sha256"}
    if not isinstance(claimed, str) or stable_sha256(base) != claimed:
        raise OfficialIntegrationAcceptanceError("acceptance SHA-256 does not match")
    source = source_manifest(project_root)
    if source != value.get("source"):
        raise OfficialIntegrationAcceptanceError("accepted source state is stale")
    children = value.get("children")
    if not isinstance(children, Mapping):
        raise OfficialIntegrationAcceptanceError("acceptance child records are missing")
    root = project_root.resolve()
    for name in ("codex", "claude", "grok"):
        child = children.get(name)
        if not isinstance(child, Mapping):
            raise OfficialIntegrationAcceptanceError("acceptance child is missing")
        path = root / str(child.get("path") or "")
        live = _relative_file(root, path)
        if any(live[key] != child.get(key) for key in ("path", "bytes", "sha256")):
            raise OfficialIntegrationAcceptanceError("accepted child receipt is stale")
    _acceptance_base(
        project_root=root,
        codex_receipt=root / str(children["codex"]["path"]),
        claude_receipt=root / str(children["claude"]["path"]),
        grok_receipt=root / str(children["grok"]["path"]),
    )
    return {
        "valid": True,
        "writes_performed": 0,
        "acceptance_sha256": claimed,
        "source_tree_sha256": source["tree_sha256"],
        "child_count": 3,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture or verify official Agent acceptance.")
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex-receipt", type=Path)
    parser.add_argument("--claude-receipt", type=Path)
    parser.add_argument("--grok-receipt", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.project_root.resolve()
    try:
        if args.verify_only:
            result = verify_acceptance(args.output.resolve(), root)
        else:
            if not all((args.codex_receipt, args.claude_receipt, args.grok_receipt)):
                raise OfficialIntegrationAcceptanceError("three child receipts are required")
            result = capture_acceptance(
                project_root=root,
                codex_receipt=args.codex_receipt.resolve(),
                claude_receipt=args.claude_receipt.resolve(),
                grok_receipt=args.grok_receipt.resolve(),
                output=args.output.resolve(),
            )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OfficialIntegrationAcceptanceError",
    "capture_acceptance",
    "source_manifest",
    "verify_acceptance",
]
