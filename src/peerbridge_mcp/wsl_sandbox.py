"""WSL2/bubblewrap execution boundary for governed non-Codex writers.

The Windows desktop remains the authority process. A write-capable Claude,
Grok, or Kimi task is launched through a dedicated, non-root WSL user and a
bubblewrap mount namespace. Only the already-governed Git worktree is mounted
writable; Windows drives and the rest of the Linux home are hidden.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .child_environment import build_local_child_environment


WSL_SANDBOX_BOUNDARY = "wsl2-bubblewrap-worktree-v1"
DEFAULT_WSL_DISTRIBUTION = "Ubuntu-24.04"
DEFAULT_WSL_USER = "peerbridge"
DEFAULT_WSL_HOME = "/home/peerbridge"
WSL_NODE_ROOT = f"{DEFAULT_WSL_HOME}/.local/opt/node-v22.22.0-linux-x64"
WSL_PATH = ":".join(
    (
        f"{DEFAULT_WSL_HOME}/.local/bin",
        f"{WSL_NODE_ROOT}/bin",
        f"{DEFAULT_WSL_HOME}/.grok/bin",
        f"{DEFAULT_WSL_HOME}/.kimi-code/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    )
)
_SAFE_WSL_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_SAFE_LINUX_PATH = re.compile(r"/[A-Za-z0-9_./ +:@%=-]{1,1000}\Z")
_AGENT_IDS = frozenset({"claude-code", "grok", "kimi-code"})
_AGENT_STATE = {
    "claude-code": f"{DEFAULT_WSL_HOME}/.claude",
    "grok": f"{DEFAULT_WSL_HOME}/.grok",
    "kimi-code": f"{DEFAULT_WSL_HOME}/.kimi-code",
}
_AGENT_BINARY = {
    "claude-code": f"{DEFAULT_WSL_HOME}/.local/bin/claude",
    "grok": f"{DEFAULT_WSL_HOME}/.grok/bin/grok",
    "kimi-code": f"{DEFAULT_WSL_HOME}/.kimi-code/bin/kimi",
}
_AGENT_ACPX_PROFILE = {"grok": "grok-build", "kimi-code": "kimi"}


class WslSandboxError(RuntimeError):
    """The WSL execution boundary is unavailable or invalid."""


@dataclass(frozen=True)
class WslSandboxStatus:
    available: bool
    sandbox_verified: bool
    distribution: str
    user: str
    node_version: str | None
    acpx_version: str | None
    agent_available: Mapping[str, bool]
    reason: str | None = None


@dataclass(frozen=True)
class WslSandboxCommand:
    executable: Path
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    boundary: str
    worktree_linux_path: str


RunProcess = Callable[..., subprocess.CompletedProcess[str]]


def _safe_id(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_WSL_ID.fullmatch(normalized):
        raise WslSandboxError(f"invalid {label}")
    return normalized


def _wsl_executable(source: Mapping[str, str] | None = None) -> Path:
    values = os.environ if source is None else source
    system_root = Path(str(values.get("SYSTEMROOT") or r"C:\Windows"))
    executable = (system_root / "System32" / "wsl.exe").resolve()
    if not executable.is_file():
        raise WslSandboxError("WSL executable is unavailable")
    return executable


def _windows_child_environment(
    source: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    selected = build_local_child_environment(source)
    # WSL receives provider network state from inside the distribution. Do not
    # forward a possibly credential-bearing Windows proxy URL to the launcher.
    for name in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "HOME"):
        selected.pop(name, None)
    return tuple(sorted(selected.items(), key=lambda item: item[0].upper()))


def _failed_status(distribution: str, user: str, reason: str) -> WslSandboxStatus:
    return WslSandboxStatus(
        available=False,
        sandbox_verified=False,
        distribution=distribution,
        user=user,
        node_version=None,
        acpx_version=None,
        agent_available={agent: False for agent in sorted(_AGENT_IDS)},
        reason=reason,
    )


def _probe(
    *,
    distribution: str,
    user: str,
    runner: RunProcess,
    source: Mapping[str, str] | None = None,
) -> WslSandboxStatus:
    distribution = _safe_id(distribution, "WSL distribution")
    user = _safe_id(user, "WSL user")
    try:
        executable = _wsl_executable(source)
    except WslSandboxError as exc:
        return _failed_status(distribution, user, str(exc))
    script = """
set -eu
case "$(uname -r)" in *microsoft-standard-WSL2*) ;; *) exit 21 ;; esac
test "$(id -u)" -ne 0
test -x /usr/bin/bwrap
test -x /home/peerbridge/.local/bin/node
test -x /home/peerbridge/.local/bin/acpx
/home/peerbridge/.local/bin/node -e 'const [a,b]=process.versions.node.split(".").map(Number); if(a<22 || (a===22 && b<13)) process.exit(22)'
/usr/bin/bwrap --die-with-parent --new-session --unshare-pid --unshare-ipc --unshare-uts --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp -- /bin/sh -c 'test -w /tmp && test ! -w /etc'
printf 'PB_SANDBOX_PASS\n'
printf 'NODE=%s\n' "$(/home/peerbridge/.local/bin/node --version)"
printf 'ACPX=%s\n' "$(PATH=/home/peerbridge/.local/bin:/home/peerbridge/.local/opt/node-v22.22.0-linux-x64/bin:/usr/bin:/bin /home/peerbridge/.local/bin/acpx --version)"
if test -x /home/peerbridge/.local/bin/claude; then printf 'AGENT=claude-code:1\n'; else printf 'AGENT=claude-code:0\n'; fi
if test -x /home/peerbridge/.grok/bin/grok; then printf 'AGENT=grok:1\n'; else printf 'AGENT=grok:0\n'; fi
if test -x /home/peerbridge/.kimi-code/bin/kimi; then printf 'AGENT=kimi-code:1\n'; else printf 'AGENT=kimi-code:0\n'; fi
""".strip()
    try:
        completed = runner(
            [
                str(executable),
                "-d",
                distribution,
                "-u",
                user,
                "--",
                "/bin/sh",
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            env=dict(_windows_child_environment(source)),
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return _failed_status(distribution, user, "WSL2 bubblewrap preflight failed")
    if completed.returncode != 0:
        return _failed_status(distribution, user, "WSL2 bubblewrap preflight failed")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    values = {
        key: value
        for line in lines
        if "=" in line and not line.startswith("AGENT=")
        for key, value in (line.split("=", 1),)
    }
    agents = {
        agent: f"AGENT={agent}:1" in lines for agent in sorted(_AGENT_IDS)
    }
    verified = "PB_SANDBOX_PASS" in lines
    return WslSandboxStatus(
        available=True,
        sandbox_verified=verified,
        distribution=distribution,
        user=user,
        node_version=values.get("NODE"),
        acpx_version=values.get("ACPX"),
        agent_available=agents,
        reason=None if verified else "WSL2 bubblewrap proof marker is missing",
    )


@functools.lru_cache(maxsize=4)
def _cached_probe(distribution: str, user: str) -> WslSandboxStatus:
    return _probe(
        distribution=distribution,
        user=user,
        runner=subprocess.run,
    )


def probe_wsl_sandbox(
    *,
    distribution: str = DEFAULT_WSL_DISTRIBUTION,
    user: str = DEFAULT_WSL_USER,
    runner: RunProcess | None = None,
) -> WslSandboxStatus:
    """Return a bounded, credential-free host capability observation."""

    if os.name != "nt" and runner is None:
        return _failed_status(distribution, user, "WSL2 is available only on Windows")
    if runner is None:
        return _cached_probe(distribution, user)
    return _probe(distribution=distribution, user=user, runner=runner)


def clear_wsl_sandbox_probe_cache() -> None:
    _cached_probe.cache_clear()


def _run_wsl_text(
    args: Sequence[str],
    *,
    distribution: str,
    user: str,
    timeout: float = 15.0,
) -> str:
    executable = _wsl_executable()
    completed = subprocess.run(
        [str(executable), "-d", distribution, "-u", user, "--", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=dict(_windows_child_environment()),
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )
    if completed.returncode != 0:
        raise WslSandboxError("WSL2 path or state preparation failed")
    return completed.stdout.strip()


def _linux_worktree_path(
    worktree: Path,
    *,
    distribution: str,
    user: str,
) -> str:
    resolved = Path(worktree).resolve(strict=True)
    if not resolved.is_dir():
        raise WslSandboxError("governed worktree is unavailable")
    translated = _run_wsl_text(
        ("/usr/bin/wslpath", "-a", "-u", str(resolved)),
        distribution=distribution,
        user=user,
    )
    if not _SAFE_LINUX_PATH.fullmatch(translated) or not translated.startswith("/mnt/"):
        raise WslSandboxError("governed worktree did not map to a safe WSL path")
    return translated


def _prepare_state(*, distribution: str, user: str, agent_id: str) -> None:
    state = _AGENT_STATE[agent_id]
    _run_wsl_text(
        (
            "/bin/mkdir",
            "-p",
            f"{DEFAULT_WSL_HOME}/.acpx",
            f"{DEFAULT_WSL_HOME}/.local/state/PeerBridge/acpx-runtime",
            state,
        ),
        distribution=distribution,
        user=user,
    )


def build_wsl_sandbox_command(
    agent_id: str,
    *,
    working_directory: Path,
    requested_route: str | None,
    permission_tier: str,
    distribution: str = DEFAULT_WSL_DISTRIBUTION,
    user: str = DEFAULT_WSL_USER,
) -> WslSandboxCommand:
    """Build one stdin-driven command for a governed WSL worktree turn."""

    agent_id = str(agent_id or "").strip()
    if agent_id not in _AGENT_IDS:
        raise WslSandboxError("Agent has no reviewed WSL sandbox profile")
    if permission_tier not in {"edit", "full-development"}:
        raise WslSandboxError("WSL sandbox is only for write-capable tiers")
    status = probe_wsl_sandbox(distribution=distribution, user=user)
    if not status.sandbox_verified or not status.agent_available.get(agent_id, False):
        raise WslSandboxError("verified WSL2 Agent sandbox is unavailable")
    linux_worktree = _linux_worktree_path(
        working_directory,
        distribution=distribution,
        user=user,
    )
    _prepare_state(distribution=distribution, user=user, agent_id=agent_id)
    agent_state = _AGENT_STATE[agent_id]
    bwrap: list[str] = [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--ro-bind",
        "/etc",
        "/etc",
        "--ro-bind",
        "/var",
        "/var",
        "--ro-bind",
        "/sys",
        "/sys",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--dir",
        "/mnt",
        "--dir",
        "/mnt/wsl",
        "--ro-bind",
        "/mnt/wsl/resolv.conf",
        "/mnt/wsl/resolv.conf",
        "--dir",
        "/home",
        "--dir",
        DEFAULT_WSL_HOME,
        "--ro-bind",
        f"{DEFAULT_WSL_HOME}/.local",
        f"{DEFAULT_WSL_HOME}/.local",
        "--ro-bind",
        f"{DEFAULT_WSL_HOME}/.acpx",
        f"{DEFAULT_WSL_HOME}/.acpx",
        "--ro-bind",
        f"{DEFAULT_WSL_HOME}/.local/state/PeerBridge/acpx-runtime",
        f"{DEFAULT_WSL_HOME}/.local/state/PeerBridge/acpx-runtime",
        "--ro-bind",
        agent_state,
        agent_state,
    ]
    if agent_id in {"grok", "kimi-code"}:
        bwrap.extend(("--ro-bind", f"{agent_state}/bin", f"{agent_state}/bin"))
    bwrap.extend(
        (
            "--dir",
            "/workspace",
            "--bind",
            linux_worktree,
            "/workspace",
            "--chdir",
            "/workspace",
            "--setenv",
            "HOME",
            DEFAULT_WSL_HOME,
            "--setenv",
            "PWD",
            "/workspace",
            "--setenv",
            "PATH",
            WSL_PATH,
            "--setenv",
            "PEERBRIDGE_SANDBOX_BOUNDARY",
            WSL_SANDBOX_BOUNDARY,
            "--",
        )
    )
    if agent_id == "claude-code":
        bwrap.extend(
            (
                _AGENT_BINARY[agent_id],
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                (
                    "bypassPermissions"
                    if permission_tier == "full-development"
                    else "acceptEdits"
                ),
                "--no-session-persistence",
                "--no-chrome",
            )
        )
        if permission_tier == "full-development":
            bwrap.append("--allow-dangerously-skip-permissions")
        if requested_route:
            bwrap.extend(("--model", requested_route))
    else:
        bwrap.extend(
            (
                f"{DEFAULT_WSL_HOME}/.local/bin/acpx",
                "--cwd",
                "/workspace",
                "--auth-policy",
                "fail",
                "--approve-all",
                "--format",
                "json",
                "--json-strict",
                "--max-turns",
                "1",
                "--prompt-retries",
                "0",
                "--timeout",
                "300",
            )
        )
        if permission_tier == "edit":
            bwrap.append("--no-terminal")
        if requested_route:
            bwrap.extend(("--model", requested_route))
        bwrap.extend((_AGENT_ACPX_PROFILE[agent_id], "exec", "-f", "-"))
    executable = _wsl_executable()
    arguments = (
        "-d",
        _safe_id(distribution, "WSL distribution"),
        "-u",
        _safe_id(user, "WSL user"),
        "--",
        *bwrap,
    )
    return WslSandboxCommand(
        executable=executable,
        arguments=tuple(arguments),
        environment=_windows_child_environment(),
        boundary=WSL_SANDBOX_BOUNDARY,
        worktree_linux_path=linux_worktree,
    )


__all__ = [
    "DEFAULT_WSL_DISTRIBUTION",
    "DEFAULT_WSL_USER",
    "WSL_SANDBOX_BOUNDARY",
    "WslSandboxCommand",
    "WslSandboxError",
    "WslSandboxStatus",
    "build_wsl_sandbox_command",
    "clear_wsl_sandbox_probe_cache",
    "probe_wsl_sandbox",
]
