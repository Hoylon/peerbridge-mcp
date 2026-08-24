from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.operation_queue import (
    RELEASE_GATE_RECEIPT_SCHEMA,
    RELEASE_GATE_TERMINAL_OUTCOME,
    DurableOperationQueue,
    OperationQueueError,
)
from peerbridge_mcp.release_gate import ReleaseGateError, ReleaseGateService
from peerbridge_mcp.trust_timeline import TrustTimeline


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "PeerBridge Tests")
    (root / ".gitignore").write_text(
        ".peerbridge/\n.peerbridge-artifacts/\n", encoding="utf-8"
    )
    (root / "source.txt").write_text("release candidate\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    return root


def _bridge(root: Path, agent_id: str = "human-operator") -> Bridge:
    return Bridge(
        root,
        root / ".peerbridge" / "release-gate.sqlite3",
        agent_id,
        "release-gate-test",
    )


def _service(root: Path) -> ReleaseGateService:
    return ReleaseGateService.for_control_room_ui(_bridge(root))


def _request_and_materialize(service: ReleaseGateService) -> dict[str, object]:
    requested = service.request()
    materializer = ReleaseGateService(
        _bridge(service.bridge.root, "control-room-workflow")
    )
    materializer.materialize_pending_requests()
    return service.status(str(requested["fingerprint"]))


def _complete_gate(service: ReleaseGateService, fingerprint: str) -> None:
    queue = DurableOperationQueue(_bridge(service.bridge.root, "control-room-workflow"))
    claim = queue.claim("release-test-worker", now_epoch=1_000, lease_seconds=60)
    assert claim is not None
    receipt = {
        "schema": RELEASE_GATE_RECEIPT_SCHEMA,
        "operation_id": claim.operation["operation_id"],
        "source_fingerprint": fingerprint,
        "reviews": [
            {
                "agent_id": "codex-reviewer",
                "session_id": "codex-session",
                "role": "reviewer",
                "decision": "approve",
                "answer_sha256": "a" * 64,
            },
            {
                "agent_id": "claude-auditor",
                "session_id": "claude-session",
                "role": "auditor",
                "decision": "approve",
                "answer_sha256": "b" * 64,
            },
        ],
    }
    queue.complete(
        claim.operation["operation_id"],
        "release-test-worker",
        claim.lease_token,
        outcome=RELEASE_GATE_TERMINAL_OUTCOME,
        detail=json.dumps(
            receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ),
        now_epoch=1_001,
    )


def test_release_request_is_exact_idempotent_and_never_publishes(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    service = _service(root)

    first = service.request()
    second = service.request()
    assert first["created"] is True
    assert second["created"] is False
    assert second["operation_id"] == first["operation_id"]
    assert second["manifest"]["valid"] is False
    assert second["materialization_pending"] is True
    assert len(DurableOperationQueue(service.bridge).list_operations()) == 0

    materialized = ReleaseGateService(
        _bridge(root, "control-room-workflow")
    ).materialize_pending_requests()
    assert len(materialized) == 1
    ready_for_review = service.status(first["fingerprint"])
    assert ready_for_review["manifest"]["valid"] is True
    assert second["ready"] is False
    assert second["publishing_performed"] is False
    assert len(DurableOperationQueue(service.bridge).list_operations()) == 1
    assert _git(root, "tag", "--list") == ""
    assert _git(root, "remote") == ""

    with pytest.raises(ReleaseGateError, match="two fresh affirmative reviews"):
        service.decide(
            first["fingerprint"],
            decision="approve",
            reason="The source has not passed its gate yet.",
        )

    _complete_gate(service, first["fingerprint"])
    approved = service.decide(
        first["fingerprint"],
        decision="approve",
        reason="I reviewed the exact successful gate and approve this source.",
    )
    assert approved["ready"] is True
    assert approved["human_decision"]["decision"] == "approve"
    assert approved["publishing_performed"] is False

    (root / "source.txt").write_text("changed after approval\n", encoding="utf-8")
    stale = service.status(first["fingerprint"])
    assert stale["ready"] is False
    assert stale["source_fresh"] is False
    assert "source_changed" in stale["blockers"]


def test_release_gate_rejects_non_human_requests_and_secret_decisions(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    with pytest.raises(ReleaseGateError, match="human-operator"):
        ReleaseGateService.for_control_room_ui(_bridge(root, "worker"))
    with pytest.raises(ReleaseGateError, match="direct Control Room UI action"):
        ReleaseGateService(_bridge(root)).request()

    service = _service(root)
    requested = _request_and_materialize(service)
    _complete_gate(service, requested["fingerprint"])
    with pytest.raises(ReleaseGateError, match="credential-like"):
        service.decide(
            requested["fingerprint"],
            decision="approve",
            reason="api_key=realistic-secret-value-123",
        )


def test_release_gate_rejects_invalid_verdict_receipt(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    service = _service(root)
    requested = _request_and_materialize(service)
    queue = DurableOperationQueue(_bridge(root, "control-room-workflow"))
    claim = queue.claim("release-test-worker", now_epoch=1_000, lease_seconds=60)
    assert claim is not None
    incomplete_receipt = {
        "schema": RELEASE_GATE_RECEIPT_SCHEMA,
        "operation_id": claim.operation["operation_id"],
        "source_fingerprint": requested["fingerprint"],
        "reviews": [
            {
                "agent_id": "codex-reviewer",
                "session_id": "codex-session",
                "role": "reviewer",
                "decision": "approve",
                "answer_sha256": "a" * 64,
            }
        ],
    }

    with pytest.raises(OperationQueueError, match="receipt is invalid"):
        queue.complete(
            claim.operation["operation_id"],
            "release-test-worker",
            claim.lease_token,
            outcome=RELEASE_GATE_TERMINAL_OUTCOME,
            detail=json.dumps(
                incomplete_receipt,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            now_epoch=1_001,
        )
    status = service.status(requested["fingerprint"])
    assert status["ready"] is False
    assert status["verdict_receipt"]["valid"] is False


def test_release_gate_rejects_conflicting_operation(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    service = _service(root)
    fingerprint = service._fingerprint(service._source())
    queue = DurableOperationQueue(_bridge(root, "control-room-workflow"))
    queue.ensure(
        operation_id=f"release-gate:{fingerprint}",
        workflow_id="read-only-audit",
        task_text="A conflicting workflow must not become a Release Gate.",
        working_directory=".",
        resource_key=f"release:{fingerprint}",
        requested_by="control-room-workflow",
        max_attempts=1,
        timeout_seconds=3_600,
        not_before_epoch=0.0,
    )

    with pytest.raises(ReleaseGateError, match="conflicts with another workflow"):
        service.request()


def test_release_gate_ignores_prewritten_decision_and_honors_fresh_rejection(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    service = _service(root)
    requested = _request_and_materialize(service)
    TrustTimeline(service.bridge).record(
        record_id="premature-release-decision",
        task_id=requested["operation_id"],
        stage="decision",
        statement="Approve this gate before its independent verdicts exist.",
        artifact_paths=[requested["manifest"]["path"]],
    )
    _complete_gate(service, requested["fingerprint"])

    before = service.status(requested["fingerprint"])
    assert before["human_decision"] is None
    assert before["ready"] is False
    rejected = service.decide(
        requested["fingerprint"],
        decision="reject",
        reason="The exact reviewed source is not ready to publish.",
    )
    assert rejected["ready"] is False
    assert rejected["human_decision"]["decision"] == "reject"
    assert "human_rejected" in rejected["blockers"]
