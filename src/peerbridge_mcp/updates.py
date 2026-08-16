"""Read-only GitHub Release metadata checks; never downloads or executes updates."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from . import __version__


RELEASE_API = "https://api.github.com/repos/oscarho200407-hue/peerbridge-mcp/releases?per_page=20"
MAX_RELEASE_RESPONSE_BYTES = 256 * 1024
_VERSION = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:(?:-(?P<sem_label>alpha|beta|rc)(?:[.-]?(?P<sem_number>\d+))?)|"
    r"(?P<pep_label>a|b|rc)(?P<pep_number>\d+))?$",
    re.IGNORECASE,
)


class UpdateCheckError(RuntimeError):
    """Release metadata could not be safely validated."""


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str
    update_available: bool
    prerelease: bool
    release_url: str
    release_name: str
    published_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _version_key(value: str) -> tuple[int, int, int, int, int]:
    match = _VERSION.fullmatch(str(value).strip())
    if not match:
        raise UpdateCheckError("release version is not valid semantic version metadata")
    label = (match.group("sem_label") or match.group("pep_label") or "").lower()
    number_text = match.group("sem_number") or match.group("pep_number") or "0"
    stage = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "rc": 2, "": 3}[label]
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        stage,
        int(number_text),
    )


def _is_prerelease(value: str) -> bool:
    return _version_key(value)[3] < 3


def _validated_release_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise UpdateCheckError("release URL is not an official HTTPS GitHub URL")
    if parsed.port is not None or parsed.query or parsed.fragment:
        raise UpdateCheckError("release URL must not contain a port, query, or fragment")
    expected_prefix = "/oscarho200407-hue/peerbridge-mcp/releases/"
    if not parsed.path.startswith(expected_prefix) or parsed.username or parsed.password:
        raise UpdateCheckError("release URL does not belong to the PeerBridge repository")
    return url


def check_for_updates(
    *,
    current_version: str = __version__,
    timeout_seconds: float = 10.0,
    opener: Any | None = None,
) -> UpdateCheckResult:
    request = urllib.request.Request(
        RELEASE_API,
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"PeerBridge/{current_version}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    client = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with client.open(request, timeout=max(1.0, min(float(timeout_seconds), 30.0))) as response:
            status = int(getattr(response, "status", 0))
            body = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise UpdateCheckError("GitHub release metadata is currently unavailable") from exc
    if status != 200 or len(body) > MAX_RELEASE_RESPONSE_BYTES:
        raise UpdateCheckError("GitHub release metadata response is invalid")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateCheckError("GitHub release metadata is not valid JSON") from exc
    current = str(current_version).strip().removeprefix("v")
    current_key = _version_key(current)
    if not isinstance(payload, list):
        raise UpdateCheckError("GitHub release metadata must contain a release list")
    allow_prerelease = _is_prerelease(current)
    candidates: list[tuple[tuple[int, int, int, int, int], dict[str, Any], str, str]] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("draft") is not False:
            continue
        latest = str(item.get("tag_name") or "").strip().removeprefix("v")
        try:
            latest_key = _version_key(latest)
            release_url = _validated_release_url(item.get("html_url"))
        except UpdateCheckError:
            continue
        if bool(item.get("prerelease")) and not allow_prerelease:
            continue
        candidates.append((latest_key, item, latest, release_url))
    if not candidates:
        raise UpdateCheckError("GitHub release metadata contains no usable release")
    latest_key, selected, latest, release_url = max(candidates, key=lambda row: row[0])
    return UpdateCheckResult(
        current_version=current,
        latest_version=latest,
        update_available=latest_key > current_key,
        prerelease=bool(selected.get("prerelease")),
        release_url=release_url,
        release_name=str(selected.get("name") or selected.get("tag_name") or "")[:200],
        published_at=str(selected.get("published_at") or "")[:80],
    )
