from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.execution_governance import (
    ExecutionGovernance,
    repository_resource_key,
)
from peerbridge_mcp.managed_agents import ManagedAgentLaunch, ManagedAgentManager
from peerbridge_mcp.managed_workflows import ManagedWorkflowError, ManagedWorkflowRunner
from peerbridge_mcp.operation_queue import (
    RELEASE_GATE_RECEIPT_SCHEMA,
    RELEASE_GATE_TERMINAL_OUTCOME,
    RELEASE_REVIEW_VERDICT_MARKER,
    RELEASE_REVIEW_VERDICT_SCHEMA,
    DurableOperationQueue,
    OperationQueueError,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
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
    (root / ".gitignore").write_text(".peerbridge/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    return root


def _bridges(root: Path, scope: str = "workflow-test") -> tuple[Bridge, Bridge]:
    database = root / ".peerbridge" / "workflow.sqlite3"
    return (
        Bridge(root, database, "human-operator", scope),
        Bridge(root, database, "control-room-workflow", scope),
    )


@pytest.fixture
def short_local_app_data():
    with tempfile.TemporaryDirectory(prefix="peerbridge-alpha52-test-") as directory:
        yield Path(directory)


def _provider_success_script(
    agent_id: str,
    answer: str,
    *,
    prelude: str = "",
) -> str:
    if agent_id == "codex":
        events = [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": answer},
            },
            {"type": "turn.completed"},
        ]
    elif agent_id == "claude-code":
        events = [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "terminal_reason": "completed",
                "result": answer,
            }
        ]
    else:
        raise AssertionError(f"unsupported test Agent: {agent_id}")
    return (
        "import json,sys; sys.stdin.read(); "
        f"{prelude}"
        f"events={events!r}; "
        "[print(json.dumps(event), flush=True) for event in events]"
    )


def _launch_builder(
    agent_id: str,
    *,
    session_id: str,
    role: str,
    working_directory: Path,
    execution_mode: str,
    governance_binding_id: str | None = None,
    isolation_verified: bool = False,
) -> ManagedAgentLaunch:
    if execution_mode == "isolated-write":
        assert isolation_verified is True
        prelude = (
            "import pathlib; "
            "path=pathlib.Path('tracked.txt'); "
            "path.write_text(path.read_text(encoding='utf-8')+'managed writer\\n', "
            "encoding='utf-8'); "
        )
        script = _provider_success_script(agent_id, session_id, prelude=prelude)
    else:
        script = _provider_success_script(
            agent_id,
            session_id,
            prelude="import time; time.sleep(.05); ",
        )
    return ManagedAgentLaunch(
        session_id=session_id,
        agent_id=agent_id,
        display_name=agent_id,
        role=role,
        executable=Path(sys.executable),
        arguments=("-u", "-c", script),
        working_directory=working_directory,
        execution_mode=execution_mode,
        permission_tier="edit" if execution_mode == "isolated-write" else "observe",
        governance_binding_id=governance_binding_id,
        isolation_boundary=(
            "test-isolated-worktree-v1"
            if execution_mode == "isolated-write"
            else None
        ),
    )


def _slow_launch_builder(
    agent_id: str,
    *,
    session_id: str,
    role: str,
    working_directory: Path,
    execution_mode: str,
    governance_binding_id: str | None = None,
    isolation_verified: bool = False,
) -> ManagedAgentLaunch:
    assert execution_mode == "observe"
    return ManagedAgentLaunch(
        session_id=session_id,
        agent_id=agent_id,
        display_name=agent_id,
        role=role,
        executable=Path(sys.executable),
        arguments=("-u", "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"),
        working_directory=working_directory,
        execution_mode="observe",
        governance_binding_id=governance_binding_id,
    )


class _FailingCompleteQueue:
    def __init__(self, delegate: DurableOperationQueue) -> None:
        self.delegate = delegate
        self.heartbeat_count = 0

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    def heartbeat(self, *args, **kwargs):
        self.heartbeat_count += 1
        return self.delegate.heartbeat(*args, **kwargs)

    def complete(self, *args, **kwargs):
        raise OperationQueueError("injected final queue transition failure")


class _FailingLiveReadQueue:
    def __init__(self, delegate: DurableOperationQueue) -> None:
        self.delegate = delegate
        self.read_count = 0

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    def get_operation(self, *args, **kwargs):
        self.read_count += 1
        raise OperationQueueError("injected live queue read failure")


def _release_launch_builder(
    fingerprint: str,
    decisions: dict[str, str | None],
):
    def build(
        agent_id: str,
        *,
        session_id: str,
        role: str,
        working_directory: Path,
        execution_mode: str,
        governance_binding_id: str | None = None,
        isolation_verified: bool = False,
    ) -> ManagedAgentLaunch:
        assert execution_mode == "observe"
        decision = decisions[role]
        if decision is None:
            answer = f"{role} completed without an explicit verdict."
        else:
            verdict = json.dumps(
                {
                    "schema": RELEASE_REVIEW_VERDICT_SCHEMA,
                    "decision": decision,
                    "source_fingerprint": fingerprint,
                    "role": role,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            answer = f"{role} reviewed the exact source.\n{RELEASE_REVIEW_VERDICT_MARKER} {verdict}"
        script = _provider_success_script(agent_id, answer)
        return ManagedAgentLaunch(
            session_id=session_id,
            agent_id=agent_id,
            display_name=agent_id,
            role=role,
            executable=Path(sys.executable),
            arguments=("-u", "-c", script),
            working_directory=working_directory,
            execution_mode="observe",
            governance_binding_id=governance_binding_id,
        )

    return build


def _wait_status(
    queue: DurableOperationQueue,
    operation_id: str,
    expected: str,
    *,
    timeout: float = 20.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        last = queue.get_operation(operation_id)
        if last["status"] == expected:
            return last
        time.sleep(0.05)
    raise AssertionError(f"operation did not reach {expected}: {last}")


def test_workflow_directory_allows_root_and_rejects_protected_subdirectory(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    _human, worker = _bridges(root, "workflow-directory")
    runner = ManagedWorkflowRunner(
        worker,
        ManagedAgentManager(max_sessions=1),
        available_agent_ids=("codex",),
    )

    assert runner._project_directory(".") == root.resolve()
    with pytest.raises(ManagedWorkflowError, match="protected"):
        runner._project_directory(".git")


def test_runner_fails_closed_when_final_queue_transition_errors(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    human, worker = _bridges(root, "workflow-queue-error")
    queue = DurableOperationQueue(human)
    queue.enqueue(
        workflow_id="read-only-audit",
        task_text="Complete one bounded audit before the injected queue error.",
        working_directory=".",
        resource_key="repo:queue-error",
        operation_id="queue-error-observe",
        timeout_seconds=30,
    )
    runner = ManagedWorkflowRunner(
        worker,
        ManagedAgentManager(max_sessions=1),
        launch_builder=_launch_builder,
        available_agent_ids=("codex",),
        lease_seconds=5,
        heartbeat_seconds=1,
        poll_seconds=0.05,
    )
    injected_queue = _FailingCompleteQueue(runner.queue)
    runner.queue = injected_queue
    try:
        runner.start()
        operation = _wait_status(queue, "queue-error-observe", "failed")
    finally:
        runner.close()

    assert injected_queue.heartbeat_count >= 1
    assert operation["terminal_outcome"] == "queue-state"


def test_runner_stops_live_session_when_queue_read_fails(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    human, worker = _bridges(root, "workflow-live-read-error")
    queue = DurableOperationQueue(human)
    queue.enqueue(
        workflow_id="read-only-audit",
        task_text="Keep one Agent alive until the injected queue read fails.",
        working_directory=".",
        resource_key="repo:live-read-error",
        operation_id="live-read-error-observe",
        max_attempts=1,
        timeout_seconds=30,
    )
    manager = ManagedAgentManager(max_sessions=1)
    runner = ManagedWorkflowRunner(
        worker,
        manager,
        launch_builder=_slow_launch_builder,
        available_agent_ids=("codex",),
        lease_seconds=5,
        heartbeat_seconds=1,
        poll_seconds=0.05,
    )
    injected_queue = _FailingLiveReadQueue(runner.queue)
    runner.queue = injected_queue
    try:
        runner.start()
        operation = _wait_status(queue, "live-read-error-observe", "failed")
    finally:
        runner.close()

    snapshots = manager.snapshots()
    assert injected_queue.read_count == 1
    assert operation["attempt_count"] == 1
    assert operation["terminal_outcome"] == "queue-state"
    assert len(snapshots) == 1
    assert snapshots[0]["state"] == "stopped"


def test_partial_group_launch_failure_stops_the_first_agent(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    human, worker = _bridges(root, "workflow-partial-launch")
    queue = DurableOperationQueue(human)
    queue.enqueue(
        workflow_id="investigate-debate",
        task_text="Start a bounded group that exceeds the injected manager limit.",
        working_directory=".",
        resource_key="repo:partial-launch",
        operation_id="partial-group-launch",
        max_attempts=1,
        timeout_seconds=30,
    )
    manager = ManagedAgentManager(max_sessions=1)
    runner = ManagedWorkflowRunner(
        worker,
        manager,
        launch_builder=_slow_launch_builder,
        available_agent_ids=("codex", "claude-code"),
        lease_seconds=5,
        heartbeat_seconds=1,
        poll_seconds=0.05,
    )
    try:
        runner.start()
        operation = _wait_status(queue, "partial-group-launch", "failed")
    finally:
        runner.close()

    snapshots = manager.snapshots()
    assert operation["terminal_outcome"] == "configuration"
    assert len(snapshots) == 1
    assert snapshots[0]["state"] == "stopped"


@pytest.mark.parametrize(
    ("decisions", "expected_status", "expected_outcome"),
    [
        ({"auditor": "approve", "reviewer": "approve"}, "succeeded", RELEASE_GATE_TERMINAL_OUTCOME),
        ({"auditor": "approve", "reviewer": "reject"}, "failed", "review-blocked"),
        ({"auditor": "approve", "reviewer": None}, "failed", "configuration"),
    ],
)
def test_release_gate_requires_two_explicit_affirmative_verdicts(
    tmp_path: Path,
    decisions: dict[str, str | None],
    expected_status: str,
    expected_outcome: str,
) -> None:
    root = _repository(tmp_path)
    human, worker = _bridges(root, f"workflow-release-{expected_outcome}")
    fingerprint = "a" * 64
    queue = DurableOperationQueue(human)
    queue.enqueue(
        workflow_id="release-gate",
        task_text="Inspect this exact source without publishing it.",
        working_directory=".",
        resource_key=f"release:{fingerprint}",
        operation_id=f"release-gate-{expected_outcome}",
        max_attempts=1,
        timeout_seconds=30,
    )
    manager = ManagedAgentManager(max_sessions=2)
    runner = ManagedWorkflowRunner(
        worker,
        manager,
        launch_builder=_release_launch_builder(fingerprint, decisions),
        available_agent_ids=("codex", "claude-code"),
        lease_seconds=5,
        heartbeat_seconds=1,
        poll_seconds=0.05,
    )
    try:
        runner.start()
        operation = _wait_status(
            queue, f"release-gate-{expected_outcome}", expected_status
        )
    finally:
        runner.close()

    assert operation["terminal_outcome"] == expected_outcome
    assert len(manager.snapshots()) == 2
    if expected_status == "succeeded":
        receipt = json.loads(str(operation["terminal_detail"]))
        assert receipt["schema"] == RELEASE_GATE_RECEIPT_SCHEMA
        assert receipt["source_fingerprint"] == fingerprint
        assert {review["decision"] for review in receipt["reviews"]} == {"approve"}
        assert len({review["agent_id"] for review in receipt["reviews"]}) == 2


def test_runner_retry_uses_attempt_scoped_session_identity(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    human, worker = _bridges(root, "workflow-retry")
    queue = DurableOperationQueue(human)
    queue.enqueue(
        workflow_id="read-only-audit",
        task_text="Retry once after a provider process failure.",
        working_directory=".",
        resource_key="repo:retry",
        operation_id="retry-observe",
        max_attempts=2,
        timeout_seconds=30,
    )

    def retry_launch_builder(
        agent_id: str,
        *,
        session_id: str,
        role: str,
        working_directory: Path,
        execution_mode: str,
        governance_binding_id: str | None = None,
        isolation_verified: bool = False,
    ) -> ManagedAgentLaunch:
        assert execution_mode == "observe"
        script = (
            "import sys; sys.stdin.read(); sys.exit(7)"
            if "-a1-" in session_id
            else _provider_success_script(agent_id, "retry completed")
        )
        return ManagedAgentLaunch(
            session_id=session_id,
            agent_id=agent_id,
            display_name=agent_id,
            role=role,
            executable=Path(sys.executable),
            arguments=("-u", "-c", script),
            working_directory=working_directory,
            execution_mode="observe",
            governance_binding_id=governance_binding_id,
        )

    manager = ManagedAgentManager(max_sessions=2)
    runner = ManagedWorkflowRunner(
        worker,
        manager,
        launch_builder=retry_launch_builder,
        available_agent_ids=("codex",),
        lease_seconds=5,
        heartbeat_seconds=1,
        poll_seconds=0.05,
    )
    original_fail_claim = runner._fail_claim

    def fail_claim_without_delay(claim, **kwargs) -> None:
        original_fail_claim(claim, **{**kwargs, "retry_after_seconds": 0})

    runner._fail_claim = fail_claim_without_delay
    try:
        runner.start()
        operation = _wait_status(queue, "retry-observe", "succeeded")
    finally:
        runner.close()

    snapshots = manager.snapshots()
    assert operation["attempt_count"] == 2
    assert [item["state"] for item in snapshots] == ["failed", "completed"]
    assert "-a1-" in snapshots[0]["session_id"]
    assert "-a2-" in snapshots[1]["session_id"]


def test_runner_completes_three_stable_observe_sessions(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    human, worker = _bridges(root)
    queue = DurableOperationQueue(human)
    queue.enqueue(
        workflow_id="investigate-debate",
        task_text="Compare the exact local source from three independent roles.",
        working_directory=".",
        resource_key="repo:investigation",
        operation_id="three-agent-observe",
        timeout_seconds=30,
    )
    manager = ManagedAgentManager(max_sessions=4)
    runner = ManagedWorkflowRunner(
        worker,
        manager,
        launch_builder=_launch_builder,
        available_agent_ids=("codex", "claude-code"),
        lease_seconds=5,
        heartbeat_seconds=1,
        poll_seconds=0.05,
    )
    try:
        runner.start()
        operation = _wait_status(queue, "three-agent-observe", "succeeded")
    finally:
        runner.close()

    assert operation["terminal_outcome"] == "managed_sessions_completed"
    snapshots = manager.snapshots()
    assert len(snapshots) == 3
    assert all(item["state"] == "completed" for item in snapshots)
    assert all(item["execution_mode"] == "observe" for item in snapshots)
    for snapshot in snapshots:
        encoded = json.dumps(snapshot)
        assert snapshot["session_id"] in encoded
        assert all(
            other["session_id"] not in encoded
            for other in snapshots
            if other is not snapshot
        )


@pytest.mark.parametrize(
    ("provider_event", "expected_snapshot_status", "expected_provider_status"),
    [
        ({"type": "turn.failed"}, "conflict", "failed"),
        (
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "No terminal event."},
            },
            "completed",
            None,
        ),
    ],
)
def test_runner_requires_explicit_provider_completion_when_process_exits_zero(
    tmp_path: Path,
    provider_event: dict[str, object],
    expected_snapshot_status: str,
    expected_provider_status: str | None,
) -> None:
    root = _repository(tmp_path)
    human, worker = _bridges(root, "workflow-provider-failure")
    queue = DurableOperationQueue(human)
    queue.enqueue(
        workflow_id="read-only-audit",
        task_text="Fail closed on the provider terminal event.",
        working_directory=".",
        resource_key="repo:provider-failure",
        operation_id="provider-failure-zero-exit",
        max_attempts=1,
        timeout_seconds=30,
    )

    def provider_failure_launch(
        agent_id: str,
        *,
        session_id: str,
        role: str,
        working_directory: Path,
        execution_mode: str,
        governance_binding_id: str | None = None,
        isolation_verified: bool = False,
    ) -> ManagedAgentLaunch:
        del isolation_verified
        script = (
            "import json,sys; sys.stdin.read(); "
            f"print(json.dumps({provider_event!r}), flush=True)"
        )
        return ManagedAgentLaunch(
            session_id=session_id,
            agent_id=agent_id,
            display_name=agent_id,
            role=role,
            executable=Path(sys.executable),
            arguments=("-u", "-c", script),
            working_directory=working_directory,
            execution_mode=execution_mode,
            governance_binding_id=governance_binding_id,
        )

    manager = ManagedAgentManager(max_sessions=1)
    runner = ManagedWorkflowRunner(
        worker,
        manager,
        launch_builder=provider_failure_launch,
        available_agent_ids=("codex",),
        lease_seconds=5,
        heartbeat_seconds=1,
        poll_seconds=0.05,
    )
    try:
        runner.start()
        operation = _wait_status(queue, "provider-failure-zero-exit", "failed")
    finally:
        runner.close()

    snapshot = manager.snapshots()[0]
    assert operation["terminal_outcome"] == "provider"
    assert snapshot["state"] == "completed"
    assert snapshot["terminal_outcome"]["status"] == expected_snapshot_status
    assert (
        snapshot["terminal_outcome"]["provider_status"]
        == expected_provider_status
    )


def test_runner_cancellation_stops_only_owned_session_tree(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    human, worker = _bridges(root, "workflow-cancel")
    queue = DurableOperationQueue(human)
    queue.enqueue(
        workflow_id="read-only-audit",
        task_text="Wait until the operator cancels this bounded audit.",
        working_directory=".",
        resource_key="repo:cancel",
        operation_id="cancel-observe",
        timeout_seconds=30,
    )
    manager = ManagedAgentManager(max_sessions=2)
    runner = ManagedWorkflowRunner(
        worker,
        manager,
        launch_builder=_slow_launch_builder,
        available_agent_ids=("codex",),
        lease_seconds=5,
        heartbeat_seconds=1,
        poll_seconds=0.05,
    )
    try:
        runner.start()
        _wait_status(queue, "cancel-observe", "running")
        queue.request_cancel("cancel-observe", reason="Operator cancelled the audit.")
        cancelled = _wait_status(queue, "cancel-observe", "cancelled")
    finally:
        runner.close()

    assert cancelled["terminal_outcome"] == "cancelled"
    assert [item["state"] for item in manager.snapshots()] == ["stopped"]


def test_implement_review_uses_human_bound_worktree_and_same_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    short_local_app_data: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(short_local_app_data))
    root = _repository(tmp_path)
    human, worker = _bridges(root, "workflow-implement")
    governance = ExecutionGovernance(human)
    now = time.time()
    permission = governance.decide_permission(
        task_id="implement-task",
        agent_id="codex",
        action="git.worktree.create",
        resource_key=repository_resource_key(root),
        decision="allow",
        reason="Allow one isolated Codex writer for the managed workflow.",
        expires_epoch=now + 600,
        now_epoch=now,
        decision_id="implement-permission",
    )
    binding = governance.create_isolated_worktree(
        task_id="implement-task",
        agent_id="codex",
        permission_decision_id=str(permission["decision_id"]),
        repository=root,
        binding_id="implement-binding",
    )
    worktree = Path(str(binding["worktree_path"]))
    queue = DurableOperationQueue(human)
    queue.enqueue(
        workflow_id="implement-review",
        task_text="Append one governed line and review the exact resulting source.",
        working_directory=".",
        resource_key=repository_resource_key(root),
        permission_decision_id="implement-permission",
        operation_id="implement-review-one",
        timeout_seconds=30,
    )
    manager = ManagedAgentManager(max_sessions=3)
    runner = ManagedWorkflowRunner(
        worker,
        manager,
        launch_builder=_launch_builder,
        available_agent_ids=("codex", "claude-code"),
        lease_seconds=5,
        heartbeat_seconds=1,
        poll_seconds=0.05,
    )
    try:
        runner.start()
        operation = _wait_status(queue, "implement-review-one", "succeeded")
    finally:
        runner.close()

    sealed = governance.execution_binding_for_permission("implement-permission")
    assert operation["terminal_outcome"] == "managed_sessions_completed"
    assert sealed["state"] == "sealed"
    assert governance.verify_execution_source("implement-binding")["stale"] is False
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "baseline\n"
    assert "managed writer" in (worktree / "tracked.txt").read_text(encoding="utf-8")
    snapshots = manager.snapshots()
    assert [item["execution_mode"] for item in snapshots] == [
        "isolated-write",
        "observe",
    ]
    assert "in isolated-write mode" in snapshots[0]["events"][0]["text"]
    assert "in observe mode" in snapshots[1]["events"][0]["text"]
    assert all(
        item["governance_binding_id"] == "implement-binding" for item in snapshots
    )
