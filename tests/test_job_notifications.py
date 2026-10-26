"""后台任务完成通知的两条投递路（M45）。

任务跑完只是开始——**没人收的结算 = 模型永远不会知道的完成**。所以真正要钉的是
"通知怎么到模型面前"，而这有两条路（对齐 DSH）：

    繁忙的会话 → 在**下一个轮次边界**注入（不是等整轮跑完）
    空闲的会话 → 自己起一轮唤醒（有连续次数预算，用户说话就重置）

两条路共用 `jobs.drain_notices`（它会把任务标记成已通知），所以同一条通知
**只可能**走其中一条——既注入又唤醒就是纯噪音。
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.config import transcripts_dir
from code_agent.jobs.registry import JobRegistry, notice_text
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import BashTool
from code_agent.tools.registry import ToolRegistry


# ----------------------------------------------------------------------
# 路一：繁忙 → 轮次边界注入
# ----------------------------------------------------------------------
def _loop_with_notes(workspace, home, notes_by_call):
    """建一个 loop：`notes_by_call(i)` 给出第 i 次模型调用前该注入的通知。"""
    seen: list[list[dict]] = []
    calls = {"n": 0}

    class Spy(OfflineProvider):
        async def chat(self, messages, tools):
            seen.append([dict(m) for m in messages])
            return await super().chat(messages, tools)

    reg = ToolRegistry()
    reg.register(BashTool())
    provider = Spy(
        script=[
            OfflineProvider.tool_call_turn("bash", {"command": "echo tick"}),
            OfflineProvider.text_turn("收到"),
        ]
    )

    def pending():
        i = calls["n"]
        calls["n"] += 1
        return notes_by_call(i)

    loop = AgentLoop(
        provider=provider,
        registry=reg,
        permissions=PermissionManager(workspace_root=workspace),
        transcript=TranscriptStore(home / "transcripts"),
        workspace=workspace,
        on_pending_notes=pending,
    )
    return loop, seen


def test_pending_note_is_injected_before_the_next_model_call(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    note = "[系统] 后台任务 job_1 已结束 [done]。"
    loop, seen = _loop_with_notes(tmp_path, home, lambda i: [note] if i == 1 else [])

    asyncio.run(loop.run("跑个命令"))

    assert len(seen) == 2
    first = [m.get("content") for m in seen[0] if m.get("role") == "user"]
    second = [m.get("content") for m in seen[1] if m.get("role") == "user"]
    assert note not in first, "第一次调用前不该有通知"
    assert any(note == str(c) for c in second), "通知必须在下一次模型调用前到达"


def test_injected_note_is_recorded_quiet_in_the_transcript(tmp_path) -> None:
    """通知要落轨迹（刷新后仍在），并且标成 quiet —— 界面渲染成一行淡字，
    而不是伪装成"用户说的话"（与 M26 的续跑提示同一约定）。"""
    home = tmp_path / "home"
    home.mkdir()
    note = "[系统] 后台任务 job_2 已结束 [done]。"
    loop, _seen = _loop_with_notes(tmp_path, home, lambda i: [note] if i == 1 else [])

    asyncio.run(loop.run("跑个命令"))

    user_events = [
        e for e in loop.transcript.replay(loop.session_id)
        if e.type.value == "user_message" and e.payload.get("kind") == "job_notice"
    ]
    assert user_events, "注入的通知没有落轨迹"
    assert user_events[0].payload["text"] == note
    assert user_events[0].payload["quiet"] is True
    assert user_events[0].payload["auto"] is True


def test_broken_note_callback_does_not_break_the_turn(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    def boom():
        raise RuntimeError("note source exploded")

    loop, _seen = _loop_with_notes(tmp_path, home, lambda i: [])
    loop.on_pending_notes = boom
    result = asyncio.run(loop.run("跑个命令"))
    assert result.stop_reason == "end_turn", "通知回调异常不该打断这一轮"


# ----------------------------------------------------------------------
# runtime 接线：任务表 + 两条投递路
# ----------------------------------------------------------------------
@pytest.fixture
def runtime(agent_home):
    db = Database(agent_home / "jobs.db")
    manager = SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )
    uid = UserRepo(db).create("u_jobs", "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    return manager.get_runtime(meta["id"], uid)


async def _settled_job(rt, label="build"):
    """在 runtime 的任务表里放一个**已经结算**的任务（必须在自己的事件循环里 await）。"""
    async def runner(job):
        return "exit_code=0"

    job = rt.jobs.start(session_id=rt.session_id, kind="bash", label=label, runner=runner)
    await job._task
    return job


def test_runtime_hands_over_a_notice_exactly_once(runtime) -> None:
    """一条通知只交一次。这里把 runtime 装成"繁忙"，让通知走**下一步注入**那条路
    （空闲时它会被唤醒轮先收走——那也是一次，见下一条测试）。"""

    async def main():
        rt_task = asyncio.get_running_loop().create_task(asyncio.sleep(5))
        runtime._task = rt_task
        try:
            job = await _settled_job(runtime)
            notes = runtime._drain_job_notes()
            assert len(notes) == 1
            assert job.id in notes[0]
            assert runtime._drain_job_notes() == [], "同一条通知只交一次"
        finally:
            rt_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await rt_task

    asyncio.run(main())


def test_busy_runtime_does_not_wake_but_still_hands_over_the_notice(runtime) -> None:
    """繁忙时**不抢**：留给 loop 在轮次边界取走（抢会把通知塞进轮次的中间）。"""

    async def main():
        rt_task = asyncio.get_running_loop().create_task(asyncio.sleep(5))
        runtime._task = rt_task                     # 伪装成"正在跑"
        try:
            await _settled_job(runtime)
            assert runtime.busy is True
            # 结算回调在任务协程里被调用过：繁忙路径不该起唤醒轮
            assert runtime._task is rt_task, "繁忙时不该把 _task 换成一个唤醒轮"
            notes = runtime._drain_job_notes()
            assert len(notes) == 1
        finally:
            rt_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await rt_task

    asyncio.run(main())


def test_idle_runtime_wakes_a_round_for_a_settled_job(runtime, monkeypatch) -> None:
    """空闲时必须叫醒：否则那条"任务完成了"只有用户自己看得到。"""
    woken: list[str] = []

    async def fake_wake(prompt):
        woken.append(prompt)
        return None

    monkeypatch.setattr(runtime, "_run_wake", fake_wake)

    async def main():
        runtime._consecutive_wakes = 0
        await _settled_job(runtime)
        # 唤醒是通过 create_task 调度的，给它一次机会跑
        for _ in range(5):
            await asyncio.sleep(0)
        assert woken, "空闲会话没有为完成的任务起一轮"
        assert "job_" in woken[0] and "job_output" in woken[0]

    asyncio.run(main())


def test_wake_budget_exhaustion_stashes_instead_of_waking(runtime, monkeypatch) -> None:
    """预算用尽 → 不再自动起轮，但**通知不能丢**（下次模型调用带上）。"""
    woken: list[str] = []

    async def fake_wake(prompt):
        woken.append(prompt)

    monkeypatch.setattr(runtime, "_run_wake", fake_wake)
    from code_agent.sessions import manager as manager_mod

    monkeypatch.setattr(manager_mod, "MAX_CONSECUTIVE_WAKES", 0)

    async def main():
        await _settled_job(runtime)
        for _ in range(5):
            await asyncio.sleep(0)
        assert woken == [], "预算为 0 时不该自动起轮"
        notes = runtime._drain_job_notes()
        assert len(notes) == 1, "通知被预算吞掉了——那等于任务完成这件事消失了"

    asyncio.run(main())


def test_awaited_job_never_reaches_the_runtime_notice_path(runtime) -> None:
    """模型自己用 wait 收走的任务，不该再走通知（DSH 的 awaited 语义）。"""

    async def runner(job):
        return "done"

    async def main():
        job = runtime.jobs.start(
            session_id=runtime.session_id, kind="bash", label="waited", runner=runner
        )
        await runtime.jobs.read(job.id, wait=True, timeout_ms=5000)
        assert job.awaited is True
        assert runtime._drain_job_notes() == []
        # 顺带确认通知文案本身是"可执行的下一步"，而不是一句感叹
        assert "job_output" in notice_text(job)

    asyncio.run(main())


def test_session_runtime_wiring_is_present_in_source() -> None:
    """源码级守卫：任务表必须建在 runtime 上、且一路传到 loop。

    真正的代价（每个 prompt 都丢掉在跑的任务）在单测里很贵，而"接线接在哪一层"
    恰恰是这类改动最容易错的地方——M42 的写租约就是接错层接成了死代码。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "code_agent/sessions/manager.py").read_text(
        encoding="utf-8"
    )
    assert "self.jobs = JobRegistry(on_settle=self._on_job_settled)" in src
    assert "jobs=self.jobs," in src
    assert "on_pending_notes=self._drain_job_notes," in src
