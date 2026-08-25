from __future__ import annotations

import threading
from pathlib import Path

from peerbridge_mcp.official_agent_runtime import CodexAppServerSession


def _session(tmp_path: Path, approval_mode: str) -> CodexAppServerSession:
    return CodexAppServerSession(
        session_id=f"codex-approval-{approval_mode}",
        role="implementer",
        working_directory=tmp_path,
        requested_route=None,
        permission_tier="edit",
        approval_mode=approval_mode,  # type: ignore[arg-type]
        governance_binding_id="binding-one",
        project_root=tmp_path,
    )


def test_codex_request_pauses_until_allow_once(tmp_path: Path) -> None:
    session = _session(tmp_path, "approval-required")
    sent: list[dict[str, object]] = []
    session._send = sent.append  # type: ignore[method-assign]
    worker = threading.Thread(
        target=session._handle_server_request,
        args=(7, "item/commandExecution/requestApproval"),
        kwargs={
            "params": {
                "command": ["python", "-m", "pytest"],
                "cwd": str(tmp_path),
                "reason": "Run focused tests",
                "availableDecisions": ["accept", "acceptForSession", "decline"],
            },
            "generation": 0,
        },
    )
    worker.start()
    for _ in range(100):
        pending = session.snapshot()["approval_broker"]["pending"]
        if pending:
            break
        worker.join(timeout=0.01)
    else:
        raise AssertionError("Codex approval request was not published")

    session.resolve_approval(str(pending[0]["approval_id"]), "allow-once")
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert sent == [{"jsonrpc": "2.0", "id": 7, "result": {"decision": "accept"}}]
    assert session.snapshot()["approval_broker"]["pending_count"] == 0


def test_codex_session_grant_round_trips_accept_for_session(tmp_path: Path) -> None:
    session = _session(tmp_path, "approval-required")
    sent: list[dict[str, object]] = []
    session._send = sent.append  # type: ignore[method-assign]
    worker = threading.Thread(
        target=session._handle_server_request,
        args=(9, "item/fileChange/requestApproval"),
        kwargs={
            "params": {
                "reason": "Update source file",
                "changes": [{"path": "src/example.py"}],
            },
            "generation": 0,
        },
    )
    worker.start()
    for _ in range(100):
        pending = session.snapshot()["approval_broker"]["pending"]
        if pending:
            break
        worker.join(timeout=0.01)
    else:
        raise AssertionError("Codex approval request was not published")
    session.resolve_approval(str(pending[0]["approval_id"]), "allow-session")
    worker.join(timeout=5)

    assert sent[0]["result"] == {"decision": "acceptForSession"}


def test_read_only_codex_declines_without_fake_pending_card(tmp_path: Path) -> None:
    session = CodexAppServerSession(
        session_id="codex-read-only-approval",
        role="reviewer",
        working_directory=tmp_path,
        requested_route=None,
        permission_tier="review",
        governance_binding_id=None,
        project_root=tmp_path,
    )
    sent: list[dict[str, object]] = []
    session._send = sent.append  # type: ignore[method-assign]

    session._handle_server_request(
        11,
        "item/fileChange/requestApproval",
        {"reason": "Unexpected write"},
        0,
    )

    assert sent[0]["result"] == {"decision": "decline"}
    assert session.snapshot()["approval_broker"]["pending_count"] == 0


def test_codex_delegated_mode_uses_official_auto_reviewer(tmp_path: Path) -> None:
    delegated = _session(tmp_path, "agent-delegated")
    manual = _session(tmp_path, "approval-required")

    assert delegated._approvals_reviewer() == "auto_review"
    assert manual._approvals_reviewer() == "user"
