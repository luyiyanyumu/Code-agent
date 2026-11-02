"""Picker protocol/COM tests; visible-window regressions are explicitly opt-in."""

import asyncio
import ctypes
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from code_agent.codeintel import native_dialog as native


class HelperProcess:
    def __init__(self, result=None, *, returncode=0, wait=False):
        self.result = result if result is not None else {"path": None, "cancelled": True}
        self.returncode = None
        self.finished_code = returncode
        self.waiting = wait
        self.killed = False
        self.input = None
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def communicate(self, data):
        self.input = data
        self.started.set()
        if self.waiting:
            await self.finished.wait()
        if not self.killed:
            self.returncode = self.finished_code
        output = self.result if isinstance(self.result, bytes) else json.dumps(self.result).encode("utf-8")
        return output, b""

    def kill(self):
        self.killed = True
        self.returncode = -1
        self.finished.set()

    async def wait(self):
        return self.returncode


def enabled(monkeypatch):
    monkeypatch.setattr(native, "capabilities", lambda: {"available": True})
    monkeypatch.setattr(native, "_parent_identity", lambda: {"parent_pid": 123, "parent_created": 456})


def spawn(monkeypatch, helper, calls=None):
    async def fake(*args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        return helper
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", fake)


def test_unavailable_platform_is_metadata_only(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "linux")
    result = native.capabilities()
    assert result["available"] is False
    assert result["local_only"] is True
    assert result["reason"]


def test_unavailable_does_not_spawn(monkeypatch):
    monkeypatch.setattr(native, "capabilities", lambda: {"available": False, "reason": "no desktop"})
    async def should_not_spawn(*args, **kwargs):
        pytest.fail("unavailable picker must not spawn a process")
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", should_not_spawn)
    with pytest.raises(native.NativeDialogUnavailable, match="no desktop"):
        asyncio.run(native.pick_folder())


def test_unicode_folder_and_shell_characters_use_json_stdin(monkeypatch, tmp_path):
    enabled(monkeypatch)
    folder = tmp_path / "中文 $() `quotes' project"
    folder.mkdir()
    helper = HelperProcess({"path": str(folder), "cancelled": False})
    calls = []
    spawn(monkeypatch, helper, calls)
    result = asyncio.run(native.pick_folder(str(folder)))
    assert result == {"path": str(folder.resolve()), "cancelled": False}
    assert json.loads(helper.input) == {"start": str(folder), "title": "选择项目文件夹",
                                        "parent_pid": 123, "parent_created": 456}
    args, options = calls[0]
    assert args == tuple(native._helper_command())
    assert args[1:5] == ("-I", "-S", "-X", "utf8")
    assert Path(args[-2]).resolve() == Path(native.__file__).resolve()
    assert "shell" not in options
    if sys.platform == "win32":
        assert options["creationflags"] == 0x08000000
    assert not helper.killed
    assert native._active_process is None


def test_cancel_is_success_and_discards_any_path(monkeypatch):
    enabled(monkeypatch)
    spawn(monkeypatch, HelperProcess({"path": "unexpected", "cancelled": True}))
    assert asyncio.run(native.pick_folder()) == {"path": None, "cancelled": True}


@pytest.mark.parametrize("result", [b"not JSON", [], {"cancelled": "yes"},
                                    {"cancelled": False, "path": "relative"},
                                    {"cancelled": False, "path": None}])
def test_invalid_helper_result_is_an_error_and_unlocks(monkeypatch, result):
    enabled(monkeypatch)
    spawn(monkeypatch, HelperProcess(result))
    with pytest.raises(native.NativeDialogError):
        asyncio.run(native.pick_folder())
    assert not native._dialog_lock.locked()
    assert native._active_process is None


def test_helper_unavailable_error_has_correct_type(monkeypatch):
    enabled(monkeypatch)
    spawn(monkeypatch, HelperProcess({"error": "headless", "unavailable": True}, returncode=1))
    with pytest.raises(native.NativeDialogUnavailable, match="headless"):
        asyncio.run(native.pick_folder())


def test_spawn_error_unlocks(monkeypatch):
    enabled(monkeypatch)
    async def failing(*args, **kwargs):
        raise OSError("fixture spawn failure")
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", failing)
    with pytest.raises(native.NativeDialogUnavailable):
        asyncio.run(native.pick_folder())
    assert not native._dialog_lock.locked()


def test_busy_refuses_second_window_and_task_cancellation_closes_first(monkeypatch):
    enabled(monkeypatch)
    async def check():
        helper = HelperProcess(wait=True)
        spawn(monkeypatch, helper)
        task = asyncio.create_task(native.pick_folder())
        await helper.started.wait()
        with pytest.raises(native.NativeDialogBusy):
            await native.pick_folder()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert helper.killed
        assert native._active_process is None
        assert not native._dialog_lock.locked()
    asyncio.run(check())


def test_timeout_closes_and_reaps_helper(monkeypatch):
    enabled(monkeypatch)
    helper = HelperProcess(wait=True)
    spawn(monkeypatch, helper)
    with pytest.raises(native.NativeDialogError, match="超时"):
        asyncio.run(native.pick_folder(timeout=0.01))
    assert helper.killed
    assert not native._dialog_lock.locked()


def test_shutdown_closes_active_window(monkeypatch):
    enabled(monkeypatch)
    async def check():
        helper = HelperProcess(wait=True)
        spawn(monkeypatch, helper)
        task = asyncio.create_task(native.pick_folder())
        await helper.started.wait()
        await native.aclose()
        with pytest.raises(native.NativeDialogError, match="意外关闭"):
            await task
        assert helper.killed
        assert native._active_process is None
    asyncio.run(check())


def test_cleanup_failure_still_clears_active_process_and_lock(monkeypatch):
    enabled(monkeypatch)
    helper = HelperProcess()
    spawn(monkeypatch, helper)
    async def cleanup_failure(process):
        assert process is helper
        raise OSError("fixture cleanup failure")
    monkeypatch.setattr(native, "_terminate", cleanup_failure)
    with pytest.raises(OSError, match="fixture cleanup failure"):
        asyncio.run(native.pick_folder())
    assert native._active_process is None
    assert not native._dialog_lock.locked()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor process cleanup")
def test_cancellation_during_real_process_creation_reaps_helper(monkeypatch):
    """Cancel while CPython owns the startup transport; stdin never reaches Show."""
    from asyncio.windows_events import _WindowsSubprocessTransport
    enabled(monkeypatch)
    original = _WindowsSubprocessTransport._connect_pipes
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        processes = []
        async def delayed_connect(transport, waiter):
            processes.append(transport._proc)
            started.set()
            await release.wait()
            await original(transport, waiter)
        monkeypatch.setattr(_WindowsSubprocessTransport, "_connect_pipes", delayed_connect)
        task = asyncio.create_task(native.pick_folder())
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert processes[0].poll() is not None
        assert native._active_process is None
        assert not native._dialog_lock.locked()
    asyncio.run(check())


@pytest.mark.parametrize("start,title", [(None, "x"), ("x\x00", "x"), ("x" * 4097, "x"),
                                       ("", ""), ("", "x\x00"), ("", "x" * 201)])
def test_invalid_parameters_rejected_before_spawn(monkeypatch, start, title):
    enabled(monkeypatch)
    with pytest.raises(native.NativeDialogError):
        asyncio.run(native.pick_folder(start, title))


def test_helper_cancel_protocol_without_show(monkeypatch):
    enabled(monkeypatch)
    dpi_calls = []
    monkeypatch.setattr(native, "_configure_helper_dpi", lambda: dpi_calls.append("configured"))
    watchdog_calls = []
    def watchdog(pid, created):
        watchdog_calls.append((pid, created))
        return lambda: watchdog_calls.append("closed")
    monkeypatch.setattr(native, "_start_parent_watchdog", watchdog)
    class FakeDialog:
        def __init__(self, start, title):
            assert dpi_calls == ["configured"]
            assert start == "C:\\fixture\\中文"
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def show(self):
            return {"path": None, "cancelled": True}
    output = io.StringIO()
    monkeypatch.setattr(native, "_WindowsFolderDialog", FakeDialog)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(
        json.dumps({"start": "C:\\fixture\\中文", "parent_pid": 123, "parent_created": 456}).encode("utf-8")), encoding="utf-8"))
    monkeypatch.setattr(sys, "stdout", output)
    assert native._helper_main() == 0
    assert json.loads(output.getvalue()) == {"path": None, "cancelled": True}
    assert watchdog_calls == [(123, 456), "closed"]


def test_helper_reports_missing_desktop_as_json(monkeypatch):
    monkeypatch.setattr(native, "_configure_helper_dpi", lambda: None)
    monkeypatch.setattr(native, "capabilities", lambda: {"available": False, "reason": "headless"})
    monkeypatch.setattr(native, "_start_parent_watchdog", lambda *_: lambda: None)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}"), encoding="utf-8"))
    monkeypatch.setattr(sys, "stdout", output)
    assert native._helper_main() == 1
    assert json.loads(output.getvalue()) == {"error": "headless", "unavailable": True}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPI helper context")
def test_real_isolated_helper_has_per_monitor_dpi_before_creating_windows():
    script = """
import ctypes, json
from code_agent.codeintel.native_dialog import _configure_helper_dpi
user = ctypes.WinDLL('user32')
user.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
before = user.GetAwarenessFromDpiAwarenessContext(user.GetThreadDpiAwarenessContext())
_configure_helper_dpi()
after = user.GetAwarenessFromDpiAwarenessContext(user.GetThreadDpiAwarenessContext())
print(json.dumps({'before': before, 'after': after}))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=15,
                            creationflags=0x08000000)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout)["after"] == 2  # DPI_AWARENESS_PER_MONITOR_AWARE


def test_real_helper_protocol_rejects_invalid_request_without_a_window():
    process = subprocess.run([sys.executable, "-m", "code_agent.codeintel.native_dialog", "--helper"],
        input=b'{"title":""}', capture_output=True, timeout=15,
        creationflags=0x08000000 if os.name == "nt" else 0)
    assert process.returncode == 1
    assert json.loads(process.stdout)["error"] == "文件夹选择窗口标题无效"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native COM fixture")
def test_real_com_configuration_without_show(tmp_path):
    """Check actual GUIDs, ABI, flags, Unicode folder and COM release without a window."""
    folder = tmp_path / "项目文件夹"
    folder.mkdir()
    with native._WindowsFolderDialog(str(folder), "测试：不会打开窗口") as dialog:
        assert dialog.options() & native._FOLDER_OPTIONS == native._FOLDER_OPTIONS
        assert dialog.options() & 0x200 == 0  # single folder only
        item, text = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            native._checked(native._com_call(dialog.pointer, 13,
                [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(item)), "GetFolder")
            native._checked(native._com_call(item, 5,
                [native._DWORD, ctypes.POINTER(ctypes.c_void_p)],
                0x80058000, ctypes.byref(text)), "GetDisplayName")
            assert native._folder_path(ctypes.wstring_at(text)) == str(folder.resolve())
        finally:
            if text.value:
                dialog.ole32.CoTaskMemFree(text)
            native._release(item)
    assert dialog.pointer.value is None
    assert dialog._initialized is False
    dialog.close()  # cleanup is idempotent


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native COM fixture")
def test_com_show_cancel_hresult_without_displaying(monkeypatch):
    original = native._com_call
    def fake_show(pointer, index, types, *args, **kwargs):
        if index == 3:
            assert args == (None,), "the isolated picker must never disable an external foreground owner"
            return ctypes.c_int32(native._CANCELLED).value
        return original(pointer, index, types, *args, **kwargs)
    monkeypatch.setattr(native, "_com_call", fake_show)
    with native._WindowsFolderDialog() as dialog:
        assert dialog.show() == {"path": None, "cancelled": True}


@pytest.mark.parametrize("pid,created", [(None, None), (0, 1), (-1, 1), (True, 1),
                                          (1, None), (1, 0), (1, True), (1, 2**64)])
def test_helper_watchdog_rejects_invalid_parent_before_opening_handle(pid, created):
    with pytest.raises(native.NativeDialogError, match="启动进程无效"):
        native._start_parent_watchdog(pid, created)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process identity")
def test_real_watchdog_refuses_a_recycled_parent_identity():
    with pytest.raises(native.NativeDialogError, match="启动进程已变化或退出"):
        native._start_parent_watchdog(os.getppid(), 1)


def _wait_fixture(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError("isolated Windows fixture did not reach its expected state")


def _helper_windows(pid):
    """Read only the test-created helper's own top-level window inventory."""
    user = ctypes.WinDLL("user32")
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    user.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(native._DWORD)]
    user.IsWindowVisible.argtypes = [ctypes.c_void_p]
    found = []
    @callback_type
    def visit(window, _):
        owner = native._DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(window):
            found.append(window)
        return 1
    user.EnumWindows(visit, None)
    return found


@pytest.fixture(scope="module")
def real_picker_owner_program(tmp_path_factory):
    if sys.platform != "win32" or os.environ.get("CODE_AGENT_REAL_NATIVE_DIALOG_TESTS") != "1":
        pytest.skip("visible native helper lifecycle regressions are explicitly opt-in")
    directory = tmp_path_factory.mktemp("picker owner fixture")
    source, executable = directory / "Owner.cs", directory / "Owner.exe"
    source.write_text(r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
class OwnerFixture {
    [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr handle);
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles();
        Form form = new Form { Text = "Code-agent picker lifetime fixture", Width = 460, Height = 170 };
        form.Controls.Add(new Label { Text = "Isolated regression window; closes automatically.", Dock = DockStyle.Fill });
        bool everDisabled = false, closed = false;
        Action report = delegate {
            bool enabled = IsWindowEnabled(form.Handle);
            everDisabled = everDisabled || !enabled;
            File.WriteAllText(args[0], "{\"handle\":" + form.Handle.ToInt64() + ",\"enabled\":" + enabled.ToString().ToLowerInvariant() + ",\"everDisabled\":" + everDisabled.ToString().ToLowerInvariant() + ",\"closed\":" + closed.ToString().ToLowerInvariant() + "}");
        };
        Stopwatch clock = Stopwatch.StartNew();
        Timer timer = new Timer { Interval = 50 };
        timer.Tick += delegate { report(); if (File.Exists(args[1]) || clock.ElapsedMilliseconds > 30000) form.Close(); };
        form.Shown += delegate { form.Activate(); report(); timer.Start(); };
        form.FormClosing += delegate { closed = true; report(); timer.Stop(); };
        Application.Run(form);
        timer.Dispose(); form.Dispose();
    }
}
''', encoding="utf-8")
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    result = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64",
                             "/reference:System.dll", "/reference:System.Windows.Forms.dll",
                             "/reference:System.Drawing.dll", f"/out:{executable}", str(source)],
                            capture_output=True, timeout=30, creationflags=0x08000000)
    assert result.returncode == 0, result.stdout.decode(errors="replace")
    return executable


@pytest.fixture
def real_picker_owner(real_picker_owner_program, tmp_path):
    state_file, close_file = tmp_path / "owner.json", tmp_path / "close-owner"
    owner = subprocess.Popen([str(real_picker_owner_program), str(state_file), str(close_file)],
                             creationflags=0x08000000)
    read = lambda: json.loads(state_file.read_text())
    initial = _wait_fixture(read)
    assert initial["enabled"] and not initial["everDisabled"]
    try:
        yield read
    finally:
        close_file.write_text("normal-close")
        owner.wait(timeout=10)
        assert owner.returncode == 0
        final = read()
        assert final["closed"], "the host's normal FormClosing path must remain usable"
        assert not final["everDisabled"], "the external foreground application must never be disabled"


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_real_helper_timeout_and_cancellation_preserve_host(real_picker_owner, tmp_path, mode):
    """Use only the repaired NULL-owner helper, never the unsafe old baseline."""
    async def check():
        pending = asyncio.create_task(native.pick_folder(str(tmp_path),
            title=f"Code-agent picker lifetime {mode}", timeout=8))
        process = None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            process = native._active_process
            if process is not None and _helper_windows(process.pid):
                break
            if pending.done():
                await pending
                raise AssertionError("native dialog ended before its real window was observed")
            await asyncio.sleep(0.05)
        assert process is not None and _helper_windows(process.pid), "real helper window must be visible"
        assert real_picker_owner()["enabled"]
        if mode == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 7)
        else:
            with pytest.raises(native.NativeDialogError, match="超时"):
                await asyncio.wait_for(pending, 12)
        assert process.returncode is not None
        assert not _helper_windows(process.pid)
        assert native._active_process is None and not native._dialog_lock.locked()
        assert real_picker_owner()["enabled"]
    asyncio.run(check())


def test_real_helper_exits_when_only_its_parent_fixture_dies(real_picker_owner, tmp_path):
    info = tmp_path / "helper.json"
    parent_script = r'''
import json, os, runpy, subprocess, sys, time
from pathlib import Path
helper = runpy.run_path(sys.argv[3], run_name="fixture_protocol")
payload = {"start": sys.argv[2], "title": "Code-agent picker lifetime parent-exit", **helper["_parent_identity"]()}
child = subprocess.Popen(helper["_helper_command"](),
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
child.stdin.write(json.dumps(payload).encode()); child.stdin.close()
Path(sys.argv[1]).write_text(json.dumps({"pid": child.pid, "parent_pid": os.getpid()}))
while True: time.sleep(1)
'''
    # A venv redirector can own a kill-on-close job and hide a missing watchdog.
    # Use the real interpreter for the fixture parent too, with no such stub.
    parent = subprocess.Popen([(getattr(sys, "_base_executable", None) or sys.executable),
                               "-I", "-S", "-X", "utf8", "-c", parent_script,
                               str(info), str(tmp_path), str(Path(native.__file__).resolve())],
                              creationflags=0x08000000)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [native._DWORD, ctypes.c_int, native._DWORD]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, native._DWORD]
    kernel.WaitForSingleObject.restype = native._DWORD
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = None
    try:
        details = _wait_fixture(lambda: json.loads(info.read_text()))
        assert details["parent_pid"] == parent.pid, "the parent fixture must not hide behind a venv redirector"
        pid = details["pid"]
        # Retain a handle to this exact test child; cleanup cannot hit a recycled PID.
        handle = kernel.OpenProcess(0x100000 | 0x1000 | 0x1, False, pid)
        assert handle
        _wait_fixture(lambda: _helper_windows(pid))
        assert real_picker_owner()["enabled"]
        parent.terminate()  # Popen's retained handle belongs only to our fixture parent.
        parent.wait(timeout=5)
        assert kernel.WaitForSingleObject(handle, 4000) == 0, "helper must self-exit after backend-like parent termination"
        _wait_fixture(lambda: not _helper_windows(pid))
        assert real_picker_owner()["enabled"]
    finally:
        if parent.poll() is None:
            parent.terminate(); parent.wait(timeout=5)
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 0x102:
                kernel.TerminateProcess(handle, 1)  # Failure cleanup: only our retained test-child handle.
                kernel.WaitForSingleObject(handle, 4000)
            kernel.CloseHandle(handle)


def test_real_http_disconnect_reaps_only_its_request_helper(real_picker_owner, tmp_path):
    """Actual local TCP abort, FastAPI disconnect, and visible helper cleanup."""
    import socket
    import httpx
    import uvicorn
    from fastapi import FastAPI
    from code_agent.api.deps import CurrentUser, get_current_user
    from code_agent.api.native_dialog_api import mount_native_dialog_api

    async def check():
        app = FastAPI()
        app.dependency_overrides[get_current_user] = lambda: CurrentUser("fixture", "fixture", "user")
        mount_native_dialog_api(app)
        @app.get("/probe")
        async def probe():
            return {"ok": True}
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                              lifespan="off", access_log=False, log_level="warning"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        pending = None
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert server.started
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                for attempt in range(2):
                    pending = asyncio.create_task(client.post("/api/v1/code/fs/pick-folder", json={"start": str(tmp_path)}))
                    process = None
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        process = native._active_process
                        if process is not None and _helper_windows(process.pid):
                            break
                        if pending.done():
                            raise AssertionError(f"picker request ended early: {(await pending).text}")
                        await asyncio.sleep(0.05)
                    assert process is not None and _helper_windows(process.pid)
                    assert (await client.get("/probe")).json() == {"ok": True}
                    if attempt:
                        await asyncio.sleep(0.25)
                        assert process.returncode is None and _helper_windows(process.pid), "the previous disconnect must not cancel this later selector"
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                    deadline = time.monotonic() + 5
                    while native._active_process is not None and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
                    assert native._active_process is None and not native._dialog_lock.locked()
                    assert process.returncode is not None and not _helper_windows(process.pid)
                    assert real_picker_owner()["enabled"]
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await native.aclose()
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
            listener.close()
    asyncio.run(check())
