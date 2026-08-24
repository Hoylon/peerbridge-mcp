"""Durable, auditable workflow operations for the local Control Room."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .bridge import (
    CONTROL_ROOM_WORKFLOW_ID,
    HUMAN_OPERATOR_ID,
    Bridge,
    BridgeError,
    governance_operation_payload,
    stable_sha256,
    utc_now,
)
from .secret_scan import contains_secret


WORKFLOW_TEMPLATES: dict[str, dict[str, Any]] = {
    "implement-review": {
        "label": "Implement + Review",
        "roles": ("implementer", "reviewer"),
        "session_modes": ("isolated-write", "observe"),
        "same_source_review": True,
        "automatic_retry": False,
    },
    "investigate-debate": {
        "label": "Investigate + Debate",
        "roles": ("investigator", "investigator", "reviewer"),
        "session_modes": ("observe", "observe", "observe"),
        "same_source_review": True,
        "automatic_retry": True,
    },
    "read-only-audit": {
        "label": "Read-only Audit",
        "roles": ("auditor",),
        "session_modes": ("observe",),
        "same_source_review": True,
        "automatic_retry": True,
    },
    "release-gate": {
        "label": "Release Gate",
        "roles": ("auditor", "reviewer"),
        "session_modes": ("observe", "observe"),
        "same_source_review": True,
        "automatic_retry": True,
    },
}
OPERATION_STATUSES = frozenset(
    {"queued", "running", "retry", "cancelling", "succeeded", "failed", "cancelled"}
)
TERMINAL_OPERATION_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
RETRY_CLASSES = frozenset({"transient", "resource", "provider", "timeout"})
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
MAX_TASK_CHARS = 50_000
MAX_OPERATION_ATTEMPTS = 10
MAX_OPERATION_TIMEOUT_SECONDS = 86_400
MAX_OPERATION_LEASE_SECONDS = 3_600
MIN_SCHEDULE_INTERVAL_SECONDS = 60
MAX_SCHEDULE_INTERVAL_SECONDS = 31 * 86_400
RELEASE_GATE_RECEIPT_SCHEMA = "peerbridge.release-gate-verdict-receipt.v1"
RELEASE_GATE_TERMINAL_OUTCOME = "release_gate_verified"
RELEASE_REVIEW_VERDICT_MARKER = "PEERBRIDGE_RELEASE_GATE_VERDICT_V1"
RELEASE_REVIEW_VERDICT_SCHEMA = "peerbridge.release-review-verdict.v1"
ROOM_DISCUSSION_RESOURCE_PREFIX = "room-discussion:"
GUIDED_DISCUSSION_BINDING_GRACE_SECONDS = 30


class OperationQueueError(RuntimeError):
    """A durable operation transition would violate its state contract."""


class OperationCapacityError(OperationQueueError):
    """The atomic open-operation limit is already full."""


def _identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise OperationQueueError(f"{label} is invalid")
    return text


def _safe_text(value: Any, label: str, *, limit: int = MAX_TASK_CHARS) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise OperationQueueError(f"{label} is invalid or too large")
    if contains_secret(text):
        raise OperationQueueError(f"{label} contains credential-like data")
    return text


def _schedule_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "scope",
            "schedule_id",
            "workflow_id",
            "requested_by",
            "task_text",
            "working_directory",
            "resource_key",
            "permission_decision_id",
            "interval_seconds",
            "next_run_epoch",
            "enabled",
            "last_materialized_epoch",
            "created_utc",
            "updated_utc",
        )
    }


@dataclass(frozen=True)
class OperationClaim:
    operation: dict[str, Any]
    lease_token: str


class DurableOperationQueue:
    """State machine over governance tables and the main audit chain."""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self.scope = bridge.scope
        with self.bridge._connect() as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        required = {"governance_operations", "workflow_schedules", "events"}
        if not required.issubset(tables):
            raise OperationQueueError("database does not have the governance schema")

    def _require_service_transition(self, action: str) -> None:
        if self.bridge.agent_id not in {
            HUMAN_OPERATOR_ID,
            CONTROL_ROOM_WORKFLOW_ID,
        }:
            raise OperationQueueError(
                f"only {HUMAN_OPERATOR_ID} or {CONTROL_ROOM_WORKFLOW_ID} may {action}"
            )

    @staticmethod
    def templates() -> tuple[dict[str, Any], ...]:
        return tuple(
            {"workflow_id": workflow_id, **template}
            for workflow_id, template in WORKFLOW_TEMPLATES.items()
        )

    def _row(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
            (self.scope, operation_id),
        ).fetchone()
        if row is None:
            raise OperationQueueError("operation does not exist")
        if stable_sha256(governance_operation_payload(row)) != row["operation_sha256"]:
            raise OperationQueueError("operation SHA-256 does not match its state")
        return row

    @staticmethod
    def _result(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["cancellation_requested"] = bool(result["cancellation_requested"])
        return result

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        operation_id = _identifier(operation_id, "operation id")
        with self.bridge._connect() as connection:
            row = self._row(connection, operation_id)
        return self._result(row)

    def _rehash(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
            (self.scope, operation_id),
        ).fetchone()
        assert row is not None
        digest = stable_sha256(governance_operation_payload(row))
        connection.execute(
            "UPDATE governance_operations SET operation_sha256=? WHERE scope=? AND operation_id=?",
            (digest, self.scope, operation_id),
        )
        row = connection.execute(
            "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
            (self.scope, operation_id),
        ).fetchone()
        assert row is not None
        return row

    def _enqueue_locked(
        self,
        connection: sqlite3.Connection,
        *,
        workflow_id: str,
        task_text: str,
        working_directory: str,
        resource_key: str,
        requested_by: str,
        permission_decision_id: str | None,
        max_attempts: int,
        timeout_seconds: int,
        not_before_epoch: float,
        operation_id: str | None = None,
    ) -> sqlite3.Row:
        workflow_id = _identifier(workflow_id, "workflow id")
        if workflow_id not in WORKFLOW_TEMPLATES:
            raise OperationQueueError("workflow template is not registered")
        task_text = _safe_text(task_text, "task text")
        working_directory = _safe_text(
            working_directory, "working directory", limit=2_000
        )
        resource_key = _identifier(resource_key, "resource key")
        requested_by = _identifier(requested_by, "requester")
        if requested_by not in {HUMAN_OPERATOR_ID, CONTROL_ROOM_WORKFLOW_ID}:
            raise OperationQueueError(
                "only human-operator or control-room-workflow may enqueue local workflows"
            )
        permission_decision_id = (
            _identifier(permission_decision_id, "permission decision id")
            if permission_decision_id
            else None
        )
        max_attempts = int(max_attempts)
        timeout_seconds = int(timeout_seconds)
        if not 1 <= max_attempts <= MAX_OPERATION_ATTEMPTS:
            raise OperationQueueError("operation attempt limit is invalid")
        if not 1 <= timeout_seconds <= MAX_OPERATION_TIMEOUT_SECONDS:
            raise OperationQueueError("operation timeout is invalid")
        operation_id = _identifier(
            operation_id or uuid.uuid4().hex, "operation id"
        )
        created = utc_now()
        payload = {
            "scope": self.scope,
            "operation_id": operation_id,
            "workflow_id": workflow_id,
            "requested_by": requested_by,
            "task_text": task_text,
            "working_directory": working_directory,
            "resource_key": resource_key,
            "permission_decision_id": permission_decision_id,
            "bound_discussion_id": None,
            "status": "queued",
            "attempt_count": 0,
            "max_attempts": max_attempts,
            "timeout_seconds": timeout_seconds,
            "not_before_epoch": float(not_before_epoch),
            "lease_owner": None,
            "lease_token_sha256": None,
            "lease_expires_epoch": None,
            "attempt_deadline_epoch": None,
            "cancellation_requested": 0,
            "terminal_outcome": None,
            "terminal_detail": None,
            "created_utc": created,
            "updated_utc": created,
        }
        digest = stable_sha256(payload)
        try:
            connection.execute(
                """INSERT INTO governance_operations(
                    scope, operation_id, workflow_id, requested_by, task_text,
                    working_directory, resource_key, permission_decision_id,
                    bound_discussion_id, status, attempt_count, max_attempts, timeout_seconds,
                    not_before_epoch, lease_owner, lease_token_sha256,
                    lease_expires_epoch, attempt_deadline_epoch,
                    cancellation_requested, terminal_outcome,
                    terminal_detail, created_utc, updated_utc, operation_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'queued', 0, ?, ?, ?, NULL,
                          NULL, NULL, NULL, 0, NULL, NULL, ?, ?, ?)""",
                (
                    self.scope,
                    operation_id,
                    workflow_id,
                    requested_by,
                    task_text,
                    working_directory,
                    resource_key,
                    permission_decision_id,
                    max_attempts,
                    timeout_seconds,
                    float(not_before_epoch),
                    created,
                    created,
                    digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise OperationQueueError("operation id already exists") from exc
        row = self._row(connection, operation_id)
        self.bridge._event(
            connection,
            "workflow.operation.queued",
            {
                "operation_id": operation_id,
                "workflow_id": workflow_id,
                "resource_key": resource_key,
                "operation_sha256": row["operation_sha256"],
            },
        )
        return row

    def enqueue(
        self,
        *,
        workflow_id: str,
        task_text: str,
        working_directory: str,
        resource_key: str,
        requested_by: str = "human-operator",
        permission_decision_id: str | None = None,
        max_attempts: int = 3,
        timeout_seconds: int = 1_800,
        not_before_epoch: float | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            self.bridge.agent_id != HUMAN_OPERATOR_ID
            or requested_by != HUMAN_OPERATOR_ID
        ):
            raise OperationQueueError("only human-operator may enqueue local workflows")
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._enqueue_locked(
                connection,
                workflow_id=workflow_id,
                task_text=task_text,
                working_directory=working_directory,
                resource_key=resource_key,
                requested_by=requested_by,
                permission_decision_id=permission_decision_id,
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
                not_before_epoch=(
                    time.time() if not_before_epoch is None else float(not_before_epoch)
                ),
                operation_id=operation_id,
            )
        return self._result(row)

    def ensure(
        self,
        *,
        operation_id: str,
        workflow_id: str,
        task_text: str,
        working_directory: str,
        resource_key: str,
        requested_by: str | None = None,
        permission_decision_id: str | None = None,
        max_attempts: int = 1,
        timeout_seconds: int = 1_800,
        not_before_epoch: float = 0.0,
        max_open_operations: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically return one exact operation, creating it only when absent."""

        requester = str(requested_by or self.bridge.agent_id)
        if requester not in {HUMAN_OPERATOR_ID, CONTROL_ROOM_WORKFLOW_ID}:
            raise OperationQueueError("operation requester is not authorized")
        if self.bridge.agent_id != requester:
            raise OperationQueueError("operation requester does not match bridge identity")
        operation_id = _identifier(operation_id, "operation id")
        workflow_id = _identifier(workflow_id, "workflow id")
        if workflow_id not in WORKFLOW_TEMPLATES:
            raise OperationQueueError("workflow template is not registered")
        task_text = _safe_text(task_text, "task text")
        working_directory = _safe_text(
            working_directory, "working directory", limit=2_000
        )
        resource_key = _identifier(resource_key, "resource key")
        permission_decision_id = (
            _identifier(permission_decision_id, "permission decision id")
            if permission_decision_id
            else None
        )
        max_attempts = int(max_attempts)
        timeout_seconds = int(timeout_seconds)
        if not 1 <= max_attempts <= MAX_OPERATION_ATTEMPTS:
            raise OperationQueueError("operation attempt limit is invalid")
        if not 1 <= timeout_seconds <= MAX_OPERATION_TIMEOUT_SECONDS:
            raise OperationQueueError("operation timeout is invalid")
        if max_open_operations is not None:
            if requester != CONTROL_ROOM_WORKFLOW_ID:
                raise OperationQueueError(
                    "atomic operation capacity is reserved for control-room-workflow"
                )
            max_open_operations = int(max_open_operations)
            if not 1 <= max_open_operations <= 10_000:
                raise OperationQueueError("automatic operation capacity is invalid")
        expected = {
            "workflow_id": workflow_id,
            "requested_by": requester,
            "task_text": task_text,
            "working_directory": working_directory,
            "resource_key": resource_key,
            "permission_decision_id": permission_decision_id,
            "max_attempts": max_attempts,
            "timeout_seconds": timeout_seconds,
            "not_before_epoch": float(not_before_epoch),
        }
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM governance_operations WHERE scope=? AND operation_id=?",
                (self.scope, operation_id),
            ).fetchone()
            if existing is not None:
                row = self._row(connection, operation_id)
                if any(row[key] != value for key, value in expected.items()):
                    raise OperationQueueError(
                        "operation id already exists with a different source request"
                    )
                return self._result(row), False
            if max_open_operations is not None:
                open_count = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM governance_operations
                            WHERE scope=? AND requested_by=?
                              AND status NOT IN ('succeeded', 'failed', 'cancelled')""",
                        (self.scope, requester),
                    ).fetchone()[0]
                )
                if open_count >= max_open_operations:
                    raise OperationCapacityError(
                        "automatic operation capacity is already full"
                    )
            row = self._enqueue_locked(
                connection,
                workflow_id=workflow_id,
                task_text=task_text,
                working_directory=working_directory,
                resource_key=resource_key,
                requested_by=requester,
                permission_decision_id=permission_decision_id,
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
                not_before_epoch=float(not_before_epoch),
                operation_id=operation_id,
            )
        return self._result(row), True

    def _set_terminal_locked(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        status: Literal["succeeded", "failed", "cancelled"],
        outcome: str,
        detail: str,
    ) -> sqlite3.Row:
        if row["status"] in TERMINAL_OPERATION_STATUSES:
            raise OperationQueueError("operation already has a terminal outcome")
        updated = utc_now()
        connection.execute(
            """UPDATE governance_operations
                      SET status=?, terminal_outcome=?, terminal_detail=?,
                      lease_owner=NULL, lease_token_sha256=NULL,
                      lease_expires_epoch=NULL, attempt_deadline_epoch=NULL,
                      updated_utc=?
                WHERE scope=? AND operation_id=?""",
            (status, outcome, detail, updated, self.scope, row["operation_id"]),
        )
        terminal = self._rehash(connection, str(row["operation_id"]))
        self.bridge._event(
            connection,
            f"workflow.operation.{status}",
            {
                "operation_id": terminal["operation_id"],
                "workflow_id": terminal["workflow_id"],
                "outcome": outcome,
                "operation_sha256": terminal["operation_sha256"],
            },
        )
        return terminal

    @staticmethod
    def _guided_room_binding(row: sqlite3.Row) -> dict[str, Any] | None:
        resource_key = str(row["resource_key"])
        if not resource_key.startswith(ROOM_DISCUSSION_RESOURCE_PREFIX):
            return None
        try:
            payload = json.loads(str(row["task_text"]))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("schema") != (
            "peerbridge.guided-room-operation.v1"
        ):
            return None
        room_id = str(payload.get("room_id") or "")
        task_id = str(payload.get("task_id") or "")
        source_sha256 = str(payload.get("source_binding_sha256") or "")
        prompt_sha256 = str(payload.get("prompt_sha256") or "")
        participants = payload.get("participants")
        expected_operation_id = "guided-room:" + hashlib.sha256(
            f"{room_id}\0{task_id}".encode("utf-8")
        ).hexdigest()[:40]
        if (
            not SAFE_ID.fullmatch(room_id)
            or not SAFE_ID.fullmatch(task_id)
            or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256)
            or not isinstance(participants, list)
            or not participants
            or resource_key != f"{ROOM_DISCUSSION_RESOURCE_PREFIX}{source_sha256}"
            or str(row["operation_id"]) != expected_operation_id
            or str(row["workflow_id"]) != "investigate-debate"
        ):
            return None
        normalized_participants: list[dict[str, Any]] = []
        seen_agents: set[str] = set()
        for participant in participants:
            if not isinstance(participant, Mapping):
                return None
            agent_id = str(participant.get("agent_id") or "")
            route_profile_id = str(participant.get("route_profile_id") or "")
            route_profile_sha256 = str(
                participant.get("route_profile_sha256") or ""
            )
            if (
                not SAFE_ID.fullmatch(agent_id)
                or not SAFE_ID.fullmatch(route_profile_id)
                or not re.fullmatch(r"[0-9a-f]{64}", route_profile_sha256)
                or agent_id in seen_agents
            ):
                return None
            seen_agents.add(agent_id)
            normalized_participants.append(dict(participant))
        return {
            "room_id": room_id,
            "task_id": task_id,
            "prompt_sha256": prompt_sha256,
            "participants": normalized_participants,
        }

    def _open_guided_task_rows_locked(
        self,
        connection: sqlite3.Connection,
        binding: Mapping[str, Any],
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """SELECT * FROM room_discussions
                WHERE scope=? AND room_id=? AND task_id=?
                  AND status IN ('active', 'paused', 'waiting_human')
                ORDER BY created_utc, discussion_id""",
            (self.scope, binding["room_id"], binding["task_id"]),
        ).fetchall()

    def _stop_guided_rows_locked(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        discussions: list[sqlite3.Row],
        *,
        stop_reason: str,
        dispatch_error_code: str,
        event_type: str,
    ) -> int:
        stopped = 0
        for discussion in discussions:
            if str(discussion["status"]) not in {
                "active",
                "paused",
                "waiting_human",
            }:
                continue
            now = utc_now()
            cancelled = self.bridge._cancel_discussion_dispatches(
                connection,
                str(discussion["discussion_id"]),
                error_code=dispatch_error_code,
                now=now,
            )
            _updated, discussion_sha256 = self.bridge._store_discussion_state(
                connection,
                discussion,
                now=now,
                status="stopped",
                stop_reason=stop_reason,
            )
            self.bridge._event(
                connection,
                event_type,
                {
                    "discussion_id": discussion["discussion_id"],
                    "operation_id": row["operation_id"],
                    "cancelled_dispatch_count": cancelled,
                    "discussion_sha256": discussion_sha256,
                },
                str(discussion["task_id"]),
            )
            stopped += 1
        return stopped

    def _bind_guided_locked(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        binding: Mapping[str, Any],
        discussion: sqlite3.Row,
        *,
        recovery: bool,
    ) -> sqlite3.Row:
        discussion_id = str(discussion["discussion_id"])
        try:
            connection.execute(
                """UPDATE governance_operations
                      SET bound_discussion_id=?, updated_utc=?
                    WHERE scope=? AND operation_id=?
                      AND bound_discussion_id IS NULL""",
                (discussion_id, utc_now(), self.scope, row["operation_id"]),
            )
        except sqlite3.IntegrityError as exc:
            raise OperationQueueError(
                "guided discussion is already bound to another operation"
            ) from exc
        updated = self._rehash(connection, str(row["operation_id"]))
        self.bridge._event(
            connection,
            "workflow.operation.discussion_recovered"
            if recovery
            else "workflow.operation.discussion_bound",
            {
                "operation_id": updated["operation_id"],
                "discussion_id": discussion_id,
                "room_id": binding["room_id"],
                "task_id": binding["task_id"],
                "operation_sha256": updated["operation_sha256"],
            },
            str(binding["task_id"]),
        )
        return updated

    def _fail_unbound_guided_locked(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        detail: str,
        open_rows: list[sqlite3.Row] | None = None,
    ) -> sqlite3.Row:
        stopped = self._stop_guided_rows_locked(
            connection,
            row,
            open_rows or [],
            stop_reason="workflow_source_binding_failed",
            dispatch_error_code="discussion_source_binding_failed",
            event_type="discussion.source_binding_failed",
        )
        suffix = (
            f" Stopped {stopped} open discussion(s) atomically."
            if stopped
            else ""
        )
        return self._set_terminal_locked(
            connection,
            row,
            status="failed",
            outcome="source-binding",
            detail=f"{detail}{suffix}",
        )

    def bind_guided_discussion(
        self, operation_id: str, discussion_id: str
    ) -> dict[str, Any]:
        """Persist the exact validated discussion before a guided worker may claim it."""

        if self.bridge.agent_id != HUMAN_OPERATOR_ID:
            raise OperationQueueError(
                "only human-operator may bind a guided discussion"
            )
        operation_id = _identifier(operation_id, "operation id")
        discussion_id = _identifier(discussion_id, "discussion id")
        failure: str | None = None
        result: sqlite3.Row
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, operation_id)
            existing_id = str(row["bound_discussion_id"] or "")
            if existing_id:
                if existing_id != discussion_id:
                    raise OperationQueueError(
                        "operation is already bound to another discussion"
                    )
                return self._result(row)
            if row["status"] not in {"queued", "retry"}:
                raise OperationQueueError(
                    "guided operation cannot be bound in its current state"
                )
            binding = self._guided_room_binding(row)
            if binding is None:
                result = self._fail_unbound_guided_locked(
                    connection,
                    row,
                    detail="Guided operation binding is malformed.",
                )
                failure = "guided operation binding is malformed"
            else:
                open_rows = self._open_guided_task_rows_locked(connection, binding)
                try:
                    discussion = self.bridge._bound_discussion_for_operation(
                        connection,
                        discussion_id=discussion_id,
                        room_id=str(binding["room_id"]),
                        task_id=str(binding["task_id"]),
                        prompt_sha256=str(binding["prompt_sha256"]),
                        participants=binding["participants"],
                    )
                except BridgeError:
                    discussion = None
                other_open = [
                    candidate
                    for candidate in open_rows
                    if str(candidate["discussion_id"]) != discussion_id
                ]
                if discussion is None or other_open:
                    result = self._fail_unbound_guided_locked(
                        connection,
                        row,
                        detail=(
                            "Guided discussion did not match the exact operation "
                            "prompt, participants, routes, or unique open state."
                        ),
                        open_rows=open_rows,
                    )
                    failure = "guided discussion binding did not match"
                else:
                    result = self._bind_guided_locked(
                        connection,
                        row,
                        binding,
                        discussion,
                        recovery=False,
                    )
        if failure is not None:
            raise OperationQueueError(failure)
        return self._result(result)

    def _recover_unbound_guided_locked(
        self,
        connection: sqlite3.Connection,
        *,
        now_epoch: float,
    ) -> list[sqlite3.Row]:
        reconciled: list[sqlite3.Row] = []
        rows = connection.execute(
            """SELECT * FROM governance_operations
                WHERE scope=? AND bound_discussion_id IS NULL
                  AND resource_key LIKE ? AND status IN ('queued', 'retry')
                ORDER BY created_utc, operation_id""",
            (self.scope, f"{ROOM_DISCUSSION_RESOURCE_PREFIX}%"),
        ).fetchall()
        for original in rows:
            row = self._row(connection, str(original["operation_id"]))
            binding = self._guided_room_binding(row)
            if binding is None:
                reconciled.append(
                    self._fail_unbound_guided_locked(
                        connection,
                        row,
                        detail="Guided operation binding is malformed.",
                    )
                )
                continue
            open_rows = self._open_guided_task_rows_locked(connection, binding)
            try:
                matching = self.bridge._matching_discussions_for_operation(
                    connection,
                    room_id=str(binding["room_id"]),
                    task_id=str(binding["task_id"]),
                    prompt_sha256=str(binding["prompt_sha256"]),
                    participants=binding["participants"],
                )
            except BridgeError:
                matching = []
            matching_ids = {
                str(discussion["discussion_id"]) for discussion in matching
            }
            open_matching = [
                discussion
                for discussion in open_rows
                if str(discussion["discussion_id"]) in matching_ids
            ]
            mismatched_open = [
                discussion
                for discussion in open_rows
                if str(discussion["discussion_id"]) not in matching_ids
            ]
            if mismatched_open or len(open_matching) > 1:
                reconciled.append(
                    self._fail_unbound_guided_locked(
                        connection,
                        row,
                        detail=(
                            "Guided crash recovery found an ambiguous or mismatched "
                            "open discussion."
                        ),
                        open_rows=open_rows,
                    )
                )
                continue
            if len(open_matching) == 1:
                self._bind_guided_locked(
                    connection,
                    row,
                    binding,
                    open_matching[0],
                    recovery=True,
                )
                continue
            if len(matching) == 1:
                self._bind_guided_locked(
                    connection,
                    row,
                    binding,
                    matching[0],
                    recovery=True,
                )
                continue
            if len(matching) > 1:
                reconciled.append(
                    self._fail_unbound_guided_locked(
                        connection,
                        row,
                        detail=(
                            "Guided crash recovery found multiple terminal discussions "
                            "and refused to guess by timestamp or identifier."
                        ),
                    )
                )
                continue
            if now_epoch >= (
                float(row["not_before_epoch"])
                + GUIDED_DISCUSSION_BINDING_GRACE_SECONDS
            ):
                reconciled.append(
                    self._fail_unbound_guided_locked(
                        connection,
                        row,
                        detail=(
                            "Guided operation was never bound to a complete discussion "
                            "before its startup grace period expired."
                        ),
                    )
                )
        return reconciled

    def _stop_terminal_guided_discussion_locked(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        terminal_detail: str,
        stop_reason: str,
        dispatch_error_code: str,
        event_type: str,
    ) -> str:
        discussion_id = str(row["bound_discussion_id"] or "")
        if not SAFE_ID.fullmatch(discussion_id):
            return (
                f"{terminal_detail} No persisted guided discussion ID was "
                "available for cleanup."
            )
        discussion = connection.execute(
            "SELECT * FROM room_discussions WHERE scope=? AND discussion_id=?",
            (self.scope, discussion_id),
        ).fetchone()
        if discussion is None:
            return f"{terminal_detail} No persisted bound discussion was present."

        binding = self._guided_room_binding(row)
        source_binding_revalidated = False
        if binding is not None:
            try:
                verified = self.bridge._bound_discussion_for_operation(
                    connection,
                    discussion_id=discussion_id,
                    room_id=str(binding["room_id"]),
                    task_id=str(binding["task_id"]),
                    prompt_sha256=str(binding["prompt_sha256"]),
                    participants=binding["participants"],
                )
            except BridgeError:
                verified = None
            if verified is not None:
                discussion = verified
                source_binding_revalidated = True
        current_status = str(discussion["status"])
        if current_status not in {"active", "paused", "waiting_human"}:
            return (
                f"{terminal_detail} The bound discussion's preserved state was "
                f"already {current_status}."
            )

        now = utc_now()
        cancelled = self.bridge._cancel_discussion_dispatches(
            connection,
            str(discussion["discussion_id"]),
            error_code=dispatch_error_code,
            now=now,
        )
        _updated, discussion_sha256 = self.bridge._store_discussion_state(
            connection,
            discussion,
            now=now,
            status="stopped",
            stop_reason=stop_reason,
        )
        self.bridge._event(
            connection,
            event_type,
            {
                "discussion_id": discussion["discussion_id"],
                "operation_id": row["operation_id"],
                "cancelled_dispatch_count": cancelled,
                "discussion_sha256": discussion_sha256,
                "cleanup_authority": "persisted-bound-discussion-id",
                "source_binding_revalidated": source_binding_revalidated,
            },
            str(discussion["task_id"]),
        )
        if source_binding_revalidated:
            suffix = "The bound discussion was stopped atomically."
        else:
            suffix = (
                "The exact persisted discussion was fenced and stopped atomically "
                "after full source revalidation failed."
            )
        return f"{terminal_detail} {suffix}"

    def _reconcile_locked(
        self, connection: sqlite3.Connection, *, now_epoch: float
    ) -> list[sqlite3.Row]:
        reconciled: list[sqlite3.Row] = []
        persisted_retries = connection.execute(
            """SELECT * FROM governance_operations
                WHERE scope=? AND status='retry'
                ORDER BY created_utc, operation_id""",
            (self.scope,),
        ).fetchall()
        for original in persisted_retries:
            workflow_id = str(original["workflow_id"])
            if bool(WORKFLOW_TEMPLATES[workflow_id]["automatic_retry"]):
                continue
            row = self._row(connection, str(original["operation_id"]))
            reconciled.append(
                self._set_terminal_locked(
                    connection,
                    row,
                    status="failed",
                    outcome="retry_disallowed",
                    detail=(
                        "A persisted retry was not started because the workflow may "
                        "write to its source. Create a new operator-approved operation "
                        "after inspecting the previous attempt."
                    ),
                )
            )

        rows = connection.execute(
            """SELECT * FROM governance_operations
                WHERE scope=? AND status IN ('running', 'cancelling')
                  AND ((lease_expires_epoch IS NOT NULL AND lease_expires_epoch<=?)
                    OR (attempt_deadline_epoch IS NOT NULL AND attempt_deadline_epoch<=?))
                ORDER BY created_utc, operation_id""",
            (self.scope, float(now_epoch), float(now_epoch)),
        ).fetchall()
        for original in rows:
            row = self._row(connection, str(original["operation_id"]))
            if row["status"] == "cancelling" or row["cancellation_requested"]:
                detail = "Worker lease expired after cancellation was requested."
                if str(row["resource_key"]).startswith(
                    ROOM_DISCUSSION_RESOURCE_PREFIX
                ):
                    detail = self._stop_terminal_guided_discussion_locked(
                        connection,
                        row,
                        terminal_detail=detail,
                        stop_reason="workflow_cancelled",
                        dispatch_error_code="discussion_cancelled",
                        event_type="discussion.cancelled_after_worker_loss",
                    )
                reconciled.append(
                    self._set_terminal_locked(
                        connection,
                        row,
                        status="cancelled",
                        outcome="cancelled_after_worker_loss",
                        detail=detail,
                    )
                )
                continue
            timed_out = float(row["attempt_deadline_epoch"] or 0) <= now_epoch
            if timed_out and str(row["resource_key"]).startswith(
                ROOM_DISCUSSION_RESOURCE_PREFIX
            ):
                reconciled.append(
                    self._set_terminal_locked(
                        connection,
                        row,
                        status="failed",
                        outcome="timeout",
                        detail=self._stop_terminal_guided_discussion_locked(
                            connection,
                            row,
                            terminal_detail=(
                                "Guided discussion exceeded its hard timeout."
                            ),
                            stop_reason="workflow_timeout",
                            dispatch_error_code="discussion_timed_out",
                            event_type="discussion.timed_out",
                        ),
                    )
                )
                continue
            automatic_retry = bool(
                WORKFLOW_TEMPLATES[str(row["workflow_id"])]["automatic_retry"]
            )
            if (
                automatic_retry
                and int(row["attempt_count"]) < int(row["max_attempts"])
            ):
                connection.execute(
                    """UPDATE governance_operations
                          SET status='retry', not_before_epoch=?, lease_owner=NULL,
                              lease_token_sha256=NULL, lease_expires_epoch=NULL,
                              attempt_deadline_epoch=NULL, terminal_detail=?,
                              updated_utc=?
                        WHERE scope=? AND operation_id=?""",
                    (
                        float(now_epoch),
                        (
                            "Operation attempt timed out; retry is eligible."
                            if timed_out
                            else "Worker lease expired; retry is eligible."
                        ),
                        utc_now(),
                        self.scope,
                        row["operation_id"],
                    ),
                )
                retry = self._rehash(connection, str(row["operation_id"]))
                self.bridge._event(
                    connection,
                    (
                        "workflow.operation.timeout_retry"
                        if timed_out
                        else "workflow.operation.reconciled_retry"
                    ),
                    {
                        "operation_id": retry["operation_id"],
                        "attempt_count": retry["attempt_count"],
                        "operation_sha256": retry["operation_sha256"],
                    },
                )
                reconciled.append(retry)
            else:
                detail = (
                    "Operation attempt timed out and the attempt limit was reached."
                    if timed_out
                    else "Worker lease expired and the attempt limit was reached."
                )
                if (
                    not automatic_retry
                    and int(row["attempt_count"]) < int(row["max_attempts"])
                ):
                    detail = (
                        "Operation attempt timed out and was not automatically retried "
                        "because the workflow may write to its source."
                        if timed_out
                        else "Worker lease expired and the operation was not automatically "
                        "retried because the workflow may write to its source."
                    )
                if str(row["resource_key"]).startswith(
                    ROOM_DISCUSSION_RESOURCE_PREFIX
                ):
                    detail = self._stop_terminal_guided_discussion_locked(
                        connection,
                        row,
                        terminal_detail=detail,
                        stop_reason=(
                            "workflow_timeout" if timed_out else "workflow_worker_lost"
                        ),
                        dispatch_error_code=(
                            "discussion_timed_out"
                            if timed_out
                            else "discussion_worker_lost"
                        ),
                        event_type=(
                            "discussion.timed_out"
                            if timed_out
                            else "discussion.worker_lost"
                        ),
                    )
                reconciled.append(
                    self._set_terminal_locked(
                        connection,
                        row,
                        status="failed",
                        outcome="timeout" if timed_out else "worker_lost",
                        detail=detail,
                    )
                )
        return reconciled

    def reconcile(self, *, now_epoch: float | None = None) -> list[dict[str, Any]]:
        self._require_service_transition("reconcile workflows")
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time() if now_epoch is None else float(now_epoch)
            rows = self._recover_unbound_guided_locked(
                connection, now_epoch=now
            )
            rows.extend(self._reconcile_locked(connection, now_epoch=now))
        return [self._result(row) for row in rows]

    def claim(
        self,
        worker_id: str,
        *,
        now_epoch: float | None = None,
        lease_seconds: int = 300,
        operation_class: Literal["any", "managed", "room-discussion"] = "any",
    ) -> OperationClaim | None:
        worker_id = _identifier(worker_id, "worker id")
        lease_seconds = int(lease_seconds)
        if not 1 <= lease_seconds <= MAX_OPERATION_LEASE_SECONDS:
            raise OperationQueueError("operation lease is invalid")
        if operation_class not in {"any", "managed", "room-discussion"}:
            raise OperationQueueError("operation claim class is invalid")
        now = time.time() if now_epoch is None else float(now_epoch)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_unbound_guided_locked(connection, now_epoch=now)
            self._reconcile_locked(connection, now_epoch=now)
            release_filter = (
                ""
                if self.bridge.agent_id == CONTROL_ROOM_WORKFLOW_ID
                else " AND candidate.workflow_id!='release-gate'"
            )
            class_filter = {
                "any": "",
                "managed": " AND candidate.resource_key NOT LIKE ?",
                "room-discussion": " AND candidate.resource_key LIKE ?",
            }[operation_class]
            params: list[Any] = [self.scope, now]
            if operation_class != "any":
                params.append(f"{ROOM_DISCUSSION_RESOURCE_PREFIX}%")
            candidates = connection.execute(
                """SELECT * FROM governance_operations candidate
                    WHERE candidate.scope=?
                      AND candidate.status IN ('queued', 'retry')
                      AND candidate.not_before_epoch<=?
                      AND candidate.cancellation_requested=0"""
                + release_filter
                + class_filter
                + """
                      AND (
                          candidate.resource_key NOT LIKE 'room-discussion:%'
                          OR candidate.bound_discussion_id IS NOT NULL
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM governance_operations active
                           WHERE active.scope=candidate.scope
                             AND active.resource_key=candidate.resource_key
                             AND active.status IN ('running', 'cancelling')
                      )
                    ORDER BY candidate.not_before_epoch, candidate.created_utc,
                             candidate.operation_id LIMIT 1""",
                tuple(params),
            ).fetchone()
            if candidates is None:
                return None
            row = self._row(connection, str(candidates["operation_id"]))
            token = secrets.token_urlsafe(32)
            token_sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
            attempt_deadline = now + int(row["timeout_seconds"])
            connection.execute(
                """UPDATE governance_operations
                      SET status='running', attempt_count=attempt_count+1,
                          lease_owner=?, lease_token_sha256=?, lease_expires_epoch=?,
                          attempt_deadline_epoch=?, terminal_detail=NULL, updated_utc=?
                    WHERE scope=? AND operation_id=?""",
                (
                    worker_id,
                    token_sha,
                    now + lease_seconds,
                    attempt_deadline,
                    utc_now(),
                    self.scope,
                    row["operation_id"],
                ),
            )
            claimed = self._rehash(connection, str(row["operation_id"]))
            self.bridge._event(
                connection,
                "workflow.operation.claimed",
                {
                    "operation_id": claimed["operation_id"],
                    "worker_id": worker_id,
                    "attempt_count": claimed["attempt_count"],
                    "operation_sha256": claimed["operation_sha256"],
                },
            )
        return OperationClaim(self._result(claimed), token)

    @staticmethod
    def _token_sha(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def _require_worker(
        self, row: sqlite3.Row, worker_id: str, lease_token: str
    ) -> None:
        if row["status"] not in {"running", "cancelling"}:
            raise OperationQueueError("operation is not owned by a running worker")
        if row["lease_owner"] != _identifier(worker_id, "worker id"):
            raise OperationQueueError("operation worker identity does not match")
        if not secrets.compare_digest(
            str(row["lease_token_sha256"] or ""), self._token_sha(lease_token)
        ):
            raise OperationQueueError("operation lease token does not match")

    def _require_live_worker(
        self,
        row: sqlite3.Row,
        worker_id: str,
        lease_token: str,
        *,
        now_epoch: float,
    ) -> None:
        self._require_worker(row, worker_id, lease_token)
        if float(row["lease_expires_epoch"] or 0) <= now_epoch:
            raise OperationQueueError("operation lease already expired")
        if float(row["attempt_deadline_epoch"] or 0) <= now_epoch:
            raise OperationQueueError("operation attempt already timed out")

    def heartbeat(
        self,
        operation_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now_epoch: float | None = None,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        operation_id = _identifier(operation_id, "operation id")
        lease_seconds = int(lease_seconds)
        if not 1 <= lease_seconds <= MAX_OPERATION_LEASE_SECONDS:
            raise OperationQueueError("operation lease is invalid")
        now = time.time() if now_epoch is None else float(now_epoch)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, operation_id)
            self._require_live_worker(
                row, worker_id, lease_token, now_epoch=now
            )
            deadline = float(row["attempt_deadline_epoch"] or 0)
            connection.execute(
                """UPDATE governance_operations SET lease_expires_epoch=?, updated_utc=?
                    WHERE scope=? AND operation_id=?""",
                (
                    min(now + lease_seconds, deadline),
                    utc_now(),
                    self.scope,
                    operation_id,
                ),
            )
            updated = self._rehash(connection, operation_id)
        return self._result(updated)

    def complete(
        self,
        operation_id: str,
        worker_id: str,
        lease_token: str,
        *,
        outcome: str = "completed",
        detail: str = "Workflow completed.",
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        operation_id = _identifier(operation_id, "operation id")
        outcome = _identifier(outcome, "terminal outcome")
        detail = _safe_text(detail, "terminal detail", limit=4_000)
        now = time.time() if now_epoch is None else float(now_epoch)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, operation_id)
            self._require_live_worker(
                row, worker_id, lease_token, now_epoch=now
            )
            if row["workflow_id"] == "release-gate":
                if self.bridge.agent_id != CONTROL_ROOM_WORKFLOW_ID:
                    raise OperationQueueError(
                        "only control-room-workflow may complete a release gate"
                    )
                if outcome != RELEASE_GATE_TERMINAL_OUTCOME:
                    raise OperationQueueError(
                        "release gate completion outcome is invalid"
                    )
                try:
                    receipt = json.loads(detail)
                except json.JSONDecodeError as exc:
                    raise OperationQueueError(
                        "release gate completion receipt is invalid"
                    ) from exc
                reviews = receipt.get("reviews") if isinstance(receipt, dict) else None
                fingerprint = str(receipt.get("source_fingerprint") or "") if isinstance(receipt, dict) else ""
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("schema") != RELEASE_GATE_RECEIPT_SCHEMA
                    or receipt.get("operation_id") != operation_id
                    or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                    or row["resource_key"] != f"release:{fingerprint}"
                    or not isinstance(reviews, list)
                    or len(reviews) != 2
                    or {str(review.get("role") or "") for review in reviews if isinstance(review, dict)}
                    != {"auditor", "reviewer"}
                    or any(
                        not isinstance(review, dict)
                        or review.get("decision") != "approve"
                        or not re.fullmatch(
                            r"[0-9a-f]{64}", str(review.get("answer_sha256") or "")
                        )
                        or not str(review.get("session_id") or "")
                        or not str(review.get("agent_id") or "")
                        for review in reviews
                    )
                    or len({str(review["session_id"]) for review in reviews}) != 2
                    or len({str(review["agent_id"]) for review in reviews}) != 2
                ):
                    raise OperationQueueError(
                        "release gate completion receipt is invalid"
                    )
            if row["cancellation_requested"]:
                raise OperationQueueError("operation cancellation must be acknowledged")
            terminal = self._set_terminal_locked(
                connection,
                row,
                status="succeeded",
                outcome=outcome,
                detail=detail,
            )
        return self._result(terminal)

    def fail(
        self,
        operation_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_class: str,
        detail: str,
        retry_after_seconds: int = 15,
        allow_retry: bool = True,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        operation_id = _identifier(operation_id, "operation id")
        error_class = _identifier(error_class, "error class")
        detail = _safe_text(detail, "failure detail", limit=4_000)
        retry_after_seconds = max(0, min(int(retry_after_seconds), 86_400))
        now = time.time() if now_epoch is None else float(now_epoch)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, operation_id)
            self._require_live_worker(
                row, worker_id, lease_token, now_epoch=now
            )
            if row["cancellation_requested"]:
                terminal = self._set_terminal_locked(
                    connection,
                    row,
                    status="cancelled",
                    outcome="cancelled",
                    detail="Worker acknowledged cancellation while handling a failure.",
                )
            elif (
                error_class in RETRY_CLASSES
                and allow_retry
                and bool(
                    WORKFLOW_TEMPLATES[str(row["workflow_id"])]["automatic_retry"]
                )
                and int(row["attempt_count"]) < int(row["max_attempts"])
            ):
                connection.execute(
                    """UPDATE governance_operations
                          SET status='retry', not_before_epoch=?, lease_owner=NULL,
                              lease_token_sha256=NULL, lease_expires_epoch=NULL,
                              attempt_deadline_epoch=NULL,
                              terminal_detail=?, updated_utc=?
                        WHERE scope=? AND operation_id=?""",
                    (
                        now + retry_after_seconds,
                        detail,
                        utc_now(),
                        self.scope,
                        operation_id,
                    ),
                )
                terminal = self._rehash(connection, operation_id)
                self.bridge._event(
                    connection,
                    "workflow.operation.retry_scheduled",
                    {
                        "operation_id": operation_id,
                        "error_class": error_class,
                        "not_before_epoch": terminal["not_before_epoch"],
                        "operation_sha256": terminal["operation_sha256"],
                    },
                )
            else:
                terminal = self._set_terminal_locked(
                    connection,
                    row,
                    status="failed",
                    outcome=error_class,
                    detail=detail,
                )
        return self._result(terminal)

    def request_cancel(
        self,
        operation_id: str,
        *,
        requested_by: str = "human-operator",
        reason: str = "Cancelled by the operator.",
    ) -> dict[str, Any]:
        operation_id = _identifier(operation_id, "operation id")
        if (
            self.bridge.agent_id != HUMAN_OPERATOR_ID
            or _identifier(requested_by, "requester") != HUMAN_OPERATOR_ID
        ):
            raise OperationQueueError("only human-operator may cancel workflows")
        reason = _safe_text(reason, "cancellation reason", limit=2_000)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, operation_id)
            if row["status"] in TERMINAL_OPERATION_STATUSES:
                raise OperationQueueError("operation already has a terminal outcome")
            if row["status"] in {"queued", "retry"}:
                updated = self._set_terminal_locked(
                    connection,
                    row,
                    status="cancelled",
                    outcome="cancelled_before_start",
                    detail=reason,
                )
            else:
                connection.execute(
                    """UPDATE governance_operations
                          SET status='cancelling', cancellation_requested=1,
                              terminal_detail=?, updated_utc=?
                        WHERE scope=? AND operation_id=?""",
                    (reason, utc_now(), self.scope, operation_id),
                )
                updated = self._rehash(connection, operation_id)
                self.bridge._event(
                    connection,
                    "workflow.operation.cancellation_requested",
                    {
                        "operation_id": operation_id,
                        "operation_sha256": updated["operation_sha256"],
                    },
                )
        return self._result(updated)

    def acknowledge_cancel(
        self,
        operation_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        operation_id = _identifier(operation_id, "operation id")
        now = time.time() if now_epoch is None else float(now_epoch)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, operation_id)
            self._require_live_worker(
                row, worker_id, lease_token, now_epoch=now
            )
            if not row["cancellation_requested"]:
                raise OperationQueueError("operation cancellation was not requested")
            terminal = self._set_terminal_locked(
                connection,
                row,
                status="cancelled",
                outcome="cancelled",
                detail=str(row["terminal_detail"] or "Cancellation acknowledged."),
            )
        return self._result(terminal)

    def list_operations(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if status is not None and status not in OPERATION_STATUSES:
            raise OperationQueueError("operation status filter is invalid")
        limit = max(1, min(int(limit), 500))
        where = "scope=?" + (" AND status=?" if status else "")
        params: tuple[Any, ...] = (self.scope, status) if status else (self.scope,)
        with self.bridge._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM governance_operations WHERE {where}
                    ORDER BY created_utc DESC, operation_id DESC LIMIT ?""",
                (*params, limit),
            ).fetchall()
            return [self._result(self._row(connection, str(row["operation_id"]))) for row in rows]

    def save_schedule(
        self,
        *,
        workflow_id: str,
        task_text: str,
        working_directory: str,
        resource_key: str,
        interval_seconds: int,
        next_run_epoch: float,
        enabled: bool,
        requested_by: str = "human-operator",
        permission_decision_id: str | None = None,
        schedule_id: str | None = None,
    ) -> dict[str, Any]:
        if self.bridge.agent_id != HUMAN_OPERATOR_ID or requested_by != HUMAN_OPERATOR_ID:
            raise OperationQueueError("only human-operator may save workflow schedules")
        if not isinstance(enabled, bool):
            raise OperationQueueError("schedule enabled state must be explicit")
        interval_seconds = int(interval_seconds)
        if not MIN_SCHEDULE_INTERVAL_SECONDS <= interval_seconds <= MAX_SCHEDULE_INTERVAL_SECONDS:
            raise OperationQueueError("schedule interval is outside the bounded range")
        workflow_id = _identifier(workflow_id, "workflow id")
        if workflow_id not in WORKFLOW_TEMPLATES:
            raise OperationQueueError("workflow template is not registered")
        task_text = _safe_text(task_text, "task text")
        working_directory = _safe_text(working_directory, "working directory", limit=2_000)
        resource_key = _identifier(resource_key, "resource key")
        permission_decision_id = (
            _identifier(permission_decision_id, "permission decision id")
            if permission_decision_id
            else None
        )
        schedule_id = _identifier(schedule_id or uuid.uuid4().hex, "schedule id")
        created = utc_now()
        payload = {
            "scope": self.scope,
            "schedule_id": schedule_id,
            "workflow_id": workflow_id,
            "requested_by": requested_by,
            "task_text": task_text,
            "working_directory": working_directory,
            "resource_key": resource_key,
            "permission_decision_id": permission_decision_id,
            "interval_seconds": interval_seconds,
            "next_run_epoch": float(next_run_epoch),
            "enabled": int(enabled),
            "last_materialized_epoch": None,
            "created_utc": created,
            "updated_utc": created,
        }
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO workflow_schedules(
                        scope, schedule_id, workflow_id, requested_by, task_text,
                        working_directory, resource_key, permission_decision_id,
                        interval_seconds, next_run_epoch, enabled,
                        last_materialized_epoch, created_utc, updated_utc,
                        schedule_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                    (
                        self.scope,
                        schedule_id,
                        workflow_id,
                        requested_by,
                        task_text,
                        working_directory,
                        resource_key,
                        permission_decision_id,
                        interval_seconds,
                        float(next_run_epoch),
                        int(enabled),
                        created,
                        created,
                        stable_sha256(payload),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OperationQueueError("schedule id already exists") from exc
            self.bridge._event(
                connection,
                "workflow.schedule.saved",
                {
                    "schedule_id": schedule_id,
                    "workflow_id": workflow_id,
                    "enabled": enabled,
                    "schedule_sha256": stable_sha256(payload),
                },
            )
        return {**payload, "enabled": enabled, "schedule_sha256": stable_sha256(payload)}

    def list_schedules(
        self, *, enabled: bool | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if enabled is not None and not isinstance(enabled, bool):
            raise OperationQueueError("schedule enabled filter must be boolean")
        limit = max(1, min(int(limit), 500))
        where = "scope=?" + (" AND enabled=?" if enabled is not None else "")
        params: tuple[Any, ...] = (
            (self.scope, int(enabled)) if enabled is not None else (self.scope,)
        )
        with self.bridge._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM workflow_schedules WHERE {where}
                    ORDER BY next_run_epoch, schedule_id LIMIT ?""",
                (*params, limit),
            ).fetchall()
        result = []
        for row in rows:
            if stable_sha256(_schedule_payload(row)) != row["schedule_sha256"]:
                raise OperationQueueError("schedule SHA-256 does not match its state")
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    def set_schedule_enabled(
        self,
        schedule_id: str,
        *,
        enabled: bool,
        requested_by: str = "human-operator",
    ) -> dict[str, Any]:
        if self.bridge.agent_id != HUMAN_OPERATOR_ID or requested_by != HUMAN_OPERATOR_ID:
            raise OperationQueueError("only human-operator may change workflow schedules")
        if not isinstance(enabled, bool):
            raise OperationQueueError("schedule enabled state must be explicit")
        schedule_id = _identifier(schedule_id, "schedule id")
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_schedules WHERE scope=? AND schedule_id=?",
                (self.scope, schedule_id),
            ).fetchone()
            if row is None:
                raise OperationQueueError("schedule does not exist")
            if stable_sha256(_schedule_payload(row)) != row["schedule_sha256"]:
                raise OperationQueueError("schedule SHA-256 does not match its state")
            connection.execute(
                """UPDATE workflow_schedules SET enabled=?, updated_utc=?
                    WHERE scope=? AND schedule_id=?""",
                (int(enabled), utc_now(), self.scope, schedule_id),
            )
            changed = connection.execute(
                "SELECT * FROM workflow_schedules WHERE scope=? AND schedule_id=?",
                (self.scope, schedule_id),
            ).fetchone()
            assert changed is not None
            digest = stable_sha256(_schedule_payload(changed))
            connection.execute(
                """UPDATE workflow_schedules SET schedule_sha256=?
                    WHERE scope=? AND schedule_id=?""",
                (digest, self.scope, schedule_id),
            )
            self.bridge._event(
                connection,
                "workflow.schedule.enabled" if enabled else "workflow.schedule.disabled",
                {
                    "schedule_id": schedule_id,
                    "enabled": enabled,
                    "schedule_sha256": digest,
                },
            )
        result = dict(changed)
        result["enabled"] = enabled
        result["schedule_sha256"] = digest
        return result

    def materialize_due_schedules(
        self, *, now_epoch: float | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        self._require_service_transition("materialize workflow schedules")
        now = time.time() if now_epoch is None else float(now_epoch)
        limit = max(1, min(int(limit), 100))
        created: list[sqlite3.Row] = []
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            schedules = connection.execute(
                """SELECT * FROM workflow_schedules
                    WHERE scope=? AND enabled=1 AND next_run_epoch<=?
                    ORDER BY next_run_epoch, schedule_id LIMIT ?""",
                (self.scope, now, limit),
            ).fetchall()
            for schedule in schedules:
                if stable_sha256(_schedule_payload(schedule)) != schedule["schedule_sha256"]:
                    raise OperationQueueError("schedule SHA-256 does not match its state")
                operation_id = f"schedule:{schedule['schedule_id']}:{int(schedule['next_run_epoch'])}"
                row = self._enqueue_locked(
                    connection,
                    workflow_id=str(schedule["workflow_id"]),
                    task_text=str(schedule["task_text"]),
                    working_directory=str(schedule["working_directory"]),
                    resource_key=str(schedule["resource_key"]),
                    requested_by=str(schedule["requested_by"]),
                    permission_decision_id=(
                        str(schedule["permission_decision_id"])
                        if schedule["permission_decision_id"]
                        else None
                    ),
                    max_attempts=(
                        3
                        if WORKFLOW_TEMPLATES[str(schedule["workflow_id"])][
                            "automatic_retry"
                        ]
                        else 1
                    ),
                    timeout_seconds=1_800,
                    not_before_epoch=now,
                    operation_id=operation_id,
                )
                next_run = now + int(schedule["interval_seconds"])
                connection.execute(
                    """UPDATE workflow_schedules
                          SET last_materialized_epoch=?, next_run_epoch=?, updated_utc=?
                        WHERE scope=? AND schedule_id=?""",
                    (now, next_run, utc_now(), self.scope, schedule["schedule_id"]),
                )
                updated = connection.execute(
                    "SELECT * FROM workflow_schedules WHERE scope=? AND schedule_id=?",
                    (self.scope, schedule["schedule_id"]),
                ).fetchone()
                assert updated is not None
                connection.execute(
                    "UPDATE workflow_schedules SET schedule_sha256=? WHERE scope=? AND schedule_id=?",
                    (
                        stable_sha256(_schedule_payload(updated)),
                        self.scope,
                        schedule["schedule_id"],
                    ),
                )
                created.append(row)
        return [self._result(row) for row in created]


__all__ = [
    "DurableOperationQueue",
    "MAX_OPERATION_ATTEMPTS",
    "OPERATION_STATUSES",
    "OperationCapacityError",
    "OperationClaim",
    "OperationQueueError",
    "RELEASE_GATE_RECEIPT_SCHEMA",
    "RELEASE_GATE_TERMINAL_OUTCOME",
    "RELEASE_REVIEW_VERDICT_MARKER",
    "RELEASE_REVIEW_VERDICT_SCHEMA",
    "TERMINAL_OPERATION_STATUSES",
    "WORKFLOW_TEMPLATES",
]
