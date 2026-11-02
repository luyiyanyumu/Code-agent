"""Bounded Windows service identity checks without starting PowerShell/WMI.

Only exact launcher command lines on the configured listener ports are owned.
Stop retains a process handle across both listener snapshots and terminates
that handle, so a recycled PID can never inherit permission to be stopped.
This script deliberately imports no application modules or third-party code.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import json
import ntpath
import os
import socket
import sys


class CannotVerify(Exception):
    pass


def same_path(actual: str, expected: str) -> bool:
    # A bare executable or a drive-relative path is never an ownership proof.
    if not isinstance(actual, str) or not ntpath.isabs(actual) or not ntpath.splitdrive(actual)[0]:
        return False
    return ntpath.normcase(ntpath.abspath(actual).rstrip("\\")) == ntpath.normcase(ntpath.abspath(expected).rstrip("\\"))


def backend_arguments(arguments: list[str], python: str, port: int) -> bool:
    if len(arguments) < 7 or not same_path(arguments[0], python):
        return False
    index = 1
    while index < len(arguments):
        if arguments[index] in ("-I", "-B", "-u"):
            index += 1
        elif arguments[index:index + 2] == ["-X", "utf8"]:
            index += 2
        else:
            break
    tail = arguments[index:]
    if len(tail) not in (6, 8) or tail[:6] != ["-m", "code_agent.server", "--host", "127.0.0.1", "--port", str(port)]:
        return False
    return len(tail) == 6 or tail[6] == "--provider" and tail[7] in ("offline", "deepseek", "openai", "openai_compat")


def frontend_arguments(arguments: list[str], root: str, port: int, api_port: int) -> bool:
    if len(arguments) < 2 or ntpath.basename(arguments[0]).lower() not in ("node", "node.exe"):
        return False
    if same_path(arguments[1], ntpath.join(root, "npm", "launcher-preview.mjs")):
        return arguments[2:] == ["--port", str(port), "--backend-port", str(api_port)]
    return same_path(arguments[1], ntpath.join(root, "web", "node_modules", "vite", "bin", "vite.js")) and arguments[2:] == ["--host", "127.0.0.1", "--port", str(port), "--strictPort"]


def created_string(ticks: int) -> str:
    micros, remainder = divmod(ticks, 10)
    stamp = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond:06d}{remainder}Z"


class _Tcp4(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in ("state", "local_address", "local_port", "remote_address", "remote_port", "pid")]


class _Tcp6(ctypes.Structure):
    _fields_ = [("local_address", ctypes.c_ubyte * 16), ("local_scope", wintypes.DWORD),
                ("local_port", wintypes.DWORD), ("remote_address", ctypes.c_ubyte * 16),
                ("remote_scope", wintypes.DWORD), ("remote_port", wintypes.DWORD),
                ("state", wintypes.DWORD), ("pid", wintypes.DWORD)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [("length", wintypes.USHORT), ("maximum_length", wintypes.USHORT), ("buffer", ctypes.c_void_p)]


class Windows:
    QUERY = 0x1000
    TERMINATE = 0x0001
    SYNCHRONIZE = 0x100000

    def __init__(self):
        if sys.platform != "win32":
            raise CannotVerify("Windows process inspection is required.")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
        self.nt = ctypes.WinDLL("ntdll", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.ReleaseMutex.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p
        self.ip.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
        self.ip.GetExtendedTcpTable.restype = wintypes.DWORD
        self.nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        self.nt.NtQueryInformationProcess.restype = wintypes.LONG
        self.shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        self.shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)

    def listeners(self) -> list[tuple[int, int]]:
        result = []
        for family, row_type in ((2, _Tcp4), (23, _Tcp6)):
            length = wintypes.DWORD()
            status = self.ip.GetExtendedTcpTable(None, ctypes.byref(length), False, family, 3, 0)
            if status not in (0, 122):
                raise CannotVerify("Listener inspection failed.")
            for _ in range(4):
                if not 4 <= length.value <= 16 * 1024 * 1024:
                    raise CannotVerify("Listener table size is invalid.")
                buffer = ctypes.create_string_buffer(length.value)
                status = self.ip.GetExtendedTcpTable(buffer, ctypes.byref(length), False, family, 3, 0)
                if status != 122:
                    break
            if status != 0:
                raise CannotVerify("Listener inspection failed.")
            count = wintypes.DWORD.from_buffer_copy(buffer.raw[:4]).value
            row_size = ctypes.sizeof(row_type)
            if 4 + count * row_size > len(buffer):
                raise CannotVerify("Listener table is incomplete.")
            for index in range(count):
                row = row_type.from_buffer_copy(buffer, 4 + index * row_size)
                if row.state == 2:  # MIB_TCP_STATE_LISTEN
                    result.append((socket.ntohs(row.local_port & 0xFFFF), row.pid))
        return result

    @contextmanager
    def process(self, pid: int, *, terminate=False):
        flags = self.QUERY | (self.TERMINATE | self.SYNCHRONIZE if terminate else 0)
        handle = self.kernel.OpenProcess(flags, False, pid)
        if not handle:
            raise CannotVerify("Process identity is unavailable.")
        try:
            yield handle
        finally:
            self.kernel.CloseHandle(handle)

    def created(self, handle) -> str:
        values = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            raise CannotVerify("Process creation time is unavailable.")
        value = values[0]
        return created_string(value.dwLowDateTime | value.dwHighDateTime << 32)

    def arguments(self, handle) -> list[str]:
        # ProcessCommandLineInformation returns a UNICODE_STRING whose pointer
        # must refer to our local output buffer, never the target's address space.
        length = wintypes.ULONG()
        status = self.nt.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(length))
        if status not in (0, -1073741820, -1073741789, -2147483643):
            raise CannotVerify("Process command line is unavailable.")
        for _ in range(4):
            if not ctypes.sizeof(_UnicodeString) <= length.value <= 1024 * 1024:
                raise CannotVerify("Process command line size is invalid.")
            buffer = ctypes.create_string_buffer(length.value)
            status = self.nt.NtQueryInformationProcess(handle, 60, buffer, len(buffer), ctypes.byref(length))
            if status not in (-1073741820, -1073741789, -2147483643):
                break
        if status != 0:
            raise CannotVerify("Process command line is unavailable.")
        value = _UnicodeString.from_buffer_copy(buffer)
        start = ctypes.addressof(buffer)
        if value.length % 2 or not value.buffer or not start <= value.buffer <= start + len(buffer) - value.length:
            raise CannotVerify("Process command line is invalid.")
        line = ctypes.wstring_at(value.buffer, value.length // 2)
        if not line or "\x00" in line:
            raise CannotVerify("Process command line is empty or invalid.")
        count = ctypes.c_int()
        arguments = self.shell.CommandLineToArgvW(line, ctypes.byref(count))
        if not arguments:
            raise CannotVerify("Process arguments are unavailable.")
        try:
            if not 1 <= count.value <= 256:
                raise CannotVerify("Process arguments are invalid.")
            return [arguments[index] for index in range(count.value)]
        finally:
            self.kernel.LocalFree(arguments)

    def image(self, handle) -> str:
        length = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            raise CannotVerify("Process executable is unavailable.")
        return buffer.value

    @contextmanager
    def lock(self, api_port: int, web_port: int):
        mutex = self.kernel.CreateMutexW(None, False, f"Local\\CodeAgentLauncher-{api_port}-{web_port}")
        if not mutex:
            raise CannotVerify("Launcher lock is unavailable.")
        acquired = False
        try:
            acquired = self.kernel.WaitForSingleObject(mutex, 0) in (0, 0x80)
            if not acquired:
                raise CannotVerify("Another launcher is changing these services; stop deferred.")
            yield
        finally:
            if acquired:
                self.kernel.ReleaseMutex(mutex)
            self.kernel.CloseHandle(mutex)

    def terminate(self, handle):
        if not self.kernel.TerminateProcess(handle, 0):
            raise CannotVerify("The verified service could not be stopped.")


class Inspector:
    def __init__(self, windows, root, python, api_port, web_port):
        self.windows = windows
        self.root, self.python = root, python
        self.api_port, self.web_port = api_port, web_port

    def matches(self, name, arguments, image):
        # Venv redirectors may run their base Python executable; do not require
        # the image directory to be the venv. The full argv[0] must still match.
        if name == "backend":
            return ntpath.basename(image).lower() == ntpath.basename(self.python).lower() and backend_arguments(arguments, self.python, self.api_port)
        return ntpath.basename(image).lower() == "node.exe" and frontend_arguments(arguments, self.root, self.web_port, self.api_port)

    def owners(self, name, listeners):
        port = self.api_port if name == "backend" else self.web_port
        return {pid for actual_port, pid in listeners if actual_port == port}

    def inspect_service(self, name, listeners):
        owners = self.owners(name, listeners)
        if not owners:
            return {"pid": None, "created": None, "owned": False, "detail": "not-listening"}
        if len(owners) != 1:
            return {"pid": None, "created": None, "owned": False, "detail": "multiple-listener-owners"}
        pid = next(iter(owners))
        try:
            with self.windows.process(pid) as handle:
                created = self.windows.created(handle)
                owned = self.matches(name, self.windows.arguments(handle), self.windows.image(handle))
        except CannotVerify:
            return {"pid": pid, "created": None, "owned": False, "detail": "identity-unavailable"}
        return {"pid": pid, "created": created, "owned": owned, "detail": "verified-workspace-process" if owned else "unrecognized-process"}

    def inspect(self):
        listeners = self.windows.listeners()
        return {name: self.inspect_service(name, listeners) for name in ("backend", "frontend")}

    def stop(self, name, expected_pid, expected_created):
        if expected_pid <= 0 or not expected_created:
            raise CannotVerify("A verified process identity is required.")
        with self.windows.lock(self.api_port, self.web_port):
            # Retain the same kernel object, rather than doing a final PID-only
            # lookup after the identity checks. Other services are never touched.
            with self.windows.process(expected_pid, terminate=True) as handle:
                for _ in range(2):
                    if (self.owners(name, self.windows.listeners()) != {expected_pid}
                            or self.windows.created(handle) != expected_created
                            or not self.matches(name, self.windows.arguments(handle), self.windows.image(handle))):
                        raise CannotVerify("The service identity changed; stop refused.")
                # A process can change its command line after startup; recheck
                # immediately before using the retained termination handle.
                if self.windows.created(handle) != expected_created or not self.matches(name, self.windows.arguments(handle), self.windows.image(handle)):
                    raise CannotVerify("The service identity changed; stop refused.")
                self.windows.terminate(handle)
        return {"stopped": True, "service": name, "pid": expected_pid, "created": expected_created}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("-Action", choices=("Inspect", "Stop"), default="Inspect")
    parser.add_argument("-Root", required=True)
    parser.add_argument("-BackendPython", required=True)
    parser.add_argument("-BackendPort", type=int, default=8765)
    parser.add_argument("-FrontendPort", type=int, default=5175)
    parser.add_argument("-Service", choices=("backend", "frontend"), default="backend")
    parser.add_argument("-ExpectedPid", type=int, default=0)
    parser.add_argument("-ExpectedCreated", default="")
    options = parser.parse_args(argv)
    try:
        if not 1 <= options.BackendPort <= 65535 or not 1 <= options.FrontendPort <= 65535 or options.BackendPort == options.FrontendPort:
            raise CannotVerify("Backend and frontend ports must be valid and differ.")
        inspector = Inspector(Windows(), os.path.abspath(options.Root), os.path.abspath(options.BackendPython), options.BackendPort, options.FrontendPort)
        result = inspector.inspect() if options.Action == "Inspect" else inspector.stop(options.Service, options.ExpectedPid, options.ExpectedCreated)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    except (CannotVerify, OSError, ValueError):
        # Never emit command lines, credentials, exception paths, or arbitrary
        # operating-system error strings to the launcher log.
        print(json.dumps({"error": "Service identity could not be verified; no unverified process was stopped."}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
