"""三处真 bug 的回归用例（A 组修复）。

背景：这三条都是"注释说会降级/会累加，代码其实没做到"的典型——
它们在正常路径上完全看不出来，只有走到异常/多段/失败路径才暴露：

1. `sessions/manager.py` 里 4 处 `logger.exception(...)` 用的 `logger` **从未定义**
   （该文件没有 `import logging`）→ 本该"忽略并降级"的兜底分支反而抛 `NameError`；
2. 续跑只把**最后一段**的 usage 抄进返回值与 usage 表，
   前面每段的用量都丢了；
3. `no_failures` 判据的失败数没有从实际轨迹读取 → 这条判据恒为真，
   模型会拿着"全部满足"去收口。
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.agent import loop as loop_module
from code_agent.agent.router import ModelRouter
from code_agent.cli import build_registry
from code_agent.config import transcripts_dir
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions import manager as manager_module
from code_agent.sessions.acceptance import failure_count, since_last_prompt
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore

from tests.test_continuity import SEG, AlwaysToolProvider, NOTE_FILE


@pytest.fixture
def manager(agent_home):
    db = Database(agent_home / "fixes.db")
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )


def _make_runtime(manager: SessionManager, user_id: str = "u_fix"):
    db = manager.session_repo.db
    users = UserRepo(db)
    existing = db.query_one("SELECT id FROM users WHERE username=?", (user_id,))
    uid = existing["id"] if existing else users.create(user_id, "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    rt = manager.get_runtime(meta["id"], uid)
    (rt.workspace / NOTE_FILE).write_text("内容", encoding="utf-8")
    return rt


# ---------------------------------------------------------------------------
# 1. 兜底分支抛 NameError（logger 未定义）
# ---------------------------------------------------------------------------
def test_route_failure_degrades_instead_of_crashing(manager, monkeypatch) -> None:
    """任务类型路由抛异常时必须**降级**（无提示），而不是把整条 prompt 弄挂。

    路由只是"给模型一句提示"，分类失败顶多少一行字；这条用例同时是
    `_route_for_prompt` 里那个 `except` 的唯一覆盖——修复前它抛的是
    `NameError: name 'logger' is not defined`，异常直接冒到 submit()。
    """
    def _boom(prompt: str):
        raise ValueError("路由炸了")

    monkeypatch.setattr(manager_module, "route_task", _boom)
    rt = _make_runtime(manager)
    rt.router.provider_override = OfflineProvider(
        script=[OfflineProvider.text_turn("照常回答")]
    )

    result = asyncio.run(rt.submit("随便问一句"))

    assert result.stop_reason == "end_turn"
    assert result.text == "照常回答"
    assert rt._route_for_prompt("再来一次") is None       # 降级：本轮没有路由提示
    assert rt._route_obj is None


def test_checkpoint_failure_degrades_instead_of_crashing(manager, monkeypatch) -> None:
    """检查点写不进去也只该记一条日志——它不该拦住这条 prompt。"""

    class BoomStore:
        def __init__(self, *args, **kwargs):
            pass

        def record(self, **kwargs):
            raise OSError("磁盘满了")

        def list(self, **kwargs):
            return []

        def get(self, *args, **kwargs):
            return None

        def drop_after(self, *args, **kwargs):
            return 0

    monkeypatch.setattr(manager_module, "CheckpointStore", BoomStore)
    rt = _make_runtime(manager)
    rt.router.provider_override = OfflineProvider(
        script=[OfflineProvider.text_turn("还是跑完了")]
    )

    result = asyncio.run(rt.submit("检查点会失败的任务"))

    assert result.stop_reason == "end_turn"
    assert result.text == "还是跑完了"


# ---------------------------------------------------------------------------
# 2. 跨段用量必须累加
# ---------------------------------------------------------------------------
def test_continuation_usage_sums_all_segments(manager, monkeypatch) -> None:
    """REST 返回值里的 usage 必须等于**各段之和**。

    口径来源是轨迹本身：每段各发一个带 usage 的事件（正常收口是 done，
    撞检查点是 code=segment_boundary 的 error）。修复前只抄最后一段。
    """
    monkeypatch.setattr(manager_module, "MAX_CONTINUATIONS", 2)
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", SEG)
    rt = _make_runtime(manager)
    provider = AlwaysToolProvider()
    provider.tool_until = SEG + 1        # 第 1 段跑满 SEG 轮，第 2 段再 1 轮后收口
    rt.router.provider_override = provider

    result = asyncio.run(rt.submit("跨段任务"))

    events = rt.transcript.replay(rt.session_id)
    per_segment = sum(
        int((e.payload.get("usage") or {}).get("input_tokens") or 0)
        for e in events
        if isinstance(e.payload, dict)
    )
    assert per_segment > 0, "轨迹里没有任何用量事件，用例本身失效了"
    assert result.usage.input_tokens == per_segment, (
        f"返回值 {result.usage.input_tokens} != 各段之和 {per_segment}"
        f"（修复前只会拿到最后一段）"
    )
    # 至少两段才叫"跨段"，否则这条用例没意义
    assert len([e for e in events if e.payload.get("auto")]) >= 1


def test_single_segment_usage_is_unchanged(manager, monkeypatch) -> None:
    """没发生续跑时行为不变（别把单段的用量记成双份）。"""
    monkeypatch.setattr(manager_module, "MAX_CONTINUATIONS", 2)
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", SEG)
    rt = _make_runtime(manager)
    rt.router.provider_override = OfflineProvider(
        script=[OfflineProvider.tool_call_turn("read_file", {"path": NOTE_FILE}),
                OfflineProvider.text_turn("一轮就完")]
    )

    result = asyncio.run(rt.submit("短任务"))

    assert result.stop_reason == "end_turn"
    assert result.turns == 2
    assert result.usage.input_tokens == 42 * 2      # offline mock 每轮 42 in
    assert result.usage.output_tokens == 17 * 2


# ---------------------------------------------------------------------------
# 3. no_failures 判据要真的看失败数
# ---------------------------------------------------------------------------
def test_no_failures_item_sees_real_failures(manager) -> None:
    """本轮有工具失败时，`no_failures` 判据必须判不通过（修复前恒为真）。"""
    rt = _make_runtime(manager)
    rt.set_acceptance([{"label": "没有遗留失败", "check": "no_failures"}])
    rt.router.provider_override = OfflineProvider(
        script=[
            OfflineProvider.tool_call_turn("bash", {"command": "sudo rm -rf /"}),  # 命中黑名单 -> deny
            OfflineProvider.text_turn("换个方式。"),
        ]
    )

    asyncio.run(rt.submit("跑个会被拒的命令"))

    state = rt.acceptance()
    assert state["declared"] is True
    item = next(i for i in state["items"] if i["check"] == "no_failures")
    assert item["ok"] is False, "本轮有失败却被判为满足——no_failures 又变成恒真了"
    assert "未解决" in item["detail"]
    assert state["passed"] is False


def test_no_failures_item_passes_when_this_round_is_clean(manager) -> None:
    """本轮没有失败时照常通过（别把上一轮的历史失败也算进来）。"""
    rt = _make_runtime(manager)
    rt.set_acceptance([{"label": "没有遗留失败", "check": "no_failures"}])
    rt.router.provider_override = OfflineProvider(
        script=[OfflineProvider.text_turn("直接回答，不调工具")]
    )

    asyncio.run(rt.submit("纯问答"))

    state = rt.acceptance()
    assert state["passed"] is True, state


def test_failed_tool_does_not_reopen_a_completed_task(manager) -> None:
    """工具失败保留真实结果和日志，模型结束后不再发起门禁补做。"""
    from code_agent.models.events import EventType

    rt = _make_runtime(manager)
    provider = OfflineProvider(script=[
        OfflineProvider.tool_call_turn("read_file", {"path": "missing-file.txt"}),
        OfflineProvider.text_turn("文件不存在，无法读取。"),
    ])
    rt.router.provider_override = provider
    result = asyncio.run(rt.submit("读取 missing-file.txt"))

    assert result.stop_reason == "end_turn" and provider.calls == 2
    events = rt.transcript.replay(rt.session_id)
    assert [event.payload["text"] for event in events if event.type == EventType.user_message] == ["读取 missing-file.txt"]
    assert any(str(event.payload.get("result", "")).startswith("[tool error]")
               for event in events if event.type == EventType.tool_result)
    assert not any(event.payload.get("auto") for event in events)
    logs = [path.read_text(encoding="utf-8") for path in (rt.memory_root / "memory").glob("*.md")]
    assert any("read_file" in text and "失败" in text for text in logs)


def test_legacy_session_ignores_execution_mode_and_keeps_history(manager) -> None:
    """旧会话模式字段不再参与执行，历史与已有台账原样保留。"""
    from code_agent.models.events import EventType

    db = manager.session_repo.db
    user = UserRepo(db).create("legacy-mode", "test-only")
    meta = manager.session_repo.create(user["id"])
    db.execute("UPDATE sessions SET loop_mode='cage' WHERE id=?", (meta["id"],))
    event = manager.transcript.append(meta["id"], EventType.assistant_message,
                                      {"text": "历史验收说明", "cage": True, "round": 1})
    path = manager.transcript._path(meta["id"])
    original_history = path.read_bytes()
    rt = manager.get_runtime(meta["id"], user["id"])
    journal = rt.memory_root / "cage-journal.json"
    journal.write_text('{"historical": true}', encoding="utf-8")
    original_journal = journal.read_bytes()
    provider = OfflineProvider(script=[OfflineProvider.text_turn("当前回答")])
    rt.router.provider_override = provider

    result = asyncio.run(rt.submit("你好"))

    assert result.stop_reason == "end_turn" and result.text == "当前回答"
    assert provider.calls == 1 and not hasattr(rt, "loop_mode")
    assert path.read_bytes().startswith(original_history)
    assert manager.transcript.replay(meta["id"])[0] == event
    assert journal.read_bytes() == original_journal
    assert not any(item.payload.get("cage") for item in manager.transcript.replay(meta["id"], after_seq=event.seq))


def test_failure_count_and_prompt_anchor() -> None:
    """两个纯函数的口径：失败只认前缀，起点只认真实用户消息（自动续跑不算）。"""
    from code_agent.models.events import Event, EventType

    events = [
        Event(seq=1, type=EventType.user_message, payload={"text": "原始任务"}),
        Event(seq=2, type=EventType.tool_result, payload={"call_id": "c1", "result": "[tool error] 炸了"}),
        Event(seq=3, type=EventType.tool_result, payload={"call_id": "c2", "result": "exit_code=0\nok"}),
        Event(seq=4, type=EventType.user_message, payload={"text": "自动继续", "auto": True}),
        Event(seq=5, type=EventType.tool_result, payload={"call_id": "c3", "result": "[permission denied] 拒了"}),
    ]

    start = since_last_prompt(events)
    assert start == 1, "自动续跑提示不该被当成新的一轮起点"
    assert failure_count(events, since_seq=start) == 2
    assert failure_count(events, since_seq=4) == 1     # 只看 seq>4
    assert failure_count([]) == 0
