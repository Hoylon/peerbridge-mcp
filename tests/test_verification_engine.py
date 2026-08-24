from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from peerbridge_mcp.bridge import CONTROL_ROOM_WORKFLOW_ID, Bridge
from peerbridge_mcp.execution_governance import (
    ExecutionGovernance,
    repository_resource_key,
)
from peerbridge_mcp.operation_queue import DurableOperationQueue
from peerbridge_mcp.release_gate import ReleaseGateService
from peerbridge_mcp.trust_timeline import TrustTimeline
from peerbridge_mcp.verification_engine import (
    VerificationTrigger,
    VerificationTriggerEngine,
)


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
    (root / "source.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    return root


def _bridge(root: Path, agent_id: str) -> Bridge:
    return Bridge(
        root,
        root / ".peerbridge" / "verification.sqlite3",
        agent_id,
        "verification-test",
    )


@pytest.fixture
def short_local_app_data():
    with tempfile.TemporaryDirectory(prefix="pbve-") as directory:
        yield Path(directory)


def test_engine_starts_independently_and_deduplicates_stale_trust_work(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    human = _bridge(root, "human-operator")
    record = TrustTimeline(human).record(
        task_id="trust-task",
        stage="test",
        statement="The baseline source passed its bounded test.",
        artifact_paths=["source.txt"],
    )
    (root / "source.txt").write_text("changed\n", encoding="utf-8")
    service = _bridge(root, CONTROL_ROOM_WORKFLOW_ID)
    engine = VerificationTriggerEngine(
        service,
        max_new_operations_per_scan=10,
        max_open_automatic_operations=20,
    )

    first = engine.scan_once(now_epoch=1_000)
    second = engine.scan_once(now_epoch=1_001)
    operations = DurableOperationQueue(service).list_operations()
    assert first["last_counts"]["created"] == 1
    assert second["last_counts"]["created"] == 0
    assert second["last_counts"]["already_present"] == 1
    assert len(operations) == 1
    assert operations[0]["operation_id"].startswith("auto:trust:")
    assert record["record_id"] in operations[0]["task_text"]
    assert operations[0]["max_attempts"] == 1

    DurableOperationQueue(human).request_cancel(
        operations[0]["operation_id"], reason="Close the first bounded drift check."
    )
    (root / "source.txt").write_text("changed again\n", encoding="utf-8")
    third = engine.scan_once(now_epoch=1_002)
    drift_operations = [
        row
        for row in DurableOperationQueue(service).list_operations()
        if row["operation_id"].startswith("auto:trust:")
    ]
    assert third["last_counts"]["created"] == 1
    assert len(drift_operations) == 2
    assert len({row["operation_id"] for row in drift_operations}) == 2

    manual_scan_count = engine.status()["scan_count"]
    engine.start()
    deadline = time.time() + 3
    while (
        time.time() < deadline
        and engine.status()["scan_count"] <= manual_scan_count
    ):
        time.sleep(0.02)
    status = engine.status()
    assert status["thread_alive"] is True
    assert status["state"] == "running"
    engine.close()
    assert engine.status()["state"] == "stopped"


def test_engine_does_not_hide_an_older_stale_record_behind_a_fresh_record(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "fresh.txt").write_text("still fresh\n", encoding="utf-8")
    human = _bridge(root, "human-operator")
    timeline = TrustTimeline(human)
    stale = timeline.record(
        task_id="multi-record-task",
        stage="test",
        statement="The original source passed its bounded test.",
        artifact_paths=["source.txt"],
    )
    fresh = timeline.record(
        task_id="multi-record-task",
        stage="review",
        statement="The independent review remains bound to its own source.",
        artifact_paths=["fresh.txt"],
    )
    (root / "source.txt").write_text("changed after the test\n", encoding="utf-8")

    service = _bridge(root, CONTROL_ROOM_WORKFLOW_ID)
    result = VerificationTriggerEngine(
        service,
        max_new_operations_per_scan=10,
        max_open_automatic_operations=20,
    ).scan_once(now_epoch=1_000)
    operations = DurableOperationQueue(service).list_operations()

    assert result["last_counts"]["created"] == 1
    assert len(operations) == 1
    assert stale["record_id"] in operations[0]["task_text"]
    assert fresh["record_id"] not in operations[0]["task_text"]


def test_engine_materializes_an_explicit_release_request_without_opening_trust_ui(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    requested = ReleaseGateService.for_control_room_ui(
        _bridge(root, "human-operator")
    ).request()
    service = _bridge(root, CONTROL_ROOM_WORKFLOW_ID)
    engine = VerificationTriggerEngine(
        service,
        max_new_operations_per_scan=10,
        max_open_automatic_operations=20,
    )

    first = engine.scan_once(now_epoch=1_000)
    second = engine.scan_once(now_epoch=1_001)
    operations = DurableOperationQueue(service).list_operations()

    assert first["last_counts"]["materialized_release_requests"] == 1
    assert second["last_counts"]["materialized_release_requests"] == 0
    assert len(operations) == 1
    assert operations[0]["operation_id"] == requested["operation_id"]
    assert operations[0]["workflow_id"] == "release-gate"
    assert operations[0]["requested_by"] == CONTROL_ROOM_WORKFLOW_ID
    assert requested["publishing_performed"] is False


def test_engine_creates_bounded_permission_execution_and_failure_checks(
    tmp_path: Path, monkeypatch, short_local_app_data: Path
) -> None:
    root = _repository(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(short_local_app_data))
    human = _bridge(root, "human-operator")
    governance = ExecutionGovernance(human)
    now = time.time()
    governance.decide_permission(
        decision_id="pending-permission",
        task_id="pending-task",
        agent_id="writer",
        action="git.worktree.create",
        resource_key=repository_resource_key(root),
        decision="allow",
        reason="Prepare one bounded isolated worktree.",
        expires_epoch=now + 600,
        now_epoch=now,
    )

    worktree_permission = governance.decide_permission(
        decision_id="binding-permission",
        task_id="binding-task",
        agent_id="binding-writer",
        action="git.worktree.create",
        resource_key=repository_resource_key(root),
        decision="allow",
        reason="Create one isolated source binding.",
        expires_epoch=now + 600,
        now_epoch=now,
    )
    binding = governance.create_isolated_worktree(
        binding_id="stale-binding",
        task_id="binding-task",
        agent_id="binding-writer",
        permission_decision_id=worktree_permission["decision_id"],
        repository=root,
    )
    Path(binding["worktree_path"], "source.txt").write_text(
        "stale worktree source\n", encoding="utf-8"
    )

    manual = DurableOperationQueue(human).enqueue(
        operation_id="failed-manual-check",
        workflow_id="read-only-audit",
        task_text="Run a manual check that will fail in this test.",
        working_directory=".",
        resource_key="repo:failed-check",
        max_attempts=1,
        timeout_seconds=60,
        not_before_epoch=0,
    )
    manual_queue = DurableOperationQueue(human)
    claim = manual_queue.claim("failing-worker", now_epoch=now, lease_seconds=60)
    assert claim is not None and claim.operation["operation_id"] == manual["operation_id"]
    manual_queue.fail(
        manual["operation_id"],
        "failing-worker",
        claim.lease_token,
        error_class="configuration",
        detail="The bounded test failure is intentional.",
        now_epoch=now + 1,
    )

    service = _bridge(root, CONTROL_ROOM_WORKFLOW_ID)
    engine = VerificationTriggerEngine(
        service,
        max_new_operations_per_scan=10,
        max_open_automatic_operations=20,
    )
    result = engine.scan_once(now_epoch=now + 2)
    automatic = [
        row
        for row in DurableOperationQueue(service).list_operations(limit=20)
        if row["requested_by"] == CONTROL_ROOM_WORKFLOW_ID
    ]
    kinds = {row["operation_id"].split(":", 2)[1] for row in automatic}
    assert result["last_counts"]["created"] == 3
    assert kinds == {"permission", "execution", "failed"}

    repeated = engine.scan_once(now_epoch=now + 3)
    assert repeated["last_counts"]["created"] == 0
    assert repeated["last_counts"]["already_present"] == 3


def test_engine_rotates_trigger_kinds_when_scan_creation_budget_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    engine = VerificationTriggerEngine(
        _bridge(root, CONTROL_ROOM_WORKFLOW_ID),
        max_new_operations_per_scan=1,
        max_open_automatic_operations=10,
        max_candidates_per_scan=4,
    )

    def trigger(kind: str) -> VerificationTrigger:
        return VerificationTrigger(
            kind=kind,
            subject_id=f"{kind}-subject",
            source_sha256=(kind[0] * 64),
            task_text=f"Verify the bounded {kind} candidate.",
        )

    monkeypatch.setattr(
        engine,
        "_permission_triggers",
        lambda _now, *, limit: [trigger("permission")][:limit],
    )
    monkeypatch.setattr(
        engine,
        "_execution_triggers",
        lambda *, limit: [trigger("execution")][:limit],
    )
    monkeypatch.setattr(
        engine,
        "_trust_triggers",
        lambda *, limit: [trigger("trust")][:limit],
    )
    monkeypatch.setattr(
        engine,
        "_failed_operation_triggers",
        lambda *, limit: [trigger("failed")][:limit],
    )

    for offset in range(4):
        result = engine.scan_once(now_epoch=1_000 + offset)
        assert result["last_counts"]["created"] == 1
    kinds = {
        row["operation_id"].split(":", 2)[1]
        for row in DurableOperationQueue(engine.bridge).list_operations()
    }
    assert kinds == {"permission", "execution", "trust", "failed"}


def test_engine_close_does_not_report_stopped_while_thread_is_alive(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    engine = VerificationTriggerEngine(_bridge(root, CONTROL_ROOM_WORKFLOW_ID))

    class StuckThread:
        def join(self, timeout: float) -> None:
            assert timeout == 0.0

        @staticmethod
        def is_alive() -> bool:
            return True

    engine._thread = StuckThread()  # type: ignore[assignment]
    engine.close(wait_seconds=0)
    status = engine.status()
    assert status["state"] == "stop-timeout"
    assert status["thread_alive"] is True
    assert "did not stop" in status["last_error"]
