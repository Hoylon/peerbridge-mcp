"""macOS Seatbelt policy contracts for governed PeerBridge worktrees."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MACOS_SANDBOX_BOUNDARY = "macos-seatbelt-worktree-v1"
_SAFE_PROFILE_PATH = re.compile(r"/[^\x00\r\n]{0,1500}\Z")


class MacSandboxError(RuntimeError):
    """A macOS Seatbelt profile or runtime is unavailable."""


@dataclass(frozen=True)
class MacSandboxProfile:
    boundary: str
    worktree: Path
    scratch: Path
    profile: str


def _scheme_string(path: Path) -> str:
    resolved = str(Path(path).resolve())
    if not _SAFE_PROFILE_PATH.fullmatch(resolved):
        raise MacSandboxError("macOS sandbox path is invalid")
    return '"' + resolved.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_macos_seatbelt_profile(
    *,
    worktree: Path,
    scratch: Path,
    readable_roots: Iterable[Path] = (),
) -> MacSandboxProfile:
    """Return a deny-by-default profile with one writable governed worktree."""

    try:
        worktree = Path(worktree).resolve(strict=True)
        scratch = Path(scratch).resolve(strict=True)
    except OSError as exc:
        raise MacSandboxError("macOS sandbox directory is unavailable") from exc
    if not worktree.is_dir() or not scratch.is_dir():
        raise MacSandboxError("macOS sandbox directory is unavailable")
    system_reads = (
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library"),
        Path("/private/etc"),
        Path("/dev"),
    )
    read_paths = [*system_reads, worktree, scratch, *(Path(p).resolve() for p in readable_roots)]
    read_rules = "\n".join(
        f"    (subpath {_scheme_string(path)})" for path in read_paths
    )
    profile = f"""(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow mach-lookup)
(allow signal (target self))
(allow network-outbound)
(allow file-read*
{read_rules})
(allow file-write*
    (literal {_scheme_string(worktree)})
    (subpath {_scheme_string(worktree)})
    (literal {_scheme_string(scratch)})
    (subpath {_scheme_string(scratch)}))
"""
    return MacSandboxProfile(
        boundary=MACOS_SANDBOX_BOUNDARY,
        worktree=worktree,
        scratch=scratch,
        profile=profile,
    )


def seatbelt_available() -> bool:
    return sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


def build_macos_seatbelt_command(
    command: Iterable[str],
    *,
    worktree: Path,
    scratch: Path,
    readable_roots: Iterable[Path] = (),
) -> tuple[str, ...]:
    values = tuple(str(value) for value in command)
    if not values or any(not value or "\x00" in value for value in values):
        raise MacSandboxError("macOS sandbox command is invalid")
    if not seatbelt_available():
        raise MacSandboxError("macOS sandbox-exec is unavailable")
    policy = build_macos_seatbelt_profile(
        worktree=worktree,
        scratch=scratch,
        readable_roots=readable_roots,
    )
    return (
        "/usr/bin/sandbox-exec",
        "-p",
        policy.profile,
        *values,
    )


__all__ = [
    "MACOS_SANDBOX_BOUNDARY",
    "MacSandboxError",
    "MacSandboxProfile",
    "build_macos_seatbelt_command",
    "build_macos_seatbelt_profile",
    "seatbelt_available",
]
