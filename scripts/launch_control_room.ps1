$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$entryScript = Join-Path $PSScriptRoot 'peerbridge_control_room_entry.py'
$database = Join-Path $projectRoot '.peerbridge\peerbridge.sqlite3'
$scope = 'peerbridge-main'
$startupTimeoutSeconds = 15

function Show-LaunchError {
    param([Parameter(Mandatory = $true)][string]$Message)

    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        $Message,
        'PeerBridge MCP Control Room',
        'OK',
        'Error'
    ) | Out-Null
}

if (-not (Test-Path -LiteralPath $pythonw)) {
    Show-LaunchError "PeerBridge Python runtime not found:`n$pythonw"
    exit 1
}

if (-not (Test-Path -LiteralPath $entryScript -PathType Leaf)) {
    Show-LaunchError "PeerBridge source launcher not found:`n$entryScript"
    exit 1
}

$readyDirectory = Join-Path $projectRoot '.peerbridge\launcher-ready'
[System.IO.Directory]::CreateDirectory($readyDirectory) | Out-Null
$readyPath = Join-Path $readyDirectory ("source-launch-{0}.json" -f [Guid]::NewGuid().ToString('N'))
$arguments = '--source-launch --project-root "{0}" --db "{1}" --scope "{2}" --launcher-ready-path "{3}"' -f $projectRoot, $database, $scope, $readyPath
$launcher = $null
$supervisorPid = $null

try {
    $launcher = Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Normal -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds($startupTimeoutSeconds)
    $payload = $null
    $lastReadError = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        $launcher.Refresh()
        if ($launcher.HasExited) {
            throw "Source launcher exited before health handshake (code $($launcher.ExitCode))."
        }
        if (Test-Path -LiteralPath $readyPath -PathType Leaf) {
            try {
                $payload = Get-Content -LiteralPath $readyPath -Raw | ConvertFrom-Json
                break
            } catch {
                $lastReadError = $_.Exception.Message
            }
        }
        Start-Sleep -Milliseconds 50
    }
    if ($null -eq $payload) {
        $detail = if ($lastReadError) { " Last read error: $lastReadError" } else { '' }
        throw "Source launcher health handshake timed out.$detail"
    }

    $expectedRuntime = [System.IO.Path]::GetFullPath($pythonw)
    $expectedHash = (Get-FileHash -LiteralPath $pythonw -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($payload.schema -ne 'peerbridge-launch-health-v1' -or $payload.status -ne 'ready') {
        throw 'Source launcher returned an invalid health handshake.'
    }
    if ([int]$payload.launcher_pid -ne $launcher.Id) {
        throw 'Source launcher PID does not match the process that was started.'
    }
    if ($payload.runtime_kind -ne 'source') {
        throw 'Source launcher attempted to use a frozen runtime.'
    }
    if (-not [string]::Equals([string]$payload.runtime_path, $expectedRuntime, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Source launcher runtime does not match the selected virtual environment.'
    }
    if ([string]$payload.runtime_sha256 -ne $expectedHash) {
        throw 'Source launcher runtime hash does not match the selected virtual environment.'
    }
    if ([string]::IsNullOrWhiteSpace([string]$payload.version)) {
        throw 'Source launcher did not report its package version.'
    }

    $supervisorPid = [int]$payload.supervisor_pid
    $supervisor = Get-Process -Id $supervisorPid -ErrorAction Stop
    if (-not [string]::Equals([string]$supervisor.Path, $expectedRuntime, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Source supervisor runtime does not match the source UI runtime.'
    }
    exit 0
} catch {
    $launchError = $_.Exception.Message
    if ($null -ne $launcher) {
        $launcher.Refresh()
        if (-not $launcher.HasExited) {
            Stop-Process -Id $launcher.Id -Force -ErrorAction SilentlyContinue
            $launcher.WaitForExit(5000) | Out-Null
        }
    }
    if ($null -ne $supervisorPid) {
        try {
            Wait-Process -Id $supervisorPid -Timeout 6 -ErrorAction Stop
        } catch {
            # The managed child owns parent-liveness shutdown; never kill an
            # unrelated process after a possible PID reuse.
        }
    }
    Show-LaunchError $launchError
    exit 1
}
