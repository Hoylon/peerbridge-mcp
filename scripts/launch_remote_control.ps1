param(
    [int]$Port = 8765,
    [string]$Scope = "peerbridge-main",
    [string]$EvidenceRunId = "",
    [switch]$TestMode,
    [string]$TestRoot = "",
    [string]$TestBackendExecutable = "",
    [string]$TestBackendScript = "",
    [string]$TestTailscaleExecutable = "",
    [string]$TestTailscaleScript = "",
    [int]$TestHealthAttempts = 0,
    [int]$TestHealthDelayMilliseconds = 0,
    [int]$TestHealthTimeoutSeconds = 0,
    [int]$TestExternalTimeoutSeconds = 0,
    [int]$TestFailStopProcessId = 0
)

$ErrorActionPreference = "Stop"

function Resolve-TrustedTailscaleExecutable {
    $SecurityModule = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
    if (-not (Test-Path -LiteralPath $SecurityModule -PathType Leaf)) {
        throw 'The Windows PowerShell signature-verification module is unavailable.'
    }
    Import-Module -Name $SecurityModule -ErrorAction Stop
    $Candidates = @(
        (Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe')
    )
    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
        $Candidates += Join-Path ${env:ProgramFiles(x86)} 'Tailscale\tailscale.exe'
    }
    foreach ($Candidate in $Candidates) {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { continue }
        $Item = Get-Item -LiteralPath $Candidate -Force
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $Signature = Get-AuthenticodeSignature -LiteralPath $Candidate
        if ($Signature.Status -ne 'Valid' -or
            $null -eq $Signature.SignerCertificate -or
            $Signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Tailscale Inc\.(,|$)') {
            continue
        }
        return $Item.FullName
    }
    throw 'A publisher-verified Tailscale CLI was not found in Program Files.'
}
if ($Port -lt 1 -or $Port -gt 65535) {
    throw "PeerBridge remote port must be between 1 and 65535."
}
if ($EvidenceRunId.Length -gt 0 -and $EvidenceRunId -notmatch '^[A-Za-z0-9_.-]{1,120}$') {
    throw "EvidenceRunId must contain only letters, digits, dot, underscore, or hyphen (maximum 120 characters)."
}
$RequestedEvidenceRunId = if ($EvidenceRunId.Length -gt 0) { $EvidenceRunId } else { $null }
$ProductionRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TestOverridesPresent = -not [string]::IsNullOrWhiteSpace($TestRoot) -or
    -not [string]::IsNullOrWhiteSpace($TestBackendExecutable) -or
    -not [string]::IsNullOrWhiteSpace($TestBackendScript) -or
    -not [string]::IsNullOrWhiteSpace($TestTailscaleExecutable) -or
    -not [string]::IsNullOrWhiteSpace($TestTailscaleScript) -or
    $TestHealthAttempts -ne 0 -or $TestHealthDelayMilliseconds -ne 0 -or
    $TestHealthTimeoutSeconds -ne 0 -or $TestExternalTimeoutSeconds -ne 0 -or
    $TestFailStopProcessId -ne 0
if (-not $TestMode -and $TestOverridesPresent) {
    throw "Test-only launcher overrides require -TestMode."
}

$BackendPrefixArguments = @("-m", "peerbridge_mcp", "remote")
$TailscalePrefixArguments = @()
$HealthAttempts = 80
$HealthDelayMilliseconds = 250
$HealthTimeoutSeconds = 2
$ExternalTimeoutSeconds = 20
$Root = $ProductionRoot
$BackendExecutable = Join-Path $Root ".venv\Scripts\python.exe"
$TailscaleExecutable = $null

if ($TestMode) {
    if ($env:PEERBRIDGE_REMOTE_LAUNCHER_TESTING -ne "isolated-fixture-v1") {
        throw "Test mode requires the isolated launcher-test environment sentinel."
    }
    foreach ($Required in @(
        $TestRoot,
        $TestBackendExecutable,
        $TestBackendScript,
        $TestTailscaleExecutable,
        $TestTailscaleScript
    )) {
        if ([string]::IsNullOrWhiteSpace($Required)) {
            throw "Test mode requires explicit isolated executable and script paths."
        }
    }
    $Root = (Resolve-Path -LiteralPath $TestRoot).Path
    if ($Root -eq $ProductionRoot -or
        -not (Test-Path -LiteralPath (Join-Path $Root ".peerbridge-launcher-test-root") -PathType Leaf)) {
        throw "Test mode requires a marker-bearing root outside the production project."
    }
    $RootPrefix = $Root.TrimEnd("\") + "\"
    $TestBackendScript = (Resolve-Path -LiteralPath $TestBackendScript).Path
    $TestTailscaleScript = (Resolve-Path -LiteralPath $TestTailscaleScript).Path
    foreach ($FixtureScript in @($TestBackendScript, $TestTailscaleScript)) {
        if (-not $FixtureScript.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Test fixture scripts must remain inside the isolated test root."
        }
    }
    $BackendExecutable = (Resolve-Path -LiteralPath $TestBackendExecutable).Path
    $TailscaleExecutable = (Resolve-Path -LiteralPath $TestTailscaleExecutable).Path
    if ($TestHealthAttempts -lt 1 -or $TestHealthAttempts -gt 100 -or
        $TestHealthDelayMilliseconds -lt 10 -or $TestHealthDelayMilliseconds -gt 1000 -or
        $TestHealthTimeoutSeconds -lt 1 -or $TestHealthTimeoutSeconds -gt 10 -or
        $TestExternalTimeoutSeconds -lt 1 -or $TestExternalTimeoutSeconds -gt 30 -or
        $TestFailStopProcessId -lt 0) {
        throw "Test launcher timing and process overrides are outside their safe bounds."
    }
    $BackendPrefixArguments = @($TestBackendScript)
    $TailscalePrefixArguments = @($TestTailscaleScript)
    $HealthAttempts = $TestHealthAttempts
    $HealthDelayMilliseconds = $TestHealthDelayMilliseconds
    $HealthTimeoutSeconds = $TestHealthTimeoutSeconds
    $ExternalTimeoutSeconds = $TestExternalTimeoutSeconds
} else {
    $TailscaleExecutable = Resolve-TrustedTailscaleExecutable
}

$State = Join-Path $Root ".peerbridge"
$PrimaryPidFile = Join-Path $State "remote-control.pid"
$PidFile = $PrimaryPidFile
$FallbackPidFile = Join-Path $State "remote-control-v2.pid"
# Pre-v2 launchers wrote a bare PID. Preserve that historical file byte-for-byte
# and use a versioned ownership document instead of deleting or overwriting it.
if (Test-Path -LiteralPath $PrimaryPidFile -PathType Leaf) {
    $PrimaryOwnership = $null
    try { $PrimaryOwnership = Get-Content -LiteralPath $PrimaryPidFile -Raw | ConvertFrom-Json } catch {}
    $PrimaryProperties = if ($PrimaryOwnership) {
        @($PrimaryOwnership.PSObject.Properties.Name)
    } else {
        @()
    }
    if (-not ($PrimaryProperties -contains "pid") -or
        -not ($PrimaryProperties -contains "start_time_utc_ticks") -or
        -not ($PrimaryProperties -contains "port") -or
        -not ($PrimaryProperties -contains "scope") -or
        -not ($PrimaryProperties -contains "instance_id")) {
        $PidFile = $FallbackPidFile
    }
}
$LogStem = if ($RequestedEvidenceRunId) {
    "remote-control-" + $RequestedEvidenceRunId
} else {
    "remote-control"
}
$Stdout = Join-Path $State ($LogStem + ".stdout.log")
$Stderr = Join-Path $State ($LogStem + ".stderr.log")
$ServeState = Join-Path $State "remote-control-serve.json"
$AccessUrlFile = Join-Path $State "remote-control-access-url.txt"
$AccessFragmentName = 'access' + '_token'
$MutexName = "Local\PeerBridgeRemote-" + (
    [Convert]::ToBase64String(
        [Security.Cryptography.SHA256]::Create().ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant())
        )
    ).Replace("=", "").Replace("+", "-").Replace("/", "_").Substring(0, 24)
)
$LauncherMutex = New-Object Threading.Mutex($false, $MutexName)
$LauncherMutexOwned = $false
$Process = $null
$LauncherPid = 0
$LauncherStartTicks = 0

if (-not ("PeerBridgeNativeProcess" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class PeerBridgeNativeProcess {
    private const uint TH32CS_SNAPPROCESS = 0x00000002;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct PROCESSENTRY32 {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public UIntPtr th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_BASIC_INFORMATION {
        public IntPtr Reserved1;
        public IntPtr PebBaseAddress;
        public IntPtr Reserved2_0;
        public IntPtr Reserved2_1;
        public IntPtr UniqueProcessId;
        public IntPtr InheritedFromUniqueProcessId;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool Process32FirstW(IntPtr snapshot, ref PROCESSENTRY32 entry);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool Process32NextW(IntPtr snapshot, ref PROCESSENTRY32 entry);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(
        IntPtr processHandle,
        int processInformationClass,
        ref PROCESS_BASIC_INFORMATION processInformation,
        uint processInformationLength,
        out uint returnLength
    );

    public static Dictionary<int, int> ParentMap() {
        IntPtr snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snapshot == new IntPtr(-1)) {
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }
        try {
            var result = new Dictionary<int, int>();
            var entry = new PROCESSENTRY32();
            entry.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32));
            if (!Process32FirstW(snapshot, ref entry)) {
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
            do {
                result[(int)entry.th32ProcessID] = (int)entry.th32ParentProcessID;
                entry.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32));
            } while (Process32NextW(snapshot, ref entry));
            return result;
        } finally {
            CloseHandle(snapshot);
        }
    }

    public static int ParentProcessId(IntPtr processHandle) {
        var information = new PROCESS_BASIC_INFORMATION();
        uint returnLength;
        int status = NtQueryInformationProcess(
            processHandle,
            0,
            ref information,
            (uint)Marshal.SizeOf(typeof(PROCESS_BASIC_INFORMATION)),
            out returnLength
        );
        if (status != 0) {
            throw new System.ComponentModel.Win32Exception(
                "NtQueryInformationProcess failed with NTSTATUS 0x" +
                status.ToString("X8")
            );
        }
        return checked((int)information.InheritedFromUniqueProcessId.ToInt64());
    }
}
'@
}
try {
    if (-not $LauncherMutex.WaitOne(0)) {
        throw "Another PeerBridge remote launcher is already running for this project."
    }
    $LauncherMutexOwned = $true

function Remove-OwnedLock {
    param([int]$ProcessId, [int64]$StartTicks)
    if (-not (Test-Path -LiteralPath $PidFile)) { return }
    try {
        $Current = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
        if ([int]$Current.pid -eq $ProcessId -and [int64]$Current.start_time_utc_ticks -eq $StartTicks) {
            Remove-Item -LiteralPath $PidFile -Force
        }
    } catch {}
}

function ConvertTo-ProcessArgument {
    param([AllowEmptyString()][string]$Value)
    if ($Value.IndexOf([char]0) -ge 0 -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "Process arguments must not contain control characters."
    }
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $Builder = New-Object Text.StringBuilder
    [void]$Builder.Append([char]34)
    $Backslashes = 0
    foreach ($Character in $Value.ToCharArray()) {
        if ($Character -eq [char]92) {
            $Backslashes++
            continue
        }
        if ($Character -eq [char]34) {
            if ($Backslashes -gt 0) {
                [void]$Builder.Append((([string][char]92) * ($Backslashes * 2)) -join "")
            }
            [void]$Builder.Append([char]92)
            [void]$Builder.Append([char]34)
            $Backslashes = 0
            continue
        }
        if ($Backslashes -gt 0) {
            [void]$Builder.Append((([string][char]92) * $Backslashes) -join "")
            $Backslashes = 0
        }
        [void]$Builder.Append($Character)
    }
    if ($Backslashes -gt 0) {
        [void]$Builder.Append((([string][char]92) * ($Backslashes * 2)) -join "")
    }
    [void]$Builder.Append([char]34)
    return $Builder.ToString()
}

function Join-ProcessArguments {
    param([string[]]$Values)
    return (($Values | ForEach-Object { ConvertTo-ProcessArgument -Value $_ }) -join " ")
}

function Test-OwnedProcessAlive {
    param([int]$ProcessId, [int64]$StartTicks)
    $Candidate = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $Candidate) { return $false }
    try {
        if ($Candidate.HasExited) { return $false }
        return [int64]$Candidate.StartTime.ToUniversalTime().Ticks -eq $StartTicks
    } catch {
        return $false
    } finally {
        $Candidate.Dispose()
    }
}

function Open-OwnedProcessHandle {
    param([int]$ProcessId, [int64]$StartTicks)
    $Candidate = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $Candidate) {
        return [pscustomobject]@{ status = "missing"; process = $null }
    }
    try {
        # Force one OS process handle to remain open from identity validation
        # through termination. This prevents a recycled PID from being killed.
        $null = $Candidate.Handle
        if ($Candidate.HasExited) {
            [void]$Candidate.Dispose()
            return [pscustomobject]@{ status = "exited"; process = $null }
        }
        $ActualStartTicks = [int64]$Candidate.StartTime.ToUniversalTime().Ticks
        if ($ActualStartTicks -ne $StartTicks) {
            [void]$Candidate.Dispose()
            return [pscustomobject]@{ status = "mismatch"; process = $null }
        }
        return [pscustomobject]@{ status = "owned"; process = $Candidate }
    } catch {
        [void]$Candidate.Dispose()
        return [pscustomobject]@{ status = "inaccessible"; process = $null }
    }
}

function Open-ProcessIdentityHandle {
    param([int]$ProcessId)
    $Candidate = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $Candidate) {
        return [pscustomobject]@{ status = "missing"; process = $null }
    }
    try {
        $null = $Candidate.Handle
        if ($Candidate.HasExited) {
            [void]$Candidate.Dispose()
            return [pscustomobject]@{ status = "exited"; process = $null }
        }
        return [pscustomobject]@{
            status = "owned"
            pid = $ProcessId
            start_time_utc_ticks = [int64]$Candidate.StartTime.ToUniversalTime().Ticks
            parent_pid = [PeerBridgeNativeProcess]::ParentProcessId($Candidate.Handle)
            process = $Candidate
        }
    } catch {
        [void]$Candidate.Dispose()
        $Current = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        try {
            if (-not $Current) {
                return [pscustomobject]@{ status = "exited"; process = $null }
            }
        } finally {
            if ($Current) { [void]$Current.Dispose() }
        }
        return [pscustomobject]@{ status = "inaccessible"; process = $null }
    }
}

function Get-OwnedDescendantHandles {
    param([int]$ProcessId, [int64]$RootStartTicks)
    $ParentMap = [PeerBridgeNativeProcess]::ParentMap()
    $Depths = @{}
    $Depths[$ProcessId] = 0
    $Pending = New-Object Collections.Generic.Queue[int]
    $Pending.Enqueue($ProcessId)
    while ($Pending.Count -gt 0) {
        $ParentId = $Pending.Dequeue()
        foreach ($ChildId in $ParentMap.Keys) {
            if ([int]$ParentMap[$ChildId] -ne $ParentId -or $Depths.ContainsKey($ChildId)) {
                continue
            }
            $Depths[$ChildId] = [int]$Depths[$ParentId] + 1
            $Pending.Enqueue($ChildId)
        }
    }
    $Opened = @()
    try {
        foreach ($ChildId in $Depths.Keys) {
            if ([int]$ChildId -eq $ProcessId) { continue }
            $Identity = Open-ProcessIdentityHandle -ProcessId ([int]$ChildId)
            if ($Identity.status -eq "owned") {
                $Opened += $Identity
            } elseif ($Identity.status -eq "inaccessible") {
                throw "PeerBridge could not verify a discovered descendant process."
            }
        }

        # The Toolhelp snapshot is only a candidate list. Parent linkage is
        # accepted from each exact, held process handle so PID reuse between
        # snapshot and inspection cannot turn an unrelated process into an
        # owned descendant.
        $AcceptedIdentity = @{}
        $AcceptedIdentity[$ProcessId] = [pscustomobject]@{
            depth = 0
            start_time_utc_ticks = $RootStartTicks
        }
        $Pending = @($Opened)
        $Accepted = @()
        while ($Pending.Count -gt 0) {
            $Next = @()
            $Progress = $false
            foreach ($Identity in $Pending) {
                $ParentId = [int]$Identity.parent_pid
                $ParentIdentity = if ($AcceptedIdentity.ContainsKey($ParentId)) {
                    $AcceptedIdentity[$ParentId]
                } else {
                    $null
                }
                # Windows preserves only the numeric inherited parent PID. An
                # orphan from an older process incarnation can therefore name
                # a PID now reused by PeerBridge. Creation-time ordering binds
                # the child to the held parent incarnation rather than merely
                # to its recycled PID number.
                if ($ParentIdentity -and
                    [int64]$Identity.start_time_utc_ticks -ge
                        [int64]$ParentIdentity.start_time_utc_ticks) {
                    $Depth = [int]$ParentIdentity.depth + 1
                    $AcceptedIdentity[[int]$Identity.pid] = [pscustomobject]@{
                        depth = $Depth
                        start_time_utc_ticks = [int64]$Identity.start_time_utc_ticks
                    }
                    $Accepted += [pscustomobject]@{
                        pid = [int]$Identity.pid
                        start_time_utc_ticks = [int64]$Identity.start_time_utc_ticks
                        depth = $Depth
                        process = $Identity.process
                    }
                    $Progress = $true
                } else {
                    $Next += $Identity
                }
            }
            if (-not $Progress) {
                foreach ($Identity in $Next) {
                    [void]$Identity.process.Dispose()
                }
                break
            }
            $Pending = @($Next)
        }
        return @($Accepted | Sort-Object depth -Descending)
    } catch {
        foreach ($Identity in $Opened) {
            if ($Identity.process) {
                [void]$Identity.process.Dispose()
            }
        }
        throw
    }
}

function Stop-OwnedProcess {
    param(
        [int]$ProcessId,
        [int64]$StartTicks,
        [switch]$IgnoreTestFailure
    )
    $Held = @()
    try {
        $Root = Open-OwnedProcessHandle -ProcessId $ProcessId -StartTicks $StartTicks
        if ($Root.status -in @("missing", "exited", "mismatch")) {
            return $true
        }
        if ($Root.status -ne "owned") {
            return $false
        }
        $Held += [pscustomobject]@{
            pid = $ProcessId
            depth = 0
            process = $Root.process
        }
        if (-not $IgnoreTestFailure -and $TestMode -and $TestFailStopProcessId -eq $ProcessId) {
            return $false
        }
        try {
            $Descendants = @(
                Get-OwnedDescendantHandles `
                    -ProcessId $ProcessId `
                    -RootStartTicks $StartTicks
            )
        } catch {
            return $false
        }
        $Held += $Descendants

        $TerminationFailed = $false
        foreach ($Identity in @($Held | Sort-Object depth -Descending)) {
            try {
                if (-not $Identity.process.HasExited) {
                    $Identity.process.Kill()
                }
            } catch {
                $TerminationFailed = $true
            }
        }

        $Remaining = @()
        foreach ($Identity in $Held) {
            try {
                if (-not $Identity.process.WaitForExit(10000)) {
                    $Remaining += $Identity
                }
            } catch {
                $Remaining += $Identity
            }
        }
        if ($Remaining.Count -gt 0) {
            $RemainingIds = (($Remaining | ForEach-Object { [string]$_.pid }) -join ",")
            [Console]::Error.WriteLine(
                "PeerBridge cleanup could not confirm termination for owned process IDs: $RemainingIds"
            )
            [Console]::Error.Flush()
        }
        return (-not $TerminationFailed -and $Remaining.Count -eq 0)
    } finally {
        foreach ($Identity in $Held) {
            if ($Identity.process) {
                [void]$Identity.process.Dispose()
            }
        }
    }
}

function Stop-OwnedBackendTree {
    param($Ownership)
    if (-not $Ownership) { return $true }
    $Targets = @()
    $HasLauncherIdentity = $Ownership.launcher_pid -and $Ownership.launcher_start_time_utc_ticks
    if ($HasLauncherIdentity) {
        $Targets += ,@([int]$Ownership.launcher_pid, [int64]$Ownership.launcher_start_time_utc_ticks)
    }
    if ($Ownership.pid -and $Ownership.start_time_utc_ticks -and
        (-not $HasLauncherIdentity -or [int]$Ownership.pid -ne [int]$Ownership.launcher_pid)) {
        $Targets += ,@([int]$Ownership.pid, [int64]$Ownership.start_time_utc_ticks)
    }
    foreach ($Target in $Targets) {
        if (-not (Stop-OwnedProcess -ProcessId $Target[0] -StartTicks $Target[1])) {
            return $false
        }
    }
    return $true
}

function Stop-NewlyStartedProcessTree {
    param([Diagnostics.Process]$StartedProcess)
    if (-not $StartedProcess) { return $true }
    try {
        if ($StartedProcess.HasExited) {
            $StartedProcess.WaitForExit()
            return $true
        }
        $StartedPid = [int]$StartedProcess.Id
        $Taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
        & $Taskkill /PID $StartedPid /T /F 2>$null | Out-Null
        if (-not $StartedProcess.WaitForExit(10000)) {
            try { $StartedProcess.Kill() } catch {}
            if (-not $StartedProcess.WaitForExit(10000)) { return $false }
        }
        return $true
    } catch {
        return $false
    }
}

function Get-LoopbackListenerProcessId {
    param([int]$ListenPort)
    $Pattern = '^\s*TCP\s+127\.0\.0\.1:' + [regex]::Escape([string]$ListenPort) + '\s+\S+\s+LISTENING\s+(\d+)\s*$'
    foreach ($Line in (& "$env:SystemRoot\System32\netstat.exe" -ano -p tcp)) {
        if ($Line -match $Pattern) { return [int]$Matches[1] }
    }
    return 0
}

function Invoke-ExternalWithTimeout {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(Mandatory=$true)][string]$Arguments,
        [int]$TimeoutSeconds = 20
    )
    $External = $null
    $ExternalId = 0
    $ExternalStartTicks = 0
    $OutputRead = $null
    $ErrorRead = $null
    try {
        $StartInfo = New-Object Diagnostics.ProcessStartInfo
        $StartInfo.FileName = $FilePath
        $StartInfo.Arguments = $Arguments
        $StartInfo.UseShellExecute = $false
        $StartInfo.CreateNoWindow = $true
        $StartInfo.RedirectStandardOutput = $true
        $StartInfo.RedirectStandardError = $true
        $External = New-Object Diagnostics.Process
        $External.StartInfo = $StartInfo
        if (-not $External.Start()) {
            throw "$FilePath did not return a process handle"
        }
        $ExternalId = [int]$External.Id
        $ExternalStartTicks = [int64]$External.StartTime.ToUniversalTime().Ticks
        $OutputRead = $External.StandardOutput.ReadToEndAsync()
        $ErrorRead = $External.StandardError.ReadToEndAsync()
        if (-not $External.WaitForExit($TimeoutSeconds * 1000)) {
            if (-not (Stop-OwnedProcess -ProcessId $ExternalId `
                -StartTicks $ExternalStartTicks -IgnoreTestFailure)) {
                throw "$FilePath timed out after $TimeoutSeconds seconds and termination was not confirmed"
            }
            throw "$FilePath timed out after $TimeoutSeconds seconds"
        }
        # Windows PowerShell 5.1 can expose a stale/null ExitCode until the
        # redirected streams finish and the Process object is refreshed.
        $External.WaitForExit()
        $External.Refresh()
        $ExitCode = [int]$External.ExitCode
        $Output = $OutputRead.GetAwaiter().GetResult()
        $ErrorOutput = $ErrorRead.GetAwaiter().GetResult()
        if ($null -eq $Output) { $Output = "" }
        if ($null -eq $ErrorOutput) { $ErrorOutput = "" }
        if ($ExitCode -ne 0) {
            $Failure = $ErrorOutput.Trim() -replace '[\r\n]+', ' '
            if ([string]::IsNullOrWhiteSpace($Failure)) {
                $Failure = "$FilePath exited with code $ExitCode"
            }
            throw $Failure
        }
        return $Output.Trim()
    } finally {
        if ($External) {
            try {
                if (-not $External.HasExited -and $ExternalId -gt 0 -and $ExternalStartTicks -gt 0) {
                    [void](Stop-OwnedProcess -ProcessId $ExternalId `
                        -StartTicks $ExternalStartTicks -IgnoreTestFailure)
                } elseif (-not $External.HasExited) {
                    [void](Stop-NewlyStartedProcessTree -StartedProcess $External)
                }
                if ($External.HasExited) { $External.WaitForExit() }
            } catch {}
            $External.Dispose()
        }
    }
}

function Invoke-Tailscale {
    param([string[]]$ArgumentList)
    $Arguments = Join-ProcessArguments -Values @($TailscalePrefixArguments + $ArgumentList)
    return Invoke-ExternalWithTimeout -FilePath $TailscaleExecutable `
        -Arguments $Arguments -TimeoutSeconds $ExternalTimeoutSeconds
}

function Test-JsonContainsExpectedProxy {
    param($Value, [string]$Expected)
    if ($null -eq $Value) { return $false }
    if ($Value -is [string] -or $Value -is [ValueType]) { return $false }
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($Key in $Value.Keys) {
            if ([string]$Key -eq "Proxy" -and [string]$Value[$Key] -eq $Expected) {
                return $true
            }
            if (Test-JsonContainsExpectedProxy -Value $Value[$Key] -Expected $Expected) {
                return $true
            }
        }
        return $false
    }
    if ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
        foreach ($Child in $Value) {
            if (Test-JsonContainsExpectedProxy -Value $Child -Expected $Expected) { return $true }
        }
        return $false
    }
    foreach ($Property in $Value.PSObject.Properties) {
        if ($Property.Name -eq "Proxy" -and [string]$Property.Value -eq $Expected) {
            return $true
        }
        if (Test-JsonContainsExpectedProxy -Value $Property.Value -Expected $Expected) {
            return $true
        }
    }
    return $false
}

function Test-JsonValueIsTruthy {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return $false }
    if ($Value -is [bool]) { return [bool]$Value }
    if ($Value -is [string]) { return -not [string]::IsNullOrEmpty([string]$Value) }
    if ($Value -is [System.Collections.IDictionary]) { return $Value.Count -gt 0 }
    if ($Value -is [System.Collections.IEnumerable]) {
        foreach ($Item in $Value) { return $true }
        return $false
    }
    if ($Value.PSObject -and $Value.PSObject.Properties.Count -gt 0) { return $true }
    return [bool]$Value
}

function Test-JsonContainsTruthyFunnelMarker {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return $false }
    if ($Value -is [string] -or $Value -is [ValueType]) { return $false }
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($Key in $Value.Keys) {
            $Child = $Value[$Key]
            if ([string]$Key -match "(?i)funnel" -and (Test-JsonValueIsTruthy -Value $Child)) {
                return $true
            }
            if (Test-JsonContainsTruthyFunnelMarker -Value $Child) { return $true }
        }
        return $false
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        foreach ($Child in $Value) {
            if (Test-JsonContainsTruthyFunnelMarker -Value $Child) { return $true }
        }
        return $false
    }
    foreach ($Property in $Value.PSObject.Properties) {
        if ($Property.Name -match "(?i)funnel" -and
            (Test-JsonValueIsTruthy -Value $Property.Value)) {
            return $true
        }
        if (Test-JsonContainsTruthyFunnelMarker -Value $Property.Value) { return $true }
    }
    return $false
}

function Assert-ServeConfiguration {
    param([string]$ExpectedTarget)
    $RawStatus = Invoke-Tailscale -ArgumentList @("serve", "status", "--json")
    try { $ParsedStatus = $RawStatus | ConvertFrom-Json } catch {
        throw "Tailscale Serve status did not return valid JSON"
    }
    if (-not $ParsedStatus -or $ParsedStatus.PSObject.Properties.Count -eq 0) {
        throw "Tailscale Serve status is empty after configuration"
    }
    if (Test-JsonContainsTruthyFunnelMarker -Value $ParsedStatus) {
        throw "Tailscale Serve status reports a Funnel-enabled route"
    }
    if (-not $ParsedStatus.Web -or
        -not (Test-JsonContainsExpectedProxy -Value $ParsedStatus.Web -Expected $ExpectedTarget)) {
        throw "Tailscale Serve status does not bind the expected PeerBridge backend"
    }
    return $RawStatus
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Value)
    $Bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    return [BitConverter]::ToString(
        [Security.Cryptography.SHA256]::Create().ComputeHash($Bytes)
    ).Replace("-", "").ToLowerInvariant()
}

function New-RemoteAccessCredential {
    $Bytes = New-Object byte[] 32
    $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Generator.GetBytes($Bytes) } finally { $Generator.Dispose() }
    return ([BitConverter]::ToString($Bytes)).Replace("-", "").ToLowerInvariant()
}

function Protect-CurrentUserFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $Security = New-Object Security.AccessControl.FileSecurity
    $Security.SetOwner($Identity)
    $Security.SetAccessRuleProtection($true, $false)
    $Rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $Identity,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $Security.AddAccessRule($Rule)
    $LegacySetter = [IO.File].GetMethods() | Where-Object {
        $_.Name -eq 'SetAccessControl' -and $_.GetParameters().Count -eq 2
    } | Select-Object -First 1
    if ($null -ne $LegacySetter) {
        [IO.File]::SetAccessControl($Path, $Security)
    } else {
        $FileInfo = Get-Item -LiteralPath $Path -Force
        [IO.FileSystemAclExtensions]::SetAccessControl($FileInfo, $Security)
    }
}

function Write-PrivateAccessUrl {
    param(
        [Parameter(Mandatory = $true)][string]$PublicOrigin,
        [Parameter(Mandatory = $true)][string]$Credential
    )
    if ($Credential -notmatch '^[A-Za-z0-9_-]{43,256}$') {
        throw "Generated remote access credential is outside the safe contract."
    }
    if (-not (Test-Path -LiteralPath $AccessUrlFile -PathType Leaf)) {
        [IO.File]::WriteAllBytes($AccessUrlFile, [byte[]]@())
    }
    Protect-CurrentUserFile -Path $AccessUrlFile
    $Utf8 = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText(
        $AccessUrlFile,
        $PublicOrigin.TrimEnd('/') + "/#${AccessFragmentName}=" + $Credential,
        $Utf8
    )
    Protect-CurrentUserFile -Path $AccessUrlFile
}

function Read-PrivateAccessCredential {
    param([Parameter(Mandatory = $true)][string]$PublicOrigin)
    if (-not (Test-Path -LiteralPath $AccessUrlFile -PathType Leaf)) { return $null }
    $Value = [IO.File]::ReadAllText($AccessUrlFile).Trim()
    $Prefix = $PublicOrigin.TrimEnd('/') + "/#${AccessFragmentName}="
    if (-not $Value.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $null
    }
    $Credential = $Value.Substring($Prefix.Length)
    if ($Credential -notmatch '^[A-Za-z0-9_-]{43,256}$') { return $null }
    return $Credential
}

function Write-ServeState {
    param(
        [string]$PublicOrigin,
        [string]$LocalBackend,
        [string]$Scope,
        [string]$ValidatedServeStatus,
        [string]$ProxyCredentialSha256,
        [AllowNull()][string]$EvidenceRunId
    )
    if ([string]::IsNullOrWhiteSpace($ValidatedServeStatus)) {
        throw "Validated Tailscale Serve status is required before state can be written."
    }
    $StatusBytes = [Text.Encoding]::UTF8.GetBytes($ValidatedServeStatus)
    $StatusHash = [BitConverter]::ToString(
        [Security.Cryptography.SHA256]::Create().ComputeHash($StatusBytes)
    ).Replace("-", "").ToLowerInvariant()
    $TemporaryState = $ServeState + ".tmp"
    $StateDocument = @{
        public_origin = $PublicOrigin
        local_backend = $LocalBackend
        scope = $Scope
        transport = "tailscale-serve"
        tailnet_only = $true
        funnel_enabled = $false
        proxy_credential_sha256 = $ProxyCredentialSha256
        validated_serve_status_sha256 = $StatusHash
        configured_utc = [DateTime]::UtcNow.ToString("o")
    }
    if (-not [string]::IsNullOrEmpty($EvidenceRunId)) {
        $StateDocument.evidence_run_id = $EvidenceRunId
    }
    $StateDocument | ConvertTo-Json -Compress | Set-Content -LiteralPath $TemporaryState -Encoding ascii
    Move-Item -LiteralPath $TemporaryState -Destination $ServeState -Force
}

if (-not (Test-Path -LiteralPath $BackendExecutable -PathType Leaf)) {
    throw "PeerBridge isolated .venv is missing."
}
if (-not $TestMode -and -not (Get-Command $TailscaleExecutable -ErrorAction SilentlyContinue)) {
    throw "Tailscale CLI is unavailable."
}

New-Item -ItemType Directory -Path $State -Force | Out-Null
$TailscaleStatus = Invoke-Tailscale -ArgumentList @("status", "--json")
try {
    $Tail = $TailscaleStatus | ConvertFrom-Json
    $DnsName = [string]$Tail.Self.DNSName
} catch { throw "Tailscale status did not return a usable DNS identity" }
if ([string]$Tail.BackendState -ne "Running" -or -not [bool]$Tail.Self.Online) {
    throw "Tailscale is not online (BackendState must be Running). PeerBridge did not expose a listener."
}
if ([string]::IsNullOrWhiteSpace($DnsName)) {
    throw "Tailscale MagicDNS name is unavailable"
}
$CertDomains = @(
    $Tail.CertDomains | Where-Object {
        -not [string]::IsNullOrWhiteSpace([string]$_)
    }
)
if ($CertDomains.Count -eq 0) {
    throw "Tailscale HTTPS certificates are not enabled for this tailnet. Open the Tailscale admin DNS page once, enable HTTPS certificates, then rerun this launcher. PeerBridge did not expose a listener."
}
$PublicOrigin = "https://" + $DnsName.TrimEnd('.').ToLowerInvariant()
$ReusableProxyCredential = Read-PrivateAccessCredential -PublicOrigin $PublicOrigin
$ReusableProxyCredentialSha256 = if ($ReusableProxyCredential) {
    Get-Sha256Hex -Value $ReusableProxyCredential
} else { $null }
if (Test-Path -LiteralPath $PidFile) {
    $ExistingLock = $null
    try { $ExistingLock = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json } catch {}
    $ExistingPid = if ($ExistingLock -and $ExistingLock.pid) { [int]$ExistingLock.pid } else { 0 }
    $Existing = if ($ExistingPid) { Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue } else { $null }
    $ExistingStart = if ($Existing) { $Existing.StartTime.ToUniversalTime().Ticks } else { 0 }
    $OwnedIdentity = $Existing -and $ExistingLock -and
        ([int64]$ExistingLock.start_time_utc_ticks -eq $ExistingStart)
    $IsRequestedInstance = $OwnedIdentity -and
        ([int]$ExistingLock.port -eq $Port) -and
        ([string]$ExistingLock.scope -eq $Scope)
    $ExistingEvidenceRunId = if (
        $ExistingLock -and
        @($ExistingLock.PSObject.Properties.Name) -contains "evidence_run_id" -and
        -not [string]::IsNullOrEmpty([string]$ExistingLock.evidence_run_id)
    ) { [string]$ExistingLock.evidence_run_id } else { $null }
    $EvidenceIdentityMatches = if ($RequestedEvidenceRunId) {
        $ExistingEvidenceRunId -eq $RequestedEvidenceRunId
    } else {
        $null -eq $ExistingEvidenceRunId
    }
    $ExistingProxyCredentialSha256 = if (
        $ExistingLock -and
        @($ExistingLock.PSObject.Properties.Name) -contains "proxy_credential_sha256"
    ) { [string]$ExistingLock.proxy_credential_sha256 } else { $null }
    $CredentialIdentityMatches = $ReusableProxyCredential -and
        $ExistingProxyCredentialSha256 -eq $ReusableProxyCredentialSha256
    $IsRequestedInstance = $IsRequestedInstance -and $EvidenceIdentityMatches -and
        $CredentialIdentityMatches
    if ($IsRequestedInstance) {
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 2
            $IsRequestedInstance = $Health.status -eq "ok" -and
                [string]$Health.instance_id -eq [string]$ExistingLock.instance_id -and
                [int]$Health.process_id -eq $ExistingPid -and
                [string]$Health.proxy_credential_sha256 -eq $ReusableProxyCredentialSha256 -and
                [string]$Health.surface -eq "full-workspace"
            if ($IsRequestedInstance -and $RequestedEvidenceRunId) {
                $IsRequestedInstance = [string]$Health.evidence_run_id -eq $RequestedEvidenceRunId
            }
        } catch { $IsRequestedInstance = $false }
    }
    if ($IsRequestedInstance) {
        Write-Output "PeerBridge remote backend is already running (PID $ExistingPid)."
        $TargetBackend = "http://127.0.0.1:$Port"
        $ServeOutput = Invoke-Tailscale -ArgumentList @(
            "serve", "--bg", "--yes", $TargetBackend
        )
        if ($ServeOutput) { Write-Output $ServeOutput }
        $StatusOutput = Assert-ServeConfiguration -ExpectedTarget $TargetBackend
        Write-PrivateAccessUrl -PublicOrigin $PublicOrigin `
            -Credential $ReusableProxyCredential
        Write-ServeState -PublicOrigin $PublicOrigin -LocalBackend $TargetBackend `
            -Scope $Scope -ValidatedServeStatus $StatusOutput `
            -ProxyCredentialSha256 $ReusableProxyCredentialSha256 `
            -EvidenceRunId $RequestedEvidenceRunId
        Write-Output "PeerBridge private access URL saved to: $AccessUrlFile"
        if ($StatusOutput) { Write-Output $StatusOutput }
        exit 0
    }
    if ($OwnedIdentity) {
        if (-not (Stop-OwnedBackendTree -Ownership $ExistingLock)) {
            throw "The previously owned PeerBridge backend could not be stopped; ownership was retained."
        }
        Remove-OwnedLock -ProcessId $ExistingPid -StartTicks $ExistingStart
    } elseif (-not $Existing) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
}

$InstanceId = "remote-" + [guid]::NewGuid().ToString("N")
$ProxyCredential = New-RemoteAccessCredential
$ProxyCredentialSha256 = Get-Sha256Hex -Value $ProxyCredential
$BackendArgumentList = @($BackendPrefixArguments) + @(
    "--project-root", ".",
    "--db", ".peerbridge\peerbridge.sqlite3",
    "--scope", $Scope,
    "--host", "127.0.0.1",
    "--port", [string]$Port,
    "--public-origin", $PublicOrigin,
    "--instance-id", $InstanceId,
    "--full-workspace"
)
if ($RequestedEvidenceRunId) {
    $BackendArgumentList += @("--evidence-run-id", $RequestedEvidenceRunId)
}
$Arguments = Join-ProcessArguments -Values $BackendArgumentList
$PreviousProxyCredential = [Environment]::GetEnvironmentVariable(
    "PEERBRIDGE_REMOTE_PROXY_CREDENTIAL",
    [EnvironmentVariableTarget]::Process
)
try {
    [Environment]::SetEnvironmentVariable(
        "PEERBRIDGE_REMOTE_PROXY_CREDENTIAL",
        $ProxyCredential,
        [EnvironmentVariableTarget]::Process
    )
    try {
        $Process = Start-Process -FilePath $BackendExecutable -ArgumentList $Arguments -WorkingDirectory $Root `
            -WindowStyle Hidden -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -PassThru
    } finally {
        [Environment]::SetEnvironmentVariable(
            "PEERBRIDGE_REMOTE_PROXY_CREDENTIAL",
            $PreviousProxyCredential,
            [EnvironmentVariableTarget]::Process
        )
    }
    if (-not $Process) {
        throw "PeerBridge backend launch did not return a process handle."
    }
    $LauncherPid = [int]$Process.Id
    $LauncherStartTicks = [int64]$Process.StartTime.ToUniversalTime().Ticks
    $InitialOwnershipDocument = @{
        pid = $LauncherPid
        start_time_utc_ticks = $LauncherStartTicks
        port = $Port
        scope = $Scope
        instance_id = $InstanceId
        proxy_credential_sha256 = $ProxyCredentialSha256
    }
    if ($RequestedEvidenceRunId) {
        $InitialOwnershipDocument.evidence_run_id = $RequestedEvidenceRunId
    }
    $InitialOwnershipDocument | ConvertTo-Json -Compress | Set-Content -LiteralPath $PidFile -Encoding ascii
    $Process.Dispose()
    $Process = $null
} catch {
    if ($LauncherPid -gt 0 -and $LauncherStartTicks -gt 0 -and
        -not (Stop-OwnedProcess -ProcessId $LauncherPid -StartTicks $LauncherStartTicks)) {
        throw "PeerBridge started but its ownership lock failed and the process could not be stopped."
    } elseif ($Process -and ($LauncherStartTicks -le 0) -and
        -not (Stop-NewlyStartedProcessTree -StartedProcess $Process)) {
        throw "PeerBridge started without a stable identity and the process could not be stopped."
    }
    throw
}

$Ready = $false
$HealthProcessId = 0
for ($Attempt = 0; $Attempt -lt $HealthAttempts; $Attempt++) {
    Start-Sleep -Milliseconds $HealthDelayMilliseconds
    try {
        $Response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" `
            -TimeoutSec $HealthTimeoutSeconds
        if ($Response.status -eq "ok" -and $Response.instance_id -eq $InstanceId -and
            [int]$Response.process_id -gt 0 -and
            [string]$Response.proxy_credential_sha256 -eq $ProxyCredentialSha256) {
            $HealthEvidenceMatches = -not $RequestedEvidenceRunId -or
                [string]$Response.evidence_run_id -eq $RequestedEvidenceRunId
            if ($HealthEvidenceMatches) {
                $HealthProcessId = [int]$Response.process_id
                $Ready = $true
                break
            }
        }
    } catch {}
}
if (-not $Ready) {
    $InitialOwnership = [pscustomobject]@{
        pid = $LauncherPid
        start_time_utc_ticks = $LauncherStartTicks
    }
    if (-not (Stop-OwnedBackendTree -Ownership $InitialOwnership)) {
        throw "PeerBridge remote backend did not become ready and cleanup was not confirmed; ownership was retained."
    }
    Remove-OwnedLock -ProcessId $LauncherPid -StartTicks $LauncherStartTicks
    throw "PeerBridge remote backend did not become ready; inspect $Stderr"
}

$ListenerPid = $HealthProcessId
$ObservedListenerPid = Get-LoopbackListenerProcessId -ListenPort $Port
$ListenerPidMatches = $ObservedListenerPid -eq $ListenerPid
$Listener = if ($ListenerPid) { Get-Process -Id $ListenerPid -ErrorAction SilentlyContinue } else { $null }
if (-not $Listener -or -not $ListenerPidMatches) {
    $InitialOwnership = [pscustomobject]@{
        pid = $LauncherPid
        start_time_utc_ticks = $LauncherStartTicks
    }
    if (-not (Stop-OwnedBackendTree -Ownership $InitialOwnership)) {
        throw "PeerBridge health identity failed and cleanup was not confirmed; ownership was retained."
    }
    Remove-OwnedLock -ProcessId $LauncherPid -StartTicks $LauncherStartTicks
    throw "PeerBridge health passed but its process identity could not be resolved"
}
$ListenerStart = $Listener.StartTime.ToUniversalTime().Ticks
$Listener.Dispose()
$Listener = $null
$FinalOwnershipDocument = @{
    pid = $ListenerPid
    start_time_utc_ticks = $ListenerStart
    launcher_pid = $LauncherPid
    launcher_start_time_utc_ticks = $LauncherStartTicks
    port = $Port
    scope = $Scope
    instance_id = $InstanceId
    proxy_credential_sha256 = $ProxyCredentialSha256
}
if ($RequestedEvidenceRunId) {
    $FinalOwnershipDocument.evidence_run_id = $RequestedEvidenceRunId
}
$FinalOwnershipDocument | ConvertTo-Json -Compress | Set-Content -LiteralPath $PidFile -Encoding ascii

$ServeFailed = $false
$Ownership = [pscustomobject]@{
    pid = $ListenerPid
    start_time_utc_ticks = $ListenerStart
    launcher_pid = $LauncherPid
    launcher_start_time_utc_ticks = $LauncherStartTicks
}
$TargetBackend = "http://127.0.0.1:$Port"
try {
    $ServeOutput = Invoke-Tailscale -ArgumentList @(
        "serve", "--bg", "--yes", $TargetBackend
    )
    if ($ServeOutput) { Write-Output $ServeOutput }
    $StatusOutput = Assert-ServeConfiguration -ExpectedTarget $TargetBackend
    Write-PrivateAccessUrl -PublicOrigin $PublicOrigin -Credential $ProxyCredential
    Write-ServeState -PublicOrigin $PublicOrigin -LocalBackend $TargetBackend `
        -Scope $Scope -ValidatedServeStatus $StatusOutput `
        -ProxyCredentialSha256 $ProxyCredentialSha256 `
        -EvidenceRunId $RequestedEvidenceRunId
    Write-Output "PeerBridge private access URL saved to: $AccessUrlFile"
    if ($StatusOutput) { Write-Output $StatusOutput }
} catch {
    $ServeFailed = $true
    $ServeFailureMessage = "Tailscale Serve configuration failed: $($_.Exception.Message)"
    # Emit the original validation failure before process cleanup. Under heavy
    # Windows load, cleanup can terminate descendants before PowerShell flushes
    # an unhandled terminating error, which would otherwise hide the cause.
    [Console]::Error.WriteLine($ServeFailureMessage)
    [Console]::Error.Flush()
    if (-not (Stop-OwnedBackendTree -Ownership $Ownership)) {
        throw "Tailscale Serve failed and PeerBridge cleanup was not confirmed; ownership was retained. Original error: $($_.Exception.Message)"
    }
    Remove-OwnedLock -ProcessId $ListenerPid -StartTicks $ListenerStart
    throw $ServeFailureMessage
}
} finally {
    if ($Process) {
        try {
            if ($Process.HasExited) { $Process.WaitForExit() }
        } catch {}
        $Process.Dispose()
    }
    if ($LauncherMutexOwned) {
        try { $LauncherMutex.ReleaseMutex() | Out-Null } catch {}
    }
    $LauncherMutex.Dispose()
}
