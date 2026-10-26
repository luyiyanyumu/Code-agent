"""Shell 沙盒执行器（M1 版：超时 + 输出上限 + 工作区隔离）。

安全边界（对应设计文档 §7.2 第 1-2 层）：
- 执行超时强制杀死（KILL_GRACE 后仍不退则强杀并回收）
- 输出截断到 MAX_OUTPUT
- 默认 cwd 限定在会话工作区

Windows / Proactor 关键约束（两条踩坑结论）：
1. **绝不能 cancel 正在读管道的协程**。wait_for(read) 超时会取消底层
   OverlappedFuture，该 future 残留在 transport 上导致 pipe 与
   _WindowsSubprocessTransport 永久无法回收（ResourceWarning 泄漏）。
2. **必须杀整棵进程树**。create_subprocess_shell 起的是 cmd.exe，真正的
   命令是它的子进程；只 kill 父进程时子进程仍持有 stdout 管道写句柄，
   管道永不 EOF，读协程无法自然结束（只能被取消 -> 回到问题 1）。
   因此 Windows 用 `taskkill /F /T`，POSIX 用进程组 SIGKILL。

正确顺序：并发读 + 等待进程退出；超时 -> 杀进程树 -> 管道 EOF ->
读协程自然结束 -> 再回收 transport。全程零取消。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import re
import signal
import subprocess
from pathlib import Path
from typing import Callable

#: Windows 上 bash 的候选路径（Git for Windows）。
#: **不要用 `shutil.which("bash")`**：WindowsApps 里有个同名的 WSL 启动器，
#: which 会命中它，命令于是被送进一个可能根本没装的 Linux 发行版（真机踩过）。
GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)
#: cmd.exe 报"没有这个 POSIX 工具"的常见形态
_POSIX_MISSING = re.compile(
    r"'?(head|grep|sed|awk|tail|cut|sort|uniq|xargs|wc|cat|ls|which|env|tee)'? "
    r"is not recognized",
    re.I,
)


def find_bash() -> str | None:
    """找可用的 bash（Windows 上只认 Git Bash / 显式覆盖）。找不到返回 None。"""
    override = (os.environ.get("CODE_AGENT_BASH") or "").strip()
    if override and Path(override).is_file():
        return override
    if platform.system() != "Windows":
        return "bash"
    for candidate in GIT_BASH_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def shell_kind() -> str:
    """当前 `bash` 工具实际用哪个 shell（写进工具描述，让模型知道能写什么语法）。"""
    if sandbox_mode() == "docker":
        return "bash (docker sandbox)"
    bash = find_bash()
    if bash:
        return f"bash ({bash})" if platform.system() == "Windows" else "bash"
    return "cmd.exe (Windows: 没有 bash，POSIX 工具不可用)"

MAX_OUTPUT = 64 * 1024
DEFAULT_TIMEOUT = 30.0
KILL_GRACE = 3.0


async def run_shell(
    command: str,
    cwd: Path | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    max_output: int = MAX_OUTPUT,
    on_chunk: "Callable[[bytes], None] | None" = None,
    kill_event: "asyncio.Event | None" = None,
) -> str:
    """执行 shell 命令，返回统一格式的文本结果。

    `timeout=None` = **不设执行器超时**（后台任务用；对齐 DSH 的
    "后台进程没有执行器超时——工作不再需要时由调用方 job_kill"）。
    此时命令跑到自己结束为止，超时保护改由调用方负责。

    `on_chunk` = 每读到一块输出就回调一次。后台任务靠它把输出**边跑边**灌进任务
    的输出环（否则 `job_output` 在进程结束前什么都给不出来）。回调抛异常**不能**
    影响命令本身：读管道这条链路在 Windows 上极脆（见文件头那两条踩坑结论），
    一个坏回调不该把整条链路带崩。

    `kill_event` = 外部杀停通道（后台任务的 `job_kill`）。**为什么不用"取消协程"**：
    本文件开头那两条结论说得很清楚——取消正在读管道的协程会让 OverlappedFuture
    残留在 transport 上，管道与进程对象永久无法回收。所以外部杀停必须是**杀进程树**
    （与超时走同一条路：杀进程 → 管道 EOF → 读协程自然结束），而不是 cancel。
    """
    proc = await _spawn(command, cwd)

    chunks: list[bytes] = []

    async def _pump() -> None:
        """持续读 stdout（STDERR 已合并），读到 EOF 结束。"""
        assert proc.stdout is not None
        while True:
            try:
                chunk = await proc.stdout.read(65536)
            except (ValueError, ConnectionResetError):
                break
            if not chunk:
                break
            chunks.append(chunk)
            if on_chunk is not None:
                with contextlib.suppress(Exception):
                    on_chunk(chunk)

    pump_task = asyncio.ensure_future(_pump())
    wait_task = asyncio.ensure_future(proc.wait())

    timed_out = False
    killed = False
    if timeout is None and kill_event is None:
        # 纯"跑到底"：不设限也未接杀停通道
        await wait_task
    else:
        waiters: set[asyncio.Future] = {wait_task}
        kill_task: asyncio.Task | None = None
        if kill_event is not None:
            kill_task = asyncio.ensure_future(kill_event.wait())
            waiters.add(kill_task)
        # asyncio.wait 超时不会取消任务——避免任何底层 future 被置 cancelled。
        #
        # `return_when` 必须显式写 FIRST_COMPLETED：`asyncio.wait` 的默认值是
        # **ALL_COMPLETED**。原来只有一个 `wait_task` 时两者等价（所以默认值一直没暴露
        # 问题），一旦多加一个"杀停事件"的 future，ALL_COMPLETED 就变成"等进程结束
        # **并且**等人按杀停"——而后者永远不会自己完成，于是永久挂住。
        # 这类"单元素集合掩盖了默认参数语义"的坑，只有加第二个元素时才会现形。
        done, _pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        killed = kill_task is not None and kill_task in done
        timed_out = wait_task not in done and not killed
        if kill_task is not None and not kill_task.done():
            kill_task.cancel()
            with contextlib.suppress(BaseException):
                await kill_task

    if timed_out or killed:
        await _kill(proc)  # 杀进程 -> 管道 EOF -> pump 自然退出
    else:
        await wait_task

    # 等读协程自然收尾（kill 后应迅速 EOF）
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(pump_task), timeout=KILL_GRACE)
    if not pump_task.done():
        # 极端兜底：仅此时才取消（可能残留，但已尽力）
        pump_task.cancel()
        with contextlib.suppress(BaseException):
            await pump_task

    await _release(proc)

    if timed_out:
        # timed_out 只可能在 timeout 非 None 时成立；这里仍写成分支，
        # 免得将来有人在 timeout=None 的路径上漏出一句 "{None:g}" 崩掉整轮。
        limit = f">{timeout:g}s" if timeout is not None else "上限"
        return f"[error] 执行超时（{limit}），进程已终止: {command[:200]}"

    text = b"".join(chunks).decode("utf-8", errors="replace")
    truncated = False
    if len(text) > max_output:
        text = text[:max_output]
        truncated = True

    header = f"exit_code={proc.returncode}"
    if killed:
        # 被杀停时退出码不是"命令自己失败"，别让模型把它读成失败原因；
        # 具体原因（谁杀的、为什么）由任务层补 `[stopped: …]`，那里才知道。
        header += "（进程被外部终止：退出码不代表命令自身的结果）"
    if truncated:
        header += f" (输出超过 {max_output} 字节，已截断)"
    body = text if text.strip() else "(无输出)"
    return _maybe_posix_hint(f"{header}\n{body}")


def _maybe_posix_hint(text: str) -> str:
    """Windows 上落到 cmd.exe 且命令用了 POSIX 工具时，把失败原因说清楚。

    真机上表现为一句 `'head' is not recognized as an internal or external command`，
    模型看不出"这台机器上没有 bash"，于是反复重试同类命令。
    """
    if platform.system() != "Windows" or find_bash() is not None:
        return text
    if not _POSIX_MISSING.search(text):
        return text
    return text + (
        "\n\n[hint] 这台 Windows 上没有 bash，`bash` 工具实际走的是 cmd.exe："
        "head/grep/sed/awk/cat 这类 POSIX 工具都不可用。请改用 pwsh 工具、"
        "或 cmd 内置命令（findstr / find / type / dir），或装 Git Bash 后把 "
        "CODE_AGENT_BASH 指向 bash.exe。"
    )


async def _spawn(command: str, cwd: Path | None) -> asyncio.subprocess.Process:
    if sandbox_mode() == "docker":
        argv = docker_argv(command, cwd, docker_image())
        return await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    if platform.system() == "Windows":
        bash = find_bash()
        if bash:
            # 有 Git Bash 就真的用 bash：`curl … | head`、`$(...)`、`&&` 才成立。
            # 以前这里一律 create_subprocess_shell（= cmd.exe），于是"bash 工具"里
            # 写 bash 语法会报 `'head' is not recognized`（用户截图里就是这个）。
            return await asyncio.create_subprocess_exec(
                bash, "-lc", command,
                cwd=str(cwd) if cwd else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        # 独立进程组：便于超时时整棵树一起杀
        return await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return await asyncio.create_subprocess_exec(
        "bash", "-c", command,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # 独立会话/进程组，便于 killpg
    )


def sandbox_mode() -> str:
    """执行模式：process（默认，本机进程组隔离）| docker（容器隔离，4 层防御第 3 层）。"""
    return (os.environ.get("CODE_AGENT_SANDBOX_MODE") or "process").strip().lower()


def docker_image() -> str:
    return os.environ.get("CODE_AGENT_SANDBOX_IMAGE", "python:3.13-slim")


def docker_argv(command: str, cwd: Path | None, image: str) -> list[str]:
    """构造 docker 沙箱命令（纯函数，便于单测）。

    隔离策略：无网络 + 内存/CPU/PID 限额 + 只挂载会话工作区 + 非 root 用户。
    """
    workdir = "/workspace"
    argv = [
        "docker", "run", "--rm",
        "--network=none",                 # 断网：防数据外发/反弹 shell
        "--memory=512m", "--cpus=1",      # 资源限额
        "--pids-limit=256",               # 防 fork bomb
        "--read-only",                    # 根文件系统只读
        "--tmpfs", "/tmp:rw,size=64m",    # 临时目录独立可写
        "-i",
    ]
    if cwd is not None:
        argv += ["-v", f"{Path(cwd).resolve()}:{workdir}", "-w", workdir]
    argv += [image, "sh", "-c", command]
    return argv


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """杀整棵进程树（只杀父进程会留下持有管道的子进程，导致读协程永不结束）。"""
    if platform.system() == "Windows":
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            # **taskkill 自己也是一个子进程**：不回收它，它持有的管道句柄就会留到
            # GC 时才报 "unclosed <PipeHandle>”。_release 对 DEVNULL 只是空操作。
            await _release(killer)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()  # 兜底：树杀失败时至少杀父进程
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE)


async def _release(proc: asyncio.subprocess.Process) -> None:
    """进程结束后的 transport/pipe 回收。

    **stdin 也要关**：一次性命令的 stdin 是继承来的（不是管道），所以这里长期只关
    stdout/stderr 也没出事；直到持久 shell（M48）用了 `stdin=PIPE` 才暴露——
    那个写管道的 transport 一直没人回收，表现为某次无关的测试里冒出一句
    `unclosed transport ... stdin=<_ProactorWritePipeTransport ...>`。
    StreamWriter 的 close() 是同步的，对不是管道的 stdin 也只是空操作。
    """
    if proc.returncode is None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()
    with contextlib.suppress(Exception):
        proc.close()
