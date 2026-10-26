"""Windows Explorer's native folder picker, isolated from the API event loop.

The helper owns its COM STA and window. The server passes JSON through stdin,
never shell commands, and can terminate the helper on cancellation/shutdown.
No window is created by :func:`capabilities` or by importing this module.

Interface layouts/GUIDs follow Microsoft's Win32 SDK ``ShObjIdl_core.h``.
IFileOpenDialog with FOS_PICKFOLDERS is the modern Explorer dialog, including
the address bar, navigation sidebar, search and new-folder controls.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any


class NativeDialogError(RuntimeError):
    """The local operating system could not complete the selection."""


class NativeDialogUnavailable(NativeDialogError):
    """There is no supported interactive desktop on this server."""


class NativeDialogBusy(NativeDialogError):
    """A folder picker is already open on this server's desktop."""


_dialog_lock = threading.Lock()
_active_process: asyncio.subprocess.Process | None = None
_HRESULT = ctypes.c_int32
_DWORD = ctypes.c_uint32
_CANCELLED = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
_FOLDER_OPTIONS = 0x20 | 0x40 | 0x800 | 0x8  # PICKFOLDERS, FORCEFILESYSTEM, PATHMUSTEXIST, NOCHANGEDIR
_MAX_INPUT = 16_384


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value: str) -> _GUID:
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


_CLSID_FILE_OPEN_DIALOG = _GUID.parse("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
_IID_FILE_OPEN_DIALOG = _GUID.parse("D57C7288-D4AD-4768-BE02-9D969532D960")
_IID_SHELL_ITEM = _GUID.parse("43826D1E-E718-42EE-BC55-A1E261C37BFE")


def _interactive_desktop() -> bool:
    """Checking desktop access does not display or activate a window."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.argtypes = [_DWORD, ctypes.c_int, _DWORD]
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    user32.CloseDesktop.restype = ctypes.c_int
    desktop = user32.OpenInputDesktop(0, False, 0x0001 | 0x0002)
    if not desktop:
        return False
    user32.CloseDesktop(desktop)
    return True


def capabilities() -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "platform": sys.platform,
                              "kind": "windows-file-open-dialog", "local_only": True}
    if sys.platform != "win32":
        result["reason"] = "当前服务未运行在 Windows 桌面，无法打开系统文件夹选择窗口"
        return result
    try:
        if not _interactive_desktop():
            result["reason"] = "当前服务没有可用的 Windows 交互桌面"
            return result
        ctypes.WinDLL("ole32")
        ctypes.WinDLL("shell32")
    except (OSError, AttributeError):
        result["reason"] = "Windows 系统文件夹选择组件不可用"
        return result
    result["available"] = True
    return result


def _checked(hr: int, operation: str) -> None:
    if ctypes.c_int32(hr).value < 0:
        raise NativeDialogError(f"系统文件夹选择失败（{operation}: 0x{hr & 0xFFFFFFFF:08X}）")


def _com_call(pointer: ctypes.c_void_p, index: int, argtypes: list[Any], *args: Any,
              restype: Any = _HRESULT) -> int:
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])
    return function(pointer, *args)


def _release(pointer: ctypes.c_void_p) -> None:
    if pointer.value:
        _com_call(pointer, 2, [], restype=_DWORD)
        pointer.value = None


class _WindowsFolderDialog:
    """Used only on the helper's main STA thread; constructing never calls Show."""

    def __init__(self, start: str = "", title: str = "选择项目文件夹"):
        if sys.platform != "win32":
            raise NativeDialogUnavailable("系统文件夹选择仅支持 Windows 桌面")
        self.pointer = ctypes.c_void_p()
        self._initialized = False
        self.ole32 = ctypes.WinDLL("ole32")
        self.shell32 = ctypes.WinDLL("shell32")
        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, _DWORD]
        self.ole32.CoInitializeEx.restype = _HRESULT
        self.ole32.CoUninitialize.argtypes = []
        self.ole32.CoUninitialize.restype = None
        self.ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p,
                                               _DWORD, ctypes.POINTER(_GUID),
                                               ctypes.POINTER(ctypes.c_void_p)]
        self.ole32.CoCreateInstance.restype = _HRESULT
        self.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole32.CoTaskMemFree.restype = None
        self.shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                            ctypes.POINTER(_GUID),
                                                            ctypes.POINTER(ctypes.c_void_p)]
        self.shell32.SHCreateItemFromParsingName.restype = _HRESULT
        try:
            _checked(self.ole32.CoInitializeEx(None, 0x2 | 0x4), "CoInitializeEx")
            self._initialized = True  # S_FALSE also requires matching CoUninitialize.
            _checked(self.ole32.CoCreateInstance(ctypes.byref(_CLSID_FILE_OPEN_DIALOG), None,
                                                 1, ctypes.byref(_IID_FILE_OPEN_DIALOG),
                                                 ctypes.byref(self.pointer)), "CoCreateInstance")
            options = self.options()
            # Preserve the system defaults, but always return one real filesystem folder.
            options = (options | _FOLDER_OPTIONS) & ~0x200 & ~0x80
            _checked(_com_call(self.pointer, 9, [_DWORD], options), "SetOptions")
            _checked(_com_call(self.pointer, 17, [ctypes.c_wchar_p], title), "SetTitle")
            _checked(_com_call(self.pointer, 18, [ctypes.c_wchar_p], "选择文件夹"), "SetOkButtonLabel")
            if start:
                folder = Path(start).expanduser()
                if folder.is_file():
                    folder = folder.parent
                if folder.is_dir():
                    shell_item = ctypes.c_void_p()
                    try:
                        _checked(self.shell32.SHCreateItemFromParsingName(
                            str(folder.resolve()), None, ctypes.byref(_IID_SHELL_ITEM),
                            ctypes.byref(shell_item)), "SHCreateItemFromParsingName")
                        # An explicit caller path should be used; a blank path retains Explorer's MRU.
                        _checked(_com_call(self.pointer, 12, [ctypes.c_void_p], shell_item), "SetFolder")
                    finally:
                        _release(shell_item)
        except BaseException:
            self.close()
            raise

    def options(self) -> int:
        options = _DWORD()
        _checked(_com_call(self.pointer, 10, [ctypes.POINTER(_DWORD)], ctypes.byref(options)), "GetOptions")
        return options.value

    def show(self) -> dict[str, Any]:
        # This is called only after a user's explicit request. No automatic focus stealing.
        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hr = _com_call(self.pointer, 3, [ctypes.c_void_p], user32.GetForegroundWindow())
        if (hr & 0xFFFFFFFF) == _CANCELLED:
            return {"path": None, "cancelled": True}
        _checked(hr, "Show")
        item, text = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            _checked(_com_call(self.pointer, 20, [ctypes.POINTER(ctypes.c_void_p)],
                               ctypes.byref(item)), "GetResult")
            _checked(_com_call(item, 5, [_DWORD, ctypes.POINTER(ctypes.c_void_p)],
                               0x80058000, ctypes.byref(text)), "GetDisplayName")  # SIGDN_FILESYSPATH
            if not text.value:
                raise NativeDialogError("系统没有返回所选文件夹路径")
            return {"path": _folder_path(ctypes.wstring_at(text)), "cancelled": False}
        finally:
            if text.value:
                self.ole32.CoTaskMemFree(text)
            _release(item)

    def close(self) -> None:
        try:
            _release(self.pointer)
        finally:
            if self._initialized:
                self._initialized = False
                self.ole32.CoUninitialize()

    def __enter__(self) -> _WindowsFolderDialog:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _folder_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise NativeDialogError("系统没有返回有效的文件夹路径")
    path = Path(value)
    try:
        if not path.is_absolute() or not path.is_dir():
            raise NativeDialogError("所选文件夹不存在或不是绝对路径")
        return str(path.resolve(strict=True))
    except OSError as exc:
        raise NativeDialogError("所选文件夹已不存在或无法访问") from exc


def _arguments(start: str, title: str) -> tuple[str, str]:
    if not isinstance(start, str) or len(start) > 4096 or "\x00" in start:
        raise NativeDialogError("初始文件夹路径无效")
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or "\x00" in title:
        raise NativeDialogError("文件夹选择窗口标题无效")
    return start.strip(), title.strip()


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def aclose() -> None:
    """Close and reap the helper/window during API shutdown."""
    if _active_process is not None:
        await _terminate(_active_process)


cancel_active = aclose


async def pick_folder(start: str = "", title: str = "选择项目文件夹", *,
                      timeout: float = 300) -> dict[str, Any]:
    global _active_process
    start, title = _arguments(start, title)
    available = capabilities()
    if not available["available"]:
        raise NativeDialogUnavailable(available.get("reason", "系统文件夹选择不可用"))
    if not _dialog_lock.acquire(blocking=False):
        raise NativeDialogBusy("文件夹选择窗口已打开，请完成或取消当前选择")
    process: asyncio.subprocess.Process | None = None
    try:
        # CREATE_NO_WINDOW hides only the helper's console; Explorer's picker stays visible.
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "code_agent.codeintel.native_dialog", "--helper",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
        _active_process = process
        data = json.dumps({"start": start, "title": title}, ensure_ascii=False).encode("utf-8")
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(data), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise NativeDialogError("选择文件夹超时，请重新打开选择窗口") from exc
        if len(stdout) > _MAX_INPUT:
            raise NativeDialogError("系统文件夹选择返回了无效结果")
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise NativeDialogError("系统文件夹选择窗口意外关闭，请重试") from exc
        if not isinstance(result, dict):
            raise NativeDialogError("系统文件夹选择返回了无效结果")
        if result.get("error"):
            kind = NativeDialogUnavailable if result.get("unavailable") else NativeDialogError
            raise kind(str(result["error"]))
        if process.returncode:
            raise NativeDialogError("系统文件夹选择窗口意外关闭，请重试")
        if result.get("cancelled") is True:
            return {"path": None, "cancelled": True}
        if result.get("cancelled") is not False:
            raise NativeDialogError("系统文件夹选择返回了无效结果")
        return {"path": _folder_path(result.get("path")), "cancelled": False}
    except OSError as exc:
        raise NativeDialogUnavailable("无法启动系统文件夹选择窗口") from exc
    finally:
        try:
            if process is not None:
                await _terminate(process)
        finally:
            if _active_process is process:
                _active_process = None
            _dialog_lock.release()


def _helper_main() -> int:
    try:
        payload = sys.stdin.buffer.read(_MAX_INPUT + 1)
        if len(payload) > _MAX_INPUT:
            raise NativeDialogError("文件夹选择参数过长")
        body = json.loads(payload.decode("utf-8"))
        if not isinstance(body, dict):
            raise NativeDialogError("文件夹选择参数无效")
        start, title = _arguments(body.get("start", ""), body.get("title", "选择项目文件夹"))
        available = capabilities()
        if not available["available"]:
            raise NativeDialogUnavailable(available.get("reason", "系统文件夹选择不可用"))
        with _WindowsFolderDialog(start, title) as dialog:
            result = dialog.show()
    except NativeDialogError as exc:
        result = {"error": str(exc), "unavailable": isinstance(exc, NativeDialogUnavailable)}
    except Exception:
        result = {"error": "系统文件夹选择窗口无法打开，请重试", "unavailable": False}
    # ASCII JSON survives Windows' console encodings; the receiving process decodes UTF-8.
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.flush()
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--helper"]:
        raise SystemExit("This module is an internal folder-picker helper.")
    raise SystemExit(_helper_main())
