"""Run restart selection/guard logic against fake Windows process cmdlets."""

from __future__ import annotations

import json
import shutil
import subprocess
import re
import time
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESTART = ROOT / "scripts" / "restart-code-agent-backend.ps1"
LAUNCH_WRAPPER = ROOT / "scripts" / "launch-code-agent.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required")


def run_restart_fixture(tmp_path, scenario):
    """Execute real helper functions; all service/process interaction is mocked."""
    source = RESTART.read_text(encoding="utf-8-sig")
    marker = "# Main is kept separate"
    assert marker in source, "Restart helpers must be testable without their production main"
    functions = source.split(marker, 1)[0]
    # The extracted mandatory parameter is supplied with a disposable existing
    # directory. Never dot-source or invoke the production script's main.
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    mocks = r'''
$global:Scenario = $FixtureScenario
$global:ReadCount = 0
$global:IdleCount = 0
$global:SleepCount = 0
$global:Stopped = New-Object 'System.Collections.Generic.List[int]'
$global:WasStopped = $false

function Get-NetTCPConnection {
    param($State, $ErrorAction)
    if ($State -ne 'Listen') { throw 'Only listener queries are allowed' }
    $global:ReadCount++
    # A frontend always exists. It must never be selected or stopped.
    [pscustomobject]@{ LocalPort = 5175; OwningProcess = 999 }
    if ($global:Scenario -eq 'no_backend') { return }
    if ($global:Scenario -eq 'listener_exited' -and $global:ReadCount -ge 2) { return }
    if ($global:WasStopped) {
        if ($global:Scenario -eq 'lingering') {
            [pscustomobject]@{ LocalPort = 8765; OwningProcess = 222 }
        } elseif ($global:Scenario -eq 'replacement_after_stop') {
            [pscustomobject]@{ LocalPort = 8765; OwningProcess = 333 }
        } elseif ($global:Scenario -eq 'stale_after_stop' -and $global:ReadCount -eq 3) {
            [pscustomobject]@{ LocalPort = 8765; OwningProcess = 222 }
        }
        return
    }
    $owner = 222
    if ($global:Scenario -eq 'changed_pid' -and $global:ReadCount -ge 2) { $owner = 333 }
    if ($global:Scenario -eq 'owner_changed_during_retry' -and $global:ReadCount -ge 2) { $owner = 333 }
    [pscustomobject]@{ LocalPort = 8765; OwningProcess = $owner }
    # Duplicate connections belonging to one process are harmless.
    [pscustomobject]@{ LocalPort = 8765; OwningProcess = $owner }
    if ($global:Scenario -eq 'multiple_owners') {
        [pscustomobject]@{ LocalPort = 8765; OwningProcess = 444 }
    }
}

function Get-CimInstance {
    param($ClassName, $Filter, $OperationTimeoutSec, $ErrorAction)
    if ($ClassName -ne 'Win32_Process' -or $Filter -notmatch '^ProcessId=(\d+)$') {
        throw 'Unexpected process query'
    }
    if ($OperationTimeoutSec -ne 3) { throw 'CIM queries must have a bounded timeout' }
    if ($global:Scenario -eq 'missing_process' -or
        ($global:Scenario -in @('transient_missing_process', 'listener_exited', 'owner_changed_during_retry') -and $global:ReadCount -eq 1) -or
        ($global:Scenario -eq 'stale_after_stop' -and $global:WasStopped)) { return }
    $owner = [int]$Matches[1]
    $created = [datetime]'2026-10-12T00:00:00Z'
    if ($global:Scenario -eq 'changed_creation' -and $global:ReadCount -ge 2) {
        $created = $created.AddSeconds(1)
    }
    if ($global:Scenario -eq 'missing_creation') { $created = $null }
    if ($global:Scenario -eq 'transient_missing_creation' -and $global:ReadCount -eq 1) { $created = $null }
    $python = Join-Path $Root '.venv\Scripts\python.exe'
    $command = '"' + $python + '" -m code_agent.server --host 127.0.0.1 --port 8765'
    switch ($global:Scenario) {
        'wrong_workspace' { $command = '"C:\another\.venv\Scripts\python.exe" -m code_agent.server --host 127.0.0.1 --port 8765' }
        'wrong_module' { $command = $command.Replace('code_agent.server', 'other.server') }
        'extra_command' { $command += ' --other-flag' }
        'unquoted_python' { $command = $python + ' -m code_agent.server --host 127.0.0.1 --port 8765' }
    }
    [pscustomobject]@{ ProcessId = $owner; CreationDate = $created; CommandLine = $command }
}

function Test-CodeAgentIdle {
    param($WorkspaceRoot)
    $global:IdleCount++
    if ($WorkspaceRoot -ne $Root) { throw 'Incorrect guard workspace' }
    if ($global:Scenario -eq 'guard_unavailable') { throw 'Could not inspect executions' }
    Write-Output ('IDLE|' + $global:IdleCount)
    if ($global:Scenario -eq 'first_busy' -or
        ($global:Scenario -eq 'second_busy' -and $global:IdleCount -eq 2)) {
        return 12
    }
    return 0
}

function Stop-Process {
    param($Id, $ErrorAction)
    # If production code selected anything else, fail before recording a stop.
    if ($Id -ne 222) { throw 'Unexpected stop target' }
    [void]$global:Stopped.Add([int]$Id)
    $global:WasStopped = $true
}

function Start-Sleep {
    param($Milliseconds)
    if ($Milliseconds -ne 100) { throw 'Unexpected wait interval' }
    $global:SleepCount++
}

$caughtError = ''
$resultCode = 1
try {
    $result = @(Invoke-CodeAgentBackendRestart $Root)
    $resultCode = $result[-1]
} catch {
    $caughtError = $_.Exception.Message
}
[pscustomobject]@{
    code = $resultCode
    stops = @($global:Stopped.ToArray())
    idle_checks = $global:IdleCount
    listener_reads = $global:ReadCount
    sleeps = $global:SleepCount
    error = $caughtError
} | ConvertTo-Json -Compress
'''
    # FixtureScenario is a separate script parameter to avoid embedding values
    # into PowerShell source. The test's Stop-Process function is the only stop.
    functions = functions.replace(
        "param([Parameter(Mandatory = $true)][string]$Root)",
        "param([Parameter(Mandatory = $true)][string]$Root, [string]$FixtureScenario)",
        1,
    )
    assert "[string]$FixtureScenario" in functions
    script = tmp_path / "restart-fixture.ps1"
    script.write_text(functions + mocks, encoding="utf-8-sig")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), "-Root", str(workspace), "-FixtureScenario", scenario],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=12,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


@pytest.mark.parametrize("scenario", ["normal", "unquoted_python"])
def test_restart_selects_only_this_backend_after_two_idle_checks(tmp_path, scenario):
    result = run_restart_fixture(tmp_path, scenario)
    assert result == {
        "code": 0, "stops": [222], "idle_checks": 2,
        "listener_reads": 3, "sleeps": 0, "error": "",
    }


def test_no_backend_listener_allows_launcher_to_start_without_a_stop(tmp_path):
    result = run_restart_fixture(tmp_path, "no_backend")
    assert result["code"] == 0
    assert result["stops"] == []
    assert result["idle_checks"] == 0


@pytest.mark.parametrize("scenario", ["transient_missing_process", "transient_missing_creation"])
def test_transient_cim_identity_is_retried_with_a_fresh_listener_before_restart(tmp_path, scenario):
    result = run_restart_fixture(tmp_path, scenario)
    assert result == {
        "code": 0, "stops": [222], "idle_checks": 2,
        "listener_reads": 4, "sleeps": 1, "error": "",
    }


def test_missing_process_allows_start_only_after_a_fresh_snapshot_has_no_listener(tmp_path):
    result = run_restart_fixture(tmp_path, "listener_exited")
    assert result == {
        "code": 0, "stops": [], "idle_checks": 0,
        "listener_reads": 2, "sleeps": 1, "error": "",
    }


def test_stale_listener_after_stop_is_rechecked_before_allowing_a_replacement(tmp_path):
    result = run_restart_fixture(tmp_path, "stale_after_stop")
    assert result == {
        "code": 0, "stops": [222], "idle_checks": 2,
        "listener_reads": 4, "sleeps": 1, "error": "",
    }


@pytest.mark.parametrize("scenario", ["missing_process", "missing_creation"])
def test_stable_unknown_identity_fails_closed_after_only_three_snapshots(tmp_path, scenario):
    result = run_restart_fixture(tmp_path, scenario)
    assert result["code"] == 1
    assert result["stops"] == []
    assert result["idle_checks"] == 0
    assert result["listener_reads"] == 3
    assert result["sleeps"] == 2
    assert "identity could not be verified" in result["error"]


def test_a_different_owner_during_identity_retry_is_not_accepted_or_stopped(tmp_path):
    result = run_restart_fixture(tmp_path, "owner_changed_during_retry")
    assert result["code"] == 1
    assert result["stops"] == []
    assert result["idle_checks"] == 0
    assert result["listener_reads"] == 2
    assert result["sleeps"] == 1
    assert "changed during identity inspection" in result["error"]


@pytest.mark.parametrize("scenario", [
    "multiple_owners", "wrong_workspace", "wrong_module", "extra_command", "missing_creation",
])
def test_unverified_process_is_refused_before_idle_or_stop(tmp_path, scenario):
    result = run_restart_fixture(tmp_path, scenario)
    assert result["code"] == 1
    assert result["stops"] == []
    assert result["idle_checks"] == 0
    assert result["error"]


@pytest.mark.parametrize(("scenario", "checks"), [("first_busy", 1), ("second_busy", 2)])
def test_busy_first_or_last_snapshot_preserves_all_services(tmp_path, scenario, checks):
    result = run_restart_fixture(tmp_path, scenario)
    assert result["code"] == 12
    assert result["stops"] == []
    assert result["idle_checks"] == checks
    assert result["error"] == ""


@pytest.mark.parametrize("scenario", ["changed_pid", "changed_creation"])
def test_listener_identity_is_rechecked_immediately_before_stop(tmp_path, scenario):
    result = run_restart_fixture(tmp_path, scenario)
    assert result["code"] == 1
    assert result["idle_checks"] == 2
    assert result["stops"] == []
    assert "changed during inspection" in result["error"]


def test_guard_failure_preserves_services(tmp_path):
    result = run_restart_fixture(tmp_path, "guard_unavailable")
    assert result["code"] == 1
    assert result["stops"] == []
    assert "Could not inspect executions" in result["error"]


@pytest.mark.parametrize("scenario", ["lingering", "replacement_after_stop"])
def test_unclosed_or_replaced_listener_refuses_a_new_launch(tmp_path, scenario):
    result = run_restart_fixture(tmp_path, scenario)
    assert result["code"] == 1
    assert result["stops"] == [222]
    assert result["error"]
    if scenario == "lingering":
        assert result["sleeps"] == 50
        assert "did not close" in result["error"]
    else:
        assert result["sleeps"] == 0
        assert "Another backend appeared" in result["error"]


def test_real_quoted_launcher_chain_in_spaced_workspace_defers_busy_without_services(tmp_path):
    """Exercise cmd -> PowerShell -> cscript quoting, not production services."""
    cscript = shutil.which("cscript.exe")
    if cscript is None:
        pytest.skip("Windows Script Host is required")
    workspace = tmp_path / "real quoted workspace with spaces"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    vbs = (ROOT / "start-code-agent.vbs").read_text(encoding="utf-8-sig")
    health = re.search(r"^Function IsUp\(.*?^End Function", vbs, re.MULTILINE | re.DOTALL)
    assert health
    # Even a regression in refusal handling cannot query or launch services:
    # reaching any health probe exits this disposable script immediately.
    vbs = vbs[:health.start()] + '''Function IsUp(url, ByRef healthError)
    WScript.Echo "UNEXPECTED_REAL_PROBE"
    WScript.Quit 88
End Function''' + vbs[health.end():]
    vbs = vbs.replace('MsgBox message, vbExclamation, "Code-agent"', 'WScript.Echo message')
    (workspace / "start-code-agent.vbs").write_text(vbs, encoding="utf-16")
    wrapper = LAUNCH_WRAPPER.read_text(encoding="utf-8-sig").replace(
        "Local\\CodeAgentLauncher-8765-5175", "Local\\CodeAgentFixture-" + uuid.uuid4().hex,
    )
    (scripts / "launch-code-agent.ps1").write_text(wrapper, encoding="utf-8-sig")
    (scripts / "restart-code-agent-backend.ps1").write_text(
        'param([string]$Root)\nWrite-Output "FIXTURE_GUARD_BUSY"\nexit 12\n',
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [cscript, "//nologo", str(workspace / "start-code-agent.vbs"), "--restart-backend", "--no-open"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
    )
    assert result.returncode == 12, result.stderr
    assert "Backend restart deferred" in result.stdout
    guard_log = (workspace / "logs" / "launcher.log").read_text(encoding="utf-8", errors="replace")
    dispatch_files = list((workspace / "logs").glob("launcher-*.log"))
    assert len(dispatch_files) == 1
    dispatch_log = dispatch_files[0].read_text(encoding="utf-8-sig", errors="replace")
    assert "FIXTURE_GUARD_BUSY" in guard_log
    assert "UNEXPECTED_REAL_PROBE" not in result.stdout + guard_log + dispatch_log
    assert "Services and tasks have been preserved" in dispatch_log
    assert "Launcher finished; exit=12" in dispatch_log


def test_wrapper_private_mutex_prevents_concurrent_launcher_and_preserves_child_exit(tmp_path):
    """A private named mutex checks real locking without blocking the real launcher."""
    mutex_name = "Local\\CodeAgentFixture-" + uuid.uuid4().hex
    wrapper = LAUNCH_WRAPPER.read_text(encoding="utf-8-sig").replace(
        "Local\\CodeAgentLauncher-8765-5175", mutex_name,
    )
    wrapper_file = tmp_path / "wrapper.ps1"
    wrapper_file.write_text(wrapper, encoding="utf-8-sig")
    (tmp_path / "start-code-agent.vbs").write_text(
        'WScript.Echo "FIXTURE_CHILD_RAN"\nWScript.Quit 12\n', encoding="utf-16",
    )
    holder_file = tmp_path / "hold-mutex.ps1"
    holder_file.write_text('''param([string]$MutexName, [string]$Ready, [string]$Release)
$mutex = [System.Threading.Mutex]::new($true, $MutexName)
try {
    [System.IO.File]::WriteAllText($Ready, 'ready')
    while (-not [System.IO.File]::Exists($Release)) { Start-Sleep -Milliseconds 50 }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
''', encoding="utf-8-sig")
    ready = tmp_path / "holder-ready"
    release = tmp_path / "holder-release"
    holder = subprocess.Popen(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(holder_file), "-MutexName", mutex_name, "-Ready", str(ready), "-Release", str(release)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(wrapper_file), "-Root", str(tmp_path), "-NoOpen"]
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "The fixture mutex holder must start before contention is checked"
        locked = subprocess.run(command, capture_output=True, text=True, timeout=12)
        assert locked.returncode == 13, locked.stderr
        assert "FIXTURE_CHILD_RAN" not in locked.stdout
    finally:
        release.write_text("release", encoding="utf-8")
        try:
            holder.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate(timeout=5)
    unlocked = subprocess.run(command, capture_output=True, text=True, timeout=12)
    assert unlocked.returncode == 12, unlocked.stderr
    assert "FIXTURE_CHILD_RAN" in unlocked.stdout
