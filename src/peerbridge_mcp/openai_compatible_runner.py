"""Secure OpenAI-compatible inference with a real PeerBridge MCP tool loop.

Provider credentials remain in Windows Credential Manager and are loaded only for
the lifetime of one run.  Receipts intentionally contain identities and hashes,
never prompts, completions, tool arguments/results, endpoints, or API keys.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

from . import __version__, credentials
from .bridge import ROUTE_CLASSES, SAFE_ID, sha256_bytes, stable_sha256
from .child_environment import build_local_child_environment
from .protocol import PROTOCOL_VERSION
from .process_control import (
    attach_process_tree,
    process_group_popen_kwargs,
    release_process_tree,
    terminate_process_tree,
)
from .resource_guard import ResourceGuardError, provider_runtime_slot
from .server import READ_ONLY_TOOLS
from .usage import aggregate_usage, normalize_provider_usage


RECEIPT_SCHEMA = "peerbridge.openai-compatible-run.v1"
CLIENT_NAME = "openai-compatible-runner"
REGISTRY_TOOL = "list_provider_connections"
# Hashing reads file bytes but never returns their contents.  Keep it audited by
# the MCP server while allowing the model runner to use it for provenance checks.
MODEL_SAFE_TOOLS = frozenset(READ_ONLY_TOOLS) | {"hash_artifact"}
DEFAULT_ALLOWED_TOOLS = (
    "bridge_status",
    "hash_artifact",
    "list_memories",
    "list_rooms",
    "list_route_profiles",
    "read_memory",
    "room_members",
    "verify_audit_chain",
)
_MAX_HTTP_BODY_BYTES = 16 * 1024 * 1024
_MAX_TOOL_ARGUMENT_CHARS = 1_000_000
_MAX_TOOL_RESULT_CHARS = 4_000_000
_MAX_CUMULATIVE_TOOL_ARGUMENT_CHARS = 8_000_000
_MAX_CUMULATIVE_TOOL_RESULT_CHARS = 32_000_000
_RESPONSE_ONLY_FALLBACK_INSTRUCTION = (
    "PeerBridge has disabled model-visible tools for this safety fallback. "
    "Answer the addressed request directly using only the supplied conversation. "
    "If live PeerBridge evidence would be required, state that limitation instead "
    "of inventing facts. Do not request or simulate any tool call."
)


class RunnerError(RuntimeError):
    """A fail-closed runner error whose message is safe to display."""


class ConfigurationError(RunnerError):
    """Runner configuration is unsafe or internally inconsistent."""


class CredentialUnavailableError(RunnerError):
    """The required local WCM credential cannot be used."""


class RouteMismatchError(RunnerError):
    """Observed redacted route evidence does not match the requested route."""


class ProviderHTTPError(RunnerError):
    """The provider failed without exposing its URL or response body."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class MCPTransportError(RunnerError):
    """The local MCP subprocess or response was invalid."""


class ToolCallError(RunnerError):
    """A model-requested tool call failed validation."""


class MaxToolRoundsError(RunnerError):
    """The model requested more tool rounds than the configured limit."""


class RunCancelledError(RunnerError):
    """The caller cancelled the run at a cooperative provider/tool boundary."""


class ResourceUnavailableError(RunnerError):
    """The host cannot safely start another provider runtime right now."""


@contextlib.contextmanager
def provider_runtime_admission(*, already_admitted: bool = False) -> Iterator[None]:
    """Use the shared ResourceGuard path unless a supervisor holds the slot."""
    if already_admitted:
        yield
        return
    try:
        with provider_runtime_slot(timeout=0.0):
            yield
    except ResourceGuardError as exc:
        raise ResourceUnavailableError(str(exc)) from None


@dataclass(frozen=True, repr=False)
class HTTPResponse:
    """A bounded HTTP response whose repr never includes its body."""

    status: int
    body: bytes

    def __repr__(self) -> str:
        return f"HTTPResponse(status={self.status}, body_bytes={len(self.body)})"


class HTTPTransport(Protocol):
    """Injectable byte-oriented HTTP transport."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse: ...


class MCPTransport(Protocol):
    """Injectable request-oriented MCP transport."""

    def start(self) -> None: ...

    def request(
        self, method: str, params: Mapping[str, Any], *, timeout: float
    ) -> Mapping[str, Any]: ...

    def notify(self, method: str, params: Mapping[str, Any]) -> None: ...

    def close(self) -> None: ...


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Turn every redirect into a terminal HTTP response without forwarding headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class StdlibHTTPTransport:
    """OpenAI-compatible HTTP transport implemented only with urllib."""

    def __init__(self) -> None:
        self._opener = urllib_request.build_opener(_NoRedirectHandler())

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        outgoing = urllib_request.Request(
            url=url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with self._opener.open(outgoing, timeout=timeout) as response:
                status = int(response.status)
                payload = response.read(_MAX_HTTP_BODY_BYTES + 1)
        except urllib_error.HTTPError as exc:
            status = int(exc.code)
            exc.close()
            raise ProviderHTTPError(
                f"provider returned HTTP status {status}",
                status_code=status,
                retryable=status in {408, 429, 500, 502, 503, 504},
            ) from None
        except (OSError, TimeoutError, urllib_error.URLError):
            raise ProviderHTTPError(
                "provider HTTP request failed", retryable=True
            ) from None
        if len(payload) > _MAX_HTTP_BODY_BYTES:
            raise ProviderHTTPError("provider response exceeded the size limit")
        return HTTPResponse(status=status, body=payload)


class StdioMCPTransport:
    """Line-delimited JSON-RPC transport for ``python -m peerbridge_mcp serve``."""

    def __init__(self, command: Sequence[str], *, cwd: Path) -> None:
        self._command = tuple(str(item) for item in command)
        self._cwd = Path(cwd)
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._request_id = 0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        state = "running" if self._process and self._process.poll() is None else "stopped"
        return f"StdioMCPTransport(state={state!r})"

    def start(self) -> None:
        if self._process is not None:
            raise MCPTransportError("MCP transport has already been started")
        try:
            self._process = subprocess.Popen(
                list(self._command),
                cwd=self._cwd,
                env=build_local_child_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
                **process_group_popen_kwargs(),
            )
            attach_process_tree(self._process)
        except OSError:
            raise MCPTransportError("unable to start the local MCP server") from None
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._responses.put(None)
            return
        try:
            for line in process.stdout:
                self._responses.put(line)
        finally:
            self._responses.put(None)

    def _write(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise MCPTransportError("local MCP server is not running")
        try:
            process.stdin.write(_json_dumps(payload) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, UnicodeError):
            raise MCPTransportError("local MCP server transport failed") from None

    def request(
        self, method: str, params: Mapping[str, Any], *, timeout: float
    ) -> Mapping[str, Any]:
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            try:
                line = self._responses.get(timeout=timeout)
            except queue.Empty:
                raise MCPTransportError("local MCP server request timed out") from None
            if line is None:
                raise MCPTransportError("local MCP server exited unexpectedly")
            response = _parse_json_object(line.encode("utf-8"), "MCP response")
            if response.get("id") != request_id:
                raise MCPTransportError("local MCP response ID mismatch")
            if "error" in response:
                raise MCPTransportError(f"local MCP method failed: {method}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise MCPTransportError("local MCP result is not an object")
            return result

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        with self._lock:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": dict(params),
                }
            )

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process, wait_seconds=2)
        else:
            release_process_tree(process)
        if self._reader is not None:
            self._reader.join(timeout=2)


@dataclass(frozen=True, repr=False)
class RunnerConfig:
    """Non-secret route and execution configuration."""

    project_root: Path
    scope: str
    connection_id: str
    route_class: str
    provider_id: str
    model: str
    response_model: str | None = None
    reasoning_mode: str | None = None
    route_profile_id: str | None = None
    route_profile_sha256: str | None = None
    room_id: str = "lobby"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    agent_id: str = "openai-compatible-runner"
    db_path: Path | None = None
    timeout_seconds: float = 60.0
    max_http_attempts: int = 3
    retry_backoff_seconds: float = 0.25
    max_tool_rounds: int = 4
    max_tool_calls: int = 16
    max_cumulative_tool_argument_chars: int = 2_000_000
    max_cumulative_tool_result_chars: int = 8_000_000
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    response_only_fallback_on_tool_error: bool = False

    def __repr__(self) -> str:
        return (
            "RunnerConfig("
            f"scope={self.scope!r}, connection_id={self.connection_id!r}, "
            f"route_class={self.route_class!r}, provider_id={self.provider_id!r}, "
            f"model={self.model!r}, response_model={self.response_model!r}, "
            f"reasoning_mode={self.reasoning_mode!r}, "
            f"route_profile_id={self.route_profile_id!r}, "
            f"room_id={self.room_id!r}, "
            f"session_id={self.session_id!r})"
        )


@dataclass(frozen=True, repr=False)
class InferenceResult:
    """Final model message plus a content-free, secret-free receipt."""

    assistant_message: Mapping[str, Any]
    receipt: Mapping[str, Any]

    @property
    def content(self) -> Any:
        return self.assistant_message.get("content")

    def __repr__(self) -> str:
        receipt_sha = str(self.receipt.get("receipt_sha256") or "")
        return f"InferenceResult(receipt_sha256={receipt_sha!r})"


@dataclass(frozen=True, repr=False)
class ProviderModelRegistry:
    """Sanitized direct-API model discovery result.

    The endpoint and API key never leave the local adapter.  Callers receive only
    model identifiers and hashes that can safely be displayed or audited.
    """

    models: tuple[str, ...]
    registry_sha256: str
    endpoint_sha256: str
    credential_version_sha256: str

    def __repr__(self) -> str:
        return (
            "ProviderModelRegistry("
            f"model_count={len(self.models)}, "
            f"registry_sha256={self.registry_sha256!r}, "
            f"endpoint_sha256={self.endpoint_sha256!r}, "
            f"credential_version_sha256={self.credential_version_sha256!r})"
        )


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise RunnerError("payload is not strict JSON") from None


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def _parse_json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RunnerError(f"{label} is not valid strict JSON") from None
    if not isinstance(value, dict):
        raise RunnerError(f"{label} is not a JSON object")
    return value


def _safe_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise ConfigurationError(f"{label} is not a safe identifier")
    return text


def _normalized_model(value: Any) -> str:
    model = str(value or "").strip()
    if not model or len(model) > 500 or any(ord(char) < 32 for char in model):
        raise ConfigurationError("model is invalid")
    return model


def _runtime_model_id(model: str) -> str:
    if SAFE_ID.fullmatch(model):
        return model
    return f"model-sha256:{stable_sha256(model)[:32]}"


def _api_base_url(endpoint: str) -> str:
    """Return an OpenAI API base without rewriting an explicit provider path.

    A host-only endpoint uses the conventional ``/v1`` base. A non-empty path
    is treated as an explicit API base and is preserved, which is required for
    compatible providers such as Gemini's ``/v1beta/openai`` endpoint.
    """
    normalized = credentials.normalize_endpoint(endpoint)
    parsed = urlsplit(normalized)
    if parsed.scheme == "http":
        host = parsed.hostname or ""
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host.lower() == "localhost"
        if not local:
            raise CredentialUnavailableError("remote provider endpoint must use HTTPS")
    elif parsed.scheme != "https":
        raise CredentialUnavailableError("provider endpoint scheme is unsupported")
    return normalized if parsed.path.rstrip("/") else normalized + "/v1"


def discover_provider_models(
    *,
    scope: str,
    connection_id: str,
    route_class: str,
    provider_id: str | None = None,
    timeout_seconds: float = 20.0,
    http_transport: HTTPTransport | None = None,
) -> ProviderModelRegistry:
    """Read a saved credential locally and query its OpenAI-compatible model list.

    This is intentionally independent of MCP so neither the endpoint nor the API
    key can enter an MCP request, SQLite row, message, or receipt.
    """

    safe_scope = _safe_id(scope, "scope")
    safe_connection = _safe_id(connection_id, "connection_id")
    route = str(route_class or "").strip().lower()
    if route not in ROUTE_CLASSES:
        raise ConfigurationError("route_class must be official, relay or local")
    safe_provider = _safe_id(provider_id or safe_connection, "provider_id")
    if isinstance(timeout_seconds, bool) or not 0 < float(timeout_seconds) <= 300:
        raise ConfigurationError("timeout_seconds must be between 0 and 300")
    try:
        access = credentials.load_provider_access(
            scope=safe_scope,
            connection_id=safe_connection,
            route_class=route,
            provider_id=safe_provider,
        )
        api_base = _api_base_url(access.endpoint)
    except credentials.CredentialStoreError:
        raise CredentialUnavailableError(
            "required Windows Credential Manager credential is unavailable"
        ) from None

    headers = {
        "Accept": "application/json",
        "User-Agent": f"peerbridge-mcp/{__version__}",
    }
    if access.api_key is not None:
        headers["Authorization"] = f"Bearer {access.api_key}"
    transport = http_transport or StdlibHTTPTransport()
    try:
        response = transport.request(
            "GET",
            f"{api_base}/models",
            headers=headers,
            body=None,
            timeout=float(timeout_seconds),
        )
    except ProviderHTTPError:
        raise
    except Exception:
        raise ProviderHTTPError("provider model discovery failed") from None
    if not isinstance(response, HTTPResponse):
        raise ProviderHTTPError("provider HTTP transport returned an invalid response")
    if not 200 <= response.status < 300:
        raise ProviderHTTPError(
            f"provider returned HTTP status {response.status}",
            status_code=response.status,
            retryable=response.status in {408, 429, 500, 502, 503, 504},
        )
    if len(response.body) > _MAX_HTTP_BODY_BYTES:
        raise ProviderHTTPError("provider response exceeded the size limit")
    try:
        payload = _parse_json_object(response.body, "provider response")
    except RunnerError:
        raise ProviderHTTPError("provider returned malformed JSON") from None
    models, registry_sha256 = _canonical_model_registry(payload)
    return ProviderModelRegistry(
        models=models,
        registry_sha256=registry_sha256,
        endpoint_sha256=access.endpoint_sha256,
        credential_version_sha256=access.credential_version_sha256,
    )


def _canonical_model_registry(payload: Mapping[str, Any]) -> tuple[tuple[str, ...], str]:
    """Return a stable identity over advertised model IDs only.

    Provider payload order and unrelated metadata are not identity.  Sorting and
    deduplicating the IDs keeps receipts reproducible without weakening the
    requirement that the requested model must be explicitly advertised.
    """

    data = payload.get("data")
    if not isinstance(data, list) or len(data) > 10_000:
        raise RouteMismatchError("provider model registry is malformed")
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise RouteMismatchError("provider model registry is malformed")
        seen.add(_normalized_model(item["id"]))
    models = tuple(sorted(seen))
    if not models:
        raise RouteMismatchError("provider advertised no models")
    return models, stable_sha256({"models": models})


def _validated_config(config: RunnerConfig) -> tuple[str, ...]:
    if not Path(config.project_root).resolve().is_dir():
        raise ConfigurationError("project_root does not exist")
    _safe_id(config.scope, "scope")
    _safe_id(config.connection_id, "connection_id")
    _safe_id(config.provider_id, "provider_id")
    _normalized_model(config.model)
    if config.response_model is not None:
        _normalized_model(config.response_model)
    if config.reasoning_mode is not None:
        _safe_id(config.reasoning_mode, "reasoning_mode")
    if config.route_profile_id is not None:
        _safe_id(config.route_profile_id, "route_profile_id")
    if config.route_profile_sha256 is not None:
        profile_sha = str(config.route_profile_sha256)
        if len(profile_sha) != 64 or any(
            character not in "0123456789abcdef" for character in profile_sha
        ):
            raise ConfigurationError("route_profile_sha256 is not a SHA-256 digest")
    if (config.route_profile_id is None) != (config.route_profile_sha256 is None):
        raise ConfigurationError("route profile identity is incomplete")
    _safe_id(config.room_id, "room_id")
    _safe_id(config.session_id, "session_id")
    _safe_id(config.agent_id, "agent_id")
    if config.route_class not in ROUTE_CLASSES:
        raise ConfigurationError("route_class must be official, relay or local")
    if not isinstance(config.max_tool_rounds, int) or isinstance(
        config.max_tool_rounds, bool
    ) or config.max_tool_rounds < 0:
        raise ConfigurationError("max_tool_rounds must be a non-negative integer")
    if (
        not isinstance(config.max_tool_calls, int)
        or isinstance(config.max_tool_calls, bool)
        or not 1 <= config.max_tool_calls <= 10_000
    ):
        raise ConfigurationError("max_tool_calls must be between 1 and 10000")
    if (
        not isinstance(config.max_cumulative_tool_argument_chars, int)
        or isinstance(config.max_cumulative_tool_argument_chars, bool)
        or not 1
        <= config.max_cumulative_tool_argument_chars
        <= _MAX_CUMULATIVE_TOOL_ARGUMENT_CHARS
    ):
        raise ConfigurationError("cumulative tool argument budget is invalid")
    if (
        not isinstance(config.max_cumulative_tool_result_chars, int)
        or isinstance(config.max_cumulative_tool_result_chars, bool)
        or not 1
        <= config.max_cumulative_tool_result_chars
        <= _MAX_CUMULATIVE_TOOL_RESULT_CHARS
    ):
        raise ConfigurationError("cumulative tool result budget is invalid")
    if isinstance(config.timeout_seconds, bool) or not 0 < float(
        config.timeout_seconds
    ) <= 3600:
        raise ConfigurationError("timeout_seconds must be between 0 and 3600")
    if (
        not isinstance(config.max_http_attempts, int)
        or isinstance(config.max_http_attempts, bool)
        or not 1 <= config.max_http_attempts <= 10
    ):
        raise ConfigurationError("max_http_attempts must be between 1 and 10")
    if isinstance(config.retry_backoff_seconds, bool) or not 0 <= float(
        config.retry_backoff_seconds
    ) <= 60:
        raise ConfigurationError("retry_backoff_seconds must be between 0 and 60")
    if not isinstance(config.response_only_fallback_on_tool_error, bool):
        raise ConfigurationError(
            "response_only_fallback_on_tool_error must be a boolean"
        )
    tools = tuple(config.allowed_tools)
    if len(tools) != len(set(tools)):
        raise ConfigurationError("allowed_tools contains duplicates")
    invalid = sorted(set(tools) - MODEL_SAFE_TOOLS)
    if invalid:
        raise ConfigurationError("allowed_tools contains a non-read-only MCP tool")
    return tools


def _mcp_command(config: RunnerConfig, transport_tools: Sequence[str]) -> tuple[str, ...]:
    root = Path(config.project_root).resolve()
    command = [
        sys.executable,
        "-m",
        "peerbridge_mcp",
        "serve",
        "--project-root",
        str(root),
        "--agent-id",
        config.agent_id,
        "--scope",
        config.scope,
        "--session-id",
        config.session_id,
        "--client-name",
        CLIENT_NAME,
        "--provider-id",
        config.provider_id,
        "--model-id",
        _runtime_model_id(config.model),
        "--route-class",
        config.route_class,
    ]
    if config.reasoning_mode is not None:
        command.extend(("--reasoning-mode", config.reasoning_mode))
    if config.db_path is not None:
        command.extend(("--db", str(Path(config.db_path).resolve())))
    for tool in transport_tools:
        command.extend(("--allow-tool", tool))
    return tuple(command)


def _mcp_content_payload(result: Mapping[str, Any], label: str) -> dict[str, Any]:
    if result.get("isError") is True:
        raise MCPTransportError(f"{label} returned an error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise MCPTransportError(f"{label} returned malformed content")
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        raise MCPTransportError(f"{label} returned unsupported content")
    text = first.get("text")
    if not isinstance(text, str):
        raise MCPTransportError(f"{label} returned malformed text content")
    try:
        return _parse_json_object(text.encode("utf-8"), label)
    except RunnerError:
        raise MCPTransportError(f"{label} returned malformed JSON content") from None


def _connection_identity(
    registry_result: Mapping[str, Any],
    *,
    config: RunnerConfig,
    access: credentials.ProviderAccess,
) -> dict[str, Any]:
    payload = _mcp_content_payload(registry_result, "provider registry")
    connections = payload.get("connections")
    if not isinstance(connections, list):
        raise RouteMismatchError("provider registry response is malformed")
    matches = [
        item
        for item in connections
        if isinstance(item, dict) and item.get("connection_id") == config.connection_id
    ]
    if len(matches) != 1:
        raise RouteMismatchError("provider connection is missing or ambiguous")
    item = matches[0]
    normalized_endpoint = credentials.normalize_endpoint(access.endpoint)
    expected_target = credentials.credential_target(config.scope, config.connection_id)
    if (
        access.credential_target != expected_target
        or access.endpoint_sha256 != sha256_bytes(normalized_endpoint.encode("utf-8"))
        or not access.descriptor_bound
        or access.descriptor_schema != "peerbridge.provider-credential.v2"
        or access.route_class != config.route_class
        or access.provider_id != config.provider_id
        or access.credential_version_sha256
        != access.credential_fingerprint_sha256
    ):
        raise RouteMismatchError("local provider access identity mismatch")
    if config.route_class == "local":
        if access.api_key is not None or access.secret_present:
            raise RouteMismatchError("local provider route unexpectedly contains a secret")
    elif not isinstance(access.api_key, str) or not access.api_key or not access.secret_present:
        raise RouteMismatchError("remote provider route is missing its local secret")
    expected_fields = {
        "scope": config.scope,
        "connection_id": config.connection_id,
        "route_class": config.route_class,
        "provider_id": config.provider_id,
        "secret_backend": "windows-credential-manager",
        "credential_target": expected_target,
        "endpoint_sha256": access.endpoint_sha256,
        "credential_fingerprint_sha256": access.credential_fingerprint_sha256,
        "descriptor_schema": access.descriptor_schema,
        "credential_version_sha256": access.credential_version_sha256,
        "enabled": True,
    }
    if any(item.get(key) != value for key, value in expected_fields.items()):
        raise RouteMismatchError("provider connection identity mismatch")
    display_name = item.get("display_name")
    connection_sha = item.get("connection_sha256")
    if not isinstance(display_name, str) or not display_name:
        raise RouteMismatchError("provider connection identity is incomplete")
    metadata = {**expected_fields, "display_name": display_name}
    expected_connection_sha = stable_sha256(metadata)
    if connection_sha != expected_connection_sha:
        raise RouteMismatchError("provider connection registry SHA mismatch")
    return {
        "connection_id": config.connection_id,
        "connection_sha256": expected_connection_sha,
        "endpoint_sha256": access.endpoint_sha256,
        "credential_fingerprint_sha256": access.credential_fingerprint_sha256,
        "descriptor_schema": access.descriptor_schema,
        "credential_version_sha256": access.credential_version_sha256,
        "secret_present": access.secret_present,
    }


class OpenAICompatibleRunner:
    """Execute one OpenAI-compatible chat with bounded read-only MCP tool rounds."""

    def __init__(
        self,
        config: RunnerConfig,
        *,
        http_transport: HTTPTransport | None = None,
        mcp_transport: MCPTransport | None = None,
        cancellation_token: CancellationToken | None = None,
        runtime_admitted: bool = False,
    ) -> None:
        self.config = config
        self._allowed_tools = _validated_config(config)
        transport_tools = tuple(sorted(set(self._allowed_tools) | {REGISTRY_TOOL}))
        self._http = http_transport or StdlibHTTPTransport()
        self._cancellation_token = cancellation_token
        self._internal_cancellation = threading.Event()
        self._runtime_admitted = bool(runtime_admitted)
        self._provider_calls: list[dict[str, Any]] = []
        self._tool_input_schemas: dict[str, dict[str, Any]] = {}
        self._mcp = (
            mcp_transport
            if mcp_transport is not None
            else StdioMCPTransport(
                _mcp_command(config, transport_tools),
                cwd=Path(sys.executable).resolve().parent,
            )
        )

    def __repr__(self) -> str:
        return f"OpenAICompatibleRunner(config={self.config!r})"

    def _check_cancelled(self) -> None:
        if self._internal_cancellation.is_set() or (
            self._cancellation_token is not None
            and self._cancellation_token.is_set()
        ):
            raise RunCancelledError("provider run was cancelled")

    def cancel(self) -> None:
        self._internal_cancellation.set()

    def _retry_wait(self, attempt: int) -> None:
        delay = float(self.config.retry_backoff_seconds) * (2 ** max(0, attempt - 1))
        if delay <= 0:
            self._check_cancelled()
            return
        deadline = time.monotonic() + delay
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._internal_cancellation.wait(min(0.05, remaining))

    def _mcp_request(
        self,
        method: str,
        params: Mapping[str, Any],
        trace: list[dict[str, Any]],
    ) -> Mapping[str, Any]:
        params_object = dict(params)
        entry: dict[str, Any] = {
            "method": method,
            "params_sha256": stable_sha256(params_object),
        }
        if method == "tools/call":
            name = params_object.get("name")
            arguments = params_object.get("arguments")
            if isinstance(name, str):
                entry["tool_name"] = name
            if isinstance(arguments, dict):
                entry["arguments_sha256"] = stable_sha256(arguments)
        try:
            result = self._mcp.request(
                method,
                params_object,
                timeout=float(self.config.timeout_seconds),
            )
        except RunnerError:
            raise
        except Exception:
            raise MCPTransportError(f"local MCP method failed: {method}") from None
        if not isinstance(result, Mapping):
            raise MCPTransportError("local MCP transport returned a non-object result")
        entry["result_sha256"] = stable_sha256(result)
        trace.append(entry)
        return result

    def _mcp_notify(
        self,
        method: str,
        params: Mapping[str, Any],
        trace: list[dict[str, Any]],
    ) -> None:
        params_object = dict(params)
        try:
            self._mcp.notify(method, params_object)
        except RunnerError:
            raise
        except Exception:
            raise MCPTransportError(f"local MCP notification failed: {method}") from None
        trace.append(
            {"method": method, "params_sha256": stable_sha256(params_object)}
        )

    def _handshake(
        self, trace: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
        initialized = self._mcp_request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": __version__},
            },
            trace,
        )
        server_info = initialized.get("serverInfo")
        if not isinstance(server_info, dict) or server_info.get("name") != "peerbridge-mcp":
            raise MCPTransportError("unexpected MCP server identity")
        self._mcp_notify("notifications/initialized", {}, trace)
        listed = self._mcp_request("tools/list", {}, trace)
        tools = listed.get("tools")
        if not isinstance(tools, list):
            raise MCPTransportError("MCP tool list is malformed")
        by_name: dict[str, dict[str, Any]] = {}
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise MCPTransportError("MCP tool list contains a malformed tool")
            name = tool["name"]
            if name in by_name:
                raise MCPTransportError("MCP tool list contains duplicate names")
            by_name[name] = tool
        expected = set(self._allowed_tools) | {REGISTRY_TOOL}
        if not expected.issubset(by_name):
            raise MCPTransportError("MCP server did not expose the requested allowlist")
        selected_tools = [by_name[name] for name in self._allowed_tools]
        self._tool_input_schemas = {
            tool["name"]: dict(tool["inputSchema"])
            for tool in selected_tools
            if isinstance(tool.get("inputSchema"), dict)
        }
        registry = self._mcp_request(
            "tools/call",
            {"name": REGISTRY_TOOL, "arguments": {"enabled_only": False}},
            trace,
        )
        return selected_tools, registry

    def _provider_json(
        self,
        method: str,
        url: str,
        *,
        api_key: str | None,
        body: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"peerbridge-mcp/{__version__}",
        }
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        encoded: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            encoded = _json_dumps(body).encode("utf-8")
            headers["Idempotency-Key"] = uuid.uuid4().hex
        operation = (
            "models"
            if url.rstrip("/").endswith("/models")
            else "chat.completions"
        )
        response: HTTPResponse | None = None
        for attempt in range(1, self.config.max_http_attempts + 1):
            self._check_cancelled()
            try:
                response = self._http.request(
                    method,
                    url,
                    headers=headers,
                    body=encoded,
                    timeout=float(self.config.timeout_seconds),
                )
                if not isinstance(response, HTTPResponse):
                    raise ProviderHTTPError(
                        "provider HTTP transport returned an invalid response"
                    )
                if not 200 <= response.status < 300:
                    raise ProviderHTTPError(
                        f"provider returned HTTP status {response.status}",
                        status_code=response.status,
                        retryable=response.status in {408, 429, 500, 502, 503, 504},
                    )
                break
            except RunCancelledError:
                raise
            except ProviderHTTPError as exc:
                if not exc.retryable or attempt >= self.config.max_http_attempts:
                    raise
                self._retry_wait(attempt)
            except Exception:
                raise ProviderHTTPError("provider HTTP request failed") from None
        if response is None:
            raise ProviderHTTPError("provider HTTP request failed")
        self._check_cancelled()
        if len(response.body) > _MAX_HTTP_BODY_BYTES:
            raise ProviderHTTPError("provider response exceeded the size limit")
        try:
            payload = _parse_json_object(response.body, "provider response")
        except RunnerError:
            raise ProviderHTTPError("provider returned malformed JSON") from None
        self._provider_calls.append(
            {
                "operation": operation,
                "method": method,
                "attempts": attempt,
                "request_body_sha256": stable_sha256(body) if body is not None else None,
                "idempotency_key_sha256": (
                    stable_sha256(headers["Idempotency-Key"])
                    if "Idempotency-Key" in headers
                    else None
                ),
                "response_sha256": stable_sha256(payload),
                "status": response.status,
            }
        )
        return payload

    def _verify_model(self, api_base: str, api_key: str | None) -> str:
        payload = self._provider_json(
            "GET", f"{api_base}/models", api_key=api_key, body=None
        )
        model_ids, registry_sha256 = _canonical_model_registry(payload)
        if self.config.model not in model_ids:
            raise RouteMismatchError("requested model is not advertised by the provider")
        return registry_sha256

    def _openai_tools(self, tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            name = tool.get("name")
            description = tool.get("description")
            input_schema = tool.get("inputSchema")
            if (
                not isinstance(name, str)
                or not isinstance(description, str)
                or not isinstance(input_schema, dict)
            ):
                raise MCPTransportError("MCP tool schema is malformed")
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": input_schema,
                    },
                }
            )
        return converted

    def _chat(
        self,
        api_base: str,
        api_key: str | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str, str | None, str, dict[str, Any]]:
        body: dict[str, Any] = {"model": self.config.model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        payload = self._provider_json(
            "POST", f"{api_base}/chat/completions", api_key=api_key, body=body
        )
        observed_model = payload.get("model")
        accepted_response_models = {self.config.model}
        if self.config.response_model is not None:
            accepted_response_models.add(self.config.response_model)
        if observed_model not in accepted_response_models:
            raise RouteMismatchError("chat completion model identity mismatch")
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProviderHTTPError("provider returned an invalid choices array")
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ProviderHTTPError("provider returned an invalid assistant message")
        message = choice["message"]
        if message.get("role") != "assistant":
            raise ProviderHTTPError("provider returned a non-assistant message")
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ProviderHTTPError("provider returned an invalid finish reason")
        normalized: dict[str, Any] = {
            key: message[key]
            for key in ("role", "content", "tool_calls")
            if key in message
        }
        _json_dumps(normalized)
        raw_usage = payload.get("usage")
        usage = normalize_provider_usage(
            raw_usage if isinstance(raw_usage, Mapping) else None,
            source="openai-compatible/chat.completions",
        )
        return normalized, stable_sha256(payload), finish_reason, observed_model, usage

    def _tool_calls(self, message: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            return []
        if not isinstance(raw_calls, list) or not raw_calls:
            raise ToolCallError("assistant tool_calls must be a non-empty array")
        calls: list[dict[str, Any]] = []
        call_ids: set[str] = set()
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise ToolCallError("assistant returned a malformed tool call")
            call_id = raw_call.get("id")
            function = raw_call.get("function")
            if (
                not isinstance(call_id, str)
                or not call_id
                or len(call_id) > 500
                or call_id in call_ids
                or raw_call.get("type") != "function"
                or not isinstance(function, dict)
            ):
                raise ToolCallError("assistant returned a malformed tool call")
            name = function.get("name")
            raw_arguments = function.get("arguments")
            if name not in self._allowed_tools:
                raise ToolCallError("assistant requested an unknown or disallowed tool")
            if isinstance(raw_arguments, str):
                if not raw_arguments.strip():
                    raise ToolCallError("assistant returned empty tool arguments")
                argument_chars = len(raw_arguments)
                schema = self._tool_input_schemas.get(name)
                zero_arg_gateway_formats = {
                    "{{}}": "gateway-double-brace-empty-object",
                    "{}{}": "gateway-concatenated-empty-objects",
                }
                gateway_format = zero_arg_gateway_formats.get(raw_arguments)
                if gateway_format is not None and self._accepts_empty_object_schema(schema):
                    # Some OpenAI-compatible gateways escape or duplicate an
                    # empty object. Accept only the two observed, content-free
                    # wire values and only for a schema proven by the live MCP
                    # handshake to take no arguments. Every other malformed
                    # dialect stays closed.
                    arguments = {}
                    arguments_format = gateway_format
                else:
                    arguments_text = raw_arguments
                    arguments_format = "json-string"
                    if len(arguments_text) > _MAX_TOOL_ARGUMENT_CHARS:
                        raise ToolCallError("assistant returned malformed tool arguments")
                    try:
                        arguments = _parse_json_object(
                            arguments_text.encode("utf-8"), "tool arguments"
                        )
                    except RunnerError:
                        raise ToolCallError(
                            "assistant returned non-object JSON tool arguments"
                        ) from None
            elif isinstance(raw_arguments, dict):
                try:
                    arguments_text = _json_dumps(raw_arguments)
                except RunnerError:
                    raise ToolCallError(
                        "assistant returned malformed tool arguments"
                    ) from None
                arguments_format = "json-object"
                argument_chars = len(arguments_text)
                arguments = dict(raw_arguments)
            else:
                raise ToolCallError(
                    "assistant returned unsupported tool argument type: "
                    + type(raw_arguments).__name__
                )
            if argument_chars > _MAX_TOOL_ARGUMENT_CHARS:
                raise ToolCallError("assistant returned malformed tool arguments")
            call_ids.add(call_id)
            calls.append(
                {
                    "id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "argument_chars": argument_chars,
                    "arguments_format": arguments_format,
                }
            )
        return calls

    @staticmethod
    def _accepts_empty_object_schema(schema: Mapping[str, Any] | None) -> bool:
        """Return true only when an empty object satisfies the live MCP schema."""
        if not isinstance(schema, Mapping):
            return False
        required = schema.get("required", [])
        return (
            schema.get("type") == "object"
            and isinstance(schema.get("properties"), Mapping)
            and required == []
            and schema.get("additionalProperties") is False
        )

    def _tool_result_text(self, result: Mapping[str, Any]) -> str:
        if result.get("isError") is True:
            raise MCPTransportError("MCP tool returned an error")
        structured = result.get("structuredContent")
        if structured is not None:
            text = _json_dumps(structured)
        else:
            content = result.get("content")
            if not isinstance(content, list) or not content:
                raise MCPTransportError("MCP tool result is malformed")
            texts: list[str] = []
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(
                    item.get("text"), str
                ):
                    raise MCPTransportError("MCP tool result contains unsupported content")
                texts.append(item["text"])
            text = "\n".join(texts)
        if len(text) > _MAX_TOOL_RESULT_CHARS:
            raise MCPTransportError("MCP tool result exceeded the size limit")
        return text

    @staticmethod
    def _response_only_fallback_reason(exc: Exception) -> str:
        if isinstance(exc, MaxToolRoundsError):
            return "max_tool_rounds_exceeded"
        if isinstance(exc, ToolCallError):
            return "tool_call_validation_failed"
        if isinstance(exc, MCPTransportError):
            return "mcp_tool_execution_failed"
        raise AssertionError("unsupported response-only fallback exception")

    def run(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        message_id: str | None = None,
    ) -> InferenceResult:
        """Run one inference while holding a bounded cross-process runtime slot."""
        with provider_runtime_admission(already_admitted=self._runtime_admitted):
            return self._run_unchecked(messages, message_id=message_id)

    def _run_unchecked(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        message_id: str | None = None,
    ) -> InferenceResult:
        """Run real inference and return output separately from its sanitized receipt."""
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise ConfigurationError("messages must be a sequence of JSON objects")
        conversation: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise ConfigurationError("messages must contain only JSON objects")
            conversation.append(dict(message))
        if not conversation:
            raise ConfigurationError("at least one input message is required")
        _json_dumps(conversation)
        run_message_id = _safe_id(message_id or uuid.uuid4().hex, "message_id")
        self._provider_calls = []
        input_messages_sha = stable_sha256(conversation)
        trace: list[dict[str, Any]] = []
        tool_receipts: list[dict[str, Any]] = []
        completion_hashes: list[str] = []
        completion_usages: list[dict[str, Any]] = []
        observed_response_models: list[str] = []
        response_only_fallback: dict[str, Any] = {
            "used": False,
            "reason_code": None,
            "model_visible_tools": True,
            "instruction_sha256": None,
        }
        finish_reason: str | None = None
        self._mcp.start()
        try:
            self._check_cancelled()
            mcp_tools, registry_result = self._handshake(trace)
            try:
                access = credentials.load_provider_access(
                    scope=self.config.scope,
                    connection_id=self.config.connection_id,
                    route_class=self.config.route_class,
                    provider_id=self.config.provider_id,
                )
            except Exception:
                raise CredentialUnavailableError(
                    "required Windows Credential Manager credential is unavailable"
                ) from None
            try:
                api_base = _api_base_url(access.endpoint)
                connection = _connection_identity(
                    registry_result,
                    config=self.config,
                    access=access,
                )
            except credentials.CredentialStoreError:
                raise CredentialUnavailableError(
                    "provider credential payload is invalid"
                ) from None
            models_sha = self._verify_model(api_base, access.api_key)
            openai_tools = self._openai_tools(mcp_tools)
            tool_rounds = 0
            tool_call_count = 1  # provider registry is a real MCP tools/call.
            tool_argument_chars = len(_json_dumps({"enabled_only": False}))
            tool_result_chars = len(_json_dumps(registry_result))
            if tool_argument_chars > self.config.max_cumulative_tool_argument_chars:
                raise ToolCallError("cumulative MCP tool argument budget exceeded")
            if tool_result_chars > self.config.max_cumulative_tool_result_chars:
                raise ToolCallError("cumulative MCP tool result budget exceeded")
            initial_conversation = [dict(item) for item in conversation]
            try:
                while True:
                    tools_for_round = (
                        openai_tools
                        if tool_rounds < self.config.max_tool_rounds
                        else []
                    )
                    (
                        assistant,
                        completion_sha,
                        finish_reason,
                        observed_response_model,
                        completion_usage,
                    ) = self._chat(
                        api_base,
                        access.api_key,
                        conversation,
                        tools_for_round,
                    )
                    completion_hashes.append(completion_sha)
                    completion_usages.append(completion_usage)
                    observed_response_models.append(observed_response_model)
                    calls = self._tool_calls(assistant)
                    if not calls:
                        final_assistant = assistant
                        break
                    if tool_rounds >= self.config.max_tool_rounds:
                        raise MaxToolRoundsError("maximum MCP tool rounds exceeded")
                    proposed_calls = tool_call_count + len(calls)
                    proposed_argument_chars = tool_argument_chars + sum(
                        int(call["argument_chars"]) for call in calls
                    )
                    if proposed_calls > self.config.max_tool_calls:
                        raise ToolCallError("maximum total MCP tool calls exceeded")
                    if (
                        proposed_argument_chars
                        > self.config.max_cumulative_tool_argument_chars
                    ):
                        raise ToolCallError("cumulative MCP tool argument budget exceeded")
                    if (
                        calls
                        and tool_result_chars
                        >= self.config.max_cumulative_tool_result_chars
                    ):
                        raise ToolCallError("cumulative MCP tool result budget exhausted")
                    tool_rounds += 1
                    conversation.append(assistant)
                    for call in calls:
                        self._check_cancelled()
                        params = {"name": call["name"], "arguments": call["arguments"]}
                        result = self._mcp_request("tools/call", params, trace)
                        result_text = self._tool_result_text(result)
                        next_result_chars = tool_result_chars + len(result_text)
                        if (
                            next_result_chars
                            > self.config.max_cumulative_tool_result_chars
                        ):
                            raise ToolCallError(
                                "cumulative MCP tool result budget exceeded"
                            )
                        tool_call_count += 1
                        tool_argument_chars += int(call["argument_chars"])
                        tool_result_chars = next_result_chars
                        arguments_sha = stable_sha256(call["arguments"])
                        result_sha = stable_sha256(result)
                        tool_receipts.append(
                            {
                                "round": tool_rounds,
                                "name": call["name"],
                                "arguments_format": call["arguments_format"],
                                "call_id_sha256": stable_sha256(call["id"]),
                                "arguments_sha256": arguments_sha,
                                "result_sha256": result_sha,
                            }
                        )
                        conversation.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "name": call["name"],
                                "content": result_text,
                            }
                        )
            except (ToolCallError, MaxToolRoundsError, MCPTransportError) as exc:
                if not self.config.response_only_fallback_on_tool_error:
                    raise
                self._check_cancelled()
                fallback_conversation = [
                    {
                        "role": "system",
                        "content": _RESPONSE_ONLY_FALLBACK_INSTRUCTION,
                    },
                    *initial_conversation,
                ]
                (
                    assistant,
                    completion_sha,
                    finish_reason,
                    observed_response_model,
                    completion_usage,
                ) = self._chat(
                    api_base,
                    access.api_key,
                    fallback_conversation,
                    [],
                )
                completion_hashes.append(completion_sha)
                completion_usages.append(completion_usage)
                observed_response_models.append(observed_response_model)
                if self._tool_calls(assistant):
                    raise ToolCallError(
                        "assistant requested tools during response-only fallback"
                    )
                final_assistant = assistant
                response_only_fallback = {
                    "used": True,
                    "reason_code": self._response_only_fallback_reason(exc),
                    "model_visible_tools": False,
                    "instruction_sha256": stable_sha256(
                        _RESPONSE_ONLY_FALLBACK_INSTRUCTION
                    ),
                }
        finally:
            self._mcp.close()

        receipt: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "route": {
                "route_profile_id": self.config.route_profile_id,
                "route_profile_sha256": self.config.route_profile_sha256,
                "route_class": self.config.route_class,
                "provider_id": self.config.provider_id,
                "provider_id_sha256": stable_sha256(self.config.provider_id),
                "model_id": self.config.model,
                "model_id_sha256": stable_sha256(self.config.model),
                "response_model_id": self.config.response_model or self.config.model,
                "response_model_id_sha256": stable_sha256(
                    self.config.response_model or self.config.model
                ),
                "accepted_response_model_ids": sorted(
                    {
                        self.config.model,
                        self.config.response_model or self.config.model,
                    }
                ),
                "accepted_response_model_ids_sha256": stable_sha256(
                    sorted(
                        {
                            self.config.model,
                            self.config.response_model or self.config.model,
                        }
                    )
                ),
                "reasoning_mode": self.config.reasoning_mode,
                **connection,
            },
            "room_id": self.config.room_id,
            "room_id_sha256": stable_sha256(self.config.room_id),
            "session_id": self.config.session_id,
            "session_id_sha256": stable_sha256(self.config.session_id),
            "message_id_sha256": stable_sha256(run_message_id),
            "input_messages_sha256": input_messages_sha,
            "output_message_sha256": stable_sha256(final_assistant),
            "models_response_sha256": models_sha,
            "completion_response_sha256": completion_hashes,
            "observed_response_model_ids": observed_response_models,
            "finish_reason": finish_reason,
            "tool_rounds": tool_rounds,
            "tool_budget": {
                "max_tool_calls": self.config.max_tool_calls,
                "tool_calls_used": tool_call_count,
                "max_cumulative_argument_chars": self.config.max_cumulative_tool_argument_chars,
                "cumulative_argument_chars_used": tool_argument_chars,
                "max_cumulative_result_chars": self.config.max_cumulative_tool_result_chars,
                "cumulative_result_chars_used": tool_result_chars,
            },
            "tool_calls": tool_receipts,
            "response_only_fallback": response_only_fallback,
            "mcp_calls": trace,
            "provider_http_calls": self._provider_calls,
            "usage": aggregate_usage(
                completion_usages,
                source="openai-compatible/chat.completions",
            ),
            "raw_content_recorded": False,
            "credential_contents_recorded": False,
        }
        receipt["receipt_sha256"] = stable_sha256(receipt)
        return InferenceResult(assistant_message=final_assistant, receipt=receipt)


def run_chat_completion(
    config: RunnerConfig,
    messages: Sequence[Mapping[str, Any]],
    *,
    message_id: str | None = None,
    http_transport: HTTPTransport | None = None,
    mcp_transport: MCPTransport | None = None,
    cancellation_token: CancellationToken | None = None,
) -> InferenceResult:
    """Convenience entry point for one bounded OpenAI-compatible run."""
    return OpenAICompatibleRunner(
        config,
        http_transport=http_transport,
        mcp_transport=mcp_transport,
        cancellation_token=cancellation_token,
    ).run(messages, message_id=message_id)


__all__ = [
    "ConfigurationError",
    "CancellationToken",
    "CredentialUnavailableError",
    "DEFAULT_ALLOWED_TOOLS",
    "HTTPResponse",
    "HTTPTransport",
    "InferenceResult",
    "MCPTransport",
    "MCPTransportError",
    "MaxToolRoundsError",
    "OpenAICompatibleRunner",
    "ProviderHTTPError",
    "provider_runtime_admission",
    "ResourceUnavailableError",
    "RouteMismatchError",
    "RunCancelledError",
    "RunnerConfig",
    "RunnerError",
    "StdioMCPTransport",
    "StdlibHTTPTransport",
    "ToolCallError",
    "run_chat_completion",
]
