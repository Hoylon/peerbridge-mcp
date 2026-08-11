"""Small dependency-free helpers for the MCP stdio JSON-RPC transport."""

from __future__ import annotations

import json
from typing import Any


MODERN_PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOLS = (
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
LEGACY_PROTOCOLS = tuple(
    version for version in SUPPORTED_PROTOCOLS if version != MODERN_PROTOCOL_VERSION
)


def content_response(
    request_id: Any,
    result: Any,
    *,
    modern: bool = False,
    is_error: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, sort_keys=True),
            }
        ],
        "isError": is_error,
    }
    if modern:
        payload.update(
            {
                "resultType": "complete",
                "structuredContent": result,
            }
        )
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": payload,
    }


def direct_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(
    request_id: Any,
    code: int,
    message: str,
    *,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }
