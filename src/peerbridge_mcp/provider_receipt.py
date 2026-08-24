"""SHA-bound receipts for real provider-to-PeerBridge MCP invocations."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

from .agent_identity import AgentIdentityError, verify_agent_identity_launch_args
from .bridge import ZERO_SHA256, sha256_bytes, stable_sha256, utc_now
from .child_environment import build_local_child_environment
from .secret_scan import contains_secret


RECEIPT_SCHEMA = "peerbridge.provider-identity-receipt.v2"


class ReceiptError(RuntimeError):
    """A receipt could not be captured or verified without ambiguity."""


def _current_process_image() -> Path | None:
    """Return the real Windows image behind an App Execution Alias.

    Microsoft Store Python can expose ``sys.executable`` as a reparse-point
    alias that is executable but cannot be opened for hashing.  The loaded
    process image remains readable and is the binary identity receipts need.
    """
    if os.name != "nt":
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetModuleFileNameW(  # type: ignore[attr-defined]
        None, buffer, len(buffer)
    )
    if not length or length >= len(buffer):
        return None
    return Path(buffer.value)


def _hashable_file_path(path: Path) -> Path:
    try:
        with path.open("rb") as source:
            source.read(0)
        return path
    except OSError as original:
        if os.name != "nt":
            raise
        requested = os.path.normcase(os.path.abspath(str(path)))
        interpreter_aliases = {
            os.path.normcase(os.path.abspath(str(candidate)))
            for candidate in (
                getattr(sys, "executable", ""),
                getattr(sys, "_base_executable", ""),
            )
            if candidate
        }
        if requested not in interpreter_aliases:
            raise
        process_image = _current_process_image()
        if process_image is None:
            raise original
        try:
            with process_image.open("rb") as source:
                source.read(0)
        except OSError:
            raise original from None
        return process_image


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _hashable_file_path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReceiptError(f"expected a JSON object: {path}")
    return value


def _read_stream(path: Path, line_limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line_limit is not None and line_number > line_limit:
            break
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ReceiptError(f"stream line {line_number} is not an object")
        value["_receipt_line_number"] = line_number
        rows.append(value)
    return rows


def _stream_prefix_sha256(path: Path, line_count: int) -> str:
    lines = path.read_bytes().splitlines(keepends=True)
    if len(lines) < line_count:
        raise ReceiptError(
            f"stream has {len(lines)} lines; receipt requires prefix of {line_count}"
        )
    return sha256_bytes(b"".join(lines[:line_count]))


def _session_identity(session: dict[str, Any]) -> dict[str, Any]:
    """Select fields that identify a session but do not change as it continues."""

    command = str(session.get("agent_command") or "").strip()
    argv = _safe_command_args(session.get("agent_argv") or (), "Agent argv")
    if not command or len(command) > 240 or contains_secret(command):
        raise ReceiptError("Agent command identity is invalid or sensitive")
    return {
        "schema": session.get("schema"),
        "acpx_record_id": session.get("acpx_record_id"),
        "acp_session_id": session.get("acp_session_id"),
        "agent_session_id": session.get("agent_session_id"),
        "agent_command": command,
        "agent_argv_sha256": stable_sha256(argv),
        "cwd_sha256": stable_sha256(str(session.get("cwd") or "")),
        "name": session.get("name"),
        "created_at": session.get("created_at"),
        "protocol_version": session.get("protocol_version"),
        "current_model_id": (session.get("acpx") or {}).get("current_model_id"),
    }


def _provider_version(binary: Path, version_args: Iterable[str]) -> str:
    completed = subprocess.run(
        [str(binary), *version_args],
        text=True,
        capture_output=True,
        env=build_local_child_environment(),
        timeout=30,
        check=False,
    )
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0 or not output:
        raise ReceiptError(
            f"provider version command failed ({completed.returncode}): {binary}"
        )
    version = output.splitlines()[0].strip()
    if (
        not version
        or len(version) > 240
        or any(character in version for character in "\x00\r\n")
        or contains_secret(version)
    ):
        raise ReceiptError("provider version output is invalid or sensitive")
    return version


def _safe_command_args(values: Iterable[str], label: str) -> tuple[str, ...]:
    args = tuple(str(value) for value in values)
    for value in args:
        if not value or len(value) > 4096 or any(char in value for char in "\x00\r\n"):
            raise ReceiptError(f"{label} contains an invalid argument")
        if contains_secret(value):
            raise ReceiptError(f"{label} appears to contain a credential")
    return args


def _argument_file_evidence(values: Iterable[str]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        candidate = Path(str(value)).expanduser().resolve()
        if candidate.is_file():
            evidence.append(
                {
                    "argument_index": index,
                    "path": str(candidate),
                    "sha256": _file_sha256(candidate),
                }
            )
    return evidence


def _verify_argument_file_evidence(
    values: Iterable[str], expected: Any, label: str
) -> None:
    if not isinstance(expected, list):
        raise ReceiptError(f"{label} file evidence is malformed")
    if _argument_file_evidence(values) != expected:
        raise ReceiptError(f"{label} argument file evidence drifted")


def _write_json_create_only(path: Path, value: dict[str, Any]) -> None:
    """Create a JSON evidence file without any overwrite race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            descriptor = -1
            target.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _route_from_args(args: list[str]) -> dict[str, str | None]:
    def option(name: str) -> str | None:
        positions = [index for index, value in enumerate(args) if value == name]
        if not positions:
            return None
        if len(positions) != 1:
            raise ReceiptError(f"MCP server option appears more than once: {name}")
        index = positions[0]
        if index + 1 >= len(args) or str(args[index + 1]).startswith("--"):
            raise ReceiptError(f"MCP server option lacks a value: {name}")
        return str(args[index + 1])

    return {
        "agent_id": option("--agent-id"),
        "scope": option("--scope"),
        "client_name": option("--client-name"),
        "provider_id": option("--provider-id"),
        "model_id": option("--model-id"),
        "reasoning_mode": option("--reasoning-mode"),
        "route_class": option("--route-class"),
    }


def _mcp_config_evidence(
    path: Path,
    *,
    db_path: Path,
    agent_id: str,
    scope: str,
    runtime_identity: dict[str, Any],
) -> dict[str, Any]:
    document = _read_json(path)
    servers = document.get("mcpServers")
    if not isinstance(servers, list):
        raise ReceiptError("MCP config requires an mcpServers array")
    expected_route = {"agent_id": agent_id, "scope": scope, **runtime_identity}
    include_route_class = "route_class" in runtime_identity
    matches: list[dict[str, Any]] = []
    for server in servers:
        if not isinstance(server, dict):
            raise ReceiptError("MCP config server entry must be an object")
        if any(key in server for key in ("env", "environment")):
            raise ReceiptError("provider receipt MCP config must not contain environment secrets")
        command = server.get("command")
        args = server.get("args")
        if not isinstance(command, str) or not isinstance(args, list) or not all(
            isinstance(value, str) for value in args
        ):
            raise ReceiptError("MCP config server command/args are invalid")
        try:
            sanitized_args, identity_capability = verify_agent_identity_launch_args(
                args,
                db_path=db_path,
                scope=scope,
                claimed_agent_id=agent_id,
            )
        except AgentIdentityError as exc:
            raise ReceiptError("MCP config Agent identity binding is invalid") from exc
        route = _route_from_args(args)
        if not include_route_class:
            route.pop("route_class", None)
        if route == expected_route:
            matches.append(
                {
                    "name": str(server.get("name") or ""),
                    "command": command,
                    "args": sanitized_args,
                    "route": route,
                    "identity_capability": identity_capability,
                }
            )
    if len(matches) != 1:
        raise ReceiptError(
            f"expected one MCP server route matching provider runtime, found {len(matches)}"
        )
    return {
        "path": str(path.resolve()),
        "sha256": _file_sha256(path),
        "sanitized_server": matches[0],
        "sanitized_server_sha256": stable_sha256(matches[0]),
    }


@contextlib.contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _event_envelope(row: sqlite3.Row, payload_sha256: str, previous: str) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "scope": row["scope"],
        "actor": row["actor"],
        "event_type": row["event_type"],
        "task_id": row["task_id"],
        "payload_sha256": payload_sha256,
        "created_utc": row["created_utc"],
        "prev_chain_sha256": previous,
    }


def _verify_chain_prefix(
    connection: sqlite3.Connection, scope: str, through_sequence: int
) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM events WHERE scope=? AND sequence<=? ORDER BY sequence ASC",
        (scope, through_sequence),
    ).fetchall()
    if not rows:
        raise ReceiptError("audit chain prefix is empty")
    previous = ZERO_SHA256
    for row in rows:
        payload_sha = sha256_bytes(row["payload_json"].encode("utf-8"))
        chain_sha = stable_sha256(_event_envelope(row, payload_sha, previous))
        if row["payload_sha256"] != payload_sha:
            raise ReceiptError(f"event {row['sequence']} payload SHA mismatch")
        if row["prev_chain_sha256"] != previous:
            raise ReceiptError(f"event {row['sequence']} previous-chain SHA mismatch")
        if row["chain_sha256"] != chain_sha:
            raise ReceiptError(f"event {row['sequence']} chain SHA mismatch")
        previous = row["chain_sha256"]
    return rows


def _event_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sequence": row["sequence"],
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "actor": row["actor"],
        "task_id": row["task_id"],
        "created_utc": row["created_utc"],
        "payload_sha256": row["payload_sha256"],
        "prev_chain_sha256": row["prev_chain_sha256"],
        "chain_sha256": row["chain_sha256"],
    }


def _matching_event_pair(
    connection: sqlite3.Connection,
    *,
    scope: str,
    agent_id: str,
    tool: str,
    runtime_identity: dict[str, Any],
    result_sha256: str,
) -> tuple[sqlite3.Row, sqlite3.Row, dict[str, Any], dict[str, Any]]:
    def identity_matches(observed: Any) -> bool:
        if not isinstance(observed, dict):
            return False
        if "route_class" in runtime_identity:
            return observed == runtime_identity
        return (
            {key: observed.get(key) for key in runtime_identity} == runtime_identity
            and set(observed) <= set(runtime_identity) | {"route_class"}
            and observed.get("route_class") is None
        )

    rows = connection.execute(
        "SELECT * FROM events WHERE scope=? ORDER BY sequence ASC",
        (scope,),
    ).fetchall()
    matches: list[tuple[sqlite3.Row, sqlite3.Row, dict[str, Any], dict[str, Any]]] = []
    for index, called in enumerate(rows):
        if called["event_type"] != "tool.called" or called["actor"] != agent_id:
            continue
        called_payload = json.loads(called["payload_json"])
        if (
            called_payload.get("tool") != tool
            or not identity_matches(called_payload.get("runtime_identity"))
        ):
            continue
        for returned in rows[index + 1 :]:
            if returned["actor"] != agent_id:
                continue
            returned_payload = json.loads(returned["payload_json"])
            same_invocation = (
                returned_payload.get("tool") == tool
                and returned_payload.get("session_id")
                == called_payload.get("session_id")
            )
            if returned["event_type"] == "tool.called" and same_invocation:
                break
            if (
                returned["event_type"] == "tool.returned"
                and same_invocation
                and identity_matches(returned_payload.get("runtime_identity"))
                and returned_payload.get("result_sha256") == result_sha256
            ):
                matches.append((called, returned, called_payload, returned_payload))
                break
    if len(matches) != 1:
        raise ReceiptError(
            f"expected one exact provider-to-bridge event pair, found {len(matches)}"
        )
    return matches[0]


def _stream_evidence(
    rows: list[dict[str, Any]], *, tool: str, scope: str, agent_id: str
) -> dict[str, Any]:
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
    completions: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    permission_requests: dict[str, Any] = {}
    allowed_permission_ids: set[Any] = set()
    assistant_chunks: list[tuple[int, str]] = []
    for row in rows:
        params = row.get("params") or {}
        update = params.get("update") or {}
        line_number = int(row["_receipt_line_number"])
        if update.get("sessionUpdate") == "agent_message_chunk":
            text = (update.get("content") or {}).get("text")
            if isinstance(text, str):
                assistant_chunks.append((line_number, text))
        if update.get("sessionUpdate") == "tool_call":
            raw_input = update.get("rawInput") or {}
            tool_name = str(raw_input.get("tool_name") or "")
            if tool_name == tool or tool_name.endswith(f"__{tool}"):
                calls.append((row, update))
        if update.get("sessionUpdate") == "tool_call_update" and update.get("status") == "completed":
            completions[str(update.get("toolCallId"))] = (row, update)
        if row.get("method") == "session/request_permission":
            call = params.get("toolCall") or {}
            permission_requests[str(call.get("toolCallId"))] = row.get("id")
        if row.get("id") is not None:
            result = row.get("result") or {}
            outcome = result.get("outcome") or {}
            if outcome.get("outcome") == "selected" and outcome.get("optionId") in {
                "allow-once",
                "allow_always",
            }:
                allowed_permission_ids.add(row["id"])
    if len(calls) != 1:
        raise ReceiptError(f"expected exactly one ACP tool call, found {len(calls)}")
    call_row, call_update = calls[0]
    call_id = str(call_update.get("toolCallId"))
    completed = completions.get(call_id)
    if not completed:
        raise ReceiptError("matching completed ACP tool result is absent")
    result_row, result_update = completed
    raw_output = result_update.get("rawOutput") or {}
    okay_output = ((raw_output.get("output") or {}).get("OkayOutput"))
    if not isinstance(okay_output, str):
        raise ReceiptError("completed ACP MCP result lacks OkayOutput JSON")
    tool_result = json.loads(okay_output)
    if "scope" in tool_result and tool_result.get("scope") != scope:
        raise ReceiptError("ACP tool result scope identity mismatch")
    result_agent_id = tool_result.get("agent_id")
    if result_agent_id is not None and result_agent_id != agent_id:
        raise ReceiptError("ACP tool result agent identity mismatch")
    final_text = "".join(
        text for line, text in assistant_chunks if line > int(result_row["_receipt_line_number"])
    )
    if not final_text.strip():
        raise ReceiptError("final model response after the completed MCP tool call is absent")
    permission_allowed = permission_requests.get(call_id) in allowed_permission_ids
    if not permission_allowed:
        raise ReceiptError("ACP permission request/allow evidence is incomplete")
    return {
        "tool_call_id": call_id,
        "tool_call_line": call_row["_receipt_line_number"],
        "tool_result_line": result_row["_receipt_line_number"],
        "tool_result_sha256": stable_sha256(tool_result),
        "permission_request_observed": True,
        "permission_allow_observed": True,
        "final_response_sha256": sha256_bytes(final_text.encode("utf-8")),
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
    route_class: str | None = None,
    tool: str,
    session_path: Path,
    stream_path: Path,
    provider_binary: Path,
    provider_version_args: tuple[str, ...] = ("--version",),
    acpx_cli_path: Path | None = None,
    mcp_config_path: Path | None = None,
) -> dict[str, Any]:
    provider_version_args = _safe_command_args(
        provider_version_args, "provider version arguments"
    )
    if mcp_config_path is None:
        raise ReceiptError("provider receipt requires a capability-bound MCP config")
    paths = [db_path, session_path, stream_path, provider_binary, mcp_config_path]
    for path in paths:
        if not path.is_file():
            raise ReceiptError(f"required evidence file is absent: {path}")
    runtime_identity = {
        "client_name": client_name,
        "provider_id": provider_id,
        "model_id": model_id,
        "reasoning_mode": reasoning_mode,
    }
    if route_class is not None:
        runtime_identity["route_class"] = route_class
    session = _read_json(session_path)
    stream_rows = _read_stream(stream_path)
    stream_line_count = len(stream_rows)
    stream_evidence = _stream_evidence(
        stream_rows, tool=tool, scope=scope, agent_id=agent_id
    )
    with _readonly_connection(db_path) as connection:
        called, returned, called_payload, returned_payload = _matching_event_pair(
            connection,
            scope=scope,
            agent_id=agent_id,
            tool=tool,
            runtime_identity=runtime_identity,
            result_sha256=stream_evidence["tool_result_sha256"],
        )
        chain_rows = _verify_chain_prefix(connection, scope, int(returned["sequence"]))
        event_window = [
            _event_snapshot(row)
            for row in chain_rows
            if int(called["sequence"]) <= int(row["sequence"]) <= int(returned["sequence"])
        ]
    current_model = (session.get("acpx") or {}).get("current_model_id")
    if current_model != model_id:
        raise ReceiptError("ACPX current model does not match the expected model")
    acp_session_id = str(session.get("acp_session_id") or "")
    if not acp_session_id:
        raise ReceiptError("ACPX session metadata lacks acp_session_id")
    if stream_evidence["tool_result_sha256"] != returned_payload.get("result_sha256"):
        raise ReceiptError("ACPX tool output does not match PeerBridge tool.returned SHA")
    provider_identity_path = _hashable_file_path(provider_binary)
    artifacts: dict[str, Any] = {}
    for name, path in (("acpx_cli", acpx_cli_path),):
        if path is not None:
            if not path.is_file():
                raise ReceiptError(f"optional evidence file is absent: {path}")
            artifacts[name] = {"path": str(path.resolve()), "sha256": _file_sha256(path)}
    artifacts["mcp_config"] = _mcp_config_evidence(
        mcp_config_path,
        db_path=db_path,
        agent_id=agent_id,
        scope=scope,
        runtime_identity=runtime_identity,
    )
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "created_utc": utc_now(),
        "provider": {
            "binary_path": str(provider_identity_path.resolve()),
            "binary_sha256": _file_sha256(provider_identity_path),
            "version_args": list(provider_version_args),
            "version_argument_files": _argument_file_evidence(provider_version_args),
            "version_output": _provider_version(
                provider_identity_path, provider_version_args
            ),
        },
        "acpx": {
            "record_id": session.get("acpx_record_id"),
            "acp_session_id": acp_session_id,
            "agent_command_sha256": stable_sha256(
                str(session.get("agent_command") or "")
            ),
            "current_model_id": current_model,
            "session_path": str(session_path.resolve()),
            "session_identity": _session_identity(session),
            "session_identity_sha256": stable_sha256(_session_identity(session)),
            "stream_path": str(stream_path.resolve()),
            "stream_prefix_sha256": _stream_prefix_sha256(stream_path, stream_line_count),
            "stream_prefix_line_count": stream_line_count,
            **stream_evidence,
        },
        "bridge": {
            "database_path": str(db_path.resolve()),
            "scope": scope,
            "agent_id": agent_id,
            "tool": tool,
            "runtime_session_id": called_payload.get("session_id"),
            "runtime_identity": runtime_identity,
            "arguments_sha256": called_payload.get("arguments_sha256"),
            "result_sha256": returned_payload.get("result_sha256"),
            "chain_prefix_event_count": len(chain_rows),
            "chain_prefix_head_sha256": returned["chain_sha256"],
            "call_event": _event_snapshot(called),
            "return_event": _event_snapshot(returned),
            "event_window": event_window,
        },
        "artifacts": artifacts,
        "credential_contents_recorded": False,
    }
    receipt["receipt_sha256"] = stable_sha256(receipt)
    return receipt


def verify_receipt(receipt_path: Path) -> dict[str, Any]:
    receipt = _read_json(receipt_path)
    errors: list[str] = []
    if receipt.get("schema") != RECEIPT_SCHEMA:
        errors.append("schema")
    expected_receipt_sha = str(receipt.get("receipt_sha256") or "")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if stable_sha256(unsigned) != expected_receipt_sha:
        errors.append("receipt_sha256")
    if errors:
        return {
            "valid": False,
            "receipt_sha256": expected_receipt_sha,
            "errors": errors,
            "writes_performed": 0,
            "processes_started": 0,
        }
    provider = receipt.get("provider") or {}
    acpx = receipt.get("acpx") or {}
    bridge = receipt.get("bridge") or {}
    artifacts = receipt.get("artifacts") or {}
    try:
        binary = Path(provider["binary_path"])
        if _file_sha256(binary) != provider.get("binary_sha256"):
            errors.append("provider_binary_sha256")
        version_args = _safe_command_args(
            provider.get("version_args") or ("--version",),
            "provider version arguments",
        )
        if not isinstance(provider.get("version_output"), str) or not provider[
            "version_output"
        ].strip():
            errors.append("provider_version_output")
        _verify_argument_file_evidence(
            version_args,
            provider.get("version_argument_files", []),
            "provider version arguments",
        )
    except (KeyError, OSError, ReceiptError) as exc:
        errors.append(f"provider:{exc}")
    try:
        session = _read_json(Path(acpx["session_path"]))
        identity = _session_identity(session)
        if identity != acpx.get("session_identity"):
            errors.append("acpx_session_identity")
        if stable_sha256(identity) != acpx.get("session_identity_sha256"):
            errors.append("acpx_session_identity_sha256")
    except (KeyError, OSError, ReceiptError) as exc:
        errors.append(f"acpx_session:{exc}")
    try:
        prefix_count = int(acpx["stream_prefix_line_count"])
        if _stream_prefix_sha256(Path(acpx["stream_path"]), prefix_count) != acpx.get(
            "stream_prefix_sha256"
        ):
            errors.append("acpx_stream_prefix_sha256")
    except (KeyError, OSError, ReceiptError, ValueError) as exc:
        errors.append(f"acpx_stream:{exc}")
    for label, value in artifacts.items():
        try:
            if _file_sha256(Path(value["path"])) != value.get("sha256"):
                errors.append(f"artifact_{label}_sha256")
        except (KeyError, OSError) as exc:
            errors.append(f"artifact_{label}:{exc}")
    if "mcp_config" in artifacts and "sanitized_server" in artifacts["mcp_config"]:
        try:
            current_config = _mcp_config_evidence(
                Path(artifacts["mcp_config"]["path"]),
                db_path=Path(bridge["database_path"]),
                agent_id=bridge["agent_id"],
                scope=bridge["scope"],
                runtime_identity=bridge["runtime_identity"],
            )
            if current_config != artifacts["mcp_config"]:
                errors.append("artifact_mcp_config_route")
        except (KeyError, OSError, ReceiptError) as exc:
            errors.append(f"artifact_mcp_config_route:{exc}")
    try:
        db_path = Path(bridge["database_path"])
        with _readonly_connection(db_path) as connection:
            return_sequence = int((bridge["return_event"])["sequence"])
            rows = _verify_chain_prefix(connection, bridge["scope"], return_sequence)
            snapshots = {int(row["sequence"]): _event_snapshot(row) for row in rows}
        for label in ("call_event", "return_event"):
            expected = bridge[label]
            if snapshots.get(int(expected["sequence"])) != expected:
                errors.append(label)
        if "event_window" in bridge:
            call_sequence = int(bridge["call_event"]["sequence"])
            expected_window = [
                snapshot
                for sequence, snapshot in sorted(snapshots.items())
                if call_sequence <= sequence <= return_sequence
            ]
            if bridge.get("event_window") != expected_window:
                errors.append("event_window")
        if rows[-1]["chain_sha256"] != bridge.get("chain_prefix_head_sha256"):
            errors.append("chain_prefix_head_sha256")
        if len(rows) != bridge.get("chain_prefix_event_count"):
            errors.append("chain_prefix_event_count")
        session = _read_json(Path(acpx["session_path"]))
        stream_rows = _read_stream(
            Path(acpx["stream_path"]), int(acpx["stream_prefix_line_count"])
        )
        stream = _stream_evidence(
            stream_rows,
            tool=bridge["tool"],
            scope=bridge["scope"],
            agent_id=bridge["agent_id"],
        )
        for key in (
            "tool_call_id",
            "tool_call_line",
            "tool_result_line",
            "tool_result_sha256",
            "permission_request_observed",
            "permission_allow_observed",
            "final_response_sha256",
        ):
            if stream.get(key) != acpx.get(key):
                errors.append(f"acpx_{key}")
        if (session.get("acpx") or {}).get("current_model_id") != bridge[
            "runtime_identity"
        ].get("model_id"):
            errors.append("acpx_model_identity")
        if stream["tool_result_sha256"] != bridge.get("result_sha256"):
            errors.append("bridge_result_sha256")
    except (KeyError, OSError, ReceiptError, sqlite3.Error, ValueError) as exc:
        errors.append(f"evidence:{exc}")
    return {
        "valid": not errors,
        "receipt_sha256": expected_receipt_sha,
        "errors": errors,
        "writes_performed": 0,
        "processes_started": 0,
    }


def capture_main() -> int:
    parser = argparse.ArgumentParser(description="Capture one provider identity receipt.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--client-name", required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--reasoning-mode")
    parser.add_argument("--route-class", choices=("official", "relay", "local"))
    parser.add_argument("--tool", required=True)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--stream", type=Path, required=True)
    parser.add_argument("--provider-binary", type=Path, required=True)
    parser.add_argument("--provider-version-arg", action="append", default=[])
    parser.add_argument("--acpx-cli", type=Path)
    parser.add_argument("--mcp-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
        session_path=args.session.resolve(),
        stream_path=args.stream.resolve(),
        provider_binary=args.provider_binary.resolve(),
        provider_version_args=tuple(args.provider_version_arg or ["--version"]),
        acpx_cli_path=args.acpx_cli.resolve() if args.acpx_cli else None,
        mcp_config_path=args.mcp_config.resolve(),
    )
    try:
        _write_json_create_only(args.output, receipt)
    except FileExistsError:
        parser.error(f"refusing to overwrite existing receipt: {args.output}")
    print(json.dumps({"status": "CAPTURED", **verify_receipt(args.output)}, sort_keys=True))
    return 0


def verify_main() -> int:
    parser = argparse.ArgumentParser(description="Verify a provider receipt without writes.")
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    result = verify_receipt(args.receipt.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(capture_main())
