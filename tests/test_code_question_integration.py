"""Model-directed code questions through the official Harness core.

The source is read by the real tool/permission/registry chain.  Providers are
deterministic local scripts; no shell, remote model or running service is used.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from code_agent.agent.code_question import match_code_question
from code_agent.agent.loop import AgentLoop
from code_agent.models.events import EventType, ModelTurn, TokenUsage
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import Decision, PolicyResult
from code_agent.providers.base import GenerationOptions, StreamChunk, current_generation_options
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import ReadFileTool
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry


SOURCE = "AdaptiveToolWorkflow.java"
QUESTION = f"解释 {SOURCE} 的逻辑和潜在问题，引用实际行号；只用中文，最多六点。"
ANSWER = "依据当前源码作静态解释；未执行测试，其他文件的实际调用关系尚未核查。"
USAGE = TokenUsage(input_tokens=17, output_tokens=9, cached_tokens=3)
LEAK = '<｜DSML｜calls><｜DSML｜invoke name="bash">must-not-run</｜DSML｜invoke></｜DSML｜calls>'


class RecordingProvider(OfflineProvider):
    def __init__(self, turns=None):
        super().__init__(script=turns or [ModelTurn(text=ANSWER, stop_reason="end_turn", usage=USAGE)])
        self.requests = []
        self.before_request = None

    async def chat(self, messages, tools):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        if self.before_request is not None:
            self.before_request(messages, tools)
        return await super().chat(messages, tools)


class CountingRead(ReadFileTool):
    def __init__(self):
        self.reads = []

    async def execute(self, arguments, ctx):
        self.reads.append(copy.deepcopy(arguments))
        return await super().execute(arguments, ctx)


def make_loop(workspace, *, provider=None, permissions=None, registry=None, question=QUESTION, **kwargs):
    workspace.mkdir(parents=True, exist_ok=True)
    reader = CountingRead()
    if registry is None:
        registry = ToolRegistry()
        registry.register(reader)
    provider = provider or RecordingProvider()
    loop = AgentLoop(
        provider=provider, registry=registry,
        permissions=permissions or PermissionManager(workspace_root=workspace, mode="read-only"),
        transcript=TranscriptStore(workspace.parent / "question-transcripts"),
        workspace=workspace, code_question=match_code_question(question), **kwargs,
    )
    return loop, provider, reader


def request_text(provider, index=0):
    return json.dumps(provider.requests[index]["messages"], ensure_ascii=False)


def events(loop, kind):
    return [event for event in loop.transcript.replay(loop.session_id) if event.type == kind]


def test_code_question_has_no_implicit_source_preload_or_extra_request(workspace):
    (workspace / SOURCE).write_text("class Example {}\n", encoding="utf8")
    loop, provider, reader = make_loop(workspace)
    result = asyncio.run(loop.run(QUESTION))
    assert result.text == ANSWER and provider.calls == 1
    assert reader.reads == []
    assert provider.requests[0]["tools"], "A file question keeps native tools available"
    assert not events(loop, EventType.tool_use)


def test_native_read_supplies_actual_evidence_without_losing_user_constraints(workspace):
    (workspace / SOURCE).write_text("class Example { int value = 3; }\n", encoding="utf8")
    provider = RecordingProvider([OfflineProvider.tool_call_turn("read_file", {"path": SOURCE}),
                                  ModelTurn(text=ANSWER, usage=USAGE)])
    loop, provider, reader = make_loop(workspace, provider=provider)
    result = asyncio.run(loop.run(QUESTION, history=[{"role": "user", "content": "保留公开API，不修改文件。"}]))
    assert result.text == ANSWER and provider.calls == 2 and len(reader.reads) == 1
    assert "int value = 3" in request_text(provider, 1)
    assert "保留公开API，不修改文件。" in request_text(provider, 1)
    assert [event.payload["name"] for event in events(loop, EventType.tool_result)] == ["read_file"]


def test_denied_native_code_read_is_never_promoted_to_source_evidence(workspace):
    class DenyRead:
        def evaluate(self, arguments, ctx):
            return PolicyResult(Decision.deny, "private source denied")
    (workspace / SOURCE).write_text("PRIVATE_SOURCE_MUST_NOT_APPEAR", encoding="utf8")
    permissions = PermissionManager(workspace_root=workspace)
    permissions.register_policy("read_file", DenyRead())
    provider = RecordingProvider([OfflineProvider.tool_call_turn("read_file", {"path": SOURCE}),
                                  ModelTurn(text="没有读取权限，无法核查源码。")])
    loop, provider, reader = make_loop(workspace, provider=provider, permissions=permissions)
    asyncio.run(loop.run(QUESTION))
    assert reader.reads == [] and provider.calls == 2
    receipt = events(loop, EventType.tool_result)[0].payload["result"]
    assert receipt.startswith("[permission denied]")
    assert "PRIVATE_SOURCE_MUST_NOT_APPEAR" not in request_text(provider, 1)


def test_textual_dsml_is_not_executed_or_implicitly_retried(workspace):
    loop, provider, reader = make_loop(workspace, provider=RecordingProvider([ModelTurn(text=LEAK)]))
    result = asyncio.run(loop.run(QUESTION))
    assert result.stop_reason == "error" and provider.calls == 1
    assert reader.reads == [] and not events(loop, EventType.tool_use)
    assert "DSML" not in result.text


def test_code_answer_streams_without_duplicate_final_text(workspace):
    class Streaming(OfflineProvider):
        async def stream(self, messages, tools):
            yield StreamChunk(text_delta="依据")
            yield StreamChunk(text_delta="源码")
            yield StreamChunk(turn=ModelTurn(text="依据源码"))
    seen=[]
    loop, _, reader=make_loop(workspace, provider=Streaming(), on_event=seen.append)
    result=asyncio.run(loop.run(QUESTION))
    assert result.text == "依据源码" and not reader.reads
    assert "".join(event.payload["text"] for event in seen if event.type == EventType.assistant_delta) == "依据源码"
