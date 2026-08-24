from __future__ import annotations

import hashlib
import runpy
import struct
import sys
from types import SimpleNamespace
from pathlib import Path

from peerbridge_mcp.monitor import (
    DEFAULT_INSTANCE_MUTEX,
    WINDOWS_APP_USER_MODEL_ID,
    apply_windows_window_icon,
    packaged_icon_paths,
    release_windows_icon_handles,
    windows_instance_mutex_name,
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


def test_portable_verifier_instance_mutex_is_bounded() -> None:
    assert windows_instance_mutex_name("") == DEFAULT_INSTANCE_MUTEX
    assert (
        windows_instance_mutex_name("verify-0123456789abcdef")
        == DEFAULT_INSTANCE_MUTEX + "-verify-0123456789abcdef"
    )
    for invalid in ("short", r"..\global", "contains space", "x" * 65):
        try:
            windows_instance_mutex_name(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe instance id accepted: {invalid!r}")


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
    assert "--collect-all webview" not in build_script
    assert "--hidden-import webview" in build_script
    assert "--collect-data peerbridge_mcp" not in build_script
    assert "$packageData = [ordered]@{" in build_script
    assert "@dataArguments" in build_script
    assert "workbench\\app.js" in build_script
    assert "acpx_runtime_bridge.mjs" in build_script
    assert "--exclude-module cffi._shimmed_dist_utils" in build_script
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
    assert "main as monitor_main" in entry
    assert "make_server, run_native_workbench" in entry
    assert "return run_native_workbench(workbench)" in entry
    assert 'args[:1] == ["--legacy-pixel"]' in entry
    webview_hook = (
        PROJECT_ROOT / "scripts" / "pyinstaller-hooks" / "hook-webview.py"
    ).read_text(encoding="utf-8")
    assert 'collect_data_files("webview", subdir="lib")' in webview_hook
    assert 'collect_data_files("webview", subdir="js")' in webview_hook
    assert 'collect_dynamic_libs("webview")' in webview_hook
    assert '"webview.platforms.edgechromium"' in webview_hook
    top_level_imports = entry.split("STARTUP_TIMEOUT_SECONDS", maxsplit=1)[0]
    assert "peerbridge_mcp.monitor" not in top_level_imports
    assert "peerbridge_mcp.mailbox_supervisor" not in top_level_imports
    assert "peerbridge_mcp.cli" not in top_level_imports
    assert "def _ensure_frozen_standard_streams()" in entry
    assert 'open(os.devnull, "w", encoding="utf-8")' in entry
    assert "_ensure_frozen_standard_streams()" in entry
    assert "PEERBRIDGE_SELF_TEST_RECEIPT_PATH" in entry
    assert "peerbridge-packaged-self-test-v1" in entry
    assert 'args == ["--announcement-self-test"]' in entry
    assert 'args == ["--modern-workbench-self-test"]' in entry
    assert '"test": "modern-workbench"' in entry
    assert '"navigation_panel_count": 12' in entry
    assert '"test": "announcement-feed"' in entry
    assert "_write_json_create_only(receipt_path, receipt)" in entry
    assert '"runtime_sha256": _runtime_sha256()' in entry
    assert "import traceback" not in entry
    assert 'args[:2] == ["-m", "peerbridge_mcp"]' in entry
    assert "return cli_main(args[2:])" in entry
    assert "_start_managed_supervisor" in entry
    assert "def _activate_existing_control_room" in entry
    assert '"status": "existing-instance"' in entry
    assert "OpenMutexW" in entry
    assert "existing-instance" in launcher
    assert "Test-Path -LiteralPath $readyPath" in launcher
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


def test_existing_instance_receipt_reports_the_running_binary_not_the_new_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_path = PROJECT_ROOT / "scripts" / "peerbridge_control_room_entry.py"
    namespace = runpy.run_path(str(entry_path), run_name="peerbridge_entry_identity_test")
    requested = tmp_path / "requested.exe"
    running = tmp_path / "running.exe"
    requested.write_bytes(b"requested build")
    running.write_bytes(b"older running build")
    payload_factory = namespace["_existing_instance_payload"]
    monkeypatch.setitem(payload_factory.__globals__, "_runtime_path", lambda: requested)
    monkeypatch.setitem(payload_factory.__globals__, "_runtime_kind", lambda: "frozen")

    payload = payload_factory(
        {
            "existing_pid": 1234,
            "runtime_kind": "frozen",
            "runtime_path": str(running),
            "runtime_sha256": hashlib.sha256(running.read_bytes()).hexdigest(),
        }
    )

    assert payload["runtime_path"] == str(running)
    assert payload["runtime_sha256"] == hashlib.sha256(running.read_bytes()).hexdigest()
    assert payload["requested_runtime_path"] == str(requested)
    assert payload["requested_runtime_sha256"] == hashlib.sha256(
        requested.read_bytes()
    ).hexdigest()
    assert payload["same_runtime"] is False
    assert payload["version"] is None


def test_missing_instance_mutex_returns_no_existing_control_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_path = PROJECT_ROOT / "scripts" / "peerbridge_control_room_entry.py"
    namespace = runpy.run_path(str(entry_path), run_name="peerbridge_entry_mutex_test")
    activate = namespace["_activate_existing_control_room"]

    class FakeFunction:
        def __init__(self, result: object) -> None:
            self.result = result

        def __call__(self, *_args: object) -> object:
            return self.result

    kernel32 = SimpleNamespace(
        OpenMutexW=FakeFunction(0),
        CloseHandle=FakeFunction(True),
    )
    fake_ctypes = SimpleNamespace(
        WinDLL=lambda *_args, **_kwargs: kernel32,
        c_uint32=object,
        c_bool=object,
        c_wchar_p=object,
        c_void_p=object,
    )
    monkeypatch.setitem(activate.__globals__, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(activate.__globals__, "ctypes", fake_ctypes)

    assert activate(wait_seconds=0) is None


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
    assert "primaryPackagePurpose = 'APPLICATION'" in script
    assert "primaryPackagePurpose = 'LIBRARY'" in script
    assert "relationshipType = 'DEPENDS_ON'" in script
    assert "referenceType = 'purl'" in script
    assert "support_public_key_sha256" in script
    assert "Portable support configuration does not bind the packaged public key" in script
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
    assert '"pywebview", "PyWebView"' in script
    assert '"pythonnet"' in script
    assert '"clr-loader"' in script
    assert '"Tcl-Tk"' in script
    assert '"sha256"' in script
    assert '"spdx_id"' in script
    assert '"license_declared"' in script
    assert '"package_url"' in script
    assert "create-only" in script


def test_runtime_license_manifest_emits_component_sbom_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import json

    from scripts import collect_windows_runtime_licenses as collector

    bundle = tmp_path / "bundle"
    internal = bundle / "_internal"
    crypto_licenses = internal / "cryptography-50.0.0.dist-info" / "licenses"
    tk_data = internal / "_tk_data"
    crypto_licenses.mkdir(parents=True)
    tk_data.mkdir()
    (internal / "python313.dll").write_bytes(b"python")
    (internal / "_cffi_backend.cp313-win_amd64.pyd").write_bytes(b"cffi")
    (internal / "tcl86t.dll").write_bytes(b"tcl")
    for marker in ("webview", "pythonnet", "clr_loader"):
        (internal / marker).mkdir()
    (crypto_licenses / "LICENSE").write_text("crypto license\n", encoding="utf-8")
    (tk_data / "license.terms").write_text("tcl license\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "LICENSE.txt").write_text("python license\n", encoding="utf-8")
    monkeypatch.setattr(collector.sys, "base_prefix", str(runtime))

    versions = {
        "pyinstaller": "6.22.1",
        "cffi": "2.1.1",
        "pywebview": "6.1",
        "pythonnet": "3.0.5",
        "clr-loader": "0.2.7.post0",
    }

    def fake_distribution_licenses(
        distribution_name: str,
        component_name: str,
        output_root: Path,
    ):
        source = tmp_path / f"{distribution_name}-LICENSE.txt"
        source.write_text(f"{distribution_name} license\n", encoding="utf-8")
        version = versions[distribution_name]
        record = collector._copy_license(
            source,
            output_root,
            f"{distribution_name}-{version}-LICENSE.txt",
            component_name,
            f"fixture:{distribution_name}",
        )
        return version, "MIT", [record]

    monkeypatch.setattr(collector, "_distribution_licenses", fake_distribution_licenses)
    monkeypatch.setattr(
        collector.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="50.0.0",
            metadata={"License-Expression": "Apache-2.0 OR BSD-3-Clause"},
        ),
    )

    output = tmp_path / "licenses"
    collector.collect(bundle, output)
    manifest = json.loads(
        (output / "LICENSES_MANIFEST.json").read_text(encoding="utf-8")
    )
    components = {item["name"]: item for item in manifest["components"]}

    assert set(components) == {
        "Python",
        "PyInstaller",
        "cryptography",
        "cffi",
        "PyWebView",
        "pythonnet",
        "clr-loader",
        "Tcl-Tk",
    }
    assert components["Python"]["license_declared"] == "Python-2.0"
    assert components["cryptography"]["package_url"] == "pkg:pypi/cryptography@50.0.0"
    assert components["PyInstaller"]["spdx_id"] == "SPDXRef-Package-Runtime-PyInstaller"
    assert all(item["package_url"].startswith("pkg:") for item in components.values())


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
    assert "[System.IO.FileMode]::CreateNew" in script
    assert "$stagedArchive" in script
    assert "$archiveItem = Get-Item -LiteralPath $Archive" in script
    assert "Add-Type -AssemblyName System.IO.Compression" in script
    assert "Add-Type -AssemblyName System.IO.Compression.FileSystem" in script
    assert "$sha256.ComputeHash($archiveStream)" in script
    assert "Expand-Archive -LiteralPath $stagedArchive" in script
    assert script.index("$archiveStream.Dispose()", script.index("$zip.Dispose()")) < script.index(
        "Expand-Archive -LiteralPath $stagedArchive"
    )
    assert "Get-FileHash -LiteralPath $Archive" not in script
    assert "--ui-self-test" in script
    assert "@('1.0', '1.25', '1.5')" in script
    assert "--ui-scale-factor" in script
    assert "@('zh-Hant', 'zh-Hans', 'en')" in script
    assert "@('pixel', 'modern')" in script
    assert "--locale" in script
    assert "--theme" in script
    assert "--send-self-test" in script
    assert "--announcement-self-test" in script
    assert "SkipLiveAnnouncement" in script
    assert script.index("-Name 'create-only-init'") < script.index(
        '-Name "ui-self-test-$uiLocale-$uiTheme-$scaleName"'
    )
    assert "$monitorArguments = @(" in script
    assert "'--ui-self-test'," in script
    assert "'--locale', $uiLocale" in script
    assert "'--theme', $uiTheme" in script
    assert "-Arguments ($monitorArguments + @('--send-self-test'))" in script
    assert "'peerbridge_mcp', 'doctor'" in script
    assert "Portable create-only init did not create" in script
    assert "Portable localized quickstart differs from source" in script
    assert "$leaf -eq 'direct_url.json'" in script
    assert "THIRD_PARTY_NOTICES.md" in script
    assert "peerbridge.windows-runtime-licenses.v1" in script
    assert "Portable runtime-license file differs from its manifest" in script
    assert "Portable runtime-license manifest omits bundled cffi" in script
    assert "Portable runtime-license manifest omits bundled" in script
    assert "Portable SPDX SBOM file count differs" in script
    assert "Portable SPDX SBOM checksum differs" in script
    assert "package verification code is invalid" in script
    assert "document namespace does not bind" in script
    assert "dependency relationships differ from runtime components" in script
    assert "Portable support configuration does not bind the packaged public key" in script
    assert "SupportPublicKeySha256" in script
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
    assert "PEERBRIDGE_INSTANCE_ID" in script
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


def test_ui_self_test_is_hidden_before_layout_and_runs_offscreen() -> None:
    source = (PROJECT_ROOT / "src" / "peerbridge_mcp" / "monitor.py").read_text(
        encoding="utf-8"
    )

    assert "hidden_self_test=True" in source
    assert "if hidden_self_test:" in source
    assert 'self.root.attributes("-alpha", 0.0)' in source
    assert "self.root.overrideredirect(True)" in source
    assert 'self.root.geometry("980x650-32000-32000")' in source


def test_published_release_vm_workflow_verifies_the_downloaded_asset() -> None:
    workflow = (
        PROJECT_ROOT / ".github" / "workflows" / "release-vm-acceptance.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "runs-on: windows-2025" in workflow
    assert "gh release download" in workflow
    assert "verify_windows_portable.ps1" in workflow
    assert "verify_portable_provenance.py" in workflow
    assert "-ExpectedSha256 $env:EXPECTED_SHA256" in workflow
    assert "--expected-archive-name $env:RELEASE_ASSET" in workflow
    assert "--expected-archive-sha256 $env:EXPECTED_SHA256" in workflow
    assert "release_ref=refs/tags/$($env:RELEASE_TAG)" in workflow
    assert "tag = $env:RELEASE_TAG" in workflow
    assert "tag = '${{ inputs.tag }}'" not in workflow
    assert "published-release-vm-acceptance.v2" in workflow
    assert "source_commit = $env:SOURCE_COMMIT" in workflow
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
    assert "--expected-archive-name $archives[0].Name" in workflow
    assert "--expected-archive-sha256 $env:EXPECTED_SHA256" in workflow
    assert "${{ steps.portable.outputs.licenses }}" in workflow
    assert "gh release create" in workflow
    assert "-Headless" in workflow
    assert "-SkipLiveAnnouncement" in workflow
    assert "clean-windows-release-gate:" in workflow
    assert "peerbridge.clean-windows-release-gate.v1" in workflow
    assert (
        "needs: [test, edge-contract, windows-portable-contract, "
        "clean-windows-release-gate]" in workflow
    )


def test_alpha_five_is_published_as_a_normal_non_latest_release() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "Publish GitHub Alpha release" in workflow
    assert "--latest=false" in workflow
    assert "--prerelease" not in workflow
    assert (
        "--notes-file release-publication/docs/"
        "GITHUB_ALPHA_5_2_RELEASE_DRAFT_20260819.md"
    ) in workflow
    assert "--notes-file docs/GITHUB_ALPHA_RELEASE_DRAFT_20260818.md" not in workflow


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
