"""Bounded structural validation for supported image attachment formats."""

from __future__ import annotations

import binascii
import struct


MAX_IMAGE_DIMENSION = 32_768
MAX_IMAGE_PIXELS = 40_000_000


class _InvalidImage(ValueError):
    pass


def _dimensions_are_safe(width: int, height: int) -> bool:
    return (
        0 < width <= MAX_IMAGE_DIMENSION
        and 0 < height <= MAX_IMAGE_DIMENSION
        and width * height <= MAX_IMAGE_PIXELS
    )


def _validate_png(payload: bytes) -> bool:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    saw_header = False
    saw_data = False
    data_ended = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            return False
        length = struct.unpack_from(">I", payload, offset)[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            return False
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack_from(">I", payload, offset + 8 + length)[0]
        observed_crc = binascii.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if expected_crc != observed_crc:
            return False
        if not saw_header:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            allowed_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                not _dimensions_are_safe(width, height)
                or depth not in allowed_depths.get(color, set())
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                return False
            saw_header = True
        elif chunk_type == b"IHDR":
            return False
        if chunk_type == b"IDAT":
            if data_ended:
                return False
            saw_data = True
        elif saw_data and chunk_type != b"IEND":
            data_ended = True
        if chunk_type == b"IEND":
            return length == 0 and saw_header and saw_data and chunk_end == len(payload)
        offset = chunk_end
    return False


_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _validate_jpeg(payload: bytes) -> bool:
    if len(payload) < 4 or payload[:2] != b"\xff\xd8":
        return False
    offset = 2
    saw_frame = False
    saw_scan = False
    while offset < len(payload):
        if payload[offset] != 0xFF:
            return False
        marker_offset = offset
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            return False
        marker = payload[offset]
        offset += 1
        if marker == 0xD9:
            return saw_frame and saw_scan and offset == len(payload)
        if marker in {0x00, 0xD8} or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            return False
        if offset + 2 > len(payload):
            return False
        segment_length = struct.unpack_from(">H", payload, offset)[0]
        if segment_length < 2:
            return False
        segment_end = offset + segment_length
        if segment_end > len(payload):
            return False
        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 8:
                return False
            height, width = struct.unpack_from(">HH", payload, offset + 3)
            if not _dimensions_are_safe(width, height):
                return False
            saw_frame = True
        if marker != 0xDA:
            offset = segment_end
            continue
        saw_scan = True
        offset = segment_end
        while offset < len(payload):
            marker_offset = payload.find(b"\xff", offset)
            if marker_offset < 0 or marker_offset + 1 >= len(payload):
                return False
            next_byte = payload[marker_offset + 1]
            if next_byte == 0x00 or 0xD0 <= next_byte <= 0xD7:
                offset = marker_offset + 2
                continue
            if next_byte == 0xFF:
                offset = marker_offset + 1
                continue
            offset = marker_offset
            break
    return False


def _skip_gif_sub_blocks(payload: bytes, offset: int) -> int:
    while True:
        if offset >= len(payload):
            raise _InvalidImage
        length = payload[offset]
        offset += 1
        if length == 0:
            return offset
        offset += length
        if offset > len(payload):
            raise _InvalidImage


def _validate_gif(payload: bytes) -> bool:
    if len(payload) < 14 or payload[:6] not in {b"GIF87a", b"GIF89a"}:
        return False
    width, height = struct.unpack_from("<HH", payload, 6)
    if not _dimensions_are_safe(width, height):
        return False
    packed = payload[10]
    offset = 13 + (3 * (2 ** ((packed & 0x07) + 1)) if packed & 0x80 else 0)
    saw_image = False
    try:
        while offset < len(payload):
            introducer = payload[offset]
            offset += 1
            if introducer == 0x3B:
                return saw_image and offset == len(payload)
            if introducer == 0x21:
                if offset >= len(payload):
                    return False
                offset += 1
                offset = _skip_gif_sub_blocks(payload, offset)
                continue
            if introducer != 0x2C or offset + 9 > len(payload):
                return False
            _, _, image_width, image_height, image_packed = struct.unpack_from(
                "<HHHHB", payload, offset
            )
            if not _dimensions_are_safe(image_width, image_height):
                return False
            offset += 9
            if image_packed & 0x80:
                offset += 3 * (2 ** ((image_packed & 0x07) + 1))
            if offset >= len(payload):
                return False
            offset += 1
            offset = _skip_gif_sub_blocks(payload, offset)
            saw_image = True
    except (_InvalidImage, struct.error):
        return False
    return False


def _uint24_le(value: bytes) -> int:
    if len(value) != 3:
        raise _InvalidImage
    return value[0] | (value[1] << 8) | (value[2] << 16)


def _validate_webp(payload: bytes) -> bool:
    if (
        len(payload) < 20
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
        or struct.unpack_from("<I", payload, 4)[0] + 8 != len(payload)
    ):
        return False
    offset = 12
    dimensions: tuple[int, int] | None = None
    saw_image_data = False
    try:
        while offset < len(payload):
            if offset + 8 > len(payload):
                return False
            chunk_type = payload[offset : offset + 4]
            chunk_length = struct.unpack_from("<I", payload, offset + 4)[0]
            data_start = offset + 8
            data_end = data_start + chunk_length
            padded_end = data_end + (chunk_length & 1)
            if data_end > len(payload) or padded_end > len(payload):
                return False
            data = payload[data_start:data_end]
            if chunk_type == b"VP8X":
                if len(data) != 10:
                    return False
                dimensions = (_uint24_le(data[4:7]) + 1, _uint24_le(data[7:10]) + 1)
            elif chunk_type == b"VP8 ":
                if len(data) < 10 or data[3:6] != b"\x9d\x01\x2a":
                    return False
                dimensions = (
                    struct.unpack_from("<H", data, 6)[0] & 0x3FFF,
                    struct.unpack_from("<H", data, 8)[0] & 0x3FFF,
                )
                saw_image_data = True
            elif chunk_type == b"VP8L":
                if len(data) < 5 or data[0] != 0x2F:
                    return False
                bits = int.from_bytes(data[1:5], "little")
                dimensions = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
                saw_image_data = True
            elif chunk_type == b"ANMF":
                if len(data) < 16:
                    return False
                frame_dimensions = (
                    _uint24_le(data[6:9]) + 1,
                    _uint24_le(data[9:12]) + 1,
                )
                if not _dimensions_are_safe(*frame_dimensions):
                    return False
                saw_image_data = True
            offset = padded_end
    except (_InvalidImage, struct.error):
        return False
    return (
        offset == len(payload)
        and dimensions is not None
        and _dimensions_are_safe(*dimensions)
        and saw_image_data
    )


def image_payload_is_valid(suffix: str, payload: bytes) -> bool:
    """Return whether an image is structurally complete and safely bounded."""
    normalized = suffix.lower()
    if normalized == ".png":
        return _validate_png(payload)
    if normalized in {".jpg", ".jpeg"}:
        return _validate_jpeg(payload)
    if normalized == ".gif":
        return _validate_gif(payload)
    if normalized == ".webp":
        return _validate_webp(payload)
    return False
