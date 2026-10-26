"""Java DAP integration with the Microsoft java-debug plugin loaded by JDT LS.

Only a trusted backend callback can execute the small Java command allowlist.
No browser supplied adapter address, classpath, executable, or shell is accepted.
Existing plugin bundles are discovered; this module never installs dependencies.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from code_agent.codeintel.debug_runtime import DebugError, DebugSession
from code_agent.codeintel.paths import ProjectPathError, safe_child
from code_agent.config import _legacy_home, agent_home

JAVA_DEBUG_COMMANDS = frozenset({
    "vscode.java.startDebugSession", "vscode.java.resolveMainClass",
    "vscode.java.resolveMainMethod", "vscode.java.resolveClasspath",
    "vscode.java.resolveJavaExecutable",
})
_MAIN_CLASS = re.compile(r"(?:[\w.$]+/)?[\w$]+(?:\.[\w$]+)*\Z", re.UNICODE)
_INSTALL_HINT = (
    "安装 Microsoft Debugger for Java，或将 com.microsoft.java.debug.plugin-*.jar 放入 "
    f"{agent_home() / 'ls' / 'java-debug'}；也可设置 CODE_AGENT_JAVA_DEBUG_BUNDLE。"
    "安装后重新连接 Java 代码分析服务。"
)


def _bundle_valid(path: Path) -> bool:
    try:
        if not path.is_file() or path.suffix.lower() != ".jar" or not 0 < path.stat().st_size <= 32 * 1024 * 1024:
            return False
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("META-INF/MANIFEST.MF")
            if info.file_size > 64 * 1024:
                return False
            manifest = archive.read(info).decode("utf-8", errors="replace").replace("\r\n ", "").replace("\n ", "")
            return bool(re.search(r"^Bundle-SymbolicName:\s*com\.microsoft\.java\.debug\.plugin(?:;[^\r\n]*)?\s*$", manifest, re.MULTILINE))
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
        return False


def _version_key(path: Path):
    return tuple((1, int(part)) if part.isdigit() else (0, part.lower()) for part in re.split(r"(\d+)", path.name))


def debug_bundles() -> list[str]:
    """Return one existing validated bundle, newest version within the first source."""
    explicit = os.environ.get("CODE_AGENT_JAVA_DEBUG_BUNDLE", "")
    sources: list[list[Path]] = []
    if explicit:
        sources.append([Path(value).expanduser() for value in explicit.split(os.pathsep) if value.strip()][:16])
    for base in dict.fromkeys((agent_home(), _legacy_home())):
        sources.append(list((base / "ls" / "java-debug").glob("com.microsoft.java.debug.plugin*.jar"))[:32])
    for home in (".vscode", ".vscode-insiders", ".cursor"):
        extensions = Path.home() / home / "extensions"
        candidates = sorted(extensions.glob("vscjava.vscode-java-debug-*"), key=_version_key, reverse=True)[:24]
        for directory in candidates:
            sources.append(list((directory / "server").glob("com.microsoft.java.debug.plugin*.jar"))[:16])
    for candidates in sources:
        for path in sorted(candidates, key=_version_key, reverse=True):
            if _bundle_valid(path):
                return [str(path.resolve())]
    return []


def java_file(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ProjectPathError("请选择项目内的 Java 文件")
    path = safe_child(root, raw)
    if not path.is_file() or path.suffix.lower() != ".java":
        raise ProjectPathError("Java 调试需要项目内已保存的 .java 文件")
    return path


def _text_option(options: dict, key: str, maximum: int = 512) -> str:
    value = options.get(key) or ""
    if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ProjectPathError(f"{key} 必须是有效的文本")
    return value.strip()


def validate_java_options(options: Any) -> dict:
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ProjectPathError("Java 调试设置必须是对象")
    allowed = {"request", "main_class", "project_name", "host", "port", "stop_on_entry"}
    if set(options) - allowed:
        raise ProjectPathError("Java 调试设置包含不支持的字段")
    request = options.get("request", "launch")
    if request not in {"launch", "attach"}:
        raise ProjectPathError("Java 调试支持 launch 或 attach")
    main_class = _text_option(options, "main_class")
    if main_class and not _MAIN_CLASS.fullmatch(main_class):
        raise ProjectPathError("Java 主类名称无效")
    project_name = _text_option(options, "project_name")
    stop_on_entry = options.get("stop_on_entry", True)
    if not isinstance(stop_on_entry, bool):
        raise ProjectPathError("stop_on_entry 必须是布尔值")
    result = {"request": request, "main_class": main_class, "project_name": project_name, "stop_on_entry": stop_on_entry}
    if request == "attach":
        host = options.get("host", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ProjectPathError("Java 附加调试仅允许本机地址")
        port = options.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ProjectPathError("Java 附加端口必须在 1 到 65535 之间")
        result.update(host="127.0.0.1" if host == "localhost" else host, port=port)
    elif "host" in options or "port" in options:
        raise ProjectPathError("启动调试不接受附加地址")
    return result


JavaCommand = Callable[[str, str, Path, str, list, float], Awaitable[Any]]


class JavaDebugProvider:
    def __init__(self, command: JavaCommand, *, acquire=None, release=None):
        self.command, self.acquire, self.release = command, acquire, release

    def meta(self) -> dict:
        bundles = debug_bundles()
        return {
            "available": bool(bundles), "adapter": "java-debug", "bundle_found": bool(bundles),
            "install_hint": None if bundles else _INSTALL_HINT,
            "requests": ["launch", "attach"], "attach_hosts": ["127.0.0.1", "::1"],
            "limitations": ["需要已加载 Java Debug 插件的 JDT LS 和可解析的 Java 工程；启动前请构建并保存。", "附加会话停止时仅断开连接，不终止被附加的 Java 进程。"],
        }

    async def execute(self, owner: str, project_id: str, root: Path, command: str, arguments: list, timeout=30):
        if command not in JAVA_DEBUG_COMMANDS:
            raise DebugError("Java 调试命令不在允许列表内", 422)
        try:
            return await self.command(owner, project_id, root, command, arguments, timeout)
        except (DebugError, ProjectPathError):
            raise
        except Exception as exc:
            raise DebugError(f"Java 调试服务不可用：{str(exc)[:1000]}。请确认 JDT LS 已加载 Java Debug 插件。", 503) from exc

    async def main_classes(self, owner: str, project_id: str, root: Path, program: Path | None = None) -> list[dict]:
        command = "vscode.java.resolveMainMethod" if program else "vscode.java.resolveMainClass"
        values = await self.execute(owner, project_id, root, command, [(program or root).as_uri()])
        result = []
        for value in values[:100] if isinstance(values, list) else []:
            if not isinstance(value, dict) or not isinstance(value.get("mainClass"), str):
                continue
            if not _MAIN_CLASS.fullmatch(value["mainClass"]):
                continue
            path = value.get("filePath")
            if path:
                try:
                    source = Path(path).resolve()
                    if not source.is_relative_to(root):
                        continue
                    path = source.relative_to(root).as_posix()
                except (ValueError, OSError, TypeError):
                    continue
            result.append({"main_class": value["mainClass"], "project_name": str(value.get("projectName") or "")[:512], "path": path})
        return result

    async def launch_arguments(self, owner: str, project_id: str, root: Path, program: Path, cwd: Path, args: list[str], options: dict) -> dict:
        if options["request"] == "attach":
            return {"type": "java", "request": "attach", "hostName": options["host"], "port": options["port"],
                    "projectName": options["project_name"], "sourcePaths": [str(root)]}
        main_class, project_name = options["main_class"], options["project_name"]
        if not main_class:
            candidates = await self.main_classes(owner, project_id, root, program)
            if project_name:
                candidates = [item for item in candidates if item["project_name"] == project_name]
            if len(candidates) != 1:
                message = "当前文件没有可运行的 Java main 方法" if not candidates else "当前文件有多个入口，请指定主类和 Java 工程"
                raise DebugError(message, 422)
            main_class, project_name = candidates[0]["main_class"], candidates[0]["project_name"]
        classpaths = await self.execute(owner, project_id, root, "vscode.java.resolveClasspath", [main_class, project_name])
        if not (isinstance(classpaths, list) and len(classpaths) == 2 and all(isinstance(value, list) for value in classpaths)
                and all(isinstance(path, str) and "\x00" not in path for value in classpaths for path in value)):
            raise DebugError("Java 工程未返回有效的运行类路径；请等待导入完成并构建项目", 422)
        if not any(classpaths):
            raise DebugError("Java 工程运行类路径为空，请先构建项目", 422)
        java = await self.execute(owner, project_id, root, "vscode.java.resolveJavaExecutable", [main_class, project_name])
        if not isinstance(java, str) or "\x00" in java or not Path(java).is_file():
            raise DebugError("Java 工程没有可用的 JDK 运行程序，请检查工程 JDK 配置", 422)
        # Program arguments are appended as an array by the backend's reverse
        # runInTerminal handler. Sending an encoded string through both Java's
        # command parser and Windows quoting loses quotes/trailing backslashes.
        return {"type": "java", "request": "launch", "name": program.name, "mainClass": main_class,
                "projectName": project_name, "modulePaths": classpaths[0], "classPaths": classpaths[1],
                "javaExec": java, "cwd": str(cwd), "args": "", "console": "integratedTerminal",
                "vmArgs": "-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8",
                "encoding": "UTF-8", "shortenCommandLine": "argfile", "sourcePaths": [str(root)],
                "stopOnEntry": options["stop_on_entry"]}

    async def adapter_port(self, owner: str, project_id: str, root: Path) -> int:
        port = await self.execute(owner, project_id, root, "vscode.java.startDebugSession", [])
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise DebugError("Java 调试适配器未返回有效的本机端口", 503)
        return port


class JavaDebugSession(DebugSession):
    def __init__(self, owner, project_id, root, program, cwd, args, provider: JavaDebugProvider, options: dict):
        super().__init__(owner, project_id, root, program, cwd, args)
        self.provider, self.options = provider, options
        self._lease = None
        self._leased = False
        self._java_exec: Path | None = None
        self.debuggee_process: asyncio.subprocess.Process | None = None
        self._java_output_tasks: list[asyncio.Task] = []
        self.state.update(language="java", adapter="java-debug", request=options["request"], main_class=options["main_class"])
        for key in ("python", "interpreter_source", "interpreter_exists"):
            self.state.pop(key, None)

    def _event(self, event: str, body: dict):
        if event in {"process", "processid"}:
            # Only a backend-spawned JVM is owned. An adapter event may never
            # authorize terminating JDT or an externally attached process.
            return
        super()._event(event, body)

    async def _read_java_output(self, stream: asyncio.StreamReader, category: str):
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while blob := await stream.read(4096):
            self._output(category, decoder.decode(blob))
        self._output(category, decoder.decode(b"", final=True))

    async def _watch_java_exit(self):
        assert self.debuggee_process
        self.state["exit_code"] = await self.debuggee_process.wait()
        self._touch()

    async def _handle_client_request(self, message: dict):
        if message.get("command") != "runInTerminal" or self.options["request"] != "launch":
            return await super()._handle_client_request(message)
        success, body, error = False, {}, None
        try:
            if self._closing or self.debuggee_process is not None:
                raise DebugError("Java 调试启动已经取消或正在运行")
            arguments = message.get("arguments") or {}
            argv = arguments.get("args")
            if (not isinstance(argv, list) or not argv or len(argv) > 2048
                    or any(not isinstance(value, str) or len(value) > 32768 or "\x00" in value for value in argv)
                    or not self._java_exec or Path(argv[0]).resolve() != self._java_exec
                    or argv[-1] != self.state["main_class"]):
                raise DebugError("Java 调试器返回了未授权的启动程序", 422)
            argv = [*argv, *self.args]
            directory = Path(arguments.get("cwd") or self.cwd).resolve()
            if directory != self.cwd or not directory.is_relative_to(self.root):
                raise DebugError("Java 调试器返回了项目之外的工作目录", 422)
            raw_env = arguments.get("env") or {}
            if (not isinstance(raw_env, dict) or len(raw_env) > 256
                    or any(not isinstance(key, str) or "=" in key or "\x00" in key or len(key) > 512
                           or (value is not None and (not isinstance(value, str) or len(value) > 32768 or "\x00" in value))
                           for key, value in raw_env.items())):
                raise DebugError("Java 调试器返回了无效的启动环境", 422)
            environment = dict(os.environ)
            for key, value in raw_env.items():
                if value is None:
                    environment.pop(key, None)
                else:
                    environment[key] = value
            # Stop and spawn share a lock: a cancellation cannot finish cleanup
            # before a pending spawn assigns the owned process to this session.
            async with self._stop_lock:
                if self._closing:
                    raise DebugError("Java 调试启动已经取消")
                self.debuggee_process = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(directory), env=environment, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    **({"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}),
                )
                self.debuggee_pid = self.debuggee_process.pid
                self._java_output_tasks = [
                    asyncio.create_task(self._read_java_output(self.debuggee_process.stdout, "stdout")),
                    asyncio.create_task(self._read_java_output(self.debuggee_process.stderr, "stderr")),
                    asyncio.create_task(self._watch_java_exit()),
                ]
            success, body = True, {"processId": self.debuggee_pid}
        except Exception as exc:
            error = str(exc)
        self._seq += 1
        response = {"seq": self._seq, "type": "response", "request_seq": message["seq"], "command": message["command"], "success": success, "body": body}
        if error:
            response["message"] = error
        await self._write(response)

    async def start(self, breakpoints: dict[str, list[int]]):
        launch = None
        try:
            if self.provider.acquire:
                self._lease = await self.provider.acquire(self.owner, self.project_id, self.root)
                self._leased = True
                if self._closing:
                    if self.provider.release:
                        await self.provider.release(self._lease)
                    self._leased = False
                    raise DebugError("Java 调试启动已经取消", 503)
            config = await self.provider.launch_arguments(self.owner, self.project_id, self.root, self.program, self.cwd, self.args, self.options)
            if self._closing:
                raise DebugError("Java 调试启动已经取消", 503)
            self._java_exec = Path(config["javaExec"]).resolve() if config.get("javaExec") else None
            self.state["main_class"] = config.get("mainClass") or None
            port = await self.provider.adapter_port(self.owner, self.project_id, self.root)
            if self._closing:
                raise DebugError("Java 调试启动已经取消", 503)
            try:
                self._dap_reader, self._dap_writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 8)
            except (OSError, asyncio.TimeoutError) as exc:
                raise DebugError("无法连接本机 Java 调试适配器，请重新连接 Java 代码分析服务", 503) from exc
            if self._closing:
                self._dap_writer.close()
                with contextlib.suppress(Exception):
                    await self._dap_writer.wait_closed()
                raise DebugError("Java 调试启动已经取消", 503)
            self._reader = asyncio.create_task(self._read())
            await self.request("initialize", {"adapterID": "java", "clientID": "code-agent", "clientName": "Code Agent",
                                               "linesStartAt1": True, "columnsStartAt1": True, "pathFormat": "path",
                                               "supportsVariableType": True, "supportsRunInTerminalRequest": True})
            launch = asyncio.create_task(self.request(self.options["request"], config, timeout=45))
            initialized = asyncio.create_task(self._initialized.wait())
            try:
                done, _ = await asyncio.wait({initialized, launch}, timeout=35, return_when=asyncio.FIRST_COMPLETED)
                if launch in done:
                    await launch
                if not self._initialized.is_set():
                    # Some adapters acknowledge attach before initialized; wait for
                    # the actual event before installing breakpoints.
                    await asyncio.wait_for(initialized, 8)
            except asyncio.TimeoutError as exc:
                raise DebugError("Java 调试器初始化超时", 504) from exc
            finally:
                initialized.cancel()
                await asyncio.gather(initialized, return_exceptions=True)
            await self.set_breakpoints(breakpoints)
            await self.request("setExceptionBreakpoints", {"filters": []})
            await self.request("configurationDone")
            await launch
            if self.state["status"] == "starting":
                self.state.update(status="running")
                self._touch()
        except BaseException as exc:
            if isinstance(exc, Exception) and not self._closing:
                self.state.update(status="error", error=str(exc))
            if launch:
                launch.cancel()
                await asyncio.gather(launch, return_exceptions=True)
            await self.stop()
            raise

    async def stop(self):
        async with self._stop_lock:
            if self._closing:
                return
            self._closing = True
            self._invalidate()
            try:
                if self._dap_writer and not self._dap_writer.is_closing():
                    with contextlib.suppress(Exception):
                        await self.request("disconnect", {"terminateDebuggee": self.options["request"] == "launch"}, timeout=4)
                    self._dap_writer.close()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self._dap_writer.wait_closed(), 3)
                for task in (self._reader, self._inspection):
                    if task and task is not asyncio.current_task() and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (self._reader, self._inspection) if task and task is not asyncio.current_task()), return_exceptions=True)
            finally:
                if self.debuggee_process and self.debuggee_process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        self.debuggee_process.kill()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.debuggee_process.wait(), 4)
                if self._java_output_tasks:
                    try:
                        await asyncio.wait_for(asyncio.gather(*self._java_output_tasks, return_exceptions=True), 3)
                    except asyncio.TimeoutError:
                        for task in self._java_output_tasks:
                            task.cancel()
                        await asyncio.gather(*self._java_output_tasks, return_exceptions=True)
                if self._leased and self.provider.release:
                    with contextlib.suppress(Exception):
                        await self.provider.release(self._lease)
                    self._leased = False
                if self.state["status"] != "error":
                    self.state.update(status="stopped", reason=self.state["reason"] or "stopped")
                self._touch()
