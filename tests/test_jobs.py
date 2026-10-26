"""后台任务（M45，对齐 DSH 的 `dsh-jobs` / `dsh-tool-jobs`）。

这个功能的价值全在"模型不必再轮询"：过去一条 `npm install` 要么卡住整轮、
要么撞 30 秒超时被杀，模型只能拆成"起进程 + 反复查"——而每次查都要烧一轮模型调用。

所以断言的重点不是"能起任务"，而是那几条容易被写漏的语义：

1. **输出是有损的，而且必须说自己是有的损的**（环满丢最旧的块）；
2. **已被等待收走的结算不再通知**（否则"我刚读完它，它又来叫醒我一次"）；
3. **模型自己杀掉的任务不再通知**（它当然知道自己杀了）；
4. **杀停是杀进程树，不是取消协程**（取消会漏 transport，见 executor 文件头）；
5. **没有任务表就说没有**，不假装接下了任务。
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from code_agent.jobs.registry import (
    DONE,
    KILLED,
    RUNNING,
    JobRegistry,
    notice_text,
)
from code_agent.sandbox.executor import run_shell
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.job_tools import (
    NO_REGISTRY,
    JobKillTool,
    JobListTool,
    JobOutputTool,
)
from code_agent.tools.builtin.tools_builtin import BashTool


def _sleep_cmd(seconds: float) -> str:
    """跨 shell 都能跑的"睡一会儿"（bash 与 cmd.exe 的引号规则不同）。"""
    exe = sys.executable
    if os.name == "nt":
        return f'"{exe}" -c "import time; time.sleep({seconds})"'
    return f"'{exe}' -c 'import time; time.sleep({seconds})'"


def _ctx(workspace, jobs=None) -> ToolContext:
    return ToolContext(session_id="s_job", workspace=workspace, extra={"jobs": jobs} if jobs else {})


# ----------------------------------------------------------------------
# 输出环：有损必须说清楚
# ----------------------------------------------------------------------
def test_ring_keeps_the_tail_and_reports_what_it_dropped() -> None:
    reg = JobRegistry(max_ring_bytes=1024)

    async def main():
        async def runner(job):
            return ""

        job = reg.start(session_id="s", kind="test", label="x", runner=runner)
        for i in range(10):
            job.append(f"chunk-{i}-".encode() * 40)      # 每块 320 字节
        text, missed = job.take_new()
        assert job.dropped_bytes > 0
        assert missed == job.dropped_bytes, "丢掉的字节数必须如实报给读者"
        assert "chunk-9-" in text, "环要保留尾部（最新输出），而不是头部"
        # 第二次读：自那之后没有新输出
        again, missed2 = job.take_new()
        assert again == "" and missed2 == 0
        await job._task

    asyncio.run(main())


# ----------------------------------------------------------------------
# 注册表语义
# ----------------------------------------------------------------------
def test_start_returns_immediately_with_a_running_job(tmp_path) -> None:
    reg = JobRegistry()

    async def runner(job):
        await asyncio.sleep(0.05)
        return "exit_code=0\nhi"

    async def main():
        job = reg.start(session_id="s", kind="bash", label="echo hi", runner=runner)
        assert job.status == RUNNING           # 立刻返回，不等它跑完
        await asyncio.sleep(0.15)
        read = await reg.read(job.id)
        assert read is not None
        assert read.status == DONE
        assert "hi" in read.text and "任务结果" in read.text
        assert "[status: done]" in read.text

    asyncio.run(main())


def test_settled_result_is_delivered_exactly_once() -> None:
    """结算文本只给一次（DSH：结算后的第一次读取恰好携带一次任务的结果）。"""
    reg = JobRegistry()

    async def runner(job):
        job.append(b"partial\n")
        return "final-answer"

    async def main():
        job = reg.start(session_id="s", kind="bash", label="x", runner=runner)
        await asyncio.sleep(0.05)
        first = await reg.read(job.id)
        second = await reg.read(job.id)
        assert "final-answer" in first.text
        assert "final-answer" not in second.text

    asyncio.run(main())


def test_waited_job_is_not_notified_later() -> None:
    """已经用 wait=true 收走的结算，不再进通知队列（否则是纯噪音）。"""
    reg = JobRegistry()

    async def runner(job):
        await asyncio.sleep(0.05)
        return "done-text"

    async def main():
        job = reg.start(session_id="s", kind="bash", label="x", runner=runner)
        await reg.read(job.id, wait=True, timeout_ms=2000)
        assert job.awaited is True
        assert reg.drain_notices("s") == []

    asyncio.run(main())


def test_model_killed_job_is_not_notified() -> None:
    reg = JobRegistry()
    killed = []

    async def runner(job):
        await asyncio.sleep(5)
        return "never"

    async def main():
        job = reg.start(
            session_id="s", kind="bash", label="x", runner=runner, kill=lambda: killed.append(1)
        )
        assert reg.kill(job.id, "user asked", by_model=True) == "ok"
        assert killed == [1], "杀停要调运行方注册的回调（对 bash 就是杀进程树）"
        job.status = KILLED           # 模拟真正的结算
        job.finished_at = 0.0
        assert reg.drain_notices("s") == [], "模型自己杀的，不该再叫醒它"
        job._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job._task

    asyncio.run(main())


def test_notices_are_sent_once_and_can_be_exempted(tmp_path) -> None:
    reg = JobRegistry()
    jobs = []

    async def runner(job):
        return f"result-{job.label}"

    async def main():
        for i in range(3):
            jobs.append(reg.start(session_id="s", kind="bash", label=f"j{i}", runner=runner))
        await asyncio.sleep(0.1)
        first = reg.drain_notices("s")
        assert len(first) == 3
        assert reg.drain_notices("s") == [], "同一条通知只发一次"

    asyncio.run(main())


def test_kill_outcomes_and_unknown_ids() -> None:
    reg = JobRegistry()

    async def runner(job):
        await asyncio.sleep(5)

    async def main():
        job = reg.start(session_id="s", kind="bash", label="x", runner=runner)
        assert reg.kill("nope") == "not-found"
        assert reg.kill(job.id) == "ok"
        assert reg.kill(job.id) in ("ok", "already-finished")
        job.status = DONE
        assert reg.kill(job.id) == "already-finished"
        job._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job._task

    asyncio.run(main())


def test_job_table_is_bounded_per_session() -> None:
    """任务表会淘汰最老的**已结算**任务，但绝不淘汰正在跑的。"""
    reg = JobRegistry()

    async def runner(job):
        return "ok"

    async def main():
        ids = []
        for i in range(60):
            job = reg.start(session_id="s", kind="bash", label=f"j{i}", runner=runner)
            ids.append(job.id)
            await asyncio.sleep(0)          # 让它有机会结算
        await asyncio.sleep(0.2)
        listed = reg.list("s")
        assert len(listed) <= 50
        assert reg.get(ids[-1]) is not None, "最新的一定还在"

    asyncio.run(main())


def test_running_job_survives_pruning() -> None:
    reg = JobRegistry()

    async def slow(job):
        await asyncio.sleep(1)

    async def quick(job):
        return "ok"

    async def main():
        running = reg.start(session_id="s", kind="bash", label="long", runner=slow)
        for i in range(60):
            reg.start(session_id="s", kind="bash", label=f"q{i}", runner=quick)
            await asyncio.sleep(0)
        assert reg.get(running.id) is not None, "正在跑的任务绝不能被淘汰"
        assert any(j.status == RUNNING for j in reg.list("s"))
        running._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running._task

    asyncio.run(main())


# ----------------------------------------------------------------------
# 执行器：流式回调 + 无超时 + 外部杀停
# ----------------------------------------------------------------------
def test_run_shell_streams_chunks_to_the_callback(tmp_path) -> None:
    seen: list[bytes] = []
    out = asyncio.run(
        run_shell("echo streamed-line", cwd=tmp_path, on_chunk=seen.append, timeout=30)
    )
    assert "streamed-line" in out
    assert b"".join(seen).decode("utf-8", "replace").strip() == "streamed-line"


def test_broken_callback_does_not_break_the_command(tmp_path) -> None:
    """回调抛异常不能影响命令本身——管道链路在 Windows 上极脆（见 executor 文件头）。"""
    def boom(_chunk: bytes) -> None:
        raise RuntimeError("bad sink")

    out = asyncio.run(run_shell("echo still-works", cwd=tmp_path, on_chunk=boom, timeout=30))
    assert "still-works" in out


def test_run_shell_without_timeout_runs_to_completion(tmp_path) -> None:
    out = asyncio.run(run_shell("echo no-timeout-ok", cwd=tmp_path, timeout=None))
    assert "no-timeout-ok" in out


def test_kill_event_stops_the_process_without_cancelling(tmp_path) -> None:
    """外部杀停 = 杀进程树（不是 cancel 协程），返回文本要如实说明。"""
    async def main():
        event = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, event.set)
        return await run_shell(_sleep_cmd(10), cwd=tmp_path, timeout=None, kill_event=event)

    out = asyncio.run(main())
    assert "外部终止" in out, out
    assert "exit_code=" in out


# ----------------------------------------------------------------------
# bash 后台模式 + 三个 job 工具
# ----------------------------------------------------------------------
def test_background_without_a_registry_says_so(tmp_path) -> None:
    """没有任务表就如实说没有——不假装接下了任务。"""
    out = asyncio.run(
        BashTool().execute(
            {"command": "echo hi", "run_in_background": True}, _ctx(tmp_path, jobs=None)
        )
    )
    assert "没有后台任务表" in out
    assert "started background job" not in out


def test_background_bash_starts_a_job_and_streams_output(tmp_path) -> None:
    reg = JobRegistry()

    async def main():
        out = await BashTool().execute(
            {"command": "echo from-background", "run_in_background": True},
            _ctx(tmp_path, jobs=reg),
        )
        assert "started background job" in out
        job = reg.list("s_job")[0]
        read = await reg.read(job.id, wait=True, timeout_ms=15000)
        assert "from-background" in read.text
        assert read.status == DONE

    asyncio.run(main())


def test_background_bash_ignores_the_timeout_budget(tmp_path) -> None:
    """后台任务不设执行器超时：慢命令不该因为 timeout 参数被杀。"""
    reg = JobRegistry()

    async def main():
        await BashTool().execute(
            {"command": _sleep_cmd(1.5), "timeout": 1, "run_in_background": True},
            _ctx(tmp_path, jobs=reg),
        )
        job = reg.list("s_job")[0]
        read = await reg.read(job.id, wait=True, timeout_ms=20000)
        assert read.status == DONE, "后台任务被 timeout 参数杀了（不该）"

    asyncio.run(main())


def test_job_kill_stops_a_real_background_command(tmp_path) -> None:
    reg = JobRegistry()

    async def main():
        await BashTool().execute(
            {"command": _sleep_cmd(30), "run_in_background": True}, _ctx(tmp_path, jobs=reg)
        )
        job = reg.list("s_job")[0]
        out = await JobKillTool().execute(
            {"job_id": job.id, "reason": "测试里不需要它了"}, _ctx(tmp_path, jobs=reg)
        )
        assert "已请求停止" in out
        read = await reg.read(job.id, wait=True, timeout_ms=20000)
        assert read.status == KILLED
        assert "测试里不需要它了" in read.text

    asyncio.run(main())


def test_job_tools_report_honestly_without_a_registry(tmp_path) -> None:
    ctx = _ctx(tmp_path, jobs=None)
    for tool in (JobOutputTool(), JobListTool(), JobKillTool()):
        args = {"job_id": "job_x"} if tool.name != "job_list" else {}
        assert asyncio.run(tool.execute(args, ctx)) == NO_REGISTRY


def test_job_output_and_list_and_kill_messages(tmp_path) -> None:
    reg = JobRegistry()

    async def runner(job):
        job.append(b"line-1\n")
        await asyncio.sleep(0.05)
        return "exit_code=0"

    async def main():
        ctx = _ctx(tmp_path, jobs=reg)
        job = reg.start(session_id="s_job", kind="bash", label="demo", runner=runner)
        listed = await JobListTool().execute({}, ctx)
        assert job.id in listed and "[bash]" in listed

        other = await JobOutputTool().execute({"job_id": "job_missing"}, ctx)
        assert "找不到任务" in other and job.id in other   # 顺手给出可用 id

        await asyncio.sleep(0.1)
        read = await JobOutputTool().execute({"job_id": job.id, "wait": True}, ctx)
        assert "line-1" in read and "[status: done]" in read

        killed = await JobKillTool().execute({"job_id": job.id}, ctx)
        assert "已经结束" in killed

    asyncio.run(main())


def test_notice_text_points_at_the_right_next_action() -> None:
    reg = JobRegistry()

    async def runner(job):
        return "ok"

    async def main():
        job = reg.start(session_id="s", kind="bash", label="build", runner=runner)
        await asyncio.sleep(0.05)
        text = notice_text(job)
        assert job.id in text and "job_output" in text and "job_kill" in text
        # 通知必须明确"不需要立刻处理"，否则模型会放下手头的活去读它（DSH 同款措辞）
        assert "不需要在下一步立刻处理" in text

    asyncio.run(main())
