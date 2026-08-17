from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from peerbridge_mcp import agent_install
from peerbridge_mcp.agent_install import (
    AgentInstallError,
    build_install_command,
    build_official_install_command,
    detect_installable_agent,
    detect_official_agent,
    installable_agent_specs,
    launch_agent_installer,
    launch_official_agent_installer,
    official_agent_spec,
    official_agent_specs,
)


def test_catalog_is_fixed_to_publisher_https_sources() -> None:
    specs = official_agent_specs()
    assert tuple(spec.agent_id for spec in specs) == (
        "codex",
        "claude-code",
        "kimi-code",
        "grok",
    )
    assert all(spec.docs_url.startswith("https://") for spec in specs)
    assert all("api_key" not in vars(spec) for spec in specs)
    with pytest.raises(AgentInstallError):
        official_agent_spec("https://attacker.invalid/install")


def test_optional_runtime_is_separate_and_version_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs = installable_agent_specs()
    assert tuple(spec.agent_id for spec in specs[-1:]) == ("acpx-runtime",)
    assert specs[-1].publisher == "OpenClaw community"
    assert specs[-1].package_identifier == "acpx@0.13.0"

    npm = r"C:\Program Files\nodejs\npm.cmd"
    monkeypatch.setattr(agent_install, "_path_is_within", lambda *_args: True)
    command = build_install_command(
        "acpx-runtime", which=lambda name: npm if name == "npm.cmd" else None
    )
    assert command[1:4] == ("install", "--global", "acpx@0.13.0")


def test_detect_reports_presence_without_requiring_version_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, stdout="", stderr="old client")

    trusted = tmp_path / "trusted"
    executable = trusted / "codex.exe"
    monkeypatch.setattr(
        agent_install, "_trusted_executable_roots", lambda _spec: (trusted,)
    )
    status = detect_official_agent(
        "codex",
        which=lambda name: str(executable) if name == "codex.exe" else None,
        run=fake_run,
    )
    assert status.installed is True
    assert status.executable_path == str(executable.resolve())
    assert status.version is None


def test_detect_optional_runtime_uses_the_same_bounded_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 8
        return subprocess.CompletedProcess([], 0, stdout="0.13.0\n", stderr="")

    trusted = tmp_path / "trusted"
    executable = trusted / "acpx.cmd"
    monkeypatch.setattr(
        agent_install, "_trusted_executable_roots", lambda _spec: (trusted,)
    )
    status = detect_installable_agent(
        "acpx-runtime",
        which=lambda name: str(executable) if name == "acpx.cmd" else None,
        run=fake_run,
    )
    assert status.installed is True
    assert status.version == "0.13.0"


def test_npm_commands_are_exact_allowlisted_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    npm = r"C:\Program Files\nodejs\npm.cmd"
    monkeypatch.setattr(agent_install, "_path_is_within", lambda *_args: True)

    def which(name: str) -> str | None:
        return npm if name == "npm.cmd" else None

    codex = build_official_install_command("codex", which=which)
    kimi = build_official_install_command("kimi-code", which=which)
    assert codex[1:4] == ("install", "--global", "@openai/codex@latest")
    assert kimi[1:4] == ("install", "--global", "@moonshot-ai/kimi-code@latest")


def test_claude_uses_publisher_winget_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winget = r"C:\Windows\winget.exe"
    monkeypatch.setattr(agent_install.os, "name", "nt")
    monkeypatch.setattr(agent_install, "_path_is_within", lambda *_args: True)

    def which(name: str) -> str | None:
        return winget if name == "winget.exe" else None

    install = build_official_install_command("claude-code", which=which)
    update = build_official_install_command("claude-code", update=True, which=which)
    assert install[1:4] == ("install", "--id", "Anthropic.ClaudeCode")
    assert update[1:4] == ("upgrade", "--id", "Anthropic.ClaudeCode")


def test_grok_windows_auto_install_fails_closed() -> None:
    with pytest.raises(AgentInstallError, match="publisher-verified"):
        build_official_install_command("grok")


def test_launcher_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class Process:
        pass

    def fake_popen(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return Process()

    npm = r"C:\Program Files\nodejs\npm.cmd"
    monkeypatch.setattr(agent_install, "_path_is_within", lambda *_args: True)
    process = launch_official_agent_installer(
        "codex",
        which=lambda name: npm if name == "npm.cmd" else None,
        popen=fake_popen,
    )
    assert isinstance(process, Process)
    assert calls[0][1]["shell"] is False
    assert "@openai/codex@latest" in calls[0][0]

    runtime = launch_agent_installer(
        "acpx-runtime",
        which=lambda name: npm if name == "npm.cmd" else None,
        popen=fake_popen,
    )
    assert isinstance(runtime, Process)
    assert calls[1][1]["shell"] is False
    assert "acpx@0.13.0" in calls[1][0]


def test_hostile_path_agent_is_not_probed_or_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted"
    hostile = tmp_path / "hostile" / "codex.exe"
    calls = 0

    def forbidden_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("untrusted executable must not be probed")

    monkeypatch.setattr(
        agent_install, "_trusted_executable_roots", lambda _spec: (trusted,)
    )
    status = detect_official_agent(
        "codex",
        which=lambda name: str(hostile) if name == "codex.exe" else None,
        run=forbidden_run,
    )

    assert status.installed is False
    assert status.executable_path is None
    assert calls == 0


def test_hostile_path_package_manager_is_rejected(tmp_path: Path) -> None:
    hostile = tmp_path / ("npm.cmd" if agent_install.os.name == "nt" else "npm")

    with pytest.raises(AgentInstallError, match="package manager is unavailable"):
        build_official_install_command(
            "codex",
            which=lambda name: str(hostile) if name in {"npm", "npm.cmd"} else None,
        )
