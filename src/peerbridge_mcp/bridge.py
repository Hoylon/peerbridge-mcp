"""Auditable local coordination primitives used by the PeerBridge MCP server."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable

from .attachments import (
    CHAT_ATTACHMENT_ROOT,
    CHAT_ATTACHMENT_SUFFIXES,
    MAX_CHAT_ATTACHMENT_BYTES,
    MAX_CHAT_ATTACHMENT_COUNT,
)
from .inference_receipts import (
    InferenceReceiptError,
    validate_inference_receipt,
)
from .secret_scan import contains_secret, contains_secret_bytes
from .usage import UsageError, unavailable_usage, usage_from_receipt, validate_usage


ZERO_SHA256 = "0" * 64
DEFAULT_PRESENCE_TTL_SECONDS = 120
DEFAULT_LEASE_SECONDS = 900
MAX_LEASE_SECONDS = 86_400
DEFAULT_DISPATCH_LEASE_SECONDS = 300
MAX_DISPATCH_ATTEMPTS = 5
DEFAULT_DISPATCH_RETRY_SECONDS = 15
MAX_DISPATCH_RETRY_SECONDS = 86_400
MAX_TEXT_CHARS = 50_000
MAX_ROUTE_INFERENCE_TIMEOUT_SECONDS = 300
SCHEMA_VERSION = "27"
DEFAULT_ROOM_ID = "lobby"
HUMAN_OPERATOR_ID = "human-operator"
CONTROL_ROOM_WORKFLOW_ID = "control-room-workflow"
RELEASE_GATE_ARTIFACT_ROOT = ".peerbridge-artifacts/release-gates"
DEFAULT_ROOM_ROLE = "equal-participant"
ROOM_MEMBER_ROLES = {
    DEFAULT_ROOM_ROLE,
    "researcher",
    "implementer",
    "reviewer",
    "custom",
}
ROUTE_CLASSES = {"official", "relay", "local"}
SECRET_BACKENDS = {"windows-credential-manager", "cc-switch", "native-acp"}
MEMORY_VISIBILITIES = {"private", "room", "project"}
MEMORY_RECORD_TYPES = {"FACT", "DECISION", "CONSTRAINT", "PREFERENCE", "DEPRECATED"}
ROOM_AUTOMATION_MODES = {"off", "once", "discussion"}
DISCUSSION_STATUSES = {"active", "paused", "waiting_human", "completed", "stopped"}
DISCUSSION_SIGNALS = {"CONTINUE", "CONSENSUS", "BLOCKED"}
DEFAULT_DISCUSSION_MAX_ROUNDS = 4
DEFAULT_DISCUSSION_MAX_MESSAGES = 40
DEFAULT_DISCUSSION_STAGNATION_ROUNDS = 2
MAX_DISCUSSION_ROUNDS = 20
MAX_DISCUSSION_MESSAGES = 200
MAX_DISCUSSION_CONTEXT_CHARS = 100_000
DEFAULT_ROOM_CONTEXT_MESSAGES = 24
MAX_ROOM_CONTEXT_MESSAGES = 100
DEFAULT_ROOM_CONTEXT_CHARS = 24_000
MAX_ROOM_CONTEXT_CHARS = 100_000
MAX_ROOM_CONTEXT_MESSAGE_CHARS = 8_000
MAX_ROOM_FANOUT_RECIPIENTS = 32
MAX_MEMORY_ARTIFACTS = 20
MAX_MEMORY_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_MEMORY_ARTIFACT_TOTAL_BYTES = 64 * 1024 * 1024
MAX_SCOPE_MESSAGE_ROWS = 200_000
MAX_SCOPE_MESSAGE_BYTES = 256 * 1024 * 1024
MAX_SCOPE_EVENT_ROWS = 1_000_000
MAX_SCOPE_EVENT_BYTES = 1024 * 1024 * 1024
MAX_MCP_HASH_BYTES = 256 * 1024 * 1024
MAX_MCP_HASH_SECONDS = 15.0
MAX_PROOF_FILES = 100
MAX_PROOF_TOTAL_BYTES = 256 * 1024 * 1024
MAX_PROOF_HASH_SECONDS = 15.0
MAX_TASK_PATHS = 100
MAX_REQUIRED_PEERS = 32
MAX_ACTIVE_TASK_PATH_ROWS = 10_000
MAX_MCP_AUDIT_EVENTS = MAX_SCOPE_EVENT_ROWS
MAX_MCP_AUDIT_SECONDS = 60.0
DISCUSSION_ORCHESTRATOR_ID = "peerbridge-orchestrator"
DISCUSSION_COORDINATOR_ID = "mailbox-supervisor"
RESERVED_INTERNAL_AGENT_IDS = frozenset(
    {
        CONTROL_ROOM_WORKFLOW_ID,
        "control-room-migrator",
        DISCUSSION_COORDINATOR_ID,
        DISCUSSION_ORCHESTRATOR_ID,
    }
)
GOVERNANCE_OPERATION_PAYLOAD_FIELDS = (
    "scope",
    "operation_id",
    "workflow_id",
    "requested_by",
    "task_text",
    "working_directory",
    "resource_key",
    "permission_decision_id",
    "bound_discussion_id",
    "status",
    "attempt_count",
    "max_attempts",
    "timeout_seconds",
    "not_before_epoch",
    "lease_owner",
    "lease_token_sha256",
    "lease_expires_epoch",
    "attempt_deadline_epoch",
    "cancellation_requested",
    "terminal_outcome",
    "terminal_detail",
    "created_utc",
    "updated_utc",
)
TRUSTED_INFERENCE_RECEIPT_PAYLOAD_FIELDS = (
    "scope",
    "message_id",
    "agent_id",
    "session_id",
    "attempt_count",
    "lease_token_sha256",
    "source_content_sha256",
    "source_route_request_sha256",
    "receipt_schema",
    "inference_receipt_sha256",
    "assistant_message_sha256",
    "assistant_content_sha256",
    "reply_body_sha256",
    "inference_usage_sha256",
    "route_profile_id",
    "route_profile_sha256",
    "connection_id",
    "connection_sha256",
    "provider_id",
    "model_id",
    "response_model_id",
    "reasoning_mode",
    "route_class",
    "room_id",
    "recorded_utc",
)

SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
SAFE_MODEL_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,500}\Z")
PATCH_DESTRUCTIVE = re.compile(
    r"(?im)^---\s+/dev/null|^\+\+\+\s+/dev/null|^diff --git\s+.*(?:\.git/|\.peerbridge/)"
)
SENSITIVE_PARTS = {
    ".env",
    ".aws",
    ".ssh",
    ".azure",
    ".docker",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".terraform.d",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets",
    "secret",
    "private_key",
}
SENSITIVE_PATH_PREFIXES = ((".config", "gcloud"),)
SENSITIVE_SUFFIXES = {
    ".der",
    ".jks",
    ".kdbx",
    ".key",
    ".mobileconfig",
    ".ovpn",
    ".p12",
    ".pem",
    ".pfx",
}
WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class BridgeError(Exception):
    """Expected error that is safe to return to an MCP client."""


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back, then deterministically release the SQLite handle."""

    def __exit__(self, exc_type, exc_value, traceback):  # type: ignore[no-untyped-def]
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def governance_operation_payload(
    row: sqlite3.Row | dict[str, Any],
) -> dict[str, Any]:
    return {key: row[key] for key in GOVERNANCE_OPERATION_PAYLOAD_FIELDS}


def trusted_inference_receipt_payload(
    row: sqlite3.Row | dict[str, Any],
) -> dict[str, Any]:
    return {key: row[key] for key in TRUSTED_INFERENCE_RECEIPT_PAYLOAD_FIELDS}


def _require_identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text) or text in {".", ".."}:
        raise BridgeError(
            f"{label} must contain only letters, digits, dot, underscore, colon or dash"
        )
    return text


def _optional_identifier(value: Any, label: str) -> str | None:
    text = str(value or "").strip()
    return _require_identifier(text, label) if text else None


def _optional_model_identifier(value: Any, label: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not SAFE_MODEL_ID.fullmatch(text):
        raise BridgeError(
            f"{label} must contain only letters, digits, dot, underscore, colon, "
            "slash or dash"
        )
    return text


def _optional_route_class(value: Any, label: str = "route_class") -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text not in ROUTE_CLASSES:
        raise BridgeError(f"{label} must be official, relay or local")
    return text


def _optional_inference_timeout_seconds(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+", text):
        raise BridgeError("inference_timeout_seconds must be an integer")
    timeout_seconds = int(text)
    if not 1 <= timeout_seconds <= MAX_ROUTE_INFERENCE_TIMEOUT_SECONDS:
        raise BridgeError(
            "inference_timeout_seconds must be between 1 and "
            f"{MAX_ROUTE_INFERENCE_TIMEOUT_SECONDS}"
        )
    return timeout_seconds


def _require_sha256(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise BridgeError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _require_text(value: Any, label: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "").strip()
    if not text:
        raise BridgeError(f"{label} is required")
    if len(text) > limit:
        raise BridgeError(f"{label} exceeds {limit} characters")
    if contains_secret(text):
        raise BridgeError(f"{label} appears to contain a credential or private key")
    return text


def _json_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BridgeError(f"{label} must be an array")
    return value


def _path_parts_are_sensitive(path: Path) -> bool:
    lowered_parts = tuple(part.lower().rstrip(" .") for part in path.parts)
    if any(
        lowered_parts[index : index + len(prefix)] == prefix
        for prefix in SENSITIVE_PATH_PREFIXES
        for index in range(0, len(lowered_parts) - len(prefix) + 1)
    ):
        return True
    for part in path.parts:
        lowered = part.lower()
        basename = lowered.split(":", 1)[0].rstrip(" .")
        stem = basename.split(".", 1)[0]
        if (
            ":" in lowered
            or basename.startswith(".env.")
            or basename in SENSITIVE_PARTS
            or stem in WINDOWS_RESERVED_NAMES
            or Path(basename).suffix in SENSITIVE_SUFFIXES
        ):
            return True
    return False


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


def _reject_reparse_ancestry(root: Path, target: Path, label: str) -> None:
    lexical_root = Path(root).absolute()
    lexical_target = Path(target).absolute()
    try:
        relative = lexical_target.relative_to(lexical_root)
    except ValueError as exc:
        raise BridgeError(f"{label} escapes local state") from exc
    current = lexical_root
    if _is_link_or_reparse(current):
        raise BridgeError(f"{label} crosses a link or reparse point")
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise BridgeError(f"{label} crosses a link or reparse point")


class Bridge:
    """One local agent session sharing an append-only SQLite coordination store."""

    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        agent_id: str,
        scope: str = "default",
        *,
        session_id: str | None = None,
        client_name: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        reasoning_mode: str | None = None,
        route_class: str | None = None,
        discussion_coordinator: bool = False,
        presence_ttl_seconds: int = DEFAULT_PRESENCE_TTL_SECONDS,
        protected_paths: Iterable[str] = (),
    ) -> None:
        lexical_root = Path(project_root).absolute()
        lexical_db = Path(db_path).absolute()
        self.root = lexical_root.resolve()
        if not self.root.is_dir():
            raise BridgeError(f"project root does not exist: {self.root}")
        lexical_default_state = lexical_root / ".peerbridge"
        try:
            lexical_db.relative_to(lexical_default_state)
            uses_default_state = True
        except ValueError:
            uses_default_state = False
        if uses_default_state:
            if _is_link_or_reparse(lexical_default_state):
                raise BridgeError(
                    "default .peerbridge state must not be a link or reparse point"
                )
            _reject_reparse_ancestry(
                lexical_default_state,
                lexical_db,
                "default .peerbridge database",
            )
            resolved_state = lexical_default_state.resolve()
            try:
                resolved_state.relative_to(self.root)
            except ValueError as exc:
                raise BridgeError("default .peerbridge state escapes the project root") from exc
        self.db_path = lexical_db.resolve()
        self.agent_id = _require_identifier(agent_id, "agent_id")
        self.scope = _require_identifier(scope, "scope")
        self.session_id = _require_identifier(
            session_id or uuid.uuid4().hex, "session_id"
        )
        self.client_name = _optional_identifier(client_name, "client_name")
        self.provider_id = _optional_identifier(provider_id, "provider_id")
        self.model_id = _optional_identifier(model_id, "model_id")
        self.reasoning_mode = _optional_identifier(reasoning_mode, "reasoning_mode")
        self.route_class = _optional_route_class(route_class)
        self._discussion_coordinator = bool(discussion_coordinator)
        self.presence_ttl_seconds = max(30, min(int(presence_ttl_seconds), 3600))
        self.state_root = self.db_path.parent
        canonical_root = os.path.normcase(str(self.root))
        if os.name == "nt":
            canonical_root = canonical_root.casefold()
        self.workspace_root_key = sha256_bytes(canonical_root.encode("utf-8"))
        self.draft_root = self.state_root / "drafts"
        defaults = [".git", ".peerbridge", ".peerbridge-artifacts"]
        self.protected_paths = tuple(
            sorted({self._normalize_path(item) for item in [*defaults, *protected_paths]})
        )
        _reject_reparse_ancestry(self.state_root, self.db_path, "PeerBridge database")
        _reject_reparse_ancestry(self.state_root, self.draft_root, "PeerBridge drafts")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.draft_root.mkdir(parents=True, exist_ok=True)
        _reject_reparse_ancestry(self.state_root, self.db_path, "PeerBridge database")
        _reject_reparse_ancestry(self.state_root, self.draft_root, "PeerBridge drafts")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=10,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def checkpoint_wal(self) -> tuple[int, int, int]:
        """Merge committed WAL pages after an owned writer shuts down."""

        with self._connect() as connection:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None:
            raise BridgeError("SQLite WAL checkpoint returned no result")
        return int(row[0]), int(row[1]), int(row[2])

    def _init_db(self) -> None:
        with self._connect() as connection:
            for attempt in range(40):
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 39:
                        raise
                    time.sleep(0.25)
            connection.executescript(
                """
                BEGIN EXCLUSIVE;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL,
                    room_id TEXT NOT NULL DEFAULT 'lobby',
                    task_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    reply_to TEXT,
                    artifact_paths_json TEXT NOT NULL,
                    route_profile_id TEXT,
                    route_profile_sha256 TEXT,
                    requested_provider_id TEXT,
                    requested_model_id TEXT,
                    requested_reasoning_mode TEXT,
                    requested_route_class TEXT,
                    route_request_sha256 TEXT,
                    discussion_id TEXT,
                    discussion_round INTEGER,
                    discussion_role TEXT,
                    visibility TEXT NOT NULL DEFAULT 'direct',
                    created_utc TEXT NOT NULL,
                    acknowledged_utc TEXT,
                    content_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_inbox
                    ON messages(scope, recipient, sequence);
                CREATE INDEX IF NOT EXISTS idx_messages_scope_created
                    ON messages(scope, created_utc DESC, sequence DESC);
                CREATE TABLE IF NOT EXISTS message_receipts (
                    scope TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    acknowledged_utc TEXT NOT NULL,
                    PRIMARY KEY(scope, message_id, agent_id),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id)
                );
                CREATE TABLE IF NOT EXISTS scope_storage_usage (
                    scope TEXT PRIMARY KEY,
                    message_rows INTEGER NOT NULL,
                    message_bytes INTEGER NOT NULL,
                    event_rows INTEGER NOT NULL DEFAULT 0,
                    event_bytes INTEGER NOT NULL DEFAULT 0,
                    updated_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS message_route_receipts (
                    scope TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    observed_provider_id TEXT,
                    observed_model_id TEXT,
                    observed_reasoning_mode TEXT,
                    observed_route_class TEXT,
                    route_status TEXT NOT NULL,
                    acknowledged_utc TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, message_id, agent_id, session_id),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_message_route_receipt_agent
                    ON message_route_receipts(scope, message_id, agent_id);
                CREATE TABLE IF NOT EXISTS message_dispatches (
                    scope TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    claimed_session_id TEXT,
                    lease_token_sha256 TEXT,
                    lease_expires_epoch REAL,
                    attempt_count INTEGER NOT NULL,
                    claimed_utc TEXT,
                    updated_utc TEXT NOT NULL,
                    completed_utc TEXT,
                    reply_message_id TEXT,
                    inference_receipt_sha256 TEXT,
                    error_code TEXT,
                    dispatch_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, message_id, agent_id),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id),
                    FOREIGN KEY(reply_message_id) REFERENCES messages(message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_message_dispatches_status
                    ON message_dispatches(scope, agent_id, status, lease_expires_epoch);
                CREATE INDEX IF NOT EXISTS idx_message_dispatches_scope_updated
                    ON message_dispatches(scope, updated_utc DESC, message_id);
                CREATE TABLE IF NOT EXISTS trusted_inference_receipts (
                    scope TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    lease_token_sha256 TEXT NOT NULL,
                    source_content_sha256 TEXT NOT NULL,
                    source_route_request_sha256 TEXT,
                    receipt_schema TEXT NOT NULL,
                    inference_receipt_sha256 TEXT NOT NULL,
                    assistant_message_sha256 TEXT NOT NULL,
                    assistant_content_sha256 TEXT NOT NULL,
                    reply_body_sha256 TEXT NOT NULL,
                    inference_usage_sha256 TEXT NOT NULL,
                    route_profile_id TEXT,
                    route_profile_sha256 TEXT,
                    connection_id TEXT,
                    connection_sha256 TEXT,
                    provider_id TEXT,
                    model_id TEXT,
                    response_model_id TEXT,
                    reasoning_mode TEXT,
                    route_class TEXT,
                    room_id TEXT NOT NULL,
                    recorded_utc TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, message_id, agent_id, attempt_count),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trusted_inference_receipt_sha
                    ON trusted_inference_receipts(
                        scope, message_id, agent_id, inference_receipt_sha256
                    );
                CREATE TABLE IF NOT EXISTS inference_usage (
                    scope TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    reply_message_id TEXT NOT NULL,
                    route_profile_id TEXT,
                    provider_id TEXT,
                    model_id TEXT,
                    reasoning_mode TEXT,
                    route_class TEXT,
                    usage_status TEXT NOT NULL,
                    usage_source TEXT NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    input_tokens_reported_calls INTEGER NOT NULL DEFAULT 0,
                    output_tokens_reported_calls INTEGER NOT NULL DEFAULT 0,
                    total_tokens_reported_calls INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens_reported_calls INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens_reported_calls INTEGER NOT NULL DEFAULT 0,
                    reported_calls INTEGER NOT NULL,
                    total_calls INTEGER NOT NULL,
                    total_tokens_derived INTEGER NOT NULL,
                    recorded_utc TEXT NOT NULL,
                    inference_receipt_sha256 TEXT NOT NULL,
                    usage_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, message_id, agent_id),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id),
                    FOREIGN KEY(reply_message_id) REFERENCES messages(message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_inference_usage_scope_time
                    ON inference_usage(scope, recorded_utc DESC, message_id);
                CREATE INDEX IF NOT EXISTS idx_inference_usage_scope_model
                    ON inference_usage(scope, provider_id, model_id, recorded_utc DESC);
                CREATE TABLE IF NOT EXISTS message_dispatch_retry_schedules (
                    scope TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    not_before_epoch REAL NOT NULL,
                    error_code TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    schedule_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, message_id, agent_id, attempt_count),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_dispatch_retry_schedule_due
                    ON message_dispatch_retry_schedules(
                        scope, agent_id, not_before_epoch, message_id
                    );
                CREATE TABLE IF NOT EXISTS route_profiles (
                    scope TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    client_name TEXT,
                    provider_id TEXT,
                    model_id TEXT,
                    response_model_id TEXT,
                    inference_timeout_seconds INTEGER,
                    reasoning_mode TEXT,
                    route_class TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    profile_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, route_id)
                );
                CREATE TABLE IF NOT EXISTS provider_connections (
                    scope TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    route_class TEXT NOT NULL,
                    provider_id TEXT,
                    secret_backend TEXT NOT NULL,
                    credential_target TEXT NOT NULL,
                    endpoint_sha256 TEXT NOT NULL,
                    credential_fingerprint_sha256 TEXT NOT NULL,
                    descriptor_schema TEXT,
                    credential_version_sha256 TEXT,
                    enabled INTEGER NOT NULL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    connection_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, connection_id)
                );
                CREATE TABLE IF NOT EXISTS consumer_cursors (
                    scope TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    consumer TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    updated_utc TEXT NOT NULL,
                    PRIMARY KEY(scope, channel, consumer)
                );
                CREATE TABLE IF NOT EXISTS rooms (
                    scope TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    archived INTEGER NOT NULL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    room_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, room_id)
                );
                CREATE TABLE IF NOT EXISTS room_memberships (
                    scope TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    route_profile_id TEXT,
                    room_session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    joined_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    left_utc TEXT,
                    membership_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, room_id, agent_id),
                    FOREIGN KEY(scope, room_id) REFERENCES rooms(scope, room_id)
                );
                CREATE TABLE IF NOT EXISTS room_member_roles (
                    scope TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    room_session_id TEXT NOT NULL,
                    role_id TEXT NOT NULL,
                    role_label TEXT,
                    updated_by TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    role_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, room_id, agent_id),
                    FOREIGN KEY(scope, room_id, agent_id)
                        REFERENCES room_memberships(scope, room_id, agent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_room_member_roles_session
                    ON room_member_roles(scope, room_session_id);
                CREATE TABLE IF NOT EXISTS room_automation_policies (
                    scope TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    max_rounds INTEGER NOT NULL,
                    max_messages INTEGER NOT NULL,
                    stagnation_rounds INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, room_id),
                    FOREIGN KEY(scope, room_id) REFERENCES rooms(scope, room_id)
                );
                CREATE TABLE IF NOT EXISTS room_discussions (
                    scope TEXT NOT NULL,
                    discussion_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    starter_agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_round INTEGER NOT NULL,
                    processed_round INTEGER NOT NULL,
                    max_rounds INTEGER NOT NULL,
                    max_messages INTEGER NOT NULL,
                    stagnation_rounds INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    stagnation_count INTEGER NOT NULL,
                    last_round_digest TEXT,
                    stop_reason TEXT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    discussion_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, discussion_id),
                    FOREIGN KEY(scope, room_id) REFERENCES rooms(scope, room_id)
                );
                CREATE INDEX IF NOT EXISTS idx_room_discussions_room_status
                    ON room_discussions(scope, room_id, status, updated_utc);
                CREATE TABLE IF NOT EXISTS memories (
                    scope TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    visibility TEXT NOT NULL,
                    room_id TEXT,
                    owner_agent_id TEXT NOT NULL,
                    record_type TEXT NOT NULL DEFAULT 'FACT',
                    authority_id TEXT,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source_message_id TEXT,
                    source_message_sha256 TEXT,
                    artifact_bindings_json TEXT NOT NULL,
                    parent_memory_id TEXT,
                    supersedes_memory_id TEXT,
                    applicability_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    revoked_utc TEXT,
                    revocation_reason TEXT,
                    revocation_sha256 TEXT,
                    memory_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, memory_id),
                    FOREIGN KEY(scope, room_id) REFERENCES rooms(scope, room_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memories_visibility_room_time
                    ON memories(scope, visibility, room_id, status, created_utc);
                CREATE TABLE IF NOT EXISTS tasks (
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    workspace_root_key TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    owner TEXT,
                    status TEXT NOT NULL,
                    claimed_by TEXT,
                    claimed_session_id TEXT,
                    lease_token_sha256 TEXT,
                    lease_expires_epoch REAL,
                    claimed_utc TEXT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    task_sha256 TEXT NOT NULL,
                    task_revision INTEGER NOT NULL DEFAULT 1,
                    approval_mode TEXT NOT NULL,
                    required_peer TEXT,
                    review_quorum INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(scope, task_id)
                );
                CREATE TABLE IF NOT EXISTS task_required_peers (
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    PRIMARY KEY(scope, task_id, peer_id),
                    FOREIGN KEY(scope, task_id) REFERENCES tasks(scope, task_id)
                );
                CREATE TABLE IF NOT EXISTS task_paths (
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    access TEXT NOT NULL,
                    path_prefix TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    PRIMARY KEY(scope, task_id, access, path_prefix),
                    FOREIGN KEY(scope, task_id) REFERENCES tasks(scope, task_id)
                );
                CREATE TABLE IF NOT EXISTS work_updates (
                    update_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    artifact_paths_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    update_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_updates_scope_task_time
                    ON work_updates(scope, task_id, created_utc);
                CREATE INDEX IF NOT EXISTS idx_work_updates_scope_created
                    ON work_updates(scope, created_utc DESC, update_id);
                CREATE TABLE IF NOT EXISTS peer_calls (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    requester TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    question TEXT NOT NULL,
                    artifact_paths_json TEXT NOT NULL,
                    request_utc TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    task_revision INTEGER NOT NULL,
                    task_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_mode TEXT NOT NULL,
                    response TEXT,
                    response_artifact_paths_json TEXT,
                    response_utc TEXT,
                    response_sha256 TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_peer_calls_inbox
                    ON peer_calls(scope, recipient, status, sequence);
                CREATE INDEX IF NOT EXISTS idx_peer_calls_scope_request_time
                    ON peer_calls(scope, request_utc DESC, sequence DESC);
                CREATE TABLE IF NOT EXISTS peer_reviews (
                    review_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    findings TEXT NOT NULL,
                    artifact_paths_json TEXT NOT NULL,
                    review_utc TEXT NOT NULL,
                    review_sha256 TEXT NOT NULL,
                    task_revision INTEGER NOT NULL,
                    task_sha256 TEXT NOT NULL,
                    UNIQUE(scope, request_id, reviewer),
                    FOREIGN KEY(request_id) REFERENCES peer_calls(request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_peer_reviews_scope_review_time
                    ON peer_reviews(scope, review_utc DESC, review_id);
                CREATE TABLE IF NOT EXISTS integration_records (
                    record_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    change_summary TEXT NOT NULL,
                    changed_paths_json TEXT NOT NULL,
                    before_hashes_json TEXT NOT NULL,
                    after_hashes_json TEXT NOT NULL,
                    tests TEXT NOT NULL,
                    evidence_paths_json TEXT NOT NULL,
                    approval_mode TEXT NOT NULL,
                    review_ids_json TEXT NOT NULL,
                    recorded_utc TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_integration_records_task_time
                    ON integration_records(scope, task_id, recorded_utc);
                CREATE INDEX IF NOT EXISTS idx_integration_records_scope_recorded
                    ON integration_records(scope, recorded_utc DESC, record_id);
                CREATE TABLE IF NOT EXISTS agent_presence (
                    scope TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    client_name TEXT,
                    provider_id TEXT,
                    model_id TEXT,
                    reasoning_mode TEXT,
                    route_class TEXT,
                    last_seen_utc TEXT NOT NULL,
                    last_seen_epoch REAL NOT NULL,
                    PRIMARY KEY(scope, agent_id, session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_presence_scope_time
                    ON agent_presence(scope, last_seen_epoch);
                CREATE TABLE IF NOT EXISTS authorized_sessions (
                    scope TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_session_id TEXT NOT NULL,
                    source_conversation_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    owner_agent_id TEXT NOT NULL,
                    owner_bridge_session_id TEXT NOT NULL,
                    room_id TEXT,
                    room_session_id TEXT,
                    display_name TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    client_version TEXT,
                    requested_route TEXT,
                    observed_route TEXT,
                    observed_route_source TEXT,
                    model_id TEXT,
                    model_source TEXT,
                    role_id TEXT NOT NULL,
                    role_label TEXT,
                    state TEXT NOT NULL,
                    supports_events INTEGER NOT NULL,
                    created_utc TEXT NOT NULL,
                    started_utc TEXT NOT NULL,
                    ended_utc TEXT,
                    last_seen_utc TEXT NOT NULL,
                    last_seen_epoch REAL NOT NULL,
                    latest_sequence INTEGER NOT NULL,
                    session_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, source_type, source_session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_authorized_sessions_scope_seen
                    ON authorized_sessions(scope, last_seen_epoch DESC);
                CREATE INDEX IF NOT EXISTS idx_authorized_sessions_room_session
                    ON authorized_sessions(scope, room_session_id, last_seen_epoch DESC);
                CREATE TABLE IF NOT EXISTS authorized_session_events (
                    scope TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    adapter_event_id TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    summary TEXT,
                    state_after TEXT NOT NULL,
                    secret_redacted INTEGER NOT NULL,
                    event_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, source_type, source_session_id, sequence),
                    UNIQUE(scope, source_type, source_session_id, adapter_event_id),
                    FOREIGN KEY(scope, source_type, source_session_id)
                        REFERENCES authorized_sessions(
                            scope, source_type, source_session_id
                        )
                );
                CREATE INDEX IF NOT EXISTS idx_authorized_session_events_retained
                    ON authorized_session_events(
                        scope, source_type, source_session_id, sequence DESC
                    );
                CREATE TABLE IF NOT EXISTS agent_identity_capabilities (
                    scope TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    workspace_root_key TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    secret_file_relpath TEXT NOT NULL,
                    token_sha256 TEXT NOT NULL,
                    capability_sha256 TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    revoked_utc TEXT,
                    PRIMARY KEY(scope, capability_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_identity_capabilities_agent
                    ON agent_identity_capabilities(
                        scope, workspace_root_key, agent_id, revoked_utc, created_utc
                    );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    task_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    prev_chain_sha256 TEXT NOT NULL,
                    chain_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_mutation_receipts (
                    call_sha256 TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    arguments_sha256 TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL,
                    created_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_mcp_mutation_receipts_scope_actor
                    ON mcp_mutation_receipts(scope, actor, created_utc DESC);
                CREATE TABLE IF NOT EXISTS governance_operations (
                    scope TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    permission_decision_id TEXT,
                    bound_discussion_id TEXT,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    not_before_epoch REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token_sha256 TEXT,
                    lease_expires_epoch REAL,
                    attempt_deadline_epoch REAL,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0,
                    terminal_outcome TEXT,
                    terminal_detail TEXT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    operation_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, operation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_governance_operations_due
                    ON governance_operations(
                        scope, status, not_before_epoch, created_utc, operation_id
                    );
                CREATE INDEX IF NOT EXISTS idx_governance_operations_resource
                    ON governance_operations(
                        scope, resource_key, status, lease_expires_epoch
                    );
                CREATE TABLE IF NOT EXISTS workflow_schedules (
                    scope TEXT NOT NULL,
                    schedule_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    permission_decision_id TEXT,
                    interval_seconds INTEGER NOT NULL,
                    next_run_epoch REAL NOT NULL,
                    enabled INTEGER NOT NULL,
                    last_materialized_epoch REAL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    schedule_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, schedule_id)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_schedules_due
                    ON workflow_schedules(scope, enabled, next_run_epoch);
                CREATE TABLE IF NOT EXISTS capability_registry (
                    scope TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    registry_version TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    registered_by TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    capability_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, capability_id, registry_version)
                );
                CREATE TABLE IF NOT EXISTS capability_grants (
                    scope TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    principal_type TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    registry_version TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    decided_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    revoked_utc TEXT,
                    grant_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, grant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_capability_grants_principal
                    ON capability_grants(
                        scope, principal_type, principal_id, capability_id,
                        created_utc DESC
                    );
                CREATE TABLE IF NOT EXISTS permission_decisions (
                    scope TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    decided_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    expires_epoch REAL NOT NULL,
                    consumed_utc TEXT,
                    created_utc TEXT NOT NULL,
                    decision_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, decision_id)
                );
                CREATE INDEX IF NOT EXISTS idx_permission_decisions_lookup
                    ON permission_decisions(
                        scope, task_id, agent_id, action, resource_key,
                        created_utc DESC
                    );
                CREATE TABLE IF NOT EXISTS execution_bindings (
                    scope TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    permission_decision_id TEXT NOT NULL,
                    repository_root TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    base_commit_id TEXT NOT NULL,
                    base_diff_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    final_commit_id TEXT,
                    final_diff_sha256 TEXT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, binding_id)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_bindings_task
                    ON execution_bindings(scope, task_id, created_utc DESC);
                CREATE TABLE IF NOT EXISTS task_briefings (
                    scope TEXT NOT NULL,
                    briefing_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    room_id TEXT,
                    applicability_json TEXT NOT NULL,
                    memory_bindings_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    briefing_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, briefing_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_briefings_task
                    ON task_briefings(scope, task_id, created_utc DESC);
                CREATE TABLE IF NOT EXISTS decision_conflict_findings (
                    scope TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    briefing_id TEXT NOT NULL,
                    memory_ids_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    finding_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, finding_id)
                );
                CREATE INDEX IF NOT EXISTS idx_decision_conflict_findings_task
                    ON decision_conflict_findings(scope, task_id, created_utc DESC);
                CREATE TABLE IF NOT EXISTS trust_records (
                    scope TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    source_bindings_json TEXT NOT NULL,
                    related_record_ids_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    trust_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scope, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_trust_records_task
                    ON trust_records(scope, task_id, created_utc, record_id);
                CREATE INDEX IF NOT EXISTS idx_events_scope_time
                    ON events(scope, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_scope_created
                    ON events(scope, created_utc DESC, sequence DESC);
                """
            )
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row and row["value"] not in {
                "1",
                "2",
                "3",
                "4",
                "5",
                "6",
                "7",
                "8",
                "9",
                "10",
                "11",
                "12",
                "13",
                "14",
                "15",
                "16",
                "17",
                "18",
                "19",
                "20",
                "21",
                "22",
                "23",
                "24",
                "25",
                "26",
                SCHEMA_VERSION,
            }:
                raise BridgeError(
                    f"unsupported database schema {row['value']}; expected {SCHEMA_VERSION}"
                )
            storage_columns = {
                item["name"]
                for item in connection.execute(
                    "PRAGMA table_info(scope_storage_usage)"
                )
            }
            for column in ("event_rows", "event_bytes"):
                if column not in storage_columns:
                    connection.execute(
                        f"ALTER TABLE scope_storage_usage "
                        f"ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
            governance_columns = {
                item["name"]
                for item in connection.execute(
                    "PRAGMA table_info(governance_operations)"
                )
            }
            missing_governance_columns = {
                key
                for key in ("attempt_deadline_epoch", "bound_discussion_id")
                if key not in governance_columns
            }
            if missing_governance_columns:
                legacy_fields = tuple(
                    key
                    for key in GOVERNANCE_OPERATION_PAYLOAD_FIELDS
                    if key in governance_columns
                )
                legacy_operations = connection.execute(
                    "SELECT * FROM governance_operations"
                ).fetchall()
                for operation in legacy_operations:
                    legacy_payload = {key: operation[key] for key in legacy_fields}
                    if stable_sha256(legacy_payload) != operation["operation_sha256"]:
                        raise BridgeError(
                            "legacy governance operation SHA-256 does not match its state"
                        )
                if "attempt_deadline_epoch" in missing_governance_columns:
                    connection.execute(
                        "ALTER TABLE governance_operations "
                        "ADD COLUMN attempt_deadline_epoch REAL"
                    )
                if "bound_discussion_id" in missing_governance_columns:
                    connection.execute(
                        "ALTER TABLE governance_operations "
                        "ADD COLUMN bound_discussion_id TEXT"
                    )
                for operation in connection.execute(
                    "SELECT * FROM governance_operations"
                ).fetchall():
                    connection.execute(
                        "UPDATE governance_operations SET operation_sha256=? "
                        "WHERE scope=? AND operation_id=?",
                        (
                            stable_sha256(governance_operation_payload(operation)),
                            operation["scope"],
                            operation["operation_id"],
                        ),
                    )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_governance_operations_discussion "
                "ON governance_operations(scope, bound_discussion_id) "
                "WHERE bound_discussion_id IS NOT NULL"
            )
            task_columns = {
                item["name"] for item in connection.execute("PRAGMA table_info(tasks)")
            }
            if "workspace_root_key" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN workspace_root_key TEXT"
                )
                connection.execute(
                    "UPDATE tasks SET workspace_root_key=? WHERE workspace_root_key IS NULL",
                    (self.workspace_root_key,),
                )
            if "review_quorum" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN review_quorum INTEGER NOT NULL DEFAULT 1"
                )
            if "task_revision" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN task_revision INTEGER NOT NULL DEFAULT 1"
                )
            peer_call_columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(peer_calls)")
            }
            if "task_revision" not in peer_call_columns:
                connection.execute(
                    "ALTER TABLE peer_calls ADD COLUMN task_revision INTEGER NOT NULL DEFAULT 0"
                )
            if "task_sha256" not in peer_call_columns:
                connection.execute(
                    "ALTER TABLE peer_calls ADD COLUMN task_sha256 TEXT NOT NULL DEFAULT ''"
                )
            peer_review_columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(peer_reviews)")
            }
            if "task_revision" not in peer_review_columns:
                connection.execute(
                    "ALTER TABLE peer_reviews ADD COLUMN task_revision INTEGER NOT NULL DEFAULT 0"
                )
            if "task_sha256" not in peer_review_columns:
                connection.execute(
                    "ALTER TABLE peer_reviews ADD COLUMN task_sha256 TEXT NOT NULL DEFAULT ''"
                )
            presence_columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(agent_presence)")
            }
            for column in (
                "client_name",
                "provider_id",
                "model_id",
                "reasoning_mode",
                "route_class",
            ):
                if column not in presence_columns:
                    connection.execute(
                        f"ALTER TABLE agent_presence ADD COLUMN {column} TEXT"
                    )
            message_columns = {
                item["name"] for item in connection.execute("PRAGMA table_info(messages)")
            }
            for column in (
                "room_id",
                "route_profile_id",
                "route_profile_sha256",
                "requested_provider_id",
                "requested_model_id",
                "requested_reasoning_mode",
                "requested_route_class",
                "route_request_sha256",
                "discussion_id",
                "discussion_round",
                "discussion_role",
                "visibility",
            ):
                if column not in message_columns:
                    if column == "room_id":
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN room_id TEXT NOT NULL DEFAULT 'lobby'"
                        )
                    elif column == "discussion_round":
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN discussion_round INTEGER"
                        )
                    elif column == "visibility":
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN visibility TEXT NOT NULL DEFAULT 'direct'"
                        )
                    else:
                        connection.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
            route_receipt_columns = {
                item["name"]
                for item in connection.execute(
                    "PRAGMA table_info(message_route_receipts)"
                )
            }
            if "observed_route_class" not in route_receipt_columns:
                connection.execute(
                    "ALTER TABLE message_route_receipts "
                    "ADD COLUMN observed_route_class TEXT"
                )
            provider_columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(provider_connections)")
            }
            for column in (
                "provider_id",
                "descriptor_schema",
                "credential_version_sha256",
            ):
                if column not in provider_columns:
                    connection.execute(
                        f"ALTER TABLE provider_connections ADD COLUMN {column} TEXT"
                    )
            route_profile_columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(route_profiles)")
            }
            if "response_model_id" not in route_profile_columns:
                connection.execute(
                    "ALTER TABLE route_profiles ADD COLUMN response_model_id TEXT"
                )
            if "inference_timeout_seconds" not in route_profile_columns:
                connection.execute(
                    "ALTER TABLE route_profiles "
                    "ADD COLUMN inference_timeout_seconds INTEGER"
                )
            usage_columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(inference_usage)")
            }
            for token_field in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cached_input_tokens",
                "reasoning_tokens",
            ):
                coverage_column = f"{token_field}_reported_calls"
                if coverage_column not in usage_columns:
                    connection.execute(
                        f"ALTER TABLE inference_usage ADD COLUMN {coverage_column} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                    connection.execute(
                        f"UPDATE inference_usage SET {coverage_column}=reported_calls "
                        f"WHERE {token_field} IS NOT NULL"
                    )
            discussion_columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(room_discussions)")
            }
            if "processed_round" not in discussion_columns:
                connection.execute(
                    "ALTER TABLE room_discussions "
                    "ADD COLUMN processed_round INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    """UPDATE room_discussions
                          SET processed_round = CASE
                              WHEN status IN ('completed', 'stopped', 'waiting_human')
                                  THEN current_round
                              WHEN current_round > 0 THEN current_round - 1
                              ELSE 0
                          END"""
                )
                for migrated_discussion in connection.execute(
                    "SELECT * FROM room_discussions"
                ).fetchall():
                    connection.execute(
                        """UPDATE room_discussions SET discussion_sha256=?
                            WHERE scope=? AND discussion_id=?""",
                        (
                            stable_sha256(
                                self._discussion_row_payload(migrated_discussion)
                            ),
                            migrated_discussion["scope"],
                            migrated_discussion["discussion_id"],
                        ),
                    )
            memory_columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(memories)")
            }
            for column, declaration in (
                ("record_type", "TEXT NOT NULL DEFAULT 'FACT'"),
                ("authority_id", "TEXT"),
                ("supersedes_memory_id", "TEXT"),
                ("applicability_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if column not in memory_columns:
                    connection.execute(
                        f"ALTER TABLE memories ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_supersession "
                "ON memories(scope, supersedes_memory_id, created_utc)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_room_inbox "
                "ON messages(scope, room_id, recipient, sequence)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_discussion_round "
                "ON messages(scope, discussion_id, discussion_round, discussion_role, sequence)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_room_memberships_agent "
                "ON room_memberships(scope, agent_id, status, room_id)"
            )
            for membership in connection.execute(
                "SELECT * FROM room_memberships"
            ).fetchall():
                existing_role = connection.execute(
                    """SELECT * FROM room_member_roles
                       WHERE scope=? AND room_id=? AND agent_id=?""",
                    (
                        membership["scope"],
                        membership["room_id"],
                        membership["agent_id"],
                    ),
                ).fetchone()
                role_id = (
                    str(existing_role["role_id"])
                    if existing_role is not None
                    else DEFAULT_ROOM_ROLE
                )
                role_label = (
                    existing_role["role_label"]
                    if existing_role is not None
                    else None
                )
                created_utc = (
                    str(existing_role["created_utc"])
                    if existing_role is not None
                    else str(membership["joined_utc"])
                )
                updated_by = (
                    str(existing_role["updated_by"])
                    if existing_role is not None
                    else "peerbridge-schema-migration"
                )
                role_payload = self._room_role_payload(
                    scope=str(membership["scope"]),
                    room_id=str(membership["room_id"]),
                    agent_id=str(membership["agent_id"]),
                    room_session_id=str(membership["room_session_id"]),
                    role_id=role_id,
                    role_label=role_label,
                    updated_by=updated_by,
                )
                connection.execute(
                    """INSERT INTO room_member_roles(
                           scope, room_id, agent_id, room_session_id, role_id,
                           role_label, updated_by, created_utc, updated_utc,
                           role_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(scope, room_id, agent_id) DO UPDATE SET
                           room_session_id=excluded.room_session_id,
                           role_id=excluded.role_id,
                           role_label=excluded.role_label,
                           updated_by=excluded.updated_by,
                           updated_utc=excluded.updated_utc,
                           role_sha256=excluded.role_sha256""",
                    (
                        membership["scope"],
                        membership["room_id"],
                        membership["agent_id"],
                        membership["room_session_id"],
                        role_id,
                        role_label,
                        updated_by,
                        created_utc,
                        membership["updated_utc"],
                        stable_sha256(role_payload),
                    ),
                )
            for existing_room in connection.execute(
                "SELECT scope, room_id, created_utc FROM rooms"
            ).fetchall():
                default_mode = "once"
                created_utc = str(existing_room["created_utc"])
                policy_payload = {
                    "scope": str(existing_room["scope"]),
                    "room_id": str(existing_room["room_id"]),
                    "mode": default_mode,
                    "max_rounds": DEFAULT_DISCUSSION_MAX_ROUNDS,
                    "max_messages": DEFAULT_DISCUSSION_MAX_MESSAGES,
                    "stagnation_rounds": DEFAULT_DISCUSSION_STAGNATION_ROUNDS,
                    "updated_by": "peerbridge-system",
                }
                connection.execute(
                    """INSERT OR IGNORE INTO room_automation_policies(
                           scope, room_id, mode, max_rounds, max_messages,
                           stagnation_rounds, updated_by, created_utc, updated_utc,
                           policy_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, 'peerbridge-system', ?, ?, ?)""",
                    (
                        existing_room["scope"],
                        existing_room["room_id"],
                        default_mode,
                        DEFAULT_DISCUSSION_MAX_ROUNDS,
                        DEFAULT_DISCUSSION_MAX_MESSAGES,
                        DEFAULT_DISCUSSION_STAGNATION_ROUNDS,
                        created_utc,
                        created_utc,
                        stable_sha256(policy_payload),
                    ),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_scope_updated "
                "ON tasks(scope, updated_utc DESC, task_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_workspace_lease "
                "ON tasks(workspace_root_key, status, lease_expires_epoch)"
            )
            connection.execute(
                """INSERT OR IGNORE INTO consumer_cursors(
                    scope, channel, consumer, position, updated_utc
                ) SELECT scope, 'messages:lobby', consumer, position, updated_utc
                  FROM consumer_cursors
                 WHERE scope=? AND channel='messages'""",
                (self.scope,),
            )
            lobby_created = "1970-01-01T00:00:00Z"
            lobby_payload = {
                "scope": self.scope,
                "room_id": DEFAULT_ROOM_ID,
                "name": "Lobby",
                "created_by": "peerbridge-system",
                "archived": False,
            }
            connection.execute(
                """INSERT OR IGNORE INTO rooms(
                    scope, room_id, name, created_by, archived, created_utc,
                    updated_utc, room_sha256
                ) VALUES (?, ?, 'Lobby', 'peerbridge-system', 0, ?, ?, ?)""",
                (
                    self.scope,
                    DEFAULT_ROOM_ID,
                    lobby_created,
                    lobby_created,
                    stable_sha256(lobby_payload),
                ),
            )
            lobby_policy = {
                "scope": self.scope,
                "room_id": DEFAULT_ROOM_ID,
                "mode": "once",
                "max_rounds": DEFAULT_DISCUSSION_MAX_ROUNDS,
                "max_messages": DEFAULT_DISCUSSION_MAX_MESSAGES,
                "stagnation_rounds": DEFAULT_DISCUSSION_STAGNATION_ROUNDS,
                "updated_by": "peerbridge-system",
            }
            connection.execute(
                """INSERT OR IGNORE INTO room_automation_policies(
                       scope, room_id, mode, max_rounds, max_messages,
                       stagnation_rounds, updated_by, created_utc, updated_utc,
                       policy_sha256
                   ) VALUES (?, ?, 'once', ?, ?, ?, 'peerbridge-system', ?, ?, ?)""",
                (
                    self.scope,
                    DEFAULT_ROOM_ID,
                    DEFAULT_DISCUSSION_MAX_ROUNDS,
                    DEFAULT_DISCUSSION_MAX_MESSAGES,
                    DEFAULT_DISCUSSION_STAGNATION_ROUNDS,
                    lobby_created,
                    lobby_created,
                    stable_sha256(lobby_policy),
                ),
            )
            # Upgrade only the original system default. A user-selected OFF
            # policy remains untouched.
            connection.execute(
                """UPDATE room_automation_policies
                      SET mode='once', policy_sha256=?
                    WHERE scope=? AND room_id=? AND mode='off'
                      AND updated_by='peerbridge-system'""",
                (
                    stable_sha256(lobby_policy),
                    self.scope,
                    DEFAULT_ROOM_ID,
                ),
            )
            connection.execute(
                """INSERT OR IGNORE INTO task_required_peers(scope, task_id, peer_id)
                   SELECT scope, task_id, required_peer FROM tasks
                   WHERE required_peer IS NOT NULL AND required_peer != ''"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO scope_storage_usage(
                       scope, message_rows, message_bytes, event_rows, event_bytes,
                       updated_utc
                    )
                    SELECT scope, COUNT(*),
                           COALESCE(SUM(length(CAST(body AS BLOB))), 0), 0, 0, ?
                      FROM messages GROUP BY scope""",
                (utc_now(),),
            )
            connection.execute(
                """UPDATE scope_storage_usage
                      SET message_rows=(
                              SELECT COUNT(*) FROM messages m
                               WHERE m.scope=scope_storage_usage.scope
                          ),
                          message_bytes=(
                              SELECT COALESCE(SUM(
                                  length(CAST(body AS BLOB))
                                  + length(CAST(subject AS BLOB))
                                  + length(CAST(artifact_paths_json AS BLOB))
                                  + length(CAST(COALESCE(route_request_sha256, '') AS BLOB))
                                  + length(CAST(COALESCE(reply_to, '') AS BLOB))
                              ), 0)
                                FROM messages m
                               WHERE m.scope=scope_storage_usage.scope
                          ),
                          updated_utc=?""",
                (utc_now(),),
            )
            connection.execute(
                """INSERT OR IGNORE INTO scope_storage_usage(
                       scope, message_rows, message_bytes, event_rows, event_bytes,
                       updated_utc
                   )
                   SELECT DISTINCT scope, 0, 0, 0, 0, ? FROM events""",
                (utc_now(),),
            )
            connection.execute(
                """UPDATE scope_storage_usage
                      SET event_rows=(
                              SELECT COUNT(*) FROM events e
                               WHERE e.scope=scope_storage_usage.scope
                          ),
                          event_bytes=(
                              SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0)
                                FROM events e
                               WHERE e.scope=scope_storage_usage.scope
                          ),
                          updated_utc=?""",
                (utc_now(),),
            )
            connection.execute(
                """INSERT INTO metadata(key, value) VALUES ('schema_version', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (SCHEMA_VERSION,),
            )

    def _reserve_event_storage(
        self, connection: sqlite3.Connection, payload_bytes: int
    ) -> None:
        payload_bytes = int(payload_bytes)
        if payload_bytes < 0:
            raise BridgeError("event storage reservation is invalid")
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO scope_storage_usage(
                   scope, message_rows, message_bytes, event_rows, event_bytes,
                   updated_utc
               ) VALUES (?, 0, 0, 0, 0, ?)""",
            (self.scope, now),
        )
        updated = connection.execute(
            """UPDATE scope_storage_usage
                  SET event_rows=event_rows+1,
                      event_bytes=event_bytes+?,
                      updated_utc=?
                WHERE scope=?
                  AND event_rows+1<=?
                  AND event_bytes+?<=?""",
            (
                payload_bytes,
                now,
                self.scope,
                MAX_SCOPE_EVENT_ROWS,
                payload_bytes,
                MAX_SCOPE_EVENT_BYTES,
            ),
        )
        if updated.rowcount != 1:
            raise BridgeError("durable event storage quota exceeded")

    def _event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        payload: dict[str, Any],
        task_id: str | None = None,
    ) -> dict[str, str]:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT chain_sha256 FROM events WHERE scope=? ORDER BY sequence DESC LIMIT 1",
            (self.scope,),
        ).fetchone()
        prev_sha = previous["chain_sha256"] if previous else ZERO_SHA256
        event_id = uuid.uuid4().hex
        created = utc_now()
        full_payload = {
            **payload,
            "session_id": self.session_id,
            "runtime_identity": {
                "client_name": self.client_name,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "reasoning_mode": self.reasoning_mode,
                "route_class": self.route_class,
            },
        }
        payload_json = json.dumps(
            full_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self._reserve_event_storage(
            connection, len(payload_json.encode("utf-8"))
        )
        payload_sha = sha256_bytes(payload_json.encode("utf-8"))
        envelope = {
            "event_id": event_id,
            "scope": self.scope,
            "actor": self.agent_id,
            "event_type": event_type,
            "task_id": task_id,
            "payload_sha256": payload_sha,
            "created_utc": created,
            "prev_chain_sha256": prev_sha,
        }
        chain_sha = stable_sha256(envelope)
        connection.execute(
            """INSERT INTO events(
                event_id, scope, actor, event_type, task_id, payload_json,
                created_utc, payload_sha256, prev_chain_sha256, chain_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                self.scope,
                self.agent_id,
                event_type,
                task_id,
                payload_json,
                created,
                payload_sha,
                prev_sha,
                chain_sha,
            ),
        )
        return {"event_id": event_id, "chain_sha256": chain_sha}

    def _mcp_receipt_metadata(
        self, args: dict[str, Any], expected_tool: str
    ) -> dict[str, str] | None:
        raw = args.get("__mcp_receipt")
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) != {
            "call_sha256",
            "arguments_sha256",
            "session_id",
            "tool",
        }:
            raise BridgeError("invalid internal MCP mutation receipt metadata")
        tool = _require_identifier(raw.get("tool"), "receipt tool")
        session_id = _require_identifier(raw.get("session_id"), "receipt session_id")
        if tool != expected_tool or session_id != self.session_id:
            raise BridgeError("MCP mutation receipt identity mismatch")
        return {
            "call_sha256": _require_sha256(
                raw.get("call_sha256"), "receipt call_sha256"
            ),
            "arguments_sha256": _require_sha256(
                raw.get("arguments_sha256"), "receipt arguments_sha256"
            ),
            "session_id": session_id,
            "tool": tool,
        }

    def _load_mcp_mutation_receipt_locked(
        self,
        connection: sqlite3.Connection,
        metadata: dict[str, str] | None,
    ) -> dict[str, Any] | None:
        if metadata is None:
            return None
        row = connection.execute(
            """SELECT actor, tool, arguments_sha256, result_json, result_sha256
                 FROM mcp_mutation_receipts
                WHERE call_sha256=? AND scope=?""",
            (metadata["call_sha256"], self.scope),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row["actor"]) != self.agent_id
            or str(row["tool"]) != metadata["tool"]
            or str(row["arguments_sha256"]) != metadata["arguments_sha256"]
        ):
            raise BridgeError("MCP idempotency key was reused with different arguments")
        try:
            result = json.loads(str(row["result_json"]))
        except json.JSONDecodeError as exc:
            raise sqlite3.DatabaseError("invalid MCP mutation receipt JSON") from exc
        if not isinstance(result, dict) or stable_sha256(result) != row["result_sha256"]:
            raise sqlite3.DatabaseError("MCP mutation receipt integrity check failed")
        return result

    def _store_mcp_mutation_receipt_locked(
        self,
        connection: sqlite3.Connection,
        metadata: dict[str, str] | None,
        result: dict[str, Any],
    ) -> None:
        if metadata is None:
            return
        result_json = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection.execute(
            """INSERT INTO mcp_mutation_receipts(
                   call_sha256, scope, actor, session_id, tool,
                   arguments_sha256, result_json, result_sha256
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                metadata["call_sha256"],
                self.scope,
                self.agent_id,
                metadata["session_id"],
                metadata["tool"],
                metadata["arguments_sha256"],
                result_json,
                stable_sha256(result),
            ),
        )

    def _normalize_path(self, value: Any) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if not text:
            raise BridgeError("path must be a non-empty project-relative path")
        candidate = Path(text)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise BridgeError("path must remain inside the project root")
        resolved = (self.root / candidate).resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise BridgeError("path escapes the project root") from exc
        normalized = relative.as_posix()
        return "." if normalized in {"", "."} else normalized.rstrip("/")

    @staticmethod
    def _path_collision_key(normalized: str) -> str:
        """Return the filesystem collision identity used by task leases."""
        return normalized.casefold() if os.name == "nt" else normalized

    def _resolve_path(self, value: Any, *, must_exist: bool = False) -> Path:
        normalized = self._normalize_path(value)
        resolved = self.root if normalized == "." else self.root / normalized
        if must_exist and not resolved.is_file():
            raise BridgeError(f"artifact does not exist as a file: {normalized}")
        return resolved

    @staticmethod
    def _path_overlaps(left: str, right: str) -> bool:
        left = Bridge._path_collision_key(left)
        right = Bridge._path_collision_key(right)
        if left == "." or right == ".":
            return True
        return (
            left == right
            or left.startswith(right + "/")
            or right.startswith(left + "/")
        )

    @staticmethod
    def _path_within(path: str, prefix: str) -> bool:
        path = Bridge._path_collision_key(path)
        prefix = Bridge._path_collision_key(prefix)
        return prefix == "." or path == prefix or path.startswith(prefix + "/")

    @staticmethod
    def _hash_file_streaming(
        path: Path,
        *,
        prefix_bytes: int = 0,
        max_bytes: int | None = None,
        max_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Hash a stable file without allocating its full contents."""
        before = path.stat()
        if max_bytes is not None and before.st_size > int(max_bytes):
            raise BridgeError("artifact exceeds the interactive hash byte budget")
        hasher = hashlib.sha256()
        prefix = bytearray()
        total = 0
        deadline = (
            time.monotonic() + float(max_seconds)
            if max_seconds is not None
            else None
        )
        with path.open("rb") as handle:
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    raise BridgeError("artifact hashing exceeded the interactive time budget")
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                total += len(chunk)
                if len(prefix) < prefix_bytes:
                    prefix.extend(chunk[: prefix_bytes - len(prefix)])
        after = path.stat()
        identity_before = (before.st_size, before.st_mtime_ns)
        identity_after = (after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or total != after.st_size:
            raise BridgeError("artifact changed while it was being hashed")
        return {
            "bytes": total,
            "sha256": hasher.hexdigest(),
            "prefix": bytes(prefix),
            "identity": identity_after,
        }

    def _hash_proof_files(self, paths: Iterable[str]) -> dict[str, dict[str, Any]]:
        ordered = list(dict.fromkeys(paths))
        if not ordered or len(ordered) > MAX_PROOF_FILES:
            raise BridgeError("proof file count exceeds the bounded proof budget")
        deadline = time.monotonic() + MAX_PROOF_HASH_SECONDS
        total_bytes = 0
        result: dict[str, dict[str, Any]] = {}
        for path in ordered:
            resolved = self._resolve_path(path, must_exist=True)
            try:
                size = int(resolved.stat().st_size)
            except OSError as exc:
                raise BridgeError(f"proof artifact cannot be inspected: {path}") from exc
            total_bytes += size
            if total_bytes > MAX_PROOF_TOTAL_BYTES:
                raise BridgeError("proof artifacts exceed the cumulative byte budget")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError("proof hashing exceeded the cumulative time budget")
            result[path] = self._hash_file_streaming(
                resolved,
                max_bytes=MAX_MCP_HASH_BYTES,
                max_seconds=remaining,
            )
        return result

    def _is_protected(self, normalized: str) -> bool:
        path = Path(normalized)
        if _path_parts_are_sensitive(path):
            return True
        return any(self._path_overlaps(normalized, item) for item in self.protected_paths)

    def _is_within_protected(self, normalized: str) -> bool:
        path = Path(normalized)
        if _path_parts_are_sensitive(path):
            return True
        return any(
            self._path_within(normalized, item) for item in self.protected_paths
        )

    def _clean_artifacts(
        self,
        values: Any,
        *,
        max_count: int | None = None,
        reject_duplicates: bool = False,
    ) -> list[str]:
        raw_values = _json_list(values, "artifact_paths")
        if max_count is not None and len(raw_values) > int(max_count):
            raise BridgeError("artifact_paths exceeds the attachment count limit")
        clean: list[str] = []
        seen: set[str] = set()
        for value in raw_values:
            normalized = self._normalize_path(value)
            if reject_duplicates and normalized in seen:
                raise BridgeError("artifact_paths contains duplicate normalized paths")
            if normalized in seen:
                continue
            seen.add(normalized)
            staged_chat_attachment = normalized.startswith(CHAT_ATTACHMENT_ROOT + "/")
            relative = Path(normalized)
            release_gate_manifest = bool(
                len(relative.parts) == 4
                and Path(*relative.parts[:2]).as_posix()
                == RELEASE_GATE_ARTIFACT_ROOT
                and re.fullmatch(r"[0-9a-f]{64}", relative.parts[2])
                and relative.parts[3] == "source.json"
            )
            if (
                self._is_protected(normalized)
                and not staged_chat_attachment
                and not release_gate_manifest
            ):
                raise BridgeError(f"protected or sensitive artifact is not exposed: {normalized}")
            resolved = self._resolve_path(normalized, must_exist=True)
            if staged_chat_attachment:
                digest = relative.stem.lower()
                valid_shape = (
                    relative.parent.as_posix() == CHAT_ATTACHMENT_ROOT
                    and relative.suffix.lower() in CHAT_ATTACHMENT_SUFFIXES
                    and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
                    and resolved.stat().st_size <= MAX_CHAT_ATTACHMENT_BYTES
                )
                if not valid_shape or sha256_bytes(resolved.read_bytes()) != digest:
                    raise BridgeError(
                        "staged chat attachment is not a valid content-addressed file"
                    )
            if release_gate_manifest and resolved.stat().st_size > 64 * 1024:
                raise BridgeError("release gate manifest exceeds the bounded size")
            clean.append(resolved.relative_to(self.root).as_posix())
        return clean

    def _clean_task_paths(self, values: Any, access: str) -> list[str]:
        raw_values = _json_list(values, f"{access}_paths")
        if len(raw_values) > MAX_TASK_PATHS:
            raise BridgeError(f"{access}_paths exceeds the task path limit")
        clean: list[str] = []
        for value in raw_values:
            normalized = self._normalize_path(value)
            if access == "write" and self._is_protected(normalized):
                raise BridgeError(f"write scope is protected or sensitive: {normalized}")
            clean.append(normalized)
        return sorted(set(clean))

    def touch_presence(self, transport: str = "stdio") -> None:
        now_utc = utc_now()
        now_epoch = time.time()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_presence(
                    scope, agent_id, session_id, transport, client_name, provider_id,
                    model_id, reasoning_mode, route_class, last_seen_utc, last_seen_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, agent_id, session_id) DO UPDATE SET
                    transport=excluded.transport,
                    client_name=excluded.client_name,
                    provider_id=excluded.provider_id,
                    model_id=excluded.model_id,
                    reasoning_mode=excluded.reasoning_mode,
                    route_class=excluded.route_class,
                    last_seen_utc=excluded.last_seen_utc,
                    last_seen_epoch=excluded.last_seen_epoch""",
                (
                    self.scope,
                    self.agent_id,
                    self.session_id,
                    transport,
                    self.client_name,
                    self.provider_id,
                    self.model_id,
                    self.reasoning_mode,
                    self.route_class,
                    now_utc,
                    now_epoch,
                ),
            )

    def clear_presence(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM agent_presence WHERE scope=? AND agent_id=? AND session_id=?",
                (self.scope, self.agent_id, self.session_id),
            )

    def presence_snapshot(self) -> dict[str, Any]:
        cutoff = time.time() - self.presence_ttl_seconds
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT agent_id, session_id, transport, client_name, provider_id,
                          model_id, reasoning_mode, route_class, last_seen_utc
                   FROM agent_presence
                   WHERE scope=? AND last_seen_epoch>=?
                   ORDER BY agent_id, last_seen_epoch DESC""",
                (self.scope, cutoff),
            ).fetchall()
        sessions = [dict(row) for row in rows]
        return {
            "online_agents": sorted({row["agent_id"] for row in rows}),
            "online_sessions": sessions,
            "presence_ttl_seconds": self.presence_ttl_seconds,
            "observed_utc": utc_now(),
        }

    def _expire_leases(self, connection: sqlite3.Connection) -> None:
        now_epoch = time.time()
        rows = connection.execute(
            """SELECT task_id, claimed_by FROM tasks
               WHERE scope=? AND status='claimed' AND lease_expires_epoch<=?""",
            (self.scope, now_epoch),
        ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE tasks SET status='open', claimed_by=NULL,
                   claimed_session_id=NULL, lease_token_sha256=NULL,
                   lease_expires_epoch=NULL, claimed_utc=NULL, updated_utc=?
                   WHERE scope=? AND task_id=?""",
                (utc_now(), self.scope, row["task_id"]),
            )
            self._event(
                connection,
                "task.lease_expired",
                {"previous_holder": row["claimed_by"]},
                row["task_id"],
            )

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        lease_token: Any,
    ) -> sqlite3.Row:
        token = str(lease_token or "")
        if not token:
            raise BridgeError("lease_token is required")
        row = connection.execute(
            "SELECT * FROM tasks WHERE scope=? AND task_id=?",
            (self.scope, task_id),
        ).fetchone()
        if row is None:
            raise BridgeError("task not found")
        if row["status"] != "claimed" or row["lease_expires_epoch"] is None:
            raise BridgeError("task has no active lease")
        if row["lease_expires_epoch"] <= time.time():
            raise BridgeError("task lease has expired")
        if row["claimed_by"] != self.agent_id:
            raise BridgeError(f"task is leased by {row['claimed_by']}")
        if row["claimed_session_id"] != self.session_id:
            raise BridgeError("task lease belongs to another agent session")
        if not secrets.compare_digest(
            row["lease_token_sha256"], sha256_bytes(token.encode("utf-8"))
        ):
            raise BridgeError("lease token does not match")
        return row

    def status(self, _args: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks WHERE scope=? GROUP BY status",
                    (self.scope,),
                ).fetchall()
            }
            message_count = connection.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE scope=?", (self.scope,)
            ).fetchone()["n"]
            event_count = connection.execute(
                "SELECT COUNT(*) AS n FROM events WHERE scope=?", (self.scope,)
            ).fetchone()["n"]
            dispatch_counts = {
                row["status"]: row["n"]
                for row in connection.execute(
                    """SELECT status, COUNT(*) AS n FROM message_dispatches
                       WHERE scope=? GROUP BY status""",
                    (self.scope,),
                ).fetchall()
            }
        return {
            "scope": self.scope,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "runtime_identity": {
                "client_name": self.client_name,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "reasoning_mode": self.reasoning_mode,
                "route_class": self.route_class,
            },
            "transport": "stdio",
            "network_listener": False,
            "database": str(self.db_path),
            "message_count": message_count,
            "message_dispatch_counts": dispatch_counts,
            "task_counts": counts,
            "audit_event_count": event_count,
            "schema_version": SCHEMA_VERSION,
            "presence": self.presence_snapshot(),
        }

    def upsert_route_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        route_id = _require_identifier(args.get("route_id"), "route_id")
        agent_id = _require_identifier(args.get("agent_id"), "agent_id")
        if agent_id in RESERVED_INTERNAL_AGENT_IDS and self.agent_id != agent_id:
            raise BridgeError("reserved internal Agent cannot be configured as a route")
        if self.agent_id not in {HUMAN_OPERATOR_ID, agent_id}:
            raise BridgeError(
                "only the human operator or route owner can register a route profile"
            )
        client_name = _optional_identifier(args.get("client_name"), "client_name")
        provider_id = _optional_identifier(args.get("provider_id"), "provider_id")
        model_id = _optional_model_identifier(args.get("model_id"), "model_id")
        response_model_id = _optional_model_identifier(
            args.get("response_model_id"), "response_model_id"
        )
        inference_timeout_seconds = _optional_inference_timeout_seconds(
            args.get("inference_timeout_seconds")
        )
        reasoning_mode = _optional_identifier(
            args.get("reasoning_mode"), "reasoning_mode"
        )
        route_class = str(args.get("route_class") or "local").strip().lower()
        if route_class not in ROUTE_CLASSES:
            raise BridgeError("route_class must be official, relay or local")
        if not any((provider_id, model_id, reasoning_mode)):
            raise BridgeError(
                "a route profile requires provider_id, model_id or reasoning_mode"
            )
        enabled = bool(args.get("enabled", True))
        now = utc_now()
        profile = {
            "scope": self.scope,
            "route_id": route_id,
            "agent_id": agent_id,
            "client_name": client_name,
            "provider_id": provider_id,
            "model_id": model_id,
            "response_model_id": response_model_id,
            "inference_timeout_seconds": inference_timeout_seconds,
            "reasoning_mode": reasoning_mode,
            "route_class": route_class,
            "enabled": enabled,
        }
        profile_sha = stable_sha256(profile)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM route_profiles
                   WHERE scope=? AND route_id=?""",
                (self.scope, route_id),
            ).fetchone()
            if existing is not None:
                existing_identity = self._route_profile_identity_payload(existing)
                existing_profile_sha = self._verified_route_profile_sha256(existing)
                if existing_identity != profile:
                    raise BridgeError(
                        "route profiles are immutable; create a new route_id"
                    )
                return {
                    **profile,
                    "profile_sha256": existing_profile_sha,
                    "updated_utc": existing["updated_utc"],
                }
            connection.execute(
                """INSERT INTO route_profiles(
                    scope, route_id, agent_id, client_name, provider_id, model_id,
                    response_model_id, inference_timeout_seconds,
                    reasoning_mode, route_class, enabled, created_utc, updated_utc,
                    profile_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.scope,
                    route_id,
                    agent_id,
                    client_name,
                    provider_id,
                    model_id,
                    response_model_id,
                    inference_timeout_seconds,
                    reasoning_mode,
                    route_class,
                    int(enabled),
                    now,
                    now,
                    profile_sha,
                ),
            )
            self._event(
                connection,
                "route.profile_upserted",
                {
                    "route_id": route_id,
                    "agent_id": agent_id,
                    "profile_sha256": profile_sha,
                    "enabled": enabled,
                },
            )
        return {**profile, "profile_sha256": profile_sha, "updated_utc": now}

    def upsert_provider_connection(self, args: dict[str, Any]) -> dict[str, Any]:
        """Record redacted provider metadata after a local secret backend accepts it."""
        if self.agent_id != HUMAN_OPERATOR_ID:
            raise BridgeError(
                "only the human operator can register a provider connection"
            )
        connection_id = _require_identifier(args.get("connection_id"), "connection_id")
        display_name = _require_text(args.get("display_name"), "display_name", limit=200)
        route_class = str(args.get("route_class") or "relay").strip().lower()
        if route_class not in ROUTE_CLASSES:
            raise BridgeError("route_class must be official, relay or local")
        provider_id = _require_identifier(args.get("provider_id"), "provider_id")
        secret_backend = str(args.get("secret_backend") or "").strip().lower()
        if secret_backend not in SECRET_BACKENDS:
            raise BridgeError(
                "secret_backend must be windows-credential-manager, cc-switch or native-acp"
            )
        if secret_backend == "cc-switch" and route_class != "relay":
            raise BridgeError("cc-switch provider connections must use relay route_class")
        if secret_backend == "native-acp" and route_class != "official":
            raise BridgeError("native-acp provider connections must use official route_class")
        stored_credential_target = _require_identifier(
            args.get("credential_target"), "credential_target"
        )
        if secret_backend == "windows-credential-manager":
            from .credentials import credential_target as expected_credential_target

            expected = expected_credential_target(self.scope, connection_id)
            if stored_credential_target != expected:
                raise BridgeError("credential_target does not match this scope and connection")
        endpoint_sha = _require_sha256(args.get("endpoint_sha256"), "endpoint_sha256")
        credential_sha = _require_sha256(
            args.get("credential_fingerprint_sha256"),
            "credential_fingerprint_sha256",
        )
        descriptor_schema = _require_identifier(
            args.get("descriptor_schema"), "descriptor_schema"
        )
        credential_version_sha = _require_sha256(
            args.get("credential_version_sha256"),
            "credential_version_sha256",
        )
        enabled = bool(args.get("enabled", True))
        now = utc_now()
        metadata = {
            "scope": self.scope,
            "connection_id": connection_id,
            "display_name": display_name,
            "route_class": route_class,
            "provider_id": provider_id,
            "secret_backend": secret_backend,
            "credential_target": stored_credential_target,
            "endpoint_sha256": endpoint_sha,
            "credential_fingerprint_sha256": credential_sha,
            "descriptor_schema": descriptor_schema,
            "credential_version_sha256": credential_version_sha,
            "enabled": enabled,
        }
        connection_sha = stable_sha256(metadata)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO provider_connections(
                    scope, connection_id, display_name, route_class, provider_id, secret_backend,
                    credential_target, endpoint_sha256, credential_fingerprint_sha256,
                    descriptor_schema, credential_version_sha256,
                    enabled, created_utc, updated_utc, connection_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, connection_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    route_class=excluded.route_class,
                    provider_id=excluded.provider_id,
                    secret_backend=excluded.secret_backend,
                    credential_target=excluded.credential_target,
                    endpoint_sha256=excluded.endpoint_sha256,
                    credential_fingerprint_sha256=excluded.credential_fingerprint_sha256,
                    descriptor_schema=excluded.descriptor_schema,
                    credential_version_sha256=excluded.credential_version_sha256,
                    enabled=excluded.enabled,
                    updated_utc=excluded.updated_utc,
                    connection_sha256=excluded.connection_sha256""",
                (
                    self.scope,
                    connection_id,
                    display_name,
                    route_class,
                    provider_id,
                    secret_backend,
                    stored_credential_target,
                    endpoint_sha,
                    credential_sha,
                    descriptor_schema,
                    credential_version_sha,
                    int(enabled),
                    now,
                    now,
                    connection_sha,
                ),
            )
            self._event(
                connection,
                "provider.connection_upserted",
                {
                    "connection_id": connection_id,
                    "connection_sha256": connection_sha,
                    "endpoint_sha256": endpoint_sha,
                    "secret_backend": secret_backend,
                    "enabled": enabled,
                },
            )
        return {
            **metadata,
            "connection_sha256": connection_sha,
            "updated_utc": now,
        }

    def list_provider_connections(self, args: dict[str, Any]) -> dict[str, Any]:
        enabled_only = bool(args.get("enabled_only", True))
        where = ["scope=?"]
        params: list[Any] = [self.scope]
        if enabled_only:
            where.append("enabled=1")
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM provider_connections
                    WHERE {' AND '.join(where)} ORDER BY display_name, connection_id""",
                tuple(params),
            ).fetchall()
        connections = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            connections.append(item)
        return {"connections": connections, "count": len(connections)}

    def list_route_profiles(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_agent = str(args.get("agent_id") or "").strip()
        agent_id = _require_identifier(raw_agent, "agent_id") if raw_agent else None
        enabled_only = bool(args.get("enabled_only", True))
        where = ["scope=?"]
        params: list[Any] = [self.scope]
        if agent_id:
            where.append("agent_id=?")
            params.append(agent_id)
        if enabled_only:
            where.append("enabled=1")
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM route_profiles WHERE {' AND '.join(where)}
                    ORDER BY route_id""",
                tuple(params),
            ).fetchall()
        profiles = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            profiles.append(item)
        return {"profiles": profiles, "count": len(profiles)}

    def list_agents(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return persistent global identities independently of room seats."""
        include_disabled_routes = bool(args.get("include_disabled_routes", False))
        cutoff = time.time() - self.presence_ttl_seconds
        with self._connect() as connection:
            visible_room_ids = self._visible_room_ids_locked(connection, self.agent_id)
            rows = connection.execute(
                """SELECT agent_id FROM agent_presence WHERE scope=?
                   UNION SELECT agent_id FROM route_profiles WHERE scope=?
                   UNION SELECT agent_id FROM room_memberships WHERE scope=?
                   UNION SELECT sender AS agent_id FROM messages WHERE scope=?
                   UNION SELECT recipient AS agent_id FROM messages
                         WHERE scope=? AND recipient!='*'
                   ORDER BY agent_id""",
                (self.scope,) * 5,
            ).fetchall()
            agents: list[dict[str, Any]] = []
            for row in rows:
                agent_id = str(row["agent_id"])
                sessions = [
                    dict(item)
                    for item in connection.execute(
                        """SELECT session_id, transport, client_name, provider_id,
                                  model_id, reasoning_mode, route_class, last_seen_utc
                           FROM agent_presence
                           WHERE scope=? AND agent_id=? AND last_seen_epoch>=?
                           ORDER BY last_seen_epoch DESC, session_id""",
                        (self.scope, agent_id, cutoff),
                    ).fetchall()
                ]
                route_where = "" if include_disabled_routes else "AND enabled=1"
                profiles = [
                    {**dict(item), "enabled": bool(item["enabled"])}
                    for item in connection.execute(
                        f"""SELECT * FROM route_profiles
                            WHERE scope=? AND agent_id=? {route_where}
                            ORDER BY route_id""",
                        (self.scope, agent_id),
                    ).fetchall()
                ]
                active_room_ids = [
                    str(item["room_id"])
                    for item in connection.execute(
                        """SELECT room_id FROM room_memberships
                           WHERE scope=? AND agent_id=? AND status='active'
                           ORDER BY room_id""",
                        (self.scope, agent_id),
                    ).fetchall()
                    if str(item["room_id"]) in visible_room_ids
                ]
                catalog_payload = {
                    "scope": self.scope,
                    "agent_id": agent_id,
                    "online_sessions": sessions,
                    "route_profiles": profiles,
                    "active_room_ids": active_room_ids,
                }
                agents.append(
                    {
                        **catalog_payload,
                        "online": bool(sessions),
                        "catalog_sha256": stable_sha256(catalog_payload),
                    }
                )
        return {
            "agents": agents,
            "count": len(agents),
            "presence_ttl_seconds": self.presence_ttl_seconds,
            "observed_utc": utc_now(),
        }

    def create_room(self, args: dict[str, Any]) -> dict[str, Any]:
        """Create one durable conversation room and join its creator."""
        room_id = _require_identifier(args.get("room_id"), "room_id")
        if room_id == DEFAULT_ROOM_ID:
            raise BridgeError("the built-in lobby room already exists")
        if room_id.startswith("history."):
            raise BridgeError("history.* room IDs are reserved for read-only imports")
        name = _require_text(args.get("name"), "name", limit=200)
        now = utc_now()
        payload = {
            "scope": self.scope,
            "room_id": room_id,
            "name": name,
            "created_by": self.agent_id,
            "archived": False,
        }
        room_sha = stable_sha256(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO rooms(
                        scope, room_id, name, created_by, archived, created_utc,
                        updated_utc, room_sha256
                    ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                    (
                        self.scope,
                        room_id,
                        name,
                        self.agent_id,
                        now,
                        now,
                        room_sha,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BridgeError("room already exists") from exc
            self._upsert_room_membership(
                connection,
                room_id=room_id,
                agent_id=self.agent_id,
                route_profile_id=None,
                now=now,
            )
            policy_payload = {
                "scope": self.scope,
                "room_id": room_id,
                "mode": "once",
                "max_rounds": DEFAULT_DISCUSSION_MAX_ROUNDS,
                "max_messages": DEFAULT_DISCUSSION_MAX_MESSAGES,
                "stagnation_rounds": DEFAULT_DISCUSSION_STAGNATION_ROUNDS,
                "updated_by": self.agent_id,
            }
            policy_sha = stable_sha256(policy_payload)
            connection.execute(
                """INSERT INTO room_automation_policies(
                       scope, room_id, mode, max_rounds, max_messages,
                       stagnation_rounds, updated_by, created_utc, updated_utc,
                       policy_sha256
                   ) VALUES (?, ?, 'once', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.scope,
                    room_id,
                    DEFAULT_DISCUSSION_MAX_ROUNDS,
                    DEFAULT_DISCUSSION_MAX_MESSAGES,
                    DEFAULT_DISCUSSION_STAGNATION_ROUNDS,
                    self.agent_id,
                    now,
                    now,
                    policy_sha,
                ),
            )
            event = self._event(
                connection,
                "room.created",
                {"room_id": room_id, "room_sha256": room_sha},
            )
        return {
            **payload,
            "room_sha256": room_sha,
            "created_utc": now,
            "creator_joined": True,
            "automation_mode": "once",
            "audit_chain_sha256": event["chain_sha256"],
        }

    def _room(self, connection: sqlite3.Connection, room_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM rooms WHERE scope=? AND room_id=?",
            (self.scope, room_id),
        ).fetchone()
        if row is None:
            raise BridgeError("room not found")
        if row["archived"]:
            raise BridgeError("room is archived")
        return row

    def _require_room_manager(
        self, connection: sqlite3.Connection, room_id: str
    ) -> sqlite3.Row:
        room = self._room(connection, room_id)
        if self.agent_id == HUMAN_OPERATOR_ID:
            return room
        if self.agent_id != str(room["created_by"]):
            raise BridgeError("only the room creator or human operator may manage seats")
        if self._active_room_member(connection, room_id, self.agent_id) is None:
            raise BridgeError("room creator must remain an active member to manage seats")
        return room

    @staticmethod
    def _normalize_room_role(
        role_id: Any,
        role_label: Any = None,
    ) -> tuple[str, str | None]:
        normalized = str(role_id or DEFAULT_ROOM_ROLE).strip().lower()
        if normalized not in ROOM_MEMBER_ROLES:
            raise BridgeError(
                "role_id must be equal-participant, researcher, implementer, "
                "reviewer or custom"
            )
        label_text = str(role_label or "").strip()
        if normalized == "custom":
            return normalized, _require_text(
                label_text,
                "role_label",
                limit=80,
            )
        if label_text:
            raise BridgeError("role_label is only valid for a custom role")
        return normalized, None

    @staticmethod
    def _room_role_payload(
        *,
        scope: str,
        room_id: str,
        agent_id: str,
        room_session_id: str,
        role_id: str,
        role_label: str | None,
        updated_by: str,
    ) -> dict[str, Any]:
        return {
            "scope": scope,
            "room_id": room_id,
            "agent_id": agent_id,
            "room_session_id": room_session_id,
            "role_id": role_id,
            "role_label": role_label,
            "updated_by": updated_by,
        }

    def _upsert_room_role(
        self,
        connection: sqlite3.Connection,
        *,
        room_id: str,
        agent_id: str,
        room_session_id: str,
        role_id: str | None,
        role_label: str | None,
        now: str,
    ) -> dict[str, Any]:
        existing = connection.execute(
            """SELECT * FROM room_member_roles
               WHERE scope=? AND room_id=? AND agent_id=?""",
            (self.scope, room_id, agent_id),
        ).fetchone()
        if role_id is None and existing is not None:
            normalized_role = str(existing["role_id"])
            normalized_label = existing["role_label"]
            created_utc = str(existing["created_utc"])
        else:
            normalized_role, normalized_label = self._normalize_room_role(
                role_id,
                role_label,
            )
            created_utc = str(existing["created_utc"]) if existing is not None else now
        payload = self._room_role_payload(
            scope=self.scope,
            room_id=room_id,
            agent_id=agent_id,
            room_session_id=room_session_id,
            role_id=normalized_role,
            role_label=(str(normalized_label) if normalized_label is not None else None),
            updated_by=self.agent_id,
        )
        role_sha = stable_sha256(payload)
        connection.execute(
            """INSERT INTO room_member_roles(
                   scope, room_id, agent_id, room_session_id, role_id,
                   role_label, updated_by, created_utc, updated_utc, role_sha256
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, room_id, agent_id) DO UPDATE SET
                   room_session_id=excluded.room_session_id,
                   role_id=excluded.role_id,
                   role_label=excluded.role_label,
                   updated_by=excluded.updated_by,
                   updated_utc=excluded.updated_utc,
                   role_sha256=excluded.role_sha256""",
            (
                self.scope,
                room_id,
                agent_id,
                room_session_id,
                normalized_role,
                normalized_label,
                self.agent_id,
                created_utc,
                now,
                role_sha,
            ),
        )
        return {
            **payload,
            "created_utc": created_utc,
            "updated_utc": now,
            "role_sha256": role_sha,
        }

    @staticmethod
    def _bounded_integer(
        value: Any, label: str, *, minimum: int, maximum: int
    ) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise BridgeError(f"{label} must be an integer") from exc
        if result < minimum or result > maximum:
            raise BridgeError(f"{label} must be between {minimum} and {maximum}")
        return result

    @staticmethod
    def _policy_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "scope": str(row["scope"]),
            "room_id": str(row["room_id"]),
            "mode": str(row["mode"]),
            "max_rounds": int(row["max_rounds"]),
            "max_messages": int(row["max_messages"]),
            "stagnation_rounds": int(row["stagnation_rounds"]),
            "updated_by": str(row["updated_by"]),
        }

    def _room_policy(
        self, connection: sqlite3.Connection, room_id: str
    ) -> sqlite3.Row:
        self._room(connection, room_id)
        row = connection.execute(
            """SELECT * FROM room_automation_policies
               WHERE scope=? AND room_id=?""",
            (self.scope, room_id),
        ).fetchone()
        if row is None:
            raise BridgeError("room automation policy is missing")
        return row

    def get_room_automation(self, args: dict[str, Any]) -> dict[str, Any]:
        room_id = _require_identifier(args.get("room_id"), "room_id")
        with self._connect() as connection:
            if self.agent_id not in {
                HUMAN_OPERATOR_ID,
                CONTROL_ROOM_WORKFLOW_ID,
            }:
                self._require_room_member(connection, room_id, self.agent_id)
            policy = self._room_policy(connection, room_id)
            active = connection.execute(
                """SELECT * FROM room_discussions
                   WHERE scope=? AND room_id=?
                     AND status IN ('active', 'paused', 'waiting_human')
                   ORDER BY updated_utc DESC, discussion_id DESC LIMIT 1""",
                (self.scope, room_id),
            ).fetchone()
        result = dict(policy)
        result["policy_sha256_valid"] = (
            stable_sha256(self._policy_payload(policy)) == policy["policy_sha256"]
        )
        result["active_discussion"] = dict(active) if active else None
        return result

    def set_room_automation(self, args: dict[str, Any]) -> dict[str, Any]:
        room_id = _require_identifier(args.get("room_id"), "room_id")
        mode = str(args.get("mode") or "").strip().lower()
        if mode not in ROOM_AUTOMATION_MODES:
            raise BridgeError("mode must be off, once or discussion")
        max_rounds = self._bounded_integer(
            args.get("max_rounds", DEFAULT_DISCUSSION_MAX_ROUNDS),
            "max_rounds",
            minimum=1,
            maximum=MAX_DISCUSSION_ROUNDS,
        )
        max_messages = self._bounded_integer(
            args.get("max_messages", DEFAULT_DISCUSSION_MAX_MESSAGES),
            "max_messages",
            minimum=2,
            maximum=MAX_DISCUSSION_MESSAGES,
        )
        stagnation_rounds = self._bounded_integer(
            args.get("stagnation_rounds", DEFAULT_DISCUSSION_STAGNATION_ROUNDS),
            "stagnation_rounds",
            minimum=1,
            maximum=5,
        )
        if stagnation_rounds > max_rounds:
            raise BridgeError("stagnation_rounds cannot exceed max_rounds")
        now = utc_now()
        payload = {
            "scope": self.scope,
            "room_id": room_id,
            "mode": mode,
            "max_rounds": max_rounds,
            "max_messages": max_messages,
            "stagnation_rounds": stagnation_rounds,
            "updated_by": self.agent_id,
        }
        policy_sha = stable_sha256(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_room_manager(connection, room_id)
            existing = self._room_policy(connection, room_id)
            connection.execute(
                """UPDATE room_automation_policies
                      SET mode=?, max_rounds=?, max_messages=?, stagnation_rounds=?,
                          updated_by=?, updated_utc=?, policy_sha256=?
                    WHERE scope=? AND room_id=?""",
                (
                    mode,
                    max_rounds,
                    max_messages,
                    stagnation_rounds,
                    self.agent_id,
                    now,
                    policy_sha,
                    self.scope,
                    room_id,
                ),
            )
            if mode != "discussion":
                open_discussions = connection.execute(
                    """SELECT * FROM room_discussions
                        WHERE scope=? AND room_id=?
                          AND status IN ('active', 'paused', 'waiting_human')""",
                    (self.scope, room_id),
                ).fetchall()
                for discussion in open_discussions:
                    self._store_discussion_state(
                        connection,
                        discussion,
                        now=now,
                        status="stopped",
                        stop_reason="policy_changed",
                    )
            event = self._event(
                connection,
                "room.automation_updated",
                {
                    "room_id": room_id,
                    "previous_policy_sha256": existing["policy_sha256"],
                    "policy_sha256": policy_sha,
                    "mode": mode,
                    "max_rounds": max_rounds,
                    "max_messages": max_messages,
                    "stagnation_rounds": stagnation_rounds,
                },
            )
        return {
            **payload,
            "created_utc": existing["created_utc"],
            "updated_utc": now,
            "policy_sha256": policy_sha,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def _active_room_member(
        self, connection: sqlite3.Connection, room_id: str, agent_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT * FROM room_memberships
               WHERE scope=? AND room_id=? AND agent_id=? AND status='active'""",
            (self.scope, room_id, agent_id),
        ).fetchone()

    def _require_room_member(
        self, connection: sqlite3.Connection, room_id: str, agent_id: str
    ) -> sqlite3.Row | None:
        self._room(connection, room_id)
        if room_id == DEFAULT_ROOM_ID:
            # Lobby membership is implicit unless an audited ``left`` override
            # exists.  This lets the UI remove one Agent from Lobby without
            # deleting its global identity, route profiles, or message history.
            row = connection.execute(
                """SELECT * FROM room_memberships
                   WHERE scope=? AND room_id=? AND agent_id=?""",
                (self.scope, room_id, agent_id),
            ).fetchone()
            if row is not None and row["status"] != "active":
                raise BridgeError(f"agent is not an active member of room {room_id}")
            return row
        row = self._active_room_member(connection, room_id, agent_id)
        if row is None:
            raise BridgeError(f"agent is not an active member of room {room_id}")
        return row

    def _upsert_room_membership(
        self,
        connection: sqlite3.Connection,
        *,
        room_id: str,
        agent_id: str,
        route_profile_id: str | None,
        now: str,
        role_id: str | None = None,
        role_label: str | None = None,
    ) -> dict[str, Any]:
        existing = connection.execute(
            "SELECT room_session_id, joined_utc, route_profile_id, status "
            "FROM room_memberships "
            "WHERE scope=? AND room_id=? AND agent_id=?",
            (self.scope, room_id, agent_id),
        ).fetchone()
        reuse_session = bool(
            existing
            and existing["status"] == "active"
            and existing["route_profile_id"] == route_profile_id
        )
        room_session_id = str(existing["room_session_id"]) if reuse_session else uuid.uuid4().hex
        joined_utc = str(existing["joined_utc"]) if reuse_session else now
        payload = {
            "scope": self.scope,
            "room_id": room_id,
            "agent_id": agent_id,
            "route_profile_id": route_profile_id,
            "room_session_id": room_session_id,
            "status": "active",
            "joined_utc": joined_utc,
        }
        membership_sha = stable_sha256(payload)
        connection.execute(
            """INSERT INTO room_memberships(
                scope, room_id, agent_id, route_profile_id, room_session_id,
                status, joined_utc, updated_utc, left_utc, membership_sha256
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, NULL, ?)
            ON CONFLICT(scope, room_id, agent_id) DO UPDATE SET
                route_profile_id=excluded.route_profile_id,
                room_session_id=excluded.room_session_id,
                status='active', joined_utc=excluded.joined_utc,
                updated_utc=excluded.updated_utc,
                left_utc=NULL, membership_sha256=excluded.membership_sha256""",
            (
                self.scope,
                room_id,
                agent_id,
                route_profile_id,
                room_session_id,
                joined_utc,
                now,
                membership_sha,
            ),
        )
        role = self._upsert_room_role(
            connection,
            room_id=room_id,
            agent_id=agent_id,
            room_session_id=room_session_id,
            role_id=role_id,
            role_label=role_label,
            now=now,
        )
        return {
            **payload,
            "membership_sha256": membership_sha,
            "updated_utc": now,
            "role_id": role["role_id"],
            "role_label": role["role_label"],
            "role_sha256": role["role_sha256"],
        }

    def join_room(self, args: dict[str, Any]) -> dict[str, Any]:
        room_id = _require_identifier(args.get("room_id"), "room_id")
        agent_id = _require_identifier(args.get("agent_id", self.agent_id), "agent_id")
        if agent_id in RESERVED_INTERNAL_AGENT_IDS and self.agent_id != agent_id:
            raise BridgeError("reserved internal Agent cannot be added as a room seat")
        route_profile_id = _optional_identifier(
            args.get("route_profile_id"), "route_profile_id"
        )
        role_supplied = "role_id" in args or "role_label" in args
        role_id: str | None = None
        role_label: str | None = None
        if role_supplied:
            role_id, role_label = self._normalize_room_role(
                args.get("role_id"),
                args.get("role_label"),
            )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_room_manager(connection, room_id)
            if route_profile_id:
                profile = connection.execute(
                    """SELECT agent_id FROM route_profiles
                       WHERE scope=? AND route_id=? AND enabled=1""",
                    (self.scope, route_profile_id),
                ).fetchone()
                if profile is None:
                    raise BridgeError("route profile is missing or disabled")
                if profile["agent_id"] != agent_id:
                    raise BridgeError("route profile targets a different agent")
            membership = self._upsert_room_membership(
                connection,
                room_id=room_id,
                agent_id=agent_id,
                route_profile_id=route_profile_id,
                now=now,
                role_id=role_id,
                role_label=role_label,
            )
            event = self._event(
                connection,
                "room.member_joined",
                {
                    "room_id": room_id,
                    "agent_id": agent_id,
                    "membership_sha256": membership["membership_sha256"],
                    "role_id": membership["role_id"],
                    "role_sha256": membership["role_sha256"],
                },
            )
        return {**membership, "audit_chain_sha256": event["chain_sha256"]}

    def set_room_member_role(self, args: dict[str, Any]) -> dict[str, Any]:
        room_id = _require_identifier(args.get("room_id"), "room_id")
        agent_id = _require_identifier(args.get("agent_id"), "agent_id")
        role_id, role_label = self._normalize_room_role(
            args.get("role_id"),
            args.get("role_label"),
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_room_manager(connection, room_id)
            membership = connection.execute(
                """SELECT * FROM room_memberships
                   WHERE scope=? AND room_id=? AND agent_id=?""",
                (self.scope, room_id, agent_id),
            ).fetchone()
            materialized = False
            if membership is None:
                if room_id != DEFAULT_ROOM_ID:
                    raise BridgeError("agent is not an active room member")
                created = self._upsert_room_membership(
                    connection,
                    room_id=room_id,
                    agent_id=agent_id,
                    route_profile_id=None,
                    now=now,
                    role_id=role_id,
                    role_label=role_label,
                )
                membership = connection.execute(
                    """SELECT * FROM room_memberships
                       WHERE scope=? AND room_id=? AND agent_id=?""",
                    (self.scope, room_id, agent_id),
                ).fetchone()
                assert membership is not None
                previous_role_sha = None
                role = {
                    "scope": self.scope,
                    "room_id": room_id,
                    "agent_id": agent_id,
                    "room_session_id": created["room_session_id"],
                    "role_id": created["role_id"],
                    "role_label": created["role_label"],
                    "updated_by": self.agent_id,
                    "created_utc": now,
                    "updated_utc": now,
                    "role_sha256": created["role_sha256"],
                }
                materialized = True
            else:
                if membership["status"] != "active":
                    raise BridgeError("agent is not an active room member")
                previous = connection.execute(
                    """SELECT * FROM room_member_roles
                       WHERE scope=? AND room_id=? AND agent_id=?""",
                    (self.scope, room_id, agent_id),
                ).fetchone()
                previous_role_sha = (
                    str(previous["role_sha256"]) if previous is not None else None
                )
                role = self._upsert_room_role(
                    connection,
                    room_id=room_id,
                    agent_id=agent_id,
                    room_session_id=str(membership["room_session_id"]),
                    role_id=role_id,
                    role_label=role_label,
                    now=now,
                )
            event = self._event(
                connection,
                "room.member_role_updated",
                {
                    "room_id": room_id,
                    "agent_id": agent_id,
                    "room_session_id": str(membership["room_session_id"]),
                    "role_id": role["role_id"],
                    "role_label": role["role_label"],
                    "previous_role_sha256": previous_role_sha,
                    "role_sha256": role["role_sha256"],
                    "membership_materialized": materialized,
                    "authority_effect": "none",
                },
            )
        return {
            **role,
            "membership_sha256": str(membership["membership_sha256"]),
            "membership_status": str(membership["status"]),
            "authority_effect": "none",
            "audit_chain_sha256": event["chain_sha256"],
        }

    def leave_room(self, args: dict[str, Any]) -> dict[str, Any]:
        room_id = _require_identifier(args.get("room_id"), "room_id")
        agent_id = _require_identifier(args.get("agent_id", self.agent_id), "agent_id")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._room(connection, room_id)
            if agent_id != self.agent_id:
                self._require_room_manager(connection, room_id)
            row = (
                connection.execute(
                    """SELECT * FROM room_memberships
                       WHERE scope=? AND room_id=? AND agent_id=?""",
                    (self.scope, room_id, agent_id),
                ).fetchone()
                if room_id == DEFAULT_ROOM_ID
                else self._active_room_member(connection, room_id, agent_id)
            )
            if row is not None and row["status"] != "active":
                raise BridgeError("agent is not an active room member")
            if row is None and room_id != DEFAULT_ROOM_ID:
                raise BridgeError("agent is not an active room member")
            route_profile_id = row["route_profile_id"] if row is not None else None
            room_session_id = (
                str(row["room_session_id"]) if row is not None else uuid.uuid4().hex
            )
            joined_utc = str(row["joined_utc"]) if row is not None else now
            payload = {
                "scope": self.scope,
                "room_id": room_id,
                "agent_id": agent_id,
                "route_profile_id": route_profile_id,
                "room_session_id": room_session_id,
                "status": "left",
                "joined_utc": joined_utc,
                "left_utc": now,
            }
            membership_sha = stable_sha256(payload)
            connection.execute(
                """INSERT INTO room_memberships(
                       scope, room_id, agent_id, route_profile_id, room_session_id,
                       status, joined_utc, updated_utc, left_utc, membership_sha256
                   ) VALUES (?, ?, ?, ?, ?, 'left', ?, ?, ?, ?)
                   ON CONFLICT(scope, room_id, agent_id) DO UPDATE SET
                       route_profile_id=excluded.route_profile_id,
                       room_session_id=excluded.room_session_id,
                       status='left', joined_utc=excluded.joined_utc,
                       updated_utc=excluded.updated_utc,
                       left_utc=excluded.left_utc,
                       membership_sha256=excluded.membership_sha256""",
                (
                    self.scope,
                    room_id,
                    agent_id,
                    route_profile_id,
                    room_session_id,
                    joined_utc,
                    now,
                    now,
                    membership_sha,
                ),
            )
            role = self._upsert_room_role(
                connection,
                room_id=room_id,
                agent_id=agent_id,
                room_session_id=room_session_id,
                role_id=None,
                role_label=None,
                now=now,
            )
            event = self._event(
                connection,
                "room.member_left",
                {
                    "room_id": room_id,
                    "agent_id": agent_id,
                    "membership_sha256": membership_sha,
                    "role_id": role["role_id"],
                    "role_sha256": role["role_sha256"],
                },
            )
        return {
            **payload,
            "membership_sha256": membership_sha,
            "role_id": role["role_id"],
            "role_label": role["role_label"],
            "role_sha256": role["role_sha256"],
            "audit_chain_sha256": event["chain_sha256"],
        }

    def list_rooms(self, args: dict[str, Any]) -> dict[str, Any]:
        include_archived = bool(args.get("include_archived", False))
        where = [] if include_archived else ["r.archived=0"]
        params: list[Any] = [self.scope]
        if self.agent_id != HUMAN_OPERATOR_ID:
            where.append(
                """(
                    (r.room_id=? AND NOT EXISTS(
                        SELECT 1 FROM room_memberships denied
                        WHERE denied.scope=r.scope AND denied.room_id=r.room_id
                          AND denied.agent_id=? AND denied.status!='active'
                    ))
                    OR EXISTS(
                        SELECT 1 FROM room_memberships viewer
                        WHERE viewer.scope=r.scope AND viewer.room_id=r.room_id
                          AND viewer.agent_id=? AND viewer.status='active'
                    )
                )"""
            )
            params.extend((DEFAULT_ROOM_ID, self.agent_id, self.agent_id))
        where_sql = f"AND {' AND '.join(where)}" if where else ""
        params.append(DEFAULT_ROOM_ID)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT r.*, rap.mode AS automation_mode,
                            rap.max_rounds AS automation_max_rounds,
                            rap.max_messages AS automation_max_messages,
                            rap.stagnation_rounds AS automation_stagnation_rounds,
                            rap.policy_sha256 AS automation_policy_sha256,
                    (SELECT COUNT(*) FROM room_memberships rm
                     WHERE rm.scope=r.scope AND rm.room_id=r.room_id
                       AND rm.status='active') AS active_member_count,
                    (SELECT COUNT(*) FROM messages m
                     WHERE m.scope=r.scope AND m.room_id=r.room_id) AS message_count
                    FROM rooms r
                    LEFT JOIN room_automation_policies rap
                      ON rap.scope=r.scope AND rap.room_id=r.room_id
                    WHERE r.scope=? {where_sql}
                    ORDER BY CASE WHEN r.room_id=? THEN 0 ELSE 1 END,
                             r.updated_utc DESC, r.room_id""",
                tuple(params),
            ).fetchall()
        rooms = []
        for row in rows:
            item = dict(row)
            item["archived"] = bool(item["archived"])
            rooms.append(item)
        return {"rooms": rooms, "count": len(rooms)}

    def _room_members_locked(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        self._room(connection, room_id)
        where = "" if include_inactive else "AND rm.status='active'"
        rows = connection.execute(
            f"""SELECT rm.*, rp.client_name, rp.provider_id, rp.model_id,
                        rp.response_model_id, rp.reasoning_mode, rp.route_class,
                        rp.profile_sha256 AS route_profile_sha256,
                        COALESCE(rmr.role_id, ?) AS role_id,
                        rmr.role_label, rmr.role_sha256,
                        CASE WHEN EXISTS(
                            SELECT 1 FROM agent_presence ap
                            WHERE ap.scope=rm.scope AND ap.agent_id=rm.agent_id
                              AND ap.last_seen_epoch>=?
                        ) THEN 1 ELSE 0 END AS online
                FROM room_memberships rm
                LEFT JOIN route_profiles rp
                  ON rp.scope=rm.scope AND rp.route_id=rm.route_profile_id
                LEFT JOIN room_member_roles rmr
                  ON rmr.scope=rm.scope AND rmr.room_id=rm.room_id
                 AND rmr.agent_id=rm.agent_id
                 AND rmr.room_session_id=rm.room_session_id
                WHERE rm.scope=? AND rm.room_id=? {where}
                ORDER BY rm.status, rm.agent_id""",
            (
                DEFAULT_ROOM_ROLE,
                time.time() - self.presence_ttl_seconds,
                self.scope,
                room_id,
            ),
        ).fetchall()
        members = []
        for row in rows:
            item = dict(row)
            item["online"] = bool(item["online"])
            members.append(item)
        return members

    def _visible_room_ids_locked(
        self,
        connection: sqlite3.Connection,
        agent_id: str,
    ) -> set[str]:
        if agent_id == HUMAN_OPERATOR_ID:
            return {
                str(row["room_id"])
                for row in connection.execute(
                    "SELECT room_id FROM rooms WHERE scope=?",
                    (self.scope,),
                ).fetchall()
            }

        visible = {
            str(row["room_id"])
            for row in connection.execute(
                """SELECT room_id FROM room_memberships
                   WHERE scope=? AND agent_id=? AND status='active'""",
                (self.scope, agent_id),
            ).fetchall()
        }
        lobby_override = connection.execute(
            """SELECT status FROM room_memberships
               WHERE scope=? AND room_id=? AND agent_id=?""",
            (self.scope, DEFAULT_ROOM_ID, agent_id),
        ).fetchone()
        if lobby_override is None or lobby_override["status"] == "active":
            visible.add(DEFAULT_ROOM_ID)
        return visible

    def room_members(self, args: dict[str, Any]) -> dict[str, Any]:
        room_id = _require_identifier(args.get("room_id"), "room_id")
        include_inactive = bool(args.get("include_inactive", False))
        with self._connect() as connection:
            if self.agent_id != HUMAN_OPERATOR_ID:
                self._require_room_member(connection, room_id, self.agent_id)
            members = self._room_members_locked(
                connection,
                room_id,
                include_inactive=include_inactive,
            )
        return {"room_id": room_id, "members": members, "count": len(members)}

    def _memory_artifact_bindings(self, values: Any) -> list[dict[str, Any]]:
        normalized_paths = self._clean_artifacts(values)
        if len(normalized_paths) > MAX_MEMORY_ARTIFACTS:
            raise BridgeError("memory artifact count exceeds the limit")
        bindings = []
        total_bytes = 0
        for normalized in normalized_paths:
            resolved = self._resolve_path(normalized, must_exist=True)
            try:
                size = resolved.stat().st_size
            except OSError as exc:
                raise BridgeError("memory artifact is unavailable") from exc
            if size > MAX_MEMORY_ARTIFACT_BYTES:
                raise BridgeError("memory artifact exceeds the per-file limit")
            total_bytes += size
            if total_bytes > MAX_MEMORY_ARTIFACT_TOTAL_BYTES:
                raise BridgeError("memory artifacts exceed the total byte limit")
            hashed = self._hash_file_streaming(resolved)
            if int(hashed["bytes"]) != size:
                raise BridgeError("memory artifact changed while it was being hashed")
            bindings.append(
                {
                    "path": normalized,
                    "bytes": int(hashed["bytes"]),
                    "sha256": str(hashed["sha256"]),
                }
            )
        return bindings

    def _memory_row(
        self, connection: sqlite3.Connection, memory_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM memories WHERE scope=? AND memory_id=?",
            (self.scope, memory_id),
        ).fetchone()
        if row is None:
            raise BridgeError("memory not found")
        self._verify_memory_row(connection, row)
        return row

    def _verify_memory_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> None:
        stored = str(row["memory_sha256"] or "")
        if re.fullmatch(r"[0-9a-f]{64}", stored) is None:
            raise BridgeError("memory SHA-256 mismatch")

        source_message_id = row["source_message_id"]
        source_room_id = None
        source_message_sha256 = None
        if source_message_id is not None:
            source = connection.execute(
                """SELECT room_id, content_sha256 FROM messages
                    WHERE scope=? AND message_id=?""",
                (self.scope, source_message_id),
            ).fetchone()
            if source is None:
                raise BridgeError("memory source message is unavailable")
            source_room_id = str(source["room_id"])
            source_message_sha256 = str(source["content_sha256"])
            if row["source_message_sha256"] != source_message_sha256:
                raise BridgeError("memory source message SHA-256 mismatch")
        elif row["source_message_sha256"] is not None:
            raise BridgeError("memory source message SHA-256 mismatch")

        parent_memory_sha256 = None
        if row["parent_memory_id"] is not None:
            parent = connection.execute(
                """SELECT memory_sha256 FROM memories
                    WHERE scope=? AND memory_id=?""",
                (self.scope, row["parent_memory_id"]),
            ).fetchone()
            if parent is None:
                raise BridgeError("memory parent is unavailable")
            parent_memory_sha256 = str(parent["memory_sha256"])

        supersedes_memory_sha256 = None
        if row["supersedes_memory_id"] is not None:
            superseded = connection.execute(
                """SELECT memory_sha256 FROM memories
                    WHERE scope=? AND memory_id=?""",
                (self.scope, row["supersedes_memory_id"]),
            ).fetchone()
            if superseded is None:
                raise BridgeError("superseded memory is unavailable")
            supersedes_memory_sha256 = str(superseded["memory_sha256"])

        try:
            artifact_bindings = json.loads(str(row["artifact_bindings_json"]))
            applicability = json.loads(str(row["applicability_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BridgeError("memory binding JSON is invalid") from exc
        if not isinstance(artifact_bindings, list) or not isinstance(
            applicability, list
        ):
            raise BridgeError("memory binding JSON is invalid")

        payload = {
            "scope": row["scope"],
            "memory_id": row["memory_id"],
            "visibility": row["visibility"],
            "room_id": row["room_id"],
            "owner_agent_id": row["owner_agent_id"],
            "record_type": row["record_type"],
            "authority_id": row["authority_id"],
            "title": row["title"],
            "body": row["body"],
            "source_message_id": source_message_id,
            "source_message_sha256": source_message_sha256,
            "source_room_id": source_room_id,
            "artifact_bindings": artifact_bindings,
            "parent_memory_id": row["parent_memory_id"],
            "parent_memory_sha256": parent_memory_sha256,
            "supersedes_memory_id": row["supersedes_memory_id"],
            "supersedes_memory_sha256": supersedes_memory_sha256,
            "applicability": applicability,
            "status": "active",
            "created_utc": row["created_utc"],
        }
        if secrets.compare_digest(stored, stable_sha256(payload)):
            return

        legacy_v17 = (
            row["record_type"] == "FACT"
            and row["authority_id"] is None
            and row["supersedes_memory_id"] is None
            and applicability == []
        )
        if legacy_v17:
            legacy_payload = dict(payload)
            for key in (
                "record_type",
                "authority_id",
                "supersedes_memory_id",
                "supersedes_memory_sha256",
                "applicability",
            ):
                legacy_payload.pop(key)
            if secrets.compare_digest(stored, stable_sha256(legacy_payload)):
                return
        raise BridgeError("memory SHA-256 mismatch")

    def _require_memory_read_access(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> None:
        visibility = str(row["visibility"])
        if visibility == "project":
            return
        if visibility == "private":
            if row["owner_agent_id"] != self.agent_id:
                raise BridgeError("private memory belongs to another agent")
            self._require_room_member(
                connection, str(row["room_id"] or ""), self.agent_id
            )
            return
        room_id = str(row["room_id"] or "")
        self._require_room_member(connection, room_id, self.agent_id)

    def _source_message_binding(
        self, connection: sqlite3.Connection, message_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT message_id, room_id, sender, recipient, content_sha256
               FROM messages WHERE scope=? AND message_id=?""",
            (self.scope, message_id),
        ).fetchone()
        if row is None:
            raise BridgeError("source message not found")
        room_id = str(row["room_id"])
        self._require_room_member(connection, room_id, self.agent_id)
        if self.agent_id != "human-operator" and self.agent_id not in {
            str(row["sender"]),
            str(row["recipient"]),
        } and row["recipient"] != "*":
            raise BridgeError("source message is not visible to this agent")
        return {
            "message_id": str(row["message_id"]),
            "room_id": room_id,
            "content_sha256": str(row["content_sha256"]),
        }

    @staticmethod
    def _memory_result(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["artifact_bindings"] = json.loads(
            result.pop("artifact_bindings_json")
        )
        result["applicability"] = json.loads(result.pop("applicability_json", "[]"))
        return result

    def record_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        """Record an explicit memory without copying a model's private reasoning."""
        visibility = str(args.get("visibility") or "private").strip().lower()
        if visibility not in MEMORY_VISIBILITIES:
            raise BridgeError("visibility must be private, room or project")
        room_id = _optional_identifier(args.get("room_id"), "room_id")
        if visibility in {"private", "room"} and not room_id:
            raise BridgeError("private and room memory require room_id")
        if visibility == "project" and room_id:
            raise BridgeError("project memory must not bind one room_id")
        if visibility == "project" and self.agent_id != "human-operator":
            raise BridgeError("only human-operator may publish project memory")

        record_type = str(args.get("record_type") or "FACT").strip().upper()
        if record_type not in MEMORY_RECORD_TYPES:
            raise BridgeError(
                "record_type must be FACT, DECISION, CONSTRAINT, PREFERENCE or DEPRECATED"
            )
        authority_id = _optional_identifier(
            args.get("authority_id") or self.agent_id, "authority_id"
        )
        if visibility == "project" and authority_id != "human-operator":
            raise BridgeError("project memory requires human-operator authority")
        applicability = []
        for item in _json_list(args.get("applicability", []), "applicability"):
            value = _require_text(item, "applicability item", limit=200)
            if value not in applicability:
                applicability.append(value)
        if len(applicability) > 50:
            raise BridgeError("applicability exceeds 50 entries")

        title = _require_text(args.get("title"), "title", limit=500)
        body = _require_text(args.get("body"), "body")
        source_message_id = _optional_identifier(
            args.get("source_message_id"), "source_message_id"
        )
        parent_memory_id = _optional_identifier(
            args.get("parent_memory_id"), "parent_memory_id"
        )
        supersedes_memory_id = _optional_identifier(
            args.get("supersedes_memory_id"), "supersedes_memory_id"
        )
        if record_type == "DEPRECATED" and not supersedes_memory_id:
            raise BridgeError("DEPRECATED memory must supersede an existing memory")
        artifact_bindings = self._memory_artifact_bindings(
            args.get("artifact_paths", [])
        )
        memory_id = uuid.uuid4().hex
        created = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if visibility in {"private", "room"}:
                assert room_id is not None
                self._require_room_member(connection, room_id, self.agent_id)

            source = None
            if source_message_id:
                source = self._source_message_binding(connection, source_message_id)
                if visibility in {"private", "room"} and source["room_id"] != room_id:
                    raise BridgeError(
                        "private or room memory source_message_id must be from the same room"
                    )

            parent = None
            if parent_memory_id:
                parent = self._memory_row(connection, parent_memory_id)
                self._require_memory_read_access(connection, parent)
                if parent["status"] != "active":
                    raise BridgeError("parent memory is revoked")
                if visibility in {"private", "room"} and (
                    parent["visibility"] != visibility
                    or parent["room_id"] != room_id
                ):
                    raise BridgeError(
                        "private or room memory parent must have the same visibility and room"
                    )

            superseded = None
            if supersedes_memory_id:
                superseded = self._memory_row(connection, supersedes_memory_id)
                self._require_memory_read_access(connection, superseded)
                if superseded["status"] != "active":
                    raise BridgeError("superseded memory is revoked")
                if superseded["visibility"] != visibility or superseded["room_id"] != room_id:
                    raise BridgeError(
                        "superseded memory must have the same visibility and room"
                    )
                if (
                    superseded["owner_agent_id"] != self.agent_id
                    and self.agent_id != HUMAN_OPERATOR_ID
                ):
                    if visibility != "room" or room_id is None:
                        raise BridgeError(
                            "only the memory owner or human operator may supersede it"
                        )
                    self._require_room_manager(connection, room_id)

            if visibility == "project" and not any(
                (source, parent, superseded, artifact_bindings)
            ):
                raise BridgeError(
                    "project memory requires a source message, parent memory or artifact"
                )

            payload = {
                "scope": self.scope,
                "memory_id": memory_id,
                "visibility": visibility,
                "room_id": room_id,
                "owner_agent_id": self.agent_id,
                "record_type": record_type,
                "authority_id": authority_id,
                "title": title,
                "body": body,
                "source_message_id": source_message_id,
                "source_message_sha256": (
                    source["content_sha256"] if source else None
                ),
                "source_room_id": source["room_id"] if source else None,
                "artifact_bindings": artifact_bindings,
                "parent_memory_id": parent_memory_id,
                "parent_memory_sha256": (
                    str(parent["memory_sha256"]) if parent else None
                ),
                "supersedes_memory_id": supersedes_memory_id,
                "supersedes_memory_sha256": (
                    str(superseded["memory_sha256"]) if superseded else None
                ),
                "applicability": applicability,
                "status": "active",
                "created_utc": created,
            }
            memory_sha = stable_sha256(payload)
            connection.execute(
                """INSERT INTO memories(
                    scope, memory_id, visibility, room_id, owner_agent_id,
                    record_type, authority_id,
                    title, body, source_message_id, source_message_sha256,
                    artifact_bindings_json, parent_memory_id,
                    supersedes_memory_id, applicability_json, status,
                    created_utc, revoked_utc, revocation_reason,
                    revocation_sha256, memory_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?,
                          NULL, NULL, NULL, ?)""",
                (
                    self.scope,
                    memory_id,
                    visibility,
                    room_id,
                    self.agent_id,
                    record_type,
                    authority_id,
                    title,
                    body,
                    source_message_id,
                    source["content_sha256"] if source else None,
                    json.dumps(
                        artifact_bindings,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    parent_memory_id,
                    supersedes_memory_id,
                    json.dumps(
                        applicability,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    created,
                    memory_sha,
                ),
            )
            event = self._event(
                connection,
                "memory.recorded",
                {
                    "memory_id": memory_id,
                    "visibility": visibility,
                    "room_id": room_id,
                    "record_type": record_type,
                    "authority_id": authority_id,
                    "supersedes_memory_id": supersedes_memory_id,
                    "memory_sha256": memory_sha,
                },
            )
        return {
            **payload,
            "memory_sha256": memory_sha,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def list_memories(self, args: dict[str, Any]) -> dict[str, Any]:
        visibility = str(args.get("visibility") or "").strip().lower() or None
        if visibility and visibility not in MEMORY_VISIBILITIES:
            raise BridgeError("visibility must be private, room or project")
        room_id = _optional_identifier(args.get("room_id"), "room_id")
        if visibility in {"private", "room"} and not room_id:
            raise BridgeError("listing private or room memory requires room_id")
        if room_id:
            with self._connect() as connection:
                self._require_room_member(connection, room_id, self.agent_id)
        query = str(args.get("query") or "").strip().lower()
        if len(query) > 500:
            raise BridgeError("query exceeds 500 characters")
        include_revoked = bool(args.get("include_revoked", False))
        limit = max(1, min(int(args.get("limit", 100)), 500))

        where = ["scope=?"]
        params: list[Any] = [self.scope]
        if not include_revoked:
            where.append("status='active'")
        if visibility:
            where.append("visibility=?")
            params.append(visibility)
        if room_id:
            where.append("(visibility='project' OR room_id=?)")
            params.append(room_id)
        else:
            where.append("visibility='project'")
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM memories WHERE {' AND '.join(where)}
                    ORDER BY created_utc DESC, memory_id LIMIT ?""",
                (*params, limit * 4),
            ).fetchall()
            superseding_rows = connection.execute(
                """SELECT * FROM memories
                    WHERE scope=? AND supersedes_memory_id IS NOT NULL
                    ORDER BY created_utc DESC, memory_id DESC""",
                (self.scope,),
            ).fetchall()
            superseded_by = {}
            for item in superseding_rows:
                try:
                    self._require_memory_read_access(connection, item)
                except BridgeError:
                    continue
                self._verify_memory_row(connection, item)
                superseded_by[str(item["supersedes_memory_id"])] = str(
                    item["memory_id"]
                )
            memories = []
            for row in rows:
                try:
                    self._require_memory_read_access(connection, row)
                except BridgeError:
                    continue
                self._verify_memory_row(connection, row)
                if query and query not in str(row["title"]).lower() and query not in str(
                    row["body"]
                ).lower():
                    continue
                result = self._memory_result(row)
                result["superseded_by_memory_id"] = superseded_by.get(
                    str(row["memory_id"])
                )
                memories.append(result)
                if len(memories) >= limit:
                    break
        return {
            "memories": memories,
            "count": len(memories),
            "room_id": room_id,
            "visibility": visibility,
            "access_contract": "project + owner-private + explicitly selected room",
        }

    def read_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        memory_id = _require_identifier(args.get("memory_id"), "memory_id")
        with self._connect() as connection:
            row = self._memory_row(connection, memory_id)
            self._require_memory_read_access(connection, row)
            superseding = connection.execute(
                """SELECT memory_id FROM memories
                    WHERE scope=? AND supersedes_memory_id=?
                    ORDER BY created_utc DESC, memory_id DESC LIMIT 1""",
                (self.scope, memory_id),
            ).fetchone()
            result = self._memory_result(row)
            result["superseded_by_memory_id"] = (
                str(superseding["memory_id"]) if superseding else None
            )
            return result

    def brief_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Bind only applicable, visible, non-superseded records to one task."""

        task_id = _require_identifier(args.get("task_id"), "task_id")
        room_id = _optional_identifier(args.get("room_id"), "room_id")
        applicability = []
        for item in _json_list(args.get("applicability", []), "applicability"):
            value = _require_text(item, "applicability item", limit=200)
            if value not in applicability:
                applicability.append(value)
        if len(applicability) > 50:
            raise BridgeError("applicability exceeds 50 entries")
        created = utc_now()
        briefing_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if room_id:
                self._require_room_member(connection, room_id, self.agent_id)
                rows = connection.execute(
                    """SELECT * FROM memories
                        WHERE scope=? AND status='active'
                          AND (visibility='project' OR room_id=?)
                        ORDER BY created_utc, memory_id""",
                    (self.scope, room_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM memories
                        WHERE scope=? AND status='active' AND visibility='project'
                        ORDER BY created_utc, memory_id""",
                    (self.scope,),
                ).fetchall()
            visible: list[sqlite3.Row] = []
            for row in rows:
                try:
                    self._require_memory_read_access(connection, row)
                except BridgeError:
                    continue
                self._verify_memory_row(connection, row)
                visible.append(row)
            historical_superseders = connection.execute(
                """SELECT * FROM memories
                    WHERE scope=? AND supersedes_memory_id IS NOT NULL
                    ORDER BY created_utc, memory_id""",
                (self.scope,),
            ).fetchall()
            superseded_ids = set()
            for row in historical_superseders:
                try:
                    self._require_memory_read_access(connection, row)
                except BridgeError:
                    continue
                self._verify_memory_row(connection, row)
                superseded_ids.add(str(row["supersedes_memory_id"]))
            records = []
            bindings = []
            requested_applicability = set(applicability)
            for row in visible:
                if str(row["memory_id"]) in superseded_ids:
                    continue
                if str(row["record_type"]) == "DEPRECATED":
                    continue
                record_applicability = set(
                    json.loads(str(row["applicability_json"] or "[]"))
                )
                if record_applicability and not (
                    record_applicability & requested_applicability
                ):
                    continue
                result = self._memory_result(row)
                records.append(result)
                bindings.append(
                    {
                        "memory_id": str(row["memory_id"]),
                        "memory_sha256": str(row["memory_sha256"]),
                        "record_type": str(row["record_type"]),
                        "authority_id": str(row["authority_id"] or ""),
                    }
                )
            payload = {
                "scope": self.scope,
                "briefing_id": briefing_id,
                "task_id": task_id,
                "agent_id": self.agent_id,
                "room_id": room_id,
                "applicability": applicability,
                "memory_bindings": bindings,
                "created_utc": created,
            }
            digest = stable_sha256(payload)
            connection.execute(
                """INSERT INTO task_briefings(
                    scope, briefing_id, task_id, agent_id, room_id,
                    applicability_json, memory_bindings_json, created_utc,
                    briefing_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.scope,
                    briefing_id,
                    task_id,
                    self.agent_id,
                    room_id,
                    json.dumps(
                        applicability,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        bindings,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    created,
                    digest,
                ),
            )
            event = self._event(
                connection,
                "memory.task_briefed",
                {
                    "briefing_id": briefing_id,
                    "task_id": task_id,
                    "record_count": len(bindings),
                    "briefing_sha256": digest,
                },
                task_id,
            )
        return {
            **payload,
            "records": records,
            "briefing_sha256": digest,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def record_decision_conflict(self, args: dict[str, Any]) -> dict[str, Any]:
        """Record a review finding; never convert it into an automatic block."""

        task_id = _require_identifier(args.get("task_id"), "task_id")
        briefing_id = _require_identifier(args.get("briefing_id"), "briefing_id")
        summary = _require_text(args.get("summary"), "summary", limit=4_000)
        severity = str(args.get("severity") or "medium").strip().lower()
        if severity not in {"low", "medium", "high", "critical"}:
            raise BridgeError("severity must be low, medium, high or critical")
        memory_ids = []
        for item in _json_list(args.get("memory_ids", []), "memory_ids"):
            memory_id = _require_identifier(item, "memory_id")
            if memory_id not in memory_ids:
                memory_ids.append(memory_id)
        if not memory_ids:
            raise BridgeError("decision conflict requires at least one memory_id")
        finding_id = uuid.uuid4().hex
        created = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            briefing = connection.execute(
                """SELECT * FROM task_briefings
                    WHERE scope=? AND briefing_id=? AND task_id=?""",
                (self.scope, briefing_id, task_id),
            ).fetchone()
            if briefing is None:
                raise BridgeError("task briefing not found")
            briefing_payload = {
                "scope": briefing["scope"],
                "briefing_id": briefing["briefing_id"],
                "task_id": briefing["task_id"],
                "agent_id": briefing["agent_id"],
                "room_id": briefing["room_id"],
                "applicability": json.loads(briefing["applicability_json"]),
                "memory_bindings": json.loads(briefing["memory_bindings_json"]),
                "created_utc": briefing["created_utc"],
            }
            if stable_sha256(briefing_payload) != briefing["briefing_sha256"]:
                raise BridgeError("task briefing SHA-256 mismatch")
            bound_ids = {
                str(item["memory_id"])
                for item in briefing_payload["memory_bindings"]
            }
            if not set(memory_ids).issubset(bound_ids):
                raise BridgeError("decision conflict references an unbriefed memory")
            payload = {
                "scope": self.scope,
                "finding_id": finding_id,
                "task_id": task_id,
                "reviewer": self.agent_id,
                "briefing_id": briefing_id,
                "memory_ids": memory_ids,
                "summary": summary,
                "severity": severity,
                "status": "finding",
                "created_utc": created,
            }
            digest = stable_sha256(payload)
            connection.execute(
                """INSERT INTO decision_conflict_findings(
                    scope, finding_id, task_id, reviewer, briefing_id,
                    memory_ids_json, summary, severity, status, created_utc,
                    finding_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'finding', ?, ?)""",
                (
                    self.scope,
                    finding_id,
                    task_id,
                    self.agent_id,
                    briefing_id,
                    json.dumps(memory_ids, separators=(",", ":")),
                    summary,
                    severity,
                    created,
                    digest,
                ),
            )
            event = self._event(
                connection,
                "memory.decision_conflict_found",
                {
                    "finding_id": finding_id,
                    "task_id": task_id,
                    "briefing_id": briefing_id,
                    "severity": severity,
                    "status": "finding",
                    "finding_sha256": digest,
                },
                task_id,
            )
        return {
            **payload,
            "finding_sha256": digest,
            "audit_chain_sha256": event["chain_sha256"],
            "enforcement": "review-finding-only",
        }

    def revoke_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        memory_id = _require_identifier(args.get("memory_id"), "memory_id")
        reason = _require_text(args.get("reason"), "reason", limit=2_000)
        revoked = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._memory_row(connection, memory_id)
            if row["status"] != "active":
                raise BridgeError("memory is already revoked")
            visibility = str(row["visibility"])
            if visibility == "project":
                if self.agent_id != "human-operator":
                    raise BridgeError("only human-operator may revoke project memory")
            elif visibility == "private":
                if row["owner_agent_id"] != self.agent_id:
                    raise BridgeError("private memory belongs to another agent")
            elif row["owner_agent_id"] != self.agent_id:
                self._require_room_manager(connection, str(row["room_id"]))
            revocation = {
                "scope": self.scope,
                "memory_id": memory_id,
                "memory_sha256": str(row["memory_sha256"]),
                "revoked_by": self.agent_id,
                "revoked_utc": revoked,
                "reason": reason,
            }
            revocation_sha = stable_sha256(revocation)
            connection.execute(
                """UPDATE memories SET status='revoked', revoked_utc=?,
                   revocation_reason=?, revocation_sha256=?
                   WHERE scope=? AND memory_id=?""",
                (revoked, reason, revocation_sha, self.scope, memory_id),
            )
            event = self._event(
                connection,
                "memory.revoked",
                {
                    "memory_id": memory_id,
                    "memory_sha256": row["memory_sha256"],
                    "revocation_sha256": revocation_sha,
                },
            )
        return {
            **revocation,
            "status": "revoked",
            "revocation_sha256": revocation_sha,
            "audit_chain_sha256": event["chain_sha256"],
        }

    @staticmethod
    def _route_profile_identity_payload(
        row: sqlite3.Row | dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "scope": row["scope"],
            "route_id": row["route_id"],
            "agent_id": row["agent_id"],
            "client_name": row["client_name"],
            "provider_id": row["provider_id"],
            "model_id": row["model_id"],
            "response_model_id": row["response_model_id"],
            "inference_timeout_seconds": row["inference_timeout_seconds"],
            "reasoning_mode": row["reasoning_mode"],
            "route_class": row["route_class"],
            "enabled": bool(row["enabled"]),
        }

    @classmethod
    def _verified_route_profile_sha256(
        cls, row: sqlite3.Row | dict[str, Any]
    ) -> str:
        stored = row["profile_sha256"]
        if not isinstance(stored, str) or re.fullmatch(r"[0-9a-f]{64}", stored) is None:
            raise BridgeError("route profile identity SHA mismatch")
        identity = cls._route_profile_identity_payload(row)
        expected = stable_sha256(identity)
        if secrets.compare_digest(stored, expected):
            return stored

        # Schema v22 added an explicit route timeout. A NULL timeout preserves the
        # exact v21 identity, while older pre-v17 profiles may also omit the NULL
        # response-model field. Non-NULL values always require the current hash.
        if identity["inference_timeout_seconds"] is None:
            v21_identity = dict(identity)
            v21_identity.pop("inference_timeout_seconds")
            if secrets.compare_digest(stored, stable_sha256(v21_identity)):
                return stored
            if identity["response_model_id"] is None:
                legacy_identity = dict(v21_identity)
                legacy_identity.pop("response_model_id")
                if secrets.compare_digest(stored, stable_sha256(legacy_identity)):
                    return stored
        raise BridgeError("route profile identity SHA mismatch")

    def _resolve_route_request(
        self,
        connection: sqlite3.Connection,
        recipient: str,
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        route_profile_id = _optional_identifier(
            args.get("route_profile_id"), "route_profile_id"
        )
        route_profile_sha256: str | None = None
        requested = {
            "provider_id": _optional_identifier(
                args.get("requested_provider_id"), "requested_provider_id"
            ),
            "model_id": _optional_identifier(
                args.get("requested_model_id"), "requested_model_id"
            ),
            "reasoning_mode": _optional_identifier(
                args.get("requested_reasoning_mode"), "requested_reasoning_mode"
            ),
            "route_class": _optional_route_class(
                args.get("requested_route_class"), "requested_route_class"
            ),
        }
        if route_profile_id:
            row = connection.execute(
                """SELECT * FROM route_profiles
                   WHERE scope=? AND route_id=? AND enabled=1""",
                (self.scope, route_profile_id),
            ).fetchone()
            if row is None:
                raise BridgeError("route profile is not found or is disabled")
            route_profile_sha256 = self._verified_route_profile_sha256(row)
            if recipient != row["agent_id"]:
                raise BridgeError(
                    f"route profile {route_profile_id} targets {row['agent_id']}, not {recipient}"
                )
            for key in (
                "provider_id",
                "model_id",
                "reasoning_mode",
                "route_class",
            ):
                profile_value = row[key]
                if requested[key] and profile_value and requested[key] != profile_value:
                    raise BridgeError(
                        f"requested_{key} conflicts with route profile {route_profile_id}"
                    )
                requested[key] = requested[key] or profile_value
        if not route_profile_id and not any(requested.values()):
            return None
        if recipient == "*":
            raise BridgeError("model-routed messages require one explicit recipient")
        if requested["route_class"] is None:
            raise BridgeError(
                "routed messages require route_profile_id or requested_route_class"
            )
        request = {
            "route_profile_id": route_profile_id,
            "target_agent_id": recipient,
            "requested_provider_id": requested["provider_id"],
            "requested_model_id": requested["model_id"],
            "requested_reasoning_mode": requested["reasoning_mode"],
            "requested_route_class": requested["route_class"],
        }
        if route_profile_id:
            request["route_profile_sha256"] = route_profile_sha256
        return {**request, "route_request_sha256": stable_sha256(request)}

    @staticmethod
    def _route_request_from_row(
        row: sqlite3.Row | dict[str, Any],
    ) -> dict[str, Any] | None:
        row_keys = row.keys()
        route_profile_sha256 = (
            row["route_profile_sha256"]
            if "route_profile_sha256" in row_keys
            else None
        )
        route_fields = {
            "route_profile_id": row["route_profile_id"],
            "target_agent_id": row["recipient"],
            "requested_provider_id": row["requested_provider_id"],
            "requested_model_id": row["requested_model_id"],
            "requested_reasoning_mode": row["requested_reasoning_mode"],
            "requested_route_class": row["requested_route_class"],
        }
        if route_fields["route_profile_id"] or route_profile_sha256 is not None:
            route_fields["route_profile_sha256"] = route_profile_sha256
        if not row["route_request_sha256"] and not any(
            route_fields[key]
            for key in route_fields
            if key != "target_agent_id"
        ):
            return None
        return {
            **route_fields,
            "route_request_sha256": row["route_request_sha256"],
        }

    @staticmethod
    def _route_request_content_binding(
        route_request: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if route_request is None:
            return None
        # The request digest binds the profile digest while preserving legacy receipts.
        return {
            key: value
            for key, value in route_request.items()
            if key != "route_profile_sha256"
        }

    def _evaluate_route(
        self,
        row: sqlite3.Row | dict[str, Any],
        consumer: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        request = self._route_request_from_row(row)
        observed = {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "reasoning_mode": self.reasoning_mode,
            "route_class": self.route_class,
        }
        if request is None:
            return {"status": "not_requested", "observed": observed, "mismatches": []}
        mismatches = []
        if consumer != self.agent_id or request["target_agent_id"] != self.agent_id:
            mismatches.append("agent_id")
        request_content = {
            key: value
            for key, value in request.items()
            if key != "route_request_sha256"
        }
        expected_request_sha = stable_sha256(request_content)
        stored_request_sha = request["route_request_sha256"]
        if not isinstance(stored_request_sha, str) or not secrets.compare_digest(
            stored_request_sha, expected_request_sha
        ):
            mismatches.append("route_request_sha256")
        route_profile_id = request["route_profile_id"]
        route_profile_sha256 = request.get("route_profile_sha256")
        if route_profile_id:
            def profile_identity_matches(
                active_connection: sqlite3.Connection,
            ) -> bool:
                current = active_connection.execute(
                    """SELECT * FROM route_profiles
                       WHERE scope=? AND route_id=? AND enabled=1""",
                    (self.scope, route_profile_id),
                ).fetchone()
                if current is None:
                    return False
                try:
                    current_sha256 = self._verified_route_profile_sha256(current)
                except BridgeError:
                    return False
                return (
                    isinstance(route_profile_sha256, str)
                    and re.fullmatch(r"[0-9a-f]{64}", route_profile_sha256)
                    is not None
                    and secrets.compare_digest(route_profile_sha256, current_sha256)
                )

            if connection is None:
                with self._connect() as profile_connection:
                    profile_matches = profile_identity_matches(profile_connection)
            else:
                profile_matches = profile_identity_matches(connection)
            if not profile_matches:
                mismatches.append("route_profile_sha256")
        elif route_profile_sha256 is not None:
            mismatches.append("route_profile_sha256")
        expected_route_class = request["requested_route_class"]
        if (
            expected_route_class not in ROUTE_CLASSES
            or observed["route_class"] not in ROUTE_CLASSES
            or expected_route_class != observed["route_class"]
        ):
            mismatches.append("route_class")
        for requested_key, observed_key in (
            ("requested_provider_id", "provider_id"),
            ("requested_model_id", "model_id"),
            ("requested_reasoning_mode", "reasoning_mode"),
        ):
            expected = request[requested_key]
            if expected and expected != observed[observed_key]:
                mismatches.append(observed_key)
        return {
            "status": "verified" if not mismatches else "mismatch",
            "request": request,
            "observed": observed,
            "mismatches": mismatches,
        }

    def _reserve_message_storage(
        self,
        connection: sqlite3.Connection,
        *,
        rows: int,
        body_bytes: int,
    ) -> None:
        rows = int(rows)
        body_bytes = int(body_bytes)
        if rows < 1 or body_bytes < 0:
            raise BridgeError("message storage reservation is invalid")
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO scope_storage_usage(
                   scope, message_rows, message_bytes, updated_utc
               ) VALUES (?, 0, 0, ?)""",
            (self.scope, now),
        )
        updated = connection.execute(
            """UPDATE scope_storage_usage
                  SET message_rows=message_rows+?,
                      message_bytes=message_bytes+?, updated_utc=?
                WHERE scope=?
                  AND message_rows+?<=?
                  AND message_bytes+?<=?""",
            (
                rows,
                body_bytes,
                now,
                self.scope,
                rows,
                MAX_SCOPE_MESSAGE_ROWS,
                body_bytes,
                MAX_SCOPE_MESSAGE_BYTES,
            ),
        )
        if updated.rowcount != 1:
            raise BridgeError("durable message storage quota exceeded")

    @staticmethod
    def _serialized_message_bytes(content: Mapping[str, Any]) -> int:
        return len(
            json.dumps(
                dict(content),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def send_message(self, args: dict[str, Any]) -> dict[str, Any]:
        receipt_metadata = self._mcp_receipt_metadata(args, "send_message")
        room_id = _require_identifier(
            args.get("room_id", DEFAULT_ROOM_ID), "room_id"
        )
        raw_recipient = str(args.get("recipient") or "").strip()
        recipient = "*" if raw_recipient == "*" else _require_identifier(raw_recipient, "recipient")
        task_id = _require_identifier(args.get("task_id"), "task_id")
        subject = _require_text(args.get("subject"), "subject", limit=500)
        body = _require_text(args.get("body"), "body")
        priority = str(args.get("priority", "normal")).strip().lower()
        if priority not in {"low", "normal", "high", "critical"}:
            raise BridgeError("priority must be low, normal, high or critical")
        reply_to = str(args.get("reply_to") or "").strip() or None
        artifacts = self._clean_artifacts(
            args.get("artifact_paths", []),
            max_count=MAX_CHAT_ATTACHMENT_COUNT,
            reject_duplicates=True,
        )
        visibility = "room" if recipient == "*" else "direct"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._load_mcp_mutation_receipt_locked(
                connection, receipt_metadata
            )
            if replay is not None:
                return replay
            self._require_room_member(connection, room_id, self.agent_id)
            if room_id != DEFAULT_ROOM_ID and recipient != "*":
                if self._active_room_member(connection, room_id, recipient) is None:
                    raise BridgeError("recipient is not an active member of this room")
            if reply_to:
                parent = connection.execute(
                    "SELECT room_id FROM messages WHERE scope=? AND message_id=?",
                    (self.scope, reply_to),
                ).fetchone()
                if parent is None or parent["room_id"] != room_id:
                    raise BridgeError("reply_to must reference a message in the same room")
            route_request = self._resolve_route_request(connection, recipient, args)
            message_id = uuid.uuid4().hex
            created = utc_now()
            content = {
                "message_id": message_id,
                "scope": self.scope,
                "room_id": room_id,
                "task_id": task_id,
                "sender": self.agent_id,
                "recipient": recipient,
                "subject": subject,
                "body": body,
                "priority": priority,
                "reply_to": reply_to,
                "artifact_paths": artifacts,
                "route_request": self._route_request_content_binding(route_request),
                "visibility": visibility,
                "created_utc": created,
            }
            self._reserve_message_storage(
                connection,
                rows=1,
                body_bytes=self._serialized_message_bytes(content),
            )
            content_sha = stable_sha256(content)
            cursor = connection.execute(
                """INSERT INTO messages(
                    message_id, scope, room_id, task_id, sender, recipient, subject, body,
                    priority, reply_to, artifact_paths_json, route_profile_id,
                    route_profile_sha256, requested_provider_id, requested_model_id,
                    requested_reasoning_mode, requested_route_class,
                    route_request_sha256, visibility, created_utc,
                    acknowledged_utc, content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    message_id,
                    self.scope,
                    room_id,
                    task_id,
                    self.agent_id,
                    recipient,
                    subject,
                    body,
                    priority,
                    reply_to,
                    json.dumps(artifacts, ensure_ascii=False),
                    route_request["route_profile_id"] if route_request else None,
                    route_request.get("route_profile_sha256") if route_request else None,
                    route_request["requested_provider_id"] if route_request else None,
                    route_request["requested_model_id"] if route_request else None,
                    route_request["requested_reasoning_mode"] if route_request else None,
                    route_request["requested_route_class"] if route_request else None,
                    route_request["route_request_sha256"] if route_request else None,
                    visibility,
                    created,
                    content_sha,
                ),
            )
            self._event(
                connection,
                "message.sent",
                {
                    "message_id": message_id,
                    "room_id": room_id,
                    "sequence": cursor.lastrowid,
                    "content_sha256": content_sha,
                    "route_request_sha256": (
                        route_request["route_request_sha256"] if route_request else None
                    ),
                    "route_profile_sha256": (
                        route_request.get("route_profile_sha256")
                        if route_request
                        else None
                    ),
                },
                task_id,
            )
            result = {
                "message_id": message_id,
                "room_id": room_id,
                "sequence": cursor.lastrowid,
                "content_sha256": content_sha,
                "route_request": route_request,
                "route_status": "requested" if route_request else "not_requested",
                "created_utc": created,
            }
            self._store_mcp_mutation_receipt_locked(
                connection, receipt_metadata, result
            )
        return result

    def _send_room_fanout_locked(
        self,
        connection: sqlite3.Connection,
        *,
        room_id: str,
        task_id: str,
        subject: str,
        body: str,
        priority: str,
        artifacts: list[str],
        enforce_once_policy: bool = True,
    ) -> dict[str, Any]:
        """Insert one complete fan-out while the caller holds a write transaction."""
        self._require_room_member(connection, room_id, self.agent_id)
        if enforce_once_policy:
            policy = self._room_policy(connection, room_id)
            if str(policy["mode"]) != "once":
                raise BridgeError(
                    "direct room fanout requires automation mode once; "
                    "use post_room_message for off or discussion mode"
                )
        fanout_id = uuid.uuid4().hex
        created = utc_now()
        seats = connection.execute(
            """SELECT agent_id, route_profile_id
                 FROM room_memberships
                WHERE scope=? AND room_id=? AND status='active'
                  AND agent_id!=? AND agent_id!='human-operator'
                ORDER BY agent_id""",
            (self.scope, room_id, self.agent_id),
        ).fetchall()
        if not seats:
            raise BridgeError("room has no active Agent seats")
        if len(seats) > MAX_ROOM_FANOUT_RECIPIENTS:
            raise BridgeError("room fanout exceeds the active Agent seat limit")

        prepared: list[tuple[str, str, dict[str, Any]]] = []
        for seat in seats:
            agent_id = str(seat["agent_id"])
            route_profile_id = str(seat["route_profile_id"] or "")
            if not route_profile_id:
                raise BridgeError(
                    f"room seat {agent_id} has no route profile; fanout was not written"
                )
            route_request = self._resolve_route_request(
                connection,
                agent_id,
                {"route_profile_id": route_profile_id},
            )
            if route_request is None:
                raise BridgeError(f"room seat {agent_id} has no runnable route request")
            prepared.append((agent_id, route_profile_id, route_request))

        messages: list[dict[str, Any]] = []
        reserved_message_bytes = sum(
            self._serialized_message_bytes(
                {
                    "message_id": "0" * 32,
                    "scope": self.scope,
                    "room_id": room_id,
                    "task_id": task_id,
                    "sender": self.agent_id,
                    "recipient": agent_id,
                    "subject": subject,
                    "body": body,
                    "priority": priority,
                    "reply_to": None,
                    "artifact_paths": artifacts,
                    "route_request": self._route_request_content_binding(route_request),
                    "visibility": "room",
                    "created_utc": created,
                }
            )
            for agent_id, _route_profile_id, route_request in prepared
        )
        self._reserve_message_storage(
            connection,
            rows=len(prepared),
            body_bytes=reserved_message_bytes,
        )
        for agent_id, route_profile_id, route_request in prepared:
            message_id = uuid.uuid4().hex
            content = {
                "message_id": message_id,
                "scope": self.scope,
                "room_id": room_id,
                "task_id": task_id,
                "sender": self.agent_id,
                "recipient": agent_id,
                "subject": subject,
                "body": body,
                "priority": priority,
                "reply_to": None,
                "artifact_paths": artifacts,
                "route_request": self._route_request_content_binding(route_request),
                "visibility": "room",
                "created_utc": created,
            }
            content_sha = stable_sha256(content)
            cursor = connection.execute(
                """INSERT INTO messages(
                    message_id, scope, room_id, task_id, sender, recipient,
                    subject, body, priority, reply_to, artifact_paths_json,
                    route_profile_id, route_profile_sha256, requested_provider_id,
                    requested_model_id, requested_reasoning_mode,
                    requested_route_class, route_request_sha256, visibility, created_utc,
                    acknowledged_utc, content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'room', ?, NULL, ?)""",
                (
                    message_id,
                    self.scope,
                    room_id,
                    task_id,
                    self.agent_id,
                    agent_id,
                    subject,
                    body,
                    priority,
                    json.dumps(artifacts, ensure_ascii=False),
                    route_profile_id,
                    route_request["route_profile_sha256"],
                    route_request["requested_provider_id"],
                    route_request["requested_model_id"],
                    route_request["requested_reasoning_mode"],
                    route_request["requested_route_class"],
                    route_request["route_request_sha256"],
                    created,
                    content_sha,
                ),
            )
            messages.append(
                {
                    "agent_id": agent_id,
                    "message_id": message_id,
                    "sequence": int(cursor.lastrowid),
                    "route_profile_id": route_profile_id,
                    "route_profile_sha256": route_request["route_profile_sha256"],
                    "route_request_sha256": route_request["route_request_sha256"],
                    "content_sha256": content_sha,
                }
            )

        fanout = {
            "fanout_id": fanout_id,
            "scope": self.scope,
            "room_id": room_id,
            "task_id": task_id,
            "sender": self.agent_id,
            "recipients": messages,
            "created_utc": created,
        }
        fanout_sha = stable_sha256(fanout)
        event = self._event(
            connection,
            "message.room_fanout_sent",
            {
                "fanout_id": fanout_id,
                "fanout_sha256": fanout_sha,
                "room_id": room_id,
                "recipient_count": len(messages),
                "recipients": messages,
            },
            task_id,
        )
        return {
            **fanout,
            "fanout_count": len(messages),
            "fanout_sha256": fanout_sha,
            "content_sha256": fanout_sha,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def send_room_fanout(self, args: dict[str, Any]) -> dict[str, Any]:
        """Atomically route one room member message under the room's once policy."""
        receipt_metadata = self._mcp_receipt_metadata(args, "send_room_fanout")
        room_id = _require_identifier(args.get("room_id"), "room_id")
        task_id = _require_identifier(args.get("task_id"), "task_id")
        subject = _require_text(args.get("subject"), "subject", limit=500)
        body = _require_text(args.get("body"), "body")
        priority = str(args.get("priority", "normal")).strip().lower()
        if priority not in {"low", "normal", "high", "critical"}:
            raise BridgeError("priority must be low, normal, high or critical")
        artifacts = self._clean_artifacts(
            args.get("artifact_paths", []),
            max_count=MAX_CHAT_ATTACHMENT_COUNT,
            reject_duplicates=True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._load_mcp_mutation_receipt_locked(
                connection, receipt_metadata
            )
            if replay is not None:
                return replay
            result = self._send_room_fanout_locked(
                connection,
                room_id=room_id,
                task_id=task_id,
                subject=subject,
                body=body,
                priority=priority,
                artifacts=artifacts,
            )
            self._store_mcp_mutation_receipt_locked(
                connection, receipt_metadata, result
            )
            return result

    def _routed_room_seats(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        *,
        exclude_agent_id: str | None = None,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        params: list[Any] = [self.scope, room_id]
        exclusion = ""
        if exclude_agent_id:
            exclusion = "AND agent_id!=?"
            params.append(exclude_agent_id)
        rows = connection.execute(
            f"""SELECT agent_id, route_profile_id
                  FROM room_memberships
                 WHERE scope=? AND room_id=? AND status='active'
                   AND agent_id!='human-operator' {exclusion}
                 ORDER BY agent_id""",
            tuple(params),
        ).fetchall()
        if not rows:
            raise BridgeError("room has no active Agent seats")
        if len(rows) > MAX_ROOM_FANOUT_RECIPIENTS:
            raise BridgeError("room fanout exceeds the active Agent seat limit")
        prepared: list[tuple[str, str, dict[str, Any]]] = []
        for row in rows:
            agent_id = str(row["agent_id"])
            route_profile_id = str(row["route_profile_id"] or "")
            if not route_profile_id:
                raise BridgeError(
                    f"room seat {agent_id} has no route profile; room post was not written"
                )
            request = self._resolve_route_request(
                connection, agent_id, {"route_profile_id": route_profile_id}
            )
            if request is None:
                raise BridgeError(
                    f"room seat {agent_id} has no runnable route request"
                )
            prepared.append((agent_id, route_profile_id, request))
        return prepared

    def _insert_discussion_prompt(
        self,
        connection: sqlite3.Connection,
        *,
        room_id: str,
        task_id: str,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        priority: str,
        artifacts: list[str],
        route_profile_id: str,
        route_request: dict[str, Any],
        discussion_id: str,
        discussion_round: int,
        created: str,
    ) -> dict[str, Any]:
        message_id = uuid.uuid4().hex
        content = {
            "message_id": message_id,
            "scope": self.scope,
            "room_id": room_id,
            "task_id": task_id,
            "sender": sender,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "priority": priority,
            "reply_to": None,
            "artifact_paths": artifacts,
            "route_request": self._route_request_content_binding(route_request),
            "discussion_id": discussion_id,
            "discussion_round": discussion_round,
            "discussion_role": "prompt",
            "visibility": "room",
            "created_utc": created,
        }
        self._reserve_message_storage(
            connection,
            rows=1,
            body_bytes=self._serialized_message_bytes(content),
        )
        content_sha = stable_sha256(content)
        cursor = connection.execute(
            """INSERT INTO messages(
                   message_id, scope, room_id, task_id, sender, recipient,
                   subject, body, priority, reply_to, artifact_paths_json,
                   route_profile_id, route_profile_sha256, requested_provider_id,
                   requested_model_id,
                   requested_reasoning_mode, requested_route_class,
                   route_request_sha256, discussion_id, discussion_round,
                   discussion_role, visibility, created_utc, acknowledged_utc, content_sha256
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, 'prompt', 'room', ?, NULL, ?)""",
            (
                message_id,
                self.scope,
                room_id,
                task_id,
                sender,
                recipient,
                subject,
                body,
                priority,
                json.dumps(artifacts, ensure_ascii=False),
                route_profile_id,
                route_request["route_profile_sha256"],
                route_request["requested_provider_id"],
                route_request["requested_model_id"],
                route_request["requested_reasoning_mode"],
                route_request["requested_route_class"],
                route_request["route_request_sha256"],
                discussion_id,
                discussion_round,
                created,
                content_sha,
            ),
        )
        return {
            "agent_id": recipient,
            "message_id": message_id,
            "sequence": int(cursor.lastrowid),
            "route_profile_id": route_profile_id,
            "route_profile_sha256": route_request["route_profile_sha256"],
            "route_request_sha256": route_request["route_request_sha256"],
            "content_sha256": content_sha,
        }

    @staticmethod
    def _discussion_row_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "scope": str(row["scope"]),
            "discussion_id": str(row["discussion_id"]),
            "room_id": str(row["room_id"]),
            "task_id": str(row["task_id"]),
            "subject": str(row["subject"]),
            "starter_agent_id": str(row["starter_agent_id"]),
            "status": str(row["status"]),
            "current_round": int(row["current_round"]),
            "processed_round": int(row["processed_round"]),
            "max_rounds": int(row["max_rounds"]),
            "max_messages": int(row["max_messages"]),
            "stagnation_rounds": int(row["stagnation_rounds"]),
            "message_count": int(row["message_count"]),
            "stagnation_count": int(row["stagnation_count"]),
            "last_round_digest": row["last_round_digest"],
            "stop_reason": row["stop_reason"],
        }

    def _store_discussion_state(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        *,
        now: str,
        **changes: Any,
    ) -> tuple[dict[str, Any], str]:
        """Write one complete discussion state while keeping its SHA binding valid."""
        payload = {**self._discussion_row_payload(row), **changes}
        discussion_sha = stable_sha256(payload)
        connection.execute(
            """UPDATE room_discussions
                  SET status=?, current_round=?, processed_round=?, max_rounds=?,
                      max_messages=?, stagnation_rounds=?, message_count=?,
                      stagnation_count=?, last_round_digest=?, stop_reason=?,
                      updated_utc=?, discussion_sha256=?
                WHERE scope=? AND discussion_id=?""",
            (
                payload["status"],
                payload["current_round"],
                payload["processed_round"],
                payload["max_rounds"],
                payload["max_messages"],
                payload["stagnation_rounds"],
                payload["message_count"],
                payload["stagnation_count"],
                payload["last_round_digest"],
                payload["stop_reason"],
                now,
                discussion_sha,
                self.scope,
                payload["discussion_id"],
            ),
        )
        return payload, discussion_sha

    def _cancel_discussion_dispatches(
        self,
        connection: sqlite3.Connection,
        discussion_id: str,
        *,
        error_code: str,
        now: str,
    ) -> int:
        """Fence every unfinished prompt in a terminal discussion."""
        rows = connection.execute(
            """SELECT d.* FROM message_dispatches d
                 JOIN messages m
                   ON m.scope=d.scope AND m.message_id=d.message_id
                WHERE m.scope=? AND m.discussion_id=?
                  AND m.discussion_role='prompt'
                  AND d.status IN ('claimed', 'retryable')
                ORDER BY m.sequence""",
            (self.scope, discussion_id),
        ).fetchall()
        for row in rows:
            failed = {
                **self._dispatch_payload(row),
                "status": "failed",
                "claimed_session_id": None,
                "lease_token_sha256": None,
                "lease_expires_epoch": None,
                "updated_utc": now,
                "completed_utc": now,
                "reply_message_id": None,
                "inference_receipt_sha256": None,
                "error_code": error_code,
            }
            dispatch_sha = stable_sha256(failed)
            connection.execute(
                """UPDATE message_dispatches
                      SET status='failed', claimed_session_id=NULL,
                          lease_token_sha256=NULL, lease_expires_epoch=NULL,
                          updated_utc=?, completed_utc=?, reply_message_id=NULL,
                          inference_receipt_sha256=NULL, error_code=?,
                          dispatch_sha256=?
                    WHERE scope=? AND message_id=? AND agent_id=?""",
                (
                    now,
                    now,
                    error_code,
                    dispatch_sha,
                    self.scope,
                    row["message_id"],
                    row["agent_id"],
                ),
            )
        return len(rows)

    @staticmethod
    def _guided_participant_binding(
        participants: Iterable[Mapping[str, Any]],
    ) -> list[tuple[str, str, str]]:
        if isinstance(participants, (str, bytes, Mapping)):
            raise BridgeError("bound discussion participant binding is invalid")
        expected: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for participant in participants:
            if not isinstance(participant, Mapping):
                raise BridgeError("bound discussion participant binding is invalid")
            agent_id = str(participant.get("agent_id") or "")
            route_profile_id = str(participant.get("route_profile_id") or "")
            route_profile_sha256 = str(
                participant.get("route_profile_sha256") or ""
            )
            if (
                not SAFE_ID.fullmatch(agent_id)
                or not SAFE_ID.fullmatch(route_profile_id)
                or not re.fullmatch(r"[0-9a-f]{64}", route_profile_sha256)
                or agent_id in seen
            ):
                raise BridgeError("bound discussion participant binding is invalid")
            seen.add(agent_id)
            expected.append((agent_id, route_profile_id, route_profile_sha256))
        if not expected:
            raise BridgeError("bound discussion participant binding is invalid")
        return sorted(expected)

    def _matching_discussions_for_operation(
        self,
        connection: sqlite3.Connection,
        *,
        room_id: str,
        task_id: str,
        prompt_sha256: str,
        participants: Iterable[Mapping[str, Any]],
    ) -> list[sqlite3.Row]:
        """Return every discussion with the exact immutable prompt and route binding."""

        if not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256):
            raise BridgeError("bound discussion prompt SHA-256 is invalid")
        room_id = _require_identifier(room_id, "bound discussion room id")
        task_id = _require_identifier(task_id, "bound discussion task id")
        expected = self._guided_participant_binding(participants)

        rows = connection.execute(
            """SELECT * FROM room_discussions
                WHERE scope=? AND room_id=? AND task_id=?
                ORDER BY created_utc, discussion_id""",
            (self.scope, room_id, task_id),
        ).fetchall()
        matching: list[sqlite3.Row] = []
        for discussion in rows:
            prompts = connection.execute(
                """SELECT recipient, route_profile_id, route_profile_sha256, body
                    FROM messages
                    WHERE scope=? AND discussion_id=? AND discussion_round=1
                      AND discussion_role='prompt'
                    ORDER BY recipient""",
                (self.scope, discussion["discussion_id"]),
            ).fetchall()
            observed = sorted(
                (
                    str(prompt["recipient"]),
                    str(prompt["route_profile_id"] or ""),
                    str(prompt["route_profile_sha256"] or ""),
                )
                for prompt in prompts
            )
            if observed != expected or any(
                hashlib.sha256(str(prompt["body"]).encode("utf-8")).hexdigest()
                != prompt_sha256
                for prompt in prompts
            ):
                continue
            if (
                stable_sha256(self._discussion_row_payload(discussion))
                != discussion["discussion_sha256"]
            ):
                raise BridgeError("bound discussion SHA-256 mismatch")
            matching.append(discussion)
        return matching

    def _bound_discussion_for_operation(
        self,
        connection: sqlite3.Connection,
        *,
        discussion_id: str,
        room_id: str,
        task_id: str,
        prompt_sha256: str,
        participants: Iterable[Mapping[str, Any]],
    ) -> sqlite3.Row | None:
        """Resolve only the discussion ID durably stored on the operation."""

        discussion_id = _require_identifier(
            discussion_id, "bound discussion id"
        )
        row = connection.execute(
            "SELECT * FROM room_discussions WHERE scope=? AND discussion_id=?",
            (self.scope, discussion_id),
        ).fetchone()
        if row is None:
            return None
        if str(row["room_id"]) != room_id or str(row["task_id"]) != task_id:
            raise BridgeError("bound discussion source identity does not match")
        matching = self._matching_discussions_for_operation(
            connection,
            room_id=room_id,
            task_id=task_id,
            prompt_sha256=prompt_sha256,
            participants=participants,
        )
        if not any(
            str(candidate["discussion_id"]) == discussion_id
            for candidate in matching
        ):
            raise BridgeError("bound discussion prompt or route binding does not match")
        return row

    def _discussion_participants(
        self,
        connection: sqlite3.Connection,
        discussion_id: str,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Return the immutable round-one participant and route snapshot."""
        rows = connection.execute(
            """SELECT * FROM messages
                 WHERE scope=? AND discussion_id=? AND discussion_round=1
                   AND discussion_role='prompt'
                 ORDER BY recipient""",
            (self.scope, discussion_id),
        ).fetchall()
        if not rows:
            raise BridgeError("discussion participant snapshot is missing")
        participants: list[tuple[str, str, dict[str, Any]]] = []
        for row in rows:
            agent_id = str(row["recipient"])
            route_id = str(row["route_profile_id"] or "")
            if not route_id:
                raise BridgeError(
                    f"discussion participant {agent_id} has no bound route profile"
                )
            membership = connection.execute(
                """SELECT route_profile_id FROM room_memberships
                     WHERE scope=? AND room_id=? AND agent_id=? AND status='active'""",
                (self.scope, row["room_id"], agent_id),
            ).fetchone()
            if membership is None or str(membership["route_profile_id"] or "") != route_id:
                raise BridgeError(
                    f"discussion participant {agent_id} is unavailable or changed route"
                )
            bound = self._route_request_from_row(row)
            if bound is None:
                raise BridgeError(
                    f"discussion participant {agent_id} has no bound route request"
                )
            bound_payload = {key: value for key, value in bound.items() if key != "route_request_sha256"}
            if stable_sha256(bound_payload) != str(bound["route_request_sha256"]):
                raise BridgeError(
                    f"discussion participant {agent_id} route request SHA mismatch"
                )
            current = self._resolve_route_request(
                connection, agent_id, {"route_profile_id": route_id}
            )
            if current != bound:
                raise BridgeError(
                    f"discussion participant {agent_id} route profile changed"
                )
            participants.append((agent_id, route_id, bound))
        return participants

    def post_room_message(self, args: dict[str, Any]) -> dict[str, Any]:
        """Post once according to the room policy and optionally open a discussion."""
        receipt_metadata = self._mcp_receipt_metadata(args, "post_room_message")
        room_id = _require_identifier(args.get("room_id"), "room_id")
        task_id = _require_identifier(args.get("task_id"), "task_id")
        subject = _require_text(args.get("subject"), "subject", limit=500)
        body = _require_text(args.get("body"), "body")
        priority = str(args.get("priority", "normal")).strip().lower()
        if priority not in {"low", "normal", "high", "critical"}:
            raise BridgeError("priority must be low, normal, high or critical")
        artifacts = self._clean_artifacts(
            args.get("artifact_paths", []),
            max_count=MAX_CHAT_ATTACHMENT_COUNT,
            reject_duplicates=True,
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._load_mcp_mutation_receipt_locked(
                connection, receipt_metadata
            )
            if replay is not None:
                return replay
            self._require_room_member(connection, room_id, self.agent_id)
            policy = self._room_policy(connection, room_id)
            mode = str(policy["mode"])
            if mode == "off":
                message_id = uuid.uuid4().hex
                content = {
                    "message_id": message_id,
                    "scope": self.scope,
                    "room_id": room_id,
                    "task_id": task_id,
                    "sender": self.agent_id,
                    "recipient": self.agent_id,
                    "subject": subject,
                    "body": body,
                    "priority": priority,
                    "reply_to": None,
                    "artifact_paths": artifacts,
                    "route_request": None,
                    "visibility": "room",
                    "created_utc": now,
                }
                self._reserve_message_storage(
                    connection,
                    rows=1,
                    body_bytes=self._serialized_message_bytes(content),
                )
                content_sha = stable_sha256(content)
                cursor = connection.execute(
                    """INSERT INTO messages(
                           message_id, scope, room_id, task_id, sender, recipient,
                           subject, body, priority, reply_to, artifact_paths_json,
                           route_profile_id, route_profile_sha256,
                           requested_provider_id, requested_model_id,
                           requested_reasoning_mode, requested_route_class,
                           route_request_sha256, visibility, created_utc, acknowledged_utc,
                           content_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL,
                                 NULL, NULL, NULL, NULL, NULL, 'room', ?, NULL, ?)""",
                    (
                        message_id,
                        self.scope,
                        room_id,
                        task_id,
                        self.agent_id,
                        self.agent_id,
                        subject,
                        body,
                        priority,
                        json.dumps(artifacts, ensure_ascii=False),
                        now,
                        content_sha,
                    ),
                )
                event = self._event(
                    connection,
                    "message.room_history_posted",
                    {
                        "message_id": message_id,
                        "room_id": room_id,
                        "sequence": int(cursor.lastrowid),
                        "content_sha256": content_sha,
                    },
                    task_id,
                )
                result = {
                    "message_id": message_id,
                    "room_id": room_id,
                    "sequence": int(cursor.lastrowid),
                    "content_sha256": content_sha,
                    "route_request": None,
                    "route_status": "not_requested",
                    "created_utc": now,
                    "automation_mode": mode,
                    "fanout_count": 0,
                    "audit_chain_sha256": event["chain_sha256"],
                }
                self._store_mcp_mutation_receipt_locked(
                    connection, receipt_metadata, result
                )
                return result
            if mode == "once":
                receipt = self._send_room_fanout_locked(
                    connection,
                    room_id=room_id,
                    task_id=task_id,
                    subject=subject,
                    body=body,
                    priority=priority,
                    artifacts=artifacts,
                    enforce_once_policy=False,
                )
                result = {**receipt, "automation_mode": mode}
                self._store_mcp_mutation_receipt_locked(
                    connection, receipt_metadata, result
                )
                return result

            if mode != "discussion":
                raise BridgeError("unsupported room automation mode")
            discussion_id = uuid.uuid4().hex
            prepared = self._routed_room_seats(
                connection, room_id, exclude_agent_id=self.agent_id
            )
            minimum_complete_round_messages = 2 * len(prepared)
            if int(policy["max_messages"]) < minimum_complete_round_messages:
                raise BridgeError(
                    "max_messages cannot fit one complete discussion round for all "
                    f"{len(prepared)} routed Agent seats; requires at least "
                    f"{minimum_complete_round_messages}"
                )
            superseded = connection.execute(
                """SELECT * FROM room_discussions
                    WHERE scope=? AND room_id=?
                      AND status IN ('active', 'paused', 'waiting_human')""",
                (self.scope, room_id),
            ).fetchall()
            for previous in superseded:
                cancelled = self._cancel_discussion_dispatches(
                    connection,
                    str(previous["discussion_id"]),
                    error_code="discussion_superseded",
                    now=now,
                )
                self._store_discussion_state(
                    connection,
                    previous,
                    now=now,
                    status="stopped",
                    stop_reason="superseded_by_new_post",
                )
                self._event(
                    connection,
                    "discussion.superseded",
                    {
                        "discussion_id": previous["discussion_id"],
                        "replacement_discussion_id": discussion_id,
                        "cancelled_dispatch_count": cancelled,
                    },
                    str(previous["task_id"]),
                )
            messages = [
                self._insert_discussion_prompt(
                    connection,
                    room_id=room_id,
                    task_id=task_id,
                    sender=self.agent_id,
                    recipient=agent_id,
                    subject=subject,
                    body=body,
                    priority=priority,
                    artifacts=artifacts,
                    route_profile_id=route_id,
                    route_request=request,
                    discussion_id=discussion_id,
                    discussion_round=1,
                    created=now,
                )
                for agent_id, route_id, request in prepared
            ]
            discussion = {
                "scope": self.scope,
                "discussion_id": discussion_id,
                "room_id": room_id,
                "task_id": task_id,
                "subject": subject,
                "starter_agent_id": self.agent_id,
                "status": "active",
                "current_round": 1,
                "processed_round": 0,
                "max_rounds": int(policy["max_rounds"]),
                "max_messages": int(policy["max_messages"]),
                "stagnation_rounds": int(policy["stagnation_rounds"]),
                "message_count": len(messages),
                "stagnation_count": 0,
                "last_round_digest": None,
                "stop_reason": None,
            }
            discussion_sha = stable_sha256(discussion)
            connection.execute(
                """INSERT INTO room_discussions(
                       scope, discussion_id, room_id, task_id, subject,
                       starter_agent_id, status, current_round, processed_round,
                       max_rounds, max_messages, stagnation_rounds, message_count,
                       stagnation_count, last_round_digest, stop_reason,
                       created_utc, updated_utc, discussion_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 0, ?, ?, ?, ?, 0,
                             NULL, NULL, ?, ?, ?)""",
                (
                    self.scope,
                    discussion_id,
                    room_id,
                    task_id,
                    subject,
                    self.agent_id,
                    discussion["max_rounds"],
                    discussion["max_messages"],
                    discussion["stagnation_rounds"],
                    len(messages),
                    now,
                    now,
                    discussion_sha,
                ),
            )
            event = self._event(
                connection,
                "discussion.started",
                {
                    "discussion_id": discussion_id,
                    "discussion_sha256": discussion_sha,
                    "room_id": room_id,
                    "round": 1,
                    "message_count": len(messages),
                    "policy_sha256": policy["policy_sha256"],
                },
                task_id,
            )
            result = {
                "automation_mode": "discussion",
                "discussion_id": discussion_id,
                "room_id": room_id,
                "task_id": task_id,
                "round": 1,
                "fanout_count": len(messages),
                "recipients": messages,
                "discussion_sha256": discussion_sha,
                "content_sha256": discussion_sha,
                "created_utc": now,
                "audit_chain_sha256": event["chain_sha256"],
            }
            self._store_mcp_mutation_receipt_locked(
                connection, receipt_metadata, result
            )
        return result

    def _consumer_cursor(
        self, connection: sqlite3.Connection, channel: str, consumer: str
    ) -> int:
        row = connection.execute(
            """SELECT position FROM consumer_cursors
               WHERE scope=? AND channel=? AND consumer=?""",
            (self.scope, channel, consumer),
        ).fetchone()
        return int(row["position"]) if row else 0

    def _bound_consumer(self, args: dict[str, Any]) -> str:
        """Return the runtime identity and reject caller-selected impersonation."""
        requested = args.get("agent_id")
        if requested is None:
            return self.agent_id
        consumer = _require_identifier(requested, "agent_id")
        if consumer != self.agent_id:
            raise BridgeError("agent_id must match the current runtime identity")
        return consumer

    @staticmethod
    def _room_context_dedup_key(row: Mapping[str, Any]) -> str:
        """Collapse route-specific copies of one room fan-out root message."""

        if row.get("reply_to") is None:
            payload = {
                "sender": row.get("sender"),
                "task_id": row.get("task_id"),
                "subject": row.get("subject"),
                "body": row.get("body"),
                "created_utc": row.get("created_utc"),
                "artifact_paths_json": row.get("artifact_paths_json"),
                "discussion_id": row.get("discussion_id"),
                "discussion_round": row.get("discussion_round"),
                "discussion_role": row.get("discussion_role"),
            }
        else:
            payload = {
                "sender": row.get("sender"),
                "recipient": row.get("recipient"),
                "subject": row.get("subject"),
                "body": row.get("body"),
                "reply_to": row.get("reply_to"),
                "created_utc": row.get("created_utc"),
                "discussion_id": row.get("discussion_id"),
                "discussion_round": row.get("discussion_round"),
                "discussion_role": row.get("discussion_role"),
            }
        return stable_sha256(payload)

    @staticmethod
    def _room_context_message(row: Mapping[str, Any], char_limit: int) -> tuple[dict[str, str], bool]:
        sender = str(row.get("sender") or "unknown")
        subject = str(row.get("subject") or "").strip()
        body = str(row.get("body") or "").strip()
        heading = f"[Same-room history | sender={sender}"
        if subject:
            heading += f" | subject={subject}"
        heading += "]\n"
        available = max(0, char_limit - len(heading))
        truncated = len(body) > available
        if truncated:
            marker = "\n[history message truncated]"
            body = body[: max(0, available - len(marker))] + marker
        return {
            "role": "user" if sender == HUMAN_OPERATOR_ID else "assistant",
            "content": heading + body,
        }, truncated

    def room_prompt_context(
        self,
        message_id: str,
        *,
        max_messages: int = DEFAULT_ROOM_CONTEXT_MESSAGES,
        max_chars: int = DEFAULT_ROOM_CONTEXT_CHARS,
    ) -> dict[str, Any]:
        """Return a bounded, de-duplicated same-room history snapshot for one dispatch.

        Imported-history rooms are not stored in ``messages`` and therefore cannot
        leak into a live room prompt.  The current root message seeds the de-dup set
        so earlier route-specific copies from the same fan-out are also omitted.
        """

        message_id = _require_identifier(message_id, "message_id")
        max_messages = max(0, min(int(max_messages), MAX_ROOM_CONTEXT_MESSAGES))
        max_chars = max(0, min(int(max_chars), MAX_ROOM_CONTEXT_CHARS))
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM messages WHERE scope=? AND message_id=?",
                (self.scope, message_id),
            ).fetchone()
            if current is None or current["recipient"] not in {self.agent_id, "*"}:
                raise BridgeError("source message is not addressed to this agent")
            self._require_room_member(connection, current["room_id"], self.agent_id)
            scan_limit = max(256, min(2_000, max(1, max_messages) * 16))
            rows = connection.execute(
                """SELECT * FROM messages m
                     WHERE m.scope=? AND m.room_id=? AND m.sequence<?
                       AND (m.recipient=? OR m.recipient='*' OR m.visibility='room')
                     ORDER BY m.sequence DESC LIMIT ?""",
                (
                    self.scope,
                    current["room_id"],
                    int(current["sequence"]),
                    self.agent_id,
                    scan_limit,
                ),
            ).fetchall()

        current_item = dict(current)
        seen = {self._room_context_dedup_key(current_item)}
        selected_desc: list[tuple[int, dict[str, str]]] = []
        duplicate_rows_omitted = 0
        omitted_for_limit = 0
        any_message_truncated = False
        used_chars = 0
        for row in rows:
            row_item = dict(row)
            key = self._room_context_dedup_key(row_item)
            if key in seen:
                duplicate_rows_omitted += 1
                continue
            seen.add(key)
            if len(selected_desc) >= max_messages or used_chars >= max_chars:
                omitted_for_limit += 1
                continue
            remaining = max_chars - used_chars
            per_message_limit = min(MAX_ROOM_CONTEXT_MESSAGE_CHARS, remaining)
            if per_message_limit <= 0:
                omitted_for_limit += 1
                continue
            rendered, truncated = self._room_context_message(
                row_item, per_message_limit
            )
            any_message_truncated = any_message_truncated or truncated
            used_chars += len(rendered["content"])
            selected_desc.append((int(row["sequence"]), rendered))

        selected_desc.reverse()
        messages = [item for _, item in selected_desc]
        sequences = [sequence for sequence, _ in selected_desc]
        context_sha = stable_sha256(messages)
        receipt = {
            "schema": "peerbridge.room-prompt-context.v1",
            "scope": self.scope,
            "room_id": str(current["room_id"]),
            "agent_id": self.agent_id,
            "source_message_id": message_id,
            "history_message_count": len(messages),
            "history_chars": used_chars,
            "history_sha256": context_sha,
            "duplicate_rows_omitted": duplicate_rows_omitted,
            "visibility_filter": (
                "recipient_self_or_broadcast_or_explicit_room_visibility"
            ),
            "history_truncated": bool(
                any_message_truncated
                or omitted_for_limit
                or len(rows) == scan_limit
            ),
            "oldest_sequence": sequences[0] if sequences else None,
            "newest_sequence": sequences[-1] if sequences else None,
        }
        receipt["receipt_sha256"] = stable_sha256(receipt)
        return {"messages": messages, "receipt": receipt}

    def poll_messages(self, args: dict[str, Any]) -> dict[str, Any]:
        room_id = _require_identifier(
            args.get("room_id", DEFAULT_ROOM_ID), "room_id"
        )
        consumer = self._bound_consumer(args)
        limit = max(1, min(int(args.get("limit", 50)), 500))
        include_sent = bool(args.get("include_sent", False))
        with self._connect() as connection:
            self._require_room_member(connection, room_id, consumer)
            channel = f"messages:{room_id}"
            stored_cursor = self._consumer_cursor(connection, channel, consumer)
            cursor = int(args.get("after_cursor", stored_cursor))
            sender_clause = " OR m.sender=?" if include_sent else ""
            params: list[Any] = [
                consumer,
                self.scope,
                room_id,
                cursor,
                consumer,
            ]
            if include_sent:
                params.append(consumer)
            params.append(limit)
            rows = connection.execute(
                f"""SELECT m.*, CASE WHEN r.message_id IS NULL THEN 0 ELSE 1 END AS acknowledged
                    FROM messages m
                    LEFT JOIN message_receipts r
                      ON r.scope=m.scope AND r.message_id=m.message_id AND r.agent_id=?
                    WHERE m.scope=? AND m.room_id=? AND m.sequence>?
                      AND (m.recipient=? OR m.recipient='*'{sender_clause})
                    ORDER BY m.sequence ASC LIMIT ?""",
                tuple(params),
            ).fetchall()
            messages = []
            for row in rows:
                item = dict(row)
                item["artifact_paths"] = json.loads(item.pop("artifact_paths_json"))
                item["acknowledged"] = bool(item["acknowledged"])
                item["route_request"] = self._route_request_from_row(item)
                item["route_evaluation"] = self._evaluate_route(
                    item, consumer, connection=connection
                )
                messages.append(item)
        return {
            "messages": messages,
            "room_id": room_id,
            "count": len(messages),
            "stored_cursor": stored_cursor,
            "requested_cursor": cursor,
            "next_cursor": messages[-1]["sequence"] if messages else cursor,
        }

    def ack_message(self, args: dict[str, Any]) -> dict[str, Any]:
        message_id = _require_identifier(args.get("message_id"), "message_id")
        consumer = self._bound_consumer(args)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT sequence, room_id, recipient, route_profile_id,
                          route_profile_sha256,
                          requested_provider_id, requested_model_id,
                          requested_reasoning_mode, requested_route_class,
                          route_request_sha256
                   FROM messages WHERE scope=? AND message_id=?""",
                (self.scope, message_id),
            ).fetchone()
            if row is None or row["recipient"] not in {consumer, "*"}:
                raise BridgeError("message is not addressed to this consumer")
            room_id = _require_identifier(row["room_id"], "room_id")
            self._require_room_member(connection, room_id, consumer)
            acknowledged = utc_now()
            route_evaluation = self._evaluate_route(
                row, consumer, connection=connection
            )
            if route_evaluation["status"] == "mismatch":
                raise BridgeError(
                    "route request is not satisfied by this runtime identity: "
                    + ", ".join(route_evaluation["mismatches"])
                )
            connection.execute(
                """INSERT OR IGNORE INTO message_receipts(
                    scope, message_id, agent_id, acknowledged_utc
                ) VALUES (?, ?, ?, ?)""",
                (self.scope, message_id, consumer, acknowledged),
            )
            route_receipt = None
            if route_evaluation["status"] == "verified":
                route_receipt_content = {
                    "scope": self.scope,
                    "message_id": message_id,
                    "agent_id": consumer,
                    "session_id": self.session_id,
                    "observed_provider_id": self.provider_id,
                    "observed_model_id": self.model_id,
                    "observed_reasoning_mode": self.reasoning_mode,
                    "observed_route_class": self.route_class,
                    "route_status": "verified",
                    "acknowledged_utc": acknowledged,
                    "route_request_sha256": row["route_request_sha256"],
                }
                receipt_sha = stable_sha256(route_receipt_content)
                connection.execute(
                    """INSERT OR IGNORE INTO message_route_receipts(
                        scope, message_id, agent_id, session_id,
                        observed_provider_id, observed_model_id,
                        observed_reasoning_mode, observed_route_class, route_status,
                        acknowledged_utc, receipt_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?)""",
                    (
                        self.scope,
                        message_id,
                        consumer,
                        self.session_id,
                        self.provider_id,
                        self.model_id,
                        self.reasoning_mode,
                        self.route_class,
                        acknowledged,
                        receipt_sha,
                    ),
                )
                stored_receipt = connection.execute(
                    """SELECT * FROM message_route_receipts
                       WHERE scope=? AND message_id=? AND agent_id=?""",
                    (self.scope, message_id, consumer),
                ).fetchone()
                route_receipt = dict(stored_receipt) if stored_receipt else None
            if row["recipient"] != "*":
                connection.execute(
                    "UPDATE messages SET acknowledged_utc=COALESCE(acknowledged_utc, ?) WHERE message_id=?",
                    (acknowledged, message_id),
                )
            channel = f"messages:{room_id}"
            current = self._consumer_cursor(connection, channel, consumer)
            eligible = connection.execute(
                """SELECT
                          MIN(CASE WHEN r.message_id IS NULL THEN m.sequence END)
                            AS first_unacknowledged,
                          MAX(m.sequence) AS maximum_sequence
                   FROM messages m
                   LEFT JOIN message_receipts r
                     ON r.scope=m.scope AND r.message_id=m.message_id AND r.agent_id=?
                   WHERE m.scope=? AND m.room_id=? AND m.sequence>?
                     AND (m.recipient=? OR m.recipient='*')""",
                (consumer, self.scope, room_id, current, consumer),
            ).fetchone()
            first_unacknowledged = eligible["first_unacknowledged"]
            if first_unacknowledged is None:
                advanced = int(eligible["maximum_sequence"] or current)
            else:
                preceding = connection.execute(
                    """SELECT MAX(m.sequence)
                       FROM messages m
                       JOIN message_receipts r
                         ON r.scope=m.scope AND r.message_id=m.message_id
                        AND r.agent_id=?
                       WHERE m.scope=? AND m.room_id=?
                         AND m.sequence>? AND m.sequence<?
                         AND (m.recipient=? OR m.recipient='*')""",
                    (
                        consumer,
                        self.scope,
                        room_id,
                        current,
                        int(first_unacknowledged),
                        consumer,
                    ),
                ).fetchone()[0]
                advanced = int(preceding or current)
            connection.execute(
                """INSERT INTO consumer_cursors(scope, channel, consumer, position, updated_utc)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(scope, channel, consumer) DO UPDATE SET
                     position=excluded.position, updated_utc=excluded.updated_utc""",
                (self.scope, channel, consumer, advanced, utc_now()),
            )
            self._event(
                connection,
                "message.acknowledged",
                {
                    "message_id": message_id,
                    "room_id": room_id,
                    "consumer": consumer,
                    "cursor": advanced,
                    "route_status": route_evaluation["status"],
                    "route_receipt_sha256": (
                        route_receipt["receipt_sha256"] if route_receipt else None
                    ),
                },
            )
        return {
            "message_id": message_id,
            "room_id": room_id,
            "acknowledged": True,
            "consumer": consumer,
            "cursor": advanced,
            "route_evaluation": route_evaluation,
            "route_receipt": route_receipt,
        }

    @staticmethod
    def _dispatch_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "scope": row["scope"],
            "message_id": row["message_id"],
            "agent_id": row["agent_id"],
            "status": row["status"],
            "claimed_session_id": row["claimed_session_id"],
            "lease_token_sha256": row["lease_token_sha256"],
            "lease_expires_epoch": row["lease_expires_epoch"],
            "attempt_count": int(row["attempt_count"]),
            "claimed_utc": row["claimed_utc"],
            "updated_utc": row["updated_utc"],
            "completed_utc": row["completed_utc"],
            "reply_message_id": row["reply_message_id"],
            "inference_receipt_sha256": row["inference_receipt_sha256"],
            "error_code": row["error_code"],
        }

    @classmethod
    def _public_dispatch(cls, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = cls._dispatch_payload(row)
        item.pop("lease_token_sha256", None)
        item["dispatch_sha256"] = row["dispatch_sha256"]
        return item

    def _require_dispatch_lease(
        self,
        connection: sqlite3.Connection,
        message_id: str,
        lease_token: Any,
        *,
        allow_completed: bool = False,
    ) -> sqlite3.Row:
        token = str(lease_token or "")
        if not token:
            raise BridgeError("lease_token is required")
        row = connection.execute(
            """SELECT * FROM message_dispatches
               WHERE scope=? AND message_id=? AND agent_id=?""",
            (self.scope, message_id, self.agent_id),
        ).fetchone()
        if row is None:
            raise BridgeError("message dispatch is not claimed by this agent")
        if allow_completed and row["status"] == "completed":
            if row["claimed_session_id"] != self.session_id:
                raise BridgeError("message dispatch was completed by another session")
        elif row["status"] != "claimed":
            raise BridgeError("message dispatch has no active lease")
        if row["claimed_session_id"] != self.session_id:
            raise BridgeError("message dispatch is claimed by another session")
        if not secrets.compare_digest(
            str(row["lease_token_sha256"] or ""),
            sha256_bytes(token.encode("utf-8")),
        ):
            raise BridgeError("message dispatch lease token does not match")
        if (
            row["status"] == "claimed"
            and row["lease_expires_epoch"] is not None
            and float(row["lease_expires_epoch"]) <= time.time()
        ):
            raise BridgeError("message dispatch lease has expired")
        return row

    def claim_message_dispatch(self, args: dict[str, Any]) -> dict[str, Any]:
        """Claim one addressed message without allowing duplicate active workers."""
        requested_message_id = _optional_identifier(
            args.get("message_id"), "message_id"
        )
        requested_room_id = _optional_identifier(args.get("room_id"), "room_id")
        requested_route_profile_id = _optional_identifier(
            args.get("route_profile_id"), "route_profile_id"
        )
        require_route = bool(args.get("require_route", False))
        lease_seconds = max(
            30,
            min(
                int(args.get("lease_seconds", DEFAULT_DISPATCH_LEASE_SECONDS)),
                MAX_LEASE_SECONDS,
            ),
        )
        max_attempts = max(
            1, min(int(args.get("max_attempts", MAX_DISPATCH_ATTEMPTS)), 100)
        )
        now_epoch = time.time()
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            where = [
                "m.scope=?",
                "(m.recipient=? OR m.recipient='*')",
                "m.sender!=?",
                "m.reply_to IS NULL",
                "r.message_id IS NULL",
                "(m.discussion_id IS NULL OR EXISTS ("
                "SELECT 1 FROM room_discussions rd "
                "WHERE rd.scope=m.scope AND rd.discussion_id=m.discussion_id "
                "AND rd.status='active'))",
            ]
            if require_route:
                where.append("m.route_request_sha256 IS NOT NULL")
            params: list[Any] = [
                self.agent_id,
                self.agent_id,
                self.scope,
                self.agent_id,
                self.agent_id,
            ]
            if requested_message_id:
                where.append("m.message_id=?")
                params.append(requested_message_id)
            if requested_room_id:
                where.append("m.room_id=?")
                params.append(requested_room_id)
            if requested_route_profile_id:
                where.append("m.route_profile_id=?")
                params.append(requested_route_profile_id)
            rows = connection.execute(
                f"""SELECT m.*, d.status AS dispatch_status,
                            d.claimed_session_id AS dispatch_session_id,
                            d.lease_expires_epoch AS dispatch_lease_expires_epoch,
                            d.attempt_count AS dispatch_attempt_count,
                            s.not_before_epoch AS dispatch_retry_not_before_epoch,
                            s.error_code AS dispatch_retry_error_code,
                            s.created_utc AS dispatch_retry_created_utc,
                            s.schedule_sha256 AS dispatch_retry_schedule_sha256
                     FROM messages m
                     LEFT JOIN message_receipts r
                       ON r.scope=m.scope AND r.message_id=m.message_id AND r.agent_id=?
                     LEFT JOIN message_dispatches d
                       ON d.scope=m.scope AND d.message_id=m.message_id AND d.agent_id=?
                     LEFT JOIN message_dispatch_retry_schedules s
                       ON s.scope=d.scope AND s.message_id=d.message_id
                      AND s.agent_id=d.agent_id AND s.attempt_count=d.attempt_count
                     WHERE {' AND '.join(where)}
                     ORDER BY m.sequence ASC LIMIT 500""",
                tuple(params),
            ).fetchall()

            selected: sqlite3.Row | None = None
            for candidate in rows:
                try:
                    self._require_room_member(
                        connection, candidate["room_id"], self.agent_id
                    )
                except BridgeError:
                    continue
                if (
                    candidate["route_profile_id"]
                    and requested_message_id
                    and not requested_route_profile_id
                ):
                    raise BridgeError(
                        "route_profile_id is required to claim this routed message"
                    )
                route = self._evaluate_route(
                    candidate, self.agent_id, connection=connection
                )
                if route["status"] == "mismatch":
                    if requested_message_id:
                        raise BridgeError(
                            "route request is not satisfied by this runtime identity: "
                            + ", ".join(route["mismatches"])
                        )
                    continue
                status = candidate["dispatch_status"]
                attempts = int(candidate["dispatch_attempt_count"] or 0)
                if status in {"completed", "failed"}:
                    continue
                schedule_sha = str(candidate["dispatch_retry_schedule_sha256"] or "")
                if schedule_sha:
                    retry_schedule = {
                        "scope": self.scope,
                        "message_id": candidate["message_id"],
                        "agent_id": self.agent_id,
                        "attempt_count": attempts,
                        "not_before_epoch": float(
                            candidate["dispatch_retry_not_before_epoch"]
                        ),
                        "error_code": candidate["dispatch_retry_error_code"],
                        "created_utc": candidate["dispatch_retry_created_utc"],
                    }
                    if stable_sha256(retry_schedule) != schedule_sha:
                        raise BridgeError("message dispatch retry schedule SHA mismatch")
                    if (
                        status == "retryable"
                        and retry_schedule["not_before_epoch"] > now_epoch
                    ):
                        continue
                if status == "claimed" and float(
                    candidate["dispatch_lease_expires_epoch"] or 0
                ) > now_epoch:
                    continue
                if attempts >= max_attempts:
                    continue
                selected = candidate
                break

            if selected is None:
                if requested_message_id and not rows:
                    raise BridgeError("message is not available to this agent")
                return {"claimed": False, "message": None, "dispatch": None}

            token = secrets.token_urlsafe(32)
            token_sha = sha256_bytes(token.encode("utf-8"))
            expires = now_epoch + lease_seconds
            attempts = int(selected["dispatch_attempt_count"] or 0) + 1
            dispatch = {
                "scope": self.scope,
                "message_id": selected["message_id"],
                "agent_id": self.agent_id,
                "status": "claimed",
                "claimed_session_id": self.session_id,
                "lease_token_sha256": token_sha,
                "lease_expires_epoch": expires,
                "attempt_count": attempts,
                "claimed_utc": now,
                "updated_utc": now,
                "completed_utc": None,
                "reply_message_id": None,
                "inference_receipt_sha256": None,
                "error_code": None,
            }
            dispatch_sha = stable_sha256(dispatch)
            connection.execute(
                """INSERT INTO message_dispatches(
                       scope, message_id, agent_id, status, claimed_session_id,
                       lease_token_sha256, lease_expires_epoch, attempt_count,
                       claimed_utc, updated_utc, completed_utc, reply_message_id,
                       inference_receipt_sha256, error_code, dispatch_sha256
                   ) VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
                   ON CONFLICT(scope, message_id, agent_id) DO UPDATE SET
                       status='claimed', claimed_session_id=excluded.claimed_session_id,
                       lease_token_sha256=excluded.lease_token_sha256,
                       lease_expires_epoch=excluded.lease_expires_epoch,
                       attempt_count=excluded.attempt_count,
                       claimed_utc=excluded.claimed_utc, updated_utc=excluded.updated_utc,
                       completed_utc=NULL, reply_message_id=NULL,
                       inference_receipt_sha256=NULL, error_code=NULL,
                       dispatch_sha256=excluded.dispatch_sha256""",
                (
                    self.scope,
                    selected["message_id"],
                    self.agent_id,
                    self.session_id,
                    token_sha,
                    expires,
                    attempts,
                    now,
                    now,
                    dispatch_sha,
                ),
            )
            event = self._event(
                connection,
                "message.dispatch_claimed",
                {
                    "message_id": selected["message_id"],
                    "room_id": selected["room_id"],
                    "attempt_count": attempts,
                    "lease_expires_epoch": expires,
                    "dispatch_sha256": dispatch_sha,
                },
                selected["task_id"],
            )
            message = dict(selected)
            for key in tuple(message):
                if key.startswith("dispatch_"):
                    message.pop(key)
            message["artifact_paths"] = json.loads(message.pop("artifact_paths_json"))
            message["route_request"] = self._route_request_from_row(message)
            message["route_evaluation"] = self._evaluate_route(
                message, self.agent_id, connection=connection
            )
            return {
                "claimed": True,
                "lease_token": token,
                "message": message,
                "dispatch": {
                    **self._public_dispatch({**dispatch, "dispatch_sha256": dispatch_sha}),
                    "audit_chain_sha256": event["chain_sha256"],
                },
            }

    def record_trusted_inference_receipt(
        self, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Bind one runner receipt to the exact active dispatch attempt.

        This method is intentionally not exposed as an MCP tool. The mailbox
        supervisor calls it only after a provider runner returns.
        """
        message_id = _require_identifier(args.get("message_id"), "message_id")
        lease_token = str(args.get("lease_token") or "")
        body = _require_text(args.get("body"), "body")
        receipt = args.get("receipt")
        assistant_message = args.get("assistant_message")
        if not isinstance(receipt, Mapping):
            raise BridgeError("receipt must be an object")
        if not isinstance(assistant_message, Mapping):
            raise BridgeError("assistant_message must be an object")
        connection_id = _optional_identifier(
            args.get("connection_id"), "connection_id"
        )
        raw_connection_sha = str(args.get("connection_sha256") or "").strip()
        connection_sha = (
            _require_sha256(raw_connection_sha, "connection_sha256")
            if raw_connection_sha
            else None
        )
        if (connection_id is None) != (connection_sha is None):
            raise BridgeError(
                "connection_id and connection_sha256 must be supplied together"
            )
        execution_route_profile_id = _optional_identifier(
            args.get("execution_route_profile_id"),
            "execution_route_profile_id",
        )
        raw_execution_profile_sha = str(
            args.get("execution_route_profile_sha256") or ""
        ).strip()
        execution_route_profile_sha = (
            _require_sha256(
                raw_execution_profile_sha,
                "execution_route_profile_sha256",
            )
            if raw_execution_profile_sha
            else None
        )
        if (execution_route_profile_id is None) != (
            execution_route_profile_sha is None
        ):
            raise BridgeError(
                "execution route profile id and sha256 must be supplied together"
            )

        try:
            receipt_usage = usage_from_receipt(receipt)
        except UsageError as exc:
            raise BridgeError(str(exc)) from None
        usage_sha = stable_sha256(receipt_usage)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            dispatch = self._require_dispatch_lease(
                connection, message_id, lease_token
            )
            source = connection.execute(
                "SELECT * FROM messages WHERE scope=? AND message_id=?",
                (self.scope, message_id),
            ).fetchone()
            if source is None or source["recipient"] not in {self.agent_id, "*"}:
                raise BridgeError("source message is not addressed to this agent")
            self._require_room_member(connection, source["room_id"], self.agent_id)
            route_evaluation = self._evaluate_route(
                source, self.agent_id, connection=connection
            )
            if route_evaluation["status"] == "mismatch":
                raise BridgeError(
                    "route request is not satisfied by this runtime identity: "
                    + ", ".join(route_evaluation["mismatches"])
                )

            response_model_id = self.model_id
            expected_receipt_schema = "peerbridge.openai-compatible-run.v1"
            source_route_profile_id = source["route_profile_id"]
            source_route_profile_sha = source["route_profile_sha256"]
            if execution_route_profile_id is None and source_route_profile_id:
                execution_route_profile_id = source_route_profile_id
                execution_route_profile_sha = source_route_profile_sha
            if source_route_profile_id and (
                execution_route_profile_id != source_route_profile_id
                or execution_route_profile_sha != source_route_profile_sha
            ):
                raise BridgeError(
                    "execution route profile does not match the source request"
                )
            if execution_route_profile_id:
                profile = connection.execute(
                    """SELECT * FROM route_profiles
                       WHERE scope=? AND route_id=? AND agent_id=? AND enabled=1""",
                    (self.scope, execution_route_profile_id, self.agent_id),
                ).fetchone()
                if (
                    profile is None
                    or profile["profile_sha256"] != execution_route_profile_sha
                    or profile["provider_id"] != self.provider_id
                    or profile["model_id"] != self.model_id
                    or profile["reasoning_mode"] != self.reasoning_mode
                    or profile["route_class"] != self.route_class
                ):
                    raise BridgeError("execution route profile is no longer exact")
                response_model_id = profile["response_model_id"] or profile["model_id"]
            if connection_id is not None:
                if execution_route_profile_id is None:
                    raise BridgeError(
                        "routed inference receipt is missing its execution profile"
                    )
                provider_connection = connection.execute(
                    """SELECT * FROM provider_connections
                       WHERE scope=? AND connection_id=? AND enabled=1""",
                    (self.scope, connection_id),
                ).fetchone()
                bound_provider_id = (
                    provider_connection["provider_id"]
                    if provider_connection is not None
                    else None
                )
                if (
                    provider_connection is not None
                    and not bound_provider_id
                    and provider_connection["secret_backend"] == "cc-switch"
                ):
                    bound_provider_id = provider_connection["connection_id"]
                if (
                    provider_connection is None
                    or provider_connection["connection_sha256"] != connection_sha
                    or bound_provider_id != self.provider_id
                    or provider_connection["route_class"] != self.route_class
                ):
                    raise BridgeError("provider connection binding does not match")
                expected_receipt_schema = {
                    "windows-credential-manager": "peerbridge.openai-compatible-run.v1",
                    "native-acp": "peerbridge.acpx-inference-receipt.v1",
                    "cc-switch": "peerbridge.ccswitch-inference-receipt.v1",
                }[provider_connection["secret_backend"]]

            expected_route = {
                "route_profile_id": execution_route_profile_id,
                "route_profile_sha256": execution_route_profile_sha,
                "route_class": self.route_class,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "response_model_id": response_model_id,
                "reasoning_mode": self.reasoning_mode,
                "connection_id": connection_id,
                "connection_sha256": connection_sha,
                "room_id": source["room_id"],
                "session_id": self.session_id,
            }
            try:
                validated = validate_inference_receipt(
                    receipt,
                    message_id=message_id,
                    assistant_message=assistant_message,
                    reply_body=body,
                    expected_route=expected_route,
                )
            except InferenceReceiptError as exc:
                raise BridgeError(str(exc)) from None
            if validated["receipt_schema"] != expected_receipt_schema:
                raise BridgeError(
                    "provider receipt schema does not match the bound secret backend"
                )

            lease_token_sha = sha256_bytes(lease_token.encode("utf-8"))
            existing = connection.execute(
                """SELECT * FROM trusted_inference_receipts
                   WHERE scope=? AND message_id=? AND agent_id=? AND attempt_count=?""",
                (
                    self.scope,
                    message_id,
                    self.agent_id,
                    int(dispatch["attempt_count"]),
                ),
            ).fetchone()
            if existing is not None:
                if (
                    existing["session_id"] != self.session_id
                    or existing["lease_token_sha256"] != lease_token_sha
                    or existing["inference_receipt_sha256"]
                    != validated["inference_receipt_sha256"]
                    or existing["reply_body_sha256"]
                    != validated["reply_body_sha256"]
                    or existing["inference_usage_sha256"] != usage_sha
                    or existing["binding_sha256"]
                    != stable_sha256(trusted_inference_receipt_payload(existing))
                ):
                    raise BridgeError(
                        "dispatch attempt already has a different trusted receipt"
                    )
                return {
                    "recorded": True,
                    "idempotent_replay": True,
                    "inference_receipt_sha256": existing[
                        "inference_receipt_sha256"
                    ],
                    "binding_sha256": existing["binding_sha256"],
                }

            binding = {
                "scope": self.scope,
                "message_id": message_id,
                "agent_id": self.agent_id,
                "session_id": self.session_id,
                "attempt_count": int(dispatch["attempt_count"]),
                "lease_token_sha256": lease_token_sha,
                "source_content_sha256": source["content_sha256"],
                "source_route_request_sha256": source["route_request_sha256"],
                **validated,
                "inference_usage_sha256": usage_sha,
                "route_profile_id": execution_route_profile_id,
                "route_profile_sha256": execution_route_profile_sha,
                "connection_id": connection_id,
                "connection_sha256": connection_sha,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "response_model_id": response_model_id,
                "reasoning_mode": self.reasoning_mode,
                "route_class": self.route_class,
                "room_id": source["room_id"],
                "recorded_utc": now,
            }
            binding.pop("expected_route_sha256", None)
            binding_sha = stable_sha256(binding)
            connection.execute(
                """INSERT INTO trusted_inference_receipts(
                       scope, message_id, agent_id, session_id, attempt_count,
                       lease_token_sha256, source_content_sha256,
                       source_route_request_sha256, receipt_schema,
                       inference_receipt_sha256, assistant_message_sha256,
                       assistant_content_sha256, reply_body_sha256,
                       inference_usage_sha256, route_profile_id,
                       route_profile_sha256, connection_id, connection_sha256,
                       provider_id, model_id, response_model_id, reasoning_mode,
                       route_class, room_id, recorded_utc, binding_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(binding[key] for key in TRUSTED_INFERENCE_RECEIPT_PAYLOAD_FIELDS)
                + (binding_sha,),
            )
            event = self._event(
                connection,
                "message.inference_receipt_trusted",
                {
                    "message_id": message_id,
                    "agent_id": self.agent_id,
                    "attempt_count": int(dispatch["attempt_count"]),
                    "inference_receipt_sha256": validated[
                        "inference_receipt_sha256"
                    ],
                    "binding_sha256": binding_sha,
                },
                source["task_id"],
            )
        return {
            "recorded": True,
            "idempotent_replay": False,
            "inference_receipt_sha256": validated["inference_receipt_sha256"],
            "binding_sha256": binding_sha,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def complete_message_dispatch(self, args: dict[str, Any]) -> dict[str, Any]:
        """Atomically create one reply and mark its source message completed."""
        message_id = _require_identifier(args.get("message_id"), "message_id")
        lease_token = str(args.get("lease_token") or "")
        body = _require_text(args.get("body"), "body")
        inference_receipt_sha = _require_sha256(
            args.get("inference_receipt_sha256"), "inference_receipt_sha256"
        )
        try:
            inference_usage = validate_usage(
                args.get("inference_usage") or unavailable_usage()
            )
        except UsageError as exc:
            raise BridgeError(str(exc)) from None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            dispatch_row = self._require_dispatch_lease(
                connection, message_id, lease_token, allow_completed=True
            )
            if dispatch_row["status"] == "completed":
                return {
                    "completed": True,
                    "idempotent_replay": True,
                    "reply_message_id": dispatch_row["reply_message_id"],
                    "dispatch": self._public_dispatch(dispatch_row),
                }
            source = connection.execute(
                "SELECT * FROM messages WHERE scope=? AND message_id=?",
                (self.scope, message_id),
            ).fetchone()
            if source is None or source["recipient"] not in {self.agent_id, "*"}:
                raise BridgeError("source message is not addressed to this agent")
            if source["discussion_id"]:
                discussion = connection.execute(
                    """SELECT status FROM room_discussions
                         WHERE scope=? AND discussion_id=?""",
                    (self.scope, source["discussion_id"]),
                ).fetchone()
                if discussion is None or str(discussion["status"]) not in {
                    "active",
                    "paused",
                }:
                    raise BridgeError(
                        "discussion is no longer active; reply completion is fenced"
                    )
            self._require_room_member(connection, source["room_id"], self.agent_id)
            route_evaluation = self._evaluate_route(
                source, self.agent_id, connection=connection
            )
            if route_evaluation["status"] == "mismatch":
                raise BridgeError(
                    "route request is not satisfied by this runtime identity: "
                    + ", ".join(route_evaluation["mismatches"])
                )
            trusted_receipt = connection.execute(
                """SELECT * FROM trusted_inference_receipts
                   WHERE scope=? AND message_id=? AND agent_id=?
                     AND session_id=? AND attempt_count=?
                     AND lease_token_sha256=? AND inference_receipt_sha256=?""",
                (
                    self.scope,
                    message_id,
                    self.agent_id,
                    self.session_id,
                    int(dispatch_row["attempt_count"]),
                    sha256_bytes(lease_token.encode("utf-8")),
                    inference_receipt_sha,
                ),
            ).fetchone()
            if trusted_receipt is None:
                raise BridgeError(
                    "trusted inference receipt binding is required before completion"
                )
            trusted_binding_sha = stable_sha256(
                trusted_inference_receipt_payload(trusted_receipt)
            )
            trusted_profile = None
            if trusted_receipt["route_profile_id"]:
                trusted_profile = connection.execute(
                    """SELECT * FROM route_profiles
                       WHERE scope=? AND route_id=? AND agent_id=? AND enabled=1""",
                    (
                        self.scope,
                        trusted_receipt["route_profile_id"],
                        self.agent_id,
                    ),
                ).fetchone()
            source_profile_matches = (
                source["route_profile_id"] is None
                or (
                    trusted_receipt["route_profile_id"]
                    == source["route_profile_id"]
                    and trusted_receipt["route_profile_sha256"]
                    == source["route_profile_sha256"]
                )
            )
            trusted_profile_matches = (
                trusted_receipt["route_profile_id"] is None
                or (
                    trusted_profile is not None
                    and trusted_profile["profile_sha256"]
                    == trusted_receipt["route_profile_sha256"]
                    and trusted_profile["provider_id"] == self.provider_id
                    and trusted_profile["model_id"] == self.model_id
                    and trusted_profile["reasoning_mode"] == self.reasoning_mode
                    and trusted_profile["route_class"] == self.route_class
                )
            )
            if (
                trusted_receipt["binding_sha256"] != trusted_binding_sha
                or trusted_receipt["source_content_sha256"]
                != source["content_sha256"]
                or trusted_receipt["source_route_request_sha256"]
                != source["route_request_sha256"]
                or trusted_receipt["reply_body_sha256"]
                != sha256_bytes(body.encode("utf-8"))
                or trusted_receipt["inference_usage_sha256"]
                != stable_sha256(inference_usage)
                or not source_profile_matches
                or not trusted_profile_matches
                or trusted_receipt["provider_id"] != self.provider_id
                or trusted_receipt["model_id"] != self.model_id
                or trusted_receipt["reasoning_mode"] != self.reasoning_mode
                or trusted_receipt["route_class"] != self.route_class
                or trusted_receipt["room_id"] != source["room_id"]
            ):
                raise BridgeError("trusted inference receipt binding does not match")
            subject = str(args.get("subject") or f"Re: {source['subject']}").strip()
            subject = _require_text(subject, "subject", limit=500)
            created = utc_now()
            reply_id = uuid.uuid4().hex
            visibility = (
                "room" if str(source["visibility"] or "direct") == "room" else "direct"
            )
            content = {
                "message_id": reply_id,
                "scope": self.scope,
                "room_id": source["room_id"],
                "task_id": source["task_id"],
                "sender": self.agent_id,
                "recipient": source["sender"],
                "subject": subject,
                "body": body,
                "priority": "normal",
                "reply_to": message_id,
                "artifact_paths": [],
                "route_request": None,
                "discussion_id": source["discussion_id"],
                "discussion_round": source["discussion_round"],
                "discussion_role": (
                    "response" if source["discussion_id"] else None
                ),
                "visibility": visibility,
                "created_utc": created,
            }
            self._reserve_message_storage(
                connection,
                rows=1,
                body_bytes=self._serialized_message_bytes(content),
            )
            content_sha = stable_sha256(content)
            cursor = connection.execute(
                """INSERT INTO messages(
                       message_id, scope, room_id, task_id, sender, recipient,
                       subject, body, priority, reply_to, artifact_paths_json,
                       route_profile_id, route_profile_sha256,
                       requested_provider_id, requested_model_id,
                       requested_reasoning_mode, requested_route_class,
                       route_request_sha256, discussion_id, discussion_round,
                       discussion_role, visibility, created_utc, acknowledged_utc, content_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'normal', ?, '[]',
                             NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    reply_id,
                    self.scope,
                    source["room_id"],
                    source["task_id"],
                    self.agent_id,
                    source["sender"],
                    subject,
                    body,
                    message_id,
                    source["discussion_id"],
                    source["discussion_round"],
                    "response" if source["discussion_id"] else None,
                    visibility,
                    created,
                    content_sha,
                ),
            )
            connection.execute(
                """INSERT OR IGNORE INTO message_receipts(
                       scope, message_id, agent_id, acknowledged_utc
                   ) VALUES (?, ?, ?, ?)""",
                (self.scope, message_id, self.agent_id, created),
            )
            route_receipt_sha = None
            if route_evaluation["status"] == "verified":
                route_receipt_content = {
                    "scope": self.scope,
                    "message_id": message_id,
                    "agent_id": self.agent_id,
                    "session_id": self.session_id,
                    "observed_provider_id": self.provider_id,
                    "observed_model_id": self.model_id,
                    "observed_reasoning_mode": self.reasoning_mode,
                    "observed_route_class": self.route_class,
                    "route_status": "verified",
                    "acknowledged_utc": created,
                    "route_request_sha256": source["route_request_sha256"],
                }
                route_receipt_sha = stable_sha256(route_receipt_content)
                connection.execute(
                    """INSERT OR IGNORE INTO message_route_receipts(
                           scope, message_id, agent_id, session_id,
                           observed_provider_id, observed_model_id,
                           observed_reasoning_mode, observed_route_class,
                           route_status, acknowledged_utc, receipt_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?)""",
                    (
                        self.scope,
                        message_id,
                        self.agent_id,
                        self.session_id,
                        self.provider_id,
                        self.model_id,
                        self.reasoning_mode,
                        self.route_class,
                        created,
                        route_receipt_sha,
                    ),
                )
            if source["recipient"] != "*":
                connection.execute(
                    """UPDATE messages
                       SET acknowledged_utc=COALESCE(acknowledged_utc, ?)
                       WHERE scope=? AND message_id=?""",
                    (created, self.scope, message_id),
                )
            channel = f"messages:{source['room_id']}"
            current = self._consumer_cursor(connection, channel, self.agent_id)
            eligible = connection.execute(
                """SELECT m.sequence,
                          CASE WHEN r.message_id IS NULL THEN 0 ELSE 1 END AS acknowledged
                   FROM messages m
                   LEFT JOIN message_receipts r
                     ON r.scope=m.scope AND r.message_id=m.message_id AND r.agent_id=?
                   WHERE m.scope=? AND m.room_id=? AND m.sequence>?
                     AND (m.recipient=? OR m.recipient='*')
                   ORDER BY m.sequence ASC""",
                (self.agent_id, self.scope, source["room_id"], current, self.agent_id),
            ).fetchall()
            advanced = current
            for item in eligible:
                if not item["acknowledged"]:
                    break
                advanced = int(item["sequence"])
            connection.execute(
                """INSERT INTO consumer_cursors(scope, channel, consumer, position, updated_utc)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(scope, channel, consumer) DO UPDATE SET
                     position=excluded.position, updated_utc=excluded.updated_utc""",
                (self.scope, channel, self.agent_id, advanced, created),
            )
            completed = {
                **self._dispatch_payload(dispatch_row),
                "status": "completed",
                "lease_expires_epoch": None,
                "updated_utc": created,
                "completed_utc": created,
                "reply_message_id": reply_id,
                "inference_receipt_sha256": inference_receipt_sha,
                "error_code": None,
            }
            dispatch_sha = stable_sha256(completed)
            connection.execute(
                """UPDATE message_dispatches
                   SET status='completed', lease_expires_epoch=NULL,
                       updated_utc=?, completed_utc=?, reply_message_id=?,
                       inference_receipt_sha256=?, error_code=NULL,
                       dispatch_sha256=?
                   WHERE scope=? AND message_id=? AND agent_id=?""",
                (
                    created,
                    created,
                    reply_id,
                    inference_receipt_sha,
                    dispatch_sha,
                    self.scope,
                    message_id,
                    self.agent_id,
                ),
            )
            usage_content = {
                "scope": self.scope,
                "message_id": message_id,
                "agent_id": self.agent_id,
                "reply_message_id": reply_id,
                "route_profile_id": source["route_profile_id"],
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "reasoning_mode": self.reasoning_mode,
                "route_class": self.route_class,
                "usage_status": inference_usage["status"],
                "usage_source": inference_usage["source"],
                "input_tokens": inference_usage["input_tokens"],
                "output_tokens": inference_usage["output_tokens"],
                "total_tokens": inference_usage["total_tokens"],
                "cached_input_tokens": inference_usage["cached_input_tokens"],
                "reasoning_tokens": inference_usage["reasoning_tokens"],
                "field_reported_calls": inference_usage["field_reported_calls"],
                "reported_calls": inference_usage["reported_calls"],
                "total_calls": inference_usage["total_calls"],
                "total_tokens_derived": bool(
                    inference_usage["total_tokens_derived"]
                ),
                "recorded_utc": created,
                "inference_receipt_sha256": inference_receipt_sha,
            }
            usage_sha = stable_sha256(usage_content)
            connection.execute(
                """INSERT INTO inference_usage(
                       scope, message_id, agent_id, reply_message_id,
                       route_profile_id, provider_id, model_id, reasoning_mode,
                       route_class, usage_status, usage_source, input_tokens,
                       output_tokens, total_tokens, cached_input_tokens,
                       reasoning_tokens, input_tokens_reported_calls,
                       output_tokens_reported_calls, total_tokens_reported_calls,
                       cached_input_tokens_reported_calls,
                       reasoning_tokens_reported_calls, reported_calls, total_calls,
                       total_tokens_derived, recorded_utc,
                       inference_receipt_sha256, usage_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.scope,
                    message_id,
                    self.agent_id,
                    reply_id,
                    source["route_profile_id"],
                    self.provider_id,
                    self.model_id,
                    self.reasoning_mode,
                    self.route_class,
                    inference_usage["status"],
                    inference_usage["source"],
                    inference_usage["input_tokens"],
                    inference_usage["output_tokens"],
                    inference_usage["total_tokens"],
                    inference_usage["cached_input_tokens"],
                    inference_usage["reasoning_tokens"],
                    inference_usage["field_reported_calls"]["input_tokens"],
                    inference_usage["field_reported_calls"]["output_tokens"],
                    inference_usage["field_reported_calls"]["total_tokens"],
                    inference_usage["field_reported_calls"]["cached_input_tokens"],
                    inference_usage["field_reported_calls"]["reasoning_tokens"],
                    inference_usage["reported_calls"],
                    inference_usage["total_calls"],
                    int(bool(inference_usage["total_tokens_derived"])),
                    created,
                    inference_receipt_sha,
                    usage_sha,
                ),
            )
            self._event(
                connection,
                "message.sent",
                {
                    "message_id": reply_id,
                    "room_id": source["room_id"],
                    "sequence": cursor.lastrowid,
                    "content_sha256": content_sha,
                    "route_request_sha256": None,
                },
                source["task_id"],
            )
            event = self._event(
                connection,
                "message.dispatch_completed",
                {
                    "message_id": message_id,
                    "reply_message_id": reply_id,
                    "dispatch_sha256": dispatch_sha,
                    "inference_receipt_sha256": inference_receipt_sha,
                    "route_receipt_sha256": route_receipt_sha,
                    "inference_usage_sha256": usage_sha,
                    "inference_usage_status": inference_usage["status"],
                },
                source["task_id"],
            )
        return {
            "completed": True,
            "idempotent_replay": False,
            "reply_message_id": reply_id,
            "reply_content_sha256": content_sha,
            "cursor": advanced,
            "inference_usage": {**inference_usage, "usage_sha256": usage_sha},
            "dispatch": {
                **self._public_dispatch({**completed, "dispatch_sha256": dispatch_sha}),
                "audit_chain_sha256": event["chain_sha256"],
            },
        }

    def renew_message_dispatch(self, args: dict[str, Any]) -> dict[str, Any]:
        """Extend an active dispatch lease without exposing its lease credential."""
        message_id = _require_identifier(args.get("message_id"), "message_id")
        lease_seconds = max(
            30,
            min(
                int(args.get("lease_seconds", DEFAULT_DISPATCH_LEASE_SECONDS)),
                MAX_LEASE_SECONDS,
            ),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_dispatch_lease(
                connection, message_id, args.get("lease_token")
            )
            source = connection.execute(
                "SELECT task_id FROM messages WHERE scope=? AND message_id=?",
                (self.scope, message_id),
            ).fetchone()
            if source is None:
                raise BridgeError("source message is absent")
            updated = utc_now()
            expires = time.time() + lease_seconds
            renewed = {
                **self._dispatch_payload(row),
                "lease_expires_epoch": expires,
                "updated_utc": updated,
            }
            dispatch_sha = stable_sha256(renewed)
            connection.execute(
                """UPDATE message_dispatches
                   SET lease_expires_epoch=?, updated_utc=?, dispatch_sha256=?
                   WHERE scope=? AND message_id=? AND agent_id=?""",
                (
                    expires,
                    updated,
                    dispatch_sha,
                    self.scope,
                    message_id,
                    self.agent_id,
                ),
            )
            event = self._event(
                connection,
                "message.dispatch_renewed",
                {
                    "message_id": message_id,
                    "lease_expires_epoch": expires,
                    "dispatch_sha256": dispatch_sha,
                },
            )
        return {
            "renewed": True,
            "dispatch": {
                **self._public_dispatch(
                    {**renewed, "dispatch_sha256": dispatch_sha}
                ),
                "audit_chain_sha256": event["chain_sha256"],
            },
        }

    @staticmethod
    def _discussion_signal(body: str) -> str:
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines:
            return "INVALID"
        pattern = re.compile(
            r"^PEERBRIDGE_SIGNAL\s*:\s*(CONTINUE|CONSENSUS|BLOCKED)$",
            re.IGNORECASE,
        )
        matches = [pattern.fullmatch(line) for line in lines]
        signal_indexes = [index for index, match in enumerate(matches) if match]
        if len(signal_indexes) != 1 or signal_indexes[0] != len(lines) - 1:
            return "INVALID"
        return str(matches[-1].group(1)).upper()

    @staticmethod
    def _normalized_discussion_digest(rows: Iterable[sqlite3.Row]) -> str:
        normalized = []
        for row in rows:
            body = re.sub(r"\s+", " ", str(row["body"])).strip().lower()
            normalized.append(
                {
                    "sender": str(row["sender"]),
                    "signal": Bridge._discussion_signal(str(row["body"])),
                    "body_sha256": sha256_bytes(body.encode("utf-8")),
                }
            )
        return stable_sha256(sorted(normalized, key=lambda item: item["sender"]))

    @staticmethod
    def _discussion_context(rows: Iterable[sqlite3.Row], round_number: int) -> str:
        parts = [
            f"DISCUSSION ROUND {round_number} COMPLETE.",
            "Review the peer contributions below. Add only new evidence, resolve disagreements, "
            "or state a concrete blocker. End with exactly one line: "
            "PEERBRIDGE_SIGNAL: CONTINUE, CONSENSUS, or BLOCKED.",
        ]
        for row in rows:
            parts.append(f"\n[{row['sender']}]\n{str(row['body']).strip()}")
        return "\n".join(parts)

    def advance_discussions(self, args: dict[str, Any]) -> dict[str, Any]:
        """Advance complete discussion rounds exactly once under a global DB transaction."""
        if (
            self.agent_id != DISCUSSION_COORDINATOR_ID
            or not self._discussion_coordinator
        ):
            raise BridgeError("only the discussion coordinator may advance rounds")
        raw_room_id = str(args.get("room_id") or "").strip()
        room_id = _require_identifier(raw_room_id, "room_id") if raw_room_id else None
        limit = self._bounded_integer(
            args.get("limit", 10), "limit", minimum=1, maximum=100
        )
        advanced: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            where = ["scope=?", "status='active'", "processed_round<current_round"]
            params: list[Any] = [self.scope]
            if room_id:
                where.append("room_id=?")
                params.append(room_id)
            params.append(limit)
            discussions = connection.execute(
                f"""SELECT * FROM room_discussions
                     WHERE {' AND '.join(where)}
                     ORDER BY updated_utc, discussion_id LIMIT ?""",
                tuple(params),
            ).fetchall()
            for discussion in discussions:
                discussion_id = str(discussion["discussion_id"])
                current_round = int(discussion["current_round"])
                prompts = connection.execute(
                    """SELECT * FROM messages
                       WHERE scope=? AND discussion_id=? AND discussion_round=?
                         AND discussion_role='prompt'
                       ORDER BY recipient""",
                    (self.scope, discussion_id, current_round),
                ).fetchall()
                if not prompts:
                    continue
                responses = connection.execute(
                    """SELECT reply.* FROM messages prompt
                       JOIN message_dispatches d
                         ON d.scope=prompt.scope AND d.message_id=prompt.message_id
                        AND d.status='completed'
                       JOIN messages reply
                         ON reply.scope=d.scope AND reply.message_id=d.reply_message_id
                      WHERE prompt.scope=? AND prompt.discussion_id=?
                        AND prompt.discussion_round=? AND prompt.discussion_role='prompt'
                      ORDER BY reply.sender""",
                    (self.scope, discussion_id, current_round),
                ).fetchall()
                try:
                    participants = self._discussion_participants(
                        connection, discussion_id
                    )
                except BridgeError as exc:
                    now = utc_now()
                    cancelled = self._cancel_discussion_dispatches(
                        connection,
                        discussion_id,
                        error_code="discussion_participant_unavailable",
                        now=now,
                    )
                    updated, updated_sha = self._store_discussion_state(
                        connection,
                        discussion,
                        now=now,
                        status="waiting_human",
                        processed_round=current_round,
                        message_count=int(discussion["message_count"])
                        + len(responses),
                        stop_reason="participant_unavailable",
                    )
                    event = self._event(
                        connection,
                        "discussion.round_blocked",
                        {
                            "discussion_id": discussion_id,
                            "processed_round": current_round,
                            "status": "waiting_human",
                            "stop_reason": "participant_unavailable",
                            "detail": str(exc),
                            "response_count": len(responses),
                            "cancelled_dispatch_count": cancelled,
                            "discussion_sha256": updated_sha,
                        },
                        str(discussion["task_id"]),
                    )
                    advanced.append(
                        {
                            "discussion_id": discussion_id,
                            "room_id": discussion["room_id"],
                            "processed_round": current_round,
                            "current_round": updated["current_round"],
                            "status": "waiting_human",
                            "stop_reason": "participant_unavailable",
                            "new_prompt_count": 0,
                            "discussion_sha256": updated_sha,
                            "audit_chain_sha256": event["chain_sha256"],
                        }
                    )
                    continue
                if len(prompts) != len(participants):
                    raise BridgeError("discussion prompt participant set drifted")
                terminal = connection.execute(
                    """SELECT COUNT(*) FROM message_dispatches d
                       JOIN messages prompt
                         ON prompt.scope=d.scope AND prompt.message_id=d.message_id
                      WHERE prompt.scope=? AND prompt.discussion_id=?
                        AND prompt.discussion_round=? AND prompt.discussion_role='prompt'
                        AND d.status='failed'""",
                    (self.scope, discussion_id, current_round),
                ).fetchone()[0]
                if len(responses) + int(terminal) < len(prompts):
                    continue

                digest = self._normalized_discussion_digest(responses)
                stagnant = bool(
                    discussion["last_round_digest"]
                    and digest == discussion["last_round_digest"]
                )
                stagnation_count = int(discussion["stagnation_count"]) + (1 if stagnant else 0)
                if not stagnant:
                    stagnation_count = 0
                signals = [self._discussion_signal(str(row["body"])) for row in responses]
                complete_response_set = len(responses) == len(prompts)
                malformed_signal = complete_response_set and any(
                    signal == "INVALID" for signal in signals
                )
                consensus = complete_response_set and all(
                    signal == "CONSENSUS" for signal in signals
                )
                blocked = complete_response_set and all(
                    signal == "BLOCKED" for signal in signals
                )
                message_count = int(discussion["message_count"]) + len(responses)
                next_round = current_round + 1
                stop_reason = None
                next_status = "active"
                if int(terminal):
                    next_status, stop_reason = "waiting_human", "agent_dispatch_failed"
                elif malformed_signal:
                    next_status, stop_reason = "waiting_human", "malformed_signal"
                elif consensus:
                    next_status, stop_reason = "completed", "consensus"
                elif blocked:
                    next_status, stop_reason = "waiting_human", "all_agents_blocked"
                elif current_round >= int(discussion["max_rounds"]):
                    next_status, stop_reason = "waiting_human", "round_limit"
                elif message_count + (2 * len(participants)) > int(
                    discussion["max_messages"]
                ):
                    next_status, stop_reason = "waiting_human", "message_limit"
                elif stagnation_count >= int(discussion["stagnation_rounds"]):
                    next_status, stop_reason = "waiting_human", "stagnation"

                new_messages: list[dict[str, Any]] = []
                now = utc_now()
                if next_status == "active":
                    context = self._discussion_context(responses, current_round)
                    if len(context) > MAX_DISCUSSION_CONTEXT_CHARS:
                        next_status, stop_reason = "waiting_human", "context_limit"
                    for agent_id, route_id, request in (
                        participants if next_status == "active" else []
                    ):
                        new_messages.append(
                            self._insert_discussion_prompt(
                                connection,
                                room_id=str(discussion["room_id"]),
                                task_id=str(discussion["task_id"]),
                                sender=DISCUSSION_ORCHESTRATOR_ID,
                                recipient=agent_id,
                                subject=f"Re: {discussion['subject']} // round {next_round}",
                                body=context,
                                artifacts=[],
                                priority="normal",
                                route_profile_id=route_id,
                                route_request=request,
                                discussion_id=discussion_id,
                                discussion_round=next_round,
                                created=now,
                            )
                        )
                    message_count += len(new_messages)

                updated, updated_sha = self._store_discussion_state(
                    connection,
                    discussion,
                    now=now,
                    status=next_status,
                    current_round=(
                        next_round if next_status == "active" else current_round
                    ),
                    processed_round=current_round,
                    message_count=message_count,
                    stagnation_count=stagnation_count,
                    last_round_digest=digest,
                    stop_reason=stop_reason,
                )
                event = self._event(
                    connection,
                    "discussion.round_advanced",
                    {
                        "discussion_id": discussion_id,
                        "processed_round": current_round,
                        "next_round": updated["current_round"],
                        "status": next_status,
                        "stop_reason": stop_reason,
                        "response_count": len(responses),
                        "terminal_failure_count": int(terminal),
                        "new_prompt_count": len(new_messages),
                        "round_digest": digest,
                        "discussion_sha256": updated_sha,
                    },
                    str(discussion["task_id"]),
                )
                advanced.append(
                    {
                        "discussion_id": discussion_id,
                        "room_id": discussion["room_id"],
                        "processed_round": current_round,
                        "current_round": updated["current_round"],
                        "status": next_status,
                        "stop_reason": stop_reason,
                        "new_prompt_count": len(new_messages),
                        "discussion_sha256": updated_sha,
                        "audit_chain_sha256": event["chain_sha256"],
                    }
                )
        return {"advanced": advanced, "count": len(advanced)}

    def control_discussion(self, args: dict[str, Any]) -> dict[str, Any]:
        discussion_id = _require_identifier(args.get("discussion_id"), "discussion_id")
        action = str(args.get("action") or "").strip().lower()
        if action not in {"pause", "resume", "stop", "continue"}:
            raise BridgeError("action must be pause, resume, stop or continue")
        extra_rounds = self._bounded_integer(
            args.get("extra_rounds", 2), "extra_rounds", minimum=1, maximum=10
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM room_discussions WHERE scope=? AND discussion_id=?",
                (self.scope, discussion_id),
            ).fetchone()
            if row is None:
                raise BridgeError("discussion not found")
            self._require_room_manager(connection, str(row["room_id"]))
            status = str(row["status"])
            max_rounds = int(row["max_rounds"])
            max_messages = int(row["max_messages"])
            stop_reason = row["stop_reason"]
            new_messages: list[dict[str, Any]] = []
            cancelled_dispatches = 0
            if action == "pause":
                if row["status"] != "active":
                    raise BridgeError("only an active discussion can pause")
                status, stop_reason = "paused", "human_paused"
            elif action == "stop":
                if status not in {"active", "paused", "waiting_human"}:
                    raise BridgeError("only an open discussion can stop")
                status, stop_reason = "stopped", "human_stopped"
                cancelled_dispatches = self._cancel_discussion_dispatches(
                    connection,
                    discussion_id,
                    error_code="discussion_stopped",
                    now=now,
                )
            elif action == "resume":
                if row["status"] != "paused":
                    raise BridgeError("only a paused discussion can resume")
                status, stop_reason = "active", None
            else:
                if row["status"] not in {"waiting_human", "paused"}:
                    raise BridgeError("continue requires a paused or waiting discussion")
                current_round = int(row["current_round"])
                processed_round = int(row["processed_round"])
                max_rounds = min(
                    MAX_DISCUSSION_ROUNDS,
                    max(max_rounds, current_round + extra_rounds),
                )
                status, stop_reason = "active", None
                # A paused, unprocessed round already has prompts and only needs
                # to be re-enabled. A waiting discussion needs a brand-new round.
                if processed_round >= current_round:
                    if current_round >= MAX_DISCUSSION_ROUNDS:
                        raise BridgeError("discussion reached the absolute round limit")
                    responses = connection.execute(
                        """SELECT * FROM messages
                            WHERE scope=? AND discussion_id=? AND discussion_round=?
                              AND discussion_role='response'
                            ORDER BY sender""",
                        (self.scope, discussion_id, int(row["current_round"])),
                    ).fetchall()
                    seats = self._discussion_participants(connection, discussion_id)
                    next_round = current_round + 1
                    complete_round_messages = 2 * len(seats)
                    if (
                        int(row["message_count"]) + complete_round_messages
                        > MAX_DISCUSSION_MESSAGES
                    ):
                        raise BridgeError("discussion reached the absolute message limit")
                    context = self._discussion_context(
                        responses, current_round
                    )
                    context += "\n\nA human operator explicitly requested another bounded round."
                    if len(context) > MAX_DISCUSSION_CONTEXT_CHARS:
                        raise BridgeError("discussion context reached the absolute size limit")
                    for agent_id, route_id, request in seats:
                        new_messages.append(
                            self._insert_discussion_prompt(
                                connection,
                                room_id=str(row["room_id"]),
                                task_id=str(row["task_id"]),
                                sender=DISCUSSION_ORCHESTRATOR_ID,
                                recipient=agent_id,
                                subject=f"Re: {row['subject']} // round {next_round}",
                                body=context,
                                artifacts=[],
                                priority="normal",
                                route_profile_id=route_id,
                                route_request=request,
                                discussion_id=discussion_id,
                                discussion_round=next_round,
                                created=now,
                            )
                        )
                    max_messages = min(
                        MAX_DISCUSSION_MESSAGES,
                        max(
                            max_messages,
                            int(row["message_count"])
                            + len(seats) * 2 * extra_rounds,
                        ),
                    )
                    row_changes = {
                        "current_round": next_round,
                        "message_count": int(row["message_count"]) + len(new_messages),
                    }
                else:
                    row_changes = {}
            updated, discussion_sha = self._store_discussion_state(
                connection,
                row,
                now=now,
                status=status,
                max_rounds=max_rounds,
                max_messages=max_messages,
                stop_reason=stop_reason,
                **row_changes if action == "continue" else {},
            )
            event = self._event(
                connection,
                "discussion.controlled",
                {
                    "discussion_id": discussion_id,
                    "action": action,
                    "status": status,
                    "max_rounds": max_rounds,
                    "max_messages": max_messages,
                    "new_prompt_count": len(new_messages),
                    "cancelled_dispatch_count": cancelled_dispatches,
                    "discussion_sha256": discussion_sha,
                },
                str(row["task_id"]),
            )
        return {
            **updated,
            "updated_utc": now,
            "discussion_sha256": discussion_sha,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def reconcile_message_dispatches(self, args: dict[str, Any]) -> dict[str, Any]:
        """Terminally classify routed root messages that can no longer dispatch."""
        if (
            self.agent_id != DISCUSSION_COORDINATOR_ID
            or not self._discussion_coordinator
        ):
            raise BridgeError("only the discussion coordinator may reconcile dispatches")
        limit = self._bounded_integer(
            args.get("limit", 250), "limit", minimum=1, maximum=1000
        )
        max_attempts = self._bounded_integer(
            args.get("max_attempts", MAX_DISPATCH_ATTEMPTS),
            "max_attempts",
            minimum=1,
            maximum=100,
        )
        raw_observations = args.get("route_runtime_observations", [])
        if not isinstance(raw_observations, list):
            raise BridgeError("route_runtime_observations must be a list")
        route_runtime_observations: dict[str, dict[str, Any]] = {}
        for raw_observation in raw_observations:
            if not isinstance(raw_observation, dict):
                raise BridgeError("route runtime observation must be an object")
            message_id = _require_identifier(
                raw_observation.get("message_id"), "message_id"
            )
            route_request_sha256 = _require_sha256(
                raw_observation.get("route_request_sha256"),
                "route_request_sha256",
            )
            match_count = self._bounded_integer(
                raw_observation.get("match_count"),
                "match_count",
                minimum=0,
                maximum=1000,
            )
            if match_count == 1:
                raise BridgeError(
                    "route runtime observations only describe terminal match counts"
                )
            if message_id in route_runtime_observations:
                raise BridgeError("route runtime observation is duplicated")
            observation = {
                "message_id": message_id,
                "route_request_sha256": route_request_sha256,
                "match_count": match_count,
            }
            route_runtime_observations[message_id] = {
                **observation,
                "observation_sha256": stable_sha256(observation),
            }
        now = utc_now()
        now_epoch = time.time()
        reconciled: list[dict[str, Any]] = []
        observed_message_ids: set[str] = set()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT m.*, rd.status AS discussion_status,
                          d.status AS dispatch_status,
                          d.claimed_session_id, d.lease_token_sha256,
                          d.lease_expires_epoch, d.attempt_count,
                          d.claimed_utc, d.updated_utc AS dispatch_updated_utc,
                          d.completed_utc, d.reply_message_id,
                          d.inference_receipt_sha256, d.error_code,
                          d.dispatch_sha256
                     FROM messages m
                     LEFT JOIN room_discussions rd
                       ON rd.scope=m.scope AND rd.discussion_id=m.discussion_id
                     LEFT JOIN message_receipts r
                       ON r.scope=m.scope AND r.message_id=m.message_id
                      AND r.agent_id=m.recipient
                     LEFT JOIN message_dispatches d
                       ON d.scope=m.scope AND d.message_id=m.message_id
                      AND d.agent_id=m.recipient
                    WHERE m.scope=? AND m.recipient!='*'
                      AND m.sender!=m.recipient AND m.reply_to IS NULL
                      AND m.route_request_sha256 IS NOT NULL
                      AND r.message_id IS NULL
                      AND (m.discussion_id IS NULL OR rd.status='active')
                      AND (d.status IS NULL OR d.status NOT IN ('completed', 'failed'))
                    ORDER BY m.sequence LIMIT ?""",
                (self.scope, limit),
            ).fetchall()
            for row in rows:
                message_id = str(row["message_id"])
                runtime_observation = route_runtime_observations.get(message_id)
                if runtime_observation is not None:
                    observed_message_ids.add(message_id)
                    stored_route_sha256 = str(row["route_request_sha256"] or "")
                    if not secrets.compare_digest(
                        stored_route_sha256,
                        runtime_observation["route_request_sha256"],
                    ):
                        raise BridgeError(
                            "route runtime observation does not match the message route SHA"
                        )
                error_code: str | None = None
                membership = None
                if str(row["room_id"]) != DEFAULT_ROOM_ID:
                    membership = self._active_room_member(
                        connection, str(row["room_id"]), str(row["recipient"])
                    )
                    if membership is None:
                        error_code = "room_seat_unavailable"
                    elif str(membership["route_profile_id"] or "") != str(
                        row["route_profile_id"] or ""
                    ):
                        error_code = "room_seat_route_changed"
                if error_code is None:
                    try:
                        current_route = self._resolve_route_request(
                            connection,
                            str(row["recipient"]),
                            {
                                "route_profile_id": row["route_profile_id"],
                                "requested_provider_id": row[
                                    "requested_provider_id"
                                ],
                                "requested_model_id": row["requested_model_id"],
                                "requested_reasoning_mode": row[
                                    "requested_reasoning_mode"
                                ],
                                "requested_route_class": row[
                                    "requested_route_class"
                                ],
                            },
                        )
                    except BridgeError:
                        error_code = "route_profile_unavailable"
                    else:
                        if current_route != self._route_request_from_row(row):
                            error_code = "route_profile_changed"

                attempts = int(row["attempt_count"] or 0)
                lease_active = (
                    row["dispatch_status"] == "claimed"
                    and float(row["lease_expires_epoch"] or 0) > now_epoch
                )
                if (
                    error_code is None
                    and attempts >= max_attempts
                    and not lease_active
                ):
                    error_code = "dispatch_attempts_exhausted"
                if (
                    error_code is None
                    and runtime_observation is not None
                    and not lease_active
                ):
                    error_code = (
                        "route_runtime_unavailable"
                        if runtime_observation["match_count"] == 0
                        else "route_runtime_ambiguous"
                    )
                if error_code is None:
                    continue

                failed = {
                    "scope": self.scope,
                    "message_id": row["message_id"],
                    "agent_id": row["recipient"],
                    "status": "failed",
                    "claimed_session_id": None,
                    "lease_token_sha256": None,
                    "lease_expires_epoch": None,
                    "attempt_count": attempts,
                    "claimed_utc": row["claimed_utc"],
                    "updated_utc": now,
                    "completed_utc": now,
                    "reply_message_id": None,
                    "inference_receipt_sha256": None,
                    "error_code": error_code,
                }
                dispatch_sha = stable_sha256(failed)
                connection.execute(
                    """INSERT INTO message_dispatches(
                           scope, message_id, agent_id, status, claimed_session_id,
                           lease_token_sha256, lease_expires_epoch, attempt_count,
                           claimed_utc, updated_utc, completed_utc, reply_message_id,
                           inference_receipt_sha256, error_code, dispatch_sha256
                       ) VALUES (?, ?, ?, 'failed', NULL, NULL, NULL, ?, ?, ?, ?,
                                 NULL, NULL, ?, ?)
                       ON CONFLICT(scope, message_id, agent_id) DO UPDATE SET
                           status='failed', claimed_session_id=NULL,
                           lease_token_sha256=NULL, lease_expires_epoch=NULL,
                           attempt_count=excluded.attempt_count,
                           updated_utc=excluded.updated_utc,
                           completed_utc=excluded.completed_utc,
                           reply_message_id=NULL, inference_receipt_sha256=NULL,
                           error_code=excluded.error_code,
                           dispatch_sha256=excluded.dispatch_sha256""",
                    (
                        self.scope,
                        row["message_id"],
                        row["recipient"],
                        attempts,
                        row["claimed_utc"],
                        now,
                        now,
                        error_code,
                        dispatch_sha,
                    ),
                )
                reconciled.append(
                    {
                        "message_id": row["message_id"],
                        "discussion_id": row["discussion_id"],
                        "agent_id": row["recipient"],
                        "error_code": error_code,
                        "route_runtime_observation_sha256": (
                            runtime_observation["observation_sha256"]
                            if runtime_observation is not None
                            else None
                        ),
                        "dispatch_sha256": dispatch_sha,
                    }
                )
            missing_observations = set(route_runtime_observations) - observed_message_ids
            if missing_observations:
                raise BridgeError(
                    "route runtime observation does not identify a pending routed message"
                )
            event = (
                self._event(
                    connection,
                    "message.dispatches_reconciled",
                    {"count": len(reconciled), "dispatches": reconciled},
                )
                if reconciled
                else None
            )
        return {
            "reconciled": reconciled,
            "count": len(reconciled),
            "audit_chain_sha256": event["chain_sha256"] if event else None,
        }

    def fail_message_dispatch(self, args: dict[str, Any]) -> dict[str, Any]:
        message_id = _require_identifier(args.get("message_id"), "message_id")
        error_code = _require_identifier(args.get("error_code"), "error_code")
        retryable = bool(args.get("retryable", True))
        retry_after_seconds = (
            max(
                1,
                min(
                    int(
                        args.get(
                            "retry_after_seconds", DEFAULT_DISPATCH_RETRY_SECONDS
                        )
                    ),
                    MAX_DISPATCH_RETRY_SECONDS,
                ),
            )
            if retryable
            else 0
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_dispatch_lease(
                connection, message_id, args.get("lease_token")
            )
            source = connection.execute(
                "SELECT task_id FROM messages WHERE scope=? AND message_id=?",
                (self.scope, message_id),
            ).fetchone()
            if source is None:
                raise BridgeError("source message is absent")
            updated = utc_now()
            failed = {
                **self._dispatch_payload(row),
                "status": "retryable" if retryable else "failed",
                "claimed_session_id": None,
                "lease_token_sha256": None,
                "lease_expires_epoch": None,
                "updated_utc": updated,
                "error_code": error_code,
            }
            dispatch_sha = stable_sha256(failed)
            retry_schedule = None
            if retryable:
                retry_schedule = {
                    "scope": self.scope,
                    "message_id": message_id,
                    "agent_id": self.agent_id,
                    "attempt_count": int(row["attempt_count"]),
                    "not_before_epoch": time.time() + retry_after_seconds,
                    "error_code": error_code,
                    "created_utc": updated,
                }
                retry_schedule["schedule_sha256"] = stable_sha256(retry_schedule)
                connection.execute(
                    """INSERT INTO message_dispatch_retry_schedules(
                           scope, message_id, agent_id, attempt_count,
                           not_before_epoch, error_code, created_utc,
                           schedule_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        retry_schedule["scope"],
                        retry_schedule["message_id"],
                        retry_schedule["agent_id"],
                        retry_schedule["attempt_count"],
                        retry_schedule["not_before_epoch"],
                        retry_schedule["error_code"],
                        retry_schedule["created_utc"],
                        retry_schedule["schedule_sha256"],
                    ),
                )
            connection.execute(
                """UPDATE message_dispatches SET status=?, claimed_session_id=NULL,
                       lease_token_sha256=NULL, lease_expires_epoch=NULL,
                       updated_utc=?, error_code=?, dispatch_sha256=?
                   WHERE scope=? AND message_id=? AND agent_id=?""",
                (
                    failed["status"],
                    updated,
                    error_code,
                    dispatch_sha,
                    self.scope,
                    message_id,
                    self.agent_id,
                ),
            )
            event = self._event(
                connection,
                "message.dispatch_failed",
                {
                    "message_id": message_id,
                    "status": failed["status"],
                    "error_code": error_code,
                    "retry_after_seconds": retry_after_seconds if retryable else None,
                    "retry_not_before_epoch": (
                        retry_schedule["not_before_epoch"] if retry_schedule else None
                    ),
                    "retry_schedule_sha256": (
                        retry_schedule["schedule_sha256"] if retry_schedule else None
                    ),
                    "dispatch_sha256": dispatch_sha,
                },
                str(source["task_id"]),
            )
        return {
            "failed": True,
            "retryable": retryable,
            "retry_schedule": retry_schedule,
            "dispatch": {
                **self._public_dispatch({**failed, "dispatch_sha256": dispatch_sha}),
                "audit_chain_sha256": event["chain_sha256"],
            },
        }

    def list_message_dispatches(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_status = str(args.get("status") or "").strip().lower()
        allowed = {"claimed", "retryable", "failed", "completed"}
        if raw_status and raw_status not in allowed:
            raise BridgeError("dispatch status is invalid")
        limit = max(1, min(int(args.get("limit", 100)), 500))
        where = ["scope=?"]
        params: list[Any] = [self.scope]
        if self.agent_id != "human-operator":
            where.append("agent_id=?")
            params.append(self.agent_id)
        if raw_status:
            where.append("status=?")
            params.append(raw_status)
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM message_dispatches
                     WHERE {' AND '.join(where)}
                     ORDER BY updated_utc DESC, message_id LIMIT ?""",
                tuple(params),
            ).fetchall()
        dispatches = [self._public_dispatch(row) for row in rows]
        return {"dispatches": dispatches, "count": len(dispatches)}

    def claim_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        summary = _require_text(args.get("summary"), "summary", limit=10_000)
        read_paths = self._clean_task_paths(args.get("read_paths", []), "read")
        write_paths = self._clean_task_paths(args.get("write_paths", []), "write")
        lease_seconds = max(
            30,
            min(int(args.get("lease_seconds", DEFAULT_LEASE_SECONDS)), MAX_LEASE_SECONDS),
        )
        approval_mode = str(args.get("approval_mode", "presence_aware")).lower()
        if approval_mode not in {
            "solo_allowed",
            "two_party_required",
            "presence_aware",
            "quorum_required",
        }:
            raise BridgeError(
                "approval_mode must be solo_allowed, two_party_required, "
                "presence_aware or quorum_required"
            )
        required_peer = str(args.get("required_peer") or "").strip() or None
        if required_peer:
            required_peer = _require_identifier(required_peer, "required_peer")
            if required_peer == self.agent_id:
                raise BridgeError("required_peer must be another agent")
        raw_required_peers = _json_list(args.get("required_peers"), "required_peers")
        if len(raw_required_peers) > MAX_REQUIRED_PEERS:
            raise BridgeError("required_peers exceeds the peer limit")
        required_peers = []
        for item in raw_required_peers:
            peer = _require_identifier(item, "required_peers item")
            if peer == self.agent_id:
                raise BridgeError("required_peers must contain only other agents")
            if peer not in required_peers:
                required_peers.append(peer)
        if required_peer and required_peer not in required_peers:
            required_peers.append(required_peer)
        required_peers.sort()
        try:
            review_quorum = int(args.get("review_quorum", 1))
        except (TypeError, ValueError) as exc:
            raise BridgeError("review_quorum must be a positive integer") from exc
        if review_quorum < 1:
            raise BridgeError("review_quorum must be a positive integer")
        if required_peers and review_quorum > len(required_peers):
            raise BridgeError("review_quorum cannot exceed required_peers count")
        if approval_mode == "quorum_required" and not required_peers:
            raise BridgeError("quorum_required needs at least one required_peers entry")
        owner = str(args.get("owner") or "").strip() or None
        now = utc_now()
        now_epoch = time.time()
        expires = now_epoch + lease_seconds
        token = secrets.token_urlsafe(32)
        token_sha = sha256_bytes(token.encode("utf-8"))
        task_content = {
            "scope": self.scope,
            "task_id": task_id,
            "workspace_root_key": self.workspace_root_key,
            "summary": summary,
            "owner": owner,
            "read_paths": read_paths,
            "write_paths": write_paths,
            "approval_mode": approval_mode,
            "required_peer": required_peer,
            "required_peers": required_peers,
            "review_quorum": review_quorum,
        }
        task_sha = stable_sha256(task_content)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_leases(connection)
            existing = connection.execute(
                "SELECT * FROM tasks WHERE scope=? AND task_id=?",
                (self.scope, task_id),
            ).fetchone()
            if existing and existing["status"] == "complete":
                raise BridgeError("task is already complete")
            if existing and existing["status"] == "claimed":
                raise BridgeError(
                    f"task already has an active lease held by {existing['claimed_by']}"
                )
            requested = [("read", item) for item in read_paths] + [
                ("write", item) for item in write_paths
            ]
            active = connection.execute(
                """SELECT t.scope, t.task_id, t.claimed_by, p.access, p.path_prefix
                   FROM tasks t JOIN task_paths p
                     ON p.scope=t.scope AND p.task_id=t.task_id
                   WHERE t.workspace_root_key=? AND t.status='claimed'
                     AND t.lease_expires_epoch>?
                   LIMIT ?""",
                (self.workspace_root_key, now_epoch, MAX_ACTIVE_TASK_PATH_ROWS + 1),
            ).fetchall()
            if len(active) > MAX_ACTIVE_TASK_PATH_ROWS:
                raise BridgeError("active task path conflict set exceeds the bounded limit")
            conflicts = []
            for new_access, new_path in requested:
                for row in active:
                    if row["scope"] == self.scope and row["task_id"] == task_id:
                        continue
                    if new_access == "read" and row["access"] == "read":
                        continue
                    if self._path_overlaps(new_path, row["path_prefix"]):
                        conflicts.append(
                            {
                                "scope": row["scope"],
                                "task_id": row["task_id"],
                                "claimed_by": row["claimed_by"],
                                "existing_access": row["access"],
                                "existing_path": row["path_prefix"],
                                "requested_access": new_access,
                                "requested_path": new_path,
                            }
                        )
            if conflicts:
                raise BridgeError(
                    "task path scope conflicts with an active lease: "
                    + json.dumps(conflicts, ensure_ascii=False, sort_keys=True)
                )
            task_revision = (
                int(existing["task_revision"] or 1)
                + (0 if existing["task_sha256"] == task_sha else 1)
                if existing
                else 1
            )
            if existing:
                connection.execute(
                    """UPDATE tasks SET workspace_root_key=?, summary=?, owner=?, status='claimed', claimed_by=?,
                       claimed_session_id=?, lease_token_sha256=?, lease_expires_epoch=?,
                       claimed_utc=?, updated_utc=?, task_sha256=?, task_revision=?, approval_mode=?,
                       required_peer=?, review_quorum=? WHERE scope=? AND task_id=?""",
                    (
                        self.workspace_root_key,
                        summary,
                        owner,
                        self.agent_id,
                        self.session_id,
                        token_sha,
                        expires,
                        now,
                        now,
                        task_sha,
                        task_revision,
                        approval_mode,
                        required_peer,
                        review_quorum,
                        self.scope,
                        task_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM task_paths WHERE scope=? AND task_id=?",
                    (self.scope, task_id),
                )
            else:
                connection.execute(
                    """INSERT INTO tasks(
                        scope, task_id, workspace_root_key, summary, owner, status, claimed_by,
                        claimed_session_id, lease_token_sha256, lease_expires_epoch,
                        claimed_utc, created_utc, updated_utc, task_sha256,
                        task_revision, approval_mode, required_peer, review_quorum
                    ) VALUES (?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.scope,
                        task_id,
                        self.workspace_root_key,
                        summary,
                        owner,
                        self.agent_id,
                        self.session_id,
                        token_sha,
                        expires,
                        now,
                        now,
                        now,
                        task_sha,
                        task_revision,
                        approval_mode,
                        required_peer,
                        review_quorum,
                    ),
                )
            for access, path_prefix in requested:
                connection.execute(
                    "INSERT INTO task_paths VALUES (?, ?, ?, ?, ?)",
                    (self.scope, task_id, access, path_prefix, now),
                )
            connection.execute(
                "DELETE FROM task_required_peers WHERE scope=? AND task_id=?",
                (self.scope, task_id),
            )
            connection.executemany(
                "INSERT INTO task_required_peers(scope, task_id, peer_id) VALUES (?, ?, ?)",
                [(self.scope, task_id, peer) for peer in required_peers],
            )
            self._event(
                connection,
                "task.claimed",
                {
                    "task_sha256": task_sha,
                    "task_revision": task_revision,
                    "lease_seconds": lease_seconds,
                    "read_paths": read_paths,
                    "write_paths": write_paths,
                    "approval_mode": approval_mode,
                    "required_peer": required_peer,
                    "required_peers": required_peers,
                    "review_quorum": review_quorum,
                },
                task_id,
            )
        return {
            "task_id": task_id,
            "claimed_by": self.agent_id,
            "lease_token": token,
            "lease_expires_epoch": expires,
            "lease_seconds": lease_seconds,
            "read_paths": read_paths,
            "write_paths": write_paths,
            "task_sha256": task_sha,
            "task_revision": task_revision,
            "approval_mode": approval_mode,
            "required_peers": required_peers,
            "review_quorum": review_quorum,
        }

    def renew_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        lease_seconds = max(
            30,
            min(int(args.get("lease_seconds", DEFAULT_LEASE_SECONDS)), MAX_LEASE_SECONDS),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, args.get("lease_token"))
            expires = time.time() + lease_seconds
            connection.execute(
                "UPDATE tasks SET lease_expires_epoch=?, updated_utc=? WHERE scope=? AND task_id=?",
                (expires, utc_now(), self.scope, task_id),
            )
            self._event(
                connection,
                "task.lease_renewed",
                {"lease_seconds": lease_seconds, "lease_expires_epoch": expires},
                task_id,
            )
        return {"task_id": task_id, "lease_expires_epoch": expires}

    def release_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        status = str(args.get("status", "open")).lower()
        if status not in {"open", "blocked"}:
            raise BridgeError("release status must be open or blocked")
        reason = str(args.get("reason") or "").strip()
        if reason and contains_secret(reason):
            raise BridgeError("reason appears to contain a credential")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, args.get("lease_token"))
            connection.execute(
                """UPDATE tasks SET status=?, claimed_by=NULL, claimed_session_id=NULL,
                   lease_token_sha256=NULL, lease_expires_epoch=NULL, claimed_utc=NULL,
                   updated_utc=? WHERE scope=? AND task_id=?""",
                (status, utc_now(), self.scope, task_id),
            )
            self._event(
                connection,
                "task.released",
                {"status": status, "reason_sha256": stable_sha256(reason)},
                task_id,
            )
        return {"task_id": task_id, "status": status, "released": True}

    def announce_work(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        summary = _require_text(args.get("summary"), "summary", limit=10_000)
        status = str(args.get("status", "working")).lower()
        if status not in {"working", "waiting", "review"}:
            raise BridgeError("status must be working, waiting or review")
        artifacts = self._clean_artifacts(args.get("artifact_paths", []))
        update_id = uuid.uuid4().hex
        created = utc_now()
        content = {
            "update_id": update_id,
            "scope": self.scope,
            "task_id": task_id,
            "agent_id": self.agent_id,
            "status": status,
            "summary": summary,
            "artifact_paths": artifacts,
            "created_utc": created,
        }
        update_sha = stable_sha256(content)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, args.get("lease_token"))
            connection.execute(
                "INSERT INTO work_updates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    update_id,
                    self.scope,
                    task_id,
                    self.agent_id,
                    status,
                    summary,
                    json.dumps(artifacts, ensure_ascii=False),
                    created,
                    update_sha,
                ),
            )
            connection.execute(
                "UPDATE tasks SET summary=?, updated_utc=? WHERE scope=? AND task_id=?",
                (summary, created, self.scope, task_id),
            )
            self._event(
                connection,
                "work.announced",
                {"update_id": update_id, "status": status, "update_sha256": update_sha},
                task_id,
            )
        return {
            "update_id": update_id,
            "task_id": task_id,
            "status": status,
            "update_sha256": update_sha,
        }

    def workboard(self, args: dict[str, Any]) -> dict[str, Any]:
        include_completed = bool(args.get("include_completed", False))
        limit = max(1, min(int(args.get("limit", 100)), 500))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_leases(connection)
            status_clause = "" if include_completed else "AND t.status!='complete'"
            rows = connection.execute(
                f"""SELECT t.* FROM tasks t WHERE t.scope=? {status_clause}
                    ORDER BY t.updated_utc DESC, t.task_id ASC LIMIT ?""",
                (self.scope, limit),
            ).fetchall()
            tasks = []
            for row in rows:
                paths = connection.execute(
                    "SELECT access, path_prefix FROM task_paths WHERE scope=? AND task_id=? ORDER BY access, path_prefix",
                    (self.scope, row["task_id"]),
                ).fetchall()
                latest = connection.execute(
                    """SELECT * FROM work_updates WHERE scope=? AND task_id=?
                       ORDER BY created_utc DESC, rowid DESC LIMIT 1""",
                    (self.scope, row["task_id"]),
                ).fetchone()
                item = dict(row)
                item.pop("lease_token_sha256", None)
                item["paths"] = [dict(path) for path in paths]
                item["latest_update"] = dict(latest) if latest else None
                if item["latest_update"]:
                    item["latest_update"]["artifact_paths"] = json.loads(
                        item["latest_update"].pop("artifact_paths_json")
                    )
                tasks.append(item)
        return {
            "scope": self.scope,
            "tasks": tasks,
            "count": len(tasks),
            "presence": self.presence_snapshot(),
        }

    def request_review(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        recipient = _require_identifier(args.get("recipient"), "recipient")
        if recipient == self.agent_id:
            raise BridgeError("review recipient must be another agent")
        question = _require_text(args.get("question"), "question", limit=20_000)
        artifacts = self._clean_artifacts(args.get("artifact_paths", []))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._require_lease(connection, task_id, args.get("lease_token"))
            request_id = uuid.uuid4().hex
            created = utc_now()
            content = {
                "request_id": request_id,
                "scope": self.scope,
                "task_id": task_id,
                "requester": self.agent_id,
                "recipient": recipient,
                "question": question,
                "artifact_paths": artifacts,
                "request_utc": created,
                "approval_mode": task["approval_mode"],
                "task_revision": int(task["task_revision"]),
                "task_sha256": str(task["task_sha256"]),
            }
            request_sha = stable_sha256(content)
            cursor = connection.execute(
                """INSERT INTO peer_calls(
                    request_id, scope, task_id, requester, recipient, question,
                    artifact_paths_json, request_utc, request_sha256,
                    task_revision, task_sha256, status,
                    approval_mode, response, response_artifact_paths_json,
                    response_utc, response_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL, NULL, NULL, NULL)""",
                (
                    request_id,
                    self.scope,
                    task_id,
                    self.agent_id,
                    recipient,
                    question,
                    json.dumps(artifacts, ensure_ascii=False),
                    created,
                    request_sha,
                    int(task["task_revision"]),
                    str(task["task_sha256"]),
                    task["approval_mode"],
                ),
            )
            self._event(
                connection,
                "peer.requested",
                {
                    "request_id": request_id,
                    "sequence": cursor.lastrowid,
                    "request_sha256": request_sha,
                    "recipient": recipient,
                },
                task_id,
            )
        return {
            "request_id": request_id,
            "sequence": cursor.lastrowid,
            "request_sha256": request_sha,
            "status": "open",
        }

    def poll_reviews(self, args: dict[str, Any]) -> dict[str, Any]:
        agent = self._bound_consumer(args)
        after = max(0, int(args.get("after_cursor", 0)))
        limit = max(1, min(int(args.get("limit", 50)), 500))
        include_closed = bool(args.get("include_closed", False))
        status_clause = "" if include_closed else "AND status IN ('open', 'responded')"
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM peer_calls
                    WHERE scope=? AND sequence>? AND (recipient=? OR requester=?)
                    {status_clause} ORDER BY sequence ASC LIMIT ?""",
                (self.scope, after, agent, agent, limit),
            ).fetchall()
        calls = []
        for row in rows:
            item = dict(row)
            item["artifact_paths"] = json.loads(item.pop("artifact_paths_json"))
            item["response_artifact_paths"] = json.loads(
                item.pop("response_artifact_paths_json") or "[]"
            )
            calls.append(item)
        return {
            "calls": calls,
            "count": len(calls),
            "next_cursor": calls[-1]["sequence"] if calls else after,
        }

    def submit_review(self, args: dict[str, Any]) -> dict[str, Any]:
        request_id = _require_identifier(args.get("request_id"), "request_id")
        verdict = str(args.get("verdict", "")).lower()
        if verdict not in {"approved", "changes_requested", "blocked"}:
            raise BridgeError("verdict must be approved, changes_requested or blocked")
        try:
            score = int(args.get("score"))
        except (TypeError, ValueError) as exc:
            raise BridgeError("score must be an integer from 0 to 100") from exc
        if not 0 <= score <= 100:
            raise BridgeError("score must be an integer from 0 to 100")
        findings = _require_text(args.get("findings"), "findings")
        response_text = str(args.get("response") or "").strip()
        if response_text and contains_secret(response_text):
            raise BridgeError("response appears to contain a credential")
        artifacts = self._clean_artifacts(args.get("artifact_paths", []))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            call = connection.execute(
                "SELECT * FROM peer_calls WHERE scope=? AND request_id=?",
                (self.scope, request_id),
            ).fetchone()
            if call is None:
                raise BridgeError("review request not found")
            if call["recipient"] != self.agent_id:
                raise BridgeError("only the addressed peer may submit this review")
            task = connection.execute(
                "SELECT task_revision, task_sha256 FROM tasks WHERE scope=? AND task_id=?",
                (self.scope, call["task_id"]),
            ).fetchone()
            if task is None:
                raise BridgeError("review task not found")
            if (
                int(call["task_revision"]) != int(task["task_revision"])
                or str(call["task_sha256"]) != str(task["task_sha256"])
            ):
                raise BridgeError("review request is bound to a superseded task revision")
            review_id = uuid.uuid4().hex
            reviewed = utc_now()
            content = {
                "review_id": review_id,
                "scope": self.scope,
                "request_id": request_id,
                "task_id": call["task_id"],
                "reviewer": self.agent_id,
                "verdict": verdict,
                "score": score,
                "findings": findings,
                "artifact_paths": artifacts,
                "review_utc": reviewed,
                "task_revision": int(call["task_revision"]),
                "task_sha256": str(call["task_sha256"]),
            }
            review_sha = stable_sha256(content)
            try:
                connection.execute(
                    """INSERT INTO peer_reviews(
                           review_id, scope, request_id, task_id, reviewer, verdict,
                           score, findings, artifact_paths_json, review_utc,
                           review_sha256, task_revision, task_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        review_id,
                        self.scope,
                        request_id,
                        call["task_id"],
                        self.agent_id,
                        verdict,
                        score,
                        findings,
                        json.dumps(artifacts, ensure_ascii=False),
                        reviewed,
                        review_sha,
                        int(call["task_revision"]),
                        str(call["task_sha256"]),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BridgeError("this peer already reviewed the request") from exc
            if response_text:
                response_sha = stable_sha256(
                    {
                        "request_id": request_id,
                        "response": response_text,
                        "artifact_paths": artifacts,
                    }
                )
                connection.execute(
                    """UPDATE peer_calls SET status='responded', response=?,
                       response_artifact_paths_json=?, response_utc=?, response_sha256=?
                       WHERE scope=? AND request_id=?""",
                    (
                        response_text,
                        json.dumps(artifacts, ensure_ascii=False),
                        reviewed,
                        response_sha,
                        self.scope,
                        request_id,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE peer_calls SET status='responded', response_utc=? WHERE scope=? AND request_id=?",
                    (reviewed, self.scope, request_id),
                )
            self._event(
                connection,
                "peer.reviewed",
                {
                    "request_id": request_id,
                    "review_id": review_id,
                    "review_sha256": review_sha,
                    "verdict": verdict,
                },
                call["task_id"],
            )
        return {
            "request_id": request_id,
            "review_id": review_id,
            "review_sha256": review_sha,
            "verdict": verdict,
            "score": score,
        }

    def review_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        with self._connect() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE scope=? AND task_id=?",
                (self.scope, task_id),
            ).fetchone()
            if task is None:
                raise BridgeError("task not found")
            rows = connection.execute(
                """SELECT r.*, c.recipient, c.requester, c.status AS request_status
                   FROM peer_reviews r JOIN peer_calls c ON c.request_id=r.request_id
                   WHERE r.scope=? AND r.task_id=?
                     AND r.task_revision=? AND r.task_sha256=?
                     AND c.task_revision=? AND c.task_sha256=?
                   ORDER BY r.review_utc ASC""",
                (
                    self.scope,
                    task_id,
                    int(task["task_revision"]),
                    str(task["task_sha256"]),
                    int(task["task_revision"]),
                    str(task["task_sha256"]),
                ),
            ).fetchall()
            stale_review_count = connection.execute(
                """SELECT COUNT(*) AS n FROM peer_reviews
                   WHERE scope=? AND task_id=?
                     AND (task_revision!=? OR task_sha256!=?)""",
                (
                    self.scope,
                    task_id,
                    int(task["task_revision"]),
                    str(task["task_sha256"]),
                ),
            ).fetchone()["n"]
            required_peer_rows = connection.execute(
                """SELECT peer_id FROM task_required_peers
                   WHERE scope=? AND task_id=? ORDER BY peer_id""",
                (self.scope, task_id),
            ).fetchall()
        reviews = []
        for row in rows:
            item = dict(row)
            item["artifact_paths"] = json.loads(item.pop("artifact_paths_json"))
            reviews.append(item)
        presence = self.presence_snapshot()
        online = {
            agent
            for agent in presence["online_agents"]
            if agent not in {task["claimed_by"], "human-operator"}
        }
        approved = {row["reviewer"] for row in rows if row["verdict"] == "approved"}
        required_peer = task["required_peer"]
        required_peers = {row["peer_id"] for row in required_peer_rows}
        if required_peer:
            required_peers.add(required_peer)
        review_quorum = max(1, int(task["review_quorum"] or 1))
        mode = task["approval_mode"]
        if mode == "solo_allowed":
            ready = True
            reason = "solo_allowed"
        elif mode == "two_party_required":
            ready = required_peer in approved if required_peer else bool(approved)
            reason = "required_peer_approved" if ready else "peer_approval_required"
        elif mode == "quorum_required":
            eligible = required_peers or approved
            approved_count = len(eligible.intersection(approved))
            ready = approved_count >= review_quorum
            reason = "review_quorum_met" if ready else "review_quorum_pending"
        elif required_peers.intersection(online):
            eligible = required_peers.intersection(online)
            needed = min(review_quorum, len(eligible))
            ready = len(eligible.intersection(approved)) >= needed
            reason = "online_review_quorum_met" if ready else "online_review_quorum_pending"
        elif online:
            needed = min(review_quorum, len(online))
            ready = len(online.intersection(approved)) >= needed
            reason = "online_peer_quorum_met" if ready else "online_peer_quorum_pending"
        else:
            ready = True
            reason = "peer_offline_solo_fallback"
        return {
            "task_id": task_id,
            "approval_mode": mode,
            "required_peer": required_peer,
            "required_peers": sorted(required_peers),
            "review_quorum": review_quorum,
            "task_revision": int(task["task_revision"]),
            "task_sha256": str(task["task_sha256"]),
            "reviews": reviews,
            "stale_review_count": int(stale_review_count),
            "approved_reviewers": sorted(approved),
            "online_peer_agents": sorted(online),
            "ready_for_completion": ready,
            "policy_reason": reason,
        }

    def read_artifact(self, args: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_path(args.get("path"))
        if self._is_protected(normalized):
            raise BridgeError("protected or sensitive files are not exposed")
        resolved = self._resolve_path(normalized, must_exist=True)
        max_bytes = max(1, min(int(args.get("max_bytes", 100_000)), 500_000))
        hashed = self._hash_file_streaming(
            resolved,
            prefix_bytes=max_bytes,
            max_bytes=MAX_MCP_HASH_BYTES,
            max_seconds=MAX_MCP_HASH_SECONDS,
        )
        if contains_secret_bytes(hashed["prefix"]):
            raise BridgeError("artifact content appears to contain a credential or private key")
        result: dict[str, Any] = {
            "path": normalized,
            "bytes": hashed["bytes"],
            "sha256": hashed["sha256"],
            "truncated": hashed["bytes"] > max_bytes,
        }
        try:
            result["text"] = hashed["prefix"].decode("utf-8")
        except UnicodeDecodeError:
            result["binary"] = True
        return result

    def hash_artifact(self, args: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_path(args.get("path"))
        if self._is_protected(normalized):
            raise BridgeError("protected or sensitive files are not exposed")
        resolved = self._resolve_path(normalized, must_exist=True)
        hashed = self._hash_file_streaming(
            resolved,
            max_bytes=MAX_MCP_HASH_BYTES,
            max_seconds=MAX_MCP_HASH_SECONDS,
        )
        return {
            "path": normalized,
            "bytes": hashed["bytes"],
            "sha256": hashed["sha256"],
        }

    def record_proof(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        summary = _require_text(args.get("change_summary"), "change_summary", limit=20_000)
        tests = _require_text(args.get("tests"), "tests", limit=20_000)
        changed_paths = [
            self._normalize_path(item)
            for item in _json_list(args.get("changed_paths", []), "changed_paths")
        ]
        if any(self._is_protected(path) for path in changed_paths):
            raise BridgeError("changed_paths contains a protected or sensitive target")
        evidence_paths = self._clean_artifacts(args.get("evidence_paths", []))
        if not changed_paths and not evidence_paths:
            raise BridgeError("proof needs at least one changed path or evidence artifact")
        raw_before_hashes = args.get("before_hashes", {})
        if not isinstance(raw_before_hashes, dict):
            raise BridgeError("before_hashes must be an object")
        before_hashes: dict[str, str | None] = {}
        for raw_path, raw_hash in raw_before_hashes.items():
            path = self._normalize_path(raw_path)
            if path not in changed_paths:
                raise BridgeError(
                    "before_hashes keys must identify entries in changed_paths"
                )
            if path in before_hashes:
                raise BridgeError("before_hashes contains duplicate normalized paths")
            before_hashes[path] = (
                None
                if raw_hash is None
                else _require_sha256(raw_hash, f"before_hashes[{path}]")
            )
        if set(before_hashes) != set(changed_paths):
            raise BridgeError(
                "before_hashes must contain exactly one entry for every changed path"
            )
        review_ids = [
            _require_identifier(item, "review_id")
            for item in _json_list(args.get("review_ids", []), "review_ids")
        ]
        with self._connect() as connection:
            self._require_lease(connection, task_id, args.get("lease_token"))
            initial_write_scopes = [
                row["path_prefix"]
                for row in connection.execute(
                    "SELECT path_prefix FROM task_paths WHERE scope=? AND task_id=? AND access='write'",
                    (self.scope, task_id),
                ).fetchall()
            ]
        for path in changed_paths:
            if not any(
                self._path_within(path, prefix) for prefix in initial_write_scopes
            ):
                raise BridgeError(f"changed path is outside the task write scope: {path}")
        hashed_paths = self._hash_proof_files([*changed_paths, *evidence_paths])
        after_hashes = {
            path: str(hashed_paths[path]["sha256"]) for path in changed_paths
        }
        evidence_hashes = {
            path: str(hashed_paths[path]["sha256"]) for path in evidence_paths
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._require_lease(connection, task_id, args.get("lease_token"))
            write_scopes = [
                row["path_prefix"]
                for row in connection.execute(
                    "SELECT path_prefix FROM task_paths WHERE scope=? AND task_id=? AND access='write'",
                    (self.scope, task_id),
                ).fetchall()
            ]
            for path in changed_paths:
                if not any(self._path_within(path, prefix) for prefix in write_scopes):
                    raise BridgeError(f"changed path is outside the task write scope: {path}")
            for path, hashed in hashed_paths.items():
                current = self._resolve_path(path, must_exist=True).stat()
                if (current.st_size, current.st_mtime_ns) != tuple(hashed["identity"]):
                    raise BridgeError(
                        f"artifact changed before proof recording: {path}"
                    )
            record_id = uuid.uuid4().hex
            recorded = utc_now()
            content = {
                "record_id": record_id,
                "scope": self.scope,
                "task_id": task_id,
                "actor": self.agent_id,
                "change_summary": summary,
                "changed_paths": changed_paths,
                "before_hashes": before_hashes,
                "after_hashes": after_hashes,
                "tests": tests,
                "evidence_paths": evidence_paths,
                "evidence_hashes": evidence_hashes,
                "approval_mode": task["approval_mode"],
                "review_ids": review_ids,
                "recorded_utc": recorded,
            }
            record_sha = stable_sha256(content)
            connection.execute(
                """INSERT INTO integration_records(
                    record_id, scope, task_id, actor, change_summary,
                    changed_paths_json, before_hashes_json, after_hashes_json,
                    tests, evidence_paths_json, approval_mode, review_ids_json,
                    recorded_utc, record_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id,
                    self.scope,
                    task_id,
                    self.agent_id,
                    summary,
                    json.dumps(changed_paths, ensure_ascii=False),
                    json.dumps(before_hashes, ensure_ascii=False, sort_keys=True),
                    json.dumps(after_hashes, ensure_ascii=False, sort_keys=True),
                    tests,
                    json.dumps(
                        {"paths": evidence_paths, "hashes": evidence_hashes},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    task["approval_mode"],
                    json.dumps(review_ids, ensure_ascii=False),
                    recorded,
                    record_sha,
                ),
            )
            self._event(
                connection,
                "proof.recorded",
                {"record_id": record_id, "record_sha256": record_sha},
                task_id,
            )
        return {
            "record_id": record_id,
            "record_sha256": record_sha,
            "after_hashes": after_hashes,
            "evidence_hashes": evidence_hashes,
            "bridge_write_performed": False,
        }

    def change_log(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args.get("task_id") or "").strip()
        if task_id:
            task_id = _require_identifier(task_id, "task_id")
        limit = max(1, min(int(args.get("limit", 50)), 500))
        where = "AND task_id=?" if task_id else ""
        params: tuple[Any, ...] = (
            (self.scope, task_id, limit) if task_id else (self.scope, limit)
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM integration_records WHERE scope=? {where}
                    ORDER BY recorded_utc DESC, rowid DESC LIMIT ?""",
                params,
            ).fetchall()
        records = []
        for row in rows:
            item = dict(row)
            for source, target in (
                ("changed_paths_json", "changed_paths"),
                ("before_hashes_json", "before_hashes"),
                ("after_hashes_json", "after_hashes"),
                ("evidence_paths_json", "evidence"),
                ("review_ids_json", "review_ids"),
            ):
                item[target] = json.loads(item.pop(source))
            records.append(item)
        return {"records": records, "count": len(records)}

    def _proofs_are_live(
        self, connection: sqlite3.Connection, task_id: str
    ) -> tuple[bool, list[dict[str, Any]], dict[str, tuple[int, int]]]:
        rows = connection.execute(
            "SELECT * FROM integration_records WHERE scope=? AND task_id=? ORDER BY recorded_utc ASC",
            (self.scope, task_id),
        ).fetchall()
        expected_by_record: list[tuple[sqlite3.Row, dict[str, str]]] = []
        ordered_paths: list[str] = []
        for row in rows:
            after_hashes = json.loads(row["after_hashes_json"])
            evidence = json.loads(row["evidence_paths_json"])
            expected = {**after_hashes, **evidence.get("hashes", {})}
            expected_by_record.append((row, expected))
            ordered_paths.extend(expected)
        existing_paths = [
            path for path in dict.fromkeys(ordered_paths) if self._resolve_path(path).is_file()
        ]
        hashed = self._hash_proof_files(existing_paths) if existing_paths else {}
        identities = {
            path: tuple(item["identity"]) for path, item in hashed.items()
        }
        checks = []
        for row, expected_hashes in expected_by_record:
            drift = []
            for path, expected in expected_hashes.items():
                live = str(hashed[path]["sha256"]) if path in hashed else None
                if live != expected:
                    drift.append({"path": path, "expected": expected, "live": live})
            checks.append(
                {
                    "record_id": row["record_id"],
                    "record_sha256": row["record_sha256"],
                    "tests": row["tests"],
                    "drift": drift,
                }
            )
        return (
            bool(rows) and all(not item["drift"] and item["tests"] for item in checks),
            checks,
            identities,
        )

    def complete_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        with self._connect() as connection:
            self._require_lease(connection, task_id, args.get("lease_token"))
            proof_ready, proof_checks, proof_identities = self._proofs_are_live(
                connection, task_id
            )
            if not proof_ready:
                raise BridgeError(
                    "task proof is missing or has live hash drift: "
                    + json.dumps(proof_checks, ensure_ascii=False, sort_keys=True)
                )
        review = self.review_summary({"task_id": task_id})
        if not review["ready_for_completion"]:
            raise BridgeError(
                f"review gate is not ready: {review['policy_reason']}"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, task_id, args.get("lease_token"))
            live_records = connection.execute(
                "SELECT record_id, record_sha256 FROM integration_records "
                "WHERE scope=? AND task_id=? ORDER BY recorded_utc ASC",
                (self.scope, task_id),
            ).fetchall()
            expected_records = [
                (item["record_id"], item["record_sha256"]) for item in proof_checks
            ]
            if [tuple(row) for row in live_records] != expected_records:
                raise BridgeError("proof records changed while completing the task")
            for path, identity in proof_identities.items():
                resolved = self._resolve_path(path)
                if not resolved.is_file():
                    raise BridgeError("proof changed while completing the task")
                current = resolved.stat()
                if (current.st_size, current.st_mtime_ns) != identity:
                    raise BridgeError("proof changed while completing the task")
            completed = utc_now()
            connection.execute(
                """UPDATE tasks SET status='complete', claimed_by=NULL,
                   claimed_session_id=NULL, lease_token_sha256=NULL,
                   lease_expires_epoch=NULL, claimed_utc=NULL, updated_utc=?
                   WHERE scope=? AND task_id=?""",
                (completed, self.scope, task_id),
            )
            event = self._event(
                connection,
                "task.completed",
                {
                    "proof_records": [item["record_sha256"] for item in proof_checks],
                    "review_policy_reason": review["policy_reason"],
                    "approved_reviewers": review["approved_reviewers"],
                },
                task_id,
            )
        return {
            "task_id": task_id,
            "status": "complete",
            "completed_utc": completed,
            "proof_checks": proof_checks,
            "review": review,
            "completion_chain_sha256": event["chain_sha256"],
        }

    def _write_draft(self, task_id: str, filename: str, data: bytes) -> dict[str, Any]:
        normalized_task = _require_identifier(task_id, "task_id")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
            raise BridgeError("draft filename contains unsupported characters")
        scope_key = sha256_bytes(self.scope.encode("utf-8"))
        task_key = sha256_bytes(
            f"{self.scope}\0{normalized_task}".encode("utf-8")
        )
        draft_root = self.draft_root.resolve()
        task_root = (
            self.draft_root
            / f"scope-{scope_key}"
            / f"task-{task_key}"
        ).resolve()
        if not task_root.is_relative_to(draft_root):
            raise BridgeError("draft path escaped local state")
        task_root.mkdir(parents=True, exist_ok=True)
        owner = {
            "schema": "peerbridge.draft-owner.v1",
            "scope_sha256": scope_key,
            "task_id_sha256": sha256_bytes(normalized_task.encode("utf-8")),
            "task_path_key_sha256": task_key,
        }
        owner_bytes = (
            json.dumps(owner, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        owner_path = task_root / "OWNER.json"
        try:
            descriptor = os.open(
                owner_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                existing_owner = owner_path.read_bytes()
            except OSError as exc:
                raise BridgeError("draft owner manifest is unreadable") from exc
            if not hmac.compare_digest(existing_owner, owner_bytes):
                raise BridgeError("draft directory ownership mismatch")
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(owner_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        destination = task_root / filename
        temporary = destination.with_name(f".{filename}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        return {
            "path": destination.relative_to(self.state_root).as_posix(),
            "bytes": len(data),
            "sha256": sha256_bytes(data),
        }

    def submit_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        plan = _require_text(args.get("plan"), "plan")
        with self._connect() as connection:
            self._require_lease(connection, task_id, args.get("lease_token"))
        draft = self._write_draft(task_id, "PLAN.md", plan.encode("utf-8"))
        with self._connect() as connection:
            self._event(connection, "plan.submitted", draft, task_id)
        return {"task_id": task_id, "draft": draft, "project_write_performed": False}

    def submit_patch(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_identifier(args.get("task_id"), "task_id")
        summary = _require_text(args.get("change_summary"), "change_summary")
        patch = _require_text(args.get("patch"), "patch", limit=500_000)
        if PATCH_DESTRUCTIVE.search(patch):
            raise BridgeError("destructive or repository-control patch is not accepted")
        targets = [
            self._normalize_path(item)
            for item in _json_list(args.get("target_paths"), "target_paths")
        ]
        if not targets:
            raise BridgeError("target_paths must be a non-empty array")
        if any(self._is_protected(path) for path in targets):
            raise BridgeError("patch targets a protected or sensitive path")
        with self._connect() as connection:
            task = self._require_lease(connection, task_id, args.get("lease_token"))
            write_scopes = [
                row["path_prefix"]
                for row in connection.execute(
                    "SELECT path_prefix FROM task_paths WHERE scope=? AND task_id=? AND access='write'",
                    (self.scope, task_id),
                ).fetchall()
            ]
            for target in targets:
                if not any(self._path_within(target, prefix) for prefix in write_scopes):
                    raise BridgeError(f"patch target is outside the task write scope: {target}")
        draft = self._write_draft(task_id, "PATCH.diff", patch.encode("utf-8"))
        metadata = {
            "task_id": task_id,
            "target_paths": targets,
            "change_summary": summary,
            "patch": draft,
            "approval_mode": task["approval_mode"],
            "project_write_performed": False,
        }
        meta = self._write_draft(
            task_id,
            "PATCH_METADATA.json",
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )
        with self._connect() as connection:
            self._event(
                connection,
                "patch.submitted",
                {"patch": draft, "metadata": meta, "target_paths": targets},
                task_id,
            )
        return {"task_id": task_id, "patch": draft, "metadata": meta, "project_write_performed": False}

    def verify_audit_chain(self, _args: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            available = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE scope=?", (self.scope,)
                ).fetchone()[0]
            )
            if available > MAX_MCP_AUDIT_EVENTS:
                raise BridgeError(
                    "audit chain exceeds the interactive verification event budget"
                )
            rows = connection.execute(
                "SELECT * FROM events WHERE scope=? ORDER BY sequence ASC",
                (self.scope,),
            )
            previous = ZERO_SHA256
            errors = []
            error_count = 0
            event_count = 0
            deadline = time.monotonic() + MAX_MCP_AUDIT_SECONDS
            for row in rows:
                if time.monotonic() > deadline:
                    raise BridgeError(
                        "audit chain verification exceeded the interactive time budget"
                    )
                event_count += 1
                payload_sha = sha256_bytes(row["payload_json"].encode("utf-8"))
                envelope = {
                    "event_id": row["event_id"],
                    "scope": row["scope"],
                    "actor": row["actor"],
                    "event_type": row["event_type"],
                    "task_id": row["task_id"],
                    "payload_sha256": payload_sha,
                    "created_utc": row["created_utc"],
                    "prev_chain_sha256": previous,
                }
                chain_sha = stable_sha256(envelope)
                for mismatch, failed in (
                    ("payload_sha256", row["payload_sha256"] != payload_sha),
                    ("prev_chain_sha256", row["prev_chain_sha256"] != previous),
                    ("chain_sha256", row["chain_sha256"] != chain_sha),
                ):
                    if failed:
                        error_count += 1
                        if len(errors) < 100:
                            errors.append(
                                {"sequence": row["sequence"], "error": mismatch}
                            )
                previous = row["chain_sha256"]
        return {
            "valid": error_count == 0,
            "event_count": event_count,
            "head_chain_sha256": previous,
            "errors": errors,
            "error_count": error_count,
            "errors_truncated": error_count > len(errors),
            "writes_performed": 0,
        }
