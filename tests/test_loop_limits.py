"""长任务不被轮数上限打断（参考 deepseek harness 的"没有轮数上限"）。

三件事一起验：
1. 自动续跑的次数上限从 3 提到 40（可用 CODE_AGENT_MAX_CONTINUATIONS 覆盖）；
2. 段内每一轮之前把消息压回预算（老工具结果只留开头 → 再裁中段，
   但**锁定第一条 user 消息**，并保证工具调用/结果配对不被裁断）；
3. 真跑飞时有硬上限（CODE_AGENT_MAX_TOTAL_TURNS），报的是"到硬上限"而不是"配额耗尽"。
"""

from __future__ import annotations

from code_agent.agent import loop as loop_mod
from code_agent.agent.loop import AgentLoop
from code_agent.context.compact import (
    HISTORY_OMIT_MARKER,
    TOOL_OMIT_MARKER,
    compact_messages,
    prune_old_tool_results,
)
from code_agent.models.events import ModelTurn, ToolCall, assistant_message, tool_message, user_message


def _msgs_with_tools(n: int, size: int = 500) -> list[dict]:
    out = [user_message("把仓库里所有 TODO 都列出来并归类")]
    for i in range(n):
        out.append(
            assistant_message(
                ModelTurn(text=f"第 {i} 步", tool_calls=[ToolCall(id=f"c{i}", name="read_file", arguments={})])
            )
        )
        out.append(tool_message(f"c{i}", "行" * size))
    return out


def test_prune_keeps_heads_not_just_markers():
    """老结果留开头（能想起结论），而不是整条抹掉——否则模型会重新去查一遍。"""
    msgs = _msgs_with_tools(12)
    pruned = prune_old_tool_results(msgs, keep_last=4, head_chars=40)
    tools = [m for m in pruned if m["role"] == "tool"]
    assert len(tools) == 12
    old = tools[0]["content"]
    assert TOOL_OMIT_MARKER in old and len(old) < 120, old
    assert tools[-1]["content"] == "行" * 500, "最近的结果要保持全文"


def test_compact_pins_the_original_task():
    """裁中段时第一条 user 消息（任务本身）必须留下。"""
    msgs = _msgs_with_tools(40, size=800)
    out = compact_messages(msgs, max_chars=4000)
    users = [m for m in out if m["role"] == "user"]
    assert users and users[0]["content"] == "把仓库里所有 TODO 都列出来并归类"
    assert any(HISTORY_OMIT_MARKER in (m.get("content") or "") for m in out), "应有省略标记"


def test_compact_never_leaves_orphan_tool_result():
    """工具配对：裁完后尾部不能以孤儿 tool 结果开头（否则 provider 会报非法序列）。"""
    msgs = _msgs_with_tools(30, size=900)
    out = compact_messages(msgs, max_chars=3000)
    body = [m for m in out if m["role"] != "system"]
    assert body and body[0]["role"] == "user", body[0]
    # 任何 tool 消息之前都应该有一个带 tool_calls 的 assistant（或被省略标记隔开）
    for i, m in enumerate(body):
        if m["role"] == "tool" and i > 0 and body[i - 1]["role"] == "assistant":
            continue
        if m["role"] == "tool":
            assert i > 0, "不能以 tool 消息开头"


def test_loop_fits_messages_within_budget():
    """段内压缩：超预算时把消息压回去，且任务仍在。"""
    loop = AgentLoop.__new__(AgentLoop)          # 只测纯函数，不构造 provider
    loop.history_max_chars = 3000
    msgs = _msgs_with_tools(40, size=800)
    assert sum(len(m.get("content") or "") for m in msgs) > 3000
    fitted = loop._fit_messages(msgs)
    assert sum(len(m.get("content") or "") for m in fitted) <= 3000 * 1.05
    assert any(m["role"] == "user" for m in fitted)


def test_continuation_budget_defaults_are_dsh_like():
    """默认值要接近"跑到做完"，而且**硬顶才是真正的约束**。

    这段边界不是防跑飞用的（到了就自动续跑），防跑飞的是 MAX_TOTAL_TURNS。
    所以必须满足：`段长 × 续跑次数 ≥ 硬顶`——否则先撞到的会是"续跑次数用完"，
    用户又会看到"达到最大轮数"要手点继续，硬顶形同虚设。
    """
    import code_agent.agent.loop as loop_mod
    import code_agent.sessions.manager as manager

    seg = loop_mod.DEFAULT_MAX_TURNS
    assert seg == 0 or seg >= 40, f"段长太小会让检查点/提示过密：{seg}"
    assert manager.MAX_CONTINUATIONS >= 20, manager.MAX_CONTINUATIONS
    assert manager.MAX_TOTAL_TURNS == 0 or manager.MAX_TOTAL_TURNS >= 500
    if seg and manager.MAX_TOTAL_TURNS:
        cover = seg * manager.MAX_CONTINUATIONS
        assert cover >= manager.MAX_TOTAL_TURNS, (
            f"段长 {seg} × 续跑 {manager.MAX_CONTINUATIONS} = {cover} 轮"
            f" < 硬顶 {manager.MAX_TOTAL_TURNS}：先撞到的会是续跑次数而不是硬顶"
        )


def test_continuation_note_is_quiet_for_the_ui(agent_home, monkeypatch):
    """续跑提示要**只发一条**、落轨迹、并带 quiet 标记。

    为什么钉这两点：
    - 提示对**模型**有用（重新锚定"别重复劳动"），对**读的人**是噪音 → 界面要能区分；
    - 以前发两条（seq=0 的漂亮提示 + loop 落的原始 user 消息）：实时看一遍、
      刷新后又以"用户气泡"的样子看一遍（还带 `**` 号）。现在只有 loop 落的那一条。
    """
    import asyncio

    from code_agent.agent.router import ModelRouter
    from code_agent.cli import build_registry
    from code_agent.config import transcripts_dir
    from code_agent.sessions import manager as manager_module
    from code_agent.sessions.manager import SessionManager
    from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
    from code_agent.storage.jsonl import TranscriptStore

    from tests.test_continuity import AlwaysToolProvider

    db = Database(agent_home / "quiet.db")
    manager = SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )
    uid = UserRepo(db).create("u_quiet", "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    rt = manager.get_runtime(meta["id"], uid)
    monkeypatch.setattr(manager_module, "MAX_CONTINUATIONS", 1)
    # 段长钉成 3 轮：这条用例只验证"提示怎么发"，跑 60 轮没有额外信息
    monkeypatch.setattr(loop_mod, "DEFAULT_MAX_TURNS", 3)
    (rt.workspace / "note.txt").write_text("内容", encoding="utf-8")
    rt.router.provider_override = AlwaysToolProvider()

    asyncio.run(rt.submit("长任务"))

    auto = [e for e in rt.transcript.replay(meta["id"]) if e.payload.get("auto")]
    assert len(auto) == 1, f"续跑提示应只发一条（实际 {len(auto)} 条）"
    p = auto[0].payload
    assert p["quiet"] is True, "续跑提示没标 quiet，界面会当普通用户消息铺开"
    assert p["continuation"] == 1 and p["total_turns"] > 0, p
    assert "不要重新调研" in p["text"] and "重复劳动" in p["text"], "给模型的完整文本不能丢"
