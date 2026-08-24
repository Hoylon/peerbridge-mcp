"""Bounded background triggers for visible local verification work."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from .bridge import CONTROL_ROOM_WORKFLOW_ID, Bridge, stable_sha256, utc_now
from .execution_governance import ExecutionGovernance, GovernanceError
from .operation_queue import (
    DurableOperationQueue,
    OperationCapacityError,
    OperationQueueError,
)
from .release_gate import ReleaseGateError, ReleaseGateService
from .secret_scan import redact_secrets
from .trust_timeline import TrustTimeline, TrustTimelineError


@dataclass(frozen=True)
class VerificationTrigger:
    kind: str
    subject_id: str
    source_sha256: str
    task_text: str


class VerificationTriggerEngine:
    """Scan local authority state without depending on Page 9 visibility."""

    def __init__(
        self,
        bridge: Bridge,
        *,
        poll_seconds: float = 15.0,
        max_new_operations_per_scan: int = 3,
        max_open_automatic_operations: int = 8,
        max_candidates_per_scan: int = 100,
    ) -> None:
        if bridge.agent_id != CONTROL_ROOM_WORKFLOW_ID:
            raise ValueError("verification engine requires control-room-workflow identity")
        self.bridge = bridge
        self.queue = DurableOperationQueue(bridge)
        self.governance = ExecutionGovernance(bridge)
        self.timeline = TrustTimeline(bridge)
        self.poll_seconds = max(1.0, min(float(poll_seconds), 300.0))
        self.max_new_operations_per_scan = max(
            1, min(int(max_new_operations_per_scan), 20)
        )
        self.max_open_automatic_operations = max(
            1, min(int(max_open_automatic_operations), 100)
        )
        self.max_candidates_per_scan = max(
            1, min(int(max_candidates_per_scan), 500)
        )
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._scan_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._kind_cursor = 0
        self._row_cursors = {
            "permission": 0,
            "execution": 0,
            "trust": 0,
            "failed": 0,
        }
        self._status: dict[str, Any] = {
            "state": "not-started",
            "scan_count": 0,
            "last_scan_utc": None,
            "last_error": None,
            "last_counts": {},
            "manual_scan_requested": False,
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._stop.is_set():
            raise RuntimeError("verification engine is closed")
        with self._status_lock:
            self._status["state"] = "starting"
        self._thread = threading.Thread(
            target=self._run,
            name="peerbridge-verification-triggers",
            daemon=True,
        )
        self._thread.start()

    def close(self, *, wait_seconds: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, min(float(wait_seconds), 30.0)))
        alive = bool(thread is not None and thread.is_alive())
        with self._status_lock:
            self._status["state"] = "stop-timeout" if alive else "stopped"
            if alive:
                self._status["last_error"] = (
                    "verification engine did not stop within the bounded wait"
                )

    def request_scan(self) -> None:
        with self._status_lock:
            self._status["manual_scan_requested"] = True
        self._wake.set()

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            result = dict(self._status)
            result["last_counts"] = dict(self._status.get("last_counts") or {})
        result["thread_alive"] = bool(
            self._thread is not None and self._thread.is_alive()
        )
        return result

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.scan_once()
                self._wake.wait(self.poll_seconds)
                self._wake.clear()
        finally:
            with self._status_lock:
                self._status["state"] = "stopped"

    @staticmethod
    def _trigger_id(trigger: VerificationTrigger, scope: str) -> tuple[str, str]:
        digest = stable_sha256(
            {
                "schema": "peerbridge.verification-trigger.v1",
                "scope": scope,
                "kind": trigger.kind,
                "subject_id": trigger.subject_id,
                "source_sha256": trigger.source_sha256,
            }
        )
        return f"auto:{trigger.kind}:{digest}", digest

    def _ensure_trigger(
        self, trigger: VerificationTrigger
    ) -> tuple[dict[str, Any], bool]:
        operation_id, digest = self._trigger_id(trigger, self.bridge.scope)
        return self.queue.ensure(
            operation_id=operation_id,
            workflow_id="read-only-audit",
            task_text=trigger.task_text,
            working_directory=".",
            resource_key=f"verify:{trigger.kind}:{digest}",
            requested_by=CONTROL_ROOM_WORKFLOW_ID,
            max_attempts=1,
            timeout_seconds=900,
            not_before_epoch=0.0,
            max_open_operations=self.max_open_automatic_operations,
        )

    def _permission_triggers(
        self, now_epoch: float, *, limit: int
    ) -> list[VerificationTrigger]:
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT rowid AS source_rowid, decision_id FROM permission_decisions
                    WHERE scope=? AND decision='allow' AND consumed_utc IS NULL
                      AND expires_epoch>? AND rowid>?
                    ORDER BY rowid LIMIT ?""",
                (
                    self.bridge.scope,
                    now_epoch,
                    self._row_cursors["permission"],
                    limit,
                ),
            ).fetchall()
            if not rows and self._row_cursors["permission"]:
                self._row_cursors["permission"] = 0
                rows = connection.execute(
                    """SELECT rowid AS source_rowid, decision_id FROM permission_decisions
                        WHERE scope=? AND decision='allow' AND consumed_utc IS NULL
                          AND expires_epoch>?
                        ORDER BY rowid LIMIT ?""",
                    (self.bridge.scope, now_epoch, limit),
                ).fetchall()
        if rows:
            self._row_cursors["permission"] = int(rows[-1]["source_rowid"])
        result = []
        for row in rows:
            decision = self.governance.inspect_permission(
                str(row["decision_id"]), now_epoch=now_epoch
            )
            if not decision["pending_sensitive_work"]:
                continue
            result.append(
                VerificationTrigger(
                    kind="permission",
                    subject_id=str(decision["decision_id"]),
                    source_sha256=str(decision["decision_sha256"]),
                    task_text=(
                        "Verify the pending permission-sensitive action "
                        f"{decision['action']} for task {decision['task_id']}. Check the "
                        "exact decision and resource only; do not execute the action."
                    ),
                )
            )
        return result

    def _execution_triggers(self, *, limit: int) -> list[VerificationTrigger]:
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT rowid AS source_rowid, binding_id, binding_sha256
                    FROM execution_bindings
                    WHERE scope=? AND state IN ('active', 'sealed') AND rowid>?
                    ORDER BY rowid LIMIT ?""",
                (self.bridge.scope, self._row_cursors["execution"], limit),
            ).fetchall()
            if not rows and self._row_cursors["execution"]:
                self._row_cursors["execution"] = 0
                rows = connection.execute(
                    """SELECT rowid AS source_rowid, binding_id, binding_sha256
                        FROM execution_bindings
                        WHERE scope=? AND state IN ('active', 'sealed')
                        ORDER BY rowid LIMIT ?""",
                    (self.bridge.scope, limit),
                ).fetchall()
        if rows:
            self._row_cursors["execution"] = int(rows[-1]["source_rowid"])
        result = []
        for row in rows:
            binding_id = str(row["binding_id"])
            try:
                verified = self.governance.verify_execution_source(binding_id)
                if not verified["stale"]:
                    continue
                source_sha = stable_sha256(
                    {
                        "binding_sha256": str(verified["binding_sha256"]),
                        "expected_commit_id": str(verified["expected_commit_id"]),
                        "live_commit_id": str(verified["live_commit_id"]),
                        "expected_diff_sha256": str(
                            verified["expected_diff_sha256"]
                        ),
                        "live_diff_sha256": str(verified["live_diff_sha256"]),
                    }
                )
                task_text = (
                    f"Recheck stale execution binding {binding_id} for task "
                    f"{verified['task_id']}. Inspect only; do not apply or merge changes."
                )
            except (GovernanceError, OSError):
                source_sha = str(row["binding_sha256"])
                task_text = (
                    f"Execution binding {binding_id} failed its bounded source check. "
                    "Inspect integrity and source availability only; do not modify it."
                )
            result.append(
                VerificationTrigger(
                    kind="execution",
                    subject_id=binding_id,
                    source_sha256=source_sha,
                    task_text=task_text,
                )
            )
        return result

    def _trust_triggers(self, *, limit: int) -> list[VerificationTrigger]:
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT rowid AS source_rowid, task_id, record_id
                    FROM trust_records WHERE scope=? AND rowid>?
                    ORDER BY rowid LIMIT ?""",
                (self.bridge.scope, self._row_cursors["trust"], limit),
            ).fetchall()
            if not rows and self._row_cursors["trust"]:
                self._row_cursors["trust"] = 0
                rows = connection.execute(
                    """SELECT rowid AS source_rowid, task_id, record_id
                        FROM trust_records WHERE scope=?
                        ORDER BY rowid LIMIT ?""",
                    (self.bridge.scope, limit),
                ).fetchall()
        if rows:
            self._row_cursors["trust"] = int(rows[-1]["source_rowid"])
        result = []
        timelines: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            task_id = str(row["task_id"])
            record_id = str(row["record_id"])
            try:
                if task_id not in timelines:
                    timelines[task_id] = self.timeline.timeline(task_id)
                timeline = timelines[task_id]
                record = next(
                    (
                        item
                        for item in timeline
                        if str(item.get("record_id") or "") == record_id
                    ),
                    None,
                )
                if record is None:
                    raise TrustTimelineError("trust record disappeared during verification")
                if not record["stale"]:
                    continue
                source_sha = stable_sha256(
                    {
                        "trust_sha256": str(record["trust_sha256"]),
                        "live_bindings": [
                            {
                                "path": str(binding["path"]),
                                "bytes": int(binding["bytes"]),
                                "sha256": str(binding["sha256"]),
                                "stale_reason": binding.get("stale_reason"),
                                "live_bytes": binding.get("live_bytes"),
                                "live_sha256": binding.get("live_sha256"),
                            }
                            for binding in record["source_bindings"]
                        ],
                    }
                )
                task_text = (
                    f"Recheck stale Trust Timeline record {record_id} for task {task_id} "
                    "against its exact source. Record findings only; do not approve work."
                )
            except TrustTimelineError:
                source_sha = stable_sha256(
                    {
                        "scope": self.bridge.scope,
                        "task_id": task_id,
                        "record_id": record_id,
                    }
                )
                task_text = (
                    f"Trust Timeline integrity verification failed for record {record_id} "
                    f"in task {task_id}. "
                    "Inspect the bounded local records only; do not approve work."
                )
            result.append(
                VerificationTrigger(
                    kind="trust",
                    subject_id=record_id,
                    source_sha256=source_sha,
                    task_text=task_text,
                )
            )
        return result

    def _failed_operation_triggers(self, *, limit: int) -> list[VerificationTrigger]:
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT rowid AS source_rowid, operation_id
                    FROM governance_operations
                    WHERE scope=? AND status='failed' AND requested_by!=?
                      AND workflow_id!='release-gate' AND rowid>?
                    ORDER BY rowid LIMIT ?""",
                (
                    self.bridge.scope,
                    CONTROL_ROOM_WORKFLOW_ID,
                    self._row_cursors["failed"],
                    limit,
                ),
            ).fetchall()
            if not rows and self._row_cursors["failed"]:
                self._row_cursors["failed"] = 0
                rows = connection.execute(
                    """SELECT rowid AS source_rowid, operation_id
                        FROM governance_operations
                        WHERE scope=? AND status='failed' AND requested_by!=?
                          AND workflow_id!='release-gate'
                        ORDER BY rowid LIMIT ?""",
                    (self.bridge.scope, CONTROL_ROOM_WORKFLOW_ID, limit),
                ).fetchall()
        if rows:
            self._row_cursors["failed"] = int(rows[-1]["source_rowid"])
        result = []
        for row in rows:
            operation = self.queue.get_operation(str(row["operation_id"]))
            operation_id = str(operation["operation_id"])
            result.append(
                VerificationTrigger(
                    kind="failed",
                    subject_id=operation_id,
                    source_sha256=str(operation["operation_sha256"]),
                    task_text=(
                        f"Inspect failed workflow operation {operation_id} and its exact "
                        "terminal receipt. Report the failure only; do not retry or execute it."
                    ),
                )
            )
        return result

    def _open_automatic_count(self) -> int:
        with self.bridge._connect() as connection:
            return int(
                connection.execute(
                    """SELECT COUNT(*) FROM governance_operations
                        WHERE scope=? AND requested_by=?
                          AND status NOT IN ('succeeded', 'failed', 'cancelled')""",
                    (self.bridge.scope, CONTROL_ROOM_WORKFLOW_ID),
                ).fetchone()[0]
            )

    def _candidates(self, now_epoch: float) -> list[VerificationTrigger]:
        kinds = ("permission", "execution", "trust", "failed")
        start = self._kind_cursor % len(kinds)
        ordered = (*kinds[start:], *kinds[:start])
        self._kind_cursor = (start + 1) % len(kinds)
        base, extra = divmod(self.max_candidates_per_scan, len(kinds))
        groups: dict[str, list[VerificationTrigger]] = {}
        for index, kind in enumerate(ordered):
            limit = base + (1 if index < extra else 0)
            if limit <= 0:
                groups[kind] = []
            elif kind == "permission":
                groups[kind] = self._permission_triggers(now_epoch, limit=limit)
            elif kind == "execution":
                groups[kind] = self._execution_triggers(limit=limit)
            elif kind == "trust":
                groups[kind] = self._trust_triggers(limit=limit)
            else:
                groups[kind] = self._failed_operation_triggers(limit=limit)
        candidates = []
        for row_index in range(max((len(groups[kind]) for kind in ordered), default=0)):
            for kind in ordered:
                if row_index < len(groups[kind]):
                    candidates.append(groups[kind][row_index])
        return candidates

    def scan_once(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        if not self._scan_lock.acquire(blocking=False):
            return self.status()
        now = time.time() if now_epoch is None else float(now_epoch)
        counts: dict[str, Any] = {
            "materialized_release_requests": 0,
            "materialized_schedules": 0,
            "reconciled_operations": 0,
            "candidates": 0,
            "created": 0,
            "already_present": 0,
            "open_automatic": 0,
            "capacity_blocked": 0,
            "scan_budget_blocked": 0,
        }
        error: str | None = None
        try:
            counts["materialized_release_requests"] = len(
                ReleaseGateService(self.bridge).materialize_pending_requests(limit=20)
            )
            counts["materialized_schedules"] = len(
                self.queue.materialize_due_schedules(now_epoch=now, limit=20)
            )
            counts["reconciled_operations"] = len(
                self.queue.reconcile(now_epoch=now)
            )
            open_auto = self._open_automatic_count()
            counts["open_automatic"] = open_auto
            candidates = self._candidates(now)
            counts["candidates"] = len(candidates)
            for index, trigger in enumerate(candidates):
                if counts["created"] >= self.max_new_operations_per_scan:
                    counts["scan_budget_blocked"] += 1
                    continue
                try:
                    _operation, created = self._ensure_trigger(trigger)
                except OperationCapacityError:
                    counts["capacity_blocked"] += len(candidates) - index
                    break
                if created:
                    counts["created"] += 1
                else:
                    counts["already_present"] += 1
            counts["open_automatic"] = self._open_automatic_count()
        except (
            GovernanceError,
            OperationQueueError,
            ReleaseGateError,
            TrustTimelineError,
            OSError,
        ) as exc:
            error = redact_secrets(str(exc))[:500]
        except Exception as exc:
            error = redact_secrets(str(exc))[:500]
        finally:
            with self._status_lock:
                self._status.update(
                    {
                        "state": "error" if error else "running",
                        "scan_count": int(self._status["scan_count"]) + 1,
                        "last_scan_utc": utc_now(),
                        "last_error": error,
                        "last_counts": counts,
                        "manual_scan_requested": False,
                    }
                )
            self._scan_lock.release()
        return self.status()


__all__ = [
    "VerificationTrigger",
    "VerificationTriggerEngine",
]
