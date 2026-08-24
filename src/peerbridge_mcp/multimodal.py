"""Verified attachment delivery contracts for official Agent runtimes.

PeerBridge stages attachments before a model turn and verifies them again at
the provider boundary.  Receipts deliberately describe transport acceptance,
not model comprehension.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import zlib
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .attachments import (
    CHAT_ATTACHMENT_ROOT,
    CHAT_ATTACHMENT_SUFFIXES,
    MAX_CHAT_ATTACHMENT_BYTES,
    MAX_CHAT_ATTACHMENT_COUNT,
    MAX_CHAT_ATTACHMENT_TOTAL_BYTES,
    AttachmentError,
    StagedAttachment,
    attachment_payload_is_valid,
    chat_attachment_media_type,
    read_stable_attachment_source,
)
from .secret_scan import contains_secret_bytes


VERIFIED_ATTACHMENT_MESSAGE_KEY = "_peerbridge_verified_attachments"
MAX_INLINE_TEXT_ATTACHMENT_BYTES = 256 * 1024
VISION_CHALLENGE_DIGITS = "23456789"
VISION_CHALLENGE_LENGTH = 6
VISION_CHALLENGE_PROMPT = (
    "Read the six-digit code shown in the supplied image. Reply with exactly "
    "VISION: followed immediately by the six digits. Do not add spaces, markdown, "
    "punctuation, or any other text."
)


@dataclass(frozen=True)
class VisionChallenge:
    challenge_id: str
    prompt: str
    png: bytes = field(repr=False)
    expected_code: str = field(repr=False)

    @property
    def image_sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()

    @property
    def expected_response(self) -> str:
        return f"VISION:{self.expected_code}"


_SEVEN_SEGMENT_DIGITS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abdeg",
    "3": "abcdg",
    "4": "bcfg",
    "5": "acdfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
}


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _render_vision_code_png(code: str) -> bytes:
    """Render a high-contrast six-digit challenge without textual metadata."""

    width, height = 640, 240
    background = (248, 250, 252)
    segment = (13, 31, 58)
    border = (188, 199, 216)
    pixels = bytearray(background * (width * height))

    def rectangle(x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + w), min(height, y + h)
        for row in range(y0, y1):
            start = (row * width + x0) * 3
            for column in range(x0, x1):
                offset = start + (column - x0) * 3
                pixels[offset : offset + 3] = bytes(color)

    digit_width, digit_height, thickness, gap = 70, 132, 12, 22
    total_width = len(code) * digit_width + (len(code) - 1) * gap
    origin_x = (width - total_width) // 2
    origin_y = 54
    for index, digit in enumerate(code):
        x = origin_x + index * (digit_width + gap)
        y = origin_y
        rectangle(x - 8, y - 8, digit_width + 16, digit_height + 16, border)
        rectangle(x - 5, y - 5, digit_width + 10, digit_height + 10, background)
        half = digit_height // 2
        coordinates = {
            "a": (x + thickness, y, digit_width - 2 * thickness, thickness),
            "b": (x + digit_width - thickness, y + thickness, thickness, half - thickness),
            "c": (x + digit_width - thickness, y + half, thickness, half - thickness),
            "d": (x + thickness, y + digit_height - thickness, digit_width - 2 * thickness, thickness),
            "e": (x, y + half, thickness, half - thickness),
            "f": (x, y + thickness, thickness, half - thickness),
            "g": (x + thickness, y + half - thickness // 2, digit_width - 2 * thickness, thickness),
        }
        for name in _SEVEN_SEGMENT_DIGITS[digit]:
            rectangle(*coordinates[name], segment)

    raw = b"".join(
        b"\x00" + bytes(pixels[row * width * 3 : (row + 1) * width * 3])
        for row in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + _png_chunk(b"IEND", b"")
    )


def create_vision_challenge(*, code: str | None = None) -> VisionChallenge:
    """Create a one-use OCR challenge whose answer appears only in image pixels."""

    selected = code or "".join(
        secrets.choice(VISION_CHALLENGE_DIGITS)
        for _ in range(VISION_CHALLENGE_LENGTH)
    )
    if (
        len(selected) != VISION_CHALLENGE_LENGTH
        or any(character not in VISION_CHALLENGE_DIGITS for character in selected)
    ):
        raise ValueError("vision challenge code is invalid")
    return VisionChallenge(
        challenge_id=secrets.token_hex(16),
        prompt=VISION_CHALLENGE_PROMPT,
        png=_render_vision_code_png(selected),
        expected_code=selected,
    )


def vision_verification_receipt(
    *,
    challenge: VisionChallenge,
    answer: str,
    provider_id: str,
    protocol: str,
    delivery_mode: str,
    provider_identity: str | None,
    model_id: str | None,
    client_version: str | None,
    failure_status: str = "semantic_image_failed",
) -> dict[str, object]:
    """Evaluate a challenge and return a receipt without its expected answer."""

    normalized_answer = str(answer or "").strip()
    confirmed = secrets.compare_digest(
        normalized_answer.encode("utf-8"),
        challenge.expected_response.encode("utf-8"),
    )
    body: dict[str, object] = {
        "challenge_id": challenge.challenge_id,
        "provider_id": str(provider_id),
        "protocol": str(protocol),
        "delivery_mode": str(delivery_mode),
        "provider_identity": str(provider_identity or ""),
        "model_id": str(model_id or ""),
        "client_version": str(client_version or ""),
        "status": "semantic_image_verified" if confirmed else str(failure_status),
        "model_view_confirmed": confirmed,
        "image_sha256": challenge.image_sha256,
        "image_bytes": len(challenge.png),
        "prompt_sha256": hashlib.sha256(challenge.prompt.encode("utf-8")).hexdigest(),
        "response_sha256": hashlib.sha256(normalized_answer.encode("utf-8")).hexdigest(),
        "response_present": bool(normalized_answer),
        "evaluated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**body, "receipt_sha256": hashlib.sha256(encoded).hexdigest()}


@dataclass(frozen=True)
class VerifiedAttachment:
    absolute_path: Path
    relative_path: str
    sha256: str
    bytes: int
    media_type: str
    kind: str

    def public_record(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "media_type": self.media_type,
            "kind": self.kind,
        }


def _attachment_value(
    attachment: StagedAttachment | Mapping[str, object], key: str
) -> object:
    if isinstance(attachment, StagedAttachment):
        return getattr(attachment, key)
    return attachment.get(key)


def verify_staged_attachments(
    project_root: Path,
    attachments: Iterable[StagedAttachment | Mapping[str, object]],
) -> tuple[VerifiedAttachment, ...]:
    """Rehash staged files and prove they remain inside the project store."""

    selected = tuple(attachments)
    if len(selected) > MAX_CHAT_ATTACHMENT_COUNT:
        raise AttachmentError("too many chat attachments")
    if not selected:
        return ()
    project = Path(project_root).resolve(strict=True)
    artifact_root = (project / CHAT_ATTACHMENT_ROOT).resolve(strict=True)
    try:
        artifact_root.relative_to(project)
    except ValueError as exc:
        raise AttachmentError("chat attachment store escapes the project root") from exc
    if artifact_root.is_symlink():
        raise AttachmentError("chat attachment store must not be a symbolic link")

    total = 0
    verified: list[VerifiedAttachment] = []
    seen: set[tuple[str, str]] = set()
    for attachment in selected:
        relative = str(_attachment_value(attachment, "relative_path") or "")
        digest = str(_attachment_value(attachment, "sha256") or "").lower()
        expected_bytes = _attachment_value(attachment, "bytes")
        expected_media_type = str(_attachment_value(attachment, "media_type") or "")
        posix = PurePosixPath(relative)
        if (
            not relative
            or posix.is_absolute()
            or ".." in posix.parts
            or tuple(posix.parts[:2]) != (".peerbridge-artifacts", "chat")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
        ):
            raise AttachmentError("staged attachment receipt is invalid")
        candidate = project.joinpath(*posix.parts)
        if candidate.is_symlink():
            raise AttachmentError("staged attachment must not be a symbolic link")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(artifact_root)
        except (OSError, ValueError) as exc:
            raise AttachmentError("staged attachment escapes the project store") from exc
        source = read_stable_attachment_source(
            resolved,
            allowed_suffixes=CHAT_ATTACHMENT_SUFFIXES,
            maximum_bytes=MAX_CHAT_ATTACHMENT_BYTES,
        )
        observed_digest = hashlib.sha256(source.payload).hexdigest()
        canonical_media_type = chat_attachment_media_type(source.suffix)
        if (
            source.path.stem != digest
            or observed_digest != digest
            or len(source.payload) != expected_bytes
            or canonical_media_type != expected_media_type
            or not attachment_payload_is_valid(source.suffix, source.payload)
        ):
            raise AttachmentError("staged attachment no longer matches its receipt")
        if canonical_media_type.startswith("text/") or canonical_media_type in {
            "application/json"
        }:
            if contains_secret_bytes(source.payload):
                raise AttachmentError("staged text attachment contains credential-like data")
            kind = "text"
        elif canonical_media_type.startswith("audio/"):
            kind = "audio"
        else:
            kind = "image"
        total += len(source.payload)
        if total > MAX_CHAT_ATTACHMENT_TOTAL_BYTES:
            raise AttachmentError("chat attachments exceed the total size limit")
        identity = (digest, source.suffix)
        if identity in seen:
            continue
        seen.add(identity)
        verified.append(
            VerifiedAttachment(
                absolute_path=source.path,
                relative_path=relative,
                sha256=digest,
                bytes=len(source.payload),
                media_type=canonical_media_type,
                kind=kind,
            )
        )
    return tuple(verified)


def verify_staged_attachment_paths(
    project_root: Path,
    relative_paths: Iterable[str],
) -> tuple[VerifiedAttachment, ...]:
    """Reconstruct receipts for content-addressed room attachments and verify them."""

    paths = tuple(str(value or "") for value in relative_paths)
    receipts: list[dict[str, object]] = []
    for relative in paths:
        posix = PurePosixPath(relative)
        if (
            not relative
            or posix.is_absolute()
            or ".." in posix.parts
            or tuple(posix.parts[:2]) != (".peerbridge-artifacts", "chat")
        ):
            raise AttachmentError("room attachment path is not a staged chat attachment")
        suffix = posix.suffix.lower()
        media_type = chat_attachment_media_type(suffix)
        digest = posix.stem.lower()
        if (
            media_type is None
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise AttachmentError("room attachment path is not content addressed")
        candidate = Path(project_root).joinpath(*posix.parts)
        try:
            size = candidate.stat(follow_symlinks=False).st_size
        except OSError as exc:
            raise AttachmentError("a room attachment is unavailable") from exc
        receipts.append(
            {
                "relative_path": relative,
                "sha256": digest,
                "bytes": size,
                "media_type": media_type,
            }
        )
    return verify_staged_attachments(project_root, receipts)


def extract_verified_attachments(
    project_root: Path,
    messages: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[VerifiedAttachment, ...]]:
    """Remove PeerBridge-only metadata and reverify it at a provider boundary."""

    cleaned: list[dict[str, Any]] = []
    receipts: list[Mapping[str, object]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise AttachmentError("attachment-bearing messages must be objects")
        item = dict(message)
        raw = item.pop(VERIFIED_ATTACHMENT_MESSAGE_KEY, ())
        if raw is None:
            raw = ()
        if not isinstance(raw, (list, tuple)):
            raise AttachmentError("verified attachment metadata is invalid")
        for attachment in raw:
            if isinstance(attachment, VerifiedAttachment):
                receipts.append(attachment.public_record())
            elif isinstance(attachment, Mapping):
                receipts.append(attachment)
            else:
                raise AttachmentError("verified attachment metadata is invalid")
        cleaned.append(item)
    return cleaned, verify_staged_attachments(project_root, receipts)


def read_verified_attachment_payload(attachment: VerifiedAttachment) -> bytes:
    """Read a verified attachment once more immediately before provider delivery."""

    source = read_stable_attachment_source(
        attachment.absolute_path,
        allowed_suffixes=CHAT_ATTACHMENT_SUFFIXES,
        maximum_bytes=MAX_CHAT_ATTACHMENT_BYTES,
    )
    observed_digest = hashlib.sha256(source.payload).hexdigest()
    if (
        observed_digest != attachment.sha256
        or len(source.payload) != attachment.bytes
        or chat_attachment_media_type(source.suffix) != attachment.media_type
        or not attachment_payload_is_valid(source.suffix, source.payload)
    ):
        raise AttachmentError("verified attachment changed before provider delivery")
    if attachment.kind == "text" and contains_secret_bytes(source.payload):
        raise AttachmentError("verified text attachment contains credential-like data")
    return source.payload


def claude_native_content_blocks(
    prompt: str,
    attachments: Iterable[VerifiedAttachment],
) -> list[dict[str, Any]]:
    """Build Claude stream-json blocks from reverified attachment bytes."""

    blocks: list[dict[str, Any]] = [{"type": "text", "text": str(prompt)}]
    for attachment in attachments:
        if attachment.kind == "audio":
            raise AttachmentError(
                "Claude stream runtime does not advertise native audio input"
            )
        payload = read_verified_attachment_payload(attachment)
        if attachment.kind == "image":
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": attachment.media_type,
                        "data": base64.b64encode(payload).decode("ascii"),
                    },
                }
            )
            continue
        if len(payload) > MAX_INLINE_TEXT_ATTACHMENT_BYTES:
            raise AttachmentError(
                "text attachment is too large for Claude inline delivery"
            )
        blocks.append(
            {
                "type": "text",
                "text": (
                    "[PeerBridge verified text attachment "
                    f"{attachment.relative_path}; sha256={attachment.sha256}]\n"
                    f"{payload.decode('utf-8-sig')}"
                ),
            }
        )
    return blocks


def acp_native_turn_payload(
    prompt: str,
    attachments: Iterable[VerifiedAttachment],
) -> tuple[str, tuple[dict[str, str], ...]]:
    """Build a native ACP prompt and binary attachment list.

    ACP defines binary prompt blocks for image and audio media. Image and audio
    bytes are passed as native ACP content blocks while verified text is folded
    into the prompt. Absolute paths are never disclosed to the provider.
    """

    text = str(prompt or "").rstrip()
    native: list[dict[str, str]] = []
    text_blocks: list[str] = []
    for attachment in attachments:
        payload = read_verified_attachment_payload(attachment)
        if attachment.kind in {"image", "audio"}:
            native.append(
                {
                    "mediaType": attachment.media_type,
                    "data": base64.b64encode(payload).decode("ascii"),
                }
            )
            continue
        if attachment.kind != "text":
            raise AttachmentError("ACP attachment kind is not supported")
        if len(payload) > MAX_INLINE_TEXT_ATTACHMENT_BYTES:
            raise AttachmentError("text attachment is too large for ACP inline delivery")
        try:
            decoded = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AttachmentError("verified text attachment is not valid UTF-8") from exc
        text_blocks.append(
            "[PeerBridge verified text attachment "
            f"{attachment.relative_path}; sha256={attachment.sha256}]\n{decoded}"
        )
    if text_blocks:
        suffix = "\n\n".join(text_blocks)
        text = f"{text}\n\n{suffix}" if text else suffix
    return text, tuple(native)


def attachment_path_instruction(
    attachments: Iterable[VerifiedAttachment],
    *,
    working_directory: Path,
) -> str:
    """Build a bounded prompt appendix for runtimes that read verified paths."""

    rows: list[str] = []
    base = Path(working_directory).resolve()
    for attachment in attachments:
        try:
            display_path = os.path.relpath(attachment.absolute_path, base)
        except ValueError:
            display_path = str(attachment.absolute_path)
        rows.append(
            f"- {display_path} ({attachment.media_type})"
        )
    if not rows:
        return ""
    return (
        "[PeerBridge verified attachments]\n"
        + "\n".join(rows)
        + "\nOpen only the listed files when your runtime supports it. "
        "Do not claim that an image was inspected unless it was actually opened and decoded."
    )


def attachment_delivery_receipt(
    *,
    provider_id: str,
    protocol: str,
    delivery_mode: str,
    status: str,
    attachments: Iterable[VerifiedAttachment],
) -> dict[str, object]:
    """Return a sanitized, SHA-bound transport receipt."""

    rows = tuple(attachments)
    body: dict[str, object] = {
        "provider_id": provider_id,
        "protocol": protocol,
        "delivery_mode": delivery_mode,
        "status": status,
        "attachment_count": len(rows),
        "model_view_confirmed": False,
        "attachments": [item.public_record() for item in rows],
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**body, "receipt_sha256": hashlib.sha256(encoded).hexdigest()}
