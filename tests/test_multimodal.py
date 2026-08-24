from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from peerbridge_mcp.attachments import AttachmentError, stage_chat_attachment_payloads
from peerbridge_mcp.multimodal import (
    VERIFIED_ATTACHMENT_MESSAGE_KEY,
    acp_native_turn_payload,
    attachment_delivery_receipt,
    attachment_path_instruction,
    claude_native_content_blocks,
    create_vision_challenge,
    extract_verified_attachments,
    read_verified_attachment_payload,
    verify_staged_attachment_paths,
    verify_staged_attachments,
    vision_verification_receipt,
)
from peerbridge_mcp.image_validation import image_payload_is_valid
from tests._image_fixtures import PNG


def test_empty_multimodal_turn_does_not_require_an_attachment_store(
    tmp_path: Path,
) -> None:
    assert verify_staged_attachments(tmp_path, ()) == ()
    assert not (tmp_path / ".peerbridge-artifacts" / "chat").exists()


def test_staged_multimodal_inputs_are_reverified_and_receipted_without_paths(
    tmp_path: Path,
) -> None:
    staged = stage_chat_attachment_payloads(
        tmp_path,
        (("chart.png", PNG), ("notes.txt", b"Inspect the visible breakout structure.")),
    )

    verified = verify_staged_attachments(tmp_path, staged)
    assert [item.kind for item in verified] == ["image", "text"]
    assert [item.bytes for item in verified] == [len(PNG), 39]
    assert all(item.absolute_path.is_file() for item in verified)

    instruction = attachment_path_instruction(verified, working_directory=tmp_path)
    assert "PeerBridge verified attachments" in instruction
    assert "Do not claim that an image was inspected" in instruction
    assert str(tmp_path) not in instruction

    receipt = attachment_delivery_receipt(
        provider_id="codex",
        protocol="codex-app-server-jsonrpc",
        delivery_mode="native_local_image_and_verified_path",
        status="transport_accepted",
        attachments=verified,
    )
    assert receipt["attachment_count"] == 2
    assert receipt["model_view_confirmed"] is False
    assert [item["kind"] for item in receipt["attachments"]] == ["image", "text"]
    serialized = json.dumps(receipt, sort_keys=True)
    assert str(tmp_path) not in serialized
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert receipt["receipt_sha256"] == expected


def test_provider_boundary_rejects_tampered_staged_bytes(tmp_path: Path) -> None:
    staged = stage_chat_attachment_payloads(tmp_path, (("chart.png", PNG),))
    target = tmp_path / staged[0].relative_path
    target.write_bytes(PNG[:-1] + bytes([PNG[-1] ^ 1]))

    with pytest.raises(AttachmentError, match="no longer matches"):
        verify_staged_attachments(tmp_path, staged)


def test_provider_boundary_rejects_receipt_path_escape(tmp_path: Path) -> None:
    staged = stage_chat_attachment_payloads(tmp_path, (("chart.png", PNG),))
    escaped = asdict(staged[0]) | {"relative_path": "../chart.png"}

    with pytest.raises(AttachmentError, match="receipt is invalid"):
        verify_staged_attachments(tmp_path, (escaped,))


def test_room_paths_are_reverified_and_private_metadata_is_removed(
    tmp_path: Path,
) -> None:
    staged = stage_chat_attachment_payloads(
        tmp_path,
        (("chart.png", PNG), ("notes.txt", b"Visible base above support.")),
    )
    verified = verify_staged_attachment_paths(
        tmp_path,
        [item.relative_path for item in staged],
    )
    messages = [
        {
            "role": "user",
            "content": "Review both attachments.",
            VERIFIED_ATTACHMENT_MESSAGE_KEY: [
                item.public_record() for item in verified
            ],
        }
    ]

    cleaned, extracted = extract_verified_attachments(tmp_path, messages)

    assert cleaned == [{"role": "user", "content": "Review both attachments."}]
    assert [item.kind for item in extracted] == ["image", "text"]
    assert read_verified_attachment_payload(extracted[0]) == PNG
    assert read_verified_attachment_payload(extracted[1]) == b"Visible base above support."
    assert str(tmp_path) not in json.dumps(
        [item.public_record() for item in extracted], sort_keys=True
    )


def test_acp_native_turn_payload_uses_binary_image_and_bounded_inline_text(
    tmp_path: Path,
) -> None:
    staged = stage_chat_attachment_payloads(
        tmp_path,
        (("chart.png", PNG), ("notes.txt", b"Visible base above support.")),
    )
    verified = verify_staged_attachments(tmp_path, staged)

    text, native = acp_native_turn_payload("Review the evidence.", verified)

    assert len(native) == 1
    assert native[0]["mediaType"] == "image/png"
    assert base64.b64decode(native[0]["data"], validate=True) == PNG
    assert "Review the evidence." in text
    assert "PeerBridge verified text attachment" in text
    assert "Visible base above support." in text
    assert "chart.png" not in text
    assert str(tmp_path) not in text


def test_acp_native_turn_payload_uses_binary_audio_without_disclosing_a_path(
    tmp_path: Path,
) -> None:
    audio = b"RIFF" + (8).to_bytes(4, "little") + b"WAVE" + b"data"
    staged = stage_chat_attachment_payloads(tmp_path, (("voice.wav", audio),))
    verified = verify_staged_attachments(tmp_path, staged)

    text, native = acp_native_turn_payload("Transcribe the audio.", verified)

    assert [item.kind for item in verified] == ["audio"]
    assert text == "Transcribe the audio."
    assert native[0]["mediaType"] == "audio/wav"
    assert base64.b64decode(native[0]["data"], validate=True) == audio
    assert "voice.wav" not in text
    assert str(tmp_path) not in text


def test_claude_native_blocks_reject_audio_before_transport(tmp_path: Path) -> None:
    audio = b"RIFF" + (8).to_bytes(4, "little") + b"WAVE" + b"data"
    staged = stage_chat_attachment_payloads(tmp_path, (("voice.wav", audio),))
    verified = verify_staged_attachments(tmp_path, staged)

    with pytest.raises(AttachmentError, match="does not advertise native audio"):
        claude_native_content_blocks("Transcribe the audio.", verified)


def test_vision_challenge_answer_exists_only_in_valid_image_pixels() -> None:
    challenge = create_vision_challenge(code="234567")

    assert image_payload_is_valid(".png", challenge.png) is True
    assert challenge.image_sha256 == hashlib.sha256(challenge.png).hexdigest()
    assert "234567" not in challenge.prompt
    assert "234567" not in challenge.challenge_id
    assert "234567" not in repr(challenge)


def test_vision_receipt_requires_exact_answer_and_never_contains_answer() -> None:
    challenge = create_vision_challenge(code="876543")
    passed = vision_verification_receipt(
        challenge=challenge,
        answer="VISION:876543",
        provider_id="codex",
        protocol="codex-app-server-jsonrpc",
        delivery_mode="native_local_image",
        provider_identity="openai-official-codex",
        model_id="gpt-test",
        client_version="test",
    )
    failed = vision_verification_receipt(
        challenge=challenge,
        answer="VISION:876543 extra",
        provider_id="codex",
        protocol="codex-app-server-jsonrpc",
        delivery_mode="native_local_image",
        provider_identity="openai-official-codex",
        model_id="gpt-test",
        client_version="test",
    )
    non_ascii = vision_verification_receipt(
        challenge=challenge,
        answer="我看到 VISION:876543",
        provider_id="codex",
        protocol="codex-app-server-jsonrpc",
        delivery_mode="native_local_image",
        provider_identity="openai-official-codex",
        model_id="gpt-test",
        client_version="test",
    )

    assert passed["status"] == "semantic_image_verified"
    assert passed["model_view_confirmed"] is True
    assert failed["status"] == "semantic_image_failed"
    assert failed["model_view_confirmed"] is False
    assert non_ascii["status"] == "semantic_image_failed"
    assert non_ascii["model_view_confirmed"] is False
    assert "876543" not in json.dumps(passed, sort_keys=True)
    assert "876543" not in json.dumps(failed, sort_keys=True)
