"""Local-only, explicit opt-in aggregate analytics with no network sender."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .product import update_channel

CONSENT_SCHEMA = "peerbridge.telemetry-consent.v1"
EXPORT_SCHEMA = "peerbridge.telemetry-export.v1"
EVENT_SCHEMA = "peerbridge.telemetry.v1"

_EVENT_DIMENSIONS: dict[str, dict[str, frozenset[str]]] = {
    "installation_activated": {},
    "installation_active": {},
    "session_started": {},
    "feature_used": {
        "feature": frozenset(
            {"local_core", "control_room", "experimental_remote"}
        )
    },
    "operation_outcome": {
        "operation": frozenset(
            {
                "mcp_request",
                "agent_dispatch",
                "room_discussion",
                "remote_control",
                "update_check",
            }
        ),
        "outcome": frozenset({"success", "failure", "cancelled"}),
    },
    "update_result": {
        "result": frozenset(
            {
                "none",
                "available",
                "installed",
                "signature_failed",
                "download_failed",
            }
        )
    },
}
_ONCE_PER_DAY_EVENTS = frozenset({"installation_activated", "installation_active"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class AnalyticsError(ValueError):
    """Telemetry configuration or event data is invalid."""


def _home_directory() -> Path:
    try:
        return Path.home()
    except (OSError, RuntimeError) as exc:
        raise AnalyticsError(
            "analytics state location is unavailable without a home directory"
        ) from exc


def _default_state_root(environ: Mapping[str, str]) -> Path:
    """Return one per-user analytics location without creating it."""
    override = str(environ.get("PEERBRIDGE_ANALYTICS_HOME", "")).strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser().resolve()
    if os.name == "nt":
        base = str(environ.get("LOCALAPPDATA", "")).strip()
        if base:
            return (Path(base) / "PeerBridge" / "analytics").resolve()
        home = _home_directory()
        return (home / "AppData" / "Local" / "PeerBridge" / "analytics").resolve()
    if sys.platform == "darwin":
        return (
            _home_directory()
            / "Library"
            / "Application Support"
            / "PeerBridge"
            / "analytics"
        ).resolve()
    base = str(environ.get("XDG_STATE_HOME", "")).strip()
    if base:
        return (Path(base) / "peerbridge" / "analytics").resolve()
    return (
        _home_directory() / ".local" / "state" / "peerbridge" / "analytics"
    ).resolve()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _os_family() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def _arch() -> str:
    if os.name == "nt":
        value = str(
            os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE")
            or ""
        ).strip().lower()
    else:
        try:
            value = os.uname().machine.strip().lower()
        except AttributeError:
            value = ""
    if value in {"amd64", "x86_64"}:
        return "x86_64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    return "other"


def _blocked_by_environment(environ: Mapping[str, str]) -> bool:
    return (
        str(environ.get("DNT", "")).strip().lower() in _TRUTHY
        or str(environ.get("DO_NOT_TRACK", "")).strip().lower() in _TRUTHY
        or str(environ.get("PEERBRIDGE_TELEMETRY", "")).strip() == "0"
    )


def _environment_opt_in(environ: Mapping[str, str]) -> bool:
    return str(environ.get("PEERBRIDGE_TELEMETRY", "")).strip() == "1"


def _validate_dimensions(event: str, dimensions: Mapping[str, str]) -> dict[str, str]:
    allowed = _EVENT_DIMENSIONS.get(event)
    if allowed is None:
        raise AnalyticsError("unknown telemetry event")
    if set(dimensions) != set(allowed):
        raise AnalyticsError("telemetry event dimensions do not match the public schema")
    normalized: dict[str, str] = {}
    for key, values in allowed.items():
        value = str(dimensions[key]).strip()
        if value not in values:
            raise AnalyticsError(f"unsupported telemetry dimension value for {key}")
        normalized[key] = value
    return normalized


class AnalyticsStore:
    """Store privacy-bounded daily counters on the local machine only."""

    def __init__(
        self,
        project_root: Path,
        *,
        environ: Mapping[str, str] | None = None,
        state_root: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.environ = dict(os.environ if environ is None else environ)
        self.state_root = (
            state_root.resolve()
            if state_root is not None
            else _default_state_root(self.environ)
        )
        self.consent_path = self.state_root / "consent.json"
        self.db_path = self.state_root / "daily.sqlite3"

    def _ensure_private_state_root(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            os.chmod(self.state_root, 0o700)

    @staticmethod
    def _ensure_private_file(path: Path) -> None:
        with suppress(OSError):
            os.chmod(path, 0o600)

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self._ensure_private_state_root()
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._ensure_private_file(temporary)
        os.replace(temporary, path)
        self._ensure_private_file(path)

    def _read_consent(self) -> dict[str, Any] | None:
        if not self.consent_path.exists():
            return None
        try:
            payload = json.loads(self.consent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AnalyticsError("telemetry consent is not valid JSON") from exc
        required = {
            "schema",
            "enabled",
            "installation_id",
            "created_utc",
            "consent_source",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise AnalyticsError("telemetry consent contains unsupported fields")
        if payload.get("schema") != CONSENT_SCHEMA:
            raise AnalyticsError("telemetry consent has an unsupported schema")
        if not isinstance(payload.get("enabled"), bool):
            raise AnalyticsError("telemetry consent enabled flag is invalid")
        installation_id = payload.get("installation_id")
        if payload["enabled"]:
            if not isinstance(installation_id, str) or len(installation_id) != 32:
                raise AnalyticsError("telemetry installation ID is invalid")
            try:
                int(installation_id, 16)
            except ValueError as exc:
                raise AnalyticsError("telemetry installation ID is invalid") from exc
        elif installation_id is not None:
            raise AnalyticsError("disabled telemetry consent must not retain an ID")
        if payload.get("consent_source") not in {"cli", "environment", "disabled"}:
            raise AnalyticsError("telemetry consent source is invalid")
        return payload

    def _new_consent(self, source: str) -> dict[str, Any]:
        now = _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
        payload = {
            "schema": CONSENT_SCHEMA,
            "enabled": True,
            "installation_id": secrets.token_hex(16),
            "created_utc": now,
            "consent_source": source,
        }
        self._atomic_write_json(self.consent_path, payload)
        return payload

    def _effective_consent(self, *, create_from_environment: bool) -> dict[str, Any] | None:
        if _blocked_by_environment(self.environ):
            return None
        consent = self._read_consent()
        if consent is not None:
            return consent if consent["enabled"] else None
        if create_from_environment and _environment_opt_in(self.environ):
            return self._new_consent("environment")
        return None

    def _connect(self) -> sqlite3.Connection:
        self._ensure_private_state_root()
        connection = sqlite3.connect(self.db_path)
        self._ensure_private_file(self.db_path)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS daily_aggregates (
                   utc_date TEXT NOT NULL,
                   installation_id TEXT NOT NULL,
                   app_version TEXT NOT NULL,
                   os_family TEXT NOT NULL,
                   arch TEXT NOT NULL,
                   update_channel TEXT NOT NULL,
                   event TEXT NOT NULL,
                   dimensions_json TEXT NOT NULL,
                   count INTEGER NOT NULL,
                   PRIMARY KEY(
                       utc_date, installation_id, app_version, os_family, arch,
                       update_channel, event, dimensions_json
                   )
               )"""
        )
        return connection

    def enable(self) -> dict[str, Any]:
        if _blocked_by_environment(self.environ):
            raise AnalyticsError("telemetry is disabled by an environment override")
        current = self._read_consent()
        if current is None or not current["enabled"]:
            self._new_consent("cli")
        self.record("installation_activated")
        return self.status()

    def disable(self) -> dict[str, Any]:
        now = _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
        payload = {
            "schema": CONSENT_SCHEMA,
            "enabled": False,
            "installation_id": None,
            "created_utc": now,
            "consent_source": "disabled",
        }
        self._atomic_write_json(self.consent_path, payload)
        if self.db_path.exists():
            self.db_path.unlink()
        return self.status()

    def reset(self) -> dict[str, Any]:
        if _blocked_by_environment(self.environ):
            raise AnalyticsError("telemetry is disabled by an environment override")
        current = self._read_consent()
        if current is None or not current["enabled"]:
            raise AnalyticsError("telemetry must be enabled before it can be reset")
        if self.db_path.exists():
            self.db_path.unlink()
        self._new_consent("cli")
        self.record("installation_activated")
        return self.status()

    def record(self, event: str, dimensions: Mapping[str, str] | None = None) -> dict[str, Any]:
        normalized_dimensions = _validate_dimensions(event, dimensions or {})
        consent = self._effective_consent(create_from_environment=True)
        if consent is None:
            return {"recorded": False, "reason": "disabled"}
        date = _utc_now().date().isoformat()
        dimensions_json = json.dumps(
            normalized_dimensions, separators=(",", ":"), sort_keys=True
        )
        row = (
            date,
            consent["installation_id"],
            __version__,
            _os_family(),
            _arch(),
            update_channel(self.project_root),
            event,
            dimensions_json,
        )
        with closing(self._connect()) as connection:
            if event in _ONCE_PER_DAY_EVENTS:
                connection.execute(
                    """INSERT OR IGNORE INTO daily_aggregates(
                           utc_date, installation_id, app_version, os_family, arch,
                           update_channel, event, dimensions_json, count
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    row,
                )
            else:
                connection.execute(
                    """INSERT INTO daily_aggregates(
                           utc_date, installation_id, app_version, os_family, arch,
                           update_channel, event, dimensions_json, count
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(
                           utc_date, installation_id, app_version, os_family, arch,
                           update_channel, event, dimensions_json
                       ) DO UPDATE SET count=count+1""",
                    row,
                )
            connection.commit()
        return {"recorded": True, "event": event, "utc_date": date}

    def _rows(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT utc_date, installation_id, app_version, os_family, arch,
                          update_channel, event, dimensions_json, count
                     FROM daily_aggregates
                 ORDER BY utc_date, event, dimensions_json"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["dimensions"] = json.loads(item.pop("dimensions_json"))
            result.append(item)
        return result

    def export(self) -> dict[str, Any]:
        return {
            "schema": EXPORT_SCHEMA,
            "event_schema": EVENT_SCHEMA,
            "network_transport": "disabled",
            "aggregates": self._rows(),
        }

    def status(self) -> dict[str, Any]:
        consent = self._read_consent()
        environment_blocked = _blocked_by_environment(self.environ)
        effective = bool(consent and consent["enabled"] and not environment_blocked)
        rows = self._rows() if consent and consent["enabled"] else []
        return {
            "schema": CONSENT_SCHEMA,
            "enabled": effective,
            "configured": consent is not None,
            "environment_blocked": environment_blocked,
            "installation_id": consent["installation_id"] if effective else None,
            "created_utc": consent["created_utc"] if consent else None,
            "network_transport": "disabled",
            "endpoint": None,
            "aggregate_rows": len(rows),
            "aggregate_count": sum(int(row["count"]) for row in rows),
        }


def record_launch(project_root: Path, feature: str) -> bool:
    """Record a coarse launch only when the operator already opted in."""
    try:
        store = AnalyticsStore(project_root)
        results = (
            store.record("installation_active"),
            store.record("session_started"),
            store.record("feature_used", {"feature": feature}),
        )
    except (AnalyticsError, OSError, sqlite3.Error, RuntimeError, ValueError):
        return False
    return any(bool(result.get("recorded")) for result in results)
