from __future__ import annotations

import json
from pathlib import Path

import pytest

from peerbridge_mcp.codex_history_adapter import CodexHistoryAdapter
from peerbridge_mcp.managed_agents import ManagedAgentError


def test_codex_history_list_is_exact_project_scoped_and_revision_bound(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    adapter = CodexHistoryAdapter(project_root=project)
    observed_params: dict[str, object] = {}

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        assert method == "thread/list"
        observed_params.update(params)
        return {
            "data": [
                {
                    "id": "thread-1",
                    "sessionId": "session-1",
                    "name": "Release review",
                    "preview": "Private preview is not needed",
                    "createdAt": 1787356800,
                    "updatedAt": 1787356860,
                    "modelProvider": "openai",
                    "source": "appServer",
                    "cwd": str(project.resolve()),
                    "path": "C:/private/rollout.jsonl",
                    "isPinned": True,
                },
                {
                    "id": "thread-other",
                    "sessionId": "session-other",
                    "name": "Other project",
                    "preview": "must not be listed",
                    "createdAt": 1787356800,
                    "updatedAt": 1787356860,
                    "modelProvider": "openai",
                    "source": "appServer",
                    "cwd": str(other.resolve()),
                },
            ]
        }

    adapter._request = request  # type: ignore[method-assign]

    rows = adapter.list_threads()

    assert observed_params["cwd"] == str(project.resolve())
    assert len(rows) == 1
    assert rows[0]["thread_id"] == "thread-1"
    assert rows[0]["title"] == "Release review"
    assert rows[0]["created_utc"] == "2026-08-22T00:00:00Z"
    assert rows[0]["updated_utc"] == "2026-08-22T00:01:00Z"
    assert rows[0]["model_provider"] == "openai"
    assert rows[0]["source"] == "appServer"
    assert rows[0]["is_pinned"] is True
    assert len(rows[0]["source_revision"]) == 64
    assert "private" not in json.dumps(rows).lower()


def test_codex_history_read_keeps_only_visible_user_and_agent_messages(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    thread = {
        "id": "thread-1",
        "sessionId": "session-1",
        "cwd": str(project.resolve()),
        "name": "Release review",
        "preview": "Review Alpha 5.2",
        "createdAt": 1787356800,
        "updatedAt": 1787356802,
        "modelProvider": "openai",
        "turns": [
            {
                "id": "turn-1",
                "startedAt": 1787356800,
                "completedAt": 1787356802,
                "items": [
                    {
                        "id": "user-1",
                        "type": "userMessage",
                        "content": [
                            {"type": "text", "text": "Review Alpha 5.2"},
                            {"type": "localImage", "path": "C:/private/image.png"},
                        ],
                    },
                    {
                        "id": "reasoning-1",
                        "type": "reasoning",
                        "content": ["hidden reasoning"],
                    },
                    {
                        "id": "agent-1",
                        "type": "agentMessage",
                        "text": "The review passed.",
                    },
                    {
                        "id": "command-1",
                        "type": "commandExecution",
                        "aggregatedOutput": "private command output",
                    },
                ],
            }
        ],
    }
    adapter = CodexHistoryAdapter(project_root=project)
    adapter._request = lambda method, params: (  # type: ignore[method-assign]
        {"data": [thread]} if method == "thread/list" else {"thread": thread}
    )
    revision = adapter.list_threads()[0]["source_revision"]

    exported = json.loads(
        adapter.read_thread_export("thread-1", source_revision=revision)
    )

    assert exported["source_conversation_id"] == "thread-1"
    assert [row["role"] for row in exported["messages"]] == ["user", "assistant"]
    serialized = json.dumps(exported)
    assert "hidden reasoning" not in serialized
    assert "private command" not in serialized
    assert "image.png" not in serialized


def test_codex_history_read_rejects_cross_project_or_stale_revision(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    thread: dict[str, object] = {
        "id": "thread-1",
        "sessionId": "session-1",
        "cwd": str(project.resolve()),
        "name": "Release review",
        "preview": "Review",
        "createdAt": 1787356800,
        "updatedAt": 1787356802,
        "modelProvider": "openai",
        "turns": [
            {
                "id": "turn-1",
                "items": [
                    {
                        "id": "user-1",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "Review"}],
                    }
                ],
            }
        ],
    }
    adapter = CodexHistoryAdapter(project_root=project)
    adapter._request = lambda method, params: (  # type: ignore[method-assign]
        {"data": [thread]} if method == "thread/list" else {"thread": thread}
    )
    revision = adapter.list_threads()[0]["source_revision"]

    thread["updatedAt"] = 1787356803
    with pytest.raises(ManagedAgentError, match="changed since discovery"):
        adapter.read_thread_export("thread-1", source_revision=revision)

    thread["updatedAt"] = 1787356802
    thread["cwd"] = str(other.resolve())
    with pytest.raises(ManagedAgentError, match="not bound"):
        adapter.read_thread_export("thread-1", source_revision=revision)
