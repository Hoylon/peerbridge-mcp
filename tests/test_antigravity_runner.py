from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from peerbridge_mcp.antigravity_runner import AntigravityAgentApiRunner, _reasoning_request
from peerbridge_mcp.openai_compatible_runner import ConfigurationError, RunnerConfig


def config(tmp_path: Path, *, model: str = "gemini-3.7-flash") -> RunnerConfig:
    return RunnerConfig(
        project_root=tmp_path,
        scope="test-scope",
        connection_id="antigravity-local",
        route_class="official",
        provider_id="google-antigravity",
        model=model,
        timeout_seconds=2,
    )


def make_runtime(tmp_path: Path) -> tuple[Path, Path]:
    agentapi = tmp_path / "language_server.exe"
    program = tmp_path / "Antigravity.exe"
    agentapi.write_bytes(b"fixture")
    program.write_bytes(b"fixture")
    return agentapi, program


def test_rejects_unsupported_model(tmp_path: Path) -> None:
    agentapi, program = make_runtime(tmp_path)
    runner = AntigravityAgentApiRunner(
        config(tmp_path, model="unknown-model"),
        agentapi_path=agentapi,
        program_path=program,
    )
    with pytest.raises(ConfigurationError, match="flash_lite, flash, or pro"):
        runner.run([{"role": "user", "content": "hello"}])


def test_high_reasoning_is_requested_without_claiming_provider_verification() -> None:
    instruction = _reasoning_request("high")
    assert "Requested reasoning effort: high" in instruction
    assert "never expose hidden chain-of-thought" in instruction
    assert _reasoning_request("default") == ""


def test_run_reads_only_bound_transcript_and_records_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agentapi, program = make_runtime(tmp_path)
    conversation_id = "11111111-2222-3333-4444-555555555555"
    home = tmp_path / "home"
    transcript = (
        home
        / ".gemini/antigravity/brain"
        / conversation_id
        / ".system_generated/logs/transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 0,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "content": "<USER>\nReply with the marker.\n</USER>",
                    }
                ),
                json.dumps(
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": "intermediate tool result",
                    }
                ),
                json.dumps(
                    {
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "content": "ANTIGRAVITY_AGENTAPI_OK",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        request = json.loads(str(kwargs["input"]))
        assert request["arguments"][0] == "new-conversation"
        assert request["arguments"][1] == "--model=flash"
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {"response": {"newConversation": {"conversationId": conversation_id}}}
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("peerbridge_mcp.antigravity_runner._powershell", lambda: "pwsh")
    runner = AntigravityAgentApiRunner(
        config(tmp_path),
        agentapi_path=agentapi,
        program_path=program,
        poll_interval_seconds=0.001,
    )
    result = runner.run(
        [{"role": "user", "content": "Reply with the marker."}],
        message_id="probe",
    )
    assert result.content == "ANTIGRAVITY_AGENTAPI_OK"
    assert result.receipt["provider_id"] == "google-antigravity"
    assert result.receipt["requested_model_alias"] == "flash"
    assert result.receipt["requested_reasoning_mode"] is None
    assert result.receipt["observed_reasoning_mode"] is None
    assert result.receipt["reasoning_mode_verified"] is False
    assert result.receipt["reasoning_mode_contract"] == (
        "prompt_requested_only_agentapi_has_no_effort_control_or_readback"
    )
    assert result.receipt["gemini_api_key_used"] is False
    assert result.receipt["oauth_credential_read_by_peerbridge"] is False
    assert result.receipt["csrf_token_recorded"] is False
    assert result.receipt["credential_contents_recorded"] is False
    assert result.receipt["receipt_sha256"]
