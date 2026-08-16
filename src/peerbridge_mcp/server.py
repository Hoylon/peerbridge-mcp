"""Dependency-free MCP stdio server exposing the PeerBridge tools."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
from typing import AbstractSet, Any, Callable

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
SHA256_OR_NULL = {
    "anyOf": [
        {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        {"type": "null"},
    ]
}
HASH_MAP = {"type": "object", "additionalProperties": SHA256_OR_NULL}

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
                "room_id": STRING,
                "recipient": STRING,
                "task_id": STRING,
                "subject": STRING,
                "body": STRING,
                "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"]},
                "reply_to": STRING,
                "artifact_paths": STRING_ARRAY,
                "route_profile_id": STRING,
                "requested_provider_id": STRING,
                "requested_model_id": STRING,
                "requested_reasoning_mode": STRING,
                "requested_route_class": {
                    "type": "string",
                    "enum": ["official", "relay", "local"],
                },
            },
            ["recipient", "task_id", "subject", "body"],
        ),
    },
    {
        "name": "send_room_fanout",
        "description": (
            "Atomically create one exact routed message from the calling room member "
            "to every other active Agent seat in the selected room, including Lobby. Every destination "
            "seat must have an enabled route profile; replies are direct and do not "
            "automatically fan out again."
        ),
        "inputSchema": _object(
            {
                "room_id": STRING,
                "task_id": STRING,
                "subject": STRING,
                "body": STRING,
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "critical"],
                },
                "artifact_paths": STRING_ARRAY,
            },
            ["room_id", "task_id", "subject", "body"],
        ),
    },
    {
        "name": "post_room_message",
        "description": (
            "Post one room message using the persisted room automation policy. "
            "off records only, once fans out one parallel response round, and "
            "discussion opens a bounded orchestrated multi-round discussion."
        ),
        "inputSchema": _object(
            {
                "room_id": STRING,
                "task_id": STRING,
                "subject": STRING,
                "body": STRING,
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "critical"],
                },
                "artifact_paths": STRING_ARRAY,
            },
            ["room_id", "task_id", "subject", "body"],
        ),
    },
    {
        "name": "get_room_automation",
        "description": "Read one room's auto-response policy and active discussion state.",
        "inputSchema": _object({"room_id": STRING}, ["room_id"]),
    },
    {
        "name": "set_room_automation",
        "description": (
            "Set a room's auto-response mode and bounded discussion limits. "
            "Only the room creator or human operator may change it."
        ),
        "inputSchema": _object(
            {
                "room_id": STRING,
                "mode": {
                    "type": "string",
                    "enum": ["off", "once", "discussion"],
                },
                "max_rounds": {"type": "integer", "minimum": 1, "maximum": 20},
                "max_messages": {"type": "integer", "minimum": 2, "maximum": 200},
                "stagnation_rounds": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            ["room_id", "mode"],
        ),
    },
    {
        "name": "control_discussion",
        "description": "Pause, resume, stop, or extend one bounded room discussion.",
        "inputSchema": _object(
            {
                "discussion_id": STRING,
                "action": {
                    "type": "string",
                    "enum": ["pause", "resume", "stop", "continue"],
                },
                "extra_rounds": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            ["discussion_id", "action"],
        ),
    },
    {
        "name": "advance_discussions",
        "description": (
            "Coordinator-only idempotent round advancement after every prompt in a "
            "discussion round reaches a terminal dispatch state."
        ),
        "inputSchema": _object(
            {
                "room_id": STRING,
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            }
        ),
    },
    {
        "name": "reconcile_message_dispatches",
        "description": (
            "Coordinator-only terminal reconciliation for active discussion prompts "
            "whose seat, immutable route, or retry budget can no longer run."
        ),
        "inputSchema": _object(
            {"limit": {"type": "integer", "minimum": 1, "maximum": 1000}}
        ),
    },
    {
        "name": "create_room",
        "description": "Create a durable conversation room with isolated membership and cursors.",
        "inputSchema": _object(
            {"room_id": STRING, "name": STRING}, ["room_id", "name"]
        ),
    },
    {
        "name": "list_rooms",
        "description": "List conversation rooms without changing membership.",
        "inputSchema": _object({"include_archived": {"type": "boolean"}}),
    },
    {
        "name": "list_agents",
        "description": (
            "List persistent global agent identities independently of reusable room seats."
        ),
        "inputSchema": _object(
            {"include_disabled_routes": {"type": "boolean"}}
        ),
    },
    {
        "name": "join_room",
        "description": "Add an agent session seat to a room; the global agent remains reusable.",
        "inputSchema": _object(
            {"room_id": STRING, "agent_id": STRING, "route_profile_id": STRING},
            ["room_id"],
        ),
    },
    {
        "name": "leave_room",
        "description": "Stop an agent receiving new room messages while preserving history.",
        "inputSchema": _object(
            {"room_id": STRING, "agent_id": STRING}, ["room_id"]
        ),
    },
    {
        "name": "room_members",
        "description": "List active or historical room seats and their requested routes.",
        "inputSchema": _object(
            {"room_id": STRING, "include_inactive": {"type": "boolean"}},
            ["room_id"],
        ),
    },
    {
        "name": "record_memory",
        "description": (
            "Record an explicit provider-neutral memory summary with SHA-bound sources. "
            "Private memory stays owner-only inside one room, room memory stays in one room, and only "
            "human-operator may publish project memory. Never submit hidden reasoning."
        ),
        "inputSchema": _object(
            {
                "visibility": {
                    "type": "string",
                    "enum": ["private", "room", "project"],
                },
                "room_id": STRING,
                "title": STRING,
                "body": STRING,
                "source_message_id": STRING,
                "artifact_paths": STRING_ARRAY,
                "parent_memory_id": STRING,
            },
            ["visibility", "title", "body"],
        ),
    },
    {
        "name": "list_memories",
        "description": (
            "List only the project, selected-room, and owner-private memories visible "
            "to this agent; room context is never merged implicitly."
        ),
        "inputSchema": _object(
            {
                "visibility": {
                    "type": "string",
                    "enum": ["private", "room", "project"],
                },
                "room_id": STRING,
                "query": STRING,
                "include_revoked": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    },
    {
        "name": "read_memory",
        "description": "Read one memory only when its visibility policy grants access.",
        "inputSchema": _object({"memory_id": STRING}, ["memory_id"]),
    },
    {
        "name": "revoke_memory",
        "description": (
            "Append a revocation receipt while retaining the original memory and audit history."
        ),
        "inputSchema": _object(
            {"memory_id": STRING, "reason": STRING},
            ["memory_id", "reason"],
        ),
    },
    {
        "name": "upsert_route_profile",
        "description": (
            "Register an immutable, auditable agent/provider/model/reasoning route. "
            "Only the human operator or that route's agent may register it; changing "
            "a selection requires a new route_id. A profile is a routing request, "
            "not proof that a provider honored it."
        ),
        "inputSchema": _object(
            {
                "route_id": STRING,
                "agent_id": STRING,
                "client_name": STRING,
                "provider_id": STRING,
                "model_id": STRING,
                "response_model_id": STRING,
                "reasoning_mode": STRING,
                "route_class": {
                    "type": "string",
                    "enum": ["official", "relay", "local"],
                },
                "enabled": {"type": "boolean"},
            },
            ["route_id", "agent_id"],
        ),
    },
    {
        "name": "list_route_profiles",
        "description": "List saved model routes for the human operator or an agent.",
        "inputSchema": _object(
            {"agent_id": STRING, "enabled_only": {"type": "boolean"}}
        ),
    },
    {
        "name": "upsert_provider_connection",
        "description": (
            "Register redacted provider metadata after the local control room stores "
            "the secret outside MCP. Only the human operator may call this tool, and "
            "it never accepts a URL or API key."
        ),
        "inputSchema": _object(
            {
                "connection_id": STRING,
                "display_name": STRING,
                "route_class": {
                    "type": "string",
                    "enum": ["official", "relay", "local"],
                },
                "provider_id": STRING,
                "secret_backend": {
                    "type": "string",
                    "enum": ["windows-credential-manager", "cc-switch"],
                },
                "credential_target": STRING,
                "endpoint_sha256": STRING,
                "credential_fingerprint_sha256": STRING,
                "descriptor_schema": STRING,
                "credential_version_sha256": STRING,
                "enabled": {"type": "boolean"},
            },
            [
                "connection_id",
                "display_name",
                "route_class",
                "provider_id",
                "secret_backend",
                "credential_target",
                "endpoint_sha256",
                "credential_fingerprint_sha256",
                "descriptor_schema",
                "credential_version_sha256",
            ],
        ),
    },
    {
        "name": "list_provider_connections",
        "description": "List redacted provider connections; secrets and endpoints are never returned.",
        "inputSchema": _object({"enabled_only": {"type": "boolean"}}),
    },
    {
        "name": "poll_messages",
        "description": "Poll this consumer's ordered mailbox using its durable cursor.",
        "inputSchema": _object(
            {
                "room_id": STRING,
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
        "name": "claim_message_dispatch",
        "description": (
            "Claim one addressed message with a crash-recoverable lease. "
            "The lease token is returned once and is never written to the audit log."
        ),
        "inputSchema": _object(
            {
                "message_id": STRING,
                "room_id": STRING,
                "route_profile_id": STRING,
                "require_route": {"type": "boolean"},
                "lease_seconds": {"type": "integer", "minimum": 30, "maximum": 86400},
                "max_attempts": {"type": "integer", "minimum": 1, "maximum": 100},
            }
        ),
    },
    {
        "name": "complete_message_dispatch",
        "description": (
            "Atomically write exactly one reply, acknowledge its source message, "
            "and bind the sanitized inference receipt."
        ),
        "inputSchema": _object(
            {
                "message_id": STRING,
                "lease_token": STRING,
                "subject": STRING,
                "body": STRING,
                "inference_receipt_sha256": STRING,
            },
            ["message_id", "lease_token", "body", "inference_receipt_sha256"],
        ),
    },
    {
        "name": "renew_message_dispatch",
        "description": (
            "Extend this runtime session's active message-dispatch lease during "
            "long inference. The lease token is verified and never persisted raw."
        ),
        "inputSchema": _object(
            {
                "message_id": STRING,
                "lease_token": STRING,
                "lease_seconds": {
                    "type": "integer",
                    "minimum": 30,
                    "maximum": 86400,
                },
            },
            ["message_id", "lease_token"],
        ),
    },
    {
        "name": "fail_message_dispatch",
        "description": (
            "Release a message lease with a typed, non-secret error code and an "
            "explicit retry decision."
        ),
        "inputSchema": _object(
            {
                "message_id": STRING,
                "lease_token": STRING,
                "error_code": STRING,
                "retryable": {"type": "boolean"},
            },
            ["message_id", "lease_token", "error_code"],
        ),
    },
    {
        "name": "list_message_dispatches",
        "description": "List sanitized durable message-dispatch states without lease secrets.",
        "inputSchema": _object(
            {
                "status": {
                    "type": "string",
                    "enum": ["claimed", "retryable", "failed", "completed"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
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
        "description": (
            "Queue a manual source-bound governance review for another agent. "
            "This does not invoke a model route; use post_room_message for an "
            "automatic room response or bounded discussion."
        ),
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
                "before_hashes": HASH_MAP,
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
    "list_message_dispatches",
    "list_route_profiles",
    "list_provider_connections",
    "list_rooms",
    "list_agents",
    "room_members",
    "get_room_automation",
    "list_memories",
    "read_memory",
    "workboard",
    "poll_reviews",
    "review_summary",
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
    "send_room_fanout": "send_room_fanout",
    "post_room_message": "post_room_message",
    "get_room_automation": "get_room_automation",
    "set_room_automation": "set_room_automation",
    "control_discussion": "control_discussion",
    "advance_discussions": "advance_discussions",
    "reconcile_message_dispatches": "reconcile_message_dispatches",
    "create_room": "create_room",
    "list_rooms": "list_rooms",
    "list_agents": "list_agents",
    "join_room": "join_room",
    "leave_room": "leave_room",
    "room_members": "room_members",
    "record_memory": "record_memory",
    "list_memories": "list_memories",
    "read_memory": "read_memory",
    "revoke_memory": "revoke_memory",
    "poll_messages": "poll_messages",
    "ack_message": "ack_message",
    "claim_message_dispatch": "claim_message_dispatch",
    "complete_message_dispatch": "complete_message_dispatch",
    "renew_message_dispatch": "renew_message_dispatch",
    "fail_message_dispatch": "fail_message_dispatch",
    "list_message_dispatches": "list_message_dispatches",
    "upsert_route_profile": "upsert_route_profile",
    "list_route_profiles": "list_route_profiles",
    "upsert_provider_connection": "upsert_provider_connection",
    "list_provider_connections": "list_provider_connections",
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


def _allowed_tool_schemas(
    allowed_tools: AbstractSet[str] | None,
    denied_tools: AbstractSet[str] = frozenset(),
) -> list[dict[str, Any]]:
    return [
        tool
        for tool in TOOL_SCHEMAS
        if tool["name"] not in denied_tools
        and (allowed_tools is None or tool["name"] in allowed_tools)
    ]


def _validate_schema_value(value: Any, schema: dict[str, Any]) -> bool:
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        return any(
            isinstance(alternative, dict)
            and _validate_schema_value(value, alternative)
            for alternative in alternatives
        )
    expected = schema.get("type")
    if expected == "string":
        valid = isinstance(value, str)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "array":
        valid = isinstance(value, list) and all(
            _validate_schema_value(item, schema.get("items", {})) for item in value
        )
    elif expected == "object":
        valid = isinstance(value, dict)
    elif expected == "null":
        valid = value is None
    else:
        valid = True
    if not valid:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if expected == "string" and "pattern" in schema:
        if re.fullmatch(str(schema["pattern"]), value) is None:
            return False
    if expected == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        required = schema.get("required", [])
        if not isinstance(required, list):
            return False
        if any(not isinstance(key, str) or key not in value for key in required):
            return False
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            property_schema = properties.get(key)
            if isinstance(property_schema, dict):
                if not _validate_schema_value(item, property_schema):
                    return False
                continue
            if additional is False:
                return False
            if isinstance(additional, dict) and not _validate_schema_value(
                item, additional
            ):
                return False
    if expected == "integer":
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    return True


def _validate_tool_arguments(name: str, arguments: dict[str, Any]) -> str | None:
    schema = next(
        (tool["inputSchema"] for tool in TOOL_SCHEMAS if tool["name"] == name),
        None,
    )
    if schema is None:
        return "unknown tool schema"
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False and any(
        key not in properties for key in arguments
    ):
        return "tool arguments contain unsupported fields"
    if any(key not in arguments for key in schema.get("required", [])):
        return "tool arguments are missing required fields"
    if any(
        not _validate_schema_value(value, properties[key])
        for key, value in arguments.items()
        if key in properties
    ):
        return "tool arguments do not match the declared schema"
    return None


def handle_request(
    bridge: Bridge,
    request: dict[str, Any],
    allowed_tools: AbstractSet[str] | None = None,
    denied_tools: AbstractSet[str] = frozenset(),
) -> dict[str, Any] | None:
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
                    "PeerBridge never applies patches. Manual governance reviews do not "
                    "invoke models; root room posts may invoke only explicitly routed Seats "
                    "through the optional local supervisor, and replies never re-fan-out."
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
                    "messages, reviews and proof. PeerBridge never applies patches. Manual "
                    "reviews remain poll-based; optional room automation invokes only "
                    "explicitly routed Seats and prevents reply cascades."
                ),
                "ttlMs": 300_000,
                "cacheScope": "public",
            },
        )
    if method == "tools/list":
        result: dict[str, Any] = {
            "tools": _allowed_tool_schemas(allowed_tools, denied_tools)
        }
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
        if allowed_tools is not None and name not in allowed_tools:
            return error_response(request_id, -32602, f"Tool is not allowed: {name}")
        if name in denied_tools:
            return error_response(request_id, -32602, f"Tool is not allowed: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return error_response(request_id, -32602, "tool arguments must be an object")
        validation_error = _validate_tool_arguments(name, arguments)
        if validation_error:
            if validation_error == "tool arguments contain unsupported fields":
                return error_response(request_id, -32602, validation_error)
            return content_response(
                request_id,
                {"error": validation_error, "tool": name},
                modern=modern,
                is_error=True,
            )
        # Interactive clients poll read-only tools frequently. Recording a pair
        # of durable audit rows for every poll does not prove a state change and
        # previously allowed idle UIs to grow the event ledger without bound.
        # Restricted model runs pass an explicit allow-list; keep their read-only
        # calls fully audited because provider receipts bind those exact events.
        audit_tool_call = name not in READ_ONLY_TOOLS or allowed_tools is not None
        if audit_tool_call:
            with bridge._connect() as connection:
                bridge._event(
                    connection,
                    "tool.called",
                    {"tool": name, "arguments_sha256": stable_sha256(arguments)},
                )
        try:
            result = dispatch(bridge, str(name), arguments)
            if audit_tool_call:
                with bridge._connect() as connection:
                    bridge._event(
                        connection,
                        "tool.returned",
                        {"tool": name, "result_sha256": stable_sha256(result)},
                    )
            return content_response(request_id, result, modern=modern)
        except (BridgeError, ValueError, TypeError) as exc:
            if audit_tool_call:
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
            if audit_tool_call:
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


def serve(
    bridge: Bridge,
    allowed_tools: AbstractSet[str] | None = None,
    denied_tools: AbstractSet[str] = frozenset(),
) -> int:
    if allowed_tools is not None:
        unknown = sorted(set(allowed_tools) - HANDLERS.keys())
        if unknown:
            raise ValueError(f"unknown allowed tools: {', '.join(unknown)}")
    unknown_denied = sorted(set(denied_tools) - HANDLERS.keys())
    if unknown_denied:
        raise ValueError(f"unknown denied tools: {', '.join(unknown_denied)}")
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
                result = handle_request(
                    bridge,
                    request,
                    allowed_tools,
                    denied_tools,
                )
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
