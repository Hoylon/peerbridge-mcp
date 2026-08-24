from __future__ import annotations

import sqlite3
import threading

import pytest

from peerbridge_mcp.bridge import (
    CONTROL_ROOM_WORKFLOW_ID,
    GOVERNANCE_OPERATION_PAYLOAD_FIELDS,
    Bridge,
    BridgeError,
    SCHEMA_VERSION,
    stable_sha256,
)
from peerbridge_mcp.operation_queue import (
    DurableOperationQueue,
    OperationCapacityError,
    OperationQueueError,
    WORKFLOW_TEMPLATES,
)


def _queue(tmp_path, *, agent_id: str = "human-operator") -> DurableOperationQueue:
    bridge = Bridge(
        tmp_path,
        tmp_path / ".peerbridge" / "queue.sqlite3",
        agent_id,
        "queue-test",
    )
    return DurableOperationQueue(bridge)


def _enqueue(
    queue: DurableOperationQueue,
    operation_id: str,
    *,
    resource_key: str = "repo:main",
    max_attempts: int = 3,
    timeout_seconds: int = 30,
) -> dict[str, object]:
    return queue.enqueue(
        workflow_id="read-only-audit",
        task_text=f"Audit operation {operation_id}",
        working_directory=".",
        resource_key=resource_key,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        not_before_epoch=1_000,
        operation_id=operation_id,
    )


def _remove_attempt_deadline_column(
    queue: DurableOperationQueue, operation_id: str, *, tampered: bool = False
) -> None:
    with sqlite3.connect(queue.bridge.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
            (queue.scope, operation_id),
        ).fetchone()
        assert row is not None
        legacy_payload = {
            key: row[key]
            for key in GOVERNANCE_OPERATION_PAYLOAD_FIELDS
            if key != "attempt_deadline_epoch"
        }
        legacy_sha256 = "f" * 64 if tampered else stable_sha256(legacy_payload)
        connection.execute(
            "UPDATE governance_operations SET operation_sha256=? "
            "WHERE scope=? AND operation_id=?",
            (legacy_sha256, queue.scope, operation_id),
        )
        connection.execute(
            "ALTER TABLE governance_operations DROP COLUMN attempt_deadline_epoch"
        )


def _remove_bound_discussion_column(
    queue: DurableOperationQueue, operation_id: str
) -> None:
    with sqlite3.connect(queue.bridge.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
            (queue.scope, operation_id),
        ).fetchone()
        assert row is not None
        legacy_payload = {
            key: row[key]
            for key in GOVERNANCE_OPERATION_PAYLOAD_FIELDS
            if key != "bound_discussion_id"
        }
        connection.execute(
            "UPDATE governance_operations SET operation_sha256=? "
            "WHERE scope=? AND operation_id=?",
            (stable_sha256(legacy_payload), queue.scope, operation_id),
        )
        connection.execute(
            "DROP INDEX IF EXISTS idx_governance_operations_discussion"
        )
        connection.execute(
            "ALTER TABLE governance_operations DROP COLUMN bound_discussion_id"
        )
        connection.execute(
            "UPDATE metadata SET value='20' WHERE key='schema_version'"
        )


def test_current_schema_adds_governed_execution_and_room_role_tables(tmp_path) -> None:
    queue = _queue(tmp_path)
    assert int(SCHEMA_VERSION) >= 22
    with queue.bridge._connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
    assert version == SCHEMA_VERSION
    assert {
        "governance_operations",
        "workflow_schedules",
        "capability_registry",
        "capability_grants",
        "permission_decisions",
        "execution_bindings",
        "room_member_roles",
        "authorized_sessions",
        "authorized_session_events",
    }.issubset(tables)


def test_schema_20_repairs_legacy_operation_deadline_column(tmp_path) -> None:
    queue = _queue(tmp_path)
    original = _enqueue(queue, "legacy-deadline")
    _remove_attempt_deadline_column(queue, "legacy-deadline")

    migrated_bridge = Bridge(
        tmp_path,
        queue.bridge.db_path,
        "human-operator",
        queue.scope,
    )
    migrated = DurableOperationQueue(migrated_bridge).get_operation("legacy-deadline")

    assert migrated["attempt_deadline_epoch"] is None
    assert migrated["task_text"] == original["task_text"]
    with migrated_bridge._connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(governance_operations)")
        }
        stored = connection.execute(
            "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
            (queue.scope, "legacy-deadline"),
        ).fetchone()
        assert stored is not None
        assert stored["operation_sha256"] == stable_sha256(
            {key: stored[key] for key in GOVERNANCE_OPERATION_PAYLOAD_FIELDS}
        )
    assert "attempt_deadline_epoch" in columns


def test_schema_20_adds_bound_discussion_before_creating_its_index(tmp_path) -> None:
    queue = _queue(tmp_path)
    original = _enqueue(queue, "legacy-bound-discussion")
    _remove_bound_discussion_column(queue, "legacy-bound-discussion")

    migrated_bridge = Bridge(
        tmp_path,
        queue.bridge.db_path,
        "human-operator",
        queue.scope,
    )
    migrated = DurableOperationQueue(migrated_bridge).get_operation(
        "legacy-bound-discussion"
    )

    assert migrated["bound_discussion_id"] is None
    assert migrated["task_text"] == original["task_text"]
    with migrated_bridge._connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(governance_operations)")
        }
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(governance_operations)")
        }
        stored = connection.execute(
            "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
            (queue.scope, "legacy-bound-discussion"),
        ).fetchone()
        assert stored is not None
        assert stored["operation_sha256"] == stable_sha256(
            {key: stored[key] for key in GOVERNANCE_OPERATION_PAYLOAD_FIELDS}
        )
    assert "bound_discussion_id" in columns
    assert "idx_governance_operations_discussion" in indexes


def test_schema_20_rejects_tampered_legacy_governance_operation(tmp_path) -> None:
    queue = _queue(tmp_path)
    _enqueue(queue, "tampered-legacy-deadline")
    _remove_attempt_deadline_column(
        queue, "tampered-legacy-deadline", tampered=True
    )

    with pytest.raises(
        BridgeError, match="legacy governance operation SHA-256 does not match"
    ):
        Bridge(
            tmp_path,
            queue.bridge.db_path,
            "human-operator",
            queue.scope,
        )


def test_workflow_templates_cover_the_approved_first_success_paths(tmp_path) -> None:
    queue = _queue(tmp_path)
    assert set(WORKFLOW_TEMPLATES) == {
        "implement-review",
        "investigate-debate",
        "read-only-audit",
        "release-gate",
    }
    assert all(template["same_source_review"] for template in queue.templates())
    assert WORKFLOW_TEMPLATES["implement-review"]["automatic_retry"] is False
    assert all(
        WORKFLOW_TEMPLATES[workflow_id]["automatic_retry"] is True
        for workflow_id in (
            "investigate-debate",
            "read-only-audit",
            "release-gate",
        )
    )


def test_claim_serializes_resource_and_terminal_outcome_is_exactly_once(tmp_path) -> None:
    queue = _queue(tmp_path)
    _enqueue(queue, "one")
    _enqueue(queue, "two")

    first = queue.claim("worker-a", now_epoch=1_000, lease_seconds=10)
    assert first is not None
    assert first.operation["operation_id"] == "one"
    assert queue.claim("worker-b", now_epoch=1_001, lease_seconds=10) is None

    complete = queue.complete(
        "one",
        "worker-a",
        first.lease_token,
        outcome="verified",
        now_epoch=1_001,
    )
    assert complete["status"] == "succeeded"
    assert complete["terminal_outcome"] == "verified"
    with pytest.raises(OperationQueueError, match="running worker"):
        queue.complete("one", "worker-a", first.lease_token, now_epoch=1_001)

    second = queue.claim("worker-b", now_epoch=1_002, lease_seconds=10)
    assert second is not None
    assert second.operation["operation_id"] == "two"


def test_wrong_lease_token_and_identity_fail_closed(tmp_path) -> None:
    queue = _queue(tmp_path)
    _enqueue(queue, "lease")
    claim = queue.claim("worker-a", now_epoch=1_000, lease_seconds=10)
    assert claim is not None

    with pytest.raises(OperationQueueError, match="token"):
        queue.heartbeat("lease", "worker-a", "wrong", now_epoch=1_001)
    with pytest.raises(OperationQueueError, match="identity"):
        queue.heartbeat("lease", "worker-b", claim.lease_token, now_epoch=1_001)


def test_transient_failure_retries_then_terminal_failure_is_single(tmp_path) -> None:
    queue = _queue(tmp_path)
    _enqueue(queue, "retry", max_attempts=2)
    first = queue.claim("worker", now_epoch=1_000, lease_seconds=10)
    assert first is not None
    retry = queue.fail(
        "retry",
        "worker",
        first.lease_token,
        error_class="transient",
        detail="Temporary local runtime failure.",
        retry_after_seconds=5,
        now_epoch=1_001,
    )
    assert retry["status"] == "retry"
    assert queue.claim("worker", now_epoch=1_005, lease_seconds=10) is None
    second = queue.claim("worker", now_epoch=1_006, lease_seconds=10)
    assert second is not None
    failed = queue.fail(
        "retry",
        "worker",
        second.lease_token,
        error_class="transient",
        detail="Temporary failure repeated.",
        now_epoch=1_007,
    )
    assert failed["status"] == "failed"
    assert failed["terminal_outcome"] == "transient"


def test_explicit_non_retry_failure_is_terminal_on_the_first_attempt(tmp_path) -> None:
    queue = _queue(tmp_path)
    _enqueue(queue, "no-retry", max_attempts=3)
    claim = queue.claim("worker", now_epoch=1_000, lease_seconds=10)
    assert claim is not None

    failed = queue.fail(
        "no-retry",
        "worker",
        claim.lease_token,
        error_class="timeout",
        detail="A side-effectful operation was stopped at its deadline.",
        allow_retry=False,
        now_epoch=1_001,
    )

    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 1
    assert failed["terminal_outcome"] == "timeout"


def test_timeout_is_a_hard_attempt_deadline_and_reconciles(tmp_path) -> None:
    queue = _queue(tmp_path)
    _enqueue(queue, "timeout", max_attempts=2, timeout_seconds=5)
    first = queue.claim("worker", now_epoch=1_000, lease_seconds=10)
    assert first is not None
    assert first.operation["attempt_deadline_epoch"] == 1_005

    with pytest.raises(OperationQueueError, match="timed out"):
        queue.heartbeat(
            "timeout", "worker", first.lease_token, now_epoch=1_005, lease_seconds=10
        )
    reconciled = queue.reconcile(now_epoch=1_005)
    assert reconciled[0]["status"] == "retry"
    assert "timed out" in str(reconciled[0]["terminal_detail"])

    second = queue.claim("worker", now_epoch=1_005, lease_seconds=10)
    assert second is not None
    failed = queue.reconcile(now_epoch=1_010)[0]
    assert failed["status"] == "failed"
    assert failed["terminal_outcome"] == "timeout"


def test_isolated_write_timeout_worker_loss_and_failure_never_retry(tmp_path) -> None:
    queue = _queue(tmp_path)
    timed = queue.enqueue(
        workflow_id="implement-review",
        task_text="Implement and review one bounded source change.",
        working_directory=".",
        resource_key="repo:write-timeout",
        max_attempts=3,
        timeout_seconds=5,
        not_before_epoch=1_000,
        operation_id="write-timeout",
    )
    assert timed["max_attempts"] == 3
    timed_claim = queue.claim("writer-one", now_epoch=1_000, lease_seconds=10)
    assert timed_claim is not None
    timed_out = queue.reconcile(now_epoch=1_005)[0]
    assert timed_out["status"] == "failed"
    assert timed_out["attempt_count"] == 1
    assert timed_out["terminal_outcome"] == "timeout"
    assert "not automatically retried" in str(timed_out["terminal_detail"])

    lost = queue.enqueue(
        workflow_id="implement-review",
        task_text="Implement a second bounded source change.",
        working_directory=".",
        resource_key="repo:write-worker-loss",
        max_attempts=3,
        timeout_seconds=30,
        not_before_epoch=2_000,
        operation_id="write-worker-loss",
    )
    assert lost["max_attempts"] == 3
    lost_claim = queue.claim("writer-two", now_epoch=2_000, lease_seconds=5)
    assert lost_claim is not None
    worker_lost = queue.reconcile(now_epoch=2_005)[0]
    assert worker_lost["status"] == "failed"
    assert worker_lost["attempt_count"] == 1
    assert worker_lost["terminal_outcome"] == "worker_lost"
    assert "not automatically retried" in str(worker_lost["terminal_detail"])

    reported = queue.enqueue(
        workflow_id="implement-review",
        task_text="Implement a third bounded source change.",
        working_directory=".",
        resource_key="repo:write-reported-failure",
        max_attempts=3,
        timeout_seconds=30,
        not_before_epoch=3_000,
        operation_id="write-reported-failure",
    )
    assert reported["max_attempts"] == 3
    reported_claim = queue.claim("writer-three", now_epoch=3_000, lease_seconds=10)
    assert reported_claim is not None
    failed = queue.fail(
        "write-reported-failure",
        "writer-three",
        reported_claim.lease_token,
        error_class="transient",
        detail="The isolated writer reported a transient failure.",
        allow_retry=True,
        now_epoch=3_001,
    )
    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 1
    assert queue.claim("writer-four", now_epoch=3_100) is None


def test_persisted_isolated_write_retry_is_fenced_before_claim(tmp_path) -> None:
    queue = _queue(tmp_path)
    queue.enqueue(
        workflow_id="implement-review",
        task_text="Preserve a retry record written by the previous build.",
        working_directory=".",
        resource_key="repo:persisted-write-retry",
        max_attempts=3,
        timeout_seconds=30,
        not_before_epoch=1_000,
        operation_id="persisted-write-retry",
    )
    claim = queue.claim("old-writer", now_epoch=1_000, lease_seconds=10)
    assert claim is not None
    with queue.bridge._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE governance_operations
                  SET status='retry', not_before_epoch=1001,
                      lease_owner=NULL, lease_token_sha256=NULL,
                      lease_expires_epoch=NULL, attempt_deadline_epoch=NULL,
                      terminal_detail='Retry persisted by the previous build.',
                      updated_utc=?
                WHERE scope=? AND operation_id=?""",
            (
                "2026-08-20T00:00:00Z",
                queue.scope,
                "persisted-write-retry",
            ),
        )
        queue._rehash(connection, "persisted-write-retry")

    assert queue.claim("new-writer", now_epoch=1_001) is None
    fenced = queue.get_operation("persisted-write-retry")
    assert fenced["status"] == "failed"
    assert fenced["attempt_count"] == 1
    assert fenced["terminal_outcome"] == "retry_disallowed"
    assert "persisted retry was not started" in str(fenced["terminal_detail"])


def test_terminal_transitions_reject_expired_lease_and_hard_deadline(tmp_path) -> None:
    queue = _queue(tmp_path)
    _enqueue(queue, "late-complete", max_attempts=1, timeout_seconds=30)
    complete_claim = queue.claim("worker", now_epoch=1_000, lease_seconds=5)
    assert complete_claim is not None
    with pytest.raises(OperationQueueError, match="lease already expired"):
        queue.complete(
            "late-complete",
            "worker",
            complete_claim.lease_token,
            now_epoch=1_005,
        )

    _enqueue(queue, "late-fail", timeout_seconds=5, resource_key="repo:other")
    fail_claim = queue.claim("worker", now_epoch=2_000, lease_seconds=10)
    assert fail_claim is not None
    with pytest.raises(OperationQueueError, match="attempt already timed out"):
        queue.fail(
            "late-fail",
            "worker",
            fail_claim.lease_token,
            error_class="timeout",
            detail="Worker reported after the hard deadline.",
            now_epoch=2_005,
        )

    cancelling = queue.request_cancel(
        "late-fail", reason="Stop the timed-out operation."
    )
    assert cancelling["status"] == "cancelling"
    with pytest.raises(OperationQueueError, match="attempt already timed out"):
        queue.acknowledge_cancel(
            "late-fail",
            "worker",
            fail_claim.lease_token,
            now_epoch=2_005,
        )


def test_worker_loss_retries_and_cancel_acknowledgement_is_terminal(tmp_path) -> None:
    queue = _queue(tmp_path)
    _enqueue(queue, "worker-loss")
    claim = queue.claim("worker", now_epoch=1_000, lease_seconds=5)
    assert claim is not None
    retry = queue.reconcile(now_epoch=1_005)[0]
    assert retry["status"] == "retry"
    assert "lease expired" in str(retry["terminal_detail"])

    recovered = queue.claim("worker-two", now_epoch=1_005, lease_seconds=10)
    assert recovered is not None
    cancelling = queue.request_cancel(
        "worker-loss", reason="Operator no longer wants this workflow."
    )
    assert cancelling["status"] == "cancelling"
    assert cancelling["cancellation_requested"] is True
    with pytest.raises(OperationQueueError, match="acknowledged"):
        queue.complete(
            "worker-loss",
            "worker-two",
            recovered.lease_token,
            now_epoch=1_006,
        )
    cancelled = queue.acknowledge_cancel(
        "worker-loss",
        "worker-two",
        recovered.lease_token,
        now_epoch=1_006,
    )
    assert cancelled["status"] == "cancelled"


def test_queued_cancel_never_becomes_claimable(tmp_path) -> None:
    queue = _queue(tmp_path)
    _enqueue(queue, "cancelled")
    cancelled = queue.request_cancel("cancelled")
    assert cancelled["status"] == "cancelled"
    assert queue.claim("worker", now_epoch=2_000) is None


def test_opt_in_schedule_materializes_once_and_advances_atomically(tmp_path) -> None:
    queue = _queue(tmp_path)
    schedule = queue.save_schedule(
        workflow_id="release-gate",
        task_text="Run the saved release gate workflow.",
        working_directory=".",
        resource_key="repo:release",
        interval_seconds=60,
        next_run_epoch=1_000,
        enabled=True,
        schedule_id="nightly-release-gate",
    )
    assert schedule["enabled"] is True
    listed = queue.list_schedules(enabled=True)
    assert [item["schedule_id"] for item in listed] == ["nightly-release-gate"]
    assert listed[0]["enabled"] is True
    disabled = queue.set_schedule_enabled("nightly-release-gate", enabled=False)
    assert disabled["enabled"] is False
    assert queue.materialize_due_schedules(now_epoch=1_000) == []
    queue.set_schedule_enabled("nightly-release-gate", enabled=True)

    first = queue.materialize_due_schedules(now_epoch=1_000)
    duplicate = queue.materialize_due_schedules(now_epoch=1_000)
    assert len(first) == 1
    assert duplicate == []
    assert first[0]["operation_id"] == "schedule:nightly-release-gate:1000"
    assert queue.materialize_due_schedules(now_epoch=1_059) == []
    assert len(queue.materialize_due_schedules(now_epoch=1_060)) == 1


def test_only_operator_or_control_room_service_runs_scheduler_transitions(tmp_path) -> None:
    queue = _queue(tmp_path)
    queue.save_schedule(
        workflow_id="read-only-audit",
        task_text="Run the authorized scheduled audit.",
        working_directory=".",
        resource_key="repo:audit",
        interval_seconds=60,
        next_run_epoch=1_000,
        enabled=True,
        schedule_id="authority-test",
    )

    worker = _queue(tmp_path, agent_id="unrelated-agent")
    with pytest.raises(OperationQueueError, match="control-room-workflow"):
        worker.materialize_due_schedules(now_epoch=1_000)
    with pytest.raises(OperationQueueError, match="control-room-workflow"):
        worker.reconcile(now_epoch=1_000)

    service = _queue(tmp_path, agent_id=CONTROL_ROOM_WORKFLOW_ID)
    materialized = service.materialize_due_schedules(now_epoch=1_000)
    assert [item["operation_id"] for item in materialized] == [
        "schedule:authority-test:1000"
    ]
    assert service.reconcile(now_epoch=1_000) == []


def test_release_gate_claim_and_human_mutations_are_bridge_identity_bound(
    tmp_path,
) -> None:
    human = _queue(tmp_path)
    human.enqueue(
        workflow_id="release-gate",
        task_text="Run one source-bound Release Gate.",
        working_directory=".",
        resource_key=f"release:{'a' * 64}",
        operation_id="release-gate:identity-test",
        max_attempts=1,
        timeout_seconds=60,
        not_before_epoch=0.0,
    )
    assert human.claim("spoofed-release-worker", now_epoch=1_000) is None
    service = _queue(tmp_path, agent_id=CONTROL_ROOM_WORKFLOW_ID)
    claim = service.claim("control-room-worker", now_epoch=1_000)
    assert claim is not None
    assert claim.operation["workflow_id"] == "release-gate"

    unrelated = _queue(tmp_path, agent_id="unrelated-agent")
    with pytest.raises(OperationQueueError, match="only human-operator"):
        unrelated.request_cancel(
            claim.operation["operation_id"], requested_by="human-operator"
        )
    with pytest.raises(OperationQueueError, match="only human-operator"):
        unrelated.save_schedule(
            workflow_id="read-only-audit",
            task_text="Do not accept a forged human schedule.",
            working_directory=".",
            resource_key="repo:forged-schedule",
            interval_seconds=60,
            next_run_epoch=2_000,
            enabled=True,
            requested_by="human-operator",
            schedule_id="forged-schedule",
        )

    human.save_schedule(
        workflow_id="read-only-audit",
        task_text="A real operator-owned schedule.",
        working_directory=".",
        resource_key="repo:real-schedule",
        interval_seconds=60,
        next_run_epoch=2_000,
        enabled=True,
        schedule_id="real-schedule",
    )
    with pytest.raises(OperationQueueError, match="only human-operator"):
        unrelated.set_schedule_enabled(
            "real-schedule", enabled=False, requested_by="human-operator"
        )


def test_disabled_schedule_does_not_materialize(tmp_path) -> None:
    queue = _queue(tmp_path)
    queue.save_schedule(
        workflow_id="read-only-audit",
        task_text="Saved but disabled audit.",
        working_directory=".",
        resource_key="repo:audit",
        interval_seconds=60,
        next_run_epoch=1_000,
        enabled=False,
        schedule_id="disabled-audit",
    )
    assert queue.materialize_due_schedules(now_epoch=2_000) == []


def test_queue_rejects_secrets_and_tampered_state(tmp_path) -> None:
    queue = _queue(tmp_path)
    with pytest.raises(OperationQueueError, match="credential-like"):
        queue.enqueue(
            workflow_id="read-only-audit",
            task_text="api_key=realistic-secret-value-123",
            working_directory=".",
            resource_key="repo:main",
        )
    _enqueue(queue, "tampered")
    with sqlite3.connect(queue.bridge.db_path) as connection:
        connection.execute(
            "UPDATE governance_operations SET status='succeeded' WHERE operation_id='tampered'"
        )
    with pytest.raises(OperationQueueError, match="SHA-256"):
        queue.list_operations()


def test_exact_operation_ensure_is_idempotent_and_service_bound(tmp_path) -> None:
    queue = _queue(tmp_path)
    arguments = {
        "operation_id": "exact-source-operation",
        "workflow_id": "read-only-audit",
        "task_text": "Audit one exact source state.",
        "working_directory": ".",
        "resource_key": "verify:source:one",
        "max_attempts": 1,
        "timeout_seconds": 60,
        "not_before_epoch": 0.0,
    }
    first, first_created = queue.ensure(**arguments)
    second, second_created = queue.ensure(**arguments)
    assert first_created is True
    assert second_created is False
    assert second["operation_sha256"] == first["operation_sha256"]
    with pytest.raises(OperationQueueError, match="different source request"):
        queue.ensure(**{**arguments, "task_text": "A different source request."})

    service = _queue(tmp_path, agent_id=CONTROL_ROOM_WORKFLOW_ID)
    automatic, created = service.ensure(
        operation_id="auto:trust:one",
        workflow_id="read-only-audit",
        task_text="Recheck one stale trust record.",
        working_directory=".",
        resource_key="verify:trust:one",
        max_attempts=1,
        timeout_seconds=60,
        not_before_epoch=0.0,
    )
    assert created is True
    assert automatic["requested_by"] == CONTROL_ROOM_WORKFLOW_ID

    unrelated = _queue(tmp_path, agent_id="unrelated-agent")
    with pytest.raises(OperationQueueError, match="not authorized"):
        unrelated.ensure(**arguments)


def test_claim_classes_keep_room_discussions_out_of_managed_cli_runner(
    tmp_path,
) -> None:
    human = _queue(tmp_path)
    _enqueue(
        human,
        "guided-room-operation",
        resource_key="room-discussion:" + "a" * 64,
    )
    _enqueue(human, "managed-operation", resource_key="repo:managed")
    service = _queue(tmp_path, agent_id=CONTROL_ROOM_WORKFLOW_ID)

    managed = service.claim(
        "managed-worker", now_epoch=1_000, operation_class="managed"
    )
    room = service.claim(
        "room-worker", now_epoch=1_000, operation_class="room-discussion"
    )

    assert managed is not None
    assert managed.operation["operation_id"] == "managed-operation"
    assert room is None
    rejected = service.get_operation("guided-room-operation")
    assert rejected["status"] == "failed"
    assert rejected["terminal_outcome"] == "source-binding"
    assert rejected["bound_discussion_id"] is None
    with pytest.raises(OperationQueueError, match="claim class"):
        service.claim(
            "invalid-worker",
            now_epoch=1_000,
            operation_class="unknown",  # type: ignore[arg-type]
        )


def test_automatic_capacity_is_atomic_across_service_instances(tmp_path) -> None:
    queues = [
        _queue(tmp_path, agent_id=CONTROL_ROOM_WORKFLOW_ID),
        _queue(tmp_path, agent_id=CONTROL_ROOM_WORKFLOW_ID),
    ]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create(index: int) -> None:
        barrier.wait()
        try:
            queues[index].ensure(
                operation_id=f"auto:trust:atomic-{index}",
                workflow_id="read-only-audit",
                task_text=f"Verify atomic capacity candidate {index}.",
                working_directory=".",
                resource_key=f"verify:trust:atomic-{index}",
                requested_by=CONTROL_ROOM_WORKFLOW_ID,
                max_attempts=1,
                timeout_seconds=60,
                not_before_epoch=0.0,
                max_open_operations=1,
            )
        except OperationCapacityError:
            outcomes.append("capacity")
        else:
            outcomes.append("created")

    threads = [threading.Thread(target=create, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["capacity", "created"]
    assert len(queues[0].list_operations()) == 1
