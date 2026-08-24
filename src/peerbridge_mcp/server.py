"""Dependency-free MCP stdio server exposing the PeerBridge tools."""

from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import AbstractSet, Any, Callable

from . import __version__
from . import authorized_sessions as authorized_sessions_module
from .agent_identity import (
    AgentIdentityCapability,
    AgentIdentityError,
    verify_agent_identity_capability,
    verify_agent_identity_route_binding,
)
from .authorized_sessions import AuthorizedSessionError, AuthorizedSessionRegistry
from .bridge import Bridge, BridgeError, stable_sha256
from .execution_governance import (
    ExecutionGovernance,
    GovernanceError,
)
from .operation_queue import DurableOperationQueue, OperationQueueError
from .proof_bundle import (
    PROOF_OUTPUT_ROOT,
    ProofBundleError,
    create_proof_bundle,
    verify_proof_bundle,
)
from .release_gate import ReleaseGateError, ReleaseGateService
from .secret_scan import contains_secret
from .protocol import (
    LEGACY_PROTOCOLS,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOLS,
    content_response,
    direct_response,
    error_response,
)
from .trust_timeline import TrustTimeline, TrustTimelineError


SERVER_NAME = "peerbridge-mcp"
MAX_STDIO_REQUEST_BYTES = 1024 * 1024
MAX_STDIO_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_STDIO_CALLS_PER_MINUTE = 600
MAX_STDIO_CALLS_PER_SESSION = 10_000
GENERIC_TRUST_STAGES = (
    "claim",
    "execution",
    "test",
    "proof",
    "review",
    "decision",
)


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


STRING = {"type": "string", "maxLength": MAX_STDIO_REQUEST_BYTES}
IDEMPOTENCY_KEY = {
    "type": "string",
    "pattern": "^[A-Za-z0-9_.:-]{1,200}$",
}
STRING_ARRAY = {"type": "array", "items": STRING, "maxItems": 100}
NUMBER = {"type": "number"}
SHA256_OR_NULL = {
    "anyOf": [
        {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        {"type": "null"},
    ]
}
HASH_MAP = {
    "type": "object",
    "additionalProperties": SHA256_OR_NULL,
    "maxProperties": 100,
}
NULLABLE_NONNEGATIVE_INTEGER = {
    "anyOf": [
        {"type": "integer", "minimum": 0},
        {"type": "null"},
    ]
}
FIELD_REPORTED_CALLS_SCHEMA = _object(
    {
        field: {"type": "integer", "minimum": 0}
        for field in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        )
    },
    [
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    ],
)
INFERENCE_USAGE_SCHEMA = _object(
    {
        "schema": {"type": "string", "enum": ["peerbridge.inference-usage.v1"]},
        "status": {
            "type": "string",
            "enum": ["reported", "partial", "unavailable"],
        },
        "source": STRING,
        "input_tokens": NULLABLE_NONNEGATIVE_INTEGER,
        "output_tokens": NULLABLE_NONNEGATIVE_INTEGER,
        "total_tokens": NULLABLE_NONNEGATIVE_INTEGER,
        "cached_input_tokens": NULLABLE_NONNEGATIVE_INTEGER,
        "reasoning_tokens": NULLABLE_NONNEGATIVE_INTEGER,
        "field_reported_calls": FIELD_REPORTED_CALLS_SCHEMA,
        "reported_calls": {"type": "integer", "minimum": 0},
        "total_calls": {"type": "integer", "minimum": 1},
        "total_tokens_derived": {"type": "boolean"},
    },
    [
        "schema",
        "status",
        "source",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "reported_calls",
        "total_calls",
        "total_tokens_derived",
    ],
)

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
                "idempotency_key": IDEMPOTENCY_KEY,
            },
            ["recipient", "task_id", "subject", "body", "idempotency_key"],
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
                "idempotency_key": IDEMPOTENCY_KEY,
            },
            ["room_id", "task_id", "subject", "body", "idempotency_key"],
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
                "idempotency_key": IDEMPOTENCY_KEY,
            },
            ["room_id", "task_id", "subject", "body", "idempotency_key"],
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
            "whose seat, immutable route, runtime match, or retry budget can no longer run."
        ),
        "inputSchema": _object(
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "route_runtime_observations": {
                    "type": "array",
                    "items": _object(
                        {
                            "message_id": STRING,
                            "route_request_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                            "match_count": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 1000,
                            },
                        },
                        ["message_id", "route_request_sha256", "match_count"],
                    ),
                },
            }
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
            {
                "room_id": STRING,
                "agent_id": STRING,
                "route_profile_id": STRING,
                "role_id": {
                    "type": "string",
                    "enum": [
                        "equal-participant",
                        "researcher",
                        "implementer",
                        "reviewer",
                        "custom",
                    ],
                },
                "role_label": STRING,
            },
            ["room_id"],
        ),
    },
    {
        "name": "set_room_member_role",
        "description": (
            "Assign a division-of-work label to one exact room membership without "
            "changing Agent authority, permissions or room session identity."
        ),
        "inputSchema": _object(
            {
                "room_id": STRING,
                "agent_id": STRING,
                "role_id": {
                    "type": "string",
                    "enum": [
                        "equal-participant",
                        "researcher",
                        "implementer",
                        "reviewer",
                        "custom",
                    ],
                },
                "role_label": STRING,
            },
            ["room_id", "agent_id", "role_id"],
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
                "record_type": {
                    "type": "string",
                    "enum": [
                        "FACT",
                        "DECISION",
                        "CONSTRAINT",
                        "PREFERENCE",
                        "DEPRECATED",
                    ],
                },
                "authority_id": STRING,
                "room_id": STRING,
                "title": STRING,
                "body": STRING,
                "source_message_id": STRING,
                "artifact_paths": STRING_ARRAY,
                "parent_memory_id": STRING,
                "supersedes_memory_id": STRING,
                "applicability": STRING_ARRAY,
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
                "inference_timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                },
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
                    "enum": [
                        "windows-credential-manager",
                        "cc-switch",
                        "native-acp",
                    ],
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
                "inference_usage": INFERENCE_USAGE_SCHEMA,
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

ALPHA52_TOOL_SCHEMAS = [
    {
        "name": "connect_observable_session",
        "description": (
            "Bind one external desktop or terminal conversation to its calling MCP "
            "session. This authorizes only explicitly published observable events; it "
            "does not expose private history, hidden reasoning, or PeerBridge input control."
        ),
        "inputSchema": _object(
            {
                "source_type": {
                    "type": "string",
                    "enum": ["authorized-desktop", "authorized-terminal"],
                },
                "source_session_id": STRING,
                "source_conversation_id": STRING,
                "adapter_id": STRING,
                "display_name": STRING,
                "client_name": STRING,
                "client_version": STRING,
                "room_id": STRING,
                "requested_route": STRING,
                "observed_route": STRING,
                "observed_route_source": STRING,
                "model_id": STRING,
                "model_source": STRING,
                "supports_events": {"type": "boolean"},
                "state": {
                    "type": "string",
                    "enum": [
                        "detected",
                        "running",
                        "waiting",
                        "completed",
                        "stopped",
                        "failed",
                    ],
                },
            },
            [
                "source_type",
                "source_session_id",
                "source_conversation_id",
                "adapter_id",
                "display_name",
                "client_name",
                "supports_events",
            ],
        ),
    },
    {
        "name": "publish_observable_session_event",
        "description": (
            "Publish one bounded, redacted terminal output, tool event, explicit progress "
            "summary, or final answer from the calling session's authorized adapter."
        ),
        "inputSchema": _object(
            {
                "source_type": {
                    "type": "string",
                    "enum": ["authorized-desktop", "authorized-terminal"],
                },
                "source_session_id": STRING,
                "event_id": STRING,
                "stream": {
                    "type": "string",
                    "enum": ["system", "stdout", "stderr"],
                },
                "kind": {
                    "type": "string",
                    "enum": ["system", "terminal", "activity", "answer", "error"],
                },
                "text": STRING,
                "summary": STRING,
                "state": {
                    "type": "string",
                    "enum": [
                        "detected",
                        "running",
                        "waiting",
                        "completed",
                        "stopped",
                        "failed",
                    ],
                },
            },
            [
                "source_type",
                "source_session_id",
                "event_id",
                "stream",
                "kind",
                "text",
            ],
        ),
    },
    {
        "name": "close_observable_session",
        "description": (
            "Mark the calling adapter's exact external session terminal without closing "
            "or controlling the original desktop or terminal window."
        ),
        "inputSchema": _object(
            {
                "source_type": {
                    "type": "string",
                    "enum": ["authorized-desktop", "authorized-terminal"],
                },
                "source_session_id": STRING,
                "state": {
                    "type": "string",
                    "enum": ["completed", "stopped", "failed"],
                },
            },
            ["source_type", "source_session_id", "state"],
        ),
    },
    {
        "name": "list_own_observable_sessions",
        "description": (
            "List only external observable sessions owned by the calling Agent identity; "
            "other Agents' sessions remain Control Room-only."
        ),
        "inputSchema": _object(
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 64},
                "after_sequences": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "integer",
                        "minimum": 0,
                    },
                },
            }
        ),
    },
    {
        "name": "brief_task",
        "description": (
            "Create a SHA-bound task briefing from only visible, applicable, approved "
            "memory records, excluding superseded and deprecated records."
        ),
        "inputSchema": _object(
            {
                "task_id": STRING,
                "room_id": STRING,
                "applicability": STRING_ARRAY,
            },
            ["task_id"],
        ),
    },
    {
        "name": "record_decision_conflict",
        "description": (
            "Record a source-bound decision conflict as a review finding only; it never "
            "silently blocks or approves a change."
        ),
        "inputSchema": _object(
            {
                "task_id": STRING,
                "briefing_id": STRING,
                "memory_ids": STRING_ARRAY,
                "summary": STRING,
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                },
            },
            ["task_id", "briefing_id", "memory_ids", "summary"],
        ),
    },
    {
        "name": "list_workflow_templates",
        "description": "List the bounded local workflow templates shipped by PeerBridge.",
        "inputSchema": _object({}),
    },
    {
        "name": "enqueue_workflow",
        "description": (
            "Queue one human-requested local workflow with a stable operation id, bounded "
            "attempts, timeout, resource serialization, and no automatic merge."
        ),
        "inputSchema": _object(
            {
                "operation_id": STRING,
                "workflow_id": STRING,
                "task_text": STRING,
                "working_directory": STRING,
                "resource_key": STRING,
                "permission_decision_id": STRING,
                "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 86400,
                },
                "not_before_epoch": NUMBER,
            },
            [
                "operation_id",
                "workflow_id",
                "task_text",
                "working_directory",
                "resource_key",
            ],
        ),
    },
    {
        "name": "list_operations",
        "description": "List durable workflow operations and their exact terminal state.",
        "inputSchema": _object(
            {
                "status": {
                    "type": "string",
                    "enum": [
                        "queued",
                        "running",
                        "retry",
                        "cancelling",
                        "succeeded",
                        "failed",
                        "cancelled",
                    ],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    },
    {
        "name": "bind_guided_discussion",
        "description": (
            "Permanently bind one queued guided workflow to the exact validated "
            "room discussion before any worker may claim it."
        ),
        "inputSchema": _object(
            {"operation_id": STRING, "discussion_id": STRING},
            ["operation_id", "discussion_id"],
        ),
    },
    {
        "name": "cancel_operation",
        "description": (
            "Request operator-visible cancellation; running workers must acknowledge it, "
            "while queued work reaches one terminal outcome immediately."
        ),
        "inputSchema": _object(
            {"operation_id": STRING, "reason": STRING}, ["operation_id"]
        ),
    },
    {
        "name": "reconcile_operations",
        "description": "Reconcile expired worker leases and hard attempt deadlines.",
        "inputSchema": _object({}),
    },
    {
        "name": "save_workflow_schedule",
        "description": (
            "Create one opt-in local schedule. Scheduled work keeps the normal queue, "
            "permission, evidence, timeout, and stop controls."
        ),
        "inputSchema": _object(
            {
                "schedule_id": STRING,
                "workflow_id": STRING,
                "task_text": STRING,
                "working_directory": STRING,
                "resource_key": STRING,
                "permission_decision_id": STRING,
                "interval_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 2678400,
                },
                "next_run_epoch": NUMBER,
                "enabled": {"type": "boolean"},
            },
            [
                "schedule_id",
                "workflow_id",
                "task_text",
                "working_directory",
                "resource_key",
                "interval_seconds",
                "next_run_epoch",
                "enabled",
            ],
        ),
    },
    {
        "name": "list_workflow_schedules",
        "description": "List SHA-verified local workflow schedules without materializing work.",
        "inputSchema": _object(
            {
                "enabled": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
    },
    {
        "name": "set_workflow_schedule_enabled",
        "description": "Enable or disable one schedule without deleting its history.",
        "inputSchema": _object(
            {"schedule_id": STRING, "enabled": {"type": "boolean"}},
            ["schedule_id", "enabled"],
        ),
    },
    {
        "name": "materialize_workflow_schedules",
        "description": "Atomically materialize due enabled schedules into the durable queue.",
        "inputSchema": _object(
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}
        ),
    },
    {
        "name": "release_gate_status",
        "description": (
            "Recompute whether one Release Gate is current, successful, and explicitly "
            "approved by the human operator without publishing anything."
        ),
        "inputSchema": _object(
            {"fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}
        ),
    },
    {
        "name": "register_capability",
        "description": "Register one immutable version of a Skill or MCP tool capability.",
        "inputSchema": _object(
            {
                "capability_id": STRING,
                "registry_version": STRING,
                "kind": {"type": "string", "enum": ["skill", "mcp-tool"]},
                "display_name": STRING,
                "source_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "sensitivity": {
                    "type": "string",
                    "enum": ["read", "write", "sensitive"],
                },
                "enabled": {"type": "boolean"},
            },
            [
                "capability_id",
                "registry_version",
                "kind",
                "display_name",
                "source_sha256",
                "sensitivity",
            ],
        ),
    },
    {
        "name": "grant_capability",
        "description": "Append one human allow or deny decision for an Agent or Room capability.",
        "inputSchema": _object(
            {
                "principal_type": {"type": "string", "enum": ["agent", "room"]},
                "principal_id": STRING,
                "capability_id": STRING,
                "registry_version": STRING,
                "decision": {"type": "string", "enum": ["allow", "deny"]},
                "reason": STRING,
            },
            [
                "principal_type",
                "principal_id",
                "capability_id",
                "registry_version",
                "decision",
                "reason",
            ],
        ),
    },
    {
        "name": "effective_capabilities",
        "description": "List the latest enabled capabilities allowed for one Agent or Room.",
        "inputSchema": _object(
            {
                "principal_type": {"type": "string", "enum": ["agent", "room"]},
                "principal_id": STRING,
            },
            ["principal_type", "principal_id"],
        ),
    },
    {
        "name": "decide_permission",
        "description": (
            "Append one exact, bounded, expiring human allow or deny decision for a "
            "sensitive local action."
        ),
        "inputSchema": _object(
            {
                "decision_id": STRING,
                "task_id": STRING,
                "agent_id": STRING,
                "action": STRING,
                "resource_key": STRING,
                "decision": {"type": "string", "enum": ["allow", "deny"]},
                "reason": STRING,
                "expires_epoch": NUMBER,
            },
            [
                "decision_id",
                "task_id",
                "agent_id",
                "action",
                "resource_key",
                "decision",
                "reason",
                "expires_epoch",
            ],
        ),
    },
    {
        "name": "create_execution_worktree",
        "description": (
            "Consume an exact human permission and create a detached isolated Git worktree; "
            "this never applies or merges a patch."
        ),
        "inputSchema": _object(
            {
                "binding_id": STRING,
                "task_id": STRING,
                "agent_id": STRING,
                "permission_decision_id": STRING,
                "repository": STRING,
                "base_commit": STRING,
            },
            [
                "binding_id",
                "task_id",
                "agent_id",
                "permission_decision_id",
                "repository",
            ],
        ),
    },
    {
        "name": "seal_execution",
        "description": "Seal one worktree's exact commit and binary diff hash for review.",
        "inputSchema": _object({"binding_id": STRING}, ["binding_id"]),
    },
    {
        "name": "verify_execution_source",
        "description": "Rehash a governed worktree and report whether its sealed source is stale.",
        "inputSchema": _object({"binding_id": STRING}, ["binding_id"]),
    },
    {
        "name": "record_trust",
        "description": (
            "Append a visible Trust Timeline record with exact relative source hashes. "
            "Captured summaries are not hidden chain-of-thought."
        ),
        "inputSchema": _object(
            {
                "record_id": STRING,
                "task_id": STRING,
                "stage": {
                    "type": "string",
                    "enum": list(GENERIC_TRUST_STAGES),
                },
                "statement": STRING,
                "artifact_paths": STRING_ARRAY,
                "related_record_ids": STRING_ARRAY,
            },
            ["record_id", "task_id", "stage", "statement"],
        ),
    },
    {
        "name": "trust_timeline",
        "description": "Read one task's Trust Timeline with live stale-evidence checks.",
        "inputSchema": _object({"task_id": STRING}, ["task_id"]),
    },
    {
        "name": "record_trust_disagreement",
        "description": "Record a substantive disagreement bound to at least two fresh evidence records.",
        "inputSchema": _object(
            {
                "task_id": STRING,
                "statement": STRING,
                "evidence_record_ids": STRING_ARRAY,
            },
            ["task_id", "statement", "evidence_record_ids"],
        ),
    },
    {
        "name": "recheck_trust_record",
        "description": "Append one bounded recheck against the exact source of an earlier record.",
        "inputSchema": _object(
            {"record_id": STRING, "statement": STRING},
            ["record_id", "statement"],
        ),
    },
    {
        "name": "complete_trust_timeline",
        "description": (
            "Record completion only when fresh test, proof, and review records bind one "
            "exact source state; include the human decision for Proof Bundle export."
        ),
        "inputSchema": _object(
            {
                "task_id": STRING,
                "statement": STRING,
                "evidence_record_ids": STRING_ARRAY,
            },
            ["task_id", "statement", "evidence_record_ids"],
        ),
    },
    {
        "name": "export_proof_bundle",
        "description": (
            "Create one sanitized portable JSON and Markdown Proof Bundle with relative "
            "evidence and manifest. Output is create-only and requires a trusted "
            "installed PeerBridge verifier; unsigned results are structural-only."
        ),
        "inputSchema": _object(
            {"task_id": STRING, "output_path": STRING},
            ["task_id", "output_path"],
        ),
    },
    {
        "name": "verify_proof_bundle",
        "description": "Verify a portable Proof Bundle without writing or following unsafe paths.",
        "inputSchema": _object({"bundle_path": STRING}, ["bundle_path"]),
    },
]

TOOL_SCHEMAS.extend(ALPHA52_TOOL_SCHEMAS)

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
    "effective_capabilities",
    "list_operations",
    "list_workflow_schedules",
    "list_workflow_templates",
    "release_gate_status",
    "list_own_observable_sessions",
    "trust_timeline",
    "verify_execution_source",
    "verify_audit_chain",
    "verify_proof_bundle",
}
LEGACY_CAPABILITY_TOOLS = frozenset(READ_ONLY_TOOLS) | {"hash_artifact"}
_TOOL_INPUT_SCHEMAS = {
    str(tool["name"]): tool["inputSchema"] for tool in TOOL_SCHEMAS
}
_AUDIT_REDACTED_ARGUMENT_KEYS = frozenset({"lease_token"})
_CREDENTIAL_REJECTION_METADATA = {"reason": "credential-bearing-arguments"}

# These tools allocate message, fanout, and dispatch identifiers. Persist their
# completed results before the trailing tool audit so a retried JSON-RPC request
# cannot allocate a second delivery after the first mutation committed.
IDEMPOTENT_MESSAGE_TOOLS = frozenset(
    {"send_message", "send_room_fanout", "post_room_message"}
)

for _tool in TOOL_SCHEMAS:
    _tool["annotations"] = {
        "readOnlyHint": _tool["name"] in READ_ONLY_TOOLS,
        "destructiveHint": False,
        "idempotentHint": _tool["name"] in READ_ONLY_TOOLS
        ,
        "openWorldHint": False,
    }


def _project_directory(bridge: Bridge, value: Any) -> str:
    normalized = bridge._normalize_path(value)
    if bridge._is_within_protected(normalized):
        raise BridgeError("working directory is protected")
    resolved = bridge.root if normalized == "." else bridge.root / normalized
    if not resolved.is_dir():
        raise BridgeError("working directory does not exist inside the project")
    return normalized


def _proof_bundle_directory(
    bridge: Bridge, value: Any, *, must_exist: bool
) -> Path:
    normalized = bridge._normalize_path(value)
    allowed_artifact_path = normalized == PROOF_OUTPUT_ROOT or normalized.startswith(
        PROOF_OUTPUT_ROOT + "/"
    )
    if bridge._is_protected(normalized) and not allowed_artifact_path:
        raise BridgeError("Proof Bundle path is protected")
    resolved = bridge.root if normalized == "." else bridge.root / normalized
    if must_exist and not resolved.is_dir():
        raise BridgeError("Proof Bundle directory does not exist")
    return resolved


def _list_workflow_templates(
    _bridge: Bridge, _args: dict[str, Any]
) -> dict[str, Any]:
    templates = list(DurableOperationQueue.templates())
    return {"templates": templates, "count": len(templates)}


def _enqueue_workflow(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    return DurableOperationQueue(bridge).enqueue(
        operation_id=str(args["operation_id"]),
        workflow_id=str(args["workflow_id"]),
        task_text=str(args["task_text"]),
        working_directory=_project_directory(bridge, args["working_directory"]),
        resource_key=str(args["resource_key"]),
        requested_by=bridge.agent_id,
        permission_decision_id=(
            str(args["permission_decision_id"])
            if args.get("permission_decision_id")
            else None
        ),
        max_attempts=int(args.get("max_attempts", 3)),
        timeout_seconds=int(args.get("timeout_seconds", 1_800)),
        not_before_epoch=(
            float(args["not_before_epoch"])
            if args.get("not_before_epoch") is not None
            else None
        ),
    )


def _list_operations(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    operations = DurableOperationQueue(bridge).list_operations(
        status=str(args["status"]) if args.get("status") else None,
        limit=int(args.get("limit", 100)),
    )
    return {"operations": operations, "count": len(operations)}


def _bind_guided_discussion(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return DurableOperationQueue(bridge).bind_guided_discussion(
        str(args["operation_id"]), str(args["discussion_id"])
    )


def _cancel_operation(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    return DurableOperationQueue(bridge).request_cancel(
        str(args["operation_id"]),
        requested_by=bridge.agent_id,
        reason=str(args.get("reason") or "Cancelled by the operator."),
    )


def _reconcile_operations(
    bridge: Bridge, _args: dict[str, Any]
) -> dict[str, Any]:
    operations = DurableOperationQueue(bridge).reconcile()
    return {"operations": operations, "count": len(operations)}


def _save_workflow_schedule(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return DurableOperationQueue(bridge).save_schedule(
        schedule_id=str(args["schedule_id"]),
        workflow_id=str(args["workflow_id"]),
        task_text=str(args["task_text"]),
        working_directory=_project_directory(bridge, args["working_directory"]),
        resource_key=str(args["resource_key"]),
        interval_seconds=int(args["interval_seconds"]),
        next_run_epoch=float(args["next_run_epoch"]),
        enabled=bool(args["enabled"]),
        requested_by=bridge.agent_id,
        permission_decision_id=(
            str(args["permission_decision_id"])
            if args.get("permission_decision_id")
            else None
        ),
    )


def _list_workflow_schedules(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    schedules = DurableOperationQueue(bridge).list_schedules(
        enabled=args.get("enabled"), limit=int(args.get("limit", 100))
    )
    return {"schedules": schedules, "count": len(schedules)}


def _set_workflow_schedule_enabled(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return DurableOperationQueue(bridge).set_schedule_enabled(
        str(args["schedule_id"]),
        enabled=bool(args["enabled"]),
        requested_by=bridge.agent_id,
    )


def _materialize_workflow_schedules(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    operations = DurableOperationQueue(bridge).materialize_due_schedules(
        limit=int(args.get("limit", 20))
    )
    return {"operations": operations, "count": len(operations)}


def _release_gate_status(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    return ReleaseGateService(bridge).status(
        str(args["fingerprint"]) if args.get("fingerprint") else None
    )


def _register_capability(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    return ExecutionGovernance(bridge).register_capability(
        capability_id=str(args["capability_id"]),
        registry_version=str(args["registry_version"]),
        kind=str(args["kind"]),
        display_name=str(args["display_name"]),
        source_sha256=str(args["source_sha256"]),
        sensitivity=str(args["sensitivity"]),
        enabled=bool(args.get("enabled", True)),
    )


def _grant_capability(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    return ExecutionGovernance(bridge).grant_capability(
        principal_type=str(args["principal_type"]),
        principal_id=str(args["principal_id"]),
        capability_id=str(args["capability_id"]),
        registry_version=str(args["registry_version"]),
        decision=str(args["decision"]),
        reason=str(args["reason"]),
    )


def _effective_capabilities(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    capabilities = ExecutionGovernance(bridge).effective_capabilities(
        principal_type=str(args["principal_type"]),
        principal_id=str(args["principal_id"]),
    )
    return {"capabilities": capabilities, "count": len(capabilities)}


def _decide_permission(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    return ExecutionGovernance(bridge).decide_permission(
        decision_id=str(args["decision_id"]),
        task_id=str(args["task_id"]),
        agent_id=str(args["agent_id"]),
        action=str(args["action"]),
        resource_key=str(args["resource_key"]),
        decision=str(args["decision"]),
        reason=str(args["reason"]),
        expires_epoch=float(args["expires_epoch"]),
    )


def _create_execution_worktree(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    repository_path = _project_directory(bridge, args["repository"])
    repository = (
        bridge.root if repository_path == "." else bridge.root / repository_path
    )
    return ExecutionGovernance(bridge).create_isolated_worktree(
        binding_id=str(args["binding_id"]),
        task_id=str(args["task_id"]),
        agent_id=str(args["agent_id"]),
        permission_decision_id=str(args["permission_decision_id"]),
        repository=repository,
        base_commit=str(args.get("base_commit") or "HEAD"),
    )


def _seal_execution(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    return ExecutionGovernance(bridge).seal_execution(str(args["binding_id"]))


def _verify_execution_source(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return ExecutionGovernance(bridge).verify_execution_source(
        str(args["binding_id"])
    )


def _record_trust(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    stage = str(args["stage"])
    if stage not in GENERIC_TRUST_STAGES:
        raise TrustTimelineError(
            "specialized trust stages require their dedicated MCP tool"
        )
    return TrustTimeline(bridge).record(
        record_id=str(args["record_id"]),
        task_id=str(args["task_id"]),
        stage=stage,
        statement=str(args["statement"]),
        artifact_paths=args.get("artifact_paths", []),
        related_record_ids=args.get("related_record_ids", []),
    )


def _trust_timeline(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    records = TrustTimeline(bridge).timeline(str(args["task_id"]))
    return {"records": records, "count": len(records)}


def _record_trust_disagreement(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return TrustTimeline(bridge).record_disagreement(
        task_id=str(args["task_id"]),
        statement=str(args["statement"]),
        evidence_record_ids=args["evidence_record_ids"],
    )


def _recheck_trust_record(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return TrustTimeline(bridge).recheck(
        str(args["record_id"]), statement=str(args["statement"])
    )


def _complete_trust_timeline(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return TrustTimeline(bridge).record_completion(
        task_id=str(args["task_id"]),
        statement=str(args["statement"]),
        evidence_record_ids=args["evidence_record_ids"],
    )


def _export_proof_bundle(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    output = _proof_bundle_directory(bridge, args["output_path"], must_exist=False)
    return create_proof_bundle(
        bridge, task_id=str(args["task_id"]), output_path=output
    )


def _verify_proof_bundle(bridge: Bridge, args: dict[str, Any]) -> dict[str, Any]:
    bundle = _proof_bundle_directory(bridge, args["bundle_path"], must_exist=True)
    return verify_proof_bundle(bundle)


def _connect_observable_session(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return AuthorizedSessionRegistry(bridge).connect(args)


def _publish_observable_session_event(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return AuthorizedSessionRegistry(bridge).publish_event(args)


def _close_observable_session(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    return AuthorizedSessionRegistry(bridge).close(args)


def _list_own_observable_sessions(
    bridge: Bridge, args: dict[str, Any]
) -> dict[str, Any]:
    after_sequences = args.get("after_sequences")
    if after_sequences is not None and not isinstance(after_sequences, dict):
        raise AuthorizedSessionError("after_sequences must be an object")
    sessions = AuthorizedSessionRegistry(bridge).list_owned(
        limit=int(args.get("limit", 64)),
        after_sequences=after_sequences,
    )
    return {"sessions": list(sessions), "count": len(sessions)}


def _transport_response_bytes(response: dict[str, Any]) -> int:
    serialized = json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
    return len(serialized.encode("utf-8"))


def _bounded_observable_sessions_response(
    request_id: Any,
    result: dict[str, Any],
    *,
    modern: bool,
) -> dict[str, Any]:
    max_bytes = max(1, int(authorized_sessions_module.MAX_AUTHORIZED_RESPONSE_BYTES))
    response = content_response(request_id, result, modern=modern)
    if _transport_response_bytes(response) <= max_bytes:
        return response

    sessions = result.get("sessions")
    if not isinstance(sessions, list):
        return error_response(
            request_id,
            -32603,
            "authorized session response has an invalid shape",
        )

    event_lists = [
        session.get("events", []) if isinstance(session, dict) else []
        for session in sessions
    ]
    if any(not isinstance(events, list) for events in event_lists):
        return error_response(
            request_id,
            -32603,
            "authorized session response has an invalid event list",
        )

    event_order = [
        session_index
        for event_index in range(max((len(events) for events in event_lists), default=0))
        for session_index, events in enumerate(event_lists)
        if event_index < len(events)
    ]

    def response_with_event_count(event_count: int) -> dict[str, Any]:
        keep_counts = [0] * len(sessions)
        for session_index in event_order[:event_count]:
            keep_counts[session_index] += 1
        bounded_sessions: list[dict[str, Any]] = []
        for session_index, session in enumerate(sessions):
            if not isinstance(session, dict):
                continue
            bounded_session = dict(session)
            bounded_session["events"] = event_lists[session_index][
                : keep_counts[session_index]
            ]
            bounded_sessions.append(bounded_session)
        bounded_result = {**result, "sessions": bounded_sessions}
        return content_response(request_id, bounded_result, modern=modern)

    empty_response = response_with_event_count(0)
    if _transport_response_bytes(empty_response) > max_bytes:
        overflow = error_response(
            request_id,
            -32603,
            "authorized session response metadata exceeds the UTF-8 byte limit",
        )
        if _transport_response_bytes(overflow) <= max_bytes:
            return overflow
        return error_response(None, -32603, "response byte limit exceeded")

    best = empty_response
    lower = 1
    upper = len(event_order)
    while lower <= upper:
        candidate_count = (lower + upper) // 2
        candidate = response_with_event_count(candidate_count)
        if _transport_response_bytes(candidate) <= max_bytes:
            best = candidate
            lower = candidate_count + 1
        else:
            upper = candidate_count - 1
    return best


SPECIAL_HANDLERS: dict[
    str, Callable[[Bridge, dict[str, Any]], dict[str, Any]]
] = {
    "connect_observable_session": _connect_observable_session,
    "publish_observable_session_event": _publish_observable_session_event,
    "close_observable_session": _close_observable_session,
    "list_own_observable_sessions": _list_own_observable_sessions,
    "list_workflow_templates": _list_workflow_templates,
    "enqueue_workflow": _enqueue_workflow,
    "bind_guided_discussion": _bind_guided_discussion,
    "list_operations": _list_operations,
    "cancel_operation": _cancel_operation,
    "reconcile_operations": _reconcile_operations,
    "save_workflow_schedule": _save_workflow_schedule,
    "list_workflow_schedules": _list_workflow_schedules,
    "set_workflow_schedule_enabled": _set_workflow_schedule_enabled,
    "materialize_workflow_schedules": _materialize_workflow_schedules,
    "release_gate_status": _release_gate_status,
    "register_capability": _register_capability,
    "grant_capability": _grant_capability,
    "effective_capabilities": _effective_capabilities,
    "decide_permission": _decide_permission,
    "create_execution_worktree": _create_execution_worktree,
    "seal_execution": _seal_execution,
    "verify_execution_source": _verify_execution_source,
    "record_trust": _record_trust,
    "trust_timeline": _trust_timeline,
    "record_trust_disagreement": _record_trust_disagreement,
    "recheck_trust_record": _recheck_trust_record,
    "complete_trust_timeline": _complete_trust_timeline,
    "export_proof_bundle": _export_proof_bundle,
    "verify_proof_bundle": _verify_proof_bundle,
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
    "set_room_member_role": "set_room_member_role",
    "leave_room": "leave_room",
    "room_members": "room_members",
    "record_memory": "record_memory",
    "brief_task": "brief_task",
    "record_decision_conflict": "record_decision_conflict",
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
HANDLERS.update({name: "__alpha52__" for name in SPECIAL_HANDLERS})


def dispatch(bridge: Bridge, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    special = SPECIAL_HANDLERS.get(name)
    if special is not None:
        try:
            return special(bridge, arguments)
        except (
            AuthorizedSessionError,
            GovernanceError,
            OperationQueueError,
            ProofBundleError,
            ReleaseGateError,
            TrustTimelineError,
            OSError,
        ) as exc:
            raise BridgeError(str(exc)) from exc
    method_name = HANDLERS.get(name)
    if method_name is None:
        raise BridgeError(f"unknown tool: {name}")
    method: Callable[[dict[str, Any]], dict[str, Any]] = getattr(bridge, method_name)
    return method(arguments)


def _mutation_call_sha256(
    bridge: Bridge,
    name: str,
    arguments: dict[str, Any],
) -> str:
    idempotency_key = str(arguments.get("idempotency_key") or "")
    return stable_sha256(
        {
            "scope": bridge.scope,
            "actor": bridge.agent_id,
            "tool": name,
            "idempotency_key": idempotency_key,
        }
    )


def _load_mutation_receipt(
    bridge: Bridge,
    call_sha256: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    with bridge._connect() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS mcp_mutation_receipts(
                   call_sha256 TEXT PRIMARY KEY,
                   scope TEXT NOT NULL,
                   actor TEXT NOT NULL,
                   session_id TEXT NOT NULL,
                   tool TEXT NOT NULL,
                   arguments_sha256 TEXT NOT NULL,
                   result_json TEXT NOT NULL,
                   result_sha256 TEXT NOT NULL,
                   created_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        row = connection.execute(
            """SELECT actor, tool, arguments_sha256, result_json, result_sha256
                 FROM mcp_mutation_receipts
                WHERE call_sha256=? AND scope=?""",
            (call_sha256, bridge.scope),
        ).fetchone()
    if row is None:
        return None
    if (
        str(row["actor"]) != bridge.agent_id
        or str(row["tool"]) != name
        or str(row["arguments_sha256"]) != stable_sha256(arguments)
    ):
        raise BridgeError("MCP idempotency key was reused with different arguments")
    result = json.loads(str(row["result_json"]))
    if not isinstance(result, dict) or stable_sha256(result) != row["result_sha256"]:
        raise sqlite3.DatabaseError("MCP mutation receipt integrity check failed")
    return result


def _audit_tool_result_fail_safe(
    bridge: Bridge, name: str, result: dict[str, Any]
) -> None:
    try:
        with bridge._connect() as connection:
            bridge._event(
                connection,
                "tool.returned",
                {"tool": name, "result_sha256": stable_sha256(result)},
            )
    except sqlite3.Error:
        # Dispatch already committed. Audit availability must not turn that
        # durable success into an internal error that invites a duplicate retry.
        return


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
    elif expected == "number":
        valid = (
            isinstance(value, int)
            and not isinstance(value, bool)
            or isinstance(value, float)
            and math.isfinite(value)
        )
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
    if expected == "string" and "maxLength" in schema:
        if len(value) > int(schema["maxLength"]):
            return False
    if expected == "array" and "maxItems" in schema:
        if len(value) > int(schema["maxItems"]):
            return False
    if expected == "object":
        if "maxProperties" in schema and len(value) > int(schema["maxProperties"]):
            return False
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
    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    return True


def _validate_tool_arguments(name: str, arguments: dict[str, Any]) -> str | None:
    schema = _TOOL_INPUT_SCHEMAS.get(name)
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


def _tool_arguments_contain_credentials(arguments: dict[str, Any]) -> bool:
    candidate = json.dumps(
        _audit_safe_arguments(arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return contains_secret(candidate)


def _audit_safe_arguments(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "token"
                if str(key) in _AUDIT_REDACTED_ARGUMENT_KEYS
                else _audit_safe_arguments(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_audit_safe_arguments(item) for item in value]
    return value


def _record_credential_rejection(bridge: Bridge) -> None:
    with bridge._connect() as connection:
        bridge._event(
            connection,
            "tool.rejected",
            dict(_CREDENTIAL_REJECTION_METADATA),
        )


def _room_binding_error(
    name: str, arguments: dict[str, Any], bound_room_id: str | None
) -> str | None:
    if bound_room_id is None:
        return None
    schema = _TOOL_INPUT_SCHEMAS.get(name, {})
    properties = schema.get("properties", {})
    if "room_id" in properties and arguments.get("room_id") != bound_room_id:
        return "Tool call must use the capability-bound room_id"
    return None


def handle_request(
    bridge: Bridge,
    request: dict[str, Any],
    allowed_tools: AbstractSet[str] | None = None,
    denied_tools: AbstractSet[str] = frozenset(),
    *,
    capability_validator: Callable[[], None] | None = None,
    bound_room_id: str | None = None,
) -> dict[str, Any] | None:
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
        if capability_validator is not None:
            try:
                capability_validator()
            except AgentIdentityError:
                return error_response(
                    request_id,
                    -32003,
                    "Agent identity capability is invalid or revoked; session fenced",
                )
        params = request.get("params", {})
        if not isinstance(params, dict):
            return error_response(request_id, -32602, "tool call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return error_response(request_id, -32602, "tool arguments must be an object")
        credential_rejected = _tool_arguments_contain_credentials(arguments)
        if credential_rejected:
            try:
                _record_credential_rejection(bridge)
            except sqlite3.Error:
                return error_response(
                    request_id, -32603, "internal bridge database error"
                )
        if not isinstance(name, str) or name not in HANDLERS:
            return error_response(request_id, -32602, f"Unknown tool: {name}")
        if allowed_tools is not None and name not in allowed_tools:
            return error_response(request_id, -32602, f"Tool is not allowed: {name}")
        if name in denied_tools:
            return error_response(request_id, -32602, f"Tool is not allowed: {name}")
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
        if credential_rejected:
            return content_response(
                request_id,
                {"error": "tool arguments contain credential material"},
                modern=modern,
                is_error=True,
            )
        room_binding_error = _room_binding_error(name, arguments, bound_room_id)
        if room_binding_error is not None:
            return content_response(
                request_id,
                {"error": room_binding_error},
                modern=modern,
                is_error=True,
            )
        # Interactive clients poll read-only tools frequently. Recording a pair
        # of durable audit rows for every poll does not prove a state change and
        # previously allowed idle UIs to grow the event ledger without bound.
        # Restricted model runs pass an explicit allow-list; keep their read-only
        # calls fully audited because provider receipts bind those exact events.
        audit_tool_call = name not in READ_ONLY_TOOLS or allowed_tools is not None
        call_sha256 = (
            _mutation_call_sha256(bridge, name, arguments)
            if name in IDEMPOTENT_MESSAGE_TOOLS
            else None
        )
        replayed_result: dict[str, Any] | None = None
        if call_sha256 is not None:
            try:
                replayed_result = _load_mutation_receipt(
                    bridge, call_sha256, name, arguments
                )
            except BridgeError as exc:
                return content_response(
                    request_id,
                    {"error": str(exc), "tool": name},
                    modern=modern,
                    is_error=True,
                )
            except (json.JSONDecodeError, sqlite3.Error):
                return error_response(
                    request_id, -32603, "internal bridge database error"
                )
        if audit_tool_call:
            with bridge._connect() as connection:
                bridge._event(
                    connection,
                    "tool.called",
                    {
                        "tool": name,
                        "arguments_sha256": stable_sha256(
                            _audit_safe_arguments(arguments)
                        ),
                    },
                )
        try:
            dispatch_arguments = arguments
            if call_sha256 is not None and replayed_result is None:
                dispatch_arguments = {
                    **arguments,
                    "__mcp_receipt": {
                        "call_sha256": call_sha256,
                        "arguments_sha256": stable_sha256(arguments),
                        "session_id": bridge.session_id,
                        "tool": name,
                    },
                }
            result = (
                replayed_result
                if replayed_result is not None
                else dispatch(bridge, str(name), dispatch_arguments)
            )
            if audit_tool_call:
                _audit_tool_result_fail_safe(bridge, name, result)
            if name == "list_own_observable_sessions":
                return _bounded_observable_sessions_response(
                    request_id,
                    result,
                    modern=modern,
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


def _read_bounded_stdio_frame(stream: Any) -> tuple[str | None, str | None]:
    """Read one newline-delimited request without retaining an oversized frame."""

    binary = getattr(stream, "buffer", None)
    if binary is not None:
        payload = binary.readline(MAX_STDIO_REQUEST_BYTES + 1)
        if not payload:
            return None, None
        oversized = len(payload) > MAX_STDIO_REQUEST_BYTES
        while oversized and payload and not payload.endswith(b"\n"):
            payload = binary.readline(MAX_STDIO_REQUEST_BYTES + 1)
        if oversized:
            return "", "request exceeds the stdio byte limit"
        try:
            return payload.decode("utf-8"), None
        except UnicodeDecodeError:
            return "", "request is not valid UTF-8"

    line = stream.readline(MAX_STDIO_REQUEST_BYTES + 1)
    if not line:
        return None, None
    try:
        oversized = len(line.encode("utf-8")) > MAX_STDIO_REQUEST_BYTES
    except UnicodeEncodeError:
        return "", "request is not valid UTF-8"
    while oversized and line and not line.endswith("\n"):
        line = stream.readline(MAX_STDIO_REQUEST_BYTES + 1)
    if oversized:
        return "", "request exceeds the stdio byte limit"
    return line, None


def _serialize_stdio_frame_with_limit(
    response: dict[str, Any], max_bytes: int
) -> str | None:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        separators=(",", ":"),
    )
    parts: list[str] = []
    byte_count = 1  # Trailing newline.
    for part in encoder.iterencode(response):
        byte_count += len(part.encode("utf-8"))
        if byte_count > max_bytes:
            return None
        parts.append(part)
    return "".join(parts) + "\n"


def _bounded_stdio_response_frame(response: dict[str, Any]) -> str:
    max_bytes = max(1, int(MAX_STDIO_RESPONSE_BYTES))
    serialized = _serialize_stdio_frame_with_limit(response, max_bytes)
    if serialized is not None:
        return serialized
    request_id = response.get("id") if isinstance(response, dict) else None
    overflow = error_response(
        request_id,
        -32603,
        "response exceeds the stdio byte limit",
    )
    serialized = _serialize_stdio_frame_with_limit(overflow, max_bytes)
    if serialized is not None:
        return serialized
    minimal = error_response(None, -32603, "stdio response too large")
    serialized = _serialize_stdio_frame_with_limit(minimal, max_bytes)
    if serialized is None:
        raise ValueError("stdio response byte limit is too small for an error frame")
    return serialized


def _write_stdio_response(response: dict[str, Any]) -> None:
    frame = _bounded_stdio_response_frame(response)
    binary = getattr(sys.stdout, "buffer", None)
    if binary is not None:
        binary.write(frame.encode("utf-8"))
        binary.flush()
        return
    sys.stdout.write(frame)
    sys.stdout.flush()


def _effective_capability_tools(
    capability: AgentIdentityCapability,
) -> frozenset[str]:
    if capability.uses_legacy_tool_fallback:
        return LEGACY_CAPABILITY_TOOLS
    return frozenset(capability.allowed_tools)


def _revalidate_bound_room_revision(
    bridge: Bridge, capability: AgentIdentityCapability
) -> None:
    room_id = capability.bound_room_id
    if room_id is None:
        return
    with bridge._connect() as connection:
        membership = connection.execute(
            """SELECT room_session_id, route_profile_id
                 FROM room_memberships
                WHERE scope=? AND room_id=? AND agent_id=? AND status='active'""",
            (bridge.scope, room_id, bridge.agent_id),
        ).fetchone()
        if membership is None:
            raise AgentIdentityError("capability-bound room membership is inactive")
        if (
            capability.bound_room_session_id is not None
            and membership["room_session_id"] != capability.bound_room_session_id
        ):
            raise AgentIdentityError("capability-bound room session changed")
        if (
            capability.bound_route_profile_id is not None
            and membership["route_profile_id"] != capability.bound_route_profile_id
        ):
            raise AgentIdentityError("capability-bound room route changed")
        if capability.bound_route_profile_id is not None:
            profile = connection.execute(
                """SELECT * FROM route_profiles
                    WHERE scope=? AND route_id=? AND enabled=1""",
                (bridge.scope, capability.bound_route_profile_id),
            ).fetchone()
            if profile is None:
                raise AgentIdentityError("capability-bound route profile is unavailable")
            try:
                profile_sha256 = bridge._verified_route_profile_sha256(profile)
            except BridgeError as exc:
                raise AgentIdentityError(
                    "capability-bound route profile is invalid"
                ) from exc
            if not secrets.compare_digest(
                profile_sha256,
                str(capability.bound_route_profile_sha256 or ""),
            ):
                raise AgentIdentityError("capability-bound route profile changed")


def _revalidate_identity_capability_session(
    bridge: Bridge,
    expected: AgentIdentityCapability,
    allowed_tools: AbstractSet[str],
) -> None:
    current = verify_agent_identity_capability(
        bridge.root,
        bridge.db_path,
        bridge.scope,
        bridge.agent_id,
        expected.path,
    )
    if (
        current.capability_id != expected.capability_id
        or current.capability_sha256 != expected.capability_sha256
        or current.schema != expected.schema
        or current.allowed_tools != expected.allowed_tools
        or current.issued_by != expected.issued_by
        or current.route_binding != expected.route_binding
        or current.bound_room_id != expected.bound_room_id
        or current.bound_room_session_id != expected.bound_room_session_id
        or current.bound_route_profile_id != expected.bound_route_profile_id
        or current.bound_route_profile_sha256
        != expected.bound_route_profile_sha256
    ):
        raise AgentIdentityError("Agent identity capability binding changed")
    verify_agent_identity_route_binding(
        current,
        client_name=bridge.client_name,
        provider_id=bridge.provider_id,
        model_id=bridge.model_id,
        reasoning_mode=bridge.reasoning_mode,
        route_class=bridge.route_class,
    )
    _revalidate_bound_room_revision(bridge, current)
    if not set(allowed_tools).issubset(_effective_capability_tools(current)):
        raise AgentIdentityError("Agent identity capability tool binding changed")


def serve(
    bridge: Bridge,
    allowed_tools: AbstractSet[str] | None = None,
    denied_tools: AbstractSet[str] = frozenset(),
    *,
    identity_capability: AgentIdentityCapability | None = None,
) -> int:
    if identity_capability is not None:
        capability_tools = _effective_capability_tools(identity_capability)
        if allowed_tools is None:
            allowed_tools = capability_tools
        if identity_capability.bound_room_id is not None:
            denied_tools = frozenset({*denied_tools, "list_rooms", "read_memory"})
        _revalidate_identity_capability_session(
            bridge,
            identity_capability,
            allowed_tools,
        )
    if allowed_tools is not None:
        unknown = sorted(set(allowed_tools) - HANDLERS.keys())
        if unknown:
            raise ValueError(f"unknown allowed tools: {', '.join(unknown)}")
    unknown_denied = sorted(set(denied_tools) - HANDLERS.keys())
    if unknown_denied:
        raise ValueError(f"unknown denied tools: {', '.join(unknown_denied)}")
    bridge.touch_presence("stdio")
    recent_calls: deque[float] = deque()
    call_count = 0
    stop = threading.Event()
    capability_fenced = False

    def revalidate_capability() -> None:
        nonlocal capability_fenced
        if capability_fenced:
            raise AgentIdentityError("Agent identity capability session is fenced")
        if identity_capability is None:
            return
        try:
            _revalidate_identity_capability_session(
                bridge,
                identity_capability,
                allowed_tools or frozenset(),
            )
        except AgentIdentityError:
            capability_fenced = True
            stop.set()
            try:
                bridge.clear_presence()
            except sqlite3.Error:
                pass
            raise AgentIdentityError(
                "Agent identity capability session is fenced"
            ) from None

    thread = threading.Thread(target=_heartbeat, args=(bridge, stop), daemon=True)
    thread.start()
    try:
        while True:
            line, frame_error = _read_bounded_stdio_frame(sys.stdin)
            if line is None:
                break
            if frame_error is not None or line.strip():
                now = time.monotonic()
                while recent_calls and now - recent_calls[0] >= 60.0:
                    recent_calls.popleft()
                if (
                    call_count >= MAX_STDIO_CALLS_PER_SESSION
                    or len(recent_calls) >= MAX_STDIO_CALLS_PER_MINUTE
                ):
                    _write_stdio_response(
                        error_response(
                            None,
                            -32003,
                            "MCP session call budget exceeded; session closed",
                        )
                    )
                    break
                call_count += 1
                recent_calls.append(now)
            if frame_error is not None:
                _write_stdio_response(error_response(None, -32600, frame_error))
                continue
            if not line.strip():
                continue
            request: Any = None
            try:
                request = json.loads(line.lstrip("\ufeff"))
                if (
                    isinstance(request, dict)
                    and request.get("method") == "tools/call"
                ):
                    try:
                        revalidate_capability()
                    except AgentIdentityError:
                        _write_stdio_response(
                            error_response(
                                request.get("id"),
                                -32003,
                                "Agent identity capability is invalid or revoked; "
                                "session fenced",
                            )
                        )
                        continue
                result = handle_request(
                    bridge,
                    request,
                    allowed_tools,
                    denied_tools,
                    bound_room_id=(
                        identity_capability.bound_room_id
                        if identity_capability is not None
                        else None
                    ),
                )
                if result is not None:
                    _write_stdio_response(result)
            except json.JSONDecodeError as exc:
                _write_stdio_response(
                    error_response(None, -32700, f"invalid JSON: {exc}")
                )
            except Exception as exc:  # pragma: no cover - final transport guard
                print(f"peerbridge internal error: {exc}", file=sys.stderr, flush=True)
                request_id = request.get("id") if isinstance(request, dict) else None
                if request_id is not None:
                    _write_stdio_response(
                        error_response(request_id, -32603, "internal bridge error")
                    )
    finally:
        stop.set()
        thread.join(timeout=2)
        bridge.clear_presence()
        bridge.checkpoint_wal()
    return 0
