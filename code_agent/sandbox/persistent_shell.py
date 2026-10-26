"""持久 shell（对齐 DSH 的 `dsh-tool-bash-persistent`）。

每次调用新起一个 shell 的话，`cd` / `export` / 定义函数在下一句里就没了——用户要装一次
环境跑五条命令，就得把五条命令拼成一条 `cd x && export A=B && …`（真机上这种做法很快
变成一坨没法读也没法改的字符串）。持久 shell 让**同一个会话的多次调用共享一只 shell**：
工作目录、导出的变量、函数、shell 选项都留着。

## 协议

往那只看似的长驻 `bash -l -s` 的 stdin 写：

    <命令>
    <哨兵> <退出码> <当前目录>

stdout 里读到哨兵行就算这一条执行完——哨兵里带**随机 token**，所以命令自己输出
同样形状的字符串也几乎不可能撞上（真撞上也只是这一次的解析提前结束，不会串到下一次）。

## 三条与 `executor.py` 一致的铁律（都踩过）

1. **绝不 cancel 正在读管道的协程**（见 `executor.py` 文件头）。所以这里有一个**常驻**
   读协程：它只被 EOF 结束（进程死掉），从不会被取消；`run()` 等的是一个
   `asyncio.Event`（等事件被取消是安全的，等管道 future 不是）。
2. **超时/杀停 = 杀整棵进程树**，不是取消协程。杀完管道 EOF、读协程自然收尾。
3. **杀停之后必须如实说"状态没了"**。命令超时被杀时，cwd / 变量 / 函数**一起消失**；
   如果只说"超时了"，模型会以为接着还能在原来的目录里干活。

## 已知限制（不是待修的 bug，是如实声明）

- **命令不要从 stdin 读**。stdin 是协议的通道，`read` / 交互式程序会吃掉后面的脚本。
  要输入就用 here-string、重定向或 `printf | cmd`。
- **here-doc 会被协议撞坏**（`<<EOF` 会吞掉后面那行哨兵）。用 `printf` 管道代替。
- **需要 bash**：Windows 上没装 Git Bash（`bash` 工具实际走 cmd.exe）时，持久 shell
  **不支持**——不做"假装持久"的降级。
- **docker 沙箱模式不支持**：那是一个命令一个容器，容器里没有跨调用的状态可留。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import subprocess
import time
from pathlib import Path

from code_agent.sandbox.executor import MAX_OUTPUT, _kill, _release, find_bash, sandbox_mode

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"

#: 单条命令的默认/最大等待（秒）。与一次性 `bash` 的默认一致。
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0

#: 不支持持久 shell 时的两种如实说法（工具层直接用，免得各处措辞不一致）
NO_BASH = (
    "持久 shell 需要 bash，而这台机器上没有（`bash` 工具实际走的是 cmd.exe）："
    "`cd` / `export` 这类状态无法跨调用保留。请用一次性 `bash`（每条命令自己 `cd`），"
    "或装了 Git Bash 之后把 CODE_AGENT_BASH 指向 bash.exe。"
)
NO_DOCKER = (
    "docker 沙箱模式下不支持持久 shell：那是一个命令一个容器，容器之间没有可保留的状态。"
    "请用一次性 `bash`。"
)


def unavailable_reason() -> str:
    """当前环境能不能用持久 shell；能则返回 ''。"""
    if sandbox_mode() == "docker":
        return NO_DOCKER
    if find_bash() is None:
        return NO_BASH
    return ""


class PersistentShell:
    """一只长驻的 shell。**一个会话最多一只**（由 `PersistentShells` 管）。"""

    def __init__(self, proc: asyncio.subprocess.Process, token: str) -> None:
        self._proc = proc
        self._token = token
        self._buf = bytearray()
        self._read_event = asyncio.Event()
        self._eof = False
        self._reader: asyncio.Task | None = None
        #: 同步关闭路径排队的那次异步收尾（close() 要等它跑完再自己关管道）
        self._closing: asyncio.Task | None = None
        self.started_at = time.time()

    # -- 生命周期 -----------------------------------------------------------
    @classmethod
    async def spawn(cls, cwd: Path | None) -> "PersistentShell":
        """起一只读 stdin 的登录 shell。

        `-l`（登录 shell）会读 profile，于是 PATH 里该有的东西都在——与一次性
        `bash -lc` 的语义一致；`-s` 让它从 stdin 读命令。**不加 `-i`**：
        交互模式会去要 tty，在没有终端的 sidecar 里只会多出一堆警告。
        """
        argv = [find_bash() or "bash", "-l", "-s"]
        kwargs: dict = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,   # 合并 stderr：与一次性 bash 同一约定
            cwd=str(cwd) if cwd else None,
        )
        if _IS_WINDOWS:
            # 独立进程组：超时/关闭时整棵树一起杀（与 executor._spawn 同一做法）
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        shell = cls(proc, secrets.token_hex(4))
        shell._reader = asyncio.ensure_future(shell._read_loop())
        return shell

    @property
    def alive(self) -> bool:
        return not self._eof and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid

    async def close(self) -> None:
        """杀整棵树并等读协程自然收尾（顺序与 executor 一致：杀 → EOF → 读自然结束）。

        `_release(proc)` 不能省：它关掉 stdin/stdout/stderr 的流与 proc 对象本身。
        少了这一步，管道 transport 会留到 GC 时才报 "unclosed transport"
        （实测就是这句 ResourceWarning 把泄漏暴露出来的——长驻进程尤其明显，
        因为它不像一次性命令那样很快被回收）。
        """
        # 先等同步路径排队的那次收尾跑完——它会 drain 读协程；抢在它前面关管道
        # 就会把那个未完成的读取消掉（同一条铁律）。
        if self._closing is not None:
            with contextlib.suppress(Exception):
                await self._closing
            self._closing = None
        await _kill(self._proc)
        await self._drain_reader()
        await _release(self._proc)

    def close_nowait(self) -> None:
        """同步兜底关闭（`runtime.cancel()` / `shutdown()` 这类没有 await 的地方用）。

        **不在这里直接关管道**：读协程还挂着一个没完成的读，关掉管道会让那个
        `OverlappedFuture` 被取消——正是 `executor.py` 文件头警告过的那个坑。
        实测的现场：`<... read=<_OverlappedFuture cancelled>>` 加一句
        "unclosed transport" 的 ResourceWarning（`PipeHandle` 也没关）。
        正确顺序仍然是：**杀进程 → 管道 EOF → 读协程自然结束 → 再回收**，
        所以这里只杀进程，收尾排到事件循环上做；调用方（`PersistentShells`）
        必须**保住这个对象的引用**并在 `close()` 里等 `_closing` 跑完，
        否则循环关闭时那个任务会被取消，收尾就永远没做完。
        """
        with contextlib.suppress(Exception):
            self._proc.kill()
        self._eof = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的循环（进程正在退出）：进程已被杀，交给 OS 回收。
            return
        self._closing = loop.create_task(self._finish_close())

    async def _finish_close(self) -> None:
        """异步收尾：等读协程自然结束，再回收 transport 与句柄。"""
        await self._drain_reader()
        await _release(self._proc)

    async def _drain_reader(self) -> None:
        if self._reader is None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(self._reader), timeout=5.0)
        if not self._reader.done():
            # 极端兜底：只有到这里才取消（与 executor 的注释同一取舍）
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader

    # -- 读协程（**永不取消**）---------------------------------------------
    async def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        while True:
            try:
                chunk = await self._proc.stdout.read(65536)
            except (ValueError, ConnectionResetError):
                break
            if not chunk:
                break
            self._buf.extend(chunk)
            self._read_event.set()
        self._eof = True
        self._read_event.set()

    # -- 执行 ---------------------------------------------------------------
    async def run(self, command: str, *, timeout: float = DEFAULT_TIMEOUT, max_output: int = MAX_OUTPUT) -> str:
        if not self.alive:
            return self._dead_result()
        assert self._proc.stdin is not None

        marker = f"__CA_SHELL_DONE_{self._token}__"
        script = (
            f"{command}\n"
            f"__ca_rc=$?\n"
            f"printf '\\n{marker} %s %s\\n' \"$__ca_rc\" \"$PWD\"\n"
        )
        self._buf.clear()
        self._read_event.clear()
        try:
            self._proc.stdin.write(script.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            self._eof = True
            return self._dead_result()

        deadline = asyncio.get_running_loop().time() + max(0.1, timeout)
        while True:
            found = self._split_at_marker(marker)
            if found is not None:
                output, code, cwd = found
                return self._format(output, code, cwd, max_output)
            if self._eof:
                # 进程自己死了（命令 `exit`、崩溃、被外部杀）：如实报，并说明状态没了
                partial = self._take_all()
                return (
                    "[error] 持久 shell 已退出（命令可能调用了 exit，或进程被外部终止）。"
                    "**它保存的状态（cwd / 变量 / 函数）已经没了**，下一次调用会重新起一只。\n"
                    f"退出前的输出：\n{partial.rstrip()}"
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self._kill_and_report()
                return (
                    f"[error] 持久 shell 执行超时（>{timeout:g}s），已终止这个 shell。"
                    "**它保存的状态（cwd / 变量 / 函数）一并消失**——下一次调用会重新起一只，"
                    "请在新的 shell 里重新 cd / export。\n"
                    "（如果这条命令本来就要跑很久，改用 bash 的 run_in_background。）\n"
                    f"超时前已产生的输出：\n{self._take_all().rstrip()}"
                )
            try:
                # 等事件（不是等管道 future）：取消一个 Event.wait() 是安全的
                await asyncio.wait_for(self._read_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            self._read_event.clear()

    async def _kill_and_report(self) -> None:
        await _kill(self._proc)
        self._eof = True
        await self._drain_reader()
        await _release(self._proc)

    def _dead_result(self) -> str:
        return (
            "[error] 持久 shell 已经不在了（进程已退出）。"
            "下一次调用会重新起一只，请重新 cd / export。"
        )

    # -- 解析 ---------------------------------------------------------------
    def _split_at_marker(self, marker: str) -> tuple[str, int, str] | None:
        """在缓冲区里找哨兵行；找到就返回 (输出, 退出码, cwd) 并把消耗掉的部分删掉。"""
        needle = ("\n" + marker + " ").encode("utf-8")
        idx = bytes(self._buf).find(needle)
        if idx < 0:
            return None
        line_end = bytes(self._buf).find(b"\n", idx + len(needle))
        if line_end < 0:
            return None  # 哨兵行还没读全
        payload = bytes(self._buf)[idx + len(needle) : line_end].decode("utf-8", "replace")
        output = bytes(self._buf)[:idx].decode("utf-8", "replace")
        del self._buf[: line_end + 1]
        rc_text, _, cwd = payload.partition(" ")
        try:
            code = int(rc_text.strip())
        except ValueError:
            code = 0
        return output, code, cwd.strip()

    def _take_all(self) -> str:
        text = bytes(self._buf).decode("utf-8", "replace")
        self._buf.clear()
        return text

    @staticmethod
    def _format(output: str, code: int, cwd: str, max_output: int) -> str:
        """与一次性 `bash` 同一形状（`exit_code=` 开头），另附当前目录。

        附 cwd 不是装饰：持久 shell 里"我现在在哪"是跨调用状态的一部分，
        不告诉模型它就只能在下一步先 `pwd` 一次（白花一轮）。
        """
        body = output.strip("\n")
        truncated = False
        data = body.encode("utf-8")
        if len(data) > max_output:
            # `errors="ignore"` 而不是 "replace"：按字节切会切断多字节字符，
            # "replace" 会在切口留下一个 U+FFFD（乱码方块），而模型会把乱码
            # 当成输出内容的一部分。宁少一个字，不给坏字符（M46 同一条取舍）。
            body = data[:max_output].decode("utf-8", errors="ignore")
            truncated = True
        header = f"exit_code={code}" + (f"  cwd={cwd}" if cwd else "")
        if truncated:
            header += f" (输出超过 {max_output} 字节，已截断)"
        return f"{header}\n{body if body.strip() else '(无输出)'}"


class PersistentShells:
    """一个会话的持久 shell 管理器：懒起、死了自动重起、随会话生命周期关闭。"""

    def __init__(self, *, cwd: Path | None = None) -> None:
        self._cwd = cwd
        self._shell: PersistentShell | None = None
        self._closed = False
        #: 已经排队收尾、但还没被 await 回来的 shell（见 _drain_closing）
        self._closing: list[PersistentShell] = []
        #: 上一只 shell 死掉的原因（下次调用时如实告诉模型"为什么你的 cd 不见了"）
        self._last_loss = ""

    @property
    def alive(self) -> bool:
        return self._shell is not None and self._shell.alive

    async def run(self, command: str, *, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> str:
        if self._closed:
            return "[error] 这个会话的持久 shell 已经关闭（会话运行时已释放）。请用一次性 bash。"
        reason = unavailable_reason()
        if reason:
            return reason
        if self._shell is None or not self._shell.alive:
            lost = ""
            if self._last_loss:
                lost = f"（上一只持久 shell 已经不在了：{self._last_loss}）\n"
                self._last_loss = ""
            if self._shell is not None:
                # **换掉死掉的那只之前必须回收它**：直接覆盖引用等于把它的
                # transport / 管道句柄丢给 GC，而 GC 只会在某次无关的测试里
                # 丢出一句 "unclosed transport"（实测就是这么发现的）。
                await self._shell.close()
                self._shell = None
            self._shell = await PersistentShell.spawn(cwd or self._cwd)
            prefix = lost + "（已为该会话启动持久 shell：cwd / 导出的变量 / 函数会在后续调用里保留）\n"
            result = await self._shell.run(command, timeout=timeout)
            if result.startswith("exit_code="):
                # Keep the real process receipt first, including on startup.
                # Loop failure recovery and verification use this same header.
                header, separator, body = result.partition("\n")
                return header + "\n" + prefix + body
            return prefix + result
        result = await self._shell.run(command, timeout=timeout)
        if not self._shell.alive:
            self._last_loss = "进程退出或被超时终止"
        return result

    async def close(self) -> None:
        self._closed = True
        if self._shell is not None:
            await self._shell.close()
            self._shell = None
        await self._drain_closing()

    async def _drain_closing(self) -> None:
        """把同步路径排队的收尾等回来。

        不保住这些引用的话：`kill_nowait`/`close_nowait` 把 `_shell` 置空之后，
        排到循环上的 `_finish_close` 就成了"没人认领的任务"，事件循环一关就被取消，
        收尾永远没做完 → transport 泄漏（实测：`read=<_OverlappedFuture cancelled>`
        + "unclosed transport"）。这里既保住引用，也在能 await 的地方把它等完。
        """
        closing, self._closing = self._closing, []
        for shell in closing:
            with contextlib.suppress(Exception):
                await shell.close()

    def kill_nowait(self) -> None:
        """杀掉**当前这一只** shell，但管理器**仍然可用**（下次调用会重新起一只）。

        这是"用户点了停止"的语义：正在跑的那条命令必须立刻停（留着它等于取消没生效），
        但会话没结束，用户下一条消息里还应该能用持久 shell。所以这里不置 `_closed`。
        """
        if self._shell is not None:
            self._shell.close_nowait()
            self._closing.append(self._shell)   # 保住引用，等 close() 收尾
            self._shell = None
            self._last_loss = "被停止（用户取消或会话重置）"

    def close_nowait(self) -> None:
        """永久关闭（会话运行时被释放）：之后这个会话不再提供持久 shell。"""
        self._closed = True
        if self._shell is not None:
            self._shell.close_nowait()
            self._closing.append(self._shell)
            self._shell = None
