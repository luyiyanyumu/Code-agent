"""Exercise the real pinned JS core through Python's production adapters."""

import asyncio
import json
from pathlib import Path

import pytest
import code_agent.harness.runtime as harness_runtime

from code_agent.agent.loop import AgentLoop
from code_agent.harness.runtime import run_harness
from code_agent.models.events import ModelTurn, TokenUsage, ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool
from code_agent.tools.builtin import ReadFileTool
from code_agent.tools.registry import ToolRegistry


def make_loop(workspace, provider, *tools, **kwargs):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentLoop(provider, registry, PermissionManager(workspace_root=workspace),
                     TranscriptStore(workspace.parent / "transcripts"), workspace=workspace, **kwargs)


def test_official_core_does_not_force_answers_or_shrink_tools(workspace):
    (workspace / "source.txt").write_text("current source", encoding="utf8")

    class Recording(OfflineProvider):
        async def chat(self, messages, tools):
            assert all("name" in row and "function" not in row for row in tools)
            assert any(row["name"] == "read_file" for row in tools)
            return await super().chat(messages, tools)

    provider = Recording(script=[
        *[OfflineProvider.tool_call_turn("read_file", {"path": "source.txt"}) for _ in range(8)],
        OfflineProvider.text_turn("已有证据，可以回答。"),
    ])
    loop = make_loop(workspace, provider, ReadFileTool())
    result = asyncio.run(loop.run("检查当前源码"))
    assert result.stop_reason == "end_turn" and result.turns == 9
    assert result.text == "已有证据，可以回答。"
    assert len([event for event in loop.transcript.replay(loop.session_id) if event.type.value == "tool_result"]) == 8
    assert not any(event.payload.get("code") in {"execution_progress", "final_answer_repair", "edit_recovery"}
                   for event in loop.transcript.replay(loop.session_id))


def test_unknown_tool_has_one_synthetic_receipt_and_can_recover(workspace):
    provider = OfflineProvider(script=[
        OfflineProvider.tool_call_turn("not_registered", {}),
        OfflineProvider.text_turn("工具不可用，已说明具体阻碍。"),
    ])
    loop = make_loop(workspace, provider)
    result = asyncio.run(loop.run("运行所需工具"))
    assert result.turns == 2
    receipts = [event for event in loop.transcript.replay(loop.session_id) if event.type.value == "tool_result"]
    assert len(receipts) == 1 and receipts[0].payload["name"] == "not_registered"
    assert "not_registered" in receipts[0].payload["result"]


def test_native_driver_keeps_long_frames_and_context_notices(workspace):
    payload = "source-evidence-" * 8000
    seen = []

    async def host(method, frame):
        assert method == "model"
        seen.extend(frame["messages"])
        return {"text": "done", "tool_calls": [], "stop_reason": "end_turn",
                "contextMessages": [{"role": "user", "content": "background tool finished"}]}

    result = asyncio.run(run_harness({
        "sessionId": "long-frame", "tools": [],
        "messages": [{"role": "user", "content": payload}], "maxTurns": 0,
    }, host))
    assert seen[-1]["content"] == payload
    assert any(message.get("content") == "background tool finished" for message in result["messages"])
    assert result["harness"]["package"] == "@deepseek-ai/dsh-agent-loop"


def test_lifecycle_baseexception_and_independent_provider_cancel_propagate():
    class ActivationEnded(BaseException):
        pass

    async def check(exc):
        async def fail(method, frame):
            raise exc
        with pytest.raises(type(exc)):
            await asyncio.wait_for(run_harness({"messages": [{"role": "user", "content": "run"}]}, fail), 5)

    asyncio.run(check(ActivationEnded("activation ended")))
    asyncio.run(check(asyncio.CancelledError()))


def test_cancel_running_python_tool_releases_host_task(workspace):
    entered, released = asyncio.Event(), asyncio.Event()

    class WaitingRead(Tool):
        name = "read_file"
        description = "Wait for cancellation"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, arguments, ctx):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                released.set()

    async def scenario():
        loop = make_loop(workspace, OfflineProvider(script=[OfflineProvider.tool_call_turn("read_file", {})]), WaitingRead())
        task = asyncio.create_task(loop.run("read"))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert released.is_set()
        assert not any(event.type.value == "done" for event in loop.transcript.replay(loop.session_id))

    asyncio.run(scenario())


def test_abnormal_provider_stop_never_runs_partial_calls(workspace):
    provider = OfflineProvider(script=[ModelTurn(
        text="output limit", stop_reason="max_tokens", usage=TokenUsage(input_tokens=12, output_tokens=3),
        tool_calls=[ToolCall(name="read_file", arguments={"path": "does-not-exist"})],
    )])
    loop = make_loop(workspace, provider, ReadFileTool())
    result = asyncio.run(loop.run("read"))
    assert result.stop_reason == "max_tokens" and result.turns == 1 and result.usage.input_tokens == 12
    assert not any(event.type.value == "tool_use" for event in loop.transcript.replay(loop.session_id))


def test_usage_meter_failure_does_not_start_a_second_request(workspace):
    def fail(usage):
        raise RuntimeError("usage ledger unavailable")
    loop = make_loop(workspace, OfflineProvider(script=[OfflineProvider.text_turn("complete")]), on_model_usage=fail)
    with pytest.raises(RuntimeError, match="usage ledger unavailable"):
        asyncio.run(asyncio.wait_for(loop.run("answer"), 5))
    assert not any(event.type.value == "done" for event in loop.transcript.replay(loop.session_id))


def test_verified_engine_is_reused_without_repeated_probe_processes(monkeypatch):
    monkeypatch.setattr(harness_runtime, "_prepared", {})
    original, probes = harness_runtime._command, []
    async def measured(*args, **kwargs):
        probes.append(args)
        return await original(*args, **kwargs)
    monkeypatch.setattr(harness_runtime, "_command", measured)
    async def scenario():
        first = await harness_runtime.prepare_runtime()
        second = await harness_runtime.prepare_runtime()
        assert first == second
    asyncio.run(scenario())
    assert len(probes) == 1 and probes[0][-1] == "--check"
