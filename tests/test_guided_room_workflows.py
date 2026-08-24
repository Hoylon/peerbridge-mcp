from __future__ import annotations

import pytest

from peerbridge_mcp.bridge import stable_sha256
from peerbridge_mcp.guided_room_workflows import (
    GuidedRoomWorkflowError,
    guided_plan_from_operation,
    guided_room_readiness,
    guided_room_workflow_plan,
    validate_guided_room_source,
    validate_guided_room_start,
)


def _member(
    agent_id: str,
    *,
    role_id: str,
    route_profile_id: str | None = None,
) -> dict[str, object]:
    route_id = route_profile_id or f"route-{agent_id}"
    return {
        "agent_id": agent_id,
        "status": "active",
        "room_session_id": f"session-{agent_id}",
        "membership_sha256": stable_sha256(
            {"agent_id": agent_id, "kind": "membership"}
        ),
        "route_profile_id": route_id,
        "route_profile_sha256": stable_sha256(
            {"agent_id": agent_id, "route_profile_id": route_id}
        ),
        "role_id": role_id,
        "role_label": None,
    }


def test_guided_workflow_reuses_exact_room_roles_routes_and_registered_template() -> None:
    plan = guided_room_workflow_plan(
        room_id="alpha-room",
        task_id="guided-task-one",
        task_text="Compare the README against the current user flow.",
        members=(
            _member("reviewer-one", role_id="reviewer"),
            _member("researcher-one", role_id="researcher"),
            {
                "agent_id": "human-operator",
                "status": "active",
                "room_session_id": "human-room-session",
                "route_profile_id": None,
                "role_id": "equal-participant",
            },
        ),
    )

    assert plan["workflow_id"] == "investigate-debate"
    assert plan["workflow_label"] == "Investigate + Debate"
    assert plan["automation_mode"] == "discussion"
    assert plan["max_rounds"] == 2
    assert plan["participant_count"] == 2
    assert [row["agent_id"] for row in plan["participants"]] == [
        "researcher-one",
        "reviewer-one",
    ]
    assert "researcher-one: researcher" in plan["body"]
    assert "reviewer-one: reviewer" in plan["body"]
    assert "Do not modify project files" in plan["body"]
    assert "never hidden reasoning" in plan["body"]
    assert len(plan["source_binding_sha256"]) == 64
    assert plan["operation_id"].startswith("guided-room:")
    assert plan["operation_resource_key"] == (
        "room-discussion:" + plan["source_binding_sha256"]
    )
    assert plan["operation_max_attempts"] == 2
    assert plan["operation_timeout_seconds"] == 600

    rebuilt = guided_plan_from_operation(
        {
            "workflow_id": plan["workflow_id"],
            "operation_id": plan["operation_id"],
            "task_text": plan["operation_task_text"],
            "working_directory": plan["operation_working_directory"],
            "resource_key": plan["operation_resource_key"],
            "max_attempts": plan["operation_max_attempts"],
            "timeout_seconds": plan["operation_timeout_seconds"],
        }
    )
    assert rebuilt["source_binding_sha256"] == plan["source_binding_sha256"]


def test_guided_readiness_explains_missing_agents_routes_and_invalid_bindings() -> None:
    one = _member("one", role_id="reviewer")
    assert guided_room_readiness((one,)) == {
        "ready": False,
        "code": "need_agents",
        "participant_count": 1,
        "missing_route_count": 0,
        "maximum_participants": 12,
    }

    unrouted = _member("two", role_id="researcher")
    unrouted["route_profile_id"] = None
    missing = guided_room_readiness((one, unrouted))
    assert missing["code"] == "need_routes"
    assert missing["missing_route_count"] == 1

    invalid = _member("two", role_id="researcher")
    invalid["membership_sha256"] = "invalid"
    assert guided_room_readiness((one, invalid))["code"] == "incomplete_binding"

    ready = guided_room_readiness((one, _member("two", role_id="researcher")))
    assert ready["ready"] is True
    assert ready["code"] == "ready"
    assert ready["participant_count"] == 2


def test_guided_start_requires_the_exact_existing_room_routes() -> None:
    members = (
        _member("one", role_id="reviewer"),
        _member("two", role_id="researcher"),
    )
    plan = guided_room_workflow_plan(
        room_id="alpha-room",
        task_id="guided-task-binding",
        task_text="Compare observable evidence.",
        members=members,
    )
    receipt = {
        "automation_mode": "discussion",
        "room_id": "alpha-room",
        "task_id": "guided-task-binding",
        "discussion_id": "discussion-one",
        "discussion_sha256": "a" * 64,
        "fanout_count": 2,
        "recipients": [
            {
                "agent_id": participant["agent_id"],
                "route_profile_id": participant["route_profile_id"],
                "route_profile_sha256": participant["route_profile_sha256"],
            }
            for participant in plan["participants"]
        ],
    }

    assert (
        validate_guided_room_start(plan, receipt, members=members)[
            "participant_count"
        ]
        == 2
    )
    receipt["recipients"][1]["route_profile_id"] = "changed-route"
    with pytest.raises(GuidedRoomWorkflowError, match="routes changed"):
        validate_guided_room_start(plan, receipt, members=members)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("membership_sha256", "f" * 64),
        ("route_profile_sha256", "e" * 64),
        ("role_id", "implementer"),
    ),
)
def test_guided_source_rejects_live_membership_route_or_role_drift(
    field: str, replacement: str
) -> None:
    members = [
        _member("one", role_id="reviewer"),
        _member("two", role_id="researcher"),
    ]
    plan = guided_room_workflow_plan(
        room_id="alpha-room",
        task_id="guided-task-source-drift",
        task_text="Compare observable evidence.",
        members=members,
    )
    changed = [dict(member) for member in members]
    changed[0][field] = replacement

    with pytest.raises(GuidedRoomWorkflowError, match="membership, role, or complete route"):
        validate_guided_room_source(plan, changed)


def test_guided_workflow_fails_closed_without_two_routed_room_agents() -> None:
    with pytest.raises(GuidedRoomWorkflowError, match="at least two"):
        guided_room_workflow_plan(
            room_id="alpha-room",
            task_id="guided-task-one",
            task_text="Inspect only.",
            members=(_member("one", role_id="reviewer"),),
        )
    unrouted = _member("two", role_id="researcher")
    unrouted["route_profile_id"] = None
    with pytest.raises(GuidedRoomWorkflowError, match="no bound model route"):
        guided_room_workflow_plan(
            room_id="alpha-room",
            task_id="guided-task-two",
            task_text="Inspect only.",
            members=(
                _member("one", role_id="reviewer"),
                unrouted,
            ),
        )


def test_guided_workflow_rejects_secret_bearing_tasks() -> None:
    with pytest.raises(GuidedRoomWorkflowError, match="credential"):
        guided_room_workflow_plan(
            room_id="alpha-room",
            task_id="guided-task-secret",
            task_text="api_key=realistic-secret-value-123",
            members=(
                _member("one", role_id="reviewer"),
                _member("two", role_id="researcher"),
            ),
        )
