param(
    [Parameter(Mandatory = $true)]
    [string]$Archive,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string]$ExpectedSha256,
    [string]$OutputRoot,
    [switch]$Headless,
    [switch]$SkipLiveAnnouncement
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$maxArchiveBytes = 1GB
$maxEntries = 20000
$maxExpandedBytes = 1GB
$maxMemberBytes = 256MB
$maxCompressionRatio = 100.0

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
$Archive = (Resolve-Path -LiteralPath $Archive).Path
$archiveItem = Get-Item -LiteralPath $Archive
if ($archiveItem.Length -gt $maxArchiveBytes) {
    throw "Portable archive exceeds the $maxArchiveBytes-byte input limit."
}
$archiveSha256 = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($archiveSha256 -cne $ExpectedSha256.ToLowerInvariant()) {
    throw 'Portable archive SHA-256 differs from the independently supplied expected digest.'
}
if (-not $OutputRoot) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $OutputRoot = Join-Path $projectRoot ".peerbridge-artifacts\portable-verification\$stamp"
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $OutputRoot) {
    throw "Portable verification output is create-only and already exists: $OutputRoot"
}
New-Item -ItemType Directory -Path $OutputRoot -Force:$false | Out-Null

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
try {
    $entries = @($zip.Entries)
    if ($entries.Count -lt 3) {
        throw 'Portable archive contains too few entries.'
    }
    if ($entries.Count -gt $maxEntries) {
        throw "Portable archive exceeds the $maxEntries-entry limit."
    }
    $roots = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    $normalizedNames = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    [long]$expandedBytes = 0
    foreach ($entry in $entries) {
        $name = $entry.FullName.Replace('\', '/')
        if (
            [string]::IsNullOrWhiteSpace($name) -or $name.Length -gt 1024 -or
            $name.StartsWith('/') -or $name -match '^[A-Za-z]:'
        ) {
            throw "Unsafe portable archive member: $name"
        }
        $parts = @($name.Split('/') | Where-Object { $_ -ne '' })
        if ($parts.Count -eq 0 -or $parts -contains '..') {
            throw "Unsafe portable archive member: $name"
        }
        $normalizedName = $parts -join '/'
        if (-not $normalizedNames.Add($normalizedName)) {
            throw "Portable archive contains a duplicate normalized member: $normalizedName"
        }
        if ($entry.Length -gt $maxMemberBytes) {
            throw "Portable archive member exceeds the $maxMemberBytes-byte limit: $name"
        }
        $expandedBytes += [long]$entry.Length
        if ($expandedBytes -gt $maxExpandedBytes) {
            throw "Portable archive exceeds the $maxExpandedBytes-byte expanded limit."
        }
        if ($entry.Length -gt 0) {
            $ratio = [double]$entry.Length / [Math]::Max(1.0, [double]$entry.CompressedLength)
            if ($ratio -gt $maxCompressionRatio) {
                throw "Portable archive member exceeds the compression-ratio limit: $name"
            }
        }
        [void]$roots.Add($parts[0])
        $leaf = $parts[-1].ToLowerInvariant()
        if (
            $leaf -eq '.env' -or $leaf.EndsWith('.pem') -or $leaf.EndsWith('.key') -or
            $leaf.EndsWith('.p12') -or $leaf.EndsWith('.pfx') -or
            $leaf -eq 'direct_url.json' -or
            $leaf.EndsWith('.sqlite') -or $leaf.EndsWith('.sqlite3') -or
            $leaf.EndsWith('.db') -or $leaf.EndsWith('.log')
        ) {
            throw "Sensitive or runtime file is forbidden in the portable archive: $name"
        }
    }
    if ($roots.Count -ne 1) {
        throw 'Portable archive must have exactly one top-level directory.'
    }
} finally {
    $zip.Dispose()
}

Expand-Archive -LiteralPath $Archive -DestinationPath $OutputRoot
$packageRoot = Get-ChildItem -LiteralPath $OutputRoot -Directory | Select-Object -First 1
if (-not $packageRoot) {
    throw 'Portable archive did not extract a package directory.'
}
$executable = Join-Path $packageRoot.FullName 'PeerBridgeControlRoom.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Portable executable is missing: $executable"
}
foreach ($legalName in @('LICENSE', 'TRADEMARKS.md', 'BRAND_ASSETS.md', 'THIRD_PARTY_NOTICES.md')) {
    $source = Join-Path $projectRoot $legalName
    $packaged = Join-Path $packageRoot.FullName $legalName
    if (-not (Test-Path -LiteralPath $packaged -PathType Leaf)) {
        throw "Portable legal file is missing: $legalName"
    }
    $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    $packagedHash = (Get-FileHash -LiteralPath $packaged -Algorithm SHA256).Hash
    if ($sourceHash -ne $packagedHash) {
        throw "Portable legal file differs from source: $legalName"
    }
}
$localizedReadmes = [ordered]@{
    'README.zh-Hant.md' = 'docs\alpha-quickstart.zh-Hant.md'
    'README.zh-Hans.md' = 'docs\alpha-quickstart.zh-Hans.md'
}
foreach ($localizedName in $localizedReadmes.Keys) {
    $source = Join-Path $projectRoot $localizedReadmes[$localizedName]
    $packaged = Join-Path $packageRoot.FullName $localizedName
    if (-not (Test-Path -LiteralPath $packaged -PathType Leaf)) {
        throw "Portable localized quickstart is missing: $localizedName"
    }
    if (
        (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $packaged -Algorithm SHA256).Hash
    ) {
        throw "Portable localized quickstart differs from source: $localizedName"
    }
}

$runtimeLicenseRoot = Join-Path $packageRoot.FullName 'THIRD_PARTY_LICENSES'
$runtimeLicenseManifestPath = Join-Path $runtimeLicenseRoot 'LICENSES_MANIFEST.json'
if (-not (Test-Path -LiteralPath $runtimeLicenseManifestPath -PathType Leaf)) {
    throw 'Portable runtime-license manifest is missing.'
}
try {
    $runtimeLicenseManifest = Get-Content `
        -LiteralPath $runtimeLicenseManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    throw 'Portable runtime-license manifest is not valid UTF-8 JSON.'
}
if (
    $runtimeLicenseManifest.schema -ne 'peerbridge.windows-runtime-licenses.v1' -or
    $runtimeLicenseManifest.python_implementation -ne 'CPython'
) {
    throw 'Portable runtime-license manifest identity is invalid.'
}
$runtimeComponents = @($runtimeLicenseManifest.components)
$runtimeComponentNames = @($runtimeComponents | ForEach-Object { [string]$_.name })
$uniqueRuntimeComponentNames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::Ordinal
)
foreach ($runtimeComponentName in $runtimeComponentNames) {
    if (
        [string]::IsNullOrWhiteSpace($runtimeComponentName) -or
        -not $uniqueRuntimeComponentNames.Add($runtimeComponentName)
    ) {
        throw "Portable runtime-license component is missing or duplicated: $runtimeComponentName"
    }
}
foreach ($requiredComponent in @('Python', 'PyInstaller', 'cryptography', 'Tcl-Tk')) {
    if ($runtimeComponentNames -cnotcontains $requiredComponent) {
        throw "Portable runtime-license manifest omits $requiredComponent."
    }
}
$cffiRuntimePresent = @(
    Get-ChildItem -LiteralPath (Join-Path $packageRoot.FullName '_internal') `
        -Filter '_cffi_backend*.pyd' -File
).Count -gt 0
if ($cffiRuntimePresent -and $runtimeComponentNames -cnotcontains 'cffi') {
    throw 'Portable runtime-license manifest omits bundled cffi.'
}
$runtimeLicenseFiles = @(
    Get-ChildItem -LiteralPath $runtimeLicenseRoot -File |
        Where-Object { $_.Name -ne 'LICENSES_MANIFEST.json' }
)
$runtimeLicenseRecords = @($runtimeLicenseManifest.files)
if ($runtimeLicenseFiles.Count -ne $runtimeLicenseRecords.Count) {
    throw 'Portable runtime-license file count differs from its manifest.'
}
$runtimeLicenseNames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::Ordinal
)
foreach ($record in $runtimeLicenseRecords) {
    $relative = [string]$record.path
    if (
        [System.IO.Path]::GetFileName($relative) -cne $relative -or
        -not $runtimeLicenseNames.Add($relative)
    ) {
        throw "Portable runtime-license path is invalid or duplicated: $relative"
    }
    $licensePath = Join-Path $runtimeLicenseRoot $relative
    if (-not (Test-Path -LiteralPath $licensePath -PathType Leaf)) {
        throw "Portable runtime-license file is missing: $relative"
    }
    $licenseItem = Get-Item -LiteralPath $licensePath
    $licenseSha256 = (
        Get-FileHash -LiteralPath $licensePath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if (
        [long]$record.bytes -ne $licenseItem.Length -or
        ([string]$record.sha256).ToLowerInvariant() -cne $licenseSha256
    ) {
        throw "Portable runtime-license file differs from its manifest: $relative"
    }
    if (-not $uniqueRuntimeComponentNames.Contains([string]$record.component)) {
        throw "Portable runtime-license file has an unknown component: $relative"
    }
}
foreach ($component in $runtimeComponents) {
    if ([string]::IsNullOrWhiteSpace([string]$component.version)) {
        throw "Portable runtime-license component lacks a version: $($component.name)"
    }
    foreach ($licenseName in @($component.licenses)) {
        if (-not $runtimeLicenseNames.Contains([string]$licenseName)) {
            throw "Portable runtime-license component has an unbound license: $($component.name)"
        }
    }
}

$sbomPath = Join-Path $packageRoot.FullName 'SBOM.spdx.json'
if (-not (Test-Path -LiteralPath $sbomPath -PathType Leaf)) {
    throw 'Portable SPDX SBOM is missing: SBOM.spdx.json'
}
try {
    $sbom = Get-Content -LiteralPath $sbomPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    throw 'Portable SPDX SBOM is not valid UTF-8 JSON.'
}
if (
    $sbom.spdxVersion -ne 'SPDX-2.3' -or $sbom.dataLicense -ne 'CC0-1.0' -or
    $sbom.SPDXID -ne 'SPDXRef-DOCUMENT'
) {
    throw 'Portable SPDX SBOM has an invalid document identity.'
}
$packages = @($sbom.packages)
if ($packages.Count -ne 1) {
    throw 'Portable SPDX SBOM must describe exactly one PeerBridge package.'
}
$package = $packages[0]
$packageMatch = [regex]::Match(
    $packageRoot.Name,
    '^PeerBridgeControlRoom-(?<version>.+)-windows-x64-portable$'
)
if (
    -not $packageMatch.Success -or $package.name -ne 'peerbridge-mcp' -or
    $package.SPDXID -ne 'SPDXRef-Package-PeerBridge-MCP' -or
    $package.versionInfo -ne $packageMatch.Groups['version'].Value -or
    $package.filesAnalyzed -ne $true -or $package.licenseDeclared -ne 'Apache-2.0'
) {
    throw 'Portable SPDX SBOM package metadata differs from the archive identity.'
}

$actualFiles = @(
    Get-ChildItem -LiteralPath $packageRoot.FullName -File -Recurse |
        Where-Object { $_.FullName -ne $sbomPath } |
        Sort-Object FullName
)
$actualByRelative = @{}
$verificationHashes = @()
$inventoryLines = @()
foreach ($file in $actualFiles) {
    $relative = Get-RelativePortablePath -BasePath $packageRoot.FullName -FullPath $file.FullName
    if ($actualByRelative.ContainsKey($relative)) {
        throw "Portable archive has a duplicate normalized file path: $relative"
    }
    $sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $actualByRelative[$relative] = $sha256
    $verificationHashes += (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA1).Hash.ToLowerInvariant()
    $inventoryLines += $relative + "`0" + $sha256
}

$records = @($sbom.files)
if ($records.Count -ne $actualFiles.Count) {
    throw 'Portable SPDX SBOM file count differs from the extracted package.'
}
$seenNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
$seenIds = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
foreach ($record in $records) {
    $fileName = [string]$record.fileName
    if (-not $fileName.StartsWith('./') -or $fileName.Contains('\')) {
        throw "Portable SPDX SBOM has an unsafe file name: $fileName"
    }
    $relative = $fileName.Substring(2)
    $parts = @($relative.Split('/') | Where-Object { $_ -ne '' })
    if ($parts.Count -eq 0 -or $parts -contains '..' -or $parts -contains '.') {
        throw "Portable SPDX SBOM has an unsafe file name: $fileName"
    }
    if (-not $seenNames.Add($relative) -or -not $seenIds.Add([string]$record.SPDXID)) {
        throw 'Portable SPDX SBOM contains duplicate file names or SPDX identifiers.'
    }
    $expectedId = 'SPDXRef-File-' + (Get-StringDigest -Value $relative -Algorithm 'SHA256')
    if ($record.SPDXID -ne $expectedId -or -not $actualByRelative.ContainsKey($relative)) {
        throw "Portable SPDX SBOM file identity is invalid: $fileName"
    }
    $sha256Records = @($record.checksums | Where-Object { $_.algorithm -eq 'SHA256' })
    if (
        $sha256Records.Count -ne 1 -or
        ([string]$sha256Records[0].checksumValue).ToLowerInvariant() -ne $actualByRelative[$relative]
    ) {
        throw "Portable SPDX SBOM checksum differs from extracted bytes: $fileName"
    }
}

$verificationCode = Get-StringDigest -Value (($verificationHashes | Sort-Object) -join '') -Algorithm 'SHA1'
if (
    ([string]$package.packageVerificationCode.packageVerificationCodeValue).ToLowerInvariant() -ne
    $verificationCode
) {
    throw 'Portable SPDX SBOM package verification code is invalid.'
}
$inventoryDigest = Get-StringDigest -Value ($inventoryLines -join "`n") -Algorithm 'SHA256'
$expectedNamespace = (
    'https://github.com/hoylon/peerbridge-mcp/spdx/' +
    $package.versionInfo + '/windows-x64/' + $inventoryDigest
)
if ($sbom.documentNamespace -ne $expectedNamespace) {
    throw 'Portable SPDX SBOM document namespace does not bind the file inventory.'
}
$containsIds = @(
    $sbom.relationships |
        Where-Object {
            $_.spdxElementId -eq 'SPDXRef-Package-PeerBridge-MCP' -and
            $_.relationshipType -eq 'CONTAINS'
        } |
        ForEach-Object { [string]$_.relatedSpdxElement }
)
if (
    $containsIds.Count -ne $seenIds.Count -or
    @($containsIds | Where-Object { -not $seenIds.Contains($_) }).Count -ne 0
) {
    throw 'Portable SPDX SBOM package relationships do not match its files.'
}
$describes = @(
    $sbom.relationships |
        Where-Object {
            $_.spdxElementId -eq 'SPDXRef-DOCUMENT' -and
            $_.relationshipType -eq 'DESCRIBES' -and
            $_.relatedSpdxElement -eq 'SPDXRef-Package-PeerBridge-MCP'
        }
)
if ($describes.Count -ne 1) {
    throw 'Portable SPDX SBOM does not describe the PeerBridge package exactly once.'
}

function Get-PeMachine {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    $reader = [System.IO.BinaryReader]::new($stream)
    try {
        if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) {
            throw 'Portable executable does not have a valid DOS header.'
        }
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        if ($peOffset -lt 64 -or $peOffset -gt ($stream.Length - 6)) {
            throw 'Portable executable has an invalid PE header offset.'
        }
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) {
            throw 'Portable executable does not have a valid PE signature.'
        }
        return $reader.ReadUInt16()
    } finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

$peMachine = Get-PeMachine -Path $executable
if ($peMachine -ne 0x8664) {
    throw ('Portable executable must be PE AMD64 (0x8664), found 0x{0:X4}.' -f $peMachine)
}
$productVersion = [string]$package.versionInfo
$versionMatch = [regex]::Match($productVersion, '^(\d+)\.(\d+)\.(\d+)(?:(?:a|b|rc)(\d+))?$')
if (-not $versionMatch.Success) {
    throw "Portable package version cannot be mapped to Windows metadata: $productVersion"
}
$revision = if ($versionMatch.Groups[4].Success) { [int]$versionMatch.Groups[4].Value } else { 0 }
$expectedFileVersion = @(
    [int]$versionMatch.Groups[1].Value
    [int]$versionMatch.Groups[2].Value
    [int]$versionMatch.Groups[3].Value
    $revision
) -join '.'
$versionInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($executable)
$expectedVersionFields = [ordered]@{
    ProductName = 'PeerBridge MCP Control Room'
    CompanyName = 'Hoylon'
    FileVersion = $expectedFileVersion
    ProductVersion = $productVersion
    OriginalFilename = 'PeerBridgeControlRoom.exe'
}
foreach ($field in $expectedVersionFields.Keys) {
    if ([string]$versionInfo.$field -cne [string]$expectedVersionFields[$field]) {
        throw "Portable executable version field $field is invalid: $($versionInfo.$field)"
    }
}

function Stop-OwnedProcess {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            $Process.Kill()
            $Process.WaitForExit(5000) | Out-Null
        }
    } catch {
        # The exact process may already have exited. Never select another PID.
    }
}

function Get-AttestedExecutableProcess {
    param(
        [Parameter(Mandatory = $true)][int]$Id,
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)][datetime]$NotBeforeUtc,
        [Parameter(Mandatory = $true)][string]$Role
    )

    $process = Get-Process -Id $Id -ErrorAction Stop
    $actualPath = [System.IO.Path]::GetFullPath([string]$process.Path)
    if (-not [string]::Equals($actualPath, $ExpectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Role PID $Id is not the packaged executable."
    }
    if ($process.StartTime.ToUniversalTime() -lt $NotBeforeUtc) {
        throw "$Role PID $Id predates this isolated startup contract."
    }
    return $process
}

function New-CreateOnlyFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    $stream.Dispose()
}

function Invoke-StartupLifecycle {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [switch]$ViaCmd
    )

    $caseRoot = Join-Path $OutputRoot ("lifecycle-{0}" -f $Name)
    $localAppData = Join-Path $caseRoot 'localappdata'
    $contractPath = Join-Path $caseRoot 'contract'
    [System.IO.Directory]::CreateDirectory($localAppData) | Out-Null
    [System.IO.Directory]::CreateDirectory($contractPath) | Out-Null
    $readyPath = Join-Path $contractPath 'launcher-ready.json'
    $shutdownPath = Join-Path $contractPath 'shutdown.request'
    $expectedExecutable = [System.IO.Path]::GetFullPath($executable)
    $expectedHash = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    $notBeforeUtc = [DateTime]::UtcNow.AddSeconds(-2)
    $previousLocalAppData = [Environment]::GetEnvironmentVariable('LOCALAPPDATA', 'Process')
    $previousContract = [Environment]::GetEnvironmentVariable('PEERBRIDGE_STARTUP_CONTRACT_PATH', 'Process')
    $previousHeadless = [Environment]::GetEnvironmentVariable('PEERBRIDGE_LAUNCHER_HEADLESS', 'Process')
    $wrapper = $null
    $launcherProcess = $null
    $supervisorProcess = $null
    $readyReceived = $false
    $success = $false

    try {
        [Environment]::SetEnvironmentVariable('LOCALAPPDATA', $localAppData, 'Process')
        [Environment]::SetEnvironmentVariable('PEERBRIDGE_STARTUP_CONTRACT_PATH', $contractPath, 'Process')
        [Environment]::SetEnvironmentVariable('PEERBRIDGE_LAUNCHER_HEADLESS', '1', 'Process')
        if ($ViaCmd) {
            $cmdLauncher = Join-Path $packageRoot.FullName 'Launch PeerBridge.cmd'
            if (-not (Test-Path -LiteralPath $cmdLauncher -PathType Leaf)) {
                throw 'Portable CMD launcher is missing.'
            }
            $cmdArguments = '/d /s /c ""{0}""' -f $cmdLauncher
            $wrapper = Start-Process -FilePath $env:ComSpec -ArgumentList $cmdArguments -WorkingDirectory $packageRoot.FullName -WindowStyle Hidden -PassThru
        } else {
            $wrapper = Start-Process -FilePath $executable -WorkingDirectory $packageRoot.FullName -WindowStyle Hidden -PassThru
        }

        $payload = $null
        $lastReadError = $null
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        while ([DateTime]::UtcNow -lt $deadline) {
            $wrapper.Refresh()
            if ($wrapper.HasExited) {
                throw "Startup lifecycle $Name exited before health handshake (code $($wrapper.ExitCode))."
            }
            if (Test-Path -LiteralPath $readyPath -PathType Leaf) {
                try {
                    $payload = Get-Content -LiteralPath $readyPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    break
                } catch {
                    $lastReadError = $_.Exception.Message
                }
            }
            Start-Sleep -Milliseconds 50
        }
        if ($null -eq $payload) {
            $detail = if ($lastReadError) { " Last read error: $lastReadError" } else { '' }
            throw "Startup lifecycle $Name health handshake timed out.$detail"
        }
        $readyReceived = $true
        if (
            $payload.schema -ne 'peerbridge-launch-health-v1' -or
            $payload.status -ne 'ready' -or
            $payload.health -ne 'database-and-supervisor-lock-ready' -or
            $payload.runtime_kind -ne 'frozen' -or
            [string]$payload.version -cne $productVersion -or
            [string]$payload.runtime_sha256 -cne $expectedHash -or
            -not [string]::Equals([string]$payload.runtime_path, $expectedExecutable, [StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "Startup lifecycle $Name returned an invalid runtime health handshake."
        }

        $launcherProcess = Get-AttestedExecutableProcess -Id ([int]$payload.launcher_pid) -ExpectedPath $expectedExecutable -NotBeforeUtc $notBeforeUtc -Role 'launcher'
        $supervisorProcess = Get-AttestedExecutableProcess -Id ([int]$payload.supervisor_pid) -ExpectedPath $expectedExecutable -NotBeforeUtc $notBeforeUtc -Role 'supervisor'
        if (-not $ViaCmd -and $launcherProcess.Id -ne $wrapper.Id) {
            throw 'Direct zero-argument launcher PID does not match the started process.'
        }
        if ($ViaCmd -and $launcherProcess.Id -eq $wrapper.Id) {
            throw 'CMD lifecycle did not start the packaged executable.'
        }
        $workspaceDatabase = Join-Path $localAppData 'PeerBridge\workspace\.peerbridge\peerbridge.sqlite3'
        if (-not (Test-Path -LiteralPath $workspaceDatabase -PathType Leaf)) {
            throw "Startup lifecycle $Name did not initialize its isolated LOCALAPPDATA workspace."
        }

        New-CreateOnlyFile -Path $shutdownPath
        if (-not $wrapper.WaitForExit(15000)) {
            throw "Startup lifecycle $Name did not shut down within 15 seconds."
        }
        if ($wrapper.ExitCode -ne 0) {
            throw "Startup lifecycle $Name returned exit code $($wrapper.ExitCode)."
        }
        if (-not $launcherProcess.WaitForExit(5000)) {
            throw "Startup lifecycle $Name left its launcher process running."
        }
        if (-not $supervisorProcess.WaitForExit(5000)) {
            throw "Startup lifecycle $Name left its supervisor process running."
        }
        $success = $true
        return [pscustomobject]@{
            Name = $Name
            ExitCode = $wrapper.ExitCode
            LauncherPid = $launcherProcess.Id
            SupervisorPid = $supervisorProcess.Id
            RuntimeSha256 = $expectedHash
            LocalAppData = $localAppData
        }
    } finally {
        if (-not $success) {
            if ($readyReceived -and -not (Test-Path -LiteralPath $shutdownPath)) {
                try { New-CreateOnlyFile -Path $shutdownPath } catch { }
            }
            if ($null -ne $wrapper) {
                try { $wrapper.WaitForExit(5000) | Out-Null } catch { }
            }
            Stop-OwnedProcess -Process $supervisorProcess
            Stop-OwnedProcess -Process $launcherProcess
            Stop-OwnedProcess -Process $wrapper
        }
        [Environment]::SetEnvironmentVariable('LOCALAPPDATA', $previousLocalAppData, 'Process')
        [Environment]::SetEnvironmentVariable('PEERBRIDGE_STARTUP_CONTRACT_PATH', $previousContract, 'Process')
        [Environment]::SetEnvironmentVariable('PEERBRIDGE_LAUNCHER_HEADLESS', $previousHeadless, 'Process')
    }
}

function Invoke-PortableCheck {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [switch]$RequireReceipt,
        [string]$ExpectedReceiptTest = 'feedback-encryption'
    )
    $receiptPath = Join-Path $OutputRoot "self-tests\$Name.json"
    $previousReceiptPath = [Environment]::GetEnvironmentVariable(
        'PEERBRIDGE_SELF_TEST_RECEIPT_PATH',
        'Process'
    )
    try {
        if ($RequireReceipt) {
            [Environment]::SetEnvironmentVariable(
                'PEERBRIDGE_SELF_TEST_RECEIPT_PATH',
                $receiptPath,
                'Process'
            )
        }
        $process = Start-Process -FilePath $executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden
        if (-not $process.WaitForExit(60000)) {
            Stop-OwnedProcess -Process $process
            throw "Portable check $Name timed out after 60 seconds."
        }
    } finally {
        [Environment]::SetEnvironmentVariable(
            'PEERBRIDGE_SELF_TEST_RECEIPT_PATH',
            $previousReceiptPath,
            'Process'
        )
    }
    $receipt = $null
    if ($RequireReceipt) {
        if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
            throw "Portable check $Name did not create its required receipt."
        }
        try {
            $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            throw "Portable check $Name created an invalid receipt."
        }
        $expectedExecutableSha256 = (
            Get-FileHash -LiteralPath $executable -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if (
            $receipt.schema -ne 'peerbridge-packaged-self-test-v1' -or
            $receipt.test -ne $ExpectedReceiptTest -or
            $receipt.runtime_kind -ne 'frozen' -or
            [string]$receipt.version -cne $productVersion -or
            [string]$receipt.runtime_sha256 -cne $expectedExecutableSha256
        ) {
            throw "Portable check $Name receipt identity is invalid."
        }
    }
    if ($process.ExitCode -ne 0) {
        $detail = if ($null -ne $receipt) {
            ' ' + (($receipt.error_chain | ConvertTo-Json -Compress -Depth 4))
        } else { '' }
        throw "Portable check $Name failed with exit code $($process.ExitCode).$detail"
    }
    if ($RequireReceipt -and $receipt.status -ne 'PASS') {
        throw "Portable check $Name did not return a PASS receipt."
    }
    return [pscustomobject]@{
        Name = $Name
        ExitCode = $process.ExitCode
        ReceiptSha256 = $(
            if ($RequireReceipt) {
                (Get-FileHash -LiteralPath $receiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
            } else { $null }
        )
    }
}

$checks = @()
$checks += Invoke-PortableCheck `
    -Name 'feedback-encryption-self-test' `
    -Arguments @('--feedback-encryption-self-test') `
    -RequireReceipt
if (-not $SkipLiveAnnouncement) {
    $checks += Invoke-PortableCheck `
        -Name 'announcement-self-test' `
        -Arguments @('--announcement-self-test') `
        -RequireReceipt `
        -ExpectedReceiptTest 'announcement-feed'
}
if (-not $Headless) {
    $checks += Invoke-PortableCheck -Name 'ui-self-test' -Arguments @('--ui-self-test')
}
$checks += Invoke-PortableCheck -Name 'mcp-send-self-test' -Arguments @('--send-self-test')
$runtimeRoot = Join-Path $OutputRoot 'runtime-smoke'
$quotedRuntime = '"' + $runtimeRoot + '"'
$checks += Invoke-PortableCheck -Name 'create-only-init' -Arguments @(
    '-m', 'peerbridge_mcp', 'init', '--project-root', $quotedRuntime, '--scope', 'portable-e2e'
)
$database = Join-Path $runtimeRoot '.peerbridge\peerbridge.sqlite3'
if (-not (Test-Path -LiteralPath $database -PathType Leaf)) {
    throw 'Portable create-only init did not create the expected SQLite database.'
}
$quotedDatabase = '"' + $database + '"'
$checks += Invoke-PortableCheck -Name 'audit-doctor' -Arguments @(
    '-m', 'peerbridge_mcp', 'doctor', '--project-root', $quotedRuntime,
    '--db', $quotedDatabase, '--scope', 'portable-e2e'
)
$checks += Invoke-StartupLifecycle -Name 'zero-argument'
$checks += Invoke-StartupLifecycle -Name 'cmd-zero-argument' -ViaCmd

[pscustomobject]@{
    Status = 'PASS'
    Archive = $Archive
    ArchiveBytes = $archiveItem.Length
    ArchiveSha256 = $archiveSha256
    RuntimeLicenseManifestSha256 = (
        Get-FileHash -LiteralPath $runtimeLicenseManifestPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    ExtractedRoot = $packageRoot.FullName
    SbomFiles = $records.Count
    PeMachine = ('0x{0:X4}' -f $peMachine)
    ProductVersion = $versionInfo.ProductVersion
    FileVersion = $versionInfo.FileVersion
    DatabaseBytes = (Get-Item -LiteralPath $database).Length
    Checks = $checks
} | ConvertTo-Json -Depth 4
