"""Private provider reasoning survives tool round trips without persistence."""

import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.context.compact import compact_messages, message_size_chars, messages_size_chars
from code_agent.context.prompt import provider_messages
from code_agent.models.events import ModelTurn, ProviderPayload, ToolCall, assistant_message, tool_message
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.openai_compat import LLMError, OpenAICompatProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin.tools_builtin import ReadFileTool
from code_agent.tools.registry import ToolRegistry


PRIVATE = "PRIVATE-PROVIDER-PROTOCOL-ONLY"
TOOLS = [{"name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]
CALL = {"id": "read-1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"source.py"}'}}


def sse(*deltas, finish="tool_calls"):
    chunks = [{"choices": [{"delta": delta}]} for delta in deltas]
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("streaming", [False, True])
def test_mock_http_tool_round_trip_keeps_exact_private_reasoning(streaming):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            if streaming:
                return sse({"reasoning_content": PRIVATE[:15]}, {"reasoning_content": PRIVATE[15:]},
                           {"tool_calls": [{"index": 0, **CALL}]})
            return httpx.Response(200, json={"choices": [{"message": {
                "content": None, "reasoning_content": PRIVATE, "tool_calls": [CALL],
            }, "finish_reason": "tool_calls"}]})
        assistant = next(m for m in body["messages"] if m["role"] == "assistant")
        assert assistant["reasoning_content"] == PRIVATE
        assert "_provider_payload" not in assistant
        assert assistant["content"] is None
        assert body["messages"][-1] == {"role": "tool", "tool_call_id": "read-1", "content": "return 42"}
        if streaming:
            return sse({"content": "返回42"}, finish="stop")
        return httpx.Response(200, json={"choices": [{"message": {"content": "返回42"}, "finish_reason": "stop"}]})

    async def run():
        provider = OpenAICompatProvider(api_key="test-key", model="deepseek-flash", base_url="http://mock.local/v1",
                                        transport=httpx.MockTransport(handler), max_retries=0)
        messages = [{"role": "user", "content": "read it"}]
        if streaming:
            chunks = [chunk async for chunk in provider.stream(provider_messages(messages), TOOLS)]
            assert "".join(c.thinking_delta for c in chunks) == PRIVATE
            first = chunks[-1].turn
        else:
            first = await provider.chat(provider_messages(messages), TOOLS)
        assert PRIVATE not in first.model_dump_json()
        assert PRIVATE not in repr(first)
        messages += [assistant_message(first), tool_message("read-1", "return 42")]
        # This is the request-diagnostics representation used before HTTP.
        assert PRIVATE not in json.dumps(provider_messages(messages))
        assert PRIVATE not in repr(messages)
        if streaming:
            second = [chunk async for chunk in provider.stream(provider_messages(messages), TOOLS)]
            result = second[-1].turn
        else:
            result = await provider.chat(provider_messages(messages), TOOLS)
        await provider.aclose()
        return result

    assert asyncio.run(run()).text == "返回42"
    assert len(bodies) == 2


def test_automatic_segment_keeps_private_protocol_in_memory_only(tmp_path):
    (tmp_path/'source.py').write_text('return 42\n',encoding='utf8')
    requests=[]
    def handler(request):
        body=json.loads(request.content);requests.append(body)
        if len(requests)==1:
            return sse({'reasoning_content':PRIVATE},{'tool_calls':[{'index':0,**CALL}]})
        assistant=next(m for m in body['messages'] if m.get('tool_calls'))
        assert assistant['reasoning_content']==PRIVATE
        return sse({'content':'返回42。'},finish='stop')
    async def run():
        provider=OpenAICompatProvider(api_key='test',model='deepseek-flash',base_url='http://mock.local',transport=httpx.MockTransport(handler),max_retries=0)
        registry=ToolRegistry();registry.register(ReadFileTool())
        loop=AgentLoop(provider,registry,PermissionManager(workspace_root=tmp_path),TranscriptStore(tmp_path/'logs'),workspace=tmp_path,max_turns=1)
        first=await loop.run('读取 source.py 后解释返回值。')
        assert first.segment_boundary and loop.continuation_history
        assert PRIVATE not in json.dumps(loop.continuation_history,ensure_ascii=False)
        second=await loop.run('继续完成。',history=loop.continuation_history,context_meta={'continuation':1})
        await provider.aclose()
        return second
    assert asyncio.run(run()).text=='返回42。'


def test_actual_agent_loop_streams_read_tool_and_persists_no_reasoning(tmp_path):
    (tmp_path / "source.py").write_text("return 42\n", encoding="utf-8")
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse({"reasoning_content": PRIVATE}, {"tool_calls": [{"index": 0, **CALL}]})
        assistant = next(m for m in body["messages"] if m["role"] == "assistant" and m.get("tool_calls"))
        assert assistant["reasoning_content"] == PRIVATE
        assert any(m.get("role") == "tool" and "return 42" in m.get("content", "") for m in body["messages"])
        return sse({"reasoning_content": PRIVATE + "-answer"}, {"content": "文件返回42。"}, finish="stop")

    async def run():
        provider = OpenAICompatProvider(api_key="test-key", model="deepseek-flash", base_url="http://mock.local/v1",
                                        transport=httpx.MockTransport(handler), max_retries=0)
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        store = TranscriptStore(tmp_path / "transcripts")
        loop = AgentLoop(provider, registry, PermissionManager(workspace_root=tmp_path), store,
                         workspace=tmp_path, max_turns=4)
        result = await loop.run("检查运行情况并汇报结果")
        await provider.aclose()
        return result, store.replay(loop.session_id)

    result, events = asyncio.run(run())
    assert result.text == "文件返回42。" and result.turns == 2
    assert len(requests) == 2
    assert PRIVATE not in json.dumps([event.model_dump() for event in events])
    assert all(PRIVATE not in path.read_text(encoding="utf-8") for path in (tmp_path / "transcripts").rglob("*.jsonl"))


def test_private_payload_survives_deepcopy_and_complete_bundle_compaction():
    turn = ModelTurn(tool_calls=[ToolCall(id="c", name="read_file", arguments={"path": "source.py"})],
                     provider_payload={"reasoning_content": PRIVATE})
    messages = [{"role": "user", "content": "task"}, assistant_message(turn), tool_message("c", "source\n" * 600)]
    compacted = compact_messages(deepcopy(messages), max_chars=600)
    assistant = next(m for m in compacted if m.get("tool_calls"))
    assert isinstance(assistant["_provider_payload"], ProviderPayload)
    assert compacted[compacted.index(assistant) + 1]["tool_call_id"] == "c"
    provider = OpenAICompatProvider(api_key="test-key", model="deepseek-flash")
    wire = provider._body(provider_messages(compacted), TOOLS, stream=False)["messages"]
    assert next(m for m in wire if m.get("tool_calls"))["reasoning_content"] == PRIVATE
    assert PRIVATE not in json.dumps(compacted)


@pytest.mark.parametrize("reasoning", [None, "", PRIVATE])
def test_only_observed_reasoning_is_preserved_including_real_empty_string(reasoning):
    provider = OpenAICompatProvider(api_key="test-key", model="deepseek-flash")
    message = {"content": "answer"}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    turn = provider._normalize({"choices": [{"message": message}]})
    wire = provider._body(provider_messages([assistant_message(turn)]), TOOLS, stream=False)["messages"][0]
    assert ("reasoning_content" in wire) == (reasoning is not None)
    if reasoning is not None:
        assert wire["reasoning_content"] == reasoning


def test_disk_placeholder_and_missing_history_never_fabricate_protocol_state():
    turn = ModelTurn(text="answer", provider_payload={"reasoning_content": PRIVATE})
    restored = json.loads(json.dumps(assistant_message(turn)))
    provider = OpenAICompatProvider(api_key="test-key", model="deepseek-flash")
    assert provider._body(provider_messages([restored]), TOOLS, stream=False)["messages"] == [
        {"role": "assistant", "content": "answer"},
    ]
    assert provider._body([assistant_message(ModelTurn(text="plain"))], [], stream=False)["messages"] == [
        {"role": "assistant", "content": "plain"},
    ]


def test_private_reasoning_size_counts_toward_compaction_without_exposing_text():
    def bundle(call_id, reasoning):
        turn = ModelTurn(tool_calls=[ToolCall(id=call_id, name="read_file", arguments={"path": "source.py"})],
                         provider_payload={"reasoning_content": reasoning})
        return [assistant_message(turn), tool_message(call_id, "source evidence")]

    old = bundle("old", PRIVATE * 100)
    latest = bundle("latest", PRIVATE)
    payload = latest[0]["_provider_payload"]
    assert payload.size_chars == len(PRIVATE)
    with pytest.raises(AttributeError):
        payload.size_chars = 0
    plain = {key: value for key, value in latest[0].items() if key != "_provider_payload"}
    assert message_size_chars(latest[0]) == message_size_chars(plain) + len(PRIVATE)
    messages = [{"role": "user", "content": "task"}, *old, *latest]
    compacted = compact_messages(messages, max_chars=500)
    assert messages_size_chars(compacted) <= 500
    assistants = [message for message in compacted if message.get("tool_calls")]
    assert [message["tool_calls"][0]["id"] for message in assistants] == ["latest"]
    assert [message["tool_call_id"] for message in compacted if message.get("role") == "tool"] == ["latest"]
    assert PRIVATE not in json.dumps(compacted)
    assert PRIVATE not in repr(compacted)


def test_http_capacity_preflight_counts_unsealed_reasoning():
    provider = OpenAICompatProvider(api_key="test-key", model="deepseek-flash", context_window=256, max_output_tokens=16)
    plain = assistant_message(ModelTurn(text="answer"))
    assert provider._body([plain], TOOLS, stream=False)["messages"] == [plain]
    private = assistant_message(ModelTurn(text="answer", provider_payload={"reasoning_content": PRIVATE * 100}))
    assert PRIVATE not in json.dumps(private)
    with pytest.raises(LLMError, match="超出上下文预算"):
        provider._body(provider_messages([private]), TOOLS, stream=False)


def test_tiny_compaction_notice_is_not_a_fabricated_assistant_turn():
    old = assistant_message(ModelTurn(text="old report " * 500, provider_payload={"reasoning_content": PRIVATE}))
    latest = assistant_message(ModelTurn(tool_calls=[ToolCall(id="latest", name="read_file", arguments={"path": "source.py"})],
                                        provider_payload={"reasoning_content": PRIVATE}))
    fitted = compact_messages([{"role": "user", "content": "task"}, old, latest, tool_message("latest", "source" * 100)],
                              max_chars=250)
    notices = [message for message in fitted if str(message.get("content") or "").startswith("[compact]")]
    assert not notices  # A complete data wrapper cannot fit this tiny reserve.
    assert all(isinstance(message.get("_provider_payload"), ProviderPayload)
               for message in fitted if message.get("role") == "assistant")
    assert messages_size_chars(fitted) <= 250


def test_incomplete_tool_bundle_keeps_real_assistant_text_protocol_state():
    original = assistant_message(ModelTurn(text="Need one source window", tool_calls=[ToolCall(id="pending", name="read_file")],
                                          provider_payload={"reasoning_content": PRIVATE}))
    fitted = compact_messages([{"role": "user", "content": "task"}, original], max_chars=1000)
    assistant = next(message for message in fitted if message.get("role") == "assistant")
    assert assistant["content"] == original["content"]
    assert "tool_calls" not in assistant
    assert isinstance(assistant["_provider_payload"], ProviderPayload)
    provider = OpenAICompatProvider(api_key="test-key", model="deepseek-flash")
    assert provider._body(provider_messages(fitted), TOOLS, stream=False)["messages"][-1]["reasoning_content"] == PRIVATE
    assert PRIVATE not in json.dumps(fitted)


@pytest.mark.parametrize("history_budget", [260, 520])
def test_actual_agent_loop_repeated_pressure_compaction_keeps_all_assistants_protocol_valid(tmp_path, history_budget):
    (tmp_path / "source.py").write_text("\n".join(f"line_{number}: " + "x" * 100 for number in range(1, 30)), encoding="utf-8")
    requests = []
    saw_omission = []
    saw_trimmed = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        # The strict documented DeepSeek contract applies to every assistant,
        # not merely assistants with tool calls. Old fallback summaries failed
        # here despite every genuine tool call retaining its private payload.
        for message in body["messages"]:
            if message.get("role") == "assistant":
                assert message.get("reasoning_content") == PRIVATE
        saw_omission.extend(message for message in body["messages"]
                            if message.get("role") != "tool" and "[compact]" in str(message.get("content") or ""))
        number = len(requests)
        if sum(len(message.get("tool_calls") or []) for message in body["messages"]) < number - 1:
            saw_trimmed.append(number)
        if number <= 14:
            call = {"id": f"pressure-{number}", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "source.py", "offset": number, "limit": 1}),
            }}
            return sse({"reasoning_content": PRIVATE}, {"tool_calls": [{"index": 0, **call}]})
        return sse({"reasoning_content": PRIVATE}, {"content": "Checked fourteen source windows."}, finish="stop")

    async def run():
        provider = OpenAICompatProvider(api_key="test-key", model="deepseek-flash", base_url="http://mock.local/v1",
                                        transport=httpx.MockTransport(handler), max_retries=0)
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        store = TranscriptStore(tmp_path / "transcripts")
        loop = AgentLoop(provider, registry, PermissionManager(workspace_root=tmp_path), store,
                         workspace=tmp_path, max_turns=20, history_max_chars=history_budget)
        result = await loop.run("检查运行情况并汇报结果")
        await provider.aclose()
        return result, store.replay(loop.session_id)

    result, events = asyncio.run(run())
    assert result.text == "Checked fourteen source windows." and result.turns == 15
    assert len(requests) == 15 and saw_trimmed
    if history_budget == 520:
        assert saw_omission
    assert all(message["role"] == "user" for message in saw_omission)
    assert len([event for event in events if event.type.value == "tool_result"]) == 14
    assert PRIVATE not in json.dumps([event.model_dump() for event in events])
    assert all(PRIVATE not in path.read_text(encoding="utf-8") for path in (tmp_path / "transcripts").rglob("*.jsonl"))

