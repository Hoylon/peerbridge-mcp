from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from peerbridge_mcp import server as server_module
from peerbridge_mcp.agent_identity import ensure_agent_identity_capability
from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.server import (
    MAX_STDIO_REQUEST_BYTES,
    _validate_schema_value,
    handle_request,
    serve,
)


def _tool_events(bridge: Bridge) -> list[str]:
    with sqlite3.connect(bridge.db_path) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM events WHERE event_type LIKE 'tool.%' "
                "ORDER BY sequence"
            )
        ]


def _identity_serve_arguments(
    project_root: Path,
    agent_id: str,
    *,
    scope: str = "default",
    allowed_tools: tuple[str, ...] | None = None,
    route_binding: dict[str, str | None] | None = None,
    bound_room_id: str | None = None,
) -> list[str]:
    root = Path(project_root).resolve()
    db_path = root / ".peerbridge" / "peerbridge.sqlite3"
    Bridge(root, db_path, "test-identity-authority", scope)
    capability = ensure_agent_identity_capability(
        root,
        db_path,
        scope,
        agent_id,
        allowed_tools=(
            tuple(sorted(server_module.LEGACY_CAPABILITY_TOOLS))
            if allowed_tools is None
            else allowed_tools
        ),
        route_binding=route_binding,
        bound_room_id=bound_room_id,
    )
    return [
        "--db",
        str(db_path),
        "--identity-capability",
        str(capability.path),
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


def test_schema_validation_rejects_non_finite_numbers_recursively() -> None:
    schema = {
        "type": "object",
        "properties": {
            "values": {"type": "array", "items": {"type": "number"}}
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    assert _validate_schema_value({"values": [0, 1.5]}, schema)
    for value in (float("nan"), float("inf"), float("-inf")):
        assert not _validate_schema_value({"values": [value]}, schema)


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


def test_stdio_session_call_budget_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    bridge = Bridge(tmp_path, tmp_path / "bridge.sqlite3", "model", "test")
    requests = "".join(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": "bridge_status", "arguments": {}},
            }
        )
        + "\n"
        for index in range(3)
    )
    stdin = io.StringIO(requests)
    stdout = io.StringIO()
    monkeypatch.setattr(server_module, "MAX_STDIO_CALLS_PER_SESSION", 2)
    monkeypatch.setattr(server_module.sys, "stdin", stdin)
    monkeypatch.setattr(server_module.sys, "stdout", stdout)

    assert serve(bridge, {"bridge_status"}) == 0

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [row["id"] for row in responses] == [0, 1, None]
    assert "result" in responses[0]
    assert "result" in responses[1]
    assert (
        responses[2]["error"]["message"]
        == "MCP session call budget exceeded; session closed"
    )


def test_stdio_replaces_oversized_serialized_response_with_bounded_error(
    tmp_path: Path, monkeypatch
) -> None:
    bridge = Bridge(tmp_path, tmp_path / "bridge.sqlite3", "model", "test")
    stdin = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "oversized",
                "method": "tools/call",
                "params": {"name": "bridge_status", "arguments": {}},
            }
        )
        + "\n"
    )
    stdout = io.StringIO()

    def oversized_response(*_args, **_kwargs):
        return {
            "jsonrpc": "2.0",
            "id": "oversized",
            "result": {"value": "x" * 8_192},
        }

    monkeypatch.setattr(server_module, "MAX_STDIO_RESPONSE_BYTES", 512)
    monkeypatch.setattr(server_module, "handle_request", oversized_response)
    monkeypatch.setattr(server_module.sys, "stdin", stdin)
    monkeypatch.setattr(server_module.sys, "stdout", stdout)

    assert serve(bridge, {"bridge_status"}) == 0

    encoded = stdout.getvalue().encode("utf-8")
    response = json.loads(stdout.getvalue())
    assert len(encoded) <= 512
    assert response["id"] == "oversized"
    assert response["error"]["code"] == -32603
    assert response["error"]["message"] == "response exceeds the stdio byte limit"


def test_credential_arguments_are_rejected_before_any_argument_hash_is_audited(
    tmp_path: Path,
) -> None:
    bridge = Bridge(tmp_path, tmp_path / "bridge.sqlite3", "model", "test")
    credential = bytes((115, 107, 45, 110, 111, 116, 45, 102, 111, 114, 45, 108, 111, 103, 115)).decode()

    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": "credential",
            "method": "tools/call",
            "params": {
                "name": "send_message",
                "arguments": {
                    "recipient": "peer",
                    "task_id": "task",
                    "subject": "unsafe",
                    "body": "authorization: " + "".join(("Bear", "er")) + " " + credential,
                    "idempotency_key": "credential-test",
                },
            },
        },
        {"send_message"},
    )

    assert response is not None and response["result"]["isError"] is True
    with sqlite3.connect(bridge.db_path) as connection:
        rows = connection.execute(
            "SELECT event_type, payload_json FROM events "
            "WHERE event_type LIKE 'tool.%' ORDER BY sequence"
        ).fetchall()
    assert [row[0] for row in rows] == ["tool.rejected"]
    payload = json.loads(rows[0][1])
    assert payload["reason"] == "credential-bearing-arguments"
    assert "arguments_sha256" not in payload
    assert credential not in rows[0][1]


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
            *_identity_serve_arguments(
                tmp_path,
                "stdio-agent",
                scope="stdio-test",
                route_binding={
                    "client_name": "codex",
                    "provider_id": "relay-main",
                    "model_id": "deepseek",
                    "reasoning_mode": "high",
                    "route_class": "relay",
                },
            ),
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


def test_stdio_rejects_oversized_frame_and_processes_the_next_request(
    tmp_path: Path,
) -> None:
    valid = {"jsonrpc": "2.0", "id": "after-limit", "method": "tools/list"}
    input_text = "x" * (MAX_STDIO_REQUEST_BYTES + 32) + "\n" + json.dumps(valid) + "\n"
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "peerbridge_mcp",
            "serve",
            "--project-root",
            str(tmp_path),
            "--agent-id",
            "bounded-stdio",
            *_identity_serve_arguments(tmp_path, "bounded-stdio"),
        ],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert responses[0]["id"] is None
    assert responses[0]["error"]["code"] == -32600
    assert "byte limit" in responses[0]["error"]["message"]
    assert responses[1]["id"] == "after-limit"
    assert isinstance(responses[1]["result"]["tools"], list)


def test_malformed_frames_consume_the_session_budget(
    tmp_path: Path, monkeypatch
) -> None:
    bridge = Bridge(tmp_path, tmp_path / "bridge.sqlite3", "model", "test")
    valid = {"jsonrpc": "2.0", "id": "must-not-run", "method": "tools/list"}
    stdin = io.StringIO("{not-json}\n" + json.dumps(valid) + "\n")
    stdout = io.StringIO()
    monkeypatch.setattr(server_module, "MAX_STDIO_CALLS_PER_SESSION", 1)
    monkeypatch.setattr(server_module.sys, "stdin", stdin)
    monkeypatch.setattr(server_module.sys, "stdout", stdout)

    assert serve(bridge, {"bridge_status"}) == 0

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["error"]["message"].endswith("session closed")
    assert all(row.get("id") != "must-not-run" for row in responses)


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
        *_identity_serve_arguments(
            tmp_path,
            "limited-stdio",
            scope="limited",
        ),
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
        *_identity_serve_arguments(tmp_path, "safe-default"),
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
        *_identity_serve_arguments(
            tmp_path,
            "catalog-agent",
            scope="rooms",
        ),
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
        "list_rooms",
        "room_members",
        "list_memories",
        "read_memory",
        "get_room_automation",
        "bridge_status",
    }.issubset(names)
    assert {
        "create_room",
        "join_room",
        "leave_room",
        "record_memory",
        "post_room_message",
        "set_room_automation",
    }.isdisjoint(names)
    agents = json.loads(by_id["agents"]["result"]["content"][0]["text"])
    assert agents["agents"][0]["agent_id"] == "catalog-agent"


def test_room_bound_stdio_denies_room_discovery_and_cross_room_calls(
    tmp_path: Path,
) -> None:
    scope = "bound-room"
    database = tmp_path / ".peerbridge" / "peerbridge.sqlite3"
    authority = Bridge(tmp_path, database, "room-authority", scope)
    authority.create_room({"room_id": "team", "name": "Team"})
    authority.join_room({"room_id": "team", "agent_id": "room-runner"})
    requests = [
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": "rooms",
            "method": "tools/call",
            "params": {"name": "list_rooms", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": "wrong-room",
            "method": "tools/call",
            "params": {
                "name": "room_members",
                "arguments": {"room_id": "lobby"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "direct-memory",
            "method": "tools/call",
            "params": {"name": "read_memory", "arguments": {"memory_id": "other"}},
        },
        {
            "jsonrpc": "2.0",
            "id": "bound-room",
            "method": "tools/call",
            "params": {
                "name": "room_members",
                "arguments": {"room_id": "team"},
            },
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
        "room-runner",
        *_identity_serve_arguments(
            tmp_path,
            "room-runner",
            scope=scope,
            allowed_tools=("list_rooms", "read_memory", "room_members"),
            bound_room_id="team",
        ),
        "--scope",
        scope,
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
    responses = {
        item["id"]: item
        for item in (
            json.loads(line) for line in completed.stdout.splitlines() if line.strip()
        )
    }
    assert {tool["name"] for tool in responses["list"]["result"]["tools"]} == {
        "room_members"
    }
    assert responses["rooms"]["error"]["message"] == "Tool is not allowed: list_rooms"
    assert responses["direct-memory"]["error"]["message"] == "Tool is not allowed: read_memory"
    wrong = json.loads(responses["wrong-room"]["result"]["content"][0]["text"])
    assert wrong["error"] == "Tool call must use the capability-bound room_id"
    bound = json.loads(responses["bound-room"]["result"]["content"][0]["text"])
    assert bound["room_id"] == "team"


def test_room_bound_capability_fences_after_leave_and_rejoin(
    tmp_path: Path,
) -> None:
    scope = "room-revision"
    database = tmp_path / ".peerbridge" / "peerbridge.sqlite3"
    authority = Bridge(tmp_path, database, "human-operator", scope)
    authority.create_room({"room_id": "team", "name": "Team"})
    joined = authority.join_room({"room_id": "team", "agent_id": "room-runner"})
    capability = ensure_agent_identity_capability(
        tmp_path,
        database,
        scope,
        "room-runner",
        allowed_tools=("bridge_status",),
        bound_room_id="team",
        bound_room_session_id=joined["room_session_id"],
    )
    authority.leave_room({"room_id": "team", "agent_id": "room-runner"})
    replacement = authority.join_room({"room_id": "team", "agent_id": "room-runner"})
    assert replacement["room_session_id"] != joined["room_session_id"]
    request = {
        "jsonrpc": "2.0",
        "id": "stale-room-session",
        "method": "tools/call",
        "params": {"name": "bridge_status", "arguments": {}},
    }
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "peerbridge_mcp",
            "serve",
            "--project-root",
            str(tmp_path),
            "--db",
            str(database),
            "--agent-id",
            "room-runner",
            "--identity-capability",
            str(capability.path),
            "--scope",
            scope,
        ],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "capability-bound room session changed" in completed.stderr
