"""不间断执行：max_turns 自动续跑 + 停止/审批恢复。

背景（真机踩到的坑）：deepseek-v4-pro 跑长任务，bash 反复要审批，
段长（当时是 25 轮，现已 60）耗尽直接"强制结束"——任务断在半截；用户在 busy 期间发消息只收到
一句 "busy"，前端还把 busy 状态错误清掉，看起来像卡死。

这里断言的是续跑语义本身：
- 达到 max_turns ≠ 任务失败，自动从轨迹重建历史接着跑；
- 续跑次数有上限（防止无限烧 token），耗尽才交还控制权；
- 用户主动停止时，挂起审批必须一并 fail closed。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from code_agent.agent import loop as loop_module
from code_agent.agent.loop import DEFAULT_MAX_TURNS

#: 测试把"段长"钉成 3 轮：只验证续跑语义，不必真跑生产默认的 60 轮
#: （`_build_loop` 是**调用时**读 `loop_module.DEFAULT_MAX_TURNS`，所以这里 monkeypatch 有效）。
SEG = 3
from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.config import transcripts_dir
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions import manager as manager_module
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore


class AlwaysToolProvider(OfflineProvider):
    """永远要求调工具——用于把 loop 逼到 max_turns（检查点）。

    这里调的是 **read_file**（进程内逐行读文件，每轮有新证据），不是 bash：
    这些用例只验证"续跑语义"，用 bash 会让每一轮都起一次子进程 ——
    真机实测每轮 ~3 秒，段长从 25 提到 60 之后，一条续跑用例就要跑 3 分钟，
    整个测试套直接超时。换成 read_file 后每轮 ~1ms，语义完全一样（有工具调用即可）。
    """

    def __init__(self):
        super().__init__(script=[])
        self.chat_calls = 0

    async def chat(self, messages, tools):
        self.chat_calls += 1
        if self.chat_calls <= self.tool_until:
            return OfflineProvider.tool_call_turn("read_file", {
                "path": NOTE_FILE, "offset": self.chat_calls, "limit": 1,
            })
        return OfflineProvider.text_turn("任务完成，这是结论。")

    tool_until = 10 ** 9


#: 上面那个 provider 逐行读的文件（重复同一内容的收敛另见 test_loop_convergence）
NOTE_FILE = "note.txt"


@pytest.fixture
def manager(agent_home):
    db = Database(agent_home / "t.db")
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )


def _make_runtime(manager: SessionManager, user_id="u_cont"):
    db = manager.session_repo.db
    users = UserRepo(db)
    existing = db.query_one("SELECT id FROM users WHERE username=?", (user_id,))
    uid = existing["id"] if existing else users.create(user_id, "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    rt = manager.get_runtime(meta["id"], uid)
    # AlwaysToolProvider 每轮都会 read_file NOTE_FILE：先把它写进工作区
    try:
        (rt.workspace / NOTE_FILE).write_text(
            "\n".join(f"内容 {index}" for index in range(1, 1025)), encoding="utf-8",
        )
    except OSError:
        pass
    return rt


def _user_messages(runtime) -> list[str]:
    events = runtime.transcript.replay(runtime.session_id)
    return [
        e.payload.get("text", "")
        for e in events
        if e.type.value == "user_message"
    ]


def test_auto_continue_exhausts_quota_then_stops(manager, monkeypatch) -> None:
    """永远调工具：每次 max_turns 触发一次续跑，配额耗尽才真正停。"""
    monkeypatch.setattr(manager_module, "MAX_CONTINUATIONS", 2)
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", SEG)
    rt = _make_runtime(manager)
    rt.router.provider_override = AlwaysToolProvider()

    result = asyncio.run(rt.submit("无限工具任务"))

    assert result.stop_reason == "max_turns"
    msgs = _user_messages(rt)
    # 原始 prompt + 2 条系统续跑
    assert len(msgs) == 3
    assert "自动继续（第 1 次" in msgs[1]
    assert "自动继续（第 2 次" in msgs[2]
    assert "重复劳动" in msgs[1]
    # 每段都跑满了 max_turns（续跑不是提前放弃）
    assert result.turns == SEG


def test_auto_continue_until_task_finishes(manager, monkeypatch) -> None:
    """续跑后任务在配额内完成：收口 end_turn，不浪费剩余配额。"""
    monkeypatch.setattr(manager_module, "MAX_CONTINUATIONS", 3)
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", SEG)
    rt = _make_runtime(manager)
    provider = AlwaysToolProvider()
    provider.tool_until = SEG + 2            # 第一段跑满 SEG 轮工具，第二段再 2 轮后收口
    rt.router.provider_override = provider

    result = asyncio.run(rt.submit("长任务"))

    assert result.stop_reason == "end_turn"
    assert result.text == "任务完成，这是结论。"
    msgs = _user_messages(rt)
    assert len(msgs) == 2                          # 原始 prompt + 1 条续跑
    assert "自动继续（第 1 次" in msgs[1]
    # provider 一共被调 SEG + 3 次（第二段 2 轮工具 + 1 轮文本）
    assert provider.chat_calls == SEG + 3


def test_zero_continuations_restores_old_behavior(manager, monkeypatch) -> None:
    """配额 0 = 关闭续跑，保持旧的"强制收口"行为。"""
    monkeypatch.setattr(manager_module, "MAX_CONTINUATIONS", 0)
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", SEG)
    rt = _make_runtime(manager)
    rt.router.provider_override = AlwaysToolProvider()

    result = asyncio.run(rt.submit("一次性任务"))

    assert result.stop_reason == "max_turns"
    assert len(_user_messages(rt)) == 1


def test_continuous_default_keeps_one_user_request_past_the_old_checkpoint(manager, monkeypatch):
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", 0)
    rt = _make_runtime(manager)
    provider = AlwaysToolProvider()
    provider.tool_until = 62
    rt.router.provider_override = provider
    result = asyncio.run(rt.submit("持续完成任务"))
    assert result.stop_reason == "end_turn" and provider.chat_calls == 63
    assert _user_messages(rt) == ["持续完成任务"]
    events = rt.transcript.replay(rt.session_id)
    assert not any(event.payload.get("code") == "segment_boundary" for event in events)


@pytest.mark.parametrize("segment", [0, 2])
def test_external_hard_cap_stops_at_the_exact_total_without_overshoot(manager, monkeypatch, segment):
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", segment)
    monkeypatch.setattr(manager_module, "MAX_TOTAL_TURNS", 3)
    monkeypatch.setattr(manager_module, "MAX_CONTINUATIONS", 5)
    rt = _make_runtime(manager)
    provider = AlwaysToolProvider()
    rt.router.provider_override = provider
    result = asyncio.run(rt.submit("达到配置预算时停止"))
    assert result.stop_reason == "max_turns" and result.segment_boundary
    assert provider.chat_calls == 3
    assert result.usage.input_tokens == 42 * 3


def test_continuation_history_sees_previous_segment(manager, monkeypatch) -> None:
    """续跑的历史必须包含上一段的工具结果——模型才知道中断点在哪。"""
    monkeypatch.setattr(manager_module, "MAX_CONTINUATIONS", 1)
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", SEG)
    rt = _make_runtime(manager)

    seen_sizes: list[int] = []

    class SpyProvider(AlwaysToolProvider):
        async def chat(self, messages, tools):
            seen_sizes.append(len(messages))
            return await super().chat(messages, tools)

    rt.router.provider_override = SpyProvider()
    asyncio.run(rt.submit("带历史的任务"))

    # 第一段第一条消息最短；续跑段的入参消息数必须明显更多（含上一段轨迹）
    assert seen_sizes[0] < seen_sizes[SEG]


def test_cancel_denies_pending_approvals(agent_home) -> None:
    """用户停止时：挂起审批的 future 被拒绝、DB 落 denied——不留悬空 pending。"""
    db = Database(agent_home / "t.db")
    approval_repo = ApprovalRepo(db)
    mgr = SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=approval_repo,
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )
    rt = _make_runtime(mgr)

    async def scenario():
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        aid = approval_repo.create(rt.session_id, "u_cont", "bash", "echo x", "reason")
        rt._pending_approvals[aid] = fut
        rt.cancel()
        assert fut.done() and fut.result() is False
        row = approval_repo.get(aid)
        assert row["status"] == "denied"
        assert "cancel" in (row["decided_by"] or "")

    asyncio.run(scenario())
    assert rt._pending_approvals == {}


# ----------------------------------------------------------------------
# API：挂起审批恢复（前端刷新后的兜底）
# ----------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / "code-agent-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("CODE_AGENT_HOME", str(home))
    for key in ("CODE_AGENT_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from code_agent.api.app import create_app

    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_pending_approval_endpoint_ignores_orphaned_request(client) -> None:
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    # 没有挂起审批：返回空对象（不是 404）
    assert client.get(f"/api/v1/sessions/{sid}/approvals/pending", headers=h).json() == {}

    approvals = client.app.state.approvals
    # 注意 user_id 是真实 uid（u_ 开头），不是 username——决策端点按它鉴权
    uid = client.get("/api/v1/auth/me", headers=h).json()["id"]
    aid = approvals.create(sid, uid, "bash", "sudo rm -rf /", "命中黑名单外的命令")
    body = client.get(f"/api/v1/sessions/{sid}/approvals/pending", headers=h).json()
    # A row without a live waiter (e.g. after a restart) cannot execute anything.
    # Do not offer the user an approval which can no longer be delivered.
    assert body == {}

    # 决策后不再返回（前端据此收起弹窗）
    invalid = client.post(f"/api/v1/approvals/{aid}/decide", json={"approved": "false"}, headers=h)
    assert invalid.status_code == 422
    result = client.post(f"/api/v1/approvals/{aid}/decide", json={"approved": True}, headers=h)
    assert result.json()["delivered"] is False
    assert approvals.get(aid)["status"] == "denied"
    assert client.get(f"/api/v1/sessions/{sid}/approvals/pending", headers=h).json() == {}


def test_pending_approval_excludes_stale_rows(client) -> None:
    """超时窗口外创建的 pending 不该弹给用户（马上会被 fail closed 拒掉）。"""
    import sqlite3

    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    approvals = client.app.state.approvals
    uid2 = client.get("/api/v1/auth/me", headers=h).json()["id"]
    aid = approvals.create(sid, uid2, "bash", "echo old", "旧请求")

    # 把 created_at 拨回 1 小时前
    db_path = client.app.state.db._conn
    db_path.execute(
        "UPDATE approvals SET created_at=datetime('now', '-1 hour') WHERE id=?", (aid,)
    )
    db_path.commit()

    assert client.get(f"/api/v1/sessions/{sid}/approvals/pending", headers=h).json() == {}
    # 表里仍是 pending（由 wait_for 超时负责收口），只是不再给前端弹
    assert approvals.get(aid)["status"] == "pending"
    _ = sqlite3  # 仅用于上面直接 SQL
