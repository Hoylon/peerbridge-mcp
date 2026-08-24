from __future__ import annotations

import argparse
import atexit
import contextlib
import ctypes
import hashlib
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import __version__
from .agent_identity import ensure_agent_identity_capability
from .announcements import (
    Announcement,
    AnnouncementConfig,
    AnnouncementError,
    default_announcement_preferences,
    fail_closed_announcement_preferences,
    fetch_announcements,
    load_announcement_preferences,
    save_announcement_preferences,
)
from .agent_install import (
    AgentInstallError,
    AgentInstallStatus,
    detect_all_installable_agents,
    installable_agent_spec,
    installable_agent_specs,
    launch_agent_installer,
)
from .attachments import stage_chat_attachments
from .authorized_sessions import AuthorizedSessionError, AuthorizedSessionRegistry
from .bridge import (
    CONTROL_ROOM_WORKFLOW_ID,
    DEFAULT_ROOM_ID,
    DEFAULT_ROOM_ROLE,
    Bridge,
    stable_sha256,
)
from .ccswitch import (
    SUPPORTED_APPS as CC_SWITCH_APPS,
    CcSwitchError,
    CcSwitchProvider,
    fetch_models as ccswitch_fetch_models,
    find_app as ccswitch_find_app,
    find_cli as ccswitch_find_cli,
    list_providers as ccswitch_list_providers,
    open_app as ccswitch_open_app,
    switch_provider as ccswitch_switch_provider,
)
from .codex_catalog import (
    CodexCatalogError,
    CodexModelCatalog,
    discover_codex_model_catalog,
)
from .credentials import (
    CredentialStoreError,
    is_loopback_endpoint,
    store_local_provider_endpoint,
    store_provider_credentials,
)
from .desktop_cockpit import AgentCockpit
from .feedback import (
    FeedbackBundle,
    FeedbackConfig,
    create_feedback_bundle,
    deliver_feedback_bundle,
    feedback_mailto,
)
from .guided_room_workflows import (
    MAX_GUIDED_ROOM_AGENTS,
    GuidedRoomWorkflowError,
    guided_operation_id,
    guided_room_readiness,
    guided_room_workflow_plan,
    validate_guided_room_start,
)
from .localization import (
    LABEL_LOCALES,
    LOCALE_LABELS,
    SUPPORTED_THEMES,
    SUPPORTED_LOCALES,
    THEME_LABELS,
    LocalizationError,
    default_preferences,
    load_preferences,
    save_preferences,
    translate,
)
from .managed_workflows import ManagedWorkflowRunner
from .openai_compatible_runner import (
    ProviderModelRegistry,
    RunnerError,
    discover_provider_models,
)
from .release_gate import ReleaseGateService
from .room_discussion_tracker import RoomDiscussionTracker
from .secret_scan import contains_secret, redact_secrets
from .session_contract import linked_room_session_target, native_room_session_contract
from .trust_workflows_ui import TrustWorkflowsPage
from .updates import UpdateCheckResult, check_for_updates
from .verification_engine import VerificationTriggerEngine


def runtime_build_sha256(path: Path | None = None) -> str:
    """Return the full path-free digest for the running source or frozen binary."""

    target = path or Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    hasher = hashlib.sha256()
    try:
        with target.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
    except OSError:
        return "unavailable"
    return hasher.hexdigest()


def runtime_build_identity(path: Path | None = None) -> str:
    """Return a compact display digest that distinguishes local builds."""

    digest = runtime_build_sha256(path)
    return digest[:12] if digest != "unavailable" else digest


APP_VERSION = __version__
APP_BUILD_SHA256 = runtime_build_sha256()
APP_BUILD_ID = (
    APP_BUILD_SHA256[:12]
    if APP_BUILD_SHA256 != "unavailable"
    else APP_BUILD_SHA256
)
WINDOW_TITLE = "PeerBridge MCP Control Room"
WINDOW_TITLE_LIVE = f"{WINDOW_TITLE} // LIVE"
DEFAULT_INSTANCE_MUTEX = r"Local\PeerBridgeMcpControlRoomV1"
SAFE_INSTANCE_ID = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")
WINDOWS_APP_USER_MODEL_ID = "PeerBridge.MCP.ControlRoom"
_INSTANCE_HANDLE: int | None = None
BROADCAST_LABEL = "chat.recipient.all"
DIRECT_LABEL = "chat.route.direct"
BROADCAST_ROUTE_LABEL = "chat.route.broadcast"
NO_ROUTE_LABEL = "chat.route.unregistered"
PROVIDER_DEFAULT_MODEL_LABEL = "chat.model.default"
PROVIDER_DEFAULT_REASONING_LABEL = "chat.reasoning.default"
HUMAN_AGENT_ID = "human-operator"
ROOM_ROLE_IDS = (
    DEFAULT_ROOM_ROLE,
    "researcher",
    "implementer",
    "reviewer",
    "custom",
)
SAFE_ROUTE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
SAFE_CHAT_ARTIFACT_PATH = re.compile(
    r"\.peerbridge-artifacts/chat/([0-9a-f]{64})(\.[a-z0-9]{1,10})\Z"
)
ZERO_SHA256 = "0" * 64
DEFAULT_MONITOR_SNAPSHOT_LIMIT = 120
DEFAULT_ROOM_PAGE_SIZE = 60
CHAT_PAGE_MIN_CONTENT_HEIGHT = 860
CHAT_HISTORY_MIN_HEIGHT = 250
WINDOWS_UI_SCALE_FACTORS = (1.0, 1.25, 1.5)
TK_POINTS_PER_INCH = 72.0
WINDOWS_BASE_DPI = 96.0
SIDEBAR_WIDTH = 300
MODERN_SIDEBAR_WIDTH = 232
MODERN_INSPECTOR_BREAKPOINT = 1120
MODERN_INSPECTOR_WIDTH = 292
MODERN_CHAT_MAX_WIDTH = 1040
MODERN_CHAT_MIN_GUTTER = 28
MODERN_SIDEBAR_BG = "#f2f3f6"
MODERN_CARD_BG = "#ffffff"
MODERN_USER_BUBBLE_BG = "#edf2f8"
MODERN_NAV_ACTIVE_BG = "#e6ebf2"
MODERN_WORKSPACE_BG = "#f6f7f9"
SIDEBAR_SCROLLBAR_WIDTH = 16
SIDEBAR_SCROLLBAR_STYLE = "Sidebar.Vertical.TScrollbar"
ROOM_AGENT_CARD_WIDTH = 166
ROOM_AGENT_CARD_PAD_X = 4
ROOM_AGENT_CARD_OUTER_WIDTH = ROOM_AGENT_CARD_WIDTH + (ROOM_AGENT_CARD_PAD_X * 2)
ROOM_AGENT_STRIP_HEADER_WIDTH = 95
ROOM_AGENT_OVERFLOW_WIDTH = 58
ROOM_AGENT_DEFAULT_VISIBLE_LIMIT = 5
ROOM_AGENT_DETAIL_TEXT_SIZE = 9
SIDEBAR_TEXT_SIZE = 9
AGENT_LIBRARY_CANVAS_WIDTH = 222
AGENT_LIBRARY_CANVAS_HEIGHT = 176
AGENT_LIBRARY_COLUMNS = 2
AGENT_LIBRARY_VISIBLE_ROWS = 3
AGENT_LIBRARY_VISIBLE_CAPACITY = AGENT_LIBRARY_COLUMNS * AGENT_LIBRARY_VISIBLE_ROWS
AGENT_LIBRARY_CARD_WIDTH = 104
AGENT_LIBRARY_CARD_HEIGHT = 50
AGENT_LIBRARY_COLUMN_STRIDE = 108
AGENT_LIBRARY_CARD_STRIDE = 57
AGENT_LIBRARY_TOP_MARGIN = 5
TUTORIAL_BODY_TEXT_SIZE = 11
USAGE_TABLE_COLUMNS = (
    "provider",
    "model",
    "calls",
    "reported",
    "input",
    "output",
    "total",
)
USAGE_PERIOD_KEYS = ("today", "7d", "30d", "all")
TUTORIAL_PAGE_KEYS = (
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
MODERN_NAV_GROUPS = (
    ("workspace", ("cockpit", "chat", "work")),
    ("governance", ("review", "change", "audit", "trust")),
    ("system", ("connect", "memory", "feedback", "usage", "announcement")),
)
MODERN_INSPECTOR_KEYS = ("agents", "workflow", "evidence")
MODERN_NAV_ICONS = {
    "cockpit": "⌂",
    "chat": "▣",
    "work": "✓",
    "review": "◇",
    "change": "±",
    "audit": "◎",
    "connect": "↔",
    "memory": "◫",
    "trust": "◆",
    "feedback": "✎",
    "usage": "▥",
    "announcement": "○",
}


def modern_navigation_pages() -> tuple[str, ...]:
    """Return the grouped Modern navigation contract in display order."""

    return tuple(
        page_key
        for _group_key, page_keys in MODERN_NAV_GROUPS
        for page_key in page_keys
    )


def modern_navigation_is_complete() -> bool:
    """Keep the alternative shell feature-complete with the Pixel shell."""

    pages = modern_navigation_pages()
    return len(pages) == len(set(pages)) and set(pages) == set(TUTORIAL_PAGE_KEYS)


def modern_chat_content_geometry(viewport_width: int) -> tuple[int, int]:
    """Return a centered, readable Modern chat width and its left offset."""

    width = max(1, int(viewport_width))
    available = max(1, width - (MODERN_CHAT_MIN_GUTTER * 2))
    content_width = min(MODERN_CHAT_MAX_WIDTH, available)
    return content_width, max(0, (width - content_width) // 2)
TUTORIAL_DIAGRAM_SPECS = {
    "cockpit": ("cockpit", ((0.16, 0.18), (0.33, 0.58), (0.78, 0.58))),
    "chat": ("chat", ((0.18, 0.16), (0.15, 0.53), (0.62, 0.82))),
    "work": ("table", ((0.18, 0.16), (0.34, 0.48), (0.72, 0.83))),
    "review": ("table", ((0.18, 0.16), (0.47, 0.48), (0.72, 0.83))),
    "change": ("table", ((0.18, 0.16), (0.59, 0.48), (0.72, 0.83))),
    "audit": ("table", ((0.18, 0.16), (0.74, 0.48), (0.72, 0.83))),
    "connect": ("connect", ((0.24, 0.20), (0.27, 0.53), (0.70, 0.82))),
    "memory": ("table", ((0.18, 0.16), (0.41, 0.48), (0.72, 0.83))),
    "trust": ("trust", ((0.18, 0.15), (0.28, 0.54), (0.76, 0.75))),
    "feedback": ("feedback", ((0.24, 0.20), (0.32, 0.55), (0.76, 0.82))),
    "usage": ("usage", ((0.22, 0.15), (0.24, 0.39), (0.70, 0.73))),
    "announcement": ("announcement", ((0.24, 0.18), (0.26, 0.53), (0.76, 0.62))),
}
MESSAGE_PRIORITIES = ("low", "normal", "high", "critical")
AUTOMATION_MODE_TO_KEY = {
    "off": "chat.mode.off",
    "once": "chat.mode.once",
    "discussion": "chat.mode.discussion",
}
DISCUSSION_STATUS_TO_KEY = {
    "active": "chat.discussion.status.active",
    "paused": "chat.discussion.status.paused",
    "completed": "chat.discussion.status.completed",
    "stopped": "chat.discussion.status.stopped",
    "failed": "chat.discussion.status.failed",
    "blocked": "chat.discussion.status.blocked",
}
MCP_NATIVE = "MCP_NATIVE"
MCP_TOOL_LOOP = "MCP_TOOL_LOOP"
INFERENCE_ONLY = "INFERENCE_ONLY"
MCP_UNVERIFIED = "MCP_UNVERIFIED"
MCP_NATIVE_TRANSPORTS = frozenset({"stdio", "streamable-http", "sse"})
MCP_TOOL_LOOP_CLIENTS = frozenset(
    {"openai-compatible", "openai-compatible-runner"}
)
MCP_NATIVE_CLIENTS = frozenset(
    {
        "codex",
        "codex-cli",
        "codex-desktop",
        "claude-code",
        "claude-code-native",
        "kimi",
        "kimi-cli",
        "kimi-code",
        "grok-build-acpx",
        "claude-agent-acp",
    }
)

COLOR_PALETTES = {
    "pixel": {
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
    },
    "modern": {
        "bg": "#f6f7f9",
        "panel": "#ffffff",
        "panel_2": "#eef1f5",
        "line": "#d8dde6",
        "text": "#151922",
        "muted": "#667085",
        "cyan": "#245fdb",
        "amber": "#8a5b17",
        "green": "#24805c",
        "red": "#b64646",
        "purple": "#7257a8",
        "blue": "#315fc4",
        "black": "#ffffff",
    },
}
COLORS = dict(COLOR_PALETTES["pixel"])
ACTIVE_THEME = "pixel"
PIXEL_FONT_FAMILY = "Cascadia Mono"
MODERN_FONT_FAMILY = "Segoe UI Variable Text"
UI_FONT_FAMILY = PIXEL_FONT_FAMILY


def apply_color_palette(theme: str) -> dict[str, str]:
    """Apply one bounded built-in palette before any desktop widgets exist."""

    global ACTIVE_THEME, UI_FONT_FAMILY
    if theme not in SUPPORTED_THEMES:
        raise ValueError("unsupported UI theme")
    ACTIVE_THEME = theme
    UI_FONT_FAMILY = (
        MODERN_FONT_FAMILY if theme == "modern" else PIXEL_FONT_FAMILY
    )
    COLORS.clear()
    COLORS.update(COLOR_PALETTES[theme])
    return dict(COLORS)


def windows_instance_mutex_name(instance_id: str | None = None) -> str:
    """Return the default mutex or a bounded verifier-only instance mutex."""
    selected = (
        os.environ.get("PEERBRIDGE_INSTANCE_ID", "")
        if instance_id is None
        else instance_id
    ).strip()
    if not selected:
        return DEFAULT_INSTANCE_MUTEX
    if SAFE_INSTANCE_ID.fullmatch(selected) is None:
        raise ValueError("PEERBRIDGE_INSTANCE_ID has an invalid format")
    return f"{DEFAULT_INSTANCE_MUTEX}-{selected}"


INSTANCE_MUTEX = windows_instance_mutex_name()


def configure_windows_app_identity() -> bool:
    """Give Windows a stable identity so the taskbar uses PeerBridge branding."""
    if sys.platform != "win32":
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        setter = shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = (ctypes.c_wchar_p,)
        setter.restype = ctypes.c_long
        return setter(WINDOWS_APP_USER_MODEL_ID) == 0
    except (AttributeError, OSError):
        return False


def packaged_icon_paths() -> tuple[Path, Path]:
    support = Path(__file__).resolve().parent / "release_support"
    return support / "peerbridge-icon.png", support / "peerbridge-icon.ico"


def apply_windows_window_icon(root: tk.Misc, ico_path: Path) -> tuple[int, ...]:
    """Bind owned icon handles to Tk's child and top-level Windows handles."""
    if sys.platform != "win32" or not ico_path.is_file():
        return ()
    try:
        root.update_idletasks()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        )
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_void_p,
        )
        user32.SendMessageW.restype = ctypes.c_ssize_t

        child_hwnd = int(root.winfo_id())
        top_hwnd = int(user32.GetAncestor(ctypes.c_void_p(child_hwnd), 2) or 0)
        windows = tuple(dict.fromkeys(hwnd for hwnd in (child_hwnd, top_hwnd) if hwnd))
        handles: list[int] = []
        for size, icon_kind in ((32, 1), (16, 0)):
            handle = int(
                user32.LoadImageW(
                    None,
                    str(ico_path),
                    1,  # IMAGE_ICON
                    size,
                    size,
                    0x0010,  # LR_LOADFROMFILE
                )
                or 0
            )
            if not handle:
                continue
            for hwnd in windows:
                user32.SendMessageW(
                    ctypes.c_void_p(hwnd),
                    0x0080,  # WM_SETICON
                    icon_kind,
                    ctypes.c_void_p(handle),
                )
            handles.append(handle)
        return tuple(handles)
    except (AttributeError, OSError, tk.TclError, ValueError):
        return ()


def release_windows_icon_handles(handles: Iterable[int]) -> None:
    """Release LoadImageW handles after their window has been destroyed."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.DestroyIcon.argtypes = (ctypes.c_void_p,)
        user32.DestroyIcon.restype = ctypes.c_bool
        for handle in handles:
            if handle:
                user32.DestroyIcon(ctypes.c_void_p(int(handle)))
    except (AttributeError, OSError, ValueError):
        return


def acquire_single_instance() -> bool:
    """Keep repeated shortcut clicks from creating duplicate monitor processes."""
    global _INSTANCE_HANDLE
    if sys.platform != "win32":
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")

    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
        user32.FindWindowW.restype = ctypes.c_void_p
        for title in (WINDOW_TITLE_LIVE, WINDOW_TITLE):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                break
        kernel32.CloseHandle(handle)
        return False

    _INSTANCE_HANDLE = int(handle)
    atexit.register(kernel32.CloseHandle, handle)
    return True


def utc_text(value: str | None) -> str:
    if not value:
        return "--"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        return value


def clip(value: Any, length: int = 140) -> str:
    text = "" if value is None else str(value).replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    return text if len(text) <= length else text[: length - 1] + "..."


def compact_sidebar_stats(
    *,
    online: int,
    total_agents: int,
    rooms: int,
    messages: int,
    dispatch: str,
    memories: int,
    open_calls: int,
    active_tasks: int,
    audit_events: int,
    sync: str,
    labels: Mapping[str, str] | None = None,
) -> str:
    """Keep every status counter visible in a height-constrained sidebar."""
    names = {
        "online": "ONLINE",
        "rooms": "ROOMS",
        "messages": "MESSAGES",
        "memory": "MEMORY",
        "dispatch": "DISPATCH",
        "open_calls": "OPEN CALL",
        "active": "ACTIVE",
        "audit": "AUDIT",
        "sync": "SYNC",
        **dict(labels or {}),
    }
    return (
        f"{names['online']} {online}/{total_agents}  {names['rooms']} {rooms}\n"
        f"{names['messages']} {messages}  {names['memory']} {memories}\n"
        f"{names['dispatch']} {dispatch}\n"
        f"{names['open_calls']} {open_calls}  {names['active']} {active_tasks}\n"
        f"{names['audit']} {audit_events}\n"
        f"{names['sync']} {sync}"
    )


def safe_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def redact_sensitive(value: Any) -> str:
    """Keep recognizable credentials out of labels, errors, and detail panes."""
    return redact_secrets(value)


def ui_content_signature(value: Any) -> str:
    """Return a stable digest for UI projections without retaining their payload."""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def safe_chat_artifact_labels(paths: Iterable[Any]) -> tuple[str, ...]:
    """Return history labels without exposing untrusted or original path names."""
    labels: list[str] = []
    for index, raw_path in enumerate(paths, start=1):
        normalized = str(raw_path or "").replace("\\", "/")
        match = SAFE_CHAT_ARTIFACT_PATH.fullmatch(normalized)
        if not match:
            labels.append(f"UNVERIFIED ATTACHMENT {index}")
            continue
        digest, suffix = match.groups()
        labels.append(f"{digest[:16]}{suffix} // SHA {digest[:16]}")
    return tuple(labels)


def room_agent_card_groups(
    cards: Iterable[dict[str, Any]], *, visible_limit: int = 5
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Keep the compact strip bounded while retaining controls for every seat."""
    rows = tuple(cards)
    limit = max(0, int(visible_limit))
    return rows[:limit], rows[limit:]


def room_agent_visible_limit(strip_width: int, card_count: int) -> int:
    """Use the actual strip width before collapsing room Agents into +N."""
    count = max(0, int(card_count))
    if count == 0:
        return 0
    width = max(0, int(strip_width))
    if width <= 1:
        return min(count, ROOM_AGENT_DEFAULT_VISIBLE_LIMIT)
    available = max(0, width - ROOM_AGENT_STRIP_HEADER_WIDTH)
    all_capacity = available // ROOM_AGENT_CARD_OUTER_WIDTH
    if count <= all_capacity:
        return count
    overflow_capacity = max(
        1,
        (max(0, available - ROOM_AGENT_OVERFLOW_WIDTH))
        // ROOM_AGENT_CARD_OUTER_WIDTH,
    )
    return min(count, overflow_capacity)


def incremental_render_mode(
    previous: tuple[str, ...] | None,
    current: tuple[str, ...],
    *,
    same_context: bool,
) -> str:
    """Choose the smallest safe UI update for an ordered row projection."""
    if previous is not None and same_context and previous == current:
        return "unchanged"
    if (
        previous is not None
        and bool(previous)
        and same_context
        and len(current) > len(previous)
        and current[: len(previous)] == previous
    ):
        return "append"
    return "rebuild"


def chat_split_sash_position(
    total_height: int,
    composer_requested_height: int,
    *,
    min_history_height: int = 90,
    min_composer_height: int = 215,
) -> int:
    """Keep every composer control visible while preserving useful chat history."""
    available = max(0, int(total_height))
    if available <= min_history_height:
        return 0
    desired_composer_height = max(
        min_composer_height,
        int(composer_requested_height) + 8,
    )
    composer_height = min(
        desired_composer_height,
        max(0, available - min_history_height),
    )
    return max(0, available - composer_height)


def vertical_scroll_fraction_to_reveal(
    *,
    widget_top: int,
    widget_bottom: int,
    viewport_top: float,
    viewport_height: int,
    content_height: int,
) -> float:
    """Return the canvas yview fraction that fully reveals one child widget."""
    content = max(1, int(content_height))
    viewport = max(1, int(viewport_height))
    maximum_offset = max(0.0, float(content - viewport))
    current = min(max(0.0, float(viewport_top)), maximum_offset)
    if widget_top < current:
        target = float(widget_top)
    elif widget_bottom > current + viewport:
        target = float(widget_bottom - viewport)
    else:
        target = current
    target = min(max(0.0, target), maximum_offset)
    return target / content


def tutorial_diagram_spec(
    page_key: str,
) -> tuple[str, tuple[tuple[float, float], ...]]:
    """Return a privacy-safe schematic and its three numbered callouts."""

    try:
        return TUTORIAL_DIAGRAM_SPECS[page_key]
    except KeyError as exc:
        raise ValueError(f"unsupported tutorial page: {page_key}") from exc


def tk_scaling_for_windows_factor(scale_factor: float) -> float:
    """Translate a Windows display scale factor into Tk pixels per point."""
    factor = float(scale_factor)
    if factor not in WINDOWS_UI_SCALE_FACTORS:
        raise ValueError(f"unsupported Windows UI scale factor: {factor}")
    return (WINDOWS_BASE_DPI * factor) / TK_POINTS_PER_INCH


def centered_transient_geometry(
    *,
    parent_x: int,
    parent_y: int,
    parent_width: int,
    parent_height: int,
    width: int,
    height: int,
) -> str:
    """Place a transient inside its parent, including on negative-coordinate monitors."""
    window_width = max(1, int(width))
    window_height = max(1, int(height))
    x = int(parent_x) + max(0, (int(parent_width) - window_width) // 2)
    y = int(parent_y) + max(0, (int(parent_height) - window_height) // 2)
    return f"{window_width}x{window_height}{x:+d}{y:+d}"


def chat_bubble_metrics(canvas_width: int) -> tuple[int, int]:
    """Return responsive body wrap length and opposite-side chat padding."""
    width = max(480, int(canvas_width))
    wraplength = max(420, min(1100, int(width * 0.68)))
    opposite_padding = max(24, min(220, int(width * 0.12)))
    return wraplength, opposite_padding


def build_global_agent_library(
    presence: Iterable[dict[str, Any]],
    route_profiles: Iterable[dict[str, Any]],
    scope: str,
    *,
    now_epoch: float | None = None,
) -> tuple[dict[str, Any], ...]:
    """Project reusable global agents without consulting room membership state."""
    now = time.time() if now_epoch is None else float(now_epoch)
    latest_presence: dict[str, dict[str, Any]] = {}
    profiles_by_agent: dict[str, list[dict[str, Any]]] = {}

    for row in presence:
        agent_id = str(row.get("agent_id") or "")
        if row.get("scope") != scope or not agent_id or agent_id == HUMAN_AGENT_ID:
            continue
        current = latest_presence.get(agent_id)
        if current is None or float(row.get("last_seen_epoch") or 0) > float(
            current.get("last_seen_epoch") or 0
        ):
            latest_presence[agent_id] = row

    for row in route_profiles:
        agent_id = str(row.get("agent_id") or "")
        if (
            row.get("scope") != scope
            or not row.get("enabled")
            or not agent_id
            or agent_id == HUMAN_AGENT_ID
        ):
            continue
        profiles_by_agent.setdefault(agent_id, []).append(row)

    agents: list[dict[str, Any]] = []
    for agent_id in sorted(set(latest_presence) | set(profiles_by_agent)):
        live = latest_presence.get(agent_id, {})
        profiles = sorted(
            profiles_by_agent.get(agent_id, []), key=lambda item: str(item.get("route_id") or "")
        )
        fallback = profiles[0] if profiles else {}
        last_seen = float(live.get("last_seen_epoch") or 0)
        agents.append(
            {
                "agent_id": agent_id,
                "last_seen_epoch": last_seen,
                "online": bool(last_seen and now - last_seen <= 120),
                "provider_id": live.get("provider_id") or fallback.get("provider_id"),
                "model_id": live.get("model_id") or fallback.get("model_id"),
                "mcp_access_mode": agent_mcp_access_mode(
                    (live,) if live else (), profiles
                ),
                "route_ids": tuple(
                    str(profile["route_id"])
                    for profile in profiles
                    if profile.get("route_id")
                ),
            }
        )
    return tuple(agents)


def agent_mcp_access_mode(
    sessions: Iterable[dict[str, Any]],
    profiles: Iterable[dict[str, Any]],
) -> str:
    """Describe tool access without upgrading an inference fallback to MCP-native."""

    live_sessions = tuple(dict(row) for row in sessions)
    saved_profiles = tuple(dict(row) for row in profiles)

    # The API runner owns a bounded MCP tool loop. Its internal stdio server is
    # transport plumbing, not proof that the upstream model is an MCP client.
    if any(
        str(row.get("client_name") or "").strip().lower()
        in MCP_TOOL_LOOP_CLIENTS
        for row in live_sessions
    ):
        return MCP_TOOL_LOOP

    # A live, non-runner MCP transport is stronger evidence than saved fallback
    # profiles for the same durable Agent identity.
    if any(
        str(row.get("transport") or "").strip().lower()
        in MCP_NATIVE_TRANSPORTS
        and str(row.get("client_name") or "").strip().lower()
        not in MCP_TOOL_LOOP_CLIENTS
        for row in live_sessions
    ):
        return MCP_NATIVE

    if any(
        str(row.get("client_name") or "").strip().lower()
        in MCP_TOOL_LOOP_CLIENTS
        for row in saved_profiles
    ):
        return MCP_TOOL_LOOP

    # CC Switch routes are bounded inference fallbacks. A client_name such as
    # ``codex`` or ``claude`` must never upgrade them to native MCP capability.
    if any(
        str(row.get("provider_id") or "").strip().lower().startswith("ccswitch-")
        or str(row.get("secret_backend") or "").strip().lower() == "cc-switch"
        or str(row.get("transport") or "").strip().lower() == "mailbox-supervisor"
        for row in saved_profiles
    ):
        return INFERENCE_ONLY

    if any(
        str(row.get("client_name") or "").strip().lower() in MCP_NATIVE_CLIENTS
        for row in saved_profiles
    ):
        return MCP_NATIVE

    return MCP_UNVERIFIED


def mcp_access_label(mode: str) -> str:
    return {
        MCP_NATIVE: "MCP NATIVE",
        MCP_TOOL_LOOP: "MCP TOOL",
        INFERENCE_ONLY: "INFERENCE",
        MCP_UNVERIFIED: "MCP ?",
    }.get(mode, "MCP ?")


def merge_global_agent_catalog(
    projected: Iterable[dict[str, Any]],
    catalog: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Keep durable identities visible even when every runtime session is offline."""
    merged = {
        str(row["agent_id"]): dict(row)
        for row in projected
        if row.get("agent_id") and row.get("agent_id") != HUMAN_AGENT_ID
    }
    for row in catalog:
        agent_id = str(row.get("agent_id") or "")
        if not agent_id or agent_id == HUMAN_AGENT_ID:
            continue
        sessions = tuple(row.get("online_sessions") or ())
        profiles = tuple(row.get("route_profiles") or ())
        latest = sessions[0] if sessions else {}
        fallback = profiles[0] if profiles else {}
        durable = {
            "agent_id": agent_id,
            "last_seen_epoch": float(latest.get("last_seen_epoch") or 0),
            "online": bool(row.get("online") or sessions),
            "provider_id": latest.get("provider_id") or fallback.get("provider_id"),
            "model_id": latest.get("model_id") or fallback.get("model_id"),
            "route_ids": tuple(
                str(profile["route_id"])
                for profile in profiles
                if profile.get("route_id")
            ),
            "active_room_ids": tuple(row.get("active_room_ids") or ()),
            "catalog_sha256": row.get("catalog_sha256"),
            "online_sessions": sessions,
            "route_profiles": profiles,
            "mcp_access_mode": agent_mcp_access_mode(sessions, profiles),
        }
        if agent_id in merged:
            current = merged[agent_id]
            durable = {
                **durable,
                **{key: value for key, value in current.items() if value not in (None, (), "")},
                "active_room_ids": durable["active_room_ids"],
                "catalog_sha256": durable["catalog_sha256"],
            }
        merged[agent_id] = durable
    return tuple(merged[key] for key in sorted(merged))


def active_room_recipient_ids(
    room_id: str,
    members: Iterable[dict[str, Any]],
    global_agent_ids: Iterable[str],
) -> tuple[str, ...]:
    """Return global lobby recipients or active seat references for a custom room."""
    if room_id == DEFAULT_ROOM_ID:
        excluded = {
            str(row["agent_id"])
            for row in members
            if row.get("status") == "left" and row.get("agent_id")
        }
        return tuple(
            sorted(
                {
                    str(agent_id)
                    for agent_id in global_agent_ids
                    if agent_id and str(agent_id) not in excluded
                }
            )
        )
    return tuple(
        sorted(
            {
                str(row["agent_id"])
                for row in members
                if row.get("status") == "active"
                and row.get("agent_id")
                and row.get("agent_id") != HUMAN_AGENT_ID
            }
        )
    )


def room_agent_cards(
    room_id: str,
    members: Iterable[dict[str, Any]],
    global_agents: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Project the exact agents participating in one room for the visual seat strip."""
    catalog = {
        str(row["agent_id"]): dict(row)
        for row in global_agents
        if row.get("agent_id") and row.get("agent_id") != HUMAN_AGENT_ID
    }
    cards: list[dict[str, Any]] = []
    if room_id == DEFAULT_ROOM_ID:
        memberships = {
            str(row["agent_id"]): dict(row)
            for row in members
            if row.get("agent_id")
        }
        excluded = {
            str(row["agent_id"])
            for row in members
            if row.get("status") == "left" and row.get("agent_id")
        }
        cards.append(
            {
                "agent_id": HUMAN_AGENT_ID,
                "provider_id": "control-room",
                "model_id": None,
                "reasoning_mode": None,
                "online": True,
                "state": "CONTROL",
            }
        )
        cards.extend(
            {
                **row,
                **(
                    memberships[agent_id]
                    if memberships.get(agent_id, {}).get("status") == "active"
                    else {}
                ),
                "agent_id": agent_id,
                "provider_id": (
                    memberships.get(agent_id, {}).get("provider_id")
                    if memberships.get(agent_id, {}).get("route_profile_id")
                    else None
                ),
                "model_id": (
                    memberships.get(agent_id, {}).get("model_id")
                    if memberships.get(agent_id, {}).get("route_profile_id")
                    else None
                ),
                "reasoning_mode": (
                    memberships.get(agent_id, {}).get("reasoning_mode")
                    if memberships.get(agent_id, {}).get("route_profile_id")
                    else None
                ),
                "mcp_access_mode": (
                    agent_mcp_access_mode((), (memberships[agent_id],))
                    if memberships.get(agent_id, {}).get("route_profile_id")
                    else MCP_UNVERIFIED
                ),
                "state": (
                    "ONLINE"
                    if memberships.get(agent_id, {}).get("route_profile_id")
                    and row.get("online")
                    else (
                        "OFFLINE"
                        if memberships.get(agent_id, {}).get("route_profile_id")
                        else "UNROUTED"
                    )
                ),
            }
            for agent_id, row in catalog.items()
            if agent_id not in excluded
        )
    else:
        for member in members:
            if member.get("status") != "active" or not member.get("agent_id"):
                continue
            agent_id = str(member["agent_id"])
            fallback = catalog.get(agent_id, {})
            is_human = agent_id == HUMAN_AGENT_ID
            has_route = bool(member.get("route_profile_id"))
            cards.append(
                {
                    **fallback,
                    **dict(member),
                    "agent_id": agent_id,
                    # A custom-room card reports its bound seat route. Falling
                    # back to a global profile would make an unrouted seat look
                    # ready for fanout when it is not.
                    "provider_id": member.get("provider_id"),
                    "model_id": member.get("model_id"),
                    "reasoning_mode": member.get("reasoning_mode"),
                    "mcp_access_mode": (
                        agent_mcp_access_mode((), (member,))
                        if has_route
                        else MCP_UNVERIFIED
                    ),
                    "online": bool(is_human or member.get("online") or fallback.get("online")),
                    "state": "CONTROL" if is_human else (
                        "UNROUTED" if not has_route else (
                            "ONLINE"
                            if member.get("online") or fallback.get("online")
                            else "OFFLINE"
                        )
                    ),
                }
            )
    return tuple(sorted(cards, key=lambda row: (row.get("agent_id") != HUMAN_AGENT_ID, str(row.get("agent_id")))))


def provider_display_label(
    profile: dict[str, Any],
    provider_connections: Iterable[dict[str, Any]],
) -> str:
    """Render a provider source with an unambiguous provider identity."""
    provider_id = str(profile.get("provider_id") or "unknown")
    connection = next(
        (
            row
            for row in provider_connections
            if row.get("enabled")
            and provider_id
            in {
                str(row.get("connection_id") or ""),
                str(row.get("provider_id") or ""),
            }
        ),
        None,
    )
    display_name = str((connection or {}).get("display_name") or provider_id)
    route_class = str(profile.get("route_class") or "local").upper()
    return f"{route_class} | {display_name} [{provider_id}]"


def ccswitch_route_specs(
    provider: CcSwitchProvider,
    *,
    agent_id: str,
    models: Iterable[str],
    reasoning_mode: str | None = None,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Build redacted, deterministic metadata for a saved CC Switch source."""
    identity = f"{provider.app}:{provider.provider_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    connection_id = f"ccswitch-{provider.app}-{digest[:12]}"
    # Public CLI discovery proves an intermediary selection, not upstream ownership.
    route_class = "relay"
    source_suffix = provider.provider_id.rsplit("-", 1)[-1][-12:]
    connection = {
        "connection_id": connection_id,
        "display_name": f"CC Switch / {provider.name} [{source_suffix}]",
        "route_class": route_class,
        "provider_id": connection_id,
        "secret_backend": "cc-switch",
        "credential_target": f"CCSwitch:{digest[:32]}",
        "endpoint_sha256": ZERO_SHA256,
        "credential_fingerprint_sha256": digest,
        "descriptor_schema": "peerbridge.ccswitch-reference.v1",
        "credential_version_sha256": digest,
        "enabled": True,
    }
    routes: list[dict[str, Any]] = []
    for model in dict.fromkeys(str(value).strip() for value in models if str(value).strip()):
        route_id = f"{connection_id}-{hashlib.sha256(model.encode('utf-8')).hexdigest()[:10]}"
        route: dict[str, Any] = {
            "route_id": route_id,
            "agent_id": agent_id,
            "client_name": provider.app,
            "provider_id": connection_id,
            "model_id": model,
            "route_class": route_class,
            "enabled": True,
        }
        if reasoning_mode:
            route["reasoning_mode"] = reasoning_mode
        routes.append(route)
    return connection, tuple(routes)


def room_display_label(room: dict[str, Any]) -> str:
    room_id = str(room.get("room_id") or DEFAULT_ROOM_ID)
    name = clip(room.get("name") or room_id, 34)
    messages = int(room.get("message_count") or 0)
    return f"{name} [{room_id}]  MSG {messages}"


def room_seat_route(member: dict[str, Any]) -> str:
    route_id = str(member.get("route_profile_id") or "DIRECT")
    runtime = "/".join(
        str(value)
        for value in (
            member.get("provider_id"),
            member.get("model_id"),
            member.get("reasoning_mode"),
        )
        if value
    )
    return f"{route_id} // {runtime}" if runtime else route_id


def exact_route_profile(
    profiles: Iterable[dict[str, Any]],
    *,
    provider_id: str,
    model_id: str | None,
    reasoning_mode: str | None,
) -> dict[str, Any] | None:
    """Resolve one exact room-seat route without silently changing model settings."""
    matches = [
        row
        for row in profiles
        if str(row.get("provider_id") or "") == provider_id
        and (str(row.get("model_id")) if row.get("model_id") else None) == model_id
        and (
            str(row.get("reasoning_mode")) if row.get("reasoning_mode") else None
        ) == reasoning_mode
    ]
    return matches[0] if len(matches) == 1 else None


def agent_route_options(
    agent_id: str,
    profiles: Iterable[dict[str, Any]],
    provider_connections: Iterable[dict[str, Any]],
    advertised_models: Mapping[str, Iterable[str]],
    registry_sha256: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Merge runnable routes with live provider-advertised model choices.

    Advertised-only rows are intentionally not runnable route profiles.  The UI
    must re-query the provider and register the selected model before assigning
    it to a room seat.
    """

    registered = [
        dict(row)
        for row in profiles
        if row.get("enabled") and str(row.get("agent_id") or "") == agent_id
    ]
    connections = {
        str(row.get("connection_id") or ""): row
        for row in provider_connections
        if row.get("enabled") and row.get("connection_id")
    }
    known_models = {
        (str(row.get("provider_id") or ""), str(row.get("model_id") or ""))
        for row in registered
        if row.get("provider_id") and row.get("model_id")
    }
    templates: dict[str, dict[str, Any]] = {}
    for row in registered:
        provider_id = str(row.get("provider_id") or "")
        if provider_id and provider_id in connections:
            templates.setdefault(provider_id, row)

    options = list(registered)
    registry_sha256 = registry_sha256 or {}
    for provider_id, template in templates.items():
        connection = connections[provider_id]
        for model_id in sorted(
            {
                str(value).strip()
                for value in advertised_models.get(provider_id, ())
                if str(value).strip()
            }
        ):
            if (provider_id, model_id) in known_models:
                continue
            options.append(
                {
                    "route_id": None,
                    "agent_id": agent_id,
                    "client_name": template.get("client_name") or "openai-compatible",
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "response_model_id": None,
                    "reasoning_mode": None,
                    "route_class": connection.get("route_class") or template.get("route_class"),
                    "enabled": True,
                    "_advertised_only": True,
                    "_registry_sha256": registry_sha256.get(provider_id),
                }
            )
    return tuple(
        sorted(
            options,
            key=lambda row: (
                str(row.get("provider_id") or ""),
                str(row.get("model_id") or ""),
                str(row.get("reasoning_mode") or ""),
                bool(row.get("_advertised_only")),
                str(row.get("route_id") or ""),
            ),
        )
    )


def codex_catalog_route_options(
    agent_id: str,
    catalog: CodexModelCatalog | None,
) -> tuple[dict[str, Any], ...]:
    """Expose visible local Codex models as verified-but-unregistered choices."""

    if agent_id != "codex-main" or catalog is None:
        return ()
    rows: list[dict[str, Any]] = []
    for model in catalog.models:
        reasoning_modes = model.supported_reasoning_modes or (
            (model.default_reasoning_mode,) if model.default_reasoning_mode else (None,)
        )
        for reasoning_mode in reasoning_modes:
            rows.append(
                {
                    "route_id": None,
                    "agent_id": agent_id,
                    "client_name": "codex",
                    "provider_id": "openai-official",
                    "model_id": model.model_id,
                    "response_model_id": None,
                    "reasoning_mode": reasoning_mode,
                    "route_class": "official",
                    "enabled": True,
                    "_advertised_only": True,
                    "_catalog_source": "codex-cli",
                    "_registry_sha256": catalog.catalog_sha256,
                }
            )
    return tuple(rows)


def merge_agent_route_options(
    *groups: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Merge route choices, preferring durable registered rows over discoveries."""

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for group in groups:
        for raw in group:
            row = dict(raw)
            key = (
                str(row.get("provider_id") or ""),
                str(row.get("model_id") or ""),
                str(row.get("reasoning_mode") or ""),
            )
            current = merged.get(key)
            if current is None or (
                current.get("_advertised_only") and not row.get("_advertised_only")
            ):
                merged[key] = row
    return tuple(
        sorted(
            merged.values(),
            key=lambda row: (
                str(row.get("provider_id") or ""),
                str(row.get("model_id") or ""),
                str(row.get("reasoning_mode") or ""),
                bool(row.get("_advertised_only")),
                str(row.get("route_id") or ""),
            ),
        )
    )


def point_in_rectangle(
    x: int,
    y: int,
    *,
    left: int,
    top: int,
    width: int,
    height: int,
) -> bool:
    """Return whether a root-window point is inside a live widget rectangle."""

    return (
        width > 0
        and height > 0
        and left <= x <= left + width
        and top <= y <= top + height
    )


def discovered_route_profile_id(
    *,
    scope: str,
    agent_id: str,
    provider_id: str,
    model_id: str,
    reasoning_mode: str | None,
) -> str:
    """Return a stable safe route ID for an explicitly selected live model."""

    payload = json.dumps(
        {
            "scope": scope,
            "agent_id": agent_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "reasoning_mode": reasoning_mode,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"discovered-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:40]}"


def room_members_missing_routes(members: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """Return active non-human seats that cannot participate in routed fanout."""
    return tuple(
        sorted(
            {
                str(row["agent_id"])
                for row in members
                if row.get("status") == "active"
                and row.get("agent_id")
                and row.get("agent_id") != HUMAN_AGENT_ID
                and not row.get("route_profile_id")
            }
        )
    )


def room_messages(
    messages: Iterable[dict[str, Any]], room_id: str
) -> tuple[dict[str, Any], ...]:
    """Defensively constrain polled history to one selected room."""
    return tuple(row for row in messages if row.get("room_id", DEFAULT_ROOM_ID) == room_id)


@dataclass(frozen=True)
class Snapshot:
    generated_utc: str
    database_path: str
    database_mtime_ns: int
    presence: tuple[dict[str, Any], ...]
    route_profiles: tuple[dict[str, Any], ...]
    provider_connections: tuple[dict[str, Any], ...]
    memories: tuple[dict[str, Any], ...]
    operations: tuple[dict[str, Any], ...]
    schedules: tuple[dict[str, Any], ...]
    capabilities: tuple[dict[str, Any], ...]
    capability_grants: tuple[dict[str, Any], ...]
    permission_decisions: tuple[dict[str, Any], ...]
    execution_bindings: tuple[dict[str, Any], ...]
    task_briefings: tuple[dict[str, Any], ...]
    decision_conflicts: tuple[dict[str, Any], ...]
    trust_records: tuple[dict[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
    message_dispatches: tuple[dict[str, Any], ...]
    usage_totals: dict[str, Any]
    usage_by_provider: tuple[dict[str, Any], ...]
    usage_by_model: tuple[dict[str, Any], ...]
    usage_model_totals: tuple[dict[str, Any], ...]
    usage_daily: tuple[dict[str, Any], ...]
    usage_periods: dict[str, dict[str, Any]]
    usage_recent: tuple[dict[str, Any], ...]
    peer_calls: tuple[dict[str, Any], ...]
    peer_reviews: tuple[dict[str, Any], ...]
    tasks: tuple[dict[str, Any], ...]
    work_updates: tuple[dict[str, Any], ...]
    changes: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    table_counts: dict[str, int]

    def signature(self) -> str:
        payload = {
            "mtime": self.database_mtime_ns,
            "counts": self.table_counts,
            "latest": {
                "message": self.messages[0].get("content_sha256") if self.messages else None,
                "usage": (
                    self.usage_recent[0].get("usage_sha256")
                    if self.usage_recent
                    else None
                ),
                "call": self.peer_calls[0].get("response_sha256") if self.peer_calls else None,
                "event": self.events[0].get("payload_sha256") if self.events else None,
                "update": self.work_updates[0].get("update_sha256") if self.work_updates else None,
                "connection": (
                    self.provider_connections[0].get("connection_sha256")
                    if self.provider_connections
                    else None
                ),
                "memory": (
                    self.memories[0].get("revocation_sha256")
                    or self.memories[0].get("memory_sha256")
                    if self.memories
                    else None
                ),
                "operation": (
                    self.operations[0].get("operation_sha256")
                    if self.operations
                    else None
                ),
                "schedule": (
                    self.schedules[0].get("schedule_sha256")
                    if self.schedules
                    else None
                ),
                "capability": (
                    self.capabilities[0].get("capability_sha256")
                    if self.capabilities
                    else None
                ),
                "grant": (
                    self.capability_grants[0].get("grant_sha256")
                    if self.capability_grants
                    else None
                ),
                "permission": (
                    self.permission_decisions[0].get("decision_sha256")
                    if self.permission_decisions
                    else None
                ),
                "binding": (
                    self.execution_bindings[0].get("binding_sha256")
                    if self.execution_bindings
                    else None
                ),
                "briefing": (
                    self.task_briefings[0].get("briefing_sha256")
                    if self.task_briefings
                    else None
                ),
                "conflict": (
                    self.decision_conflicts[0].get("finding_sha256")
                    if self.decision_conflicts
                    else None
                ),
                "trust": (
                    self.trust_records[0].get("trust_sha256")
                    if self.trust_records
                    else None
                ),
            },
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class BridgeReader:
    TABLES = (
        "agent_presence",
        "route_profiles",
        "provider_connections",
        "room_memberships",
        "room_automation_policies",
        "room_discussions",
        "memories",
        "governance_operations",
        "workflow_schedules",
        "capability_registry",
        "capability_grants",
        "permission_decisions",
        "execution_bindings",
        "task_briefings",
        "decision_conflict_findings",
        "trust_records",
        "message_route_receipts",
        "messages",
        "message_dispatches",
        "inference_usage",
        "peer_calls",
        "peer_reviews",
        "tasks",
        "work_updates",
        "integration_records",
        "events",
    )

    def __init__(self, db_path: Path, project_root: Path | None = None) -> None:
        self.db_path = db_path.resolve()
        self.project_root = project_root.resolve() if project_root is not None else None
        self._token_connection: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def change_token(self) -> tuple[tuple[str, int, int], ...]:
        """Return a logical change token without reacting to WAL housekeeping.

        A persistent read-only connection is required because SQLite's
        ``data_version`` is meaningful relative to one connection.  Checkpoint
        or WAL file-size changes do not increment it, so harmless read activity
        cannot cause an expensive UI rebuild.
        """
        if self._token_connection is None:
            self._token_connection = self.connect()
        data_version = int(
            self._token_connection.execute("PRAGMA data_version").fetchone()[0]
        )
        schema_version = int(
            self._token_connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        return (("sqlite-logical", data_version, schema_version),)

    def close(self) -> None:
        connection = self._token_connection
        self._token_connection = None
        if connection is not None:
            connection.close()

    @staticmethod
    def _rows(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in connection.execute(query, params).fetchall())

    @staticmethod
    def _usage_period_where(
        period: str,
        scope: str | None,
        *,
        timestamp: str = "recorded_utc",
    ) -> tuple[str, tuple[Any, ...]]:
        if period not in USAGE_PERIOD_KEYS:
            raise ValueError(f"unsupported usage period: {period}")
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.append("scope=?")
            params.append(scope)
        boundaries = {
            "today": "datetime('now', 'start of day')",
            "7d": "datetime('now', 'start of day', '-6 days')",
            "30d": "datetime('now', 'start of day', '-29 days')",
        }
        if period in boundaries:
            clauses.append(f"datetime({timestamp}) >= {boundaries[period]}")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where, tuple(params)

    @staticmethod
    def _usage_trend_projection(alias: str = "usage") -> str:
        prefix = f"{alias}." if alias else ""
        return f"""COUNT({prefix}message_id) AS completed_dispatches,
                    COALESCE(SUM({prefix}total_calls), 0) AS provider_calls,
                    COALESCE(SUM({prefix}reported_calls), 0) AS reported_calls,
                    CASE WHEN COUNT({prefix}message_id) = 0 THEN 0
                         ELSE SUM({prefix}input_tokens) END AS input_tokens,
                    CASE WHEN COUNT({prefix}message_id) = 0 THEN 0
                         ELSE SUM({prefix}output_tokens) END AS output_tokens,
                    CASE WHEN COUNT({prefix}message_id) = 0 THEN 0
                         ELSE SUM({prefix}total_tokens) END AS total_tokens,
                    CASE WHEN COUNT({prefix}message_id) = 0 THEN 0
                         ELSE SUM({prefix}cached_input_tokens) END AS cached_input_tokens,
                    CASE WHEN COUNT({prefix}message_id) = 0 THEN 0
                         ELSE SUM({prefix}reasoning_tokens) END AS reasoning_tokens,
                    COALESCE(SUM({prefix}input_tokens_reported_calls), 0)
                        AS input_tokens_reported_calls,
                    COALESCE(SUM({prefix}output_tokens_reported_calls), 0)
                        AS output_tokens_reported_calls,
                    COALESCE(SUM({prefix}total_tokens_reported_calls), 0)
                        AS total_tokens_reported_calls,
                    COALESCE(SUM({prefix}cached_input_tokens_reported_calls), 0)
                        AS cached_input_tokens_reported_calls,
                    COALESCE(SUM({prefix}reasoning_tokens_reported_calls), 0)
                        AS reasoning_tokens_reported_calls"""

    def _usage_trend(
        self,
        connection: sqlite3.Connection,
        *,
        period: str,
        scope: str | None,
    ) -> tuple[tuple[dict[str, Any], ...], str, bool]:
        projection = self._usage_trend_projection()
        scope_join = " AND usage.scope=?" if scope is not None else ""
        params: tuple[Any, ...] = (scope,) if scope is not None else ()
        if period == "today":
            rows = self._rows(
                connection,
                f"""WITH RECURSIVE hours(hour_number) AS (
                            SELECT 0
                            UNION ALL
                            SELECT hour_number + 1 FROM hours WHERE hour_number < 23
                        )
                        SELECT printf('%02d:00', hours.hour_number) AS period_label,
                               date('now') || 'T' || printf('%02d:00Z', hours.hour_number)
                                   AS period_key,
                               {projection}
                          FROM hours
                     LEFT JOIN inference_usage AS usage
                            ON strftime('%H', usage.recorded_utc) = printf('%02d', hours.hour_number)
                           AND datetime(usage.recorded_utc) >= datetime('now', 'start of day')
                           {scope_join}
                      GROUP BY hours.hour_number
                      ORDER BY hours.hour_number""",
                params,
            )
            return rows, "hour", False
        if period in {"7d", "30d"}:
            days = 6 if period == "7d" else 29
            rows = self._rows(
                connection,
                f"""WITH RECURSIVE days(utc_date) AS (
                            SELECT date('now', '-{days} days')
                            UNION ALL
                            SELECT date(utc_date, '+1 day') FROM days
                             WHERE utc_date < date('now')
                        )
                        SELECT days.utc_date AS period_label,
                               days.utc_date AS period_key,
                               {projection}
                          FROM days
                     LEFT JOIN inference_usage AS usage
                            ON substr(usage.recorded_utc, 1, 10) = days.utc_date
                           {scope_join}
                      GROUP BY days.utc_date
                      ORDER BY days.utc_date""",
                params,
            )
            return rows, "day", False

        where, all_params = self._usage_period_where("all", scope)
        monthly_projection = self._usage_trend_projection("")
        rows = self._rows(
            connection,
            f"""SELECT substr(recorded_utc, 1, 7) AS period_label,
                       substr(recorded_utc, 1, 7) AS period_key,
                       {monthly_projection}
                  FROM inference_usage{where}
                 GROUP BY substr(recorded_utc, 1, 7)
                 ORDER BY period_key DESC
                 LIMIT 241""",
            all_params,
        )
        truncated = len(rows) > 240
        return tuple(reversed(rows[:240])), "month", truncated

    def _usage_period_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        period: str,
        scope: str | None,
    ) -> dict[str, Any]:
        where, params = self._usage_period_where(period, scope)
        total_row = connection.execute(
            f"""SELECT COUNT(*) AS completed_dispatches,
                        COALESCE(SUM(total_calls), 0) AS provider_calls,
                        COALESCE(SUM(reported_calls), 0) AS reported_calls,
                        SUM(input_tokens) AS input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(total_tokens) AS total_tokens,
                        SUM(cached_input_tokens) AS cached_input_tokens,
                        SUM(reasoning_tokens) AS reasoning_tokens,
                        COALESCE(SUM(input_tokens_reported_calls), 0)
                            AS input_tokens_reported_calls,
                        COALESCE(SUM(output_tokens_reported_calls), 0)
                            AS output_tokens_reported_calls,
                        COALESCE(SUM(total_tokens_reported_calls), 0)
                            AS total_tokens_reported_calls,
                        COALESCE(SUM(cached_input_tokens_reported_calls), 0)
                            AS cached_input_tokens_reported_calls,
                        COALESCE(SUM(reasoning_tokens_reported_calls), 0)
                            AS reasoning_tokens_reported_calls,
                        COALESCE(SUM(total_tokens_derived), 0)
                            AS derived_total_dispatches,
                        SUM(CASE WHEN usage_status='reported' THEN 1 ELSE 0 END)
                            AS fully_reported_dispatches,
                        SUM(CASE WHEN usage_status='partial' THEN 1 ELSE 0 END)
                            AS partial_dispatches,
                        SUM(CASE WHEN usage_status='unavailable' THEN 1 ELSE 0 END)
                            AS unavailable_dispatches
                   FROM inference_usage{where}""",
            params,
        ).fetchone()
        totals = dict(total_row)
        dispatch_where, dispatch_params = self._usage_period_where(
            period, scope, timestamp="updated_utc"
        )
        dispatch_rows = connection.execute(
            f"""SELECT status, COUNT(*) AS count
                   FROM message_dispatches{dispatch_where}
                  GROUP BY status""",
            dispatch_params,
        ).fetchall()
        totals["dispatch_statuses"] = {
            str(row["status"]): int(row["count"]) for row in dispatch_rows
        }
        by_model = self._rows(
            connection,
            f"""SELECT COALESCE(provider_id, '--') AS provider_id,
                        COALESCE(model_id, '--') AS model_id,
                        COUNT(*) AS completed_dispatches,
                        COALESCE(SUM(total_calls), 0) AS provider_calls,
                        COALESCE(SUM(reported_calls), 0) AS reported_calls,
                        SUM(input_tokens) AS input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(total_tokens) AS total_tokens,
                        SUM(cached_input_tokens) AS cached_input_tokens,
                        SUM(reasoning_tokens) AS reasoning_tokens,
                        COALESCE(SUM(input_tokens_reported_calls), 0)
                            AS input_tokens_reported_calls,
                        COALESCE(SUM(output_tokens_reported_calls), 0)
                            AS output_tokens_reported_calls,
                        COALESCE(SUM(total_tokens_reported_calls), 0)
                            AS total_tokens_reported_calls,
                        COALESCE(SUM(cached_input_tokens_reported_calls), 0)
                            AS cached_input_tokens_reported_calls,
                        COALESCE(SUM(reasoning_tokens_reported_calls), 0)
                            AS reasoning_tokens_reported_calls,
                        COALESCE(SUM(total_tokens_derived), 0)
                            AS derived_total_dispatches
                   FROM inference_usage{where}
                  GROUP BY provider_id, model_id
                  ORDER BY total_tokens DESC, provider_calls DESC, model_id
                  LIMIT 100""",
            params,
        )
        by_model_total = self._rows(
            connection,
            f"""SELECT COALESCE(model_id, '--') AS model_id,
                        COUNT(*) AS completed_dispatches,
                        COALESCE(SUM(total_calls), 0) AS provider_calls,
                        COALESCE(SUM(reported_calls), 0) AS reported_calls,
                        SUM(input_tokens) AS input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(total_tokens) AS total_tokens,
                        SUM(cached_input_tokens) AS cached_input_tokens,
                        SUM(reasoning_tokens) AS reasoning_tokens,
                        COALESCE(SUM(input_tokens_reported_calls), 0)
                            AS input_tokens_reported_calls,
                        COALESCE(SUM(output_tokens_reported_calls), 0)
                            AS output_tokens_reported_calls,
                        COALESCE(SUM(total_tokens_reported_calls), 0)
                            AS total_tokens_reported_calls,
                        COALESCE(SUM(cached_input_tokens_reported_calls), 0)
                            AS cached_input_tokens_reported_calls,
                        COALESCE(SUM(reasoning_tokens_reported_calls), 0)
                            AS reasoning_tokens_reported_calls
                   FROM inference_usage{where}
                  GROUP BY model_id
                  ORDER BY total_tokens DESC, provider_calls DESC, model_id
                  LIMIT 100""",
            params,
        )
        by_provider = self._rows(
            connection,
            f"""SELECT COALESCE(provider_id, '--') AS provider_id,
                        COUNT(*) AS completed_dispatches,
                        COALESCE(SUM(total_calls), 0) AS provider_calls,
                        COALESCE(SUM(reported_calls), 0) AS reported_calls,
                        SUM(input_tokens) AS input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(total_tokens) AS total_tokens,
                        COALESCE(SUM(input_tokens_reported_calls), 0)
                            AS input_tokens_reported_calls,
                        COALESCE(SUM(output_tokens_reported_calls), 0)
                            AS output_tokens_reported_calls,
                        COALESCE(SUM(total_tokens_reported_calls), 0)
                            AS total_tokens_reported_calls,
                        CASE
                          WHEN SUM(CASE WHEN substr(recorded_utc, 1, 10) = date('now')
                                        THEN 1 ELSE 0 END) = 0 THEN 0
                          ELSE SUM(CASE WHEN substr(recorded_utc, 1, 10) = date('now')
                                        THEN total_tokens END)
                        END AS today_tokens,
                        COALESCE(SUM(CASE
                          WHEN substr(recorded_utc, 1, 10) = date('now')
                          THEN total_tokens_reported_calls ELSE 0 END), 0)
                            AS today_total_tokens_reported_calls
                   FROM inference_usage{where}
                  GROUP BY provider_id
                  ORDER BY total_tokens DESC, provider_calls DESC, provider_id
                  LIMIT 12""",
            params,
        )
        trend, granularity, trend_truncated = self._usage_trend(
            connection, period=period, scope=scope
        )
        return {
            "period": period,
            "totals": totals,
            "by_provider": by_provider,
            "by_model": by_model,
            "model_totals": by_model_total,
            "trend": trend,
            "granularity": granularity,
            "trend_truncated": trend_truncated,
        }

    def _trust_rows(
        self,
        connection: sqlite3.Connection,
        where: str,
        params: tuple[Any, ...],
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        rows = self._rows(
            connection,
            f"SELECT * FROM trust_records{where} "
            "ORDER BY created_utc DESC, rowid DESC LIMIT ?",
            (*params, limit),
        )
        projected = []
        for raw in rows:
            row = dict(raw)
            bindings = safe_json(row.get("source_bindings_json"), [])
            related = safe_json(row.get("related_record_ids_json"), [])
            if not isinstance(bindings, list):
                bindings = []
            if not isinstance(related, list):
                related = []
            payload = {
                "scope": row.get("scope"),
                "record_id": row.get("record_id"),
                "task_id": row.get("task_id"),
                "actor": row.get("actor"),
                "stage": row.get("stage"),
                "statement": row.get("statement"),
                "source_bindings": bindings,
                "related_record_ids": related,
                "created_utc": row.get("created_utc"),
            }
            integrity_valid = stable_sha256(payload) == row.get("trust_sha256")
            live_bindings = [self._trust_binding_freshness(item) for item in bindings]
            if not integrity_valid:
                freshness = "invalid"
            elif self.project_root is None:
                freshness = "unavailable"
            elif any(item.get("stale") for item in live_bindings):
                freshness = "stale"
            else:
                freshness = "fresh"
            row.update(
                {
                    "source_bindings": live_bindings,
                    "related_record_ids": related,
                    "integrity_valid": integrity_valid,
                    "freshness": freshness,
                    "stale": freshness in {"stale", "invalid"},
                }
            )
            projected.append(row)
        return tuple(projected)

    def _trust_binding_freshness(self, value: Any) -> dict[str, Any]:
        binding = dict(value) if isinstance(value, dict) else {"path": str(value)}
        if self.project_root is None:
            return {**binding, "stale": None, "stale_reason": "project_root_unavailable"}
        relative = str(binding.get("path") or "").replace("\\", "/")
        candidate = Path(relative)
        if not relative or candidate.is_absolute() or ".." in candidate.parts:
            return {**binding, "stale": True, "stale_reason": "unsafe_source_path"}
        resolved = (self.project_root / candidate).resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError:
            return {**binding, "stale": True, "stale_reason": "unsafe_source_path"}
        if not resolved.is_file():
            return {**binding, "stale": True, "stale_reason": "source_missing"}
        hasher = hashlib.sha256()
        size = 0
        try:
            with resolved.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    hasher.update(chunk)
                    size += len(chunk)
        except OSError:
            return {**binding, "stale": True, "stale_reason": "source_unreadable"}
        live_sha = hasher.hexdigest()
        stale = size != int(binding.get("bytes") or -1) or live_sha != str(
            binding.get("sha256") or ""
        )
        return {
            **binding,
            "stale": stale,
            "stale_reason": "source_changed" if stale else None,
            "live_bytes": size,
            "live_sha256": live_sha,
        }

    def snapshot(
        self,
        limit: int = DEFAULT_MONITOR_SNAPSHOT_LIMIT,
        *,
        scope: str | None = None,
    ) -> Snapshot:
        if not self.db_path.is_file():
            raise FileNotFoundError(f"MCP database not found: {self.db_path}")

        where = " WHERE scope=?" if scope is not None else ""
        params: tuple[Any, ...] = (scope,) if scope is not None else ()

        with contextlib.closing(self.connect()) as connection:
            live_tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = set(self.TABLES) - live_tables
            if missing:
                raise RuntimeError("MCP database is missing tables: " + ", ".join(sorted(missing)))

            counts = {
                name: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{name}"{where}', params
                    ).fetchone()[0]
                )
                for name in self.TABLES
            }
            presence = self._rows(
                connection,
                f"SELECT * FROM agent_presence{where} ORDER BY last_seen_epoch DESC",
                params,
            )
            route_profiles = self._rows(
                connection,
                f"SELECT * FROM route_profiles{where} ORDER BY route_id",
                params,
            )
            provider_connections = self._rows(
                connection,
                f"SELECT * FROM provider_connections{where} ORDER BY display_name, connection_id",
                params,
            )
            memory_scope = "mem.scope=? AND " if scope is not None else ""
            memory_params: tuple[Any, ...] = (
                (scope, HUMAN_AGENT_ID, DEFAULT_ROOM_ID, HUMAN_AGENT_ID, limit)
                if scope is not None
                else (HUMAN_AGENT_ID, DEFAULT_ROOM_ID, HUMAN_AGENT_ID, limit)
            )
            memory_access = f"""{memory_scope}(
                mem.visibility='project'
                OR (
                    (
                        mem.visibility='room'
                        OR (mem.visibility='private' AND mem.owner_agent_id=?)
                    )
                    AND (
                        mem.room_id=?
                        OR EXISTS (
                            SELECT 1 FROM room_memberships rm
                             WHERE rm.scope=mem.scope
                               AND rm.room_id=mem.room_id
                               AND rm.agent_id=?
                               AND rm.status='active'
                        )
                    )
                )
            )"""
            memories = self._rows(
                connection,
                f"""SELECT mem.* FROM memories mem
                     WHERE {memory_access}
                     ORDER BY mem.created_utc DESC, mem.memory_id LIMIT ?""",
                memory_params,
            )
            memory_count_params = memory_params[:-1]
            counts["memories"] = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM memories mem WHERE {memory_access}",
                    memory_count_params,
                ).fetchone()[0]
            )
            operations = self._rows(
                connection,
                f"SELECT * FROM governance_operations{where} "
                "ORDER BY updated_utc DESC, operation_id LIMIT ?",
                (*params, limit),
            )
            schedules = self._rows(
                connection,
                f"SELECT * FROM workflow_schedules{where} "
                "ORDER BY updated_utc DESC, schedule_id LIMIT ?",
                (*params, limit),
            )
            capabilities = self._rows(
                connection,
                f"SELECT * FROM capability_registry{where} "
                "ORDER BY created_utc DESC, capability_id, registry_version LIMIT ?",
                (*params, limit),
            )
            capability_grants = self._rows(
                connection,
                f"SELECT * FROM capability_grants{where} "
                "ORDER BY created_utc DESC, rowid DESC LIMIT ?",
                (*params, limit),
            )
            permission_decisions = self._rows(
                connection,
                f"SELECT * FROM permission_decisions{where} "
                "ORDER BY created_utc DESC, decision_id LIMIT ?",
                (*params, limit),
            )
            execution_bindings = self._rows(
                connection,
                f"SELECT * FROM execution_bindings{where} "
                "ORDER BY updated_utc DESC, binding_id LIMIT ?",
                (*params, limit),
            )
            task_briefings = self._rows(
                connection,
                f"SELECT * FROM task_briefings{where} "
                "ORDER BY created_utc DESC, briefing_id LIMIT ?",
                (*params, limit),
            )
            decision_conflicts = self._rows(
                connection,
                f"SELECT * FROM decision_conflict_findings{where} "
                "ORDER BY created_utc DESC, finding_id LIMIT ?",
                (*params, limit),
            )
            trust_records = self._trust_rows(connection, where, params, limit)
            message_where = "WHERE m.scope=?" if scope is not None else ""
            message_params: tuple[Any, ...] = (
                (scope, limit) if scope is not None else (limit,)
            )
            messages = self._rows(
                connection,
                """SELECT m.*, rr.route_status, rr.observed_provider_id,
                          rr.observed_model_id, rr.observed_reasoning_mode,
                          rr.receipt_sha256 AS route_receipt_sha256
                   FROM messages m
                   LEFT JOIN message_route_receipts rr
                     ON rr.scope=m.scope AND rr.message_id=m.message_id
                    AND rr.agent_id=m.recipient
                   {message_where}
                   ORDER BY m.created_utc DESC LIMIT ?""".format(
                    message_where=message_where
                ),
                message_params,
            )
            message_dispatches = self._rows(
                connection,
                f"SELECT * FROM message_dispatches{where} "
                "ORDER BY updated_utc DESC, message_id LIMIT ?",
                (*params, limit),
            )
            usage_total_row = connection.execute(
                f"""SELECT COUNT(*) AS completed_dispatches,
                             COALESCE(SUM(total_calls), 0) AS provider_calls,
                             COALESCE(SUM(reported_calls), 0) AS reported_calls,
                             SUM(input_tokens) AS input_tokens,
                             SUM(output_tokens) AS output_tokens,
                             SUM(total_tokens) AS total_tokens,
                             SUM(cached_input_tokens) AS cached_input_tokens,
                             SUM(reasoning_tokens) AS reasoning_tokens,
                             COALESCE(SUM(input_tokens_reported_calls), 0)
                                 AS input_tokens_reported_calls,
                             COALESCE(SUM(output_tokens_reported_calls), 0)
                                 AS output_tokens_reported_calls,
                             COALESCE(SUM(total_tokens_reported_calls), 0)
                                 AS total_tokens_reported_calls,
                             COALESCE(SUM(cached_input_tokens_reported_calls), 0)
                                 AS cached_input_tokens_reported_calls,
                             COALESCE(SUM(reasoning_tokens_reported_calls), 0)
                                 AS reasoning_tokens_reported_calls,
                            COALESCE(SUM(total_tokens_derived), 0)
                                AS derived_total_dispatches,
                            SUM(CASE WHEN usage_status='reported' THEN 1 ELSE 0 END)
                                AS fully_reported_dispatches,
                            SUM(CASE WHEN usage_status='partial' THEN 1 ELSE 0 END)
                                AS partial_dispatches,
                            SUM(CASE WHEN usage_status='unavailable' THEN 1 ELSE 0 END)
                                AS unavailable_dispatches
                       FROM inference_usage{where}""",
                params,
            ).fetchone()
            usage_totals = dict(usage_total_row)
            dispatch_status_rows = connection.execute(
                f"""SELECT status, COUNT(*) AS count
                       FROM message_dispatches{where}
                      GROUP BY status""",
                params,
            ).fetchall()
            usage_totals["dispatch_statuses"] = {
                str(row["status"]): int(row["count"])
                for row in dispatch_status_rows
            }
            usage_by_model = self._rows(
                connection,
                f"""SELECT COALESCE(provider_id, '--') AS provider_id,
                            COALESCE(model_id, '--') AS model_id,
                            COUNT(*) AS completed_dispatches,
                             COALESCE(SUM(total_calls), 0) AS provider_calls,
                             COALESCE(SUM(reported_calls), 0) AS reported_calls,
                             SUM(input_tokens) AS input_tokens,
                             SUM(output_tokens) AS output_tokens,
                             SUM(total_tokens) AS total_tokens,
                             SUM(cached_input_tokens) AS cached_input_tokens,
                             SUM(reasoning_tokens) AS reasoning_tokens,
                             COALESCE(SUM(input_tokens_reported_calls), 0)
                                 AS input_tokens_reported_calls,
                             COALESCE(SUM(output_tokens_reported_calls), 0)
                                 AS output_tokens_reported_calls,
                             COALESCE(SUM(total_tokens_reported_calls), 0)
                                 AS total_tokens_reported_calls,
                             COALESCE(SUM(cached_input_tokens_reported_calls), 0)
                                 AS cached_input_tokens_reported_calls,
                             COALESCE(SUM(reasoning_tokens_reported_calls), 0)
                                 AS reasoning_tokens_reported_calls
                            ,COALESCE(SUM(total_tokens_derived), 0)
                                AS derived_total_dispatches
                       FROM inference_usage{where}
                      GROUP BY provider_id, model_id
                      ORDER BY total_tokens DESC, provider_calls DESC, model_id
                      LIMIT 100""",
                params,
            )
            usage_model_totals = self._rows(
                connection,
                f"""SELECT COALESCE(model_id, '--') AS model_id,
                            COUNT(*) AS completed_dispatches,
                             COALESCE(SUM(total_calls), 0) AS provider_calls,
                             COALESCE(SUM(reported_calls), 0) AS reported_calls,
                             SUM(input_tokens) AS input_tokens,
                             SUM(output_tokens) AS output_tokens,
                             SUM(total_tokens) AS total_tokens,
                             SUM(cached_input_tokens) AS cached_input_tokens,
                             SUM(reasoning_tokens) AS reasoning_tokens,
                             COALESCE(SUM(input_tokens_reported_calls), 0)
                                 AS input_tokens_reported_calls,
                             COALESCE(SUM(output_tokens_reported_calls), 0)
                                 AS output_tokens_reported_calls,
                             COALESCE(SUM(total_tokens_reported_calls), 0)
                                 AS total_tokens_reported_calls,
                             COALESCE(SUM(cached_input_tokens_reported_calls), 0)
                                 AS cached_input_tokens_reported_calls,
                             COALESCE(SUM(reasoning_tokens_reported_calls), 0)
                                 AS reasoning_tokens_reported_calls
                       FROM inference_usage{where}
                      GROUP BY model_id
                      ORDER BY total_tokens DESC, provider_calls DESC, model_id
                      LIMIT 100""",
                params,
            )
            usage_by_provider = self._rows(
                connection,
                f"""SELECT COALESCE(provider_id, '--') AS provider_id,
                            COUNT(*) AS completed_dispatches,
                             COALESCE(SUM(total_calls), 0) AS provider_calls,
                             COALESCE(SUM(reported_calls), 0) AS reported_calls,
                             SUM(input_tokens) AS input_tokens,
                             SUM(output_tokens) AS output_tokens,
                             SUM(total_tokens) AS total_tokens,
                             COALESCE(SUM(input_tokens_reported_calls), 0)
                                 AS input_tokens_reported_calls,
                             COALESCE(SUM(output_tokens_reported_calls), 0)
                                 AS output_tokens_reported_calls,
                             COALESCE(SUM(total_tokens_reported_calls), 0)
                                 AS total_tokens_reported_calls,
                            CASE
                              WHEN SUM(CASE WHEN substr(recorded_utc, 1, 10) = date('now')
                                            THEN 1 ELSE 0 END) = 0 THEN 0
                               ELSE SUM(CASE WHEN substr(recorded_utc, 1, 10) = date('now')
                                             THEN total_tokens END)
                             END AS today_tokens
                            ,COALESCE(SUM(CASE
                               WHEN substr(recorded_utc, 1, 10) = date('now')
                               THEN total_tokens_reported_calls ELSE 0 END), 0)
                                 AS today_total_tokens_reported_calls
                       FROM inference_usage{where}
                      GROUP BY provider_id
                      ORDER BY total_tokens DESC, provider_calls DESC, provider_id
                      LIMIT 12""",
                params,
            )
            daily_scope_join = " AND usage.scope=?" if scope is not None else ""
            usage_daily = self._rows(
                connection,
                f"""WITH RECURSIVE days(utc_date) AS (
                            SELECT date('now', '-29 days')
                            UNION ALL
                            SELECT date(utc_date, '+1 day') FROM days
                             WHERE utc_date < date('now')
                        )
                        SELECT days.utc_date AS utc_date,
                            COUNT(usage.message_id) AS completed_dispatches,
                             COALESCE(SUM(usage.total_calls), 0) AS provider_calls,
                             COALESCE(SUM(usage.reported_calls), 0) AS reported_calls,
                             CASE WHEN COUNT(usage.message_id) = 0 THEN 0
                                  ELSE SUM(usage.input_tokens) END AS input_tokens,
                             CASE WHEN COUNT(usage.message_id) = 0 THEN 0
                                  ELSE SUM(usage.output_tokens) END AS output_tokens,
                             CASE WHEN COUNT(usage.message_id) = 0 THEN 0
                                  ELSE SUM(usage.total_tokens) END AS total_tokens,
                             CASE WHEN COUNT(usage.message_id) = 0 THEN 0
                                  ELSE SUM(usage.cached_input_tokens) END
                                 AS cached_input_tokens,
                             CASE WHEN COUNT(usage.message_id) = 0 THEN 0
                                  ELSE SUM(usage.reasoning_tokens) END
                                 AS reasoning_tokens,
                             COALESCE(SUM(usage.input_tokens_reported_calls), 0)
                                 AS input_tokens_reported_calls,
                             COALESCE(SUM(usage.output_tokens_reported_calls), 0)
                                 AS output_tokens_reported_calls,
                             COALESCE(SUM(usage.total_tokens_reported_calls), 0)
                                 AS total_tokens_reported_calls,
                             COALESCE(SUM(usage.cached_input_tokens_reported_calls), 0)
                                 AS cached_input_tokens_reported_calls,
                             COALESCE(SUM(usage.reasoning_tokens_reported_calls), 0)
                                 AS reasoning_tokens_reported_calls
                       FROM days
                       LEFT JOIN inference_usage AS usage
                         ON substr(usage.recorded_utc, 1, 10) = days.utc_date
                        {daily_scope_join}
                      GROUP BY days.utc_date
                      ORDER BY days.utc_date""",
                params,
            )
            usage_periods = {
                period: self._usage_period_snapshot(
                    connection,
                    period=period,
                    scope=scope,
                )
                for period in USAGE_PERIOD_KEYS
            }
            usage_recent = self._rows(
                connection,
                f"SELECT * FROM inference_usage{where} "
                "ORDER BY recorded_utc DESC, message_id LIMIT ?",
                (*params, limit),
            )
            calls = self._rows(
                connection,
                f"SELECT * FROM peer_calls{where} ORDER BY request_utc DESC LIMIT ?",
                (*params, limit),
            )
            reviews = self._rows(
                connection,
                f"SELECT * FROM peer_reviews{where} ORDER BY review_utc DESC LIMIT ?",
                (*params, limit),
            )
            tasks = self._rows(
                connection,
                f"SELECT * FROM tasks{where} ORDER BY updated_utc DESC LIMIT ?",
                (*params, limit),
            )
            updates = self._rows(
                connection,
                f"SELECT * FROM work_updates{where} ORDER BY created_utc DESC LIMIT ?",
                (*params, limit),
            )
            changes = self._rows(
                connection,
                f"SELECT * FROM integration_records{where} ORDER BY recorded_utc DESC LIMIT ?",
                (*params, limit),
            )
            events = self._rows(
                connection,
                f"SELECT * FROM events{where} ORDER BY created_utc DESC LIMIT ?",
                (*params, limit),
            )

        return Snapshot(
            generated_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            database_path=str(self.db_path),
            database_mtime_ns=self.db_path.stat().st_mtime_ns,
            presence=presence,
            route_profiles=route_profiles,
            provider_connections=provider_connections,
            memories=memories,
            operations=operations,
            schedules=schedules,
            capabilities=capabilities,
            capability_grants=capability_grants,
            permission_decisions=permission_decisions,
            execution_bindings=execution_bindings,
            task_briefings=task_briefings,
            decision_conflicts=decision_conflicts,
            trust_records=trust_records,
            messages=messages,
            message_dispatches=message_dispatches,
            usage_totals=usage_totals,
            usage_by_provider=usage_by_provider,
            usage_by_model=usage_by_model,
            usage_model_totals=usage_model_totals,
            usage_daily=usage_daily,
            usage_periods=usage_periods,
            usage_recent=usage_recent,
            peer_calls=calls,
            peer_reviews=reviews,
            tasks=tasks,
            work_updates=updates,
            changes=changes,
            events=events,
            table_counts=counts,
        )

    @staticmethod
    def _catalog_sha256(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def room_view(
        self,
        *,
        scope: str,
        requested_room_id: str,
        consumer: str = HUMAN_AGENT_ID,
        limit: int = DEFAULT_ROOM_PAGE_SIZE,
        before_sequence: int | None = None,
        presence_ttl_seconds: int = 120,
    ) -> dict[str, Any]:
        """Read one bounded room-history page without creating writes.

        The local control-room monitor can inspect the complete room transcript,
        including peer-to-peer messages, even before the human operator joins a
        room. Room membership still gates every write or management action.
        ``before_sequence`` pages backwards without ever retaining the whole
        room history in the UI process.
        """
        if not self.db_path.is_file():
            raise FileNotFoundError(f"MCP database not found: {self.db_path}")
        limit = max(1, min(int(limit), 500))
        cutoff = time.time() - max(30, min(int(presence_ttl_seconds), 3600))

        with contextlib.closing(self.connect()) as connection:
            room_rows = connection.execute(
                """SELECT r.*,
                    (SELECT COUNT(*) FROM room_memberships rm
                     WHERE rm.scope=r.scope AND rm.room_id=r.room_id
                       AND rm.status='active') AS active_member_count,
                    (SELECT COUNT(*) FROM messages m
                     WHERE m.scope=r.scope AND m.room_id=r.room_id) AS message_count
                   FROM rooms r WHERE r.scope=? AND r.archived=0
                   ORDER BY CASE WHEN r.room_id=? THEN 0 ELSE 1 END,
                            r.updated_utc DESC, r.room_id""",
                (scope, DEFAULT_ROOM_ID),
            ).fetchall()
            rooms = tuple(
                {**dict(row), "archived": bool(row["archived"])} for row in room_rows
            )
            room_ids = {str(row["room_id"]) for row in room_rows}
            room_id = (
                requested_room_id
                if requested_room_id in room_ids
                else DEFAULT_ROOM_ID
            )
            if room_id not in room_ids:
                raise RuntimeError("built-in lobby room is missing")

            policy_row = connection.execute(
                """SELECT * FROM room_automation_policies
                   WHERE scope=? AND room_id=?""",
                (scope, room_id),
            ).fetchone()
            if policy_row is None:
                raise RuntimeError("room automation policy is missing")
            discussion_row = connection.execute(
                """SELECT * FROM room_discussions
                   WHERE scope=? AND room_id=?
                     AND status IN ('active', 'paused', 'waiting_human')
                   ORDER BY updated_utc DESC, discussion_id DESC LIMIT 1""",
                (scope, room_id),
            ).fetchone()
            automation = dict(policy_row)
            automation["active_discussion"] = (
                dict(discussion_row) if discussion_row is not None else None
            )

            member_status_clause = (
                "" if room_id == DEFAULT_ROOM_ID else " AND rm.status='active'"
            )
            member_rows = connection.execute(
                f"""SELECT rm.*, rp.client_name, rp.provider_id, rp.model_id,
                            rp.reasoning_mode, rp.route_class,
                            COALESCE(rmr.role_id, ?) AS role_id,
                            rmr.role_label, rmr.role_sha256,
                            CASE WHEN EXISTS(
                                SELECT 1 FROM agent_presence ap
                                WHERE ap.scope=rm.scope AND ap.agent_id=rm.agent_id
                                  AND ap.last_seen_epoch>=?
                            ) THEN 1 ELSE 0 END AS online
                    FROM room_memberships rm
                    LEFT JOIN route_profiles rp
                      ON rp.scope=rm.scope AND rp.route_id=rm.route_profile_id
                    LEFT JOIN room_member_roles rmr
                      ON rmr.scope=rm.scope AND rmr.room_id=rm.room_id
                     AND rmr.agent_id=rm.agent_id
                     AND rmr.room_session_id=rm.room_session_id
                    WHERE rm.scope=? AND rm.room_id=?{member_status_clause}
                    ORDER BY rm.status, rm.agent_id""",
                (DEFAULT_ROOM_ROLE, cutoff, scope, room_id),
            ).fetchall()
            members = tuple(
                {**dict(row), "online": bool(row["online"])} for row in member_rows
            )
            operator_active = room_id == DEFAULT_ROOM_ID or any(
                row["agent_id"] == consumer and row["status"] == "active"
                for row in member_rows
            )

            identity_rows = connection.execute(
                """SELECT agent_id FROM agent_presence WHERE scope=?
                   UNION SELECT agent_id FROM route_profiles WHERE scope=?
                   UNION SELECT agent_id FROM room_memberships WHERE scope=?
                   UNION SELECT sender AS agent_id FROM messages WHERE scope=?
                   UNION SELECT recipient AS agent_id FROM messages
                         WHERE scope=? AND recipient!='*'
                   ORDER BY agent_id""",
                (scope,) * 5,
            ).fetchall()
            catalog_agents: list[dict[str, Any]] = []
            for identity in identity_rows:
                agent_id = str(identity["agent_id"])
                sessions = tuple(
                    dict(row)
                    for row in connection.execute(
                        """SELECT session_id, transport, client_name, provider_id,
                                  model_id, reasoning_mode, route_class,
                                  last_seen_epoch, last_seen_utc
                           FROM agent_presence
                           WHERE scope=? AND agent_id=? AND last_seen_epoch>=?
                           ORDER BY last_seen_epoch DESC, session_id""",
                        (scope, agent_id, cutoff),
                    ).fetchall()
                )
                profiles = tuple(
                    {**dict(row), "enabled": bool(row["enabled"])}
                    for row in connection.execute(
                        """SELECT * FROM route_profiles
                           WHERE scope=? AND agent_id=? AND enabled=1
                           ORDER BY route_id""",
                        (scope, agent_id),
                    ).fetchall()
                )
                active_room_ids = tuple(
                    str(row["room_id"])
                    for row in connection.execute(
                        """SELECT room_id FROM room_memberships
                           WHERE scope=? AND agent_id=? AND status='active'
                           ORDER BY room_id""",
                        (scope, agent_id),
                    ).fetchall()
                )
                catalog_payload = {
                    "scope": scope,
                    "agent_id": agent_id,
                    "online_sessions": sessions,
                    "route_profiles": profiles,
                    "active_room_ids": active_room_ids,
                }
                catalog_agents.append(
                    {
                        **catalog_payload,
                        "online": bool(sessions),
                        "catalog_sha256": self._catalog_sha256(catalog_payload),
                    }
                )

            page_clause = " AND m.sequence<?" if before_sequence is not None else ""
            page_params: tuple[Any, ...] = (
                (consumer, scope, room_id, int(before_sequence), limit + 1)
                if before_sequence is not None
                else (consumer, scope, room_id, limit + 1)
            )
            raw_messages = connection.execute(
                """SELECT * FROM (
                       SELECT m.*,
                              CASE WHEN mr.message_id IS NULL THEN 0 ELSE 1 END
                                  AS acknowledged,
                              rr.route_status,
                              rr.observed_provider_id,
                              rr.observed_model_id,
                              rr.observed_reasoning_mode,
                              rr.observed_route_class,
                              rr.receipt_sha256 AS route_receipt_sha256
                       FROM messages m
                       LEFT JOIN message_receipts mr
                         ON mr.scope=m.scope AND mr.message_id=m.message_id
                        AND mr.agent_id=?
                       LEFT JOIN message_route_receipts rr
                         ON rr.scope=m.scope AND rr.message_id=m.message_id
                        AND rr.agent_id=m.recipient
                       WHERE m.scope=? AND m.room_id=?
                         {page_clause}
                       ORDER BY m.sequence DESC LIMIT ?
                   ) visible ORDER BY sequence ASC""".format(
                    page_clause=page_clause
                ),
                page_params,
            ).fetchall()
            has_older = len(raw_messages) > limit
            if has_older:
                # The nested query returns descending rows; after its outer
                # ascending order the extra oldest row is first.
                raw_messages = raw_messages[1:]
            parsed_messages: list[dict[str, Any]] = []
            for row in raw_messages:
                item = dict(row)
                item["artifact_paths"] = json.loads(item.pop("artifact_paths_json"))
                item["acknowledged"] = bool(item["acknowledged"])
                route_request = {
                    "route_profile_id": item.get("route_profile_id"),
                    "target_agent_id": item.get("recipient"),
                    "requested_provider_id": item.get("requested_provider_id"),
                    "requested_model_id": item.get("requested_model_id"),
                    "requested_reasoning_mode": item.get("requested_reasoning_mode"),
                    "requested_route_class": item.get("requested_route_class"),
                    "route_request_sha256": item.get("route_request_sha256"),
                }
                if not item.get("route_request_sha256") and not any(
                    route_request[key]
                    for key in route_request
                    if key not in {"target_agent_id", "route_request_sha256"}
                ):
                    route_request = None
                observed = {
                    "agent_id": item.get("recipient"),
                    "provider_id": item.get("observed_provider_id"),
                    "model_id": item.get("observed_model_id"),
                    "reasoning_mode": item.get("observed_reasoning_mode"),
                    "route_class": item.get("observed_route_class"),
                }
                item["route_request"] = route_request
                item["route_evaluation"] = {
                    "status": item.get("route_status")
                    or ("requested" if route_request else "not_requested"),
                    "request": route_request,
                    "observed": observed,
                    "mismatches": (),
                }
                parsed_messages.append(item)
            messages = tuple(parsed_messages)

        return {
            "rooms": rooms,
            "room_id": room_id,
            "members": members,
            "messages": messages,
            "page": {
                "before_sequence": before_sequence,
                "oldest_sequence": (
                    int(messages[0]["sequence"]) if messages else None
                ),
                "newest_sequence": (
                    int(messages[-1]["sequence"]) if messages else None
                ),
                "count": len(messages),
                "has_older": has_older,
                "has_newer": before_sequence is not None,
                "limit": limit,
            },
            "catalog_agents": tuple(catalog_agents),
            "automation": automation,
            "operator_active": operator_active,
            "read_error": None,
        }

    def self_test(self, *, scope: str | None = None) -> dict[str, Any]:
        snapshot = self.snapshot(limit=5, scope=scope)
        write_blocked = False
        try:
            with contextlib.closing(self.connect()) as connection:
                connection.execute("CREATE TABLE monitor_must_not_write(value TEXT)")
        except sqlite3.OperationalError:
            write_blocked = True
        return {
            "status": "PASS" if write_blocked else "FAIL",
            "database": str(self.db_path),
            "read_only_write_blocked": write_blocked,
            "tables": snapshot.table_counts,
            "signature": snapshot.signature(),
        }


MCP_HUMAN_CLIENT_TOOLS = (
    "bind_guided_discussion",
    "cancel_operation",
    "control_discussion",
    "create_execution_worktree",
    "create_room",
    "decide_permission",
    "enqueue_workflow",
    "export_proof_bundle",
    "grant_capability",
    "join_room",
    "leave_room",
    "list_agents",
    "list_provider_connections",
    "list_rooms",
    "poll_messages",
    "post_room_message",
    "register_capability",
    "room_members",
    "save_workflow_schedule",
    "seal_execution",
    "send_message",
    "send_room_fanout",
    "set_room_automation",
    "set_room_member_role",
    "set_workflow_schedule_enabled",
    "upsert_provider_connection",
    "upsert_route_profile",
    "verify_audit_chain",
    "verify_execution_source",
    "verify_proof_bundle",
)


class McpHumanClient:
    """Use the bridge's stdio MCP tool path for explicit human messages."""

    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        scope: str,
        *,
        agent_id: str = "human-operator",
        client_name: str = "mcp-pixel-monitor",
    ) -> None:
        self.project_root = project_root.resolve()
        self.db_path = db_path.resolve()
        self.scope = scope
        if not SAFE_ROUTE_ID.fullmatch(agent_id):
            raise ValueError("invalid human agent identity")
        if not SAFE_ROUTE_ID.fullmatch(client_name):
            raise ValueError("invalid human client identity")
        self.agent_id = agent_id
        self.client_name = client_name
        Bridge(
            self.project_root,
            self.db_path,
            self.agent_id,
            self.scope,
            client_name=self.client_name,
        )
        self.identity_capability = ensure_agent_identity_capability(
            self.project_root,
            self.db_path,
            self.scope,
            self.agent_id,
            allowed_tools=MCP_HUMAN_CLIENT_TOOLS,
            issued_by="peerbridge-control-room",
        )

    @staticmethod
    def _python_executable() -> str:
        current = Path(sys.executable)
        console = current.with_name("python.exe")
        return str(console if console.is_file() else current)

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
        command = [
            self._python_executable(),
            "-m",
            "peerbridge_mcp",
            "serve",
            "--project-root",
            str(self.project_root),
            "--db",
            str(self.db_path),
            "--agent-id",
            self.agent_id,
            "--identity-capability",
            str(self.identity_capability.path),
            "--scope",
            self.scope,
            "--allow-tool",
            name,
        ]
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": APP_VERSION},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        ]
        payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n"
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        completed = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creation_flags,
            check=False,
        )
        responses: list[dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                responses.append(item)
        response = next((item for item in responses if item.get("id") == 2), None)
        if response is None:
            raise RuntimeError(
                f"MCP {name} returned no tool response (exit {completed.returncode}); "
                "subprocess output was hidden."
            )
        if "error" in response:
            detail = redact_sensitive(response["error"].get("message", response["error"]))
            raise RuntimeError(clip(detail, 300))
        result_envelope = response.get("result", {})
        content = result_envelope.get("content", [])
        if not content or "text" not in content[0]:
            raise RuntimeError(f"MCP {name} returned malformed content")
        result_text = str(content[0]["text"])
        if result_envelope.get("isError"):
            try:
                error_result = json.loads(result_text)
            except json.JSONDecodeError:
                error_result = {"error": result_text}
            detail = (
                error_result.get("error", error_result)
                if isinstance(error_result, dict)
                else error_result
            )
            raise RuntimeError(clip(redact_sensitive(detail), 300))
        result = json.loads(result_text)
        if not isinstance(result, dict):
            raise RuntimeError(f"MCP {name} result is not an object")
        return result

    def create_room(self, *, room_id: str, name: str) -> dict[str, Any]:
        return self.call_tool("create_room", {"room_id": room_id, "name": name})

    def list_rooms(self) -> dict[str, Any]:
        return self.call_tool("list_rooms", {"include_archived": False})

    def list_agents(self) -> dict[str, Any]:
        return self.call_tool("list_agents", {"include_disabled_routes": False})

    def join_room(
        self,
        *,
        room_id: str,
        agent_id: str,
        route_profile_id: str | None = None,
        role_id: str | None = None,
        role_label: str | None = None,
    ) -> dict[str, Any]:
        payload = {"room_id": room_id, "agent_id": agent_id}
        if route_profile_id:
            payload["route_profile_id"] = route_profile_id
        if role_id:
            payload["role_id"] = role_id
            if role_label:
                payload["role_label"] = role_label
        return self.call_tool("join_room", payload)

    def set_room_member_role(
        self,
        *,
        room_id: str,
        agent_id: str,
        role_id: str,
        role_label: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "room_id": room_id,
            "agent_id": agent_id,
            "role_id": role_id,
        }
        if role_label:
            payload["role_label"] = role_label
        return self.call_tool("set_room_member_role", payload)

    def leave_room(self, *, room_id: str, agent_id: str) -> dict[str, Any]:
        return self.call_tool("leave_room", {"room_id": room_id, "agent_id": agent_id})

    def room_members(
        self, *, room_id: str, include_inactive: bool = False
    ) -> dict[str, Any]:
        return self.call_tool(
            "room_members",
            {"room_id": room_id, "include_inactive": include_inactive},
        )

    def poll_messages(
        self,
        *,
        room_id: str,
        after_cursor: int = 0,
        limit: int = 500,
        include_sent: bool = True,
    ) -> dict[str, Any]:
        return self.call_tool(
            "poll_messages",
            {
                "room_id": room_id,
                "agent_id": self.agent_id,
                "after_cursor": after_cursor,
                "limit": limit,
                "include_sent": include_sent,
            },
        )

    def send_message(
        self,
        *,
        room_id: str = DEFAULT_ROOM_ID,
        recipient: str,
        task_id: str,
        subject: str,
        body: str,
        priority: str,
        route_profile_id: str | None = None,
        requested_provider_id: str | None = None,
        requested_model_id: str | None = None,
        requested_reasoning_mode: str | None = None,
        artifact_paths: Iterable[str] = (),
    ) -> dict[str, Any]:
        clean_body = body.strip()
        if contains_secret(clean_body):
            raise ValueError("MESSAGE_SECRET_REJECTED")
        if len(clean_body) > 20_000:
            raise ValueError("MESSAGE_BODY_TOO_LONG")
        payload = {
            "room_id": room_id,
            "recipient": recipient,
            "task_id": task_id,
            "subject": subject,
            "body": clean_body,
            "priority": priority,
            "artifact_paths": list(artifact_paths),
            "idempotency_key": uuid.uuid4().hex,
        }
        for key, value in (
            ("route_profile_id", route_profile_id),
            ("requested_provider_id", requested_provider_id),
            ("requested_model_id", requested_model_id),
            ("requested_reasoning_mode", requested_reasoning_mode),
        ):
            if value:
                payload[key] = value
        return self.call_tool("send_message", payload)

    def send_room_fanout(
        self,
        *,
        room_id: str,
        task_id: str,
        subject: str,
        body: str,
        priority: str,
        artifact_paths: Iterable[str] = (),
    ) -> dict[str, Any]:
        clean_body = body.strip()
        if contains_secret(clean_body):
            raise ValueError("MESSAGE_SECRET_REJECTED")
        if len(clean_body) > 20_000:
            raise ValueError("MESSAGE_BODY_TOO_LONG")
        return self.call_tool(
            "send_room_fanout",
            {
                "room_id": room_id,
                "task_id": task_id,
                "subject": subject,
                "body": clean_body,
                "priority": priority,
                "artifact_paths": list(artifact_paths),
                "idempotency_key": uuid.uuid4().hex,
            },
        )

    def post_room_message(
        self,
        *,
        room_id: str,
        task_id: str,
        subject: str,
        body: str,
        priority: str,
        artifact_paths: Iterable[str] = (),
    ) -> dict[str, Any]:
        clean_body = body.strip()
        if contains_secret(clean_body):
            raise ValueError("MESSAGE_SECRET_REJECTED")
        if len(clean_body) > 20_000:
            raise ValueError("MESSAGE_BODY_TOO_LONG")
        return self.call_tool(
            "post_room_message",
            {
                "room_id": room_id,
                "task_id": task_id,
                "subject": subject,
                "body": clean_body,
                "priority": priority,
                "artifact_paths": list(artifact_paths),
                "idempotency_key": uuid.uuid4().hex,
            },
        )

    def set_room_automation(
        self,
        *,
        room_id: str,
        mode: str,
        max_rounds: int,
        max_messages: int,
        stagnation_rounds: int,
    ) -> dict[str, Any]:
        return self.call_tool(
            "set_room_automation",
            {
                "room_id": room_id,
                "mode": mode,
                "max_rounds": max_rounds,
                "max_messages": max_messages,
                "stagnation_rounds": stagnation_rounds,
            },
        )

    def control_discussion(
        self,
        *,
        discussion_id: str,
        action: str,
        extra_rounds: int = 2,
    ) -> dict[str, Any]:
        return self.call_tool(
            "control_discussion",
            {
                "discussion_id": discussion_id,
                "action": action,
                "extra_rounds": extra_rounds,
            },
        )

    def enqueue_workflow(
        self,
        *,
        operation_id: str,
        workflow_id: str,
        task_text: str,
        working_directory: str,
        resource_key: str,
        max_attempts: int,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        return self.call_tool(
            "enqueue_workflow",
            {
                "operation_id": operation_id,
                "workflow_id": workflow_id,
                "task_text": task_text,
                "working_directory": working_directory,
                "resource_key": resource_key,
                "max_attempts": max_attempts,
                "timeout_seconds": timeout_seconds,
            },
        )

    def cancel_operation(
        self, *, operation_id: str, reason: str
    ) -> dict[str, Any]:
        return self.call_tool(
            "cancel_operation",
            {"operation_id": operation_id, "reason": reason},
        )

    def bind_guided_discussion(
        self, *, operation_id: str, discussion_id: str
    ) -> dict[str, Any]:
        return self.call_tool(
            "bind_guided_discussion",
            {
                "operation_id": operation_id,
                "discussion_id": discussion_id,
            },
        )

    def self_test(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="mcp_pixel_send_test_") as temp:
            test_root = Path(temp)
            test_db = test_root / "collab-test.sqlite3"
            client = McpHumanClient(test_root, test_db, self.scope)
            receipt = client.send_message(
                recipient="*",
                task_id="mcp-pixel-send-self-test",
                subject="SELF TEST",
                body="append-only MCP message test",
                priority="low",
            )
            connection = sqlite3.connect(test_db)
            try:
                message_count = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
                event_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            finally:
                connection.close()
        passed = message_count == 1 and event_count >= 1 and bool(receipt.get("content_sha256"))
        return {
            "status": "PASS" if passed else "FAIL",
            "message_count": message_count,
            "event_count": event_count,
            "receipt_sha256_present": bool(receipt.get("content_sha256")),
            "production_database_writes": 0,
        }


class DetailTree(tk.Frame):
    def __init__(self, parent: tk.Misc, columns: list[tuple[str, str, int]]) -> None:
        super().__init__(parent, bg=COLORS["panel"])
        self.records: dict[str, dict[str, Any]] = {}
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for key, heading, width in columns:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, minwidth=70, stretch=True)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        detail_wrap = tk.Frame(self, bg=COLORS["black"], bd=2, relief="sunken")
        detail_wrap.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        self.detail = tk.Text(
            detail_wrap,
            height=9,
            wrap="word",
            bg=COLORS["black"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="flat",
            padx=10,
            pady=8,
            font=("Cascadia Mono", 9),
        )
        detail_scroll = ttk.Scrollbar(detail_wrap, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self.detail.configure(state="disabled")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected)
        self.grid_rowconfigure(0, weight=3)
        self.grid_rowconfigure(1, weight=2)
        self.grid_columnconfigure(0, weight=1)

    def replace(self, rows: Iterable[tuple[str, tuple[Any, ...], dict[str, Any]]]) -> None:
        selected = self.tree.selection()
        selected_id = selected[0] if selected else None
        self.tree.delete(*self.tree.get_children())
        self.records.clear()
        for row_id, values, record in rows:
            safe_id = row_id or hashlib.sha256(repr(values).encode("utf-8")).hexdigest()[:16]
            self.tree.insert("", "end", iid=safe_id, values=values)
            self.records[safe_id] = record
        if selected_id and selected_id in self.records:
            self.tree.selection_set(selected_id)
            self.tree.see(selected_id)
        elif self.tree.get_children():
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self._show_selected()

    def _show_selected(self, _event: Any = None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        record = self.records.get(selected[0], {})
        rendered = json.dumps(record, ensure_ascii=False, indent=2)
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", rendered)
        self.detail.configure(state="disabled")


class PixelMonitor:
    REFRESH_MS = 2000

    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        scope: str,
        refresh_ms: int = 1500,
        *,
        ui_scale_factor: float | None = None,
        theme: str | None = None,
        locale: str | None = None,
        hidden_self_test: bool = False,
    ) -> None:
        self.project_root = project_root.resolve()
        self.scope = scope
        self.ui_scale_factor = ui_scale_factor
        self.REFRESH_MS = max(500, min(int(refresh_ms), 10000))
        # Run the append-only schema migration before the read-only monitor opens.
        Bridge(self.project_root, db_path, "control-room-migrator", scope)
        self.reader = BridgeReader(db_path, self.project_root)
        self.human_client = McpHumanClient(self.project_root, db_path, scope)
        control_room_bridge = Bridge(
            self.project_root, db_path, HUMAN_AGENT_ID, scope
        )
        self.release_gate_service = ReleaseGateService.for_control_room_ui(
            control_room_bridge
        )
        self.authorized_sessions = AuthorizedSessionRegistry(control_room_bridge)
        try:
            ui_preferences = load_preferences(self.project_root)
            self._preferences_load_error: str | None = None
        except LocalizationError as exc:
            ui_preferences = default_preferences()
            self._preferences_load_error = str(exc)
        selected_theme = theme or str(ui_preferences["theme"])
        selected_locale = locale or str(ui_preferences["locale"])
        if selected_locale not in SUPPORTED_LOCALES:
            raise LocalizationError("unsupported UI locale")
        apply_color_palette(selected_theme)
        try:
            announcement_preferences = load_announcement_preferences(self.project_root)
            self._announcement_preferences_load_error: str | None = None
        except AnnouncementError as exc:
            announcement_preferences = fail_closed_announcement_preferences()
            self._announcement_preferences_load_error = str(exc)
        configure_windows_app_identity()
        self.root = tk.Tk()
        self._hidden_self_test = hidden_self_test
        if hidden_self_test:
            # Tk can map the root during constructor-time layout updates. Hide it
            # immediately so release verification never flashes or steals focus.
            self.root.withdraw()
            with contextlib.suppress(tk.TclError):
                self.root.attributes("-alpha", 0.0)
        if ui_scale_factor is not None:
            self.root.tk.call(
                "tk", "scaling", tk_scaling_for_windows_factor(ui_scale_factor)
            )
        self.root.title(WINDOW_TITLE)
        self.root.geometry("1440x900" if selected_theme == "modern" else "1320x820")
        self.root.minsize(980, 650)
        self.root.configure(bg=COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._app_icon: tk.PhotoImage | None = None
        self._sidebar_brand_icon: tk.PhotoImage | None = None
        self._windows_icon_handles: tuple[int, ...] = ()
        self._window_icon_after_id: str | None = None
        self._install_window_icon()
        self.chat_focus_mode = False
        self.root.bind("<F11>", self._toggle_chat_focus_event)
        self.root.bind("<Escape>", self._exit_chat_focus)
        self.last_signature = ""
        self.snapshot: Snapshot | None = None
        self._last_database_token: tuple[tuple[str, int, int], ...] | None = None
        self._last_full_refresh = 0.0
        self._refresh_after_id: str | None = None
        self._ui_pump_after_id: str | None = None
        self._ui_callbacks: queue.SimpleQueue[
            tuple[int, Callable[..., None], tuple[Any, ...]]
        ] = queue.SimpleQueue()
        self._ui_generation = 0
        self._closing = False
        self._trust_action_in_progress = False
        self._last_agent_canvas_signature = ""
        self._last_room_seats_signature = ""
        self._last_room_agent_cards_signature = ""
        self._room_agent_card_capacity = -1
        self._chat_render_query = ""
        self._chat_render_room_id = ""
        self._chat_render_row_signatures: tuple[str, ...] | None = None
        self._chat_follow_latest_on_open = False
        self._chat_layout_history_width = 0
        self._modern_workspace_positioned_width = 0
        self.modern_toolbar_options_visible = False
        self.modern_more_nav_visible = False
        self.modern_room_settings_visible = False
        self.modern_inspector_user_override: bool | None = None
        self.modern_inspector_is_visible = True
        self.modern_composer_advanced = False
        self.modern_agent_editor_visible = False
        self.modern_agent_library_visible = False
        self.modern_recent_room_buttons: list[tk.Widget] = []
        self.paused = tk.BooleanVar(value=False)
        self.search = tk.StringVar(value="")
        self.locale = tk.StringVar(value=selected_locale)
        self.locale_label = tk.StringVar(value=LOCALE_LABELS[self.locale.get()])
        self.theme = tk.StringVar(value=selected_theme)
        self.theme_choice = tk.StringVar(
            value=THEME_LABELS[self.locale.get()][selected_theme]
        )
        self.refresh_status = tk.StringVar(value="")
        self._last_successful_refresh: datetime | None = None
        self.tutorial_completed = bool(ui_preferences["tutorial_completed"])
        self.update_status = tk.StringVar(value="")
        self.update_in_progress = False
        self.announcement_network_enabled = tk.BooleanVar(
            value=bool(announcement_preferences["network_enabled"])
        )
        self.announcement_popup_enabled = tk.BooleanVar(
            value=bool(announcement_preferences["popup_enabled"])
        )
        self.announcement_status = tk.StringVar(value="")
        self.announcement_in_progress = False
        self._announcements: dict[str, Announcement] = {}
        self._announcement_read_ids = set(announcement_preferences["read_ids"])
        self._announcement_cursors = dict(announcement_preferences["cursors"])
        self._announcement_after_id: str | None = None
        self._announcement_window: tk.Toplevel | None = None
        self._pending_announcement_popup_rows: tuple[Announcement, ...] = ()
        self._feedback_reflow_after_id: str | None = None
        self._tutorial_window: tk.Toplevel | None = None
        self._tutorial_select_page: Callable[[str], None] | None = None
        # Empty means initial render should select the first real room agent.
        # Once a human explicitly selects broadcast, refreshes preserve it.
        self.message_recipient = tk.StringVar(value="")
        self.message_priority = tk.StringVar(value="normal")
        self.message_priority_label = tk.StringVar(value="")
        self.message_route_profile = tk.StringVar(value="DIRECT")
        self.message_provider_choice = tk.StringVar(
            value=translate(self.locale.get(), DIRECT_LABEL)
        )
        self.message_provider = tk.StringVar(value="")
        self.message_model = tk.StringVar(value="")
        self.message_reasoning = tk.StringVar(value="")
        self.message_task = tk.StringVar(value=f"human-chat-{datetime.now().strftime('%Y%m%d')}")
        self.message_subject = tk.StringVar(
            value=translate(self.locale.get(), "chat.default_subject")
        )
        self.message_status = tk.StringVar(
            value=translate(self.locale.get(), "chat.message_hint")
        )
        self.chat_attachment_status = tk.StringVar(
            value=translate(self.locale.get(), "chat.no_attachments")
        )
        self._chat_attachment_paths: tuple[Path, ...] = ()
        self.connection_id = tk.StringVar(value="")
        self.connection_name = tk.StringVar(value="")
        self.connection_class = tk.StringVar(value="relay")
        self.connection_endpoint = tk.StringVar(value="")
        self.connection_api_key = tk.StringVar(value="")
        self.connection_key_visible = tk.BooleanVar(value=False)
        self.connection_agent = tk.StringVar(value="")
        self.connection_client = tk.StringVar(value="")
        self.connection_route_id = tk.StringVar(value="")
        self.connection_model = tk.StringVar(value="")
        self.connection_response_model = tk.StringVar(value="")
        self.connection_timeout_seconds = tk.StringVar(value="60")
        self.connection_reasoning = tk.StringVar(value="")
        self.connection_status = tk.StringVar(
            value=translate(self.locale.get(), "connection.status.initial")
        )
        self.connection_in_progress = False
        self.agent_install_status = {
            spec.agent_id: tk.StringVar(
                value=translate(self.locale.get(), "agent_install.detecting")
            )
            for spec in installable_agent_specs()
        }
        self._agent_install_statuses: dict[str, AgentInstallStatus] = {}
        self._agent_install_buttons: dict[str, tk.Button] = {}
        self._agent_install_docs_buttons: dict[str, tk.Button] = {}
        self._agent_install_processes: dict[str, subprocess.Popen[bytes]] = {}
        self._agent_detection_in_progress = False
        self.feedback_contact = tk.StringVar(value="")
        self.feedback_include_key = tk.BooleanVar(value=False)
        self.feedback_key = tk.StringVar(value="")
        self.feedback_status = tk.StringVar(
            value=translate(self.locale.get(), "feedback.status.initial")
        )
        self.feedback_attachment_status = tk.StringVar(
            value=translate(self.locale.get(), "feedback.attachment.none")
        )
        self.feedback_in_progress = False
        self._feedback_attachment_paths: tuple[Path, ...] = ()
        self._last_feedback_bundle: FeedbackBundle | None = None
        self.ccswitch_app = tk.StringVar(value="codex")
        self.ccswitch_class = tk.StringVar(value="relay")
        self.ccswitch_provider = tk.StringVar(value="")
        self.ccswitch_model = tk.StringVar(value="")
        self.ccswitch_agent = tk.StringVar(value="codex-main")
        self.ccswitch_reasoning = tk.StringVar(value="")
        self.ccswitch_status = tk.StringVar(
            value=translate(self.locale.get(), "ccswitch.status.initial")
        )
        self._ccswitch_providers: dict[str, CcSwitchProvider] = {}
        self.room_choice = tk.StringVar(value="")
        self.room_status = tk.StringVar(
            value=translate(self.locale.get(), "chat.rooms_loading")
        )
        self.room_automation_choice = tk.StringVar(
            value=translate(self.locale.get(), AUTOMATION_MODE_TO_KEY["once"])
        )
        self.room_round_limit = tk.StringVar(value="4")
        self.room_message_limit = tk.StringVar(value="40")
        self.room_stagnation_limit = tk.StringVar(value="2")
        self.discussion_status = tk.StringVar(
            value=translate(self.locale.get(), "chat.discussion.idle")
        )
        self.guided_workflow_status = tk.StringVar(value="")
        self._active_discussion: dict[str, Any] | None = None
        self.seat_agent = tk.StringVar(value="")
        self.seat_provider_choice = tk.StringVar(value="")
        self.seat_model_choice = tk.StringVar(value="")
        self.seat_reasoning_choice = tk.StringVar(value="")
        self.seat_role_choice = tk.StringVar(value="")
        self.seat_custom_role = tk.StringVar(value="")
        self._seat_role_labels: dict[str, str] = {}
        self.seat_status = tk.StringVar(
            value=translate(self.locale.get(), "chat.seat_hint")
        )
        self.library_selection = tk.StringVar(
            value=translate(self.locale.get(), "sidebar.library_none")
        )
        self.selected_room_id = DEFAULT_ROOM_ID
        self._rooms: dict[str, dict[str, Any]] = {}
        self._room_ids_by_label: dict[str, str] = {}
        self._room_members: tuple[dict[str, Any], ...] = ()
        self._room_view_unavailable = False
        self._room_messages: tuple[dict[str, Any], ...] = ()
        self._room_history_stack: list[int | None] = [None]
        self._room_page_has_older = False
        self._library_agents: tuple[dict[str, Any], ...] = ()
        self._catalog_agents: tuple[dict[str, Any], ...] = ()
        self._library_hitboxes: list[tuple[int, int, int, int, str]] = []
        self._drag_agent_id: str | None = None
        self._drag_action: str | None = None
        self._drag_origin: tuple[int, int] | None = None
        self._drag_ghost: tk.Toplevel | None = None
        self._seat_profiles: tuple[dict[str, Any], ...] = ()
        self._seat_provider_ids: dict[str, str] = {}
        self._seat_reasoning_values: dict[str, str | None] = {}
        self._seat_selected_route_id: str | None = None
        self._seat_selected_candidate: dict[str, Any] | None = None
        self._provider_model_catalog: dict[str, tuple[str, ...]] = {}
        self._provider_model_registry_sha256: dict[str, str] = {}
        self._provider_model_catalog_version: dict[str, str] = {}
        self._provider_discovery_inflight: set[str] = set()
        self._provider_discovery_retry_at: dict[str, float] = {}
        self._provider_discovery_errors: dict[str, str] = {}
        self._codex_model_catalog: CodexModelCatalog | None = None
        self._codex_catalog_inflight = False
        self._codex_catalog_retry_at = 0.0
        self._codex_catalog_error: str | None = None
        self._selected_room_seat_agent: str | None = None
        self.room_refresh_in_progress = False
        self._room_refresh_pending = False
        self.room_action_in_progress = False
        self._last_room_refresh = 0.0
        self._last_room_view_signature = ""
        self._recipient_ids: dict[str, str] = {
            translate(self.locale.get(), BROADCAST_LABEL): "*"
        }
        self.send_in_progress = False
        self._send_token_sequence = 0
        self._active_send_token: int | None = None
        self.active_page = "cockpit"
        self.nav_buttons: dict[str, tk.Button] = {}
        self.pages: dict[str, tk.Frame] = {}
        self._localized_label_widgets: dict[str, list[tk.Label]] = {}
        self._configure_styles()
        self._install_text_editing_bindings()
        self._build_layout()
        workflow_bridge = Bridge(
            self.project_root,
            db_path,
            CONTROL_ROOM_WORKFLOW_ID,
            scope,
        )
        self.workflow_runner = ManagedWorkflowRunner(
            workflow_bridge,
            self.cockpit.manager,
        )
        self.room_discussion_tracker = RoomDiscussionTracker(workflow_bridge)
        self.verification_engine = VerificationTriggerEngine(workflow_bridge)
        self.trust_workflows.set_verification_callbacks(
            status_provider=self.verification_engine.status,
            request_scan=self.verification_engine.request_scan,
        )
        self._apply_locale(save=False)
        self.search.trace_add("write", lambda *_: self.render(force=True))
        self._schedule_ui_pump()
        self.verification_engine.start()
        self.room_discussion_tracker.start()
        self.workflow_runner.start()
        self.refresh(force=True)
        self._schedule_announcement_check(1500)
        self.root.after(900, self.refresh_official_agent_statuses)
        if not self.tutorial_completed:
            self.root.after(350, self.show_tutorial)

    def _t(self, key: str) -> str:
        locale = getattr(self, "locale", None)
        return translate(locale.get() if locale is not None else "zh-Hant", key)

    def _navigation_label(self, key: str) -> str:
        """Keep numbered Pixel navigation while Modern reads like a workspace."""
        text = self._t(f"nav.{key}")
        if ACTIVE_THEME == "modern":
            prefix, separator, remainder = text.partition("  ")
            if separator and prefix.strip().isdigit():
                text = remainder.lstrip()
            icon = MODERN_NAV_ICONS.get(key, "·")
            return f"{icon}   {text}"
        return text

    def _localized_label(
        self,
        parent: tk.Misc,
        key: str,
        **kwargs: Any,
    ) -> tk.Label:
        label = tk.Label(parent, text=self._t(key), **kwargs)
        self._localized_label_widgets.setdefault(key, []).append(label)
        return label

    def _discussion_status_text(self, status: Any) -> str:
        key = DISCUSSION_STATUS_TO_KEY.get(
            str(status or "").strip().lower(),
            "chat.discussion.status.unknown",
        )
        return self._t(key)

    def _room_state_text(self, state: Any) -> str:
        key = {
            "online": "chat.state.online",
            "offline": "chat.state.offline",
            "control": "chat.state.control",
            "active": "chat.state.active",
            "unrouted": "chat.state.unrouted",
        }.get(str(state or "").strip().lower())
        return self._t(key) if key else str(state or "--")

    @staticmethod
    def _text_widget_is_editable(widget: Any) -> bool:
        try:
            state = str(widget.cget("state"))
        except (AttributeError, tk.TclError):
            state = "normal"
        if state in {"disabled", "readonly"}:
            return False
        try:
            ttk_states = set(widget.state())
        except (AttributeError, tk.TclError, TypeError):
            ttk_states = set()
        return not bool({"disabled", "readonly"} & ttk_states)

    @staticmethod
    def _text_widget_selection(widget: Any) -> tuple[Any, Any] | None:
        try:
            if str(widget.winfo_class()) == "Text":
                ranges = widget.tag_ranges("sel")
                if len(ranges) >= 2:
                    return ranges[0], ranges[1]
                return None
            return widget.index("sel.first"), widget.index("sel.last")
        except (AttributeError, tk.TclError, ValueError):
            return None

    def _copy_text_widget_selection(self, widget: Any) -> bool:
        selection = self._text_widget_selection(widget)
        if selection is None:
            return False
        try:
            if str(widget.winfo_class()) == "Text":
                value = widget.get(selection[0], selection[1])
            else:
                raw = widget.get()
                value = raw[int(selection[0]) : int(selection[1])]
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
        except (AttributeError, tk.TclError, TypeError, ValueError):
            return False
        return True

    def _cut_text_widget_selection(self, widget: Any) -> bool:
        if not self._text_widget_is_editable(widget):
            return False
        selection = self._text_widget_selection(widget)
        if selection is None or not self._copy_text_widget_selection(widget):
            return False
        try:
            widget.delete(selection[0], selection[1])
        except (AttributeError, tk.TclError):
            return False
        return True

    def _paste_into_text_widget(self, widget: Any) -> bool:
        if not self._text_widget_is_editable(widget):
            return False
        try:
            value = self.root.clipboard_get()
        except (AttributeError, tk.TclError):
            return False
        selection = self._text_widget_selection(widget)
        try:
            if selection is not None:
                widget.delete(selection[0], selection[1])
            widget.insert("insert", value)
        except (AttributeError, tk.TclError):
            return False
        return True

    @staticmethod
    def _select_all_text_widget(widget: Any) -> bool:
        try:
            if str(widget.winfo_class()) == "Text":
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "end-1c")
                widget.see("insert")
            else:
                widget.selection_range(0, "end")
                widget.icursor("end")
        except (AttributeError, tk.TclError):
            return False
        return True

    @staticmethod
    def _shortcut(handler: Callable[[Any], Any]) -> Callable[[Any], str]:
        def invoke(event: Any) -> str:
            handler(event.widget)
            return "break"

        return invoke

    def _show_text_context_menu(self, event: Any) -> str:
        widget = event.widget
        try:
            widget.focus_set()
        except (AttributeError, tk.TclError):
            return "break"
        selection = self._text_widget_selection(widget)
        editable = self._text_widget_is_editable(widget)
        try:
            self.root.clipboard_get()
            can_paste = editable
        except tk.TclError:
            can_paste = False
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(
            label=self._t("edit.cut"),
            command=lambda: self._cut_text_widget_selection(widget),
            state="normal" if editable and selection is not None else "disabled",
        )
        menu.add_command(
            label=self._t("edit.copy"),
            command=lambda: self._copy_text_widget_selection(widget),
            state="normal" if selection is not None else "disabled",
        )
        menu.add_command(
            label=self._t("edit.paste"),
            command=lambda: self._paste_into_text_widget(widget),
            state="normal" if can_paste else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label=self._t("edit.select_all"),
            command=lambda: self._select_all_text_widget(widget),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _install_text_editing_bindings(self) -> None:
        bindings = {
            "<Control-a>": self._shortcut(self._select_all_text_widget),
            "<Control-A>": self._shortcut(self._select_all_text_widget),
            "<Control-c>": self._shortcut(self._copy_text_widget_selection),
            "<Control-C>": self._shortcut(self._copy_text_widget_selection),
            "<Control-v>": self._shortcut(self._paste_into_text_widget),
            "<Control-V>": self._shortcut(self._paste_into_text_widget),
            "<Control-x>": self._shortcut(self._cut_text_widget_selection),
            "<Control-X>": self._shortcut(self._cut_text_widget_selection),
            "<Control-Insert>": self._shortcut(self._copy_text_widget_selection),
            "<Shift-Insert>": self._shortcut(self._paste_into_text_widget),
            "<Shift-Delete>": self._shortcut(self._cut_text_widget_selection),
            "<Button-3>": self._show_text_context_menu,
        }
        for widget_class in ("Entry", "TEntry", "Text", "TCombobox"):
            for sequence, handler in bindings.items():
                self.root.bind_class(widget_class, sequence, handler)

    def _place_transient(self, window: tk.Toplevel, width: int, height: int) -> None:
        self.root.update_idletasks()
        window.geometry(
            centered_transient_geometry(
                parent_x=self.root.winfo_rootx(),
                parent_y=self.root.winfo_rooty(),
                parent_width=self.root.winfo_width(),
                parent_height=self.root.winfo_height(),
                width=width,
                height=height,
            )
        )

    def _tutorial_is_open(self) -> bool:
        try:
            return bool(
                self._tutorial_window is not None
                and self._tutorial_window.winfo_exists()
            )
        except tk.TclError:
            return False

    def _library_selection_text(self, agent_id: str | None = None) -> str:
        if agent_id:
            return self._t("sidebar.library_selected").format(agent=agent_id)
        return self._t("sidebar.library_none")

    def _sidebar_stat_labels(self) -> dict[str, str]:
        return {
            key: self._t(f"sidebar.{key}")
            for key in (
                "online",
                "rooms",
                "messages",
                "memory",
                "dispatch",
                "open_calls",
                "active",
                "audit",
                "sync",
            )
        }

    def _install_window_icon(self) -> None:
        png_path, ico_path = packaged_icon_paths()
        if sys.platform == "win32" and ico_path.is_file():
            try:
                self.root.iconbitmap(default=str(ico_path))
            except (OSError, tk.TclError):
                pass
        if png_path.is_file():
            try:
                self._app_icon = tk.PhotoImage(file=str(png_path))
                longest_edge = max(self._app_icon.width(), self._app_icon.height())
                subsample = max(1, (longest_edge + 27) // 28)
                self._sidebar_brand_icon = self._app_icon.subsample(
                    subsample, subsample
                )
                self.root.iconphoto(True, self._app_icon)
            except tk.TclError:
                self._app_icon = None
                self._sidebar_brand_icon = None
        self._windows_icon_handles = apply_windows_window_icon(self.root, ico_path)
        if sys.platform == "win32":
            # Tk replaces its native top-level during initial mapping. Reapply the
            # application identity and icon after that final HWND exists.
            self._window_icon_after_id = self.root.after(
                250, self._reapply_mapped_window_icon
            )

    def _reapply_mapped_window_icon(self) -> None:
        self._window_icon_after_id = None
        if self._closing:
            return
        configure_windows_app_identity()
        _png_path, ico_path = packaged_icon_paths()
        replacement_handles = apply_windows_window_icon(self.root, ico_path)
        if not replacement_handles:
            return
        previous_handles = self._windows_icon_handles
        self._windows_icon_handles = replacement_handles
        release_windows_icon_handles(previous_handles)

    def _toggle_chat_focus_event(self, _event: Any = None) -> str:
        self._set_chat_focus(not self.chat_focus_mode)
        return "break"

    def _exit_chat_focus(self, _event: Any = None) -> str:
        if self.chat_focus_mode:
            self._set_chat_focus(False)
        return "break"

    def _set_chat_focus(self, enabled: bool) -> None:
        self.chat_focus_mode = bool(enabled)
        if self.chat_focus_mode:
            self.show_page("chat")
            self.sidebar_frame.grid_remove()
            self.toolbar_frame.grid_remove()
            modern_inspector = getattr(self, "modern_chat_inspector", None)
            modern_workspace = getattr(self, "modern_chat_workspace", None)
            if modern_inspector is not None and modern_workspace is not None:
                with contextlib.suppress(tk.TclError):
                    modern_workspace.forget(modern_inspector)
            else:
                self.room_seats_frame.grid_remove()
            self.page_host.grid_configure(padx=0, pady=0)
        else:
            self.sidebar_frame.grid()
            self.toolbar_frame.grid()
            modern_inspector = getattr(self, "modern_chat_inspector", None)
            modern_workspace = getattr(self, "modern_chat_workspace", None)
            if modern_inspector is not None and modern_workspace is not None:
                with contextlib.suppress(tk.TclError):
                    if str(modern_inspector) not in modern_workspace.panes():
                        modern_workspace.add(
                            modern_inspector, minsize=320, stretch="never"
                        )
            else:
                self.room_seats_frame.grid()
            self.page_host.grid_configure(padx=12, pady=(0, 12))
        try:
            self.root.attributes("-fullscreen", self.chat_focus_mode)
        except tk.TclError:
            pass
        self.chat_focus_button.configure(
            text=self._t(
                "chat.exit_focus" if self.chat_focus_mode else "chat.focus"
            )
        )
        self.root.after_idle(self._layout_chat_after_resize)
        self.root.after(100, self._layout_chat_after_resize)

    def _show_modern_inspector(self, key: str) -> None:
        if ACTIVE_THEME != "modern" or key not in MODERN_INSPECTOR_KEYS:
            return
        frames = getattr(self, "modern_inspector_frames", {})
        frame = frames.get(key)
        if frame is None:
            return
        frame.tkraise()
        self.modern_inspector_active = key
        for candidate, button in self.modern_inspector_buttons.items():
            selected = candidate == key
            button.configure(
                bg=MODERN_NAV_ACTIVE_BG if selected else COLORS["panel"],
                fg=COLORS["text"] if selected else COLORS["muted"],
            )

    def _modern_inspector_present(self) -> bool:
        workspace = getattr(self, "modern_chat_workspace", None)
        inspector = getattr(self, "modern_chat_inspector", None)
        if workspace is None or inspector is None:
            return False
        try:
            return str(inspector) in {str(pane) for pane in workspace.panes()}
        except tk.TclError:
            return False

    def _sync_modern_inspector_visibility(self, workspace_width: int | None = None) -> None:
        if ACTIVE_THEME != "modern":
            return
        workspace = getattr(self, "modern_chat_workspace", None)
        inspector = getattr(self, "modern_chat_inspector", None)
        button = getattr(self, "modern_inspector_toggle_button", None)
        if workspace is None or inspector is None:
            return
        try:
            width = max(1, int(workspace_width or workspace.winfo_width()))
            desired = (
                self.modern_inspector_user_override
                if self.modern_inspector_user_override is not None
                else width >= MODERN_INSPECTOR_BREAKPOINT
            )
            present = self._modern_inspector_present()
            if desired and not present:
                workspace.add(
                    inspector,
                    minsize=268,
                    width=MODERN_INSPECTOR_WIDTH,
                    stretch="never",
                )
            elif not desired and present:
                workspace.forget(inspector)
            self.modern_inspector_is_visible = bool(desired)
            if button is not None:
                button.configure(
                    text=self._t(
                        "modern.room.hide_context"
                        if desired
                        else "modern.room.show_context"
                    )
                )
        except (tk.TclError, TypeError, ValueError):
            return

    def _toggle_modern_inspector(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        self.modern_inspector_user_override = not self._modern_inspector_present()
        self._sync_modern_inspector_visibility()
        self.root.after_idle(self._layout_chat_after_resize)

    def _toggle_modern_toolbar_options(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        self.modern_toolbar_options_visible = not self.modern_toolbar_options_visible
        for widget in getattr(self, "modern_toolbar_option_widgets", ()):
            with contextlib.suppress(tk.TclError):
                if self.modern_toolbar_options_visible:
                    widget.grid()
                else:
                    widget.grid_remove()
        button = getattr(self, "modern_toolbar_options_button", None)
        if button is not None:
            button.configure(
                text=(
                    self._t("modern.toolbar.close_options")
                    if self.modern_toolbar_options_visible
                    else self._t("modern.toolbar.options")
                )
            )

    def _toggle_modern_agent_library(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        self.modern_agent_library_visible = not self.modern_agent_library_visible
        panel = getattr(self, "modern_agent_library_panel", None)
        button = getattr(self, "modern_agent_library_button", None)
        if panel is not None:
            if self.modern_agent_library_visible:
                panel.pack(fill="x", pady=(2, 8))
            else:
                panel.pack_forget()
        if button is not None:
            button.configure(
                text=(
                    self._t("modern.sidebar.hide_agents")
                    if self.modern_agent_library_visible
                    else self._t("modern.sidebar.show_agents")
                )
            )

    def _toggle_modern_more_nav(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        self.modern_more_nav_visible = not self.modern_more_nav_visible
        frame = getattr(self, "modern_more_nav_frame", None)
        button = getattr(self, "modern_more_nav_button", None)
        if frame is not None:
            if self.modern_more_nav_visible:
                frame.pack(fill="x")
            else:
                frame.pack_forget()
        if button is not None:
            button.configure(
                text=self._t(
                    "modern.nav.less"
                    if self.modern_more_nav_visible
                    else "modern.nav.more"
                )
            )

    def _select_modern_recent_room(self, room_id: str) -> None:
        label = next(
            (
                candidate
                for candidate, candidate_id in self._room_ids_by_label.items()
                if candidate_id == room_id
            ),
            "",
        )
        if not label:
            return
        self.room_choice.set(label)
        self._on_room_selected()
        self.show_page("chat")

    def _sync_modern_recent_rooms(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        frame = getattr(self, "modern_recent_rooms_frame", None)
        if frame is None:
            return
        for button in self.modern_recent_room_buttons:
            with contextlib.suppress(tk.TclError):
                button.destroy()
        self.modern_recent_room_buttons = []
        rooms = sorted(
            self._rooms.values(),
            key=lambda row: str(row.get("updated_utc") or row.get("created_utc") or ""),
            reverse=True,
        )[:5]
        if not rooms:
            empty = tk.Label(
                frame,
                text=self._t("modern.rooms.empty"),
                bg=MODERN_SIDEBAR_BG,
                fg=COLORS["muted"],
                anchor="w",
                padx=10,
                pady=7,
                font=(MODERN_FONT_FAMILY, 9),
            )
            empty.pack(fill="x")
            self.modern_recent_room_buttons.append(empty)
            return
        for row in rooms:
            room_id = str(row.get("room_id") or "")
            active = room_id == self.selected_room_id
            name = str(row.get("name") or room_id or self._t("modern.rooms.unnamed"))
            count = int(row.get("message_count") or 0)
            updated_text = utc_text(
                row.get("updated_utc") or row.get("created_utc")
            ).rsplit(" ", 1)[-1][:5]
            row_bg = MODERN_NAV_ACTIVE_BG if active else MODERN_SIDEBAR_BG
            room_row = tk.Frame(
                frame,
                bg=row_bg,
                highlightthickness=0,
            )
            room_row.pack(fill="x", pady=1, ipady=1)
            tk.Frame(
                room_row,
                bg=COLORS["blue"] if active else row_bg,
                width=3,
            ).pack(side="left", fill="y")
            room_content = tk.Frame(room_row, bg=row_bg)
            room_content.pack(side="left", fill="x", expand=True, padx=(7, 6), pady=4)
            room_content.grid_columnconfigure(0, weight=1)
            name_button = tk.Button(
                room_content,
                text=clip(name, 22),
                command=lambda value=room_id: self._select_modern_recent_room(value),
                bg=row_bg,
                fg=COLORS["text"],
                activebackground=MODERN_NAV_ACTIVE_BG,
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                anchor="w",
                padx=0,
                pady=0,
                font=(MODERN_FONT_FAMILY, 9, "bold" if active else "normal"),
            )
            name_button.grid(row=0, column=0, sticky="ew")
            time_button = tk.Button(
                room_content,
                text=updated_text,
                command=lambda value=room_id: self._select_modern_recent_room(value),
                bg=row_bg,
                fg=COLORS["muted"],
                activebackground=MODERN_NAV_ACTIVE_BG,
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                padx=0,
                pady=0,
                font=(MODERN_FONT_FAMILY, 7),
            )
            time_button.grid(row=0, column=1, sticky="e")
            count_button = tk.Button(
                room_content,
                text=self._t("modern.rooms.messages").format(count=count),
                command=lambda value=room_id: self._select_modern_recent_room(value),
                bg=row_bg,
                fg=COLORS["muted"],
                activebackground=MODERN_NAV_ACTIVE_BG,
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                anchor="w",
                padx=0,
                pady=0,
                font=(MODERN_FONT_FAMILY, 7),
            )
            count_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(1, 0))
            self.modern_recent_room_buttons.append(room_row)

    def _toggle_modern_room_settings(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        self.modern_room_settings_visible = not self.modern_room_settings_visible
        self._reflow_modern_room_bar(
            self.modern_room_bar,
            self.modern_limit_frame,
            self.modern_discussion_controls,
        )

    def _toggle_modern_composer_advanced(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        self.modern_composer_advanced = not self.modern_composer_advanced
        self._configure_chat_composer_layout(self._chat_composer_compact)
        self.root.after_idle(self._layout_chat_after_resize)

    def _toggle_modern_agent_editor(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        self.modern_agent_editor_visible = not self.modern_agent_editor_visible
        self._reflow_modern_room_inspector(
            self.room_seats_frame,
            self.modern_role_bar,
            self.modern_seat_scroll,
            self.modern_guided_panel,
        )

    def _sync_modern_chat_context(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        title_label = getattr(self, "modern_room_title_label", None)
        context_label = getattr(self, "modern_room_context_label", None)
        room_id = str(getattr(self, "selected_room_id", "") or "")
        room = getattr(self, "_rooms", {}).get(room_id, {})
        room_name = str(
            room.get("name")
            or room_id
            or self._t("modern.rooms.unnamed")
        )
        room_messages = tuple(getattr(self, "_room_messages", ()) or ())
        focus_title = ""
        for message in reversed(room_messages):
            subject = str(message.get("subject") or "").strip()
            if subject and not str(message.get("reply_to") or "").strip():
                focus_title = subject
                break
        if not focus_title:
            focus_title = room_name
        members = tuple(getattr(self, "_room_members", ()) or ())
        active_agents = sum(
            1
            for row in members
            if str(row.get("status") or "active").lower() == "active"
            and str(row.get("agent_id") or "") != HUMAN_AGENT_ID
        )
        messages = int(room.get("message_count") or len(room_messages))
        context = self._t("modern.room.context").format(
            scope=clip(self.scope, 32),
            count=active_agents,
            messages=messages,
        )
        if title_label is not None:
            title_label.configure(text=clip(room_name, 52))
        if context_label is not None:
            context_label.configure(text=context)
        if getattr(self, "active_page", "") == "chat":
            page_title = getattr(self, "page_title", None)
            if page_title is not None:
                page_title.configure(text=clip(focus_title, 58))
            scope_label = getattr(self, "modern_toolbar_scope_label", None)
            if scope_label is not None:
                scope_label.configure(text=f"{clip(room_name, 24)}  ·  {context}")

    @staticmethod
    def _modern_agent_presentation(sender: str) -> tuple[str, str, str, str]:
        sender_key = sender.lower()
        sender_style = {
            "codex-main": ("Codex", "C", "#e8f0fe", "#315c9f"),
            "claude-code": ("Claude Code", "CL", "#f2edf9", "#72579b"),
            "grok-relay": ("Grok", "G", "#edf1f4", "#4f5b66"),
            "grok-official": ("Grok", "G", "#edf1f4", "#4f5b66"),
            "kimi-relay": ("Kimi", "K", "#edf4f1", "#39725f"),
            "peerbridge-orchestrator": ("PeerBridge", "PB", "#eef2f7", "#385d83"),
        }
        return sender_style.get(
            sender_key,
            (
                sender,
                "".join(part[:1] for part in sender.split("-")[:2]).upper() or "?",
                "#f4f6f8",
                "#5d6873",
            )
        )

    def _render_modern_agent_inspector(self) -> None:
        if ACTIVE_THEME != "modern":
            return
        frame = getattr(self, "modern_agents_summary_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        cards = tuple(
            card
            for card in room_agent_cards(
                self.selected_room_id,
                self._room_members,
                self._library_agents,
            )
            if str(card.get("agent_id") or "") != HUMAN_AGENT_ID
        )
        room_rows = tuple(
            room_messages(
                getattr(self, "_room_messages", ()) or (),
                self.selected_room_id,
            )
        )
        room_message_ids = {
            str(row.get("message_id"))
            for row in room_rows
            if row.get("message_id")
        }
        snapshot = getattr(self, "snapshot", None)
        dispatches = tuple(
            row
            for row in (getattr(snapshot, "message_dispatches", ()) if snapshot else ())
            if str(row.get("message_id") or "") in room_message_ids
        )
        dispatch_by_agent: dict[str, dict[str, Any]] = {}
        for row in dispatches:
            agent_id = str(row.get("agent_id") or "")
            if agent_id:
                dispatch_by_agent[agent_id] = row
        replied_agents = {
            str(row.get("sender") or "")
            for row in room_rows
            if str(row.get("sender") or "") not in {"", HUMAN_AGENT_ID}
        }
        status_counts: dict[str, int] = {}
        for row in dispatches:
            status = str(row.get("status") or "unknown").lower()
            status_counts[status] = status_counts.get(status, 0) + 1
        completed = max(status_counts.get("completed", 0), len(replied_agents))
        in_flight = status_counts.get("claimed", 0) + status_counts.get("retryable", 0)
        failed = status_counts.get("failed", 0)
        header = tk.Frame(frame, bg=COLORS["panel"])
        header.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(
            header,
            text=self._t("modern.inspector.room_status"),
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(MODERN_FONT_FAMILY, 11, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text=self._t("modern.inspector.seat_count").format(count=len(cards)),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(MODERN_FONT_FAMILY, 9),
        ).pack(side="right")

        metrics = tk.Frame(frame, bg=COLORS["panel_2"], height=44)
        metrics.pack(fill="x", padx=12, pady=(0, 8))
        metrics.pack_propagate(False)
        metric_specs = (
            (self._t("modern.inspector.messages"), len(room_rows)),
            (self._t("modern.inspector.completed"), completed),
            (self._t("modern.inspector.in_flight"), in_flight),
            (self._t("modern.inspector.failed"), failed),
        )
        for column in range(len(metric_specs)):
            metrics.grid_columnconfigure(column, weight=1, uniform="modern-room-metric")
        metrics.grid_rowconfigure(0, weight=1)
        for index, (label, value) in enumerate(metric_specs):
            metric = tk.Frame(
                metrics,
                bg=COLORS["panel_2"],
                highlightthickness=0,
            )
            metric.grid(
                row=0,
                column=index,
                sticky="nsew",
                padx=(0, 1) if index < len(metric_specs) - 1 else 0,
            )
            tk.Label(
                metric,
                text=str(value),
                bg=COLORS["panel_2"],
                fg=COLORS["text"],
                anchor="center",
                font=(MODERN_FONT_FAMILY, 10, "bold"),
            ).pack(fill="x", pady=(5, 0))
            tk.Label(
                metric,
                text=label,
                bg=COLORS["panel_2"],
                fg=COLORS["muted"],
                anchor="center",
                font=(MODERN_FONT_FAMILY, 7),
            ).pack(fill="x", pady=(0, 4))

        target = max(len(cards), 1)
        progress_ratio = min(completed / target, 1.0)
        progress = tk.Frame(frame, bg=COLORS["panel"])
        progress.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(
            progress,
            text=self._t("modern.inspector.reply_progress").format(
                completed=completed,
                total=len(cards),
            ),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            font=(MODERN_FONT_FAMILY, 8),
        ).pack(fill="x")
        track = tk.Frame(progress, bg=COLORS["panel_2"], height=4)
        track.pack(fill="x", pady=(5, 0))
        track.pack_propagate(False)
        bar = tk.Frame(track, bg=COLORS["green"], height=4)
        bar.place(relx=0, rely=0, relwidth=progress_ratio, relheight=1)

        if not cards:
            tk.Label(
                frame,
                text=self._t("chat.no_agents"),
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                anchor="w",
                padx=12,
                pady=12,
                font=(MODERN_FONT_FAMILY, 9),
            ).pack(fill="x")
        for card in cards:
            agent_id = str(card.get("agent_id") or "")
            online = str(card.get("state") or "").upper() in {"ONLINE", "CONTROL"}
            dispatch_status = str(
                dispatch_by_agent.get(agent_id, {}).get("status") or ""
            ).lower()
            if agent_id in replied_agents:
                state_key = "modern.agent.replied"
                state_color = COLORS["green"]
            elif dispatch_status == "claimed":
                state_key = "modern.agent.working"
                state_color = COLORS["blue"]
            elif dispatch_status == "retryable":
                state_key = "modern.agent.retrying"
                state_color = COLORS["amber"]
            elif dispatch_status == "failed":
                state_key = "modern.agent.failed"
                state_color = COLORS["red"]
            else:
                state_key = "chat.state.online" if online else "chat.state.offline"
                state_color = COLORS["green"] if online else COLORS["muted"]
            row = tk.Frame(
                frame,
                bg=COLORS["panel"],
                highlightthickness=0,
            )
            row.pack(fill="x", padx=12, pady=1)
            avatar = tk.Label(
                row,
                text=str(card.get("agent_id") or "?")[:2].upper(),
                width=3,
                height=1,
                bg=COLORS["panel_2"],
                fg=COLORS["text"],
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            )
            avatar.pack(side="left", padx=(4, 8), pady=5, ipady=3)
            detail = tk.Frame(row, bg=COLORS["panel"])
            detail.pack(side="left", fill="x", expand=True, pady=4)
            tk.Label(
                detail,
                text=clip(str(card.get("agent_id") or "unknown"), 24),
                bg=COLORS["panel"],
                fg=COLORS["text"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 9, "bold"),
            ).pack(fill="x")
            runtime = "/".join(
                str(value)
                for value in (card.get("model_id"), card.get("reasoning_mode"))
                if value
            ) or self._t("chat.route.none")
            tk.Label(
                detail,
                text=clip(runtime, 32),
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8),
            ).pack(fill="x")
            tk.Label(
                row,
                text=self._t(state_key),
                bg=COLORS["panel"],
                fg=state_color,
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            ).pack(side="right", padx=(6, 8))

        divider = tk.Frame(frame, bg=COLORS["line"], height=1)
        divider.pack(fill="x", padx=12, pady=(8, 7))
        tk.Label(
            frame,
            text=self._t("modern.inspector.automation"),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            font=(MODERN_FONT_FAMILY, 8, "bold"),
        ).pack(fill="x", padx=12)
        automation = tk.Frame(
            frame,
            bg=COLORS["panel_2"],
            highlightthickness=0,
        )
        automation.pack(fill="x", padx=12, pady=(5, 10))
        tk.Label(
            automation,
            text=self._t("modern.inspector.automation_value").format(
                mode=self.room_automation_choice.get(),
                rounds=self.room_round_limit.get(),
            ),
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            anchor="w",
            justify="left",
            wraplength=248,
            font=(MODERN_FONT_FAMILY, 9, "bold"),
        ).pack(fill="x", padx=9, pady=7)

    def _build_modern_evidence_panel(self, parent: tk.Frame) -> tk.Frame:
        frame = tk.Frame(parent, bg=COLORS["panel"], bd=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        title = self._localized_label(
            frame,
            "modern.inspector.evidence",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            anchor="w",
            font=(MODERN_FONT_FAMILY, 11, "bold"),
        )
        title.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 4))
        self.modern_evidence_intro = tk.Label(
            frame,
            text=self._t("modern.inspector.evidence_intro"),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify="left",
            anchor="w",
            wraplength=300,
            font=(MODERN_FONT_FAMILY, 9),
        )
        self.modern_evidence_intro.grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 10)
        )
        status_specs = (
            ("discussion", self.discussion_status),
            ("workflow", self.guided_workflow_status),
            ("seat", self.seat_status),
        )
        for row, (status_key, variable) in enumerate(status_specs, start=2):
            label = self._localized_label(
                frame,
                f"modern.status.{status_key}",
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            )
            label.grid(row=row, column=0, sticky="nw", padx=(12, 6), pady=4)
            tk.Label(
                frame,
                textvariable=variable,
                bg=COLORS["panel"],
                fg=COLORS["text"],
                justify="left",
                anchor="w",
                wraplength=205,
                font=(MODERN_FONT_FAMILY, 8),
            ).grid(row=row, column=1, sticky="ew", padx=(4, 12), pady=4)

        divider = tk.Frame(frame, bg=COLORS["line"], height=1)
        divider.grid(row=5, column=0, columnspan=2, sticky="ew", padx=12, pady=10)
        self.modern_evidence_buttons: dict[str, tk.Button] = {}
        for index, page_key in enumerate(
            ("work", "review", "audit", "trust", "memory", "usage")
        ):
            button = tk.Button(
                frame,
                text=self._t(f"modern.open.{page_key}"),
                command=lambda value=page_key: self.show_page(value),
                bg=COLORS["panel_2"],
                fg=COLORS["text"],
                activebackground=COLORS["line"],
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                anchor="w",
                padx=10,
                pady=8,
                font=(MODERN_FONT_FAMILY, 9, "bold"),
            )
            button.grid(
                row=6 + index // 2,
                column=index % 2,
                sticky="ew",
                padx=(12 if index % 2 == 0 else 4, 4 if index % 2 == 0 else 12),
                pady=4,
            )
            self.modern_evidence_buttons[page_key] = button
        return frame

    def _reflow_modern_room_bar(
        self,
        room_bar: tk.Frame,
        limit_frame: tk.Frame,
        controls: tk.Frame,
    ) -> None:
        if ACTIVE_THEME != "modern":
            return
        managed = (
            self.modern_room_identity_frame,
            self.room_bar_label,
            self.room_combo,
            self.room_status_label,
            self.chat_focus_button,
            self.new_room_button,
            self.operator_room_button,
            self.older_history_button,
            self.latest_history_button,
            self.auto_label,
            self.room_automation_combo,
            limit_frame,
            self.apply_automation_button,
            controls,
            self.discussion_status_label,
            self.modern_room_settings_button,
            self.modern_inspector_toggle_button,
        )
        for widget in managed:
            widget.grid_forget()
        for column in range(8):
            room_bar.grid_columnconfigure(column, weight=0)
        room_bar.grid_columnconfigure(0, weight=1)
        self.modern_room_settings_button.grid(row=0, column=4, padx=3, pady=6)
        self.modern_room_settings_button.configure(
            text=self._t(
                "modern.room.hide_settings"
                if self.modern_room_settings_visible
                else "modern.room.settings"
            ),
            bg=MODERN_WORKSPACE_BG,
            fg=COLORS["muted"],
            activebackground=COLORS["panel_2"],
            relief="flat",
            bd=0,
            font=(MODERN_FONT_FAMILY, 8),
        )
        self.chat_focus_button.configure(
            bg=MODERN_WORKSPACE_BG,
            fg=COLORS["text"],
            activebackground=COLORS["panel_2"],
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            font=(MODERN_FONT_FAMILY, 8, "bold"),
        )
        self.modern_inspector_toggle_button.grid(
            row=0, column=5, padx=3, pady=6
        )
        self.modern_inspector_toggle_button.configure(
            bg=MODERN_WORKSPACE_BG,
            fg=COLORS["muted"],
            activebackground=COLORS["panel_2"],
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            font=(MODERN_FONT_FAMILY, 8),
        )
        self.chat_focus_button.grid(row=0, column=6, padx=(3, 18), pady=6)
        if not self.modern_room_settings_visible:
            return
        for widget in (
            self.new_room_button,
            self.operator_room_button,
            self.older_history_button,
            self.latest_history_button,
            self.apply_automation_button,
        ):
            widget.configure(
                relief="flat",
                bd=0,
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            )
        self.room_bar_label.configure(
            bg=MODERN_WORKSPACE_BG,
            fg=COLORS["muted"],
            font=(MODERN_FONT_FAMILY, 8, "bold"),
        )
        self.room_status_label.configure(
            bg=MODERN_WORKSPACE_BG, font=(MODERN_FONT_FAMILY, 8)
        )
        self.auto_label.configure(
            bg=MODERN_WORKSPACE_BG,
            fg=COLORS["muted"],
            font=(MODERN_FONT_FAMILY, 8, "bold"),
        )
        limit_frame.configure(bg=MODERN_WORKSPACE_BG)
        controls.configure(bg=MODERN_WORKSPACE_BG)
        self.room_bar_label.grid(row=1, column=0, sticky="w", padx=(24, 6), pady=(2, 5))
        self.room_combo.grid(row=1, column=1, columnspan=3, sticky="ew", padx=4, pady=(2, 5))
        self.new_room_button.grid(row=1, column=4, padx=4, pady=(2, 5))
        self.operator_room_button.grid(row=1, column=5, padx=4, pady=(2, 5))
        self.older_history_button.grid(row=2, column=0, padx=(24, 4), pady=4)
        self.latest_history_button.grid(row=2, column=1, sticky="w", padx=4, pady=4)
        self.auto_label.grid(row=2, column=2, sticky="e", padx=(8, 4), pady=4)
        self.room_automation_combo.grid(row=2, column=3, sticky="ew", padx=4, pady=4)
        limit_frame.grid(row=2, column=4, columnspan=2, sticky="e", padx=(8, 18), pady=4)
        self.apply_automation_button.grid(row=3, column=0, padx=(24, 4), pady=(4, 9))
        controls.grid(row=3, column=1, columnspan=3, sticky="w", padx=4, pady=(4, 9))
        self.discussion_status_label.grid(
            row=3, column=4, columnspan=3, sticky="ew", padx=(8, 18), pady=(4, 9)
        )
        self.room_status_label.grid(
            row=4, column=0, columnspan=7, sticky="ew", padx=24, pady=(0, 10)
        )

    def _reflow_modern_room_inspector(
        self,
        seats: tk.Frame,
        role_bar: tk.Frame,
        seat_scroll: ttk.Scrollbar,
        guided: tk.Frame,
    ) -> None:
        if ACTIVE_THEME != "modern":
            return
        for widget in seats.grid_slaves():
            widget.grid_remove()
        for column in range(7):
            seats.grid_columnconfigure(column, weight=0)
        seats.grid_columnconfigure(1, weight=1)
        seats.grid_rowconfigure(12, weight=0)
        self.modern_agents_summary_frame.grid(
            row=0, column=0, columnspan=2, sticky="ew"
        )
        self.modern_manage_agents_button.grid(
            row=1, column=1, sticky="e", padx=12, pady=(2, 10)
        )
        self.modern_manage_agents_button.configure(
            text=self._t(
                "modern.agents.done"
                if self.modern_agent_editor_visible
                else "modern.agents.manage"
            )
        )
        if not self.modern_agent_editor_visible:
            self._render_modern_agent_inspector()
            return
        self.room_seats_label.grid_configure(
            row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 5)
        )
        field_widgets = (
            ("agent", self.seat_agent_combo),
            ("provider", self.seat_provider_combo),
            ("model", self.seat_model_combo),
            ("reasoning", self.seat_reasoning_combo),
        )
        if not hasattr(self, "modern_seat_field_labels"):
            self.modern_seat_field_labels = {}
        for row, (field_key, widget) in enumerate(field_widgets, start=3):
            label = self.modern_seat_field_labels.get(field_key)
            if label is None:
                label = self._localized_label(
                    seats,
                    f"modern.seat.{field_key}",
                    bg=COLORS["panel_2"],
                    fg=COLORS["muted"],
                    anchor="w",
                    font=(MODERN_FONT_FAMILY, 8, "bold"),
                )
                self.modern_seat_field_labels[field_key] = label
            label.grid(row=row, column=0, sticky="w", padx=(10, 5), pady=4)
            widget.grid_configure(
                row=row,
                column=1,
                columnspan=1,
                sticky="ew",
                padx=(4, 10),
                pady=4,
            )
        self.add_seat_button.grid_configure(row=7, column=0, sticky="ew", padx=(10, 4))
        self.remove_seat_button.grid_configure(row=7, column=1, sticky="ew", padx=(4, 10))
        role_bar.grid_configure(
            row=8, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4)
        )
        for column in range(7):
            role_bar.grid_columnconfigure(column, weight=0)
        role_bar.grid_columnconfigure(1, weight=1)
        self.seat_role_label.grid_configure(row=0, column=0, sticky="w")
        self.seat_role_combo.grid_configure(row=0, column=1, columnspan=1, sticky="ew")
        self.seat_custom_role_entry.grid_configure(
            row=1, column=0, columnspan=2, sticky="ew"
        )
        self.apply_role_button.grid_configure(row=2, column=0, sticky="ew")
        self.view_live_work_button.grid_configure(row=2, column=1, sticky="ew")
        self.seat_role_note.grid_configure(
            row=3, column=0, columnspan=2, sticky="ew"
        )
        self.seat_role_note.configure(wraplength=275)
        self.seat_status_label.grid_configure(
            row=9, column=0, columnspan=2, sticky="ew", padx=10
        )
        self.room_seat_tree.configure(
            displaycolumns=("agent", "role", "state"), height=4
        )
        self.room_seat_tree.grid_configure(
            row=10, column=0, columnspan=2, sticky="nsew", padx=(10, 10), pady=(0, 10)
        )
        seat_scroll.grid_configure(
            row=10, column=1, sticky="nse", padx=(0, 10), pady=(0, 10)
        )

        guided.grid_columnconfigure(0, weight=1)
        guided.grid_columnconfigure(1, weight=0)
        self.guided_workflow_title.grid_configure(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 4)
        )
        self.guided_workflow_detail.grid_configure(
            row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=4
        )
        self.guided_workflow_detail.configure(wraplength=285)
        self.guided_workflow_button.grid_configure(
            row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=6
        )
        self.guided_workflow_status_label.grid_configure(
            row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 10)
        )
        self.guided_workflow_status_label.configure(wraplength=285)

    def _sync_sidebar_scrollregion(self, _event: Any = None) -> None:
        try:
            bounds = self.sidebar_canvas.bbox("all")
            if bounds is not None:
                self.sidebar_canvas.configure(scrollregion=bounds)
        except (AttributeError, tk.TclError):
            return

    def _resize_sidebar_viewport(self, event: Any) -> None:
        try:
            self.sidebar_canvas.itemconfigure(
                self.sidebar_window, width=max(1, int(event.width))
            )
            self._sync_sidebar_scrollregion()
        except (AttributeError, tk.TclError, ValueError):
            return

    def _scroll_sidebar(self, event: Any) -> str:
        try:
            delta = int(getattr(event, "delta", 0))
            if delta:
                self.sidebar_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        except (AttributeError, tk.TclError, ValueError):
            pass
        return "break"

    def _sidebar_widget_visible(self, widget: tk.Widget) -> bool:
        try:
            viewport_top = self.sidebar_canvas.winfo_rooty()
            viewport_bottom = viewport_top + self.sidebar_canvas.winfo_height()
            widget_top = widget.winfo_rooty()
            widget_bottom = widget_top + widget.winfo_height()
            return bool(
                widget.winfo_ismapped()
                and widget_top >= viewport_top
                and widget_bottom <= viewport_bottom
            )
        except (AttributeError, tk.TclError):
            return False

    def _reveal_sidebar_widget(self, widget: tk.Widget) -> bool:
        try:
            self.root.update_idletasks()
            if self._sidebar_widget_visible(widget):
                return True
            bounds = self.sidebar_canvas.bbox(self.sidebar_window)
            if bounds is None:
                return False
            content_top = self.sidebar_content.winfo_rooty()
            widget_top = widget.winfo_rooty() - content_top
            widget_bottom = widget_top + widget.winfo_height()
            fraction = vertical_scroll_fraction_to_reveal(
                widget_top=widget_top,
                widget_bottom=widget_bottom,
                viewport_top=float(self.sidebar_canvas.canvasy(0)),
                viewport_height=self.sidebar_canvas.winfo_height(),
                content_height=bounds[3] - bounds[1],
            )
            self.sidebar_canvas.yview_moveto(fraction)
            self.root.update_idletasks()
            return self._sidebar_widget_visible(widget)
        except (AttributeError, tk.TclError, ValueError):
            return False

    def _configure_chat_composer_layout(self, compact: bool) -> None:
        widgets = (
            *self.composer_labels.values(),
            self.recipient_combo,
            self.priority_combo,
            self.message_task_entry,
            self.profile_combo,
            self.model_combo,
            self.reasoning_combo,
            self.manage_sources_button,
            self.message_subject_entry,
            self.message_body,
            self.chat_attach_button,
            self.chat_clear_attachments_button,
            self.chat_attachment_label,
            self.modern_composer_advanced_button,
            self.modern_composer_prompt_label,
            self.send_button,
            self.message_status_label,
        )
        for widget in widgets:
            widget.grid_forget()
        for column in range(8):
            self.chat_composer.grid_columnconfigure(column, weight=0, minsize=0)

        if ACTIVE_THEME == "modern":
            self.chat_composer.grid_columnconfigure(2, weight=1, minsize=120)
            self.modern_composer_prompt_label.grid(
                row=0,
                column=0,
                columnspan=8,
                sticky="w",
                padx=14,
                pady=(5, 0),
            )
            self.message_body.configure(
                height=1,
                bg=COLORS["panel"],
                fg=COLORS["text"],
                insertbackground=COLORS["text"],
                relief="flat",
                bd=0,
                highlightthickness=0,
                font=(MODERN_FONT_FAMILY, 10),
            )
            self.message_body.grid(
                row=1,
                column=0,
                columnspan=8,
                sticky="ew",
                padx=14,
                pady=(1, 0),
            )
            self.chat_attach_button.configure(
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                activebackground=COLORS["panel_2"],
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                font=(MODERN_FONT_FAMILY, 8),
            )
            self.chat_attach_button.grid(
                row=2, column=0, sticky="w", padx=(12, 2), pady=(0, 2)
            )
            self.chat_clear_attachments_button.configure(
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                relief="flat",
                bd=0,
                font=(MODERN_FONT_FAMILY, 8),
            )
            self.chat_clear_attachments_button.grid(
                row=2, column=1, sticky="w", padx=2, pady=(0, 2)
            )
            self.recipient_combo.configure(width=20)
            self.recipient_combo.grid(
                row=2, column=2, sticky="w", padx=4, pady=(0, 2)
            )
            self.modern_composer_advanced_button.configure(
                text=self._t(
                    "modern.composer.simple"
                    if self.modern_composer_advanced
                    else "modern.composer.advanced"
                )
            )
            self.modern_composer_advanced_button.grid(
                row=2, column=3, sticky="w", padx=4, pady=(0, 2)
            )
            self.send_button.configure(width=8)
            self.send_button.configure(
                text="↑",
                width=3,
                padx=6,
                pady=3,
                bg="#172033",
                fg="#ffffff",
                activebackground="#26334c",
                activeforeground="#ffffff",
                relief="flat",
                bd=0,
                font=(MODERN_FONT_FAMILY, 12, "bold"),
            )
            self.send_button.grid(
                row=2, column=7, sticky="e", padx=(6, 12), pady=(0, 2)
            )
            status_row = 3
            if self._chat_attachment_paths:
                self.chat_attachment_label.grid(
                    row=3,
                    column=0,
                    columnspan=8,
                    sticky="ew",
                    padx=12,
                    pady=(0, 1),
                )
                status_row = 4
            if self.modern_composer_advanced:
                label_positions = {
                    "to": (4, 0),
                    "priority": (4, 2),
                    "task": (4, 4),
                    "provider": (5, 0),
                    "model": (5, 2),
                    "reasoning": (5, 4),
                    "subject": (6, 0),
                }
                for key, (row, column) in label_positions.items():
                    self.composer_labels[key].configure(
                        bg=COLORS["panel"],
                        fg=COLORS["muted"],
                        font=(MODERN_FONT_FAMILY, 8, "bold"),
                    )
                    self.composer_labels[key].grid(
                        row=row,
                        column=column,
                        sticky="w",
                        padx=(12 if column == 0 else 6, 3),
                        pady=3,
                    )
                self.recipient_combo.grid(
                    row=4, column=1, sticky="ew", padx=3, pady=3
                )
                self.priority_combo.grid(row=4, column=3, sticky="ew", padx=3, pady=3)
                self.message_task_entry.grid(
                    row=4, column=5, columnspan=3, sticky="ew", padx=(3, 12), pady=3
                )
                self.profile_combo.grid(row=5, column=1, sticky="ew", padx=3, pady=3)
                self.model_combo.grid(row=5, column=3, sticky="ew", padx=3, pady=3)
                self.reasoning_combo.grid(row=5, column=5, sticky="ew", padx=3, pady=3)
                self.manage_sources_button.configure(
                    bg=COLORS["panel_2"],
                    fg=COLORS["text"],
                    relief="flat",
                    bd=0,
                    font=(MODERN_FONT_FAMILY, 8, "bold"),
                )
                self.manage_sources_button.grid(
                    row=5, column=7, sticky="ew", padx=(3, 12), pady=3
                )
                self.message_subject_entry.grid(
                    row=6, column=1, columnspan=7, sticky="ew", padx=(3, 12), pady=3
                )
                status_row = 7
            self.message_status_label.configure(
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                font=(MODERN_FONT_FAMILY, 8),
            )
            self.message_status_label.grid(
                row=status_row,
                column=0,
                columnspan=8,
                sticky="ew",
                padx=12,
                pady=(0, 2),
            )
            return

        if compact:
            self.chat_composer.grid_columnconfigure(1, weight=1, minsize=120)
            self.recipient_combo.configure(width=18)
            self.profile_combo.configure(width=18)
            self.model_combo.configure(width=14)
            self.reasoning_combo.configure(width=12)
            for row, key in enumerate(
                ("to", "priority", "task", "provider", "model", "reasoning", "subject")
            ):
                self.composer_labels[key].grid(
                    row=row,
                    column=0,
                    sticky="w",
                    padx=(8, 4),
                    pady=(4, 2),
                )
            self.recipient_combo.grid(
                row=0, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=(4, 2)
            )
            self.priority_combo.grid(row=1, column=1, sticky="w", padx=4, pady=2)
            self.message_task_entry.grid(
                row=2, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=2
            )
            self.profile_combo.grid(row=3, column=1, sticky="ew", padx=4, pady=2)
            self.manage_sources_button.grid(
                row=3, column=2, sticky="ew", padx=(4, 8), pady=2
            )
            self.model_combo.grid(
                row=4, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=2
            )
            self.reasoning_combo.grid(
                row=5, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=2
            )
            self.message_subject_entry.grid(
                row=6, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=2
            )
            self.message_body.grid(
                row=7, column=0, columnspan=3, sticky="ew", padx=8, pady=(5, 4)
            )
            self.chat_attach_button.grid(
                row=8, column=0, sticky="w", padx=(8, 4), pady=2
            )
            self.chat_clear_attachments_button.grid(
                row=8, column=1, sticky="w", padx=4, pady=2
            )
            self.send_button.grid(row=8, column=2, sticky="e", padx=8, pady=2)
            self.chat_attachment_label.grid(
                row=9, column=0, columnspan=3, sticky="ew", padx=8, pady=2
            )
            self.message_status_label.grid(
                row=10, column=0, columnspan=3, sticky="ew", padx=8, pady=(2, 5)
            )
            return

        self.chat_composer.grid_columnconfigure(7, weight=1, minsize=104)
        self.recipient_combo.configure(width=23)
        self.profile_combo.configure(width=23)
        self.model_combo.configure(width=14)
        self.reasoning_combo.configure(width=10)
        label_positions = {
            "to": (0, 0, (8, 4), 6),
            "priority": (0, 2, (10, 4), 6),
            "task": (0, 4, (10, 4), 6),
            "provider": (1, 0, (8, 4), 4),
            "model": (1, 2, (8, 4), 4),
            "reasoning": (1, 4, (8, 4), 4),
            "subject": (2, 0, (8, 4), 4),
        }
        for key, (row, column, padx, pady) in label_positions.items():
            self.composer_labels[key].grid(
                row=row, column=column, padx=padx, pady=pady
            )
        self.recipient_combo.grid(row=0, column=1, padx=4, pady=6)
        self.priority_combo.grid(row=0, column=3, padx=4, pady=6)
        self.message_task_entry.grid(
            row=0, column=5, columnspan=3, sticky="ew", padx=4, pady=6
        )
        self.profile_combo.grid(row=1, column=1, padx=4, pady=4)
        self.model_combo.grid(row=1, column=3, padx=4, pady=4)
        self.reasoning_combo.grid(
            row=1, column=5, columnspan=2, sticky="ew", padx=4, pady=4
        )
        self.manage_sources_button.grid(
            row=1, column=7, sticky="ew", padx=(4, 8), pady=4
        )
        self.message_subject_entry.grid(
            row=2, column=1, columnspan=7, sticky="ew", padx=4, pady=4
        )
        self.message_body.grid(
            row=3, column=0, columnspan=8, sticky="ew", padx=8, pady=(5, 4)
        )
        self.chat_attach_button.grid(
            row=4, column=0, sticky="w", padx=(8, 4), pady=(2, 2)
        )
        self.chat_clear_attachments_button.grid(
            row=4, column=1, sticky="w", padx=4, pady=(2, 2)
        )
        self.chat_attachment_label.grid(
            row=4, column=2, columnspan=5, sticky="ew", padx=4, pady=(2, 2)
        )
        self.send_button.grid(row=4, column=7, sticky="e", padx=8, pady=(2, 2))
        self.message_status_label.grid(
            row=5, column=0, columnspan=8, sticky="ew", padx=8, pady=(2, 5)
        )

    def _reflow_chat_composer(
        self, event: Any = None, *, schedule_layout: bool = True
    ) -> bool:
        try:
            width = max(
                1, int(getattr(event, "width", self.chat_composer.winfo_width()))
            )
            compact = width < self._chat_composer_wide_required_width
            wraplength = max(220, width - 24) if compact else 0
            self.chat_attachment_label.configure(wraplength=wraplength, justify="left")
            self.message_status_label.configure(wraplength=wraplength, justify="left")
            if compact == self._chat_composer_compact:
                return False
            self._chat_composer_compact = compact
            self._configure_chat_composer_layout(compact)
            if schedule_layout and not self._closing:
                self.root.after_idle(self._layout_chat_after_resize)
            return True
        except (AttributeError, tk.TclError, ValueError):
            return False

    def _reflow_modern_composer_shell(self, event: Any = None) -> None:
        if ACTIVE_THEME != "modern":
            return
        try:
            width = max(
                1,
                int(
                    getattr(
                        event,
                        "width",
                        self.chat_composer_pane.winfo_width(),
                    )
                ),
            )
            content_width, _offset = modern_chat_content_geometry(width)
            horizontal_padding = max(0, (width - content_width) // 2)
            self.chat_composer.grid_configure(
                padx=(horizontal_padding, horizontal_padding)
            )
        except (AttributeError, tk.TclError, ValueError):
            return

    def _resize_chat_history_viewport(self, event: Any = None) -> None:
        """Keep Modern messages readable on wide screens and fluid on small ones."""

        try:
            viewport_width = max(
                1,
                int(getattr(event, "width", self.chat_canvas.winfo_width())),
            )
            if ACTIVE_THEME == "modern":
                content_width, offset = modern_chat_content_geometry(viewport_width)
                self.chat_canvas.coords(self.chat_history_window, offset, 0)
                self.chat_canvas.itemconfigure(
                    self.chat_history_window,
                    width=content_width,
                )
            else:
                self.chat_canvas.coords(self.chat_history_window, 0, 0)
                self.chat_canvas.itemconfigure(
                    self.chat_history_window,
                    width=viewport_width,
                )
            self._sync_chat_history_scrollregion()
        except (AttributeError, tk.TclError, TypeError, ValueError):
            return

    def _sync_chat_history_scrollregion(self, _event: Any = None) -> None:
        try:
            bbox = self.chat_canvas.bbox("all")
            if bbox is None:
                return
            viewport_width = max(1, int(self.chat_canvas.winfo_width()))
            self.chat_canvas.configure(
                scrollregion=(0, 0, viewport_width, max(1, int(bbox[3])))
            )
        except (AttributeError, tk.TclError, TypeError, ValueError):
            return

    def _layout_chat_after_resize(self) -> None:
        """Reflow the split and bubbles after fullscreen geometry settles."""
        history_width = 0
        try:
            self.root.update_idletasks()
            if ACTIVE_THEME == "modern" and self.modern_chat_workspace is not None:
                workspace_width = max(
                    1, int(self.modern_chat_workspace.winfo_width())
                )
                self._sync_modern_inspector_visibility(workspace_width)
                previous_workspace_width = int(
                    self._modern_workspace_positioned_width or 0
                )
                if (
                    previous_workspace_width <= 0
                    or abs(workspace_width - previous_workspace_width) >= 64
                ):
                    inspector_width = min(
                        308, max(268, workspace_width // 4)
                    )
                    center_width = max(520, workspace_width - inspector_width)
                    if self._modern_inspector_present() and center_width < workspace_width:
                        self.modern_chat_workspace.sash_place(0, center_width, 0)
                    self._modern_workspace_positioned_width = workspace_width
                    self.root.update_idletasks()
            self._reflow_chat_composer(schedule_layout=False)
            self.root.update_idletasks()
            composer_pane = getattr(
                self, "chat_composer_pane", self.chat_composer
            )
            minimum_composer_height = 116 if ACTIVE_THEME == "modern" else 215
            composer_height = max(
                minimum_composer_height, composer_pane.winfo_reqheight()
            )
            self.chat_split.paneconfigure(
                composer_pane, minsize=composer_height
            )
            self.root.update_idletasks()
            self._resize_chat_page_viewport()
            self.root.update_idletasks()
            total_height = self.chat_split.winfo_height()
            sash_y = chat_split_sash_position(
                total_height,
                composer_height,
                min_history_height=CHAT_HISTORY_MIN_HEIGHT,
                min_composer_height=composer_height,
            )
            self.chat_split.sash_place(0, 0, sash_y)
            self.root.update_idletasks()
            self._resize_chat_page_viewport()
            self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
            history_width = max(1, int(self.chat_canvas.winfo_width()))
        except (AttributeError, tk.TclError, ValueError):
            return
        previous_width = int(getattr(self, "_chat_layout_history_width", 0) or 0)
        width_changed = previous_width <= 0 or abs(history_width - previous_width) >= 2
        self._chat_layout_history_width = history_width
        if (
            width_changed
            and self.snapshot is not None
            and self.active_page == "chat"
        ):
            self._chat_render_row_signatures = None
            self._render_chat(self.search.get().strip().lower())

    def _resize_chat_page_viewport(self, event: Any = None) -> None:
        """Keep the chat page full-height, or expose overflow through its scrollbar."""
        try:
            viewport_width = int(
                getattr(event, "width", self.chat_page_canvas.winfo_width())
            )
            viewport_height = int(
                getattr(event, "height", self.chat_page_canvas.winfo_height())
            )
            if ACTIVE_THEME == "modern":
                viewport_width = max(1, viewport_width)
                viewport_height = max(1, viewport_height)
                self.chat_page_canvas.itemconfigure(
                    self.chat_page_window,
                    width=viewport_width,
                    height=viewport_height,
                )
                self.chat_page_canvas.configure(
                    scrollregion=(0, 0, viewport_width, viewport_height)
                )
                self.chat_page_canvas.yview_moveto(0.0)
                return
            requested_height = self.chat_page_content.winfo_reqheight()
            content_height = max(
                1,
                viewport_height,
                requested_height,
                CHAT_PAGE_MIN_CONTENT_HEIGHT,
            )
            self.chat_page_canvas.itemconfigure(
                self.chat_page_window,
                width=max(1, viewport_width),
                height=content_height,
            )
            self.chat_page_canvas.configure(
                scrollregion=(0, 0, max(1, viewport_width), content_height)
            )
        except (AttributeError, tk.TclError):
            return

    def _sync_chat_page_scrollregion(self, _event: Any = None) -> None:
        try:
            self._resize_chat_page_viewport()
        except tk.TclError:
            return

    def _post_to_ui(self, callback: Callable[..., None], *args: Any) -> bool:
        """Queue one UI callback without touching Tk from a worker thread."""
        if self._closing:
            return False
        self._ui_callbacks.put((self._ui_generation, callback, args))
        return True

    def _schedule_ui_pump(self) -> None:
        if self._closing or self._ui_pump_after_id is not None:
            return
        self._ui_pump_after_id = self.root.after(50, self._drain_ui_callbacks)

    def _drain_ui_callbacks(self) -> None:
        self._ui_pump_after_id = None
        if self._closing:
            return
        for _ in range(256):
            try:
                generation, callback, args = self._ui_callbacks.get_nowait()
            except queue.Empty:
                break
            if generation != self._ui_generation:
                continue
            try:
                callback(*args)
            except Exception as exc:
                if not self._closing:
                    self.root.report_callback_exception(
                        type(exc), exc, exc.__traceback__
                    )
            if self._closing:
                return
        self._schedule_ui_pump()

    def _automation_mode_from_label(self, label: str) -> str | None:
        for locale in SUPPORTED_LOCALES:
            for mode, key in AUTOMATION_MODE_TO_KEY.items():
                if label == translate(locale, key):
                    return mode
        return None

    def _automation_labels(self) -> tuple[str, ...]:
        return tuple(self._t(AUTOMATION_MODE_TO_KEY[mode]) for mode in AUTOMATION_MODE_TO_KEY)

    def _room_role_from_label(self, label: str) -> str | None:
        mapped = self._seat_role_labels.get(label)
        if mapped:
            return mapped
        for locale in SUPPORTED_LOCALES:
            for role_id in ROOM_ROLE_IDS:
                if label == translate(locale, f"chat.role.{role_id}"):
                    return role_id
        return None

    def _room_role_label(self, member: Mapping[str, Any]) -> str:
        role_id = str(member.get("role_id") or DEFAULT_ROOM_ROLE)
        if role_id == "custom":
            custom = str(member.get("role_label") or "").strip()
            return custom or self._t("chat.role.custom")
        if role_id not in ROOM_ROLE_IDS:
            role_id = DEFAULT_ROOM_ROLE
        return self._t(f"chat.role.{role_id}")

    @staticmethod
    def _catalog_value_matches(value: str, key: str) -> bool:
        return any(value == translate(locale, key) for locale in SUPPORTED_LOCALES)

    def _save_ui_preferences(self) -> None:
        save_preferences(
            self.project_root,
            locale=self.locale.get(),
            tutorial_completed=self.tutorial_completed,
            theme=self.theme.get(),
        )

    def _theme_from_label(self, label: str) -> str | None:
        for labels in THEME_LABELS.values():
            for theme, candidate in labels.items():
                if label == candidate:
                    return theme
        return None

    def _theme_changed(self, _event: Any = None) -> None:
        selected = self._theme_from_label(self.theme_choice.get())
        if selected is None:
            return
        changed = selected != self.theme.get()
        self.theme.set(selected)
        self._save_ui_preferences()
        if changed:
            self.refresh_status.set(self._t("toolbar.theme_restart"))

    def _save_announcement_preferences(self) -> None:
        save_announcement_preferences(
            self.project_root,
            network_enabled=self.announcement_network_enabled.get(),
            popup_enabled=self.announcement_popup_enabled.get(),
            read_ids=self._announcement_read_ids,
            cursors=self._announcement_cursors,
        )

    def _locale_changed(self, _event: Any = None) -> None:
        selected = LABEL_LOCALES.get(self.locale_label.get())
        if not selected:
            return
        self.locale.set(selected)
        self._apply_locale(save=True)
        if self.announcement_network_enabled.get():
            self._schedule_announcement_check(250)

    def _apply_locale(self, *, save: bool) -> None:
        locale = self.locale.get()
        automation_mode = (
            self._automation_mode_from_label(self.room_automation_choice.get()) or "once"
        )
        seat_role_id = (
            self._room_role_from_label(self.seat_role_choice.get())
            or DEFAULT_ROOM_ROLE
        )
        selected_recipient_id = self._recipient_ids.get(
            self.message_recipient.get(), self.message_recipient.get()
        )
        selected_route_key = next(
            (
                key
                for key in (DIRECT_LABEL, BROADCAST_ROUTE_LABEL, NO_ROUTE_LABEL)
                if self._catalog_value_matches(self.message_provider_choice.get(), key)
            ),
            None,
        )
        seat_model_is_default = self._catalog_value_matches(
            self.seat_model_choice.get(), PROVIDER_DEFAULT_MODEL_LABEL
        )
        seat_reasoning_is_default = self._catalog_value_matches(
            self.seat_reasoning_choice.get(), PROVIDER_DEFAULT_REASONING_LABEL
        )
        self.locale_label.set(LOCALE_LABELS.get(locale, LOCALE_LABELS["en"]))
        self.theme_choice.set(THEME_LABELS[locale][self.theme.get()])
        for key, widgets in self._localized_label_widgets.items():
            for widget in tuple(widgets):
                with contextlib.suppress(tk.TclError):
                    if widget.winfo_exists():
                        widget.configure(text=self._t(key))
        self.version_label.configure(
            text=(
                self._t("sidebar.version").format(version=APP_VERSION)
                + "\n"
                + self._t("sidebar.build").format(build=APP_BUILD_ID)
            )
        )
        self.agent_library_label.configure(text=self._t("sidebar.agent_library"))
        self.library_route_notice.configure(
            text=self._t("sidebar.library_route_notice")
        )
        self.sidebar_scroll_hint.configure(text=self._t("sidebar.scroll_hint"))
        self.language_label.configure(text=self._t("toolbar.language"))
        self.theme_title_label.configure(text=self._t("toolbar.theme"))
        self.theme_combo.configure(
            values=tuple(THEME_LABELS[locale][key] for key in SUPPORTED_THEMES)
        )
        for key, button in self.nav_buttons.items():
            button.configure(text=self._navigation_label(key))
        for group_key, label in getattr(
            self, "modern_nav_group_labels", {}
        ).items():
            label.configure(text=self._t(f"modern.nav.{group_key}"))
        if ACTIVE_THEME == "modern":
            new_room_button = getattr(self, "modern_sidebar_new_room_button", None)
            if new_room_button is not None:
                new_room_button.configure(text=self._t("modern.sidebar.new_room"))
            projects_title = getattr(self, "modern_projects_title", None)
            if projects_title is not None:
                projects_title.configure(text=self._t("modern.projects.title"))
            project_status = getattr(self, "modern_project_status_label", None)
            if project_status is not None:
                project_status.configure(
                    text=self._t("modern.projects.local_active")
                )
            more_button = getattr(self, "modern_more_nav_button", None)
            if more_button is not None:
                more_button.configure(
                    text=self._t(
                        "modern.nav.less"
                        if self.modern_more_nav_visible
                        else "modern.nav.more"
                    )
                )
            recent_title = getattr(self, "modern_recent_rooms_title", None)
            if recent_title is not None:
                recent_title.configure(text=self._t("modern.rooms.title"))
            library_button = getattr(self, "modern_agent_library_button", None)
            if library_button is not None:
                library_button.configure(
                    text=self._t(
                        "modern.sidebar.hide_agents"
                        if self.modern_agent_library_visible
                        else "modern.sidebar.show_agents"
                    )
                )
            account_role = getattr(self, "modern_account_role", None)
            if account_role is not None:
                account_role.configure(text=self._t("modern.account.role"))
            options_button = getattr(self, "modern_toolbar_options_button", None)
            if options_button is not None:
                options_button.configure(
                    text=self._t(
                        "modern.toolbar.close_options"
                        if self.modern_toolbar_options_visible
                        else "modern.toolbar.options"
                    )
                )
            connection = getattr(self, "modern_toolbar_connection_label", None)
            if connection is not None:
                unavailable = bool(getattr(self, "_room_view_unavailable", False))
                connection.configure(
                    text=self._t(
                        "modern.toolbar.unavailable"
                        if unavailable
                        else "modern.toolbar.connected"
                    ),
                    fg=COLORS["red"] if unavailable else COLORS["green"],
                )
            room_settings = getattr(self, "modern_room_settings_button", None)
            if room_settings is not None:
                room_settings.configure(
                    text=self._t(
                        "modern.room.hide_settings"
                        if self.modern_room_settings_visible
                        else "modern.room.settings"
                    )
                )
            agent_manager = getattr(self, "modern_manage_agents_button", None)
            if agent_manager is not None:
                agent_manager.configure(
                    text=self._t(
                        "modern.agents.done"
                        if self.modern_agent_editor_visible
                        else "modern.agents.manage"
                    )
                )
            composer_button = getattr(
                self, "modern_composer_advanced_button", None
            )
            if composer_button is not None:
                composer_button.configure(
                    text=self._t(
                        "modern.composer.simple"
                        if self.modern_composer_advanced
                        else "modern.composer.advanced"
                    )
                )
            for field_key, label in getattr(
                self, "modern_seat_field_labels", {}
            ).items():
                label.configure(text=self._t(f"modern.seat.{field_key}"))
            self._sync_modern_recent_rooms()
            self._sync_modern_chat_context()
            self._render_modern_agent_inspector()
        for inspector_key, button in getattr(
            self, "modern_inspector_buttons", {}
        ).items():
            button.configure(
                text=self._t(f"modern.inspector.{inspector_key}")
            )
        for page_key, button in getattr(
            self, "modern_evidence_buttons", {}
        ).items():
            button.configure(text=self._t(f"modern.open.{page_key}"))
        modern_evidence_intro = getattr(self, "modern_evidence_intro", None)
        if modern_evidence_intro is not None:
            modern_evidence_intro.configure(
                text=self._t("modern.inspector.evidence_intro")
            )
        self.search_label.configure(text=self._t("toolbar.search"))
        self.pause_button.configure(text=self._t("toolbar.pause"))
        self.refresh_button.configure(text=self._t("toolbar.refresh"))
        if self._last_successful_refresh is not None:
            self.refresh_status.set(
                self._t("toolbar.refreshed").format(
                    time=self._last_successful_refresh.strftime("%H:%M:%S")
                )
            )
        self.help_button.configure(text=self._t("toolbar.help"))
        self._refresh_announcement_button()
        self.update_button.configure(text=self._t("toolbar.updates"))
        self.chat_attach_button.configure(text=self._t("chat.attach"))
        self.chat_clear_attachments_button.configure(
            text=self._t("chat.clear_attachments")
        )
        self.chat_attachment_note.configure(text=self._t("chat.attachment_note"))
        for key, label in self.composer_labels.items():
            label.configure(text=self._t(f"chat.{key}"))
        self.new_room_button.configure(text=self._t("chat.new_room"))
        self.operator_room_button.configure(text=self._t("chat.join_control"))
        self.older_history_button.configure(text=self._t("chat.older"))
        self.latest_history_button.configure(text=self._t("chat.latest"))
        self.chat_focus_button.configure(
            text=self._t(
                "chat.exit_focus" if self.chat_focus_mode else "chat.focus"
            )
        )
        self.auto_label.configure(text=self._t("chat.auto"))
        self.room_automation_combo.configure(values=self._automation_labels())
        self.room_automation_choice.set(
            self._t(AUTOMATION_MODE_TO_KEY[automation_mode])
        )
        for key, label in self.limit_labels.items():
            label.configure(text=self._t(f"chat.{key}"))
        self.apply_automation_button.configure(text=self._t("chat.apply"))
        self.pause_discussion_button.configure(text=self._t("chat.pause"))
        self.resume_discussion_button.configure(text=self._t("chat.resume"))
        self.continue_discussion_button.configure(text=self._t("chat.continue"))
        self.stop_discussion_button.configure(text=self._t("chat.stop"))
        self.guided_workflow_title.configure(text=self._t("chat.guided.title"))
        self.guided_workflow_detail.configure(text=self._t("chat.guided.detail"))
        self.guided_workflow_button.configure(text=self._t("chat.guided.start"))
        self.room_seats_label.configure(text=self._t("chat.room_seats"))
        self.add_seat_button.configure(text=self._t("chat.apply_seat"))
        self.remove_seat_button.configure(text=self._t("chat.remove_seat"))
        self.seat_role_label.configure(text=self._t("chat.role"))
        self.apply_role_button.configure(text=self._t("chat.apply_role"))
        self.view_live_work_button.configure(text=self._t("chat.view_live_work"))
        self.seat_role_note.configure(text=self._t("chat.role_no_authority"))
        self._seat_role_labels = {
            self._t(f"chat.role.{role_id}"): role_id for role_id in ROOM_ROLE_IDS
        }
        self.seat_role_combo.configure(values=tuple(self._seat_role_labels))
        self.seat_role_choice.set(
            next(
                label
                for label, role_id in self._seat_role_labels.items()
                if role_id == seat_role_id
            )
        )
        for column, key in (
            ("agent", "chat.seat_column.agent"),
            ("role", "chat.seat_column.role"),
            ("session", "chat.seat_column.session"),
            ("route", "chat.seat_column.route"),
            ("state", "chat.seat_column.state"),
        ):
            self.room_seat_tree.heading(column, text=self._t(key))
        self._on_seat_role_selected()
        self.manage_sources_button.configure(text=self._t("chat.manage_providers"))
        self.agent_install_frame.configure(text=self._t("agent_install.heading"))
        self.agent_install_intro.configure(text=self._t("agent_install.intro"))
        self.agent_install_detect_button.configure(
            text=self._t("agent_install.detect_all")
        )
        for spec in installable_agent_specs():
            self._agent_install_docs_buttons[spec.agent_id].configure(
                text=self._t("agent_install.docs").format(name=spec.display_name)
            )
            status = self._agent_install_statuses.get(spec.agent_id)
            if spec.agent_id in self._agent_install_processes:
                status_text = self._t("agent_install.running")
            elif status is None:
                status_text = self._t("agent_install.detecting")
            elif status.installed:
                status_text = self._t("agent_install.installed").format(
                    version=status.version
                    or self._t("agent_install.version_unknown")
                )
                status_text += "  " + self._t(spec.note_key)
            else:
                status_text = (
                    self._t("agent_install.not_installed")
                    + "  "
                    + self._t(spec.note_key)
                )
            self.agent_install_status[spec.agent_id].set(status_text)
            action_key = (
                "agent_install.update"
                if status is not None and status.installed
                else "agent_install.install"
            )
            if not spec.automatic_install_supported:
                action_key = "agent_install.open_guide"
            self._agent_install_buttons[spec.agent_id].configure(
                text=self._t(action_key).format(name=spec.display_name)
            )
        self.connect_heading_label.configure(text=self._t("connect.heading"))
        self.connect_privacy_label.configure(text=self._t("connect.privacy"))
        self.native_connection_frame.configure(
            text=self._t("connect.native_heading")
        )
        self._apply_connection_key_visibility()
        for key, button in self.native_connection_buttons.items():
            button.configure(text=self._t(key))
        self.ccswitch_connection_frame.configure(
            text=self._t("connect.ccswitch_heading")
        )
        for key, button in self.ccswitch_connection_buttons.items():
            button.configure(text=self._t(key))
        if not self.send_in_progress:
            self.send_button.configure(text=self._t("chat.send"))
        if self._catalog_value_matches(
            self.message_subject.get(), "chat.default_subject"
        ):
            self.message_subject.set(self._t("chat.default_subject"))
        if self._catalog_value_matches(
            self.room_status.get(), "chat.rooms_loading"
        ):
            self.room_status.set(self._t("chat.rooms_loading"))
        if self._catalog_value_matches(
            self.discussion_status.get(), "chat.discussion.idle"
        ):
            self.discussion_status.set(self._t("chat.discussion.idle"))
        if self._catalog_value_matches(self.message_status.get(), "chat.message_hint"):
            self.message_status.set(self._t("chat.message_hint"))
        elif self._catalog_value_matches(
            self.message_status.get(), "chat.join_to_send"
        ):
            self.message_status.set(self._t("chat.join_to_send"))
        if self._catalog_value_matches(self.seat_status.get(), "chat.seat_hint"):
            self.seat_status.set(self._t("chat.seat_hint"))
        elif self._catalog_value_matches(self.seat_status.get(), "chat.lobby_hint"):
            self.seat_status.set(self._t("chat.lobby_hint"))
        if not self._chat_attachment_paths:
            self.chat_attachment_status.set(self._t("chat.no_attachments"))
        else:
            self.chat_attachment_status.set(
                self._t("chat.attachments_selected").format(
                    count=len(self._chat_attachment_paths)
                )
            )
        recipient_ids = tuple(
            value for value in self._recipient_ids.values() if value != "*"
        )
        broadcast_label = self._t(BROADCAST_LABEL)
        self._recipient_ids = {
            broadcast_label: "*",
            **{agent_id: agent_id for agent_id in recipient_ids},
        }
        self.recipient_combo.configure(values=(broadcast_label, *recipient_ids))
        self.message_recipient.set(
            broadcast_label if selected_recipient_id == "*" else selected_recipient_id
        )
        if selected_route_key is not None:
            self.message_provider_choice.set(self._t(selected_route_key))
        if seat_model_is_default:
            self.seat_model_choice.set(self._t(PROVIDER_DEFAULT_MODEL_LABEL))
        if seat_reasoning_is_default:
            self.seat_reasoning_choice.set(self._t(PROVIDER_DEFAULT_REASONING_LABEL))
        self.feedback_prompt_label.configure(text=self._t("feedback.prompt"))
        self.feedback_contact_label.configure(text=self._t("feedback.contact"))
        self.feedback_attach_button.configure(text=self._t("feedback.attach"))
        self.feedback_clear_button.configure(text=self._t("feedback.clear"))
        self.feedback_key_toggle.configure(text=self._t("feedback.include_key"))
        self.feedback_privacy_label.configure(text=self._feedback_privacy_text())
        self._schedule_feedback_reflow()
        if not self.feedback_in_progress:
            self.feedback_send_button.configure(text=self._t("feedback.send"))
        for variable, key in (
            (self.feedback_status, "feedback.status.initial"),
            (self.connection_status, "connection.status.initial"),
            (self.ccswitch_status, "ccswitch.status.initial"),
        ):
            if self._catalog_value_matches(variable.get(), key):
                variable.set(self._t(key))
        if not self._feedback_attachment_paths:
            self.feedback_attachment_status.set(
                self._t("feedback.attachment.none")
            )
        for key, label in self.usage_kpi_labels.items():
            label.configure(text=self._t(f"usage.{key}"))
        self.usage_period_label.configure(text=self._t("usage.period"))
        self.usage_timezone_label.configure(text=self._t("usage.timezone"))
        for period, button in self.usage_period_buttons.items():
            button.configure(text=self._t(f"usage.period.{period}"))
        self._sync_usage_period_buttons()
        self._sync_usage_section_titles()
        for column in USAGE_TABLE_COLUMNS:
            self.usage_tree.heading(column, text=self._t(f"usage.{column}"))
        self._sync_priority_choices()
        if self.snapshot is None:
            self.usage_note_label.configure(text=self._t("usage.note"))
        self.announcement_network_toggle.configure(text=self._t("announcement.network"))
        self.announcement_popup_toggle.configure(text=self._t("announcement.popup"))
        self.announcement_sync_button.configure(text=self._t("announcement.sync"))
        for column, key in (
            ("severity", "announcement.severity"),
            ("title", "announcement.title"),
            ("published", "announcement.published"),
        ):
            self.announcement_tree.tree.heading(column, text=self._t(key))
        self._render_announcements(self.search.get().strip().lower())
        self.cockpit.apply_locale()
        self.trust_workflows.apply_locale()
        for column, key in (
            ("type", "memory.heading.type"),
            ("scope", "memory.heading.scope"),
            ("authority", "memory.heading.authority"),
            ("applicability", "memory.heading.applicability"),
            ("status", "memory.heading.status"),
            ("title", "memory.heading.title"),
            ("supersession", "memory.heading.supersession"),
            ("time", "memory.heading.time"),
        ):
            self.memory_tree.tree.heading(column, text=self._t(key))
        self.library_selection.set(
            self._library_selection_text(self.seat_agent.get().strip() or None)
        )
        self._last_agent_canvas_signature = ""
        if self.snapshot is not None:
            self._render_presence()
        else:
            self._draw_agents(list(self._library_agents))
        self._refresh_guided_workflow_readiness()
        self.show_page(self.active_page)
        if save:
            try:
                self._save_ui_preferences()
            except (OSError, LocalizationError) as exc:
                self.update_status.set(
                    self._t("ui.preferences_error").format(error=clip(exc, 100))
                )
        if self._tutorial_window is not None and self._tutorial_window.winfo_exists():
            self._tutorial_window.destroy()
            self._tutorial_window = None
            self.show_tutorial()
        if self.snapshot is not None:
            self.render(force=True)
        if self.selected_room_id and not self.room_refresh_in_progress:
            self._last_room_view_signature = ""
            self.root.after_idle(lambda: self._request_room_refresh(force=True))

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        modern = ACTIVE_THEME == "modern"
        style.configure(
            "Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            foreground=COLORS["text"],
            rowheight=32 if modern else 30,
            borderwidth=0,
            font=(UI_FONT_FAMILY, 9),
        )
        style.map("Treeview", background=[("selected", COLORS["blue"])], foreground=[("selected", COLORS["black"])])
        style.configure(
            "Treeview.Heading",
            background=COLORS["panel_2"],
            foreground=COLORS["text"] if modern else COLORS["amber"],
            relief="flat" if modern else "raised",
            borderwidth=0 if modern else 1,
            font=(UI_FONT_FAMILY, 9, "bold"),
        )
        style.map("Treeview.Heading", background=[("active", COLORS["line"])])
        style.configure(
            "Vertical.TScrollbar",
            background=COLORS["line"],
            troughcolor=COLORS["panel"],
            borderwidth=0,
            relief="flat",
            arrowsize=12 if modern else 14,
        )
        style.configure(
            "TCombobox",
            fieldbackground=COLORS["panel"],
            background=COLORS["panel"],
            foreground=COLORS["text"],
            bordercolor=COLORS["line"],
            lightcolor=COLORS["line"],
            darkcolor=COLORS["line"],
            arrowsize=12,
            font=(UI_FONT_FAMILY, 9),
            arrowcolor=COLORS["text"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", COLORS["panel"]),
                ("disabled", COLORS["panel_2"]),
            ],
            foreground=[
                ("readonly", COLORS["text"]),
                ("disabled", COLORS["muted"]),
            ],
            selectbackground=[("readonly", COLORS["panel"])],
            selectforeground=[("readonly", COLORS["text"])],
            arrowcolor=[
                ("readonly", COLORS["text"]),
                ("disabled", COLORS["muted"]),
            ],
        )
        self.root.option_add("*TCombobox*Listbox.background", COLORS["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["blue"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", COLORS["black"])
        self.root.option_add("*TCombobox*Listbox.font", (UI_FONT_FAMILY, 9))
        self.root.option_add("*Menu.background", COLORS["panel"])
        self.root.option_add("*Menu.foreground", COLORS["text"])
        self.root.option_add("*Menu.activeBackground", COLORS["blue"])
        self.root.option_add("*Menu.activeForeground", COLORS["black"])
        style.configure(
            SIDEBAR_SCROLLBAR_STYLE,
            background=COLORS["line"],
            troughcolor=COLORS["panel"],
            borderwidth=0,
            relief="flat",
            arrowsize=12 if modern else 14,
        )
        style.map(
            SIDEBAR_SCROLLBAR_STYLE,
            background=[
                ("pressed", COLORS["line"]),
                ("disabled", COLORS["line"]),
                ("active", COLORS["line"]),
            ],
        )

    def _apply_modern_widget_treatment(self) -> None:
        """Flatten the live Tk hierarchy without changing the pixel layout contract."""

        if ACTIVE_THEME != "modern":
            return

        font_classes = {
            "Button",
            "Checkbutton",
            "Entry",
            "Label",
            "Labelframe",
            "Listbox",
            "Menubutton",
            "Message",
            "Radiobutton",
            "Scale",
            "Spinbox",
        }

        def modern_font(widget: tk.Misc) -> None:
            if widget.winfo_class() not in font_classes:
                return
            try:
                current = tkfont.Font(root=self.root, font=widget.cget("font")).actual()
                traits: list[str] = []
                if current.get("weight") == "bold":
                    traits.append("bold")
                if current.get("slant") == "italic":
                    traits.append("italic")
                if current.get("underline"):
                    traits.append("underline")
                if current.get("overstrike"):
                    traits.append("overstrike")
                font_spec: tuple[Any, ...] = (
                    MODERN_FONT_FAMILY,
                    int(current.get("size") or 9),
                )
                if traits:
                    font_spec += (" ".join(traits),)
                widget.configure(font=font_spec)
            except (KeyError, tk.TclError, TypeError, ValueError):
                return

        def walk(widget: tk.Misc) -> None:
            modern_font(widget)
            widget_class = widget.winfo_class()
            try:
                if widget_class in {"Frame", "Labelframe"}:
                    widget.configure(relief="flat", bd=0, highlightthickness=0)
                elif widget_class == "Button":
                    widget.configure(
                        relief="flat",
                        bd=0,
                        highlightthickness=0,
                        cursor="hand2",
                    )
                elif widget_class in {"Entry", "Text", "Listbox", "Spinbox"}:
                    widget.configure(
                        relief="flat",
                        bd=0,
                        highlightthickness=1,
                        highlightbackground=COLORS["line"],
                        highlightcolor=COLORS["blue"],
                    )
                elif widget_class == "Canvas" and int(widget.cget("highlightthickness")):
                    widget.configure(
                        highlightthickness=1,
                        highlightbackground=COLORS["line"],
                    )
                elif widget_class == "Panedwindow":
                    widget.configure(
                        relief="flat",
                        bd=0,
                        sashrelief="flat",
                        sashwidth=6,
                    )
            except (tk.TclError, TypeError, ValueError):
                pass
            for child in widget.winfo_children():
                walk(child)

        walk(self.root)

    def _build_layout(self) -> None:
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)
        sidebar_bg = (
            MODERN_SIDEBAR_BG if ACTIVE_THEME == "modern" else COLORS["black"]
        )
        sidebar = tk.Frame(
            self.root,
            bg=sidebar_bg,
            width=MODERN_SIDEBAR_WIDTH if ACTIVE_THEME == "modern" else SIDEBAR_WIDTH,
            bd=0 if ACTIVE_THEME == "modern" else 2,
            relief="flat" if ACTIVE_THEME == "modern" else "ridge",
        )
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        self.sidebar_frame = sidebar

        self.stats_label = self._localized_label(
            sidebar,
            "sidebar.waiting_database",
            bg=sidebar_bg,
            fg=COLORS["muted"],
            justify="left",
            anchor="sw",
            font=("Cascadia Mono", SIDEBAR_TEXT_SIZE),
        )
        if ACTIVE_THEME != "modern":
            self.stats_label.pack(side="bottom", fill="x", padx=14, pady=6)
        self.sidebar_scroll_hint = tk.Label(
            sidebar,
            text=self._t("sidebar.scroll_hint"),
            bg=sidebar_bg,
            fg=COLORS["amber"],
            justify="center",
            anchor="center",
            font=("Cascadia Mono", SIDEBAR_TEXT_SIZE, "bold"),
        )
        if ACTIVE_THEME != "modern":
            self.sidebar_scroll_hint.pack(side="bottom", fill="x", padx=10, pady=(2, 0))

        sidebar_scroll_host = tk.Frame(sidebar, bg=sidebar_bg)
        sidebar_scroll_host.pack(side="top", fill="both", expand=True)
        sidebar_scroll_host.grid_rowconfigure(0, weight=1)
        sidebar_scroll_host.grid_columnconfigure(0, weight=1)
        self.sidebar_canvas = tk.Canvas(
            sidebar_scroll_host,
            width=(
                MODERN_SIDEBAR_WIDTH if ACTIVE_THEME == "modern" else SIDEBAR_WIDTH
            )
            - SIDEBAR_SCROLLBAR_WIDTH
            - 8,
            bg=sidebar_bg,
            highlightthickness=0,
            bd=0,
        )
        self.sidebar_scrollbar = ttk.Scrollbar(
            sidebar_scroll_host,
            orient="vertical",
            command=self.sidebar_canvas.yview,
            style=SIDEBAR_SCROLLBAR_STYLE,
        )
        self.sidebar_canvas.configure(yscrollcommand=self.sidebar_scrollbar.set)
        self.sidebar_canvas.grid(row=0, column=0, sticky="nsew")
        self.sidebar_scrollbar.grid(row=0, column=1, sticky="ns", padx=(2, 1))
        sidebar_content = tk.Frame(self.sidebar_canvas, bg=sidebar_bg)
        self.sidebar_content = sidebar_content
        self.sidebar_window = self.sidebar_canvas.create_window(
            (0, 0), window=sidebar_content, anchor="nw"
        )
        sidebar_content.bind("<Configure>", self._sync_sidebar_scrollregion)
        self.sidebar_canvas.bind("<Configure>", self._resize_sidebar_viewport)
        self.sidebar_canvas.bind("<MouseWheel>", self._scroll_sidebar)
        sidebar_content.bind("<MouseWheel>", self._scroll_sidebar)

        brand_row = tk.Frame(sidebar_content, bg=sidebar_bg)
        brand_row.pack(
            fill="x",
            padx=18 if ACTIVE_THEME == "modern" else 14,
            pady=(17, 6) if ACTIVE_THEME == "modern" else (14, 4),
        )
        if ACTIVE_THEME == "modern" and self._sidebar_brand_icon is not None:
            tk.Label(
                brand_row,
                image=self._sidebar_brand_icon,
                bg=sidebar_bg,
                bd=0,
            ).pack(side="left", padx=(2, 9))
        title = tk.Label(
            brand_row,
            text=(
                "PeerBridge"
                if ACTIVE_THEME == "modern"
                else "PEERBRIDGE\nCONTROL ROOM"
            ),
            bg=sidebar_bg,
            fg=COLORS["text"] if ACTIVE_THEME == "modern" else COLORS["cyan"],
            justify="left",
            font=(
                UI_FONT_FAMILY,
                16 if ACTIVE_THEME == "modern" else 17,
                "bold",
            ),
        )
        title.pack(side="left", anchor="w")
        self.version_label = tk.Label(
            sidebar_content,
            text=(
                self._t("sidebar.version").format(version=APP_VERSION)
                + "\n"
                + self._t("sidebar.build").format(build=APP_BUILD_ID)
            ),
            bg=sidebar_bg,
            fg=COLORS["muted"],
            justify="left",
            font=("Cascadia Mono", SIDEBAR_TEXT_SIZE),
        )
        if ACTIVE_THEME != "modern":
            self.version_label.pack(anchor="w", padx=18, pady=(0, 8))

        agent_library_parent = sidebar_content
        if ACTIVE_THEME == "modern":
            agent_library_parent = tk.Frame(sidebar_content, bg=sidebar_bg)
            self.modern_agent_library_panel = agent_library_parent

        self.agent_library_label = tk.Label(
            agent_library_parent,
            text=self._t("sidebar.agent_library"),
            bg=sidebar_bg,
            fg=COLORS["amber"],
            font=("Cascadia Mono", 10, "bold"),
        )
        self.agent_library_label.pack(anchor="w", padx=18, pady=(0, 3))
        agent_canvas_host = tk.Frame(agent_library_parent, bg=sidebar_bg)
        agent_canvas_host.pack(padx=6, fill="x")
        self.agent_canvas = tk.Canvas(
            agent_canvas_host,
            width=AGENT_LIBRARY_CANVAS_WIDTH,
            height=AGENT_LIBRARY_CANVAS_HEIGHT,
            bg=COLORS["panel"],
            highlightthickness=2,
            highlightbackground=COLORS["line"],
        )
        self.agent_scrollbar = ttk.Scrollbar(
            agent_canvas_host,
            orient="vertical",
            command=self.agent_canvas.yview,
        )
        self.agent_canvas.configure(yscrollcommand=self.agent_scrollbar.set)
        self.agent_canvas.pack(side="left", fill="x", expand=True)
        self.agent_scrollbar.pack(side="right", fill="y", padx=(2, 0))
        self.agent_canvas.bind("<ButtonPress-1>", self._begin_library_drag)
        self.agent_canvas.bind("<B1-Motion>", self._move_library_drag)
        self.agent_canvas.bind("<ButtonRelease-1>", self._finish_library_drag)
        self.agent_canvas.bind("<Double-1>", self._add_library_agent_by_double_click)
        self.agent_canvas.bind("<MouseWheel>", self._scroll_agent_library)
        self._draw_agents([])
        self.library_selection_label = tk.Label(
            agent_library_parent,
            textvariable=self.library_selection,
            bg=sidebar_bg,
            fg=COLORS["muted"],
            anchor="w",
            font=("Cascadia Mono", SIDEBAR_TEXT_SIZE, "bold"),
        )
        self.library_selection_label.pack(fill="x", padx=18, pady=(3, 0))
        self.library_route_notice = tk.Label(
            agent_library_parent,
            text=self._t("sidebar.library_route_notice"),
            bg=sidebar_bg,
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=244,
            font=("Cascadia Mono", SIDEBAR_TEXT_SIZE),
        )
        self.library_route_notice.pack(fill="x", padx=18, pady=(3, 4))

        if ACTIVE_THEME == "modern":
            self.modern_sidebar_new_room_button = tk.Button(
                sidebar_content,
                text=self._t("modern.sidebar.new_room"),
                command=self._open_create_room_dialog,
                bg=COLORS["panel"],
                fg=COLORS["text"],
                activebackground=COLORS["panel_2"],
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                highlightthickness=1,
                highlightbackground=COLORS["line"],
                anchor="w",
                padx=12,
                pady=9,
                font=(MODERN_FONT_FAMILY, 10, "bold"),
            )
            self.modern_sidebar_new_room_button.pack(
                fill="x", padx=14, pady=(6, 12)
            )

            self.modern_projects_title = tk.Label(
                sidebar_content,
                text=self._t("modern.projects.title"),
                bg=sidebar_bg,
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            )
            self.modern_projects_title.pack(fill="x", padx=22, pady=(1, 4))
            project_row = tk.Frame(
                sidebar_content,
                bg=COLORS["panel"],
                highlightthickness=1,
                highlightbackground=COLORS["line"],
            )
            project_row.pack(fill="x", padx=14, pady=(0, 12))
            tk.Label(
                project_row,
                text="●",
                bg=COLORS["panel"],
                fg=COLORS["green"],
                font=(MODERN_FONT_FAMILY, 7),
            ).pack(side="left", padx=(10, 7), pady=9)
            project_text = tk.Frame(project_row, bg=COLORS["panel"])
            project_text.pack(side="left", fill="x", expand=True, pady=7)
            self.modern_project_scope_label = tk.Label(
                project_text,
                text=clip(self.scope, 26),
                bg=COLORS["panel"],
                fg=COLORS["text"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 9, "bold"),
            )
            self.modern_project_scope_label.pack(fill="x")
            self.modern_project_status_label = tk.Label(
                project_text,
                text=self._t("modern.projects.local_active"),
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8),
            )
            self.modern_project_status_label.pack(fill="x")
            tk.Label(
                project_row,
                text="›",
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                font=(MODERN_FONT_FAMILY, 12),
            ).pack(side="right", padx=(4, 10))

        nav = tk.Frame(sidebar_content, bg=sidebar_bg)
        nav.pack(fill="x", padx=12, pady=(2, 2))
        self.nav_frame = nav
        self.modern_nav_group_labels: dict[str, tk.Label] = {}

        def add_nav_button(key: str, parent: tk.Misc = nav) -> None:
            button = tk.Button(
                parent,
                text=self._navigation_label(key),
                command=lambda value=key: self.show_page(value),
                anchor="w",
                bg=sidebar_bg if ACTIVE_THEME == "modern" else COLORS["panel"],
                fg=COLORS["text"],
                activebackground=COLORS["cyan"],
                activeforeground=COLORS["black"],
                relief="flat" if ACTIVE_THEME == "modern" else "raised",
                bd=0 if ACTIVE_THEME == "modern" else 2,
                highlightthickness=1 if ACTIVE_THEME == "modern" else 0,
                highlightbackground=sidebar_bg,
                highlightcolor=COLORS["line"],
                padx=13 if ACTIVE_THEME == "modern" else 11,
                pady=9 if ACTIVE_THEME == "modern" else 1,
                font=(UI_FONT_FAMILY, 10, "bold" if ACTIVE_THEME != "modern" else "normal"),
            )
            button.pack(fill="x", pady=1)
            button.bind("<MouseWheel>", self._scroll_sidebar)
            self.nav_buttons[key] = button

        if ACTIVE_THEME == "modern":
            if not modern_navigation_is_complete():
                raise RuntimeError("Modern navigation is missing a PeerBridge page")
            workspace_label = tk.Label(
                nav,
                text=self._t("modern.nav.workspace"),
                bg=sidebar_bg,
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            )
            workspace_label.pack(fill="x", padx=9, pady=(1, 5))
            self.modern_nav_group_labels["workspace"] = workspace_label
            primary_pages = ("cockpit", "chat", "work", "review", "usage")
            for key in primary_pages:
                add_nav_button(key)

            self.modern_more_nav_button = tk.Button(
                nav,
                text=self._t("modern.nav.more"),
                command=self._toggle_modern_more_nav,
                bg=sidebar_bg,
                fg=COLORS["muted"],
                activebackground=COLORS["panel_2"],
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                anchor="w",
                padx=11,
                pady=7,
                font=(MODERN_FONT_FAMILY, 9),
            )
            self.modern_more_nav_button.pack(fill="x", pady=(2, 0))
            self.modern_more_nav_frame = tk.Frame(nav, bg=sidebar_bg)
            for group_key, page_keys in MODERN_NAV_GROUPS:
                group_label = tk.Label(
                    self.modern_more_nav_frame,
                    text=self._t(f"modern.nav.{group_key}"),
                    bg=sidebar_bg,
                    fg=COLORS["muted"],
                    anchor="w",
                    font=(UI_FONT_FAMILY, 8, "bold"),
                )
                group_label.pack(fill="x", padx=9, pady=(10, 3))
                self.modern_nav_group_labels[group_key] = group_label
                for key in page_keys:
                    if key not in primary_pages:
                        add_nav_button(key, self.modern_more_nav_frame)

            self.modern_recent_rooms_title = tk.Label(
                sidebar_content,
                text=self._t("modern.rooms.title"),
                bg=sidebar_bg,
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            )
            self.modern_recent_rooms_title.pack(
                fill="x", padx=22, pady=(16, 4)
            )
            self.modern_recent_rooms_frame = tk.Frame(
                sidebar_content, bg=sidebar_bg
            )
            self.modern_recent_rooms_frame.pack(fill="x", padx=12)
            self._sync_modern_recent_rooms()

            self.modern_agent_library_button = tk.Button(
                sidebar_content,
                text=self._t("modern.sidebar.show_agents"),
                command=self._toggle_modern_agent_library,
                bg=sidebar_bg,
                fg=COLORS["muted"],
                activebackground=COLORS["panel_2"],
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                anchor="w",
                padx=10,
                pady=7,
                font=(MODERN_FONT_FAMILY, 9),
            )
            self.modern_agent_library_button.pack(
                fill="x", padx=12, pady=(10, 0)
            )

            account = tk.Frame(sidebar_content, bg=sidebar_bg)
            account.pack(fill="x", padx=16, pady=(18, 12))
            tk.Frame(account, bg=COLORS["line"], height=1).pack(
                fill="x", pady=(0, 10)
            )
            avatar = tk.Label(
                account,
                text="HY",
                bg="#071d38",
                fg="#ffffff",
                width=3,
                height=1,
                font=(MODERN_FONT_FAMILY, 9, "bold"),
            )
            avatar.pack(side="left", padx=(0, 9), ipady=5)
            account_text = tk.Frame(account, bg=sidebar_bg)
            account_text.pack(side="left", fill="x", expand=True)
            tk.Label(
                account_text,
                text="Hoylon",
                bg=sidebar_bg,
                fg=COLORS["text"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 9, "bold"),
            ).pack(fill="x")
            self.modern_account_role = tk.Label(
                account_text,
                text=self._t("modern.account.role"),
                bg=sidebar_bg,
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8),
            )
            self.modern_account_role.pack(fill="x")
        else:
            for key in TUTORIAL_PAGE_KEYS:
                add_nav_button(key)

        main = tk.Frame(
            self.root,
            bg=MODERN_WORKSPACE_BG if ACTIVE_THEME == "modern" else COLORS["bg"],
        )
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)
        self.main_frame = main

        toolbar = tk.Frame(
            main,
            bg=COLORS["panel"],
            bd=0 if ACTIVE_THEME == "modern" else 2,
            relief="flat" if ACTIVE_THEME == "modern" else "ridge",
            height=60 if ACTIVE_THEME == "modern" else 64,
            highlightthickness=1 if ACTIVE_THEME == "modern" else 0,
            highlightbackground=COLORS["line"],
        )
        toolbar.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=0 if ACTIVE_THEME == "modern" else 12,
            pady=0 if ACTIVE_THEME == "modern" else (12, 8),
        )
        toolbar.grid_columnconfigure(2, weight=1)
        self.toolbar_frame = toolbar
        self.page_title = tk.Label(
            toolbar,
            text=self._t("page.cockpit"),
            bg=COLORS["panel"],
            fg=COLORS["text"] if ACTIVE_THEME == "modern" else COLORS["amber"],
            font=(
                UI_FONT_FAMILY,
                16 if ACTIVE_THEME == "modern" else 14,
                "bold",
            ),
        )
        self.page_title.grid(
            row=0,
            column=0,
            padx=26 if ACTIVE_THEME == "modern" else 14,
            pady=14 if ACTIVE_THEME == "modern" else 12,
            sticky="w",
        )
        self.search_label = tk.Label(
            toolbar,
            text=self._t("toolbar.search"),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Cascadia Mono", 9, "bold"),
        )
        self.search_label.grid(row=0, column=1, padx=(6, 0))
        search_entry = tk.Entry(
            toolbar,
            textvariable=self.search,
            bg=COLORS["black"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="flat" if ACTIVE_THEME == "modern" else "sunken",
            bd=0 if ACTIVE_THEME == "modern" else 2,
            highlightthickness=1 if ACTIVE_THEME == "modern" else 0,
            highlightbackground=COLORS["line"],
            highlightcolor=COLORS["blue"],
            font=(UI_FONT_FAMILY, 10),
        )
        search_entry.grid(row=0, column=2, sticky="ew", padx=10, ipady=6)
        self.pause_button = tk.Button(
            toolbar,
            text=self._t("toolbar.pause"),
            command=self.toggle_pause,
            bg=COLORS["purple"],
            fg=COLORS["black"],
            activebackground=COLORS["amber"],
            relief="raised",
            bd=2,
            padx=10,
            pady=5,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.pause_button.grid(row=0, column=3, padx=5)
        refresh_cluster = tk.Frame(toolbar, bg=COLORS["panel"])
        refresh_cluster.grid(row=0, column=4, padx=(5, 12), pady=(4, 2), sticky="e")
        self.refresh_button = tk.Button(
            refresh_cluster,
            text=self._t("toolbar.refresh"),
            command=lambda: self.refresh(force=True),
            bg=COLORS["green"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=10,
            pady=5,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.refresh_button.pack(anchor="e")
        self.refresh_status_label = tk.Label(
            refresh_cluster,
            textvariable=self.refresh_status,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="e",
            font=("Cascadia Mono", 7),
        )
        self.refresh_status_label.pack(anchor="e", pady=(2, 0))

        language_cluster = tk.Frame(toolbar, bg=COLORS["panel"])
        language_cluster.grid(row=1, column=0, padx=(14, 5), pady=(0, 10), sticky="w")
        self.language_label = tk.Label(
            language_cluster,
            text=self._t("toolbar.language"),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Cascadia Mono", 8, "bold"),
        )
        self.language_label.pack(side="left", padx=(0, 6))
        self.locale_combo = ttk.Combobox(
            language_cluster,
            textvariable=self.locale_label,
            values=tuple(LOCALE_LABELS.values()),
            state="readonly",
            width=12,
            font=("Cascadia Mono", 9),
        )
        self.locale_combo.pack(side="left")
        self.locale_combo.bind("<<ComboboxSelected>>", self._locale_changed)
        self.theme_title_label = tk.Label(
            language_cluster,
            text=self._t("toolbar.theme"),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Cascadia Mono", 8, "bold"),
        )
        self.theme_title_label.pack(side="left", padx=(14, 6))
        self.theme_combo = ttk.Combobox(
            language_cluster,
            textvariable=self.theme_choice,
            values=tuple(
                THEME_LABELS[self.locale.get()][key] for key in SUPPORTED_THEMES
            ),
            state="readonly",
            width=18,
            font=("Cascadia Mono", 9),
        )
        self.theme_combo.pack(side="left")
        self.theme_combo.bind("<<ComboboxSelected>>", self._theme_changed)
        self.help_button = tk.Button(
            toolbar,
            text=self._t("toolbar.help"),
            command=self.show_tutorial,
            bg=COLORS["blue"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=8,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.help_button.grid(row=1, column=1, padx=5, pady=(0, 10))
        self.update_status_label = tk.Label(
            toolbar,
            textvariable=self.update_status,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="e",
            font=("Cascadia Mono", 8),
        )
        self.update_status_label.grid(row=1, column=2, padx=8, pady=(0, 10), sticky="ew")
        self.announcement_button = tk.Button(
            toolbar,
            text=self._t("toolbar.announcements"),
            command=self.open_announcements,
            bg=COLORS["line"],
            fg=COLORS["text"],
            activebackground=COLORS["cyan"],
            activeforeground=COLORS["black"],
            relief="raised",
            bd=2,
            padx=8,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.announcement_button.grid(
            row=1, column=3, padx=(5, 3), pady=(0, 10), sticky="e"
        )
        self.update_button = tk.Button(
            toolbar,
            text=self._t("toolbar.updates"),
            command=self.check_updates,
            bg=COLORS["line"],
            fg=COLORS["text"],
            activebackground=COLORS["cyan"],
            activeforeground=COLORS["black"],
            relief="raised",
            bd=2,
            padx=8,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.update_button.grid(
            row=1, column=4, padx=(3, 12), pady=(0, 10), sticky="e"
        )

        if ACTIVE_THEME == "modern":
            for column in range(6):
                toolbar.grid_columnconfigure(column, weight=0)
            toolbar.grid_columnconfigure(0, weight=1)
            self.page_title.grid_configure(
                row=0, column=0, padx=(24, 12), pady=15, sticky="w"
            )
            self.modern_toolbar_scope_label = tk.Label(
                toolbar,
                text=self.scope,
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                font=(MODERN_FONT_FAMILY, 9),
            )
            self.modern_toolbar_scope_label.grid(
                row=0, column=1, padx=8, pady=12, sticky="e"
            )
            self.modern_toolbar_connection_label = tk.Label(
                toolbar,
                text=self._t("modern.toolbar.connected"),
                bg=COLORS["panel"],
                fg=COLORS["green"],
                font=(MODERN_FONT_FAMILY, 9, "bold"),
            )
            self.modern_toolbar_connection_label.grid(
                row=0, column=2, padx=8, pady=12, sticky="e"
            )
            self.modern_toolbar_options_button = tk.Button(
                toolbar,
                text=self._t("modern.toolbar.options"),
                command=self._toggle_modern_toolbar_options,
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                activebackground=COLORS["panel_2"],
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                padx=10,
                pady=6,
                font=(MODERN_FONT_FAMILY, 9),
            )
            self.modern_toolbar_options_button.grid(
                row=0, column=3, padx=(4, 2), pady=8, sticky="e"
            )

            self.search_label.grid_configure(
                row=1, column=0, padx=(4, 4), pady=(4, 6), sticky="w"
            )
            search_entry.grid_configure(
                row=1,
                column=1,
                columnspan=2,
                sticky="ew",
                padx=6,
                pady=(4, 6),
            )
            self.pause_button.grid_configure(
                row=1, column=3, padx=4, pady=(4, 6), sticky="e"
            )
            refresh_cluster.grid_configure(
                row=1, column=4, padx=(4, 2), pady=(4, 6), sticky="e"
            )
            language_cluster.grid_configure(
                row=2,
                column=0,
                columnspan=2,
                padx=(4, 6),
                pady=(2, 6),
                sticky="w",
            )
            self.help_button.grid_configure(
                row=2, column=2, padx=4, pady=(2, 6), sticky="w"
            )
            self.announcement_button.grid_configure(
                row=2, column=3, padx=4, pady=(2, 6), sticky="e"
            )
            self.update_button.grid_configure(
                row=2, column=4, padx=(4, 2), pady=(2, 6), sticky="e"
            )
            self.update_status_label.grid_configure(
                row=3,
                column=0,
                columnspan=5,
                padx=4,
                pady=(0, 8),
                sticky="ew",
            )
            self.modern_toolbar_option_widgets = (
                self.search_label,
                search_entry,
                self.pause_button,
                refresh_cluster,
                language_cluster,
                self.help_button,
                self.announcement_button,
                self.update_button,
                self.update_status_label,
            )
            for widget in self.modern_toolbar_option_widgets:
                widget.grid_remove()

        page_host = tk.Frame(
            main,
            bg=MODERN_WORKSPACE_BG if ACTIVE_THEME == "modern" else COLORS["bg"],
        )
        page_host.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=0 if ACTIVE_THEME == "modern" else 12,
            pady=0 if ACTIVE_THEME == "modern" else (0, 12),
        )
        page_host.grid_rowconfigure(0, weight=1)
        page_host.grid_columnconfigure(0, weight=1)
        self.page_host = page_host

        self.cockpit = AgentCockpit(
            page_host,
            project_root=self.project_root,
            translate=self._t,
            colors=COLORS,
            external_sessions=self._cockpit_external_sessions,
            room_sender=self._send_cockpit_room_message,
            external_input_complete=self._finish_cockpit_room_input,
        )
        self.cockpit.set_room_context(self.selected_room_id)
        self.pages["cockpit"] = self.cockpit.frame
        self._build_chat_page(page_host)
        self.work_tree = self._make_tree_page(
            page_host,
            "work",
            [("task", "TASK", 220), ("owner", "OWNER", 110), ("status", "STATUS", 95), ("summary", "SUMMARY", 400), ("time", "UPDATED", 120)],
        )
        self.review_tree = self._make_tree_page(
            page_host,
            "review",
            [("request", "REQUEST", 210), ("reviewer", "REVIEWER", 120), ("verdict", "VERDICT", 90), ("score", "SCORE", 60), ("findings", "FINDINGS", 430), ("time", "TIME", 115)],
        )
        self.change_tree = self._make_tree_page(
            page_host,
            "change",
            [("actor", "ACTOR", 110), ("task", "TASK", 220), ("summary", "CHANGE SUMMARY", 520), ("time", "TIME", 115), ("hash", "SHA", 110)],
        )
        self.audit_tree = self._make_tree_page(
            page_host,
            "audit",
            [("actor", "ACTOR", 110), ("type", "EVENT", 150), ("task", "TASK", 220), ("payload", "PAYLOAD", 430), ("time", "TIME", 115)],
        )
        self._build_connections_page(page_host)
        self.memory_tree = self._make_tree_page(
            page_host,
            "memory",
            [
                ("type", "TYPE", 90),
                ("scope", "SCOPE", 80),
                ("authority", "AUTHORITY", 125),
                ("applicability", "APPLIES TO", 145),
                ("status", "STATUS", 80),
                ("title", "TITLE", 260),
                ("supersession", "SUPERSESSION", 150),
                ("time", "CREATED", 120),
            ],
        )
        self.trust_workflows = TrustWorkflowsPage(
            page_host,
            project_root=self.project_root,
            translate=self._t,
            colors=COLORS,
            execute_tool=self._execute_trust_tool,
        )
        self.pages["trust"] = self.trust_workflows.frame
        self._build_feedback_page(page_host)
        self._build_usage_page(page_host)
        self._build_announcements_page(page_host)
        self._apply_modern_widget_treatment()
        self.show_page("cockpit")

    def _draw_tutorial_diagram(self, canvas: tk.Canvas, page_key: str) -> None:
        layout, markers = tutorial_diagram_spec(page_key)
        canvas.delete("all")
        width = max(560, canvas.winfo_width())
        height = max(230, canvas.winfo_height())

        def box(
            left: float,
            top: float,
            right: float,
            bottom: float,
            *,
            fill: str = COLORS["panel_2"],
            outline: str = COLORS["line"],
            line_width: int = 1,
        ) -> None:
            canvas.create_rectangle(
                left * width,
                top * height,
                right * width,
                bottom * height,
                fill=fill,
                outline=outline,
                width=line_width,
            )

        box(0.02, 0.04, 0.98, 0.96, fill=COLORS["black"], line_width=2)
        if layout == "cockpit":
            box(0.05, 0.09, 0.95, 0.27)
            box(0.05, 0.32, 0.33, 0.90)
            box(0.36, 0.32, 0.64, 0.90)
            box(0.67, 0.32, 0.95, 0.90)
            for left in (0.08, 0.39, 0.70):
                box(left, 0.72, left + 0.21, 0.84, fill=COLORS["panel"])
        elif layout == "chat":
            box(0.05, 0.09, 0.95, 0.25)
            box(0.05, 0.30, 0.25, 0.72)
            box(0.28, 0.30, 0.95, 0.72)
            box(0.05, 0.77, 0.95, 0.91, fill=COLORS["panel"])
            for top in (0.37, 0.50, 0.63):
                box(0.33, top, 0.78 if top != 0.50 else 0.90, top + 0.07)
        elif layout == "table":
            box(0.05, 0.09, 0.95, 0.24)
            box(0.05, 0.29, 0.95, 0.67)
            for top in (0.37, 0.46, 0.55):
                canvas.create_line(
                    0.07 * width,
                    top * height,
                    0.93 * width,
                    top * height,
                    fill=COLORS["line"],
                )
            box(0.05, 0.72, 0.95, 0.91, fill=COLORS["panel"])
        elif layout == "connect":
            box(0.05, 0.09, 0.95, 0.34)
            for top in (0.16, 0.23, 0.30):
                canvas.create_line(
                    0.08 * width,
                    top * height,
                    0.92 * width,
                    top * height,
                    fill=COLORS["line"],
                )
            box(0.05, 0.39, 0.95, 0.68)
            box(0.05, 0.73, 0.95, 0.91, fill=COLORS["panel"])
        elif layout == "trust":
            for index in range(5):
                box(0.05 + index * 0.18, 0.09, 0.21 + index * 0.18, 0.22)
            box(0.05, 0.28, 0.45, 0.88)
            box(0.49, 0.28, 0.95, 0.63)
            box(0.49, 0.68, 0.95, 0.88, fill=COLORS["panel"])
        elif layout == "feedback":
            box(0.05, 0.09, 0.62, 0.28)
            box(0.05, 0.33, 0.62, 0.68)
            box(0.66, 0.09, 0.95, 0.68)
            box(0.05, 0.73, 0.95, 0.91, fill=COLORS["panel"])
        elif layout == "usage":
            for index in range(4):
                box(0.05 + index * 0.225, 0.09, 0.25 + index * 0.225, 0.22)
                box(0.05 + index * 0.225, 0.28, 0.25 + index * 0.225, 0.48)
            box(0.05, 0.54, 0.58, 0.91)
            points = (
                0.09 * width,
                0.83 * height,
                0.20 * width,
                0.72 * height,
                0.31 * width,
                0.76 * height,
                0.43 * width,
                0.62 * height,
                0.54 * width,
                0.67 * height,
            )
            canvas.create_line(*points, fill=COLORS["cyan"], width=3)
            box(0.62, 0.54, 0.95, 0.91, fill=COLORS["panel"])
        else:
            box(0.05, 0.09, 0.95, 0.27)
            box(0.05, 0.32, 0.42, 0.90)
            box(0.46, 0.32, 0.95, 0.90, fill=COLORS["panel"])

        radius = 13
        for number, (x_fraction, y_fraction) in enumerate(markers, start=1):
            x = x_fraction * width
            y = y_fraction * height
            canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                fill=COLORS["amber"],
                outline=COLORS["black"],
                width=2,
            )
            canvas.create_text(
                x,
                y,
                text=str(number),
                fill=COLORS["black"],
                font=("Cascadia Mono", 10, "bold"),
            )

    def show_tutorial(self, page_key: str | None = None) -> None:
        selected_page = page_key or getattr(self, "active_page", "cockpit")
        if selected_page not in TUTORIAL_PAGE_KEYS:
            selected_page = "cockpit"
        if self._tutorial_is_open():
            assert self._tutorial_window is not None
            if self._tutorial_select_page is not None:
                self._tutorial_select_page(selected_page)
            self._tutorial_window.deiconify()
            self._tutorial_window.lift()
            self._tutorial_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self._tutorial_window = window
        window.title(self._t("tutorial.title"))
        self._place_transient(window, 1080, 780)
        window.minsize(900, 680)
        window.configure(bg=COLORS["bg"])
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_tutorial(False))

        shell = tk.Frame(window, bg=COLORS["bg"])
        shell.pack(fill="both", expand=True, padx=18, pady=18)
        sidebar = tk.Frame(shell, bg=COLORS["black"], width=235, bd=2, relief="ridge")
        sidebar.pack(side="left", fill="y", padx=(0, 14))
        sidebar.pack_propagate(False)
        tk.Label(
            sidebar,
            text=self._t("tutorial.all_panels"),
            bg=COLORS["black"],
            fg=COLORS["cyan"],
            anchor="w",
            justify="left",
            wraplength=205,
            font=("Cascadia Mono", 12, "bold"),
        ).pack(fill="x", padx=12, pady=(14, 10))

        content = tk.Frame(shell, bg=COLORS["bg"])
        content.pack(side="left", fill="both", expand=True)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(5, weight=1)
        progress_value = tk.StringVar()
        title_value = tk.StringVar()
        purpose_value = tk.StringVar()
        body_value = tk.StringVar()
        tk.Label(
            content,
            textvariable=progress_value,
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            anchor="w",
            font=("Cascadia Mono", 10, "bold"),
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            content,
            textvariable=title_value,
            bg=COLORS["bg"],
            fg=COLORS["amber"],
            anchor="w",
            font=("Cascadia Mono", 15, "bold"),
        ).grid(row=1, column=0, sticky="ew", pady=(2, 8))
        purpose = tk.Frame(content, bg=COLORS["panel_2"], bd=1, relief="ridge")
        purpose.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        purpose.grid_columnconfigure(1, weight=1)
        tk.Label(
            purpose,
            text=self._t("tutorial.purpose_label"),
            bg=COLORS["panel_2"],
            fg=COLORS["cyan"],
            anchor="w",
            font=("Cascadia Mono", 10, "bold"),
        ).grid(row=0, column=0, sticky="nw", padx=(10, 12), pady=8)
        tk.Label(
            purpose,
            textvariable=purpose_value,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            anchor="w",
            justify="left",
            wraplength=570,
            font=("Cascadia Mono", 10),
        ).grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=8)
        diagram = tk.Canvas(
            content,
            height=260,
            bg=COLORS["panel"],
            highlightthickness=2,
            highlightbackground=COLORS["line"],
        )
        diagram.grid(row=3, column=0, sticky="nsew")
        tk.Label(
            content,
            text=self._t("tutorial.diagram_note"),
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=760,
            font=("Cascadia Mono", 10),
        ).grid(row=4, column=0, sticky="ew", pady=(6, 8))
        tk.Label(
            content,
            textvariable=body_value,
            bg=COLORS["bg"],
            fg=COLORS["text"],
            anchor="nw",
            justify="left",
            wraplength=760,
            font=("Cascadia Mono", TUTORIAL_BODY_TEXT_SIZE),
        ).grid(row=5, column=0, sticky="nsew")

        controls = tk.Frame(content, bg=COLORS["bg"])
        controls.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        skip_button = tk.Button(
            controls,
            text=self._t("tutorial.skip"),
            command=lambda: self._close_tutorial(True),
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            relief="raised",
            bd=2,
            font=("Cascadia Mono", 9, "bold"),
        )
        skip_button.pack(side="left", padx=(0, 5))
        open_button = tk.Button(
            controls,
            text=self._t("tutorial.open_panel"),
            bg=COLORS["blue"],
            fg=COLORS["black"],
            relief="raised",
            bd=2,
            font=("Cascadia Mono", 9, "bold"),
        )
        open_button.pack(side="left", padx=5)
        next_button = tk.Button(
            controls,
            bg=COLORS["green"],
            fg=COLORS["black"],
            relief="raised",
            bd=2,
            font=("Cascadia Mono", 9, "bold"),
        )
        next_button.pack(side="right", padx=(5, 0))
        previous_button = tk.Button(
            controls,
            text=self._t("tutorial.previous_panel"),
            bg=COLORS["line"],
            fg=COLORS["text"],
            relief="raised",
            bd=2,
            font=("Cascadia Mono", 9, "bold"),
        )
        previous_button.pack(side="right", padx=5)

        nav_buttons: dict[str, tk.Button] = {}
        current_page = {"key": selected_page}

        def render_page(key: str) -> None:
            if key not in TUTORIAL_PAGE_KEYS:
                return
            current_page["key"] = key
            index = TUTORIAL_PAGE_KEYS.index(key)
            title_value.set(self._t(f"page.{key}"))
            progress_value.set(
                self._t("tutorial.panel_progress").format(
                    current=index + 1,
                    total=len(TUTORIAL_PAGE_KEYS),
                )
            )
            purpose_value.set(self._t(f"tutorial.panel.{key}.purpose"))
            body_value.set(self._t(f"tutorial.panel.{key}.body"))
            for nav_key, button in nav_buttons.items():
                active = nav_key == key
                button.configure(
                    bg=COLORS["cyan"] if active else COLORS["panel"],
                    fg=COLORS["black"] if active else COLORS["text"],
                )
            previous_button.configure(
                state="normal" if index else "disabled",
                command=lambda: render_page(TUTORIAL_PAGE_KEYS[index - 1]),
            )
            if index == len(TUTORIAL_PAGE_KEYS) - 1:
                next_button.configure(
                    text=self._t("tutorial.done"),
                    command=lambda: self._close_tutorial(True),
                )
            else:
                next_button.configure(
                    text=self._t("tutorial.next_panel"),
                    command=lambda: render_page(TUTORIAL_PAGE_KEYS[index + 1]),
                )
            open_button.configure(
                command=lambda: (
                    self.show_page(current_page["key"]),
                    self._close_tutorial(True),
                )
            )
            self._draw_tutorial_diagram(diagram, key)

        for key in TUTORIAL_PAGE_KEYS:
            button = tk.Button(
                sidebar,
                text=self._t(f"nav.{key}"),
                command=lambda value=key: render_page(value),
                anchor="w",
                justify="left",
                wraplength=200,
                bg=COLORS["panel"],
                fg=COLORS["text"],
                activebackground=COLORS["cyan"],
                activeforeground=COLORS["black"],
                relief="raised",
                bd=2,
                padx=8,
                pady=2,
                font=("Cascadia Mono", 9, "bold"),
            )
            button.pack(fill="x", padx=8, pady=1)
            nav_buttons[key] = button

        self._tutorial_select_page = render_page
        diagram.bind(
            "<Configure>",
            lambda _event: self._draw_tutorial_diagram(
                diagram, current_page["key"]
            ),
        )
        render_page(selected_page)

    def _close_tutorial(self, completed: bool) -> None:
        if completed:
            self.tutorial_completed = True
            try:
                self._save_ui_preferences()
            except (OSError, LocalizationError) as exc:
                self.update_status.set(
                    self._t("ui.preferences_error").format(error=clip(exc, 100))
                )
        if self._tutorial_window is not None:
            with contextlib.suppress(tk.TclError):
                self._tutorial_window.destroy()
        self._tutorial_window = None
        self._tutorial_select_page = None
        pending = self._pending_announcement_popup_rows
        self._pending_announcement_popup_rows = ()
        if pending and not self._closing:
            self.root.after_idle(
                lambda rows=pending: self._show_announcement_popup(list(rows))
            )

    def check_updates(self) -> None:
        if self.update_in_progress:
            return
        self.update_in_progress = True
        self.update_button.configure(state="disabled")
        self.update_status.set(self._t("updates.checking"))

        def worker() -> None:
            try:
                result = check_for_updates(
                    current_version=APP_VERSION,
                    current_build_sha256=(
                        APP_BUILD_SHA256
                        if APP_BUILD_SHA256 != "unavailable"
                        else None
                    ),
                )
                self._post_to_ui(self._update_finished, result, None)
            except Exception as exc:
                self._post_to_ui(self._update_finished, None, exc)

        threading.Thread(target=worker, name="peerbridge-update-check", daemon=True).start()

    def _update_finished(
        self,
        result: UpdateCheckResult | None,
        error: Exception | None,
    ) -> None:
        self.update_in_progress = False
        self.update_button.configure(state="normal")
        if error is not None:
            self.update_status.set(
                self._t("updates.error").format(error=clip(error, 90))
            )
            return
        assert result is not None
        if result.update_available:
            status_key = (
                "updates.available_build"
                if result.same_version_build_update
                else "updates.available"
            )
            self.update_status.set(
                self._t(status_key).format(version=result.latest_version)
            )
            if messagebox.askyesno(
                self._t("toolbar.updates"),
                self._t(status_key).format(version=result.latest_version)
                + "\n\n"
                + result.release_url
                + "\n\n"
                + self._t("updates.open_release"),
                parent=self.root,
            ):
                webbrowser.open(result.release_url, new=2)
        else:
            status_key = (
                "updates.current_release"
                if result.current_release_published
                else "updates.current_local_build"
            )
            self.update_status.set(
                self._t(status_key).format(
                    version=result.current_version,
                    build=APP_BUILD_ID,
                )
            )

    @staticmethod
    def _announcement_key(item: Announcement) -> str:
        return f"{item.locale}:{item.announcement_id}"

    def _build_announcements_page(self, host: tk.Frame) -> None:
        page = tk.Frame(host, bg=COLORS["panel"], bd=2, relief="ridge")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self.pages["announcement"] = page

        controls = tk.Frame(page, bg=COLORS["panel_2"], bd=1, relief="ridge")
        controls.grid(row=0, column=0, padx=12, pady=12, sticky="ew")
        controls.grid_columnconfigure(1, weight=1)
        self.announcement_network_toggle = tk.Checkbutton(
            controls,
            text=self._t("announcement.network"),
            variable=self.announcement_network_enabled,
            command=self._toggle_announcement_network,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            activebackground=COLORS["panel_2"],
            activeforeground=COLORS["cyan"],
            selectcolor=COLORS["black"],
            anchor="w",
            justify="left",
            wraplength=520,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.announcement_network_toggle.grid(
            row=0, column=0, padx=10, pady=(8, 2), sticky="w"
        )
        self.announcement_popup_toggle = tk.Checkbutton(
            controls,
            text=self._t("announcement.popup"),
            variable=self.announcement_popup_enabled,
            command=self._toggle_announcement_popup,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            activebackground=COLORS["panel_2"],
            activeforeground=COLORS["cyan"],
            selectcolor=COLORS["black"],
            anchor="w",
            justify="left",
            wraplength=520,
            font=("Cascadia Mono", 9),
        )
        self.announcement_popup_toggle.grid(
            row=1, column=0, padx=10, pady=(2, 8), sticky="w"
        )
        self.announcement_status_label = tk.Label(
            controls,
            textvariable=self.announcement_status,
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            anchor="e",
            justify="right",
            wraplength=520,
            font=("Cascadia Mono", 8),
        )
        self.announcement_status_label.grid(
            row=0, column=1, rowspan=2, padx=8, pady=8, sticky="ew"
        )
        self.announcement_sync_button = tk.Button(
            controls,
            text=self._t("announcement.sync"),
            command=lambda: self.check_announcements(force=True),
            bg=COLORS["blue"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=9,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.announcement_sync_button.grid(
            row=0, column=2, rowspan=2, padx=10, pady=8, sticky="e"
        )
        if not self.announcement_network_enabled.get():
            self.announcement_sync_button.configure(state="disabled")
        self.announcement_tree = DetailTree(
            page,
            [
                ("severity", self._t("announcement.severity"), 105),
                ("title", self._t("announcement.title"), 520),
                ("published", self._t("announcement.published"), 170),
            ],
        )
        self.announcement_tree.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")

    def _toggle_announcement_popup(self) -> None:
        try:
            self._save_announcement_preferences()
        except (OSError, AnnouncementError) as exc:
            self.announcement_status.set(
                self._t("announcement.error").format(error=clip(exc, 100))
            )

    def _toggle_announcement_network(self) -> None:
        try:
            self._save_announcement_preferences()
        except (OSError, AnnouncementError) as exc:
            self.announcement_status.set(
                self._t("announcement.error").format(error=clip(exc, 100))
            )
            return
        if not self.announcement_network_enabled.get():
            if self._announcement_after_id is not None:
                with contextlib.suppress(tk.TclError):
                    self.root.after_cancel(self._announcement_after_id)
            self._announcement_after_id = None
            self.announcement_sync_button.configure(state="disabled")
            self.announcement_status.set(self._t("announcement.network_off"))
            return
        self.announcement_sync_button.configure(state="normal")
        self._schedule_announcement_check(250)

    def _refresh_announcement_button(self) -> None:
        unread = sum(
            1
            for key in self._announcements
            if key not in self._announcement_read_ids
        )
        if unread:
            text = self._t("toolbar.announcements_unread").format(count=unread)
            self.announcement_button.configure(text=text, bg=COLORS["amber"], fg=COLORS["black"])
        else:
            self.announcement_button.configure(
                text=self._t("toolbar.announcements"),
                bg=COLORS["line"],
                fg=COLORS["text"],
            )

    def _schedule_announcement_check(self, delay_ms: int) -> None:
        if self._closing:
            return
        if self._announcement_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._announcement_after_id)
        self._announcement_after_id = self.root.after(
            max(250, int(delay_ms)), self.check_announcements
        )

    def check_announcements(self, force: bool = False) -> None:
        self._announcement_after_id = None
        if self.announcement_in_progress or self._closing:
            return
        if not self.announcement_network_enabled.get():
            self.announcement_sync_button.configure(state="disabled")
            self.announcement_status.set(self._t("announcement.network_off"))
            return
        try:
            config = AnnouncementConfig.load()
        except AnnouncementError as exc:
            self.announcement_status.set(
                self._t("announcement.error").format(error=clip(exc, 100))
            )
            self._schedule_announcement_check(15 * 60 * 1000)
            return
        if config is None:
            self.announcement_status.set(self._t("announcement.disabled"))
            self._schedule_announcement_check(60 * 60 * 1000)
            return
        self.announcement_in_progress = True
        self.announcement_sync_button.configure(state="disabled")
        self.announcement_status.set(self._t("announcement.checking"))
        locale = self.locale.get()
        cursor = self._announcement_cursors.get(locale, "1970-01-01T00:00:00Z")

        def worker() -> None:
            try:
                rows = fetch_announcements(
                    config,
                    locale=locale,
                    after_utc=cursor,
                )
                self._post_to_ui(
                    self._announcement_finished, config, locale, rows, None
                )
            except Exception as exc:
                self._post_to_ui(
                    self._announcement_finished, config, locale, (), exc
                )

        threading.Thread(
            target=worker,
            name="peerbridge-announcement-sync",
            daemon=True,
        ).start()

    def _announcement_finished(
        self,
        config: AnnouncementConfig,
        locale: str,
        rows: tuple[Announcement, ...],
        error: Exception | None,
    ) -> None:
        self.announcement_in_progress = False
        if not self.announcement_network_enabled.get():
            self.announcement_sync_button.configure(state="disabled")
            self.announcement_status.set(self._t("announcement.network_off"))
            return
        self.announcement_sync_button.configure(state="normal")
        if error is not None:
            self.announcement_status.set(
                self._t("announcement.error").format(error=clip(error, 100))
            )
            self._schedule_announcement_check(min(config.poll_seconds, 900) * 1000)
            return
        newly_visible: list[Announcement] = []
        for item in rows:
            key = self._announcement_key(item)
            if key not in self._announcements and key not in self._announcement_read_ids:
                newly_visible.append(item)
            self._announcements[key] = item
        self.announcement_status.set(
            self._t("announcement.updated").format(count=len(newly_visible))
            if newly_visible
            else self._t("announcement.none")
        )
        self._refresh_announcement_button()
        self._render_announcements(self.search.get().strip().lower())
        if self.active_page == "announcement":
            self._mark_current_announcements_read()
        elif newly_visible and self.announcement_popup_enabled.get():
            self._show_announcement_popup(newly_visible)
        if locale != self.locale.get():
            self._schedule_announcement_check(250)
        else:
            self._schedule_announcement_check(config.poll_seconds * 1000)

    def _show_announcement_popup(self, rows: list[Announcement]) -> None:
        if self._tutorial_is_open():
            self._pending_announcement_popup_rows = tuple(rows)
            return
        self._pending_announcement_popup_rows = ()
        self._close_announcement_popup()
        window = tk.Toplevel(self.root)
        self._announcement_window = window
        window.title(self._t("announcement.popup_title"))
        width = max(700, min(1000, int(self.root.winfo_width() * 0.78)))
        height = max(460, min(720, int(self.root.winfo_height() * 0.78)))
        self._place_transient(window, width, height)
        window.minsize(620, 420)
        window.configure(bg=COLORS["bg"])
        window.transient(self.root)
        content = tk.Text(
            window,
            wrap="word",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            relief="sunken",
            bd=2,
            padx=18,
            pady=18,
            font=("Cascadia Mono", 10),
        )
        content.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        for item in rows[:10]:
            content.insert("end", f"[{item.severity.upper()}] {item.title}\n")
            content.insert("end", f"{item.body}\n")
            if item.link_url:
                content.insert("end", f"{item.link_url}\n")
            content.insert("end", f"{item.published_utc}\n\n")
        content.configure(state="disabled")
        tk.Button(
            window,
            text=self._t("announcement.open"),
            command=lambda: (self._close_announcement_popup(), self.open_announcements()),
            bg=COLORS["cyan"],
            fg=COLORS["black"],
            activebackground=COLORS["green"],
            relief="raised",
            bd=2,
            padx=14,
            pady=7,
            font=("Cascadia Mono", 10, "bold"),
        ).pack(pady=(0, 16))
        window.protocol("WM_DELETE_WINDOW", self._close_announcement_popup)

    def _close_announcement_popup(self) -> None:
        window = self._announcement_window
        self._announcement_window = None
        if window is not None:
            with contextlib.suppress(tk.TclError):
                window.destroy()

    def open_announcements(self) -> None:
        self.show_page("announcement")
        self.check_announcements(force=True)

    def _mark_current_announcements_read(self) -> None:
        locale = self.locale.get()
        visible = [
            item for item in self._announcements.values() if item.locale == locale
        ]
        if not visible:
            return
        for item in visible:
            self._announcement_read_ids.add(self._announcement_key(item))
        self._announcement_cursors[locale] = max(
            item.published_utc for item in visible
        )
        try:
            self._save_announcement_preferences()
        except (OSError, AnnouncementError) as exc:
            self.announcement_status.set(
                self._t("announcement.error").format(error=clip(exc, 100))
            )
        self._refresh_announcement_button()

    def _render_announcements(self, query: str) -> None:
        if not hasattr(self, "announcement_tree"):
            return
        locale = self.locale.get()
        rows = []
        for item in sorted(
            (value for value in self._announcements.values() if value.locale == locale),
            key=lambda value: (value.published_utc, value.announcement_id),
            reverse=True,
        ):
            record = {
                "announcement_id": item.announcement_id,
                "locale": item.locale,
                "severity": item.severity,
                "title": item.title,
                "body": item.body,
                "link_url": item.link_url,
                "published_utc": item.published_utc,
                "expires_utc": item.expires_utc,
            }
            if query and query not in json.dumps(record, ensure_ascii=False).lower():
                continue
            rows.append(
                (
                    f"{item.locale}:{item.announcement_id}",
                    (item.severity.upper(), item.title, utc_text(item.published_utc)),
                    record,
                )
            )
        self.announcement_tree.replace(rows)

    def _build_usage_page(self, host: tk.Frame) -> None:
        page = tk.Frame(host, bg=COLORS["panel"], bd=2, relief="ridge")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(3, weight=2)
        page.grid_rowconfigure(4, weight=3)
        self.pages["usage"] = page

        period_strip = tk.Frame(page, bg=COLORS["panel_2"], bd=1, relief="ridge")
        period_strip.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        self.usage_period_label = tk.Label(
            period_strip,
            text=self._t("usage.period"),
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            font=("Cascadia Mono", 8, "bold"),
        )
        self.usage_period_label.pack(side="left", padx=(10, 8), pady=7)
        self.usage_period = tk.StringVar(value="30d")
        self.usage_period_buttons: dict[str, tk.Button] = {}
        for period in USAGE_PERIOD_KEYS:
            button = tk.Button(
                period_strip,
                text=self._t(f"usage.period.{period}"),
                command=lambda value=period: self._set_usage_period(value),
                bg=COLORS["line"],
                fg=COLORS["text"],
                activebackground=COLORS["cyan"],
                activeforeground=COLORS["black"],
                relief="raised",
                bd=2,
                padx=10,
                pady=3,
                font=("Cascadia Mono", 8, "bold"),
            )
            button.pack(side="left", padx=2, pady=5)
            self.usage_period_buttons[period] = button
        self.usage_timezone_label = tk.Label(
            period_strip,
            text=self._t("usage.timezone"),
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            font=("Cascadia Mono", 7),
        )
        self.usage_timezone_label.pack(side="right", padx=10, pady=7)
        self._sync_usage_period_buttons()

        kpi_strip = tk.Frame(page, bg=COLORS["panel"])
        kpi_strip.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        self.usage_kpi_labels: dict[str, tk.Label] = {}
        self.usage_kpi_values: dict[str, tk.StringVar] = {}
        for column, key in enumerate(
            ("total_tokens", "input_tokens", "output_tokens", "coverage", "dispatches")
        ):
            kpi_strip.grid_columnconfigure(column, weight=1, uniform="usage-kpi")
            cell = tk.Frame(kpi_strip, bg=COLORS["black"], bd=1, relief="ridge")
            cell.grid(row=0, column=column, sticky="nsew", padx=3)
            label = tk.Label(
                cell,
                text=self._t(f"usage.{key}"),
                bg=COLORS["black"],
                fg=COLORS["muted"],
                anchor="w",
                font=("Cascadia Mono", 8, "bold"),
            )
            label.pack(fill="x", padx=10, pady=(7, 1))
            value = tk.StringVar(value="0")
            tk.Label(
                cell,
                textvariable=value,
                bg=COLORS["black"],
                fg=COLORS["cyan"] if key != "dispatches" else COLORS["amber"],
                anchor="w",
                font=("Cascadia Mono", 13, "bold"),
            ).pack(fill="x", padx=10, pady=(1, 8))
            self.usage_kpi_labels[key] = label
            self.usage_kpi_values[key] = value

        self.usage_provider_frame = tk.LabelFrame(
            page,
            text=self._t("usage.platforms"),
            bg=COLORS["panel_2"],
            fg=COLORS["amber"],
            bd=1,
            relief="ridge",
            font=("Cascadia Mono", 9, "bold"),
        )
        self.usage_provider_frame.grid(
            row=2, column=0, sticky="ew", padx=10, pady=5
        )
        self.usage_provider_canvas = tk.Canvas(
            self.usage_provider_frame,
            height=160,
            bg=COLORS["black"],
            highlightthickness=0,
        )
        self.usage_provider_canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self._usage_provider_rows: tuple[dict[str, Any], ...] = ()
        self.usage_provider_canvas.bind(
            "<Configure>", lambda _event: self._draw_usage_charts()
        )

        charts = tk.Frame(page, bg=COLORS["panel"])
        charts.grid(row=3, column=0, sticky="nsew", padx=10, pady=5)
        charts.grid_columnconfigure(0, weight=3, uniform="usage-chart")
        charts.grid_columnconfigure(1, weight=2, uniform="usage-chart")
        charts.grid_rowconfigure(0, weight=1)
        self.usage_daily_frame = tk.LabelFrame(
            charts,
            text=self._t("usage.daily"),
            bg=COLORS["panel_2"],
            fg=COLORS["amber"],
            bd=1,
            relief="ridge",
            font=("Cascadia Mono", 9, "bold"),
        )
        self.usage_daily_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.usage_model_frame = tk.LabelFrame(
            charts,
            text=self._t("usage.models"),
            bg=COLORS["panel_2"],
            fg=COLORS["amber"],
            bd=1,
            relief="ridge",
            font=("Cascadia Mono", 9, "bold"),
        )
        self.usage_model_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.usage_daily_canvas = tk.Canvas(
            self.usage_daily_frame,
            height=175,
            bg=COLORS["black"],
            highlightthickness=0,
        )
        self.usage_daily_canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self.usage_model_canvas = tk.Canvas(
            self.usage_model_frame,
            height=175,
            bg=COLORS["black"],
            highlightthickness=0,
        )
        self.usage_model_canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self._usage_daily_rows: tuple[dict[str, Any], ...] = ()
        self._usage_trend_limit = 30
        self._usage_model_rows: tuple[dict[str, Any], ...] = ()
        self.usage_daily_canvas.bind(
            "<Configure>", lambda _event: self._draw_usage_charts()
        )
        self.usage_model_canvas.bind(
            "<Configure>", lambda _event: self._draw_usage_charts()
        )

        table_wrap = tk.Frame(page, bg=COLORS["panel"], bd=1, relief="ridge")
        table_wrap.grid(row=4, column=0, sticky="nsew", padx=10, pady=5)
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)
        self.usage_tree = ttk.Treeview(
            table_wrap,
            columns=USAGE_TABLE_COLUMNS,
            show="headings",
            selectmode="browse",
            height=7,
        )
        widths = (155, 220, 76, 90, 105, 105, 115)
        for key, width in zip(USAGE_TABLE_COLUMNS, widths):
            self.usage_tree.heading(key, text=self._t(f"usage.{key}"))
            self.usage_tree.column(
                key,
                width=width,
                minwidth=64,
                stretch=key in {"provider", "model"},
                anchor="e" if key in {"calls", "reported", "input", "output", "total"} else "w",
            )
        usage_scroll = ttk.Scrollbar(
            table_wrap, orient="vertical", command=self.usage_tree.yview
        )
        self.usage_tree.configure(yscrollcommand=usage_scroll.set)
        self.usage_tree.grid(row=0, column=0, sticky="nsew")
        usage_scroll.grid(row=0, column=1, sticky="ns")

        self.usage_note_label = tk.Label(
            page,
            text=self._t("usage.note"),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            font=("Cascadia Mono", 8),
        )
        self.usage_note_label.grid(
            row=5, column=0, sticky="ew", padx=14, pady=(1, 9)
        )

    def _set_usage_period(self, period: str) -> None:
        if period not in USAGE_PERIOD_KEYS:
            return
        self.usage_period.set(period)
        self._sync_usage_period_buttons()
        if self.snapshot is not None:
            self._render_usage(self.search.get().strip().lower())

    def _sync_usage_period_buttons(self) -> None:
        selected = self.usage_period.get()
        for period, button in self.usage_period_buttons.items():
            active = period == selected
            button.configure(
                bg=COLORS["cyan"] if active else COLORS["line"],
                fg=COLORS["black"] if active else COLORS["text"],
                relief="sunken" if active else "raised",
            )

    def _sync_usage_section_titles(self) -> None:
        period = self._t(f"usage.period.{self.usage_period.get()}")
        self.usage_provider_frame.configure(
            text=self._t("usage.section_title").format(
                section=self._t("usage.platforms"), period=period
            )
        )
        self.usage_daily_frame.configure(
            text=self._t("usage.section_title").format(
                section=self._t("usage.trend"), period=period
            )
        )
        self.usage_model_frame.configure(
            text=self._t("usage.section_title").format(
                section=self._t("usage.models"), period=period
            )
        )

    def _usage_number(self, value: Any) -> str:
        if value is None:
            return self._t("usage.unavailable")
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return self._t("usage.unavailable")

    def _usage_number_with_coverage(
        self,
        row: Mapping[str, Any],
        field: str,
    ) -> str:
        rendered = self._usage_number(row.get(field))
        calls = max(0, int(row.get("provider_calls") or 0))
        covered = max(0, int(row.get(f"{field}_reported_calls") or 0))
        if calls and covered < calls:
            return f"{rendered} · {covered:,}/{calls:,}"
        return rendered

    @staticmethod
    def _usage_short_number(value: Any) -> str:
        try:
            number = max(0, int(value or 0))
        except (TypeError, ValueError):
            return "0"
        for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if number >= divisor:
                scaled = number / divisor
                precision = 0 if scaled >= 100 else 1
                return f"{scaled:.{precision}f}{suffix}"
        return f"{number:,}"

    def _render_usage_provider_chart(
        self, rows: tuple[dict[str, Any], ...]
    ) -> None:
        self._usage_provider_rows = rows

    def _draw_usage_bar_chart(
        self,
        canvas: tk.Canvas,
        rows: tuple[dict[str, Any], ...],
        *,
        label_key: str,
        limit: int,
        orientation: str = "vertical",
    ) -> None:
        canvas.delete("all")
        width = max(260, int(canvas.winfo_width()))
        height = max(130, int(canvas.winfo_height()))
        visible = list(rows[-limit:] if label_key == "utc_date" else rows[:limit])
        token_values = [max(0, int(row.get("total_tokens") or 0)) for row in visible]
        use_calls = not any(token_values)
        values = (
            [max(0, int(row.get("provider_calls") or 0)) for row in visible]
            if use_calls
            else token_values
        )
        if not values or max(values, default=0) <= 0:
            canvas.create_text(
                width / 2,
                height / 2,
                text=self._t("usage.no_data"),
                fill=COLORS["muted"],
                font=("Cascadia Mono", 8, "bold"),
            )
            return
        canvas.create_text(
            10,
            8,
            anchor="nw",
            text=(
                self._t("usage.calls_no_tokens")
                if use_calls
                else self._t("usage.provider_reported_tokens")
            ),
            fill=COLORS["muted"],
            font=("Cascadia Mono", 7, "bold"),
        )
        maximum = max(values)
        color = COLORS["cyan"] if not use_calls else COLORS["amber"]
        if orientation == "horizontal":
            top, bottom = 30, height - 12
            gap = 3
            natural_height = (
                max(1, bottom - top) - gap * max(0, len(values) - 1)
            ) / len(values)
            bar_height = min(24, max(6, natural_height))
            group_height = bar_height * len(values) + gap * max(0, len(values) - 1)
            group_top = max(top, top + (max(1, bottom - top) - group_height) / 2)
            label_right = min(130, max(82, width // 4))
            track_left = label_right + 8
            value_width = 62
            track_right = max(track_left + 30, width - value_width)
            for index, (row, value) in enumerate(zip(visible, values)):
                y0 = group_top + index * (bar_height + gap)
                y1 = y0 + bar_height
                label = clip(str(row.get(label_key) or "--"), 16)
                canvas.create_text(
                    label_right,
                    (y0 + y1) / 2,
                    anchor="e",
                    text=label,
                    fill=COLORS["muted"],
                    font=("Cascadia Mono", 7),
                )
                canvas.create_rectangle(
                    track_left,
                    y0,
                    track_right,
                    y1,
                    fill=COLORS["line"],
                    outline="",
                )
                x1 = track_left + (track_right - track_left) * (value / maximum)
                canvas.create_rectangle(
                    track_left,
                    y0,
                    x1,
                    y1,
                    fill=color,
                    outline="",
                )
                canvas.create_text(
                    width - 8,
                    (y0 + y1) / 2,
                    anchor="e",
                    text=self._usage_number(value),
                    fill=COLORS["text"],
                    font=("Cascadia Mono", 7, "bold"),
                )
            return

        left, top, bottom = 12, 28, height - 30
        chart_height = max(30, bottom - top)
        gap = 5
        available_width = max(1, width - left * 2)
        natural_width = (
            available_width - gap * max(0, len(values) - 1)
        ) / len(values)
        bar_width = min(54, max(8, natural_width))
        group_width = bar_width * len(values) + gap * max(0, len(values) - 1)
        group_left = max(left, (width - group_width) / 2)
        canvas.create_line(
            left,
            bottom,
            width - left,
            bottom,
            fill=COLORS["line"],
        )
        for index, (row, value) in enumerate(zip(visible, values)):
            x0 = group_left + index * (bar_width + gap)
            x1 = min(width - left, x0 + bar_width)
            y0 = bottom - chart_height * (value / maximum)
            canvas.create_rectangle(x0, y0, x1, bottom, fill=color, outline="")
            canvas.create_text(
                (x0 + x1) / 2,
                max(top, y0 - 4),
                anchor="s",
                text=self._usage_number(value),
                fill=COLORS["text"],
                font=("Cascadia Mono", 7, "bold"),
            )
            label = str(row.get(label_key) or "--")
            if label_key == "utc_date":
                label = label[5:]
            else:
                label = clip(label, 10)
            canvas.create_text(
                (x0 + x1) / 2,
                bottom + 7,
                anchor="n",
                text=label,
                fill=COLORS["muted"],
                font=("Cascadia Mono", 6),
            )

    def _draw_usage_trend_chart(
        self,
        canvas: tk.Canvas,
        rows: tuple[dict[str, Any], ...],
        *,
        limit: int = 30,
    ) -> None:
        canvas.delete("all")
        width = max(320, int(canvas.winfo_width()))
        height = max(150, int(canvas.winfo_height()))
        visible = list(rows[-limit:])
        series = (
            ("input_tokens", self._t("usage.input"), COLORS["cyan"], ()),
            ("output_tokens", self._t("usage.output"), COLORS["amber"], ()),
            (
                "cached_input_tokens",
                self._t("usage.cached_input"),
                COLORS["purple"],
                (4, 3),
            ),
            (
                "reasoning_tokens",
                self._t("usage.reasoning"),
                COLORS["green"],
                (2, 3),
            ),
        )
        numeric_values = [
            max(0, int(row[field]))
            for row in visible
            for field, _label, _color, _dash in series
            if row.get(field) is not None
        ]
        maximum = max(numeric_values, default=0)
        if not visible or maximum <= 0:
            canvas.create_text(
                width / 2,
                height / 2,
                text=self._t("usage.no_data"),
                fill=COLORS["muted"],
                font=("Cascadia Mono", 8, "bold"),
            )
            return

        left, right, top, bottom = 54, width - 12, 30, height - 28
        chart_width = max(1, right - left)
        chart_height = max(1, bottom - top)
        for tick in range(5):
            ratio = tick / 4
            y = bottom - chart_height * ratio
            canvas.create_line(left, y, right, y, fill=COLORS["line"])
            canvas.create_text(
                left - 7,
                y,
                anchor="e",
                text=self._usage_short_number(round(maximum * ratio)),
                fill=COLORS["muted"],
                font=("Cascadia Mono", 6),
            )

        legend_x = left
        for _field, label, color, dash in series:
            canvas.create_line(
                legend_x,
                14,
                legend_x + 20,
                14,
                fill=color,
                width=2,
                dash=dash or None,
            )
            canvas.create_text(
                legend_x + 25,
                14,
                anchor="w",
                text=label,
                fill=COLORS["muted"],
                font=("Cascadia Mono", 6, "bold"),
            )
            legend_x += 76

        divisor = max(1, len(visible) - 1)
        label_step = max(1, len(visible) // 6)
        for index, row in enumerate(visible):
            if index % label_step and index != len(visible) - 1:
                continue
            x = left + chart_width * (index / divisor)
            period_label = str(
                row.get("period_label") or row.get("utc_date") or "--"
            )
            if len(period_label) == 10 and period_label[4:5] == "-":
                period_label = period_label[5:]
            canvas.create_text(
                x,
                bottom + 7,
                anchor="n",
                text=period_label,
                fill=COLORS["muted"],
                font=("Cascadia Mono", 6),
            )

        for field, _label, color, dash in series:
            segments: list[list[float]] = []
            points: list[float] = []
            for index, row in enumerate(visible):
                raw_value = row.get(field)
                if raw_value is None:
                    if points:
                        segments.append(points)
                        points = []
                    continue
                value = max(0, int(raw_value))
                x = left + chart_width * (index / divisor)
                y = bottom - chart_height * (value / maximum)
                points.extend((x, y))
            if points:
                segments.append(points)
            for segment in segments:
                if len(segment) == 2:
                    x, y = segment
                    canvas.create_oval(
                        x - 3, y - 3, x + 3, y + 3, fill=color, outline=""
                    )
                    continue
                canvas.create_line(
                    *segment,
                    fill=color,
                    width=2,
                    smooth=True,
                    splinesteps=12,
                    dash=dash or None,
                )

    def _draw_usage_charts(self) -> None:
        if not hasattr(self, "usage_daily_canvas"):
            return
        self._draw_usage_bar_chart(
            self.usage_provider_canvas,
            self._usage_provider_rows,
            label_key="provider_id",
            limit=12,
            orientation="horizontal",
        )
        self._draw_usage_trend_chart(
            self.usage_daily_canvas,
            self._usage_daily_rows,
            limit=self._usage_trend_limit,
        )
        self._draw_usage_bar_chart(
            self.usage_model_canvas,
            self._usage_model_rows,
            label_key="model_id",
            limit=8,
            orientation="horizontal",
        )

    def _build_feedback_page(self, host: tk.Frame) -> None:
        page = tk.Frame(host, bg=COLORS["panel"], bd=2, relief="ridge")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(3, weight=1)
        self.pages["feedback"] = page

        self.feedback_prompt_label = tk.Label(
            page,
            text=self._t("feedback.prompt"),
            bg=COLORS["panel"],
            fg=COLORS["amber"],
            anchor="nw",
            justify="left",
            wraplength=190,
            font=("Cascadia Mono", 11, "bold"),
        )
        self.feedback_prompt_label.grid(
            row=0, column=0, padx=(18, 10), pady=(18, 8), sticky="nw"
        )
        self.feedback_message = tk.Text(
            page,
            height=8,
            wrap="word",
            bg=COLORS["black"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 10),
        )
        self.feedback_message.grid(
            row=0, column=1, columnspan=3, padx=(0, 18), pady=(18, 8), sticky="nsew"
        )

        self.feedback_contact_label = tk.Label(
            page,
            text=self._t("feedback.contact"),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=190,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.feedback_contact_label.grid(
            row=1, column=0, padx=(18, 10), pady=8, sticky="w"
        )
        tk.Entry(
            page,
            textvariable=self.feedback_contact,
            bg=COLORS["black"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 10),
        ).grid(row=1, column=1, columnspan=3, padx=(0, 18), pady=8, sticky="ew", ipady=5)

        self.feedback_options = tk.Frame(
            page, bg=COLORS["panel_2"], bd=2, relief="ridge"
        )
        self.feedback_options.grid(
            row=2, column=0, columnspan=4, padx=18, pady=10, sticky="nsew"
        )
        self.feedback_options.grid_columnconfigure(1, weight=1)
        self.feedback_attach_button = tk.Button(
            self.feedback_options,
            text=self._t("feedback.attach"),
            command=self._choose_feedback_attachments,
            bg=COLORS["blue"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=10,
            pady=5,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.feedback_attach_button.grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self.feedback_attachment_label = tk.Label(
            self.feedback_options,
            textvariable=self.feedback_attachment_status,
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=420,
            font=("Cascadia Mono", 8),
        )
        self.feedback_attachment_label.grid(
            row=0, column=1, padx=8, pady=10, sticky="ew"
        )
        self.feedback_clear_button = tk.Button(
            self.feedback_options,
            text=self._t("feedback.clear"),
            command=self._clear_feedback_attachments,
            bg=COLORS["line"],
            fg=COLORS["text"],
            activebackground=COLORS["red"],
            relief="raised",
            bd=2,
            padx=8,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.feedback_clear_button.grid(row=0, column=2, padx=10, pady=10)

        self.feedback_key_toggle = tk.Checkbutton(
            self.feedback_options,
            text=self._t("feedback.include_key"),
            variable=self.feedback_include_key,
            command=self._sync_feedback_key_state,
            bg=COLORS["panel_2"],
            fg=COLORS["amber"],
            activebackground=COLORS["panel_2"],
            activeforeground=COLORS["cyan"],
            selectcolor=COLORS["black"],
            anchor="w",
            justify="left",
            wraplength=760,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.feedback_key_toggle.grid(
            row=1, column=0, columnspan=3, padx=10, pady=(4, 2), sticky="w"
        )
        self.feedback_key_entry = tk.Entry(
            self.feedback_options,
            textvariable=self.feedback_key,
            show="*",
            state="disabled",
            bg=COLORS["black"],
            fg=COLORS["text"],
            disabledbackground=COLORS["panel"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 10),
        )
        self.feedback_key_entry.grid(
            row=2, column=0, columnspan=3, padx=10, pady=(2, 8), sticky="ew", ipady=5
        )
        self.feedback_privacy_label = tk.Label(
            self.feedback_options,
            text=self._feedback_privacy_text(),
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            justify="left",
            anchor="nw",
            wraplength=760,
            font=("Cascadia Mono", 8),
        )
        self.feedback_privacy_label.grid(
            row=3, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="ew"
        )

        self.feedback_send_button = tk.Button(
            page,
            text=self._t("feedback.send"),
            command=self.submit_feedback,
            bg=COLORS["green"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=14,
            pady=7,
            font=("Cascadia Mono", 10, "bold"),
        )
        self.feedback_send_button.grid(row=3, column=0, padx=18, pady=12, sticky="w")
        self.feedback_status_label = tk.Label(
            page,
            textvariable=self.feedback_status,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify="left",
            anchor="w",
            wraplength=680,
            font=("Cascadia Mono", 9),
        )
        self.feedback_status_label.grid(
            row=3, column=1, columnspan=3, padx=(0, 18), pady=12, sticky="ew"
        )
        page.bind("<Configure>", self._schedule_feedback_reflow, add="+")
        self.feedback_options.bind(
            "<Configure>", self._schedule_feedback_reflow, add="+"
        )
        self._schedule_feedback_reflow()

    def _schedule_feedback_reflow(self, _event: Any = None) -> None:
        if self._closing or not hasattr(self, "feedback_privacy_label"):
            return
        if self._feedback_reflow_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._feedback_reflow_after_id)
        self._feedback_reflow_after_id = self.root.after_idle(
            self._update_feedback_wraplengths
        )

    def _update_feedback_wraplengths(self) -> None:
        self._feedback_reflow_after_id = None
        if self._closing or not hasattr(self, "feedback_options"):
            return
        page = self.pages.get("feedback")
        if page is None:
            return
        page_width = max(560, page.winfo_width())
        options_width = max(420, self.feedback_options.winfo_width())
        label_width = max(150, min(240, page_width // 5))
        attachment_width = max(
            180,
            options_width
            - self.feedback_attach_button.winfo_reqwidth()
            - self.feedback_clear_button.winfo_reqwidth()
            - 72,
        )
        options_text_width = max(260, options_width - 32)
        status_width = max(
            220,
            page_width - self.feedback_send_button.winfo_reqwidth() - 76,
        )
        self.feedback_prompt_label.configure(wraplength=label_width)
        self.feedback_contact_label.configure(wraplength=label_width)
        self.feedback_attachment_label.configure(wraplength=attachment_width)
        self.feedback_key_toggle.configure(wraplength=options_text_width)
        self.feedback_privacy_label.configure(wraplength=options_text_width)
        self.feedback_status_label.configure(wraplength=status_width)

    def _feedback_privacy_text(self) -> str:
        base = self._t("feedback.privacy")
        try:
            config = FeedbackConfig.load()
        except Exception as exc:
            return f"{base}\nCONFIG ERROR: {clip(exc, 160)}"
        destination = config.endpoint or (
            f"mailto:{config.support_email}" if config.support_email else "local encrypted bundle"
        )
        fingerprint = config.public_key_sha256 or "not configured"
        privacy_url = config.privacy_url or "not configured"
        recipient = config.recipient_label or "not configured"
        return (
            f"{base}\n"
            f"{self._t('feedback.recipient')}: {recipient} // "
            f"{self._t('feedback.destination')}: {destination}\n"
            f"{self._t('feedback.key_fingerprint')}: {fingerprint}\n"
            f"{self._t('feedback.privacy_url')}: {privacy_url}"
        )

    def _sync_feedback_key_state(self) -> None:
        enabled = self.feedback_include_key.get()
        if enabled:
            try:
                config = FeedbackConfig.load()
            except Exception as exc:
                self.feedback_include_key.set(False)
                self.feedback_key_entry.configure(state="disabled")
                self.feedback_status.set(
                    self._t("feedback.encrypt_error").format(error=clip(exc, 120))
                )
                return
            if not config.encrypted_secret_available:
                self.feedback_include_key.set(False)
                self.feedback_key_entry.configure(state="disabled")
                self.feedback_status.set(self._t("feedback.encrypt_unavailable"))
                return
            self.feedback_status.set(
                self._t("feedback.recipient_ready").format(
                    recipient=config.recipient_label,
                    sha=config.public_key_sha256,
                )
            )
        self.feedback_key_entry.configure(state="normal" if enabled else "disabled")
        if not enabled:
            self.feedback_key.set("")

    def _choose_feedback_attachments(self) -> None:
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title=self._t("feedback.file_dialog_title"),
            filetypes=(
                (self._t("feedback.file_dialog_safe"), "*.png *.jpg *.jpeg *.webp *.gif *.txt *.log *.json"),
                (self._t("feedback.file_dialog_all"), "*.*"),
            ),
        )
        if not selected:
            return
        self._feedback_attachment_paths = tuple(Path(value) for value in selected[:5])
        self.feedback_attachment_status.set(
            clip(", ".join(path.name for path in self._feedback_attachment_paths), 180)
        )

    def _clear_feedback_attachments(self) -> None:
        self._feedback_attachment_paths = ()
        self.feedback_attachment_status.set(self._t("feedback.attachment.none"))

    def submit_feedback(self) -> None:
        if self.feedback_in_progress:
            return
        message = self.feedback_message.get("1.0", "end-1c").strip()
        if not message:
            self.feedback_status.set(self._t("feedback.describe_required"))
            return
        include_key = self.feedback_include_key.get()
        credential = self.feedback_key.get() if include_key else ""
        if include_key and not credential:
            self.feedback_status.set(self._t("feedback.key_required"))
            return
        summary = clip(message, 160)
        contact = self.feedback_contact.get().strip()
        attachments = self._feedback_attachment_paths
        self.feedback_in_progress = True
        self.feedback_send_button.configure(
            state="disabled", text=self._t("feedback.sending")
        )
        self.feedback_status.set(self._t("feedback.packaging"))

        def worker() -> None:
            bundle: FeedbackBundle | None = None
            try:
                config = FeedbackConfig.load()
                bundle = create_feedback_bundle(
                    self.project_root,
                    summary=summary,
                    message=message,
                    contact=contact,
                    locale=self.locale.get(),
                    parser_stage="desktop-feedback-form",
                    credential_input=credential,
                    include_encrypted_credential=include_key,
                    attachment_paths=attachments,
                    attachment_consent=bool(attachments),
                    config=config,
                )
                result = deliver_feedback_bundle(bundle, config)
                mailto = feedback_mailto(config, bundle) if not result["delivered"] else None
                self._post_to_ui(
                    self._feedback_finished, bundle, result, mailto, None
                )
            except Exception as exc:
                self._post_to_ui(
                    self._feedback_finished, bundle, None, None, exc
                )

        threading.Thread(target=worker, name="peerbridge-feedback", daemon=True).start()

    def _feedback_finished(
        self,
        bundle: FeedbackBundle | None,
        result: dict[str, Any] | None,
        mailto: str | None,
        error: Exception | None,
    ) -> None:
        self.feedback_in_progress = False
        self.feedback_send_button.configure(state="normal", text=self._t("feedback.send"))
        self.feedback_key.set("")
        self.feedback_include_key.set(False)
        self._sync_feedback_key_state()
        if bundle is not None:
            self._last_feedback_bundle = bundle
        if error is not None:
            suffix = (
                self._t("feedback.local_bundle_suffix").format(path=bundle.path)
                if bundle is not None
                else ""
            )
            self.feedback_status.set(
                self._t("feedback.send_failed").format(
                    error=clip(error, 140), suffix=suffix
                )
            )
            return
        assert bundle is not None and result is not None
        self.feedback_message.delete("1.0", "end")
        self._clear_feedback_attachments()
        if result.get("delivered"):
            if result.get("notification_sent") is True:
                status = self._t("feedback.received_notified")
            elif result.get("notification_sent") is False:
                status = self._t("feedback.received_notify_failed")
            else:
                status = self._t("feedback.received_notify_unknown")
            self.feedback_status.set(
                self._t("feedback.received_summary").format(
                    status=status,
                    case_id=bundle.case_id,
                    sha=bundle.sha256[:16],
                )
            )
        elif mailto:
            webbrowser.open(mailto, new=1)
            self.feedback_status.set(
                self._t("feedback.email_opened").format(
                    case_id=bundle.case_id, path=bundle.path
                )
            )
        else:
            self.feedback_status.set(
                self._t("feedback.saved_local").format(
                    case_id=bundle.case_id, path=bundle.path
                )
            )

    def _build_chat_page(self, host: tk.Frame) -> None:
        outer_page = tk.Frame(
            host,
            bg=MODERN_WORKSPACE_BG if ACTIVE_THEME == "modern" else COLORS["panel"],
            bd=0 if ACTIVE_THEME == "modern" else 2,
            relief="flat" if ACTIVE_THEME == "modern" else "ridge",
        )
        outer_page.grid(row=0, column=0, sticky="nsew")
        outer_page.grid_rowconfigure(0, weight=1)
        outer_page.grid_columnconfigure(0, weight=1)
        self.chat_page_canvas = tk.Canvas(
            outer_page,
            bg=MODERN_WORKSPACE_BG if ACTIVE_THEME == "modern" else COLORS["panel"],
            highlightthickness=0,
            bd=0,
        )
        self.chat_page_scrollbar = ttk.Scrollbar(
            outer_page,
            orient="vertical",
            command=self.chat_page_canvas.yview,
        )
        self.chat_page_canvas.configure(
            yscrollcommand=self.chat_page_scrollbar.set
        )
        self.chat_page_canvas.grid(row=0, column=0, sticky="nsew")
        if ACTIVE_THEME != "modern":
            self.chat_page_scrollbar.grid(row=0, column=1, sticky="ns")

        page = tk.Frame(
            self.chat_page_canvas,
            bg=MODERN_WORKSPACE_BG if ACTIVE_THEME == "modern" else COLORS["panel"],
        )
        self.chat_page_content = page
        self.chat_page_window = self.chat_page_canvas.create_window(
            (0, 0), window=page, anchor="nw"
        )
        page.bind("<Configure>", self._sync_chat_page_scrollregion)
        self.chat_page_canvas.bind("<Configure>", self._resize_chat_page_viewport)
        page.grid_columnconfigure(0, weight=1)
        self.modern_inspector_buttons: dict[str, tk.Button] = {}
        self.modern_inspector_frames: dict[str, tk.Frame] = {}
        self.modern_evidence_buttons: dict[str, tk.Button] = {}
        self.modern_chat_workspace: tk.PanedWindow | None = None
        self.modern_chat_inspector: tk.Frame | None = None
        if ACTIVE_THEME == "modern":
            page.grid_rowconfigure(0, weight=1)
            workspace = tk.PanedWindow(
                page,
                orient="horizontal",
                bg=COLORS["line"],
                bd=0,
                relief="flat",
                sashrelief="flat",
                sashwidth=7,
                opaqueresize=True,
            )
            workspace.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
            center = tk.Frame(workspace, bg=MODERN_WORKSPACE_BG)
            center.grid_rowconfigure(1, weight=1)
            center.grid_columnconfigure(0, weight=1)
            inspector = tk.Frame(
                workspace,
                bg=COLORS["panel"],
                width=MODERN_INSPECTOR_WIDTH,
                highlightthickness=1,
                highlightbackground=COLORS["line"],
            )
            inspector.grid_rowconfigure(1, weight=1)
            inspector.grid_columnconfigure(0, weight=1)
            workspace.add(center, minsize=520, stretch="always")
            workspace.add(
                inspector,
                minsize=268,
                width=MODERN_INSPECTOR_WIDTH,
                stretch="never",
            )
            self.modern_chat_workspace = workspace
            self.modern_chat_inspector = inspector

            inspector_tabs = tk.Frame(inspector, bg=COLORS["panel"])
            inspector_tabs.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
            for column in range(len(MODERN_INSPECTOR_KEYS)):
                inspector_tabs.grid_columnconfigure(column, weight=1)
            for column, inspector_key in enumerate(MODERN_INSPECTOR_KEYS):
                button = tk.Button(
                    inspector_tabs,
                    text=self._t(f"modern.inspector.{inspector_key}"),
                    command=lambda value=inspector_key: self._show_modern_inspector(value),
                    bg=COLORS["panel"],
                    fg=COLORS["muted"],
                    activebackground=COLORS["panel_2"],
                    activeforeground=COLORS["blue"],
                    relief="flat",
                    bd=0,
                    padx=6,
                    pady=8,
                    font=(MODERN_FONT_FAMILY, 9, "bold"),
                )
                button.grid(row=0, column=column, sticky="ew")
                self.modern_inspector_buttons[inspector_key] = button
            inspector_body = tk.Frame(inspector, bg=COLORS["panel"])
            inspector_body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
            inspector_body.grid_rowconfigure(0, weight=1)
            inspector_body.grid_columnconfigure(0, weight=1)
            room_parent = center
            seats_parent = inspector_body
            guided_parent = inspector_body
            split_parent = center
            room_row = 0
            split_row = 1
        else:
            page.grid_rowconfigure(3, weight=1)
            room_parent = page
            seats_parent = page
            guided_parent = page
            split_parent = page
            room_row = 0
            split_row = 3

        room_bar = tk.Frame(
            room_parent,
            bg=MODERN_WORKSPACE_BG if ACTIVE_THEME == "modern" else COLORS["black"],
            bd=0 if ACTIVE_THEME == "modern" else 2,
            relief="flat" if ACTIVE_THEME == "modern" else "ridge",
        )
        room_bar.grid(
            row=room_row,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=0 if ACTIVE_THEME == "modern" else 8,
            pady=0 if ACTIVE_THEME == "modern" else (8, 4),
        )
        room_bar.grid_columnconfigure(6, weight=1)
        if ACTIVE_THEME == "modern":
            self.modern_room_identity_frame = tk.Frame(
                room_bar, bg=MODERN_WORKSPACE_BG
            )
            self.modern_room_title_label = tk.Label(
                self.modern_room_identity_frame,
                text=self._t("modern.rooms.unnamed"),
                bg=MODERN_WORKSPACE_BG,
                fg=COLORS["text"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 13, "bold"),
            )
            self.modern_room_title_label.pack(side="left")
            self.modern_room_context_label = tk.Label(
                self.modern_room_identity_frame,
                text="",
                bg=MODERN_WORKSPACE_BG,
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8),
            )
            self.modern_room_context_label.pack(side="left", padx=(10, 0), pady=(3, 0))
        self.room_bar_label = self._localized_label(
            room_bar,
            "chat.room_label",
            bg=COLORS["black"],
            fg=COLORS["amber"],
            font=("Cascadia Mono", 8, "bold"),
        )
        self.room_bar_label.grid(row=0, column=0, padx=(8, 4), pady=7)
        self.room_combo = ttk.Combobox(
            room_bar,
            textvariable=self.room_choice,
            values=(),
            width=38,
            state="readonly",
        )
        self.room_combo.grid(row=0, column=1, padx=4, pady=7)
        self.room_combo.bind("<<ComboboxSelected>>", self._on_room_selected)
        self.new_room_button = tk.Button(
            room_bar,
            text=self._t("chat.new_room"),
            command=self._open_create_room_dialog,
            bg=COLORS["green"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=9,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.new_room_button.grid(row=0, column=2, padx=6, pady=6)
        self.operator_room_button = tk.Button(
            room_bar,
            text=self._t("chat.join_control"),
            command=self.join_selected_room_as_operator,
            bg=COLORS["purple"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=8,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.operator_room_button.grid(row=0, column=3, padx=4, pady=6)
        self.older_history_button = tk.Button(
            room_bar,
            text=self._t("chat.older"),
            command=self.load_older_room_history,
            bg=COLORS["line"],
            fg=COLORS["text"],
            activebackground=COLORS["cyan"],
            activeforeground=COLORS["black"],
            relief="raised",
            bd=2,
            padx=7,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.older_history_button.grid(row=0, column=4, padx=4, pady=6)
        self.latest_history_button = tk.Button(
            room_bar,
            text=self._t("chat.latest"),
            command=self.load_latest_room_history,
            bg=COLORS["line"],
            fg=COLORS["text"],
            activebackground=COLORS["cyan"],
            activeforeground=COLORS["black"],
            relief="raised",
            bd=2,
            padx=7,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.latest_history_button.grid(row=0, column=5, padx=4, pady=6)
        self.room_status_label = tk.Label(
            room_bar,
            textvariable=self.room_status,
            bg=COLORS["black"],
            fg=COLORS["muted"],
            anchor="e",
            font=("Cascadia Mono", 8, "bold"),
        )
        self.room_status_label.grid(row=0, column=6, sticky="ew", padx=(8, 10), pady=7)
        self.chat_focus_button = tk.Button(
            room_bar,
            text=self._t("chat.focus"),
            command=lambda: self._set_chat_focus(not self.chat_focus_mode),
            bg=COLORS["blue"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=7,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.chat_focus_button.grid(row=0, column=7, padx=(0, 8), pady=6)
        self.modern_room_settings_button = tk.Button(
            room_bar,
            text=self._t("modern.room.settings"),
            command=self._toggle_modern_room_settings,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            activebackground=COLORS["panel_2"],
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            padx=8,
            pady=5,
            font=(MODERN_FONT_FAMILY, 8, "bold"),
        )
        self.modern_inspector_toggle_button = tk.Button(
            room_bar,
            text=self._t("modern.room.hide_context"),
            command=self._toggle_modern_inspector,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            activebackground=COLORS["panel_2"],
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            padx=8,
            pady=5,
            font=(MODERN_FONT_FAMILY, 8),
        )

        self.auto_label = tk.Label(
            room_bar,
            text=self._t("chat.auto"),
            bg=COLORS["black"],
            fg=COLORS["amber"],
            font=("Cascadia Mono", 8, "bold"),
        )
        self.auto_label.grid(row=1, column=0, padx=(8, 4), pady=(1, 7))
        self.room_automation_combo = ttk.Combobox(
            room_bar,
            textvariable=self.room_automation_choice,
            values=self._automation_labels(),
            width=22,
            state="readonly",
        )
        self.room_automation_combo.grid(row=1, column=1, padx=4, pady=(1, 7), sticky="w")
        limit_frame = tk.Frame(room_bar, bg=COLORS["black"])
        limit_frame.grid(row=1, column=2, columnspan=2, padx=4, pady=(1, 7), sticky="w")
        self.limit_labels: dict[str, tk.Label] = {}
        for index, (key, variable, width, lower, upper) in enumerate(
            (
                ("rounds", self.room_round_limit, 3, 1, 20),
                ("messages", self.room_message_limit, 4, 2, 200),
                ("stagnation", self.room_stagnation_limit, 3, 1, 5),
            )
        ):
            limit_label = tk.Label(
                limit_frame,
                text=self._t(f"chat.{key}"),
                bg=COLORS["black"],
                fg=COLORS["muted"],
                font=("Cascadia Mono", 8),
            )
            limit_label.grid(row=0, column=index * 2, padx=(4, 2))
            self.limit_labels[key] = limit_label
            tk.Spinbox(
                limit_frame,
                textvariable=variable,
                from_=lower,
                to=upper,
                width=width,
                bg=COLORS["panel"],
                fg=COLORS["text"],
                buttonbackground=COLORS["line"],
                relief="sunken",
                font=("Cascadia Mono", 8),
            ).grid(row=0, column=index * 2 + 1, padx=(0, 3))
        self.apply_automation_button = tk.Button(
            room_bar,
            text=self._t("chat.apply"),
            command=self.apply_room_automation,
            bg=COLORS["cyan"],
            fg=COLORS["black"],
            activebackground=COLORS["green"],
            relief="raised",
            bd=2,
            padx=7,
            pady=3,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.apply_automation_button.grid(row=1, column=4, padx=4, pady=(1, 7))
        controls = tk.Frame(room_bar, bg=COLORS["black"])
        controls.grid(row=1, column=5, padx=4, pady=(1, 7))
        self.pause_discussion_button = tk.Button(
            controls,
            text=self._t("chat.pause"),
            command=lambda: self.control_active_discussion("pause"),
            bg=COLORS["purple"],
            fg=COLORS["black"],
            bd=2,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.pause_discussion_button.pack(side="left", padx=2)
        self.resume_discussion_button = tk.Button(
            controls,
            text=self._t("chat.resume"),
            command=lambda: self.control_active_discussion("resume"),
            bg=COLORS["cyan"],
            fg=COLORS["black"],
            bd=2,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.resume_discussion_button.pack(side="left", padx=2)
        self.continue_discussion_button = tk.Button(
            controls,
            text=self._t("chat.continue"),
            command=lambda: self.control_active_discussion("continue"),
            bg=COLORS["green"],
            fg=COLORS["black"],
            bd=2,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.continue_discussion_button.pack(side="left", padx=2)
        self.stop_discussion_button = tk.Button(
            controls,
            text=self._t("chat.stop"),
            command=lambda: self.control_active_discussion("stop"),
            bg=COLORS["red"],
            fg=COLORS["black"],
            bd=2,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.stop_discussion_button.pack(side="left", padx=2)
        self.discussion_status_label = tk.Label(
            room_bar,
            textvariable=self.discussion_status,
            bg=COLORS["black"],
            fg=COLORS["muted"],
            anchor="e",
            font=("Cascadia Mono", 8, "bold"),
        )
        self.discussion_status_label.grid(
            row=1, column=6, sticky="ew", padx=(8, 10), pady=(1, 7)
        )
        self.modern_room_bar = room_bar
        self.modern_limit_frame = limit_frame
        self.modern_discussion_controls = controls

        seats = tk.Frame(
            seats_parent,
            bg=COLORS["panel"] if ACTIVE_THEME == "modern" else COLORS["panel_2"],
            bd=2,
            relief="ridge",
        )
        seats.grid(
            row=0 if ACTIVE_THEME == "modern" else 1,
            column=0,
            columnspan=2,
            sticky="nsew" if ACTIVE_THEME == "modern" else "ew",
            padx=0 if ACTIVE_THEME == "modern" else 8,
            pady=0 if ACTIVE_THEME == "modern" else 4,
        )
        seats.grid_columnconfigure(4, weight=1)
        self.room_seats_frame = seats
        self.room_seats_label = tk.Label(
            seats,
            text=self._t("chat.room_seats"),
            bg=COLORS["panel_2"],
            fg=COLORS["cyan"],
            font=("Cascadia Mono", 8, "bold"),
        )
        self.room_seats_label.grid(row=0, column=0, padx=(8, 4), pady=5)
        if ACTIVE_THEME == "modern":
            self.modern_agents_summary_frame = tk.Frame(
                seats, bg=COLORS["panel"]
            )
            self.modern_manage_agents_button = tk.Button(
                seats,
                text=self._t("modern.agents.manage"),
                command=self._toggle_modern_agent_editor,
                bg=COLORS["panel"],
                fg=COLORS["blue"],
                activebackground=COLORS["line"],
                activeforeground=COLORS["blue"],
                relief="flat",
                bd=0,
                padx=4,
                pady=4,
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            )
        self.seat_agent_combo = ttk.Combobox(
            seats,
            textvariable=self.seat_agent,
            values=(),
            width=19,
            state="readonly",
        )
        self.seat_agent_combo.grid(row=0, column=1, padx=4, pady=5)
        self.seat_agent_combo.bind("<<ComboboxSelected>>", self._on_seat_agent_selected)
        self.seat_provider_combo = ttk.Combobox(
            seats,
            textvariable=self.seat_provider_choice,
            values=(),
            width=24,
            state="disabled",
        )
        self.seat_provider_combo.grid(row=0, column=2, padx=4, pady=5)
        self.seat_provider_combo.bind("<<ComboboxSelected>>", self._on_seat_provider_selected)
        self.seat_model_combo = ttk.Combobox(
            seats,
            textvariable=self.seat_model_choice,
            values=(),
            width=20,
            state="disabled",
        )
        self.seat_model_combo.grid(row=0, column=3, padx=4, pady=5)
        self.seat_model_combo.bind("<<ComboboxSelected>>", self._on_seat_model_selected)
        self.seat_reasoning_combo = ttk.Combobox(
            seats,
            textvariable=self.seat_reasoning_choice,
            values=(),
            width=17,
            state="disabled",
        )
        self.seat_reasoning_combo.grid(row=0, column=4, padx=4, pady=5, sticky="w")
        self.seat_reasoning_combo.bind(
            "<<ComboboxSelected>>", self._on_seat_reasoning_selected
        )
        self.add_seat_button = tk.Button(
            seats,
            text=self._t("chat.apply_seat"),
            command=self.add_room_seat,
            bg=COLORS["cyan"],
            fg=COLORS["black"],
            activebackground=COLORS["green"],
            relief="raised",
            bd=2,
            padx=8,
            pady=3,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.add_seat_button.grid(row=0, column=5, padx=4, pady=5)
        self.remove_seat_button = tk.Button(
            seats,
            text=self._t("chat.remove_seat"),
            command=self.remove_room_seat,
            bg=COLORS["red"],
            fg=COLORS["black"],
            activebackground=COLORS["amber"],
            relief="raised",
            bd=2,
            padx=8,
            pady=3,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.remove_seat_button.grid(row=0, column=6, padx=4, pady=5)

        role_bar = tk.Frame(seats, bg=COLORS["black"], bd=1, relief="ridge")
        role_bar.grid(
            row=1,
            column=0,
            columnspan=7,
            sticky="ew",
            padx=8,
            pady=(1, 4),
        )
        role_bar.grid_columnconfigure(3, weight=1)
        self.seat_role_label = tk.Label(
            role_bar,
            text=self._t("chat.role"),
            bg=COLORS["black"],
            fg=COLORS["amber"],
            font=("Cascadia Mono", 8, "bold"),
        )
        self.seat_role_label.grid(row=0, column=0, padx=(8, 4), pady=6)
        self.seat_role_combo = ttk.Combobox(
            role_bar,
            textvariable=self.seat_role_choice,
            values=(),
            state="readonly",
            width=18,
        )
        self.seat_role_combo.grid(row=0, column=1, padx=4, pady=6)
        self.seat_role_combo.bind(
            "<<ComboboxSelected>>", self._on_seat_role_selected
        )
        self.seat_custom_role_entry = tk.Entry(
            role_bar,
            textvariable=self.seat_custom_role,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
            state="disabled",
        )
        self.seat_custom_role_entry.grid(
            row=0, column=2, columnspan=2, sticky="ew", padx=4, pady=6, ipady=3
        )
        self.apply_role_button = tk.Button(
            role_bar,
            text=self._t("chat.apply_role"),
            command=self.apply_room_member_role,
            bg=COLORS["amber"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=8,
            pady=3,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.apply_role_button.grid(row=0, column=4, padx=4, pady=6)
        self.view_live_work_button = tk.Button(
            role_bar,
            text=self._t("chat.view_live_work"),
            command=self.view_selected_agent_work,
            bg=COLORS["green"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=8,
            pady=3,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.view_live_work_button.grid(row=0, column=5, padx=4, pady=6)
        self.seat_role_note = tk.Label(
            role_bar,
            text=self._t("chat.role_no_authority"),
            bg=COLORS["black"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=220,
            font=("Cascadia Mono", 7),
        )
        self.seat_role_note.grid(row=0, column=6, sticky="ew", padx=(4, 8), pady=6)
        self.seat_status_label = tk.Label(
            seats,
            textvariable=self.seat_status,
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            anchor="w",
            font=("Cascadia Mono", 7),
        )
        self.seat_status_label.grid(
            row=2, column=0, columnspan=7, sticky="ew", padx=10, pady=(0, 4)
        )

        self.room_seat_tree = ttk.Treeview(
            seats,
            columns=("agent", "role", "session", "route", "state"),
            show="headings",
            selectmode="browse",
            height=1,
        )
        for key, heading, width in (
            ("agent", "AGENT", 150),
            ("role", "ROLE", 150),
            ("session", "ROOM SESSION ID", 220),
            ("route", "ROUTE", 280),
            ("state", "STATE", 80),
        ):
            self.room_seat_tree.heading(key, text=heading)
            self.room_seat_tree.column(key, width=width, minwidth=70, stretch=True)
        seat_scroll = ttk.Scrollbar(seats, orient="vertical", command=self.room_seat_tree.yview)
        self.room_seat_tree.configure(yscrollcommand=seat_scroll.set)
        self.room_agent_strip = tk.Frame(seats, bg=COLORS["black"], height=92)
        self.room_agent_strip.grid(row=3, column=0, columnspan=7, sticky="ew", padx=8, pady=(1, 6))
        self.room_agent_strip.grid_propagate(False)
        self.room_agent_strip.bind(
            "<Configure>", self._room_agent_strip_resized, add="+"
        )
        self.room_seat_tree.grid(row=4, column=0, columnspan=6, sticky="ew", padx=(8, 0), pady=(0, 7))
        seat_scroll.grid(row=4, column=6, sticky="ns", padx=(0, 8), pady=(0, 7))
        self.room_seat_tree.bind("<<TreeviewSelect>>", self._select_room_seat_for_edit)
        self.modern_role_bar = role_bar
        self.modern_seat_scroll = seat_scroll

        guided = tk.Frame(guided_parent, bg=COLORS["black"], bd=2, relief="ridge")
        guided.grid(
            row=0 if ACTIVE_THEME == "modern" else 2,
            column=0,
            columnspan=2,
            sticky="nsew" if ACTIVE_THEME == "modern" else "ew",
            padx=0 if ACTIVE_THEME == "modern" else 8,
            pady=0 if ACTIVE_THEME == "modern" else 4,
        )
        guided.grid_columnconfigure(1, weight=1)
        self.guided_workflow_title = tk.Label(
            guided,
            text=self._t("chat.guided.title"),
            bg=COLORS["black"],
            fg=COLORS["cyan"],
            font=("Cascadia Mono", 8, "bold"),
        )
        self.guided_workflow_title.grid(row=0, column=0, padx=(9, 8), pady=6)
        self.guided_workflow_detail = tk.Label(
            guided,
            text=self._t("chat.guided.detail"),
            bg=COLORS["black"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=520,
            font=("Cascadia Mono", 8),
        )
        self.guided_workflow_detail.grid(row=0, column=1, sticky="ew", pady=6)
        self.guided_workflow_button = tk.Button(
            guided,
            text=self._t("chat.guided.start"),
            command=self.start_guided_room_workflow,
            bg=COLORS["green"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=9,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.guided_workflow_button.grid(row=0, column=2, padx=7, pady=5)
        self.guided_workflow_status_label = tk.Label(
            guided,
            textvariable=self.guided_workflow_status,
            bg=COLORS["black"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=1080,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.guided_workflow_status_label.grid(
            row=1, column=0, columnspan=4, sticky="ew", padx=9, pady=(0, 7)
        )
        self.modern_guided_panel = guided

        if ACTIVE_THEME == "modern":
            evidence = self._build_modern_evidence_panel(inspector_body)
            evidence.grid(row=0, column=0, sticky="nsew")
            self.modern_inspector_frames = {
                "agents": seats,
                "workflow": guided,
                "evidence": evidence,
            }
            self._reflow_modern_room_bar(room_bar, limit_frame, controls)
            self._reflow_modern_room_inspector(
                seats, role_bar, seat_scroll, guided
            )
            self._show_modern_inspector("agents")

        self.chat_split = tk.PanedWindow(
            split_parent,
            orient="vertical",
            bg=COLORS["line"],
            bd=0,
            relief="flat",
            sashrelief="raised",
            sashwidth=6,
            opaqueresize=True,
        )
        self.chat_split.grid(
            row=split_row,
            column=0,
            columnspan=2,
            sticky="nsew",
            padx=0 if ACTIVE_THEME == "modern" else 8,
            pady=0 if ACTIVE_THEME == "modern" else (4, 8),
        )
        history_bg = (
            MODERN_WORKSPACE_BG if ACTIVE_THEME == "modern" else COLORS["panel"]
        )
        history_host = tk.Frame(self.chat_split, bg=history_bg)
        history_host.grid_rowconfigure(0, weight=1)
        history_host.grid_columnconfigure(0, weight=1)
        canvas = tk.Canvas(
            history_host,
            bg=history_bg,
            height=CHAT_HISTORY_MIN_HEIGHT,
            highlightthickness=0,
        )
        scrollbar = ttk.Scrollbar(history_host, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        inner = tk.Frame(canvas, bg=history_bg)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        self.chat_canvas = canvas
        self.chat_inner = inner
        self.chat_history_window = window
        inner.bind("<Configure>", self._sync_chat_history_scrollregion)
        canvas.bind("<Configure>", self._resize_chat_history_viewport)
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"),
        )
        self.chat_split.add(
            history_host,
            minsize=CHAT_HISTORY_MIN_HEIGHT,
            stretch="always",
        )

        if ACTIVE_THEME == "modern":
            composer_pane = tk.Frame(self.chat_split, bg=MODERN_WORKSPACE_BG)
            composer_pane.grid_columnconfigure(0, weight=1)
            composer = tk.Frame(
                composer_pane,
                bg=COLORS["panel"],
                bd=0,
                relief="flat",
                highlightthickness=1,
                highlightbackground=COLORS["line"],
            )
            composer.grid(
                row=0,
                column=0,
                sticky="ew",
                padx=(54, 54),
                pady=(6, 8),
            )
            composer_pane.bind("<Configure>", self._reflow_modern_composer_shell)
        else:
            composer_pane = self.chat_split
            composer = tk.Frame(
                composer_pane,
                bg=COLORS["black"],
                bd=2,
                relief="ridge",
                highlightthickness=0,
                highlightbackground=COLORS["line"],
            )
        self.chat_composer_pane = (
            composer_pane if ACTIVE_THEME == "modern" else composer
        )
        composer.grid_columnconfigure(7, weight=1, minsize=104)
        self.composer_labels: dict[str, tk.Label] = {}
        for key, row, column, padx, pady in (
            ("to", 0, 0, (8, 4), 6),
            ("priority", 0, 2, (10, 4), 6),
            ("task", 0, 4, (10, 4), 6),
            ("provider", 1, 0, (8, 4), 4),
            ("model", 1, 2, (8, 4), 4),
            ("reasoning", 1, 4, (8, 4), 4),
            ("subject", 2, 0, (8, 4), 4),
        ):
            label = tk.Label(
                composer,
                text=self._t(f"chat.{key}"),
                bg=COLORS["black"],
                fg=COLORS["amber"],
                font=("Cascadia Mono", 8, "bold"),
            )
            label.grid(row=row, column=column, padx=padx, pady=pady)
            self.composer_labels[key] = label
        recipient = ttk.Combobox(
            composer,
            textvariable=self.message_recipient,
            values=(self._t(BROADCAST_LABEL),),
            width=23,
            state="readonly",
        )
        self.recipient_combo = recipient
        recipient.grid(row=0, column=1, padx=4, pady=6)
        recipient.bind("<<ComboboxSelected>>", self._on_recipient_selected)
        self.priority_combo = ttk.Combobox(
            composer,
            textvariable=self.message_priority_label,
            values=(),
            width=9,
            state="readonly",
        )
        self.priority_combo.grid(row=0, column=3, padx=4, pady=6)
        self.priority_combo.bind("<<ComboboxSelected>>", self._on_priority_selected)
        self._sync_priority_choices()
        self.message_task_entry = tk.Entry(
            composer,
            textvariable=self.message_task,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
        )
        self.message_task_entry.grid(
            row=0, column=5, columnspan=3, sticky="ew", padx=4, pady=6
        )

        self.profile_combo = ttk.Combobox(
            composer,
            textvariable=self.message_provider_choice,
            values=(self._t(DIRECT_LABEL),),
            width=23,
            state="disabled",
        )
        self.profile_combo.grid(row=1, column=1, padx=4, pady=4)
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_provider_selected)
        self.provider_combo = ttk.Combobox(
            composer,
            textvariable=self.message_provider,
            values=("",),
            width=14,
            state="readonly",
        )
        self.model_combo = ttk.Combobox(
            composer,
            textvariable=self.message_model,
            values=("",),
            width=14,
            state="disabled",
        )
        self.model_combo.grid(row=1, column=3, padx=4, pady=4)
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        self.reasoning_combo = ttk.Combobox(
            composer,
            textvariable=self.message_reasoning,
            values=("",),
            width=10,
            state="disabled",
        )
        self.reasoning_combo.grid(row=1, column=5, columnspan=2, sticky="ew", padx=4, pady=4)
        self.reasoning_combo.bind("<<ComboboxSelected>>", self._on_reasoning_selected)
        self.manage_sources_button = tk.Button(
            composer,
            text=self._t("chat.manage_providers"),
            command=lambda: self.show_page("connect"),
            bg=COLORS["blue"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=8,
            pady=3,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.manage_sources_button.grid(row=1, column=7, sticky="ew", padx=(4, 8), pady=4)
        self.modern_composer_advanced_button = tk.Button(
            composer,
            text=self._t("modern.composer.advanced"),
            command=self._toggle_modern_composer_advanced,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            activebackground=COLORS["panel_2"],
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            padx=8,
            pady=4,
            font=(MODERN_FONT_FAMILY, 8),
        )
        self.modern_composer_prompt_label = tk.Label(
            composer,
            text=self._t("modern.composer.prompt"),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            font=(MODERN_FONT_FAMILY, 9),
        )

        self.message_subject_entry = tk.Entry(
            composer,
            textvariable=self.message_subject,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
        )
        self.message_subject_entry.grid(
            row=2, column=1, columnspan=7, sticky="ew", padx=4, pady=4
        )
        self.message_body = tk.Text(
            composer,
            height=2,
            wrap="word",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            padx=8,
            pady=6,
            font=("Cascadia Mono", 10),
        )
        self.message_body.grid(row=3, column=0, columnspan=8, sticky="ew", padx=8, pady=(5, 4))
        self.message_body.bind("<Return>", self._on_message_enter)
        self.chat_attach_button = tk.Button(
            composer,
            text=self._t("chat.attach"),
            command=self._choose_chat_attachments,
            bg=COLORS["blue"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=8,
            pady=3,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.chat_attach_button.grid(row=4, column=0, sticky="w", padx=(8, 4), pady=(2, 2))
        self.chat_clear_attachments_button = tk.Button(
            composer,
            text=self._t("chat.clear_attachments"),
            command=self._clear_chat_attachments,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            activebackground=COLORS["line"],
            relief="raised",
            bd=2,
            padx=6,
            pady=3,
            font=("Cascadia Mono", 8),
        )
        self.chat_clear_attachments_button.grid(
            row=4, column=1, sticky="w", padx=4, pady=(2, 2)
        )
        self.chat_attachment_label = tk.Label(
            composer,
            textvariable=self.chat_attachment_status,
            bg=COLORS["black"],
            fg=COLORS["muted"],
            anchor="w",
            font=("Cascadia Mono", 8),
        )
        self.chat_attachment_label.grid(
            row=4, column=2, columnspan=5, sticky="ew", padx=4, pady=(2, 2)
        )
        self.send_button = tk.Button(
            composer,
            text=self._t("chat.send"),
            command=self.send_human_message,
            bg="#202124" if ACTIVE_THEME == "modern" else COLORS["cyan"],
            fg="#ffffff" if ACTIVE_THEME == "modern" else COLORS["black"],
            activebackground=COLORS["green"],
            relief="flat" if ACTIVE_THEME == "modern" else "raised",
            bd=0 if ACTIVE_THEME == "modern" else 2,
            padx=12,
            pady=5,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.send_button.grid(row=4, column=7, sticky="e", padx=8, pady=(2, 2))
        self.chat_attachment_note = tk.Label(
            composer,
            text=self._t("chat.attachment_note"),
            bg=COLORS["black"],
            fg=COLORS["muted"],
            anchor="w",
            font=("Cascadia Mono", 7),
        )
        self.message_status_label = tk.Label(
            composer,
            textvariable=self.message_status,
            bg=COLORS["black"],
            fg=COLORS["muted"],
            anchor="w",
            font=("Cascadia Mono", 8),
        )
        self.message_status_label.grid(row=5, column=0, columnspan=8, sticky="ew", padx=8, pady=(2, 5))
        self.chat_composer = composer
        composer.update_idletasks()
        self._chat_composer_wide_required_width = max(1, composer.winfo_reqwidth())
        self._chat_composer_compact = False
        composer.bind("<Configure>", self._reflow_chat_composer)
        self.chat_split.add(
            self.chat_composer_pane,
            minsize=106 if ACTIVE_THEME == "modern" else 215,
            stretch="never",
        )
        self.root.after_idle(self._layout_chat_after_resize)
        self.pages["chat"] = outer_page

    def _room_has_operator(self) -> bool:
        if self.selected_room_id == DEFAULT_ROOM_ID:
            return True
        return any(
            row.get("agent_id") == HUMAN_AGENT_ID and row.get("status") == "active"
            for row in self._room_members
        )

    def _sync_room_control_states(self) -> None:
        custom_room = self.selected_room_id != DEFAULT_ROOM_ID
        operator_active = self._room_has_operator()
        busy = self.room_action_in_progress
        library_available = bool(self._library_agents)
        selected_agent = str(self._selected_room_seat_agent or "")
        selected_member = next(
            (
                row
                for row in self._room_members
                if row.get("status") == "active"
                and str(row.get("agent_id") or "") == selected_agent
            ),
            None,
        )
        role_target_available = bool(
            selected_agent
            and selected_agent != HUMAN_AGENT_ID
            and (selected_member is not None or self.selected_room_id == DEFAULT_ROOM_ID)
        )
        role_controls = operator_active and role_target_available and not busy
        self.new_room_button.configure(state="disabled" if busy else "normal")
        self.operator_room_button.configure(
            state="normal" if custom_room and not operator_active and not busy else "disabled"
        )
        self.add_seat_button.configure(
            state="normal" if operator_active and library_available and not busy else "disabled"
        )
        self.remove_seat_button.configure(
            state="normal" if operator_active and not busy else "disabled"
        )
        self.seat_agent_combo.configure(
            state="readonly" if operator_active and library_available and not busy else "disabled"
        )
        provider_values = tuple(self._seat_provider_ids)
        self.seat_provider_combo.configure(
            values=provider_values,
            state=(
                "readonly"
                if operator_active and self.seat_agent.get() and provider_values and not busy
                else "disabled"
            ),
        )
        self.seat_model_combo.configure(
            state=(
                "readonly"
                if operator_active
                and self.seat_provider_choice.get()
                and self.seat_model_combo.cget("values")
                and not busy
                else "disabled"
            )
        )
        self.seat_reasoning_combo.configure(
            state=(
                "readonly"
                if operator_active
                and self.seat_model_choice.get()
                and self.seat_reasoning_combo.cget("values")
                and not busy
                else "disabled"
            )
        )
        if not self.send_in_progress:
            self.send_button.configure(state="normal")
            self.chat_attach_button.configure(
                state="normal" if operator_active else "disabled"
            )
            self.chat_clear_attachments_button.configure(
                state=(
                    "normal"
                    if operator_active and self._chat_attachment_paths
                    else "disabled"
                )
            )
        self.older_history_button.configure(
            state=(
                "normal"
                if self._room_page_has_older and not self.room_refresh_in_progress
                else "disabled"
            )
        )
        self.latest_history_button.configure(
            state=(
                "normal"
                if len(self._room_history_stack) > 1 and not self.room_refresh_in_progress
                else "disabled"
            )
        )
        policy_controls = operator_active and not busy
        self.room_automation_combo.configure(
            state="readonly" if policy_controls else "disabled"
        )
        self.apply_automation_button.configure(
            state="normal" if policy_controls else "disabled"
        )
        guided_state = self._guided_workflow_state(operator_active=operator_active)
        self.guided_workflow_button.configure(
            state=(
                "normal"
                if not busy and bool(guided_state["ready"])
                else "disabled"
            )
        )
        self.seat_role_combo.configure(
            state="readonly" if role_controls else "disabled"
        )
        self.apply_role_button.configure(
            state="normal" if role_controls else "disabled"
        )
        self.seat_custom_role_entry.configure(
            state=(
                "normal"
                if role_controls and self._selected_room_role()[0] == "custom"
                else "disabled"
            )
        )
        self.view_live_work_button.configure(
            state=(
                "normal"
                if selected_member is not None
                and bool(selected_member.get("room_session_id"))
                and not busy
                else "disabled"
            )
        )
        discussion = self._active_discussion or {}
        discussion_state = str(discussion.get("status") or "")
        self.pause_discussion_button.configure(
            state=(
                "normal"
                if policy_controls and discussion_state == "active"
                else "disabled"
            )
        )
        self.continue_discussion_button.configure(
            state=(
                "normal"
                if policy_controls and discussion_state == "waiting_human"
                else "disabled"
            )
        )
        self.resume_discussion_button.configure(
            state=(
                "normal"
                if policy_controls and discussion_state == "paused"
                else "disabled"
            )
        )
        self.stop_discussion_button.configure(
            state=(
                "normal"
                if policy_controls
                and discussion_state in {"active", "paused", "waiting_human"}
                else "disabled"
            )
        )

    def _guided_workflow_state(
        self, *, operator_active: bool | None = None
    ) -> dict[str, Any]:
        if operator_active is None:
            operator_active = self._room_has_operator()
        if getattr(self, "_room_view_unavailable", False):
            return {"ready": False, "code": "room_unavailable"}
        if not operator_active:
            return {"ready": False, "code": "join_control"}
        if self.room_refresh_in_progress:
            return {"ready": False, "code": "loading"}
        discussion_state = str((self._active_discussion or {}).get("status") or "")
        if discussion_state in {"active", "paused", "waiting_human"}:
            return {"ready": False, "code": "active_discussion"}
        return guided_room_readiness(self._room_members)

    def _refresh_guided_workflow_readiness(self) -> None:
        if getattr(self, "room_action_in_progress", False) or not hasattr(
            self, "guided_workflow_status"
        ):
            return
        state = self._guided_workflow_state()
        code = str(state["code"])
        self.guided_workflow_status.set(
            self._t(f"chat.guided.readiness.{code}").format(
                count=int(state.get("participant_count") or 0),
                missing=int(state.get("missing_route_count") or 0),
                maximum=int(
                    state.get("maximum_participants") or MAX_GUIDED_ROOM_AGENTS
                ),
            )
        )
        self.guided_workflow_status_label.configure(
            fg=(
                COLORS["green"]
                if state["ready"]
                else COLORS["amber"]
                if code in {"loading", "active_discussion"}
                else COLORS["red"]
            )
        )

    def apply_room_automation(self) -> None:
        mode = self._automation_mode_from_label(self.room_automation_choice.get())
        if mode is None:
            self.discussion_status.set(self._t("chat.automation.invalid"))
            self.discussion_status_label.configure(fg=COLORS["red"])
            return
        try:
            max_rounds = int(self.room_round_limit.get())
            max_messages = int(self.room_message_limit.get())
            stagnation_rounds = int(self.room_stagnation_limit.get())
        except ValueError:
            self.discussion_status.set(self._t("discussion.limit_integer"))
            self.discussion_status_label.configure(fg=COLORS["red"])
            return
        self._run_room_action(
            lambda: self.human_client.set_room_automation(
                room_id=self.selected_room_id,
                mode=mode,
                max_rounds=max_rounds,
                max_messages=max_messages,
                stagnation_rounds=stagnation_rounds,
            ),
            pending="AUTO // APPLYING...",
            target=self.discussion_status,
            target_label=self.discussion_status_label,
            success=lambda receipt: (
                f"AUTO // {str(receipt['mode']).upper()} // "
                f"R{receipt['max_rounds']} M{receipt['max_messages']} "
                f"S{receipt['stagnation_rounds']}"
            ),
        )
    def start_guided_room_workflow(self) -> None:
        if self.room_action_in_progress:
            return
        current_discussion = self._active_discussion or {}
        if str(current_discussion.get("status") or "") in {
            "active",
            "paused",
            "waiting_human",
        }:
            self.guided_workflow_status.set(
                self._t("chat.guided.active_discussion")
            )
            self.guided_workflow_status_label.configure(fg=COLORS["amber"])
            return
        task = self.message_body.get("1.0", "end-1c").strip()
        if not task:
            task = self._t("chat.guided.default_task")
        task_id = f"guided-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        try:
            plan = guided_room_workflow_plan(
                room_id=self.selected_room_id,
                task_id=task_id,
                task_text=task,
                members=self._room_members,
            )
        except GuidedRoomWorkflowError as exc:
            self.guided_workflow_status.set(
                self._t("chat.guided.unavailable").format(
                    error=redact_secrets(str(exc))[:150]
                )
            )
            self.guided_workflow_status_label.configure(fg=COLORS["red"])
            return

        def start() -> dict[str, Any]:
            operation: dict[str, Any] | None = None
            message: dict[str, Any] | None = None
            try:
                operation = self.human_client.enqueue_workflow(
                    operation_id=str(plan["operation_id"]),
                    workflow_id=str(plan["workflow_id"]),
                    task_text=str(plan["operation_task_text"]),
                    working_directory=str(plan["operation_working_directory"]),
                    resource_key=str(plan["operation_resource_key"]),
                    max_attempts=int(plan["operation_max_attempts"]),
                    timeout_seconds=int(plan["operation_timeout_seconds"]),
                )
                policy = self.human_client.set_room_automation(
                    room_id=str(plan["room_id"]),
                    mode=str(plan["automation_mode"]),
                    max_rounds=int(plan["max_rounds"]),
                    max_messages=int(plan["max_messages"]),
                    stagnation_rounds=int(plan["stagnation_rounds"]),
                )
                message = self.human_client.post_room_message(
                    room_id=str(plan["room_id"]),
                    task_id=str(plan["task_id"]),
                    subject=str(plan["subject"]),
                    body=str(plan["body"]),
                    priority=str(plan["priority"]),
                )
                current_members = self.human_client.room_members(
                    room_id=str(plan["room_id"])
                )["members"]
                binding = validate_guided_room_start(
                    plan,
                    message,
                    members=current_members,
                )
                bound_operation = self.human_client.bind_guided_discussion(
                    operation_id=str(plan["operation_id"]),
                    discussion_id=str(binding["discussion_id"]),
                )
                return {
                    "plan": plan,
                    "operation": bound_operation,
                    "policy": policy,
                    "message": message,
                    "binding": binding,
                }
            except Exception:
                if message and message.get("discussion_id"):
                    with contextlib.suppress(Exception):
                        self.human_client.control_discussion(
                            discussion_id=str(message["discussion_id"]),
                            action="stop",
                        )
                if operation is not None:
                    with contextlib.suppress(Exception):
                        self.human_client.cancel_operation(
                            operation_id=str(plan["operation_id"]),
                            reason="Guided discussion failed source-bound startup.",
                        )
                raise

        def after_success(receipt: dict[str, Any]) -> None:
            completed_plan = receipt["plan"]
            self.room_automation_choice.set(
                self._t(AUTOMATION_MODE_TO_KEY[str(completed_plan["automation_mode"])])
            )
            self.room_round_limit.set(str(completed_plan["max_rounds"]))
            self.room_message_limit.set(str(completed_plan["max_messages"]))
            self.room_stagnation_limit.set(
                str(completed_plan["stagnation_rounds"])
            )

        self._run_room_action(
            start,
            pending=self._t("chat.guided.starting"),
            target=self.guided_workflow_status,
            target_label=self.guided_workflow_status_label,
            success=lambda receipt: self._t("chat.guided.started").format(
                count=receipt["plan"]["participant_count"],
                task=receipt["plan"]["task_id"],
            ),
            after_success=after_success,
        )

    def control_active_discussion(self, action: str) -> None:
        discussion_id = str(
            (self._active_discussion or {}).get("discussion_id") or ""
        )
        if not discussion_id:
            return
        task_id = str((self._active_discussion or {}).get("task_id") or "")

        def control() -> dict[str, Any]:
            receipt = self.human_client.control_discussion(
                discussion_id=discussion_id,
                action=action,
            )
            if action == "stop" and task_id.startswith("guided-"):
                with contextlib.suppress(Exception):
                    self.human_client.cancel_operation(
                        operation_id=guided_operation_id(
                            self.selected_room_id, task_id
                        ),
                        reason="Guided discussion stopped by the operator.",
                    )
            return receipt

        self._run_room_action(
            control,
            pending=self._t("chat.discussion.pending").format(
                action=self._t(f"chat.{action}")
            ),
            target=self.discussion_status,
            target_label=self.discussion_status_label,
            success=lambda receipt: self._t("chat.discussion.round").format(
                status=self._discussion_status_text(receipt["status"]),
                current=receipt["current_round"],
                maximum=receipt["max_rounds"],
                reason="",
            ),
        )

    def load_older_room_history(self) -> None:
        if self.room_refresh_in_progress or not self._room_page_has_older:
            return
        sequences = [
            int(row["sequence"])
            for row in self._room_messages
            if row.get("sequence") is not None
        ]
        if not sequences:
            return
        self._room_history_stack.append(min(sequences))
        self._request_room_refresh(force=True)

    def load_latest_room_history(self) -> None:
        if self.room_refresh_in_progress or len(self._room_history_stack) <= 1:
            return
        self._room_history_stack = [None]
        self._request_room_refresh(force=True)

    def _request_room_refresh(self, *, force: bool = False) -> None:
        if self.room_refresh_in_progress:
            if force:
                self._room_refresh_pending = True
            return
        interval = max(2.0, self.REFRESH_MS / 1000.0)
        if not force and time.monotonic() - self._last_room_refresh < interval:
            return
        requested_room = self.selected_room_id
        before_sequence = self._room_history_stack[-1]
        self.room_refresh_in_progress = True
        self._sync_room_control_states()

        def worker() -> None:
            try:
                view = self.reader.room_view(
                    scope=self.scope,
                    requested_room_id=requested_room,
                    consumer=HUMAN_AGENT_ID,
                    before_sequence=before_sequence,
                )
                rooms = tuple(view["rooms"])
                room_id = str(view["room_id"])
                members = tuple(view["members"])
                messages = tuple(view["messages"])
                catalog_agents = tuple(view["catalog_agents"])
                automation = dict(view["automation"])
                page = dict(view["page"])
                poll_error = (
                    RuntimeError(str(view["read_error"]))
                    if view.get("read_error")
                    else None
                )
            except Exception as exc:
                self._post_to_ui(
                    self._finish_room_refresh,
                    None,
                    requested_room,
                    (),
                    (),
                    (),
                    {},
                    {},
                    exc,
                )
                return
            self._post_to_ui(
                self._finish_room_refresh,
                rooms,
                room_id,
                members,
                messages,
                catalog_agents,
                automation,
                page,
                poll_error,
            )

        threading.Thread(target=worker, name="mcp-room-refresh", daemon=True).start()

    def _finish_room_refresh(
        self,
        rooms: tuple[dict[str, Any], ...] | None,
        room_id: str,
        members: tuple[dict[str, Any], ...],
        messages: tuple[dict[str, Any], ...],
        catalog_agents: tuple[dict[str, Any], ...],
        automation: dict[str, Any],
        page: dict[str, Any],
        error: Exception | None,
    ) -> None:
        self.room_refresh_in_progress = False
        self._last_room_refresh = time.monotonic()
        pending_refresh = self._room_refresh_pending
        self._room_refresh_pending = False
        if pending_refresh and not self._closing:
            self.root.after_idle(lambda: self._request_room_refresh(force=True))
        if rooms is None:
            self._room_view_unavailable = True
            self.room_status.set(
                self._t("chat.room_api_error").format(
                    error=clip(redact_sensitive(error), 120)
                )
            )
            self.room_status_label.configure(fg=COLORS["red"])
            if ACTIVE_THEME == "modern":
                connection = getattr(self, "modern_toolbar_connection_label", None)
                if connection is not None:
                    connection.configure(
                        text=self._t("modern.toolbar.unavailable"),
                        fg=COLORS["red"],
                    )
            self._refresh_guided_workflow_readiness()
            self._sync_room_control_states()
            return
        self._room_view_unavailable = False

        room_view_signature = hashlib.sha256(
            json.dumps(
                {
                    "rooms": rooms,
                    "room_id": room_id,
                    "members": members,
                    "messages": messages,
                    "catalog_agents": catalog_agents,
                    "automation": automation,
                    "page": page,
                    "error": redact_sensitive(error),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if room_view_signature == self._last_room_view_signature:
            self._sync_room_control_states()
            self._sync_modern_recent_rooms()
            self._sync_modern_chat_context()
            self._render_modern_agent_inspector()
            return
        self._last_room_view_signature = room_view_signature

        self._rooms = {
            str(row["room_id"]): row for row in rooms if row.get("room_id")
        }
        labels = tuple(room_display_label(row) for row in rooms)
        self._room_ids_by_label = {
            room_display_label(row): str(row["room_id"])
            for row in rooms
            if row.get("room_id")
        }
        self.room_combo.configure(values=labels)
        if self.selected_room_id not in self._rooms:
            self.selected_room_id = room_id
        selected_label = next(
            (
                label
                for label, candidate in self._room_ids_by_label.items()
                if candidate == self.selected_room_id
            ),
            "",
        )
        self.room_choice.set(selected_label)
        self.cockpit.set_room_context(self.selected_room_id)
        if room_id != self.selected_room_id:
            self._request_room_refresh(force=True)
            return

        self._room_members = members
        self._room_messages = messages
        self._room_page_has_older = bool(page.get("has_older"))
        self._catalog_agents = catalog_agents
        mode = str(automation.get("mode") or "once")
        self.room_automation_choice.set(
            self._t(AUTOMATION_MODE_TO_KEY.get(mode, AUTOMATION_MODE_TO_KEY["once"]))
        )
        self.room_round_limit.set(str(automation.get("max_rounds") or 4))
        self.room_message_limit.set(str(automation.get("max_messages") or 40))
        self.room_stagnation_limit.set(str(automation.get("stagnation_rounds") or 2))
        self._active_discussion = automation.get("active_discussion")
        if self._active_discussion:
            discussion = self._active_discussion
            status = str(discussion.get("status") or "unknown").lower()
            reason = str(discussion.get("stop_reason") or "")
            self.discussion_status.set(
                self._t("chat.discussion.round").format(
                    status=self._discussion_status_text(status),
                    current=discussion.get("current_round"),
                    maximum=discussion.get("max_rounds"),
                    reason=f" // {reason}" if reason else "",
                )
            )
            self.discussion_status_label.configure(
                fg=COLORS["amber"] if status != "active" else COLORS["green"]
            )
        else:
            self.discussion_status.set(
                self._t("chat.automation.idle").format(
                    mode=self._t(
                        AUTOMATION_MODE_TO_KEY.get(
                            mode, AUTOMATION_MODE_TO_KEY["once"]
                        )
                    )
                )
            )
            self.discussion_status_label.configure(fg=COLORS["muted"])
        room = self._rooms.get(room_id, {})
        active_seats = sum(1 for row in members if row.get("status") == "active")
        history = int(room.get("message_count") or len(messages))
        oldest = page.get("oldest_sequence")
        newest = page.get("newest_sequence")
        page_text = (
            self._t("chat.room_status.loaded_range").format(
                loaded=len(messages), oldest=oldest, newest=newest
            )
            if oldest is not None and newest is not None
            else self._t("chat.room_status.loaded").format(loaded=len(messages))
        )
        self.room_status.set(
            self._t("chat.room_status.base").format(
                room=room_id, seats=active_seats, history=history
            )
            + page_text
            + (f" // {clip(redact_sensitive(error), 70)}" if error else "")
        )
        self.room_status_label.configure(fg=COLORS["red"] if error else COLORS["green"])
        if ACTIVE_THEME == "modern":
            connection = getattr(self, "modern_toolbar_connection_label", None)
            if connection is not None:
                connection.configure(
                    text=self._t("modern.toolbar.connected"),
                    fg=COLORS["green"],
                )
        self._render_room_seats()
        self._sync_modern_recent_rooms()
        self._sync_modern_chat_context()
        self._render_modern_agent_inspector()
        self._refresh_guided_workflow_readiness()
        self._sync_room_control_states()
        if self.snapshot:
            self.render(force=True)

    def _on_room_selected(self, _event: Any = None) -> None:
        room_id = self._room_ids_by_label.get(self.room_choice.get())
        if not room_id or room_id == self.selected_room_id:
            return
        self.selected_room_id = room_id
        self.cockpit.set_room_context(room_id)
        self._room_members = ()
        self._room_messages = ()
        self._room_history_stack = [None]
        self._room_page_has_older = False
        self._active_discussion = None
        self.message_recipient.set(self._t(BROADCAST_LABEL))
        self._reset_route_selection()
        self.room_status.set(
            self._t("chat.room_status.loading").format(room=room_id)
        )
        self.room_status_label.configure(fg=COLORS["amber"])
        self.guided_workflow_status.set(self._t("chat.guided.readiness.loading"))
        self.guided_workflow_status_label.configure(fg=COLORS["amber"])
        self._render_room_seats()
        self._sync_modern_recent_rooms()
        self._sync_modern_chat_context()
        self._render_modern_agent_inspector()
        self._sync_room_control_states()
        if self.snapshot:
            self.render(force=True)
        self._request_room_refresh(force=True)

    def _open_create_room_dialog(self) -> None:
        if self.room_action_in_progress:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(self._t("chat.room_dialog.title"))
        dialog.geometry("520x210")
        dialog.resizable(False, False)
        dialog.configure(bg=COLORS["black"])
        dialog.transient(self.root)
        dialog.grab_set()
        room_id = tk.StringVar(value="")
        room_name = tk.StringVar(value="")
        panel = tk.Frame(dialog, bg=COLORS["panel"], bd=2, relief="ridge")
        panel.pack(fill="both", expand=True, padx=12, pady=12)
        panel.grid_columnconfigure(1, weight=1)
        for row_index, (label, variable) in enumerate(
            (
                (self._t("chat.room_dialog.id"), room_id),
                (self._t("chat.room_dialog.name"), room_name),
            )
        ):
            tk.Label(
                panel,
                text=label,
                bg=COLORS["panel"],
                fg=COLORS["amber"],
                font=("Cascadia Mono", 9, "bold"),
            ).grid(row=row_index, column=0, sticky="w", padx=10, pady=10)
            tk.Entry(
                panel,
                textvariable=variable,
                bg=COLORS["black"],
                fg=COLORS["text"],
                insertbackground=COLORS["cyan"],
                relief="sunken",
                bd=2,
                font=("Cascadia Mono", 10),
            ).grid(row=row_index, column=1, sticky="ew", padx=10, pady=10, ipady=4)
        buttons = tk.Frame(panel, bg=COLORS["panel"])
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", padx=10, pady=8)
        tk.Button(
            buttons,
            text=self._t("chat.room_dialog.cancel"),
            command=dialog.destroy,
            bg=COLORS["line"],
            fg=COLORS["text"],
            bd=2,
            font=("Cascadia Mono", 8, "bold"),
        ).pack(side="left", padx=4)
        tk.Button(
            buttons,
            text=self._t("chat.room_dialog.create"),
            command=lambda: self._submit_new_room(dialog, room_id.get(), room_name.get()),
            bg=COLORS["green"],
            fg=COLORS["black"],
            bd=2,
            font=("Cascadia Mono", 8, "bold"),
        ).pack(side="left", padx=4)
        dialog.bind(
            "<Return>",
            lambda _event: self._submit_new_room(dialog, room_id.get(), room_name.get()),
        )
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.after_idle(lambda: dialog.focus_force())

    def _run_room_action(
        self,
        operation: Callable[[], dict[str, Any]],
        *,
        pending: str,
        target: tk.StringVar,
        target_label: tk.Label,
        success: Callable[[dict[str, Any]], str],
        after_success: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if self.room_action_in_progress:
            return
        self.room_action_in_progress = True
        expected_room_id = self.selected_room_id
        target.set(pending)
        target_label.configure(fg=COLORS["amber"])
        self._sync_room_control_states()

        def worker() -> None:
            try:
                receipt = operation()
            except Exception as exc:
                self._post_to_ui(
                    self._finish_room_action,
                    None,
                    exc,
                    target,
                    target_label,
                    success,
                    after_success,
                    expected_room_id,
                )
                return
            self._post_to_ui(
                self._finish_room_action,
                receipt,
                None,
                target,
                target_label,
                success,
                after_success,
                expected_room_id,
            )

        threading.Thread(target=worker, name="mcp-room-action", daemon=True).start()

    def _finish_room_action(
        self,
        receipt: dict[str, Any] | None,
        error: Exception | None,
        target: tk.StringVar,
        target_label: tk.Label,
        success: Callable[[dict[str, Any]], str],
        after_success: Callable[[dict[str, Any]], None] | None,
        expected_room_id: str,
    ) -> None:
        self.room_action_in_progress = False
        if self.selected_room_id != expected_room_id:
            self._sync_room_control_states()
            return
        if error:
            target.set(
                self._t("status.failed").format(
                    error=clip(redact_sensitive(error), 150)
                )
            )
            target_label.configure(fg=COLORS["red"])
            self._sync_room_control_states()
            return
        assert receipt is not None
        if after_success:
            after_success(receipt)
        target.set(success(receipt))
        target_label.configure(fg=COLORS["green"])
        self._sync_room_control_states()
        self._request_room_refresh(force=True)

    def _submit_new_room(self, dialog: tk.Toplevel, room_id: str, name: str) -> None:
        try:
            clean_id = self._safe_identifier(
                room_id, self._t("chat.room_dialog.id")
            )
            clean_name = name.strip()
            if clean_id == DEFAULT_ROOM_ID:
                raise ValueError(self._t("room.lobby_exists"))
            if not clean_name or len(clean_name) > 200:
                raise ValueError(self._t("room.name_length"))
            if contains_secret(clean_name):
                raise ValueError(self._t("room.name_sensitive"))
        except ValueError as exc:
            self.room_status.set(redact_sensitive(exc))
            self.room_status_label.configure(fg=COLORS["red"])
            return
        dialog.grab_release()
        dialog.destroy()

        def selected(receipt: dict[str, Any]) -> None:
            self.selected_room_id = str(receipt["room_id"])
            self._room_members = ()
            self._room_messages = ()

        self._run_room_action(
            lambda: self.human_client.create_room(room_id=clean_id, name=clean_name),
            pending=f"CREATING {clean_id}...",
            target=self.room_status,
            target_label=self.room_status_label,
            success=lambda receipt: f"CREATED {receipt['room_id']} // CTRL SEATED",
            after_success=selected,
        )

    def join_selected_room_as_operator(self) -> None:
        room_id = self.selected_room_id
        if room_id == DEFAULT_ROOM_ID or self._room_has_operator():
            return
        self._run_room_action(
            lambda: self.human_client.join_room(
                room_id=room_id, agent_id=HUMAN_AGENT_ID
            ),
            pending=f"JOINING CTRL TO {room_id}...",
            target=self.room_status,
            target_label=self.room_status_label,
            success=lambda receipt: (
                f"CTRL SEATED // SESSION {receipt['room_session_id']}"
            ),
        )

    def _agent_provider_ids(self, agent_id: str) -> tuple[str, ...]:
        if not self.snapshot:
            return ()
        return tuple(
            sorted(
                {
                    str(row.get("provider_id") or "")
                    for row in self.snapshot.route_profiles
                    if row.get("scope") == self.scope
                    and row.get("enabled")
                    and row.get("agent_id") == agent_id
                    and row.get("provider_id")
                }
            )
        )

    def _schedule_provider_model_discovery(
        self,
        *,
        provider_ids: Iterable[str] | None = None,
        force: bool = False,
    ) -> None:
        """Refresh WCM-backed provider catalogs without moving secrets into MCP."""

        if not self.snapshot:
            return
        selected = set(provider_ids or ())
        if not selected or "openai-official" in selected:
            self._schedule_codex_model_discovery(force=force)
        now = time.monotonic()
        for raw in self.snapshot.provider_connections:
            connection = dict(raw)
            connection_id = str(connection.get("connection_id") or "")
            if (
                connection.get("scope") != self.scope
                or not connection.get("enabled")
                or connection.get("secret_backend") != "windows-credential-manager"
                or not connection_id
                or (selected and connection_id not in selected)
            ):
                continue
            version = str(
                connection.get("credential_version_sha256")
                or connection.get("connection_sha256")
                or "unknown"
            )
            if force:
                self._provider_discovery_retry_at.pop(connection_id, None)
            elif self._provider_model_catalog_version.get(connection_id) == version:
                continue
            if connection_id in self._provider_discovery_inflight:
                continue
            if now < self._provider_discovery_retry_at.get(connection_id, 0.0):
                continue
            self._provider_discovery_inflight.add(connection_id)

            def worker(
                item: dict[str, Any] = connection,
                expected_version: str = version,
            ) -> None:
                cid = str(item["connection_id"])
                try:
                    discovery = discover_provider_models(
                        scope=self.scope,
                        connection_id=cid,
                        route_class=str(item.get("route_class") or ""),
                        provider_id=str(item.get("provider_id") or cid),
                    )
                except Exception as exc:
                    self._post_to_ui(
                        self._finish_provider_model_discovery,
                        cid,
                        expected_version,
                        None,
                        exc,
                    )
                    return
                self._post_to_ui(
                    self._finish_provider_model_discovery,
                    cid,
                    expected_version,
                    discovery,
                    None,
                )

            threading.Thread(
                target=worker,
                name=f"provider-model-catalog-{connection_id[:24]}",
                daemon=True,
            ).start()

    def _schedule_codex_model_discovery(self, *, force: bool = False) -> None:
        """Refresh the installed official Codex client's visible model catalog."""

        now = time.monotonic()
        if self._codex_catalog_inflight:
            return
        if not force and self._codex_model_catalog is not None:
            return
        if not force and now < self._codex_catalog_retry_at:
            return
        self._codex_catalog_inflight = True

        def worker() -> None:
            try:
                catalog = discover_codex_model_catalog()
            except Exception as exc:
                self._post_to_ui(self._finish_codex_model_discovery, None, exc)
                return
            self._post_to_ui(self._finish_codex_model_discovery, catalog, None)

        threading.Thread(
            target=worker,
            name="official-codex-model-catalog",
            daemon=True,
        ).start()

    def _finish_codex_model_discovery(
        self,
        catalog: CodexModelCatalog | None,
        error: Exception | None,
    ) -> None:
        self._codex_catalog_inflight = False
        if error:
            self._codex_catalog_error = self._safe_error(error)
            self._codex_catalog_retry_at = time.monotonic() + 300.0
            if self.seat_agent.get():
                self._on_seat_agent_selected()
            self._render_room_agent_cards()
            return
        assert catalog is not None
        self._codex_model_catalog = catalog
        self._codex_catalog_error = None
        self._codex_catalog_retry_at = 0.0
        if self.seat_agent.get():
            self._on_seat_agent_selected()
        self._render_room_agent_cards()

    def _finish_provider_model_discovery(
        self,
        connection_id: str,
        version: str,
        discovery: ProviderModelRegistry | None,
        error: Exception | None,
    ) -> None:
        self._provider_discovery_inflight.discard(connection_id)
        if error:
            self._provider_discovery_errors[connection_id] = self._safe_error(error)
            self._provider_discovery_retry_at[connection_id] = time.monotonic() + 300.0
            if self.seat_agent.get():
                self._on_seat_agent_selected()
            self._render_room_agent_cards()
            return
        assert discovery is not None
        self._provider_model_catalog[connection_id] = tuple(discovery.models)
        self._provider_model_registry_sha256[connection_id] = discovery.registry_sha256
        self._provider_model_catalog_version[connection_id] = version
        self._provider_discovery_errors.pop(connection_id, None)
        self._provider_discovery_retry_at.pop(connection_id, None)
        if self.seat_agent.get():
            self._on_seat_agent_selected()
        self._render_room_agent_cards()

    def _refresh_agent_provider_models(self, agent_id: str) -> None:
        provider_ids = self._agent_provider_ids(agent_id)
        local_codex = agent_id == "codex-main"
        if not provider_ids and not local_codex:
            self.seat_status.set(
                self._t("seat.no_connections").format(agent=agent_id)
            )
            self.seat_status_label.configure(fg=COLORS["red"])
            return
        if local_codex:
            self._schedule_codex_model_discovery(force=True)
        self._schedule_provider_model_discovery(
            provider_ids=provider_ids,
            force=True,
        )
        self.seat_status.set(self._t("seat.refreshing").format(agent=agent_id))
        self.seat_status_label.configure(fg=COLORS["amber"])

    def _on_seat_agent_selected(self, _event: Any = None) -> None:
        agent_id = self.seat_agent.get().strip()
        self.library_selection.set(self._library_selection_text(agent_id or None))
        self._draw_agents(list(self._library_agents))
        registered_profiles = tuple(
            row
            for row in (self.snapshot.route_profiles if self.snapshot else ())
            if row.get("scope") == self.scope
            and row.get("enabled")
            and row.get("agent_id") == agent_id
        )
        connections = tuple(
            row
            for row in (self.snapshot.provider_connections if self.snapshot else ())
            if row.get("scope") == self.scope and row.get("enabled")
        )
        self._seat_profiles = merge_agent_route_options(
            agent_route_options(
                agent_id,
                registered_profiles,
                connections,
                self._provider_model_catalog,
                self._provider_model_registry_sha256,
            ),
            codex_catalog_route_options(agent_id, self._codex_model_catalog),
        )
        self._seat_provider_ids = {}
        for profile in self._seat_profiles:
            provider_id = str(profile.get("provider_id") or "")
            if not provider_id:
                continue
            label = self._profile_provider_label(profile)
            if label in self._seat_provider_ids and self._seat_provider_ids[label] != provider_id:
                label = f"{label} [{provider_id}]"
            self._seat_provider_ids[label] = provider_id
        self._reset_seat_route_selection()

        active_member = next(
            (
                row
                for row in self._room_members
                if row.get("status") == "active" and row.get("agent_id") == agent_id
            ),
            None,
        )
        self._set_seat_role_from_member(active_member)
        current_route_id = str((active_member or {}).get("route_profile_id") or "")
        current_profile = next(
            (
                row
                for row in self._seat_profiles
                if str(row.get("route_id") or "") == current_route_id
            ),
            None,
        )
        if current_profile:
            self._select_seat_profile(current_profile)
            self._selected_room_seat_agent = agent_id
            self.seat_status.set(self._t("seat.selected").format(agent=agent_id))
            self.seat_status_label.configure(fg=COLORS["cyan"])
        elif len(self._seat_provider_ids) == 1:
            self.seat_provider_choice.set(next(iter(self._seat_provider_ids)))
            self._on_seat_provider_selected()
        elif not self._seat_profiles and agent_id:
            self.seat_status.set(self._t("seat.no_route").format(agent=agent_id))
            self.seat_status_label.configure(fg=COLORS["red"])
        elif agent_id:
            self.seat_status.set(
                self._t("seat.multiple_providers").format(agent=agent_id)
            )
            self.seat_status_label.configure(fg=COLORS["amber"])
        self._sync_room_control_states()

    def _set_seat_role_from_member(
        self, member: Mapping[str, Any] | None
    ) -> None:
        role_id = str((member or {}).get("role_id") or DEFAULT_ROOM_ROLE)
        if role_id not in ROOM_ROLE_IDS:
            role_id = DEFAULT_ROOM_ROLE
        label = next(
            (
                item
                for item, candidate in self._seat_role_labels.items()
                if candidate == role_id
            ),
            role_id,
        )
        self.seat_role_choice.set(label)
        self.seat_custom_role.set(
            str((member or {}).get("role_label") or "")
            if role_id == "custom"
            else ""
        )
        self._on_seat_role_selected()

    def _selected_room_role(self) -> tuple[str, str | None]:
        role_id = (
            self._room_role_from_label(self.seat_role_choice.get())
            or DEFAULT_ROOM_ROLE
        )
        role_label = self.seat_custom_role.get().strip() if role_id == "custom" else None
        return role_id, role_label

    def _on_seat_role_selected(self, _event: Any = None) -> None:
        role_id, _role_label = self._selected_room_role()
        operator_active = self._room_has_operator()
        selected_agent = str(self._selected_room_seat_agent or "")
        selected_member = any(
            row.get("status") == "active"
            and str(row.get("agent_id") or "") == selected_agent
            for row in self._room_members
        )
        editable = bool(
            operator_active
            and selected_agent
            and selected_agent != HUMAN_AGENT_ID
            and (selected_member or self.selected_room_id == DEFAULT_ROOM_ID)
            and not self.room_action_in_progress
        )
        self.seat_custom_role_entry.configure(
            state="normal" if editable and role_id == "custom" else "disabled"
        )

    def apply_room_member_role(self) -> None:
        agent_id = str(self._selected_room_seat_agent or "")
        if not agent_id or agent_id == HUMAN_AGENT_ID:
            self.seat_status.set(self._t("chat.role_select_agent"))
            self.seat_status_label.configure(fg=COLORS["red"])
            return
        role_id, role_label = self._selected_room_role()
        if role_id == "custom" and not role_label:
            self.seat_status.set(self._t("chat.role_custom_required"))
            self.seat_status_label.configure(fg=COLORS["red"])
            return
        room_id = self.selected_room_id
        self._run_room_action(
            lambda: self.human_client.set_room_member_role(
                room_id=room_id,
                agent_id=agent_id,
                role_id=role_id,
                role_label=role_label,
            ),
            pending=self._t("chat.role_applying").format(agent=agent_id),
            target=self.seat_status,
            target_label=self.seat_status_label,
            success=lambda receipt: self._t("chat.role_applied").format(
                agent=receipt["agent_id"],
                role=self._room_role_label(receipt),
            ),
            after_success=lambda _receipt: setattr(
                self, "_selected_room_seat_agent", agent_id
            ),
        )

    def _cockpit_external_sessions(
        self, after_sequences: Mapping[str, int]
    ) -> tuple[dict[str, Any], ...]:
        sessions: list[dict[str, Any]] = []
        selected_room = self._rooms.get(self.selected_room_id, {})
        selected_room_name = str(
            selected_room.get("name") or self.selected_room_id
        )
        operator_active = self.selected_room_id == DEFAULT_ROOM_ID or any(
            member.get("status") == "active"
            and member.get("agent_id") == HUMAN_AGENT_ID
            for member in self._room_members
        )
        snapshot = getattr(self, "snapshot", None)
        dispatches = tuple(getattr(snapshot, "message_dispatches", ()))
        for member in self._room_members:
            if member.get("status") != "active" or not member.get("room_session_id"):
                continue
            sessions.append(
                native_room_session_contract(
                    scope=self.scope,
                    room_id=self.selected_room_id,
                    member=member,
                    messages=self._room_messages,
                    dispatches=dispatches,
                    room_name=selected_room_name,
                    operator_active=operator_active,
                )
            )
        try:
            external = self.authorized_sessions.list_for_control_room(
                after_sequences=after_sequences
            )
            for snapshot in external:
                enriched = dict(snapshot)
                room_id = str(enriched.get("room_id") or "")
                if room_id and room_id != self.selected_room_id:
                    continue
                room = self._rooms.get(room_id, {})
                enriched["room_name"] = str(room.get("name") or room_id or "")
                enriched["source_conversation_name"] = str(
                    enriched.get("source_conversation_name")
                    or enriched.get("display_name")
                    or enriched.get("source_conversation_id")
                    or ""
                )
                sessions.append(enriched)
        except AuthorizedSessionError as exc:
            if hasattr(self, "cockpit"):
                self.cockpit.status.set(
                    self._t("cockpit.status.adapter_failed").format(
                        error=redact_secrets(str(exc))[:160]
                    )
                )
                self.cockpit.status_label.configure(fg=COLORS["red"])
        return tuple(sessions)

    def _send_cockpit_room_message(
        self,
        snapshot: Mapping[str, Any],
        body: str,
        artifact_paths: Iterable[str] = (),
    ) -> Mapping[str, Any]:
        if str(snapshot.get("source_type") or "") != "peerbridge-room":
            raise RuntimeError("Cockpit room input target is invalid")
        room_id = str(snapshot.get("room_id") or "").strip()
        agent_id = str(snapshot.get("agent_id") or "").strip()
        room_session_id = str(snapshot.get("source_session_id") or "").strip()
        route_profile_id = str(snapshot.get("requested_route") or "").strip()
        if not all((room_id, agent_id, room_session_id, route_profile_id)):
            raise RuntimeError("Cockpit room input target is incomplete")
        current_members = self.human_client.room_members(
            room_id=room_id, include_inactive=False
        ).get("members", ())
        operator_active = room_id == DEFAULT_ROOM_ID or any(
            row.get("status") == "active" and row.get("agent_id") == HUMAN_AGENT_ID
            for row in current_members
        )
        exact_targets = [
            row
            for row in current_members
            if row.get("status") == "active"
            and str(row.get("agent_id") or "") == agent_id
            and str(row.get("room_session_id") or "") == room_session_id
            and str(row.get("route_profile_id") or "") == route_profile_id
        ]
        if not operator_active or len(exact_targets) != 1:
            raise RuntimeError(
                "Selected room Agent changed or the human operator is not joined"
            )
        return self.human_client.send_message(
            room_id=room_id,
            recipient=agent_id,
            task_id=f"cockpit-{uuid.uuid4().hex[:20]}",
            subject="Cockpit conversation",
            body=body,
            priority="normal",
            route_profile_id=route_profile_id,
            artifact_paths=tuple(artifact_paths),
        )

    def _finish_cockpit_room_input(self, receipt: Mapping[str, Any]) -> None:
        if str(receipt.get("room_id") or "") == self.selected_room_id:
            self._request_room_refresh(force=True)
        self.refresh(force=True)

    def view_selected_agent_work(self) -> None:
        agent_id = str(self._selected_room_seat_agent or "")
        member = next(
            (
                row
                for row in self._room_members
                if row.get("status") == "active"
                and str(row.get("agent_id") or "") == agent_id
                and row.get("room_session_id")
            ),
            None,
        )
        if member is None:
            self.seat_status.set(self._t("chat.live_work_unavailable"))
            self.seat_status_label.configure(fg=COLORS["amber"])
            return
        external_sessions: tuple[dict[str, Any], ...] = ()
        try:
            external_sessions = self.authorized_sessions.list_for_control_room(
                include_detected=False
            )
        except AuthorizedSessionError:
            pass
        linked_target = linked_room_session_target(
            external_sessions,
            agent_id=agent_id,
            room_session_id=str(member["room_session_id"]),
        )
        source_type, source_session_id = linked_target or (
            "peerbridge-room",
            str(member["room_session_id"]),
        )
        self.show_page("cockpit")
        focused = self.cockpit.focus_source(
            source_type, source_session_id
        )
        self.seat_status.set(
            self._t(
                "chat.live_work_focused"
                if focused
                else "chat.live_work_unavailable"
            ).format(agent=agent_id)
        )
        self.seat_status_label.configure(
            fg=COLORS["green"] if focused else COLORS["amber"]
        )

    def _reset_seat_route_selection(self) -> None:
        self.seat_provider_choice.set("")
        self.seat_model_choice.set("")
        self.seat_reasoning_choice.set("")
        self._seat_reasoning_values = {}
        self._seat_selected_route_id = None
        self._seat_selected_candidate = None
        self.seat_model_combo.configure(values=(), state="disabled")
        self.seat_reasoning_combo.configure(values=(), state="disabled")

    def _on_seat_provider_selected(self, _event: Any = None) -> None:
        provider_id = self._seat_provider_ids.get(self.seat_provider_choice.get(), "")
        profiles = [
            row
            for row in self._seat_profiles
            if str(row.get("provider_id") or "") == provider_id
        ]
        model_values = {
            str(row.get("model_id") or self._t(PROVIDER_DEFAULT_MODEL_LABEL))
            for row in profiles
        }
        models = tuple(sorted(model_values))
        self.seat_model_combo.configure(values=models)
        self.seat_model_choice.set(models[0] if len(models) == 1 else "")
        self.seat_reasoning_choice.set("")
        self._seat_selected_route_id = None
        self.seat_reasoning_combo.configure(values=(), state="disabled")
        if len(models) == 1:
            self._on_seat_model_selected()
        elif models:
            self.seat_status.set(self._t("seat.select_model"))
            self.seat_status_label.configure(fg=COLORS["amber"])
        self._sync_room_control_states()

    def _on_seat_model_selected(self, _event: Any = None) -> None:
        provider_id = self._seat_provider_ids.get(self.seat_provider_choice.get(), "")
        selected_model = self.seat_model_choice.get()
        model_id = (
            None
            if self._catalog_value_matches(selected_model, PROVIDER_DEFAULT_MODEL_LABEL)
            else selected_model
        )
        profiles = [
            row
            for row in self._seat_profiles
            if str(row.get("provider_id") or "") == provider_id
            and (str(row.get("model_id")) if row.get("model_id") else None) == model_id
        ]
        reasoning_values: dict[str, str | None] = {}
        for row in profiles:
            value = str(row.get("reasoning_mode")) if row.get("reasoning_mode") else None
            label = value or self._t(PROVIDER_DEFAULT_REASONING_LABEL)
            reasoning_values[label] = value
        self._seat_reasoning_values = reasoning_values
        labels = tuple(sorted(reasoning_values))
        self.seat_reasoning_combo.configure(values=labels)
        self.seat_reasoning_choice.set(labels[0] if len(labels) == 1 else "")
        self._seat_selected_route_id = None
        if len(labels) == 1:
            self._on_seat_reasoning_selected()
        elif labels:
            self.seat_status.set(self._t("seat.select_reasoning"))
            self.seat_status_label.configure(fg=COLORS["amber"])
        self._sync_room_control_states()

    def _on_seat_reasoning_selected(self, _event: Any = None) -> None:
        provider_id = self._seat_provider_ids.get(self.seat_provider_choice.get(), "")
        selected_model = self.seat_model_choice.get()
        model_id = (
            None
            if self._catalog_value_matches(selected_model, PROVIDER_DEFAULT_MODEL_LABEL)
            else selected_model
        )
        reasoning_mode = self._seat_reasoning_values.get(self.seat_reasoning_choice.get())
        profile = exact_route_profile(
            self._seat_profiles,
            provider_id=provider_id,
            model_id=model_id,
            reasoning_mode=reasoning_mode,
        )
        advertised_only = bool(profile and profile.get("_advertised_only"))
        self._seat_selected_route_id = (
            str(profile.get("route_id") or "")
            if profile and not advertised_only
            else None
        )
        self._seat_selected_candidate = dict(profile) if advertised_only else None
        if advertised_only:
            self.seat_status.set(
                self._t("seat.advertised").format(model=model_id)
            )
            self.seat_status_label.configure(fg=COLORS["amber"])
        elif profile:
            self.seat_status.set(
                self._t("seat.configured").format(
                    agent=self.seat_agent.get(),
                    model=model_id or self._t(PROVIDER_DEFAULT_MODEL_LABEL),
                    reasoning=reasoning_mode
                    or self._t(PROVIDER_DEFAULT_REASONING_LABEL),
                )
            )
            self.seat_status_label.configure(fg=COLORS["green"])
        else:
            self.seat_status.set(self._t("seat.route_unique"))
            self.seat_status_label.configure(fg=COLORS["red"])
        self._sync_room_control_states()

    def _select_seat_profile(self, profile: dict[str, Any]) -> None:
        provider_id = str(profile.get("provider_id") or "")
        provider_label = next(
            (label for label, value in self._seat_provider_ids.items() if value == provider_id),
            "",
        )
        if not provider_label:
            return
        self.seat_provider_choice.set(provider_label)
        self._on_seat_provider_selected()
        self.seat_model_choice.set(
            str(profile.get("model_id") or self._t(PROVIDER_DEFAULT_MODEL_LABEL))
        )
        self._on_seat_model_selected()
        reasoning = (
            str(profile.get("reasoning_mode"))
            if profile.get("reasoning_mode")
            else self._t(PROVIDER_DEFAULT_REASONING_LABEL)
        )
        self.seat_reasoning_choice.set(reasoning)
        self._on_seat_reasoning_selected()

    def _select_library_agent(self, event: tk.Event[Any]) -> str | None:
        x = int(self.agent_canvas.canvasx(event.x))
        y = int(self.agent_canvas.canvasy(event.y))
        for left, top, right, bottom, agent_id in self._library_hitboxes:
            if left <= x <= right and top <= y <= bottom:
                self.seat_agent.set(agent_id)
                self._selected_room_seat_agent = None
                self._on_seat_agent_selected()
                return agent_id
        return None

    def _scroll_agent_library(self, event: tk.Event[Any]) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.agent_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def _begin_library_drag(self, event: tk.Event[Any]) -> None:
        self._drag_agent_id = self._select_library_agent(event)
        self._drag_action = "add" if self._drag_agent_id else None
        self._drag_origin = (event.x_root, event.y_root) if self._drag_agent_id else None

    def _show_drag_ghost(self, x_root: int, y_root: int) -> None:
        if not self._drag_agent_id:
            return
        if self._drag_ghost is None:
            ghost = tk.Toplevel(self.root)
            ghost.overrideredirect(True)
            try:
                ghost.attributes("-alpha", 0.90)
                ghost.attributes("-topmost", True)
            except tk.TclError:
                pass
            removing = self._drag_action == "remove"
            tk.Label(
                ghost,
                text=(
                    f"- {self._drag_agent_id}\nDROP ON GLOBAL LIBRARY"
                    if removing
                    else f"+ {self._drag_agent_id}\nDROP ON ROOM SEATS"
                ),
                bg=COLORS["panel_2"],
                fg=COLORS["red"] if removing else COLORS["cyan"],
                bd=2,
                relief="ridge",
                padx=10,
                pady=6,
                font=("Cascadia Mono", 8, "bold"),
            ).pack()
            self._drag_ghost = ghost
        self._drag_ghost.geometry(f"+{x_root + 12}+{y_root + 12}")

    def _move_library_drag(self, event: tk.Event[Any]) -> None:
        if self._drag_action != "add" or not self._drag_agent_id or not self._drag_origin:
            return
        if abs(event.x_root - self._drag_origin[0]) + abs(
            event.y_root - self._drag_origin[1]
        ) < 8:
            return
        self._show_drag_ghost(event.x_root, event.y_root)

    def _begin_room_agent_drag(self, event: tk.Event[Any], agent_id: str) -> None:
        if agent_id == HUMAN_AGENT_ID:
            return
        self._drag_agent_id = agent_id
        self._drag_action = "remove"
        self._drag_origin = (event.x_root, event.y_root)

    def _move_room_agent_drag(self, event: tk.Event[Any]) -> None:
        if self._drag_action != "remove" or not self._drag_agent_id or not self._drag_origin:
            return
        if abs(event.x_root - self._drag_origin[0]) + abs(
            event.y_root - self._drag_origin[1]
        ) < 8:
            return
        self.agent_canvas.configure(highlightbackground=COLORS["green"])
        self._show_drag_ghost(event.x_root, event.y_root)

    def _drop_hits_room_seats(self, x_root: int, y_root: int) -> bool:
        try:
            left = self.room_seats_frame.winfo_rootx()
            top = self.room_seats_frame.winfo_rooty()
            return point_in_rectangle(
                x_root,
                y_root,
                left=left,
                top=top,
                width=self.room_seats_frame.winfo_width(),
                height=self.room_seats_frame.winfo_height(),
            )
        except tk.TclError:
            return False

    def _drop_hits_global_library(self, x_root: int, y_root: int) -> bool:
        try:
            # The user's target is the whole left library rail, not only the
            # small icon canvas.  Restricting this to 200x178 px made valid
            # leftward drags appear to do nothing.
            target = self.sidebar_frame
            left = target.winfo_rootx()
            top = target.winfo_rooty()
            return point_in_rectangle(
                x_root,
                y_root,
                left=left,
                top=top,
                width=target.winfo_width(),
                height=target.winfo_height(),
            )
        except tk.TclError:
            return False

    def _clear_library_drag(self) -> None:
        if self._drag_ghost is not None:
            self._drag_ghost.destroy()
        self._drag_ghost = None
        self._drag_agent_id = None
        self._drag_action = None
        self._drag_origin = None
        self.agent_canvas.configure(highlightbackground=COLORS["line"])

    def _finish_library_drag(self, event: tk.Event[Any]) -> None:
        dragged = self._drag_action == "add" and self._drag_ghost is not None
        should_add = dragged and self._drop_hits_room_seats(event.x_root, event.y_root)
        agent_id = self._drag_agent_id
        self._clear_library_drag()
        if should_add:
            if self.selected_room_id == DEFAULT_ROOM_ID:
                if agent_id:
                    self._restore_lobby_agent(agent_id)
            elif self._seat_selected_route_id or self._seat_selected_candidate:
                self.add_room_seat()
            elif agent_id:
                self._show_agent_route_menu(
                    agent_id,
                    x_root=event.x_root,
                    y_root=event.y_root,
                )
        elif dragged:
            self.seat_status.set(self._t("seat.drop_agent_cancelled"))
            self.seat_status_label.configure(fg=COLORS["muted"])

    def _finish_room_agent_drag(self, event: tk.Event[Any]) -> None:
        agent_id = self._drag_agent_id
        dragged = self._drag_action == "remove" and self._drag_ghost is not None
        should_remove = dragged and self._drop_hits_global_library(
            event.x_root, event.y_root
        )
        self._clear_library_drag()
        if should_remove and agent_id:
            self._remove_room_agent(agent_id)
        elif dragged:
            self.seat_status.set(self._t("seat.drop_seat_cancelled"))
            self.seat_status_label.configure(fg=COLORS["muted"])
        elif agent_id:
            self._select_room_agent_card(agent_id)

    def _add_library_agent_by_double_click(self, event: tk.Event[Any]) -> None:
        agent_id = self._select_library_agent(event)
        if not agent_id:
            return
        if self._seat_selected_route_id or self._seat_selected_candidate:
            self.add_room_seat()
        else:
            self._show_agent_route_menu(
                agent_id,
                x_root=event.x_root,
                y_root=event.y_root,
            )

    def _show_agent_route_menu(
        self,
        agent_id: str,
        *,
        widget: tk.Misc | None = None,
        x_root: int | None = None,
        y_root: int | None = None,
    ) -> None:
        if agent_id == HUMAN_AGENT_ID:
            return
        self.seat_agent.set(agent_id)
        self._selected_room_seat_agent = agent_id
        self._on_seat_agent_selected()
        profiles = list(self._seat_profiles)
        menu = tk.Menu(
            self.root,
            tearoff=False,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            activebackground=COLORS["cyan"],
            activeforeground=COLORS["black"],
            font=("Cascadia Mono", 8),
        )
        member = next(
            (
                row
                for row in self._room_members
                if row.get("status") == "active" and row.get("agent_id") == agent_id
            ),
            None,
        )
        current_route = str((member or {}).get("route_profile_id") or "")
        if current_route:
            menu.add_command(
                label=self._t("seat.menu_current").format(
                    route=room_seat_route(member or {})
                ),
                state="disabled",
            )
            menu.add_separator()

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for profile in profiles:
            provider = self._profile_provider_label(profile)
            model = str(
                profile.get("model_id") or self._t(PROVIDER_DEFAULT_MODEL_LABEL)
            )
            grouped.setdefault(provider, {}).setdefault(model, []).append(profile)
        model_count = sum(len(models) for models in grouped.values())
        if grouped:
            menu.add_command(
                label=self._t("seat.menu_options").format(
                    models=model_count, providers=len(grouped)
                ),
                state="disabled",
            )
            menu.add_separator()
        for provider, models in sorted(grouped.items()):
            provider_menu = tk.Menu(
                menu,
                tearoff=False,
                bg=COLORS["panel_2"],
                fg=COLORS["text"],
                activebackground=COLORS["cyan"],
                activeforeground=COLORS["black"],
                font=("Cascadia Mono", 8),
            )
            for model, routes in sorted(models.items()):
                model_menu = tk.Menu(
                    provider_menu,
                    tearoff=False,
                    bg=COLORS["panel_2"],
                    fg=COLORS["text"],
                    activebackground=COLORS["cyan"],
                    activeforeground=COLORS["black"],
                    font=("Cascadia Mono", 8),
                )
                for profile in sorted(
                    routes,
                    key=lambda row: (
                        str(row.get("reasoning_mode") or ""),
                        str(row.get("route_id") or ""),
                    ),
                ):
                    reasoning = str(
                        profile.get("reasoning_mode")
                        or self._t(PROVIDER_DEFAULT_REASONING_LABEL)
                    )
                    prefix = "✓ " if str(profile.get("route_id") or "") == current_route else ""
                    suffix = (
                        self._t("seat.menu_advertised")
                        if profile.get("_advertised_only")
                        else ""
                    )
                    model_menu.add_command(
                        label=f"{prefix}{reasoning}{suffix}",
                        command=lambda selected=dict(profile): self._apply_room_agent_profile(
                            agent_id, selected
                        ),
                    )
                provider_menu.add_cascade(label=model, menu=model_menu)
            menu.add_cascade(
                label=f"{provider} ({len(models)} models)",
                menu=provider_menu,
            )
        if not profiles:
            menu.add_command(label=self._t("seat.menu_no_route"), state="disabled")
        refreshable = agent_id == "codex-main" or any(
            provider_id in self._provider_model_catalog
            or provider_id in self._provider_discovery_inflight
            or provider_id in self._provider_discovery_errors
            for provider_id in self._agent_provider_ids(agent_id)
        )
        if refreshable:
            menu.add_separator()
            menu.add_command(
                label=self._t("seat.menu_refresh"),
                command=lambda: self._refresh_agent_provider_models(agent_id),
            )
        if member:
            menu.add_separator()
            menu.add_command(
                label=self._t("seat.menu_remove"),
                command=lambda: self._remove_room_agent(agent_id),
            )
        if x_root is None or y_root is None:
            anchor = widget or self.room_agent_strip
            x_root = anchor.winfo_rootx() + max(0, anchor.winfo_width() - 20)
            y_root = anchor.winfo_rooty() + anchor.winfo_height()
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def _apply_room_agent_profile(
        self, agent_id: str, profile: dict[str, Any]
    ) -> None:
        self.seat_agent.set(agent_id)
        self._selected_room_seat_agent = agent_id
        self._on_seat_agent_selected()
        self._select_seat_profile(profile)
        self._seat_selected_route_id = str(profile.get("route_id") or "") or None
        self.add_room_seat()

    def add_room_seat(self) -> None:
        room_id = self.selected_room_id
        agent_id = self.seat_agent.get().strip()
        role_id, role_label = self._selected_room_role()
        library_ids = {str(row["agent_id"]) for row in self._library_agents}
        if agent_id not in library_ids:
            self.seat_status.set(self._t("seat.select_agent"))
            self.seat_status_label.configure(fg=COLORS["red"])
            return
        if self._seat_selected_candidate:
            self._register_discovered_room_agent_profile(
                agent_id,
                dict(self._seat_selected_candidate),
            )
            return
        route_id = self._seat_selected_route_id
        if not route_id:
            self.seat_status.set(self._t("seat.incomplete"))
            self.seat_status_label.configure(fg=COLORS["red"])
            return
        updating = any(
            row.get("status") == "active" and row.get("agent_id") == agent_id
            for row in self._room_members
        )
        self._run_room_action(
            lambda: self.human_client.join_room(
                room_id=room_id,
                agent_id=agent_id,
                route_profile_id=route_id,
                role_id=role_id,
                role_label=role_label,
            ),
            pending=f"{'UPDATING' if updating else 'ADDING'} {agent_id} IN {room_id}...",
            target=self.seat_status,
            target_label=self.seat_status_label,
            success=lambda receipt: (
                f"SEAT {'UPDATED' if updating else 'READY'} // "
                f"SESSION {receipt['room_session_id']} // LIBRARY RETAINED"
            ),
            after_success=lambda _receipt: setattr(
                self, "_selected_room_seat_agent", agent_id
            ),
        )

    def _register_discovered_room_agent_profile(
        self,
        agent_id: str,
        profile: dict[str, Any],
    ) -> None:
        if profile.get("_catalog_source") == "codex-cli":
            self._register_discovered_codex_profile(agent_id, profile)
            return
        provider_id = str(profile.get("provider_id") or "")
        model_id = str(profile.get("model_id") or "")
        reasoning_mode = (
            str(profile.get("reasoning_mode"))
            if profile.get("reasoning_mode")
            else None
        )
        route_class = str(profile.get("route_class") or "")
        client_name = str(profile.get("client_name") or "openai-compatible")
        if not all((provider_id, model_id, route_class)):
            self.seat_status.set(self._t("seat.missing_provider_identity"))
            self.seat_status_label.configure(fg=COLORS["red"])
            return
        route_id = discovered_route_profile_id(
            scope=self.scope,
            agent_id=agent_id,
            provider_id=provider_id,
            model_id=model_id,
            reasoning_mode=reasoning_mode,
        )
        room_id = self.selected_room_id
        role_id, role_label = self._selected_room_role()

        def operation() -> dict[str, Any]:
            connections = self.human_client.call_tool(
                "list_provider_connections",
                {"enabled_only": True},
            ).get("connections", [])
            matches = [
                row
                for row in connections
                if isinstance(row, dict)
                and row.get("connection_id") == provider_id
                and row.get("provider_id") == provider_id
                and row.get("route_class") == route_class
                and row.get("secret_backend") == "windows-credential-manager"
            ]
            if len(matches) != 1:
                raise ValueError(self._t("seat.no_revalidatable_connection"))
            discovery = discover_provider_models(
                scope=self.scope,
                connection_id=provider_id,
                route_class=route_class,
                provider_id=provider_id,
            )
            if model_id not in discovery.models:
                raise ValueError(self._t("seat.provider_model_removed"))
            payload: dict[str, Any] = {
                "route_id": route_id,
                "agent_id": agent_id,
                "client_name": client_name,
                "provider_id": provider_id,
                "model_id": model_id,
                "route_class": route_class,
                "enabled": True,
            }
            if reasoning_mode:
                payload["reasoning_mode"] = reasoning_mode
            route = self.human_client.call_tool("upsert_route_profile", payload)
            joined = self.human_client.join_room(
                room_id=room_id,
                agent_id=agent_id,
                route_profile_id=str(route["route_id"]),
                role_id=role_id,
                role_label=role_label,
            )
            return {
                **joined,
                "_route_id": str(route["route_id"]),
                "_provider_id": provider_id,
                "_registry_models": tuple(discovery.models),
                "_registry_sha256": discovery.registry_sha256,
            }

        def accepted(receipt: dict[str, Any]) -> None:
            self._selected_room_seat_agent = agent_id
            self._seat_selected_route_id = str(receipt["_route_id"])
            self._seat_selected_candidate = None
            self._provider_model_catalog[provider_id] = tuple(
                receipt["_registry_models"]
            )
            self._provider_model_registry_sha256[provider_id] = str(
                receipt["_registry_sha256"]
            )

        self._run_room_action(
            operation,
            pending=f"VERIFYING {provider_id}/{model_id} AND APPLYING SEAT...",
            target=self.seat_status,
            target_label=self.seat_status_label,
            success=lambda receipt: (
                f"SEAT READY // {agent_id} → {model_id} // "
                f"ROUTE {receipt['_route_id']} // API REVERIFIED"
            ),
            after_success=accepted,
        )

    def _register_discovered_codex_profile(
        self,
        agent_id: str,
        profile: dict[str, Any],
    ) -> None:
        provider_id = str(profile.get("provider_id") or "")
        model_id = str(profile.get("model_id") or "")
        reasoning_mode = str(profile.get("reasoning_mode") or "") or None
        if (
            agent_id != "codex-main"
            or provider_id != "openai-official"
            or profile.get("route_class") != "official"
            or not model_id
        ):
            self.seat_status.set(self._t("seat.codex_identity_incomplete"))
            self.seat_status_label.configure(fg=COLORS["red"])
            return
        route_id = discovered_route_profile_id(
            scope=self.scope,
            agent_id=agent_id,
            provider_id=provider_id,
            model_id=model_id,
            reasoning_mode=reasoning_mode,
        )
        room_id = self.selected_room_id
        role_id, role_label = self._selected_room_role()

        def operation() -> dict[str, Any]:
            catalog = discover_codex_model_catalog()
            matches = [item for item in catalog.models if item.model_id == model_id]
            if len(matches) != 1:
                raise CodexCatalogError(self._t("seat.codex_model_removed"))
            model = matches[0]
            if reasoning_mode and reasoning_mode not in model.supported_reasoning_modes:
                raise CodexCatalogError(self._t("seat.codex_reasoning_removed"))
            route = self.human_client.call_tool(
                "upsert_route_profile",
                {
                    "route_id": route_id,
                    "agent_id": agent_id,
                    "client_name": "codex",
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "reasoning_mode": reasoning_mode,
                    "route_class": "official",
                    "enabled": True,
                },
            )
            joined = self.human_client.join_room(
                room_id=room_id,
                agent_id=agent_id,
                route_profile_id=str(route["route_id"]),
                role_id=role_id,
                role_label=role_label,
            )
            return {
                **joined,
                "_route_id": str(route["route_id"]),
                "_catalog": catalog,
            }

        def accepted(receipt: dict[str, Any]) -> None:
            self._selected_room_seat_agent = agent_id
            self._seat_selected_route_id = str(receipt["_route_id"])
            self._seat_selected_candidate = None
            self._codex_model_catalog = receipt["_catalog"]

        self._run_room_action(
            operation,
            pending=f"VERIFYING OFFICIAL CODEX {model_id} / {reasoning_mode or 'default'}...",
            target=self.seat_status,
            target_label=self.seat_status_label,
            success=lambda receipt: (
                f"SEAT READY // {agent_id} → {model_id} / "
                f"{reasoning_mode or 'provider-default'} // SESSION {receipt['room_session_id']}"
            ),
            after_success=accepted,
        )

    def remove_room_seat(self) -> None:
        selected = self.room_seat_tree.selection()
        member = (
            getattr(self, "_seat_records", {}).get(selected[0])
            if selected
            else next(
                (
                    row
                    for row in self._room_members
                    if row.get("status") == "active"
                    and row.get("agent_id") == self._selected_room_seat_agent
                ),
                None,
            )
        )
        if not member:
            self.seat_status.set(self._t("seat.select_remove"))
            self.seat_status_label.configure(fg=COLORS["red"])
            return
        agent_id = str(member.get("agent_id") or "")
        self._remove_room_agent(agent_id)

    def _remove_room_agent(self, agent_id: str) -> None:
        if agent_id == HUMAN_AGENT_ID:
            self.seat_status.set(self._t("seat.keep_control"))
            self.seat_status_label.configure(fg=COLORS["red"])
            return
        room_id = self.selected_room_id
        if not messagebox.askyesno(
            "Remove Room Seat",
            self._t("seat.confirm_remove").format(agent=agent_id, room=room_id),
        ):
            return
        self._run_room_action(
            lambda: self.human_client.leave_room(room_id=room_id, agent_id=agent_id),
            pending=f"REMOVING {agent_id} FROM {room_id}...",
            target=self.seat_status,
            target_label=self.seat_status_label,
            success=lambda receipt: (
                f"SEAT REMOVED // SESSION {receipt['room_session_id']} // HISTORY PRESERVED"
            ),
            after_success=lambda _receipt: setattr(
                self, "_selected_room_seat_agent", None
            ),
        )

    def _restore_lobby_agent(self, agent_id: str) -> None:
        """Clear a durable Lobby opt-out without changing global registration."""

        self._run_room_action(
            lambda: self.human_client.join_room(
                room_id=DEFAULT_ROOM_ID,
                agent_id=agent_id,
            ),
            pending=f"RESTORING {agent_id} TO LOBBY...",
            target=self.seat_status,
            target_label=self.seat_status_label,
            success=lambda receipt: (
                f"LOBBY AGENT RESTORED // SESSION {receipt['room_session_id']}"
            ),
            after_success=lambda _receipt: setattr(
                self, "_selected_room_seat_agent", agent_id
            ),
        )

    def _render_room_seats(self) -> None:
        self._render_room_agent_cards()
        member_projection = tuple(
            {
                key: row.get(key)
                for key in (
                    "agent_id",
                    "status",
                    "room_session_id",
                    "role_id",
                    "role_label",
                    "route_profile_id",
                    "provider_id",
                    "model_id",
                    "reasoning_mode",
                    "online",
                )
            }
            for row in self._room_members
        )
        library_projection = tuple(
            {
                key: row.get(key)
                for key in ("agent_id", "online", "provider_id", "model_id")
            }
            for row in self._library_agents
        )
        seats_signature = ui_content_signature(
            {
                "room_id": self.selected_room_id,
                "locale": self.locale.get(),
                "members": member_projection,
                "library_agents": library_projection,
            }
        )
        if seats_signature == self._last_room_seats_signature:
            return
        self._last_room_seats_signature = seats_signature
        self.room_seat_tree.delete(*self.room_seat_tree.get_children())
        self._seat_records: dict[str, dict[str, Any]] = {}
        if self.selected_room_id == DEFAULT_ROOM_ID:
            memberships = {
                str(row.get("agent_id")): dict(row)
                for row in self._room_members
                if row.get("agent_id")
            }
            excluded = {
                str(row.get("agent_id") or "")
                for row in self._room_members
                if row.get("status") == "left" and row.get("agent_id")
            }
            self.room_seat_tree.insert(
                "",
                "end",
                iid="lobby-implicit",
                values=(
                    HUMAN_AGENT_ID,
                    self._t(f"chat.role.{DEFAULT_ROOM_ROLE}"),
                    self._t("chat.session.implicit"),
                    self._t("chat.route.direct_global"),
                    self._t("chat.state.active"),
                ),
            )
            for agent in self._library_agents:
                agent_id = str(agent.get("agent_id") or "")
                if not agent_id or agent_id in excluded:
                    continue
                row_id = f"lobby-{agent_id}"
                member = memberships.get(agent_id, {})
                active_route = (
                    member if member.get("status") == "active" else {}
                )
                runtime = (
                    room_seat_route(active_route)
                    if active_route
                    else self._t("chat.route.none")
                )
                self.room_seat_tree.insert(
                    "",
                    "end",
                    iid=row_id,
                    values=(
                        agent_id,
                        self._room_role_label(active_route),
                        active_route.get("room_session_id")
                        or self._t("chat.session.global"),
                        runtime,
                        (
                            self._t("chat.state.online")
                            if active_route.get("route_profile_id") and agent.get("online")
                            else (
                                self._t("chat.state.offline")
                                if active_route.get("route_profile_id")
                                else self._t("chat.state.unrouted")
                            )
                        ),
                    ),
                )
                self._seat_records[row_id] = {**agent, **active_route}
            self.seat_status.set(self._t("seat.lobby_bound_only"))
            self.seat_status_label.configure(fg=COLORS["muted"])
            return
        for member in self._room_members:
            if member.get("status") != "active":
                continue
            session_id = str(member.get("room_session_id") or "")
            row_id = session_id or hashlib.sha256(repr(member).encode("utf-8")).hexdigest()
            self.room_seat_tree.insert(
                "",
                "end",
                iid=row_id,
                values=(
                    member.get("agent_id") or "--",
                    self._room_role_label(member),
                    session_id or "--",
                    room_seat_route(member),
                    self._t(
                        "chat.state.online"
                        if member.get("online")
                        else "chat.state.offline"
                    ),
                ),
            )
            self._seat_records[row_id] = member
        if not self._room_members:
            self.seat_status.set(self._t("chat.no_active_seats"))
            self.seat_status_label.configure(fg=COLORS["muted"])

    def _render_room_agent_cards(self) -> None:
        cards = room_agent_cards(
            self.selected_room_id,
            self._room_members,
            self._library_agents,
        )
        visible_limit = room_agent_visible_limit(
            self.room_agent_strip.winfo_width(), len(cards)
        )
        self._room_agent_card_capacity = visible_limit
        card_projection = tuple(
            {
                key: card.get(key)
                for key in (
                    "agent_id",
                    "provider_id",
                    "model_id",
                    "reasoning_mode",
                    "route_profile_id",
                    "role_id",
                    "role_label",
                    "online",
                    "state",
                    "mcp_access_mode",
                )
            }
            for card in cards
        )
        cards_signature = ui_content_signature(
            {
                "room_id": self.selected_room_id,
                "selected_agent": self._selected_room_seat_agent,
                "locale": self.locale.get(),
                "visible_limit": visible_limit,
                "cards": card_projection,
            }
        )
        if cards_signature == self._last_room_agent_cards_signature:
            self._render_modern_agent_inspector()
            return
        self._last_room_agent_cards_signature = cards_signature
        for child in self.room_agent_strip.winfo_children():
            child.destroy()
        tk.Label(
            self.room_agent_strip,
            text=self._t("chat.room_agents").format(count=len(cards)),
            bg=COLORS["black"],
            fg=COLORS["amber"],
            font=("Cascadia Mono", 8, "bold"),
        ).pack(side="left", padx=(8, 10))
        if not cards:
            tk.Label(
                self.room_agent_strip,
                text=self._t("chat.no_agents"),
                bg=COLORS["black"],
                fg=COLORS["red"],
                font=("Cascadia Mono", 9, "bold"),
            ).pack(side="left", padx=8)
            self._render_modern_agent_inspector()
            return
        visible, overflow = room_agent_card_groups(
            cards, visible_limit=visible_limit
        )
        for card in visible:
            agent_id = str(card.get("agent_id") or "unknown")
            state = str(card.get("state") or "OFFLINE")
            online = state in {"ONLINE", "CONTROL"}
            selected = self._selected_room_seat_agent == agent_id
            frame = tk.Frame(
                self.room_agent_strip,
                bg=COLORS["panel_2"],
                bd=2,
                relief="ridge",
                highlightthickness=2 if selected else 0,
                highlightbackground=COLORS["amber"],
                width=ROOM_AGENT_CARD_WIDTH,
                height=80,
                cursor="hand2" if agent_id != HUMAN_AGENT_ID else "arrow",
            )
            frame.pack(side="left", padx=ROOM_AGENT_CARD_PAD_X, pady=6)
            frame.pack_propagate(False)
            title = tk.Label(
                frame,
                text=clip(agent_id, 14),
                bg=COLORS["panel_2"],
                fg=(
                    COLORS["amber"]
                    if selected
                    else COLORS["cyan"] if online else COLORS["muted"]
                ),
                anchor="w",
                font=("Cascadia Mono", 8, "bold"),
            )
            title.pack(fill="x", padx=6, pady=(4, 0))
            runtime = "/".join(
                str(value)
                for value in (card.get("provider_id"), card.get("model_id"))
                if value
            ) or self._t("chat.route.none")
            access = mcp_access_label(
                str(card.get("mcp_access_mode") or MCP_UNVERIFIED)
            )
            role = self._room_role_label(card)
            detail = tk.Label(
                frame,
                text=(
                    f"{clip(runtime, 19)}\n"
                    f"{clip(role, 19)}\n"
                    f"{access}  {self._room_state_text(state)}"
                ),
                bg=COLORS["panel_2"],
                fg=COLORS["green"] if online else COLORS["red"],
                anchor="w",
                justify="left",
                font=("Cascadia Mono", ROOM_AGENT_DETAIL_TEXT_SIZE),
            )
            detail.pack(fill="x", padx=6, pady=(1, 4))
            if agent_id != HUMAN_AGENT_ID:
                for widget in (frame, title, detail):
                    widget.bind(
                        "<ButtonPress-1>",
                        lambda event, value=agent_id: self._begin_room_agent_drag(
                            event, value
                        ),
                    )
                    widget.bind(
                        "<B1-Motion>",
                        self._move_room_agent_drag,
                    )
                    widget.bind(
                        "<ButtonRelease-1>",
                        self._finish_room_agent_drag,
                    )
                route_button = tk.Button(
                    frame,
                    text="▼",
                    bg=COLORS["line"],
                    fg=COLORS["amber"],
                    activebackground=COLORS["cyan"],
                    activeforeground=COLORS["black"],
                    relief="raised",
                    bd=1,
                    padx=3,
                    pady=0,
                    cursor="hand2",
                    font=("Cascadia Mono", 7, "bold"),
                )
                route_button.configure(
                    command=lambda value=agent_id, button=route_button: self._show_agent_route_menu(
                        value, widget=button
                    )
                )
                route_button.place(relx=1.0, x=-5, y=4, anchor="ne")
        if overflow:
            overflow_button = tk.Menubutton(
                self.room_agent_strip,
                text=f"+{len(overflow)} ▼",
                bg=COLORS["black"],
                fg=COLORS["amber"],
                activebackground=COLORS["cyan"],
                activeforeground=COLORS["black"],
                relief="raised",
                cursor="hand2",
                font=("Cascadia Mono", 9, "bold"),
            )
            overflow_menu = tk.Menu(overflow_button, tearoff=False)
            for card in overflow:
                agent_id = str(card.get("agent_id") or "unknown")
                state = str(card.get("state") or "OFFLINE")
                overflow_menu.add_command(
                    label=(
                        f"{clip(agent_id, 28)} // "
                        f"{self._room_state_text(state)}"
                    ),
                    command=lambda value=agent_id: self._select_room_agent_card(value),
                )
            overflow_button.configure(menu=overflow_menu)
            overflow_button.pack(side="left", padx=5)
        self._render_modern_agent_inspector()

    def _room_agent_strip_resized(self, event: Any) -> None:
        cards = room_agent_cards(
            self.selected_room_id,
            self._room_members,
            self._library_agents,
        )
        capacity = room_agent_visible_limit(
            int(getattr(event, "width", 0) or 0), len(cards)
        )
        if capacity == self._room_agent_card_capacity:
            return
        self._room_agent_card_capacity = capacity
        self._last_room_agent_cards_signature = ""
        if not self._closing:
            self.root.after_idle(self._render_room_agent_cards)

    def _select_room_agent_card(self, agent_id: str) -> None:
        self._selected_room_seat_agent = agent_id
        self.seat_agent.set(agent_id)
        self._on_seat_agent_selected()
        for item_id, member in getattr(self, "_seat_records", {}).items():
            if str(member.get("agent_id") or "") == agent_id:
                self.room_seat_tree.selection_set(item_id)
                self.room_seat_tree.focus(item_id)
                break
        if agent_id in self._recipient_ids:
            self.message_recipient.set(agent_id)
            self._on_recipient_selected()
        self._render_room_agent_cards()

    def _select_room_seat_for_edit(self, _event: Any = None) -> None:
        selected = self.room_seat_tree.selection()
        if not selected:
            return
        member = getattr(self, "_seat_records", {}).get(selected[0], {})
        agent_id = str(member.get("agent_id") or "")
        if not agent_id or agent_id == HUMAN_AGENT_ID:
            return
        self._selected_room_seat_agent = agent_id
        self.seat_agent.set(agent_id)
        self._on_seat_agent_selected()
        if agent_id in self._recipient_ids:
            self.message_recipient.set(agent_id)
            self._on_recipient_selected()
        self._render_room_agent_cards()

    def _select_seat_recipient(self, _event: Any = None) -> None:
        self._select_room_seat_for_edit(_event)

    def _field(
        self,
        parent: tk.Misc,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        *,
        secret: bool = False,
        width: int = 24,
        localization_key: str | None = None,
    ) -> tk.Entry:
        label_widget = (
            self._localized_label(
                parent,
                localization_key,
                bg=COLORS["panel"],
                fg=COLORS["amber"],
                anchor="w",
                font=("Cascadia Mono", 8, "bold"),
            )
            if localization_key
            else tk.Label(
                parent,
                text=label,
                bg=COLORS["panel"],
                fg=COLORS["amber"],
                anchor="w",
                font=("Cascadia Mono", 8, "bold"),
            )
        )
        label_widget.grid(
            row=row, column=column, sticky="w", padx=8, pady=(7, 2)
        )
        entry = tk.Entry(
            parent,
            textvariable=variable,
            show="*" if secret else "",
            width=width,
            bg=COLORS["black"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
        )
        entry.grid(row=row + 1, column=column, sticky="ew", padx=8, pady=(0, 5), ipady=4)
        return entry

    def _apply_connection_key_visibility(self) -> None:
        visible = bool(self.connection_key_visible.get())
        self.connection_key_entry.configure(show="" if visible else "*")
        self.connection_key_visibility_button.configure(
            text=self._t(
                "provider.hide_api_key" if visible else "provider.show_api_key"
            )
        )

    def _toggle_connection_key_visibility(self) -> None:
        self.connection_key_visible.set(not self.connection_key_visible.get())
        self._apply_connection_key_visibility()

    def _build_connections_page(self, host: tk.Frame) -> None:
        page = tk.Frame(host, bg=COLORS["panel"], bd=2, relief="ridge")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=1)
        canvas = tk.Canvas(page, bg=COLORS["panel"], highlightthickness=0)
        scroll = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        inner = tk.Frame(canvas, bg=COLORS["panel"])
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        heading = tk.Frame(inner, bg=COLORS["black"], bd=2, relief="ridge")
        heading.pack(fill="x", padx=10, pady=10)
        self.connect_heading_label = tk.Label(
            heading,
            text=self._t("connect.heading"),
            bg=COLORS["black"], fg=COLORS["cyan"],
            font=("Cascadia Mono", 12, "bold"),
        )
        self.connect_heading_label.pack(anchor="w", padx=12, pady=(10, 2))
        self.connect_privacy_label = tk.Label(
            heading,
            text=self._t("connect.privacy"),
            bg=COLORS["black"], fg=COLORS["muted"], justify="left",
            font=("Cascadia Mono", 9),
        )
        self.connect_privacy_label.pack(anchor="w", padx=12, pady=(2, 10))

        self.agent_install_frame = tk.LabelFrame(
            inner,
            text=self._t("agent_install.heading"),
            bg=COLORS["panel"],
            fg=COLORS["amber"],
            bd=2,
            relief="ridge",
            font=("Cascadia Mono", 10, "bold"),
        )
        self.agent_install_frame.pack(fill="x", padx=10, pady=6)
        self.agent_install_frame.grid_columnconfigure(1, weight=1)
        self.agent_install_intro = tk.Label(
            self.agent_install_frame,
            text=self._t("agent_install.intro"),
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=980,
            font=("Cascadia Mono", 8),
        )
        self.agent_install_intro.grid(
            row=0, column=0, columnspan=4, sticky="ew", padx=8, pady=(8, 6)
        )
        for row, spec in enumerate(installable_agent_specs(), start=1):
            tk.Label(
                self.agent_install_frame,
                text=f"{spec.display_name} // {spec.publisher}",
                bg=COLORS["panel"],
                fg=COLORS["text"],
                anchor="w",
                font=("Cascadia Mono", 9, "bold"),
            ).grid(row=row, column=0, sticky="w", padx=8, pady=6)
            note = tk.Label(
                self.agent_install_frame,
                text=self._t(spec.note_key),
                textvariable=self.agent_install_status[spec.agent_id],
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                anchor="w",
                justify="left",
                wraplength=500,
                font=("Cascadia Mono", 8),
            )
            note.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
            action = tk.Button(
                self.agent_install_frame,
                text=self._t(
                    "agent_install.install"
                    if spec.automatic_install_supported
                    else "agent_install.open_guide"
                ).format(name=spec.display_name),
                command=lambda agent_id=spec.agent_id: self.install_official_agent(
                    agent_id
                ),
                bg=COLORS["green"]
                if spec.automatic_install_supported
                else COLORS["purple"],
                fg=COLORS["black"],
                activebackground=COLORS["cyan"],
                bd=2,
                font=("Cascadia Mono", 8, "bold"),
            )
            action.grid(row=row, column=2, sticky="ew", padx=4, pady=5)
            self._agent_install_buttons[spec.agent_id] = action
            docs = tk.Button(
                self.agent_install_frame,
                text=self._t("agent_install.docs").format(name=spec.display_name),
                command=lambda url=spec.docs_url: webbrowser.open(url),
                bg=COLORS["blue"],
                fg=COLORS["black"],
                activebackground=COLORS["cyan"],
                bd=2,
                font=("Cascadia Mono", 8, "bold"),
            )
            docs.grid(row=row, column=3, sticky="ew", padx=(4, 8), pady=5)
            self._agent_install_docs_buttons[spec.agent_id] = docs
        self.agent_install_detect_button = tk.Button(
            self.agent_install_frame,
            text=self._t("agent_install.detect_all"),
            command=self.refresh_official_agent_statuses,
            bg=COLORS["cyan"],
            fg=COLORS["black"],
            activebackground=COLORS["green"],
            bd=2,
            font=("Cascadia Mono", 8, "bold"),
        )
        self.agent_install_detect_button.grid(
            row=len(installable_agent_specs()) + 1,
            column=0,
            columnspan=4,
            sticky="e",
            padx=8,
            pady=(4, 9),
        )

        native = self.native_connection_frame = tk.LabelFrame(
            inner,
            text=self._t("connect.native_heading"),
            bg=COLORS["panel"], fg=COLORS["amber"],
            bd=2, relief="ridge", font=("Cascadia Mono", 10, "bold"),
        )
        native.pack(fill="x", padx=10, pady=6)
        for column in range(4):
            native.grid_columnconfigure(column, weight=1)
        self._field(
            native, "", self.connection_id, 0, 0,
            localization_key="connect.field.connection_id",
        )
        self._field(
            native, "", self.connection_name, 0, 1,
            localization_key="connect.field.display_name",
        )
        self._localized_label(
            native,
            "connect.field.class",
            bg=COLORS["panel"], fg=COLORS["amber"], anchor="w",
            font=("Cascadia Mono", 8, "bold"),
        ).grid(row=0, column=2, sticky="w", padx=8, pady=(7, 2))
        ttk.Combobox(native, textvariable=self.connection_class, values=("official", "relay", "local"), state="readonly").grid(row=1, column=2, sticky="ew", padx=8, pady=(0, 5), ipady=4)
        self._field(
            native, "", self.connection_agent, 0, 3,
            localization_key="connect.field.agent_id",
        )
        self._field(
            native, "", self.connection_endpoint, 2, 0,
            localization_key="connect.field.endpoint",
        )
        self._localized_label(
            native,
            "connect.field.api_key",
            bg=COLORS["panel"],
            fg=COLORS["amber"],
            anchor="w",
            font=("Cascadia Mono", 8, "bold"),
        ).grid(row=2, column=1, sticky="w", padx=8, pady=(7, 2))
        connection_key_row = tk.Frame(native, bg=COLORS["panel"])
        connection_key_row.grid(
            row=3, column=1, sticky="ew", padx=8, pady=(0, 5)
        )
        self.connection_key_entry = tk.Entry(
            connection_key_row,
            textvariable=self.connection_api_key,
            show="*",
            bg=COLORS["black"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
        )
        self.connection_key_entry.pack(
            side="left", fill="x", expand=True, ipady=4
        )
        self.connection_key_visibility_button = tk.Button(
            connection_key_row,
            text=self._t("provider.show_api_key"),
            command=self._toggle_connection_key_visibility,
            bg=COLORS["blue"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            bd=2,
            font=("Cascadia Mono", 8, "bold"),
            takefocus=True,
        )
        self.connection_key_visibility_button.pack(side="right", padx=(5, 0))
        self._field(
            native, "", self.connection_client, 2, 2,
            localization_key="connect.field.client_name",
        )
        self._field(
            native, "", self.connection_route_id, 2, 3,
            localization_key="connect.field.route_id",
        )
        self._localized_label(
            native,
            "connect.field.model",
            bg=COLORS["panel"], fg=COLORS["amber"], anchor="w",
            font=("Cascadia Mono", 8, "bold"),
        ).grid(row=4, column=0, sticky="w", padx=8, pady=(7, 2))
        self.connection_model_combo = ttk.Combobox(native, textvariable=self.connection_model, values=(), state="normal")
        self.connection_model_combo.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 5), ipady=4)
        self._field(
            native, "", self.connection_response_model, 4, 1,
            localization_key="connect.field.response_model",
        )
        self._field(
            native, "", self.connection_reasoning, 4, 2,
            localization_key="connect.field.reasoning",
        )
        self._field(
            native,
            "",
            self.connection_timeout_seconds,
            4,
            3,
            localization_key="connect.timeout_label",
        )
        native_buttons = tk.Frame(native, bg=COLORS["panel"])
        native_buttons.grid(row=7, column=0, columnspan=4, sticky="e", padx=8, pady=8)
        self.native_connection_buttons: dict[str, tk.Button] = {}
        for key, command, color in (
            ("connect.save_discover", self.save_native_connection, COLORS["green"]),
            ("connect.discover", self.discover_native_models, COLORS["purple"]),
            ("connect.register_route", self.register_native_route, COLORS["cyan"]),
            ("connect.test_reply", self.test_native_route, COLORS["amber"]),
        ):
            button = tk.Button(
                native_buttons, text=self._t(key), command=command, bg=color,
                fg=COLORS["black"], activebackground=COLORS["cyan"], bd=2,
                font=("Cascadia Mono", 8, "bold"),
            )
            button.pack(side="left", padx=(0, 7), pady=2)
            self.native_connection_buttons[key] = button
        self.connection_status_label = tk.Label(
            native, textvariable=self.connection_status, bg=COLORS["panel"],
            fg=COLORS["muted"], anchor="w", justify="left", wraplength=760,
            font=("Cascadia Mono", 8),
        )
        self.connection_status_label.grid(row=8, column=0, columnspan=4, sticky="ew", padx=8, pady=8)

        cc = self.ccswitch_connection_frame = tk.LabelFrame(
            inner,
            text=self._t("connect.ccswitch_heading"),
            bg=COLORS["panel"], fg=COLORS["amber"],
            bd=2, relief="ridge", font=("Cascadia Mono", 10, "bold"),
        )
        cc.pack(fill="x", padx=10, pady=6)
        for column in range(6):
            cc.grid_columnconfigure(column, weight=1)
        self._localized_label(
            cc,
            "connect.field.app",
            bg=COLORS["panel"], fg=COLORS["amber"],
            font=("Cascadia Mono", 8, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(7, 2))
        app_combo = ttk.Combobox(
            cc,
            textvariable=self.ccswitch_app,
            values=CC_SWITCH_APPS,
            state="readonly",
        )
        app_combo.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6), ipady=4)
        app_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_ccswitch())
        self._localized_label(
            cc,
            "connect.field.provider",
            bg=COLORS["panel"], fg=COLORS["amber"],
            font=("Cascadia Mono", 8, "bold"),
        ).grid(row=0, column=1, sticky="w", padx=8, pady=(7, 2))
        self.ccswitch_provider_combo = ttk.Combobox(cc, textvariable=self.ccswitch_provider, values=(), state="readonly")
        self.ccswitch_provider_combo.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=(0, 6), ipady=4)
        self.ccswitch_provider_combo.bind("<<ComboboxSelected>>", lambda _e: self.fetch_ccswitch_models())
        self._localized_label(
            cc,
            "connect.field.model",
            bg=COLORS["panel"], fg=COLORS["amber"],
            font=("Cascadia Mono", 8, "bold"),
        ).grid(row=0, column=3, sticky="w", padx=8, pady=(7, 2))
        self.ccswitch_model_combo = ttk.Combobox(cc, textvariable=self.ccswitch_model, values=(), state="readonly")
        self.ccswitch_model_combo.grid(row=1, column=3, sticky="ew", padx=8, pady=(0, 6), ipady=4)
        self._field(
            cc, "", self.ccswitch_agent, 0, 4,
            localization_key="connect.field.agent_id",
        )
        self._field(
            cc, "", self.ccswitch_reasoning, 0, 5,
            localization_key="connect.field.reasoning",
        )
        buttons = tk.Frame(cc, bg=COLORS["panel"])
        buttons.grid(row=2, column=0, columnspan=6, sticky="ew", padx=8, pady=6)
        self.ccswitch_connection_buttons: dict[str, tk.Button] = {}
        for key, command, color in (
            ("connect.ccswitch_refresh", self.refresh_ccswitch, COLORS["blue"]),
            ("connect.ccswitch_models", self.fetch_ccswitch_models, COLORS["purple"]),
            ("connect.ccswitch_register", self.register_ccswitch_route, COLORS["green"]),
            ("connect.ccswitch_sync", self.sync_ccswitch_routes, COLORS["cyan"]),
            ("connect.ccswitch_switch", self.switch_ccswitch_provider, COLORS["amber"]),
            ("connect.ccswitch_open", self.open_ccswitch, COLORS["cyan"]),
        ):
            button = tk.Button(
                buttons,
                text=self._t(key),
                command=command,
                bg=color,
                fg=COLORS["black"],
                bd=2,
                font=("Cascadia Mono", 8, "bold"),
            )
            button.pack(side="left", padx=(0, 7), pady=2)
            self.ccswitch_connection_buttons[key] = button
        self.ccswitch_status_label = tk.Label(
            cc, textvariable=self.ccswitch_status, bg=COLORS["panel"],
            fg=COLORS["muted"], anchor="w", justify="left", wraplength=1000,
            font=("Cascadia Mono", 8),
        )
        self.ccswitch_status_label.grid(row=3, column=0, columnspan=6, sticky="ew", padx=8, pady=(2, 9))

        self.connection_tree = DetailTree(
            inner,
            [("id", "CONNECTION", 180), ("name", "NAME", 180), ("class", "CLASS", 90),
             ("backend", "SECRET BACKEND", 190), ("enabled", "ENABLED", 75), ("hash", "SHA", 120)],
        )
        self.connection_tree.pack(fill="both", expand=True, padx=10, pady=(6, 12))
        self.pages["connect"] = page

    def refresh_official_agent_statuses(self) -> None:
        if self._agent_detection_in_progress or self._closing:
            return
        self._agent_detection_in_progress = True
        for spec in installable_agent_specs():
            self.agent_install_status[spec.agent_id].set(
                self._t("agent_install.detecting")
            )

        def worker() -> None:
            try:
                statuses = detect_all_installable_agents()
            except Exception as exc:
                self._post_to_ui(self._finish_agent_detection, (), exc)
                return
            self._post_to_ui(self._finish_agent_detection, statuses, None)

        threading.Thread(
            target=worker, name="peerbridge-agent-detection", daemon=True
        ).start()

    def _finish_agent_detection(
        self,
        statuses: tuple[AgentInstallStatus, ...],
        error: Exception | None,
    ) -> None:
        self._agent_detection_in_progress = False
        if error is not None:
            for spec in installable_agent_specs():
                self.agent_install_status[spec.agent_id].set(
                    self._t("agent_install.detect_failed")
                )
            return
        self._agent_install_statuses = {
            status.agent_id: status for status in statuses
        }
        for spec in installable_agent_specs():
            status = self._agent_install_statuses[spec.agent_id]
            if status.installed:
                detail = status.version or self._t("agent_install.version_unknown")
                text = self._t("agent_install.installed").format(version=detail)
            else:
                text = self._t("agent_install.not_installed")
            text += "  " + self._t(spec.note_key)
            self.agent_install_status[spec.agent_id].set(text)
            action_key = (
                "agent_install.open_guide"
                if not spec.automatic_install_supported
                else (
                    "agent_install.update"
                    if status.installed
                    else "agent_install.install"
                )
            )
            self._agent_install_buttons[spec.agent_id].configure(
                text=self._t(action_key).format(name=spec.display_name),
                state="normal",
            )

    def install_official_agent(self, agent_id: str) -> None:
        try:
            spec = installable_agent_spec(agent_id)
        except AgentInstallError:
            return
        if not spec.automatic_install_supported:
            webbrowser.open(spec.docs_url)
            self.agent_install_status[agent_id].set(
                self._t("agent_install.manual_only")
            )
            return
        if agent_id in self._agent_install_processes:
            return
        status = self._agent_install_statuses.get(agent_id)
        update = bool(status and status.installed)
        if not messagebox.askyesno(
            self._t("agent_install.confirm_title"),
            self._t("agent_install.confirm_body").format(
                name=spec.display_name,
                package=spec.package_identifier,
            ),
            parent=self.root,
        ):
            return
        try:
            process = launch_agent_installer(agent_id, update=update)
        except AgentInstallError as exc:
            self.agent_install_status[agent_id].set(
                self._t("agent_install.launch_failed").format(error=clip(exc, 100))
            )
            return
        self._agent_install_processes[agent_id] = process
        self._agent_install_buttons[agent_id].configure(state="disabled")
        self.agent_install_status[agent_id].set(
            self._t("agent_install.running")
        )

        def waiter() -> None:
            try:
                return_code = process.wait()
            except Exception:
                return_code = -1
            self._post_to_ui(
                self._finish_agent_install, agent_id, int(return_code)
            )

        threading.Thread(
            target=waiter,
            name=f"peerbridge-agent-install-{agent_id}",
            daemon=True,
        ).start()

    def _finish_agent_install(self, agent_id: str, return_code: int) -> None:
        self._agent_install_processes.pop(agent_id, None)
        self._agent_install_buttons[agent_id].configure(state="normal")
        if return_code == 0:
            self.agent_install_status[agent_id].set(
                self._t("agent_install.completed")
            )
        else:
            self.agent_install_status[agent_id].set(
                self._t("agent_install.failed").format(code=return_code)
            )
        self.refresh_official_agent_statuses()

    def _safe_identifier(self, value: str, label: str) -> str:
        text = value.strip()
        if not SAFE_ROUTE_ID.fullmatch(text):
            raise ValueError(
                self._t("connect.identifier_chars").format(label=label)
            )
        return text

    def _safe_error(self, exc: Exception) -> str:
        if isinstance(exc, (CredentialStoreError, CcSwitchError, RunnerError, ValueError)):
            return clip(str(exc), 180)
        return self._t("connect.safe_error")

    def save_native_connection(self) -> None:
        if self.connection_in_progress:
            return
        try:
            connection_id = self._safe_identifier(
                self.connection_id.get(), self._t("connect.field.connection_id")
            )
            display_name = self.connection_name.get().strip()
            route_class = self.connection_class.get().strip()
            endpoint = self.connection_endpoint.get().strip()
            api_key = self.connection_api_key.get().strip()
            if not display_name:
                raise ValueError(self._t("provider.display_name_required"))
            if route_class not in {"official", "relay", "local"}:
                raise ValueError(self._t("provider.class_invalid"))
            if not endpoint:
                raise ValueError(self._t("provider.endpoint_required"))
            if route_class == "local":
                if not is_loopback_endpoint(endpoint):
                    raise ValueError(self._t("provider.local_loopback_only"))
            elif not api_key:
                raise ValueError(self._t("provider.api_key_required"))
        except Exception as exc:
            self.connection_status.set(self._safe_error(exc))
            self.connection_status_label.configure(fg=COLORS["red"])
            return

        self.connection_in_progress = True
        self.connection_status.set(self._t("provider.saving"))
        self.connection_status_label.configure(fg=COLORS["amber"])

        def worker() -> None:
            try:
                if route_class == "local":
                    reference = store_local_provider_endpoint(
                        scope=self.scope,
                        connection_id=connection_id,
                        endpoint=endpoint,
                        provider_id=connection_id,
                    )
                else:
                    reference = store_provider_credentials(
                        scope=self.scope,
                        connection_id=connection_id,
                        endpoint=endpoint,
                        api_key=api_key,
                        route_class=route_class,
                        provider_id=connection_id,
                    )
                discovery = discover_provider_models(
                    scope=self.scope,
                    connection_id=connection_id,
                    route_class=route_class,
                    provider_id=connection_id,
                )
                connection = self.human_client.call_tool(
                    "upsert_provider_connection",
                    {
                        "connection_id": connection_id,
                        "display_name": display_name,
                        "route_class": route_class,
                        "provider_id": reference.provider_id,
                        "secret_backend": "windows-credential-manager",
                        "credential_target": reference.credential_target,
                        "endpoint_sha256": reference.endpoint_sha256,
                        "credential_fingerprint_sha256": reference.credential_fingerprint_sha256,
                        "descriptor_schema": reference.descriptor_schema,
                        "credential_version_sha256": reference.credential_version_sha256,
                        "enabled": True,
                    },
                )
            except Exception as exc:
                self._post_to_ui(
                    self._finish_native_connection, None, None, None, exc
                )
                return
            self._post_to_ui(
                self._finish_native_connection, connection, None, discovery, None
            )

        threading.Thread(target=worker, name="peerbridge-provider-save", daemon=True).start()

    def _finish_native_connection(
        self,
        connection: dict[str, Any] | None,
        route: dict[str, Any] | None,
        discovery: ProviderModelRegistry | None,
        error: Exception | None,
    ) -> None:
        self.connection_in_progress = False
        self.connection_api_key.set("")
        self.connection_key_visible.set(False)
        self._apply_connection_key_visibility()
        self.connection_endpoint.set("")
        if error:
            self.connection_status.set(
                self._t("provider.save_failed").format(
                    error=self._safe_error(error)
                )
            )
            self.connection_status_label.configure(fg=COLORS["red"])
            return
        assert connection is not None
        assert discovery is not None
        self.connection_model_combo.configure(values=discovery.models)
        if len(discovery.models) == 1:
            self.connection_model.set(discovery.models[0])
        suffix = self._t(
            "connect.route_registered_suffix"
            if route
            else "connect.route_missing_suffix"
        ).format(route=route["route_id"] if route else "")
        self.connection_status.set(
            self._t("connect.saved").format(
                connection=connection["connection_id"],
                count=len(discovery.models),
                suffix=f"SHA {connection['connection_sha256'][:16]}{suffix}",
            )
        )
        self.connection_status_label.configure(fg=COLORS["green"])
        self.refresh(force=True)

    def discover_native_models(self) -> None:
        if self.connection_in_progress:
            return
        try:
            connection_id = self._safe_identifier(
                self.connection_id.get(), self._t("connect.field.connection_id")
            )
            route_class = self.connection_class.get().strip()
            if route_class not in {"official", "relay", "local"}:
                raise ValueError(self._t("provider.class_invalid"))
        except Exception as exc:
            self.connection_status.set(self._safe_error(exc))
            self.connection_status_label.configure(fg=COLORS["red"])
            return
        self.connection_in_progress = True
        self.connection_status.set(self._t("connect.discovering"))
        self.connection_status_label.configure(fg=COLORS["amber"])

        def worker() -> None:
            try:
                discovery = discover_provider_models(
                    scope=self.scope,
                    connection_id=connection_id,
                    route_class=route_class,
                    provider_id=connection_id,
                )
            except Exception as exc:
                self._post_to_ui(self._finish_native_discovery, None, exc)
                return
            self._post_to_ui(self._finish_native_discovery, discovery, None)

        threading.Thread(target=worker, name="peerbridge-direct-models", daemon=True).start()

    def _finish_native_discovery(
        self,
        discovery: ProviderModelRegistry | None,
        error: Exception | None,
    ) -> None:
        self.connection_in_progress = False
        if error:
            self.connection_status.set(
                self._t("connect.discovery_failed").format(
                    error=self._safe_error(error)
                )
            )
            self.connection_status_label.configure(fg=COLORS["red"])
            return
        assert discovery is not None
        self.connection_model_combo.configure(values=discovery.models)
        if len(discovery.models) == 1:
            self.connection_model.set(discovery.models[0])
        self.connection_status.set(
            self._t("connect.discovered").format(
                count=len(discovery.models),
                sha=discovery.registry_sha256[:16],
            )
        )
        self.connection_status_label.configure(fg=COLORS["green"])

    def register_native_route(self) -> None:
        if self.connection_in_progress:
            return
        try:
            connection_id = self._safe_identifier(
                self.connection_id.get(), self._t("connect.field.connection_id")
            )
            route_class = self.connection_class.get().strip()
            agent_id = self._safe_identifier(
                self.connection_agent.get(), self._t("connect.field.agent_id")
            )
            route_id = self._safe_identifier(
                self.connection_route_id.get(), self._t("connect.field.route_id")
            )
            model_id = self._safe_identifier(
                self.connection_model.get(), self._t("connect.field.model")
            )
            response_model_id = self.connection_response_model.get().strip()
            timeout_text = self.connection_timeout_seconds.get().strip()
            client_name = self.connection_client.get().strip()
            reasoning = self.connection_reasoning.get().strip()
            if client_name:
                client_name = self._safe_identifier(
                    client_name, self._t("connect.field.client_name")
                )
            if response_model_id:
                response_model_id = self._safe_identifier(
                    response_model_id, self._t("connect.field.response_model")
                )
            if reasoning:
                reasoning = self._safe_identifier(
                    reasoning, self._t("connect.field.reasoning")
                )
            if not timeout_text.isdigit() or not 1 <= int(timeout_text) <= 300:
                raise ValueError(self._t("connect.timeout_invalid"))
            inference_timeout_seconds = int(timeout_text)
            advertised = tuple(self.connection_model_combo.cget("values"))
            if advertised and model_id not in advertised:
                raise ValueError(self._t("connect.model_unverified"))
            connections = self.human_client.call_tool(
                "list_provider_connections", {"enabled_only": True}
            ).get("connections", [])
            matches = [
                row for row in connections
                if isinstance(row, dict)
                and row.get("connection_id") == connection_id
                and row.get("provider_id") == connection_id
                and row.get("route_class") == route_class
                and row.get("secret_backend") == "windows-credential-manager"
            ]
            if len(matches) != 1:
                raise ValueError(self._t("connect.connection_missing"))
            payload: dict[str, Any] = {
                "route_id": route_id,
                "agent_id": agent_id,
                "provider_id": connection_id,
                "model_id": model_id,
                "inference_timeout_seconds": inference_timeout_seconds,
                "route_class": route_class,
                "enabled": True,
            }
            if client_name:
                payload["client_name"] = client_name
            if response_model_id:
                payload["response_model_id"] = response_model_id
            if reasoning:
                payload["reasoning_mode"] = reasoning
            route = self.human_client.call_tool("upsert_route_profile", payload)
        except Exception as exc:
            self.connection_status.set(
                self._t("connect.route_failed").format(
                    error=self._safe_error(exc)
                )
            )
            self.connection_status_label.configure(fg=COLORS["red"])
            return
        self.connection_status.set(
            self._t("connect.route_registered").format(
                route=route["route_id"],
                model=route["model_id"],
                sha=route["profile_sha256"][:16],
            )
        )
        self.connection_status_label.configure(fg=COLORS["green"])
        self.refresh(force=True)

    def test_native_route(self) -> None:
        """Queue one auditable, explicitly paid route probe through normal dispatch."""
        try:
            agent_id = self._safe_identifier(
                self.connection_agent.get(), self._t("connect.field.agent_id")
            )
            route_id = self._safe_identifier(
                self.connection_route_id.get(), self._t("connect.field.route_id")
            )
            profiles = self.human_client.call_tool(
                "list_route_profiles",
                {"agent_id": agent_id, "enabled_only": True},
            ).get("profiles", [])
            matches = [
                row
                for row in profiles
                if isinstance(row, dict) and row.get("route_id") == route_id
            ]
            if len(matches) != 1:
                raise ValueError(self._t("provider.test_route_missing"))
            profile = matches[0]
        except Exception as exc:
            self.connection_status.set(self._safe_error(exc))
            self.connection_status_label.configure(fg=COLORS["red"])
            return
        if not messagebox.askyesno(
            self._t("provider.test_confirm_title"),
            self._t("provider.test_confirm_body").format(
                agent=agent_id,
                model=profile.get("model_id") or "provider-default",
            ),
            parent=self.root,
        ):
            return
        task_id = f"provider-preflight-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        try:
            receipt = self.human_client.send_message(
                room_id=DEFAULT_ROOM_ID,
                recipient=agent_id,
                task_id=task_id,
                subject="PROVIDER PREFLIGHT",
                body=(
                    "Reply with exactly PEERBRIDGE_PROVIDER_OK. This is an explicit "
                    "one-request provider availability test; do not call tools."
                ),
                priority="high",
                route_profile_id=route_id,
                requested_provider_id=str(profile.get("provider_id") or "") or None,
                requested_model_id=str(profile.get("model_id") or "") or None,
                requested_reasoning_mode=(
                    str(profile.get("reasoning_mode") or "") or None
                ),
            )
        except Exception as exc:
            self.connection_status.set(
                self._t("provider.test_queue_failed").format(
                    error=self._safe_error(exc)
                )
            )
            self.connection_status_label.configure(fg=COLORS["red"])
            return
        self.connection_status.set(
            self._t("provider.test_queued").format(
                task=task_id,
                sha=str(receipt.get("content_sha256") or "")[:16],
            )
        )
        self.connection_status_label.configure(fg=COLORS["amber"])
        self.selected_room_id = DEFAULT_ROOM_ID
        self._request_room_refresh(force=True)
        self.show_page("chat")

    def refresh_ccswitch(self) -> None:
        if not ccswitch_find_cli():
            self.ccswitch_status.set(self._t("ccswitch.cli_missing"))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        app = self.ccswitch_app.get()
        self.ccswitch_status.set(
            self._t("ccswitch.reading").format(app=app)
        )
        self.ccswitch_status_label.configure(fg=COLORS["amber"])

        def worker() -> None:
            try:
                providers = ccswitch_list_providers(app)
            except Exception as exc:
                self._post_to_ui(self._finish_ccswitch_refresh, (), exc)
                return
            self._post_to_ui(self._finish_ccswitch_refresh, providers, None)

        threading.Thread(target=worker, name="ccswitch-provider-list", daemon=True).start()

    def _finish_ccswitch_refresh(
        self, providers: tuple[CcSwitchProvider, ...], error: Exception | None
    ) -> None:
        if error:
            self.ccswitch_status.set(self._safe_error(error))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        labels: dict[str, CcSwitchProvider] = {}
        for provider in providers:
            marker = "* " if provider.current else "  "
            labels[f"{marker}{provider.name} [{provider.provider_id}]"] = provider
        self._ccswitch_providers = labels
        self.ccswitch_provider_combo.configure(values=tuple(labels))
        selected = next((label for label, item in labels.items() if item.current), "")
        self.ccswitch_provider.set(selected or (next(iter(labels), "")))
        self.ccswitch_model.set("")
        self.ccswitch_model_combo.configure(values=())
        self.ccswitch_status.set(
            self._t("ccswitch.read").format(count=len(labels))
        )
        self.ccswitch_status_label.configure(fg=COLORS["green"])

    def _selected_ccswitch_provider(self) -> CcSwitchProvider:
        provider = self._ccswitch_providers.get(self.ccswitch_provider.get())
        if not provider:
            raise ValueError(self._t("ccswitch.choose"))
        return provider

    def fetch_ccswitch_models(self) -> None:
        try:
            provider = self._selected_ccswitch_provider()
        except Exception as exc:
            self.ccswitch_status.set(self._safe_error(exc))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        self.ccswitch_status.set(self._t("ccswitch.fetching"))
        self.ccswitch_status_label.configure(fg=COLORS["amber"])

        def worker() -> None:
            try:
                models = ccswitch_fetch_models(provider.app, provider.provider_id)
            except Exception as exc:
                self._post_to_ui(self._finish_ccswitch_models, (), exc)
                return
            self._post_to_ui(self._finish_ccswitch_models, models, None)

        threading.Thread(target=worker, name="ccswitch-model-list", daemon=True).start()

    def _finish_ccswitch_models(self, models: tuple[str, ...], error: Exception | None) -> None:
        if error:
            self.ccswitch_status.set(self._safe_error(error))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        self.ccswitch_model_combo.configure(values=models)
        self.ccswitch_model.set(models[0] if models else "")
        self.ccswitch_status.set(
            self._t("ccswitch.fetched").format(count=len(models))
        )
        self.ccswitch_status_label.configure(fg=COLORS["green"])

    def register_ccswitch_route(self) -> None:
        try:
            provider = self._selected_ccswitch_provider()
            agent_id = self._safe_identifier(
                self.ccswitch_agent.get(), self._t("connect.field.agent_id")
            )
            model = self._safe_identifier(
                self.ccswitch_model.get(), self._t("connect.field.model")
            )
            reasoning = self.ccswitch_reasoning.get().strip()
            if reasoning:
                reasoning = self._safe_identifier(
                    reasoning, self._t("connect.field.reasoning")
                )
        except Exception as exc:
            self.ccswitch_status.set(self._safe_error(exc))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        connection, routes = ccswitch_route_specs(
            provider,
            agent_id=agent_id,
            models=(model,),
            reasoning_mode=reasoning or None,
        )
        self.ccswitch_status.set(self._t("ccswitch.registering"))
        self.ccswitch_status_label.configure(fg=COLORS["amber"])

        def worker() -> None:
            try:
                self.human_client.call_tool(
                    "upsert_provider_connection",
                    connection,
                )
                route = self.human_client.call_tool("upsert_route_profile", routes[0])
            except Exception as exc:
                self._post_to_ui(self._finish_ccswitch_route, None, exc)
                return
            self._post_to_ui(self._finish_ccswitch_route, route, None)

        threading.Thread(target=worker, name="ccswitch-route-register", daemon=True).start()

    def sync_ccswitch_routes(self) -> None:
        """Register all models from one saved source without switching providers."""
        try:
            provider = self._selected_ccswitch_provider()
            agent_id = self._safe_identifier(
                self.ccswitch_agent.get(), self._t("connect.field.agent_id")
            )
            reasoning = self.ccswitch_reasoning.get().strip()
            if reasoning:
                reasoning = self._safe_identifier(
                    reasoning, self._t("connect.field.reasoning")
                )
        except Exception as exc:
            self.ccswitch_status.set(self._safe_error(exc))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        self.ccswitch_status.set(self._t("ccswitch.syncing"))
        self.ccswitch_status_label.configure(fg=COLORS["amber"])
        no_models_error = self._t("ccswitch.no_models")

        def worker() -> None:
            try:
                models = ccswitch_fetch_models(provider.app, provider.provider_id)
                if not models:
                    raise ValueError(no_models_error)
                connection, routes = ccswitch_route_specs(
                    provider,
                    agent_id=agent_id,
                    models=models,
                    reasoning_mode=reasoning or None,
                )
                self.human_client.call_tool("upsert_provider_connection", connection)
                for route in routes:
                    self.human_client.call_tool("upsert_route_profile", route)
            except Exception as exc:
                self._post_to_ui(self._finish_ccswitch_sync, 0, exc)
                return
            self._post_to_ui(self._finish_ccswitch_sync, len(routes), None)

        threading.Thread(target=worker, name="ccswitch-route-sync", daemon=True).start()

    def _finish_ccswitch_sync(self, count: int, error: Exception | None) -> None:
        if error:
            self.ccswitch_status.set(self._safe_error(error))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        self.ccswitch_status.set(
            self._t("ccswitch.synced").format(count=count)
        )
        self.ccswitch_status_label.configure(fg=COLORS["green"])
        self.refresh(force=True)

    def _finish_ccswitch_route(self, route: dict[str, Any] | None, error: Exception | None) -> None:
        if error:
            self.ccswitch_status.set(self._safe_error(error))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        assert route is not None
        self.ccswitch_status.set(
            self._t("ccswitch.registered").format(route=route["route_id"])
        )
        self.ccswitch_status_label.configure(fg=COLORS["green"])
        self.refresh(force=True)

    def switch_ccswitch_provider(self) -> None:
        try:
            provider = self._selected_ccswitch_provider()
        except Exception as exc:
            self.ccswitch_status.set(self._safe_error(exc))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        if not messagebox.askyesno(
            self._t("ccswitch.switch_title"),
            self._t("ccswitch.switch_body").format(
                app=provider.app,
                provider=provider.name,
            ),
        ):
            return
        try:
            ccswitch_switch_provider(provider.app, provider.provider_id)
        except Exception as exc:
            self.ccswitch_status.set(self._safe_error(exc))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        self.ccswitch_status.set(
            self._t("ccswitch.switched").format(
                app=provider.app,
                provider=provider.name,
            )
        )
        self.ccswitch_status_label.configure(fg=COLORS["green"])
        self.refresh_ccswitch()

    def open_ccswitch(self) -> None:
        if not ccswitch_find_app():
            self.ccswitch_status.set(self._t("ccswitch.app_missing"))
            self.ccswitch_status_label.configure(fg=COLORS["red"])
            return
        try:
            ccswitch_open_app()
        except Exception as exc:
            self.ccswitch_status.set(self._safe_error(exc))
            self.ccswitch_status_label.configure(fg=COLORS["red"])

    def _selected_recipient_id(self) -> str:
        selected = self.message_recipient.get()
        return self._recipient_ids.get(selected, selected)

    def _scope_profiles(self) -> list[dict[str, Any]]:
        if not self.snapshot:
            return []
        recipient = self._selected_recipient_id()
        return [
            row
            for row in self.snapshot.route_profiles
            if row.get("enabled")
            and row.get("scope") == self.scope
            and row.get("agent_id") == recipient
        ]

    def _profile_provider_label(self, profile: dict[str, Any]) -> str:
        connections = self.snapshot.provider_connections if self.snapshot else ()
        return provider_display_label(profile, connections)

    def _reset_route_selection(self) -> None:
        self.message_route_profile.set("DIRECT")
        self.message_provider_choice.set(self._t(DIRECT_LABEL))
        self.message_provider.set("")
        self.message_model.set("")
        self.message_reasoning.set("")
        self.model_combo.configure(values=("",), state="disabled")
        self.reasoning_combo.configure(values=("",), state="disabled")

    def _on_recipient_selected(self, _event: Any = None) -> None:
        self._reset_route_selection()
        recipient = self._selected_recipient_id()
        if recipient == "*":
            broadcast_route_label = self._t(BROADCAST_ROUTE_LABEL)
            self.message_provider_choice.set(broadcast_route_label)
            self.profile_combo.configure(
                values=(broadcast_route_label,), state="disabled"
            )
            return
        profiles = self._scope_profiles()
        labels = sorted({self._profile_provider_label(row) for row in profiles})
        if labels:
            self.profile_combo.configure(
                values=(self._t(DIRECT_LABEL), *labels), state="readonly"
            )
        else:
            no_route_label = self._t(NO_ROUTE_LABEL)
            self.message_provider_choice.set(no_route_label)
            self.profile_combo.configure(values=(no_route_label,), state="disabled")

    def _on_provider_selected(self, _event: Any = None) -> None:
        selected = self.message_provider_choice.get()
        self.message_route_profile.set("DIRECT")
        self.message_provider.set("")
        self.message_model.set("")
        self.message_reasoning.set("")
        if any(
            self._catalog_value_matches(selected, key)
            for key in (DIRECT_LABEL, BROADCAST_ROUTE_LABEL, NO_ROUTE_LABEL)
        ):
            self.model_combo.configure(values=("",), state="disabled")
            self.reasoning_combo.configure(values=("",), state="disabled")
            return
        profiles = [
            row for row in self._scope_profiles()
            if self._profile_provider_label(row) == selected
        ]
        if not profiles:
            self._reset_route_selection()
            return
        self.message_provider.set(str(profiles[0].get("provider_id") or ""))
        models = sorted({str(row.get("model_id")) for row in profiles if row.get("model_id")})
        self.model_combo.configure(values=("", *models), state="readonly")
        if len(models) == 1:
            self.message_model.set(models[0])
            self._on_model_selected()
        else:
            self.reasoning_combo.configure(values=("",), state="disabled")

    def _on_model_selected(self, _event: Any = None) -> None:
        self.message_route_profile.set("DIRECT")
        provider = self.message_provider.get()
        model = self.message_model.get()
        profiles = [
            row for row in self._scope_profiles()
            if row.get("provider_id") == provider and row.get("model_id") == model
        ]
        modes = sorted({str(row.get("reasoning_mode")) for row in profiles if row.get("reasoning_mode")})
        self.reasoning_combo.configure(values=("", *modes), state="readonly" if modes else "disabled")
        self.message_reasoning.set(modes[0] if len(modes) == 1 else "")
        self._select_exact_route()

    def _on_reasoning_selected(self, _event: Any = None) -> None:
        self.message_route_profile.set("DIRECT")
        self._select_exact_route()

    def _sync_priority_choices(self) -> None:
        if not hasattr(self, "priority_combo"):
            return
        self._priority_ids = {
            self._t(f"chat.priority.{priority}"): priority
            for priority in MESSAGE_PRIORITIES
        }
        values = tuple(self._priority_ids)
        self.priority_combo.configure(values=values)
        selected = self.message_priority.get()
        self.message_priority_label.set(
            self._t(f"chat.priority.{selected}")
            if selected in MESSAGE_PRIORITIES
            else self._t("chat.priority.normal")
        )

    def _on_priority_selected(self, _event: Any = None) -> None:
        selected = getattr(self, "_priority_ids", {}).get(
            self.message_priority_label.get()
        )
        if selected in MESSAGE_PRIORITIES:
            self.message_priority.set(selected)

    def _select_exact_route(self) -> None:
        self.message_route_profile.set("DIRECT")
        provider = self.message_provider.get()
        model = self.message_model.get()
        reasoning = self.message_reasoning.get()
        matches = [
            row for row in self._scope_profiles()
            if str(row.get("provider_id") or "") == provider
            and str(row.get("model_id") or "") == model
            and str(row.get("reasoning_mode") or "") == reasoning
        ]
        if len(matches) == 1:
            self.message_route_profile.set(str(matches[0]["route_id"]))

    def _send_draft_snapshot(
        self,
    ) -> tuple[str, str, str, str, tuple[Path, ...]]:
        return (
            self.message_body.get("1.0", "end-1c"),
            self.message_task.get(),
            self.message_subject.get(),
            self.message_priority.get(),
            tuple(self._chat_attachment_paths),
        )

    def _on_message_enter(self, event: tk.Event[Any]) -> str | None:
        if event.state & 0x0001:  # Shift+Enter inserts a newline.
            return None
        self.send_human_message()
        return "break"

    def _choose_chat_attachments(self) -> None:
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title=self._t("chat.attach"),
            filetypes=(
                ("Safe images and text", "*.png *.jpg *.jpeg *.gif *.webp *.txt *.md *.csv *.json *.log"),
                ("All files", "*.*"),
            ),
        )
        if not selected:
            return
        self._chat_attachment_paths = tuple(Path(value) for value in selected[:5])
        self.chat_attachment_status.set(
            self._t("chat.attachments_selected").format(
                count=len(self._chat_attachment_paths)
            )
        )
        if ACTIVE_THEME == "modern":
            self._configure_chat_composer_layout(self._chat_composer_compact)
            self.root.after_idle(self._layout_chat_after_resize)
        self._sync_room_control_states()

    def _clear_chat_attachments(self) -> None:
        self._chat_attachment_paths = ()
        self.chat_attachment_status.set(self._t("chat.no_attachments"))
        if ACTIVE_THEME == "modern":
            self._configure_chat_composer_layout(self._chat_composer_compact)
            self.root.after_idle(self._layout_chat_after_resize)
        self._sync_room_control_states()

    def send_human_message(self) -> None:
        if self.send_in_progress:
            return
        if not self._room_has_operator():
            self.message_status.set(self._t("chat.join_to_send"))
            self.message_status_label.configure(fg=COLORS["red"])
            return
        draft_snapshot = self._send_draft_snapshot()
        raw_body, raw_task_id, raw_subject, priority, selected_attachments = (
            draft_snapshot
        )
        body = raw_body.strip()
        task_id = raw_task_id.strip()
        subject = raw_subject.strip()
        route_profile = self.message_route_profile.get().strip()
        provider = self.message_provider.get().strip()
        model = self.message_model.get().strip()
        reasoning = self.message_reasoning.get().strip()
        if not body or not task_id or not subject:
            self.message_status.set(self._t("chat.required_fields"))
            self.message_status_label.configure(fg=COLORS["red"])
            return
        if any((provider, model, reasoning)):
            self._select_exact_route()
            route_profile = self.message_route_profile.get().strip()
            if route_profile == "DIRECT" or self._catalog_value_matches(
                route_profile, DIRECT_LABEL
            ):
                self.message_status.set(self._t("chat.route_unique_required"))
                self.message_status_label.configure(fg=COLORS["red"])
                return
        recipient = self._selected_recipient_id()
        if recipient == "*":
            automation_mode = (
                self._automation_mode_from_label(self.room_automation_choice.get())
                or "once"
            )
            missing_routes = (
                ()
                if automation_mode == "off"
                else room_members_missing_routes(self._room_members)
            )
            if missing_routes:
                self.message_status.set(
                    self._t("chat.broadcast_missing_routes").format(
                        agents=", ".join(missing_routes)
                    )
                )
                self.message_status_label.configure(fg=COLORS["red"])
                return
        routed = (
            route_profile != "DIRECT"
            and not self._catalog_value_matches(route_profile, DIRECT_LABEL)
        ) or any((provider, model, reasoning))
        if routed and recipient == "*":
            self.message_status.set(self._t("chat.routed_broadcast_forbidden"))
            self.message_status_label.configure(fg=COLORS["red"])
            return
        requested_room_id = self.selected_room_id
        payload = {
            "room_id": requested_room_id,
            "recipient": recipient,
            "task_id": task_id,
            "subject": subject,
            "body": body,
            "priority": priority,
            "route_profile_id": (
                route_profile
                if route_profile != "DIRECT"
                and not self._catalog_value_matches(route_profile, DIRECT_LABEL)
                else None
            ),
            "requested_provider_id": provider or None,
            "requested_model_id": model or None,
            "requested_reasoning_mode": reasoning or None,
        }
        self._send_token_sequence += 1
        send_token = self._send_token_sequence
        self._active_send_token = send_token
        self.send_in_progress = True
        self.send_button.configure(
            state="disabled", text=self._t("chat.sending_button")
        )
        self.chat_attach_button.configure(state="disabled")
        self.chat_clear_attachments_button.configure(state="disabled")
        self.message_status.set(
            self._t("chat.sending").format(room=requested_room_id)
        )
        self.message_status_label.configure(fg=COLORS["amber"])

        def worker() -> None:
            try:
                staged = stage_chat_attachments(
                    self.project_root, selected_attachments
                )
                artifact_paths = [item.relative_path for item in staged]
                if recipient == "*":
                    receipt = self.human_client.post_room_message(
                        room_id=requested_room_id,
                        task_id=task_id,
                        subject=subject,
                        body=body,
                        priority=str(payload["priority"]),
                        artifact_paths=artifact_paths,
                    )
                else:
                    payload["artifact_paths"] = artifact_paths
                    receipt = self.human_client.send_message(**payload)
            except Exception as exc:
                error_text = str(exc)
                self._post_to_ui(
                    self._finish_human_send,
                    None,
                    error_text,
                    requested_room_id,
                    send_token,
                    draft_snapshot,
                )
                return
            self._post_to_ui(
                self._finish_human_send,
                receipt,
                None,
                requested_room_id,
                send_token,
                draft_snapshot,
            )

        threading.Thread(target=worker, name="mcp-human-send", daemon=True).start()

    def _finish_human_send(
        self,
        receipt: dict[str, Any] | None,
        error: str | None,
        expected_room_id: str,
        send_token: int,
        draft_snapshot: tuple[str, str, str, str, tuple[Path, ...]],
    ) -> None:
        if send_token != self._active_send_token:
            return
        self._active_send_token = None
        self.send_in_progress = False
        self.send_button.configure(state="normal", text=self._t("chat.send"))
        self._sync_room_control_states()
        if error:
            if self.selected_room_id != expected_room_id:
                return
            error_key = {
                "MESSAGE_SECRET_REJECTED": "chat.secret_rejected",
                "MESSAGE_BODY_TOO_LONG": "chat.body_too_long",
            }.get(error)
            safe_error = self._t(error_key) if error_key else clip(error, 180)
            self.message_status.set(
                self._t("chat.send_failed").format(error=safe_error)
            )
            self.message_status_label.configure(fg=COLORS["red"])
            return
        assert receipt is not None
        sha = str(receipt.get("content_sha256", ""))
        if self._send_draft_snapshot() == draft_snapshot:
            self.message_body.delete("1.0", "end")
            self._clear_chat_attachments()
        if self.selected_room_id != expected_room_id:
            return
        fanout = int(receipt.get("fanout_count") or 0)
        delivery = (
            self._t("chat.fanout").format(count=fanout) if fanout else ""
        )
        self.message_status.set(
            self._t("chat.sent").format(
                fanout=delivery,
                room=receipt.get("room_id") or self.selected_room_id,
                sha=sha[:16],
                created=utc_text(receipt.get("created_utc")),
            )
        )
        self.message_status_label.configure(fg=COLORS["green"])
        self._request_room_refresh(force=True)
        self.refresh(force=True)

    def _make_tree_page(self, host: tk.Frame, key: str, columns: list[tuple[str, str, int]]) -> DetailTree:
        page = DetailTree(host, columns)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages[key] = page
        return page

    def _execute_trust_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        callback: Callable[[dict[str, Any] | None, str | None], None],
    ) -> None:
        if self._trust_action_in_progress:
            callback(None, self._t("trustui.status.busy"))
            return
        self._trust_action_in_progress = True

        def worker() -> None:
            try:
                if name == "request_release_gate":
                    result = self.release_gate_service.request()
                    self.verification_engine.request_scan()
                elif name == "decide_release_gate":
                    result = self.release_gate_service.decide(
                        str(arguments.get("fingerprint") or ""),
                        decision=str(arguments.get("decision") or ""),
                        reason=str(arguments.get("reason") or ""),
                    )
                else:
                    result = self.human_client.call_tool(name, arguments, timeout=60)
                error = None
            except Exception as exc:
                result = None
                error = clip(redact_sensitive(exc), 300)
            self._post_to_ui(self._finish_trust_tool, callback, result, error)

        threading.Thread(
            target=worker,
            name=f"peerbridge-trust-{name}",
            daemon=True,
        ).start()

    def _finish_trust_tool(
        self,
        callback: Callable[[dict[str, Any] | None, str | None], None],
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        self._trust_action_in_progress = False
        callback(result, error)
        if error is None:
            self.refresh(force=True)

    def show_page(self, key: str) -> None:
        previous_page = getattr(self, "active_page", "")
        if key == "chat" and previous_page != "chat":
            self._chat_follow_latest_on_open = True
        self.active_page = key
        for page_key, page in self.pages.items():
            if page_key == key:
                page.tkraise()
        titles = {page_key: self._t(f"page.{page_key}") for page_key in self.pages}
        self.page_title.configure(text=titles[key])
        if ACTIVE_THEME == "modern":
            scope_label = getattr(self, "modern_toolbar_scope_label", None)
            if scope_label is not None:
                scope_text = self.scope
                if key == "chat" and self.selected_room_id:
                    room = self._rooms.get(self.selected_room_id, {})
                    room_name = str(room.get("name") or self.selected_room_id)
                    scope_text = f"{clip(room_name, 28)}  ·  {self.scope}"
                scope_label.configure(text=scope_text)
            self._sync_modern_recent_rooms()
            self._sync_modern_chat_context()
        for button_key, button in self.nav_buttons.items():
            active = button_key == key
            if ACTIVE_THEME == "modern":
                button.configure(
                    bg=MODERN_NAV_ACTIVE_BG if active else MODERN_SIDEBAR_BG,
                    fg=COLORS["text"],
                    highlightbackground=(
                        COLORS["line"] if active else MODERN_SIDEBAR_BG
                    ),
                )
            else:
                button.configure(
                    bg=COLORS["cyan"] if active else COLORS["panel"],
                    fg=COLORS["black"] if active else COLORS["text"],
                )
        if key == "announcement":
            self._render_announcements(self.search.get().strip().lower())
            self._mark_current_announcements_read()
        if self.snapshot:
            self._render_active_page(self.search.get().strip().lower())

    def toggle_pause(self) -> None:
        self.paused.set(not self.paused.get())
        self.stats_label.configure(fg=COLORS["amber"] if self.paused.get() else COLORS["muted"])
        if not self.paused.get():
            self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        # Manual refreshes and post-action refreshes replace the pending timer;
        # they must never create another permanent refresh chain.
        if self._refresh_after_id is not None:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
            self._refresh_after_id = None
        if self.paused.get() and not force:
            self.trust_workflows.refresh_verification_status()
            self._refresh_after_id = self.root.after(self.REFRESH_MS, self.refresh)
            return
        if force:
            self.refresh_status.set(self._t("toolbar.refreshing"))
        try:
            now = time.monotonic()
            database_token = self.reader.change_token()
            should_load = (
                force
                or self.snapshot is None
                or database_token != self._last_database_token
                or now - self._last_full_refresh >= 30.0
            )
            if should_load:
                snapshot = self.reader.snapshot(scope=self.scope)
                signature = snapshot.signature()
                self.snapshot = snapshot
                self._schedule_provider_model_discovery()
                self._last_database_token = self.reader.change_token()
                self._last_full_refresh = now
                if force or signature != self.last_signature:
                    self.last_signature = signature
                    self.render(force=force)
                self._request_room_refresh(force=force)
                self._last_successful_refresh = datetime.now()
                self.refresh_status.set(
                    self._t("toolbar.refreshed").format(
                        time=self._last_successful_refresh.strftime("%H:%M:%S")
                    )
                )
            self.root.title(WINDOW_TITLE_LIVE)
            self.trust_workflows.refresh_verification_status()
        except Exception as exc:  # UI must remain alive on transient WAL/lock errors.
            self.root.title(f"{WINDOW_TITLE} // ERROR")
            self.refresh_status.set(self._t("toolbar.refresh_failed"))
            self.stats_label.configure(
                text=f"{self._t('sidebar.database_error')}\n{clip(exc, 80)}",
                fg=COLORS["red"],
            )
        self._refresh_after_id = self.root.after(self.REFRESH_MS, self.refresh)

    def render(self, force: bool = False) -> None:
        if not self.snapshot:
            return
        query = self.search.get().strip().lower()
        self._render_presence()
        self._render_active_page(query)

    def _render_active_page(self, query: str) -> None:
        renderers: dict[str, Callable[[str], None]] = {
            "cockpit": lambda _query: self.cockpit.render(),
            "chat": self._render_chat,
            "work": self._render_work,
            "review": self._render_reviews,
            "change": self._render_changes,
            "audit": self._render_audit,
            "connect": self._render_connections,
            "memory": self._render_memories,
            "trust": lambda value: self.trust_workflows.render(self.snapshot, value),
            "feedback": lambda _query: None,
            "usage": self._render_usage,
            "announcement": self._render_announcements,
        }
        renderers[self.active_page](query)

    @staticmethod
    def _match(record: dict[str, Any], query: str) -> bool:
        if not query:
            return True
        return query in json.dumps(record, ensure_ascii=False).lower()

    def _render_presence(self) -> None:
        assert self.snapshot is not None
        now = time.time()
        profiles = [
            row
            for row in self.snapshot.route_profiles
            if row.get("enabled")
            and row.get("scope") == self.scope
            and row.get("agent_id") != HUMAN_AGENT_ID
        ]
        self._library_agents = merge_global_agent_catalog(
            build_global_agent_library(
                self.snapshot.presence,
                self.snapshot.route_profiles,
                self.scope,
                now_epoch=now,
            ),
            self._catalog_agents,
        )
        self._draw_agents(list(self._library_agents))
        library_ids = tuple(str(row["agent_id"]) for row in self._library_agents)
        self.seat_agent_combo.configure(values=library_ids)
        if self.seat_agent.get() not in library_ids:
            self.seat_agent.set("")
            self.library_selection.set(self._library_selection_text())
            self._seat_profiles = ()
            self._seat_provider_ids = {}
            self._reset_seat_route_selection()

        open_calls = sum(1 for row in self.snapshot.peer_calls if row.get("status") == "open")
        active_tasks = sum(1 for row in self.snapshot.tasks if row.get("status") not in {"complete", "closed"})
        dispatch_counts: dict[str, int] = {}
        for row in self.snapshot.message_dispatches:
            status = str(row.get("status") or "unknown").upper()
            dispatch_counts[status] = dispatch_counts.get(status, 0) + 1
        dispatch_line = "/".join(
            f"{label}{dispatch_counts.get(status, 0)}"
            for label, status in (
                (self._t("sidebar.dispatch_running"), "CLAIMED"),
                (self._t("sidebar.dispatch_retry"), "RETRYABLE"),
                (self._t("sidebar.dispatch_failed"), "FAILED"),
                (self._t("sidebar.dispatch_completed"), "COMPLETED"),
            )
        )
        online = sum(1 for row in self._library_agents if row.get("online"))
        recipient_ids = active_room_recipient_ids(
            self.selected_room_id,
            self._room_members,
            library_ids,
        )
        broadcast_label = self._t(BROADCAST_LABEL)
        self._recipient_ids = {
            broadcast_label: "*",
            **{name: name for name in recipient_ids},
        }
        current_recipient = self.message_recipient.get()
        values = tuple([broadcast_label, *recipient_ids])
        self.recipient_combo.configure(values=values)
        if current_recipient not in values:
            self.message_recipient.set(broadcast_label)
            self._on_recipient_selected()
        elif self._selected_recipient_id() == "*":
            broadcast_route_label = self._t(BROADCAST_ROUTE_LABEL)
            self.message_provider_choice.set(broadcast_route_label)
            self.profile_combo.configure(
                values=(broadcast_route_label,), state="disabled"
            )
            self.model_combo.configure(values=("",), state="disabled")
            self.reasoning_combo.configure(values=("",), state="disabled")
        else:
            labels = sorted(
                {
                    self._profile_provider_label(row)
                    for row in profiles
                    if row.get("agent_id") == self._selected_recipient_id()
                }
            )
            allowed = (
                (self._t(DIRECT_LABEL), *labels)
                if labels
                else (self._t(NO_ROUTE_LABEL),)
            )
            self.profile_combo.configure(
                values=allowed,
                state="readonly" if labels else "disabled",
            )
            if self.message_provider_choice.get() not in allowed:
                self._reset_route_selection()
                if not labels:
                    self.message_provider_choice.set(self._t(NO_ROUTE_LABEL))
        self.stats_label.configure(
            text=compact_sidebar_stats(
                online=online,
                total_agents=len(self._library_agents),
                rooms=len(self._rooms) or 1,
                messages=self.snapshot.table_counts["messages"],
                dispatch=dispatch_line,
                memories=self.snapshot.table_counts["memories"],
                open_calls=open_calls,
                active_tasks=active_tasks,
                audit_events=self.snapshot.table_counts["events"],
                sync=utc_text(self.snapshot.generated_utc),
                labels=self._sidebar_stat_labels(),
            ),
            fg=COLORS["amber"] if self.paused.get() else COLORS["muted"],
        )
        self._sync_room_control_states()

    def _draw_agents(self, rows: list[dict[str, Any]]) -> None:
        canvas = self.agent_canvas
        latest = {
            str(row.get("agent_id")): row
            for row in rows
            if row.get("agent_id") and row.get("agent_id") != HUMAN_AGENT_ID
        }
        now = time.time()
        names = sorted(
            latest,
            key=lambda name: (-float(latest[name].get("last_seen_epoch", 0)), name),
        )
        scrollbar = getattr(self, "agent_scrollbar", None)
        if scrollbar is not None:
            if len(names) > AGENT_LIBRARY_VISIBLE_CAPACITY:
                if not scrollbar.winfo_manager():
                    scrollbar.pack(side="right", fill="y", padx=(2, 0))
            elif scrollbar.winfo_manager():
                scrollbar.pack_forget()
        display_rows = []
        for name in names:
            row = latest[name]
            online = bool(
                row.get("online")
                or now - float(row.get("last_seen_epoch", 0)) <= 120
            )
            display_rows.append(
                {
                    "agent_id": name,
                    "online": online,
                    "route": row.get("model_id") or row.get("provider_id"),
                    "selected": self.seat_agent.get() == name,
                }
            )
        canvas_signature = ui_content_signature({"rows": display_rows})
        if canvas_signature == self._last_agent_canvas_signature:
            return
        self._last_agent_canvas_signature = canvas_signature
        canvas.delete("all")
        self._library_hitboxes = []
        if not latest:
            canvas.configure(
                scrollregion=(
                    0,
                    0,
                    AGENT_LIBRARY_CANVAS_WIDTH,
                    AGENT_LIBRARY_CANVAS_HEIGHT,
                )
            )
            canvas.create_text(
                AGENT_LIBRARY_CANVAS_WIDTH // 2,
                AGENT_LIBRARY_CANVAS_HEIGHT // 2,
                text=self._t("sidebar.library_empty"),
                fill=COLORS["muted"],
                font=("Cascadia Mono", 9, "bold"),
            )
            return
        palette = (COLORS["cyan"], COLORS["amber"], COLORS["green"], COLORS["purple"], COLORS["blue"])
        for index, name in enumerate(names):
            column = index % AGENT_LIBRARY_COLUMNS
            row_index = index // AGENT_LIBRARY_COLUMNS
            x = 5 + column * AGENT_LIBRARY_COLUMN_STRIDE
            y = AGENT_LIBRARY_TOP_MARGIN + row_index * AGENT_LIBRARY_CARD_STRIDE
            color = palette[int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:2], 16) % len(palette)]
            row = latest.get(name)
            online = bool(
                row
                and (
                    row.get("online")
                    or now - float(row.get("last_seen_epoch", 0)) <= 120
                )
            )
            shade = color if online else COLORS["line"]
            selected = self.seat_agent.get() == name
            canvas.create_rectangle(
                x,
                y,
                x + AGENT_LIBRARY_CARD_WIDTH,
                y + AGENT_LIBRARY_CARD_HEIGHT,
                fill=COLORS["panel_2"],
                outline=COLORS["amber"] if selected else COLORS["line"],
                width=3 if selected else 1,
            )
            self._library_hitboxes.append(
                (
                    x,
                    y,
                    x + AGENT_LIBRARY_CARD_WIDTH,
                    y + AGENT_LIBRARY_CARD_HEIGHT,
                    name,
                )
            )
            for px, py in ((0, 4), (4, 0), (8, 0), (12, 4), (0, 8), (4, 8), (8, 8), (12, 8), (4, 12), (8, 12)):
                canvas.create_rectangle(x + 7 + px, y + 7 + py, x + 11 + px, y + 11 + py, fill=shade, outline=shade)
            canvas.create_rectangle(x + 11, y + 15, x + 13, y + 17, fill=COLORS["black"], outline="")
            canvas.create_rectangle(x + 17, y + 15, x + 19, y + 17, fill=COLORS["black"], outline="")
            canvas.create_oval(x + 91, y + 7, x + 99, y + 15, fill=COLORS["green"] if online else COLORS["red"], outline=COLORS["black"])
            canvas.create_text(x + 31, y + 16, text=clip(name.upper(), 10), anchor="w", fill=shade, font=("Cascadia Mono", 8, "bold"))
            route = row.get("model_id") or row.get("provider_id") or ("ONLINE" if online else "OFFLINE")
            canvas.create_text(x + 31, y + 35, text=clip(str(route).upper(), 10), anchor="w", fill=COLORS["green"] if online else COLORS["red"], font=("Cascadia Mono", 8))
        row_count = (len(names) + AGENT_LIBRARY_COLUMNS - 1) // AGENT_LIBRARY_COLUMNS
        content_height = max(
            AGENT_LIBRARY_CANVAS_HEIGHT,
            AGENT_LIBRARY_TOP_MARGIN + row_count * AGENT_LIBRARY_CARD_STRIDE,
        )
        canvas.configure(
            scrollregion=(0, 0, AGENT_LIBRARY_CANVAS_WIDTH, content_height)
        )

    def _chat_records(self) -> list[dict[str, Any]]:
        assert self.snapshot is not None
        timeline: list[dict[str, Any]] = []
        dispatch_by_target = {
            (row.get("message_id"), row.get("agent_id")): row
            for row in getattr(self.snapshot, "message_dispatches", ())
        }
        for row in room_messages(self._room_messages, self.selected_room_id):
            route_request = row.get("route_request") or {}
            route_evaluation = row.get("route_evaluation") or {}
            observed = route_evaluation.get("observed") or {}
            dispatch = dispatch_by_target.get(
                (row.get("message_id"), row.get("recipient")), {}
            )
            timeline.append(
                {
                    "kind": "message",
                    "scope": row.get("scope"),
                    "room_id": row.get("room_id", DEFAULT_ROOM_ID),
                    "time": row.get("created_utc"),
                    "sender": row.get("sender"),
                    "recipient": row.get("recipient"),
                    "task": row.get("task_id"),
                    "subject": row.get("subject"),
                    "body": row.get("body"),
                    "sha": row.get("content_sha256"),
                    "artifacts": safe_chat_artifact_labels(
                        row.get("artifact_paths") or ()
                    ),
                    "status": "ACK" if row.get("acknowledged") else "UNREAD",
                    "route_status": (
                        row.get("route_status")
                        or ("requested" if row.get("route_request_sha256") else "not_requested")
                    ),
                    "route_profile": row.get("route_profile_id") or route_request.get("route_profile_id"),
                    "requested_provider": row.get("requested_provider_id") or route_request.get("requested_provider_id"),
                    "requested_model": row.get("requested_model_id") or route_request.get("requested_model_id"),
                    "requested_reasoning": row.get("requested_reasoning_mode") or route_request.get("requested_reasoning_mode"),
                    "observed_provider": observed.get("provider_id"),
                    "observed_model": observed.get("model_id"),
                    "observed_reasoning": observed.get("reasoning_mode"),
                    "dispatch_status": dispatch.get("status"),
                    "dispatch_error": dispatch.get("error_code"),
                    "dispatch_attempts": dispatch.get("attempt_count"),
                    "dispatch_reply_message_id": dispatch.get("reply_message_id"),
                }
            )
        timeline.sort(key=lambda row: row.get("time") or "")
        return timeline

    def _move_chat_view_if_current(self, room_id: str, position: float) -> None:
        if (
            getattr(self, "selected_room_id", "") != room_id
            or getattr(self, "_chat_render_room_id", "") != room_id
        ):
            return
        with contextlib.suppress(AttributeError, tk.TclError):
            self.root.update_idletasks()
        with contextlib.suppress(AttributeError, tk.TclError):
            self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
        self.chat_canvas.yview_moveto(position)

    def _modern_chat_blocks(
        self, rows: Sequence[dict[str, Any]]
    ) -> tuple[tuple[str, Any], ...]:
        subjects_by_task = {
            str(row.get("task") or ""): str(row.get("subject") or "")
            for row in rows
            if str(row.get("sender") or "") == HUMAN_AGENT_ID
            and row.get("task")
        }
        blocks: list[tuple[str, Any]] = []
        index = 0
        while index < len(rows):
            row = rows[index]
            sender = str(row.get("sender") or "")
            task_id = str(row.get("task") or "")
            if sender == HUMAN_AGENT_ID or not task_id:
                blocks.append(("message", row))
                index += 1
                continue
            group = [row]
            cursor = index + 1
            while cursor < len(rows):
                candidate = rows[cursor]
                candidate_sender = str(candidate.get("sender") or "")
                if (
                    candidate_sender == HUMAN_AGENT_ID
                    or str(candidate.get("task") or "") != task_id
                ):
                    break
                group.append(candidate)
                cursor += 1
            if len(group) < 2:
                blocks.append(("message", row))
                index += 1
                continue
            subject = subjects_by_task.get(task_id) or str(
                group[0].get("subject") or task_id
            )
            blocks.append(
                (
                    "round",
                    {
                        "task": task_id,
                        "subject": subject,
                        "rows": tuple(group),
                    },
                )
            )
            index = cursor
        return tuple(blocks)

    def _add_modern_round_group(self, payload: Mapping[str, Any]) -> None:
        rows = tuple(payload.get("rows") or ())
        if not rows:
            return
        line = tk.Frame(self.chat_inner, bg=MODERN_WORKSPACE_BG)
        line.pack(fill="x", padx=28, pady=(9, 7))
        round_card = tk.Frame(
            line,
            bg=MODERN_WORKSPACE_BG,
        )
        round_card.pack(fill="x")

        header = tk.Frame(round_card, bg=MODERN_WORKSPACE_BG)
        header.pack(fill="x", pady=(9, 4))
        title_group = tk.Frame(header, bg=MODERN_WORKSPACE_BG)
        title_group.pack(side="left", fill="x", expand=True)
        tk.Frame(title_group, bg=COLORS["blue"], width=3, height=20).pack(
            side="left", padx=(0, 9)
        )
        tk.Label(
            title_group,
            text=self._t("modern.round.title"),
            bg=MODERN_WORKSPACE_BG,
            fg=COLORS["text"],
            anchor="w",
            font=(MODERN_FONT_FAMILY, 11, "bold"),
        ).pack(side="left")
        tk.Label(
            title_group,
            text=self._t("modern.round.response_count").format(count=len(rows)),
            bg=MODERN_NAV_ACTIVE_BG,
            fg=COLORS["blue"],
            padx=7,
            pady=2,
            font=(MODERN_FONT_FAMILY, 8, "bold"),
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            header,
            text=utc_text(rows[-1].get("time")).rsplit(" ", 1)[-1][:5],
            bg=MODERN_WORKSPACE_BG,
            fg=COLORS["muted"],
            font=(MODERN_FONT_FAMILY, 8),
        ).pack(side="right")

        subject = clip(str(payload.get("subject") or payload.get("task") or ""), 120)
        tk.Label(
            round_card,
            text=self._t("modern.round.summary").format(
                count=len(rows),
                subject=subject,
            ),
            bg=MODERN_WORKSPACE_BG,
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=760,
            font=(MODERN_FONT_FAMILY, 9),
        ).pack(fill="x", padx=(12, 0), pady=(0, 10))

        responses = tk.Frame(round_card, bg=MODERN_WORKSPACE_BG)
        responses.pack(fill="x", pady=(0, 5))
        response_width = max(420, int(self.chat_canvas.winfo_width()) - 150)
        for index, row in enumerate(rows):
            sender = str(row.get("sender") or "unknown")
            sender_name, initials, agent_tint, avatar_color = (
                self._modern_agent_presentation(sender)
            )
            response_row = tk.Frame(responses, bg=MODERN_WORKSPACE_BG)
            response_row.pack(fill="x", pady=(0, 7))
            avatar = tk.Label(
                response_row,
                text=initials[:2],
                bg=avatar_color,
                fg="#ffffff",
                width=3,
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            )
            avatar.pack(side="left", anchor="n", padx=(0, 9), pady=(3, 0), ipady=5)
            response = tk.Frame(
                response_row,
                bg=COLORS["panel"],
                highlightthickness=1,
                highlightbackground=COLORS["line"],
            )
            response.pack(side="left", fill="x", expand=True)
            response_header = tk.Frame(response, bg=COLORS["panel"])
            response_header.pack(fill="x", padx=13, pady=(9, 4))
            tk.Label(
                response_header,
                text=sender_name,
                bg=COLORS["panel"],
                fg=COLORS["text"],
                font=(MODERN_FONT_FAMILY, 9, "bold"),
            ).pack(side="left")
            route_values = (
                row.get("observed_provider") or row.get("requested_provider"),
                row.get("observed_model") or row.get("requested_model"),
            )
            route_text = " / ".join(
                str(value) for value in route_values if value
            )
            if route_text:
                tk.Label(
                    response_header,
                    text=clip(route_text, 42),
                    bg=COLORS["panel"],
                    fg=COLORS["muted"],
                    font=(MODERN_FONT_FAMILY, 8),
                ).pack(side="left", padx=(8, 0))
            state_text = (
                self._t("modern.agent.failed")
                if str(row.get("dispatch_status") or "").lower() == "failed"
                else self._t("modern.agent.replied")
            )
            state_color = (
                COLORS["red"]
                if str(row.get("dispatch_status") or "").lower() == "failed"
                else COLORS["green"]
            )
            tk.Label(
                response_header,
                text=state_text,
                bg=COLORS["panel"],
                fg=state_color,
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            ).pack(side="right")
            response_body = tk.Frame(response, bg=COLORS["panel"])
            response_body.pack(fill="both", expand=True, padx=13, pady=(0, 9))
            tk.Label(
                response_body,
                text=clip(str(row.get("body") or ""), 520),
                bg=COLORS["panel"],
                fg=COLORS["text"],
                anchor="nw",
                justify="left",
                wraplength=max(330, response_width - 96),
                font=(MODERN_FONT_FAMILY, 10),
            ).pack(fill="x")
            receipt_text = str(row.get("sha") or "")[:10] or "--"
            tk.Label(
                response_body,
                text=self._t("modern.round.receipt").format(sha=receipt_text),
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8),
            ).pack(fill="x", pady=(7, 0))

        activity = tk.Frame(
            round_card,
            bg=COLORS["panel_2"],
            highlightthickness=0,
        )
        details = tk.Frame(round_card, bg=MODERN_WORKSPACE_BG)
        details_visible = tk.BooleanVar(value=False)

        def toggle_activity() -> None:
            details_visible.set(not details_visible.get())
            if details_visible.get():
                details.pack(fill="x", padx=(40, 0), pady=(0, 7), before=activity)
            else:
                details.pack_forget()
            activity_button.configure(
                text=("▴  " if details_visible.get() else "▾  ")
                + self._t(
                    "modern.round.hide_activity"
                    if details_visible.get()
                    else "modern.round.activity"
                ).format(count=len(rows))
            )

        for row in rows:
            sender = str(row.get("sender") or "unknown")
            sender_name, _initials, _agent_tint, _avatar_color = (
                self._modern_agent_presentation(sender)
            )
            detail_row = tk.Frame(details, bg=MODERN_WORKSPACE_BG)
            detail_row.pack(fill="x", pady=3)
            tk.Label(
                detail_row,
                text=sender_name,
                width=13,
                bg=MODERN_WORKSPACE_BG,
                fg=COLORS["text"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            ).pack(side="left")
            tk.Label(
                detail_row,
                text=clip(self._bubble_metadata(row).replace("\n", "  ·  "), 150),
                bg=MODERN_WORKSPACE_BG,
                fg=COLORS["muted"],
                anchor="w",
                font=(MODERN_FONT_FAMILY, 8),
            ).pack(side="left", fill="x", expand=True)
            sha = str(row.get("sha") or "")
            tk.Button(
                detail_row,
                text=self._t("chat.copy_sha"),
                command=lambda value=sha: self.copy_text(value),
                bg=MODERN_WORKSPACE_BG,
                fg=COLORS["muted"],
                activebackground=COLORS["panel_2"],
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                font=(MODERN_FONT_FAMILY, 7),
            ).pack(side="right")

        if details_visible.get():
            details.pack(fill="x", padx=(40, 0), pady=(0, 7))
        activity.pack(fill="x", padx=(40, 0), pady=(1, 10))
        activity_button = tk.Button(
            activity,
            text=("▴  " if details_visible.get() else "▾  ")
            + self._t(
                "modern.round.hide_activity"
                if details_visible.get()
                else "modern.round.activity"
            ).format(count=len(rows)),
            command=toggle_activity,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            activebackground=MODERN_NAV_ACTIVE_BG,
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            anchor="w",
            padx=10,
            pady=7,
            font=(MODERN_FONT_FAMILY, 8, "bold"),
        )
        activity_button.pack(side="left", fill="x", expand=True)
        linked_receipts = sum(1 for row in rows if str(row.get("sha") or ""))
        tk.Label(
            activity,
            text=self._t("modern.round.linked").format(
                linked=linked_receipts,
                total=len(rows),
            ),
            bg=COLORS["panel_2"],
            fg=COLORS["green"] if linked_receipts == len(rows) else COLORS["amber"],
            font=(MODERN_FONT_FAMILY, 8, "bold"),
        ).pack(side="right", padx=(8, 10))

    def _render_chat(self, query: str) -> None:
        rows = [row for row in self._chat_records() if self._match(row, query)]
        row_signatures = tuple(ui_content_signature(row) for row in rows)
        previous_signatures = self._chat_render_row_signatures
        render_room_id = getattr(self, "selected_room_id", "")
        same_context = (
            query == self._chat_render_query
            and render_room_id == getattr(self, "_chat_render_room_id", "")
        )
        mode = incremental_render_mode(
            previous_signatures,
            row_signatures,
            same_context=same_context,
        )
        follow_latest = bool(getattr(self, "_chat_follow_latest_on_open", False))
        if mode == "unchanged":
            if follow_latest:
                self._chat_follow_latest_on_open = False
                self.root.after_idle(
                    lambda room_id=render_room_id: self._move_chat_view_if_current(
                        room_id, 1.0
                    )
                )
            return

        viewport = self.chat_canvas.yview()
        was_at_bottom = not viewport or viewport[-1] >= 0.985
        if mode == "append" and ACTIVE_THEME != "modern":
            previous_count = len(previous_signatures or ())
            for row in rows[previous_count:]:
                self._add_bubble(row)
            self._chat_render_row_signatures = row_signatures
            self._chat_render_query = query
            self._chat_render_room_id = render_room_id
            if was_at_bottom or follow_latest:
                self._chat_follow_latest_on_open = False
                self.root.after_idle(
                    lambda room_id=render_room_id: self._move_chat_view_if_current(
                        room_id, 1.0
                    )
                )
            return

        for child in self.chat_inner.winfo_children():
            child.destroy()
        if not rows:
            tk.Label(
                self.chat_inner,
                text=self._t("chat.no_messages"),
                bg=(
                    MODERN_WORKSPACE_BG
                    if ACTIVE_THEME == "modern"
                    else COLORS["panel"]
                ),
                fg=COLORS["muted"],
                font=(UI_FONT_FAMILY, 12, "bold"),
            ).pack(pady=50)
            self._chat_render_row_signatures = row_signatures
            self._chat_render_query = query
            self._chat_render_room_id = render_room_id
            self._chat_follow_latest_on_open = False
            self.root.after_idle(
                lambda room_id=render_room_id: self._move_chat_view_if_current(
                    room_id, 0.0
                )
            )
            return
        if ACTIVE_THEME == "modern":
            for kind, payload in self._modern_chat_blocks(rows):
                if kind == "round":
                    self._add_modern_round_group(payload)
                else:
                    self._add_bubble(payload)
        else:
            for row in rows:
                self._add_bubble(row)
        self._chat_render_row_signatures = row_signatures
        self._chat_render_query = query
        self._chat_render_room_id = render_room_id
        filled_empty_context = (
            same_context and previous_signatures == () and bool(row_signatures)
        )
        target = (
            1.0
            if (
                previous_signatures is None
                or was_at_bottom
                or not same_context
                or filled_empty_context
                or follow_latest
            )
            else viewport[0]
        )
        self._chat_follow_latest_on_open = False
        self.root.after_idle(
            lambda room_id=render_room_id, position=target: (
                self._move_chat_view_if_current(room_id, position)
            )
        )

    def _dispatch_status_text(self, row: Mapping[str, Any]) -> str:
        status = str(row.get("dispatch_status") or "").strip().lower()
        if not status:
            return ""
        agent = str(row.get("recipient") or self._t("cockpit.unavailable"))
        attempts = int(row.get("dispatch_attempts") or 0)
        error = str(row.get("dispatch_error") or "").strip().lower()
        error_key = {
            "credential_unavailable": "chat.delivery.credential_unavailable",
            "provider_authentication_required": "chat.delivery.credential_unavailable",
            "provider_http_retryable": "chat.delivery.provider_unavailable",
            "provider_rate_limited": "chat.delivery.rate_limited",
            "runner_hard_deadline_exceeded": "chat.delivery.runner_hard_deadline_exceeded",
            "discussion_timed_out": "chat.delivery.discussion_timed_out",
            "route_mismatch": "chat.delivery.route_mismatch",
            "route_handoff_mismatch": "chat.delivery.route_mismatch",
            "mcp_transport_failed": "chat.delivery.transport_failed",
            "tool_policy_failed": "chat.delivery.tool_failed",
        }.get(error)
        if error_key and status in {"retryable", "failed"}:
            return self._t(error_key).format(agent=agent, attempts=attempts)
        status_key = {
            "claimed": "chat.delivery.running",
            "retryable": "chat.delivery.retrying",
            "completed": "chat.delivery.completed",
            "failed": "chat.delivery.failed",
        }.get(status, "chat.delivery.unknown")
        return self._t(status_key).format(agent=agent, attempts=attempts)

    @staticmethod
    def _dispatch_status_color(row: Mapping[str, Any]) -> str:
        status = str(row.get("dispatch_status") or "").strip().lower()
        return {
            "completed": COLORS["green"],
            "failed": COLORS["red"],
            "retryable": COLORS["amber"],
            "claimed": COLORS["cyan"],
        }.get(status, COLORS["muted"])

    def _add_bubble(self, row: dict[str, Any]) -> None:
        if ACTIVE_THEME == "modern":
            self._add_modern_bubble(row)
            return
        sender = row.get("sender") or "unknown"
        own = sender == "human-operator"
        bubble_color = COLORS["panel_2"] if own else "#2b261a"
        accent = COLORS["cyan"] if own else COLORS["amber"]
        line = tk.Frame(self.chat_inner, bg=COLORS["panel"])
        line.pack(fill="x", padx=16, pady=7)
        bubble = tk.Frame(line, bg=bubble_color, bd=2, relief="ridge")
        wraplength, opposite_padding = chat_bubble_metrics(
            self.chat_canvas.winfo_width()
        )
        bubble.pack(
            side="right" if own else "left",
            fill="x",
            expand=False,
            padx=(opposite_padding, 0) if own else (0, opposite_padding),
        )
        header = tk.Frame(bubble, bg=accent)
        header.pack(fill="x")
        tk.Label(
            header,
            text=f"{sender}  >  {row.get('recipient') or '*'}",
            bg=accent,
            fg=COLORS["black"],
            font=("Cascadia Mono", 9, "bold"),
        ).pack(side="left", padx=8, pady=4)
        tk.Label(header, text=utc_text(row.get("time")), bg=accent, fg=COLORS["black"], font=("Cascadia Mono", 8)).pack(side="right", padx=8)
        tk.Label(
            bubble,
            text=self._bubble_metadata(row),
            justify="left",
            anchor="w",
            wraplength=wraplength,
            bg=bubble_color,
            fg=COLORS["purple"],
            font=("Cascadia Mono", 8, "bold"),
        ).pack(fill="x", padx=10, pady=(8, 4))
        delivery_text = self._dispatch_status_text(row)
        if delivery_text:
            tk.Label(
                bubble,
                text=delivery_text,
                justify="left",
                anchor="w",
                wraplength=wraplength,
                bg=bubble_color,
                fg=self._dispatch_status_color(row),
                font=("Cascadia Mono", 9, "bold"),
            ).pack(fill="x", padx=10, pady=(2, 4))
        tk.Label(
            bubble,
            text=row.get("body") or "",
            justify="left",
            anchor="w",
            wraplength=wraplength,
            bg=bubble_color,
            fg=COLORS["text"],
            font=("Cascadia Mono", 10),
        ).pack(fill="x", padx=10, pady=5)
        footer = tk.Frame(bubble, bg=bubble_color)
        footer.pack(fill="x", padx=10, pady=(4, 8))
        sha = row.get("sha") or ""
        tk.Label(footer, text=f"SHA {sha[:16] or '--'}", bg=bubble_color, fg=COLORS["muted"], font=("Cascadia Mono", 8)).pack(side="left")
        tk.Button(
            footer,
            text=self._t("chat.copy_sha"),
            command=lambda value=sha: self.copy_text(value),
            bg=COLORS["line"],
            fg=COLORS["text"],
            activebackground=accent,
            activeforeground=COLORS["black"],
            relief="raised",
            bd=1,
            font=("Cascadia Mono", 7, "bold"),
        ).pack(side="right")

    def _add_modern_bubble(self, row: dict[str, Any]) -> None:
        sender = str(row.get("sender") or "unknown")
        own = sender == "human-operator"
        sender_name, initials, agent_tint, avatar_color = (
            self._modern_agent_presentation(sender)
        )
        bubble_color = MODERN_USER_BUBBLE_BG if own else MODERN_CARD_BG
        line = tk.Frame(self.chat_inner, bg=MODERN_WORKSPACE_BG)
        line.pack(fill="x", padx=48, pady=4)
        wraplength, opposite_padding = chat_bubble_metrics(
            self.chat_canvas.winfo_width()
        )
        content = tk.Frame(line, bg=MODERN_WORKSPACE_BG)
        content.pack(
            side="right" if own else "left",
            fill="x",
            expand=True,
            padx=(opposite_padding, 0) if own else (0, opposite_padding),
        )
        if not own:
            tk.Label(
                content,
                text=initials[:2],
                bg=avatar_color,
                fg="#ffffff",
                width=3,
                height=1,
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            ).pack(side="left", anchor="n", padx=(0, 9), pady=(2, 0), ipady=4)
        bubble = tk.Frame(
            content,
            bg=bubble_color,
            bd=0,
            highlightthickness=1 if own else 0,
            highlightbackground=COLORS["line"],
        )
        bubble.pack(
            side="right" if own else "left",
            fill="x",
            expand=True,
        )
        header = tk.Frame(bubble, bg=bubble_color)
        header.pack(fill="x", padx=13, pady=(7, 1))
        tk.Label(
            header,
            text=self._t("modern.message.you") if own else sender_name,
            bg=bubble_color,
            fg=COLORS["text"],
            font=(MODERN_FONT_FAMILY, 10, "bold"),
        ).pack(side="left")
        timestamp = utc_text(row.get("time"))
        if " " in timestamp:
            timestamp = timestamp.rsplit(" ", 1)[-1][:5]
        tk.Label(
            header,
            text=timestamp,
            bg=bubble_color,
            fg=COLORS["muted"],
            font=(MODERN_FONT_FAMILY, 8),
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            bubble,
            text=row.get("body") or "",
            justify="left",
            anchor="w",
            wraplength=wraplength,
            bg=bubble_color,
            fg=COLORS["text"],
            font=(MODERN_FONT_FAMILY, 10),
        ).pack(fill="x", padx=13, pady=(3, 6))

        details = tk.Frame(bubble, bg=bubble_color)
        tk.Label(
            details,
            text=self._bubble_metadata(row),
            justify="left",
            anchor="w",
            wraplength=wraplength,
            bg=bubble_color,
            fg=COLORS["muted"],
            font=(MODERN_FONT_FAMILY, 8),
        ).pack(fill="x")
        delivery_text = self._dispatch_status_text(row)
        if delivery_text:
            tk.Label(
                details,
                text=delivery_text,
                justify="left",
                anchor="w",
                wraplength=wraplength,
                bg=bubble_color,
                fg=self._dispatch_status_color(row),
                font=(MODERN_FONT_FAMILY, 8, "bold"),
            ).pack(fill="x", pady=(3, 0))
        sha = str(row.get("sha") or "")
        tk.Label(
            details,
            text=f"SHA {sha[:16] or '--'}",
            bg=bubble_color,
            fg=COLORS["muted"],
            anchor="w",
            font=(MODERN_FONT_FAMILY, 8),
        ).pack(fill="x", pady=(3, 0))

        actions = tk.Frame(bubble, bg=bubble_color)
        actions.pack(fill="x", padx=11, pady=(0, 6))
        details_visible = tk.BooleanVar(value=False)

        def toggle_details() -> None:
            details_visible.set(not details_visible.get())
            if details_visible.get():
                details.pack(fill="x", padx=14, pady=(0, 8), before=actions)
            else:
                details.pack_forget()
            details_button.configure(
                text=self._t(
                    "modern.message.hide_details"
                    if details_visible.get()
                    else "modern.message.details"
                )
            )

        details_button = tk.Button(
            actions,
            text=self._t("modern.message.details"),
            command=toggle_details,
            bg=bubble_color,
            fg=COLORS["muted"],
            activebackground=bubble_color,
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            padx=2,
            font=(MODERN_FONT_FAMILY, 8),
        )
        details_button.pack(side="left")
        tk.Button(
            actions,
            text=self._t("chat.copy_sha"),
            command=lambda value=sha: self.copy_text(value),
            bg=bubble_color,
            fg=COLORS["muted"],
            activebackground=bubble_color,
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            padx=2,
            font=(MODERN_FONT_FAMILY, 8),
        ).pack(side="right")

    def _bubble_metadata(self, row: dict[str, Any]) -> str:
        text = (
            f"[{row.get('status')}]  {row.get('subject') or row.get('kind')}\n"
            f"{self._t('chat.metadata.scope')}: {row.get('scope') or '--'}"
            f"  // {self._t('chat.metadata.task')}: {row.get('task') or '--'}"
        )
        if row.get("route_status") and row.get("route_status") != "not_requested":
            requested = "/".join(
                str(value)
                for value in (
                    row.get("requested_provider"),
                    row.get("requested_model"),
                    row.get("requested_reasoning"),
                )
                if value
            )
            observed = "/".join(
                str(value)
                for value in (
                    row.get("observed_provider"),
                    row.get("observed_model"),
                    row.get("observed_reasoning"),
                )
                if value
            )
            text += f"\nROUTE: {str(row.get('route_status')).upper()}  {requested or '--'}"
            if observed:
                text += f"  // OBSERVED {observed}"
        if row.get("dispatch_status"):
            text += f"\nDELIVERY: {str(row.get('dispatch_status')).upper()}"
            if row.get("dispatch_error"):
                text += f"  // ERROR: {row.get('dispatch_error')}"
            if row.get("dispatch_attempts") is not None:
                text += f"  // ATTEMPTS: {row.get('dispatch_attempts')}"
        if row.get("artifacts"):
            text += "\nATTACH: " + " | ".join(str(value) for value in row["artifacts"])
        return text

    def _render_work(self, query: str) -> None:
        assert self.snapshot is not None
        latest_updates: dict[str, dict[str, Any]] = {}
        for row in self.snapshot.work_updates:
            latest_updates.setdefault(row["task_id"], row)
        rows = []
        for task in self.snapshot.tasks:
            update = latest_updates.get(task["task_id"], {})
            record = dict(task)
            record["latest_update"] = update
            if not self._match(record, query):
                continue
            rows.append(
                (
                    task["task_id"],
                    (
                        task["task_id"],
                        task.get("claimed_by") or task.get("owner") or "--",
                        update.get("status") or task.get("status") or "--",
                        clip(update.get("summary") or task.get("summary"), 110),
                        utc_text(update.get("created_utc") or task.get("updated_utc")),
                    ),
                    record,
                )
            )
        self.work_tree.replace(rows)

    def _render_reviews(self, query: str) -> None:
        assert self.snapshot is not None
        rows = []
        for row in self.snapshot.peer_calls:
            if row.get("status") != "open" or not self._match(row, query):
                continue
            rows.append(
                (
                    f"request-{row['request_id']}",
                    (
                        row.get("request_id"),
                        row.get("recipient"),
                        "PENDING",
                        "--",
                        clip(row.get("question"), 120),
                        utc_text(row.get("request_utc")),
                    ),
                    {**row, "record_kind": "manual_review_request"},
                )
            )
        for row in self.snapshot.peer_reviews:
            if not self._match(row, query):
                continue
            rows.append(
                (
                    row["review_id"],
                    (
                        row.get("request_id"),
                        row.get("reviewer"),
                        row.get("verdict"),
                        row.get("score"),
                        clip(row.get("findings"), 120),
                        utc_text(row.get("review_utc")),
                    ),
                    row,
                )
            )
        self.review_tree.replace(rows)

    def _render_changes(self, query: str) -> None:
        assert self.snapshot is not None
        rows = []
        for row in self.snapshot.changes:
            if not self._match(row, query):
                continue
            rows.append(
                (
                    row["record_id"],
                    (
                        row.get("actor"),
                        row.get("task_id"),
                        clip(row.get("change_summary"), 150),
                        utc_text(row.get("recorded_utc")),
                        (row.get("record_sha256") or "")[:12],
                    ),
                    row,
                )
            )
        self.change_tree.replace(rows)

    def _render_audit(self, query: str) -> None:
        assert self.snapshot is not None
        rows = []
        for row in self.snapshot.events:
            if not self._match(row, query):
                continue
            rows.append(
                (
                    row["event_id"],
                    (
                        row.get("actor"),
                        row.get("event_type"),
                        row.get("task_id") or "--",
                        clip(row.get("payload_json"), 130),
                        utc_text(row.get("created_utc")),
                    ),
                    row,
                )
            )
        self.audit_tree.replace(rows)

    def _render_connections(self, query: str) -> None:
        assert self.snapshot is not None
        rows = []
        for row in self.snapshot.provider_connections:
            if row.get("scope") != self.scope or not self._match(row, query):
                continue
            rows.append(
                (
                    row["connection_id"],
                    (
                        row.get("connection_id"),
                        row.get("display_name"),
                        row.get("route_class"),
                        row.get("secret_backend"),
                        "YES" if row.get("enabled") else "NO",
                        (row.get("connection_sha256") or "")[:12],
                    ),
                    row,
                )
            )
        self.connection_tree.replace(rows)

    def _render_memories(self, query: str) -> None:
        assert self.snapshot is not None
        rows = []
        superseded_by = {
            str(item.get("supersedes_memory_id")): str(item.get("memory_id"))
            for item in self.snapshot.memories
            if item.get("supersedes_memory_id")
        }
        for row in self.snapshot.memories:
            if row.get("scope") != self.scope or not self._match(row, query):
                continue
            record = dict(row)
            record["artifact_bindings"] = safe_json(
                record.pop("artifact_bindings_json", "[]"), []
            )
            applicability = safe_json(record.pop("applicability_json", "[]"), [])
            if not isinstance(applicability, list):
                applicability = []
            record["applicability"] = applicability
            record["superseded_by_memory_id"] = superseded_by.get(
                str(row.get("memory_id"))
            )
            supersession = row.get("supersedes_memory_id") or record.get(
                "superseded_by_memory_id"
            )
            rows.append(
                (
                    row["memory_id"],
                    (
                        str(row.get("record_type") or "FACT").upper(),
                        str(row.get("visibility") or "--").upper(),
                        row.get("authority_id")
                        or row.get("owner_agent_id")
                        or "--",
                        ", ".join(str(value) for value in applicability) or "--",
                        str(row.get("status") or "--").upper(),
                        clip(row.get("title"), 90),
                        clip(supersession, 80) if supersession else "--",
                        utc_text(row.get("created_utc")),
                    ),
                    record,
                )
            )
        self.memory_tree.replace(rows)

    def _render_usage(self, query: str) -> None:
        assert self.snapshot is not None
        period_key = self.usage_period.get()
        period = self.snapshot.usage_periods.get(period_key)
        if not isinstance(period, Mapping):
            period_key = "all"
            period = {
                "totals": self.snapshot.usage_totals,
                "by_provider": self.snapshot.usage_by_provider,
                "by_model": self.snapshot.usage_by_model,
                "model_totals": self.snapshot.usage_model_totals,
                "trend": self.snapshot.usage_daily,
                "granularity": "day",
                "trend_truncated": False,
            }
        totals = period.get("totals") or {}
        statuses = totals.get("dispatch_statuses") or {}
        provider_calls = int(totals.get("provider_calls") or 0)
        reported_calls = int(totals.get("total_tokens_reported_calls") or 0)
        coverage = (
            f"{reported_calls / provider_calls:.0%}  {reported_calls:,}/{provider_calls:,}"
            if provider_calls
            else "0%  0/0"
        )
        self.usage_kpi_values["total_tokens"].set(
            self._usage_number(totals.get("total_tokens"))
        )
        self.usage_kpi_values["input_tokens"].set(
            self._usage_number(totals.get("input_tokens"))
        )
        self.usage_kpi_values["output_tokens"].set(
            self._usage_number(totals.get("output_tokens"))
        )
        self.usage_kpi_values["coverage"].set(coverage)
        self.usage_kpi_values["dispatches"].set(
            f"{int(totals.get('completed_dispatches') or 0):,} / "
            f"{int(statuses.get('failed') or 0):,}"
        )

        model_rows = tuple(
            row
            for row in period.get("by_model") or ()
            if self._match(row, query)
        )
        provider_rows = tuple(
            row
            for row in period.get("by_provider") or ()
            if self._match(row, query)
        )
        self._render_usage_provider_chart(provider_rows)
        self.usage_tree.delete(*self.usage_tree.get_children())
        for index, row in enumerate(model_rows):
            calls = int(row.get("provider_calls") or 0)
            reported = int(row.get("total_tokens_reported_calls") or 0)
            self.usage_tree.insert(
                "",
                "end",
                iid=f"usage-{index}",
                values=(
                    row.get("provider_id") or "--",
                    row.get("model_id") or "--",
                    self._usage_number(calls),
                    f"{reported:,}/{calls:,}",
                    self._usage_number_with_coverage(row, "input_tokens"),
                    self._usage_number_with_coverage(row, "output_tokens"),
                    self._usage_number_with_coverage(row, "total_tokens"),
                ),
            )
        self._usage_daily_rows = tuple(period.get("trend") or ())
        self._usage_trend_limit = {
            "hour": 24,
            "day": 30,
            "month": 240,
        }.get(str(period.get("granularity") or "day"), 30)
        self._usage_model_rows = tuple(
            row
            for row in period.get("model_totals") or ()
            if self._match(row, query)
        )
        unavailable = int(totals.get("unavailable_dispatches") or 0)
        partial = int(totals.get("partial_dispatches") or 0)
        derived = int(totals.get("derived_total_dispatches") or 0)
        self.usage_note_label.configure(
            text=(
                f"{self._t('usage.note_period').format(period=self._t(f'usage.period.{period_key}'))}  // "
                f"{self._t('usage.unavailable')} {unavailable:,}  // "
                f"{self._t('usage.partial')} {partial:,}  // "
                f"{self._t('usage.derived_total')} {derived:,}"
                + (
                    f"  // {self._t('usage.trend_truncated')}"
                    if period.get("trend_truncated")
                    else ""
                )
            )
        )
        self._sync_usage_section_titles()
        self._draw_usage_charts()

    def copy_text(self, value: str) -> None:
        if not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self._closing = True
            self._ui_generation += 1
            self.verification_engine.close()
            self.room_discussion_tracker.close()
            self.workflow_runner.close()
            self.cockpit.close()
            self.reader.close()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._ui_generation += 1
        if self._refresh_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        if self._ui_pump_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._ui_pump_after_id)
            self._ui_pump_after_id = None
        if getattr(self, "_announcement_after_id", None) is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._announcement_after_id)
            self._announcement_after_id = None
        if getattr(self, "_feedback_reflow_after_id", None) is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._feedback_reflow_after_id)
            self._feedback_reflow_after_id = None
        if getattr(self, "_window_icon_after_id", None) is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._window_icon_after_id)
            self._window_icon_after_id = None
        if getattr(self, "_announcement_window", None) is not None:
            with contextlib.suppress(tk.TclError):
                self._announcement_window.destroy()
            self._announcement_window = None
        cockpit = getattr(self, "cockpit", None)
        workflow_runner = getattr(self, "workflow_runner", None)
        room_discussion_tracker = getattr(self, "room_discussion_tracker", None)
        verification_engine = getattr(self, "verification_engine", None)
        if verification_engine is not None:
            verification_engine.close()
        if workflow_runner is not None:
            workflow_runner.close()
        if room_discussion_tracker is not None:
            room_discussion_tracker.close()
        if cockpit is not None:
            cockpit.close()
        self.reader.close()
        with contextlib.suppress(tk.TclError):
            self.root.destroy()
        release_windows_icon_handles(getattr(self, "_windows_icon_handles", ()))
        self._windows_icon_handles = ()

    def ui_self_test(self) -> dict[str, Any]:
        # Exercise real mapped geometry without presenting or activating a
        # desktop window. The constructor withdrew the root before any layout
        # update; an alpha-zero off-screen override window keeps map-dependent
        # checks meaningful without taskbar or foreground flashes.
        self.root.withdraw()
        with contextlib.suppress(tk.TclError):
            self.root.attributes("-alpha", 0.0)
            self.root.attributes("-topmost", False)
            self.root.overrideredirect(True)
            if sys.platform == "win32":
                self.root.attributes("-toolwindow", True)
        self.root.geometry("980x650-32000-32000")
        self.root.deiconify()
        self.root.update()
        checks: dict[str, bool] = {}
        actual_tk_scaling = float(self.root.tk.call("tk", "scaling"))
        configured_scale = self.ui_scale_factor
        expected_tk_scaling = (
            tk_scaling_for_windows_factor(configured_scale)
            if configured_scale is not None
            else None
        )
        checks["minimum_window_geometry"] = (
            self.root.winfo_width() == 980 and self.root.winfo_height() == 650
        )
        checks["configured_ui_scale"] = (
            expected_tk_scaling is None
            or abs(actual_tk_scaling - expected_tk_scaling) <= 0.02
        )
        titles = {page_key: self._t(f"page.{page_key}") for page_key in self.pages}
        modern_more_was_visible = self.modern_more_nav_visible
        if self.theme.get() == "modern" and not modern_more_was_visible:
            self._toggle_modern_more_nav()
            self.root.update_idletasks()
        sidebar_visibility: list[bool] = []
        for key, button in self.nav_buttons.items():
            sidebar_visibility.append(self._reveal_sidebar_widget(button))
            button.invoke()
            self.root.update_idletasks()
            expected_title = titles[key]
            if self.theme.get() == "modern" and key == "chat":
                room_id = str(getattr(self, "selected_room_id", "") or "")
                room = getattr(self, "_rooms", {}).get(room_id, {})
                expected_title = clip(
                    str(
                        room.get("name")
                        or room_id
                        or self._t("modern.rooms.unnamed")
                    ),
                    52,
                )
            checks[key] = (
                self.active_page == key
                and self.page_title.cget("text") == expected_title
            )
        sidebar_view = self.sidebar_canvas.yview()
        sidebar_overflows = (
            self.sidebar_content.winfo_reqheight() > self.sidebar_canvas.winfo_height()
        )
        checks["sidebar_navigation_visible"] = (
            all(sidebar_visibility)
            and self.sidebar_scrollbar.winfo_ismapped()
            and bool(self.sidebar_scrollbar.cget("command"))
            and (
                self.theme.get() == "modern"
                or (
                    self.sidebar_scroll_hint.winfo_ismapped()
                    and bool(self.sidebar_scroll_hint.cget("text"))
                )
            )
            and (not sidebar_overflows or sidebar_view[1] - sidebar_view[0] < 1.0)
        )
        if self.theme.get() == "modern" and not modern_more_was_visible:
            self._toggle_modern_more_nav()
            self.root.update_idletasks()
        checks["sidebar_readability"] = (
            SIDEBAR_WIDTH >= 300
            and self.sidebar_scrollbar.winfo_class() == "TScrollbar"
            and self.agent_scrollbar.winfo_class() == "TScrollbar"
            and self.sidebar_scrollbar.cget("style") == SIDEBAR_SCROLLBAR_STYLE
            and not self.agent_scrollbar.cget("style")
            and ttk.Style(self.root).lookup(
                SIDEBAR_SCROLLBAR_STYLE, "background", ("active",)
            )
            == COLORS["line"]
            and ttk.Style(self.root).lookup(
                SIDEBAR_SCROLLBAR_STYLE, "troughcolor"
            )
            == COLORS["panel"]
            and SIDEBAR_TEXT_SIZE >= 9
            and TUTORIAL_BODY_TEXT_SIZE >= 11
        )
        checks["text_editing_shortcuts"] = all(
            bool(self.root.bind_class(widget_class, sequence))
            for widget_class in ("Entry", "TEntry", "Text", "TCombobox")
            for sequence in (
                "<Control-a>",
                "<Control-c>",
                "<Control-v>",
                "<Control-x>",
                "<Button-3>",
            )
        ) and all(
            self._t(key)
            for key in ("edit.cut", "edit.copy", "edit.paste", "edit.select_all")
        )
        self.sidebar_canvas.yview_moveto(0.0)
        cockpit_checks = self.cockpit.self_test()
        checks["cockpit_controls"] = all(cockpit_checks.values())
        checks["cockpit_launch_horizontal_bounds"] = bool(
            cockpit_checks.get("launch_horizontal_bounds")
        )
        checks["trust_workflows"] = all(
            self.trust_workflows.self_test().values()
        )
        recipient = self.message_recipient.get()
        recipient_values = tuple(self.recipient_combo.cget("values"))
        provider_values = tuple(self.profile_combo.cget("values"))
        provider_choice = self.message_provider_choice.get()
        checks["composer"] = (
            bool(recipient)
            and recipient in recipient_values
            and self.message_priority.get() == "normal"
            and self.message_route_profile.get() == "DIRECT"
            and bool(provider_choice)
            and provider_choice in provider_values
            and (
                (
                    self._catalog_value_matches(recipient, BROADCAST_LABEL)
                    and self._catalog_value_matches(
                        provider_choice, BROADCAST_ROUTE_LABEL
                    )
                )
                or (
                    not self._catalog_value_matches(recipient, BROADCAST_LABEL)
                    and any(
                        self._catalog_value_matches(provider_choice, key)
                        for key in (DIRECT_LABEL, NO_ROUTE_LABEL)
                    )
                )
            )
            and self.message_provider.get() == ""
            and self.message_model.get() == ""
            and self.message_reasoning.get() == ""
            and "SOL" not in self.reasoning_combo.cget("values")
            and "LUNA" not in self.reasoning_combo.cget("values")
            and self.send_button.cget("state") == "normal"
            and self.chat_attach_button.cget("state") == "normal"
            and self.chat_clear_attachments_button.cget("state") == "disabled"
            and self.chat_attachment_status.get() == self._t("chat.no_attachments")
            and bool(self.chat_attachment_note.cget("text"))
            and self.manage_sources_button.cget("state") == "normal"
        )
        checks["secret_plane"] = (
            self.connection_key_entry.cget("show") == "*"
            and self.connection_api_key.get() == ""
            and not self.connection_key_visible.get()
            and self.connection_key_visibility_button.cget("text")
            == self._t("provider.show_api_key")
            and self.connection_endpoint.get() == ""
            and self.feedback_key_entry.cget("show") == "*"
            and self.feedback_key.get() == ""
            and self.feedback_key_entry.cget("state") == "disabled"
        )
        checks["room_automation"] = (
            tuple(self.room_automation_combo.cget("values"))
            == self._automation_labels()
            and self._automation_mode_from_label(self.room_automation_choice.get())
            in AUTOMATION_MODE_TO_KEY
            and self.pause_discussion_button.cget("state") == "disabled"
            and self.resume_discussion_button.cget("state") == "disabled"
            and self.continue_discussion_button.cget("state") == "disabled"
            and self.stop_discussion_button.cget("state") == "disabled"
        )
        checks["localization"] = (
            tuple(self.locale_combo.cget("values")) == tuple(LOCALE_LABELS.values())
            and self.locale_label.get() in LOCALE_LABELS.values()
            and self.language_label.cget("text") == self._t("toolbar.language")
            and tuple(self.theme_combo.cget("values"))
            == tuple(
                THEME_LABELS[self.locale.get()][key] for key in SUPPORTED_THEMES
            )
            and self.theme_choice.get()
            == THEME_LABELS[self.locale.get()][self.theme.get()]
            and self.theme_title_label.cget("text") == self._t("toolbar.theme")
            and bool(self.refresh_status.get())
            and self.help_button.cget("state") == "normal"
            and bool(self.library_route_notice.cget("text"))
            and all(
                self._t(f"tutorial.panel.{page_key}.purpose")
                and self._t(f"tutorial.panel.{page_key}.body")
                for page_key in TUTORIAL_PAGE_KEYS
            )
        )
        checks["update_check"] = (
            self.update_button.cget("state") == "normal"
            and bool(self.update_button.cget("command"))
        )
        self._update_feedback_wraplengths()
        modern_options_were_visible = self.modern_toolbar_options_visible
        if self.theme.get() == "modern" and not modern_options_were_visible:
            self._toggle_modern_toolbar_options()
            self.root.update_idletasks()
        checks["announcements"] = (
            "announcement" in self.pages
            and "announcement" in self.nav_buttons
            and bool(self.announcement_button.cget("command"))
            and bool(self.announcement_sync_button.cget("command"))
            and self.announcement_button.winfo_ismapped()
            and self.update_button.winfo_ismapped()
            and (
                self.theme.get() != "modern"
                or self.modern_toolbar_options_button.winfo_ismapped()
            )
        )
        if self.theme.get() == "modern" and not modern_options_were_visible:
            self._toggle_modern_toolbar_options()
            self.root.update_idletasks()
        checks["feedback_layout"] = all(
            int(widget.cget("wraplength")) >= minimum
            for widget, minimum in (
                (self.feedback_prompt_label, 150),
                (self.feedback_contact_label, 150),
                (self.feedback_attachment_label, 180),
                (self.feedback_key_toggle, 260),
                (self.feedback_privacy_label, 260),
                (self.feedback_status_label, 220),
            )
        )
        self.show_page("chat")
        self.root.update_idletasks()
        self._layout_chat_after_resize()
        self.root.update_idletasks()
        self.chat_page_canvas.yview_moveto(1.0)
        self.root.update_idletasks()
        root_right = self.root.winfo_rootx() + self.root.winfo_width()
        checks["composer_visible"] = (
            self.send_button.winfo_y() + self.send_button.winfo_height()
            <= self.chat_composer.winfo_height()
            and self.send_button.winfo_x() + self.send_button.winfo_width()
            <= self.chat_composer.winfo_width()
            and self.send_button.winfo_width()
            >= (40 if self.theme.get() == "modern" else 80)
            and self.message_status_label.winfo_y()
            + self.message_status_label.winfo_height()
            <= self.chat_composer.winfo_height()
            and self.message_status_label.winfo_x()
            + self.message_status_label.winfo_width()
            <= self.chat_composer.winfo_width()
            and self.chat_composer.winfo_reqheight()
            <= self.chat_composer.winfo_height()
            and (
                (
                    self.theme.get() == "modern"
                    and not self.chat_page_scrollbar.winfo_ismapped()
                    and abs(
                        self.chat_page_content.winfo_height()
                        - self.chat_page_canvas.winfo_height()
                    )
                    <= 2
                )
                or (
                    self.theme.get() != "modern"
                    and self.chat_page_scrollbar.winfo_ismapped()
                    and self.chat_page_content.winfo_height()
                    > self.chat_page_canvas.winfo_height()
                )
            )
            and self.send_button.winfo_rooty() + self.send_button.winfo_height()
            <= self.root.winfo_rooty() + self.root.winfo_height()
            and self.send_button.winfo_rootx() + self.send_button.winfo_width()
            <= root_right
        )
        checks["chat_history_height"] = (
            self.chat_canvas.winfo_height() >= CHAT_HISTORY_MIN_HEIGHT
            and (
                self.theme.get() == "modern"
                or self.chat_page_content.winfo_height()
                >= CHAT_PAGE_MIN_CONTENT_HEIGHT
            )
        )
        if self.theme.get() == "modern":
            workspace = self.modern_chat_workspace
            inspector = self.modern_chat_inspector
            inspector_present = self._modern_inspector_present()
            inspector_switches: list[bool] = []
            for inspector_key in MODERN_INSPECTOR_KEYS:
                self._show_modern_inspector(inspector_key)
                self.root.update_idletasks()
                inspector_switches.append(
                    self.modern_inspector_active == inspector_key
                    and (
                        self.modern_inspector_frames[inspector_key].winfo_ismapped()
                        if inspector_present
                        else self.modern_inspector_frames[inspector_key].winfo_exists()
                    )
                )
            self._show_modern_inspector("agents")
            checks["modern_workspace_contract"] = (
                modern_navigation_is_complete()
                and tuple(self.modern_nav_group_labels) == tuple(
                    group_key for group_key, _page_keys in MODERN_NAV_GROUPS
                )
                and set(self.nav_buttons) == set(modern_navigation_pages())
                and workspace is not None
                and inspector is not None
                and len(workspace.panes()) == (2 if inspector_present else 1)
                and tuple(self.modern_inspector_buttons) == MODERN_INSPECTOR_KEYS
                and tuple(self.modern_inspector_frames) == MODERN_INSPECTOR_KEYS
                and all(inspector_switches)
                and set(self.modern_evidence_buttons)
                == {"work", "review", "audit", "trust", "memory", "usage"}
            )
        else:
            checks["modern_workspace_contract"] = (
                self.modern_chat_workspace is None
                and self.modern_chat_inspector is None
                and not self.modern_inspector_buttons
                and not self.modern_inspector_frames
                and not self.modern_evidence_buttons
            )
        result = {
            "status": "PASS" if all(checks.values()) else "FAIL",
            "ui_identity": {
                "locale": self.locale.get(),
                "theme": self.theme.get(),
            },
            "ui_geometry": {
                "client_width": self.root.winfo_width(),
                "client_height": self.root.winfo_height(),
                "configured_scale_factor": configured_scale,
                "tk_scaling": actual_tk_scaling,
                "expected_tk_scaling": expected_tk_scaling,
            },
            "cockpit": cockpit_checks,
            "navigation": checks,
        }
        self.close()
        return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PeerBridge Control Room and explicit human MCP message console."
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path)
    parser.add_argument("--scope", default="default")
    parser.add_argument("--refresh-ms", type=int, default=1500)
    parser.add_argument(
        "--ui-scale-factor",
        type=float,
        choices=WINDOWS_UI_SCALE_FACTORS,
        help="Set the Windows display scale used by the UI self-test.",
    )
    parser.add_argument(
        "--theme",
        choices=SUPPORTED_THEMES,
        help="Override the saved appearance for this launch.",
    )
    parser.add_argument(
        "--locale",
        choices=SUPPORTED_LOCALES,
        help="Override the saved language for this launch.",
    )
    parser.add_argument("--snapshot", action="store_true", help="Print a small read-only JSON snapshot and exit.")
    parser.add_argument("--self-test", action="store_true", help="Verify schema and read-only enforcement, then exit.")
    parser.add_argument("--ui-self-test", action="store_true", help="Exercise every navigation button and exit.")
    parser.add_argument("--send-self-test", action="store_true", help="Send one message through MCP into a temporary database.")
    return parser.parse_args(argv)


def resolved_runtime_theme(project_root: Path, requested_theme: str | None) -> str:
    """Resolve the normal-launch UI without changing the self-test contract."""
    if requested_theme in SUPPORTED_THEMES:
        return requested_theme
    try:
        saved = str(load_preferences(project_root)["theme"])
    except LocalizationError:
        saved = str(default_preferences()["theme"])
    return saved if saved in SUPPORTED_THEMES else "pixel"


def main(argv: list[str] | None = None) -> int:
    configure_windows_app_identity()
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    db = args.db.resolve() if args.db else project_root / ".peerbridge" / "peerbridge.sqlite3"
    reader = BridgeReader(db, project_root)
    if args.self_test:
        result = reader.self_test(scope=args.scope)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    if args.snapshot:
        snapshot = reader.snapshot(limit=20, scope=args.scope)
        result = {
            "generated_utc": snapshot.generated_utc,
            "database": snapshot.database_path,
            "table_counts": snapshot.table_counts,
            "agents": [row.get("agent_id") for row in snapshot.presence],
            "open_peer_calls": sum(1 for row in snapshot.peer_calls if row.get("status") == "open"),
            "signature": snapshot.signature(),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.ui_self_test:
        monitor = PixelMonitor(
            project_root,
            db,
            args.scope,
            args.refresh_ms,
            ui_scale_factor=args.ui_scale_factor,
            theme=args.theme,
            locale=args.locale,
            hidden_self_test=True,
        )
        result = monitor.ui_self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    if args.send_self_test:
        result = McpHumanClient(project_root, db, args.scope).self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    if not acquire_single_instance():
        return 0
    try:
        runtime_theme = resolved_runtime_theme(project_root, args.theme)
        if runtime_theme == "modern":
            from .local_workbench import main as run_local_workbench

            return run_local_workbench(
                [
                    "--project-root",
                    str(project_root),
                    "--db",
                    str(db),
                    "--scope",
                    args.scope,
                ]
            )
        PixelMonitor(
            project_root,
            db,
            args.scope,
            args.refresh_ms,
            theme=runtime_theme,
            locale=args.locale,
        ).run()
        return 0
    except Exception as exc:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(WINDOW_TITLE, str(exc))
            root.destroy()
        except Exception:
            print(f"ERROR: {exc}", file=sys.stderr)
    return 1


def run_monitor(project_root: Path, db_path: Path, scope: str, refresh_ms: int = 1500) -> int:
    return main(
        [
            "--project-root",
            str(project_root),
            "--db",
            str(db_path),
            "--scope",
            scope,
            "--refresh-ms",
            str(refresh_ms),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
