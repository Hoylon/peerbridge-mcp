"""Build a minimal environment for external Agent runtimes.

PeerBridge launches provider-owned processes, so they still need ordinary OS
paths and their own authentication family. They must not inherit every secret
held by the desktop process.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit


_BASE_KEYS = frozenset(
    {
        "ALL_PROXY",
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TZ",
        "USERPROFILE",
        "WINDIR",
        "__PYVENV_LAUNCHER__",
    }
)

_PROVIDER_KEYS: Mapping[str, frozenset[str]] = {
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
        }
    ),
    "codex": frozenset(
        {
            "CODEX_HOME",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT_ID",
        }
    ),
    "grok-build": frozenset(
        {
            "GROK_API_KEY",
            "GROK_BASE_URL",
            "GROK_HOME",
            "XAI_API_KEY",
            "XAI_BASE_URL",
        }
    ),
    "kimi": frozenset(
        {
            "KIMI_API_KEY",
            "KIMI_BASE_URL",
            "MOONSHOT_API_KEY",
            "MOONSHOT_BASE_URL",
        }
    ),
    "kimi-code": frozenset(
        {
            "KIMI_API_KEY",
            "KIMI_BASE_URL",
            "MOONSHOT_API_KEY",
            "MOONSHOT_BASE_URL",
        }
    ),
}


def _environment_path(values: Mapping[str, str], name: str, *parts: str) -> Path | None:
    value = values.get(name)
    return Path(str(value), *parts).resolve() if value else None


def _trusted_path_roots(values: Mapping[str, str]) -> tuple[Path, ...]:
    roots: list[Path | None]
    if os.name == "nt":
        roots = [
            _environment_path(values, "SYSTEMROOT", "System32"),
            _environment_path(values, "SYSTEMROOT", "System32", "Wbem"),
            _environment_path(values, "SYSTEMROOT", "System32", "WindowsPowerShell", "v1.0"),
            _environment_path(values, "SYSTEMROOT", "System32", "OpenSSH"),
            _environment_path(values, "PROGRAMFILES", "nodejs"),
            _environment_path(values, "PROGRAMFILES", "Git", "cmd"),
            _environment_path(values, "PROGRAMFILES", "Git", "bin"),
            _environment_path(values, "LOCALAPPDATA", "Microsoft", "WindowsApps"),
            _environment_path(values, "LOCALAPPDATA", "Microsoft", "WinGet", "Links"),
            _environment_path(values, "LOCALAPPDATA", "Programs", "OpenAI", "Codex", "bin"),
            _environment_path(values, "APPDATA", "npm"),
            _environment_path(values, "USERPROFILE", ".covs", "npm-global"),
            _environment_path(values, "USERPROFILE", ".local", "bin"),
            _environment_path(values, "USERPROFILE", ".grok", "bin"),
        ]
    else:
        roots = [
            Path("/bin"),
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            _environment_path(values, "HOME", ".local", "bin"),
            _environment_path(values, "HOME", ".npm-global", "bin"),
        ]
    return tuple(root for root in roots if root is not None)


def _sanitized_path(
    values: Mapping[str, str],
    *,
    required_roots: Iterable[Path] = (),
) -> str:
    """Keep PATH ordering but retain only exact reviewed executable directories."""

    roots = _trusted_path_roots(values)
    allowed = {os.path.normcase(str(root.resolve())) for root in roots}
    selected: list[str] = []
    for raw in str(values.get("PATH") or "").split(os.pathsep):
        if not raw.strip():
            continue
        resolved = str(Path(raw).resolve())
        normalized = os.path.normcase(resolved)
        if normalized in allowed and normalized not in {
            os.path.normcase(item) for item in selected
        }:
            selected.append(resolved)
    selected_normalized = {os.path.normcase(item) for item in selected}
    for root in required_roots:
        resolved = str(Path(root).resolve())
        normalized = os.path.normcase(resolved)
        trusted_descendant = False
        for candidate in allowed:
            try:
                if os.path.commonpath((normalized, candidate)) == candidate:
                    trusted_descendant = True
                    break
            except ValueError:
                continue
        if not trusted_descendant:
            raise ValueError("required child executable root is not trusted")
        if not Path(resolved).is_dir():
            raise ValueError("required child executable root is unavailable")
        if normalized not in selected_normalized:
            selected.append(resolved)
            selected_normalized.add(normalized)
    if not selected:
        selected = [str(root) for root in roots if root.is_dir()]
    return os.pathsep.join(selected)


def build_local_child_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return only OS essentials for a trusted local PeerBridge child."""

    values = os.environ if source is None else source
    selected: dict[str, str] = {}
    for key, value in values.items():
        normalized = str(key).upper()
        if normalized not in _BASE_KEYS:
            continue
        text = str(value)
        if normalized in {"ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY"}:
            try:
                parsed = urlsplit(text)
            except ValueError:
                continue
            if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
                continue
        selected[str(key)] = text
    if os.name == "nt" and not selected.get("HOME"):
        user_profile = str(values.get("USERPROFILE") or "").strip()
        if user_profile:
            selected["HOME"] = user_profile
    sanitized_path = _sanitized_path(values)
    if sanitized_path:
        selected["PATH"] = sanitized_path
    return selected


def build_agent_child_environment(
    provider_family: str,
    source: Mapping[str, str] | None = None,
    *,
    required_path_roots: Iterable[Path] = (),
    include_provider_credentials: bool = False,
) -> dict[str, str]:
    """Return OS essentials and only explicitly requested provider credentials."""

    family = str(provider_family or "").strip().lower()
    if family not in _PROVIDER_KEYS:
        raise ValueError("unsupported Agent environment family")
    values = os.environ if source is None else source
    allowed_keys = _PROVIDER_KEYS[family]
    selected = build_local_child_environment(values)
    sanitized_path = _sanitized_path(values, required_roots=required_path_roots)
    if sanitized_path:
        selected["PATH"] = sanitized_path
    if include_provider_credentials:
        for key, value in values.items():
            normalized = str(key).upper()
            if normalized in allowed_keys:
                selected[str(key)] = str(value)
    if family == "grok-build" and not selected.get("GROK_HOME"):
        user_profile = str(values.get("USERPROFILE") or "").strip()
        cached_home = Path(user_profile, ".grok").resolve() if user_profile else None
        if cached_home is not None and cached_home.is_dir():
            selected["GROK_HOME"] = str(cached_home)
    return selected
