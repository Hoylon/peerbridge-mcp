from __future__ import annotations

import hashlib
import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from peerbridge_mcp.localization import save_preferences
from peerbridge_mcp.monitor import (
    AGENT_LIBRARY_CANVAS_HEIGHT,
    AGENT_LIBRARY_CANVAS_WIDTH,
    AGENT_LIBRARY_COLUMNS,
    AGENT_LIBRARY_CARD_STRIDE,
    AGENT_LIBRARY_TOP_MARGIN,
    AGENT_LIBRARY_VISIBLE_CAPACITY,
    CHAT_HISTORY_MIN_HEIGHT,
    CHAT_PAGE_MIN_CONTENT_HEIGHT,
    INFERENCE_ONLY,
    MCP_NATIVE,
    MCP_TOOL_LOOP,
    MCP_UNVERIFIED,
    MODERN_CHAT_MAX_WIDTH,
    MODERN_CHAT_MIN_GUTTER,
    MODERN_INSPECTOR_KEYS,
    MODERN_FONT_FAMILY,
    MODERN_NAV_GROUPS,
    PIXEL_FONT_FAMILY,
    SIDEBAR_SCROLLBAR_WIDTH,
    SIDEBAR_SCROLLBAR_STYLE,
    SIDEBAR_TEXT_SIZE,
    SIDEBAR_WIDTH,
    TUTORIAL_BODY_TEXT_SIZE,
    TUTORIAL_PAGE_KEYS,
    WINDOWS_UI_SCALE_FACTORS,
    COLOR_PALETTES,
    COLORS,
    McpHumanClient,
    PixelMonitor,
    apply_color_palette,
    agent_mcp_access_mode,
    chat_bubble_metrics,
    chat_split_sash_position,
    centered_transient_geometry,
    compact_sidebar_stats,
    incremental_render_mode,
    modern_navigation_is_complete,
    modern_navigation_pages,
    modern_chat_content_geometry,
    provider_display_label,
    room_agent_card_groups,
    room_agent_visible_limit,
    resolved_runtime_theme,
    runtime_build_identity,
    safe_chat_artifact_labels,
    tk_scaling_for_windows_factor,
    tutorial_diagram_spec,
    ui_content_signature,
    vertical_scroll_fraction_to_reveal,
)


def test_compact_window_keeps_a_useful_history_and_scrollable_page() -> None:
    assert CHAT_HISTORY_MIN_HEIGHT >= 240
    assert CHAT_PAGE_MIN_CONTENT_HEIGHT >= 840


def test_built_in_themes_are_bounded_and_switch_without_shared_state_leaks() -> None:
    required = {
        "bg",
        "panel",
        "panel_2",
        "line",
        "text",
        "muted",
        "cyan",
        "amber",
        "green",
        "red",
        "purple",
        "blue",
        "black",
    }
    try:
        assert set(COLOR_PALETTES) == {"pixel", "modern"}
        assert all(set(palette) == required for palette in COLOR_PALETTES.values())
        modern = apply_color_palette("modern")
        assert modern == COLOR_PALETTES["modern"]
        assert COLORS == COLOR_PALETTES["modern"]
        modern["bg"] = "#ffffff"
        assert COLORS["bg"] != "#ffffff"
        with pytest.raises(ValueError, match="unsupported UI theme"):
            apply_color_palette("untrusted")
    finally:
        apply_color_palette("pixel")


def test_pixel_theme_contract_is_unchanged_and_modern_theme_is_genuinely_light() -> None:
    assert COLOR_PALETTES["pixel"] == {
        "bg": "#101419",
        "panel": "#171d24",
        "panel_2": "#202832",
        "line": "#36414f",
        "text": "#e8edf2",
        "muted": "#91a0ad",
        "cyan": "#5dd9e8",
        "amber": "#ffc857",
        "green": "#67d391",
        "red": "#ff6b6b",
        "purple": "#b8a1ff",
        "blue": "#68a7ff",
        "black": "#080b0f",
    }
    modern = COLOR_PALETTES["modern"]
    assert modern["bg"].lower() == "#f6f7f9"
    assert modern["panel"].lower() == "#ffffff"
    assert modern["text"].lower() == "#151922"
    assert MODERN_FONT_FAMILY == "Segoe UI Variable Text"
    assert PIXEL_FONT_FAMILY == "Cascadia Mono"


def test_modern_chat_content_geometry_centers_wide_and_preserves_gutters() -> None:
    content_width, offset = modern_chat_content_geometry(1600)
    assert content_width == MODERN_CHAT_MAX_WIDTH
    assert offset == 280

    narrow_width, narrow_offset = modern_chat_content_geometry(800)
    assert narrow_width == 800 - (MODERN_CHAT_MIN_GUTTER * 2)
    assert narrow_offset == MODERN_CHAT_MIN_GUTTER


def test_modern_shell_groups_every_peerbridge_page_once() -> None:
    grouped_pages = modern_navigation_pages()
    assert MODERN_INSPECTOR_KEYS == ("agents", "workflow", "evidence")
    assert tuple(group for group, _pages in MODERN_NAV_GROUPS) == (
        "workspace",
        "governance",
        "system",
    )
    assert grouped_pages == (
        "cockpit",
        "chat",
        "work",
        "review",
        "change",
        "audit",
        "trust",
        "connect",
        "memory",
        "feedback",
        "usage",
        "announcement",
    )
    assert modern_navigation_is_complete()
    assert set(grouped_pages) == set(TUTORIAL_PAGE_KEYS)


def test_monitor_cli_accepts_release_locales_and_themes() -> None:
    from peerbridge_mcp.monitor import parse_args

    for locale in ("zh-Hant", "zh-Hans", "en"):
        for theme in ("pixel", "modern"):
            args = parse_args(["--ui-self-test", "--locale", locale, "--theme", theme])
            assert args.locale == locale
            assert args.theme == theme


def test_normal_launch_resolves_explicit_saved_and_invalid_themes(tmp_path: Path) -> None:
    save_preferences(
        tmp_path,
        locale="zh-Hant",
        tutorial_completed=True,
        theme="modern",
    )
    assert resolved_runtime_theme(tmp_path, None) == "modern"
    assert resolved_runtime_theme(tmp_path, "pixel") == "pixel"

    preference_path = tmp_path / ".peerbridge" / "ui-preferences.json"
    preference_path.write_text('{"schema":"invalid"}', encoding="utf-8")
    assert resolved_runtime_theme(tmp_path, None) == "pixel"


def test_sidebar_and_tutorial_keep_readable_minimum_dimensions() -> None:
    assert SIDEBAR_WIDTH >= 300
    assert SIDEBAR_SCROLLBAR_WIDTH <= 16
    assert SIDEBAR_SCROLLBAR_STYLE == "Sidebar.Vertical.TScrollbar"
    assert SIDEBAR_TEXT_SIZE >= 9
    assert TUTORIAL_BODY_TEXT_SIZE >= 11


def test_runtime_build_identity_distinguishes_same_version_binaries(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "PeerBridgeControlRoom.exe"
    binary.write_bytes(b"peerbridge-alpha-5.2-candidate")

    assert runtime_build_identity(binary) == hashlib.sha256(binary.read_bytes()).hexdigest()[:12]


def test_mcp_human_client_surfaces_tool_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    response = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"error": "room has no active Agent seats", "tool": "post_room_message"}
                    ),
                }
            ],
        },
    }

    monkeypatch.setattr(
        "peerbridge_mcp.monitor.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=json.dumps(response), stderr="", returncode=0
        ),
    )
    client = McpHumanClient(tmp_path, tmp_path / "test.sqlite3", "test")

    with pytest.raises(RuntimeError, match="room has no active Agent seats"):
        client.call_tool("post_room_message", {})


def test_native_route_registration_sends_explicit_bounded_timeout() -> None:
    monitor = object.__new__(PixelMonitor)
    monitor.connection_in_progress = False
    monitor.connection_id = _Value("grok-build")
    monitor.connection_class = _Value("relay")
    monitor.connection_agent = _Value("grok-relay")
    monitor.connection_route_id = _Value("grok-build-timeout-180")
    monitor.connection_model = _Value("grok-4.6")
    monitor.connection_response_model = _Value("grok-4.6")
    monitor.connection_timeout_seconds = _Value("180")
    monitor.connection_client = _Value("openai-compatible")
    monitor.connection_reasoning = _Value("high")
    monitor.connection_model_combo = _OptionWidget(values=("grok-4.6",))
    monitor.connection_status = _Value()
    monitor.connection_status_label = _Configurable()
    monitor.refresh = lambda *, force=False: None

    def translated(key: str) -> str:
        if key == "connect.route_registered":
            return "{route} {model} {sha}"
        if key == "connect.route_failed":
            return "failed: {error}"
        return key

    monitor._t = translated
    calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(tool: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((tool, dict(arguments)))
        if tool == "list_provider_connections":
            return {
                "connections": [
                    {
                        "connection_id": "grok-build",
                        "provider_id": "grok-build",
                        "route_class": "relay",
                        "secret_backend": "windows-credential-manager",
                    }
                ]
            }
        assert tool == "upsert_route_profile"
        return {
            **arguments,
            "profile_sha256": "a" * 64,
        }

    monitor.human_client = SimpleNamespace(call_tool=call_tool)
    monitor.register_native_route()

    assert calls[1] == (
        "upsert_route_profile",
        {
            "route_id": "grok-build-timeout-180",
            "agent_id": "grok-relay",
            "provider_id": "grok-build",
            "model_id": "grok-4.6",
            "inference_timeout_seconds": 180,
            "route_class": "relay",
            "enabled": True,
            "client_name": "openai-compatible",
            "response_model_id": "grok-4.6",
            "reasoning_mode": "high",
        },
    )

    monitor.connection_timeout_seconds.set("301")
    monitor.register_native_route()
    assert len(calls) == 2
    assert monitor.connection_status.get() == "failed: connect.timeout_invalid"


class _StableInner:
    def winfo_children(self) -> list[object]:
        raise AssertionError("stable refresh must not inspect or destroy chat widgets")


class _Viewport:
    def __init__(self, view: tuple[float, float] = (0.5, 1.0)) -> None:
        self.view = view
        self.moves: list[float] = []

    def yview(self) -> tuple[float, float]:
        return self.view

    def yview_moveto(self, position: float) -> None:
        self.moves.append(position)


class _IdleRoot:
    @staticmethod
    def after_idle(callback: object) -> None:
        callback()


class _QueuedIdleRoot:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def after_idle(self, callback: object) -> None:
        self.callbacks.append(callback)


class _DestroyableChild:
    def __init__(self) -> None:
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


class _RebuildInner:
    def __init__(self, child: _DestroyableChild) -> None:
        self.child = child

    def winfo_children(self) -> list[_DestroyableChild]:
        return [self.child]


class _Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _Configurable:
    def __init__(self) -> None:
        self.configurations: list[dict[str, object]] = []

    def configure(self, **kwargs: object) -> None:
        self.configurations.append(kwargs)


class _OptionWidget(_Configurable):
    def __init__(self, **options: object) -> None:
        super().__init__()
        self.options = dict(options)

    def configure(self, **kwargs: object) -> None:
        super().configure(**kwargs)
        self.options.update(kwargs)

    def cget(self, key: str) -> object:
        return self.options[key]


class _Canvas(_Configurable):
    def delete(self, _target: str) -> None:
        return None

    def create_text(self, *_args: object, **_kwargs: object) -> None:
        return None

    def create_rectangle(self, *_args: object, **_kwargs: object) -> None:
        return None

    def create_oval(self, *_args: object, **_kwargs: object) -> None:
        return None

    @staticmethod
    def canvasx(value: int) -> int:
        return value

    @staticmethod
    def canvasy(value: int) -> int:
        return value


class _PackableScrollbar:
    def __init__(self) -> None:
        self.manager = "pack"
        self.pack_calls = 0
        self.forget_calls = 0

    def winfo_manager(self) -> str:
        return self.manager

    def pack(self, **_kwargs: object) -> None:
        self.manager = "pack"
        self.pack_calls += 1

    def pack_forget(self) -> None:
        self.manager = ""
        self.forget_calls += 1


class _Body:
    def __init__(self, value: str) -> None:
        self.value = value
        self.deletes = 0

    def get(self, _start: str, _end: str) -> str:
        return self.value

    def delete(self, _start: str, _end: str) -> None:
        self.value = ""
        self.deletes += 1


class _ClipboardRoot:
    def __init__(self, value: str = "") -> None:
        self.clipboard = value
        self.bindings: dict[tuple[str, str], object] = {}

    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, value: str) -> None:
        self.clipboard += value

    def clipboard_get(self) -> str:
        return self.clipboard

    def bind_class(self, widget_class: str, sequence: str, handler: object) -> None:
        self.bindings[(widget_class, sequence)] = handler


class _EditableEntry:
    def __init__(
        self,
        value: str,
        *,
        selection: tuple[int, int] | None = None,
        state: str = "normal",
    ) -> None:
        self.value = value
        self.selection = selection
        self.cursor = selection[1] if selection else len(value)
        self.widget_state = state

    @staticmethod
    def winfo_class() -> str:
        return "Entry"

    def cget(self, key: str) -> str:
        assert key == "state"
        return self.widget_state

    def index(self, index: str) -> int:
        if index == "sel.first" and self.selection is not None:
            return self.selection[0]
        if index == "sel.last" and self.selection is not None:
            return self.selection[1]
        raise ValueError(index)

    def get(self) -> str:
        return self.value

    def delete(self, first: int, last: int) -> None:
        self.value = self.value[: int(first)] + self.value[int(last) :]
        self.cursor = int(first)
        self.selection = None

    def insert(self, index: str, value: str) -> None:
        assert index == "insert"
        self.value = self.value[: self.cursor] + value + self.value[self.cursor :]
        self.cursor += len(value)

    def selection_range(self, first: int, last: str) -> None:
        assert last == "end"
        self.selection = (int(first), len(self.value))

    def icursor(self, index: str) -> None:
        assert index == "end"
        self.cursor = len(self.value)


def test_text_editing_shortcuts_cover_every_desktop_input_class() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.root = _ClipboardRoot()

    monitor._install_text_editing_bindings()

    for widget_class in ("Entry", "TEntry", "Text", "TCombobox"):
        for sequence in (
            "<Control-a>",
            "<Control-c>",
            "<Control-v>",
            "<Control-x>",
            "<Button-3>",
        ):
            assert (widget_class, sequence) in monitor.root.bindings


def test_text_editing_copy_cut_paste_and_select_all() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.root = _ClipboardRoot()
    entry = _EditableEntry("alpha beta", selection=(0, 5))

    assert monitor._copy_text_widget_selection(entry) is True
    assert monitor.root.clipboard == "alpha"
    assert monitor._cut_text_widget_selection(entry) is True
    assert entry.value == " beta"

    monitor.root.clipboard = "new"
    assert monitor._paste_into_text_widget(entry) is True
    assert entry.value == "new beta"
    assert monitor._select_all_text_widget(entry) is True
    assert entry.selection == (0, len("new beta"))


def test_read_only_text_fields_allow_copy_but_reject_mutation() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.root = _ClipboardRoot("replacement")
    entry = _EditableEntry("audit receipt", selection=(0, 5), state="readonly")

    assert monitor._copy_text_widget_selection(entry) is True
    assert monitor.root.clipboard == "audit"
    assert monitor._cut_text_widget_selection(entry) is False
    monitor.root.clipboard = "replacement"
    assert monitor._paste_into_text_widget(entry) is False
    assert entry.value == "audit receipt"


def test_send_worker_uses_priority_captured_on_ui_thread() -> None:
    owner_thread = threading.get_ident()

    class ThreadBoundValue(_Value):
        reads = 0

        def get(self) -> str:
            assert threading.get_ident() == owner_thread
            self.reads += 1
            return super().get()

    class Body:
        @staticmethod
        def get(_start: str, _end: str) -> str:
            return "captured payload"

    class HumanClient:
        calls: list[dict[str, object]] = []

        def post_room_message(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            return {"content_sha256": "a" * 64, "room_id": "lobby"}

    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.send_in_progress = False
    monitor._send_token_sequence = 0
    monitor._active_send_token = None
    monitor._room_has_operator = lambda: True
    monitor.message_body = Body()
    monitor.message_task = _Value("send-worker")
    monitor.message_subject = _Value("CAPTURED PRIORITY")
    monitor.message_route_profile = _Value("DIRECT")
    monitor.message_provider = _Value("")
    monitor.message_model = _Value("")
    monitor.message_reasoning = _Value("")
    monitor.message_priority = ThreadBoundValue("critical")
    monitor._chat_attachment_paths = ()
    monitor.project_root = Path.cwd()
    monitor._selected_recipient_id = lambda: "*"
    monitor.room_automation_choice = _Value("off")
    monitor._automation_mode_from_label = lambda _value: "off"
    monitor._room_members = []
    monitor.selected_room_id = "lobby"
    monitor.send_button = _Configurable()
    monitor.chat_attach_button = _Configurable()
    monitor.chat_clear_attachments_button = _Configurable()
    monitor.message_status = _Value()
    monitor.message_status_label = _Configurable()
    monitor.human_client = HumanClient()
    completed = threading.Event()
    result: list[tuple[object, ...]] = []

    def post_to_ui(_callback: object, *args: object) -> bool:
        result.append(args)
        completed.set()
        return True

    monitor._post_to_ui = post_to_ui

    monitor.send_human_message()

    assert completed.wait(3)
    assert monitor.message_priority.reads == 1
    assert monitor.human_client.calls == [
        {
            "room_id": "lobby",
            "task_id": "send-worker",
            "subject": "CAPTURED PRIORITY",
            "body": "captured payload",
            "priority": "critical",
            "artifact_paths": [],
        }
    ]
    assert result[0][1] is None
    assert result[0][2:4] == ("lobby", 1)
    assert result[0][4] == (
        "captured payload",
        "send-worker",
        "CAPTURED PRIORITY",
        "critical",
        (),
    )


class _PumpRoot:
    def __init__(self) -> None:
        self.after_calls: list[tuple[int, object]] = []
        self.cancelled: list[str] = []
        self.destroy_count = 0
        self.reported: list[BaseException] = []

    def after(self, delay: int, callback: object) -> str:
        self.after_calls.append((delay, callback))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, token: str) -> None:
        self.cancelled.append(token)

    def destroy(self) -> None:
        self.destroy_count += 1

    def report_callback_exception(
        self, _kind: type[BaseException], error: BaseException, _traceback: object
    ) -> None:
        self.reported.append(error)


class _ClosableReader:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def test_incremental_render_mode_skips_unchanged_refreshes() -> None:
    rows = ("a", "b")

    assert incremental_render_mode(rows, rows, same_context=True) == "unchanged"
    assert incremental_render_mode(rows, rows, same_context=False) == "rebuild"


def test_chat_split_gives_resized_space_to_history_not_composer() -> None:
    assert chat_split_sash_position(950, 220) == 722
    assert chat_split_sash_position(600, 220) == 372
    assert chat_split_sash_position(300, 220) == 90


def test_sidebar_scroll_reveals_widgets_without_overshooting_content() -> None:
    assert vertical_scroll_fraction_to_reveal(
        widget_top=350,
        widget_bottom=390,
        viewport_top=0,
        viewport_height=300,
        content_height=800,
    ) == pytest.approx(90 / 800)
    assert vertical_scroll_fraction_to_reveal(
        widget_top=150,
        widget_bottom=190,
        viewport_top=400,
        viewport_height=300,
        content_height=800,
    ) == pytest.approx(150 / 800)
    assert vertical_scroll_fraction_to_reveal(
        widget_top=760,
        widget_bottom=800,
        viewport_top=0,
        viewport_height=300,
        content_height=800,
    ) == pytest.approx(500 / 800)


def test_panel_tutorial_schematics_cover_every_page_with_three_callouts() -> None:
    assert len(TUTORIAL_PAGE_KEYS) == 12
    for page_key in TUTORIAL_PAGE_KEYS:
        layout, markers = tutorial_diagram_spec(page_key)
        assert layout in {
            "cockpit",
            "chat",
            "table",
            "connect",
            "trust",
            "feedback",
            "usage",
            "announcement",
        }
        assert len(markers) == 3
        assert all(0.0 < x < 1.0 and 0.0 < y < 1.0 for x, y in markers)

    with pytest.raises(ValueError, match="unsupported tutorial page"):
        tutorial_diagram_spec("missing")


def test_windows_ui_scale_factors_map_to_tk_points_per_inch() -> None:
    assert WINDOWS_UI_SCALE_FACTORS == (1.0, 1.25, 1.5)
    assert tk_scaling_for_windows_factor(1.0) == pytest.approx(4 / 3)
    assert tk_scaling_for_windows_factor(1.25) == pytest.approx(5 / 3)
    assert tk_scaling_for_windows_factor(1.5) == pytest.approx(2.0)
    with pytest.raises(ValueError, match="unsupported Windows UI scale factor"):
        tk_scaling_for_windows_factor(2.0)


def test_transient_geometry_stays_inside_parent_on_negative_coordinate_monitor() -> None:
    assert centered_transient_geometry(
        parent_x=-1920,
        parent_y=120,
        parent_width=1320,
        parent_height=820,
        width=1000,
        height=640,
    ) == "1000x640-1760+210"


class _ExistingWindow:
    def __init__(self) -> None:
        self.destroyed = False

    def winfo_exists(self) -> bool:
        return not self.destroyed

    def destroy(self) -> None:
        self.destroyed = True


def test_announcement_popup_waits_for_first_run_tutorial() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor._tutorial_window = _ExistingWindow()
    monitor._announcement_window = None
    monitor._pending_announcement_popup_rows = ()
    announcements = [SimpleNamespace(announcement_id="first-run")]

    monitor._show_announcement_popup(announcements)

    assert monitor._pending_announcement_popup_rows == tuple(announcements)
    assert monitor._announcement_window is None


def test_closing_tutorial_releases_one_deferred_announcement_popup() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    tutorial = _ExistingWindow()
    pending = (SimpleNamespace(announcement_id="after-tutorial"),)
    shown: list[object] = []
    monitor.tutorial_completed = False
    monitor._tutorial_window = tutorial
    monitor._pending_announcement_popup_rows = pending
    monitor._closing = False
    monitor.root = _IdleRoot()
    monitor._show_announcement_popup = lambda rows: shown.extend(rows)

    monitor._close_tutorial(False)

    assert tutorial.destroyed is True
    assert monitor._tutorial_window is None
    assert monitor._pending_announcement_popup_rows == ()
    assert shown == list(pending)


def test_chat_bubbles_expand_responsively_without_filling_the_screen() -> None:
    assert chat_bubble_metrics(720) == (489, 86)
    assert chat_bubble_metrics(1920) == (1100, 220)
    assert chat_bubble_metrics(300) == (420, 57)


def test_agent_mcp_access_mode_distinguishes_native_tool_loop_and_inference() -> None:
    assert agent_mcp_access_mode(
        ({"transport": "stdio", "client_name": "codex-desktop"},), ()
    ) == MCP_NATIVE
    assert agent_mcp_access_mode(
        (),
        ({"client_name": "openai-compatible", "provider_id": "relay-grok"},),
    ) == MCP_TOOL_LOOP
    assert agent_mcp_access_mode(
        (),
        (
            {
                "client_name": "claude",
                "provider_id": "ccswitch-claude-example",
            },
        ),
    ) == INFERENCE_ONLY
    assert agent_mcp_access_mode(
        (),
        (
            {
                "client_name": "codex",
                "provider_id": "ccswitch-codex-example",
            },
        ),
    ) == INFERENCE_ONLY
    assert agent_mcp_access_mode((), ({"client_name": "browser"},)) == MCP_UNVERIFIED


def test_tool_loop_identity_is_not_upgraded_by_internal_stdio_transport() -> None:
    assert agent_mcp_access_mode(
        ({"transport": "stdio", "client_name": "openai-compatible"},), ()
    ) == MCP_TOOL_LOOP


def test_live_native_session_outranks_saved_inference_fallback() -> None:
    assert agent_mcp_access_mode(
        ({"transport": "stdio", "client_name": "codex-desktop"},),
        ({"client_name": "codex", "provider_id": "ccswitch-codex-example"},),
    ) == MCP_NATIVE


def test_incremental_render_mode_appends_only_a_stable_suffix() -> None:
    assert (
        incremental_render_mode(("a", "b"), ("a", "b", "c"), same_context=True)
        == "append"
    )
    assert (
        incremental_render_mode(("a", "b"), ("x", "a", "b"), same_context=True)
        == "rebuild"
    )
    assert incremental_render_mode((), ("a",), same_context=True) == "rebuild"
    assert incremental_render_mode(None, ("a",), same_context=True) == "rebuild"


def test_sidebar_stats_keep_every_counter_in_six_compact_lines() -> None:
    text = compact_sidebar_stats(
        online=3,
        total_agents=5,
        rooms=2,
        messages=54,
        dispatch="RUN0/RETRY0/FAIL17/DONE15",
        memories=9,
        open_calls=1,
        active_tasks=1,
        audit_events=166_991,
        sync="08-15 16:32:27",
    )

    assert text.splitlines() == [
        "ONLINE 3/5  ROOMS 2",
        "MESSAGES 54  MEMORY 9",
        "DISPATCH RUN0/RETRY0/FAIL17/DONE15",
        "OPEN CALL 1  ACTIVE 1",
        "AUDIT 166991",
        "SYNC 08-15 16:32:27",
    ]


def test_sidebar_stats_accept_localized_status_labels() -> None:
    text = compact_sidebar_stats(
        online=3,
        total_agents=5,
        rooms=2,
        messages=54,
        dispatch="執行0/重試0/失敗17/完成15",
        memories=9,
        open_calls=1,
        active_tasks=1,
        audit_events=166_991,
        sync="08-15 16:32:27",
        labels={
            "online": "在線",
            "rooms": "房間",
            "messages": "訊息",
            "memory": "記憶",
            "dispatch": "派送",
            "open_calls": "待處理呼叫",
            "active": "進行中",
            "audit": "審計",
            "sync": "同步",
        },
    )

    assert text.splitlines() == [
        "在線 3/5  房間 2",
        "訊息 54  記憶 9",
        "派送 執行0/重試0/失敗17/完成15",
        "待處理呼叫 1  進行中 1",
        "審計 166991",
        "同步 08-15 16:32:27",
    ]


def test_chat_refresh_with_identical_rows_touches_no_widgets() -> None:
    row = {"time": "2026-08-15T00:00:00Z", "sha": "a" * 64, "body": "same"}
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor._chat_render_query = ""
    monitor._chat_render_row_signatures = (ui_content_signature(row),)
    monitor._chat_records = lambda: [row]
    monitor.chat_inner = _StableInner()

    monitor._render_chat("")


def test_reopening_chat_follows_latest_without_rebuilding_widgets() -> None:
    row = {"time": "2026-08-15T00:00:00Z", "sha": "a" * 64, "body": "same"}
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.selected_room_id = "room-a"
    monitor._chat_render_room_id = "room-a"
    monitor._chat_render_query = ""
    monitor._chat_render_row_signatures = (ui_content_signature(row),)
    monitor._chat_follow_latest_on_open = True
    monitor._chat_records = lambda: [row]
    monitor.chat_inner = _StableInner()
    monitor.chat_canvas = _Viewport((0.0, 0.5))
    monitor.root = _IdleRoot()

    monitor._render_chat("")

    assert monitor.chat_canvas.moves == [1.0]
    assert monitor._chat_follow_latest_on_open is False


def test_open_chat_unchanged_refresh_preserves_reader_position() -> None:
    row = {"time": "2026-08-15T00:00:00Z", "sha": "a" * 64, "body": "same"}
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.selected_room_id = "room-a"
    monitor._chat_render_room_id = "room-a"
    monitor._chat_render_query = ""
    monitor._chat_render_row_signatures = (ui_content_signature(row),)
    monitor._chat_follow_latest_on_open = False
    monitor._chat_records = lambda: [row]
    monitor.chat_inner = _StableInner()
    monitor.chat_canvas = _Viewport((0.0, 0.5))
    monitor.root = _IdleRoot()

    monitor._render_chat("")

    assert monitor.chat_canvas.moves == []


def test_chat_layout_rebuilds_only_when_history_width_changes() -> None:
    class Root:
        @staticmethod
        def update_idletasks() -> None:
            return None

    class Composer:
        @staticmethod
        def winfo_reqheight() -> int:
            return 220

    class Split:
        @staticmethod
        def paneconfigure(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def winfo_height() -> int:
            return 800

        @staticmethod
        def sash_place(*_args: object) -> None:
            return None

    class Canvas:
        width = 900

        @staticmethod
        def configure(**_kwargs: object) -> None:
            return None

        @staticmethod
        def bbox(_value: str) -> tuple[int, int, int, int]:
            return (0, 0, 900, 600)

        @classmethod
        def winfo_width(cls) -> int:
            return cls.width

    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.root = Root()
    monitor.chat_composer = Composer()
    monitor.chat_split = Split()
    monitor.chat_canvas = Canvas()
    monitor._reflow_chat_composer = lambda **_kwargs: False
    monitor._resize_chat_page_viewport = lambda: None
    monitor.snapshot = object()
    monitor.active_page = "chat"
    monitor.search = SimpleNamespace(get=lambda: "")
    monitor._chat_layout_history_width = 0
    rendered: list[str] = []
    monitor._render_chat = rendered.append

    monitor._layout_chat_after_resize()
    assert rendered == [""]

    monitor._chat_render_row_signatures = ("stable",)
    monitor._layout_chat_after_resize()
    assert rendered == [""]
    assert monitor._chat_render_row_signatures == ("stable",)

    Canvas.width = 910
    monitor._layout_chat_after_resize()
    assert rendered == ["", ""]
    assert monitor._chat_render_row_signatures is None


def test_chat_refresh_appends_only_new_suffix() -> None:
    old = {"time": "2026-08-15T00:00:00Z", "sha": "a" * 64, "body": "old"}
    new = {"time": "2026-08-15T00:00:01Z", "sha": "b" * 64, "body": "new"}
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor._chat_render_query = ""
    monitor._chat_render_row_signatures = (ui_content_signature(old),)
    monitor._chat_records = lambda: [old, new]
    monitor.chat_inner = _StableInner()
    monitor.chat_canvas = _Viewport()
    monitor.root = _IdleRoot()
    appended: list[dict[str, str]] = []
    monitor._add_bubble = appended.append

    monitor._render_chat("")

    assert appended == [new]
    assert monitor.chat_canvas.moves == [1.0]


def test_chat_refresh_rebuilds_identical_rows_after_room_switch() -> None:
    row = {"time": "2026-08-15T00:00:00Z", "sha": "a" * 64, "body": "same"}
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.selected_room_id = "new-room"
    monitor._chat_render_room_id = "old-room"
    monitor._chat_render_query = ""
    monitor._chat_render_row_signatures = (ui_content_signature(row),)
    monitor._chat_records = lambda: [row]
    child = _DestroyableChild()
    monitor.chat_inner = _RebuildInner(child)
    monitor.chat_canvas = _Viewport((0.25, 0.75))
    monitor.root = _IdleRoot()
    rendered: list[dict[str, str]] = []
    monitor._add_bubble = rendered.append

    monitor._render_chat("")

    assert child.destroyed is True
    assert rendered == [row]
    assert monitor._chat_render_room_id == "new-room"
    assert monitor.chat_canvas.moves == [1.0]


def test_delayed_chat_scroll_from_previous_room_is_ignored() -> None:
    old = {"time": "2026-08-15T00:00:00Z", "sha": "a" * 64, "body": "old"}
    new = {"time": "2026-08-15T00:00:01Z", "sha": "b" * 64, "body": "new"}
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.selected_room_id = "old-room"
    monitor._chat_render_room_id = ""
    monitor._chat_render_query = ""
    monitor._chat_render_row_signatures = None
    current_rows = [old]
    monitor._chat_records = lambda: current_rows
    monitor.chat_canvas = _Viewport((0.25, 0.75))
    monitor.root = _QueuedIdleRoot()
    child = _DestroyableChild()
    monitor.chat_inner = _RebuildInner(child)
    monitor._add_bubble = lambda _row: None

    monitor._render_chat("")
    monitor.selected_room_id = "new-room"
    current_rows[:] = [new]
    monitor._render_chat("")

    old_scroll, new_scroll = monitor.root.callbacks
    old_scroll()
    assert monitor.chat_canvas.moves == []
    new_scroll()
    assert monitor.chat_canvas.moves == [1.0]


def test_async_room_rows_follow_latest_after_empty_room_placeholder() -> None:
    row = {"time": "2026-08-15T00:00:00Z", "sha": "a" * 64, "body": "loaded"}
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.selected_room_id = "room-a"
    monitor._chat_render_room_id = "room-a"
    monitor._chat_render_query = ""
    monitor._chat_render_row_signatures = ()
    monitor._chat_records = lambda: [row]
    child = _DestroyableChild()
    monitor.chat_inner = _RebuildInner(child)
    monitor.chat_canvas = _Viewport((0.0, 0.5))
    monitor.root = _IdleRoot()
    rendered: list[dict[str, str]] = []
    monitor._add_bubble = rendered.append

    monitor._render_chat("")

    assert child.destroyed is True
    assert rendered == [row]
    assert monitor.chat_canvas.moves == [1.0]


def test_chat_records_never_hide_room_messages_also_present_as_peer_calls() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.snapshot = SimpleNamespace(
        peer_calls=({"request_id": "req-1"},), message_dispatches=()
    )
    monitor.selected_room_id = "review-room"
    monitor._room_messages = (
        {
            "room_id": "review-room",
            "created_utc": "2026-08-15T00:00:00Z",
            "sender": "claude-code",
            "recipient": "codex-main",
            "task_id": "review",
            "subject": "Visible response",
            "body": '{"request_id":"req-1","result":"keep me"}',
            "content_sha256": "a" * 64,
            "acknowledged": True,
        },
    )

    rows = monitor._chat_records()

    assert len(rows) == 1
    assert rows[0]["body"] == '{"request_id":"req-1","result":"keep me"}'


def test_cockpit_sessions_include_room_and_conversation_names() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.scope = "project"
    monitor.selected_room_id = "review-room"
    monitor._rooms = {
        "review-room": {"room_id": "review-room", "name": "Release Review"}
    }
    monitor._room_members = (
        {
            "agent_id": "codex-main",
            "room_session_id": "room-session-one",
            "status": "active",
        },
    )
    monitor._room_messages = ()
    monitor.authorized_sessions = SimpleNamespace(
        list_for_control_room=lambda **_kwargs: (
            {
                "source_type": "authorized-terminal",
                "source_session_id": "terminal-one",
                "source_conversation_id": "conversation-one",
                "display_name": "PowerShell release check",
                "room_id": "review-room",
            },
            {
                "source_type": "authorized-terminal",
                "source_session_id": "terminal-other-room",
                "source_conversation_id": "conversation-other",
                "display_name": "Other room terminal",
                "room_id": "other-room",
            },
        )
    )

    sessions = monitor._cockpit_external_sessions({})
    native = next(row for row in sessions if row["source_type"] == "peerbridge-room")
    external = next(
        row for row in sessions if row["source_type"] == "authorized-terminal"
    )

    assert native["room_name"] == "Release Review"
    assert native["source_conversation_name"] == "Release Review"
    assert external["room_name"] == "Release Review"
    assert external["source_conversation_name"] == "PowerShell release check"
    assert all(
        row.get("source_session_id") != "terminal-other-room" for row in sessions
    )


def test_cockpit_lobby_uses_implicit_human_operator_for_room_input() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.scope = "project"
    monitor.selected_room_id = "lobby"
    monitor._rooms = {"lobby": {"room_id": "lobby", "name": "Lobby"}}
    monitor._room_members = (
        {
            "agent_id": "grok-relay",
            "room_session_id": "grok-session",
            "route_profile_id": "grok-route",
            "status": "active",
        },
    )
    monitor._room_messages = ()
    monitor.snapshot = SimpleNamespace(message_dispatches=())
    monitor.authorized_sessions = SimpleNamespace(
        list_for_control_room=lambda **_kwargs: ()
    )

    sessions = monitor._cockpit_external_sessions({})

    assert len(sessions) == 1
    assert sessions[0]["capabilities"]["input_capable"] is True


def test_cockpit_lobby_send_accepts_implicit_human_operator() -> None:
    sent: dict[str, object] = {}

    class HumanClient:
        @staticmethod
        def room_members(**_kwargs: object) -> dict[str, object]:
            return {
                "members": (
                    {
                        "agent_id": "grok-relay",
                        "status": "active",
                        "room_session_id": "grok-session",
                        "route_profile_id": "grok-route",
                    },
                )
            }

        @staticmethod
        def send_message(**kwargs: object) -> dict[str, object]:
            sent.update(kwargs)
            return {"room_id": kwargs["room_id"], "message_id": "message-one"}

    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.human_client = HumanClient()

    receipt = monitor._send_cockpit_room_message(
        {
            "source_type": "peerbridge-room",
            "source_session_id": "grok-session",
            "room_id": "lobby",
            "agent_id": "grok-relay",
            "requested_route": "grok-route",
        },
        "Continue independently.",
        (".peerbridge-artifacts/chat/evidence.txt",),
    )

    assert receipt["message_id"] == "message-one"
    assert sent["recipient"] == "grok-relay"
    assert sent["body"] == "Continue independently."
    assert sent["artifact_paths"] == (
        ".peerbridge-artifacts/chat/evidence.txt",
    )


def test_cockpit_room_input_revalidates_exact_member_and_route_before_sending() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class HumanClient:
        @staticmethod
        def room_members(**kwargs: object) -> dict[str, object]:
            calls.append(("room_members", kwargs))
            return {
                "members": (
                    {
                        "agent_id": "human-operator",
                        "status": "active",
                        "room_session_id": "human-session",
                    },
                    {
                        "agent_id": "grok-relay",
                        "status": "active",
                        "room_session_id": "grok-session",
                        "route_profile_id": "grok-route",
                    },
                )
            }

        @staticmethod
        def send_message(**kwargs: object) -> dict[str, object]:
            calls.append(("send_message", kwargs))
            return {"room_id": kwargs["room_id"], "message_id": "message-one"}

    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.human_client = HumanClient()
    receipt = monitor._send_cockpit_room_message(
        {
            "source_type": "peerbridge-room",
            "source_session_id": "grok-session",
            "room_id": "review-room",
            "agent_id": "grok-relay",
            "requested_route": "grok-route",
        },
        "Review this exact change.",
    )

    assert receipt["message_id"] == "message-one"
    assert calls[0] == (
        "room_members",
        {"room_id": "review-room", "include_inactive": False},
    )
    sent = calls[1][1]
    assert sent["room_id"] == "review-room"
    assert sent["recipient"] == "grok-relay"
    assert sent["route_profile_id"] == "grok-route"
    assert sent["body"] == "Review this exact change."
    assert str(sent["task_id"]).startswith("cockpit-")


@pytest.mark.parametrize("drift", ["operator-left", "route-changed", "session-rotated"])
def test_cockpit_room_input_fails_closed_when_live_binding_drifts(
    drift: str,
) -> None:
    members: list[dict[str, object]] = [
        {
            "agent_id": "human-operator",
            "status": "active",
            "room_session_id": "human-session",
        },
        {
            "agent_id": "grok-relay",
            "status": "active",
            "room_session_id": "grok-session",
            "route_profile_id": "grok-route",
        },
    ]
    if drift == "operator-left":
        members[0]["status"] = "left"
    elif drift == "route-changed":
        members[1]["route_profile_id"] = "different-route"
    else:
        members[1]["room_session_id"] = "rotated-session"

    class HumanClient:
        @staticmethod
        def room_members(**_kwargs: object) -> dict[str, object]:
            return {"members": members}

        @staticmethod
        def send_message(**_kwargs: object) -> dict[str, object]:
            pytest.fail("drifted target was sent")

    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.human_client = HumanClient()

    with pytest.raises(RuntimeError, match="changed|not joined"):
        monitor._send_cockpit_room_message(
            {
                "source_type": "peerbridge-room",
                "source_session_id": "grok-session",
                "room_id": "review-room",
                "agent_id": "grok-relay",
                "requested_route": "grok-route",
            },
            "Review this exact change.",
        )


def test_chat_records_expose_terminal_dispatch_failure() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.snapshot = SimpleNamespace(
        message_dispatches=(
            {
                "message_id": "msg-1",
                "agent_id": "grok-relay",
                "status": "failed",
                "error_code": "tool_policy_failed",
                "attempt_count": 2,
                "reply_message_id": None,
            },
        )
    )
    monitor.selected_room_id = "lobby"
    monitor._t = lambda key: key
    monitor._room_messages = (
        {
            "message_id": "msg-1",
            "room_id": "lobby",
            "created_utc": "2026-08-15T16:05:02Z",
            "sender": "human-operator",
            "recipient": "grok-relay",
            "task_id": "red-team",
            "subject": "Attack review",
            "body": "Review this.",
            "content_sha256": "b" * 64,
            "acknowledged": False,
        },
    )

    row = monitor._chat_records()[0]

    assert row["dispatch_status"] == "failed"
    assert row["dispatch_error"] == "tool_policy_failed"
    assert row["dispatch_attempts"] == 2
    metadata = monitor._bubble_metadata(row)
    assert "DELIVERY: FAILED" in metadata
    assert "ERROR: tool_policy_failed" in metadata
    assert "ATTEMPTS: 2" in metadata
    assert monitor._dispatch_status_text(row) == "chat.delivery.tool_failed"


@pytest.mark.parametrize("status", ("retryable", "failed"))
@pytest.mark.parametrize(
    ("error_code", "expected_key"),
    (
        (
            "runner_hard_deadline_exceeded",
            "chat.delivery.runner_hard_deadline_exceeded",
        ),
        ("discussion_timed_out", "chat.delivery.discussion_timed_out"),
        ("provider_http_retryable", "chat.delivery.provider_unavailable"),
        ("provider_rate_limited", "chat.delivery.rate_limited"),
    ),
)
def test_chat_delivery_uses_exact_timeout_and_provider_states(
    status: str, error_code: str, expected_key: str
) -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor._t = lambda key: key

    text = monitor._dispatch_status_text(
        {
            "dispatch_status": status,
            "dispatch_error": error_code,
            "dispatch_attempts": 5,
            "recipient": "grok-relay",
        }
    )

    assert text == expected_key


def test_unseated_send_explains_how_to_join_instead_of_failing_silently() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.send_in_progress = False
    monitor._room_has_operator = lambda: False
    monitor._t = lambda key: "JOIN CONTROL" if key == "chat.join_to_send" else key
    monitor.message_status = _Value()
    monitor.message_status_label = _Configurable()

    monitor.send_human_message()

    assert monitor.message_status.value == "JOIN CONTROL"
    assert monitor.message_status_label.configurations[-1]["fg"]


class _RectWidget:
    def __init__(self, *, left: int, top: int, width: int, height: int) -> None:
        self._left = left
        self._top = top
        self._width = width
        self._height = height

    def winfo_rootx(self) -> int:
        return self._left

    def winfo_rooty(self) -> int:
        return self._top

    def winfo_width(self) -> int:
        return self._width

    def winfo_height(self) -> int:
        return self._height


def test_room_agent_leftward_drop_uses_the_full_sidebar() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.sidebar_frame = _RectWidget(left=10, top=20, width=230, height=760)

    assert monitor._drop_hits_global_library(12, 25) is True
    assert monitor._drop_hits_global_library(225, 760) is True
    assert monitor._drop_hits_global_library(250, 760) is False
    assert monitor._drop_hits_global_library(225, 790) is False


def test_room_agent_drop_on_sidebar_removes_the_selected_seat() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor._drag_agent_id = "kimi-relay"
    monitor._drag_action = "remove"
    monitor._drag_ghost = object()
    calls: list[tuple[str, str | None]] = []
    monitor._drop_hits_global_library = lambda _x, _y: True
    monitor._clear_library_drag = lambda: calls.append(("clear", None))
    monitor._remove_room_agent = lambda agent_id: calls.append(("remove", agent_id))
    monitor._select_room_agent_card = lambda agent_id: calls.append(("select", agent_id))

    monitor._finish_room_agent_drag(SimpleNamespace(x_root=30, y_root=300))

    assert calls == [("clear", None), ("remove", "kimi-relay")]


def test_library_drop_on_room_seats_applies_a_preselected_route() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor._drag_agent_id = "grok-relay"
    monitor._drag_action = "add"
    monitor._drag_ghost = object()
    monitor.selected_room_id = "review-room"
    monitor._seat_selected_route_id = "relay-grok-route"
    monitor._seat_selected_candidate = None
    calls: list[str] = []
    monitor._drop_hits_room_seats = lambda _x, _y: True
    monitor._clear_library_drag = lambda: calls.append("clear")
    monitor.add_room_seat = lambda: calls.append("add")
    monitor._show_agent_route_menu = lambda *_args, **_kwargs: calls.append("menu")

    monitor._finish_library_drag(SimpleNamespace(x_root=500, y_root=180))

    assert calls == ["clear", "add"]


def test_stale_room_action_callback_does_not_overwrite_new_room_state() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.room_action_in_progress = True
    monitor.selected_room_id = "new-room"
    target = _Value("new-room status")
    label = _Configurable()
    calls: list[str] = []
    monitor._sync_room_control_states = lambda: calls.append("sync")
    monitor._request_room_refresh = lambda **_kwargs: calls.append("refresh")

    monitor._finish_room_action(
        {"room_id": "old-room"},
        None,
        target,
        label,
        lambda _receipt: "old-room success",
        lambda _receipt: calls.append("after-success"),
        "old-room",
    )

    assert monitor.room_action_in_progress is False
    assert target.value == "new-room status"
    assert calls == ["sync"]
    assert label.configurations == []


def test_stale_send_token_callback_is_ignored() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.send_in_progress = True
    monitor._active_send_token = 2
    monitor.selected_room_id = "new-room"
    monitor.send_button = _Configurable()
    monitor._t = lambda _key: "SEND"
    calls: list[str] = []
    monitor._sync_room_control_states = lambda: calls.append("sync")
    monitor._request_room_refresh = lambda **_kwargs: calls.append("refresh")
    monitor.refresh = lambda **_kwargs: calls.append("full-refresh")

    monitor._finish_human_send(
        {"room_id": "old-room", "content_sha256": "a" * 64},
        None,
        "old-room",
        1,
        ("old draft", "task", "subject", "normal", ()),
    )

    assert monitor.send_in_progress is True
    assert monitor._active_send_token == 2
    assert calls == []
    assert monitor.send_button.configurations == []


def test_send_completion_after_room_switch_clears_only_unchanged_snapshot_once() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.send_in_progress = True
    monitor._active_send_token = 7
    monitor.selected_room_id = "new-room"
    monitor.send_button = _Configurable()
    monitor._t = lambda _key: "SEND"
    monitor.message_body = _Body("sent body")
    monitor.message_task = _Value("task")
    monitor.message_subject = _Value("subject")
    monitor.message_priority = _Value("normal")
    monitor._chat_attachment_paths = (Path("proof.txt"),)
    calls: list[str] = []
    monitor._sync_room_control_states = lambda: calls.append("sync")
    monitor._request_room_refresh = lambda **_kwargs: calls.append("refresh")
    monitor.refresh = lambda **_kwargs: calls.append("full-refresh")

    def clear_attachments() -> None:
        calls.append("clear")
        monitor._chat_attachment_paths = ()

    monitor._clear_chat_attachments = clear_attachments
    snapshot = monitor._send_draft_snapshot()
    receipt = {"room_id": "old-room", "content_sha256": "a" * 64}

    monitor._finish_human_send(receipt, None, "old-room", 7, snapshot)
    monitor._finish_human_send(receipt, None, "old-room", 7, snapshot)

    assert monitor.send_in_progress is False
    assert monitor._active_send_token is None
    assert monitor.message_body.value == ""
    assert monitor.message_body.deletes == 1
    assert monitor._chat_attachment_paths == ()
    assert calls == ["sync", "clear"]


def test_send_completion_preserves_draft_edited_while_request_was_running() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.send_in_progress = True
    monitor._active_send_token = 9
    monitor.selected_room_id = "room-a"
    monitor.send_button = _Configurable()
    monitor._t = lambda _key: "SEND"
    monitor.message_body = _Body("original")
    monitor.message_task = _Value("task")
    monitor.message_subject = _Value("subject")
    monitor.message_priority = _Value("normal")
    monitor._chat_attachment_paths = ()
    snapshot = monitor._send_draft_snapshot()
    monitor.message_body.value = "new draft"
    monitor.message_status = _Value()
    monitor.message_status_label = _Configurable()
    calls: list[str] = []
    monitor._sync_room_control_states = lambda: calls.append("sync")
    monitor._clear_chat_attachments = lambda: calls.append("clear")
    monitor._request_room_refresh = lambda **_kwargs: calls.append("refresh")
    monitor.refresh = lambda **_kwargs: calls.append("full-refresh")

    monitor._finish_human_send(
        {"room_id": "room-a", "content_sha256": "b" * 64},
        None,
        "room-a",
        9,
        snapshot,
    )

    assert monitor.message_body.value == "new draft"
    assert monitor.message_body.deletes == 0
    assert calls == ["sync", "refresh", "full-refresh"]


def test_send_completion_replays_forced_refresh_after_an_inflight_poll() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.send_in_progress = True
    monitor._active_send_token = 11
    monitor.selected_room_id = "room-a"
    monitor.send_button = _Configurable()
    monitor._t = lambda key: key
    monitor.message_body = _Body("sent body")
    monitor.message_task = _Value("task")
    monitor.message_subject = _Value("subject")
    monitor.message_priority = _Value("normal")
    monitor.message_status = _Value()
    monitor.message_status_label = _Configurable()
    monitor._chat_attachment_paths = ()
    monitor._clear_chat_attachments = lambda: None
    monitor._sync_room_control_states = lambda: None
    monitor.room_refresh_in_progress = True
    monitor._room_refresh_pending = False
    monitor.refresh = lambda **_kwargs: None
    snapshot = monitor._send_draft_snapshot()
    monitor.message_body.value = "next draft"

    monitor._finish_human_send(
        {"room_id": "room-a", "content_sha256": "c" * 64},
        None,
        "room-a",
        11,
        snapshot,
    )

    assert monitor._room_refresh_pending is True

    replayed: list[dict[str, object]] = []
    monitor._request_room_refresh = lambda **kwargs: replayed.append(kwargs)
    monitor.root = _IdleRoot()
    monitor._closing = False
    monitor._last_room_refresh = 0.0
    monitor.room_status = _Value()
    monitor.room_status_label = _Configurable()
    monitor._finish_room_refresh(
        None,
        "room-a",
        (),
        (),
        (),
        {},
        {},
        RuntimeError("old poll failed"),
    )

    assert monitor._room_refresh_pending is False
    assert replayed == [{"force": True}]


def test_model_and_reasoning_transitions_clear_stale_or_ambiguous_route() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.message_route_profile = _Value("stale-route")
    monitor.message_provider = _Value("provider-a")
    monitor.message_model = _Value("model-a")
    monitor.message_reasoning = _Value("high")
    monitor.reasoning_combo = _Configurable()
    profiles = [
        {
            "route_id": "route-a",
            "provider_id": "provider-a",
            "model_id": "model-a",
            "reasoning_mode": "high",
        },
        {
            "route_id": "route-duplicate",
            "provider_id": "provider-a",
            "model_id": "model-a",
            "reasoning_mode": "high",
        },
    ]
    monitor._scope_profiles = lambda: profiles

    monitor._on_model_selected()
    assert monitor.message_route_profile.get() == "DIRECT"

    profiles.pop()
    monitor.message_route_profile.set("stale-route")
    monitor._on_reasoning_selected()
    assert monitor.message_route_profile.get() == "route-a"

    monitor.message_route_profile.set("route-a")
    monitor.message_reasoning.set("unsupported")
    monitor._on_reasoning_selected()
    assert monitor.message_route_profile.get() == "DIRECT"


def test_send_fails_closed_when_route_selection_is_not_unique() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.send_in_progress = False
    monitor._room_has_operator = lambda: True
    monitor.message_body = _Body("payload")
    monitor.message_task = _Value("task")
    monitor.message_subject = _Value("subject")
    monitor.message_priority = _Value("normal")
    monitor._chat_attachment_paths = ()
    monitor.message_route_profile = _Value("stale-route")
    monitor.message_provider = _Value("provider-a")
    monitor.message_model = _Value("model-a")
    monitor.message_reasoning = _Value("high")
    monitor._scope_profiles = lambda: ()
    monitor._t = lambda key: f"translated:{key}"
    monitor.message_status = _Value()
    monitor.message_status_label = _Configurable()

    monitor.send_human_message()

    assert monitor.message_route_profile.get() == "DIRECT"
    assert monitor.message_status.get() == "translated:chat.route_unique_required"
    assert monitor.message_status_label.configurations[-1] == {"fg": "#ff6b6b"}


def test_provider_display_label_is_unique_even_when_display_names_collide() -> None:
    connections = (
        {
            "connection_id": "relay-a",
            "provider_id": "relay-a",
            "display_name": "Shared Relay",
            "enabled": True,
        },
        {
            "connection_id": "relay-b",
            "provider_id": "relay-b",
            "display_name": "Shared Relay",
            "enabled": True,
        },
    )

    first = provider_display_label(
        {"provider_id": "relay-a", "route_class": "relay"}, connections
    )
    second = provider_display_label(
        {"provider_id": "relay-b", "route_class": "relay"}, connections
    )

    assert first == "RELAY | Shared Relay [relay-a]"
    assert second == "RELAY | Shared Relay [relay-b]"
    assert first != second


def test_attachment_history_labels_expose_only_verified_content_sha_names() -> None:
    digest = "a" * 64

    labels = safe_chat_artifact_labels(
        (
            f".peerbridge-artifacts/chat/{digest}.txt",
            "../../customer-secret-name.png",
        )
    )

    assert labels == (
        f"{digest[:16]}.txt // SHA {digest[:16]}",
        "UNVERIFIED ATTACHMENT 2",
    )
    assert "customer" not in " ".join(labels)


def test_chat_attachment_dialog_is_owned_by_control_room(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.root = object()
    monitor._chat_attachment_paths = ()
    monitor.chat_attachment_status = _Value()
    monitor._t = lambda key: key
    monitor._sync_room_control_states = lambda: None
    selected_path = tmp_path / "chart.png"
    captured: dict[str, object] = {}

    def choose(**kwargs: object) -> tuple[str, ...]:
        captured.update(kwargs)
        return (str(selected_path),)

    monkeypatch.setattr("peerbridge_mcp.monitor.filedialog.askopenfilenames", choose)

    monitor._choose_chat_attachments()

    assert captured["parent"] is monitor.root
    assert monitor._chat_attachment_paths == (selected_path,)
    assert monitor.chat_attachment_status.get() == "chat.attachments_selected"


def test_room_agent_overflow_retains_every_hidden_seat_for_menu_actions() -> None:
    cards = tuple({"agent_id": f"agent-{index}"} for index in range(9))

    visible, overflow = room_agent_card_groups(cards)

    assert [row["agent_id"] for row in visible] == [
        "agent-0",
        "agent-1",
        "agent-2",
        "agent-3",
        "agent-4",
    ]
    assert [row["agent_id"] for row in overflow] == [
        "agent-5",
        "agent-6",
        "agent-7",
        "agent-8",
    ]


def test_room_agent_capacity_uses_real_strip_width_before_showing_overflow() -> None:
    assert room_agent_visible_limit(1500, 7) == 7
    assert room_agent_visible_limit(1416, 7) == 7
    assert room_agent_visible_limit(980, 7) < 7
    assert room_agent_visible_limit(1, 7) == 5
    assert room_agent_visible_limit(1500, 0) == 0


def test_global_agent_canvas_keeps_more_than_eight_agents_operable() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.agent_canvas = _Canvas()
    monitor.seat_agent = _Value("")
    monitor._last_agent_canvas_signature = ""
    rows = [
        {
            "agent_id": f"agent-{index:02d}",
            "last_seen_epoch": float(index),
            "provider_id": "provider-a",
        }
        for index in range(12)
    ]

    monitor._draw_agents(rows)

    assert len(monitor._library_hitboxes) == 12
    assert AGENT_LIBRARY_VISIBLE_CAPACITY == 6
    first_screen = monitor._library_hitboxes[:AGENT_LIBRARY_VISIBLE_CAPACITY]
    overflow = monitor._library_hitboxes[AGENT_LIBRARY_VISIBLE_CAPACITY:]
    assert len({left for left, _top, _right, _bottom, _name in first_screen}) == 2
    assert len({top for _left, top, _right, _bottom, _name in first_screen}) == 3
    assert all(bottom <= AGENT_LIBRARY_CANVAS_HEIGHT for _left, _top, _right, bottom, _name in first_screen)
    assert all(top >= AGENT_LIBRARY_CANVAS_HEIGHT for _left, top, _right, _bottom, _name in overflow)
    assert monitor.agent_canvas.configurations[-1]["scrollregion"] == (
        0,
        0,
        AGENT_LIBRARY_CANVAS_WIDTH,
        max(
            AGENT_LIBRARY_CANVAS_HEIGHT,
            AGENT_LIBRARY_TOP_MARGIN
            + ((12 + AGENT_LIBRARY_COLUMNS - 1) // AGENT_LIBRARY_COLUMNS)
            * AGENT_LIBRARY_CARD_STRIDE,
        ),
    )


def test_global_agent_scrollbar_only_appears_after_six_agents() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.agent_canvas = _Canvas()
    monitor.agent_scrollbar = _PackableScrollbar()
    monitor.seat_agent = _Value("")
    monitor._last_agent_canvas_signature = ""

    six_rows = [
        {"agent_id": f"agent-{index:02d}", "last_seen_epoch": float(index)}
        for index in range(AGENT_LIBRARY_VISIBLE_CAPACITY)
    ]
    monitor._draw_agents(six_rows)

    assert monitor.agent_scrollbar.winfo_manager() == ""
    assert monitor.agent_scrollbar.forget_calls == 1

    monitor._draw_agents(
        six_rows
        + [
            {
                "agent_id": "agent-06",
                "last_seen_epoch": float(AGENT_LIBRARY_VISIBLE_CAPACITY),
            }
        ]
    )

    assert monitor.agent_scrollbar.winfo_manager() == "pack"
    assert monitor.agent_scrollbar.pack_calls == 1


def test_local_provider_save_uses_loopback_descriptor_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.connection_in_progress = False
    monitor.connection_id = _Value("local-ollama")
    monitor.connection_name = _Value("Local Ollama")
    monitor.connection_class = _Value("local")
    monitor.connection_endpoint = _Value("http://127.0.0.1:11434/v1")
    monitor.connection_api_key = _Value("")
    monitor.connection_status = _Value()
    monitor.connection_status_label = _Configurable()
    monitor.scope = "test-scope"
    monitor._t = lambda key: key
    calls: list[tuple[str, dict[str, object]]] = []
    stored: list[dict[str, object]] = []
    posted: list[tuple[object, ...]] = []

    reference = SimpleNamespace(
        provider_id="local-ollama",
        credential_target="PeerBridgeMCP:v2:test",
        endpoint_sha256="a" * 64,
        credential_fingerprint_sha256="b" * 64,
        descriptor_schema="peerbridge.provider-credential.v2",
        credential_version_sha256="c" * 64,
    )

    def store_local(**kwargs: object) -> object:
        stored.append(kwargs)
        return reference

    def reject_remote_store(**_kwargs: object) -> object:
        raise AssertionError("local providers must not enter the API-key store")

    class HumanClient:
        @staticmethod
        def call_tool(tool: str, payload: dict[str, object]) -> dict[str, object]:
            calls.append((tool, payload))
            return {"connection_id": "local-ollama", "connection_sha256": "d" * 64}

    class ImmediateThread:
        def __init__(self, *, target: object, **_kwargs: object) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    monitor.human_client = HumanClient()
    monitor._post_to_ui = lambda _callback, *args: posted.append(args) or True
    monkeypatch.setattr("peerbridge_mcp.monitor.store_local_provider_endpoint", store_local)
    monkeypatch.setattr("peerbridge_mcp.monitor.store_provider_credentials", reject_remote_store)
    monkeypatch.setattr(
        "peerbridge_mcp.monitor.discover_provider_models",
        lambda **_kwargs: SimpleNamespace(models=("model-a",), registry_sha256="e" * 64),
    )
    monkeypatch.setattr("peerbridge_mcp.monitor.threading.Thread", ImmediateThread)

    monitor.save_native_connection()

    assert stored == [
        {
            "scope": "test-scope",
            "connection_id": "local-ollama",
            "endpoint": "http://127.0.0.1:11434/v1",
            "provider_id": "local-ollama",
        }
    ]
    assert calls[0][0] == "upsert_provider_connection"
    assert calls[0][1]["route_class"] == "local"
    assert calls[0][1]["credential_target"] == "PeerBridgeMCP:v2:test"
    assert posted and posted[0][-1] is None


def test_api_key_visibility_toggle_is_presentational_only() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.connection_api_key = _Value("provider-secret-stays-in-memory")
    monitor.connection_key_visible = _Value(False)
    monitor.connection_key_entry = _OptionWidget(show="*")
    monitor.connection_key_visibility_button = _OptionWidget(text="顯示")
    monitor._t = lambda key: {
        "provider.show_api_key": "顯示",
        "provider.hide_api_key": "隱藏",
    }[key]

    monitor._toggle_connection_key_visibility()

    assert monitor.connection_key_entry.cget("show") == ""
    assert monitor.connection_key_visibility_button.cget("text") == "隱藏"
    assert monitor.connection_api_key.get() == "provider-secret-stays-in-memory"

    monitor._toggle_connection_key_visibility()

    assert monitor.connection_key_entry.cget("show") == "*"
    assert monitor.connection_key_visibility_button.cget("text") == "顯示"
    assert monitor.connection_api_key.get() == "provider-secret-stays-in-memory"


def test_local_provider_save_rejects_non_loopback_endpoint_before_worker_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.connection_in_progress = False
    monitor.connection_id = _Value("local-remote")
    monitor.connection_name = _Value("Not Local")
    monitor.connection_class = _Value("local")
    monitor.connection_endpoint = _Value("https://provider.example/v1")
    monitor.connection_api_key = _Value("")
    monitor.connection_status = _Value()
    monitor.connection_status_label = _Configurable()
    monitor._t = lambda key: key
    started: list[bool] = []

    class ForbiddenThread:
        def __init__(self, **_kwargs: object) -> None:
            started.append(True)

    monkeypatch.setattr("peerbridge_mcp.monitor.threading.Thread", ForbiddenThread)

    monitor.save_native_connection()

    assert monitor.connection_in_progress is False
    assert monitor.connection_status.get() == "provider.local_loopback_only"
    assert monitor.connection_status_label.configurations[-1] == {"fg": "#ff6b6b"}
    assert started == []


def test_catalog_failures_leave_querying_state_and_refresh_route_views() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor._codex_catalog_inflight = True
    monitor._codex_catalog_retry_at = 0.0
    monitor._codex_catalog_error = None
    monitor._provider_discovery_inflight = {"relay-a"}
    monitor._provider_discovery_errors = {}
    monitor._provider_discovery_retry_at = {}
    monitor.seat_agent = _Value("agent-a")
    monitor._safe_error = lambda _error: "redacted failure"
    calls: list[str] = []
    monitor._on_seat_agent_selected = lambda: calls.append("selected")
    monitor._render_room_agent_cards = lambda: calls.append("cards")

    monitor._finish_codex_model_discovery(None, RuntimeError("catalog failed"))
    monitor._finish_provider_model_discovery(
        "relay-a", "version-a", None, RuntimeError("provider failed")
    )

    assert monitor._codex_catalog_inflight is False
    assert monitor._codex_catalog_error == "redacted failure"
    assert monitor._provider_discovery_inflight == set()
    assert monitor._provider_discovery_errors == {"relay-a": "redacted failure"}
    assert calls == ["selected", "cards", "selected", "cards"]


def test_worker_callbacks_run_only_when_the_main_thread_pump_drains() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.root = _PumpRoot()
    monitor._closing = False
    monitor._ui_generation = 7
    monitor._ui_pump_after_id = None
    monitor._ui_callbacks = queue.SimpleQueue()
    calls: list[tuple[str, int]] = []

    assert monitor._post_to_ui(lambda label, value: calls.append((label, value)), "ok", 3)
    assert calls == []

    monitor._drain_ui_callbacks()

    assert calls == [("ok", 3)]
    assert monitor.root.after_calls
    assert monitor.root.after_calls[-1][0] == 50


def test_ui_pump_discards_stale_generation_and_reports_callback_errors() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.root = _PumpRoot()
    monitor._closing = False
    monitor._ui_generation = 2
    monitor._ui_pump_after_id = None
    monitor._ui_callbacks = queue.SimpleQueue()
    calls: list[str] = []
    monitor._ui_callbacks.put((1, lambda: calls.append("stale"), ()))
    monitor._ui_callbacks.put((2, lambda: (_ for _ in ()).throw(ValueError("bad")), ()))

    monitor._drain_ui_callbacks()

    assert calls == []
    assert len(monitor.root.reported) == 1
    assert str(monitor.root.reported[0]) == "bad"


def test_close_invalidates_queued_callbacks_and_is_idempotent() -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.root = _PumpRoot()
    monitor.reader = _ClosableReader()
    monitor._closing = False
    monitor._ui_generation = 4
    monitor._ui_pump_after_id = "pump-token"
    monitor._refresh_after_id = "refresh-token"
    monitor._ui_callbacks = queue.SimpleQueue()
    cockpit = SimpleNamespace(close_count=0)
    cockpit.close = lambda: setattr(cockpit, "close_count", cockpit.close_count + 1)
    monitor.cockpit = cockpit
    calls: list[str] = []
    assert monitor._post_to_ui(lambda: calls.append("late"))

    monitor.close()
    assert monitor._post_to_ui(lambda: calls.append("too-late")) is False
    monitor.close()

    assert monitor._ui_generation == 5
    assert monitor.root.cancelled == ["refresh-token", "pump-token"]
    assert monitor.root.destroy_count == 1
    assert monitor.reader.close_count == 1
    assert cockpit.close_count == 1
    assert calls == []


def test_guided_room_workflow_applies_bounds_before_posting_one_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.room_action_in_progress = False
    monitor._active_discussion = None
    monitor.selected_room_id = "room-one"
    raw_members = [
        {
            "agent_id": agent_id,
            "status": "active",
            "room_session_id": f"session-{agent_id}",
            "membership_sha256": hashlib.sha256(
                f"membership:{agent_id}".encode("utf-8")
            ).hexdigest(),
            "route_profile_id": route_id,
            "route_profile_sha256": hashlib.sha256(
                f"route:{route_id}".encode("utf-8")
            ).hexdigest(),
            "role_id": "equal-participant",
            "role_label": None,
        }
        for agent_id, route_id in (("agent-a", "route-a"), ("agent-b", "route-b"))
    ]
    participants = tuple(
        {
            "agent_id": member["agent_id"],
            "room_session_id": member["room_session_id"],
            "membership_sha256": member["membership_sha256"],
            "route_profile_id": member["route_profile_id"],
            "route_profile_sha256": member["route_profile_sha256"],
            "role_id": member["role_id"],
            "role_label": member["role_label"],
            "role_binding_sha256": hashlib.sha256(
                b'{"role_id":"equal-participant","role_label":null}'
            ).hexdigest(),
        }
        for member in raw_members
    )
    monitor._room_members = raw_members
    monitor.message_body = _Body("Compare the current user flow.")
    monitor.guided_workflow_status = _Value()
    monitor.guided_workflow_status_label = _Configurable()
    monitor.room_automation_choice = _Value()
    monitor.room_round_limit = _Value()
    monitor.room_message_limit = _Value()
    monitor.room_stagnation_limit = _Value()
    monitor._t = lambda key: f"<{key}>"
    calls: list[tuple[str, dict[str, object]]] = []
    captured_plan_input: dict[str, object] = {}

    class HumanClient:
        @staticmethod
        def enqueue_workflow(**kwargs: object) -> dict[str, object]:
            calls.append(("enqueue_workflow", kwargs))
            return {"operation_id": kwargs["operation_id"], "status": "queued"}

        @staticmethod
        def set_room_automation(**kwargs: object) -> dict[str, object]:
            calls.append(("set_room_automation", kwargs))
            return {"automation_mode": kwargs["mode"]}

        @staticmethod
        def post_room_message(**kwargs: object) -> dict[str, object]:
            calls.append(("post_room_message", kwargs))
            return {
                "message_id": "message-one",
                "automation_mode": "discussion",
                "room_id": "room-one",
                "task_id": "guided-task-one",
                "discussion_id": "discussion-one",
                "discussion_sha256": "a" * 64,
                "fanout_count": 2,
                "recipients": [
                    {
                        "agent_id": participant["agent_id"],
                        "route_profile_id": participant["route_profile_id"],
                        "route_profile_sha256": participant[
                            "route_profile_sha256"
                        ],
                    }
                    for participant in participants
                ],
            }

        @staticmethod
        def room_members(**kwargs: object) -> dict[str, object]:
            calls.append(("room_members", kwargs))
            return {"members": raw_members}

        @staticmethod
        def bind_guided_discussion(**kwargs: object) -> dict[str, object]:
            calls.append(("bind_guided_discussion", kwargs))
            return {
                "operation_id": kwargs["operation_id"],
                "bound_discussion_id": kwargs["discussion_id"],
                "status": "queued",
            }

        @staticmethod
        def cancel_operation(**kwargs: object) -> dict[str, object]:
            calls.append(("cancel_operation", kwargs))
            return {"status": "cancelled"}

    plan = {
        "room_id": "room-one",
        "task_id": "guided-task-one",
        "subject": "Investigate + Debate",
        "body": "Bounded read-only comparison",
        "priority": "normal",
        "automation_mode": "discussion",
        "max_rounds": 2,
        "max_messages": 8,
        "stagnation_rounds": 1,
        "participant_count": 2,
        "participants": participants,
        "source_binding_sha256": hashlib.sha256(
            json.dumps(
                {
                    "participants": participants,
                    "room_id": "room-one",
                    "workflow_id": "investigate-debate",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "workflow_id": "investigate-debate",
        "operation_id": "guided-room:operation-one",
        "operation_task_text": "durable guided operation",
        "operation_working_directory": ".",
        "operation_resource_key": "room-discussion:" + "b" * 64,
        "operation_max_attempts": 2,
        "operation_timeout_seconds": 600,
    }

    def make_plan(**kwargs: object) -> dict[str, object]:
        captured_plan_input.update(kwargs)
        return dict(plan)

    def run_action(operation: object, **kwargs: object) -> None:
        receipt = operation()
        kwargs["after_success"](receipt)
        assert kwargs["success"](receipt) == "<chat.guided.started>"

    monitor.human_client = HumanClient()
    monitor._run_room_action = run_action
    monkeypatch.setattr(
        "peerbridge_mcp.monitor.guided_room_workflow_plan", make_plan
    )

    monitor.start_guided_room_workflow()

    assert captured_plan_input["room_id"] == "room-one"
    assert captured_plan_input["task_text"] == "Compare the current user flow."
    assert [name for name, _payload in calls] == [
        "enqueue_workflow",
        "set_room_automation",
        "post_room_message",
        "room_members",
        "bind_guided_discussion",
    ]
    assert calls[0][1]["operation_id"] == "guided-room:operation-one"
    assert calls[1][1] == {
        "room_id": "room-one",
        "mode": "discussion",
        "max_rounds": 2,
        "max_messages": 8,
        "stagnation_rounds": 1,
    }
    assert calls[2][1]["task_id"] == "guided-task-one"
    assert calls[4] == (
        "bind_guided_discussion",
        {
            "operation_id": "guided-room:operation-one",
            "discussion_id": "discussion-one",
        },
    )
    assert monitor.room_automation_choice.get() == "<chat.mode.discussion>"
    assert monitor.room_round_limit.get() == "2"
    assert monitor.room_message_limit.get() == "8"
    assert monitor.room_stagnation_limit.get() == "1"
