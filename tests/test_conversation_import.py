from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import peerbridge_mcp.conversation_import as conversation_import_module

from peerbridge_mcp.conversation_import import (
    ConversationImportError,
    conversation_export_metadata,
    import_conversation_export,
    imported_room_view,
    list_conversation_imports,
    parse_conversation_export,
)


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def test_codex_jsonl_import_preserves_identity_and_excludes_hidden_reasoning() -> None:
    rows = [
        {
            "timestamp": "2026-08-22T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": "codex-thread-1", "title": "Release review"},
        },
        {
            "timestamp": "2026-08-22T01:00:01Z",
            "type": "response_item",
            "payload": {
                "id": "user-1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Check Alpha 5.2"}],
            },
        },
        {
            "timestamp": "2026-08-22T01:00:02Z",
            "type": "response_item",
            "payload": {
                "id": "assistant-1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "reasoning", "text": "private reasoning"},
                    {"type": "output_text", "text": "The gate is ready."},
                ],
            },
        },
    ]
    payload = "\n".join(json.dumps(row) for row in rows).encode()

    result = parse_conversation_export("codex", payload)

    assert len(result) == 1
    assert result[0]["source_conversation_id"] == "codex-thread-1"
    assert [row["role"] for row in result[0]["messages"]] == ["user", "assistant"]
    assert result[0]["messages"][1]["text"] == "The gate is ready."
    assert "private reasoning" not in json.dumps(result)


def test_claude_jsonl_import_groups_sessions_and_redacts_secret() -> None:
    secret = "sk-test-" + ("A" * 40)
    rows = [
        {
            "type": "user",
            "sessionId": "claude-session-1",
            "uuid": "message-1",
            "timestamp": "2026-08-22T02:00:00Z",
            "message": {"role": "user", "content": f"Use {secret}"},
        },
        {
            "type": "assistant",
            "sessionId": "claude-session-1",
            "uuid": "message-2",
            "timestamp": "2026-08-22T02:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "Credential was not persisted."},
                ],
            },
        },
    ]
    payload = "\n".join(json.dumps(row) for row in rows).encode()

    result = parse_conversation_export("claude", payload)

    assert len(result) == 1
    assert result[0]["source_conversation_id"] == "claude-session-1"
    serialized = json.dumps(result)
    assert secret not in serialized
    assert "hidden" not in serialized
    assert result[0]["messages"][0]["secret_redacted"] is True


def test_create_only_import_is_idempotent_and_renders_a_read_only_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export = {
        "source_conversation_id": "grok-conversation-1",
        "title": "Grok review",
        "messages": [
            {
                "id": "user-1",
                "role": "user",
                "timestamp": "2026-08-22T03:00:00Z",
                "content": "Review the release.",
            },
            {
                "id": "assistant-1",
                "role": "assistant",
                "timestamp": "2026-08-22T03:00:01Z",
                "content": "No blocking finding.",
            },
        ],
    }
    content = json.dumps(export).encode()
    arguments = {
        "project_root": tmp_path,
        "scope": "scope-a",
        "provider": "grok",
        "source_name": "conversation.json",
        "content_base64": _encoded(content),
    }

    timestamps = iter(("2026-08-22T03:00:02Z", "2026-08-23T04:00:00Z"))
    monkeypatch.setattr(conversation_import_module, "utc_now", lambda: next(timestamps))
    created = import_conversation_export(**arguments)
    repeated = import_conversation_export(
        **{**arguments, "source_name": "renamed-conversation.json"}
    )
    records = list_conversation_imports(tmp_path, "scope-a")
    room = imported_room_view(records[0], rooms=[], limit=1)

    assert created[0]["status"] == "created"
    assert repeated[0]["status"] == "existing"
    assert len(records) == 1
    assert records[0]["source_name"] == "conversation.json"
    assert records[0]["imported_utc"] == "2026-08-22T03:00:02Z"
    assert room["operator_active"] is False
    assert room["imported_history"]["source_conversation_id"] == "grok-conversation-1"
    assert room["page"]["returned"] == 1
    assert room["page"]["has_older"] is True
    assert room["messages"][0]["sender"] == "grok-history"


def test_history_import_store_has_a_lifetime_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation_import_module, "MAX_IMPORT_RECORDS", 1)
    first = {
        "source_conversation_id": "one",
        "messages": [{"role": "user", "content": "first"}],
    }
    second = {
        "source_conversation_id": "two",
        "messages": [{"role": "user", "content": "second"}],
    }
    common = {
        "project_root": tmp_path,
        "scope": "quota",
        "provider": "generic",
        "source_name": "history.json",
    }
    import_conversation_export(
        **common,
        content_base64=_encoded(json.dumps(first).encode("utf-8")),
    )

    with pytest.raises(ConversationImportError, match="store quota"):
        import_conversation_export(
            **common,
            content_base64=_encoded(json.dumps(second).encode("utf-8")),
        )


def test_file_metadata_selection_imports_only_checked_conversation(
    tmp_path: Path,
) -> None:
    secret_id = "sk-test-" + ("B" * 40)
    export = [
        {
            "source_conversation_id": secret_id,
            "title": "Do not select",
            "messages": [{"role": "user", "content": "first"}],
        },
        {
            "source_conversation_id": "selected-conversation",
            "title": "Selected",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_result", "content": "private tool output"},
                        {"type": "text", "text": "visible answer"},
                    ],
                }
            ],
        },
    ]
    source = json.dumps(export).encode()

    metadata = conversation_export_metadata("generic", source)
    imported = import_conversation_export(
        project_root=tmp_path,
        scope="scope-a",
        provider="generic",
        source_name="multi.json",
        content_base64=_encoded(source),
        selected_conversation_ids=["selected-conversation"],
    )
    records = list_conversation_imports(tmp_path, "scope-a")

    assert [row["selection_id"] for row in metadata] == [
        secret_id,
        "selected-conversation",
    ]
    assert metadata[0]["display_id"].startswith("conversation-redacted-")
    assert len(imported) == 1
    assert len(records) == 1
    assert records[0]["source_conversation_id"] == "selected-conversation"
    assert "private tool output" not in json.dumps(records[0])
    assert records[0]["messages"][0]["text"] == "visible answer"


def test_file_export_rejects_duplicate_conversation_identities(
    tmp_path: Path,
) -> None:
    export = [
        {
            "source_conversation_id": "duplicate-id",
            "messages": [{"role": "user", "content": "first"}],
        },
        {
            "source_conversation_id": "duplicate-id",
            "messages": [{"role": "assistant", "content": "second"}],
        },
    ]
    source = json.dumps(export).encode()

    with pytest.raises(ConversationImportError, match="duplicate conversation"):
        conversation_export_metadata("generic", source)
    with pytest.raises(ConversationImportError, match="duplicate conversation"):
        import_conversation_export(
            project_root=tmp_path,
            scope="scope-a",
            provider="generic",
            source_name="duplicates.json",
            content_base64=_encoded(source),
        )


def test_imported_room_view_collapses_only_truly_identical_messages(
    tmp_path: Path,
) -> None:
    export = {
        "source_conversation_id": "duplicate-display",
        "title": "Duplicate display",
        "messages": [
            {"id": "u-1", "role": "user", "timestamp": 1, "content": "Run the check."},
            {"id": "u-1", "role": "user", "timestamp": 2, "content": "Run the check."},
            {"id": "u-2", "role": "user", "timestamp": 3, "content": "Run the check."},
            {"id": "a-1", "role": "assistant", "timestamp": 4, "content": "Done."},
            {"id": "u-3", "role": "user", "timestamp": 5, "content": "Run the check."},
        ],
    }
    import_conversation_export(
        project_root=tmp_path,
        scope="scope-a",
        provider="generic",
        source_name="duplicates.json",
        content_base64=_encoded(json.dumps(export).encode()),
    )
    record = list_conversation_imports(tmp_path, "scope-a")[0]
    exact_duplicate = dict(record["messages"][0])
    exact_duplicate["sequence"] = len(record["messages"]) + 1
    record["messages"].append(exact_duplicate)

    room = imported_room_view(record, rooms=[], limit=20)

    assert [row["body"] for row in room["messages"]] == [
        "Run the check.",
        "Run the check.",
        "Run the check.",
        "Done.",
        "Run the check.",
    ]
    assert room["page"]["collapsed_duplicate_count"] == 1
    assert room["page"]["has_older"] is False
