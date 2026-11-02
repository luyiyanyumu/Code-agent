"""Working-context regressions with recorded fake payloads, no network/models."""

import asyncio
import copy
import json

from code_agent.agent.loop import AgentLoop
from code_agent.context.compact import messages_size_chars
from code_agent.context.prompt import assemble, build_history, dynamic_history_budget, provider_messages
from code_agent.models.events import Event, EventType, ModelTurn, ToolCall, assistant_message, tool_message, user_message
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.tools.registry import ToolRegistry


def make_loop(workspace, **kwargs):
    return AgentLoop(provider=OfflineProvider(script=[OfflineProvider.text_turn("finished")]),
                     registry=ToolRegistry(), permissions=PermissionManager(workspace),
                     transcript=TranscriptStore(workspace.parent / "cost-transcripts"),
                     workspace=workspace, **kwargs)


def test_large_capacity_is_ceiling_not_working_context_target(monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "24000")
    assert dynamic_history_budget(1_000_000, system_chars=10_000, tools_chars=20_000) == 24_000
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "48000")
    assert dynamic_history_budget(1_000_000) == 48_000
    assert dynamic_history_budget(1000, system_chars=220, max_output_tokens=800) <= 220
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "0")
    assert dynamic_history_budget(1_000_000) == 0


def test_tool_argument_cost_triggers_fit_without_double_counting_system(workspace):
    loop = make_loop(workspace, history_max_chars=4000)
    messages = [{"role": "system", "content": "fixed instructions " * 1000}, user_message("Task; keep file A unchanged")]
    for index in range(10):
        call = ToolCall(id=f"c{index}", name="read_file", arguments={"path": "file", "selection": "x" * 600})
        messages += [assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, "ok")]
    original = copy.deepcopy(messages)
    fitted = loop._fit_messages(messages)
    assert messages_size_chars(m for m in messages if m["role"] != "system") > 4000
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 4000
    assert fitted[0] == messages[0]
    assert original == messages
    short = messages[:2]
    assert loop._fit_messages(short) == short


def test_automatic_history_is_bounded_without_muting_generated_instruction(workspace):
    history = build_history([
        Event(seq=1, type=EventType.user_message, payload={"text": "Original task: no database edits"}),
        Event(seq=2, type=EventType.user_message, payload={"text": "Continue from known evidence " * 200, "auto": True}),
        Event(seq=3, type=EventType.user_message, payload={"text": "Also preserve the public API"}),
    ])
    assert history[1]["_context_source"] == "auto"
    loop = make_loop(workspace, history_max_chars=1000)
    fitted = loop._fit_messages(assemble("system", [], history, "Continue actual task", max_chars=1000, automatic=True))
    assert any(m.get("content") == "Original task: no database edits" for m in fitted)
    assert any(m.get("content") == "Also preserve the public API" for m in fitted)
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 1000
    outgoing = provider_messages(fitted)
    assert all("_context_source" not in m for m in outgoing)
    assert [m.get("role") for m in outgoing] == [m.get("role") for m in fitted]


def test_source_bookkeeping_never_reaches_provider(workspace):
    loop = make_loop(workspace)
    received = []
    original_stream = loop.provider.stream

    async def record(messages, tools):
        received.append(copy.deepcopy(messages))
        async for chunk in original_stream(messages, tools):
            yield chunk

    loop.provider.stream = record
    result = asyncio.run(loop.run("Finish the original task", context_meta={"auto": True}))
    assert result.text == "finished"
    assert received and all("_context_source" not in m for m in received[0])
    assert received[0][-1]["role"] == "user"
    assert received[0][-1]["content"] == "Finish the original task"


def test_rich_image_evidence_is_not_converted_to_a_string(workspace):
    loop = make_loop(workspace, history_max_chars=100)
    content = [{"type": "text", "text": "external evidence"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200}}]
    messages = [user_message("Inspect this evidence"), {"role": "user", "content": content, "_context_source": "image"}]
    fitted = loop._fit_messages(messages)
    assert fitted[-1]["content"] == content
    assert provider_messages(fitted)[-1] == {"role": "user", "content": content}


def test_synthetic_long_loop_payload_is_smaller_and_keeps_constraints(workspace, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "24000")
    messages = [user_message("Change only one module. Keep authentication and approvals.")]
    for index in range(40):
        call = ToolCall(id=f"c{index}", name="read_file", arguments={"path": f"file{index}"})
        messages += [assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, "source evidence\n" + "details " * 1800 + "\nexit_code=0")]
    loop = make_loop(workspace, history_max_chars=dynamic_history_budget(1_000_000))
    before = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    fitted = loop._fit_messages(messages)
    after = len(json.dumps(provider_messages(fitted), ensure_ascii=False).encode("utf-8"))
    assert after < before // 8
    assert messages[0] in fitted
    assert any("exit_code=0" in (m.get("content") or "") for m in fitted)


