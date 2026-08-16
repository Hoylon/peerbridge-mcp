from __future__ import annotations

from pathlib import Path

import pytest

from peerbridge_mcp import ccswitch


PROVIDER_OUTPUT = """
┌───┬────────────────────┬─────────────────┬─────────────────────────┐
│ ✓ ┆ codex-official     ┆ OpenAI Official ┆ N/A                     │
│   ┆ relay-one          ┆ Relay One       ┆ https://relay.invalid   │
└───┴────────────────────┴─────────────────┴─────────────────────────┘
"""

MODEL_OUTPUT = """
┌────┬──────────────────┐
│ 1  ┆ gpt-5.6          │
│ 2  ┆ gpt-5.6-sol      │
│ 3  ┆ gpt-5.6-luna     │
│ 4  ┆ gpt-5.6-sol      │
└────┴──────────────────┘
"""


def test_import_safe_discovery_does_not_require_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    def unavailable_home() -> Path:
        raise RuntimeError("headless environment has no home")

    monkeypatch.setattr(Path, "home", unavailable_home)

    assert ccswitch._local_appdata_roots() == ()
    assert ccswitch.find_cli() is None
    assert ccswitch.find_app() is None


def test_discovery_uses_local_appdata_without_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Programs/CC Switch CLI/cc-switch.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"stub")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def unavailable_home() -> Path:
        raise RuntimeError("headless environment has no home")

    monkeypatch.setattr(Path, "home", unavailable_home)

    assert ccswitch.find_cli() == executable


def test_list_providers_returns_redacted_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, timeout: int = 20) -> str:
        calls.append(args)
        return PROVIDER_OUTPUT

    monkeypatch.setattr(ccswitch, "_run", fake_run)
    providers = ccswitch.list_providers("codex")

    assert calls == [["provider", "list", "-a", "codex"]]
    assert [(p.provider_id, p.name, p.current, p.has_endpoint) for p in providers] == [
        ("codex-official", "OpenAI Official", True, False),
        ("relay-one", "Relay One", False, True),
    ]
    assert all(not hasattr(provider, "endpoint") for provider in providers)


@pytest.mark.parametrize("app", ccswitch.SUPPORTED_APPS)
def test_every_public_ccswitch_application_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
    app: str,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ccswitch,
        "_run",
        lambda args, timeout=20: calls.append(args) or PROVIDER_OUTPUT,
    )

    providers = ccswitch.list_providers(app)

    assert providers
    assert calls == [["provider", "list", "-a", app]]


def test_fetch_models_uses_saved_provider_identity_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], int]] = []

    def fake_run(args: list[str], *, timeout: int = 20) -> str:
        calls.append((args, timeout))
        return MODEL_OUTPUT

    monkeypatch.setattr(ccswitch, "_run", fake_run)
    assert ccswitch.fetch_models("codex", "relay-one") == (
        "gpt-5.6",
        "gpt-5.6-sol",
        "gpt-5.6-luna",
    )
    assert calls == [
        (["provider", "fetch-models", "-a", "codex", "relay-one"], 60)
    ]


def test_switch_provider_requires_supported_app_and_safe_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ccswitch,
        "_run",
        lambda args, timeout=20: calls.append(args) or "",
    )
    ccswitch.switch_provider("claude", "provider-one")
    assert calls == [["provider", "switch", "-a", "claude", "provider-one"]]
    with pytest.raises(ccswitch.CcSwitchError):
        ccswitch.switch_provider("unknown", "provider-one")
    with pytest.raises(ccswitch.CcSwitchError):
        ccswitch.switch_provider("codex", "bad\nprovider")


def test_resolve_route_identity_binds_provider_model_and_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ccswitch,
        "_run",
        lambda args, timeout=20: PROVIDER_OUTPUT
        if args[1] == "list"
        else MODEL_OUTPUT,
    )
    identity = ccswitch.resolve_route_identity(
        app="codex",
        route_class="relay",
        provider_id="relay-one",
        model_id="gpt-5.6-sol",
    )
    assert identity.route_class == "relay"
    assert identity.provider_id == "relay-one"
    assert identity.model_id == "gpt-5.6-sol"
    assert len(identity.identity_sha256) == 64
    assert "relay.invalid" not in repr(identity)


def test_resolve_route_identity_rejects_model_or_route_confusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ccswitch,
        "_run",
        lambda args, timeout=20: PROVIDER_OUTPUT
        if args[1] == "list"
        else MODEL_OUTPUT,
    )
    with pytest.raises(ccswitch.CcSwitchError):
        ccswitch.resolve_route_identity(
            app="codex",
            route_class="local",
            provider_id="relay-one",
            model_id="missing-model",
        )
    with pytest.raises(ccswitch.CcSwitchError):
        ccswitch.resolve_route_identity(
            app="codex",
            route_class="hybrid",
            provider_id="relay-one",
            model_id="gpt-5.6",
        )
