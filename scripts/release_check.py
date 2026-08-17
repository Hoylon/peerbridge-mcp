from __future__ import annotations

import argparse
import ast
import copy
import configparser
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from peerbridge_mcp.secret_scan import (  # noqa: E402
    SAFE_TEST_CREDENTIAL_LITERALS,
    contains_secret,
    contains_secret_bytes,
    source_text_contains_secret,
)

REQUIRED = {
    "BRAND_ASSETS.md",
    "MANIFEST.in",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    "THIRD_PARTY_NOTICES.md",
    "TRADEMARKS.md",
    "pyproject.toml",
}
IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".peerbridge",
    ".peerbridge-artifacts",
    ".peerbridge-test-tmp",
    ".pytest-tmp",
    ".pytest_runs",
    ".pytest_cache",
    ".test-tmp",
    ".test-runtime",
    ".test-runs",
    ".ruff_cache",
    ".tools",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "drafts",
    "htmlcov",
    "logs",
    "venv",
}
PRIVATE_PATTERNS = (
    re.compile(r"(?i)source_discovery_[A-Za-z0-9_-]+"),
    re.compile(r"(?i)[A-Z]:[\\/]Users[\\/][^\\/\r\n]+"),
    re.compile(r"(?i)/(?:Users|home)/[^/\r\n]+"),
)
TEXT_FILENAMES = {
    "Dockerfile",
    "LICENSE",
    "NOTICE",
}
TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".cjs",
    ".cmd",
    ".css",
    ".csv",
    ".example",
    ".gs",
    ".html",
    ".htm",
    ".in",
    ".ini",
    ".json",
    ".jsonc",
    ".js",
    ".jsx",
    ".lock",
    ".md",
    ".mjs",
    ".pem",
    ".properties",
    ".pub",
    ".py",
    ".ps1",
    ".rst",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
JAVASCRIPT_SUFFIXES = {".cjs", ".gs", ".js", ".jsx", ".mjs", ".ts", ".tsx"}
JAVASCRIPT_FRAGMENT_PATTERN = re.compile(
    r"(?P<line>//[^\r\n]*)|"
    r"(?P<block>/\*.*?\*/)|"
    r"(?P<regex>/(?!(?:/|\*))(?:(?:\\.)|\[(?:\\.|[^\]\\\r\n])*\]|"
    r"[^/\\\[\r\n])+/[dgimsuvy]*)|"
    r"(?P<string>'(?:\\.|[^'\\\r\n])*'|\"(?:\\.|[^\"\\\r\n])*\")|"
    r"(?P<template>`(?:\\.|[^`\\])*`)",
    re.DOTALL,
)
JAVASCRIPT_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?:[A-Za-z_$][\w$.-]*(?:api[_-]?key|access[_-]?token|authorization|"
    r"password|secret|token)|['\"](?:api[_-]?key|access[_-]?token|authorization|"
    r"password|secret|token)['\"])\s*[:=]\s*$"
)
REMOTE_RECEIPT_SCHEMA = "peerbridge.remote-mobile-e2e-receipt.v2"
REMOTE_RECEIPT_DEFAULT = Path(".peerbridge/receipts/remote-mobile-e2e-v2.json")
REMOTE_ARTIFACT_NAMES = {
    "serve_state",
    "serve_status",
    "funnel_status",
    "network_observation",
    "browser_trace",
    "audit_verification",
}
REMOTE_RECEIPT_FIELDS = {
    "schema",
    "created_at_utc",
    "scope",
    "public_origin",
    "local_backend",
    "tailnet_only",
    "funnel_enabled",
    "test_mode",
    "evidence_origin",
    "source_tree_sha256",
    "artifacts",
    "receipt_sha256",
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
TEST_EVIDENCE_PATTERN = re.compile(r"(?i)(?:^|[^a-z])(fake|fixture|mock|simulat(?:ed|ion)?)(?:[^a-z]|$)")
MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 128 * 1024 * 1024
WHEEL_DIST_INFO_FILES = {
    "METADATA",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
    "RECORD",
    "licenses/LICENSE",
    "licenses/BRAND_ASSETS.md",
    "licenses/THIRD_PARTY_NOTICES.md",
    "licenses/TRADEMARKS.md",
}
REQUIRED_WHEEL_LEGAL_FILES = {
    "licenses/LICENSE",
    "licenses/BRAND_ASSETS.md",
    "licenses/THIRD_PARTY_NOTICES.md",
    "licenses/TRADEMARKS.md",
}
SDIST_ROOT_FILES = {
    "BRAND_ASSETS.md",
    "LICENSE",
    "MANIFEST.in",
    "PKG-INFO",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    "setup.cfg",
    "TRADEMARKS.md",
}
REQUIRED_SDIST_ROOT_FILES = {
    "BRAND_ASSETS.md",
    "LICENSE",
    "MANIFEST.in",
    "PKG-INFO",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "TRADEMARKS.md",
    "pyproject.toml",
}
SDIST_EGG_INFO_FILES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "requires.txt",
    "top_level.txt",
}


class ReleaseCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    version: str
    package_version: str
    source_base: Path
    source_modules: tuple[str, ...]
    package_data: tuple[str, ...]
    scripts: Mapping[str, str]


@dataclass(frozen=True)
class DevCheckResult:
    info: ProjectInfo | None
    files_checked: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class RemoteEvidenceResult:
    receipt: Path
    receipt_sha256: str
    source_tree_sha256: str
    errors: tuple[str, ...]


def _git_file_inventory(
    root: Path, *, include_untracked: bool = True
) -> list[Path] | None:
    """Return index paths and, for development checks, non-ignored untracked paths."""
    root = root.resolve()
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if top.returncode != 0:
        return None
    try:
        git_root = Path(top.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        return None
    if git_root != root:
        return None
    files: set[Path] = set()
    inventories = [(["ls-files", "--cached", "-z"], False)]
    if include_untracked:
        inventories.append(
            (["ls-files", "--others", "--exclude-standard", "-z"], True)
        )
    for arguments, filter_runtime_scratch in inventories:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        for raw_name in completed.stdout.split(b"\0"):
            if not raw_name:
                continue
            relative = Path(os.fsdecode(raw_name))
            if filter_runtime_scratch and any(
                _ignored_part(part) for part in relative.parts
            ):
                continue
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file() and not candidate.is_symlink():
                files.add(candidate)
    return sorted(files)


def expected_release_tag(version: str) -> str:
    match = re.fullmatch(
        r"(?P<release>\d+(?:\.\d+)*)(?:(?P<phase>a|b|rc)(?P<number>\d+))?",
        version,
    )
    if match is None:
        return f"v{version}"
    phase = match.group("phase")
    if phase is None:
        return f"v{match.group('release')}"
    label = {"a": "alpha", "b": "beta", "rc": "rc"}[phase]
    return f"v{match.group('release')}-{label}.{match.group('number')}"


def _configured_project_version(root: Path) -> str | None:
    try:
        document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    project = document.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    return version if isinstance(version, str) and version else None


def git_release_preflight_errors(root: Path) -> tuple[str, ...]:
    """Require a clean commit at the annotated tag matching project.version."""
    root = root.resolve()
    if _git_file_inventory(root, include_untracked=False) is None:
        return ("strict release requires the exact Git repository root",)
    version = _configured_project_version(root)
    expected_tag = expected_release_tag(version) if version else None
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
            check=False,
        )
        tags = subprocess.run(
            ["git", "-C", str(root), "tag", "--points-at", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ("strict release could not verify Git state",)
    errors: list[str] = []
    if head.returncode != 0:
        errors.append("strict release requires a committed Git HEAD")
    if status.returncode != 0:
        errors.append("strict release could not read Git worktree status")
    elif status.stdout.strip():
        changed = len(status.stdout.splitlines())
        errors.append(f"strict release requires a clean Git worktree ({changed} changed paths)")
    if expected_tag is None:
        errors.append("strict release cannot derive a tag from project.version")
    elif tags.returncode != 0:
        errors.append("strict release could not read tags at Git HEAD")
    elif expected_tag not in tags.stdout.splitlines():
        errors.append(f"strict release requires annotated tag {expected_tag} at Git HEAD")
    else:
        try:
            tag_type = subprocess.run(
                ["git", "-C", str(root), "cat-file", "-t", f"refs/tags/{expected_tag}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            errors.append(f"strict release could not inspect tag {expected_tag}")
        else:
            if tag_type.returncode != 0 or tag_type.stdout.strip() != "tag":
                errors.append(f"strict release tag {expected_tag} must be annotated")
    return tuple(errors)


def iter_text_files(root: Path = ROOT) -> list[Path]:
    files = []
    inventory = _git_file_inventory(root)
    candidates = inventory if inventory is not None else root.rglob("*")
    for path in candidates:
        if not path.is_file() or (
            path.name not in TEXT_FILENAMES and path.suffix.lower() not in TEXT_SUFFIXES
        ):
            continue
        parts = path.relative_to(root).parts
        if inventory is None and any(_ignored_part(part) for part in parts):
            continue
        files.append(path)
    return sorted(files)


def iter_release_source_files(
    root: Path = ROOT, *, tracked_only: bool = False
) -> list[Path]:
    """Return files that define a release, including binary package data.

    Runtime and build directories are deliberately excluded, while every regular
    top-level file and every file in a release-owned source directory is bound.
    """
    inventory = _git_file_inventory(root, include_untracked=not tracked_only)
    if inventory is not None:
        return inventory
    release_directories = {
        ".github",
        "docs",
        "examples",
        "scripts",
        "src",
        "support",
        "tests",
    }
    files: set[Path] = set()
    for path in root.iterdir():
        if path.is_file() and not path.is_symlink():
            files.add(path)
    for directory_name in release_directories:
        directory = root / directory_name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            parts = path.relative_to(root).parts
            if any(_ignored_part(part) for part in parts):
                continue
            files.add(path)
    return sorted(files)


def _ignored_part(part: str) -> bool:
    return (
        part in IGNORED_PARTS
        or part.startswith(".test-tmp-")
        or part.startswith(".pytest-")
        or part.startswith(".pytest_")
        or part.endswith(".egg-info")
        or part.endswith(".pyc")
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: object) -> str:
    return _sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def release_source_tree_sha256(root: Path = ROOT, *, tracked_only: bool = False) -> str:
    digest = hashlib.sha256()
    for path in iter_release_source_files(root, tracked_only=tracked_only):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith(("Z", "+00:00")):
        raise ReleaseCheckError(f"{label} must be an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseCheckError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ReleaseCheckError(f"{label} must use UTC")
    return parsed


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseCheckError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseCheckError(f"{label} must contain a JSON object")
    return value


def _contains_forbidden_evidence_marker(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in {"fake", "fixture", "mock", "simulated", "simulation"}:
                if child not in (False, None, "", 0, [], {}):
                    return True
            if _contains_forbidden_evidence_marker(child):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden_evidence_marker(child) for child in value)
    return isinstance(value, str) and TEST_EVIDENCE_PATTERN.search(value) is not None


def _contains_credential_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower().replace("-", "_")
            if any(
                token in lowered
                for token in ("api_key", "password", "access_token", "refresh_token", "credential")
            ):
                return True
            if _contains_credential_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_credential_field(child) for child in value)
    return False


def _safe_evidence_path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseCheckError(f"{label} must use a repository-relative POSIX path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReleaseCheckError(f"{label} escapes the repository")
    if relative.parts[:2] != (".peerbridge", "evidence"):
        raise ReleaseCheckError(f"{label} must be stored below .peerbridge/evidence")
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise ReleaseCheckError(f"{label} does not identify a regular evidence file")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ReleaseCheckError(f"{label} escapes the repository") from exc
    return path


def _artifact_payloads(
    root: Path, receipt: Mapping[str, object]
) -> tuple[dict[str, bytes], list[str]]:
    errors: list[str] = []
    payloads: dict[str, bytes] = {}
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != REMOTE_ARTIFACT_NAMES:
        return payloads, [
            "remote receipt must bind exactly these artifacts: "
            + ", ".join(sorted(REMOTE_ARTIFACT_NAMES))
        ]
    for name in sorted(REMOTE_ARTIFACT_NAMES):
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, dict):
            errors.append(f"remote artifact {name} descriptor must be an object")
            continue
        if set(descriptor) != {"path", "bytes", "sha256", "capture_command", "exit_code"}:
            errors.append(f"remote artifact {name} descriptor has an invalid field set")
            continue
        command = descriptor.get("capture_command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            errors.append(f"remote artifact {name} has no exact capture command")
        if descriptor.get("exit_code") != 0:
            errors.append(f"remote artifact {name} capture did not exit successfully")
        if not _is_sha256(descriptor.get("sha256")):
            errors.append(f"remote artifact {name} has an invalid SHA-256")
            continue
        try:
            path = _safe_evidence_path(root, descriptor.get("path"), f"artifact {name}")
            payload = path.read_bytes()
        except (OSError, ReleaseCheckError) as exc:
            errors.append(str(exc))
            continue
        if len(payload) <= 1 or len(payload) > 1_048_576:
            errors.append(f"remote artifact {name} has an invalid byte length")
            continue
        try:
            artifact_text = payload.decode("utf-8")
        except UnicodeError:
            errors.append(f"remote artifact {name} is not UTF-8")
            continue
        if contains_secret(artifact_text):
            errors.append(f"remote artifact {name} contains a credential-like value")
        if descriptor.get("bytes") != len(payload):
            errors.append(f"remote artifact {name} byte count differs from live evidence")
        if descriptor.get("sha256") != _sha256(payload):
            errors.append(f"remote artifact {name} SHA-256 differs from live evidence")
        payloads[name] = payload
    return payloads, errors


def _has_exact_proxy(value: object, expected: str) -> bool:
    if isinstance(value, dict):
        return any(
            (str(key).lower() == "proxy" and child == expected)
            or _has_exact_proxy(child, expected)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_has_exact_proxy(child, expected) for child in value)
    return False


def _has_public_funnel_route(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if "funnel" in lowered and child not in (False, None, "", 0, [], {}):
                return True
            if lowered in {"proxy", "url", "https", "tcp", "web"} and child not in (
                False,
                None,
                "",
                0,
                [],
                {},
            ):
                return True
            if _has_public_funnel_route(child):
                return True
        return False
    if isinstance(value, list):
        return any(_has_public_funnel_route(child) for child in value)
    return isinstance(value, str) and ("https://" in value.lower() or ".ts.net" in value.lower())


def _has_truthy_funnel_marker(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            ("funnel" in str(key).lower() and child not in (False, None, "", 0, [], {}))
            or _has_truthy_funnel_marker(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_has_truthy_funnel_marker(child) for child in value)
    return False


def _validate_remote_documents(
    receipt: Mapping[str, object], payloads: Mapping[str, bytes]
) -> list[str]:
    errors: list[str] = []
    documents: dict[str, dict[str, object]] = {}
    for name, payload in payloads.items():
        try:
            documents[name] = _json_object(payload, f"remote artifact {name}")
        except ReleaseCheckError as exc:
            errors.append(str(exc))
    if set(documents) != REMOTE_ARTIFACT_NAMES:
        return errors
    if any(_contains_forbidden_evidence_marker(value) for value in documents.values()):
        errors.append("remote evidence contains a fixture, mock, or simulated marker")
    if any(_contains_credential_field(value) for value in documents.values()):
        errors.append("remote evidence contains a credential-bearing field")

    scope = receipt.get("scope")
    public_origin = receipt.get("public_origin")
    local_backend = receipt.get("local_backend")
    parsed_origin = urlsplit(public_origin if isinstance(public_origin, str) else "")
    parsed_backend = urlsplit(local_backend if isinstance(local_backend, str) else "")
    if (
        parsed_origin.scheme != "https"
        or not parsed_origin.hostname
        or not parsed_origin.hostname.lower().endswith(".ts.net")
        or parsed_origin.port is not None
        or parsed_origin.path not in ("", "/")
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        errors.append("remote public origin must be a private Tailscale HTTPS name")
    if (
        parsed_backend.scheme != "http"
        or parsed_backend.hostname != "127.0.0.1"
        or parsed_backend.port is None
        or parsed_backend.path not in ("", "/")
    ):
        errors.append("remote backend must be an explicit 127.0.0.1 listener")

    serve_state = documents["serve_state"]
    required_state = {
        "scope": scope,
        "public_origin": public_origin,
        "local_backend": local_backend,
        "transport": "tailscale-serve",
        "tailnet_only": True,
        "funnel_enabled": False,
    }
    if any(serve_state.get(key) != value for key, value in required_state.items()):
        errors.append("serve state does not match the receipt's tailnet-only route")
    status_hash = serve_state.get("validated_serve_status_sha256")
    serve_status_payload = payloads["serve_status"]
    if status_hash not in {
        _sha256(serve_status_payload),
        _sha256(serve_status_payload.rstrip(b"\r\n")),
    }:
        errors.append("serve state does not bind the captured Serve status")
    if not _has_exact_proxy(documents["serve_status"], str(local_backend)):
        errors.append("Serve status does not proxy the exact loopback backend")
    if _has_truthy_funnel_marker(documents["serve_status"]):
        errors.append("Serve status reports a Funnel-enabled route")
    if _has_public_funnel_route(documents["funnel_status"]):
        errors.append("Funnel status contains a public route")

    network = documents["network_observation"]
    listener = network.get("backend_listener")
    if not isinstance(listener, dict) or listener.get("address") != "127.0.0.1":
        errors.append("network evidence does not bind a loopback backend listener")
    elif parsed_backend.port is not None and listener.get("port") != parsed_backend.port:
        errors.append("network evidence listener port differs from the backend")
    if network.get("non_loopback_listeners_on_backend_port") != []:
        errors.append("network evidence reports a non-loopback backend listener")
    if not isinstance(listener, dict) or not isinstance(listener.get("process_id"), int) or listener.get("process_id", 0) <= 0:
        errors.append("network evidence has no backend process identity")

    browser = documents["browser_trace"]
    if browser.get("schema") != "peerbridge.mobile-browser-reconnect-trace.v2":
        errors.append("browser trace schema is invalid")
    if (
        browser.get("test_mode") is not False
        or browser.get("evidence_origin") != "real-device"
        or browser.get("device_class") != "phone"
    ):
        errors.append("browser trace is not a real phone run")
    if browser.get("scope") != scope or browser.get("public_origin") != public_origin:
        errors.append("browser trace route differs from the receipt")
    identity_hash = browser.get("tailnet_identity_sha256")
    if not _is_sha256(identity_hash):
        errors.append("browser trace lacks a hashed tailnet identity")
    if (
        browser.get("browser_device_continuity_source")
        != "browser-local-storage-random-nonce"
    ):
        errors.append("browser trace lacks the browser device-continuity contract")
    if browser.get("network_layer_node_identity_attested") is not False:
        errors.append("browser trace overclaims network-layer node identity")
    viewport = browser.get("viewport")
    if (
        not isinstance(viewport, dict)
        or not isinstance(viewport.get("width"), int)
        or not 240 <= viewport.get("width", 0) <= 1024
        or not isinstance(viewport.get("height"), int)
        or viewport.get("height", 0) < 320
        or not isinstance(viewport.get("max_touch_points"), int)
        or viewport.get("max_touch_points", 0) < 1
    ):
        errors.append("browser trace lacks a credible mobile viewport/touch observation")
    sessions = browser.get("sessions")
    parsed_sessions: list[tuple[datetime, dict[str, object]]] = []
    if not isinstance(sessions, list) or len(sessions) != 2:
        errors.append("browser trace must contain exactly initial and reconnect sessions")
    else:
        expected_phases = ("initial", "reconnect")
        for index, (session, phase) in enumerate(zip(sessions, expected_phases, strict=True)):
            if not isinstance(session, dict) or session.get("phase") != phase:
                errors.append(f"browser session {index + 1} has the wrong phase")
                continue
            try:
                connected = _utc(session.get("connected_at_utc"), f"browser session {phase}")
            except ReleaseCheckError as exc:
                errors.append(str(exc))
                continue
            parsed_sessions.append((connected, session))
            if session.get("authenticated") is not True or session.get("transport") != "tailnet-https":
                errors.append(f"browser session {phase} was not authenticated through the tailnet")
            if session.get("identity_source") != "Tailscale-User-Login":
                errors.append(f"browser session {phase} does not bind Tailscale identity injection")
            if session.get("page_status") != 200 or session.get("snapshot_status") != 200:
                errors.append(f"browser session {phase} did not load the UI and snapshot")
            for field in (
                "browser_session_id_sha256",
                "browser_device_continuity_sha256",
                "tailnet_identity_sha256",
                "user_agent_sha256",
                "snapshot_signature",
            ):
                if not _is_sha256(session.get(field)):
                    errors.append(f"browser session {phase} lacks {field}")
            if session.get("tailnet_identity_sha256") != identity_hash:
                errors.append(f"browser session {phase} identity differs from the trace")
            if not isinstance(session.get("instance_id"), str) or not session.get("instance_id"):
                errors.append(f"browser session {phase} lacks the PeerBridge instance identity")
        if len(parsed_sessions) == 2:
            first_time, first = parsed_sessions[0]
            second_time, second = parsed_sessions[1]
            if first_time >= second_time:
                errors.append("phone reconnect did not occur after the initial session")
            disconnected: datetime | None = None
            try:
                disconnected = _utc(browser.get("disconnected_at_utc"), "phone disconnect")
                if not first_time < disconnected < second_time:
                    errors.append("phone disconnect is not between the two sessions")
            except ReleaseCheckError as exc:
                errors.append(str(exc))
            if first.get("browser_session_id_sha256") == second.get(
                "browser_session_id_sha256"
            ):
                errors.append("phone reconnect reused the initial browser session ID")
            if first.get("browser_device_continuity_sha256") != second.get(
                "browser_device_continuity_sha256"
            ):
                errors.append("phone reconnect did not preserve browser device continuity")
            if first.get("instance_id") != second.get("instance_id"):
                errors.append("phone reconnect did not return to the same PeerBridge instance")
            if first.get("snapshot_signature") == second.get("snapshot_signature"):
                errors.append("phone reconnect reused the initial snapshot")

            disconnect = browser.get("disconnect_evidence")
            if not isinstance(disconnect, dict):
                errors.append("browser trace lacks disconnect evidence")
            else:
                if (
                    disconnect.get("method")
                    != "operator-marked-disconnect-plus-fresh-browser-session"
                ):
                    errors.append("browser trace has an unsupported disconnect method")
                if not _is_sha256(disconnect.get("challenge_sha256")):
                    errors.append("browser trace lacks a hashed disconnect challenge")
                minimum_gap = disconnect.get("minimum_gap_seconds")
                observed_gap = disconnect.get("observed_gap_seconds")
                if (
                    not isinstance(minimum_gap, int)
                    or isinstance(minimum_gap, bool)
                    or not 0 <= minimum_gap <= 3600
                    or not isinstance(observed_gap, int)
                    or isinstance(observed_gap, bool)
                    or observed_gap < minimum_gap
                ):
                    errors.append("browser trace has invalid disconnect timing evidence")
                if disconnect.get("network_layer_disconnect_cryptographically_proven") is not False:
                    errors.append("browser trace overclaims network-layer disconnect proof")
                if isinstance(observed_gap, int) and disconnected is not None:
                    actual_gap = int((second_time - disconnected).total_seconds())
                    if actual_gap != observed_gap:
                        errors.append("browser disconnect gap differs from session chronology")

    message = browser.get("message")
    if (
        not isinstance(message, dict)
        or message.get("status") != 201
        or not isinstance(message.get("message_id"), str)
        or not message.get("message_id")
        or not _is_sha256(message.get("content_sha256"))
    ):
        errors.append("phone browser trace lacks a successful audited MCP message")
    elif isinstance(sessions, list) and len(sessions) == 2:
        reconnect = sessions[1]
        if not isinstance(reconnect, dict) or reconnect.get("observed_message_id") != message.get(
            "message_id"
        ):
            errors.append("reconnect snapshot does not contain the phone message")

    audit = documents["audit_verification"]
    if audit.get("schema") != "peerbridge.remote-audit-verification.v1":
        errors.append("remote audit verification schema is invalid")
    if audit.get("scope") != scope or audit.get("valid") is not True:
        errors.append("remote audit verification did not pass for the receipt scope")
    if not _is_sha256(audit.get("audit_head_sha256")) or not isinstance(audit.get("event_count"), int) or audit.get("event_count", 0) < 1:
        errors.append("remote audit verification lacks an audit head or event count")
    if isinstance(message, dict) and audit.get("message_id") != message.get("message_id"):
        errors.append("remote audit verification does not bind the phone message")
    try:
        verified = _utc(audit.get("verified_at_utc"), "remote audit verification")
        if parsed_sessions and verified < parsed_sessions[-1][0]:
            errors.append("remote audit was verified before the reconnect")
    except ReleaseCheckError as exc:
        errors.append(str(exc))
    return errors


def validate_remote_mobile_evidence(
    root: Path, receipt_path: Path | None = None
) -> RemoteEvidenceResult:
    selected = receipt_path or root / REMOTE_RECEIPT_DEFAULT
    if not selected.is_absolute():
        selected = root / selected
    errors: list[str] = []
    source_hash = release_source_tree_sha256(root)
    if selected.is_symlink() or not selected.is_file():
        return RemoteEvidenceResult(
            selected,
            "",
            source_hash,
            ("real phone reconnect/mobile-browser receipt is missing",),
        )
    try:
        receipt_payload = selected.read_bytes()
        receipt = _json_object(receipt_payload, "remote mobile receipt")
    except (OSError, ReleaseCheckError) as exc:
        return RemoteEvidenceResult(selected, "", source_hash, (str(exc),))
    receipt_file_hash = _sha256(receipt_payload)
    try:
        receipt_text = receipt_payload.decode("utf-8")
    except UnicodeError:
        receipt_text = ""
    if contains_secret(receipt_text):
        errors.append("remote mobile receipt contains a credential-like value")
    if set(receipt) != REMOTE_RECEIPT_FIELDS:
        errors.append("remote mobile receipt has an invalid field set")
    if receipt.get("schema") != REMOTE_RECEIPT_SCHEMA:
        errors.append("remote mobile receipt schema is invalid")
    if receipt.get("source_tree_sha256") != source_hash:
        errors.append("remote mobile receipt does not bind the current release source tree")
    claimed_receipt_hash = receipt.get("receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if not _is_sha256(claimed_receipt_hash) or claimed_receipt_hash != _canonical_sha256(unsigned):
        errors.append("remote mobile receipt self SHA-256 is invalid")
    if receipt.get("tailnet_only") is not True or receipt.get("funnel_enabled") is not False:
        errors.append("remote mobile receipt is not explicitly tailnet-only with Funnel disabled")
    if receipt.get("test_mode") is not False or receipt.get("evidence_origin") != "real-device":
        errors.append("remote mobile receipt is not marked as real-device evidence")
    if not isinstance(receipt.get("scope"), str) or not receipt.get("scope"):
        errors.append("remote mobile receipt has no scope")
    receipt_created: datetime | None = None
    try:
        receipt_created = _utc(receipt.get("created_at_utc"), "remote mobile receipt")
        if receipt_created > datetime.now(timezone.utc):
            errors.append("remote mobile receipt creation time is in the future")
    except ReleaseCheckError as exc:
        errors.append(str(exc))
    if _contains_forbidden_evidence_marker(receipt) or _contains_credential_field(receipt):
        errors.append("remote mobile receipt contains test markers or credential fields")
    payloads, artifact_errors = _artifact_payloads(root, receipt)
    errors.extend(artifact_errors)
    errors.extend(_validate_remote_documents(receipt, payloads))
    if receipt_created is not None and "audit_verification" in payloads:
        try:
            audit_document = _json_object(
                payloads["audit_verification"], "remote artifact audit_verification"
            )
            if receipt_created < _utc(
                audit_document.get("verified_at_utc"), "remote audit verification"
            ):
                errors.append("remote mobile receipt was created before audit verification")
        except ReleaseCheckError:
            pass
    return RemoteEvidenceResult(
        selected,
        receipt_file_hash,
        source_hash,
        tuple(dict.fromkeys(errors)),
    )


def _normalized_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _filename_version(version: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "_", version)


def _literal_package_version(text: str, label: str) -> str:
    try:
        tree = ast.parse(text, filename=label)
    except SyntaxError as exc:
        raise ReleaseCheckError(f"cannot parse {label}: {exc.msg}") from exc
    versions: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            versions.append(value.value)
    if len(versions) != 1:
        raise ReleaseCheckError(f"{label} must contain exactly one literal __version__")
    return versions[0]


def _source_base(project: Mapping[str, object], root: Path) -> Path:
    tool = project.get("tool", {})
    setuptools = tool.get("setuptools", {}) if isinstance(tool, dict) else {}
    package_dir = setuptools.get("package-dir", {}) if isinstance(setuptools, dict) else {}
    relative = package_dir.get("", ".") if isinstance(package_dir, dict) else "."
    if not isinstance(relative, str):
        raise ReleaseCheckError("tool.setuptools.package-dir[''] must be a path string")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ReleaseCheckError("package source directory escapes the repository") from exc
    if not candidate.is_dir():
        raise ReleaseCheckError(f"package source directory is missing: {relative}")
    return candidate


def _package_data_inventory(
    project: Mapping[str, object], source_base: Path
) -> tuple[str, ...]:
    tool = project.get("tool", {})
    setuptools = tool.get("setuptools", {}) if isinstance(tool, dict) else {}
    configured = (
        setuptools.get("package-data", {}) if isinstance(setuptools, dict) else {}
    )
    if configured in ({}, None):
        return ()
    if not isinstance(configured, dict):
        raise ReleaseCheckError("tool.setuptools.package-data must be a table")

    package_dirs = {
        init.parent.relative_to(source_base).as_posix(): init.parent
        for init in source_base.rglob("__init__.py")
        if init.is_file() and not init.is_symlink()
    }
    inventory: set[str] = set()
    for package, patterns in sorted(configured.items()):
        if not isinstance(package, str) or not isinstance(patterns, list) or not all(
            isinstance(pattern, str) for pattern in patterns
        ):
            raise ReleaseCheckError(
                "tool.setuptools.package-data entries must map package names to string lists"
            )
        if package == "*":
            targets = tuple(package_dirs.values())
        else:
            package_relative = package.replace(".", "/")
            package_dir = source_base / package_relative
            if package_relative not in package_dirs or not package_dir.is_dir():
                raise ReleaseCheckError(
                    f"package-data targets an unknown package: {package}"
                )
            targets = (package_dir,)
        for pattern in patterns:
            pattern_path = PurePosixPath(pattern)
            if pattern_path.is_absolute() or ".." in pattern_path.parts:
                raise ReleaseCheckError(
                    f"package-data pattern escapes its package: {pattern}"
                )
            matched = False
            for package_dir in targets:
                for path in package_dir.glob(pattern):
                    if not path.is_file() or path.is_symlink():
                        continue
                    inventory.add(path.relative_to(source_base).as_posix())
                    matched = True
            if not matched:
                raise ReleaseCheckError(
                    f"package-data pattern matched no regular files: {package}:{pattern}"
                )
    return tuple(sorted(inventory))


def _entry_point_names(text: str, label: str) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise ReleaseCheckError(f"cannot parse console scripts in {label}") from exc
    if not parser.has_section("console_scripts"):
        return {}
    return {name: value.strip() for name, value in parser.items("console_scripts")}


def _entry_target_path(source_base: Path, module: str) -> Path | None:
    module_path = Path(*module.split("."))
    file_target = source_base / module_path.with_suffix(".py")
    if file_target.is_file():
        return file_target
    package_target = source_base / module_path / "__init__.py"
    return package_target if package_target.is_file() else None


def _validate_source_entry_points(source_base: Path, scripts: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    target_pattern = re.compile(
        r"(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):"
        r"(?P<object>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
    )
    for script_name, target in sorted(scripts.items()):
        match = target_pattern.fullmatch(target.strip())
        if not match:
            errors.append(f"console script {script_name!r} has an invalid target")
            continue
        target_path = _entry_target_path(source_base, match.group("module"))
        if target_path is None:
            errors.append(f"console script {script_name!r} targets a missing module")
            continue
        try:
            tree = ast.parse(target_path.read_text(encoding="utf-8"), filename=str(target_path))
        except (OSError, UnicodeError, SyntaxError):
            errors.append(f"console script {script_name!r} target module cannot be parsed")
            continue
        top_name = match.group("object").split(".", 1)[0]
        declared = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        declared.update(
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        )
        if top_name not in declared:
            errors.append(f"console script {script_name!r} targets missing object {top_name!r}")
    return errors


def load_project_info(root: Path = ROOT) -> ProjectInfo:
    pyproject_path = root / "pyproject.toml"
    try:
        project_document = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseCheckError("pyproject.toml is missing or invalid") from exc
    project = project_document.get("project")
    if not isinstance(project, dict):
        raise ReleaseCheckError("pyproject.toml has no project table")
    name = project.get("name")
    version = project.get("version")
    scripts = project.get("scripts", {})
    if not isinstance(name, str) or not name.strip():
        raise ReleaseCheckError("project.name must be a non-empty string")
    if not isinstance(version, str) or not version.strip():
        raise ReleaseCheckError("project.version must be a non-empty string")
    if not isinstance(scripts, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in scripts.items()
    ):
        raise ReleaseCheckError("project.scripts must contain string entry points")

    source_base = _source_base(project_document, root)
    modules = tuple(
        sorted(path.relative_to(source_base).as_posix() for path in source_base.rglob("*.py"))
    )
    if not modules:
        raise ReleaseCheckError("package source contains no Python modules")
    package_name = _normalized_distribution(name)
    package_init = source_base / package_name / "__init__.py"
    if not package_init.is_file():
        raise ReleaseCheckError(f"package initializer is missing: {package_name}/__init__.py")
    package_version = _literal_package_version(
        package_init.read_text(encoding="utf-8"),
        package_init.relative_to(root).as_posix(),
    )
    package_data = _package_data_inventory(project_document, source_base)
    return ProjectInfo(
        name=name,
        version=version,
        package_version=package_version,
        source_base=source_base,
        source_modules=modules,
        package_data=package_data,
        scripts=dict(scripts),
    )


def _javascript_source_contains_secret(
    text: str, *, allow_test_fixtures: bool
) -> bool:
    """Inspect JavaScript literals/comments without treating binding references as values."""
    for match in JAVASCRIPT_FRAGMENT_PATTERN.finditer(text):
        fragment = match.group(0)
        if match.lastgroup in {"line", "block"}:
            if contains_secret(fragment):
                return True
            continue
        if match.lastgroup == "regex":
            continue
        literal = fragment[1:-1]
        if contains_secret(literal):
            return True
        prefix = text[max(0, match.start() - 192) : match.start()]
        if not JAVASCRIPT_SENSITIVE_ASSIGNMENT.search(prefix):
            continue
        if allow_test_fixtures and literal in SAFE_TEST_CREDENTIAL_LITERALS:
            continue
        if contains_secret(f"token={literal}"):
            return True
    return False


def _source_text_contains_secret(
    text: str, suffix: str, *, allow_test_fixtures: bool = False
) -> bool:
    normalized_suffix = suffix.lower()
    if normalized_suffix in JAVASCRIPT_SUFFIXES:
        return _javascript_source_contains_secret(
            text, allow_test_fixtures=allow_test_fixtures
        )
    return source_text_contains_secret(
        text,
        normalized_suffix,
        allow_test_fixtures=allow_test_fixtures,
    )


def _is_test_source_path(relative: str) -> bool:
    return "tests" in PurePosixPath(relative).parts


def run_dev_checks(root: Path = ROOT) -> DevCheckResult:
    errors: list[str] = []
    missing = sorted(name for name in REQUIRED if not (root / name).is_file())
    if missing:
        errors.append("missing required files: " + ", ".join(missing))

    info: ProjectInfo | None = None
    try:
        info = load_project_info(root)
    except (OSError, UnicodeError, ReleaseCheckError) as exc:
        errors.append(str(exc))
    if info is not None:
        if info.version != info.package_version:
            errors.append("pyproject.toml and package __version__ differ")
        errors.extend(_validate_source_entry_points(info.source_base, info.scripts))

    local_markers = tuple(
        marker.strip()
        for marker in os.environ.get("PEERBRIDGE_RELEASE_DENYLIST", "").split(",")
        if marker.strip()
    )
    files = iter_text_files(root)
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"text file is not readable UTF-8: {relative}")
            continue
        if any(pattern.search(text) for pattern in PRIVATE_PATTERNS):
            errors.append(f"private path or project marker in {relative}")
        if any(marker in text for marker in local_markers):
            errors.append(f"operator denylist match in {relative}")
        if _source_text_contains_secret(
            text,
            path.suffix,
            allow_test_fixtures=_is_test_source_path(relative),
        ):
            errors.append(f"credential-like value in {relative}")
    return DevCheckResult(info=info, files_checked=len(files), errors=tuple(errors))


def _safe_archive_names(names: Sequence[str], label: str) -> list[str]:
    errors: list[str] = []
    if len(names) != len(set(names)):
        errors.append(f"{label} contains duplicate member names")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            errors.append(f"{label} contains unsafe member name")
            break
    return errors


def _archive_payload_contains_secret(
    name: str, payload: bytes, *, allow_test_fixtures: bool = False
) -> bool:
    archive_path = PurePosixPath(name)
    suffix = archive_path.suffix.lower()
    if archive_path.name not in TEXT_FILENAMES and suffix not in TEXT_SUFFIXES:
        return contains_secret_bytes(payload)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return contains_secret_bytes(payload)
    return _source_text_contains_secret(
        text,
        suffix,
        allow_test_fixtures=allow_test_fixtures,
    )


def _archive_size_errors(
    members: Sequence[tuple[str, int]], label: str
) -> list[str]:
    errors: list[str] = []
    total = 0
    for name, size in members:
        if size < 0:
            errors.append(f"{label} member has an invalid size: {name}")
            continue
        total += size
        if size > MAX_ARCHIVE_MEMBER_BYTES:
            errors.append(f"{label} member exceeds the size limit: {name}")
    if total > MAX_ARCHIVE_TOTAL_BYTES:
        errors.append(f"{label} uncompressed payload exceeds the total size limit")
    return errors


def _metadata_errors(text: str, info: ProjectInfo, label: str) -> list[str]:
    metadata = Parser().parsestr(text)
    errors: list[str] = []
    if metadata.get("Name") != info.name:
        errors.append(f"{label} project name differs from pyproject.toml")
    if metadata.get("Version") != info.version:
        errors.append(f"{label} version differs from pyproject.toml")
    return errors


def _module_inventory_errors(
    actual: set[str], expected: set[str], label: str
) -> list[str]:
    errors: list[str] = []
    missing = sorted(expected - actual)
    stale = sorted(actual - expected)
    if missing:
        errors.append(f"{label} is missing package modules: {', '.join(missing)}")
    if stale:
        errors.append(f"{label} contains stale package modules: {', '.join(stale)}")
    return errors


def _source_payload_errors(
    archive_payloads: Mapping[str, bytes],
    expected_sources: Mapping[str, Path],
    label: str,
) -> list[str]:
    errors: list[str] = []
    missing = sorted(set(expected_sources) - set(archive_payloads))
    if missing:
        errors.append(f"{label} is missing source-bound files: {', '.join(missing)}")
    for archive_name, source_path in expected_sources.items():
        observed = archive_payloads.get(archive_name)
        if observed is None:
            continue
        try:
            expected = source_path.read_bytes()
        except OSError:
            errors.append(f"{label} source file cannot be read: {archive_name}")
            continue
        if observed != expected:
            errors.append(f"{label} differs from the source tree: {archive_name}")
    return errors


def _wheel_errors(path: Path, info: ProjectInfo, root: Path) -> list[str]:
    errors: list[str] = []
    expected_prefix = f"{_normalized_distribution(info.name)}-{_filename_version(info.version)}-"
    if not path.name.startswith(expected_prefix) or path.suffix != ".whl":
        errors.append("wheel filename does not match project name and version")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            errors.extend(_safe_archive_names(names, "wheel"))
            file_members = [member for member in members if not member.is_dir()]
            errors.extend(
                _archive_size_errors(
                    [(member.filename, member.file_size) for member in file_members],
                    "wheel",
                )
            )
            dist_info = (
                f"{_normalized_distribution(info.name)}-"
                f"{_filename_version(info.version)}.dist-info"
            )
            allowed_members = set(info.source_modules)
            allowed_members.update(info.package_data)
            allowed_members.update(
                f"{dist_info}/{relative}" for relative in WHEEL_DIST_INFO_FILES
            )
            unexpected = sorted(
                member.filename
                for member in file_members
                if member.filename not in allowed_members
            )
            if unexpected:
                errors.append(
                    "wheel contains unexpected members: " + ", ".join(unexpected)
                )
            archive_payloads: dict[str, bytes] = {}
            for member in file_members:
                unix_mode = member.external_attr >> 16
                if unix_mode and stat.S_ISLNK(unix_mode):
                    errors.append(f"wheel contains a symbolic link: {member.filename}")
                    continue
                if member.flag_bits & 0x1:
                    errors.append(f"wheel contains an encrypted member: {member.filename}")
                    continue
                if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    continue
                payload = archive.read(member)
                archive_payloads[member.filename] = payload
                if _archive_payload_contains_secret(member.filename, payload):
                    errors.append(
                        f"wheel contains credential-like content: {member.filename}"
                    )
            missing_legal = sorted(
                f"{dist_info}/{relative}"
                for relative in REQUIRED_WHEEL_LEGAL_FILES
                if f"{dist_info}/{relative}" not in archive_payloads
            )
            if missing_legal:
                errors.append(
                    "wheel is missing required legal files: " + ", ".join(missing_legal)
                )
            source_bound = {
                relative: info.source_base / relative
                for relative in (*info.source_modules, *info.package_data)
            }
            source_bound.update(
                {
                    f"{dist_info}/licenses/{name}": root / name
                    for name in (
                        "LICENSE",
                        "BRAND_ASSETS.md",
                        "THIRD_PARTY_NOTICES.md",
                        "TRADEMARKS.md",
                    )
                }
            )
            errors.extend(
                _source_payload_errors(archive_payloads, source_bound, "wheel")
            )
            metadata_names = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            entry_names = [
                name for name in names if name.endswith(".dist-info/entry_points.txt")
            ]
            if len(metadata_names) != 1:
                errors.append("wheel must contain exactly one METADATA file")
            else:
                metadata = archive.read(metadata_names[0]).decode("utf-8")
                errors.extend(_metadata_errors(metadata, info, "wheel metadata"))
            if len(entry_names) != 1:
                errors.append("wheel must contain exactly one entry_points.txt")
            else:
                actual_scripts = _entry_point_names(
                    archive.read(entry_names[0]).decode("utf-8"), "wheel entry_points.txt"
                )
                if actual_scripts != dict(info.scripts):
                    errors.append("wheel console scripts differ from pyproject.toml")
            actual_modules = {
                name
                for name in names
                if name.endswith(".py") and not name.endswith(".dist-info.py")
            }
            errors.extend(
                _module_inventory_errors(
                    actual_modules, set(info.source_modules), "wheel"
                )
            )
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError, ReleaseCheckError) as exc:
        errors.append(f"wheel cannot be validated: {type(exc).__name__}")
    return errors


def _tar_text(archive: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    stream = archive.extractfile(member)
    if stream is None:
        raise ReleaseCheckError(f"cannot read sdist member {member.name}")
    return stream.read().decode("utf-8")


def _sdist_errors(path: Path, info: ProjectInfo, root: Path) -> list[str]:
    errors: list[str] = []
    expected_name = (
        f"{_normalized_distribution(info.name)}-{_filename_version(info.version)}.tar.gz"
    )
    if path.name != expected_name:
        errors.append("sdist filename does not match project name and version")
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            all_members = archive.getmembers()
            all_names = [member.name for member in all_members]
            errors.extend(_safe_archive_names(all_names, "sdist"))
            unsupported = [
                member.name
                for member in all_members
                if not member.isfile() and not member.isdir()
            ]
            if unsupported:
                errors.append(
                    "sdist contains links or special members: " + ", ".join(unsupported)
                )
            members = [member for member in all_members if member.isfile()]
            errors.extend(
                _archive_size_errors(
                    [(member.name, member.size) for member in members], "sdist"
                )
            )
            roots = {PurePosixPath(name).parts[0] for name in all_names if name}
            if len(roots) != 1:
                errors.append("sdist members must share one top-level directory")
                return errors
            archive_root = next(iter(roots))
            by_relative = {
                PurePosixPath(member.name).relative_to(archive_root).as_posix(): member
                for member in members
            }
            source_relative = info.source_base.relative_to(root).as_posix()
            expected_modules = {
                f"{source_relative}/{module}" for module in info.source_modules
            }
            expected_package_data = {
                f"{source_relative}/{relative}" for relative in info.package_data
            }
            distribution = _normalized_distribution(info.name)
            egg_info_prefix = f"{source_relative}/{distribution}.egg-info"
            expected_tests = {
                path.relative_to(root).as_posix()
                for path in (root / "tests").rglob("*.py")
                if path.is_file()
            }
            allowed_members = set(SDIST_ROOT_FILES)
            allowed_members.update(expected_modules)
            allowed_members.update(expected_package_data)
            allowed_members.update(expected_tests)
            allowed_members.update(
                f"{egg_info_prefix}/{relative}"
                for relative in SDIST_EGG_INFO_FILES
            )
            unexpected = sorted(set(by_relative) - allowed_members)
            if unexpected:
                errors.append(
                    "sdist contains unexpected members: " + ", ".join(unexpected)
                )
            archive_payloads: dict[str, bytes] = {}
            for relative, member in by_relative.items():
                if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    errors.append(f"sdist member cannot be read: {relative}")
                    continue
                payload = stream.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
                if len(payload) != member.size:
                    errors.append(f"sdist member size differs from header: {relative}")
                    continue
                archive_payloads[relative] = payload
                if _archive_payload_contains_secret(
                    relative,
                    payload,
                    allow_test_fixtures=relative.startswith("tests/"),
                ):
                    errors.append(
                        f"sdist contains credential-like content: {relative}"
                    )
            missing_roots = sorted(REQUIRED_SDIST_ROOT_FILES - set(by_relative))
            if missing_roots:
                errors.append(
                    "sdist is missing required root files: " + ", ".join(missing_roots)
                )
            source_bound = {
                f"{source_relative}/{relative}": info.source_base / relative
                for relative in (*info.source_modules, *info.package_data)
            }
            source_bound.update(
                {
                    name: root / name
                    for name in (
                        "BRAND_ASSETS.md",
                        "LICENSE",
                        "MANIFEST.in",
                        "README.md",
                        "THIRD_PARTY_NOTICES.md",
                        "TRADEMARKS.md",
                        "pyproject.toml",
                        "setup.cfg",
                    )
                    if (root / name).is_file()
                }
            )
            errors.extend(
                _source_payload_errors(archive_payloads, source_bound, "sdist")
            )
            actual_modules = {
                name
                for name in by_relative
                if name.endswith(".py")
                and name.startswith(f"{source_relative}/")
            }
            errors.extend(
                _module_inventory_errors(actual_modules, expected_modules, "sdist")
            )

            pyproject_member = by_relative.get("pyproject.toml")
            if pyproject_member is None:
                errors.append("sdist is missing pyproject.toml")
            else:
                built_project = tomllib.loads(_tar_text(archive, pyproject_member))
                built_metadata = built_project.get("project", {})
                if built_metadata.get("name") != info.name:
                    errors.append("sdist pyproject project name differs from source")
                if built_metadata.get("version") != info.version:
                    errors.append("sdist pyproject version differs from source")
                if built_metadata.get("scripts", {}) != dict(info.scripts):
                    errors.append("sdist pyproject console scripts differ from source")

            metadata_members = [
                member
                for relative, member in by_relative.items()
                if relative == "PKG-INFO"
            ]
            if len(metadata_members) != 1:
                errors.append("sdist must contain exactly one top-level PKG-INFO")
            else:
                errors.extend(
                    _metadata_errors(
                        _tar_text(archive, metadata_members[0]), info, "sdist metadata"
                    )
                )

            entry_members = [
                member
                for relative, member in by_relative.items()
                if relative.endswith(".egg-info/entry_points.txt")
            ]
            if len(entry_members) != 1:
                errors.append("sdist must contain exactly one entry_points.txt")
            else:
                actual_scripts = _entry_point_names(
                    _tar_text(archive, entry_members[0]), "sdist entry_points.txt"
                )
                if actual_scripts != dict(info.scripts):
                    errors.append("sdist console scripts differ from pyproject.toml")

            package_init = (
                f"{source_relative}/{_normalized_distribution(info.name)}/__init__.py"
            )
            init_member = by_relative.get(package_init)
            if init_member is None:
                errors.append("sdist is missing the package initializer")
            else:
                built_version = _literal_package_version(
                    _tar_text(archive, init_member), "sdist package initializer"
                )
                if built_version != info.version:
                    errors.append("sdist package __version__ differs from pyproject.toml")
    except (
        OSError,
        UnicodeError,
        tarfile.TarError,
        KeyError,
        ValueError,
        tomllib.TOMLDecodeError,
        ReleaseCheckError,
    ) as exc:
        errors.append(f"sdist cannot be validated: {type(exc).__name__}")
    return errors


def validate_artifacts(
    root: Path, info: ProjectInfo, artifacts: Sequence[Path]
) -> tuple[str, ...]:
    wheels = [path for path in artifacts if path.suffix == ".whl"]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    errors: list[str] = []
    if len(wheels) != 1:
        errors.append(f"fresh build produced {len(wheels)} wheels; expected exactly one")
    if len(sdists) != 1:
        errors.append(f"fresh build produced {len(sdists)} sdists; expected exactly one")
    known = set(wheels + sdists)
    extras = sorted(path.name for path in artifacts if path not in known)
    if extras:
        errors.append("fresh build produced unexpected artifacts: " + ", ".join(extras))
    if len(wheels) == 1:
        errors.extend(_wheel_errors(wheels[0], info, root))
    if len(sdists) == 1:
        errors.extend(_sdist_errors(sdists[0], info, root))
    return tuple(errors)


def find_same_version_artifacts(dist_dir: Path, info: ProjectInfo) -> list[Path]:
    if not dist_dir.is_dir():
        return []
    prefix = f"{_normalized_distribution(info.name)}-{_filename_version(info.version)}"
    return sorted(
        path
        for path in dist_dir.iterdir()
        if path.is_file()
        and (
            (path.name.startswith(prefix + "-") and path.suffix == ".whl")
            or path.name == prefix + ".tar.gz"
        )
    )


def _snapshot_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _ignored_part(name)}


def _copy_release_snapshot(root: Path, destination: Path) -> None:
    inventory = None
    if (root / ".git").exists():
        inventory = _git_file_inventory(root, include_untracked=False)
    if inventory is None:
        shutil.copytree(root, destination, ignore=_snapshot_ignore)
        return
    destination.mkdir()
    for source in inventory:
        if source.is_symlink():
            raise ReleaseCheckError(
                f"release index contains a symbolic link: {source.relative_to(root).as_posix()}"
            )
        target = destination / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _git_head_timestamp(root: Path) -> int | None:
    if not (root / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%ct", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return int(value) if completed.returncode == 0 and value.isdigit() else None


def _build_environment(
    work_dir: Path, *, source_date_epoch: int | None = None
) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["SOURCE_DATE_EPOCH"] = str(source_date_epoch or 315532800)
    env["TZ"] = "UTC"
    if sys.platform != "win32":
        return env

    bootstrap_dir = work_dir / "build-bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / "sitecustomize.py").write_text(
        "import platform as _platform\n"
        "if hasattr(_platform, '_wmi'):\n"
        "    _platform._wmi = None\n",
        encoding="utf-8",
    )
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(bootstrap_dir)
    if existing:
        env["PYTHONPATH"] += os.pathsep + existing
    return env


def _normalize_sdist(path: Path, source_date_epoch: int) -> None:
    replacement = path.with_name(path.name + ".normalized")
    try:
        with tarfile.open(path, mode="r:gz") as source:
            with replacement.open("wb") as raw_output:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=9,
                    fileobj=raw_output,
                    mtime=source_date_epoch,
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                    ) as target:
                        for member in source.getmembers():
                            normalized = copy.copy(member)
                            normalized.mtime = source_date_epoch
                            normalized.uid = 0
                            normalized.gid = 0
                            normalized.uname = ""
                            normalized.gname = ""
                            normalized.pax_headers = {
                                key: value
                                for key, value in member.pax_headers.items()
                                if key not in {"atime", "ctime", "mtime"}
                            }
                            stream = source.extractfile(member) if member.isfile() else None
                            target.addfile(normalized, stream)
        os.replace(replacement, path)
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseCheckError("fresh sdist could not be normalized") from exc
    finally:
        replacement.unlink(missing_ok=True)


def build_fresh_artifacts(root: Path, work_dir: Path) -> list[Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    source_snapshot = work_dir / "source"
    artifact_dir = work_dir / "artifacts"
    _copy_release_snapshot(root, source_snapshot)
    artifact_dir.mkdir()
    source_date_epoch = _git_head_timestamp(root) or 315532800
    command = [
        sys.executable,
        "-m",
        "build",
        "--no-isolation",
        "--sdist",
        "--wheel",
        "--outdir",
        str(artifact_dir),
        str(source_snapshot),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=source_snapshot,
            env=_build_environment(
                work_dir, source_date_epoch=source_date_epoch
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseCheckError("fresh artifact build timed out") from exc
    except OSError as exc:
        raise ReleaseCheckError("fresh artifact build could not start") from exc
    if completed.returncode != 0:
        raise ReleaseCheckError(
            f"fresh artifact build failed with exit code {completed.returncode}"
        )
    artifacts = sorted(path for path in artifact_dir.iterdir() if path.is_file())
    for artifact in artifacts:
        if artifact.name.endswith(".tar.gz"):
            _normalize_sdist(artifact, source_date_epoch)
    return artifacts


def _artifact_reproducibility_errors(
    first: Sequence[Path], second: Sequence[Path]
) -> tuple[str, ...]:
    first_by_name = {path.name: path for path in first}
    second_by_name = {path.name: path for path in second}
    errors: list[str] = []
    if set(first_by_name) != set(second_by_name):
        errors.append("repeated release builds produced different artifact names")
        return tuple(errors)
    for name in sorted(first_by_name):
        if _sha256(first_by_name[name].read_bytes()) != _sha256(
            second_by_name[name].read_bytes()
        ):
            errors.append(f"repeated release build is not byte-identical: {name}")
    return tuple(errors)


def _publish_artifacts(
    artifacts: Sequence[Path],
    dist_dir: Path,
    info: ProjectInfo,
    replace_existing: bool,
) -> list[Path]:
    collisions = find_same_version_artifacts(dist_dir, info)
    if collisions and not replace_existing:
        names = ", ".join(path.name for path in collisions)
        raise ReleaseCheckError(
            "same-version dist artifacts appeared before publish: " + names
        )
    dist_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for artifact in artifacts:
            handle = tempfile.NamedTemporaryFile(
                prefix=f".{artifact.name}.", suffix=".tmp", dir=dist_dir, delete=False
            )
            temporary = Path(handle.name)
            handle.close()
            shutil.copyfile(artifact, temporary)
            staged.append((temporary, dist_dir / artifact.name))
        if replace_existing:
            target_names = {target.name for _, target in staged}
            for collision in collisions:
                if collision.name not in target_names:
                    collision.unlink()
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    return [target for _, target in staged]


def run_build_check(root: Path) -> tuple[ProjectInfo, int, int]:
    dev = run_dev_checks(root)
    if dev.errors or dev.info is None:
        detail = "; ".join(dev.errors) or "project metadata is unavailable"
        raise ReleaseCheckError(f"development checks failed before build check: {detail}")
    source_tree_before_build = release_source_tree_sha256(root)
    with tempfile.TemporaryDirectory(prefix="peerbridge-build-check-") as temporary:
        artifacts = build_fresh_artifacts(root, Path(temporary))
        if release_source_tree_sha256(root) != source_tree_before_build:
            raise ReleaseCheckError("release source tree changed during build check")
        artifact_errors = validate_artifacts(root, dev.info, artifacts)
        if artifact_errors:
            raise ReleaseCheckError("; ".join(artifact_errors))
        artifact_count = len(artifacts)
    return dev.info, artifact_count, dev.files_checked


def run_strict_release(
    root: Path,
    dist_dir: Path,
    *,
    replace_existing: bool = False,
    remote_receipt: Path | None = None,
    require_remote_mobile: bool = True,
    require_clean_git: bool = True,
) -> tuple[ProjectInfo, list[Path], int, RemoteEvidenceResult | None]:
    if require_clean_git:
        git_errors = git_release_preflight_errors(root)
        if git_errors:
            raise ReleaseCheckError("; ".join(git_errors))
    dev = run_dev_checks(root)
    if dev.errors or dev.info is None:
        detail = "; ".join(dev.errors) or "project metadata is unavailable"
        raise ReleaseCheckError(f"development checks failed before strict release: {detail}")
    info = dev.info
    source_tree_before_build = release_source_tree_sha256(
        root, tracked_only=require_clean_git
    )
    remote: RemoteEvidenceResult | None = None
    if require_remote_mobile:
        remote = validate_remote_mobile_evidence(root, remote_receipt)
        if remote.errors:
            raise ReleaseCheckError(
                "remote/mobile release gate failed: " + "; ".join(remote.errors)
            )
    collisions = find_same_version_artifacts(dist_dir, info)
    if collisions and not replace_existing:
        names = ", ".join(path.name for path in collisions)
        raise ReleaseCheckError(
            "same-version dist artifacts already exist: "
            + names
            + "; use --replace-existing-dist to replace them after validation"
        )
    with tempfile.TemporaryDirectory(prefix="peerbridge-release-") as temporary:
        work_dir = Path(temporary)
        artifacts = build_fresh_artifacts(root, work_dir / "first-build")
        source_tree_after_build = release_source_tree_sha256(
            root, tracked_only=require_clean_git
        )
        if source_tree_after_build != source_tree_before_build:
            raise ReleaseCheckError(
                "release source tree changed during artifact validation"
            )
        if remote is not None and source_tree_after_build != remote.source_tree_sha256:
            raise ReleaseCheckError(
                "release source tree changed after remote/mobile evidence validation"
            )
        artifact_errors = validate_artifacts(root, info, artifacts)
        if artifact_errors:
            raise ReleaseCheckError("; ".join(artifact_errors))
        repeated_artifacts = build_fresh_artifacts(root, work_dir / "repeat-build")
        if release_source_tree_sha256(
            root, tracked_only=require_clean_git
        ) != source_tree_before_build:
            raise ReleaseCheckError("release source tree changed during repeated build")
        reproducibility_errors = _artifact_reproducibility_errors(
            artifacts, repeated_artifacts
        )
        if reproducibility_errors:
            raise ReleaseCheckError("; ".join(reproducibility_errors))
        published = _publish_artifacts(
            artifacts, dist_dir, info, replace_existing=replace_existing
        )
    return info, published, dev.files_checked, remote


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a non-release development check, or build and validate fresh release artifacts."
        )
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="build fresh wheel/sdist artifacts and perform strict release validation",
    )
    parser.add_argument(
        "--build-check",
        action="store_true",
        help="build and inspect a fresh wheel/sdist without claiming release readiness",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=Path("dist"),
        help="release artifact destination (default: dist)",
    )
    parser.add_argument(
        "--replace-existing-dist",
        action="store_true",
        help="explicitly replace existing same-version artifacts after fresh validation",
    )
    parser.add_argument(
        "--remote-receipt",
        type=Path,
        default=REMOTE_RECEIPT_DEFAULT,
        help=(
            "real phone reconnect/mobile-browser receipt "
            "(default: .peerbridge/receipts/remote-mobile-e2e-v2.json)"
        ),
    )
    parser.add_argument(
        "--release-profile",
        choices=("local-alpha", "remote-experimental"),
        default="remote-experimental",
        help=(
            "release evidence profile: local-alpha excludes the separately gated "
            "experimental remote/mobile feature; remote-experimental requires a "
            "fresh real-phone receipt"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.release and args.build_check:
        parser.error("--release and --build-check are mutually exclusive")
    if args.replace_existing_dist and not args.release:
        parser.error("--replace-existing-dist requires --release")
    dist_dir = args.dist_dir if args.dist_dir.is_absolute() else ROOT / args.dist_dir

    if args.build_check:
        try:
            info, artifact_count, files_checked = run_build_check(ROOT)
        except ReleaseCheckError as exc:
            print("BUILD_CHECK_FAILED release_ready=false")
            print(f"- {exc}")
            return 1
        print(
            "BUILD_CHECK_OK "
            f"files={files_checked} version={info.version} artifacts={artifact_count} "
            "release_ready=false strict_release_required=true"
        )
        return 0

    if not args.release:
        result = run_dev_checks(ROOT)
        if result.errors:
            print("DEV_CHECK_FAILED release_ready=false")
            for error in result.errors:
                print(f"- {error}")
            return 1
        assert result.info is not None
        print(
            "DEV_CHECK_OK "
            f"files={result.files_checked} version={result.info.version} "
            "release_ready=false strict_release_required=true"
        )
        return 0

    try:
        remote_receipt = (
            args.remote_receipt
            if args.remote_receipt.is_absolute()
            else ROOT / args.remote_receipt
        )
        info, artifacts, files_checked, remote = run_strict_release(
            ROOT,
            dist_dir.resolve(),
            replace_existing=args.replace_existing_dist,
            remote_receipt=remote_receipt,
            require_remote_mobile=args.release_profile == "remote-experimental",
        )
    except ReleaseCheckError as exc:
        print("RELEASE_CHECK_FAILED release_ready=false")
        print(f"- {exc}")
        return 1
    if remote is None:
        print(
            "RELEASE_CHECK_OK "
            f"files={files_checked} version={info.version} artifacts={len(artifacts)} "
            "profile=local-alpha remote_mobile_e2e=not_in_release_scope "
            "release_ready=true"
        )
    else:
        print(
            "RELEASE_CHECK_OK "
            f"files={files_checked} version={info.version} artifacts={len(artifacts)} "
            f"profile=remote-experimental remote_receipt_sha256={remote.receipt_sha256} "
            "remote_mobile_e2e=true tailnet_only=true funnel_enabled=false "
            "release_ready=true"
        )
    for artifact in artifacts:
        print(f"- {artifact.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
