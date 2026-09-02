from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "launch_tailcat_remote.ps1"
DOC = ROOT / "docs" / "tailcat-remote.md"


def test_tailcat_launcher_is_opt_in_hash_bound_and_foreground() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ExpectedSha256" in source
    assert "[Security.Cryptography.SHA256]::Create()" in source
    assert "$sha256.ComputeHash($stream)" in source
    assert "ReparsePoint" in source
    assert '"--key=new"' in source
    assert "Get-AllowedClientArguments -Keys $AllowClientKey -Required" in source
    assert '("--allow=" + ($Keys -join ","))' in source
    assert "& $tailcat @arguments" in source
    assert "Start-Process" not in source
    assert "Invoke-WebRequest" not in source
    assert "no-auth-ssh" in source
    assert 'throw "PeerBridge never launches Tailcat no-auth-ssh."' in source


def test_tailcat_high_risk_modes_need_separate_switches() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "if ($EnableWrite)" in source
    assert "if (-not $EnableExitNode)" in source
    assert "ExitNode requires the explicit EnableExitNode switch" in source
    assert '"--files=$served`:$access"' in source
    assert 'Get-Command -Name $CommandPath -CommandType Application' in source


def test_tailcat_managed_server_combines_default_services_with_real_gates() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"ManagedServer"' in source
    assert "[string]$ServerKeyFile" in source
    assert "[int]$SshPort = 22" in source
    assert 'Resolve-RegularFile -Path $ServerKeyFile -Description "ManagedServer"' in source
    assert 'Get-AllowedClientArguments -Keys $AllowClientKey -Required' in source
    assert '"${Port},${SshPort},exit-node"' in source
    assert "ManagedServer requires the explicit EnableExitNode switch" in source


def test_tailcat_client_token_is_file_bound_not_a_raw_parameter() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    parameter_block = source.split(")\n\n$ErrorActionPreference", 1)[0]
    assert "[string]$TokenFile" in parameter_block
    assert "[string]$Token," not in parameter_block
    assert "Read-TailcatToken -Path $TokenFile" in source
    assert '"^tc[A-Za-z0-9_-]{40,4096}$"' in source


def test_tailcat_document_keeps_browser_and_production_boundaries_explicit() -> None:
    source = DOC.read_text(encoding="utf-8")
    assert "Tailscale Serve" in source
    assert "does not replace PeerBridge authorization" in source
    assert "DERP-relayed" in source
    assert "not a general authenticated HTTP tunnel" in source
    assert "Do not expose the unauthenticated local WebView workbench" in source
