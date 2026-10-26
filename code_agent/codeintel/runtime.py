"""Manually started IDE processes. Real PTYs, bounded replay, no agent execution.

Process readers use ordinary threads: stopping a process closes its output at the
OS level, then readers drain to EOF. Cancelling asyncio pipe reads on Windows
would leave overlapped I/O behind. Windows job objects also retain descendants
after their parent exits; POSIX processes have their own process group.
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import errno
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from code_agent.codeintel.paths import ProjectPathError, relative_of, safe_child
from code_agent.codeintel.interpreter import select_python
from code_agent.codeintel.scanner import walk_project
from code_agent.sandbox.executor import find_bash

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_OUTPUT_EVENTS = 1024
MAX_ACTIVE = 4
MAX_HISTORY = 100
MAX_CONFIGS = 100
MAX_MANIFEST_BYTES = 128 * 1024
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _manifest_text(path: Path) -> str | None:
    """Only small local text manifests; no build evaluation or external entities."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES or b"\0" in raw:
            return None
        return raw.decode("utf-8-sig")
    except (OSError, UnicodeError):
        return None


def _maven_goals(path: Path) -> list[str]:
    goals = ["compile", "test"]
    text = _manifest_text(path)
    if text is None or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return goals
    try:
        project = ET.fromstring(text)
    except ET.ParseError:
        return goals
    # Strip the default XML namespace, then inspect active build declarations.
    for element in project.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    for plugin in project.findall("./build/plugins/plugin"):
        if (plugin.findtext("artifactId", "").strip() == "spring-boot-maven-plugin"
                and plugin.findtext("groupId", "").strip() == "org.springframework.boot"):
            goals.insert(0, "spring-boot:run")
            break
    return goals


_GRADLE_TOKENS = re.compile(
    r'//[^\n]*|/\*.*?\*/|\"\"\".*?\"\"\"|\x27\x27\x27.*?\x27\x27\x27|'
    r'"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`[^`]*`|[A-Za-z_][A-Za-z_0-9.-]*|[{}():=;,]',
    re.DOTALL,
)


def _gradle_goals(path: Path) -> list[str]:
    goals = ["build", "test"]
    text = _manifest_text(path)
    if text is None:
        return goals
    tokens = [token for token in _GRADLE_TOKENS.findall(text) if not token.startswith(("//", "/*"))]
    plugins: set[str] = set()
    blocks: list[str] = []
    for index, token in enumerate(tokens):
        if token == "{":
            blocks.append(tokens[index - 1] if index else "")
        elif token == "}":
            if blocks:
                blocks.pop()
        elif token in ("id", "application", "`application`") and "plugins" in blocks:
            cursor = index + 1
            if token == "id":
                if cursor < len(tokens) and tokens[cursor] == "(":
                    cursor += 1
                if cursor >= len(tokens) or not tokens[cursor].startswith(("'", '"')):
                    continue
                plugin = tokens[cursor][1:-1]
                cursor += 1
            else:
                plugin = "application"
            # A root `apply false` declaration does not provide runnable tasks.
            tail = []
            for following in tokens[cursor:]:
                if following in ("id", "application", "`application`", "}", ";"):
                    break
                tail.append(following)
            if any(tail[i:i + 2] == ["apply", "false"] for i in range(len(tail) - 1)):
                continue
            plugins.add(plugin)
        elif token == "apply":
            cursor = index + 1
            if cursor < len(tokens) and tokens[cursor] == "(":
                cursor += 1
            if tokens[cursor:cursor + 2] in (["plugin", ":"], ["plugin", "="]):
                value = tokens[cursor + 2] if cursor + 2 < len(tokens) else ""
                if value.startswith(("'", '"')):
                    plugins.add(value[1:-1])
    if "application" in plugins:
        goals.insert(0, "run")
    if "org.springframework.boot" in plugins:
        goals.insert(0, "bootRun")
    return goals


class RuntimeUnavailable(RuntimeError):
    pass


class RuntimeBusy(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shells() -> dict[str, tuple[str | None, list[str]]]:
    if os.name == "nt":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        return {"powershell": (powershell, ["-NoLogo", "-NoProfile"]), "bash": (find_bash(), ["--noprofile", "--norc"])}
    return {"bash": (shutil.which("bash"), ["--noprofile", "--norc"]), "sh": (shutil.which("sh"), [])}


def terminal_capabilities() -> dict:
    shells = _shells()
    available, reason = True, ""
    if os.name == "nt":
        try:
            from winpty import PTY  # noqa: F401
            from winpty.enums import Backend  # noqa: F401
            if sys.getwindowsversion().build < 17763:
                available, reason = False, "ConPTY 需要 Windows 10 1809 或更高版本"
        except (ImportError, OSError):
            available, reason = False, "缺少 pywinpty；请安装 pywinpty>=3.0,<4"
    else:
        try:
            import pty  # noqa: F401
        except ImportError:
            available, reason = False, "当前平台没有 POSIX pty 支持"
    if not any(executable for executable, _ in shells.values()):
        available, reason = False, "未找到可用的交互式 shell"
    return {"available": available, "backend": "conpty" if os.name == "nt" else "pty", "reason": reason,
            "shells": [{"id": name, "label": "PowerShell" if name == "powershell" else name,
                        "available": bool(executable)} for name, (executable, _) in shells.items()]}


class _WindowsJob:
    """Close-owned job kills the tree, including children of an exited parent."""
    def __init__(self, pid: int):
        import ctypes
        from ctypes import wintypes as W

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", W.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", W.DWORD), ("affinity", ctypes.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("user_time", ctypes.c_longlong), ("kernel_time", ctypes.c_longlong),
                        ("period_user_time", ctypes.c_longlong), ("period_kernel_time", ctypes.c_longlong),
                        ("page_faults", W.DWORD), ("total", W.DWORD), ("active", W.DWORD), ("terminated", W.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, W.LPCWSTR], W.HANDLE
        kernel.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
        kernel.SetInformationJobObject.restype = W.BOOL
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
        kernel.AssignProcessToJobObject.argtypes, kernel.AssignProcessToJobObject.restype = [W.HANDLE, W.HANDLE], W.BOOL
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [W.HANDLE], W.BOOL
        kernel.TerminateJobObject.argtypes, kernel.TerminateJobObject.restype = [W.HANDLE, W.UINT], W.BOOL
        kernel.QueryInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = W.BOOL
        kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [W.HANDLE, W.DWORD], W.DWORD
        self.kernel, self.handle, self.lock = kernel, kernel.CreateJobObjectW(None, None), threading.Lock()
        self.accounting_type = Accounting
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = None
        try:
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            self.close()
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self):
        import ctypes
        from ctypes import wintypes as W
        with self.lock:
            if self.handle:
                # The accounting count can reach zero before process handles
                # signal termination. Retain handles before killing the job so
                # a stopped result waits for descendant cleanup, not just EOF.
                processes = []
                capacity = 64
                while capacity <= 65536:
                    class Pids(ctypes.Structure):
                        _fields_ = [("assigned", W.DWORD), ("count", W.DWORD), ("ids", ctypes.c_size_t * capacity)]
                    pids = Pids()
                    ok = self.kernel.QueryInformationJobObject(self.handle, 3, ctypes.byref(pids), ctypes.sizeof(pids), None)
                    if not ok and ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                        break
                    if pids.assigned > capacity:
                        capacity = max(capacity * 2, pids.assigned)
                        continue
                    for pid in pids.ids[:pids.count]:
                        process = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                        if process:
                            processes.append(process)
                    break
                try:
                    self.kernel.TerminateJobObject(self.handle, 1)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        info = self.accounting_type()
                        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None) or not info.active:
                            break
                        time.sleep(0.01)
                    for process in processes:
                        remaining = max(0, int((deadline - time.monotonic()) * 1000))
                        self.kernel.WaitForSingleObject(process, remaining)
                finally:
                    for process in processes:
                        self.kernel.CloseHandle(process)
                    self.kernel.CloseHandle(self.handle)
                    self.handle = None


def _resume_windows_process(pid: int) -> None:
    """Resume the primary thread only after the process belongs to our job."""
    import ctypes
    from ctypes import wintypes as W

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("id", W.DWORD), ("owner", W.DWORD),
                    ("base_priority", W.LONG), ("delta_priority", W.LONG), ("flags", W.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes, kernel.CreateToolhelp32Snapshot.restype = [W.DWORD, W.DWORD], W.HANDLE
    kernel.Thread32First.argtypes = kernel.Thread32Next.argtypes = [W.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32First.restype = kernel.Thread32Next.restype = W.BOOL
    kernel.OpenThread.argtypes, kernel.OpenThread.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
    kernel.ResumeThread.argtypes, kernel.ResumeThread.restype = [W.HANDLE], W.DWORD
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [W.HANDLE], W.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    resumed = False
    try:
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.owner == pid:
                thread = kernel.OpenThread(0x0002, False, entry.id)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    previous = kernel.ResumeThread(thread)
                    if previous == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    resumed = resumed or previous == 1
                finally:
                    kernel.CloseHandle(thread)
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    if not resumed:
        raise RuntimeUnavailable("无法恢复运行进程的启动线程")


class _ConPTY:
    """pywinpty's documented low-level API, without its extra socket thread.

    Nonblocking PTY reads allow a shell killed before its first output to reach
    EOF too. PtyProcess's internal blocking reader can wait forever in that
    specific ConPTY startup race. No pipe I/O is cancelled here.
    """
    def __init__(self, argv, cwd, env, cols, rows):
        from winpty import PTY
        from winpty.enums import Backend
        self.pty = PTY(cols, rows, backend=int(Backend.ConPTY))
        block = "\0".join(f"{key}={value}" for key, value in env.items()) + "\0"
        if not self.pty.spawn(argv[0], cmdline=" " + subprocess.list2cmdline(argv[1:]), cwd=str(cwd), env=block):
            raise RuntimeUnavailable("ConPTY 未能创建 shell")
        self.pid = self.pty.pid

    def read(self, size=16384):
        while True:
            data = self.pty.read(blocking=False)
            if data:
                return data
            if not self.pty.isalive():
                raise EOFError("ConPTY process exited")
            time.sleep(0.02)

    def isalive(self):
        return self.pty.isalive()

    @property
    def exitstatus(self):
        return self.pty.get_exitstatus()

    def write(self, data):
        return self.pty.write(data)

    def setwinsize(self, rows, cols):
        self.pty.set_size(cols, rows)

    def close(self):
        self.pty = None  # OS process has exited and the reader has drained.


@dataclass
class _Process:
    id: str
    project_id: str
    user_id: str
    category: str
    cwd: str
    command: list[str]
    name: str
    kind: str = ""
    shell: str = ""
    cols: int = 80
    rows: int = 24
    status: str = "running"
    exit_code: int | None = None
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    error: str = ""
    process: object = None
    master: int | None = None
    job: object = None
    reader: threading.Thread | None = None
    watcher: threading.Thread | None = None
    events: deque = field(default_factory=deque)
    output_bytes: int = 0
    seq: int = 0
    stop_requested: bool = False
    finished: threading.Event = field(default_factory=threading.Event)

    @property
    def pid(self):
        return self.process.pid


class IdeRuntime:
    def __init__(self, project_repo, *, max_output_bytes: int = MAX_OUTPUT_BYTES, max_output_events: int = MAX_OUTPUT_EVENTS):
        self.project_repo = project_repo
        self.max_output_bytes, self.max_output_events = max_output_bytes, max_output_events
        self._records: dict[str, _Process] = {}
        self._lock = threading.RLock()
        self._closed = False

    def owned_project(self, project_id: str, user_id: str) -> dict:
        row = self.project_repo.get(project_id)
        if not row or row["user_id"] != user_id:
            raise KeyError("项目不存在")
        return row

    def _root(self, project_id, user_id) -> Path:
        root = Path(self.owned_project(project_id, user_id)["root_path"]).resolve()
        if not root.is_dir():
            raise RuntimeBusy("项目目录不可用")
        return root

    def _cwd(self, root: Path, raw: str) -> Path:
        if len(raw) > 4096 or "\x00" in raw:
            raise ValueError("cwd 不合法")
        target = safe_child(root, raw)
        if not target.is_dir():
            raise ProjectPathError("cwd 必须是项目内已存在的目录")
        return target

    def _record(self, project_id, user_id, id, category) -> _Process:
        self.owned_project(project_id, user_id)
        with self._lock:
            record = self._records.get(id)
            if not record or record.project_id != project_id or record.user_id != user_id or record.category != category:
                raise KeyError("终端/运行不存在")
            return record

    def _append(self, record: _Process, event: dict):
        with self._lock:
            record.seq += 1
            event = {**event, "seq": record.seq}
            size = len(event.get("data", "").encode("utf-8"))
            record.events.append((event, size))
            record.output_bytes += size
            while len(record.events) > self.max_output_events or (record.output_bytes > self.max_output_bytes and len(record.events) > 1):
                _, dropped = record.events.popleft()
                record.output_bytes -= dropped

    def _output(self, record, data: str):
        # A single event must fit the buffer even for very small test buffers.
        chunk_chars = max(1, min(8192, self.max_output_bytes // 4))
        for start in range(0, len(data), chunk_chars):
            self._append(record, {"type": "output", "data": data[start:start + chunk_chars]})

    def _meta(self, record) -> dict:
        return {"id": record.id, "project_id": record.project_id, "name": record.name, "kind": record.kind,
                "shell": record.shell, "cwd": record.cwd, "command": record.command, "status": record.status,
                "pid": record.pid, "exit_code": record.exit_code, "created_at": record.created_at,
                "finished_at": record.finished_at, "error": record.error, "cols": record.cols, "rows": record.rows,
                "first_seq": record.events[0][0]["seq"] if record.events else record.seq + 1,
                "next_seq": record.seq}

    def snapshot(self, project_id, user_id, id, category, after_seq=0) -> dict:
        record = self._record(project_id, user_id, id, category)
        with self._lock:
            meta = self._meta(record)
            return {**meta, "events": [dict(e) for e, _ in record.events if e["seq"] > after_seq],
                    "truncated": after_seq < meta["first_seq"] - 1}

    def list(self, project_id, user_id, category) -> dict:
        self.owned_project(project_id, user_id)
        with self._lock:
            values = [self._meta(r) for r in self._records.values() if r.project_id == project_id and r.user_id == user_id and r.category == category]
        return {**terminal_capabilities(), "terminals": values} if category == "terminal" else {"runs": values}

    def _reserve(self, project_id, category):
        if self._closed:
            raise RuntimeBusy("运行服务已关闭")
        if sum(r.project_id == project_id and r.category == category and not r.finished.is_set() for r in self._records.values()) >= MAX_ACTIVE:
            raise RuntimeBusy(f"每个项目最多同时打开 {MAX_ACTIVE} 个终端或运行")
        completed = [r for r in self._records.values() if r.finished.is_set()]
        while len(self._records) >= MAX_HISTORY and completed:
            self._records.pop(completed.pop(0).id, None)
        if len(self._records) >= MAX_HISTORY:
            raise RuntimeBusy("运行服务进程数量达到上限")

    def create_terminal(self, project_id, user_id, *, shell="", cwd="", cols=80, rows=24) -> dict:
        root = self._root(project_id, user_id)
        capability = terminal_capabilities()
        if not capability["available"]:
            raise RuntimeUnavailable(capability["reason"])
        self._dimensions(cols, rows)
        directory = self._cwd(root, cwd)
        shell = shell or ("powershell" if os.name == "nt" else ("bash" if shutil.which("bash") else "sh"))
        selected = _shells().get(shell)
        if not selected:
            raise ValueError("shell 只支持可用列表中的名称")
        executable, args = selected
        if not executable:
            raise RuntimeUnavailable(f"未安装 {shell}")
        argv = [executable, *args]
        with self._lock:
            self._reserve(project_id, "terminal")
            record = _Process(uuid.uuid4().hex, project_id, user_id, "terminal", relative_of(root, directory), argv, shell,
                              shell=shell, cols=cols, rows=rows)
            if os.name == "nt":
                try:
                    record.process = _ConPTY(argv, directory, self._env(), cols, rows)
                    record.job = _WindowsJob(record.pid)
                except Exception as exc:
                    if record.process:
                        self._fallback_kill(record.pid)
                    raise RuntimeUnavailable(f"ConPTY 启动失败：{exc}") from exc
            else:
                import pty
                import fcntl
                import termios
                import struct
                master, slave = pty.openpty()
                try:
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                    # Opening the slave *after* setsid gives the session its
                    # controlling terminal, without unsafe Python preexec_fn.
                    launcher = [shutil.which("sh") or "/bin/sh", "-c", 'exec < "$1"; shift; exec "$@"', "ide-pty", os.ttyname(slave), *argv]
                    record.process = subprocess.Popen(launcher, cwd=directory, env=self._env(), stdin=slave, stdout=slave, stderr=slave,
                                                      start_new_session=True, close_fds=True)
                    record.master = master
                except Exception:
                    os.close(master)
                    raise
                finally:
                    os.close(slave)
            self._records[record.id] = record
            self._start_readers(record)
            return self._meta(record)

    @staticmethod
    def _env():
        return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "TERM": "xterm-256color"}

    @staticmethod
    def _dimensions(cols, rows):
        if isinstance(cols, bool) or isinstance(rows, bool) or not isinstance(cols, int) or not isinstance(rows, int) or not 2 <= cols <= 500 or not 2 <= rows <= 300:
            raise ValueError("终端大小：cols 2..500，rows 2..300")

    def input(self, project_id, user_id, id, data):
        if not isinstance(data, str) or len(data.encode("utf-8")) > 65536:
            raise ValueError("终端输入必须是不超过 64 KiB 的文本")
        record = self._record(project_id, user_id, id, "terminal")
        if record.status != "running":
            raise RuntimeBusy("终端已退出")
        if os.name == "nt":
            record.process.write(data)
        else:
            data = data.encode("utf-8")
            while data:
                data = data[os.write(record.master, data):]

    def resize(self, project_id, user_id, id, cols, rows):
        self._dimensions(cols, rows)
        record = self._record(project_id, user_id, id, "terminal")
        if record.status != "running":
            raise RuntimeBusy("终端已退出")
        if os.name == "nt":
            record.process.setwinsize(rows, cols)
        else:
            import fcntl
            import termios
            import struct
            fcntl.ioctl(record.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        with self._lock:
            record.cols, record.rows = cols, rows

    def _start_readers(self, record):
        record.reader = threading.Thread(target=self._read, args=(record,), name=f"ide-{record.id[:8]}-read", daemon=True)
        record.watcher = threading.Thread(target=self._watch, args=(record,), name=f"ide-{record.id[:8]}-wait", daemon=True)
        record.reader.start()
        record.watcher.start()

    def _wait(self, record):
        if record.category == "terminal" and os.name == "nt":
            while record.process.isalive():
                time.sleep(0.03)
            return record.process.exitstatus
        return record.process.wait()

    def _watch(self, record):
        try:
            self._wait(record)
        finally:
            # Descendants can otherwise hold stdout open after the root exits.
            if record.job:
                record.job.close()
            elif os.name != "nt":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(record.pid, signal.SIGKILL)

    def _read(self, record):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                if record.category == "terminal" and os.name == "nt":
                    try:
                        text = record.process.read(16384)
                    except EOFError:
                        break
                else:
                    try:
                        blob = os.read(record.master, 16384) if record.category == "terminal" else record.process.stdout.read1(16384)
                    except OSError as exc:
                        if record.category == "terminal" and exc.errno == errno.EIO:
                            break
                        raise
                    if not blob:
                        break
                    text = decoder.decode(blob)
                if text:
                    self._output(record, text)
            tail = decoder.decode(b"", final=True)
            if tail:
                self._output(record, tail)
        except Exception as exc:
            with self._lock:
                record.error = str(exc)
            self._kill(record)
        finally:
            code = self._wait(record)
            if record.category == "terminal" and os.name == "nt":
                if record.watcher:
                    record.watcher.join(2)
                record.process.close()
            elif record.category == "terminal":
                with contextlib.suppress(OSError):
                    os.close(record.master)
            else:
                record.process.stdout.close()
            if record.job:
                record.job.close()
            with self._lock:
                record.exit_code = code
                record.status = ("closed" if record.category == "terminal" else "stopped") if record.stop_requested else "exited"
                record.finished_at = _now()
                self._append(record, {"type": "exit", "code": code})
                record.finished.set()

    @staticmethod
    def _fallback_kill(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW, timeout=10, check=False)

    def _kill(self, record):
        if record.job:
            record.job.close()
        elif os.name == "nt":
            self._fallback_kill(record.pid)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(record.pid, signal.SIGKILL)

    def stop(self, project_id, user_id, id, category) -> dict:
        record = self._record(project_id, user_id, id, category)
        with self._lock:
            if not record.finished.is_set():
                record.stop_requested = True
        if not record.finished.is_set():
            self._kill(record)
            record.finished.wait(5)
        with self._lock:
            return self._meta(record)

    @staticmethod
    def _tools() -> dict:
        # PATH probing on Windows is costly; do it once per discovery/request.
        tools = {name: shutil.which(name) for name in ("node", "npm", "java", "javac", "mvn", "gradle")}
        if os.name == "nt":
            tools["cmd"] = os.environ.get("COMSPEC") or shutil.which("cmd")
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            for name in ("java", "javac"):
                target = Path(java_home) / "bin" / (name + (".exe" if os.name == "nt" else ""))
                if not tools[name] and target.is_file() and (os.name == "nt" or os.access(target, os.X_OK)):
                    tools[name] = str(target)
        return tools

    @staticmethod
    def _jvm_tool(root: Path, directory: Path, kind: str, tools: dict) -> tuple[str | None, str]:
        filename = ("mvnw.cmd" if os.name == "nt" else "mvnw") if kind == "maven" else (
            "gradlew.bat" if os.name == "nt" else "gradlew")
        current = directory
        while True:
            try:
                target = safe_child(root, (Path(relative_of(root, current)) / filename).as_posix())
                if target.is_file() and (os.name == "nt" or os.access(target, os.X_OK)):
                    return str(target), "wrapper"
            except (ProjectPathError, OSError):
                pass  # An escaped/non-executable wrapper is never launched.
            if current == root:
                break
            current = current.parent
        return tools["mvn" if kind == "maven" else "gradle"], "path"

    @staticmethod
    def _jvm_command(executable: str, kind: str, goal: str, tools: dict, *, directory: Path, runner: str) -> list[str]:
        args = ["-B", "-Dstyle.color=never", goal] if kind == "maven" else ["--console=plain", goal]
        if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
            shell = tools.get("cmd")
            if not shell:
                raise RuntimeUnavailable("找不到 Windows cmd.exe")
            if runner == "wrapper":
                # Wrappers are ancestors of cwd, so this is only ../ segments
                # plus a fixed filename. cmd cannot expand % in the absolute
                # project path; the OS receives cwd literally via Popen.
                executable = os.path.relpath(executable, directory)
                if not executable.startswith("."):
                    executable = ".\\" + executable
            elif "%" in executable:
                raise RuntimeUnavailable("PATH 工具脚本所在目录含 %，请使用项目 wrapper 或无 % 的工具路径")
            # Always quote the checked executable; goals/flags are fixed ASCII
            # tokens. /d blocks AutoRun, /v:off blocks !variable! expansion.
            invocation = '"' + executable + '" ' + " ".join(args)
            return [shell, "/d", "/v:off", "/s", "/c", "chcp 65001>nul 2>&1 & " + invocation]
        return [executable, *args]

    def _jvm_config(self, root: Path, directory: Path, kind: str, goal: str, manifest: Path, tools: dict) -> dict:
        executable, runner = self._jvm_tool(root, directory, kind, tools)
        reason = ""
        if not tools["java"] or not tools["javac"]:
            reason = "未找到 JDK，请配置 JAVA_HOME 或将 java/javac 加入 PATH"
        elif not executable:
            reason = f"未找到 {kind.title()}：需要项目 wrapper 或 PATH 中的 " + ("mvn" if kind == "maven" else "gradle")
        command = []
        if not reason:
            try:
                command = self._jvm_command(executable, kind, goal, tools, directory=directory, runner=runner)
            except RuntimeUnavailable as exc:
                reason = str(exc)
        cwd = relative_of(root, directory)
        return {"id": f"{kind}:{relative_of(root, manifest)}:{goal}", "name": f"{kind.title()}: {goal}" + (f" · {cwd}" if cwd else ""),
                "kind": kind, "script": goal, "cwd": cwd, "source": "project", "manifest": relative_of(root, manifest),
                "runner": runner, "available": not reason, "reason": reason, "command": command}

    def _argv(self, root: Path, directory: Path, *, kind, path="", script="", command="") -> list[str]:
        if kind == "command":
            if not command.strip() or len(command) > 8192 or "\x00" in command:
                raise ValueError("command 必须是不超过 8192 字符的非空文本")
            name = "powershell" if os.name == "nt" else "bash"
            exe, _ = _shells().get(name, (None, []))
            if not exe:
                raise RuntimeUnavailable(f"未安装 {name}")
            if os.name == "nt":
                # Windows PowerShell 5 otherwise writes redirected output in
                # its legacy console encoding. Preserve Unicode in pipe reads.
                utf8 = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);$OutputEncoding=[Console]::OutputEncoding;"
                return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", utf8 + command]
            return [exe, "-lc", command]
        if kind in {"python", "node"}:
            target = safe_child(root, path)
            suffixes = {".py", ".pyw"} if kind == "python" else {".js", ".mjs", ".cjs"}
            if not path or not target.is_file() or target.suffix.lower() not in suffixes:
                raise ValueError(f"{kind} path 必须是项目内已存在的脚本")
            executable = str(select_python(root)) if kind == "python" else shutil.which("node")
            if not executable:
                raise RuntimeUnavailable("未安装 Node.js")
            return [executable, "-u", str(target)] if kind == "python" else [executable, str(target)]
        if kind == "pytest":
            target = safe_child(root, path) if path else None
            if target and not target.exists():
                raise ValueError("pytest path 不存在")
            return [str(select_python(root)), "-u", "-m", "pytest", *([str(target)] if target else [])]
        if kind == "npm":
            data = self._package(directory / "package.json")
            scripts = data.get("scripts", {})
            if not isinstance(scripts, dict) or not script or not isinstance(scripts.get(script), str):
                raise ValueError("script 必须是 cwd/package.json 中已有的 npm script")
            node, npm = shutil.which("node"), shutil.which("npm")
            if not npm:
                raise RuntimeUnavailable("未安装 npm")
            if os.name == "nt":
                cli = Path(npm).parent / "node_modules/npm/bin/npm-cli.js"
                if not node or not cli.is_file():
                    raise RuntimeUnavailable("找不到 npm-cli.js；请检查 Node.js/npm 安装")
                return [node, str(cli), "run", "--", script]
            return [npm, "run", "--", script]
        if kind in ("maven", "gradle"):
            if path or command:
                raise ValueError("Maven/Gradle 使用 script 任务和 cwd 模块目录，不直接运行 Java 服务文件")
            filenames = ("pom.xml",) if kind == "maven" else ("build.gradle", "build.gradle.kts")
            manifest = next((safe_child(root, (Path(relative_of(root, directory)) / name).as_posix()) for name in filenames
                             if (directory / name).is_file()), None)
            if manifest is None:
                raise ValueError("cwd 没有 " + "/".join(filenames))
            allowed = _maven_goals(manifest) if kind == "maven" else _gradle_goals(manifest)
            if script not in allowed:
                raise ValueError("script 不是该模块声明的运行任务；允许：" + "、".join(allowed))
            config = self._jvm_config(root, directory, kind, script, manifest, self._tools())
            if not config["available"]:
                raise RuntimeUnavailable(config["reason"])
            return config["command"]
        raise ValueError("kind 仅支持 command、python、node、npm、pytest、maven、gradle")

    def create_run(self, project_id, user_id, *, kind, path="", script="", command="", cwd="", name="") -> dict:
        root = self._root(project_id, user_id)
        directory = self._cwd(root, cwd)
        argv = self._argv(root, directory, kind=kind, path=path, script=script, command=command)
        if len(name) > 200 or "\x00" in name:
            raise ValueError("name 不合法")
        with self._lock:
            self._reserve(project_id, "run")
            record = _Process(uuid.uuid4().hex, project_id, user_id, "run", relative_of(root, directory), argv, name or path or script or kind, kind=kind)
            try:
                launch = argv
                if os.name == "nt" and kind in ("maven", "gradle") and argv[1:5] == ["/d", "/v:off", "/s", "/c"]:
                    # cmd parses its /c tail itself, not with C-runtime argv
                    # quoting. list2cmdline would turn its quotes into \".
                    launch = subprocess.list2cmdline(argv[:5]) + " " + argv[5]
                record.process = subprocess.Popen(launch, cwd=directory, env=self._env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                  stderr=subprocess.STDOUT, creationflags=_NO_WINDOW | (0x00000004 if os.name == "nt" else 0),
                                                  start_new_session=os.name != "nt")
                if os.name == "nt":
                    record.job = _WindowsJob(record.pid)
                    _resume_windows_process(record.pid)
            except Exception as exc:
                if record.process:
                    if record.job:
                        record.job.close()
                    else:
                        self._fallback_kill(record.pid)
                    record.process.wait(timeout=10)
                    record.process.stdout.close()
                raise RuntimeUnavailable(f"运行启动失败：{exc}") from exc
            self._records[record.id] = record
            self._start_readers(record)
            return self._meta(record)

    @staticmethod
    def _package(path: Path) -> dict:
        try:
            if path.stat().st_size > 512 * 1024:
                return {}
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def run_configs(self, project_id, user_id) -> dict:
        root = self._root(project_id, user_id)
        inventory = walk_project(root)
        project_configs, file_configs = [], []
        tools = self._tools()
        pytest = False
        for entry in sorted(inventory.files, key=lambda value: value.rel):
            if entry.skipped:
                continue
            # Ordinary file configs need only inventory metadata, not a second
            # Windows realpath/PATH traversal for every source file.
            rel = Path(entry.rel)
            name, suffix = rel.name, rel.suffix.lower()
            if name in {"pytest.ini", "conftest.py"} or name.startswith("test_") and suffix == ".py":
                pytest = True
            if name in {"pyproject.toml", "setup.cfg", "tox.ini"}:
                text = _manifest_text(safe_child(root, entry.rel)) or ""
                pytest = pytest or any(marker in text for marker in ("[tool.pytest", "[pytest]", "[tool:pytest]"))
            is_test = name.startswith("test_") or name == "conftest.py" or bool(re.search(r"\.(?:test|spec)\.", name, re.IGNORECASE))
            if suffix in {".py", ".js", ".mjs", ".cjs"} and not is_test and len(file_configs) <= MAX_CONFIGS:
                kind = "python" if suffix == ".py" else "node"
                available = kind == "python" or bool(tools["node"])
                file_configs.append({"id": f"{kind}:{entry.rel}", "name": entry.rel, "kind": kind, "path": entry.rel, "cwd": "",
                                     "source": "file", "available": available, "reason": "" if available else "未安装 Node.js"})
            if name == "package.json":
                path = safe_child(root, entry.rel)
                scripts = self._package(path).get("scripts", {})
                if isinstance(scripts, dict):
                    for script, command in scripts.items():
                        if isinstance(script, str) and isinstance(command, str):
                            available = bool(tools["npm"])
                            project_configs.append({"id": f"npm:{entry.rel}:{script}", "name": f"npm: {script}", "kind": "npm", "script": script,
                                                    "cwd": rel.parent.as_posix() if rel.parent != Path(".") else "",
                                                    "source": "project", "manifest": entry.rel, "available": available,
                                                    "reason": "" if available else "未安装 npm"})
                            if len(project_configs) > MAX_CONFIGS:
                                break
            if name in {"pom.xml", "build.gradle", "build.gradle.kts"} and len(project_configs) <= MAX_CONFIGS:
                path = safe_child(root, entry.rel)
                kind = "maven" if name == "pom.xml" else "gradle"
                goals = _maven_goals(path) if kind == "maven" else _gradle_goals(path)
                for goal in goals:
                    project_configs.append(self._jvm_config(root, path.parent, kind, goal, path, tools))
        if pytest:
            import importlib.util
            python = select_python(root)
            if os.path.normcase(str(python)) == os.path.normcase(sys.executable):
                available = importlib.util.find_spec("pytest") is not None
            else:
                environment = python.parent.parent
                available = (environment / "Lib/site-packages/pytest/__init__.py").is_file() or any(environment.glob("lib/python*/site-packages/pytest/__init__.py"))
            project_configs.insert(0, {"id": "pytest", "name": "pytest", "kind": "pytest", "cwd": "", "source": "project", "available": available,
                                       "reason": "" if available else "当前 Python 未安装 pytest"})
        configs = project_configs + file_configs
        return {"configs": configs[:MAX_CONFIGS], "truncated": inventory.truncated or len(configs) > MAX_CONFIGS,
                "limits": {"configs": MAX_CONFIGS, "output_bytes": self.max_output_bytes, "output_events": self.max_output_events, "active_per_project": MAX_ACTIVE}}

    def close(self):
        with self._lock:
            self._closed = True
            records = list(self._records.values())
            for record in records:
                if not record.finished.is_set():
                    record.stop_requested = True
        for record in records:
            if not record.finished.is_set():
                self._kill(record)
        # Join ordinary readers only after the OS has terminated their tree.
        deadline = time.monotonic() + 10
        for record in records:
            for thread in (record.reader, record.watcher):
                if thread:
                    thread.join(max(0, deadline - time.monotonic()))

    async def aclose(self):
        await asyncio.to_thread(self.close)
