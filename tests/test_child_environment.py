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
        allowed = tmp_path / "home" / ".local" / "bin"
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
            "OPENAI_SECRET_ARCHIVE": "unit-test-provider-secret",
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "ANTHROPIC_API_KEY": "unit-test-provider-secret",
            "GITHUB_TOKEN": "unit-test-provider-secret",
        }
    )

    selected = build_agent_child_environment(
        "codex", values, include_provider_credentials=True
    )

    assert selected["OPENAI_API_KEY"] == "unit-test-provider-secret"
    assert selected["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert "OPENAI_SECRET_ARCHIVE" not in selected
    assert "ANTHROPIC_API_KEY" not in selected
    assert "GITHUB_TOKEN" not in selected


def test_agent_environment_can_explicitly_exclude_provider_credentials(
    tmp_path: Path,
) -> None:
    values, _allowed, _hostile = _environment(tmp_path)
    values.update(
        {
            "ANTHROPIC_API_KEY": "unit-test-provider-secret",
            "ANTHROPIC_BASE_URL": "https://stale-relay.invalid",
            "CLAUDE_CODE_USE_VERTEX": "1",
        }
    )

    selected = build_agent_child_environment(
        "claude", values, include_provider_credentials=False
    )

    assert "ANTHROPIC_API_KEY" not in selected
    assert "ANTHROPIC_BASE_URL" not in selected
    assert "CLAUDE_CODE_USE_VERTEX" not in selected


def test_agent_environment_excludes_provider_credentials_by_default(
    tmp_path: Path,
) -> None:
    values, _allowed, _hostile = _environment(tmp_path)
    values["OPENAI_API_KEY"] = "unit-test-provider-secret"
    values["OPENAI_BASE_URL"] = "https://relay.invalid"

    selected = build_agent_child_environment("codex", values)

    assert "OPENAI_API_KEY" not in selected
    assert "OPENAI_BASE_URL" not in selected


def test_kimi_code_environment_uses_kimi_only_auth_allowlist(tmp_path: Path) -> None:
    values, _allowed, _hostile = _environment(tmp_path)
    values.update(
        {
            "KIMI_API_KEY": "unit-test-provider-secret",
            "MOONSHOT_BASE_URL": "https://example.invalid",
            "OPENAI_API_KEY": "unit-test-provider-secret",
        }
    )

    selected = build_agent_child_environment(
        "kimi-code", values, include_provider_credentials=True
    )

    assert selected["KIMI_API_KEY"] == "unit-test-provider-secret"
    assert selected["MOONSHOT_BASE_URL"] == "https://example.invalid"
    assert "OPENAI_API_KEY" not in selected


def test_agent_environment_adds_required_trusted_runtime_root(tmp_path: Path) -> None:
    values, allowed, hostile = _environment(tmp_path)
    allowed.mkdir(parents=True)
    values["PATH"] = str(hostile)

    selected = build_agent_child_environment(
        "grok-build", values, required_path_roots=(allowed,)
    )

    assert selected["PATH"] == str(allowed)
    assert str(hostile) not in selected["PATH"]


def test_agent_environment_accepts_verified_runtime_subdirectory(tmp_path: Path) -> None:
    values, allowed, _hostile = _environment(tmp_path)
    runtime = allowed / "node_modules" / "publisher" / "bin"
    runtime.mkdir(parents=True)

    selected = build_agent_child_environment(
        "claude", values, required_path_roots=(runtime,)
    )

    assert str(runtime.resolve()) in selected["PATH"].split(os.pathsep)


@pytest.mark.skipif(os.name != "nt", reason="Windows environment fallback")
def test_windows_grok_environment_reuses_only_its_cached_home(tmp_path: Path) -> None:
    values, _allowed, _hostile = _environment(tmp_path)
    user_profile = Path(values["USERPROFILE"])
    grok_home = user_profile / ".grok"
    grok_home.mkdir(parents=True)

    local = build_local_child_environment(values)
    grok = build_agent_child_environment("grok-build", values)

    assert local["HOME"] == str(user_profile)
    assert grok["HOME"] == str(user_profile)
    assert grok["GROK_HOME"] == str(grok_home.resolve())
    for family in ("codex", "claude", "kimi"):
        assert "GROK_HOME" not in build_agent_child_environment(family, values)


def test_explicit_grok_home_is_provider_isolated(tmp_path: Path) -> None:
    values, _allowed, _hostile = _environment(tmp_path)
    values["GROK_HOME"] = str(tmp_path / "explicit-grok-home")

    assert build_agent_child_environment(
        "grok-build", values, include_provider_credentials=True
    )["GROK_HOME"] == values["GROK_HOME"]
    assert "GROK_HOME" not in build_agent_child_environment("codex", values)


def test_local_child_environment_drops_proxy_urls_with_credentials(
    tmp_path: Path,
) -> None:
    values, _allowed, _hostile = _environment(tmp_path)
    values.update(
        {
            "HTTPS_PROXY": "https://user:password@proxy.example:8443",
            "HTTP_PROXY": "http://proxy.example:8080",
            "ALL_PROXY": "not-a-url",
        }
    )

    selected = build_local_child_environment(values)

    assert "HTTPS_PROXY" not in selected
    assert selected["HTTP_PROXY"] == "http://proxy.example:8080"
    assert "ALL_PROXY" not in selected


def test_agent_environment_rejects_untrusted_required_runtime_root(
    tmp_path: Path,
) -> None:
    values, _allowed, hostile = _environment(tmp_path)
    hostile.mkdir(parents=True)

    with pytest.raises(ValueError, match="not trusted"):
        build_agent_child_environment(
            "grok-build", values, required_path_roots=(hostile,)
        )


def test_unknown_agent_environment_family_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported Agent environment family"):
        build_agent_child_environment("attacker")
