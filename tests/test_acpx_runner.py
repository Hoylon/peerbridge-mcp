from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from peerbridge_mcp.acpx_runner import AcpxRunner, verify_acpx_inference_receipt
from peerbridge_mcp.agent_identity import ensure_agent_identity_capability
from peerbridge_mcp.attachments import stage_chat_attachment_payloads
from peerbridge_mcp.bridge import Bridge, stable_sha256
from peerbridge_mcp.multimodal import (
    VERIFIED_ATTACHMENT_MESSAGE_KEY,
    verify_staged_attachments,
)
from peerbridge_mcp.openai_compatible_runner import (
    ConfigurationError,
    CredentialUnavailableError,
    ProviderHTTPError,
    ResourceUnavailableError,
    RouteMismatchError,
    RunnerConfig,
)
from tests._image_fixtures import PNG


HEX_A = "a" * 64


def config(tmp_path: Path, *, model: str = "gpt-5.6-luna") -> RunnerConfig:
    base = RunnerConfig(
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
    assert base.db_path is not None
    Bridge(tmp_path, base.db_path, "test-identity-authority", base.scope)
    capability = ensure_agent_identity_capability(
        tmp_path,
        base.db_path,
        base.scope,
        base.agent_id,
    )
    return replace(base, identity_capability_path=capability.path)


def json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def codex_prompt_events(
    *,
    model: str = "gpt-5.6-luna",
    reasoning: str = "medium",
    answer: str = "audited reply",
) -> bytes:
    values = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {
                "protocolVersion": 1,
                "agentInfo": {
                    "name": "@agentclientprotocol/codex-acp",
                    "version": "0.9.0-test",
                },
            },
        },
        {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "sessionId": "session-one",
                "models": {
                    "currentModelId": model,
                    "availableModels": [{"modelId": model}],
                },
                "configOptions": [
                    {
                        "id": "reasoning_effort",
                        "currentValue": reasoning,
                    }
                ],
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tool-one",
                    "title": "bridge_status",
                    "rawInput": {"tool_name": "mcp.peerbridge.bridge_status"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": answer},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "stopReason": "end_turn",
                "usage": {"input_tokens": 14, "output_tokens": 6},
            },
        },
    ]
    return ("\n".join(json.dumps(value) for value in values) + "\n").encode()


def codex_session_record(
    session_name: str,
    *,
    model: str = "gpt-5.6-luna",
    reasoning: str = "medium",
    answer: str = "audited reply",
) -> dict[str, object]:
    return {
        "name": session_name,
        "closed": False,
        "acpxRecordId": "record-one",
        "acpSessionId": "session-one",
        "lastRequestId": "request-one",
        "lastSeq": 12,
        "protocolVersion": 1,
        "agentCommand": "npx @agentclientprotocol/codex-acp",
        "eventLog": {"lastWriteError": None},
        "messages": [
            {
                "User": {
                    "id": "user-one",
                    "content": [{"Text": "room prompt"}],
                }
            },
            {
                "Agent": {
                    "id": "agent-one",
                    "content": [
                        {"Thinking": "private reasoning is not retained"},
                        {
                            "ToolUse": {
                                "id": "tool-one",
                                "name": "mcp.peerbridge.bridge_status",
                                "input": {},
                            }
                        },
                        {"Text": answer},
                    ],
                    "tool_results": {
                        "tool-one": {
                            "tool_name": "mcp.peerbridge.bridge_status",
                            "is_error": False,
                        }
                    },
                }
            },
        ],
        "request_token_usage": {
            "user-one": {
                "input_tokens": 14,
                "output_tokens": 6,
                "thought_tokens": 2,
            }
        },
        "acpx": {
            "current_model_id": model,
            "desired_config_options": {"reasoning_effort": reasoning},
            "available_models": [{"modelId": model}],
        },
    }


def codex_process_runner(
    calls: list[tuple[tuple[str, ...], dict[str, object]]],
    *,
    observed_model: str = "gpt-5.6-luna",
    observed_reasoning: str = "medium",
):
    def process_runner(command, **kwargs):
        command = tuple(command)
        calls.append((command, kwargs))
        index = command.index("codex")
        tail = command[index:]
        if tail[:3] == ("codex", "sessions", "new"):
            name = tail[tail.index("--name") + 1]
            return 0, json_bytes(
                {
                    "action": "session_ensured",
                    "created": True,
                    "name": name,
                    "replacedSessionId": None,
                    "acpxRecordId": "record-one",
                    "acpxSessionId": "record-one",
                }
            ), b""
        name = (
            tail[tail.index("--session") + 1]
            if "--session" in tail
            else tail[3]
        )
        if tail[:2] == ("codex", "set") and tail[-2] == "model":
            return 0, json_bytes(
                {
                    "action": "model_set",
                    "modelId": tail[-1],
                    "acpxRecordId": "record-one",
                }
            ), b""
        if tail[:2] == ("codex", "set") and tail[-2] == "reasoning_effort":
            return 0, json_bytes(
                {
                    "action": "config_set",
                    "configId": "reasoning_effort",
                    "value": tail[-1],
                    "acpxRecordId": "record-one",
                }
            ), b""
        if tail[:2] == ("codex", "prompt"):
            return 0, codex_prompt_events(
                model=observed_model,
                reasoning=observed_reasoning,
            ), b""
        if tail[:3] == ("codex", "sessions", "show"):
            return 0, json_bytes(
                codex_session_record(
                    name,
                    model=observed_model,
                    reasoning=observed_reasoning,
                )
            ), b""
        if tail[:3] == ("codex", "sessions", "close"):
            return 0, json_bytes(
                {
                    "action": "session_closed",
                    "acpxRecordId": "record-one",
                    "acpxSessionId": "session-one",
                }
            ), b""
        raise AssertionError(f"unexpected ACPX command: {tail}")

    return process_runner


def grok_acp_events() -> bytes:
    values = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {
                "protocolVersion": 1,
                "_meta": {"grokShell": True, "agentVersion": "1.0.1"},
            },
        },
        {"jsonrpc": "2.0", "id": 1, "method": "authenticate", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "result": {"_meta": {"private": True}}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "sessionId": "grok-session",
                "models": {
                    "currentModelId": "grok-4.6",
                    "availableModels": [{"modelId": "grok-4.6"}],
                },
            },
        },
        {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-search-one",
                    "title": "search_tool",
                    "rawInput": {"query": "bridge status"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-tool-one",
                    "title": "use_tool",
                    "rawInput": {
                        "tool_name": "peerbridge__bridge_status",
                        "tool_input": {},
                    },
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "grok ready"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "stopReason": "end_turn",
                "_meta": {"usage": {"inputTokens": 8, "outputTokens": 2}},
            },
        },
    ]
    return ("\n".join(json.dumps(value) for value in values) + "\n").encode()


def failed_acp_events(
    method: str,
    *,
    code: int = -32603,
    message: str = "must not be exposed",
) -> bytes:
    values = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": 1}},
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": code, "message": message},
        },
    ]
    return ("\n".join(json.dumps(value) for value in values) + "\n").encode()


def test_runner_uses_stdin_and_emits_sanitized_identity_receipt(tmp_path: Path) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    runner_config = config(tmp_path)
    result = AcpxRunner(
        runner_config,
        credential_target="ACPX:codex",
        client_name="codex",
        executable=executable,
        process_runner=codex_process_runner(calls),
        runtime_admitted=True,
    ).run(
        [
            {"role": "system", "content": "reply safely"},
            {"role": "user", "content": "room prompt"},
        ],
        message_id="message-one",
    )

    assert len(calls) == 6
    commands = [call[0] for call in calls]
    assert all("room prompt" not in command for command in commands)
    prompt_command, prompt_kwargs = next(
        call for call in calls if "prompt" in call[0]
    )
    prompt_index = prompt_command.index("codex")
    assert prompt_command[prompt_index : prompt_index + 2] == ("codex", "prompt")
    assert "--no-wait" not in prompt_command
    assert prompt_command[-2:] == ("--file", "-")
    command = commands[0]
    assert "--deny-all" not in command
    assert "--permission-policy" in command
    permission_policy = json.loads(
        command[command.index("--permission-policy") + 1]
    )
    expected_matchers = {
        variant
        for tool in runner_config.allowed_tools
        for variant in (
            tool,
            f"mcp__peerbridge__{tool}",
            f"peerbridge__{tool}",
            f"mcp.peerbridge.{tool}",
        )
    }
    assert set(permission_policy["autoApprove"]) == expected_matchers
    assert permission_policy["defaultAction"] == "deny"
    assert "*" not in permission_policy["autoApprove"]
    assert command[command.index("--auth-policy") + 1] == "skip"
    assert "--no-fs" in command
    assert "--no-terminal" in command
    assert "--mcp-config" in command
    assert "--model" not in command
    model_command = next(value for value in commands if value[-2] == "model")
    assert model_command[-1] == "gpt-5.6-luna"
    reasoning_command = next(
        value for value in commands if value[-2] == "reasoning_effort"
    )
    assert reasoning_command[-1] == "medium"
    assert str(prompt_kwargs["stdin_text"]).endswith("room prompt")
    assert all(
        not str(kwargs["stdin_text"])
        for invoked, kwargs in calls
        if invoked is not prompt_command
    )
    assert result.content == "audited reply"
    assert result.receipt["secret_backend"] == "native-acp"
    assert result.receipt["observed_agent_name"] == "@agentclientprotocol/codex-acp"
    assert result.receipt["observed_agent_version"] == "0.9.0-test"
    assert result.receipt["credential_values_read_by_peerbridge"] is False
    assert result.receipt["route_class"] == "official"
    assert result.receipt["route_class_source"] == "requested-route-profile-binding"
    assert result.receipt["requested_route_class"] == "official"
    assert result.receipt["observed_route_class"] is None
    assert result.receipt["provider_route_class"] is None
    assert result.receipt["provider_route_class_attested"] is False
    assert result.receipt["mcp_tools_exposed"] is True
    assert result.receipt["permission_policy"] == (
        "explicit-mcp-allowlist-default-deny"
    )
    assert len(result.receipt["permission_policy_sha256"]) == 64
    assert result.receipt["usage"]["total_tokens"] == 20
    assert result.receipt["usage"]["total_tokens_derived"] is True
    assert result.receipt["usage"]["reasoning_tokens"] == 2
    assert result.receipt["mcp_allowed_tool_call_count"] == 1
    assert result.receipt["mcp_tool_error_count"] == 0
    assert result.receipt["session_soft_close_confirmed"] is True
    assert result.receipt["acp_session_transition_observed"] is True
    assert len(result.receipt["provisional_acp_session_id_sha256"]) == 64
    assert result.receipt["provisional_acp_session_id_sha256"] != result.receipt[
        "acp_session_id_sha256"
    ]
    assert result.receipt["lifecycle_mode"] == (
        "persistent-session-blocking-prompt-poll-soft-close"
    )
    assert result.receipt["stream_and_record_tool_audit_equal"] is True
    assert result.receipt["lifecycle_command_count"] == 6
    mcp_config = Path(command[command.index("--mcp-config") + 1])
    payload = json.loads(mcp_config.read_text(encoding="utf-8"))
    server = payload["mcpServers"][0]
    assert server["name"] == "peerbridge"
    assert "send_message" not in server["args"]
    assert "bridge_status" in server["args"]
    assert "room prompt" not in json.dumps(result.receipt)
    verification = verify_acpx_inference_receipt(result.receipt)
    assert verification["valid"] is True
    assert verification["writes_performed"] == 0
    assert verification["requested_route_class"] == "official"
    assert verification["observed_route_class"] is None
    assert verification["mcp_canonical_tools_called"] == ["bridge_status"]
    tampered = dict(result.receipt)
    tampered["observed_model"] = "tampered-model"
    with pytest.raises(ValueError, match="does not match"):
        verify_acpx_inference_receipt(tampered)
    overclaimed = dict(result.receipt)
    overclaimed["observed_route_class"] = "official"
    overclaimed["receipt_sha256"] = stable_sha256(
        {key: value for key, value in overclaimed.items() if key != "receipt_sha256"}
    )
    with pytest.raises(ValueError, match="overclaims"):
        verify_acpx_inference_receipt(overclaimed)


def test_runner_rejects_soft_close_effective_session_identity_drift(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    lifecycle_runner = codex_process_runner(calls)

    def drifting_close_runner(command, **kwargs):
        command_tuple = tuple(command)
        index = command_tuple.index("codex")
        if command_tuple[index : index + 3] == (
            "codex",
            "sessions",
            "close",
        ):
            return 0, json_bytes(
                {
                    "action": "session_closed",
                    "acpxRecordId": "record-one",
                    "acpxSessionId": "wrong-session",
                }
            ), b""
        return lifecycle_runner(command, **kwargs)

    runner = AcpxRunner(
        config(tmp_path),
        credential_target="ACPX:codex",
        client_name="codex",
        executable=executable,
        process_runner=drifting_close_runner,
        runtime_admitted=True,
    )

    with pytest.raises(ProviderHTTPError, match="soft-close failed") as captured:
        runner.run([{"role": "user", "content": "health check"}])
    assert isinstance(captured.value.__cause__, RouteMismatchError)
    assert captured.value.retryable is False


def test_ambiguous_paid_codex_turn_is_terminal_after_cleanup(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    lifecycle_runner = codex_process_runner(calls)

    def ambiguous_prompt_runner(command, **kwargs):
        command_tuple = tuple(command)
        index = command_tuple.index("codex")
        if command_tuple[index : index + 2] == ("codex", "prompt"):
            calls.append((command_tuple, kwargs))
            raise ResourceUnavailableError("prompt transport timed out")
        return lifecycle_runner(command, **kwargs)

    runner = AcpxRunner(
        config(tmp_path),
        credential_target="ACPX:codex",
        client_name="codex",
        executable=executable,
        process_runner=ambiguous_prompt_runner,
        runtime_admitted=True,
    )

    with pytest.raises(ProviderHTTPError, match="cannot be replayed") as captured:
        runner.run([{"role": "user", "content": "health check"}])

    assert captured.value.retryable is False
    assert isinstance(captured.value.__cause__, ResourceUnavailableError)
    assert sum("prompt" in command for command, _kwargs in calls) == 1
    assert sum("close" in command for command, _kwargs in calls) == 1


def test_runner_passes_no_provider_credentials_to_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-auth")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://relay.invalid")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-provider-secret")
    captured: dict[str, str] = {}
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    lifecycle_runner = codex_process_runner(calls)

    def process_runner(command, **kwargs):
        captured.update(kwargs["environment"])
        return lifecycle_runner(command, **kwargs)

    AcpxRunner(
        config(tmp_path),
        credential_target="ACPX:codex",
        client_name="codex",
        executable=executable,
        process_runner=process_runner,
        runtime_admitted=True,
    ).run([{"role": "user", "content": "room prompt"}])

    assert "OPENAI_API_KEY" not in captured
    assert "AWS_SECRET_ACCESS_KEY" not in captured
    assert "ANTHROPIC_API_KEY" not in captured
    assert "OPENAI_BASE_URL" not in captured


def test_official_claude_route_removes_relay_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    selectors = {
        "ANTHROPIC_API_KEY": "unit-test-provider-secret",
        "ANTHROPIC_AUTH_TOKEN": "unit-test-provider-secret",
        "ANTHROPIC_BASE_URL": "https://relay.invalid",
        "CLAUDE_CODE_OAUTH_TOKEN": "unit-test-provider-secret",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
    }
    for name, value in selectors.items():
        monkeypatch.setenv(name, value)
    runner = AcpxRunner(
        replace(
            config(tmp_path, model="claude-fable-5"),
            connection_id="official-claude",
            provider_id="anthropic-official",
            route_profile_id="official-claude-fable",
        ),
        credential_target="ACPX:claude",
        client_name="claude",
        executable=executable,
        runtime_admitted=True,
    )

    environment, _runtime, audit = runner._child_environment()

    assert not selectors.keys() & environment.keys()
    assert audit["requested_route_class"] == "official"
    assert audit["observed_route_class"] is None
    assert audit["provider_route_class"] is None
    assert audit["provider_route_class_attested"] is False
    assert audit["provider_environment_policy"] == "provider-credentials-stripped"
    assert audit["provider_override_names_removed"] == sorted(selectors)
    assert audit["provider_override_names_present"] == []


def test_relay_claude_route_also_strips_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "relay-auth")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://relay.invalid")
    runner = AcpxRunner(
        replace(
            config(tmp_path, model="claude-fable-5"),
            connection_id="relay-claude",
            route_class="relay",
            provider_id="relay-provider",
            route_profile_id="relay-claude-fable",
        ),
        credential_target="ACPX:claude",
        client_name="claude",
        executable=executable,
        runtime_admitted=True,
    )

    environment, _runtime, audit = runner._child_environment()

    assert "ANTHROPIC_AUTH_TOKEN" not in environment
    assert "ANTHROPIC_BASE_URL" not in environment
    assert audit["requested_route_class"] == "relay"
    assert audit["observed_route_class"] is None
    assert audit["provider_route_class"] is None
    assert audit["provider_route_class_attested"] is False
    assert audit["provider_environment_policy"] == "provider-credentials-stripped"
    assert audit["provider_override_names_removed"] == [
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    ]
    assert audit["provider_override_names_present"] == []


def test_runner_handles_grok_auth_handshake_and_identity_metadata(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    result = AcpxRunner(
        config(tmp_path, model="grok-4.6"),
        credential_target="ACPX:grok-build",
        client_name="grok-build",
        executable=executable,
        process_runner=lambda *_args, **_kwargs: (0, grok_acp_events(), b""),
        runtime_admitted=True,
    ).run([{"role": "user", "content": "health check"}])

    assert result.content == "grok ready"
    assert result.receipt["agent_id"] == "grok-build"
    assert result.receipt["observed_agent_name"] == "grok-build"
    assert result.receipt["observed_agent_version"] == "1.0.1"
    assert result.receipt["observed_model"] == "grok-4.6"
    assert result.receipt["agent_tool_call_event_count"] == 2
    assert result.receipt["agent_auxiliary_tool_call_count"] == 1
    assert result.receipt["mcp_tool_call_count"] == 1
    assert result.receipt["mcp_allowed_tool_call_count"] == 1
    assert result.receipt["mcp_unrecognized_tool_call_count"] == 0
    assert result.receipt["mcp_canonical_tools_called"] == ["bridge_status"]
    assert "private" not in json.dumps(result.receipt)


def test_legacy_grok_acpx_turn_rejects_filesystem_attachments_before_launch(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    staged = stage_chat_attachment_payloads(tmp_path, (("chart.png", PNG),))
    verified = verify_staged_attachments(tmp_path, staged)
    process_started = False

    def process_runner(*_args, **_kwargs):
        nonlocal process_started
        process_started = True
        return 0, grok_acp_events(), b""

    with pytest.raises(ConfigurationError, match="does not expose filesystem"):
        AcpxRunner(
            config(tmp_path, model="grok-4.6"),
            credential_target="ACPX:grok-build",
            client_name="grok-build",
            executable=executable,
            process_runner=process_runner,
            runtime_admitted=True,
        ).run(
            [
                {
                    "role": "user",
                    "content": "Inspect this chart.",
                    VERIFIED_ATTACHMENT_MESSAGE_KEY: [
                        item.public_record() for item in verified
                    ],
                }
            ]
        )
    assert process_started is False


def test_legacy_acpx_adapter_rejects_audio_before_process_start(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    staged = stage_chat_attachment_payloads(
        tmp_path,
        (("voice.wav", b"RIFF" + (8).to_bytes(4, "little") + b"WAVEdata"),),
    )
    verified = verify_staged_attachments(tmp_path, staged)
    process_started = False

    def process_runner(*_args, **_kwargs):
        nonlocal process_started
        process_started = True
        return 0, grok_acp_events(), b""

    runner = AcpxRunner(
        config(tmp_path, model="grok-4.6"),
        credential_target="ACPX:grok-build",
        client_name="grok-build",
        executable=executable,
        process_runner=process_runner,
        runtime_admitted=True,
    )

    with pytest.raises(
        ConfigurationError,
        match="does not expose filesystem attachments",
    ):
        runner.run(
            [
                {
                    "role": "user",
                    "content": "Transcribe this evidence.",
                    VERIFIED_ATTACHMENT_MESSAGE_KEY: [
                        item.public_record() for item in verified
                    ],
                }
            ]
        )

    assert process_started is False


def test_runner_classifies_only_explicit_authentication_failure_as_credential_error(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    runner = AcpxRunner(
        config(tmp_path, model="grok-4.6"),
        credential_target="ACPX:grok-build",
        client_name="grok-build",
        executable=executable,
        process_runner=lambda *_args, **_kwargs: (
            1,
            failed_acp_events("authenticate"),
            b"private diagnostic",
        ),
        runtime_admitted=True,
    )

    with pytest.raises(CredentialUnavailableError, match="authentication failed"):
        runner.run([{"role": "user", "content": "health check"}])


def test_runner_classifies_acp_auth_required_without_exposing_provider_detail(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    runner = AcpxRunner(
        config(tmp_path, model="claude-fable-5"),
        credential_target="ACPX:claude",
        client_name="claude",
        executable=executable,
        process_runner=lambda *_args, **_kwargs: (
            1,
            failed_acp_events(
                "session/prompt",
                code=-32000,
                message="Authentication required: private provider diagnostic",
            ),
            b"private stderr",
        ),
        runtime_admitted=True,
    )

    with pytest.raises(CredentialUnavailableError) as captured:
        runner.run([{"role": "user", "content": "health check"}])
    assert str(captured.value) == (
        "ACPX Agent credential is unavailable or unsupported"
    )
    assert "private" not in str(captured.value)


def test_runner_keeps_non_authentication_exit_retryable_and_sanitized(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    runner = AcpxRunner(
        config(tmp_path, model="grok-4.6"),
        credential_target="ACPX:grok-build",
        client_name="grok-build",
        executable=executable,
        process_runner=lambda *_args, **_kwargs: (
            1,
            failed_acp_events("session/new"),
            b"private diagnostic",
        ),
        runtime_admitted=True,
    )

    with pytest.raises(ProviderHTTPError) as captured:
        runner.run([{"role": "user", "content": "health check"}])
    assert captured.value.retryable is True
    assert captured.value.status_code == 502
    assert "private" not in str(captured.value)


def test_runner_keeps_ambiguous_rate_limit_terminal_without_provider_detail(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    runner = AcpxRunner(
        config(tmp_path, model="grok-4.6"),
        credential_target="ACPX:grok-build",
        client_name="grok-build",
        executable=executable,
        process_runner=lambda *_args, **_kwargs: (
            1,
            failed_acp_events(
                "session/prompt",
                code=-32003,
                message="Rate limited: private provider diagnostic",
            ),
            b"private stderr",
        ),
        runtime_admitted=True,
    )

    with pytest.raises(ProviderHTTPError) as captured:
        runner.run([{"role": "user", "content": "health check"}])
    assert captured.value.retryable is False
    assert captured.value.status_code == 429
    assert str(captured.value) == "ACPX Agent rate limited"


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


def test_runner_rejects_conflicting_codex_model_variant(tmp_path: Path) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    runner = AcpxRunner(
        config(tmp_path, model="gpt-5.6-luna[high]"),
        credential_target="ACPX:codex",
        client_name="codex",
        executable=executable,
        process_runner=lambda *_args, **_kwargs: (0, b"{}", b""),
        runtime_admitted=True,
    )
    with pytest.raises(
        ConfigurationError,
        match="model and reasoning mode must be configured separately",
    ):
        runner.run([{"role": "user", "content": "hello"}])


def test_runner_rejects_observed_model_drift(tmp_path: Path) -> None:
    executable = tmp_path / "acpx.cmd"
    executable.write_bytes(b"acpx launcher")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    runner = AcpxRunner(
        config(tmp_path),
        credential_target="ACPX:codex",
        client_name="codex",
        executable=executable,
        process_runner=codex_process_runner(
            calls,
            observed_model="gpt-5.6-terra",
        ),
        runtime_admitted=True,
    )
    with pytest.raises(RouteMismatchError, match="model"):
        runner.run([{"role": "user", "content": "hello"}])
