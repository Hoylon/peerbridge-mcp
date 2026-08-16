[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PrivateStore = (Join-Path $env:LOCALAPPDATA "PeerBridge\maintainer")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $IsWindows) {
    throw "PeerBridge DPAPI support identity setup requires Windows."
}
if (-not $env:LOCALAPPDATA -and -not $PSBoundParameters.ContainsKey("PrivateStore")) {
    throw "LOCALAPPDATA is unavailable; pass an explicit -PrivateStore path."
}

$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$supportRoot = Join-Path $root "support"
if (-not (Test-Path -LiteralPath $supportRoot -PathType Container)) {
    throw "The project support directory is unavailable."
}

$privateRoot = [IO.Path]::GetFullPath($PrivateStore)
$privatePath = Join-Path $privateRoot "peerbridge-support-private.pkcs8.dpapi"
$publicPath = Join-Path $supportRoot "peerbridge-support-public.pub"
if ((Test-Path -LiteralPath $privatePath) -or (Test-Path -LiteralPath $publicPath)) {
    throw "A support identity already exists. This create-only command will not rotate or overwrite it."
}

[IO.Directory]::CreateDirectory($privateRoot) | Out-Null
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $privateRoot /inheritance:r /grant:r "${identity}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not apply the maintainer-store access policy."
}

$rsa = [Security.Cryptography.RSA]::Create()
$privateBytes = $null
$protectedBytes = $null
try {
    $rsa.KeySize = 3072
    $privateBytes = $rsa.ExportPkcs8PrivateKey()
    $publicBytes = $rsa.ExportSubjectPublicKeyInfo()
    $entropy = [Text.Encoding]::UTF8.GetBytes("PeerBridgeFeedbackIdentityV1")
    $protectedBytes = [Security.Cryptography.ProtectedData]::Protect(
        $privateBytes,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )

    $privateStream = [IO.File]::Open(
        $privatePath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $privateStream.Write($protectedBytes, 0, $protectedBytes.Length)
        $privateStream.Flush($true)
    }
    finally {
        $privateStream.Dispose()
    }

    $encoded = [Convert]::ToBase64String($publicBytes)
    $lines = for ($offset = 0; $offset -lt $encoded.Length; $offset += 64) {
        $count = [Math]::Min(64, $encoded.Length - $offset)
        $encoded.Substring($offset, $count)
    }
    $pem = "-----BEGIN PUBLIC KEY-----`n" + ($lines -join "`n") + "`n-----END PUBLIC KEY-----`n"
    $publicStream = [IO.File]::Open(
        $publicPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $payload = [Text.UTF8Encoding]::new($false).GetBytes($pem)
        $publicStream.Write($payload, 0, $payload.Length)
        $publicStream.Flush($true)
    }
    finally {
        $publicStream.Dispose()
    }

    $sha = [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData([IO.File]::ReadAllBytes($publicPath))
    ).ToLowerInvariant()
    [pscustomobject]@{
        Schema = "peerbridge.support-identity-setup.v1"
        PublicKeyPath = $publicPath
        PublicKeySha256 = $sha
        ProtectedPrivateKeyPath = $privatePath
        Protection = "windows-dpapi-current-user"
    } | ConvertTo-Json
}
finally {
    if ($privateBytes) {
        [Security.Cryptography.CryptographicOperations]::ZeroMemory($privateBytes)
    }
    if ($protectedBytes) {
        [Security.Cryptography.CryptographicOperations]::ZeroMemory($protectedBytes)
    }
    $rsa.Dispose()
}
