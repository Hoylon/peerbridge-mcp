"""Create-only, privacy-preserving evidence for a physical-phone remote run."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from .bridge import ZERO_SHA256, sha256_bytes, stable_sha256


TRACE_SCHEMA = "peerbridge.mobile-browser-reconnect-trace.v2"
AUDIT_SCHEMA = "peerbridge.remote-audit-verification.v1"
RECEIPT_SCHEMA = "peerbridge.remote-mobile-e2e-receipt.v2"
RECEIPT_DEFAULT = Path(".peerbridge/receipts/remote-mobile-e2e-v2.json")
STATE_SCHEMA = "peerbridge.mobile-evidence-capture-state.v1"
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ARTIFACT_NAMES = {
    "serve_state",
    "serve_status",
    "funnel_status",
    "network_observation",
    "browser_trace",
    "audit_verification",
}
TEXT_SUFFIXES = {
    ".cfg", ".cmd", ".ini", ".json", ".md", ".py", ".ps1", ".toml",
    ".txt", ".yaml", ".yml",
}
IGNORED_PARTS = {
    ".git", ".mypy_cache", ".peerbridge", ".pytest-tmp", ".pytest_cache",
    ".ruff_cache", ".tools", ".venv", "__pycache__", "build", "dist",
    "drafts", "htmlcov", "logs", "venv",
}


class EvidenceError(ValueError):
    """A fail-closed evidence capture error safe to show to the operator."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EvidenceError("invalid evidence timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceError("evidence timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _hash_secret(value: str, label: str) -> str:
    text = str(value or "")
    if not 24 <= len(text) <= 512 or any(ch in text for ch in "\r\n\x00"):
        raise EvidenceError(f"invalid {label}")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _write_create_or_verify(path: Path, payload: bytes) -> None:
    """Create immutable evidence, or accept an exact crash-retry replay."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise EvidenceError(f"cannot verify existing evidence: {path.name}") from exc
        if existing != payload:
            raise EvidenceError(f"immutable evidence differs: {path.name}")


def _write_state(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(value)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} is not an object")
    return value


def _mobile_viewport(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"width", "height", "max_touch_points"}:
        raise EvidenceError("invalid mobile viewport")
    try:
        result = {key: int(value[key]) for key in value}
    except (TypeError, ValueError) as exc:
        raise EvidenceError("invalid mobile viewport") from exc
    if not 240 <= result["width"] <= 1024 or result["height"] < 320:
        raise EvidenceError("viewport is not credible for a phone")
    if not 1 <= result["max_touch_points"] <= 32:
        raise EvidenceError("touch observation is not credible for a phone")
    return result


def _verify_audit_read_only(db_path: Path, scope: str, message: Mapping[str, Any]) -> dict[str, Any]:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with contextlib.closing(
        sqlite3.connect(uri, uri=True, timeout=3.0)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        stored = connection.execute(
            "SELECT content_sha256 FROM messages WHERE scope=? AND message_id=?",
            (scope, message["message_id"]),
        ).fetchone()
        if stored is None or stored["content_sha256"] != message["content_sha256"]:
            raise EvidenceError("captured MCP message is not present in the scope database")
        rows = connection.execute(
            "SELECT * FROM events WHERE scope=? ORDER BY sequence ASC", (scope,)
        ).fetchall()
    previous = ZERO_SHA256
    bound_message = False
    errors: list[dict[str, Any]] = []
    for row in rows:
        payload_sha = sha256_bytes(row["payload_json"].encode("utf-8"))
        envelope = {
            "event_id": row["event_id"], "scope": row["scope"], "actor": row["actor"],
            "event_type": row["event_type"], "task_id": row["task_id"],
            "payload_sha256": payload_sha, "created_utc": row["created_utc"],
            "prev_chain_sha256": previous,
        }
        chain_sha = stable_sha256(envelope)
        if row["payload_sha256"] != payload_sha:
            errors.append({"sequence": row["sequence"], "error": "payload_sha256"})
        if row["prev_chain_sha256"] != previous:
            errors.append({"sequence": row["sequence"], "error": "prev_chain_sha256"})
        if row["chain_sha256"] != chain_sha:
            errors.append({"sequence": row["sequence"], "error": "chain_sha256"})
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {}
        if (
            row["event_type"] == "message.sent"
            and payload.get("message_id") == message["message_id"]
            and payload.get("content_sha256") == message["content_sha256"]
        ):
            bound_message = True
        previous = row["chain_sha256"]
    if errors or not bound_message:
        raise EvidenceError("scope audit chain or message binding is invalid")
    return {
        "schema": AUDIT_SCHEMA,
        "scope": scope,
        "valid": True,
        "verified_at_utc": _utc_now(),
        "audit_head_sha256": previous,
        "event_count": len(rows),
        "message_id": message["message_id"],
        "writes_performed": 0,
    }


class MobileEvidenceCapture:
    """Durable hashed state; final browser and audit documents are create-only."""

    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        *,
        run_id: str,
        scope: str,
        public_origin: str,
        instance_id: str,
        minimum_gap_seconds: int = 10,
    ) -> None:
        if not SAFE_RUN_ID.fullmatch(run_id):
            raise EvidenceError("invalid evidence run ID")
        if not 0 <= minimum_gap_seconds <= 3600:
            raise EvidenceError("invalid evidence disconnect gap")
        self.project_root = project_root.resolve()
        self.db_path = db_path.resolve()
        self.run_id = run_id
        self.scope = scope
        self.public_origin = public_origin
        self.instance_id = instance_id
        self.minimum_gap_seconds = minimum_gap_seconds
        self.directory = self.project_root / ".peerbridge" / "evidence" / run_id
        self.state_path = self.directory / "capture-state.json"
        self.browser_trace_path = self.directory / "browser-trace.json"
        self.audit_path = self.directory / "audit-verification.json"
        self._lock = threading.Lock()

    def _new_state(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "run_id": self.run_id,
            "scope": self.scope,
            "public_origin": self.public_origin,
            "instance_id": self.instance_id,
            "minimum_gap_seconds": self.minimum_gap_seconds,
            "status": "ready",
            "created_at_utc": _utc_now(),
            "sessions": [],
            "message": None,
            "disconnect": None,
        }

    def _load(self) -> dict[str, Any]:
        state = _read_object(self.state_path, "capture state") if self.state_path.exists() else self._new_state()
        expected = {
            "schema": STATE_SCHEMA, "run_id": self.run_id, "scope": self.scope,
            "public_origin": self.public_origin, "instance_id": self.instance_id,
            "minimum_gap_seconds": self.minimum_gap_seconds,
        }
        if any(state.get(key) != value for key, value in expected.items()):
            raise EvidenceError("capture state does not match the running remote instance")
        return state

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            return {
                "enabled": True,
                "run_id": self.run_id,
                "status": state["status"],
                "expected_task_id": f"remote-e2e-{self.run_id}",
                "initial_recorded": len(state["sessions"]) >= 1,
                "message_recorded": state["message"] is not None,
                "disconnect_recorded": state["disconnect"] is not None,
                "complete": self.browser_trace_path.exists() and self.audit_path.exists(),
            }

    def _trace(self, state: Mapping[str, Any]) -> dict[str, Any]:
        sessions = list(state.get("sessions") or [])
        if len(sessions) != 2 or not isinstance(state.get("disconnect"), dict):
            raise EvidenceError("capture state is not ready to seal")
        return {
            "schema": TRACE_SCHEMA,
            "test_mode": False,
            "evidence_origin": "real-device",
            "device_class": "phone",
            "scope": self.scope,
            "public_origin": self.public_origin,
            "tailnet_identity_sha256": sessions[0]["tailnet_identity_sha256"],
            "browser_device_continuity_source": (
                "browser-local-storage-random-nonce"
            ),
            "network_layer_node_identity_attested": False,
            "viewport": state["viewport"],
            "sessions": sessions,
            "disconnected_at_utc": state["disconnect"]["marked_at_utc"],
            "disconnect_evidence": {
                "method": (
                    "operator-marked-disconnect-plus-fresh-browser-session"
                ),
                "challenge_sha256": state["disconnect_challenge_sha256"],
                "minimum_gap_seconds": self.minimum_gap_seconds,
                "observed_gap_seconds": state["disconnect"][
                    "observed_gap_seconds"
                ],
                "network_layer_disconnect_cryptographically_proven": False,
            },
            "message": state["message"],
        }

    def _finish_seal(self, state: dict[str, Any]) -> dict[str, Any]:
        audit = state.get("pending_audit")
        if not isinstance(audit, dict):
            raise EvidenceError("capture state lacks pending audit verification")
        _write_create_or_verify(
            self.browser_trace_path, _canonical_bytes(self._trace(state))
        )
        _write_create_or_verify(self.audit_path, _canonical_bytes(audit))
        state = dict(state)
        state.pop("pending_audit", None)
        state["status"] = "sealed"
        _write_state(self.state_path, state)
        return self.status_unlocked(state)

    def record_session(
        self,
        *,
        phase: str,
        login: str,
        user_agent: str,
        device_nonce: str,
        session_nonce: str,
        viewport: object,
        snapshot_signature: str,
        live_snapshot: Mapping[str, Any],
        disconnect_challenge: str | None,
    ) -> dict[str, Any]:
        if phase not in {"initial", "reconnect"}:
            raise EvidenceError("invalid evidence phase")
        if not SHA256.fullmatch(str(snapshot_signature or "")):
            raise EvidenceError("snapshot signature is missing")
        if snapshot_signature != live_snapshot.get("snapshot_signature"):
            raise EvidenceError("browser snapshot is stale")
        identity_hash = hashlib.sha256(login.encode("utf-8")).hexdigest()
        device_hash = _hash_secret(device_nonce, "browser device nonce")
        session_hash = _hash_secret(session_nonce, "browser session nonce")
        ua_hash = hashlib.sha256(str(user_agent or "").encode("utf-8")).hexdigest()
        mobile = _mobile_viewport(viewport)
        with self._lock:
            state = self._load()
            if state.get("status") == "sealed":
                if not self.browser_trace_path.is_file() or not self.audit_path.is_file():
                    raise EvidenceError("sealed capture state is incomplete")
                return self.status_unlocked(state)
            if state.get("status") == "sealing":
                return self._finish_seal(state)
            if self.browser_trace_path.exists() or self.audit_path.exists():
                raise EvidenceError("evidence files exist without sealing state")
            sessions = state["sessions"]
            now = _utc_now()
            if phase == "initial":
                if sessions or state["status"] != "ready":
                    raise EvidenceError("initial phone session is already recorded")
                challenge = secrets_token()
                state["sessions"] = [{
                    "phase": "initial", "connected_at_utc": now,
                    "authenticated": True, "transport": "tailnet-https",
                    "identity_source": "Tailscale-User-Login", "page_status": 200,
                    "snapshot_status": 200, "browser_session_id_sha256": session_hash,
                    "browser_device_continuity_sha256": device_hash,
                    "tailnet_identity_sha256": identity_hash,
                    "user_agent_sha256": ua_hash, "instance_id": self.instance_id,
                    "snapshot_signature": snapshot_signature,
                }]
                state["viewport"] = mobile
                state["disconnect_challenge_sha256"] = hashlib.sha256(challenge.encode()).hexdigest()
                state["status"] = "initial-recorded-awaiting-message"
                _write_state(self.state_path, state)
                return {**self.status_unlocked(state), "disconnect_challenge": challenge}
            if len(sessions) != 1 or state["disconnect"] is None or state["message"] is None:
                raise EvidenceError("initial session, message and disconnect marker are required")
            if not disconnect_challenge or hashlib.sha256(disconnect_challenge.encode()).hexdigest() != state.get("disconnect_challenge_sha256"):
                raise EvidenceError("disconnect challenge does not match")
            first = sessions[0]
            if first["tailnet_identity_sha256"] != identity_hash:
                raise EvidenceError("tailnet user identity changed")
            if first["browser_device_continuity_sha256"] != device_hash:
                raise EvidenceError("browser device continuity changed")
            if first["browser_session_id_sha256"] == session_hash:
                raise EvidenceError("reconnect must use a fresh browser session")
            gap = int((_utc(now) - _utc(state["disconnect"]["marked_at_utc"])).total_seconds())
            if gap < self.minimum_gap_seconds:
                raise EvidenceError("disconnect interval is too short")
            observed = {
                str(row.get("message_id")) for row in live_snapshot.get("messages", [])
            }
            if state["message"]["message_id"] not in observed:
                raise EvidenceError("reconnect snapshot does not contain the captured message")
            reconnect = {
                "phase": "reconnect", "connected_at_utc": now,
                "authenticated": True, "transport": "tailnet-https",
                "identity_source": "Tailscale-User-Login", "page_status": 200,
                "snapshot_status": 200, "browser_session_id_sha256": session_hash,
                "browser_device_continuity_sha256": device_hash,
                "tailnet_identity_sha256": identity_hash,
                "user_agent_sha256": ua_hash, "instance_id": self.instance_id,
                "snapshot_signature": snapshot_signature,
                "observed_message_id": state["message"]["message_id"],
            }
            state["sessions"].append(reconnect)
            state["status"] = "sealing"
            state["disconnect"]["observed_gap_seconds"] = gap
            state["pending_audit"] = _verify_audit_read_only(
                self.db_path, self.scope, state["message"]
            )
            _write_state(self.state_path, state)
            return self._finish_seal(state)

    def status_unlocked(self, state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "enabled": True, "run_id": self.run_id, "status": state["status"],
            "expected_task_id": f"remote-e2e-{self.run_id}",
            "initial_recorded": len(state["sessions"]) >= 1,
            "message_recorded": state["message"] is not None,
            "disconnect_recorded": state["disconnect"] is not None,
            "complete": self.browser_trace_path.exists() and self.audit_path.exists(),
        }

    def record_message(self, *, task_id: str, result: Mapping[str, Any]) -> None:
        with self._lock:
            state = self._load()
            if state["status"] not in {"initial-recorded-awaiting-message", "message-recorded-awaiting-disconnect"}:
                return
            if task_id != f"remote-e2e-{self.run_id}":
                return
            message_id = str(result.get("message_id") or "")
            content_sha = str(result.get("content_sha256") or "")
            if not message_id or not SHA256.fullmatch(content_sha):
                raise EvidenceError("MCP message receipt is incomplete")
            candidate = {"status": 201, "message_id": message_id, "content_sha256": content_sha}
            if state["message"] not in (None, candidate):
                raise EvidenceError("evidence message is already bound")
            state["message"] = candidate
            state["status"] = "message-recorded-awaiting-disconnect"
            _write_state(self.state_path, state)

    def mark_disconnect(self, *, login: str, device_nonce: str, disconnect_challenge: str) -> dict[str, Any]:
        identity_hash = hashlib.sha256(login.encode("utf-8")).hexdigest()
        device_hash = _hash_secret(device_nonce, "browser device nonce")
        with self._lock:
            state = self._load()
            if state["status"] != "message-recorded-awaiting-disconnect" or len(state["sessions"]) != 1:
                raise EvidenceError("initial session and captured MCP message are required")
            first = state["sessions"][0]
            if first["tailnet_identity_sha256"] != identity_hash or first["browser_device_continuity_sha256"] != device_hash:
                raise EvidenceError("disconnect marker identity differs from initial session")
            if hashlib.sha256(disconnect_challenge.encode()).hexdigest() != state.get("disconnect_challenge_sha256"):
                raise EvidenceError("disconnect challenge does not match")
            state["disconnect"] = {"marked_at_utc": _utc_now()}
            state["status"] = "disconnect-marked-awaiting-fresh-session"
            _write_state(self.state_path, state)
            return self.status_unlocked(state)


def secrets_token() -> str:
    return secrets.token_urlsafe(32)


def iter_release_text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        parts = path.relative_to(root).parts
        if any(
            part in IGNORED_PARTS or part.startswith(".pytest-tmp-")
            or part.endswith(".egg-info") or part.endswith(".pyc")
            for part in parts
        ):
            continue
        files.append(path)
    return sorted(files)


def release_source_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in iter_release_text_files(root.resolve()):
        relative = path.relative_to(root.resolve()).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big")); digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


def _safe_evidence_path(root: Path, run_id: str, filename: str) -> Path:
    relative = PurePosixPath(".peerbridge", "evidence", run_id, filename)
    path = root.joinpath(*relative.parts)
    path.resolve().relative_to(root.resolve())
    return path


def _capture(command: list[str], *, timeout: int = 20) -> tuple[bytes, int]:
    completed = subprocess.run(
        command, capture_output=True, timeout=timeout, check=False
    )
    return completed.stdout, completed.returncode


def _network_observation(port: int) -> tuple[dict[str, Any], list[str]]:
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        f"Get-NetTCPConnection -State Listen -LocalPort {port} | Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress",
    ]
    payload, code = _capture(command)
    if code:
        raise EvidenceError("cannot capture backend listener ownership")
    try:
        value = json.loads(payload.decode("utf-8-sig") or "null")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("listener capture was not JSON") from exc
    rows = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    loopback = [row for row in rows if row.get("LocalAddress") == "127.0.0.1"]
    public = [row for row in rows if row.get("LocalAddress") not in {"127.0.0.1", "::1"}]
    if len(loopback) != 1 or public:
        raise EvidenceError("backend listener is not exclusively IPv4 loopback")
    return {
        "schema": "peerbridge.remote-network-observation.v1",
        "captured_at_utc": _utc_now(),
        "backend_listener": {
            "address": "127.0.0.1", "port": int(loopback[0]["LocalPort"]),
            "process_id": int(loopback[0]["OwningProcess"]),
        },
        "non_loopback_listeners_on_backend_port": [],
    }, command


def _descriptor(path: Path, root: Path, command: list[str], exit_code: int = 0) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(), "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "capture_command": command, "exit_code": exit_code,
    }


def finalize_receipt(
    project_root: Path,
    *,
    run_id: str,
    serve_state_path: Path,
    tailscale_executable: str,
    receipt_path: Path | None = None,
) -> Path:
    root = project_root.resolve()
    if not SAFE_RUN_ID.fullmatch(run_id):
        raise EvidenceError("invalid evidence run ID")
    evidence = root / ".peerbridge" / "evidence" / run_id
    browser = evidence / "browser-trace.json"
    audit = evidence / "audit-verification.json"
    if not browser.is_file() or not audit.is_file():
        raise EvidenceError("phone browser and audit evidence are not sealed")
    state_source = serve_state_path if serve_state_path.is_absolute() else root / serve_state_path
    state_payload = state_source.read_bytes()
    serve_state = _read_object(state_source, "Serve state")
    parsed = urlsplit(str(serve_state.get("local_backend") or ""))
    if parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise EvidenceError("Serve state backend is not explicit loopback")
    commands: dict[str, list[str]] = {
        "serve_state": ["read-file", str(serve_state_path)],
        "serve_status": [tailscale_executable, "serve", "status", "--json"],
        "funnel_status": [tailscale_executable, "funnel", "status", "--json"],
        "browser_trace": ["peerbridge-remote", "server-side-phone-evidence", run_id],
        "audit_verification": ["peerbridge-remote", "read-only-audit-verification", run_id],
    }
    serve_payload, serve_code = _capture(commands["serve_status"])
    funnel_payload, funnel_code = _capture(commands["funnel_status"])
    if serve_code or funnel_code:
        raise EvidenceError("Tailscale Serve or Funnel status capture failed")
    try:
        json.loads(serve_payload.decode("utf-8-sig")); json.loads(funnel_payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("Tailscale status capture was not UTF-8 JSON") from exc
    network, network_command = _network_observation(parsed.port)
    commands["network_observation"] = network_command
    artifact_paths = {
        "serve_state": _safe_evidence_path(root, run_id, "serve-state.json"),
        "serve_status": _safe_evidence_path(root, run_id, "serve-status.json"),
        "funnel_status": _safe_evidence_path(root, run_id, "funnel-status.json"),
        "network_observation": _safe_evidence_path(root, run_id, "network-observation.json"),
        "browser_trace": browser,
        "audit_verification": audit,
    }
    _write_create_or_verify(artifact_paths["serve_state"], state_payload)
    _write_create_or_verify(artifact_paths["serve_status"], serve_payload)
    _write_create_or_verify(artifact_paths["funnel_status"], funnel_payload)
    _write_create_or_verify(
        artifact_paths["network_observation"], _canonical_bytes(network)
    )
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        # Bind receipt time to the sealed audit so a crash retry is byte-stable.
        "created_at_utc": _read_object(
            audit, "audit verification"
        )["verified_at_utc"],
        "scope": serve_state["scope"],
        "public_origin": serve_state["public_origin"],
        "local_backend": serve_state["local_backend"],
        "tailnet_only": True,
        "funnel_enabled": False,
        "test_mode": False,
        "evidence_origin": "real-device",
        "source_tree_sha256": release_source_tree_sha256(root),
        "artifacts": {
            name: _descriptor(artifact_paths[name], root, commands[name])
            for name in sorted(ARTIFACT_NAMES)
        },
    }
    receipt["receipt_sha256"] = _canonical_sha(receipt)
    target = receipt_path or root / RECEIPT_DEFAULT
    if not target.is_absolute():
        target = root / target
    _write_create_or_verify(target, _canonical_bytes(receipt))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--serve-state", type=Path, default=Path(".peerbridge/remote-control-serve.json"))
    parser.add_argument("--tailscale-executable", default=r"C:\Program Files\Tailscale\tailscale.exe")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        target = finalize_receipt(
            args.project_root, run_id=args.run_id, serve_state_path=args.serve_state,
            tailscale_executable=args.tailscale_executable, receipt_path=args.receipt,
        )
    except (EvidenceError, OSError, subprocess.SubprocessError) as exc:
        print(f"REMOTE_EVIDENCE_FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "REMOTE_EVIDENCE_RECEIPT_CREATED", "path": str(target)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
