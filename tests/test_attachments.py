from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import peerbridge_mcp.attachments as attachments_module
from peerbridge_mcp.attachments import (
    MAX_CHAT_ATTACHMENT_BYTES,
    AttachmentError,
    stage_chat_attachments,
)


PNG = b"\x89PNG\r\n\x1a\npeerbridge-test"


def test_safe_attachments_are_content_addressed_and_idempotent(tmp_path: Path) -> None:
    image = tmp_path / "private-name.png"
    note = tmp_path / "notes.txt"
    image.write_bytes(PNG)
    note.write_text("ordinary diagnostic context", encoding="utf-8")

    first = stage_chat_attachments(tmp_path, [image, note, image])
    second = stage_chat_attachments(tmp_path, [image, note])

    assert first == second
    assert len(first) == 2
    for item, payload in zip(first, (PNG, note.read_bytes()), strict=True):
        assert item.sha256 == hashlib.sha256(payload).hexdigest()
        assert Path(item.relative_path).name.startswith(item.sha256)
        assert "private-name" not in item.relative_path
        assert (tmp_path / item.relative_path).read_bytes() == payload


@pytest.mark.parametrize(
    ("name", "payload", "message"),
    [
        ("program.exe", b"MZ", "type is not allowed"),
        ("fake.png", b"this is not a png", "does not match"),
        ("broken.json", b"{not-json", "does not match"),
    ],
)
def test_unsafe_attachment_content_is_rejected(
    tmp_path: Path, name: str, payload: bytes, message: str
) -> None:
    source = tmp_path / name
    source.write_bytes(payload)
    with pytest.raises(AttachmentError, match=message):
        stage_chat_attachments(tmp_path, [source])


def test_credential_like_text_is_rejected_without_embedding_a_test_key(
    tmp_path: Path,
) -> None:
    source = tmp_path / "secret.txt"
    source.write_bytes(b"token=" + b"sk" + b"-" + b"1234567890abcdef")
    with pytest.raises(AttachmentError, match="credential"):
        stage_chat_attachments(tmp_path, [source])


def test_generic_credential_assignment_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "diagnostic.txt"
    source.write_bytes(b"api_" + b"key=" + b"Q" * 24)
    with pytest.raises(AttachmentError, match="credential"):
        stage_chat_attachments(tmp_path, [source])


def test_oversized_attachment_is_rejected_without_staging(tmp_path: Path) -> None:
    source = tmp_path / "large.txt"
    source.write_bytes(b"x" * (MAX_CHAT_ATTACHMENT_BYTES + 1))
    with pytest.raises(AttachmentError, match="too large"):
        stage_chat_attachments(tmp_path, [source])
    assert not list((tmp_path / ".peerbridge-artifacts" / "chat").glob("*"))


def test_symbolic_link_attachment_is_rejected_when_supported(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    link = tmp_path / "link.txt"
    source.write_text("safe text", encoding="utf-8")
    try:
        os.symlink(source, link)
    except OSError:
        pytest.skip("symbolic-link creation is unavailable on this Windows host")
    with pytest.raises(AttachmentError, match="symbolic links"):
        stage_chat_attachments(tmp_path, [link])


def test_source_swap_between_validation_and_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "screen.png"
    replacement = tmp_path / "replacement.png"
    source.write_bytes(PNG)
    replacement.write_bytes(PNG + b"-replacement")
    real_open = attachments_module.os.open
    swapped = False

    def swap_then_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if not swapped and Path(path) == source.resolve():
            swapped = True
            os.replace(replacement, source)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(attachments_module.os, "open", swap_then_open)

    with pytest.raises(AttachmentError, match="changed before it was opened"):
        stage_chat_attachments(tmp_path, [source])
    assert not list((tmp_path / ".peerbridge-artifacts" / "chat").glob("*"))
