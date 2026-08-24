from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path

import pytest

from peerbridge_mcp.agent_identity import ensure_agent_identity_capability
from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.claude_client_receipt import (
    RECEIPT_SCHEMA,
    capture_receipt,
    verify_receipt,
)
from peerbridge_mcp.collaboration_receipt import CHILD_RECEIPT_VERIFIERS
from peerbridge_mcp.provider_receipt import ReceiptError
from peerbridge_mcp.server import handle_request


META = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
SCOPE = "claude-native-receipt-test"
AGENT = "claude-native-test"
SERVER = "peerbridge-claude-native-test"
RUNTIME_SESSION = "claude-peerbridge-runtime-test"
CLAUDE_SESSION = "claude-stream-session-test"
THINKING_SENTINEL = "private-thought-that-must-never-be-copied"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(path: Path) -> dict:
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": _sha(path)}


def _state(paths: list[Path]) -> dict[Path, tuple[int, int, str]]:
    return {
        path: (path.stat().st_size, path.stat().st_mtime_ns, _sha(path))
        for path in paths
    }


def _server(root: Path, db: Path, capability_path: Path) -> dict:
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": [
            "-m",
            "peerbridge_mcp",
            "serve",
            "--project-root",
            str(root),
            "--db",
            str(db),
            "--agent-id",
            AGENT,
            "--scope",
            SCOPE,
            "--identity-capability",
            str(capability_path),
            "--client-name",
            "claude-code-native",
            "--provider-id",
            "anthropic-official:claude-code",
            "--model-id",
            "sonnet",
            "--reasoning-mode",
            "default",
            "--route-class",
            "official",
            "--allow-tool",
            "ack_message",
        ],
    }


def _evidence(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "bridge.sqlite3"
    sender = Bridge(root, db, "codex-test", SCOPE, session_id="codex-runtime")
    sent = sender.send_message(
        {
            "recipient": AGENT,
            "task_id": "claude-native-task",
            "subject": "CLAUDE_NATIVE_STEP",
            "body": "safe test body",
            "requested_provider_id": "anthropic-official:claude-code",
            "requested_model_id": "sonnet",
            "requested_reasoning_mode": "default",
            "requested_route_class": "official",
        }
    )
    claude = Bridge(
        root,
        db,
        AGENT,
        SCOPE,
        session_id=RUNTIME_SESSION,
        client_name="claude-code-native",
        provider_id="anthropic-official:claude-code",
        model_id="sonnet",
        reasoning_mode="default",
        route_class="official",
    )
    arguments = {"message_id": sent["message_id"], "agent_id": AGENT}
    response = handle_request(
        claude,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {**META, "name": "ack_message", "arguments": arguments},
        },
        {"ack_message"},
    )
    tool_result = response["result"]["structuredContent"]

    capability = ensure_agent_identity_capability(
        root,
        db,
        SCOPE,
        AGENT,
        route_binding={
            "client_name": "claude-code-native",
            "provider_id": "anthropic-official:claude-code",
            "model_id": "sonnet",
            "reasoning_mode": "default",
            "route_class": "official",
        },
    )
    config = root / "claude-native-mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {SERVER: _server(root, db, capability.path)}}),
        encoding="utf-8",
    )
    observed_model = "claude-sonnet-5"
    tool_id = "toolu_test_native_claude"
    rows = [
        {
            "type": "system",
            "subtype": "init",
            "session_id": CLAUDE_SESSION,
            "model": observed_model,
            "claude_code_version": platform.python_version(),
            "apiKeySource": "none",
            "mcp_servers": [{"name": SERVER, "status": "connected"}],
            "tools": [f"mcp__{SERVER}__ack_message"],
        },
        {
            "type": "assistant",
            "session_id": CLAUDE_SESSION,
            "message": {"content": [{"type": "thinking", "thinking": THINKING_SENTINEL}]},
        },
        {
            "type": "assistant",
            "session_id": CLAUDE_SESSION,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": f"mcp__{SERVER}__ack_message",
                        "input": arguments,
                    }
                ]
            },
        },
        {
            "type": "user",
            "session_id": CLAUDE_SESSION,
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": [{"type": "text", "text": json.dumps(tool_result)}],
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "session_id": CLAUDE_SESSION,
            "message": {"content": [{"type": "text", "text": f"{SCOPE} {AGENT} done"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": CLAUDE_SESSION,
            "is_error": False,
            "terminal_reason": "completed",
            "result": f"{SCOPE} {AGENT} done",
            "modelUsage": {observed_model: {"inputTokens": 10, "outputTokens": 2}},
        },
    ]
    transcript = root / "claude-native-stream.jsonl"
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    stderr = root / "claude-native.stderr.log"
    stderr.write_bytes(b"")
    prompt = root / "claude-native-prompt.txt"
    prompt.write_text("Safe native Claude MCP test prompt.\n", encoding="utf-8")
    lifecycle = root / "claude-native-lifecycle.json"
    lifecycle.write_text(
        json.dumps(
            {
                "schema": "peerbridge.test-native-client-lifecycle.v1",
                "attempt": "test",
                "client": "claude-code",
                "provider_class": "official",
                "server": SERVER,
                "allowed_tools": ["ack_message"],
                "started_utc": "2026-08-13T00:00:00Z",
                "finished_utc": "2026-08-13T00:00:01Z",
                "timeout_seconds": 60,
                "exit_code": 0,
                "timed_out": False,
                "relay_override_names_removed": [
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL",
                ],
                "config": _record(config),
                "stdout": _record(transcript),
                "stderr": _record(stderr),
                "prompt": _record(prompt),
                "credential_values_read": False,
                "credential_values_recorded": False,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return db, config, transcript, lifecycle, Path(sys.executable)


def _capture(root: Path) -> tuple[Path, dict, list[Path]]:
    db, config, transcript, lifecycle, binary = _evidence(root)
    receipt = capture_receipt(
        db_path=db,
        scope=SCOPE,
        agent_id=AGENT,
        client_name="claude-code-native",
        provider_id="anthropic-official:claude-code",
        model_id="sonnet",
        reasoning_mode="default",
        route_class="official",
        tool="ack_message",
        server_name=SERVER,
        transcript_path=transcript,
        config_path=config,
        lifecycle_path=lifecycle,
        client_binary=binary,
        client_version_args=("--version",),
    )
    receipt_path = root / "claude-native-receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    return receipt_path, receipt, [receipt_path, db, config, transcript, lifecycle]


def test_native_claude_receipt_binds_lifecycle_tool_and_zero_write_verify(
    tmp_path: Path,
) -> None:
    receipt_path, receipt, paths = _capture(tmp_path)
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["official_native_client_observed"] is True
    assert receipt["upstream_provider_identity_attested"] is False
    assert receipt["thinking_contents_recorded"] is False
    assert receipt["transcript"]["thinking_block_count"] == 1
    assert THINKING_SENTINEL not in json.dumps(receipt)
    before = _state(paths)
    result = verify_receipt(receipt_path)
    after = _state(paths)
    assert result["valid"] is True
    assert result["writes_performed"] == 0
    assert before == after


def test_native_claude_receipt_is_supported_as_a_collaboration_child(
    tmp_path: Path,
) -> None:
    receipt_path, _, _ = _capture(tmp_path)
    assert RECEIPT_SCHEMA in CHILD_RECEIPT_VERIFIERS
    assert CHILD_RECEIPT_VERIFIERS[RECEIPT_SCHEMA](receipt_path)["valid"] is True


def test_native_claude_capture_rejects_lifecycle_credential_claim(
    tmp_path: Path,
) -> None:
    db, config, transcript, lifecycle, binary = _evidence(tmp_path)
    value = json.loads(lifecycle.read_text(encoding="utf-8"))
    value["credential_values_read"] = True
    lifecycle.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ReceiptError, match="credential-safe"):
        capture_receipt(
            db_path=db,
            scope=SCOPE,
            agent_id=AGENT,
            client_name="claude-code-native",
            provider_id="anthropic-official:claude-code",
            model_id="sonnet",
            reasoning_mode="default",
            route_class="official",
            tool="ack_message",
            server_name=SERVER,
            transcript_path=transcript,
            config_path=config,
            lifecycle_path=lifecycle,
            client_binary=binary,
        )


def test_native_claude_verify_detects_transcript_tamper(tmp_path: Path) -> None:
    receipt_path, receipt, _ = _capture(tmp_path)
    transcript = Path(receipt["transcript"]["path"])
    transcript.write_text(transcript.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    result = verify_receipt(receipt_path)
    assert result["valid"] is False
    assert any(item.startswith("lifecycle:") for item in result["errors"])
