param(
    [string]$PythonPath,
    [string]$OutputRoot
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

function Get-RelativePortablePath {
    param([string]$BasePath, [string]$FullPath)
    return $FullPath.Substring($BasePath.Length).TrimStart([char[]]'\/').Replace('\', '/')
}

function Get-StringDigest {
    param([string]$Value, [string]$Algorithm)
    $hasher = [System.Security.Cryptography.HashAlgorithm]::Create($Algorithm)
    if (-not $hasher) {
        throw "Unsupported digest algorithm: $Algorithm"
    }
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([System.BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
}
if (-not $PythonPath) {
    $PythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
}
if (-not $OutputRoot) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $OutputRoot = Join-Path $projectRoot ".peerbridge-artifacts\portable-candidates\$stamp"
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $OutputRoot) {
    throw "Portable output is create-only and already exists: $OutputRoot"
}

$buildRoot = Join-Path $OutputRoot 'build'
$artifactRoot = Join-Path $buildRoot 'dist'
$workRoot = Join-Path $buildRoot 'work'
$specRoot = Join-Path $buildRoot 'spec'
& (Join-Path $PSScriptRoot 'build_windows_desktop.ps1') `
    -PythonPath $PythonPath `
    -ArtifactRoot $artifactRoot `
    -WorkRoot $workRoot `
    -SpecRoot $specRoot | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Windows desktop build failed with exit code $LASTEXITCODE"
}

$version = (& $PythonPath -c 'from peerbridge_mcp import __version__; print(__version__)').Trim()
if (-not $version) {
    throw 'Unable to read the PeerBridge package version.'
}
$bundleSource = Join-Path $artifactRoot 'PeerBridgeControlRoom'
$packageName = "PeerBridgeControlRoom-$version-windows-x64-portable"
$stageRoot = Join-Path $OutputRoot $packageName
$bundleTarget = $stageRoot
New-Item -ItemType Directory -Path $stageRoot -Force:$false | Out-Null
Get-ChildItem -LiteralPath $bundleSource | Copy-Item -Destination $bundleTarget -Recurse

$launcher = @'
@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0PeerBridgeControlRoom.exe" (
  >&2 echo PeerBridge Control Room executable is missing.
  exit /b 1
)
"%~dp0PeerBridgeControlRoom.exe"
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" >&2 echo PeerBridge Control Room exited with code %exit_code%.
exit /b %exit_code%
'@
$readme = @"
PeerBridge MCP Control Room $version (Windows x64 portable Alpha)

1. Extract the complete ZIP before running it.
2. Double-click "Launch PeerBridge.cmd".
3. PeerBridge stores local state under %LOCALAPPDATA%\PeerBridge\workspace.

This is an unsigned Alpha build. Windows SmartScreen may ask for confirmation.
Provider credentials are not included. Add your own provider in the Safe Connections page;
raw credentials remain in Windows Credential Manager and are not written to chat history.

Project: https://github.com/oscarho200407-hue/peerbridge-mcp
Security: https://github.com/oscarho200407-hue/peerbridge-mcp/blob/main/SECURITY.md
"@
[System.IO.File]::WriteAllText(
    (Join-Path $stageRoot 'Launch PeerBridge.cmd'),
    $launcher,
    [System.Text.UTF8Encoding]::new($false)
)
[System.IO.File]::WriteAllText(
    (Join-Path $stageRoot 'README.txt'),
    $readme,
    [System.Text.UTF8Encoding]::new($false)
)
$localizedReadmes = [ordered]@{
    'README.zh-Hant.md' = 'docs\alpha-quickstart.zh-Hant.md'
    'README.zh-Hans.md' = 'docs\alpha-quickstart.zh-Hans.md'
}
foreach ($localizedName in $localizedReadmes.Keys) {
    $localizedSource = Join-Path $projectRoot $localizedReadmes[$localizedName]
    if (-not (Test-Path -LiteralPath $localizedSource -PathType Leaf)) {
        throw "Required localized quickstart is missing: $localizedSource"
    }
    Copy-Item -LiteralPath $localizedSource -Destination (Join-Path $stageRoot $localizedName)
}
foreach ($legalName in @('LICENSE', 'TRADEMARKS.md', 'BRAND_ASSETS.md', 'THIRD_PARTY_NOTICES.md')) {
    $legalSource = Join-Path $projectRoot $legalName
    if (-not (Test-Path -LiteralPath $legalSource -PathType Leaf)) {
        throw "Required legal file is missing: $legalSource"
    }
    Copy-Item -LiteralPath $legalSource -Destination (Join-Path $stageRoot $legalName)
}

$runtimeLicenseRoot = Join-Path $stageRoot 'THIRD_PARTY_LICENSES'
$runtimeLicenseJson = & $PythonPath `
    (Join-Path $PSScriptRoot 'collect_windows_runtime_licenses.py') `
    --bundle-root $bundleTarget `
    --output-root $runtimeLicenseRoot
if ($LASTEXITCODE -ne 0) {
    throw "Windows runtime-license collection failed with exit code $LASTEXITCODE"
}
$runtimeLicenseResult = $runtimeLicenseJson | ConvertFrom-Json
if ($runtimeLicenseResult.status -ne 'PASS') {
    throw 'Windows runtime-license collection did not pass.'
}
$runtimeLicenseManifestPath = Join-Path $runtimeLicenseRoot 'LICENSES_MANIFEST.json'
$runtimeLicenseManifestName = 'THIRD_PARTY_LICENSES_MANIFEST.json'
$retainedRuntimeLicenseManifest = Join-Path $OutputRoot $runtimeLicenseManifestName
Copy-Item -LiteralPath $runtimeLicenseManifestPath -Destination $retainedRuntimeLicenseManifest

$sourceEpoch = 0L
if ($env:SOURCE_DATE_EPOCH -match '^\d+$') {
    $sourceEpoch = [long]$env:SOURCE_DATE_EPOCH
} else {
    $gitEpoch = (& git -C $projectRoot show -s --format=%ct HEAD 2>$null).Trim()
    if ($LASTEXITCODE -eq 0 -and $gitEpoch -match '^\d+$') {
        $sourceEpoch = [long]$gitEpoch
    }
}
if ($sourceEpoch -gt 0) {
    $createdUtc = [System.DateTimeOffset]::FromUnixTimeSeconds($sourceEpoch).UtcDateTime.ToString('yyyy-MM-ddTHH:mm:ssZ')
} else {
    $createdUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
}

$sbomName = 'SBOM.spdx.json'
$sbomPath = Join-Path $stageRoot $sbomName
$spdxFiles = @()
$verificationHashes = @()
$inventoryLines = @()
$relationships = @(
    [ordered]@{
        spdxElementId = 'SPDXRef-DOCUMENT'
        relationshipType = 'DESCRIBES'
        relatedSpdxElement = 'SPDXRef-Package-PeerBridge-MCP'
    }
)
foreach ($file in @(Get-ChildItem -LiteralPath $stageRoot -File -Recurse | Sort-Object FullName)) {
    $relative = Get-RelativePortablePath -BasePath $stageRoot -FullPath $file.FullName
    $sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $sha1 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA1).Hash.ToLowerInvariant()
    $fileId = 'SPDXRef-File-' + (Get-StringDigest -Value $relative -Algorithm 'SHA256')
    $spdxFiles += [ordered]@{
        fileName = './' + $relative
        SPDXID = $fileId
        checksums = @([ordered]@{ algorithm = 'SHA256'; checksumValue = $sha256 })
        licenseConcluded = 'NOASSERTION'
        copyrightText = 'NOASSERTION'
    }
    $relationships += [ordered]@{
        spdxElementId = 'SPDXRef-Package-PeerBridge-MCP'
        relationshipType = 'CONTAINS'
        relatedSpdxElement = $fileId
    }
    $verificationHashes += $sha1
    $inventoryLines += $relative + "`0" + $sha256
}
$verificationCode = Get-StringDigest -Value (($verificationHashes | Sort-Object) -join '') -Algorithm 'SHA1'
$inventoryDigest = Get-StringDigest -Value ($inventoryLines -join "`n") -Algorithm 'SHA256'
$sbom = [ordered]@{
    spdxVersion = 'SPDX-2.3'
    dataLicense = 'CC0-1.0'
    SPDXID = 'SPDXRef-DOCUMENT'
    name = "$packageName file inventory"
    documentNamespace = "https://github.com/oscarho200407-hue/peerbridge-mcp/spdx/$version/windows-x64/$inventoryDigest"
    creationInfo = [ordered]@{
        created = $createdUtc
        creators = @('Tool: scripts/package_windows_portable.ps1')
        comment = 'Alpha file inventory; third-party attribution remains qualified by THIRD_PARTY_NOTICES.md.'
    }
    packages = @(
        [ordered]@{
            name = 'peerbridge-mcp'
            SPDXID = 'SPDXRef-Package-PeerBridge-MCP'
            versionInfo = $version
            downloadLocation = 'NOASSERTION'
            filesAnalyzed = $true
            packageVerificationCode = [ordered]@{ packageVerificationCodeValue = $verificationCode }
            licenseConcluded = 'NOASSERTION'
            licenseDeclared = 'Apache-2.0'
            copyrightText = 'NOASSERTION'
        }
    )
    files = $spdxFiles
    relationships = $relationships
}
[System.IO.File]::WriteAllText(
    $sbomPath,
    ($sbom | ConvertTo-Json -Depth 8),
    [System.Text.UTF8Encoding]::new($false)
)

$zipPath = Join-Path $OutputRoot "$packageName.zip"
Compress-Archive -LiteralPath $stageRoot -DestinationPath $zipPath -CompressionLevel Optimal
if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) {
    throw "Portable ZIP was not produced: $zipPath"
}

$archiveSha256 = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$sbomSha256 = (Get-FileHash -LiteralPath $sbomPath -Algorithm SHA256).Hash.ToLowerInvariant()
$runtimeLicenseManifestSha256 = (
    Get-FileHash -LiteralPath $runtimeLicenseManifestPath -Algorithm SHA256
).Hash.ToLowerInvariant()
$sourceCommit = (& git -C $projectRoot rev-parse HEAD 2>$null).Trim()
$sourceTree = (& git -C $projectRoot rev-parse 'HEAD^{tree}' 2>$null).Trim()
$sourceStatus = (& git -C $projectRoot status --porcelain=v1 --untracked-files=all 2>$null) -join "`n"
if ($sourceCommit -notmatch '^[0-9a-f]{40}$' -or $sourceTree -notmatch '^[0-9a-f]{40}$') {
    throw 'Portable provenance could not resolve the source commit and tree.'
}
$provenancePath = Join-Path $OutputRoot "$packageName.provenance.json"
$provenance = [ordered]@{
    schema = 'peerbridge.windows-portable-provenance.v1'
    version = $version
    source_commit = $sourceCommit
    source_tree = $sourceTree
    source_dirty = -not [string]::IsNullOrWhiteSpace($sourceStatus)
    archive_name = [System.IO.Path]::GetFileName($zipPath)
    archive_bytes = (Get-Item -LiteralPath $zipPath).Length
    archive_sha256 = $archiveSha256
    sbom_name = [System.IO.Path]::GetFileName($sbomPath)
    sbom_sha256 = $sbomSha256
    runtime_license_manifest_name = $runtimeLicenseManifestName
    runtime_license_manifest_sha256 = $runtimeLicenseManifestSha256
    packager_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    created_utc = $createdUtc
}
[System.IO.File]::WriteAllText(
    $provenancePath,
    ($provenance | ConvertTo-Json -Depth 4),
    [System.Text.UTF8Encoding]::new($false)
)

[pscustomobject]@{
    Version = $version
    Directory = $stageRoot
    Archive = $zipPath
    Provenance = $provenancePath
    Sbom = $sbomPath
    LicenseManifest = $retainedRuntimeLicenseManifest
    Bytes = (Get-Item -LiteralPath $zipPath).Length
    Sha256 = $archiveSha256
    SbomSha256 = $sbomSha256
    LicenseManifestSha256 = $runtimeLicenseManifestSha256
} | ConvertTo-Json
