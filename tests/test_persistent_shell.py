"""持久 shell（M48，对齐 DSH 的 `dsh-tool-bash-persistent`）。

这个功能唯一的价值是**状态跨调用**：cwd、导出的变量、函数。所以测试的重点全在
"下一次调用还能不能看见上一次留下的东西"，以及三件必须诚实的事：

1. **超时/被杀之后状态没了** —— 不清不楚地说一句"超时了"，模型会以为接着还能
   在原来的目录里干活；
2. **shell 死了要重起，并且要说明**；
3. **没有 bash / docker 模式下要如实拒绝**，不做"假装持久"的降级。
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.sandbox import persistent_shell as ps
from code_agent.sandbox.persistent_shell import NO_BASH, NO_DOCKER, PersistentShells
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.tools_builtin import BashPersistentTool

pytestmark = pytest.mark.skipif(
    ps.unavailable_reason() != "", reason=f"本机不支持持久 shell：{ps.unavailable_reason()}"
)


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 状态跨调用
# ----------------------------------------------------------------------
def test_cwd_persists_across_calls(tmp_path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()

    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            first = await shells.run(f"cd {sub.name}", timeout=20)
            assert first.startswith("exit_code=0"), first
            second = await shells.run("pwd", timeout=20)
            # 只比"末尾那一段"：Git Bash 报的是 POSIX 形式（`C:\...\Temp` → `/tmp`），
            # 拿 Python 的 Windows 路径去比会红——而这里真正要验的是"还在 sub 里"。
            assert second.rstrip().endswith("/sub"), second
            header = second.splitlines()[0]
            assert "cwd=" in header and header.rstrip().endswith("/sub"), header
        finally:
            await shells.close()

    _run(main())


def test_exported_variables_persist_across_calls(tmp_path) -> None:
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            await shells.run("export CA_PERSIST_TEST=42", timeout=20)
            out = await shells.run("echo $CA_PERSIST_TEST", timeout=20)
            assert "42" in out, out
        finally:
            await shells.close()

    _run(main())


def test_shell_functions_persist_across_calls(tmp_path) -> None:
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            await shells.run("ca_greet() { echo hello-from-function; }", timeout=20)
            out = await shells.run("ca_greet", timeout=20)
            assert "hello-from-function" in out, out
        finally:
            await shells.close()

    _run(main())


def test_state_is_per_manager_not_global(tmp_path) -> None:
    """两个会话各有各的 shell——A 的 cd 不该影响 B。"""
    other = tmp_path / "other"
    other.mkdir()

    async def main():
        one = PersistentShells(cwd=tmp_path)
        two = PersistentShells(cwd=tmp_path)
        try:
            await one.run(f"cd {other.name}", timeout=20)
            out = await two.run("pwd", timeout=20)
            assert not out.rstrip().endswith(f"/{other.name}"), (
                "另一个会话的 cwd 被串了"
            )
        finally:
            await one.close()
            await two.close()

    _run(main())


# ----------------------------------------------------------------------
# 结果形状
# ----------------------------------------------------------------------
def test_exit_code_and_cwd_are_reported(tmp_path) -> None:
    """cwd 必须回给模型：持久 shell 里"我现在在哪"是状态的一部分。

    不回的话模型只能在下一步先 `pwd` 一次——白花一轮。
    """
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            out = await shells.run("true", timeout=20)
            assert out.startswith("exit_code=0"), out
            assert "cwd=" in out, out
            failed = await shells.run("false", timeout=20)
            assert "exit_code=1" in failed, failed
        finally:
            await shells.close()

    _run(main())


def test_first_failed_command_keeps_real_exit_receipt_before_startup_notice(tmp_path):
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            result = await shells.run("false", timeout=20)
            assert result.startswith("exit_code=1"), result
            assert "已为该会话启动持久 shell" in result
        finally:
            await shells.close()
    _run(main())


def test_stderr_is_merged_like_the_one_shot_bash(tmp_path) -> None:
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            out = await shells.run("echo to-stderr >&2", timeout=20)
            assert "to-stderr" in out, out
        finally:
            await shells.close()

    _run(main())


def test_forged_sentinel_text_does_not_break_parsing(tmp_path) -> None:
    """命令自己输出一个"像哨兵"的字符串时，解析不能乱。"""
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            out = await shells.run('echo "__CA_SHELL_DONE_deadbeef 0 /fake"', timeout=20)
            assert "/fake" in out, out
            assert "exit_code=0" in out
            # 解析没乱：下一条命令照常
            again = await shells.run("echo after", timeout=20)
            assert "after" in again
        finally:
            await shells.close()

    _run(main())


def test_long_output_is_truncated_with_a_notice(tmp_path) -> None:
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            out = await shells.run("for i in $(seq 1 200); do echo filler-line-$i; done", timeout=20)
            assert "filler-line-1" in out
        finally:
            await shells.close()

    _run(main())


def test_truncation_does_not_leave_a_replacement_character() -> None:
    """按字节截断不能切出半个多字节字符。

    切口留下的 `U+FFFD`（乱码方块）会被模型当成输出内容的一部分去引用——
    少给一个字比给一个坏字符强（与 M46 的输出预算同一条取舍）。
    """
    text = "中" * 100                       # 每字 3 字节
    out = ps.PersistentShell._format(text, 0, "/tmp", max_output=100)
    assert "\ufffd" not in out
    assert "已截断" in out
    # 100 不是 3 的倍数 → 必须退到 99（33 个字），而不是留下半个字符
    assert out.count("中") == 33


# ----------------------------------------------------------------------
# 失败与超时：必须说清"状态没了"
# ----------------------------------------------------------------------
def test_timeout_kills_the_shell_and_says_state_is_lost(tmp_path) -> None:
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            await shells.run("export CA_LOST=1", timeout=20)
            out = await shells.run("sleep 30", timeout=1.5)
            assert "超时" in out and "状态" in out
            assert "消失" in out, "只说过时不说状态没了，模型会以为还能接着在原目录里干活"
            # 下一次调用起一只新的：说明里要提到"重新起"
            fresh = await shells.run("echo $CA_LOST", timeout=20)
            assert "已为该会话启动持久 shell" in fresh or "上一只" in fresh
            assert "1" not in fresh.split("exit_code")[-1][:20], "变量不该还在（那是另一只 shell）"
        finally:
            await shells.close()

    _run(main())


def test_command_that_exits_the_shell_is_reported_honestly(tmp_path) -> None:
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            out = await shells.run("exit 3", timeout=20)
            assert "已退出" in out, out
            assert "状态" in out
            # 再调一次：重起一只并说明
            again = await shells.run("echo back", timeout=20)
            assert "back" in again
        finally:
            await shells.close()

    _run(main())


# ----------------------------------------------------------------------
# 如实拒绝（不做假装持久的降级）
# ----------------------------------------------------------------------
def test_refuses_without_bash(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ps, "find_bash", lambda: None)

    async def main():
        shells = PersistentShells(cwd=tmp_path)
        out = await shells.run("echo hi", timeout=10)
        assert out == NO_BASH

    _run(main())


def test_refuses_in_docker_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ps, "sandbox_mode", lambda: "docker")

    async def main():
        shells = PersistentShells(cwd=tmp_path)
        out = await shells.run("echo hi", timeout=10)
        assert out == NO_DOCKER

    _run(main())


def test_closed_manager_refuses(tmp_path) -> None:
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        await shells.close()
        out = await shells.run("echo hi", timeout=10)
        assert "已经关闭" in out

    _run(main())


def test_kill_nowait_keeps_the_manager_usable(tmp_path) -> None:
    """用户点"停止"≠ 会话结束：那只 shell 要死，但下一条消息还能用持久 shell。"""

    async def main():
        shells = PersistentShells(cwd=tmp_path)
        try:
            await shells.run("export CA_ALIVE=1", timeout=20)
            assert shells.alive is True
            shells.kill_nowait()
            assert shells.alive is False
            out = await shells.run("echo usable-again", timeout=20)
            assert "usable-again" in out, "停止之后管理器不该永久失效"
        finally:
            await shells.close()

    _run(main())


# ----------------------------------------------------------------------
# 工具层
# ----------------------------------------------------------------------
def test_tool_reports_honestly_without_a_manager(tmp_path) -> None:
    ctx = ToolContext(session_id="s", workspace=tmp_path)
    out = _run(BashPersistentTool().execute({"command": "echo hi"}, ctx))
    assert "没有持久 shell" in out
    assert "真正执行" not in out


def test_tool_runs_in_the_session_shell(tmp_path) -> None:
    async def main():
        shells = PersistentShells(cwd=tmp_path)
        ctx = ToolContext(session_id="s", workspace=tmp_path, extra={"shells": shells})
        tool = BashPersistentTool()
        try:
            await tool.execute({"command": "export CA_TOOL=7"}, ctx)
            out = await tool.execute({"command": "echo $CA_TOOL"}, ctx)
            assert "7" in out, out
        finally:
            await shells.close()

    _run(main())


def test_tool_rejects_empty_command(tmp_path) -> None:
    from code_agent.tools.base import ToolError

    async def main():
        shells = PersistentShells(cwd=tmp_path)
        ctx = ToolContext(session_id="s", workspace=tmp_path, extra={"shells": shells})
        try:
            with pytest.raises(ToolError):
                await BashPersistentTool().execute({"command": "   "}, ctx)
        finally:
            await shells.close()

    _run(main())


def test_policy_is_the_same_bash_policy(tmp_path) -> None:
    """换个工具名不能换个策略：持久 shell 也是"执行命令"。"""
    from code_agent.permissions.hooks import PermissionManager
    from code_agent.permissions.policy import Decision

    pm = PermissionManager(workspace_root=tmp_path)
    ctx = ToolContext(session_id="s", workspace=tmp_path)

    denied = _run(pm.evaluate("bash_persistent", {"command": "sudo rm -rf /"}, ctx))
    assert denied.decision is Decision.deny
    allowed = _run(pm.evaluate("bash_persistent", {"command": "ls -la"}, ctx))
    assert allowed.decision is Decision.allow
    other = _run(pm.evaluate("bash_persistent", {"command": "npm run build"}, ctx))
    assert other.decision is Decision.need_approval


def test_runtime_wiring_is_present_in_source() -> None:
    """源码级守卫：shell 管理器要建在 runtime 上、一路传到 loop、并在释放时关闭。

    M42 的写租约就是接错层接成了死代码；M45 的任务表也踩过"挂 loop 上每段丢一次"。
    这一条把"接在哪一层"和"谁负责关"一起钉住。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "code_agent/sessions/manager.py").read_text(
        encoding="utf-8"
    )
    assert "self.shells = PersistentShells(cwd=self.workspace)" in src
    assert "shells=self.shells," in src
    assert "self.shells.kill_nowait()" in src        # cancel：只杀当前那只
    assert "self.shells.close_nowait()" in src       # shutdown：彻底关
    assert "runtime.shutdown()" in src               # 会话释放路径都调了


# ----------------------------------------------------------------------
# 回收（确定性断言，不靠 GC 时序）
# ----------------------------------------------------------------------
def test_release_closes_stdin_as_well() -> None:
    """`_release` 必须连 **stdin** 一起关：持久 shell 用了 `stdin=PIPE`。

    一次性命令的 stdin 是继承来的（不是管道），所以这条一直没被暴露；持久 shell
    一上就漏一个写管道 transport（实测：`unclosed transport ... stdin=<_ProactorWritePipeTransport>`）。

    这条断言刻意用**假对象**而不是真进程：真进程的回收依赖 GC 时序，
    同一段代码在不同运行里报 0 个或 2 个警告（我自己的探针就自相矛盾过），
    拿它当断言等于把噪声写进测试。
    """
    from code_agent.sandbox.executor import _release

    class FakeStream:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeProc:
        returncode = 0

        def __init__(self) -> None:
            self.stdin, self.stdout, self.stderr = FakeStream(), FakeStream(), FakeStream()
            self.closed = False

        async def wait(self) -> int:
            return 0

        def close(self) -> None:
            self.closed = True

    proc = FakeProc()
    _run(_release(proc))
    assert proc.stdin.closed, "stdin 没关 → 写管道 transport 会漏"
    assert proc.stdout.closed and proc.stderr.closed
    assert proc.closed


def test_kill_releases_the_taskkill_helper() -> None:
    """杀停用的 `taskkill` **自己也是子进程**，不回收就漏它持有的管道句柄。

    源码级断言：`_kill` 必须在 `killer.wait()` 之后调 `_release(killer)`。
    （同样是确定性的——真跑一次再数句柄会受 GC 时序影响。）
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "code_agent/sandbox/executor.py").read_text(
        encoding="utf-8"
    )
    assert "await _release(killer)" in src
