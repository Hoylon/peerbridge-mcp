from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from peerbridge_mcp import tailcat_runtime
from peerbridge_mcp.tailcat_runtime import (
    TAILCAT_SETTINGS_SCHEMA,
    TailcatRuntimeError,
    TailcatRuntimeManager,
)


def _archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return output.getvalue()


def test_verified_archive_installs_only_the_expected_official_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = b"synthetic-tailcat-executable"
    payload = _archive(
        {
            "tailcat.exe": executable,
            "LICENSE": b"BSD-3-Clause fixture",
            "README.md": b"fixture documentation",
        }
    )
    monkeypatch.setattr(
        tailcat_runtime, "TAILCAT_ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest()
    )
    monkeypatch.setattr(
        tailcat_runtime, "TAILCAT_EXE_SHA256", hashlib.sha256(executable).hexdigest()
    )

    installed = tailcat_runtime._install_archive(payload, tmp_path / "install")

    assert installed.read_bytes() == executable
    assert {path.name for path in installed.parent.iterdir()} == {
        "tailcat.exe",
        "LICENSE",
        "README.md",
    }


def test_archive_rejects_extra_or_nested_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _archive(
        {
            "tailcat.exe": b"exe",
            "LICENSE": b"license",
            "README.md": b"readme",
            "../unexpected.txt": b"no",
        }
    )
    monkeypatch.setattr(
        tailcat_runtime, "TAILCAT_ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest()
    )

    with pytest.raises(TailcatRuntimeError, match="layout is unsafe"):
        tailcat_runtime._install_archive(payload, tmp_path / "install")


def test_tailcat_preference_defaults_on_and_persists_master_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = TailcatRuntimeManager(tmp_path)
    started: list[bool] = []
    monkeypatch.setattr(manager, "start_if_enabled", lambda: started.append(True))

    assert manager.status()["enabled"] is True
    disabled = manager.set_enabled(False)
    assert disabled["enabled"] is False
    saved = json.loads(manager.settings_path.read_text(encoding="utf-8"))
    assert saved == {
        "schema": TAILCAT_SETTINGS_SCHEMA,
        "enabled": False,
        "auto_start": True,
        "port": 8765,
        "ssh_port": 22,
        "services": ["port", "ssh", "exit_node"],
    }

    enabled = manager.set_enabled(True)
    assert enabled["enabled"] is True
    assert started == [True]


def test_managed_launch_owns_one_allowlisted_port_ssh_exit_node_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "launch_tailcat_remote.ps1"
    launcher.write_text("fixture", encoding="utf-8")
    executable = tmp_path / "tailcat.exe"
    executable.write_bytes(b"fixture")
    manager = TailcatRuntimeManager(tmp_path, launcher_path=launcher)
    manager.server_key_path.parent.mkdir(parents=True, exist_ok=True)
    manager.server_key_path.write_text("{}", encoding="utf-8")
    address = "tc" + "A" * 64
    calls: list[list[str]] = []

    class Process:
        pid = 4242

        def poll(self):
            return None

    def popen(command, **_kwargs):
        calls.append(list(command))
        return Process()

    monkeypatch.setattr(tailcat_runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(tailcat_runtime, "attach_process_tree", lambda _process: True)
    monkeypatch.setattr(tailcat_runtime, "terminate_process_tree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tailcat_runtime, "process_group_popen_kwargs", lambda: {})
    monkeypatch.setattr(tailcat_runtime, "_protect_private_path", lambda _path: None)
    monkeypatch.setattr(tailcat_runtime, "_process_start_ticks", lambda _pid: 123456)
    monkeypatch.setattr(tailcat_runtime, "_read_small_text", lambda _path: address)

    manager._launch(
        executable,
        "nodekey:" + "a" * 64,
        9876,
    )

    command = calls[0]
    assert command[command.index("-Mode") + 1] == "ManagedServer"
    assert command[command.index("-Port") + 1] == "9876"
    assert command[command.index("-SshPort") + 1] == "22"
    assert command[command.index("-AllowClientKey") + 1] == "nodekey:" + "a" * 64
    assert "-EnableExitNode" in command
    receipt = json.loads(manager.receipt_path.read_text(encoding="utf-8"))
    assert receipt["services"] == ["port", "ssh", "exit_node"]
    assert receipt["pid"] == 4242
    assert (manager.pairing_root / "CONNECT.txt").is_file()
    manager.stop(disabled=False)


def test_status_can_observe_the_owned_runtime_from_remote_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = TailcatRuntimeManager(tmp_path)
    manager.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    manager.receipt_path.write_text(
        json.dumps(
            {
                "schema": "peerbridge.tailcat-runtime.v1",
                "pid": 7788,
                "start_time_utc_ticks": 9911,
                "version": tailcat_runtime.TAILCAT_VERSION,
                "executable_sha256": tailcat_runtime.TAILCAT_EXE_SHA256,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tailcat_runtime, "_process_start_ticks", lambda pid: 9911 if pid == 7788 else None)
    monkeypatch.setattr(manager, "_verified_executable", lambda: Path("tailcat.exe"))

    status = manager.status()

    assert status["running"] is True
    assert status["phase"] == "running"
    assert status["process_id"] == 7788
