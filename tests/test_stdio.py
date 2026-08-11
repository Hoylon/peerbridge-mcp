from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


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
    }
    assert by_id["legacy-init"]["result"]["protocolVersion"] == "2025-11-25"
