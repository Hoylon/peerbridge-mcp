from __future__ import annotations

from peerbridge_mcp.secret_scan import (
    contains_secret,
    contains_secret_bytes,
    decode_text_bytes,
    redact_secrets,
    source_text_contains_secret,
)


def _join(*parts: str) -> str:
    return "".join(parts)


def test_shared_detector_covers_prefixes_jwt_auth_query_and_assignments() -> None:
    jwt = ".".join(("eyJ" + "a" * 12, "b" * 12, "c" * 12))
    values = (
        _join("s", "k-", "a" * 24),
        _join("xox", "b-", "b" * 24),
        _join("AI", "za", "C" * 24),
        jwt,
        _join("Bearer ", "D" * 24),
        _join("https://example.invalid/?to", "ken=", "E" * 24),
        _join("api_", "key=", "F" * 24),
        _join("pass", "word: ", "G" * 24),
    )

    assert all(contains_secret(value) for value in values)


def test_shared_redactor_preserves_diagnostic_structure_without_secret() -> None:
    secret = "H" * 24
    source = _join("api_", "key=", secret, "&stage=parse")
    redacted = redact_secrets(source)

    assert secret not in redacted
    assert "api_key=[REDACTED]" in redacted
    assert "stage=parse" in redacted


def test_runtime_redactor_removes_short_sensitive_assignments() -> None:
    source = _join("pass", "word=", "abc123xyz", "&stage=parse")

    redacted = redact_secrets(source)

    assert "abc123xyz" not in redacted
    assert redacted == "password=[REDACTED]&stage=parse"


def test_runtime_redactor_removes_complete_private_key_block() -> None:
    opening = _join("-----BE", "GIN PRIVATE KEY-----")
    closing = _join("-----E", "ND PRIVATE KEY-----")
    body = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    source = f"before\n{opening}\n{body}\n{closing}\nafter"

    redacted = redact_secrets(source)

    assert opening not in redacted
    assert body not in redacted
    assert closing not in redacted
    assert redacted == "before\n[REDACTED]\nafter"


def test_assignment_scanner_does_not_cross_line_after_prose_label() -> None:
    source = "Deploy without an admin or Worker secret:\n\n```powershell\nnpm run deploy\n```"

    assert contains_secret(source) is False
    assert redact_secrets(source) == source


def test_assignment_scanner_still_detects_same_line_credentials() -> None:
    secret = _join("same-line-", "credential-", "0123456789")
    source = f"provider_api_key = {secret}"

    assert contains_secret(source) is True
    assert redact_secrets(source) == "provider_api_key = [REDACTED]"


def test_source_references_are_not_credentials() -> None:
    assert contains_secret("api_key=api_key") is False


def test_runtime_assignments_do_not_exempt_fixture_substrings() -> None:
    for marker in ("example", "dummy", "fake"):
        secret = f"live-{marker}-credential-0123456789"
        source = f"provider_api_key={secret}"

        assert contains_secret(source) is True
        assert secret not in redact_secrets(source)

    assert contains_secret("token=[REDACTED]") is True
    assert contains_secret("password=test-secret") is True


def test_byte_scanner_canonical_decodes_supported_text_encodings() -> None:
    secret = _join("s", "k-", "U" * 24)
    text = f"provider_api_key={secret}\n"
    payloads = (
        text.encode("utf-8"),
        text.encode("utf-8-sig"),
        b"\xff\xfe" + text.encode("utf-16-le"),
        b"\xfe\xff" + text.encode("utf-16-be"),
    )

    for payload in payloads:
        assert decode_text_bytes(payload) == text
        assert contains_secret_bytes(payload) is True


def test_python_source_scan_distinguishes_variables_from_literal_credentials() -> None:
    safe_source = "api_key = str(payload.get('api_key') or '').strip()\n"
    unsafe_source = "api_key = " + repr("J" * 24) + "\n"

    assert source_text_contains_secret(safe_source, ".py") is False
    assert source_text_contains_secret(unsafe_source, ".py") is True


def test_python_test_source_allows_named_fixture_but_not_real_token_shape() -> None:
    fixture = "api_key = 'unit-test-provider-secret'\n"
    token = "api_key = " + repr(_join("s", "k-", "K" * 24)) + "\n"

    assert source_text_contains_secret(
        fixture, ".py", allow_test_fixtures=True
    ) is False
    assert source_text_contains_secret(
        token, ".py", allow_test_fixtures=True
    ) is True


def test_test_source_exemption_is_exact_not_substring_based() -> None:
    exact = "api_key = 'unit-test-provider-secret'\n"
    disguised = "api_key = 'live-example-credential-0123456789'\n"
    near_match = "api_key = 'unit-test-provider-secret-extra'\n"

    assert source_text_contains_secret(exact, ".py", allow_test_fixtures=True) is False
    assert source_text_contains_secret(
        disguised, ".py", allow_test_fixtures=True
    ) is True
    assert source_text_contains_secret(
        near_match, ".py", allow_test_fixtures=True
    ) is True
