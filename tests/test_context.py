"""上下文管理单测（s13/s14/s15）：外部化、压缩、历史重建、组装。"""

import copy

import pytest

from code_agent.context.compact import (
    HISTORY_OMIT_MARKER,
    TOOL_OMIT_MARKER,
    compact_messages,
    prune_old_tool_results,
)
from code_agent.context.externalize import externalize_result
from code_agent.context.prompt import assemble, build_history, memory_blocks
from code_agent.models.events import Event, EventType, ModelTurn, ToolCall


def test_externalize_under_threshold():
    r = externalize_result("short output", "s1", "tc1", threshold=1024)
    assert r == "short output"


def test_externalize_over_threshold(agent_home):
    big = "x" * 100000
    r = externalize_result(big, "s1", "tc1", threshold=1024)
    assert "[外部化]" in r
    assert "read_file" in r
    # 预览在、全文不在：窗口由**预算**决定（这里显式给了 threshold=1024），
    # 所以上界就是那 1024 字节。旧断言写的是 `< 1000`，对应当时固定 400 字的预览——
    # 预算可配之后那个数字不再是产品常量，硬写它会变成"改预算就红"的假门槛。
    assert 0 < r.count("x") <= 1024 + 64
    assert "x" * 5000 not in r
    # 文件确实落盘且内容完整
    import re

    path = re.search(r"存盘: (.+)", r).group(1)
    from pathlib import Path

    assert Path(path).read_text() == big


def test_prune_old_tool_results():
    msgs = [{"role": "tool", "tool_call_id": str(i), "content": "r" * 100} for i in range(10)]
    msgs.append({"role": "tool", "tool_call_id": "latest", "content": "fresh"})
    out = prune_old_tool_results(msgs, keep_last=3, head_chars=20)
    # 最近 3 条保留全文（含最新那条）
    full = [m for m in out if TOOL_OMIT_MARKER not in m["content"]]
    assert len(full) == 3
    assert full[-1]["content"] == "fresh"
    # 更早的**留开头**（能想起结论）而不是整条抹掉——M20 起的行为
    old = out[0]["content"]
    assert old.endswith(TOOL_OMIT_MARKER)
    assert old.startswith("r" * 20)
    assert len(old) < 60


def test_compact_keeps_tail_and_budget():
    msgs = [{"role": "user" if i == 0 else "assistant", "content": f"消息{i}" + "y" * 200} for i in range(20)]
    out = compact_messages(msgs, max_chars=1500)
    joined = "".join(m["content"] for m in out if isinstance(m.get("content"), str))
    assert HISTORY_OMIT_MARKER in joined
    # 尾部消息一定保留
    assert any("消息19" in (m.get("content") or "") for m in out)


def test_compact_under_budget_no_change():
    msgs = [{"role": "user", "content": "短消息"} for _ in range(3)]
    out = compact_messages(msgs, max_chars=10000)
    assert out == msgs


def test_build_history_from_events():
    events = [
        Event(seq=1, type=EventType.user_message, payload={"text": "第一问"}),
        Event(
            seq=2,
            type=EventType.assistant_message,
            payload={"text": "", "tool_calls": [{"id": "t1", "name": "bash", "arguments": {"command": "ls"}}]},
        ),
        Event(seq=3, type=EventType.tool_result, payload={"call_id": "t1", "result": "ok"}),
        Event(seq=4, type=EventType.done, payload={"text": "第一答"}),
    ]
    msgs = build_history(events)
    # done 事件（最终回答）也映射为 assistant 消息
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "bash"
    assert msgs[2]["tool_call_id"] == "t1"
    assert msgs[3]["content"] == "第一答"


@pytest.mark.parametrize("metadata", [{"auto": True, "closure_kind": "evidence"}, {"auto": True, "closures": 1}])
def test_legacy_internal_closure_questions_do_not_become_new_work_on_resume(metadata):
    events = [
        Event(seq=1, type=EventType.user_message, payload={"text": "修复功能"}),
        Event(seq=2, type=EventType.done, payload={"text": "已修复并通过测试"}),
        Event(seq=3, type=EventType.user_message, payload={"text": "[收口自查] 证据够了吗？", **metadata}),
        Event(seq=4, type=EventType.user_message, payload={"text": "接着完善交互"}),
    ]
    original = copy.deepcopy(events)
    history = build_history(events)
    assert [message["content"] for message in history] == ["修复功能", "已修复并通过测试", "接着完善交互"]
    assert events == original


@pytest.mark.parametrize("metadata", [{}, {"auto": False, "closure_kind": "evidence"}, {"auto": "true", "closures": 1}, {"auto": True}])
def test_user_text_and_unrelated_automatic_continuations_are_not_filtered(metadata):
    text = "[收口自查] 证据够了吗？"
    history = build_history([Event(seq=1, type=EventType.user_message, payload={"text": text, **metadata})])
    assert history[0]["content"] == text


@pytest.mark.parametrize("notice", [{"inbox_ids": ["message-1"]}, {"job_notice": {"id": "job-1"}}, {"kind": "job_notice"}])
def test_combined_legacy_closure_and_runtime_notifications_keep_the_delivery(notice):
    text = "后台任务已完成，可以继续实现。"
    history = build_history([Event(seq=1, type=EventType.user_message,
                                  payload={"text": text, "auto": True, "closure_kind": "evidence", **notice})])
    assert history == [{"role": "user", "content": text, "_context_source": "auto"}]


def test_memory_blocks_injection():
    ws_entries = [("workspace", "本项目数据库是 MySQL")]
    hits = memory_blocks(ws_entries, [("user", "用户在杭州")], query="数据库用什么")
    assert hits and "MySQL" in hits[0]["content"]
    # M6：记忆降权为 user 角色数据块，绝不进 system（防提示词注入）
    assert hits[0]["role"] == "user"
    assert "数据块" in hits[0]["content"]
    # 无命中 -> 不注入
    assert memory_blocks(ws_entries, [], query="完全无关的量子问题") == []


def test_assemble_order():
    history = [{"role": "user", "content": "旧消息"}]
    mem = [{"role": "system", "content": "【记忆】x"}]
    msgs = assemble("sys", mem, history, "新问题", max_chars=5000)
    assert msgs[0]["content"] == "sys"
    assert msgs[1]["content"] == "【记忆】x"
    assert msgs[2]["content"] == "旧消息"
    assert msgs[-1]["content"] == "新问题"
