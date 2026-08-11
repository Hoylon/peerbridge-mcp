from __future__ import annotations

import argparse
import atexit
import ctypes
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Iterable

from . import __version__


APP_VERSION = __version__
WINDOW_TITLE = "PeerBridge MCP Control Room"
WINDOW_TITLE_LIVE = f"{WINDOW_TITLE} // LIVE"
INSTANCE_MUTEX = r"Local\PeerBridgeMcpControlRoomV1"
_INSTANCE_HANDLE: int | None = None
SENSITIVE_INPUT = re.compile(
    r"(?i)(?:sk-|ghp_|github_pat_|Bearer\s+)[A-Za-z0-9_\-.]{16,}|AKIA[0-9A-Z]{16}"
)

COLORS = {
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


def safe_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


@dataclass(frozen=True)
class Snapshot:
    generated_utc: str
    database_path: str
    database_mtime_ns: int
    presence: tuple[dict[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
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
                "call": self.peer_calls[0].get("response_sha256") if self.peer_calls else None,
                "event": self.events[0].get("payload_sha256") if self.events else None,
                "update": self.work_updates[0].get("update_sha256") if self.work_updates else None,
            },
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class BridgeReader:
    TABLES = (
        "agent_presence",
        "messages",
        "peer_calls",
        "peer_reviews",
        "tasks",
        "work_updates",
        "integration_records",
        "events",
    )

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()

    def connect(self) -> sqlite3.Connection:
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _rows(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in connection.execute(query, params).fetchall())

    def snapshot(self, limit: int = 500) -> Snapshot:
        if not self.db_path.is_file():
            raise FileNotFoundError(f"MCP database not found: {self.db_path}")

        with self.connect() as connection:
            live_tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = set(self.TABLES) - live_tables
            if missing:
                raise RuntimeError("MCP database is missing tables: " + ", ".join(sorted(missing)))

            counts = {
                name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                for name in self.TABLES
            }
            presence = self._rows(
                connection,
                "SELECT * FROM agent_presence ORDER BY last_seen_epoch DESC",
            )
            messages = self._rows(
                connection,
                "SELECT * FROM messages ORDER BY created_utc DESC LIMIT ?",
                (limit,),
            )
            calls = self._rows(
                connection,
                "SELECT * FROM peer_calls ORDER BY request_utc DESC LIMIT ?",
                (limit,),
            )
            reviews = self._rows(
                connection,
                "SELECT * FROM peer_reviews ORDER BY review_utc DESC LIMIT ?",
                (limit,),
            )
            tasks = self._rows(
                connection,
                "SELECT * FROM tasks ORDER BY updated_utc DESC LIMIT ?",
                (limit,),
            )
            updates = self._rows(
                connection,
                "SELECT * FROM work_updates ORDER BY created_utc DESC LIMIT ?",
                (limit,),
            )
            changes = self._rows(
                connection,
                "SELECT * FROM integration_records ORDER BY recorded_utc DESC LIMIT ?",
                (limit,),
            )
            events = self._rows(
                connection,
                "SELECT * FROM events ORDER BY created_utc DESC LIMIT ?",
                (limit,),
            )

        return Snapshot(
            generated_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            database_path=str(self.db_path),
            database_mtime_ns=self.db_path.stat().st_mtime_ns,
            presence=presence,
            messages=messages,
            peer_calls=calls,
            peer_reviews=reviews,
            tasks=tasks,
            work_updates=updates,
            changes=changes,
            events=events,
            table_counts=counts,
        )

    def self_test(self) -> dict[str, Any]:
        snapshot = self.snapshot(limit=5)
        write_blocked = False
        try:
            with self.connect() as connection:
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


class McpHumanClient:
    """Use the bridge's stdio MCP tool path for explicit human messages."""

    def __init__(self, project_root: Path, db_path: Path, scope: str) -> None:
        self.project_root = project_root.resolve()
        self.db_path = db_path.resolve()
        self.scope = scope

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
            "human-operator",
            "--scope",
            self.scope,
        ]
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-pixel-monitor", "version": APP_VERSION},
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
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
            raise RuntimeError(f"MCP server returned no tool response: {clip(detail, 300)}")
        if "error" in response:
            raise RuntimeError(str(response["error"].get("message", response["error"])))
        content = response.get("result", {}).get("content", [])
        if not content or "text" not in content[0]:
            raise RuntimeError("MCP send_message returned malformed content")
        result = json.loads(content[0]["text"])
        if not isinstance(result, dict):
            raise RuntimeError("MCP send_message result is not an object")
        return result

    def send_message(
        self,
        *,
        recipient: str,
        task_id: str,
        subject: str,
        body: str,
        priority: str,
    ) -> dict[str, Any]:
        clean_body = body.strip()
        if SENSITIVE_INPUT.search(clean_body):
            raise ValueError("訊息看似包含 API key/token；為避免寫入審計庫，已拒絕發送。")
        if len(clean_body) > 20_000:
            raise ValueError("訊息超過 20,000 字元上限。")
        return self.call_tool(
            "send_message",
            {
                "recipient": recipient,
                "task_id": task_id,
                "subject": subject,
                "body": clean_body,
                "priority": priority,
                "artifact_paths": [],
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

    def __init__(self, project_root: Path, db_path: Path, scope: str, refresh_ms: int = 1500) -> None:
        self.project_root = project_root.resolve()
        self.scope = scope
        self.REFRESH_MS = max(500, min(int(refresh_ms), 10000))
        self.reader = BridgeReader(db_path)
        self.human_client = McpHumanClient(self.project_root, db_path, scope)
        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry("1320x820")
        self.root.minsize(980, 650)
        self.root.configure(bg=COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.last_signature = ""
        self.snapshot: Snapshot | None = None
        self.paused = tk.BooleanVar(value=False)
        self.search = tk.StringVar(value="")
        self.message_recipient = tk.StringVar(value="*")
        self.message_priority = tk.StringVar(value="normal")
        self.message_task = tk.StringVar(value=f"human-chat-{datetime.now().strftime('%Y%m%d')}")
        self.message_subject = tk.StringVar(value="HUMAN INTERVENTION")
        self.message_status = tk.StringVar(value="Enter 送出 / Shift+Enter 換行")
        self.send_in_progress = False
        self.active_page = "chat"
        self.nav_buttons: dict[str, tk.Button] = {}
        self.pages: dict[str, tk.Frame] = {}
        self._configure_styles()
        self._build_layout()
        self.search.trace_add("write", lambda *_: self.render(force=True))
        self.refresh(force=True)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            foreground=COLORS["text"],
            rowheight=30,
            borderwidth=0,
            font=("Cascadia Mono", 9),
        )
        style.map("Treeview", background=[("selected", COLORS["blue"])], foreground=[("selected", COLORS["black"])])
        style.configure(
            "Treeview.Heading",
            background=COLORS["panel_2"],
            foreground=COLORS["amber"],
            relief="raised",
            borderwidth=1,
            font=("Cascadia Mono", 9, "bold"),
        )
        style.map("Treeview.Heading", background=[("active", COLORS["line"])])
        style.configure("Vertical.TScrollbar", background=COLORS["line"], troughcolor=COLORS["black"], arrowsize=14)

    def _build_layout(self) -> None:
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)
        sidebar = tk.Frame(self.root, bg=COLORS["black"], width=236, bd=2, relief="ridge")
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)

        title = tk.Label(
            sidebar,
            text="PEERBRIDGE\nCONTROL ROOM",
            bg=COLORS["black"],
            fg=COLORS["cyan"],
            justify="left",
            font=("Cascadia Mono", 17, "bold"),
        )
        title.pack(anchor="w", padx=18, pady=(18, 4))
        tk.Label(
            sidebar,
            text=f"RO VIEW + MCP TX // v{APP_VERSION}",
            bg=COLORS["black"],
            fg=COLORS["muted"],
            font=("Cascadia Mono", 8),
        ).pack(anchor="w", padx=18, pady=(0, 16))

        self.agent_canvas = tk.Canvas(sidebar, width=200, height=194, bg=COLORS["panel"], highlightthickness=2, highlightbackground=COLORS["line"])
        self.agent_canvas.pack(padx=16, fill="x")
        self._draw_agents([])

        nav_items = [
            ("chat", "01  對話"),
            ("work", "02  工作板"),
            ("review", "03  互評"),
            ("change", "04  變更"),
            ("audit", "05  審計"),
        ]
        nav = tk.Frame(sidebar, bg=COLORS["black"])
        nav.pack(fill="x", padx=12, pady=18)
        for key, text in nav_items:
            button = tk.Button(
                nav,
                text=text,
                command=lambda value=key: self.show_page(value),
                anchor="w",
                bg=COLORS["panel"],
                fg=COLORS["text"],
                activebackground=COLORS["cyan"],
                activeforeground=COLORS["black"],
                relief="raised",
                bd=2,
                padx=12,
                pady=8,
                font=("Cascadia Mono", 10, "bold"),
            )
            button.pack(fill="x", pady=4)
            self.nav_buttons[key] = button

        self.stats_label = tk.Label(
            sidebar,
            text="WAITING FOR DB...",
            bg=COLORS["black"],
            fg=COLORS["muted"],
            justify="left",
            anchor="sw",
            font=("Cascadia Mono", 9),
        )
        self.stats_label.pack(side="bottom", fill="x", padx=18, pady=18)

        main = tk.Frame(self.root, bg=COLORS["bg"])
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(main, bg=COLORS["panel"], bd=2, relief="ridge", height=64)
        toolbar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        toolbar.grid_columnconfigure(2, weight=1)
        self.page_title = tk.Label(toolbar, text="對話", bg=COLORS["panel"], fg=COLORS["amber"], font=("Cascadia Mono", 14, "bold"))
        self.page_title.grid(row=0, column=0, padx=14, pady=12, sticky="w")
        tk.Label(
            toolbar,
            text="搜尋",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Cascadia Mono", 9, "bold"),
        ).grid(row=0, column=1, padx=(6, 0))
        search_entry = tk.Entry(
            toolbar,
            textvariable=self.search,
            bg=COLORS["black"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 10),
        )
        search_entry.grid(row=0, column=2, sticky="ew", padx=10, ipady=6)
        tk.Button(
            toolbar,
            text="|| 暫停",
            command=self.toggle_pause,
            bg=COLORS["purple"],
            fg=COLORS["black"],
            activebackground=COLORS["amber"],
            relief="raised",
            bd=2,
            padx=10,
            pady=5,
            font=("Cascadia Mono", 9, "bold"),
        ).grid(row=0, column=3, padx=5)
        tk.Button(
            toolbar,
            text=">> 刷新",
            command=lambda: self.refresh(force=True),
            bg=COLORS["green"],
            fg=COLORS["black"],
            activebackground=COLORS["cyan"],
            relief="raised",
            bd=2,
            padx=10,
            pady=5,
            font=("Cascadia Mono", 9, "bold"),
        ).grid(row=0, column=4, padx=(5, 12))

        page_host = tk.Frame(main, bg=COLORS["bg"])
        page_host.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        page_host.grid_rowconfigure(0, weight=1)
        page_host.grid_columnconfigure(0, weight=1)

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
        self.show_page("chat")

    def _build_chat_page(self, host: tk.Frame) -> None:
        page = tk.Frame(host, bg=COLORS["panel"], bd=2, relief="ridge")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=1)
        canvas = tk.Canvas(page, bg=COLORS["panel"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        inner = tk.Frame(canvas, bg=COLORS["panel"])
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.chat_canvas = canvas
        self.chat_inner = inner

        composer = tk.Frame(page, bg=COLORS["black"], bd=2, relief="ridge")
        composer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        composer.grid_columnconfigure(5, weight=1)
        tk.Label(composer, text="TO", bg=COLORS["black"], fg=COLORS["amber"], font=("Cascadia Mono", 8, "bold")).grid(row=0, column=0, padx=(8, 4), pady=6)
        recipient = ttk.Combobox(
            composer,
            textvariable=self.message_recipient,
            values=("*", "agent-a", "agent-b", "human-operator"),
            width=13,
            state="readonly",
        )
        self.recipient_combo = recipient
        recipient.grid(row=0, column=1, padx=4, pady=6)
        tk.Label(composer, text="PRIORITY", bg=COLORS["black"], fg=COLORS["amber"], font=("Cascadia Mono", 8, "bold")).grid(row=0, column=2, padx=(10, 4), pady=6)
        priority = ttk.Combobox(
            composer,
            textvariable=self.message_priority,
            values=("low", "normal", "high", "critical"),
            width=9,
            state="readonly",
        )
        priority.grid(row=0, column=3, padx=4, pady=6)
        tk.Label(composer, text="TASK", bg=COLORS["black"], fg=COLORS["amber"], font=("Cascadia Mono", 8, "bold")).grid(row=0, column=4, padx=(10, 4), pady=6)
        tk.Entry(
            composer,
            textvariable=self.message_task,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
        ).grid(row=0, column=5, sticky="ew", padx=4, pady=6)
        tk.Label(composer, text="SUBJECT", bg=COLORS["black"], fg=COLORS["amber"], font=("Cascadia Mono", 8, "bold")).grid(row=1, column=0, padx=(8, 4), pady=4)
        tk.Entry(
            composer,
            textvariable=self.message_subject,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
        ).grid(row=1, column=1, columnspan=5, sticky="ew", padx=4, pady=4)
        self.message_body = tk.Text(
            composer,
            height=3,
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
        self.message_body.grid(row=2, column=0, columnspan=6, sticky="ew", padx=8, pady=(5, 4))
        self.message_body.bind("<Return>", self._on_message_enter)
        self.send_button = tk.Button(
            composer,
            text=">> MCP 送出",
            command=self.send_human_message,
            bg=COLORS["cyan"],
            fg=COLORS["black"],
            activebackground=COLORS["green"],
            relief="raised",
            bd=2,
            padx=12,
            pady=5,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.send_button.grid(row=3, column=5, sticky="e", padx=8, pady=(2, 7))
        self.message_status_label = tk.Label(
            composer,
            textvariable=self.message_status,
            bg=COLORS["black"],
            fg=COLORS["muted"],
            anchor="w",
            font=("Cascadia Mono", 8),
        )
        self.message_status_label.grid(row=3, column=0, columnspan=5, sticky="ew", padx=8, pady=(2, 7))
        self.pages["chat"] = page

    def _on_message_enter(self, event: tk.Event[Any]) -> str | None:
        if event.state & 0x0001:  # Shift+Enter inserts a newline.
            return None
        self.send_human_message()
        return "break"

    def send_human_message(self) -> None:
        if self.send_in_progress:
            return
        body = self.message_body.get("1.0", "end-1c").strip()
        task_id = self.message_task.get().strip()
        subject = self.message_subject.get().strip()
        if not body or not task_id or not subject:
            self.message_status.set("需要 TASK、SUBJECT 和訊息內容。")
            self.message_status_label.configure(fg=COLORS["red"])
            return
        payload = {
            "recipient": self.message_recipient.get(),
            "task_id": task_id,
            "subject": subject,
            "body": body,
            "priority": self.message_priority.get(),
        }
        self.send_in_progress = True
        self.send_button.configure(state="disabled", text="SENDING...")
        self.message_status.set("正透過 MCP stdio 寫入 SHA 綁定訊息...")
        self.message_status_label.configure(fg=COLORS["amber"])

        def worker() -> None:
            try:
                receipt = self.human_client.send_message(**payload)
            except Exception as exc:
                self.root.after(0, lambda: self._finish_human_send(None, str(exc)))
                return
            self.root.after(0, lambda: self._finish_human_send(receipt, None))

        threading.Thread(target=worker, name="mcp-human-send", daemon=True).start()

    def _finish_human_send(self, receipt: dict[str, Any] | None, error: str | None) -> None:
        self.send_in_progress = False
        self.send_button.configure(state="normal", text=">> MCP 送出")
        if error:
            self.message_status.set(f"發送失敗：{clip(error, 180)}")
            self.message_status_label.configure(fg=COLORS["red"])
            return
        assert receipt is not None
        sha = str(receipt.get("content_sha256", ""))
        self.message_body.delete("1.0", "end")
        self.message_status.set(f"已送出 // SHA {sha[:16]} // {utc_text(receipt.get('created_utc'))}")
        self.message_status_label.configure(fg=COLORS["green"])
        self.refresh(force=True)

    def _make_tree_page(self, host: tk.Frame, key: str, columns: list[tuple[str, str, int]]) -> DetailTree:
        page = DetailTree(host, columns)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages[key] = page
        return page

    def show_page(self, key: str) -> None:
        self.active_page = key
        for page_key, page in self.pages.items():
            if page_key == key:
                page.tkraise()
        titles = {"chat": "對話", "work": "工作板", "review": "互評", "change": "變更", "audit": "審計"}
        self.page_title.configure(text=titles[key])
        for button_key, button in self.nav_buttons.items():
            active = button_key == key
            button.configure(bg=COLORS["cyan"] if active else COLORS["panel"], fg=COLORS["black"] if active else COLORS["text"])

    def toggle_pause(self) -> None:
        self.paused.set(not self.paused.get())
        self.stats_label.configure(fg=COLORS["amber"] if self.paused.get() else COLORS["muted"])
        if not self.paused.get():
            self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        if self.paused.get() and not force:
            self.root.after(self.REFRESH_MS, self.refresh)
            return
        try:
            snapshot = self.reader.snapshot()
            signature = snapshot.signature()
            self.snapshot = snapshot
            if force or signature != self.last_signature:
                self.last_signature = signature
                self.render(force=force)
            self.root.title(WINDOW_TITLE_LIVE)
        except Exception as exc:  # UI must remain alive on transient WAL/lock errors.
            self.root.title(f"{WINDOW_TITLE} // ERROR")
            self.stats_label.configure(text=f"DB ERROR\n{clip(exc, 80)}", fg=COLORS["red"])
        self.root.after(self.REFRESH_MS, self.refresh)

    def render(self, force: bool = False) -> None:
        if not self.snapshot:
            return
        query = self.search.get().strip().lower()
        self._render_presence()
        self._render_chat(query)
        self._render_work(query)
        self._render_reviews(query)
        self._render_changes(query)
        self._render_audit(query)

    @staticmethod
    def _match(record: dict[str, Any], query: str) -> bool:
        if not query:
            return True
        return query in json.dumps(record, ensure_ascii=False).lower()

    def _render_presence(self) -> None:
        assert self.snapshot is not None
        now = time.time()
        agents: dict[str, dict[str, Any]] = {}
        for row in self.snapshot.presence:
            current = agents.get(row["agent_id"])
            if current is None or float(row["last_seen_epoch"]) > float(current["last_seen_epoch"]):
                agents[row["agent_id"]] = row
        self._draw_agents(list(agents.values()))
        open_calls = sum(1 for row in self.snapshot.peer_calls if row.get("status") == "open")
        active_tasks = sum(1 for row in self.snapshot.tasks if row.get("status") not in {"complete", "closed"})
        online = sum(1 for row in agents.values() if now - float(row.get("last_seen_epoch", 0)) <= 120)
        visible_agents = sorted(name for name in agents if name != "human-operator")
        self.recipient_combo.configure(values=tuple(["*", *visible_agents, "human-operator"]))
        self.stats_label.configure(
            text=(
                f"ONLINE   {online}/{len(agents)}\n"
                f"MESSAGES {self.snapshot.table_counts['messages']}\n"
                f"OPEN CALL {open_calls}\n"
                f"ACTIVE   {active_tasks}\n"
                f"AUDIT    {self.snapshot.table_counts['events']}\n\n"
                f"SYNC {utc_text(self.snapshot.generated_utc)}"
            ),
            fg=COLORS["amber"] if self.paused.get() else COLORS["muted"],
        )

    def _draw_agents(self, rows: list[dict[str, Any]]) -> None:
        canvas = self.agent_canvas
        canvas.delete("all")
        latest = {str(row.get("agent_id")): row for row in rows if row.get("agent_id") != "human-operator"}
        now = time.time()
        if not latest:
            canvas.create_text(
                100,
                90,
                text="NO AGENTS ONLINE",
                fill=COLORS["muted"],
                font=("Cascadia Mono", 9, "bold"),
            )
            return
        names = sorted(
            latest,
            key=lambda name: (-float(latest[name].get("last_seen_epoch", 0)), name),
        )
        palette = (COLORS["cyan"], COLORS["amber"], COLORS["green"], COLORS["purple"], COLORS["blue"])
        for index, name in enumerate(names[:8]):
            column = index % 2
            row_index = index // 2
            x = 7 + column * 96
            y = 7 + row_index * 44
            color = palette[int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:2], 16) % len(palette)]
            row = latest.get(name)
            online = bool(row and now - float(row.get("last_seen_epoch", 0)) <= 120)
            shade = color if online else COLORS["line"]
            canvas.create_rectangle(x, y, x + 90, y + 38, fill=COLORS["panel_2"], outline=COLORS["line"])
            for px, py in ((0, 4), (4, 0), (8, 0), (12, 4), (0, 8), (4, 8), (8, 8), (12, 8), (4, 12), (8, 12)):
                canvas.create_rectangle(x + 7 + px, y + 7 + py, x + 11 + px, y + 11 + py, fill=shade, outline=shade)
            canvas.create_rectangle(x + 11, y + 15, x + 13, y + 17, fill=COLORS["black"], outline="")
            canvas.create_rectangle(x + 17, y + 15, x + 19, y + 17, fill=COLORS["black"], outline="")
            canvas.create_oval(x + 78, y + 5, x + 86, y + 13, fill=COLORS["green"] if online else COLORS["red"], outline=COLORS["black"])
            canvas.create_text(x + 33, y + 12, text=clip(name.upper(), 9), anchor="w", fill=shade, font=("Cascadia Mono", 7, "bold"))
            route = row.get("model_id") or row.get("provider_id") or ("ONLINE" if online else "OFFLINE")
            canvas.create_text(x + 33, y + 27, text=clip(str(route).upper(), 9), anchor="w", fill=COLORS["green"] if online else COLORS["red"], font=("Cascadia Mono", 6))
        if len(names) > 8:
            canvas.create_text(194, 188, text=f"+{len(names) - 8}", anchor="se", fill=COLORS["amber"], font=("Cascadia Mono", 7, "bold"))

    def _chat_records(self) -> list[dict[str, Any]]:
        assert self.snapshot is not None
        timeline: list[dict[str, Any]] = []
        peer_request_ids = {row["request_id"] for row in self.snapshot.peer_calls}
        for row in self.snapshot.messages:
            body_json = safe_json(row.get("body"), {})
            if isinstance(body_json, dict) and body_json.get("request_id") in peer_request_ids:
                continue
            timeline.append(
                {
                    "kind": "message",
                    "time": row.get("created_utc"),
                    "sender": row.get("sender"),
                    "recipient": row.get("recipient"),
                    "task": row.get("task_id"),
                    "subject": row.get("subject"),
                    "body": row.get("body"),
                    "sha": row.get("content_sha256"),
                    "status": "ACK" if row.get("acknowledged_utc") else "UNREAD",
                }
            )
        for row in self.snapshot.peer_calls:
            timeline.append(
                {
                    "kind": "peer_request",
                    "time": row.get("request_utc"),
                    "sender": row.get("requester"),
                    "recipient": row.get("recipient"),
                    "task": row.get("task_id"),
                    "subject": f"PEER REQUEST // {row.get('approval_mode')}",
                    "body": row.get("question"),
                    "sha": row.get("request_sha256"),
                    "status": row.get("status", "open").upper(),
                }
            )
            if row.get("response_utc"):
                timeline.append(
                    {
                        "kind": "peer_response",
                        "time": row.get("response_utc"),
                        "sender": row.get("recipient"),
                        "recipient": row.get("requester"),
                        "task": row.get("task_id"),
                        "subject": "PEER RESPONSE",
                        "body": row.get("response"),
                        "sha": row.get("response_sha256"),
                        "status": "RESPONDED",
                    }
                )
        timeline.sort(key=lambda row: row.get("time") or "")
        return timeline

    def _render_chat(self, query: str) -> None:
        for child in self.chat_inner.winfo_children():
            child.destroy()
        rows = [row for row in self._chat_records() if self._match(row, query)]
        if not rows:
            tk.Label(self.chat_inner, text="NO MESSAGES", bg=COLORS["panel"], fg=COLORS["muted"], font=("Cascadia Mono", 12, "bold")).pack(pady=50)
            return
        for row in rows:
            self._add_bubble(row)
        self.root.after_idle(lambda: self.chat_canvas.yview_moveto(1.0))

    def _add_bubble(self, row: dict[str, Any]) -> None:
        sender = row.get("sender") or "unknown"
        own = sender == "human-operator"
        bubble_color = COLORS["panel_2"] if own else "#2b261a"
        accent = COLORS["cyan"] if own else COLORS["amber"]
        line = tk.Frame(self.chat_inner, bg=COLORS["panel"])
        line.pack(fill="x", padx=16, pady=7)
        bubble = tk.Frame(line, bg=bubble_color, bd=2, relief="ridge")
        bubble.pack(side="right" if own else "left", fill="x", expand=False, padx=(180, 0) if own else (0, 180))
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
            text=f"[{row.get('status')}]  {row.get('subject') or row.get('kind')}\nTASK: {row.get('task') or '--'}",
            justify="left",
            anchor="w",
            bg=bubble_color,
            fg=COLORS["purple"],
            font=("Cascadia Mono", 8, "bold"),
        ).pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(
            bubble,
            text=row.get("body") or "",
            justify="left",
            anchor="w",
            wraplength=720,
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
            text="COPY SHA",
            command=lambda value=sha: self.copy_text(value),
            bg=COLORS["line"],
            fg=COLORS["text"],
            activebackground=accent,
            activeforeground=COLORS["black"],
            relief="raised",
            bd=1,
            font=("Cascadia Mono", 7, "bold"),
        ).pack(side="right")

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

    def copy_text(self, value: str) -> None:
        if not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)

    def run(self) -> None:
        self.root.mainloop()

    def ui_self_test(self) -> dict[str, Any]:
        self.root.withdraw()
        checks: dict[str, bool] = {}
        titles = {"chat": "對話", "work": "工作板", "review": "互評", "change": "變更", "audit": "審計"}
        for key, button in self.nav_buttons.items():
            button.invoke()
            self.root.update_idletasks()
            checks[key] = self.active_page == key and self.page_title.cget("text") == titles[key]
        checks["composer"] = (
            self.message_recipient.get() == "*"
            and self.message_priority.get() == "normal"
            and self.send_button.cget("state") == "normal"
        )
        self.root.destroy()
        return {"status": "PASS" if all(checks.values()) else "FAIL", "navigation": checks}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pixel monitor and explicit human MCP message console.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path)
    parser.add_argument("--scope", default="default")
    parser.add_argument("--refresh-ms", type=int, default=1500)
    parser.add_argument("--snapshot", action="store_true", help="Print a small read-only JSON snapshot and exit.")
    parser.add_argument("--self-test", action="store_true", help="Verify schema and read-only enforcement, then exit.")
    parser.add_argument("--ui-self-test", action="store_true", help="Exercise every navigation button and exit.")
    parser.add_argument("--send-self-test", action="store_true", help="Send one message through MCP into a temporary database.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    db = args.db.resolve() if args.db else project_root / ".peerbridge" / "peerbridge.sqlite3"
    reader = BridgeReader(db)
    if args.self_test:
        result = reader.self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    if args.snapshot:
        snapshot = reader.snapshot(limit=20)
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
        monitor = PixelMonitor(project_root, db, args.scope, args.refresh_ms)
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
        PixelMonitor(project_root, db, args.scope, args.refresh_ms).run()
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
