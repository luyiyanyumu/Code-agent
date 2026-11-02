"""Run real Windows identity matching with fake listeners, CIM and stop cmdlets."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "npm" / "launcher-process.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required")


def fixture_result(tmp_path, scenario, *, action="Inspect", service="backend"):
    source = HELPER.read_text(encoding="utf-8-sig").split("# Main is separate", 1)[0]
    source = source.replace("[string]$ExpectedCreated = ''", "[string]$ExpectedCreated = '',\n    [string]$FixtureScenario = ''")
    mocks = r'''
$global:ReadCount = 0
$global:Stops = @()
function Get-LauncherListeners {
    $global:ReadCount++
    if ($FixtureScenario -eq 'no_listener') { return }
    [pscustomobject]@{port=$BackendPort; pid=222}
    [pscustomobject]@{port=$BackendPort; pid=222}
    [pscustomobject]@{port=$FrontendPort; pid=333}
    # A substring match against this unrelated port would incorrectly select it.
    [pscustomobject]@{port=18765; pid=999}
    if ($FixtureScenario -eq 'multiple_owners') { [pscustomobject]@{port=$BackendPort; pid=444} }
}
function Get-CimInstance {
    param($ClassName, $Filter, $OperationTimeoutSec, $ErrorAction)
    if ($ClassName -ne 'Win32_Process' -or $Filter -notmatch '^ProcessId=(\d+)$' -or $OperationTimeoutSec -ne 3) {
        throw 'Unexpected process query'
    }
    $identityPid = [int]$Matches[1]
    if ($FixtureScenario -eq 'missing_process') { return }
    $created = [datetime]'2026-10-01T01:02:03Z'
    if ($FixtureScenario -eq 'changed_creation' -and $global:ReadCount -ge 2) { $created = $created.AddSeconds(1) }
    if ($FixtureScenario -eq 'missing_creation') { $created = $null }
    $command = '"' + $BackendPython + '" -m code_agent.server --host 127.0.0.1 --port ' + $BackendPort
    if ($FixtureScenario -eq 'installed') {
        $command = '"' + $BackendPython + '" -I -X utf8 -u -m code_agent.server --host 127.0.0.1 --port ' + $BackendPort
    }
    if ($FixtureScenario -eq 'bundled') {
        $command = '"' + $BackendPython + '" -I -B -X utf8 -m code_agent.server --host 127.0.0.1 --port ' + $BackendPort
    }
    if ($FixtureScenario -eq 'provider') { $command += ' --provider offline' }
    if ($FixtureScenario -eq 'wrong_backend') { $command = $command.Replace($BackendPython, 'C:\other\python.exe') }
    if ($FixtureScenario -eq 'wrong_module') { $command = $command.Replace('code_agent.server', 'other.server') }
    if ($FixtureScenario -eq 'extra_args') { $command += ' --reload' }
    if ($FixtureScenario -eq 'wrong_host') { $command = $command.Replace('127.0.0.1', '0.0.0.0') }
    if ($identityPid -eq 333) {
        $entry = Join-Path $Root 'web\node_modules\.bin\\..\vite\bin\vite.js'
        if ($FixtureScenario -eq 'wrong_frontend') { $entry = 'C:\other\web\node_modules\vite\bin\vite.js' }
        $command = '"node" "' + $entry + '" --host 127.0.0.1 --port ' + $FrontendPort + ' --strictPort'
        if ($FixtureScenario -eq 'preview') {
            $command = '"C:\Program Files\nodejs\node.exe" "' + (Join-Path $Root 'npm\launcher-preview.mjs') + '" --port ' + $FrontendPort + ' --backend-port ' + $BackendPort
        }
    }
    if ($FixtureScenario -eq 'changed_pid' -and $global:ReadCount -ge 2 -and $identityPid -eq 222) { $identityPid = 777 }
    [pscustomobject]@{ ProcessId=$identityPid; CreationDate=$created; CommandLine=$command }
}
function Stop-Process {
    param($Id, $ErrorAction)
    if ($Id -notin @(222,333)) { throw 'An unrelated process was selected' }
    $global:Stops += $Id
}
try {
    if ($Action -eq 'Stop') {
        $result = Invoke-LauncherStop $Service $Root $BackendPython $BackendPort $FrontendPort $ExpectedPid $ExpectedCreated
    } else {
        $result = Invoke-LauncherInspect $Root $BackendPython $BackendPort $FrontendPort
    }
    [pscustomobject]@{result=$result; stops=@($global:Stops); reads=$global:ReadCount; error=''} | ConvertTo-Json -Compress -Depth 8
} catch {
    [pscustomobject]@{result=$null; stops=@($global:Stops); reads=$global:ReadCount; error=$_.Exception.Message} | ConvertTo-Json -Compress -Depth 8
}
'''
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    fixture = tmp_path / "launcher-process-fixture.ps1"
    fixture.write_text(source + mocks, encoding="utf-8-sig")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(fixture),
         "-Action", action, "-Root", str(workspace), "-BackendPython", str(workspace / ".venv/Scripts/python.exe"),
         "-Service", service, "-ExpectedPid", "222" if service == "backend" else "333",
         "-ExpectedCreated", "2026-10-01T01:02:03.0000000Z", "-FixtureScenario", scenario],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


@pytest.mark.parametrize("scenario", ["normal", "installed", "bundled", "provider", "preview"])
def test_inspection_accepts_only_exact_workspace_service_commands(tmp_path, scenario):
    result = fixture_result(tmp_path, scenario)
    assert result["error"] == ""
    assert result["stops"] == []
    assert result["result"]["backend"]["owned"] is True
    assert result["result"]["frontend"]["owned"] is True
    assert result["result"]["backend"]["pid"] == 222
    assert result["result"]["frontend"]["pid"] == 333


@pytest.mark.parametrize("scenario", ["wrong_backend", "wrong_module", "extra_args", "wrong_host", "multiple_owners", "missing_process", "missing_creation"])
def test_unverified_backend_cannot_be_stopped(tmp_path, scenario):
    result = fixture_result(tmp_path, scenario, action="Stop")
    assert result["error"] == "The service identity changed; stop refused."
    assert result["stops"] == []


def test_other_checkout_frontend_cannot_be_stopped(tmp_path):
    result = fixture_result(tmp_path, "wrong_frontend", action="Stop", service="frontend")
    assert result["error"] == "The service identity changed; stop refused."
    assert result["stops"] == []


@pytest.mark.parametrize("service, expected", [("backend", 222), ("frontend", 333)])
def test_stop_rechecks_identity_and_stops_only_selected_process(tmp_path, service, expected):
    result = fixture_result(tmp_path, "normal", action="Stop", service=service)
    assert result["error"] == ""
    assert result["stops"] == [expected]
    assert result["reads"] == 2
    assert result["result"]["stopped"] is True


@pytest.mark.parametrize("scenario", ["changed_pid", "changed_creation"])
def test_replacement_between_snapshots_does_not_inherit_stop_permission(tmp_path, scenario):
    result = fixture_result(tmp_path, scenario, action="Stop")
    assert result["error"] == "The service identity changed; stop refused."
    assert result["stops"] == []
    assert result["reads"] == 2


def test_absent_services_are_not_reported_as_owned(tmp_path):
    result = fixture_result(tmp_path, "no_listener")
    assert result["error"] == ""
    for service in ("backend", "frontend"):
        assert result["result"][service] == {"pid": None, "created": None, "owned": False, "detail": "not-listening"}
