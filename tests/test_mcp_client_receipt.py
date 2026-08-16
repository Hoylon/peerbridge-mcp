from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import peerbridge_mcp.mcp_client_receipt as mcp_client_receipt_module
from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.mcp_client_receipt import capture_receipt, verify_receipt
from peerbridge_mcp.server import handle_request


MODERN_META = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}


def _config_script(root: Path, config: dict) -> Path:
    path = root / "config.py"
    path.write_text(f"import json\nprint(json.dumps({config!r}))\n", encoding="utf-8")
    return path


def _evidence(root: Path) -> tuple[dict, Path, Path, Path, Bridge]:
    scope = "receipt-test"
    agent_id = "codex-main"
    identity = {
        "client_name": "codex-test",
        "provider_id": "openai-official",
        "model_id": "gpt-test",
        "reasoning_mode": "high",
    }
    bridge = Bridge(root, root / "bridge.sqlite3", agent_id, scope, **identity)
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {**MODERN_META, "name": "bridge_status", "arguments": {}},
        },
        {"bridge_status"},
    )
    result = response["result"]["structuredContent"]
    transcript = root / "transcript.jsonl"
    rows = [
        {"type": "thread.started", "thread_id": "thread-test"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-0",
                "type": "mcp_tool_call",
                "server": "peerbridge-main",
                "tool": "bridge_status",
                "arguments": {},
                "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
                "error": None,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "agent_message",
                "text": f"scope={scope} agent={agent_id}",
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    transcript.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    config = {
        "name": "peerbridge-main",
        "enabled": True,
        "disabled_reason": None,
        "transport": {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "peerbridge_mcp", "serve"],
            "env": None,
            "env_vars": [],
            "cwd": None,
        },
        "enabled_tools": None,
        "disabled_tools": None,
        "startup_timeout_sec": None,
        "tool_timeout_sec": None,
    }
    lifecycle = root / "lifecycle.json"
    stderr = root / "client.stderr.log"
    stderr.write_bytes(b"")
    lifecycle.write_text(
        json.dumps(
            {
                "schema": "peerbridge.test-child-lifecycle.v1",
                "step": "test",
                "started_utc": "2026-08-13T00:00:00Z",
                "finished_utc": "2026-08-13T00:00:01Z",
                "elapsed_seconds": 1.0,
                "completed_turn_observed": True,
                "exit_code": 0,
                "timed_out": False,
                "credential_values_read": False,
                "credential_values_recorded": False,
                "database_exists": True,
                "stdout": {
                    "path": str(transcript.resolve()),
                    "bytes": transcript.stat().st_size,
                    "sha256": __import__("hashlib").sha256(transcript.read_bytes()).hexdigest(),
                },
                "stderr": {
                    "path": str(stderr.resolve()),
                    "bytes": 0,
                    "sha256": __import__("hashlib").sha256(b"").hexdigest(),
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return identity, transcript, _config_script(root, config), lifecycle, bridge


def _capture(root: Path) -> tuple[Path, dict]:
    identity, transcript, config_script, lifecycle, _ = _evidence(root)
    receipt = capture_receipt(
        db_path=root / "bridge.sqlite3",
        scope="receipt-test",
        agent_id="codex-main",
        tool="bridge_status",
        server_name="peerbridge-main",
        transcript_path=transcript,
        client_binary=Path(sys.executable),
        client_version_args=("--version",),
        config_args=(str(config_script),),
        lifecycle_path=lifecycle,
        **identity,
    )
    path = root / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path, receipt


def test_receipt_binds_transcript_client_config_and_adjacent_events(tmp_path: Path) -> None:
    path, receipt = _capture(tmp_path)
    targets = [
        path,
        Path(receipt["transcript"]["path"]),
        Path(receipt["bridge"]["database_path"]),
        Path(receipt["client"]["binary_path"]),
        Path(receipt["lifecycle"]["path"]),
    ]
    before = {target: (target.stat().st_size, target.stat().st_mtime_ns) for target in targets}
    verified = verify_receipt(path)
    after = {target: (target.stat().st_size, target.stat().st_mtime_ns) for target in targets}
    assert verified["valid"] is True
    assert verified["writes_performed"] == 0
    assert before == after


def test_receipt_allows_append_only_bridge_events(tmp_path: Path) -> None:
    path, _ = _capture(tmp_path)
    Bridge(tmp_path, tmp_path / "bridge.sqlite3", "later", "receipt-test")
    assert verify_receipt(path)["valid"] is True


def test_receipt_rejects_transcript_tamper(tmp_path: Path) -> None:
    path, receipt = _capture(tmp_path)
    transcript = Path(receipt["transcript"]["path"])
    transcript.write_text(
        transcript.read_text(encoding="utf-8").replace("scope=receipt-test", "scope=drift"),
        encoding="utf-8",
    )
    verified = verify_receipt(path)
    assert verified["valid"] is False
    assert any(error.startswith("transcript:") for error in verified["errors"])


def test_receipt_allows_append_only_transcript_progress(tmp_path: Path) -> None:
    path, receipt = _capture(tmp_path)
    transcript = Path(receipt["transcript"]["path"])
    with transcript.open("a", encoding="utf-8") as target:
        target.write(json.dumps({"type": "later.unrelated"}) + "\n")
    assert verify_receipt(path)["valid"] is True


def test_receipt_rejects_missing_turn_completed(tmp_path: Path) -> None:
    identity, transcript, config_script, lifecycle, _ = _evidence(tmp_path)
    rows = transcript.read_text(encoding="utf-8").splitlines()
    transcript.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
    try:
        capture_receipt(
            db_path=tmp_path / "bridge.sqlite3",
            scope="receipt-test",
            agent_id="codex-main",
            tool="bridge_status",
            server_name="peerbridge-main",
            transcript_path=transcript,
            client_binary=Path(sys.executable),
            client_version_args=("--version",),
            config_args=(str(config_script),),
            lifecycle_path=lifecycle,
            **identity,
        )
    except Exception as exc:
        assert "completed turn" in str(exc)
    else:
        raise AssertionError("incomplete client turn was accepted")


def test_receipt_rejects_config_drift(tmp_path: Path) -> None:
    path, receipt = _capture(tmp_path)
    script = Path(receipt["client"]["config_args"][0])
    text = script.read_text(encoding="utf-8").replace("peerbridge-main", "other-server")
    script.write_text(text, encoding="utf-8")
    verified = verify_receipt(path)
    assert verified["valid"] is False
    assert any(error.startswith("client:") for error in verified["errors"])


def test_receipt_rejects_secret_bearing_config(tmp_path: Path) -> None:
    identity, transcript, script, lifecycle, _ = _evidence(tmp_path)
    script.write_text(
        "import json\nprint(json.dumps({'name':'peerbridge-main','transport':"
        "{'type':'stdio','command':'python','args':[],'env':{'TOKEN':'secret'},'env_vars':[]}}))\n",
        encoding="utf-8",
    )
    try:
        capture_receipt(
            db_path=tmp_path / "bridge.sqlite3",
            scope="receipt-test",
            agent_id="codex-main",
            tool="bridge_status",
            server_name="peerbridge-main",
            transcript_path=transcript,
            client_binary=Path(sys.executable),
            client_version_args=("--version",),
            config_args=(str(script),),
            lifecycle_path=lifecycle,
            **identity,
        )
    except Exception as exc:
        assert "environment data" in str(exc)
    else:
        raise AssertionError("secret-bearing config was accepted")


def test_receipt_rejects_unsuccessful_lifecycle(tmp_path: Path) -> None:
    path, receipt = _capture(tmp_path)
    lifecycle = Path(receipt["lifecycle"]["path"])
    value = json.loads(lifecycle.read_text(encoding="utf-8"))
    value["exit_code"] = 1
    lifecycle.write_text(json.dumps(value), encoding="utf-8")
    verified = verify_receipt(path)
    assert verified["valid"] is False
    assert any(error.startswith("lifecycle:") for error in verified["errors"])


def test_mcp_client_verify_never_starts_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = _capture(tmp_path)

    def forbidden_process(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("verify_receipt attempted to start a process")

    monkeypatch.setattr(mcp_client_receipt_module.subprocess, "run", forbidden_process)
    verified = verify_receipt(path)
    assert verified["valid"] is True
    assert verified["processes_started"] == 0
