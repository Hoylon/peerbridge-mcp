from __future__ import annotations

import io
import hashlib
import json

import pytest

from peerbridge_mcp.updates import UpdateCheckError, check_for_updates


class _Response:
    status = 200

    def __init__(self, payload: object, url: str) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def geturl(self) -> str:
        return self._url


class _Opener:
    def __init__(
        self,
        payload: object,
        asset_payloads: dict[str, object] | None = None,
    ) -> None:
        self.payload = payload
        self.asset_payloads = dict(asset_payloads or {})
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        payload = self.asset_payloads.get(request.full_url, self.payload)
        return _Response(payload, request.full_url)


def _release(**overrides):
    payload = {
        "tag_name": "v0.2.0",
        "name": "PeerBridge Alpha 0.2.0",
        "draft": False,
        "prerelease": True,
        "published_at": "2026-08-16T00:00:00Z",
        "html_url": "https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.2.0",
    }
    payload.update(overrides)
    return payload


def _published_build(
    *, version: str, runtime_sha256: str
) -> tuple[dict[str, object], _Opener]:
    asset_name = (
        f"PeerBridgeControlRoom-{version}-windows-x64-portable.provenance.json"
    )
    asset_url = (
        "https://github.com/Hoylon/peerbridge-mcp/releases/download/"
        f"v{version}/{asset_name}"
    )
    provenance = {
        "schema": "peerbridge.windows-portable-provenance.v1",
        "version": version,
        "source_dirty": False,
        "runtime_sha256": runtime_sha256,
    }
    body = json.dumps(provenance).encode("utf-8")
    release = _release(
        tag_name=f"v{version}",
        name=f"PeerBridge {version}",
        prerelease=False,
        html_url=f"https://github.com/Hoylon/peerbridge-mcp/releases/tag/v{version}",
        assets=[
            {
                "name": asset_name,
                "state": "uploaded",
                "size": len(body),
                "digest": f"sha256:{hashlib.sha256(body).hexdigest()}",
                "browser_download_url": asset_url,
            }
        ],
    )
    return release, _Opener([release], {asset_url: provenance})


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
    runtime_sha256 = hashlib.sha256(b"published runtime").hexdigest()
    _release_payload, opener = _published_build(
        version="0.2.0",
        runtime_sha256=runtime_sha256,
    )
    result = check_for_updates(
        current_version="0.2.0",
        current_build_sha256=runtime_sha256,
        opener=opener,
    )
    assert result.update_available is False
    assert result.current_version_published is True
    assert result.current_release_published is True
    assert result.build_identity_status == "verified"


def test_same_version_different_build_is_offered_as_a_build_update() -> None:
    published_sha256 = hashlib.sha256(b"published runtime").hexdigest()
    _release_payload, opener = _published_build(
        version="0.2.0",
        runtime_sha256=published_sha256,
    )

    result = check_for_updates(
        current_version="0.2.0",
        current_build_sha256=hashlib.sha256(b"local runtime").hexdigest(),
        opener=opener,
    )

    assert result.current_version_published is True
    assert result.current_release_published is False
    assert result.build_identity_status == "mismatch"
    assert result.same_version_build_update is True
    assert result.update_available is True


def test_same_version_without_provenance_never_claims_the_build_is_published() -> None:
    result = check_for_updates(
        current_version="0.2.0",
        current_build_sha256=hashlib.sha256(b"local runtime").hexdigest(),
        opener=_Opener([_release(prerelease=False)]),
    )

    assert result.current_version_published is True
    assert result.current_release_published is False
    assert result.build_identity_status == "unavailable"
    assert result.update_available is False


def test_newer_unpublished_local_build_is_not_called_the_latest_release() -> None:
    published = _release(
        tag_name="v0.1.0-alpha.5.1",
        name="PeerBridge Alpha 5.1",
        prerelease=False,
        html_url=(
            "https://github.com/hoylon/peerbridge-mcp/"
            "releases/tag/v0.1.0-alpha.5.1"
        ),
    )

    result = check_for_updates(
        current_version="0.1.0a5.post2", opener=_Opener([published])
    )

    assert result.update_available is False
    assert result.current_version_published is False
    assert result.current_release_published is False
    assert result.latest_version == "0.1.0-alpha.5.1"


def test_alpha_channel_reads_newer_prerelease_from_release_list() -> None:
    payload = _release(
        tag_name="v0.1.0-alpha.2",
        name="PeerBridge Alpha 0.1.0-alpha.2",
        html_url=(
            "https://github.com/hoylon/peerbridge-mcp/"
            "releases/tag/v0.1.0-alpha.2"
        ),
    )

    result = check_for_updates(current_version="0.1.0a1", opener=_Opener([payload]))

    assert result.update_available is True
    assert result.latest_version == "0.1.0-alpha.2"


def test_alpha_maintenance_release_orders_after_its_base_alpha() -> None:
    payload = _release(
        tag_name="v0.1.0-alpha.5.1",
        name="PeerBridge Alpha 0.1.0-alpha.5.1",
        prerelease=False,
        html_url=(
            "https://github.com/hoylon/peerbridge-mcp/"
            "releases/tag/v0.1.0-alpha.5.1"
        ),
    )

    result = check_for_updates(current_version="0.1.0a5", opener=_Opener([payload]))

    assert result.update_available is True
    assert result.latest_version == "0.1.0-alpha.5.1"


def test_stable_channel_does_not_offer_prerelease() -> None:
    stable = _release(
        tag_name="v0.1.0",
        name="PeerBridge 0.1.0",
        prerelease=False,
        html_url="https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.1.0",
    )
    alpha = _release(
        tag_name="v0.2.0-alpha.1",
        html_url=(
            "https://github.com/hoylon/peerbridge-mcp/"
            "releases/tag/v0.2.0-alpha.1"
        ),
    )

    result = check_for_updates(current_version="0.1.0", opener=_Opener([alpha, stable]))

    assert result.update_available is False
    assert result.latest_version == "0.1.0"


def test_stable_channel_rejects_alpha_tag_even_when_github_marks_it_normal() -> None:
    stable = _release(
        tag_name="v0.1.0",
        name="PeerBridge 0.1.0",
        prerelease=False,
        html_url="https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.1.0",
    )
    mislabeled_alpha = _release(
        tag_name="v0.2.0-alpha.1",
        prerelease=False,
        html_url=(
            "https://github.com/hoylon/peerbridge-mcp/"
            "releases/tag/v0.2.0-alpha.1"
        ),
    )

    result = check_for_updates(
        current_version="0.1.0", opener=_Opener([mislabeled_alpha, stable])
    )

    assert result.latest_version == "0.1.0"
    assert result.update_available is False


def test_alpha_channel_recognizes_semantic_alpha_on_normal_github_release() -> None:
    payload = _release(
        tag_name="v0.1.0-alpha.2",
        prerelease=False,
        html_url=(
            "https://github.com/hoylon/peerbridge-mcp/"
            "releases/tag/v0.1.0-alpha.2"
        ),
    )

    result = check_for_updates(current_version="0.1.0a1", opener=_Opener([payload]))

    assert result.update_available is True
    assert result.prerelease is True


def test_official_release_url_accepts_github_owner_display_case() -> None:
    payload = _release(
        tag_name="v0.1.0-alpha.4",
        html_url=(
            "https://github.com/Hoylon/peerbridge-mcp/"
            "releases/tag/v0.1.0-alpha.4"
        ),
    )

    result = check_for_updates(current_version="0.1.0a3", opener=_Opener([payload]))

    assert result.update_available is True
    assert result.release_url == payload["html_url"]


@pytest.mark.parametrize(
    "payload",
    [
        _release(draft=True),
        _release(tag_name="latest"),
        _release(html_url="https://evil.example/releases/v0.2.0"),
        _release(html_url="http://github.com/hoylon/peerbridge-mcp/releases/v0.2.0"),
        _release(html_url="https://github.com:444/hoylon/peerbridge-mcp/releases/tag/v0.2.0"),
        _release(html_url="https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.2.0?token=x"),
        _release(html_url="https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.2.0#download"),
    ],
)
def test_untrusted_release_metadata_fails_closed(payload) -> None:
    with pytest.raises(UpdateCheckError):
        check_for_updates(current_version="0.1.0", opener=_Opener([payload]))
