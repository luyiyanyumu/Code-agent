"""Offline regressions for DSML text output leaking from answer-only turns."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from code_agent.agent.execution_progress import ExecutionProgress
from code_agent.agent.loop import AgentLoop
from code_agent.agent.text_tool_output import is_text_tool_call
from code_agent.models.events import EventType, ModelTurn, TokenUsage, ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.base import Provider, StreamChunk
from code_agent.providers.deepseek import DeepSeekProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool
from code_agent.tools.registry import ToolRegistry


@pytest.mark.parametrize("text", [
    '<｜DSML｜function_calls><｜DSML｜invoke name="bash">',
    '<｜DSML｜calls><｜DSML｜invoke name="bash">',
    '<｜DSML｜invoke name="bash">cd backend && sed -n \'14,56p\' File.java',
    ' \n\ufeff\t< | DSML | calls>\n< | DSML | invoke name="bash">',
    '\u3000\ufeff\u00a0<｜｜DSML｜｜ calls>',
    '< | | DSML | | calls>',
    '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="bash">',
    '</｜DSML｜function_calls>',
    '< / | DSML | calls>',
    '<｜DSML｜/invoke>',
    '<｜DSML｜function_calls',
    '<｜DSML｜function_call',
    '<｜DSML｜funct',
    '< | DSML | inv',
    '<｜DSML｜',
    '<｜DSML',
])
def test_recognizes_response_start_protocol_and_truncated_tags(text):
    assert is_text_tool_call(text)


@pytest.mark.parametrize("text", [
    None, '', ' ', '正文说明：DSML 是模型工具调用标记。',
    '例如 <｜DSML｜function_calls> 不应出现在用户答案里。',
    '`<｜DSML｜calls>` 是协议标记。',
    '```xml\n<｜DSML｜calls>\n```',
    '```\n< | DSML | invoke name="bash">\n```',
    '> <｜DSML｜calls>',
    '<DSML><calls>这是普通 XML 示例</calls></DSML>',
    '<calls><invoke name="bash">普通 XML</invoke></calls>',
    '<｜DSML｜documentation>解释协议</｜DSML｜documentation>',
    '<｜DSML｜callsign>普通标识符</｜DSML｜callsign>',
    '<｜DSML｜function_calls_examples>协议说明',
    '<｜DSML｜calls-are-not-supported>',
    '<｜DSML｜funct 这是普通说明，不是截断标签。',
    '<｜DSML｜parameter name="command">仅参数片段',
    '<', '<｜', '<｜DSM', 'DSML',
])
def test_preserves_normal_answers_documentation_and_code_examples(text):
    assert not is_text_tool_call(text)


def test_recognition_uses_final_assembled_chunks():
    chunks = ['\ufeff ', '<｜D', 'SML', '｜func', 'tion_calls>', '<｜DSML｜invoke name="bash">']
    assert not is_text_tool_call(chunks[1])
    assert is_text_tool_call(''.join(chunks))


@pytest.mark.parametrize("stream", [False, True])
def test_deepseek_empty_schema_explicitly_disables_tool_choice(stream):
    provider = DeepSeekProvider(api_key="offline-placeholder", proxy_mode="direct")
    body = provider._body([{"role": "user", "content": "仅给结论"}], [], stream=stream)
    assert body["tool_choice"] == "none"
    assert "tools" not in body or body["tools"] == []


def test_deepseek_available_tools_keep_function_calling_enabled():
    provider = DeepSeekProvider(api_key="offline-placeholder", proxy_mode="direct")
    schema = {"name": "bash", "description": "fixture", "parameters": {"type": "object"}}
    body = provider._body([{"role": "user", "content": "执行任务"}], [schema], stream=True)
    assert body["tools"] == [{"type": "function", "function": schema}]
    assert body.get("tool_choice") != "none"


LEAK = (
    '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="bash">\n'
    '<｜｜DSML｜｜ parameter name="command" string="true">'
    'cd backend && sed -n \'14,56p;104,147p\' AdaptiveToolWorkflow.java'
    '</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'
)
NORMAL_ANSWER = "已读取的第 1 行声明 AdaptiveToolWorkflow；缺少完整上下文，无法确认其他调用与运行结果。"
FIXTURE_USAGE = TokenUsage(input_tokens=13, output_tokens=5, cached_tokens=2)


def text_turn(text=NORMAL_ANSWER, *, stop_reason="end_turn", tool_calls=()):
    return ModelTurn(text=text, stop_reason=stop_reason, tool_calls=list(tool_calls), usage=FIXTURE_USAGE)


class FragmentProvider(Provider):
    """Deterministic provider that emits the broken protocol across chunks."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def chat(self, messages, tools):
        raise AssertionError("The real loop must consume the streaming path")

    async def stream(self, messages, tools):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        assert self.turns, "The repair must be bounded to one additional request"
        turn = self.turns.pop(0)
        if is_text_tool_call(turn.text) or turn.tool_calls:
            yield StreamChunk(thinking_delta="hidden-invalid-protocol-thought")
        else:
            yield StreamChunk(thinking_delta="归纳已有信息。")
        text = turn.text or ""
        # Split both namespace and tag identifiers, exactly like SSE fragments.
        for start, end in ((0, 2), (2, 7), (7, 12), (12, len(text))):
            if text[start:end]:
                yield StreamChunk(text_delta=text[start:end])
        yield StreamChunk(turn=turn)


class NeverExecute(Tool):
    name = "bash"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}}

    def __init__(self):
        self.calls = 0

    async def execute(self, args, ctx):
        self.calls += 1
        raise AssertionError("Answer-only correction must never execute a command")


def stalled_loop(workspace, turns, **kwargs):
    workspace.mkdir(parents=True, exist_ok=True)
    progress = ExecutionProgress(workspace)
    for _ in range(7):
        progress.observe("read_file", {"path": "AdaptiveToolWorkflow.java"}, "class AdaptiveToolWorkflow {}")
        progress.finish_turn()
    assert progress.should_finalize
    registry = ToolRegistry()
    tool = NeverExecute()
    registry.register(tool)
    provider = FragmentProvider(turns)
    events = []
    metered = []
    checks = []

    async def before_request():
        checks.append(len(provider.requests) + 1)

    loop = AgentLoop(
        provider=provider, registry=registry,
        permissions=PermissionManager(workspace_root=workspace),
        transcript=TranscriptStore(workspace.parent / "text-output-transcripts"),
        workspace=workspace, execution_progress=progress,
        on_event=events.append, on_model_usage=metered.append,
        before_model_turn=kwargs.pop("before_model_turn", before_request), **kwargs,
    )
    return loop, provider, tool, events, metered, checks


def assert_no_protocol_or_tool_execution(loop, provider, tool, events):
    assert all(request["tools"] == [] for request in provider.requests)
    assert tool.calls == 0
    assert not [event for event in events if event.type in (EventType.tool_use, EventType.tool_result)]
    # Runtime error messages can name DSML; raw envelope/parameters never enter
    # visible streams, terminal answers or persisted assistant messages.
    visible = [event for event in events if event.type in (
        EventType.assistant_delta, EventType.thinking_delta, EventType.assistant_message, EventType.done,
    )]
    text = json.dumps([event.payload for event in visible], ensure_ascii=False)
    assert "<｜" not in text and "hidden-invalid-protocol-thought" not in text
    stored = loop.transcript.replay(loop.session_id)
    assert "<｜" not in json.dumps([event.payload for event in stored], ensure_ascii=False)


def test_raw_dsml_answer_is_corrected_once_without_executing_or_displaying_it(workspace):
    loop, provider, tool, events, metered, checks = stalled_loop(workspace, [text_turn(LEAK), text_turn()])
    result = asyncio.run(loop.run("解释 AdaptiveToolWorkflow.java"))

    assert result.stop_reason == "end_turn" and result.text == NORMAL_ANSWER
    assert len(provider.requests) == 2 and len(metered) == 2 and checks == [1, 2]
    assert result.usage == TokenUsage(input_tokens=26, output_tokens=10, cached_tokens=4)
    assert_no_protocol_or_tool_execution(loop, provider, tool, events)
    assert len([event for event in events if event.type == EventType.done]) == 1
    displayed = ''.join(event.payload.get("text", "") for event in events if event.type == EventType.assistant_delta)
    assert displayed == NORMAL_ANSWER


@pytest.mark.parametrize("prefix", ["", "\u3000\ufeff\u00a0"])
def test_normal_stream_also_holds_split_dsml_prefix_before_one_answer_only_repair(workspace, prefix):
    loop, provider, tool, events, metered, checks = stalled_loop(
        workspace, [text_turn(prefix + LEAK), text_turn()],
    )
    # No stalled ledger: even the ordinary streaming path must not flash a
    # split protocol envelope before redirecting to the single correction.
    loop.execution_progress = ExecutionProgress(workspace)
    result = asyncio.run(loop.run("解释源码"))
    assert result.stop_reason == "end_turn" and result.text == NORMAL_ANSWER
    assert len(provider.requests) == 2 and provider.requests[0]["tools"]
    assert provider.requests[1]["tools"] == []
    assert len(metered) == 2 and checks == [1, 2] and tool.calls == 0
    visible_text = ''.join(event.payload.get("text", "") for event in events
                           if event.type in (EventType.assistant_delta, EventType.done))
    assert '<｜' not in visible_text and 'SHOULD_NOT_EXECUTE' not in visible_text
    assert not [event for event in events if event.type in (EventType.tool_use, EventType.tool_result)]


def test_native_tool_call_with_duplicated_protocol_content_keeps_normal_dispatch(workspace):
    class NativeRead(Tool):
        name = "read_file"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

        async def execute(self, args, ctx):
            return "actual fixture read result"

    native_call = ToolCall(name="read_file", arguments={"path": "fixture.txt"})
    loop, provider, tool, events, metered, checks = stalled_loop(
        workspace, [text_turn(LEAK, tool_calls=[native_call]), text_turn()],
    )
    loop.execution_progress = ExecutionProgress(workspace)
    loop.registry.register(NativeRead())
    result = asyncio.run(loop.run("读取并解释源码"))
    assert result.stop_reason == "end_turn" and result.text == NORMAL_ANSWER
    assert len(provider.requests) == 2 and all(request["tools"] for request in provider.requests)
    assert len([event for event in events if event.type == EventType.tool_use]) == 1
    results = [event.payload["result"] for event in events if event.type == EventType.tool_result]
    assert results == ["actual fixture read result"] and tool.calls == 0
    assert len(metered) == 2 and checks == [1, 2]
    assert '<｜' not in json.dumps([event.payload for event in events], ensure_ascii=False)


@pytest.mark.parametrize("broken_second", [
    text_turn(LEAK), text_turn(""),
    text_turn(None, tool_calls=[ToolCall(name="bash", arguments={"command": "SHOULD_NOT_EXECUTE"})]),
])
def test_failed_single_correction_stops_truthfully(workspace, broken_second):
    loop, provider, tool, events, metered, checks = stalled_loop(workspace, [text_turn(LEAK), broken_second])
    result = asyncio.run(loop.run("解释源码"))
    assert result.stop_reason == "stalled"
    assert len(provider.requests) == 2 and len(metered) == 2 and checks == [1, 2]
    assert result.usage.input_tokens == 26
    assert "未完成" in result.text and "已完成" not in result.text
    assert_no_protocol_or_tool_execution(loop, provider, tool, events)


@pytest.mark.parametrize("first", [
    text_turn(""),
    text_turn(None, tool_calls=[ToolCall(name="bash", arguments={"command": "SHOULD_NOT_EXECUTE"})]),
])
def test_initial_structured_call_or_empty_answer_stays_stalled_without_a_repair(workspace, first):
    loop, provider, tool, events, metered, checks = stalled_loop(workspace, [first])
    result = asyncio.run(loop.run("解释源码"))
    assert result.stop_reason == "stalled"
    assert len(provider.requests) == 1 and len(metered) == 1 and checks == [1]
    assert_no_protocol_or_tool_execution(loop, provider, tool, events)


@pytest.mark.parametrize("stop_reason, expected", [
    ("max_tokens", "max_tokens"), ("length", "max_tokens"),
    ("error", "error"), ("content_filter", "content_filter"), ("cancelled", "cancelled"),
])
def test_abnormal_protocol_response_preserves_stop_reason_and_is_not_retried(workspace, stop_reason, expected):
    loop, provider, tool, events, metered, checks = stalled_loop(
        workspace, [text_turn(LEAK, stop_reason=stop_reason)],
    )
    result = asyncio.run(loop.run("解释源码"))
    assert result.stop_reason == expected
    assert len(provider.requests) == 1 and len(metered) == 1 and checks == [1]
    assert result.usage.input_tokens == 13
    assert_no_protocol_or_tool_execution(loop, provider, tool, events)


def test_correction_truncation_is_not_reported_as_completed(workspace):
    loop, provider, tool, events, metered, checks = stalled_loop(
        workspace, [text_turn(LEAK), text_turn("尚未完整输出", stop_reason="max_tokens")],
    )
    result = asyncio.run(loop.run("解释源码"))
    assert result.stop_reason == "max_tokens" and result.text == "尚未完整输出"
    assert len(provider.requests) == 2 and len(metered) == 2 and checks == [1, 2]
    assert_no_protocol_or_tool_execution(loop, provider, tool, events)


def test_correction_does_not_exceed_explicit_turn_limit(workspace):
    loop, provider, tool, events, metered, checks = stalled_loop(workspace, [text_turn(LEAK)], max_turns=1)
    result = asyncio.run(loop.run("解释源码"))
    assert result.stop_reason == "stalled"
    assert not result.segment_boundary
    assert len(provider.requests) == 1 and len(metered) == 1 and checks == [1]
    assert_no_protocol_or_tool_execution(loop, provider, tool, events)


@pytest.mark.parametrize("answer", [NORMAL_ANSWER, '```xml\n<｜DSML｜function_calls>\n```\n这是协议示例。'])
def test_normal_answer_or_fenced_protocol_documentation_does_not_trigger_correction(workspace, answer):
    loop, provider, tool, events, metered, checks = stalled_loop(workspace, [text_turn(answer)])
    result = asyncio.run(loop.run("解释源码及协议示例"))
    assert result.stop_reason == "end_turn" and result.text == answer
    assert len(provider.requests) == 1 and len(metered) == 1 and checks == [1]
    assert tool.calls == 0


def test_correction_obeys_budget_lifecycle_hook_before_second_request(workspace):
    checks = []

    async def reject_second():
        checks.append(1)
        if len(checks) > 1:
            raise RuntimeError("fixture model budget exhausted")

    loop, provider, tool, events, metered, _ = stalled_loop(
        workspace, [text_turn(LEAK)], before_model_turn=reject_second,
    )
    with pytest.raises(RuntimeError, match="fixture model budget exhausted"):
        asyncio.run(loop.run("解释源码"))
    assert len(checks) == 2 and len(provider.requests) == 1 and len(metered) == 1
    assert not [event for event in events if event.type == EventType.done]
    assert_no_protocol_or_tool_execution(loop, provider, tool, events)
