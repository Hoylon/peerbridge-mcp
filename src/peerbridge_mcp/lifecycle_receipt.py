"""Create-only receipts for an isolated MCP crash and recovery lifecycle."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .bridge import ZERO_SHA256, sha256_bytes, stable_sha256, utc_now
from .secret_scan import contains_secret
from .protocol import PROTOCOL_VERSION


RECEIPT_SCHEMA = "peerbridge.lifecycle-receipt.v1"
EVIDENCE_SCHEMA = "peerbridge.lifecycle-test-evidence.v1"
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_EMPTY_RUNTIME_IDENTITY = {
    "client_name": None,
    "provider_id": None,
    "model_id": None,
    "reasoning_mode": None,
    "route_class": None,
}
_MCP_SERVER_NAME = "peerbridge-mcp"
_REQUIRED_ENVIRONMENT_KEYS = {
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "PYTHONUTF8",
}
_OPTIONAL_ENVIRONMENT_KEYS = {"SYSTEMROOT", "TEMP", "TMP", "WINDIR"}


class ReceiptError(RuntimeError):
    """Lifecycle evidence is absent, ambiguous, or internally inconsistent."""


LifecycleReceiptError = ReceiptError


def _read_object(path: Path, *, evidence: bool = False) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise ReceiptError(f"required JSON file is absent: {path}")
    raw = path.read_bytes()
    if evidence and len(raw) > MAX_EVIDENCE_BYTES:
        raise ReceiptError("lifecycle evidence exceeds the size limit")
    try:
        text = raw.decode("utf-8")
        value = json.loads(text)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ReceiptError(f"invalid UTF-8 JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise ReceiptError(f"expected a JSON object: {path}")
    if evidence and contains_secret(text):
        raise ReceiptError("lifecycle evidence appears to contain credential material")
    return value, raw


@contextlib.contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise ReceiptError(f"lifecycle database is absent: {path}")
    wal_path = path.with_name(path.name + "-wal")
    if wal_path.is_file() and wal_path.stat().st_size:
        raise ReceiptError("lifecycle database has uncheckpointed WAL evidence")
    # The lifecycle is quiescent after bounded child shutdown. Immutable mode is
    # therefore complete and avoids creating or touching SQLite sidecar files.
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=3.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()
        if wal_path.is_file() and wal_path.stat().st_size:
            raise ReceiptError("lifecycle database changed during verification")


def _require_id(value: Any, label: str) -> str:
    text = str(value or "")
    if not _SAFE_ID.fullmatch(text):
        raise ReceiptError(f"invalid {label}")
    return text


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReceiptError(f"{label} must be an integer")
    return value


def _require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReceiptError(f"{label} must be numeric")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ReceiptError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise ReceiptError(f"{label} must be finite")
    return result


def _tool_result(response: Mapping[str, Any], label: str) -> dict[str, Any]:
    if "error" in response:
        raise ReceiptError(f"{label} unexpectedly returned a JSON-RPC error")
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError") is not False:
        raise ReceiptError(f"{label} did not complete successfully")
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        raise ReceiptError(f"{label} must return exactly one content block")
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != "text":
        raise ReceiptError(f"{label} did not return JSON text")
    try:
        value = json.loads(str(block.get("text") or ""))
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"{label} returned invalid JSON text") from exc
    if not isinstance(value, dict):
        raise ReceiptError(f"{label} result is not an object")
    return value


def _exchanges(phase: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    raw = phase.get("exchanges")
    if not isinstance(raw, list):
        raise ReceiptError(f"{label} exchanges must be an array")
    seen_ids: set[Any] = set()
    exchanges: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ReceiptError(f"{label} exchange is not an object")
        request = item.get("request")
        response = item.get("response")
        if not isinstance(request, dict) or not isinstance(response, dict):
            raise ReceiptError(f"{label} exchange lacks request/response objects")
        request_id = request.get("id")
        if (
            request.get("jsonrpc") != "2.0"
            or response.get("jsonrpc") != "2.0"
            or isinstance(request_id, bool)
            or not isinstance(request_id, (int, str))
            or response.get("id") != request_id
            or request_id in seen_ids
        ):
            raise ReceiptError(f"{label} exchange has an invalid JSON-RPC identity")
        seen_ids.add(request_id)
        exchanges.append({"request": request, "response": response})
    return exchanges


def _exchange_signature(exchange: Mapping[str, Any]) -> tuple[str, str | None]:
    request = exchange["request"]
    method = str(request.get("method") or "")
    params = request.get("params")
    tool = params.get("name") if isinstance(params, dict) else None
    return method, str(tool) if tool is not None else None


def _arguments(exchange: Mapping[str, Any], tool: str) -> dict[str, Any]:
    request = exchange["request"]
    params = request.get("params")
    if (
        request.get("method") != "tools/call"
        or not isinstance(params, dict)
        or params.get("name") != tool
    ):
        raise ReceiptError(f"expected a {tool} tool call")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ReceiptError(f"{tool} arguments are not an object")
    return arguments


def _listed_tools(exchange: Mapping[str, Any], label: str) -> list[str]:
    if _exchange_signature(exchange) != ("tools/list", None):
        raise ReceiptError(f"{label} does not begin with tools/list")
    result = exchange["response"].get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise ReceiptError(f"{label} tools/list result is invalid")
    names = [item.get("name") for item in tools if isinstance(item, dict)]
    if len(names) != len(tools) or any(not isinstance(name, str) for name in names):
        raise ReceiptError(f"{label} tools/list contains an invalid tool")
    if len(set(names)) != len(names):
        raise ReceiptError(f"{label} tools/list contains duplicate tools")
    return names


def _initialize_result(exchange: Mapping[str, Any], label: str) -> dict[str, str]:
    if _exchange_signature(exchange) != ("initialize", None):
        raise ReceiptError(f"{label} does not begin with initialize")
    request = exchange["request"]
    params = request.get("params")
    if not isinstance(params, dict) or params.get("protocolVersion") != PROTOCOL_VERSION:
        raise ReceiptError(f"{label} requested an unexpected MCP protocol")
    client_info = params.get("clientInfo")
    if (
        not isinstance(client_info, dict)
        or not isinstance(client_info.get("name"), str)
        or not client_info["name"]
        or not isinstance(client_info.get("version"), str)
        or not client_info["version"]
    ):
        raise ReceiptError(f"{label} initialize request lacks client identity")
    result = exchange["response"].get("result")
    server_info = result.get("serverInfo") if isinstance(result, dict) else None
    capabilities = result.get("capabilities") if isinstance(result, dict) else None
    if (
        not isinstance(result, dict)
        or result.get("protocolVersion") != PROTOCOL_VERSION
        or not isinstance(server_info, dict)
        or server_info.get("name") != _MCP_SERVER_NAME
        or not isinstance(server_info.get("version"), str)
        or not server_info["version"]
        or not isinstance(capabilities, dict)
        or not isinstance(capabilities.get("tools"), dict)
    ):
        raise ReceiptError(f"{label} initialize response is invalid")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "server_name": _MCP_SERVER_NAME,
        "server_version": server_info["version"],
        "client_name": client_info["name"],
        "client_version": client_info["version"],
    }


def _launch_snapshot(
    phase: Mapping[str, Any],
    *,
    project_root: Path,
    db_path: Path,
    scope: str,
    agent_id: str,
    session_id: str,
    allowed_tools: list[str],
    label: str,
) -> dict[str, Any]:
    command = phase.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise ReceiptError(f"{label} launch command is invalid")
    executable = Path(command[0])
    if not executable.is_absolute():
        raise ReceiptError(f"{label} Python executable is not absolute")
    fixed = [
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
        "--session-id",
        session_id,
    ]
    allow_arguments = [
        value
        for tool in allowed_tools
        for value in ("--allow-tool", tool)
    ]
    if command[1:] != [*fixed, *allow_arguments]:
        raise ReceiptError(f"{label} launch command or allowlist drifted")
    cwd = Path(str(phase.get("cwd") or ""))
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ReceiptError(f"{label} working directory is invalid")
    environment_keys = phase.get("environment_keys")
    if not isinstance(environment_keys, list) or not all(
        isinstance(item, str) and item for item in environment_keys
    ):
        raise ReceiptError(f"{label} environment key list is invalid")
    environment_set = set(environment_keys)
    if (
        len(environment_set) != len(environment_keys)
        or not _REQUIRED_ENVIRONMENT_KEYS.issubset(environment_set)
        or not environment_set.issubset(
            _REQUIRED_ENVIRONMENT_KEYS | _OPTIONAL_ENVIRONMENT_KEYS
        )
    ):
        raise ReceiptError(f"{label} environment is not credential-free and isolated")
    return {
        "python_executable": str(executable.resolve()),
        "command_sha256": stable_sha256(command),
        "project_root": str(project_root),
        "database_path": str(db_path),
        "scope": scope,
        "agent_id": agent_id,
        "session_id": session_id,
        "allowed_tools": allowed_tools,
        "cwd": str(cwd.resolve()),
        "environment_keys": sorted(environment_set),
    }


def _presence_identity(row: Mapping[str, Any], label: str) -> dict[str, Any]:
    expected_keys = {
        "scope",
        "agent_id",
        "session_id",
        "transport",
        "client_name",
        "provider_id",
        "model_id",
        "reasoning_mode",
        "route_class",
        "last_seen_utc",
        "last_seen_epoch",
    }
    if set(row) != expected_keys:
        raise ReceiptError(f"{label} presence snapshot has unexpected fields")
    if not isinstance(row.get("last_seen_utc"), str) or not row["last_seen_utc"]:
        raise ReceiptError(f"{label} presence snapshot lacks a timestamp")
    last_seen_epoch = _require_number(
        row.get("last_seen_epoch"), f"{label} presence epoch"
    )
    if last_seen_epoch <= 0:
        raise ReceiptError(f"{label} presence epoch must be positive")
    return {key: row[key] for key in sorted(expected_keys)}


def _one_presence(
    value: Any, *, scope: str, agent_id: str, session_id: str, label: str
) -> dict[str, Any]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ReceiptError(f"{label} must contain one live session")
    row = _presence_identity(value[0], label)
    expected = {
        "scope": scope,
        "agent_id": agent_id,
        "session_id": session_id,
        "transport": "stdio",
        **_EMPTY_RUNTIME_IDENTITY,
    }
    if any(row.get(key) != expected_value for key, expected_value in expected.items()):
        raise ReceiptError(f"{label} presence identity mismatch")
    return row


def _status_result(
    exchange: Mapping[str, Any],
    *,
    db_path: Path,
    scope: str,
    agent_id: str,
    session_id: str,
    label: str,
) -> dict[str, Any]:
    _arguments(exchange, "bridge_status")
    result = _tool_result(exchange["response"], label)
    if (
        result.get("scope") != scope
        or result.get("agent_id") != agent_id
        or result.get("session_id") != session_id
        or result.get("transport") != "stdio"
        or result.get("network_listener") is not False
        or Path(str(result.get("database") or "")).resolve() != db_path
        or result.get("runtime_identity") != _EMPTY_RUNTIME_IDENTITY
    ):
        raise ReceiptError(f"{label} status identity or transport mismatch")
    presence = result.get("presence")
    sessions = presence.get("online_sessions") if isinstance(presence, dict) else None
    if not isinstance(sessions, list):
        raise ReceiptError(f"{label} status lacks live presence")
    matching = [
        item
        for item in sessions
        if isinstance(item, dict)
        and item.get("agent_id") == agent_id
        and item.get("session_id") == session_id
        and item.get("transport") == "stdio"
    ]
    if len(matching) != 1:
        raise ReceiptError(f"{label} status does not show the selected live session")
    if any(matching[0].get(key) is not None for key in _EMPTY_RUNTIME_IDENTITY):
        raise ReceiptError(f"{label} status contains provider configuration")
    return result


def _shutdown(value: Any, *, method: str, successful: bool, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptError(f"{label} shutdown observation is not an object")
    timeout_seconds = _require_number(value.get("timeout_seconds"), f"{label} timeout")
    elapsed_seconds = _require_number(value.get("elapsed_seconds"), f"{label} elapsed time")
    exit_code = _require_int(value.get("exit_code"), f"{label} exit code")
    if (
        value.get("method") != method
        or value.get("alive_after_wait") is not False
        or value.get("fallback_kill_used") is not False
        or not 0 < timeout_seconds <= 30
        or not 0 <= elapsed_seconds <= timeout_seconds + 0.25
        or (exit_code == 0) is not successful
    ):
        raise ReceiptError(f"{label} shutdown was not bounded as required")
    return {
        "method": method,
        "timeout_seconds": timeout_seconds,
        "elapsed_seconds": elapsed_seconds,
        "exit_code": exit_code,
        "alive_after_wait": False,
        "fallback_kill_used": False,
    }


def _phase_header(phase: Any, label: str) -> tuple[dict[str, Any], int, list[str]]:
    if not isinstance(phase, dict):
        raise ReceiptError(f"{label} phase is not an object")
    if phase.get("spawned_by") != "test" or phase.get("transport") != "stdio":
        raise ReceiptError(f"{label} child was not recorded as a test-owned stdio process")
    pid = _require_int(phase.get("pid"), f"{label} pid")
    allowed = phase.get("allowed_tools")
    if pid <= 0 or not isinstance(allowed, list) or not all(
        isinstance(item, str) for item in allowed
    ):
        raise ReceiptError(f"{label} process metadata is invalid")
    if len(set(allowed)) != len(allowed):
        raise ReceiptError(f"{label} allowlist contains duplicates")
    return phase, pid, sorted(allowed)


def _parse_evidence(
    value: dict[str, Any],
    *,
    db_path: Path,
    scope: str,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    if value.get("schema") != EVIDENCE_SCHEMA:
        raise ReceiptError("unsupported lifecycle evidence schema")
    if (
        value.get("scope") != scope
        or value.get("agent_id") != agent_id
        or value.get("session_id") != session_id
    ):
        raise ReceiptError("lifecycle evidence identity mismatch")
    evidence_db = Path(str(value.get("database_path") or "")).resolve()
    project_root = Path(str(value.get("project_root") or "")).resolve()
    try:
        db_path.relative_to(project_root)
    except ValueError as exc:
        raise ReceiptError("lifecycle database is not isolated inside the test root") from exc
    if evidence_db != db_path or not project_root.is_dir():
        raise ReceiptError("lifecycle evidence database or project root mismatch")
    if (
        value.get("subprocess_owner") != "test"
        or value.get("external_provider_calls") != 0
        or value.get("credential_inputs") != 0
    ):
        raise ReceiptError("lifecycle evidence crossed its process or provider boundary")

    before, before_pid, before_allowed = _phase_header(
        value.get("before_crash"), "before-crash"
    )
    recovery, recovery_pid, recovery_allowed = _phase_header(
        value.get("after_restart"), "after-restart"
    )
    if before_pid == recovery_pid:
        raise ReceiptError("crash and recovery evidence must identify distinct child processes")
    if before_allowed != ["bridge_status", "send_message"]:
        raise ReceiptError("before-crash allowlist is not the selected narrow set")
    if recovery_allowed != ["bridge_status", "poll_messages"]:
        raise ReceiptError("after-restart allowlist is not the selected narrow set")

    before_launch = _launch_snapshot(
        before,
        project_root=project_root,
        db_path=db_path,
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        allowed_tools=before_allowed,
        label="before-crash",
    )
    recovery_launch = _launch_snapshot(
        recovery,
        project_root=project_root,
        db_path=db_path,
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        allowed_tools=recovery_allowed,
        label="after-restart",
    )
    if (
        before_launch["python_executable"] != recovery_launch["python_executable"]
        or before_launch["cwd"] != recovery_launch["cwd"]
    ):
        raise ReceiptError("crash and recovery children used different runtimes")

    before_exchanges = _exchanges(before, "before-crash")
    recovery_exchanges = _exchanges(recovery, "after-restart")
    if [_exchange_signature(item) for item in before_exchanges] != [
        ("initialize", None),
        ("tools/list", None),
        ("tools/call", "poll_messages"),
        ("tools/call", "bridge_status"),
        ("tools/call", "send_message"),
    ]:
        raise ReceiptError("before-crash MCP exchange sequence is incomplete")
    if [_exchange_signature(item) for item in recovery_exchanges] != [
        ("initialize", None),
        ("tools/list", None),
        ("tools/call", "bridge_status"),
        ("tools/call", "poll_messages"),
    ]:
        raise ReceiptError("after-restart MCP exchange sequence is incomplete")
    before_protocol = _initialize_result(before_exchanges[0], "before-crash")
    recovery_protocol = _initialize_result(recovery_exchanges[0], "after-restart")
    if before_protocol != recovery_protocol:
        raise ReceiptError("crash and recovery MCP identities differ")
    if sorted(_listed_tools(before_exchanges[1], "before-crash")) != before_allowed:
        raise ReceiptError("before-crash server exposed tools outside its allowlist")
    if sorted(_listed_tools(recovery_exchanges[1], "after-restart")) != recovery_allowed:
        raise ReceiptError("after-restart server exposed tools outside its allowlist")

    denied_arguments = _arguments(before_exchanges[2], "poll_messages")
    denied = before_exchanges[2]["response"].get("error")
    if not isinstance(denied, dict) or denied != {
        "code": -32602,
        "message": "Tool is not allowed: poll_messages",
    }:
        raise ReceiptError("selected poll_messages denial is absent")

    before_status = _status_result(
        before_exchanges[3],
        db_path=db_path,
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        label="before-crash",
    )
    recovery_status = _status_result(
        recovery_exchanges[2],
        db_path=db_path,
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        label="after-restart",
    )
    if before_status.get("message_count") != 0 or recovery_status.get("message_count") != 1:
        raise ReceiptError("status results do not bracket one durable message")

    send_arguments = _arguments(before_exchanges[4], "send_message")
    send_result = _tool_result(before_exchanges[4]["response"], "send_message")
    poll_arguments = _arguments(recovery_exchanges[3], "poll_messages")
    poll_result = _tool_result(recovery_exchanges[3]["response"], "poll_messages")
    after_cursor = _require_int(
        poll_arguments.get("after_cursor"), "poll_messages after_cursor"
    )
    if (
        send_arguments.get("recipient") != agent_id
        or poll_arguments.get("agent_id") != agent_id
        or after_cursor != 0
    ):
        raise ReceiptError("message recovery did not use the selected agent and initial cursor")
    messages = poll_result.get("messages")
    if (
        not isinstance(messages, list)
        or len(messages) != 1
        or not isinstance(messages[0], dict)
        or messages[0].get("message_id") != send_result.get("message_id")
        or messages[0].get("content_sha256") != send_result.get("content_sha256")
    ):
        raise ReceiptError("restarted child did not recover the sent message")

    live_before = _one_presence(
        before.get("presence_while_live"),
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        label="before-crash live",
    )
    crash_residue = _one_presence(
        before.get("presence_after_shutdown"),
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        label="after-crash residue",
    )
    live_recovery = _one_presence(
        recovery.get("presence_while_live"),
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        label="after-restart live",
    )
    if crash_residue != live_before:
        raise ReceiptError("crash residue does not match the terminated live session")
    if live_recovery["last_seen_epoch"] <= crash_residue["last_seen_epoch"]:
        raise ReceiptError("restarted child did not refresh stale presence")
    if recovery.get("presence_after_shutdown") != []:
        raise ReceiptError("recovered child did not clean up live presence")

    crash_shutdown = _shutdown(
        before.get("shutdown"), method="terminate", successful=False, label="crash"
    )
    recovery_shutdown = _shutdown(
        recovery.get("shutdown"), method="stdin_eof", successful=True, label="recovery"
    )
    return {
        "authorization": {
            "before_crash_allowed_tools": before_allowed,
            "after_restart_allowed_tools": recovery_allowed,
            "denied_tool": "poll_messages",
            "denied_arguments_sha256": stable_sha256(denied_arguments),
            "denial_code": -32602,
            "denial_message": "Tool is not allowed: poll_messages",
        },
        "lifecycle": {
            "agent_id": agent_id,
            "session_id": session_id,
            "before_crash_pid": before_pid,
            "after_restart_pid": recovery_pid,
            "same_logical_session_recovered": True,
            "crash_shutdown": crash_shutdown,
            "recovery_shutdown": recovery_shutdown,
        },
        "launches": {
            "before_crash": before_launch,
            "after_restart": recovery_launch,
        },
        "mcp": before_protocol,
        "presence": {
            "live_before_crash": live_before,
            "residue_after_crash": crash_residue,
            "live_after_restart": live_recovery,
            "clean_after_recovery_shutdown": True,
        },
        "security": {
            "children_spawned_by_test_only": True,
            "stdio_transport_only": True,
            "network_listener_observed": False,
            "provider_configuration_supplied": False,
            "external_provider_calls": 0,
            "credential_inputs": 0,
            "credential_contents_recorded": False,
        },
        "send_arguments": send_arguments,
        "send_result": send_result,
        "poll_arguments": poll_arguments,
        "poll_result": poll_result,
        "recovered_message": messages[0],
    }


def _event_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"event {row['sequence']} payload is not JSON") from exc
    if not isinstance(value, dict):
        raise ReceiptError(f"event {row['sequence']} payload is not an object")
    return value


def _event_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sequence": int(row["sequence"]),
        "event_id": row["event_id"],
        "actor": row["actor"],
        "event_type": row["event_type"],
        "task_id": row["task_id"],
        "created_utc": row["created_utc"],
        "payload_sha256": row["payload_sha256"],
        "prev_chain_sha256": row["prev_chain_sha256"],
        "chain_sha256": row["chain_sha256"],
    }


def _verify_chain_prefix(rows: list[sqlite3.Row], through_sequence: int) -> list[sqlite3.Row]:
    prefix = [row for row in rows if int(row["sequence"]) <= through_sequence]
    if not prefix:
        raise ReceiptError("audit chain prefix is empty")
    previous = ZERO_SHA256
    for row in prefix:
        payload_sha = sha256_bytes(row["payload_json"].encode("utf-8"))
        envelope = {
            "event_id": row["event_id"],
            "scope": row["scope"],
            "actor": row["actor"],
            "event_type": row["event_type"],
            "task_id": row["task_id"],
            "payload_sha256": payload_sha,
            "created_utc": row["created_utc"],
            "prev_chain_sha256": previous,
        }
        chain_sha = stable_sha256(envelope)
        if row["payload_sha256"] != payload_sha:
            raise ReceiptError(f"event {row['sequence']} payload SHA mismatch")
        if row["prev_chain_sha256"] != previous:
            raise ReceiptError(f"event {row['sequence']} previous-chain SHA mismatch")
        if row["chain_sha256"] != chain_sha:
            raise ReceiptError(f"event {row['sequence']} chain SHA mismatch")
        previous = row["chain_sha256"]
    return prefix


def _matching_tool_pair(
    rows: list[sqlite3.Row],
    *,
    agent_id: str,
    session_id: str,
    tool: str,
    arguments_sha256: str,
    result_sha256: str,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    matches: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    for index, called in enumerate(rows):
        if called["actor"] != agent_id or called["event_type"] != "tool.called":
            continue
        try:
            called_payload = _event_payload(called)
        except ReceiptError:
            continue
        if (
            called_payload.get("session_id") != session_id
            or called_payload.get("runtime_identity") != _EMPTY_RUNTIME_IDENTITY
            or called_payload.get("tool") != tool
            or called_payload.get("arguments_sha256") != arguments_sha256
        ):
            continue
        for returned in rows[index + 1 :]:
            if returned["actor"] != agent_id:
                continue
            try:
                returned_payload = _event_payload(returned)
            except ReceiptError:
                continue
            if (
                returned["event_type"] == "tool.called"
                and returned_payload.get("session_id") == session_id
            ):
                break
            if (
                returned["event_type"] == "tool.returned"
                and returned_payload.get("session_id") == session_id
                and returned_payload.get("runtime_identity") == _EMPTY_RUNTIME_IDENTITY
                and returned_payload.get("tool") == tool
                and returned_payload.get("result_sha256") == result_sha256
            ):
                matches.append((called, returned))
                break
    if len(matches) != 1:
        raise ReceiptError(f"expected one exact {tool} event pair, found {len(matches)}")
    return matches[0]


def _message_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    try:
        artifacts = json.loads(row["artifact_paths_json"])
    except json.JSONDecodeError as exc:
        raise ReceiptError("message artifact paths are invalid JSON") from exc
    if not isinstance(artifacts, list):
        raise ReceiptError("message artifact paths are not an array")
    route_fields = (
        "route_profile_id",
        "requested_provider_id",
        "requested_model_id",
        "requested_reasoning_mode",
        "requested_route_class",
        "route_request_sha256",
    )
    if any(row[key] is not None for key in route_fields):
        raise ReceiptError("lifecycle message unexpectedly contains provider routing")
    content = {
        "message_id": row["message_id"],
        "scope": row["scope"],
        "room_id": row["room_id"],
        "task_id": row["task_id"],
        "sender": row["sender"],
        "recipient": row["recipient"],
        "subject": row["subject"],
        "body": row["body"],
        "priority": row["priority"],
        "reply_to": row["reply_to"],
        "artifact_paths": artifacts,
        "route_request": None,
        "created_utc": row["created_utc"],
    }
    if stable_sha256(content) != row["content_sha256"]:
        raise ReceiptError("bound message content SHA mismatch")
    return {
        "sequence": int(row["sequence"]),
        "message_id": row["message_id"],
        "scope": row["scope"],
        "room_id": row["room_id"],
        "task_id": row["task_id"],
        "sender": row["sender"],
        "recipient": row["recipient"],
        "subject_sha256": sha256_bytes(row["subject"].encode("utf-8")),
        "body_sha256": sha256_bytes(row["body"].encode("utf-8")),
        "priority": row["priority"],
        "reply_to": row["reply_to"],
        "artifact_paths_sha256": stable_sha256(artifacts),
        "created_utc": row["created_utc"],
        "acknowledged_utc": row["acknowledged_utc"],
        "content_sha256": row["content_sha256"],
    }


def _database_evidence(
    *,
    db_path: Path,
    scope: str,
    agent_id: str,
    session_id: str,
    facts: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    send_arguments_sha = stable_sha256(facts["send_arguments"])
    send_result_sha = stable_sha256(facts["send_result"])
    poll_arguments_sha = stable_sha256(facts["poll_arguments"])
    poll_result_sha = stable_sha256(facts["poll_result"])
    with _readonly_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM events WHERE scope=? ORDER BY sequence ASC", (scope,)
        ).fetchall()
        send_called, send_returned = _matching_tool_pair(
            rows,
            agent_id=agent_id,
            session_id=session_id,
            tool="send_message",
            arguments_sha256=send_arguments_sha,
            result_sha256=send_result_sha,
        )
        poll_called, poll_returned = _matching_tool_pair(
            rows,
            agent_id=agent_id,
            session_id=session_id,
            tool="poll_messages",
            arguments_sha256=poll_arguments_sha,
            result_sha256=poll_result_sha,
        )
        if int(poll_called["sequence"]) <= int(send_returned["sequence"]):
            raise ReceiptError("poll evidence does not follow the shutdown/restart boundary")
        prefix = _verify_chain_prefix(rows, int(poll_returned["sequence"]))
        poll_calls = []
        for row in prefix:
            if row["actor"] != agent_id or row["event_type"] != "tool.called":
                continue
            payload = _event_payload(row)
            if payload.get("session_id") == session_id and payload.get("tool") == "poll_messages":
                poll_calls.append(row)
        if len(poll_calls) != 1 or poll_calls[0]["event_id"] != poll_called["event_id"]:
            raise ReceiptError("denied poll unexpectedly reached the bridge dispatcher")

        message_id = str(facts["send_result"].get("message_id") or "")
        content_sha = str(facts["send_result"].get("content_sha256") or "")
        message_events = []
        for row in prefix:
            if not (
                int(send_called["sequence"])
                < int(row["sequence"])
                < int(send_returned["sequence"])
                and row["actor"] == agent_id
                and row["event_type"] == "message.sent"
            ):
                continue
            payload = _event_payload(row)
            if (
                payload.get("session_id") == session_id
                and payload.get("message_id") == message_id
                and payload.get("content_sha256") == content_sha
            ):
                message_events.append(row)
        if len(message_events) != 1:
            raise ReceiptError("send event does not uniquely bind the durable message")
        message_row = connection.execute(
            "SELECT * FROM messages WHERE scope=? AND message_id=?",
            (scope, message_id),
        ).fetchone()
        if message_row is None:
            raise ReceiptError("bound message is absent from SQLite")
        final_presence = connection.execute(
            "SELECT 1 FROM agent_presence WHERE scope=? AND agent_id=? AND session_id=?",
            (scope, agent_id, session_id),
        ).fetchall()
    if final_presence:
        raise ReceiptError("recovered child presence was not cleaned up")

    message = _message_snapshot(message_row)
    recovered = facts["recovered_message"]
    if (
        message["message_id"] != recovered.get("message_id")
        or message["content_sha256"] != recovered.get("content_sha256")
        or message["body_sha256"]
        != sha256_bytes(str(recovered.get("body") or "").encode("utf-8"))
        or message["sender"] != agent_id
        or message["recipient"] != agent_id
    ):
        raise ReceiptError("recovered MCP result drifted from the bound SQLite message")
    database = {
        "path": str(db_path),
        "open_mode": "ro+immutable+quiescent",
        "scope": scope,
        "agent_id": agent_id,
        "session_id": session_id,
        "chain_prefix_event_count": len(prefix),
        "chain_prefix_through_sequence": int(poll_returned["sequence"]),
        "chain_prefix_head_sha256": poll_returned["chain_sha256"],
        "send_call_event": _event_snapshot(send_called),
        "message_event": _event_snapshot(message_events[0]),
        "send_return_event": _event_snapshot(send_returned),
        "poll_call_event": _event_snapshot(poll_called),
        "poll_return_event": _event_snapshot(poll_returned),
        "denied_tool_dispatch_count": 0,
        "final_bound_presence_count": 0,
    }
    return database, message


def _bound_sections(
    *,
    db_path: Path,
    evidence_path: Path,
    scope: str,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    evidence_value, evidence_raw = _read_object(evidence_path, evidence=True)
    facts = _parse_evidence(
        evidence_value,
        db_path=db_path,
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
    )
    database, message = _database_evidence(
        db_path=db_path,
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        facts=facts,
    )
    return {
        "evidence": {
            "schema": EVIDENCE_SCHEMA,
            "path": str(evidence_path),
            "size_bytes": len(evidence_raw),
            "sha256": hashlib.sha256(evidence_raw).hexdigest(),
        },
        "authorization": facts["authorization"],
        "lifecycle": facts["lifecycle"],
        "launches": facts["launches"],
        "mcp": facts["mcp"],
        "presence": facts["presence"],
        "message": message,
        "database": database,
        "security": facts["security"],
        "verification_contract": {
            "append_only_unrelated_database_progress_permitted": True,
            "bound_evidence_drift_permitted": False,
            "verify_only_writes": 0,
        },
    }


def _write_create_only(path: Path, receipt: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        with path.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError as exc:
        raise ReceiptError(f"refusing to overwrite existing receipt: {path}") from exc


def capture_receipt(
    *,
    db_path: Path,
    evidence_path: Path,
    scope: str,
    agent_id: str,
    session_id: str,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Validate lifecycle evidence and optionally create one immutable JSON receipt."""
    db_path = Path(db_path).resolve()
    evidence_path = Path(evidence_path).resolve()
    output_path = Path(output_path).resolve() if output_path is not None else None
    scope = _require_id(scope, "scope")
    agent_id = _require_id(agent_id, "agent_id")
    session_id = _require_id(session_id, "session_id")
    if output_path is not None:
        if output_path.exists():
            raise ReceiptError(f"refusing to overwrite existing receipt: {output_path}")
        if not output_path.parent.is_dir():
            raise ReceiptError(f"receipt parent directory is absent: {output_path.parent}")
        if output_path in {db_path, evidence_path}:
            raise ReceiptError("receipt output must be separate from its bound evidence")
    sections = _bound_sections(
        db_path=db_path,
        evidence_path=evidence_path,
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
    )
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "created_utc": utc_now(),
        **sections,
    }
    receipt["receipt_sha256"] = stable_sha256(receipt)
    if output_path is not None:
        _write_create_only(output_path, receipt)
    return receipt


capture_lifecycle_receipt = capture_receipt


def write_lifecycle_receipt(
    *,
    db_path: Path,
    evidence_path: Path,
    scope: str,
    agent_id: str,
    session_id: str,
    output_path: Path,
) -> dict[str, Any]:
    return capture_receipt(
        db_path=db_path,
        evidence_path=evidence_path,
        scope=scope,
        agent_id=agent_id,
        session_id=session_id,
        output_path=output_path,
    )


def _verification_result(expected_sha: str, errors: list[str]) -> dict[str, Any]:
    return {
        "valid": not errors,
        "receipt_sha256": expected_sha,
        "errors": errors,
        "writes_performed": 0,
    }


def verify_receipt(receipt_path: Path) -> dict[str, Any]:
    """Verify a receipt and its bound prefix without mutating any evidence."""
    errors: list[str] = []
    expected_sha = ""
    try:
        receipt, _ = _read_object(Path(receipt_path).resolve())
    except Exception as exc:
        return _verification_result(
            expected_sha, [f"receipt:{type(exc).__name__}:{exc}"]
        )
    try:
        expected_sha = str(receipt.get("receipt_sha256") or "")
        unsigned = dict(receipt)
        unsigned.pop("receipt_sha256", None)
        if receipt.get("schema") != RECEIPT_SCHEMA:
            errors.append("schema")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
            or stable_sha256(unsigned) != expected_sha
        ):
            errors.append("receipt_sha256")
    except Exception as exc:
        errors.append(f"receipt:{type(exc).__name__}:{exc}")
    # Do not follow paths supplied by a receipt that failed its own integrity gate.
    if errors:
        return _verification_result(expected_sha, errors)
    try:
        database = receipt["database"]
        evidence = receipt["evidence"]
        rebuilt = _bound_sections(
            db_path=Path(database["path"]).resolve(),
            evidence_path=Path(evidence["path"]).resolve(),
            scope=str(database["scope"]),
            agent_id=str(database["agent_id"]),
            session_id=str(database["session_id"]),
        )
        for key, value in rebuilt.items():
            if receipt.get(key) != value:
                errors.append(key)
    except Exception as exc:
        errors.append(f"evidence:{type(exc).__name__}:{exc}")
    return _verification_result(expected_sha, errors)


verify_lifecycle_receipt = verify_receipt


def capture_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture one MCP lifecycle receipt.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = capture_receipt(
        db_path=args.db,
        evidence_path=args.evidence,
        scope=args.scope,
        agent_id=args.agent_id,
        session_id=args.session_id,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "status": "CAPTURED",
                "path": str(args.output.resolve()),
                "receipt_sha256": receipt["receipt_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


def verify_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an MCP lifecycle receipt without writes.")
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args(argv)
    result = verify_receipt(args.receipt)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "verify":
        return verify_main(arguments[1:])
    if arguments and arguments[0] == "capture":
        arguments = arguments[1:]
    return capture_main(arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ReceiptError, sqlite3.Error) as exc:
        print(f"peerbridge lifecycle receipt: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
