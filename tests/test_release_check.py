from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Mapping, Sequence

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_check.py"
SPEC = importlib.util.spec_from_file_location("peerbridge_release_check", SCRIPT)
assert SPEC and SPEC.loader
release_check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_check
SPEC.loader.exec_module(release_check)


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in release_check.REQUIRED - {"pyproject.toml"}:
        (root / name).write_text(f"# {name}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "sample-pkg"
version = "1.2.3"

[project.scripts]
sample = "sample_pkg.cli:main"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
""",
        encoding="utf-8",
    )
    package = root / "src" / "sample_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    (package / "cli.py").write_text(
        "def main() -> int:\n    return 0\n", encoding="utf-8"
    )
    write_remote_evidence(root)
    return root


def run_git(root: Path, *arguments: str) -> None:
    completed = release_check.subprocess.run(
        ["git", *arguments],
        cwd=root,
        stdin=release_check.subprocess.DEVNULL,
        stdout=release_check.subprocess.PIPE,
        stderr=release_check.subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def init_git_project(root: Path, *, create_tag: bool = True) -> None:
    (root / ".gitignore").write_text(".peerbridge/\nignored-private/\n", encoding="utf-8")
    commands = (
        ["git", "init"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "config", "user.name", "PeerBridge Test"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "add", "--all"],
        ["git", "commit", "-m", "test fixture"],
    )
    for command in commands:
        run_git(root, *command[1:])
    if create_tag:
        run_git(root, "tag", "-a", "v1.2.3", "-m", "test release")


def _write_json(path: Path, value: object) -> bytes:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _artifact_descriptor(path: Path, root: Path, command: list[str]) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "capture_command": command,
        "exit_code": 0,
    }


def write_remote_evidence(root: Path) -> Path:
    evidence = root / ".peerbridge" / "evidence" / "remote-mobile-v2"
    origin = "https://peerbridge-phone.tail0001.ts.net"
    backend = "http://127.0.0.1:8765"
    identity = "1" * 64
    serve_status_value = {"Web": {origin: {"Handlers": {"/": {"Proxy": backend}}}}}
    serve_status = _write_json(evidence / "serve-status.json", serve_status_value)
    paths = {
        "serve_status": evidence / "serve-status.json",
        "serve_state": evidence / "serve-state.json",
        "funnel_status": evidence / "funnel-status.json",
        "network_observation": evidence / "network-observation.json",
        "browser_trace": evidence / "browser-trace.json",
        "audit_verification": evidence / "audit-verification.json",
    }
    _write_json(
        paths["serve_state"],
        {
            "scope": "mobile-release",
            "public_origin": origin,
            "local_backend": backend,
            "transport": "tailscale-serve",
            "tailnet_only": True,
            "funnel_enabled": False,
            "validated_serve_status_sha256": hashlib.sha256(serve_status).hexdigest(),
            "configured_utc": "2026-08-12T01:00:00Z",
        },
    )
    _write_json(paths["funnel_status"], {"TCP": {}, "Web": {}})
    _write_json(
        paths["network_observation"],
        {
            "schema": "peerbridge.remote-network-observation.v1",
            "captured_at_utc": "2026-08-12T01:01:00Z",
            "backend_listener": {"address": "127.0.0.1", "port": 8765, "process_id": 4321},
            "non_loopback_listeners_on_backend_port": [],
        },
    )
    _write_json(
        paths["browser_trace"],
        {
            "schema": "peerbridge.mobile-browser-reconnect-trace.v2",
            "test_mode": False,
            "evidence_origin": "real-device",
            "device_class": "phone",
            "scope": "mobile-release",
            "public_origin": origin,
            "tailnet_identity_sha256": identity,
            "browser_device_continuity_source": "browser-local-storage-random-nonce",
            "network_layer_node_identity_attested": False,
            "viewport": {"width": 390, "height": 844, "max_touch_points": 5},
            "sessions": [
                {
                    "phase": "initial",
                    "connected_at_utc": "2026-08-12T01:02:00Z",
                    "authenticated": True,
                    "transport": "tailnet-https",
                    "identity_source": "Tailscale-User-Login",
                    "page_status": 200,
                    "snapshot_status": 200,
                    "browser_session_id_sha256": "3" * 64,
                    "browser_device_continuity_sha256": "4" * 64,
                    "tailnet_identity_sha256": identity,
                    "user_agent_sha256": "2" * 64,
                    "instance_id": "remote-stable",
                    "snapshot_signature": "8" * 64,
                },
                {
                    "phase": "reconnect",
                    "connected_at_utc": "2026-08-12T01:04:00Z",
                    "authenticated": True,
                    "transport": "tailnet-https",
                    "identity_source": "Tailscale-User-Login",
                    "page_status": 200,
                    "snapshot_status": 200,
                    "browser_session_id_sha256": "5" * 64,
                    "browser_device_continuity_sha256": "4" * 64,
                    "tailnet_identity_sha256": identity,
                    "user_agent_sha256": "2" * 64,
                    "instance_id": "remote-stable",
                    "snapshot_signature": "9" * 64,
                    "observed_message_id": "mobile-message-1",
                },
            ],
            "disconnected_at_utc": "2026-08-12T01:03:00Z",
            "disconnect_evidence": {
                "method": "operator-marked-disconnect-plus-fresh-browser-session",
                "challenge_sha256": "a" * 64,
                "minimum_gap_seconds": 10,
                "observed_gap_seconds": 60,
                "network_layer_disconnect_cryptographically_proven": False,
            },
            "message": {
                "status": 201,
                "message_id": "mobile-message-1",
                "content_sha256": "6" * 64,
            },
        },
    )
    _write_json(
        paths["audit_verification"],
        {
            "schema": "peerbridge.remote-audit-verification.v1",
            "scope": "mobile-release",
            "valid": True,
            "verified_at_utc": "2026-08-12T01:05:00Z",
            "audit_head_sha256": "7" * 64,
            "event_count": 12,
            "message_id": "mobile-message-1",
        },
    )
    commands = {
        "serve_state": ["read-file", ".peerbridge/remote-control-serve.json"],
        "serve_status": ["tailscale", "serve", "status", "--json"],
        "funnel_status": ["tailscale", "funnel", "status", "--json"],
        "network_observation": ["powershell", "Get-NetTCPConnection", "-LocalPort", "8765"],
        "browser_trace": ["phone-browser", "export-peerbridge-trace"],
        "audit_verification": ["peerbridge", "verify-audit", "--scope", "mobile-release"],
    }
    receipt: dict[str, object] = {
        "schema": release_check.REMOTE_RECEIPT_SCHEMA,
        "created_at_utc": "2026-08-12T01:06:00Z",
        "scope": "mobile-release",
        "public_origin": origin,
        "local_backend": backend,
        "tailnet_only": True,
        "funnel_enabled": False,
        "test_mode": False,
        "evidence_origin": "real-device",
        "source_tree_sha256": release_check.release_source_tree_sha256(root),
        "artifacts": {
            name: _artifact_descriptor(path, root, commands[name])
            for name, path in paths.items()
        },
    }
    receipt["receipt_sha256"] = release_check._canonical_sha256(receipt)
    return _write_json(
        root / release_check.REMOTE_RECEIPT_DEFAULT,
        receipt,
    ) and root / release_check.REMOTE_RECEIPT_DEFAULT


def mutate_remote_artifact(root: Path, name: str, mutation: object) -> None:
    receipt_path = root / release_check.REMOTE_RECEIPT_DEFAULT
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    descriptor = receipt["artifacts"][name]
    path = root / descriptor["path"]
    value = json.loads(path.read_text(encoding="utf-8"))
    assert callable(mutation)
    mutation(value)
    payload = _write_json(path, value)
    descriptor["bytes"] = len(payload)
    descriptor["sha256"] = hashlib.sha256(payload).hexdigest()
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = release_check._canonical_sha256(receipt)
    _write_json(receipt_path, receipt)


def entry_points(scripts: Mapping[str, str]) -> str:
    lines = ["[console_scripts]"]
    lines.extend(f"{name} = {target}" for name, target in sorted(scripts.items()))
    return "\n".join(lines) + "\n"


def add_tar_text(archive: tarfile.TarFile, name: str, text: str) -> None:
    add_tar_bytes(archive, name, text.encode("utf-8"))


def add_tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mtime = 1
    archive.addfile(member, io.BytesIO(payload))


def write_artifacts(
    root: Path,
    output: Path,
    *,
    wheel_modules: Sequence[str] | None = None,
    sdist_modules: Sequence[str] | None = None,
    wheel_package_data: Sequence[str] | None = None,
    sdist_package_data: Sequence[str] | None = None,
    sdist_root_files: Sequence[str] | None = None,
    wheel_legal_files: Sequence[str] | None = None,
    artifact_scripts: Mapping[str, str] | None = None,
    artifact_version: str | None = None,
) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    info = release_check.load_project_info(root)
    version = artifact_version or info.version
    scripts = dict(info.scripts if artifact_scripts is None else artifact_scripts)
    wheel_module_names = list(info.source_modules if wheel_modules is None else wheel_modules)
    sdist_module_names = list(info.source_modules if sdist_modules is None else sdist_modules)
    wheel_data_names = list(
        info.package_data if wheel_package_data is None else wheel_package_data
    )
    sdist_data_names = list(
        info.package_data if sdist_package_data is None else sdist_package_data
    )
    root_names = list(
        (
            "BRAND_ASSETS.md",
            "LICENSE",
            "MANIFEST.in",
            "README.md",
            "THIRD_PARTY_NOTICES.md",
            "TRADEMARKS.md",
            "pyproject.toml",
            "PKG-INFO",
        )
        if sdist_root_files is None
        else sdist_root_files
    )
    distribution = release_check._normalized_distribution(info.name)
    wheel = output / f"{distribution}-{version}-py3-none-any.whl"
    dist_info = f"{distribution}-{version}.dist-info"

    def write_wheel_member(
        archive: zipfile.ZipFile, name: str, payload: bytes | str
    ) -> None:
        member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        member.compress_type = zipfile.ZIP_STORED
        member.external_attr = 0o100644 << 16
        archive.writestr(member, payload)

    with zipfile.ZipFile(wheel, "w") as archive:
        for module in wheel_module_names:
            source = info.source_base / module
            payload = source.read_bytes() if source.is_file() else b"stale = True\n"
            write_wheel_member(archive, module, payload)
        for relative in wheel_data_names:
            source = info.source_base / relative
            write_wheel_member(
                archive,
                relative,
                source.read_bytes() if source.is_file() else b"stale-package-data",
            )
        write_wheel_member(
            archive,
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.4\nName: {info.name}\nVersion: {version}\n",
        )
        write_wheel_member(
            archive, f"{dist_info}/entry_points.txt", entry_points(scripts)
        )
        legal_names = (
            (
                "LICENSE",
                "BRAND_ASSETS.md",
                "THIRD_PARTY_NOTICES.md",
                "TRADEMARKS.md",
            )
            if wheel_legal_files is None
            else wheel_legal_files
        )
        for legal_name in legal_names:
            write_wheel_member(
                archive,
                f"{dist_info}/licenses/{legal_name}",
                (root / legal_name).read_bytes(),
            )

    sdist = output / f"{distribution}-{version}.tar.gz"
    archive_root = f"{distribution}-{version}"
    source_relative = info.source_base.relative_to(root).as_posix()
    with tarfile.open(sdist, "w:gz") as archive:
        for name in root_names:
            if name == "PKG-INFO":
                text = f"Metadata-Version: 2.4\nName: {info.name}\nVersion: {version}\n"
            elif name == "pyproject.toml":
                if version == info.version:
                    add_tar_bytes(archive, f"{archive_root}/{name}", (root / name).read_bytes())
                    continue
                text = (root / name).read_text(encoding="utf-8").replace(
                    'version = "1.2.3"', f'version = "{version}"'
                )
            else:
                source = root / name
                if source.is_file():
                    add_tar_bytes(archive, f"{archive_root}/{name}", source.read_bytes())
                    continue
                text = "stale\n"
            add_tar_text(archive, f"{archive_root}/{name}", text)
        add_tar_text(
            archive,
            f"{archive_root}/{source_relative}/{distribution}.egg-info/entry_points.txt",
            entry_points(scripts),
        )
        for module in sdist_module_names:
            source = info.source_base / module
            payload = source.read_bytes() if source.is_file() else b"stale = True\n"
            if module == f"{distribution}/__init__.py" and version != info.version:
                payload = f'__version__ = "{version}"\n'.encode("utf-8")
            add_tar_bytes(
                archive,
                f"{archive_root}/{source_relative}/{module}",
                payload,
            )
        for relative in sdist_data_names:
            source = info.source_base / relative
            payload = source.read_bytes() if source.is_file() else b"stale-package-data"
            member = tarfile.TarInfo(
                f"{archive_root}/{source_relative}/{relative}"
            )
            member.size = len(payload)
            member.mtime = 1
            archive.addfile(member, io.BytesIO(payload))
    # Keep the fixture byte-reproducible across wall-clock second boundaries. The
    # production builder applies the same normalization after backend output.
    release_check._normalize_sdist(sdist, 1)
    return [sdist, wheel]


def append_sdist_member(
    path: Path,
    member: tarfile.TarInfo,
    payload: bytes | None = None,
) -> None:
    replacement = path.with_name(path.name + ".replacement")
    with tarfile.open(path, "r:gz") as source, tarfile.open(replacement, "w:gz") as target:
        for existing in source.getmembers():
            stream = source.extractfile(existing) if existing.isfile() else None
            target.addfile(existing, stream)
        target.addfile(member, io.BytesIO(payload) if payload is not None else None)
    replacement.replace(path)


def test_default_mode_cannot_claim_release_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path)
    monkeypatch.setattr(release_check, "ROOT", root)

    assert release_check.main([]) == 0

    output = capsys.readouterr().out
    assert "DEV_CHECK_OK" in output
    assert "release_ready=false" in output
    assert "RELEASE_CHECK_OK" not in output


def test_build_check_validates_artifacts_without_claiming_release_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path)
    monkeypatch.setattr(release_check, "ROOT", root)
    monkeypatch.setattr(
        release_check,
        "build_fresh_artifacts",
        lambda source_root, work_dir: write_artifacts(
            source_root, work_dir / "artifacts"
        ),
    )

    assert release_check.main(["--build-check"]) == 0

    output = capsys.readouterr().out
    assert "BUILD_CHECK_OK" in output
    assert "artifacts=2" in output
    assert "release_ready=false" in output
    assert "RELEASE_CHECK_OK" not in output


def test_dev_check_rejects_source_version_drift(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "src" / "sample_pkg" / "__init__.py").write_text(
        '__version__ = "1.2.4"\n', encoding="utf-8"
    )

    result = release_check.run_dev_checks(root)

    assert "pyproject.toml and package __version__ differ" in result.errors


def test_dev_check_reports_secret_location_without_echoing_secret(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    secret = "sk-" + "A" * 24
    (root / "operator.txt").write_text(secret, encoding="utf-8")

    result = release_check.run_dev_checks(root)

    assert any("credential-like value in operator.txt" == error for error in result.errors)
    assert all(secret not in error for error in result.errors)


def test_javascript_scan_distinguishes_bindings_fixtures_and_literal_secrets(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    edge = root / "support" / "cloudflare-edge"
    tests = edge / "tests"
    tests.mkdir(parents=True)
    source = edge / "index.js"
    source.write_text(
        'const observed = String(env.ADMIN_TOKEN || "");\n'
        'if (!env.DIGEST_SHARED_SECRET) throw new Error("missing binding");\n',
        encoding="utf-8",
    )
    (tests / "index.test.mjs").write_text(
        'const env = { ADMIN_TOKEN: "test-admin-token-not-for-production" };\n',
        encoding="utf-8",
    )

    clean = release_check.run_dev_checks(root)

    assert not any("cloudflare-edge" in error for error in clean.errors)
    secret = "sk-" + "J" * 24
    source.write_text(f'const ADMIN_TOKEN = "{secret}";\n', encoding="utf-8")
    unsafe = release_check.run_dev_checks(root)
    assert "credential-like value in support/cloudflare-edge/index.js" in unsafe.errors
    assert all(secret not in error for error in unsafe.errors)


def test_dev_check_ignores_named_pytest_runtime_directories(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    runtime = root / ".pytest-heartbeat-local-alpha"
    runtime.mkdir()
    (runtime / "operator.txt").write_text("sk-" + "A" * 24, encoding="utf-8")

    result = release_check.run_dev_checks(root)

    assert not any("pytest-heartbeat" in error for error in result.errors)


def test_matching_wheel_and_sdist_pass_strict_artifact_validation(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    info = release_check.load_project_info(root)
    artifacts = write_artifacts(root, tmp_path / "artifacts")

    assert release_check.validate_artifacts(root, info, artifacts) == ()


def test_declared_package_data_is_required_and_allowed_in_artifacts(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    with (root / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            '\n[tool.setuptools.package-data]\n'
            'sample_pkg = ["release_support/*.pub"]\n'
        )
    support = root / "src" / "sample_pkg" / "release_support"
    support.mkdir()
    (support / "support.pub").write_bytes(b"public-key-fixture")
    info = release_check.load_project_info(root)
    artifacts = write_artifacts(root, tmp_path / "artifacts")

    assert info.package_data == ("sample_pkg/release_support/support.pub",)
    assert release_check.validate_artifacts(root, info, artifacts) == ()


def test_artifact_validation_rejects_missing_or_drifted_package_data(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    with (root / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            '\n[tool.setuptools.package-data]\n'
            'sample_pkg = ["release_support/*.pub"]\n'
        )
    support = root / "src" / "sample_pkg" / "release_support"
    support.mkdir()
    public_key = support / "support.pub"
    public_key.write_bytes(b"public-key-v1")
    info = release_check.load_project_info(root)

    missing = write_artifacts(
        root,
        tmp_path / "missing-data",
        wheel_package_data=(),
        sdist_package_data=(),
    )
    missing_errors = release_check.validate_artifacts(root, info, missing)
    assert any("wheel is missing source-bound files" in error for error in missing_errors)
    assert any("sdist is missing source-bound files" in error for error in missing_errors)

    drifted = write_artifacts(root, tmp_path / "drifted-data")
    public_key.write_bytes(b"public-key-v2")
    drift_errors = release_check.validate_artifacts(root, info, drifted)
    assert any("wheel differs from the source tree" in error for error in drift_errors)
    assert any("sdist differs from the source tree" in error for error in drift_errors)


def test_sdist_requires_release_identity_root_files(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    info = release_check.load_project_info(root)
    artifacts = write_artifacts(
        root,
        tmp_path / "missing-roots",
        sdist_root_files=("pyproject.toml", "PKG-INFO"),
    )

    errors = release_check.validate_artifacts(root, info, artifacts)

    assert any(
        "sdist is missing required root files: BRAND_ASSETS.md, LICENSE, MANIFEST.in, "
        "README.md, THIRD_PARTY_NOTICES.md, TRADEMARKS.md"
        in error
        for error in errors
    )


def test_wheel_requires_license_and_brand_notices(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    info = release_check.load_project_info(root)
    artifacts = write_artifacts(
        root,
        tmp_path / "missing-wheel-legal",
        wheel_legal_files=("LICENSE",),
    )

    errors = release_check.validate_artifacts(root, info, artifacts)

    assert any("wheel is missing required legal files" in error for error in errors)
    assert any("BRAND_ASSETS.md" in error for error in errors)
    assert any("THIRD_PARTY_NOTICES.md" in error for error in errors)
    assert any("TRADEMARKS.md" in error for error in errors)


def test_artifact_validation_detects_missing_and_stale_modules(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    info = release_check.load_project_info(root)
    artifacts = write_artifacts(
        root,
        tmp_path / "artifacts",
        wheel_modules=["sample_pkg/__init__.py"],
        sdist_modules=[*info.source_modules, "sample_pkg/removed.py"],
    )

    errors = release_check.validate_artifacts(root, info, artifacts)

    assert any("wheel is missing package modules: sample_pkg/cli.py" in error for error in errors)
    assert any("sdist contains stale package modules" in error for error in errors)
    assert any("sample_pkg/removed.py" in error for error in errors)


def test_artifact_validation_detects_entry_point_and_version_drift(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    info = release_check.load_project_info(root)
    artifacts = write_artifacts(
        root,
        tmp_path / "artifacts",
        artifact_scripts={"sample": "sample_pkg.cli:obsolete"},
        artifact_version="1.2.4",
    )

    errors = release_check.validate_artifacts(root, info, artifacts)

    assert any("filename does not match project name and version" in error for error in errors)
    assert any("console scripts differ from pyproject.toml" in error for error in errors)
    assert any("version differs from pyproject.toml" in error for error in errors)


def test_artifact_validation_rejects_secret_and_unexpected_wheel_member(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    info = release_check.load_project_info(root)
    artifacts = write_artifacts(root, tmp_path / "artifacts")
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    secret = "sk-" + "Z" * 24
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("operator.env", f"API_KEY={secret}\n")

    errors = release_check.validate_artifacts(root, info, artifacts)

    assert any("wheel contains unexpected members: operator.env" in error for error in errors)
    assert any("wheel contains credential-like content: operator.env" in error for error in errors)
    assert all(secret not in error for error in errors)


def test_artifact_validation_rejects_secret_and_link_in_sdist(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    info = release_check.load_project_info(root)
    artifacts = write_artifacts(root, tmp_path / "artifacts")
    sdist = next(path for path in artifacts if path.name.endswith(".tar.gz"))
    archive_root = "sample_pkg-1.2.3"
    secret = "ghp_" + "Y" * 24
    payload = f"Authorization: Bearer {secret}\n".encode("utf-8")
    private_member = tarfile.TarInfo(f"{archive_root}/private.env")
    private_member.size = len(payload)
    append_sdist_member(sdist, private_member, payload)
    link_member = tarfile.TarInfo(f"{archive_root}/src/peerbridge-link")
    link_member.type = tarfile.SYMTYPE
    link_member.linkname = "../../operator-home"
    append_sdist_member(sdist, link_member)

    errors = release_check.validate_artifacts(root, info, artifacts)

    assert any("sdist contains links or special members" in error for error in errors)
    assert any("sdist contains unexpected members: private.env" in error for error in errors)
    assert any("sdist contains credential-like content: private.env" in error for error in errors)
    assert all(secret not in error for error in errors)


def test_artifact_validation_rejects_oversized_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    info = release_check.load_project_info(root)
    artifacts = write_artifacts(root, tmp_path / "artifacts")
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("oversized.bin", b"x" * 128)
    monkeypatch.setattr(release_check, "MAX_ARCHIVE_MEMBER_BYTES", 64)

    errors = release_check.validate_artifacts(root, info, artifacts)

    assert any("wheel member exceeds the size limit: oversized.bin" in error for error in errors)


def test_strict_release_refuses_same_version_collision_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "sample_pkg-1.2.3-py3-none-any.whl").write_bytes(b"old")
    called = False

    def unexpected_builder(_root: Path, _work_dir: Path) -> list[Path]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(release_check, "build_fresh_artifacts", unexpected_builder)

    with pytest.raises(release_check.ReleaseCheckError, match="same-version"):
        release_check.run_strict_release(root, dist, require_clean_git=False)
    assert called is False


def test_explicit_collision_replacement_happens_only_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    old_wheel = dist / "sample_pkg-1.2.3-py3-none-any.whl"
    old_sdist = dist / "sample_pkg-1.2.3.tar.gz"
    old_wheel.write_bytes(b"old wheel")
    old_sdist.write_bytes(b"old sdist")

    def valid_builder(source_root: Path, work_dir: Path) -> list[Path]:
        assert not (work_dir / "artifacts").exists()
        return write_artifacts(source_root, work_dir / "artifacts")

    monkeypatch.setattr(release_check, "build_fresh_artifacts", valid_builder)

    _, published, _, remote = release_check.run_strict_release(
        root, dist, replace_existing=True, require_clean_git=False
    )

    assert {path.name for path in published} == {old_wheel.name, old_sdist.name}
    assert remote.errors == ()
    assert zipfile.is_zipfile(old_wheel)
    assert tarfile.is_tarfile(old_sdist)


def test_invalid_fresh_artifacts_are_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    dist = tmp_path / "dist"

    def invalid_builder(source_root: Path, work_dir: Path) -> list[Path]:
        return write_artifacts(
            source_root,
            work_dir / "artifacts",
            wheel_modules=["sample_pkg/__init__.py"],
        )

    monkeypatch.setattr(release_check, "build_fresh_artifacts", invalid_builder)

    with pytest.raises(release_check.ReleaseCheckError, match="missing package modules"):
        release_check.run_strict_release(root, dist, require_clean_git=False)
    assert not dist.exists()


def test_real_mobile_reconnect_receipt_passes_tailnet_only_gate(tmp_path: Path) -> None:
    root = make_project(tmp_path)

    result = release_check.validate_remote_mobile_evidence(root)

    assert result.errors == ()
    assert len(result.receipt_sha256) == 64
    assert result.source_tree_sha256 == release_check.release_source_tree_sha256(root)


def test_remote_gate_fails_closed_when_phone_receipt_is_missing(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / release_check.REMOTE_RECEIPT_DEFAULT).unlink()

    result = release_check.validate_remote_mobile_evidence(root)

    assert result.errors == ("real phone reconnect/mobile-browser receipt is missing",)


def test_remote_gate_rejects_simulated_phone_and_public_funnel(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    mutate_remote_artifact(
        root,
        "browser_trace",
        lambda trace: trace.update({"test_mode": True, "device_class": "simulated-phone"}),
    )
    mutate_remote_artifact(
        root,
        "funnel_status",
        lambda status: status.update(
            {"Web": {"https://public.example.invalid": {"Proxy": "http://127.0.0.1:8765"}}}
        ),
    )

    result = release_check.validate_remote_mobile_evidence(root)

    assert "remote evidence contains a fixture, mock, or simulated marker" in result.errors
    assert "browser trace is not a real phone run" in result.errors
    assert "Funnel status contains a public route" in result.errors


def test_remote_gate_rejects_reconnect_without_persisted_message(tmp_path: Path) -> None:
    root = make_project(tmp_path)

    def remove_reconnect_binding(trace: dict[str, object]) -> None:
        sessions = trace["sessions"]
        assert isinstance(sessions, list) and isinstance(sessions[1], dict)
        sessions[1].pop("observed_message_id")

    mutate_remote_artifact(root, "browser_trace", remove_reconnect_binding)

    result = release_check.validate_remote_mobile_evidence(root)

    assert "reconnect snapshot does not contain the phone message" in result.errors


def test_remote_gate_rejects_false_device_and_network_disconnect_claims(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)

    def overclaim(trace: dict[str, object]) -> None:
        sessions = trace["sessions"]
        disconnect = trace["disconnect_evidence"]
        assert isinstance(sessions, list) and isinstance(sessions[1], dict)
        assert isinstance(disconnect, dict)
        sessions[1]["browser_device_continuity_sha256"] = "b" * 64
        disconnect["network_layer_disconnect_cryptographically_proven"] = True

    mutate_remote_artifact(root, "browser_trace", overclaim)

    result = release_check.validate_remote_mobile_evidence(root)

    assert "phone reconnect did not preserve browser device continuity" in result.errors
    assert "browser trace overclaims network-layer disconnect proof" in result.errors


def test_remote_gate_rejects_invalid_disconnect_timestamp_without_crashing(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)

    mutate_remote_artifact(
        root,
        "browser_trace",
        lambda trace: trace.update({"disconnected_at_utc": "not-a-timestamp"}),
    )

    result = release_check.validate_remote_mobile_evidence(root)

    assert "phone disconnect must be an explicit UTC timestamp" in result.errors


def test_remote_gate_rejects_public_listener_and_artifact_tamper(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    mutate_remote_artifact(
        root,
        "network_observation",
        lambda network: network.update(
            {
                "backend_listener": {"address": "0.0.0.0", "port": 8765, "process_id": 4321},
                "non_loopback_listeners_on_backend_port": ["0.0.0.0:8765"],
            }
        ),
    )
    receipt = json.loads(
        (root / release_check.REMOTE_RECEIPT_DEFAULT).read_text(encoding="utf-8")
    )
    browser_path = root / receipt["artifacts"]["browser_trace"]["path"]
    browser_path.write_bytes(browser_path.read_bytes() + b" ")

    result = release_check.validate_remote_mobile_evidence(root)

    assert "network evidence does not bind a loopback backend listener" in result.errors
    assert "network evidence reports a non-loopback backend listener" in result.errors
    assert "remote artifact browser_trace byte count differs from live evidence" in result.errors
    assert "remote artifact browser_trace SHA-256 differs from live evidence" in result.errors


def test_remote_gate_rejects_source_tree_drift(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "README.md").write_text("changed after phone evidence\n", encoding="utf-8")

    result = release_check.validate_remote_mobile_evidence(root)

    assert "remote mobile receipt does not bind the current release source tree" in result.errors


def test_source_tree_hash_ignores_numbered_pytest_scratch_directories(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    before = release_check.release_source_tree_sha256(root)
    scratch = root / ".pytest-tmp-focused-20260812"
    scratch.mkdir()
    (scratch / "captured.txt").write_text("transient test output\n", encoding="utf-8")

    assert release_check.release_source_tree_sha256(root) == before
    assert scratch / "captured.txt" not in release_check.iter_text_files(root)


def test_source_tree_hash_binds_license_and_binary_public_key(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    support = root / "support"
    support.mkdir()
    public_key = support / "maintainer.pub"
    public_key.write_bytes(b"public-key-v1")
    first = release_check.release_source_tree_sha256(root)

    public_key.write_bytes(b"public-key-v2")
    second = release_check.release_source_tree_sha256(root)
    (root / "LICENSE").write_text("changed license\n", encoding="utf-8")
    third = release_check.release_source_tree_sha256(root)

    assert first != second
    assert second != third


def test_git_inventory_uses_tracked_and_nonignored_files_and_new_text_suffixes(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    script = root / "support" / "edge.mjs"
    script.parent.mkdir()
    script.write_text("export const healthy = true;\n", encoding="utf-8")
    init_git_project(root)
    ignored = root / "ignored-private" / "secret.jsonc"
    ignored.parent.mkdir()
    ignored.write_text('{"token":"sk-' + "Q" * 24 + '"}\n', encoding="utf-8")
    untracked = root / "support" / "schema.sql"
    untracked.write_text("select 1;\n", encoding="utf-8")

    text_files = release_check.iter_text_files(root)
    release_files = release_check.iter_release_source_files(root)
    tracked_release_files = release_check.iter_release_source_files(
        root, tracked_only=True
    )

    assert script in text_files
    assert untracked in text_files
    assert untracked in release_files
    assert untracked not in tracked_release_files
    assert ignored not in text_files
    assert ignored not in release_files

    snapshot = tmp_path / "snapshot"
    release_check._copy_release_snapshot(root, snapshot)
    assert (snapshot / "support" / "edge.mjs").is_file()
    assert not (snapshot / "support" / "schema.sql").exists()
    assert not (snapshot / "ignored-private").exists()


def test_strict_git_preflight_requires_committed_clean_tree(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    init_git_project(root)
    assert release_check.git_release_preflight_errors(root) == ()

    (root / "README.md").write_text("dirty\n", encoding="utf-8")
    errors = release_check.git_release_preflight_errors(root)

    assert len(errors) == 1
    assert "clean Git worktree" in errors[0]


def test_strict_git_preflight_requires_expected_annotated_tag(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    init_git_project(root, create_tag=False)

    missing = release_check.git_release_preflight_errors(root)
    assert missing == ("strict release requires annotated tag v1.2.3 at Git HEAD",)

    run_git(root, "tag", "v1.2.3")
    lightweight = release_check.git_release_preflight_errors(root)
    assert lightweight == ("strict release tag v1.2.3 must be annotated",)


def test_pep440_alpha_version_maps_to_public_release_tag() -> None:
    assert release_check.expected_release_tag("0.1.0a1") == "v0.1.0-alpha.1"
    assert release_check.expected_release_tag("1.2.3") == "v1.2.3"


def test_python_and_edge_build_inputs_are_pinned_and_lockfile_bound() -> None:
    root = SCRIPT.parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["build-system"]["requires"] == ["setuptools==83.0.0"]
    assert all("==" in requirement for requirement in project["project"]["optional-dependencies"]["dev"])
    assert all("==" in requirement for requirement in project["project"]["optional-dependencies"]["windows"])

    edge = root / "support" / "cloudflare-edge"
    package = json.loads((edge / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((edge / "package-lock.json").read_text(encoding="utf-8"))
    assert package["engines"]["node"] == "22.22.0"
    assert package["devDependencies"] == {"wrangler": "4.123.0"}
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]
    assert lock["packages"][""]["engines"] == package["engines"]
    assert lock["packages"]["node_modules/wrangler"]["version"] == "4.123.0"
    for relative, descriptor in lock["packages"].items():
        if not relative:
            continue
        assert descriptor["resolved"].startswith("https://registry.npmjs.org/")
        assert descriptor["integrity"].startswith("sha512-")
        assert descriptor["license"]


def test_dev_check_ignores_preserved_test_scratch_but_scans_release_tree(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    scratch = root / ".test-tmp"
    scratch.mkdir()
    (scratch / "captured.txt").write_text(
        "C:\\Users\\operator\\private\n" + "sk-" + "A" * 24,
        encoding="utf-8",
    )

    clean = release_check.run_dev_checks(root)

    assert not any(".test-tmp" in error for error in clean.errors)
    (root / "operator.txt").write_text("sk-" + "B" * 24, encoding="utf-8")
    unsafe = release_check.run_dev_checks(root)
    assert "credential-like value in operator.txt" in unsafe.errors


def test_dev_check_ignores_named_test_tmp_scratch_directories(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    before = release_check.release_source_tree_sha256(root)
    scratch = root / ".test-tmp-release-full-20260816" / "fixture"
    scratch.mkdir(parents=True)
    (scratch / "operator.txt").write_text(
        "C:\\Users\\operator\\private\n" + "sk-" + "E" * 24,
        encoding="utf-8",
    )

    result = release_check.run_dev_checks(root)

    assert not any(".test-tmp-release" in error for error in result.errors)
    assert scratch / "operator.txt" not in release_check.iter_text_files(root)
    assert release_check.release_source_tree_sha256(root) == before


def test_dev_check_ignores_project_local_pytest_runtime(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scratch = root / ".test-runtime" / "pytest-full" / "fixture"
    scratch.mkdir(parents=True)
    (scratch / "operator.txt").write_text(
        "C:\\Users\\operator\\private\n" + "sk-" + "C" * 24,
        encoding="utf-8",
    )

    result = release_check.run_dev_checks(root)

    assert not any(".test-runtime" in error for error in result.errors)
    assert scratch / "operator.txt" not in release_check.iter_text_files(root)


def test_dev_check_ignores_project_local_pytest_runs(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scratch = root / ".pytest_runs" / "run-isolated" / "fixture"
    scratch.mkdir(parents=True)
    (scratch / "operator.txt").write_text(
        "C:\\Users\\operator\\private\n" + "sk-" + "D" * 24,
        encoding="utf-8",
    )

    result = release_check.run_dev_checks(root)

    assert not any(".pytest_runs" in error for error in result.errors)
    assert scratch / "operator.txt" not in release_check.iter_text_files(root)


def test_windows_build_environment_bypasses_wmi_without_losing_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_check.sys, "platform", "win32")
    monkeypatch.setenv("PYTHONPATH", "existing-build-path")
    monkeypatch.setenv("PEERBRIDGE_TEST_SECRET", "sensitive-test-value")

    env = release_check._build_environment(tmp_path)
    bootstrap = tmp_path / "build-bootstrap"

    assert env["PYTHONPATH"].split(release_check.os.pathsep) == [
        str(bootstrap),
        "existing-build-path",
    ]
    assert (bootstrap / "sitecustomize.py").read_text(encoding="utf-8") == (
        "import platform as _platform\n"
        "if hasattr(_platform, '_wmi'):\n"
        "    _platform._wmi = None\n"
    )
    assert env["PEERBRIDGE_TEST_SECRET"] == "sensitive-test-value"
    assert env["PYTHONHASHSEED"] == "0"
    assert env["SOURCE_DATE_EPOCH"] == "315532800"
    assert env["TZ"] == "UTC"


def test_non_windows_build_environment_does_not_create_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_check.sys, "platform", "linux")

    env = release_check._build_environment(tmp_path, source_date_epoch=1_700_000_000)

    assert not (tmp_path / "build-bootstrap").exists()
    assert env["SOURCE_DATE_EPOCH"] == "1700000000"


def test_fresh_build_uses_checked_local_backend_and_wmi_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    work_dir = tmp_path / "release-work"
    work_dir.mkdir()
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        artifact_dir = Path(command[command.index("--outdir") + 1])
        artifact_dir.joinpath("sample-pkg-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
        with tarfile.open(artifact_dir / "sample-pkg-1.2.3.tar.gz", "w:gz") as archive:
            add_tar_text(archive, "sample-pkg-1.2.3/README.md", "sample\n")
        return Completed()

    monkeypatch.setattr(release_check.sys, "platform", "win32")
    monkeypatch.setattr(release_check.subprocess, "run", fake_run)

    artifacts = release_check.build_fresh_artifacts(root, work_dir)

    command = captured["command"]
    assert isinstance(command, list)
    assert "--no-isolation" in command
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONPATH"].split(release_check.os.pathsep)[0] == str(
        work_dir / "build-bootstrap"
    )
    assert [path.name for path in artifacts] == [
        "sample-pkg-1.2.3-py3-none-any.whl",
        "sample-pkg-1.2.3.tar.gz",
    ]


def test_sdist_normalization_is_byte_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    for path, mtime in ((first, 10), (second, 20)):
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo("sample-pkg-1.2.3/README.md")
            payload = b"sample\n"
            member.size = len(payload)
            member.mtime = mtime
            member.uid = mtime
            member.gid = mtime
            archive.addfile(member, io.BytesIO(payload))

    release_check._normalize_sdist(first, 1_700_000_000)
    release_check._normalize_sdist(second, 1_700_000_000)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        member = archive.getmember("sample-pkg-1.2.3/README.md")
        assert (member.mtime, member.uid, member.gid, member.uname, member.gname) == (
            1_700_000_000,
            0,
            0,
            "",
            "",
        )


def test_strict_release_checks_mobile_gate_before_artifact_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    (root / release_check.REMOTE_RECEIPT_DEFAULT).unlink()
    called = False

    def unexpected_builder(_root: Path, _work_dir: Path) -> list[Path]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(release_check, "build_fresh_artifacts", unexpected_builder)

    with pytest.raises(release_check.ReleaseCheckError, match="phone reconnect"):
        release_check.run_strict_release(
            root, tmp_path / "dist", require_clean_git=False
        )
    assert called is False
    assert not (tmp_path / "dist").exists()


def test_local_alpha_release_does_not_require_remote_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    (root / release_check.REMOTE_RECEIPT_DEFAULT).unlink()
    dist = tmp_path / "dist"
    monkeypatch.setattr(
        release_check,
        "build_fresh_artifacts",
        lambda source_root, work_dir: write_artifacts(
            source_root, work_dir / "artifacts"
        ),
    )

    _, published, _, remote = release_check.run_strict_release(
        root,
        dist,
        require_remote_mobile=False,
        require_clean_git=False,
    )

    assert remote is None
    assert len(published) == 2


def test_strict_release_rejects_nonreproducible_repeat_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    build_count = 0

    def changing_builder(source_root: Path, work_dir: Path) -> list[Path]:
        nonlocal build_count
        build_count += 1
        artifacts = write_artifacts(source_root, work_dir / "artifacts")
        if build_count == 2:
            wheel = next(path for path in artifacts if path.suffix == ".whl")
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.comment = b"repeat-build-drift"
        return artifacts

    monkeypatch.setattr(release_check, "build_fresh_artifacts", changing_builder)

    with pytest.raises(release_check.ReleaseCheckError, match="not byte-identical"):
        release_check.run_strict_release(
            root,
            tmp_path / "dist",
            require_remote_mobile=False,
            require_clean_git=False,
        )
    assert build_count == 2
    assert not (tmp_path / "dist").exists()


def test_strict_release_rejects_source_drift_after_evidence_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    dist = tmp_path / "dist"

    def drifting_builder(source_root: Path, work_dir: Path) -> list[Path]:
        artifacts = write_artifacts(source_root, work_dir / "artifacts")
        (source_root / "README.md").write_text("drift during build\n", encoding="utf-8")
        return artifacts

    monkeypatch.setattr(release_check, "build_fresh_artifacts", drifting_builder)

    with pytest.raises(release_check.ReleaseCheckError, match="source tree changed"):
        release_check.run_strict_release(root, dist, require_clean_git=False)
    assert not dist.exists()


def test_release_cli_reports_verified_remote_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path)
    monkeypatch.setattr(release_check, "ROOT", root)
    monkeypatch.setattr(release_check, "git_release_preflight_errors", lambda _root: ())
    monkeypatch.setattr(
        release_check,
        "build_fresh_artifacts",
        lambda source_root, work_dir: write_artifacts(source_root, work_dir / "artifacts"),
    )

    assert release_check.main(["--release", "--dist-dir", "dist"]) == 0

    output = capsys.readouterr().out
    assert "RELEASE_CHECK_OK" in output
    assert "remote_mobile_e2e=true" in output
    assert "tailnet_only=true" in output
    assert "funnel_enabled=false" in output
    assert "release_ready=true" in output


def test_release_cli_reports_local_alpha_scope_without_remote_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path)
    (root / release_check.REMOTE_RECEIPT_DEFAULT).unlink()
    monkeypatch.setattr(release_check, "ROOT", root)
    monkeypatch.setattr(release_check, "git_release_preflight_errors", lambda _root: ())
    monkeypatch.setattr(
        release_check,
        "build_fresh_artifacts",
        lambda source_root, work_dir: write_artifacts(
            source_root, work_dir / "artifacts"
        ),
    )

    assert (
        release_check.main(
            [
                "--release",
                "--release-profile",
                "local-alpha",
                "--dist-dir",
                "dist",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "RELEASE_CHECK_OK" in output
    assert "profile=local-alpha" in output
    assert "remote_mobile_e2e=not_in_release_scope" in output
    assert "remote_receipt_sha256=" not in output
    assert "release_ready=true" in output
