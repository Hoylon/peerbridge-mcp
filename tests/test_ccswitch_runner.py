from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from peerbridge_mcp import openai_compatible_runner as openai_runner_module
from peerbridge_mcp.ccswitch import CcSwitchProvider, CcSwitchRouteIdentity
from peerbridge_mcp.ccswitch_runner import CcSwitchRunner, resolve_reference
from peerbridge_mcp.openai_compatible_runner import (
    CredentialUnavailableError,
    ProviderHTTPError,
    RouteMismatchError,
    RunnerConfig,
)


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
                json.dumps({"type": "result", "is_error": False, "result": result}),
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
    assert events == ["admitted", "identity", "provider", "released"]


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
    ((429, True), (503, True), (422, False), (True, True)),
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
