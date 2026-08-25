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
) -> MacSandboxProfile:
    """Return a minimal write-denial profile for one governed worktree.

    This defense-in-depth contract deliberately allows reads. Agent-level read
    authority remains governed by the selected provider permission tier; the
    Seatbelt layer prevents writes outside the approved worktree and scratch.
    """

    try:
        worktree = Path(worktree).resolve(strict=True)
        scratch = Path(scratch).resolve(strict=True)
    except OSError as exc:
        raise MacSandboxError("macOS sandbox directory is unavailable") from exc
    if not worktree.is_dir() or not scratch.is_dir():
        raise MacSandboxError("macOS sandbox directory is unavailable")
    profile = f"""(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow network-outbound)
(allow file-read*)
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
) -> tuple[str, ...]:
    values = tuple(str(value) for value in command)
    if not values or any(not value or "\x00" in value for value in values):
        raise MacSandboxError("macOS sandbox command is invalid")
    if not seatbelt_available():
        raise MacSandboxError("macOS sandbox-exec is unavailable")
    policy = build_macos_seatbelt_profile(
        worktree=worktree,
        scratch=scratch,
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
