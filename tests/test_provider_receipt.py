from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import peerbridge_mcp.provider_receipt as provider_receipt_module
from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.provider_receipt import (
    _write_json_create_only,
    capture_receipt,
    verify_receipt,
)
from peerbridge_mcp.server import handle_request


MODERN_META = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}


def _write_fake_acpx_evidence(
    root: Path,
    *,
    scope: str,
    agent_id: str,
    model_id: str,
    result: dict,
    tool: str = "bridge_status",
) -> tuple[Path, Path]:
    session_id = "acpx-session-test"
    call_id = "call-provider-test"
    session = root / "session.json"
    stream = root / "session.stream.ndjson"
    session.write_text(
        json.dumps(
            {
                "schema": "acpx.session.v1",
                "acpx_record_id": session_id,
                "acp_session_id": session_id,
                "agent_command": "test-provider agent stdio",
                "acpx": {"current_model_id": model_id},
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "rawInput": {"tool_name": f"smoke__{tool}", "tool_input": {}},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "session/request_permission",
            "params": {"sessionId": session_id, "toolCall": {"toolCallId": call_id}},
        },
        {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {"outcome": {"optionId": "allow-once", "outcome": "selected"}},
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "completed",
                    "rawOutput": {"output": {"OkayOutput": json.dumps(result)}},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": f"scope {scope} agent {agent_id}"},
                },
            },
        },
    ]
    stream.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    return session, stream


def test_provider_receipt_binds_binary_acpx_tool_result_and_chain(tmp_path: Path) -> None:
    scope = "provider-test"
    agent_id = "official-provider"
    model_id = "provider-model"
    bridge = Bridge(
        tmp_path,
        tmp_path / "bridge.sqlite3",
        agent_id,
        scope,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
    )
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {**MODERN_META, "name": "bridge_status", "arguments": {}},
        },
        {"bridge_status"},
    )
    result = response["result"]["structuredContent"]
    session, stream = _write_fake_acpx_evidence(
        tmp_path, scope=scope, agent_id=agent_id, model_id=model_id, result=result
    )
    receipt = capture_receipt(
        db_path=tmp_path / "bridge.sqlite3",
        scope=scope,
        agent_id=agent_id,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
        reasoning_mode=None,
        tool="bridge_status",
        session_path=session,
        stream_path=stream,
        provider_binary=Path(sys.executable),
        provider_version_args=("--version",),
        acpx_cli_path=None,
        mcp_config_path=None,
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (receipt_path, session, stream, tmp_path / "bridge.sqlite3")
    }
    verified = verify_receipt(receipt_path)
    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in before
    }
    assert verified["valid"] is True
    assert verified["writes_performed"] == 0
    assert before == after


def test_provider_receipt_fails_closed_after_stream_drift(tmp_path: Path) -> None:
    scope = "provider-test"
    agent_id = "official-provider"
    model_id = "provider-model"
    bridge = Bridge(
        tmp_path,
        tmp_path / "bridge.sqlite3",
        agent_id,
        scope,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
    )
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {**MODERN_META, "name": "bridge_status", "arguments": {}},
        },
        {"bridge_status"},
    )
    result = response["result"]["structuredContent"]
    session, stream = _write_fake_acpx_evidence(
        tmp_path, scope=scope, agent_id=agent_id, model_id=model_id, result=result
    )
    receipt = capture_receipt(
        db_path=tmp_path / "bridge.sqlite3",
        scope=scope,
        agent_id=agent_id,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
        reasoning_mode=None,
        tool="bridge_status",
        session_path=session,
        stream_path=stream,
        provider_binary=Path(sys.executable),
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    lines = stream.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["unexpected"] = "prefix drift"
    lines[0] = json.dumps(first, sort_keys=True)
    stream.write_text("\n".join(lines) + "\n", encoding="utf-8")
    verified = verify_receipt(receipt_path)
    assert verified["valid"] is False
    assert "acpx_stream_prefix_sha256" in verified["errors"]


def test_provider_receipt_allows_append_only_session_progress(tmp_path: Path) -> None:
    scope = "provider-test"
    agent_id = "official-provider"
    model_id = "provider-model"
    bridge = Bridge(
        tmp_path,
        tmp_path / "bridge.sqlite3",
        agent_id,
        scope,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
    )
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {**MODERN_META, "name": "bridge_status", "arguments": {}},
        },
        {"bridge_status"},
    )
    result = response["result"]["structuredContent"]
    session, stream = _write_fake_acpx_evidence(
        tmp_path, scope=scope, agent_id=agent_id, model_id=model_id, result=result
    )
    receipt = capture_receipt(
        db_path=tmp_path / "bridge.sqlite3",
        scope=scope,
        agent_id=agent_id,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
        reasoning_mode=None,
        tool="bridge_status",
        session_path=session,
        stream_path=stream,
        provider_binary=Path(sys.executable),
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    session_value = json.loads(session.read_text(encoding="utf-8"))
    session_value["last_used_at"] = "later"
    session_value["messages"] = [{"User": {"content": "follow-up"}}]
    session.write_text(json.dumps(session_value), encoding="utf-8")
    with stream.open("a", encoding="utf-8") as target:
        target.write(json.dumps({"later": "append-only event"}) + "\n")
    verified = verify_receipt(receipt_path)
    assert verified["valid"] is True
    assert verified["writes_performed"] == 0


def test_provider_receipt_binds_intervening_domain_event_window(tmp_path: Path) -> None:
    scope = "provider-message-test"
    agent_id = "official-provider"
    model_id = "provider-model"
    bridge = Bridge(
        tmp_path,
        tmp_path / "bridge.sqlite3",
        agent_id,
        scope,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
    )
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                **MODERN_META,
                "name": "send_message",
                "arguments": {
                    "task_id": "provider-message-window",
                    "recipient": "peer-agent",
                    "subject": "DOMAIN_EVENT_WINDOW",
                    "body": "Receipt test message.",
                },
            },
        },
        {"send_message"},
    )
    result = response["result"]["structuredContent"]
    session, stream = _write_fake_acpx_evidence(
        tmp_path,
        scope=scope,
        agent_id=agent_id,
        model_id=model_id,
        result=result,
        tool="send_message",
    )
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": [
                    {
                        "name": "smoke",
                        "command": sys.executable,
                        "args": [
                            "-m",
                            "peerbridge_mcp",
                            "serve",
                            "--agent-id",
                            agent_id,
                            "--scope",
                            scope,
                            "--client-name",
                            "provider-acpx",
                            "--provider-id",
                            "official:provider",
                            "--model-id",
                            model_id,
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    receipt = capture_receipt(
        db_path=tmp_path / "bridge.sqlite3",
        scope=scope,
        agent_id=agent_id,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
        reasoning_mode=None,
        tool="send_message",
        session_path=session,
        stream_path=stream,
        provider_binary=Path(sys.executable),
        mcp_config_path=config,
    )
    assert [row["event_type"] for row in receipt["bridge"]["event_window"]] == [
        "tool.called",
        "message.sent",
        "tool.returned",
    ]
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert verify_receipt(receipt_path)["valid"] is True
    assert receipt["artifacts"]["mcp_config"]["sanitized_server"]["route"] == {
        "agent_id": agent_id,
        "scope": scope,
        "client_name": "provider-acpx",
        "provider_id": "official:provider",
        "model_id": model_id,
        "reasoning_mode": None,
    }

    receipt["bridge"]["event_window"] = [
        receipt["bridge"]["event_window"][0],
        receipt["bridge"]["event_window"][-1],
    ]
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    from peerbridge_mcp.bridge import stable_sha256

    receipt["receipt_sha256"] = stable_sha256(unsigned)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    verified = verify_receipt(receipt_path)
    assert verified["valid"] is False
    assert "event_window" in verified["errors"]


def test_provider_verify_never_starts_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = "provider-no-exec"
    agent_id = "official-provider"
    model_id = "provider-model"
    bridge = Bridge(
        tmp_path,
        tmp_path / "bridge.sqlite3",
        agent_id,
        scope,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
    )
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {**MODERN_META, "name": "bridge_status", "arguments": {}},
        },
        {"bridge_status"},
    )
    session, stream = _write_fake_acpx_evidence(
        tmp_path,
        scope=scope,
        agent_id=agent_id,
        model_id=model_id,
        result=response["result"]["structuredContent"],
    )
    receipt = capture_receipt(
        db_path=tmp_path / "bridge.sqlite3",
        scope=scope,
        agent_id=agent_id,
        client_name="provider-acpx",
        provider_id="official:provider",
        model_id=model_id,
        reasoning_mode=None,
        tool="bridge_status",
        session_path=session,
        stream_path=stream,
        provider_binary=Path(sys.executable),
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    def forbidden_process(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("verify_receipt attempted to start a process")

    monkeypatch.setattr(provider_receipt_module.subprocess, "run", forbidden_process)
    verified = verify_receipt(receipt_path)
    assert verified["valid"] is True
    assert verified["processes_started"] == 0


def test_receipt_create_only_writer_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "receipt.json"
    first = {"sequence": 1}
    _write_json_create_only(target, first)
    original = target.read_bytes()

    with pytest.raises(FileExistsError):
        _write_json_create_only(target, {"sequence": 2})

    assert target.read_bytes() == original
