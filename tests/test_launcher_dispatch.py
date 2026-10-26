"""Exercise launcher dispatch in disposable workspaces without real services."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
CSCRIPT = shutil.which("cscript.exe")
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or CSCRIPT is None or POWERSHELL is None,
    reason="Windows Script Host and Windows PowerShell are required",
)


@contextmanager
def exclusive_file(path: Path):
    """Keep only this disposable log open, denying sharing to child processes."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    # OPEN_EXISTING + share=0. No real log or service is accessed by the fixture.
    handle = create_file(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        assert close_handle(handle), "The fixture log handle must be released"


def workspace_paths(tmp_path):
    workspace = tmp_path / "launcher workspace with spaces"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    logs = workspace / "logs"
    logs.mkdir()
    unrelated_cwd = tmp_path / "unrelated launch directory"
    unrelated_cwd.mkdir()
    old_log = logs / "launcher-dispatch.log"
    old_log.write_text("PREVIOUS_DISPATCH_HISTORY\n", encoding="utf-8")
    return workspace, scripts, logs, unrelated_cwd, old_log


def run_hidden_command(command, cwd):
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def private_wrapper(source):
    assert "Local\\CodeAgentLauncher-8765-5175" in source
    return source.replace(
        "Local\\CodeAgentLauncher-8765-5175",
        "Local\\CodeAgentDispatchFixture-" + uuid.uuid4().hex,
    )


def diagnostic_text(path):
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig", errors="replace")


def assert_recorded_exit(text, code):
    assert re.search(
        rf"(?im)(?:exit\s*(?:code)?|exit_code|code)\s*[:=]?\s*{code}\b", text,
    ), f"The diagnostic must include the final exit code {code}: {text}"


@pytest.mark.parametrize(("stub_exit", "mode_args", "expected_start_only"), [
    (0, (), True),
    (12, ("--start-only",), True),
    (12, ("--restart-backend",), False),
])
def test_outer_launcher_reaches_wrapper_when_old_dispatch_log_is_exclusively_locked(
    tmp_path, stub_exit, mode_args, expected_start_only,
):
    workspace, scripts, logs, other_cwd, old_log = workspace_paths(tmp_path)
    source = (ROOT / "start-code-agent.vbs").read_text(encoding="utf-8-sig")
    # The actual outer VBS runs, but even an accidental internal entry cannot
    # probe, stop or launch services from this isolated fixture.
    anchor = "If restartBackend Then"
    assert anchor in source
    source = source.replace(
        anchor,
        'WScript.Echo "UNEXPECTED_FIXTURE_INTERNAL_ENTRY"\nWScript.Quit 88\n' + anchor,
        1,
    )
    (workspace / "start-code-agent.vbs").write_text(source, encoding="utf-16")
    (scripts / "launch-code-agent.ps1").write_text(
        f'''param([string]$Root, [switch]$StartOnly, [switch]$NoOpen, [string]$LogPath)
$receipt = [pscustomobject]@{{ root = $Root; start_only = [bool]$StartOnly; no_open = [bool]$NoOpen; log_path = $LogPath }}
[System.IO.File]::WriteAllText((Join-Path $Root 'wrapper-receipt.json'), ($receipt | ConvertTo-Json -Compress))
[System.IO.File]::WriteAllText($LogPath, "FIXTURE_WRAPPER_REACHED`nExit code: {stub_exit}`n")
exit {stub_exit}
''',
        encoding="utf-8-sig",
    )
    with exclusive_file(old_log):
        result = run_hidden_command(
            [CSCRIPT, "//nologo", str(workspace / "start-code-agent.vbs"),
             *mode_args, "--no-open"], other_cwd,
        )
    assert result.returncode == stub_exit, result.stdout + result.stderr
    assert "UNEXPECTED_FIXTURE_INTERNAL_ENTRY" not in result.stdout + result.stderr
    receipt = json.loads((workspace / "wrapper-receipt.json").read_text(encoding="utf-8-sig"))
    assert Path(receipt["root"]) == workspace
    assert receipt["start_only"] is expected_start_only
    assert receipt["no_open"] is True
    run_log = Path(receipt["log_path"])
    assert run_log.parent == logs
    assert run_log != old_log
    assert "FIXTURE_WRAPPER_REACHED" in diagnostic_text(run_log)
    assert old_log.read_text(encoding="utf-8") == "PREVIOUS_DISPATCH_HISTORY\n"
    if stub_exit == 12:
        assert "Backend restart deferred" in result.stdout


@pytest.mark.parametrize("explicit_log_path", [False, True])
def test_wrapper_records_child_stdout_stderr_and_nonzero_exit_without_shared_log(
    tmp_path, explicit_log_path,
):
    workspace, scripts, logs, other_cwd, old_log = workspace_paths(tmp_path)
    wrapper = private_wrapper(
        (ROOT / "scripts" / "launch-code-agent.ps1").read_text(encoding="utf-8-sig")
    )
    wrapper_file = scripts / "launch-code-agent.ps1"
    wrapper_file.write_text(wrapper, encoding="utf-8-sig")
    (workspace / "start-code-agent.vbs").write_text(
        'WScript.StdOut.WriteLine "FIXTURE_CHILD_STDOUT"\n'
        'WScript.StdErr.WriteLine "FIXTURE_CHILD_STDERR"\nWScript.Quit 7\n',
        encoding="utf-16",
    )
    command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(wrapper_file), "-Root", str(workspace), "-NoOpen"]
    requested_log = logs / "explicit fixture diagnostic.log"
    if explicit_log_path:
        command.extend(["-LogPath", str(requested_log)])
    with exclusive_file(old_log):
        result = run_hidden_command(command, other_cwd)
    assert result.returncode == 7, result.stdout + result.stderr
    run_logs = [p for p in logs.glob("*.log") if p != old_log]
    assert len(run_logs) == 1, "Each fixture launch must persist its own diagnostic"
    if explicit_log_path:
        assert run_logs == [requested_log]
    diagnostic = diagnostic_text(run_logs[0])
    assert "FIXTURE_CHILD_STDOUT" in diagnostic
    assert "FIXTURE_CHILD_STDERR" in diagnostic
    assert_recorded_exit(diagnostic, 7)
    assert old_log.read_text(encoding="utf-8") == "PREVIOUS_DISPATCH_HISTORY\n"


def test_wrapper_exception_records_requested_diagnostic_and_final_exit(tmp_path):
    workspace, scripts, logs, other_cwd, old_log = workspace_paths(tmp_path)
    wrapper_file = scripts / "launch-code-agent.ps1"
    wrapper_file.write_text(
        private_wrapper((ROOT / "scripts" / "launch-code-agent.ps1").read_text(encoding="utf-8-sig")),
        encoding="utf-8-sig",
    )
    missing_root = workspace / "missing fixture workspace"
    run_log = logs / "exception fixture diagnostic.log"
    with exclusive_file(old_log):
        result = run_hidden_command(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(wrapper_file), "-Root", str(missing_root), "-NoOpen", "-LogPath", str(run_log)],
            other_cwd,
        )
    assert result.returncode == 14, result.stdout + result.stderr
    diagnostic = diagnostic_text(run_log)
    assert "Launcher failed" in diagnostic
    assert missing_root.name in diagnostic
    assert_recorded_exit(diagnostic, 14)
