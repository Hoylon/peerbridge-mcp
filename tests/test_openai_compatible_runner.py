from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import pytest

from peerbridge_mcp import __version__
from peerbridge_mcp import credentials
from peerbridge_mcp import openai_compatible_runner as runner_module
from peerbridge_mcp.bridge import sha256_bytes, stable_sha256
from peerbridge_mcp.openai_compatible_runner import (
    DEFAULT_ALLOWED_TOOLS,
    HTTPResponse,
    OpenAICompatibleRunner,
    ProviderModelRegistry,
    ProviderHTTPError,
    ResourceUnavailableError,
    RouteMismatchError,
    RunCancelledError,
    RunnerConfig,
    RunnerError,
    StdlibHTTPTransport,
    ToolCallError,
    discover_provider_models,
)
from peerbridge_mcp.resource_guard import ResourceGuardError
from peerbridge_mcp.server import TOOL_SCHEMAS


def _test_credential(*parts: str) -> str:
    return "-".join(parts)


@pytest.fixture(autouse=True)
def deterministic_provider_runtime_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def available_slot(**_kwargs: Any):
        yield

    monkeypatch.setattr(runner_module, "provider_runtime_slot", available_slot)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    (
        ("https://relay.example", "https://relay.example/v1"),
        ("https://relay.example/v1/", "https://relay.example/v1"),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        ("https://relay.example/custom/base", "https://relay.example/custom/base"),
    ),
)
def test_api_base_preserves_explicit_compatible_provider_paths(
    endpoint: str,
    expected: str,
) -> None:
    assert runner_module._api_base_url(endpoint) == expected


class FakeMCPTransport:
    def __init__(
        self,
        *,
        scope: str,
        connection_id: str,
        route_class: str,
        endpoint: str,
        api_key: str,
        expected_timeout: float = 15.0,
    ) -> None:
        normalized = credentials.normalize_endpoint(endpoint)
        metadata = {
            "scope": scope,
            "connection_id": connection_id,
            "display_name": "Test relay",
            "route_class": route_class,
            "secret_backend": "windows-credential-manager",
            "credential_target": credentials.credential_target(scope, connection_id),
            "endpoint_sha256": sha256_bytes(normalized.encode("utf-8")),
            "credential_fingerprint_sha256": stable_sha256(
                {"api_key": api_key, "endpoint": normalized}
            ),
            "enabled": True,
        }
        self.connection = {
            **metadata,
            "connection_sha256": stable_sha256(metadata),
        }
        self.started = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.expected_timeout = expected_timeout

    def start(self) -> None:
        self.started = True

    def request(
        self, method: str, params: Mapping[str, Any], *, timeout: float
    ) -> Mapping[str, Any]:
        assert timeout == self.expected_timeout
        arguments = dict(params)
        self.calls.append((method, arguments))
        if method == "initialize":
            return {
                "protocolVersion": "2026-07-28",
                "serverInfo": {"name": "peerbridge-mcp", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }
        if method == "tools/list":
            expected = set(DEFAULT_ALLOWED_TOOLS) | {"list_provider_connections"}
            return {
                "tools": [
                    schema for schema in TOOL_SCHEMAS if schema["name"] in expected
                ]
            }
        if method != "tools/call":
            raise AssertionError(f"unexpected MCP method: {method}")
        name = arguments["name"]
        if name == "list_provider_connections":
            return {
                "structuredContent": {
                    "connections": [self.connection],
                    "count": 1,
                }
            }
        if name == "list_memories":
            assert arguments["arguments"] == {"room_id": "alpha"}
            return {
                "structuredContent": {
                    "memories": [
                        {
                            "memory_id": "memory-alpha",
                            "visibility": "room",
                            "room_id": "alpha",
                            "title": "Approved Alpha rule",
                            "body": "Use the audited room constraint.",
                            "memory_sha256": "a" * 64,
                            "status": "active",
                        }
                    ],
                    "count": 1,
                }
            }
        if name == "bridge_status":
            assert arguments["arguments"] == {}
            return {
                "structuredContent": {
                    "scope": self.connection["scope"],
                    "agent_id": "fake-provider-agent",
                    "audit_chain_sha256": "b" * 64,
                }
            }
        raise AssertionError(f"unexpected MCP tool: {name}")

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self.calls.append((method, dict(params)))

    def close(self) -> None:
        self.closed = True


class BudgetMCPTransport(FakeMCPTransport):
    def __init__(self, *args: Any, tool_result: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tool_result = dict(tool_result)
        self.executed_tools: list[str] = []

    def request(
        self, method: str, params: Mapping[str, Any], *, timeout: float
    ) -> Mapping[str, Any]:
        if method == "tools/call" and params.get("name") == "bridge_status":
            assert timeout == self.expected_timeout
            arguments = dict(params)
            self.calls.append((method, arguments))
            self.executed_tools.append("bridge_status")
            return {"structuredContent": self.tool_result}
        return super().request(method, params, timeout=timeout)


def _bound_access(
    *,
    scope: str,
    connection_id: str,
    route_class: str,
    provider_id: str,
    endpoint: str,
    api_key: str | None,
    credential_fingerprint_sha256: str,
    endpoint_sha256: str | None = None,
) -> credentials.ProviderAccess:
    normalized = credentials.normalize_endpoint(endpoint)
    return credentials.ProviderAccess(
        endpoint=normalized,
        api_key=api_key,
        credential_target=credentials.credential_target(scope, connection_id),
        endpoint_sha256=endpoint_sha256
        or sha256_bytes(normalized.encode("utf-8")),
        credential_fingerprint_sha256=credential_fingerprint_sha256,
        secret_present=api_key is not None,
        descriptor_schema="peerbridge.provider-credential.v2",
        route_class=route_class,
        provider_id=provider_id,
        credential_version_sha256=credential_fingerprint_sha256,
        descriptor_bound=True,
    )


class FakeOpenAITransport:
    def __init__(self, *, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        assert timeout == 15.0
        assert headers["Authorization"] == f"Bearer {self.api_key}"
        parsed = json.loads(body) if body is not None else None
        self.requests.append({"method": method, "url": url, "body": parsed})
        if method == "GET":
            return HTTPResponse(
                status=200,
                body=json.dumps({"data": [{"id": self.model}]}).encode("utf-8"),
            )
        assert method == "POST"
        assert parsed["model"] == self.model
        if len([item for item in self.requests if item["method"] == "POST"]) == 1:
            tool_names = {
                item["function"]["name"] for item in parsed["tools"]
            }
            assert {"list_memories", "read_memory"}.issubset(tool_names)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-memory",
                        "type": "function",
                        "function": {
                            "name": "list_memories",
                            "arguments": json.dumps({"room_id": "alpha"}),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            tool_messages = [
                item for item in parsed["messages"] if item["role"] == "tool"
            ]
            assert len(tool_messages) == 1
            assert "Use the audited room constraint." in tool_messages[0]["content"]
            message = {"role": "assistant", "content": "Applied approved memory."}
            finish_reason = "stop"
        response = {
            "model": self.model,
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
        }
        return HTTPResponse(status=200, body=json.dumps(response).encode("utf-8"))


class ScriptedOpenAITransport:
    def __init__(self, *, model: str, messages: list[dict[str, Any]]) -> None:
        self.model = model
        self.messages = list(messages)
        self.request_bodies: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        _url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        assert timeout > 0
        assert headers.get("Authorization")
        if method == "GET":
            return HTTPResponse(
                status=200,
                body=json.dumps({"data": [{"id": self.model}]}).encode("utf-8"),
            )
        assert method == "POST"
        assert body is not None
        request_body = json.loads(body)
        assert request_body["model"] == self.model
        self.request_bodies.append(request_body)
        message = self.messages.pop(0)
        finish_reason = "tool_calls" if message.get("tool_calls") else "stop"
        response = {
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
        }
        return HTTPResponse(status=200, body=json.dumps(response).encode("utf-8"))


def test_default_mcp_subprocess_uses_controlled_cwd_outside_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path.resolve()
    (project_root / "peerbridge_mcp.py").write_text(
        "raise RuntimeError('project module shadow executed')\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    class CapturingStdioTransport:
        def __init__(self, command: Any, *, cwd: Path) -> None:
            captured["command"] = tuple(command)
            captured["cwd"] = Path(cwd).resolve()

    monkeypatch.setattr(runner_module, "StdioMCPTransport", CapturingStdioTransport)
    config = RunnerConfig(
        project_root=project_root,
        scope="shadow-test",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
    )

    OpenAICompatibleRunner(config)

    subprocess_cwd = captured["cwd"]
    assert subprocess_cwd != project_root
    assert project_root not in subprocess_cwd.parents
    command = captured["command"]
    project_root_index = command.index("--project-root") + 1
    assert Path(command[project_root_index]).resolve() == project_root


def test_stdio_mcp_subprocess_receives_only_os_essentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class FakeProcess:
        stdout = None

        def poll(self) -> None:
            return None

    def capture_popen(*_args: Any, **kwargs: Any) -> FakeProcess:
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-local-mcp")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-local-mcp")
    monkeypatch.setenv("XAI_API_KEY", "must-not-reach-local-mcp")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-local-mcp")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "must-not-reach-local-mcp")
    monkeypatch.setenv("ARBITRARY_PRIVATE_VALUE", "must-not-reach-local-mcp")
    monkeypatch.setattr(runner_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(runner_module, "attach_process_tree", lambda _process: True)

    transport = runner_module.StdioMCPTransport(
        (runner_module.sys.executable, "-V"), cwd=tmp_path
    )
    transport.start()

    environment = captured["env"]
    assert str(tmp_path / "bin") not in environment.get("PATH", "")
    assert str(Path(r"C:\Windows") / "System32") in environment["PATH"]
    assert environment["SYSTEMROOT"] == r"C:\Windows"
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "GITHUB_TOKEN",
        "CLOUDFLARE_API_TOKEN",
        "ARBITRARY_PRIVATE_VALUE",
    ):
        assert name not in environment


def test_runner_uses_shared_resource_admission_before_provider_work(
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

    monkeypatch.setattr(runner_module, "provider_runtime_slot", tracked_slot)
    config = RunnerConfig(
        project_root=tmp_path,
        scope="shared-admission",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
    )
    runner = OpenAICompatibleRunner(config)
    sentinel = object()

    def provider_work(_messages, *, message_id=None):
        assert message_id == "message-one"
        events.append("provider")
        return sentinel

    monkeypatch.setattr(runner, "_run_unchecked", provider_work)

    assert runner.run([{"role": "user", "content": "test"}], message_id="message-one") is sentinel
    assert events == ["admitted", "provider", "released"]


def test_resource_admission_failure_prevents_openai_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def rejected_slot(**_kwargs: Any):
        raise ResourceGuardError("synthetic full capacity")
        yield

    monkeypatch.setattr(runner_module, "provider_runtime_slot", rejected_slot)
    config = RunnerConfig(
        project_root=tmp_path,
        scope="rejected-admission",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
    )
    runner = OpenAICompatibleRunner(config)
    provider_called = False

    def provider_work(_messages, *, message_id=None):
        nonlocal provider_called
        provider_called = True
        return object()

    monkeypatch.setattr(runner, "_run_unchecked", provider_work)

    with pytest.raises(ResourceUnavailableError, match="synthetic full capacity"):
        runner.run([{"role": "user", "content": "test"}])
    assert provider_called is False


def test_runner_reads_scoped_memory_without_putting_content_in_receipt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    endpoint = "https://relay.example/v1"
    api_key = "unit-test-provider-secret"
    model = "deepseek-chat"
    raw = json.dumps(
        {"api_key": api_key, "endpoint": endpoint},
        sort_keys=True,
        separators=(",", ":"),
    )
    access = _bound_access(
        scope="runner-test",
        connection_id="relay-main",
        route_class="relay",
        provider_id="relay-sui-xiang",
        endpoint=endpoint,
        api_key=api_key,
        credential_fingerprint_sha256=sha256_bytes(raw.encode("utf-8")),
    )
    monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
    config = RunnerConfig(
        project_root=tmp_path,
        scope="runner-test",
        connection_id="relay-main",
        route_class="relay",
        provider_id="relay-sui-xiang",
        model=model,
        route_profile_id="relay-main-model",
        route_profile_sha256="c" * 64,
        room_id="alpha",
        session_id="runner-session",
        agent_id="deepseek-agent",
        timeout_seconds=15.0,
    )
    mcp = FakeMCPTransport(
        scope=config.scope,
        connection_id=config.connection_id,
        route_class=config.route_class,
        endpoint=endpoint,
        api_key=api_key,
    )
    _bind_fake_connection(mcp, config=config, access=access)
    http = FakeOpenAITransport(model=model, api_key=api_key)

    result = OpenAICompatibleRunner(
        config,
        http_transport=http,
        mcp_transport=mcp,
    ).run(
        [{"role": "user", "content": "Apply the room-approved context."}],
        message_id="message-one",
    )

    assert result.content == "Applied approved memory."
    assert mcp.started is True
    assert mcp.closed is True
    assert any(
        method == "tools/call" and params.get("name") == "list_memories"
        for method, params in mcp.calls
    )
    assert result.receipt["tool_calls"][0]["name"] == "list_memories"
    serialized_receipt = json.dumps(result.receipt, sort_keys=True)
    assert api_key not in serialized_receipt
    assert endpoint not in serialized_receipt
    assert "Use the audited room constraint." not in serialized_receipt
    assert "Apply the room-approved context." not in serialized_receipt
    assert result.receipt["credential_contents_recorded"] is False
    assert result.receipt["raw_content_recorded"] is False
    assert result.receipt["route"]["route_profile_id"] == "relay-main-model"
    assert result.receipt["route"]["route_profile_sha256"] == "c" * 64
    assert result.receipt["usage"]["status"] == "reported"
    assert result.receipt["usage"]["reported_calls"] == 2
    assert result.receipt["usage"]["total_tokens"] == 30


class FakeProviderState:
    def __init__(
        self,
        *,
        model: str,
        expected_api_key: str | None,
        transient_failures: int = 0,
        delay_seconds: float = 0.0,
        response_model: str | None = None,
        tool_name: str = "bridge_status",
    ) -> None:
        self.model = model
        self.expected_api_key = expected_api_key
        self.transient_failures = transient_failures
        self.delay_seconds = delay_seconds
        self.response_model = response_model or model
        self.tool_name = tool_name
        self.lock = threading.Lock()
        self.models_calls = 0
        self.chat_calls = 0
        self.authorization_headers: list[str | None] = []
        self.idempotency_keys: list[str] = []
        self.request_bodies: list[bytes] = []


@contextmanager
def fake_provider(state: FakeProviderState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
            payload = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _authorize(self) -> bool:
            observed = self.headers.get("Authorization")
            state.authorization_headers.append(observed)
            expected = (
                f"Bearer {state.expected_api_key}"
                if state.expected_api_key is not None
                else None
            )
            if observed != expected:
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            if self.path != "/v1/models" or not self._authorize():
                return
            with state.lock:
                state.models_calls += 1
            self._send_json(200, {"data": [{"id": state.model}]})

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions" or not self._authorize():
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            idempotency_key = self.headers.get("Idempotency-Key")
            assert idempotency_key
            state.idempotency_keys.append(idempotency_key)
            state.request_bodies.append(raw)
            with state.lock:
                state.chat_calls += 1
                chat_call = state.chat_calls
            if state.delay_seconds:
                time.sleep(state.delay_seconds)
            if chat_call <= state.transient_failures:
                self._send_json(503, {"error": "try later"})
                return
            request = json.loads(raw)
            assert request["model"] == state.model
            if chat_call == state.transient_failures + 1:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-status",
                            "type": "function",
                            "function": {
                                "name": state.tool_name,
                                "arguments": "{}",
                            },
                        }
                    ],
                }
                finish_reason = "tool_calls"
            else:
                assert any(item.get("role") == "tool" for item in request["messages"])
                message = {"role": "assistant", "content": "done"}
                finish_reason = "stop"
            self._send_json(
                200,
                {
                    "model": state.response_model,
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": finish_reason,
                        }
                    ],
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@contextmanager
def cross_origin_redirect():
    forwarded_authorization: list[str | None] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            forwarded_authorization.append(self.headers.get("Authorization"))
            payload = b'{"data":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{sink.server_port}/captured"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        yield (
            f"http://127.0.0.1:{redirect.server_port}/v1/models",
            forwarded_authorization,
        )
    finally:
        redirect.shutdown()
        redirect.server_close()
        redirect_thread.join(timeout=3)
        sink.shutdown()
        sink.server_close()
        sink_thread.join(timeout=3)


def test_stdlib_http_transport_does_not_follow_cross_origin_redirects() -> None:
    response: HTTPResponse | None = None
    failure: ProviderHTTPError | None = None
    with cross_origin_redirect() as (url, forwarded_authorization):
        try:
            response = StdlibHTTPTransport().request(
                "GET",
                url,
                headers={
                    "Authorization": "Bearer "
                    + _test_credential("test", "token")
                },
                body=None,
                timeout=2,
            )
        except ProviderHTTPError as exc:
            failure = exc

        assert forwarded_authorization == []
        observed_status = failure.status_code if failure is not None else response.status
        assert 300 <= observed_status < 400


def _mcp_for_endpoint(
    *,
    config: RunnerConfig,
    endpoint: str,
    api_key: str | None,
) -> FakeMCPTransport:
    key = api_key or ""
    normalized = credentials.normalize_endpoint(endpoint)
    mcp = FakeMCPTransport(
        scope=config.scope,
        connection_id=config.connection_id,
        route_class=config.route_class,
        endpoint=endpoint,
        api_key=key,
        expected_timeout=float(config.timeout_seconds),
    )
    if api_key is None:
        metadata = {
            "scope": config.scope,
            "connection_id": config.connection_id,
            "display_name": "Test relay",
            "route_class": config.route_class,
            "secret_backend": "windows-credential-manager",
            "credential_target": credentials.credential_target(
                config.scope, config.connection_id
            ),
            "endpoint_sha256": sha256_bytes(normalized.encode("utf-8")),
            "credential_fingerprint_sha256": "",
            "enabled": True,
        }
        mcp.connection = metadata
    return mcp


def _bind_fake_connection(
    mcp: FakeMCPTransport,
    *,
    config: RunnerConfig,
    access: credentials.ProviderAccess,
) -> None:
    mcp.connection.update(
        {
            "provider_id": config.provider_id,
            "credential_fingerprint_sha256": (
                access.credential_fingerprint_sha256
            ),
            "descriptor_schema": access.descriptor_schema,
            "credential_version_sha256": access.credential_version_sha256,
        }
    )
    identity = {
        key: value
        for key, value in mcp.connection.items()
        if key != "connection_sha256"
    }
    mcp.connection["connection_sha256"] = stable_sha256(identity)


def _patch_remote_access(
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: RunnerConfig,
    endpoint: str,
    api_key: str,
    mcp: FakeMCPTransport,
) -> None:
    normalized = credentials.normalize_endpoint(endpoint)
    raw = json.dumps(
        {"api_key": api_key, "endpoint": normalized},
        sort_keys=True,
        separators=(",", ":"),
    )
    access = _bound_access(
        scope=config.scope,
        connection_id=config.connection_id,
        route_class=config.route_class,
        provider_id=config.provider_id,
        endpoint=normalized,
        api_key=api_key,
        credential_fingerprint_sha256=sha256_bytes(raw.encode("utf-8")),
    )
    monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
    _bind_fake_connection(mcp, config=config, access=access)


@pytest.mark.parametrize(
    ("provider_id", "model"),
    [
        ("relay-deepseek", "deepseek-chat"),
        ("relay-kimi", "moonshot-v1-8k"),
    ],
)
def test_real_local_http_relay_routes_bind_identity_tool_boundaries_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    model: str,
) -> None:
    secret = f"test-{provider_id}-secret"
    state = FakeProviderState(
        model=model, expected_api_key=secret, transient_failures=1
    )
    with fake_provider(state) as endpoint:
        raw = json.dumps(
            {"api_key": secret, "endpoint": endpoint},
            sort_keys=True,
            separators=(",", ":"),
        )
        access = _bound_access(
            scope="route-e2e",
            connection_id=provider_id,
            route_class="relay",
            provider_id=provider_id,
            endpoint=endpoint,
            api_key=secret,
            credential_fingerprint_sha256=sha256_bytes(raw.encode("utf-8")),
        )
        monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
        config = RunnerConfig(
            project_root=tmp_path,
            scope="route-e2e",
            connection_id=provider_id,
            route_class="relay",
            provider_id=provider_id,
            model=model,
            agent_id=f"{provider_id}-agent",
            timeout_seconds=1.0,
            retry_backoff_seconds=0,
            allowed_tools=("bridge_status",),
        )
        mcp = _mcp_for_endpoint(config=config, endpoint=endpoint, api_key=secret)
        _bind_fake_connection(mcp, config=config, access=access)

        result = OpenAICompatibleRunner(config, mcp_transport=mcp).run(
            [{"role": "user", "content": "inspect"}], message_id="route-message"
        )

    assert result.content == "done"
    assert result.receipt["route"]["route_class"] == "relay"
    assert result.receipt["route"]["provider_id"] == provider_id
    assert result.receipt["route"]["model_id"] == model
    assert result.receipt["tool_calls"][0]["name"] == "bridge_status"
    chat_calls = [
        call
        for call in result.receipt["provider_http_calls"]
        if call["operation"] == "chat.completions"
    ]
    assert chat_calls[0]["attempts"] == 2
    assert chat_calls[1]["attempts"] == 1
    assert len(state.idempotency_keys) == 3
    assert state.idempotency_keys[0] == state.idempotency_keys[1]
    assert state.idempotency_keys[0] != state.idempotency_keys[2]
    serialized = json.dumps(result.receipt, sort_keys=True)
    assert secret not in serialized
    assert endpoint not in serialized
    assert "inspect" not in serialized
    assert all(
        secret.encode("utf-8") not in path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def test_real_local_http_local_route_uses_no_authorization_or_persisted_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = FakeProviderState(model="local-qwen", expected_api_key=None)
    with fake_provider(state) as endpoint:
        descriptor = json.dumps(
            {"endpoint": endpoint, "kind": "local-openai-compatible"},
            sort_keys=True,
            separators=(",", ":"),
        )
        access = _bound_access(
            scope="local-e2e",
            connection_id="local-qwen",
            route_class="local",
            provider_id="local-runtime",
            endpoint=endpoint,
            api_key=None,
            credential_fingerprint_sha256=sha256_bytes(descriptor.encode("utf-8")),
        )
        monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
        config = RunnerConfig(
            project_root=tmp_path,
            scope="local-e2e",
            connection_id="local-qwen",
            route_class="local",
            provider_id="local-runtime",
            model="local-qwen",
            allowed_tools=("bridge_status",),
            retry_backoff_seconds=0,
        )
        mcp = _mcp_for_endpoint(config=config, endpoint=endpoint, api_key=None)
        _bind_fake_connection(mcp, config=config, access=access)
        result = OpenAICompatibleRunner(config, mcp_transport=mcp).run(
            [{"role": "user", "content": "inspect local"}]
        )

    assert state.authorization_headers and set(state.authorization_headers) == {None}
    assert result.receipt["route"]["route_class"] == "local"
    assert result.receipt["route"]["secret_present"] is False
    assert result.receipt["credential_contents_recorded"] is False


def test_real_local_http_rejects_completion_model_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = _test_credential("identity", "secret")
    state = FakeProviderState(
        model="deepseek-chat",
        expected_api_key=secret,
        response_model="unexpected-model",
    )
    with fake_provider(state) as endpoint:
        raw = json.dumps(
            {"api_key": secret, "endpoint": endpoint},
            sort_keys=True,
            separators=(",", ":"),
        )
        access = _bound_access(
            scope="identity-e2e",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-deepseek",
            endpoint=endpoint,
            api_key=secret,
            credential_fingerprint_sha256=sha256_bytes(raw.encode()),
        )
        monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
        config = RunnerConfig(
            project_root=tmp_path,
            scope="identity-e2e",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-deepseek",
            model="deepseek-chat",
            allowed_tools=("bridge_status",),
        )
        mcp = _mcp_for_endpoint(config=config, endpoint=endpoint, api_key=secret)
        _bind_fake_connection(mcp, config=config, access=access)
        with pytest.raises(RouteMismatchError):
            OpenAICompatibleRunner(config, mcp_transport=mcp).run(
                [{"role": "user", "content": "identity check"}]
            )
    assert mcp.closed is True


def test_real_local_http_accepts_only_explicit_response_model_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = _test_credential("response", "binding", "secret")
    state = FakeProviderState(
        model="grok-4.6",
        expected_api_key=secret,
        response_model="grok-4.6-build",
    )
    with fake_provider(state) as endpoint:
        access = _bound_access(
            scope="response-binding",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-grok",
            endpoint=endpoint,
            api_key=secret,
            credential_fingerprint_sha256="a" * 64,
        )
        monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
        config = RunnerConfig(
            project_root=tmp_path,
            scope="response-binding",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-grok",
            model="grok-4.6",
            response_model="grok-4.6-build",
            allowed_tools=("bridge_status",),
        )
        mcp = _mcp_for_endpoint(config=config, endpoint=endpoint, api_key=secret)
        _bind_fake_connection(mcp, config=config, access=access)
        result = OpenAICompatibleRunner(config, mcp_transport=mcp).run(
            [{"role": "user", "content": "identity check"}]
        )

    assert result.receipt["route"]["model_id"] == "grok-4.6"
    assert result.receipt["route"]["response_model_id"] == "grok-4.6-build"
    assert result.receipt["observed_response_model_ids"] == [
        "grok-4.6-build",
        "grok-4.6-build",
    ]
    assert result.receipt["route"]["accepted_response_model_ids"] == [
        "grok-4.6",
        "grok-4.6-build",
    ]


def test_explicit_response_alias_also_accepts_advertised_request_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = _test_credential("response", "alias", "request", "model", "secret")
    state = FakeProviderState(
        model="grok-4.6",
        expected_api_key=secret,
        response_model="grok-4.6",
    )
    with fake_provider(state) as endpoint:
        access = _bound_access(
            scope="response-alias-request-model",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-grok",
            endpoint=endpoint,
            api_key=secret,
            credential_fingerprint_sha256="b" * 64,
        )
        monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
        config = RunnerConfig(
            project_root=tmp_path,
            scope="response-alias-request-model",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-grok",
            model="grok-4.6",
            response_model="grok-4.6-build",
            allowed_tools=("bridge_status",),
        )
        mcp = _mcp_for_endpoint(config=config, endpoint=endpoint, api_key=secret)
        _bind_fake_connection(mcp, config=config, access=access)
        result = OpenAICompatibleRunner(config, mcp_transport=mcp).run(
            [{"role": "user", "content": "identity alias check"}]
        )

    assert result.receipt["observed_response_model_ids"] == [
        "grok-4.6",
        "grok-4.6",
    ]
    assert result.receipt["route"]["accepted_response_model_ids"] == [
        "grok-4.6",
        "grok-4.6-build",
    ]


def test_registry_route_identity_mismatch_fails_before_provider_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "http://127.0.0.1:8765/v1"
    secret = _test_credential("route", "secret")
    raw = json.dumps(
        {"api_key": secret, "endpoint": endpoint},
        sort_keys=True,
        separators=(",", ":"),
    )
    access = _bound_access(
        scope="route-mismatch",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-deepseek",
        endpoint=endpoint,
        api_key=secret,
        credential_fingerprint_sha256=sha256_bytes(raw.encode()),
    )
    monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
    config = RunnerConfig(
        project_root=tmp_path,
        scope="route-mismatch",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-deepseek",
        model="deepseek-chat",
        allowed_tools=("bridge_status",),
    )
    mcp = _mcp_for_endpoint(config=config, endpoint=endpoint, api_key=secret)
    _bind_fake_connection(mcp, config=config, access=access)
    mcp.connection["route_class"] = "local"
    identity = {key: value for key, value in mcp.connection.items() if key != "connection_sha256"}
    mcp.connection["connection_sha256"] = stable_sha256(identity)

    class NeverHTTP:
        def request(self, *_args: Any, **_kwargs: Any) -> HTTPResponse:
            raise AssertionError("provider HTTP must not run after registry mismatch")

    with pytest.raises(RouteMismatchError):
        OpenAICompatibleRunner(
            config, mcp_transport=mcp, http_transport=NeverHTTP()
        ).run([{"role": "user", "content": "route check"}])
    assert mcp.closed is True


def test_local_access_hash_mismatch_fails_before_provider_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "http://127.0.0.1:8765/v1"
    access = _bound_access(
        scope="hash-mismatch",
        connection_id="local",
        route_class="local",
        provider_id="local-runtime",
        endpoint=endpoint,
        api_key=None,
        endpoint_sha256="0" * 64,
        credential_fingerprint_sha256="1" * 64,
    )
    monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
    config = RunnerConfig(
        project_root=tmp_path,
        scope="hash-mismatch",
        connection_id="local",
        route_class="local",
        provider_id="local-runtime",
        model="local-model",
        allowed_tools=("bridge_status",),
    )
    mcp = _mcp_for_endpoint(config=config, endpoint=endpoint, api_key=None)
    _bind_fake_connection(mcp, config=config, access=access)

    class NeverHTTP:
        def request(self, *_args: Any, **_kwargs: Any) -> HTTPResponse:
            raise AssertionError("provider HTTP must not run after access hash mismatch")

    with pytest.raises(RouteMismatchError):
        OpenAICompatibleRunner(
            config, mcp_transport=mcp, http_transport=NeverHTTP()
        ).run([{"role": "user", "content": "hash check"}])
    assert mcp.closed is True


def test_real_local_http_disallowed_tool_is_rejected_before_mcp_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = _test_credential("tool", "boundary", "secret")
    state = FakeProviderState(
        model="deepseek-chat",
        expected_api_key=secret,
        tool_name="send_message",
    )
    with fake_provider(state) as endpoint:
        raw = json.dumps(
            {"api_key": secret, "endpoint": endpoint},
            sort_keys=True,
            separators=(",", ":"),
        )
        access = _bound_access(
            scope="tool-e2e",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-deepseek",
            endpoint=endpoint,
            api_key=secret,
            credential_fingerprint_sha256=sha256_bytes(raw.encode()),
        )
        monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
        config = RunnerConfig(
            project_root=tmp_path,
            scope="tool-e2e",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-deepseek",
            model="deepseek-chat",
            allowed_tools=("bridge_status",),
        )
        mcp = _mcp_for_endpoint(config=config, endpoint=endpoint, api_key=secret)
        _bind_fake_connection(mcp, config=config, access=access)
        with pytest.raises(ToolCallError):
            OpenAICompatibleRunner(config, mcp_transport=mcp).run(
                [{"role": "user", "content": "attempt write"}]
            )
    called_tools = [
        params.get("name")
        for method, params in mcp.calls
        if method == "tools/call"
    ]
    assert called_tools == ["list_provider_connections"]
    assert mcp.closed is True


def test_real_local_http_timeout_is_redacted_and_mcp_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = _test_credential("timeout", "secret")
    state = FakeProviderState(
        model="deepseek-chat",
        expected_api_key=secret,
        delay_seconds=0.2,
    )
    with fake_provider(state) as endpoint:
        raw = json.dumps(
            {"api_key": secret, "endpoint": endpoint},
            sort_keys=True,
            separators=(",", ":"),
        )
        access = _bound_access(
            scope="timeout-e2e",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-deepseek",
            endpoint=endpoint,
            api_key=secret,
            credential_fingerprint_sha256=sha256_bytes(raw.encode()),
        )
        monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
        config = RunnerConfig(
            project_root=tmp_path,
            scope="timeout-e2e",
            connection_id="relay",
            route_class="relay",
            provider_id="relay-deepseek",
            model="deepseek-chat",
            timeout_seconds=0.03,
            max_http_attempts=1,
            allowed_tools=("bridge_status",),
        )
        mcp = _mcp_for_endpoint(config=config, endpoint=endpoint, api_key=secret)
        _bind_fake_connection(mcp, config=config, access=access)
        with pytest.raises(ProviderHTTPError) as error:
            OpenAICompatibleRunner(config, mcp_transport=mcp).run(
                [{"role": "user", "content": "timeout prompt"}]
            )
    assert mcp.closed is True
    assert secret not in str(error.value)
    assert endpoint not in str(error.value)


def test_cancellation_during_retry_backoff_stops_before_second_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = threading.Event()

    class CancellingTransport:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, *_args: Any, **_kwargs: Any) -> HTTPResponse:
            self.calls += 1
            token.set()
            return HTTPResponse(status=503, body=b'{}')

    endpoint = "http://127.0.0.1:8765/v1"
    raw = json.dumps(
        {"api_key": _test_credential("cancel", "secret"), "endpoint": endpoint},
        sort_keys=True,
        separators=(",", ":"),
    )
    access = _bound_access(
        scope="cancel-e2e",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-deepseek",
        endpoint=endpoint,
        api_key=_test_credential("cancel", "secret"),
        credential_fingerprint_sha256=sha256_bytes(raw.encode()),
    )
    monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)
    config = RunnerConfig(
        project_root=tmp_path,
        scope="cancel-e2e",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-deepseek",
        model="deepseek-chat",
        retry_backoff_seconds=5,
        allowed_tools=("bridge_status",),
    )
    mcp = _mcp_for_endpoint(
        config=config,
        endpoint=endpoint,
        api_key=_test_credential("cancel", "secret"),
    )
    _bind_fake_connection(mcp, config=config, access=access)
    transport = CancellingTransport()

    with pytest.raises(RunCancelledError):
        OpenAICompatibleRunner(
            config,
            mcp_transport=mcp,
            http_transport=transport,
            cancellation_token=token,
        ).run([{"role": "user", "content": "cancel me"}])
    assert transport.calls == 1
    assert mcp.closed is True


def test_post_idempotency_key_is_stable_only_within_one_logical_request(
    tmp_path: Path,
) -> None:
    body = {
        "model": "model-one",
        "messages": [{"role": "user", "content": "identical request"}],
    }
    config = RunnerConfig(
        project_root=tmp_path,
        scope="idempotency-test",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        session_id="fixed-session",
        max_http_attempts=2,
        retry_backoff_seconds=0,
    )

    def issue_logical_post() -> list[str]:
        class RetryOnceTransport:
            def __init__(self) -> None:
                self.keys: list[str] = []

            def request(
                self,
                _method: str,
                _url: str,
                *,
                headers: Mapping[str, str],
                body: bytes | None,
                timeout: float,
            ) -> HTTPResponse:
                assert body is not None
                assert timeout > 0
                self.keys.append(headers["Idempotency-Key"])
                status = 503 if len(self.keys) == 1 else 200
                return HTTPResponse(status=status, body=b"{}")

        transport = RetryOnceTransport()
        runner = OpenAICompatibleRunner(
            config,
            http_transport=transport,
            mcp_transport=object(),
        )
        runner._provider_json(
            "POST",
            "https://relay.example/v1/chat/completions",
            api_key=_test_credential("provider", "secret"),
            body=body,
        )
        return transport.keys

    first = issue_logical_post()
    second = issue_logical_post()
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    assert len(first) == 2
    assert len(second) == 2
    assert first[0] == first[1]
    assert second[0] == second[1]
    assert first[0] != second[0]
    assert first[0] != sha256_bytes(encoded)


def test_programming_error_from_http_transport_is_not_retried(tmp_path: Path) -> None:
    class ProgrammingErrorTransport:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, *_args: Any, **_kwargs: Any) -> HTTPResponse:
            self.calls += 1
            raise ValueError("transport programming error")

    transport = ProgrammingErrorTransport()
    config = RunnerConfig(
        project_root=tmp_path,
        scope="programming-error-test",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        max_http_attempts=3,
        retry_backoff_seconds=0,
    )
    runner = OpenAICompatibleRunner(
        config,
        http_transport=transport,
        mcp_transport=object(),
    )

    with pytest.raises((ValueError, ProviderHTTPError)):
        runner._provider_json(
            "POST",
            "https://relay.example/v1/chat/completions",
            api_key=_test_credential("provider", "secret"),
            body={"model": "model-one", "messages": []},
        )
    assert transport.calls == 1


def test_direct_model_discovery_uses_bound_wcm_access_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "https://relay.example"
    secret = _test_credential("direct", "model", "discovery", "secret")
    access = _bound_access(
        scope="direct-models",
        connection_id="relay-grok",
        route_class="relay",
        provider_id="relay-grok",
        endpoint=endpoint,
        api_key=secret,
        credential_fingerprint_sha256="d" * 64,
    )
    monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)

    class ModelTransport:
        def __init__(self) -> None:
            self.request_record: dict[str, Any] | None = None

        def request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str],
            body: bytes | None,
            timeout: float,
        ) -> HTTPResponse:
            self.request_record = {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
            return HTTPResponse(
                status=200,
                body=json.dumps(
                    {
                        "object": "list",
                        "data": [
                            {"id": "grok-4.5", "object": "model"},
                            {"id": "grok-4.5", "object": "model"},
                            {"id": "grok-4.6", "object": "model"},
                        ],
                    }
                ).encode("utf-8"),
            )

    transport = ModelTransport()
    result = discover_provider_models(
        scope="direct-models",
        connection_id="relay-grok",
        route_class="relay",
        provider_id="relay-grok",
        timeout_seconds=7,
        http_transport=transport,
    )

    assert isinstance(result, ProviderModelRegistry)
    assert result.models == ("grok-4.5", "grok-4.6")
    assert result.endpoint_sha256 == access.endpoint_sha256
    assert result.credential_version_sha256 == access.credential_version_sha256
    assert transport.request_record == {
        "method": "GET",
        "url": "https://relay.example/v1/models",
        "headers": {
            "Accept": "application/json",
            "Authorization": f"Bearer {secret}",
            "User-Agent": f"peerbridge-mcp/{__version__}",
        },
        "body": None,
        "timeout": 7.0,
    }
    assert secret not in repr(result)
    assert endpoint not in repr(result)


def test_direct_model_discovery_hash_ignores_order_duplicates_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = _bound_access(
        scope="stable-models",
        connection_id="relay",
        route_class="relay",
        provider_id="relay",
        endpoint="https://relay.example",
        api_key=_test_credential("stable", "secret"),
        credential_fingerprint_sha256="f" * 64,
    )
    monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)

    class ModelTransport:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def request(self, *_args: Any, **_kwargs: Any) -> HTTPResponse:
            return HTTPResponse(status=200, body=json.dumps(self.payload).encode())

    first = discover_provider_models(
        scope="stable-models",
        connection_id="relay",
        route_class="relay",
        provider_id="relay",
        http_transport=ModelTransport(
            {"request_id": "volatile-a", "data": [{"id": "z"}, {"id": "a"}]}
        ),
    )
    second = discover_provider_models(
        scope="stable-models",
        connection_id="relay",
        route_class="relay",
        provider_id="relay",
        http_transport=ModelTransport(
            {
                "request_id": "volatile-b",
                "data": [{"id": "a"}, {"id": "z"}, {"id": "a"}],
            }
        ),
    )

    assert first.models == second.models == ("a", "z")
    assert first.registry_sha256 == second.registry_sha256


def test_direct_model_discovery_rejects_malformed_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = _bound_access(
        scope="direct-models-bad",
        connection_id="relay-kimi",
        route_class="relay",
        provider_id="relay-kimi",
        endpoint="https://relay.example/v1",
        api_key=_test_credential("hidden", "secret"),
        credential_fingerprint_sha256="e" * 64,
    )
    monkeypatch.setattr(credentials, "load_provider_access", lambda **_: access)

    class InvalidRegistryTransport:
        def request(self, *_args: Any, **_kwargs: Any) -> HTTPResponse:
            return HTTPResponse(status=200, body=b'{"data":[{"not_id":"x"}]}')

    with pytest.raises(RouteMismatchError):
        discover_provider_models(
            scope="direct-models-bad",
            connection_id="relay-kimi",
            route_class="relay",
            provider_id="relay-kimi",
            http_transport=InvalidRegistryTransport(),
        )


def test_total_tool_call_budget_rejects_batch_before_partial_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "https://relay.example/v1"
    api_key = _test_credential("tool", "budget", "secret")
    config = RunnerConfig(
        project_root=tmp_path,
        scope="tool-count-budget",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        max_tool_calls=2,
        max_cumulative_tool_result_chars=10_000,
        allowed_tools=("bridge_status",),
    )
    mcp = BudgetMCPTransport(
        scope=config.scope,
        connection_id=config.connection_id,
        route_class=config.route_class,
        endpoint=endpoint,
        api_key=api_key,
        expected_timeout=float(config.timeout_seconds),
        tool_result={"value": "x"},
    )
    _patch_remote_access(
        monkeypatch,
        config=config,
        endpoint=endpoint,
        api_key=api_key,
        mcp=mcp,
    )
    tool_calls = [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": "bridge_status", "arguments": "{}"},
        }
        for call_id in ("call-one", "call-two")
    ]
    http = ScriptedOpenAITransport(
        model=config.model,
        messages=[
            {"role": "assistant", "content": None, "tool_calls": tool_calls},
            {"role": "assistant", "content": "should not be reached"},
        ],
    )

    failure: RunnerError | None = None
    try:
        OpenAICompatibleRunner(
            config,
            mcp_transport=mcp,
            http_transport=http,
        ).run([{"role": "user", "content": "inspect"}])
    except RunnerError as exc:
        failure = exc

    assert mcp.executed_tools == []
    assert failure is not None


def test_cumulative_tool_result_budget_blocks_next_batch_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "https://relay.example/v1"
    api_key = _test_credential("result", "budget", "secret")
    tool_result = {"value": "x"}
    result_chars = len(
        json.dumps(
            tool_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    config = RunnerConfig(
        project_root=tmp_path,
        scope="tool-result-budget",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        max_tool_calls=10,
        allowed_tools=("bridge_status",),
    )
    mcp = BudgetMCPTransport(
        scope=config.scope,
        connection_id=config.connection_id,
        route_class=config.route_class,
        endpoint=endpoint,
        api_key=api_key,
        expected_timeout=float(config.timeout_seconds),
        tool_result=tool_result,
    )
    _patch_remote_access(
        monkeypatch,
        config=config,
        endpoint=endpoint,
        api_key=api_key,
        mcp=mcp,
    )
    registry_result = {
        "structuredContent": {"connections": [mcp.connection], "count": 1}
    }
    registry_chars = len(
        json.dumps(
            registry_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    object.__setattr__(
        config,
        "max_cumulative_tool_result_chars",
        registry_chars + result_chars,
    )

    def assistant_tool_call(call_id: str) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "bridge_status", "arguments": "{}"},
                }
            ],
        }

    http = ScriptedOpenAITransport(
        model=config.model,
        messages=[
            assistant_tool_call("call-one"),
            assistant_tool_call("call-two"),
            {"role": "assistant", "content": "should not be reached"},
        ],
    )

    failure: RunnerError | None = None
    try:
        OpenAICompatibleRunner(
            config,
            mcp_transport=mcp,
            http_transport=http,
        ).run([{"role": "user", "content": "inspect"}])
    except RunnerError as exc:
        failure = exc

    assert mcp.executed_tools == ["bridge_status"]
    assert failure is not None


def test_tool_call_accepts_strict_object_arguments_with_audited_dialect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "https://relay.example/v1"
    api_key = _test_credential("object", "argument", "secret")
    config = RunnerConfig(
        project_root=tmp_path,
        scope="object-argument-dialect",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        allowed_tools=("bridge_status",),
    )
    mcp = BudgetMCPTransport(
        scope=config.scope,
        connection_id=config.connection_id,
        route_class=config.route_class,
        endpoint=endpoint,
        api_key=api_key,
        expected_timeout=float(config.timeout_seconds),
        tool_result={"ok": True},
    )
    _patch_remote_access(
        monkeypatch,
        config=config,
        endpoint=endpoint,
        api_key=api_key,
        mcp=mcp,
    )
    http = ScriptedOpenAITransport(
        model=config.model,
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-object",
                        "type": "function",
                        "function": {
                            "name": "bridge_status",
                            "arguments": {},
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "done"},
        ],
    )

    result = OpenAICompatibleRunner(
        config,
        mcp_transport=mcp,
        http_transport=http,
    ).run([{"role": "user", "content": "inspect"}])

    assert mcp.executed_tools == ["bridge_status"]
    assert result.receipt["tool_calls"][0]["arguments_format"] == "json-object"


def test_tool_round_limit_allows_one_tool_then_forces_tool_free_final_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "https://relay.example/v1"
    api_key = _test_credential("tool", "round", "finalization", "secret")
    config = RunnerConfig(
        project_root=tmp_path,
        scope="tool-round-finalization",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        max_tool_rounds=1,
        allowed_tools=("bridge_status",),
    )
    mcp = BudgetMCPTransport(
        scope=config.scope,
        connection_id=config.connection_id,
        route_class=config.route_class,
        endpoint=endpoint,
        api_key=api_key,
        expected_timeout=float(config.timeout_seconds),
        tool_result={"ok": True},
    )
    _patch_remote_access(
        monkeypatch,
        config=config,
        endpoint=endpoint,
        api_key=api_key,
        mcp=mcp,
    )
    http = ScriptedOpenAITransport(
        model=config.model,
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-status",
                        "type": "function",
                        "function": {"name": "bridge_status", "arguments": "{}"},
                    }
                ],
            },
            {"role": "assistant", "content": "done"},
        ],
    )

    result = OpenAICompatibleRunner(
        config,
        mcp_transport=mcp,
        http_transport=http,
    ).run([{"role": "user", "content": "inspect"}])

    assert result.content == "done"
    assert mcp.executed_tools == ["bridge_status"]
    assert "tools" in http.request_bodies[0]
    assert "tools" not in http.request_bodies[1]


def test_mailbox_response_only_fallback_recovers_from_disallowed_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "https://relay.example/v1"
    api_key = _test_credential("response", "only", "fallback", "secret")
    config = RunnerConfig(
        project_root=tmp_path,
        scope="response-only-fallback",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        allowed_tools=("bridge_status",),
        response_only_fallback_on_tool_error=True,
    )
    mcp = FakeMCPTransport(
        scope=config.scope,
        connection_id=config.connection_id,
        route_class=config.route_class,
        endpoint=endpoint,
        api_key=api_key,
        expected_timeout=float(config.timeout_seconds),
    )
    _patch_remote_access(
        monkeypatch,
        config=config,
        endpoint=endpoint,
        api_key=api_key,
        mcp=mcp,
    )
    http = ScriptedOpenAITransport(
        model=config.model,
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "Read-only review completed without live tools.",
            },
        ],
    )

    result = OpenAICompatibleRunner(
        config,
        mcp_transport=mcp,
        http_transport=http,
    ).run([{"role": "user", "content": "Review this supplied proposal."}])

    assert result.content == "Read-only review completed without live tools."
    assert "tools" in http.request_bodies[0]
    assert "tools" not in http.request_bodies[1]
    assert http.request_bodies[1]["messages"][0]["role"] == "system"
    fallback = result.receipt["response_only_fallback"]
    assert fallback == {
        "used": True,
        "reason_code": "tool_call_validation_failed",
        "model_visible_tools": False,
        "instruction_sha256": runner_module.stable_sha256(
            runner_module._RESPONSE_ONLY_FALLBACK_INSTRUCTION
        ),
    }
    assert result.receipt["raw_content_recorded"] is False
    assert result.receipt["credential_contents_recorded"] is False


@pytest.mark.parametrize(
    ("arguments", "expected_format"),
    [
        ("{{}}", "gateway-double-brace-empty-object"),
        ("{}{}", "gateway-concatenated-empty-objects"),
    ],
)
def test_tool_call_accepts_exact_gateway_empty_dialect_only_for_live_zero_arg_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: str,
    expected_format: str,
) -> None:
    endpoint = "https://relay.example/v1"
    api_key = _test_credential("double", "brace", "test", "secret")
    config = RunnerConfig(
        project_root=tmp_path,
        scope="double-brace-dialect",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        allowed_tools=("bridge_status",),
    )
    mcp = BudgetMCPTransport(
        scope=config.scope,
        connection_id=config.connection_id,
        route_class=config.route_class,
        endpoint=endpoint,
        api_key=api_key,
        expected_timeout=float(config.timeout_seconds),
        tool_result={"ok": True},
    )
    _patch_remote_access(
        monkeypatch,
        config=config,
        endpoint=endpoint,
        api_key=api_key,
        mcp=mcp,
    )
    http = ScriptedOpenAITransport(
        model=config.model,
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-double-brace",
                        "type": "function",
                        "function": {
                            "name": "bridge_status",
                            "arguments": arguments,
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "done"},
        ],
    )

    result = OpenAICompatibleRunner(
        config,
        mcp_transport=mcp,
        http_transport=http,
    ).run([{"role": "user", "content": "inspect"}])

    assert mcp.executed_tools == ["bridge_status"]
    call = result.receipt["tool_calls"][0]
    assert call["arguments_format"] == expected_format
    assert call["arguments_sha256"] == stable_sha256({})


@pytest.mark.parametrize(
    "arguments", ["{{}}", "{}{}", " {{}}", "{{}} ", " {}{}", "{}{} ", "{{ }}", "{{}{}}"]
)
def test_gateway_double_brace_requires_exact_value_and_bound_zero_arg_schema(
    tmp_path: Path, arguments: str
) -> None:
    config = RunnerConfig(
        project_root=tmp_path,
        scope="double-brace-rejection",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        allowed_tools=("bridge_status",),
    )
    runner = OpenAICompatibleRunner(
        config,
        mcp_transport=FakeMCPTransport(
            scope=config.scope,
            connection_id=config.connection_id,
            route_class=config.route_class,
            endpoint="https://relay.example/v1",
            api_key=_test_credential("unused", "test", "secret"),
        ),
    )
    if arguments not in {"{{}}", "{}{}"}:
        runner._tool_input_schemas["bridge_status"] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-rejected",
                "type": "function",
                "function": {"name": "bridge_status", "arguments": arguments},
            }
        ],
    }

    with pytest.raises(ToolCallError, match="non-object JSON tool arguments"):
        runner._tool_calls(message)


def test_gateway_empty_dialect_accepts_schema_with_only_optional_properties(
    tmp_path: Path
) -> None:
    config = RunnerConfig(
        project_root=tmp_path,
        scope="optional-empty-schema",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        allowed_tools=("list_rooms",),
    )
    runner = OpenAICompatibleRunner(
        config,
        mcp_transport=FakeMCPTransport(
            scope=config.scope,
            connection_id=config.connection_id,
            route_class=config.route_class,
            endpoint="https://relay.example/v1",
            api_key=_test_credential("unused", "test", "secret"),
        ),
    )
    runner._tool_input_schemas["list_rooms"] = {
        "type": "object",
        "properties": {"include_archived": {"type": "boolean"}},
        "additionalProperties": False,
    }

    calls = runner._tool_calls(
        {
            "tool_calls": [
                {
                    "id": "call-optional",
                    "type": "function",
                    "function": {"name": "list_rooms", "arguments": "{}{}"},
                }
            ]
        }
    )

    assert calls[0]["arguments"] == {}
    assert calls[0]["arguments_format"] == "gateway-concatenated-empty-objects"


def test_gateway_empty_dialect_rejects_schema_with_required_property(
    tmp_path: Path
) -> None:
    config = RunnerConfig(
        project_root=tmp_path,
        scope="required-empty-schema",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        allowed_tools=("room_members",),
    )
    runner = OpenAICompatibleRunner(
        config,
        mcp_transport=FakeMCPTransport(
            scope=config.scope,
            connection_id=config.connection_id,
            route_class=config.route_class,
            endpoint="https://relay.example/v1",
            api_key=_test_credential("unused", "test", "secret"),
        ),
    )
    runner._tool_input_schemas["room_members"] = {
        "type": "object",
        "properties": {"room_id": {"type": "string"}},
        "required": ["room_id"],
        "additionalProperties": False,
    }

    with pytest.raises(ToolCallError, match="non-object JSON tool arguments"):
        runner._tool_calls(
            {
                "tool_calls": [
                    {
                        "id": "call-required",
                        "type": "function",
                        "function": {"name": "room_members", "arguments": "{}{}"},
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ([], "unsupported tool argument type: list"),
        (1, "unsupported tool argument type: int"),
        (True, "unsupported tool argument type: bool"),
        (None, "unsupported tool argument type: NoneType"),
        ("not-json", "non-object JSON tool arguments"),
        ("   ", "empty tool arguments"),
        ("[]", "non-object JSON tool arguments"),
    ],
)
def test_tool_call_rejects_non_object_argument_dialects(
    tmp_path: Path, arguments: object, error: str
) -> None:
    config = RunnerConfig(
        project_root=tmp_path,
        scope="bad-argument-dialect",
        connection_id="relay",
        route_class="relay",
        provider_id="relay-provider",
        model="model-one",
        allowed_tools=("bridge_status",),
    )
    runner = OpenAICompatibleRunner(
        config,
        mcp_transport=FakeMCPTransport(
            scope=config.scope,
            connection_id=config.connection_id,
            route_class=config.route_class,
            endpoint="https://relay.example/v1",
            api_key=_test_credential("unused", "test", "secret"),
        ),
    )
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-bad",
                "type": "function",
                "function": {"name": "bridge_status", "arguments": arguments},
            }
        ],
    }

    with pytest.raises(ToolCallError, match=error):
        runner._tool_calls(message)
