"""Track room-backed guided workflows without launching duplicate Agent CLIs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .bridge import (
    CONTROL_ROOM_WORKFLOW_ID,
    HUMAN_OPERATOR_ID,
    Bridge,
    BridgeError,
    stable_sha256,
)
from .guided_room_workflows import (
    GuidedRoomWorkflowError,
    guided_plan_from_operation,
    validate_guided_room_source,
)
from .operation_queue import (
    DurableOperationQueue,
    OperationClaim,
    OperationQueueError,
)
from .secret_scan import redact_secrets


ROOM_DISCUSSION_RECEIPT_SCHEMA = "peerbridge.room-discussion-receipt.v1"
ROOM_DISCUSSION_TERMINAL_OUTCOME = "room_discussion_completed"
OPEN_DISCUSSION_STATUSES = frozenset({"active", "paused", "waiting_human"})


class RoomDiscussionTrackerError(RuntimeError):
    """A durable room discussion cannot be tracked without weakening its binding."""


class RoomDiscussionTracker:
    """Claim only room-backed operations and follow their existing discussions."""

    def __init__(
        self,
        bridge: Bridge,
        *,
        human_bridge: Bridge | None = None,
        lease_seconds: int = 30,
        heartbeat_seconds: float = 8.0,
        poll_seconds: float = 0.25,
        max_parallel: int = 8,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if bridge.agent_id != CONTROL_ROOM_WORKFLOW_ID:
            raise RoomDiscussionTrackerError(
                "room discussion tracker requires control-room-workflow identity"
            )
        self.bridge = bridge
        self.queue = DurableOperationQueue(bridge)
        self.human_bridge = human_bridge or Bridge(
            bridge.root,
            bridge.db_path,
            HUMAN_OPERATOR_ID,
            bridge.scope,
        )
        self.lease_seconds = max(5, min(int(lease_seconds), 300))
        self.heartbeat_seconds = max(
            1.0, min(float(heartbeat_seconds), self.lease_seconds / 2)
        )
        self.poll_seconds = max(0.05, min(float(poll_seconds), 5.0))
        self.max_parallel = max(1, min(int(max_parallel), 32))
        self.clock = clock
        self.worker_prefix = f"room-tracker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers: dict[str, threading.Thread] = {}
        self._workers_lock = threading.RLock()
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._stop.is_set():
            raise RoomDiscussionTrackerError("room discussion tracker is closed")
        self._thread = threading.Thread(
            target=self._run,
            name="peerbridge-room-discussion-tracker",
            daemon=True,
        )
        self._thread.start()

    def close(self, *, wait_seconds: float = 10.0) -> None:
        self._stop.set()
        deadline = time.monotonic() + max(0.0, min(float(wait_seconds), 30.0))
        main = self._thread
        if main is not None and main is not threading.current_thread():
            main.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._workers_lock:
            workers = tuple(self._workers.values())
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.queue.reconcile()
                with self._workers_lock:
                    self._workers = {
                        operation_id: worker
                        for operation_id, worker in self._workers.items()
                        if worker.is_alive()
                    }
                    capacity = self.max_parallel - len(self._workers)
                claimed = False
                for _index in range(capacity):
                    claim = self.queue.claim(
                        f"{self.worker_prefix}-{_index + 1}",
                        lease_seconds=self.lease_seconds,
                        operation_class="room-discussion",
                    )
                    if claim is None:
                        break
                    operation_id = str(claim.operation["operation_id"])
                    worker = threading.Thread(
                        target=self._track_wrapper,
                        args=(claim,),
                        name=f"peerbridge-room-discussion-{operation_id[-12:]}",
                        daemon=True,
                    )
                    with self._workers_lock:
                        self._workers[operation_id] = worker
                    worker.start()
                    claimed = True
                if claimed:
                    self.last_error = None
                self._stop.wait(self.poll_seconds)
            except (BridgeError, OperationQueueError, OSError) as exc:
                self.last_error = redact_secrets(str(exc))[:500]
                self._stop.wait(self.poll_seconds)
            except Exception as exc:
                self.last_error = redact_secrets(str(exc))[:500]
                self._stop.wait(1.0)

    def _track_wrapper(self, claim: OperationClaim) -> None:
        try:
            self._track_claim(claim)
        except (
            BridgeError,
            GuidedRoomWorkflowError,
            OperationQueueError,
            RoomDiscussionTrackerError,
            OSError,
        ) as exc:
            self.last_error = redact_secrets(str(exc))[:500]
            if not self._stop.is_set():
                self._fail_claim(
                    claim,
                    error_class="source-binding",
                    detail="Guided room discussion failed source-bound tracking.",
                )
        finally:
            operation_id = str(claim.operation["operation_id"])
            with self._workers_lock:
                self._workers.pop(operation_id, None)

    @staticmethod
    def _message_content(row: Mapping[str, Any], bridge: Bridge) -> dict[str, Any]:
        route_request = bridge._route_request_from_row(row)
        if route_request is None:
            raise RoomDiscussionTrackerError(
                "guided discussion prompt has no route binding"
            )
        try:
            artifacts = json.loads(str(row["artifact_paths_json"]))
        except json.JSONDecodeError as exc:
            raise RoomDiscussionTrackerError(
                "guided discussion prompt artifacts are invalid"
            ) from exc
        return {
            "message_id": str(row["message_id"]),
            "scope": str(row["scope"]),
            "room_id": str(row["room_id"]),
            "task_id": str(row["task_id"]),
            "sender": str(row["sender"]),
            "recipient": str(row["recipient"]),
            "subject": str(row["subject"]),
            "body": str(row["body"]),
            "priority": str(row["priority"]),
            "reply_to": row["reply_to"],
            "artifact_paths": artifacts,
            "route_request": bridge._route_request_content_binding(route_request),
            "discussion_id": str(row["discussion_id"]),
            "discussion_round": int(row["discussion_round"]),
            "discussion_role": str(row["discussion_role"]),
            "visibility": str(row["visibility"] or "direct"),
            "created_utc": str(row["created_utc"]),
        }

    def _discussion_snapshot(self, plan: Mapping[str, Any]) -> dict[str, Any] | None:
        with self.bridge._connect() as connection:
            connection.execute("BEGIN")
            validate_guided_room_source(
                plan,
                self.bridge._room_members_locked(
                    connection,
                    str(plan["room_id"]),
                ),
            )
            discussion = self.bridge._bound_discussion_for_operation(
                connection,
                discussion_id=str(plan.get("bound_discussion_id") or ""),
                room_id=str(plan["room_id"]),
                task_id=str(plan["task_id"]),
                prompt_sha256=str(plan["prompt_sha256"]),
                participants=plan["participants"],
            )
            if discussion is None:
                return None
            if (
                stable_sha256(Bridge._discussion_row_payload(discussion))
                != discussion["discussion_sha256"]
            ):
                raise RoomDiscussionTrackerError("guided discussion SHA-256 mismatch")
            expected_discussion = {
                "room_id": str(plan["room_id"]),
                "task_id": str(plan["task_id"]),
                "subject": str(plan["subject"]),
                "starter_agent_id": HUMAN_OPERATOR_ID,
                "max_rounds": int(plan["max_rounds"]),
                "max_messages": int(plan["max_messages"]),
                "stagnation_rounds": int(plan["stagnation_rounds"]),
            }
            if any(
                discussion[key] != value
                for key, value in expected_discussion.items()
            ):
                raise RoomDiscussionTrackerError(
                    "guided discussion policy or source identity changed"
                )
            prompts = connection.execute(
                """SELECT * FROM messages
                    WHERE scope=? AND discussion_id=? AND discussion_round=1
                      AND discussion_role='prompt'
                    ORDER BY recipient""",
                (self.bridge.scope, discussion["discussion_id"]),
            ).fetchall()
            bound_routes = self.bridge._discussion_participants(
                connection, str(discussion["discussion_id"])
            )

        expected = sorted(
            (
                str(participant["agent_id"]),
                str(participant["route_profile_id"]),
                str(participant["route_profile_sha256"]),
            )
            for participant in plan["participants"]
        )
        observed = sorted(
            (
                str(prompt["recipient"]),
                str(prompt["route_profile_id"] or ""),
                str(prompt["route_profile_sha256"] or ""),
            )
            for prompt in prompts
        )
        current_routes = sorted(
            (
                str(agent_id),
                str(route_profile_id),
                str(route_request["route_profile_sha256"]),
            )
            for agent_id, route_profile_id, route_request in bound_routes
        )
        if observed != expected or current_routes != expected:
            raise RoomDiscussionTrackerError(
                "guided discussion participant or complete route snapshot changed"
            )
        for prompt in prompts:
            if stable_sha256(self._message_content(prompt, self.bridge)) != str(
                prompt["content_sha256"]
            ):
                raise RoomDiscussionTrackerError(
                    "guided discussion prompt SHA-256 mismatch"
                )
            body_sha256 = hashlib.sha256(
                str(prompt["body"]).encode("utf-8")
            ).hexdigest()
            if body_sha256 != plan["prompt_sha256"]:
                raise RoomDiscussionTrackerError(
                    "guided discussion prompt does not match its durable operation"
                )
        return dict(discussion)

    def _bound_discussion_snapshot(
        self, plan: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        with self.bridge._connect() as connection:
            row = self.bridge._bound_discussion_for_operation(
                connection,
                discussion_id=str(plan.get("bound_discussion_id") or ""),
                room_id=str(plan["room_id"]),
                task_id=str(plan["task_id"]),
                prompt_sha256=str(plan["prompt_sha256"]),
                participants=plan["participants"],
            )
        return dict(row) if row is not None else None

    def _stop_bound_discussion(
        self,
        plan: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None,
        *,
        trigger: str,
    ) -> None:
        if snapshot is None:
            snapshot = self._bound_discussion_snapshot(plan)
        if snapshot is None or str(snapshot.get("status") or "") not in OPEN_DISCUSSION_STATUSES:
            return
        try:
            self.human_bridge.control_discussion(
                {
                    "discussion_id": str(snapshot["discussion_id"]),
                    "action": "stop",
                }
            )
        except BridgeError:
            refreshed = self._bound_discussion_snapshot(plan)
            if refreshed is not None and str(
                refreshed.get("status") or ""
            ) not in OPEN_DISCUSSION_STATUSES:
                return
            raise
        refreshed = self._bound_discussion_snapshot(plan)
        if (
            refreshed is None
            or str(refreshed.get("discussion_id") or "")
            != str(snapshot.get("discussion_id") or "")
        ):
            raise RoomDiscussionTrackerError(
                f"guided discussion did not stop after {trigger}"
            )
        if str(refreshed.get("status") or "") == "stopped":
            return
        if str(refreshed.get("status") or "") not in OPEN_DISCUSSION_STATUSES:
            return
        raise RoomDiscussionTrackerError(
            f"guided discussion did not stop after {trigger}"
        )

    def _completion_detail(
        self, claim: OperationClaim, plan: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> str:
        receipt = {
            "schema": ROOM_DISCUSSION_RECEIPT_SCHEMA,
            "operation_id": str(claim.operation["operation_id"]),
            "room_id": str(snapshot["room_id"]),
            "task_id": str(snapshot["task_id"]),
            "discussion_id": str(snapshot["discussion_id"]),
            "source_binding_sha256": str(plan["source_binding_sha256"]),
            "discussion_sha256": str(snapshot["discussion_sha256"]),
            "status": str(snapshot["status"]),
            "stop_reason": str(snapshot.get("stop_reason") or ""),
            "processed_round": int(snapshot["processed_round"]),
            "message_count": int(snapshot["message_count"]),
        }
        return json.dumps(
            receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )

    def _fail_claim(
        self,
        claim: OperationClaim,
        *,
        error_class: str,
        detail: str,
        allow_retry: bool = True,
        now_epoch: float | None = None,
    ) -> None:
        try:
            self.queue.fail(
                str(claim.operation["operation_id"]),
                str(claim.operation["lease_owner"]),
                claim.lease_token,
                error_class=error_class,
                detail=detail,
                retry_after_seconds=0,
                allow_retry=allow_retry,
                now_epoch=now_epoch,
            )
        except OperationQueueError:
            with contextlib.suppress(OperationQueueError):
                self.queue.reconcile(now_epoch=now_epoch)

    def _track_claim(self, claim: OperationClaim) -> None:
        plan = guided_plan_from_operation(claim.operation)
        if not plan.get("bound_discussion_id"):
            raise RoomDiscussionTrackerError(
                "guided operation has no persisted discussion binding"
            )
        operation_id = str(claim.operation["operation_id"])
        worker_id = str(claim.operation["lease_owner"])
        next_heartbeat = self.clock()
        while not self._stop.is_set():
            live = self.queue.get_operation(operation_id)
            try:
                snapshot = self._discussion_snapshot(plan)
            except (BridgeError, GuidedRoomWorkflowError, RoomDiscussionTrackerError):
                self._stop_bound_discussion(
                    plan,
                    None,
                    trigger="source-binding failure",
                )
                raise
            if live["cancellation_requested"]:
                self._stop_bound_discussion(
                    plan,
                    snapshot,
                    trigger="cancellation",
                )
                self.queue.acknowledge_cancel(
                    operation_id,
                    worker_id,
                    claim.lease_token,
                    now_epoch=self.clock(),
                )
                return

            now = self.clock()
            if snapshot is not None:
                status = str(snapshot["status"])
                if status == "completed":
                    self.queue.complete(
                        operation_id,
                        worker_id,
                        claim.lease_token,
                        outcome=ROOM_DISCUSSION_TERMINAL_OUTCOME,
                        detail=self._completion_detail(claim, plan, snapshot),
                        now_epoch=now,
                    )
                    return
                if status == "waiting_human":
                    self._fail_claim(
                        claim,
                        error_class="human-attention",
                        detail=(
                            "Guided discussion reached a bounded human-attention state: "
                            f"{snapshot.get('stop_reason') or 'unspecified'}."
                        ),
                        now_epoch=now,
                    )
                    return
                if status == "stopped":
                    self._fail_claim(
                        claim,
                        error_class="discussion-stopped",
                        detail=(
                            "Guided discussion stopped without a verified completion: "
                            f"{snapshot.get('stop_reason') or 'unspecified'}."
                        ),
                        now_epoch=now,
                    )
                    return
                if status not in {"active", "paused"}:
                    raise RoomDiscussionTrackerError(
                        "guided discussion status is unsupported"
                    )
            deadline = float(live["attempt_deadline_epoch"] or 0)
            if deadline and now >= deadline - 0.05:
                self._stop_bound_discussion(
                    plan,
                    snapshot,
                    trigger="timeout",
                )
                terminal_now = math.nextafter(deadline, -math.inf)
                self._fail_claim(
                    claim,
                    error_class="timeout",
                    detail="Guided discussion stopped after it exceeded its timeout.",
                    allow_retry=False,
                    now_epoch=terminal_now,
                )
                return
            if now >= next_heartbeat:
                self.queue.heartbeat(
                    operation_id,
                    worker_id,
                    claim.lease_token,
                    lease_seconds=self.lease_seconds,
                    now_epoch=now,
                )
                next_heartbeat = now + self.heartbeat_seconds
            self._stop.wait(self.poll_seconds)


__all__ = [
    "ROOM_DISCUSSION_RECEIPT_SCHEMA",
    "ROOM_DISCUSSION_TERMINAL_OUTCOME",
    "RoomDiscussionTracker",
    "RoomDiscussionTrackerError",
]
