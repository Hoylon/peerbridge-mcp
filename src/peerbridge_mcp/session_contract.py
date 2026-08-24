"""Provider-neutral, capability-explicit session projections for the Cockpit."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .bridge import DEFAULT_ROOM_ROLE
from .secret_scan import redact_secrets


SESSION_SOURCE_TYPES = frozenset(
    {
        "peerbridge-room",
        "managed-cli",
        "authorized-desktop",
        "authorized-terminal",
    }
)
SESSION_CAPABILITIES = (
    "detectable",
    "mirrorable",
    "input_capable",
    "context_resumable",
    "terminal_controllable",
    "model_route_only",
)
ROOM_DISPATCH_ERROR_CODES = frozenset(
    {
        "credential_unavailable",
        "dispatch_attempts_exhausted",
        "dispatch_lease_renewal_failed",
        "discussion_cancelled",
        "discussion_participant_unavailable",
        "discussion_source_binding_failed",
        "discussion_stopped",
        "discussion_superseded",
        "discussion_timed_out",
        "inference_failed",
        "mcp_transport_failed",
        "provider_access_denied",
        "provider_authentication_required",
        "provider_billing_required",
        "provider_http_failed",
        "provider_http_retryable",
        "provider_rate_limited",
        "resource_unavailable",
        "room_seat_route_changed",
        "room_seat_unavailable",
        "route_handoff_mismatch",
        "route_mismatch",
        "route_profile_changed",
        "route_profile_unavailable",
        "run_cancelled",
        "runner_cancellation_incomplete",
        "runner_hard_deadline_exceeded",
        "tool_policy_failed",
        "unexpected_runtime_failure",
        "unexpected_worker_failure",
    }
)


class SessionContractError(ValueError):
    """A source attempted to claim an invalid or ambiguous Cockpit session."""


def session_panel_id(source_type: str, source_session_id: str) -> str:
    source = str(source_type or "").strip()
    session = str(source_session_id or "").strip()
    if source not in SESSION_SOURCE_TYPES or not session or len(session) > 500:
        raise SessionContractError("session source identity is invalid")
    return session if source == "managed-cli" else f"{source}:{session}"


def _capabilities(values: Mapping[str, Any]) -> dict[str, bool]:
    missing = [key for key in SESSION_CAPABILITIES if key not in values]
    if missing:
        raise SessionContractError(
            "session capability contract is incomplete: " + ", ".join(missing)
        )
    return {key: bool(values[key]) for key in SESSION_CAPABILITIES}


def _sha256_value(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        return None
    return text


def _route_identity_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > 500 or redact_secrets(text) != text:
        return None
    return text


def _latest_verified_room_route_evidence(
    messages: Iterable[Mapping[str, Any]],
    *,
    agent_id: str,
    route_profile_id: str | None,
    route_profile_sha256: str | None,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for message in messages:
        if str(message.get("recipient") or "") != agent_id:
            continue
        evaluation = message.get("route_evaluation")
        if not isinstance(evaluation, Mapping) or evaluation.get("status") != "verified":
            continue
        observed = evaluation.get("observed")
        if not isinstance(observed, Mapping):
            continue
        if str(observed.get("agent_id") or "") != agent_id:
            continue
        provider_id = _route_identity_value(observed.get("provider_id"))
        model_id = _route_identity_value(observed.get("model_id"))
        if provider_id is None or model_id is None:
            continue
        request = evaluation.get("request")
        request = request if isinstance(request, Mapping) else {}
        observed_profile_id = str(
            message.get("route_profile_id") or request.get("route_profile_id") or ""
        ).strip()
        if route_profile_id and observed_profile_id != route_profile_id:
            continue
        observed_profile_sha = _sha256_value(
            message.get("route_profile_sha256")
            or request.get("route_profile_sha256")
        )
        if route_profile_sha256 and observed_profile_sha != route_profile_sha256:
            continue
        route_receipt = message.get("route_receipt")
        route_receipt = route_receipt if isinstance(route_receipt, Mapping) else {}
        receipt_sha256 = _sha256_value(
            message.get("route_receipt_sha256")
            or route_receipt.get("receipt_sha256")
        )
        if receipt_sha256 is None:
            continue
        try:
            sequence = int(message.get("sequence") or 0)
        except (TypeError, ValueError):
            continue
        candidates.append(
            (
                sequence,
                {
                    "status": "verified",
                    "source": "message-route-receipt",
                    "message_id": str(message.get("message_id") or ""),
                    "route_profile_id": observed_profile_id or None,
                    "route_receipt_sha256": receipt_sha256,
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "reasoning_mode": _route_identity_value(
                        observed.get("reasoning_mode")
                    ),
                    "route_class": _route_identity_value(
                        observed.get("route_class")
                    ),
                },
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _observed_route_label(evidence: Mapping[str, Any]) -> str:
    return " / ".join(
        str(evidence[key])
        for key in ("provider_id", "model_id", "reasoning_mode", "route_class")
        if evidence.get(key)
    )


def normalize_session_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    source_type = str(value.get("source_type") or "").strip()
    source_session_id = str(value.get("source_session_id") or "").strip()
    panel_id = session_panel_id(source_type, source_session_id)
    supplied_panel_id = str(value.get("session_id") or panel_id)
    if supplied_panel_id != panel_id:
        raise SessionContractError("session panel identity does not match its source")
    result = dict(value)
    result["session_id"] = panel_id
    result["source_type"] = source_type
    result["source_session_id"] = source_session_id
    result["capabilities"] = _capabilities(
        value.get("capabilities")
        if isinstance(value.get("capabilities"), Mapping)
        else {}
    )
    result["events"] = [
        dict(event)
        for event in value.get("events") or ()
        if isinstance(event, Mapping)
    ]
    return result


def managed_cli_session_contract(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    source_session_id = str(snapshot.get("session_id") or "").strip()
    state = str(snapshot.get("state") or "").strip().lower()
    running = state == "running"
    raw_session_contract = snapshot.get("session_contract")
    session_contract = (
        raw_session_contract if isinstance(raw_session_contract, Mapping) else {}
    )
    explicit_input_capability = snapshot.get("can_submit_input")
    if isinstance(explicit_input_capability, bool):
        input_capable = running and explicit_input_capability
    elif bool(session_contract.get("additional_input_supported")):
        input_capable = running
    else:
        input_capable = running and not bool(snapshot.get("input_submitted"))
    result = {
        **dict(snapshot),
        "source_type": "managed-cli",
        "source_session_id": source_session_id,
        "session_id": source_session_id,
        "source_conversation_id": source_session_id,
        "input_owner": "peerbridge-cockpit",
        "capabilities": {
            "detectable": True,
            "mirrorable": True,
            "input_capable": input_capable,
            "context_resumable": bool(session_contract.get("resume_supported")),
            "terminal_controllable": running,
            "model_route_only": False,
        },
    }
    return normalize_session_contract(result)


def native_room_session_contract(
    *,
    scope: str,
    room_id: str,
    member: Mapping[str, Any],
    messages: Iterable[Mapping[str, Any]],
    dispatches: Iterable[Mapping[str, Any]] = (),
    room_name: str | None = None,
    operator_active: bool = False,
) -> dict[str, Any]:
    expected_room_id = str(room_id or "").strip()
    source_session_id = str(member.get("room_session_id") or "").strip()
    agent_id = str(member.get("agent_id") or "").strip()
    if not source_session_id or not agent_id:
        raise SessionContractError("room session identity is incomplete")
    room_messages = tuple(
        message
        for message in messages
        if str(message.get("room_id") or "").strip() == expected_room_id
    )
    dispatch_by_message = {
        str(row.get("message_id") or ""): row
        for row in dispatches
        if str(row.get("agent_id") or "") == agent_id
        and str(row.get("message_id") or "")
    }
    route_id = str(member.get("route_profile_id") or "").strip() or None
    raw_route_profile_sha256 = str(
        member.get("route_profile_sha256") or ""
    ).strip()
    route_profile_sha256 = _sha256_value(raw_route_profile_sha256)
    route_binding_valid = bool(
        route_id
        and (
            not raw_route_profile_sha256
            or route_profile_sha256 is not None
        )
    )
    route_evidence = (
        _latest_verified_room_route_evidence(
            room_messages,
            agent_id=agent_id,
            route_profile_id=route_id,
            route_profile_sha256=route_profile_sha256,
        )
        if route_binding_valid
        else None
    )
    route_evidence_source = (
        f"message-route-receipt:{route_evidence['route_receipt_sha256']}"
        if route_evidence is not None
        else None
    )
    events: list[dict[str, Any]] = []
    for message in room_messages:
        sender = str(message.get("sender") or "")
        recipient = str(message.get("recipient") or "")
        if sender != agent_id and recipient not in {agent_id, "*"}:
            continue
        try:
            sequence = int(message.get("sequence") or 0)
        except (TypeError, ValueError):
            continue
        body = redact_secrets(str(message.get("body") or ""))
        subject = redact_secrets(str(message.get("subject") or ""))
        message_sequence = sequence * 2
        events.append(
            {
                "sequence": message_sequence,
                "source_sequence": sequence,
                "created_utc": str(message.get("created_utc") or ""),
                "stream": "system",
                "kind": "answer" if sender == agent_id else "activity",
                "text": body,
                "summary": subject or None,
            }
        )
        dispatch = dispatch_by_message.get(str(message.get("message_id") or ""))
        dispatch_status = str((dispatch or {}).get("status") or "").strip().lower()
        if sender == agent_id or dispatch_status not in {
            "claimed",
            "retryable",
            "failed",
        }:
            continue
        raw_error_code = str((dispatch or {}).get("error_code") or "").strip()
        error_code = (
            raw_error_code
            if raw_error_code in ROOM_DISPATCH_ERROR_CODES
            else "dispatch_failed"
            if raw_error_code
            else ""
        )
        try:
            attempts = max(0, int((dispatch or {}).get("attempt_count") or 0))
        except (TypeError, ValueError):
            attempts = 0
        detail = f"Delivery to {agent_id}: {dispatch_status}"
        if error_code:
            detail += f" ({error_code})"
        if attempts:
            detail += f", attempts={attempts}"
        events.append(
            {
                "sequence": message_sequence + 1,
                "source_sequence": sequence,
                "created_utc": str(
                    (dispatch or {}).get("updated_utc")
                    or message.get("created_utc")
                    or ""
                ),
                "stream": "system",
                "kind": "error" if dispatch_status == "failed" else "activity",
                "text": detail,
                "summary": error_code or dispatch_status,
            }
        )
    events.sort(key=lambda row: (row["sequence"], row["created_utc"]))
    role_id = str(member.get("role_id") or DEFAULT_ROOM_ROLE)
    role_label = (
        str(member.get("role_label") or "").strip()
        if role_id == "custom"
        else None
    )
    online = bool(member.get("online"))
    input_capable = bool(
        operator_active and route_id and agent_id != "human-operator"
    )
    safe_room_name = redact_secrets(str(room_name or room_id).strip()) or str(room_id)
    result = {
        "session_id": session_panel_id("peerbridge-room", source_session_id),
        "source_type": "peerbridge-room",
        "source_session_id": source_session_id,
        "source_conversation_id": str(room_id),
        "source_conversation_name": safe_room_name,
        "room_id": str(room_id),
        "room_name": safe_room_name,
        "scope": str(scope),
        "agent_id": agent_id,
        "display_name": agent_id,
        "client_name": str(member.get("client_name") or "peerbridge-room"),
        "client_version": None,
        "role": role_id,
        "role_label": role_label,
        "working_directory": None,
        "state": "running" if online else "waiting" if route_id else "unavailable",
        "return_code": None,
        "started_utc": str(member.get("joined_utc") or ""),
        "ended_utc": None,
        "input_submitted": False,
        "requested_route": route_id,
        "observed_route": (
            _observed_route_label(route_evidence)
            if route_evidence is not None
            else None
        ),
        "observed_route_source": route_evidence_source,
        "observed_provider_id": (
            route_evidence.get("provider_id") if route_evidence is not None else None
        ),
        "observed_model_id": (
            route_evidence.get("model_id") if route_evidence is not None else None
        ),
        "observed_reasoning_mode": (
            route_evidence.get("reasoning_mode")
            if route_evidence is not None
            else None
        ),
        "observed_route_class": (
            route_evidence.get("route_class")
            if route_evidence is not None
            else None
        ),
        "route_evidence": dict(route_evidence) if route_evidence is not None else None,
        "model_id": (
            route_evidence.get("model_id")
            if route_evidence is not None
            else member.get("model_id")
        ),
        "model_source": (
            route_evidence_source
            if route_evidence is not None
            else "room-seat-request"
            if member.get("model_id")
            else None
        ),
        "usage": {"status": "unavailable", "source": None},
        "usage_capture_bounded": True,
        "usage_capture_truncated": False,
        "terminal_outcome": {
            "status": "unavailable",
            "process_status": "unavailable",
            "exit_code": None,
            "provider_status": "unavailable",
            "provider_reason": None,
            "source": "peerbridge-room",
        },
        "execution_mode": "observe",
        "governance_binding_id": None,
        "capture_mode": "peerbridge-room",
        "reasoning_contract": "observable-output-only",
        "input_owner": "peerbridge-conversation",
        "capabilities": {
            "detectable": True,
            "mirrorable": True,
            "input_capable": input_capable,
            "context_resumable": True,
            "terminal_controllable": False,
            "model_route_only": True,
        },
        "first_retained_sequence": events[0]["sequence"] if events else 0,
        "latest_sequence": events[-1]["sequence"] if events else 0,
        "events": events,
    }
    return normalize_session_contract(result)


def linked_room_session_target(
    sessions: Iterable[Mapping[str, Any]],
    *,
    agent_id: str,
    room_session_id: str,
) -> tuple[str, str] | None:
    """Return one exact adapter target, failing closed on live ambiguity."""

    expected_agent = str(agent_id or "").strip()
    expected_room_session = str(room_session_id or "").strip()
    matches = [
        value
        for value in sessions
        if str(value.get("owner_agent_id") or value.get("agent_id") or "")
        == expected_agent
        and str(value.get("room_session_id") or "") == expected_room_session
        and str(value.get("source_type") or "")
        in {"authorized-desktop", "authorized-terminal"}
    ]
    live = [
        value
        for value in matches
        if str(value.get("state") or "")
        not in {"completed", "stopped", "failed", "unavailable"}
    ]
    selected = live if live else matches
    if len(selected) != 1:
        return None
    return (
        str(selected[0]["source_type"]),
        str(selected[0]["source_session_id"]),
    )


__all__ = [
    "ROOM_DISPATCH_ERROR_CODES",
    "SESSION_CAPABILITIES",
    "SESSION_SOURCE_TYPES",
    "SessionContractError",
    "managed_cli_session_contract",
    "linked_room_session_target",
    "native_room_session_contract",
    "normalize_session_contract",
    "session_panel_id",
]
