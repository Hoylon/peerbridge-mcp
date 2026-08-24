from __future__ import annotations

import json
import queue
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pytest

from peerbridge_mcp.desktop_cockpit import (
    COCKPIT_AGENTS,
    COCKPIT_DEFAULT_LAUNCH_ROLE,
    COCKPIT_TIMELINE_LIMIT,
    AgentCockpit,
    cockpit_activity_projection,
    cockpit_action_allowed,
    cockpit_current_activity,
    cockpit_elapsed_seconds,
    cockpit_event_visible_in_terminal,
    cockpit_focus_panel_id,
    cockpit_grid_columns,
    cockpit_horizontal_bounds_fit,
    cockpit_localized_status,
    cockpit_rows_fit,
    cockpit_room_context_panel_ids,
    cockpit_session_binding_changed,
    cockpit_stale_panel_ids,
    cockpit_timeline_rows,
    cockpit_visible_in_room_context,
    cockpit_working_directory,
)
from peerbridge_mcp.managed_agents import ManagedAgentError


def test_cockpit_offers_every_reviewed_managed_cli_profile() -> None:
    assert COCKPIT_AGENTS == (
        ("OpenAI Codex", "codex"),
        ("Claude Code", "claude-code"),
        ("Kimi Code", "kimi-code"),
        ("Grok CLI", "grok"),
    )


def test_cockpit_grid_keeps_compact_panels_readable() -> None:
    assert cockpit_grid_columns(859) == 1
    assert cockpit_grid_columns(860) == 2


def test_cockpit_horizontal_bounds_reject_overflow() -> None:
    assert cockpit_horizontal_bounds_fit(600, ((8, 540), (8, 360))) is True
    assert cockpit_horizontal_bounds_fit(600, ((8, 593),)) is False


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_cockpit_launch_rows_fit_minimum_window_at_supported_scales(
    scale: float,
) -> None:
    # The physical minimum is 980 px; Tk receives the logical width after DPI scaling.
    logical_width = int(980 / scale)
    assert cockpit_rows_fit(
        logical_width,
        (
            ((10, 80), (95, logical_width - 105)),
            ((10, logical_width - 20),),
            ((10, max(120, logical_width - 135)), (logical_width - 120, 110)),
        ),
    ) is True


def test_cockpit_rows_reject_overlap() -> None:
    assert cockpit_rows_fit(300, (((10, 180), (150, 120)),)) is False


def test_cockpit_prompt_installs_explicit_clipboard_and_select_all_bindings() -> None:
    class Prompt:
        def __init__(self) -> None:
            self.bindings: dict[str, object] = {}

        def bind(self, sequence: str, callback: object, *, add: str) -> None:
            assert add == "+"
            self.bindings[sequence] = callback

    calls: list[str] = []
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit._copy_text_selection = lambda _widget: calls.append("copy") or True
    cockpit._cut_text_selection = lambda _widget: calls.append("cut") or True
    cockpit._paste_text = lambda _widget: calls.append("paste") or True
    cockpit._select_all_text = lambda _widget: calls.append("all") or True
    prompt = Prompt()

    cockpit._bind_text_editing(prompt)

    assert {
        "<Control-c>",
        "<Control-x>",
        "<Control-v>",
        "<Control-a>",
        "<Control-Insert>",
        "<Shift-Delete>",
        "<Shift-Insert>",
    }.issubset(prompt.bindings)
    assert prompt.bindings["<Control-c>"](None) == "break"
    assert prompt.bindings["<Control-x>"](None) == "break"
    assert prompt.bindings["<Control-v>"](None) == "break"
    assert prompt.bindings["<Control-a>"](None) == "break"
    assert calls == ["copy", "cut", "paste", "all"]


def test_cockpit_working_directory_stays_inside_non_sensitive_project_paths(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "src"
    source.mkdir()
    protected = project / ".peerbridge"
    protected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert cockpit_working_directory(project, ".") == project.resolve()
    assert cockpit_working_directory(project, "src") == source.resolve()
    with pytest.raises(ManagedAgentError, match="inside the project"):
        cockpit_working_directory(project, outside)
    with pytest.raises(ManagedAgentError, match="protected"):
        cockpit_working_directory(project, protected)


def test_cockpit_timeline_keeps_session_identity_when_completion_order_differs() -> None:
    snapshots = [
        {
            "session_id": "slow",
            "display_name": "Codex",
            "events": [
                {
                    "sequence": 2,
                    "created_utc": "2026-08-19T01:00:02Z",
                    "kind": "answer",
                    "text": "slow answer",
                }
            ],
        },
        {
            "session_id": "fast",
            "display_name": "Claude",
            "events": [
                {
                    "sequence": 5,
                    "created_utc": "2026-08-19T01:00:01Z",
                    "kind": "activity",
                    "summary": "fast activity",
                }
            ],
        },
    ]

    rows = cockpit_timeline_rows(snapshots)

    assert [(row["session_id"], row["text"]) for row in rows] == [
        ("fast", "fast activity"),
        ("slow", "slow answer"),
    ]


def test_cockpit_timeline_rows_retain_room_identity_for_context_filtering() -> None:
    rows = cockpit_timeline_rows(
        [
            {
                "session_id": "room-a-agent",
                "source_type": "peerbridge-room",
                "room_id": "room-a",
                "events": [{"sequence": 1, "kind": "answer", "text": "A"}],
            },
            {
                "session_id": "room-b-agent",
                "source_type": "peerbridge-room",
                "room_id": "room-b",
                "events": [{"sequence": 1, "kind": "answer", "text": "B"}],
            },
            {
                "session_id": "global-managed",
                "source_type": "managed-cli",
                "events": [{"sequence": 1, "kind": "activity", "text": "global"}],
            },
        ]
    )

    assert [(row["session_id"], row["room_id"]) for row in rows] == [
        ("global-managed", ""),
        ("room-a-agent", "room-a"),
        ("room-b-agent", "room-b"),
    ]
    assert [
        row["session_id"]
        for row in rows
        if cockpit_visible_in_room_context(row, "room-b")
    ] == ["global-managed", "room-b-agent"]


def test_cockpit_timeline_is_bounded() -> None:
    snapshots = [
        {
            "session_id": "one",
            "display_name": "Codex",
            "events": [
                {
                    "sequence": index,
                    "created_utc": f"2026-08-19T01:{index // 60:02}:{index % 60:02}Z",
                    "kind": "activity",
                    "text": str(index),
                }
                for index in range(COCKPIT_TIMELINE_LIMIT + 20)
            ],
        }
    ]

    rows = cockpit_timeline_rows(snapshots, limit=COCKPIT_TIMELINE_LIMIT + 100)

    assert len(rows) == COCKPIT_TIMELINE_LIMIT
    assert rows[-1]["sequence"] == COCKPIT_TIMELINE_LIMIT + 19


def test_cockpit_elapsed_uses_terminal_time_when_available() -> None:
    snapshot = {
        "started_utc": "2026-08-19T01:00:00Z",
        "ended_utc": "2026-08-19T01:00:07Z",
    }
    assert cockpit_elapsed_seconds(snapshot) == 7
    assert cockpit_elapsed_seconds(
        {"started_utc": "2026-08-19T01:00:00Z"},
        now=datetime(2026, 8, 19, 1, 0, 9, tzinfo=timezone.utc),
    ) == 9


def test_cockpit_ready_status_tracks_locale_without_overwriting_live_state() -> None:
    assert cockpit_localized_status("", "", "Ready") == "Ready"
    assert cockpit_localized_status("Ready", "Ready", "Bereit") == "Bereit"
    assert cockpit_localized_status("Running task", "Ready", "Bereit") == "Running task"


def test_cockpit_projects_only_explicit_structured_activity() -> None:
    planning = cockpit_activity_projection(
        {
            "kind": "activity",
            "text": json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "reasoning",
                        "text": "Planning localized status text implementation",
                    },
                }
            ),
        }
    )
    command = cockpit_activity_projection(
        {
            "kind": "activity",
            "text": json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "pytest -q",
                        "status": "completed",
                    },
                }
            ),
        }
    )
    changed = cockpit_activity_projection(
        {
            "kind": "activity",
            "text": json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "file_change",
                        "changes": [
                            {"path": "src/peerbridge_mcp/session_contract.py"}
                        ],
                    },
                }
            ),
        }
    )

    assert planning == {
        "action": "planning",
        "detail": "Planning localized status text implementation",
    }
    assert command == {"action": "command_completed", "detail": "pytest -q"}
    assert changed == {"action": "file_updated", "detail": "session_contract.py"}


def test_cockpit_current_activity_is_truthful_when_no_events_are_available() -> None:
    assert cockpit_current_activity({"state": "running", "events": []}) == {
        "action": "waiting",
        "detail": "",
    }
    assert cockpit_current_activity({"state": "detected", "events": []}) == {
        "action": "unavailable",
        "detail": "",
    }


def test_cockpit_current_activity_survives_incremental_polls_until_source_is_stale() -> None:
    previous = {"action": "file_reading", "detail": "desktop_cockpit.py"}

    assert cockpit_current_activity(
        {"state": "running", "events": []}, previous=previous
    ) == previous
    assert cockpit_current_activity(
        {"state": "completed", "events": []}, previous=previous
    ) == previous
    assert cockpit_current_activity(
        {"state": "unavailable", "events": []}, previous=previous
    ) == {"action": "unavailable", "detail": ""}


def test_cockpit_actions_remain_bound_to_peerbridge_owned_managed_sessions() -> None:
    capabilities = {
        "input_capable": True,
        "terminal_controllable": True,
    }
    managed = {"source_type": "managed-cli", "capabilities": capabilities}
    external = {
        "source_type": "authorized-desktop",
        "capabilities": capabilities,
    }
    room = {
        "source_type": "peerbridge-room",
        "capabilities": capabilities,
    }
    assert cockpit_action_allowed(managed, "send") is True
    assert cockpit_action_allowed(managed, "stop") is True
    assert cockpit_action_allowed(room, "send") is True
    assert cockpit_action_allowed(room, "interrupt") is False
    assert cockpit_action_allowed(room, "stop") is False
    assert cockpit_action_allowed(external, "send") is False
    assert cockpit_action_allowed(external, "stop") is False


def test_model_route_room_events_never_project_as_terminal_capture() -> None:
    event = {
        "sequence": 2,
        "created_utc": "2026-08-20T10:00:00Z",
        "stream": "system",
        "kind": "answer",
        "text": "Kimi room answer",
        "summary": "Kimi answer",
    }
    snapshot = {
        "session_id": "peerbridge-room:kimi-room-session",
        "source_type": "peerbridge-room",
        "capabilities": {
            "model_route_only": True,
            "terminal_controllable": False,
        },
        "events": [event],
    }
    appended: list[tuple[str, str]] = []
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit._localized_enum = lambda _prefix, value, _choices, **_kwargs: str(value)
    cockpit._t = lambda key: key
    cockpit._append_text = lambda widget, value: appended.append((widget, value))
    cockpit._record_timeline_event = lambda _snapshot, _event: None
    panel = {
        "texts": {
            "terminal": "terminal-widget",
            "activity": "activity-widget",
            "answer": "answer-widget",
        },
        "latest_sequence": 0,
    }

    assert cockpit_event_visible_in_terminal(snapshot, event) is False
    cockpit._append_events(panel, snapshot)

    assert all(widget != "terminal-widget" for widget, _value in appended)
    assert [widget for widget, _value in appended] == [
        "activity-widget",
        "answer-widget",
    ]
    assert panel["latest_sequence"] == 2


def test_managed_cli_events_keep_existing_terminal_projection() -> None:
    event = {
        "sequence": 3,
        "created_utc": "2026-08-20T10:00:01Z",
        "stream": "stdout",
        "kind": "answer",
        "text": "Managed CLI answer",
    }
    snapshot = {
        "session_id": "managed-one",
        "source_type": "managed-cli",
        "capabilities": {
            "model_route_only": False,
            "terminal_controllable": True,
        },
        "events": [event],
    }
    appended: list[tuple[str, str]] = []
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit._localized_enum = lambda _prefix, value, _choices, **_kwargs: str(value)
    cockpit._t = lambda key: key
    cockpit._append_text = lambda widget, value: appended.append((widget, value))
    cockpit._record_timeline_event = lambda _snapshot, _event: None
    panel = {
        "texts": {
            "terminal": "terminal-widget",
            "activity": "activity-widget",
            "answer": "answer-widget",
        },
        "latest_sequence": 0,
    }

    assert cockpit_event_visible_in_terminal(snapshot, event) is True
    cockpit._append_events(panel, snapshot)

    assert [widget for widget, _value in appended] == [
        "terminal-widget",
        "activity-widget",
        "answer-widget",
    ]
    assert "Managed CLI answer" in appended[0][1]


def test_model_route_surface_uses_room_label_and_capability_contract() -> None:
    class Notebook:
        def __init__(self) -> None:
            self.labels: list[tuple[object, str]] = []

        def tab(self, tab: object, *, text: str) -> None:
            self.labels.append((tab, text))

    notebook = Notebook()
    replacements: list[tuple[object, str]] = []
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit._localized_source = lambda _value: "PeerBridge room"
    cockpit._t = lambda key: {
        "cockpit.tab.terminal": "Raw terminal output",
    }.get(key, key)
    cockpit._source_contract_text = lambda _snapshot: (
        "Terminal controllable: No | Model route only: Yes"
    )
    cockpit._replace_text = lambda widget, value: replacements.append((widget, value))
    panel = {
        "notebook": notebook,
        "tabs": {"terminal": "terminal-tab"},
        "texts": {"terminal": "terminal-widget"},
    }
    room_snapshot = {
        "source_type": "peerbridge-room",
        "capabilities": {
            "model_route_only": True,
            "terminal_controllable": False,
        },
    }

    cockpit._sync_terminal_surface(panel, room_snapshot)

    assert notebook.labels == [("terminal-tab", "PeerBridge room")]
    assert replacements == [
        (
            "terminal-widget",
            "Terminal controllable: No | Model route only: Yes",
        )
    ]

    cockpit._sync_terminal_surface(panel, room_snapshot)
    assert replacements == [
        (
            "terminal-widget",
            "Terminal controllable: No | Model route only: Yes",
        )
    ]

    replacements.clear()
    cockpit._sync_terminal_surface(
        panel,
        {
            "source_type": "managed-cli",
            "capabilities": {
                "model_route_only": False,
                "terminal_controllable": True,
            },
        },
    )
    assert notebook.labels[-1] == ("terminal-tab", "Raw terminal output")
    assert replacements == []


def test_cockpit_focus_uses_the_exact_source_bound_panel_identity() -> None:
    available = {
        "peerbridge-room:room-session-one",
        "authorized-desktop:desktop-one",
    }
    assert cockpit_focus_panel_id(
        "authorized-desktop", "desktop-one", available
    ) == "authorized-desktop:desktop-one"
    assert (
        cockpit_focus_panel_id(
            "authorized-desktop", "desktop-missing", available
        )
        is None
    )


def test_rotated_room_session_removes_the_old_panel_instead_of_rekeying_it() -> None:
    old_panel = "peerbridge-room:old-room-session"
    new_panel = "peerbridge-room:new-room-session"

    assert cockpit_stale_panel_ids(
        (old_panel, "managed-one"),
        (
            {"session_id": new_panel},
            {"session_id": "managed-one"},
        ),
    ) == (old_panel,)


def test_room_context_prefers_exact_native_panels_and_excludes_other_rooms() -> None:
    snapshots = (
        {
            "session_id": "managed-global",
            "source_type": "managed-cli",
        },
        {
            "session_id": "authorized-terminal:room-a-terminal",
            "source_type": "authorized-terminal",
            "room_id": "room-a",
            "display_name": "Terminal",
        },
        {
            "session_id": "peerbridge-room:room-a-kimi",
            "source_type": "peerbridge-room",
            "room_id": "room-a",
            "display_name": "Kimi",
        },
        {
            "session_id": "peerbridge-room:room-b-grok",
            "source_type": "peerbridge-room",
            "room_id": "room-b",
            "display_name": "Grok",
        },
    )

    assert cockpit_room_context_panel_ids(snapshots, "room-a") == (
        "peerbridge-room:room-a-kimi",
        "authorized-terminal:room-a-terminal",
    )


def test_room_context_hides_old_room_while_new_room_panels_are_pending() -> None:
    old_room = {
        "session_id": "peerbridge-room:room-a-kimi",
        "source_type": "peerbridge-room",
        "room_id": "room-a",
    }
    global_managed = {
        "session_id": "managed-global",
        "source_type": "managed-cli",
    }
    global_terminal = {
        "session_id": "authorized-terminal:unbound-terminal",
        "source_type": "authorized-terminal",
    }
    malformed_room = {
        "session_id": "peerbridge-room:missing-room-binding",
        "source_type": "peerbridge-room",
    }
    new_room = {
        "session_id": "peerbridge-room:room-b-grok",
        "source_type": "peerbridge-room",
        "room_id": "room-b",
    }

    pending = (old_room, global_managed, global_terminal, malformed_room)
    assert [
        row["session_id"]
        for row in pending
        if cockpit_visible_in_room_context(row, "room-b")
    ] == ["managed-global", "authorized-terminal:unbound-terminal"]

    arrived = (*pending, new_room)
    assert [
        row["session_id"]
        for row in arrived
        if cockpit_visible_in_room_context(row, "room-b")
    ] == [
        "managed-global",
        "authorized-terminal:unbound-terminal",
        "peerbridge-room:room-b-grok",
    ]


def test_panel_binding_drift_is_detected_before_old_output_can_be_relabelled() -> None:
    current = {
        "source_type": "peerbridge-room",
        "source_session_id": "stable-session",
        "room_id": "room-a",
    }

    assert cockpit_session_binding_changed(current, dict(current)) is False
    assert cockpit_session_binding_changed(
        current, {**current, "room_id": "room-b"}
    ) is True


def test_panel_binding_reset_clears_old_output_and_timeline_identity() -> None:
    cockpit = AgentCockpit.__new__(AgentCockpit)
    old_widgets = {key: object() for key in ("terminal", "activity", "answer", "evidence")}
    cleared: list[object] = []
    cockpit._replace_text = lambda widget, value: cleared.append(widget)
    cockpit._timeline = deque(
        [
            {
                "source_type": "peerbridge-room",
                "room_id": "room-a",
                "session_id": "peerbridge-room:stable-session",
                "sequence": 1,
            },
            {
                "source_type": "managed-cli",
                "room_id": "",
                "session_id": "global-managed",
                "sequence": 2,
            },
        ]
    )
    cockpit._timeline_keys = {
        ("peerbridge-room", "room-a", "peerbridge-room:stable-session", 1),
        ("managed-cli", "", "global-managed", 2),
    }
    cockpit._timeline_signature = (("old", 1),)
    panel = {
        "session_id": "peerbridge-room:stable-session",
        "source_type": "peerbridge-room",
        "source_session_id": "stable-session",
        "room_id": "room-a",
        "texts": old_widgets,
        "latest_sequence": 9,
        "first_retained_sequence": 1,
        "current_activity": {"action": "answer"},
    }

    cockpit._reset_panel_binding(
        panel,
        {
            "session_id": "peerbridge-room:stable-session",
            "source_type": "peerbridge-room",
            "source_session_id": "stable-session",
            "room_id": "room-b",
        },
    )

    assert set(cleared) == set(old_widgets.values())
    assert panel["room_id"] == "room-b"
    assert panel["latest_sequence"] == 0
    assert panel["first_retained_sequence"] == 0
    assert panel["current_activity"] is None
    assert [row["session_id"] for row in cockpit._timeline] == ["global-managed"]
    assert cockpit._timeline_keys == {("managed-cli", "", "global-managed", 2)}
    assert cockpit._timeline_signature == ()


def test_focus_source_cannot_reopen_a_panel_bound_to_another_room() -> None:
    class Value:
        def __init__(self, value: str = "") -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit.render = lambda: False
    cockpit._room_context_id = "room-b"
    cockpit._panels = {
        "peerbridge-room:room-a-session": {
            "source_type": "peerbridge-room",
            "room_id": "room-a",
        }
    }
    cockpit.status = Value()
    cockpit.status_label = type(
        "Label", (), {"configure": lambda self, **kwargs: None}
    )()
    cockpit.colors = {"amber": "amber"}
    cockpit._t = lambda key: key

    assert cockpit.focus_source("peerbridge-room", "room-a-session") is False
    assert cockpit.status.get() == "cockpit.status.source_unavailable"


def test_focus_layout_falls_back_without_regridding_unchanged_panels() -> None:
    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

    class Widget:
        def __init__(self, width: int = 1000) -> None:
            self.width = width
            self.grid_calls = 0

        def configure(self, **_kwargs: object) -> None:
            return None

        def grid_forget(self) -> None:
            return None

        def grid_remove(self) -> None:
            return None

        def grid(self, **_kwargs: object) -> None:
            self.grid_calls += 1

        def winfo_width(self) -> int:
            return self.width

        def grid_columnconfigure(self, *_args: object, **_kwargs: object) -> None:
            return None

    def panel(source_type: str, room_id: str) -> dict[str, object]:
        return {
            "source_type": source_type,
            "room_id": room_id,
            "frame": Widget(),
            "meta_label": Widget(),
            "activity_label": Widget(),
        }

    old = panel("peerbridge-room", "room-a")
    current = panel("peerbridge-room", "room-b")
    global_managed = panel("managed-cli", "")
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit.view_mode = Value("focus")
    cockpit.selected_session = Value("old-room")
    cockpit._room_context_id = "room-b"
    cockpit._panels = {
        "old-room": old,
        "current-room": current,
        "global-managed": global_managed,
    }
    cockpit.timeline_frame = Widget()
    cockpit.panel_canvas = Widget()
    cockpit.panel_scroll = Widget()
    cockpit.panel_host = Widget()

    cockpit._layout_panels()
    cockpit._layout_panels()

    assert old["frame"].grid_calls == 0
    assert current["frame"].grid_calls == 1
    assert global_managed["frame"].grid_calls == 0


def test_replace_text_does_not_redraw_unchanged_content() -> None:
    class Text:
        def __init__(self) -> None:
            self.value = "stable output"
            self.mutations = 0

        def get(self, _start: str, _end: str) -> str:
            return self.value

        def configure(self, **_kwargs: object) -> None:
            self.mutations += 1

        def delete(self, _start: str, _end: str) -> None:
            self.mutations += 1

        def insert(self, _index: str, _value: str) -> None:
            self.mutations += 1

    widget = Text()

    AgentCockpit._replace_text(widget, "stable output")

    assert widget.mutations == 0


def test_cockpit_session_options_use_room_and_agent_names_without_raw_ids() -> None:
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit._t = lambda key: "Unavailable" if key == "cockpit.unavailable" else key
    cockpit._localized_source = lambda value: {
        "peerbridge-room": "PeerBridge room"
    }.get(value, str(value))

    labels = cockpit._sync_session_options(
        (
            {
                "session_id": "peerbridge-room:opaque-room-session-a",
                "source_type": "peerbridge-room",
                "room_name": "Release Review",
                "display_name": "grok-relay",
            },
            {
                "session_id": "peerbridge-room:opaque-room-session-b",
                "source_type": "peerbridge-room",
                "room_name": "Release Review",
                "display_name": "grok-relay",
            },
        )
    )

    assert labels == (
        "Release Review / grok-relay / PeerBridge room",
        "Release Review / grok-relay / PeerBridge room (2)",
    )
    assert all("opaque-room-session" not in label for label in labels)
    assert cockpit._session_label_to_id[labels[1]].endswith("session-b")


def test_selecting_a_cockpit_work_item_immediately_focuses_that_panel() -> None:
    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    calls: list[str] = []
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit.selected_session = Value("panel-b")
    cockpit.selected_session_label = Value("")
    cockpit.view_mode = Value("grid")
    cockpit._room_context_pending = False
    cockpit._session_id_to_label = {"panel-b": "Room B / Kimi"}
    cockpit._sync_view_buttons = lambda: calls.append("buttons")
    cockpit._sync_session_actions = lambda: calls.append("actions")
    cockpit._layout_panels = lambda: calls.append("layout")

    cockpit._selection_changed()

    assert cockpit.selected_session_label.get() == "Room B / Kimi"
    assert cockpit.view_mode.get() == "focus"
    assert calls == ["buttons", "actions", "layout"]


def test_changing_room_context_clears_old_focus_until_new_room_arrives() -> None:
    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    calls: list[str] = []
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit._room_context_id = "room-a"
    cockpit._room_context_pending = False
    cockpit.selected_session = Value("peerbridge-room:room-a-session")
    cockpit.selected_session_label = Value("Room A / Kimi")
    cockpit._session_id_to_label = {}
    cockpit.view_mode = Value("focus")
    cockpit._sync_view_buttons = lambda: calls.append("buttons")
    cockpit._sync_session_actions = lambda: calls.append("actions")
    cockpit._layout_panels = lambda: calls.append("layout")

    assert cockpit.set_room_context("room-b") is True
    assert cockpit.selected_session.get() == ""
    assert cockpit.view_mode.get() == "grid"
    assert cockpit._room_context_pending is True
    assert calls == ["buttons", "actions", "layout"]
    assert cockpit.set_room_context("room-b") is False


def test_room_send_completion_does_not_steal_focus_after_switching_panels() -> None:
    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    selected: list[str] = []
    completed: list[dict[str, object]] = []
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit._action_results = queue.SimpleQueue()
    cockpit._action_results.put(
        {
            "ok": True,
            "action": "send",
            "session_id": "peerbridge-room:room-a-session",
            "source_type": "peerbridge-room",
            "receipt": {"room_id": "room-a", "message_id": "message-a"},
        }
    )
    cockpit._set_busy = lambda *_args, **_kwargs: True
    cockpit._select_session = selected.append
    cockpit._pending_prompt_sha256 = None
    cockpit._prompt_text = lambda: "draft for room B"
    cockpit.status = Value("")
    cockpit.status_label = type(
        "Label", (), {"configure": lambda self, **kwargs: None}
    )()
    cockpit.colors = {"green": "green"}
    cockpit._t = lambda key: key
    cockpit._external_input_complete = lambda receipt: completed.append(dict(receipt))

    cockpit._drain_action_results()

    assert selected == []
    assert completed == [{"room_id": "room-a", "message_id": "message-a"}]


def test_external_adapter_cannot_send_even_with_forged_input_capability() -> None:
    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit.selected_session = Value("authorized-terminal:external-one")
    cockpit._latest_snapshots = {
        "authorized-terminal:external-one": {
            "source_type": "authorized-terminal",
            "source_session_id": "external-one",
            "capabilities": {
                "input_capable": True,
                "terminal_controllable": True,
            },
        }
    }
    cockpit.status = Value("")
    cockpit.status_label = type(
        "Label", (), {"configure": lambda self, **kwargs: None}
    )()
    cockpit.colors = {"amber": "amber"}
    cockpit._t = lambda key: key
    cockpit.manager = type(
        "Manager", (), {"get": lambda self, _value: pytest.fail("manager used")}
    )()

    cockpit._session_action("send")

    assert cockpit.status.get() == "cockpit.status.source_read_only"


def test_room_send_uses_room_adapter_instead_of_managed_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class ImmediateThread:
        def __init__(self, *, target: object, **_kwargs: object) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    snapshot = {
        "source_type": "peerbridge-room",
        "source_session_id": "room-session",
        "capabilities": {"input_capable": True, "terminal_controllable": False},
    }
    sent: dict[str, object] = {}
    cockpit = AgentCockpit.__new__(AgentCockpit)
    attachment = tmp_path / "review-notes.txt"
    attachment.write_text("independent review notes", encoding="utf-8")
    cockpit.project_root = tmp_path
    cockpit._global_attachment_paths = (attachment,)
    cockpit.selected_session = Value("peerbridge-room:room-session")
    cockpit._latest_snapshots = {
        "peerbridge-room:room-session": snapshot,
    }
    cockpit._prompt_text = lambda: "Continue this room conversation."
    cockpit._prompt_sha256 = lambda value: value
    cockpit._set_busy = lambda *_args, **_kwargs: True
    cockpit.status = Value("")
    cockpit.status_label = type(
        "Label", (), {"configure": lambda self, **kwargs: None}
    )()
    cockpit._t = lambda key: key
    cockpit._action_results = queue.SimpleQueue()
    cockpit._room_sender = lambda target, text, artifacts: sent.update(
        {"snapshot": target, "text": text, "artifacts": tuple(artifacts)}
    ) or {"room_id": "room-a"}
    cockpit.manager = type(
        "Manager", (), {"get": lambda self, _value: pytest.fail("manager used")}
    )()

    monkeypatch.setattr(
        "peerbridge_mcp.desktop_cockpit.threading.Thread", ImmediateThread
    )
    cockpit._session_action("send")

    result = cockpit._action_results.get_nowait()
    assert sent["snapshot"] == snapshot
    assert sent["text"] == "Continue this room conversation."
    artifact_paths = sent["artifacts"]
    assert isinstance(artifact_paths, tuple) and len(artifact_paths) == 1
    assert str(artifact_paths[0]).startswith(".peerbridge-artifacts/chat/")
    assert (tmp_path / str(artifact_paths[0])).read_text(encoding="utf-8") == (
        "independent review notes"
    )
    assert result["ok"] is True
    assert result["source_type"] == "peerbridge-room"


def test_panel_send_routes_exact_card_even_when_another_session_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ImmediateThread:
        def __init__(self, *, target: object, **_kwargs: object) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    class Prompt:
        @staticmethod
        def get(_start: str, _end: str) -> str:
            return "Message only the Grok card."

    grok_snapshot = {
        "source_type": "peerbridge-room",
        "source_session_id": "grok-session",
        "capabilities": {"input_capable": True, "terminal_controllable": False},
    }
    kimi_snapshot = {
        "source_type": "peerbridge-room",
        "source_session_id": "kimi-session",
        "capabilities": {"input_capable": True, "terminal_controllable": False},
    }
    sent: dict[str, object] = {}
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit.selected_session = Value("peerbridge-room:kimi-session")
    cockpit._latest_snapshots = {
        "peerbridge-room:grok-session": grok_snapshot,
        "peerbridge-room:kimi-session": kimi_snapshot,
    }
    cockpit._prompt_sha256 = lambda value: value
    cockpit._set_busy = lambda *_args, **_kwargs: True
    cockpit.status = Value("")
    cockpit.status_label = type(
        "Label", (), {"configure": lambda self, **kwargs: None}
    )()
    cockpit._t = lambda key: key
    cockpit._action_results = queue.SimpleQueue()
    cockpit._room_sender = lambda target, text, artifacts: sent.update(
        {"snapshot": target, "text": text, "artifacts": tuple(artifacts)}
    ) or {"room_id": "lobby"}
    cockpit.manager = type(
        "Manager", (), {"get": lambda self, _value: pytest.fail("manager used")}
    )()

    monkeypatch.setattr(
        "peerbridge_mcp.desktop_cockpit.threading.Thread", ImmediateThread
    )
    cockpit._session_action(
        "send",
        panel_id="peerbridge-room:grok-session",
        prompt_widget=Prompt(),
    )

    assert sent == {
        "snapshot": grok_snapshot,
        "text": "Message only the Grok card.",
        "artifacts": (),
    }


def test_successful_panel_send_clears_only_the_matching_card_draft() -> None:
    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    class Prompt:
        def __init__(self, value: str) -> None:
            self.value = value
            self.deleted = False

        def get(self, _start: str, _end: str) -> str:
            return self.value

        def delete(self, _start: str, _end: str) -> None:
            self.value = ""
            self.deleted = True

    grok_prompt = Prompt("Grok draft")
    kimi_prompt = Prompt("Kimi draft")
    global_prompt = Prompt("Global draft")
    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit._action_results = queue.SimpleQueue()
    cockpit._action_results.put(
        {
            "ok": True,
            "action": "send",
            "session_id": "peerbridge-room:grok-session",
            "source_type": "peerbridge-room",
            "receipt": {"room_id": "lobby"},
        }
    )
    cockpit._set_busy = lambda *_args, **_kwargs: True
    cockpit._pending_prompt_sha256 = "Grok draft"
    cockpit._pending_prompt_target = "peerbridge-room:grok-session"
    cockpit._prompt_sha256 = lambda value: value
    cockpit._panels = {
        "peerbridge-room:grok-session": {"prompt": grok_prompt},
        "peerbridge-room:kimi-session": {"prompt": kimi_prompt},
    }
    cockpit.prompt = global_prompt
    cockpit.status = Value("")
    cockpit.status_label = type(
        "Label", (), {"configure": lambda self, **kwargs: None}
    )()
    cockpit.colors = {"green": "green"}
    cockpit._t = lambda key: key
    cockpit._external_input_complete = None

    cockpit._drain_action_results()

    assert grok_prompt.deleted is True
    assert kimi_prompt.deleted is False
    assert global_prompt.deleted is False


def test_failed_panel_send_preserves_draft_and_selected_attachments() -> None:
    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    cockpit = AgentCockpit.__new__(AgentCockpit)
    attachment = Path("review-notes.txt")
    cockpit._action_results = queue.SimpleQueue()
    cockpit._action_results.put(
        {"ok": False, "action": "send", "error": "provider unavailable"}
    )
    cockpit._set_busy = lambda *_args, **_kwargs: True
    cockpit._pending_prompt_sha256 = "draft"
    cockpit._pending_prompt_target = "peerbridge-room:grok-session"
    cockpit._pending_attachment_paths = (attachment,)
    cockpit._panels = {
        "peerbridge-room:grok-session": {
            "attachment_paths": (attachment,),
        }
    }
    cockpit.status = Value("")
    cockpit.status_label = type(
        "Label", (), {"configure": lambda self, **kwargs: None}
    )()
    cockpit.colors = {"red": "red"}
    cockpit._t = lambda key: key

    cockpit._drain_action_results()

    assert cockpit._panels["peerbridge-room:grok-session"][
        "attachment_paths"
    ] == (attachment,)
    assert cockpit._pending_attachment_paths == ()


def test_new_cockpit_launch_uses_equal_participant_without_a_second_role_editor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class ImmediateThread:
        def __init__(self, *, target: object, **_kwargs: object) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    class Manager:
        @staticmethod
        def start(launch: object, *, input_text: str | None = None) -> None:
            captured["launch"] = launch
            captured["input_text"] = input_text

    def build_launch(agent_id: str, **kwargs: object) -> object:
        captured["agent_id"] = agent_id
        captured.update(kwargs)
        return object()

    cockpit = AgentCockpit.__new__(AgentCockpit)
    cockpit.manager = Manager()
    cockpit._action_results = queue.SimpleQueue()
    cockpit._prompt_text = lambda: "Inspect the current user flow."
    cockpit._selected_agent = lambda: "codex"
    cockpit._working_path = lambda: tmp_path.resolve()
    cockpit._set_busy = lambda *_args, **_kwargs: True
    cockpit.status = type("Status", (), {"set": lambda self, value: None})()
    cockpit._t = lambda key: key

    monkeypatch.setattr(
        "peerbridge_mcp.desktop_cockpit.build_observe_launch", build_launch
    )
    monkeypatch.setattr(
        "peerbridge_mcp.desktop_cockpit.threading.Thread", ImmediateThread
    )

    cockpit._start(True)

    assert COCKPIT_DEFAULT_LAUNCH_ROLE == "equal-participant"
    assert captured["role"] == "equal-participant"
    assert captured["working_directory"] == tmp_path.resolve()
    assert captured["input_text"] == "Inspect the current user flow."
    assert not hasattr(cockpit, "role_choice")
