"""Local provider credential storage that never routes secrets through MCP."""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit


_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_MAX_CREDENTIAL_BLOB_BYTES = 2560
_DESCRIPTOR_SCHEMA = "peerbridge.provider-credential.v2"
_LOCAL_DESCRIPTOR_KIND = "local-openai-compatible"
_OFFICIAL_API_HOSTS = frozenset(
    {
        "api.anthropic.com",
        "api.deepseek.com",
        "api.moonshot.cn",
        "api.openai.com",
        "api.x.ai",
        "aiplatform.googleapis.com",
        "generativelanguage.googleapis.com",
    }
)


class CredentialStoreError(RuntimeError):
    """A redacted credential-store failure safe to show in the local UI."""


class CredentialStore(Protocol):
    def write(self, target: str, secret: str) -> None: ...

    def read(self, target: str) -> str: ...

    def exists(self, target: str) -> bool: ...

    def delete(self, target: str) -> bool: ...


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Store opaque UTF-8 blobs in the current user's Windows vault."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise CredentialStoreError("Windows Credential Manager is unavailable")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi32.CredWriteW.argtypes = (
            ctypes.POINTER(_CredentialW),
            wintypes.DWORD,
        )
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredReadW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        )
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = (wintypes.LPVOID,)
        self._advapi32.CredFree.restype = None

    @staticmethod
    def _target(target: str) -> str:
        value = str(target or "").strip()
        if not value or len(value) > 256:
            raise CredentialStoreError("invalid local credential target")
        return value

    def write(self, target: str, secret: str) -> None:
        target = self._target(target)
        encoded = secret.encode("utf-8")
        if not encoded or len(encoded) > _MAX_CREDENTIAL_BLOB_BYTES:
            raise CredentialStoreError("credential payload is empty or too large")
        blob = (wintypes.BYTE * len(encoded)).from_buffer_copy(encoded)
        credential = _CredentialW()
        credential.Flags = 0
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.Comment = "PeerBridge MCP local provider credential"
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(wintypes.BYTE))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = None
        credential.TargetAlias = None
        credential.UserName = "PeerBridge MCP"
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialStoreError(
                f"Windows Credential Manager write failed ({ctypes.get_last_error()})"
            )

    def read(self, target: str) -> str:
        target = self._target(target)
        pointer = ctypes.POINTER(_CredentialW)()
        if not self._advapi32.CredReadW(
            target,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            code = ctypes.get_last_error()
            if code == _ERROR_NOT_FOUND:
                raise CredentialStoreError("local credential is not configured")
            raise CredentialStoreError(f"Windows Credential Manager read failed ({code})")
        try:
            size = int(pointer.contents.CredentialBlobSize)
            raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
            return raw.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise CredentialStoreError("local credential payload is invalid") from exc
        finally:
            self._advapi32.CredFree(pointer)

    def exists(self, target: str) -> bool:
        try:
            self.read(target)
        except CredentialStoreError as exc:
            if str(exc) == "local credential is not configured":
                return False
            raise
        return True

    def delete(self, target: str) -> bool:
        target = self._target(target)
        if self._advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
            return True
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            return False
        raise CredentialStoreError(f"Windows Credential Manager delete failed ({code})")


@dataclass(frozen=True)
class ProviderCredentialRef:
    credential_target: str
    endpoint_sha256: str
    credential_fingerprint_sha256: str
    descriptor_schema: str
    route_class: str
    provider_id: str
    credential_version_sha256: str


@dataclass(frozen=True, repr=False)
class ProviderAccess:
    """Ephemeral provider access resolved inside one local adapter process."""

    endpoint: str
    api_key: str | None
    credential_target: str
    endpoint_sha256: str
    credential_fingerprint_sha256: str
    secret_present: bool
    descriptor_schema: str = "legacy-v1"
    route_class: str | None = None
    provider_id: str | None = None
    credential_version_sha256: str = ""
    descriptor_bound: bool = False

    def __repr__(self) -> str:
        return (
            "ProviderAccess("
            f"credential_target={self.credential_target!r}, "
            f"endpoint_sha256={self.endpoint_sha256!r}, "
            f"credential_fingerprint_sha256={self.credential_fingerprint_sha256!r}, "
            f"descriptor_schema={self.descriptor_schema!r}, "
            f"route_class={self.route_class!r}, provider_id={self.provider_id!r}, "
            f"credential_version_sha256={self.credential_version_sha256!r}, "
            f"descriptor_bound={self.descriptor_bound!r}, "
            f"secret_present={self.secret_present!r})"
        )


def normalize_endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    if len(endpoint) > 2048:
        raise CredentialStoreError("provider endpoint is too long")
    parsed = urlsplit(endpoint)
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise CredentialStoreError("provider endpoint must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and parsed.hostname not in local_hosts:
        raise CredentialStoreError("remote provider endpoints must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CredentialStoreError(
            "provider endpoint must not contain credentials, query parameters or fragments"
        )
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def is_loopback_endpoint(value: str) -> bool:
    """Return whether a normalized provider endpoint resolves by loopback syntax."""
    parsed = urlsplit(normalize_endpoint(value))
    host = (parsed.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def credential_target(scope: str, connection_id: str) -> str:
    if not _SAFE_ID.fullmatch(scope) or not _SAFE_ID.fullmatch(connection_id):
        raise CredentialStoreError("scope and connection ID must be safe identifiers")
    identity = json.dumps(
        {"connection_id": connection_id, "scope": scope},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"PeerBridgeMCP:v2:{hashlib.sha256(identity).hexdigest()}"


def legacy_credential_target(scope: str, connection_id: str) -> str:
    """Return the ambiguous v1 target only for explicit read compatibility."""
    if not _SAFE_ID.fullmatch(scope) or not _SAFE_ID.fullmatch(connection_id):
        raise CredentialStoreError("scope and connection ID must be safe identifiers")
    return f"PeerBridgeMCP:{scope}:{connection_id}"


def _bound_identifier(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise CredentialStoreError(f"{label} must be a safe identifier")
    return text


def _bound_route(route_class: str) -> str:
    route = str(route_class or "").strip().lower()
    if route not in {"official", "relay", "local"}:
        raise CredentialStoreError("provider route class is invalid")
    return route


def _require_official_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in _OFFICIAL_API_HOSTS
        or parsed.port not in {None, 443}
    ):
        raise CredentialStoreError(
            "official routes must use a built-in official provider endpoint; "
            "use relay for custom HTTPS endpoints"
        )


def _version_fingerprint(version_id: str) -> str:
    return hashlib.sha256(
        f"PeerBridgeMCP:opaque-credential-version:{version_id}".encode("ascii")
    ).hexdigest()


def _read_with_legacy_fallback(
    store: CredentialStore,
    *,
    scope: str,
    connection_id: str,
    allow_legacy: bool = False,
) -> tuple[str, str, bool]:
    target = credential_target(scope, connection_id)
    try:
        return target, store.read(target), False
    except CredentialStoreError as exc:
        if str(exc) != "local credential is not configured":
            raise
        if not allow_legacy:
            raise
    if ":" in scope or ":" in connection_id:
        raise CredentialStoreError(
            "legacy credential target is ambiguous; migrate it to a v2 descriptor"
        )
    legacy_target = legacy_credential_target(scope, connection_id)
    return legacy_target, store.read(legacy_target), True


def store_provider_credentials(
    *,
    scope: str,
    connection_id: str,
    endpoint: str,
    api_key: str,
    route_class: str = "relay",
    provider_id: str | None = None,
    store: CredentialStore | None = None,
) -> ProviderCredentialRef:
    normalized_endpoint = normalize_endpoint(endpoint)
    route = _bound_route(route_class)
    if route == "local":
        raise CredentialStoreError("remote provider credentials cannot use local route class")
    if route == "official":
        _require_official_endpoint(normalized_endpoint)
    bound_provider = _bound_identifier(provider_id or connection_id, "provider ID")
    secret = str(api_key or "").strip()
    if not secret or len(secret) > 2048:
        raise CredentialStoreError("API key is empty or too large")
    version_id = uuid.uuid4().hex
    payload = json.dumps(
        {
            "api_key": secret,
            "credential_version_id": version_id,
            "endpoint": normalized_endpoint,
            "provider_id": bound_provider,
            "route_class": route,
            "schema": _DESCRIPTOR_SCHEMA,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    target = credential_target(scope, connection_id)
    (store or WindowsCredentialStore()).write(target, payload)
    version_sha = _version_fingerprint(version_id)
    return ProviderCredentialRef(
        credential_target=target,
        endpoint_sha256=hashlib.sha256(normalized_endpoint.encode("utf-8")).hexdigest(),
        credential_fingerprint_sha256=version_sha,
        descriptor_schema=_DESCRIPTOR_SCHEMA,
        route_class=route,
        provider_id=bound_provider,
        credential_version_sha256=version_sha,
    )


def store_local_provider_endpoint(
    *,
    scope: str,
    connection_id: str,
    endpoint: str,
    provider_id: str | None = None,
    store: CredentialStore | None = None,
) -> ProviderCredentialRef:
    """Store a loopback endpoint descriptor without creating or persisting a secret."""
    normalized_endpoint = normalize_endpoint(endpoint)
    if not is_loopback_endpoint(normalized_endpoint):
        raise CredentialStoreError("local provider endpoint must use a loopback host")
    bound_provider = _bound_identifier(provider_id or connection_id, "provider ID")
    version_id = uuid.uuid4().hex
    payload = json.dumps(
        {
            "credential_version_id": version_id,
            "endpoint": normalized_endpoint,
            "kind": _LOCAL_DESCRIPTOR_KIND,
            "provider_id": bound_provider,
            "route_class": "local",
            "schema": _DESCRIPTOR_SCHEMA,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    target = credential_target(scope, connection_id)
    (store or WindowsCredentialStore()).write(target, payload)
    version_sha = _version_fingerprint(version_id)
    return ProviderCredentialRef(
        credential_target=target,
        endpoint_sha256=hashlib.sha256(normalized_endpoint.encode("utf-8")).hexdigest(),
        credential_fingerprint_sha256=version_sha,
        descriptor_schema=_DESCRIPTOR_SCHEMA,
        route_class="local",
        provider_id=bound_provider,
        credential_version_sha256=version_sha,
    )


def load_provider_credentials(
    *,
    scope: str,
    connection_id: str,
    store: CredentialStore | None = None,
    allow_legacy: bool = False,
) -> dict[str, str]:
    """Return secrets to a local adapter; legacy reads are migration-only and opt-in."""
    _target, raw, _legacy = _read_with_legacy_fallback(
        store or WindowsCredentialStore(),
        scope=scope,
        connection_id=connection_id,
        allow_legacy=allow_legacy,
    )
    try:
        payload = json.loads(raw)
        endpoint = normalize_endpoint(payload["endpoint"])
        api_key = str(payload["api_key"] or "").strip()
    except (KeyError, TypeError, json.JSONDecodeError, CredentialStoreError) as exc:
        raise CredentialStoreError("local provider credential payload is invalid") from exc
    if not api_key:
        raise CredentialStoreError("local provider credential payload is invalid")
    return {"endpoint": endpoint, "api_key": api_key}


def load_provider_access(
    *,
    scope: str,
    connection_id: str,
    route_class: str,
    provider_id: str | None = None,
    store: CredentialStore | None = None,
    allow_legacy: bool = False,
) -> ProviderAccess:
    """Resolve one route without exposing its endpoint or secret through MCP."""
    route = _bound_route(route_class)
    bound_provider = _bound_identifier(provider_id or connection_id, "provider ID")
    target, raw, legacy = _read_with_legacy_fallback(
        store or WindowsCredentialStore(),
        scope=scope,
        connection_id=connection_id,
        allow_legacy=allow_legacy,
    )
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError
        endpoint = normalize_endpoint(payload["endpoint"])
    except (KeyError, TypeError, json.JSONDecodeError, CredentialStoreError) as exc:
        raise CredentialStoreError("local provider credential payload is invalid") from exc

    schema = payload.get("schema")
    descriptor_bound = not legacy and schema == _DESCRIPTOR_SCHEMA
    stored_route = payload.get("route_class") if descriptor_bound else None
    stored_provider = payload.get("provider_id") if descriptor_bound else None
    version_id = payload.get("credential_version_id") if descriptor_bound else None
    if descriptor_bound:
        if (
            stored_route != route
            or stored_provider != bound_provider
            or not isinstance(version_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", version_id)
        ):
            raise CredentialStoreError("provider credential binding mismatch")
    if route == "official":
        _require_official_endpoint(endpoint)

    api_key: str | None
    if route == "local":
        if payload.get("kind") != _LOCAL_DESCRIPTOR_KIND or not is_loopback_endpoint(
            endpoint
        ) or payload.get("api_key") not in {None, ""}:
            raise CredentialStoreError("local provider descriptor is invalid")
        api_key = None
    else:
        if payload.get("kind") is not None:
            raise CredentialStoreError("local provider credential payload is invalid")
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key:
            raise CredentialStoreError("local provider credential payload is invalid")

    version_sha = (
        _version_fingerprint(version_id)
        if descriptor_bound and isinstance(version_id, str)
        else hashlib.sha256(
            f"PeerBridgeMCP:legacy-migration-only:{target}".encode("utf-8")
        ).hexdigest()
    )
    return ProviderAccess(
        endpoint=endpoint,
        api_key=api_key,
        credential_target=target,
        endpoint_sha256=hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
        credential_fingerprint_sha256=version_sha,
        secret_present=api_key is not None,
        descriptor_schema=str(schema or "legacy-v1"),
        route_class=str(stored_route) if stored_route is not None else None,
        provider_id=str(stored_provider) if stored_provider is not None else None,
        credential_version_sha256=version_sha,
        descriptor_bound=descriptor_bound,
    )
