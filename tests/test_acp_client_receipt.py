from __future__ import annotations

import json
from pathlib import Path

import pytest

from peerbridge_mcp.acp_client_receipt import (
    RECEIPT_SCHEMA,
    capture_receipt,
    verify_receipt,
)
from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.collaboration_receipt import CHILD_RECEIPT_VERIFIERS
from peerbridge_mcp.provider_receipt import ReceiptError
from peerbridge_mcp.server import handle_request


META = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}


def _server(root: Path, *, model_id: str = "sonnet") -> dict:
    return {
        "name": "peerbridge-claude-test",
        "command": "python",
        "args": [
            "-m",
            "peerbridge_mcp",
            "serve",
            "--project-root",
            str(root),
            "--agent-id",
            "claude-relay-test",
            "--scope",
            "acp-client-test",
            "--client-name",
            "claude-agent-acp",
            "--provider-id",
            "relay:test",
            "--model-id",
            model_id,
            "--reasoning-mode",
            "default",
            "--allow-tool",
            "bridge_status",
        ],
    }


def _write_evidence(
    root: Path,
    *,
    tool_result: dict,
    model_id: str = "sonnet",
    allow_permission: bool = True,
) -> tuple[Path, Path]:
    server = _server(root, model_id=model_id)
    config = root / "claude-acp-config.json"
    config.write_text(json.dumps({"mcpServers": [server]}), encoding="utf-8")
    session_id = "claude-acp-session-test"
    call_id = "tool-call-test"
    permission_result = (
        {"outcome": {"outcome": "selected", "optionId": "allow"}}
        if allow_permission
        else {"outcome": {"outcome": "cancelled"}}
    )
    rows = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"mcpCapabilities": {"http": True}},
                "agentInfo": {
                    "name": "@agentclientprotocol/claude-agent-acp",
                    "title": "Claude Agent",
                    "version": "0.60.0",
                },
                "authMethods": [],
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {"cwd": str(root), "mcpServers": [server]},
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "sessionId": session_id,
                "configOptions": [
                    {"id": "model", "currentValue": "default", "type": "select"}
                ],
                "modes": {},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/set_config_option",
            "params": {
                "sessionId": session_id,
                "configId": "model",
                "value": model_id,
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "configOptions": [
                    {"id": "model", "currentValue": model_id, "type": "select"}
                ]
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "status"}]},
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": "mcp__peerbridge-claude-test__bridge_status",
                    "status": "pending",
                    "rawInput": {},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {"toolCallId": call_id},
                "options": [{"optionId": "allow"}, {"optionId": "reject"}],
            },
        },
        {"jsonrpc": "2.0", "id": 10, "result": permission_result},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "completed",
                    "rawOutput": [{"type": "text", "text": json.dumps(tool_result)}],
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
                    "content": {"type": "text", "text": "PeerBridge MCP status returned successfully."},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5}},
        },
    ]
    transcript = root / "claude-acp-transcript.ndjson"
    transcript.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-16",
    )
    return transcript, config


def _grok_server(root: Path) -> dict:
    return {
        "name": "peerbridge-grok-test",
        "command": "python",
        "args": [
            "-m",
            "peerbridge_mcp",
            "serve",
            "--project-root",
            str(root),
            "--agent-id",
            "grok-official-test",
            "--scope",
            "grok-acp-client-test",
            "--client-name",
            "grok-build-acpx",
            "--provider-id",
            "xai-official:grok-build",
            "--model-id",
            "grok-4.6",
            "--reasoning-mode",
            "default",
            "--allow-tool",
            "bridge_status",
        ],
    }


def _write_grok_evidence(root: Path, *, tool_result: dict) -> tuple[Path, Path]:
    server = _grok_server(root)
    config = root / "grok-acp-config.json"
    config.write_text(json.dumps({"mcpServers": [server]}), encoding="utf-8")
    session_id = "grok-acp-session-test"
    call_id = "grok-tool-call-test"
    rows = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"mcpCapabilities": {"http": True}},
                "authMethods": [{"id": "cached_token"}],
                "_meta": {
                    "grokShell": True,
                    "agentVersion": "1.0.1",
                    "agentId": "grok-agent-test",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "authenticate",
            "params": {"methodId": "cached_token"},
        },
        {"jsonrpc": "2.0", "id": 1, "result": {"_meta": {}}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": str(root), "mcpServers": [server]},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "sessionId": session_id,
                "models": {
                    "currentModelId": "grok-4.6",
                    "availableModels": [{"modelId": "grok-4.6"}],
                },
                "_meta": {},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "status"}]},
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": "use_tool",
                    "rawInput": {
                        "tool_name": "peerbridge-grok-test__bridge_status",
                        "tool_input": {},
                    },
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "title": "peerbridge-grok-test__bridge_status",
                    "rawInput": {
                        "tool_name": "peerbridge-grok-test__bridge_status",
                        "tool_input": {},
                        "variant": "default",
                    },
                },
            },
        },
        {
            "jsonrpc": "2.0",
            # ACP is bidirectional JSON-RPC. The agent may reuse the same ID as
            # the client's outstanding prompt request in the opposite direction.
            "id": 3,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {"toolCallId": call_id},
                "options": [{"optionId": "allow-once"}, {"optionId": "reject-once"}],
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"outcome": {"outcome": "selected", "optionId": "allow-once"}},
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
                    "rawOutput": {
                        "output": {"OkayOutput": json.dumps(tool_result)},
                        "server_name": "peerbridge-grok-test",
                        "tool_name": "bridge_status",
                    },
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
                    "content": {"type": "text", "text": "OFFICIAL_GROK_SESSION_NEW_MCP_OK"},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "stopReason": "end_turn",
                "_meta": {"usage": {"inputTokens": 10, "outputTokens": 5}},
            },
        },
    ]
    transcript = root / "grok-acp-transcript.ndjson"
    transcript.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return transcript, config


def _evidence(root: Path) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "bridge.sqlite3"
    bridge = Bridge(
        root,
        db,
        "claude-relay-test",
        "acp-client-test",
        client_name="claude-agent-acp",
        provider_id="relay:test",
        model_id="sonnet",
        reasoning_mode="default",
    )
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {**META, "name": "bridge_status", "arguments": {}},
        },
        {"bridge_status"},
    )
    result = response["result"]["structuredContent"]
    transcript, config = _write_evidence(root, tool_result=result)
    return db, transcript, config


def _grok_evidence(root: Path) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "grok-bridge.sqlite3"
    bridge = Bridge(
        root,
        db,
        "grok-official-test",
        "grok-acp-client-test",
        client_name="grok-build-acpx",
        provider_id="xai-official:grok-build",
        model_id="grok-4.6",
        reasoning_mode="default",
    )
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {**META, "name": "bridge_status", "arguments": {}},
        },
        {"bridge_status"},
    )
    transcript, config = _write_grok_evidence(
        root, tool_result=response["result"]["structuredContent"]
    )
    return db, transcript, config


def _capture(root: Path) -> tuple[Path, dict]:
    db, transcript, config = _evidence(root)
    receipt = capture_receipt(
        db_path=db,
        scope="acp-client-test",
        agent_id="claude-relay-test",
        client_name="claude-agent-acp",
        provider_id="relay:test",
        model_id="sonnet",
        reasoning_mode="default",
        tool="bridge_status",
        server_name="peerbridge-claude-test",
        transcript_path=transcript,
        config_path=config,
    )
    path = root / "receipt.json"
    path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    return path, receipt


def _state(paths: list[Path]) -> dict[Path, tuple[int, int, str]]:
    import hashlib

    return {
        path: (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in paths
    }


def test_direct_acp_receipt_binds_lifecycle_tool_permission_and_zero_write_verify(
    tmp_path: Path,
) -> None:
    path, receipt = _capture(tmp_path)
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["adapter"]["agent_info"]["name"].endswith("claude-agent-acp")
    assert receipt["upstream_provider_identity_attested"] is False
    assert receipt["transcript"]["real_model_inference_observed"] is True
    assert receipt["transcript"]["mcp_tool_invocation_observed"] is True
    paths = [
        path,
        Path(receipt["transcript"]["path"]),
        Path(receipt["config"]["path"]),
        Path(receipt["bridge"]["database_path"]),
    ]
    before = _state(paths)
    verified = verify_receipt(path)
    after = _state(paths)
    assert verified["valid"] is True
    assert verified["writes_performed"] == 0
    assert before == after


def test_direct_acp_receipt_allows_append_only_transcript_and_bridge_progress(tmp_path: Path) -> None:
    path, receipt = _capture(tmp_path)
    transcript = Path(receipt["transcript"]["path"])
    with transcript.open("a", encoding="utf-16") as target:
        target.write(json.dumps({"jsonrpc": "2.0", "method": "later"}) + "\n")
    Bridge(tmp_path, tmp_path / "bridge.sqlite3", "later", "acp-client-test")
    assert verify_receipt(path)["valid"] is True


def test_direct_acp_receipt_normalizes_only_missing_optional_v1_route_class(
    tmp_path: Path,
) -> None:
    path, receipt = _capture(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["config"]["route"].pop("route_class") is None
    unsigned = dict(document)
    unsigned.pop("receipt_sha256")
    from peerbridge_mcp.bridge import stable_sha256

    document["receipt_sha256"] = stable_sha256(unsigned)
    path.write_text(json.dumps(document), encoding="utf-8")

    assert verify_receipt(path)["valid"] is True

    document["config"]["route"].pop("model_id")
    unsigned = dict(document)
    unsigned.pop("receipt_sha256")
    document["receipt_sha256"] = stable_sha256(unsigned)
    path.write_text(json.dumps(document), encoding="utf-8")
    verified = verify_receipt(path)
    assert verified["valid"] is False
    assert any(error.startswith("config:") for error in verified["errors"])


def test_direct_acp_receipt_accepts_official_grok_session_new_dialect(tmp_path: Path) -> None:
    db, transcript, config = _grok_evidence(tmp_path)
    receipt = capture_receipt(
        db_path=db,
        scope="grok-acp-client-test",
        agent_id="grok-official-test",
        client_name="grok-build-acpx",
        provider_id="xai-official:grok-build",
        model_id="grok-4.6",
        reasoning_mode="default",
        tool="bridge_status",
        server_name="peerbridge-grok-test",
        transcript_path=transcript,
        config_path=config,
    )
    assert receipt["adapter"]["agent_info"]["dialect"] == "grok-shell"
    assert receipt["transcript"]["authentication_method_id"] == "cached_token"
    assert receipt["transcript"]["model_selection_method"] == "session/new-current-model"
    assert receipt["transcript"]["selected_model_id"] == "grok-4.6"
    path = tmp_path / "grok-receipt.json"
    path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    before = _state([path, transcript, config, db])
    verified = verify_receipt(path)
    assert verified["valid"] is True
    assert verified["writes_performed"] == 0
    assert _state([path, transcript, config, db]) == before


def test_direct_acp_receipt_rejects_prefix_or_config_tamper(tmp_path: Path) -> None:
    path, receipt = _capture(tmp_path)
    transcript = Path(receipt["transcript"]["path"])
    text = transcript.read_text(encoding="utf-16").replace("0.60.0", "0.61.0")
    transcript.write_text(text, encoding="utf-16")
    assert verify_receipt(path)["valid"] is False

    path2, receipt2 = _capture(tmp_path / "second")
    config = Path(receipt2["config"]["path"])
    value = json.loads(config.read_text(encoding="utf-8"))
    value["mcpServers"][0]["args"][-1] = "other_tool"
    config.write_text(json.dumps(value), encoding="utf-8")
    assert verify_receipt(path2)["valid"] is False


def test_direct_acp_receipt_rejects_missing_permission_model_or_result_binding(tmp_path: Path) -> None:
    db, _, _ = _evidence(tmp_path)
    bridge = Bridge(
        tmp_path,
        db,
        "evidence-reader",
        "acp-client-test",
    )
    del bridge
    result = {"wrong": "result"}
    transcript, config = _write_evidence(tmp_path, tool_result=result, allow_permission=False)
    with pytest.raises(ReceiptError, match="permission"):
        capture_receipt(
            db_path=db,
            scope="acp-client-test",
            agent_id="claude-relay-test",
            client_name="claude-agent-acp",
            provider_id="relay:test",
            model_id="sonnet",
            reasoning_mode="default",
            tool="bridge_status",
            server_name="peerbridge-claude-test",
            transcript_path=transcript,
            config_path=config,
        )

    transcript, config = _write_evidence(tmp_path, tool_result=result, model_id="other-model")
    with pytest.raises(ReceiptError, match="route differs"):
        capture_receipt(
            db_path=db,
            scope="acp-client-test",
            agent_id="claude-relay-test",
            client_name="claude-agent-acp",
            provider_id="relay:test",
            model_id="sonnet",
            reasoning_mode="default",
            tool="bridge_status",
            server_name="peerbridge-claude-test",
            transcript_path=transcript,
            config_path=config,
        )


def test_direct_acp_receipt_rejects_secret_environment_and_is_registered(tmp_path: Path) -> None:
    _, transcript, config = _evidence(tmp_path)
    value = json.loads(config.read_text(encoding="utf-8"))
    value["mcpServers"][0]["env"] = {"TOKEN": "sk-" + "x" * 30}
    config.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ReceiptError, match="credential|environment"):
        capture_receipt(
            db_path=tmp_path / "bridge.sqlite3",
            scope="acp-client-test",
            agent_id="claude-relay-test",
            client_name="claude-agent-acp",
            provider_id="relay:test",
            model_id="sonnet",
            reasoning_mode="default",
            tool="bridge_status",
            server_name="peerbridge-claude-test",
            transcript_path=transcript,
            config_path=config,
        )
    assert CHILD_RECEIPT_VERIFIERS[RECEIPT_SCHEMA] is verify_receipt
