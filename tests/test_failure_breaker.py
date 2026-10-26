"""重复失败熔断（M29 · 借自 AgentRewind 的终止判据）。

AgentRewind 的结论：一个失败的尝试被原样重试 N 次后，第 N+1 次成功的概率极低，
继续跑只是烧 token。这里断言的是这条判据本身——**同一个调用连续失败到阈值就停**，
而不是"任务失败"：

- 指纹把同一次调用认出来：键顺序、路径分隔符、空白、数字写法都不影响；
- 连续 ≠ 累计：中间插进别的失败就重置（那是另一条排错路线）；
- 成功清掉连续计数（这条路通了）；
- 触发后循环以 `repeated_failure` 停下，并留下一条可回放的说明。

阈值本身可用 CODE_AGENT_FAILURE_STREAK 覆盖（设 0 关闭）。
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.agent import breaker as breaker_module
from code_agent.agent import loop as loop_module
from code_agent.agent.breaker import FailureBreaker, fingerprint
from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.config import transcripts_dir
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore

#: 命中 bash 黑名单 → 必定 deny（不挂审批，测试不会卡在审批超时上）
DENIED_CMD = {"command": "sudo rm -rf /"}


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def test_fingerprint_ignores_noise_but_keeps_identity() -> None:
    """同一次调用必须得到同一个指纹——否则熔断永远不触发（每次都是"新失败"）。"""
    a = fingerprint("read_file", {"path": "pkg\\mathx.py", "offset": 10})
    b = fingerprint("read_file", {"offset": 10.0, "path": "  pkg/mathx.py "})
    assert a == b, (a, b)

    c = fingerprint("read_file", {"path": "pkg/other.py", "offset": 10})
    assert a != c, "换了文件就是另一次调用"

    d = fingerprint("read_file", {"path": "pkg/mathx.py", "offset": 10, "timeout": 30})
    assert a == d, "易变参数（timeout 之类）不参与指纹"


def test_fingerprint_falls_back_to_tool_name() -> None:
    """没有参数时指纹退化成工具名：同一个工具反复失败也是一种原地打转。"""
    assert fingerprint("bash", {}) == "bash"
    assert fingerprint("bash", None) == "bash"


# ---------------------------------------------------------------------------
# 计数器语义
# ---------------------------------------------------------------------------
def test_breaker_trips_on_consecutive_identical_failures() -> None:
    b = FailureBreaker(limit=3)
    fp = fingerprint("bash", DENIED_CMD)
    assert b.observe(fp, tool="bash", reason="denied") is False
    assert b.observe(fp, tool="bash", reason="denied") is False
    assert b.observe(fp, tool="bash", reason="denied") is True
    assert b.tripped == fp
    assert b.streaks[fp] == 3
    assert "连续 3 次" in b.report() and "停" in b.report()


def test_breaker_resets_on_a_different_failure() -> None:
    """连续 ≠ 累计：中间失败的是别的东西，说明在换路线排查，不该熔断。"""
    b = FailureBreaker(limit=2)
    a = fingerprint("bash", DENIED_CMD)
    c = fingerprint("read_file", {"path": "missing.txt"})
    assert b.observe(a, tool="bash", reason="denied") is False
    assert b.observe(c, tool="read_file", reason="no such file") is False
    assert b.observe(a, tool="bash", reason="denied") is False   # 连续被打断，重新计
    assert b.observe(a, tool="bash", reason="denied") is True
    assert b.totals[a] == 3 and b.streaks[a] == 2


def test_breaker_success_clears_the_streak() -> None:
    b = FailureBreaker(limit=2)
    a = fingerprint("bash", DENIED_CMD)
    b.observe(a, tool="bash", reason="denied")
    b.succeeded("bash")
    assert b.observe(a, tool="bash", reason="denied") is False
    assert b.observe(a, tool="bash", reason="denied") is True


def test_breaker_can_be_disabled() -> None:
    b = FailureBreaker(limit=0)
    fp = fingerprint("bash", DENIED_CMD)
    for _ in range(50):
        assert b.observe(fp, tool="bash", reason="denied") is False
    assert b.tripped == ""


# ---------------------------------------------------------------------------
# 接到 loop 上
# ---------------------------------------------------------------------------
class AlwaysDeniedProvider(OfflineProvider):
    """每轮都调同一条被拒命令：指纹完全相同，注定触发熔断。"""

    def __init__(self) -> None:
        super().__init__(script=[])

    async def chat(self, messages, tools):
        return OfflineProvider.tool_call_turn("bash", DENIED_CMD)


@pytest.fixture
def manager(agent_home):
    db = Database(agent_home / "breaker.db")
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )


def _runtime(manager: SessionManager):
    db = manager.session_repo.db
    users = UserRepo(db)
    uid = users.create("u_breaker", "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    return manager.get_runtime(meta["id"], uid)


def test_loop_stops_with_repeated_failure(manager, monkeypatch) -> None:
    """同一个失败连着 3 次 → 停下交回人，而不是把剩下的段长烧在重试上。"""
    monkeypatch.setattr(breaker_module, "DEFAULT_STREAK", 3)
    # 段长给足 10 轮：能停在熔断而不是段末，才说明"停"这件事是熔断做的
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", 10)
    rt = _runtime(manager)
    rt.router.provider_override = AlwaysDeniedProvider()

    result = asyncio.run(rt.submit("反复失败的任务"))

    assert result.stop_reason == "repeated_failure", result.stop_reason
    assert result.turns == 3, "应在第 3 次失败处停下，而不是跑满段长"
    events = rt.transcript.replay(rt.session_id)
    stopped = [e for e in events if e.payload.get("code") == "repeated_failure"]
    assert len(stopped) == 1, "熔断说明必须落轨迹（刷新后仍能看到为什么停的）"
    msg = stopped[0].payload["message"]
    assert "连续 3 次" in msg and "bash" in msg
    assert stopped[0].payload["streak"] == 3


def test_advisory_repetition_allows_the_model_to_adjust_and_deliver(manager, monkeypatch):
    monkeypatch.setattr(breaker_module, "DEFAULT_STREAK", 0)
    rt = _runtime(manager)
    provider = OfflineProvider(script=[
        *(OfflineProvider.tool_call_turn("read_file", {"path": "missing.txt"}) for _ in range(6)),
        OfflineProvider.text_turn("文件不存在，已确认当前无法读取。"),
    ])
    rt.router.provider_override = provider
    result = asyncio.run(rt.submit("查找文件并报告实际结果"))
    assert result.stop_reason == "end_turn" and provider.calls == 7
    events = rt.transcript.replay(rt.session_id)
    assert len([event for event in events if event.type.value == "tool_result"]) == 6
    assert not any(event.payload.get("code") == "repeated_failure" for event in events)
    reminders = [event for event in events if event.payload.get("code") == "repeat_tool_reminder"]
    assert len(reminders) == 2 and all(event.payload.get("quiet") for event in reminders)


def test_loop_keeps_going_when_fingerprints_differ(manager, monkeypatch) -> None:
    """每次调用的参数都不同 → 不算原地打转，循环照常推进到收口。"""
    monkeypatch.setattr(breaker_module, "DEFAULT_STREAK", 3)
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", 10)

    class VaryingProvider(OfflineProvider):
        def __init__(self) -> None:
            super().__init__(script=[])
            self.n = 0

        async def chat(self, messages, tools):
            self.n += 1
            if self.n <= 4:
                # 换不同的路径：每次都是"新失败"，不该熔断
                return OfflineProvider.tool_call_turn("read_file", {"path": f"missing-{self.n}.txt"})
            return OfflineProvider.text_turn("查完了，结论如下。")

    rt = _runtime(manager)
    rt.router.provider_override = VaryingProvider()

    result = asyncio.run(rt.submit("换着试"))

    assert result.stop_reason == "end_turn", result.stop_reason
    assert not [e for e in rt.transcript.replay(rt.session_id)
                if e.payload.get("code") == "repeated_failure"]
