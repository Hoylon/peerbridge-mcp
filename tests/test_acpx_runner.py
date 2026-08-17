from __future__ import annotations

import json
from pathlib import Path

import pytest

from peerbridge_mcp.acpx_runner import AcpxRunner
from peerbridge_mcp.openai_compatible_runner import (
    RouteMismatchError,
    RunnerConfig,
)


HEX_A = "a" * 64


def config(tmp_path: Path, *, model: str = "gpt-5.6-luna") -> RunnerConfig:
    return RunnerConfig(
        project_root=tmp_path,
        db_path=tmp_path / "peerbridge.sqlite3",
        scope="test",
        connection_id="acpx-codex",
        route_class="official",
        provider_id="openai-official",
        model=model,
        reasoning_mode="medium",
        route_profile_id="acpx-codex-luna-medium",
        route_profile_sha256=HEX_A,
        room_id="lobby",
        session_id="supervisor-one",
        agent_id="codex-main",
    )


def acp_events(*, model: str = "gpt-5.6-luna", reasoning: str = "medium") -> bytes:
    values = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {
                "agentInfo": {
                    "name": "@agentclientprotocol/codex-acp",
                    "version": "1.4.0",
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "sessionId": "session-one",
                "models": {
                    "currentModelId": f"{model}[{reasoning}]",
                    "availableModels": [{"modelId": f"{model}[{reasoning}]"}],
                },
                "configOptions": [
                    {"id": "model", "currentValue": model},
                    {"id": "reasoning_effort", "currentValue": reasoning},
                ],
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "audited "},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "reply"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 14, "outputTokens": 6},
            },
        },
    ]
    return ("\n".join(json.dumps(value) for value in values) + "\n").encode()


def test_runner_uses_stdin_and_emits_sanitized_identity_receipt(tmp_path: Path) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    calls = []

    def process_runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return 0, acp_events(), b"ignored stderr"

    result = AcpxRunner(
        config(tmp_path),
        credential_target="ACPX:codex",
        client_name="codex",
        executable=executable,
        process_runner=process_runner,
        runtime_admitted=True,
    ).run(
        [
            {"role": "system", "content": "reply safely"},
            {"role": "user", "content": "room prompt"},
        ],
        message_id="message-one",
    )

    command, kwargs = calls[0]
    assert "room prompt" not in command
    assert command[-4:] == ("codex", "exec", "-f", "-")
    assert "--deny-all" in command
    assert command[command.index("--auth-policy") + 1] == "skip"
    assert "--no-fs" in command
    assert "--no-terminal" in command
    assert "--mcp-config" in command
    assert "gpt-5.6-luna" in command
    assert kwargs["stdin_text"].endswith("room prompt")
    assert result.content == "audited reply"
    assert result.receipt["secret_backend"] == "native-acp"
    assert result.receipt["observed_agent_name"] == "@agentclientprotocol/codex-acp"
    assert result.receipt["credential_values_read_by_peerbridge"] is False
    assert result.receipt["mcp_tools_exposed"] is True
    assert result.receipt["usage"]["total_tokens"] == 20
    assert result.receipt["usage"]["total_tokens_derived"] is True
    mcp_config = Path(command[command.index("--mcp-config") + 1])
    payload = json.loads(mcp_config.read_text(encoding="utf-8"))
    server = payload["mcpServers"][0]
    assert server["name"] == "peerbridge"
    assert "send_message" not in server["args"]
    assert "bridge_status" in server["args"]
    assert "room prompt" not in json.dumps(result.receipt)


def test_runner_passes_only_codex_auth_family_to_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-auth")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-provider-secret")
    captured: dict[str, str] = {}

    def process_runner(_command, **kwargs):
        captured.update(kwargs["environment"])
        return 0, acp_events(), b""

    AcpxRunner(
        config(tmp_path),
        credential_target="ACPX:codex",
        client_name="codex",
        executable=executable,
        process_runner=process_runner,
        runtime_admitted=True,
    ).run([{"role": "user", "content": "room prompt"}])

    assert captured["OPENAI_API_KEY"] == "provider-auth"
    assert "AWS_SECRET_ACCESS_KEY" not in captured
    assert "ANTHROPIC_API_KEY" not in captured


def test_runner_rejects_agent_reference_mismatch(tmp_path: Path) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    with pytest.raises(RouteMismatchError, match="reference"):
        AcpxRunner(
            config(tmp_path),
            credential_target="ACPX:kimi",
            client_name="codex",
            executable=executable,
        )


def test_runner_rejects_observed_model_drift(tmp_path: Path) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    runner = AcpxRunner(
        config(tmp_path),
        credential_target="ACPX:codex",
        client_name="codex",
        executable=executable,
        process_runner=lambda *_args, **_kwargs: (
            0,
            acp_events(model="gpt-5.6-terra"),
            b"",
        ),
        runtime_admitted=True,
    )
    with pytest.raises(RouteMismatchError, match="model"):
        runner.run([{"role": "user", "content": "hello"}])
