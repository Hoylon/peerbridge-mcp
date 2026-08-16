from __future__ import annotations

import io
import json

import pytest

from peerbridge_mcp.updates import UpdateCheckError, check_for_updates


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
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response(self.payload)


def _release(**overrides):
    payload = {
        "tag_name": "v0.2.0",
        "name": "PeerBridge Alpha 0.2.0",
        "draft": False,
        "prerelease": True,
        "published_at": "2026-08-16T00:00:00Z",
        "html_url": "https://github.com/oscarho200407-hue/peerbridge-mcp/releases/tag/v0.2.0",
    }
    payload.update(overrides)
    return payload


def test_read_only_update_check_reports_newer_release() -> None:
    opener = _Opener([_release(prerelease=False)])
    result = check_for_updates(current_version="0.1.0", opener=opener)
    assert result.update_available is True
    assert result.latest_version == "0.2.0"
    request, timeout = opener.requests[0]
    assert request.full_url.startswith("https://api.github.com/")
    assert request.method == "GET"
    assert timeout <= 30


def test_same_version_does_not_claim_an_update() -> None:
    result = check_for_updates(
        current_version="0.2.0", opener=_Opener([_release(prerelease=False)])
    )
    assert result.update_available is False


def test_alpha_channel_reads_newer_prerelease_from_release_list() -> None:
    payload = _release(
        tag_name="v0.1.0-alpha.2",
        name="PeerBridge Alpha 0.1.0-alpha.2",
        html_url=(
            "https://github.com/oscarho200407-hue/peerbridge-mcp/"
            "releases/tag/v0.1.0-alpha.2"
        ),
    )

    result = check_for_updates(current_version="0.1.0a1", opener=_Opener([payload]))

    assert result.update_available is True
    assert result.latest_version == "0.1.0-alpha.2"


def test_stable_channel_does_not_offer_prerelease() -> None:
    stable = _release(
        tag_name="v0.1.0",
        name="PeerBridge 0.1.0",
        prerelease=False,
        html_url="https://github.com/oscarho200407-hue/peerbridge-mcp/releases/tag/v0.1.0",
    )
    alpha = _release(
        tag_name="v0.2.0-alpha.1",
        html_url=(
            "https://github.com/oscarho200407-hue/peerbridge-mcp/"
            "releases/tag/v0.2.0-alpha.1"
        ),
    )

    result = check_for_updates(current_version="0.1.0", opener=_Opener([alpha, stable]))

    assert result.update_available is False
    assert result.latest_version == "0.1.0"


@pytest.mark.parametrize(
    "payload",
    [
        _release(draft=True),
        _release(tag_name="latest"),
        _release(html_url="https://evil.example/releases/v0.2.0"),
        _release(html_url="http://github.com/oscarho200407-hue/peerbridge-mcp/releases/v0.2.0"),
        _release(html_url="https://github.com:444/oscarho200407-hue/peerbridge-mcp/releases/tag/v0.2.0"),
        _release(html_url="https://github.com/oscarho200407-hue/peerbridge-mcp/releases/tag/v0.2.0?token=x"),
        _release(html_url="https://github.com/oscarho200407-hue/peerbridge-mcp/releases/tag/v0.2.0#download"),
    ],
)
def test_untrusted_release_metadata_fails_closed(payload) -> None:
    with pytest.raises(UpdateCheckError):
        check_for_updates(current_version="0.1.0", opener=_Opener([payload]))
