from __future__ import annotations

import base64
import http.client
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.agent_identity import ensure_agent_identity_capability
from peerbridge_mcp import local_workbench as workbench_module
from peerbridge_mcp.announcements import Announcement
from peerbridge_mcp.local_workbench import (
    WorkbenchError,
    _system_webview2_runtime_path,
    make_server,
    run_native_workbench,
    workbench_url,
)
from peerbridge_mcp.monitor import MCP_HUMAN_CLIENT_TOOLS, McpHumanClient


TOKEN = "test-admin-token-not-for-production"


def test_managed_agent_catalog_coalesces_concurrent_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    results: list[list[dict[str, object]]] = []

    def build(_project_root: Path) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return [{"agent_id": "codex", "installed": True}]

    monkeypatch.setattr(workbench_module, "_build_managed_agent_catalog", build)
    workbench_module._AGENT_CATALOG_CACHE.clear()

    def read_catalog() -> None:
        results.append(workbench_module.managed_agent_catalog(tmp_path))

    first = threading.Thread(target=read_catalog)
    second = threading.Thread(target=read_catalog)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == 1
    assert results == [
        [{"agent_id": "codex", "installed": True}],
        [{"agent_id": "codex", "installed": True}],
    ]
    workbench_module._AGENT_CATALOG_CACHE.clear()


def seed(root: Path, db: Path, scope: str = "scope-a") -> None:
    human = Bridge(root, db, "human-operator", scope)
    human.create_room({"room_id": "alpha", "name": "Alpha 5.2"})
    human.upsert_route_profile(
        {
            "route_id": "grok-high",
            "agent_id": "grok-relay",
            "provider_id": "relay-grok",
            "model_id": "grok-4.6",
            "reasoning_mode": "high",
            "route_class": "relay",
        }
    )
    human.join_room(
        {
            "room_id": "alpha",
            "agent_id": "grok-relay",
            "route_profile_id": "grok-high",
        }
    )
    fanout = human.send_room_fanout(
        {
            "room_id": "alpha",
            "task_id": "alpha-52",
            "subject": "Release review",
            "body": "Review the local workbench and preserve its security boundary.",
            "priority": "high",
        }
    )
    worker = Bridge(
        root,
        db,
        "grok-relay",
        scope,
        provider_id="relay-grok",
        model_id="grok-4.6",
        reasoning_mode="high",
        route_class="relay",
    )
    worker.claim_message_dispatch(
        {
            "message_id": fanout["recipients"][0]["message_id"],
            "route_profile_id": "grok-high",
        }
    )
    claim = human.claim_task(
        {
            "task_id": "release-readiness",
            "summary": "Validate the local workbench release boundary.",
            "approval_mode": "solo_allowed",
        }
    )
    human.announce_work(
        {
            "task_id": claim["task_id"],
            "lease_token": claim["lease_token"],
            "status": "review",
            "summary": "Focused workbench verification is in review.",
        }
    )


@contextmanager
def running_server(
    root: Path,
    db: Path,
    *,
    managed_agent_manager: object | None = None,
) -> Iterator[tuple[int, object]]:
    server = make_server(
        root,
        db,
        "scope-a",
        port=0,
        token=TOKEN,
        initial_room_id="alpha",
        instance_id="test-workbench",
        managed_agent_manager=managed_agent_manager,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1]), server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    merged = dict(headers or {})
    if body is not None:
        merged.setdefault("Content-Type", "application/json")
        merged.setdefault("Content-Length", str(len(body)))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(method, path, body=body, headers=merged)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def auth_headers(port: int, **extra: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Origin": f"http://127.0.0.1:{port}",
        **extra,
    }


def message_payload(body: str = "Please review the Alpha 5.2 workbench.") -> dict[str, object]:
    return {
        "request_id": "request0123456789abcdef0123456789",
        "room_id": "alpha",
        "recipient": "grok-relay",
        "task_id": "human-chat-20260821",
        "subject": "HUMAN INTERVENTION",
        "body": body,
        "priority": "normal",
    }


def test_workbench_routes_provider_ccswitch_and_agent_install_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".peerbridge" / "peerbridge.sqlite3"
    seed(root, db)
    calls: list[tuple[str, dict[str, object]]] = []
    endpoints = {
        "/api/agent/install": "_handle_agent_install",
        "/api/provider/save": "_handle_provider_save",
        "/api/provider/discover": "_handle_provider_discover",
        "/api/provider/route": "_handle_provider_route",
        "/api/ccswitch/providers": "_handle_ccswitch_providers",
        "/api/ccswitch/models": "_handle_ccswitch_models",
        "/api/ccswitch/route": "_handle_ccswitch_route",
        "/api/ccswitch/switch": "_handle_ccswitch_switch",
    }

    for expected_path, method_name in endpoints.items():
        def handler(self: object, payload: dict[str, object], *, path: str = expected_path) -> None:
            calls.append((path, payload))
            self._json(200, {"status": "ok", "path": path})

        monkeypatch.setattr(workbench_module.WorkbenchHandler, method_name, handler)

    monkeypatch.setattr(workbench_module, "managed_agent_catalog", lambda *_a, **_k: [])
    with running_server(root, db) as (port, _server):
        for index, expected_path in enumerate(endpoints):
            status, _, body = request(
                port,
                "POST",
                expected_path,
                headers=auth_headers(port),
                payload={"request_id": f"integrationroute{index:02d}0123456789abcdef"},
            )
            assert status == 200
            assert json.loads(body)["path"] == expected_path

    assert [path for path, _payload in calls] == list(endpoints)


def test_history_import_creates_a_source_bound_read_only_virtual_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".peerbridge" / "peerbridge.sqlite3"
    seed(root, db)
    monkeypatch.setattr(workbench_module, "managed_agent_catalog", lambda *_a, **_k: [])
    export = {
        "source_conversation_id": "codex-history-1",
        "title": "Imported release discussion",
        "messages": [
            {
                "id": "source-user-1",
                "role": "user",
                "timestamp": "2026-08-22T01:00:00Z",
                "content": "Review the same frozen source.",
            },
            {
                "id": "source-agent-1",
                "role": "assistant",
                "timestamp": "2026-08-22T01:00:01Z",
                "content": "The source-bound review completed.",
            },
        ],
    }
    content = base64.b64encode(json.dumps(export).encode()).decode("ascii")
    with running_server(root, db) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/history/file/discover",
            headers=auth_headers(port),
            payload={
                "request_id": "historydiscover0123456789abcdef0123",
                "provider": "codex",
                "source_name": "codex-export.json",
                "content_base64": content,
            },
        )
        assert status == 200
        discovered = json.loads(body)
        assert len(discovered["conversations"]) == 1
        selection_handle = discovered["conversations"][0]["selection_handle"]
        assert "selection_id" not in discovered["conversations"][0]

        status, _, body = request(
            port,
            "POST",
            "/api/history/import",
            headers=auth_headers(port),
            payload={
                "request_id": "historyimport0123456789abcdef012345",
                "provider": "codex",
                "source_name": "codex-export.json",
                "content_base64": content,
                "selection_handles": [selection_handle],
            },
        )
        assert status == 200
        imported = json.loads(body)
        room_id = imported["room_id"]

        status, _, replay_body = request(
            port,
            "POST",
            "/api/history/import",
            headers=auth_headers(port),
            payload={
                "request_id": "historyimport0123456789abcdef012345",
                "provider": "codex",
                "source_name": "codex-export.json",
                "content_base64": content,
                "selection_handles": [selection_handle],
            },
        )
        assert status == 200
        assert json.loads(replay_body) == imported

        status, _, body = request(
            port,
            "GET",
            f"/api/bootstrap?room_id={urllib.parse.quote(room_id)}",
            headers=auth_headers(port),
        )
        assert status == 200
        snapshot = json.loads(body)

        for path, mutation in (
            (
                "/api/room/member",
                {"action": "join", "agent_id": "grok-relay", "route_profile_id": "", "role_id": "equal-participant", "role_label": ""},
            ),
            (
                "/api/room/member-role",
                {"agent_id": "codex-history", "role_id": "reviewer", "role_label": ""},
            ),
            (
                "/api/room/automation",
                {"mode": "once", "max_rounds": 2, "max_messages": 8, "stagnation_rounds": 1},
            ),
        ):
            status, _, mutation_body = request(
                port,
                "POST",
                path,
                headers=auth_headers(port),
                payload={
                    "request_id": f"historyreadonly{len(path):02d}0123456789abcdef",
                    "room_id": room_id,
                    **mutation,
                },
            )
            assert status == 400
            assert "read-only" in json.loads(mutation_body)["error"]

        status, _, body = request(
            port,
            "POST",
            "/api/history/continue",
            headers=auth_headers(port),
            payload={
                "request_id": "historycontinue0123456789abcdef",
                "source_room_id": room_id,
                "room_id": "continued-history-room",
                "name": "Continued history room",
            },
        )
        assert status == 200
        continuation = json.loads(body)["result"]
        assert continuation["source_sha256"]

        status, _, body = request(
            port,
            "GET",
            "/api/bootstrap?room_id=continued-history-room",
            headers=auth_headers(port),
        )
        assert status == 200
        continuation_snapshot = json.loads(body)
        assert continuation_snapshot["operator_active"] is True
        assert continuation_snapshot["history_import"]["selected"] is None
        assert "PEERBRIDGE_HISTORY_CONTINUATION_V1" in continuation_snapshot["messages"][0]["body"]
        assert continuation["source_sha256"] in continuation_snapshot["messages"][0]["body"]

        status, _, _ = request(
            port,
            "POST",
            "/api/room/member",
            headers=auth_headers(port),
            payload={
                "request_id": "continuedroomseat0123456789abcdef",
                "action": "join",
                "room_id": "continued-history-room",
                "agent_id": "grok-relay",
                "route_profile_id": "grok-high",
                "role_id": "reviewer",
                "role_label": "",
            },
        )
        assert status == 200

    assert snapshot["room_id"] == room_id
    assert snapshot["operator_active"] is False
    assert snapshot["history_import"]["selected"]["read_only"] is True
    assert snapshot["history_import"]["selected"]["source_conversation_id"] == "codex-history-1"
    assert [row["sender"] for row in snapshot["messages"]] == [
        "human-operator",
        "codex-history",
    ]
    imported_room = next(row for row in snapshot["rooms"] if row["room_id"] == room_id)
    assert imported_room["room_kind"] == "imported-history"
    assert imported_room["provider"] == "codex"


def test_native_history_routes_keep_provider_identity_and_selected_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".peerbridge" / "peerbridge.sqlite3"
    seed(root, db)
    monkeypatch.setattr(
        workbench_module,
        "list_native_sessions",
        lambda provider, project_root: [
            {
                "session_id": f"{provider}-session-1",
                "title": f"{provider} review",
                "updated_utc": "2026-08-22T01:00:00Z",
                "source": f"{provider}-official-history",
                "source_revision": "c" * 64,
            }
        ],
    )
    import_calls: list[dict[str, object]] = []

    def import_native(**kwargs: object) -> list[dict[str, object]]:
        import_calls.append(dict(kwargs))
        return [
            {
                "status": "created",
                "import_id": "grok-import-1",
                "room_id": "history.grok-import-1",
                "provider": kwargs["provider"],
                "title": "Grok review",
                "message_count": 2,
                "source_sha256": "a" * 64,
                "record_sha256": "b" * 64,
            }
        ]

    monkeypatch.setattr(
        workbench_module,
        "import_native_session",
        import_native,
    )
    with running_server(root, db) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/history/native/discover",
            headers=auth_headers(port),
            payload={
                "request_id": "nativehistorydiscover01234567890",
                "provider": "grok",
            },
        )
        assert status == 200
        discovered = json.loads(body)
        assert discovered["provider"] == "grok"
        assert "session_id" not in discovered["sessions"][0]
        assert "source_revision" not in discovered["sessions"][0]
        selection_handle = discovered["sessions"][0]["selection_handle"]

        status, _, _ = request(
            port,
            "POST",
            "/api/history/native/import",
            headers=auth_headers(port),
            payload={
                "request_id": "nativerawidrejected012345678901",
                "provider": "grok",
                "session_id": "grok-session-1",
            },
        )
        assert status == 400

        status, _, body = request(
            port,
            "POST",
            "/api/history/native/import",
            headers=auth_headers(port),
            payload={
                "request_id": "nativehistoryimport012345678901",
                "provider": "grok",
                "selection_handle": selection_handle,
            },
        )
        assert status == 200
        imported = json.loads(body)
        assert imported["room_id"] == "history.grok-import-1"
        assert imported["imports"][0]["provider"] == "grok"
        assert import_calls[0]["project_root"] == root
        assert import_calls[0]["session_id"] == "grok-session-1"
        assert import_calls[0]["source_revision"] == "c" * 64


def test_codex_history_routes_bind_project_and_discovery_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".peerbridge" / "peerbridge.sqlite3"
    seed(root, db)
    revision = "d" * 64
    discovery_calls: list[dict[str, object]] = []
    import_calls: list[dict[str, object]] = []

    def discover(**kwargs: object) -> list[dict[str, object]]:
        discovery_calls.append(dict(kwargs))
        return [
            {
                "thread_id": "codex-thread-1",
                "title": "Codex review",
                "updated_utc": "2026-08-22T01:00:00Z",
                "source": "appServer",
                "source_revision": revision,
            }
        ]

    def import_thread(**kwargs: object) -> list[dict[str, object]]:
        import_calls.append(dict(kwargs))
        return [
            {
                "status": "created",
                "import_id": "codex-import-1",
                "room_id": "history.codex-import-1",
                "provider": "codex",
                "title": "Codex review",
                "message_count": 2,
                "source_sha256": "a" * 64,
                "record_sha256": "b" * 64,
            }
        ]

    monkeypatch.setattr(workbench_module, "discover_codex_threads", discover)
    monkeypatch.setattr(workbench_module, "import_codex_thread", import_thread)

    with running_server(root, db) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/history/codex/discover",
            headers=auth_headers(port),
            payload={
                "request_id": "codexhistorydiscover01234567890",
                "search_term": "release",
            },
        )
        assert status == 200
        discovered = json.loads(body)
        assert "thread_id" not in discovered["threads"][0]
        assert "source_revision" not in discovered["threads"][0]
        selection_handle = discovered["threads"][0]["selection_handle"]

        status, _, body = request(
            port,
            "POST",
            "/api/history/codex/import",
            headers=auth_headers(port),
            payload={
                "request_id": "codexhistoryimport012345678901",
                "selection_handle": selection_handle,
            },
        )
        assert status == 200, body.decode("utf-8")

    assert discovery_calls == [{"project_root": root, "search_term": "release"}]
    assert import_calls[0]["project_root"] == root
    assert import_calls[0]["thread_id"] == "codex-thread-1"
    assert import_calls[0]["source_revision"] == revision


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def __iadd__(self, handler: object) -> "FakeEvent":
        self.handlers.append(handler)
        return self


class FakeWebview:
    def __init__(self) -> None:
        self.closed = FakeEvent()
        self.settings: dict[str, object] = {"WEBVIEW2_RUNTIME_PATH": None}
        self.window_options: dict[str, object] = {}
        self.start_options: dict[str, object] = {}
        self.platform_seen = ""

    def create_window(self, title: str, url: str, **options: object) -> object:
        self.window_options = {"title": title, "url": url, **options}
        return SimpleNamespace(events=SimpleNamespace(closed=self.closed))

    def start(self, **options: object) -> None:
        self.start_options = dict(options)
        self.platform_seen = workbench_module.platform.system()
        parsed = urllib.parse.urlsplit(str(self.window_options["url"]))
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            connection.request("GET", "/")
            assert connection.getresponse().status == 200
        finally:
            connection.close()
        for handler in tuple(self.closed.handlers):
            handler()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows WebView2 contract")
def test_native_workbench_uses_webview2_and_stops_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    server = make_server(
        tmp_path,
        db,
        "scope-a",
        token=TOKEN,
        initial_room_id="alpha",
    )
    fake = FakeWebview()
    runtime = tmp_path / "webview2-runtime"
    runtime.mkdir()
    (runtime / "msedgewebview2.exe").write_bytes(b"runtime")
    monkeypatch.setattr(
        workbench_module,
        "_system_webview2_runtime_path",
        lambda: runtime,
    )
    monkeypatch.setattr(workbench_module.platform, "system", lambda: "WMI-BLOCKED")

    assert run_native_workbench(server, webview_module=fake) == 0
    assert fake.window_options["title"] == "PeerBridge MCP Control Room // LIVE"
    assert fake.window_options["min_size"] == (980, 650)
    assert fake.window_options["text_select"] is True
    assert fake.start_options["gui"] == "edgechromium"
    assert fake.start_options["private_mode"] is True
    assert fake.settings["WEBVIEW2_RUNTIME_PATH"] == str(runtime)
    assert fake.platform_seen == "Windows"
    assert workbench_module.platform.system() == "WMI-BLOCKED"


def test_public_text_redacts_private_macos_temporary_paths() -> None:
    rendered = workbench_module._public_text(
        "/private/var/folders/example/T/peerbridge/private-file.txt"
    )
    assert rendered == "[LOCAL PATH]"


def test_workbench_refuses_external_browser_token_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeServer:
        closed = False

        def server_close(self) -> None:
            self.closed = True

    server = FakeServer()
    monkeypatch.setattr(workbench_module, "make_server", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(workbench_module, "_load_native_webview", lambda: None)

    with pytest.raises(WorkbenchError, match="external browser token launch is disabled"):
        workbench_module.main(
            [
                "--project-root",
                str(tmp_path),
                "--db",
                str(tmp_path / "bridge.sqlite3"),
            ]
        )

    assert server.closed is True


def test_system_webview2_runtime_path_uses_highest_numeric_version(tmp_path: Path) -> None:
    application = tmp_path / "Microsoft" / "EdgeWebView" / "Application"
    for version in ("119.0.1.2", "151.0.4129.93", "SetupMetrics"):
        folder = application / version
        folder.mkdir(parents=True)
        (folder / "msedgewebview2.exe").write_bytes(b"runtime")

    runtime = _system_webview2_runtime_path(search_roots=(tmp_path,))

    assert runtime == (application / "151.0.4129.93").resolve()


def test_system_webview2_runtime_path_reads_windows_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "Microsoft" / "EdgeWebView" / "Application" / "151.0.4129.93"
    runtime.mkdir(parents=True)
    (runtime / "msedgewebview2.exe").write_bytes(b"runtime")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert _system_webview2_runtime_path() == runtime.resolve()


def test_workbench_url_keeps_token_in_fragment(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    server = make_server(
        tmp_path,
        db,
        "scope-a",
        token=TOKEN,
        initial_room_id="alpha",
    )
    try:
        parsed = urllib.parse.urlsplit(workbench_url(server))
        assert parsed.path == "/"
        assert parsed.query == "room_id=alpha"
        assert urllib.parse.parse_qs(parsed.fragment)["access_token"] == [TOKEN]
        assert TOKEN not in parsed.query
    finally:
        server.server_close()


def test_workbench_port_collision_preserves_the_bind_error(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = int(blocker.getsockname()[1])
    try:
        with pytest.raises(OSError):
            make_server(
                tmp_path,
                db,
                "scope-a",
                port=port,
                token=TOKEN,
                initial_room_id="alpha",
            )
    finally:
        blocker.close()


def test_workbench_is_loopback_only_and_static_assets_are_hardened(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    with pytest.raises(WorkbenchError, match="loopback"):
        from peerbridge_mcp.local_workbench import WorkbenchConfig, WorkbenchServer

        WorkbenchServer(
            ("0.0.0.0", 0),
            WorkbenchConfig(tmp_path, db, "scope-a", TOKEN, "alpha", "bad"),
        )

    with running_server(tmp_path, db) as (port, _server):
        status, headers, body = request(port, "GET", "/")
        assert status == 200
        assert b"PeerBridge Workbench" in body
        assert b'id="new-room-dialog"' in body
        assert b'id="managed-session-form"' in body
        assert b'id="workflow-form"' in body
        assert b'id="schedule-form"' in body
        assert b'id="capability-form"' in body
        assert b'id="capability-grant-form"' in body
        assert b'id="permission-form"' in body
        assert b'id="execution-form"' in body
        assert b'id="proof-export"' in body
        assert b'id="proof-verify"' in body
        assert b'id="seat-add"' in body
        assert b'id="seat-remove"' in body
        assert b'id="history-dialog"' in body
        assert b'id="import-history"' in body
        assert b'id="identity-authorize-form"' in body
        assert b'/assets/app.css?v=alpha52-20260825-18' in body
        assert b'/assets/app.js?v=alpha52-20260825-18' in body
        assert b'id="chat-focus-button"' in body
        assert b'id="room-search-button"' in body
        assert b'id="announcement-button"' in body
        assert b'id="agent-runtime-strip"' in body
        assert b'id="worktree-diff-view"' in body
        assert b'id="worktree-diff-refresh"' in body
        assert b'id="appearance-button"' in body
        assert b'id="appearance-dialog"' in body
        assert b'/assets/peerbridge-pixel-preview.png' in body
        assert b'/assets/peerbridge-modern-preview.png' in body
        assert b'id="composer-permission"' in body
        assert b'id="tutorial-button"' in body
        assert b'id="tutorial-dialog"' in body
        assert body.index(b'id="room-search-button"') < body.index(b'id="room-list"')
        assert b'/assets/peerbridge-icon.png' in body
        assert TOKEN.encode() not in body
        assert headers["Content-Security-Policy"].startswith("default-src 'none'")
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Cache-Control"] == "no-store, max-age=0"

        status, _, body = request(port, "GET", "/assets/app.js")
        assert status == 200
        assert b"/api/bootstrap" in body
        assert b"/api/room/create" in body
        assert b"/api/room/member" in body
        assert b"/api/workflow/enqueue" in body
        assert b"/api/session/start" in body
        assert b"/api/session/action" in body
        assert b"/api/preferences/save" in body
        assert b"/api/schedule/save" in body
        assert b"/api/capability/register" in body
        assert b"/api/capability/grant" in body
        assert b"/api/identity/authorize" in body
        assert b"/api/permission/decide" in body
        assert b"/api/execution/create" in body
        assert b"/api/proof/export" in body
        assert b"/api/proof/verify" in body
        assert b'byId("schedule-form").addEventListener("submit", saveSchedule)' in body
        assert b'byId("capability-form").addEventListener("submit", registerCapability)' in body
        assert b'byId("proof-export").addEventListener("click", exportProof)' in body
        assert b"localizedErrorMessage" in body
        assert b"AbortController" in body
        assert b"fetchWithTimeout" in body
        assert b"clipboardImageFiles" in body
        assert b'byId("message-body").addEventListener("paste"' in body
        assert b'byId("managed-input").addEventListener("paste"' in body
        assert b'input.addEventListener("paste"' in body
        assert b'/api/ccswitch/providers' in body
        assert b'/api/provider/save' in body
        assert b'/api/agent/install' in body
        assert b'/api/worktree/diff' in body
        assert b'/api/appearance/save' in body
        assert "本機請求逾時".encode() in body
        assert "本地请求超时".encode() in body
        assert "MCP 訊息通道目前無法使用".encode() in body
        assert "MCP 消息通道当前不可用".encode() in body
        assert b"sessionStorage" in body
        assert b"peerbridge.workbench.accessToken" in body
        assert b'native_compact: "Compact context"' in body
        assert b'native_fork: "Fork session"' in body
        assert b'native_review: "Native review"' in body
        assert b'addActionButton("compact"' in body
        assert b'addActionButton("fork"' in body
        assert b'addActionButton("review"' in body
        assert b'window.confirm(t(confirmationKey))' in body
        assert b'authorization_confirmed: writeCapable' in body
        assert b'full_access_session_confirm' in body
        assert TOKEN.encode() not in body

        status, _, body = request(
            port,
            "GET",
            "/api/worktree/diff",
            headers=auth_headers(port),
        )
        assert status == 200
        diff_payload = json.loads(body)
        assert isinstance(diff_payload["available"], bool)
        assert "files" in diff_payload
        assert TOKEN not in body.decode("utf-8")
        assert str(tmp_path) not in body.decode("utf-8")

        status, headers, body = request(port, "GET", "/assets/peerbridge-icon.png")
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert body.startswith(b"\x89PNG\r\n\x1a\n")

        for preview_name in (
            "peerbridge-pixel-preview.png",
            "peerbridge-modern-preview.png",
        ):
            status, headers, body = request(port, "GET", f"/assets/{preview_name}")
            assert status == 200
            assert headers["Content-Type"] == "image/png"
            assert body.startswith(b"\x89PNG\r\n\x1a\n")
            assert len(body) > 10_000

        status, headers, body = request(port, "GET", "/favicon.ico")
        assert status == 200
        assert headers["Content-Type"] in {"image/vnd.microsoft.icon", "image/x-icon"}
        assert len(body) > 100


def test_workbench_navigation_panels_and_renderers_stay_in_sync() -> None:
    asset_root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "peerbridge_mcp"
        / "workbench"
    )
    html = (asset_root / "index.html").read_text(encoding="utf-8")
    javascript = (asset_root / "app.js").read_text(encoding="utf-8")
    expected = {
        "cockpit": "renderCockpit",
        "chat": "renderMessages",
        "work": "renderOperations",
        "review": "renderReviews",
        "change": "renderChanges",
        "audit": "renderAudit",
        "trust": "renderTrust",
        "connect": "renderConnections",
        "memory": "renderMemory",
        "feedback": "renderSupport",
        "usage": "renderUsage",
        "announcement": "renderSupport",
    }

    navigation = set(re.findall(r'data-view="([a-z]+)"', html))
    panels = set(re.findall(r'id="([a-z]+)-view"\s+class="content-view', html))

    assert navigation == set(expected)
    assert panels == set(expected)
    for renderer in set(expected.values()):
        assert f"function {renderer}()" in javascript
        assert re.search(rf"\b{renderer}\(\)", javascript)
    assert javascript.count("capability_multimodal_input:") == 3
    assert javascript.count("session_action_completed:") == 3
    assert 'byId("managed-session-status").textContent = `${t("session_action_completed")}' in javascript
    assert 'document.querySelector(".content-view.active-view")?.scrollTo({ top: 0' in javascript
    assert 'id="locale-select"' in html
    assert '<span>文 / EN</span>' in html
    assert 'id="ccswitch-form"' in html
    assert 'id="provider-connection-form"' in html
    assert 'id="provider-route-form"' in html
    assert 'id="agent-runtime-strip"' in html
    assert 'id="worktree-diff-view"' in html
    assert 'function agentRuntime(agentId)' in javascript
    assert 'function renderAgentRuntimeStrip()' in javascript
    assert 'function fetchWorktreeDiff(force = false)' in javascript
    assert 'const MAX_DIFF_RENDER_LINES = 4000' in javascript
    assert 'const MAX_MODEL_OPTIONS = 500' in javascript
    assert 'patchLines.slice(0, MAX_DIFF_RENDER_LINES)' in javascript
    assert 'permissionEvidence ? permissionTierLabel(permissionEvidence) : t("unknown")' in javascript
    assert 'recentObservable(latestObservableEvent, ["created_utc"], 180000)' in javascript
    assert workbench_module.PRIMARY_OFFICIAL_AGENT_IDS == frozenset(
        {"codex", "claude-code", "grok", "kimi-code"}
    )
    assert 'state.cockpitMode === "timeline"' in javascript
    assert 'all_session_timeline' in javascript
    assert 'const body = typedBody || (state.attachments.length ? t("attachment_only_message")' in javascript
    assert 'byId("provider-api-key").value = ""' in javascript
    assert 'workflow_implement_review: "實作與審查"' in javascript
    assert 'workflow_investigate_debate: "调查与讨论"' in javascript
    assert 'workflow_release_gate: "Release gate"' in javascript
    assert "workflowLabel(entry)" in javascript
    assert javascript.count("workflowLabel({ workflow_id: entry.workflow_id })") == 2
    assert 'node("pre", "session-terminal session-terminal-preview", t("terminal_not_started"))' not in javascript


def test_modern_workbench_readable_layout_does_not_shrink_sections_into_each_other() -> None:
    stylesheet = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "peerbridge_mcp"
        / "workbench"
        / "app.css"
    ).read_text(encoding="utf-8")

    assert "#cockpit-view.active-view { display: grid;" in stylesheet
    assert "grid-auto-rows: max-content" in stylesheet
    assert "repeat(auto-fit, minmax(min(100%, 340px), 1fr))" in stylesheet
    assert ".capability-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in stylesheet
    assert ".capability-row:nth-child(-n + 2) { border-top: 0; }" in stylesheet
    assert ".session-workspace.mode-grid { min-height: 0;" in stylesheet
    assert ".dormant-terminal-state" in stylesheet
    assert ".field { min-width: 0; display: grid; gap: 7px; color: var(--muted); font-size: 13px; }" in stylesheet
    assert "@media (max-width: 1360px)" in stylesheet


def test_worktree_diff_reports_real_additions_and_deletions(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "peerbridge-test@example.invalid"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "PeerBridge Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    source = tmp_path / "sample.py"
    excluded = tmp_path / ".env"
    source.write_text("alpha\nbeta\n", encoding="utf-8")
    excluded.write_text("PLACEHOLDER=before\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.py", ".env"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    source.write_text("alpha\ngamma\n", encoding="utf-8")
    excluded.write_text("PLACEHOLDER=after\n", encoding="utf-8")

    observed = workbench_module._worktree_diff(tmp_path)

    assert observed["available"] is True
    assert observed["dirty"] is True
    assert observed["file_count"] == 1
    assert observed["additions"] == 1
    assert observed["deletions"] == 1
    assert observed["files"][0]["path"] == "sample.py"
    assert "+gamma" in observed["patch"]
    assert "-beta" in observed["patch"]
    assert ".env" not in observed["patch"]


def test_worktree_diff_resolves_git_outside_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = workbench_module._resolve_git_executable(tmp_path)
    assert trusted is not None
    fake = tmp_path / ("git.exe" if sys.platform == "win32" else "git")
    fake.write_bytes(b"not executable")
    if sys.platform != "win32":
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{trusted.parent}")

    resolved = workbench_module._resolve_git_executable(tmp_path)

    assert resolved == trusted
    assert resolved != fake


def test_worktree_diff_bounds_git_output_before_rendering(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "peerbridge-test@example.invalid"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "PeerBridge Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    source = tmp_path / "many-lines.txt"
    source.write_text("", encoding="utf-8")
    subprocess.run(["git", "add", source.name], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    source.write_text("x\n" * 300_000, encoding="utf-8")

    observed = workbench_module._worktree_diff(tmp_path)

    assert observed["available"] is True
    assert observed["patch_truncated"] is True
    assert len(observed["patch"].encode("utf-8")) <= workbench_module.MAX_WORKTREE_DIFF_BYTES
    assert observed["patch_sha256"] == ""
    assert len(observed["bounded_patch_sha256"]) == 64


def test_provider_model_options_are_bounded_and_deduplicated() -> None:
    models, truncated = workbench_module._bounded_model_ids(
        ["model-0", *[f"model-{index}" for index in range(501)], "model-1"]
    )

    assert len(models) == workbench_module.MAX_PROVIDER_MODEL_OPTIONS
    assert len(set(models)) == len(models)
    assert truncated is True


def test_workbench_command_controls_and_frontend_routes_are_not_orphaned() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "src" / "peerbridge_mcp" / "workbench" / "index.html").read_text(
        encoding="utf-8"
    )
    javascript = (
        root / "src" / "peerbridge_mcp" / "workbench" / "app.js"
    ).read_text(encoding="utf-8")
    server = (root / "src" / "peerbridge_mcp" / "local_workbench.py").read_text(
        encoding="utf-8"
    )

    form_ids = set(re.findall(r'<form[^>]+id="([^"]+)"', html))
    button_ids = set(re.findall(r'<button[^>]+id="([^"]+)"', html))
    for element_id in form_ids - {"proof-form"}:
        assert f'byId("{element_id}")' in javascript, element_id
    for element_id in button_ids - {"ccswitch-save-route"}:
        assert element_id in javascript, element_id

    frontend_routes = set(re.findall(r"/api/[a-z0-9_./-]+", javascript))
    frontend_routes.remove("/api/execution/")
    frontend_routes.update({"/api/execution/seal", "/api/execution/verify"})
    server_routes = set(re.findall(r"/api/[a-z0-9_./-]+", server))
    assert frontend_routes <= server_routes
    assert {
        "create_execution_worktree",
        "decide_permission",
        "export_proof_bundle",
        "grant_capability",
        "list_provider_connections",
        "register_capability",
        "save_workflow_schedule",
        "seal_execution",
        "set_workflow_schedule_enabled",
        "upsert_provider_connection",
        "upsert_route_profile",
        "verify_audit_chain",
        "verify_execution_source",
        "verify_proof_bundle",
    } <= set(MCP_HUMAN_CLIENT_TOOLS)


def test_history_discovery_never_preselects_conversations() -> None:
    javascript = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "peerbridge_mcp"
        / "workbench"
        / "app.js"
    ).read_text(encoding="utf-8")
    discover_start = javascript.index("async function discoverNativeHistory")
    discover_end = javascript.index("async function addSeat", discover_start)
    discover = javascript[discover_start:discover_end]
    import_start = javascript.index("async function importHistory")
    import_end = javascript.index("function updateHistorySourceControls", import_start)
    importer = javascript[import_start:import_end]

    assert 'checkbox.type = "checkbox"' in discover
    assert "checkbox.checked" not in discover
    assert 'querySelectorAll(\'input[type="checkbox"]:checked\')' in importer
    assert "if (!selectedSessions.length" in importer
    assert "selectedSessions.length > 20" in importer
    assert "function updateHistorySubmitState" in importer
    assert 'checkbox.addEventListener("change", updateHistorySubmitState)' in discover
    assert 'byId("history-submit").disabled = selected === 0' in importer


def test_official_agent_catalog_separates_native_contract_from_verified_mapping() -> None:
    native = workbench_module._native_agent_contract(
        "claude-code",
        installed=True,
        observe_dependencies_ready=True,
    )
    mappings = workbench_module._capability_rows(
        "claude-code",
        {
            "real_inference_verified": True,
            "mcp_tool_verified": False,
            "persistent_session_verified": False,
            "observed_model": "claude-test-model",
            "observed_reasoning": "",
        },
        installed=True,
        observe_dependencies_ready=True,
    )
    statuses = {row["capability_id"]: row["status"] for row in mappings}
    tiers = workbench_module._permission_tiers(
        "claude-code",
        installed=True,
        observe_dependencies_ready=True,
    )
    tier_status = {row["tier_id"]: row for row in tiers}

    assert native == {
        "client_origin": "official",
        "transport": "direct_official_cli",
        "managed_session_mode": "persistent",
        "input_transport": "official_persistent_protocol",
        "read_only_profile_ready": True,
        "model_route_configurable": True,
        "resume_mapped": True,
        "attachment_input": "configured",
        "write_profile": "governed_worktree",
    }
    assert statuses["real_inference"] == "verified"
    assert statuses["model_selection"] == "verified"
    assert statuses["mcp_tools"] == "not_verified"
    assert statuses["persistent_session"] == "not_verified"
    assert statuses["session_resume"] == "configured"
    assert statuses["file_edit"] == "gated"
    assert tier_status["observe"]["launchable"] is True
    assert tier_status["review"]["launchable"] is True
    assert tier_status["edit"]["launchable"] is True
    assert tier_status["edit"]["requires_governance_binding"] is True
    assert tier_status["edit"]["network_access"] is True
    assert tier_status["edit"]["approval_behavior"] == "provider-native-standard"
    assert (
        tier_status["edit"]["security_boundary"]
        == "claude_accept_edits_native_policy"
    )
    assert tier_status["full-development"]["launchable"] is True
    assert tier_status["full-development"]["network_access"] is True
    assert tier_status["full-development"]["approval_behavior"] == "once-per-session"
    assert (
        tier_status["full-development"]["security_boundary"]
        == "claude_bypass_permissions_session_trusted"
    )
    assert tier_status["observe"]["network_access"] is False
    assert tier_status["review"]["network_access"] is False

    wsl_tiers = workbench_module._permission_tiers(
        "claude-code",
        installed=True,
        observe_dependencies_ready=True,
        wsl_sandbox_verified=True,
    )
    wsl_by_id = {row["tier_id"]: row for row in wsl_tiers}
    assert wsl_by_id["edit"]["launchable"] is True
    assert wsl_by_id["full-development"]["launchable"] is True
    assert wsl_by_id["edit"]["status"] == "gated"
    assert (
        wsl_by_id["edit"]["security_boundary"]
        == "claude_accept_edits_native_policy"
    )
    assert wsl_by_id["edit"]["unavailable_reason"] is None
    assert wsl_by_id["edit"]["network_access"] is True

    codex_tiers = workbench_module._permission_tiers(
        "codex",
        installed=True,
        observe_dependencies_ready=True,
    )
    codex_by_id = {row["tier_id"]: row for row in codex_tiers}
    assert codex_by_id["edit"]["launchable"] is True
    assert (
        codex_by_id["edit"]["security_boundary"]
        == "codex_workspace_write_on_request"
    )


def test_cockpit_presence_counts_latest_live_agent_identity_once() -> None:
    rows = [
        {"agent_id": "codex", "session_id": "old", "last_seen_epoch": 700.0},
        {"agent_id": "codex", "session_id": "new", "last_seen_epoch": 995.0},
        {"agent_id": "claude", "session_id": "live", "last_seen_epoch": 990.0},
        {"agent_id": "stale", "session_id": "stale", "last_seen_epoch": 100.0},
    ]

    observed = workbench_module._latest_live_presence(
        rows,
        now_epoch=1_000.0,
        ttl_seconds=120.0,
    )

    assert [(row["agent_id"], row["session_id"]) for row in observed] == [
        ("claude", "live"),
        ("codex", "new"),
    ]


def test_usage_period_preserves_bounded_period_specific_breakdowns() -> None:
    observed = workbench_module._safe_usage_period(
        {
            "period": "7d",
            "granularity": "day",
            "totals": {"total_tokens": 30, "input_tokens": 20, "output_tokens": 10},
            "by_provider": [{"provider_id": "provider-a", "total_tokens": 30}],
            "by_model": [
                {
                    "provider_id": "provider-a",
                    "model_id": "model-a",
                    "total_tokens": 30,
                }
            ],
            "trend": [
                {
                    "period_label": "2026-08-22",
                    "period_key": "2026-08-22",
                    "input_tokens": 20,
                    "output_tokens": 10,
                }
            ],
        }
    )

    assert observed["period"] == "7d"
    assert observed["total_tokens"] == 30
    assert observed["providers"][0]["provider_id"] == "provider-a"
    assert observed["models"][0]["model_id"] == "model-a"
    assert observed["trend"][0]["input_tokens"] == 20


def test_usage_page_has_period_switch_and_four_line_trend() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "peerbridge_mcp"
        / "workbench"
        / "app.js"
    ).read_text(encoding="utf-8")
    html = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "peerbridge_mcp"
        / "workbench"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert 'usagePeriod: "30d"' in source
    assert 'state.usagePeriod = key; renderUsage()' in source
    assert source.count('["input_tokens", t("input")') == 1
    assert '["output_tokens", t("output")' in source
    assert '["cached_input_tokens", t("cache")' in source
    assert '["reasoning_tokens", t("reasoning")' in source
    assert 'id="usage-trend"' in html
    assert 'id="usage-providers"' in html


def test_feedback_secret_is_removed_before_network_io() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "peerbridge_mcp"
        / "workbench"
        / "app.js"
    ).read_text(encoding="utf-8")
    start = source.index("async function submitFeedback")
    end = source.index("async function refreshAnnouncements", start)
    function = source[start:end]
    clear_index = function.index('credentialInput.value = ""')
    fetch_index = function.index('await fetch("/api/feedback"')
    assert clear_index < fetch_index
    assert 'finally { requestBody = ""; credential = "";' in function


def test_bootstrap_requires_token_and_expected_host(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    with running_server(tmp_path, db) as (port, _server):
        status, _, _ = request(port, "GET", "/api/bootstrap?room_id=alpha")
        assert status == 401

        status, _, _ = request(
            port,
            "GET",
            "/api/bootstrap?room_id=alpha",
            headers={"Authorization": f"Bearer {TOKEN}", "Host": "attacker.example"},
        )
        assert status == 403

        status, headers, body = request(
            port,
            "GET",
            "/api/bootstrap?room_id=alpha",
            headers=auth_headers(port),
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["schema"] == "peerbridge.local-workbench.v1"
        assert payload["room_id"] == "alpha"
        assert {row["agent_id"] for row in payload["members"]} >= {
            "human-operator",
            "grok-relay",
        }
        assert len(payload["messages"]) == 1
        assert len(payload["dispatches"]) == 1
        assert payload["dispatches"][0]["agent_id"] == "grok-relay"
        assert payload["counts"]["dispatches"] == 1
        assert len(payload["tasks"]) == 1
        assert payload["tasks"][0]["task_id"] == "release-readiness"
        assert len(payload["work_updates"]) == 1
        assert payload["work_updates"][0]["status"] == "review"
        assert TOKEN not in body.decode("utf-8")
        assert str(tmp_path) not in body.decode("utf-8")
        assert "lease_token" not in body.decode("utf-8")
        assert "claimed_session_id" not in body.decode("utf-8")
        assert "artifact_paths_json" not in body.decode("utf-8")
        assert "endpoint" not in payload["feedback"]
        assert "endpoint" not in payload["announcement_state"]
        assert len(payload["signature"]) == 64

        status, _, body = request(
            port,
            "GET",
            "/api/bootstrap?room_id=alpha",
            headers=auth_headers(port, **{"If-None-Match": headers["ETag"]}),
        )
        assert status == 304
        assert body == b""


def test_message_send_checks_origin_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    calls: list[dict[str, object]] = []

    def fake_send(self: McpHumanClient, **kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {"message_id": "message-1", "content_sha256": "a" * 64}

    monkeypatch.setattr(McpHumanClient, "send_message", fake_send)

    with running_server(tmp_path, db) as (port, _server):
        status, _, _ = request(
            port,
            "POST",
            "/api/message",
            headers={"Authorization": f"Bearer {TOKEN}", "Origin": "http://evil.test"},
            payload=message_payload(),
        )
        assert status == 403
        assert calls == []

        status, _, body = request(
            port,
            "POST",
            "/api/message",
            headers=auth_headers(port),
            payload=message_payload(),
        )
        assert status == 201
        first = json.loads(body)
        assert first["status"] == "sent"
        assert len(calls) == 1
        assert calls[0]["room_id"] == "alpha"

        status, _, body = request(
            port,
            "POST",
            "/api/message",
            headers=auth_headers(port),
            payload=message_payload(),
        )
        assert status == 200
        assert json.loads(body) == first
        assert len(calls) == 1

        status, _, body = request(
            port,
            "POST",
            "/api/message",
            headers=auth_headers(port),
            payload=message_payload("Different content."),
        )
        assert status == 400
        assert "reused with different content" in json.loads(body)["error"]
        assert len(calls) == 1


def test_workbench_saves_built_in_desktop_appearance(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    payload = {
        "request_id": "appearance0123456789abcdef012345",
        "surface": "pixel",
    }
    with running_server(tmp_path, db) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/appearance/save",
            headers=auth_headers(port),
            payload=payload,
        )
        assert status == 200
        response = json.loads(body)
        assert response["selected"] == "pixel"
        assert response["restart_required"] is True
        assert workbench_module.load_preferences(tmp_path)["theme"] == "pixel"

        replay_status, _, replay_body = request(
            port,
            "POST",
            "/api/appearance/save",
            headers=auth_headers(port),
            payload=payload,
        )
        assert replay_status == 200
        assert json.loads(replay_body) == response

        status, _, body = request(
            port,
            "GET",
            "/api/bootstrap?room_id=alpha",
            headers=auth_headers(port),
        )
        assert status == 200
        assert json.loads(body)["appearance"]["selected"] == "pixel"


def test_workbench_persists_modern_locale_and_tutorial_completion(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    payload = {
        "request_id": "preferences0123456789abcdef01234",
        "locale": "en",
        "tutorial_completed": True,
    }

    with running_server(tmp_path, db) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/preferences/save",
            headers=auth_headers(port),
            payload=payload,
        )

        assert status == 200
        response = json.loads(body)
        assert response == {
            "status": "saved",
            "locale": "en",
            "tutorial_completed": True,
        }
        preferences = workbench_module.load_preferences(tmp_path)
        assert preferences["locale"] == "en"
        assert preferences["tutorial_completed"] is True

        status, _, body = request(
            port,
            "GET",
            "/api/bootstrap?room_id=alpha",
            headers=auth_headers(port),
        )
        assert status == 200
        appearance = json.loads(body)["appearance"]
        assert appearance["locale"] == "en"
        assert appearance["tutorial_completed"] is True


def test_workbench_resolves_one_exact_adapter_approval(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    resolved: list[tuple[str, str]] = []

    class Session:
        working_directory = tmp_path

        def snapshot(self) -> dict[str, object]:
            return {
                "session_id": "approval-session-one",
                "agent_id": "codex",
                "display_name": "Codex",
                "role": "implementer",
                "state": "running",
                "input_mode": "persistent",
                "can_submit_input": False,
                "permission_tier": "review",
                "approval_mode": "approval-required",
                "approval_broker": {"pending": [], "history": []},
                "session_contract": {},
                "usage": {},
                "events": [],
            }

        def resolve_approval(
            self, approval_id: str, decision: str
        ) -> dict[str, object]:
            resolved.append((approval_id, decision))
            return {"approval_id": approval_id, "decision": decision}

    class Manager:
        def get(self, session_id: str) -> Session:
            assert session_id == "approval-session-one"
            return Session()

        def snapshots(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

        def close(self) -> None:
            return

    with running_server(tmp_path, db, managed_agent_manager=Manager()) as (
        port,
        _server,
    ):
        status, _, body = request(
            port,
            "POST",
            "/api/session/action",
            headers=auth_headers(port),
            payload={
                "request_id": "resolveapproval0123456789abcdef",
                "session_id": "approval-session-one",
                "action": "approval",
                "approval_id": "approval-one",
                "approval_decision": "allow-once",
            },
        )

    assert status == 200
    assert json.loads(body)["result"]["approval_mode"] == "approval-required"
    assert resolved == [("approval-one", "allow-once")]


def test_health_exposes_identity_hash_not_access_token(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    with running_server(tmp_path, db) as (port, _server):
        status, _, body = request(port, "GET", "/healthz")
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "ok"
        assert payload["transport"] == "loopback"
        assert payload["instance_id"] == "test-workbench"
        assert payload["token_sha256"] != TOKEN
        assert TOKEN.encode() not in body


def test_feedback_submission_uses_sealed_pipeline_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    calls: list[dict[str, object]] = []
    config = SimpleNamespace(
        endpoint="https://support.example/v1/feedback",
        support_email=None,
        encrypted_secret_available=True,
        recipient_label="PeerBridge Support",
        privacy_url="https://support.example/privacy",
    )

    monkeypatch.setattr(
        workbench_module.FeedbackConfig,
        "load",
        classmethod(lambda _cls: config),
    )

    def fake_create(root: Path, **kwargs: object) -> SimpleNamespace:
        attachments = list(kwargs["attachment_paths"])
        calls.append(
            {
                "root": root,
                "summary": kwargs["summary"],
                "credential_input": kwargs["credential_input"],
                "include_encrypted_credential": kwargs[
                    "include_encrypted_credential"
                ],
                "attachment_bytes": [path.read_bytes() for path in attachments],
            }
        )
        return SimpleNamespace(
            case_id="PB-TEST-001",
            sha256="a" * 64,
            encrypted_secret_included=True,
        )

    monkeypatch.setattr(workbench_module, "create_feedback_bundle", fake_create)
    monkeypatch.setattr(
        workbench_module,
        "deliver_feedback_bundle",
        lambda _bundle, _config: {
            "delivered": True,
            "notification_sent": True,
        },
    )
    attachment = base64.b64encode(b"safe diagnostic").decode("ascii")
    payload = {
        "request_id": "feedbackrequest0123456789abcdef",
        "summary": "Provider parser failure",
        "message": "The provider rejected a valid local configuration.",
        "contact": "operator@example.test",
        "locale": "en",
        "credential_input": "unit-test-provider-secret",
        "include_encrypted_credential": True,
        "attachments": [{"name": "diagnostic.txt", "content_base64": attachment}],
    }

    with running_server(tmp_path, db) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/feedback",
            headers=auth_headers(port),
            payload=payload,
        )
        assert status == 201
        receipt = json.loads(body)
        assert receipt["case_id"] == "PB-TEST-001"
        assert receipt["delivered"] is True
        assert receipt["encrypted_credential_included"] is True
        assert "unit-test-provider-secret" not in body.decode("utf-8")
        assert calls == [
            {
                "root": tmp_path,
                "summary": "Provider parser failure",
                "credential_input": "unit-test-provider-secret",
                "include_encrypted_credential": True,
                "attachment_bytes": [b"safe diagnostic"],
            }
        ]

        status, _, second_body = request(
            port,
            "POST",
            "/api/feedback",
            headers=auth_headers(port),
            payload=payload,
        )
        assert status == 200
        assert json.loads(second_body) == receipt
        assert len(calls) == 1


def test_announcement_refresh_updates_redacted_bootstrap_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    cached: list[Announcement] = []
    preferences = {
        "network_enabled": True,
        "popup_enabled": False,
        "read_ids": [],
        "cursors": {
            "en": "1970-01-01T00:00:00Z",
            "zh-Hans": "1970-01-01T00:00:00Z",
            "zh-Hant": "1970-01-01T00:00:00Z",
        },
    }
    config = SimpleNamespace(endpoint="https://news.example/v1/announcements")
    announcement = Announcement(
        announcement_id="alpha-52",
        locale="zh-Hant",
        title="Alpha 5.2 已推出",
        body="請從更新頁取得已簽署套件。",
        severity="info",
        link_url="https://news.example/releases/alpha-52",
        published_utc="2026-08-22T02:00:00Z",
        expires_utc=None,
    )
    monkeypatch.setattr(
        workbench_module.AnnouncementConfig,
        "load",
        classmethod(lambda _cls: config),
    )
    monkeypatch.setattr(
        workbench_module,
        "load_announcement_preferences",
        lambda _root: preferences,
    )
    monkeypatch.setattr(
        workbench_module,
        "load_announcement_cache",
        lambda _root: tuple(cached),
    )
    monkeypatch.setattr(
        workbench_module,
        "fetch_announcements",
        lambda _config, **_kwargs: (announcement,),
    )

    def fake_save_cache(_root: Path, rows: object) -> tuple[Announcement, ...]:
        cached[:] = list(rows)
        return tuple(cached)

    monkeypatch.setattr(workbench_module, "save_announcement_cache", fake_save_cache)
    def fake_save_preferences(_root: Path, **kwargs: object) -> dict[str, object]:
        preferences.update(kwargs)
        return preferences

    monkeypatch.setattr(workbench_module, "save_announcement_preferences", fake_save_preferences)

    with running_server(tmp_path, db) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/announcements/refresh",
            headers=auth_headers(port),
            payload={
                "request_id": "announcementrequest0123456789",
                "locale": "zh-Hant",
            },
        )
        assert status == 200
        assert json.loads(body)["received"] == 1

        status, _, body = request(
            port,
            "GET",
            "/api/bootstrap?room_id=alpha",
            headers=auth_headers(port),
        )
        assert status == 200
        payload = json.loads(body)
        rows = payload["announcement_state"]["announcements"]
        assert rows[0]["announcement_id"] == "alpha-52"
        assert rows[0]["title"] == "Alpha 5.2 已推出"
        assert "news.example/v1/announcements" not in body.decode("utf-8")

        status, _, body = request(
            port,
            "POST",
            "/api/announcements/read",
            headers=auth_headers(port),
            payload={
                "request_id": "announcementread0123456789abcdef",
                "locale": "zh-Hant",
            },
        )
        assert status == 200
        assert json.loads(body)["status"] == "read"
        assert preferences["read_ids"]


def test_agent_install_request_replay_launches_one_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    launches: list[tuple[str, bool]] = []
    monkeypatch.setattr(workbench_module, "managed_agent_catalog", lambda *_a, **_k: [])
    monkeypatch.setattr(
        workbench_module,
        "installable_agent_spec",
        lambda agent_id: SimpleNamespace(
            automatic_install_supported=True,
            display_name="ACPX",
            publisher="Agent Client Protocol",
        ),
    )
    monkeypatch.setattr(
        workbench_module,
        "launch_agent_installer",
        lambda agent_id, update=False: launches.append((agent_id, update)) or SimpleNamespace(pid=4242),
    )
    payload = {
        "request_id": "agentinstallreplay0123456789abcdef",
        "agent_id": "acpx-runtime",
        "confirmed": True,
        "update": False,
    }

    with running_server(tmp_path, db) as (port, _server):
        first = request(port, "POST", "/api/agent/install", headers=auth_headers(port), payload=payload)
        second = request(port, "POST", "/api/agent/install", headers=auth_headers(port), payload=payload)

    assert first[0] == 202
    assert second[0] == 200
    assert json.loads(first[2]) == json.loads(second[2])
    assert launches == [("acpx-runtime", False)]


def test_room_controls_call_only_the_explicit_human_client_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        McpHumanClient,
        "set_room_automation",
        lambda _self, **kwargs: calls.append(("automation", kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        McpHumanClient,
        "set_room_member_role",
        lambda _self, **kwargs: calls.append(("role", kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        McpHumanClient,
        "control_discussion",
        lambda _self, **kwargs: calls.append(("discussion", kwargs)) or {"ok": True},
    )
    actions = [
        (
            "/api/room/automation",
            {
                "request_id": "automationrequest0123456789",
                "room_id": "alpha",
                "mode": "discussion",
                "max_rounds": 4,
                "max_messages": 40,
                "stagnation_rounds": 2,
            },
        ),
        (
            "/api/room/member-role",
            {
                "request_id": "memberrolerequest0123456789",
                "room_id": "alpha",
                "agent_id": "grok-relay",
                "role_id": "reviewer",
                "role_label": "",
            },
        ),
        (
            "/api/discussion/control",
            {
                "request_id": "discussionrequest0123456789",
                "discussion_id": "discussion-alpha",
                "action": "continue",
                "extra_rounds": 2,
            },
        ),
    ]
    with running_server(tmp_path, db) as (port, _server):
        for path, payload in actions:
            status, _, body = request(
                port,
                "POST",
                path,
                headers=auth_headers(port),
                payload=payload,
            )
            assert status == 200, body.decode("utf-8")
    assert [name for name, _ in calls] == ["automation", "role", "discussion"]


def test_workbench_room_workflow_and_managed_session_actions_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        McpHumanClient,
        "create_room",
        lambda _self, **kwargs: calls.append(("create", kwargs)) or dict(kwargs),
    )
    monkeypatch.setattr(
        McpHumanClient,
        "join_room",
        lambda _self, **kwargs: calls.append(("join", kwargs)) or dict(kwargs),
    )
    monkeypatch.setattr(
        McpHumanClient,
        "leave_room",
        lambda _self, **kwargs: calls.append(("leave", kwargs)) or dict(kwargs),
    )
    monkeypatch.setattr(
        McpHumanClient,
        "enqueue_workflow",
        lambda _self, **kwargs: calls.append(("enqueue", kwargs)) or dict(kwargs),
    )
    monkeypatch.setattr(
        McpHumanClient,
        "cancel_operation",
        lambda _self, **kwargs: calls.append(("cancel", kwargs)) or dict(kwargs),
    )

    class FakeSession:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            self.state = "running"
            self.input_submitted = False

        def submit(self, text: str) -> None:
            assert text == "Inspect the release boundary."
            self.input_submitted = True

        def interrupt(self) -> None:
            self.state = "stopping"

        def stop(self) -> None:
            self.state = "stopped"

        def snapshot(self) -> dict[str, object]:
            return {
                "session_id": self.session_id,
                "agent_id": "codex",
                "display_name": "OpenAI Codex",
                "client_name": "codex",
                "client_version": "test",
                "role": "reviewer",
                "working_directory": str(tmp_path),
                "state": self.state,
                "return_code": 0 if self.state == "stopped" else None,
                "started_utc": "2026-08-22T00:00:00Z",
                "ended_utc": "2026-08-22T00:00:01Z" if self.state == "stopped" else "",
                "input_submitted": self.input_submitted,
                "input_mode": "single",
                "can_submit_input": self.state == "running" and not self.input_submitted,
                "session_contract": {
                    "mode": "one_shot",
                    "input_transport": "stdin_once",
                    "additional_input_supported": False,
                    "resume_supported": False,
                    "process_terminal_after_turn": True,
                },
                "requested_route": "gpt-5.3-codex",
                "observed_route": "",
                "observed_route_source": "",
                "model_id": "",
                "model_source": "",
                "usage": {},
                "terminal_outcome": "stopped" if self.state == "stopped" else "",
                "execution_mode": "observe",
                "permission_tier": "review",
                "latest_sequence": 1,
                "events": [
                    {
                        "sequence": 1,
                        "created_utc": "2026-08-22T00:00:00Z",
                        "stream": "system",
                        "kind": "system",
                        "text": "Managed test session started.",
                    }
                ],
            }

    class FakeManager:
        def __init__(self) -> None:
            self.sessions: dict[str, FakeSession] = {}
            self.closed = False

        def start(self, launch: object, *, input_text: str | None = None) -> FakeSession:
            session = FakeSession(str(getattr(launch, "session_id")))
            self.sessions[session.session_id] = session
            if input_text is not None:
                session.submit(input_text)
            return session

        def get(self, session_id: str) -> FakeSession:
            return self.sessions[session_id]

        def snapshots(self) -> list[dict[str, object]]:
            return [session.snapshot() for session in self.sessions.values()]

        def close(self) -> None:
            self.closed = True

    manager = FakeManager()
    monkeypatch.setattr(
        workbench_module,
        "build_observe_launch",
        lambda agent_id, **kwargs: SimpleNamespace(
            agent_id=agent_id,
            session_id=kwargs["session_id"],
        ),
    )
    actions = [
        (
            "/api/room/create",
            {
                "request_id": "createroomrequest0123456789",
                "room_id": "beta",
                "name": "Beta Room",
            },
        ),
        (
            "/api/room/member",
            {
                "request_id": "joinroomrequest01234567890",
                "action": "join",
                "room_id": "alpha",
                "agent_id": "grok-relay",
                "route_profile_id": "grok-high",
                "role_id": "reviewer",
                "role_label": "",
            },
        ),
        (
            "/api/room/member",
            {
                "request_id": "leaveroomrequest0123456789",
                "action": "leave",
                "room_id": "alpha",
                "agent_id": "grok-relay",
                "route_profile_id": "",
                "role_id": "equal-participant",
                "role_label": "",
            },
        ),
        (
            "/api/workflow/enqueue",
            {
                "request_id": "enqueueworkflow0123456789",
                "workflow_id": "release-gate",
                "task_text": "Review the Alpha 5.2 candidate.",
                "max_attempts": 2,
                "timeout_seconds": 900,
            },
        ),
        (
            "/api/operation/cancel",
            {
                "request_id": "canceloperation0123456789",
                "operation_id": "operation-alpha",
                "reason": "Operator cancelled the test.",
            },
        ),
    ]
    with running_server(
        tmp_path,
        db,
        managed_agent_manager=manager,
    ) as (port, _server):
        for path, payload in actions:
            status, _, body = request(
                port,
                "POST",
                path,
                headers=auth_headers(port),
                payload=payload,
            )
            assert status == 200, body.decode("utf-8")
            assert str(tmp_path) not in body.decode("utf-8")

        status, _, body = request(
            port,
            "POST",
            "/api/session/start",
            headers=auth_headers(port),
            payload={
                "request_id": "editwithoutbinding0123456789012",
                "agent_id": "codex",
                "role": "implementer",
                "permission_tier": "edit",
                "requested_route": "gpt-5.3-codex",
                "working_directory": ".",
                "input_text": "Implement the scoped change.",
                "governance_binding_id": "",
            },
        )
        assert status == 400
        assert b"governed worktree" in body

        status, _, body = request(
            port,
            "POST",
            "/api/session/start",
            headers=auth_headers(port),
            payload={
                "request_id": "startsessionrequest012345678",
                "agent_id": "codex",
                "role": "reviewer",
                "permission_tier": "review",
                "requested_route": "gpt-5.3-codex",
                "working_directory": ".",
                "input_text": "Inspect the release boundary.",
            },
        )
        assert status == 200, body.decode("utf-8")
        started = json.loads(body)["result"]
        session_id = started["session_id"]
        assert started["managed"] is True
        assert started["input_submitted"] is True
        assert started["can_submit_input"] is False
        assert started["permission_tier"] == "review"
        assert started["session_contract"]["mode"] == "one_shot"
        assert started["session_contract"]["resume_supported"] is False
        assert str(tmp_path) not in body.decode("utf-8")

        status, _, body = request(
            port,
            "GET",
            "/api/bootstrap?room_id=alpha",
            headers=auth_headers(port),
        )
        assert status == 200
        bootstrap = json.loads(body)
        assert bootstrap["feature_status"]["managed_session_control"] is True
        assert bootstrap["feature_status"]["same_room_context"] is True
        assert bootstrap["context_policy"] == {
            "enabled": True,
            "scope": "same-room",
            "max_messages": 24,
            "max_chars": 24_000,
            "fanout_root_deduplication": True,
            "cross_room_access": False,
        }
        assert bootstrap["cockpit"]["sessions"][0]["session_id"] == session_id
        assert bootstrap["cockpit"]["events"][0]["summary"] == "Managed test session started."
        assert {row["workflow_id"] for row in bootstrap["workflow_templates"]} == {
            "implement-review",
            "investigate-debate",
            "read-only-audit",
            "release-gate",
        }

        status, _, body = request(
            port,
            "POST",
            "/api/session/action",
            headers=auth_headers(port),
            payload={
                "request_id": "stopsessionrequest0123456789",
                "session_id": session_id,
                "action": "stop",
                "input_text": "",
            },
        )
        assert status == 200, body.decode("utf-8")
        assert json.loads(body)["result"]["terminal"] is True

    assert manager.closed is True
    assert [name for name, _ in calls] == [
        "create",
        "join",
        "leave",
        "enqueue",
        "cancel",
    ]


def test_full_access_uses_one_audited_session_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    worktree = tmp_path / "isolated-worktree"
    worktree.mkdir()
    governance_calls: list[tuple[str, dict[str, object]]] = []

    class FakeGovernance:
        def __init__(self, _bridge: object) -> None:
            pass

        def resolve_launch_binding(self, binding_id: str, agent_id: str) -> dict[str, object]:
            governance_calls.append(
                ("resolve", {"binding_id": binding_id, "agent_id": agent_id})
            )
            return {"worktree_path": str(worktree)}

        def decide_permission(self, **kwargs: object) -> dict[str, object]:
            governance_calls.append(("decide", dict(kwargs)))
            return {
                "decision_id": kwargs["decision_id"],
                "decision_sha256": "a" * 64,
                "expires_epoch": kwargs["expires_epoch"],
            }

        def authorize_permission(self, decision_id: str, **kwargs: object) -> dict[str, object]:
            governance_calls.append(
                ("authorize", {"decision_id": decision_id, **kwargs})
            )
            return {"decision_id": decision_id, "consumed_utc": "2026-08-24T00:00:00Z"}

    class FullAccessSession:
        supports_verified_attachments = False

        def __init__(self, session_id: str, binding_id: str) -> None:
            self.session_id = session_id
            self.governance_binding_id = binding_id
            self.working_directory = worktree
            self.state = "running"
            self.messages: list[str] = []

        def submit(self, text: str) -> None:
            self.messages.append(text)

        def stop(self) -> None:
            self.state = "stopped"

        def snapshot(self) -> dict[str, object]:
            return {
                "session_id": self.session_id,
                "agent_id": "codex",
                "display_name": "OpenAI Codex",
                "client_name": "codex-app-server",
                "client_version": "test",
                "role": "implementer",
                "working_directory": str(worktree),
                "state": self.state,
                "return_code": 0 if self.state == "stopped" else None,
                "started_utc": "2026-08-24T00:00:00Z",
                "ended_utc": "2026-08-24T00:00:01Z" if self.state == "stopped" else "",
                "input_submitted": bool(self.messages),
                "input_mode": "persistent",
                "can_submit_input": self.state == "running",
                "session_contract": {
                    "mode": "persistent",
                    "input_transport": "jsonrpc",
                    "additional_input_supported": True,
                    "resume_supported": True,
                    "process_terminal_after_turn": False,
                },
                "requested_route": "gpt-test",
                "observed_route": "gpt-test",
                "observed_route_source": "test",
                "model_id": "gpt-test",
                "model_source": "test",
                "usage": {},
                "terminal_outcome": "stopped" if self.state == "stopped" else "",
                "execution_mode": "isolated-write",
                "permission_tier": "full-development",
                "governance_binding_id": self.governance_binding_id,
                "latest_sequence": len(self.messages),
                "events": [],
            }

    class FullAccessManager:
        def __init__(self) -> None:
            self.sessions: dict[str, FullAccessSession] = {}

        def start_official(self, **kwargs: object) -> FullAccessSession:
            session = FullAccessSession(
                str(kwargs["session_id"]), str(kwargs["governance_binding_id"])
            )
            self.sessions[session.session_id] = session
            if kwargs.get("input_text"):
                session.submit(str(kwargs["input_text"]))
            return session

        def get(self, session_id: str) -> FullAccessSession:
            return self.sessions[session_id]

        def snapshots(self) -> list[dict[str, object]]:
            return [session.snapshot() for session in self.sessions.values()]

        def close(self) -> None:
            return

    monkeypatch.setattr(workbench_module, "ExecutionGovernance", FakeGovernance)
    manager = FullAccessManager()
    payload = {
        "agent_id": "codex",
        "role": "implementer",
        "permission_tier": "full-development",
        "requested_route": "gpt-test",
        "working_directory": ".",
        "input_text": "Implement inside the isolated worktree.",
        "governance_binding_id": "binding-full-access",
    }
    with running_server(tmp_path, db, managed_agent_manager=manager) as (port, server):
        status, _, body = request(
            port,
            "POST",
            "/api/session/start",
            headers=auth_headers(port),
            payload={"request_id": "fullaccessmissingack01234567", **payload},
        )
        assert status == 400
        assert b"one-time authorization" in body

        status, _, body = request(
            port,
            "POST",
            "/api/session/start",
            headers=auth_headers(port),
            payload={
                "request_id": "fullaccessauthorized0123456",
                **payload,
                "authorization_confirmed": True,
            },
        )
        assert status == 200, body.decode("utf-8")
        started = json.loads(body)["result"]
        session_id = started["session_id"]
        assert started["session_authorization"]["mode"] == "once-per-session"
        assert started["session_authorization"]["permission_tier"] == "full-development"
        assert server.session_authorization(session_id) is not None

        status, _, body = request(
            port,
            "POST",
            "/api/session/action",
            headers=auth_headers(port),
            payload={
                "request_id": "fullaccessfollowup012345678",
                "session_id": session_id,
                "action": "send",
                "input_text": "Run the focused checks.",
                "attachments": [],
            },
        )
        assert status == 200, body.decode("utf-8")
        assert manager.sessions[session_id].messages[-1] == "Run the focused checks."

        server.forget_session_authorization(session_id)
        status, _, body = request(
            port,
            "POST",
            "/api/session/action",
            headers=auth_headers(port),
            payload={
                "request_id": "fullaccessexpired0123456789",
                "session_id": session_id,
                "action": "send",
                "input_text": "This must be rejected.",
                "attachments": [],
            },
        )
        assert status == 400
        assert b"authorization is absent" in body

    assert [name for name, _payload in governance_calls].count("decide") == 1
    assert [name for name, _payload in governance_calls].count("authorize") == 1


def test_workbench_issues_bounded_single_use_identity_authorization(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)

    with running_server(tmp_path, db) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/identity/authorize",
            headers=auth_headers(port),
            payload={
                "request_id": "identityauthorize012345678901",
                "agent_id": "codex-reviewer",
                "profile": "collaborator",
            },
        )
        assert status == 200, body.decode("utf-8")
        result = json.loads(body)["result"]
        assert result["agent_id"] == "codex-reviewer"
        assert result["profile"] == "collaborator"
        assert result["consumed"] is False
        assert result["permission_decision_id"].startswith("identityauth-")

        status, _, body = request(
            port,
            "POST",
            "/api/identity/authorize",
            headers=auth_headers(port),
            payload={
                "request_id": "identityreserved0123456789012",
                "agent_id": "human-operator",
                "profile": "collaborator",
            },
        )
        assert status == 400
        assert b"reserved operator identity" in body

    with sqlite3.connect(db) as connection:
        decision = connection.execute(
            "SELECT task_id, agent_id, action, resource_key, decision, consumed_utc "
            "FROM permission_decisions WHERE decision_id=?",
            (result["permission_decision_id"],),
        ).fetchone()
    assert decision == (
        "identity-issue:codex-reviewer",
        "codex-reviewer",
        "identity.capability.issue",
        "identity-profile:collaborator",
        "allow",
        None,
    )


def test_workbench_revokes_identity_capability_and_records_audit(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)
    capability = ensure_agent_identity_capability(
        tmp_path,
        db,
        "scope-a",
        "revoked-agent",
        allowed_tools=("bridge_status",),
        issued_by="test-control-room",
    )

    with running_server(tmp_path, db) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/identity/revoke",
            headers=auth_headers(port),
            payload={
                "request_id": "identityrevoke0123456789012",
                "capability_id": capability.capability_id,
                "reason": "Operator revoked this test capability.",
            },
        )
        assert status == 200, body.decode("utf-8")
        result = json.loads(body)["result"]
        assert result["revoked"] is True
        assert len(result["audit_chain_sha256"]) == 64

    with sqlite3.connect(db) as connection:
        revoked_utc = connection.execute(
            "SELECT revoked_utc FROM agent_identity_capabilities WHERE capability_id=?",
            (capability.capability_id,),
        ).fetchone()[0]
        event_type = connection.execute(
            "SELECT event_type FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
    assert revoked_utc
    assert event_type == "identity.capability.revoked"


def test_workbench_rejects_default_state_reparse_before_server_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".peerbridge"
    state.mkdir()
    db = state / "peerbridge.sqlite3"
    seed(tmp_path, db)
    monkeypatch.setattr(
        workbench_module,
        "_workbench_path_is_reparse",
        lambda path: Path(path).name == ".peerbridge",
    )
    with pytest.raises(WorkbenchError, match="must not be a link or reparse point"):
        make_server(tmp_path, db, "scope-a", token=TOKEN)
    monkeypatch.setattr(
        workbench_module,
        "_workbench_path_is_reparse",
        lambda path: Path(path).name == "peerbridge.sqlite3",
    )
    with pytest.raises(WorkbenchError, match="database must not cross"):
        make_server(tmp_path, db, "scope-a", token=TOKEN)


def test_workbench_persistent_session_forwards_verified_attachments(
    tmp_path: Path,
) -> None:
    from tests._image_fixtures import PNG

    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)

    class FakePersistentSession:
        supports_verified_attachments = True

        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            self.state = "running"
            self.submissions: list[tuple[str, tuple[object, ...]]] = []
            self.receipts: list[dict[str, object]] = []
            self.vision_receipts: list[dict[str, object]] = []

        def submit(self, text: str, *, attachments: object = ()) -> None:
            rows = tuple(attachments)
            self.submissions.append((text, rows))
            for item in rows:
                target = tmp_path / str(getattr(item, "relative_path"))
                assert target.is_file()
                assert target.stat().st_size == int(getattr(item, "bytes"))
            if rows:
                self.receipts.append(
                    {
                        "provider_id": "codex",
                        "protocol": "codex-app-server-jsonrpc",
                        "delivery_mode": "native_local_image",
                        "status": "transport_accepted",
                        "attachment_count": len(rows),
                        "model_view_confirmed": False,
                        "receipt_sha256": "a" * 64,
                        "attachments": [
                            {
                                "relative_path": str(getattr(item, "relative_path")),
                                "sha256": str(getattr(item, "sha256")),
                                "bytes": int(getattr(item, "bytes")),
                                "media_type": str(getattr(item, "media_type")),
                                "kind": (
                                    "image"
                                    if str(getattr(item, "media_type")).startswith("image/")
                                    else "text"
                                ),
                            }
                            for item in rows
                        ],
                    }
                )

        def interrupt(self) -> None:
            self.state = "stopping"

        def stop(self) -> None:
            self.state = "stopped"

        def start_vision_probe(self) -> str:
            self.vision_receipts.append(
                {
                    "challenge_id": "visionchallenge0123456789abcdef",
                    "provider_id": "codex",
                    "protocol": "codex-app-server-jsonrpc",
                    "delivery_mode": "native_local_image",
                    "provider_identity": "openai-official-codex",
                    "model_id": "gpt-5.3-codex",
                    "client_version": "test",
                    "status": "semantic_image_verified",
                    "model_view_confirmed": True,
                    "image_sha256": "b" * 64,
                    "image_bytes": 1234,
                    "prompt_sha256": "c" * 64,
                    "response_sha256": "d" * 64,
                    "response_present": True,
                    "evaluated_utc": "2026-08-22T00:00:01Z",
                    "receipt_sha256": "e" * 64,
                    "expected_code": "999999",
                }
            )
            return "visionchallenge0123456789abcdef"

        def snapshot(self) -> dict[str, object]:
            return {
                "session_id": self.session_id,
                "agent_id": "codex",
                "display_name": "OpenAI Codex",
                "client_name": "codex",
                "client_version": "test",
                "role": "reviewer",
                "state": self.state,
                "return_code": None,
                "started_utc": "2026-08-22T00:00:00Z",
                "ended_utc": "",
                "input_submitted": bool(self.submissions),
                "input_mode": "persistent",
                "can_submit_input": self.state == "running",
                "session_contract": {
                    "mode": "persistent",
                    "input_transport": "codex-app-server-jsonrpc",
                    "additional_input_supported": True,
                    "resume_supported": True,
                    "process_terminal_after_turn": False,
                    "protocol": "codex-app-server-jsonrpc",
                    "provider_identity": "openai-official-codex",
                },
                "multimodal_capability": {
                    "attachment_input_supported": True,
                    "image_input": "native_local_image",
                    "text_file_input": "verified_path",
                    "model_view_confirmation": bool(self.vision_receipts),
                    "semantic_image_verification": (
                        "semantic_image_verified"
                        if self.vision_receipts
                        else "available"
                    ),
                },
                "attachment_delivery_receipts": list(self.receipts),
                "vision_verification_receipts": list(self.vision_receipts),
                "requested_route": "gpt-5.3-codex",
                "observed_route": "",
                "observed_route_source": "",
                "model_id": "",
                "model_source": "",
                "usage": {},
                "terminal_outcome": "",
                "execution_mode": "observe",
                "permission_tier": "review",
                "latest_sequence": 0,
                "events": [],
            }

    class FakePersistentManager:
        def __init__(self) -> None:
            self.sessions: dict[str, FakePersistentSession] = {}
            self.closed = False

        def start_official(self, **kwargs: object) -> FakePersistentSession:
            session = FakePersistentSession(str(kwargs["session_id"]))
            self.sessions[session.session_id] = session
            attachments = tuple(kwargs.get("attachments") or ())
            input_text = kwargs.get("input_text")
            if input_text is not None or attachments:
                session.submit(str(input_text or ""), attachments=attachments)
            return session

        def get(self, session_id: str) -> FakePersistentSession:
            return self.sessions[session_id]

        def snapshots(self) -> list[dict[str, object]]:
            return [session.snapshot() for session in self.sessions.values()]

        def close(self) -> None:
            self.closed = True

    manager = FakePersistentManager()
    with running_server(
        tmp_path,
        db,
        managed_agent_manager=manager,
    ) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/session/start",
            headers=auth_headers(port),
            payload={
                "request_id": "persistentstart0123456789",
                "agent_id": "codex",
                "role": "reviewer",
                "permission_tier": "review",
                "requested_route": "gpt-5.3-codex",
                "working_directory": ".",
                "input_text": "Review this chart.",
                "attachments": [
                    {
                        "name": "chart.png",
                        "content_base64": base64.b64encode(PNG).decode("ascii"),
                    }
                ],
            },
        )
        assert status == 200, body.decode("utf-8")
        started = json.loads(body)["result"]
        session_id = started["session_id"]
        assert started["can_submit_input"] is True
        assert started["session_contract"]["mode"] == "persistent"
        assert started["multimodal_capability"]["image_input"] == "native_local_image"
        assert started["attachment_delivery_receipts"][0]["attachment_count"] == 1
        assert started["attachment_delivery_receipts"][0]["model_view_confirmed"] is False
        assert str(tmp_path) not in body.decode("utf-8")

        status, _, body = request(
            port,
            "POST",
            "/api/session/action",
            headers=auth_headers(port),
            payload={
                "request_id": "persistentfollowup012345678",
                "session_id": session_id,
                "action": "send",
                "input_text": "",
                "attachments": [
                    {
                        "name": "notes.txt",
                        "content_base64": base64.b64encode(b"safe follow-up notes").decode(
                            "ascii"
                        ),
                    }
                ],
            },
        )
        assert status == 200, body.decode("utf-8")
        followed_up = json.loads(body)["result"]
        assert followed_up["can_submit_input"] is True
        assert len(followed_up["attachment_delivery_receipts"]) == 2
        assert followed_up["attachment_delivery_receipts"][-1]["attachment_count"] == 1
        assert str(tmp_path) not in body.decode("utf-8")

        status, _, body = request(
            port,
            "POST",
            "/api/session/action",
            headers=auth_headers(port),
            payload={
                "request_id": "persistentvision0123456789",
                "session_id": session_id,
                "action": "vision-test",
                "input_text": "",
                "attachments": [],
            },
        )
        assert status == 200, body.decode("utf-8")
        vision_result = json.loads(body)["result"]
        assert (
            vision_result["multimodal_capability"]["semantic_image_verification"]
            == "semantic_image_verified"
        )
        assert vision_result["vision_verification_receipts"][-1]["model_view_confirmed"] is True
        assert "expected_code" not in body.decode("utf-8")
        assert "999999" not in body.decode("utf-8")

    session = manager.sessions[session_id]
    assert [text for text, _rows in session.submissions] == ["Review this chart.", ""]
    assert [len(rows) for _text, rows in session.submissions] == [1, 1]
    assert manager.closed is True


def test_workbench_persistent_session_exposes_native_lifecycle_actions(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db)

    class NativeSession:
        supports_verified_attachments = True

        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            self.state = "running"
            self.actions: list[tuple[str, str | None]] = []

        def submit(self, text: str, *, attachments: object = ()) -> None:
            self.actions.append(("send", text))

        def review(self, instructions: str | None = None) -> None:
            self.actions.append(("review", instructions))

        def compact(self) -> None:
            self.actions.append(("compact", None))

        def fork(self) -> None:
            self.actions.append(("fork", None))

        def interrupt(self) -> None:
            self.actions.append(("interrupt", None))

        def stop(self) -> None:
            self.actions.append(("stop", None))
            self.state = "stopped"

        def resume(self) -> None:
            self.actions.append(("resume", None))
            self.state = "running"

        def snapshot(self) -> dict[str, object]:
            return {
                "session_id": self.session_id,
                "agent_id": "codex",
                "display_name": "OpenAI Codex",
                "client_name": "codex",
                "client_version": "test",
                "role": "reviewer",
                "state": self.state,
                "return_code": 0 if self.state == "stopped" else None,
                "started_utc": "2026-08-22T00:00:00Z",
                "ended_utc": "2026-08-22T00:00:01Z" if self.state == "stopped" else "",
                "input_submitted": bool(self.actions),
                "input_mode": "persistent",
                "can_submit_input": self.state == "running",
                "session_contract": {
                    "mode": "persistent",
                    "input_transport": "codex-app-server-jsonrpc",
                    "additional_input_supported": True,
                    "resume_supported": True,
                    "fork_supported": True,
                    "compact_supported": True,
                    "review_supported": True,
                    "interrupt_supported": True,
                    "process_terminal_after_turn": False,
                },
                "multimodal_capability": {
                    "attachment_input_supported": False,
                    "image_input_supported": False,
                },
                "requested_route": "gpt-test",
                "observed_route": "gpt-test",
                "observed_route_source": "test",
                "model_id": "gpt-test",
                "model_source": "test",
                "usage": {},
                "terminal_outcome": "stopped" if self.state == "stopped" else "",
                "execution_mode": "observe",
                "permission_tier": "review",
                "latest_sequence": len(self.actions),
                "events": [],
            }

    class NativeManager:
        def __init__(self) -> None:
            self.sessions: dict[str, NativeSession] = {}
            self.closed = False

        def start_official(self, **kwargs: object) -> NativeSession:
            session = NativeSession(str(kwargs["session_id"]))
            self.sessions[session.session_id] = session
            input_text = kwargs.get("input_text")
            if input_text is not None:
                session.submit(str(input_text))
            return session

        def get(self, session_id: str) -> NativeSession:
            return self.sessions[session_id]

        def snapshots(self) -> list[dict[str, object]]:
            return [session.snapshot() for session in self.sessions.values()]

        def close(self) -> None:
            self.closed = True

    manager = NativeManager()
    with running_server(tmp_path, db, managed_agent_manager=manager) as (port, _server):
        status, _, body = request(
            port,
            "POST",
            "/api/session/start",
            headers=auth_headers(port),
            payload={
                "request_id": "nativeactionsstart0123456789",
                "agent_id": "codex",
                "role": "reviewer",
                "permission_tier": "review",
                "requested_route": "gpt-test",
                "working_directory": ".",
                "input_text": "Inspect the candidate.",
            },
        )
        assert status == 200, body.decode("utf-8")
        session_id = json.loads(body)["result"]["session_id"]

        for action, text in (
            ("review", "Review the uncommitted changes."),
            ("compact", ""),
            ("fork", ""),
            ("stop", ""),
            ("resume", ""),
        ):
            status, _, body = request(
                port,
                "POST",
                "/api/session/action",
                headers=auth_headers(port),
                payload={
                    "request_id": f"native{action}request0123456789",
                    "session_id": session_id,
                    "action": action,
                    "input_text": text,
                    "attachments": [],
                },
            )
            assert status == 200, body.decode("utf-8")

        status, _, body = request(
            port,
            "POST",
            "/api/session/action",
            headers=auth_headers(port),
            payload={
                "request_id": "nativecompactinvalid0123456789",
                "session_id": session_id,
                "action": "compact",
                "input_text": "must be rejected",
                "attachments": [],
            },
        )
        assert status == 400
        assert b"does not accept input" in body

    session = manager.sessions[session_id]
    assert session.actions == [
        ("send", "Inspect the candidate."),
        ("review", "Review the uncommitted changes."),
        ("compact", None),
        ("fork", None),
        ("stop", None),
        ("resume", None),
    ]
    assert session.state == "running"
    assert manager.closed is True
