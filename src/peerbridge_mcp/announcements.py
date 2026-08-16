"""Read-only announcement feed with a bounded local cache and preferences."""

from __future__ import annotations

import json
import os
import secrets
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


ANNOUNCEMENT_CONFIG_SCHEMA = "peerbridge.announcement-config.v1"
ANNOUNCEMENT_FEED_SCHEMA = "peerbridge.announcement-feed.v1"
ANNOUNCEMENT_CACHE_SCHEMA = "peerbridge.announcement-cache.v1"
ANNOUNCEMENT_PREFERENCES_SCHEMA = "peerbridge.announcement-preferences.v1"
SUPPORTED_LOCALES = frozenset({"en", "zh-Hans", "zh-Hant"})
SUPPORTED_SEVERITIES = frozenset({"info", "important", "critical"})
MAX_RESPONSE_BYTES = 256 * 1024
MAX_ANNOUNCEMENTS = 50
MAX_CACHED_ANNOUNCEMENTS = MAX_ANNOUNCEMENTS * len(SUPPORTED_LOCALES)
MAX_CACHE_BYTES = 1024 * 1024
MAX_ANNOUNCEMENT_ID_LENGTH = 64
MAX_ANNOUNCEMENT_READ_KEY_LENGTH = (
    MAX_ANNOUNCEMENT_ID_LENGTH + 1 + max(map(len, SUPPORTED_LOCALES))
)
MAX_LINK_URL_LENGTH = 2048
MAX_READ_IDS = 500


class AnnouncementError(RuntimeError):
    """Announcement configuration, transport, or content is invalid."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise AnnouncementError("announcement endpoint redirects are not allowed")


@dataclass(frozen=True)
class AnnouncementConfig:
    endpoint: str
    poll_seconds: int = 300

    def __post_init__(self) -> None:
        endpoint = _https_endpoint(self.endpoint)
        poll_seconds = int(self.poll_seconds)
        if poll_seconds < 60 or poll_seconds > 86_400:
            raise AnnouncementError("announcement poll interval must be 60..86400 seconds")
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "poll_seconds", poll_seconds)

    @classmethod
    def load(cls) -> "AnnouncementConfig | None":
        """Load only the announcement endpoint pinned into the package."""
        path = Path(__file__).resolve().parent / "release_support" / "announcements.json"
        if not path.exists():
            return None
        return cls.load_from_file(path)

    @classmethod
    def load_from_file(cls, path: Path) -> "AnnouncementConfig":
        """Load an explicitly selected maintainer/test announcement config."""
        path = path.resolve()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AnnouncementError("announcement configuration is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema") != ANNOUNCEMENT_CONFIG_SCHEMA:
            raise AnnouncementError("announcement configuration has an unsupported schema")
        if set(payload) - {"schema", "endpoint", "poll_seconds"}:
            raise AnnouncementError("announcement configuration contains unsupported fields")
        return cls(
            endpoint=str(payload.get("endpoint") or ""),
            poll_seconds=int(payload.get("poll_seconds") or 300),
        )


@dataclass(frozen=True)
class Announcement:
    announcement_id: str
    locale: str
    title: str
    body: str
    severity: str
    link_url: str | None
    published_utc: str
    expires_utc: str | None


def _https_endpoint(value: Any) -> str:
    text = str(value or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/v1/announcements")
    ):
        raise AnnouncementError(
            "announcement endpoint must be a plain HTTPS /v1/announcements URL"
        )
    return text


def _utc_datetime(
    value: Any, label: str, *, optional: bool = False
) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        raise AnnouncementError(f"{label} is not a valid UTC timestamp")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise AnnouncementError(f"{label} is not a valid UTC timestamp") from exc
    else:
        raise AnnouncementError(f"{label} is not a valid UTC timestamp")
    if parsed.tzinfo is None:
        raise AnnouncementError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _utc_iso(value: Any, label: str, *, optional: bool = False) -> str | None:
    parsed = _utc_datetime(value, label, optional=optional)
    if parsed is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def _bounded_text(value: Any, maximum: int, label: str) -> str:
    if not isinstance(value, str):
        raise AnnouncementError(f"{label} must be plain text")
    text = value.strip()
    if not text or len(text) > maximum:
        raise AnnouncementError(f"{label} is missing or too long")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in text):
        raise AnnouncementError(f"{label} contains control characters")
    return text


def _link(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnnouncementError("announcement link must be plain text")
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_LINK_URL_LENGTH:
        raise AnnouncementError("announcement link is too long")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AnnouncementError("announcement link must use HTTPS")
    if parsed.fragment:
        raise AnnouncementError("announcement link fragments are not allowed")
    return text


def _announcement_id(value: Any) -> str:
    announcement_id = _bounded_text(
        value, MAX_ANNOUNCEMENT_ID_LENGTH, "announcement id"
    )
    if not all(
        character.isalnum() or character in "._-" for character in announcement_id
    ):
        raise AnnouncementError("announcement id is invalid")
    return announcement_id


def _parse_announcement(row: Any, expected_locale: str) -> Announcement:
    if not isinstance(row, dict):
        raise AnnouncementError("announcement row must be an object")
    allowed = {
        "announcement_id",
        "locale",
        "title",
        "body",
        "severity",
        "link_url",
        "published_utc",
        "expires_utc",
    }
    if set(row) - allowed:
        raise AnnouncementError("announcement row contains unsupported fields")
    announcement_id = _announcement_id(row.get("announcement_id"))
    locale = row.get("locale")
    severity = row.get("severity")
    if not isinstance(locale, str):
        raise AnnouncementError("announcement locale is invalid")
    if not isinstance(severity, str):
        raise AnnouncementError("announcement severity is invalid")
    if locale != expected_locale or locale not in SUPPORTED_LOCALES:
        raise AnnouncementError("announcement locale is invalid")
    if severity not in SUPPORTED_SEVERITIES:
        raise AnnouncementError("announcement severity is invalid")
    return Announcement(
        announcement_id=announcement_id,
        locale=locale,
        title=_bounded_text(row.get("title"), 160, "announcement title"),
        body=_bounded_text(row.get("body"), 4000, "announcement body"),
        severity=severity,
        link_url=_link(row.get("link_url")),
        published_utc=str(_utc_iso(row.get("published_utc"), "published UTC")),
        expires_utc=_utc_iso(row.get("expires_utc"), "expiry UTC", optional=True),
    )


def announcement_read_key(item: Announcement) -> str:
    """Return the stable locale-qualified key used by unread preferences."""
    validated = _parse_announcement(_announcement_payload(item), item.locale)
    return f"{validated.locale}:{validated.announcement_id}"


def _normalized_read_key(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AnnouncementError("announcement read history is invalid")
    if len(value) > MAX_ANNOUNCEMENT_READ_KEY_LENGTH:
        raise AnnouncementError("announcement read history is invalid")
    locale, separator, identity = value.partition(":")
    if separator:
        if locale not in SUPPORTED_LOCALES or _announcement_id(identity) != identity:
            raise AnnouncementError("announcement read history is invalid")
    elif _announcement_id(value) != value:
        raise AnnouncementError("announcement read history is invalid")
    return value


def _announcement_payload(item: Announcement) -> dict[str, Any]:
    if not isinstance(item, Announcement):
        raise AnnouncementError("announcement cache entries must be announcements")
    return {
        "announcement_id": item.announcement_id,
        "locale": item.locale,
        "title": item.title,
        "body": item.body,
        "severity": item.severity,
        "link_url": item.link_url,
        "published_utc": item.published_utc,
        "expires_utc": item.expires_utc,
    }


def _active_at(item: Announcement, now: datetime) -> bool:
    published = _utc_datetime(item.published_utc, "published UTC")
    expires = _utc_datetime(item.expires_utc, "expiry UTC", optional=True)
    assert published is not None
    return published <= now and (expires is None or now < expires)


def _published_at(item: Announcement) -> datetime:
    published = _utc_datetime(item.published_utc, "published UTC")
    assert published is not None
    return published


def _selected_now(now_utc: str | datetime | None) -> datetime:
    if now_utc is None:
        return datetime.now(UTC)
    parsed = _utc_datetime(now_utc, "announcement current UTC")
    assert parsed is not None
    return parsed


def fetch_announcements(
    config: AnnouncementConfig,
    *,
    locale: str,
    after_utc: str,
    opener: Any | None = None,
    timeout: float = 15.0,
    now_utc: str | datetime | None = None,
) -> tuple[Announcement, ...]:
    if locale not in SUPPORTED_LOCALES:
        raise AnnouncementError("unsupported announcement locale")
    after = str(_utc_iso(after_utc, "announcement cursor"))
    query = urllib.parse.urlencode({"locale": locale, "after": after})
    request = urllib.request.Request(
        f"{config.endpoint}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "PeerBridge-Announcement-Client/0.1",
        },
        method="GET",
    )
    selected = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with selected.open(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                raise AnnouncementError("announcement endpoint did not return HTTP 200")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except AnnouncementError:
        raise
    except Exception as exc:
        raise AnnouncementError("announcement endpoint is unavailable") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AnnouncementError("announcement response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnouncementError("announcement response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != ANNOUNCEMENT_FEED_SCHEMA:
        raise AnnouncementError("announcement response schema is unsupported")
    if set(payload) != {"schema", "generated_utc", "announcements"}:
        raise AnnouncementError("announcement response contains unsupported fields")
    _utc_iso(payload.get("generated_utc"), "generated UTC")
    rows = payload.get("announcements")
    if not isinstance(rows, list) or len(rows) > MAX_ANNOUNCEMENTS:
        raise AnnouncementError("announcement response row count is invalid")
    parsed = tuple(_parse_announcement(row, locale) for row in rows)
    identities = [item.announcement_id for item in parsed]
    if len(set(identities)) != len(identities):
        raise AnnouncementError("announcement response contains duplicate IDs")
    now = _selected_now(now_utc)
    active = (item for item in parsed if _active_at(item, now))
    return tuple(sorted(active, key=lambda item: (_published_at(item), item.announcement_id)))


def _cache_path(project_root: Path) -> Path:
    return project_root.resolve() / ".peerbridge" / "announcement-cache.json"


def _read_bounded(path: Path, maximum: int, label: str) -> bytes:
    try:
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
    except OSError as exc:
        raise AnnouncementError(f"{label} could not be read") from exc
    if len(payload) > maximum:
        raise AnnouncementError(f"{label} is too large")
    return payload


def _decode_cache(raw: bytes, now: datetime) -> tuple[Announcement, ...]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnouncementError("announcement cache is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != ANNOUNCEMENT_CACHE_SCHEMA:
        raise AnnouncementError("announcement cache schema is unsupported")
    if set(payload) != {"schema", "saved_utc", "announcements"}:
        raise AnnouncementError("announcement cache contains unsupported fields")
    _utc_iso(payload.get("saved_utc"), "announcement cache saved UTC")
    rows = payload.get("announcements")
    if not isinstance(rows, list) or len(rows) > MAX_CACHED_ANNOUNCEMENTS:
        raise AnnouncementError("announcement cache row count is invalid")
    parsed: list[Announcement] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("locale") not in SUPPORTED_LOCALES:
            raise AnnouncementError("announcement cache locale is invalid")
        parsed.append(_parse_announcement(row, str(row["locale"])))
    identities = [(item.locale, item.announcement_id) for item in parsed]
    if len(set(identities)) != len(identities):
        raise AnnouncementError("announcement cache contains duplicate IDs")
    active = (item for item in parsed if _active_at(item, now))
    return tuple(
        sorted(
            active,
            key=lambda item: (_published_at(item), item.locale, item.announcement_id),
        )
    )


def load_announcement_cache(
    project_root: Path,
    *,
    now_utc: str | datetime | None = None,
) -> tuple[Announcement, ...]:
    """Load validated, currently active notices for offline display."""
    path = _cache_path(project_root)
    if not path.exists():
        return ()
    return _decode_cache(
        _read_bounded(path, MAX_CACHE_BYTES, "announcement cache"),
        _selected_now(now_utc),
    )


def _encoded_cache(rows: list[Announcement], saved_utc: str) -> bytes:
    payload = {
        "schema": ANNOUNCEMENT_CACHE_SCHEMA,
        "saved_utc": saved_utc,
        "announcements": [_announcement_payload(item) for item in rows],
    }
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def save_announcement_cache(
    project_root: Path,
    announcements: Iterable[Announcement],
    *,
    now_utc: str | datetime | None = None,
) -> tuple[Announcement, ...]:
    """Merge active notices into a bounded local cache and return saved rows."""
    now = _selected_now(now_utc)
    path = _cache_path(project_root)
    try:
        cached = load_announcement_cache(project_root, now_utc=now)
    except AnnouncementError:
        cached = ()
    merged = {
        (item.locale, item.announcement_id): item
        for item in cached
    }
    for item in announcements:
        payload = _announcement_payload(item)
        validated = _parse_announcement(payload, item.locale)
        identity = (validated.locale, validated.announcement_id)
        if _active_at(validated, now):
            merged[identity] = validated
        else:
            merged.pop(identity, None)

    newest = sorted(
        merged.values(),
        key=lambda item: (_published_at(item), item.locale, item.announcement_id),
        reverse=True,
    )[:MAX_CACHED_ANNOUNCEMENTS]
    rows = sorted(
        newest,
        key=lambda item: (_published_at(item), item.locale, item.announcement_id),
    )
    saved_utc = now.isoformat().replace("+00:00", "Z")
    encoded = _encoded_cache(rows, saved_utc)
    while rows and len(encoded) > MAX_CACHE_BYTES:
        rows.pop(0)
        encoded = _encoded_cache(rows, saved_utc)
    if len(encoded) > MAX_CACHE_BYTES:
        raise AnnouncementError("announcement cache is too large")
    _atomic_write(path, encoded)
    return tuple(rows)


def _preferences_path(project_root: Path) -> Path:
    return project_root.resolve() / ".peerbridge" / "announcement-preferences.json"


def default_announcement_preferences() -> dict[str, Any]:
    return {
        "schema": ANNOUNCEMENT_PREFERENCES_SCHEMA,
        "popup_enabled": True,
        "read_ids": [],
        "cursors": {
            locale: "1970-01-01T00:00:00Z" for locale in sorted(SUPPORTED_LOCALES)
        },
    }


def load_announcement_preferences(project_root: Path) -> dict[str, Any]:
    path = _preferences_path(project_root)
    if not path.exists():
        return default_announcement_preferences()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnnouncementError("announcement preferences are not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != ANNOUNCEMENT_PREFERENCES_SCHEMA:
        raise AnnouncementError("announcement preferences schema is unsupported")
    if set(payload) != {"schema", "popup_enabled", "read_ids", "cursors"}:
        raise AnnouncementError("announcement preferences contain unsupported fields")
    if not isinstance(payload.get("popup_enabled"), bool):
        raise AnnouncementError("announcement popup preference is invalid")
    read_ids = payload.get("read_ids")
    if (
        not isinstance(read_ids, list)
        or len(read_ids) > MAX_READ_IDS
    ):
        raise AnnouncementError("announcement read history is invalid")
    normalized_ids = [_normalized_read_key(value) for value in read_ids]
    if len(set(normalized_ids)) != len(normalized_ids):
        raise AnnouncementError("announcement read history is invalid")
    cursors = payload.get("cursors")
    if not isinstance(cursors, dict) or set(cursors) != SUPPORTED_LOCALES:
        raise AnnouncementError("announcement locale cursors are invalid")
    for locale, value in cursors.items():
        _utc_iso(value, f"{locale} announcement cursor")
    return dict(payload)


def save_announcement_preferences(
    project_root: Path,
    *,
    popup_enabled: bool,
    read_ids: list[str] | tuple[str, ...] | set[str],
    cursors: Mapping[str, str],
) -> dict[str, Any]:
    normalized_ids = sorted({_normalized_read_key(value) for value in read_ids})[
        -MAX_READ_IDS:
    ]
    if set(cursors) != SUPPORTED_LOCALES:
        raise AnnouncementError("announcement locale cursors are invalid")
    normalized_cursors = {
        locale: str(_utc_iso(cursors[locale], f"{locale} announcement cursor"))
        for locale in sorted(SUPPORTED_LOCALES)
    }
    payload = {
        "schema": ANNOUNCEMENT_PREFERENCES_SCHEMA,
        "popup_enabled": bool(popup_enabled),
        "read_ids": normalized_ids,
        "cursors": normalized_cursors,
    }
    path = _preferences_path(project_root)
    _atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )
    return payload
