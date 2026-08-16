from __future__ import annotations

import json
import subprocess

import pytest

from peerbridge_mcp.codex_catalog import (
    CodexCatalogError,
    discover_codex_model_catalog,
    parse_codex_model_catalog,
)


def _catalog_payload() -> dict[str, object]:
    return {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6 Sol",
                "visibility": "list",
                "priority": 0,
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "high"},
                    {"effort": "ultra"},
                ],
            },
            {
                "slug": "gpt-5.6-terra",
                "display_name": "GPT-5.6 Terra",
                "visibility": "list",
                "priority": 1,
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "high"},
                ],
            },
            {
                "slug": "gpt-5.6-luna",
                "display_name": "GPT-5.6 Luna",
                "visibility": "list",
                "priority": 2,
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [{"effort": "high"}],
            },
            {
                "slug": "codex-auto-review",
                "display_name": "Hidden",
                "visibility": "hide",
                "priority": 99,
                "supported_reasoning_levels": [{"effort": "high"}],
            },
        ]
    }


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_parse_codex_catalog_decodes_visible_models(encoding: str) -> None:
    payload = json.dumps(_catalog_payload()).encode(encoding)

    catalog = parse_codex_model_catalog(payload)

    assert [model.model_id for model in catalog.models] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    assert catalog.models[0].supported_reasoning_modes == ("low", "high", "ultra")
    assert catalog.models[2].supported_reasoning_modes == ("high",)
    assert len(catalog.catalog_sha256) == 64


def test_parse_codex_catalog_sha_is_deterministic() -> None:
    payload = json.dumps(_catalog_payload())

    assert (
        parse_codex_model_catalog(payload).catalog_sha256
        == parse_codex_model_catalog(payload).catalog_sha256
    )


def test_parse_codex_catalog_rejects_missing_visible_models() -> None:
    with pytest.raises(CodexCatalogError, match="no visible models"):
        parse_codex_model_catalog('{"models": []}')


def test_discover_codex_catalog_uses_cli_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}
    stdout = json.dumps(_catalog_payload()).encode("utf-8")

    monkeypatch.setattr("peerbridge_mcp.codex_catalog.shutil.which", lambda name: "codex.exe")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr("peerbridge_mcp.codex_catalog.subprocess.run", fake_run)

    catalog = discover_codex_model_catalog(timeout=7)

    assert observed["command"] == ["codex.exe", "debug", "models"]
    assert "shell" not in observed
    assert observed["capture_output"] is True
    assert observed["timeout"] == 7
    assert len(catalog.models) == 3


def test_discover_codex_catalog_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("peerbridge_mcp.codex_catalog.shutil.which", lambda name: None)

    with pytest.raises(CodexCatalogError, match="not installed"):
        discover_codex_model_catalog()
