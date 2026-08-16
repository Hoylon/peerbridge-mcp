from __future__ import annotations

import json
import os
import subprocess
import sys
import sqlite3
from pathlib import Path

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.server import _validate_schema_value, handle_request


def _tool_events(bridge: Bridge) -> list[str]:
    with sqlite3.connect(bridge.db_path) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM events WHERE event_type LIKE 'tool.%' "
                "ORDER BY sequence"
            )
        ]


def test_nested_tool_schema_validation_is_recursive() -> None:
    schema = {
        "type": "object",
        "properties": {
            "route": {
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "limits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"rounds": {"type": "integer", "minimum": 1}},
                            "required": ["rounds"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["model"],
                "additionalProperties": False,
            }
        },
        "required": ["route"],
        "additionalProperties": False,
    }
    assert _validate_schema_value(
        {"route": {"model": "gpt", "limits": [{"rounds": 2}]}}, schema
    )
    assert not _validate_schema_value({"route": {}}, schema)
    assert not _validate_schema_value(
        {"route": {"model": "gpt", "unknown": True}}, schema
    )
    assert not _validate_schema_value(
        {"route": {"model": "gpt", "limits": [{"rounds": 0}]}}, schema
    )
    assert not _validate_schema_value(
        {"route": {"model": "gpt", "limits": [{"rounds": 1, "extra": 1}]}},
        schema,
    )
    assert not _validate_schema_value({"route": {"model": "gpt"}, "extra": 1}, schema)


def test_unrestricted_read_only_poll_does_not_grow_tool_audit(tmp_path: Path) -> None:
    bridge = Bridge(tmp_path, tmp_path / "bridge.sqlite3", "interactive", "test")
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {}},
        },
    )
    assert response is not None and "result" in response
    assert _tool_events(bridge) == []


def test_restricted_read_only_model_call_retains_receipt_events(tmp_path: Path) -> None:
    bridge = Bridge(tmp_path, tmp_path / "bridge.sqlite3", "model", "test")
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {}},
        },
        {"bridge_status"},
    )
    assert response is not None and "result" in response
    assert _tool_events(bridge) == ["tool.called", "tool.returned"]


def test_stdio_process_supports_legacy_and_modern_clients(tmp_path: Path) -> None:
    meta = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "stdio-test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }
    requests = [
        {
            "jsonrpc": "2.0",
            "id": "discover",
            "method": "server/discover",
            "params": meta,
        },
        {
            "jsonrpc": "2.0",
            "id": "modern-call",
            "method": "tools/call",
            "params": {**meta, "name": "bridge_status", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": "legacy-init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
    ]
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    command = [
        sys.executable,
        "-m",
        "peerbridge_mcp",
        "serve",
        "--project-root",
        str(tmp_path),
        "--agent-id",
        "stdio-agent",
        "--client-name",
        "codex",
        "--provider-id",
        "relay-main",
        "--model-id",
        "deepseek",
        "--reasoning-mode",
        "high",
        "--route-class",
        "relay",
        "--scope",
        "stdio-test",
    ]
    completed = subprocess.run(
        command,
        input="".join(json.dumps(item) + "\n" for item in requests),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    by_id = {item["id"]: item for item in responses}
    assert by_id["discover"]["result"]["resultType"] == "complete"
    assert by_id["modern-call"]["result"]["structuredContent"]["network_listener"] is False
    assert by_id["modern-call"]["result"]["structuredContent"]["runtime_identity"] == {
        "client_name": "codex",
        "provider_id": "relay-main",
        "model_id": "deepseek",
        "reasoning_mode": "high",
        "route_class": "relay",
    }
    assert by_id["legacy-init"]["result"]["protocolVersion"] == "2025-11-25"


def test_stdio_allow_tool_exposes_only_selected_tool(tmp_path: Path) -> None:
    requests = [
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": "denied",
            "method": "tools/call",
            "params": {"name": "send_message", "arguments": {}},
        },
    ]
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    command = [
        sys.executable,
        "-m",
        "peerbridge_mcp",
        "serve",
        "--project-root",
        str(tmp_path),
        "--agent-id",
        "limited-stdio",
        "--scope",
        "limited",
        "--allow-tool",
        "bridge_status",
    ]
    completed = subprocess.run(
        command,
        input="".join(json.dumps(item) + "\n" for item in requests),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    by_id = {item["id"]: item for item in responses}
    assert [tool["name"] for tool in by_id["list"]["result"]["tools"]] == [
        "bridge_status"
    ]
    assert by_id["denied"]["error"]["message"] == "Tool is not allowed: send_message"


def test_stdio_disables_artifact_content_read_by_default(tmp_path: Path) -> None:
    source = tmp_path / "public.txt"
    source.write_text("public source", encoding="utf-8")
    requests = [
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tools/call",
            "params": {"name": "read_artifact", "arguments": {"path": "public.txt"}},
        },
    ]
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    command = [
        sys.executable,
        "-m",
        "peerbridge_mcp",
        "serve",
        "--project-root",
        str(tmp_path),
        "--agent-id",
        "safe-default",
    ]
    completed = subprocess.run(
        command,
        input="".join(json.dumps(item) + "\n" for item in requests),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    by_id = {
        item["id"]: item
        for item in (
            json.loads(line) for line in completed.stdout.splitlines() if line.strip()
        )
    }
    assert "read_artifact" not in {
        tool["name"] for tool in by_id["list"]["result"]["tools"]
    }
    assert by_id["read"]["error"]["message"] == "Tool is not allowed: read_artifact"


def test_stdio_exposes_global_agents_and_room_tools(tmp_path: Path) -> None:
    requests = [
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": "agents",
            "method": "tools/call",
            "params": {"name": "list_agents", "arguments": {}},
        },
    ]
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    command = [
        sys.executable,
        "-m",
        "peerbridge_mcp",
        "serve",
        "--project-root",
        str(tmp_path),
        "--agent-id",
        "catalog-agent",
        "--scope",
        "rooms",
    ]
    completed = subprocess.run(
        command,
        input="".join(json.dumps(item) + "\n" for item in requests),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    by_id = {
        item["id"]: item
        for item in (
            json.loads(line) for line in completed.stdout.splitlines() if line.strip()
        )
    }
    names = {item["name"] for item in by_id["list"]["result"]["tools"]}
    assert {
        "list_agents",
        "create_room",
        "list_rooms",
        "join_room",
        "leave_room",
        "room_members",
        "record_memory",
        "list_memories",
        "read_memory",
        "revoke_memory",
        "post_room_message",
        "get_room_automation",
        "set_room_automation",
        "control_discussion",
        "advance_discussions",
        "reconcile_message_dispatches",
    }.issubset(names)
    agents = json.loads(by_id["agents"]["result"]["content"][0]["text"])
    assert agents["agents"][0]["agent_id"] == "catalog-agent"
