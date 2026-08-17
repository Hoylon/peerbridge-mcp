from __future__ import annotations

import hashlib
import runpy
import struct
import sys
from pathlib import Path

from peerbridge_mcp.monitor import (
    WINDOWS_APP_USER_MODEL_ID,
    apply_windows_window_icon,
    packaged_icon_paths,
    release_windows_icon_handles,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OWNER_SOURCE_SHA256 = "0b431e90e1a92bdf31045ea174270aa09bfef4d5c54a4bdccd0b7e1887b35333"


def test_owner_logo_source_and_operational_assets_are_packaged() -> None:
    png_path, ico_path = packaged_icon_paths()
    source_path = png_path.with_name("peerbridge-logo-source-owner-20260816.png")

    assert png_path.is_file()
    assert ico_path.is_file()
    assert source_path.is_file()
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == OWNER_SOURCE_SHA256
    assert WINDOWS_APP_USER_MODEL_ID == "PeerBridge.MCP.ControlRoom"


def test_windows_icon_has_expected_multisize_png_frames() -> None:
    _png_path, ico_path = packaged_icon_paths()
    payload = ico_path.read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", payload, 0)
    assert (reserved, image_type, count) == (0, 1, 9)

    dimensions: list[int] = []
    for index in range(count):
        width, height, _colors, _reserved, planes, bits, size, offset = struct.unpack_from(
            "<BBBBHHII", payload, 6 + (index * 16)
        )
        dimensions.append(256 if width == 0 else width)
        assert (256 if height == 0 else height) == dimensions[-1]
        assert (planes, bits) == (1, 32)
        assert payload[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + size <= len(payload)

    assert dimensions == [16, 20, 24, 32, 40, 48, 64, 128, 256]


def test_desktop_shortcut_installer_binds_the_peerbridge_icon() -> None:
    script = (PROJECT_ROOT / "scripts" / "install_desktop_shortcut.ps1").read_text(
        encoding="utf-8"
    )
    assert "CreateShortcut" in script
    assert "peerbridge-icon.ico" in script
    assert "$shortcut.IconLocation" in script
    assert "SetAppUserModelId" in script
    assert "PeerBridge.MCP.ControlRoom" in script
    assert "GetFolderPath('Programs')" in script
    assert "HKCU:\\Software\\Classes\\AppUserModelId" in script
    assert "[string]$ExecutablePath" in script
    assert '"--workspace-launch --project-root' in script
    assert '--db `"$database`" --scope `"peerbridge-main`"' in script
    assert "$shortcutWorkingDirectory = $projectRoot" in script
    assert "$shortcutIcon = $ExecutablePath" in script


def test_windows_desktop_build_and_launchers_bind_one_managed_runtime() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build_windows_desktop.ps1").read_text(
        encoding="utf-8"
    )
    launcher = (PROJECT_ROOT / "scripts" / "launch_control_room.ps1").read_text(
        encoding="utf-8"
    )
    entry = (PROJECT_ROOT / "scripts" / "peerbridge_control_room_entry.py").read_text(
        encoding="utf-8"
    )
    version_template = (
        PROJECT_ROOT / "scripts" / "peerbridge_control_room_version_info.template"
    ).read_text(encoding="utf-8")

    assert "$pyInstallerRunner" in build_script
    assert "$hookRoot" in build_script
    assert "--additional-hooks-dir $hookRoot" in build_script
    assert "-m PyInstaller" not in build_script
    assert "--collect-all cryptography" not in build_script
    for module in (
        "cryptography.hazmat.primitives.hashes",
        "cryptography.hazmat.primitives.serialization",
        "cryptography.hazmat.primitives.asymmetric.padding",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        "cryptography.hazmat.primitives.ciphers.aead",
    ):
        assert f"--hidden-import {module}" in build_script
    assert "--icon $icon" in build_script
    assert 'args[:1] == ["--workspace-launch"]' in entry
    assert "--version-file $versionFile" in build_script
    assert "PeerBridgeControlRoom.exe" in build_script
    assert "[string]$ArtifactRoot" in build_script
    assert "--source-launch" in launcher
    assert "--launcher-ready-path" in launcher
    assert "__PYVENV_LAUNCHER__" in launcher
    assert "sys._base_executable" in launcher
    assert "-f $entryScript, $projectRoot, $database, $scope, $readyPath" in launcher
    assert "'\"{0}\" --source-launch" in launcher
    assert "runtime_kind -ne 'source'" in launcher
    assert ".peerbridge-artifacts" not in launcher
    assert "PeerBridgeControlRoom.exe" not in launcher
    assert "from peerbridge_mcp.monitor import main as monitor_main" in entry
    assert "def _ensure_frozen_standard_streams()" in entry
    assert 'open(os.devnull, "w", encoding="utf-8")' in entry
    assert "_ensure_frozen_standard_streams()" in entry
    assert "PEERBRIDGE_SELF_TEST_RECEIPT_PATH" in entry
    assert "peerbridge-packaged-self-test-v1" in entry
    assert 'args == ["--announcement-self-test"]' in entry
    assert '"test": "announcement-feed"' in entry
    assert "_write_json_create_only(receipt_path, receipt)" in entry
    assert '"runtime_sha256": _runtime_sha256()' in entry
    assert "import traceback" not in entry
    assert 'args[:2] == ["-m", "peerbridge_mcp"]' in entry
    assert "return cli_main(args[2:])" in entry
    assert "_start_managed_supervisor" in entry
    assert "database-and-supervisor-lock-ready" in entry
    assert "STARTUP_TIMEOUT_SECONDS = 15.0" in entry
    assert "PEERBRIDGE_STARTUP_CONTRACT_PATH" in entry
    assert "_terminate_owned_process(supervisor)" in entry
    assert "DETACHED_PROCESS" not in entry
    for field in (
        "CompanyName",
        "ProductName",
        "FileVersion",
        "ProductVersion",
        "OriginalFilename",
    ):
        assert f"StringStruct('{field}'" in version_template


def test_source_launcher_keeps_managed_children_in_the_active_environment() -> None:
    entry_path = PROJECT_ROOT / "scripts" / "peerbridge_control_room_entry.py"
    namespace = runpy.run_path(str(entry_path), run_name="peerbridge_entry_contract_test")

    expected_runtime = Path(sys.executable).resolve()
    assert namespace["_runtime_path"]() == expected_runtime
    expected_command_runtime = expected_runtime
    if sys.platform == "win32":
        expected_command_runtime = Path(sys._base_executable).resolve()
    assert namespace["_runtime_command"]() == [
        str(expected_command_runtime),
        str(entry_path.resolve()),
    ]
    environment = namespace["_runtime_environment"]()
    if sys.platform == "win32":
        assert environment["__PYVENV_LAUNCHER__"] == str(expected_runtime)


def test_pyinstaller_runner_avoids_windows_wmi_dependency() -> None:
    script = (PROJECT_ROOT / "scripts" / "run_pyinstaller.py").read_text(
        encoding="utf-8"
    )

    assert "sys.getwindowsversion()" in script
    assert "platform.win32_ver =" in script
    assert "platform._uname_cache =" in script
    assert "from PyInstaller.__main__ import run" in script
    assert "_install_isolated_child_wrapper()" in script
    assert "_parent.CHILD_PY = Path(__file__).resolve()" in script
    assert "--peerbridge-project-version" in script
    assert 'project["project"]["version"]' in script


def test_windows_portable_packager_is_create_only_and_credential_free() -> None:
    script = (PROJECT_ROOT / "scripts" / "package_windows_portable.ps1").read_text(
        encoding="utf-8"
    )

    assert "create-only" in script
    assert "Compress-Archive" in script
    assert "Launch PeerBridge.cmd" in script
    assert "README.zh-Hant.md" in script
    assert "README.zh-Hans.md" in script
    assert "status --porcelain=v1 --untracked-files=all" in script
    assert "--untracked-files=no" not in script
    assert "%LOCALAPPDATA%\\PeerBridge\\workspace" in script
    assert "Credential Manager" in script
    for name in (
        "LICENSE",
        "TRADEMARKS.md",
        "BRAND_ASSETS.md",
        "THIRD_PARTY_NOTICES.md",
    ):
        assert name in script
    assert "SBOM.spdx.json" in script
    assert "collect_windows_runtime_licenses.py" in script
    assert "THIRD_PARTY_LICENSES_MANIFEST.json" in script
    assert "LicenseManifestSha256" in script
    assert "SPDX-2.3" in script
    assert "packageVerificationCodeValue" in script
    assert "Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256" in script
    assert '"%~dp0PeerBridgeControlRoom.exe"' in script
    assert 'PeerBridgeControlRoom\\PeerBridgeControlRoom.exe' not in script
    assert 'set "exit_code=%ERRORLEVEL%"' in script
    assert 'start ""' not in script
    assert "exit /b 0" not in script
    assert "API key" not in script


def test_windows_runtime_license_collector_binds_exact_build_components() -> None:
    script = (
        PROJECT_ROOT / "scripts" / "collect_windows_runtime_licenses.py"
    ).read_text(encoding="utf-8")

    assert "peerbridge.windows-runtime-licenses.v1" in script
    assert "platform.python_version()" in script
    assert '"pyinstaller", "PyInstaller"' in script
    assert '"cryptography"' in script
    assert '"cffi"' in script
    assert '"Tcl-Tk"' in script
    assert '"sha256"' in script
    assert "create-only" in script


def test_windows_portable_verifier_extracts_and_runs_real_mcp_checks() -> None:
    script = (PROJECT_ROOT / "scripts" / "verify_windows_portable.ps1").read_text(
        encoding="utf-8"
    )

    assert "create-only" in script
    assert "Unsafe portable archive member" in script
    assert "ExpectedSha256" in script
    assert "independently supplied expected digest" in script
    assert "$maxEntries" in script
    assert "$maxExpandedBytes" in script
    assert "$maxMemberBytes" in script
    assert "$maxCompressionRatio" in script
    assert "--ui-self-test" in script
    assert "--send-self-test" in script
    assert "--announcement-self-test" in script
    assert "'peerbridge_mcp', 'doctor'" in script
    assert "Portable create-only init did not create" in script
    assert "Portable localized quickstart differs from source" in script
    assert "$leaf -eq 'direct_url.json'" in script
    assert "THIRD_PARTY_NOTICES.md" in script
    assert "peerbridge.windows-runtime-licenses.v1" in script
    assert "Portable runtime-license file differs from its manifest" in script
    assert "Portable runtime-license manifest omits bundled cffi" in script
    assert "Portable SPDX SBOM file count differs" in script
    assert "Portable SPDX SBOM checksum differs" in script
    assert "package verification code is invalid" in script
    assert "document namespace does not bind" in script
    assert "Get-PeMachine" in script
    assert "$peMachine -ne 0x8664" in script
    for field in (
        "ProductName",
        "CompanyName",
        "FileVersion",
        "ProductVersion",
        "OriginalFilename",
    ):
        assert field in script
    assert "PEERBRIDGE_STARTUP_CONTRACT_PATH" in script
    assert "PEERBRIDGE_LAUNCHER_HEADLESS" in script
    assert "[Environment]::SetEnvironmentVariable('LOCALAPPDATA'" in script
    assert "Invoke-StartupLifecycle -Name 'zero-argument'" in script
    assert "Invoke-StartupLifecycle -Name 'cmd-zero-argument' -ViaCmd" in script
    assert "shutdown.request" in script
    assert "left its supervisor process running" in script
    assert "Get-Process -Id $Id" in script
    assert "PEERBRIDGE_SELF_TEST_RECEIPT_PATH" in script
    assert "-RequireReceipt" in script
    assert "receipt.runtime_sha256" in script
    assert "did not create its required receipt" in script
    assert "taskkill" not in script.lower()


def test_published_release_vm_workflow_verifies_the_downloaded_asset() -> None:
    workflow = (
        PROJECT_ROOT / ".github" / "workflows" / "release-vm-acceptance.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "runs-on: windows-2025" in workflow
    assert "gh release download" in workflow
    assert "verify_windows_portable.ps1" in workflow
    assert "-ExpectedSha256 $env:EXPECTED_SHA256" in workflow
    assert "published-release-vm-acceptance.v1" in workflow
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow


def test_windows_ci_runs_the_headless_portable_lifecycle_contract() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "windows-portable-contract:" in workflow
    assert "runs-on: windows-2025" in workflow
    assert "package_windows_portable.ps1" in workflow
    assert "verify_windows_portable.ps1" in workflow
    assert "-ExpectedSha256 $package.Sha256" in workflow
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in workflow
    assert "verify_portable_provenance.py" in workflow
    assert "${{ steps.portable.outputs.licenses }}" in workflow
    assert "gh release create" in workflow
    assert "-Headless" in workflow
    assert "needs: [test, edge-contract, windows-portable-contract]" in workflow


def test_alpha_five_is_published_as_the_latest_normal_release() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "Publish GitHub Alpha release" in workflow
    assert "--latest" in workflow
    assert "--prerelease" not in workflow


def test_brand_assets_are_explicitly_separate_from_apache_code_license() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    trademarks = (PROJECT_ROOT / "TRADEMARKS.md").read_text(encoding="utf-8")
    provenance = (PROJECT_ROOT / "BRAND_ASSETS.md").read_text(encoding="utf-8")
    third_party = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "not licensed under\nApache-2.0" in readme
    assert "does **not** grant a trademark licence" in trademarks
    assert OWNER_SOURCE_SHA256 in provenance
    assert "core Python package has no mandatory third-party runtime dependencies" in third_party
    assert "THIRD_PARTY_LICENSES/LICENSES_MANIFEST.json" in third_party
    assert "exact build interpreter" in third_party
    assert 'license-files = ["LICENSE", "TRADEMARKS.md", "BRAND_ASSETS.md", "THIRD_PARTY_NOTICES.md"]' in pyproject
    assert '"release_support/*.ico"' in pyproject


def test_windows_icon_helpers_are_safe_without_a_windows_icon(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("peerbridge_mcp.monitor.sys.platform", "linux")
    assert apply_windows_window_icon(object(), tmp_path / "missing.ico") == ()
    release_windows_icon_handles((123,))


def test_monitor_reapplies_branding_after_tk_maps_the_native_window() -> None:
    source = (PROJECT_ROOT / "src" / "peerbridge_mcp" / "monitor.py").read_text(
        encoding="utf-8"
    )

    assert "250, self._reapply_mapped_window_icon" in source
    assert "configure_windows_app_identity()" in source
    assert "release_windows_icon_handles(previous_handles)" in source
    assert "self.root.after_cancel(self._window_icon_after_id)" in source
