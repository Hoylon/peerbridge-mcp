from __future__ import annotations

import json
import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from peerbridge_mcp.managed_agents import (
    NON_CODEX_WRITE_UNAVAILABLE_REASON,
    ManagedAgentError,
    ManagedAgentLaunch,
    ManagedAgentManager,
    ManagedAgentUnavailableError,
    build_managed_launch,
    build_observe_launch,
    build_wsl_write_launch,
)


def _launch(tmp_path: Path, session_id: str, script: str) -> ManagedAgentLaunch:
    return ManagedAgentLaunch(
        session_id=session_id,
        agent_id="test-agent",
        display_name="Test Agent",
        role="reviewer",
        executable=Path(sys.executable),
        arguments=("-u", "-c", script),
        working_directory=tmp_path,
    )


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        exit_code = wintypes.DWORD()
        try:
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _trusted_acpx_test_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    if os.name == "nt":
        appdata = tmp_path / "appdata"
        root = appdata / "npm"
        monkeypatch.setenv("APPDATA", str(appdata))
        names = {
            "acpx-runtime": "acpx.cmd",
            "kimi-code": "kimi.cmd",
            "grok": "grok.cmd",
        }
    else:
        home = tmp_path / "home"
        root = home / ".npm-global" / "bin"
        monkeypatch.setenv("HOME", str(home))
        names = {"acpx-runtime": "acpx", "kimi-code": "kimi", "grok": "grok"}
    root.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(root))
    paths = {agent_id: root / name for agent_id, name in names.items()}
    for path in paths.values():
        path.write_text("reviewed test executable", encoding="ascii")
    return paths


def _agent_launch(
    tmp_path: Path, agent_id: str, script: str
) -> ManagedAgentLaunch:
    return ManagedAgentLaunch(
        session_id=f"{agent_id}-event-test",
        agent_id=agent_id,
        display_name=agent_id,
        role="reviewer",
        executable=Path(sys.executable),
        arguments=("-u", "-c", script),
        working_directory=tmp_path,
    )


def test_managed_session_redacts_streams_and_extracts_explicit_answer(
    tmp_path: Path,
) -> None:
    script = (
        "import json,sys; prompt=sys.stdin.read().strip(); "
        "marker=''.join(chr(value) for value in (115,107,45)); "
        "credential=marker+chr(65)*24; "
        "header=''.join(chr(value) for value in "
        "(97,117,116,104,111,114,105,122,97,116,105,111,110,58,32,"
        "66,101,97,114,101,114,32)); "
        "print(json.dumps({'type':'item.completed','item':"
        "{'type':'agent_message','text':'answer '+prompt}}), flush=True); "
        "print(header+credential, file=sys.stderr, flush=True)"
    )
    manager = ManagedAgentManager(max_sessions=3)
    session = manager.start(_launch(tmp_path, "codex-one", script), input_text="review")
    assert session.wait(15)
    snapshot = session.snapshot()
    assert snapshot["state"] == "completed"
    assert snapshot["reasoning_contract"] == "observable-output-only"
    encoded = json.dumps(snapshot)
    assert "sk-" not in encoded
    assert "[REDACTED]" in encoded
    answers = [event for event in snapshot["events"] if event["kind"] == "answer"]
    assert answers[0]["summary"] == "answer review"
    assert all("review" not in event["text"] for event in snapshot["events"] if event["stream"] == "system")


def test_managed_sessions_keep_stable_identity_when_completion_order_differs(
    tmp_path: Path,
) -> None:
    manager = ManagedAgentManager(max_sessions=3)
    slow = "import sys,time; sys.stdin.read(); time.sleep(.3); print('slow')"
    medium = "import sys,time; sys.stdin.read(); time.sleep(.15); print('medium')"
    fast = "import sys; sys.stdin.read(); print('fast')"
    first = manager.start(_launch(tmp_path, "first", slow), input_text="go")
    second = manager.start(_launch(tmp_path, "second", medium), input_text="go")
    third = manager.start(_launch(tmp_path, "third", fast), input_text="go")
    assert third.wait(15)
    assert second.wait(15)
    assert first.wait(15)
    snapshots = {item["session_id"]: item for item in manager.snapshots()}
    assert "slow" in json.dumps(snapshots["first"])
    assert "medium" not in json.dumps(snapshots["first"])
    assert "fast" not in json.dumps(snapshots["first"])
    assert "medium" in json.dumps(snapshots["second"])
    assert "slow" not in json.dumps(snapshots["second"])
    assert "fast" not in json.dumps(snapshots["second"])
    assert "fast" in json.dumps(snapshots["third"])
    assert "slow" not in json.dumps(snapshots["third"])
    assert "medium" not in json.dumps(snapshots["third"])


def test_provider_failure_cannot_be_hidden_by_zero_process_exit(tmp_path: Path) -> None:
    script = (
        "import json,sys; sys.stdin.read(); "
        "print(json.dumps({'type':'turn.failed'}), flush=True)"
    )
    manager = ManagedAgentManager(max_sessions=1)
    launch = ManagedAgentLaunch(
        session_id="provider-failed-zero-exit",
        agent_id="codex",
        display_name="Codex",
        role="reviewer",
        executable=Path(sys.executable),
        arguments=("-u", "-c", script),
        working_directory=tmp_path,
    )

    session = manager.start(launch, input_text="review")
    assert session.wait(15)
    snapshot = session.snapshot()

    assert snapshot["state"] == "completed"
    assert snapshot["return_code"] == 0
    assert snapshot["terminal_outcome"]["status"] == "conflict"
    assert snapshot["terminal_outcome"]["provider_status"] == "failed"


def test_managed_session_rejects_secret_input_and_duplicate_identity(
    tmp_path: Path,
) -> None:
    manager = ManagedAgentManager(max_sessions=2)
    script = "import sys,time; sys.stdin.read(); time.sleep(.2)"
    session = manager.start(_launch(tmp_path, "one", script))
    with pytest.raises(ManagedAgentError, match="credential-like"):
        session.submit("api_key=realistic-secret-value-123")
    with pytest.raises(ManagedAgentError, match="already exists"):
        manager.start(_launch(tmp_path, "one", script))
    before_input = session.snapshot()
    assert before_input["can_submit_input"] is True
    assert before_input["input_mode"] == "single"
    assert before_input["permission_tier"] == "observe"
    assert before_input["session_contract"] == {
        "mode": "one_shot",
        "input_transport": "stdin_once",
        "additional_input_supported": False,
        "resume_supported": False,
        "process_terminal_after_turn": True,
    }
    session.submit("safe task")
    assert session.snapshot()["can_submit_input"] is False
    assert session.wait(15)


def test_managed_session_omits_oversized_unbroken_line(tmp_path: Path) -> None:
    script = "print('X'*70000, flush=True)"
    manager = ManagedAgentManager(max_sessions=1)
    session = manager.start(_launch(tmp_path, "large", script))
    assert session.wait(15)
    snapshot = session.snapshot()
    encoded = json.dumps(snapshot)
    assert "line exceeded the bounded capture limit" in encoded
    assert "X" * 1000 not in encoded


def test_managed_session_stop_owns_and_reaps_child(tmp_path: Path) -> None:
    script = "import time; time.sleep(30)"
    manager = ManagedAgentManager(max_sessions=1)
    session = manager.start(_launch(tmp_path, "stop", script))
    time.sleep(0.1)
    session.stop()
    assert session.wait(15)
    assert session.snapshot()["state"] == "stopped"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_managed_session_natural_exit_reaps_descendant_tree(tmp_path: Path) -> None:
    python_executable = Path(getattr(sys, "_base_executable", sys.executable))
    child_pid_path = tmp_path / "managed-child.pid"
    parent_script = tmp_path / "managed-parent.py"
    parent_script.write_text(
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n",
        encoding="utf-8",
    )
    launch = ManagedAgentLaunch(
        session_id="descendant-exit",
        agent_id="test-agent",
        display_name="Test Agent",
        role="reviewer",
        executable=python_executable,
        arguments=(str(parent_script), str(child_pid_path)),
        working_directory=tmp_path,
    )
    manager = ManagedAgentManager(max_sessions=1)
    session = manager.start(launch)
    assert session.wait(15)
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    deadline = time.monotonic() + 3.0
    while _process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _process_alive(child_pid)


def test_reviewed_observe_profiles_never_put_prompt_on_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import peerbridge_mcp.managed_agents as managed_agents

    monkeypatch.setattr(
        managed_agents, "find_trusted_executable", lambda _spec: Path(sys.executable)
    )
    codex = build_observe_launch(
        "codex",
        session_id="codex-review",
        role="reviewer",
        working_directory=tmp_path,
    )
    claude = build_observe_launch(
        "claude-code",
        session_id="claude-review",
        role="reviewer",
        working_directory=tmp_path,
    )
    assert codex.arguments == (
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "-",
    )
    assert claude.arguments == (
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "plan",
        "--no-session-persistence",
    )
    assert codex.environment is not None
    assert claude.environment is not None
    assert "prompt" not in " ".join((*codex.arguments, *claude.arguments)).lower()


def test_review_permission_uses_the_same_read_only_launch_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import peerbridge_mcp.managed_agents as managed_agents

    monkeypatch.setattr(
        managed_agents, "find_trusted_executable", lambda _spec: Path(sys.executable)
    )
    launch = build_observe_launch(
        "codex",
        session_id="codex-read-only-review",
        role="reviewer",
        working_directory=tmp_path,
        permission_tier="review",
    )

    assert launch.execution_mode == "observe"
    assert launch.permission_tier == "review"
    assert "read-only" in launch.arguments
    assert "workspace-write" not in launch.arguments


def test_managed_official_profiles_strip_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import peerbridge_mcp.managed_agents as managed_agents

    monkeypatch.setenv("OPENAI_API_KEY", "openai-family-test-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-family-test-value")
    monkeypatch.setenv("XAI_API_KEY", "unrelated-grok-test-value")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-inherited")
    monkeypatch.setattr(
        managed_agents, "find_trusted_executable", lambda _spec: Path(sys.executable)
    )

    codex = build_observe_launch(
        "codex",
        session_id="codex-isolated-environment",
        role="reviewer",
        working_directory=tmp_path,
    )
    claude = build_observe_launch(
        "claude-code",
        session_id="claude-isolated-environment",
        role="reviewer",
        working_directory=tmp_path,
    )
    writer = build_managed_launch(
        "codex",
        session_id="codex-isolated-writer",
        role="implementer",
        working_directory=tmp_path,
        execution_mode="isolated-write",
        governance_binding_id="binding-environment",
        isolation_verified=True,
    )

    codex_environment = dict(codex.environment or ())
    claude_environment = dict(claude.environment or ())
    writer_environment = dict(writer.environment or ())
    assert "OPENAI_API_KEY" not in codex_environment
    assert "OPENAI_API_KEY" not in writer_environment
    assert "ANTHROPIC_API_KEY" not in codex_environment
    assert "ANTHROPIC_API_KEY" not in writer_environment
    assert "ANTHROPIC_API_KEY" not in claude_environment
    assert "OPENAI_API_KEY" not in claude_environment
    for environment in (codex_environment, claude_environment, writer_environment):
        assert "XAI_API_KEY" not in environment
        assert "UNRELATED_SECRET" not in environment


@pytest.mark.parametrize("agent_id", ["kimi-code", "grok"])
@pytest.mark.parametrize("missing", ["official", "acpx"])
def test_acpx_observe_profile_requires_both_trusted_runtimes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
    missing: str,
) -> None:
    import peerbridge_mcp.managed_agents as managed_agents

    paths = _trusted_acpx_test_paths(tmp_path, monkeypatch)

    def find(spec: object) -> Path | None:
        spec_id = str(getattr(spec, "agent_id", ""))
        if spec_id == agent_id and missing != "official":
            return paths[agent_id]
        if spec_id == "acpx-runtime" and missing != "acpx":
            return paths["acpx-runtime"]
        return None

    monkeypatch.setattr(managed_agents, "find_trusted_executable", find)
    expected = "official Agent CLI" if missing == "official" else "ACPX interoperability"
    with pytest.raises(ManagedAgentError, match=expected):
        build_observe_launch(
            agent_id,
            session_id=f"{agent_id}-review",
            role="reviewer",
            working_directory=tmp_path,
        )


def test_acpx_observe_profiles_strip_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import peerbridge_mcp.managed_agents as managed_agents

    paths = _trusted_acpx_test_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("KIMI_API_KEY", "kimi-family-test-value")
    monkeypatch.setenv("XAI_API_KEY", "grok-family-test-value")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-test-value")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-inherited")
    monkeypatch.setattr(
        managed_agents,
        "find_trusted_executable",
        lambda spec: paths.get(spec.agent_id),
    )

    kimi = build_observe_launch(
        "kimi-code",
        session_id="kimi-review",
        role="reviewer",
        working_directory=tmp_path,
    )
    grok = build_observe_launch(
        "grok",
        session_id="grok-review",
        role="reviewer",
        working_directory=tmp_path,
    )
    kimi_environment = dict(kimi.environment or ())
    grok_environment = dict(grok.environment or ())

    assert "KIMI_API_KEY" not in kimi_environment
    assert "XAI_API_KEY" not in kimi_environment
    assert "OPENAI_API_KEY" not in kimi_environment
    assert "UNRELATED_SECRET" not in kimi_environment
    assert "XAI_API_KEY" not in grok_environment
    assert "KIMI_API_KEY" not in grok_environment
    assert "OPENAI_API_KEY" not in grok_environment
    assert "UNRELATED_SECRET" not in grok_environment
    assert "kimi-family-test-value" not in repr(kimi)
    assert "grok-family-test-value" not in repr(grok)


@pytest.mark.parametrize(
    ("agent_id", "acpx_agent"),
    [("kimi-code", "kimi"), ("grok", "grok-build")],
)
def test_acpx_observe_command_is_bounded_and_prompt_stays_on_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
    acpx_agent: str,
) -> None:
    import peerbridge_mcp.managed_agents as managed_agents

    paths = _trusted_acpx_test_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(
        managed_agents,
        "find_trusted_executable",
        lambda spec: paths.get(spec.agent_id),
    )
    monkeypatch.setattr(managed_agents, "attach_process_tree", lambda _process: None)
    monkeypatch.setattr(managed_agents, "release_process_tree", lambda _process: None)
    launch = build_observe_launch(
        agent_id,
        session_id=f"{agent_id}-safe-command",
        role="reviewer",
        working_directory=tmp_path,
    )
    delivered = bytearray()
    input_closed = threading.Event()
    captured: dict[str, object] = {}

    class InputPipe:
        closed = False

        def write(self, value: bytes) -> int:
            delivered.extend(value)
            return len(value)

        @staticmethod
        def flush() -> None:
            return None

        def close(self) -> None:
            self.closed = True
            input_closed.set()

    class Process:
        pid = 43210
        stdin = InputPipe()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        @staticmethod
        def wait() -> int:
            assert input_closed.wait(5)
            return 0

    def popen(command: tuple[str, ...], **kwargs: object) -> Process:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Process()

    prompt = "PRIVATE OBSERVE TASK SENT ONLY THROUGH STDIN"
    manager = ManagedAgentManager(max_sessions=1)
    session = manager.start(launch, input_text=prompt, popen=popen)
    assert session.wait(10)

    command = tuple(captured["command"])
    kwargs = dict(captured["kwargs"])
    assert command[0] == str(paths["acpx-runtime"].resolve())
    assert command[-4:] == (acpx_agent, "exec", "-f", "-")
    assert prompt not in " ".join(command)
    assert delivered == (prompt + "\n").encode("utf-8")
    assert command[command.index("--max-turns") + 1] == "1"
    assert command[command.index("--prompt-retries") + 1] == "0"
    assert "--deny-all" in command
    assert "--no-fs" in command
    assert "--no-terminal" in command
    assert "--mcp-config" not in command
    assert "--approve-all" not in command
    assert kwargs["shell"] is False
    assert kwargs["env"] == dict(launch.environment or ())


def test_grok_acpx_events_project_answer_identity_usage_and_success(
    tmp_path: Path,
) -> None:
    events = [
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
                "models": {"currentModelId": "grok-4.6[fast]"},
            },
        },
        {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Grok observed answer"},
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
    script = (
        "import json,sys; sys.stdin.read(); events="
        + repr(events)
        + "; [print(json.dumps(event), flush=True) for event in events]"
    )
    manager = ManagedAgentManager(max_sessions=1)
    session = manager.start(
        _agent_launch(tmp_path, "grok", script), input_text="private room question"
    )
    assert session.wait(15)
    snapshot = session.snapshot()
    encoded = json.dumps(snapshot)

    assert snapshot["client_version"] == "1.0.1"
    assert snapshot["model_id"] == "grok-4.6"
    assert snapshot["observed_route"] == "grok-4.6[fast]"
    assert snapshot["usage"]["status"] == "reported"
    assert snapshot["usage"]["total_tokens"] == 10
    assert snapshot["terminal_outcome"]["status"] == "completed"
    assert snapshot["terminal_outcome"]["provider_status"] == "completed"
    answers = [event for event in snapshot["events"] if event["kind"] == "answer"]
    assert answers[0]["summary"] == "Grok observed answer"
    assert answers[0]["text"] == "Grok observed answer"
    assert "private room question" not in encoded
    assert '"private": true' not in encoded


def test_kimi_acpx_error_is_sanitized_and_cannot_look_successful(
    tmp_path: Path,
) -> None:
    events = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {"agentInfo": {"name": "kimi", "version": "1.2.3"}},
        },
        {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"models": {"currentModelId": "kimi-k3"}},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {
                "code": -32000,
                "message": "private upstream detail must-not-be-retained",
            },
        },
    ]
    script = (
        "import json,sys; sys.stdin.read(); events="
        + repr(events)
        + "; [print(json.dumps(event), flush=True) for event in events]"
    )
    manager = ManagedAgentManager(max_sessions=1)
    session = manager.start(
        _agent_launch(tmp_path, "kimi-code", script), input_text="review safely"
    )
    assert session.wait(15)
    snapshot = session.snapshot()
    encoded = json.dumps(snapshot)

    assert snapshot["state"] == "completed"
    assert snapshot["client_version"] == "1.2.3"
    assert snapshot["model_id"] == "kimi-k3"
    assert snapshot["terminal_outcome"]["status"] == "conflict"
    assert snapshot["terminal_outcome"]["provider_status"] == "failed"
    assert snapshot["terminal_outcome"]["provider_reason"] == "session/prompt.error"
    assert "private upstream detail" not in encoded
    assert "must-not-be-retained" not in encoded


def test_isolated_write_profile_requires_verified_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import peerbridge_mcp.managed_agents as managed_agents

    monkeypatch.setattr(
        managed_agents, "find_trusted_executable", lambda _spec: Path(sys.executable)
    )
    with pytest.raises(ManagedAgentError, match="verified governance binding"):
        build_managed_launch(
            "codex",
            session_id="unverified-writer",
            role="implementer",
            working_directory=tmp_path,
            execution_mode="isolated-write",
        )
    launch = build_managed_launch(
        "codex",
        session_id="verified-writer",
        role="implementer",
        working_directory=tmp_path,
        execution_mode="isolated-write",
        governance_binding_id="binding-one",
        isolation_verified=True,
    )
    assert "workspace-write" in launch.arguments
    assert "dangerously" not in " ".join(launch.arguments)
    assert launch.execution_mode == "isolated-write"
    assert launch.governance_binding_id == "binding-one"
    assert launch.isolation_boundary == "codex-workspace-write-v1"
    with pytest.raises(
        ManagedAgentUnavailableError, match="no reviewed launch profile"
    ) as unavailable:
        build_managed_launch(
            "claude-code",
            session_id="unsupported-writer",
            role="implementer",
            working_directory=tmp_path,
            execution_mode="isolated-write",
            governance_binding_id="binding-two",
            isolation_verified=True,
        )
    assert unavailable.value.reason == NON_CODEX_WRITE_UNAVAILABLE_REASON

    with pytest.raises(ManagedAgentUnavailableError) as unavailable_without_binding:
        build_managed_launch(
            "claude-code",
            session_id="unsupported-writer-without-binding",
            role="implementer",
            working_directory=tmp_path,
            execution_mode="isolated-write",
        )
    assert (
        unavailable_without_binding.value.unavailable_reason
        == NON_CODEX_WRITE_UNAVAILABLE_REASON
    )


@pytest.mark.parametrize("agent_id", ("claude-code", "grok", "kimi-code"))
@pytest.mark.parametrize("permission_tier", ("edit", "full-development"))
def test_non_codex_wsl_write_profiles_require_a_credential_broker(
    tmp_path: Path,
    agent_id: str,
    permission_tier: str,
) -> None:
    with pytest.raises(ManagedAgentUnavailableError) as unavailable:
        build_wsl_write_launch(
            agent_id,
            session_id=f"blocked-{agent_id}-{permission_tier}",
            role="implementer",
            working_directory=tmp_path,
            permission_tier=permission_tier,  # type: ignore[arg-type]
            requested_route=None,
            governance_binding_id="binding-credential-broker",
        )

    assert unavailable.value.reason == "credential_broker_required"
    assert unavailable.value.reason_code == NON_CODEX_WRITE_UNAVAILABLE_REASON


def test_wait_returns_after_observable_output_is_drained(tmp_path: Path) -> None:
    script = "import sys; [print(f'line-{value}') for value in range(200)]"
    manager = ManagedAgentManager(max_sessions=1)
    session = manager.start(_launch(tmp_path, "drain", script))

    assert session.wait(15)
    assert "line-199" in json.dumps(session.snapshot())


def test_manager_prunes_oldest_terminal_sessions(tmp_path: Path) -> None:
    manager = ManagedAgentManager(max_sessions=1, max_retained_sessions=2)
    for session_id in ("first", "second", "third"):
        session = manager.start(_launch(tmp_path, session_id, "print('done')"))
        assert session.wait(15)

    assert [row["session_id"] for row in manager.snapshots()] == ["second", "third"]


def test_manager_close_rejects_future_processes(tmp_path: Path) -> None:
    manager = ManagedAgentManager(max_sessions=1)
    manager.close()

    with pytest.raises(ManagedAgentError, match="manager is closed"):
        manager.start(_launch(tmp_path, "late", "print('must not run')"))


def test_manager_snapshots_are_incremental_by_stable_session_id(tmp_path: Path) -> None:
    manager = ManagedAgentManager(max_sessions=1)
    session = manager.start(_launch(tmp_path, "incremental", "print('one')"))
    assert session.wait(15)
    complete = manager.snapshots()[0]

    incremental = manager.snapshots(
        after_sequences={"incremental": complete["latest_sequence"]}
    )[0]

    assert incremental["session_id"] == "incremental"
    assert incremental["state"] == "completed"
    assert incremental["events"] == []
