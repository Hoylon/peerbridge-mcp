"""Safe, read-only discovery and explicit handoff to the official CC Switch CLI."""

from __future__ import annotations

import os
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_CLI_RELATIVE_CANDIDATES = (
    Path("Programs/CC Switch CLI/cc-switch.exe"),
    Path("Programs/CC Switch/cc-switch.exe"),
)
_APP_RELATIVE_CANDIDATES = (
    Path("Programs/CC Switch/cc-switch.exe"),
)
_TABLE_ROW = re.compile(r"^│\s*(?P<current>✓?)\s*┆\s*(?P<id>.*?)\s*┆\s*(?P<name>.*?)\s*┆\s*(?P<url>.*?)\s*│$")
_MODEL_ROW = re.compile(r"^│\s*\d+\s*┆\s*(?P<model>.*?)\s*│$")

# Mirrors the application values exposed by the public ``cc-switch --help``
# contract. Keep this explicit so an unexpected CLI update cannot silently
# widen which local application configuration PeerBridge may switch.
SUPPORTED_APPS = (
    "claude",
    "codex",
    "gemini",
    "open-code",
    "hermes",
    "open-claw",
)


class CcSwitchError(RuntimeError):
    """A redacted CC Switch integration error."""


@dataclass(frozen=True)
class CcSwitchProvider:
    app: str
    provider_id: str
    name: str
    current: bool
    has_endpoint: bool


@dataclass(frozen=True)
class CcSwitchRouteIdentity:
    app: str
    route_class: str
    provider_id: str
    provider_name: str
    model_id: str
    current: bool
    identity_sha256: str


def _validated_app(app: str) -> str:
    value = str(app or "").strip().lower()
    if value not in SUPPORTED_APPS:
        raise CcSwitchError("unsupported CC Switch application")
    return value


def _local_appdata_roots() -> tuple[Path, ...]:
    """Return available per-user install roots without requiring a desktop home."""
    roots: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        roots.append(Path(local_appdata))
    try:
        roots.append(Path.home() / "AppData/Local")
    except (OSError, RuntimeError):
        pass
    return tuple(dict.fromkeys(roots))


def _installation_candidates(relative_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(
        root / relative_path
        for root in _local_appdata_roots()
        for relative_path in relative_paths
    )


def find_cli() -> Path | None:
    return next(
        (
            path
            for path in _installation_candidates(_CLI_RELATIVE_CANDIDATES)
            if path.is_file()
        ),
        None,
    )


def find_app() -> Path | None:
    return next(
        (
            path
            for path in _installation_candidates(_APP_RELATIVE_CANDIDATES)
            if path.is_file()
        ),
        None,
    )


def _run(args: list[str], *, timeout: int = 20) -> str:
    cli = find_cli()
    if not cli:
        raise CcSwitchError("CC Switch CLI is not installed")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            [str(cli), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CcSwitchError("CC Switch CLI timed out") from None
    except OSError:
        raise CcSwitchError("CC Switch CLI could not be started") from None
    if completed.returncode:
        raise CcSwitchError(f"CC Switch CLI failed ({completed.returncode})")
    return completed.stdout


def list_providers(app: str) -> tuple[CcSwitchProvider, ...]:
    """Read provider identities without exporting provider configuration or credentials."""
    app = _validated_app(app)
    output = _run(["provider", "list", "-a", app])
    providers: list[CcSwitchProvider] = []
    for line in output.splitlines():
        match = _TABLE_ROW.match(line.strip())
        if not match:
            continue
        provider_id = match.group("id").strip()
        name = match.group("name").strip()
        endpoint = match.group("url").strip()
        if not provider_id or provider_id == "ID":
            continue
        providers.append(
            CcSwitchProvider(
                app=app,
                provider_id=provider_id,
                name=name,
                current=bool(match.group("current").strip()),
                has_endpoint=endpoint not in {"", "N/A"},
            )
        )
    return tuple(providers)


def open_app() -> None:
    app = find_app()
    if not app:
        raise CcSwitchError("CC Switch desktop app is not installed")
    os.startfile(str(app))


def fetch_models(app: str, provider_id: str) -> tuple[str, ...]:
    """Ask CC Switch to use its saved credential and return model IDs only."""
    app = _validated_app(app)
    if not re.fullmatch(r"[^\r\n\x00]{1,200}", str(provider_id or "")):
        raise CcSwitchError("invalid CC Switch provider ID")
    output = _run(
        ["provider", "fetch-models", "-a", app, provider_id],
        timeout=60,
    )
    models = []
    for line in output.splitlines():
        match = _MODEL_ROW.match(line.strip())
        if match:
            model = match.group("model").strip()
            if model and model != "Model":
                models.append(model)
    return tuple(dict.fromkeys(models))


def switch_provider(app: str, provider_id: str) -> None:
    """Use the public CLI for an explicit switch; never access CC Switch's database."""
    app = _validated_app(app)
    if not re.fullmatch(r"[^\r\n\x00]{1,200}", str(provider_id or "")):
        raise CcSwitchError("invalid CC Switch provider ID")
    _run(["provider", "switch", "-a", app, provider_id])


def resolve_route_identity(
    *,
    app: str,
    route_class: str,
    provider_id: str,
    model_id: str,
) -> CcSwitchRouteIdentity:
    """Bind one redacted CC Switch provider/model selection without exporting config."""
    route = str(route_class or "").strip().lower()
    if route not in {"official", "relay", "local"}:
        raise CcSwitchError("invalid route class")
    provider_matches = [
        provider
        for provider in list_providers(app)
        if provider.provider_id == provider_id
    ]
    if len(provider_matches) != 1:
        raise CcSwitchError("CC Switch provider identity is missing or ambiguous")
    model = str(model_id or "").strip()
    if not re.fullmatch(r"[^\r\n\x00]{1,500}", model):
        raise CcSwitchError("invalid CC Switch model ID")
    if model not in fetch_models(app, provider_id):
        raise CcSwitchError("CC Switch provider does not advertise the requested model")
    provider = provider_matches[0]
    if route != "relay":
        raise CcSwitchError(
            "CC Switch discovery is intermediary evidence and must remain relay class"
        )
    identity = {
        "app": app,
        "route_class": route,
        "provider_id": provider.provider_id,
        "provider_name": provider.name,
        "model_id": model,
        "current": provider.current,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return CcSwitchRouteIdentity(**identity, identity_sha256=digest)
