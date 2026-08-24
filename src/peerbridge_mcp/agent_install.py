"""Allowlisted discovery and visible installation of Agent terminals and runtimes.

PeerBridge never accepts an installer URL or command from room messages, provider
metadata, or user input.  Every executable, package identifier, and documentation
URL below is a reviewed constant from the Agent publisher.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .child_environment import build_local_child_environment


class AgentInstallError(RuntimeError):
    """An official Agent terminal cannot be detected or installed safely."""


@dataclass(frozen=True)
class AgentInstallSpec:
    agent_id: str
    display_name: str
    publisher: str
    executable_names: tuple[str, ...]
    version_args: tuple[str, ...]
    docs_url: str
    package_manager: str | None
    package_identifier: str | None
    package_integrity: str | None
    note_key: str
    install_scripts_required: bool = False

    @property
    def automatic_install_supported(self) -> bool:
        return bool(self.package_manager and self.package_identifier)


@dataclass(frozen=True)
class AgentInstallStatus:
    agent_id: str
    installed: bool
    executable_path: str | None
    version: str | None
    automatic_install_supported: bool


@dataclass(frozen=True)
class _VerifiedNpmTarball:
    path: Path
    staging_directory: Path
    bytes: int
    sha512: str
    stat_identity: tuple[int, int, int, int]


OFFICIAL_AGENT_CATALOG: Mapping[str, AgentInstallSpec] = {
    "codex": AgentInstallSpec(
        agent_id="codex",
        display_name="OpenAI Codex",
        publisher="OpenAI",
        executable_names=("codex.exe", "codex.cmd", "codex"),
        version_args=("--version",),
        docs_url="https://github.com/openai/codex",
        package_manager="npm",
        package_identifier="@openai/codex@0.149.0",
        package_integrity=(
            "sha512-i4dryj2Y1j+00Mb5n+0n71EYnTK9/KDc2cdFo/dXD0d1oTog2bhUssKDEIOn"
            "KmnEf51P0Z/HJTWvTKw/UHyOvQ=="
        ),
        note_key="agent_install.note.codex",
    ),
    "claude-code": AgentInstallSpec(
        agent_id="claude-code",
        display_name="Claude Code",
        publisher="Anthropic",
        executable_names=("claude.exe", "claude.cmd", "claude"),
        version_args=("--version",),
        docs_url="https://code.claude.com/docs/en/installation",
        package_manager="winget",
        package_identifier="Anthropic.ClaudeCode",
        package_integrity=None,
        note_key="agent_install.note.claude",
    ),
    "kimi-code": AgentInstallSpec(
        agent_id="kimi-code",
        display_name="Kimi Code",
        publisher="Moonshot AI",
        executable_names=("kimi.exe", "kimi.cmd", "kimi"),
        version_args=("--version",),
        docs_url="https://github.com/MoonshotAI/kimi-code",
        package_manager="npm",
        package_identifier="@moonshot-ai/kimi-code@0.38.0",
        package_integrity=(
            "sha512-O/z6sfjFdoDPPeTnoXzdsJ2U8IqP6K2gD3LsT+Nu8BAlHwdhCjdCQFkFTjIb"
            "LBun+aZT6x81ha5FiFt7trEilg=="
        ),
        note_key="agent_install.note.kimi",
        install_scripts_required=True,
    ),
    "grok": AgentInstallSpec(
        agent_id="grok",
        display_name="Grok CLI",
        publisher="xAI",
        executable_names=("grok.exe", "grok.cmd", "grok"),
        version_args=("--version",),
        docs_url="https://github.com/xai-org/grok-build",
        package_manager=None,
        package_identifier=None,
        package_integrity=None,
        note_key="agent_install.note.grok",
    ),
}


# ACPX is a community interoperability runtime, not an Agent publisher.  Keep it
# outside the official catalog so receipts and UI labels cannot confuse the
# transport/runtime with Codex, Claude Code, Kimi Code, or Grok identity.
ACPX_RUNTIME_SPEC = AgentInstallSpec(
    agent_id="acpx-runtime",
    display_name="ACPX interoperability runtime",
    publisher="OpenClaw community",
    executable_names=("acpx.exe", "acpx.cmd", "acpx"),
    version_args=("--version",),
    docs_url="https://github.com/openclaw/acpx",
    package_manager="npm",
    package_identifier="acpx@0.13.0",
    package_integrity=(
        "sha512-EdGgMx5osY4bNpVN+7dTTT67ZXsFqx/itl4QjGYTKH/Nzm3fqGmWL3E6FjRk"
        "VrlWRpiFnRNi+J1lxUJPie4lmg=="
    ),
    note_key="agent_install.note.acpx",
)


_PINNED_NPM_PACKAGE = re.compile(
    r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*@"
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?\Z"
)
_NPM_INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/]+={0,2}\Z")
_NPM_REGISTRY = "https://registry.npmjs.org/"
_MAX_NPM_TARBALL_BYTES = 256 * 1024 * 1024
_NPM_STAGE_PREFIX = "peerbridge-npm-stage-"
_WINDOWS_SIGNER_FRAGMENTS: Mapping[str, tuple[str, ...]] = {
    "codex": ("OpenAI OpCo, LLC",),
    "claude-code": ("Anthropic, PBC",),
}


def _validate_reviewed_npm_spec(spec: AgentInstallSpec) -> None:
    """Reject mutable registry tags and unbound npm payloads before launch."""

    identifier = spec.package_identifier or ""
    integrity = spec.package_integrity or ""
    if not _PINNED_NPM_PACKAGE.fullmatch(identifier):
        raise AgentInstallError("npm package must use one reviewed exact version")
    if not _NPM_INTEGRITY.fullmatch(integrity):
        raise AgentInstallError("npm package must bind one reviewed SHA-512 integrity")
    try:
        digest = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AgentInstallError("npm package has an invalid reviewed SHA-512 integrity") from exc
    if len(digest) != hashlib.sha512().digest_size:
        raise AgentInstallError("npm package has an invalid reviewed SHA-512 integrity")


def _verify_published_npm_integrity(
    spec: AgentInstallSpec,
    manager: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Fail closed unless the official registry still serves the reviewed payload."""

    _validate_reviewed_npm_spec(spec)
    assert spec.package_identifier is not None
    assert spec.package_integrity is not None
    try:
        result = run(
            (
                manager,
                "view",
                spec.package_identifier,
                "dist.integrity",
                "--json",
                "--registry",
                _NPM_REGISTRY,
            ),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            shell=False,
            cwd=str(Path(manager).resolve().parent),
            env=build_local_child_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentInstallError(
            "reviewed npm package integrity could not be verified"
        ) from exc
    if result.returncode != 0:
        raise AgentInstallError("reviewed npm package integrity could not be verified")
    try:
        observed = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AgentInstallError(
            "npm registry returned invalid integrity metadata"
        ) from exc
    if not isinstance(observed, str) or not hmac.compare_digest(
        observed, spec.package_integrity
    ):
        raise AgentInstallError("npm package integrity differs from reviewed release")


def _npm_file_identity(path: Path) -> tuple[int, int, int, int]:
    metadata = os.stat(path, follow_symlinks=False)
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _remove_npm_stage(path: Path) -> None:
    """Remove only a staging directory created by this module."""

    try:
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        candidate = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    if candidate.parent != root or not candidate.name.startswith(_NPM_STAGE_PREFIX):
        return
    shutil.rmtree(candidate, ignore_errors=True)


def _stage_verified_npm_tarball(
    spec: AgentInstallSpec,
    manager: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> _VerifiedNpmTarball:
    """Download once and bind the exact archive bytes used by npm install."""

    _validate_reviewed_npm_spec(spec)
    assert spec.package_identifier is not None
    assert spec.package_integrity is not None
    root = Path(tempfile.gettempdir()).resolve(strict=True)
    if _is_link_or_reparse(root) or not root.is_dir():
        raise AgentInstallError("npm staging root is not a trusted local directory")
    staging = Path(tempfile.mkdtemp(prefix=_NPM_STAGE_PREFIX, dir=root)).resolve(strict=True)
    try:
        result = run(
            (
                manager,
                "pack",
                spec.package_identifier,
                "--ignore-scripts",
                "--pack-destination",
                str(staging),
                "--registry",
                _NPM_REGISTRY,
                "--silent",
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
            shell=False,
            cwd=str(Path(manager).resolve().parent),
            env=build_local_child_environment(),
        )
        if result.returncode != 0:
            raise AgentInstallError("reviewed npm package archive could not be downloaded")
        entries = list(staging.iterdir())
        if len(entries) != 1:
            raise AgentInstallError("npm staging directory contains an unexpected file set")
        tarball = entries[0]
        if (
            tarball.parent != staging
            or tarball.suffix.lower() != ".tgz"
            or _is_link_or_reparse(tarball)
        ):
            raise AgentInstallError("npm package archive path is invalid")
        metadata = os.stat(tarball, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise AgentInstallError("npm package archive is not a regular file")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_NPM_TARBALL_BYTES:
            raise AgentInstallError("npm package archive exceeds its byte budget")
        before = _npm_file_identity(tarball)
        digest = hashlib.sha512()
        total = 0
        with tarball.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                total += len(block)
                if total > _MAX_NPM_TARBALL_BYTES:
                    raise AgentInstallError("npm package archive exceeds its byte budget")
                digest.update(block)
        after = _npm_file_identity(tarball)
        if before != after or total != before[2]:
            raise AgentInstallError("npm package archive changed during verification")
        observed = "sha512-" + base64.b64encode(digest.digest()).decode("ascii")
        if not hmac.compare_digest(observed, spec.package_integrity):
            raise AgentInstallError("npm package archive differs from reviewed release")
        os.chmod(tarball, stat.S_IREAD)
        final_identity = _npm_file_identity(tarball)
        return _VerifiedNpmTarball(
            path=tarball,
            staging_directory=staging,
            bytes=total,
            sha512=observed,
            stat_identity=final_identity,
        )
    except BaseException:
        _remove_npm_stage(staging)
        raise


def _revalidate_npm_tarball(staged: _VerifiedNpmTarball) -> None:
    try:
        if _is_link_or_reparse(staged.path):
            raise AgentInstallError("verified npm package archive became a link")
        identity = _npm_file_identity(staged.path)
    except OSError as exc:
        raise AgentInstallError("verified npm package archive is unavailable") from exc
    if identity != staged.stat_identity:
        raise AgentInstallError("verified npm package archive changed before launch")


def _verified_npm_install_command(
    spec: AgentInstallSpec,
    manager: str,
    staged: _VerifiedNpmTarball,
) -> tuple[str, ...]:
    command = [
        manager,
        "install",
        "--global",
        str(staged.path),
        "--no-audit",
        "--no-fund",
        "--registry",
        _NPM_REGISTRY,
    ]
    if not spec.install_scripts_required:
        command.append("--ignore-scripts")
    return tuple(command)


def _cleanup_npm_stage_after_exit(
    process: subprocess.Popen[bytes], staging_directory: Path
) -> None:
    def cleanup() -> None:
        try:
            try:
                process.wait()
            except BaseException:
                pass
        finally:
            _remove_npm_stage(staging_directory)

    threading.Thread(
        target=cleanup,
        name=f"peerbridge-npm-cleanup-{process.pid}",
        daemon=True,
    ).start()


def official_agent_specs() -> tuple[AgentInstallSpec, ...]:
    return tuple(OFFICIAL_AGENT_CATALOG.values())


def installable_agent_specs() -> tuple[AgentInstallSpec, ...]:
    """Return official Agents followed by reviewed optional runtimes."""
    return (*official_agent_specs(), ACPX_RUNTIME_SPEC)


def official_agent_spec(agent_id: str) -> AgentInstallSpec:
    try:
        return OFFICIAL_AGENT_CATALOG[agent_id]
    except KeyError as exc:
        raise AgentInstallError("unknown official Agent terminal") from exc


def installable_agent_spec(agent_id: str) -> AgentInstallSpec:
    if agent_id == ACPX_RUNTIME_SPEC.agent_id:
        return ACPX_RUNTIME_SPEC
    return official_agent_spec(agent_id)


def _existing_environment_path(name: str, *parts: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value, *parts).resolve() if value else None


def _trusted_executable_roots(spec: AgentInstallSpec) -> tuple[Path, ...]:
    """Return reviewed install roots; ambient PATH alone is never authority."""

    roots: list[Path | None]
    if os.name == "nt":
        common = [
            _existing_environment_path("LOCALAPPDATA", "Microsoft", "WinGet", "Links"),
            _existing_environment_path("USERPROFILE", ".local", "bin"),
        ]
        if spec.agent_id == "codex":
            roots = [
                _existing_environment_path(
                    "LOCALAPPDATA", "Programs", "OpenAI", "Codex", "bin"
                ),
                _existing_environment_path("APPDATA", "npm"),
                *common,
            ]
        elif spec.agent_id == "claude-code":
            roots = [
                _existing_environment_path("USERPROFILE", ".covs", "npm-global"),
                _existing_environment_path("APPDATA", "npm"),
                *common,
            ]
        elif spec.agent_id in {"kimi-code", "acpx-runtime"}:
            roots = [
                _existing_environment_path("APPDATA", "npm"),
                *common,
            ]
        else:
            roots = [
                _existing_environment_path("LOCALAPPDATA", "Programs", "xAI"),
                _existing_environment_path("USERPROFILE", ".grok", "bin"),
                *common,
            ]
    else:
        roots = [
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            _existing_environment_path("HOME", ".local", "bin"),
            _existing_environment_path("HOME", ".npm-global", "bin"),
        ]
    return tuple(path for path in roots if path is not None)


def _path_is_within(path: Path, roots: Iterable[Path]) -> bool:
    candidate = os.path.normcase(str(path.resolve()))
    for root in roots:
        trusted = os.path.normcase(str(root.resolve()))
        try:
            if os.path.commonpath((candidate, trusted)) == trusted:
                return True
        except ValueError:
            continue
    return False


def _is_link_or_reparse(path: Path) -> bool:
    """Reject link-like launch files before resolving their target."""

    try:
        metadata = os.lstat(path)
    except OSError:
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _resolved_launch_target(spec: AgentInstallSpec, path: Path) -> Path | None:
    """Resolve reviewed publisher wrappers without executing wrapper text."""

    if _is_link_or_reparse(path) or not path.is_file():
        return None
    resolved = path.resolve()
    if spec.agent_id != "claude-code" or path.suffix.lower() != ".cmd":
        return resolved
    try:
        wrapper = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    if len(wrapper.encode("utf-8")) > 4096:
        return None
    expected = (
        '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"'
    )
    normalized_lines = {line.strip().lower() for line in wrapper.splitlines()}
    if not any(line.startswith(expected.lower()) and line.endswith("%*") for line in normalized_lines):
        return None
    target = (
        path.parent
        / "node_modules"
        / "@anthropic-ai"
        / "claude-code"
        / "bin"
        / "claude.exe"
    )
    if _is_link_or_reparse(target) or not target.is_file():
        return None
    if not _path_is_within(target, (path.parent,)):
        return None
    return target.resolve()


def _verify_windows_authenticode(spec: AgentInstallSpec, path: Path) -> bool:
    """Verify signed Windows publishers without trusting shell interpolation."""

    expected = _WINDOWS_SIGNER_FRAGMENTS.get(spec.agent_id)
    if os.name != "nt" or not expected:
        return True
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    powershell = (
        system_root
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.is_file():
        return False
    script = (
        "$p=[Environment]::GetEnvironmentVariable('PEERBRIDGE_VERIFY_PATH','Process');"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "[pscustomobject]@{Status=$s.Status.ToString();"
        "Subject=$(if($s.SignerCertificate){$s.SignerCertificate.Subject}else{''})}"
        "|ConvertTo-Json -Compress"
    )
    environment = {
        key: value
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH")
        if (value := os.environ.get(key))
    }
    environment["PEERBRIDGE_VERIFY_PATH"] = str(path)
    try:
        result = subprocess.run(
            (
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            shell=False,
            env=environment,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping) or payload.get("Status") != "Valid":
        return False
    subject = str(payload.get("Subject") or "")
    return any(fragment in subject for fragment in expected)


def _find_executable(
    spec: AgentInstallSpec,
    *,
    which: Callable[[str], str | None],
) -> str | None:
    trusted_roots = _trusted_executable_roots(spec)
    for name in spec.executable_names:
        match = which(name)
        if match:
            path = Path(match).resolve()
            if path.name.lower() != Path(name).name.lower():
                continue
            if _path_is_within(path, trusted_roots):
                return str(path)
    # Publisher installers can update the user's PATH for future processes only.
    # Probe exact executable names under reviewed roots so the current desktop
    # process can detect a newly installed Agent without trusting ambient PATH.
    for root in trusted_roots:
        for name in spec.executable_names:
            path = (root / name).resolve()
            if (
                path.is_file()
                and path.name.lower() == Path(name).name.lower()
                and _path_is_within(path, trusted_roots)
            ):
                return str(path)
    return None


def find_trusted_executable(
    spec: AgentInstallSpec,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """Resolve one executable only when its real path is under a reviewed root."""

    match = _find_executable(spec, which=which)
    if match is None:
        return None
    target = _resolved_launch_target(spec, Path(match))
    if target is None or not _verify_windows_authenticode(spec, target):
        return None
    return target


def detect_official_agent(
    agent_id: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> AgentInstallStatus:
    spec = official_agent_spec(agent_id)
    resolved = find_trusted_executable(spec, which=which)
    executable = str(resolved) if resolved is not None else None
    version: str | None = None
    if executable:
        try:
            result = run(
                (executable, *spec.version_args),
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                shell=False,
                cwd=str(Path(executable).resolve().parent),
                env=build_local_child_environment(),
            )
            output = (result.stdout or result.stderr or "").strip().splitlines()
            if result.returncode == 0 and output:
                version = output[0][:160]
        except (OSError, subprocess.SubprocessError):
            # Presence is still useful even when an old client lacks --version.
            version = None
    return AgentInstallStatus(
        agent_id=agent_id,
        installed=executable is not None,
        executable_path=executable,
        version=version,
        automatic_install_supported=spec.automatic_install_supported,
    )


def detect_installable_agent(
    agent_id: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> AgentInstallStatus:
    spec = installable_agent_spec(agent_id)
    resolved = find_trusted_executable(spec, which=which)
    executable = str(resolved) if resolved is not None else None
    version: str | None = None
    if executable:
        try:
            result = run(
                (executable, *spec.version_args),
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                shell=False,
                cwd=str(Path(executable).resolve().parent),
                env=build_local_child_environment(),
            )
            output = (result.stdout or result.stderr or "").strip().splitlines()
            if result.returncode == 0 and output:
                version = output[0][:160]
        except (OSError, subprocess.SubprocessError):
            version = None
    return AgentInstallStatus(
        agent_id=agent_id,
        installed=executable is not None,
        executable_path=executable,
        version=version,
        automatic_install_supported=spec.automatic_install_supported,
    )


def detect_all_official_agents() -> tuple[AgentInstallStatus, ...]:
    return tuple(detect_official_agent(spec.agent_id) for spec in official_agent_specs())


def detect_all_installable_agents() -> tuple[AgentInstallStatus, ...]:
    return tuple(
        detect_installable_agent(spec.agent_id) for spec in installable_agent_specs()
    )


def _resolve_package_manager(
    manager: str,
    *,
    which: Callable[[str], str | None],
) -> str:
    candidates: Sequence[str]
    expected_names: frozenset[str]
    if manager == "npm":
        candidates = ("npm.cmd", "npm") if os.name == "nt" else ("npm",)
        expected_names = frozenset({"npm", "npm.cmd", "npm.exe"})
        trusted_roots = (
            (_existing_environment_path("PROGRAMFILES", "nodejs"),)
            if os.name == "nt"
            else (Path("/usr/bin"), Path("/usr/local/bin"), Path("/opt/homebrew/bin"))
        )
    elif manager == "winget":
        if os.name != "nt":
            raise AgentInstallError("winget installation is only available on Windows")
        candidates = ("winget.exe", "winget")
        expected_names = frozenset({"winget", "winget.exe"})
        trusted_roots = (
            _existing_environment_path("LOCALAPPDATA", "Microsoft", "WindowsApps"),
        )
    else:
        raise AgentInstallError("unsupported official package manager")
    for candidate in candidates:
        resolved = which(candidate)
        if not resolved:
            continue
        path = Path(resolved).resolve()
        if path.name.lower() not in expected_names:
            continue
        if not _path_is_within(path, (root for root in trusted_roots if root)):
            continue
        return str(path)
    raise AgentInstallError(f"required package manager is unavailable: {manager}")


def build_official_install_command(
    agent_id: str,
    *,
    update: bool = False,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    spec = official_agent_spec(agent_id)
    if not spec.automatic_install_supported:
        raise AgentInstallError("automatic Windows installation is not publisher-verified")
    assert spec.package_manager is not None
    assert spec.package_identifier is not None
    manager = _resolve_package_manager(spec.package_manager, which=which)
    if spec.package_manager == "npm":
        _validate_reviewed_npm_spec(spec)
        return (
            manager,
            "install",
            "--global",
            spec.package_identifier,
            "--no-audit",
            "--no-fund",
            "--registry",
            _NPM_REGISTRY,
        )
    action = "upgrade" if update else "install"
    return (
        manager,
        action,
        "--id",
        spec.package_identifier,
        "--exact",
        "--source",
        "winget",
        "--accept-package-agreements",
        "--accept-source-agreements",
    )


def build_install_command(
    agent_id: str,
    *,
    update: bool = False,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    if agent_id != ACPX_RUNTIME_SPEC.agent_id:
        return build_official_install_command(agent_id, update=update, which=which)
    spec = ACPX_RUNTIME_SPEC
    assert spec.package_manager is not None
    assert spec.package_identifier is not None
    manager = _resolve_package_manager(spec.package_manager, which=which)
    _validate_reviewed_npm_spec(spec)
    return (
        manager,
        "install",
        "--global",
        spec.package_identifier,
        "--no-audit",
        "--no-fund",
        "--registry",
        _NPM_REGISTRY,
    )


def launch_official_agent_installer(
    agent_id: str,
    *,
    update: bool = False,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes]:
    command = build_official_install_command(agent_id, update=update, which=which)
    spec = official_agent_spec(agent_id)
    staged: _VerifiedNpmTarball | None = None
    if spec.package_manager == "npm":
        _verify_published_npm_integrity(spec, command[0], run=run)
        staged = _stage_verified_npm_tarball(spec, command[0], run=run)
        try:
            _revalidate_npm_tarball(staged)
        except BaseException:
            _remove_npm_stage(staged.staging_directory)
            raise
        command = _verified_npm_install_command(spec, command[0], staged)
    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        process = popen(
            command,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            cwd=str(Path(command[0]).resolve().parent),
            env=build_local_child_environment(),
        )
    except BaseException as exc:
        if staged is not None:
            _remove_npm_stage(staged.staging_directory)
        if isinstance(exc, OSError):
            raise AgentInstallError("official package manager could not be launched") from exc
        raise
    if staged is not None:
        _cleanup_npm_stage_after_exit(process, staged.staging_directory)
    return process


def launch_agent_installer(
    agent_id: str,
    *,
    update: bool = False,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes]:
    command = build_install_command(agent_id, update=update, which=which)
    spec = installable_agent_spec(agent_id)
    staged: _VerifiedNpmTarball | None = None
    if spec.package_manager == "npm":
        _verify_published_npm_integrity(spec, command[0], run=run)
        staged = _stage_verified_npm_tarball(spec, command[0], run=run)
        try:
            _revalidate_npm_tarball(staged)
        except BaseException:
            _remove_npm_stage(staged.staging_directory)
            raise
        command = _verified_npm_install_command(spec, command[0], staged)
    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        process = popen(
            command,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            cwd=str(Path(command[0]).resolve().parent),
            env=build_local_child_environment(),
        )
    except BaseException as exc:
        if staged is not None:
            _remove_npm_stage(staged.staging_directory)
        if isinstance(exc, OSError):
            raise AgentInstallError("reviewed package manager could not be launched") from exc
        raise
    if staged is not None:
        _cleanup_npm_stage_after_exit(process, staged.staging_directory)
    return process


__all__ = [
    "ACPX_RUNTIME_SPEC",
    "AgentInstallError",
    "AgentInstallSpec",
    "AgentInstallStatus",
    "build_install_command",
    "build_official_install_command",
    "detect_all_installable_agents",
    "detect_all_official_agents",
    "detect_installable_agent",
    "detect_official_agent",
    "find_trusted_executable",
    "installable_agent_spec",
    "installable_agent_specs",
    "launch_agent_installer",
    "launch_official_agent_installer",
    "official_agent_spec",
    "official_agent_specs",
]
