"""Tailnet-only human control plane for PeerBridge.

The backend deliberately binds to loopback. Tailscale Serve terminates HTTPS and
injects authenticated identity headers before proxying to this process.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .secret_scan import redact_secrets
from .monitor import BridgeReader, McpHumanClient
from .remote_evidence import EvidenceError, MobileEvidenceCapture


MAX_REQUEST_BYTES = 64 * 1024
MAX_BODY_CHARS = 20_000
MAX_WRITES_PER_MINUTE = 12
IDENTITY_HEADER = "Tailscale-User-Login"
CSRF_HEADER = "X-PeerBridge-CSRF"
PROXY_AUTH_HEADER = "X-PeerBridge-Proxy-Authorization"
PROXY_CREDENTIAL_ENV = "PEERBRIDGE_REMOTE_PROXY_CREDENTIAL"
SAFE_TASK = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
SAFE_PROXY_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{43,256}\Z")
ALLOWED_PRIORITIES = {"low", "normal", "high", "critical"}


class RemoteControlError(ValueError):
    """An operator-safe remote-control configuration error."""


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def _normalize_login(value: str) -> str:
    login = str(value or "").strip().lower()
    if not login or len(login) > 320 or any(ch in login for ch in "\r\n\x00"):
        raise RemoteControlError("invalid Tailscale login identity")
    return login


def _proxy_credential_sha256(value: str) -> str:
    credential = str(value or "")
    if not SAFE_PROXY_CREDENTIAL.fullmatch(credential):
        raise RemoteControlError(
            "remote proxy credential must be a 256-bit URL-safe secret"
        )
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def identity_agent_id(login: str) -> str:
    digest = hashlib.sha256(_normalize_login(login).encode("utf-8")).hexdigest()
    return f"human-remote-{digest[:16]}"


def tailscale_self_login() -> str:
    """Return the local tailnet owner without persisting or printing it."""
    completed = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if completed.returncode:
        raise RemoteControlError("Tailscale is unavailable or not signed in")
    try:
        payload = json.loads(completed.stdout)
        user_id = str((payload.get("Self") or {}).get("UserID") or "")
        login = str(((payload.get("User") or {}).get(user_id) or {}).get("LoginName") or "")
        return _normalize_login(login)
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RemoteControlError("Tailscale did not return a usable owner identity") from exc


def _redact(value: Any) -> str:
    text = "" if value is None else str(value)
    return redact_secrets(text, "[REDACTED CREDENTIAL]")


def _snapshot_payload(
    reader: BridgeReader, scope: str, instance_id: str | None = None
) -> dict[str, Any]:
    snapshot = reader.snapshot(limit=500, scope=scope)
    now = datetime.now(timezone.utc).timestamp()
    presence = snapshot.presence
    messages = snapshot.messages
    tasks = snapshot.tasks
    routes = snapshot.route_profiles
    providers = snapshot.provider_connections
    payload = {
        "generated_utc": snapshot.generated_utc,
        "scope": scope,
        "instance_id": instance_id,
        "counts": {
            "agents": snapshot.table_counts["agent_presence"],
            "messages": snapshot.table_counts["messages"],
            "tasks": snapshot.table_counts["tasks"],
            "routes": snapshot.table_counts["route_profiles"],
            "providers": snapshot.table_counts["provider_connections"],
        },
        "agents": [
            {
                "agent_id": row.get("agent_id"),
                "client_name": row.get("client_name"),
                "provider_id": row.get("provider_id"),
                "model_id": row.get("model_id"),
                "reasoning_mode": row.get("reasoning_mode"),
                "online": float(row.get("expires_epoch") or 0) >= now,
                "last_seen_utc": row.get("last_seen_utc"),
            }
            for row in presence
        ],
        "messages": [
            {
                "message_id": row.get("message_id"),
                "sender": row.get("sender"),
                "recipient": row.get("recipient"),
                "task_id": row.get("task_id"),
                "subject": _redact(row.get("subject")),
                "body": _redact(row.get("body")),
                "priority": row.get("priority"),
                "created_utc": row.get("created_utc"),
                "content_sha256": row.get("content_sha256"),
                "route_status": row.get("route_status"),
                "requested_provider_id": row.get("requested_provider_id"),
                "requested_model_id": row.get("requested_model_id"),
                "requested_reasoning_mode": row.get("requested_reasoning_mode"),
                "observed_provider_id": row.get("observed_provider_id"),
                "observed_model_id": row.get("observed_model_id"),
                "observed_reasoning_mode": row.get("observed_reasoning_mode"),
            }
            for row in messages
        ],
        "tasks": [
            {
                "task_id": row.get("task_id"),
                "summary": _redact(row.get("summary")),
                "status": row.get("status"),
                "claimed_by": row.get("claimed_by"),
                "approval_mode": row.get("approval_mode"),
                "updated_utc": row.get("updated_utc"),
            }
            for row in tasks
        ],
        "routes": [
            {
                "route_id": row.get("route_id"),
                "agent_id": row.get("agent_id"),
                "route_class": row.get("route_class"),
                "provider_id": row.get("provider_id"),
                "model_id": row.get("model_id"),
                "reasoning_mode": row.get("reasoning_mode"),
                "enabled": bool(row.get("enabled")),
            }
            for row in routes
        ],
        "providers": [
            {
                "connection_id": row.get("connection_id"),
                "display_name": row.get("display_name"),
                "route_class": row.get("route_class"),
                "secret_backend": row.get("secret_backend"),
                "enabled": bool(row.get("enabled")),
                "connection_sha256": row.get("connection_sha256"),
            }
            for row in providers
        ],
    }
    signature_payload = {key: value for key, value in payload.items() if key != "generated_utc"}
    payload["snapshot_signature"] = hashlib.sha256(
        json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


def _page(csrf_token: str, *, evidence_enabled: bool = False) -> bytes:
    token = json.dumps(csrf_token)
    proxy_header = json.dumps(PROXY_AUTH_HEADER)
    evidence = "true" if evidence_enabled else "false"
    markup = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PeerBridge Remote</title><style>
:root{{--bg:#0b1015;--panel:#151d25;--line:#344452;--text:#eaf4f7;--muted:#8ea0ad;--cyan:#59d7e7;--amber:#ffc857;--green:#62d28b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px ui-monospace,Consolas,monospace;letter-spacing:0}}
header{{position:sticky;top:0;z-index:2;background:#090d11;border-bottom:2px solid var(--line);padding:14px 16px;display:flex;justify-content:space-between;align-items:center}}
h1{{font-size:18px;margin:0;color:var(--cyan)}}button,input,select,textarea{{font:inherit;color:var(--text);background:#0d141a;border:1px solid var(--line);border-radius:3px;padding:10px}}
button{{background:var(--cyan);color:#061015;font-weight:700;cursor:pointer}}button:disabled{{opacity:.5}}main{{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:12px;padding:12px;max-width:1400px;margin:auto}}
.panel{{background:var(--panel);border:1px solid var(--line);padding:12px}}.tabs{{display:flex;gap:8px;margin-bottom:10px}}.tabs button{{background:#202b35;color:var(--text)}}.tabs button.active{{background:var(--amber);color:#15100a}}
.row{{border-top:1px solid var(--line);padding:11px 2px}}.meta{{color:var(--muted);font-size:12px;margin-bottom:6px}}.subject{{color:var(--amber);font-weight:700}}.body{{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.45}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}textarea{{min-height:130px;resize:vertical;width:100%}}label{{display:block;color:var(--muted);font-size:12px;margin:9px 0 4px}}.status{{color:var(--green)}}
@media(max-width:760px){{main{{grid-template-columns:1fr;padding:8px}}header{{padding-top:calc(12px + env(safe-area-inset-top))}}.grid{{grid-template-columns:1fr}}aside{{order:-1}}}}
</style></head><body><header><h1>PEERBRIDGE // TAILNET</h1><span id="health" class="status">SYNC</span></header>
<main><section class="panel"><div class="tabs"><button data-tab="messages" class="active">對話</button><button data-tab="tasks">工作</button><button data-tab="agents">Agent</button></div><div id="content">載入中...</div></section>
<aside class="panel"><strong>人工介入</strong><div class="grid"><div><label>收件者</label><input id="recipient" value="*" maxlength="200"></div><div><label>優先度</label><select id="priority"><option>normal</option><option>high</option><option>critical</option><option>low</option></select></div></div>
<label>Task ID</label><input id="task" value="human-mobile" maxlength="200" style="width:100%"><label>主旨</label><input id="subject" value="HUMAN INTERVENTION" maxlength="500" style="width:100%"><label>內容</label><textarea id="body" maxlength="20000"></textarea><button id="send" style="width:100%;margin-top:9px">MCP 發送</button><div id="sendStatus" class="meta" style="margin-top:8px"></div>
<div id="evidencePanel" style="display:none;border-top:1px solid var(--line);margin-top:14px;padding-top:12px"><strong>實機重連證據</strong><div id="evidenceStatus" class="meta" style="margin:8px 0">讀取中...</div><div class="grid"><button id="evidenceInitial">記錄初次連線</button><button id="evidenceDisconnect">標記斷線</button></div><button id="evidenceReconnect" style="width:100%;margin-top:8px">記錄重新連線</button></div></aside></main>
<script>const CSRF={token},EVIDENCE={evidence},PROXY_HEADER={proxy_header};let state=null,tab='messages',evidenceState=null;const esc=s=>String(s??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
const accessKey='peerbridge.remote.access.v1';let accessToken='';try{{let fragment=location.hash.slice(1);if(fragment){{sessionStorage.setItem(accessKey,fragment);history.replaceState(null,'',location.pathname+location.search)}}accessToken=sessionStorage.getItem(accessKey)||''}}catch(_e){{accessToken=''}}
function authHeaders(extra={{}}){{if(!accessToken)throw Error('private access token missing');return Object.assign({{[PROXY_HEADER]:accessToken}},extra)}}
function randomNonce(){{let b=new Uint8Array(32);crypto.getRandomValues(b);return Array.from(b,x=>x.toString(16).padStart(2,'0')).join('')}}
const deviceKey='peerbridge.mobile.device.v1';let deviceNonce=localStorage.getItem(deviceKey);if(!deviceNonce){{deviceNonce=randomNonce();localStorage.setItem(deviceKey,deviceNonce)}}const sessionNonce=randomNonce();
function render(){{if(!state)return;let rows=[];if(tab==='messages')rows=state.messages.map(x=>`<div class=row><div class=meta>${{esc(x.created_utc)}} · ${{esc(x.sender)}} → ${{esc(x.recipient)}} · ${{esc(x.priority)}}</div><div class=subject>${{esc(x.subject)}}</div><div class=body>${{esc(x.body)}}</div></div>`);if(tab==='tasks')rows=state.tasks.map(x=>`<div class=row><div class=meta>${{esc(x.status)}} · ${{esc(x.claimed_by)}} · ${{esc(x.approval_mode)}}</div><div class=subject>${{esc(x.task_id)}}</div><div class=body>${{esc(x.summary)}}</div></div>`);if(tab==='agents')rows=state.agents.map(x=>`<div class=row><div class=meta>${{x.online?'ONLINE':'OFFLINE'}} · ${{esc(x.provider_id)}} · ${{esc(x.model_id)}}</div><div class=subject>${{esc(x.agent_id)}}</div><div class=body>${{esc(x.client_name)}} · ${{esc(x.reasoning_mode)}}</div></div>`);document.getElementById('content').innerHTML=rows.join('')||'<div class=row>暫無資料</div>'}}
async function refresh(){{try{{let r=await fetch('/api/snapshot',{{cache:'no-store',headers:authHeaders()}});if(!r.ok)throw Error(r.status);state=await r.json();render();document.getElementById('health').textContent='ONLINE'}}catch(e){{document.getElementById('health').textContent=accessToken?'RECONNECTING':'ACCESS LINK REQUIRED'}}}}
async function evidenceRefresh(){{if(!EVIDENCE)return;let r=await fetch('/api/e2e/status',{{cache:'no-store',headers:authHeaders()}});if(!r.ok)throw Error(r.status);evidenceState=await r.json();document.getElementById('evidencePanel').style.display='block';document.getElementById('evidenceStatus').textContent=evidenceState.status+' · '+(evidenceState.complete?'COMPLETE':'INCOMPLETE');if(evidenceState.expected_task_id)document.getElementById('task').value=evidenceState.expected_task_id}}
async function evidencePost(path,payload){{let r=await fetch(path,{{method:'POST',headers:authHeaders({{'Content-Type':'application/json','X-PeerBridge-CSRF':CSRF}}),body:JSON.stringify(payload)}});let j=await r.json();if(!r.ok)throw Error(j.error||r.status);await evidenceRefresh();return j}}
function sessionPayload(phase){{if(!state)throw Error('snapshot unavailable');return{{phase,device_nonce:deviceNonce,session_nonce:sessionNonce,viewport:{{width:window.innerWidth,height:window.innerHeight,max_touch_points:navigator.maxTouchPoints||0}},snapshot_signature:state.snapshot_signature,disconnect_challenge:localStorage.getItem('peerbridge.mobile.disconnect.v1')}}}}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{{tab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));render()}});
document.getElementById('send').onclick=async()=>{{let b=document.getElementById('send'),s=document.getElementById('sendStatus');b.disabled=true;s.textContent='發送中...';try{{let payload={{recipient:recipient.value,task_id:task.value,subject:subject.value,body:body.value,priority:priority.value}};let r=await fetch('/api/message',{{method:'POST',headers:authHeaders({{'Content-Type':'application/json','X-PeerBridge-CSRF':CSRF}}),body:JSON.stringify(payload)}});let j=await r.json();if(!r.ok)throw Error(j.error||r.status);s.textContent='已寫入審計鏈 '+String(j.content_sha256||'').slice(0,12);body.value='';await refresh()}}catch(e){{s.textContent='失敗：'+e.message}}finally{{b.disabled=false}}}};
document.getElementById('evidenceInitial').onclick=async()=>{{try{{await refresh();let j=await evidencePost('/api/e2e/session',sessionPayload('initial'));if(j.disconnect_challenge)localStorage.setItem('peerbridge.mobile.disconnect.v1',j.disconnect_challenge)}}catch(e){{document.getElementById('evidenceStatus').textContent='失敗：'+e.message}}}};
document.getElementById('evidenceDisconnect').onclick=async()=>{{try{{await evidencePost('/api/e2e/disconnect',{{device_nonce:deviceNonce,disconnect_challenge:localStorage.getItem('peerbridge.mobile.disconnect.v1')}})}}catch(e){{document.getElementById('evidenceStatus').textContent='失敗：'+e.message}}}};
document.getElementById('evidenceReconnect').onclick=async()=>{{try{{await refresh();await evidencePost('/api/e2e/session',sessionPayload('reconnect'))}}catch(e){{document.getElementById('evidenceStatus').textContent='失敗：'+e.message}}}};
refresh().then(evidenceRefresh).catch(()=>{{}});setInterval(refresh,3000);</script></body></html>"""
    return markup.encode("utf-8")


@dataclass(frozen=True)
class RemoteConfig:
    project_root: Path
    db_path: Path
    scope: str
    allowed_logins: frozenset[str]
    proxy_credential_sha256: str
    csrf_token: str
    public_origin: str
    instance_id: str
    evidence_run_id: str | None = None
    evidence_capture: MobileEvidenceCapture | None = None


class RemoteControlServer(ThreadingHTTPServer):
    daemon_threads = True
    # Reusing an active TCP listener lets two Python processes bind the same
    # loopback port on Windows. A remote-control instance must own its port
    # exclusively so PID/health attestation cannot bind to the wrong process.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], config: RemoteConfig) -> None:
        if not _is_loopback(address[0]):
            raise RemoteControlError("remote control backend must bind to loopback")
        self.config = config
        self.reader = BridgeReader(config.db_path)
        self._write_lock = threading.Lock()
        self._write_times: dict[str, collections.deque[float]] = {}
        super().__init__(address, RemoteHandler)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(10.0)
        return request, client_address

    def allow_write(self, login: str) -> bool:
        now = time.monotonic()
        with self._write_lock:
            samples = self._write_times.setdefault(login, collections.deque())
            while samples and now - samples[0] >= 60:
                samples.popleft()
            if len(samples) >= MAX_WRITES_PER_MINUTE:
                return False
            samples.append(now)
            return True


class RemoteHandler(BaseHTTPRequestHandler):
    server: RemoteControlServer
    protocol_version = "HTTP/1.1"
    server_version = "PeerBridge"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.close_connection = True
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _identity(self) -> str | None:
        try:
            if not _is_loopback(self.client_address[0]):
                self._json(HTTPStatus.FORBIDDEN, {"error": "proxy boundary required"})
                return None
            proxy_credential = self.headers.get(PROXY_AUTH_HEADER, "")
            observed_sha256 = hashlib.sha256(proxy_credential.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(
                observed_sha256,
                self.server.config.proxy_credential_sha256,
            ):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "private access link required"})
                return None
            raw = self.headers.get(IDENTITY_HEADER, "")
            if not raw:
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "Tailscale identity required"})
                return None
            login = _normalize_login(raw)
            if login not in self.server.config.allowed_logins:
                self._json(HTTPStatus.FORBIDDEN, {"error": "Tailscale identity not authorized"})
                return None
            return login
        except RemoteControlError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return None

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/healthz":
            payload: dict[str, Any] = {
                "status": "ok",
                "transport": "loopback",
                "instance_id": self.server.config.instance_id,
                "process_id": os.getpid(),
                "proxy_credential_sha256": self.server.config.proxy_credential_sha256,
            }
            if self.server.config.evidence_run_id is not None:
                payload["evidence_run_id"] = self.server.config.evidence_run_id
            self._json(HTTPStatus.OK, payload)
            return
        if path == "/":
            self._send(
                HTTPStatus.OK,
                _page(
                    self.server.config.csrf_token,
                    evidence_enabled=self.server.config.evidence_capture is not None,
                ),
                "text/html; charset=utf-8",
            )
            return
        if self._identity() is None:
            return
        if path == "/api/snapshot":
            try:
                payload = _snapshot_payload(
                    self.server.reader,
                    self.server.config.scope,
                    self.server.config.instance_id,
                )
            except (OSError, RuntimeError):
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "coordination snapshot unavailable"},
                )
                return
            self._json(HTTPStatus.OK, payload)
            return
        if path == "/api/e2e/status":
            capture = self.server.config.evidence_capture
            if capture is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "evidence capture disabled"})
                return
            try:
                self._json(HTTPStatus.OK, capture.status())
            except EvidenceError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        login = self._identity()
        if login is None:
            return
        path = urlsplit(self.path).path
        if path not in {"/api/message", "/api/e2e/session", "/api/e2e/disconnect"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if self.headers.get(CSRF_HEADER, "") != self.server.config.csrf_token:
            self._json(HTTPStatus.FORBIDDEN, {"error": "CSRF check failed"})
            return
        content_type = self.headers.get("Content-Type", "").lower()
        if not content_type.startswith("application/json"):
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json required"})
            return
        origin = self.headers.get("Origin")
        if not origin or origin.rstrip("/").lower() != self.server.config.public_origin:
            self._json(HTTPStatus.FORBIDDEN, {"error": "origin check failed"})
            return
        if not self.server.allow_write(login):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "write rate limit exceeded"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 1 or length > MAX_REQUEST_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request size rejected"})
            return
        try:
            self.connection.settimeout(10.0)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise RemoteControlError("JSON object required")
            if path == "/api/e2e/session":
                capture = self.server.config.evidence_capture
                if capture is None:
                    raise RemoteControlError("evidence capture disabled")
                if set(payload) - {
                    "phase", "device_nonce", "session_nonce", "viewport",
                    "snapshot_signature", "disconnect_challenge",
                }:
                    raise RemoteControlError("unsupported evidence fields")
                live_snapshot = _snapshot_payload(
                    self.server.reader,
                    self.server.config.scope,
                    self.server.config.instance_id,
                )
                result = capture.record_session(
                    phase=str(payload.get("phase") or ""),
                    login=login,
                    user_agent=self.headers.get("User-Agent", ""),
                    device_nonce=str(payload.get("device_nonce") or ""),
                    session_nonce=str(payload.get("session_nonce") or ""),
                    viewport=payload.get("viewport"),
                    snapshot_signature=str(payload.get("snapshot_signature") or ""),
                    live_snapshot=live_snapshot,
                    disconnect_challenge=(
                        str(payload.get("disconnect_challenge"))
                        if payload.get("disconnect_challenge") is not None
                        else None
                    ),
                )
                self._json(HTTPStatus.OK, result)
                return
            if path == "/api/e2e/disconnect":
                capture = self.server.config.evidence_capture
                if capture is None:
                    raise RemoteControlError("evidence capture disabled")
                if set(payload) - {"device_nonce", "disconnect_challenge"}:
                    raise RemoteControlError("unsupported evidence fields")
                result = capture.mark_disconnect(
                    login=login,
                    device_nonce=str(payload.get("device_nonce") or ""),
                    disconnect_challenge=str(payload.get("disconnect_challenge") or ""),
                )
                self._json(HTTPStatus.OK, result)
                return
            if set(payload) - {
                "recipient", "task_id", "subject", "body", "priority",
                "route_profile_id", "requested_provider_id", "requested_model_id",
                "requested_reasoning_mode",
            }:
                raise RemoteControlError("unsupported message fields")
            recipient = str(payload.get("recipient") or "").strip()
            task_id = str(payload.get("task_id") or "").strip()
            subject = str(payload.get("subject") or "").strip()
            body = str(payload.get("body") or "").strip()
            priority = str(payload.get("priority") or "normal").strip()
            if not SAFE_TASK.fullmatch(task_id):
                raise RemoteControlError("invalid task ID")
            if recipient != "*" and not SAFE_TASK.fullmatch(recipient):
                raise RemoteControlError("invalid recipient")
            if not subject or len(subject) > 500:
                raise RemoteControlError("invalid subject")
            if not body or len(body) > MAX_BODY_CHARS:
                raise RemoteControlError("invalid message body")
            if priority not in ALLOWED_PRIORITIES:
                raise RemoteControlError("invalid priority")
            for key in (
                "route_profile_id",
                "requested_provider_id",
                "requested_model_id",
                "requested_reasoning_mode",
            ):
                value = str(payload.get(key) or "").strip()
                if value and not SAFE_TASK.fullmatch(value):
                    raise RemoteControlError(f"invalid {key}")
            client = McpHumanClient(
                self.server.config.project_root,
                self.server.config.db_path,
                self.server.config.scope,
                agent_id=identity_agent_id(login),
                client_name="tailscale-web",
            )
            result = client.send_message(
                recipient=recipient,
                task_id=task_id,
                subject=subject,
                body=body,
                priority=priority,
                route_profile_id=payload.get("route_profile_id"),
                requested_provider_id=payload.get("requested_provider_id"),
                requested_model_id=payload.get("requested_model_id"),
                requested_reasoning_mode=payload.get("requested_reasoning_mode"),
            )
            capture = self.server.config.evidence_capture
            evidence_recorded: bool | None = None
            if capture is not None:
                try:
                    capture.record_message(task_id=task_id, result=result)
                    evidence_recorded = True
                except EvidenceError:
                    # The MCP audit-chain write is already committed. Never make
                    # the browser retry and duplicate the message because an
                    # optional evidence sidecar could not advance.
                    evidence_recorded = False
        except socket.timeout:
            self._json(HTTPStatus.REQUEST_TIMEOUT, {"error": "request body timed out"})
            return
        except (EvidenceError, json.JSONDecodeError, RemoteControlError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except (OSError, RuntimeError):
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "MCP message path unavailable"},
            )
            return
        response = {
            "status": "sent",
            "message_id": result.get("message_id"),
            "content_sha256": result.get("content_sha256"),
            "operator_identity_sha256": hashlib.sha256(
                login.encode("utf-8")
            ).hexdigest(),
        }
        if evidence_recorded is not None:
            response["evidence_recorded"] = evidence_recorded
        self._json(
            HTTPStatus.CREATED,
            response,
        )


def make_server(
    project_root: Path,
    db_path: Path,
    scope: str,
    host: str,
    port: int,
    allowed_logins: set[str] | frozenset[str],
    *,
    proxy_credential: str,
    csrf_token: str | None = None,
    public_origin: str,
    instance_id: str | None = None,
    evidence_run_id: str | None = None,
    evidence_minimum_gap_seconds: int = 10,
) -> RemoteControlServer:
    if not 0 <= int(port) <= 65535:
        raise RemoteControlError("remote control port must be between 0 and 65535")
    if not SAFE_TASK.fullmatch(scope):
        raise RemoteControlError("invalid coordination scope")
    normalized = frozenset(_normalize_login(item) for item in allowed_logins)
    if not normalized:
        raise RemoteControlError("at least one Tailscale login must be authorized")
    parsed_origin = urlsplit(str(public_origin or ""))
    if (
        parsed_origin.scheme.lower() != "https"
        or not parsed_origin.netloc
        or parsed_origin.username is not None
        or parsed_origin.password is not None
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise RemoteControlError("public origin must be an HTTPS authority")
    normalized_origin = f"https://{parsed_origin.netloc.lower()}"
    runtime_id = instance_id or f"remote-{secrets.token_hex(16)}"
    if not SAFE_TASK.fullmatch(runtime_id):
        raise RemoteControlError("invalid remote instance ID")
    evidence_capture = (
        MobileEvidenceCapture(
            project_root,
            db_path,
            run_id=evidence_run_id,
            scope=scope,
            public_origin=normalized_origin,
            instance_id=runtime_id,
            minimum_gap_seconds=evidence_minimum_gap_seconds,
        )
        if evidence_run_id
        else None
    )
    config = RemoteConfig(
        project_root=project_root.resolve(),
        db_path=db_path.resolve(),
        scope=scope,
        allowed_logins=normalized,
        proxy_credential_sha256=_proxy_credential_sha256(proxy_credential),
        csrf_token=csrf_token or secrets.token_urlsafe(32),
        public_origin=normalized_origin,
        instance_id=runtime_id,
        evidence_run_id=evidence_run_id,
        evidence_capture=evidence_capture,
    )
    return RemoteControlServer((host, port), config)


def run_remote(
    project_root: Path,
    db_path: Path,
    scope: str,
    host: str,
    port: int,
    allowed_logins: set[str],
    public_origin: str,
    instance_id: str | None = None,
    evidence_run_id: str | None = None,
    evidence_minimum_gap_seconds: int = 10,
    proxy_credential: str | None = None,
) -> int:
    if not 1 <= int(port) <= 65535:
        raise RemoteControlError("remote control port must be between 1 and 65535")
    if not allowed_logins:
        allowed_logins = {tailscale_self_login()}
    runtime_proxy_credential = proxy_credential or os.environ.get(
        PROXY_CREDENTIAL_ENV,
        "",
    )
    server = make_server(
        project_root,
        db_path,
        scope,
        host,
        port,
        allowed_logins,
        proxy_credential=runtime_proxy_credential,
        public_origin=public_origin,
        instance_id=instance_id,
        evidence_run_id=evidence_run_id,
        evidence_minimum_gap_seconds=evidence_minimum_gap_seconds,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the loopback-only PeerBridge mobile control plane.")
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--scope", default="default")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-login", action="append", default=[])
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--evidence-run-id")
    parser.add_argument("--evidence-minimum-gap-seconds", type=int, default=10)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    db = args.db.resolve() if args.db else root / ".peerbridge" / "peerbridge.sqlite3"
    try:
        return run_remote(
            root,
            db,
            args.scope,
            args.host,
            args.port,
            set(args.allow_login),
            args.public_origin,
            args.instance_id,
            args.evidence_run_id,
            args.evidence_minimum_gap_seconds,
        )
    except (OSError, RemoteControlError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"peerbridge-remote: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
