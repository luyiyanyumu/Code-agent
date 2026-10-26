"""延迟加载单测（s03）：摘要先行，tool_search 激活后完整 schema 才进上下文。"""

import asyncio

from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.defer import DeferredToolRegistry


class WriteTool(Tool):
    name = "write_file"
    description = "write a file"

    async def execute(self, arguments, ctx):
        return "ok"


def _ctx(tmp_path):
    return ToolContext(session_id="s", workspace=tmp_path)


def test_deferred_tool_hidden_behind_summary(tmp_path):
    reg = DeferredToolRegistry()
    reg.register_deferred(WriteTool())
    names = [s["name"] for s in reg.schemas()]
    assert set(names) == {"tool_search", "write_file"}
    write_schema = next(s for s in reg.schemas() if s["name"] == "write_file")
    assert write_schema["description"].startswith("[deferred]")
    # 未激活前不能真正调用（schema 缺失是模型层面的，注册表层面仍可调用 —— 由 loop 的 schemas 控制）


def test_tool_search_activates(tmp_path):
    reg = DeferredToolRegistry()
    reg.register_deferred(WriteTool())
    search = reg.get("tool_search")
    result = asyncio.run(search.execute({"names": ["write_file"]}, _ctx(tmp_path)))
    assert "activated" in result
    assert "write_file" in result
    schema = next(s for s in reg.schemas() if s["name"] == "write_file")
    assert not schema["description"].startswith("[deferred]")
    assert "properties" in schema["parameters"]


def test_tool_search_missing_reported(tmp_path):
    reg = DeferredToolRegistry()
    search = reg.get("tool_search")
    result = asyncio.run(search.execute({"names": ["ghost"]}, _ctx(tmp_path)))
    assert "not found" in result
