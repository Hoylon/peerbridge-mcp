"""Pre-issued local capabilities that bind an MCP process to one Agent identity."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LEGACY_CAPABILITY_SCHEMA = "peerbridge.agent-identity-capability.v1"
CAPABILITY_SCHEMA = "peerbridge.agent-identity-capability.v2"
CAPABILITY_DIRECTORY = "identity-capabilities"
MAX_CAPABILITY_FILE_BYTES = 16_384
REDACTED_CAPABILITY_ARGUMENT = "<peerbridge-agent-identity-capability>"
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_EXPECTED_V1_FILE_KEYS = frozenset(
    {
        "schema",
        "capability_id",
        "workspace_root_key",
        "scope",
        "agent_id",
        "secret_file_relpath",
        "issued_utc",
        "token_sha256",
        "capability_sha256",
        "secret_token",
    }
)
_EXPECTED_V2_REQUIRED_FILE_KEYS = _EXPECTED_V1_FILE_KEYS | {
    "allowed_tools",
    "issued_by",
}
_EXPECTED_V2_FILE_KEYS = _EXPECTED_V2_REQUIRED_FILE_KEYS | {
    "route_binding",
    "bound_room_id",
    "bound_room_session_id",
    "bound_route_profile_id",
    "bound_route_profile_sha256",
}
_ROUTE_BINDING_FIELDS = (
    "client_name",
    "provider_id",
    "model_id",
    "reasoning_mode",
    "route_class",
)
_ROUTE_CLASSES = frozenset({"official", "relay", "local"})
_ROUTE_LABEL = re.compile(r"[A-Za-z0-9_.:/-]{1,500}\Z")
MAX_CAPABILITY_TOOLS = 128


class AgentIdentityError(RuntimeError):
    """A redacted identity-capability error safe to show to a local operator."""


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back, then deterministically release the SQLite handle."""

    def __exit__(self, exc_type, exc_value, traceback):  # type: ignore[no-untyped-def]
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


@dataclass(frozen=True)
class AgentIdentityRouteBinding:
    client_name: str | None
    provider_id: str | None
    model_id: str | None
    reasoning_mode: str | None
    route_class: str | None


@dataclass(frozen=True)
class AgentIdentityCapability:
    capability_id: str
    agent_id: str
    scope: str
    path: Path
    capability_sha256: str
    schema: str = LEGACY_CAPABILITY_SCHEMA
    allowed_tools: tuple[str, ...] = ()
    issued_by: str = "legacy"
    route_binding: AgentIdentityRouteBinding | None = None
    bound_room_id: str | None = None
    bound_room_session_id: str | None = None
    bound_route_profile_id: str | None = None
    bound_route_profile_sha256: str | None = None

    @property
    def uses_legacy_tool_fallback(self) -> bool:
        return self.schema == LEGACY_CAPABILITY_SCHEMA


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _single_launch_option(args: list[str], option: str) -> tuple[int, str]:
    positions = [index for index, value in enumerate(args) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(args):
        raise AgentIdentityError(f"Agent identity launch option is invalid: {option}")
    value = args[positions[0] + 1]
    if not value or value.startswith("--"):
        raise AgentIdentityError(f"Agent identity launch option is invalid: {option}")
    return positions[0], value


def _optional_launch_option(args: list[str], option: str) -> str | None:
    positions = [index for index, value in enumerate(args) if value == option]
    if not positions:
        return None
    if len(positions) != 1 or positions[0] + 1 >= len(args):
        raise AgentIdentityError(f"Agent identity launch option is invalid: {option}")
    value = args[positions[0] + 1]
    if not value or value.startswith("--"):
        raise AgentIdentityError(f"Agent identity launch option is invalid: {option}")
    return value


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _identifier(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized) or normalized in {".", ".."}:
        raise AgentIdentityError(f"invalid {label}")
    return normalized


def workspace_root_key(project_root: Path) -> str:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise AgentIdentityError("project root does not exist")
    canonical = os.path.normcase(str(root))
    if os.name == "nt":
        canonical = canonical.casefold()
    return _sha256_bytes(canonical.encode("utf-8"))


def _state_root(db_path: Path) -> Path:
    return Path(db_path).resolve().parent


def _capability_root(db_path: Path) -> Path:
    return _state_root(db_path) / CAPABILITY_DIRECTORY


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _reject_capability_reparse_ancestry(db_path: Path, target: Path) -> None:
    root = _state_root(db_path)
    lexical = Path(target).absolute()
    try:
        relative = lexical.relative_to(root.absolute())
    except ValueError as exc:
        raise AgentIdentityError("identity capability path escaped local state") from exc
    current = root.absolute()
    if _is_link_or_reparse(current):
        raise AgentIdentityError("identity capability path crosses a filesystem link")
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise AgentIdentityError("identity capability path crosses a filesystem link")


def _read_only_connection(
    db_path: Path, *, immutable: bool = False
) -> sqlite3.Connection:
    database = Path(db_path).resolve()
    if not database.is_file():
        raise AgentIdentityError("PeerBridge database does not exist")
    wal_path = database.with_name(database.name + "-wal")
    if immutable and wal_path.is_file() and wal_path.stat().st_size:
        raise AgentIdentityError("identity capability registry has uncheckpointed WAL evidence")
    try:
        query = "mode=ro&immutable=1" if immutable else "mode=ro"
        connection = sqlite3.connect(
            f"{database.as_uri()}?{query}",
            uri=True,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as exc:
        raise AgentIdentityError("unable to read identity capability registry") from exc


def _capability_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": str(data["schema"]),
        "capability_id": str(data["capability_id"]),
        "workspace_root_key": str(data["workspace_root_key"]),
        "scope": str(data["scope"]),
        "agent_id": str(data["agent_id"]),
        "secret_file_relpath": str(data["secret_file_relpath"]),
        "issued_utc": str(data["issued_utc"]),
        "token_sha256": str(data["token_sha256"]),
    }
    if data.get("schema") == CAPABILITY_SCHEMA:
        payload["allowed_tools"] = list(data["allowed_tools"])
        payload["issued_by"] = str(data["issued_by"])
        # These fields were added without changing the v2 schema identifier.
        # Include only fields present in the source so early v2 files retain
        # their original descriptor digest.
        if "route_binding" in data:
            payload["route_binding"] = data["route_binding"]
        if "bound_room_id" in data:
            payload["bound_room_id"] = data["bound_room_id"]
        for key in (
            "bound_room_session_id",
            "bound_route_profile_id",
            "bound_route_profile_sha256",
        ):
            if key in data:
                payload[key] = data[key]
    return payload


def _allowed_tools(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_CAPABILITY_TOOLS:
        raise AgentIdentityError("identity capability tool allowlist is invalid")
    tools = tuple(sorted({_identifier(str(item), "tool name") for item in value}))
    if len(tools) != len(value):
        raise AgentIdentityError("identity capability tool allowlist contains duplicates")
    return tools


def _optional_route_label(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentIdentityError(f"identity capability {label} is invalid")
    normalized = value.strip()
    if not _ROUTE_LABEL.fullmatch(normalized):
        raise AgentIdentityError(f"identity capability {label} is invalid")
    return normalized


def _route_binding(
    value: Mapping[str, Any] | AgentIdentityRouteBinding | None,
) -> AgentIdentityRouteBinding | None:
    if value is None:
        return None
    if isinstance(value, AgentIdentityRouteBinding):
        source: Mapping[str, Any] = {
            field: getattr(value, field) for field in _ROUTE_BINDING_FIELDS
        }
    elif isinstance(value, Mapping):
        source = value
    else:
        raise AgentIdentityError("identity capability route binding is invalid")
    if frozenset(source) != frozenset(_ROUTE_BINDING_FIELDS):
        raise AgentIdentityError("identity capability route binding is invalid")
    route_class = _optional_route_label(source["route_class"], "route_class")
    if route_class is not None and route_class not in _ROUTE_CLASSES:
        raise AgentIdentityError("identity capability route_class is invalid")
    return AgentIdentityRouteBinding(
        client_name=_optional_route_label(source["client_name"], "client_name"),
        provider_id=_optional_route_label(source["provider_id"], "provider_id"),
        model_id=_optional_route_label(source["model_id"], "model_id"),
        reasoning_mode=_optional_route_label(
            source["reasoning_mode"], "reasoning_mode"
        ),
        route_class=route_class,
    )


def _route_binding_payload(
    value: AgentIdentityRouteBinding | None,
) -> dict[str, str | None] | None:
    if value is None:
        return None
    return {field: getattr(value, field) for field in _ROUTE_BINDING_FIELDS}


def _bound_room_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentIdentityError("identity capability bound_room_id is invalid")
    return _identifier(value, "bound_room_id")


def _optional_bound_identifier(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentIdentityError(f"identity capability {label} is invalid")
    return _identifier(value, label)


def _optional_bound_sha256(value: Any, label: str) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip().lower()
    if _SHA256.fullmatch(candidate) is None:
        raise AgentIdentityError(f"identity capability {label} is invalid")
    return candidate


def _resolved_registered_path(db_path: Path, relative_path: str) -> Path:
    lexical = _state_root(db_path) / relative_path
    _reject_capability_reparse_ancestry(db_path, lexical)
    candidate = lexical.resolve()
    root = _capability_root(db_path).resolve()
    if not candidate.is_relative_to(root):
        raise AgentIdentityError("identity capability path escaped local state")
    return candidate


def _read_capability_file(db_path: Path, capability_path: Path) -> dict[str, Any]:
    supplied = Path(capability_path)
    if not supplied.is_absolute():
        raise AgentIdentityError("identity capability path must be absolute")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise AgentIdentityError("identity capability file is unavailable") from exc
    root = _capability_root(db_path).resolve()
    if not resolved.is_relative_to(root):
        raise AgentIdentityError("identity capability file is outside local state")
    try:
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise AgentIdentityError("identity capability is not a regular file")
        if not 0 < metadata.st_size <= MAX_CAPABILITY_FILE_BYTES:
            raise AgentIdentityError("identity capability file size is invalid")
        if os.name != "nt" and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise AgentIdentityError("identity capability permissions are too broad")
        raw = resolved.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise AgentIdentityError("identity capability encoding is invalid")
        data = json.loads(raw.decode("utf-8"))
    except AgentIdentityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentIdentityError("identity capability file is invalid") from exc
    if not isinstance(data, dict):
        raise AgentIdentityError("identity capability fields are invalid")
    schema = data.get("schema")
    keys = frozenset(data)
    valid_fields = (
        _EXPECTED_V2_REQUIRED_FILE_KEYS <= keys <= _EXPECTED_V2_FILE_KEYS
        if schema == CAPABILITY_SCHEMA
        else keys == _EXPECTED_V1_FILE_KEYS
        if schema == LEGACY_CAPABILITY_SCHEMA
        else False
    )
    if not valid_fields:
        raise AgentIdentityError("identity capability fields are invalid")
    return data


def verify_agent_identity_capability(
    project_root: Path,
    db_path: Path,
    scope: str,
    claimed_agent_id: str,
    capability_path: Path,
    *,
    immutable: bool = False,
) -> AgentIdentityCapability:
    """Verify a capability without mutating the database or local state."""

    normalized_scope = _identifier(scope, "scope")
    normalized_agent = _identifier(claimed_agent_id, "agent_id")
    root_key = workspace_root_key(project_root)
    data = _read_capability_file(db_path, capability_path)
    schema = data.get("schema")
    if schema not in {CAPABILITY_SCHEMA, LEGACY_CAPABILITY_SCHEMA}:
        raise AgentIdentityError("unsupported identity capability schema")
    allowed_tools = (
        _allowed_tools(data.get("allowed_tools"))
        if schema == CAPABILITY_SCHEMA
        else ()
    )
    issued_by = (
        _identifier(str(data.get("issued_by") or ""), "identity issuer")
        if schema == CAPABILITY_SCHEMA
        else "legacy"
    )
    route_binding = (
        _route_binding(data.get("route_binding"))
        if schema == CAPABILITY_SCHEMA and "route_binding" in data
        else None
    )
    bound_room_id = (
        _bound_room_id(data.get("bound_room_id"))
        if schema == CAPABILITY_SCHEMA and "bound_room_id" in data
        else None
    )
    bound_room_session_id = (
        _optional_bound_identifier(
            data.get("bound_room_session_id"), "bound_room_session_id"
        )
        if schema == CAPABILITY_SCHEMA and "bound_room_session_id" in data
        else None
    )
    bound_route_profile_id = (
        _optional_bound_identifier(
            data.get("bound_route_profile_id"), "bound_route_profile_id"
        )
        if schema == CAPABILITY_SCHEMA and "bound_route_profile_id" in data
        else None
    )
    bound_route_profile_sha256 = (
        _optional_bound_sha256(
            data.get("bound_route_profile_sha256"), "bound_route_profile_sha256"
        )
        if schema == CAPABILITY_SCHEMA and "bound_route_profile_sha256" in data
        else None
    )
    capability_id = _identifier(str(data.get("capability_id") or ""), "capability_id")
    token = str(data.get("secret_token") or "")
    token_sha256 = str(data.get("token_sha256") or "").lower()
    capability_sha256 = str(data.get("capability_sha256") or "").lower()
    if len(token) < 48 or not _SHA256.fullmatch(token_sha256):
        raise AgentIdentityError("identity capability secret is invalid")
    if not _SHA256.fullmatch(capability_sha256):
        raise AgentIdentityError("identity capability digest is invalid")
    if not secrets.compare_digest(_sha256_bytes(token.encode("utf-8")), token_sha256):
        raise AgentIdentityError("identity capability secret does not match")
    if not secrets.compare_digest(_stable_sha256(_capability_payload(data)), capability_sha256):
        raise AgentIdentityError("identity capability descriptor does not match")
    if (
        data.get("workspace_root_key") != root_key
        or data.get("scope") != normalized_scope
        or data.get("agent_id") != normalized_agent
    ):
        raise AgentIdentityError("identity capability is bound to another Agent session")

    expected_path = _resolved_registered_path(
        db_path, str(data.get("secret_file_relpath") or "")
    )
    if not secrets.compare_digest(
        os.path.normcase(str(expected_path)),
        os.path.normcase(str(Path(capability_path).resolve())),
    ):
        raise AgentIdentityError("identity capability path does not match its descriptor")

    try:
        with _read_only_connection(db_path, immutable=immutable) as connection:
            row = connection.execute(
                """SELECT * FROM agent_identity_capabilities
                    WHERE scope=? AND capability_id=?""",
                (normalized_scope, capability_id),
            ).fetchone()
    except sqlite3.Error as exc:
        raise AgentIdentityError("identity capability registry is unavailable") from exc
    if row is None or row["revoked_utc"] is not None:
        raise AgentIdentityError("identity capability is unknown or revoked")
    registered = {
        "workspace_root_key": root_key,
        "agent_id": normalized_agent,
        "secret_file_relpath": str(data["secret_file_relpath"]),
        "token_sha256": token_sha256,
        "capability_sha256": capability_sha256,
    }
    for key, expected in registered.items():
        observed = str(row[key] or "")
        if not secrets.compare_digest(observed, expected):
            raise AgentIdentityError("identity capability registry binding does not match")
    return AgentIdentityCapability(
        capability_id=capability_id,
        agent_id=normalized_agent,
        scope=normalized_scope,
        path=Path(capability_path).resolve(),
        capability_sha256=capability_sha256,
        schema=str(schema),
        allowed_tools=allowed_tools,
        issued_by=issued_by,
        route_binding=route_binding,
        bound_room_id=bound_room_id,
        bound_room_session_id=bound_room_session_id,
        bound_route_profile_id=bound_route_profile_id,
        bound_route_profile_sha256=bound_route_profile_sha256,
    )


def verify_agent_identity_route_binding(
    capability: AgentIdentityCapability,
    *,
    client_name: str | None,
    provider_id: str | None,
    model_id: str | None,
    reasoning_mode: str | None,
    route_class: str | None,
) -> None:
    """Require a launched stdio route to match its pre-issued route evidence."""

    observed = _route_binding(
        {
            "client_name": client_name,
            "provider_id": provider_id,
            "model_id": model_id,
            "reasoning_mode": reasoning_mode,
            "route_class": route_class,
        }
    )
    if capability.route_binding is not None:
        if observed != capability.route_binding:
            raise AgentIdentityError(
                "Agent identity capability is bound to another exact route"
            )
        return
    if (
        capability.schema == CAPABILITY_SCHEMA
        and any(getattr(observed, field) is not None for field in _ROUTE_BINDING_FIELDS)
    ):
        raise AgentIdentityError(
            "unbound v2 identity capabilities cannot self-attest route labels"
        )


def verify_agent_identity_launch_args(
    args: Sequence[str],
    *,
    db_path: Path,
    scope: str,
    claimed_agent_id: str,
) -> tuple[list[str], dict[str, Any]]:
    """Verify one stdio launch and return args with the secret-file path redacted."""

    values = list(args)
    if values[:3] != ["-m", "peerbridge_mcp", "serve"]:
        raise AgentIdentityError("Agent identity launch is not a PeerBridge stdio server")
    _, project_root_value = _single_launch_option(values, "--project-root")
    _, database_value = _single_launch_option(values, "--db")
    _, scope_value = _single_launch_option(values, "--scope")
    _, agent_value = _single_launch_option(values, "--agent-id")
    capability_index, capability_value = _single_launch_option(
        values, "--identity-capability"
    )

    project_root = Path(project_root_value)
    configured_database = Path(database_value)
    capability_path = Path(capability_value)
    if not project_root.is_absolute() or not project_root.is_dir():
        raise AgentIdentityError("Agent identity project root is invalid")
    if not configured_database.is_absolute() or not configured_database.is_file():
        raise AgentIdentityError("Agent identity database path is invalid")
    expected_database = Path(db_path)
    if not expected_database.is_absolute() or not expected_database.is_file():
        raise AgentIdentityError("Agent identity evidence database is invalid")
    if not _same_path(configured_database, expected_database):
        raise AgentIdentityError("Agent identity launch is bound to another database")
    if scope_value != str(scope) or agent_value != str(claimed_agent_id):
        raise AgentIdentityError("Agent identity launch route does not match the receipt")
    capability = verify_agent_identity_capability(
        project_root,
        expected_database,
        scope,
        claimed_agent_id,
        capability_path,
    )
    verify_agent_identity_route_binding(
        capability,
        client_name=_optional_launch_option(values, "--client-name"),
        provider_id=_optional_launch_option(values, "--provider-id"),
        model_id=_optional_launch_option(values, "--model-id"),
        reasoning_mode=_optional_launch_option(values, "--reasoning-mode"),
        route_class=_optional_launch_option(values, "--route-class"),
    )
    sanitized = list(values)
    sanitized[capability_index + 1] = REDACTED_CAPABILITY_ARGUMENT
    return sanitized, {
        "bound": True,
        "capability_id": capability.capability_id,
        "capability_sha256": capability.capability_sha256,
        "workspace_root_key": workspace_root_key(project_root),
        "project_root": str(project_root.resolve()),
        "database_path": str(expected_database.resolve()),
    }


def verify_agent_identity_binding_record(
    binding: Any,
    *,
    db_path: Path,
    scope: str,
    claimed_agent_id: str,
) -> AgentIdentityCapability:
    """Revalidate a redacted receipt binding without recording its secret-file path."""

    expected_keys = {
        "bound",
        "capability_id",
        "capability_sha256",
        "workspace_root_key",
        "project_root",
        "database_path",
    }
    if not isinstance(binding, dict) or set(binding) != expected_keys:
        raise AgentIdentityError("Agent identity receipt binding is invalid")
    if binding.get("bound") is not True:
        raise AgentIdentityError("Agent identity receipt is not capability-bound")
    project_root = Path(str(binding.get("project_root") or ""))
    database = Path(str(binding.get("database_path") or ""))
    expected_database = Path(db_path)
    if (
        not project_root.is_absolute()
        or not project_root.is_dir()
        or not database.is_absolute()
        or not expected_database.is_absolute()
        or not expected_database.is_file()
        or not _same_path(database, expected_database)
    ):
        raise AgentIdentityError("Agent identity receipt paths are invalid")
    if binding.get("workspace_root_key") != workspace_root_key(project_root):
        raise AgentIdentityError("Agent identity receipt workspace binding drifted")
    capability_id = _identifier(
        str(binding.get("capability_id") or ""), "capability_id"
    )
    try:
        with _read_only_connection(expected_database) as connection:
            row = connection.execute(
                """SELECT secret_file_relpath
                     FROM agent_identity_capabilities
                    WHERE scope=? AND capability_id=?""",
                (_identifier(scope, "scope"), capability_id),
            ).fetchone()
    except sqlite3.Error as exc:
        raise AgentIdentityError("identity capability registry is unavailable") from exc
    if row is None:
        raise AgentIdentityError("identity capability is unknown")
    capability = verify_agent_identity_capability(
        project_root,
        expected_database,
        scope,
        claimed_agent_id,
        _resolved_registered_path(expected_database, str(row["secret_file_relpath"])),
    )
    if not secrets.compare_digest(
        capability.capability_sha256,
        str(binding.get("capability_sha256") or ""),
    ):
        raise AgentIdentityError("Agent identity receipt capability digest drifted")
    return capability


def verify_redacted_agent_identity_launch_args(
    args: Sequence[str],
    binding: Any,
    *,
    db_path: Path,
    scope: str,
    claimed_agent_id: str,
) -> AgentIdentityCapability:
    """Verify sanitized receipt args and their live capability-registry binding."""

    values = list(args)
    if not all(isinstance(value, str) for value in values):
        raise AgentIdentityError("Agent identity receipt arguments are invalid")
    if values[:3] != ["-m", "peerbridge_mcp", "serve"]:
        raise AgentIdentityError("Agent identity receipt is not a PeerBridge stdio server")
    _, project_root_value = _single_launch_option(values, "--project-root")
    _, database_value = _single_launch_option(values, "--db")
    _, scope_value = _single_launch_option(values, "--scope")
    _, agent_value = _single_launch_option(values, "--agent-id")
    _, capability_value = _single_launch_option(values, "--identity-capability")
    if capability_value != REDACTED_CAPABILITY_ARGUMENT:
        raise AgentIdentityError("Agent identity receipt capability path is not redacted")

    project_root = Path(project_root_value)
    configured_database = Path(database_value)
    expected_database = Path(db_path)
    if not project_root.is_absolute() or not project_root.is_dir():
        raise AgentIdentityError("Agent identity receipt project root is invalid")
    if (
        not configured_database.is_absolute()
        or not expected_database.is_absolute()
        or not expected_database.is_file()
        or not _same_path(configured_database, expected_database)
    ):
        raise AgentIdentityError("Agent identity receipt database binding is invalid")
    if scope_value != str(scope) or agent_value != str(claimed_agent_id):
        raise AgentIdentityError("Agent identity receipt route does not match")

    capability = verify_agent_identity_binding_record(
        binding,
        db_path=expected_database,
        scope=scope,
        claimed_agent_id=claimed_agent_id,
    )
    verify_agent_identity_route_binding(
        capability,
        client_name=_optional_launch_option(values, "--client-name"),
        provider_id=_optional_launch_option(values, "--provider-id"),
        model_id=_optional_launch_option(values, "--model-id"),
        reasoning_mode=_optional_launch_option(values, "--reasoning-mode"),
        route_class=_optional_launch_option(values, "--route-class"),
    )
    if not _same_path(project_root, Path(str(binding["project_root"]))):
        raise AgentIdentityError("Agent identity receipt project root binding drifted")
    if not _same_path(configured_database, Path(str(binding["database_path"]))):
        raise AgentIdentityError("Agent identity receipt database binding drifted")
    return capability


def ensure_agent_identity_capability(
    project_root: Path,
    db_path: Path,
    scope: str,
    agent_id: str,
    *,
    allowed_tools: Sequence[str] = (),
    issued_by: str = "peerbridge-managed-runtime",
    route_binding: Mapping[str, Any] | AgentIdentityRouteBinding | None = None,
    bound_room_id: str | None = None,
    bound_room_session_id: str | None = None,
    bound_route_profile_id: str | None = None,
    bound_route_profile_sha256: str | None = None,
) -> AgentIdentityCapability:
    """Return an existing valid capability or create one with a new random secret."""

    normalized_scope = _identifier(scope, "scope")
    normalized_agent = _identifier(agent_id, "agent_id")
    normalized_tools = _allowed_tools(tuple(allowed_tools))
    normalized_issuer = _identifier(issued_by, "identity issuer")
    normalized_route = _route_binding(route_binding)
    normalized_room = _bound_room_id(bound_room_id)
    normalized_room_session = _optional_bound_identifier(
        bound_room_session_id, "bound_room_session_id"
    )
    normalized_route_profile = _optional_bound_identifier(
        bound_route_profile_id, "bound_route_profile_id"
    )
    normalized_route_profile_sha = _optional_bound_sha256(
        bound_route_profile_sha256, "bound_route_profile_sha256"
    )
    if normalized_room is None and any(
        value is not None
        for value in (
            normalized_room_session,
            normalized_route_profile,
            normalized_route_profile_sha,
        )
    ):
        raise AgentIdentityError("room revision binding requires bound_room_id")
    if (normalized_route_profile is None) != (
        normalized_route_profile_sha is None
    ):
        raise AgentIdentityError("route profile ID and SHA binding must be paired")
    database = Path(db_path).resolve()
    root_key = workspace_root_key(project_root)
    try:
        with _read_only_connection(database) as connection:
            rows = connection.execute(
                """SELECT capability_id, secret_file_relpath
                     FROM agent_identity_capabilities
                    WHERE scope=? AND workspace_root_key=? AND agent_id=?
                      AND revoked_utc IS NULL
                    ORDER BY created_utc DESC, capability_id DESC""",
                (normalized_scope, root_key, normalized_agent),
            ).fetchall()
    except sqlite3.Error as exc:
        raise AgentIdentityError(
            "identity capability registry is unavailable; run peerbridge migrate"
        ) from exc
    for row in rows:
        candidate = _resolved_registered_path(database, row["secret_file_relpath"])
        try:
            verified = verify_agent_identity_capability(
                project_root,
                database,
                normalized_scope,
                normalized_agent,
                candidate,
            )
            if (
                verified.schema == CAPABILITY_SCHEMA
                and verified.allowed_tools == normalized_tools
                and verified.issued_by == normalized_issuer
                and verified.route_binding == normalized_route
                and verified.bound_room_id == normalized_room
                and verified.bound_room_session_id == normalized_room_session
                and verified.bound_route_profile_id == normalized_route_profile
                and verified.bound_route_profile_sha256
                == normalized_route_profile_sha
            ):
                return verified
        except AgentIdentityError:
            continue

    capability_id = f"identity-{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(48)
    token_sha256 = _sha256_bytes(token.encode("utf-8"))
    issued_utc = _utc_now()
    capability_root = _capability_root(database)
    _reject_capability_reparse_ancestry(database, capability_root)
    capability_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_capability_reparse_ancestry(database, capability_root)
    try:
        os.chmod(capability_root, 0o700)
    except OSError:
        pass
    path = capability_root / f"{capability_id}.json"
    _reject_capability_reparse_ancestry(database, path)
    relative_path = path.relative_to(_state_root(database)).as_posix()
    descriptor = {
        "schema": CAPABILITY_SCHEMA,
        "capability_id": capability_id,
        "workspace_root_key": root_key,
        "scope": normalized_scope,
        "agent_id": normalized_agent,
        "secret_file_relpath": relative_path,
        "issued_utc": issued_utc,
        "token_sha256": token_sha256,
        "allowed_tools": list(normalized_tools),
        "issued_by": normalized_issuer,
        "route_binding": _route_binding_payload(normalized_route),
        "bound_room_id": normalized_room,
        "bound_room_session_id": normalized_room_session,
        "bound_route_profile_id": normalized_route_profile,
        "bound_route_profile_sha256": normalized_route_profile_sha,
    }
    capability_sha256 = _stable_sha256(descriptor)
    content = {
        **descriptor,
        "capability_sha256": capability_sha256,
        "secret_token": token,
    }
    encoded = (
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor_handle = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor_handle, "wb", closefd=True) as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    try:
        with sqlite3.connect(
            database,
            timeout=10,
            factory=_ClosingConnection,
        ) as connection:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO agent_identity_capabilities(
                       scope, capability_id, workspace_root_key, agent_id,
                       secret_file_relpath, token_sha256, capability_sha256,
                       created_utc, revoked_utc
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    normalized_scope,
                    capability_id,
                    root_key,
                    normalized_agent,
                    relative_path,
                    token_sha256,
                    capability_sha256,
                    issued_utc,
                ),
            )
    except sqlite3.Error as exc:
        raise AgentIdentityError(
            "identity capability registry write failed; an unregistered local file remains"
        ) from exc
    return verify_agent_identity_capability(
        project_root,
        database,
        normalized_scope,
        normalized_agent,
        path,
    )


def revoke_agent_identity_capability(
    db_path: Path,
    scope: str,
    capability_id: str,
) -> bool:
    normalized_scope = _identifier(scope, "scope")
    normalized_id = _identifier(capability_id, "capability_id")
    with sqlite3.connect(
        Path(db_path).resolve(),
        timeout=10,
        factory=_ClosingConnection,
    ) as connection:
        connection.execute("PRAGMA busy_timeout=10000")
        cursor = connection.execute(
            """UPDATE agent_identity_capabilities
                  SET revoked_utc=?
                WHERE scope=? AND capability_id=? AND revoked_utc IS NULL""",
            (_utc_now(), normalized_scope, normalized_id),
        )
        return cursor.rowcount == 1
