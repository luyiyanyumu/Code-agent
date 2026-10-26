"""工具注册表与分发单测（s02）：注册、schema、并发分发。"""

import asyncio

from code_agent.tools.base import Tool, ToolContext, ToolError
from code_agent.tools.registry import ToolRegistry


class EchoTool(Tool):
    name = "echo"
    description = "echo text"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, arguments, ctx):
        return arguments.get("text", "")


class FailTool(Tool):
    name = "fail"
    description = "always fails"
    async def execute(self, arguments, ctx):
        raise ToolError("boom")


def _ctx(tmp_path):
    return ToolContext(session_id="s_test", workspace=tmp_path)


def test_register_and_schema(tmp_path):
    reg = ToolRegistry()
    reg.register(EchoTool())
    assert reg.names() == ["echo"]
    schema = reg.schemas()[0]
    assert schema["name"] == "echo"
    assert "properties" in schema["parameters"]


def test_dispatch_unknown_tool(tmp_path):
    reg = ToolRegistry()
    try:
        asyncio.run(reg.dispatch("nope", {}, _ctx(tmp_path)))
        assert False, "should raise"
    except ToolError as e:
        assert "nope" in str(e)


def test_concurrent_dispatch(tmp_path):
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(FailTool())
    calls = [("echo", {"text": "a"}), ("echo", {"text": "b"}), ("fail", {})]
    results = asyncio.run(reg.dispatch_all(calls, _ctx(tmp_path)))
    assert results[0] == "a"
    assert results[1] == "b"
    assert results[2].startswith("[tool error]")
