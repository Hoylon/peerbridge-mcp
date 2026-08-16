"""SHA-bound receipts for direct ACP clients that invoke PeerBridge MCP tools.

This receipt is deliberately distinct from ``provider_receipt``.  A direct ACP
handshake proves which adapter participated and what it reported; it does not
turn a relay route into an official-provider attestation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .bridge import sha256_bytes, stable_sha256, utc_now
from .secret_scan import contains_secret
from .mcp_client_receipt import _raw_prefix, _read_jsonl_text
from .provider_receipt import (
    ReceiptError,
    _event_snapshot,
    _file_sha256,
    _readonly_connection,
    _verify_chain_prefix,
)


RECEIPT_SCHEMA = "peerbridge.acp-client-receipt.v1"
ALLOWED_PERMISSION_OPTIONS = {"allow", "allow-once", "allow_always"}
ROUTE_FLAGS = {
    "agent_id": "--agent-id",
    "scope": "--scope",
    "client_name": "--client-name",
    "provider_id": "--provider-id",
    "model_id": "--model-id",
    "reasoning_mode": "--reasoning-mode",
    "route_class": "--route-class",
}


def _json_rows(path: Path, prefix_line_count: int | None = None) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, Any]]:
    text, encoding, raw = _read_jsonl_text(path)
    lines = text.splitlines()
    if prefix_line_count is None:
        prefix_line_count = len(lines)
    if prefix_line_count < 1 or len(lines) < prefix_line_count:
        raise ReceiptError("ACP transcript has fewer lines than its bound prefix")
    prefix_text = "\n".join(lines[:prefix_line_count])
    if contains_secret(prefix_text):
        raise ReceiptError("ACP transcript prefix appears to contain a credential")
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines[:prefix_line_count], 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReceiptError(f"ACP transcript line {line_number} is not JSON") from exc
        if not isinstance(value, dict):
            raise ReceiptError(f"ACP transcript line {line_number} is not an object")
        rows.append((line_number, value))
    prefix = _raw_prefix(raw, encoding, prefix_line_count)
    return rows, {
        "encoding": encoding,
        "prefix_bytes": len(prefix),
        "prefix_line_count": prefix_line_count,
        "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
    }


def _response_after(
    rows: list[tuple[int, dict[str, Any]]],
    *,
    request_line: int,
    request_id: Any,
    before_line: int | None = None,
    required_result_keys: tuple[str, ...] = (),
) -> tuple[int, dict[str, Any]]:
    matches = [
        (line, row)
        for line, row in rows
        if line > request_line
        and (before_line is None or line < before_line)
        and row.get("id") == request_id
        and "result" in row
        and row.get("method") is None
        and isinstance(row.get("result"), dict)
        and all(key in row["result"] for key in required_result_keys)
    ]
    if len(matches) != 1:
        raise ReceiptError(
            f"expected one ACP response for request {request_id!r}, found {len(matches)}"
        )
    return matches[0]


def _one_request(
    rows: list[tuple[int, dict[str, Any]]], method: str
) -> tuple[int, dict[str, Any]]:
    matches = [(line, row) for line, row in rows if row.get("method") == method]
    if len(matches) != 1:
        raise ReceiptError(f"expected one ACP {method} request, found {len(matches)}")
    return matches[0]


def _server_snapshot(value: Any, *, server_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("name") != server_name:
        raise ReceiptError("ACP MCP server config does not identify the expected server")
    environment = value.get("env")
    if environment not in (None, [], {}):
        raise ReceiptError("ACP MCP server config contains environment data")
    command = value.get("command")
    args = value.get("args")
    if not isinstance(command, str) or not command:
        raise ReceiptError("ACP MCP server config lacks a stdio command")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ReceiptError("ACP MCP server config args must be a string array")
    snapshot = {
        "name": server_name,
        "command": command,
        "args": list(args),
        "cwd": value.get("cwd"),
        "environment_supplied": False,
    }
    if contains_secret(json.dumps(snapshot, sort_keys=True)):
        raise ReceiptError("ACP MCP server config appears to contain a credential")
    return snapshot


def _config_snapshot(path: Path, *, server_name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("ACP MCP config file is missing or invalid") from exc
    if not isinstance(value, dict):
        raise ReceiptError("ACP MCP config document must be an object")
    if contains_secret(json.dumps(value, sort_keys=True)):
        raise ReceiptError("ACP MCP config document appears to contain a credential")
    servers = value.get("mcpServers")
    if not isinstance(servers, list):
        raise ReceiptError("ACP MCP config requires an mcpServers array")
    matching = [item for item in servers if isinstance(item, dict) and item.get("name") == server_name]
    if len(matching) != 1:
        raise ReceiptError("ACP MCP config requires exactly one matching server")
    return _server_snapshot(matching[0], server_name=server_name)


def _flag_value(args: list[str], flag: str, *, required: bool) -> str | None:
    positions = [index for index, value in enumerate(args) if value == flag]
    if not positions:
        if required:
            raise ReceiptError(f"ACP MCP config is missing {flag}")
        return None
    if len(positions) != 1 or positions[0] + 1 >= len(args):
        raise ReceiptError(f"ACP MCP config has an ambiguous {flag}")
    return args[positions[0] + 1]


def _route_from_server(server: dict[str, Any]) -> dict[str, Any]:
    args = list(server["args"])
    return {
        key: _flag_value(
            args,
            flag,
            required=key not in {"reasoning_mode", "route_class"},
        )
        for key, flag in ROUTE_FLAGS.items()
    }


def _normalized_recorded_route(value: Any) -> dict[str, Any]:
    """Normalize the optional v1 route-class field without weakening route checks."""
    if not isinstance(value, dict):
        raise ReceiptError("recorded ACP MCP route must be an object")
    route = dict(value)
    route.setdefault("route_class", None)
    if set(route) != set(ROUTE_FLAGS):
        raise ReceiptError("recorded ACP MCP route has an invalid field set")
    return route


def _model_current_value(config_options: Any) -> str | None:
    if not isinstance(config_options, list):
        return None
    values = [
        item.get("currentValue")
        for item in config_options
        if isinstance(item, dict) and item.get("id") == "model"
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


def _session_current_model(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    models = result.get("models")
    if isinstance(models, dict) and isinstance(models.get("currentModelId"), str):
        return models["currentModelId"]
    return _model_current_value(result.get("configOptions"))


def _adapter_identity(initialize_result: Any) -> dict[str, Any]:
    if not isinstance(initialize_result, dict):
        raise ReceiptError("ACP initialize response is not an object")
    agent_info = initialize_result.get("agentInfo")
    if isinstance(agent_info, dict) and all(
        isinstance(agent_info.get(key), str) and agent_info.get(key)
        for key in ("name", "version")
    ):
        return {"dialect": "agent-info", **agent_info}
    metadata = initialize_result.get("_meta")
    if (
        isinstance(metadata, dict)
        and metadata.get("grokShell") is True
        and isinstance(metadata.get("agentVersion"), str)
        and metadata.get("agentVersion")
        and isinstance(metadata.get("agentId"), str)
        and metadata.get("agentId")
    ):
        return {
            "dialect": "grok-shell",
            "name": "grok-shell",
            "version": metadata["agentVersion"],
            "agent_id": metadata["agentId"],
            "grok_shell": True,
        }
    raise ReceiptError("ACP initialize response lacks adapter identity")


def _authentication_evidence(
    rows: list[tuple[int, dict[str, Any]]], *, before_line: int
) -> dict[str, Any]:
    requests = [
        (line, row)
        for line, row in rows
        if line < before_line and row.get("method") == "authenticate"
    ]
    if not requests:
        return {
            "authentication_observed": False,
            "authentication_method_id": None,
            "authentication_request_line": None,
            "authentication_response_line": None,
        }
    if len(requests) != 1:
        raise ReceiptError("expected at most one ACP authenticate request")
    request_line, request = requests[0]
    method_id = (request.get("params") or {}).get("methodId")
    if not isinstance(method_id, str) or not method_id:
        raise ReceiptError("ACP authenticate request lacks methodId")
    response_line, response = _response_after(
        rows,
        request_line=request_line,
        request_id=request.get("id"),
        before_line=before_line,
    )
    if not isinstance(response.get("result"), dict):
        raise ReceiptError("ACP authenticate response lacks a result object")
    return {
        "authentication_observed": True,
        "authentication_method_id": method_id,
        "authentication_request_line": request_line,
        "authentication_response_line": response_line,
    }


def _model_selection_evidence(
    rows: list[tuple[int, dict[str, Any]]],
    *,
    after_line: int,
    before_line: int,
    session_id: str,
    model_id: str,
    session_new_result: dict[str, Any],
) -> dict[str, Any]:
    requests = [
        (line, row)
        for line, row in rows
        if after_line < line < before_line
        and row.get("method") in {"session/set_config_option", "session/set_model"}
    ]
    if not requests:
        selected_model = _session_current_model(session_new_result)
        if selected_model != model_id:
            raise ReceiptError("ACP session/new did not confirm the expected model")
        return {
            "model_selection_method": "session/new-current-model",
            "model_selection_request_line": None,
            "model_selection_response_line": None,
            "selected_model_id": selected_model,
            "model_selection_sha256": stable_sha256(
                {"method": "session/new-current-model", "model_id": selected_model}
            ),
        }
    if len(requests) != 1:
        raise ReceiptError("expected at most one ACP model selection request")
    request_line, request = requests[0]
    params = request.get("params") or {}
    method = request.get("method")
    if params.get("sessionId") != session_id:
        raise ReceiptError("ACP model selection targets the wrong session")
    if method == "session/set_config_option":
        requested_model = params.get("value") if params.get("configId") == "model" else None
    else:
        requested_model = params.get("modelId") or params.get("model")
    if requested_model != model_id:
        raise ReceiptError("ACP model selection does not match the expected model")
    response_line, response = _response_after(
        rows,
        request_line=request_line,
        request_id=request.get("id"),
        before_line=before_line,
    )
    result = response.get("result") or {}
    selected_model = _session_current_model(result) or requested_model
    if selected_model != model_id:
        raise ReceiptError("ACP model selection response did not confirm the expected model")
    return {
        "model_selection_method": method,
        "model_selection_request_line": request_line,
        "model_selection_response_line": response_line,
        "selected_model_id": selected_model,
        "model_selection_sha256": stable_sha256(
            {"method": method, "request": params, "confirmed_model_id": selected_model}
        ),
    }


def _decode_tool_output(raw_output: Any) -> dict[str, Any]:
    if isinstance(raw_output, dict):
        okay_output = ((raw_output.get("output") or {}).get("OkayOutput"))
        if isinstance(okay_output, str):
            try:
                decoded = json.loads(okay_output)
            except json.JSONDecodeError as exc:
                raise ReceiptError("completed ACP MCP OkayOutput is not JSON") from exc
            if isinstance(decoded, dict):
                return decoded
        raise ReceiptError("completed ACP MCP result lacks OkayOutput JSON")
    if not isinstance(raw_output, list):
        raise ReceiptError("completed ACP MCP result has an unsupported shape")
    decoded: list[dict[str, Any]] = []
    for block in raw_output:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            decoded.append(candidate)
    if len(decoded) != 1:
        raise ReceiptError("ACP MCP tool result must contain exactly one JSON object")
    return decoded[0]


def _transcript_evidence(
    path: Path,
    *,
    server_name: str,
    tool: str,
    model_id: str,
    expected_server: dict[str, Any],
    prefix_line_count: int | None = None,
) -> dict[str, Any]:
    rows, prefix = _json_rows(path, prefix_line_count)
    initialize_line, initialize = _one_request(rows, "initialize")
    new_line, new_request = _one_request(rows, "session/new")
    prompt_line, prompt_request = _one_request(rows, "session/prompt")
    if not (initialize_line < new_line < prompt_line):
        raise ReceiptError("ACP lifecycle requests are out of order")
    initialize_response_line, initialize_response = _response_after(
        rows,
        request_line=initialize_line,
        request_id=initialize.get("id"),
        before_line=new_line,
    )
    initialize_result = initialize_response.get("result") or {}
    agent_info = _adapter_identity(initialize_result)
    protocol_version = initialize_result.get("protocolVersion")
    if protocol_version is None:
        raise ReceiptError("ACP initialize response lacks protocol version")

    new_params = new_request.get("params") or {}
    servers = new_params.get("mcpServers")
    if not isinstance(servers, list):
        raise ReceiptError("ACP session/new lacks MCP server configuration")
    matching_servers = [
        item for item in servers if isinstance(item, dict) and item.get("name") == server_name
    ]
    if len(matching_servers) != 1:
        raise ReceiptError("ACP session/new requires exactly one matching MCP server")
    embedded_server = _server_snapshot(matching_servers[0], server_name=server_name)
    if embedded_server != expected_server:
        raise ReceiptError("ACP session/new MCP server differs from the bound config file")
    new_response_line, new_response = _response_after(
        rows,
        request_line=new_line,
        request_id=new_request.get("id"),
        before_line=prompt_line,
    )
    new_result = new_response.get("result") or {}
    acp_session_id = str(new_result.get("sessionId") or "")
    if not acp_session_id:
        raise ReceiptError("ACP session/new response lacks sessionId")
    authentication = _authentication_evidence(rows, before_line=new_line)
    model_selection = _model_selection_evidence(
        rows,
        after_line=new_response_line,
        before_line=prompt_line,
        session_id=acp_session_id,
        model_id=model_id,
        session_new_result=new_result,
    )

    if (prompt_request.get("params") or {}).get("sessionId") != acp_session_id:
        raise ReceiptError("ACP prompt session does not match session/new")
    prompt_response_line, prompt_response = _response_after(
        rows,
        request_line=prompt_line,
        request_id=prompt_request.get("id"),
        required_result_keys=("stopReason",),
    )
    prompt_result = prompt_response.get("result") or {}
    prompt_usage = prompt_result.get("usage")
    if not isinstance(prompt_usage, dict):
        prompt_usage = (prompt_result.get("_meta") or {}).get("usage")
    if not prompt_result.get("stopReason") or not isinstance(prompt_usage, dict):
        raise ReceiptError("ACP prompt lacks completed real-inference evidence")

    title = f"mcp__{server_name}__{tool}"
    starts: list[tuple[int, dict[str, Any]]] = []
    updates: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for line, row in rows:
        if row.get("method") != "session/update":
            continue
        update = (row.get("params") or {}).get("update") or {}
        call_id = str(update.get("toolCallId") or "")
        raw_input = update.get("rawInput") or {}
        raw_tool_name = raw_input.get("tool_name") if isinstance(raw_input, dict) else None
        accepted_tool_names = {
            tool,
            f"{server_name}__{tool}",
            f"mcp__{server_name}__{tool}",
        }
        if update.get("sessionUpdate") == "tool_call" and (
            update.get("title") == title or raw_tool_name in accepted_tool_names
        ):
            starts.append((line, update))
        if call_id:
            updates.setdefault(call_id, []).append((line, update))
    if len(starts) != 1:
        raise ReceiptError(f"expected one direct ACP {title} call, found {len(starts)}")
    call_line, call_start = starts[0]
    call_id = str(call_start.get("toolCallId") or "")
    call_updates = updates.get(call_id) or []
    completed = [
        (line, update)
        for line, update in call_updates
        if update.get("sessionUpdate") == "tool_call_update"
        and update.get("status") == "completed"
    ]
    if len(completed) != 1:
        raise ReceiptError("ACP MCP tool call lacks one completed result")
    result_line, result_update = completed[0]
    raw_inputs = [
        (line, update["rawInput"])
        for line, update in call_updates
        if line < result_line and isinstance(update.get("rawInput"), dict)
    ]
    if not raw_inputs:
        raise ReceiptError("ACP MCP tool call lacks final object arguments")
    arguments_line, raw_arguments = max(raw_inputs, key=lambda item: item[0])
    arguments = raw_arguments.get("tool_input", raw_arguments)
    if not isinstance(arguments, dict):
        raise ReceiptError("ACP MCP tool arguments are not an object")
    tool_result = _decode_tool_output(result_update.get("rawOutput"))

    permissions = [
        (line, row)
        for line, row in rows
        if row.get("method") == "session/request_permission"
        and str(((row.get("params") or {}).get("toolCall") or {}).get("toolCallId") or "") == call_id
    ]
    if len(permissions) != 1:
        raise ReceiptError("ACP MCP tool call lacks one permission request")
    permission_line, permission_request = permissions[0]
    if not (arguments_line < permission_line < result_line):
        raise ReceiptError("ACP permission ordering is invalid")
    permission_response_line, permission_response = _response_after(
        rows,
        request_line=permission_line,
        request_id=permission_request.get("id"),
        before_line=result_line,
    )
    outcome = (permission_response.get("result") or {}).get("outcome") or {}
    if outcome.get("outcome") != "selected" or outcome.get("optionId") not in ALLOWED_PERMISSION_OPTIONS:
        raise ReceiptError("ACP MCP permission was not explicitly allowed")

    chunks = [
        str(((row.get("params") or {}).get("update") or {}).get("content", {}).get("text"))
        for line, row in rows
        if result_line < line < prompt_response_line
        and row.get("method") == "session/update"
        and ((row.get("params") or {}).get("update") or {}).get("sessionUpdate")
        == "agent_message_chunk"
        and isinstance(((row.get("params") or {}).get("update") or {}).get("content", {}).get("text"), str)
    ]
    final_response = "".join(chunks).strip()
    if not final_response:
        raise ReceiptError("ACP MCP completion lacks a following model response")

    return {
        **prefix,
        "initialize_request_line": initialize_line,
        "initialize_response_line": initialize_response_line,
        "protocol_version": protocol_version,
        "agent_info": agent_info,
        "initialize_result_sha256": stable_sha256(initialize_result),
        **authentication,
        "session_new_request_line": new_line,
        "session_new_response_line": new_response_line,
        "acp_session_id": acp_session_id,
        "session_new_semantic_sha256": stable_sha256(
            {"cwd": new_params.get("cwd"), "mcp_server": embedded_server}
        ),
        **model_selection,
        "prompt_request_line": prompt_line,
        "prompt_response_line": prompt_response_line,
        "prompt_result_sha256": stable_sha256(prompt_result),
        "prompt_usage_sha256": stable_sha256(prompt_usage),
        "server_name": server_name,
        "tool": tool,
        "tool_call_id": call_id,
        "tool_call_line": call_line,
        "arguments_line": arguments_line,
        "arguments_sha256": stable_sha256(arguments),
        "permission_request_line": permission_line,
        "permission_response_line": permission_response_line,
        "permission_option_id": outcome.get("optionId"),
        "tool_result_line": result_line,
        "tool_result_sha256": stable_sha256(tool_result),
        "final_response_sha256": sha256_bytes(final_response.encode("utf-8")),
        "real_model_inference_observed": True,
        "mcp_tool_invocation_observed": True,
    }


def _matching_bridge_pair(
    connection: sqlite3.Connection,
    *,
    scope: str,
    agent_id: str,
    tool: str,
    runtime_identity: dict[str, Any],
    arguments_sha256: str,
    result_sha256: str,
) -> tuple[sqlite3.Row, sqlite3.Row, dict[str, Any], dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM events WHERE scope=? ORDER BY sequence ASC", (scope,)
    ).fetchall()
    matches: list[tuple[sqlite3.Row, sqlite3.Row, dict[str, Any], dict[str, Any]]] = []
    for index, called in enumerate(rows):
        if called["event_type"] != "tool.called" or called["actor"] != agent_id:
            continue
        called_payload = json.loads(called["payload_json"])
        if (
            called_payload.get("tool") != tool
            or called_payload.get("runtime_identity") != runtime_identity
            or called_payload.get("arguments_sha256") != arguments_sha256
        ):
            continue
        for returned in rows[index + 1 :]:
            if returned["actor"] != agent_id:
                continue
            returned_payload = json.loads(returned["payload_json"])
            if returned["event_type"] == "tool.called" and (
                returned_payload.get("tool") == tool
                and returned_payload.get("session_id") == called_payload.get("session_id")
            ):
                break
            if (
                returned["event_type"] == "tool.returned"
                and returned_payload.get("tool") == tool
                and returned_payload.get("session_id") == called_payload.get("session_id")
                and returned_payload.get("runtime_identity") == runtime_identity
                and returned_payload.get("result_sha256") == result_sha256
            ):
                matches.append((called, returned, called_payload, returned_payload))
                break
    if len(matches) != 1:
        raise ReceiptError(f"expected one exact ACP-to-bridge event pair, found {len(matches)}")
    return matches[0]


def capture_receipt(
    *,
    db_path: Path,
    scope: str,
    agent_id: str,
    client_name: str,
    provider_id: str,
    model_id: str,
    reasoning_mode: str | None,
    route_class: str | None = None,
    tool: str,
    server_name: str,
    transcript_path: Path,
    config_path: Path,
) -> dict[str, Any]:
    for path in (db_path, transcript_path, config_path):
        if not path.is_file():
            raise ReceiptError(f"required ACP receipt evidence is absent: {path}")
    server = _config_snapshot(config_path, server_name=server_name)
    route = _route_from_server(server)
    expected_route = {
        "agent_id": agent_id,
        "scope": scope,
        "client_name": client_name,
        "provider_id": provider_id,
        "model_id": model_id,
        "reasoning_mode": reasoning_mode,
        "route_class": route_class,
    }
    if route != expected_route:
        raise ReceiptError("bound ACP MCP config route differs from expected runtime identity")
    transcript = _transcript_evidence(
        transcript_path,
        server_name=server_name,
        tool=tool,
        model_id=model_id,
        expected_server=server,
    )
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
        chain_rows = _verify_chain_prefix(connection, scope, int(returned["sequence"]))
        event_window = [
            _event_snapshot(row)
            for row in chain_rows
            if int(called["sequence"]) <= int(row["sequence"]) <= int(returned["sequence"])
        ]
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "created_utc": utc_now(),
        "adapter": {
            "protocol_version": transcript["protocol_version"],
            "agent_info": transcript["agent_info"],
            "agent_info_sha256": stable_sha256(transcript["agent_info"]),
            "identity_source": "direct-acp-initialize-self-report",
        },
        "config": {
            "path": str(config_path.resolve()),
            "size_bytes": config_path.stat().st_size,
            "file_sha256": _file_sha256(config_path),
            "sanitized_server": server,
            "sanitized_server_sha256": stable_sha256(server),
            "route": route,
        },
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
        "credential_contents_recorded": False,
    }
    receipt["receipt_sha256"] = stable_sha256(receipt)
    return receipt


def verify_receipt(receipt_path: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "receipt_sha256": "",
            "errors": [f"receipt:{exc}"],
            "writes_performed": 0,
        }
    expected_sha = str(receipt.get("receipt_sha256") or "")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if receipt.get("schema") != RECEIPT_SCHEMA:
        errors.append("schema")
    if stable_sha256(unsigned) != expected_sha:
        errors.append("receipt_sha256")
    adapter = receipt.get("adapter") or {}
    config_record = receipt.get("config") or {}
    transcript_record = receipt.get("transcript") or {}
    bridge = receipt.get("bridge") or {}
    try:
        config_path = Path(config_record["path"])
        if config_path.stat().st_size != config_record.get("size_bytes"):
            errors.append("config_size_bytes")
        if _file_sha256(config_path) != config_record.get("file_sha256"):
            errors.append("config_file_sha256")
        server = _config_snapshot(config_path, server_name=transcript_record["server_name"])
        if server != config_record.get("sanitized_server"):
            errors.append("sanitized_server")
        if stable_sha256(server) != config_record.get("sanitized_server_sha256"):
            errors.append("sanitized_server_sha256")
        if _route_from_server(server) != _normalized_recorded_route(
            config_record.get("route")
        ):
            errors.append("config_route")
    except (KeyError, OSError, ReceiptError, ValueError) as exc:
        errors.append(f"config:{exc}")
        server = {}
    try:
        current = _transcript_evidence(
            Path(transcript_record["path"]),
            server_name=transcript_record["server_name"],
            tool=transcript_record["tool"],
            model_id=bridge["runtime_identity"]["model_id"],
            expected_server=server,
            prefix_line_count=int(transcript_record["prefix_line_count"]),
        )
        for key, value in current.items():
            if transcript_record.get(key) != value:
                errors.append(f"transcript_{key}")
        if current.get("agent_info") != adapter.get("agent_info"):
            errors.append("adapter_agent_info")
        if stable_sha256(current.get("agent_info")) != adapter.get("agent_info_sha256"):
            errors.append("adapter_agent_info_sha256")
        if current.get("protocol_version") != adapter.get("protocol_version"):
            errors.append("adapter_protocol_version")
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
    if receipt.get("upstream_provider_identity_attested") is not False:
        errors.append("upstream_provider_identity_attested")
    if receipt.get("credential_contents_recorded") is not False:
        errors.append("credential_contents_recorded")
    return {
        "valid": not errors,
        "receipt_sha256": expected_sha,
        "errors": errors,
        "writes_performed": 0,
    }


def capture_main() -> int:
    parser = argparse.ArgumentParser(description="Capture one direct ACP client receipt.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--client-name", required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--reasoning-mode")
    parser.add_argument("--route-class", choices=("official", "relay", "local"))
    parser.add_argument("--tool", required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
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
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="x", encoding="utf-8", dir=args.output.parent, prefix=".acp-receipt-", delete=False
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
    parser = argparse.ArgumentParser(description="Verify a direct ACP receipt without writes.")
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    result = verify_receipt(args.receipt.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(capture_main())
