from __future__ import annotations

import base64
import io
import json
import threading
from pathlib import Path

import pytest
import peerbridge_mcp.official_agent_runtime as runtime_module

from peerbridge_mcp.attachments import stage_chat_attachment_payloads
from peerbridge_mcp.official_agent_runtime import (
    ClaudeStreamSession,
    CodexAppServerSession,
    GrokAcpSession,
    HybridManagedAgentManager,
    KimiAcpSession,
    ManagedAgentError,
)
from peerbridge_mcp.multimodal import acp_native_turn_payload, create_vision_challenge
from tests._image_fixtures import PNG


def _session_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "session_id": "official-runtime-test",
        "role": "reviewer",
        "working_directory": tmp_path,
        "requested_route": None,
        "permission_tier": "review",
        "governance_binding_id": None,
        "project_root": tmp_path,
    }


def _staged_evidence(tmp_path: Path):
    return stage_chat_attachment_payloads(
        tmp_path,
        (
            ("chart.png", PNG),
            ("notes.txt", b"Inspect the visible breakout structure."),
        ),
    )


def test_codex_uses_native_local_image_and_sanitized_transport_receipt(
    tmp_path: Path,
) -> None:
    staged = _staged_evidence(tmp_path)
    session = CodexAppServerSession(**_session_kwargs(tmp_path))
    session._state = "running"
    session._thread_id = "thread-test"
    calls: list[tuple[str, dict[str, object]]] = []

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"turn": {"id": "turn-test"}}

    session._request = request  # type: ignore[method-assign]
    session.submit("Review this evidence.", attachments=staged)

    method, params = calls[-1]
    assert method == "turn/start"
    turn_input = params["input"]
    assert isinstance(turn_input, list)
    assert turn_input[0]["type"] == "text"
    assert "PeerBridge verified attachments" in turn_input[0]["text"]
    assert "notes.txt" not in turn_input[0]["text"]
    assert ".peerbridge-artifacts" in turn_input[0]["text"]
    assert turn_input[1] == {
        "type": "localImage",
        "path": str((tmp_path / staged[0].relative_path).resolve()),
    }
    assert params["approvalPolicy"] == "never"
    assert params["sandboxPolicy"] == {
        "type": "readOnly",
        "networkAccess": False,
    }

    snapshot = session.snapshot()
    receipt = snapshot["attachment_delivery_receipts"][0]
    assert receipt["delivery_mode"] == "native_local_image_and_verified_path"
    assert receipt["status"] == "transport_accepted"
    assert receipt["attachment_count"] == 2
    assert receipt["model_view_confirmed"] is False
    assert str(tmp_path) not in json.dumps(receipt, sort_keys=True)


def test_governed_codex_write_session_requires_binding_and_uses_workspace_policy(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    worktree = tmp_path / "isolated-worktree"
    project.mkdir()
    worktree.mkdir()
    kwargs = {
        "session_id": "governed-codex-edit",
        "role": "implementer",
        "working_directory": worktree,
        "requested_route": "gpt-test",
        "permission_tier": "edit",
        "governance_binding_id": "binding-edit-1",
        "project_root": project,
    }

    session = CodexAppServerSession(**kwargs)

    assert session._sandbox_name() == "workspace-write"
    assert session._approval_policy() == "on-request"
    assert session._sandbox_policy() == {
        "type": "workspaceWrite",
        "writableRoots": [str(worktree.resolve())],
        "networkAccess": True,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }
    assert session.snapshot()["execution_mode"] == "edit"
    without_binding = dict(kwargs)
    without_binding["governance_binding_id"] = None
    with pytest.raises(ManagedAgentError, match="requires a governed worktree"):
        CodexAppServerSession(**without_binding)


def test_claude_standard_agent_keeps_network_tools_without_preapproving_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"stub")
    captured: dict[str, object] = {}

    class NoopThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            return

    def spawn(command: tuple[str, ...], **kwargs: object) -> object:
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return object()

    monkeypatch.setattr(runtime_module, "find_trusted_executable", lambda _spec: executable)
    monkeypatch.setattr(
        runtime_module, "_official_child_environment", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(runtime_module, "_spawn_owned", spawn)
    monkeypatch.setattr(runtime_module.threading, "Thread", NoopThread)

    session = ClaudeStreamSession(
        session_id="claude-standard-agent",
        role="implementer",
        working_directory=tmp_path,
        requested_route="sonnet",
        permission_tier="edit",
        governance_binding_id="binding-standard-agent",
        project_root=tmp_path,
    )
    session.start()

    command = tuple(captured["command"])
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    available = command[command.index("--tools") + 1]
    auto_allowed = command[command.index("--allowedTools") + 1]
    assert "WebFetch" in available
    assert "WebSearch" in available
    assert "Bash" not in available
    assert "Bash" not in auto_allowed
    assert captured["cwd"] == tmp_path.resolve()


def test_claude_full_access_bypasses_per_turn_prompts_only_inside_bound_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"stub")
    captured: dict[str, object] = {}

    class NoopThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            return

    def spawn(command: tuple[str, ...], **kwargs: object) -> object:
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return object()

    monkeypatch.setattr(runtime_module, "find_trusted_executable", lambda _spec: executable)
    monkeypatch.setattr(runtime_module, "_official_child_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime_module, "_spawn_owned", spawn)
    monkeypatch.setattr(runtime_module.threading, "Thread", NoopThread)

    session = ClaudeStreamSession(
        session_id="claude-full-access",
        role="implementer",
        working_directory=tmp_path,
        requested_route="fable",
        permission_tier="full-development",
        governance_binding_id="binding-full-access",
        project_root=tmp_path,
    )
    session.start()

    command = tuple(captured["command"])
    mode_index = command.index("--permission-mode")
    assert command[mode_index + 1] == "bypassPermissions"
    assert "--allow-dangerously-skip-permissions" in command
    assert "Bash" in command[command.index("--allowedTools") + 1]
    assert captured["cwd"] == tmp_path.resolve()


@pytest.mark.parametrize("session_type", (CodexAppServerSession, ClaudeStreamSession))
def test_streaming_official_runtime_terminates_on_cumulative_output_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_type: type[CodexAppServerSession] | type[ClaudeStreamSession],
) -> None:
    session = session_type(**_session_kwargs(tmp_path))
    session._provider_output_bytes = runtime_module.MAX_PROVIDER_OUTPUT_BYTES - 1

    class Process:
        stdout = io.BytesIO(b"{}\n")
        stderr = io.BytesIO()

    process = Process()
    terminated: list[object] = []
    monkeypatch.setattr(
        runtime_module,
        "terminate_process_tree",
        lambda candidate, **_kwargs: terminated.append(candidate),
    )

    session._read_stdout(process, 0)  # type: ignore[arg-type]

    assert terminated == [process]
    snapshot = session.snapshot()
    assert snapshot["provider_output_limit_exceeded"] is True
    assert snapshot["provider_output_bytes"] > snapshot["provider_output_limit_bytes"]
    assert any(
        "bounded provider output budget" in event["text"]
        for event in snapshot["events"]
    )


def test_acp_native_bridge_uses_bounded_cancellable_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = GrokAcpSession(**_session_kwargs(tmp_path))
    node = tmp_path / "node.exe"
    helper = tmp_path / "bridge.mjs"
    runtime = tmp_path / "runtime.js"
    state = tmp_path / "state"
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        session,
        "_runtime_bridge",
        lambda: (node, helper, runtime, state, {"PATH": str(tmp_path)}),
    )

    def bounded(command, **kwargs):
        captured["command"] = tuple(command)
        captured.update(kwargs)
        return 0, b'{"type":"done","status":"ready"}\n', b""

    monkeypatch.setattr(runtime_module, "_bounded_acp_process", bounded)

    cancel_event = threading.Event()
    result = session._run_bridge(
        {"operation": "ensure"},
        timeout=7.0,
        cancel_event=cancel_event,
    )

    assert result[0] == 0
    assert captured["command"] == (str(node), str(helper))
    assert captured["timeout_seconds"] == 7.0
    assert captured["max_capture_bytes"] == runtime_module.MAX_ACP_CAPTURE_BYTES
    assert captured["cancel_event"] is cancel_event
    assert json.loads(str(captured["stdin_text"]))["operation"] == "ensure"


def test_acp_interrupt_keeps_exact_worker_busy_and_prevents_resend_overlap(
    tmp_path: Path,
) -> None:
    session = GrokAcpSession(**_session_kwargs(tmp_path))
    session._state = "running"
    entered = (threading.Event(), threading.Event())
    release = (threading.Event(), threading.Event())
    seen_cancel_events: list[threading.Event] = []
    counter_lock = threading.Lock()
    active_workers = 0
    max_active_workers = 0

    def blocked_bridge(
        _request: object,
        *,
        _timeout: float = 210.0,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, bytes, bytes]:
        nonlocal active_workers, max_active_workers
        assert cancel_event is not None
        with counter_lock:
            index = len(seen_cancel_events)
            seen_cancel_events.append(cancel_event)
            active_workers += 1
            max_active_workers = max(max_active_workers, active_workers)
        entered[index].set()
        try:
            if not release[index].wait(5):
                raise RuntimeError("test worker was not released")
            if cancel_event.is_set():
                raise ManagedAgentError("bounded ACP runner was cancelled")
            return 0, b'{"type":"done","status":"completed"}\n', b""
        finally:
            with counter_lock:
                active_workers -= 1

    session._run_bridge = blocked_bridge  # type: ignore[method-assign]
    session.submit("first turn")
    assert entered[0].wait(5)
    first_worker = session._acp_worker
    first_cancel_event = session._acp_cancel_event
    first_generation = session._acp_turn_generation
    assert first_worker is not None
    assert first_cancel_event is not None

    session.interrupt()

    assert first_cancel_event.is_set()
    assert session._busy is True
    assert session._acp_worker is first_worker
    assert session.snapshot()["can_submit_input"] is False
    with pytest.raises(ManagedAgentError, match="cannot accept input now"):
        session.submit("overlapping turn")
    assert len(seen_cancel_events) == 1

    release[0].set()
    first_worker.join(5)
    assert not first_worker.is_alive()
    assert session._busy is False
    assert session._acp_worker is None
    assert session.snapshot()["can_submit_input"] is True

    session.submit("second turn")
    assert entered[1].wait(5)
    second_worker = session._acp_worker
    second_cancel_event = session._acp_cancel_event
    assert second_worker is not None
    assert second_cancel_event is not None
    assert session._acp_turn_generation == first_generation + 1
    assert second_cancel_event is not first_cancel_event
    assert second_cancel_event.is_set() is False
    assert max_active_workers == 1

    release[1].set()
    second_worker.join(5)
    assert not second_worker.is_alive()
    assert session.snapshot()["can_submit_input"] is True


def test_acp_stop_stays_busy_and_nonterminal_until_exact_worker_exits(
    tmp_path: Path,
) -> None:
    session = GrokAcpSession(**_session_kwargs(tmp_path))
    session._state = "running"
    entered = threading.Event()
    release = threading.Event()

    def blocked_bridge(
        _request: object,
        *,
        _timeout: float = 210.0,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, bytes, bytes]:
        assert cancel_event is not None
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test worker was not released")
        if cancel_event.is_set():
            raise ManagedAgentError("bounded ACP runner was cancelled")
        return 0, b'{"type":"done","status":"completed"}\n', b""

    session._run_bridge = blocked_bridge  # type: ignore[method-assign]
    session.submit("turn to stop")
    assert entered.wait(5)
    worker = session._acp_worker
    cancel_event = session._acp_cancel_event
    assert worker is not None
    assert cancel_event is not None

    session.stop()

    assert cancel_event.is_set()
    assert session._state == "stopping"
    assert session._busy is True
    assert session._acp_worker is worker
    assert session.wait(0) is False
    with pytest.raises(ManagedAgentError, match="cannot accept input now"):
        session.submit("overlapping turn")

    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert session._state == "stopped"
    assert session._busy is False
    assert session._acp_worker is None
    assert session._acp_cancel_event is None
    assert session.wait(0) is True


def test_acp_stale_generation_finalizer_cannot_release_current_worker(
    tmp_path: Path,
) -> None:
    session = GrokAcpSession(**_session_kwargs(tmp_path))
    session._state = "running"
    session._busy = True
    session._acp_turn_generation = 2
    current_cancel_event = threading.Event()
    current_worker = threading.Thread()
    session._acp_cancel_event = current_cancel_event
    session._acp_worker = current_worker
    challenge = create_vision_challenge(code="246789")
    session._pending_vision_challenge = challenge
    session._run_bridge = (  # type: ignore[method-assign]
        lambda _request, *, cancel_event: (
            0,
            b'{"type":"done","status":"completed"}\n',
            b"",
        )
    )

    session._prompt_worker(
        "stale turn",
        (),
        (),
        1,
        current_cancel_event,
        current_worker,
    )

    assert session._busy is True
    assert session._acp_turn_generation == 2
    assert session._acp_cancel_event is current_cancel_event
    assert session._acp_worker is current_worker
    assert session._pending_vision_challenge is challenge
    assert not any(
        "turn completed" in event["text"] for event in session.snapshot()["events"]
    )


def test_claude_stream_delivers_native_multimodal_blocks_without_false_view_claim(
    tmp_path: Path,
) -> None:
    staged = _staged_evidence(tmp_path)
    session = ClaudeStreamSession(**_session_kwargs(tmp_path))
    stdin = io.BytesIO()
    session._state = "running"
    session._process = type("Process", (), {"stdin": stdin})()

    session.submit("Review this evidence.", attachments=staged)

    event = json.loads(stdin.getvalue().decode("utf-8"))
    blocks = event["message"]["content"]
    assert [block["type"] for block in blocks] == ["text", "image", "text"]
    assert blocks[0]["text"] == "Review this evidence."
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/png"
    assert ".peerbridge-artifacts/chat/" in blocks[2]["text"]
    assert "Inspect the visible breakout structure." in blocks[2]["text"]
    assert str(tmp_path) not in json.dumps(blocks)
    snapshot = session.snapshot()
    assert snapshot["session_contract"]["input_transport"] == "ndjson"
    assert snapshot["multimodal_capability"] == {
        "attachment_input_supported": True,
        "image_input_supported": True,
        "image_input": "native_image_base64",
        "audio_input_supported": False,
        "audio_input": "unavailable",
        "text_file_input": "bounded_verified_text_inline",
        "model_view_confirmation": False,
        "semantic_image_verification": "available",
    }
    receipt = snapshot["attachment_delivery_receipts"][0]
    assert receipt["delivery_mode"] == "native_image_base64_and_bounded_text_inline"
    assert receipt["status"] == "transport_accepted"
    assert receipt["model_view_confirmed"] is False


def test_codex_semantic_vision_probe_requires_model_answer_and_records_receipt(
    tmp_path: Path,
) -> None:
    session = CodexAppServerSession(**_session_kwargs(tmp_path))
    session._state = "running"
    session._thread_id = "thread-vision-test"
    calls: list[tuple[str, dict[str, object]]] = []

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"turn": {"id": "turn-vision-test"}}

    session._request = request  # type: ignore[method-assign]
    challenge_id = session.start_vision_probe()
    challenge = session._pending_vision_challenge

    assert challenge is not None
    assert challenge.challenge_id == challenge_id
    assert calls[-1][0] == "turn/start"
    turn_input = calls[-1][1]["input"]
    assert isinstance(turn_input, list)
    assert [item["type"] for item in turn_input] == ["text", "localImage"]
    assert challenge.expected_code not in turn_input[0]["text"]
    assert Path(turn_input[1]["path"]).is_file()

    session._handle_notification(
        "item/agentMessage/delta",
        {"delta": challenge.expected_response},
        session._generation,
    )
    session._handle_notification("turn/completed", {}, session._generation)

    snapshot = session.snapshot()
    receipt = snapshot["vision_verification_receipts"][-1]
    assert receipt["challenge_id"] == challenge_id
    assert receipt["status"] == "semantic_image_verified"
    assert receipt["model_view_confirmed"] is True
    assert snapshot["multimodal_capability"]["model_view_confirmation"] is True
    assert (
        snapshot["multimodal_capability"]["semantic_image_verification"]
        == "semantic_image_verified"
    )
    assert challenge.expected_code not in json.dumps(receipt, sort_keys=True)


def test_codex_usage_limit_is_visible_without_provider_message(
    tmp_path: Path,
) -> None:
    session = CodexAppServerSession(**_session_kwargs(tmp_path))
    session._state = "running"
    session._busy = True
    session._turn_id = "turn-limited"

    session._handle_notification(
        "error",
        {
            "error": {
                "codexErrorInfo": "usageLimitExceeded",
                "message": "private provider detail must not be retained",
            }
        },
        session._generation,
    )

    encoded = json.dumps(session.snapshot())
    assert "Codex turn failed (usage limit reached)." in encoded
    assert "private provider detail" not in encoded


@pytest.mark.parametrize(
    ("session_type", "expected_agent"),
    (
        (GrokAcpSession, "grok"),
        (KimiAcpSession, "kimi-code"),
    ),
)
def test_acp_sessions_prepare_native_content_without_false_visual_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_type: type[GrokAcpSession] | type[KimiAcpSession],
    expected_agent: str,
) -> None:
    staged = _staged_evidence(tmp_path)
    session = session_type(**_session_kwargs(tmp_path))
    captured: list[tuple[str, tuple[dict[str, str], ...], tuple[object, ...]]] = []

    class ImmediateThread:
        def __init__(self, *, target, args, name, daemon):
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr(
        "peerbridge_mcp.official_agent_runtime.threading.Thread", ImmediateThread
    )

    def capture(payload, native, verified, *_ownership) -> None:
        captured.append((payload, native, verified))

    session._prompt_worker = capture  # type: ignore[method-assign]
    session._state = "running"
    session._acp_prompt_capabilities["image"] = True
    session.submit("Review this evidence.", attachments=staged)

    assert len(captured) == 1
    payload, native, verified = captured[0]
    assert "PeerBridge verified text attachment" in payload
    assert "Inspect the visible breakout structure." in payload
    assert "chart.png" not in payload
    assert str(tmp_path) not in payload
    assert len(native) == 1
    assert native[0]["mediaType"] == "image/png"
    assert base64.b64decode(native[0]["data"], validate=True) == PNG
    assert len(verified) == 2
    snapshot = session.snapshot()
    receipt = snapshot["attachment_delivery_receipts"][0]
    assert receipt["provider_id"] == expected_agent
    assert receipt["delivery_mode"] == "native_acp_binary_content_block"
    assert receipt["status"] == "native_content_prepared"
    assert receipt["model_view_confirmed"] is False
    assert snapshot["multimodal_capability"] == {
        "attachment_input_supported": True,
        "image_input_supported": True,
        "image_input": "native_acp_binary_content_block",
        "audio_input_supported": False,
        "audio_input": "unavailable",
        "text_file_input": "bounded_verified_text_inline",
        "model_view_confirmation": False,
        "semantic_image_verification": "available",
    }


def test_acp_runtime_worker_requires_semantic_image_answer_for_view_receipt(
    tmp_path: Path,
) -> None:
    challenge = create_vision_challenge(code="246789")
    staged = stage_chat_attachment_payloads(
        tmp_path,
        (("peerbridge-vision-check.png", challenge.png),),
    )
    session = GrokAcpSession(**_session_kwargs(tmp_path))
    session._state = "running"
    session._acp_prompt_capabilities["image"] = True
    session._busy = True
    session._pending_vision_challenge = challenge
    verified = session._verify_attachments(staged)
    payload, native = acp_native_turn_payload(challenge.prompt, verified)
    events = (
        {"type": "session", "status": {"currentModelId": "grok-vision-test"}},
        {
            "type": "transport",
            "status": "native_acp_content_submitted",
            "attachmentCount": 1,
        },
        {
            "type": "status",
            "usage": {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15},
        },
        {"type": "text_delta", "text": "VISION:"},
        {"type": "text_delta", "text": "246789"},
        {"type": "done", "status": "completed"},
    )
    stdout = b"".join(
        json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"
        for event in events
    )

    session._run_bridge = (  # type: ignore[method-assign]
        lambda request, *, cancel_event: (0, stdout, b"")
    )
    cancel_event = threading.Event()
    worker = threading.current_thread()
    session._acp_turn_generation = 1
    session._acp_cancel_event = cancel_event
    session._acp_worker = worker
    session._prompt_worker(payload, native, verified, 1, cancel_event, worker)

    snapshot = session.snapshot()
    assert snapshot["model_id"] == "grok-vision-test"
    assert snapshot["usage"]["input_tokens"] == 12
    assert snapshot["usage"]["output_tokens"] == 3
    assert snapshot["attachment_delivery_receipts"][-1]["status"] == (
        "native_acp_content_submitted"
    )
    assert snapshot["vision_verification_receipts"][-1]["status"] == (
        "semantic_image_verified"
    )
    assert snapshot["multimodal_capability"]["model_view_confirmation"] is True
    serialized = json.dumps(snapshot, sort_keys=True)
    assert native[0]["data"] not in serialized
    assert "246789" not in serialized


def test_acp_runtime_worker_closes_vision_probe_after_unexpected_failure(
    tmp_path: Path,
) -> None:
    challenge = create_vision_challenge(code="246789")
    staged = stage_chat_attachment_payloads(
        tmp_path,
        (("peerbridge-vision-check.png", challenge.png),),
    )
    session = GrokAcpSession(**_session_kwargs(tmp_path))
    session._state = "running"
    session._acp_prompt_capabilities["image"] = True
    session._busy = True
    session._pending_vision_challenge = challenge
    verified = session._verify_attachments(staged)
    payload, native = acp_native_turn_payload(challenge.prompt, verified)

    def fail_unexpectedly(
        _request: object,
        *,
        cancel_event: threading.Event,
    ) -> tuple[int, bytes, bytes]:
        del cancel_event
        raise RuntimeError("private transport detail")

    session._run_bridge = fail_unexpectedly  # type: ignore[method-assign]
    cancel_event = threading.Event()
    worker = threading.current_thread()
    session._acp_turn_generation = 1
    session._acp_cancel_event = cancel_event
    session._acp_worker = worker
    session._prompt_worker(payload, native, verified, 1, cancel_event, worker)

    snapshot = session.snapshot()
    assert snapshot["can_submit_input"] is True
    assert snapshot["multimodal_capability"]["semantic_image_verification"] == (
        "semantic_image_runtime_failed"
    )
    assert snapshot["vision_verification_receipts"][-1]["model_view_confirmed"] is False
    serialized = json.dumps(snapshot, sort_keys=True)
    assert "semantic_image_pending" not in serialized
    assert "private transport detail" not in serialized


def test_acp_session_rejects_image_without_starting_a_turn_when_runtime_disallows_it(
    tmp_path: Path,
) -> None:
    staged = _staged_evidence(tmp_path)
    session = GrokAcpSession(**_session_kwargs(tmp_path))
    session._state = "running"
    session._acp_prompt_capabilities = {
        "image": False,
        "audio": False,
        "embeddedContext": True,
    }

    with pytest.raises(Exception, match="does not advertise image input"):
        session.submit("Review this evidence.", attachments=staged)

    snapshot = session.snapshot()
    assert snapshot["multimodal_capability"] == {
        "attachment_input_supported": True,
        "image_input_supported": False,
        "image_input": "unavailable",
        "audio_input_supported": False,
        "audio_input": "unavailable",
        "text_file_input": "bounded_verified_text_inline",
        "model_view_confirmation": False,
        "semantic_image_verification": "unsupported_by_agent_runtime",
    }
    assert snapshot["attachment_delivery_receipts"] == []


def test_acp_session_delivers_audio_only_when_runtime_advertises_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = b"RIFF" + (8).to_bytes(4, "little") + b"WAVE" + b"data"
    staged = stage_chat_attachment_payloads(tmp_path, (("voice.wav", audio),))
    session = GrokAcpSession(**_session_kwargs(tmp_path))
    captured: list[tuple[str, tuple[dict[str, str], ...], tuple[object, ...]]] = []

    class ImmediateThread:
        def __init__(self, *, target, args, name, daemon):
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr(
        "peerbridge_mcp.official_agent_runtime.threading.Thread", ImmediateThread
    )
    session._prompt_worker = (  # type: ignore[method-assign]
        lambda payload, native, verified, *_ownership: captured.append(
            (payload, native, verified)
        )
    )
    session._state = "running"
    session._acp_prompt_capabilities["audio"] = True

    session.submit("Transcribe this evidence.", attachments=staged)

    assert len(captured) == 1
    payload, native, verified = captured[0]
    assert payload == "Transcribe this evidence."
    assert native[0]["mediaType"] == "audio/wav"
    assert base64.b64decode(native[0]["data"], validate=True) == audio
    assert len(verified) == 1
    snapshot = session.snapshot()
    assert snapshot["multimodal_capability"]["audio_input_supported"] is True
    assert snapshot["multimodal_capability"]["audio_input"] == (
        "native_acp_audio_content_block"
    )
    assert snapshot["attachment_delivery_receipts"][0]["delivery_mode"] == (
        "native_acp_audio_content_block"
    )


def test_acp_session_rejects_audio_without_starting_a_turn_when_runtime_disallows_it(
    tmp_path: Path,
) -> None:
    audio = b"RIFF" + (8).to_bytes(4, "little") + b"WAVE" + b"data"
    staged = stage_chat_attachment_payloads(tmp_path, (("voice.wav", audio),))
    session = GrokAcpSession(**_session_kwargs(tmp_path))
    session._state = "running"

    with pytest.raises(ManagedAgentError, match="does not advertise audio input"):
        session.submit("Transcribe this evidence.", attachments=staged)

    assert session.snapshot()["attachment_delivery_receipts"] == []


@pytest.mark.parametrize("session_type", (CodexAppServerSession, ClaudeStreamSession))
def test_non_audio_official_sessions_fail_fast_before_transport(
    tmp_path: Path,
    session_type: type[CodexAppServerSession] | type[ClaudeStreamSession],
) -> None:
    audio = b"RIFF" + (8).to_bytes(4, "little") + b"WAVE" + b"data"
    staged = stage_chat_attachment_payloads(tmp_path, (("voice.wav", audio),))
    session = session_type(**_session_kwargs(tmp_path))

    with pytest.raises(ManagedAgentError, match="does not advertise native audio"):
        session.submit("Transcribe this evidence.", attachments=staged)

    assert session.snapshot()["attachment_delivery_receipts"] == []


def test_acp_session_start_surfaces_sanitized_authentication_failure(
    tmp_path: Path,
) -> None:
    session = KimiAcpSession(**_session_kwargs(tmp_path))
    stdout = b'{"type":"error","code":-32000,"message":"Authentication required"}\n'
    session._run_bridge = (  # type: ignore[method-assign]
        lambda request, timeout, cancel_event: (1, stdout, b"")
    )

    with pytest.raises(
        Exception,
        match="session could not be created: Authentication required",
    ):
        session.start()


def test_hybrid_manager_exposes_all_four_official_persistent_runtimes() -> None:
    assert HybridManagedAgentManager._SESSION_TYPES == {
        "codex": CodexAppServerSession,
        "claude-code": ClaudeStreamSession,
        "grok": GrokAcpSession,
        "kimi-code": KimiAcpSession,
    }


@pytest.mark.parametrize("agent_id", ("claude-code", "grok", "kimi-code"))
@pytest.mark.parametrize("permission_tier", ("edit", "full-development"))
def test_non_codex_write_tiers_use_the_official_persistent_runtime(
    tmp_path: Path,
    agent_id: str,
    permission_tier: str,
) -> None:
    captured: dict[str, object] = {}

    class NativeSession:
        supports_verified_attachments = True

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.working_directory = Path(kwargs["working_directory"])

        def start(self) -> None:
            captured["started"] = True

        def submit(self, text: str, *, attachments: object = ()) -> None:
            captured["input_text"] = text
            captured["attachments"] = tuple(attachments)

    manager = HybridManagedAgentManager(wsl_write_builder=None)
    manager._SESSION_TYPES = {agent_id: NativeSession}  # type: ignore[assignment]
    session = manager.start_official(
        agent_id=agent_id,
        session_id=f"native-{agent_id}-{permission_tier}",
        role="implementer",
        working_directory=tmp_path,
        permission_tier=permission_tier,  # type: ignore[arg-type]
        governance_binding_id="binding-1",
        project_root=tmp_path,
        input_text="Work only inside the governed worktree.",
        attachments=(),
    )

    assert isinstance(session, NativeSession)
    assert captured["started"] is True
    assert captured["permission_tier"] == permission_tier
    assert captured["governance_binding_id"] == "binding-1"
    assert captured["input_text"] == "Work only inside the governed worktree."


def test_hybrid_manager_applies_one_active_limit_across_native_and_fallback(
    tmp_path: Path,
) -> None:
    class Fallback:
        def snapshots(self, **_kwargs: object) -> list[dict[str, object]]:
            return [{"session_id": "fallback-running", "state": "running"}]

        def prune_one_terminal(self) -> bool:
            return False

        def stop_all(self) -> None:
            return

        def close(self) -> None:
            return

        def get(self, _session_id: str) -> object:
            raise ManagedAgentError("missing")

        def start(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("fallback start must not bypass the combined limit")

    manager = HybridManagedAgentManager(
        max_sessions=1,
        max_retained_sessions=2,
        fallback=Fallback(),  # type: ignore[arg-type]
    )

    with pytest.raises(ManagedAgentError, match="session limit reached"):
        manager.start_official(
            agent_id="codex",
            session_id="native-must-not-start",
            role="reviewer",
            working_directory=tmp_path,
            permission_tier="review",
            project_root=tmp_path,
        )


@pytest.mark.parametrize(
    ("family", "override"),
    (
        ("codex", "OPENAI_BASE_URL"),
        ("claude", "ANTHROPIC_BASE_URL"),
        ("grok-build", "GROK_BASE_URL"),
        ("kimi-code", "MOONSHOT_BASE_URL"),
    ),
)
def test_official_runtime_strips_relay_endpoint_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    override: str,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "build_agent_child_environment",
        lambda *_args, **kwargs: {
            override: "https://relay.invalid",
            "PATH": str(tmp_path),
            "INCLUDE_PROVIDER_CREDENTIALS": str(kwargs.get("include_provider_credentials")),
        },
    )

    environment = runtime_module._official_child_environment(
        family,
        required_path_roots=(tmp_path,),
    )

    assert override not in environment
    assert environment["PATH"] == str(tmp_path)
    assert environment["INCLUDE_PROVIDER_CREDENTIALS"] == "False"
