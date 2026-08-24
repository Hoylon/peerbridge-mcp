from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.execution_governance import repository_resource_key
from peerbridge_mcp.monitor import BridgeReader
from peerbridge_mcp.operation_queue import DurableOperationQueue
from peerbridge_mcp.server import HANDLERS, READ_ONLY_TOOLS, TOOL_SCHEMAS, handle_request


def _payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list) and content
    value = json.loads(content[0]["text"])
    assert isinstance(value, dict)
    return value


def _call(
    bridge: Bridge, name: str, arguments: dict[str, object] | None = None
) -> dict[str, object]:
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": f"call:{name}",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert response is not None and "error" not in response
    return _payload(response)


def _bridge(root: Path, *, agent_id: str = "human-operator") -> Bridge:
    return Bridge(
        root,
        root / ".peerbridge" / "alpha52-server.sqlite3",
        agent_id,
        "alpha52-server-test",
        session_id=f"{agent_id}-session",
    )


def test_alpha52_tools_are_unique_registered_and_truthfully_annotated() -> None:
    names = [str(tool["name"]) for tool in TOOL_SCHEMAS]
    assert len(names) == len(set(names))
    assert set(names) == set(HANDLERS)
    for tool in TOOL_SCHEMAS:
        annotations = tool["annotations"]
        assert annotations["readOnlyHint"] is (tool["name"] in READ_ONLY_TOOLS)
        assert annotations["destructiveHint"] is False
        assert annotations["openWorldHint"] is False

    record_trust = next(
        tool for tool in TOOL_SCHEMAS if tool["name"] == "record_trust"
    )
    assert record_trust["inputSchema"]["properties"]["stage"]["enum"] == [
        "claim",
        "execution",
        "test",
        "proof",
        "review",
        "decision",
    ]
    gate_status = next(
        tool for tool in TOOL_SCHEMAS if tool["name"] == "release_gate_status"
    )
    assert "request_release_gate" not in names
    assert "decide_release_gate" not in names
    assert "request_release_gate" not in HANDLERS
    assert "decide_release_gate" not in HANDLERS
    assert gate_status["annotations"]["readOnlyHint"] is True


def test_release_gate_mcp_path_is_status_only_and_never_publishes(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "PeerBridge Tests"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / ".gitignore").write_text(
        ".peerbridge/\n.peerbridge-artifacts/\n", encoding="utf-8"
    )
    (tmp_path / "source.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    bridge = _bridge(tmp_path)

    status = _call(bridge, "release_gate_status")
    assert status["ready"] is False
    assert "gate_not_requested" in status["blockers"]
    assert status["publishing_performed"] is False
    assert subprocess.run(
        ["git", "tag", "--list"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == ""


def test_operator_queue_schedule_memory_trust_and_proof_bundle_through_mcp(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence" / "result.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("verified source state\n", encoding="utf-8")
    bridge = _bridge(tmp_path)

    templates = _call(bridge, "list_workflow_templates")
    assert templates["count"] == 4
    queued = _call(
        bridge,
        "enqueue_workflow",
        {
            "operation_id": "mcp-operation-one",
            "workflow_id": "read-only-audit",
            "task_text": "Audit the exact local source state.",
            "working_directory": ".",
            "resource_key": "repo:alpha52",
            "max_attempts": 2,
            "timeout_seconds": 60,
        },
    )
    assert queued["status"] == "queued"
    assert _call(bridge, "list_operations")["count"] == 1

    schedule = _call(
        bridge,
        "save_workflow_schedule",
        {
            "schedule_id": "mcp-schedule-one",
            "workflow_id": "release-gate",
            "task_text": "Run the bounded release gate.",
            "working_directory": ".",
            "resource_key": "repo:release",
            "interval_seconds": 60,
            "next_run_epoch": time.time() + 600,
            "enabled": True,
        },
    )
    assert schedule["enabled"] is True
    assert _call(bridge, "list_workflow_schedules", {"enabled": True})["count"] == 1
    disabled = _call(
        bridge,
        "set_workflow_schedule_enabled",
        {"schedule_id": "mcp-schedule-one", "enabled": False},
    )
    assert disabled["enabled"] is False

    memory = _call(
        bridge,
        "record_memory",
        {
            "visibility": "project",
            "record_type": "DECISION",
            "authority_id": "human-operator",
            "title": "Exact-source review",
            "body": "Reviews must bind the exact source used by tests.",
            "artifact_paths": ["evidence/result.txt"],
            "applicability": ["alpha52"],
        },
    )
    briefing = _call(
        bridge,
        "brief_task",
        {"task_id": "mcp-proof-task", "applicability": ["alpha52"]},
    )
    assert briefing["memory_bindings"][0]["memory_id"] == memory["memory_id"]
    conflict = _call(
        bridge,
        "record_decision_conflict",
        {
            "task_id": "mcp-proof-task",
            "briefing_id": briefing["briefing_id"],
            "memory_ids": [memory["memory_id"]],
            "summary": "A proposed review referenced a different source state.",
            "severity": "high",
        },
    )
    assert conflict["enforcement"] == "review-finding-only"

    source_sha = hashlib.sha256(b"capability-source").hexdigest()
    _call(
        bridge,
        "register_capability",
        {
            "capability_id": "mcp.read-source",
            "registry_version": "v1",
            "kind": "mcp-tool",
            "display_name": "Read source",
            "source_sha256": source_sha,
            "sensitivity": "read",
        },
    )
    _call(
        bridge,
        "grant_capability",
        {
            "principal_type": "agent",
            "principal_id": "reviewer",
            "capability_id": "mcp.read-source",
            "registry_version": "v1",
            "decision": "allow",
            "reason": "Reviewer needs bounded read access.",
        },
    )
    capabilities = _call(
        bridge,
        "effective_capabilities",
        {"principal_type": "agent", "principal_id": "reviewer"},
    )
    assert capabilities["count"] == 1

    record_ids = []
    related: list[str] = []
    for stage in ("test", "proof", "review", "decision"):
        record_id = f"mcp-proof-task:{stage}"
        result = _call(
            bridge,
            "record_trust",
            {
                "record_id": record_id,
                "task_id": "mcp-proof-task",
                "stage": stage,
                "statement": f"Bounded {stage} evidence was recorded.",
                "artifact_paths": ["evidence/result.txt"],
                "related_record_ids": related,
            },
        )
        assert result["record_id"] == record_id
        record_ids.append(record_id)
        related = [record_id]
    completion = _call(
        bridge,
        "complete_trust_timeline",
        {
            "task_id": "mcp-proof-task",
            "statement": "The operator accepted the exact reviewed source.",
            "evidence_record_ids": record_ids,
        },
    )
    assert completion["stage"] == "completion"
    assert _call(bridge, "trust_timeline", {"task_id": "mcp-proof-task"})[
        "count"
    ] == 5

    bundle_path = ".peerbridge-artifacts/proof-bundles/mcp-proof-task"
    exported = _call(
        bridge,
        "export_proof_bundle",
        {"task_id": "mcp-proof-task", "output_path": bundle_path},
    )
    assert exported["status"] == "CAPTURED"
    verified = _call(
        bridge, "verify_proof_bundle", {"bundle_path": bundle_path}
    )
    assert verified["valid"] is True

    snapshot = BridgeReader(bridge.db_path, tmp_path).snapshot(scope=bridge.scope)
    assert snapshot.operations[0]["operation_id"] == "mcp-operation-one"
    assert snapshot.schedules[0]["schedule_id"] == "mcp-schedule-one"
    assert snapshot.capabilities[0]["capability_id"] == "mcp.read-source"
    assert snapshot.capability_grants[0]["principal_id"] == "reviewer"
    assert snapshot.task_briefings[0]["briefing_id"] == briefing["briefing_id"]
    assert snapshot.decision_conflicts[0]["finding_id"] == conflict["finding_id"]
    assert snapshot.trust_records[0]["stage"] == "completion"
    assert snapshot.trust_records[0]["integrity_valid"] is True
    assert snapshot.trust_records[0]["freshness"] == "fresh"

    evidence.write_text("changed after completion\n", encoding="utf-8")
    stale = BridgeReader(bridge.db_path, tmp_path).snapshot(scope=bridge.scope)
    assert stale.trust_records[0]["freshness"] == "stale"

    worker = _bridge(tmp_path, agent_id="worker")
    rejected = _call(
        worker,
        "enqueue_workflow",
        {
            "operation_id": "worker-cannot-enqueue",
            "workflow_id": "read-only-audit",
            "task_text": "This must be rejected.",
            "working_directory": ".",
            "resource_key": "repo:worker",
        },
    )
    assert "only human-operator" in rejected["error"]
    materialize_rejected = _call(worker, "materialize_workflow_schedules")
    assert "control-room-workflow" in materialize_rejected["error"]
    reconcile_rejected = _call(worker, "reconcile_operations")
    assert "control-room-workflow" in reconcile_rejected["error"]


def test_workflow_queue_rejects_protected_working_directory_before_persistence(
    tmp_path: Path,
) -> None:
    bridge = _bridge(tmp_path)
    rejected = _call(
        bridge,
        "enqueue_workflow",
        {
            "operation_id": "protected-operation",
            "workflow_id": "read-only-audit",
            "task_text": "This protected working directory must not be queued.",
            "working_directory": ".peerbridge",
            "resource_key": "repo:protected",
        },
    )

    assert "working directory is protected" in rejected["error"]
    assert _call(bridge, "list_operations")["count"] == 0


def test_governed_worktree_tools_use_exact_project_relative_repository(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "PeerBridge Tests"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / ".gitignore").write_text(
        ".peerbridge/\n.peerbridge-artifacts/\n", encoding="utf-8"
    )
    (tmp_path / "source.txt").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    bridge = _bridge(tmp_path)
    resource_key = repository_resource_key(tmp_path)
    decision = _call(
        bridge,
        "decide_permission",
        {
            "decision_id": "worktree-decision-one",
            "task_id": "worktree-task",
            "agent_id": "implementer",
            "action": "git.worktree.create",
            "resource_key": resource_key,
            "decision": "allow",
            "reason": "Create one isolated writer worktree.",
            "expires_epoch": time.time() + 300,
        },
    )
    assert decision["consumed_utc"] is None
    binding = _call(
        bridge,
        "create_execution_worktree",
        {
            "binding_id": "worktree-binding-one",
            "task_id": "worktree-task",
            "agent_id": "implementer",
            "permission_decision_id": "worktree-decision-one",
            "repository": ".",
            "base_commit": "HEAD",
        },
    )
    assert binding["state"] == "active"
    verified = _call(
        bridge, "verify_execution_source", {"binding_id": "worktree-binding-one"}
    )
    assert verified["stale"] is False
    unauthorized_seal = _call(
        _bridge(tmp_path, agent_id="unrelated-agent"),
        "seal_execution",
        {"binding_id": "worktree-binding-one"},
    )
    assert "binding agent" in unauthorized_seal["error"]
    sealed = _call(
        _bridge(tmp_path, agent_id="control-room-workflow"),
        "seal_execution",
        {"binding_id": "worktree-binding-one"},
    )
    assert sealed["state"] == "sealed"
