"""Allowlisted discovery and visible installation of Agent terminals and runtimes.

PeerBridge never accepts an installer URL or command from room messages, provider
metadata, or user input.  Every executable, package identifier, and documentation
URL below is a reviewed constant from the Agent publisher.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


class AgentInstallError(RuntimeError):
    """An official Agent terminal cannot be detected or installed safely."""


@dataclass(frozen=True)
class AgentInstallSpec:
    agent_id: str
    display_name: str
    publisher: str
    executable_names: tuple[str, ...]
    version_args: tuple[str, ...]
    docs_url: str
    package_manager: str | None
    package_identifier: str | None
    note_key: str

    @property
    def automatic_install_supported(self) -> bool:
        return bool(self.package_manager and self.package_identifier)


@dataclass(frozen=True)
class AgentInstallStatus:
    agent_id: str
    installed: bool
    executable_path: str | None
    version: str | None
    automatic_install_supported: bool


OFFICIAL_AGENT_CATALOG: Mapping[str, AgentInstallSpec] = {
    "codex": AgentInstallSpec(
        agent_id="codex",
        display_name="OpenAI Codex",
        publisher="OpenAI",
        executable_names=("codex.exe", "codex.cmd", "codex"),
        version_args=("--version",),
        docs_url="https://github.com/openai/codex",
        package_manager="npm",
        package_identifier="@openai/codex@latest",
        note_key="agent_install.note.codex",
    ),
    "claude-code": AgentInstallSpec(
        agent_id="claude-code",
        display_name="Claude Code",
        publisher="Anthropic",
        executable_names=("claude.exe", "claude.cmd", "claude"),
        version_args=("--version",),
        docs_url="https://code.claude.com/docs/en/installation",
        package_manager="winget",
        package_identifier="Anthropic.ClaudeCode",
        note_key="agent_install.note.claude",
    ),
    "kimi-code": AgentInstallSpec(
        agent_id="kimi-code",
        display_name="Kimi Code",
        publisher="Moonshot AI",
        executable_names=("kimi.exe", "kimi.cmd", "kimi"),
        version_args=("--version",),
        docs_url="https://github.com/MoonshotAI/kimi-code",
        package_manager="npm",
        package_identifier="@moonshot-ai/kimi-code@latest",
        note_key="agent_install.note.kimi",
    ),
    "grok": AgentInstallSpec(
        agent_id="grok",
        display_name="Grok CLI",
        publisher="xAI",
        executable_names=("grok.exe", "grok.cmd", "grok"),
        version_args=("--version",),
        docs_url="https://github.com/xai-org/grok-build",
        package_manager=None,
        package_identifier=None,
        note_key="agent_install.note.grok",
    ),
}


# ACPX is a community interoperability runtime, not an Agent publisher.  Keep it
# outside the official catalog so receipts and UI labels cannot confuse the
# transport/runtime with Codex, Claude Code, Kimi Code, or Grok identity.
ACPX_RUNTIME_SPEC = AgentInstallSpec(
    agent_id="acpx-runtime",
    display_name="ACPX interoperability runtime",
    publisher="OpenClaw community",
    executable_names=("acpx.exe", "acpx.cmd", "acpx"),
    version_args=("--version",),
    docs_url="https://github.com/openclaw/acpx",
    package_manager="npm",
    package_identifier="acpx@0.13.0",
    note_key="agent_install.note.acpx",
)


def official_agent_specs() -> tuple[AgentInstallSpec, ...]:
    return tuple(OFFICIAL_AGENT_CATALOG.values())


def installable_agent_specs() -> tuple[AgentInstallSpec, ...]:
    """Return official Agents followed by reviewed optional runtimes."""
    return (*official_agent_specs(), ACPX_RUNTIME_SPEC)


def official_agent_spec(agent_id: str) -> AgentInstallSpec:
    try:
        return OFFICIAL_AGENT_CATALOG[agent_id]
    except KeyError as exc:
        raise AgentInstallError("unknown official Agent terminal") from exc


def installable_agent_spec(agent_id: str) -> AgentInstallSpec:
    if agent_id == ACPX_RUNTIME_SPEC.agent_id:
        return ACPX_RUNTIME_SPEC
    return official_agent_spec(agent_id)


def _existing_environment_path(name: str, *parts: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value, *parts).resolve() if value else None


def _trusted_executable_roots(spec: AgentInstallSpec) -> tuple[Path, ...]:
    """Return reviewed install roots; ambient PATH alone is never authority."""

    roots: list[Path | None]
    if os.name == "nt":
        common = [
            _existing_environment_path("LOCALAPPDATA", "Microsoft", "WinGet", "Links"),
            _existing_environment_path("USERPROFILE", ".local", "bin"),
        ]
        if spec.agent_id == "codex":
            roots = [
                _existing_environment_path(
                    "LOCALAPPDATA", "Programs", "OpenAI", "Codex", "bin"
                ),
                _existing_environment_path("APPDATA", "npm"),
                *common,
            ]
        elif spec.agent_id == "claude-code":
            roots = [
                _existing_environment_path("USERPROFILE", ".covs", "npm-global"),
                _existing_environment_path("APPDATA", "npm"),
                *common,
            ]
        elif spec.agent_id in {"kimi-code", "acpx-runtime"}:
            roots = [
                _existing_environment_path("APPDATA", "npm"),
                *common,
            ]
        else:
            roots = [
                _existing_environment_path("LOCALAPPDATA", "Programs", "xAI"),
                _existing_environment_path("USERPROFILE", ".grok", "bin"),
                *common,
            ]
    else:
        roots = [
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            _existing_environment_path("HOME", ".local", "bin"),
            _existing_environment_path("HOME", ".npm-global", "bin"),
        ]
    return tuple(path for path in roots if path is not None)


def _path_is_within(path: Path, roots: Iterable[Path]) -> bool:
    candidate = os.path.normcase(str(path.resolve()))
    for root in roots:
        trusted = os.path.normcase(str(root.resolve()))
        try:
            if os.path.commonpath((candidate, trusted)) == trusted:
                return True
        except ValueError:
            continue
    return False


def _find_executable(
    spec: AgentInstallSpec,
    *,
    which: Callable[[str], str | None],
) -> str | None:
    trusted_roots = _trusted_executable_roots(spec)
    for name in spec.executable_names:
        match = which(name)
        if match:
            path = Path(match).resolve()
            if path.name.lower() != Path(name).name.lower():
                continue
            if _path_is_within(path, trusted_roots):
                return str(path)
    return None


def find_trusted_executable(
    spec: AgentInstallSpec,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """Resolve one executable only when its real path is under a reviewed root."""

    match = _find_executable(spec, which=which)
    return Path(match) if match is not None else None


def detect_official_agent(
    agent_id: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> AgentInstallStatus:
    spec = official_agent_spec(agent_id)
    executable = _find_executable(spec, which=which)
    version: str | None = None
    if executable:
        try:
            result = run(
                (executable, *spec.version_args),
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                shell=False,
            )
            output = (result.stdout or result.stderr or "").strip().splitlines()
            if result.returncode == 0 and output:
                version = output[0][:160]
        except (OSError, subprocess.SubprocessError):
            # Presence is still useful even when an old client lacks --version.
            version = None
    return AgentInstallStatus(
        agent_id=agent_id,
        installed=executable is not None,
        executable_path=executable,
        version=version,
        automatic_install_supported=spec.automatic_install_supported,
    )


def detect_installable_agent(
    agent_id: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> AgentInstallStatus:
    spec = installable_agent_spec(agent_id)
    executable = _find_executable(spec, which=which)
    version: str | None = None
    if executable:
        try:
            result = run(
                (executable, *spec.version_args),
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                shell=False,
            )
            output = (result.stdout or result.stderr or "").strip().splitlines()
            if result.returncode == 0 and output:
                version = output[0][:160]
        except (OSError, subprocess.SubprocessError):
            version = None
    return AgentInstallStatus(
        agent_id=agent_id,
        installed=executable is not None,
        executable_path=executable,
        version=version,
        automatic_install_supported=spec.automatic_install_supported,
    )


def detect_all_official_agents() -> tuple[AgentInstallStatus, ...]:
    return tuple(detect_official_agent(spec.agent_id) for spec in official_agent_specs())


def detect_all_installable_agents() -> tuple[AgentInstallStatus, ...]:
    return tuple(
        detect_installable_agent(spec.agent_id) for spec in installable_agent_specs()
    )


def _resolve_package_manager(
    manager: str,
    *,
    which: Callable[[str], str | None],
) -> str:
    candidates: Sequence[str]
    expected_names: frozenset[str]
    if manager == "npm":
        candidates = ("npm.cmd", "npm") if os.name == "nt" else ("npm",)
        expected_names = frozenset({"npm", "npm.cmd", "npm.exe"})
        trusted_roots = (
            (_existing_environment_path("PROGRAMFILES", "nodejs"),)
            if os.name == "nt"
            else (Path("/usr/bin"), Path("/usr/local/bin"), Path("/opt/homebrew/bin"))
        )
    elif manager == "winget":
        if os.name != "nt":
            raise AgentInstallError("winget installation is only available on Windows")
        candidates = ("winget.exe", "winget")
        expected_names = frozenset({"winget", "winget.exe"})
        trusted_roots = (
            _existing_environment_path("LOCALAPPDATA", "Microsoft", "WindowsApps"),
        )
    else:
        raise AgentInstallError("unsupported official package manager")
    for candidate in candidates:
        resolved = which(candidate)
        if not resolved:
            continue
        path = Path(resolved).resolve()
        if path.name.lower() not in expected_names:
            continue
        if not _path_is_within(path, (root for root in trusted_roots if root)):
            continue
        return str(path)
    raise AgentInstallError(f"required package manager is unavailable: {manager}")


def build_official_install_command(
    agent_id: str,
    *,
    update: bool = False,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    spec = official_agent_spec(agent_id)
    if not spec.automatic_install_supported:
        raise AgentInstallError("automatic Windows installation is not publisher-verified")
    assert spec.package_manager is not None
    assert spec.package_identifier is not None
    manager = _resolve_package_manager(spec.package_manager, which=which)
    if spec.package_manager == "npm":
        return (
            manager,
            "install",
            "--global",
            spec.package_identifier,
            "--no-audit",
            "--no-fund",
        )
    action = "upgrade" if update else "install"
    return (
        manager,
        action,
        "--id",
        spec.package_identifier,
        "--exact",
        "--source",
        "winget",
        "--accept-package-agreements",
        "--accept-source-agreements",
    )


def build_install_command(
    agent_id: str,
    *,
    update: bool = False,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    if agent_id != ACPX_RUNTIME_SPEC.agent_id:
        return build_official_install_command(agent_id, update=update, which=which)
    spec = ACPX_RUNTIME_SPEC
    assert spec.package_manager is not None
    assert spec.package_identifier is not None
    manager = _resolve_package_manager(spec.package_manager, which=which)
    return (
        manager,
        "install",
        "--global",
        spec.package_identifier,
        "--no-audit",
        "--no-fund",
    )


def launch_official_agent_installer(
    agent_id: str,
    *,
    update: bool = False,
    which: Callable[[str], str | None] = shutil.which,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes]:
    command = build_official_install_command(agent_id, update=update, which=which)
    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        return popen(
            command,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise AgentInstallError("official package manager could not be launched") from exc


def launch_agent_installer(
    agent_id: str,
    *,
    update: bool = False,
    which: Callable[[str], str | None] = shutil.which,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes]:
    command = build_install_command(agent_id, update=update, which=which)
    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        return popen(
            command,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise AgentInstallError("reviewed package manager could not be launched") from exc


__all__ = [
    "ACPX_RUNTIME_SPEC",
    "AgentInstallError",
    "AgentInstallSpec",
    "AgentInstallStatus",
    "build_install_command",
    "build_official_install_command",
    "detect_all_installable_agents",
    "detect_all_official_agents",
    "detect_installable_agent",
    "detect_official_agent",
    "find_trusted_executable",
    "installable_agent_spec",
    "installable_agent_specs",
    "launch_agent_installer",
    "launch_official_agent_installer",
    "official_agent_spec",
    "official_agent_specs",
]
