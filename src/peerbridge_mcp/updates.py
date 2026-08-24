"""Read-only GitHub Release metadata checks; never downloads or executes updates."""

from __future__ import annotations

import json
import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from . import __version__


RELEASE_API = "https://api.github.com/repos/hoylon/peerbridge-mcp/releases?per_page=20"
MAX_RELEASE_RESPONSE_BYTES = 256 * 1024
MAX_PROVENANCE_RESPONSE_BYTES = 64 * 1024
PROVENANCE_SCHEMA = "peerbridge.windows-portable-provenance.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:(?:-(?P<sem_label>alpha|beta|rc)(?:[.-]?(?P<sem_number>\d+))?"
    r"(?:[.-](?P<sem_maintenance>\d+))?)|"
    r"(?P<pep_label>a|b|rc)(?P<pep_number>\d+)"
    r"(?:\.post(?P<pep_maintenance>\d+))?)?$",
    re.IGNORECASE,
)


class UpdateCheckError(RuntimeError):
    """Release metadata could not be safely validated."""


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str
    update_available: bool
    current_version_published: bool
    current_release_published: bool
    build_identity_status: str
    same_version_build_update: bool
    prerelease: bool
    release_url: str
    release_name: str
    published_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _SafeReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parsed = urllib.parse.urlsplit(newurl)
        if (
            parsed.scheme != "https"
            or parsed.hostname
            not in {
                "api.github.com",
                "github.com",
                "objects.githubusercontent.com",
                "release-assets.githubusercontent.com",
            }
            or parsed.port is not None
            or parsed.username
            or parsed.password
        ):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _version_key(value: str) -> tuple[int, int, int, int, int, int]:
    match = _VERSION.fullmatch(str(value).strip())
    if not match:
        raise UpdateCheckError("release version is not valid semantic version metadata")
    label = (match.group("sem_label") or match.group("pep_label") or "").lower()
    number_text = match.group("sem_number") or match.group("pep_number") or "0"
    maintenance_text = (
        match.group("sem_maintenance") or match.group("pep_maintenance") or "0"
    )
    stage = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "rc": 2, "": 3}[label]
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        stage,
        int(number_text),
        int(maintenance_text),
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
    expected_prefix = "/hoylon/peerbridge-mcp/releases/"
    if (
        not parsed.path.casefold().startswith(expected_prefix)
        or parsed.username
        or parsed.password
    ):
        raise UpdateCheckError("release URL does not belong to the PeerBridge repository")
    return url


def _validated_asset_url(value: Any, *, expected_name: str) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlsplit(url)
    decoded_path = urllib.parse.unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or not decoded_path.casefold().startswith(
            "/hoylon/peerbridge-mcp/releases/download/"
        )
        or decoded_path.rsplit("/", 1)[-1] != expected_name
    ):
        raise UpdateCheckError("release provenance URL is not an official PeerBridge asset")
    return url


def _release_runtime_sha256(
    release: dict[str, Any],
    *,
    current_version: str,
    client: Any,
    timeout_seconds: float,
) -> str | None:
    expected_name = (
        f"PeerBridgeControlRoom-{current_version}-windows-x64-portable.provenance.json"
    )
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    matches = [
        asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("name") == expected_name
    ]
    if len(matches) != 1:
        return None
    asset = matches[0]
    try:
        size = int(asset.get("size"))
    except (TypeError, ValueError):
        return None
    if (
        asset.get("state") != "uploaded"
        or size < 2
        or size > MAX_PROVENANCE_RESPONSE_BYTES
    ):
        return None
    try:
        url = _validated_asset_url(
            asset.get("browser_download_url"),
            expected_name=expected_name,
        )
    except UpdateCheckError:
        return None
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json, application/octet-stream",
            "User-Agent": f"PeerBridge/{current_version}",
        },
    )
    try:
        with client.open(
            request,
            timeout=max(1.0, min(float(timeout_seconds), 30.0)),
        ) as response:
            status = int(getattr(response, "status", 0))
            body = response.read(MAX_PROVENANCE_RESPONSE_BYTES + 1)
            final_url = str(getattr(response, "geturl", lambda: url)())
    except (OSError, urllib.error.HTTPError, urllib.error.URLError):
        return None
    final = urllib.parse.urlsplit(final_url)
    if (
        status != 200
        or len(body) > MAX_PROVENANCE_RESPONSE_BYTES
        or final.scheme != "https"
        or final.hostname
        not in {
            "github.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
        }
        or final.port is not None
        or final.username
        or final.password
    ):
        return None
    digest = str(asset.get("digest") or "").strip().lower()
    if digest and digest != f"sha256:{hashlib.sha256(body).hexdigest()}":
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    runtime_sha256 = str(
        payload.get("runtime_sha256") if isinstance(payload, dict) else ""
    ).strip().lower()
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != PROVENANCE_SCHEMA
        or payload.get("version") != current_version
        or payload.get("source_dirty") is not False
        or not _SHA256.fullmatch(runtime_sha256)
    ):
        return None
    return runtime_sha256


def check_for_updates(
    *,
    current_version: str = __version__,
    current_build_sha256: str | None = None,
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
    client = opener or urllib.request.build_opener(_SafeReleaseRedirect())
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
    candidates: list[
        tuple[tuple[int, int, int, int, int, int], dict[str, Any], str, str]
    ] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("draft") is not False:
            continue
        latest = str(item.get("tag_name") or "").strip().removeprefix("v")
        try:
            latest_key = _version_key(latest)
            release_url = _validated_release_url(item.get("html_url"))
        except UpdateCheckError:
            continue
        semantic_prerelease = _is_prerelease(latest)
        if (bool(item.get("prerelease")) or semantic_prerelease) and not allow_prerelease:
            continue
        candidates.append((latest_key, item, latest, release_url))
    if not candidates:
        raise UpdateCheckError("GitHub release metadata contains no usable release")
    latest_key, selected, latest, release_url = max(candidates, key=lambda row: row[0])
    current_releases = [row for row in candidates if row[0] == current_key]
    current_version_published = bool(current_releases)
    normalized_build = str(current_build_sha256 or "").strip().lower()
    build_identity_status = (
        "version-unpublished"
        if not current_version_published
        else "unavailable"
    )
    current_release_published = False
    if current_releases and _SHA256.fullmatch(normalized_build):
        for _key, release, _version, _url in current_releases:
            release_runtime_sha256 = _release_runtime_sha256(
                release,
                current_version=current,
                client=client,
                timeout_seconds=timeout_seconds,
            )
            if release_runtime_sha256 is None:
                continue
            if release_runtime_sha256 == normalized_build:
                build_identity_status = "verified"
                current_release_published = True
                break
            build_identity_status = "mismatch"
    same_version_build_update = bool(
        latest_key == current_key and build_identity_status == "mismatch"
    )
    return UpdateCheckResult(
        current_version=current,
        latest_version=latest,
        update_available=latest_key > current_key or same_version_build_update,
        current_version_published=current_version_published,
        current_release_published=current_release_published,
        build_identity_status=build_identity_status,
        same_version_build_update=same_version_build_update,
        prerelease=bool(selected.get("prerelease")) or _is_prerelease(latest),
        release_url=release_url,
        release_name=str(selected.get("name") or selected.get("tag_name") or "")[:200],
        published_at=str(selected.get("published_at") or "")[:80],
    )
