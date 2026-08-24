"""Authorized metadata-first history adapters for official local Agent clients."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_install import find_trusted_executable, official_agent_spec
from .ccswitch_runner import _bounded_process
from .child_environment import build_agent_child_environment
from .conversation_import import (
    MAX_IMPORT_BYTES,
    ConversationImportError,
    import_conversation_export,
    parse_conversation_export,
)
from .secret_scan import redact_secrets


MAX_HISTORY_ROWS = 50
SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
SAFE_SOURCE_REVISION = re.compile(r"[0-9a-f]{64}\Z")
GROK_LIST_ROW = re.compile(
    r"^(?P<id>[0-9A-Fa-f-]{20,64})\s+"
    r"(?P<created>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<updated>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<status>\S+)\s*(?P<title>.*)$"
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _combined_revision(*parts: bytes) -> str:
    if len(parts) == 1:
        return _sha256(parts[0])
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _require_source_revision(expected: str, actual: str) -> None:
    revision = str(expected or "").strip().lower()
    if SAFE_SOURCE_REVISION.fullmatch(revision) is None:
        raise ConversationImportError("history source revision is invalid")
    if revision != actual:
        raise ConversationImportError("history source changed since discovery")


def _canonical_directory(value: Any) -> Path | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_dir() else None


def _project_directory(project_root: Path) -> Path:
    try:
        resolved = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConversationImportError("history project is unavailable") from exc
    if not resolved.is_dir():
        raise ConversationImportError("history project is unavailable")
    return resolved


def _same_directory(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _date_utc(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).isoformat().replace(
            "+00:00", "Z"
        )
    except ValueError:
        return None


def _mtime_utc(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat().replace(
        "+00:00", "Z"
    )


def _visible(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "\n".join(part for part in (_visible(item) for item in value) if part)
    if not isinstance(value, Mapping):
        return ""
    kind = str(value.get("type") or "").lower()
    if kind in {"thinking", "reasoning", "analysis", "tool_use", "tool_result"}:
        return ""
    for key in ("text", "content", "message", "parts"):
        if key in value:
            text = _visible(value[key])
            if text:
                return text
    return ""


def _safe_title(value: Any, fallback: str) -> str:
    title = " ".join(str(value or "").split())
    return redact_secrets(title)[:160] or fallback


def _safe_session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    if session_id in {".", ".."} or SAFE_SESSION_ID.fullmatch(session_id) is None:
        raise ConversationImportError("history session identity is invalid")
    return session_id


def _bounded_file(root: Path, path: Path, *, limit: int = MAX_IMPORT_BYTES) -> bytes:
    """Read one stable regular file without following a reparse ancestor."""

    try:
        lexical_root = root.absolute()
        lexical_path = path.absolute()
        relative = lexical_path.relative_to(lexical_root)
        current = lexical_root
        for part in relative.parts:
            current = current / part
            if _is_reparse(current):
                raise ConversationImportError("history source crosses a filesystem link")
        resolved_root = lexical_root.resolve(strict=True)
        resolved = lexical_path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        before = resolved.stat()
        if not resolved.is_file() or before.st_size < 1 or before.st_size > limit:
            raise ConversationImportError("history source exceeds the import limit")
        with resolved.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            payload = handle.read(limit + 1)
            opened_after = os.fstat(handle.fileno())
    except ConversationImportError:
        raise
    except (OSError, ValueError) as exc:
        raise ConversationImportError("history source is unavailable") from exc
    identity_before = (
        opened_before.st_dev,
        opened_before.st_ino,
        opened_before.st_size,
        opened_before.st_mtime_ns,
    )
    identity_after = (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_size,
        opened_after.st_mtime_ns,
    )
    if identity_before != identity_after or len(payload) > limit:
        raise ConversationImportError("history source changed or exceeds the import limit")
    return payload


def _file_metadata(
    root: Path, path: Path, *, limit: int = MAX_IMPORT_BYTES
) -> dict[str, Any]:
    """Bind one source by metadata without opening or reading its body."""

    try:
        lexical_root = root.absolute()
        lexical_path = path.absolute()
        relative = lexical_path.relative_to(lexical_root)
        current = lexical_root
        for part in relative.parts:
            current = current / part
            if _is_reparse(current):
                raise ConversationImportError("history source crosses a filesystem link")
        resolved_root = lexical_root.resolve(strict=True)
        resolved = lexical_path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        metadata = resolved.stat()
    except ConversationImportError:
        raise
    except (OSError, ValueError) as exc:
        raise ConversationImportError("history source is unavailable") from exc
    if not resolved.is_file() or metadata.st_size < 1 or metadata.st_size > limit:
        raise ConversationImportError("history source exceeds the import limit")
    return {
        "relative_path": relative.as_posix(),
        "bytes": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
    }


def _metadata_revision(*parts: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        [dict(part) for part in parts],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _source_binding(parts: Sequence[tuple[str, bytes]]) -> tuple[str, int]:
    framed = [
        str(label).encode("utf-8") + b"\0" + bytes(payload)
        for label, payload in parts
    ]
    return _combined_revision(*framed), sum(len(part) for part in framed)


def _import_normalized(
    *,
    project_root: Path,
    scope: str,
    provider: str,
    session_id: str,
    title: str,
    source_name: str,
    source_parts: Sequence[tuple[str, bytes]],
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    export = {
        "source_conversation_id": session_id,
        "title": title,
        "messages": list(messages),
    }
    normalized = json.dumps(
        export,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    source_sha256, source_bytes = _source_binding(source_parts)
    return import_conversation_export(
        project_root=project_root,
        scope=scope,
        provider=provider,
        source_name=source_name,
        content_base64=base64.b64encode(normalized).decode("ascii"),
        source_sha256_override=source_sha256,
        source_bytes_override=source_bytes,
    )


def _claude_project_dir(project_root: Path) -> Path:
    encoded = re.sub(r"[^A-Za-z0-9_-]", "-", str(project_root.resolve()))
    return Path.home() / ".claude" / "projects" / encoded


def _claude_bound_project(source: bytes) -> Path | None:
    observed: set[Path] = set()
    for line in source.decode("utf-8", errors="ignore").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        raw = row.get("cwd") or row.get("projectPath") or row.get("project_root")
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = _canonical_directory(raw)
        if candidate is None:
            return None
        observed.add(candidate)
        if len(observed) > 1:
            return None
    return next(iter(observed), None)


def _first_claude_title(source: bytes) -> str:
    for line in source[: 128 * 1024].decode("utf-8", errors="ignore").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping) or row.get("type") != "user":
            continue
        message = row.get("message") if isinstance(row.get("message"), Mapping) else row
        title = _visible(message.get("content"))
        if title.strip():
            return _safe_title(title, "Claude conversation")
    return "Claude conversation"


def list_claude_sessions(project_root: Path) -> list[dict[str, Any]]:
    project = _project_directory(project_root)
    root = _claude_project_dir(project_root)
    if not root.is_dir() or _is_reparse(root):
        return []
    rows: list[dict[str, Any]] = []
    candidates: list[Path] = []
    for path in root.iterdir():
        if path.suffix.lower() == ".jsonl":
            candidates.append(path)
        if len(candidates) >= 500:
            break
    for path in sorted(
        candidates,
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    ):
        try:
            metadata = _file_metadata(root, path)
        except (OSError, ConversationImportError):
            continue
        session_id = path.stem
        if SAFE_SESSION_ID.fullmatch(session_id) is None:
            continue
        revision = _metadata_revision(
            {
                "provider": "claude",
                "project": os.path.normcase(str(project)),
                "session_id": session_id,
                "source": metadata,
            }
        )
        rows.append(
            {
                "session_id": session_id,
                "title": "Claude conversation",
                "updated_utc": _mtime_utc(path),
                "source": "claude-code-jsonl",
                "source_revision": revision,
            }
        )
        if len(rows) >= MAX_HISTORY_ROWS:
            break
    return rows


def import_claude_session(
    *,
    project_root: Path,
    scope: str,
    session_id: str,
    source_revision: str,
) -> list[dict[str, Any]]:
    session_id = _safe_session_id(session_id)
    project = _project_directory(project_root)
    root = _claude_project_dir(project_root)
    path = root / f"{session_id}.jsonl"
    metadata = _file_metadata(root, path)
    _require_source_revision(
        source_revision,
        _metadata_revision(
            {
                "provider": "claude",
                "project": os.path.normcase(str(project)),
                "session_id": session_id,
                "source": metadata,
            }
        ),
    )
    source = _bounded_file(root, path)
    bound_project = _claude_bound_project(source)
    if bound_project is None or not _same_directory(bound_project, project):
        raise ConversationImportError(
            "Claude conversation is not bound to the selected project"
        )
    return import_conversation_export(
        project_root=project_root,
        scope=scope,
        provider="claude",
        source_name=f"claude-code-{session_id}.jsonl",
        content_base64=base64.b64encode(source).decode("ascii"),
    )


def _grok_workspace_root(project_root: Path) -> Path:
    encoded = urllib.parse.quote(str(project_root.resolve()), safe="")
    return Path.home() / ".grok" / "sessions" / encoded


def _grok_source_bundle(
    workspace: Path, session_id: str
) -> tuple[bytes, bytes | None]:
    session_root = workspace / session_id
    source = _bounded_file(workspace, session_root / "chat_history.jsonl")
    summary_path = session_root / "summary.json"
    summary: bytes | None = None
    try:
        if (
            summary_path.exists()
            and not _is_reparse(summary_path)
            and summary_path.is_file()
        ):
            summary = _bounded_file(session_root, summary_path, limit=256 * 1024)
    except (OSError, ConversationImportError):
        summary = None
    return source, summary


def _grok_source_revision(source: bytes, summary: bytes | None) -> str:
    summary_part = b"\x00" if summary is None else b"\x01" + summary
    return _combined_revision(source, summary_part)


def _grok_metadata_revision(
    workspace: Path,
    session_id: str,
    *,
    created: str,
    updated: str,
    status: str,
    title: str,
) -> str:
    session_root = workspace / session_id
    chat = _file_metadata(workspace, session_root / "chat_history.jsonl")
    summary_path = session_root / "summary.json"
    summary: dict[str, Any] | None = None
    try:
        if summary_path.exists():
            summary = _file_metadata(session_root, summary_path, limit=256 * 1024)
    except (OSError, ConversationImportError):
        summary = None
    return _metadata_revision(
        {
            "provider": "grok",
            "session_id": session_id,
            "created": created,
            "updated": updated,
            "status": status,
            "title": title,
            "chat": chat,
            "summary": summary,
        }
    )


def list_grok_sessions(project_root: Path) -> list[dict[str, Any]]:
    _project_directory(project_root)
    executable = find_trusted_executable(official_agent_spec("grok"))
    if executable is None:
        return []
    environment = build_agent_child_environment(
        "grok-build", required_path_roots=(executable.parent,)
    )
    return_code, stdout, _stderr = _bounded_process(
        (str(executable), "sessions", "list", "--limit", str(MAX_HISTORY_ROWS)),
        cwd=project_root.resolve(),
        environment=environment,
        stdin_text="",
        timeout_seconds=30,
        runtime_label="official Grok session metadata",
    )
    if return_code != 0:
        return []
    rows: list[dict[str, Any]] = []
    workspace = _grok_workspace_root(project_root)
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        match = GROK_LIST_ROW.match(line.strip())
        if match is None:
            continue
        try:
            session_id = _safe_session_id(match.group("id"))
            source_revision = _grok_metadata_revision(
                workspace,
                session_id,
                created=match.group("created"),
                updated=match.group("updated"),
                status=match.group("status"),
                title=match.group("title"),
            )
        except ConversationImportError:
            continue
        rows.append(
            {
                "session_id": session_id,
                "title": _safe_title(match.group("title"), "Grok conversation"),
                "created_utc": _date_utc(match.group("created")),
                "updated_utc": _date_utc(match.group("updated")),
                "status": match.group("status"),
                "source": "grok-sessions-list",
                "source_revision": source_revision,
            }
        )
    return rows[:MAX_HISTORY_ROWS]


def import_grok_session(
    *,
    project_root: Path,
    scope: str,
    session_id: str,
    source_revision: str,
) -> list[dict[str, Any]]:
    session_id = _safe_session_id(session_id)
    _project_directory(project_root)
    workspace = _grok_workspace_root(project_root)
    _file_metadata(workspace, workspace / session_id / "chat_history.jsonl")
    listed = {
        str(row["session_id"]): row for row in list_grok_sessions(project_root)
    }
    selected = listed.get(session_id)
    if selected is None:
        raise ConversationImportError("Grok history session is unavailable")
    _require_source_revision(source_revision, str(selected["source_revision"]))
    source, summary_source = _grok_source_bundle(workspace, session_id)
    messages: list[dict[str, Any]] = []
    for line in source.decode("utf-8", errors="strict").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping) or row.get("type") not in {"user", "assistant"}:
            continue
        text = _visible(row.get("content"))
        if text.strip():
            messages.append(
                {
                    "id": row.get("id"),
                    "role": row["type"],
                    "timestamp": row.get("timestamp") or row.get("created_at"),
                    "content": text,
                }
            )
    if not messages:
        raise ConversationImportError("Grok history session has no visible messages")
    title = "Grok conversation"
    if summary_source is not None:
        try:
            summary = json.loads(summary_source.decode("utf-8"))
            title = _safe_title(
                summary.get("generated_title") or summary.get("session_summary"),
                title,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
    return _import_normalized(
        project_root=project_root,
        scope=scope,
        provider="grok",
        session_id=session_id,
        title=title,
        source_name=f"grok-{session_id}-chat_history.jsonl",
        source_parts=[
            ("chat_history.jsonl", source),
            *(
                [("summary.json", summary_source)]
                if summary_source is not None
                else []
            ),
        ],
        messages=messages,
    )


def _kimi_home() -> Path:
    return Path.home() / ".kimi-code"


def _kimi_state_rows() -> list[tuple[Path, Mapping[str, Any], bytes]]:
    root = _kimi_home() / "sessions"
    if not root.is_dir() or _is_reparse(root):
        return []
    rows: list[tuple[Path, Mapping[str, Any], bytes]] = []
    for group in root.iterdir():
        if _is_reparse(group) or not group.is_dir():
            continue
        for session_root in group.iterdir():
            if _is_reparse(session_root) or not session_root.is_dir():
                continue
            path = session_root / "state.json"
            if not path.exists():
                continue
            try:
                source = _bounded_file(session_root, path, limit=512 * 1024)
                value = json.loads(source.decode("utf-8"))
            except (ConversationImportError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, Mapping):
                rows.append((path, value, source))
            if len(rows) >= 500:
                return rows
    return rows


def _unique_kimi_state_rows() -> list[tuple[str, Path, Mapping[str, Any], bytes]]:
    rows: list[tuple[str, Path, Mapping[str, Any], bytes]] = []
    seen: set[str] = set()
    for path, state, state_source in _kimi_state_rows():
        session_id = str(
            state.get("session_id") or state.get("sessionId") or path.parent.name
        ).strip()
        if SAFE_SESSION_ID.fullmatch(session_id) is None:
            continue
        if session_id in seen:
            raise ConversationImportError(
                "Kimi history contains duplicate session identities"
            )
        seen.add(session_id)
        rows.append((session_id, path, state, state_source))
    return rows


def _kimi_work_directory(state: Mapping[str, Any]) -> Path | None:
    raw = state.get("work_dir") or state.get("workDir") or state.get("cwd")
    return _canonical_directory(raw)


def _kimi_source_bundle(state_path: Path) -> tuple[Path, bytes]:
    candidates = (
        state_path.parent / "agents" / "main" / "wire.jsonl",
        state_path.parent / "context.jsonl",
    )
    for path in candidates:
        try:
            exists = path.exists()
        except OSError:
            continue
        if exists:
            return path, _bounded_file(state_path.parent, path)
    raise ConversationImportError("Kimi history content is unavailable")


def _kimi_source_metadata(state_path: Path) -> tuple[Path, dict[str, Any]]:
    candidates = (
        state_path.parent / "agents" / "main" / "wire.jsonl",
        state_path.parent / "context.jsonl",
    )
    for path in candidates:
        try:
            if path.exists():
                return path, _file_metadata(state_path.parent, path)
        except (OSError, ConversationImportError):
            continue
    raise ConversationImportError("Kimi history content is unavailable")


def _kimi_source_revision(
    state_path: Path,
    state_source: bytes,
    source_path: Path,
    source_metadata: Mapping[str, Any],
) -> str:
    relative_source = source_path.relative_to(state_path.parent).as_posix().encode(
        "utf-8"
    )
    return _combined_revision(
        state_source,
        relative_source,
        json.dumps(
            dict(source_metadata),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def list_kimi_sessions(project_root: Path) -> list[dict[str, Any]]:
    project = _project_directory(project_root)
    rows: list[dict[str, Any]] = []
    for session_id, path, state, state_source in _unique_kimi_state_rows():
        work_dir = _kimi_work_directory(state)
        if work_dir is None or not _same_directory(work_dir, project):
            continue
        try:
            source_path, source_metadata = _kimi_source_metadata(path)
        except ConversationImportError:
            continue
        rows.append(
            {
                "session_id": session_id,
                "title": _safe_title(
                    state.get("title") or state.get("name"), "Kimi conversation"
                ),
                "created_utc": state.get("created_at") or state.get("createdAt"),
                "updated_utc": (
                    state.get("updated_at")
                    or state.get("updatedAt")
                    or _mtime_utc(path)
                ),
                "source": "kimi-code-state",
                "source_revision": _kimi_source_revision(
                    path,
                    state_source,
                    source_path,
                    source_metadata,
                ),
            }
        )
    rows.sort(key=lambda row: str(row.get("updated_utc") or ""), reverse=True)
    return rows[:MAX_HISTORY_ROWS]


def import_kimi_session(
    *,
    project_root: Path,
    scope: str,
    session_id: str,
    source_revision: str,
) -> list[dict[str, Any]]:
    session_id = _safe_session_id(session_id)
    project = _project_directory(project_root)
    selected: tuple[Path, Mapping[str, Any], bytes] | None = None
    for candidate, path, state, state_source in _unique_kimi_state_rows():
        work_dir = _kimi_work_directory(state)
        if (
            candidate == session_id
            and work_dir is not None
            and _same_directory(work_dir, project)
        ):
            selected = (path, state, state_source)
            break
    if selected is None:
        raise ConversationImportError("Kimi history session is unavailable")
    state_path, state, state_source = selected
    source_path, source_metadata = _kimi_source_metadata(state_path)
    _require_source_revision(
        source_revision,
        _kimi_source_revision(
            state_path,
            state_source,
            source_path,
            source_metadata,
        ),
    )
    source_path, source = _kimi_source_bundle(state_path)
    parsed = parse_conversation_export("kimi", source)
    messages = parsed[0]["messages"] if parsed else []
    return _import_normalized(
        project_root=project_root,
        scope=scope,
        provider="kimi",
        session_id=session_id,
        title=_safe_title(state.get("title") or state.get("name"), "Kimi conversation"),
        source_name=f"kimi-{session_id}-{source_path.name}",
        source_parts=[
            ("state.json", state_source),
            (source_path.relative_to(state_path.parent).as_posix(), source),
        ],
        messages=messages,
    )


def list_native_sessions(provider: str, project_root: Path) -> list[dict[str, Any]]:
    if provider == "claude":
        return list_claude_sessions(project_root)
    if provider == "grok":
        return list_grok_sessions(project_root)
    if provider == "kimi":
        return list_kimi_sessions(project_root)
    raise ConversationImportError("native history provider is unsupported")


def import_native_session(
    *,
    project_root: Path,
    scope: str,
    provider: str,
    session_id: str,
    source_revision: str,
) -> list[dict[str, Any]]:
    if provider == "claude":
        return import_claude_session(
            project_root=project_root,
            scope=scope,
            session_id=session_id,
            source_revision=source_revision,
        )
    if provider == "grok":
        return import_grok_session(
            project_root=project_root,
            scope=scope,
            session_id=session_id,
            source_revision=source_revision,
        )
    if provider == "kimi":
        return import_kimi_session(
            project_root=project_root,
            scope=scope,
            session_id=session_id,
            source_revision=source_revision,
        )
    raise ConversationImportError("native history provider is unsupported")


__all__ = [
    "import_native_session",
    "list_claude_sessions",
    "list_grok_sessions",
    "list_kimi_sessions",
    "list_native_sessions",
]
