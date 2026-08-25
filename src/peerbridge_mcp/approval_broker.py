"""Provider-neutral, fail-closed approval requests for managed Agent sessions."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Literal

from .secret_scan import redact_secrets


APPROVAL_BROKER_SCHEMA = "peerbridge.approval-request.v1"
APPROVAL_MODES = frozenset(
    {"approval-required", "agent-delegated", "full-access"}
)
APPROVAL_DECISIONS = frozenset({"allow-once", "allow-session", "deny"})
APPROVAL_STATES = frozenset({"pending", "allowed", "denied", "expired", "cancelled"})
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
MAX_PENDING_APPROVALS = 16


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: object, *, limit: int) -> str:
    text = " ".join(redact_secrets(str(value or "")).replace("\x00", "").split())
    return text[:limit]


def _record_sha(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    session_id: str
    adapter_id: str
    provider_request_id: str
    action_kind: str
    title: str
    detail: str
    risk: Literal["routine", "elevated", "high"]
    available_decisions: tuple[str, ...]
    created_utc: str
    state: Literal["pending", "allowed", "denied", "expired", "cancelled"]
    decision: str | None
    resolved_utc: str | None
    record_sha256: str
    schema: str = APPROVAL_BROKER_SCHEMA

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "adapter_id": self.adapter_id,
            "provider_request_id": self.provider_request_id,
            "action_kind": self.action_kind,
            "title": self.title,
            "detail": self.detail,
            "risk": self.risk,
            "available_decisions": list(self.available_decisions),
            "created_utc": self.created_utc,
            "state": self.state,
            "decision": self.decision,
            "resolved_utc": self.resolved_utc,
            "record_sha256": self.record_sha256,
        }


@dataclass
class _PendingApproval:
    record: ApprovalRecord
    resolved: threading.Event


class ApprovalBroker:
    """Pause one provider request until an exact human or policy decision."""

    def __init__(
        self,
        *,
        session_id: str,
        adapter_id: str,
        mode: Literal["approval-required", "agent-delegated", "full-access"],
        on_change: Callable[[ApprovalRecord], None] | None = None,
    ) -> None:
        if not SAFE_ID.fullmatch(session_id) or not SAFE_ID.fullmatch(adapter_id):
            raise ValueError("invalid approval broker identity")
        if mode not in APPROVAL_MODES:
            raise ValueError("invalid approval broker mode")
        self.session_id = session_id
        self.adapter_id = adapter_id
        self.mode = mode
        self._on_change = on_change
        self._pending: dict[str, _PendingApproval] = {}
        self._history: list[ApprovalRecord] = []
        self._lock = threading.RLock()

    @staticmethod
    def _new_record(
        *,
        session_id: str,
        adapter_id: str,
        provider_request_id: str,
        action_kind: str,
        title: str,
        detail: str,
        risk: Literal["routine", "elevated", "high"],
        available_decisions: tuple[str, ...],
    ) -> ApprovalRecord:
        base = {
            "schema": APPROVAL_BROKER_SCHEMA,
            "approval_id": f"approval-{uuid.uuid4().hex}",
            "session_id": session_id,
            "adapter_id": adapter_id,
            "provider_request_id": provider_request_id,
            "action_kind": action_kind,
            "title": _safe_text(title, limit=240),
            "detail": _safe_text(detail, limit=2000),
            "risk": risk,
            "available_decisions": list(available_decisions),
            "created_utc": _utc_now(),
            "state": "pending",
            "decision": None,
            "resolved_utc": None,
        }
        return ApprovalRecord(
            approval_id=str(base["approval_id"]),
            session_id=session_id,
            adapter_id=adapter_id,
            provider_request_id=provider_request_id,
            action_kind=action_kind,
            title=str(base["title"]),
            detail=str(base["detail"]),
            risk=risk,
            available_decisions=available_decisions,
            created_utc=str(base["created_utc"]),
            state="pending",
            decision=None,
            resolved_utc=None,
            record_sha256=_record_sha(base),
        )

    def _emit(self, record: ApprovalRecord) -> None:
        if self._on_change is not None:
            self._on_change(record)

    def request(
        self,
        *,
        provider_request_id: str,
        action_kind: str,
        title: str,
        detail: str,
        risk: Literal["routine", "elevated", "high"] = "elevated",
        available_decisions: tuple[str, ...] = (
            "allow-once",
            "allow-session",
            "deny",
        ),
        timeout_seconds: float = 3600.0,
    ) -> str:
        if not SAFE_ID.fullmatch(provider_request_id):
            raise ValueError("invalid provider approval request id")
        if not SAFE_ID.fullmatch(action_kind):
            raise ValueError("invalid approval action kind")
        if risk not in {"routine", "elevated", "high"}:
            raise ValueError("invalid approval risk")
        decisions = tuple(dict.fromkeys(available_decisions))
        if not decisions or any(value not in APPROVAL_DECISIONS for value in decisions):
            raise ValueError("invalid approval decisions")
        if not 1 <= float(timeout_seconds) <= 86_400:
            raise ValueError("invalid approval timeout")
        if self.mode == "full-access":
            return "allow-once" if "allow-once" in decisions else decisions[0]
        with self._lock:
            if len(self._pending) >= MAX_PENDING_APPROVALS:
                raise ValueError("pending approval limit reached")
            if (
                self.mode == "agent-delegated"
                and risk == "routine"
                and "allow-once" in decisions
            ):
                return "allow-once"
            if any(
                item.record.provider_request_id == provider_request_id
                for item in self._pending.values()
            ):
                raise ValueError("duplicate provider approval request")
            record = self._new_record(
                session_id=self.session_id,
                adapter_id=self.adapter_id,
                provider_request_id=provider_request_id,
                action_kind=action_kind,
                title=title,
                detail=detail,
                risk=risk,
                available_decisions=decisions,
            )
            pending = _PendingApproval(record=record, resolved=threading.Event())
            self._pending[record.approval_id] = pending
        self._emit(record)
        if not pending.resolved.wait(timeout=float(timeout_seconds)):
            self._finalize(record.approval_id, "deny", state="expired")
        with self._lock:
            resolved_record = next(
                row for row in reversed(self._history) if row.approval_id == record.approval_id
            )
        return str(resolved_record.decision or "deny")

    def _finalize(
        self,
        approval_id: str,
        decision: str,
        *,
        state: Literal["allowed", "denied", "expired", "cancelled"] | None = None,
    ) -> ApprovalRecord:
        with self._lock:
            pending = self._pending.pop(approval_id, None)
            if pending is None:
                raise KeyError("approval request is not pending")
            if decision not in pending.record.available_decisions and decision != "deny":
                self._pending[approval_id] = pending
                raise ValueError("approval decision is unavailable")
            resolved_state = state or (
                "allowed" if decision in {"allow-once", "allow-session"} else "denied"
            )
            resolved_utc = _utc_now()
            base = pending.record.as_dict()
            base.update(
                {
                    "state": resolved_state,
                    "decision": decision,
                    "resolved_utc": resolved_utc,
                }
            )
            base.pop("record_sha256", None)
            resolved_record = replace(
                pending.record,
                state=resolved_state,
                decision=decision,
                resolved_utc=resolved_utc,
                record_sha256=_record_sha(base),
            )
            self._history.append(resolved_record)
            pending.record = resolved_record
            pending.resolved.set()
        self._emit(resolved_record)
        return resolved_record

    def resolve(self, approval_id: str, decision: str) -> ApprovalRecord:
        if not SAFE_ID.fullmatch(approval_id):
            raise ValueError("invalid approval id")
        if decision not in APPROVAL_DECISIONS:
            raise ValueError("invalid approval decision")
        return self._finalize(approval_id, decision)

    def cancel_all(self) -> None:
        with self._lock:
            approval_ids = tuple(self._pending)
        for approval_id in approval_ids:
            try:
                self._finalize(approval_id, "deny", state="cancelled")
            except KeyError:
                continue

    def cancel_provider_request(self, provider_request_id: str) -> bool:
        if not SAFE_ID.fullmatch(provider_request_id):
            return False
        with self._lock:
            approval_id = next(
                (
                    item.record.approval_id
                    for item in self._pending.values()
                    if item.record.provider_request_id == provider_request_id
                ),
                None,
            )
        if approval_id is None:
            return False
        try:
            self._finalize(approval_id, "deny", state="cancelled")
        except KeyError:
            return False
        return True

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            pending = [item.record.as_dict() for item in self._pending.values()]
            history = [row.as_dict() for row in self._history[-80:]]
        return {
            "schema": APPROVAL_BROKER_SCHEMA,
            "mode": self.mode,
            "pending": pending,
            "history": history,
            "pending_count": len(pending),
            "history_count": len(history),
        }


__all__ = [
    "APPROVAL_BROKER_SCHEMA",
    "APPROVAL_DECISIONS",
    "APPROVAL_MODES",
    "MAX_PENDING_APPROVALS",
    "ApprovalBroker",
    "ApprovalRecord",
]
