"""Project-scoped debugging through DAP adapters.

Each launch owns its adapter and debuggee. No TCP debugging port is exposed and
variable/frame references only live for their current suspended generation.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib.metadata
import importlib.util
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from code_agent.codeintel.paths import ProjectPathError, safe_child
from code_agent.codeintel.interpreter import interpreter_info, select_python


class DebugError(RuntimeError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def debug_meta() -> dict:
    available = importlib.util.find_spec("debugpy") is not None
    try:
        version = importlib.metadata.version("debugpy") if available else None
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {
        "available": available, "adapter": "debugpy", "version": version,
        "languages": ["python"] if available else [], "extensions": [".py"] if available else [],
        "install_hint": None if available else "在后端 Python 环境运行：python -m pip install debugpy",
        "limitations": ["当前支持 Python 文件启动调试；Vue/JavaScript 调试尚未接入。", "优先使用项目 .venv，缺失时使用后端 Python；控制台不支持交互输入。"],
    }


def _python_file(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ProjectPathError("请选择项目内的 Python 文件")
    path = safe_child(root, raw)
    if not path.is_file():
        raise ProjectPathError(f"调试文件不存在：{raw}")
    if path.suffix.lower() != ".py":
        raise ProjectPathError("当前仅支持 Python（.py）文件调试")
    return path


def _breakpoints(root: Path, items: list, *, language: str = "python") -> dict[str, list[int]]:
    if not isinstance(items, list) or len(items) > 300:
        raise ProjectPathError("断点必须是数组，最多 300 个")
    groups: dict[str, list[int]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ProjectPathError("断点必须包含 path 和 line")
        if language == "java":
            from code_agent.codeintel.java_debug import java_file
            path = java_file(root, item.get("path"))
        else:
            path = _python_file(root, item.get("path"))
        line = item.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= 10_000_000:
            raise ProjectPathError("断点行号必须是正整数")
        groups.setdefault(str(path), []).append(line)
    return {path: sorted(set(lines)) for path, lines in groups.items()}


class DebugSession:
    def __init__(self, owner: str, project_id: str, root: Path, program: Path, cwd: Path, args: list[str]):
        self.id = uuid.uuid4().hex
        self.owner, self.project_id, self.root = owner, project_id, root
        self.program, self.cwd, self.args = program, cwd, args
        self.python = select_python(root)
        self.process: asyncio.subprocess.Process | None = None
        self._dap_reader: asyncio.StreamReader | None = None
        self._dap_writer: asyncio.StreamWriter | None = None
        self.debuggee_pid: int | None = None
        self._debuggee_pidfd: int | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self._seq = 0
        self._generation = 0
        self._references: set[int] = set()
        self._breakpoint_files: set[str] = set()
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._inspection: asyncio.Task | None = None
        self._cleanup: asyncio.Task | None = None
        self._initialized = asyncio.Event()
        self._lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._closing = False
        self.state: dict[str, Any] = {
            "id": self.id, "project_id": project_id, "path": program.relative_to(root).as_posix(),
            "status": "starting", "reason": None, "thread_id": None, "threads": [],
            "stack": [], "scopes": [], "variables": [], "frame_id": None,
            "breakpoints": [], "console": [], "console_seq": 0, "seq": 0,
            "pause_seq": 0, "exit_code": None, "error": None, "created_at": time.time(),
            **interpreter_info(root),
            "language": "python", "adapter": "debugpy", "request": "launch",
        }

    def _touch(self):
        self.state["seq"] += 1

    def snapshot(self, after_console: int = 0) -> dict:
        out = copy.deepcopy(self.state)
        out["console"] = [item for item in out["console"] if item["seq"] > after_console]
        return out

    def _output(self, category: str, output: str):
        if not output or category == "telemetry":
            return
        self.state["console_seq"] += 1
        self.state["console"].append({"seq": self.state["console_seq"], "category": category, "output": output[:32_000]})
        self.state["console"] = self.state["console"][-500:]
        self._touch()

    def _invalidate(self):
        self._generation += 1
        self._references.clear()
        self.state.update(stack=[], scopes=[], variables=[], frame_id=None)
        if self._inspection and not self._inspection.done():
            self._inspection.cancel()

    async def _write(self, message: dict):
        writer = self._dap_writer or (self.process.stdin if self.process else None)
        if not writer or (self.process and self.process.returncode is not None):
            raise DebugError("调试进程已经结束")
        blob = json.dumps(message, ensure_ascii=False).encode("utf-8")
        writer.write(f"Content-Length: {len(blob)}\r\n\r\n".encode("ascii") + blob)
        await writer.drain()

    async def request(self, command: str, arguments: dict | None = None, timeout: float = 12) -> dict:
        self._seq += 1
        seq = self._seq
        future = asyncio.get_running_loop().create_future()
        self.pending[seq] = future
        try:
            await self._write({"seq": seq, "type": "request", "command": command, "arguments": arguments or {}})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise DebugError(f"调试器请求超时：{command}", 504) from exc
        finally:
            self.pending.pop(seq, None)

    async def _read(self):
        reader = self._dap_reader or (self.process.stdout if self.process else None)
        assert reader
        try:
            while True:
                header = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:"))
                if not 0 < length <= 8_000_000:
                    raise DebugError("调试器返回了无效消息长度")
                message = json.loads(await reader.readexactly(length))
                if message.get("type") == "response":
                    future = self.pending.get(message.get("request_seq"))
                    if future and not future.done():
                        if message.get("success"):
                            future.set_result(message.get("body") or {})
                        else:
                            future.set_exception(DebugError(message.get("message") or "调试器拒绝请求"))
                elif message.get("type") == "event":
                    self._event(message.get("event"), message.get("body") or {})
                elif message.get("type") == "request":
                    await self._handle_client_request(message)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            if self.state["status"] not in {"stopped", "error"} and not self._closing:
                self.state.update(status="error", error="调试器连接意外关闭")
                self._touch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self.state.update(status="error", error=str(exc))
                self._touch()
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(DebugError("调试器连接已关闭"))
            if not self._closing and self.state["status"] == "error":
                self._cleanup = asyncio.create_task(self.stop())

    async def _handle_client_request(self, message: dict):
        # Python internalConsole never asks the browser to spawn a terminal.
        self._seq += 1
        await self._write({"seq": self._seq, "type": "response", "request_seq": message["seq"], "command": message["command"], "success": False, "message": "Unsupported client request"})

    async def _read_stderr(self):
        assert self.process and self.process.stderr
        while blob := await self.process.stderr.read(4096):
            self._output("stderr", blob.decode("utf-8", errors="replace"))

    def _event(self, event: str, body: dict):
        if event == "initialized":
            self._initialized.set()
        elif event == "process":
            self.debuggee_pid = body.get("systemProcessId")
            if os.name != "nt" and hasattr(os, "pidfd_open") and self.debuggee_pid:
                with contextlib.suppress(OSError):
                    self._debuggee_pidfd = os.pidfd_open(self.debuggee_pid)
        elif event == "output":
            self._output(body.get("category", "console"), body.get("output", ""))
        elif event == "stopped":
            self._invalidate()
            self.state.update(status="paused", reason=body.get("reason"), thread_id=body.get("threadId"), pause_seq=self._generation)
            self._touch()
            self._inspection = asyncio.create_task(self.inspect(self._generation))
        elif event == "continued":
            self._invalidate()
            self.state.update(status="running", reason=None)
            self._touch()
        elif event == "thread":
            self._touch()
        elif event == "exited":
            self.state["exit_code"] = body.get("exitCode")
            self._touch()
        elif event == "terminated":
            self._invalidate()
            self.state.update(status="stopped", reason="terminated")
            self._touch()
            if not self._closing:
                self._cleanup = asyncio.create_task(self.stop())
        elif event == "breakpoint":
            breakpoint = body.get("breakpoint") or {}
            for item in self.state["breakpoints"]:
                if item.get("id") == breakpoint.get("id"):
                    item.update({key: breakpoint[key] for key in ("verified", "line", "message") if key in breakpoint})
            self._touch()

    async def start(self, breakpoints: dict[str, list[int]]):
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
        launch: asyncio.Task | None = None
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "debugpy.adapter", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs,
            )
            self._reader = asyncio.create_task(self._read())
            self._stderr = asyncio.create_task(self._read_stderr())
            await self.request("initialize", {"adapterID": "debugpy", "clientID": "code-agent", "clientName": "Code Agent", "linesStartAt1": True, "columnsStartAt1": True, "pathFormat": "path", "supportsVariableType": True, "supportsRunInTerminalRequest": False})
            launch = asyncio.create_task(self.request("launch", {
                "name": self.program.name, "type": "debugpy", "request": "launch",
                "python": [str(self.python)], "program": str(self.program), "cwd": str(self.cwd), "args": self.args,
                "console": "internalConsole", "redirectOutput": True, "stopOnEntry": True,
                "justMyCode": True, "subProcess": False,
            }, timeout=25))
            # launch response follows configurationDone; awaiting it earlier deadlocks DAP.
            initialized = asyncio.create_task(self._initialized.wait())
            try:
                done, _ = await asyncio.wait({initialized, launch}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
                if launch in done:
                    await launch  # Surface launch errors instead of an unrelated timeout.
                if not self._initialized.is_set():
                    raise DebugError("Python 调试器启动超时", 504)
            finally:
                initialized.cancel()
                await asyncio.gather(initialized, return_exceptions=True)
            await self.set_breakpoints(breakpoints)
            await self.request("setExceptionBreakpoints", {"filters": []})
            await self.request("configurationDone")
            await launch
            if self.state["status"] == "starting":
                self.state["status"] = "running"
                self._touch()
        except BaseException as exc:
            if isinstance(exc, Exception):
                self.state.update(status="error", error=str(exc))
            if launch and not launch.done():
                launch.cancel()
            if launch:
                await asyncio.gather(launch, return_exceptions=True)
            await self.stop()
            raise

    async def set_breakpoints(self, groups: dict[str, list[int]]) -> list[dict]:
        result = []
        for path in sorted(self._breakpoint_files | set(groups)):
            lines = groups.get(path, [])
            body = await self.request("setBreakpoints", {"source": {"path": path}, "breakpoints": [{"line": line} for line in lines]})
            relative = Path(path).relative_to(self.root).as_posix()
            for index, bp in enumerate(body.get("breakpoints", [])):
                result.append({"path": relative, "requested_line": lines[index] if index < len(lines) else bp.get("line"), **{key: bp[key] for key in ("id", "verified", "line", "message") if key in bp}})
        self._breakpoint_files = set(groups)
        self.state["breakpoints"] = result
        self._touch()
        return result

    def _source_path(self, source: dict) -> str | None:
        try:
            return Path(source.get("path") or "").resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return None

    async def inspect(self, generation: int, frame_id: int | None = None):
        try:
            threads = (await self.request("threads")).get("threads", [])
            thread_id = self.state["thread_id"] or (threads[0]["id"] if threads else None)
            if thread_id is None:
                return
            frames = (await self.request("stackTrace", {"threadId": thread_id, "startFrame": 0, "levels": 50})).get("stackFrames", [])
            stack = [{"id": f["id"], "name": f.get("name", ""), "line": f.get("line", 1), "column": f.get("column", 1), "path": self._source_path(f.get("source") or {}), "source_name": (f.get("source") or {}).get("name", "")} for f in frames]
            if frame_id is not None and frame_id not in {f["id"] for f in frames}:
                raise DebugError("调用栈帧已失效，请刷新暂停状态")
            chosen = frame_id if frame_id is not None else (frames[0]["id"] if frames else None)
            scopes = (await self.request("scopes", {"frameId": chosen})).get("scopes", []) if chosen is not None else []
            # Cheap scopes (locals) are loaded immediately; globals can be expanded on demand.
            variables = []
            refs = {s["variablesReference"] for s in scopes if s.get("variablesReference")}
            for scope in scopes:
                if scope.get("expensive") or scope.get("presentationHint") == "globals" or scope.get("name", "").lower() == "globals":
                    continue
                values = (await self.request("variables", {"variablesReference": scope["variablesReference"], "start": 0, "count": 100})).get("variables", [])[:100]
                for variable in values:
                    variable["scope"] = scope["name"]
                    variable["value"] = str(variable.get("value", ""))[:4000]
                    variables.append(variable)
                    if variable.get("variablesReference"):
                        refs.add(variable["variablesReference"])
            if generation == self._generation and self.state["status"] == "paused":
                self._references = refs
                self.state.update(threads=threads, thread_id=thread_id, stack=stack, scopes=scopes, variables=variables, frame_id=chosen)
                self._touch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if generation == self._generation and self.state["status"] == "paused":
                self.state["error"] = str(exc)
                self._touch()

    async def variables(self, reference: int, pause_seq: int | None = None) -> dict:
        generation = self._generation
        if self.state["status"] != "paused" or reference not in self._references or (pause_seq is not None and pause_seq != generation):
            raise DebugError("变量引用已失效，请在当前暂停位置重新展开")
        values = (await self.request("variables", {"variablesReference": reference, "start": 0, "count": 100})).get("variables", [])[:100]
        if generation != self._generation or self.state["status"] != "paused":
            raise DebugError("程序已继续运行，变量引用已失效")
        for item in values:
            item["value"] = str(item.get("value", ""))[:4000]
            if item.get("variablesReference"):
                self._references.add(item["variablesReference"])
        return {"reference": reference, "pause_seq": generation, "variables": values}

    async def action(self, action: str, thread_id: int | None = None):
        if action == "stop":
            await self.stop()
            return
        if action not in {"continue", "next", "stepIn", "stepOut", "pause"}:
            raise DebugError("不支持的调试操作", 422)
        async with self._lock:
            expected = "running" if action == "pause" else "paused"
            if self.state["status"] != expected:
                raise DebugError("请先运行程序" if action == "pause" else "程序暂停后才能继续或逐步执行")
            if self._inspection and not self._inspection.done():
                await asyncio.gather(self._inspection, return_exceptions=True)
            if action == "pause":
                threads = (await self.request("threads")).get("threads", [])
                self.state["threads"] = threads
            ids = {t["id"] for t in self.state["threads"]}
            chosen = thread_id or self.state["thread_id"] or (next(iter(ids)) if ids else None)
            if chosen is None or chosen not in ids:
                raise DebugError("调试线程不可用")
            generation = self._generation
            await self.request(action, {"threadId": chosen})
            # A successful step/continue can respond before its continued event. Clear
            # stale paused UI immediately, but preserve a new stop that already arrived.
            if action != "pause" and self._generation == generation and self.state["status"] == "paused":
                self._invalidate()
                self.state.update(status="running", reason=None)
                self._touch()

    async def _kill_adapter(self):
        if not self.process or self.process.returncode is not None:
            return
        if os.name == "nt":
            with contextlib.suppress(OSError):
                killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(self.process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=0x08000000)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(killer.wait(), 5)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.process.wait(), 5)

    async def stop(self):
        async with self._stop_lock:
            if self._closing:
                return
            self._closing = True
            self._invalidate()
            if self.process and self.process.returncode is None:
                with contextlib.suppress(Exception):
                    await self.request("disconnect", {"terminateDebuggee": True}, timeout=4)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.process.wait(), 4)
                await self._kill_adapter()
            # debugpy gives POSIX debuggees their own process group. If an adapter or
            # launcher crashed, its group alone may not contain the debuggee. A pidfd
            # pins the owned process identity, so cleanup cannot target a reused PID.
            if self._debuggee_pidfd is not None:
                try:
                    with contextlib.suppress(ProcessLookupError):
                        signal.pidfd_send_signal(self._debuggee_pidfd, 0)
                        os.killpg(self.debuggee_pid, signal.SIGKILL)
                finally:
                    os.close(self._debuggee_pidfd)
                    self._debuggee_pidfd = None
            for task in (self._reader, self._stderr, self._inspection):
                if task and task is not asyncio.current_task() and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (self._reader, self._stderr, self._inspection) if task and task is not asyncio.current_task()), return_exceptions=True)
            if self.state["status"] != "error":
                self.state.update(status="stopped", reason=self.state["reason"] or "stopped")
            self._touch()


class DebugRuntime:
    def __init__(self, *, java_provider=None):
        self.sessions: dict[str, DebugSession] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.java_provider = java_provider

    def meta(self, root: str | Path | None = None) -> dict:
        python = debug_meta()
        if self.java_provider is not None:
            java = self.java_provider.meta()
        else:
            from code_agent.codeintel.java_debug import _INSTALL_HINT
            java = {"available": False, "adapter": "java-debug", "install_hint": _INSTALL_HINT,
                    "limitations": ["Java 调试后端尚未连接 JDT LS。"]}
        languages = (["python"] if python["available"] else []) + (["java"] if java["available"] else [])
        return {**python, "available": bool(languages), "languages": languages,
                "extensions": ([".py"] if python["available"] else []) + ([".java"] if java["available"] else []),
                "adapters": {"python": python, "java": java},
                **(interpreter_info(root) if root is not None else {})}

    def get(self, owner: str, project_id: str, session_id: str) -> DebugSession:
        session = self.sessions.get(session_id)
        if not session or session.owner != owner or session.project_id != project_id:
            raise KeyError("调试会话不存在")
        return session

    def list(self, owner: str, project_id: str) -> list[dict]:
        return [session.snapshot() for session in reversed(list(self.sessions.values())) if session.owner == owner and session.project_id == project_id]

    async def start(self, owner: str, project_id: str, root: str | Path, *, path: str, args: list[str] | None = None, cwd: str = "", breakpoints: list | None = None, language: str | None = None, java: dict | None = None) -> DebugSession:
        root = Path(root).resolve()
        language = language or ("java" if isinstance(path, str) and path.lower().endswith(".java") else "python")
        if language not in {"python", "java"}:
            raise ProjectPathError("当前支持 Python 和 Java 调试")
        if language == "java":
            from code_agent.codeintel.java_debug import JavaDebugSession, java_file, validate_java_options
            program = java_file(root, path)
            options = validate_java_options(java)
        else:
            if java is not None:
                raise ProjectPathError("Python 调试不接受 Java 设置")
            program = _python_file(root, path)
        directory = safe_child(root, cwd)
        if not directory.is_dir():
            raise ProjectPathError("调试工作目录不存在")
        args = [] if args is None else args
        if not isinstance(args, list) or len(args) > 100 or any(not isinstance(arg, str) or len(arg) > 8000 or "\x00" in arg for arg in args):
            raise ProjectPathError("程序参数必须是字符串数组（最多 100 项）")
        groups = _breakpoints(root, [] if breakpoints is None else breakpoints, language=language)
        capabilities = self.meta()
        adapter = (capabilities.get("adapters") or {}).get(language, capabilities)
        if not adapter.get("available") or (language == "java" and self.java_provider is None):
            message = "缺少 debugpy；请在后端 Python 环境运行 python -m pip install debugpy" if language == "python" else (adapter.get("install_hint") or "Java 调试服务不可用，请重新连接 Java 代码分析服务")
            raise DebugError(message, 503)
        async with self._lock:
            if self._closed:
                raise DebugError("调试服务已关闭", 503)
            if sum(s.owner == owner and s.state["status"] in {"starting", "running", "paused"} for s in self.sessions.values()) >= 3:
                raise DebugError("同时最多运行 3 个调试会话；请先停止现有会话")
            for sid in list(self.sessions):
                if len(self.sessions) <= 50:
                    break
                if self.sessions[sid].state["status"] in {"stopped", "error"} and self.sessions[sid]._closing:
                    del self.sessions[sid]
            session = (JavaDebugSession(owner, project_id, root, program, directory, args, self.java_provider, options)
                       if language == "java" else DebugSession(owner, project_id, root, program, directory, args))
            self.sessions[session.id] = session
        await session.start(groups)
        return session

    async def update_breakpoints(self, session: DebugSession, items: list) -> list[dict]:
        groups = _breakpoints(session.root, items, language=session.state.get("language", "python"))
        async with session._lock:
            if session.state["status"] not in {"running", "paused"}:
                raise DebugError("调试会话已结束")
            return await session.set_breakpoints(groups)

    async def close(self):
        self._closed = True
        await asyncio.gather(*(session.stop() for session in list(self.sessions.values())), return_exceptions=True)
