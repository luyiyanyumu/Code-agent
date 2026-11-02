param(
    [ValidateSet('Inspect', 'Stop')][string]$Action = 'Inspect',
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$BackendPython,
    [ValidateRange(1, 65535)][int]$BackendPort = 8765,
    [ValidateRange(1, 65535)][int]$FrontendPort = 5175,
    [ValidateSet('backend', 'frontend')][string]$Service = 'backend',
    [int]$ExpectedPid = 0,
    [string]$ExpectedCreated = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Get-LauncherArguments {
    param([string]$CommandLine)
    # Use Windows' own quoting rules. No command line is ever evaluated.
    if (-not ('CodeAgentLauncher.Arguments' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace CodeAgentLauncher {
    public static class Arguments {
        [DllImport("shell32.dll", SetLastError = true)]
        private static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string line, out int count);
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr pointer);
        public static string[] Parse(string line) {
            int count;
            IntPtr pointer = CommandLineToArgvW(line, out count);
            if (pointer == IntPtr.Zero) throw new InvalidOperationException("Cannot parse process arguments.");
            try {
                string[] values = new string[count];
                for (int index = 0; index < count; index++)
                    values[index] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer, index * IntPtr.Size));
                return values;
            } finally { LocalFree(pointer); }
        }
    }
}
'@ -ErrorAction Stop
    }
    if (-not $CommandLine) { return @() }
    return [CodeAgentLauncher.Arguments]::Parse($CommandLine)
}

function Get-LauncherListeners {
    # netstat is much faster than a full Get-NetTCPConnection query on Windows.
    # Match the complete numeric local port, not a substring (e.g. 18765).
    $lines = @(& (Join-Path $env:SystemRoot 'System32\netstat.exe') -ano -p TCP)
    if ($LASTEXITCODE -ne 0) { throw 'Listener inspection failed.' }
    foreach ($line in $lines) {
        if ($line -match '^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$') {
            [pscustomobject]@{ port = [int]$Matches[1]; pid = [int]$Matches[2] }
        }
    }
}

function Test-LauncherPath {
    param([string]$Actual, [string]$Expected)
    if (-not [System.IO.Path]::IsPathRooted($Actual)) { return $false }
    try {
        return [System.IO.Path]::GetFullPath($Actual).TrimEnd('\') -ieq [System.IO.Path]::GetFullPath($Expected).TrimEnd('\')
    } catch { return $false }
}

function Test-LauncherBackend {
    param([string[]]$Arguments, [string]$Python, [int]$Port)
    if ($Arguments.Count -lt 7 -or -not (Test-LauncherPath $Arguments[0] $Python)) { return $false }
    $index = 1
    # The source launcher and installed npm runtime use these interpreter flags.
    while ($index -lt $Arguments.Count) {
        if ($Arguments[$index] -in @('-I', '-B', '-u')) { $index++; continue }
        if ($Arguments[$index] -eq '-X' -and $index + 1 -lt $Arguments.Count -and $Arguments[$index + 1] -eq 'utf8') {
            $index += 2; continue
        }
        break
    }
    $tail = @($Arguments | Select-Object -Skip $index)
    if ($tail.Count -notin @(6, 8)) { return $false }
    if ($tail[0] -ne '-m' -or $tail[1] -ne 'code_agent.server' -or
        $tail[2] -ne '--host' -or $tail[3] -ne '127.0.0.1' -or
        $tail[4] -ne '--port' -or $tail[5] -ne [string]$Port) { return $false }
    if ($tail.Count -eq 8 -and ($tail[6] -ne '--provider' -or
        $tail[7] -notin @('offline', 'deepseek', 'openai', 'openai_compat'))) { return $false }
    return $true
}

function Test-LauncherFrontend {
    param([string[]]$Arguments, [string]$Workspace, [int]$Port, [int]$ApiPort)
    if ($Arguments.Count -lt 2 -or [System.IO.Path]::GetFileName($Arguments[0]) -notin @('node', 'node.exe')) { return $false }
    if (Test-LauncherPath $Arguments[1] (Join-Path $Workspace 'npm\launcher-preview.mjs')) {
        return $Arguments.Count -eq 6 -and $Arguments[2] -eq '--port' -and
            $Arguments[3] -eq [string]$Port -and $Arguments[4] -eq '--backend-port' -and $Arguments[5] -eq [string]$ApiPort
    }
    if ($Arguments.Count -ne 7) { return $false }
    if (-not (Test-LauncherPath $Arguments[1] (Join-Path $Workspace 'web\node_modules\vite\bin\vite.js'))) { return $false }
    return $Arguments[2] -eq '--host' -and $Arguments[3] -eq '127.0.0.1' -and
        $Arguments[4] -eq '--port' -and $Arguments[5] -eq [string]$Port -and $Arguments[6] -eq '--strictPort'
}

function Get-LauncherService {
    param([string]$Name, [string]$Workspace, [string]$Python, [int]$Port, [object[]]$Listeners, [int]$ApiPort)
    $owners = @($Listeners | Where-Object port -eq $Port | Select-Object -ExpandProperty pid -Unique)
    if ($owners.Count -eq 0) {
        return [pscustomobject]@{ pid = $null; created = $null; owned = $false; detail = 'not-listening' }
    }
    if ($owners.Count -ne 1) {
        return [pscustomobject]@{ pid = $null; created = $null; owned = $false; detail = 'multiple-listener-owners' }
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($owners[0])" -OperationTimeoutSec 3 -ErrorAction Stop
    if ($null -eq $process -or $null -eq $process.CreationDate) {
        return [pscustomobject]@{ pid = $owners[0]; created = $null; owned = $false; detail = 'identity-unavailable' }
    }
    $arguments = @(Get-LauncherArguments ([string]$process.CommandLine))
    $owned = if ($Name -eq 'backend') { Test-LauncherBackend $arguments $Python $Port }
        else { Test-LauncherFrontend $arguments $Workspace $Port $ApiPort }
    $created = ([DateTime]$process.CreationDate).ToUniversalTime().ToString('o')
    return [pscustomobject]@{ pid = [int]$process.ProcessId; created = $created; owned = [bool]$owned;
        detail = $(if ($owned) { 'verified-workspace-process' } else { 'unrecognized-process' }) }
}

function Invoke-LauncherInspect {
    param([string]$Workspace, [string]$Python, [int]$ApiPort, [int]$WebPort)
    $listeners = @(Get-LauncherListeners)
    return [pscustomobject]@{
        backend = Get-LauncherService 'backend' $Workspace $Python $ApiPort $listeners
        frontend = Get-LauncherService 'frontend' $Workspace $Python $WebPort $listeners $ApiPort
    }
}

function Invoke-LauncherStop {
    param([string]$Name, [string]$Workspace, [string]$Python, [int]$ApiPort, [int]$WebPort,
        [int]$IdentityPid, [string]$IdentityCreated)
    if ($IdentityPid -le 0 -or -not $IdentityCreated) { throw 'A verified process identity is required.' }
    # Share the legacy launcher's lock so its in-flight launch/restart cannot
    # be stopped halfway through. Node serializes the new panel's operations.
    $mutex = New-Object System.Threading.Mutex($false, ('Local\CodeAgentLauncher-' + $ApiPort + '-' + $WebPort))
    $ownsLock = $false
    try {
        try { $ownsLock = $mutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $ownsLock = $true }
        if (-not $ownsLock) { throw 'Another launcher is changing these services; stop deferred.' }
        # Reinspect twice: neither a recycled PID nor a replacement port owner
        # may inherit authority from an earlier status or idle check.
        for ($attempt = 0; $attempt -lt 2; $attempt++) {
            $snapshot = Invoke-LauncherInspect $Workspace $Python $ApiPort $WebPort
            $current = $snapshot.$Name
            if (-not $current.owned -or $current.pid -ne $IdentityPid -or $current.created -cne $IdentityCreated) {
                throw 'The service identity changed; stop refused.'
            }
        }
        Stop-Process -Id $current.pid -ErrorAction Stop
        return [pscustomobject]@{ stopped = $true; service = $Name; pid = $current.pid; created = $current.created }
    } finally {
        if ($ownsLock) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

# Main is separate so tests replace all OS interaction without a live stop.
try {
    $workspace = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $python = [System.IO.Path]::GetFullPath($BackendPython)
    if ($BackendPort -eq $FrontendPort) { throw 'Backend and frontend ports must differ.' }
    $result = if ($Action -eq 'Inspect') {
        Invoke-LauncherInspect $workspace $python $BackendPort $FrontendPort
    } else {
        Invoke-LauncherStop $Service $workspace $python $BackendPort $FrontendPort $ExpectedPid $ExpectedCreated
    }
    $result | ConvertTo-Json -Compress -Depth 5
    exit 0
} catch {
    [pscustomobject]@{ error = $_.Exception.Message } | ConvertTo-Json -Compress
    exit 1
}
