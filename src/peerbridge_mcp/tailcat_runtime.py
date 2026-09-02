"""Managed, default-on Tailcat runtime for the local PeerBridge workbench.

The browser surface never starts an unbounded listener directly.  This module
pins one official Windows release, provisions project-local identities, and
owns the Tailcat process tree for the lifetime of the local control room.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .process_control import (
    attach_process_tree,
    process_group_popen_kwargs,
    terminate_process_tree,
)


TAILCAT_VERSION = "v0.4.0"
TAILCAT_ARCHIVE_NAME = "tailcat_0.4.0_windows_amd64.zip"
TAILCAT_ARCHIVE_URL = (
    "https://github.com/tailscale/tailcat/releases/download/"
    f"{TAILCAT_VERSION}/{TAILCAT_ARCHIVE_NAME}"
)
TAILCAT_ARCHIVE_SHA256 = (
    "c238a4e8d3b460423a67e5ad400888b73ffa0b28e15173fd32c9acb699a3a89e"
)
TAILCAT_EXE_SHA256 = (
    "bcb0c6c91e126ee9a5880e45fe067484a1bc056d721447d5fae8575ab6e672bc"
)
TAILCAT_SETTINGS_SCHEMA = "peerbridge.tailcat-settings.v1"
TAILCAT_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
TAILCAT_MAX_EXPANDED_BYTES = 32 * 1024 * 1024
TAILCAT_DEFAULT_PORT = 8765
TAILCAT_DEFAULT_SSH_PORT = 22
TAILCAT_SERVICES = ("port", "ssh", "exit_node")

_NODE_KEY = re.compile(r"nodekey:[0-9a-f]{64}\Z")
_ADDRESS = re.compile(r"tc[A-Za-z0-9_-]{40,8192}\Z")


class TailcatRuntimeError(ValueError):
    """A managed Tailcat operation failed its local trust contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _settings_payload(*, enabled: bool, port: int) -> dict[str, Any]:
    return {
        "schema": TAILCAT_SETTINGS_SCHEMA,
        "enabled": bool(enabled),
        "auto_start": True,
        "port": int(port),
        "ssh_port": TAILCAT_DEFAULT_SSH_PORT,
        "services": list(TAILCAT_SERVICES),
    }


def _read_settings(path: Path) -> dict[str, Any]:
    default = _settings_payload(enabled=True, port=TAILCAT_DEFAULT_PORT)
    if not path.is_file() or _is_reparse(path):
        return default
    try:
        if path.stat().st_size > 16 * 1024:
            return default
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default
    if not isinstance(value, dict) or value.get("schema") != TAILCAT_SETTINGS_SCHEMA:
        return default
    try:
        port = int(value.get("port") or TAILCAT_DEFAULT_PORT)
    except (TypeError, ValueError):
        port = TAILCAT_DEFAULT_PORT
    if not 1 <= port <= 65535:
        port = TAILCAT_DEFAULT_PORT
    return _settings_payload(enabled=value.get("enabled") is not False, port=port)


def _write_settings(path: Path, *, enabled: bool, port: int) -> None:
    encoded = (
        json.dumps(
            _settings_payload(enabled=enabled, port=port),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write(path, encoded)


def _download_official_archive() -> bytes:
    request = urllib.request.Request(
        TAILCAT_ARCHIVE_URL,
        headers={"User-Agent": "PeerBridge-Tailcat-Installer/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        length = response.headers.get("Content-Length")
        if length and int(length) > TAILCAT_MAX_ARCHIVE_BYTES:
            raise TailcatRuntimeError("official Tailcat archive exceeds the size limit")
        payload = response.read(TAILCAT_MAX_ARCHIVE_BYTES + 1)
    if len(payload) > TAILCAT_MAX_ARCHIVE_BYTES:
        raise TailcatRuntimeError("official Tailcat archive exceeds the size limit")
    if hashlib.sha256(payload).hexdigest() != TAILCAT_ARCHIVE_SHA256:
        raise TailcatRuntimeError("official Tailcat archive SHA-256 mismatch")
    return payload


def _install_archive(payload: bytes, destination: Path) -> Path:
    if hashlib.sha256(payload).hexdigest() != TAILCAT_ARCHIVE_SHA256:
        raise TailcatRuntimeError("official Tailcat archive SHA-256 mismatch")
    required = {"tailcat.exe", "LICENSE", "README.md"}
    extracted: dict[str, bytes] = {}
    expanded_bytes = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                normalized = member.filename.replace("\\", "/")
                if (
                    member.is_dir()
                    or normalized.startswith("/")
                    or normalized != Path(normalized).name
                    or normalized not in required
                    or stat.S_IFMT(member.external_attr >> 16) == stat.S_IFLNK
                ):
                    raise TailcatRuntimeError("official Tailcat archive layout is unsafe")
                expanded_bytes += int(member.file_size)
                if expanded_bytes > TAILCAT_MAX_EXPANDED_BYTES:
                    raise TailcatRuntimeError("official Tailcat archive expands past the limit")
                extracted[normalized] = archive.read(member)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise TailcatRuntimeError("official Tailcat archive is unreadable") from exc
    if set(extracted) != required:
        raise TailcatRuntimeError("official Tailcat archive is incomplete")
    if hashlib.sha256(extracted["tailcat.exe"]).hexdigest() != TAILCAT_EXE_SHA256:
        raise TailcatRuntimeError("official Tailcat executable SHA-256 mismatch")
    if _is_reparse(destination):
        raise TailcatRuntimeError("Tailcat install directory must not be a reparse point")
    destination.mkdir(parents=True, exist_ok=True)
    for name in sorted(required):
        target = destination / name
        if _is_reparse(target):
            raise TailcatRuntimeError("Tailcat install target must not be a reparse point")
        _atomic_write(target, extracted[name])
    executable = destination / "tailcat.exe"
    if _sha256(executable) != TAILCAT_EXE_SHA256:
        raise TailcatRuntimeError("installed Tailcat executable failed verification")
    return executable


def _private_identity() -> str:
    domain = str(os.environ.get("USERDOMAIN") or "").strip()
    username = str(os.environ.get("USERNAME") or "").strip()
    if not username or not re.fullmatch(r"[^\r\n:]{1,256}", username):
        raise TailcatRuntimeError("current Windows identity is unavailable")
    return f"{domain}\\{username}" if domain else username


def _protect_private_path(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return
    identity = _private_identity()
    rights = "(OI)(CI)F" if path.is_dir() else "F"
    completed = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:{rights}",
            "*S-1-5-18:F",
        ],
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )
    if completed.returncode != 0:
        raise TailcatRuntimeError("Tailcat private state ACL could not be applied")


def _read_small_text(path: Path, *, limit: int = 16 * 1024) -> str:
    if not path.is_file() or _is_reparse(path) or path.stat().st_size > limit:
        raise TailcatRuntimeError("Tailcat runtime output is unavailable")
    return path.read_text(encoding="utf-8").strip()


def _process_start_ticks(process_id: int) -> int | None:
    if sys.platform != "win32":
        try:
            os.kill(int(process_id), 0)
        except (OSError, ValueError):
            return None
        return 1
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, int(process_id))
    if not handle:
        return None
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


class TailcatRuntimeManager:
    """Own one allow-listed Tailcat server and its default-on preference."""

    def __init__(
        self,
        project_root: Path,
        *,
        auto_bootstrap: bool = False,
        launcher_path: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.state_root = self.project_root / ".peerbridge" / "tailcat"
        self.settings_path = self.state_root / "settings.json"
        self.install_root = self.state_root / "bin" / TAILCAT_VERSION
        self.executable_path = self.install_root / "tailcat.exe"
        self.keys_root = self.state_root / "keys"
        self.pairing_root = self.state_root / "pairing"
        self.client_key_path = self.pairing_root / "owner-client.private.json"
        self.server_key_path = self.keys_root / "peerbridge-server.private.json"
        self.address_path = self.state_root / "runtime" / "server-address.txt"
        self.receipt_path = self.state_root / "runtime" / "managed-server.json"
        self.launcher_path = (
            launcher_path or self.project_root / "scripts" / "launch_tailcat_remote.ps1"
        ).resolve()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._owns_runtime = False
        self._phase = "disabled" if not self._settings()["enabled"] else "waiting"
        self._error_code = ""
        self._client_public_key = ""
        self._verified_binary_signature: tuple[int, int, int] | None = None
        self._closed = False
        self._auto_bootstrap = bool(auto_bootstrap)
        if self._auto_bootstrap:
            self.start_if_enabled()

    def _settings(self) -> dict[str, Any]:
        return _read_settings(self.settings_path)

    def _verified_executable(self, *, force: bool = False) -> Path | None:
        try:
            if not self.executable_path.is_file() or _is_reparse(self.executable_path):
                return None
            metadata = self.executable_path.stat()
            signature = (int(metadata.st_size), int(metadata.st_mtime_ns), int(metadata.st_ctime_ns))
            if not force and self._verified_binary_signature == signature:
                return self.executable_path
            if _sha256(self.executable_path) == TAILCAT_EXE_SHA256:
                self._verified_binary_signature = signature
                return self.executable_path
        except OSError:
            return None
        self._verified_binary_signature = None
        return None

    def _set_phase(self, phase: str, error_code: str = "") -> None:
        with self._lock:
            self._phase = phase
            self._error_code = error_code

    def _ensure_install(self) -> Path:
        executable = self._verified_executable(force=True)
        if executable is not None:
            return executable
        payload = _download_official_archive()
        return _install_archive(payload, self.install_root)

    def _run_key_command(self, executable: Path, arguments: list[str]) -> str:
        completed = subprocess.run(
            [str(executable), *arguments],
            cwd=self.state_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            check=False,
        )
        if completed.returncode != 0:
            raise TailcatRuntimeError("Tailcat identity provisioning failed")
        return completed.stdout.strip()

    def _ensure_identities(self, executable: Path) -> str:
        for directory in (self.state_root, self.keys_root, self.pairing_root):
            if _is_reparse(directory):
                raise TailcatRuntimeError("Tailcat private state must not cross a reparse point")
            directory.mkdir(parents=True, exist_ok=True)
            _protect_private_path(directory)
        if not self.client_key_path.is_file():
            public_key = self._run_key_command(
                executable,
                ["genkey", "--client", f"--key={self.client_key_path}"],
            )
        else:
            if _is_reparse(self.client_key_path):
                raise TailcatRuntimeError("Tailcat client key must not be a reparse point")
            public_key = self._run_key_command(
                executable,
                [f"--key={self.client_key_path}", "printpub"],
            )
        _protect_private_path(self.client_key_path)
        if not _NODE_KEY.fullmatch(public_key):
            raise TailcatRuntimeError("Tailcat client public key is invalid")
        if not self.server_key_path.is_file():
            self._run_key_command(
                executable,
                [
                    "genkey",
                    f"--key={self.server_key_path}",
                    "--fixed-region",
                ],
            )
        elif _is_reparse(self.server_key_path):
            raise TailcatRuntimeError("Tailcat server key must not be a reparse point")
        _protect_private_path(self.server_key_path)
        return public_key

    def _launch(self, executable: Path, public_key: str, port: int) -> None:
        if os.name != "nt":
            raise TailcatRuntimeError("managed Tailcat auto-start currently requires Windows")
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if (
            not powershell.is_file()
            or not self.launcher_path.is_file()
            or _is_reparse(self.launcher_path)
        ):
            raise TailcatRuntimeError("trusted Tailcat launcher is unavailable")
        runtime_root = self.address_path.parent
        runtime_root.mkdir(parents=True, exist_ok=True)
        _protect_private_path(runtime_root)
        with contextlib.suppress(FileNotFoundError):
            self.address_path.unlink()
        command = [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.launcher_path),
            "-Mode",
            "ManagedServer",
            "-TailcatPath",
            str(executable),
            "-ExpectedSha256",
            TAILCAT_EXE_SHA256,
            "-Port",
            str(port),
            "-SshPort",
            str(TAILCAT_DEFAULT_SSH_PORT),
            "-AllowClientKey",
            public_key,
            "-ServerKeyFile",
            str(self.server_key_path),
            "-EnableExitNode",
        ]
        environment = dict(os.environ)
        environment["TAILCAT_ADDR_FILE"] = str(self.address_path)
        process = subprocess.Popen(
            command,
            cwd=self.project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            **process_group_popen_kwargs(),
        )
        attach_process_tree(process)
        with self._lock:
            if self._closed or not self._settings()["enabled"]:
                terminate_process_tree(process, wait_seconds=3)
                raise TailcatRuntimeError("Tailcat start was cancelled")
            self._process = process
            self._owns_runtime = True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise TailcatRuntimeError("Tailcat server exited during startup")
            try:
                address = _read_small_text(self.address_path)
            except (OSError, UnicodeError, TailcatRuntimeError):
                time.sleep(0.2)
                continue
            if _ADDRESS.fullmatch(address):
                start_ticks = _process_start_ticks(process.pid)
                if start_ticks is None:
                    raise TailcatRuntimeError("Tailcat process identity is unavailable")
                _atomic_write(
                    self.receipt_path,
                    (
                        json.dumps(
                            {
                                "schema": "peerbridge.tailcat-runtime.v1",
                                "pid": process.pid,
                                "start_time_utc_ticks": start_ticks,
                                "version": TAILCAT_VERSION,
                                "executable_sha256": TAILCAT_EXE_SHA256,
                                "port": port,
                                "ssh_port": TAILCAT_DEFAULT_SSH_PORT,
                                "services": list(TAILCAT_SERVICES),
                            },
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                _protect_private_path(self.receipt_path)
                self._write_pairing_guide(address, port)
                return
            raise TailcatRuntimeError("Tailcat server returned an invalid address")
        raise TailcatRuntimeError("Tailcat server startup timed out")

    def _write_pairing_guide(self, address: str, port: int) -> None:
        guide = (
            "PeerBridge Tailcat device pairing\r\n"
            "\r\n"
            "Keep owner-client.private.json private. Copy this folder only to a device "
            "you control.\r\n\r\n"
            f"PeerBridge port through the exit-node SOCKS path:\r\n"
            f"tailcat --key=owner-client.private.json socks {address} "
            f"curl http://127.0.0.1:{port}/healthz\r\n\r\n"
            f"SSH:\r\ntailcat --key=owner-client.private.json ssh {address}\r\n\r\n"
            f"SOCKS5 / exit node:\r\ntailcat --key=owner-client.private.json socks {address}\r\n"
        ).encode("utf-8")
        path = self.pairing_root / "CONNECT.txt"
        _atomic_write(path, guide)
        _protect_private_path(path)

    def _bootstrap(self) -> None:
        process: subprocess.Popen[bytes] | None = None
        try:
            self._set_phase("installing")
            executable = self._ensure_install()
            if not self._settings()["enabled"]:
                self._set_phase("disabled")
                return
            self._set_phase("provisioning")
            public_key = self._ensure_identities(executable)
            with self._lock:
                self._client_public_key = public_key
            if not self._settings()["enabled"]:
                self._set_phase("disabled")
                return
            self._set_phase("starting")
            self._launch(executable, public_key, int(self._settings()["port"]))
            with self._lock:
                process = self._process
            if process is None or process.poll() is not None:
                raise TailcatRuntimeError("Tailcat server did not stay running")
            self._set_phase("running")
        except Exception as exc:
            with self._lock:
                process = self._process
                self._process = None
                owned = self._owns_runtime
                self._owns_runtime = False
            if process is not None:
                terminate_process_tree(process, wait_seconds=3)
            if owned:
                for path in (self.receipt_path, self.address_path):
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
            if isinstance(exc, TailcatRuntimeError):
                code = re.sub(r"[^a-z0-9]+", "_", str(exc).lower()).strip("_")[:120]
            else:
                code = "unexpected_runtime_failure"
            self._set_phase("failed", code or "tailcat_runtime_failed")

    def start_if_enabled(self) -> None:
        with self._lock:
            if self._closed or not self._settings()["enabled"]:
                self._phase = "disabled"
                return
            if self._process is not None and self._process.poll() is None:
                return
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._bootstrap,
                name="peerbridge-tailcat-bootstrap",
                daemon=True,
            )
            self._worker.start()

    def stop(self, *, disabled: bool) -> None:
        with self._lock:
            process = self._process
            owned = self._owns_runtime
            self._process = None
            self._owns_runtime = False
            self._phase = "disabled" if disabled else "stopped"
            self._error_code = ""
        if process is not None:
            terminate_process_tree(process, wait_seconds=5)
        if owned:
            for path in (self.receipt_path, self.address_path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        settings = self._settings()
        _write_settings(
            self.settings_path,
            enabled=bool(enabled),
            port=int(settings["port"]),
        )
        if enabled:
            self.start_if_enabled()
        else:
            self.stop(disabled=True)
        return self.status()

    def set_forward_port(self, port: int) -> None:
        port = int(port)
        if not 1 <= port <= 65535:
            raise TailcatRuntimeError("invalid Tailcat forwarding port")
        settings = self._settings()
        if int(settings["port"]) == port:
            return
        _write_settings(self.settings_path, enabled=bool(settings["enabled"]), port=port)
        if settings["enabled"]:
            self.stop(disabled=False)
            self.start_if_enabled()

    def restart(self) -> dict[str, Any]:
        if not self._settings()["enabled"]:
            return self.status()
        self.stop(disabled=False)
        self.start_if_enabled()
        return self.status()

    def connection_address(self) -> str:
        address = _read_small_text(self.address_path)
        if not _ADDRESS.fullmatch(address):
            raise TailcatRuntimeError("Tailcat connection address is invalid")
        return address

    def open_pairing_folder(self) -> dict[str, Any]:
        if os.name != "nt" or not self.pairing_root.is_dir() or _is_reparse(self.pairing_root):
            raise TailcatRuntimeError("Tailcat pairing folder is unavailable")
        os.startfile(str(self.pairing_root))  # type: ignore[attr-defined]
        return {"status": "opened"}

    def status(self) -> dict[str, Any]:
        settings = self._settings()
        with self._lock:
            process = self._process
            phase = self._phase
            error_code = self._error_code
            public_key = self._client_public_key
        owned_process_alive = bool(process is not None and process.poll() is None)
        running = bool(owned_process_alive and phase == "running")
        process_id = process.pid if owned_process_alive and process is not None else None
        if not owned_process_alive:
            receipt: dict[str, Any] = {}
            try:
                if (
                    self.receipt_path.is_file()
                    and not _is_reparse(self.receipt_path)
                    and self.receipt_path.stat().st_size <= 16 * 1024
                ):
                    candidate = json.loads(self.receipt_path.read_text(encoding="utf-8"))
                    if isinstance(candidate, dict):
                        receipt = candidate
            except (OSError, UnicodeError, json.JSONDecodeError):
                receipt = {}
            try:
                candidate_pid = int(receipt.get("pid") or 0)
                candidate_ticks = int(receipt.get("start_time_utc_ticks") or 0)
            except (TypeError, ValueError):
                candidate_pid, candidate_ticks = 0, 0
            observed_ticks = _process_start_ticks(candidate_pid) if candidate_pid > 0 else None
            running = bool(
                receipt.get("schema") == "peerbridge.tailcat-runtime.v1"
                and receipt.get("version") == TAILCAT_VERSION
                and receipt.get("executable_sha256") == TAILCAT_EXE_SHA256
                and candidate_ticks > 0
                and observed_ticks == candidate_ticks
            )
            process_id = candidate_pid if running else None
            if running:
                phase = "running"
                error_code = ""
        if process is not None and not owned_process_alive and phase == "running":
            phase = "failed"
            error_code = "tailcat_server_exited"
        address_ready = False
        with contextlib.suppress(OSError, UnicodeError, TailcatRuntimeError):
            address_ready = bool(_ADDRESS.fullmatch(_read_small_text(self.address_path)))
        client_key_ready = False
        try:
            client_key_ready = bool(
                self.client_key_path.is_file()
                and not _is_reparse(self.client_key_path)
                and 64 <= self.client_key_path.stat().st_size <= 16 * 1024
            )
        except OSError:
            client_key_ready = False
        return {
            "enabled": bool(settings["enabled"]),
            "auto_start": True,
            "phase": phase,
            "running": running,
            "process_id": process_id,
            "error_code": error_code,
            "version": TAILCAT_VERSION,
            "binary_installed": self._verified_executable() is not None,
            "launcher_available": self.launcher_path.is_file()
            and not _is_reparse(self.launcher_path),
            "client_identity_ready": bool(_NODE_KEY.fullmatch(public_key))
            or client_key_ready,
            "address_ready": address_ready,
            "pairing_folder_ready": (
                self.client_key_path.is_file()
                and (self.pairing_root / "CONNECT.txt").is_file()
            ),
            "port": int(settings["port"]),
            "ssh_port": int(settings["ssh_port"]),
            "services": list(TAILCAT_SERVICES),
            "binary_managed_by_peerbridge": True,
            "browser_transport_supported": False,
        }

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop(disabled=not self._settings()["enabled"])
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)


__all__ = [
    "TAILCAT_ARCHIVE_SHA256",
    "TAILCAT_EXE_SHA256",
    "TAILCAT_VERSION",
    "TailcatRuntimeError",
    "TailcatRuntimeManager",
]
