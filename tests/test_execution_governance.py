from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from peerbridge_mcp.bridge import CONTROL_ROOM_WORKFLOW_ID, Bridge
from peerbridge_mcp.execution_governance import (
    ExecutionGovernance,
    GovernanceError,
    repository_source_state,
    repository_resource_key,
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "tests@example.invalid", cwd=root)
    _git("config", "user.name", "PeerBridge Tests", cwd=root)
    (root / ".gitignore").write_text(".peerbridge-artifacts/\n.peerbridge/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-qm", "baseline", cwd=root)
    return root


def _deep_repository(tmp_path: Path, *, minimum_root_length: int = 175) -> Path:
    parent = tmp_path
    while len(str(parent / "repository")) < minimum_root_length:
        remaining = minimum_root_length - len(str(parent / "repository"))
        parent /= "d" * max(1, min(40, remaining - 1))
    parent.mkdir(parents=True, exist_ok=True)
    return _repository(parent)


def _governance(root: Path, *, agent_id: str = "human-operator") -> ExecutionGovernance:
    return ExecutionGovernance(
        Bridge(
            root,
            root / ".peerbridge" / "governance.sqlite3",
            agent_id,
            "governance-test",
        )
    )


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
def test_source_state_hashes_tracked_bytes_hidden_by_git_index_flags(
    tmp_path: Path, flag: str
) -> None:
    root = _repository(tmp_path)
    baseline = repository_source_state(root)["diff_sha256"]
    _git("update-index", f"--{flag}", "tracked.txt", cwd=root)
    (root / "tracked.txt").write_text("hidden working-tree change\n", encoding="utf-8")

    changed = repository_source_state(root)["diff_sha256"]

    assert changed != baseline


def test_source_state_binds_index_and_untracked_file_modes(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    untracked = root / "new-source.txt"
    untracked.write_text("new source\n", encoding="utf-8")
    baseline = repository_source_state(root)["diff_sha256"]

    if os.name == "nt":
        pytest.skip("Windows does not expose a stable executable mode bit")
    untracked.chmod(0o755)
    executable = repository_source_state(root)["diff_sha256"]
    assert executable != baseline

    _git("add", "new-source.txt", cwd=root)
    indexed = repository_source_state(root)["diff_sha256"]
    assert indexed != executable


@pytest.fixture
def short_local_app_data():
    with tempfile.TemporaryDirectory(prefix="peerbridge-alpha52-test-") as directory:
        yield Path(directory)


def test_capability_registry_is_versioned_and_grants_are_append_only(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    governance = _governance(root)
    source_sha = hashlib.sha256(b"reviewed capability source").hexdigest()
    registered = governance.register_capability(
        capability_id="github.get-pull-request",
        registry_version="1.0.0",
        kind="mcp-tool",
        display_name="GitHub read-only pull request",
        source_sha256=source_sha,
        sensitivity="read",
    )
    assert registered["capability_sha256"]
    with pytest.raises(GovernanceError, match="already exists"):
        governance.register_capability(
            capability_id="github.get-pull-request",
            registry_version="1.0.0",
            kind="mcp-tool",
            display_name="Replacement must not overwrite",
            source_sha256=source_sha,
            sensitivity="read",
        )

    allow = governance.grant_capability(
        principal_type="agent",
        principal_id="reviewer-one",
        capability_id="github.get-pull-request",
        registry_version="1.0.0",
        decision="allow",
        reason="Reviewer needs read-only pull request evidence.",
    )
    effective = governance.effective_capabilities("agent", "reviewer-one")
    assert effective[0]["grant_id"] == allow["grant_id"]
    assert effective[0]["approval_required"] is False

    governance.grant_capability(
        principal_type="agent",
        principal_id="reviewer-one",
        capability_id="github.get-pull-request",
        registry_version="1.0.0",
        decision="deny",
        reason="The task no longer needs GitHub access.",
    )
    assert governance.effective_capabilities("agent", "reviewer-one") == []
    with governance.bridge._connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM capability_grants"
        ).fetchone()[0]
    assert count == 2


def test_sensitive_capability_is_visible_as_approval_required(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    governance = _governance(root)
    governance.register_capability(
        capability_id="git.apply-patch",
        registry_version="1.0.0",
        kind="skill",
        display_name="Apply a reviewed patch",
        source_sha256=hashlib.sha256(b"apply patch skill").hexdigest(),
        sensitivity="sensitive",
    )
    governance.grant_capability(
        principal_type="room",
        principal_id="release-room",
        capability_id="git.apply-patch",
        registry_version="1.0.0",
        decision="allow",
        reason="Expose the capability but retain the human action gate.",
    )
    assert governance.effective_capabilities("room", "release-room")[0][
        "approval_required"
    ] is True


def test_non_human_agent_cannot_make_governance_decisions(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    governance = _governance(root, agent_id="agent-one")
    with pytest.raises(GovernanceError, match="human-operator"):
        governance.register_capability(
            capability_id="unsafe",
            registry_version="1",
            kind="skill",
            display_name="Not authorized",
            source_sha256=hashlib.sha256(b"source").hexdigest(),
            sensitivity="write",
        )


def test_permission_is_exact_bounded_and_single_use(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    governance = _governance(root)
    resource = repository_resource_key(root)
    decision = governance.decide_permission(
        task_id="task-one",
        agent_id="writer-one",
        action="git.worktree.create",
        resource_key=resource,
        decision="allow",
        reason="Create one isolated writer worktree.",
        expires_epoch=1_100,
        now_epoch=1_000,
        decision_id="permission-one",
    )
    assert decision["decision"] == "allow"
    with pytest.raises(GovernanceError, match="does not match"):
        governance.authorize_permission(
            "permission-one",
            task_id="different-task",
            agent_id="writer-one",
            action="git.worktree.create",
            resource_key=resource,
            now_epoch=1_001,
        )
    authorized = governance.authorize_permission(
        "permission-one",
        task_id="task-one",
        agent_id="writer-one",
        action="git.worktree.create",
        resource_key=resource,
        now_epoch=1_001,
    )
    assert authorized["consumed_utc"]
    with pytest.raises(GovernanceError, match="already consumed"):
        governance.authorize_permission(
            "permission-one",
            task_id="task-one",
            agent_id="writer-one",
            action="git.worktree.create",
            resource_key=resource,
            now_epoch=1_002,
        )


def test_expired_denied_and_tampered_permissions_fail_closed(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    governance = _governance(root)
    resource = repository_resource_key(root)
    governance.decide_permission(
        task_id="task",
        agent_id="writer",
        action="git.worktree.create",
        resource_key=resource,
        decision="deny",
        reason="Writer access is not approved.",
        expires_epoch=1_100,
        now_epoch=1_000,
        decision_id="denied",
    )
    with pytest.raises(GovernanceError, match="denied"):
        governance.authorize_permission(
            "denied",
            task_id="task",
            agent_id="writer",
            action="git.worktree.create",
            resource_key=resource,
            now_epoch=1_001,
        )
    governance.decide_permission(
        task_id="task",
        agent_id="writer",
        action="git.worktree.create",
        resource_key=resource,
        decision="allow",
        reason="Short-lived approval.",
        expires_epoch=1_010,
        now_epoch=1_000,
        decision_id="expired",
    )
    with pytest.raises(GovernanceError, match="expired"):
        governance.authorize_permission(
            "expired",
            task_id="task",
            agent_id="writer",
            action="git.worktree.create",
            resource_key=resource,
            now_epoch=1_010,
        )
    governance.decide_permission(
        task_id="task",
        agent_id="writer",
        action="git.worktree.create",
        resource_key=resource,
        decision="allow",
        reason="Approval used for tamper test.",
        expires_epoch=1_100,
        now_epoch=1_000,
        decision_id="tampered",
    )
    with sqlite3.connect(governance.bridge.db_path) as connection:
        connection.execute(
            "UPDATE permission_decisions SET action='git.merge' WHERE decision_id='tampered'"
        )
    with pytest.raises(GovernanceError, match="SHA-256"):
        governance.authorize_permission(
            "tampered",
            task_id="task",
            agent_id="writer",
            action="git.merge",
            resource_key=resource,
            now_epoch=1_001,
        )


def test_isolated_worktree_never_changes_main_and_source_binding_goes_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    short_local_app_data: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(short_local_app_data))
    root = _repository(tmp_path)
    governance = _governance(root)
    resource = repository_resource_key(root)
    now = time.time()
    permission = governance.decide_permission(
        task_id="implement-one",
        agent_id="writer-one",
        action="git.worktree.create",
        resource_key=resource,
        decision="allow",
        reason="Writer must be isolated from the operator checkout.",
        expires_epoch=now + 600,
        now_epoch=now,
        decision_id="worktree-permission",
    )
    binding = governance.create_isolated_worktree(
        task_id="implement-one",
        agent_id="writer-one",
        permission_decision_id=permission["decision_id"],
        repository=root,
        binding_id="binding-one",
    )
    worktree = Path(binding["worktree_path"])
    assert worktree.is_dir()
    assert worktree != root
    assert binding["base_commit_id"] == _git("rev-parse", "HEAD", cwd=root)
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "baseline\n"
    launch = governance.resolve_launch_binding("binding-one", "writer-one")
    assert launch["worktree_path"] == worktree
    assert launch["repository_root"] == root.resolve()
    assert launch["binding_sha256"] == binding["binding_sha256"]
    with pytest.raises(GovernanceError, match="another Agent"):
        governance.resolve_launch_binding("binding-one", "different-writer")

    (worktree / "tracked.txt").write_text("writer change\n", encoding="utf-8")
    (worktree / "new-source.txt").write_text("new source\n", encoding="utf-8")
    before_seal = governance.verify_execution_source("binding-one")
    assert before_seal["stale"] is True
    with pytest.raises(GovernanceError, match="binding agent"):
        _governance(root, agent_id="unrelated-agent").seal_execution("binding-one")
    sealed = _governance(
        root, agent_id=CONTROL_ROOM_WORKFLOW_ID
    ).seal_execution("binding-one")
    assert sealed["state"] == "sealed"
    with pytest.raises(GovernanceError, match="not active"):
        governance.resolve_launch_binding("binding-one", "writer-one")
    verified = governance.verify_execution_source("binding-one")
    assert verified["stale"] is False
    assert verified["expected_diff_sha256"] == sealed["final_diff_sha256"]
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "baseline\n"
    assert _git("status", "--short", cwd=root) == ""

    (worktree / "tracked.txt").write_text("changed after review binding\n", encoding="utf-8")
    stale = governance.verify_execution_source("binding-one")
    assert stale["stale"] is True
    assert stale["live_diff_sha256"] != stale["expected_diff_sha256"]

    (worktree / "tracked.txt").write_text("writer change\n", encoding="utf-8")
    (worktree / "new-source.txt").write_text(
        "changed untracked source\n", encoding="utf-8"
    )
    untracked_stale = governance.verify_execution_source("binding-one")
    assert untracked_stale["stale"] is True
    assert untracked_stale["live_diff_sha256"] != sealed["final_diff_sha256"]

    with pytest.raises(GovernanceError, match="already consumed"):
        governance.create_isolated_worktree(
            task_id="implement-one",
            agent_id="writer-one",
            permission_decision_id=permission["decision_id"],
            repository=root,
            binding_id="binding-two",
        )


def test_unrelated_principal_cannot_create_worktree_for_assigned_agent(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    governance = _governance(root)
    now = time.time()
    permission = governance.decide_permission(
        task_id="principal-bound-task",
        agent_id="assigned-writer",
        action="git.worktree.create",
        resource_key=repository_resource_key(root),
        decision="allow",
        reason="Only the assigned writer may use this isolated execution grant.",
        expires_epoch=now + 600,
        now_epoch=now,
        decision_id="principal-bound-permission",
    )

    with pytest.raises(GovernanceError, match="assigned agent"):
        _governance(root, agent_id="unrelated-agent").create_isolated_worktree(
            task_id="principal-bound-task",
            agent_id="assigned-writer",
            permission_decision_id=str(permission["decision_id"]),
            repository=root,
            binding_id="principal-bound-binding",
        )

    assert governance.inspect_permission(str(permission["decision_id"]))["consumed"] is False
    assert _git("worktree", "list", "--porcelain", cwd=root).count("worktree ") == 1


def test_invalid_base_commit_does_not_consume_worktree_permission(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    governance = _governance(root)
    now = time.time()
    permission = governance.decide_permission(
        task_id="invalid-base-task",
        agent_id="invalid-base-writer",
        action="git.worktree.create",
        resource_key=repository_resource_key(root),
        decision="allow",
        reason="The permission must survive validation failures.",
        expires_epoch=now + 600,
        now_epoch=now,
        decision_id="invalid-base-permission",
    )

    with pytest.raises(GovernanceError, match="Git operation failed"):
        governance.create_isolated_worktree(
            task_id="invalid-base-task",
            agent_id="invalid-base-writer",
            permission_decision_id=str(permission["decision_id"]),
            repository=root,
            base_commit="missing-commit",
            binding_id="invalid-base-binding",
        )

    assert governance.inspect_permission(str(permission["decision_id"]))["consumed"] is False
    assert _git("worktree", "list", "--porcelain", cwd=root).count("worktree ") == 1


def test_post_creation_failure_rolls_back_permission_binding_and_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    governance = _governance(root)
    now = time.time()
    permission = governance.decide_permission(
        task_id="rollback-task",
        agent_id="rollback-writer",
        action="git.worktree.create",
        resource_key=repository_resource_key(root),
        decision="allow",
        reason="A failed binding transaction must leave no consumed grant or worktree.",
        expires_epoch=now + 600,
        now_epoch=now,
        decision_id="rollback-permission",
    )
    original_event = governance.bridge._event

    def fail_worktree_event(
        connection: sqlite3.Connection, event_type: str, *args: object, **kwargs: object
    ) -> dict[str, object]:
        if event_type == "governance.execution.worktree_created":
            raise RuntimeError("simulated post-creation database failure")
        return original_event(connection, event_type, *args, **kwargs)

    monkeypatch.setattr(governance.bridge, "_event", fail_worktree_event)
    with pytest.raises(RuntimeError, match="simulated post-creation"):
        governance.create_isolated_worktree(
            task_id="rollback-task",
            agent_id="rollback-writer",
            permission_decision_id=str(permission["decision_id"]),
            repository=root,
            binding_id="rollback-binding",
        )

    assert governance.inspect_permission(str(permission["decision_id"]))["consumed"] is False
    with governance.bridge._connect() as connection:
        binding_count = connection.execute(
            "SELECT COUNT(*) FROM execution_bindings WHERE binding_id=?",
            ("rollback-binding",),
        ).fetchone()[0]
    assert binding_count == 0
    assert _git("worktree", "list", "--porcelain", cwd=root).count("worktree ") == 1


def test_isolated_worktree_uses_bounded_path_for_deep_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    short_local_app_data: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(short_local_app_data))
    root = _deep_repository(tmp_path)
    governance = _governance(root)
    resource = repository_resource_key(root)
    now = time.time()
    permission = governance.decide_permission(
        task_id="deep-path-task",
        agent_id="deep-path-writer",
        action="git.worktree.create",
        resource_key=resource,
        decision="allow",
        reason="Verify isolated execution at the Windows Git path boundary.",
        expires_epoch=now + 600,
        now_epoch=now,
        decision_id="deep-path-permission",
    )

    binding = governance.create_isolated_worktree(
        task_id="deep-path-task",
        agent_id="deep-path-writer",
        permission_decision_id=permission["decision_id"],
        repository=root,
        binding_id="deep-path-binding",
    )

    worktree = Path(binding["worktree_path"])
    verbose_target = root / ".peerbridge-artifacts" / "worktrees" / "deep-path-binding"
    assert len(str(root)) >= 175
    if os.name == "nt":
        relative_external = worktree.relative_to(short_local_app_data.resolve())
        assert relative_external.parts[:2] == ("PeerBridge", "worktrees")
    else:
        assert worktree.parent == (root / ".peerbridge" / "wt").resolve()
    assert len(worktree.name) == 16
    assert len(str(worktree)) < len(str(verbose_target))
    assert governance.verify_execution_source("deep-path-binding")["stale"] is False
    assert _git("status", "--short", cwd=root) == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_worktree_fallback_rejects_local_app_data_junction_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    short_local_app_data: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(short_local_app_data))
    root = _deep_repository(tmp_path)
    governance = _governance(root)
    outside = tmp_path / "outside-worktrees"
    outside.mkdir()
    junction = short_local_app_data / "PeerBridge"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("Windows did not permit a temporary directory junction")
    try:
        now = time.time()
        permission = governance.decide_permission(
            task_id="junction-task",
            agent_id="junction-writer",
            action="git.worktree.create",
            resource_key=repository_resource_key(root),
            decision="allow",
            reason="A redirected worktree root must fail closed.",
            expires_epoch=now + 600,
            now_epoch=now,
            decision_id="junction-permission",
        )
        with pytest.raises(GovernanceError, match="escapes LOCALAPPDATA"):
            governance.create_isolated_worktree(
                task_id="junction-task",
                agent_id="junction-writer",
                permission_decision_id=str(permission["decision_id"]),
                repository=root,
                binding_id="junction-binding",
            )
    finally:
        junction.rmdir()
