"""Loopback-only desktop workbench for the PeerBridge local coordination core.

This module intentionally keeps the trusted Python/SQLite/MCP boundary intact.
The native WebView2 shell renders redacted room state and sends messages through
the same MCP tool path used by the legacy monitor; it never receives provider
credentials. A browser shell remains an explicit compatibility fallback.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__
from .attachments import (
    MAX_CHAT_ATTACHMENT_COUNT,
    MAX_CHAT_ATTACHMENT_TOTAL_BYTES,
    AttachmentError,
    StagedAttachment,
    stage_chat_attachment_payloads,
)
from .announcements import (
    Announcement,
    AnnouncementConfig,
    AnnouncementError,
    SUPPORTED_LOCALES as ANNOUNCEMENT_LOCALES,
    announcement_read_key,
    fail_closed_announcement_preferences,
    fetch_announcements,
    load_announcement_cache,
    load_announcement_preferences,
    save_announcement_cache,
    save_announcement_preferences,
)
from .approval_broker import APPROVAL_DECISIONS, APPROVAL_MODES
from .bridge import (
    DEFAULT_ROOM_CONTEXT_CHARS,
    DEFAULT_ROOM_CONTEXT_MESSAGES,
    DISCUSSION_ORCHESTRATOR_ID,
    Bridge,
    DEFAULT_ROOM_ID,
)
from .agent_identity import revoke_agent_identity_capability
from .conversation_import import (
    ConversationImportError,
    MAX_IMPORT_BYTES as MAX_HISTORY_IMPORT_BYTES,
    SUPPORTED_PROVIDERS as HISTORY_IMPORT_PROVIDERS,
    conversation_export_metadata,
    import_conversation_export,
    imported_room_view,
    list_conversation_imports,
)
from .codex_history_adapter import discover_codex_threads, import_codex_thread
from .native_history_adapters import import_native_session, list_native_sessions
from .feedback import (
    ALLOWED_ATTACHMENT_SUFFIXES as FEEDBACK_ATTACHMENT_SUFFIXES,
    MAX_ATTACHMENT_BYTES as MAX_FEEDBACK_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_COUNT as MAX_FEEDBACK_ATTACHMENT_COUNT,
    MAX_CREDENTIAL_CHARS,
    MAX_MESSAGE_CHARS as MAX_FEEDBACK_MESSAGE_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TOTAL_ATTACHMENT_BYTES as MAX_FEEDBACK_ATTACHMENT_TOTAL_BYTES,
    FeedbackConfig,
    FeedbackError,
    create_feedback_bundle,
    deliver_feedback_bundle,
)
from .http_limits import BoundedThreadingHTTPServer
from .desktop_cockpit import (
    COCKPIT_DEFAULT_LAUNCH_ROLE,
    COCKPIT_ROLES,
    cockpit_working_directory,
)
from .execution_governance import (
    ExecutionGovernance,
    GovernanceError,
    repository_resource_key,
)
from .agent_install import (
    AgentInstallError,
    detect_installable_agent,
    detect_official_agent,
    installable_agent_spec,
    launch_agent_installer,
    official_agent_spec,
)
from .agent_adapters import official_agent_adapter_descriptors
from .ccswitch import (
    SUPPORTED_APPS as CCSWITCH_SUPPORTED_APPS,
    CcSwitchError,
    fetch_models as ccswitch_fetch_models,
    list_providers as ccswitch_list_providers,
    resolve_route_identity as ccswitch_resolve_route_identity,
    switch_provider as ccswitch_switch_provider,
)
from .credentials import (
    CredentialStoreError,
    store_local_provider_endpoint,
    store_provider_credentials,
)
from .managed_agents import (
    TERMINAL_STATES,
    ManagedAgentError,
    ManagedAgentManager,
    build_observe_launch,
)
from .localization import (
    LocalizationError,
    SUPPORTED_LOCALES,
    load_preferences,
    save_preferences,
)
from .official_agent_runtime import HybridManagedAgentManager
from .monitor import (
    APP_BUILD_SHA256,
    BridgeReader,
    HUMAN_AGENT_ID,
    McpHumanClient,
    ccswitch_route_specs,
    configure_windows_app_identity,
    release_windows_icon_handles,
)
from .openai_compatible_runner import RunnerError, discover_provider_models
from .operation_queue import WORKFLOW_TEMPLATES
from .secret_scan import redact_secrets
from .updates import check_for_updates
from .wsl_sandbox import clear_wsl_sandbox_probe_cache, probe_wsl_sandbox


MAX_REQUEST_BYTES = 24 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_BODY_CHARS = 20_000
MAX_WRITES_PER_MINUTE = 20
MAX_IDEMPOTENCY_RECORDS = 256
MAX_HISTORY_SELECTIONS = 512
HISTORY_SELECTION_TTL_SECONDS = 10 * 60
MANAGED_SESSION_AUTHORIZATION_SECONDS = 12 * 60 * 60
MAX_WORKTREE_DIFF_BYTES = 512 * 1024
MAX_WORKTREE_DIFF_FILES = 160
MAX_WORKTREE_GIT_METADATA_BYTES = 512 * 1024
MAX_PROVIDER_MODEL_OPTIONS = 500
MAX_DISCOVERED_AGENT_CAPABILITIES = 240
MAX_SKILL_MANIFEST_BYTES = 1024 * 1024
SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
SAFE_ROUTE = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
SAFE_MODEL_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,500}\Z")
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:[\\/][^\r\n]*")
POSIX_PRIVATE_PATH = re.compile(
    r"(?<!\w)/(?:Users|home|var|tmp|etc|private/(?:var|tmp))/[^\s\"']+"
)
ALLOWED_PRIORITIES = frozenset({"low", "normal", "high", "critical"})


def _skill_frontmatter(path: Path) -> tuple[str, str, str] | None:
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_SKILL_MANIFEST_BYTES or path.is_symlink():
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    name = path.parent.name
    description = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                clean = value.strip().strip("'\"")
                if key.strip() == "name" and clean:
                    name = clean
                elif key.strip() == "description" and clean:
                    description = clean
    return name[:160], description[:500], hashlib.sha256(raw).hexdigest()


def _native_skill_roots(
    project_root: Path, *, home: Path | None = None
) -> tuple[tuple[str, str, Path], ...]:
    home = home or Path.home()
    return (
        ("codex", "personal", home / ".codex" / "skills"),
        ("shared", "personal", home / ".agents" / "skills"),
        ("claude", "personal", home / ".claude" / "skills"),
        ("grok", "personal", home / ".grok" / "skills"),
        ("kimi", "personal", home / ".kimi" / "skills"),
        ("codex", "project", project_root / ".codex" / "skills"),
        ("shared", "project", project_root / ".agents" / "skills"),
        ("claude", "project", project_root / ".claude" / "skills"),
    )


def _discover_native_agent_capabilities(
    project_root: Path,
    *,
    home: Path | None = None,
    run_cli: bool = True,
) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    seen_manifests: set[Path] = set()
    for source_agent, scope, root in _native_skill_roots(project_root, home=home):
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            manifests = sorted(root.rglob("SKILL.md"))
        except OSError:
            continue
        for manifest in manifests:
            if len(skills) >= MAX_DISCOVERED_AGENT_CAPABILITIES:
                break
            try:
                resolved = manifest.resolve(strict=True)
                resolved.relative_to(root.resolve(strict=True))
            except (OSError, ValueError):
                continue
            if resolved in seen_manifests:
                continue
            seen_manifests.add(resolved)
            parsed = _skill_frontmatter(resolved)
            if parsed is None:
                continue
            name, description, source_sha256 = parsed
            capability_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", resolved.parent.name).strip("-")
            if not capability_id:
                capability_id = source_sha256[:16]
            skills.append(
                {
                    "kind": "skill",
                    "capability_id": capability_id[:200],
                    "display_name": name,
                    "description": description,
                    "source_agent": source_agent,
                    "scope": scope,
                    "source_sha256": source_sha256,
                    "available": True,
                }
            )

    servers: list[dict[str, Any]] = []
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    codex = shutil.which("codex") if run_cli else None
    if codex:
        try:
            completed = subprocess.run(
                [codex, "mcp", "list", "--json"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=creation_flags,
            )
            payload = json.loads(completed.stdout) if completed.returncode == 0 else []
            for item in payload if isinstance(payload, list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not SAFE_IDENTIFIER.fullmatch(name):
                    continue
                transport = item.get("transport") or {}
                servers.append(
                    {
                        "kind": "mcp-server",
                        "capability_id": name,
                        "display_name": name,
                        "source_agent": "codex",
                        "transport": str(transport.get("type") or "unknown")[:40]
                        if isinstance(transport, dict)
                        else "unknown",
                        "available": bool(item.get("enabled")),
                    }
                )
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass

    claude = shutil.which("claude") if run_cli else None
    if claude:
        try:
            completed = subprocess.run(
                [claude, "mcp", "list"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=creation_flags,
            )
            for line in completed.stdout.splitlines():
                match = re.match(r"^([A-Za-z0-9_.:-]{1,200}):.*? - ([^ ]+)", line.strip())
                if not match:
                    continue
                marker = match.group(2)
                servers.append(
                    {
                        "kind": "mcp-server",
                        "capability_id": match.group(1),
                        "display_name": match.group(1),
                        "source_agent": "claude",
                        "transport": "official-cli",
                        "available": marker not in {"×", "⏸"},
                    }
                )
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "skills": skills,
        "mcp_servers": servers[:MAX_DISCOVERED_AGENT_CAPABILITIES],
        "skill_count": len(skills),
        "mcp_server_count": min(len(servers), MAX_DISCOVERED_AGENT_CAPABILITIES),
    }
ROOM_ROLE_IDS = frozenset(
    {"equal-participant", "researcher", "implementer", "reviewer", "custom"}
)
MANAGED_AGENT_IDS = frozenset({"codex", "claude-code", "grok", "kimi-code"})
MANAGED_AGENT_ORDER = ("codex", "claude-code", "grok", "kimi-code")
PRIMARY_OFFICIAL_AGENT_IDS = frozenset({"codex", "claude-code", "grok", "kimi-code"})
MANAGED_PERMISSION_TIERS = frozenset(
    {"observe", "review", "edit", "full-development"}
)
IDENTITY_PROFILES = frozenset({"observer", "collaborator"})
RESERVED_IDENTITY_IDS = frozenset(
    {
        HUMAN_AGENT_ID,
        "control-room-workflow",
        "control-room-migrator",
        "mailbox-supervisor",
        DISCUSSION_ORCHESTRATOR_ID,
    }
)
AGENT_CATALOG_CACHE_SECONDS = 300
ASSET_DIRECTORY = Path(__file__).with_name("workbench")
BRAND_ASSET_DIRECTORY = Path(__file__).with_name("release_support")

_AGENT_CATALOG_CACHE_LOCK = threading.Lock()
_AGENT_CATALOG_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class WorkbenchError(ValueError):
    """An operator-safe local workbench error."""


@dataclass(frozen=True)
class _GitCommandResult:
    returncode: int
    stdout: bytes
    truncated: bool
    timed_out: bool


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def _redact(value: Any) -> str:
    return redact_secrets("" if value is None else str(value), "[REDACTED CREDENTIAL]")


def _public_text(value: Any) -> str:
    """Redact credentials and machine-local absolute paths from UI text."""
    text = _redact(value)
    text = WINDOWS_PATH.sub("[LOCAL PATH]", text)
    return POSIX_PRIVATE_PATH.sub("[LOCAL PATH]", text)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_git_executable(project_root: Path) -> Path | None:
    """Resolve Git from explicit absolute PATH entries, never from the current directory."""

    root = project_root.resolve()
    executable_names = ("git.exe",) if sys.platform == "win32" else ("git",)
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        normalized = os.path.expandvars(raw_directory.strip().strip('"'))
        if not normalized:
            continue
        directory = Path(normalized)
        if not directory.is_absolute():
            continue
        for executable_name in executable_names:
            try:
                candidate = (directory / executable_name).resolve(strict=True)
            except OSError:
                continue
            if not candidate.is_file() or _path_is_within(candidate, root):
                continue
            if sys.platform != "win32" and not os.access(candidate, os.X_OK):
                continue
            return candidate
    return None


def _git_command(
    project_root: Path,
    *arguments: str,
    output_limit: int,
) -> _GitCommandResult:
    executable = _resolve_git_executable(project_root)
    if executable is None:
        raise FileNotFoundError("trusted Git executable is unavailable")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        }
    )
    process = subprocess.Popen(
        [str(executable), "-C", str(project_root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        close_fds=True,
        creationflags=(
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if sys.platform == "win32"
            else 0
        ),
    )
    captured = bytearray()
    truncated = threading.Event()

    def read_output() -> None:
        stream = process.stdout
        if stream is None:
            return
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            remaining = output_limit - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0) or len(captured) >= output_limit:
                truncated.set()
                try:
                    process.terminate()
                except OSError:
                    pass
                return

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=2)
    reader.join(timeout=2)
    if reader.is_alive():
        try:
            process.kill()
        except OSError:
            pass
        reader.join(timeout=1)
    return _GitCommandResult(
        returncode=int(process.returncode or 0),
        stdout=bytes(captured),
        truncated=truncated.is_set(),
        timed_out=timed_out,
    )


def _worktree_diff(project_root: Path) -> dict[str, Any]:
    """Return a bounded, redacted, read-only Git diff for the local workbench."""

    try:
        probe = _git_command(
            project_root,
            "rev-parse",
            "--is-inside-work-tree",
            output_limit=4096,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return {"available": False, "reason": "git_unavailable", "files": [], "patch": ""}
    if probe.returncode != 0 or probe.stdout.strip() != b"true":
        return {"available": False, "reason": "not_git_repository", "files": [], "patch": ""}

    exclusions = (
        ":(exclude).peerbridge/**",
        ":(exclude).env",
        ":(exclude).env.*",
        ":(exclude)**/*.pem",
        ":(exclude)**/*.key",
        ":(exclude)**/*.pfx",
        ":(exclude)**/*.p12",
    )
    pathspec = ("--", ".", *exclusions)
    head = _git_command(
        project_root,
        "rev-parse",
        "--short=12",
        "HEAD",
        output_limit=4096,
    )
    status = _git_command(
        project_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        *pathspec,
        output_limit=MAX_WORKTREE_GIT_METADATA_BYTES,
    )
    numstat = _git_command(
        project_root,
        "diff",
        "--numstat",
        "--no-renames",
        "HEAD",
        *pathspec,
        output_limit=MAX_WORKTREE_GIT_METADATA_BYTES,
    )
    patch = _git_command(
        project_root,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        "--unified=3",
        "HEAD",
        *pathspec,
        output_limit=MAX_WORKTREE_DIFF_BYTES,
    )
    if any(
        result.timed_out or (result.returncode != 0 and not result.truncated)
        for result in (status, numstat, patch)
    ):
        return {"available": False, "reason": "git_diff_failed", "files": [], "patch": ""}

    status_by_path: dict[str, str] = {}
    for line in status.stdout.decode("utf-8", errors="replace").splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip() or "M"
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if path:
            status_by_path[path.replace("\\", "/")] = code

    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_additions = 0
    total_deletions = 0
    for line in numstat.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path = parts
        normalized_path = path.replace("\\", "/")
        binary = added_raw == "-" or deleted_raw == "-"
        additions = None if binary else _safe_int(added_raw)
        deletions = None if binary else _safe_int(deleted_raw)
        if additions is not None:
            total_additions += additions
        if deletions is not None:
            total_deletions += deletions
        files.append(
            {
                "path": _public_text(normalized_path),
                "status": status_by_path.get(normalized_path, "M"),
                "additions": additions,
                "deletions": deletions,
                "binary": binary,
            }
        )
        seen.add(normalized_path)
        if len(files) >= MAX_WORKTREE_DIFF_FILES:
            break
    for path, code in sorted(status_by_path.items()):
        if path in seen or len(files) >= MAX_WORKTREE_DIFF_FILES:
            continue
        files.append(
            {
                "path": _public_text(path),
                "status": code,
                "additions": None,
                "deletions": None,
                "binary": False,
            }
        )

    raw_patch = patch.stdout
    patch_truncated = patch.truncated
    bounded_patch = raw_patch.decode("utf-8", errors="replace")
    redacted_patch = _public_text(bounded_patch)
    return {
        "available": True,
        "reason": "",
        "head": head.stdout.decode("ascii", errors="ignore").strip(),
        "dirty": bool(status_by_path),
        "additions": total_additions,
        "deletions": total_deletions,
        "file_count": len(status_by_path),
        "files": files,
        "files_truncated": bool(
            status.truncated or numstat.truncated or len(status_by_path) > len(files)
        ),
        "patch": redacted_patch,
        "patch_truncated": patch_truncated,
        "patch_sha256": (
            hashlib.sha256(raw_patch).hexdigest() if not patch_truncated else ""
        ),
        "bounded_patch_sha256": hashlib.sha256(raw_patch).hexdigest(),
    }


def _safe_action_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_action_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_action_value(item) for item in value]
    if isinstance(value, str):
        return _public_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _public_text(value)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _default_approval_mode(permission_tier: object) -> str:
    value = str(permission_tier or "observe")
    if value == "full-development":
        return "full-access"
    if value == "edit":
        return "agent-delegated"
    return "approval-required"


def _bounded_model_ids(values: Any) -> tuple[tuple[str, ...], bool]:
    rows: list[str] = []
    seen: set[str] = set()
    for raw_value in values or ():
        value = str(raw_value or "").strip()
        if not value or not SAFE_MODEL_ID.fullmatch(value) or value in seen:
            continue
        if len(rows) >= MAX_PROVIDER_MODEL_OPTIONS:
            return tuple(rows), True
        rows.append(value)
        seen.add(value)
    return tuple(rows), False


def _valid_sha256(value: Any) -> str | None:
    normalized = str(value or "").lower()
    return normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else None


def _latest_agent_receipt(project_root: Path, agent_id: str) -> dict[str, Any] | None:
    patterns = {
        "codex": "alpha52-codex-e2e-*.json",
        "claude-code": "alpha52-claude-native-e2e-*.json",
        "grok": "alpha52-grok-e2e-*.json",
    }
    pattern = patterns.get(agent_id)
    if pattern is None:
        return None
    receipt_root = project_root / ".peerbridge" / "receipts"
    try:
        candidates = sorted(
            receipt_root.glob(pattern),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return None
    for path in candidates:
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        embedded_sha = _valid_sha256(payload.get("receipt_sha256"))
        safe: dict[str, Any] = {
            "available": True,
            "receipt_sha256": embedded_sha or hashlib.sha256(raw).hexdigest(),
            "created_utc": str(payload.get("created_utc") or ""),
            "real_inference_verified": False,
            "mcp_tool_verified": False,
            "official_client_verified": False,
            "provider_identity_attested": False,
            "persistent_session_verified": False,
            "sandbox_verified": False,
            "observed_model": "",
            "observed_reasoning": "",
            "observed_version": "",
            "runtime": "",
        }
        if agent_id == "claude-code":
            transcript = payload.get("transcript") or {}
            bridge = payload.get("bridge") or {}
            identity = bridge.get("runtime_identity") or {}
            client = payload.get("client") or {}
            safe.update(
                {
                    "created_utc": str(payload.get("created_utc") or ""),
                    "real_inference_verified": bool(
                        transcript.get("real_model_inference_observed")
                    ),
                    "mcp_tool_verified": bool(
                        transcript.get("mcp_tool_invocation_observed")
                        and bridge.get("tool")
                    ),
                    "official_client_verified": bool(
                        payload.get("official_native_client_observed")
                    ),
                    "provider_identity_attested": bool(
                        payload.get("upstream_provider_identity_attested")
                    ),
                    "persistent_session_verified": False,
                    "sandbox_verified": bool(
                        not payload.get("credential_contents_recorded")
                        and not payload.get("thinking_contents_recorded")
                    ),
                    "observed_model": _public_text(
                        transcript.get("observed_model_id")
                        or identity.get("model_id")
                    ),
                    "observed_reasoning": _public_text(
                        identity.get("reasoning_mode")
                    ),
                    "observed_version": _public_text(
                        transcript.get("claude_code_version")
                        or client.get("version_output")
                    ),
                    "runtime": "claude-code-native",
                }
            )
        else:
            expected_name = "@agentclientprotocol/codex-acp" if agent_id == "codex" else "grok-build"
            safe.update(
                {
                    "real_inference_verified": bool(
                        _safe_int(payload.get("response_chars")) > 0
                        and _valid_sha256(payload.get("response_sha256"))
                    ),
                    "mcp_tool_verified": bool(
                        _safe_int(payload.get("mcp_tool_call_count")) > 0
                        and _safe_int(payload.get("mcp_tool_error_count")) == 0
                    ),
                    "official_client_verified": str(
                        payload.get("observed_agent_name") or ""
                    )
                    == expected_name,
                    "provider_identity_attested": bool(
                        payload.get("requested_provider_id")
                    ),
                    "persistent_session_verified": bool(
                        agent_id == "codex"
                        and payload.get("acp_session_transition_observed")
                    ),
                    "sandbox_verified": bool(
                        payload.get("filesystem_capability") is False
                        and payload.get("terminal_capability") is False
                    ),
                    "observed_model": _public_text(payload.get("observed_model")),
                    "observed_reasoning": _public_text(
                        payload.get("observed_reasoning_mode")
                    ),
                    "observed_version": _public_text(
                        payload.get("observed_agent_version")
                    ),
                    "runtime": _public_text(payload.get("runtime")),
                }
            )
        return safe
    return None


def _native_agent_contract(
    agent_id: str,
    *,
    installed: bool,
    observe_dependencies_ready: bool,
) -> dict[str, Any]:
    profile_ready = installed and observe_dependencies_ready
    return {
        "client_origin": "official",
        "transport": (
            "official_cli_via_acpx"
            if agent_id in {"grok", "kimi-code"}
            else "direct_official_cli"
        ),
        "managed_session_mode": "persistent",
        "input_transport": (
            "acpx_named_session"
            if agent_id in {"grok", "kimi-code"}
            else "official_persistent_protocol"
        ),
        "read_only_profile_ready": profile_ready,
        "model_route_configurable": profile_ready,
        "resume_mapped": profile_ready,
        "attachment_input": "configured" if profile_ready else "unavailable",
        "write_profile": "governed_worktree" if profile_ready else "unavailable",
    }


def _capability_rows(
    agent_id: str,
    receipt: dict[str, Any] | None,
    *,
    installed: bool,
    observe_dependencies_ready: bool,
) -> list[dict[str, str]]:
    verified = receipt or {}
    profile_ready = installed and observe_dependencies_ready
    status_by_id = {
        "real_inference": (
            "verified" if verified.get("real_inference_verified") else "not_verified"
        ),
        "mcp_tools": "verified" if verified.get("mcp_tool_verified") else "not_verified",
        "model_selection": (
            "verified"
            if verified.get("observed_model")
            else ("configured" if profile_ready else "unavailable")
        ),
        "reasoning_selection": (
            "verified"
            if verified.get("observed_reasoning")
            else "not_verified"
        ),
        "persistent_session": (
            "verified"
            if verified.get("persistent_session_verified")
            else "not_verified"
        ),
        "file_read": "configured" if profile_ready else "unavailable",
        "multimodal_input": "configured" if profile_ready else "unavailable",
        "file_edit": "gated" if profile_ready else "unavailable",
        "shell_tests": "gated" if profile_ready else "unavailable",
        "diff_review": "not_verified",
        "permission_approval": "configured" if profile_ready else "unavailable",
        "session_resume": "configured" if profile_ready else "unavailable",
        "skills": "not_verified",
        "hooks_plugins": "not_verified",
        "subagents": "not_verified",
        "progress_events": "configured" if profile_ready else "unavailable",
        "peerbridge_audit": "verified",
    }
    return [
        {
            "capability_id": capability_id,
            "status": status,
            "evidence": (
                "local_e2e"
                if status == "verified"
                else ("reviewed_launch_profile" if status == "configured" else "pending")
            ),
        }
        for capability_id, status in status_by_id.items()
    ]


def _permission_tiers(
    agent_id: str,
    *,
    installed: bool,
    observe_dependencies_ready: bool,
    wsl_sandbox_verified: bool = False,
) -> list[dict[str, Any]]:
    observe_ready = installed and observe_dependencies_ready
    governed_ready = observe_ready
    edit_status = "gated" if governed_ready else "unavailable"
    standard_boundaries = {
        "codex": "codex_workspace_write_on_request",
        "claude-code": "claude_accept_edits_native_policy",
        "grok": "acpx_scoped_edit_policy",
        "kimi-code": "acpx_scoped_edit_policy",
    }
    full_boundaries = {
        "codex": "codex_workspace_write_session_trusted",
        "claude-code": "claude_bypass_permissions_session_trusted",
        "grok": "acpx_approve_all_session_trusted",
        "kimi-code": "acpx_approve_all_session_trusted",
    }
    # Retain the parameter for compatibility with older bootstrap receipts. Write
    # availability now follows each official client's native permission contract.
    _ = wsl_sandbox_verified
    return [
        {
            "tier_id": "observe",
            "status": "verified" if observe_ready else "unavailable",
            "launchable": observe_ready,
            "workspace_access": "read-only",
            "network_access": False,
            "approval_behavior": "no-mutation",
        },
        {
            "tier_id": "review",
            "status": "verified" if observe_ready else "unavailable",
            "launchable": observe_ready,
            "workspace_access": "read-only",
            "network_access": False,
            "approval_behavior": "no-mutation",
        },
        {
            "tier_id": "edit",
            "status": edit_status,
            "launchable": governed_ready,
            "requires_governance_binding": True,
            "workspace_access": "governed-isolated-worktree",
            "network_access": True,
            "approval_behavior": "provider-native-standard",
            "unavailable_reason": None,
            "security_boundary": standard_boundaries[agent_id],
        },
        {
            "tier_id": "full-development",
            "status": edit_status,
            "launchable": governed_ready,
            "requires_governance_binding": True,
            "workspace_access": "governed-isolated-worktree",
            "network_access": True,
            "approval_behavior": "once-per-session",
            "unavailable_reason": None,
            "security_boundary": full_boundaries[agent_id],
        },
    ]


def _build_managed_agent_catalog(project_root: Path) -> list[dict[str, Any]]:
    adapter_descriptors = {
        descriptor.agent_id: descriptor
        for descriptor in official_agent_adapter_descriptors()
    }
    try:
        acpx_status = detect_installable_agent("acpx-runtime")
        acpx_ready = bool(acpx_status.installed)
    except (AgentInstallError, OSError, RuntimeError):
        acpx_ready = False
    # Native permission contracts are the default. WSL evidence remains visible
    # as an optional hardening signal but does not decide whether Edit is usable.
    wsl_status = None
    rows: list[dict[str, Any]] = []
    for agent_id in MANAGED_AGENT_ORDER:
        spec = official_agent_spec(agent_id)
        try:
            status = detect_official_agent(agent_id)
            installed = bool(status.installed)
            version = _public_text(status.version or "")
        except (AgentInstallError, OSError, RuntimeError):
            installed = False
            version = ""
        receipt = _latest_agent_receipt(project_root, agent_id)
        observe_dependencies_ready = agent_id not in {"grok", "kimi-code"} or acpx_ready
        wsl_agent_verified = bool(
            wsl_status is not None
            and wsl_status.sandbox_verified
            and wsl_status.agent_available.get(agent_id, False)
        )
        peerbridge_mappings = _capability_rows(
            agent_id,
            receipt,
            installed=installed,
            observe_dependencies_ready=observe_dependencies_ready,
        )
        rows.append(
            {
                "agent_id": agent_id,
                "label": spec.display_name,
                "publisher": spec.publisher,
                "adapter": adapter_descriptors[agent_id].as_dict(),
                "docs_url": spec.docs_url,
                "primary": agent_id in PRIMARY_OFFICIAL_AGENT_IDS,
                "installed": installed,
                "version": version,
                "automatic_install_supported": spec.automatic_install_supported,
                "observe_dependencies_ready": observe_dependencies_ready,
                "receipt": receipt,
                "permission_tiers": _permission_tiers(
                    agent_id,
                    installed=installed,
                    observe_dependencies_ready=observe_dependencies_ready,
                    wsl_sandbox_verified=wsl_agent_verified,
                ),
                "wsl_sandbox": {
                    "verified": wsl_agent_verified,
                    "boundary": (
                        "wsl2_bubblewrap_worktree"
                        if wsl_agent_verified
                        else "unavailable"
                    ),
                    "node_version": (
                        _public_text(wsl_status.node_version)
                        if wsl_status is not None and wsl_status.node_version
                        else ""
                    ),
                    "acpx_version": (
                        _public_text(wsl_status.acpx_version)
                        if wsl_status is not None and wsl_status.acpx_version
                        else ""
                    ),
                },
                "native_contract": _native_agent_contract(
                    agent_id,
                    installed=installed,
                    observe_dependencies_ready=observe_dependencies_ready,
                ),
                "peerbridge_mappings": peerbridge_mappings,
                "capabilities": peerbridge_mappings,
            }
        )
    return rows


def managed_agent_catalog(
    project_root: Path, *, force_refresh: bool = False
) -> list[dict[str, Any]]:
    if force_refresh:
        clear_wsl_sandbox_probe_cache()
    cache_key = str(project_root.resolve()).casefold()
    # Hold the lock through the bounded discovery pass. Bootstrap requests can
    # arrive concurrently, but only one of them may launch external CLI probes.
    with _AGENT_CATALOG_CACHE_LOCK:
        now = time.monotonic()
        cached = _AGENT_CATALOG_CACHE.get(cache_key)
        if (
            not force_refresh
            and cached is not None
            and now - cached[0] < AGENT_CATALOG_CACHE_SECONDS
        ):
            return json.loads(json.dumps(cached[1]))
        rows = _build_managed_agent_catalog(project_root)
        _AGENT_CATALOG_CACHE[cache_key] = (time.monotonic(), rows)
        return json.loads(json.dumps(rows))


def _safe_member(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_id": str(row.get("agent_id") or ""),
        "role_id": str(row.get("role_id") or ""),
        "role_label": _redact(row.get("role_label")),
        "provider_id": str(row.get("provider_id") or ""),
        "model_id": str(row.get("model_id") or ""),
        "reasoning_mode": str(row.get("reasoning_mode") or ""),
        "route_class": str(row.get("route_class") or ""),
        "route_profile_id": str(row.get("route_profile_id") or ""),
        "status": str(row.get("status") or ""),
        "online": bool(row.get("online")) or row.get("agent_id") == HUMAN_AGENT_ID,
    }


def _safe_message(row: dict[str, Any]) -> dict[str, Any]:
    artifacts = row.get("artifact_paths")
    return {
        "message_id": str(row.get("message_id") or ""),
        "sequence": _safe_int(row.get("sequence")),
        "sender": str(row.get("sender") or ""),
        "recipient": str(row.get("recipient") or ""),
        "task_id": str(row.get("task_id") or ""),
        "subject": _redact(row.get("subject")),
        "body": _redact(row.get("body")),
        "priority": str(row.get("priority") or "normal"),
        "created_utc": str(row.get("created_utc") or ""),
        "content_sha256": str(row.get("content_sha256") or ""),
        "route_status": str(row.get("route_status") or ""),
        "route_profile_id": str(row.get("route_profile_id") or ""),
        "requested_provider_id": str(row.get("requested_provider_id") or ""),
        "requested_model_id": str(row.get("requested_model_id") or ""),
        "requested_reasoning_mode": str(row.get("requested_reasoning_mode") or ""),
        "observed_provider_id": str(row.get("observed_provider_id") or ""),
        "observed_model_id": str(row.get("observed_model_id") or ""),
        "observed_reasoning_mode": str(row.get("observed_reasoning_mode") or ""),
        "acknowledged": bool(row.get("acknowledged")),
        "artifact_count": len(artifacts) if isinstance(artifacts, list) else 0,
    }


def _safe_dispatch(row: dict[str, Any]) -> dict[str, Any]:
    """Expose operator-useful delivery state without lease or receipt secrets."""
    return {
        "message_id": str(row.get("message_id") or ""),
        "agent_id": str(row.get("agent_id") or ""),
        "status": str(row.get("status") or ""),
        "attempt_count": _safe_int(row.get("attempt_count")),
        "updated_utc": str(row.get("updated_utc") or ""),
        "completed_utc": str(row.get("completed_utc") or ""),
        "reply_message_id": str(row.get("reply_message_id") or ""),
        "error_code": str(row.get("error_code") or ""),
        "dispatch_sha256": str(row.get("dispatch_sha256") or ""),
    }


def _safe_task(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(row.get("task_id") or ""),
        "summary": _redact(row.get("summary")),
        "status": str(row.get("status") or ""),
        "claimed_by": str(row.get("claimed_by") or ""),
        "approval_mode": str(row.get("approval_mode") or ""),
        "updated_utc": str(row.get("updated_utc") or ""),
    }


def _safe_work_update(row: dict[str, Any]) -> dict[str, Any]:
    artifacts = row.get("artifact_paths_json")
    artifact_count = 0
    if isinstance(artifacts, str):
        try:
            decoded = json.loads(artifacts)
            artifact_count = len(decoded) if isinstance(decoded, list) else 0
        except json.JSONDecodeError:
            artifact_count = 0
    return {
        "update_id": str(row.get("update_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "agent_id": str(row.get("agent_id") or ""),
        "status": str(row.get("status") or ""),
        "summary": _redact(row.get("summary")),
        "artifact_count": artifact_count,
        "created_utc": str(row.get("created_utc") or ""),
        "update_sha256": str(row.get("update_sha256") or ""),
    }


def _json_list_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if not isinstance(value, str) or not value:
        return 0
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return 0
    return len(decoded) if isinstance(decoded, list) else 0


def _safe_json_list(value: Any, *, limit: int = 40) -> list[Any]:
    if isinstance(value, list):
        decoded = value
    elif isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
    else:
        return []
    if not isinstance(decoded, list):
        return []
    return decoded[:limit]


def _safe_route(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "route_id": str(row.get("route_id") or ""),
        "agent_id": str(row.get("agent_id") or ""),
        "client_name": _public_text(row.get("client_name")),
        "provider_id": str(row.get("provider_id") or ""),
        "model_id": str(row.get("model_id") or ""),
        "response_model_id": str(row.get("response_model_id") or ""),
        "reasoning_mode": str(row.get("reasoning_mode") or ""),
        "route_class": str(row.get("route_class") or ""),
        "enabled": bool(row.get("enabled")),
        "timeout_seconds": _safe_int(row.get("inference_timeout_seconds")),
        "updated_utc": str(row.get("updated_utc") or ""),
        "sha256": str(row.get("profile_sha256") or ""),
    }


def _safe_connection(row: dict[str, Any]) -> dict[str, Any]:
    # credential_target and secret_backend are intentionally excluded.
    return {
        "connection_id": str(row.get("connection_id") or ""),
        "display_name": _public_text(row.get("display_name")),
        "provider_id": str(row.get("provider_id") or ""),
        "route_class": str(row.get("route_class") or ""),
        "enabled": bool(row.get("enabled")),
        "endpoint_sha256": str(row.get("endpoint_sha256") or ""),
        "credential_fingerprint_sha256": str(
            row.get("credential_fingerprint_sha256") or ""
        ),
        "credential_version_sha256": str(
            row.get("credential_version_sha256") or ""
        ),
        "updated_utc": str(row.get("updated_utc") or ""),
        "sha256": str(row.get("connection_sha256") or ""),
    }


def _safe_presence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_id": str(row.get("agent_id") or ""),
        "client_name": _public_text(row.get("client_name")),
        "provider_id": str(row.get("provider_id") or ""),
        "model_id": str(row.get("model_id") or ""),
        "reasoning_mode": str(row.get("reasoning_mode") or ""),
        "route_class": str(row.get("route_class") or ""),
        "transport": str(row.get("transport") or ""),
        "session_id": str(row.get("session_id") or ""),
        "last_seen_utc": str(row.get("last_seen_utc") or ""),
    }


_USAGE_METRIC_FIELDS = (
    "completed_dispatches",
    "provider_calls",
    "reported_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
)


def _safe_usage_metrics(row: Any) -> dict[str, Any]:
    source = dict(row or {})
    return {field: source.get(field) for field in _USAGE_METRIC_FIELDS}


def _safe_usage_period(row: Any) -> dict[str, Any]:
    source = dict(row or {})
    return {
        **_safe_usage_metrics(source.get("totals")),
        "period": str(source.get("period") or ""),
        "granularity": str(source.get("granularity") or ""),
        "trend_truncated": bool(source.get("trend_truncated")),
        "providers": [
            {
                "provider_id": str(item.get("provider_id") or ""),
                **_safe_usage_metrics(item),
            }
            for item in list(source.get("by_provider") or ())[:20]
            if isinstance(item, dict)
        ],
        "models": [
            {
                "provider_id": str(item.get("provider_id") or ""),
                "model_id": str(item.get("model_id") or ""),
                **_safe_usage_metrics(item),
            }
            for item in list(source.get("by_model") or ())[:100]
            if isinstance(item, dict)
        ],
        "trend": [
            {
                "period_label": str(item.get("period_label") or ""),
                "period_key": str(item.get("period_key") or ""),
                **_safe_usage_metrics(item),
            }
            for item in list(source.get("trend") or ())[:240]
            if isinstance(item, dict)
        ],
    }


def _latest_live_presence(
    rows: Any,
    *,
    now_epoch: float | None = None,
    ttl_seconds: float = 120.0,
) -> list[dict[str, Any]]:
    """Return one latest, TTL-live presence row per Agent identity."""

    now = time.time() if now_epoch is None else float(now_epoch)
    latest: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        agent_id = str(row.get("agent_id") or "")
        last_seen = float(row.get("last_seen_epoch") or 0.0)
        if not agent_id or last_seen <= 0 or now - last_seen > ttl_seconds:
            continue
        prior = latest.get(agent_id)
        if prior is None or last_seen > float(prior.get("last_seen_epoch") or 0.0):
            latest[agent_id] = row
    return [latest[agent_id] for agent_id in sorted(latest)]


def _safe_memory(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": str(row.get("memory_id") or ""),
        "record_type": str(row.get("record_type") or ""),
        "visibility": str(row.get("visibility") or ""),
        "room_id": str(row.get("room_id") or ""),
        "owner_agent_id": str(row.get("owner_agent_id") or ""),
        "title": _public_text(row.get("title")),
        "body": _public_text(row.get("body")),
        "status": str(row.get("status") or ""),
        "source_message_sha256": str(row.get("source_message_sha256") or ""),
        "created_utc": str(row.get("created_utc") or ""),
        "sha256": str(row.get("revocation_sha256") or row.get("memory_sha256") or ""),
    }


def _safe_operation(row: dict[str, Any]) -> dict[str, Any]:
    # working_directory, leases and resource keys stay behind the local boundary.
    return {
        "operation_id": str(row.get("operation_id") or ""),
        "workflow_id": str(row.get("workflow_id") or ""),
        "requested_by": str(row.get("requested_by") or ""),
        "task_text": _public_text(row.get("task_text")),
        "status": str(row.get("status") or ""),
        "attempt_count": _safe_int(row.get("attempt_count")),
        "max_attempts": _safe_int(row.get("max_attempts")),
        "timeout_seconds": _safe_int(row.get("timeout_seconds")),
        "cancellation_requested": bool(row.get("cancellation_requested")),
        "terminal_outcome": str(row.get("terminal_outcome") or ""),
        "terminal_detail": _public_text(row.get("terminal_detail")),
        "bound_discussion_id": str(row.get("bound_discussion_id") or ""),
        "updated_utc": str(row.get("updated_utc") or ""),
        "sha256": str(row.get("operation_sha256") or ""),
    }


def _safe_schedule(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schedule_id": str(row.get("schedule_id") or ""),
        "workflow_id": str(row.get("workflow_id") or ""),
        "requested_by": str(row.get("requested_by") or ""),
        "task_text": _public_text(row.get("task_text")),
        "interval_seconds": _safe_int(row.get("interval_seconds")),
        "next_run_epoch": _safe_int(row.get("next_run_epoch")),
        "enabled": bool(row.get("enabled")),
        "updated_utc": str(row.get("updated_utc") or ""),
        "sha256": str(row.get("schedule_sha256") or ""),
    }


def _safe_capability(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "capability_id": str(row.get("capability_id") or ""),
        "registry_version": str(row.get("registry_version") or ""),
        "kind": str(row.get("kind") or ""),
        "display_name": _public_text(row.get("display_name")),
        "sensitivity": str(row.get("sensitivity") or ""),
        "enabled": bool(row.get("enabled")),
        "registered_by": str(row.get("registered_by") or ""),
        "created_utc": str(row.get("created_utc") or ""),
        "sha256": str(row.get("capability_sha256") or ""),
    }


def _safe_grant(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "grant_id": str(row.get("grant_id") or ""),
        "principal_type": str(row.get("principal_type") or ""),
        "principal_id": str(row.get("principal_id") or ""),
        "capability_id": str(row.get("capability_id") or ""),
        "decision": str(row.get("decision") or ""),
        "decided_by": str(row.get("decided_by") or ""),
        "reason": _public_text(row.get("reason")),
        "created_utc": str(row.get("created_utc") or ""),
        "sha256": str(row.get("grant_sha256") or ""),
    }


def _safe_permission(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": str(row.get("decision_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "agent_id": str(row.get("agent_id") or ""),
        "action": str(row.get("action") or ""),
        "decision": str(row.get("decision") or ""),
        "decided_by": str(row.get("decided_by") or ""),
        "reason": _public_text(row.get("reason")),
        "consumed_utc": str(row.get("consumed_utc") or ""),
        "expires_epoch": float(row.get("expires_epoch") or 0),
        "created_utc": str(row.get("created_utc") or ""),
        "sha256": str(row.get("decision_sha256") or ""),
    }


def _safe_execution(row: dict[str, Any]) -> dict[str, Any]:
    # repository_root and worktree_path are deliberately not exposed.
    return {
        "binding_id": str(row.get("binding_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "agent_id": str(row.get("agent_id") or ""),
        "permission_decision_id": str(row.get("permission_decision_id") or ""),
        "state": str(row.get("state") or ""),
        "base_commit_sha256": str(row.get("base_commit_sha256") or ""),
        "base_diff_sha256": str(row.get("base_diff_sha256") or ""),
        "final_commit_sha256": str(row.get("final_commit_sha256") or ""),
        "final_diff_sha256": str(row.get("final_diff_sha256") or ""),
        "updated_utc": str(row.get("updated_utc") or ""),
        "sha256": str(row.get("binding_sha256") or ""),
    }


def _safe_briefing(row: dict[str, Any]) -> dict[str, Any]:
    bindings = []
    for item in _safe_json_list(row.get("memory_bindings_json"), limit=80):
        if not isinstance(item, dict):
            continue
        bindings.append(
            {
                "memory_id": str(item.get("memory_id") or ""),
                "memory_sha256": str(item.get("memory_sha256") or ""),
                "record_type": str(item.get("record_type") or ""),
                "authority_id": str(item.get("authority_id") or ""),
            }
        )
    return {
        "briefing_id": str(row.get("briefing_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "agent_id": str(row.get("agent_id") or ""),
        "room_id": str(row.get("room_id") or ""),
        "memory_count": _json_list_count(row.get("memory_bindings_json")),
        "memory_bindings": bindings,
        "created_utc": str(row.get("created_utc") or ""),
        "sha256": str(row.get("briefing_sha256") or ""),
    }


def _safe_conflict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "finding_id": str(row.get("finding_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "reviewer": str(row.get("reviewer") or ""),
        "summary": _public_text(row.get("summary")),
        "severity": str(row.get("severity") or ""),
        "status": str(row.get("status") or ""),
        "created_utc": str(row.get("created_utc") or ""),
        "sha256": str(row.get("finding_sha256") or ""),
    }


def _safe_trust(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": str(row.get("record_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "actor": str(row.get("actor") or ""),
        "stage": str(row.get("stage") or ""),
        "statement": _public_text(row.get("statement")),
        "source_count": _json_list_count(row.get("source_bindings_json")),
        "related_count": _json_list_count(row.get("related_record_ids_json")),
        "created_utc": str(row.get("created_utc") or ""),
        "sha256": str(row.get("trust_sha256") or ""),
    }


def _safe_peer_call(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": str(row.get("request_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "requester": str(row.get("requester") or ""),
        "recipient": str(row.get("recipient") or ""),
        "question": _public_text(row.get("question")),
        "status": str(row.get("status") or ""),
        "approval_mode": str(row.get("approval_mode") or ""),
        "response": _public_text(row.get("response")),
        "request_utc": str(row.get("request_utc") or ""),
        "response_utc": str(row.get("response_utc") or ""),
        "request_sha256": str(row.get("request_sha256") or ""),
        "response_sha256": str(row.get("response_sha256") or ""),
        "artifact_count": _json_list_count(row.get("artifact_paths_json")),
        "response_artifact_count": _json_list_count(
            row.get("response_artifact_paths_json")
        ),
    }


def _safe_review(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_id": str(row.get("review_id") or ""),
        "request_id": str(row.get("request_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "reviewer": str(row.get("reviewer") or ""),
        "verdict": str(row.get("verdict") or ""),
        "score": row.get("score"),
        "findings": _public_text(row.get("findings")),
        "artifact_count": _json_list_count(row.get("artifact_paths_json")),
        "review_utc": str(row.get("review_utc") or ""),
        "sha256": str(row.get("review_sha256") or ""),
    }


def _safe_change(row: dict[str, Any]) -> dict[str, Any]:
    changed_paths = [
        _public_text(item)
        for item in _safe_json_list(row.get("changed_paths_json"), limit=80)
        if isinstance(item, str)
    ]
    return {
        "record_id": str(row.get("record_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "actor": str(row.get("actor") or ""),
        "summary": _public_text(row.get("change_summary")),
        "changed_path_count": _json_list_count(row.get("changed_paths_json")),
        "changed_paths": changed_paths,
        "test_summary": _public_text(row.get("tests")),
        "approval_mode": str(row.get("approval_mode") or ""),
        "review_count": _json_list_count(row.get("review_ids_json")),
        "recorded_utc": str(row.get("recorded_utc") or ""),
        "sha256": str(row.get("record_sha256") or ""),
    }


def _safe_event(row: dict[str, Any]) -> dict[str, Any]:
    # payload_json is intentionally excluded from the browser contract.
    return {
        "sequence": _safe_int(row.get("sequence")),
        "event_id": str(row.get("event_id") or ""),
        "actor": str(row.get("actor") or ""),
        "event_type": str(row.get("event_type") or ""),
        "task_id": str(row.get("task_id") or ""),
        "created_utc": str(row.get("created_utc") or ""),
        "payload_sha256": str(row.get("payload_sha256") or ""),
        "prev_chain_sha256": str(row.get("prev_chain_sha256") or ""),
        "chain_sha256": str(row.get("chain_sha256") or ""),
    }


def _authorized_session_rows(
    reader: BridgeReader, scope: str, room_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read bounded, redacted terminal session state when the optional tables exist."""
    with reader.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "authorized_sessions" not in tables:
            return [], []
        sessions = [
            dict(row)
            for row in connection.execute(
                """SELECT source_type, source_session_id, adapter_id, owner_agent_id,
                          room_id, display_name, client_name, client_version,
                          requested_route, observed_route, observed_route_source,
                          model_id, model_source, role_id, role_label, state,
                          supports_events, started_utc, ended_utc, last_seen_utc,
                          latest_sequence, session_sha256
                     FROM authorized_sessions
                    WHERE scope=? AND (room_id=? OR room_id IS NULL OR room_id='')
                    ORDER BY last_seen_epoch DESC, source_session_id
                    LIMIT 24""",
                (scope, room_id),
            ).fetchall()
        ]
        events: list[dict[str, Any]] = []
        if "authorized_session_events" in tables:
            events = [
                dict(row)
                for row in connection.execute(
                    """SELECT e.source_type, e.source_session_id, e.sequence,
                              e.created_utc, e.stream, e.kind, e.summary,
                              e.state_after, e.secret_redacted, e.event_sha256
                         FROM authorized_session_events e
                         JOIN authorized_sessions s
                           ON s.scope=e.scope
                          AND s.source_type=e.source_type
                          AND s.source_session_id=e.source_session_id
                        WHERE e.scope=?
                          AND (s.room_id=? OR s.room_id IS NULL OR s.room_id='')
                        ORDER BY e.created_utc DESC, e.sequence DESC
                        LIMIT 160""",
                    (scope, room_id),
                ).fetchall()
            ]
    return sessions, events


def _safe_session(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": str(row.get("source_type") or ""),
        "session_id": str(row.get("source_session_id") or ""),
        "adapter_id": str(row.get("adapter_id") or ""),
        "owner_agent_id": str(row.get("owner_agent_id") or ""),
        "room_id": str(row.get("room_id") or ""),
        "display_name": _public_text(row.get("display_name")),
        "client_name": _public_text(row.get("client_name")),
        "client_version": str(row.get("client_version") or ""),
        "requested_route": str(row.get("requested_route") or ""),
        "observed_route": str(row.get("observed_route") or ""),
        "observed_route_source": str(row.get("observed_route_source") or ""),
        "model_id": str(row.get("model_id") or ""),
        "model_source": str(row.get("model_source") or ""),
        "role_id": str(row.get("role_id") or ""),
        "role_label": _public_text(row.get("role_label")),
        "state": str(row.get("state") or ""),
        "supports_events": bool(row.get("supports_events")),
        "started_utc": str(row.get("started_utc") or ""),
        "ended_utc": str(row.get("ended_utc") or ""),
        "last_seen_utc": str(row.get("last_seen_utc") or ""),
        "latest_sequence": _safe_int(row.get("latest_sequence")),
        "sha256": str(row.get("session_sha256") or ""),
        "managed": False,
        "input_submitted": False,
        "return_code": None,
        "terminal_outcome": "",
        "execution_mode": "observed",
        "usage": {},
    }


def _safe_session_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": str(row.get("source_type") or ""),
        "session_id": str(row.get("source_session_id") or ""),
        "sequence": _safe_int(row.get("sequence")),
        "created_utc": str(row.get("created_utc") or ""),
        "stream": str(row.get("stream") or ""),
        "kind": str(row.get("kind") or ""),
        "summary": _public_text(row.get("summary")),
        "state_after": str(row.get("state_after") or ""),
        "secret_redacted": bool(row.get("secret_redacted")),
        "sha256": str(row.get("event_sha256") or ""),
    }


def _safe_managed_session(row: dict[str, Any]) -> dict[str, Any]:
    session_id = str(row.get("session_id") or "")
    raw_adapter = row.get("adapter")
    if not isinstance(raw_adapter, dict):
        raw_adapter = {}
    adapter_capabilities = []
    for capability in raw_adapter.get("capabilities") or ():
        if not isinstance(capability, dict):
            continue
        adapter_capabilities.append(
            {
                "capability_id": str(capability.get("capability_id") or ""),
                "state": str(capability.get("state") or "unsupported"),
                "evidence": _public_text(capability.get("evidence")),
                "limitation": _public_text(capability.get("limitation")),
            }
        )
    adapter = {
        "contract_version": str(raw_adapter.get("contract_version") or ""),
        "adapter_id": str(raw_adapter.get("adapter_id") or ""),
        "agent_id": str(raw_adapter.get("agent_id") or ""),
        "vendor": _public_text(raw_adapter.get("vendor")),
        "transport": str(raw_adapter.get("transport") or ""),
        "provider_identity": str(raw_adapter.get("provider_identity") or ""),
        "official": bool(raw_adapter.get("official")),
        "capabilities": adapter_capabilities,
    }
    raw_broker = row.get("approval_broker")
    if not isinstance(raw_broker, dict):
        raw_broker = {}
    approval_broker = {
        "schema": str(raw_broker.get("schema") or ""),
        "mode": str(raw_broker.get("mode") or row.get("approval_mode") or ""),
        "pending_count": _safe_int(raw_broker.get("pending_count")),
        "history_count": _safe_int(raw_broker.get("history_count")),
        "pending": [],
        "history": [],
    }
    for target, limit in (("pending", 20), ("history", 80)):
        for record in (raw_broker.get(target) or ())[:limit]:
            if not isinstance(record, dict):
                continue
            approval_broker[target].append(
                {
                    "schema": str(record.get("schema") or ""),
                    "approval_id": str(record.get("approval_id") or ""),
                    "session_id": str(record.get("session_id") or ""),
                    "adapter_id": str(record.get("adapter_id") or ""),
                    "provider_request_id": str(
                        record.get("provider_request_id") or ""
                    ),
                    "action_kind": str(record.get("action_kind") or ""),
                    "title": _public_text(record.get("title")),
                    "detail": _public_text(record.get("detail")),
                    "risk": str(record.get("risk") or ""),
                    "available_decisions": [
                        str(value)
                        for value in (record.get("available_decisions") or ())
                        if str(value) in APPROVAL_DECISIONS
                    ],
                    "created_utc": str(record.get("created_utc") or ""),
                    "state": str(record.get("state") or ""),
                    "decision": str(record.get("decision") or ""),
                    "resolved_utc": str(record.get("resolved_utc") or ""),
                    "record_sha256": str(record.get("record_sha256") or ""),
                }
            )
    raw_authorization = row.get("session_authorization")
    if not isinstance(raw_authorization, dict):
        raw_authorization = {}
    session_authorization = {
        key: raw_authorization.get(key)
        for key in (
            "mode",
            "permission_tier",
            "approval_mode",
            "governance_binding_id",
            "decision_id",
            "decision_sha256",
            "authorized_by",
            "expires_epoch",
        )
        if raw_authorization.get(key) not in {None, ""}
    }
    raw_capability = row.get("multimodal_capability") or {}
    multimodal_capability = {
        key: value
        for key, value in dict(raw_capability).items()
        if key
        in {
            "attachment_input_supported",
            "image_input_supported",
            "image_input",
            "audio_input_supported",
            "audio_input",
            "text_file_input",
            "model_view_confirmation",
            "semantic_image_verification",
        }
    }
    delivery_receipts: list[dict[str, Any]] = []
    for receipt in row.get("attachment_delivery_receipts") or ():
        if not isinstance(receipt, dict):
            continue
        attachments = []
        for item in receipt.get("attachments") or ():
            if not isinstance(item, dict):
                continue
            attachments.append(
                {
                    key: item.get(key)
                    for key in (
                        "relative_path",
                        "sha256",
                        "bytes",
                        "media_type",
                        "kind",
                    )
                }
            )
        delivery_receipts.append(
            {
                key: receipt.get(key)
                for key in (
                    "provider_id",
                    "protocol",
                    "delivery_mode",
                    "status",
                    "attachment_count",
                    "model_view_confirmed",
                    "receipt_sha256",
                )
            }
            | {"attachments": attachments}
        )
    vision_receipts: list[dict[str, Any]] = []
    for receipt in row.get("vision_verification_receipts") or ():
        if not isinstance(receipt, dict):
            continue
        vision_receipts.append(
            {
                key: receipt.get(key)
                for key in (
                    "challenge_id",
                    "provider_id",
                    "protocol",
                    "delivery_mode",
                    "provider_identity",
                    "model_id",
                    "client_version",
                    "status",
                    "model_view_confirmed",
                    "image_sha256",
                    "image_bytes",
                    "prompt_sha256",
                    "response_sha256",
                    "response_present",
                    "evaluated_utc",
                    "receipt_sha256",
                )
            }
        )
    return {
        "source_type": "managed-local",
        "session_id": session_id,
        "adapter_id": str(adapter.get("adapter_id") or row.get("agent_id") or ""),
        "adapter": adapter,
        "owner_agent_id": str(row.get("agent_id") or ""),
        "room_id": "",
        "display_name": _public_text(row.get("display_name")),
        "client_name": _public_text(row.get("client_name")),
        "client_version": str(row.get("client_version") or ""),
        "requested_route": str(row.get("requested_route") or ""),
        "observed_route": str(row.get("observed_route") or ""),
        "observed_route_source": str(row.get("observed_route_source") or ""),
        "model_id": str(row.get("model_id") or ""),
        "model_source": str(row.get("model_source") or ""),
        "role_id": str(row.get("role") or COCKPIT_DEFAULT_LAUNCH_ROLE),
        "role_label": "",
        "state": str(row.get("state") or "unknown"),
        "supports_events": True,
        "started_utc": str(row.get("started_utc") or ""),
        "ended_utc": str(row.get("ended_utc") or ""),
        "last_seen_utc": str(row.get("ended_utc") or row.get("started_utc") or ""),
        "latest_sequence": _safe_int(row.get("latest_sequence")),
        "sha256": hashlib.sha256(
            f"managed-local:{session_id}".encode("utf-8")
        ).hexdigest(),
        "managed": True,
        "input_submitted": bool(row.get("input_submitted")),
        "input_mode": str(row.get("input_mode") or "single"),
        "can_submit_input": bool(row.get("can_submit_input")),
        "session_contract": {
            "mode": str((row.get("session_contract") or {}).get("mode") or "one_shot"),
            "input_transport": str(
                (row.get("session_contract") or {}).get("input_transport")
                or "stdin_once"
            ),
            "additional_input_supported": bool(
                (row.get("session_contract") or {}).get(
                    "additional_input_supported"
                )
            ),
            "resume_supported": bool(
                (row.get("session_contract") or {}).get("resume_supported")
            ),
            "process_terminal_after_turn": bool(
                (row.get("session_contract") or {}).get(
                    "process_terminal_after_turn", True
                )
            ),
            "protocol": str(
                (row.get("session_contract") or {}).get("protocol") or ""
            ),
            "provider_identity": str(
                (row.get("session_contract") or {}).get("provider_identity") or ""
            ),
        },
        "multimodal_capability": multimodal_capability,
        "attachment_delivery_receipts": delivery_receipts,
        "vision_verification_receipts": vision_receipts,
        "return_code": row.get("return_code"),
        "terminal_outcome": str(row.get("terminal_outcome") or ""),
        "execution_mode": str(row.get("execution_mode") or ""),
        "permission_tier": str(row.get("permission_tier") or "observe"),
        "approval_mode": str(
            row.get("approval_mode")
            or _default_approval_mode(row.get("permission_tier"))
        ),
        "approval_broker": approval_broker,
        "session_authorization": session_authorization,
        "usage": {
            str(key): value
            for key, value in dict(row.get("usage") or {}).items()
            if key
            in {
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cached_input_tokens",
                "reasoning_tokens",
            }
        },
    }


def _safe_managed_events(row: dict[str, Any]) -> list[dict[str, Any]]:
    session_id = str(row.get("session_id") or "")
    state = str(row.get("state") or "unknown")
    safe_rows: list[dict[str, Any]] = []
    for event in row.get("events") or ():
        if not isinstance(event, dict):
            continue
        sequence = _safe_int(event.get("sequence"))
        safe_rows.append(
            {
                "source_type": "managed-local",
                "session_id": session_id,
                "sequence": sequence,
                "created_utc": str(event.get("created_utc") or ""),
                "stream": str(event.get("stream") or "system"),
                "kind": str(event.get("kind") or "system"),
                "summary": _public_text(event.get("summary") or event.get("text")),
                "state_after": state,
                "secret_redacted": True,
                "sha256": hashlib.sha256(
                    f"managed-local:{session_id}:{sequence}".encode("utf-8")
                ).hexdigest(),
            }
        )
    return safe_rows


def _feedback_state() -> dict[str, Any]:
    try:
        config = FeedbackConfig.load()
    except FeedbackError:
        return {
            "configured": False,
            "delivery_configured": False,
            "encrypted_credential_available": False,
            "recipient_label": "",
            "privacy_url": "",
            "configuration_error": True,
        }
    return {
        "configured": bool(config.endpoint or config.support_email),
        "delivery_configured": bool(config.endpoint),
        "encrypted_credential_available": config.encrypted_secret_available,
        "recipient_label": _public_text(config.recipient_label),
        "privacy_url": str(config.privacy_url or ""),
        "configuration_error": False,
    }


def _appearance_state(project_root: Path) -> dict[str, Any]:
    try:
        preferences = load_preferences(project_root)
    except LocalizationError:
        return {
            "selected": "modern",
            "available": ["pixel", "modern"],
            "locale": "en",
            "tutorial_completed": False,
            "configuration_error": True,
        }
    return {
        "selected": str(preferences.get("theme") or "modern"),
        "available": ["pixel", "modern"],
        "locale": str(preferences.get("locale") or "en"),
        "tutorial_completed": bool(preferences.get("tutorial_completed")),
        "configuration_error": False,
    }


def _announcement_payload(item: Announcement, read_ids: set[str]) -> dict[str, Any]:
    return {
        "announcement_id": item.announcement_id,
        "locale": item.locale,
        "title": _public_text(item.title),
        "body": _public_text(item.body),
        "severity": item.severity,
        "link_url": str(item.link_url or ""),
        "published_utc": item.published_utc,
        "expires_utc": str(item.expires_utc or ""),
        "read": announcement_read_key(item) in read_ids,
    }


def _announcement_state(project_root: Path) -> dict[str, Any]:
    configuration_error = False
    cache_error = False
    try:
        config = AnnouncementConfig.load()
    except AnnouncementError:
        config = None
        configuration_error = True
    try:
        preferences = load_announcement_preferences(project_root)
    except AnnouncementError:
        preferences = fail_closed_announcement_preferences()
        configuration_error = True
    try:
        rows = load_announcement_cache(project_root)
    except AnnouncementError:
        rows = ()
        cache_error = True
    read_ids = set(preferences["read_ids"])
    return {
        "configured": config is not None,
        "network_enabled": bool(preferences["network_enabled"]),
        "popup_enabled": bool(preferences["popup_enabled"]),
        "configuration_error": configuration_error,
        "cache_error": cache_error,
        "announcements": [
            _announcement_payload(item, read_ids)
            for item in reversed(rows)
        ],
    }


def workbench_payload(
    reader: BridgeReader,
    *,
    project_root: Path,
    scope: str,
    room_id: str,
    limit: int = 120,
    before_sequence: int | None = None,
    managed_sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one redacted, bounded workbench snapshot from live local state."""
    history_imports = list_conversation_imports(project_root, scope)
    selected_history = next(
        (row for row in history_imports if row["room_id"] == room_id),
        None,
    )
    if selected_history is None:
        room = reader.room_view(
            scope=scope,
            requested_room_id=room_id,
            consumer=HUMAN_AGENT_ID,
            limit=limit,
            before_sequence=before_sequence,
        )
        native_room_ids = {str(row.get("room_id") or "") for row in room["rooms"]}
        room["rooms"] = list(room["rooms"]) + [
            {
                "room_id": str(row["room_id"]),
                "name": str(row["title"]),
                "message_count": _safe_int(row["message_count"]),
                "active_member_count": 0,
                "updated_utc": str(row.get("ended_utc") or row["imported_utc"]),
                "room_kind": "imported-history",
                "provider": str(row["provider"]),
                "read_only": True,
            }
            for row in history_imports
            if str(row["room_id"]) not in native_room_ids
        ]
    else:
        native_rooms = reader.room_view(
            scope=scope,
            requested_room_id=DEFAULT_ROOM_ID,
            consumer=HUMAN_AGENT_ID,
            limit=1,
        )["rooms"]
        imported_summaries = [
            {
                "room_id": str(row["room_id"]),
                "name": str(row["title"]),
                "message_count": _safe_int(row["message_count"]),
                "active_member_count": 0,
                "updated_utc": str(row.get("ended_utc") or row["imported_utc"]),
                "room_kind": "imported-history",
                "provider": str(row["provider"]),
                "read_only": True,
            }
            for row in history_imports
        ]
        room = imported_room_view(
            selected_history,
            rooms=[*native_rooms, *imported_summaries],
            limit=limit,
            before_sequence=before_sequence,
        )
    snapshot = reader.snapshot(limit=120, scope=scope)
    live_presence_rows = _latest_live_presence(snapshot.presence)
    active_room_id = str(room["room_id"])
    session_rows, session_event_rows = _authorized_session_rows(
        reader, scope, active_room_id
    )
    managed_rows = [
        row
        for row in (managed_sessions or ())
        if str(row.get("room_id") or "") in {"", active_room_id}
    ]
    persisted_sessions = [_safe_session(row) for row in session_rows]
    persisted_ids = {row["session_id"] for row in persisted_sessions}
    live_sessions = [
        _safe_managed_session(row)
        for row in managed_rows
        if str(row.get("session_id") or "") not in persisted_ids
    ]
    live_events = [
        event
        for row in managed_rows
        for event in _safe_managed_events(row)
    ]
    automation = room["automation"]
    discussion = automation.get("active_discussion")
    payload: dict[str, Any] = {
        "schema": "peerbridge.local-workbench.v1",
        "version": __version__,
        "generated_utc": snapshot.generated_utc,
        "scope": scope,
        "room_id": room["room_id"],
        "rooms": [
            {
                "room_id": str(row.get("room_id") or ""),
                "name": _redact(row.get("name")),
                "message_count": _safe_int(row.get("message_count")),
                "active_member_count": _safe_int(row.get("active_member_count")),
                "updated_utc": str(row.get("updated_utc") or ""),
                "room_kind": str(row.get("room_kind") or ""),
                "provider": _redact(row.get("provider")),
            }
            for row in room["rooms"]
        ],
        "members": [_safe_member(dict(row)) for row in room["members"]],
        "messages": [_safe_message(dict(row)) for row in room["messages"]],
        "dispatches": [
            _safe_dispatch(dict(row)) for row in snapshot.message_dispatches[:120]
        ],
        "page": dict(room["page"]),
        "operator_active": bool(room["operator_active"]),
        "automation": {
            "mode": str(automation.get("mode") or "off"),
            "enabled": bool(automation.get("enabled")),
            "max_rounds": _safe_int(automation.get("max_rounds")),
            "max_messages": _safe_int(automation.get("max_messages")),
            "stagnation_rounds": _safe_int(automation.get("stagnation_rounds")),
            "active_discussion": (
                {
                    "discussion_id": str(discussion.get("discussion_id") or ""),
                    "status": str(discussion.get("status") or ""),
                    "round_index": _safe_int(discussion.get("round_index")),
                    "message_count": _safe_int(discussion.get("message_count")),
                    "termination_reason": str(discussion.get("termination_reason") or ""),
                }
                if isinstance(discussion, dict)
                else None
            ),
        },
        "context_policy": {
            "enabled": True,
            "scope": "same-room",
            "max_messages": DEFAULT_ROOM_CONTEXT_MESSAGES,
            "max_chars": DEFAULT_ROOM_CONTEXT_CHARS,
            "fanout_root_deduplication": True,
            "cross_room_access": False,
        },
        "tasks": [_safe_task(dict(row)) for row in snapshot.tasks[:80]],
        "work_updates": [
            _safe_work_update(dict(row)) for row in snapshot.work_updates[:120]
        ],
        "cockpit": {
            "sessions": live_sessions + persisted_sessions,
            "events": live_events
            + [_safe_session_event(row) for row in session_event_rows],
            "presence": [
                _safe_presence(dict(row)) for row in live_presence_rows[:80]
            ],
        },
        "routes": [_safe_route(dict(row)) for row in snapshot.route_profiles[:80]],
        "connections": [
            _safe_connection(dict(row))
            for row in snapshot.provider_connections[:80]
        ],
        "memories": [_safe_memory(dict(row)) for row in snapshot.memories[:120]],
        "operations": [
            _safe_operation(dict(row)) for row in snapshot.operations[:120]
        ],
        "schedules": [
            _safe_schedule(dict(row)) for row in snapshot.schedules[:80]
        ],
        "workflow_templates": [
            {
                "workflow_id": workflow_id,
                "label": str(template["label"]),
                "roles": list(template["roles"]),
                "session_modes": list(template["session_modes"]),
                "automatic_retry": bool(template["automatic_retry"]),
            }
            for workflow_id, template in WORKFLOW_TEMPLATES.items()
        ],
        "managed_agent_catalog": managed_agent_catalog(project_root),
        "managed_agent_roles": list(COCKPIT_ROLES),
        "capabilities": [
            _safe_capability(dict(row)) for row in snapshot.capabilities[:120]
        ],
        "capability_grants": [
            _safe_grant(dict(row)) for row in snapshot.capability_grants[:120]
        ],
        "permissions": [
            _safe_permission(dict(row))
            for row in snapshot.permission_decisions[:120]
        ],
        "executions": [
            _safe_execution(dict(row)) for row in snapshot.execution_bindings[:120]
        ],
        "briefings": [
            _safe_briefing(dict(row)) for row in snapshot.task_briefings[:120]
        ],
        "conflicts": [
            _safe_conflict(dict(row)) for row in snapshot.decision_conflicts[:120]
        ],
        "trust": [_safe_trust(dict(row)) for row in snapshot.trust_records[:120]],
        "peer_calls": [
            _safe_peer_call(dict(row)) for row in snapshot.peer_calls[:120]
        ],
        "peer_reviews": [
            _safe_review(dict(row)) for row in snapshot.peer_reviews[:120]
        ],
        "changes": [_safe_change(dict(row)) for row in snapshot.changes[:120]],
        "events": [_safe_event(dict(row)) for row in snapshot.events[:160]],
        "usage": {
            "totals": {
                key: snapshot.usage_totals.get(key)
                for key in (
                    "completed_dispatches",
                    "provider_calls",
                    "reported_calls",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cached_input_tokens",
                    "reasoning_tokens",
                )
            },
            "models": [
                {
                    "provider_id": str(row.get("provider_id") or ""),
                    "model_id": str(row.get("model_id") or ""),
                    "provider_calls": _safe_int(row.get("provider_calls")),
                    "reported_calls": _safe_int(row.get("reported_calls")),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                    "total_tokens": row.get("total_tokens"),
                }
                for row in snapshot.usage_by_model[:20]
            ],
            "providers": [
                {
                    "provider_id": str(row.get("provider_id") or ""),
                    "provider_calls": _safe_int(row.get("provider_calls")),
                    "reported_calls": _safe_int(row.get("reported_calls")),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                    "total_tokens": row.get("total_tokens"),
                    "today_tokens": row.get("today_tokens"),
                }
                for row in snapshot.usage_by_provider[:20]
            ],
            "daily": [
                {
                    "utc_date": str(row.get("utc_date") or ""),
                    "provider_calls": _safe_int(row.get("provider_calls")),
                    "reported_calls": _safe_int(row.get("reported_calls")),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                    "total_tokens": row.get("total_tokens"),
                    "cached_input_tokens": row.get("cached_input_tokens"),
                    "reasoning_tokens": row.get("reasoning_tokens"),
                }
                for row in snapshot.usage_daily[-30:]
            ],
            "periods": {
                key: _safe_usage_period(row)
                for key, row in snapshot.usage_periods.items()
                if key in {"today", "7d", "30d", "all"}
                and isinstance(row, dict)
            },
        },
        "feature_status": {
            "message_attachments": True,
            "room_automation": True,
            "member_roles": True,
            "managed_session_events": bool(session_rows or managed_rows),
            "managed_session_control": True,
            "observable_agent_activity": True,
            "agent_model_permission_controls": True,
            "worktree_diff": True,
            "room_creation": True,
            "room_seat_management": True,
            "workflow_queue": True,
            "workflow_schedules": True,
            "capability_governance": True,
            "execution_governance": True,
            "proof_bundles": True,
            "feedback": True,
            "announcements": True,
            "history_import": True,
            "same_room_context": True,
            "loopback_only": True,
        },
        "history_import": {
            "providers": sorted(HISTORY_IMPORT_PROVIDERS),
            "max_bytes": MAX_HISTORY_IMPORT_BYTES,
            "records": [
                {
                    "import_id": str(row["import_id"]),
                    "room_id": str(row["room_id"]),
                    "provider": str(row["provider"]),
                    "title": str(row["title"]),
                    "message_count": _safe_int(row["message_count"]),
                    "source_sha256": str(row["source_sha256"]),
                    "record_sha256": str(row["record_sha256"]),
                    "imported_utc": str(row["imported_utc"]),
                    "read_only": True,
                }
                for row in history_imports
            ],
            "selected": room.get("imported_history"),
        },
        "feedback": _feedback_state(),
        "appearance": _appearance_state(project_root),
        "announcement_state": _announcement_state(project_root),
        "counts": {
            "agents": snapshot.table_counts.get("agent_presence", 0),
            "messages": snapshot.table_counts.get("messages", 0),
            "dispatches": snapshot.table_counts.get("message_dispatches", 0),
            "tasks": snapshot.table_counts.get("tasks", 0),
            "memories": snapshot.table_counts.get("memories", 0),
            "events": snapshot.table_counts.get("events", 0),
        },
    }
    signature_payload = {key: value for key, value in payload.items() if key != "generated_utc"}
    payload["signature"] = hashlib.sha256(
        json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


@dataclass(frozen=True)
class WorkbenchConfig:
    project_root: Path
    db_path: Path
    scope: str
    token: str
    initial_room_id: str
    instance_id: str


@dataclass(frozen=True)
class _HistorySelection:
    expires_at: float
    provider: str
    source_id: str
    source_revision: str | None
    project_key: str


class WorkbenchServer(BoundedThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    max_request_workers = 4

    def __init__(
        self,
        address: tuple[str, int],
        config: WorkbenchConfig,
        *,
        managed_agent_manager: ManagedAgentManager | HybridManagedAgentManager | None = None,
    ) -> None:
        if not _is_loopback(address[0]):
            raise WorkbenchError("local workbench must bind to loopback")
        self.config = config
        self.reader: BridgeReader | None = None
        self.managed_agents: ManagedAgentManager | HybridManagedAgentManager | None = None
        self._session_authorization_lock = threading.Lock()
        self._session_authorizations: dict[str, dict[str, Any]] = {}
        super().__init__(address, WorkbenchHandler)
        self.reader = BridgeReader(config.db_path, config.project_root)
        self.managed_agents = managed_agent_manager or HybridManagedAgentManager()
        self._rate_lock = threading.Lock()
        self._writes: deque[float] = deque()
        self._idempotency_lock = threading.Lock()
        self._idempotency: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self._history_selection_lock = threading.Lock()
        self._history_project_key = os.path.normcase(str(config.project_root.resolve()))
        self._history_selections: OrderedDict[str, _HistorySelection] = OrderedDict()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{int(self.server_address[1])}"

    def allow_write(self) -> bool:
        now = time.monotonic()
        with self._rate_lock:
            while self._writes and now - self._writes[0] >= 60:
                self._writes.popleft()
            if len(self._writes) >= MAX_WRITES_PER_MINUTE:
                return False
            self._writes.append(now)
            return True

    def prior_result(self, request_id: str, payload_sha256: str) -> dict[str, Any] | None:
        with self._idempotency_lock:
            prior = self._idempotency.get(request_id)
            if prior is None:
                return None
            if prior[0] != payload_sha256:
                raise WorkbenchError("request ID was reused with different content")
            self._idempotency.move_to_end(request_id)
            return dict(prior[1])

    def remember_result(
        self, request_id: str, payload_sha256: str, result: dict[str, Any]
    ) -> None:
        with self._idempotency_lock:
            self._idempotency[request_id] = (payload_sha256, dict(result))
            self._idempotency.move_to_end(request_id)
            while len(self._idempotency) > MAX_IDEMPOTENCY_RECORDS:
                self._idempotency.popitem(last=False)

    def issue_history_selection(
        self,
        *,
        provider: str,
        source_id: str,
        source_revision: str | None = None,
    ) -> str:
        revision = None
        if source_revision is not None:
            revision = _valid_sha256(source_revision)
            if revision is None:
                raise WorkbenchError("history source revision is invalid")
        now = time.monotonic()
        handle = secrets.token_urlsafe(32)
        with self._history_selection_lock:
            self._prune_history_selections(now)
            self._history_selections[handle] = _HistorySelection(
                expires_at=now + HISTORY_SELECTION_TTL_SECONDS,
                provider=provider,
                source_id=source_id,
                source_revision=revision,
                project_key=self._history_project_key,
            )
            while len(self._history_selections) > MAX_HISTORY_SELECTIONS:
                self._history_selections.popitem(last=False)
        return handle

    def _prune_history_selections(self, now: float) -> None:
        expired = [
            handle
            for handle, record in self._history_selections.items()
            if record.expires_at <= now
        ]
        for handle in expired:
            self._history_selections.pop(handle, None)

    def resolve_history_selections(
        self,
        handles: list[str],
        *,
        provider: str,
        source_revision: str | None = None,
        consume: bool = False,
    ) -> list[str]:
        return [
            record.source_id
            for record in self.resolve_history_selection_records(
                handles,
                provider=provider,
                source_revision=source_revision,
                consume=consume,
            )
        ]

    def resolve_history_selection_records(
        self,
        handles: list[str],
        *,
        provider: str,
        source_revision: str | None = None,
        consume: bool = False,
    ) -> list[_HistorySelection]:
        if not handles or len(handles) > 20 or len(set(handles)) != len(handles):
            raise WorkbenchError("history selection is invalid")
        expected_revision = None
        if source_revision is not None:
            expected_revision = _valid_sha256(source_revision)
            if expected_revision is None:
                raise WorkbenchError("history source revision is invalid")
        now = time.monotonic()
        resolved: list[_HistorySelection] = []
        with self._history_selection_lock:
            self._prune_history_selections(now)
            for handle in handles:
                if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", handle):
                    raise WorkbenchError("history selection handle is invalid")
                record = self._history_selections.get(handle)
                if (
                    record is None
                    or record.provider != provider
                    or record.project_key != self._history_project_key
                    or (
                        expected_revision is not None
                        and record.source_revision != expected_revision
                    )
                ):
                    raise WorkbenchError(
                        "history selection expired or does not match the source"
                    )
                resolved.append(record)
            if consume:
                for handle in handles:
                    self._history_selections.pop(handle, None)
        return resolved

    def remember_session_authorization(
        self, session_id: str, authorization: dict[str, Any]
    ) -> None:
        if not SAFE_IDENTIFIER.fullmatch(session_id):
            raise WorkbenchError("invalid managed session ID")
        record = dict(authorization)
        if (
            record.get("mode") != "once-per-session"
            or record.get("permission_tier") not in {"edit", "full-development"}
            or not SAFE_IDENTIFIER.fullmatch(
                str(record.get("governance_binding_id") or "")
            )
            or not SAFE_IDENTIFIER.fullmatch(str(record.get("decision_id") or ""))
        ):
            raise WorkbenchError("managed session authorization is invalid")
        with self._session_authorization_lock:
            self._session_authorizations[session_id] = record

    def session_authorization(self, session_id: str) -> dict[str, Any] | None:
        with self._session_authorization_lock:
            record = self._session_authorizations.get(session_id)
            if record is None:
                return None
            if float(record.get("expires_epoch") or 0) <= time.time():
                self._session_authorizations.pop(session_id, None)
                return None
            return dict(record)

    def forget_session_authorization(self, session_id: str) -> None:
        with self._session_authorization_lock:
            self._session_authorizations.pop(session_id, None)

    def managed_session_snapshots(self) -> list[dict[str, Any]]:
        if self.managed_agents is None:
            return []
        rows: list[dict[str, Any]] = []
        for raw in self.managed_agents.snapshots():
            row = dict(raw)
            authorization = self.session_authorization(str(row.get("session_id") or ""))
            if authorization is not None:
                row["session_authorization"] = authorization
            rows.append(row)
        return rows

    def server_close(self) -> None:
        with self._session_authorization_lock:
            self._session_authorizations.clear()
        if self.managed_agents is not None:
            self.managed_agents.close()
            self.managed_agents = None
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        super().server_close()


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _security_headers(self) -> dict[str, str]:
        return {
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in self._security_headers().items():
            self.send_header(key, value)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = b'{"error":"workbench response exceeds the byte limit"}'
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._send(
            status,
            encoded,
            "application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    def _client_is_loopback(self) -> bool:
        return _is_loopback(str(self.client_address[0]))

    def _host_is_expected(self) -> bool:
        expected = {
            f"127.0.0.1:{int(self.server.server_address[1])}",
            f"localhost:{int(self.server.server_address[1])}",
        }
        return self.headers.get("Host", "").lower() in expected

    def _authorized(self) -> bool:
        if not self._client_is_loopback() or not self._host_is_expected():
            self._json(HTTPStatus.FORBIDDEN, {"error": "local origin rejected"})
            return False
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.config.token}"
        if not hmac.compare_digest(supplied, expected):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "workbench access required"})
            return False
        return True

    def _origin_is_expected(self) -> bool:
        return self.headers.get("Origin", "").rstrip("/").lower() == self.server.origin.lower()

    def _asset(self, name: str) -> None:
        paths = {
            "index.html": ASSET_DIRECTORY / "index.html",
            "app.css": ASSET_DIRECTORY / "app.css",
            "app.js": ASSET_DIRECTORY / "app.js",
            "peerbridge-icon.png": BRAND_ASSET_DIRECTORY / "peerbridge-icon.png",
            "peerbridge-icon.ico": BRAND_ASSET_DIRECTORY / "peerbridge-icon.ico",
            "peerbridge-modern-preview.png": BRAND_ASSET_DIRECTORY
            / "peerbridge-modern-preview.png",
            "peerbridge-pixel-preview.png": BRAND_ASSET_DIRECTORY
            / "peerbridge-pixel-preview.png",
        }
        path = paths.get(name)
        if path is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = path.read_bytes()
        except OSError:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "workbench asset unavailable"})
            return
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith((".html", ".css", ".js")):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, payload, content_type)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/favicon.ico":
            self._asset("peerbridge-icon.ico")
            return
        if parsed.path in {"/", "/index.html"}:
            self._asset("index.html")
            return
        if parsed.path == "/assets/app.css":
            self._asset("app.css")
            return
        if parsed.path == "/assets/app.js":
            self._asset("app.js")
            return
        if parsed.path == "/assets/peerbridge-icon.png":
            self._asset("peerbridge-icon.png")
            return
        if parsed.path == "/assets/peerbridge-modern-preview.png":
            self._asset("peerbridge-modern-preview.png")
            return
        if parsed.path == "/assets/peerbridge-pixel-preview.png":
            self._asset("peerbridge-pixel-preview.png")
            return
        if parsed.path == "/healthz":
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "transport": "loopback",
                    "instance_id": self.server.config.instance_id,
                    "token_sha256": hashlib.sha256(
                        self.server.config.token.encode("utf-8")
                    ).hexdigest(),
                },
            )
            return
        if parsed.path == "/api/worktree/diff":
            if not self._authorized():
                return
            try:
                payload = _worktree_diff(self.server.config.project_root)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "worktree diff unavailable"},
                )
                return
            self._json(HTTPStatus.OK, payload)
            return
        if parsed.path != "/api/bootstrap":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        query = parse_qs(parsed.query)
        room_id = str(query.get("room_id", [self.server.config.initial_room_id])[0])
        if not SAFE_IDENTIFIER.fullmatch(room_id):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid room ID"})
            return
        try:
            before_raw = query.get("before_sequence", [None])[0]
            before = int(before_raw) if before_raw not in {None, ""} else None
            payload = workbench_payload(
                self.server.reader,
                project_root=self.server.config.project_root,
                scope=self.server.config.scope,
                room_id=room_id,
                before_sequence=before,
                managed_sessions=self.server.managed_session_snapshots(),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "coordination snapshot unavailable"},
            )
            return
        etag = f'"{payload["signature"]}"'
        if self.headers.get("If-None-Match") == etag:
            self._send(HTTPStatus.NOT_MODIFIED, b"", "application/json", extra_headers={"ETag": etag})
            return
        self._json(HTTPStatus.OK, payload, extra_headers={"ETag": etag})

    def _read_json_request(self) -> dict[str, Any]:
        if not self.headers.get("Content-Type", "").lower().startswith(
            "application/json"
        ):
            raise WorkbenchError("application/json required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise WorkbenchError("request size rejected") from exc
        if length < 1 or length > MAX_REQUEST_BYTES:
            raise WorkbenchError("request size rejected")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise WorkbenchError("JSON object required")
        return payload

    def _human_client(self) -> McpHumanClient:
        return McpHumanClient(
            self.server.config.project_root,
            self.server.config.db_path,
            self.server.config.scope,
            agent_id=HUMAN_AGENT_ID,
            client_name="peerbridge-workbench",
        )

    def _action_result(
        self,
        *,
        request_id: str,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        response = {"status": "updated", "result": _safe_action_value(result)}
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _stage_browser_attachments(
        self, attachment_rows: object
    ) -> tuple[StagedAttachment, ...]:
        if not isinstance(attachment_rows, list):
            raise WorkbenchError("attachments must be a list")
        if len(attachment_rows) > MAX_CHAT_ATTACHMENT_COUNT:
            raise WorkbenchError("too many chat attachments")
        encoded_total = 0
        decoded_attachments: list[tuple[str, bytes]] = []
        for row in attachment_rows:
            if not isinstance(row, dict) or set(row) != {"name", "content_base64"}:
                raise WorkbenchError("invalid chat attachment record")
            name = row.get("name")
            encoded = row.get("content_base64")
            if not isinstance(name, str) or not isinstance(encoded, str):
                raise WorkbenchError("invalid chat attachment record")
            encoded_total += len(encoded)
            if encoded_total > ((MAX_CHAT_ATTACHMENT_TOTAL_BYTES * 4) // 3) + 32:
                raise WorkbenchError("chat attachments exceed the total size limit")
            try:
                decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                raise WorkbenchError("invalid chat attachment encoding") from exc
            decoded_attachments.append((name, decoded))
        return stage_chat_attachment_payloads(
            self.server.config.project_root,
            decoded_attachments,
        )

    @staticmethod
    def _legacy_attachment_prompt(
        text: str, attachments: tuple[StagedAttachment, ...]
    ) -> str:
        if not attachments:
            return text
        references = "\n".join(f"- {item.relative_path}" for item in attachments)
        return (
            f"{str(text or '').rstrip()}\n\n"
            "[PeerBridge project-root-relative attachments]\n"
            f"{references}"
        ).strip()

    def _handle_action(self, path: str, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("request_id") or "")
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "write rate limit exceeded"},
            )
            return
        client = self._human_client()
        if path == "/api/room/create":
            allowed = {"request_id", "room_id", "name"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported room fields")
            room_id = str(payload.get("room_id") or "").strip()
            name = str(payload.get("name") or "").strip()
            if not SAFE_IDENTIFIER.fullmatch(room_id):
                raise WorkbenchError("invalid room ID")
            if room_id.startswith("history."):
                raise WorkbenchError("history room IDs are reserved")
            if not name or len(name) > 120:
                raise WorkbenchError("invalid room name")
            result = client.create_room(room_id=room_id, name=name)
        elif path == "/api/history/continue":
            allowed = {"request_id", "source_room_id", "room_id", "name"}
            if set(payload) != allowed:
                raise WorkbenchError("unsupported history continuation fields")
            source_room_id = str(payload.get("source_room_id") or "").strip()
            room_id = str(payload.get("room_id") or "").strip()
            name = str(payload.get("name") or "").strip()
            if not source_room_id.startswith("history.") or not SAFE_IDENTIFIER.fullmatch(source_room_id):
                raise WorkbenchError("invalid history source room")
            if not SAFE_IDENTIFIER.fullmatch(room_id) or room_id.startswith("history."):
                raise WorkbenchError("invalid continuation room ID")
            if not name or len(name) > 120:
                raise WorkbenchError("invalid room name")
            matches = [
                row
                for row in list_conversation_imports(
                    self.server.config.project_root, self.server.config.scope
                )
                if str(row.get("room_id") or "") == source_room_id
            ]
            if len(matches) != 1:
                raise WorkbenchError("imported history source is missing or ambiguous")
            source = matches[0]
            source_view = imported_room_view(source, rooms=(), limit=120)
            context_lines: list[str] = []
            context_bytes = 0
            for message in reversed(source_view.get("messages") or []):
                line = f"{message.get('sender') or 'Agent'}: {_public_text(message.get('body'))}"
                encoded_length = len(line.encode("utf-8")) + 1
                if context_bytes + encoded_length > 16_000:
                    break
                context_lines.insert(0, line)
                context_bytes += encoded_length
            source_sha256 = str(source.get("source_sha256") or "")
            source_conversation_id = _public_text(source.get("source_conversation_id"))
            continuation_body = "\n".join(
                [
                    "PEERBRIDGE_HISTORY_CONTINUATION_V1",
                    f"Provider: {source.get('provider') or '--'}",
                    f"Source conversation: {source_conversation_id}",
                    f"Source SHA-256: {source_sha256}",
                    "",
                    *context_lines,
                ]
            )
            client.create_room(room_id=room_id, name=name)
            message = client.send_message(
                room_id=room_id,
                recipient="*",
                task_id=f"history-continuation-{hashlib.sha256(source_room_id.encode('utf-8')).hexdigest()[:20]}",
                subject="History continuation",
                body=continuation_body,
                priority="normal",
            )
            result = {
                "status": "created",
                "room_id": room_id,
                "source_room_id": source_room_id,
                "source_sha256": source_sha256,
                "context_message_id": message.get("message_id"),
                "context_sha256": message.get("content_sha256"),
            }
        elif path == "/api/room/member":
            allowed = {
                "request_id",
                "action",
                "room_id",
                "agent_id",
                "route_profile_id",
                "role_id",
                "role_label",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported room member fields")
            action = str(payload.get("action") or "").lower()
            room_id = str(payload.get("room_id") or "")
            agent_id = str(payload.get("agent_id") or "")
            route_profile_id = str(payload.get("route_profile_id") or "")
            role_id = str(payload.get("role_id") or "equal-participant")
            role_label = str(payload.get("role_label") or "").strip()
            if action not in {"join", "leave"}:
                raise WorkbenchError("invalid room member action")
            if not SAFE_IDENTIFIER.fullmatch(room_id) or not SAFE_IDENTIFIER.fullmatch(
                agent_id
            ):
                raise WorkbenchError("invalid room member")
            if room_id.startswith("history."):
                raise WorkbenchError("imported history rooms are read-only")
            if route_profile_id and not SAFE_IDENTIFIER.fullmatch(route_profile_id):
                raise WorkbenchError("invalid route profile ID")
            if role_id not in ROOM_ROLE_IDS:
                raise WorkbenchError("invalid role")
            if role_id == "custom" and not role_label:
                raise WorkbenchError("custom role label required")
            if len(role_label) > 80:
                raise WorkbenchError("role label is too long")
            if action == "join":
                result = client.join_room(
                    room_id=room_id,
                    agent_id=agent_id,
                    route_profile_id=route_profile_id or None,
                    role_id=role_id,
                    role_label=role_label or None,
                )
            else:
                result = client.leave_room(room_id=room_id, agent_id=agent_id)
        elif path == "/api/workflow/enqueue":
            allowed = {
                "request_id",
                "workflow_id",
                "task_text",
                "max_attempts",
                "timeout_seconds",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported workflow fields")
            workflow_id = str(payload.get("workflow_id") or "")
            task_text = str(payload.get("task_text") or "").strip()
            if workflow_id not in WORKFLOW_TEMPLATES:
                raise WorkbenchError("invalid workflow template")
            if not task_text or len(task_text) > MAX_BODY_CHARS:
                raise WorkbenchError("invalid workflow task")
            default_attempts = (
                3 if WORKFLOW_TEMPLATES[workflow_id]["automatic_retry"] else 1
            )
            max_attempts = _safe_int(payload.get("max_attempts"), default_attempts)
            timeout_seconds = _safe_int(payload.get("timeout_seconds"), 1800)
            if not 1 <= max_attempts <= 10:
                raise WorkbenchError("max attempts must be 1..10")
            if not 30 <= timeout_seconds <= 86_400:
                raise WorkbenchError("workflow timeout must be 30..86400 seconds")
            operation_id = f"workbench-{uuid.uuid4().hex}"
            result = client.enqueue_workflow(
                operation_id=operation_id,
                workflow_id=workflow_id,
                task_text=task_text,
                working_directory=str(self.server.config.project_root),
                resource_key=repository_resource_key(
                    self.server.config.project_root
                ),
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
            )
        elif path == "/api/operation/cancel":
            allowed = {"request_id", "operation_id", "reason"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported operation fields")
            operation_id = str(payload.get("operation_id") or "")
            reason = str(payload.get("reason") or "Operator cancelled from Workbench").strip()
            if not SAFE_IDENTIFIER.fullmatch(operation_id):
                raise WorkbenchError("invalid operation ID")
            if not reason or len(reason) > 500:
                raise WorkbenchError("invalid cancellation reason")
            result = client.cancel_operation(
                operation_id=operation_id,
                reason=reason,
            )
        elif path == "/api/schedule/save":
            allowed = {
                "request_id",
                "schedule_id",
                "workflow_id",
                "task_text",
                "interval_minutes",
                "start_delay_minutes",
                "enabled",
                "permission_decision_id",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported schedule fields")
            schedule_id = str(payload.get("schedule_id") or "").strip()
            workflow_id = str(payload.get("workflow_id") or "").strip()
            task_text = str(payload.get("task_text") or "").strip()
            permission_decision_id = str(
                payload.get("permission_decision_id") or ""
            ).strip()
            interval_minutes = _safe_int(payload.get("interval_minutes"), 60)
            start_delay_minutes = _safe_int(payload.get("start_delay_minutes"), 1)
            enabled = payload.get("enabled", True)
            if not SAFE_IDENTIFIER.fullmatch(schedule_id):
                raise WorkbenchError("invalid schedule ID")
            if workflow_id not in WORKFLOW_TEMPLATES:
                raise WorkbenchError("invalid workflow template")
            if not task_text or len(task_text) > MAX_BODY_CHARS:
                raise WorkbenchError("invalid schedule task")
            if permission_decision_id and not SAFE_IDENTIFIER.fullmatch(
                permission_decision_id
            ):
                raise WorkbenchError("invalid permission decision ID")
            if not 1 <= interval_minutes <= 44_640:
                raise WorkbenchError("schedule interval must be 1..44640 minutes")
            if not 0 <= start_delay_minutes <= 44_640:
                raise WorkbenchError("schedule delay must be 0..44640 minutes")
            if not isinstance(enabled, bool):
                raise WorkbenchError("schedule enabled must be boolean")
            arguments: dict[str, Any] = {
                "schedule_id": schedule_id,
                "workflow_id": workflow_id,
                "task_text": task_text,
                "working_directory": ".",
                "resource_key": repository_resource_key(
                    self.server.config.project_root
                ),
                "interval_seconds": interval_minutes * 60,
                "next_run_epoch": time.time() + (start_delay_minutes * 60),
                "enabled": enabled,
            }
            if permission_decision_id:
                arguments["permission_decision_id"] = permission_decision_id
            result = client.call_tool("save_workflow_schedule", arguments)
        elif path == "/api/schedule/enabled":
            allowed = {"request_id", "schedule_id", "enabled"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported schedule state fields")
            schedule_id = str(payload.get("schedule_id") or "").strip()
            enabled = payload.get("enabled")
            if not SAFE_IDENTIFIER.fullmatch(schedule_id):
                raise WorkbenchError("invalid schedule ID")
            if not isinstance(enabled, bool):
                raise WorkbenchError("schedule enabled must be boolean")
            result = client.call_tool(
                "set_workflow_schedule_enabled",
                {"schedule_id": schedule_id, "enabled": enabled},
            )
        elif path == "/api/audit/verify":
            if set(payload) != {"request_id"}:
                raise WorkbenchError("unsupported audit verification fields")
            result = client.call_tool("verify_audit_chain", {})
        elif path == "/api/capabilities/discover":
            if set(payload) != {"request_id"}:
                raise WorkbenchError("unsupported capability discovery fields")
            result = _discover_native_agent_capabilities(
                self.server.config.project_root
            )
        elif path == "/api/capability/register":
            allowed = {
                "request_id",
                "capability_id",
                "registry_version",
                "kind",
                "display_name",
                "source_sha256",
                "sensitivity",
                "enabled",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported capability fields")
            capability_id = str(payload.get("capability_id") or "").strip()
            registry_version = str(payload.get("registry_version") or "").strip()
            kind = str(payload.get("kind") or "").strip()
            display_name = str(payload.get("display_name") or "").strip()
            source_sha256 = _valid_sha256(payload.get("source_sha256"))
            sensitivity = str(payload.get("sensitivity") or "").strip()
            enabled = payload.get("enabled", True)
            if not SAFE_IDENTIFIER.fullmatch(capability_id):
                raise WorkbenchError("invalid capability ID")
            if not SAFE_IDENTIFIER.fullmatch(registry_version):
                raise WorkbenchError("invalid capability version")
            if kind not in {"skill", "mcp-tool"}:
                raise WorkbenchError("invalid capability kind")
            if not display_name or len(display_name) > 160:
                raise WorkbenchError("invalid capability display name")
            if source_sha256 is None:
                raise WorkbenchError("invalid capability source SHA-256")
            if sensitivity not in {"read", "write", "sensitive"}:
                raise WorkbenchError("invalid capability sensitivity")
            if not isinstance(enabled, bool):
                raise WorkbenchError("capability enabled must be boolean")
            result = client.call_tool(
                "register_capability",
                {
                    "capability_id": capability_id,
                    "registry_version": registry_version,
                    "kind": kind,
                    "display_name": display_name,
                    "source_sha256": source_sha256,
                    "sensitivity": sensitivity,
                    "enabled": enabled,
                },
            )
        elif path == "/api/capability/grant":
            allowed = {
                "request_id",
                "principal_type",
                "principal_id",
                "capability_id",
                "registry_version",
                "decision",
                "reason",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported capability grant fields")
            principal_type = str(payload.get("principal_type") or "").strip()
            principal_id = str(payload.get("principal_id") or "").strip()
            capability_id = str(payload.get("capability_id") or "").strip()
            registry_version = str(payload.get("registry_version") or "").strip()
            decision = str(payload.get("decision") or "").strip()
            reason = str(payload.get("reason") or "").strip()
            if principal_type not in {"agent", "room"}:
                raise WorkbenchError("invalid capability principal type")
            if not SAFE_IDENTIFIER.fullmatch(principal_id):
                raise WorkbenchError("invalid capability principal ID")
            if not SAFE_IDENTIFIER.fullmatch(capability_id):
                raise WorkbenchError("invalid capability ID")
            if not SAFE_IDENTIFIER.fullmatch(registry_version):
                raise WorkbenchError("invalid capability version")
            if decision not in {"allow", "deny"}:
                raise WorkbenchError("invalid capability decision")
            if not reason or len(reason) > 500:
                raise WorkbenchError("invalid capability grant reason")
            result = client.call_tool(
                "grant_capability",
                {
                    "principal_type": principal_type,
                    "principal_id": principal_id,
                    "capability_id": capability_id,
                    "registry_version": registry_version,
                    "decision": decision,
                    "reason": reason,
                },
            )
        elif path == "/api/identity/authorize":
            allowed = {"request_id", "agent_id", "profile"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported identity authorization fields")
            agent_id = str(payload.get("agent_id") or "").strip()
            profile = str(payload.get("profile") or "collaborator").strip()
            if not SAFE_IDENTIFIER.fullmatch(agent_id):
                raise WorkbenchError("invalid Agent ID")
            if agent_id in RESERVED_IDENTITY_IDS or agent_id.startswith(
                "control-room-"
            ):
                raise WorkbenchError("reserved operator identity cannot be issued")
            if profile not in IDENTITY_PROFILES:
                raise WorkbenchError("invalid identity capability profile")
            now_epoch = time.time()
            governance = ExecutionGovernance(
                Bridge(
                    self.server.config.project_root,
                    self.server.config.db_path,
                    HUMAN_AGENT_ID,
                    self.server.config.scope,
                    session_id="workbench-identity-authorization",
                    client_name="peerbridge-workbench",
                )
            )
            decision = governance.decide_permission(
                task_id=f"identity-issue:{agent_id}",
                agent_id=agent_id,
                action="identity.capability.issue",
                resource_key=f"identity-profile:{profile}",
                decision="allow",
                reason="Operator authorized one bounded Agent identity capability issue.",
                expires_epoch=now_epoch + 10 * 60,
                now_epoch=now_epoch,
                decision_id=f"identityauth-{uuid.uuid4().hex}",
            )
            result = {
                "agent_id": agent_id,
                "profile": profile,
                "permission_decision_id": decision["decision_id"],
                "permission_decision_sha256": decision["decision_sha256"],
                "expires_epoch": decision["expires_epoch"],
                "consumed": False,
            }
        elif path == "/api/identity/revoke":
            allowed = {"request_id", "capability_id", "reason"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported identity revocation fields")
            capability_id = str(payload.get("capability_id") or "").strip()
            reason = str(payload.get("reason") or "").strip()
            if not SAFE_IDENTIFIER.fullmatch(capability_id):
                raise WorkbenchError("invalid identity capability ID")
            if not reason or len(reason) > 500:
                raise WorkbenchError("invalid identity revocation reason")
            revoked = revoke_agent_identity_capability(
                self.server.config.db_path,
                self.server.config.scope,
                capability_id,
            )
            if not revoked:
                raise WorkbenchError("identity capability is unknown or already revoked")
            audit = Bridge(
                self.server.config.project_root,
                self.server.config.db_path,
                HUMAN_AGENT_ID,
                self.server.config.scope,
                session_id="workbench-identity-revocation",
                client_name="peerbridge-workbench",
            )
            with audit._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                event = audit._event(
                    connection,
                    "identity.capability.revoked",
                    {
                        "capability_id": capability_id,
                        "reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
                    },
                )
            result = {
                "capability_id": capability_id,
                "revoked": True,
                "audit_chain_sha256": event["chain_sha256"],
            }
        elif path == "/api/permission/decide":
            allowed = {
                "request_id",
                "decision_id",
                "task_id",
                "agent_id",
                "decision",
                "reason",
                "ttl_hours",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported permission fields")
            decision_id = str(payload.get("decision_id") or "").strip()
            task_id = str(payload.get("task_id") or "").strip()
            agent_id = str(payload.get("agent_id") or "").strip()
            decision = str(payload.get("decision") or "").strip()
            reason = str(payload.get("reason") or "").strip()
            ttl_hours = _safe_int(payload.get("ttl_hours"), 1)
            for value, label in (
                (decision_id, "permission decision ID"),
                (task_id, "permission task ID"),
                (agent_id, "permission Agent ID"),
            ):
                if not SAFE_IDENTIFIER.fullmatch(value):
                    raise WorkbenchError(f"invalid {label}")
            if decision not in {"allow", "deny"}:
                raise WorkbenchError("invalid permission decision")
            if not reason or len(reason) > 500:
                raise WorkbenchError("invalid permission reason")
            if not 1 <= ttl_hours <= 24:
                raise WorkbenchError("permission lifetime must be 1..24 hours")
            result = client.call_tool(
                "decide_permission",
                {
                    "decision_id": decision_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "action": "git.worktree.create",
                    "resource_key": repository_resource_key(
                        self.server.config.project_root
                    ),
                    "decision": decision,
                    "reason": reason,
                    "expires_epoch": time.time() + (ttl_hours * 3600),
                },
            )
        elif path == "/api/execution/create":
            allowed = {
                "request_id",
                "binding_id",
                "task_id",
                "agent_id",
                "permission_decision_id",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported execution fields")
            binding_id = str(payload.get("binding_id") or "").strip()
            task_id = str(payload.get("task_id") or "").strip()
            agent_id = str(payload.get("agent_id") or "").strip()
            permission_decision_id = str(
                payload.get("permission_decision_id") or ""
            ).strip()
            for value, label in (
                (binding_id, "execution binding ID"),
                (task_id, "execution task ID"),
                (agent_id, "execution Agent ID"),
                (permission_decision_id, "permission decision ID"),
            ):
                if not SAFE_IDENTIFIER.fullmatch(value):
                    raise WorkbenchError(f"invalid {label}")
            result = client.call_tool(
                "create_execution_worktree",
                {
                    "binding_id": binding_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "permission_decision_id": permission_decision_id,
                    "repository": ".",
                    "base_commit": "HEAD",
                },
                timeout=60,
            )
        elif path in {"/api/execution/seal", "/api/execution/verify"}:
            allowed = {"request_id", "binding_id"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported execution action fields")
            binding_id = str(payload.get("binding_id") or "").strip()
            if not SAFE_IDENTIFIER.fullmatch(binding_id):
                raise WorkbenchError("invalid execution binding ID")
            if path == "/api/execution/seal":
                active = [
                    row
                    for row in self.server.managed_session_snapshots()
                    if row.get("governance_binding_id") == binding_id
                    and row.get("state") not in TERMINAL_STATES
                ]
                if active:
                    raise WorkbenchError(
                        "execution binding cannot be sealed while a writer session is active"
                    )
            tool = (
                "seal_execution"
                if path == "/api/execution/seal"
                else "verify_execution_source"
            )
            result = client.call_tool(tool, {"binding_id": binding_id}, timeout=60)
        elif path == "/api/proof/export":
            allowed = {"request_id", "task_id"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported Proof Bundle fields")
            task_id = str(payload.get("task_id") or "").strip()
            if not SAFE_IDENTIFIER.fullmatch(task_id):
                raise WorkbenchError("invalid Proof Bundle task ID")
            output_path = (
                f".peerbridge-artifacts/proof-bundles/"
                f"{task_id}-{uuid.uuid4().hex[:12]}"
            )
            result = client.call_tool(
                "export_proof_bundle",
                {"task_id": task_id, "output_path": output_path},
                timeout=60,
            )
        elif path == "/api/proof/verify":
            allowed = {"request_id", "bundle_path"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported Proof Bundle verification fields")
            bundle_path = str(payload.get("bundle_path") or "").strip().replace(
                "\\", "/"
            )
            parts = bundle_path.split("/")
            if (
                not bundle_path.startswith(
                    ".peerbridge-artifacts/proof-bundles/"
                )
                or len(bundle_path) > 500
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise WorkbenchError("invalid Proof Bundle path")
            result = client.call_tool(
                "verify_proof_bundle",
                {"bundle_path": bundle_path},
                timeout=60,
            )
        elif path == "/api/session/start":
            allowed = {
                "attachments",
                "request_id",
                "agent_id",
                "role",
                "permission_tier",
                "approval_mode",
                "requested_route",
                "working_directory",
                "input_text",
                "governance_binding_id",
                "authorization_confirmed",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported managed session fields")
            agent_id = str(payload.get("agent_id") or "")
            role = str(payload.get("role") or COCKPIT_DEFAULT_LAUNCH_ROLE)
            permission_tier = str(payload.get("permission_tier") or "observe")
            approval_mode = (
                str(payload.get("approval_mode") or "").strip()
                or _default_approval_mode(permission_tier)
            )
            requested_route = str(payload.get("requested_route") or "").strip()
            selected_directory = str(payload.get("working_directory") or ".").strip()
            input_text = str(payload.get("input_text") or "")
            governance_binding_id = str(
                payload.get("governance_binding_id") or ""
            ).strip()
            authorization_confirmed = payload.get("authorization_confirmed")
            attachment_rows = payload.get("attachments") or []
            if agent_id not in MANAGED_AGENT_IDS:
                raise WorkbenchError("unsupported managed Agent")
            if role not in COCKPIT_ROLES:
                raise WorkbenchError("invalid managed Agent role")
            if permission_tier not in MANAGED_PERMISSION_TIERS:
                raise WorkbenchError("invalid managed Agent permission tier")
            if approval_mode not in APPROVAL_MODES:
                raise WorkbenchError("invalid managed Agent approval mode")
            governed_write = permission_tier in {"edit", "full-development"}
            if governed_write and not SAFE_IDENTIFIER.fullmatch(governance_binding_id):
                raise WorkbenchError(
                    "edit and full-development sessions require an active governed worktree"
                )
            if not governed_write and governance_binding_id:
                raise WorkbenchError(
                    "read-only sessions must not claim a governed write binding"
                )
            if governed_write and authorization_confirmed is not True:
                raise WorkbenchError(
                    "write-capable sessions require explicit one-time authorization"
                )
            if not governed_write and authorization_confirmed not in {None, False}:
                raise WorkbenchError(
                    "read-only sessions must not claim write authorization"
                )
            if requested_route and not SAFE_ROUTE.fullmatch(requested_route):
                raise WorkbenchError("invalid requested route")
            if len(selected_directory) > 500:
                raise WorkbenchError("working directory is too long")
            if len(input_text) > MAX_BODY_CHARS:
                raise WorkbenchError("managed Agent input is too large")
            session_id = f"managed-{agent_id}-{uuid.uuid4().hex[:20]}"
            session_authorization: dict[str, Any] | None = None
            if governed_write:
                governance = ExecutionGovernance(
                    Bridge(
                        self.server.config.project_root,
                        self.server.config.db_path,
                        HUMAN_AGENT_ID,
                        self.server.config.scope,
                        session_id="workbench-governed-launch",
                        client_name="peerbridge-workbench",
                    )
                )
                launch_binding = governance.resolve_launch_binding(
                    governance_binding_id,
                    agent_id,
                )
                working_directory = Path(launch_binding["worktree_path"])
                now_epoch = time.time()
                decision = governance.decide_permission(
                    task_id=session_id,
                    agent_id=agent_id,
                    action="managed.session.start",
                    resource_key=governance_binding_id,
                    decision="allow",
                    reason="Operator confirmed one-time authorization for this managed session.",
                    expires_epoch=now_epoch + MANAGED_SESSION_AUTHORIZATION_SECONDS,
                    now_epoch=now_epoch,
                    decision_id=f"sessionauth-{uuid.uuid4().hex}",
                )
                governance.authorize_permission(
                    str(decision["decision_id"]),
                    task_id=session_id,
                    agent_id=agent_id,
                    action="managed.session.start",
                    resource_key=governance_binding_id,
                    consume=True,
                )
                session_authorization = {
                    "mode": "once-per-session",
                    "permission_tier": permission_tier,
                    "approval_mode": approval_mode,
                    "governance_binding_id": governance_binding_id,
                    "decision_id": decision["decision_id"],
                    "decision_sha256": decision["decision_sha256"],
                    "authorized_by": HUMAN_AGENT_ID,
                    "expires_epoch": decision["expires_epoch"],
                }
            else:
                working_directory = cockpit_working_directory(
                    self.server.config.project_root,
                    selected_directory or ".",
                )
            staged = self._stage_browser_attachments(attachment_rows)
            start_official = getattr(
                self.server.managed_agents, "start_official", None
            )
            if callable(start_official):
                session = start_official(
                    agent_id=agent_id,
                    session_id=session_id,
                    role=role,
                    working_directory=working_directory,
                    requested_route=requested_route or None,
                    permission_tier=permission_tier,
                    approval_mode=approval_mode,
                    governance_binding_id=governance_binding_id or None,
                    project_root=self.server.config.project_root,
                    input_text=input_text if input_text.strip() else None,
                    attachments=staged,
                )
            else:
                launch = build_observe_launch(
                    agent_id,
                    session_id=session_id,
                    role=role,
                    working_directory=working_directory,
                    requested_route=requested_route or None,
                    permission_tier=permission_tier,
                )
                legacy_input = self._legacy_attachment_prompt(input_text, staged)
                session = self.server.managed_agents.start(
                    launch,
                    input_text=legacy_input if legacy_input.strip() else None,
                )
            raw_snapshot = dict(session.snapshot())
            if session_authorization is not None:
                self.server.remember_session_authorization(
                    session_id, session_authorization
                )
                raw_snapshot["session_authorization"] = session_authorization
            result = _safe_managed_session(raw_snapshot)
        elif path == "/api/session/action":
            allowed = {
                "attachments",
                "request_id",
                "session_id",
                "action",
                "input_text",
                "approval_id",
                "approval_decision",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported managed session action fields")
            session_id = str(payload.get("session_id") or "")
            action = str(payload.get("action") or "").lower()
            input_text = str(payload.get("input_text") or "")
            attachment_rows = payload.get("attachments") or []
            if not SAFE_IDENTIFIER.fullmatch(session_id):
                raise WorkbenchError("invalid managed session ID")
            if action not in {
                "send",
                "interrupt",
                "stop",
                "vision-test",
                "resume",
                "fork",
                "compact",
                "review",
                "approval",
            }:
                raise WorkbenchError("invalid managed session action")
            session = self.server.managed_agents.get(session_id)
            session_snapshot = session.snapshot()
            if (
                session_snapshot.get("permission_tier")
                in {"edit", "full-development"}
                and action
                in {
                    "send",
                    "vision-test",
                    "resume",
                    "fork",
                    "compact",
                    "review",
                    "approval",
                }
            ):
                binding_id = str(
                    session_snapshot.get("governance_binding_id") or ""
                )
                session_authorization = self.server.session_authorization(session_id)
                session_approval_mode = str(
                    session_snapshot.get("approval_mode")
                    or _default_approval_mode(
                        session_snapshot.get("permission_tier")
                    )
                )
                if (
                    session_authorization is None
                    or session_authorization.get("mode") != "once-per-session"
                    or session_authorization.get("permission_tier")
                    != session_snapshot.get("permission_tier")
                    or session_authorization.get("approval_mode")
                    != session_approval_mode
                    or session_authorization.get("governance_binding_id")
                    != binding_id
                ):
                    raise WorkbenchError(
                        "managed Agent session authorization is absent, expired, or mismatched"
                    )
                governance = ExecutionGovernance(
                    Bridge(
                        self.server.config.project_root,
                        self.server.config.db_path,
                        HUMAN_AGENT_ID,
                        self.server.config.scope,
                        session_id="workbench-governed-revalidation",
                        client_name="peerbridge-workbench",
                    )
                )
                live = governance.resolve_launch_binding(
                    binding_id,
                    str(session_snapshot.get("agent_id") or ""),
                )
                if Path(live["worktree_path"]).resolve() != Path(
                    session.working_directory
                ).resolve():
                    raise WorkbenchError(
                        "managed Agent worktree no longer matches its governance binding"
                    )
            if action == "approval":
                if input_text or attachment_rows:
                    raise WorkbenchError("approval action does not accept input")
                approval_id = str(payload.get("approval_id") or "")
                approval_decision = str(payload.get("approval_decision") or "")
                if not SAFE_IDENTIFIER.fullmatch(approval_id):
                    raise WorkbenchError("invalid approval ID")
                if approval_decision not in APPROVAL_DECISIONS:
                    raise WorkbenchError("invalid approval decision")
                resolver = getattr(session, "resolve_approval", None)
                if not callable(resolver):
                    raise WorkbenchError("managed Agent adapter has no approval channel")
                resolver(approval_id, approval_decision)
            elif action == "send":
                staged = self._stage_browser_attachments(attachment_rows)
                if (not input_text.strip() and not staged) or len(input_text) > MAX_BODY_CHARS:
                    raise WorkbenchError("managed Agent input is invalid")
                if bool(getattr(session, "supports_verified_attachments", False)):
                    session.submit(input_text, attachments=staged)
                else:
                    session.submit(self._legacy_attachment_prompt(input_text, staged))
            elif action == "interrupt":
                if input_text or attachment_rows:
                    raise WorkbenchError("interrupt does not accept input")
                session.interrupt()
            elif action == "vision-test":
                if input_text or attachment_rows:
                    raise WorkbenchError("vision test does not accept input")
                start_vision_probe = getattr(session, "start_vision_probe", None)
                if not callable(start_vision_probe):
                    raise WorkbenchError("managed Agent does not support vision testing")
                start_vision_probe()
            elif action == "review":
                if attachment_rows:
                    raise WorkbenchError("native review does not accept attachments")
                review = getattr(session, "review", None)
                if not callable(review):
                    raise WorkbenchError("managed Agent does not support native review")
                review(input_text.strip() or None)
            elif action in {"resume", "fork", "compact"}:
                if input_text or attachment_rows:
                    raise WorkbenchError(f"{action} does not accept input")
                native_action = getattr(session, action, None)
                if not callable(native_action):
                    raise WorkbenchError(
                        f"managed Agent does not support native {action}"
                    )
                native_action()
            else:
                if input_text or attachment_rows:
                    raise WorkbenchError("stop does not accept input")
                session.stop()
            raw_snapshot = dict(session.snapshot())
            active_authorization = self.server.session_authorization(session_id)
            if active_authorization is not None:
                raw_snapshot["session_authorization"] = active_authorization
            result = _safe_managed_session(raw_snapshot)
            result["terminal"] = result["state"] in TERMINAL_STATES
            if result["terminal"]:
                self.server.forget_session_authorization(session_id)
        elif path == "/api/room/automation":
            allowed = {
                "request_id",
                "room_id",
                "mode",
                "max_rounds",
                "max_messages",
                "stagnation_rounds",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported automation fields")
            room_id = str(payload.get("room_id") or "")
            mode = str(payload.get("mode") or "").lower()
            max_rounds = _safe_int(payload.get("max_rounds"))
            max_messages = _safe_int(payload.get("max_messages"))
            stagnation = _safe_int(payload.get("stagnation_rounds"))
            if not SAFE_IDENTIFIER.fullmatch(room_id):
                raise WorkbenchError("invalid room ID")
            if room_id.startswith("history."):
                raise WorkbenchError("imported history rooms are read-only")
            if mode not in {"off", "once", "discussion", "free", "goal"}:
                raise WorkbenchError("invalid automation mode")
            if mode == "goal":
                max_rounds = 0
                max_messages = 0
            else:
                if not 1 <= max_rounds <= 20:
                    raise WorkbenchError("max rounds must be 1..20")
                if not 2 <= max_messages <= 200:
                    raise WorkbenchError("max messages must be 2..200")
            if not 1 <= stagnation <= 5:
                raise WorkbenchError("stagnation rounds are invalid")
            if max_rounds and stagnation > max_rounds:
                raise WorkbenchError("stagnation rounds are invalid")
            result = client.set_room_automation(
                room_id=room_id,
                mode=mode,
                max_rounds=max_rounds,
                max_messages=max_messages,
                stagnation_rounds=stagnation,
            )
        elif path == "/api/update/check":
            allowed = {"request_id"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported update-check fields")
            result = check_for_updates(
                current_version=__version__,
                current_build_sha256=(
                    APP_BUILD_SHA256
                    if APP_BUILD_SHA256 != "unavailable"
                    else None
                ),
            ).as_dict()
        elif path == "/api/room/member-role":
            allowed = {
                "request_id",
                "room_id",
                "agent_id",
                "role_id",
                "role_label",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported member role fields")
            room_id = str(payload.get("room_id") or "")
            agent_id = str(payload.get("agent_id") or "")
            role_id = str(payload.get("role_id") or "")
            role_label = str(payload.get("role_label") or "").strip()
            if not SAFE_IDENTIFIER.fullmatch(room_id) or not SAFE_IDENTIFIER.fullmatch(
                agent_id
            ):
                raise WorkbenchError("invalid room member")
            if room_id.startswith("history."):
                raise WorkbenchError("imported history rooms are read-only")
            if role_id not in ROOM_ROLE_IDS:
                raise WorkbenchError("invalid role")
            if role_id == "custom" and not role_label:
                raise WorkbenchError("custom role label required")
            if len(role_label) > 80:
                raise WorkbenchError("role label is too long")
            result = client.set_room_member_role(
                room_id=room_id,
                agent_id=agent_id,
                role_id=role_id,
                role_label=role_label or None,
            )
        elif path == "/api/discussion/control":
            allowed = {"request_id", "discussion_id", "action", "extra_rounds"}
            if set(payload) - allowed:
                raise WorkbenchError("unsupported discussion fields")
            discussion_id = str(payload.get("discussion_id") or "")
            action = str(payload.get("action") or "").lower()
            extra_rounds = _safe_int(payload.get("extra_rounds"), 2)
            if not SAFE_IDENTIFIER.fullmatch(discussion_id):
                raise WorkbenchError("invalid discussion ID")
            if action not in {"pause", "resume", "stop", "continue"}:
                raise WorkbenchError("invalid discussion action")
            if not 1 <= extra_rounds <= 10:
                raise WorkbenchError("extra rounds must be 1..10")
            result = client.control_discussion(
                discussion_id=discussion_id,
                action=action,
                extra_rounds=extra_rounds,
            )
        else:
            raise WorkbenchError("unsupported action")
        self._action_result(
            request_id=request_id,
            payload=payload,
            result=result,
        )

    def _handle_feedback(self, payload: dict[str, Any]) -> None:
        allowed = {
            "request_id",
            "summary",
            "message",
            "contact",
            "locale",
            "credential_input",
            "include_encrypted_credential",
            "attachments",
        }
        if set(payload) - allowed:
            raise WorkbenchError("unsupported feedback fields")
        request_id = str(payload.get("request_id") or "")
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "write rate limit exceeded"},
            )
            return

        summary = str(payload.get("summary") or "").strip()
        message = str(payload.get("message") or "").strip()
        contact = str(payload.get("contact") or "").strip()
        locale = str(payload.get("locale") or "en")
        credential_input = str(payload.get("credential_input") or "")
        include_credential = payload.get("include_encrypted_credential", False)
        attachment_rows = payload.get("attachments") or []
        if not summary or len(summary) > MAX_SUMMARY_CHARS:
            raise WorkbenchError("invalid feedback summary")
        if not message or len(message) > MAX_FEEDBACK_MESSAGE_CHARS:
            raise WorkbenchError("invalid feedback message")
        if locale not in ANNOUNCEMENT_LOCALES:
            raise WorkbenchError("invalid feedback locale")
        if not isinstance(include_credential, bool):
            raise WorkbenchError("invalid encrypted credential consent")
        if len(credential_input) > MAX_CREDENTIAL_CHARS:
            raise WorkbenchError("credential input exceeds the local safety limit")
        if not isinstance(attachment_rows, list):
            raise WorkbenchError("feedback attachments must be a list")
        if len(attachment_rows) > MAX_FEEDBACK_ATTACHMENT_COUNT:
            raise WorkbenchError("too many feedback attachments")

        encoded_total = 0
        for row in attachment_rows:
            if not isinstance(row, dict) or set(row) != {"name", "content_base64"}:
                raise WorkbenchError("invalid feedback attachment record")
            name = row.get("name")
            encoded = row.get("content_base64")
            if not isinstance(name, str) or not isinstance(encoded, str):
                raise WorkbenchError("invalid feedback attachment record")
            if Path(name).name != name or not name or len(name) > 240:
                raise WorkbenchError("invalid feedback attachment name")
            if Path(name).suffix.lower() not in FEEDBACK_ATTACHMENT_SUFFIXES:
                raise WorkbenchError("unsupported feedback attachment type")
            encoded_total += len(encoded)
        if encoded_total > ((MAX_FEEDBACK_ATTACHMENT_TOTAL_BYTES * 4) // 3) + 64:
            raise WorkbenchError("feedback attachments exceed the total size limit")

        config = FeedbackConfig.load()
        attachment_paths: list[Path] = []
        with tempfile.TemporaryDirectory(prefix="peerbridge-feedback-") as temporary:
            temporary_root = Path(temporary)
            total_bytes = 0
            for index, row in enumerate(attachment_rows, start=1):
                try:
                    decoded = base64.b64decode(
                        str(row["content_base64"]).encode("ascii"), validate=True
                    )
                except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                    raise WorkbenchError("invalid feedback attachment encoding") from exc
                if len(decoded) > MAX_FEEDBACK_ATTACHMENT_BYTES:
                    raise WorkbenchError("feedback attachment exceeds the size limit")
                total_bytes += len(decoded)
                if total_bytes > MAX_FEEDBACK_ATTACHMENT_TOTAL_BYTES:
                    raise WorkbenchError("feedback attachments exceed the total size limit")
                attachment_dir = temporary_root / f"{index:02d}"
                attachment_dir.mkdir()
                attachment_path = attachment_dir / str(row["name"])
                with attachment_path.open("xb") as stream:
                    stream.write(decoded)
                attachment_paths.append(attachment_path)
            bundle = create_feedback_bundle(
                self.server.config.project_root,
                summary=summary,
                message=message,
                contact=contact,
                locale=locale,
                credential_input=credential_input,
                include_encrypted_credential=include_credential,
                attachment_paths=attachment_paths,
                attachment_consent=bool(attachment_paths),
                config=config,
            )

        try:
            delivery = deliver_feedback_bundle(bundle, config)
            delivered = bool(delivery.get("delivered"))
            notification_sent = delivery.get("notification_sent")
            delivery_failed = False
        except FeedbackError:
            delivered = False
            notification_sent = None
            delivery_failed = True
        response = {
            "status": "delivered" if delivered else "saved",
            "case_id": bundle.case_id,
            "bundle_sha256": bundle.sha256,
            "delivered": delivered,
            "notification_sent": notification_sent,
            "delivery_failed": delivery_failed,
            "encrypted_credential_included": bundle.encrypted_secret_included,
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.CREATED if delivered else HTTPStatus.ACCEPTED, response)

    def _handle_announcement_refresh(self, payload: dict[str, Any]) -> None:
        if set(payload) - {"request_id", "locale"}:
            raise WorkbenchError("unsupported announcement fields")
        request_id = str(payload.get("request_id") or "")
        locale = str(payload.get("locale") or "en")
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        if locale not in ANNOUNCEMENT_LOCALES:
            raise WorkbenchError("invalid announcement locale")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "write rate limit exceeded"},
            )
            return
        config = AnnouncementConfig.load()
        if config is None:
            raise WorkbenchError("announcement service is not configured")
        try:
            preferences = load_announcement_preferences(
                self.server.config.project_root
            )
        except AnnouncementError:
            preferences = fail_closed_announcement_preferences()
        if not preferences["network_enabled"]:
            raise WorkbenchError("announcement network access is disabled")
        cursor = str(preferences["cursors"][locale])
        rows = fetch_announcements(config, locale=locale, after_utc=cursor)
        cached = save_announcement_cache(self.server.config.project_root, rows)
        cursors = dict(preferences["cursors"])
        if rows:
            latest = max(
                rows,
                key=lambda item: datetime.fromisoformat(
                    item.published_utc.replace("Z", "+00:00")
                ),
            )
            cursors[locale] = latest.published_utc
        save_announcement_preferences(
            self.server.config.project_root,
            network_enabled=True,
            popup_enabled=bool(preferences["popup_enabled"]),
            read_ids=preferences["read_ids"],
            cursors=cursors,
        )
        response = {
            "status": "updated",
            "received": len(rows),
            "cached": len(cached),
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_announcements_read(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"locale", "request_id"}:
            raise WorkbenchError("unsupported announcement read fields")
        request_id = self._request_id(payload)
        locale = str(payload.get("locale") or "")
        if locale not in ANNOUNCEMENT_LOCALES:
            raise WorkbenchError("invalid announcement locale")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        if not self.server.allow_write():
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "write rate limit exceeded"})
            return
        try:
            preferences = load_announcement_preferences(self.server.config.project_root)
        except AnnouncementError:
            preferences = fail_closed_announcement_preferences()
        rows = load_announcement_cache(self.server.config.project_root)
        read_ids = set(preferences["read_ids"])
        read_ids.update(
            announcement_read_key(item) for item in rows if item.locale == locale
        )
        save_announcement_preferences(
            self.server.config.project_root,
            network_enabled=bool(preferences["network_enabled"]),
            popup_enabled=bool(preferences["popup_enabled"]),
            read_ids=read_ids,
            cursors=preferences["cursors"],
        )
        response = {"status": "read", "locale": locale, "read_count": len(read_ids)}
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_appearance_save(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"request_id", "surface"}:
            raise WorkbenchError("unsupported appearance fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        surface = str(payload.get("surface") or "")
        if surface not in {"pixel", "modern"}:
            raise WorkbenchError("unsupported desktop surface")
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "write rate limit exceeded"},
            )
            return
        try:
            preferences = load_preferences(self.server.config.project_root)
            saved = save_preferences(
                self.server.config.project_root,
                locale=str(preferences["locale"]),
                tutorial_completed=bool(preferences["tutorial_completed"]),
                theme=surface,
            )
        except LocalizationError as exc:
            raise WorkbenchError("appearance preference could not be saved") from exc
        response = {
            "status": "saved",
            "selected": str(saved["theme"]),
            "restart_required": True,
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_ui_preferences_save(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"request_id", "locale", "tutorial_completed"}:
            raise WorkbenchError("unsupported UI preference fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        locale = str(payload.get("locale") or "")
        tutorial_completed = payload.get("tutorial_completed")
        if locale not in SUPPORTED_LOCALES:
            raise WorkbenchError("unsupported UI locale")
        if not isinstance(tutorial_completed, bool):
            raise WorkbenchError("tutorial completion must be a boolean")
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "write rate limit exceeded"},
            )
            return
        try:
            preferences = load_preferences(self.server.config.project_root)
            saved = save_preferences(
                self.server.config.project_root,
                locale=locale,
                tutorial_completed=tutorial_completed,
                theme=str(preferences["theme"]),
            )
        except LocalizationError as exc:
            raise WorkbenchError("UI preferences could not be saved") from exc
        response = {
            "status": "saved",
            "locale": str(saved["locale"]),
            "tutorial_completed": bool(saved["tutorial_completed"]),
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    @staticmethod
    def _request_id(payload: dict[str, Any]) -> str:
        request_id = str(payload.get("request_id") or "")
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        return request_id

    def _begin_replayable_action(
        self, payload: dict[str, Any]
    ) -> tuple[str, str] | None:
        request_id = self._request_id(payload)
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return None
        return request_id, payload_sha256

    def _provider_connection(self, connection_id: str) -> dict[str, Any]:
        if not SAFE_IDENTIFIER.fullmatch(connection_id):
            raise WorkbenchError("invalid provider connection ID")
        result = self._human_client().call_tool(
            "list_provider_connections", {"enabled_only": True}
        )
        matches = [
            row
            for row in result.get("connections", [])
            if str(row.get("connection_id") or "") == connection_id
        ]
        if len(matches) != 1:
            raise WorkbenchError("provider connection is missing or ambiguous")
        return matches[0]

    def _handle_provider_save(self, payload: dict[str, Any]) -> None:
        allowed = {
            "api_key",
            "connection_id",
            "display_name",
            "endpoint",
            "request_id",
            "route_class",
        }
        if set(payload) != allowed:
            raise WorkbenchError("unsupported provider fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "provider save rate limit exceeded"},
            )
            return
        connection_id = str(payload.get("connection_id") or "").strip()
        display_name = str(payload.get("display_name") or "").strip()
        endpoint = str(payload.get("endpoint") or "").strip()
        route_class = str(payload.get("route_class") or "relay").strip().lower()
        api_key = str(payload.get("api_key") or "").strip()
        if not SAFE_IDENTIFIER.fullmatch(connection_id):
            raise WorkbenchError("invalid provider connection ID")
        if not display_name or len(display_name) > 200:
            raise WorkbenchError("invalid provider display name")
        if route_class not in {"official", "relay", "local"}:
            raise WorkbenchError("provider route class is invalid")
        if route_class == "local":
            if api_key:
                raise WorkbenchError("local provider routes do not accept an API key")
            credential = store_local_provider_endpoint(
                scope=self.server.config.scope,
                connection_id=connection_id,
                endpoint=endpoint,
                provider_id=connection_id,
            )
        else:
            credential = store_provider_credentials(
                scope=self.server.config.scope,
                connection_id=connection_id,
                endpoint=endpoint,
                api_key=api_key,
                route_class=route_class,
                provider_id=connection_id,
            )
        connection = self._human_client().call_tool(
            "upsert_provider_connection",
            {
                "connection_id": connection_id,
                "display_name": display_name,
                "route_class": credential.route_class,
                "provider_id": credential.provider_id,
                "secret_backend": "windows-credential-manager",
                "credential_target": credential.credential_target,
                "endpoint_sha256": credential.endpoint_sha256,
                "credential_fingerprint_sha256": (
                    credential.credential_fingerprint_sha256
                ),
                "descriptor_schema": credential.descriptor_schema,
                "credential_version_sha256": credential.credential_version_sha256,
                "enabled": True,
            },
        )
        response = {
            "status": "saved",
            "connection": {
                "connection_id": connection["connection_id"],
                "display_name": connection["display_name"],
                "route_class": connection["route_class"],
                "provider_id": connection["provider_id"],
                "endpoint_sha256": connection["endpoint_sha256"],
                "connection_sha256": connection["connection_sha256"],
                "secret_present": route_class != "local",
            },
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.CREATED, response)

    def _handle_provider_discover(self, payload: dict[str, Any]) -> None:
        if set(payload) - {"connection_id", "request_id", "timeout_seconds"}:
            raise WorkbenchError("unsupported provider discovery fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        connection_id = str(payload.get("connection_id") or "").strip()
        connection = self._provider_connection(connection_id)
        try:
            registry = discover_provider_models(
                scope=self.server.config.scope,
                connection_id=connection_id,
                route_class=str(connection["route_class"]),
                provider_id=str(connection["provider_id"]),
                timeout_seconds=float(payload.get("timeout_seconds") or 20),
            )
        except RunnerError as exc:
            raise WorkbenchError("provider model discovery failed") from exc
        models, models_truncated = _bounded_model_ids(registry.models)
        response = {
            "status": "discovered",
            "connection_id": connection_id,
            "models": list(models),
            "model_count": len(models),
            "models_truncated": models_truncated,
            "registry_sha256": registry.registry_sha256,
            "endpoint_sha256": registry.endpoint_sha256,
            "credential_version_sha256": registry.credential_version_sha256,
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_provider_route(self, payload: dict[str, Any]) -> None:
        allowed = {
            "agent_id",
            "connection_id",
            "inference_timeout_seconds",
            "model_id",
            "reasoning_mode",
            "request_id",
            "route_id",
        }
        if set(payload) - allowed:
            raise WorkbenchError("unsupported provider route fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "provider route rate limit exceeded"},
            )
            return
        connection_id = str(payload.get("connection_id") or "").strip()
        agent_id = str(payload.get("agent_id") or "").strip()
        model_id = str(payload.get("model_id") or "").strip()
        reasoning_mode = str(payload.get("reasoning_mode") or "").strip()
        route_id = str(payload.get("route_id") or "").strip()
        if not SAFE_IDENTIFIER.fullmatch(agent_id):
            raise WorkbenchError("invalid Agent ID")
        if not SAFE_MODEL_ID.fullmatch(model_id):
            raise WorkbenchError("invalid model ID")
        if reasoning_mode and not SAFE_IDENTIFIER.fullmatch(reasoning_mode):
            raise WorkbenchError("invalid reasoning mode")
        if not route_id:
            identity = f"{connection_id}:{agent_id}:{model_id}:{reasoning_mode}"
            route_id = f"route-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"
        if not SAFE_IDENTIFIER.fullmatch(route_id):
            raise WorkbenchError("invalid route ID")
        connection = self._provider_connection(connection_id)
        try:
            registry = discover_provider_models(
                scope=self.server.config.scope,
                connection_id=connection_id,
                route_class=str(connection["route_class"]),
                provider_id=str(connection["provider_id"]),
                timeout_seconds=float(payload.get("inference_timeout_seconds") or 20),
            )
        except RunnerError as exc:
            raise WorkbenchError("provider model discovery failed") from exc
        if model_id not in registry.models:
            raise WorkbenchError("provider does not advertise the selected model")
        route: dict[str, Any] = {
            "route_id": route_id,
            "agent_id": agent_id,
            "provider_id": connection_id,
            "model_id": model_id,
            "response_model_id": model_id,
            "route_class": connection["route_class"],
            "enabled": True,
        }
        if reasoning_mode:
            route["reasoning_mode"] = reasoning_mode
        timeout = payload.get("inference_timeout_seconds")
        if timeout not in {None, ""}:
            route["inference_timeout_seconds"] = int(timeout)
        result = self._human_client().call_tool("upsert_route_profile", route)
        response = {"status": "created", "route": result}
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.CREATED, response)

    def _handle_ccswitch_providers(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"app", "request_id"}:
            raise WorkbenchError("unsupported CC Switch provider fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        app = str(payload.get("app") or "").strip().lower()
        if app not in CCSWITCH_SUPPORTED_APPS:
            raise WorkbenchError("unsupported CC Switch application")
        providers = ccswitch_list_providers(app)
        response = {
            "status": "discovered",
            "app": app,
            "providers": [
                {
                    "provider_id": row.provider_id,
                    "name": row.name,
                    "current": row.current,
                    "has_endpoint": row.has_endpoint,
                }
                for row in providers
            ],
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_ccswitch_models(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"app", "provider_id", "request_id"}:
            raise WorkbenchError("unsupported CC Switch model fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        app = str(payload.get("app") or "").strip().lower()
        provider_id = str(payload.get("provider_id") or "")
        models, models_truncated = _bounded_model_ids(
            ccswitch_fetch_models(app, provider_id)
        )
        response = {
            "status": "discovered",
            "app": app,
            "models": list(models),
            "model_count": len(models),
            "models_truncated": models_truncated,
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_ccswitch_route(self, payload: dict[str, Any]) -> None:
        allowed = {
            "agent_id",
            "app",
            "model_id",
            "provider_id",
            "reasoning_mode",
            "request_id",
        }
        if set(payload) - allowed:
            raise WorkbenchError("unsupported CC Switch route fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "CC Switch route rate limit exceeded"},
            )
            return
        app = str(payload.get("app") or "").strip().lower()
        provider_id = str(payload.get("provider_id") or "")
        model_id = str(payload.get("model_id") or "").strip()
        agent_id = str(payload.get("agent_id") or "").strip()
        reasoning_mode = str(payload.get("reasoning_mode") or "").strip() or None
        if not SAFE_IDENTIFIER.fullmatch(agent_id):
            raise WorkbenchError("invalid Agent ID")
        identity = ccswitch_resolve_route_identity(
            app=app,
            route_class="relay",
            provider_id=provider_id,
            model_id=model_id,
        )
        provider = next(
            (
                row
                for row in ccswitch_list_providers(app)
                if row.provider_id == identity.provider_id
            ),
            None,
        )
        if provider is None:
            raise WorkbenchError("CC Switch provider identity changed during discovery")
        connection, routes = ccswitch_route_specs(
            provider,
            agent_id=agent_id,
            models=(model_id,),
            reasoning_mode=reasoning_mode,
        )
        client = self._human_client()
        saved_connection = client.call_tool("upsert_provider_connection", connection)
        saved_route = client.call_tool("upsert_route_profile", routes[0])
        response = {
            "status": "created",
            "identity_sha256": identity.identity_sha256,
            "connection": saved_connection,
            "route": saved_route,
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.CREATED, response)

    def _handle_ccswitch_switch(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"app", "confirmed", "provider_id", "request_id"}:
            raise WorkbenchError("unsupported CC Switch switch fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        if payload.get("confirmed") is not True:
            raise WorkbenchError("CC Switch provider change requires confirmation")
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "CC Switch change rate limit exceeded"},
            )
            return
        app = str(payload.get("app") or "").strip().lower()
        provider_id = str(payload.get("provider_id") or "")
        ccswitch_switch_provider(app, provider_id)
        response = {"status": "switched", "app": app}
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_agent_install(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"agent_id", "confirmed", "request_id", "update"}:
            raise WorkbenchError("unsupported Agent install fields")
        replay = self._begin_replayable_action(payload)
        if replay is None:
            return
        request_id, payload_sha256 = replay
        if payload.get("confirmed") is not True:
            raise WorkbenchError("Agent installation requires confirmation")
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "Agent install rate limit exceeded"},
            )
            return
        agent_id = str(payload.get("agent_id") or "").strip()
        spec = installable_agent_spec(agent_id)
        if not spec.automatic_install_supported:
            raise WorkbenchError("this Agent requires its publisher installation guide")
        process = launch_agent_installer(agent_id, update=bool(payload.get("update")))
        response = {
            "status": "launched",
            "agent_id": agent_id,
            "display_name": spec.display_name,
            "publisher": spec.publisher,
            "pid": process.pid,
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.ACCEPTED, response)

    def _handle_agent_catalog_refresh(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"request_id"}:
            raise WorkbenchError("unsupported Agent catalog fields")
        request_id = str(payload.get("request_id") or "")
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "refresh rate limit exceeded"},
            )
            return
        catalog = managed_agent_catalog(
            self.server.config.project_root,
            force_refresh=True,
        )
        response = {"status": "updated", "managed_agent_catalog": catalog}
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    @staticmethod
    def _history_source_bytes(content_base64: str) -> bytes:
        if len(content_base64) > ((MAX_HISTORY_IMPORT_BYTES * 4) // 3) + 64:
            raise WorkbenchError("history import exceeds the size limit")
        try:
            source = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorkbenchError("history import is not canonical base64") from exc
        if not source or len(source) > MAX_HISTORY_IMPORT_BYTES:
            raise WorkbenchError("history import exceeds the size limit")
        return source

    def _history_discovery_rows(
        self,
        *,
        provider: str,
        rows: list[dict[str, Any]],
        identity_key: str,
        source_revision: str | None = None,
        revision_key: str | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen_source_ids: set[str] = set()
        for row in rows[:50]:
            source_id = str(row.get(identity_key) or "").strip()
            if not source_id:
                continue
            if source_id in seen_source_ids:
                raise WorkbenchError(
                    "history discovery returned duplicate source identities"
                )
            seen_source_ids.add(source_id)
            revision = source_revision
            private_keys = {identity_key}
            if revision_key is not None:
                private_keys.add(revision_key)
                revision = str(row.get(revision_key) or "").strip().lower()
                if _valid_sha256(revision) is None:
                    raise WorkbenchError("history source revision is invalid")
            public = {
                key: value for key, value in row.items() if key not in private_keys
            }
            public["source_ref"] = hashlib.sha256(
                source_id.encode("utf-8")
            ).hexdigest()[:20]
            public["selection_handle"] = self.server.issue_history_selection(
                provider=provider,
                source_id=source_id,
                source_revision=revision,
            )
            result.append(public)
        return result

    def _handle_history_file_discover(self, payload: dict[str, Any]) -> None:
        allowed = {"request_id", "provider", "source_name", "content_base64"}
        if set(payload) - allowed:
            raise WorkbenchError("unsupported history file discovery fields")
        request_id = str(payload.get("request_id") or "")
        provider = str(payload.get("provider") or "").strip().lower()
        source_name = str(payload.get("source_name") or "").strip()
        content_base64 = str(payload.get("content_base64") or "").strip()
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        if provider not in HISTORY_IMPORT_PROVIDERS:
            raise WorkbenchError("unsupported history provider")
        if not source_name or len(source_name) > 240:
            raise WorkbenchError("invalid history source name")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        source = self._history_source_bytes(content_base64)
        source_sha256 = hashlib.sha256(source).hexdigest()
        metadata = conversation_export_metadata(provider, source)
        response = {
            "status": "ready",
            "provider": provider,
            "source_sha256": source_sha256,
            "conversations": self._history_discovery_rows(
                provider=provider,
                rows=metadata,
                identity_key="selection_id",
                source_revision=source_sha256,
            ),
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_history_import(self, payload: dict[str, Any]) -> None:
        allowed = {
            "request_id",
            "provider",
            "source_name",
            "content_base64",
            "selection_handles",
        }
        if set(payload) - allowed:
            raise WorkbenchError("unsupported history import fields")
        request_id = str(payload.get("request_id") or "")
        provider = str(payload.get("provider") or "").strip().lower()
        source_name = str(payload.get("source_name") or "").strip()
        content_base64 = str(payload.get("content_base64") or "").strip()
        selection_handles = payload.get("selection_handles")
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        if provider not in HISTORY_IMPORT_PROVIDERS:
            raise WorkbenchError("unsupported history provider")
        if not source_name or len(source_name) > 240:
            raise WorkbenchError("invalid history source name")
        if not isinstance(selection_handles, list):
            raise WorkbenchError("history selection is required")
        handles = [str(item or "").strip() for item in selection_handles]
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        source = self._history_source_bytes(content_base64)
        source_sha256 = hashlib.sha256(source).hexdigest()
        selected_ids = self.server.resolve_history_selections(
            handles,
            provider=provider,
            source_revision=source_sha256,
        )
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "history import rate limit exceeded"},
            )
            return
        results = import_conversation_export(
            project_root=self.server.config.project_root,
            scope=self.server.config.scope,
            provider=provider,
            source_name=source_name,
            content_base64=content_base64,
            selected_conversation_ids=selected_ids,
        )
        self.server.resolve_history_selections(
            handles,
            provider=provider,
            source_revision=source_sha256,
            consume=True,
        )
        self._finish_history_import(
            request_id=request_id,
            payload_sha256=payload_sha256,
            results=results,
        )

    def _finish_history_import(
        self,
        *,
        request_id: str,
        payload_sha256: str,
        results: list[dict[str, Any]],
    ) -> None:
        created = [row for row in results if row["status"] == "created"]
        if created:
            bridge = Bridge(
                self.server.config.project_root,
                self.server.config.db_path,
                HUMAN_AGENT_ID,
                self.server.config.scope,
                session_id="workbench-history-import",
                client_name="peerbridge-workbench",
            )
            with bridge._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for row in created:
                    bridge._event(
                        connection,
                        "cockpit.history_import.created",
                        {
                            "import_id": row["import_id"],
                            "room_id": row["room_id"],
                            "provider": row["provider"],
                            "message_count": row["message_count"],
                            "source_sha256": row["source_sha256"],
                            "record_sha256": row["record_sha256"],
                            "read_only": True,
                        },
                    )
        response = {
            "status": "imported",
            "imports": results,
            "room_id": results[0]["room_id"],
            "created_count": len(created),
            "existing_count": len(results) - len(created),
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_codex_history_discover(self, payload: dict[str, Any]) -> None:
        allowed = {"request_id", "search_term"}
        if set(payload) - allowed:
            raise WorkbenchError("unsupported Codex history discovery fields")
        request_id = str(payload.get("request_id") or "")
        search_term = str(payload.get("search_term") or "").strip()
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        if len(search_term) > 200:
            raise WorkbenchError("Codex history search term is too long")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        threads = self._history_discovery_rows(
            provider="codex",
            rows=discover_codex_threads(
                project_root=self.server.config.project_root,
                search_term=search_term,
            ),
            identity_key="thread_id",
            revision_key="source_revision",
        )
        response = {
            "status": "ready",
            "provider": "codex",
            "threads": threads,
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_codex_history_import(self, payload: dict[str, Any]) -> None:
        allowed = {"request_id", "selection_handle"}
        if set(payload) - allowed:
            raise WorkbenchError("unsupported Codex history import fields")
        request_id = str(payload.get("request_id") or "")
        selection_handle = str(payload.get("selection_handle") or "").strip()
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        selection = self.server.resolve_history_selection_records(
            [selection_handle], provider="codex"
        )[0]
        if selection.source_revision is None:
            raise WorkbenchError("Codex history selection has no source revision")
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "history import rate limit exceeded"},
            )
            return
        results = import_codex_thread(
            project_root=self.server.config.project_root,
            scope=self.server.config.scope,
            thread_id=selection.source_id,
            source_revision=selection.source_revision,
        )
        self.server.resolve_history_selections(
            [selection_handle],
            provider="codex",
            source_revision=selection.source_revision,
            consume=True,
        )
        self._finish_history_import(
            request_id=request_id,
            payload_sha256=payload_sha256,
            results=results,
        )

    def _handle_native_history_discover(self, payload: dict[str, Any]) -> None:
        allowed = {"request_id", "provider"}
        if set(payload) - allowed:
            raise WorkbenchError("unsupported native history discovery fields")
        request_id = str(payload.get("request_id") or "")
        provider = str(payload.get("provider") or "").strip().lower()
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        if provider not in {"claude", "grok", "kimi"}:
            raise WorkbenchError("unsupported native history provider")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        sessions = self._history_discovery_rows(
            provider=provider,
            rows=list_native_sessions(provider, self.server.config.project_root),
            identity_key="session_id",
            revision_key="source_revision",
        )
        response = {
            "status": "ready",
            "provider": provider,
            "sessions": sessions,
        }
        self.server.remember_result(request_id, payload_sha256, response)
        self._json(HTTPStatus.OK, response)

    def _handle_native_history_import(self, payload: dict[str, Any]) -> None:
        allowed = {"request_id", "provider", "selection_handle"}
        if set(payload) - allowed:
            raise WorkbenchError("unsupported native history import fields")
        request_id = str(payload.get("request_id") or "")
        provider = str(payload.get("provider") or "").strip().lower()
        selection_handle = str(payload.get("selection_handle") or "").strip()
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            raise WorkbenchError("invalid request ID")
        if provider not in {"claude", "grok", "kimi"}:
            raise WorkbenchError("unsupported native history provider")
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = self.server.prior_result(request_id, payload_sha256)
        if prior is not None:
            self._json(HTTPStatus.OK, prior)
            return
        selection = self.server.resolve_history_selection_records(
            [selection_handle], provider=provider
        )[0]
        if selection.source_revision is None:
            raise WorkbenchError("native history selection has no source revision")
        if not self.server.allow_write():
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "history import rate limit exceeded"},
            )
            return
        results = import_native_session(
            project_root=self.server.config.project_root,
            scope=self.server.config.scope,
            provider=provider,
            session_id=selection.source_id,
            source_revision=selection.source_revision,
        )
        self.server.resolve_history_selections(
            [selection_handle],
            provider=provider,
            source_revision=selection.source_revision,
            consume=True,
        )
        self._finish_history_import(
            request_id=request_id,
            payload_sha256=payload_sha256,
            results=results,
        )

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path not in {
            "/api/message",
            "/api/room/create",
            "/api/room/member",
            "/api/room/automation",
            "/api/update/check",
            "/api/room/member-role",
            "/api/discussion/control",
            "/api/workflow/enqueue",
            "/api/operation/cancel",
            "/api/schedule/save",
            "/api/schedule/enabled",
            "/api/capability/register",
            "/api/capability/grant",
            "/api/identity/authorize",
            "/api/identity/revoke",
            "/api/permission/decide",
            "/api/execution/create",
            "/api/execution/seal",
            "/api/execution/verify",
            "/api/proof/export",
            "/api/proof/verify",
            "/api/session/start",
            "/api/session/action",
            "/api/agents/refresh",
            "/api/agent/install",
            "/api/provider/save",
            "/api/provider/discover",
            "/api/provider/route",
            "/api/ccswitch/providers",
            "/api/ccswitch/models",
            "/api/ccswitch/route",
            "/api/ccswitch/switch",
            "/api/history/file/discover",
            "/api/history/continue",
            "/api/history/import",
            "/api/history/codex/discover",
            "/api/history/codex/import",
            "/api/history/native/discover",
            "/api/history/native/import",
            "/api/feedback",
            "/api/announcements/refresh",
            "/api/announcements/read",
            "/api/appearance/save",
            "/api/preferences/save",
            "/api/audit/verify",
            "/api/capabilities/discover",
        }:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        if not self._origin_is_expected():
            self._json(HTTPStatus.FORBIDDEN, {"error": "origin check failed"})
            return
        try:
            payload = self._read_json_request()
            if path == "/api/feedback":
                self._handle_feedback(payload)
                return
            if path == "/api/announcements/refresh":
                self._handle_announcement_refresh(payload)
                return
            if path == "/api/announcements/read":
                self._handle_announcements_read(payload)
                return
            if path == "/api/appearance/save":
                self._handle_appearance_save(payload)
                return
            if path == "/api/preferences/save":
                self._handle_ui_preferences_save(payload)
                return
            if path == "/api/agents/refresh":
                self._handle_agent_catalog_refresh(payload)
                return
            direct_handlers = {
                "/api/agent/install": self._handle_agent_install,
                "/api/provider/save": self._handle_provider_save,
                "/api/provider/discover": self._handle_provider_discover,
                "/api/provider/route": self._handle_provider_route,
                "/api/ccswitch/providers": self._handle_ccswitch_providers,
                "/api/ccswitch/models": self._handle_ccswitch_models,
                "/api/ccswitch/route": self._handle_ccswitch_route,
                "/api/ccswitch/switch": self._handle_ccswitch_switch,
            }
            direct_handler = direct_handlers.get(path)
            if direct_handler is not None:
                direct_handler(payload)
                return
            if path == "/api/history/file/discover":
                self._handle_history_file_discover(payload)
                return
            if path == "/api/history/import":
                self._handle_history_import(payload)
                return
            if path == "/api/history/codex/discover":
                self._handle_codex_history_discover(payload)
                return
            if path == "/api/history/codex/import":
                self._handle_codex_history_import(payload)
                return
            if path == "/api/history/native/discover":
                self._handle_native_history_discover(payload)
                return
            if path == "/api/history/native/import":
                self._handle_native_history_import(payload)
                return
            if path != "/api/message":
                self._handle_action(path, payload)
                return
            allowed = {
                "attachments",
                "request_id",
                "room_id",
                "recipient",
                "task_id",
                "subject",
                "body",
                "priority",
                "route_profile_id",
                "requested_provider_id",
                "requested_model_id",
                "requested_reasoning_mode",
            }
            if set(payload) - allowed:
                raise WorkbenchError("unsupported message fields")
            request_id = str(payload.get("request_id") or "")
            room_id = str(payload.get("room_id") or DEFAULT_ROOM_ID)
            recipient = str(payload.get("recipient") or "*")
            task_id = str(payload.get("task_id") or "")
            subject = str(payload.get("subject") or "").strip()
            body = str(payload.get("body") or "").strip()
            priority = str(payload.get("priority") or "normal")
            attachment_rows = payload.get("attachments") or []
            if not SAFE_REQUEST_ID.fullmatch(request_id):
                raise WorkbenchError("invalid request ID")
            for key, value in (("room ID", room_id), ("task ID", task_id)):
                if not SAFE_IDENTIFIER.fullmatch(value):
                    raise WorkbenchError(f"invalid {key}")
            if room_id.startswith("history."):
                raise WorkbenchError("imported history rooms are read-only")
            if recipient != "*" and not SAFE_IDENTIFIER.fullmatch(recipient):
                raise WorkbenchError("invalid recipient")
            if not subject or len(subject) > 500:
                raise WorkbenchError("invalid subject")
            if not body or len(body) > MAX_BODY_CHARS:
                raise WorkbenchError("invalid message body")
            if priority not in ALLOWED_PRIORITIES:
                raise WorkbenchError("invalid priority")
            if not isinstance(attachment_rows, list):
                raise WorkbenchError("attachments must be a list")
            if len(attachment_rows) > MAX_CHAT_ATTACHMENT_COUNT:
                raise WorkbenchError("too many chat attachments")
            encoded_total = 0
            for row in attachment_rows:
                if not isinstance(row, dict) or set(row) != {"name", "content_base64"}:
                    raise WorkbenchError("invalid chat attachment record")
                name = row.get("name")
                encoded = row.get("content_base64")
                if not isinstance(name, str) or not isinstance(encoded, str):
                    raise WorkbenchError("invalid chat attachment record")
                encoded_total += len(encoded)
            if encoded_total > ((MAX_CHAT_ATTACHMENT_TOTAL_BYTES * 4) // 3) + 32:
                raise WorkbenchError("chat attachments exceed the total size limit")
            for key in (
                "route_profile_id",
                "requested_provider_id",
                "requested_model_id",
                "requested_reasoning_mode",
            ):
                value = str(payload.get(key) or "")
                if value and not SAFE_IDENTIFIER.fullmatch(value):
                    raise WorkbenchError(f"invalid {key}")
            payload_sha256 = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            prior = self.server.prior_result(request_id, payload_sha256)
            if prior is not None:
                self._json(HTTPStatus.OK, prior)
                return
            if not self.server.allow_write():
                self._json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"error": "write rate limit exceeded"},
                )
                return
            decoded_attachments: list[tuple[str, bytes]] = []
            for row in attachment_rows:
                try:
                    decoded = base64.b64decode(
                        str(row["content_base64"]).encode("ascii"), validate=True
                    )
                except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                    raise WorkbenchError("invalid chat attachment encoding") from exc
                decoded_attachments.append((str(row["name"]), decoded))
            staged = stage_chat_attachment_payloads(
                self.server.config.project_root, decoded_attachments
            )
            artifact_paths = tuple(item.relative_path for item in staged)
            client = self._human_client()
            if recipient == "*":
                result = client.post_room_message(
                    room_id=room_id,
                    task_id=task_id,
                    subject=subject,
                    body=body,
                    priority=priority,
                    artifact_paths=artifact_paths,
                )
            else:
                result = client.send_message(
                    room_id=room_id,
                    recipient=recipient,
                    task_id=task_id,
                    subject=subject,
                    body=body,
                    priority=priority,
                    route_profile_id=payload.get("route_profile_id"),
                    requested_provider_id=payload.get("requested_provider_id"),
                    requested_model_id=payload.get("requested_model_id"),
                    requested_reasoning_mode=payload.get("requested_reasoning_mode"),
                    artifact_paths=artifact_paths,
                )
            response = {
                "status": "sent",
                "message_id": result.get("message_id"),
                "content_sha256": result.get("content_sha256"),
                "room_id": room_id,
                "attachments": [
                    {
                        "sha256": item.sha256,
                        "bytes": item.bytes,
                        "media_type": item.media_type,
                    }
                    for item in staged
                ],
            }
            self.server.remember_result(request_id, payload_sha256, response)
            self._json(HTTPStatus.CREATED, response)
        except (
            AgentInstallError,
            AnnouncementError,
            AttachmentError,
            CcSwitchError,
            CredentialStoreError,
            FeedbackError,
            GovernanceError,
            ManagedAgentError,
            json.JSONDecodeError,
            WorkbenchError,
            ValueError,
        ) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (OSError, RuntimeError):
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "MCP message path unavailable"})


def _workbench_path_is_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def make_server(
    project_root: Path,
    db_path: Path,
    scope: str,
    *,
    port: int = 0,
    token: str | None = None,
    initial_room_id: str = DEFAULT_ROOM_ID,
    instance_id: str | None = None,
    managed_agent_manager: ManagedAgentManager | None = None,
) -> WorkbenchServer:
    root = project_root.resolve()
    lexical_database = Path(db_path).absolute()
    lexical_state = Path(project_root).absolute() / ".peerbridge"
    try:
        lexical_database.relative_to(lexical_state)
        default_state = True
    except ValueError:
        default_state = False
    if default_state:
        current = lexical_state
        if _workbench_path_is_reparse(current):
            raise WorkbenchError(
                "default .peerbridge state must not be a link or reparse point"
            )
        for part in lexical_database.relative_to(lexical_state).parts:
            current = current / part
            if _workbench_path_is_reparse(current):
                raise WorkbenchError(
                    "default PeerBridge database must not cross a link or reparse point"
                )
    database = lexical_database.resolve()
    if not database.is_file():
        raise WorkbenchError("PeerBridge database does not exist")
    if not SAFE_IDENTIFIER.fullmatch(scope):
        raise WorkbenchError("invalid scope")
    if not SAFE_IDENTIFIER.fullmatch(initial_room_id):
        raise WorkbenchError("invalid initial room ID")
    access_token = token or secrets.token_urlsafe(32)
    if len(access_token) < 32:
        raise WorkbenchError("workbench token is too short")
    config = WorkbenchConfig(
        project_root=root,
        db_path=database,
        scope=scope,
        token=access_token,
        initial_room_id=initial_room_id,
        instance_id=instance_id or secrets.token_hex(12),
    )
    return WorkbenchServer(
        ("127.0.0.1", int(port)),
        config,
        managed_agent_manager=managed_agent_manager,
    )


def workbench_url(server: WorkbenchServer) -> str:
    """Build the local URL without placing the capability token in HTTP traffic."""
    room = quote(server.config.initial_room_id, safe="")
    token = quote(server.config.token, safe="")
    return f"{server.origin}/?room_id={room}#access_token={token}"


def _load_native_webview() -> Any | None:
    if sys.platform != "win32":
        return None
    try:
        import webview  # type: ignore[import-not-found]
    except (ImportError, OSError):
        return None
    return webview


def _system_webview2_runtime_path(
    *, search_roots: tuple[Path, ...] | None = None
) -> Path | None:
    """Find the newest installed WebView2 runtime without invoking Windows WMI."""

    if search_roots is None:
        roots: list[Path] = []
        for key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            value = os.environ.get(key)
            if value:
                root = Path(value)
                if root not in roots:
                    roots.append(root)
        search_roots = tuple(roots)

    candidates: list[tuple[tuple[int, ...], Path]] = []
    for root in search_roots:
        application = root / "Microsoft" / "EdgeWebView" / "Application"
        try:
            children = tuple(application.iterdir())
        except OSError:
            continue
        for child in children:
            executable = child / "msedgewebview2.exe"
            if not child.is_dir() or not executable.is_file():
                continue
            try:
                version = tuple(int(part) for part in child.name.split("."))
            except ValueError:
                continue
            candidates.append((version, child.resolve()))
    return max(candidates, default=((), None), key=lambda item: item[0])[1]


def _configure_native_webview(webview_module: Any) -> Path | None:
    """Pin pywebview to the installed runtime and bypass its WMI architecture probe."""

    settings = getattr(webview_module, "settings", None)
    if settings is None:
        return None
    configured = settings.get("WEBVIEW2_RUNTIME_PATH")
    if configured:
        runtime = Path(str(configured))
        if (runtime / "msedgewebview2.exe").is_file():
            return runtime.resolve()
    runtime = _system_webview2_runtime_path()
    if runtime is None:
        raise WorkbenchError(
            "Microsoft Edge WebView2 Runtime is required for the native PeerBridge desktop"
        )
    settings["WEBVIEW2_RUNTIME_PATH"] = str(runtime)
    return runtime


def _workbench_icon() -> Path | None:
    candidate = Path(__file__).with_name("release_support") / "peerbridge-icon.ico"
    return candidate if candidate.is_file() else None


WORKBENCH_WINDOW_TITLE = "PeerBridge MCP Control Room // LIVE"


def _apply_windows_webview_icon(title: str, ico_path: Path) -> tuple[int, ...]:
    """Apply owned icon handles to this process's visible WebView top-level window."""
    if sys.platform != "win32" or not ico_path.is_file():
        return ()
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        )
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetWindowTextLengthW.argtypes = (ctypes.c_void_p,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        )
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.LoadImageW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        )
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_void_p,
        )
        user32.SendMessageW.restype = ctypes.c_ssize_t

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows.argtypes = (callback_type, ctypes.c_void_p)
        user32.EnumWindows.restype = ctypes.c_bool
        windows: list[int] = []

        @callback_type
        def collect(hwnd: ctypes.c_void_p, _state: ctypes.c_void_p) -> bool:
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if int(process_id.value) != os.getpid():
                return True
            length = int(user32.GetWindowTextLengthW(hwnd) or 0)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, len(buffer))
            if buffer.value == title:
                windows.append(int(hwnd))
            return True

        user32.EnumWindows(collect, None)
        if not windows:
            return ()
        handles: list[int] = []
        for size, icon_kind in ((32, 1), (16, 0)):
            handle = int(
                user32.LoadImageW(
                    None,
                    str(ico_path),
                    1,  # IMAGE_ICON
                    size,
                    size,
                    0x0010,  # LR_LOADFROMFILE
                )
                or 0
            )
            if not handle:
                continue
            for hwnd in windows:
                user32.SendMessageW(
                    ctypes.c_void_p(hwnd),
                    0x0080,  # WM_SETICON
                    icon_kind,
                    ctypes.c_void_p(handle),
                )
            handles.append(handle)
        return tuple(handles)
    except (AttributeError, OSError, TypeError, ValueError):
        return ()


def run_native_workbench(
    server: WorkbenchServer,
    *,
    webview_module: Any | None = None,
) -> int:
    """Run the loopback server behind a real Windows WebView2 desktop window."""
    webview = webview_module or _load_native_webview()
    if webview is None:
        raise WorkbenchError("native WebView2 shell is unavailable")
    try:
        _configure_native_webview(webview)
    except Exception:
        server.server_close()
        raise
    configure_windows_app_identity()

    stopped = threading.Event()
    server_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name="peerbridge-workbench-http",
        daemon=True,
    )

    def stop_server(*_args: object) -> None:
        if stopped.is_set():
            return
        stopped.set()
        server.shutdown()

    initial_url = workbench_url(server)
    server_thread.start()
    try:
        # Do not create a native window until every required loopback asset is
        # actually readable. A process/listener alone is not a UI health check.
        for asset in ("/", "/assets/app.js", "/assets/app.css"):
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            try:
                connection.request("GET", asset)
                response = connection.getresponse()
                body = response.read()
            finally:
                connection.close()
            if response.status != HTTPStatus.OK or not body:
                raise WorkbenchError(
                    f"native workbench asset failed health check: {asset}"
                )

        window = webview.create_window(
            WORKBENCH_WINDOW_TITLE,
            initial_url,
            width=1440,
            height=900,
            min_size=(980, 650),
            resizable=True,
            hidden=False,
            background_color="#f6f7f9",
            text_select=True,
        )
        window.events.closed += stop_server
    except Exception:
        stop_server()
        server.server_close()
        server_thread.join(timeout=5)
        raise
    original_platform_system = platform.system
    if sys.platform == "win32":
        # Python 3.13 resolves platform.system() through WMI on Windows. Some
        # healthy machines have a stalled WMI provider, which would otherwise
        # freeze pywebview before it creates the first native window.
        platform.system = lambda: "Windows"
    icon_handles: tuple[int, ...] = ()
    try:
        start_options: dict[str, Any] = {
            "gui": "edgechromium",
            "debug": False,
            "private_mode": True,
        }
        icon = _workbench_icon()
        if icon is not None:
            start_options["icon"] = str(icon)

        def load_when_webview_ready() -> None:
            nonlocal icon_handles
            show_window = getattr(window, "show", None)
            if callable(show_window):
                show_window()
            if icon is not None:
                icon_handles = _apply_windows_webview_icon(
                    WORKBENCH_WINDOW_TITLE,
                    icon,
                )
            mount_probe = """
                (() => {
                  const app = document.getElementById('app');
                  const gate = document.getElementById('access-gate');
                  const visible = (app && !app.hidden) || (gate && !gate.hidden);
                  return Boolean(visible && document.body && document.body.innerText.trim().length > 20);
                })()
            """
            mounted = False
            for _attempt in range(8):
                window.load_url(initial_url)
                time.sleep(0.5)
                try:
                    mounted = bool(window.evaluate_js(mount_probe))
                except Exception:
                    mounted = False
                if mounted:
                    break
            if not mounted:
                diagnostic = """
                    <!doctype html><html><head><meta charset="utf-8">
                    <style>body{font:16px system-ui;margin:0;background:#f6f7f9;color:#172033}
                    main{max-width:620px;margin:14vh auto;padding:28px;border:1px solid #ccd3df;background:white}
                    h1{font-size:22px}p{line-height:1.6}</style></head><body><main>
                    <h1>PeerBridge could not mount the local workspace</h1>
                    <p>The local server is running, but WebView2 did not render the application after eight verified retries.</p>
                    <p>Close this window and report <strong>WEBVIEW_MOUNT_TIMEOUT</strong>. No room data was changed.</p>
                    </main></body></html>
                """
                load_html = getattr(window, "load_html", None)
                if callable(load_html):
                    load_html(diagnostic)
                else:
                    raise WorkbenchError("WEBVIEW_MOUNT_TIMEOUT")

        webview.start(load_when_webview_ready, **start_options)
    finally:
        release_windows_icon_handles(icon_handles)
        platform.system = original_platform_system
        stop_server()
        server.server_close()
        server_thread.join(timeout=5)
    if server_thread.is_alive():
        raise WorkbenchError("native workbench server did not stop cleanly")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local PeerBridge Modern Workbench.")
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--scope", default="default")
    parser.add_argument("--room", default=DEFAULT_ROOM_ID)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    db = args.db or root / ".peerbridge" / "peerbridge.sqlite3"
    server = make_server(root, db, args.scope, port=args.port, initial_room_id=args.room)
    if not args.no_browser:
        native_webview = _load_native_webview()
        if native_webview is not None:
            return run_native_workbench(server, webview_module=native_webview)
        server.server_close()
        raise WorkbenchError(
            "native WebView2 shell is required; external browser token launch is disabled"
        )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
