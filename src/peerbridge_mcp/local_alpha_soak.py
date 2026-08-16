"""Repeatable low-memory and crash-recovery evidence for the local Alpha.

The harness uses an isolated temporary PeerBridge database and synthetic room
history.  It never opens provider credentials or sends model requests.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .bridge import Bridge, stable_sha256, utc_now
from .mailbox_supervisor import (
    SupervisorAlreadyRunningError,
    _ProcessFileLock,
)
from .monitor import BridgeReader
from .resource_guard import (
    ResourcePolicy,
    RuntimeCapacityError,
    memory_snapshot,
    provider_runtime_slot,
)


RECEIPT_SCHEMA = "peerbridge-local-alpha-soak-receipt/v1"
DEFAULT_MESSAGE_COUNT = 1_200
DEFAULT_PAGE_LIMIT = 40
DEFAULT_SAMPLE_ROUNDS = 24
DEFAULT_MAX_PLATEAU_GROWTH_MIB = 24.0
MIB = 1024 * 1024
SOURCE_BINDINGS = (
    "src/peerbridge_mcp/bridge.py",
    "src/peerbridge_mcp/mailbox_supervisor.py",
    "src/peerbridge_mcp/monitor.py",
    "src/peerbridge_mcp/resource_guard.py",
    "src/peerbridge_mcp/local_alpha_soak.py",
)


class SoakError(RuntimeError):
    """A safe soak or receipt verification failure."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_binding(project_root: Path, relative_path: str) -> dict[str, Any]:
    path = (project_root / relative_path).resolve()
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise SoakError(f"source binding escapes project root: {relative_path}") from exc
    if not path.is_file():
        raise SoakError(f"source binding is missing: {relative_path}")
    return {
        "bytes": path.stat().st_size,
        "path": relative_path,
        "sha256": _file_sha256(path),
    }


def _memory_reading() -> dict[str, int]:
    observed = memory_snapshot()
    return {
        "process_private_bytes": int(observed.process_private_bytes),
        "process_working_set_bytes": int(observed.process_working_set_bytes),
    }


def _plateau_metric(samples: list[dict[str, int]]) -> tuple[str, list[int]]:
    private = [int(row["process_private_bytes"]) for row in samples]
    working = [int(row["process_working_set_bytes"]) for row in samples]
    if any(private):
        return "process_private_bytes", private
    return "process_working_set_bytes", working


def _wait_for_file(path: Path, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if process.poll() is not None:
            _stdout, stderr = process.communicate(timeout=1.0)
            detail = stderr.decode("utf-8", errors="replace").strip().replace("\r", " ").replace("\n", " ")
            suffix = f": {detail[-600:]}" if detail else ""
            raise SoakError(
                "crash-recovery helper exited before acquiring its locks" + suffix
            )
        time.sleep(0.05)
    raise SoakError("crash-recovery helper did not become ready")


def _crash_release_probe(temp_root: Path) -> dict[str, Any]:
    lock_path = temp_root / "mailbox-supervisor-soak.lock"
    slot_root = temp_root / "runtime-slots"
    ready_path = temp_root / "helper.ready"
    command = [
        sys.executable,
        "-m",
        "peerbridge_mcp.local_alpha_soak",
        "--hold-crash-locks",
        "--lock-path",
        str(lock_path),
        "--slot-root",
        str(slot_root),
        "--ready-path",
        str(ready_path),
    ]
    allowed_environment = (
        "APPDATA",
        "COMSPEC",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    )
    environment = {
        key: os.environ[key]
        for key in allowed_environment
        if key in os.environ
    }
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PYTHONUTF8": "1",
        }
    )
    process = subprocess.Popen(
        command,
        cwd=temp_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    duplicate_supervisor_rejected = False
    duplicate_runtime_rejected = False
    post_crash_supervisor_reacquired = False
    post_crash_runtime_reacquired = False
    policy = ResourcePolicy(
        min_available_bytes=1,
        min_available_fraction=0.0,
        max_concurrent_runtimes=1,
    )
    try:
        _wait_for_file(ready_path, process, 10.0)
        try:
            with _ProcessFileLock(lock_path):
                pass
        except SupervisorAlreadyRunningError:
            duplicate_supervisor_rejected = True
        try:
            with provider_runtime_slot(
                policy=policy,
                timeout=0.0,
                slot_root=slot_root,
            ):
                pass
        except RuntimeCapacityError:
            duplicate_runtime_rejected = True
        process.kill()
        process.wait(timeout=10.0)
        release_deadline = time.monotonic() + 5.0
        while True:
            try:
                with _ProcessFileLock(lock_path):
                    post_crash_supervisor_reacquired = True
                break
            except SupervisorAlreadyRunningError:
                if time.monotonic() >= release_deadline:
                    raise SoakError(
                        "supervisor lock was not released after helper termination"
                    ) from None
                time.sleep(0.05)
        with provider_runtime_slot(
            policy=policy,
            timeout=1.0,
            slot_root=slot_root,
        ):
            post_crash_runtime_reacquired = True
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10.0)
    passed = all(
        (
            duplicate_supervisor_rejected,
            duplicate_runtime_rejected,
            post_crash_supervisor_reacquired,
            post_crash_runtime_reacquired,
        )
    )
    return {
        "duplicate_runtime_rejected": duplicate_runtime_rejected,
        "duplicate_supervisor_rejected": duplicate_supervisor_rejected,
        "helper_exit_was_forced": process.returncode is not None,
        "passed": passed,
        "post_crash_runtime_reacquired": post_crash_runtime_reacquired,
        "post_crash_supervisor_reacquired": post_crash_supervisor_reacquired,
    }


def _hold_crash_locks(lock_path: Path, slot_root: Path, ready_path: Path) -> int:
    policy = ResourcePolicy(
        min_available_bytes=1,
        min_available_fraction=0.0,
        max_concurrent_runtimes=1,
    )
    with _ProcessFileLock(lock_path):
        with provider_runtime_slot(
            policy=policy,
            timeout=0.0,
            slot_root=slot_root,
        ):
            ready_path.parent.mkdir(parents=True, exist_ok=True)
            ready_path.write_text("ready\n", encoding="ascii")
            while True:
                time.sleep(60.0)


def build_soak_receipt(
    project_root: Path,
    *,
    message_count: int = DEFAULT_MESSAGE_COUNT,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    sample_rounds: int = DEFAULT_SAMPLE_ROUNDS,
    max_plateau_growth_mib: float = DEFAULT_MAX_PLATEAU_GROWTH_MIB,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    if not project_root.is_dir():
        raise SoakError(f"project root does not exist: {project_root}")
    if message_count < 100:
        raise SoakError("message_count must be at least 100")
    if not 5 <= page_limit <= 500:
        raise SoakError("page_limit must be between 5 and 500")
    if sample_rounds < 8:
        raise SoakError("sample_rounds must be at least 8")
    max_growth_bytes = int(max_plateau_growth_mib * MIB)
    if max_growth_bytes <= 0:
        raise SoakError("max_plateau_growth_mib must be positive")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="peerbridge-local-alpha-soak-") as raw:
        temp_root = Path(raw).resolve()
        db_path = temp_root / ".peerbridge" / "peerbridge.sqlite3"
        scope = "local-alpha-soak"
        bridge = Bridge(
            temp_root,
            db_path,
            "human-operator",
            scope,
            session_id="soak-human",
        )
        bridge.create_room({"room_id": "soak-room", "name": "Soak Room"})
        bridge.set_room_automation({"room_id": "soak-room", "mode": "off"})
        reader = BridgeReader(db_path)
        growth_samples: list[dict[str, int]] = []
        try:
            batch_size = max(1, min(100, message_count // 10))
            for index in range(message_count):
                bridge.post_room_message(
                    {
                        "room_id": "soak-room",
                        "task_id": "local-alpha-soak",
                        "subject": f"Synthetic history {index:05d}",
                        "body": "Bounded synthetic room history for local Alpha memory QA.",
                    }
                )
                if (index + 1) % batch_size == 0 or index + 1 == message_count:
                    snapshot = reader.snapshot(limit=page_limit, scope=scope)
                    if len(snapshot.messages) > page_limit:
                        raise SoakError("room snapshot exceeded its configured page limit")
                    gc.collect()
                    growth_samples.append(_memory_reading())

            warm_snapshot = reader.snapshot(limit=page_limit, scope=scope)
            plateau_samples: list[dict[str, int]] = []
            for _ in range(sample_rounds):
                current = reader.snapshot(limit=page_limit, scope=scope)
                if current.signature() != warm_snapshot.signature():
                    raise SoakError("read-only snapshot signature drifted during the soak")
                gc.collect()
                plateau_samples.append(_memory_reading())
            metric_name, metric_values = _plateau_metric(plateau_samples)
            tail_size = max(4, sample_rounds // 3)
            tail = metric_values[-tail_size:]
            plateau_growth = max(tail) - min(tail)
            plateau_passed = plateau_growth <= max_growth_bytes
            if not plateau_passed:
                raise SoakError("resident memory did not reach the configured plateau")

            audit = bridge.verify_audit_chain()
            reader.close()
            reader = BridgeReader(db_path)
            restarted = reader.snapshot(limit=page_limit, scope=scope)
            restart_preserved = (
                int(restarted.table_counts["messages"]) == message_count
                and len(restarted.messages) == min(message_count, page_limit)
                and bool(audit.get("valid"))
            )
            if not restart_preserved:
                raise SoakError("database restart did not preserve bounded history")
            crash_probe = _crash_release_probe(temp_root)
            if not crash_probe["passed"]:
                raise SoakError("process crash did not release singleton/runtime locks")

            receipt: dict[str, Any] = {
                "schema": RECEIPT_SCHEMA,
                "generated_at_utc": utc_now(),
                "claims": {
                    "audit_chain_valid": bool(audit.get("valid")),
                    "bounded_history_paging": True,
                    "no_credentials_or_provider_requests_used": True,
                    "crash_released_runtime_slot": bool(
                        crash_probe["post_crash_runtime_reacquired"]
                    ),
                    "crash_released_supervisor_lock": bool(
                        crash_probe["post_crash_supervisor_reacquired"]
                    ),
                    "database_restart_preserved_history": restart_preserved,
                    "memory_plateau_passed": plateau_passed,
                },
                "parameters": {
                    "max_plateau_growth_bytes": max_growth_bytes,
                    "message_count": message_count,
                    "page_limit": page_limit,
                    "sample_rounds": sample_rounds,
                },
                "results": {
                    "audit_event_count": int(audit.get("event_count") or 0),
                    "crash_probe": crash_probe,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "growth_sample_count": len(growth_samples),
                    "message_count": int(restarted.table_counts["messages"]),
                    "page_rows_returned": len(restarted.messages),
                    "plateau": {
                        "growth_bytes": plateau_growth,
                        "metric": metric_name,
                        "sample_count": len(plateau_samples),
                        "tail_sample_count": tail_size,
                    },
                },
                "source_bindings": [
                    _source_binding(project_root, path) for path in SOURCE_BINDINGS
                ],
            }
            receipt["receipt_sha256"] = stable_sha256(receipt)
            return receipt
        finally:
            reader.close()
            # sqlite3.Connection context managers commit/rollback but do not
            # close.  Bridge operations intentionally keep no connection, so
            # force finalizers before Windows removes the isolated soak tree.
            bridge = None
            reader = None
            gc.collect()


def write_receipt(path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    path = Path(path).resolve()
    if path.exists():
        raise SoakError(f"receipt already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)
    return {
        "bytes": path.stat().st_size,
        "path": str(path),
        "receipt_sha256": receipt["receipt_sha256"],
        "status": "PASS",
    }


def verify_receipt(path: Path, project_root: Path) -> dict[str, Any]:
    path = Path(path).resolve()
    project_root = Path(project_root).resolve()
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SoakError(f"receipt is unreadable: {exc}") from exc
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise SoakError("unsupported soak receipt schema")
    expected = str(receipt.get("receipt_sha256") or "")
    body = dict(receipt)
    body.pop("receipt_sha256", None)
    if expected != stable_sha256(body):
        raise SoakError("soak receipt SHA-256 mismatch")
    claims = receipt.get("claims")
    if not isinstance(claims, dict) or not all(bool(value) for value in claims.values()):
        raise SoakError("one or more local Alpha soak claims did not pass")
    bindings = receipt.get("source_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise SoakError("source bindings are absent")
    for row in bindings:
        if not isinstance(row, dict):
            raise SoakError("source binding is malformed")
        live = _source_binding(project_root, str(row.get("path") or ""))
        if live != row:
            raise SoakError(f"source binding drifted: {row.get('path')}")
    return {
        "bytes": path.stat().st_size,
        "path": str(path),
        "receipt_sha256": expected,
        "source_binding_count": len(bindings),
        "status": "PASS",
        "writes_performed": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture or verify a bounded local Alpha memory/crash soak receipt."
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--messages", type=int, default=DEFAULT_MESSAGE_COUNT)
    parser.add_argument("--page-limit", type=int, default=DEFAULT_PAGE_LIMIT)
    parser.add_argument("--sample-rounds", type=int, default=DEFAULT_SAMPLE_ROUNDS)
    parser.add_argument(
        "--max-plateau-growth-mib",
        type=float,
        default=DEFAULT_MAX_PLATEAU_GROWTH_MIB,
    )
    parser.add_argument("--hold-crash-locks", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--lock-path", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--slot-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ready-path", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.hold_crash_locks:
            if not args.lock_path or not args.slot_root or not args.ready_path:
                raise SoakError("crash helper paths are required")
            return _hold_crash_locks(
                args.lock_path.resolve(),
                args.slot_root.resolve(),
                args.ready_path.resolve(),
            )
        if args.verify:
            result = verify_receipt(args.verify, args.project_root)
        else:
            if not args.output:
                raise SoakError("--output is required when capturing a receipt")
            receipt = build_soak_receipt(
                args.project_root,
                message_count=args.messages,
                page_limit=args.page_limit,
                sample_rounds=args.sample_rounds,
                max_plateau_growth_mib=args.max_plateau_growth_mib,
            )
            result = write_receipt(args.output, receipt)
    except SoakError as exc:
        print(json.dumps({"error": str(exc), "status": "FAIL"}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
