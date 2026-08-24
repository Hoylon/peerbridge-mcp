from __future__ import annotations

import json
from pathlib import Path

import peerbridge_mcp.native_history_adapters as adapters
import pytest

from peerbridge_mcp.conversation_import import (
    MAX_IMPORT_BYTES,
    ConversationImportError,
)


def test_claude_metadata_and_selected_import_are_project_scoped(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    history = tmp_path / "claude-project"
    history.mkdir()
    session = history / "claude-session-1.jsonl"
    rows = [
        {
            "type": "user",
            "cwd": str(project.resolve()),
            "sessionId": "claude-session-1",
            "uuid": "user-1",
            "timestamp": "2026-08-22T01:00:00Z",
            "message": {"role": "user", "content": "Review Alpha 5.2"},
        },
        {
            "type": "assistant",
            "cwd": str(project.resolve()),
            "sessionId": "claude-session-1",
            "uuid": "agent-1",
            "timestamp": "2026-08-22T01:00:01Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Ready."}]},
        },
    ]
    session.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    monkeypatch.setattr(adapters, "_claude_project_dir", lambda _root: history)

    listed = adapters.list_claude_sessions(project)
    imported = adapters.import_claude_session(
        project_root=project,
        scope="scope-a",
        session_id="claude-session-1",
        source_revision=listed[0]["source_revision"],
    )

    assert listed[0]["session_id"] == "claude-session-1"
    assert listed[0]["title"] == "Claude conversation"
    assert len(listed[0]["source_revision"]) == 64
    assert imported[0]["provider"] == "claude"
    assert imported[0]["message_count"] == 2

    session.write_text(
        session.read_text(encoding="utf-8")
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "cwd": str(project.resolve()),
                "sessionId": "claude-session-1",
                "uuid": "agent-2",
                "message": {"role": "assistant", "content": "Changed"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConversationImportError, match="changed since discovery"):
        adapters.import_claude_session(
            project_root=project,
            scope="scope-a",
            session_id="claude-session-1",
            source_revision=listed[0]["source_revision"],
        )


def test_claude_history_fails_closed_when_source_project_binding_is_missing_or_wrong(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    history = tmp_path / "claude-collision"
    history.mkdir()
    missing = history / "missing-binding.jsonl"
    missing.write_text(
        json.dumps({"type": "user", "message": {"content": "private"}}),
        encoding="utf-8",
    )
    wrong = history / "wrong-project.jsonl"
    wrong.write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": str(other.resolve()),
                "message": {"content": "other project"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(adapters, "_claude_project_dir", lambda _root: history)

    listed = {
        row["session_id"]: row for row in adapters.list_claude_sessions(project)
    }
    assert set(listed) == {"missing-binding", "wrong-project"}
    for session_id in listed:
        with pytest.raises(ConversationImportError, match="not bound"):
            adapters.import_claude_session(
                project_root=project,
                scope="scope-a",
                session_id=session_id,
                source_revision=listed[session_id]["source_revision"],
            )


def test_grok_official_list_and_selected_history_import(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"grok")
    monkeypatch.setattr(adapters, "find_trusted_executable", lambda _spec: executable)
    monkeypatch.setattr(adapters, "build_agent_child_environment", lambda *_a, **_k: {})
    monkeypatch.setattr(
        adapters,
        "_bounded_process",
        lambda *_a, **_k: (
            0,
            b"SESSION ID                            CREATED     UPDATED     STATUS      SUMMARY\n"
            b"01a029cb-a7db-7c21-afc6-f2407f760a2d  2026-08-22  2026-08-22  local  Release review\n",
            b"",
        ),
    )
    session_root = tmp_path / "grok-sessions" / "01a029cb-a7db-7c21-afc6-f2407f760a2d"
    session_root.mkdir(parents=True)
    history = session_root / "chat_history.jsonl"
    history.write_text(
        json.dumps({"type": "user", "content": "Review release"})
        + "\n"
        + json.dumps({"type": "assistant", "content": "No blocker"}),
        encoding="utf-8",
    )
    (session_root / "summary.json").write_text(
        json.dumps({"generated_title": "Release review"}), encoding="utf-8"
    )
    monkeypatch.setattr(adapters, "_grok_workspace_root", lambda _root: session_root.parent)

    listed = adapters.list_grok_sessions(project)
    imported = adapters.import_grok_session(
        project_root=project,
        scope="scope-a",
        session_id="01a029cb-a7db-7c21-afc6-f2407f760a2d",
        source_revision=listed[0]["source_revision"],
    )

    assert listed[0]["title"] == "Release review"
    assert len(listed[0]["source_revision"]) == 64
    assert imported[0]["provider"] == "grok"
    assert imported[0]["message_count"] == 2

    history.write_text(
        history.read_text(encoding="utf-8")
        + "\n"
        + json.dumps({"type": "assistant", "content": "Changed"}),
        encoding="utf-8",
    )
    with pytest.raises(ConversationImportError, match="changed since discovery"):
        adapters.import_grok_session(
            project_root=project,
            scope="scope-a",
            session_id="01a029cb-a7db-7c21-afc6-f2407f760a2d",
            source_revision=listed[0]["source_revision"],
        )


def test_grok_history_rejects_dotdot_and_prechecks_file_size(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = tmp_path / "grok-sessions" / "encoded-project"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(adapters, "_grok_workspace_root", lambda _root: workspace)

    (workspace.parent / "chat_history.jsonl").write_text(
        json.dumps({"type": "user", "content": "must remain outside"}),
        encoding="utf-8",
    )
    with pytest.raises(ConversationImportError, match="identity is invalid"):
        adapters.import_grok_session(
            project_root=project,
            scope="scope-a",
            session_id="..",
            source_revision="0" * 64,
        )

    oversized_root = workspace / "oversized-session"
    oversized_root.mkdir()
    with (oversized_root / "chat_history.jsonl").open("wb") as handle:
        handle.truncate(MAX_IMPORT_BYTES + 1)
    with pytest.raises(ConversationImportError, match="exceeds the import limit"):
        adapters.import_grok_session(
            project_root=project,
            scope="scope-a",
            session_id="oversized-session",
            source_revision="0" * 64,
        )


def test_kimi_state_index_is_metadata_only_and_selected_wire_is_imported(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / ".kimi-code"
    session = home / "sessions" / "workspace-key" / "kimi-session-1"
    wire = session / "agents" / "main"
    wire.mkdir(parents=True)
    (session / "state.json").write_text(
        json.dumps(
            {
                "session_id": "kimi-session-1",
                "work_dir": str(project),
                "title": "Kimi review",
                "created_at": "2026-08-22T01:00:00Z",
                "updated_at": "2026-08-22T01:00:01Z",
            }
        ),
        encoding="utf-8",
    )
    (wire / "wire.jsonl").write_text(
        json.dumps({"role": "user", "content": "Review release"})
        + "\n"
        + json.dumps({"role": "assistant", "content": "Ready"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(adapters, "_kimi_home", lambda: home)

    listed = adapters.list_kimi_sessions(project)
    imported = adapters.import_kimi_session(
        project_root=project,
        scope="scope-a",
        session_id="kimi-session-1",
        source_revision=listed[0]["source_revision"],
    )

    assert listed[0]["title"] == "Kimi review"
    assert len(listed[0]["source_revision"]) == 64
    assert imported[0]["provider"] == "kimi"
    assert imported[0]["message_count"] == 2

    (wire / "wire.jsonl").write_text(
        (wire / "wire.jsonl").read_text(encoding="utf-8")
        + "\n"
        + json.dumps({"role": "assistant", "content": "Changed"}),
        encoding="utf-8",
    )
    with pytest.raises(ConversationImportError, match="changed since discovery"):
        adapters.import_kimi_session(
            project_root=project,
            scope="scope-a",
            session_id="kimi-session-1",
            source_revision=listed[0]["source_revision"],
        )


def test_kimi_session_without_project_binding_is_not_listed_or_imported(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / ".kimi-code"
    session = home / "sessions" / "workspace-key" / "unbound-session"
    session.mkdir(parents=True)
    (session / "state.json").write_text(
        json.dumps({"session_id": "unbound-session", "title": "Unbound"}),
        encoding="utf-8",
    )
    (session / "context.jsonl").write_text(
        json.dumps({"role": "user", "content": "private"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(adapters, "_kimi_home", lambda: home)

    assert adapters.list_kimi_sessions(project) == []
    with pytest.raises(ConversationImportError, match="unavailable"):
        adapters.import_kimi_session(
            project_root=project,
            scope="scope-a",
            session_id="unbound-session",
            source_revision="0" * 64,
        )


def test_kimi_work_dir_must_be_absolute_and_duplicate_ids_are_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / ".kimi-code"
    relative_session = home / "sessions" / "workspace-a" / "relative-session"
    relative_session.mkdir(parents=True)
    (relative_session / "state.json").write_text(
        json.dumps(
            {
                "session_id": "relative-session",
                "work_dir": "project",
            }
        ),
        encoding="utf-8",
    )
    (relative_session / "context.jsonl").write_text(
        json.dumps({"role": "user", "content": "must remain unavailable"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(adapters, "_kimi_home", lambda: home)
    monkeypatch.chdir(tmp_path)

    assert adapters.list_kimi_sessions(project) == []

    duplicate_a = home / "sessions" / "workspace-a" / "duplicate-a"
    duplicate_b = home / "sessions" / "workspace-b" / "duplicate-b"
    duplicate_a.mkdir()
    duplicate_b.mkdir(parents=True)
    for session in (duplicate_a, duplicate_b):
        (session / "state.json").write_text(
            json.dumps(
                {
                    "session_id": "duplicate-session",
                    "work_dir": str(project.resolve()),
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(ConversationImportError, match="duplicate session"):
        adapters.list_kimi_sessions(project)
    with pytest.raises(ConversationImportError, match="duplicate session"):
        adapters.import_kimi_session(
            project_root=project,
            scope="scope-a",
            session_id="duplicate-session",
            source_revision="0" * 64,
        )
