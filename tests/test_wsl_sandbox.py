from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import peerbridge_mcp.wsl_sandbox as sandbox_module
from peerbridge_mcp.wsl_sandbox import (
    WSL_SANDBOX_BOUNDARY,
    WslSandboxStatus,
    build_wsl_sandbox_command,
    probe_wsl_sandbox,
)


def test_probe_accepts_only_explicit_wsl2_bubblewrap_evidence(tmp_path: Path) -> None:
    system_root = tmp_path / "Windows"
    wsl = system_root / "System32" / "wsl.exe"
    wsl.parent.mkdir(parents=True)
    wsl.write_bytes(b"stub")

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[0] == str(wsl.resolve())
        assert "-u" in command
        assert "peerbridge" in command
        assert kwargs["timeout"] == 15
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "PB_SANDBOX_PASS\n"
                "NODE=v22.22.0\n"
                "ACPX=0.13.0\n"
                "AGENT=claude-code:1\n"
                "AGENT=grok:1\n"
                "AGENT=kimi-code:1\n"
            ),
            stderr="",
        )

    status = sandbox_module._probe(
        distribution="Ubuntu-24.04",
        user="peerbridge",
        runner=runner,
        source={"SYSTEMROOT": str(system_root)},
    )

    assert status.sandbox_verified is True
    assert status.node_version == "v22.22.0"
    assert status.acpx_version == "0.13.0"
    assert all(status.agent_available.values())


def test_probe_fails_closed_without_proof_marker(tmp_path: Path) -> None:
    system_root = tmp_path / "Windows"
    wsl = system_root / "System32" / "wsl.exe"
    wsl.parent.mkdir(parents=True)
    wsl.write_bytes(b"stub")

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="NODE=v22.22.0\n", stderr="")

    status = sandbox_module._probe(
        distribution="Ubuntu-24.04",
        user="peerbridge",
        runner=runner,
        source={"SYSTEMROOT": str(system_root)},
    )

    assert status.sandbox_verified is False
    assert status.reason == "WSL2 bubblewrap proof marker is missing"


@pytest.mark.parametrize(
    ("agent_id", "profile"),
    (("claude-code", None), ("grok", "grok-build"), ("kimi-code", "kimi")),
)
def test_write_command_hides_host_and_keeps_prompt_on_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
    profile: str | None,
) -> None:
    wsl = tmp_path / "wsl.exe"
    wsl.write_bytes(b"stub")
    status = WslSandboxStatus(
        available=True,
        sandbox_verified=True,
        distribution="Ubuntu-24.04",
        user="peerbridge",
        node_version="v22.22.0",
        acpx_version="0.13.0",
        agent_available={
            "claude-code": True,
            "grok": True,
            "kimi-code": True,
        },
    )
    monkeypatch.setattr(sandbox_module, "probe_wsl_sandbox", lambda **_kwargs: status)
    monkeypatch.setattr(
        sandbox_module,
        "_linux_worktree_path",
        lambda *_args, **_kwargs: "/mnt/c/governed/worktree",
    )
    monkeypatch.setattr(sandbox_module, "_prepare_state", lambda **_kwargs: None)
    monkeypatch.setattr(sandbox_module, "_wsl_executable", lambda *_args, **_kwargs: wsl)
    monkeypatch.setattr(
        sandbox_module,
        "_windows_child_environment",
        lambda *_args, **_kwargs: (("SYSTEMROOT", r"C:\Windows"),),
    )

    command = build_wsl_sandbox_command(
        agent_id,
        working_directory=tmp_path,
        requested_route="reviewed-model",
        permission_tier="edit",
    )

    assert command.boundary == WSL_SANDBOX_BOUNDARY
    assert command.executable == wsl
    assert command.worktree_linux_path == "/mnt/c/governed/worktree"
    for protected_state in (
        "/home/peerbridge/.acpx",
        "/home/peerbridge/.local/state/PeerBridge/acpx-runtime",
    ):
        state_index = command.arguments.index(protected_state)
        assert command.arguments[state_index - 1] == "--ro-bind"
    assert ("--dir", "/mnt") == tuple(
        command.arguments[
            command.arguments.index("/mnt") - 1 : command.arguments.index("/mnt") + 1
        ]
    )
    assert ("--dir", "/home") == tuple(
        command.arguments[
            command.arguments.index("/home") - 1 : command.arguments.index("/home") + 1
        ]
    )
    bind_index = command.arguments.index("/mnt/c/governed/worktree")
    assert command.arguments[bind_index - 1] == "--bind"
    assert command.arguments[bind_index + 1] == "/workspace"
    assert "PRIVATE PROMPT" not in " ".join(command.arguments)
    assert "--unshare-net" not in command.arguments
    if profile is None:
        assert "/home/peerbridge/.local/bin/claude" in command.arguments
        assert "--permission-mode" in command.arguments
        full_access = build_wsl_sandbox_command(
            agent_id,
            working_directory=tmp_path,
            requested_route="reviewed-model",
            permission_tier="full-development",
        )
        mode_index = full_access.arguments.index("--permission-mode")
        assert full_access.arguments[mode_index + 1] == "bypassPermissions"
        assert "--allow-dangerously-skip-permissions" in full_access.arguments
    else:
        assert profile in command.arguments
        assert command.arguments[-4:] == (profile, "exec", "-f", "-")


def test_invalid_distribution_and_permission_tier_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="invalid WSL distribution"):
        probe_wsl_sandbox(distribution="../Ubuntu", runner=lambda *_a, **_k: None)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="write-capable tiers"):
        build_wsl_sandbox_command(
            "claude-code",
            working_directory=tmp_path,
            requested_route=None,
            permission_tier="observe",
        )
