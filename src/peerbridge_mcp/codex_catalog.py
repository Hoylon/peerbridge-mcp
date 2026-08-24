"""Read the local Codex model catalog without accessing credentials."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass

from .child_environment import build_local_child_environment
from .managed_agents import find_trusted_executable, official_agent_spec


class CodexCatalogError(RuntimeError):
    """A redacted local Codex catalog discovery error."""


@dataclass(frozen=True)
class CodexModel:
    model_id: str
    display_name: str
    default_reasoning_mode: str | None
    supported_reasoning_modes: tuple[str, ...]
    priority: int


@dataclass(frozen=True)
class CodexModelCatalog:
    models: tuple[CodexModel, ...]
    catalog_sha256: str


def _decode_output(payload: bytes) -> str:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in payload[:80]:
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                return payload.decode(encoding)
            except UnicodeDecodeError:
                continue
    return payload.decode("utf-8-sig", errors="strict")


def parse_codex_model_catalog(payload: bytes | str) -> CodexModelCatalog:
    """Parse visible models from ``codex debug models`` output."""

    text = _decode_output(payload) if isinstance(payload, bytes) else payload
    try:
        raw = json.loads(text)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CodexCatalogError("Codex returned an invalid model catalog") from exc
    entries = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise CodexCatalogError("Codex model catalog is missing its model list")

    models: list[CodexModel] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue
        model_id = str(entry.get("slug") or "").strip()
        if not model_id or model_id in seen:
            continue
        reasoning: list[str] = []
        for level in entry.get("supported_reasoning_levels") or ():
            value = str(level.get("effort") or "").strip() if isinstance(level, dict) else ""
            if value and value not in reasoning:
                reasoning.append(value)
        default_reasoning = str(entry.get("default_reasoning_level") or "").strip() or None
        if default_reasoning and default_reasoning not in reasoning:
            reasoning.insert(0, default_reasoning)
        try:
            priority = int(entry.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        seen.add(model_id)
        models.append(
            CodexModel(
                model_id=model_id,
                display_name=str(entry.get("display_name") or model_id).strip() or model_id,
                default_reasoning_mode=default_reasoning,
                supported_reasoning_modes=tuple(reasoning),
                priority=priority,
            )
        )
    if not models:
        raise CodexCatalogError("Codex model catalog contains no visible models")
    models.sort(key=lambda item: (item.priority, item.model_id))
    bound = [
        {
            "model_id": item.model_id,
            "display_name": item.display_name,
            "default_reasoning_mode": item.default_reasoning_mode,
            "supported_reasoning_modes": list(item.supported_reasoning_modes),
            "priority": item.priority,
        }
        for item in models
    ]
    digest = hashlib.sha256(
        json.dumps(bound, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return CodexModelCatalog(models=tuple(models), catalog_sha256=digest)


def discover_codex_model_catalog(*, timeout: int = 20) -> CodexModelCatalog:
    """Query the installed official Codex client for its current visible models."""

    executable = find_trusted_executable(official_agent_spec("codex"))
    if executable is None:
        raise CodexCatalogError("Codex CLI is not installed in a trusted location")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            [str(executable), "debug", "models"],
            capture_output=True,
            env=build_local_child_environment(),
            cwd=executable.parent,
            timeout=timeout,
            creationflags=flags,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CodexCatalogError("Codex model catalog query timed out") from None
    except OSError:
        raise CodexCatalogError("Codex model catalog query could not start") from None
    if completed.returncode:
        raise CodexCatalogError(
            f"Codex model catalog query failed ({completed.returncode})"
        )
    return parse_codex_model_catalog(completed.stdout)
