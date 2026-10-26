"""仓库探测（M18 代码模式 · 只读）。

P0 只做"看清现状"：这是不是一个 git 仓库、当前分支、远端、是否脏。
写操作（add/commit/pull/push/worktree）在 P4 的 vcs 操作层实现，并且
**一律 argv 直传不经 shell**（分支名/远端名里带空格或分号时，拼 shell 是
注入面）；这里先把 "argv + 超时 + 杀掉整棵进程树" 的调用范式立起来。

这里不缓存探测结果（分支在外部随时会变）。完整 Git 面板显式读取最新状态；
工作区的列表/详情仅用 ProjectService 的登记信息与快照，避免等待逐项目探测。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: 单条 git 命令的超时（探测类命令都该在毫秒级返回）
PROBE_TIMEOUT = 5.0
KILL_GRACE = 2.0


@dataclass
class RepoInfo:
    git_available: bool = False
    is_repo: bool = False
    branch: str = ""
    remote: str = ""
    head: str = ""
    dirty: bool = False
    error: str = ""
    changed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "git_available": self.git_available,
            "is_repo": self.is_repo,
            "branch": self.branch,
            "remote": self.remote,
            "head": self.head,
            "dirty": self.dirty,
            "changed": self.changed,
            "error": self.error,
        }


def git_available() -> bool:
    return shutil.which("git") is not None


#: 破坏性操作黑名单（正则）。这些命令**不提供接口**，也不在审批范围内：
#: 它们能不可逆地破坏用户的东西，属于"平台不该提供这个按钮"。
DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^push\b.*(--force|-f)(\s|$)", "强制推送会覆盖远端历史"),
    (r"^reset\b.*--hard", "reset --hard 会丢弃工作区改动"),
    (r"^clean\b.*-[a-z]*f", "clean -f 会删除未跟踪文件"),
    (r"^remote\b\s+(set-url|remove|rm)\b", "改远端地址属于运维动作，请在终端里做"),
    (r"^branch\b\s+(-D|--delete\s+--force)", "强删分支可能丢提交"),
    (r"^filter-branch\b|^filter-repo\b", "历史重写"),
    (r"^rebase\b|^cherry-pick\b", "改写提交历史"),
    (r"^gc\b|^prune\b|^fsck\b", "仓库维护"),
)

#: 需要审批的写操作（其余写操作也都要审批，这里显式列出便于界面提示）
APPROVAL_OPERATIONS = ("add", "commit", "checkout", "fetch", "pull", "push", "stash", "merge", "worktree")


def check_command(args: list[str]) -> str | None:
    """危险命令检查：命中返回原因，未命中返回 None。"""
    joined = " ".join(args)
    for pattern, reason in DENY_PATTERNS:
        if re.search(pattern, joined, re.IGNORECASE):
            return reason
    return None


def git_env(ssh_key: str = "") -> dict[str, str]:
    """git 子进程环境。

    `BatchMode=yes` 是关键：否则 SSH 会在指纹确认/密码提示上**一直挂着**，
    表现为"点了一下拉取，界面永远转圈"（这类事故在自动化里最常见）。
    凭据策略按既定决策：默认复用宿主凭据/SSH agent；配置了 ssh_key 就用它，
    并只认这一个 key（IdentitiesOnly），避免 agent 里别的 key 干扰。
    """
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")      # 禁用交互式用户名/密码提示
    parts = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if ssh_key:
        key = Path(ssh_key).expanduser()
        parts += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    env["GIT_SSH_COMMAND"] = " ".join(f'"{p}"' if " " in p else p for p in parts)
    return env


async def run_git(
    root: str | Path,
    args: list[str],
    *,
    timeout: float = PROBE_TIMEOUT,
    max_output: int = 64 * 1024,
    ssh_key: str = "",
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """执行 git 命令（只读探测与写操作共用）。返回 (exit_code, 输出)。

    与 sandbox/executor.py 同一套纪律：独立进程组、超时杀整棵树、
    绝不 cancel 正在读管道的协程（Windows Proactor 下会导致 transport 泄漏）。
    命令**一律 argv 直传**，不经 shell：分支名/远端名里带空格或分号时，拼 shell 就是注入面。
    """
    argv = ["git", "-C", str(Path(root).resolve()), *args]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if platform.system() == "Windows" else 0
    env = git_env(ssh_key)
    env.update(extra_env or {})
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        creationflags=creationflags,
    )
    chunks: list[bytes] = []

    async def _pump() -> None:
        assert proc.stdout is not None
        while True:
            try:
                chunk = await proc.stdout.read(65536)
            except (ValueError, ConnectionResetError):
                break
            if not chunk:
                break
            chunks.append(chunk)

    pump = asyncio.ensure_future(_pump())
    waiter = asyncio.ensure_future(proc.wait())
    done, _ = await asyncio.wait({waiter}, timeout=timeout)
    if waiter in done:
        await waiter
    else:
        await _kill_tree(proc)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(pump), timeout=KILL_GRACE)
    if not pump.done():
        pump.cancel()
        with contextlib.suppress(BaseException):
            await pump
    code = proc.returncode if proc.returncode is not None else -1
    text = b"".join(chunks).decode("utf-8", errors="replace")[:max_output]
    return code, text.strip()


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
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
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE)


async def probe_repo(root: str | Path, *, with_status: bool = False) -> RepoInfo:
    """探测项目目录的 git 现状。任何失败都返回 info（不抛），让 UI 显示原因。"""
    info = RepoInfo(git_available=git_available())
    if not info.git_available:
        info.error = "未安装 git"
        return info
    try:
        code, out = await run_git(root, ["rev-parse", "--is-inside-work-tree"])
    except (OSError, asyncio.TimeoutError) as exc:
        info.error = f"{type(exc).__name__}: {exc}"
        return info
    if code != 0 or out.strip() != "true":
        info.error = "不是 git 仓库"
        return info

    info.is_repo = True
    code, out = await run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if code == 0:
        info.branch = out.strip()
    code, out = await run_git(root, ["rev-parse", "--short", "HEAD"])
    if code == 0:
        info.head = out.strip()
    code, out = await run_git(root, ["remote", "get-url", "origin"])
    if code == 0:
        info.remote = out.strip()

    if with_status:
        # 用 --untracked-files=all：新建文件也是"工作区有改动"。
        # 早先用默认的 no，结果是面板的改动清单里列着新文件、dirty 却是 false（自相矛盾）。
        code, out = await run_git(root, ["status", "--porcelain", "--untracked-files=all"])
        if code == 0 and out:
            info.dirty = True
            info.changed = [line for line in out.splitlines()[:200] if line.strip()]
    return info
