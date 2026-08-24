from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import peerbridge_mcp.lifecycle_receipt as lifecycle_receipt
from peerbridge_mcp import __version__
from peerbridge_mcp.agent_identity import ensure_agent_identity_capability
from peerbridge_mcp.bridge import Bridge, stable_sha256
from peerbridge_mcp.lifecycle_receipt import (
    EVIDENCE_SCHEMA,
    RECEIPT_SCHEMA,
    ReceiptError,
    capture_receipt,
    verify_receipt,
)
from peerbridge_mcp.protocol import PROTOCOL_VERSION


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
REQUEST_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _child_environment() -> dict[str, str]:
    environment = {
        "PYTHONPATH": str(SOURCE_ROOT),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


class _StdioChild:
    def __init__(
        self,
        *,
        project_root: Path,
        db_path: Path,
        scope: str,
        agent_id: str,
        identity_capability_path: Path,
        session_id: str,
        allowed_tools: tuple[str, ...],
    ) -> None:
        command = [
            sys.executable,
            "-m",
            "peerbridge_mcp",
            "serve",
            "--project-root",
            str(project_root),
            "--db",
            str(db_path),
            "--scope",
            scope,
            "--agent-id",
            agent_id,
            "--identity-capability",
            str(identity_capability_path),
            "--session-id",
            session_id,
        ]
        for tool in allowed_tools:
            command.extend(("--allow-tool", tool))
        environment = _child_environment()
        self.command = command
        self.cwd = REPOSITORY_ROOT
        self.environment_keys = sorted(environment)
        self.allowed_tools = list(allowed_tools)
        self.exchanges: list[dict[str, Any]] = []
        self.process = subprocess.Popen(
            command,
            cwd=self.cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._handles_closed = False

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            if line.strip():
                self._lines.put(line)
        self._lines.put(None)

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None
        assert self.process.poll() is None, "MCP child exited before the request"
        self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        try:
            line = self._lines.get(timeout=REQUEST_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            raise AssertionError("timed out waiting for MCP stdio response") from exc
        if line is None:
            raise AssertionError(
                f"MCP child closed stdout with return code {self.process.poll()}"
            )
        response = json.loads(line)
        assert isinstance(response, dict)
        assert response.get("id") == request.get("id")
        self.exchanges.append({"request": request, "response": response})
        return response

    def _close_handles(self) -> str:
        if self._handles_closed:
            return ""
        self._handles_closed = True
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        self._reader.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        assert not self._reader.is_alive(), "stdout reader survived MCP child shutdown"
        stderr = self.process.stderr.read() if self.process.stderr is not None else ""
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        return stderr

    def terminate_as_crash(self) -> dict[str, Any]:
        started = time.monotonic()
        self.process.terminate()
        fallback_kill_used = False
        try:
            exit_code = self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            fallback_kill_used = True
            self.process.kill()
            exit_code = self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        elapsed = time.monotonic() - started
        stderr = self._close_handles()
        assert not stderr.strip(), stderr
        assert not fallback_kill_used, "terminate did not stop the crash-test child"
        assert exit_code != 0
        return {
            "method": "terminate",
            "timeout_seconds": SHUTDOWN_TIMEOUT_SECONDS,
            "elapsed_seconds": elapsed,
            "exit_code": exit_code,
            "alive_after_wait": self.process.poll() is None,
            "fallback_kill_used": fallback_kill_used,
        }

    def close_stdin(self) -> dict[str, Any]:
        assert self.process.stdin is not None
        started = time.monotonic()
        self.process.stdin.close()
        fallback_kill_used = False
        try:
            exit_code = self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            fallback_kill_used = True
            self.process.kill()
            exit_code = self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        elapsed = time.monotonic() - started
        stderr = self._close_handles()
        assert not stderr.strip(), stderr
        assert not fallback_kill_used, "EOF did not stop the recovered MCP child"
        assert exit_code == 0
        return {
            "method": "stdin_eof",
            "timeout_seconds": SHUTDOWN_TIMEOUT_SECONDS,
            "elapsed_seconds": elapsed,
            "exit_code": exit_code,
            "alive_after_wait": self.process.poll() is None,
            "fallback_kill_used": fallback_kill_used,
        }

    def cleanup(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        self._close_handles()


def _request(request_id: str, method: str, **params: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params:
        request["params"] = params
    return request


def _call(request_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _request(request_id, "tools/call", name=tool, arguments=arguments)


def _initialize(request_id: str) -> dict[str, Any]:
    return _request(
        request_id,
        "initialize",
        protocolVersion=PROTOCOL_VERSION,
        capabilities={},
        clientInfo={"name": "lifecycle-receipt-test", "version": "1"},
    )


def _tool_payload(response: dict[str, Any]) -> dict[str, Any]:
    text = response["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


def _presence_rows(
    db_path: Path, *, scope: str, agent_id: str, session_id: str
) -> list[dict[str, Any]]:
    # A terminated WAL writer can require SQLite recovery before read-only access.
    # This test-owned observer allows that recovery but rejects all SQL writes.
    deadline = time.monotonic() + 3.0
    while True:
        try:
            with contextlib.closing(
                sqlite3.connect(db_path, timeout=0.25)
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                rows = connection.execute(
                    """SELECT scope, agent_id, session_id, transport, client_name,
                              provider_id, model_id, reasoning_mode, route_class,
                              last_seen_utc, last_seen_epoch
                         FROM agent_presence
                        WHERE scope=? AND agent_id=? AND session_id=?""",
                    (scope, agent_id, session_id),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    state: dict[str, tuple[int, int, str]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        stat = path.stat()
        state[path.relative_to(root).as_posix()] = (
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return state


def _run_lifecycle(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], list[_StdioChild]]:
    project_root = tmp_path / "isolated-project"
    project_root.mkdir()
    db_path = project_root / "lifecycle.sqlite3"
    evidence_path = project_root / "lifecycle-evidence.json"
    receipt_path = project_root / "lifecycle-receipt.json"
    scope = "lifecycle-test"
    agent_id = "recovering-agent"
    session_id = "bounded-session"
    children: list[_StdioChild] = []
    Bridge(project_root, db_path, "test-identity-authority", scope)
    identity_capability = ensure_agent_identity_capability(
        project_root,
        db_path,
        scope,
        agent_id,
        allowed_tools=("bridge_status", "poll_messages", "send_message"),
        issued_by="test-lifecycle-receipt",
    )

    try:
        before = _StdioChild(
            project_root=project_root,
            db_path=db_path,
            scope=scope,
            agent_id=agent_id,
            identity_capability_path=identity_capability.path,
            session_id=session_id,
            allowed_tools=("bridge_status", "send_message"),
        )
        children.append(before)
        initialized = before.request(_initialize("before-initialize"))
        assert initialized["result"]["serverInfo"]["name"] == "peerbridge-mcp"
        listed = before.request(_request("before-list", "tools/list"))
        assert [tool["name"] for tool in listed["result"]["tools"]] == [
            "bridge_status",
            "send_message",
        ]
        denied = before.request(_call("before-denied", "poll_messages", {}))
        assert denied["error"] == {
            "code": -32602,
            "message": "Tool is not allowed: poll_messages",
        }
        status_before = _tool_payload(
            before.request(_call("before-status", "bridge_status", {}))
        )
        assert status_before["network_listener"] is False
        sent = _tool_payload(
            before.request(
                _call(
                    "before-send",
                    "send_message",
                    {
                        "recipient": agent_id,
                        "task_id": "lifecycle-task",
                        "subject": "Durable restart proof",
                            "body": "This message survives a terminated stdio child.",
                            "priority": "normal",
                            "idempotency_key": "lifecycle-before-send-1",
                    },
                )
            )
        )
        live_before = _presence_rows(
            db_path, scope=scope, agent_id=agent_id, session_id=session_id
        )
        assert len(live_before) == 1
        crash_shutdown = before.terminate_as_crash()
        residue_after_crash = _presence_rows(
            db_path, scope=scope, agent_id=agent_id, session_id=session_id
        )
        assert residue_after_crash == live_before

        recovery = _StdioChild(
            project_root=project_root,
            db_path=db_path,
            scope=scope,
            agent_id=agent_id,
            identity_capability_path=identity_capability.path,
            session_id=session_id,
            allowed_tools=("bridge_status", "poll_messages"),
        )
        children.append(recovery)
        recovery.request(_initialize("recovery-initialize"))
        recovery.request(_request("recovery-list", "tools/list"))
        status_after = _tool_payload(
            recovery.request(_call("recovery-status", "bridge_status", {}))
        )
        assert status_after["session_id"] == session_id
        polled = _tool_payload(
            recovery.request(
                _call(
                    "recovery-poll",
                    "poll_messages",
                    {"agent_id": agent_id, "after_cursor": 0},
                )
            )
        )
        assert polled["count"] == 1
        assert polled["messages"][0]["message_id"] == sent["message_id"]
        assert polled["messages"][0]["content_sha256"] == sent["content_sha256"]
        live_after_restart = _presence_rows(
            db_path, scope=scope, agent_id=agent_id, session_id=session_id
        )
        assert len(live_after_restart) == 1
        recovery_shutdown = recovery.close_stdin()
        clean_after_recovery = _presence_rows(
            db_path, scope=scope, agent_id=agent_id, session_id=session_id
        )
        assert clean_after_recovery == []
    finally:
        for child in children:
            child.cleanup()

    assert children and all(child.process.poll() is not None for child in children)
    wal_path = db_path.with_name(db_path.name + "-wal")
    assert not wal_path.exists() or wal_path.stat().st_size == 0
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "project_root": str(project_root.resolve()),
        "database_path": str(db_path.resolve()),
        "scope": scope,
        "agent_id": agent_id,
        "session_id": session_id,
        "subprocess_owner": "test",
        "external_provider_calls": 0,
        "credential_inputs": 0,
        "before_crash": {
            "pid": before.process.pid,
            "spawned_by": "test",
            "transport": "stdio",
            "command": before.command,
            "cwd": str(before.cwd.resolve()),
            "environment_keys": before.environment_keys,
            "allowed_tools": before.allowed_tools,
            "exchanges": before.exchanges,
            "presence_while_live": live_before,
            "shutdown": crash_shutdown,
            "presence_after_shutdown": residue_after_crash,
        },
        "after_restart": {
            "pid": recovery.process.pid,
            "spawned_by": "test",
            "transport": "stdio",
            "command": recovery.command,
            "cwd": str(recovery.cwd.resolve()),
            "environment_keys": recovery.environment_keys,
            "allowed_tools": recovery.allowed_tools,
            "exchanges": recovery.exchanges,
            "presence_while_live": live_after_restart,
            "shutdown": recovery_shutdown,
            "presence_after_shutdown": clean_after_recovery,
        },
    }
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt = capture_receipt(
        db_path=db_path,
        evidence_path=evidence_path,
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        output_path=receipt_path,
    )
    return receipt_path, evidence_path, receipt, children


def test_bounded_crash_recovery_authorization_lifecycle_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_path, evidence_path, receipt, children = _run_lifecycle(tmp_path)
    project_root = receipt_path.parent
    db_path = Path(receipt["database"]["path"])

    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["authorization"] == {
        "before_crash_allowed_tools": ["bridge_status", "send_message"],
        "after_restart_allowed_tools": ["bridge_status", "poll_messages"],
        "denied_tool": "poll_messages",
        "denied_arguments_sha256": stable_sha256({}),
        "denial_code": -32602,
        "denial_message": "Tool is not allowed: poll_messages",
    }
    assert receipt["lifecycle"]["same_logical_session_recovered"] is True
    assert receipt["mcp"] == {
        "protocol_version": PROTOCOL_VERSION,
        "server_name": "peerbridge-mcp",
        "server_version": __version__,
        "client_name": "lifecycle-receipt-test",
        "client_version": "1",
    }
    assert receipt["launches"]["before_crash"]["allowed_tools"] == [
        "bridge_status",
        "send_message",
    ]
    assert receipt["launches"]["after_restart"]["allowed_tools"] == [
        "bridge_status",
        "poll_messages",
    ]
    assert receipt["launches"]["before_crash"]["identity_capability"] == (
        receipt["launches"]["after_restart"]["identity_capability"]
    )
    assert receipt["launches"]["before_crash"]["identity_capability"]["bound"] is True
    assert "identity-capabilities" not in json.dumps(receipt, sort_keys=True)
    assert (
        receipt["presence"]["live_after_restart"]["last_seen_epoch"]
        > receipt["presence"]["residue_after_crash"]["last_seen_epoch"]
    )
    assert receipt["presence"]["clean_after_recovery_shutdown"] is True
    assert receipt["database"]["denied_tool_dispatch_count"] == 0
    assert receipt["database"]["final_bound_presence_count"] == 0
    assert receipt["security"]["provider_configuration_supplied"] is False
    assert receipt["security"]["external_provider_calls"] == 0
    assert receipt["security"]["credential_contents_recorded"] is False
    assert all(child.process.poll() is not None for child in children)

    stored = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert stored == receipt
    unsigned = dict(stored)
    receipt_sha = unsigned.pop("receipt_sha256")
    assert stable_sha256(unsigned) == receipt_sha
    verified = verify_receipt(receipt_path)
    assert verified == {
        "valid": True,
        "receipt_sha256": receipt_sha,
        "errors": [],
        "writes_performed": 0,
    }

    receipt_bytes = receipt_path.read_bytes()
    with pytest.raises(ReceiptError, match="refusing to overwrite"):
        capture_receipt(
            db_path=db_path,
            evidence_path=evidence_path,
            scope=receipt["database"]["scope"],
            agent_id=receipt["database"]["agent_id"],
            session_id=receipt["database"]["session_id"],
            output_path=receipt_path,
        )
    assert receipt_path.read_bytes() == receipt_bytes

    tampered_receipt = json.loads(json.dumps(stored))
    tampered_receipt["database"]["path"] = str(project_root / "must-not-read.sqlite3")
    tampered_receipt_path = project_root / "tampered-receipt.json"
    tampered_receipt_path.write_text(
        json.dumps(tampered_receipt, sort_keys=True), encoding="utf-8"
    )

    def unexpected_evidence_read(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid receipt followed its evidence paths")

    with monkeypatch.context() as context:
        context.setattr(lifecycle_receipt, "_bound_sections", unexpected_evidence_read)
        tampered_result = verify_receipt(tampered_receipt_path)
    assert tampered_result["valid"] is False
    assert tampered_result["errors"] == ["receipt_sha256"]
    assert tampered_result["writes_performed"] == 0

    event_count_before = receipt["database"]["chain_prefix_event_count"]
    unrelated_capability = ensure_agent_identity_capability(
        project_root,
        db_path,
        receipt["database"]["scope"],
        "unrelated-agent",
        allowed_tools=("bridge_status",),
    )
    later = _StdioChild(
        project_root=project_root,
        db_path=db_path,
        scope=receipt["database"]["scope"],
        agent_id="unrelated-agent",
        identity_capability_path=unrelated_capability.path,
        session_id="unrelated-session",
        allowed_tools=("bridge_status",),
    )
    try:
        _tool_payload(later.request(_call("later-status", "bridge_status", {})))
        later.close_stdin()
    finally:
        later.cleanup()
    assert later.process.poll() is not None
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        current_event_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE scope=?",
            (receipt["database"]["scope"],),
        ).fetchone()[0]
    assert current_event_count > event_count_before

    state_before_verify = _tree_state(project_root)
    after_progress = verify_receipt(receipt_path)
    state_after_verify = _tree_state(project_root)
    assert after_progress["valid"] is True
    assert after_progress["writes_performed"] == 0
    assert state_after_verify == state_before_verify

    original_evidence = evidence_path.read_bytes()
    evidence_path.write_bytes(original_evidence + b" \n")
    transcript_drift = verify_receipt(receipt_path)
    assert transcript_drift["valid"] is False
    assert "evidence" in transcript_drift["errors"]
    assert transcript_drift["writes_performed"] == 0
    evidence_path.write_bytes(original_evidence)
    assert verify_receipt(receipt_path)["valid"] is True

    capability_drift = json.loads(original_evidence)
    capability_command = capability_drift["before_crash"]["command"]
    capability_index = capability_command.index("--identity-capability") + 1
    capability_command[capability_index] = str(project_root / "forged-capability.json")
    evidence_path.write_text(json.dumps(capability_drift), encoding="utf-8")
    capability_result = verify_receipt(receipt_path)
    assert capability_result["valid"] is False
    assert any("identity capability" in error.lower() for error in capability_result["errors"])
    assert capability_result["writes_performed"] == 0
    evidence_path.write_bytes(original_evidence)
    assert verify_receipt(receipt_path)["valid"] is True

    launch_drift = json.loads(original_evidence)
    launch_drift["before_crash"]["command"].extend(
        ["--provider-id", "forbidden-provider"]
    )
    evidence_path.write_text(json.dumps(launch_drift), encoding="utf-8")
    launch_result = verify_receipt(receipt_path)
    assert launch_result["valid"] is False
    assert any("launch command" in error for error in launch_result["errors"])
    assert launch_result["writes_performed"] == 0
    evidence_path.write_bytes(original_evidence)
    assert verify_receipt(receipt_path)["valid"] is True

    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            "UPDATE messages SET body=? WHERE message_id=?",
            ("drifted durable body", receipt["message"]["message_id"]),
        )
        connection.commit()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert checkpoint is not None and checkpoint[0] == 0
    database_drift = verify_receipt(receipt_path)
    assert database_drift["valid"] is False
    assert any("content SHA mismatch" in error for error in database_drift["errors"])
    assert database_drift["writes_performed"] == 0


def test_verify_missing_lifecycle_receipt_is_read_only(tmp_path: Path) -> None:
    before = _tree_state(tmp_path)
    result = verify_receipt(tmp_path / "missing.json")
    assert result["valid"] is False
    assert result["writes_performed"] == 0
    assert _tree_state(tmp_path) == before

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"schema":', encoding="utf-8")
    before_malformed_verify = _tree_state(tmp_path)
    malformed_result = verify_receipt(malformed)
    assert malformed_result["valid"] is False
    assert malformed_result["writes_performed"] == 0
    assert _tree_state(tmp_path) == before_malformed_verify
