param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "PortServer",
        "PortClient",
        "ReceiveFiles",
        "ServeFiles",
        "SshServer",
        "SshClient",
        "CopyToServer",
        "CopyFromServer",
        "SocksCommand",
        "ExitNode"
    )]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$TailcatPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedSha256,

    [ValidateRange(1, 65535)]
    [int]$Port = 8765,

    [string[]]$AllowClientKey = @(),
    [string]$TokenFile,
    [string]$Directory,
    [string]$Source,
    [string]$Destination,
    [string]$CommandPath,
    [string[]]$CommandArgument = @(),
    [switch]$EnableWrite,
    [switch]$EnableExitNode
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-TrustedTailcat {
    param([string]$Path, [string]$Sha256)

    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -and $item.Extension -ieq ".exe") {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Tailcat executable must not be a reparse point."
        }
    } else {
        throw "TailcatPath must name an existing Windows executable."
    }
    $resolved = $item.FullName
    $observed = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash
    if (-not [string]::Equals($observed, $Sha256, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Tailcat executable SHA-256 does not match ExpectedSha256."
    }
    return $resolved
}

function Get-AllowedClientArguments {
    param([string[]]$Keys, [switch]$Required)

    if ($Required -and $Keys.Count -eq 0) {
        throw "This server mode requires at least one AllowClientKey."
    }
    foreach ($key in $Keys) {
        if ($key -notmatch "^nodekey:[0-9a-f]{64}$") {
            throw "AllowClientKey must be a Tailcat nodekey public key."
        }
    }
    if ($Keys.Count -eq 0) {
        return @()
    }
    return @("--allow=" + ($Keys -join ","))
}

function Read-TailcatToken {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "This client mode requires TokenFile."
    }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "TokenFile must be a regular non-reparse file."
    }
    $connectionAddress = (Get-Content -LiteralPath $item.FullName -Raw).Trim()
    if ($connectionAddress -notmatch "^tc[A-Za-z0-9_-]{40,4096}$") {
        throw "TokenFile does not contain one valid Tailcat connection token."
    }
    return $connectionAddress
}

function Resolve-RegularDirectory {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "This mode requires Directory."
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Directory must be an existing non-reparse directory."
    }
    return $item.FullName
}

$tailcat = Resolve-TrustedTailcat -Path $TailcatPath -Sha256 $ExpectedSha256
$arguments = @()

switch ($Mode) {
    "PortServer" {
        $arguments = @("serve", "--key=new")
        $arguments += Get-AllowedClientArguments -Keys $AllowClientKey -Required
        $arguments += [string]$Port
    }
    "PortClient" {
        $arguments = @(Read-TailcatToken -Path $TokenFile, [string]$Port)
    }
    "ReceiveFiles" {
        $drop = Resolve-RegularDirectory -Path $Directory
        $arguments = @("recv", "--key=new")
        $arguments += Get-AllowedClientArguments -Keys $AllowClientKey -Required
        $arguments += $drop
    }
    "ServeFiles" {
        $served = Resolve-RegularDirectory -Path $Directory
        $access = if ($EnableWrite) { "rw" } else { "ro" }
        $arguments = @("serve", "--key=new")
        $arguments += Get-AllowedClientArguments -Keys $AllowClientKey -Required
        $arguments += "--files=$served`:$access"
        $arguments += "files"
    }
    "SshServer" {
        $arguments = @("serve", "--key=new")
        $arguments += Get-AllowedClientArguments -Keys $AllowClientKey -Required
        $arguments += [string]$Port
    }
    "SshClient" {
        $arguments = @("ssh", (Read-TailcatToken -Path $TokenFile))
        $arguments += $CommandArgument
    }
    "CopyToServer" {
        if ([string]::IsNullOrWhiteSpace($Source)) { throw "CopyToServer requires Source." }
        $sourceItem = Get-Item -LiteralPath $Source -Force
        if (($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Source must not be a reparse point."
        }
        $connectionAddress = Read-TailcatToken -Path $TokenFile
        $arguments = @("cp", $sourceItem.FullName, "$connectionAddress`:")
    }
    "CopyFromServer" {
        if ([string]::IsNullOrWhiteSpace($Source) -or [string]::IsNullOrWhiteSpace($Destination)) {
            throw "CopyFromServer requires Source and Destination."
        }
        $connectionAddress = Read-TailcatToken -Path $TokenFile
        $destinationDirectory = Resolve-RegularDirectory -Path $Destination
        $arguments = @("cp", "$connectionAddress`:$Source", $destinationDirectory)
    }
    "SocksCommand" {
        if ([string]::IsNullOrWhiteSpace($CommandPath)) {
            throw "SocksCommand requires CommandPath."
        }
        $command = (Get-Command -Name $CommandPath -CommandType Application).Source
        $arguments = @("socks", (Read-TailcatToken -Path $TokenFile), $command)
        $arguments += $CommandArgument
    }
    "ExitNode" {
        if (-not $EnableExitNode) {
            throw "ExitNode requires the explicit EnableExitNode switch."
        }
        $arguments = @("serve", "--key=new")
        $arguments += Get-AllowedClientArguments -Keys $AllowClientKey -Required
        $arguments += "exit-node"
    }
}

if ($arguments -contains "no-auth-ssh") {
    throw "PeerBridge never launches Tailcat no-auth-ssh."
}

# Foreground execution is intentional: closing this process closes the ephemeral
# Tailcat key and prevents a forgotten remote listener from surviving silently.
& $tailcat @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Tailcat exited with code $LASTEXITCODE."
}
