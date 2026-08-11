"""Dependency-free MCP stdio server exposing the PeerBridge tools."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from typing import Any, Callable

from . import __version__
from .bridge import Bridge, BridgeError, stable_sha256
from .protocol import (
    LEGACY_PROTOCOLS,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOLS,
    content_response,
    direct_response,
    error_response,
)


SERVER_NAME = "peerbridge-mcp"


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


STRING = {"type": "string"}
STRING_ARRAY = {"type": "array", "items": STRING}

TOOL_SCHEMAS = [
    {
        "name": "bridge_status",
        "description": "Return local bridge, task, presence and audit status.",
        "inputSchema": _object({}),
    },
    {
        "name": "send_message",
        "description": "Send a SHA-bound message without invoking the recipient automatically.",
        "inputSchema": _object(
            {
                "recipient": STRING,
                "task_id": STRING,
                "subject": STRING,
                "body": STRING,
                "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"]},
                "reply_to": STRING,
                "artifact_paths": STRING_ARRAY,
            },
            ["recipient", "task_id", "subject", "body"],
        ),
    },
    {
        "name": "poll_messages",
        "description": "Poll this consumer's ordered mailbox using its durable cursor.",
        "inputSchema": _object(
            {
                "agent_id": STRING,
                "after_cursor": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "include_sent": {"type": "boolean"},
            }
        ),
    },
    {
        "name": "ack_message",
        "description": "Acknowledge a message for one consumer and advance only its contiguous cursor.",
        "inputSchema": _object({"message_id": STRING, "agent_id": STRING}, ["message_id"]),
    },
    {
        "name": "claim_task",
        "description": "Claim a task lease after fail-closed read/write path conflict checks.",
        "inputSchema": _object(
            {
                "task_id": STRING,
                "summary": STRING,
                "owner": STRING,
                "read_paths": STRING_ARRAY,
                "write_paths": STRING_ARRAY,
                "lease_seconds": {"type": "integer", "minimum": 30, "maximum": 86400},
                "approval_mode": {
                    "type": "string",
                    "enum": [
                        "solo_allowed",
                        "two_party_required",
                        "presence_aware",
                        "quorum_required",
                    ],
                },
                "required_peer": STRING,
                "required_peers": STRING_ARRAY,
                "review_quorum": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["task_id", "summary"],
        ),
    },
    {
        "name": "renew_task",
        "description": "Renew an active task lease using its capability token.",
        "inputSchema": _object(
            {
                "task_id": STRING,
                "lease_token": STRING,
                "lease_seconds": {"type": "integer", "minimum": 30, "maximum": 86400},
            },
            ["task_id", "lease_token"],
        ),
    },
    {
        "name": "release_task",
        "description": "Release an active lease as open or blocked; completion uses complete_task.",
        "inputSchema": _object(
            {
                "task_id": STRING,
                "lease_token": STRING,
                "status": {"type": "string", "enum": ["open", "blocked"]},
                "reason": STRING,
            },
            ["task_id", "lease_token"],
        ),
    },
    {
        "name": "announce_work",
        "description": "Append a status update to a leased task.",
        "inputSchema": _object(
            {
                "task_id": STRING,
                "lease_token": STRING,
                "summary": STRING,
                "status": {"type": "string", "enum": ["working", "waiting", "review"]},
                "artifact_paths": STRING_ARRAY,
            },
            ["task_id", "lease_token", "summary"],
        ),
    },
    {
        "name": "workboard",
        "description": "Show task leases, declared paths, latest updates and live peers.",
        "inputSchema": _object(
            {
                "include_completed": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    },
    {
        "name": "request_review",
        "description": "Queue a source-bound peer review request for another agent.",
        "inputSchema": _object(
            {
                "task_id": STRING,
                "lease_token": STRING,
                "recipient": STRING,
                "question": STRING,
                "artifact_paths": STRING_ARRAY,
            },
            ["task_id", "lease_token", "recipient", "question"],
        ),
    },
    {
        "name": "poll_reviews",
        "description": "Poll ordered peer requests addressed to or created by an agent.",
        "inputSchema": _object(
            {
                "agent_id": STRING,
                "after_cursor": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "include_closed": {"type": "boolean"},
            }
        ),
    },
    {
        "name": "submit_review",
        "description": "Submit one substantive equal-peer verdict as the addressed reviewer.",
        "inputSchema": _object(
            {
                "request_id": STRING,
                "verdict": {
                    "type": "string",
                    "enum": ["approved", "changes_requested", "blocked"],
                },
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                "findings": STRING,
                "response": STRING,
                "artifact_paths": STRING_ARRAY,
            },
            ["request_id", "verdict", "score", "findings"],
        ),
    },
    {
        "name": "review_summary",
        "description": "Evaluate the task's presence-aware approval policy without applying changes.",
        "inputSchema": _object({"task_id": STRING}, ["task_id"]),
    },
    {
        "name": "read_artifact",
        "description": "Read a non-sensitive project artifact with its live SHA-256.",
        "inputSchema": _object(
            {"path": STRING, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 500000}},
            ["path"],
        ),
    },
    {
        "name": "hash_artifact",
        "description": "Hash a project file without returning its contents.",
        "inputSchema": _object({"path": STRING}, ["path"]),
    },
    {
        "name": "submit_plan",
        "description": "Write an isolated plan draft; never edits project files.",
        "inputSchema": _object(
            {"task_id": STRING, "lease_token": STRING, "plan": STRING},
            ["task_id", "lease_token", "plan"],
        ),
    },
    {
        "name": "submit_patch",
        "description": "Write an isolated non-destructive patch draft within the task write scope.",
        "inputSchema": _object(
            {
                "task_id": STRING,
                "lease_token": STRING,
                "change_summary": STRING,
                "patch": STRING,
                "target_paths": STRING_ARRAY,
            },
            ["task_id", "lease_token", "change_summary", "patch", "target_paths"],
        ),
    },
    {
        "name": "record_proof",
        "description": "Record hashes, tests and evidence for changes already applied outside the bridge.",
        "inputSchema": _object(
            {
                "task_id": STRING,
                "lease_token": STRING,
                "change_summary": STRING,
                "changed_paths": STRING_ARRAY,
                "before_hashes": {"type": "object"},
                "tests": STRING,
                "evidence_paths": STRING_ARRAY,
                "review_ids": STRING_ARRAY,
            },
            ["task_id", "lease_token", "change_summary", "tests"],
        ),
    },
    {
        "name": "change_log",
        "description": "Read proof records, live hashes, tests and review IDs.",
        "inputSchema": _object(
            {"task_id": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}
        ),
    },
    {
        "name": "complete_task",
        "description": "Complete a task only after live proof rehash and its approval policy pass.",
        "inputSchema": _object(
            {"task_id": STRING, "lease_token": STRING},
            ["task_id", "lease_token"],
        ),
    },
    {
        "name": "verify_audit_chain",
        "description": "Verify the append-only SHA-256 event chain without mutating project artifacts.",
        "inputSchema": _object({}),
    },
]

READ_ONLY_TOOLS = {
    "bridge_status",
    "poll_messages",
    "workboard",
    "poll_reviews",
    "review_summary",
    "read_artifact",
    "hash_artifact",
    "change_log",
    "verify_audit_chain",
}

for _tool in TOOL_SCHEMAS:
    _tool["annotations"] = {
        "readOnlyHint": _tool["name"] in READ_ONLY_TOOLS,
        "destructiveHint": False,
        "idempotentHint": _tool["name"] in READ_ONLY_TOOLS,
        "openWorldHint": False,
    }


HANDLERS: dict[str, str] = {
    "bridge_status": "status",
    "send_message": "send_message",
    "poll_messages": "poll_messages",
    "ack_message": "ack_message",
    "claim_task": "claim_task",
    "renew_task": "renew_task",
    "release_task": "release_task",
    "announce_work": "announce_work",
    "workboard": "workboard",
    "request_review": "request_review",
    "poll_reviews": "poll_reviews",
    "submit_review": "submit_review",
    "review_summary": "review_summary",
    "read_artifact": "read_artifact",
    "hash_artifact": "hash_artifact",
    "submit_plan": "submit_plan",
    "submit_patch": "submit_patch",
    "record_proof": "record_proof",
    "change_log": "change_log",
    "complete_task": "complete_task",
    "verify_audit_chain": "verify_audit_chain",
}


def dispatch(bridge: Bridge, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    method_name = HANDLERS.get(name)
    if method_name is None:
        raise BridgeError(f"unknown tool: {name}")
    method: Callable[[dict[str, Any]], dict[str, Any]] = getattr(bridge, method_name)
    return method(arguments)


def _request_protocol_version(request: dict[str, Any]) -> str | None:
    params = request.get("params")
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    version = meta.get("io.modelcontextprotocol/protocolVersion")
    return str(version) if version is not None else None


def _unsupported_protocol(request_id: Any, requested: str | None) -> dict[str, Any]:
    return error_response(
        request_id,
        -32022,
        "Unsupported protocol version",
        data={"supported": list(SUPPORTED_PROTOCOLS), "requested": requested},
    )


def handle_request(bridge: Bridge, request: dict[str, Any]) -> dict[str, Any] | None:
    bridge.touch_presence("stdio")
    request_id = request.get("id")
    method = request.get("method")
    requested_version = _request_protocol_version(request)
    modern = requested_version is not None
    if modern and requested_version not in SUPPORTED_PROTOCOLS:
        return _unsupported_protocol(request_id, requested_version)
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return direct_response(request_id, {})
    if method == "initialize":
        requested = request.get("params", {}).get("protocolVersion", PROTOCOL_VERSION)
        selected = requested if requested in LEGACY_PROTOCOLS else PROTOCOL_VERSION
        return direct_response(
            request_id,
            {
                "protocolVersion": selected,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "Coordinate through leased path scopes and SHA-bound proof. "
                    "PeerBridge never applies patches or starts another model automatically."
                ),
            },
        )
    if method == "server/discover":
        if requested_version is None:
            return _unsupported_protocol(request_id, None)
        return direct_response(
            request_id,
            {
                "resultType": "complete",
                "supportedVersions": list(SUPPORTED_PROTOCOLS),
                "capabilities": {"tools": {"listChanged": False}},
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": SERVER_NAME,
                        "version": __version__,
                    }
                },
                "instructions": (
                    "Coordinate equal coding peers through leased path scopes, SHA-bound "
                    "messages, reviews and proof. PeerBridge never applies patches or starts "
                    "another model automatically."
                ),
                "ttlMs": 300_000,
                "cacheScope": "public",
            },
        )
    if method == "tools/list":
        result: dict[str, Any] = {"tools": TOOL_SCHEMAS}
        if modern:
            result.update(
                {
                    "resultType": "complete",
                    "ttlMs": 300_000,
                    "cacheScope": "public",
                }
            )
        return direct_response(request_id, result)
    if method == "tools/call":
        params = request.get("params", {})
        if not isinstance(params, dict):
            return error_response(request_id, -32602, "tool call params must be an object")
        name = params.get("name")
        if not isinstance(name, str) or name not in HANDLERS:
            return error_response(request_id, -32602, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return error_response(request_id, -32602, "tool arguments must be an object")
        with bridge._connect() as connection:
            bridge._event(
                connection,
                "tool.called",
                {"tool": name, "arguments_sha256": stable_sha256(arguments)},
            )
        try:
            result = dispatch(bridge, str(name), arguments)
            with bridge._connect() as connection:
                bridge._event(
                    connection,
                    "tool.returned",
                    {"tool": name, "result_sha256": stable_sha256(result)},
                )
            return content_response(request_id, result, modern=modern)
        except (BridgeError, ValueError, TypeError) as exc:
            with bridge._connect() as connection:
                bridge._event(
                    connection,
                    "tool.failed",
                    {"tool": name, "error_sha256": stable_sha256(str(exc))},
                )
            return content_response(
                request_id,
                {"error": str(exc), "tool": name},
                modern=modern,
                is_error=True,
            )
        except sqlite3.Error as exc:
            with bridge._connect() as connection:
                bridge._event(
                    connection,
                    "tool.failed",
                    {"tool": name, "error_sha256": stable_sha256(str(exc))},
                )
            return error_response(request_id, -32603, "internal bridge database error")
    return error_response(request_id, -32601, f"method not found: {method}")


def _heartbeat(bridge: Bridge, stop: threading.Event) -> None:
    while not stop.wait(30):
        try:
            bridge.touch_presence("stdio")
        except sqlite3.Error:
            continue


def serve(bridge: Bridge) -> int:
    bridge.touch_presence("stdio")
    stop = threading.Event()
    thread = threading.Thread(target=_heartbeat, args=(bridge, stop), daemon=True)
    thread.start()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line.lstrip("\ufeff"))
                result = handle_request(bridge, request)
                if result is not None:
                    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
            except json.JSONDecodeError as exc:
                sys.stdout.write(json.dumps(error_response(None, -32700, f"invalid JSON: {exc}")) + "\n")
                sys.stdout.flush()
            except Exception as exc:  # pragma: no cover - final transport guard
                print(f"peerbridge internal error: {exc}", file=sys.stderr, flush=True)
                request_id = request.get("id") if isinstance(locals().get("request"), dict) else None
                if request_id is not None:
                    sys.stdout.write(json.dumps(error_response(request_id, -32603, "internal bridge error")) + "\n")
                    sys.stdout.flush()
    finally:
        stop.set()
        thread.join(timeout=2)
        bridge.clear_presence()
    return 0
