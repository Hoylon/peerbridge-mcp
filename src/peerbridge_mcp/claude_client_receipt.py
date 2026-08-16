"""SHA-bound receipts for native Claude Code MCP tool invocations.

Claude Code's ``--output-format stream-json`` is not ACP.  Keeping a distinct
receipt prevents a native client observation from being mislabeled as an ACP
or cryptographic upstream-provider attestation.  Thinking blocks are hashed as
part of the immutable transcript prefix but are never copied into the receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .acp_client_receipt import _matching_bridge_pair
from .bridge import sha256_bytes, stable_sha256, utc_now
from .secret_scan import contains_secret
from .mcp_client_receipt import _raw_prefix, _read_jsonl_text
from .provider_receipt import (
    ReceiptError,
    _event_snapshot,
    _file_sha256,
    _hashable_file_path,
    _provider_version,
    _readonly_connection,
    _verify_chain_prefix,
)


RECEIPT_SCHEMA = "peerbridge.claude-native-client-receipt.v1"
LIFECYCLE_SCHEMA = "peerbridge.test-native-client-lifecycle.v1"
SUPPORTED_TOOLS = {"ack_message", "bridge_status"}
ROUTE_FLAGS = {
    "agent_id": "--agent-id",
    "scope": "--scope",
    "client_name": "--client-name",
    "provider_id": "--provider-id",
    "model_id": "--model-id",
    "reasoning_mode": "--reasoning-mode",
    "route_class": "--route-class",
}
REQUIRED_RELAY_OVERRIDES_REMOVED = {
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
}


def _json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise ReceiptError(f"required {label} evidence is absent: {path}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        value = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"{label} evidence is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReceiptError(f"{label} evidence is not an object")
    if contains_secret(text):
        raise ReceiptError(f"{label} evidence appears to contain a credential")
    return value, raw


def _flag_value(args: list[str], flag: str, *, required: bool) -> str | None:
    positions = [index for index, value in enumerate(args) if value == flag]
    if not positions:
        if required:
            raise ReceiptError(f"Claude MCP config is missing {flag}")
        return None
    if len(positions) != 1 or positions[0] + 1 >= len(args):
        raise ReceiptError(f"Claude MCP config has an ambiguous {flag}")
    return args[positions[0] + 1]


def _config_snapshot(path: Path, *, server_name: str) -> dict[str, Any]:
    value, _ = _json_object(path, "Claude MCP config")
    servers = value.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {server_name}:
        raise ReceiptError("Claude MCP config requires exactly one matching server")
    server = servers[server_name]
    if not isinstance(server, dict):
        raise ReceiptError("Claude MCP server config is not an object")
    if server.get("env") not in (None, {}, []):
        raise ReceiptError("Claude MCP server config contains environment data")
    if server.get("type", "stdio") != "stdio":
        raise ReceiptError("Claude client receipt requires a stdio MCP server")
    command = server.get("command")
    args = server.get("args")
    if not isinstance(command, str) or not command:
        raise ReceiptError("Claude MCP server config lacks a command")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ReceiptError("Claude MCP server args must be a string array")
    allowed_tools: list[str] = []
    for index, item in enumerate(args):
        if item != "--allow-tool":
            continue
        if index + 1 >= len(args) or not args[index + 1]:
            raise ReceiptError("Claude MCP server has an invalid tool allowlist")
        allowed_tools.append(args[index + 1])
    if not allowed_tools or len(allowed_tools) != len(set(allowed_tools)):
        raise ReceiptError("Claude MCP server tool allowlist is absent or duplicated")
    route = {
        key: _flag_value(
            args,
            flag,
            required=key not in {"reasoning_mode", "route_class"},
        )
        for key, flag in ROUTE_FLAGS.items()
    }
    snapshot = {
        "name": server_name,
        "type": "stdio",
        "command": command,
        "args": list(args),
        "cwd": server.get("cwd"),
        "environment_supplied": False,
        "allowed_tools": allowed_tools,
        "route": route,
    }
    if contains_secret(json.dumps(snapshot, sort_keys=True)):
        raise ReceiptError("sanitized Claude MCP config appears to contain a credential")
    return snapshot


def _file_record(value: Any, *, label: str) -> tuple[Path, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ReceiptError(f"Claude lifecycle lacks {label} evidence")
    try:
        path = Path(value["path"]).resolve()
        expected_bytes = int(value["bytes"])
        expected_sha = str(value["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReceiptError(f"Claude lifecycle {label} evidence is malformed") from exc
    if not path.is_file():
        raise ReceiptError(f"Claude lifecycle {label} file is absent")
    if path.stat().st_size != expected_bytes or _file_sha256(path) != expected_sha:
        raise ReceiptError(f"Claude lifecycle {label} evidence drifted")
    return path, {
        "path": str(path),
        "size_bytes": expected_bytes,
        "sha256": expected_sha,
    }


def _lifecycle_snapshot(
    path: Path,
    *,
    config_path: Path,
    transcript_path: Path,
    server_name: str,
    allowed_tools: list[str],
) -> dict[str, Any]:
    value, raw = _json_object(path, "Claude lifecycle")
    if value.get("schema") != LIFECYCLE_SCHEMA:
        raise ReceiptError("unsupported Claude lifecycle schema")
    if (
        value.get("client") != "claude-code"
        or value.get("provider_class") != "official"
        or value.get("server") != server_name
        or value.get("exit_code") != 0
        or value.get("timed_out") is not False
        or value.get("credential_values_read") is not False
        or value.get("credential_values_recorded") is not False
    ):
        raise ReceiptError("Claude lifecycle did not prove a successful credential-safe run")
    removed = value.get("relay_override_names_removed")
    if (
        not isinstance(removed, list)
        or not all(isinstance(item, str) for item in removed)
        or not REQUIRED_RELAY_OVERRIDES_REMOVED.issubset(set(removed))
    ):
        raise ReceiptError("Claude lifecycle did not remove relay override names")
    if value.get("allowed_tools") != allowed_tools:
        raise ReceiptError("Claude lifecycle tool allowlist differs from the MCP config")
    config_evidence_path, config = _file_record(value.get("config"), label="config")
    stdout_path, stdout = _file_record(value.get("stdout"), label="stdout")
    _, stderr = _file_record(value.get("stderr"), label="stderr")
    _, prompt = _file_record(value.get("prompt"), label="prompt")
    if config_evidence_path != config_path.resolve() or stdout_path != transcript_path.resolve():
        raise ReceiptError("Claude lifecycle is bound to different config/transcript files")
    return {
        "path": str(path.resolve()),
        "size_bytes": len(raw),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "schema": LIFECYCLE_SCHEMA,
        "attempt": value.get("attempt"),
        "client": "claude-code",
        "provider_class": "official",
        "server": server_name,
        "allowed_tools": list(allowed_tools),
        "started_utc": value.get("started_utc"),
        "finished_utc": value.get("finished_utc"),
        "timeout_seconds": value.get("timeout_seconds"),
        "exit_code": 0,
        "timed_out": False,
        "relay_override_names_removed": sorted(set(removed)),
        "config": config,
        "stdout": stdout,
        "stderr": stderr,
        "prompt": prompt,
        "credential_values_read": False,
        "credential_values_recorded": False,
    }


def _decode_tool_result(block: dict[str, Any]) -> dict[str, Any]:
    if block.get("is_error") is True:
        raise ReceiptError("Claude MCP tool returned an error")
    content = block.get("content")
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        texts.extend(
            str(item["text"])
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        )
    decoded: list[dict[str, Any]] = []
    for text in texts:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            decoded.append(value)
    if len(decoded) != 1:
        raise ReceiptError("Claude MCP tool result must contain exactly one JSON object")
    return decoded[0]


def _model_matches(configured: str, observed: str) -> bool:
    if configured == observed:
        return True
    return bool(re.fullmatch(rf"claude-{re.escape(configured)}(?:-[A-Za-z0-9.]+)+", observed))


def _transcript_evidence(
    path: Path,
    *,
    server_name: str,
    tool: str,
    scope: str,
    agent_id: str,
    configured_model_id: str,
    allowed_tools: list[str],
    prefix_line_count: int | None = None,
) -> dict[str, Any]:
    if tool not in SUPPORTED_TOOLS:
        raise ReceiptError(f"unsupported Claude receipt tool: {tool}")
    text, encoding, raw = _read_jsonl_text(path)
    lines = text.splitlines()
    if prefix_line_count is None:
        prefix_line_count = len(lines)
    if prefix_line_count < 1 or len(lines) < prefix_line_count:
        raise ReceiptError("Claude transcript has fewer lines than its bound prefix")
    prefix_text = "\n".join(lines[:prefix_line_count])
    if contains_secret(prefix_text):
        raise ReceiptError("Claude transcript prefix appears to contain a credential")
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines[:prefix_line_count], 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReceiptError(f"Claude transcript line {line_number} is not JSON") from exc
        if not isinstance(value, dict):
            raise ReceiptError(f"Claude transcript line {line_number} is not an object")
        rows.append((line_number, value))
    systems = [item for item in rows if item[1].get("type") == "system" and item[1].get("subtype") == "init"]
    if len(systems) != 1:
        raise ReceiptError("Claude transcript requires exactly one init event")
    init_line, init = systems[0]
    session_id = str(init.get("session_id") or "")
    observed_model = str(init.get("model") or "")
    client_version = str(init.get("claude_code_version") or "")
    if not session_id or not observed_model or not client_version:
        raise ReceiptError("Claude init event lacks client/session/model identity")
    if not _model_matches(configured_model_id, observed_model):
        raise ReceiptError("Claude init model differs from the configured model alias")
    if init.get("apiKeySource") != "none":
        raise ReceiptError("Claude native receipt requires cached official login, not an API key source")
    servers = init.get("mcp_servers")
    if servers != [{"name": server_name, "status": "connected"}]:
        raise ReceiptError("Claude init did not connect exactly the bound MCP server")
    expected_tools = sorted(f"mcp__{server_name}__{name}" for name in allowed_tools)
    if sorted(init.get("tools") or []) != expected_tools:
        raise ReceiptError("Claude init tool inventory differs from the MCP allowlist")
    observed_sessions = {
        str(row.get("session_id"))
        for _, row in rows
        if row.get("session_id") is not None
    }
    if observed_sessions != {session_id}:
        raise ReceiptError("Claude transcript contains mixed session identities")

    full_tool_name = f"mcp__{server_name}__{tool}"
    tool_uses: list[tuple[int, dict[str, Any]]] = []
    tool_results: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    thinking_blocks = 0
    for line_number, row in rows:
        message = row.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                thinking_blocks += 1
            if block.get("type") == "tool_use" and block.get("name") == full_tool_name:
                tool_uses.append((line_number, block))
            if block.get("type") == "tool_result":
                tool_id = str(block.get("tool_use_id") or "")
                if tool_id:
                    tool_results.setdefault(tool_id, []).append((line_number, block))
    if len(tool_uses) != 1:
        raise ReceiptError(f"expected one native Claude {full_tool_name} call, found {len(tool_uses)}")
    tool_line, tool_use = tool_uses[0]
    tool_id = str(tool_use.get("id") or "")
    arguments = tool_use.get("input")
    matches = tool_results.get(tool_id) or []
    if not tool_id or not isinstance(arguments, dict) or len(matches) != 1:
        raise ReceiptError("Claude MCP call lacks unambiguous arguments/result linkage")
    result_line, result_block = matches[0]
    if result_line <= tool_line:
        raise ReceiptError("Claude MCP result precedes its tool call")
    tool_result = _decode_tool_result(result_block)
    if tool == "ack_message":
        route_receipt = tool_result.get("route_receipt")
        if (
            not isinstance(route_receipt, dict)
            or route_receipt.get("scope") != scope
            or route_receipt.get("agent_id") != agent_id
            or route_receipt.get("route_status") != "verified"
            or tool_result.get("consumer") != agent_id
            or arguments.get("agent_id") != agent_id
            or arguments.get("message_id") != tool_result.get("message_id")
        ):
            raise ReceiptError("Claude ack result lacks the expected verified route identity")
        runtime_session_id = str(route_receipt.get("session_id") or "")
    else:
        if tool_result.get("scope") != scope or tool_result.get("agent_id") != agent_id:
            raise ReceiptError("Claude bridge status result scope/agent mismatch")
        runtime_session_id = str(tool_result.get("session_id") or "")
    if not runtime_session_id:
        raise ReceiptError("Claude MCP result lacks a PeerBridge runtime session")

    completed = [
        (line, row)
        for line, row in rows
        if line > result_line and row.get("type") == "result"
    ]
    if len(completed) != 1:
        raise ReceiptError("Claude transcript requires one final result after the tool call")
    final_line, final = completed[0]
    final_text = final.get("result")
    model_usage = final.get("modelUsage")
    if (
        final.get("subtype") != "success"
        or final.get("is_error") is not False
        or final.get("terminal_reason") != "completed"
        or not isinstance(final_text, str)
        or scope not in final_text
        or agent_id not in final_text
        or not isinstance(model_usage, dict)
        or observed_model not in model_usage
    ):
        raise ReceiptError("Claude transcript lacks completed real-model inference evidence")
    raw_prefix = _raw_prefix(raw, encoding, prefix_line_count)
    return {
        "encoding": encoding,
        "prefix_bytes": len(raw_prefix),
        "prefix_line_count": prefix_line_count,
        "prefix_sha256": hashlib.sha256(raw_prefix).hexdigest(),
        "init_line": init_line,
        "claude_session_id": session_id,
        "observed_model_id": observed_model,
        "claude_code_version": client_version,
        "api_key_source": "none",
        "server_name": server_name,
        "allowed_tools": list(allowed_tools),
        "tool": tool,
        "tool_use_id": tool_id,
        "tool_call_line": tool_line,
        "arguments_sha256": stable_sha256(arguments),
        "tool_result_line": result_line,
        "tool_result_sha256": stable_sha256(tool_result),
        "runtime_session_id": runtime_session_id,
        "final_result_line": final_line,
        "final_result_sha256": stable_sha256(final),
        "final_response_sha256": sha256_bytes(final_text.encode("utf-8")),
        "model_usage_sha256": stable_sha256(model_usage),
        "thinking_block_count": thinking_blocks,
        "thinking_content_recorded": False,
        "real_model_inference_observed": True,
        "mcp_tool_invocation_observed": True,
    }


def capture_receipt(
    *,
    db_path: Path,
    scope: str,
    agent_id: str,
    client_name: str,
    provider_id: str,
    model_id: str,
    reasoning_mode: str | None,
    route_class: str | None,
    tool: str,
    server_name: str,
    transcript_path: Path,
    config_path: Path,
    lifecycle_path: Path,
    client_binary: Path,
    client_version_args: tuple[str, ...] = ("--version",),
) -> dict[str, Any]:
    for path in (db_path, transcript_path, config_path, lifecycle_path, client_binary):
        if not path.is_file():
            raise ReceiptError(f"required Claude receipt evidence is absent: {path}")
    config = _config_snapshot(config_path, server_name=server_name)
    expected_route = {
        "agent_id": agent_id,
        "scope": scope,
        "client_name": client_name,
        "provider_id": provider_id,
        "model_id": model_id,
        "reasoning_mode": reasoning_mode,
        "route_class": route_class,
    }
    if config["route"] != expected_route:
        raise ReceiptError("Claude MCP config route differs from the expected runtime identity")
    lifecycle = _lifecycle_snapshot(
        lifecycle_path,
        config_path=config_path,
        transcript_path=transcript_path,
        server_name=server_name,
        allowed_tools=config["allowed_tools"],
    )
    transcript = _transcript_evidence(
        transcript_path,
        server_name=server_name,
        tool=tool,
        scope=scope,
        agent_id=agent_id,
        configured_model_id=model_id,
        allowed_tools=config["allowed_tools"],
    )
    version_output = _provider_version(client_binary, client_version_args)
    if transcript["claude_code_version"] not in version_output:
        raise ReceiptError("Claude transcript client version differs from the live binary")
    runtime_identity = {
        "client_name": client_name,
        "provider_id": provider_id,
        "model_id": model_id,
        "reasoning_mode": reasoning_mode,
        "route_class": route_class,
    }
    with _readonly_connection(db_path) as connection:
        called, returned, called_payload, returned_payload = _matching_bridge_pair(
            connection,
            scope=scope,
            agent_id=agent_id,
            tool=tool,
            runtime_identity=runtime_identity,
            arguments_sha256=transcript["arguments_sha256"],
            result_sha256=transcript["tool_result_sha256"],
        )
        if called_payload.get("session_id") != transcript["runtime_session_id"]:
            raise ReceiptError("Claude transcript runtime session differs from the bridge event")
        chain_rows = _verify_chain_prefix(connection, scope, int(returned["sequence"]))
        event_window = [
            _event_snapshot(row)
            for row in chain_rows
            if int(called["sequence"]) <= int(row["sequence"]) <= int(returned["sequence"])
        ]
    client_identity_path = _hashable_file_path(client_binary)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "created_utc": utc_now(),
        "client": {
            "binary_path": str(client_identity_path.resolve()),
            "binary_sha256": _file_sha256(client_identity_path),
            "version_args": list(client_version_args),
            "version_output": version_output,
            "identity_source": "native-claude-code-stream-json",
        },
        "config": {
            "path": str(config_path.resolve()),
            "size_bytes": config_path.stat().st_size,
            "file_sha256": _file_sha256(config_path),
            "sanitized_server": config,
            "sanitized_server_sha256": stable_sha256(config),
        },
        "lifecycle": lifecycle,
        "transcript": {"path": str(transcript_path.resolve()), **transcript},
        "bridge": {
            "database_path": str(db_path.resolve()),
            "scope": scope,
            "agent_id": agent_id,
            "tool": tool,
            "runtime_session_id": called_payload.get("session_id"),
            "runtime_identity": runtime_identity,
            "arguments_sha256": called_payload.get("arguments_sha256"),
            "result_sha256": returned_payload.get("result_sha256"),
            "call_event": _event_snapshot(called),
            "return_event": _event_snapshot(returned),
            "event_window": event_window,
            "chain_prefix_event_count": len(chain_rows),
            "chain_prefix_head_sha256": returned["chain_sha256"],
        },
        "upstream_provider_identity_attested": False,
        "official_native_client_observed": True,
        "credential_contents_recorded": False,
        "thinking_contents_recorded": False,
    }
    receipt["receipt_sha256"] = stable_sha256(receipt)
    return receipt


def verify_receipt(receipt_path: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"valid": False, "receipt_sha256": "", "errors": [f"receipt:{exc}"], "writes_performed": 0}
    expected_sha = str(receipt.get("receipt_sha256") or "")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if receipt.get("schema") != RECEIPT_SCHEMA:
        errors.append("schema")
    if stable_sha256(unsigned) != expected_sha:
        errors.append("receipt_sha256")
    client = receipt.get("client") or {}
    config_record = receipt.get("config") or {}
    lifecycle_record = receipt.get("lifecycle") or {}
    transcript_record = receipt.get("transcript") or {}
    bridge = receipt.get("bridge") or {}
    try:
        binary = Path(client["binary_path"])
        if _file_sha256(binary) != client.get("binary_sha256"):
            errors.append("client_binary_sha256")
        version = _provider_version(binary, tuple(client["version_args"]))
        if version != client.get("version_output"):
            errors.append("client_version_output")
    except (KeyError, OSError, ReceiptError, subprocess.SubprocessError) as exc:
        errors.append(f"client:{exc}")
    try:
        config_path = Path(config_record["path"])
        if config_path.stat().st_size != config_record.get("size_bytes"):
            errors.append("config_size_bytes")
        if _file_sha256(config_path) != config_record.get("file_sha256"):
            errors.append("config_file_sha256")
        config = _config_snapshot(config_path, server_name=transcript_record["server_name"])
        if config != config_record.get("sanitized_server"):
            errors.append("sanitized_server")
        if stable_sha256(config) != config_record.get("sanitized_server_sha256"):
            errors.append("sanitized_server_sha256")
    except (KeyError, OSError, ReceiptError, ValueError) as exc:
        errors.append(f"config:{exc}")
        config = {}
        config_path = Path(".")
    try:
        lifecycle = _lifecycle_snapshot(
            Path(lifecycle_record["path"]),
            config_path=config_path,
            transcript_path=Path(transcript_record["path"]),
            server_name=transcript_record["server_name"],
            allowed_tools=config["allowed_tools"],
        )
        if lifecycle != lifecycle_record:
            errors.append("lifecycle")
    except (KeyError, OSError, ReceiptError, ValueError) as exc:
        errors.append(f"lifecycle:{exc}")
    try:
        current = _transcript_evidence(
            Path(transcript_record["path"]),
            server_name=transcript_record["server_name"],
            tool=transcript_record["tool"],
            scope=bridge["scope"],
            agent_id=bridge["agent_id"],
            configured_model_id=bridge["runtime_identity"]["model_id"],
            allowed_tools=config["allowed_tools"],
            prefix_line_count=int(transcript_record["prefix_line_count"]),
        )
        for key, value in current.items():
            if transcript_record.get(key) != value:
                errors.append(f"transcript_{key}")
    except (KeyError, OSError, ReceiptError, UnicodeError, ValueError) as exc:
        errors.append(f"transcript:{exc}")
        current = {}
    try:
        with _readonly_connection(Path(bridge["database_path"])) as connection:
            called, returned, called_payload, returned_payload = _matching_bridge_pair(
                connection,
                scope=bridge["scope"],
                agent_id=bridge["agent_id"],
                tool=bridge["tool"],
                runtime_identity=bridge["runtime_identity"],
                arguments_sha256=current["arguments_sha256"],
                result_sha256=current["tool_result_sha256"],
            )
            rows = _verify_chain_prefix(connection, bridge["scope"], int(returned["sequence"]))
            window = [
                _event_snapshot(row)
                for row in rows
                if int(called["sequence"]) <= int(row["sequence"]) <= int(returned["sequence"])
            ]
        comparisons = {
            "call_event": _event_snapshot(called),
            "return_event": _event_snapshot(returned),
            "event_window": window,
            "chain_prefix_event_count": len(rows),
            "chain_prefix_head_sha256": returned["chain_sha256"],
            "runtime_session_id": called_payload.get("session_id"),
            "arguments_sha256": called_payload.get("arguments_sha256"),
            "result_sha256": returned_payload.get("result_sha256"),
        }
        for key, value in comparisons.items():
            if bridge.get(key) != value:
                errors.append(f"bridge_{key}")
    except (KeyError, OSError, ReceiptError, sqlite3.Error, ValueError) as exc:
        errors.append(f"bridge:{exc}")
    expected_flags = {
        "upstream_provider_identity_attested": False,
        "official_native_client_observed": True,
        "credential_contents_recorded": False,
        "thinking_contents_recorded": False,
    }
    for key, value in expected_flags.items():
        if receipt.get(key) is not value:
            errors.append(key)
    return {
        "valid": not errors,
        "receipt_sha256": expected_sha,
        "errors": errors,
        "writes_performed": 0,
    }


def capture_main() -> int:
    parser = argparse.ArgumentParser(description="Capture one native Claude Code MCP receipt.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--client-name", required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--reasoning-mode")
    parser.add_argument("--route-class", choices=("official", "relay", "local"), required=True)
    parser.add_argument("--tool", choices=sorted(SUPPORTED_TOOLS), required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lifecycle", type=Path, required=True)
    parser.add_argument("--client-binary", type=Path, required=True)
    parser.add_argument("--client-version-arg", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite existing receipt: {args.output}")
    receipt = capture_receipt(
        db_path=args.db.resolve(),
        scope=args.scope,
        agent_id=args.agent_id,
        client_name=args.client_name,
        provider_id=args.provider_id,
        model_id=args.model_id,
        reasoning_mode=args.reasoning_mode,
        route_class=args.route_class,
        tool=args.tool,
        server_name=args.server_name,
        transcript_path=args.transcript.resolve(),
        config_path=args.config.resolve(),
        lifecycle_path=args.lifecycle.resolve(),
        client_binary=args.client_binary.resolve(),
        client_version_args=tuple(args.client_version_arg or ["--version"]),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="x", encoding="utf-8", dir=args.output.parent, prefix=".claude-native-receipt-", delete=False
    ) as target:
        temporary = Path(target.name)
        json.dump(receipt, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write("\n")
    try:
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"status": "CAPTURED", **verify_receipt(args.output)}, sort_keys=True))
    return 0


def verify_main() -> int:
    parser = argparse.ArgumentParser(description="Verify a native Claude receipt without writes.")
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    result = verify_receipt(args.receipt.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(capture_main())
