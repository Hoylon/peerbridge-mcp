"""Versioned capabilities, human permission gates, and isolated Git execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from .bridge import (
    CONTROL_ROOM_WORKFLOW_ID,
    HUMAN_OPERATOR_ID,
    Bridge,
    stable_sha256,
    utc_now,
)
from .secret_scan import contains_secret, redact_secrets


CAPABILITY_KINDS = frozenset({"skill", "mcp-tool"})
CAPABILITY_SENSITIVITIES = frozenset({"read", "write", "sensitive"})
PRINCIPAL_TYPES = frozenset({"agent", "room"})
GRANT_DECISIONS = frozenset({"allow", "deny"})
PERMISSION_DECISIONS = frozenset({"allow", "deny"})
BINDING_STATES = frozenset({"active", "sealed", "stale"})
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40,64}\Z")
MAX_PERMISSION_SECONDS = 86_400
MAX_GIT_INVENTORY_BYTES = 16 * 1024 * 1024
MAX_GIT_SOURCE_FILES = 100_000
MAX_GIT_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_GIT_SOURCE_SECONDS = 60.0
WINDOWS_LOCAL_WORKTREE_PATH_LIMIT = 180


class GovernanceError(RuntimeError):
    """A local governance operation is missing authority or exact source state."""


def _trusted_git_executable() -> Path:
    if os.name == "nt":
        roots = {
            str(os.environ.get("ProgramW6432") or "").strip(),
            str(os.environ.get("ProgramFiles") or r"C:\Program Files").strip(),
            str(os.environ.get("ProgramFiles(x86)") or "").strip(),
        }
        candidates = [
            Path(root) / "Git" / leaf / "git.exe"
            for root in roots
            if root
            for leaf in ("cmd", "bin")
        ]
    else:
        candidates = [Path("/usr/bin/git"), Path("/usr/local/bin/git")]
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            info = resolved.lstat()
        except OSError:
            continue
        attributes = int(getattr(info, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
        if (
            resolved.is_file()
            and not stat.S_ISLNK(info.st_mode)
            and not attributes & reparse_flag
        ):
            return resolved
    raise GovernanceError("trusted system Git executable is unavailable")


def _identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise GovernanceError(f"{label} is invalid")
    return text


def _text(value: Any, label: str, *, limit: int = 4_000) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise GovernanceError(f"{label} is invalid or too large")
    if contains_secret(text):
        raise GovernanceError(f"{label} contains credential-like data")
    return text


def _sha256(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise GovernanceError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _run_git(repository: Path, *arguments: str) -> str:
    executable = _trusted_git_executable()
    try:
        result = subprocess.run(
            (str(executable), "-C", str(repository), *arguments),
            capture_output=True,
            text=False,
            timeout=60,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GovernanceError("Git execution was unavailable or timed out") from exc
    if result.returncode:
        detail = redact_secrets(
            (result.stderr or result.stdout).decode("utf-8", errors="replace")
        ).strip()
        raise GovernanceError(f"Git operation failed: {detail[:300]}")
    return result.stdout.decode("utf-8", errors="strict").strip()


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(
        str(right.resolve())
    )


def _reject_reparse_ancestry(path: Path) -> None:
    """Reject a lexical path whose live entry or any ancestor is a reparse point."""

    current = path.absolute()
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            raise GovernanceError("bound worktree path is unavailable") from exc
        attributes = int(getattr(info, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise GovernanceError("bound worktree path crosses a filesystem link")
        if current.parent == current:
            return
        current = current.parent


def _git_common_directory(repository: Path) -> Path:
    value = Path(_run_git(repository, "rev-parse", "--git-common-dir"))
    if not value.is_absolute():
        value = repository / value
    return value.resolve()


def _verify_linked_worktree(repository: Path, worktree: Path) -> None:
    top_level = Path(_run_git(worktree, "rev-parse", "--show-toplevel"))
    if not _same_path(top_level, worktree):
        raise GovernanceError("isolated worktree top level does not match its binding")
    if not _same_path(
        _git_common_directory(repository), _git_common_directory(worktree)
    ):
        raise GovernanceError("isolated worktree is not linked to its bound repository")


def _rollback_created_worktree(repository: Path, worktree: Path) -> None:
    """Remove only a worktree proven to be linked to this repository."""

    _verify_linked_worktree(repository, worktree)
    _run_git(repository, "worktree", "remove", "--force", str(worktree))
    if worktree.exists():
        raise GovernanceError("new isolated worktree rollback was incomplete")


def _worktree_root(repository: Path, worktree_leaf: str) -> Path:
    local_root = (repository / ".peerbridge" / "wt").resolve()
    try:
        local_root.relative_to(repository)
    except ValueError as exc:
        raise GovernanceError(
            "project isolated worktree root escapes through a filesystem link"
        ) from exc
    local_target = local_root / worktree_leaf
    if os.name != "nt" or len(str(local_target)) <= WINDOWS_LOCAL_WORKTREE_PATH_LIMIT:
        return local_root

    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if not local_app_data:
        raise GovernanceError(
            "Windows needs LOCALAPPDATA for a path-safe isolated worktree"
        )
    local_app_root = Path(local_app_data)
    if not local_app_root.is_absolute():
        raise GovernanceError("Windows LOCALAPPDATA path is invalid")
    local_app_root = local_app_root.resolve()
    repository_key = hashlib.sha256(
        os.path.normcase(str(repository)).encode("utf-8")
    ).hexdigest()[:16]
    external_root = (
        local_app_root / "PeerBridge" / "worktrees" / repository_key
    ).resolve()
    try:
        external_root.relative_to(local_app_root)
    except ValueError as exc:
        raise GovernanceError(
            "Windows isolated worktree root escapes LOCALAPPDATA"
        ) from exc
    if len(str(external_root / worktree_leaf)) > WINDOWS_LOCAL_WORKTREE_PATH_LIMIT:
        raise GovernanceError("no path-safe Windows isolated worktree root is available")
    return external_root


def _prepare_worktree_root(root: Path) -> Path:
    try:
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
        info = resolved.lstat()
    except OSError as exc:
        raise GovernanceError("isolated worktree root is unavailable") from exc
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
        raise GovernanceError("isolated worktree root must not be a reparse point")
    if not stat.S_ISDIR(info.st_mode):
        raise GovernanceError("isolated worktree root is not a directory")
    return resolved


def _git_state_bytes(repository: Path, *arguments: str) -> bytes:
    executable = _trusted_git_executable()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            (str(executable), "-C", str(repository), *arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        assert process.stdout is not None
        output = process.stdout.read(MAX_GIT_INVENTORY_BYTES + 1)
        if len(output) > MAX_GIT_INVENTORY_BYTES:
            process.kill()
            process.wait(timeout=5)
            raise GovernanceError("Git state capture exceeded the output byte limit")
        return_code = process.wait(timeout=60)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            process.kill()
            process.wait(timeout=5)
        raise GovernanceError("Git state capture was unavailable or timed out") from exc
    except OSError as exc:
        raise GovernanceError("Git state capture was unavailable or timed out") from exc
    if return_code:
        raise GovernanceError("Git state capture failed")
    return output


def _safe_source_path(repository: Path, relative_bytes: bytes) -> Path:
    relative_text = os.fsdecode(relative_bytes)
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise GovernanceError("Git reported an unsafe source path")
    path = repository.joinpath(relative)
    try:
        path.parent.resolve().relative_to(repository.resolve())
    except (OSError, ValueError) as exc:
        raise GovernanceError("Git source parent is unavailable or unsafe") from exc
    return path


def _stable_source_binding(
    repository: Path,
    relative_bytes: bytes,
    *,
    missing_allowed: bool,
    max_bytes: int | None = None,
    deadline: float | None = None,
) -> bytes:
    path = _safe_source_path(repository, relative_bytes)
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing_allowed:
            return b"missing"
        raise GovernanceError("untracked Git source disappeared while it was hashed")
    except OSError as exc:
        raise GovernanceError("Git source is unavailable or unsafe") from exc
    mode = before.st_mode
    if stat.S_ISLNK(mode):
        try:
            target = os.fsencode(os.readlink(path))
            after = path.lstat()
        except OSError as exc:
            raise GovernanceError("untracked Git symlink could not be read") from exc
        kind = b"symlink"
        digest = hashlib.sha256(target).digest()
        size = len(target)
    elif stat.S_ISREG(mode):
        if max_bytes is not None and before.st_size > max_bytes:
            raise GovernanceError("Git source exceeds the cumulative byte budget")
        hasher = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as handle:
                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        raise GovernanceError("Git source hashing exceeded the time budget")
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    size += len(chunk)
            after = path.lstat()
        except OSError as exc:
            raise GovernanceError("untracked Git file could not be read") from exc
        kind = b"file"
        digest = hasher.digest()
    else:
        raise GovernanceError("Git source has an unsupported file type")
    before_identity = (before.st_mode, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_mode, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise GovernanceError("Git source changed while it was hashed")
    return b"\0".join(
        (
            kind,
            format(stat.S_IMODE(mode), "o").encode("ascii"),
            str(size).encode("ascii"),
            digest.hex().encode("ascii"),
        )
    )


def _parse_index_inventory(raw: bytes) -> list[tuple[bytes, bytes]]:
    entries: list[tuple[bytes, bytes]] = []
    for record in (value for value in raw.split(b"\0") if value):
        metadata, separator, relative = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3 or not relative:
            raise GovernanceError("Git index inventory is malformed")
        mode, object_id, stage = fields
        if (
            not re.fullmatch(rb"[0-7]{6}", mode)
            or not re.fullmatch(rb"[0-9a-f]{40,64}", object_id)
            or stage != b"0"
        ):
            raise GovernanceError("Git index contains an unsupported or unresolved entry")
        entries.append((metadata, relative))
    entries.sort(key=lambda value: value[1])
    return entries


def _hash_frame(hasher: Any, label: bytes, payload: bytes) -> None:
    hasher.update(len(label).to_bytes(4, "big"))
    hasher.update(label)
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)


def _capture_git_source(repository: Path) -> bytes:
    repository = repository.resolve()
    index_raw = _git_state_bytes(repository, "ls-files", "--stage", "-z")
    tracked = _parse_index_inventory(index_raw)
    untracked_raw = _git_state_bytes(
        repository, "ls-files", "--others", "--exclude-standard", "-z"
    )
    untracked = sorted(value for value in untracked_raw.split(b"\0") if value)
    if len(tracked) + len(untracked) > MAX_GIT_SOURCE_FILES:
        raise GovernanceError("Git source exceeds the file-count budget")
    deadline = time.monotonic() + MAX_GIT_SOURCE_SECONDS
    total_bytes = 0

    def source_binding(relative: bytes, *, missing_allowed: bool) -> bytes:
        nonlocal total_bytes
        path = _safe_source_path(repository, relative)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if missing_allowed:
                return b"missing"
            raise GovernanceError("untracked Git source disappeared while it was hashed")
        except OSError as exc:
            raise GovernanceError("Git source is unavailable or unsafe") from exc
        total_bytes += max(0, int(metadata.st_size))
        if total_bytes > MAX_GIT_SOURCE_BYTES:
            raise GovernanceError("Git source exceeds the cumulative byte budget")
        if time.monotonic() > deadline:
            raise GovernanceError("Git source hashing exceeded the time budget")
        return _stable_source_binding(
            repository,
            relative,
            missing_allowed=missing_allowed,
            max_bytes=MAX_GIT_SOURCE_BYTES - (total_bytes - int(metadata.st_size)),
            deadline=deadline,
        )

    hasher = hashlib.sha256()
    _hash_frame(hasher, b"schema", b"peerbridge-git-state/v2")
    for metadata, relative in tracked:
        _safe_source_path(repository, relative)
        _hash_frame(hasher, b"tracked-index", metadata)
        _hash_frame(hasher, b"tracked-path", relative)
        _hash_frame(
            hasher,
            b"tracked-working-tree",
            source_binding(relative, missing_allowed=True),
        )
    for relative in untracked:
        _safe_source_path(repository, relative)
        _hash_frame(hasher, b"untracked-path", relative)
        _hash_frame(
            hasher,
            b"untracked-binding",
            source_binding(relative, missing_allowed=False),
        )
    return hasher.digest()


def _git_diff_sha256(repository: Path) -> str:
    first = _capture_git_source(repository)
    second = _capture_git_source(repository)
    if first != second:
        raise GovernanceError("Git source changed while its complete state was captured")
    return first.hex()


def repository_resource_key(repository: Path) -> str:
    canonical = os.path.normcase(str(repository.resolve()))
    if os.name == "nt":
        canonical = canonical.casefold()
    return f"git:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def repository_source_state(repository: Path) -> dict[str, str]:
    """Capture one stable commit and complete tracked/untracked Git source state."""

    root = repository.resolve()
    reported_root = Path(_run_git(root, "rev-parse", "--show-toplevel")).resolve()
    if not _same_path(root, reported_root):
        raise GovernanceError("release source must be the exact Git repository root")
    commit_before = _run_git(root, "rev-parse", "HEAD").lower()
    diff_sha256 = _git_diff_sha256(root)
    commit_after = _run_git(root, "rev-parse", "HEAD").lower()
    if commit_before != commit_after:
        raise GovernanceError("Git commit changed while source state was captured")
    return {
        "resource_key": repository_resource_key(root),
        "commit_id": commit_before,
        "diff_sha256": diff_sha256,
    }


def _capability_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "scope",
            "capability_id",
            "registry_version",
            "kind",
            "display_name",
            "source_sha256",
            "sensitivity",
            "enabled",
            "registered_by",
            "created_utc",
        )
    }


def _grant_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "scope",
            "grant_id",
            "principal_type",
            "principal_id",
            "capability_id",
            "registry_version",
            "decision",
            "decided_by",
            "reason",
            "created_utc",
        )
    }


def _permission_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "scope",
            "decision_id",
            "task_id",
            "agent_id",
            "action",
            "resource_key",
            "decision",
            "decided_by",
            "reason",
            "expires_epoch",
            "created_utc",
        )
    }


def _binding_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "scope",
            "binding_id",
            "task_id",
            "agent_id",
            "permission_decision_id",
            "repository_root",
            "worktree_path",
            "base_commit_id",
            "base_diff_sha256",
            "state",
            "final_commit_id",
            "final_diff_sha256",
            "created_utc",
            "updated_utc",
        )
    }


class ExecutionGovernance:
    """Use the main SQLite authority for every capability and write permission."""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self.scope = bridge.scope

    def _require_human(self) -> None:
        if self.bridge.agent_id != "human-operator":
            raise GovernanceError("only human-operator may make governance decisions")

    def register_capability(
        self,
        *,
        capability_id: str,
        registry_version: str,
        kind: Literal["skill", "mcp-tool"],
        display_name: str,
        source_sha256: str,
        sensitivity: Literal["read", "write", "sensitive"],
        enabled: bool = True,
    ) -> dict[str, Any]:
        self._require_human()
        capability_id = _identifier(capability_id, "capability id")
        registry_version = _identifier(registry_version, "registry version")
        if kind not in CAPABILITY_KINDS:
            raise GovernanceError("capability kind is invalid")
        if sensitivity not in CAPABILITY_SENSITIVITIES:
            raise GovernanceError("capability sensitivity is invalid")
        if not isinstance(enabled, bool):
            raise GovernanceError("capability enabled state must be explicit")
        display_name = _text(display_name, "capability display name", limit=200)
        source_sha256 = _sha256(source_sha256, "capability source")
        created = utc_now()
        payload = {
            "scope": self.scope,
            "capability_id": capability_id,
            "registry_version": registry_version,
            "kind": kind,
            "display_name": display_name,
            "source_sha256": source_sha256,
            "sensitivity": sensitivity,
            "enabled": int(enabled),
            "registered_by": self.bridge.agent_id,
            "created_utc": created,
        }
        digest = stable_sha256(payload)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO capability_registry(
                        scope, capability_id, registry_version, kind, display_name,
                        source_sha256, sensitivity, enabled, registered_by,
                        created_utc, capability_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.scope,
                        capability_id,
                        registry_version,
                        kind,
                        display_name,
                        source_sha256,
                        sensitivity,
                        int(enabled),
                        self.bridge.agent_id,
                        created,
                        digest,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GovernanceError("capability version already exists") from exc
            event = self.bridge._event(
                connection,
                "governance.capability.registered",
                {
                    "capability_id": capability_id,
                    "registry_version": registry_version,
                    "capability_sha256": digest,
                },
            )
        return {
            **payload,
            "enabled": enabled,
            "capability_sha256": digest,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def grant_capability(
        self,
        *,
        principal_type: Literal["agent", "room"],
        principal_id: str,
        capability_id: str,
        registry_version: str,
        decision: Literal["allow", "deny"],
        reason: str,
    ) -> dict[str, Any]:
        self._require_human()
        if principal_type not in PRINCIPAL_TYPES:
            raise GovernanceError("capability principal type is invalid")
        if decision not in GRANT_DECISIONS:
            raise GovernanceError("capability grant decision is invalid")
        principal_id = _identifier(principal_id, "principal id")
        capability_id = _identifier(capability_id, "capability id")
        registry_version = _identifier(registry_version, "registry version")
        reason = _text(reason, "grant reason", limit=2_000)
        grant_id = uuid.uuid4().hex
        created = utc_now()
        payload = {
            "scope": self.scope,
            "grant_id": grant_id,
            "principal_type": principal_type,
            "principal_id": principal_id,
            "capability_id": capability_id,
            "registry_version": registry_version,
            "decision": decision,
            "decided_by": self.bridge.agent_id,
            "reason": reason,
            "created_utc": created,
        }
        digest = stable_sha256(payload)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            capability = connection.execute(
                """SELECT * FROM capability_registry
                    WHERE scope=? AND capability_id=? AND registry_version=?""",
                (self.scope, capability_id, registry_version),
            ).fetchone()
            if capability is None:
                raise GovernanceError("capability version does not exist")
            if stable_sha256(_capability_payload(capability)) != capability["capability_sha256"]:
                raise GovernanceError("capability registry SHA-256 does not match")
            connection.execute(
                """INSERT INTO capability_grants(
                    scope, grant_id, principal_type, principal_id, capability_id,
                    registry_version, decision, decided_by, reason, created_utc,
                    revoked_utc, grant_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    self.scope,
                    grant_id,
                    principal_type,
                    principal_id,
                    capability_id,
                    registry_version,
                    decision,
                    self.bridge.agent_id,
                    reason,
                    created,
                    digest,
                ),
            )
            event = self.bridge._event(
                connection,
                "governance.capability.granted",
                {
                    "grant_id": grant_id,
                    "principal_type": principal_type,
                    "principal_id": principal_id,
                    "decision": decision,
                    "grant_sha256": digest,
                },
            )
        return {
            **payload,
            "grant_sha256": digest,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def effective_capabilities(
        self, principal_type: Literal["agent", "room"], principal_id: str
    ) -> list[dict[str, Any]]:
        if principal_type not in PRINCIPAL_TYPES:
            raise GovernanceError("capability principal type is invalid")
        principal_id = _identifier(principal_id, "principal id")
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT g.*, c.kind, c.display_name, c.source_sha256,
                          c.sensitivity, c.enabled, c.capability_sha256
                     FROM capability_grants g
                     JOIN capability_registry c
                       ON c.scope=g.scope AND c.capability_id=g.capability_id
                      AND c.registry_version=g.registry_version
                    WHERE g.scope=? AND g.principal_type=? AND g.principal_id=?
                    ORDER BY g.rowid DESC""",
                (self.scope, principal_type, principal_id),
            ).fetchall()
            latest: dict[tuple[str, str], sqlite3.Row] = {}
            for row in rows:
                key = (str(row["capability_id"]), str(row["registry_version"]))
                latest.setdefault(key, row)
            result = []
            for row in latest.values():
                if stable_sha256(_grant_payload(row)) != row["grant_sha256"]:
                    raise GovernanceError("capability grant SHA-256 does not match")
                capability = {
                    key: row[key]
                    for key in (
                        "scope",
                        "capability_id",
                        "registry_version",
                        "kind",
                        "display_name",
                        "source_sha256",
                        "sensitivity",
                        "enabled",
                    )
                }
                capability.update(
                    {
                        "registered_by": connection.execute(
                            """SELECT registered_by FROM capability_registry
                                WHERE scope=? AND capability_id=? AND registry_version=?""",
                            (self.scope, row["capability_id"], row["registry_version"]),
                        ).fetchone()[0],
                        "created_utc": connection.execute(
                            """SELECT created_utc FROM capability_registry
                                WHERE scope=? AND capability_id=? AND registry_version=?""",
                            (self.scope, row["capability_id"], row["registry_version"]),
                        ).fetchone()[0],
                    }
                )
                if stable_sha256(_capability_payload(capability)) != row["capability_sha256"]:
                    raise GovernanceError("capability registry SHA-256 does not match")
                if row["decision"] == "allow" and row["enabled"]:
                    result.append(
                        {
                            "capability_id": row["capability_id"],
                            "registry_version": row["registry_version"],
                            "kind": row["kind"],
                            "display_name": row["display_name"],
                            "source_sha256": row["source_sha256"],
                            "sensitivity": row["sensitivity"],
                            "approval_required": row["sensitivity"] == "sensitive",
                            "grant_id": row["grant_id"],
                            "grant_sha256": row["grant_sha256"],
                        }
                    )
        return sorted(result, key=lambda item: (item["kind"], item["capability_id"]))

    def decide_permission(
        self,
        *,
        task_id: str,
        agent_id: str,
        action: str,
        resource_key: str,
        decision: Literal["allow", "deny"],
        reason: str,
        expires_epoch: float,
        now_epoch: float | None = None,
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_human()
        if decision not in PERMISSION_DECISIONS:
            raise GovernanceError("permission decision is invalid")
        task_id = _identifier(task_id, "task id")
        agent_id = _identifier(agent_id, "agent id")
        action = _identifier(action, "action")
        resource_key = _identifier(resource_key, "resource key")
        reason = _text(reason, "permission reason", limit=2_000)
        now = time.time() if now_epoch is None else float(now_epoch)
        expires = float(expires_epoch)
        if not now < expires <= now + MAX_PERMISSION_SECONDS:
            raise GovernanceError("permission expiry is outside the bounded range")
        decision_id = _identifier(decision_id or uuid.uuid4().hex, "decision id")
        created = utc_now()
        payload = {
            "scope": self.scope,
            "decision_id": decision_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "action": action,
            "resource_key": resource_key,
            "decision": decision,
            "decided_by": self.bridge.agent_id,
            "reason": reason,
            "expires_epoch": expires,
            "created_utc": created,
        }
        digest = stable_sha256(payload)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO permission_decisions(
                    scope, decision_id, task_id, agent_id, action, resource_key,
                    decision, decided_by, reason, expires_epoch, consumed_utc,
                    created_utc, decision_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (
                    self.scope,
                    decision_id,
                    task_id,
                    agent_id,
                    action,
                    resource_key,
                    decision,
                    self.bridge.agent_id,
                    reason,
                    expires,
                    created,
                    digest,
                ),
            )
            event = self.bridge._event(
                connection,
                "governance.permission.decided",
                {
                    "decision_id": decision_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "action": action,
                    "resource_key": resource_key,
                    "decision": decision,
                    "decision_sha256": digest,
                },
                task_id,
            )
        return {
            **payload,
            "consumed_utc": None,
            "decision_sha256": digest,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def authorize_permission(
        self,
        decision_id: str,
        *,
        task_id: str,
        agent_id: str,
        action: str,
        resource_key: str,
        now_epoch: float | None = None,
        consume: bool = True,
    ) -> dict[str, Any]:
        decision_id = _identifier(decision_id, "decision id")
        expected = {
            "task_id": _identifier(task_id, "task id"),
            "agent_id": _identifier(agent_id, "agent id"),
            "action": _identifier(action, "action"),
            "resource_key": _identifier(resource_key, "resource key"),
        }
        now = time.time() if now_epoch is None else float(now_epoch)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._validated_permission_row(
                connection,
                decision_id=decision_id,
                expected=expected,
                now_epoch=now,
            )
            consumed = utc_now() if consume else None
            if consume:
                changed_count = connection.execute(
                    """UPDATE permission_decisions SET consumed_utc=?
                        WHERE scope=? AND decision_id=? AND consumed_utc IS NULL""",
                    (consumed, self.scope, decision_id),
                ).rowcount
                if changed_count != 1:
                    raise GovernanceError(
                        "permission decision could not be consumed atomically"
                    )
                event = self.bridge._event(
                    connection,
                    "governance.permission.consumed",
                    {
                        "decision_id": decision_id,
                        "decision_sha256": row["decision_sha256"],
                        "task_id": row["task_id"],
                        "agent_id": row["agent_id"],
                        "action": row["action"],
                        "resource_key": row["resource_key"],
                    },
                    str(row["task_id"]),
                )
            else:
                event = None
        result = dict(row)
        result["consumed_utc"] = consumed
        if event is not None:
            result["audit_chain_sha256"] = event["chain_sha256"]
        return result

    def _validated_permission_row(
        self,
        connection: sqlite3.Connection,
        *,
        decision_id: str,
        expected: dict[str, str],
        now_epoch: float,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM permission_decisions WHERE scope=? AND decision_id=?",
            (self.scope, decision_id),
        ).fetchone()
        if row is None:
            raise GovernanceError("permission decision does not exist")
        if stable_sha256(_permission_payload(row)) != row["decision_sha256"]:
            raise GovernanceError("permission decision SHA-256 does not match")
        if any(str(row[key]) != value for key, value in expected.items()):
            raise GovernanceError("permission decision does not match the requested action")
        if row["decision"] != "allow":
            raise GovernanceError("permission decision denied the requested action")
        if float(row["expires_epoch"]) <= now_epoch:
            raise GovernanceError("permission decision expired")
        if row["consumed_utc"]:
            raise GovernanceError("permission decision was already consumed")
        return row

    def _binding_row(
        self, connection: sqlite3.Connection, binding_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM execution_bindings WHERE scope=? AND binding_id=?",
            (self.scope, binding_id),
        ).fetchone()
        if row is None:
            raise GovernanceError("execution binding does not exist")
        if stable_sha256(_binding_payload(row)) != row["binding_sha256"]:
            raise GovernanceError("execution binding SHA-256 does not match")
        return row

    def execution_binding_for_permission(
        self, permission_decision_id: str
    ) -> dict[str, Any]:
        permission_decision_id = _identifier(
            permission_decision_id, "permission decision id"
        )
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT binding_id FROM execution_bindings
                    WHERE scope=? AND permission_decision_id=?
                    ORDER BY created_utc, binding_id""",
                (self.scope, permission_decision_id),
            ).fetchall()
            if not rows:
                raise GovernanceError(
                    "permission decision has no isolated execution binding"
                )
            if len(rows) != 1:
                raise GovernanceError(
                    "permission decision has multiple execution bindings"
                )
            row = self._binding_row(connection, str(rows[0]["binding_id"]))
        return dict(row)

    def create_isolated_worktree(
        self,
        *,
        task_id: str,
        agent_id: str,
        permission_decision_id: str,
        repository: Path,
        base_commit: str = "HEAD",
        binding_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = _identifier(task_id, "task id")
        agent_id = _identifier(agent_id, "agent id")
        permission_decision_id = _identifier(
            permission_decision_id, "permission decision id"
        )
        if self.bridge.agent_id not in {
            HUMAN_OPERATOR_ID,
            CONTROL_ROOM_WORKFLOW_ID,
            agent_id,
        }:
            raise GovernanceError(
                "only the assigned agent, control-room workflow, or human-operator "
                "may create isolated execution"
            )
        repository = repository.resolve()
        if not repository.is_dir():
            raise GovernanceError("Git repository does not exist")
        top_level = Path(_run_git(repository, "rev-parse", "--show-toplevel")).resolve()
        if os.path.normcase(str(top_level)) != os.path.normcase(str(repository)):
            raise GovernanceError("repository path must be the exact Git top level")
        resource_key = repository_resource_key(repository)
        commit_id = _run_git(repository, "rev-parse", f"{base_commit}^{{commit}}").lower()
        if not GIT_OBJECT_ID.fullmatch(commit_id):
            raise GovernanceError("Git base commit identity is invalid")
        binding_id = _identifier(binding_id or uuid.uuid4().hex, "binding id")
        worktree_leaf = hashlib.sha256(
            f"{self.scope}\0{binding_id}".encode("utf-8")
        ).hexdigest()[:16]
        worktree_root = _prepare_worktree_root(
            _worktree_root(repository, worktree_leaf)
        )
        worktree = worktree_root / worktree_leaf
        if worktree.exists():
            raise GovernanceError("isolated worktree target already exists")
        expected_permission = {
            "task_id": task_id,
            "agent_id": agent_id,
            "action": "git.worktree.create",
            "resource_key": resource_key,
        }
        self.authorize_permission(
            permission_decision_id,
            task_id=task_id,
            agent_id=agent_id,
            action="git.worktree.create",
            resource_key=resource_key,
            consume=False,
        )
        worktree_created = False
        try:
            _run_git(
                repository, "worktree", "add", "--detach", str(worktree), commit_id
            )
            worktree_created = True
            _verify_linked_worktree(repository, worktree)
            observed_commit = _run_git(worktree, "rev-parse", "HEAD").lower()
            if observed_commit != commit_id:
                raise GovernanceError("isolated worktree did not bind the requested commit")
            base_diff = _git_diff_sha256(worktree)
            created = utc_now()
            payload = {
                "scope": self.scope,
                "binding_id": binding_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "permission_decision_id": permission_decision_id,
                "repository_root": str(repository),
                "worktree_path": str(worktree),
                "base_commit_id": commit_id,
                "base_diff_sha256": base_diff,
                "state": "active",
                "final_commit_id": None,
                "final_diff_sha256": None,
                "created_utc": created,
                "updated_utc": created,
            }
            digest = stable_sha256(payload)
            with self.bridge._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                permission_row = self._validated_permission_row(
                    connection,
                    decision_id=permission_decision_id,
                    expected=expected_permission,
                    now_epoch=time.time(),
                )
                consumed = utc_now()
                changed_count = connection.execute(
                    """UPDATE permission_decisions SET consumed_utc=?
                        WHERE scope=? AND decision_id=? AND consumed_utc IS NULL""",
                    (consumed, self.scope, permission_decision_id),
                ).rowcount
                if changed_count != 1:
                    raise GovernanceError(
                        "permission decision could not be consumed atomically"
                    )
                self.bridge._event(
                    connection,
                    "governance.permission.consumed",
                    {
                        "decision_id": permission_decision_id,
                        "decision_sha256": permission_row["decision_sha256"],
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "action": "git.worktree.create",
                        "resource_key": resource_key,
                    },
                    task_id,
                )
                connection.execute(
                    """INSERT INTO execution_bindings(
                        scope, binding_id, task_id, agent_id, permission_decision_id,
                        repository_root, worktree_path, base_commit_id,
                        base_diff_sha256, state, final_commit_id, final_diff_sha256,
                        created_utc, updated_utc, binding_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?, ?)""",
                    (
                        self.scope,
                        binding_id,
                        task_id,
                        agent_id,
                        permission_decision_id,
                        str(repository),
                        str(worktree),
                        commit_id,
                        base_diff,
                        created,
                        created,
                        digest,
                    ),
                )
                event = self.bridge._event(
                    connection,
                    "governance.execution.worktree_created",
                    {
                        "binding_id": binding_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "base_commit_id": commit_id,
                        "base_diff_sha256": base_diff,
                        "binding_sha256": digest,
                    },
                    task_id,
                )
        except Exception:
            if worktree_created:
                try:
                    _rollback_created_worktree(repository, worktree)
                except GovernanceError as cleanup_error:
                    raise GovernanceError(
                        "isolated execution failed and its new worktree could not be "
                        "rolled back safely"
                    ) from cleanup_error
            raise
        return {
            **payload,
            "binding_sha256": digest,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def inspect_permission(
        self, decision_id: str, *, now_epoch: float | None = None
    ) -> dict[str, Any]:
        """Verify and describe one permission without consuming it."""

        decision_id = _identifier(decision_id, "permission decision id")
        now = time.time() if now_epoch is None else float(now_epoch)
        with self.bridge._connect() as connection:
            row = connection.execute(
                "SELECT * FROM permission_decisions WHERE scope=? AND decision_id=?",
                (self.bridge.scope, decision_id),
            ).fetchone()
        if row is None:
            raise GovernanceError("permission decision does not exist")
        if stable_sha256(_permission_payload(row)) != row["decision_sha256"]:
            raise GovernanceError("permission decision SHA-256 does not match")
        result = dict(row)
        result["expired"] = float(row["expires_epoch"]) <= now
        result["consumed"] = bool(row["consumed_utc"])
        result["pending_sensitive_work"] = bool(
            row["decision"] == "allow"
            and not result["expired"]
            and not result["consumed"]
        )
        return result

    def seal_execution(self, binding_id: str) -> dict[str, Any]:
        binding_id = _identifier(binding_id, "binding id")
        with self.bridge._connect() as connection:
            row = self._binding_row(connection, binding_id)
        allowed_actors = {
            HUMAN_OPERATOR_ID,
            CONTROL_ROOM_WORKFLOW_ID,
            str(row["agent_id"]),
        }
        if self.bridge.agent_id not in allowed_actors:
            raise GovernanceError(
                "only the binding agent, control-room workflow, or human-operator "
                "may seal execution"
            )
        if row["state"] != "active":
            raise GovernanceError("execution binding is not active")
        repository = Path(str(row["repository_root"])).resolve()
        worktree_lexical = Path(str(row["worktree_path"])).absolute()
        _reject_reparse_ancestry(worktree_lexical)
        worktree = worktree_lexical.resolve(strict=True)
        if _same_path(repository, worktree):
            raise GovernanceError("bound worktree resolves to the operator repository")
        _verify_linked_worktree(repository, worktree)
        commit_id = _run_git(worktree, "rev-parse", "HEAD").lower()
        diff_sha = _git_diff_sha256(worktree)
        updated = utc_now()
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._binding_row(connection, binding_id)
            if self.bridge.agent_id not in {
                HUMAN_OPERATOR_ID,
                CONTROL_ROOM_WORKFLOW_ID,
                str(current["agent_id"]),
            }:
                raise GovernanceError(
                    "execution binding authority changed before it was sealed"
                )
            if current["state"] != "active":
                raise GovernanceError("execution binding is not active")
            changed_count = connection.execute(
                """UPDATE execution_bindings
                      SET state='sealed', final_commit_id=?, final_diff_sha256=?,
                          updated_utc=? WHERE scope=? AND binding_id=? AND state='active'""",
                (commit_id, diff_sha, updated, self.scope, binding_id),
            ).rowcount
            if changed_count != 1:
                raise GovernanceError("execution binding could not be sealed atomically")
            changed = connection.execute(
                "SELECT * FROM execution_bindings WHERE scope=? AND binding_id=?",
                (self.scope, binding_id),
            ).fetchone()
            assert changed is not None
            digest = stable_sha256(_binding_payload(changed))
            connection.execute(
                "UPDATE execution_bindings SET binding_sha256=? WHERE scope=? AND binding_id=?",
                (digest, self.scope, binding_id),
            )
            sealed = self._binding_row(connection, binding_id)
            event = self.bridge._event(
                connection,
                "governance.execution.sealed",
                {
                    "binding_id": binding_id,
                    "task_id": current["task_id"],
                    "final_commit_id": commit_id,
                    "final_diff_sha256": diff_sha,
                    "binding_sha256": digest,
                },
                str(current["task_id"]),
            )
        return {
            **dict(sealed),
            "audit_chain_sha256": event["chain_sha256"],
        }

    def verify_execution_source(self, binding_id: str) -> dict[str, Any]:
        binding_id = _identifier(binding_id, "binding id")
        with self.bridge._connect() as connection:
            row = self._binding_row(connection, binding_id)
        repository = Path(str(row["repository_root"])).resolve()
        worktree_lexical = Path(str(row["worktree_path"])).absolute()
        _reject_reparse_ancestry(worktree_lexical)
        worktree = worktree_lexical.resolve(strict=True)
        if _same_path(repository, worktree):
            raise GovernanceError("bound worktree resolves to the operator repository")
        _verify_linked_worktree(repository, worktree)
        live_commit = _run_git(worktree, "rev-parse", "HEAD").lower()
        live_diff = _git_diff_sha256(worktree)
        expected_commit = str(row["final_commit_id"] or row["base_commit_id"])
        expected_diff = str(row["final_diff_sha256"] or row["base_diff_sha256"])
        stale = live_commit != expected_commit or live_diff != expected_diff
        return {
            "binding_id": binding_id,
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "state": row["state"],
            "stale": stale,
            "expected_commit_id": expected_commit,
            "live_commit_id": live_commit,
            "expected_diff_sha256": expected_diff,
            "live_diff_sha256": live_diff,
            "binding_sha256": row["binding_sha256"],
        }

    def resolve_launch_binding(self, binding_id: str, agent_id: str) -> dict[str, Any]:
        """Resolve one active, SHA-valid worktree for a governed Agent launch."""

        binding_id = _identifier(binding_id, "binding id")
        agent_id = _identifier(agent_id, "agent id")
        with self.bridge._connect() as connection:
            row = self._binding_row(connection, binding_id)
        if str(row["agent_id"]) != agent_id:
            raise GovernanceError("execution binding belongs to another Agent")
        if str(row["state"]) != "active":
            raise GovernanceError("execution binding is not active")
        repository = Path(str(row["repository_root"])).resolve()
        worktree = Path(str(row["worktree_path"])).resolve()
        _verify_linked_worktree(repository, worktree)
        return {
            "binding_id": binding_id,
            "task_id": str(row["task_id"]),
            "agent_id": agent_id,
            "state": "active",
            "repository_root": repository,
            "worktree_path": worktree,
            "binding_sha256": str(row["binding_sha256"]),
        }


__all__ = [
    "CAPABILITY_KINDS",
    "CAPABILITY_SENSITIVITIES",
    "ExecutionGovernance",
    "GovernanceError",
    "repository_resource_key",
    "repository_source_state",
]
