from __future__ import annotations

import json

import pytest

from peerbridge_mcp.localization import (
    LocalizationError,
    SUPPORTED_LOCALES,
    load_preferences,
    save_preferences,
    translate,
)


def test_all_top_level_strings_exist_in_all_three_locales() -> None:
    keys = (
        "nav.chat",
        "nav.feedback",
        "nav.usage",
        "nav.announcement",
        "page.connect",
        "page.usage",
        "usage.total_tokens",
        "usage.coverage",
        "usage.platforms",
        "sidebar.agent_library",
        "sidebar.version",
        "sidebar.library_none",
        "sidebar.library_selected",
        "sidebar.library_empty",
        "sidebar.online",
        "sidebar.rooms",
        "sidebar.messages",
        "sidebar.memory",
        "sidebar.dispatch",
        "sidebar.open_calls",
        "sidebar.active",
        "sidebar.audit",
        "sidebar.sync",
        "sidebar.dispatch_running",
        "sidebar.dispatch_retry",
        "sidebar.dispatch_failed",
        "sidebar.dispatch_completed",
        "sidebar.database_error",
        "usage.today",
        "usage.cached_input",
        "usage.reasoning",
        "usage.calls_no_tokens",
        "usage.provider_reported_tokens",
        "usage.unavailable",
        "usage.partial",
        "usage.derived_total",
        "usage.note",
        "toolbar.search",
        "toolbar.language",
        "toolbar.updates",
        "toolbar.announcements",
        "announcement.network",
        "announcement.network_off",
        "announcement.popup",
        "announcement.updated",
        "feedback.include_key",
        "tutorial.done",
        "tutorial.step1.body",
        "tutorial.step4.title",
        "updates.available",
        "chat.attach",
        "chat.clear_attachments",
        "chat.no_attachments",
        "chat.attachments_selected",
        "chat.attachment_note",
        "chat.to",
        "chat.priority",
        "chat.task",
        "chat.provider",
        "chat.model",
        "chat.reasoning",
        "chat.subject",
        "chat.new_room",
        "chat.join_control",
        "chat.older",
        "chat.latest",
        "chat.focus",
        "chat.exit_focus",
        "chat.auto",
        "chat.mode.off",
        "chat.mode.once",
        "chat.mode.discussion",
        "chat.rounds",
        "chat.messages",
        "chat.stagnation",
        "chat.apply",
        "chat.pause",
        "chat.resume",
        "chat.continue",
        "chat.stop",
        "chat.room_seats",
        "chat.apply_seat",
        "chat.remove_seat",
        "chat.manage_providers",
        "chat.send",
        "chat.message_hint",
        "chat.join_to_send",
        "chat.seat_hint",
        "chat.recipient.all",
        "chat.route.direct",
        "chat.route.broadcast",
        "chat.route.unregistered",
        "chat.route_unique_required",
        "chat.model.default",
        "chat.reasoning.default",
        "chat.lobby_hint",
        "provider.display_name_required",
        "provider.class_invalid",
        "provider.endpoint_required",
        "provider.local_loopback_only",
        "provider.api_key_required",
        "provider.show_api_key",
        "provider.hide_api_key",
        "provider.saving",
        "provider.save_failed",
    )
    for locale in SUPPORTED_LOCALES:
        for key in keys:
            assert translate(locale, key)


def test_chinese_usage_labels_use_token_but_no_other_english_chart_terms() -> None:
    for locale in ("zh-Hant", "zh-Hans"):
        labels = " ".join(
            translate(locale, key)
            for key in (
                "page.usage",
                "usage.total_tokens",
                "usage.input_tokens",
                "usage.output_tokens",
                "usage.coverage",
                "usage.daily",
                "usage.today",
                "usage.cached_input",
                "usage.reasoning",
                "usage.agent",
                "usage.calls_no_tokens",
                "usage.provider_reported_tokens",
                "usage.unavailable",
                "usage.partial",
                "usage.derived_total",
                "usage.no_data",
                "usage.note",
                "chat.to",
                "chat.priority",
                "chat.task",
                "chat.provider",
                "chat.model",
                "chat.reasoning",
                "chat.model.default",
                "chat.reasoning.default",
                "chat.subject",
            )
        ).lower()
        assert "token" in labels
        assert "詞元" not in labels
        assert "词元" not in labels
        assert "cache" not in labels
        assert "reason" not in labels
        assert "agent" not in labels
        assert "unavailable" not in labels
        assert "partial" not in labels
        assert "provider" not in labels
        assert "agent" not in translate(locale, "sidebar.agent_library").lower()


def test_chinese_usage_shell_has_no_english_status_labels() -> None:
    keys = (
        "sidebar.version",
        "sidebar.library_none",
        "sidebar.library_empty",
        "sidebar.online",
        "sidebar.rooms",
        "sidebar.messages",
        "sidebar.memory",
        "sidebar.dispatch",
        "sidebar.open_calls",
        "sidebar.active",
        "sidebar.audit",
        "sidebar.sync",
        "sidebar.dispatch_running",
        "sidebar.dispatch_retry",
        "sidebar.dispatch_failed",
        "sidebar.dispatch_completed",
        "sidebar.database_error",
        "toolbar.language",
    )
    forbidden = (
        "global",
        "online",
        "rooms",
        "messages",
        "memory",
        "dispatch",
        "open call",
        "active",
        "audit",
        "sync",
        "run",
        "retry",
        "fail",
        "done",
        "language",
        "database error",
    )
    for locale in ("zh-Hant", "zh-Hans"):
        labels = " ".join(translate(locale, key) for key in keys).lower()
        assert not any(word in labels for word in forbidden)


def test_tutorial_describes_local_provider_security_without_claiming_a_key() -> None:
    for locale in SUPPORTED_LOCALES:
        body = translate(locale, "tutorial.step1.body").lower()
        assert "loopback" in body
        assert "api key" in body


def test_preferences_are_local_atomic_and_persistent(tmp_path) -> None:
    assert load_preferences(tmp_path)["locale"] == "zh-Hant"
    saved = save_preferences(tmp_path, locale="en", tutorial_completed=True)
    assert load_preferences(tmp_path) == saved
    assert not list((tmp_path / ".peerbridge").glob("*.tmp"))


def test_bad_preferences_fail_closed(tmp_path) -> None:
    target = tmp_path / ".peerbridge" / "ui-preferences.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
    with pytest.raises(LocalizationError):
        load_preferences(tmp_path)
    with pytest.raises(LocalizationError):
        save_preferences(tmp_path, locale="xx", tutorial_completed=False)


def test_unknown_translation_key_is_not_silently_invented() -> None:
    with pytest.raises(LocalizationError):
        translate("en", "does.not.exist")
