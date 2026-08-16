"""Public product boundaries for open-core and dormant managed capabilities."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

CAPABILITY_SCHEMA = "peerbridge.capabilities.v1"
PRODUCT_CONFIG_SCHEMA = "peerbridge.product-config.v1"
UPDATE_CHANNELS = frozenset({"stable", "beta", "experimental"})


class ProductConfigError(ValueError):
    """A product-boundary configuration is malformed or unsupported."""


_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "capability_id": "core.local",
        "state": "available",
        "experimental": False,
        "default_enabled": True,
        "requires_activation": False,
        "requires_commercial_entitlement": False,
        "delivery": "open-source-local",
    },
    {
        "capability_id": "telemetry.local_opt_in",
        "state": "available",
        "experimental": True,
        "default_enabled": False,
        "requires_activation": True,
        "requires_commercial_entitlement": False,
        "delivery": "open-source-local",
    },
    {
        "capability_id": "remote.experimental.self_hosted",
        "state": "experimental",
        "experimental": True,
        "default_enabled": False,
        "requires_activation": True,
        "requires_commercial_entitlement": False,
        "delivery": "open-source-self-hosted",
    },
    {
        "capability_id": "updates.signed",
        "state": "unavailable",
        "experimental": True,
        "default_enabled": False,
        "requires_activation": True,
        "requires_commercial_entitlement": False,
        "delivery": "future-open-interface",
    },
    {
        "capability_id": "managed.remote",
        "state": "unavailable",
        "experimental": True,
        "default_enabled": False,
        "requires_activation": True,
        "requires_commercial_entitlement": True,
        "delivery": "future-managed-service",
    },
    {
        "capability_id": "managed.sync",
        "state": "unavailable",
        "experimental": True,
        "default_enabled": False,
        "requires_activation": True,
        "requires_commercial_entitlement": True,
        "delivery": "future-managed-service",
    },
    {
        "capability_id": "managed.mobile_push",
        "state": "unavailable",
        "experimental": True,
        "default_enabled": False,
        "requires_activation": True,
        "requires_commercial_entitlement": True,
        "delivery": "future-managed-service",
    },
    {
        "capability_id": "managed.support",
        "state": "unavailable",
        "experimental": True,
        "default_enabled": False,
        "requires_activation": True,
        "requires_commercial_entitlement": True,
        "delivery": "future-managed-service",
    },
)


def _config_path(project_root: Path) -> Path:
    return project_root.resolve() / ".peerbridge" / "product.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def update_channel(project_root: Path) -> str:
    path = _config_path(project_root)
    if not path.exists():
        return "stable"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductConfigError("product configuration is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != PRODUCT_CONFIG_SCHEMA:
        raise ProductConfigError("product configuration has an unsupported schema")
    channel = payload.get("update_channel")
    if channel not in UPDATE_CHANNELS:
        raise ProductConfigError("product configuration has an invalid update channel")
    if set(payload) != {"schema", "update_channel"}:
        raise ProductConfigError("product configuration contains unsupported fields")
    return str(channel)


def set_update_channel(project_root: Path, channel: str) -> dict[str, Any]:
    normalized = str(channel).strip().lower()
    if normalized not in UPDATE_CHANNELS:
        raise ProductConfigError("update channel must be stable, beta or experimental")
    payload = {"schema": PRODUCT_CONFIG_SCHEMA, "update_channel": normalized}
    _atomic_write_json(_config_path(project_root), payload)
    return payload


def capability_manifest(project_root: Path) -> dict[str, Any]:
    return {
        "schema": CAPABILITY_SCHEMA,
        "update_channel": update_channel(project_root),
        "entitlement_provider": "none",
        "commercial_services_active": False,
        "capabilities": [dict(item) for item in _CAPABILITIES],
    }


def capability_status(project_root: Path, capability_id: str) -> dict[str, Any]:
    normalized = str(capability_id).strip()
    for item in capability_manifest(project_root)["capabilities"]:
        if item["capability_id"] == normalized:
            return item
    raise ProductConfigError("unknown capability ID")
