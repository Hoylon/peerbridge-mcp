"""Tk desktop view for bounded, observable local Agent sessions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import queue
import threading
import time
import tkinter as tk
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any, Callable, Iterable, Mapping

from .attachments import stage_chat_attachments
from .bridge import _path_parts_are_sensitive
from .managed_agents import (
    ManagedAgentError,
    ManagedAgentManager,
    build_observe_launch,
)
from .official_agent_runtime import HybridManagedAgentManager
from .secret_scan import redact_secrets
from .session_contract import (
    SESSION_CAPABILITIES,
    SESSION_SOURCE_TYPES,
    managed_cli_session_contract,
    normalize_session_contract,
    session_panel_id,
)


COCKPIT_REFRESH_ACTIVE_MS = 250
COCKPIT_REFRESH_IDLE_MS = 800
COCKPIT_TIMELINE_LIMIT = 500
COCKPIT_PANEL_HEIGHT = 450
COCKPIT_PANEL_TEXT_ROWS = 7
COCKPIT_VIEWS = ("grid", "focus", "timeline")
COCKPIT_ROLES = (
    "equal-participant",
    "researcher",
    "implementer",
    "reviewer",
    "investigator",
    "planner",
    "auditor",
    "custom",
)
COCKPIT_STATES = (
    "detected",
    "created",
    "running",
    "waiting",
    "stopping",
    "completed",
    "stopped",
    "failed",
    "conflict",
    "unavailable",
    "unknown",
)
COCKPIT_EVENT_KINDS = ("system", "terminal", "activity", "answer", "error")
COCKPIT_STREAMS = ("system", "stdout", "stderr")
COCKPIT_USAGE_STATES = ("reported", "partial", "unavailable")
COCKPIT_ACTIVITY_ACTIONS = (
    "planning",
    "file_reading",
    "file_updated",
    "command_running",
    "command_completed",
    "tool_running",
    "progress",
    "answer",
    "error",
    "system",
    "terminal",
    "waiting",
    "unavailable",
)
COCKPIT_AGENTS = (
    ("OpenAI Codex", "codex"),
    ("Claude Code", "claude-code"),
    ("Kimi Code", "kimi-code"),
    ("Grok CLI", "grok"),
)
COCKPIT_DEFAULT_LAUNCH_ROLE = "equal-participant"
COCKPIT_PROTECTED_ROOTS = (".git", ".peerbridge", ".peerbridge-artifacts")
COCKPIT_ATTACHMENT_FILETYPES = (
    (
        "Safe images and text",
        "*.png *.jpg *.jpeg *.gif *.webp *.txt *.md *.csv *.json *.log",
    ),
    ("All files", "*.*"),
)


def cockpit_grid_columns(width: int) -> int:
    """Keep each terminal panel readable at compact desktop widths."""

    return 2 if int(width) >= 860 else 1


def cockpit_working_directory(project_root: Path, selected: Path | str) -> Path:
    root = project_root.resolve()
    candidate = Path(selected)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ManagedAgentError(
            "managed Agent working directory must remain inside the project"
        ) from exc
    normalized = relative.as_posix() or "."
    collision_key = normalized.casefold()
    if _path_parts_are_sensitive(relative) or any(
        collision_key == protected
        or collision_key.startswith(protected + "/")
        for protected in COCKPIT_PROTECTED_ROOTS
    ):
        raise ManagedAgentError("managed Agent working directory is protected")
    if not resolved.is_dir():
        raise ManagedAgentError("managed Agent working directory is unavailable")
    return resolved


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def cockpit_elapsed_seconds(
    snapshot: Mapping[str, Any], *, now: datetime | None = None
) -> int:
    started = _parse_utc(snapshot.get("started_utc"))
    if started is None:
        return 0
    ended = _parse_utc(snapshot.get("ended_utc"))
    current = ended or now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0, int((current - started).total_seconds()))


def cockpit_localized_status(
    current_status: str,
    previous_ready_status: str,
    localized_ready_status: str,
) -> str:
    if not current_status or current_status == previous_ready_status:
        return localized_ready_status
    return current_status


def cockpit_usage_summary(value: Any, unavailable: str) -> str:
    if not isinstance(value, Mapping) or value.get("status") == "unavailable":
        return unavailable

    def token(field: str) -> str:
        count = value.get(field)
        return str(count) if isinstance(count, int) and not isinstance(count, bool) else "--"

    return (
        f"{str(value.get('status') or 'unavailable').upper()} "
        f"T {token('total_tokens')} / I {token('input_tokens')} / "
        f"O {token('output_tokens')} / C {token('cached_input_tokens')} / "
        f"R {token('reasoning_tokens')}"
    )


def cockpit_terminal_summary(value: Any, unavailable: str) -> str:
    if not isinstance(value, Mapping):
        return unavailable
    status = str(value.get("status") or "").strip()
    return status.upper() if status and status != "unavailable" else unavailable


def _activity_detail(value: Any, *, limit: int = 240) -> str:
    text = " ".join(redact_secrets(str(value or "")).split())
    return text if len(text) <= limit else text[: max(1, limit - 3)] + "..."


def _structured_event(text: Any) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(str(text or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _changed_file_names(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    names: list[str] = []
    for change in value[:6]:
        if not isinstance(change, Mapping):
            continue
        raw_path = str(change.get("path") or change.get("file_path") or "").strip()
        if not raw_path:
            continue
        names.append(Path(raw_path).name or raw_path)
    return _activity_detail(", ".join(names))


def _tool_activity(content: Any) -> tuple[str, str] | None:
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "").strip()
        arguments = block.get("input")
        values = arguments if isinstance(arguments, Mapping) else {}
        lowered = name.casefold()
        if lowered in {"read", "read_file", "readfile"}:
            detail = values.get("file_path") or values.get("path") or name
            return "file_reading", _activity_detail(Path(str(detail)).name)
        if lowered in {"bash", "shell", "run_command", "execute"}:
            detail = values.get("command") or name
            return "command_running", _activity_detail(detail)
        if lowered in {"write", "edit", "apply_patch", "write_file"}:
            detail = values.get("file_path") or values.get("path") or name
            return "file_updated", _activity_detail(Path(str(detail)).name)
        return "tool_running", _activity_detail(name)
    return None


def cockpit_activity_projection(event: Mapping[str, Any]) -> dict[str, str]:
    """Project only explicit, captured progress without inferring hidden reasoning."""

    kind = str(event.get("kind") or "terminal").strip().lower()
    summary = _activity_detail(event.get("summary"))
    text = str(event.get("text") or "")
    payload = _structured_event(text)
    if payload is not None:
        event_type = str(payload.get("type") or "").strip()
        item = payload.get("item")
        if isinstance(item, Mapping):
            item_type = str(item.get("type") or "").strip()
            item_status = str(item.get("status") or "").strip().lower()
            completed = event_type.endswith("completed") or item_status == "completed"
            if item_type == "reasoning":
                return {
                    "action": "planning",
                    "detail": _activity_detail(item.get("text") or summary),
                }
            if item_type == "command_execution":
                return {
                    "action": "command_completed" if completed else "command_running",
                    "detail": _activity_detail(item.get("command")),
                }
            if item_type == "file_change":
                return {
                    "action": "file_updated",
                    "detail": _changed_file_names(item.get("changes")),
                }
            if item_type == "mcp_tool_call":
                tool = item.get("tool") or item.get("name") or item.get("server")
                return {"action": "tool_running", "detail": _activity_detail(tool)}
            if item_type == "agent_message":
                return {
                    "action": "answer",
                    "detail": _activity_detail(item.get("text") or summary),
                }
        if event_type == "assistant":
            message = payload.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            tool_activity = _tool_activity(content)
            if tool_activity is not None:
                action, detail = tool_activity
                return {"action": action, "detail": detail}
            if summary:
                return {"action": "progress", "detail": summary}
        if event_type == "result":
            return {
                "action": "answer",
                "detail": _activity_detail(payload.get("result") or summary),
            }

    action = {
        "answer": "answer",
        "error": "error",
        "system": "system",
        "activity": "progress",
    }.get(kind, "terminal")
    detail = summary
    if not detail and kind in {"answer", "error", "system"} and payload is None:
        detail = _activity_detail(text)
    return {"action": action, "detail": detail}


def cockpit_current_activity(
    snapshot: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    events = [event for event in snapshot.get("events") or () if isinstance(event, Mapping)]
    if events:
        return cockpit_activity_projection(events[-1])
    state = str(snapshot.get("state") or "").strip().lower()
    previous_action = str((previous or {}).get("action") or "")
    if (
        state in {"created", "running", "waiting", "stopping", "completed", "stopped"}
        and previous_action
        and previous_action not in {"waiting", "unavailable"}
    ):
        return {
            "action": previous_action,
            "detail": _activity_detail((previous or {}).get("detail")),
        }
    return {
        "action": (
            "waiting"
            if state in {"created", "running", "waiting", "stopping"}
            else "unavailable"
        ),
        "detail": "",
    }


def cockpit_timeline_rows(
    snapshots: list[Mapping[str, Any]], *, limit: int = COCKPIT_TIMELINE_LIMIT
) -> list[dict[str, Any]]:
    """Return a bounded, identity-bound chronological projection."""

    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        session_id = str(snapshot.get("session_id") or "")
        display_name = str(snapshot.get("display_name") or session_id)
        source_type = str(snapshot.get("source_type") or "")
        room_id = str(snapshot.get("room_id") or "").strip()
        for event in snapshot.get("events") or ():
            rows.append(
                {
                    "session_id": session_id,
                    "display_name": display_name,
                    "source_type": source_type,
                    "room_id": room_id,
                    "sequence": int(event.get("sequence") or 0),
                    "created_utc": str(event.get("created_utc") or ""),
                    "kind": str(event.get("kind") or "terminal"),
                    "text": str(event.get("summary") or event.get("text") or ""),
                }
            )
    rows.sort(
        key=lambda row: (
            row["created_utc"],
            row["session_id"],
            row["sequence"],
        )
    )
    return rows[-max(1, min(int(limit), COCKPIT_TIMELINE_LIMIT)) :]


def cockpit_visible_in_room_context(
    snapshot: Mapping[str, Any], room_id: str
) -> bool:
    """Keep the current room and genuinely unbound observable sessions visible."""

    expected_room = str(room_id or "").strip()
    if not expected_room:
        return True
    bound_room = str(snapshot.get("room_id") or "").strip()
    if bound_room:
        return bound_room == expected_room
    return str(snapshot.get("source_type") or "") in {
        "managed-cli",
        "authorized-desktop",
        "authorized-terminal",
    }


def cockpit_session_binding_changed(
    current: Mapping[str, Any], incoming: Mapping[str, Any]
) -> bool:
    """Detect identity drift before a panel can relabel previously captured output."""

    return any(
        str(current.get(key) or "").strip()
        != str(incoming.get(key) or "").strip()
        for key in ("source_type", "source_session_id", "room_id")
    )


def cockpit_action_allowed(snapshot: Mapping[str, Any] | None, action: str) -> bool:
    """Bind input to owned CLI or room mailboxes; keep process control CLI-only."""

    if not isinstance(snapshot, Mapping):
        return False
    capabilities = snapshot.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return False
    source_type = str(snapshot.get("source_type") or "")
    if action == "send":
        return source_type in {"managed-cli", "peerbridge-room"} and bool(
            capabilities.get("input_capable")
        )
    return source_type == "managed-cli" and bool(
        capabilities.get("terminal_controllable")
    )


def cockpit_event_visible_in_terminal(
    snapshot: Mapping[str, Any], event: Mapping[str, Any]
) -> bool:
    """Keep room-route messages out of terminal capture without changing managed CLI."""

    if str(snapshot.get("source_type") or "") == "managed-cli":
        return True
    capabilities = snapshot.get("capabilities")
    return not (
        str(snapshot.get("source_type") or "") == "peerbridge-room"
        and isinstance(capabilities, Mapping)
        and bool(capabilities.get("model_route_only"))
    )


def cockpit_focus_panel_id(
    source_type: str,
    source_session_id: str,
    available_panel_ids: Iterable[str],
) -> str | None:
    panel_id = session_panel_id(source_type, source_session_id)
    return panel_id if panel_id in set(available_panel_ids) else None


def cockpit_stale_panel_ids(
    panel_ids: Iterable[str], snapshots: Iterable[Mapping[str, Any]]
) -> tuple[str, ...]:
    """Return panels whose exact source session is no longer live."""

    live_ids = {str(snapshot.get("session_id") or "") for snapshot in snapshots}
    return tuple(str(panel_id) for panel_id in panel_ids if str(panel_id) not in live_ids)


def cockpit_room_context_panel_ids(
    snapshots: Iterable[Mapping[str, Any]], room_id: str
) -> tuple[str, ...]:
    """Return exact current-room panels with native room sessions first."""

    expected_room = str(room_id or "").strip()
    rows = [
        snapshot
        for snapshot in snapshots
        if expected_room
        and str(snapshot.get("room_id") or "").strip() == expected_room
        and str(snapshot.get("session_id") or "")
    ]
    rows.sort(
        key=lambda snapshot: (
            0 if snapshot.get("source_type") == "peerbridge-room" else 1,
            str(snapshot.get("display_name") or snapshot.get("agent_id") or ""),
            str(snapshot.get("session_id") or ""),
        )
    )
    return tuple(str(snapshot["session_id"]) for snapshot in rows)


def cockpit_horizontal_bounds_fit(
    container_width: int, bounds: Iterable[tuple[int, int]]
) -> bool:
    width = int(container_width)
    measured = tuple((int(x), int(item_width)) for x, item_width in bounds)
    return bool(width > 1 and measured) and all(
        x >= 0 and item_width > 1 and x + item_width <= width
        for x, item_width in measured
    )


def cockpit_rows_fit(
    container_width: int, rows: Iterable[Iterable[tuple[int, int]]]
) -> bool:
    """Validate each responsive row without allowing child overlap or overflow."""

    width = int(container_width)
    measured_rows = [
        sorted((int(x), int(item_width)) for x, item_width in row)
        for row in rows
    ]
    if width <= 1 or not measured_rows or any(not row for row in measured_rows):
        return False
    for row in measured_rows:
        if not cockpit_horizontal_bounds_fit(width, row):
            return False
        for (left_x, left_width), (right_x, _right_width) in zip(row, row[1:]):
            if left_x + left_width > right_x:
                return False
    return True


class AgentCockpit:
    """One source-aware view with a separate PeerBridge-managed launch area."""

    def __init__(
        self,
        host: tk.Misc,
        *,
        project_root: Path,
        translate: Callable[[str], str],
        colors: Mapping[str, str],
        manager: ManagedAgentManager | HybridManagedAgentManager | None = None,
        external_sessions: Callable[
            [Mapping[str, int]], Iterable[Mapping[str, Any]]
        ]
        | None = None,
        room_sender: Callable[
            [Mapping[str, Any], str, Iterable[str]], Mapping[str, Any]
        ]
        | None = None,
        external_input_complete: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.root = host.winfo_toplevel()
        self.project_root = project_root.resolve()
        self._translate = translate
        self.colors = dict(colors)
        self.manager = manager or HybridManagedAgentManager()
        self._external_sessions = external_sessions or (lambda _positions: ())
        self._room_sender = room_sender
        self._external_input_complete = external_input_complete
        self._closed = False
        self._after_id: str | None = None
        self._action_inflight: str | None = None
        self._action_results: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._pending_prompt_sha256: str | None = None
        self._pending_prompt_target: str | None = None
        self._pending_attachment_paths: tuple[Path, ...] = ()
        self._global_attachment_paths: tuple[Path, ...] = ()
        self._panels: dict[str, dict[str, Any]] = {}
        self._timeline: deque[dict[str, Any]] = deque()
        self._timeline_keys: set[tuple[str, str, str, int]] = set()
        self._timeline_signature: tuple[tuple[str, int], ...] = ()
        self._panel_layout_signature: tuple[Any, ...] = ()
        self._latest_snapshots: dict[str, dict[str, Any]] = {}
        self._room_context_id = ""
        self._room_context_pending = False

        self.agent_choice = tk.StringVar(value=COCKPIT_AGENTS[0][0])
        self.working_directory = tk.StringVar(value=str(self.project_root))
        self.selected_session = tk.StringVar(value="")
        self.selected_session_label = tk.StringVar(value="")
        self._session_label_to_id: dict[str, str] = {}
        self._session_id_to_label: dict[str, str] = {}
        self.view_mode = tk.StringVar(value="grid")
        self.status = tk.StringVar(value="")
        self.global_attachment_status = tk.StringVar(value="")
        self._ready_status_text = ""

        self.frame = tk.Frame(
            host,
            bg=self.colors["panel"],
            bd=2,
            relief="ridge",
        )
        self.frame.grid(row=0, column=0, sticky="nsew")
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(4, weight=1)
        self._build()
        self.frame.bind("<Configure>", self._resize_notice, add="+")
        self.frame.after_idle(self._resize_notice)
        self.apply_locale()
        self._schedule(100)

    def _t(self, key: str) -> str:
        return self._translate(key)

    def _localized_enum(
        self,
        prefix: str,
        value: Any,
        choices: tuple[str, ...],
        *,
        fallback: str | None = None,
    ) -> str:
        normalized = str(value or fallback or "").strip().lower()
        if normalized in choices:
            return self._t(f"{prefix}.{normalized}")
        return normalized or self._t("cockpit.unavailable")

    def _localized_role(self, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return self._localized_enum("cockpit.role", normalized, COCKPIT_ROLES)

    def _snapshot_role(self, snapshot: Mapping[str, Any]) -> str:
        if str(snapshot.get("role") or "").strip().lower() == "custom":
            custom = str(snapshot.get("role_label") or "").strip()
            if custom:
                return custom
        return self._localized_role(snapshot.get("role"))

    def _localized_source(self, value: Any) -> str:
        source = str(value or "").strip().lower()
        if source in SESSION_SOURCE_TYPES:
            return self._t(f"cockpit.source.{source}")
        return self._t("cockpit.unavailable")

    def _localized_state(self, value: Any, *, fallback: str = "unknown") -> str:
        return self._localized_enum(
            "cockpit.state", value, COCKPIT_STATES, fallback=fallback
        )

    def _usage_summary(self, value: Any) -> str:
        if not isinstance(value, Mapping):
            return self._t("cockpit.unavailable")
        status = str(value.get("status") or "unavailable").lower()
        if status == "unavailable":
            return self._t("cockpit.unavailable")

        def token(field: str) -> str:
            count = value.get(field)
            return (
                str(count)
                if isinstance(count, int) and not isinstance(count, bool)
                else self._t("cockpit.unavailable")
            )

        return self._t("cockpit.usage_summary").format(
            status=self._localized_enum(
                "cockpit.usage", status, COCKPIT_USAGE_STATES
            ),
            total=token("total_tokens"),
            input=token("input_tokens"),
            output=token("output_tokens"),
            cached=token("cached_input_tokens"),
            reasoning=token("reasoning_tokens"),
        )

    def _resize_notice(self, event: Any = None) -> None:
        width = int(getattr(event, "width", self.frame.winfo_width()))
        wraplength = max(220, width - 28)
        self.notice_label.configure(wraplength=wraplength)
        if hasattr(self, "status_label"):
            self.status_label.configure(wraplength=wraplength)

    def _button(
        self,
        parent: tk.Misc,
        *,
        command: Callable[[], None],
        color: str,
    ) -> tk.Button:
        return tk.Button(
            parent,
            command=command,
            bg=self.colors[color],
            fg=self.colors["black"] if color in {"cyan", "green", "amber"} else self.colors["text"],
            activebackground=self.colors["cyan"],
            activeforeground=self.colors["black"],
            relief="raised",
            bd=2,
            padx=9,
            pady=5,
            font=("Cascadia Mono", 9, "bold"),
        )

    def _build(self) -> None:
        self.notice_label = tk.Label(
            self.frame,
            bg=self.colors["black"],
            fg=self.colors["amber"],
            anchor="w",
            justify="left",
            padx=12,
            pady=8,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.notice_label.grid(row=0, column=0, sticky="ew")

        launch = tk.Frame(self.frame, bg=self.colors["panel_2"], bd=1, relief="ridge")
        launch.grid(row=1, column=0, sticky="ew", padx=10, pady=(10, 6))
        launch.grid_columnconfigure(1, weight=1)
        self.launch = launch
        self.launch_title = tk.Label(
            launch,
            bg=self.colors["panel_2"],
            fg=self.colors["cyan"],
            anchor="w",
            font=("Cascadia Mono", 9, "bold"),
        )
        self.launch_title.grid(
            row=0, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 2)
        )
        self.launch_labels: dict[str, tk.Label] = {}
        for key in ("agent", "directory"):
            self.launch_labels[key] = tk.Label(
                launch,
                bg=self.colors["panel_2"],
                fg=self.colors["muted"],
                anchor="w",
                font=("Cascadia Mono", 9, "bold"),
            )
        self.launch_labels["agent"].grid(
            row=1, column=0, padx=(10, 5), pady=(7, 4), sticky="w"
        )
        self.agent_combo = ttk.Combobox(
            launch,
            textvariable=self.agent_choice,
            values=tuple(label for label, _agent_id in COCKPIT_AGENTS),
            state="readonly",
            width=18,
        )
        self.agent_combo.grid(
            row=1, column=1, columnspan=2, padx=(0, 10), pady=(7, 4), sticky="ew"
        )
        self.launch_labels["directory"].grid(
            row=2, column=0, columnspan=3, padx=10, pady=(5, 2), sticky="w"
        )
        self.directory_entry = tk.Entry(
            launch,
            textvariable=self.working_directory,
            bg=self.colors["black"],
            fg=self.colors["text"],
            insertbackground=self.colors["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
        )
        self.directory_entry.grid(
            row=3, column=0, columnspan=2, sticky="ew", padx=(10, 5), pady=(2, 8), ipady=4
        )
        self.browse_button = self._button(
            launch, command=self._browse_directory, color="line"
        )
        self.browse_button.grid(row=3, column=2, padx=(5, 10), pady=(2, 8), sticky="e")

        prompt_band = tk.Frame(self.frame, bg=self.colors["panel"])
        prompt_band.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        prompt_band.grid_columnconfigure(0, weight=1)
        self.task_label = tk.Label(
            prompt_band,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Cascadia Mono", 9, "bold"),
        )
        self.task_label.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        self.prompt = tk.Text(
            prompt_band,
            height=3,
            wrap="word",
            bg=self.colors["black"],
            fg=self.colors["text"],
            insertbackground=self.colors["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
            padx=8,
            pady=6,
        )
        self.prompt.grid(row=1, column=0, sticky="ew")
        self._bind_text_editing(self.prompt)
        launch_buttons = tk.Frame(prompt_band, bg=self.colors["panel"])
        launch_buttons.grid(
            row=1, column=1, rowspan=2, padx=(8, 0), sticky="ns"
        )
        self.start_button = self._button(
            launch_buttons, command=lambda: self._start(False), color="line"
        )
        self.start_button.pack(fill="x", pady=(0, 4))
        self.start_send_button = self._button(
            launch_buttons, command=lambda: self._start(True), color="green"
        )
        self.start_send_button.pack(fill="x")

        prompt_tools = tk.Frame(prompt_band, bg=self.colors["panel"])
        prompt_tools.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.global_attach_button = self._button(
            prompt_tools,
            command=lambda: self._choose_prompt_attachments(None),
            color="blue",
        )
        self.global_attach_button.pack(side="left", padx=(0, 4))
        self.global_clear_attachments_button = self._button(
            prompt_tools,
            command=lambda: self._clear_prompt_attachments(None),
            color="line",
        )
        self.global_clear_attachments_button.pack(side="left", padx=4)
        self.global_edit_button = self._button(
            prompt_tools,
            command=lambda: self._show_text_edit_menu(
                self.prompt, self.global_edit_button
            ),
            color="line",
        )
        self.global_edit_button.pack(side="left", padx=4)
        self.global_attachment_label = tk.Label(
            prompt_tools,
            textvariable=self.global_attachment_status,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            anchor="w",
            justify="left",
            font=("Cascadia Mono", 8),
        )
        self.global_attachment_label.pack(
            side="left", fill="x", expand=True, padx=(7, 0)
        )
        self.prompt.bind(
            "<Button-3>",
            lambda event: self._show_text_edit_menu(self.prompt, event=event),
            add="+",
        )
        self._set_prompt_attachment_paths(None, ())

        toolbar = tk.Frame(self.frame, bg=self.colors["panel_2"], bd=1, relief="ridge")
        toolbar.grid(row=3, column=0, sticky="ew", padx=10, pady=(5, 7))
        toolbar.grid_columnconfigure(0, weight=1)
        self.toolbar = toolbar
        self.view_section_title = tk.Label(
            toolbar,
            bg=self.colors["panel_2"],
            fg=self.colors["cyan"],
            anchor="w",
            font=("Cascadia Mono", 9, "bold"),
        )
        self.view_section_title.grid(
            row=0, column=0, sticky="ew", padx=9, pady=(7, 1)
        )
        self.view_buttons: dict[str, tk.Button] = {}
        view_group = tk.Frame(toolbar, bg=self.colors["panel_2"])
        view_group.grid(row=1, column=0, padx=8, pady=(3, 4), sticky="w")
        self.view_group = view_group
        for view in COCKPIT_VIEWS:
            button = self._button(
                view_group,
                command=lambda selected=view: self._set_view(selected),
                color="line",
            )
            button.pack(side="left", padx=2)
            self.view_buttons[view] = button
        selected_group = tk.Frame(toolbar, bg=self.colors["panel_2"])
        selected_group.grid(row=2, column=0, padx=8, pady=4, sticky="ew")
        selected_group.grid_columnconfigure(1, weight=1)
        self.selected_group = selected_group
        self.session_label = tk.Label(
            selected_group,
            bg=self.colors["panel_2"],
            fg=self.colors["muted"],
            font=("Cascadia Mono", 9, "bold"),
        )
        self.session_label.grid(row=0, column=0, padx=(0, 5), sticky="w")
        self.session_combo = ttk.Combobox(
            selected_group,
            textvariable=self.selected_session_label,
            state="readonly",
            width=54,
        )
        self.session_combo.grid(row=0, column=1, sticky="ew")
        self.session_combo.bind("<<ComboboxSelected>>", self._session_combo_changed)
        action_group = tk.Frame(toolbar, bg=self.colors["panel_2"])
        action_group.grid(row=3, column=0, padx=8, pady=(4, 8), sticky="w")
        self.action_group = action_group
        self.send_button = self._button(action_group, command=self._send, color="cyan")
        self.send_button.pack(side="left", padx=2)
        self.interrupt_button = self._button(
            action_group, command=self._interrupt, color="amber"
        )
        self.interrupt_button.pack(side="left", padx=2)
        self.stop_button = self._button(action_group, command=self._stop, color="red")
        self.stop_button.pack(side="left", padx=2)

        content = tk.Frame(self.frame, bg=self.colors["panel"])
        content.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 8))
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)
        self.content = content
        self.panel_canvas = tk.Canvas(
            content,
            bg=self.colors["panel"],
            highlightthickness=0,
            bd=0,
        )
        self.panel_scroll = ttk.Scrollbar(
            content, orient="vertical", command=self.panel_canvas.yview
        )
        self.panel_canvas.configure(yscrollcommand=self.panel_scroll.set)
        self.panel_canvas.grid(row=0, column=0, sticky="nsew")
        self.panel_scroll.grid(row=0, column=1, sticky="ns")
        self.panel_host = tk.Frame(self.panel_canvas, bg=self.colors["panel"])
        self._panel_window = self.panel_canvas.create_window(
            (0, 0), window=self.panel_host, anchor="nw"
        )
        self.panel_host.bind("<Configure>", self._panel_host_configured)
        self.panel_canvas.bind("<Configure>", self._panel_canvas_configured)

        self.timeline_frame = tk.Frame(content, bg=self.colors["panel"])
        self.timeline_frame.grid_rowconfigure(0, weight=1)
        self.timeline_frame.grid_columnconfigure(0, weight=1)
        self.timeline_tree = ttk.Treeview(
            self.timeline_frame,
            columns=("time", "session", "kind", "summary"),
            show="headings",
            selectmode="browse",
        )
        self.timeline_tree.column("time", width=150, stretch=False)
        self.timeline_tree.column("session", width=190, stretch=False)
        self.timeline_tree.column("kind", width=90, stretch=False)
        self.timeline_tree.column("summary", width=520, stretch=True)
        timeline_scroll = ttk.Scrollbar(
            self.timeline_frame, orient="vertical", command=self.timeline_tree.yview
        )
        self.timeline_tree.configure(yscrollcommand=timeline_scroll.set)
        self.timeline_tree.grid(row=0, column=0, sticky="nsew")
        timeline_scroll.grid(row=0, column=1, sticky="ns")

        self.status_label = tk.Label(
            self.frame,
            textvariable=self.status,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            anchor="w",
            justify="left",
            font=("Cascadia Mono", 9, "bold"),
        )
        self.status_label.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 9))

    def _browse_directory(self) -> None:
        initial = Path(self.working_directory.get() or self.project_root)
        if not initial.is_dir():
            initial = self.project_root
        selected = filedialog.askdirectory(
            parent=self.root,
            initialdir=str(initial),
            title=self._t("cockpit.directory_dialog"),
            mustexist=True,
        )
        if selected:
            self.working_directory.set(selected)

    def _selected_agent(self) -> str:
        return dict(COCKPIT_AGENTS).get(self.agent_choice.get(), "codex")

    def _prompt_text(self) -> str:
        return self.prompt.get("1.0", "end-1c")

    @staticmethod
    def _set_text_variable(variable: tk.StringVar, value: str) -> None:
        if variable.get() != value:
            variable.set(value)

    @staticmethod
    def _text_selection(widget: tk.Text) -> tuple[Any, Any] | None:
        try:
            ranges = widget.tag_ranges("sel")
        except tk.TclError:
            return None
        return (ranges[0], ranges[1]) if len(ranges) >= 2 else None

    def _copy_text_selection(self, widget: tk.Text) -> bool:
        selection = self._text_selection(widget)
        if selection is None:
            return False
        try:
            value = widget.get(selection[0], selection[1])
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
        except tk.TclError:
            return False
        return True

    def _cut_text_selection(self, widget: tk.Text) -> bool:
        selection = self._text_selection(widget)
        if selection is None or not self._copy_text_selection(widget):
            return False
        try:
            widget.delete(selection[0], selection[1])
        except tk.TclError:
            return False
        return True

    def _paste_text(self, widget: tk.Text) -> bool:
        try:
            value = self.root.clipboard_get()
            selection = self._text_selection(widget)
            if selection is not None:
                widget.delete(selection[0], selection[1])
            widget.insert("insert", value)
        except tk.TclError:
            return False
        return True

    @staticmethod
    def _select_all_text(widget: tk.Text) -> bool:
        try:
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "end-1c")
            widget.see("insert")
        except tk.TclError:
            return False
        return True

    def _bind_text_editing(self, widget: tk.Text) -> None:
        """Install deterministic Windows text editing bindings on a cockpit prompt."""

        def bind(sequence: str, action: Callable[[tk.Text], bool]) -> None:
            def invoke(_event: Any = None) -> str:
                action(widget)
                return "break"

            widget.bind(sequence, invoke, add="+")

        for sequence in ("<Control-c>", "<Control-C>", "<Control-Insert>"):
            bind(sequence, self._copy_text_selection)
        for sequence in ("<Control-x>", "<Control-X>", "<Shift-Delete>"):
            bind(sequence, self._cut_text_selection)
        for sequence in ("<Control-v>", "<Control-V>", "<Shift-Insert>"):
            bind(sequence, self._paste_text)
        for sequence in ("<Control-a>", "<Control-A>"):
            bind(sequence, self._select_all_text)

    def _show_text_edit_menu(
        self,
        widget: tk.Text,
        anchor: tk.Widget | None = None,
        *,
        event: Any = None,
    ) -> str:
        try:
            widget.focus_set()
            selection = self._text_selection(widget)
            editable = str(widget.cget("state")) != "disabled"
            try:
                self.root.clipboard_get()
                can_paste = editable
            except tk.TclError:
                can_paste = False
            menu = tk.Menu(self.root, tearoff=False)
            menu.add_command(
                label=self._t("edit.cut"),
                command=lambda: self._cut_text_selection(widget),
                state="normal" if editable and selection is not None else "disabled",
            )
            menu.add_command(
                label=self._t("edit.copy"),
                command=lambda: self._copy_text_selection(widget),
                state="normal" if selection is not None else "disabled",
            )
            menu.add_command(
                label=self._t("edit.paste"),
                command=lambda: self._paste_text(widget),
                state="normal" if can_paste else "disabled",
            )
            menu.add_separator()
            menu.add_command(
                label=self._t("edit.select_all"),
                command=lambda: self._select_all_text(widget),
            )
            if event is not None:
                x, y = int(event.x_root), int(event.y_root)
            else:
                target = anchor or widget
                x = target.winfo_rootx()
                y = target.winfo_rooty() + target.winfo_height()
            menu.tk_popup(x, y)
        except tk.TclError:
            return "break"
        finally:
            with contextlib.suppress(tk.TclError, UnboundLocalError):
                menu.grab_release()
        return "break"

    def _prompt_attachment_paths(self, panel_id: str | None) -> tuple[Path, ...]:
        if panel_id is None:
            return tuple(getattr(self, "_global_attachment_paths", ()) or ())
        panel = getattr(self, "_panels", {}).get(str(panel_id))
        return tuple(panel.get("attachment_paths") or ()) if panel else ()

    def _set_prompt_attachment_paths(
        self, panel_id: str | None, paths: Iterable[Path]
    ) -> None:
        selected = tuple(Path(path) for path in paths)[:5]
        status_text = (
            self._t("chat.attachments_selected").format(count=len(selected))
            if selected
            else self._t("chat.no_attachments")
        )
        if panel_id is None:
            self._global_attachment_paths = selected
            self._set_text_variable(self.global_attachment_status, status_text)
            self.global_clear_attachments_button.configure(
                state="normal" if selected else "disabled"
            )
            return
        panel = self._panels.get(str(panel_id))
        if panel is None:
            return
        panel["attachment_paths"] = selected
        self._set_text_variable(panel["attachment_status"], status_text)
        panel["clear_attachments_button"].configure(
            state="normal" if selected else "disabled"
        )

    def _choose_prompt_attachments(self, panel_id: str | None) -> None:
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title=self._t("chat.attach"),
            filetypes=COCKPIT_ATTACHMENT_FILETYPES,
        )
        if selected:
            self._set_prompt_attachment_paths(
                panel_id, (Path(value) for value in selected)
            )

    def _clear_prompt_attachments(self, panel_id: str | None) -> None:
        self._set_prompt_attachment_paths(panel_id, ())

    @staticmethod
    def _prompt_with_artifacts(prompt: str, artifact_paths: Iterable[str]) -> str:
        paths = tuple(str(path) for path in artifact_paths if str(path).strip())
        if not paths:
            return prompt
        references = "\n".join(f"- {path}" for path in paths)
        return (
            f"{prompt.rstrip()}\n\n"
            "[PeerBridge project-root-relative attachments]\n"
            f"{references}"
        )

    @staticmethod
    def _prompt_sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _set_busy(
        self,
        action: str | None,
        *,
        prompt_sha256: str | None = None,
        prompt_target: str | None = None,
        attachment_paths: Iterable[Path] = (),
    ) -> bool:
        if action is not None and self._action_inflight is not None:
            self.status.set(self._t("cockpit.status.busy"))
            return False
        self._action_inflight = action
        if action is not None:
            self._pending_prompt_sha256 = prompt_sha256
            self._pending_prompt_target = prompt_target
            self._pending_attachment_paths = tuple(attachment_paths)
        launch_state = "disabled" if action is not None else "normal"
        self.start_button.configure(state=launch_state)
        self.start_send_button.configure(state=launch_state)
        for button in (self.send_button, self.interrupt_button, self.stop_button):
            button.configure(state="disabled" if action is not None else "normal")
        self.agent_combo.configure(state="disabled" if action else "readonly")
        self.directory_entry.configure(state=launch_state)
        self.browse_button.configure(state=launch_state)
        self.global_attach_button.configure(state=launch_state)
        self.global_edit_button.configure(state=launch_state)
        self.global_clear_attachments_button.configure(
            state=(
                "normal"
                if action is None and self._global_attachment_paths
                else "disabled"
            )
        )
        self._sync_panel_inputs()
        if action is None:
            self._sync_session_actions()
        return True

    def _working_path(self) -> Path:
        selected = self.working_directory.get().strip() or self.project_root
        return cockpit_working_directory(self.project_root, selected)

    def _start(self, submit: bool) -> None:
        prompt = self._prompt_text()
        selected_attachments = self._prompt_attachment_paths(None) if submit else ()
        if submit and not prompt.strip() and not selected_attachments:
            self.status.set(self._t("cockpit.status.prompt_required"))
            return
        agent_id = self._selected_agent()
        role = COCKPIT_DEFAULT_LAUNCH_ROLE
        try:
            working_directory = self._working_path()
        except ManagedAgentError as exc:
            self.status.set(
                self._t("cockpit.status.failed").format(error=str(exc)[:180])
            )
            self.status_label.configure(fg=self.colors["red"])
            return
        session_id = (
            f"{agent_id}-{datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        prompt_sha = self._prompt_sha256(prompt) if submit else None
        if not self._set_busy(
            "start_send" if submit else "start",
            prompt_sha256=prompt_sha,
            attachment_paths=selected_attachments,
        ):
            return
        self.status.set(self._t("cockpit.status.starting"))

        def worker() -> None:
            try:
                staged = (
                    stage_chat_attachments(self.project_root, selected_attachments)
                    if selected_attachments
                    else ()
                )
                start_official = getattr(self.manager, "start_official", None)
                if callable(start_official):
                    start_official(
                        agent_id=agent_id,
                        session_id=session_id,
                        role=role,
                        working_directory=working_directory,
                        project_root=self.project_root,
                        input_text=prompt if submit and prompt.strip() else None,
                        attachments=staged,
                    )
                else:
                    input_text = self._prompt_with_artifacts(
                        prompt, (item.relative_path for item in staged)
                    )
                    launch = build_observe_launch(
                        agent_id,
                        session_id=session_id,
                        role=role,
                        working_directory=working_directory,
                    )
                    self.manager.start(
                        launch,
                        input_text=input_text if submit else None,
                    )
                self._action_results.put(
                    {"ok": True, "action": "start_send" if submit else "start", "session_id": session_id}
                )
            except Exception as exc:
                self._action_results.put(
                    {"ok": False, "action": "start", "error": redact_secrets(str(exc))}
                )

        threading.Thread(target=worker, name="peerbridge-cockpit-start", daemon=True).start()

    def _session_action(
        self,
        action: str,
        *,
        panel_id: str | None = None,
        prompt_widget: tk.Text | None = None,
    ) -> None:
        panel_id = str(panel_id or self.selected_session.get()).strip()
        if not panel_id:
            self.status.set(self._t("cockpit.status.select_session"))
            return
        snapshot = self._latest_snapshots.get(panel_id)
        if not cockpit_action_allowed(snapshot, action):
            self.status.set(self._t("cockpit.status.source_read_only"))
            self.status_label.configure(fg=self.colors["amber"])
            return
        session_id = str(snapshot.get("source_session_id") or "")
        source_type = str(snapshot.get("source_type") or "")
        prompt = (
            prompt_widget.get("1.0", "end-1c")
            if action == "send" and prompt_widget is not None
            else self._prompt_text()
            if action == "send"
            else ""
        )
        selected_attachments = (
            self._prompt_attachment_paths(panel_id if prompt_widget is not None else None)
            if action == "send"
            else ()
        )
        if action == "send" and not prompt.strip() and not selected_attachments:
            self.status.set(self._t("cockpit.status.prompt_required"))
            return
        prompt_sha = self._prompt_sha256(prompt) if prompt else None
        if not self._set_busy(
            action,
            prompt_sha256=prompt_sha,
            prompt_target=panel_id if prompt_widget is not None else None,
            attachment_paths=selected_attachments,
        ):
            return
        self.status.set(self._t(f"cockpit.status.{action}ing"))

        def worker() -> None:
            try:
                staged = (
                    stage_chat_attachments(self.project_root, selected_attachments)
                    if selected_attachments
                    else ()
                )
                artifact_paths = tuple(item.relative_path for item in staged)
                receipt: Mapping[str, Any] | None = None
                if source_type == "peerbridge-room" and action == "send":
                    if self._room_sender is None:
                        raise ManagedAgentError(
                            "PeerBridge room input adapter is unavailable"
                        )
                    receipt = self._room_sender(snapshot, prompt, artifact_paths)
                else:
                    session = self.manager.get(session_id)
                    if action == "send":
                        if bool(
                            getattr(session, "supports_verified_attachments", False)
                        ):
                            session.submit(prompt, attachments=staged)
                        else:
                            session.submit(
                                self._prompt_with_artifacts(prompt, artifact_paths)
                            )
                    elif action == "interrupt":
                        session.interrupt()
                    elif action == "stop":
                        session.stop()
                    else:
                        raise ManagedAgentError("unsupported managed Agent action")
                self._action_results.put(
                    {
                        "ok": True,
                        "action": action,
                        "session_id": panel_id,
                        "source_type": source_type,
                        "receipt": dict(receipt or {}),
                    }
                )
            except Exception as exc:
                self._action_results.put(
                    {"ok": False, "action": action, "error": redact_secrets(str(exc))}
                )

        threading.Thread(
            target=worker,
            name=f"peerbridge-cockpit-{action}",
            daemon=True,
        ).start()

    def _send(self) -> None:
        self._session_action("send")

    def _send_panel(self, panel_id: str) -> None:
        panel = self._panels.get(str(panel_id))
        if panel is None:
            self.status.set(self._t("cockpit.status.source_unavailable"))
            self.status_label.configure(fg=self.colors["amber"])
            return
        self._session_action(
            "send",
            panel_id=str(panel_id),
            prompt_widget=panel["prompt"],
        )

    def _panel_submit_event(self, panel_id: str, event: Any = None) -> str | None:
        if int(getattr(event, "state", 0)) & 0x0001:
            return None
        self._send_panel(panel_id)
        return "break"

    def _interrupt(self) -> None:
        self._session_action("interrupt")

    def _stop(self) -> None:
        self._session_action("stop")

    def _drain_action_results(self) -> None:
        while True:
            try:
                result = self._action_results.get_nowait()
            except queue.Empty:
                return
            self._set_busy(None)
            if not result.get("ok"):
                error = str(result.get("error") or self._t("cockpit.status.unknown_error"))
                self.status.set(
                    self._t("cockpit.status.failed").format(error=error[:180])
                )
                self.status_label.configure(fg=self.colors["red"])
                self._pending_prompt_sha256 = None
                self._pending_prompt_target = None
                self._pending_attachment_paths = ()
                continue
            action = str(result.get("action") or "")
            session_id = str(result.get("session_id") or "")
            if session_id and action in {"start", "start_send"}:
                self._select_session(session_id)
            if action in {"send", "start_send"} and self._pending_prompt_sha256:
                target = getattr(self, "_pending_prompt_target", None)
                prompt_widget = None
                if target:
                    panel = self._panels.get(target)
                    if panel is not None:
                        prompt_widget = panel.get("prompt")
                if prompt_widget is None and not target:
                    prompt_widget = self.prompt
                if prompt_widget is not None:
                    current = prompt_widget.get("1.0", "end-1c")
                    if self._prompt_sha256(current) == self._pending_prompt_sha256:
                        prompt_widget.delete("1.0", "end")
                attachment_target = target if target else None
                pending_attachments = getattr(
                    self, "_pending_attachment_paths", None
                )
                if pending_attachments is not None and (
                    self._prompt_attachment_paths(attachment_target)
                    == pending_attachments
                ):
                    self._clear_prompt_attachments(attachment_target)
            source_type = str(result.get("source_type") or "")
            status_key = (
                "cockpit.status.room_send_complete"
                if action == "send" and source_type == "peerbridge-room"
                else f"cockpit.status.{action}_complete"
            )
            self.status.set(self._t(status_key))
            self.status_label.configure(fg=self.colors["green"])
            if source_type == "peerbridge-room" and self._external_input_complete:
                self._external_input_complete(result.get("receipt") or {})
            self._pending_prompt_sha256 = None
            self._pending_prompt_target = None
            self._pending_attachment_paths = ()

    def _schedule(self, delay_ms: int) -> None:
        if self._closed or self._after_id is not None:
            return
        self._after_id = self.root.after(max(50, int(delay_ms)), self._poll)

    def _poll(self) -> None:
        self._after_id = None
        if self._closed:
            return
        self._drain_action_results()
        active = self.render()
        self._schedule(
            COCKPIT_REFRESH_ACTIVE_MS if active else COCKPIT_REFRESH_IDLE_MS
        )

    def _new_text_tab(self, notebook: ttk.Notebook, key: str) -> tuple[tk.Frame, tk.Text]:
        tab = tk.Frame(notebook, bg=self.colors["black"])
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        text = tk.Text(
            tab,
            height=COCKPIT_PANEL_TEXT_ROWS,
            wrap="word",
            state="disabled",
            bg=self.colors["black"],
            fg=self.colors["text"],
            selectbackground=self.colors["blue"],
            relief="flat",
            bd=0,
            padx=8,
            pady=7,
            font=("Cascadia Mono", 9),
        )
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        notebook.add(tab, text=self._t(f"cockpit.tab.{key}"))
        return tab, text

    def _ensure_panel(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        session_id = str(snapshot["session_id"])
        current = self._panels.get(session_id)
        if current is not None:
            return current
        frame = tk.Frame(
            self.panel_host,
            bg=self.colors["panel_2"],
            bd=2,
            relief="ridge",
            height=COCKPIT_PANEL_HEIGHT,
        )
        frame.grid_propagate(False)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)
        header_value = tk.StringVar()
        header = tk.Radiobutton(
            frame,
            textvariable=header_value,
            variable=self.selected_session,
            value=session_id,
            command=self._selection_changed,
            indicatoron=False,
            anchor="w",
            bg=self.colors["line"],
            fg=self.colors["text"],
            activebackground=self.colors["cyan"],
            activeforeground=self.colors["black"],
            selectcolor=self.colors["cyan"],
            relief="flat",
            padx=9,
            pady=6,
            font=("Cascadia Mono", 9, "bold"),
        )
        header.grid(row=0, column=0, sticky="ew")
        meta_value = tk.StringVar()
        meta_label = tk.Label(
            frame,
            textvariable=meta_value,
            bg=self.colors["panel_2"],
            fg=self.colors["muted"],
            anchor="w",
            justify="left",
            wraplength=400,
            font=("Cascadia Mono", 8, "bold"),
        )
        meta_label.grid(row=1, column=0, sticky="ew", padx=9, pady=(6, 3))
        activity_value = tk.StringVar()
        activity_label = tk.Label(
            frame,
            textvariable=activity_value,
            bg=self.colors["panel_2"],
            fg=self.colors["cyan"],
            anchor="w",
            justify="left",
            wraplength=400,
            font=("Cascadia Mono", 9, "bold"),
        )
        activity_label.grid(row=2, column=0, sticky="ew", padx=9, pady=(2, 5))
        notebook = ttk.Notebook(frame)
        notebook.grid(row=3, column=0, sticky="nsew", padx=7, pady=(3, 4))
        tabs: dict[str, tk.Frame] = {}
        texts: dict[str, tk.Text] = {}
        for key in ("terminal", "activity", "answer", "evidence"):
            tab, text = self._new_text_tab(notebook, key)
            tabs[key] = tab
            texts[key] = text
        composer = tk.Frame(frame, bg=self.colors["panel_2"])
        composer.grid(row=4, column=0, sticky="ew", padx=7, pady=(2, 7))
        composer.grid_columnconfigure(0, weight=1)
        prompt_label = tk.Label(
            composer,
            bg=self.colors["panel_2"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Cascadia Mono", 8, "bold"),
        )
        prompt_label.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        prompt = tk.Text(
            composer,
            height=3,
            wrap="word",
            bg=self.colors["black"],
            fg=self.colors["text"],
            insertbackground=self.colors["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
            padx=7,
            pady=5,
        )
        prompt.grid(row=1, column=0, sticky="ew")
        self._bind_text_editing(prompt)
        panel_send_button = self._button(
            composer,
            command=lambda selected=session_id: self._send_panel(selected),
            color="cyan",
        )
        panel_send_button.grid(row=1, column=1, sticky="ns", padx=(7, 0))
        prompt_tools = tk.Frame(composer, bg=self.colors["panel_2"])
        prompt_tools.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        panel_attach_button = self._button(
            prompt_tools,
            command=lambda selected=session_id: self._choose_prompt_attachments(
                selected
            ),
            color="blue",
        )
        panel_attach_button.pack(side="left", padx=(0, 3))
        panel_clear_attachments_button = self._button(
            prompt_tools,
            command=lambda selected=session_id: self._clear_prompt_attachments(
                selected
            ),
            color="line",
        )
        panel_clear_attachments_button.pack(side="left", padx=3)
        panel_edit_button = self._button(
            prompt_tools,
            command=lambda selected_prompt=prompt: self._show_text_edit_menu(
                selected_prompt, panel_edit_button
            ),
            color="line",
        )
        panel_edit_button.pack(side="left", padx=3)
        attachment_status = tk.StringVar(value=self._t("chat.no_attachments"))
        attachment_status_label = tk.Label(
            prompt_tools,
            textvariable=attachment_status,
            bg=self.colors["panel_2"],
            fg=self.colors["muted"],
            anchor="w",
            justify="left",
            font=("Cascadia Mono", 8),
        )
        attachment_status_label.pack(
            side="left", fill="x", expand=True, padx=(6, 0)
        )
        input_status = tk.StringVar()
        input_status_label = tk.Label(
            composer,
            textvariable=input_status,
            bg=self.colors["panel_2"],
            fg=self.colors["muted"],
            anchor="w",
            justify="left",
            font=("Cascadia Mono", 8),
        )
        input_status_label.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        prompt.bind(
            "<Return>",
            lambda event, selected=session_id: self._panel_submit_event(
                selected, event
            ),
        )
        prompt.bind(
            "<Button-3>",
            lambda event, selected_prompt=prompt: self._show_text_edit_menu(
                selected_prompt, event=event
            ),
            add="+",
        )
        panel = {
            "session_id": session_id,
            "frame": frame,
            "header": header_value,
            "meta": meta_value,
            "meta_label": meta_label,
            "activity": activity_value,
            "activity_label": activity_label,
            "notebook": notebook,
            "tabs": tabs,
            "texts": texts,
            "prompt_label": prompt_label,
            "prompt": prompt,
            "send_button": panel_send_button,
            "attach_button": panel_attach_button,
            "clear_attachments_button": panel_clear_attachments_button,
            "edit_button": panel_edit_button,
            "attachment_paths": (),
            "attachment_status": attachment_status,
            "attachment_status_label": attachment_status_label,
            "input_status": input_status,
            "input_status_label": input_status_label,
            "latest_sequence": 0,
            "first_retained_sequence": 0,
            "current_activity": None,
            "source_type": str(snapshot.get("source_type") or "managed-cli"),
            "source_session_id": str(
                snapshot.get("source_session_id") or snapshot.get("session_id") or ""
            ),
            "room_id": str(snapshot.get("room_id") or "").strip(),
        }
        self._panels[session_id] = panel
        self._set_prompt_attachment_paths(session_id, ())
        self._sync_panel_input(panel, snapshot)
        return panel

    def _sync_panel_input(
        self, panel: Mapping[str, Any], snapshot: Mapping[str, Any] | None
    ) -> None:
        allowed = cockpit_action_allowed(snapshot, "send")
        busy = self._action_inflight is not None
        source_type = str((snapshot or {}).get("source_type") or "")
        panel["prompt"].configure(state="normal" if allowed else "disabled")
        panel["send_button"].configure(
            state="normal" if allowed and not busy else "disabled"
        )
        panel["attach_button"].configure(
            state="normal" if allowed and not busy else "disabled"
        )
        panel["edit_button"].configure(
            state="normal" if allowed and not busy else "disabled"
        )
        panel["clear_attachments_button"].configure(
            state=(
                "normal"
                if allowed and not busy and panel.get("attachment_paths")
                else "disabled"
            )
        )
        panel["prompt_label"].configure(text=self._t("cockpit.panel_input"))
        panel["send_button"].configure(text=self._t("cockpit.send_panel"))
        panel["attach_button"].configure(text=self._t("chat.attach"))
        panel["clear_attachments_button"].configure(
            text=self._t("chat.clear_attachments")
        )
        panel["edit_button"].configure(text=self._t("cockpit.edit"))
        status_key = (
            "cockpit.panel_input.busy"
            if allowed and busy
            else "cockpit.panel_input.ready"
            if allowed
            else "cockpit.panel_input.room_unavailable"
            if source_type == "peerbridge-room"
            else "cockpit.panel_input.read_only"
        )
        self._set_text_variable(panel["input_status"], self._t(status_key))
        panel["input_status_label"].configure(
            fg=self.colors["amber"] if not allowed or busy else self.colors["green"]
        )

    def _sync_panel_inputs(self) -> None:
        for session_id, panel in self._panels.items():
            self._sync_panel_input(panel, self._latest_snapshots.get(session_id))

    @staticmethod
    def _replace_text(widget: tk.Text, value: str) -> None:
        if widget.get("1.0", "end-1c") == value:
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if value:
            widget.insert("end", value)
        widget.configure(state="disabled")

    @staticmethod
    def _append_text(widget: tk.Text, value: str) -> None:
        if not value:
            return
        widget.configure(state="normal")
        if widget.index("end-1c") != "1.0":
            widget.insert("end", "\n\n")
        widget.insert("end", value)
        widget.see("end")
        widget.configure(state="disabled")

    def _record_timeline_event(
        self, snapshot: Mapping[str, Any], event: Mapping[str, Any]
    ) -> None:
        key = (
            str(snapshot.get("source_type") or ""),
            str(snapshot.get("room_id") or "").strip(),
            str(snapshot["session_id"]),
            int(event.get("sequence") or 0),
        )
        if key in self._timeline_keys:
            return
        if len(self._timeline) >= COCKPIT_TIMELINE_LIMIT:
            removed = self._timeline.popleft()
            self._timeline_keys.discard(
                (
                    str(removed.get("source_type") or ""),
                    str(removed.get("room_id") or ""),
                    str(removed["session_id"]),
                    int(removed["sequence"]),
                )
            )
        row = cockpit_timeline_rows(
            [{**dict(snapshot), "events": [dict(event)]}], limit=1
        )[0]
        self._timeline.append(row)
        self._timeline_keys.add(key)

    def _append_events(
        self, panel: dict[str, Any], snapshot: Mapping[str, Any]
    ) -> None:
        for event in snapshot.get("events") or ():
            created = str(event.get("created_utc") or "")
            clock = created[11:19] if len(created) >= 19 else "--:--:--"
            stream = self._localized_enum(
                "cockpit.stream",
                event.get("stream"),
                COCKPIT_STREAMS,
                fallback="system",
            )
            kind = self._localized_enum(
                "cockpit.kind",
                event.get("kind"),
                COCKPIT_EVENT_KINDS,
                fallback="terminal",
            )
            text = str(event.get("text") or "")
            summary = str(event.get("summary") or "")
            if cockpit_event_visible_in_terminal(snapshot, event):
                self._append_text(
                    panel["texts"]["terminal"],
                    f"[{clock}] {stream} / {kind}\n{text}",
                )
            if str(event.get("kind")) in {"system", "activity", "answer", "error"}:
                activity = cockpit_activity_projection(event)
                action = str(activity["action"])
                action_label = self._t(f"cockpit.activity.{action}")
                detail = str(activity.get("detail") or "")
                self._append_text(
                    panel["texts"]["activity"],
                    f"[{clock}] {action_label}"
                    + (f"\n{detail}" if detail else ""),
                )
            if str(event.get("kind")) == "answer":
                self._append_text(
                    panel["texts"]["answer"],
                    f"[{clock}]\n{summary or text}",
                )
            panel["latest_sequence"] = max(
                int(panel["latest_sequence"]), int(event.get("sequence") or 0)
            )
            self._record_timeline_event(snapshot, event)

    def _source_contract_text(self, snapshot: Mapping[str, Any]) -> str:
        capabilities = snapshot.get("capabilities")
        if not isinstance(capabilities, Mapping):
            capabilities = {}
        capability_text = " | ".join(
            self._t("cockpit.capability_value").format(
                capability=self._t(f"cockpit.capability.{key}"),
                value=self._t(
                    "cockpit.yes" if capabilities.get(key) else "cockpit.no"
                ),
            )
            for key in SESSION_CAPABILITIES
        )
        return self._t("cockpit.source_contract").format(
            source=self._localized_source(snapshot.get("source_type")),
            session=snapshot.get("source_session_id") or "--",
            owner=snapshot.get("input_owner") or self._t("cockpit.unavailable"),
            capabilities=capability_text,
        )

    def _sync_terminal_surface(
        self, panel: dict[str, Any], snapshot: Mapping[str, Any]
    ) -> None:
        capabilities = snapshot.get("capabilities")
        route_only = bool(
            str(snapshot.get("source_type") or "") == "peerbridge-room"
            and isinstance(capabilities, Mapping)
            and capabilities.get("model_route_only")
        )
        terminal_tab = panel["tabs"]["terminal"]
        panel["notebook"].tab(
            terminal_tab,
            text=(
                self._localized_source(snapshot.get("source_type"))
                if route_only
                else self._t("cockpit.tab.terminal")
            ),
        )
        if route_only:
            notice = self._source_contract_text(snapshot)
            signature = ("route-only", notice)
            if panel.get("terminal_surface_signature") != signature:
                self._replace_text(panel["texts"]["terminal"], notice)
                panel["terminal_surface_signature"] = signature
        else:
            panel["terminal_surface_signature"] = ("terminal",)

    def _evidence_text(self, snapshot: Mapping[str, Any]) -> str:
        unavailable = self._t("cockpit.unavailable")

        def shown(value: Any) -> str:
            return unavailable if value is None or value == "" else str(value)

        route = shown(snapshot.get("requested_route"))
        observed = shown(snapshot.get("observed_route"))
        usage = snapshot.get("usage")
        if not isinstance(usage, Mapping):
            usage = {}
        outcome = snapshot.get("terminal_outcome")
        if not isinstance(outcome, Mapping):
            outcome = {}
        evidence = self._t("cockpit.evidence_template").format(
            session=snapshot.get("session_id") or "--",
            agent=snapshot.get("display_name") or snapshot.get("agent_id") or "--",
            client=shown(snapshot.get("client_name")),
            client_version=shown(snapshot.get("client_version")),
            model=shown(snapshot.get("model_id")),
            model_source=shown(snapshot.get("model_source")),
            role=self._snapshot_role(snapshot),
            directory=snapshot.get("working_directory") or unavailable,
            requested=route,
            observed=observed,
            observed_source=shown(snapshot.get("observed_route_source")),
            capture=snapshot.get("capture_mode") or "--",
            contract=snapshot.get("reasoning_contract") or "--",
            mode=snapshot.get("execution_mode") or "--",
            binding=snapshot.get("governance_binding_id") or unavailable,
            submitted=self._t(
                "cockpit.yes" if snapshot.get("input_submitted") else "cockpit.no"
            ),
            usage_status=self._localized_enum(
                "cockpit.usage",
                usage.get("status"),
                COCKPIT_USAGE_STATES,
                fallback="unavailable",
            ),
            usage_source=shown(usage.get("source")),
            usage_input=shown(usage.get("input_tokens")),
            usage_output=shown(usage.get("output_tokens")),
            usage_total=shown(usage.get("total_tokens")),
            usage_cached=shown(usage.get("cached_input_tokens")),
            usage_reasoning=shown(usage.get("reasoning_tokens")),
            usage_bounded=self._t(
                "cockpit.yes" if snapshot.get("usage_capture_bounded") else "cockpit.no"
            ),
            usage_truncated=self._t(
                "cockpit.yes" if snapshot.get("usage_capture_truncated") else "cockpit.no"
            ),
            terminal=self._localized_state(
                outcome.get("status"), fallback="unavailable"
            ),
            process_status=self._localized_state(
                outcome.get("process_status"), fallback="unavailable"
            ),
            exit_code=shown(outcome.get("exit_code")),
            provider_status=self._localized_state(
                outcome.get("provider_status"), fallback="unavailable"
            ),
            provider_reason=shown(outcome.get("provider_reason")),
            outcome_source=shown(outcome.get("source")),
            first=snapshot.get("first_retained_sequence") or 0,
            latest=snapshot.get("latest_sequence") or 0,
        )
        source_contract = self._source_contract_text(snapshot)
        return f"{evidence}\n\n{source_contract}"

    def _reset_panel_binding(
        self, panel: dict[str, Any], snapshot: Mapping[str, Any]
    ) -> None:
        """Drop in-memory output when a source violates immutable panel binding."""

        for text in panel["texts"].values():
            self._replace_text(text, "")
        panel["latest_sequence"] = 0
        panel["first_retained_sequence"] = 0
        panel["current_activity"] = None
        panel.pop("terminal_surface_signature", None)
        panel["source_type"] = str(snapshot.get("source_type") or "")
        panel["source_session_id"] = str(
            snapshot.get("source_session_id") or snapshot.get("session_id") or ""
        )
        panel["room_id"] = str(snapshot.get("room_id") or "").strip()

        session_id = str(panel["session_id"])
        self._timeline = deque(
            row for row in self._timeline if str(row["session_id"]) != session_id
        )
        self._timeline_keys = {
            (
                str(row.get("source_type") or ""),
                str(row.get("room_id") or ""),
                str(row["session_id"]),
                int(row["sequence"]),
            )
            for row in self._timeline
        }
        self._timeline_signature = ()

    def _update_panel(
        self, panel: dict[str, Any], snapshot: Mapping[str, Any]
    ) -> None:
        if cockpit_session_binding_changed(panel, snapshot):
            self._reset_panel_binding(panel, snapshot)
        panel["source_type"] = str(snapshot.get("source_type") or "managed-cli")
        panel["source_session_id"] = str(
            snapshot.get("source_session_id") or snapshot.get("session_id") or ""
        )
        panel["room_id"] = str(snapshot.get("room_id") or "").strip()
        first = int(snapshot.get("first_retained_sequence") or 0)
        if panel["first_retained_sequence"] and first > panel["first_retained_sequence"]:
            if snapshot.get("source_type") == "managed-cli":
                full = managed_cli_session_contract(
                    self.manager.get(str(snapshot["source_session_id"])).snapshot()
                )
            else:
                full = dict(snapshot)
            for key in ("terminal", "activity", "answer"):
                self._replace_text(panel["texts"][key], "")
            panel["latest_sequence"] = 0
            snapshot = full
            first = int(snapshot.get("first_retained_sequence") or 0)
        panel["first_retained_sequence"] = first
        self._append_events(panel, snapshot)
        self._sync_terminal_surface(panel, snapshot)
        elapsed = cockpit_elapsed_seconds(snapshot)
        self._set_text_variable(
            panel["header"],
            self._t("cockpit.panel_header").format(
                agent=(
                    f"{self._localized_source(snapshot.get('source_type'))} / "
                    f"{snapshot.get('display_name') or snapshot.get('agent_id') or '--'}"
                ),
                state=self._localized_state(snapshot.get("state")),
            ),
        )
        activity = cockpit_current_activity(
            snapshot,
            previous=panel.get("current_activity"),
        )
        panel["current_activity"] = activity
        activity_action = str(activity["action"])
        activity_detail = str(activity.get("detail") or "")
        self._set_text_variable(
            panel["activity"],
            self._t("cockpit.activity.current").format(
                action=self._t(f"cockpit.activity.{activity_action}"),
                detail=f" // {activity_detail}" if activity_detail else "",
            ),
        )
        panel["activity_label"].configure(
            fg=self.colors[
                "red"
                if activity_action == "error"
                else "green"
                if activity_action == "answer"
                else "amber"
                if activity_action in {"waiting", "unavailable"}
                else "cyan"
            ]
        )
        self._set_text_variable(
            panel["meta"],
            self._t("cockpit.panel_meta").format(
                room=(
                    snapshot.get("room_name")
                    or snapshot.get("room_id")
                    or self._t("cockpit.unavailable")
                ),
                conversation=(
                    snapshot.get("source_conversation_name")
                    or snapshot.get("source_conversation_id")
                    or self._t("cockpit.unavailable")
                ),
                source=self._localized_source(snapshot.get("source_type")),
                client=snapshot.get("client_name") or self._t("cockpit.unavailable"),
                model=snapshot.get("model_id") or self._t("cockpit.unavailable"),
                role=self._snapshot_role(snapshot),
                elapsed=elapsed,
                requested=snapshot.get("requested_route") or self._t("cockpit.unavailable"),
                observed=snapshot.get("observed_route") or self._t("cockpit.unavailable"),
                usage=self._usage_summary(snapshot.get("usage")),
                outcome=self._localized_state(
                    (snapshot.get("terminal_outcome") or {}).get("status")
                    if isinstance(snapshot.get("terminal_outcome"), Mapping)
                    else None,
                    fallback="unavailable",
                ),
            ),
        )
        self._replace_text(panel["texts"]["evidence"], self._evidence_text(snapshot))
        self._sync_panel_input(panel, snapshot)

    def _render_timeline(self) -> None:
        ordered = sorted(
            (
                row
                for row in self._timeline
                if cockpit_visible_in_room_context(row, self._room_context_id)
            ),
            key=lambda row: (
                row["created_utc"],
                row["session_id"],
                row["sequence"],
            ),
        )
        signature = tuple(
            [(f"room:{self._room_context_id}", -1)]
            + [
                (str(row["session_id"]), int(row["sequence"]))
                for row in ordered
            ]
        )
        if signature == self._timeline_signature:
            return
        self._timeline_signature = signature
        self.timeline_tree.delete(*self.timeline_tree.get_children())
        for index, row in enumerate(ordered):
            self.timeline_tree.insert(
                "",
                "end",
                iid=f"cockpit-event-{index}",
                values=(
                    row["created_utc"],
                    f"{row['display_name']} [{row['session_id']}]",
                    self._localized_enum(
                        "cockpit.kind",
                        row["kind"],
                        COCKPIT_EVENT_KINDS,
                        fallback="terminal",
                    ),
                    str(row["text"])[:400],
                ),
            )

    def _layout_panels(self) -> None:
        mode = self.view_mode.get()
        selected = self.selected_session.get()
        session_ids = [
            session_id
            for session_id, panel in self._panels.items()
            if cockpit_visible_in_room_context(panel, self._room_context_id)
        ]
        if mode == "focus":
            session_ids = [selected] if selected in session_ids else session_ids[:1]
        canvas_width = max(100, self.panel_canvas.winfo_width())
        columns = 1 if mode == "focus" else cockpit_grid_columns(canvas_width)
        layout_signature = (
            mode,
            canvas_width,
            columns,
            selected if mode == "focus" else "",
            tuple(session_ids),
        )
        if layout_signature == getattr(self, "_panel_layout_signature", ()):
            if mode == "timeline":
                self._render_timeline()
            return
        self._panel_layout_signature = layout_signature
        for panel in self._panels.values():
            panel["frame"].grid_forget()
        if mode == "timeline":
            self.panel_canvas.grid_remove()
            self.panel_scroll.grid_remove()
            self.timeline_frame.grid(row=0, column=0, columnspan=2, sticky="nsew")
            self._render_timeline()
            return
        self.timeline_frame.grid_remove()
        self.panel_canvas.grid()
        self.panel_scroll.grid()
        meta_wrap = max(260, (canvas_width // columns) - 44)
        for column in range(2):
            self.panel_host.grid_columnconfigure(column, weight=1 if column < columns else 0)
        for index, session_id in enumerate(session_ids):
            self._panels[session_id]["meta_label"].configure(wraplength=meta_wrap)
            self._panels[session_id]["activity_label"].configure(
                wraplength=meta_wrap
            )
            row, column = divmod(index, columns)
            self._panels[session_id]["frame"].grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=4,
                pady=4,
            )

    def _panel_host_configured(self, _event: Any = None) -> None:
        self.panel_canvas.configure(scrollregion=self.panel_canvas.bbox("all"))

    def _panel_canvas_configured(self, event: Any) -> None:
        width = max(100, int(getattr(event, "width", self.panel_canvas.winfo_width())))
        self.panel_canvas.itemconfigure(self._panel_window, width=width)
        self._layout_panels()

    def _set_view(self, view: str) -> None:
        if view not in COCKPIT_VIEWS:
            return
        self.view_mode.set(view)
        self._sync_view_buttons()
        self._layout_panels()

    def _sync_view_buttons(self) -> None:
        selected = self.view_mode.get()
        for view, button in self.view_buttons.items():
            active = view == selected
            button.configure(
                bg=self.colors["cyan"] if active else self.colors["line"],
                fg=self.colors["black"] if active else self.colors["text"],
            )

    def _session_option_base(self, snapshot: Mapping[str, Any]) -> str:
        values = (
            snapshot.get("room_name")
            or snapshot.get("source_conversation_name")
            or snapshot.get("source_conversation_id"),
            snapshot.get("display_name") or snapshot.get("agent_id"),
            self._localized_source(snapshot.get("source_type")),
        )
        parts: list[str] = []
        observed: set[str] = set()
        for value in values:
            text = str(value or self._t("cockpit.unavailable")).strip()
            folded = text.casefold()
            if folded in observed:
                continue
            observed.add(folded)
            parts.append(text)
        return " / ".join(parts)

    def _sync_session_options(
        self, snapshots: Iterable[Mapping[str, Any]]
    ) -> tuple[str, ...]:
        label_to_id: dict[str, str] = {}
        id_to_label: dict[str, str] = {}
        labels: list[str] = []
        counts: dict[str, int] = {}
        for snapshot in snapshots:
            session_id = str(snapshot["session_id"])
            base = self._session_option_base(snapshot)
            counts[base] = counts.get(base, 0) + 1
            label = base if counts[base] == 1 else f"{base} ({counts[base]})"
            label_to_id[label] = session_id
            id_to_label[session_id] = label
            labels.append(label)
        self._session_label_to_id = label_to_id
        self._session_id_to_label = id_to_label
        return tuple(labels)

    def _select_session(self, session_id: str) -> None:
        selected = str(session_id or "")
        self.selected_session.set(selected)
        self.selected_session_label.set(
            self._session_id_to_label.get(selected, selected)
        )

    def _session_combo_changed(self, _event: Any = None) -> None:
        session_id = self._session_label_to_id.get(
            self.selected_session_label.get(), ""
        )
        if not session_id:
            return
        self._select_session(session_id)
        self._selection_changed()

    def _selection_changed(self) -> None:
        selected = self.selected_session.get()
        if selected:
            self._room_context_pending = False
            self.selected_session_label.set(
                self._session_id_to_label.get(selected, selected)
            )
            self.view_mode.set("focus")
            self._sync_view_buttons()
        self._sync_session_actions()
        self._layout_panels()

    def set_room_context(self, room_id: str) -> bool:
        """Reset stale focus when the Conversation page changes rooms."""

        selected_room = str(room_id or "").strip()
        if not selected_room or selected_room == self._room_context_id:
            return False
        self._room_context_id = selected_room
        self._room_context_pending = True
        self._select_session("")
        self.view_mode.set("grid")
        self._sync_view_buttons()
        self._sync_session_actions()
        self._layout_panels()
        return True

    def _sync_session_actions(self) -> None:
        if self._action_inflight is not None:
            return
        snapshot = self._latest_snapshots.get(self.selected_session.get())
        self.send_button.configure(
            text=self._t(
                "cockpit.send_room"
                if snapshot and snapshot.get("source_type") == "peerbridge-room"
                else "cockpit.send_managed"
            )
        )
        self.send_button.configure(
            state="normal" if cockpit_action_allowed(snapshot, "send") else "disabled"
        )
        terminal_state = (
            "normal" if cockpit_action_allowed(snapshot, "stop") else "disabled"
        )
        self.interrupt_button.configure(state=terminal_state)
        self.stop_button.configure(state=terminal_state)
        self._sync_panel_inputs()

    def focus_source(self, source_type: str, source_session_id: str) -> bool:
        self.render()
        panel_id = cockpit_focus_panel_id(
            source_type, source_session_id, self._panels
        )
        if panel_id is None or not cockpit_visible_in_room_context(
            self._panels[panel_id], self._room_context_id
        ):
            self.status.set(self._t("cockpit.status.source_unavailable"))
            self.status_label.configure(fg=self.colors["amber"])
            return False
        self._select_session(panel_id)
        self.view_mode.set("focus")
        self._sync_view_buttons()
        self._selection_changed()
        self.status.set(self._t("cockpit.status.source_focused"))
        self.status_label.configure(fg=self.colors["green"])
        return True

    def render(self) -> bool:
        managed_positions = {
            str(panel["source_session_id"]): int(panel["latest_sequence"])
            for panel in self._panels.values()
            if panel.get("source_type") == "managed-cli"
        }
        snapshots: list[dict[str, Any]] = [
            managed_cli_session_contract(snapshot)
            for snapshot in self.manager.snapshots(after_sequences=managed_positions)
        ]
        external_positions = {
            str(session_id): int(panel["latest_sequence"])
            for session_id, panel in self._panels.items()
            if panel.get("source_type") != "managed-cli"
        }
        for supplied in self._external_sessions(external_positions):
            snapshot = normalize_session_contract(supplied)
            panel = self._panels.get(str(snapshot["session_id"]))
            after_sequence = int(panel["latest_sequence"]) if panel else 0
            first = int(snapshot.get("first_retained_sequence") or 0)
            if panel and panel["first_retained_sequence"] and first > int(
                panel["first_retained_sequence"]
            ):
                after_sequence = 0
            snapshot["events"] = [
                event
                for event in snapshot.get("events") or ()
                if int(event.get("sequence") or 0) > after_sequence
            ]
            snapshots.append(snapshot)
        for session_id in cockpit_stale_panel_ids(self._panels, snapshots):
            self._panels[session_id]["frame"].destroy()
            del self._panels[session_id]
        visible_snapshots = [
            snapshot
            for snapshot in snapshots
            if cockpit_visible_in_room_context(snapshot, self._room_context_id)
        ]
        values = tuple(str(snapshot["session_id"]) for snapshot in visible_snapshots)
        option_labels = self._sync_session_options(visible_snapshots)
        self.session_combo.configure(values=option_labels)
        context_panels = cockpit_room_context_panel_ids(
            snapshots, self._room_context_id
        )
        if self._room_context_pending and context_panels:
            self._select_session(context_panels[0])
            self.view_mode.set("grid")
            self._room_context_pending = False
            self._sync_view_buttons()
        elif self._room_context_pending:
            self._select_session("")
        elif self.selected_session.get() not in values:
            self._select_session(values[-1] if values else "")
        else:
            self._select_session(self.selected_session.get())
        active = False
        self._latest_snapshots = {
            str(snapshot["session_id"]): dict(snapshot) for snapshot in snapshots
        }
        for snapshot in snapshots:
            panel = self._ensure_panel(snapshot)
            self._update_panel(panel, snapshot)
            active = active or snapshot.get("state") in {"created", "running", "stopping"}
        self._sync_session_actions()
        self._layout_panels()
        return active or self._action_inflight is not None

    def apply_locale(self) -> None:
        self.notice_label.configure(text=self._t("cockpit.observable_notice"))
        self.launch_title.configure(text=self._t("cockpit.launch_title"))
        self.task_label.configure(text=self._t("cockpit.task"))
        self.view_section_title.configure(text=self._t("cockpit.view_existing"))
        for key, label in self.launch_labels.items():
            label.configure(text=self._t(f"cockpit.{key}"))
        self.browse_button.configure(text=self._t("cockpit.browse"))
        self.global_attach_button.configure(text=self._t("chat.attach"))
        self.global_clear_attachments_button.configure(
            text=self._t("chat.clear_attachments")
        )
        self.global_edit_button.configure(text=self._t("cockpit.edit"))
        self._set_prompt_attachment_paths(None, self._global_attachment_paths)
        self.start_button.configure(text=self._t("cockpit.start"))
        self.start_send_button.configure(text=self._t("cockpit.start_send"))
        self.session_label.configure(text=self._t("cockpit.session"))
        self.send_button.configure(text=self._t("cockpit.send_managed"))
        self.interrupt_button.configure(text=self._t("cockpit.interrupt"))
        self.stop_button.configure(text=self._t("cockpit.stop"))
        for view, button in self.view_buttons.items():
            button.configure(text=self._t(f"cockpit.view.{view}"))
        for column in ("time", "session", "kind", "summary"):
            self.timeline_tree.heading(
                column, text=self._t(f"cockpit.timeline.{column}")
            )
        for session_id, panel in self._panels.items():
            for key, tab in panel["tabs"].items():
                panel["notebook"].tab(tab, text=self._t(f"cockpit.tab.{key}"))
            snapshot = self._latest_snapshots.get(session_id)
            if snapshot is not None:
                self._sync_terminal_surface(panel, snapshot)
                self._set_prompt_attachment_paths(
                    session_id, panel.get("attachment_paths") or ()
                )
                self._sync_panel_input(panel, snapshot)
        localized_ready = self._t("cockpit.status.ready")
        self.status.set(
            cockpit_localized_status(
                self.status.get(),
                self._ready_status_text,
                localized_ready,
            )
        )
        self._ready_status_text = localized_ready
        self._sync_view_buttons()
        self._sync_session_actions()

    def self_test(self) -> dict[str, bool]:
        with contextlib.suppress(tk.TclError):
            self.root.update_idletasks()
        selected = self._latest_snapshots.get(self.selected_session.get())
        expected_send = "normal" if cockpit_action_allowed(selected, "send") else "disabled"
        expected_terminal = (
            "normal" if cockpit_action_allowed(selected, "stop") else "disabled"
        )
        return {
            "views": tuple(self.view_buttons) == COCKPIT_VIEWS,
            "observable_boundary": bool(self.notice_label.cget("text")),
            "managed_agents": tuple(self.agent_combo.cget("values"))
            == tuple(label for label, _agent_id in COCKPIT_AGENTS),
            "explicit_default_role": COCKPIT_DEFAULT_LAUNCH_ROLE
            == "equal-participant",
            "prompt_not_command_line": self.prompt.cget("state") == "normal",
            "launch_controls": all(
                button.cget("state") == "normal"
                for button in (
                    self.start_button,
                    self.start_send_button,
                )
            ),
            "source_bound_controls": (
                self.send_button.cget("state") == expected_send
                and self.interrupt_button.cget("state") == expected_terminal
                and self.stop_button.cget("state") == expected_terminal
            ),
            "toolbar_horizontal_bounds": cockpit_horizontal_bounds_fit(
                self.toolbar.winfo_width(),
                (
                    (group.winfo_x(), group.winfo_width())
                    for group in (
                        self.view_group,
                        self.selected_group,
                        self.action_group,
                    )
                ),
            ),
            "launch_horizontal_bounds": cockpit_rows_fit(
                self.launch.winfo_width(),
                (
                    (
                        (
                            self.launch_labels["agent"].winfo_x(),
                            self.launch_labels["agent"].winfo_width(),
                        ),
                        (self.agent_combo.winfo_x(), self.agent_combo.winfo_width()),
                    ),
                    (
                        (
                            self.launch_labels["directory"].winfo_x(),
                            self.launch_labels["directory"].winfo_width(),
                        ),
                    ),
                    (
                        (
                            self.directory_entry.winfo_x(),
                            self.directory_entry.winfo_width(),
                        ),
                        (self.browse_button.winfo_x(), self.browse_button.winfo_width()),
                    ),
                ),
            ),
            "ready_status_visible": (
                self.status_label.winfo_reqwidth() <= self.status_label.winfo_width()
            ),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self.manager.close()


__all__ = [
    "AgentCockpit",
    "COCKPIT_ACTIVITY_ACTIONS",
    "COCKPIT_AGENTS",
    "COCKPIT_DEFAULT_LAUNCH_ROLE",
    "COCKPIT_ROLES",
    "COCKPIT_VIEWS",
    "cockpit_activity_projection",
    "cockpit_current_activity",
    "cockpit_elapsed_seconds",
    "cockpit_event_visible_in_terminal",
    "cockpit_action_allowed",
    "cockpit_focus_panel_id",
    "cockpit_grid_columns",
    "cockpit_horizontal_bounds_fit",
    "cockpit_rows_fit",
    "cockpit_room_context_panel_ids",
    "cockpit_session_binding_changed",
    "cockpit_stale_panel_ids",
    "cockpit_terminal_summary",
    "cockpit_timeline_rows",
    "cockpit_usage_summary",
    "cockpit_visible_in_room_context",
    "cockpit_working_directory",
]
