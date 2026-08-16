[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Bundle,
    [string]$PrivateStore = (Join-Path $env:LOCALAPPDATA "PeerBridge\maintainer"),
    [string]$OutputDirectory = (Join-Path $env:LOCALAPPDATA "PeerBridge\maintainer\decrypted-feedback"),
    [switch]$RevealCredential
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$MaxBundleBytes = 24MB
$MaxArchiveEntries = 7
$MaxReportBytes = 256KB
$MaxEnvelopeBytes = 128KB
$MaxAttachmentBytes = 8MB
$MaxTotalExpandedBytes = 20MB
$MaxCompressionRatio = 200

function Read-BoundedUtf8Json {
    param(
        [Parameter(Mandatory = $true)]
        [IO.Compression.ZipArchiveEntry]$Entry,
        [Parameter(Mandatory = $true)]
        [long]$MaximumBytes,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )
    if ($Entry.Length -le 0 -or $Entry.Length -gt $MaximumBytes) {
        throw "$Label size is invalid."
    }
    $stream = $Entry.Open()
    $reader = [IO.StreamReader]::new(
        $stream,
        [Text.UTF8Encoding]::new($false, $true),
        $false,
        4096,
        $false
    )
    try {
        $text = $reader.ReadToEnd()
        return $text | ConvertFrom-Json
    }
    catch {
        throw "$Label is not canonical UTF-8 JSON."
    }
    finally {
        $reader.Dispose()
    }
}

if (-not $IsWindows) {
    throw "PeerBridge DPAPI feedback decryption requires Windows."
}
if (-not $env:LOCALAPPDATA -and (
    -not $PSBoundParameters.ContainsKey("PrivateStore") -or
    -not $PSBoundParameters.ContainsKey("OutputDirectory")
)) {
    throw "LOCALAPPDATA is unavailable; pass explicit private-store and output paths."
}

$bundlePath = (Resolve-Path -LiteralPath $Bundle).Path
$bundleInfo = Get-Item -LiteralPath $bundlePath
if ($bundleInfo.PSIsContainer) {
    throw "The feedback bundle must be a regular ZIP file."
}
if (
    $bundleInfo.Length -le 0 -or $bundleInfo.Length -gt $MaxBundleBytes
) {
    throw "The feedback bundle size is invalid."
}
$privatePath = Join-Path ([IO.Path]::GetFullPath($PrivateStore)) "peerbridge-support-private.pkcs8.dpapi"

Add-Type -AssemblyName System.IO.Compression
$archive = [IO.Compression.ZipFile]::OpenRead($bundlePath)
$privateBytes = $null
$dataKey = $null
$plaintext = $null
try {
    if ($archive.Entries.Count -lt 2 -or $archive.Entries.Count -gt $MaxArchiveEntries) {
        throw "The feedback bundle member count is invalid."
    }
    $seen = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    [long]$expandedBytes = 0
    foreach ($entry in $archive.Entries) {
        $name = [string]$entry.FullName
        $allowed = $name -eq "report.json" -or
            $name -eq "encrypted-credential.json" -or
            $name -match '^attachments/[0-9]{2}\.(png|jpg|jpeg|gif|webp|json|log|txt)$'
        if (-not $allowed -or $name.Contains("\") -or $name.Contains(":") -or
            $name.Contains("..") -or -not $seen.Add($name)) {
            throw "The feedback bundle contains an invalid or duplicate member."
        }
        $maximum = if ($name -eq "report.json") {
            $MaxReportBytes
        } elseif ($name -eq "encrypted-credential.json") {
            $MaxEnvelopeBytes
        } else {
            $MaxAttachmentBytes
        }
        if ($entry.Length -lt 0 -or $entry.Length -gt $maximum) {
            throw "The feedback bundle contains an oversized member."
        }
        $expandedBytes += $entry.Length
        if ($expandedBytes -gt $MaxTotalExpandedBytes) {
            throw "The feedback bundle expanded size is invalid."
        }
        if ($entry.Length -gt 0 -and (
            $entry.CompressedLength -le 0 -or
            ($entry.Length / [double]$entry.CompressedLength) -gt $MaxCompressionRatio
        )) {
            throw "The feedback bundle compression ratio is unsafe."
        }
    }

    $reportEntry = $archive.GetEntry("report.json")
    $secretEntry = $archive.GetEntry("encrypted-credential.json")
    if (-not $reportEntry -or -not $secretEntry) {
        throw "The feedback bundle does not contain an encrypted credential."
    }

    $report = Read-BoundedUtf8Json $reportEntry $MaxReportBytes "feedback report"
    $envelope = Read-BoundedUtf8Json $secretEntry $MaxEnvelopeBytes "encrypted credential"

    if ($report.schema -ne "peerbridge.feedback-report.v1") {
        throw "The feedback report schema is unsupported."
    }
    if ($envelope.schema -ne "peerbridge.feedback-secret-envelope.v1") {
        throw "The encrypted credential schema is unsupported."
    }
    if ([string]$report.case_id -notmatch '^[0-9a-f]{32}$') {
        throw "The feedback case ID is invalid."
    }
    if ($report.case_id -ne $envelope.case_id) {
        throw "The feedback report and encrypted credential case IDs do not match."
    }
    if ($envelope.algorithm -ne "RSA-OAEP-SHA256+A256GCM") {
        throw "The encrypted credential algorithm is unsupported."
    }
    if (-not (Test-Path -LiteralPath $privatePath -PathType Leaf)) {
        throw "The protected maintainer private key is unavailable."
    }

    $protectedBytes = [IO.File]::ReadAllBytes($privatePath)
    $entropy = [Text.Encoding]::UTF8.GetBytes("PeerBridgeFeedbackIdentityV1")
    $privateBytes = [Security.Cryptography.ProtectedData]::Unprotect(
        $protectedBytes,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $rsa = [Security.Cryptography.RSA]::Create()
    try {
        $bytesRead = 0
        $rsa.ImportPkcs8PrivateKey($privateBytes, [ref]$bytesRead)
        if ($bytesRead -ne $privateBytes.Length) {
            throw "The protected maintainer key contains trailing data."
        }
        $publicDer = $rsa.ExportSubjectPublicKeyInfo()
        $observed = [Convert]::ToHexString(
            [Security.Cryptography.SHA256]::HashData($publicDer)
        ).ToLowerInvariant()
        if ($observed -ne [string]$envelope.public_key_sha256) {
            throw "The encrypted credential was not sealed to this maintainer identity."
        }
        $wrapped = [Convert]::FromBase64String([string]$envelope.wrapped_key_b64)
        $dataKey = $rsa.Decrypt($wrapped, [Security.Cryptography.RSAEncryptionPadding]::OaepSHA256)
    }
    finally {
        $rsa.Dispose()
    }

    $associatedData = [Convert]::FromBase64String([string]$envelope.associated_data_b64)
    $expectedAssociatedData = [Text.Encoding]::ASCII.GetBytes(
        "peerbridge.feedback-secret-envelope.v1:" + [string]$report.case_id
    )
    if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals(
        $associatedData,
        $expectedAssociatedData
    )) {
        throw "The encrypted credential associated data is invalid."
    }
    $nonce = [Convert]::FromBase64String([string]$envelope.nonce_b64)
    $ciphertextWithTag = [Convert]::FromBase64String([string]$envelope.ciphertext_b64)
    if ($ciphertextWithTag.Length -le 16) {
        throw "The encrypted credential payload is invalid."
    }
    $ciphertext = [byte[]]::new($ciphertextWithTag.Length - 16)
    $tag = [byte[]]::new(16)
    [Array]::Copy($ciphertextWithTag, 0, $ciphertext, 0, $ciphertext.Length)
    [Array]::Copy($ciphertextWithTag, $ciphertext.Length, $tag, 0, 16)
    $plaintext = [byte[]]::new($ciphertext.Length)
    $aes = [Security.Cryptography.AesGcm]::new($dataKey, 16)
    try { $aes.Decrypt($nonce, $ciphertext, $tag, $plaintext, $associatedData) }
    finally { $aes.Dispose() }

    $result = [ordered]@{
        Schema = "peerbridge.feedback-maintainer-inspection.v1"
        CaseId = [string]$report.case_id
        BundleSha256 = $null
        Summary = [string]$report.summary
        Contact = [string]$report.contact
        EncryptedCredentialVerified = $true
        CredentialCharacterCount = ([Text.Encoding]::UTF8.GetString($plaintext)).Length
        CredentialOutputPath = $null
    }
    $bundleStream = [IO.File]::OpenRead($bundlePath)
    try {
        $result.BundleSha256 = [Convert]::ToHexString(
            [Security.Cryptography.SHA256]::HashData($bundleStream)
        ).ToLowerInvariant()
    }
    finally {
        $bundleStream.Dispose()
    }

    if ($RevealCredential) {
        $outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
        [IO.Directory]::CreateDirectory($outputRoot) | Out-Null
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls.exe $outputRoot /inheritance:r /grant:r "${identity}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not apply the decrypted-output access policy."
        }
        $outputPath = Join-Path $outputRoot ("peerbridge-feedback-" + $report.case_id + "-credential.txt")
        $stream = [IO.File]::Open(
            $outputPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $stream.Write($plaintext, 0, $plaintext.Length)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
        $result.CredentialOutputPath = $outputPath
    }

    [pscustomobject]$result | ConvertTo-Json
}
finally {
    $archive.Dispose()
    if ($privateBytes) {
        [Security.Cryptography.CryptographicOperations]::ZeroMemory($privateBytes)
    }
    if ($dataKey) {
        [Security.Cryptography.CryptographicOperations]::ZeroMemory($dataKey)
    }
    if ($plaintext) {
        [Security.Cryptography.CryptographicOperations]::ZeroMemory($plaintext)
    }
}
