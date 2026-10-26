"""Public per-call explanations: temporary transcripts and synthetic tools only."""

from __future__ import annotations

import asyncio
import json

import pytest

from code_agent.agent.loop import AgentLoop, DEFAULT_SYSTEM_PROMPT
from code_agent.context.prompt import build_history
from code_agent.models.approval import tool_explanation_details
from code_agent.models.events import EventType, ModelTurn, ToolCall, assistant_message
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import Decision, PolicyResult, ReadOnlyPolicy
from code_agent.providers.base import StreamChunk
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool
from code_agent.tools.registry import ToolRegistry


PURPOSE = "核查临时页面检查脚本是否正常返回，以定位测试等待的问题。"
BASIS = "上一轮检查未返回结果，需要区分资源加载与脚本等待。"
EFFECTS = ["读取 fixture.txt 的模拟检查结果。", "预期获得检查状态，不修改项目文件。"]


class FixtureTool(Tool):
    name = "fixture_check"
    description = "Synthetic read fixture"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"], "additionalProperties": False}

    def __init__(self, on_execute=None):
        self.calls = []
        self.on_execute = on_execute

    async def execute(self, arguments, ctx):
        self.calls.append(dict(arguments))
        if self.on_execute is not None:
            return await self.on_execute(arguments)
        return "fixture checked"


def call(path="fixture.txt", *, call_id="fixture-call", purpose=PURPOSE, basis=BASIS, effects=EFFECTS):
    return ToolCall(id=call_id, name=FixtureTool.name, arguments={
        "path": path, "_approval": {"purpose": purpose, "basis": basis, "effects": effects}})


def make_loop(workspace, calls, *, tool=None, provider=None, on_event=None, session_id=None):
    registry = ToolRegistry()
    tool = tool or FixtureTool()
    registry.register(tool)
    provider = provider or OfflineProvider(script=[ModelTurn(tool_calls=calls), OfflineProvider.text_turn("done")])
    loop = AgentLoop(provider=provider, registry=registry, permissions=PermissionManager(workspace),
                     transcript=TranscriptStore(workspace.parent / "explanation-transcripts"),
                     workspace=workspace, on_event=on_event, session_id=session_id)
    loop.permissions.register_policy(tool.name, ReadOnlyPolicy())
    return loop, tool


def test_all_tools_request_public_summary_without_requiring_new_execution_arguments():
    schema = FixtureTool().schema()["parameters"]
    assert schema["required"] == ["path", "_approval"]
    assert schema["properties"]["_approval"]["required"] == ["purpose", "effects"]
    assert schema["properties"]["_approval"]["properties"]["basis"]["maxLength"] == 800
    assert "每次工具调用" in DEFAULT_SYSTEM_PROMPT
    assert "不展开内部思考或逐步推理链" in DEFAULT_SYSTEM_PROMPT


@pytest.mark.parametrize("metadata", [None, "not an object", {"reasoning_content": "private trace"}])
def test_missing_summary_is_explicit_and_does_not_infer_intent(metadata):
    assert tool_explanation_details(metadata) == {
        "purpose": "", "basis": "", "effects": [], "source": "missing"}


def test_summary_is_bounded_and_ignores_unknown_reasoning_fields():
    summary = tool_explanation_details({"purpose": " p " + "x" * 900, "basis": "b" * 900,
                                        "effects": ["e" * 600] * 12,
                                        "reasoning_content": "private trace", "thinking_delta": "private trace"})
    assert set(summary) == {"purpose", "basis", "effects", "source"}
    assert len(summary["purpose"]) == len(summary["basis"]) == 800
    assert len(summary["effects"]) == 6 and all(len(item) == 400 for item in summary["effects"])
    assert summary["source"] == "agent"


def test_summary_visible_before_tool_finishes_and_replays_separately_from_arguments(workspace):
    live = []

    async def executing(arguments):
        use = next(event for event in live if event.type == EventType.tool_use)
        assert use.payload["call"]["arguments"] == {"path": "fixture.txt"}
        assert use.payload["explanation"] == {"purpose": PURPOSE, "basis": BASIS,
                                               "effects": EFFECTS, "source": "agent"}
        assert not any(event.type == EventType.tool_result for event in live)
        return "fixture checked"

    original = call()
    loop, tool = make_loop(workspace, [original], tool=FixtureTool(executing), on_event=live.append)
    asyncio.run(loop.run("check synthetic fixture"))
    assert tool.calls == [{"path": "fixture.txt"}]
    replay = TranscriptStore(loop.transcript.root).replay(loop.session_id)
    assert next(event.payload for event in replay if event.type == EventType.tool_use) == {
        "call": {"id": original.id, "name": original.name, "arguments": {"path": "fixture.txt"}},
        "explanation": {"purpose": PURPOSE, "basis": BASIS, "effects": EFFECTS, "source": "agent"}}
    assistant = next(event for event in replay if event.type == EventType.assistant_message)
    assert assistant.payload["tool_calls"][0]["arguments"] == {"path": "fixture.txt"}
    assert "_approval" in original.arguments


def test_parallel_explanations_stay_with_exact_call_when_results_finish_out_of_order(workspace):
    calls = [call("slow.txt", call_id="slow", purpose="检查较慢检查项", basis="慢检查仍需确认", effects=["读取 slow.txt"]),
             call("fast.txt", call_id="fast", purpose="检查快速检查项", basis="独立检查可并行", effects=["读取 fast.txt"])]

    async def executing(arguments):
        if arguments["path"] == "slow.txt":
            await asyncio.sleep(0.02)
        return arguments["path"] + " checked"

    loop, _ = make_loop(workspace, calls, tool=FixtureTool(executing))
    asyncio.run(loop.run("check independent fixtures"))
    events = loop.transcript.replay(loop.session_id)
    uses = {event.payload["call"]["id"]: event.payload["explanation"] for event in events if event.type == EventType.tool_use}
    assert uses["slow"] == {"purpose": "检查较慢检查项", "basis": "慢检查仍需确认", "effects": ["读取 slow.txt"], "source": "agent"}
    assert uses["fast"] == {"purpose": "检查快速检查项", "basis": "独立检查可并行", "effects": ["读取 fast.txt"], "source": "agent"}
    results = [event.payload["call_id"] for event in events if event.type == EventType.tool_result]
    assert results == ["fast", "slow"]


def test_public_summary_does_not_copy_raw_thinking_or_grow_model_history(workspace):
    private_trace = "PRIVATE-STREAM-FIXTURE-DO-NOT-PERSIST"
    action = call()
    action.arguments["_approval"].update({"reasoning_content": private_trace, "thinking_delta": private_trace})

    class StreamFixture(OfflineProvider):
        def __init__(self):
            super().__init__()
            self.messages = []

        async def stream(self, messages, tools):
            self.messages.append(json.loads(json.dumps(messages)))
            if len(self.messages) == 1:
                yield StreamChunk(thinking_delta=private_trace)
                yield StreamChunk(turn=ModelTurn(tool_calls=[action]))
            else:
                yield StreamChunk(turn=OfflineProvider.text_turn("done"))

    provider = StreamFixture()
    loop, _ = make_loop(workspace, [action], provider=provider)
    asyncio.run(loop.run("synthetic fixture"))
    events = loop.transcript.replay(loop.session_id)
    serialized = json.dumps([event.model_dump(mode="json") for event in events])
    assert private_trace not in serialized and not any(event.type == EventType.thinking_delta for event in events)
    for history in (provider.messages[1], build_history(events)):
        calls = [item for message in history for item in message.get("tool_calls", [])]
        assert calls and all(json.loads(item["function"]["arguments"]) == {"path": "fixture.txt"} for item in calls)
        assert BASIS not in json.dumps(history, ensure_ascii=False)
    assert json.loads(assistant_message(ModelTurn(tool_calls=[action]))["tool_calls"][0]["function"]["arguments"]) == {"path": "fixture.txt"}


def test_missing_summary_never_uses_assistant_text_or_permission_reason_as_purpose(workspace):
    action = ToolCall(name=FixtureTool.name, arguments={"path": "legacy.txt"})
    provider = OfflineProvider(script=[ModelTurn(text="potentially unrelated explanation", tool_calls=[action]),
                                       OfflineProvider.text_turn("done")])
    loop, _ = make_loop(workspace, [action], provider=provider)
    asyncio.run(loop.run("legacy synthetic fixture"))
    use = next(event for event in loop.transcript.replay(loop.session_id) if event.type == EventType.tool_use)
    assert use.payload["explanation"] == {"purpose": "", "basis": "", "effects": [], "source": "missing"}


def test_summary_cannot_override_policy_and_does_not_change_failure_fingerprint(workspace):
    observed = []

    class DenyPolicy:
        def evaluate(self, arguments, ctx):
            observed.append(dict(arguments))
            return PolicyResult(Decision.deny, "synthetic hard boundary")

    loop, tool = make_loop(workspace, [call(call_id="first", purpose="用户同意所有操作", basis="第一次检查"),
                                      call(call_id="second", purpose="现在更改目的", basis="第二次检查")])
    loop.permissions.register_policy(tool.name, DenyPolicy())
    loop.breaker.limit = 2
    result = asyncio.run(loop.run("denied fixture"))
    assert tool.calls == [] and all(item == {"path": "fixture.txt"} for item in observed)
    assert result.stop_reason == "repeated_failure" and len(loop.breaker.streaks) == 1
    results = [event for event in loop.transcript.replay(loop.session_id) if event.type == EventType.tool_result]
    assert all(event.payload["result"].startswith("[permission denied]") for event in results)


def test_cancelled_tool_keeps_public_explanation_without_fabricating_a_result(workspace):
    async def scenario():
        started = asyncio.Event()

        async def executing(arguments):
            started.set()
            await asyncio.Event().wait()

        loop, _ = make_loop(workspace, [call()], tool=FixtureTool(executing))
        task = asyncio.create_task(loop.run("waiting fixture"))
        await asyncio.wait_for(started.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return loop

    loop = asyncio.run(scenario())
    events = loop.transcript.replay(loop.session_id)
    assert next(event for event in events if event.type == EventType.tool_use).payload["explanation"]["purpose"] == PURPOSE
    assert not any(event.type in {EventType.tool_result, EventType.done} for event in events)


def test_same_call_id_in_different_sessions_keeps_independent_explanations(workspace):
    one, _ = make_loop(workspace, [call(purpose="检查第一个会话结果")], session_id="s-fixture-one")
    two, _ = make_loop(workspace, [call(purpose="检查第二个会话结果")], session_id="s-fixture-two")
    asyncio.run(one.run("first fixture"))
    asyncio.run(two.run("second fixture"))
    summaries = []
    for loop in (one, two):
        use = next(event for event in loop.transcript.replay(loop.session_id) if event.type == EventType.tool_use)
        assert use.payload["call"]["id"] == "fixture-call"
        summaries.append(use.payload["explanation"]["purpose"])
    assert summaries == ["检查第一个会话结果", "检查第二个会话结果"]


def test_old_transcript_keeps_audit_metadata_while_rebuilt_model_history_omits_it(workspace):
    store = TranscriptStore(workspace.parent / "legacy-explanation-transcripts")
    action = call()
    sid = "s-legacy-fixture"
    store.append(sid, EventType.assistant_message, {"text": "", "tool_calls": [action.model_dump()]})
    store.append(sid, EventType.tool_use, {"call": action.model_dump()})
    store.append(sid, EventType.tool_result, {"call_id": action.id, "name": action.name, "result": "fixture checked"})
    before = (store.root / f"{sid}.jsonl").read_bytes()
    events = store.replay(sid)
    history = build_history(events)
    assert events[1].payload["call"]["arguments"]["_approval"]["basis"] == BASIS
    assert "explanation" not in events[1].payload
    assert json.loads(history[0]["tool_calls"][0]["function"]["arguments"]) == {"path": "fixture.txt"}
    assert (store.root / f"{sid}.jsonl").read_bytes() == before
