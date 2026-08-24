"""Source-bound PeerBridge release requests that never publish by themselves."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Literal

from .bridge import (
    CONTROL_ROOM_WORKFLOW_ID,
    HUMAN_OPERATOR_ID,
    Bridge,
    stable_sha256,
)
from .execution_governance import GovernanceError, repository_source_state
from .operation_queue import (
    RELEASE_GATE_RECEIPT_SCHEMA,
    RELEASE_GATE_TERMINAL_OUTCOME,
    DurableOperationQueue,
    OperationQueueError,
)
from .secret_scan import contains_secret
from .trust_timeline import TrustTimeline, TrustTimelineError


RELEASE_GATE_ROOT = ".peerbridge-artifacts/release-gates"
SOURCE_SCHEMA = "peerbridge.release-source.v1"
MANIFEST_SCHEMA = "peerbridge.release-gate-manifest.v1"
DECISION_HEADER = "PeerBridge release gate decision v2"
DECISION_SCHEMA = "peerbridge.release-gate-human-decision.v2"
DECISION_EVENT_TYPE = "release.gate.human_decision"
REQUEST_EVENT_TYPE = "release.gate.requested"
REQUEST_SCHEMA = "peerbridge.release-gate-request.v1"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CONTROL_ROOM_UI_AUTHORITY = object()


class ReleaseGateError(RuntimeError):
    """A release request is missing an exact, fresh, human-approved gate."""


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


class ReleaseGateService:
    """Materialize and inspect one idempotent gate per exact Git source state."""

    def __init__(self, bridge: Bridge, *, _authority: object | None = None) -> None:
        self.bridge = bridge
        self.queue = DurableOperationQueue(bridge)
        self.timeline = TrustTimeline(bridge)
        self._ui_authorized = _authority is _CONTROL_ROOM_UI_AUTHORITY

    @classmethod
    def for_control_room_ui(cls, bridge: Bridge) -> "ReleaseGateService":
        if bridge.agent_id != HUMAN_OPERATOR_ID:
            raise ReleaseGateError("Control Room release authority requires human-operator")
        return cls(bridge, _authority=_CONTROL_ROOM_UI_AUTHORITY)

    def _require_ui_authority(self) -> None:
        if not self._ui_authorized or self.bridge.agent_id != HUMAN_OPERATOR_ID:
            raise ReleaseGateError(
                "release mutations require a direct Control Room UI action"
            )

    def _source(self) -> dict[str, str]:
        state = repository_source_state(self.bridge.root)
        return {
            "schema": SOURCE_SCHEMA,
            "scope": self.bridge.scope,
            "resource_key": state["resource_key"],
            "commit_id": state["commit_id"],
            "diff_sha256": state["diff_sha256"],
        }

    @staticmethod
    def _fingerprint(source: dict[str, Any]) -> str:
        return stable_sha256(source)

    @staticmethod
    def _operation_id(fingerprint: str) -> str:
        return f"release-gate:{fingerprint}"

    @staticmethod
    def _manifest_relative(fingerprint: str) -> str:
        return f"{RELEASE_GATE_ROOT}/{fingerprint}/source.json"

    def _manifest_path(self, fingerprint: str) -> Path:
        root = (self.bridge.root / RELEASE_GATE_ROOT).resolve()
        try:
            root.relative_to(self.bridge.root)
        except ValueError as exc:
            raise ReleaseGateError("release gate artifact root escapes the project") from exc
        return root / fingerprint / "source.json"

    @staticmethod
    def _task_text(fingerprint: str, manifest_path: str) -> str:
        return (
            "Run the source-bound PeerBridge Release Gate for "
            f"{manifest_path} ({fingerprint[:16]}). Verify tests, proof, review, "
            "and release assets against that exact manifest. Observe only: do not "
            "commit, tag, push, publish, or approve the release."
        )

    def _operation_spec(self, fingerprint: str) -> dict[str, Any]:
        manifest_path = self._manifest_relative(fingerprint)
        return {
            "operation_id": self._operation_id(fingerprint),
            "workflow_id": "release-gate",
            "requested_by": CONTROL_ROOM_WORKFLOW_ID,
            "task_text": self._task_text(fingerprint, manifest_path),
            "working_directory": ".",
            "resource_key": f"release:{fingerprint}",
            "permission_decision_id": None,
            "max_attempts": 1,
            "timeout_seconds": 3_600,
            "not_before_epoch": 0.0,
        }

    @staticmethod
    def _operation_matches(operation: dict[str, Any], expected: dict[str, Any]) -> bool:
        return all(operation.get(key) == value for key, value in expected.items())

    def _write_manifest(
        self, source: dict[str, str], fingerprint: str
    ) -> tuple[str, str]:
        payload = {
            "schema": MANIFEST_SCHEMA,
            "fingerprint": fingerprint,
            "source": source,
        }
        data = _canonical_bytes(payload)
        path = self._checked_manifest_path(fingerprint, create_parent=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            existing = self._read_manifest_bytes(fingerprint)
            if existing != data:
                raise ReleaseGateError(
                    "existing release gate manifest does not match the exact source"
                )
        checked = self._checked_manifest_path(fingerprint, create_parent=False)
        try:
            info = checked.lstat()
        except OSError as exc:
            raise ReleaseGateError("release gate manifest is unavailable") from exc
        attributes = int(getattr(info, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or attributes & reparse_flag
        ):
            raise ReleaseGateError("release gate manifest must be a regular local file")
        return self._manifest_relative(fingerprint), hashlib.sha256(data).hexdigest()

    @staticmethod
    def _is_reparse_point(info: os.stat_result) -> bool:
        attributes = int(getattr(info, "st_file_attributes", 0))
        flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
        return stat.S_ISLNK(info.st_mode) or bool(attributes & flag)

    def _checked_manifest_path(self, fingerprint: str, *, create_parent: bool) -> Path:
        if not SHA256.fullmatch(fingerprint):
            raise ReleaseGateError("release gate fingerprint is invalid")
        root = self.bridge.root.resolve()
        current = root
        for part in (*Path(RELEASE_GATE_ROOT).parts, fingerprint):
            current = current / part
            if create_parent:
                try:
                    current.mkdir(exist_ok=True)
                except OSError as exc:
                    raise ReleaseGateError(
                        "release gate artifact directory could not be created"
                    ) from exc
            try:
                info = current.lstat()
                current.resolve().relative_to(root)
            except (OSError, ValueError) as exc:
                raise ReleaseGateError(
                    "release gate artifact directory escapes its root"
                ) from exc
            if not stat.S_ISDIR(info.st_mode) or self._is_reparse_point(info):
                raise ReleaseGateError(
                    "release gate artifact directory must not be a filesystem link"
                )
        return current / "source.json"

    def _read_manifest_bytes(self, fingerprint: str) -> bytes:
        path = self._checked_manifest_path(fingerprint, create_parent=False)
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ReleaseGateError("release gate manifest could not be opened safely") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024:
                raise ReleaseGateError("release gate manifest file is invalid")
            handle = os.fdopen(descriptor, "rb")
            descriptor = -1
            with handle:
                data = handle.read(64 * 1024 + 1)
            after = path.lstat()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            len(data) > 64 * 1024
            or self._is_reparse_point(after)
            or not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise ReleaseGateError("release gate manifest changed during safe read")
        self._checked_manifest_path(fingerprint, create_parent=False)
        return data

    def _manifest_status(self, fingerprint: str) -> dict[str, Any]:
        relative = self._manifest_relative(fingerprint)
        result: dict[str, Any] = {
            "path": relative,
            "exists": False,
            "valid": False,
            "sha256": None,
            "source": None,
        }
        try:
            data = self._read_manifest_bytes(fingerprint)
        except ReleaseGateError:
            return result
        result["exists"] = True
        result["sha256"] = hashlib.sha256(data).hexdigest()
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return result
        if not isinstance(payload, dict) or _canonical_bytes(payload) != data:
            return result
        source = payload.get("source")
        if not isinstance(source, dict):
            return result
        expected_source_keys = {
            "schema",
            "scope",
            "resource_key",
            "commit_id",
            "diff_sha256",
        }
        valid = bool(
            set(source) == expected_source_keys
            and source.get("schema") == SOURCE_SCHEMA
            and source.get("scope") == self.bridge.scope
            and payload.get("schema") == MANIFEST_SCHEMA
            and payload.get("fingerprint") == fingerprint
            and self._fingerprint(source) == fingerprint
            and GIT_OID.fullmatch(str(source.get("commit_id") or ""))
            and SHA256.fullmatch(str(source.get("diff_sha256") or ""))
        )
        result["valid"] = valid
        result["source"] = source if valid else None
        return result

    @staticmethod
    def _verdict_receipt(
        operation: dict[str, Any] | None, fingerprint: str
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"valid": False, "sha256": None, "reviews": []}
        if (
            operation is None
            or operation.get("status") != "succeeded"
            or operation.get("terminal_outcome") != RELEASE_GATE_TERMINAL_OUTCOME
        ):
            return result
        text = str(operation.get("terminal_detail") or "")
        result["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return result
        reviews = payload.get("reviews") if isinstance(payload, dict) else None
        valid = bool(
            isinstance(payload, dict)
            and set(payload)
            == {"schema", "operation_id", "source_fingerprint", "reviews"}
            and payload.get("schema") == RELEASE_GATE_RECEIPT_SCHEMA
            and payload.get("operation_id") == operation.get("operation_id")
            and payload.get("source_fingerprint") == fingerprint
            and isinstance(reviews, list)
            and len(reviews) == 2
            and {str(item.get("role") or "") for item in reviews if isinstance(item, dict)}
            == {"auditor", "reviewer"}
            and len(
                {str(item.get("agent_id") or "") for item in reviews if isinstance(item, dict)}
            )
            == 2
            and len(
                {str(item.get("session_id") or "") for item in reviews if isinstance(item, dict)}
            )
            == 2
            and all(
                isinstance(item, dict)
                and set(item)
                == {"agent_id", "session_id", "role", "decision", "answer_sha256"}
                and item.get("decision") == "approve"
                and SHA256.fullmatch(str(item.get("answer_sha256") or ""))
                for item in reviews
            )
            and json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            == text
        )
        result["valid"] = valid
        result["reviews"] = reviews if valid else []
        return result

    @staticmethod
    def _decision_statement(statement: Any) -> dict[str, Any] | None:
        lines = str(statement or "").splitlines()
        if len(lines) != 2 or lines[0] != DECISION_HEADER:
            return None
        try:
            payload = json.loads(lines[1])
        except json.JSONDecodeError:
            return None
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "schema",
                "decision",
                "fingerprint",
                "operation_sha256",
                "verdict_sha256",
                "reason",
            }
            or payload.get("schema") != DECISION_SCHEMA
            or payload.get("decision") not in {"approve", "reject"}
            or not SHA256.fullmatch(str(payload.get("fingerprint") or ""))
            or not SHA256.fullmatch(str(payload.get("operation_sha256") or ""))
            or not SHA256.fullmatch(str(payload.get("verdict_sha256") or ""))
            or not str(payload.get("reason") or "").strip()
            or json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            != lines[1]
        ):
            return None
        return payload

    def _human_decision(
        self,
        *,
        operation: dict[str, Any],
        fingerprint: str,
        manifest: dict[str, Any],
        verdict: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            records = {
                str(record["record_id"]): record
                for record in self.timeline.timeline(str(operation["operation_id"]))
            }
        except TrustTimelineError as exc:
            raise ReleaseGateError(str(exc)) from exc
        with self.bridge._connect() as connection:
            events = connection.execute(
                """SELECT * FROM events
                    WHERE scope=? AND event_type=? AND task_id=?
                    ORDER BY sequence DESC""",
                (self.bridge.scope, DECISION_EVENT_TYPE, operation["operation_id"]),
            ).fetchall()
        for event in events:
            payload_json = str(event["payload_json"])
            payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            envelope = {
                "event_id": event["event_id"],
                "scope": event["scope"],
                "actor": event["actor"],
                "event_type": event["event_type"],
                "task_id": event["task_id"],
                "payload_sha256": payload_sha,
                "created_utc": event["created_utc"],
                "prev_chain_sha256": event["prev_chain_sha256"],
            }
            if (
                event["actor"] != HUMAN_OPERATOR_ID
                or event["payload_sha256"] != payload_sha
                or event["chain_sha256"] != stable_sha256(envelope)
                or str(event["created_utc"]) < str(operation["updated_utc"])
            ):
                continue
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            record = records.get(str(payload.get("record_id") or ""))
            statement = self._decision_statement(
                record.get("statement") if record is not None else None
            )
            bindings = record.get("source_bindings") if record is not None else None
            expected = {
                "schema": DECISION_SCHEMA,
                "fingerprint": fingerprint,
                "operation_sha256": operation["operation_sha256"],
                "verdict_sha256": verdict["sha256"],
                "manifest_sha256": manifest["sha256"],
                "record_id": record.get("record_id") if record is not None else None,
                "trust_sha256": record.get("trust_sha256") if record is not None else None,
                "decision": statement.get("decision") if statement is not None else None,
            }
            payload_core = {key: payload.get(key) for key in expected}
            runtime_identity = payload.get("runtime_identity")
            if (
                set(payload) != {*expected, "session_id", "runtime_identity"}
                or payload_core != expected
                or not str(payload.get("session_id") or "")
                or not isinstance(runtime_identity, dict)
                or set(runtime_identity)
                != {
                    "client_name",
                    "provider_id",
                    "model_id",
                    "reasoning_mode",
                    "route_class",
                }
                or statement is None
                or statement.get("fingerprint") != fingerprint
                or statement.get("operation_sha256") != operation["operation_sha256"]
                or statement.get("verdict_sha256") != verdict["sha256"]
                or record.get("stage") != "decision"
                or record.get("actor") != HUMAN_OPERATOR_ID
                or record.get("stale")
                or not isinstance(bindings, list)
                or len(bindings) != 1
                or bindings[0].get("path") != manifest["path"]
                or bindings[0].get("sha256") != manifest["sha256"]
            ):
                continue
            return {
                "record_id": record["record_id"],
                "decision": statement["decision"],
                "created_utc": event["created_utc"],
                "fresh": True,
                "operation_sha256": operation["operation_sha256"],
                "verdict_sha256": verdict["sha256"],
            }
        return None

    def status(self, fingerprint: str | None = None) -> dict[str, Any]:
        current_source = self._source()
        current_fingerprint = self._fingerprint(current_source)
        requested = str(fingerprint or current_fingerprint).strip().lower()
        if not SHA256.fullmatch(requested):
            raise ReleaseGateError("release gate fingerprint is invalid")
        operation_id = self._operation_id(requested)
        operation: dict[str, Any] | None
        try:
            operation = self.queue.get_operation(operation_id)
        except OperationQueueError as exc:
            if "does not exist" not in str(exc):
                raise ReleaseGateError(str(exc)) from exc
            operation = None
        manifest = self._manifest_status(requested)
        expected_operation = self._operation_spec(requested)
        operation_matches = bool(
            operation is not None
            and self._operation_matches(operation, expected_operation)
        )
        verdict = self._verdict_receipt(operation, requested)
        decision = (
            self._human_decision(
                operation=operation,
                fingerprint=requested,
                manifest=manifest,
                verdict=verdict,
            )
            if operation_matches and manifest["valid"] and verdict["valid"]
            else None
        )
        source_fresh = requested == current_fingerprint
        operation_succeeded = bool(operation_matches and verdict["valid"])
        approved = bool(decision and decision.get("decision") == "approve")
        ready = bool(
            manifest["valid"]
            and source_fresh
            and operation_succeeded
            and approved
        )
        blockers = []
        if operation is None:
            blockers.append("gate_not_requested")
        elif not operation_matches:
            blockers.append("gate_operation_mismatch")
        elif not operation_succeeded:
            blockers.append(f"gate_{operation.get('status')}")
            if operation.get("status") == "succeeded" and not verdict["valid"]:
                blockers.append("gate_verdict_invalid")
        if not manifest["valid"]:
            blockers.append("manifest_invalid_or_missing")
        if not source_fresh:
            blockers.append("source_changed")
        if decision is None:
            blockers.append("human_decision_missing")
        elif not approved:
            blockers.append("human_rejected")
        return {
            "fingerprint": requested,
            "current_fingerprint": current_fingerprint,
            "operation_id": operation_id,
            "operation": operation,
            "operation_matches": operation_matches,
            "verdict_receipt": verdict,
            "manifest": manifest,
            "source_fresh": source_fresh,
            "human_decision": decision,
            "ready": ready,
            "blockers": blockers,
            "publishing_performed": False,
        }

    @staticmethod
    def _request_payload(source: dict[str, str], fingerprint: str) -> dict[str, Any]:
        return {
            "schema": REQUEST_SCHEMA,
            "fingerprint": fingerprint,
            "source": source,
            "publishing_requested": False,
        }

    @staticmethod
    def _request_from_event(row: Any) -> dict[str, Any] | None:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            return None
        source = payload.get("source") if isinstance(payload, dict) else None
        fingerprint = str(payload.get("fingerprint") or "") if isinstance(payload, dict) else ""
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != REQUEST_SCHEMA
            or payload.get("publishing_requested") is not False
            or not isinstance(source, dict)
            or not SHA256.fullmatch(fingerprint)
            or stable_sha256(source) != fingerprint
            or str(row["task_id"] or "") != f"release-gate:{fingerprint}"
        ):
            return None
        return {
            "event_id": str(row["event_id"]),
            "chain_sha256": str(row["chain_sha256"]),
            "fingerprint": fingerprint,
            "source": source,
        }

    def _matching_request(self, fingerprint: str) -> dict[str, Any] | None:
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT event_id, task_id, payload_json, chain_sha256
                    FROM events WHERE scope=? AND actor=? AND event_type=? AND task_id=?
                    ORDER BY sequence""",
                (
                    self.bridge.scope,
                    HUMAN_OPERATOR_ID,
                    REQUEST_EVENT_TYPE,
                    self._operation_id(fingerprint),
                ),
            ).fetchall()
        for row in rows:
            request = self._request_from_event(row)
            if request is not None and request["fingerprint"] == fingerprint:
                return request
        return None

    def _materialize(
        self,
        source: dict[str, str],
        fingerprint: str,
    ) -> dict[str, Any]:
        spec = self._operation_spec(fingerprint)
        try:
            existing = self.queue.get_operation(spec["operation_id"])
        except OperationQueueError as exc:
            if "does not exist" not in str(exc):
                raise ReleaseGateError(str(exc)) from exc
        else:
            if not self._operation_matches(existing, spec):
                raise ReleaseGateError(
                    "release gate operation id conflicts with another workflow"
                )
        manifest_path, manifest_sha256 = self._write_manifest(source, fingerprint)
        try:
            operation, created = self.queue.ensure(
                **spec,
            )
        except OperationQueueError as exc:
            raise ReleaseGateError(str(exc)) from exc
        result = self.status(fingerprint)
        result.update(
            {
                "created": created,
                "operation": operation,
                "manifest_sha256": manifest_sha256,
            }
        )
        return result

    def request(self) -> dict[str, Any]:
        self._require_ui_authority()
        source = self._source()
        fingerprint = self._fingerprint(source)
        spec = self._operation_spec(fingerprint)
        try:
            existing_operation = self.queue.get_operation(spec["operation_id"])
        except OperationQueueError as exc:
            if "does not exist" not in str(exc):
                raise ReleaseGateError(str(exc)) from exc
        else:
            if not self._operation_matches(existing_operation, spec):
                raise ReleaseGateError(
                    "release gate operation id conflicts with another workflow"
                )
        existing = self._matching_request(fingerprint)
        if existing is None:
            with self.bridge._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """SELECT event_id, task_id, payload_json, chain_sha256
                        FROM events WHERE scope=? AND actor=? AND event_type=? AND task_id=?
                        ORDER BY sequence""",
                    (
                        self.bridge.scope,
                        HUMAN_OPERATOR_ID,
                        REQUEST_EVENT_TYPE,
                        self._operation_id(fingerprint),
                    ),
                ).fetchall()
                existing = next(
                    (
                        request
                        for row in rows
                        if (request := self._request_from_event(row)) is not None
                    ),
                    None,
                )
                if existing is None:
                    event = self.bridge._event(
                        connection,
                        REQUEST_EVENT_TYPE,
                        self._request_payload(source, fingerprint),
                        self._operation_id(fingerprint),
                    )
                    existing = {
                        "event_id": event["event_id"],
                        "chain_sha256": event["chain_sha256"],
                        "fingerprint": fingerprint,
                        "source": source,
                    }
                    created = True
                else:
                    created = False
        else:
            created = False
        result = self.status(fingerprint)
        result.update(
            {
                "created": created,
                "request_recorded": True,
                "request_event": existing,
                "materialization_pending": result["operation"] is None,
            }
        )
        return result

    def materialize_pending_requests(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if self.bridge.agent_id != CONTROL_ROOM_WORKFLOW_ID:
            raise ReleaseGateError(
                "only control-room-workflow may materialize release requests"
            )
        audit = self.bridge.verify_audit_chain()
        if not audit["valid"]:
            raise ReleaseGateError("release request audit chain is invalid")
        bounded_limit = max(1, min(int(limit), 100))
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT event_id, task_id, payload_json, chain_sha256
                    FROM events WHERE scope=? AND actor=? AND event_type=?
                    ORDER BY sequence DESC LIMIT ?""",
                (
                    self.bridge.scope,
                    HUMAN_OPERATOR_ID,
                    REQUEST_EVENT_TYPE,
                    bounded_limit,
                ),
            ).fetchall()
        created_results = []
        for row in reversed(rows):
            request = self._request_from_event(row)
            if request is None:
                raise ReleaseGateError("release request event is invalid")
            result = self._materialize(
                dict(request["source"]),
                str(request["fingerprint"]),
            )
            if result["created"]:
                result["request_event"] = request
                created_results.append(result)
        return created_results

    def decide(
        self,
        fingerprint: str,
        *,
        decision: Literal["approve", "reject"],
        reason: str,
    ) -> dict[str, Any]:
        self._require_ui_authority()
        if decision not in {"approve", "reject"}:
            raise ReleaseGateError("release gate decision is invalid")
        clean_reason = str(reason or "").strip()
        if not clean_reason or len(clean_reason) > 1_000:
            raise ReleaseGateError("release gate decision reason is invalid")
        if contains_secret(clean_reason):
            raise ReleaseGateError("release gate decision reason contains credential-like data")
        before = self.status(fingerprint)
        if (
            before["operation"] is None
            or not before["manifest"]["valid"]
            or not before["operation_matches"]
        ):
            raise ReleaseGateError("release gate must exist before a human decision")
        if not before["source_fresh"] or not before["verdict_receipt"]["valid"]:
            raise ReleaseGateError(
                "release gate must have two fresh affirmative reviews before a decision"
            )
        statement_payload = {
            "schema": DECISION_SCHEMA,
            "decision": decision,
            "fingerprint": str(before["fingerprint"]),
            "operation_sha256": str(before["operation"]["operation_sha256"]),
            "verdict_sha256": str(before["verdict_receipt"]["sha256"]),
            "reason": clean_reason,
        }
        statement = DECISION_HEADER + "\n" + json.dumps(
            statement_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            record = self.timeline.record(
                record_id=f"release-decision:{uuid.uuid4().hex}",
                task_id=str(before["operation_id"]),
                stage="decision",
                statement=statement,
                artifact_paths=[str(before["manifest"]["path"])],
            )
        except TrustTimelineError as exc:
            raise ReleaseGateError(str(exc)) from exc
        event_payload = {
            "schema": DECISION_SCHEMA,
            "fingerprint": str(before["fingerprint"]),
            "operation_sha256": str(before["operation"]["operation_sha256"]),
            "verdict_sha256": str(before["verdict_receipt"]["sha256"]),
            "manifest_sha256": str(before["manifest"]["sha256"]),
            "record_id": str(record["record_id"]),
            "trust_sha256": str(record["trust_sha256"]),
            "decision": decision,
        }
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = self.bridge._event(
                connection,
                DECISION_EVENT_TYPE,
                event_payload,
                str(before["operation_id"]),
            )
        result = self.status(fingerprint)
        result["decision_record"] = record
        result["decision_event"] = event
        return result


__all__ = [
    "RELEASE_GATE_ROOT",
    "REQUEST_EVENT_TYPE",
    "ReleaseGateError",
    "ReleaseGateService",
]
