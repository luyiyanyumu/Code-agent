"""Native folder-picker protocol and COM setup; never opens a visible window."""

import asyncio
import ctypes
import io
import json
import os
import subprocess
import sys

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
    assert json.loads(helper.input) == {"start": str(folder), "title": "选择项目文件夹"}
    args, options = calls[0]
    assert args == (sys.executable, "-m", "code_agent.codeintel.native_dialog", "--helper")
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
    class FakeDialog:
        def __init__(self, start, title):
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
        json.dumps({"start": "C:\\fixture\\中文"}).encode("utf-8")), encoding="utf-8"))
    monkeypatch.setattr(sys, "stdout", output)
    assert native._helper_main() == 0
    assert json.loads(output.getvalue()) == {"path": None, "cancelled": True}


def test_helper_reports_missing_desktop_as_json(monkeypatch):
    monkeypatch.setattr(native, "capabilities", lambda: {"available": False, "reason": "headless"})
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}"), encoding="utf-8"))
    monkeypatch.setattr(sys, "stdout", output)
    assert native._helper_main() == 1
    assert json.loads(output.getvalue()) == {"error": "headless", "unavailable": True}


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
            return ctypes.c_int32(native._CANCELLED).value
        return original(pointer, index, types, *args, **kwargs)
    monkeypatch.setattr(native, "_com_call", fake_show)
    with native._WindowsFolderDialog() as dialog:
        assert dialog.show() == {"path": None, "cancelled": True}
