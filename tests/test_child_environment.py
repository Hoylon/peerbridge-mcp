from __future__ import annotations

import os
from pathlib import Path

import pytest

from peerbridge_mcp.child_environment import (
    build_agent_child_environment,
    build_local_child_environment,
)


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    hostile = tmp_path / "hostile-bin"
    if os.name == "nt":
        system_root = tmp_path / "Windows"
        allowed = system_root / "System32"
        values = {
            "SYSTEMROOT": str(system_root),
            "WINDIR": str(system_root),
            "USERPROFILE": str(tmp_path / "home"),
            "LOCALAPPDATA": str(tmp_path / "local"),
            "APPDATA": str(tmp_path / "roaming"),
            "PROGRAMFILES": str(tmp_path / "Program Files"),
            "PATH": os.pathsep.join((str(hostile), str(allowed))),
        }
    else:
        allowed = Path("/usr/bin")
        values = {
            "HOME": str(tmp_path / "home"),
            "PATH": os.pathsep.join((str(hostile), str(allowed))),
        }
    return values, allowed.resolve(), hostile.resolve()


def test_local_child_path_excludes_unreviewed_entries(tmp_path: Path) -> None:
    values, allowed, hostile = _environment(tmp_path)

    selected = build_local_child_environment(values)

    assert selected["PATH"] == str(allowed)
    assert str(hostile) not in selected["PATH"]


def test_agent_environment_uses_exact_auth_allowlist(tmp_path: Path) -> None:
    values, _allowed, _hostile = _environment(tmp_path)
    values.update(
        {
            "OPENAI_API_KEY": "unit-test-provider-secret",
            "OPENAI_SECRET_ARCHIVE": "must-not-pass",
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "ANTHROPIC_API_KEY": "unit-test-provider-secret",
            "GITHUB_TOKEN": "unit-test-provider-secret",
        }
    )

    selected = build_agent_child_environment("codex", values)

    assert selected["OPENAI_API_KEY"] == "unit-test-provider-secret"
    assert selected["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert "OPENAI_SECRET_ARCHIVE" not in selected
    assert "ANTHROPIC_API_KEY" not in selected
    assert "GITHUB_TOKEN" not in selected


def test_unknown_agent_environment_family_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported Agent environment family"):
        build_agent_child_environment("attacker")
