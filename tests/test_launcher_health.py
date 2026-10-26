"""Exercise the launcher's real HTTP readiness helper without launching services."""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


LAUNCHER = Path(__file__).resolve().parents[1] / "start-code-agent.vbs"
CSCRIPT = shutil.which("cscript.exe")
pytestmark = pytest.mark.skipif(CSCRIPT is None, reason="Windows Script Host is required")


@pytest.fixture
def probe_script(tmp_path):
    source = LAUNCHER.read_text(encoding="utf-8-sig")
    helper = re.search(r"^Function IsUp\(.*?^End Function", source, re.MULTILINE | re.DOTALL)
    assert helper, "The launcher must have a separately testable health probe"
    # Only the health helper is executed; shell launch and browser code are absent.
    script = tmp_path / "probe.vbs"
    script.write_text(
        "Option Explicit\n"
        "Dim ready, reason\n"
        + helper.group(0)
        + '\nready = IsUp(WScript.Arguments(0), reason)\n'
        + 'WScript.Echo CStr(ready) & "|" & reason\n',
        encoding="utf-16",
    )
    return script


def probe(script, url):
    result = subprocess.run(
        [CSCRIPT, "//nologo", str(script), url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=8,
        check=True,
    )
    ready, reason = result.stdout.strip().split("|", 1)
    return ready == "True", reason


@pytest.fixture(scope="module")
def health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = int(self.path.lstrip("/"))
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/200")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, True), (204, True), (299, True), (300, False), (302, False),
     (404, False), (500, False), (503, False)],
)
def test_only_http_2xx_counts_as_ready(probe_script, health_server, status, expected):
    ready, reason = probe(probe_script, f"{health_server}/{status}")
    assert ready is expected
    assert reason == f"HTTP {status}"


def test_unreachable_service_reports_failure(probe_script):
    # Keep the local port reserved but not listening, so no real service is involved.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        ready, reason = probe(probe_script, f"http://127.0.0.1:{reserved.getsockname()[1]}/")
    assert ready is False
    assert reason.startswith("Health check failed: ")
    assert reason.removeprefix("Health check failed: ").strip()


def run_launcher_fixture(
    tmp_path, backend, frontend, *, recover=True, no_open=True,
    start_only=True, restart_exit=0, explicit_restart=False, internal_locked=True,
    extra_args=(), track_probes=False,
):
    """Run the actual VBScript flow with shell/FS mocks, never real processes."""
    def vb_bool(value):
        return "True" if value else "False"

    source = LAUNCHER.read_text(encoding="utf-8-sig")
    source = source.replace('Set sh = CreateObject("WScript.Shell")', "Set sh = New FakeShell")
    source = source.replace('Set fso = CreateObject("Scripting.FileSystemObject")', "Set fso = New FakeFiles")
    source = source.replace("WScript.Sleep 1000", "' Fixture skips waiting.")
    source = source.replace('MsgBox message, vbExclamation, "Code-agent"', 'WScript.Echo "FAIL|" & message')
    source = source.replace('WScript.Echo message', 'WScript.Echo "FAIL|" & message')
    helper = re.search(r"^Function IsUp\(.*?^End Function", source, re.MULTILINE | re.DOTALL)
    assert helper
    source = source[:helper.start()] + f"""
Function IsUp(url, ByRef healthError)
    {'WScript.Echo "PROBE|" & url' if track_probes else ''}
    If InStr(url, ":8765/") > 0 Then
        backendChecks = backendChecks + 1
        If backendWasRestarted Then
            IsUp = {vb_bool(recover)} And backendLaunches > 0 And backendChecks > 1
        Else
            IsUp = {vb_bool(backend)} Or ({vb_bool(recover)} And backendChecks > 1)
        End If
    Else
        frontendChecks = frontendChecks + 1
        IsUp = {vb_bool(frontend)} Or ({vb_bool(recover)} And frontendChecks > 1)
    End If
    If IsUp Then
        healthError = "HTTP 200"
    Else
        healthError = "HTTP 500"
    End If
End Function
""" + source[helper.end():]
    mocks = f"""
Dim backendChecks, frontendChecks, backendWasRestarted, backendLaunches
backendChecks = 0
frontendChecks = 0
backendWasRestarted = False
backendLaunches = 0
Class FakeShell
    Public CurrentDirectory
    Public Function Run(command, windowStyle, wait)
        WScript.Echo "SHELL|" & windowStyle & "|" & wait & "|" & command
        If InStr(command, "restart-code-agent-backend.ps1") > 0 Then
            If {restart_exit} = 0 Then backendWasRestarted = True
            Run = {restart_exit}
            Exit Function
        End If
        If InStr(command, "launch-code-agent.ps1") > 0 Then
            Run = {restart_exit}
            Exit Function
        End If
        If InStr(command, " -m code_agent.server ") > 0 Then
            backendLaunches = backendLaunches + 1
        End If
        Run = 0
    End Function
End Class
Class FakeFiles
    Public Function GetTempName()
        GetTempName = "fixture.tmp"
    End Function
    Public Function GetParentFolderName(path)
        GetParentFolderName = "C:\\launcher-fixture"
    End Function
    Public Function FolderExists(path)
        FolderExists = True
    End Function
    Public Function FileExists(path)
        FileExists = True
    End Function
    Public Sub CreateFolder(path)
        Err.Raise 5, "fixture", "Unexpected directory creation"
    End Sub
End Class
"""
    source = source.replace("Option Explicit", "Option Explicit\n" + mocks, 1)
    assert 'CreateObject("WScript.Shell")' not in source
    script = tmp_path / "launcher-fixture.vbs"
    script.write_text(source, encoding="utf-16")
    args = [CSCRIPT, "//nologo", str(script)]
    if internal_locked:
        args.append("--internal-locked")
    if start_only:
        args.append("--start-only")
    if explicit_restart or start_only is False:
        args.append("--restart-backend")
    if no_open:
        args.append("--no-open")
    args.extend(extra_args)
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)


@pytest.mark.parametrize(
    ("backend", "frontend", "starts"),
    [(True, True, []), (False, True, ["backend"]),
     (True, False, ["frontend"]), (False, False, ["backend", "frontend"])],
)
def test_launcher_only_starts_missing_services(tmp_path, backend, frontend, starts):
    result = run_launcher_fixture(tmp_path, backend, frontend)
    assert result.returncode == 0, result.stderr
    commands = [line for line in result.stdout.splitlines() if line.startswith("SHELL|")]
    assert len(commands) == len(starts)
    for command, service in zip(commands, starts):
        assert command.startswith("SHELL|0|False|"), "Every service must start hidden without waiting"
        if service == "backend":
            assert " -m code_agent.server " in command
        else:
            assert "vite.cmd" in command and " --strictPort" in command


def test_browser_opens_only_when_both_services_are_ready(tmp_path):
    result = run_launcher_fixture(tmp_path, True, True, no_open=False)
    assert result.returncode == 0
    assert result.stdout.strip() == "SHELL|1|False|http://127.0.0.1:5175/"


def test_failed_backend_is_not_reported_ready_or_opened_in_browser(tmp_path):
    result = run_launcher_fixture(tmp_path, False, True, recover=False, no_open=False)
    assert result.returncode == 1
    assert "FAIL|Code-agent services did NOT become ready." in result.stdout
    assert "Backend: HTTP 500" in result.stdout
    assert "Frontend: HTTP 200" in result.stdout
    assert "\\logs\\backend.log" in result.stdout
    assert "SHELL|1|False|http://127.0.0.1:5175/" not in result.stdout


def test_explicit_restart_waits_hidden_and_probes_updated_backend(tmp_path):
    result = run_launcher_fixture(
        tmp_path, True, True, start_only=False, explicit_restart=True,
        track_probes=True,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    commands = [line for line in lines if line.startswith("SHELL|")]
    assert len(commands) == 2
    assert commands[0].startswith("SHELL|0|True|")
    assert "restart-code-agent-backend.ps1" in commands[0]
    assert '-Root "C:\\launcher-fixture"' in commands[0]
    assert commands[1].startswith("SHELL|0|False|")
    assert " -m code_agent.server " in commands[1]
    assert not any("vite.cmd" in line for line in commands), "The frontend stays running"
    backend_probes = [i for i, line in enumerate(lines) if line == "PROBE|http://127.0.0.1:8765/healthz"]
    assert len(backend_probes) == 2, "Readiness is freshly checked after stop and after replacement launch"
    assert lines.index(commands[0]) < backend_probes[0] < lines.index(commands[1]) < backend_probes[1]


def test_plain_double_click_reuses_healthy_services_without_restart_checks(tmp_path):
    result = run_launcher_fixture(tmp_path, True, True, no_open=False, restart_exit=12, start_only=None)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SHELL|1|False|http://127.0.0.1:5175/"
    assert "restart-code-agent-backend.ps1" not in result.stdout


def test_outer_default_passes_start_only_to_the_locked_wrapper(tmp_path):
    # Remove the fixture's explicit mode flag: this is the actual double-click
    # mode, with no permission to interrupt an already healthy service.
    result = run_launcher_fixture(tmp_path, True, True, internal_locked=False, start_only=None)
    assert result.returncode == 0, result.stderr
    commands = [line for line in result.stdout.splitlines() if line.startswith("SHELL|")]
    assert len(commands) == 1 and ' -StartOnly' in commands[0]


@pytest.mark.parametrize("restart_exit", [12, 1])
def test_restart_busy_or_unverified_does_not_launch_or_open_browser(tmp_path, restart_exit):
    result = run_launcher_fixture(
        tmp_path, True, True, start_only=False, restart_exit=restart_exit,
        no_open=False, track_probes=True,
    )
    assert result.returncode == (12 if restart_exit == 12 else 1)
    commands = [line for line in result.stdout.splitlines() if line.startswith("SHELL|")]
    assert len(commands) == 1 and "restart-code-agent-backend.ps1" in commands[0]
    assert not any(line.startswith("PROBE|") for line in result.stdout.splitlines())
    assert "FAIL|" in result.stdout
    if restart_exit == 12:
        assert "Services and tasks have been preserved" in result.stdout
    else:
        assert "No replacement backend was started" in result.stdout


def test_unknown_launcher_option_refused_before_shell_operations(tmp_path):
    result = run_launcher_fixture(tmp_path, True, True, extra_args=("--unknown-option",))
    assert result.returncode == 1
    assert "Unknown launcher option: --unknown-option" in result.stdout
    assert "SHELL|" not in result.stdout


@pytest.mark.parametrize(("start_only", "no_open"), [(False, False), (False, True), (True, True)])
def test_outer_launcher_waits_hidden_for_locked_flow_and_forwards_flags(tmp_path, start_only, no_open):
    result = run_launcher_fixture(
        tmp_path, True, True, start_only=start_only, no_open=no_open, internal_locked=False,
    )
    assert result.returncode == 0, result.stderr
    commands = [line for line in result.stdout.splitlines() if line.startswith("SHELL|")]
    assert len(commands) == 1
    assert commands[0].startswith("SHELL|0|True|")
    assert "launch-code-agent.ps1" in commands[0]
    assert (' -StartOnly' in commands[0]) is start_only
    assert (' -NoOpen' in commands[0]) is no_open
    assert " -m code_agent.server " not in commands[0]


@pytest.mark.parametrize(("helper_exit", "outer_exit", "message"), [
    (12, 12, "Backend restart deferred"), (1, 1, "The launcher could not complete"),
    (13, 1, "already starting or restarting"),
    (14, 1, "The launcher could not complete"),
])
def test_outer_launcher_failure_never_runs_inner_start_again(tmp_path, helper_exit, outer_exit, message):
    result = run_launcher_fixture(
        tmp_path, True, True, start_only=False, no_open=False,
        internal_locked=False, restart_exit=helper_exit,
    )
    assert result.returncode == outer_exit, result.stderr
    commands = [line for line in result.stdout.splitlines() if line.startswith("SHELL|")]
    assert len(commands) == 1 and "launch-code-agent.ps1" in commands[0]
    if message:
        assert message in result.stdout
