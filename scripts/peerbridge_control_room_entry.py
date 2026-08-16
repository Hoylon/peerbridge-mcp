from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from peerbridge_mcp import __version__
from peerbridge_mcp.cli import main as cli_main
from peerbridge_mcp.feedback import run_feedback_encryption_self_test
from peerbridge_mcp.mailbox_supervisor import MailboxSupervisor
from peerbridge_mcp.monitor import main as monitor_main


STARTUP_TIMEOUT_SECONDS = 15.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
CONTRACT_TIMEOUT_SECONDS = 30.0
STILL_ACTIVE = 259
_FROZEN_NULL_STREAMS: list[Any] = []
SELF_TEST_RECEIPT_SCHEMA = "peerbridge-packaged-self-test-v1"


def _ensure_frozen_standard_streams() -> None:
    """Give windowed frozen CLI modes safe sinks instead of ``None`` streams."""
    if not getattr(sys, "frozen", False):
        return
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is not None:
            continue
        stream = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, name, stream)
        _FROZEN_NULL_STREAMS.append(stream)


def _portable_workspace() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return (base / "PeerBridge" / "workspace").resolve()


def _runtime_path() -> Path:
    if sys.platform == "win32":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleFileNameW.argtypes = (
            wintypes.HMODULE,
            wintypes.LPWSTR,
            wintypes.DWORD,
        )
        kernel32.GetModuleFileNameW.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetModuleFileNameW(None, buffer, len(buffer))
        if length == 0 or length >= len(buffer):
            raise OSError(ctypes.get_last_error(), "unable to resolve runtime executable")
        return Path(buffer.value).resolve()
    return Path(sys.executable).resolve()


def _runtime_kind() -> str:
    return "frozen" if getattr(sys, "frozen", False) else "source"


def _runtime_sha256(path: Path | None = None) -> str:
    digest = hashlib.sha256()
    with (path or _runtime_path()).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(_runtime_path())]
    return [str(_runtime_path()), str(Path(__file__).resolve())]


def _write_json_create_only(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _safe_exception_chain(exc: BaseException) -> list[dict[str, str]]:
    """Return bounded diagnostics without tracebacks or machine-specific paths."""
    replacements = {
        str(Path.home()): "<home>",
        str(Path.cwd()): "<cwd>",
        str(_runtime_path().parent): "<runtime>",
    }
    chain: list[dict[str, str]] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 6:
        seen.add(id(current))
        message = " ".join(str(current).split())
        for value, replacement in replacements.items():
            if value:
                message = message.replace(value, replacement)
                message = message.replace(value.replace("\\", "/"), replacement)
        chain.append({"type": type(current).__name__[:120], "message": message[:500]})
        current = current.__cause__ or current.__context__
    return chain


def _run_feedback_encryption_self_test() -> int:
    receipt_value = os.environ.get("PEERBRIDGE_SELF_TEST_RECEIPT_PATH", "").strip()
    receipt_path = Path(receipt_value).resolve() if receipt_value else None
    receipt: dict[str, Any] = {
        "schema": SELF_TEST_RECEIPT_SCHEMA,
        "test": "feedback-encryption",
        "runtime_kind": _runtime_kind(),
        "runtime_sha256": _runtime_sha256(),
        "version": __version__,
    }
    try:
        result = run_feedback_encryption_self_test()
    except Exception as exc:
        receipt.update({"status": "FAIL", "error_chain": _safe_exception_chain(exc)})
        if receipt_path is not None:
            _write_json_create_only(receipt_path, receipt)
        print(json.dumps(receipt, sort_keys=True), file=sys.stderr)
        return 1
    receipt.update({"status": "PASS", "result": result})
    if receipt_path is not None:
        _write_json_create_only(receipt_path, receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _terminate_owned_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)


def _wait_for_supervisor_ready(
    process: subprocess.Popen[Any],
    ready_path: Path,
    *,
    nonce: str,
    expected_parent_pid: int,
) -> dict[str, Any]:
    expected_runtime = str(_runtime_path())
    expected_hash = _runtime_sha256()
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    parse_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"supervisor exited before health handshake (code {process.returncode})"
            )
        if ready_path.is_file():
            try:
                payload = json.loads(ready_path.read_text(encoding="utf-8"))
                if payload.get("schema") != "peerbridge-launch-health-v1":
                    raise RuntimeError("supervisor handshake schema is invalid")
                if payload.get("status") != "ready":
                    raise RuntimeError("supervisor handshake is not healthy")
                if payload.get("nonce") != nonce:
                    raise RuntimeError("supervisor handshake nonce does not match")
                if payload.get("parent_pid") != expected_parent_pid:
                    raise RuntimeError("supervisor handshake parent PID does not match")
                if payload.get("supervisor_pid") != process.pid:
                    raise RuntimeError("supervisor handshake PID does not match")
                if payload.get("runtime_path") != expected_runtime:
                    raise RuntimeError("supervisor runtime differs from launcher runtime")
                if payload.get("runtime_sha256") != expected_hash:
                    raise RuntimeError("supervisor runtime hash differs from launcher runtime")
                if payload.get("runtime_kind") != _runtime_kind():
                    raise RuntimeError("supervisor runtime kind differs from launcher runtime")
                if payload.get("version") != __version__:
                    raise RuntimeError("supervisor version differs from launcher version")
                if not _process_alive(process.pid):
                    raise RuntimeError("supervisor PID is not alive after handshake")
                return payload
            except (OSError, json.JSONDecodeError) as exc:
                parse_error = exc
        time.sleep(0.05)
    detail = f": {parse_error}" if parse_error is not None else ""
    raise RuntimeError(f"supervisor health handshake timed out{detail}")


def _start_managed_supervisor(
    project_root: Path,
    db_path: Path,
    scope: str,
) -> tuple[subprocess.Popen[Any], dict[str, Any]]:
    nonce = secrets.token_hex(16)
    ready_path = (
        project_root
        / ".peerbridge"
        / "launcher-ready"
        / f"supervisor-{os.getpid()}-{nonce}.json"
    )
    command = [
        *_runtime_command(),
        "--managed-supervisor",
        "--project-root",
        str(project_root),
        "--db",
        str(db_path),
        "--scope",
        scope,
        "--ready-path",
        str(ready_path),
        "--nonce",
        nonce,
        "--parent-pid",
        str(os.getpid()),
        "--poll-seconds",
        "5",
        "--max-parallel-dispatches",
        "8",
    ]
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        command,
        cwd=project_root,
        close_fds=True,
        creationflags=creation_flags,
    )
    try:
        payload = _wait_for_supervisor_ready(
            process,
            ready_path,
            nonce=nonce,
            expected_parent_pid=os.getpid(),
        )
    except BaseException:
        _terminate_owned_process(process)
        raise
    return process, payload


def _managed_supervisor(args: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--ready-path", type=Path, required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-parallel-dispatches", type=int, default=8)
    parsed = parser.parse_args(args)

    supervisor = MailboxSupervisor(
        parsed.project_root,
        parsed.db,
        parsed.scope,
        max_parallel_dispatches=parsed.max_parallel_dispatches,
    )
    try:
        _write_json_create_only(
            parsed.ready_path,
            {
                "schema": "peerbridge-launch-health-v1",
                "status": "ready",
                "health": "database-and-supervisor-lock-ready",
                "nonce": parsed.nonce,
                "parent_pid": parsed.parent_pid,
                "supervisor_pid": os.getpid(),
                "runtime_kind": _runtime_kind(),
                "runtime_path": str(_runtime_path()),
                "runtime_sha256": _runtime_sha256(),
                "version": __version__,
            },
        )
        interval = max(0.25, min(float(parsed.poll_seconds), 300.0))
        consecutive_failures = 0
        while _process_alive(parsed.parent_pid):
            try:
                supervisor.run_cycle()
            except Exception:
                consecutive_failures += 1
                delay = min(
                    supervisor.cycle_error_backoff_cap_seconds,
                    supervisor.cycle_error_backoff_base_seconds
                    * (2 ** min(consecutive_failures - 1, 20)),
                )
            else:
                consecutive_failures = 0
                delay = interval
            deadline = time.monotonic() + delay
            while _process_alive(parsed.parent_pid) and time.monotonic() < deadline:
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    finally:
        supervisor.close()
    return 0


def _wait_for_contract_shutdown(contract_path: Path) -> None:
    request_path = contract_path / "shutdown.request"
    deadline = time.monotonic() + CONTRACT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if request_path.is_file():
            return
        time.sleep(0.05)
    raise RuntimeError("startup lifecycle contract timed out waiting for shutdown")


def _launcher_payload(supervisor_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "peerbridge-launch-health-v1",
        "status": "ready",
        "health": supervisor_payload["health"],
        "launcher_pid": os.getpid(),
        "supervisor_pid": supervisor_payload["supervisor_pid"],
        "runtime_kind": _runtime_kind(),
        "runtime_path": str(_runtime_path()),
        "runtime_sha256": supervisor_payload["runtime_sha256"],
        "version": __version__,
    }


def _run_managed_launcher(
    project_root: Path,
    db_path: Path,
    scope: str,
    *,
    launcher_ready_path: Path | None = None,
    contract_path: Path | None = None,
) -> int:
    project_root = project_root.resolve()
    db_path = db_path.resolve()
    project_root.mkdir(parents=True, exist_ok=True)
    init_result = cli_main(["init", "--project-root", str(project_root), "--scope", scope])
    if init_result != 0:
        raise RuntimeError(f"workspace initialization failed (code {init_result})")

    supervisor, supervisor_payload = _start_managed_supervisor(
        project_root, db_path, scope
    )
    try:
        payload = _launcher_payload(supervisor_payload)
        if launcher_ready_path is not None:
            _write_json_create_only(launcher_ready_path.resolve(), payload)
        if contract_path is not None:
            contract_path = contract_path.resolve()
            _write_json_create_only(contract_path / "launcher-ready.json", payload)
            _wait_for_contract_shutdown(contract_path)
            return 0
        return monitor_main(
            [
                "--project-root",
                str(project_root),
                "--db",
                str(db_path),
                "--scope",
                scope,
                "--refresh-ms",
                "1500",
            ]
        )
    finally:
        _terminate_owned_process(supervisor)


def _source_launch(args: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--launcher-ready-path", type=Path, required=True)
    parsed = parser.parse_args(args)
    return _run_managed_launcher(
        parsed.project_root,
        parsed.db,
        parsed.scope,
        launcher_ready_path=parsed.launcher_ready_path,
    )


def _show_startup_error(message: str) -> None:
    rendered = f"PeerBridge Control Room could not start.\n\n{message}"
    print(rendered, file=sys.stderr)
    if sys.platform == "win32" and os.environ.get("PEERBRIDGE_LAUNCHER_HEADLESS") != "1":
        ctypes.windll.user32.MessageBoxW(None, rendered, "PeerBridge startup failed", 0x10)


def dispatch(argv: list[str] | None = None) -> int:
    _ensure_frozen_standard_streams()
    args = list(sys.argv[1:] if argv is None else argv)

    if args[:2] == ["-m", "peerbridge_mcp"]:
        return cli_main(args[2:])

    if args == ["--feedback-encryption-self-test"]:
        return _run_feedback_encryption_self_test()

    if args[:1] == ["--managed-supervisor"]:
        try:
            return _managed_supervisor(args[1:])
        except Exception as exc:
            print(f"managed supervisor startup failed: {exc}", file=sys.stderr)
            return 1

    if args[:1] == ["--source-launch"]:
        try:
            return _source_launch(args[1:])
        except Exception as exc:
            _show_startup_error(str(exc))
            return 1

    if not args and getattr(sys, "frozen", False):
        project_root = _portable_workspace()
        contract_value = os.environ.get("PEERBRIDGE_STARTUP_CONTRACT_PATH")
        contract_path = Path(contract_value) if contract_value else None
        try:
            return _run_managed_launcher(
                project_root,
                project_root / ".peerbridge" / "peerbridge.sqlite3",
                "peerbridge-main",
                contract_path=contract_path,
            )
        except Exception as exc:
            _show_startup_error(str(exc))
            return 1

    return monitor_main(args)


if __name__ == "__main__":
    raise SystemExit(dispatch())
