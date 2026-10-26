param([Parameter(Mandatory = $true)][string]$Root)

$ErrorActionPreference = 'Stop'

function Get-CodeAgentBackend {
    param([string]$WorkspaceRoot)
    $firstOwner = $null
    $process = $null
    for ($identityAttempt = 0; $identityAttempt -lt 3; $identityAttempt++) {
        # TCP and CIM are separate snapshots. A disappearing process must cause
        # a fresh listener query, never an assumption that its port is free.
        $owners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object LocalPort -eq 8765 |
            Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -eq 0) { return $null }
        if ($owners.Count -ne 1) { throw 'Port 8765 has multiple listener owners; restart refused.' }
        if ($null -eq $firstOwner) { $firstOwner = $owners[0] }
        elseif ($owners[0] -ne $firstOwner) {
            throw 'The backend listener changed during identity inspection; restart refused.'
        }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($owners[0])" -OperationTimeoutSec 3 -ErrorAction Stop
        if ($null -ne $process -and $null -ne $process.CreationDate) { break }
        if ($identityAttempt -lt 2) { Start-Sleep -Milliseconds 100 }
    }
    if ($null -eq $process -or $null -eq $process.CreationDate) {
        throw 'The backend listener identity could not be verified; restart refused.'
    }
    $pythonPath = [regex]::Escape((Join-Path $WorkspaceRoot '.venv\Scripts\python.exe'))
    $expected = '^(?:"' + $pythonPath + '"|' + $pythonPath + ')\s+-m\s+code_agent\.server\s+--host\s+127\.0\.0\.1\s+--port\s+8765\s*$'
    if ($process.CommandLine -notmatch $expected) {
        throw 'Port 8765 belongs to an unrecognized process; restart refused.'
    }
    return $process
}

function Test-CodeAgentIdle {
    param([string]$WorkspaceRoot)
    $guard = Join-Path $WorkspaceRoot 'scripts\backend_idle_check.py'
    $python = Join-Path $WorkspaceRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $guard -PathType Leaf) -or
        -not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'The restart safety check or backend Python is missing.'
    }
    $checkOutput = & $python $guard --base-url 'http://127.0.0.1:8765'
    $checkCode = $LASTEXITCODE
    $checkOutput | Write-Output
    if ($checkCode -eq 12) { return 12 }
    if ($checkCode -ne 0) { throw 'Could not confirm that all backend executions are idle.' }
    return 0
}

function Invoke-CodeAgentBackendRestart {
    param([string]$WorkspaceRoot)
    $workspace = (Resolve-Path -LiteralPath $WorkspaceRoot -ErrorAction Stop).ProviderPath.TrimEnd('\')
    $backend = Get-CodeAgentBackend $workspace
    if ($null -eq $backend) {
        Write-Output 'No backend listener; the launcher will start it.'
        return 0
    }
    # Two independent snapshots cover new work starting during inspection.
    # The second check runs immediately before validating the listener again.
    $firstCheck = @(Test-CodeAgentIdle $workspace)
    $firstCode = $firstCheck[-1]
    $firstCheck | Select-Object -SkipLast 1 | Write-Output
    if ($firstCode -eq 12) { return 12 }
    $finalCheck = @(Test-CodeAgentIdle $workspace)
    $finalCode = $finalCheck[-1]
    $finalCheck | Select-Object -SkipLast 1 | Write-Output
    if ($finalCode -eq 12) { return 12 }
    $current = Get-CodeAgentBackend $workspace
    if ($null -eq $current -or $current.ProcessId -ne $backend.ProcessId -or
        $current.CreationDate -ne $backend.CreationDate) {
        throw 'The backend changed during inspection; restart deferred.'
    }
    Stop-Process -Id $current.ProcessId -ErrorAction Stop
    # Do not launch a second backend while the old listener is still present.
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        $remaining = Get-CodeAgentBackend $workspace
        if ($null -eq $remaining) {
            Write-Output 'Backend stopped; the launcher will start the updated backend.'
            return 0
        }
        if ($remaining.ProcessId -ne $backend.ProcessId -or $remaining.CreationDate -ne $backend.CreationDate) {
            throw 'Another backend appeared after stopping; restart deferred.'
        }
        Start-Sleep -Milliseconds 100
    }
    throw 'The old backend listener did not close; no new backend was started.'
}

# Main is kept separate so process selection and refusal paths can be tested
# with fake Windows cmdlets without stopping or starting real services.
try {
    $result = @(Invoke-CodeAgentBackendRestart $Root)
    $exitCode = $result[-1]
    $result | Select-Object -SkipLast 1 | Write-Output
    exit $exitCode
} catch {
    Write-Output ('Backend restart failed: ' + $_.Exception.Message)
    exit 1
}
