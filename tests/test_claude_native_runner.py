from __future__ import annotations

import hashlib
import json
from pathlib import Path

from peerbridge_mcp.claude_native_runner import (
    RECEIPT_SCHEMA,
    ClaudeNativeWcmRunner,
)
from peerbridge_mcp.credentials import ProviderAccess
from peerbridge_mcp.inference_receipts import validate_inference_receipt
from peerbridge_mcp.openai_compatible_runner import RunnerConfig


def test_claude_native_runner_keeps_wcm_secret_out_of_command_and_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"trusted-claude-client")
    fixture_value = "fixture-value-never-recorded"
    access = ProviderAccess(
        endpoint="https://provider.example/v1",
        api_key=fixture_value,
        credential_target="PeerBridge:provider:test",
        endpoint_sha256="a" * 64,
        credential_fingerprint_sha256="b" * 64,
        secret_present=True,
        descriptor_schema="peerbridge.provider-credential.v2",
        route_class="relay",
        provider_id="relay-test",
        credential_version_sha256="c" * 64,
        descriptor_bound=True,
    )
    monkeypatch.setattr(
        "peerbridge_mcp.claude_native_runner.credentials.load_provider_access",
        lambda **_kwargs: access,
    )

    def process_runner(command, **kwargs):
        assert fixture_value not in command
        assert kwargs["environment"]["ANTHROPIC_API_KEY"] == fixture_value
        assert "ANTHROPIC_AUTH_TOKEN" not in kwargs["environment"]
        assert kwargs["environment"]["ANTHROPIC_BASE_URL"] == "https://provider.example"
        events = [
            {"type": "system", "model": "claude-fable-5"},
            {
                "type": "result",
                "is_error": False,
                "result": "FABLE_OK",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        ]
        return 0, ("\n".join(json.dumps(row) for row in events) + "\n").encode(), b""

    config = RunnerConfig(
        project_root=tmp_path,
        scope="scope-a",
        connection_id="relay-test",
        route_class="relay",
        provider_id="relay-test",
        model="claude-fable-5",
        reasoning_mode="high",
        route_profile_id="route-a",
        route_profile_sha256="d" * 64,
        room_id="room-a",
        session_id="session-a",
        agent_id="claude-code",
    )
    result = ClaudeNativeWcmRunner(
        config,
        executable=executable,
        process_runner=process_runner,
    ).run([{"role": "user", "content": "Review this."}], message_id="message-a")

    assert result.content == "FABLE_OK"
    assert result.receipt["schema"] == RECEIPT_SCHEMA
    assert result.receipt["secret_backend"] == "windows-credential-manager"
    assert fixture_value not in json.dumps(result.receipt, sort_keys=True)
    expected_route = {
        "route_profile_id": "route-a",
        "route_profile_sha256": "d" * 64,
        "route_class": "relay",
        "provider_id": "relay-test",
        "model_id": "claude-fable-5",
        "response_model_id": "claude-fable-5",
        "reasoning_mode": "high",
        "connection_id": "relay-test",
        "connection_sha256": "e" * 64,
        "room_id": "room-a",
        "session_id": "session-a",
        "endpoint_sha256": "a" * 64,
        "credential_version_sha256": "c" * 64,
    }
    validated = validate_inference_receipt(
        result.receipt,
        message_id="message-a",
        assistant_message=result.assistant_message,
        reply_body="FABLE_OK",
        expected_route=expected_route,
    )
    assert validated["receipt_schema"] == RECEIPT_SCHEMA
    assert result.receipt["response_sha256"] == hashlib.sha256(b"FABLE_OK").hexdigest()
