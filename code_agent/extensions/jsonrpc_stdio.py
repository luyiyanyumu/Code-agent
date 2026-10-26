"""JSON-RPC over stdio（M18 · P3）：MCP 与 LSP 共用的传输层。

**为什么必须抽公共层**：MCP 与 LSP 的帧格式不一样——
- MCP stdio   ：换行分隔（一行一个 JSON）
- LSP         ：`Content-Length: N\\r\\n\\r\\n{json}` 头 + 体
但两者的**会话语义完全一致**：id 递增、有请求/响应/通知、通知不能回 id、
响应要按 id 归位。所以差异只在"怎么封帧/解帧"，其余（进程生命周期、超时、
读循环、错误传播）只应存在一份。

Windows 上的硬约束（`sandbox/executor.py` 里踩过、这里同样适用）：
**绝不能 cancel 正在读管道/读进程的协程**，否则 Proactor 的 transport 会泄漏。
本模块的读循环因此用"读协程 + 心跳式超时检查"的结构，退出时统一走 `stop()`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Literal

Framing = Literal["newline", "content-length"]

#: 单条请求的默认超时（LSP 的 initialize 可能较慢，调用方可覆盖）
DEFAULT_TIMEOUT = 20.0
KILL_GRACE = 3.0

#: 给子进程显式设置的 UTF-8 环境。
#: 规范上 MCP/LSP 的 stdio 就是 UTF-8，但 Windows 上 Python 子进程默认按 ANSI
#: 代码页写 stdout——一个朴素写法的 Python 语言服务器/MCP server 会输出 GBK 字节，
#: 我们按 UTF-8 解码就变成乱码（真机上就是这条让"中文回显"测试一直失败）。
#: 用户若在配置里显式给了 env，则完全以用户的为准。
_CHILD_UTF8_ENV = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境：宿主环境 + UTF-8 默认（可被 extra 覆盖）。"""
    env = dict(os.environ)
    env.update(_CHILD_UTF8_ENV)
    env.update(extra or {})
    return env


class JsonRpcError(RuntimeError):
    """传输层/协议层错误（进程退出、超时、服务端返回 error）。"""


def encode_message(payload: dict[str, Any], framing: Framing) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if framing == "content-length":
        return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
    return body + b"\n"


@dataclass
class _Decoder:
    """按帧格式把字节流切成消息。两种格式的实现都在这里，便于单测直接喂字节。"""

    framing: Framing

    def __post_init__(self) -> None:
        self._buffer = bytearray()
        self._expected: int | None = None

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(chunk)
        out: list[dict[str, Any]] = []
        while True:
            if self.framing == "newline":
                idx = self._buffer.find(b"\n")
                if idx < 0:
                    break
                line = bytes(self._buffer[:idx]).strip()
                del self._buffer[: idx + 1]
                if not line:
                    continue
                try:
                    out.append(json.loads(line.decode("utf-8")))
                except json.JSONDecodeError:
                    continue  # 非法行忽略（LSP 服务器偶尔会往 stdout 写日志）
                continue

            # content-length：先读头，再按长度读体
            if self._expected is None:
                idx = self._buffer.find(b"\r\n\r\n")
                if idx < 0:
                    # 兼容只用 \n\n 的非标准实现
                    idx = self._buffer.find(b"\n\n")
                    if idx < 0:
                        break
                    header = bytes(self._buffer[:idx]).decode("ascii", errors="replace")
                    del self._buffer[: idx + 2]
                else:
                    header = bytes(self._buffer[:idx]).decode("ascii", errors="replace")
                    del self._buffer[: idx + 4]
                length = None
                for line in header.splitlines():
                    if line.lower().startswith("content-length:"):
                        with contextlib.suppress(ValueError):
                            length = int(line.split(":", 1)[1].strip())
                if length is None:
                    continue  # 没有长度头：丢这一段，别把后面的消息连带搞坏
                self._expected = length
                continue
            if len(self._buffer) < self._expected:
                break
            body = bytes(self._buffer[: self._expected])
            del self._buffer[: self._expected]
            self._expected = None
            try:
                out.append(json.loads(body.decode("utf-8")))
            except json.JSONDecodeError:
                continue
        return out


class JsonRpcStdio:
    """一个子进程的 JSON-RPC 会话。请求/响应按 id 归位，通知交给回调。"""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        framing: Framing = "newline",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        name: str = "",
        on_notification: Callable[[dict[str, Any]], None] | None = None,
        on_request: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.framing = framing
        self.cwd = cwd
        self.env = env
        self.name = name or command
        self.on_notification = on_notification
        #: 服务端发起的请求（LSP 里常见：window/workDoneProgress/create、configuration）
        self.on_request = on_request
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._stderr_lines: list[str] = []
        self.last_error = ""
        self.exit_code: int | None = None

    # ------------------------------------------------------------------
    async def start(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if platform.system() == "Windows" else 0
        )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if self._capture_stderr else asyncio.subprocess.DEVNULL,
                cwd=self.cwd,
                env=child_env(self.env),
                creationflags=creationflags,
            )
        except (OSError, FileNotFoundError) as exc:
            raise JsonRpcError(f"无法启动 {self.name!r}：{exc}") from exc
        self._reader = asyncio.ensure_future(self._read_loop())
        if self._capture_stderr:
            asyncio.ensure_future(self._drain_stderr())

    #: 是否抓 stderr（LSP 的报错基本只出现在 stderr，抓下来才能解释"为什么连不上"）
    _capture_stderr = True

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            try:
                line = await self._proc.stderr.readline()
            except (ValueError, ConnectionResetError):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            self._stderr_lines.append(text)
            if len(self._stderr_lines) > 40:
                del self._stderr_lines[0]

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        decoder = _Decoder(self.framing)
        try:
            while True:
                try:
                    chunk = await self._proc.stdout.read(65536)
                except (ValueError, ConnectionResetError):
                    break
                if not chunk:
                    break
                for message in decoder.feed(chunk):
                    self._dispatch(message)
        except asyncio.CancelledError:
            # 只有 stop() 会取消读循环（退出路径），此时不吞异常
            raise
        except Exception as exc:  # noqa: BLE001 —— 传输层异常要通知所有等待者，避免永久挂起
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self.exit_code = self._proc.returncode if self._proc else None
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(
                        JsonRpcError(f"{self.name} 连接已关闭{self.stderr_tail()}")
                    )
            self._pending.clear()
            self._on_closed()

    _on_closed_fn: Callable[[], None] | None = None

    def set_on_closed(self, callback: Callable[[], None] | None) -> None:
        self._on_closed_fn = callback

    def _on_closed(self) -> None:
        if self._on_closed_fn:
            with contextlib.suppress(Exception):
                self._on_closed_fn()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.pop(int(message["id"]), None)
            if future is not None and not future.done():
                is_error = "error" in message
                if is_error:
                    future.set_exception(
                        JsonRpcError(f"{self.name} 返回错误：{message['error']}")
                    )
                else:
                    future.set_result(message.get("result"))
            return
        if "id" in message and "method" in message:
            # 服务端发起的请求：必须回一个结果，否则对方会一直等（LSP 常见：进度条创建）
            asyncio.ensure_future(self._handle_server_request(message))
            return
        if self.on_notification is not None:
            with contextlib.suppress(Exception):
                self.on_notification(message)

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        result: Any = None
        error: dict[str, Any] | None = None
        if self.on_request is not None:
            try:
                result = self.on_request(message)
                if asyncio.iscoroutine(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001
                error = {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        await self.send_raw(payload)

    # ------------------------------------------------------------------
    async def send_raw(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise JsonRpcError(f"{self.name} 未启动")
        self._proc.stdin.write(encode_message(payload, self.framing))
        await self._proc.stdin.drain()

    async def request(
        self, method: str, params: Any = None, *, timeout: float = DEFAULT_TIMEOUT,
        on_request_id: Callable[[int], None] | None = None,
    ) -> Any:
        self._next_id += 1
        rid = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            if on_request_id is not None:
                on_request_id(rid)
            await self.send_raw(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            if on_request_id is not None:
                with contextlib.suppress(Exception):
                    await self.notify("$/cancelRequest", {"id": rid})
            raise JsonRpcError(f"{self.name} 请求超时：{method}（{timeout:g}s）{self.stderr_tail()}") from exc
        finally:
            self._pending.pop(rid, None)
            if not future.done():
                future.cancel()

    async def notify(self, method: str, params: Any = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self.send_raw(payload)

    def stderr_tail(self, limit: int = 3) -> str:
        if not self._stderr_lines:
            return ""
        tail = " / ".join(self._stderr_lines[-limit:])
        return f"（stderr: {tail[:300]}）"

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def stop(self, *, grace: float = KILL_GRACE) -> None:
        """关闭会话：先关 stdin 让对端自然退出，再杀整棵进程树，最后收读循环。"""
        proc = self._proc
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1.5)
        if proc.returncode is None:
            if platform.system() == "Windows":
                with contextlib.suppress(Exception):
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/T", "/PID", str(proc.pid),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
            else:
                import os
                import signal

                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=grace)
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader
            self._reader = None
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
        with contextlib.suppress(Exception):
            proc.close()
        self._proc = None
