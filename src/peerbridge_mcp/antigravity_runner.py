"""Credential-safe adapter for Google Antigravity's local Agent API."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Mapping, Sequence

from . import __version__
from .openai_compatible_runner import (
    ConfigurationError,
    InferenceResult,
    ResourceUnavailableError,
    RunCancelledError,
    RunnerConfig,
    RunnerError,
)
from .secret_scan import contains_secret


RECEIPT_SCHEMA = "peerbridge.antigravity-agentapi-run.v1"
_MODEL_ALIASES = {
    "gemini-3.7-flash": "flash",
    "gemini-3.7-flash-high": "flash",
    "flash": "flash",
    "flash_lite": "flash_lite",
    "pro": "pro",
}
_POWERSHELL_BRIDGE = r"""
$ErrorActionPreference = 'Stop'
$requestText = [Console]::In.ReadToEnd()
$request = $requestText | ConvertFrom-Json -Depth 20
$agentApi = [string]$request.agentapi_path
$projectId = [string]$request.project_id
$arguments = @('agentapi') + @($request.arguments | ForEach-Object { [string]$_ })

function Get-AntigravityServer {
  $servers = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'language_server.exe' -and
    [string]$_.CommandLine -match '--override_ide_name\s+antigravity' -and
    [string]$_.CommandLine -match '--csrf_token\s+([^\s]+)'
  })
  if ($servers.Count -eq 0) { return $null }
  if ($servers.Count -ne 1) { throw 'Antigravity language server is ambiguous' }
  return $servers[0]
}

$server = Get-AntigravityServer
if ($null -eq $server) {
  $program = [string]$request.program_path
  if (-not (Test-Path -LiteralPath $program -PathType Leaf)) {
    throw 'Antigravity desktop program is unavailable'
  }
  Start-Process -FilePath $program -WindowStyle Hidden | Out-Null
  $deadline = [DateTime]::UtcNow.AddSeconds([double]$request.startup_timeout_seconds)
  do {
    Start-Sleep -Milliseconds 250
    $server = Get-AntigravityServer
  } while ($null -eq $server -and [DateTime]::UtcNow -lt $deadline)
  if ($null -eq $server) { throw 'Antigravity language server did not start' }
}

$match = [regex]::Match([string]$server.CommandLine, '--csrf_token\s+([^\s]+)')
if (-not $match.Success) { throw 'Antigravity CSRF token is unavailable' }
$csrf = $match.Groups[1].Value
$ports = @(Get-NetTCPConnection -State Listen -OwningProcess $server.ProcessId |
  Where-Object { $_.LocalAddress -eq '127.0.0.1' } |
  Select-Object -ExpandProperty LocalPort |
  Sort-Object -Unique)
if ($ports.Count -ne 2) { throw 'Antigravity listener count drift' }

$env:ANTIGRAVITY_LS_ADDRESS = "http://127.0.0.1:$($ports[-1])"
$env:ANTIGRAVITY_CSRF_TOKEN = $csrf
$env:ANTIGRAVITY_PROJECT_ID = $projectId
try {
  & $agentApi @arguments
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Remove-Item Env:ANTIGRAVITY_LS_ADDRESS -ErrorAction SilentlyContinue
  Remove-Item Env:ANTIGRAVITY_CSRF_TOKEN -ErrorAction SilentlyContinue
  Remove-Item Env:ANTIGRAVITY_PROJECT_ID -ErrorAction SilentlyContinue
  $csrf = $null
}
"""


def _strict_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise RunnerError("Antigravity payload is not strict JSON") from None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _default_agentapi() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    return local_app_data / "Programs/antigravity/resources/bin/language_server.exe"


def _default_program() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    return local_app_data / "Programs/antigravity/Antigravity.exe"


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        raise ResourceUnavailableError("PowerShell is required for Antigravity discovery")
    return executable


def _model_alias(model: str) -> str:
    alias = _MODEL_ALIASES.get(str(model).strip().lower())
    if alias is None:
        raise ConfigurationError("Antigravity model must be flash_lite, flash, or pro")
    return alias


def _message_text(messages: Sequence[Mapping[str, Any]]) -> str:
    sections: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user").strip().lower()
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            text_parts = []
            for item in content:
                if isinstance(item, Mapping) and item.get("type") == "text":
                    text_parts.append(str(item.get("text") or ""))
            text = "\n".join(part for part in text_parts if part)
        else:
            text = str(content or "")
        if text:
            sections.append(f"<{role.upper()}>\n{text}\n</{role.upper()}>")
    prompt = "\n\n".join(sections).strip()
    if not prompt:
        raise ConfigurationError("Antigravity prompt is empty")
    return prompt


def _reasoning_request(mode: str | None) -> str:
    requested = str(mode or "").strip()
    if not requested or requested.casefold() == "default":
        return ""
    return (
        f"Requested reasoning effort: {requested}. Use careful internal analysis before "
        "answering, but return only the requested final output and never expose hidden "
        "chain-of-thought.\n\n"
    )


@dataclass(frozen=True, repr=False)
class AntigravityRuntime:
    agentapi_path: Path
    program_path: Path
    project_id: str

    def __repr__(self) -> str:
        return (
            "AntigravityRuntime("
            f"agentapi_available={self.agentapi_path.is_file()!r}, "
            f"program_available={self.program_path.is_file()!r}, "
            f"project_id={self.project_id!r})"
        )


class AntigravityAgentApiRunner:
    """Run one Antigravity conversation without exporting its local credentials."""

    def __init__(
        self,
        config: RunnerConfig,
        *,
        project_id: str = "outside-of-project",
        agentapi_path: Path | None = None,
        program_path: Path | None = None,
        startup_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self.config = config
        self.runtime = AntigravityRuntime(
            agentapi_path=(agentapi_path or _default_agentapi()).resolve(),
            program_path=(program_path or _default_program()).resolve(),
            project_id=str(project_id).strip(),
        )
        if not self.runtime.project_id:
            raise ConfigurationError("Antigravity project ID is required")
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def _invoke(self, arguments: list[str]) -> dict[str, Any]:
        if not self.runtime.agentapi_path.is_file():
            raise ResourceUnavailableError("Antigravity Agent API launcher is unavailable")
        request = {
            "agentapi_path": str(self.runtime.agentapi_path),
            "program_path": str(self.runtime.program_path),
            "project_id": self.runtime.project_id,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "arguments": arguments,
        }
        try:
            completed = subprocess.run(
                [
                    _powershell(),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _POWERSHELL_BRIDGE,
                ],
                input=_strict_json(request),
                text=True,
                encoding="utf-8",
                errors="strict",
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ResourceUnavailableError("Antigravity Agent API timed out") from None
        if contains_secret(completed.stdout) or contains_secret(completed.stderr):
            raise RunnerError("Antigravity Agent API output contained credential material")
        if completed.returncode != 0:
            raise ResourceUnavailableError("Antigravity Agent API invocation failed")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise RunnerError("Antigravity Agent API returned invalid JSON") from None
        if not isinstance(result, dict):
            raise RunnerError("Antigravity Agent API response must be an object")
        if result.get("error"):
            raise ResourceUnavailableError("Antigravity Agent API reported an error")
        return result

    def _wait_for_response(self, conversation_id: str, prompt: str) -> tuple[str, Path]:
        transcript = (
            Path.home()
            / ".gemini/antigravity/brain"
            / conversation_id
            / ".system_generated/logs/transcript.jsonl"
        ).resolve()
        expected_root = (Path.home() / ".gemini/antigravity/brain").resolve()
        try:
            transcript.relative_to(expected_root)
        except ValueError:
            raise RunnerError("Antigravity transcript escaped its expected root") from None
        deadline = time.monotonic() + self.config.timeout_seconds
        last_signature: tuple[int, int] | None = None
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            if self._cancelled.is_set():
                raise RunCancelledError("Antigravity response wait was cancelled")
            if transcript.is_file():
                stat = transcript.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if signature != last_signature:
                    last_signature = signature
                    stable_since = time.monotonic()
                user_step_index: int | None = None
                model_messages: list[tuple[int, str]] = []
                for line in transcript.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        event.get("source") == "USER_EXPLICIT"
                        and event.get("status") == "DONE"
                        and isinstance(event.get("content"), str)
                        and prompt in event["content"]
                    ):
                        user_step_index = int(event.get("step_index", -1))
                    if (
                        event.get("source") == "MODEL"
                        and event.get("type") == "PLANNER_RESPONSE"
                        and event.get("status") == "DONE"
                        and isinstance(event.get("content"), str)
                        and event["content"].strip()
                        and not event.get("tool_calls")
                    ):
                        model_messages.append(
                            (int(event.get("step_index", -1)), event["content"].strip())
                        )
                if user_step_index is not None:
                    eligible = [
                        text for step_index, text in model_messages if step_index > user_step_index
                    ]
                    if eligible and time.monotonic() - stable_since >= 0.5:
                        return eligible[-1], transcript
            time.sleep(self.poll_interval_seconds)
        raise ResourceUnavailableError("Antigravity response transcript did not complete")

    def run(
        self,
        messages: list[dict[str, Any]],
        *,
        message_id: str | None = None,
    ) -> InferenceResult:
        self._cancelled.clear()
        prompt = _reasoning_request(self.config.reasoning_mode) + _message_text(messages)
        alias = _model_alias(self.config.model)
        title = f"PeerBridge {message_id or self.config.session_id}"[:120]
        result = self._invoke(
            [
                "new-conversation",
                f"--model={alias}",
                f"--title={title}",
                prompt,
            ]
        )
        conversation = ((result.get("response") or {}).get("newConversation") or {})
        conversation_id = str(conversation.get("conversationId") or "").strip()
        if not conversation_id:
            raise RunnerError("Antigravity conversation ID is missing")
        response, transcript = self._wait_for_response(conversation_id, prompt)
        if contains_secret(response):
            raise RunnerError("Antigravity response contained credential material")
        transcript_sha = _sha256_file(transcript)
        receipt_core = {
            "schema_version": RECEIPT_SCHEMA,
            "peerbridge_version": __version__,
            "provider_id": "google-antigravity",
            "route_class": "official",
            "requested_model": self.config.model,
            "requested_model_alias": alias,
            "actual_provider_model_id": None,
            "actual_model_identity_verified": False,
            # agentapi exposes model tiers but no reasoning-effort control or readback.
            # Keep the legacy field while making the verification boundary explicit.
            "reasoning_mode": self.config.reasoning_mode,
            "requested_reasoning_mode": self.config.reasoning_mode,
            "observed_reasoning_mode": None,
            "reasoning_mode_verified": False,
            "reasoning_mode_contract": (
                "prompt_requested_only_agentapi_has_no_effort_control_or_readback"
            ),
            "conversation_id": conversation_id,
            "prompt_sha256": _sha256_text(prompt),
            "response_sha256": _sha256_text(response),
            "transcript_bytes": transcript.stat().st_size,
            "transcript_sha256": transcript_sha,
            "quota_source": "google-antigravity-account",
            "gemini_api_key_used": False,
            "oauth_credential_read_by_peerbridge": False,
            "csrf_token_recorded": False,
            "credential_contents_recorded": False,
            "tool_calls_exposed_to_peerbridge": False,
        }
        receipt = dict(receipt_core)
        receipt["receipt_sha256"] = _sha256_text(_strict_json(receipt_core))
        return InferenceResult(
            assistant_message={"role": "assistant", "content": response},
            receipt=receipt,
        )


__all__ = [
    "AntigravityAgentApiRunner",
    "AntigravityRuntime",
    "RECEIPT_SCHEMA",
]
