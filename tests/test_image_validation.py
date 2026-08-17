from __future__ import annotations

import binascii
import struct

import pytest

from peerbridge_mcp.image_validation import image_payload_is_valid
from tests._image_fixtures import GIF, JPEG, PNG, WEBP


@pytest.mark.parametrize(
    ("suffix", "payload"),
    [(".png", PNG), (".jpg", JPEG), (".jpeg", JPEG), (".gif", GIF), (".webp", WEBP)],
)
def test_complete_supported_images_are_accepted(suffix: str, payload: bytes) -> None:
    assert image_payload_is_valid(suffix, payload)


@pytest.mark.parametrize(
    ("suffix", "payload"),
    [(".png", PNG), (".jpg", JPEG), (".gif", GIF), (".webp", WEBP)],
)
def test_truncated_and_appended_images_are_rejected(suffix: str, payload: bytes) -> None:
    assert not image_payload_is_valid(suffix, payload[:-1])
    assert not image_payload_is_valid(suffix, payload + b"appended-polyglot-data")


def test_png_with_unsafe_dimensions_is_rejected_even_with_valid_crc() -> None:
    payload = bytearray(PNG)
    struct.pack_into(">II", payload, 16, 32_768, 32_768)
    struct.pack_into(">I", payload, 29, binascii.crc32(payload[12:29]) & 0xFFFFFFFF)
    assert not image_payload_is_valid(".png", bytes(payload))


def test_corrupt_png_chunk_crc_is_rejected() -> None:
    payload = bytearray(PNG)
    payload[20] ^= 1
    assert not image_payload_is_valid(".png", bytes(payload))


def test_unknown_suffix_is_rejected() -> None:
    assert not image_payload_is_valid(".bmp", PNG)
