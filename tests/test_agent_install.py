from __future__ import annotations

import base64
import hashlib
import os
import subprocess
from dataclasses import replace
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


def _npm_fixture() -> tuple[str, str]:
    if os.name == "nt":
        return r"C:\Program Files\nodejs\npm.cmd", "npm.cmd"
    return "/usr/bin/npm", "npm"


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
    assert specs[-1].package_integrity.startswith("sha512-")

    npm, executable_name = _npm_fixture()
    monkeypatch.setattr(agent_install, "_path_is_within", lambda *_args: True)
    command = build_install_command(
        "acpx-runtime", which=lambda name: npm if name == executable_name else None
    )
    assert command[1:4] == ("install", "--global", "acpx@0.13.0")


def _integrity(payload: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")


def test_detect_reports_presence_without_requiring_version_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, stdout="", stderr="old client")

    trusted = tmp_path / "trusted"
    executable = trusted / "codex.exe"
    trusted.mkdir()
    executable.write_bytes(b"signed test executable")
    monkeypatch.setattr(
        agent_install, "_trusted_executable_roots", lambda _spec: (trusted,)
    )
    monkeypatch.setattr(
        agent_install, "_verify_windows_authenticode", lambda *_args: True
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
    trusted.mkdir()
    executable.write_bytes(b"reviewed test executable")
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


def test_detection_rejects_an_unverified_publisher_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    executable = trusted / "codex.exe"
    executable.write_bytes(b"unsigned replacement")
    probes = 0

    def forbidden_run(*_args, **_kwargs):
        nonlocal probes
        probes += 1
        raise AssertionError("unverified executable must not be probed")

    monkeypatch.setattr(
        agent_install, "_trusted_executable_roots", lambda _spec: (trusted,)
    )
    monkeypatch.setattr(
        agent_install, "_verify_windows_authenticode", lambda *_args: False
    )

    status = detect_official_agent(
        "codex",
        which=lambda name: str(executable) if name == "codex.exe" else None,
        run=forbidden_run,
    )

    assert status.installed is False
    assert status.executable_path is None
    assert probes == 0


def test_trusted_publisher_root_is_probed_when_path_has_not_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    executable = trusted / "grok.exe"
    executable.write_bytes(b"publisher executable")
    monkeypatch.setattr(
        agent_install, "_trusted_executable_roots", lambda _spec: (trusted,)
    )

    found = agent_install.find_trusted_executable(
        official_agent_spec("grok"), which=lambda _name: None
    )

    assert found == executable.resolve()


def test_claude_wrapper_resolves_to_verified_publisher_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted"
    target = trusted / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"signed publisher binary")
    wrapper = trusted / "claude.cmd"
    wrapper.write_text(
        '@ECHO off\n"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\n',
        encoding="utf-8",
    )
    verified: list[Path] = []
    monkeypatch.setattr(agent_install, "_trusted_executable_roots", lambda _spec: (trusted,))
    monkeypatch.setattr(
        agent_install,
        "_verify_windows_authenticode",
        lambda _spec, path: not verified.append(path),
    )

    found = agent_install.find_trusted_executable(
        official_agent_spec("claude-code"),
        which=lambda name: str(wrapper) if name == "claude.cmd" else None,
    )

    assert found == target.resolve()
    assert verified == [target.resolve()]


def test_signed_agent_launch_rejects_unverified_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    executable = trusted / "codex.exe"
    executable.write_bytes(b"unsigned replacement")
    monkeypatch.setattr(agent_install, "_trusted_executable_roots", lambda _spec: (trusted,))
    monkeypatch.setattr(agent_install, "_verify_windows_authenticode", lambda *_args: False)

    assert agent_install.find_trusted_executable(
        official_agent_spec("codex"),
        which=lambda name: str(executable) if name == "codex.exe" else None,
    ) is None


def test_npm_commands_are_exact_allowlisted_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    npm, executable_name = _npm_fixture()
    monkeypatch.setattr(agent_install, "_path_is_within", lambda *_args: True)

    def which(name: str) -> str | None:
        return npm if name == executable_name else None

    codex = build_official_install_command("codex", which=which)
    kimi = build_official_install_command("kimi-code", which=which)
    assert codex[1:4] == ("install", "--global", "@openai/codex@0.149.0")
    assert kimi[1:4] == (
        "install",
        "--global",
        "@moonshot-ai/kimi-code@0.38.0",
    )


@pytest.mark.parametrize(
    "identifier",
    (
        "@openai/codex@latest",
        "@openai/codex@next",
        "https://attacker.invalid/codex.tgz",
        "../codex.tgz",
        "@openai/codex@0.149",
    ),
)
def test_npm_install_contract_rejects_mutable_or_non_registry_targets(
    identifier: str,
) -> None:
    spec = replace(official_agent_spec("codex"), package_identifier=identifier)
    with pytest.raises(AgentInstallError, match="reviewed exact version"):
        agent_install._validate_reviewed_npm_spec(spec)


def test_npm_install_contract_requires_reviewed_sha512_integrity() -> None:
    spec = replace(official_agent_spec("codex"), package_integrity=None)
    with pytest.raises(AgentInstallError, match="SHA-512 integrity"):
        agent_install._validate_reviewed_npm_spec(spec)


def test_npm_integrity_preflight_uses_fixed_registry_and_exact_sri() -> None:
    spec = official_agent_spec("codex")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f'"{spec.package_integrity}"',
            stderr="",
        )

    agent_install._verify_published_npm_integrity(
        spec, "/trusted/npm", run=fake_run
    )
    assert calls[0][0] == (
        "/trusted/npm",
        "view",
        "@openai/codex@0.149.0",
        "dist.integrity",
        "--json",
        "--registry",
        "https://registry.npmjs.org/",
    )
    assert calls[0][1]["shell"] is False


def test_npm_integrity_preflight_rejects_registry_drift() -> None:
    spec = official_agent_spec("codex")

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='"sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="',
            stderr="",
        )

    with pytest.raises(AgentInstallError, match="differs from reviewed release"):
        agent_install._verify_published_npm_integrity(
            spec, "/trusted/npm", run=fake_run
        )


def test_npm_tarball_bytes_are_verified_before_install() -> None:
    payload = b"reviewed npm tarball bytes"
    spec = replace(official_agent_spec("codex"), package_integrity=_integrity(payload))
    calls: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        calls.append(tuple(command))
        destination = Path(command[command.index("--pack-destination") + 1])
        (destination / "reviewed-package.tgz").write_bytes(payload)
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(command, 0)

    staged = agent_install._stage_verified_npm_tarball(
        spec, "/trusted/npm", run=fake_run
    )
    try:
        assert staged.path.read_bytes() == payload
        assert staged.sha512 == spec.package_integrity
        assert staged.bytes == len(payload)
        assert calls[0][1:4] == ("pack", "@openai/codex@0.149.0", "--ignore-scripts")
    finally:
        agent_install._remove_npm_stage(staged.staging_directory)


def test_npm_tarball_digest_mismatch_fails_closed_and_cleans_stage() -> None:
    expected = b"reviewed bytes"
    observed = b"substituted bytes"
    spec = replace(official_agent_spec("codex"), package_integrity=_integrity(expected))
    staged_directories: list[Path] = []

    def fake_run(command, **_kwargs):
        destination = Path(command[command.index("--pack-destination") + 1])
        staged_directories.append(destination)
        (destination / "substituted-package.tgz").write_bytes(observed)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(AgentInstallError, match="archive differs"):
        agent_install._stage_verified_npm_tarball(spec, "/trusted/npm", run=fake_run)
    assert staged_directories and not staged_directories[0].exists()


def test_npm_tarball_identity_is_revalidated_before_launch() -> None:
    payload = b"reviewed npm tarball bytes"
    spec = replace(official_agent_spec("codex"), package_integrity=_integrity(payload))

    def fake_run(command, **_kwargs):
        destination = Path(command[command.index("--pack-destination") + 1])
        (destination / "reviewed-package.tgz").write_bytes(payload)
        return subprocess.CompletedProcess(command, 0)

    staged = agent_install._stage_verified_npm_tarball(
        spec, "/trusted/npm", run=fake_run
    )
    try:
        os.chmod(staged.path, 0o600)
        staged.path.write_bytes(payload + b"changed")
        with pytest.raises(AgentInstallError, match="changed before launch"):
            agent_install._revalidate_npm_tarball(staged)
    finally:
        agent_install._remove_npm_stage(staged.staging_directory)


def test_verified_npm_install_uses_local_tarball_and_reviewed_script_policy(
    tmp_path: Path,
) -> None:
    tarball = tmp_path / "reviewed.tgz"
    tarball.write_bytes(b"payload")
    identity = agent_install._npm_file_identity(tarball)
    staged = agent_install._VerifiedNpmTarball(
        path=tarball,
        staging_directory=tmp_path,
        bytes=7,
        sha512=_integrity(b"payload"),
        stat_identity=identity,
    )
    codex = agent_install._verified_npm_install_command(
        official_agent_spec("codex"), "/trusted/npm", staged
    )
    kimi = agent_install._verified_npm_install_command(
        official_agent_spec("kimi-code"), "/trusted/npm", staged
    )
    assert codex[1:4] == ("install", "--global", str(tarball))
    assert "--ignore-scripts" in codex
    assert "--ignore-scripts" not in kimi


@pytest.mark.skipif(os.name != "nt", reason="Winget is a Windows-only contract")
def test_claude_uses_publisher_winget_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winget = r"C:\Windows\winget.exe"
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


def test_launcher_never_uses_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class Process:
        pid = 4242

        def wait(self):
            return 0

    def fake_popen(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return Process()

    def fake_registry_run(command, **_kwargs):
        package = command[2]
        if package == "@openai/codex@0.149.0":
            integrity = official_agent_spec("codex").package_integrity
        else:
            integrity = agent_install.ACPX_RUNTIME_SPEC.package_integrity
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f'"{integrity}"',
            stderr="",
        )

    def fake_stage(spec, _manager, **_kwargs):
        staging = tmp_path / spec.agent_id
        staging.mkdir()
        tarball = staging / f"{spec.agent_id}.tgz"
        tarball.write_bytes(b"reviewed tarball")
        return agent_install._VerifiedNpmTarball(
            path=tarball,
            staging_directory=staging,
            bytes=tarball.stat().st_size,
            sha512=spec.package_integrity or "",
            stat_identity=agent_install._npm_file_identity(tarball),
        )

    npm, executable_name = _npm_fixture()
    monkeypatch.setattr(agent_install, "_path_is_within", lambda *_args: True)
    monkeypatch.setattr(agent_install, "_stage_verified_npm_tarball", fake_stage)
    process = launch_official_agent_installer(
        "codex",
        which=lambda name: npm if name == executable_name else None,
        run=fake_registry_run,
        popen=fake_popen,
    )
    assert isinstance(process, Process)
    assert calls[0][1]["shell"] is False
    assert calls[0][0][1:3] == ("install", "--global")
    assert calls[0][0][3].endswith("codex.tgz")
    assert "--ignore-scripts" in calls[0][0]
    assert calls[0][1]["cwd"] == str(Path(npm).resolve().parent)

    runtime = launch_agent_installer(
        "acpx-runtime",
        which=lambda name: npm if name == executable_name else None,
        run=fake_registry_run,
        popen=fake_popen,
    )
    assert isinstance(runtime, Process)
    assert calls[1][1]["shell"] is False
    assert calls[1][0][3].endswith("acpx-runtime.tgz")
    assert "--ignore-scripts" in calls[1][0]


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
