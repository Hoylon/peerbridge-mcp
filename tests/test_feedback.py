from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
import peerbridge_mcp.feedback as feedback_module
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from peerbridge_mcp.feedback import (
    FEEDBACK_REPORT_SCHEMA,
    FEEDBACK_SECRET_SCHEMA,
    FEEDBACK_UPLOAD_SCHEMA,
    JSON_BASE64_TRANSPORT,
    FeedbackConfig,
    FeedbackError,
    collect_runtime_diagnostics,
    create_feedback_bundle,
    deliver_feedback_bundle,
    feedback_mailto,
    redact_feedback_text,
    run_feedback_encryption_self_test,
)
from tests._image_fixtures import PNG


def test_default_feedback_outbox_rejects_filesystem_links(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    try:
        (project / ".peerbridge").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem did not permit a temporary directory symlink")

    with pytest.raises(FeedbackError, match="filesystem link"):
        create_feedback_bundle(
            project,
            summary="Link regression",
            message="The default outbox must remain inside local state.",
        )


def test_feedback_outbox_has_a_lifetime_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(feedback_module, "MAX_OUTBOX_BUNDLES", 1)
    create_feedback_bundle(
        tmp_path,
        summary="First report",
        message="The first bounded report.",
        config=FeedbackConfig(),
        output_root=outbox,
    )

    with pytest.raises(FeedbackError, match="outbox quota"):
        create_feedback_bundle(
            tmp_path,
            summary="Second report",
            message="The second report must be rejected.",
            config=FeedbackConfig(),
            output_root=outbox,
        )


def _support_key_pair(tmp_path: Path) -> tuple[Path, rsa.RSAPrivateKey]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_path = tmp_path / "support-public.pem"
    public_path.write_bytes(public_key)
    return public_path, private_key


def _key_config(public_path: Path, **overrides) -> FeedbackConfig:
    values = {
        "public_key_path": public_path,
        "public_key_sha256": hashlib.sha256(public_path.read_bytes()).hexdigest(),
        "recipient_label": "PeerBridge test support",
    }
    values.update(overrides)
    return FeedbackConfig(**values)


def _zip_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _fake_provider_secret(value: str) -> str:
    """Build a realistic test token without embedding a credential-like literal."""
    return "".join(("s", "k-", value))


_PLAINTEXT_CREDENTIAL_METADATA_FIELDS = {
    "character_count",
    "class_runs",
    "leading_whitespace",
    "line_count",
    "line_ending",
    "nfc_changes_input",
    "nfkc_changes_input",
    "quote_wrapper",
    "special_positions",
    "starts_with_bom",
    "trailing_whitespace",
    "utf8_byte_count",
}


def _assert_report_has_no_credential_metadata(report: dict) -> None:
    serialized = json.dumps(report, sort_keys=True)
    assert "credential_diagnostics" not in report
    assert all(field not in serialized for field in _PLAINTEXT_CREDENTIAL_METADATA_FIELDS)


def test_redaction_never_retains_secret_content() -> None:
    secret = f"  {_fake_provider_secret('LiveExample1234567890')}\r\n"
    redacted = redact_feedback_text(f"api_key={secret}")

    assert secret not in redacted
    assert "LiveExample" not in redacted


def test_runtime_diagnostics_do_not_depend_on_platform_wmi(tmp_path: Path) -> None:
    secret = _fake_provider_secret("NeverPersistThisValue123456789")
    diagnostics = collect_runtime_diagnostics(
        tmp_path,
        locale="zh-Hant",
        parser_stage="provider import",
        failure_text=f"api_key={secret}",
    )

    assert diagnostics["os_family"]
    assert diagnostics["python_version"]
    assert diagnostics["locale"] == "zh-Hant"
    assert "NeverPersistThisValue" not in json.dumps(diagnostics, sort_keys=True)


def test_plaintext_key_is_not_persisted_when_escalation_is_off(tmp_path: Path) -> None:
    secret = _fake_provider_secret("NeverPersistThisValue123456789")
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Provider parser failure",
        message=f"api_key={secret}",
        credential_input=secret,
        parser_stage="provider import",
        failure_text=f"Authorization: Bearer {secret}",
        output_root=tmp_path / "outbox",
    )
    members = _zip_members(bundle.path)
    archive_bytes = bundle.path.read_bytes()
    report = json.loads(members["report.json"])

    assert report["schema"] == FEEDBACK_REPORT_SCHEMA
    assert report["encrypted_credential_included"] is False
    assert "encrypted-credential.json" not in members
    assert secret.encode() not in archive_bytes
    assert "NeverPersistThisValue" not in json.dumps(report, sort_keys=True)
    _assert_report_has_no_credential_metadata(report)


@pytest.mark.parametrize("marker", ["example", "dummy", "fake"])
def test_feedback_redacts_realistic_secrets_containing_fixture_words(
    tmp_path: Path, marker: str
) -> None:
    secret = f"live-{marker}-provider-credential-0123456789"
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Provider parser failure",
        message=f"provider_api_key={secret}",
        failure_text=f"Authorization: Bearer {secret}",
        output_root=tmp_path / marker,
    )
    members = _zip_members(bundle.path)
    report = json.loads(members["report.json"])

    assert secret.encode("utf-8") not in members["report.json"]
    assert secret not in json.dumps(report, sort_keys=True)
    _assert_report_has_no_credential_metadata(report)


def test_feedback_archive_redacts_short_password_and_complete_private_key(
    tmp_path: Path,
) -> None:
    opening = "".join(("-----BE", "GIN PRIVATE KEY-----"))
    closing = "".join(("-----E", "ND PRIVATE KEY-----"))
    body = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Credential parser failure",
        message=f"password=abc123xyz\n{opening}\n{body}\n{closing}",
        failure_text=f"password=abc123xyz\n{opening}\n{body}\n{closing}",
        output_root=tmp_path / "redaction-outbox",
    )

    report_payload = _zip_members(bundle.path)["report.json"]

    for plaintext in ("abc123xyz", opening, body, closing):
        assert plaintext.encode("utf-8") not in report_payload


def test_one_submission_encrypts_complete_key_locally(tmp_path: Path) -> None:
    public_path, private_key = _support_key_pair(tmp_path)
    secret = "".join(("relay-key:", "需要保留空格 and-newline", "\n"))
    config = _key_config(public_path)
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Exact provider credential parse failure",
        message="Import fails after paste.",
        credential_input=secret,
        include_encrypted_credential=True,
        config=config,
        output_root=tmp_path / "outbox",
    )
    members = _zip_members(bundle.path)
    envelope = json.loads(members["encrypted-credential.json"])
    report = json.loads(members["report.json"])

    assert envelope["schema"] == FEEDBACK_SECRET_SCHEMA
    assert envelope["case_id"] == bundle.case_id
    assert bundle.encrypted_secret_included is True
    assert report["encrypted_credential_included"] is True
    assert secret.encode("utf-8") not in bundle.path.read_bytes()
    _assert_report_has_no_credential_metadata(report)

    data_key = private_key.decrypt(
        base64.b64decode(envelope["wrapped_key_b64"]),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    decrypted = AESGCM(data_key).decrypt(
        base64.b64decode(envelope["nonce_b64"]),
        base64.b64decode(envelope["ciphertext_b64"]),
        base64.b64decode(envelope["associated_data_b64"]),
    )
    assert decrypted.decode("utf-8") == secret


def test_no_endpoint_returns_local_bundle_and_case_code(tmp_path: Path) -> None:
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Offline report",
        message="The provider cannot be initialized.",
        output_root=tmp_path / "outbox",
    )
    result = deliver_feedback_bundle(bundle, FeedbackConfig())

    assert result["delivered"] is False
    assert result["reason"] == "private_endpoint_not_configured"
    assert result["case_id"] == bundle.case_id
    assert result["bundle_sha256"] == bundle.sha256
    assert Path(result["bundle_path"]) == bundle.path


def test_config_and_mail_fallback_are_explicit(tmp_path: Path) -> None:
    config_dir = tmp_path / "support"
    config_dir.mkdir()
    (config_dir / "support.json").write_text(
        json.dumps(
            {
                "schema": "peerbridge.feedback-config.v1",
                "endpoint": "https://support.example.invalid/v1/feedback",
                "support_email": "support@example.invalid",
                "privacy_url": "https://support.example.invalid/privacy",
                "recipient_label": "PeerBridge private support",
            }
        ),
        encoding="utf-8",
    )
    config = FeedbackConfig.load_from_file(config_dir / "support.json")
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Mail fallback",
        message="Private endpoint unavailable.",
        config=config,
        output_root=tmp_path / "outbox",
    )

    assert config.endpoint == "https://support.example.invalid/v1/feedback"
    assert feedback_mailto(config, bundle).startswith("mailto:support@example.invalid?")


def test_packaged_release_support_is_pinned_and_provider_independent(tmp_path: Path) -> None:
    config = FeedbackConfig.load()

    assert config.endpoint == "https://peerbridge-edge.peerbridge-edge.workers.dev/v1/feedback"
    assert config.endpoint_transport == JSON_BASE64_TRANSPORT
    assert config.support_email is None
    assert config.recipient_label == "PeerBridge private support"
    assert config.public_key_path is not None
    assert config.public_key_path.name == "peerbridge-support-public.pub"
    assert config.encrypted_secret_available is True


def test_packaged_release_support_rejects_config_or_public_key_tampering(
    tmp_path: Path, monkeypatch
) -> None:
    import peerbridge_mcp.feedback as feedback_module

    root = Path(__file__).resolve().parents[1]
    source_support = root / "src" / "peerbridge_mcp" / "release_support"
    fake_package = tmp_path / "peerbridge_mcp"
    fake_support = fake_package / "release_support"
    fake_support.mkdir(parents=True)
    config_path = fake_support / "support.json"
    public_key_path = fake_support / "peerbridge-support-public.pub"
    pristine_config = (source_support / "support.json").read_bytes()
    pristine_public_key = (
        source_support / "peerbridge-support-public.pub"
    ).read_bytes()
    config_path.write_bytes(pristine_config)
    public_key_path.write_bytes(pristine_public_key)
    monkeypatch.setattr(feedback_module, "__file__", str(fake_package / "feedback.py"))

    assert FeedbackConfig.load().endpoint.endswith("/v1/feedback")

    tampered_config = json.loads(pristine_config.decode("utf-8"))
    tampered_config["endpoint"] = "https://attacker.example/v1/feedback"
    config_path.write_text(json.dumps(tampered_config), encoding="utf-8")
    with pytest.raises(FeedbackError, match="configuration trust anchor mismatch"):
        FeedbackConfig.load()

    config_path.write_bytes(pristine_config)
    public_key_path.write_bytes(pristine_public_key + b"\n")
    with pytest.raises(FeedbackError, match="public key trust anchor mismatch"):
        FeedbackConfig.load()


def test_packaged_support_key_is_git_byte_stable() -> None:
    root = Path(__file__).resolve().parents[1]
    attributes = (root / ".gitattributes").read_text(encoding="utf-8")
    packaged_key = (
        root
        / "src"
        / "peerbridge_mcp"
        / "release_support"
        / "peerbridge-support-public.pub"
    )
    repository_key = root / "support" / "peerbridge-support-public.pub"
    support_config = json.loads(
        (packaged_key.parent / "support.json").read_text(encoding="utf-8")
    )
    expected_sha256 = support_config["public_key_sha256"]

    assert (
        "src/peerbridge_mcp/release_support/peerbridge-support-public.pub -text"
        in attributes
    )
    assert "support/peerbridge-support-public.pub -text" in attributes
    assert packaged_key.read_bytes() == repository_key.read_bytes()
    assert b"\r\n" not in packaged_key.read_bytes()
    assert hashlib.sha256(packaged_key.read_bytes()).hexdigest() == expected_sha256


def test_packaged_feedback_encryption_self_test_is_plaintext_free() -> None:
    result = run_feedback_encryption_self_test()

    assert result["status"] == "PASS"
    assert result["schema"] == FEEDBACK_SECRET_SCHEMA
    assert result["algorithm"] == "RSA-OAEP-SHA256+A256GCM"
    assert len(result["configured_pem_sha256"]) == 64


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": "peerbridge.feedback-config.v1", "endpoint": "http://example.com"},
        {"schema": "peerbridge.feedback-config.v1", "endpoint": "https://u:p@example.com"},
        {"schema": "wrong"},
    ],
)
def test_unsafe_or_unknown_support_config_fails_closed(
    tmp_path: Path, payload: dict[str, str]
) -> None:
    config_dir = tmp_path / "support"
    config_dir.mkdir()
    (config_dir / "support.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FeedbackError):
        FeedbackConfig.load_from_file(config_dir / "support.json")


def test_project_checkout_cannot_override_packaged_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "support"
    config_dir.mkdir()
    (config_dir / "support.json").write_text(
        json.dumps(
            {
                "schema": "peerbridge.feedback-config.v1",
                "support_email": "attacker@example.invalid",
                "recipient_label": "Untrusted project",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    config = FeedbackConfig.load()

    assert config.support_email is None
    assert config.recipient_label == "PeerBridge private support"


def test_attachment_policy_accepts_images_and_rejects_unsafe_types(tmp_path: Path) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(PNG)
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Screenshot attached",
        message="The UI shows an error.",
        attachment_paths=[image],
        attachment_consent=True,
        output_root=tmp_path / "outbox",
    )
    members = _zip_members(bundle.path)
    assert members["attachments/01.png"] == image.read_bytes()

    executable = tmp_path / "unsafe.exe"
    executable.write_bytes(b"MZ")
    with pytest.raises(FeedbackError, match="type is not allowed"):
        create_feedback_bundle(
            tmp_path,
            summary="Unsafe attachment",
            message="Must fail closed.",
            attachment_paths=[executable],
            attachment_consent=True,
            output_root=tmp_path / "other-outbox",
        )


def test_text_attachment_with_plaintext_credential_uses_encrypted_field_instead(
    tmp_path: Path,
) -> None:
    secret = _fake_provider_secret("attachment-secret-must-not-enter-archive")
    diagnostic = tmp_path / "provider.log"
    diagnostic.write_text(f"provider_api_key={secret}\n", encoding="utf-8")

    with pytest.raises(FeedbackError, match="explicit encrypted credential field"):
        create_feedback_bundle(
            tmp_path,
            summary="Provider parser failure",
            message="The selected diagnostic must fail closed.",
            attachment_paths=[diagnostic],
            attachment_consent=True,
            output_root=tmp_path / "outbox-secret-attachment",
        )

    assert not (tmp_path / "outbox-secret-attachment").exists()


@pytest.mark.parametrize("extension", [".json", ".log", ".txt"])
@pytest.mark.parametrize("byte_order", ["le", "be"])
def test_utf16_credential_attachments_fail_closed(
    tmp_path: Path, extension: str, byte_order: str
) -> None:
    secret = _fake_provider_secret(f"{byte_order}-encoded-credential-0123456789")
    text = (
        json.dumps({"provider_api_key": secret})
        if extension == ".json"
        else f"provider_api_key={secret}\n"
    )
    bom = b"\xff\xfe" if byte_order == "le" else b"\xfe\xff"
    diagnostic = tmp_path / f"provider{extension}"
    diagnostic.write_bytes(bom + text.encode(f"utf-16-{byte_order}"))
    output_root = tmp_path / f"outbox-{extension[1:]}-{byte_order}"

    with pytest.raises(FeedbackError, match="explicit encrypted credential field"):
        create_feedback_bundle(
            tmp_path,
            summary="Provider parser failure",
            message="The selected diagnostic must fail closed.",
            attachment_paths=[diagnostic],
            attachment_consent=True,
            output_root=output_root,
        )

    assert not output_root.exists()


def test_feedback_attachment_is_not_reopened_by_path_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(PNG)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == image:
            raise AssertionError("validated source attachment was reopened by path")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Single handle",
        message="The selected file must be read from its validated handle.",
        attachment_paths=[image],
        attachment_consent=True,
        output_root=tmp_path / "outbox-single-handle",
    )

    assert _zip_members(bundle.path)["attachments/01.png"] == PNG


def test_direct_config_requires_a_pinned_key_and_recipient(tmp_path: Path) -> None:
    public_path, _private_key = _support_key_pair(tmp_path)
    with pytest.raises(FeedbackError, match="pinned SHA-256"):
        FeedbackConfig(public_key_path=public_path, recipient_label="Support")
    with pytest.raises(FeedbackError, match="recipient label"):
        FeedbackConfig(
            public_key_path=public_path,
            public_key_sha256=hashlib.sha256(public_path.read_bytes()).hexdigest(),
        )


def test_key_swap_after_config_validation_fails_closed(tmp_path: Path) -> None:
    public_path, _private_key = _support_key_pair(tmp_path)
    config = _key_config(public_path)
    replacement, _replacement_private = _support_key_pair(tmp_path / "replacement")
    public_path.write_bytes(replacement.read_bytes())

    with pytest.raises(FeedbackError, match="changed after configuration validation"):
        create_feedback_bundle(
            tmp_path,
            summary="Key changed",
            message="Must not encrypt to a substituted recipient.",
            credential_input=_fake_provider_secret("secret-that-must-not-persist"),
            include_encrypted_credential=True,
            config=config,
            output_root=tmp_path / "outbox",
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://localhost/v1/feedback",
        "https://localhost./v1/feedback",
        "https://service.local./v1/feedback",
        "https://127.0.0.1/v1/feedback",
        "https://10.0.0.1/v1/feedback",
        "https://support.example.invalid/v1/feedback?token=secret",
    ],
)
def test_private_or_ambiguous_feedback_endpoints_fail_closed(endpoint: str) -> None:
    with pytest.raises(FeedbackError):
        FeedbackConfig(endpoint=endpoint, recipient_label="Support")


def test_attachments_require_explicit_consent_and_valid_content(tmp_path: Path) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(PNG)
    with pytest.raises(FeedbackError, match="explicit user selection"):
        create_feedback_bundle(
            tmp_path,
            summary="No consent",
            message="Must not attach implicitly.",
            attachment_paths=[image],
            output_root=tmp_path / "outbox-a",
        )

    disguised = tmp_path / "disguised.png"
    disguised.write_bytes(b"MZ-not-an-image")
    with pytest.raises(FeedbackError, match="content does not match"):
        create_feedback_bundle(
            tmp_path,
            summary="Disguised executable",
            message="Must reject extension-only validation.",
            attachment_paths=[disguised],
            attachment_consent=True,
            output_root=tmp_path / "outbox-b",
        )


class _DeliveryResponse:
    status = 200

    def __init__(self, payload: dict[str, str]) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class _DeliveryOpener:
    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload

    def open(self, _request, timeout):
        assert timeout <= 60
        return _DeliveryResponse(self.payload)


class _CapturingDeliveryOpener(_DeliveryOpener):
    def __init__(self, payload: dict[str, str]) -> None:
        super().__init__(payload)
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return super().open(request, timeout)


def test_json_base64_delivery_binds_case_and_bundle_without_mutable_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Provider import failed",
        message="Please reply after reproducing this parser failure.",
        contact="reporter@example.com",
        output_root=tmp_path / "outbox-json",
    )
    opener = _CapturingDeliveryOpener(
        {
            "case_id": bundle.case_id,
            "bundle_sha256": bundle.sha256,
            "receipt": "stored",
            "notification_sent": True,
        }
    )
    monkeypatch.setattr(
        "peerbridge_mcp.feedback.urllib.request.build_opener",
        lambda *_args: opener,
    )
    config = FeedbackConfig(
        endpoint="https://feedback.example.invalid/v1/feedback",
        endpoint_transport=JSON_BASE64_TRANSPORT,
        recipient_label="PeerBridge test support",
    )

    result = deliver_feedback_bundle(bundle, config)

    assert result["delivered"] is True
    assert result["notification_sent"] is True
    assert opener.request is not None
    assert opener.request.headers["Content-type"] == "application/json; charset=utf-8"
    payload = json.loads(opener.request.data.decode("utf-8"))
    assert payload["schema"] == FEEDBACK_UPLOAD_SCHEMA
    assert payload["case_id"] == bundle.case_id
    assert payload["bundle_sha256"] == bundle.sha256
    assert set(payload) == {
        "schema",
        "case_id",
        "bundle_sha256",
        "bundle_base64",
    }
    assert base64.b64decode(payload["bundle_base64"]) == bundle.path.read_bytes()


def test_json_delivery_reads_sealed_bundle_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Single-read delivery",
        message="The uploaded payload must be the bytes that were verified.",
        output_root=tmp_path / "outbox-single-read",
    )
    expected_payload = bundle.path.read_bytes()
    reads = 0
    original_read_bytes = Path.read_bytes

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal reads
        if path == bundle.path:
            reads += 1
            if reads > 1:
                raise AssertionError("sealed feedback bundle was reopened")
        return original_read_bytes(path)

    opener = _CapturingDeliveryOpener(
        {
            "case_id": bundle.case_id,
            "bundle_sha256": bundle.sha256,
            "receipt": "stored",
        }
    )
    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    monkeypatch.setattr(
        "peerbridge_mcp.feedback.urllib.request.build_opener",
        lambda *_args: opener,
    )

    result = deliver_feedback_bundle(
        bundle,
        FeedbackConfig(
            endpoint="https://feedback.example.invalid/v1/feedback",
            endpoint_transport=JSON_BASE64_TRANSPORT,
            recipient_label="PeerBridge test support",
        ),
    )

    assert result["delivered"] is True
    assert reads == 1
    assert opener.request is not None
    payload = json.loads(opener.request.data.decode("utf-8"))
    assert base64.b64decode(payload["bundle_base64"]) == expected_payload


def test_delivery_preserves_unconfirmed_or_failed_notification_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Notification receipt state",
        message="Storage and email notification must remain distinct states.",
        output_root=tmp_path / "outbox-notification",
    )
    config = FeedbackConfig(
        endpoint="https://feedback.example.invalid/v1/feedback",
        endpoint_transport=JSON_BASE64_TRANSPORT,
        recipient_label="PeerBridge test support",
    )
    for response_value, expected in ((False, False), ("unknown", None)):
        monkeypatch.setattr(
            "peerbridge_mcp.feedback.urllib.request.build_opener",
            lambda *_args, value=response_value: _DeliveryOpener(
                {
                    "case_id": bundle.case_id,
                    "bundle_sha256": bundle.sha256,
                    "receipt": "stored",
                    "notification_sent": value,
                }
            ),
        )
        result = deliver_feedback_bundle(bundle, config)
        assert result["delivered"] is True
        assert result["notification_sent"] is expected


def test_json_base64_delivery_rejects_mismatched_bundle_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Receipt SHA binding",
        message="The endpoint must acknowledge the exact archive.",
        output_root=tmp_path / "outbox-json-sha",
    )
    monkeypatch.setattr(
        "peerbridge_mcp.feedback.urllib.request.build_opener",
        lambda *_args: _DeliveryOpener(
            {
                "case_id": bundle.case_id,
                "bundle_sha256": "0" * 64,
                "receipt": "wrong",
            }
        ),
    )
    config = FeedbackConfig(
        endpoint="https://feedback.example.invalid/v1/feedback",
        endpoint_transport=JSON_BASE64_TRANSPORT,
        recipient_label="PeerBridge test support",
    )

    with pytest.raises(FeedbackError, match="mismatched bundle SHA-256"):
        deliver_feedback_bundle(bundle, config)


def test_raw_zip_delivery_requires_exact_bundle_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Raw receipt SHA binding",
        message="The raw transport must acknowledge the exact archive bytes.",
        output_root=tmp_path / "outbox-raw-sha",
    )
    config = FeedbackConfig(
        endpoint="https://feedback.example.invalid/v1/feedback",
        recipient_label="PeerBridge test support",
    )
    monkeypatch.setattr(
        "peerbridge_mcp.feedback.urllib.request.build_opener",
        lambda *_args: _DeliveryOpener(
            {
                "case_id": bundle.case_id,
                "bundle_sha256": "0" * 64,
                "receipt": "wrong",
            }
        ),
    )

    with pytest.raises(FeedbackError, match="mismatched bundle SHA-256"):
        deliver_feedback_bundle(bundle, config)

    monkeypatch.setattr(
        "peerbridge_mcp.feedback.urllib.request.build_opener",
        lambda *_args: _DeliveryOpener(
            {
                "case_id": bundle.case_id,
                "bundle_sha256": bundle.sha256,
                "receipt": "stored",
            }
        ),
    )
    assert deliver_feedback_bundle(bundle, config)["delivered"] is True


def test_delivery_rejects_bundle_tamper_and_mismatched_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = create_feedback_bundle(
        tmp_path,
        summary="Delivery binding",
        message="Receipt must bind the exact case.",
        output_root=tmp_path / "outbox",
    )
    config = FeedbackConfig(
        endpoint="https://support.example.invalid/v1/feedback",
        recipient_label="PeerBridge test support",
    )
    bundle.path.write_bytes(bundle.path.read_bytes() + b"tamper")
    with pytest.raises(FeedbackError, match="changed after it was sealed"):
        deliver_feedback_bundle(bundle, config)

    fresh = create_feedback_bundle(
        tmp_path,
        summary="Receipt binding",
        message="Wrong case must fail.",
        output_root=tmp_path / "outbox-2",
    )
    monkeypatch.setattr(
        "peerbridge_mcp.feedback.urllib.request.build_opener",
        lambda *_args: _DeliveryOpener({"case_id": "wrong-case", "receipt": "no"}),
    )
    with pytest.raises(FeedbackError, match="mismatched case ID"):
        deliver_feedback_bundle(fresh, config)


def test_archive_contains_no_plaintext_secret_in_any_member(tmp_path: Path) -> None:
    public_path, _private_key = _support_key_pair(tmp_path)
    secret = _fake_provider_secret("one-shot-full-key-that-must-only-be-ciphertext")
    bundle = create_feedback_bundle(
        tmp_path,
        summary="One-shot credential escalation",
        message="The complete key is included once and encrypted locally.",
        credential_input=secret,
        include_encrypted_credential=True,
        config=_key_config(public_path),
        output_root=tmp_path / "outbox",
    )
    members = _zip_members(bundle.path)
    report = json.loads(members["report.json"])

    assert all(secret.encode("utf-8") not in payload for payload in members.values())
    _assert_report_has_no_credential_metadata(report)
