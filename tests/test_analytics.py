from __future__ import annotations

import json

import pytest

import peerbridge_mcp.analytics as analytics_module
from peerbridge_mcp.analytics import AnalyticsError, AnalyticsStore, record_launch
from peerbridge_mcp.cli import main


def _store(tmp_path, *, environ=None) -> AnalyticsStore:
    return AnalyticsStore(
        tmp_path,
        environ={} if environ is None else environ,
        state_root=tmp_path / "analytics-state",
    )


def test_analytics_is_default_off_and_status_has_no_write_side_effect(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.status()["enabled"] is False
    assert store.record("session_started") == {
        "recorded": False,
        "reason": "disabled",
    }
    assert not store.state_root.exists()


def test_opt_in_records_only_allowlisted_daily_aggregates(tmp_path) -> None:
    store = _store(tmp_path)
    enabled = store.enable()
    assert enabled["enabled"] is True
    assert len(enabled["installation_id"]) == 32
    store.record("session_started")
    store.record("session_started")
    store.record("feature_used", {"feature": "experimental_remote"})
    exported = store.export()
    assert exported["network_transport"] == "disabled"
    assert sum(row["count"] for row in exported["aggregates"]) == 4
    assert {row["event"] for row in exported["aggregates"]} == {
        "installation_activated",
        "session_started",
        "feature_used",
    }
    serialized = json.dumps(exported)
    for forbidden in ("prompt", "api_key", "file_path", "model_output"):
        assert forbidden not in serialized


def test_unknown_event_dimensions_and_values_are_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    store.enable()
    with pytest.raises(AnalyticsError):
        store.record("feature_used", {"feature": "local_core", "room": "private"})
    with pytest.raises(AnalyticsError):
        store.record("feature_used", {"feature": "secret_feature"})
    with pytest.raises(AnalyticsError):
        store.record("custom_event", {})


def test_disable_and_dnt_override_clear_or_block_collection(tmp_path) -> None:
    store = _store(tmp_path)
    store.enable()
    store.record("session_started")
    assert store.disable()["enabled"] is False
    assert store.export()["aggregates"] == []
    assert store.record("session_started")["recorded"] is False

    blocked = AnalyticsStore(
        tmp_path / "blocked",
        environ={"DNT": "1"},
        state_root=tmp_path / "blocked-state",
    )
    with pytest.raises(AnalyticsError):
        blocked.enable()
    assert blocked.record("session_started")["recorded"] is False


def test_reset_rotates_random_installation_identity_and_clears_old_rows(tmp_path) -> None:
    store = _store(tmp_path)
    old_id = store.enable()["installation_id"]
    store.record("session_started")
    reset = store.reset()
    assert reset["installation_id"] != old_id
    events = [row["event"] for row in store.export()["aggregates"]]
    assert events == ["installation_activated"]


def test_environment_opt_in_is_explicit_but_still_local_only(tmp_path) -> None:
    store = _store(tmp_path, environ={"PEERBRIDGE_TELEMETRY": "1"})
    result = store.record("installation_active")
    assert result["recorded"] is True
    status = store.status()
    assert status["enabled"] is True
    assert status["network_transport"] == "disabled"
    assert status["endpoint"] is None


def test_analytics_and_product_cli_expose_local_status(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv(
        "PEERBRIDGE_ANALYTICS_HOME", str(tmp_path / "analytics-state")
    )
    assert main(["analytics", "--project-root", str(tmp_path), "status"]) == 0
    analytics = json.loads(capsys.readouterr().out)
    assert analytics["enabled"] is False
    assert analytics["network_transport"] == "disabled"

    assert main(["product", "--project-root", str(tmp_path), "status"]) == 0
    product = json.loads(capsys.readouterr().out)
    assert product["commercial_services_active"] is False
    assert product["update_channel"] == "stable"


def test_default_state_is_per_user_not_per_project(tmp_path) -> None:
    analytics_home = tmp_path / "user-analytics"
    environment = {"PEERBRIDGE_ANALYTICS_HOME": str(analytics_home)}
    first = AnalyticsStore(tmp_path / "project-a", environ=environment)
    second = AnalyticsStore(tmp_path / "project-b", environ=environment)

    first_id = first.enable()["installation_id"]
    assert second.status()["installation_id"] == first_id
    assert first.state_root == second.state_root == analytics_home.resolve()


def test_missing_user_home_disables_optional_launch_analytics_without_crashing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable_home() -> None:
        raise RuntimeError("home unavailable")

    monkeypatch.setattr(analytics_module.Path, "home", unavailable_home)
    with pytest.raises(AnalyticsError, match="analytics state location is unavailable"):
        AnalyticsStore(tmp_path, environ={})
    assert record_launch(tmp_path, "local_core") is False
