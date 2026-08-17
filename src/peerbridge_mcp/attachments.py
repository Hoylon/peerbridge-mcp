"""Safe, content-addressed files selected explicitly for room messages."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .image_validation import image_payload_is_valid
from .secret_scan import contains_secret_bytes


MAX_CHAT_ATTACHMENT_COUNT = 5
MAX_CHAT_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_CHAT_ATTACHMENT_TOTAL_BYTES = 16 * 1024 * 1024
CHAT_ATTACHMENT_ROOT = ".peerbridge-artifacts/chat"

_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".txt": "text/plain",
    ".webp": "image/webp",
}
CHAT_ATTACHMENT_SUFFIXES = frozenset(_MEDIA_TYPES)
_TEXT_SUFFIXES = frozenset({".csv", ".json", ".log", ".md", ".txt"})


class AttachmentError(ValueError):
    """A selected chat attachment is unavailable or unsafe to persist."""


@dataclass(frozen=True)
class StagedAttachment:
    relative_path: str
    sha256: str
    bytes: int
    media_type: str


@dataclass(frozen=True)
class StableAttachmentSource:
    path: Path
    suffix: str
    payload: bytes


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def read_stable_attachment_source(
    raw_path: Path,
    *,
    allowed_suffixes: frozenset[str],
    maximum_bytes: int,
) -> StableAttachmentSource:
    """Open once, bind the handle to the checked file, and read bounded bytes."""
    path = Path(raw_path)
    if path.is_symlink():
        raise AttachmentError("selected files must not be symbolic links")
    try:
        resolved = path.resolve(strict=True)
        expected = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise AttachmentError("a selected file is unavailable") from exc
    suffix = resolved.suffix.lower()
    if not stat.S_ISREG(expected.st_mode) or suffix not in allowed_suffixes:
        raise AttachmentError("selected file type is not allowed")
    if expected.st_size > maximum_bytes:
        raise AttachmentError("a selected file is too large")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(resolved, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(
                expected
            ):
                raise AttachmentError("selected file changed before it was opened")
            payload = handle.read(maximum_bytes + 1)
            finished = os.fstat(handle.fileno())
    except AttachmentError:
        raise
    except OSError as exc:
        raise AttachmentError("a selected file could not be read") from exc
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)

    if len(payload) > maximum_bytes:
        raise AttachmentError("a selected file is too large")
    if _file_identity(opened) != _file_identity(finished):
        raise AttachmentError("selected file changed while it was being read")
    return StableAttachmentSource(path=resolved, suffix=suffix, payload=payload)


def _payload_is_valid(suffix: str, payload: bytes) -> bool:
    if suffix in {".gif", ".jpeg", ".jpg", ".png", ".webp"}:
        return image_payload_is_valid(suffix, payload)
    if suffix not in _TEXT_SUFFIXES or b"\x00" in payload:
        return False
    try:
        text = payload.decode("utf-8-sig")
        if suffix == ".json":
            json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def _safe_output_root(project_root: Path) -> Path:
    root = project_root.resolve(strict=True)
    output = root / CHAT_ATTACHMENT_ROOT
    output.mkdir(parents=True, exist_ok=True)
    try:
        output.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise AttachmentError("chat attachment store escapes the project root") from exc
    if output.is_symlink():
        raise AttachmentError("chat attachment store must not be a symbolic link")
    return output


def _atomic_create_or_verify(path: Path, payload: bytes, expected_sha256: str) -> None:
    if path.is_symlink():
        raise AttachmentError("staged attachment must not be a symbolic link")
    if path.exists():
        try:
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise AttachmentError("staged attachment could not be verified") from exc
        if observed != expected_sha256:
            raise AttachmentError("content-addressed attachment conflicts with existing bytes")
        return

    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.rename(temporary, path)
        except FileExistsError:
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
            if observed != expected_sha256:
                raise AttachmentError(
                    "content-addressed attachment changed during staging"
                )
    except OSError as exc:
        raise AttachmentError("chat attachment could not be staged") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def stage_chat_attachments(
    project_root: Path, paths: Iterable[Path]
) -> tuple[StagedAttachment, ...]:
    """Validate explicit selections and copy them into an ignored project-local store."""
    selected = tuple(Path(value) for value in paths)
    if len(selected) > MAX_CHAT_ATTACHMENT_COUNT:
        raise AttachmentError("too many chat attachments")
    project = project_root.resolve(strict=True)
    total = 0
    prepared: list[tuple[str, str, bytes, str]] = []
    seen: set[tuple[str, str]] = set()

    for raw_path in selected:
        try:
            source = read_stable_attachment_source(
                raw_path,
                allowed_suffixes=CHAT_ATTACHMENT_SUFFIXES,
                maximum_bytes=MAX_CHAT_ATTACHMENT_BYTES,
            )
        except AttachmentError as exc:
            raise AttachmentError(str(exc).replace("selected file", "chat attachment")) from exc
        suffix = source.suffix
        payload = source.payload
        size = len(payload)
        total += size
        if total > MAX_CHAT_ATTACHMENT_TOTAL_BYTES:
            raise AttachmentError("chat attachments exceed the total size limit")
        if not _payload_is_valid(suffix, payload):
            raise AttachmentError("chat attachment content does not match its safe type")
        if suffix in _TEXT_SUFFIXES and contains_secret_bytes(payload):
            raise AttachmentError(
                "text attachment appears to contain a credential; use private feedback encryption"
            )
        digest = hashlib.sha256(payload).hexdigest()
        identity = (digest, suffix)
        if identity in seen:
            continue
        seen.add(identity)
        prepared.append((digest, suffix, payload, _MEDIA_TYPES[suffix]))

    output_root = _safe_output_root(project_root)
    results: list[StagedAttachment] = []
    for digest, suffix, payload, media_type in prepared:
        target = output_root / f"{digest}{suffix}"
        _atomic_create_or_verify(target, payload, digest)
        relative_path = target.resolve(strict=True).relative_to(project).as_posix()
        results.append(
            StagedAttachment(
                relative_path=relative_path,
                sha256=digest,
                bytes=len(payload),
                media_type=media_type,
            )
        )
    return tuple(results)
