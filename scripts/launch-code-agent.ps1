param(
    [Parameter(Mandatory = $true)][string]$Root,
    [switch]$StartOnly,
    [switch]$NoOpen,
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'
$mutex = $null
$ownsLock = $false
$launchLog = $null
function Write-LauncherDiagnostic {
    param([string]$Message)
    if ($launchLog) {
        # Open only while appending. Service children never inherit a shared
        # diagnostic stream that can block the next launcher invocation.
        $stamp = [DateTimeOffset]::Now.ToString('o')
        Add-Content -LiteralPath $launchLog -Value ("[$stamp] " + $Message) -Encoding UTF8 -ErrorAction Stop
    }
}
try {
    $mode = if ($StartOnly) { 'start-only' } else { 'restart-backend' }
    # The outer launcher supplies this path. Initialize it before validating
    # Root so a missing/moved workspace also has a useful failure record.
    if ($LogPath) {
        $launchLog = [System.IO.Path]::GetFullPath($LogPath)
        [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($launchLog))
        Write-LauncherDiagnostic ('Launcher started; mode=' + $mode)
    }
    $workspace = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).ProviderPath.TrimEnd('\')
    if (-not $launchLog) {
        $LogPath = Join-Path $workspace ('logs\launcher-' + [Guid]::NewGuid().ToString('N') + '.log')
        $launchLog = [System.IO.Path]::GetFullPath($LogPath)
        [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($launchLog))
        Write-LauncherDiagnostic ('Launcher started; mode=' + $mode)
    }
    # All checkouts using these ports share the lock, including first startup.
    $mutex = New-Object System.Threading.Mutex($false, 'Local\CodeAgentLauncher-8765-5175')
    try { $ownsLock = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ownsLock = $true }
    if (-not $ownsLock) {
        Write-LauncherDiagnostic 'Another launcher holds the startup lock; exit=13'
        exit 13
    }
    Write-LauncherDiagnostic 'Startup lock acquired'
    $launcherArgs = @('//nologo', (Join-Path $workspace 'start-code-agent.vbs'), '--internal-locked')
    if ($StartOnly) { $launcherArgs += '--start-only' }
    else { $launcherArgs += '--restart-backend' }
    if ($NoOpen) { $launcherArgs += '--no-open' }
    # The mutex stays owned across inspection, stop, launch and readiness.
    # Child services detach; the launcher/checks remain in this hidden process.
    $nativeErrorPreference = $ErrorActionPreference
    try {
        # Windows PowerShell turns native stderr into ErrorRecords. Capture it
        # as diagnostic text, retaining cscript's real exit code (including 12).
        $ErrorActionPreference = 'Continue'
        & (Join-Path $env:SystemRoot 'System32\cscript.exe') @launcherArgs 2>&1 | ForEach-Object {
            Write-LauncherDiagnostic ([string]$_)
            Write-Output ([string]$_)
        }
        $launcherExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $nativeErrorPreference
    }
    Write-LauncherDiagnostic ('Launcher finished; exit=' + $launcherExitCode)
    exit $launcherExitCode
} catch {
    $failure = 'Launcher failed: ' + $_.Exception.Message
    # Diagnostic failures must not turn a failed startup into success, or hide
    # the original error if the log directory itself is unavailable.
    try { Write-LauncherDiagnostic ($failure + '; exit=14') } catch { }
    Write-Output $failure
    exit 14
} finally {
    if ($ownsLock) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
