from __future__ import annotations

import pytest

from peerbridge_mcp.session_contract import (
    SESSION_CAPABILITIES,
    SessionContractError,
    managed_cli_session_contract,
    linked_room_session_target,
    native_room_session_contract,
    normalize_session_contract,
    session_panel_id,
)


def test_native_room_contract_keeps_source_identity_and_truthful_capabilities() -> None:
    contract = native_room_session_contract(
        scope="project",
        room_id="alpha",
        member={
            "agent_id": "reviewer-one",
            "room_session_id": "room-session-one",
            "role_id": "reviewer",
            "route_profile_id": "route-one",
            "client_name": "codex",
            "model_id": "gpt-test",
            "online": True,
            "joined_utc": "2026-08-19T00:00:00Z",
        },
        messages=(
            {
                "room_id": "alpha",
                "sequence": 7,
                "sender": "reviewer-one",
                "recipient": "human-operator",
                "subject": "Review result",
                "body": "Observable answer",
                "created_utc": "2026-08-19T00:01:00Z",
            },
            {
                "room_id": "alpha",
                "sequence": 8,
                "sender": "other-agent",
                "recipient": "other-agent",
                "body": "must not cross panels",
                "created_utc": "2026-08-19T00:02:00Z",
            },
        ),
        room_name="Alpha Review Room",
    )

    assert contract["session_id"] == "peerbridge-room:room-session-one"
    assert contract["source_session_id"] == "room-session-one"
    assert contract["source_conversation_id"] == "alpha"
    assert contract["source_conversation_name"] == "Alpha Review Room"
    assert contract["room_id"] == "alpha"
    assert contract["room_name"] == "Alpha Review Room"
    assert contract["events"][0]["text"] == "Observable answer"
    assert contract["events"][0]["stream"] == "system"
    assert len(contract["events"]) == 1
    assert contract["observed_route"] is None
    assert contract["observed_route_source"] is None
    assert contract["observed_provider_id"] is None
    assert contract["observed_model_id"] is None
    assert contract["route_evidence"] is None
    assert contract["model_id"] == "gpt-test"
    assert contract["model_source"] == "room-seat-request"
    assert contract["input_owner"] == "peerbridge-conversation"
    assert contract["capabilities"] == {
        "detectable": True,
        "mirrorable": True,
        "input_capable": False,
        "context_resumable": True,
        "terminal_controllable": False,
        "model_route_only": True,
    }


def test_native_room_contract_projects_only_current_verified_route_receipt() -> None:
    profile_sha = "a" * 64
    receipt_sha = "b" * 64
    contract = native_room_session_contract(
        scope="project",
        room_id="build-room",
        member={
            "agent_id": "grok-build",
            "room_session_id": "grok-room-session",
            "route_profile_id": "grok-build-route",
            "route_profile_sha256": profile_sha,
            "model_id": "requested-alias",
            "status": "active",
        },
        messages=(
            {
                "room_id": "build-room",
                "message_id": "verified-request",
                "sequence": 10,
                "sender": "human-operator",
                "recipient": "grok-build",
                "subject": "Review",
                "body": "Review this change.",
                "route_profile_id": "grok-build-route",
                "route_profile_sha256": profile_sha,
                "route_receipt_sha256": receipt_sha,
                "route_evaluation": {
                    "status": "verified",
                    "request": {"route_profile_id": "grok-build-route"},
                    "observed": {
                        "agent_id": "grok-build",
                        "provider_id": "xai-relay",
                        "model_id": "grok-4.6-build",
                        "reasoning_mode": "high",
                        "route_class": "relay",
                    },
                },
            },
            {
                "room_id": "build-room",
                "message_id": "reply-one",
                "sequence": 11,
                "sender": "grok-build",
                "recipient": "human-operator",
                "reply_to": "verified-request",
                "subject": "Answer",
                "body": "Independent Grok Build answer.",
            },
            {
                "room_id": "build-room",
                "message_id": "unreceipted-newer-request",
                "sequence": 12,
                "sender": "human-operator",
                "recipient": "grok-build",
                "route_profile_id": "grok-build-route",
                "route_profile_sha256": profile_sha,
                "route_evaluation": {
                    "status": "verified",
                    "request": {"route_profile_id": "grok-build-route"},
                    "observed": {
                        "agent_id": "grok-build",
                        "provider_id": "must-not-project",
                        "model_id": "must-not-project",
                    },
                },
            },
        ),
    )

    assert contract["requested_route"] == "grok-build-route"
    assert contract["observed_route"] == (
        "xai-relay / grok-4.6-build / high / relay"
    )
    assert contract["observed_route_source"] == (
        f"message-route-receipt:{receipt_sha}"
    )
    assert contract["observed_provider_id"] == "xai-relay"
    assert contract["observed_model_id"] == "grok-4.6-build"
    assert contract["model_id"] == "grok-4.6-build"
    assert contract["model_source"] == f"message-route-receipt:{receipt_sha}"
    assert contract["route_evidence"] == {
        "status": "verified",
        "source": "message-route-receipt",
        "message_id": "verified-request",
        "route_profile_id": "grok-build-route",
        "route_receipt_sha256": receipt_sha,
        "provider_id": "xai-relay",
        "model_id": "grok-4.6-build",
        "reasoning_mode": "high",
        "route_class": "relay",
    }
    assert contract["capabilities"]["terminal_controllable"] is False
    assert contract["capabilities"]["model_route_only"] is True
    assert all(event["stream"] == "system" for event in contract["events"])


def test_native_room_contract_accepts_live_monitor_route_receipt_shape() -> None:
    contract = native_room_session_contract(
        scope="project",
        room_id="build-room",
        member={
            "agent_id": "kimi-k3",
            "room_session_id": "kimi-room-session",
            "route_profile_id": "kimi-current-route",
            "model_id": "requested-kimi-alias",
        },
        messages=(
            {
                "room_id": "build-room",
                "message_id": "verified-monitor-row",
                "sequence": 13,
                "sender": "human-operator",
                "recipient": "kimi-k3",
                "route_profile_id": "kimi-current-route",
                "route_profile_sha256": "c" * 64,
                "route_receipt_sha256": "d" * 64,
                "route_evaluation": {
                    "status": "verified",
                    "request": {"route_profile_id": "kimi-current-route"},
                    "observed": {
                        "agent_id": "kimi-k3",
                        "provider_id": "moonshot-relay",
                        "model_id": "kimi-k3",
                        "reasoning_mode": "high",
                        "route_class": "relay",
                    },
                },
            },
        ),
    )

    assert contract["observed_provider_id"] == "moonshot-relay"
    assert contract["observed_model_id"] == "kimi-k3"
    assert contract["model_source"] == f"message-route-receipt:{'d' * 64}"


def test_native_room_contract_does_not_reuse_receipt_from_an_old_route() -> None:
    contract = native_room_session_contract(
        scope="project",
        room_id="build-room",
        member={
            "agent_id": "kimi-k3",
            "room_session_id": "kimi-room-session",
            "route_profile_id": "kimi-current-route",
            "route_profile_sha256": "c" * 64,
            "model_id": "kimi-k3-requested",
        },
        messages=(
            {
                "room_id": "build-room",
                "message_id": "old-route-request",
                "sequence": 4,
                "sender": "human-operator",
                "recipient": "kimi-k3",
                "route_profile_id": "kimi-old-route",
                "route_profile_sha256": "d" * 64,
                "route_receipt_sha256": "e" * 64,
                "route_evaluation": {
                    "status": "verified",
                    "request": {"route_profile_id": "kimi-old-route"},
                    "observed": {
                        "agent_id": "kimi-k3",
                        "provider_id": "old-provider",
                        "model_id": "old-model",
                    },
                },
            },
        ),
    )

    assert contract["observed_route"] is None
    assert contract["observed_route_source"] is None
    assert contract["route_evidence"] is None
    assert contract["model_id"] == "kimi-k3-requested"
    assert contract["model_source"] == "room-seat-request"


def test_room_contract_isolates_same_agent_by_room_and_exposes_exact_delivery_failure() -> None:
    member = {
        "agent_id": "grok-relay",
        "room_session_id": "room-a-session",
        "route_profile_id": "grok-route",
        "status": "active",
    }
    room_a = native_room_session_contract(
        scope="project",
        room_id="room-a",
        room_name="Room A",
        member=member,
        operator_active=True,
        messages=(
            {
                "room_id": "room-a",
                "message_id": "message-a",
                "sequence": 10,
                "sender": "human-operator",
                "recipient": "grok-relay",
                "subject": "Question A",
                "body": "Only room A",
                "created_utc": "2026-08-20T00:00:00Z",
            },
            {
                "room_id": "room-b",
                "message_id": "message-b-leak",
                "sequence": 11,
                "sender": "grok-relay",
                "recipient": "human-operator",
                "body": "Must not enter room A",
                "created_utc": "2026-08-20T00:00:30Z",
            },
        ),
        dispatches=(
            {
                "message_id": "message-a",
                "agent_id": "grok-relay",
                "status": "failed",
                "error_code": "provider_http_retryable",
                "attempt_count": 5,
                "updated_utc": "2026-08-20T00:00:05Z",
            },
        ),
    )
    room_b = native_room_session_contract(
        scope="project",
        room_id="room-b",
        room_name="Room B",
        member={**member, "room_session_id": "room-b-session"},
        operator_active=True,
        messages=(
            {
                "room_id": "room-b",
                "message_id": "message-b",
                "sequence": 11,
                "sender": "grok-relay",
                "recipient": "human-operator",
                "subject": "Answer B",
                "body": "Only room B",
                "created_utc": "2026-08-20T00:01:00Z",
            },
        ),
    )

    assert room_a["session_id"] != room_b["session_id"]
    assert room_a["capabilities"]["input_capable"] is True
    assert room_b["capabilities"]["input_capable"] is True
    assert [event["text"] for event in room_a["events"]] == [
        "Only room A",
        "Delivery to grok-relay: failed (provider_http_retryable), attempts=5",
    ]
    assert [event["text"] for event in room_b["events"]] == ["Only room B"]
    assert room_a["events"][-1]["kind"] == "error"


def test_room_contract_replaces_unknown_upstream_error_with_safe_code() -> None:
    contract = native_room_session_contract(
        scope="project",
        room_id="room-a",
        member={
            "agent_id": "grok-relay",
            "room_session_id": "room-a-session",
            "route_profile_id": "grok-route",
        },
        messages=(
            {
                "room_id": "room-a",
                "message_id": "message-a",
                "sequence": 1,
                "sender": "human-operator",
                "recipient": "grok-relay",
                "body": "Review this.",
            },
        ),
        dispatches=(
            {
                "message_id": "message-a",
                "agent_id": "grok-relay",
                "status": "failed",
                "error_code": "upstream Authorization: Bearer must-not-leak",
            },
        ),
    )

    rendered = "\n".join(str(event["text"]) for event in contract["events"])
    assert "dispatch_failed" in rendered
    assert "Bearer" not in rendered


def test_managed_contract_preserves_existing_panel_id_and_claims_only_owned_control() -> None:
    contract = managed_cli_session_contract(
        {
            "session_id": "managed-one",
            "agent_id": "codex",
            "state": "running",
            "input_submitted": False,
            "events": [],
        }
    )
    assert contract["session_id"] == "managed-one"
    assert contract["source_type"] == "managed-cli"
    assert contract["capabilities"]["input_capable"] is True
    assert contract["capabilities"]["terminal_controllable"] is True
    assert contract["capabilities"]["context_resumable"] is False

    after_input = managed_cli_session_contract(
        {**contract, "state": "running", "input_submitted": True}
    )
    assert after_input["capabilities"]["input_capable"] is False
    assert after_input["capabilities"]["terminal_controllable"] is True

    completed = managed_cli_session_contract(
        {**contract, "state": "completed", "input_submitted": True}
    )
    assert completed["capabilities"]["input_capable"] is False
    assert completed["capabilities"]["terminal_controllable"] is False


def test_managed_contract_keeps_persistent_official_session_input_capable() -> None:
    contract = managed_cli_session_contract(
        {
            "session_id": "managed-persistent-one",
            "agent_id": "codex",
            "state": "running",
            "input_submitted": True,
            "can_submit_input": True,
            "session_contract": {
                "mode": "persistent",
                "input_transport": "codex-app-server-jsonrpc",
                "additional_input_supported": True,
                "resume_supported": True,
                "process_terminal_after_turn": False,
            },
            "events": [],
        }
    )

    assert contract["capabilities"]["input_capable"] is True
    assert contract["capabilities"]["context_resumable"] is True
    assert contract["capabilities"]["terminal_controllable"] is True


def test_contract_rejects_missing_capabilities_and_mismatched_identity() -> None:
    with pytest.raises(SessionContractError, match="capability contract"):
        normalize_session_contract(
            {
                "source_type": "authorized-desktop",
                "source_session_id": "desktop-one",
                "capabilities": {},
            }
        )
    with pytest.raises(SessionContractError, match="does not match"):
        normalize_session_contract(
            {
                "session_id": "wrong",
                "source_type": "authorized-desktop",
                "source_session_id": "desktop-one",
                "capabilities": {key: False for key in SESSION_CAPABILITIES},
            }
        )
    assert session_panel_id("authorized-desktop", "desktop-one") == (
        "authorized-desktop:desktop-one"
    )


def test_linked_room_session_target_requires_one_exact_live_adapter() -> None:
    sessions = [
        {
            "source_type": "authorized-desktop",
            "source_session_id": "desktop-one",
            "owner_agent_id": "reviewer-one",
            "room_session_id": "room-session-one",
            "state": "running",
        },
        {
            "source_type": "authorized-terminal",
            "source_session_id": "terminal-other",
            "owner_agent_id": "other-agent",
            "room_session_id": "room-session-other",
            "state": "running",
        },
    ]
    assert linked_room_session_target(
        sessions,
        agent_id="reviewer-one",
        room_session_id="room-session-one",
    ) == ("authorized-desktop", "desktop-one")
    assert (
        linked_room_session_target(
            [
                sessions[0],
                {
                    **sessions[0],
                    "source_type": "authorized-terminal",
                    "source_session_id": "terminal-two",
                },
            ],
            agent_id="reviewer-one",
            room_session_id="room-session-one",
        )
        is None
    )
