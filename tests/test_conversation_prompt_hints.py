"""Conversation hints keep direct execution and existing authority boundaries."""

import asyncio
from types import SimpleNamespace

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.models.events import ToolCall
from code_agent.sessions.manager import SessionRuntime
from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.registry import ToolRegistry




@pytest.mark.parametrize("delegation_enabled", [False, True])
@pytest.mark.parametrize("bash_enabled", [False, True])
def test_missing_tool_keeps_direct_capabilities_without_forcing_worker_handoff(tmp_path, delegation_enabled, bash_enabled):
    class AvailableTool(Tool):
        def __init__(self, name):
            self.name = name

        async def execute(self, arguments, ctx):
            pytest.fail("An unavailable tool must not execute an alternative automatically")

    async def permissions(*args):
        pytest.fail("An unavailable tool must not request permission")

    registry = ToolRegistry()
    registry.register(AvailableTool("read_file"))
    if delegation_enabled:
        registry.register(AvailableTool("delegate_agent"))
    if bash_enabled:
        registry.register(AvailableTool("bash"))
    audit = []
    loop = AgentLoop(
        provider=SimpleNamespace(), registry=registry,
        permissions=SimpleNamespace(evaluate=permissions), transcript=SimpleNamespace(),
        on_tool_executed=lambda name, arguments, outcome: audit.append((name, outcome)),
    )
    result = asyncio.run(loop._execute_tool(
        ToolCall(name="unavailable_tool", arguments={}), ToolContext("conversation", tmp_path),
    ))
    assert "tool not available" in result
    assert "申请权限不能使它可用" in result
    assert "可直接核查的工具：read_file" in result
    assert "委派给执行节点" not in result
    assert ("按需委派后台子任务" in result) is delegation_enabled
    if delegation_enabled:
        assert "优先使用当前可用工具继续执行" in result
        assert "符合权限与工作树边界" in result
    assert (registry.get("bash") is not None) is bash_enabled
    assert audit == [("unavailable_tool", "error:ToolError")]


def test_old_workflow_context_does_not_resolve_nodes_or_inject_task_instructions():
    runtime = SimpleNamespace(session_id="conversation", agent_team=object())
    context = {"workflow_node_id": "implementation", "workflow_tree_id": "main"}
    blocks, metadata = SessionRuntime._code_context_blocks(runtime, "完成当前任务", context)
    assert blocks == []
    assert metadata == {"code_context": context}
