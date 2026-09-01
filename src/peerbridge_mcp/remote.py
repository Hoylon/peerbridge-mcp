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
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .bridge import DEFAULT_ROOM_ID
from .secret_scan import redact_secrets
from .child_environment import build_local_child_environment
from .http_limits import BoundedThreadingHTTPServer
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


def _tailscale_executable() -> Path:
    candidates = []
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        root = str(os.environ.get(key) or "").strip()
        if root:
            candidates.append(Path(root) / "Tailscale" / "tailscale.exe")
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except OSError:
            continue
        attributes = int(getattr(info, "st_file_attributes", 0))
        if (
            stat.S_ISREG(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and not attributes & reparse_flag
        ):
            return candidate.resolve()
    raise RemoteControlError("trusted Tailscale executable is unavailable")


def tailscale_self_login() -> str:
    """Return the local tailnet owner without persisting or printing it."""
    executable = _tailscale_executable()
    completed = subprocess.run(
        [str(executable), "status", "--json"],
        capture_output=True,
        cwd=executable.parent,
        env=build_local_child_environment(),
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
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
    reader: BridgeReader,
    scope: str,
    instance_id: str | None = None,
    room_id: str = DEFAULT_ROOM_ID,
) -> dict[str, Any]:
    snapshot = reader.snapshot(limit=500, scope=scope)
    room = reader.room_view(
        scope=scope,
        requested_room_id=room_id,
        limit=200,
    )
    now = datetime.now(timezone.utc).timestamp()
    presence = snapshot.presence
    messages = room["messages"]
    tasks = snapshot.tasks
    routes = snapshot.route_profiles
    providers = snapshot.provider_connections
    message_ids = {str(row.get("message_id") or "") for row in messages}
    dispatches = tuple(
        row
        for row in snapshot.message_dispatches
        if str(row.get("message_id") or "") in message_ids
    )
    automation = room.get("automation") or {}
    discussion = automation.get("active_discussion")
    payload = {
        "generated_utc": snapshot.generated_utc,
        "scope": scope,
        "instance_id": instance_id,
        "room_id": str(room.get("room_id") or DEFAULT_ROOM_ID),
        "counts": {
            "agents": snapshot.table_counts["agent_presence"],
            "messages": snapshot.table_counts["messages"],
            "tasks": snapshot.table_counts["tasks"],
            "routes": snapshot.table_counts["route_profiles"],
            "providers": snapshot.table_counts["provider_connections"],
            "dispatches": snapshot.table_counts["message_dispatches"],
        },
        "rooms": [
            {
                "room_id": str(row.get("room_id") or ""),
                "name": _redact(row.get("name")),
                "active_member_count": int(row.get("active_member_count") or 0),
                "message_count": int(row.get("message_count") or 0),
                "updated_utc": str(row.get("updated_utc") or ""),
            }
            for row in room.get("rooms") or ()
        ],
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
        "dispatches": [
            {
                "message_id": str(row.get("message_id") or ""),
                "agent_id": str(row.get("agent_id") or ""),
                "status": str(row.get("status") or ""),
                "attempt_count": int(row.get("attempt_count") or 0),
                "updated_utc": str(row.get("updated_utc") or ""),
                "completed_utc": str(row.get("completed_utc") or ""),
                "reply_message_id": str(row.get("reply_message_id") or ""),
                "error_code": str(row.get("error_code") or ""),
                "dispatch_sha256": str(row.get("dispatch_sha256") or ""),
            }
            for row in dispatches
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
        "automation": {
            "mode": str(automation.get("mode") or "off"),
            "max_rounds": int(automation.get("max_rounds") or 0),
            "max_messages": int(automation.get("max_messages") or 0),
            "active_discussion": (
                {
                    "discussion_id": str(discussion.get("discussion_id") or ""),
                    "task_id": str(discussion.get("task_id") or ""),
                    "subject": _redact(discussion.get("subject")),
                    "status": str(discussion.get("status") or ""),
                    "current_round": int(discussion.get("current_round") or 0),
                    "max_rounds": int(discussion.get("max_rounds") or 0),
                    "message_count": int(discussion.get("message_count") or 0),
                    "stop_reason": _redact(discussion.get("stop_reason")),
                    "updated_utc": str(discussion.get("updated_utc") or ""),
                }
                if isinstance(discussion, dict)
                else None
            ),
        },
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
    csrf_json = json.dumps(csrf_token)
    proxy_header = json.dumps(PROXY_AUTH_HEADER)
    evidence = "true" if evidence_enabled else "false"
    markup = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PeerBridge Remote</title><style>
:root{--bg:#090a0c;--surface:#121418;--surface-2:#191c21;--line:#2d323a;--text:#f2f4f7;--muted:#969da8;--blue:#3b82f6;--blue-2:#2563eb;--green:#34c77b;--amber:#f3b64b;--red:#ef5a67;--header:64px;--radius:8px}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font:15px system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:0}body{overflow-x:hidden}
button,input,select,textarea{font:inherit;letter-spacing:0}button{cursor:pointer}.icon-button{width:44px;height:44px;padding:0;border:1px solid var(--line);border-radius:50%;background:var(--surface-2);color:var(--text);font-size:21px}.icon-button:disabled,.command:disabled{cursor:not-allowed;opacity:.45}
.topbar{height:var(--header);position:sticky;top:0;z-index:20;display:grid;grid-template-columns:260px 1fr 300px;align-items:center;border-bottom:1px solid var(--line);background:#0b0c0f;padding:0 14px}.brand{display:flex;align-items:center;gap:10px;font-weight:700}.brand-mark{display:grid;place-items:center;width:32px;height:32px;border:1px solid #526071;border-radius:7px;color:#9cc1ff}.brand small{display:block;color:var(--muted);font-size:11px;font-weight:500}.menu{display:none}
.mode-tabs,.view-tabs{display:flex;gap:4px}.mode-tabs{justify-self:center;padding:4px;border:1px solid var(--line);border-radius:8px;background:var(--surface)}.mode-tabs button,.view-tabs button{min-height:38px;border:0;border-radius:6px;background:transparent;color:var(--muted);padding:0 18px}.mode-tabs button.active,.view-tabs button.active{background:#343942;color:#fff}.health{justify-self:end;display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}.health-dot{width:8px;height:8px;border-radius:50%;background:var(--amber)}.health.online .health-dot{background:var(--green)}
.shell{min-height:calc(100vh - var(--header));display:grid;grid-template-columns:260px minmax(360px,1fr) 300px}.rail,.inspector{background:#0d0f12}.rail{border-right:1px solid var(--line);padding:14px 10px;overflow:auto}.inspector{border-left:1px solid var(--line);padding:16px;overflow:auto}.workspace{min-width:0;display:flex;flex-direction:column;position:relative}.rail-heading,.section-label{margin:16px 10px 8px;color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase}.rail-command,.room-button{width:100%;min-height:44px;border:0;border-radius:6px;background:transparent;color:var(--text);text-align:left;padding:9px 10px}.rail-command:hover,.room-button:hover,.room-button.active{background:var(--surface-2)}.room-button{display:grid;grid-template-columns:1fr auto;gap:8px}.room-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.room-count{color:var(--muted);font-size:12px}.transport-note{margin:18px 10px 0;border-top:1px solid var(--line);padding-top:14px;color:var(--muted);font-size:12px;line-height:1.5}.transport-note strong{display:block;color:var(--text);margin-bottom:4px}
.workspace-head{min-height:62px;display:flex;align-items:center;justify-content:space-between;gap:16px;border-bottom:1px solid var(--line);padding:10px 18px}.workspace-title{min-width:0}.workspace-title strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.workspace-title span{color:var(--muted);font-size:12px}.view-tabs{overflow:auto}.view-tabs button{white-space:nowrap;padding:0 12px}
.discussion{margin:10px 18px 0;padding:10px 12px;border:1px solid #52452d;border-radius:6px;background:#19160f;display:flex;align-items:center;gap:10px}.discussion[hidden]{display:none}.discussion-copy{min-width:0;flex:1}.discussion-copy strong,.discussion-copy span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.discussion-copy span{color:var(--muted);font-size:12px}.discussion-actions{display:flex;gap:6px}.command{min-height:38px;border:1px solid var(--line);border-radius:6px;background:var(--surface-2);color:var(--text);padding:0 12px}.command.stop{border-color:#6a3038;color:#ff9da6}
.content{flex:1;min-height:300px;padding:0 18px 18px;overflow:auto}.row{border-bottom:1px solid var(--line);padding:15px 0}.meta{display:flex;flex-wrap:wrap;gap:5px;color:var(--muted);font-size:12px;margin-bottom:7px}.subject{font-weight:700;margin-bottom:6px;overflow-wrap:anywhere}.body{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.55}.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}.chip{display:inline-flex;align-items:center;min-height:24px;padding:2px 7px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font:11px ui-monospace,Consolas,monospace}.chip.completed{border-color:#24593e;color:#7ce4aa}.chip.running,.chip.claimed{border-color:#315b94;color:#8ebeff}.chip.failed{border-color:#713039;color:#ff98a2}.chip.retryable,.chip.pending{border-color:#745a27;color:#ffd27d}.empty{min-height:220px;display:grid;place-items:center;color:var(--muted);text-align:center}
.composer{position:sticky;bottom:0;z-index:8;margin:0 18px 14px;border:1px solid var(--line);border-radius:8px;background:var(--surface);padding:10px}.composer textarea{display:block;width:100%;min-height:64px;max-height:220px;resize:vertical;border:0;outline:0;background:transparent;color:var(--text);padding:4px}.composer textarea::placeholder{color:#757b84}.composer-bar{display:flex;align-items:center;gap:8px;margin-top:8px}.composer-bar details{flex:1}.composer-bar summary{color:var(--muted);font-size:12px;cursor:pointer}.advanced{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding-top:10px}.advanced label{color:var(--muted);font-size:11px}.advanced input,.advanced select{width:100%;min-height:38px;margin-top:4px;border:1px solid var(--line);border-radius:6px;background:#0c0e11;color:var(--text);padding:7px}.send{min-width:44px;height:44px;border:0;border-radius:50%;background:var(--blue);color:#fff;font-size:19px;font-weight:700}.send:hover{background:var(--blue-2)}.send-status{min-height:18px;color:var(--muted);font-size:11px;padding:4px 2px 0}
.inspector h2{font-size:14px;margin:0 0 12px}.metric-list{display:grid;grid-template-columns:1fr auto;gap:8px;border-bottom:1px solid var(--line);padding-bottom:14px}.metric-list span{color:var(--muted)}.metric-list strong{font:12px ui-monospace,Consolas,monospace}.evidence{display:none;border-top:1px solid var(--line);margin-top:16px;padding-top:14px}.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}.evidence .command{width:100%}.mobile-backdrop{display:none}
@media(max-width:1100px){.topbar{grid-template-columns:230px 1fr 180px}.shell{grid-template-columns:230px minmax(360px,1fr)}.inspector{display:none}}
@media(max-width:760px){:root{--header:70px}.topbar{grid-template-columns:48px 1fr 48px;height:var(--header);padding:calc(env(safe-area-inset-top) + 6px) 10px 6px}.brand{display:none}.menu{display:block}.mode-tabs{justify-self:center}.mode-tabs button{padding:0 16px}.health{font-size:0}.shell{display:block;min-height:calc(100vh - var(--header))}.rail{position:fixed;z-index:40;top:0;bottom:0;left:0;width:min(86vw,330px);padding:calc(env(safe-area-inset-top) + 76px) 12px calc(env(safe-area-inset-bottom) + 16px);transform:translateX(-105%);transition:transform .18s ease;border-right:1px solid var(--line);box-shadow:10px 0 30px #000}.rail.open{transform:translateX(0)}.mobile-backdrop{position:fixed;z-index:35;inset:0;background:#000a}.mobile-backdrop.open{display:block}.workspace-head{min-height:56px;padding:8px 12px}.workspace-title span{display:none}.view-tabs{max-width:100%}.view-tabs button{min-width:44px;padding:0 10px}.discussion{margin:8px 10px 0;align-items:flex-start}.discussion-actions{flex-direction:column}.content{padding:0 12px 154px}.row{padding:14px 0}.composer{position:fixed;left:10px;right:10px;bottom:calc(env(safe-area-inset-bottom) + 8px);z-index:15;margin:0;padding:9px;box-shadow:0 -8px 28px #0009}.composer textarea{min-height:46px;max-height:130px}.advanced{grid-template-columns:1fr}.transport-note{padding-bottom:20px}}
</style></head><body>
<header class="topbar"><button id="menuToggle" class="icon-button menu" type="button" aria-label="開啟房間選單" title="房間選單">☰</button><div class="brand"><span class="brand-mark">PB</span><span>PeerBridge<small>Remote control room</small></span></div><div class="mode-tabs" role="tablist"><button type="button" data-tab="messages" class="active">對話</button><button type="button" data-tab="tasks">工作</button></div><div id="health" class="health"><span class="health-dot"></span><span id="healthText">連線中</span></div></header>
<div id="backdrop" class="mobile-backdrop"></div><div class="shell"><nav id="rail" class="rail" aria-label="房間與遠端功能"><button class="rail-command" type="button" data-tab="messages">＋ 新對話</button><button class="rail-command" type="button" data-tab="tasks">工作佇列</button><button class="rail-command" type="button" data-tab="agents">Agent</button><button class="rail-command" type="button" data-tab="activity">工具活動</button><div class="rail-heading">房間</div><div id="rooms"></div><div class="transport-note"><strong>生產遠端</strong>Tailscale Serve · 身分與獨立憑證雙重驗證<strong style="margin-top:10px">實驗傳輸</strong>Tailcat · CLI 轉發／檔案／SSH／SOCKS5，個別啟用</div></nav>
<main class="workspace"><div class="workspace-head"><div class="workspace-title"><strong id="roomTitle">PeerBridge</strong><span id="roomMeta">讀取房間狀態</span></div><div class="view-tabs"><button type="button" data-tab="messages" class="active" title="對話">對話</button><button type="button" data-tab="tasks" title="工作">工作</button><button type="button" data-tab="agents" title="Agent">Agent</button><button type="button" data-tab="activity" title="工具活動">活動</button></div></div>
<section id="discussionBar" class="discussion" hidden><div class="discussion-copy"><strong id="discussionTitle"></strong><span id="discussionMeta"></span></div><div class="discussion-actions"><button id="discussionPrimary" class="command" type="button"></button><button id="discussionStop" class="command stop" type="button">停止</button></div></section><section id="content" class="content"><div class="empty">正在載入房間</div></section>
<form id="composer" class="composer"><textarea id="body" maxlength="20000" placeholder="向目前房間或 Agent 發送訊息" aria-label="訊息內容"></textarea><div class="composer-bar"><details><summary>路由與訊息設定</summary><div class="advanced"><label>收件者<input id="recipient" value="*" maxlength="200"></label><label>優先度<select id="priority"><option>normal</option><option>high</option><option>critical</option><option>low</option></select></label><label>Task ID<input id="task" value="human-remote" maxlength="200"></label><label>主旨<input id="subject" value="HUMAN INTERVENTION" maxlength="500"></label></div></details><button id="send" class="send" type="submit" aria-label="發送" title="發送">↑</button></div><div id="sendStatus" class="send-status"></div></form></main>
<aside class="inspector"><h2>房間狀態</h2><div class="metric-list"><span>Agent</span><strong id="agentCount">0</strong><span>訊息</span><strong id="messageCount">0</strong><span>工作</span><strong id="taskCount">0</strong><span>活動</span><strong id="dispatchCount">0</strong></div><div id="evidencePanel" class="evidence"><h2>實機重連證據</h2><div id="evidenceStatus" class="meta">讀取中</div><div class="evidence-grid"><button id="evidenceInitial" class="command" type="button">初次連線</button><button id="evidenceDisconnect" class="command" type="button">標記斷線</button></div><button id="evidenceReconnect" class="command" type="button" style="margin-top:7px">重新連線</button></div></aside></div>
<script>
const CSRF=__CSRF__,EVIDENCE=__EVIDENCE__,PROXY_HEADER=__PROXY_HEADER__;let state=null,tab='messages',evidenceState=null,selectedRoom='lobby';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const accessKey='peerbridge.remote.access.v1';let linkProof='';try{let fragment=location.hash.slice(1);if(fragment){sessionStorage.setItem(accessKey,fragment);history.replaceState(null,'',location.pathname+location.search)}linkProof=sessionStorage.getItem(accessKey)||''}catch(_e){linkProof=''}
function authHeaders(extra={}){if(!linkProof)throw Error('private access proof missing');return Object.assign({[PROXY_HEADER]:linkProof},extra)}
function randomNonce(){let b=new Uint8Array(32);crypto.getRandomValues(b);return Array.from(b,x=>x.toString(16).padStart(2,'0')).join('')}
const deviceKey='peerbridge.mobile.device.v1';let deviceNonce=localStorage.getItem(deviceKey);if(!deviceNonce){deviceNonce=randomNonce();localStorage.setItem(deviceKey,deviceNonce)}const sessionNonce=randomNonce();
function dispatchChips(messageId){return (state.dispatches||[]).filter(x=>x.message_id===messageId).map(x=>`<span class="chip ${esc(x.status)}">${esc(x.agent_id)} · ${esc(x.status)}${x.attempt_count?` · #${x.attempt_count}`:''}</span>`).join('')}
function messageRows(){return (state.messages||[]).map(x=>`<article class="row"><div class="meta"><span>${esc(x.created_utc)}</span><span>${esc(x.sender)} → ${esc(x.recipient)}</span><span>${esc(x.priority)}</span></div><div class="subject">${esc(x.subject)}</div><div class="body">${esc(x.body)}</div><div class="chips">${dispatchChips(x.message_id)}</div></article>`)}
function taskRows(){return (state.tasks||[]).map(x=>`<article class="row"><div class="meta"><span>${esc(x.status)}</span><span>${esc(x.claimed_by||'未領取')}</span><span>${esc(x.approval_mode)}</span></div><div class="subject">${esc(x.task_id)}</div><div class="body">${esc(x.summary)}</div></article>`)}
function agentRows(){return (state.agents||[]).map(x=>`<article class="row"><div class="meta"><span>${x.online?'ONLINE':'OFFLINE'}</span><span>${esc(x.provider_id)}</span><span>${esc(x.model_id)}</span></div><div class="subject">${esc(x.agent_id)}</div><div class="body">${esc(x.client_name)} · ${esc(x.reasoning_mode)}</div></article>`)}
function activityRows(){return (state.dispatches||[]).map(x=>`<article class="row"><div class="meta"><span>${esc(x.updated_utc)}</span><span>嘗試 ${Number(x.attempt_count||0)}</span></div><div class="subject">${esc(x.agent_id)}</div><div class="chips"><span class="chip ${esc(x.status)}">${esc(x.status)}</span>${x.error_code?`<span class="chip failed">${esc(x.error_code)}</span>`:''}</div></article>`)}
function renderRooms(){let rooms=document.getElementById('rooms');rooms.innerHTML=(state.rooms||[]).map(x=>`<button class="room-button ${x.room_id===state.room_id?'active':''}" type="button" data-room="${esc(x.room_id)}"><span class="room-name">${esc(x.name||x.room_id)}</span><span class="room-count">${Number(x.message_count||0)}</span></button>`).join('')||'<div class="transport-note">沒有可見房間</div>';rooms.querySelectorAll('[data-room]').forEach(b=>b.onclick=()=>{selectedRoom=b.dataset.room||'lobby';closeRail();refresh()})}
function renderDiscussion(){let d=state.automation?.active_discussion,bar=document.getElementById('discussionBar');if(!d){bar.hidden=true;return}bar.hidden=false;document.getElementById('discussionTitle').textContent=d.subject||d.task_id||'協作進行中';document.getElementById('discussionMeta').textContent=`${d.status} · 回合 ${d.current_round}/${d.max_rounds} · ${d.message_count} 訊息`;let primary=document.getElementById('discussionPrimary');if(d.status==='active'){primary.textContent='暫停';primary.dataset.action='pause'}else if(d.status==='paused'){primary.textContent='恢復';primary.dataset.action='resume'}else{primary.textContent='繼續';primary.dataset.action='continue'}primary.disabled=!['active','paused','waiting_human'].includes(d.status);document.getElementById('discussionStop').disabled=!['active','paused','waiting_human'].includes(d.status)}
function render(){if(!state)return;let rows=tab==='tasks'?taskRows():tab==='agents'?agentRows():tab==='activity'?activityRows():messageRows();document.getElementById('content').innerHTML=rows.join('')||'<div class="empty">這個檢視暫時沒有資料</div>';let room=(state.rooms||[]).find(x=>x.room_id===state.room_id),title=room?.name||state.room_id||'PeerBridge',titleNode=document.getElementById('roomTitle');titleNode.textContent=title;titleNode.title=title;document.getElementById('roomMeta').textContent=`${Number(room?.active_member_count||0)} Agent · ${Number(room?.message_count||0)} 訊息`;document.getElementById('agentCount').textContent=state.counts?.agents||0;document.getElementById('messageCount').textContent=state.counts?.messages||0;document.getElementById('taskCount').textContent=state.counts?.tasks||0;document.getElementById('dispatchCount').textContent=state.counts?.dispatches||0;renderRooms();renderDiscussion()}
function setTab(next){tab=next;document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x.dataset.tab===tab));render();closeRail()}
function closeRail(){document.getElementById('rail').classList.remove('open');document.getElementById('backdrop').classList.remove('open')}
async function refresh(){let health=document.getElementById('health'),label=document.getElementById('healthText');try{let r=await fetch('/api/snapshot?room_id='+encodeURIComponent(selectedRoom),{cache:'no-store',headers:authHeaders()});let j=await r.json();if(!r.ok)throw Error(j.error||r.status);state=j;selectedRoom=state.room_id||selectedRoom;render();health.classList.add('online');label.textContent='ONLINE'}catch(e){health.classList.remove('online');label.textContent=linkProof?'RECONNECTING':'ACCESS LINK REQUIRED'}}
async function post(path,payload){let r=await fetch(path,{method:'POST',headers:authHeaders({'Content-Type':'application/json','X-PeerBridge-CSRF':CSRF}),body:JSON.stringify(payload)});let j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}
async function controlDiscussion(action){let d=state?.automation?.active_discussion;if(!d)return;let primary=document.getElementById('discussionPrimary'),stop=document.getElementById('discussionStop');primary.disabled=stop.disabled=true;try{await post('/api/discussion/control',{discussion_id:d.discussion_id,action,extra_rounds:2});await refresh()}catch(e){document.getElementById('sendStatus').textContent='控制失敗：'+e.message}finally{primary.disabled=stop.disabled=false}}
async function evidenceRefresh(){if(!EVIDENCE)return;let r=await fetch('/api/e2e/status',{cache:'no-store',headers:authHeaders()});if(!r.ok)throw Error(r.status);evidenceState=await r.json();document.getElementById('evidencePanel').style.display='block';document.getElementById('evidenceStatus').textContent=evidenceState.status+' · '+(evidenceState.complete?'COMPLETE':'INCOMPLETE');if(evidenceState.expected_task_id)document.getElementById('task').value=evidenceState.expected_task_id}
async function evidencePost(path,payload){let j=await post(path,payload);await evidenceRefresh();return j}
function sessionPayload(phase){if(!state)throw Error('snapshot unavailable');return{phase,device_nonce:deviceNonce,session_nonce:sessionNonce,viewport:{width:innerWidth,height:innerHeight,max_touch_points:navigator.maxTouchPoints||0},snapshot_signature:state.snapshot_signature,disconnect_challenge:localStorage.getItem('peerbridge.mobile.disconnect.v1')}}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>setTab(b.dataset.tab));document.getElementById('menuToggle').onclick=()=>{document.getElementById('rail').classList.add('open');document.getElementById('backdrop').classList.add('open')};document.getElementById('backdrop').onclick=closeRail;
document.getElementById('composer').onsubmit=async e=>{e.preventDefault();let b=document.getElementById('send'),s=document.getElementById('sendStatus'),body=document.getElementById('body');if(!body.value.trim())return;b.disabled=true;s.textContent='發送中';try{let j=await post('/api/message',{recipient:document.getElementById('recipient').value,task_id:document.getElementById('task').value,subject:document.getElementById('subject').value,body:body.value,priority:document.getElementById('priority').value});s.textContent='已寫入審計鏈 '+String(j.content_sha256||'').slice(0,12);body.value='';await refresh()}catch(err){s.textContent='失敗：'+err.message}finally{b.disabled=false}};
document.getElementById('discussionPrimary').onclick=e=>controlDiscussion(e.currentTarget.dataset.action);document.getElementById('discussionStop').onclick=()=>controlDiscussion('stop');
document.getElementById('evidenceInitial').onclick=async()=>{try{await refresh();let j=await evidencePost('/api/e2e/session',sessionPayload('initial'));if(j.disconnect_challenge)localStorage.setItem('peerbridge.mobile.disconnect.v1',j.disconnect_challenge)}catch(e){document.getElementById('evidenceStatus').textContent='失敗：'+e.message}};
document.getElementById('evidenceDisconnect').onclick=async()=>{try{await evidencePost('/api/e2e/disconnect',{device_nonce:deviceNonce,disconnect_challenge:localStorage.getItem('peerbridge.mobile.disconnect.v1')})}catch(e){document.getElementById('evidenceStatus').textContent='失敗：'+e.message}};
document.getElementById('evidenceReconnect').onclick=async()=>{try{await refresh();await evidencePost('/api/e2e/session',sessionPayload('reconnect'))}catch(e){document.getElementById('evidenceStatus').textContent='失敗：'+e.message}};
refresh().then(evidenceRefresh).catch(()=>{});setInterval(refresh,3000);
</script></body></html>"""
    markup = (
        markup.replace("__CSRF__", csrf_json)
        .replace("__EVIDENCE__", evidence)
        .replace("__PROXY_HEADER__", proxy_header)
    )
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


class RemoteControlServer(BoundedThreadingHTTPServer):
    daemon_threads = True
    # Windows must keep exclusive ownership because SO_REUSEADDR can let two
    # processes bind the same loopback port. POSIX needs SO_REUSEADDR to permit
    # a clean restart while prior client connections remain in TIME_WAIT.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], config: RemoteConfig) -> None:
        if not _is_loopback(address[0]):
            raise RemoteControlError("remote control backend must bind to loopback")
        self.config = config
        self.reader = BridgeReader(config.db_path)
        self._write_lock = threading.Lock()
        self._write_times: dict[str, collections.deque[float]] = {}
        super().__init__(address, RemoteHandler)

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
        parsed = urlsplit(self.path)
        path = parsed.path
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
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"room_id"}:
                    raise RemoteControlError("unsupported snapshot query")
                requested_rooms = query.get("room_id", [DEFAULT_ROOM_ID])
                if len(requested_rooms) != 1:
                    raise RemoteControlError("one room ID is required")
                room_id = str(requested_rooms[0] or DEFAULT_ROOM_ID)
                if not SAFE_TASK.fullmatch(room_id):
                    raise RemoteControlError("invalid room ID")
                payload = _snapshot_payload(
                    self.server.reader,
                    self.server.config.scope,
                    self.server.config.instance_id,
                    room_id,
                )
            except RemoteControlError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
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
        if path not in {
            "/api/message",
            "/api/discussion/control",
            "/api/e2e/session",
            "/api/e2e/disconnect",
        }:
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
            if path == "/api/discussion/control":
                if set(payload) - {"discussion_id", "action", "extra_rounds"}:
                    raise RemoteControlError("unsupported discussion fields")
                discussion_id = str(payload.get("discussion_id") or "").strip()
                action = str(payload.get("action") or "").strip().lower()
                if not SAFE_TASK.fullmatch(discussion_id):
                    raise RemoteControlError("invalid discussion ID")
                if action not in {"pause", "resume", "continue", "stop"}:
                    raise RemoteControlError("invalid discussion action")
                try:
                    extra_rounds = int(payload.get("extra_rounds") or 2)
                except (TypeError, ValueError) as exc:
                    raise RemoteControlError("invalid extra rounds") from exc
                if not 1 <= extra_rounds <= 20:
                    raise RemoteControlError("extra rounds must be 1..20")
                client = McpHumanClient(
                    self.server.config.project_root,
                    self.server.config.db_path,
                    self.server.config.scope,
                    agent_id=identity_agent_id(login),
                    client_name="tailscale-web",
                )
                result = client.control_discussion(
                    discussion_id=discussion_id,
                    action=action,
                    extra_rounds=extra_rounds,
                )
                self._json(
                    HTTPStatus.OK,
                    {
                        "status": str(result.get("status") or action),
                        "discussion_id": discussion_id,
                        "discussion_sha256": str(
                            result.get("discussion_sha256") or ""
                        ),
                    },
                )
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
    try:
        parsed_port = parsed_origin.port
    except ValueError as exc:
        raise RemoteControlError("public origin has an invalid port") from exc
    if (
        parsed_origin.scheme.lower() != "https"
        or not parsed_origin.netloc
        or parsed_origin.username is not None
        or parsed_origin.password is not None
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
        or parsed_origin.hostname is None
        or not parsed_origin.hostname.lower().endswith(".ts.net")
        or parsed_port is not None
    ):
        raise RemoteControlError(
            "public origin must be a no-port HTTPS Tailscale .ts.net authority"
        )
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
