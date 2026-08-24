"""Create-only, source-bound imports for explicitly selected Agent conversations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import stat
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bridge import stable_sha256, utc_now
from .secret_scan import contains_secret, redact_secrets


IMPORT_SCHEMA = "peerbridge.conversation-history-import.v1"
IMPORT_DIRECTORY = "history-imports"
SUPPORTED_PROVIDERS = frozenset({"codex", "claude", "grok", "kimi", "generic"})
MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_IMPORT_RECORD_BYTES = 32 * 1024 * 1024
MAX_IMPORT_RECORDS = 512
MAX_IMPORT_STORE_BYTES = 256 * 1024 * 1024
MAX_CONVERSATIONS = 128
MAX_MESSAGES_PER_CONVERSATION = 2_000
MAX_TOTAL_MESSAGES = 5_000
MAX_TEXT_CHARS = 20_000
SAFE_PROVIDER = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")


class ConversationImportError(ValueError):
    """An explicitly selected conversation export is invalid or unsafe."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS or SAFE_PROVIDER.fullmatch(provider) is None:
        raise ConversationImportError("conversation provider is unsupported")
    return provider


def _safe_source_name(value: Any) -> str:
    name = Path(str(value or "conversation-export")).name.strip()
    if not name:
        name = "conversation-export"
    redacted = redact_secrets(name)
    return redacted[:240]


def _safe_source_identifier(value: Any, *, prefix: str) -> str:
    identifier = str(value or "").strip()
    if not identifier:
        return ""
    if contains_secret(identifier):
        return f"{prefix}-redacted-{stable_sha256(identifier)[:24]}"
    return redact_secrets(identifier)[:500]


def _decode_payload(value: Any) -> bytes:
    encoded = str(value or "").strip()
    if not encoded:
        raise ConversationImportError("conversation export is empty")
    if len(encoded) > ((MAX_IMPORT_BYTES * 4) // 3) + 64:
        raise ConversationImportError("conversation export exceeds the import limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConversationImportError("conversation export is not canonical base64") from exc
    if not decoded or len(decoded) > MAX_IMPORT_BYTES:
        raise ConversationImportError("conversation export exceeds the import limit")
    return decoded


def _decode_text(value: bytes) -> str:
    try:
        return value.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConversationImportError("conversation export must be UTF-8") from exc


def _timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace(
                "+00:00", "Z"
            )
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _visible_text(value: Any) -> str:
    """Extract visible text while excluding hidden reasoning and tool payloads."""

    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        parts = [_visible_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if not isinstance(value, Mapping):
        return ""
    kind = str(value.get("type") or "").strip().lower()
    if kind in {
        "thinking",
        "reasoning",
        "analysis",
        "tool_use",
        "tool_result",
        "function_call",
        "function_result",
        "command_execution",
        "command_result",
        "computer_initialize_state",
    }:
        return ""
    for key in ("text", "message", "content", "parts"):
        if key in value:
            rendered = _visible_text(value[key])
            if rendered:
                return rendered
    return ""


def _role(value: Any) -> str | None:
    role = str(value or "").strip().lower()
    aliases = {
        "human": "user",
        "agent": "assistant",
        "model": "assistant",
        "developer": "system",
        "function": "tool",
    }
    role = aliases.get(role, role)
    return role if role in {"user", "assistant", "system", "tool"} else None


def _message(
    *,
    role: Any,
    content: Any,
    message_id: Any = None,
    created_utc: Any = None,
) -> dict[str, Any] | None:
    normalized_role = _role(role)
    text = " ".join(_visible_text(content).replace("\x00", " ").split())
    if normalized_role not in {"user", "assistant"} or not text:
        return None
    text = text[:MAX_TEXT_CHARS]
    redacted = redact_secrets(text)
    source_id = str(message_id or "").strip()
    if not source_id:
        source_id = stable_sha256(
            {
                "role": normalized_role,
                "created_utc": _timestamp(created_utc),
                "text": redacted,
            }
        )[:32]
    result = {
        "source_message_id": _safe_source_identifier(
            source_id, prefix="message"
        ),
        "role": normalized_role,
        "created_utc": _timestamp(created_utc),
        "text": redacted,
        "secret_redacted": redacted != text,
    }
    result["message_sha256"] = stable_sha256(result)
    return result


def _json_values(text: str) -> tuple[Any, ...]:
    stripped = text.strip()
    if not stripped:
        raise ConversationImportError("conversation export is empty")
    try:
        return (json.loads(stripped),)
    except json.JSONDecodeError:
        rows: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ConversationImportError(
                    f"conversation JSONL line {line_number} is invalid"
                ) from exc
        if not rows:
            raise ConversationImportError("conversation export has no JSON records")
        return tuple(rows)


def _conversation_id(row: Mapping[str, Any], provider: str) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else row
    for source in (row, payload):
        for key in (
            "source_conversation_id",
            "conversation_id",
            "conversationId",
            "session_id",
            "sessionId",
            "thread_id",
            "threadId",
            "id",
        ):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"{provider}-{stable_sha256(dict(row))[:24]}"


def _codex_rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    session_id: str | None = None
    title: str | None = None
    messages: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        payload = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else raw
        outer_type = str(raw.get("type") or "").strip().lower()
        inner_type = str(payload.get("type") or "").strip().lower()
        if outer_type == "session_meta" or inner_type == "session_meta":
            session_id = _conversation_id(payload, "codex")
            candidate = payload.get("title") or payload.get("name")
            if isinstance(candidate, str) and candidate.strip():
                title = candidate.strip()
            continue
        role: Any = payload.get("role")
        content: Any = payload.get("content")
        if inner_type == "user_message":
            role, content = "user", payload.get("message")
        elif inner_type in {"agent_message", "assistant_message"}:
            role, content = "assistant", payload.get("message")
        elif outer_type == "response_item" and inner_type != "message":
            continue
        item = _message(
            role=role,
            content=content,
            message_id=payload.get("id") or raw.get("id"),
            created_utc=raw.get("timestamp") or payload.get("timestamp"),
        )
        if item is not None:
            messages.append(item)
    if not messages:
        return []
    identifier = session_id or f"codex-{stable_sha256(messages)[:24]}"
    return [{"source_conversation_id": identifier, "title": title, "messages": messages}]


def _claude_rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    titles: dict[str, str] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        identifier = _conversation_id(raw, "claude")
        message_value = raw.get("message")
        message_map = message_value if isinstance(message_value, Mapping) else raw
        raw_type = str(raw.get("type") or "").strip().lower()
        role = message_map.get("role") or raw_type
        if raw_type in {"summary", "file-history-snapshot", "progress"}:
            continue
        item = _message(
            role=role,
            content=message_map.get("content") or raw.get("content"),
            message_id=raw.get("uuid") or message_map.get("id"),
            created_utc=raw.get("timestamp") or message_map.get("timestamp"),
        )
        if item is not None:
            grouped[identifier].append(item)
        candidate = raw.get("title") or raw.get("summary")
        if isinstance(candidate, str) and candidate.strip() and identifier not in titles:
            titles[identifier] = candidate.strip()
    return [
        {
            "source_conversation_id": identifier,
            "title": titles.get(identifier),
            "messages": messages,
        }
        for identifier, messages in grouped.items()
        if messages
    ]


def _chatgpt_mapping(value: Mapping[str, Any], provider: str) -> dict[str, Any] | None:
    mapping = value.get("mapping")
    if not isinstance(mapping, Mapping):
        return None
    messages: list[dict[str, Any]] = []
    for node in mapping.values():
        if not isinstance(node, Mapping) or not isinstance(node.get("message"), Mapping):
            continue
        raw = node["message"]
        author = raw.get("author") if isinstance(raw.get("author"), Mapping) else {}
        item = _message(
            role=author.get("role"),
            content=raw.get("content"),
            message_id=raw.get("id"),
            created_utc=raw.get("create_time"),
        )
        if item is not None:
            messages.append(item)
    if not messages:
        return None
    return {
        "source_conversation_id": _conversation_id(value, provider),
        "title": value.get("title"),
        "messages": messages,
    }


def _generic_conversation(value: Mapping[str, Any], provider: str) -> dict[str, Any] | None:
    mapped = _chatgpt_mapping(value, provider)
    if mapped is not None:
        return mapped
    raw_messages = value.get("messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        return None
    messages: list[dict[str, Any]] = []
    for raw in raw_messages:
        if not isinstance(raw, Mapping):
            continue
        author = raw.get("author") if isinstance(raw.get("author"), Mapping) else {}
        nested_message = (
            raw.get("message") if isinstance(raw.get("message"), Mapping) else {}
        )
        item = _message(
            role=(
                raw.get("role")
                or author.get("role")
                or nested_message.get("role")
                or raw.get("type")
            ),
            content=(
                raw.get("content")
                or nested_message.get("content")
                or raw.get("message")
                or raw.get("text")
            ),
            message_id=raw.get("id") or raw.get("uuid") or nested_message.get("id"),
            created_utc=(
                raw.get("created_utc")
                or raw.get("timestamp")
                or raw.get("create_time")
            ),
        )
        if item is not None:
            messages.append(item)
    if not messages:
        return None
    return {
        "source_conversation_id": _conversation_id(value, provider),
        "title": value.get("title") or value.get("name"),
        "messages": messages,
    }


def _generic_rows(values: Sequence[Any], provider: str) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    if len(values) == 1 and isinstance(values[0], Mapping):
        container = values[0]
        raw = container.get("conversations")
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            candidates.extend(raw)
        else:
            candidates.append(container)
    elif len(values) == 1 and isinstance(values[0], Sequence) and not isinstance(
        values[0], (str, bytes)
    ):
        candidates.extend(values[0])
    else:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        missing_identity: list[Mapping[str, Any]] = []
        for raw in values:
            if isinstance(raw, Mapping):
                explicit = any(
                    isinstance(raw.get(key), str) and str(raw.get(key)).strip()
                    for key in (
                        "source_conversation_id",
                        "conversation_id",
                        "conversationId",
                        "session_id",
                        "sessionId",
                        "thread_id",
                        "threadId",
                    )
                )
                if explicit:
                    grouped[_conversation_id(raw, provider)].append(raw)
                else:
                    missing_identity.append(raw)
        for identifier, rows in grouped.items():
            candidates.append(
                {
                    "source_conversation_id": identifier,
                    "messages": rows,
                }
            )
        if missing_identity:
            candidates.append(
                {
                    "source_conversation_id": (
                        f"{provider}-{stable_sha256(missing_identity)[:24]}"
                    ),
                    "messages": missing_identity,
                }
            )
    seen_identifiers: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        identifier = _conversation_id(candidate, provider)[:500]
        if identifier in seen_identifiers:
            raise ConversationImportError(
                "conversation export contains duplicate conversation identities"
            )
        seen_identifiers.add(identifier)
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            normalized = _generic_conversation(candidate, provider)
            if normalized is not None:
                result.append(normalized)
    return result


def _deduplicate_messages(messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in messages:
        fingerprint = stable_sha256(
            {
                key: value
                for key, value in raw.items()
                if key not in {"sequence", "message_sha256"}
            }
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        row = dict(raw)
        row["sequence"] = len(result) + 1
        row["message_sha256"] = stable_sha256(
            {key: value for key, value in row.items() if key != "message_sha256"}
        )
        result.append(row)
        if len(result) >= MAX_MESSAGES_PER_CONVERSATION:
            break
    return result


def parse_conversation_export(
    provider: str,
    source_bytes: bytes,
) -> list[dict[str, Any]]:
    provider = _safe_provider(provider)
    if not source_bytes or len(source_bytes) > MAX_IMPORT_BYTES:
        raise ConversationImportError("conversation export exceeds the import limit")
    values = _json_values(_decode_text(source_bytes))
    if provider == "codex" and len(values) > 1:
        conversations = _codex_rows(values)
    elif provider == "claude" and len(values) > 1:
        conversations = _claude_rows(values)
    else:
        conversations = _generic_rows(values, provider)
        if not conversations and provider == "codex":
            conversations = _codex_rows(values)
        if not conversations and provider == "claude":
            conversations = _claude_rows(values)
    normalized: list[dict[str, Any]] = []
    total_messages = 0
    seen_conversation_ids: set[str] = set()
    for raw in conversations:
        identifier = str(raw.get("source_conversation_id") or "").strip()
        if not identifier:
            identifier = (
                f"{provider}-"
                f"{stable_sha256(_deduplicate_messages(raw.get('messages') or ()))[:24]}"
            )
        identifier = identifier[:500]
        if identifier in seen_conversation_ids:
            raise ConversationImportError(
                "conversation export contains duplicate conversation identities"
            )
        seen_conversation_ids.add(identifier)
    for raw in conversations[:MAX_CONVERSATIONS]:
        messages = _deduplicate_messages(raw.get("messages") or ())
        if not messages:
            continue
        remaining = MAX_TOTAL_MESSAGES - total_messages
        if remaining <= 0:
            break
        messages = messages[:remaining]
        total_messages += len(messages)
        identifier = str(raw.get("source_conversation_id") or "").strip()
        if not identifier:
            identifier = f"{provider}-{stable_sha256(messages)[:24]}"
        title = str(raw.get("title") or "").strip()
        if not title:
            first_user = next(
                (row["text"] for row in messages if row["role"] == "user"),
                "",
            )
            title = first_user[:120] or f"{provider.title()} conversation"
        title = redact_secrets(title)[:160]
        timestamps = [row["created_utc"] for row in messages if row["created_utc"]]
        normalized.append(
            {
                "source_conversation_id": identifier[:500],
                "title": title,
                "started_utc": min(timestamps) if timestamps else None,
                "ended_utc": max(timestamps) if timestamps else None,
                "timestamp_status": "source" if timestamps else "unavailable",
                "messages": messages,
            }
        )
    if not normalized:
        raise ConversationImportError("conversation export contains no visible messages")
    return normalized


def conversation_export_metadata(
    provider: str,
    source_bytes: bytes,
) -> list[dict[str, Any]]:
    """Return bounded metadata without persisting any conversation content."""

    rows = parse_conversation_export(provider, source_bytes)
    return [
        {
            "selection_id": str(row["source_conversation_id"]),
            "display_id": _safe_source_identifier(
                row["source_conversation_id"], prefix="conversation"
            ),
            "title": str(row["title"]),
            "started_utc": row.get("started_utc"),
            "ended_utc": row.get("ended_utc"),
            "message_count": len(row.get("messages") or ()),
        }
        for row in rows
    ]


def _import_root(project_root: Path) -> Path:
    return project_root.resolve() / ".peerbridge" / IMPORT_DIRECTORY


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _ensure_import_root(project_root: Path) -> Path:
    project = project_root.resolve()
    state_root = project / ".peerbridge"
    state_root.mkdir(parents=True, exist_ok=True)
    if _is_reparse(state_root):
        raise ConversationImportError("PeerBridge state root must not be a filesystem link")
    root = state_root / IMPORT_DIRECTORY
    root.mkdir(exist_ok=True)
    if _is_reparse(root) or root.resolve().parent != state_root.resolve():
        raise ConversationImportError("history import root must not be a filesystem link")
    return root


def _record_path(project_root: Path, import_id: str) -> Path:
    return _import_root(project_root) / f"{import_id}.json"


def _verify_record(value: Any, *, scope: str | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != IMPORT_SCHEMA:
        raise ConversationImportError("history import schema is invalid")
    record = dict(value)
    claimed = str(record.pop("record_sha256", ""))
    if len(claimed) != 64 or stable_sha256(record) != claimed:
        raise ConversationImportError("history import SHA-256 does not match")
    record["record_sha256"] = claimed
    if scope is not None and record.get("scope") != scope:
        raise ConversationImportError("history import scope does not match")
    if not record.get("read_only") or not isinstance(record.get("messages"), list):
        raise ConversationImportError("history import contract is invalid")
    return record


def import_conversation_export(
    *,
    project_root: Path,
    scope: str,
    provider: str,
    source_name: str,
    content_base64: str,
    source_sha256_override: str | None = None,
    source_bytes_override: int | None = None,
    selected_conversation_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    provider = _safe_provider(provider)
    source_bytes = _decode_payload(content_base64)
    source_sha256 = _sha256(source_bytes)
    if source_sha256_override is not None:
        candidate = str(source_sha256_override).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", candidate):
            raise ConversationImportError("source SHA-256 override is invalid")
        source_sha256 = candidate
    source_size = len(source_bytes)
    if source_bytes_override is not None:
        source_size = int(source_bytes_override)
        if source_size < 1:
            raise ConversationImportError("source byte count override is invalid")
    conversations = parse_conversation_export(provider, source_bytes)
    if selected_conversation_ids is not None:
        selected_order = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in selected_conversation_ids
                if str(item or "").strip()
            )
        )
        selected = set(selected_order)
        if not selected or len(selected) > 20:
            raise ConversationImportError("conversation selection is invalid")
        available = {
            str(row["source_conversation_id"]): row for row in conversations
        }
        if not selected.issubset(available):
            raise ConversationImportError(
                "conversation selection does not match the source export"
            )
        conversations = [available[item] for item in selected_order]
    root = _ensure_import_root(project_root)
    results: list[dict[str, Any]] = []
    imported_utc = utc_now()
    for conversation in conversations:
        import_id = f"{provider}-{stable_sha256({'scope': scope, 'source_sha256': source_sha256, 'conversation_id': conversation['source_conversation_id']})[:32]}"
        messages = list(conversation["messages"])
        record: dict[str, Any] = {
            "schema": IMPORT_SCHEMA,
            "import_id": import_id,
            "room_id": f"history.{import_id}",
            "scope": scope,
            "provider": provider,
            "source_name": _safe_source_name(source_name),
            "source_bytes": source_size,
            "source_sha256": source_sha256,
            "source_conversation_id": _safe_source_identifier(
                conversation["source_conversation_id"], prefix="conversation"
            ),
            "title": conversation["title"],
            "started_utc": conversation["started_utc"],
            "ended_utc": conversation["ended_utc"],
            "timestamp_status": conversation["timestamp_status"],
            "message_count": len(messages),
            "secret_redaction_count": sum(
                1 for row in messages if row.get("secret_redacted")
            ),
            "read_only": True,
            "imported_utc": imported_utc,
            "messages": messages,
        }
        record["record_sha256"] = stable_sha256(record)
        path = _record_path(project_root, import_id)
        encoded = (
            json.dumps(
                record,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_IMPORT_RECORD_BYTES:
            raise ConversationImportError("normalized history record exceeds the output limit")
        if path.exists():
            existing = _verify_record(json.loads(path.read_text(encoding="utf-8")), scope=scope)
            immutable_existing = {
                key: value
                for key, value in existing.items()
                if key not in {"source_name", "imported_utc", "record_sha256"}
            }
            immutable_candidate = {
                key: value
                for key, value in record.items()
                if key not in {"source_name", "imported_utc", "record_sha256"}
            }
            if immutable_existing != immutable_candidate:
                raise ConversationImportError("history import identity already exists with different content")
            record = existing
            status = "existing"
        else:
            record_files = []
            total_store_bytes = 0
            for candidate in root.glob("*.json"):
                if candidate.is_symlink() or not candidate.is_file():
                    raise ConversationImportError(
                        "history import store contains an unsafe entry"
                    )
                record_files.append(candidate)
                total_store_bytes += int(candidate.stat().st_size)
                if (
                    len(record_files) >= MAX_IMPORT_RECORDS
                    or total_store_bytes + len(encoded) > MAX_IMPORT_STORE_BYTES
                ):
                    raise ConversationImportError(
                        "history import store quota exceeded"
                    )
            with path.open("xb") as handle:
                handle.write(encoded)
            status = "created"
        results.append(
            {
                "status": status,
                "import_id": import_id,
                "room_id": record["room_id"],
                "provider": provider,
                "title": record["title"],
                "message_count": record["message_count"],
                "source_sha256": source_sha256,
                "record_sha256": record["record_sha256"],
            }
        )
    return results


def list_conversation_imports(project_root: Path, scope: str) -> list[dict[str, Any]]:
    root = _import_root(project_root)
    if not root.is_dir():
        return []
    if _is_reparse(root):
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_IMPORT_BYTES:
                continue
            record = _verify_record(
                json.loads(path.read_text(encoding="utf-8")), scope=scope
            )
        except (OSError, json.JSONDecodeError, ConversationImportError):
            continue
        records.append(record)
    records.sort(key=lambda row: (str(row.get("imported_utc") or ""), row["import_id"]), reverse=True)
    return records[:MAX_CONVERSATIONS]


def imported_room_view(
    record: Mapping[str, Any],
    *,
    rooms: Sequence[Mapping[str, Any]],
    limit: int = 120,
    before_sequence: int | None = None,
) -> dict[str, Any]:
    provider = str(record["provider"])
    agent_id = f"{provider}-history"
    room_id = str(record["room_id"])
    all_source_messages = list(record["messages"])
    if before_sequence is not None:
        all_source_messages = [
            row
            for row in all_source_messages
            if int(row.get("sequence") or 0) < int(before_sequence)
        ]
    visible_source_messages: list[dict[str, Any]] = []
    seen_source_messages: set[str] = set()
    collapsed_duplicate_count = 0
    for raw in all_source_messages:
        row = dict(raw)
        fingerprint = stable_sha256(
            {
                key: value
                for key, value in row.items()
                if key not in {"sequence", "message_sha256"}
            }
        )
        if fingerprint in seen_source_messages:
            collapsed_duplicate_count += 1
            continue
        seen_source_messages.add(fingerprint)
        visible_source_messages.append(row)
    bounded_source_messages = visible_source_messages[
        -max(1, min(int(limit), 512)) :
    ]
    messages: list[dict[str, Any]] = []
    for row in bounded_source_messages:
        role = str(row["role"])
        sender = "human-operator" if role == "user" else agent_id
        messages.append(
            {
                "sequence": int(row["sequence"]),
                "message_id": f"history-{record['import_id']}-{int(row['sequence'])}",
                "scope": record["scope"],
                "room_id": room_id,
                "room_sequence": int(row["sequence"]),
                "thread_id": None,
                "sender": sender,
                "recipient": agent_id if role == "user" else "human-operator",
                "subject": f"Imported {role}" if role in {"system", "tool"} else None,
                "body": row["text"],
                "task_id": f"history:{record['import_id']}",
                "priority": "normal",
                "created_utc": row.get("created_utc") or record["imported_utc"],
                "content_sha256": row["message_sha256"],
                "requested_route_profile_id": None,
                "requested_provider_id": provider,
                "requested_model_id": None,
                "requested_reasoning_mode": None,
                "observed_provider_id": provider,
                "observed_model_id": None,
                "observed_reasoning_mode": None,
                "route_receipt_sha256": record["record_sha256"],
                "artifact_paths": [],
                "imported_history": True,
            }
        )
    imported_room = {
        "room_id": room_id,
        "name": record["title"],
        "message_count": int(record["message_count"]),
        "active_member_count": 0,
        "updated_utc": record.get("ended_utc") or record["imported_utc"],
        "room_kind": "imported-history",
        "provider": provider,
        "read_only": True,
    }
    all_rooms = [dict(row) for row in rooms if str(row.get("room_id")) != room_id]
    all_rooms.append(imported_room)
    return {
        "room_id": room_id,
        "rooms": all_rooms,
        "members": [
            {
                "scope": record["scope"],
                "room_id": room_id,
                "agent_id": agent_id,
                "room_session_id": f"history-{record['import_id']}",
                "route_profile_id": None,
                "provider_id": provider,
                "model_id": None,
                "reasoning_mode": None,
                "route_class": "imported-history",
                "role_id": "historical-source",
                "role_label": "Historical source",
                "joined_utc": record["imported_utc"],
                "updated_utc": record["imported_utc"],
                "membership_sha256": record["record_sha256"],
                "status": "completed",
                "online": False,
                "last_seen_utc": record.get("ended_utc"),
            }
        ],
        "messages": messages,
        "page": {
            "limit": max(1, min(int(limit), 512)),
            "returned": len(messages),
            "collapsed_duplicate_count": collapsed_duplicate_count,
            "has_older": bool(
                len(visible_source_messages) > len(messages)
            ),
            "oldest_sequence": (
                int(messages[0]["sequence"]) if messages else None
            ),
            "newest_sequence": (
                int(messages[-1]["sequence"]) if messages else None
            ),
        },
        "operator_active": False,
        "automation": {
            "mode": "off",
            "enabled": False,
            "max_rounds": 0,
            "max_messages": 0,
            "stagnation_rounds": 0,
            "active_discussion": None,
        },
        "imported_history": {
            "import_id": record["import_id"],
            "provider": provider,
            "source_name": record["source_name"],
            "source_conversation_id": record["source_conversation_id"],
            "source_sha256": record["source_sha256"],
            "record_sha256": record["record_sha256"],
            "timestamp_status": record["timestamp_status"],
            "secret_redaction_count": record["secret_redaction_count"],
            "read_only": True,
        },
    }


__all__ = [
    "ConversationImportError",
    "IMPORT_SCHEMA",
    "MAX_IMPORT_BYTES",
    "SUPPORTED_PROVIDERS",
    "conversation_export_metadata",
    "import_conversation_export",
    "imported_room_view",
    "list_conversation_imports",
    "parse_conversation_export",
]
