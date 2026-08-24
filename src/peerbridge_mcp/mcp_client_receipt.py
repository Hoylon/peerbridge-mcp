"""SHA-bound receipts for a completed MCP client tool invocation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .agent_identity import (
    AgentIdentityError,
    verify_agent_identity_launch_args,
    verify_redacted_agent_identity_launch_args,
)
from .bridge import stable_sha256, utc_now
from .secret_scan import contains_secret
from .provider_receipt import (
    ReceiptError,
    _argument_file_evidence,
    _event_snapshot,
    _file_sha256,
    _hashable_file_path,
    _provider_version,
    _readonly_connection,
    _route_from_args,
    _safe_command_args,
    _verify_chain_prefix,
    _verify_argument_file_evidence,
    _write_json_create_only,
)


RECEIPT_SCHEMA = "peerbridge.mcp-client-receipt.v2"
LIFECYCLE_SCHEMA = "peerbridge.test-child-lifecycle.v1"


def _read_jsonl_text(path: Path) -> tuple[str, str, bytes]:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16", raw
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig", raw
    return raw.decode("utf-8"), "utf-8", raw


def _raw_prefix(raw: bytes, encoding: str, line_count: int) -> bytes:
    if line_count < 1:
        raise ReceiptError("transcript prefix line count must be positive")
    if encoding == "utf-16":
        delimiter = b"\n\x00" if raw.startswith(b"\xff\xfe") else b"\x00\n"
        offset = 0
        for _ in range(line_count):
            index = raw.find(delimiter, offset)
            if index < 0:
                if _ == line_count - 1 and offset < len(raw):
                    return raw
                raise ReceiptError("transcript has fewer lines than the bound prefix")
            offset = index + len(delimiter)
        return raw[:offset]
    lines = raw.splitlines(keepends=True)
    if len(lines) < line_count:
        raise ReceiptError("transcript has fewer lines than the bound prefix")
    return b"".join(lines[:line_count])


def _lifecycle_file_record(
    value: Any, *, label: str, allow_append: bool = False
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ReceiptError(f"MCP client lifecycle lacks {label} evidence")
    try:
        path = Path(value["path"]).resolve()
        expected_bytes = int(value["bytes"])
        expected_sha = str(value["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReceiptError(f"MCP client lifecycle {label} evidence is malformed") from exc
    if not path.is_file():
        raise ReceiptError(f"MCP client lifecycle {label} file is absent")
    current_size = path.stat().st_size
    if current_size < expected_bytes:
        raise ReceiptError(f"MCP client lifecycle {label} evidence drifted")
    if allow_append:
        with path.open("rb") as source:
            observed_sha = hashlib.sha256(source.read(expected_bytes)).hexdigest()
    else:
        if current_size != expected_bytes:
            raise ReceiptError(f"MCP client lifecycle {label} evidence drifted")
        observed_sha = _file_sha256(path)
    if observed_sha != expected_sha:
        raise ReceiptError(f"MCP client lifecycle {label} evidence drifted")
    return path, {
        "path": str(path),
        "size_bytes": expected_bytes,
        "sha256": expected_sha,
    }


def _lifecycle_snapshot(path: Path, *, transcript_path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if contains_secret(raw.decode("utf-8", errors="strict")):
        raise ReceiptError("MCP client lifecycle appears to contain a credential")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReceiptError("MCP client lifecycle is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != LIFECYCLE_SCHEMA:
        raise ReceiptError("unsupported MCP client lifecycle schema")
    if (
        value.get("completed_turn_observed") is not True
        or value.get("exit_code") != 0
        or value.get("timed_out") is not False
        or value.get("credential_values_read") is not False
        or value.get("credential_values_recorded") is not False
        or value.get("database_exists") is not True
    ):
        raise ReceiptError("MCP client lifecycle did not prove a successful credential-safe run")
    stdout_path, stdout = _lifecycle_file_record(
        value.get("stdout"), label="stdout", allow_append=True
    )
    _, stderr = _lifecycle_file_record(value.get("stderr"), label="stderr")
    if stdout_path != transcript_path.resolve():
        raise ReceiptError("MCP client lifecycle is bound to a different transcript")
    return {
        "path": str(path.resolve()),
        "size_bytes": len(raw),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "schema": LIFECYCLE_SCHEMA,
        "step": value.get("step"),
        "started_utc": value.get("started_utc"),
        "finished_utc": value.get("finished_utc"),
        "elapsed_seconds": value.get("elapsed_seconds"),
        "completed_turn_observed": True,
        "exit_code": 0,
        "timed_out": False,
        "stdout": stdout,
        "stderr": stderr,
        "database_exists": True,
        "credential_values_read": False,
        "credential_values_recorded": False,
    }


def _transcript_evidence(
    path: Path,
    *,
    server_name: str,
    tool: str,
    scope: str,
    agent_id: str,
    prefix_line_count: int | None = None,
) -> dict[str, Any]:
    text, encoding, raw = _read_jsonl_text(path)
    all_lines = text.splitlines()
    if prefix_line_count is None:
        prefix_line_count = len(all_lines)
    if len(all_lines) < prefix_line_count:
        raise ReceiptError("transcript has fewer lines than the bound prefix")
    text = "\n".join(all_lines[:prefix_line_count])
    if contains_secret(text):
        raise ReceiptError("transcript prefix appears to contain a credential")
    raw_prefix = _raw_prefix(raw, encoding, prefix_line_count)
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append((line_number, value))

    thread_ids = {
        str(row.get("thread_id"))
        for _, row in rows
        if row.get("type") == "thread.started" and row.get("thread_id")
    }
    calls: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for line_number, row in rows:
        item = row.get("item") or {}
        if (
            row.get("type") != "item.completed"
            or item.get("type") != "mcp_tool_call"
            or item.get("server") != server_name
            or item.get("tool") != tool
        ):
            continue
        if item.get("status") != "completed" or item.get("error") is not None:
            raise ReceiptError("matching MCP tool call did not complete successfully")
        result = item.get("result") or {}
        decoded: list[dict[str, Any]] = []
        for block in result.get("content") or []:
            if block.get("type") != "text" or not isinstance(block.get("text"), str):
                continue
            try:
                candidate = json.loads(block["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                decoded.append(candidate)
        if len(decoded) != 1:
            raise ReceiptError("MCP tool result must contain exactly one JSON object")
        calls.append((line_number, item, decoded[0]))
    if len(calls) != 1:
        raise ReceiptError(f"expected exactly one matching MCP tool call, found {len(calls)}")

    call_line, item, tool_result = calls[0]
    if tool_result.get("scope") != scope or tool_result.get("agent_id") != agent_id:
        raise ReceiptError("MCP tool result scope/agent identity mismatch")
    session_id = str(tool_result.get("session_id") or "")
    if not session_id:
        raise ReceiptError("MCP tool result lacks a runtime session ID")
    arguments = item.get("arguments")
    if not isinstance(arguments, dict):
        raise ReceiptError("MCP tool call arguments are not an object")

    messages = [
        (line_number, (row.get("item") or {}).get("text"))
        for line_number, row in rows
        if row.get("type") == "item.completed"
        and (row.get("item") or {}).get("type") == "agent_message"
        and line_number > call_line
        and isinstance((row.get("item") or {}).get("text"), str)
    ]
    if not messages:
        raise ReceiptError("completed MCP call lacks a following client response")
    final_line, final_text = messages[-1]
    if scope not in final_text or agent_id not in final_text:
        raise ReceiptError("client response does not report returned scope and agent")
    turn_completions = [
        (line_number, row)
        for line_number, row in rows
        if row.get("type") == "turn.completed" and line_number > final_line
    ]
    if len(turn_completions) != 1:
        raise ReceiptError("client transcript requires exactly one completed turn")
    turn_line, turn_completed = turn_completions[0]
    return {
        "encoding": encoding,
        "prefix_bytes": len(raw_prefix),
        "prefix_line_count": prefix_line_count,
        "prefix_sha256": hashlib.sha256(raw_prefix).hexdigest(),
        "thread_ids": sorted(thread_ids),
        "server_name": server_name,
        "tool": tool,
        "tool_call_line": call_line,
        "tool_result_sha256": stable_sha256(tool_result),
        "arguments_sha256": stable_sha256(arguments),
        "runtime_session_id": session_id,
        "final_response_line": final_line,
        "final_response_sha256": stable_sha256(final_text),
        "turn_completed_line": turn_line,
        "turn_completed_sha256": stable_sha256(turn_completed),
    }


def _sanitized_mcp_config(
    value: Any,
    *,
    server_name: str,
    db_path: Path,
    scope: str,
    agent_id: str,
    runtime_identity: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("name") != server_name:
        raise ReceiptError("MCP config does not identify the expected server")
    transport = value.get("transport") or {}
    if transport.get("type") != "stdio":
        raise ReceiptError("client receipt currently requires a stdio MCP transport")
    if transport.get("env") or transport.get("env_vars"):
        raise ReceiptError("MCP config contains environment data and cannot be receipted")
    args = transport.get("args") or []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ReceiptError("MCP config stdio arguments are invalid")
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
    expected_route = {"agent_id": agent_id, "scope": scope, **runtime_identity}
    if route != expected_route:
        raise ReceiptError("MCP config route differs from the expected runtime identity")
    snapshot = {
        "name": value.get("name"),
        "enabled": value.get("enabled"),
        "disabled_reason": value.get("disabled_reason"),
        "transport": {
            "type": transport.get("type"),
            "command": transport.get("command"),
            "args": sanitized_args,
            "cwd": transport.get("cwd"),
        },
        "route": route,
        "identity_capability": identity_capability,
        "enabled_tools": value.get("enabled_tools"),
        "disabled_tools": value.get("disabled_tools"),
        "startup_timeout_sec": value.get("startup_timeout_sec"),
        "tool_timeout_sec": value.get("tool_timeout_sec"),
    }
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    if contains_secret(encoded):
        raise ReceiptError("MCP config appears to contain a credential")
    return snapshot


def _live_mcp_config(
    client_binary: Path,
    config_args: tuple[str, ...],
    *,
    server_name: str,
    db_path: Path,
    scope: str,
    agent_id: str,
    runtime_identity: dict[str, Any],
) -> dict[str, Any]:
    completed = subprocess.run(
        [str(client_binary), *config_args],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise ReceiptError(f"MCP config command failed ({completed.returncode})")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ReceiptError("MCP config command did not return JSON") from exc
    return _sanitized_mcp_config(
        value,
        server_name=server_name,
        db_path=db_path,
        scope=scope,
        agent_id=agent_id,
        runtime_identity=runtime_identity,
    )


def _matching_pair(
    connection: sqlite3.Connection,
    *,
    scope: str,
    agent_id: str,
    tool: str,
    runtime_identity: dict[str, Any],
    session_id: str,
    arguments_sha256: str,
    result_sha256: str,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM events WHERE scope=? AND actor=? ORDER BY sequence ASC",
        (scope, agent_id),
    ).fetchall()
    matches: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    for index, called in enumerate(rows):
        if called["event_type"] != "tool.called" or index + 1 >= len(rows):
            continue
        returned = rows[index + 1]
        if returned["event_type"] != "tool.returned":
            continue
        called_payload = json.loads(called["payload_json"])
        returned_payload = json.loads(returned["payload_json"])
        if (
            called_payload.get("tool") == tool
            and called_payload.get("session_id") == session_id
            and called_payload.get("runtime_identity") == runtime_identity
            and called_payload.get("arguments_sha256") == arguments_sha256
            and returned_payload.get("tool") == tool
            and returned_payload.get("session_id") == session_id
            and returned_payload.get("runtime_identity") == runtime_identity
            and returned_payload.get("result_sha256") == result_sha256
            and returned["prev_chain_sha256"] == called["chain_sha256"]
        ):
            matches.append((called, returned))
    if len(matches) != 1:
        raise ReceiptError(f"expected one exact adjacent bridge event pair, found {len(matches)}")
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
    client_binary: Path,
    client_version_args: tuple[str, ...],
    config_args: tuple[str, ...],
    lifecycle_path: Path | None = None,
) -> dict[str, Any]:
    client_version_args = _safe_command_args(
        client_version_args, "client version arguments"
    )
    config_args = _safe_command_args(config_args, "client config arguments")
    for path in (db_path, transcript_path, client_binary):
        if not path.is_file():
            raise ReceiptError(f"required evidence file is absent: {path}")
    transcript = _transcript_evidence(
        transcript_path,
        server_name=server_name,
        tool=tool,
        scope=scope,
        agent_id=agent_id,
    )
    runtime_identity = {
        "client_name": client_name,
        "provider_id": provider_id,
        "model_id": model_id,
        "reasoning_mode": reasoning_mode,
        "route_class": route_class,
    }
    with _readonly_connection(db_path) as connection:
        called, returned = _matching_pair(
            connection,
            scope=scope,
            agent_id=agent_id,
            tool=tool,
            runtime_identity=runtime_identity,
            session_id=transcript["runtime_session_id"],
            arguments_sha256=transcript["arguments_sha256"],
            result_sha256=transcript["tool_result_sha256"],
        )
        chain_rows = _verify_chain_prefix(connection, scope, int(returned["sequence"]))
    config = _live_mcp_config(
        client_binary,
        config_args,
        server_name=server_name,
        db_path=db_path,
        scope=scope,
        agent_id=agent_id,
        runtime_identity=runtime_identity,
    )
    lifecycle = (
        _lifecycle_snapshot(lifecycle_path, transcript_path=transcript_path)
        if lifecycle_path is not None
        else None
    )
    client_identity_path = _hashable_file_path(client_binary)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "created_utc": utc_now(),
        "client": {
            "binary_path": str(client_identity_path.resolve()),
            "binary_sha256": _file_sha256(client_identity_path),
            "version_args": list(client_version_args),
            "version_argument_files": _argument_file_evidence(client_version_args),
            "version_output": _provider_version(
                client_identity_path, client_version_args
            ),
            "config_args": list(config_args),
            "config_argument_files": _argument_file_evidence(config_args),
            "sanitized_mcp_config": config,
            "sanitized_mcp_config_sha256": stable_sha256(config),
        },
        "transcript": {
            "path": str(transcript_path.resolve()),
            **transcript,
        },
        "bridge": {
            "database_path": str(db_path.resolve()),
            "scope": scope,
            "agent_id": agent_id,
            "runtime_identity": runtime_identity,
            "call_event": _event_snapshot(called),
            "return_event": _event_snapshot(returned),
            "chain_prefix_event_count": len(chain_rows),
            "chain_prefix_head_sha256": returned["chain_sha256"],
        },
        "upstream_identity_attested": False,
        "credential_contents_recorded": False,
    }
    if lifecycle is not None:
        receipt["lifecycle"] = lifecycle
    receipt["receipt_sha256"] = stable_sha256(receipt)
    return receipt


def verify_receipt(receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    expected_sha = str(receipt.get("receipt_sha256") or "")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if receipt.get("schema") != RECEIPT_SCHEMA:
        errors.append("schema")
    if stable_sha256(unsigned) != expected_sha:
        errors.append("receipt_sha256")
    if errors:
        return {
            "valid": False,
            "receipt_sha256": expected_sha,
            "errors": errors,
            "writes_performed": 0,
            "processes_started": 0,
        }
    client = receipt.get("client") or {}
    transcript_record = receipt.get("transcript") or {}
    bridge = receipt.get("bridge") or {}
    lifecycle_record = receipt.get("lifecycle")
    try:
        binary = Path(client["binary_path"])
        if _file_sha256(binary) != client.get("binary_sha256"):
            errors.append("client_binary_sha256")
        version_args = _safe_command_args(
            client["version_args"], "client version arguments"
        )
        config_args = _safe_command_args(client["config_args"], "client config arguments")
        _verify_argument_file_evidence(
            version_args,
            client.get("version_argument_files", []),
            "client version arguments",
        )
        _verify_argument_file_evidence(
            config_args,
            client.get("config_argument_files", []),
            "client config arguments",
        )
        if not isinstance(client.get("version_output"), str) or not client[
            "version_output"
        ].strip():
            errors.append("client_version_output")
        config = client.get("sanitized_mcp_config")
        if not isinstance(config, dict):
            errors.append("sanitized_mcp_config")
        else:
            encoded_config = json.dumps(config, ensure_ascii=False, sort_keys=True)
            transport = config.get("transport")
            if contains_secret(encoded_config) or not isinstance(transport, dict):
                errors.append("sanitized_mcp_config")
            elif transport.get("env") or transport.get("env_vars"):
                errors.append("sanitized_mcp_config")
            elif transport.get("type") != "stdio":
                errors.append("sanitized_mcp_config")
            else:
                transport_args = transport.get("args")
                if not isinstance(transport_args, list) or not all(
                    isinstance(value, str) for value in transport_args
                ):
                    errors.append("sanitized_mcp_config")
                else:
                    try:
                        verify_redacted_agent_identity_launch_args(
                            transport_args,
                            config.get("identity_capability"),
                            db_path=Path(bridge["database_path"]),
                            scope=bridge["scope"],
                            claimed_agent_id=bridge["agent_id"],
                        )
                    except (KeyError, AgentIdentityError) as exc:
                        errors.append(f"agent_identity:{exc}")
                expected_route = {
                    "agent_id": bridge.get("agent_id"),
                    "scope": bridge.get("scope"),
                    **(bridge.get("runtime_identity") or {}),
                }
                if config.get("route") != expected_route:
                    errors.append("sanitized_mcp_config_route")
        if isinstance(config, dict) and stable_sha256(config) != client.get(
            "sanitized_mcp_config_sha256"
        ):
            errors.append("sanitized_mcp_config_sha256")
    except (KeyError, OSError, ReceiptError, subprocess.SubprocessError) as exc:
        errors.append(f"client:{exc}")
    try:
        path = Path(transcript_record["path"])
        current = _transcript_evidence(
            path,
            server_name=transcript_record["server_name"],
            tool=transcript_record["tool"],
            scope=bridge["scope"],
            agent_id=bridge["agent_id"],
            prefix_line_count=int(transcript_record["prefix_line_count"]),
        )
        for key, value in current.items():
            if transcript_record.get(key) != value:
                errors.append(f"transcript_{key}")
    except (KeyError, OSError, ReceiptError, UnicodeError) as exc:
        errors.append(f"transcript:{exc}")
    if lifecycle_record is not None:
        try:
            lifecycle = _lifecycle_snapshot(
                Path(lifecycle_record["path"]),
                transcript_path=Path(transcript_record["path"]),
            )
            if lifecycle != lifecycle_record:
                errors.append("lifecycle")
        except (KeyError, OSError, ReceiptError, UnicodeError) as exc:
            errors.append(f"lifecycle:{exc}")
    try:
        with _readonly_connection(Path(bridge["database_path"])) as connection:
            rows = _verify_chain_prefix(
                connection, bridge["scope"], int(bridge["return_event"]["sequence"])
            )
            snapshots = {int(row["sequence"]): _event_snapshot(row) for row in rows}
        for label in ("call_event", "return_event"):
            expected = bridge[label]
            if snapshots.get(int(expected["sequence"])) != expected:
                errors.append(label)
        if len(rows) != bridge.get("chain_prefix_event_count"):
            errors.append("chain_prefix_event_count")
        if rows[-1]["chain_sha256"] != bridge.get("chain_prefix_head_sha256"):
            errors.append("chain_prefix_head_sha256")
    except (KeyError, OSError, ReceiptError, sqlite3.Error, ValueError) as exc:
        errors.append(f"bridge:{exc}")
    return {
        "valid": not errors,
        "receipt_sha256": expected_sha,
        "errors": errors,
        "writes_performed": 0,
        "processes_started": 0,
    }


def capture_main() -> int:
    parser = argparse.ArgumentParser(description="Capture one MCP client receipt.")
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
    parser.add_argument("--client-binary", type=Path, required=True)
    parser.add_argument("--client-version-arg", action="append", default=[])
    parser.add_argument("--config-arg", action="append", default=[])
    parser.add_argument("--lifecycle", type=Path)
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
        server_name=args.server_name,
        transcript_path=args.transcript.resolve(),
        client_binary=args.client_binary.resolve(),
        client_version_args=tuple(args.client_version_arg or ["--version"]),
        config_args=tuple(args.config_arg),
        lifecycle_path=args.lifecycle.resolve() if args.lifecycle else None,
    )
    try:
        _write_json_create_only(args.output, receipt)
    except FileExistsError:
        parser.error(f"refusing to overwrite existing receipt: {args.output}")
    print(json.dumps({"status": "CAPTURED", **verify_receipt(args.output)}, sort_keys=True))
    return 0


def verify_main() -> int:
    parser = argparse.ArgumentParser(description="Verify an MCP client receipt without writes.")
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    result = verify_receipt(args.receipt.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(capture_main())
