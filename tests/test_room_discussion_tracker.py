from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from peerbridge_mcp.bridge import (
    Bridge,
    governance_operation_payload,
    stable_sha256,
)
from peerbridge_mcp.guided_room_workflows import (
    guided_room_workflow_plan,
    validate_guided_room_start,
)
from peerbridge_mcp.operation_queue import DurableOperationQueue
from peerbridge_mcp.room_discussion_tracker import (
    ROOM_DISCUSSION_RECEIPT_SCHEMA,
    ROOM_DISCUSSION_TERMINAL_OUTCOME,
    RoomDiscussionTracker,
)


def _bridge(
    root: Path,
    agent_id: str,
    *,
    provider_id: str | None = None,
    model_id: str | None = None,
    discussion_coordinator: bool = False,
) -> Bridge:
    return Bridge(
        root,
        root / ".peerbridge" / "tracker.sqlite3",
        agent_id,
        "tracker-test",
        provider_id=provider_id,
        model_id=model_id,
        reasoning_mode="high" if provider_id else None,
        route_class="relay" if provider_id else None,
        discussion_coordinator=discussion_coordinator,
    )


def _setup_room(
    root: Path,
    *,
    task_id: str,
    post: bool = True,
) -> tuple[
    Bridge,
    Bridge,
    tuple[Bridge, Bridge],
    Bridge,
    dict[str, object],
    dict[str, object] | None,
]:
    human = _bridge(root, "human-operator")
    human.create_room({"room_id": "guided-room", "name": "Guided Room"})
    workers: list[Bridge] = []
    for agent_id, role_id in (("alpha", "researcher"), ("beta", "reviewer")):
        route_id = f"{agent_id}-route"
        provider_id = f"relay-{agent_id}"
        model_id = f"model-{agent_id}"
        human.upsert_route_profile(
            {
                "route_id": route_id,
                "agent_id": agent_id,
                "provider_id": provider_id,
                "model_id": model_id,
                "reasoning_mode": "high",
                "route_class": "relay",
            }
        )
        human.join_room(
            {
                "room_id": "guided-room",
                "agent_id": agent_id,
                "route_profile_id": route_id,
                "role_id": role_id,
            }
        )
        workers.append(
            _bridge(
                root,
                agent_id,
                provider_id=provider_id,
                model_id=model_id,
            )
        )
    members = human.room_members({"room_id": "guided-room"})["members"]
    plan = guided_room_workflow_plan(
        room_id="guided-room",
        task_id=task_id,
        task_text="Compare the current observable workflow without modifying files.",
        members=members,
    )
    DurableOperationQueue(human).enqueue(
        operation_id=str(plan["operation_id"]),
        workflow_id=str(plan["workflow_id"]),
        task_text=str(plan["operation_task_text"]),
        working_directory=str(plan["operation_working_directory"]),
        resource_key=str(plan["operation_resource_key"]),
        max_attempts=int(plan["operation_max_attempts"]),
        timeout_seconds=int(plan["operation_timeout_seconds"]),
        not_before_epoch=0.0,
    )
    posted = None
    if post:
        human.set_room_automation(
            {
                "room_id": "guided-room",
                "mode": "discussion",
                "max_rounds": plan["max_rounds"],
                "max_messages": plan["max_messages"],
                "stagnation_rounds": plan["stagnation_rounds"],
            }
        )
        posted = human.post_room_message(
            {
                "room_id": "guided-room",
                "task_id": task_id,
                "subject": plan["subject"],
                "body": plan["body"],
                "priority": plan["priority"],
            }
        )
        validate_guided_room_start(
            plan,
            posted,
            members=human.room_members({"room_id": "guided-room"})["members"],
        )
        bound = DurableOperationQueue(human).bind_guided_discussion(
            str(plan["operation_id"]), str(posted["discussion_id"])
        )
        plan["bound_discussion_id"] = bound["bound_discussion_id"]
    service = _bridge(root, "control-room-workflow")
    coordinator = _bridge(
        root, "mailbox-supervisor", discussion_coordinator=True
    )
    return human, service, (workers[0], workers[1]), coordinator, plan, posted


def _claim_prompt(worker: Bridge) -> dict[str, object]:
    with sqlite3.connect(worker.db_path) as connection:
        route_id = connection.execute(
            """SELECT route_profile_id FROM room_memberships
                WHERE scope=? AND room_id='guided-room' AND agent_id=?""",
            (worker.scope, worker.agent_id),
        ).fetchone()[0]
    return worker.claim_message_dispatch(
        {
            "room_id": "guided-room",
            "require_route": True,
            "route_profile_id": route_id,
        }
    )


def _trust_prompt_result(worker: Bridge, claim: dict[str, object], body: str) -> str:
    message = claim["message"]
    assert isinstance(message, dict)
    assistant_message = {"role": "assistant", "content": body}
    receipt = {
        "schema": "peerbridge.openai-compatible-run.v1",
        "route": {
            "route_profile_id": message.get("route_profile_id"),
            "route_profile_sha256": message.get("route_profile_sha256"),
            "route_class": worker.route_class,
            "provider_id": worker.provider_id,
            "model_id": worker.model_id,
            "response_model_id": worker.model_id,
            "reasoning_mode": worker.reasoning_mode,
            "connection_id": None,
            "connection_sha256": None,
        },
        "room_id": message["room_id"],
        "session_id": worker.session_id,
        "message_id_sha256": stable_sha256(message["message_id"]),
        "output_message_sha256": stable_sha256(assistant_message),
    }
    receipt["receipt_sha256"] = stable_sha256(receipt)
    worker.record_trusted_inference_receipt(
        {
            "message_id": message["message_id"],
            "lease_token": claim["lease_token"],
            "body": body,
            "receipt": receipt,
            "assistant_message": assistant_message,
            "execution_route_profile_id": message.get("route_profile_id"),
            "execution_route_profile_sha256": message.get("route_profile_sha256"),
        }
    )
    return str(receipt["receipt_sha256"])


def _complete_prompt(worker: Bridge) -> None:
    claim = _claim_prompt(worker)
    assert claim["claimed"] is True
    message_id = str(claim["message"]["message_id"])
    body = (
        f"Observable contribution from {worker.agent_id}.\n\n"
        "PEERBRIDGE_SIGNAL: CONSENSUS"
    )
    receipt_sha = _trust_prompt_result(worker, claim, body)
    worker.complete_message_dispatch(
        {
            "message_id": message_id,
            "lease_token": claim["lease_token"],
            "body": body,
            "inference_receipt_sha256": receipt_sha,
        }
    )


def _replace_operation_prompt_binding(
    queue: DurableOperationQueue, operation_id: str
) -> None:
    with queue.bridge._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
            (queue.scope, operation_id),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["task_text"]))
        payload["prompt_sha256"] = "f" * 64
        connection.execute(
            "UPDATE governance_operations SET task_text=? WHERE scope=? AND operation_id=?",
            (
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                queue.scope,
                operation_id,
            ),
        )
        changed = connection.execute(
            "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
            (queue.scope, operation_id),
        ).fetchone()
        assert changed is not None
        connection.execute(
            "UPDATE governance_operations SET operation_sha256=? "
            "WHERE scope=? AND operation_id=?",
            (
                stable_sha256(governance_operation_payload(changed)),
                queue.scope,
                operation_id,
            ),
        )


def test_tracker_completes_existing_room_discussion_with_durable_evidence(
    tmp_path: Path,
) -> None:
    human, service, workers, coordinator, plan, posted = _setup_room(
        tmp_path, task_id="guided-complete"
    )
    assert posted is not None
    for worker in workers:
        _complete_prompt(worker)
    advanced = coordinator.advance_discussions({"room_id": "guided-room"})
    assert advanced["advanced"][0]["status"] == "completed"

    tracker = RoomDiscussionTracker(service, human_bridge=human)
    claim = tracker.queue.claim(
        "room-test-worker", operation_class="room-discussion"
    )
    assert claim is not None
    tracker._track_claim(claim)

    operation = tracker.queue.get_operation(str(plan["operation_id"]))
    receipt = json.loads(str(operation["terminal_detail"]))
    assert operation["status"] == "succeeded"
    assert operation["terminal_outcome"] == ROOM_DISCUSSION_TERMINAL_OUTCOME
    assert receipt["schema"] == ROOM_DISCUSSION_RECEIPT_SCHEMA
    assert receipt["discussion_id"] == posted["discussion_id"]
    assert receipt["source_binding_sha256"] == plan["source_binding_sha256"]


def test_tracker_cancellation_stops_only_the_bound_discussion(
    tmp_path: Path,
) -> None:
    human, service, _workers, _coordinator, plan, posted = _setup_room(
        tmp_path, task_id="guided-cancel"
    )
    assert posted is not None
    tracker = RoomDiscussionTracker(service, human_bridge=human)
    claim = tracker.queue.claim(
        "room-cancel-worker", operation_class="room-discussion"
    )
    assert claim is not None
    DurableOperationQueue(human).request_cancel(
        str(plan["operation_id"]), reason="Operator cancelled the guided workflow."
    )

    tracker._track_claim(claim)

    operation = tracker.queue.get_operation(str(plan["operation_id"]))
    assert operation["status"] == "cancelled"
    with service._connect() as connection:
        discussion = connection.execute(
            "SELECT status, stop_reason FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
    assert dict(discussion) == {
        "status": "stopped",
        "stop_reason": "human_stopped",
    }


def test_tracker_timeout_stops_bound_discussion_and_fails_without_retry(
    tmp_path: Path,
) -> None:
    human, service, _workers, _coordinator, plan, posted = _setup_room(
        tmp_path, task_id="guided-timeout"
    )
    assert posted is not None
    queue = DurableOperationQueue(service)
    first = queue.claim(
        "room-timeout-one",
        now_epoch=100.0,
        lease_seconds=600,
        operation_class="room-discussion",
    )
    assert first is not None
    tracker = RoomDiscussionTracker(
        service,
        human_bridge=human,
        clock=lambda: 699.96,
    )
    tracker.queue = queue
    tracker._track_claim(first)
    operation = queue.get_operation(str(plan["operation_id"]))
    assert operation["status"] == "failed"
    assert operation["attempt_count"] == 1
    assert operation["terminal_outcome"] == "timeout"
    assert (
        queue.claim(
            "room-timeout-two",
            now_epoch=700.0,
            lease_seconds=600,
            operation_class="room-discussion",
        )
        is None
    )
    with service._connect() as connection:
        discussion = connection.execute(
            "SELECT status, stop_reason FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
    assert dict(discussion) == {
        "status": "stopped",
        "stop_reason": "human_stopped",
    }


def test_background_reconcile_stops_guided_timeout_without_retry(
    tmp_path: Path,
) -> None:
    _human, service, _workers, _coordinator, plan, posted = _setup_room(
        tmp_path, task_id="guided-reconcile-timeout"
    )
    assert posted is not None
    queue = DurableOperationQueue(service)
    claim = queue.claim(
        "room-reconcile-worker",
        now_epoch=100.0,
        lease_seconds=600,
        operation_class="room-discussion",
    )
    assert claim is not None

    reconciled = queue.reconcile(now_epoch=700.0)

    assert len(reconciled) == 1
    assert reconciled[0]["status"] == "failed"
    assert reconciled[0]["attempt_count"] == 1
    assert reconciled[0]["terminal_outcome"] == "timeout"
    assert queue.claim(
        "room-retry-worker",
        now_epoch=700.0,
        operation_class="room-discussion",
    ) is None
    with service._connect() as connection:
        discussion = connection.execute(
            "SELECT status, stop_reason FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
    assert dict(discussion) == {
        "status": "stopped",
        "stop_reason": "workflow_timeout",
    }


def test_cancelled_guided_worker_loss_stops_bound_discussion(tmp_path: Path) -> None:
    human, service, _workers, _coordinator, plan, posted = _setup_room(
        tmp_path, task_id="guided-cancelled-worker-loss"
    )
    assert posted is not None
    queue = DurableOperationQueue(service)
    claim = queue.claim(
        "room-cancelled-worker",
        now_epoch=100.0,
        lease_seconds=5,
        operation_class="room-discussion",
    )
    assert claim is not None
    DurableOperationQueue(human).request_cancel(
        str(plan["operation_id"]), reason="Cancel before the worker lease expires."
    )

    reconciled = queue.reconcile(now_epoch=105.0)

    assert reconciled[0]["status"] == "cancelled"
    assert reconciled[0]["terminal_outcome"] == "cancelled_after_worker_loss"
    with service._connect() as connection:
        discussion = connection.execute(
            "SELECT status, stop_reason FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
    assert dict(discussion) == {
        "status": "stopped",
        "stop_reason": "workflow_cancelled",
    }


def test_second_guided_worker_loss_stops_bound_discussion(tmp_path: Path) -> None:
    _human, service, _workers, _coordinator, plan, posted = _setup_room(
        tmp_path, task_id="guided-exhausted-worker-loss"
    )
    assert posted is not None
    queue = DurableOperationQueue(service)
    first = queue.claim(
        "room-worker-one",
        now_epoch=100.0,
        lease_seconds=5,
        operation_class="room-discussion",
    )
    assert first is not None
    assert queue.reconcile(now_epoch=105.0)[0]["status"] == "retry"
    with service._connect() as connection:
        first_status = connection.execute(
            "SELECT status FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()[0]
    assert first_status == "active"

    second = queue.claim(
        "room-worker-two",
        now_epoch=105.0,
        lease_seconds=5,
        operation_class="room-discussion",
    )
    assert second is not None
    failed = queue.reconcile(now_epoch=110.0)[0]

    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 2
    assert failed["terminal_outcome"] == "worker_lost"
    with service._connect() as connection:
        discussion = connection.execute(
            "SELECT status, stop_reason FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
    assert dict(discussion) == {
        "status": "stopped",
        "stop_reason": "workflow_worker_lost",
    }


def test_timeout_cleanup_ignores_bound_terminal_history(tmp_path: Path) -> None:
    human, service, _workers, _coordinator, plan, first = _setup_room(
        tmp_path, task_id="guided-terminal-history"
    )
    assert first is not None
    second = human.post_room_message(
        {
            "room_id": "guided-room",
            "task_id": "guided-terminal-history",
            "subject": plan["subject"],
            "body": plan["body"],
            "priority": plan["priority"],
        }
    )
    queue = DurableOperationQueue(service)
    claim = queue.claim(
        "room-history-worker",
        now_epoch=100.0,
        lease_seconds=600,
        operation_class="room-discussion",
    )
    assert claim is not None

    failed = queue.reconcile(now_epoch=700.0)[0]

    assert failed["status"] == "failed"
    assert failed["terminal_outcome"] == "timeout"
    with service._connect() as connection:
        rows = connection.execute(
            """SELECT discussion_id, status, stop_reason FROM room_discussions
                WHERE task_id='guided-terminal-history' ORDER BY created_utc, discussion_id"""
        ).fetchall()
    observed = {row["discussion_id"]: (row["status"], row["stop_reason"]) for row in rows}
    assert observed[first["discussion_id"]] == ("stopped", "superseded_by_new_post")
    assert observed[second["discussion_id"]] == ("active", None)


def test_timeout_source_mismatch_fences_exact_persisted_discussion(
    tmp_path: Path,
) -> None:
    _human, service, workers, _coordinator, plan, posted = _setup_room(
        tmp_path, task_id="guided-timeout-source-mismatch"
    )
    assert posted is not None
    queue = DurableOperationQueue(service)
    claim = queue.claim(
        "room-source-mismatch-worker",
        now_epoch=100.0,
        lease_seconds=600,
        operation_class="room-discussion",
    )
    assert claim is not None
    prompt_claim = _claim_prompt(workers[0])
    assert prompt_claim["claimed"] is True
    _replace_operation_prompt_binding(queue, str(plan["operation_id"]))

    failed = queue.reconcile(now_epoch=700.0)[0]

    assert failed["status"] == "failed"
    assert failed["terminal_outcome"] == "timeout"
    assert "exact persisted discussion" in str(failed["terminal_detail"])
    with service._connect() as connection:
        discussion = connection.execute(
            "SELECT status, stop_reason FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
        dispatches = connection.execute(
            """SELECT status, error_code FROM message_dispatches
                WHERE message_id IN (
                    SELECT message_id FROM messages WHERE discussion_id=?
                ) ORDER BY agent_id""",
            (posted["discussion_id"],),
        ).fetchall()
    assert dict(discussion) == {
        "status": "stopped",
        "stop_reason": "workflow_timeout",
    }
    assert dispatches
    assert all(
        dict(row) == {
            "status": "failed",
            "error_code": "discussion_timed_out",
        }
        for row in dispatches
    )
    blocked_claim = _claim_prompt(workers[1])
    assert blocked_claim["claimed"] is False
    assert blocked_claim["message"] is None
    assert blocked_claim["dispatch"] is None


@pytest.mark.parametrize("terminal_state", ["completed", "stopped"])
def test_timeout_source_mismatch_preserves_terminal_discussion(
    tmp_path: Path, terminal_state: str
) -> None:
    human, service, workers, coordinator, plan, posted = _setup_room(
        tmp_path, task_id=f"guided-preserve-{terminal_state}"
    )
    assert posted is not None
    queue = DurableOperationQueue(service)
    claim = queue.claim(
        f"room-preserve-{terminal_state}-worker",
        now_epoch=100.0,
        lease_seconds=600,
        operation_class="room-discussion",
    )
    assert claim is not None
    if terminal_state == "completed":
        for worker in workers:
            _complete_prompt(worker)
        advanced = coordinator.advance_discussions({"room_id": "guided-room"})
        assert advanced["advanced"][0]["status"] == "completed"
    else:
        stopped = human.control_discussion(
            {"discussion_id": posted["discussion_id"], "action": "stop"}
        )
        assert stopped["status"] == "stopped"
    with service._connect() as connection:
        before = dict(
            connection.execute(
                """SELECT status, stop_reason, discussion_sha256
                    FROM room_discussions WHERE discussion_id=?""",
                (posted["discussion_id"],),
            ).fetchone()
        )
    _replace_operation_prompt_binding(queue, str(plan["operation_id"]))

    failed = queue.reconcile(now_epoch=700.0)[0]

    assert failed["status"] == "failed"
    assert failed["terminal_outcome"] == "timeout"
    with service._connect() as connection:
        after = dict(
            connection.execute(
                """SELECT status, stop_reason, discussion_sha256
                    FROM room_discussions WHERE discussion_id=?""",
                (posted["discussion_id"],),
            ).fetchone()
        )
    assert after == before


def test_stale_tracker_stop_preserves_completed_discussion(tmp_path: Path) -> None:
    human, service, workers, coordinator, plan, posted = _setup_room(
        tmp_path, task_id="guided-stop-race"
    )
    assert posted is not None
    tracker = RoomDiscussionTracker(service, human_bridge=human)
    stale = tracker._bound_discussion_snapshot(plan)
    assert stale is not None and stale["status"] == "active"

    for worker in workers:
        _complete_prompt(worker)
    advanced = coordinator.advance_discussions({"room_id": "guided-room"})
    assert advanced["advanced"][0]["status"] == "completed"
    completed = tracker._bound_discussion_snapshot(plan)
    assert completed is not None

    tracker._stop_bound_discussion(plan, stale, trigger="timeout race")

    preserved = tracker._bound_discussion_snapshot(plan)
    assert preserved is not None
    assert preserved["status"] == "completed"
    assert preserved["discussion_sha256"] == completed["discussion_sha256"]


def test_tracker_source_drift_stops_only_the_bound_discussion(
    tmp_path: Path,
) -> None:
    human, service, _workers, _coordinator, plan, posted = _setup_room(
        tmp_path, task_id="guided-source-drift"
    )
    assert posted is not None
    tracker = RoomDiscussionTracker(service, human_bridge=human)
    claim = tracker.queue.claim(
        "room-source-drift-worker", operation_class="room-discussion"
    )
    assert claim is not None
    human.set_room_member_role(
        {
            "room_id": "guided-room",
            "agent_id": "alpha",
            "role_id": "implementer",
        }
    )

    tracker._track_wrapper(claim)

    operation = tracker.queue.get_operation(str(plan["operation_id"]))
    assert operation["status"] == "failed"
    assert operation["terminal_outcome"] == "source-binding"
    with service._connect() as connection:
        discussion = connection.execute(
            "SELECT status, stop_reason FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
    assert dict(discussion) == {
        "status": "stopped",
        "stop_reason": "human_stopped",
    }


def test_tracker_reclaims_after_worker_loss_and_reuses_the_same_room_agents(
    tmp_path: Path,
) -> None:
    human, service, workers, coordinator, plan, _posted = _setup_room(
        tmp_path, task_id="guided-restart"
    )
    queue = DurableOperationQueue(service)
    lost = queue.claim(
        "room-lost-worker",
        now_epoch=100.0,
        lease_seconds=5,
        operation_class="room-discussion",
    )
    assert lost is not None
    reconciled = queue.reconcile(now_epoch=105.0)
    assert reconciled[0]["status"] == "retry"
    resumed = queue.claim(
        "room-resumed-worker",
        now_epoch=105.0,
        lease_seconds=30,
        operation_class="room-discussion",
    )
    assert resumed is not None

    for worker in workers:
        _complete_prompt(worker)
    coordinator.advance_discussions({"room_id": "guided-room"})
    tracker = RoomDiscussionTracker(
        service,
        human_bridge=human,
        clock=lambda: 106.0,
    )
    tracker.queue = queue
    tracker._track_claim(resumed)

    operation = queue.get_operation(str(plan["operation_id"]))
    assert operation["status"] == "succeeded"
    assert operation["attempt_count"] == 2
    assert operation["terminal_outcome"] == ROOM_DISCUSSION_TERMINAL_OUTCOME
