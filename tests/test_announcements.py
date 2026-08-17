from __future__ import annotations

import io
import json

import pytest

from peerbridge_mcp.announcements import (
    MAX_ANNOUNCEMENT_ID_LENGTH,
    MAX_ANNOUNCEMENT_READ_KEY_LENGTH,
    MAX_CACHE_BYTES,
    MAX_CACHED_ANNOUNCEMENTS,
    Announcement,
    AnnouncementConfig,
    AnnouncementError,
    announcement_read_key,
    default_announcement_preferences,
    fetch_announcements,
    load_announcement_cache,
    load_announcement_preferences,
    save_announcement_cache,
    save_announcement_preferences,
)


class _Response:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class _Opener:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response(self.payload)


def _announcement(**overrides):
    announcement = {
        "announcement_id": "alpha-20260815",
        "locale": "zh-Hant",
        "title": "Alpha 公告",
        "body": "這是一則純文字公告。",
        "severity": "important",
        "link_url": "https://github.com/hoylon/peerbridge-mcp/releases",
        "published_utc": "2026-08-15T00:00:00Z",
        "expires_utc": None,
    }
    announcement.update(overrides)
    return announcement


def _item(**overrides) -> Announcement:
    return Announcement(**_announcement(**overrides))


def _feed(**overrides):
    announcement = _announcement(**overrides.pop("announcement", {}))
    payload = {
        "schema": "peerbridge.announcement-feed.v1",
        "generated_utc": "2026-08-15T00:01:00Z",
        "announcements": [announcement],
    }
    payload.update(overrides)
    return payload


def test_config_is_https_and_uses_dedicated_endpoint() -> None:
    config = AnnouncementConfig("https://edge.example/v1/announcements", 300)
    assert config.poll_seconds == 300
    with pytest.raises(AnnouncementError):
        AnnouncementConfig("https://edge.example/v1/feedback", 300)
    with pytest.raises(AnnouncementError):
        AnnouncementConfig("http://edge.example/v1/announcements", 300)


def test_explicit_config_load_and_project_checkout_cannot_redirect_feed(
    tmp_path, monkeypatch
) -> None:
    config_dir = tmp_path / "maintainer"
    config_dir.mkdir()
    config_file = config_dir / "announcements.json"
    config_file.write_text(
        json.dumps(
            {
                "schema": "peerbridge.announcement-config.v1",
                "endpoint": "https://edge.example/v1/announcements",
                "poll_seconds": 600,
            }
        ),
        encoding="utf-8",
    )
    explicit = AnnouncementConfig.load_from_file(config_file)
    assert explicit.endpoint == "https://edge.example/v1/announcements"
    assert explicit.poll_seconds == 600

    project_support = tmp_path / "support"
    project_support.mkdir()
    (project_support / "announcements.json").write_text(
        json.dumps(
            {
                "schema": "peerbridge.announcement-config.v1",
                "endpoint": "https://attacker.example/v1/announcements",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert AnnouncementConfig.load() is None


def test_fetch_is_read_only_bounded_and_locale_bound() -> None:
    opener = _Opener(_feed())
    result = fetch_announcements(
        AnnouncementConfig("https://edge.example/v1/announcements"),
        locale="zh-Hant",
        after_utc="1970-01-01T00:00:00Z",
        opener=opener,
    )
    assert len(result) == 1
    assert result[0].title == "Alpha 公告"
    request, timeout = opener.requests[0]
    assert request.method == "GET"
    assert "locale=zh-Hant" in request.full_url
    assert timeout <= 30


def test_fetch_filters_activation_and_expiry_at_exact_boundaries() -> None:
    now = "2026-08-16T12:00:00Z"
    rows = [
        _announcement(announcement_id="active-at-publication", published_utc=now),
        _announcement(
            announcement_id="not-yet-active",
            published_utc="2026-08-16T12:00:00.000001Z",
        ),
        _announcement(
            announcement_id="expired-at-boundary",
            expires_utc=now,
        ),
        _announcement(
            announcement_id="active-until-after-boundary",
            expires_utc="2026-08-16T12:00:00.000001Z",
        ),
    ]
    result = fetch_announcements(
        AnnouncementConfig("https://edge.example/v1/announcements"),
        locale="zh-Hant",
        after_utc="1970-01-01T00:00:00Z",
        opener=_Opener(_feed(announcements=rows)),
        now_utc=now,
    )
    assert {item.announcement_id for item in result} == {
        "active-at-publication",
        "active-until-after-boundary",
    }


def test_fetch_sorts_fractional_publication_times_chronologically() -> None:
    rows = [
        _announcement(
            announcement_id="fractional",
            published_utc="2026-08-16T12:00:00.000001Z",
        ),
        _announcement(
            announcement_id="exact",
            published_utc="2026-08-16T12:00:00Z",
        ),
    ]
    result = fetch_announcements(
        AnnouncementConfig("https://edge.example/v1/announcements"),
        locale="zh-Hant",
        after_utc="1970-01-01T00:00:00Z",
        opener=_Opener(_feed(announcements=rows)),
        now_utc="2026-08-16T12:00:01Z",
    )
    assert [item.announcement_id for item in result] == ["exact", "fractional"]


@pytest.mark.parametrize(
    "announcement",
    [
        {"locale": "en"},
        {"severity": "execute"},
        {"link_url": "javascript:alert(1)"},
        {"body": ""},
        {"body": "bad\x00body"},
        {"title": {"not": "plain text"}},
        {"expires_utc": 0},
        {"announcement_id": "bad id"},
    ],
)
def test_untrusted_announcement_content_fails_closed(announcement) -> None:
    with pytest.raises(AnnouncementError):
        fetch_announcements(
            AnnouncementConfig("https://edge.example/v1/announcements"),
            locale="zh-Hant",
            after_utc="1970-01-01T00:00:00Z",
            opener=_Opener(_feed(announcement=announcement)),
        )


def test_preferences_are_local_atomic_and_bounded(tmp_path) -> None:
    assert load_announcement_preferences(tmp_path) == default_announcement_preferences()
    saved = save_announcement_preferences(
        tmp_path,
        popup_enabled=False,
        read_ids={"a", "b"},
        cursors={
            "en": "2026-08-15T00:00:00Z",
            "zh-Hans": "2026-08-15T00:00:00Z",
            "zh-Hant": "2026-08-15T00:00:00Z",
        },
    )
    assert load_announcement_preferences(tmp_path) == saved
    assert not list((tmp_path / ".peerbridge").glob("*.tmp"))


def test_id_and_locale_qualified_read_key_share_consistent_boundaries(tmp_path) -> None:
    identifier = "a" * MAX_ANNOUNCEMENT_ID_LENGTH
    result = fetch_announcements(
        AnnouncementConfig("https://edge.example/v1/announcements"),
        locale="zh-Hant",
        after_utc="1970-01-01T00:00:00Z",
        opener=_Opener(_feed(announcement={"announcement_id": identifier})),
        now_utc="2026-08-16T00:00:00Z",
    )
    read_key = announcement_read_key(result[0])
    assert len(read_key) == MAX_ANNOUNCEMENT_READ_KEY_LENGTH

    saved = save_announcement_preferences(
        tmp_path,
        popup_enabled=True,
        read_ids=[read_key],
        cursors=default_announcement_preferences()["cursors"],
    )
    assert saved["read_ids"] == [read_key]
    assert load_announcement_preferences(tmp_path)["read_ids"] == [read_key]

    with pytest.raises(AnnouncementError):
        fetch_announcements(
            AnnouncementConfig("https://edge.example/v1/announcements"),
            locale="zh-Hant",
            after_utc="1970-01-01T00:00:00Z",
            opener=_Opener(
                _feed(announcement={"announcement_id": identifier + "a"})
            ),
            now_utc="2026-08-16T00:00:00Z",
        )
    with pytest.raises(AnnouncementError):
        save_announcement_preferences(
            tmp_path,
            popup_enabled=True,
            read_ids=[f"zh-Hant:{identifier}a"],
            cursors=default_announcement_preferences()["cursors"],
        )


def test_cache_merges_saved_notices_for_offline_reload_and_filters_expiry(
    tmp_path,
) -> None:
    first = _item(announcement_id="offline-a")
    second = _item(
        announcement_id="offline-b",
        published_utc="2026-08-16T00:00:00Z",
        expires_utc="2026-08-17T00:00:00Z",
    )
    save_announcement_cache(
        tmp_path,
        [first],
        now_utc="2026-08-16T00:00:00Z",
    )
    saved = save_announcement_cache(
        tmp_path,
        [second],
        now_utc="2026-08-16T00:00:00Z",
    )
    assert [item.announcement_id for item in saved] == ["offline-a", "offline-b"]
    assert [
        item.announcement_id
        for item in load_announcement_cache(
            tmp_path, now_utc="2026-08-16T12:00:00Z"
        )
    ] == ["offline-a", "offline-b"]
    assert [
        item.announcement_id
        for item in load_announcement_cache(
            tmp_path, now_utc="2026-08-17T00:00:00Z"
        )
    ] == ["offline-a"]

    payload = json.loads(
        (tmp_path / ".peerbridge" / "announcement-cache.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(payload) == {"schema", "saved_utc", "announcements"}
    assert "endpoint" not in json.dumps(payload)
    assert not list((tmp_path / ".peerbridge").glob("*.tmp"))

    withdrawn = _item(
        announcement_id="offline-a",
        expires_utc="2026-08-17T00:00:00Z",
    )
    assert save_announcement_cache(
        tmp_path,
        [withdrawn],
        now_utc="2026-08-17T00:00:00Z",
    ) == ()


def test_cache_row_limit_keeps_the_newest_boundary(tmp_path) -> None:
    rows = [
        _item(announcement_id=f"notice-{index:03d}")
        for index in range(MAX_CACHED_ANNOUNCEMENTS + 1)
    ]
    saved = save_announcement_cache(
        tmp_path,
        rows,
        now_utc="2026-08-16T00:00:00Z",
    )
    assert len(saved) == MAX_CACHED_ANNOUNCEMENTS
    assert saved[0].announcement_id == "notice-001"
    assert saved[-1].announcement_id == f"notice-{MAX_CACHED_ANNOUNCEMENTS:03d}"


def test_cache_writer_enforces_encoded_byte_boundary(tmp_path) -> None:
    rows = [
        _item(
            announcement_id=f"large-{index:03d}",
            body="\U0001f600" * 4000,
        )
        for index in range(MAX_CACHED_ANNOUNCEMENTS)
    ]
    saved = save_announcement_cache(
        tmp_path,
        rows,
        now_utc="2026-08-16T00:00:00Z",
    )
    cache_path = tmp_path / ".peerbridge" / "announcement-cache.json"
    assert 0 < len(saved) < MAX_CACHED_ANNOUNCEMENTS
    assert cache_path.stat().st_size <= MAX_CACHE_BYTES
    assert load_announcement_cache(
        tmp_path, now_utc="2026-08-16T00:00:00Z"
    ) == saved


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        json.dumps(
            {
                "schema": "peerbridge.announcement-cache.v1",
                "saved_utc": "2026-08-16T00:00:00Z",
                "announcements": [_announcement(body="bad\x00body")],
            }
        ).encode("utf-8"),
        json.dumps(
            {
                "schema": "peerbridge.announcement-cache.v1",
                "saved_utc": "2026-08-16T00:00:00Z",
                "announcements": [_announcement(body=float("nan"))],
            }
        ).encode("utf-8"),
    ],
)
def test_cache_corruption_fails_closed(tmp_path, raw) -> None:
    cache_dir = tmp_path / ".peerbridge"
    cache_dir.mkdir()
    (cache_dir / "announcement-cache.json").write_bytes(raw)
    with pytest.raises(AnnouncementError):
        load_announcement_cache(tmp_path, now_utc="2026-08-16T00:00:00Z")


def test_cache_save_atomically_recovers_from_corruption(tmp_path) -> None:
    cache_dir = tmp_path / ".peerbridge"
    cache_dir.mkdir()
    cache_path = cache_dir / "announcement-cache.json"
    cache_path.write_bytes(b"{")
    expected = (_item(announcement_id="recovered"),)
    assert save_announcement_cache(
        tmp_path,
        expected,
        now_utc="2026-08-16T00:00:00Z",
    ) == expected
    assert load_announcement_cache(
        tmp_path, now_utc="2026-08-16T00:00:00Z"
    ) == expected
    assert not list(cache_dir.glob("*.tmp"))


def test_oversized_cache_is_rejected_without_parsing(tmp_path) -> None:
    cache_dir = tmp_path / ".peerbridge"
    cache_dir.mkdir()
    (cache_dir / "announcement-cache.json").write_bytes(b" " * (MAX_CACHE_BYTES + 1))
    with pytest.raises(AnnouncementError, match="too large"):
        load_announcement_cache(tmp_path, now_utc="2026-08-16T00:00:00Z")
