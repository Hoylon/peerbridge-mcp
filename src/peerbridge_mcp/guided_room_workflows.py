"""Five-minute onboarding plans that reuse rooms and workflow templates."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .bridge import HUMAN_OPERATOR_ID, stable_sha256
from .operation_queue import ROOM_DISCUSSION_RESOURCE_PREFIX, WORKFLOW_TEMPLATES
from .secret_scan import contains_secret


GUIDED_WORKFLOW_ID = "investigate-debate"
GUIDED_OPERATION_SCHEMA = "peerbridge.guided-room-operation.v1"
GUIDED_OPERATION_PREFIX = "guided-room:"
GUIDED_OPERATION_TIMEOUT_SECONDS = 600
GUIDED_OPERATION_MAX_ATTEMPTS = 2
MAX_GUIDED_ROOM_AGENTS = 12
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class GuidedRoomWorkflowError(ValueError):
    """The current room cannot safely run the guided workflow."""


def _identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text) or contains_secret(text):
        raise GuidedRoomWorkflowError(f"{label} is invalid")
    return text


def _sha256(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not SHA256.fullmatch(text):
        raise GuidedRoomWorkflowError(f"{label} is invalid")
    return text


def _guided_participants(
    members: Iterable[Mapping[str, Any]],
) -> list[dict[str, str | None]]:
    participants: list[dict[str, str | None]] = []
    for member in members:
        if not isinstance(member, Mapping):
            raise GuidedRoomWorkflowError("guided room member binding is invalid")
        if (
            str(member.get("status") or "") != "active"
            or str(member.get("agent_id") or "") == HUMAN_OPERATOR_ID
        ):
            continue
        agent_id = _identifier(member.get("agent_id"), "room Agent id")
        room_session_id = _identifier(
            member.get("room_session_id"), "room session id"
        )
        membership_sha256 = _sha256(
            member.get("membership_sha256"), "room membership SHA-256"
        )
        route_profile_id = str(member.get("route_profile_id") or "").strip()
        if not route_profile_id:
            raise GuidedRoomWorkflowError(
                f"room Agent {agent_id} has no bound model route"
            )
        route_profile_id = _identifier(route_profile_id, "route profile id")
        route_profile_sha256 = _sha256(
            member.get("route_profile_sha256"), "route profile SHA-256"
        )
        role_id = _identifier(
            member.get("role_id") or "equal-participant", "room role"
        )
        role_label = str(member.get("role_label") or "").strip() or None
        if role_label and (len(role_label) > 80 or contains_secret(role_label)):
            raise GuidedRoomWorkflowError("custom room role is unsafe")
        if any(participant["agent_id"] == agent_id for participant in participants):
            raise GuidedRoomWorkflowError("room Agent id is duplicated")
        participants.append(
            {
                "agent_id": agent_id,
                "room_session_id": room_session_id,
                "membership_sha256": membership_sha256,
                "route_profile_id": route_profile_id,
                "route_profile_sha256": route_profile_sha256,
                "role_id": role_id,
                "role_label": role_label,
                "role_binding_sha256": stable_sha256(
                    {"role_id": role_id, "role_label": role_label}
                ),
            }
        )
    participants.sort(key=lambda value: str(value["agent_id"]))
    return participants


def guided_room_readiness(
    members: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return an actionable, non-technical readiness state for the first workflow."""

    rows = tuple(members)
    active_agents = tuple(
        member
        for member in rows
        if isinstance(member, Mapping)
        and str(member.get("status") or "") == "active"
        and str(member.get("agent_id") or "") != HUMAN_OPERATOR_ID
    )
    active_count = len(active_agents)
    missing_route_count = sum(
        1 for member in active_agents if not str(member.get("route_profile_id") or "").strip()
    )
    base = {
        "participant_count": active_count,
        "missing_route_count": missing_route_count,
        "maximum_participants": MAX_GUIDED_ROOM_AGENTS,
    }
    if active_count < 2:
        return {**base, "ready": False, "code": "need_agents"}
    if missing_route_count:
        return {**base, "ready": False, "code": "need_routes"}
    if active_count > MAX_GUIDED_ROOM_AGENTS:
        return {**base, "ready": False, "code": "too_many_agents"}
    try:
        participants = _guided_participants(rows)
    except GuidedRoomWorkflowError:
        return {**base, "ready": False, "code": "incomplete_binding"}
    return {
        **base,
        "ready": True,
        "code": "ready",
        "participant_count": len(participants),
    }


def guided_operation_id(room_id: str, task_id: str) -> str:
    room_id = _identifier(room_id, "room id")
    task_id = _identifier(task_id, "task id")
    return GUIDED_OPERATION_PREFIX + hashlib.sha256(
        f"{room_id}\0{task_id}".encode("utf-8")
    ).hexdigest()[:40]


def guided_room_workflow_plan(
    *,
    room_id: str,
    task_id: str,
    task_text: str,
    members: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one bounded read-only room discussion from existing Seat identities."""

    room_id = _identifier(room_id, "room id")
    task_id = _identifier(task_id, "task id")
    task = str(task_text or "").strip()
    if not task:
        raise GuidedRoomWorkflowError("guided task is required")
    if len(task) > 8_000:
        raise GuidedRoomWorkflowError("guided task exceeds 8000 characters")
    if contains_secret(task):
        raise GuidedRoomWorkflowError("guided task appears to contain a credential")

    participants = _guided_participants(members)
    if len(participants) < 2:
        raise GuidedRoomWorkflowError(
            "guided multi-Agent workflow requires at least two routed room Agents"
        )
    if len(participants) > MAX_GUIDED_ROOM_AGENTS:
        raise GuidedRoomWorkflowError(
            f"guided workflow supports at most {MAX_GUIDED_ROOM_AGENTS} room Agents"
        )

    template = WORKFLOW_TEMPLATES[GUIDED_WORKFLOW_ID]
    source_binding = {
        "room_id": room_id,
        "workflow_id": GUIDED_WORKFLOW_ID,
        "participants": participants,
    }
    source_binding_sha256 = stable_sha256(source_binding)
    roster = "\n".join(
        f"- {participant['agent_id']}: "
        f"{participant['role_label'] or participant['role_id']}"
        for participant in participants
    )
    body = (
        "PeerBridge guided room workflow.\n"
        f"Template: {template['label']} ({GUIDED_WORKFLOW_ID})\n"
        "Execution: observe and report only. Do not modify project files.\n"
        "Keep each Agent's evidence and disagreement separate. Report only observable "
        "evidence and explicit summaries, never hidden reasoning.\n"
        f"Source binding SHA-256: {source_binding_sha256}\n\n"
        f"Current room roles:\n{roster}\n\n"
        f"Task:\n{task}"
    )
    prompt_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    operation_id = guided_operation_id(room_id, task_id)
    operation_payload = {
        "schema": GUIDED_OPERATION_SCHEMA,
        "room_id": room_id,
        "task_id": task_id,
        "task": task,
        "participants": participants,
        "source_binding_sha256": source_binding_sha256,
        "prompt_sha256": prompt_sha256,
    }
    operation_task_text = json.dumps(
        operation_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "workflow_id": GUIDED_WORKFLOW_ID,
        "workflow_label": str(template["label"]),
        "room_id": room_id,
        "task_id": task_id,
        "subject": "Guided investigate and compare",
        "body": body,
        "priority": "normal",
        "automation_mode": "discussion",
        "max_rounds": 2,
        "max_messages": max(8, len(participants) * 4),
        "stagnation_rounds": 1,
        "participant_count": len(participants),
        "participants": tuple(participants),
        "source_binding_sha256": source_binding_sha256,
        "prompt_sha256": prompt_sha256,
        "execution_mode": "room-observe",
        "operation_id": operation_id,
        "operation_task_text": operation_task_text,
        "operation_working_directory": ".",
        "operation_resource_key": (
            f"{ROOM_DISCUSSION_RESOURCE_PREFIX}{source_binding_sha256}"
        ),
        "operation_max_attempts": GUIDED_OPERATION_MAX_ATTEMPTS,
        "operation_timeout_seconds": GUIDED_OPERATION_TIMEOUT_SECONDS,
    }


def validate_guided_room_source(
    plan: Mapping[str, Any], members: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Recompute the live room, membership, route, and role binding."""

    room_id = _identifier(plan.get("room_id"), "room id")
    expected = [dict(participant) for participant in plan.get("participants") or ()]
    observed = _guided_participants(members)
    if observed != expected:
        raise GuidedRoomWorkflowError(
            "guided room membership, role, or complete route state changed"
        )
    source_binding = {
        "room_id": room_id,
        "workflow_id": GUIDED_WORKFLOW_ID,
        "participants": observed,
    }
    source_binding_sha256 = stable_sha256(source_binding)
    if source_binding_sha256 != str(plan.get("source_binding_sha256") or ""):
        raise GuidedRoomWorkflowError("guided room source binding SHA-256 changed")
    return {
        "source_binding_sha256": source_binding_sha256,
        "participant_count": len(observed),
    }


def validate_guided_room_start(
    plan: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    members: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Verify that the opened discussion uses exactly the planned room seats."""

    if str(receipt.get("automation_mode") or "") != "discussion":
        raise GuidedRoomWorkflowError("guided discussion did not start in discussion mode")
    for key in ("room_id", "task_id"):
        if str(receipt.get(key) or "") != str(plan.get(key) or ""):
            raise GuidedRoomWorkflowError(f"guided discussion {key} changed during start")
    discussion_id = str(receipt.get("discussion_id") or "")
    discussion_sha256 = str(receipt.get("discussion_sha256") or "")
    if not SAFE_ID.fullmatch(discussion_id) or not SHA256.fullmatch(
        discussion_sha256
    ):
        raise GuidedRoomWorkflowError("guided discussion returned invalid evidence")

    source = validate_guided_room_source(plan, members)
    expected = sorted(
        (
            str(participant["agent_id"]),
            str(participant["route_profile_id"]),
            str(participant["route_profile_sha256"]),
        )
        for participant in plan.get("participants") or ()
    )
    recipients = receipt.get("recipients")
    if not isinstance(recipients, (list, tuple)):
        raise GuidedRoomWorkflowError("guided discussion recipient evidence is missing")
    observed = sorted(
        (
            str(recipient.get("agent_id") or ""),
            str(recipient.get("route_profile_id") or ""),
            str(recipient.get("route_profile_sha256") or ""),
        )
        for recipient in recipients
        if isinstance(recipient, Mapping)
    )
    if observed != expected or int(receipt.get("fanout_count") or 0) != len(expected):
        raise GuidedRoomWorkflowError(
            "guided discussion seats or model routes changed during start"
        )
    return {
        "discussion_id": discussion_id,
        "discussion_sha256": discussion_sha256,
        "participant_count": len(expected),
        "source_binding_sha256": source["source_binding_sha256"],
    }


def guided_plan_from_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild and verify one durable guided-room operation after restart."""

    try:
        payload = json.loads(str(operation.get("task_text") or ""))
    except json.JSONDecodeError as exc:
        raise GuidedRoomWorkflowError("guided operation payload is invalid") from exc
    expected_fields = {
        "schema",
        "room_id",
        "task_id",
        "task",
        "participants",
        "source_binding_sha256",
        "prompt_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise GuidedRoomWorkflowError("guided operation payload fields are invalid")
    if payload.get("schema") != GUIDED_OPERATION_SCHEMA:
        raise GuidedRoomWorkflowError("guided operation schema is unsupported")
    raw_participants = payload.get("participants")
    if not isinstance(raw_participants, list):
        raise GuidedRoomWorkflowError("guided operation participants are invalid")
    if not raw_participants or any(
        not isinstance(member, Mapping) for member in raw_participants
    ):
        raise GuidedRoomWorkflowError("guided operation participants are invalid")
    rebuilt = guided_room_workflow_plan(
        room_id=str(payload.get("room_id") or ""),
        task_id=str(payload.get("task_id") or ""),
        task_text=str(payload.get("task") or ""),
        members=tuple({**member, "status": "active"} for member in raw_participants),
    )
    checks = {
        "workflow_id": rebuilt["workflow_id"],
        "operation_id": rebuilt["operation_id"],
        "task_text": rebuilt["operation_task_text"],
        "working_directory": rebuilt["operation_working_directory"],
        "resource_key": rebuilt["operation_resource_key"],
        "max_attempts": rebuilt["operation_max_attempts"],
        "timeout_seconds": rebuilt["operation_timeout_seconds"],
    }
    if any(operation.get(key) != value for key, value in checks.items()):
        raise GuidedRoomWorkflowError("guided operation source binding does not match")
    bound_discussion_id = str(operation.get("bound_discussion_id") or "")
    if bound_discussion_id and not SAFE_ID.fullmatch(bound_discussion_id):
        raise GuidedRoomWorkflowError("guided operation discussion binding is invalid")
    rebuilt["bound_discussion_id"] = bound_discussion_id or None
    return rebuilt


__all__ = [
    "GUIDED_WORKFLOW_ID",
    "GUIDED_OPERATION_SCHEMA",
    "GuidedRoomWorkflowError",
    "guided_operation_id",
    "guided_plan_from_operation",
    "guided_room_readiness",
    "guided_room_workflow_plan",
    "validate_guided_room_source",
    "validate_guided_room_start",
]
