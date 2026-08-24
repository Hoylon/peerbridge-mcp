from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from peerbridge_mcp import ccswitch_runner as ccswitch_runner_module
from peerbridge_mcp import openai_compatible_runner as openai_runner_module
from peerbridge_mcp.attachments import stage_chat_attachment_payloads
from peerbridge_mcp.ccswitch import CcSwitchProvider, CcSwitchRouteIdentity
from peerbridge_mcp.ccswitch_runner import (
    CcSwitchRunner,
    _bounded_process,
    find_claude_cli,
    resolve_reference,
)
from peerbridge_mcp.openai_compatible_runner import (
    CredentialUnavailableError,
    ProviderHTTPError,
    ResourceUnavailableError,
    RouteMismatchError,
    RunnerConfig,
)
from peerbridge_mcp.multimodal import (
    VERIFIED_ATTACHMENT_MESSAGE_KEY,
    verify_staged_attachments,
)
from tests._image_fixtures import PNG


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        exit_code = wintypes.DWORD()
        try:
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_bounded_process_timeout_terminates_descendant_tree(tmp_path: Path) -> None:
    # The Microsoft Store venv redirector can launch its base interpreter
    # outside a nested Job Object. Exercise the owned executable itself.
    python_executable = getattr(sys, "_base_executable", sys.executable)
    child_pid_path = tmp_path / "child.pid"
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    with pytest.raises(ResourceUnavailableError, match="timed out"):
        _bounded_process(
            [python_executable, str(parent_script), str(child_pid_path)],
            cwd=tmp_path,
            environment=os.environ,
            stdin_text="",
            timeout_seconds=0.5,
            runtime_label="test runtime",
        )

    deadline = time.monotonic() + 3.0
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    while _process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _process_alive(child_pid)


def test_bounded_process_timeout_includes_blocked_stdin_write(tmp_path: Path) -> None:
    sleeper = tmp_path / "never-read-stdin.py"
    sleeper.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    started = time.monotonic()

    with pytest.raises(ResourceUnavailableError, match="timed out"):
        _bounded_process(
            [sys.executable, str(sleeper)],
            cwd=tmp_path,
            environment=os.environ,
            stdin_text="x" * (16 * 1024 * 1024),
            timeout_seconds=0.2,
            runtime_label="blocked stdin runtime",
        )

    assert time.monotonic() - started < 3.0


def test_bounded_process_rechecks_overflow_after_final_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_drain = threading.Event()

    class Stream:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.read_once = False

        def read(self, _size: int) -> bytes:
            release_drain.wait(2)
            if self.read_once:
                return b""
            self.read_once = True
            return self.payload

        def close(self) -> None:
            return None

    class Stdin:
        closed = False

        def write(self, payload: bytes) -> int:
            return len(payload)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class Process:
        pid = 4242

        def __init__(self) -> None:
            self.stdin = Stdin()
            self.stdout = Stream(b"x" * 33)
            self.stderr = Stream(b"")

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(ccswitch_runner_module.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(ccswitch_runner_module, "attach_process_tree", lambda _p: None)
    monkeypatch.setattr(
        ccswitch_runner_module,
        "release_process_tree",
        lambda _p: release_drain.set(),
    )

    with pytest.raises(ResourceUnavailableError, match="exceeded output budget"):
        _bounded_process(
            ["trusted-client"],
            cwd=tmp_path,
            environment={},
            stdin_text="",
            timeout_seconds=1,
            max_capture_bytes=32,
            runtime_label="late overflow runtime",
        )


def test_find_claude_cli_uses_signer_aware_trusted_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = tmp_path / "claude.exe"
    resolved_specs: list[str] = []

    def resolve(spec) -> Path:
        resolved_specs.append(spec.agent_id)
        return expected

    monkeypatch.setattr(ccswitch_runner_module, "find_trusted_executable", resolve)

    assert find_claude_cli() == expected
    assert resolved_specs == ["claude-code"]


@pytest.fixture(autouse=True)
def deterministic_provider_runtime_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def available_slot(**_kwargs: Any):
        yield

    monkeypatch.setattr(openai_runner_module, "provider_runtime_slot", available_slot)


def reference(app: str, provider_id: str) -> str:
    digest = hashlib.sha256(f"{app}:{provider_id}".encode()).hexdigest()
    return f"CCSwitch:{digest[:32]}"


def config(root: Path, model: str = "claude-test") -> RunnerConfig:
    return RunnerConfig(
        project_root=root,
        scope="test",
        connection_id="relay-one",
        route_class="relay",
        provider_id="relay-one",
        model=model,
        route_profile_id="relay-one-claude",
        route_profile_sha256="c" * 64,
        timeout_seconds=10,
    )


def test_reference_resolution_is_redacted_current_and_model_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CcSwitchProvider("claude", "opaque-provider", "Relay", True, True)
    monkeypatch.setattr("peerbridge_mcp.ccswitch_runner.list_providers", lambda _app: (provider,))
    monkeypatch.setattr(
        "peerbridge_mcp.ccswitch_runner.resolve_route_identity",
        lambda **_kwargs: CcSwitchRouteIdentity(
            "claude", "relay", "opaque-provider", "Relay", "claude-test", True, "a" * 64
        ),
    )
    identity = resolve_reference(
        app="claude",
        credential_target=reference("claude", "opaque-provider"),
        route_class="relay",
        model_id="claude-test",
    )
    assert identity.current is True
    assert identity.model_id == "claude-test"


def fake_process(*, observed_model: str, result: str):
    def run(_command, **kwargs):
        assert kwargs["stdin_text"]
        stdout = "\n".join(
            (
                json.dumps({"type": "system", "model": observed_model}),
                json.dumps(
                    {
                        "type": "result",
                        "is_error": False,
                        "result": result,
                        "usage": {
                            "input_tokens": 8,
                            "output_tokens": 3,
                            "total_tokens": 11,
                        },
                    }
                ),
            )
        ).encode()
        return 0, stdout, b""

    return run


def test_runner_uses_stdin_returns_content_free_receipt_and_exact_model(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-claude.py"
    executable.write_bytes(b"test executable")
    identity = CcSwitchRouteIdentity(
        "claude", "relay", "opaque", "Relay", "claude-test", True, "b" * 64
    )
    runner = CcSwitchRunner(
        config(tmp_path),
        credential_target="CCSwitch:" + "a" * 32,
        client_name="claude",
        executable=executable,
        identity_resolver=lambda **_kwargs: identity,
        process_runner=fake_process(observed_model="claude-test", result="safe reply"),
    )
    result = runner.run(
        [{"role": "system", "content": "fixed"}, {"role": "user", "content": "secret prompt"}],
        message_id="message-1",
    )
    assert result.content == "safe reply"
    serialized = json.dumps(result.receipt)
    assert "secret prompt" not in serialized
    assert "safe reply" not in serialized
    assert result.receipt["route_class"] == "relay"
    assert result.receipt["route_profile_id"] == "relay-one-claude"
    assert result.receipt["route_profile_sha256"] == "c" * 64
    assert result.receipt["credential_values_recorded"] is False
    assert result.receipt["usage"]["total_tokens"] == 11


def test_runner_delivers_verified_images_and_text_through_claude_stream_json(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-claude.py"
    executable.write_bytes(b"test executable")
    identity = CcSwitchRouteIdentity(
        "claude", "relay", "opaque", "Relay", "claude-test", True, "b" * 64
    )
    staged = stage_chat_attachment_payloads(
        tmp_path,
        (("chart.png", PNG), ("notes.txt", b"Private visual notes.")),
    )
    verified = verify_staged_attachments(tmp_path, staged)
    captured: dict[str, Any] = {}

    def process_runner(command, **kwargs):
        captured["command"] = list(command)
        captured["stdin_text"] = kwargs["stdin_text"]
        return fake_process(observed_model="claude-test", result="reviewed")(
            command, **kwargs
        )

    result = CcSwitchRunner(
        config(tmp_path),
        credential_target="CCSwitch:" + "a" * 32,
        client_name="claude",
        executable=executable,
        identity_resolver=lambda **_kwargs: identity,
        process_runner=process_runner,
    ).run(
        [
            {
                "role": "user",
                "content": "Inspect the supplied evidence.",
                VERIFIED_ATTACHMENT_MESSAGE_KEY: [
                    item.public_record() for item in verified
                ],
            }
        ],
        message_id="message-with-attachments",
    )

    command = captured["command"]
    assert command[command.index("--input-format") + 1] == "stream-json"
    event = json.loads(captured["stdin_text"])
    blocks = event["message"]["content"]
    assert [block["type"] for block in blocks] == ["text", "image", "text"]
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/png"
    assert blocks[2]["text"].endswith("Private visual notes.")
    delivery = result.receipt["attachment_delivery"]
    assert delivery["attachment_count"] == 2
    assert delivery["model_view_confirmed"] is False
    serialized = json.dumps(result.receipt, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "Private visual notes." not in serialized


def test_runner_strips_ambient_provider_overrides_from_ccswitch_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "provider-auth")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://stale-relay.invalid")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "other-provider-secret")
    captured: dict[str, str] = {}

    def process_runner(command, **kwargs):
        captured.update(kwargs["environment"])
        return fake_process(observed_model="claude-test", result="safe reply")(
            command, **kwargs
        )

    make_runner(tmp_path, process_runner).run(
        [{"role": "user", "content": "test"}]
    )

    assert "ANTHROPIC_AUTH_TOKEN" not in captured
    assert "ANTHROPIC_BASE_URL" not in captured
    assert "CLAUDE_CODE_USE_BEDROCK" not in captured
    assert "GITHUB_TOKEN" not in captured
    assert "OPENAI_API_KEY" not in captured


def test_runner_uses_shared_resource_admission_before_native_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    @contextmanager
    def tracked_slot(**_kwargs: Any):
        events.append("admitted")
        try:
            yield
        finally:
            events.append("released")

    monkeypatch.setattr(openai_runner_module, "provider_runtime_slot", tracked_slot)
    executable = tmp_path / "fake-claude.py"
    executable.write_bytes(b"test executable")
    identity = CcSwitchRouteIdentity(
        "claude", "relay", "opaque", "Relay", "claude-test", True, "b" * 64
    )

    def resolve_identity(**_kwargs):
        events.append("identity")
        return identity

    def run_process(_command, **_kwargs):
        events.append("provider")
        return fake_process(observed_model="claude-test", result="safe reply")(
            _command, **_kwargs
        )

    runner = CcSwitchRunner(
        config(tmp_path),
        credential_target="CCSwitch:" + "a" * 32,
        client_name="claude",
        executable=executable,
        identity_resolver=resolve_identity,
        process_runner=run_process,
    )

    result = runner.run([{"role": "user", "content": "test"}])

    assert result.content == "safe reply"
    assert events == ["admitted", "identity", "provider", "identity", "released"]


def test_runner_rejects_observed_model_drift(tmp_path: Path) -> None:
    executable = tmp_path / "fake-claude.py"
    executable.write_bytes(b"test executable")
    identity = CcSwitchRouteIdentity(
        "claude", "relay", "opaque", "Relay", "claude-test", True, "b" * 64
    )
    runner = CcSwitchRunner(
        config(tmp_path),
        credential_target="CCSwitch:" + "a" * 32,
        client_name="claude",
        executable=executable,
        identity_resolver=lambda **_kwargs: identity,
        process_runner=fake_process(observed_model="other-model", result="reply"),
    )
    with pytest.raises(RouteMismatchError):
        runner.run([{"role": "user", "content": "test"}])


def test_runner_maps_unselected_provider_to_unavailable(tmp_path: Path) -> None:
    executable = tmp_path / "fake-claude.py"
    executable.write_bytes(b"test executable")

    def unavailable(**_kwargs):
        from peerbridge_mcp.ccswitch import CcSwitchError

        raise CcSwitchError("CC Switch provider is not currently selected")

    runner = CcSwitchRunner(
        config(tmp_path),
        credential_target="CCSwitch:" + "a" * 32,
        client_name="claude",
        executable=executable,
        identity_resolver=unavailable,
        process_runner=fake_process(observed_model="claude-test", result="reply"),
    )
    with pytest.raises(CredentialUnavailableError):
        runner.run([{"role": "user", "content": "test"}])


def test_runner_rejects_ccswitch_selection_drift_during_provider_call(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-claude.py"
    executable.write_bytes(b"test executable")
    identities = iter(
        (
            CcSwitchRouteIdentity(
                "claude",
                "relay",
                "provider-a",
                "Relay A",
                "claude-test",
                True,
                "a" * 64,
            ),
            CcSwitchRouteIdentity(
                "claude",
                "relay",
                "provider-b",
                "Relay B",
                "claude-test",
                True,
                "b" * 64,
            ),
        )
    )
    runner = CcSwitchRunner(
        config(tmp_path),
        credential_target="CCSwitch:" + "a" * 32,
        client_name="claude",
        executable=executable,
        identity_resolver=lambda **_kwargs: next(identities),
        process_runner=fake_process(observed_model="claude-test", result="unsafe"),
    )

    with pytest.raises(RouteMismatchError, match="changed during inference"):
        runner.run([{"role": "user", "content": "test"}])


def make_runner(tmp_path: Path, process_runner) -> CcSwitchRunner:
    executable = tmp_path / "fake-claude.py"
    executable.write_bytes(b"test executable")
    identity = CcSwitchRouteIdentity(
        "claude", "relay", "opaque", "Relay", "claude-test", True, "b" * 64
    )
    return CcSwitchRunner(
        config(tmp_path),
        credential_target="CCSwitch:" + "a" * 32,
        client_name="claude",
        executable=executable,
        identity_resolver=lambda **_kwargs: identity,
        process_runner=process_runner,
    )


def test_runner_reports_malformed_output_with_structured_status(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, lambda *_args, **_kwargs: (0, b"not-json", b"private"))

    with pytest.raises(ProviderHTTPError) as raised:
        runner.run([{"role": "user", "content": "test"}])

    assert str(raised.value) == "CC Switch provider returned malformed event output"
    assert raised.value.status_code == 502
    assert raised.value.retryable is False


@pytest.mark.parametrize(
    ("status", "retryable"),
    ((402, False), (403, False), (429, True), (503, True), (422, False), (True, True)),
)
def test_runner_preserves_sanitized_provider_status(
    tmp_path: Path, status: object, retryable: bool
) -> None:
    stdout = json.dumps(
        {"type": "result", "is_error": True, "api_error_status": status}
    ).encode()
    runner = make_runner(tmp_path, lambda *_args, **_kwargs: (1, stdout, b"private"))

    with pytest.raises(ProviderHTTPError) as raised:
        runner.run([{"role": "user", "content": "test"}])

    expected_status = status if isinstance(status, int) and not isinstance(status, bool) else 502
    assert str(raised.value) == "CC Switch provider request failed"
    assert raised.value.status_code == expected_status
    assert raised.value.retryable is retryable


def test_runner_keeps_401_as_credential_failure_but_403_as_provider_failure(
    tmp_path: Path,
) -> None:
    def output(status: int) -> bytes:
        return json.dumps(
            {"type": "result", "is_error": True, "api_error_status": status}
        ).encode()

    with pytest.raises(CredentialUnavailableError):
        make_runner(
            tmp_path,
            lambda *_args, **_kwargs: (1, output(401), b"private"),
        ).run([{"role": "user", "content": "test"}])

    with pytest.raises(ProviderHTTPError) as denied:
        make_runner(
            tmp_path,
            lambda *_args, **_kwargs: (1, output(403), b"private"),
        ).run([{"role": "user", "content": "test"}])
    assert denied.value.status_code == 403


def test_runner_reports_empty_response_with_structured_status(tmp_path: Path) -> None:
    runner = make_runner(
        tmp_path,
        fake_process(observed_model="claude-test", result=""),
    )

    with pytest.raises(ProviderHTTPError) as raised:
        runner.run([{"role": "user", "content": "test"}])

    assert str(raised.value) == "CC Switch provider returned an empty response"
    assert raised.value.status_code == 502
    assert raised.value.retryable is False
