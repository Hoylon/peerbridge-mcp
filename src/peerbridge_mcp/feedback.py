"""Provider-independent private feedback bundles and optional secret escalation."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .attachments import AttachmentError, read_stable_attachment_source
from .image_validation import image_payload_is_valid
from .secret_scan import contains_secret, decode_text_bytes, redact_secrets


FEEDBACK_CONFIG_SCHEMA = "peerbridge.feedback-config.v1"
FEEDBACK_REPORT_SCHEMA = "peerbridge.feedback-report.v1"
FEEDBACK_SECRET_SCHEMA = "peerbridge.feedback-secret-envelope.v1"
FEEDBACK_RESULT_SCHEMA = "peerbridge.feedback-result.v1"
FEEDBACK_UPLOAD_SCHEMA = "peerbridge.feedback-upload.v1"
PACKAGED_SUPPORT_CONFIG_SHA256 = (
    "73f586cfeadea78cd530d3bde2f3c11452c86a050eb95005cbd358e9400032e3"
)
PACKAGED_SUPPORT_PUBLIC_KEY_SHA256 = (
    "6c99aa67bf12d01cd8c231b67533f311a98de710ee4263d72358b9b97010ef53"
)

RAW_ZIP_TRANSPORT = "raw-zip-v1"
JSON_BASE64_TRANSPORT = "json-base64-v1"
SUPPORTED_ENDPOINT_TRANSPORTS = frozenset({RAW_ZIP_TRANSPORT, JSON_BASE64_TRANSPORT})

MAX_SUMMARY_CHARS = 240
MAX_MESSAGE_CHARS = 20_000
MAX_CONTACT_CHARS = 320
MAX_ATTACHMENT_COUNT = 5
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_OUTBOX_BUNDLES = 200
MAX_OUTBOX_BYTES = 512 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CREDENTIAL_CHARS = 16_384
ALLOWED_ATTACHMENT_SUFFIXES = frozenset(
    {".gif", ".jpeg", ".jpg", ".json", ".log", ".png", ".txt", ".webp"}
)
_TEXT_ATTACHMENT_SUFFIXES = frozenset({".json", ".log", ".txt"})

_WINDOWS_HOME_PATTERN = re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s]+")
_POSIX_HOME_PATTERN = re.compile(r"(?i)(?<![A-Za-z0-9])/(?:home|Users)/[^/\s]+")


class FeedbackError(RuntimeError):
    """A feedback bundle could not be safely prepared or delivered."""


@dataclass(frozen=True)
class FeedbackConfig:
    endpoint: str | None = None
    endpoint_transport: str = RAW_ZIP_TRANSPORT
    support_email: str | None = None
    public_key_path: Path | None = None
    public_key_sha256: str | None = None
    recipient_label: str | None = None
    privacy_url: str | None = None

    def __post_init__(self) -> None:
        endpoint = _optional_https_url(self.endpoint, "feedback endpoint")
        endpoint_transport = str(self.endpoint_transport or RAW_ZIP_TRANSPORT).strip()
        if endpoint_transport not in SUPPORTED_ENDPOINT_TRANSPORTS:
            raise FeedbackError("feedback endpoint transport is not supported")
        if endpoint_transport != RAW_ZIP_TRANSPORT and not endpoint:
            raise FeedbackError("feedback endpoint transport requires an endpoint")
        privacy_url = _optional_https_url(self.privacy_url, "privacy URL")
        support_email = _optional_email(self.support_email)
        recipient = str(self.recipient_label or "").strip() or None
        if recipient and len(recipient) > 120:
            raise FeedbackError("support recipient label is too long")
        key_path = Path(self.public_key_path).resolve() if self.public_key_path else None
        fingerprint = str(self.public_key_sha256 or "").strip().lower() or None
        if key_path is not None:
            if not key_path.is_file() or key_path.suffix.lower() not in {".pem", ".pub"}:
                raise FeedbackError("support public key is unavailable")
            if not fingerprint or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                raise FeedbackError("support public key requires a pinned SHA-256 fingerprint")
            observed = hashlib.sha256(key_path.read_bytes()).hexdigest()
            if not secrets.compare_digest(observed, fingerprint):
                raise FeedbackError("support public key does not match its pinned fingerprint")
        elif fingerprint:
            raise FeedbackError("support public key fingerprint has no key file")
        if (endpoint or key_path or support_email) and not recipient:
            raise FeedbackError("support recipient label is required")
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "endpoint_transport", endpoint_transport)
        object.__setattr__(self, "privacy_url", privacy_url)
        object.__setattr__(self, "support_email", support_email)
        object.__setattr__(self, "recipient_label", recipient)
        object.__setattr__(self, "public_key_path", key_path)
        object.__setattr__(self, "public_key_sha256", fingerprint)

    @classmethod
    def load(cls) -> "FeedbackConfig":
        """Load only the maintainer-pinned configuration shipped in the package.

        The open project directory is intentionally never consulted here. Otherwise
        an untrusted checkout could redirect feedback or replace the support key.
        Maintainer tooling and tests may use ``load_from_file`` explicitly.
        """
        packaged_support = Path(__file__).resolve().parent / "release_support"
        path = packaged_support / "support.json"
        if not path.exists():
            return cls()
        try:
            config_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise FeedbackError("packaged support configuration is unavailable") from exc
        if not secrets.compare_digest(config_sha256, PACKAGED_SUPPORT_CONFIG_SHA256):
            raise FeedbackError("packaged support configuration trust anchor mismatch")
        public_key_path = packaged_support / "peerbridge-support-public.pub"
        try:
            public_key_sha256 = hashlib.sha256(public_key_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise FeedbackError("packaged support public key is unavailable") from exc
        if not secrets.compare_digest(
            public_key_sha256,
            PACKAGED_SUPPORT_PUBLIC_KEY_SHA256,
        ):
            raise FeedbackError("packaged support public key trust anchor mismatch")
        return cls.load_from_file(path)

    @classmethod
    def load_from_file(cls, path: Path) -> "FeedbackConfig":
        """Load an explicitly selected support configuration.

        This is an opt-in maintainer/test API. Relative key paths are confined to
        the directory containing the selected configuration file.
        """
        path = path.resolve()
        config_base = path.parent
        allowed_support_root = path.parent
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FeedbackError("feedback support configuration is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema") != FEEDBACK_CONFIG_SCHEMA:
            raise FeedbackError("feedback support configuration has an unsupported schema")
        allowed = {
            "schema",
            "endpoint",
            "endpoint_transport",
            "support_email",
            "public_key_path",
            "public_key_sha256",
            "recipient_label",
            "privacy_url",
        }
        if set(payload) - allowed:
            raise FeedbackError("feedback support configuration contains unsupported fields")
        endpoint = _optional_https_url(payload.get("endpoint"), "feedback endpoint")
        privacy_url = _optional_https_url(payload.get("privacy_url"), "privacy URL")
        support_email = _optional_email(payload.get("support_email"))
        public_key_path = _optional_public_key_path(
            config_base,
            allowed_support_root,
            payload.get("public_key_path"),
        )
        return cls(
            endpoint=endpoint,
            endpoint_transport=payload.get("endpoint_transport") or RAW_ZIP_TRANSPORT,
            support_email=support_email,
            public_key_path=public_key_path,
            public_key_sha256=payload.get("public_key_sha256"),
            recipient_label=payload.get("recipient_label"),
            privacy_url=privacy_url,
        )

    @property
    def encrypted_secret_available(self) -> bool:
        return bool(
            self.public_key_path
            and self.public_key_sha256
            and importlib.util.find_spec("cryptography") is not None
        )


@dataclass(frozen=True)
class FeedbackBundle:
    case_id: str
    path: Path
    sha256: str
    report: dict[str, Any]
    encrypted_secret_included: bool


def _optional_https_url(value: Any, label: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FeedbackError(f"{label} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise FeedbackError(f"{label} must not contain credentials, a query, or a fragment")
    hostname = parsed.hostname.lower()
    if hostname.endswith("."):
        raise FeedbackError(f"{label} must not use a trailing-dot hostname")
    if hostname in {"internal", "local", "localhost", "localhost.localdomain"} or hostname.endswith(
        (".local", ".internal", ".localhost")
    ):
        raise FeedbackError(f"{label} must not target a local hostname")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise FeedbackError(f"{label} must not target a private or non-global IP address")
    return urllib.parse.urlunsplit(parsed)


def _optional_email(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > 320 or text.count("@") != 1 or any(ch.isspace() for ch in text):
        raise FeedbackError("support email address is invalid")
    return text


def _optional_public_key_path(
    config_base: Path,
    allowed_support_root: Path,
    value: Any,
) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        path = config_base.resolve() / path
    path = path.resolve()
    if path.suffix.lower() not in {".pem", ".pub"}:
        raise FeedbackError("support public key must be a PEM public-key file")
    support_root = allowed_support_root.resolve()
    try:
        path.relative_to(support_root)
    except ValueError as exc:
        raise FeedbackError("support public key must be inside the release support directory") from exc
    return path


def redact_feedback_text(value: Any) -> str:
    """Remove likely credential values and user-home segments from normal diagnostics."""
    text = redact_secrets(value)
    text = _WINDOWS_HOME_PATTERN.sub("%USERPROFILE%", text)
    return _POSIX_HOME_PATTERN.sub("$HOME", text)


def collect_runtime_diagnostics(
    project_root: Path,
    *,
    locale: str,
    parser_stage: str | None = None,
    failure_text: str | None = None,
) -> dict[str, Any]:
    if os.name == "nt":
        os_family = "windows"
        try:
            windows = sys.getwindowsversion()
            os_release = f"{windows.major}.{windows.minor}.{windows.build}"
        except (AttributeError, OSError):
            os_release = "unknown"
        machine = os.environ.get("PROCESSOR_ARCHITECTURE", "unknown")
    else:
        os_family = os.name or "unknown"
        try:
            uname = os.uname()
            os_family = uname.sysname.lower() or os_family
            os_release = uname.release or "unknown"
            machine = uname.machine or "unknown"
        except (AttributeError, OSError):
            os_release = "unknown"
            machine = "unknown"
    return {
        "app_version": __version__,
        "locale": str(locale or "en")[:32],
        "os_family": redact_feedback_text(os_family)[:80],
        "os_release": redact_feedback_text(os_release)[:120],
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "machine": redact_feedback_text(machine)[:80],
        "project_root_exists": project_root.resolve().is_dir(),
        "parser_stage": redact_feedback_text(parser_stage or "unspecified")[:120],
        "failure_text": redact_feedback_text(failure_text or "")[:2000],
    }


def _validate_text(value: Any, label: str, maximum: int, *, required: bool) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise FeedbackError(f"{label} is required")
    if len(text) > maximum:
        raise FeedbackError(f"{label} is too long")
    return redact_feedback_text(text)


def _decode_text_attachment(extension: str, payload: bytes) -> str | None:
    text = decode_text_bytes(payload)
    if text is None:
        return None
    if extension == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return None
    return text


def _attachment_payload_is_valid(extension: str, payload: bytes) -> bool:
    if extension in {".gif", ".jpeg", ".jpg", ".png", ".webp"}:
        return image_payload_is_valid(extension, payload)
    return extension in _TEXT_ATTACHMENT_SUFFIXES and _decode_text_attachment(
        extension, payload
    ) is not None


def _validate_attachments(
    paths: Iterable[Path], *, explicit_user_consent: bool
) -> list[dict[str, Any]]:
    selected = list(paths)
    if selected and not explicit_user_consent:
        raise FeedbackError("feedback attachments require explicit user selection")
    if len(selected) > MAX_ATTACHMENT_COUNT:
        raise FeedbackError("too many feedback attachments")
    total = 0
    result: list[dict[str, Any]] = []
    for index, raw_path in enumerate(selected, start=1):
        try:
            source = read_stable_attachment_source(
                Path(raw_path),
                allowed_suffixes=ALLOWED_ATTACHMENT_SUFFIXES,
                maximum_bytes=MAX_ATTACHMENT_BYTES,
            )
        except AttachmentError as exc:
            raise FeedbackError(
                str(exc).replace("selected file", "feedback attachment")
            ) from exc
        resolved = source.path
        payload = source.payload
        size = len(payload)
        total += size
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise FeedbackError("feedback attachments exceed the total size limit")
        extension = source.suffix
        canonical_text = (
            _decode_text_attachment(extension, payload)
            if extension in _TEXT_ATTACHMENT_SUFFIXES
            else None
        )
        payload_is_valid = (
            canonical_text is not None
            if extension in _TEXT_ATTACHMENT_SUFFIXES
            else _attachment_payload_is_valid(extension, payload)
        )
        if not payload_is_valid:
            raise FeedbackError("feedback attachment content does not match its safe type")
        if canonical_text is not None and contains_secret(canonical_text):
            raise FeedbackError(
                "feedback text attachment appears to contain a credential; "
                "use the explicit encrypted credential field"
            )
        archive_name = f"attachments/{index:02d}{extension}"
        result.append(
            {
                "path": resolved,
                "archive_name": archive_name,
                "original_name": redact_feedback_text(resolved.name)[:240],
                "bytes": size,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "payload": payload,
            }
        )
    return result


def _encrypt_secret(
    secret: str,
    public_key_path: Path,
    pinned_public_key_sha256: str,
    case_id: str,
) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise FeedbackError(
            "encrypted credential escalation requires the PeerBridge feedback extra"
        ) from exc
    try:
        public_key_bytes = public_key_path.read_bytes()
        observed_pem_sha256 = hashlib.sha256(public_key_bytes).hexdigest()
        if not secrets.compare_digest(observed_pem_sha256, pinned_public_key_sha256):
            raise FeedbackError("support public key changed after configuration validation")
        public_key = serialization.load_pem_public_key(public_key_bytes)
    except FeedbackError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise FeedbackError("support public key could not be loaded") from exc
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 3072:
        raise FeedbackError("support public key must be RSA with at least 3072 bits")
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(12)
    associated_data = f"{FEEDBACK_SECRET_SCHEMA}:{case_id}".encode("ascii")
    ciphertext = AESGCM(data_key).encrypt(nonce, secret.encode("utf-8"), associated_data)
    wrapped_key = public_key.encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    public_der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    envelope = {
        "schema": FEEDBACK_SECRET_SCHEMA,
        "case_id": case_id,
        "algorithm": "RSA-OAEP-SHA256+A256GCM",
        "public_key_sha256": hashlib.sha256(public_der).hexdigest(),
        "associated_data_b64": base64.b64encode(associated_data).decode("ascii"),
        "wrapped_key_b64": base64.b64encode(wrapped_key).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }
    return (json.dumps(envelope, indent=2, sort_keys=True) + "\n").encode("utf-8")


def run_feedback_encryption_self_test(
    config: FeedbackConfig | None = None,
) -> dict[str, str]:
    """Exercise the packaged crypto path using a fixed, non-secret payload."""
    support = config or FeedbackConfig.load()
    if not support.encrypted_secret_available:
        raise FeedbackError("packaged feedback encryption is unavailable")
    assert support.public_key_path is not None
    assert support.public_key_sha256 is not None
    synthetic = "PEERBRIDGE-ENCRYPTION-SELF-TEST-NON-SECRET"
    payload = _encrypt_secret(
        synthetic,
        support.public_key_path,
        support.public_key_sha256,
        "feedback-encryption-self-test",
    )
    if synthetic.encode("utf-8") in payload:
        raise FeedbackError("feedback encryption self-test leaked plaintext")
    envelope = json.loads(payload.decode("utf-8"))
    if envelope.get("schema") != FEEDBACK_SECRET_SCHEMA:
        raise FeedbackError("feedback encryption self-test returned the wrong schema")
    return {
        "status": "PASS",
        "schema": str(envelope["schema"]),
        "algorithm": str(envelope["algorithm"]),
        "configured_pem_sha256": support.public_key_sha256,
    }


def create_feedback_bundle(
    project_root: Path,
    *,
    summary: str,
    message: str,
    contact: str = "",
    locale: str = "en",
    parser_stage: str = "",
    failure_text: str = "",
    credential_input: str = "",
    include_encrypted_credential: bool = False,
    attachment_paths: Iterable[Path] = (),
    attachment_consent: bool = False,
    config: FeedbackConfig | None = None,
    output_root: Path | None = None,
) -> FeedbackBundle:
    root = project_root.resolve()
    support = config or FeedbackConfig.load()
    normalized_summary = _validate_text(summary, "feedback summary", MAX_SUMMARY_CHARS, required=True)
    normalized_message = _validate_text(message, "feedback message", MAX_MESSAGE_CHARS, required=True)
    normalized_contact = _validate_text(contact, "feedback contact", MAX_CONTACT_CHARS, required=False)
    if len(credential_input) > MAX_CREDENTIAL_CHARS:
        raise FeedbackError("credential input exceeds the local safety limit")
    attachments = _validate_attachments(
        attachment_paths, explicit_user_consent=attachment_consent
    )
    case_id = uuid.uuid4().hex
    created_utc = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    report = {
        "schema": FEEDBACK_REPORT_SCHEMA,
        "case_id": case_id,
        "created_utc": created_utc,
        "summary": normalized_summary,
        "message": normalized_message,
        "contact": normalized_contact,
        "runtime": collect_runtime_diagnostics(
            root,
            locale=locale,
            parser_stage=parser_stage,
            failure_text=failure_text,
        ),
        "encrypted_credential_included": bool(include_encrypted_credential),
        "attachments": [
            {
                key: value
                for key, value in item.items()
                if key not in {"path", "payload"}
            }
            for item in attachments
        ],
        "privacy": {
            "provider_independent": True,
            "plaintext_credential_persisted": False,
            "normal_diagnostics_redacted": True,
        },
    }
    encrypted_secret = None
    if include_encrypted_credential:
        if not credential_input:
            raise FeedbackError("a credential must be provided before encrypted escalation")
        if not support.public_key_path or not support.public_key_sha256:
            raise FeedbackError("encrypted credential escalation is not configured")
        encrypted_secret = _encrypt_secret(
            credential_input,
            support.public_key_path,
            support.public_key_sha256,
            case_id,
        )
    if output_root is None:
        destination_root = _feedback_outbox(root)
    else:
        destination_root = output_root.resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        if _is_reparse(destination_root):
            raise FeedbackError("feedback output root must not be a filesystem link")
    with contextlib.suppress(OSError):
        os.chmod(destination_root, 0o700)
    existing_bundles = []
    existing_bytes = 0
    for candidate in destination_root.glob("peerbridge-feedback-*.zip"):
        if _is_reparse(candidate) or not candidate.is_file():
            raise FeedbackError("feedback outbox contains an unsafe entry")
        existing_bundles.append(candidate)
        existing_bytes += int(candidate.stat().st_size)
        if len(existing_bundles) >= MAX_OUTBOX_BUNDLES:
            raise FeedbackError("feedback outbox quota exceeded")
    destination = destination_root / f"peerbridge-feedback-{case_id}.zip"
    temporary = destination_root / f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        with zipfile.ZipFile(
            temporary, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            archive.writestr(
                "report.json",
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            if encrypted_secret is not None:
                archive.writestr("encrypted-credential.json", encrypted_secret)
            for attachment in attachments:
                archive.writestr(attachment["archive_name"], attachment["payload"])
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        if existing_bytes + int(temporary.stat().st_size) > MAX_OUTBOX_BYTES:
            raise FeedbackError("feedback outbox quota exceeded")
        if destination.exists():
            raise FeedbackError("feedback bundle destination already exists")
        os.replace(temporary, destination)
        with contextlib.suppress(OSError):
            os.chmod(destination, 0o600)
    except Exception:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
        raise
    return FeedbackBundle(
        case_id=case_id,
        path=destination,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        report=report,
        encrypted_secret_included=encrypted_secret is not None,
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _json_base64_payload(bundle: FeedbackBundle, bundle_payload: bytes) -> bytes:
    payload = {
        "schema": FEEDBACK_UPLOAD_SCHEMA,
        "case_id": bundle.case_id,
        "bundle_sha256": bundle.sha256,
        "bundle_base64": base64.b64encode(bundle_payload).decode("ascii"),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def deliver_feedback_bundle(
    bundle: FeedbackBundle,
    config: FeedbackConfig,
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    if not config.endpoint:
        return {
            "schema": FEEDBACK_RESULT_SCHEMA,
            "delivered": False,
            "case_id": bundle.case_id,
            "reason": "private_endpoint_not_configured",
            "bundle_path": str(bundle.path),
            "bundle_sha256": bundle.sha256,
        }
    bundle_payload = bundle.path.read_bytes()
    observed_sha256 = hashlib.sha256(bundle_payload).hexdigest()
    if not secrets.compare_digest(observed_sha256, bundle.sha256):
        raise FeedbackError("feedback bundle changed after it was sealed")
    if config.endpoint_transport == JSON_BASE64_TRANSPORT:
        payload = _json_base64_payload(bundle, bundle_payload)
        content_type = "application/json; charset=utf-8"
        redirect_handler: urllib.request.BaseHandler = _NoRedirect()
    else:
        payload = bundle_payload
        content_type = "application/zip"
        redirect_handler = _NoRedirect()
    request = urllib.request.Request(
        config.endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(payload)),
            "User-Agent": f"PeerBridge/{__version__}",
            "X-PeerBridge-Case": bundle.case_id,
            "X-PeerBridge-SHA256": bundle.sha256,
        },
    )
    opener = urllib.request.build_opener(redirect_handler)
    try:
        with opener.open(request, timeout=max(1.0, min(timeout_seconds, 60.0))) as response:
            status = int(getattr(response, "status", 0))
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise FeedbackError(
            f"private feedback delivery failed: {redact_feedback_text(type(exc).__name__)}"
        ) from exc
    if status < 200 or status >= 300 or len(body) > MAX_RESPONSE_BYTES:
        raise FeedbackError("private feedback endpoint returned an invalid response")
    try:
        response_payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedbackError("private feedback endpoint returned invalid JSON") from exc
    if not isinstance(response_payload, dict):
        raise FeedbackError("private feedback endpoint returned an invalid receipt")
    received_case = str(response_payload.get("case_id") or "")
    if received_case != bundle.case_id:
        raise FeedbackError("private feedback endpoint returned a mismatched case ID")
    received_sha256 = str(response_payload.get("bundle_sha256") or "")
    if not secrets.compare_digest(received_sha256, bundle.sha256):
        raise FeedbackError("private feedback endpoint returned a mismatched bundle SHA-256")
    notification_value = response_payload.get("notification_sent")
    notification_sent = (
        notification_value if isinstance(notification_value, bool) else None
    )
    return {
        "schema": FEEDBACK_RESULT_SCHEMA,
        "delivered": True,
        "case_id": bundle.case_id,
        "bundle_sha256": bundle.sha256,
        "receipt": redact_feedback_text(response_payload.get("receipt") or "")[:240],
        "notification_sent": notification_sent,
    }


def feedback_mailto(config: FeedbackConfig, bundle: FeedbackBundle) -> str | None:
    if not config.support_email:
        return None
    subject = f"PeerBridge feedback {bundle.case_id}"
    body = (
        f"Case: {bundle.case_id}\n"
        f"Bundle SHA-256: {bundle.sha256}\n"
        "The local feedback bundle is saved on this computer. If present, only the "
        "credential member is encrypted; attach the ZIP to this private message."
    )
    return "mailto:" + config.support_email + "?" + urllib.parse.urlencode(
        {"subject": subject, "body": body}
    )
def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _feedback_outbox(root: Path) -> Path:
    current = root.resolve()
    for name in (".peerbridge", "feedback", "outbox"):
        if _is_reparse(current):
            raise FeedbackError("feedback outbox crosses a filesystem link")
        child = current / name
        child.mkdir(exist_ok=True)
        if _is_reparse(child) or child.resolve().parent != current.resolve():
            raise FeedbackError("feedback outbox crosses a filesystem link")
        current = child
    return current
