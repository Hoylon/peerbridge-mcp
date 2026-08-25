from __future__ import annotations

import subprocess
import shlex
import sys
from pathlib import Path

import pytest

from peerbridge_mcp.macos_sandbox import (
    MACOS_SANDBOX_BOUNDARY,
    MacSandboxError,
    build_macos_seatbelt_command,
    build_macos_seatbelt_profile,
)


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS Seatbelt contract runs on the macos-14 CI worker",
)


def test_profile_allows_only_governed_write_roots(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    scratch = tmp_path / "scratch"
    worktree.mkdir()
    scratch.mkdir()

    policy = build_macos_seatbelt_profile(worktree=worktree, scratch=scratch)

    assert policy.boundary == MACOS_SANDBOX_BOUNDARY
    assert f"(subpath \"{worktree.resolve()}\")" in policy.profile
    assert f"(subpath \"{scratch.resolve()}\")" in policy.profile
    assert "(deny default)" in policy.profile
    assert "(allow network-outbound)" in policy.profile
    assert "(allow file-read-metadata" in policy.profile
    assert '(literal "/")' in policy.profile
    for parent in worktree.resolve().parents:
        assert f'(literal "{parent}")' in policy.profile
    assert "(allow file-write*\n" in policy.profile
    assert "(allow file-write*)" not in policy.profile
    assert f"(literal \"{worktree.resolve()}\")" in policy.profile
    assert f"(literal \"{scratch.resolve()}\")" in policy.profile
    assert str(tmp_path / "outside") not in policy.profile


def test_profile_escapes_paths_and_rejects_missing_directories(tmp_path: Path) -> None:
    quoted = tmp_path / 'quote"dir'
    scratch = tmp_path / "scratch"
    quoted.mkdir()
    scratch.mkdir()
    policy = build_macos_seatbelt_profile(worktree=quoted, scratch=scratch)
    assert '\\"' in policy.profile

    with pytest.raises(MacSandboxError, match="unavailable"):
        build_macos_seatbelt_profile(
            worktree=tmp_path / "missing",
            scratch=scratch,
        )


def test_live_seatbelt_denies_write_outside_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    scratch = tmp_path / "scratch"
    outside = tmp_path / "outside"
    worktree.mkdir()
    scratch.mkdir()
    outside.mkdir()
    inside_file = worktree / "inside.txt"
    outside_file = outside / "outside.txt"
    inside_command = build_macos_seatbelt_command(
        ("/bin/sh", "-c", f"printf pass > {shlex.quote(str(inside_file))}"),
        worktree=worktree,
        scratch=scratch,
    )
    outside_command = build_macos_seatbelt_command(
        ("/bin/sh", "-c", f"printf blocked > {shlex.quote(str(outside_file))}"),
        worktree=worktree,
        scratch=scratch,
    )

    inside = subprocess.run(
        inside_command, capture_output=True, timeout=20, check=False
    )
    outside = subprocess.run(
        outside_command, capture_output=True, timeout=20, check=False
    )

    assert inside.returncode == 0, inside.stderr.decode(errors="replace")
    assert inside_file.read_text(encoding="utf-8") == "pass"
    assert outside.returncode != 0
    assert not outside_file.exists()
