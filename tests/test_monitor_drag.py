from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from peerbridge_mcp.monitor import (
    CHAT_HISTORY_MIN_HEIGHT,
    CHAT_PAGE_MIN_CONTENT_HEIGHT,
    INFERENCE_ONLY,
    MCP_NATIVE,
    MCP_TOOL_LOOP,
    MCP_UNVERIFIED,
    McpHumanClient,
    PixelMonitor,
    agent_mcp_access_mode,
    chat_bubble_metrics,
    chat_split_sash_position,
    compact_sidebar_stats,
    incremental_render_mode,
    provider_display_label,
    room_agent_card_groups,
    safe_chat_artifact_labels,
    ui_content_signature,
)


def test_compact_window_keeps_a_useful_history_and_scrollable_page() -> None:
    assert CHAT_HISTORY_MIN_HEIGHT >= 240
    assert CHAT_PAGE_MIN_CONTENT_HEIGHT >= 840


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


class _StableInner:
    def winfo_children(self) -> list[object]:
        raise AssertionError("stable refresh must not inspect or destroy chat widgets")


class _Viewport:
    def __init__(self) -> None:
        self.moves: list[float] = []

    def yview(self) -> tuple[float, float]:
        return (0.5, 1.0)

    def yview_moveto(self, position: float) -> None:
        self.moves.append(position)


class _IdleRoot:
    @staticmethod
    def after_idle(callback: object) -> None:
        callback()


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


class _Body:
    def __init__(self, value: str) -> None:
        self.value = value
        self.deletes = 0

    def get(self, _start: str, _end: str) -> str:
        return self.value

    def delete(self, _start: str, _end: str) -> None:
        self.value = ""
        self.deletes += 1


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
    assert monitor.agent_canvas.configurations[-1]["scrollregion"] == (
        0,
        0,
        184,
        271,
    )


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
    calls: list[str] = []
    assert monitor._post_to_ui(lambda: calls.append("late"))

    monitor.close()
    assert monitor._post_to_ui(lambda: calls.append("too-late")) is False
    monitor.close()

    assert monitor._ui_generation == 5
    assert monitor.root.cancelled == ["refresh-token", "pump-token"]
    assert monitor.root.destroy_count == 1
    assert monitor.reader.close_count == 1
    assert calls == []
