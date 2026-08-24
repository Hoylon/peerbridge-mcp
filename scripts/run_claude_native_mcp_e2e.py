from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from peerbridge_mcp.agent_identity import ensure_agent_identity_capability
from peerbridge_mcp.bridge import Bridge, utc_now
from peerbridge_mcp.ccswitch_runner import _bounded_process, find_claude_cli
from peerbridge_mcp.child_environment import build_agent_child_environment
from peerbridge_mcp.claude_client_receipt import capture_receipt, verify_receipt
from peerbridge_mcp.secret_scan import contains_secret


SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
RELAY_OVERRIDE_NAMES = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
SERVER_NAME = "peerbridge-claude-native"
TOOL_NAME = "bridge_status"


def _safe(value: str, label: str) -> str:
    text = value.strip()
    if not SAFE_NAME.fullmatch(text):
        raise ValueError(f"{label} is not a safe identifier")
    return text


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _state(paths: list[Path]) -> dict[str, tuple[int, int, str]]:
    return {
        str(path.resolve()): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            _sha256(path),
        )
        for path in paths
    }


def _write_bytes_create_only(path: Path, value: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(value)


def _write_json_create_only(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(
            value,
            handle,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        handle.write("\n")


def _official_environment(claude_binary: Path) -> tuple[dict[str, str], list[str]]:
    if not claude_binary.is_file():
        raise FileNotFoundError("official Claude Code binary is unavailable")
    environment = build_agent_child_environment("claude")
    removed: list[str] = []
    for expected in RELAY_OVERRIDE_NAMES:
        matches = [name for name in environment if name.upper() == expected]
        for name in matches:
            environment.pop(name, None)
            removed.append(expected)
    if any(name.upper() in RELAY_OVERRIDE_NAMES for name in environment):
        raise RuntimeError("official Claude environment retained a relay override")
    # This records the enforced denylist even when an override was not present.
    return environment, sorted(set(removed).union(RELAY_OVERRIDE_NAMES))


def _server_config(
    *,
    python_binary: Path,
    root: Path,
    db_path: Path,
    scope: str,
    agent_id: str,
    session_id: str,
    model: str,
    reasoning_mode: str,
    identity_capability_path: Path,
) -> dict[str, Any]:
    return {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": str(python_binary.resolve()),
                "args": [
                    "-m",
                    "peerbridge_mcp",
                    "serve",
                    "--project-root",
                    str(root),
                    "--db",
                    str(db_path),
                    "--agent-id",
                    agent_id,
                    "--identity-capability",
                    str(identity_capability_path.resolve()),
                    "--scope",
                    scope,
                    "--session-id",
                    session_id,
                    "--client-name",
                    "claude-code-native",
                    "--provider-id",
                    "anthropic-official:claude-code",
                    "--model-id",
                    model,
                    "--reasoning-mode",
                    reasoning_mode,
                    "--route-class",
                    "official",
                    "--allow-tool",
                    TOOL_NAME,
                ],
                "cwd": str(root),
            }
        }
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one create-only official Claude Code subscription-to-PeerBridge "
            "native MCP E2E gate."
        )
    )
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--reasoning-mode", default="default")
    parser.add_argument("--receipt-name", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.project_root).resolve()
        scope = _safe(args.scope, "scope")
        agent_id = _safe(args.agent_id, "agent ID")
        session_id = _safe(args.session_id, "session ID")
        model = _safe(args.model, "model")
        reasoning_mode = _safe(args.reasoning_mode, "reasoning mode")
        receipt_name = _safe(args.receipt_name, "receipt name")
        if args.timeout_seconds <= 0 or args.timeout_seconds > 600:
            raise ValueError("timeout must be between 0 and 600 seconds")

        claude_binary = find_claude_cli()
        if claude_binary is None:
            raise FileNotFoundError("official Claude Code CLI is unavailable")
        python_binary = Path(sys.executable).resolve()
        evidence_root = root / ".peerbridge" / "e2e" / receipt_name
        receipt_path = root / ".peerbridge" / "receipts" / f"{receipt_name}.json"
        if evidence_root.exists() or receipt_path.exists():
            raise FileExistsError("E2E evidence or receipt already exists")
        evidence_root.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_root.mkdir()

        db_path = evidence_root / "bridge.sqlite3"
        config_path = evidence_root / "claude-mcp.json"
        prompt_path = evidence_root / "prompt.txt"
        stdout_path = evidence_root / "stdout.jsonl"
        stderr_path = evidence_root / "stderr.log"
        lifecycle_path = evidence_root / "lifecycle.json"
        Bridge(root, db_path, agent_id, scope, session_id=session_id)
        identity_capability = ensure_agent_identity_capability(
            root,
            db_path,
            scope,
            agent_id,
            allowed_tools=(TOOL_NAME,),
            route_binding={
                "client_name": "claude-code-native",
                "provider_id": "anthropic-official:claude-code",
                "model_id": model,
                "reasoning_mode": reasoning_mode,
                "route_class": "official",
            },
        )

        config = _server_config(
            python_binary=python_binary,
            root=root,
            db_path=db_path,
            scope=scope,
            agent_id=agent_id,
            session_id=session_id,
            model=model,
            reasoning_mode=reasoning_mode,
            identity_capability_path=identity_capability.path,
        )
        _write_json_create_only(config_path, config)
        prompt = (
            f"Call {TOOL_NAME} exactly once. Then reply with exactly this text: "
            f"{scope} {agent_id} done\n"
        )
        _write_bytes_create_only(prompt_path, prompt.encode("utf-8"))

        environment, removed_names = _official_environment(claude_binary)
        full_tool_name = f"mcp__{SERVER_NAME}__{TOOL_NAME}"
        command = [
            str(claude_binary),
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            str(config_path),
            "--model",
            model,
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--allowedTools",
            full_tool_name,
            "--disable-slash-commands",
            "--no-chrome",
        ]
        if reasoning_mode != "default":
            command.extend(("--effort", reasoning_mode))
        started = utc_now()
        return_code, stdout, stderr = _bounded_process(
            command,
            cwd=root,
            environment=environment,
            stdin_text=prompt,
            timeout_seconds=args.timeout_seconds,
            runtime_label="official Claude Code native MCP E2E",
        )
        finished = utc_now()
        decoded_evidence = stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode(
            "utf-8", errors="replace"
        )
        if contains_secret(decoded_evidence):
            raise RuntimeError("Claude native evidence failed credential scan")
        _write_bytes_create_only(stdout_path, stdout)
        _write_bytes_create_only(stderr_path, stderr)
        if return_code != 0:
            raise RuntimeError("Claude native client exited unsuccessfully")

        lifecycle = {
            "schema": "peerbridge.test-native-client-lifecycle.v1",
            "attempt": receipt_name,
            "client": "claude-code",
            "provider_class": "official",
            "server": SERVER_NAME,
            "allowed_tools": [TOOL_NAME],
            "started_utc": started,
            "finished_utc": finished,
            "timeout_seconds": args.timeout_seconds,
            "exit_code": return_code,
            "timed_out": False,
            "relay_override_names_removed": removed_names,
            "config": _file_record(config_path),
            "stdout": _file_record(stdout_path),
            "stderr": _file_record(stderr_path),
            "prompt": _file_record(prompt_path),
            "credential_values_read": False,
            "credential_values_recorded": False,
        }
        _write_json_create_only(lifecycle_path, lifecycle)

        receipt = capture_receipt(
            db_path=db_path,
            scope=scope,
            agent_id=agent_id,
            client_name="claude-code-native",
            provider_id="anthropic-official:claude-code",
            model_id=model,
            reasoning_mode=reasoning_mode,
            route_class="official",
            tool=TOOL_NAME,
            server_name=SERVER_NAME,
            transcript_path=stdout_path,
            config_path=config_path,
            lifecycle_path=lifecycle_path,
            client_binary=claude_binary,
        )
        _write_json_create_only(receipt_path, receipt)
        observed = [
            db_path,
            config_path,
            prompt_path,
            stdout_path,
            stderr_path,
            lifecycle_path,
            receipt_path,
        ]
        before = _state(observed)
        verification = verify_receipt(receipt_path)
        after = _state(observed)
        if not verification.get("valid") or verification.get("writes_performed") != 0:
            raise RuntimeError("Claude native receipt verification failed")
        if before != after:
            raise RuntimeError("Claude native receipt verification changed evidence")
        summary = {
            "status": "PASS",
            "client": "claude-code-native",
            "provider_id": "anthropic-official:claude-code",
            "route_class": "official",
            "configured_model": model,
            "observed_model": receipt["transcript"]["observed_model_id"],
            "mcp_tools": [TOOL_NAME],
            "mcp_tool_call_count": 1,
            "credential_values_recorded": False,
            "receipt_sha256": receipt["receipt_sha256"],
            "receipt_path": receipt_path.relative_to(root).as_posix(),
            "zero_write_verify": True,
        }
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "FAIL", "error_type": type(exc).__name__},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
