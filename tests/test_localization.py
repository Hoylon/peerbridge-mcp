from __future__ import annotations

import json
from pathlib import Path

import pytest

from peerbridge_mcp.localization import (
    _ALPHA52_CATALOG,
    LocalizationError,
    SUPPORTED_LOCALES,
    SUPPORTED_THEMES,
    THEME_LABELS,
    UI_PREFERENCES_SCHEMA,
    load_preferences,
    save_preferences,
    translate,
)

PANEL_TUTORIAL_KEYS = (
    "cockpit",
    "chat",
    "work",
    "review",
    "change",
    "audit",
    "connect",
    "memory",
    "trust",
    "feedback",
    "usage",
    "announcement",
)


def test_all_top_level_strings_exist_in_all_three_locales() -> None:
    keys = (
        "nav.cockpit",
        "nav.chat",
        "nav.trust",
        "nav.feedback",
        "nav.usage",
        "nav.announcement",
        "modern.nav.workspace",
        "modern.nav.governance",
        "modern.nav.system",
        "modern.nav.more",
        "modern.nav.less",
        "modern.rooms.title",
        "modern.rooms.empty",
        "modern.rooms.unnamed",
        "modern.rooms.messages",
        "modern.account.role",
        "modern.toolbar.connected",
        "modern.toolbar.unavailable",
        "modern.toolbar.options",
        "modern.toolbar.close_options",
        "modern.room.settings",
        "modern.room.hide_settings",
        "modern.agents.manage",
        "modern.agents.done",
        "modern.inspector.agents",
        "modern.inspector.workflow",
        "modern.inspector.evidence",
        "modern.inspector.evidence_intro",
        "modern.inspector.room_status",
        "modern.inspector.seat_count",
        "modern.inspector.messages",
        "modern.inspector.completed",
        "modern.inspector.in_flight",
        "modern.inspector.failed",
        "modern.inspector.reply_progress",
        "modern.inspector.automation",
        "modern.inspector.automation_value",
        "modern.composer.advanced",
        "modern.composer.simple",
        "modern.composer.prompt",
        "modern.sidebar.new_room",
        "modern.sidebar.show_agents",
        "modern.sidebar.hide_agents",
        "modern.message.you",
        "modern.message.details",
        "modern.message.hide_details",
        "modern.round.title",
        "modern.round.response_count",
        "modern.round.summary",
        "modern.round.activity",
        "modern.round.hide_activity",
        "modern.agent.replied",
        "modern.agent.working",
        "modern.agent.retrying",
        "modern.agent.failed",
        "modern.status.discussion",
        "modern.status.workflow",
        "modern.status.seat",
        "modern.seat.agent",
        "modern.seat.provider",
        "modern.seat.model",
        "modern.seat.reasoning",
        "modern.open.work",
        "modern.open.review",
        "modern.open.audit",
        "modern.open.trust",
        "modern.open.memory",
        "modern.open.usage",
        "page.connect",
        "page.cockpit",
        "page.trust",
        "page.usage",
        "cockpit.launch_title",
        "cockpit.task",
        "cockpit.view_existing",
        "cockpit.observable_notice",
        "chat.guided.readiness.loading",
        "chat.guided.readiness.ready",
        "chat.guided.readiness.join_control",
        "chat.guided.readiness.need_agents",
        "chat.guided.readiness.need_routes",
        "chat.guided.readiness.too_many_agents",
        "chat.guided.readiness.incomplete_binding",
        "chat.guided.readiness.room_unavailable",
        "chat.guided.readiness.active_discussion",
        "cockpit.agent",
        "cockpit.role",
        "cockpit.directory",
        "cockpit.browse",
        "cockpit.directory_dialog",
        "cockpit.start",
        "cockpit.start_send",
        "cockpit.session",
        "cockpit.send",
        "cockpit.interrupt",
        "cockpit.stop",
        "cockpit.view.grid",
        "cockpit.view.focus",
        "cockpit.view.timeline",
        "cockpit.tab.terminal",
        "cockpit.tab.activity",
        "cockpit.tab.answer",
        "cockpit.tab.evidence",
        "cockpit.timeline.time",
        "cockpit.timeline.session",
        "cockpit.timeline.kind",
        "cockpit.timeline.summary",
        "cockpit.role.equal-participant",
        "cockpit.role.researcher",
        "cockpit.role.implementer",
        "cockpit.role.reviewer",
        "cockpit.role.investigator",
        "cockpit.role.planner",
        "cockpit.role.auditor",
        "cockpit.role.custom",
        "cockpit.source.peerbridge-room",
        "cockpit.source.managed-cli",
        "cockpit.source.authorized-desktop",
        "cockpit.source_contract",
        "cockpit.capability_value",
        "cockpit.capability.detectable",
        "cockpit.capability.mirrorable",
        "cockpit.capability.input_capable",
        "cockpit.capability.context_resumable",
        "cockpit.capability.terminal_controllable",
        "cockpit.capability.model_route_only",
        "cockpit.panel_header",
        "cockpit.panel_meta",
        "cockpit.evidence_template",
        "cockpit.yes",
        "cockpit.no",
        "cockpit.unavailable",
        "cockpit.unverified",
        "cockpit.status.ready",
        "cockpit.status.busy",
        "cockpit.status.prompt_required",
        "cockpit.status.select_session",
        "cockpit.status.starting",
        "cockpit.status.sending",
        "cockpit.status.interrupting",
        "cockpit.status.stopping",
        "cockpit.status.start_complete",
        "cockpit.status.start_send_complete",
        "cockpit.status.send_complete",
        "cockpit.status.interrupt_complete",
        "cockpit.status.stop_complete",
        "cockpit.status.unknown_error",
        "cockpit.status.failed",
        "cockpit.status.source_read_only",
        "cockpit.status.source_unavailable",
        "cockpit.status.source_focused",
        "usage.total_tokens",
        "usage.coverage",
        "usage.platforms",
        "sidebar.agent_library",
        "sidebar.version",
        "sidebar.build",
        "updates.current_release",
        "updates.current_local_build",
        "updates.available_build",
        "updates.open_release",
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
        "edit.cut",
        "edit.copy",
        "edit.paste",
        "edit.select_all",
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
        "chat.role",
        "chat.role.equal-participant",
        "chat.role.researcher",
        "chat.role.implementer",
        "chat.role.reviewer",
        "chat.role.custom",
        "chat.apply_role",
        "chat.view_live_work",
        "chat.live_work_unavailable",
        "chat.live_work_focused",
        "chat.role_no_authority",
        "chat.role_select_agent",
        "chat.role_custom_required",
        "chat.role_applying",
        "chat.role_applied",
        "chat.seat_column.agent",
        "chat.seat_column.role",
        "chat.seat_column.session",
        "chat.seat_column.route",
        "chat.seat_column.state",
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


def test_alpha52_control_room_catalog_is_complete_in_all_locales() -> None:
    expected = set(_ALPHA52_CATALOG["en"])
    assert len(expected) >= 100
    for locale in SUPPORTED_LOCALES:
        assert set(_ALPHA52_CATALOG[locale]) == expected
        assert all(translate(locale, key) for key in expected)


def test_dynamic_room_and_connection_labels_are_localized_in_all_locales() -> None:
    keys = (
        "ui.preferences_error",
        "sidebar.waiting_database",
        "chat.rooms_loading",
        "chat.room_label",
        "chat.room_dialog.title",
        "chat.room_dialog.id",
        "chat.room_dialog.name",
        "chat.room_dialog.cancel",
        "chat.room_dialog.create",
        "chat.automation.invalid",
        "chat.automation.idle",
        "chat.discussion.idle",
        "chat.discussion.pending",
        "chat.discussion.result",
        "chat.discussion.round",
        "chat.discussion.status.active",
        "chat.discussion.status.paused",
        "chat.discussion.status.completed",
        "chat.discussion.status.stopped",
        "chat.discussion.status.failed",
        "chat.discussion.status.blocked",
        "chat.discussion.status.unknown",
        "chat.room_api_error",
        "chat.room_status.base",
        "chat.room_status.loaded",
        "chat.room_status.loaded_range",
        "chat.room_status.loading",
        "chat.no_active_seats",
        "chat.room_agents",
        "chat.no_agents",
        "chat.sending_button",
        "chat.no_messages",
        "chat.default_subject",
        "chat.copy_sha",
        "chat.metadata.scope",
        "chat.metadata.task",
        "chat.state.online",
        "chat.state.offline",
        "chat.state.control",
        "chat.state.active",
        "chat.state.unrouted",
        "chat.route.none",
        "chat.route.direct_global",
        "chat.session.implicit",
        "chat.session.global",
        "connect.field.connection_id",
        "connect.field.display_name",
        "connect.field.class",
        "connect.field.agent_id",
        "connect.field.endpoint",
        "connect.field.api_key",
        "connect.field.client_name",
        "connect.field.route_id",
        "connect.field.model",
        "connect.field.response_model",
        "connect.field.reasoning",
        "connect.field.app",
        "connect.field.provider",
    )
    for locale in SUPPORTED_LOCALES:
        assert all(translate(locale, key).strip() for key in keys)


def test_monitor_does_not_reintroduce_localizable_english_ui_literals() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "peerbridge_mcp"
        / "monitor.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "Preferences:",
        "WAITING FOR DB",
        "AUTO // INVALID MODE",
        "ROOM API ERROR",
        "Create PeerBridge Room",
        "NO ACTIVE SEATS",
        "ROOM AGENTS",
        "NO AGENT IN THIS ROOM",
        "SENDING...",
        "NO MESSAGES",
        "COPY SHA",
        "ROOMS LOADING",
        "DISCUSSION //",
        "HUMAN INTERVENTION",
        "SCOPE:",
        "TASK:",
    )
    assert not [literal for literal in forbidden if literal in source]


def test_delivery_timeout_labels_are_explicit_and_distinct_from_provider_failure() -> None:
    expected = {
        "zh-Hant": (
            "{agent} 未回覆：本機推理執行器超過硬性時限，已嘗試 {attempts} 次後停止。",
            "{agent} 未回覆：引導式討論超過已設定的時限。",
        ),
        "zh-Hans": (
            "{agent} 未回复：本地推理运行器超过硬性时限，已尝试 {attempts} 次后停止。",
            "{agent} 未回复：引导式讨论超过已设置的时限。",
        ),
        "en": (
            "{agent} did not reply because the local inference runner exceeded its hard deadline after {attempts} attempt(s).",
            "{agent} did not reply because the guided discussion exceeded its configured timeout.",
        ),
    }

    for locale, (runner_timeout, discussion_timeout) in expected.items():
        provider_failure = translate(locale, "chat.delivery.provider_unavailable")
        rate_limited = translate(locale, "chat.delivery.rate_limited")
        assert translate(
            locale, "chat.delivery.runner_hard_deadline_exceeded"
        ) == runner_timeout
        assert translate(locale, "chat.delivery.discussion_timed_out") == discussion_timeout
        assert len({runner_timeout, discussion_timeout, provider_failure, rate_limited}) == 4


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


def test_sidebar_version_keeps_the_complete_release_on_its_own_line() -> None:
    version = "0.1.0a5.post2"

    for locale in SUPPORTED_LOCALES:
        lines = translate(locale, "sidebar.version").format(version=version).splitlines()
        assert len(lines) == 2
        assert lines[1] == f"v{version}"


def test_tutorial_describes_local_provider_security_without_claiming_a_key() -> None:
    for locale in SUPPORTED_LOCALES:
        body = translate(locale, "tutorial.step1.body").lower()
        assert "loopback" in body
        assert "api key" in body


def test_every_panel_has_a_localized_three_step_illustrated_guide() -> None:
    for locale in SUPPORTED_LOCALES:
        assert translate(locale, "tutorial.all_panels")
        assert translate(locale, "tutorial.diagram_note")
        assert translate(locale, "tutorial.purpose_label")
        assert translate(locale, "tutorial.open_panel")
        for page_key in PANEL_TUTORIAL_KEYS:
            purpose = translate(locale, f"tutorial.panel.{page_key}.purpose")
            body = translate(locale, f"tutorial.panel.{page_key}.body")
            assert len(purpose) >= 25
            assert not purpose.startswith("1.")
            steps = body.split("\n\n")
            assert len(steps) == 3
            assert all(
                step.startswith(f"{number}.") and len(step) >= 30
                for number, step in enumerate(steps, start=1)
            )


def test_trust_page_name_and_guide_expose_all_verification_functions() -> None:
    expected = {
        "zh-Hant": (
            "信任與任務驗證工作",
            ("作業", "排程", "信任時間線", "治理", "證據包", "權限決策", "隔離工作樹", "能力與授權", "簡報與衝突"),
        ),
        "zh-Hans": (
            "信任与任务验证工作",
            ("作业", "计划", "信任时间线", "治理", "证据包", "权限决策", "隔离工作树", "能力与授权", "简报与冲突"),
        ),
        "en": (
            "Trust & Task Verification",
            ("Operations", "Schedules", "Trust Timeline", "Governance", "Proof Bundle", "Permissions", "Isolated Worktrees", "Capabilities & Grants", "Briefings & Conflicts"),
        ),
    }

    for locale, (page_name, guide_terms) in expected.items():
        assert translate(locale, "page.trust") == page_name
        assert translate(locale, "nav.trust") == f"09  {page_name}"
        guide = translate(locale, "tutorial.panel.trust.body")
        assert all(term in guide for term in guide_terms)


def test_sidebar_and_installer_distinguish_routes_from_local_terminals() -> None:
    for locale in SUPPORTED_LOCALES:
        notice = translate(locale, "sidebar.library_route_notice").lower()
        intro = translate(locale, "agent_install.intro").lower()
        kimi = translate(locale, "agent_install.note.kimi").lower()
        grok = translate(locale, "agent_install.note.grok").lower()
        docs = translate(locale, "agent_install.docs").format(name="Kimi Code")

        assert "route" in notice or "路由" in notice
        assert "terminal" in notice or "終端" in notice or "终端" in notice
        assert "grok" in intro and "kimi" in intro
        assert "kimi" in kimi and ("confirm" in kimi or "確認" in kimi or "确认" in kimi)
        assert "grok" in grok and ("guide" in grok or "指南" in grok)
        assert "Kimi Code" in docs


def test_usage_guide_names_the_api_summary_without_kpi_wording() -> None:
    for locale in SUPPORTED_LOCALES:
        assert "API" in translate(locale, "nav.usage")
        assert "API" in translate(locale, "page.usage")
        body = translate(locale, "tutorial.panel.usage.body")
        assert "API" in body
        assert "KPI" not in body.upper()


def test_cockpit_never_claims_hidden_reasoning_is_visible() -> None:
    for locale in SUPPORTED_LOCALES:
        notice = translate(locale, "cockpit.observable_notice").lower()
        assert "peerbridge" in notice
        assert "hidden reasoning" in notice or "隱藏思考" in notice or "隐藏思考" in notice


def test_cockpit_and_chat_tutorials_explain_the_source_and_role_boundaries() -> None:
    expected = {
        "zh-Hant": ("已偵測", "原本桌面或終端", "平等參與者", "兩回合"),
        "zh-Hans": ("已检测", "原来的桌面或终端", "平等参与者", "两回合"),
        "en": ("detected", "original desktop or terminal", "equal participant", "two rounds"),
    }
    forbidden_role_editor = {
        "zh-Hant": "選擇已安裝的智能體命令列工具、執行角色",
        "zh-Hans": "选择已安装的智能体命令行工具、执行角色",
        "en": "select an installed agent cli, execution role",
    }

    for locale in SUPPORTED_LOCALES:
        cockpit = translate(locale, "tutorial.panel.cockpit.body").lower()
        chat = translate(locale, "tutorial.panel.chat.body").lower()
        first_run = translate(locale, "tutorial.step1.body").lower()
        detected, input_owner, default_role, bounded_rounds = expected[locale]
        assert "adapter" in cockpit
        assert detected.lower() in cockpit
        assert input_owner.lower() in cockpit
        assert default_role.lower() in cockpit
        assert forbidden_role_editor[locale].lower() not in cockpit
        assert forbidden_role_editor[locale].lower() not in first_run
        assert "02" in first_run and ("role" in first_run or "角色" in first_run)
        assert "folder" in first_run or "資料夾" in first_run or "文件夹" in first_run
        assert default_role.lower() in chat
        assert bounded_rounds.lower() in chat


def test_cockpit_action_labels_remain_compact() -> None:
    expected = {
        "zh-Hant": ("送到所選 CLI", "中斷所選 CLI", "停止所選 CLI"),
        "zh-Hans": ("发送到所选 CLI", "中断所选 CLI", "停止所选 CLI"),
        "en": ("Send to selected CLI", "Interrupt selected CLI", "Stop selected CLI"),
    }

    for locale, labels in expected.items():
        assert tuple(
            translate(locale, key)
            for key in ("cockpit.send", "cockpit.interrupt", "cockpit.stop")
        ) == labels
        assert all(len(label) <= 24 for label in labels)


def test_cockpit_launch_labels_remain_compact_at_high_dpi() -> None:
    expected = {
        "zh-Hant": ("智能體 CLI", "工作資料夾", "瀏覽..."),
        "zh-Hans": ("智能体 CLI", "工作文件夹", "浏览..."),
        "en": ("Agent CLI", "Working folder", "Browse..."),
    }

    for locale, labels in expected.items():
        assert tuple(
            translate(locale, key)
            for key in ("cockpit.agent", "cockpit.directory", "cockpit.browse")
        ) == labels
        assert all(len(label) <= 14 for label in labels)


def test_preferences_are_local_atomic_and_persistent(tmp_path) -> None:
    assert load_preferences(tmp_path) == {
        "schema": UI_PREFERENCES_SCHEMA,
        "locale": "zh-Hant",
        "tutorial_completed": False,
        "theme": "pixel",
    }
    saved = save_preferences(
        tmp_path,
        locale="en",
        tutorial_completed=True,
        theme="modern",
    )
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


def test_v1_preferences_migrate_to_pixel_theme_without_rewriting(tmp_path) -> None:
    target = tmp_path / ".peerbridge" / "ui-preferences.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema": "peerbridge.ui-preferences.v1",
                "locale": "zh-Hans",
                "tutorial_completed": True,
            }
        ),
        encoding="utf-8",
    )

    loaded = load_preferences(tmp_path)

    assert loaded == {
        "schema": UI_PREFERENCES_SCHEMA,
        "locale": "zh-Hans",
        "tutorial_completed": True,
        "theme": "pixel",
    }
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == (
        "peerbridge.ui-preferences.v1"
    )


def test_theme_labels_are_complete_and_localized() -> None:
    assert SUPPORTED_THEMES == ("pixel", "modern")
    assert set(THEME_LABELS) == set(SUPPORTED_LOCALES)
    for locale in SUPPORTED_LOCALES:
        assert set(THEME_LABELS[locale]) == set(SUPPORTED_THEMES)
        assert all(THEME_LABELS[locale].values())


def test_unknown_translation_key_is_not_silently_invented() -> None:
    with pytest.raises(LocalizationError):
        translate("en", "does.not.exist")
