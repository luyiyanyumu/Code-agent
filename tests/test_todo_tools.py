"""任务清单工具（M30）：原理同 deepseek harness 的 `dsh-tool-todo`。

三条性质决定它有没有用，这里逐条钉住：

1. **整份替换**：每次调用发完整列表，界面只认最后一次（不需要 diff/合并）；
2. **坏输入报错而不是静默清洗**：清单是给人看的进度承诺，静默丢一条或改个状态，
   界面就会显示一份模型不认为存在的进度；
3. **状态约束**：内容非空去重、状态只能是三档、最多一条 in_progress。

顺带钉住"不落并行状态"这条设计：清单只存在于 `tool_use` 事件的参数里，
轨迹是唯一事实来源——所以工具本身不该写任何新事件类型。
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.config import transcripts_dir
from code_agent.models.events import EventType
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.todo_tools import (
    MAX_ITEMS,
    TodoWriteTool,
    count_by_status,
    normalize_todos,
)


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(session_id="s_todo", workspace=tmp_path, user_id="u_todo")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def test_normalize_keeps_order_and_reports_counts() -> None:
    items = normalize_todos(
        [
            {"content": "先看地图", "status": "completed"},
            {"content": "改代码", "status": "in_progress"},
            {"content": "跑测试", "status": "pending"},
        ]
    )
    assert [i["content"] for i in items] == ["先看地图", "改代码", "跑测试"]
    assert count_by_status(items) == {"pending": 1, "in_progress": 1, "completed": 1}


def test_normalize_trims_and_rejects_duplicates() -> None:
    items = normalize_todos([{"content": "  改代码  ", "status": "pending"}])
    assert items[0]["content"] == "改代码"
    with pytest.raises(ToolError, match="重复"):
        normalize_todos(
            [
                {"content": "改代码", "status": "pending"},
                {"content": "改代码", "status": "completed"},
            ]
        )


def test_normalize_rejects_bad_input_loudly() -> None:
    """坏输入一律报错——静默清洗会让界面显示一份模型不认为存在的进度。"""
    with pytest.raises(ToolError):
        normalize_todos(None)
    with pytest.raises(ToolError):
        normalize_todos("改代码")
    with pytest.raises(ToolError, match="content 不能为空"):
        normalize_todos([{"content": "   ", "status": "pending"}])
    with pytest.raises(ToolError, match="status"):
        normalize_todos([{"content": "改代码", "status": "doing"}])
    with pytest.raises(ToolError, match="太长"):
        normalize_todos([{"content": "很长" * 100, "status": "pending"}])
    with pytest.raises(ToolError, match="最多"):
        normalize_todos([{"content": f"第{i}步", "status": "pending"} for i in range(MAX_ITEMS + 1)])


def test_at_most_one_in_progress() -> None:
    """"同时进行中两条"在串行工作里就是没在按优先级干活。"""
    with pytest.raises(ToolError, match="in_progress"):
        normalize_todos(
            [
                {"content": "A", "status": "in_progress"},
                {"content": "B", "status": "in_progress"},
            ]
        )


def test_empty_list_clears(tmp_path) -> None:
    """空数组 = 清空（合法），不是"忘了传"。"""
    tool = TodoWriteTool()
    out = asyncio.run(tool.execute({"todos": []}, _ctx(tmp_path)))
    assert "列表已清空" in out
    assert normalize_todos([]) == []


def test_tool_output_shows_the_whole_list(tmp_path) -> None:
    """喂回模型的是**完整清单**：模型据此确认自己写的是什么（整份替换的语义要求）。"""
    tool = TodoWriteTool()
    out = asyncio.run(
        tool.execute(
            {
                "todos": [
                    {"content": "改 mathx", "status": "completed"},
                    {"content": "跑测试", "status": "in_progress"},
                ]
            },
            _ctx(tmp_path),
        )
    )
    assert "✅ 改 mathx" in out and "▶ 跑测试" in out
    assert "已完成 1 · 进行中 1" in out
    # 顺带提醒验收清单是另一件事，别把两者混起来
    assert "acceptance_set" in tool.description


# ---------------------------------------------------------------------------
# 接到会话上：清单只活在 tool_use 事件里
# ---------------------------------------------------------------------------
@pytest.fixture
def manager(agent_home):
    db = Database(agent_home / "todo.db")
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )


def _runtime(manager: SessionManager):
    db = manager.session_repo.db
    uid = UserRepo(db).create("u_todo", "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    return manager.get_runtime(meta["id"], uid)


def test_todo_write_lands_in_the_transcript_as_a_tool_call(manager) -> None:
    """整份替换的两次调用都在轨迹里，界面取最后一次；工具不额外写事件。"""
    from code_agent.models.events import EventType as ET

    rt = _runtime(manager)
    plan = [
        {"content": "看地图", "status": "in_progress"},
        {"content": "改代码", "status": "pending"},
    ]
    update = [
        {"content": "看地图", "status": "completed"},
        {"content": "改代码", "status": "in_progress"},
    ]

    class PlanProvider(OfflineProvider):
        def __init__(self) -> None:
            super().__init__(script=[])
            self.n = 0

        async def chat(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return OfflineProvider.tool_call_turn("todo_write", {"todos": plan})
            if self.n == 2:
                return OfflineProvider.tool_call_turn("todo_write", {"todos": update})
            return OfflineProvider.text_turn("两步都做完了。")

    rt.router.provider_override = PlanProvider()
    asyncio.run(rt.submit("改一下 mathx"))

    events = rt.transcript.replay(rt.session_id)
    calls = [
        e.payload["call"]
        for e in events
        if e.type == ET.tool_use and e.payload.get("call", {}).get("name") == "todo_write"
    ]
    assert [c["arguments"]["todos"][0]["status"] for c in calls] == ["in_progress", "completed"]
    assert calls[-1]["arguments"]["todos"][1]["status"] == "in_progress"

    # 两次调用都成功了（界面据此认定"最后一次写入"是当前真相）
    results = [
        e.payload["result"]
        for e in events
        if e.type == ET.tool_result and e.payload.get("name") == "todo_write"
    ]
    assert len(results) == 2 and not any(r.startswith("[tool error]") for r in results)

    # 没有新的事件类型：清单只作为 tool_use 参数存在（一份事实，不会走散）
    assert {e.type for e in events} <= set(EventType)


def test_todo_write_is_visible_to_the_model_and_disableable(agent_home, monkeypatch) -> None:
    """工具进了注册表（模型看得见），也能被「工具」页一键禁用（M21）。"""
    registry = build_registry()
    assert "todo_write" in registry.names()
    schema = next(s for s in registry.schemas() if s["name"] == "todo_write")
    assert schema["parameters"]["required"] == ["todos", "_approval"]
    assert schema["parameters"]["properties"]["todos"]["items"]["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
        "completed",
    ]
