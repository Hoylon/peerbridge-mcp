"""Read-only Codex app-server adapter for explicit conversation history import."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_install import find_trusted_executable, official_agent_spec
from .child_environment import build_agent_child_environment
from .conversation_import import import_conversation_export
from .managed_agents import ManagedAgentError
from .process_control import (
    attach_process_tree,
    process_group_popen_kwargs,
    release_process_tree,
    terminate_process_tree,
)
from .secret_scan import redact_secrets


MAX_THREAD_RESULTS = 50
MAX_FRAME_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0
SOURCE_REVISION = re.compile(r"[0-9a-f]{64}\Z")


def _utc(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace(
                "+00:00", "Z"
            )
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, limit: int) -> str:
    return redact_secrets(" ".join(str(value or "").split()))[:limit]


def _canonical_project_root(project_root: Path) -> Path:
    try:
        candidate = Path(project_root)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManagedAgentError("Codex history project is unavailable") from exc
    if not resolved.is_dir():
        raise ManagedAgentError("Codex history project is unavailable")
    return resolved


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _same_directory(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _thread_revision(
    thread: Mapping[str, Any], project_root: Path
) -> str | None:
    raw_cwd = thread.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        return None
    candidate = Path(raw_cwd)
    if not candidate.is_absolute():
        return None
    try:
        bound_cwd = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not bound_cwd.is_dir() or not _same_directory(bound_cwd, project_root):
        return None
    revision_payload = {
        "id": thread.get("id"),
        "session_id": thread.get("sessionId"),
        "cwd": _path_key(project_root),
        "created_at": thread.get("createdAt"),
        "updated_at": thread.get("updatedAt"),
        "recency_at": thread.get("recencyAt"),
        "name": thread.get("name"),
        "preview": thread.get("preview"),
        "model_provider": thread.get("modelProvider"),
    }
    encoded = json.dumps(
        revision_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CodexHistoryAdapter:
    """Short-lived app-server process with no inference or mutation requests."""

    def __init__(
        self,
        *,
        project_root: Path,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.project_root = _canonical_project_root(project_root)
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self._process: subprocess.Popen[bytes] | None = None
        self._frames: queue.Queue[Mapping[str, Any] | None] = queue.Queue(maxsize=512)
        self._next_id = 1

    def __enter__(self) -> "CodexHistoryAdapter":
        executable = find_trusted_executable(official_agent_spec("codex"))
        if executable is None:
            raise ManagedAgentError("official Codex CLI is unavailable")
        environment = build_agent_child_environment(
            "codex", required_path_roots=(executable.parent,)
        )
        self._process = subprocess.Popen(
            (str(executable), "app-server", "--listen", "stdio://"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self.project_root,
            env=environment,
            shell=False,
            close_fds=True,
            bufsize=0,
            **process_group_popen_kwargs(),
        )
        attach_process_tree(self._process)
        threading.Thread(
            target=self._read_frames,
            name="peerbridge-codex-history-reader",
            daemon=True,
        ).start()
        self._request(
            "initialize",
            {
                "clientInfo": {"name": "PeerBridge History Import", "version": "5.2"},
                "capabilities": {"experimentalApi": False},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        return self

    def __exit__(self, *_args: object) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            terminate_process_tree(process, wait_seconds=3)
        if process is not None:
            release_process_tree(process)

    def _read_frames(self) -> None:
        process = self._process
        stream = process.stdout if process is not None else None
        if stream is None:
            self._frames.put(None)
            return
        while True:
            raw = stream.readline(MAX_FRAME_BYTES + 1)
            if not raw:
                self._frames.put(None)
                return
            if len(raw) > MAX_FRAME_BYTES:
                continue
            try:
                value = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, Mapping):
                self._frames.put(value)

    def _send(self, value: Mapping[str, Any]) -> None:
        process = self._process
        stream = process.stdin if process is not None else None
        if stream is None or stream.closed:
            raise ManagedAgentError("Codex history adapter is unavailable")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ) + b"\n"
        stream.write(encoded)
        stream.flush()

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        while True:
            try:
                value = self._frames.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                raise ManagedAgentError(f"Codex history {method} timed out") from exc
            if value is None:
                raise ManagedAgentError("Codex history app-server exited unexpectedly")
            if value.get("id") == request_id:
                if "error" in value:
                    raise ManagedAgentError(f"Codex history {method} was rejected")
                result = value.get("result")
                return dict(result) if isinstance(result, Mapping) else {}
            if isinstance(value.get("id"), int) and isinstance(value.get("method"), str):
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": value["id"],
                        "error": {
                            "code": -32001,
                            "message": "PeerBridge history import is read-only",
                        },
                    }
                )

    def list_threads(self, *, search_term: str = "") -> list[dict[str, Any]]:
        result = self._request(
            "thread/list",
            {
                "limit": MAX_THREAD_RESULTS,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": False,
                "cwd": str(self.project_root),
                "searchTerm": search_term.strip()[:200] or None,
                "useStateDbOnly": True,
            },
        )
        data = result.get("data")
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise ManagedAgentError("Codex history list response is invalid")
        rows: list[dict[str, Any]] = []
        for raw in data[:MAX_THREAD_RESULTS]:
            if not isinstance(raw, Mapping):
                continue
            thread_id = raw.get("id")
            if not isinstance(thread_id, str) or not thread_id.strip():
                continue
            source_revision = _thread_revision(raw, self.project_root)
            if source_revision is None:
                continue
            title = _safe_text(raw.get("name") or raw.get("preview"), 160)
            source = raw.get("source")
            source_label = ""
            if isinstance(source, Mapping):
                source_label = _safe_text(source.get("kind") or source.get("type"), 80)
            elif source:
                source_label = _safe_text(source, 80)
            rows.append(
                {
                    "thread_id": thread_id[:200],
                    "title": title or "Codex conversation",
                    "created_utc": _utc(raw.get("createdAt")),
                    "updated_utc": _utc(raw.get("updatedAt")),
                    "model_provider": _safe_text(raw.get("modelProvider"), 120),
                    "source": source_label,
                    "is_pinned": bool(raw.get("isPinned")),
                    "source_revision": source_revision,
                }
            )
        return rows

    @staticmethod
    def _user_text(content: Any) -> str:
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(str(item["text"]))
        return "\n".join(parts)

    def read_thread_export(self, thread_id: str, *, source_revision: str) -> bytes:
        identifier = str(thread_id or "").strip()
        if not identifier or len(identifier) > 200:
            raise ManagedAgentError("Codex thread identity is invalid")
        expected_revision = str(source_revision or "").strip().lower()
        if SOURCE_REVISION.fullmatch(expected_revision) is None:
            raise ManagedAgentError("Codex thread revision is invalid")
        result = self._request(
            "thread/read", {"threadId": identifier, "includeTurns": True}
        )
        thread = result.get("thread")
        if not isinstance(thread, Mapping) or thread.get("id") != identifier:
            raise ManagedAgentError("Codex history thread response is invalid")
        actual_revision = _thread_revision(thread, self.project_root)
        if actual_revision is None:
            raise ManagedAgentError(
                "Codex conversation is not bound to the selected project"
            )
        if actual_revision != expected_revision:
            raise ManagedAgentError("Codex conversation changed since discovery")
        messages: list[dict[str, Any]] = []
        turns = thread.get("turns")
        if isinstance(turns, Sequence) and not isinstance(turns, (str, bytes)):
            for turn in turns:
                if not isinstance(turn, Mapping):
                    continue
                started = _utc(turn.get("startedAt"))
                completed = _utc(turn.get("completedAt"))
                items = turn.get("items")
                if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                    continue
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    item_type = item.get("type")
                    if item_type == "userMessage":
                        text = self._user_text(item.get("content"))
                        role, timestamp = "user", started
                    elif item_type == "agentMessage":
                        text = str(item.get("text") or "")
                        role, timestamp = "assistant", completed or started
                    else:
                        continue
                    if text.strip():
                        messages.append(
                            {
                                "id": str(item.get("id") or ""),
                                "role": role,
                                "timestamp": timestamp,
                                "content": text,
                            }
                        )
        if not messages:
            raise ManagedAgentError("Codex thread has no importable visible messages")
        export = {
            "source_conversation_id": identifier,
            "title": _safe_text(thread.get("name") or thread.get("preview"), 160),
            "messages": messages,
        }
        return json.dumps(
            export,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def discover_codex_threads(
    *, project_root: Path, search_term: str = ""
) -> list[dict[str, Any]]:
    with CodexHistoryAdapter(project_root=project_root) as adapter:
        return adapter.list_threads(search_term=search_term)


def import_codex_thread(
    *,
    project_root: Path,
    scope: str,
    thread_id: str,
    source_revision: str,
) -> list[dict[str, Any]]:
    with CodexHistoryAdapter(project_root=project_root) as adapter:
        source = adapter.read_thread_export(
            thread_id,
            source_revision=source_revision,
        )

    return import_conversation_export(
        project_root=project_root,
        scope=scope,
        provider="codex",
        source_name=f"codex-app-server-{thread_id}.json",
        content_base64=base64.b64encode(source).decode("ascii"),
    )


__all__ = [
    "CodexHistoryAdapter",
    "discover_codex_threads",
    "import_codex_thread",
]
