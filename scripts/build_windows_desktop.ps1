param(
    [string]$PythonPath,
    [string]$ArtifactRoot,
    [string]$WorkRoot,
    [string]$SpecRoot
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$icon = Join-Path $projectRoot 'src\peerbridge_mcp\release_support\peerbridge-icon.ico'
$entry = Join-Path $projectRoot 'scripts\peerbridge_control_room_entry.py'
$versionTemplate = Join-Path $projectRoot 'scripts\peerbridge_control_room_version_info.template'
$pyInstallerRunner = Join-Path $projectRoot 'scripts\run_pyinstaller.py'
$hookRoot = Join-Path $projectRoot 'scripts\pyinstaller-hooks'
$pyproject = Join-Path $projectRoot 'pyproject.toml'
if (-not $ArtifactRoot) {
    $ArtifactRoot = Join-Path $projectRoot '.peerbridge-artifacts\windows'
}
if (-not $WorkRoot) {
    $WorkRoot = Join-Path $projectRoot '.peerbridge-artifacts\pyinstaller-work'
}
if (-not $SpecRoot) {
    $SpecRoot = Join-Path $projectRoot '.peerbridge-artifacts\pyinstaller-spec'
}
$ArtifactRoot = [System.IO.Path]::GetFullPath($ArtifactRoot)
$WorkRoot = [System.IO.Path]::GetFullPath($WorkRoot)
$SpecRoot = [System.IO.Path]::GetFullPath($SpecRoot)

if (-not $PythonPath) {
    $PythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
}
foreach ($requiredPath in @($PythonPath, $icon, $entry, $versionTemplate, $pyInstallerRunner, $pyproject)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required Windows build input not found: $requiredPath"
    }
}
if (-not (Test-Path -LiteralPath $hookRoot -PathType Container)) {
    throw "Required PyInstaller hook directory not found: $hookRoot"
}

$packageVersion = (& $PythonPath $pyInstallerRunner --peerbridge-project-version $pyproject).Trim()
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to read the package version from pyproject.toml.'
}
$versionMatch = [regex]::Match(
    $packageVersion,
    '^(\d+)\.(\d+)\.(\d+)(?:(?:a|b|rc)(\d+)(?:\.post(\d+))?)?$'
)
if (-not $versionMatch.Success) {
    throw "Unsupported package version for Windows metadata: $packageVersion"
}
$preRelease = if ($versionMatch.Groups[4].Success) { [int]$versionMatch.Groups[4].Value } else { 0 }
$maintenance = if ($versionMatch.Groups[5].Success) { [int]$versionMatch.Groups[5].Value } else { 0 }
if ($maintenance -gt 999 -or ($maintenance -gt 0 -and $preRelease -gt 65)) {
    throw "Package maintenance version cannot be mapped to Windows metadata: $packageVersion"
}
$revision = if ($maintenance -gt 0) { ($preRelease * 1000) + $maintenance } else { $preRelease }
$versionParts = @(
    [int]$versionMatch.Groups[1].Value,
    [int]$versionMatch.Groups[2].Value,
    [int]$versionMatch.Groups[3].Value,
    $revision
)
if (($versionParts | Where-Object { $_ -gt 65535 }).Count -ne 0) {
    throw "Windows version components must be no greater than 65535: $packageVersion"
}
$fileVersion = $versionParts -join '.'
$versionTuple = $versionParts -join ', '
[System.IO.Directory]::CreateDirectory($WorkRoot) | Out-Null
$versionFile = Join-Path $WorkRoot 'PeerBridgeControlRoom.version.txt'
$versionText = [System.IO.File]::ReadAllText($versionTemplate)
$versionText = $versionText.Replace('@FILE_VERSION_TUPLE@', $versionTuple)
$versionText = $versionText.Replace('@FILE_VERSION@', $fileVersion)
$versionText = $versionText.Replace('@PRODUCT_VERSION@', $packageVersion)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($versionFile, $versionText, $utf8NoBom)

& $PythonPath $pyInstallerRunner `
    --noconfirm `
    --windowed `
    --name PeerBridgeControlRoom `
    --icon $icon `
    --version-file $versionFile `
    --paths (Join-Path $projectRoot 'src') `
    --additional-hooks-dir $hookRoot `
    --collect-data peerbridge_mcp `
    --hidden-import cryptography.hazmat.primitives.hashes `
    --hidden-import cryptography.hazmat.primitives.serialization `
    --hidden-import cryptography.hazmat.primitives.asymmetric.padding `
    --hidden-import cryptography.hazmat.primitives.asymmetric.rsa `
    --hidden-import cryptography.hazmat.primitives.ciphers.aead `
    --distpath $ArtifactRoot `
    --workpath $WorkRoot `
    --specpath $SpecRoot `
    $entry
if ($LASTEXITCODE -ne 0) {
    throw "PeerBridge Windows desktop build failed with exit code $LASTEXITCODE"
}

$executable = Join-Path $ArtifactRoot 'PeerBridgeControlRoom\PeerBridgeControlRoom.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "PeerBridge Windows executable was not produced: $executable"
}

[pscustomobject]@{
    Executable = (Resolve-Path -LiteralPath $executable).Path
    Icon = (Resolve-Path -LiteralPath $icon).Path
    ProductVersion = $packageVersion
    FileVersion = $fileVersion
    Bytes = (Get-Item -LiteralPath $executable).Length
    Sha256 = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
} | ConvertTo-Json
