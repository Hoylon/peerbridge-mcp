"""Validate provider receipts before they can authorize a mailbox reply."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SUPPORTED_RECEIPT_SCHEMAS = frozenset(
    {
        "peerbridge.openai-compatible-run.v1",
        "peerbridge.acpx-inference-receipt.v1",
        "peerbridge.claude-native-wcm-inference-receipt.v1",
        "peerbridge.ccswitch-inference-receipt.v1",
    }
)


class InferenceReceiptError(ValueError):
    """A provider receipt is malformed or does not bind to the claimed run."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def _sha256(value: Any, field: str) -> str:
    text = str(value or "")
    if SHA256.fullmatch(text) is None:
        raise InferenceReceiptError(f"{field} is not a SHA-256 digest")
    return text


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InferenceReceiptError(f"{field} must be an object")
    return value


def _expect(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise InferenceReceiptError(f"provider receipt {field} does not match")


def validate_inference_receipt(
    receipt: Mapping[str, Any],
    *,
    message_id: str,
    assistant_message: Mapping[str, Any],
    reply_body: str,
    expected_route: Mapping[str, Any],
) -> dict[str, str]:
    """Return content-free receipt bindings after strict source/output checks."""
    schema = str(receipt.get("schema") or "")
    if schema not in SUPPORTED_RECEIPT_SCHEMAS:
        raise InferenceReceiptError("unsupported provider receipt schema")

    claimed_sha = _sha256(receipt.get("receipt_sha256"), "receipt_sha256")
    canonical_receipt = dict(receipt)
    canonical_receipt.pop("receipt_sha256", None)
    if stable_sha256(canonical_receipt) != claimed_sha:
        raise InferenceReceiptError("provider receipt self-hash does not match")

    if assistant_message.get("role") != "assistant":
        raise InferenceReceiptError("provider output is not an assistant message")
    content = assistant_message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise InferenceReceiptError("provider output content is empty or unsupported")
    if reply_body != content.strip():
        raise InferenceReceiptError("reply body does not match provider output")

    route_profile_id = expected_route.get("route_profile_id")
    route_profile_sha256 = expected_route.get("route_profile_sha256")
    route_class = expected_route.get("route_class")
    provider_id = expected_route.get("provider_id")
    model_id = expected_route.get("model_id")
    response_model_id = expected_route.get("response_model_id") or model_id
    reasoning_mode = expected_route.get("reasoning_mode")
    connection_id = expected_route.get("connection_id")

    if schema == "peerbridge.openai-compatible-run.v1":
        route = _mapping(receipt.get("route"), "receipt.route")
        for field, expected in (
            ("route_profile_id", route_profile_id),
            ("route_profile_sha256", route_profile_sha256),
            ("route_class", route_class),
            ("provider_id", provider_id),
            ("model_id", model_id),
            ("response_model_id", response_model_id),
            ("reasoning_mode", reasoning_mode),
            ("connection_id", connection_id),
        ):
            _expect(route.get(field), expected, f"route.{field}")
        connection_sha = expected_route.get("connection_sha256")
        _expect(
            route.get("connection_sha256"),
            connection_sha,
            "route.connection_sha256",
        )
        _expect(receipt.get("room_id"), expected_route.get("room_id"), "room_id")
        _expect(
            receipt.get("session_id"), expected_route.get("session_id"), "session_id"
        )
        _expect(
            receipt.get("message_id_sha256"),
            stable_sha256(message_id),
            "message_id_sha256",
        )
        _expect(
            receipt.get("output_message_sha256"),
            stable_sha256(dict(assistant_message)),
            "output_message_sha256",
        )
    elif schema == "peerbridge.acpx-inference-receipt.v1":
        _expect(receipt.get("secret_backend"), "native-acp", "secret_backend")
        for field, expected in (
            ("route_profile_id", route_profile_id),
            ("route_profile_sha256", route_profile_sha256),
            ("route_class", route_class),
            ("requested_provider_id", provider_id),
            ("requested_model", model_id),
            ("requested_reasoning_mode", reasoning_mode),
            ("connection_id", connection_id),
            ("message_id", message_id),
        ):
            _expect(receipt.get(field), expected, field)
        _expect(
            receipt.get("response_sha256"),
            sha256_bytes(content.encode("utf-8")),
            "response_sha256",
        )
    elif schema == "peerbridge.ccswitch-inference-receipt.v1":
        _expect(receipt.get("secret_backend"), "cc-switch", "secret_backend")
        for field, expected in (
            ("route_profile_id", route_profile_id),
            ("route_profile_sha256", route_profile_sha256),
            ("route_class", route_class),
            ("requested_model", model_id),
            ("connection_id", connection_id),
            ("message_id", message_id),
        ):
            _expect(receipt.get(field), expected, field)
        _expect(
            receipt.get("response_sha256"),
            sha256_bytes(content.encode("utf-8")),
            "response_sha256",
        )
    else:
        _expect(
            receipt.get("secret_backend"),
            "windows-credential-manager",
            "secret_backend",
        )
        for field, expected in (
            ("route_profile_id", route_profile_id),
            ("route_profile_sha256", route_profile_sha256),
            ("route_class", route_class),
            ("requested_provider_id", provider_id),
            ("requested_model", model_id),
            ("requested_reasoning_mode", reasoning_mode),
            ("connection_id", connection_id),
            ("endpoint_sha256", expected_route.get("endpoint_sha256")),
            (
                "credential_version_sha256",
                expected_route.get("credential_version_sha256"),
            ),
            ("message_id", message_id),
        ):
            _expect(receipt.get(field), expected, field)
        _expect(
            receipt.get("response_sha256"),
            sha256_bytes(content.encode("utf-8")),
            "response_sha256",
        )

    return {
        "receipt_schema": schema,
        "inference_receipt_sha256": claimed_sha,
        "assistant_message_sha256": stable_sha256(dict(assistant_message)),
        "assistant_content_sha256": sha256_bytes(content.encode("utf-8")),
        "reply_body_sha256": sha256_bytes(reply_body.encode("utf-8")),
        "expected_route_sha256": stable_sha256(dict(expected_route)),
    }


__all__ = [
    "InferenceReceiptError",
    "SUPPORTED_RECEIPT_SCHEMAS",
    "validate_inference_receipt",
]
