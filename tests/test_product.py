from __future__ import annotations

import json

import pytest

from peerbridge_mcp.product import (
    ProductConfigError,
    capability_manifest,
    capability_status,
    set_update_channel,
    update_channel,
)


def test_capability_manifest_keeps_local_core_open_and_managed_hooks_dormant(
    tmp_path,
) -> None:
    manifest = capability_manifest(tmp_path)
    capabilities = {item["capability_id"]: item for item in manifest["capabilities"]}
    assert manifest["commercial_services_active"] is False
    assert manifest["entitlement_provider"] == "none"
    assert capabilities["core.local"]["state"] == "available"
    assert capabilities["core.local"]["requires_commercial_entitlement"] is False
    assert capabilities["remote.experimental.self_hosted"]["state"] == "experimental"
    assert capabilities["remote.experimental.self_hosted"]["default_enabled"] is False
    assert capabilities["managed.remote"]["state"] == "unavailable"
    assert capabilities["managed.remote"]["requires_commercial_entitlement"] is True


def test_update_channel_is_explicit_and_fails_closed_on_bad_configuration(tmp_path) -> None:
    assert update_channel(tmp_path) == "stable"
    assert set_update_channel(tmp_path, "beta")["update_channel"] == "beta"
    assert update_channel(tmp_path) == "beta"
    config = tmp_path / ".peerbridge" / "product.json"
    config.write_text(json.dumps({"schema": "wrong", "update_channel": "stable"}))
    with pytest.raises(ProductConfigError):
        update_channel(tmp_path)
    with pytest.raises(ProductConfigError):
        set_update_channel(tmp_path, "nightly")


def test_unknown_capability_is_rejected(tmp_path) -> None:
    with pytest.raises(ProductConfigError):
        capability_status(tmp_path, "managed.magic")
