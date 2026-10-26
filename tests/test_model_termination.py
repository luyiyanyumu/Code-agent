"""Terminal model responses retain their outcome without executing partial tools."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from code_agent.agent.loop import AgentLoop, LoopResult
from code_agent.models.events import EventType, ModelTurn, TokenUsage, ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.providers.openai_compat import OpenAICompatProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import WriteFileTool
from code_agent.tools.registry import ToolRegistry
from test_project_agent_team import nodes, settled, team_env


def write_call(path="blocked.txt"):
    return ToolCall(id="call_pending", name="write_file", arguments={"path": path, "content": "actual tool effect"})


def make_loop(workspace, script, metered=None):
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    provider = OfflineProvider(script=script)
    loop = AgentLoop(
        provider=provider, registry=registry,
        permissions=PermissionManager(workspace_root=workspace, mode="full-access"),
        transcript=TranscriptStore(workspace.parent / "termination-transcripts"),
        workspace=workspace, max_turns=4, on_model_usage=metered,
    )
    return loop, provider


@pytest.mark.parametrize("reason,expected", [
    ("length", "max_tokens"), ("max_tokens", "max_tokens"), ("max-tokens", "max_tokens"),
    ("content_filter", "content_filter"), ("error", "error"), ("server_error", "server_error"),
])
@pytest.mark.parametrize("has_tools", [False, True])
def test_abnormal_model_stop_keeps_partial_text_and_usage_without_executing_or_replaying_tools(workspace, reason, expected, has_tools):
    usage = TokenUsage(input_tokens=11, output_tokens=7, cached_tokens=3)
    metered = []
    loop, provider = make_loop(workspace, [
        ModelTurn(text="partial answer", tool_calls=[write_call()] if has_tools else [], stop_reason=reason, usage=usage),
        OfflineProvider.tool_call_turn("write_file", {"path": "replayed.txt", "content": "must not run"}),
    ], metered.append)
    result = asyncio.run(loop.run("complete the task"))
    assert result.stop_reason == expected
    assert result.text == "partial answer"
    assert result.turns == 1 and provider.calls == 1
    assert result.usage == usage and metered == [usage]
    assert not (workspace / "blocked.txt").exists() and not (workspace / "replayed.txt").exists()
    events = loop.transcript.replay(loop.session_id)
    assert not any(event.type in (EventType.tool_use, EventType.tool_result, EventType.approval_request) for event in events)
    done = [event for event in events if event.type == EventType.done]
    assert len(done) == 1
    assert done[0].payload["stop_reason"] == expected
    assert done[0].payload["text"] == "partial answer"
    assert done[0].payload["usage"] == usage.model_dump()
    assert done[0].payload.get("discarded_tool_calls", 0) == int(has_tools)


@pytest.mark.parametrize("reason", ["stop", "end_turn", "tool_use", "tool_calls", "completed"])
@pytest.mark.parametrize("has_tools", [False, True])
def test_normal_model_stops_still_execute_complete_tools_and_finish_only_after_the_text_turn(workspace, reason, has_tools):
    first = ModelTurn(text="normal answer", tool_calls=[write_call()] if has_tools else [], stop_reason=reason,
                      usage=TokenUsage(input_tokens=3, output_tokens=2))
    loop, provider = make_loop(workspace, [first, ModelTurn(text="verified final answer", usage=TokenUsage(input_tokens=2, output_tokens=1))])
    result = asyncio.run(loop.run("normal task"))
    assert result.stop_reason == "end_turn"
    assert result.turns == provider.calls == (2 if has_tools else 1)
    assert result.text == ("verified final answer" if has_tools else "normal answer")
    assert (workspace / "blocked.txt").exists() == has_tools
    if has_tools:
        assert (workspace / "blocked.txt").read_text(encoding="utf-8") == "actual tool effect"
    events = loop.transcript.replay(loop.session_id)
    assert sum(event.type == EventType.tool_use for event in events) == int(has_tools)
    assert sum(event.type == EventType.tool_result for event in events) == int(has_tools)
    assert events[-1].payload["usage"] == result.usage.model_dump()


def test_later_token_limit_preserves_previous_tool_evidence_but_does_not_execute_the_truncated_next_call(workspace):
    usage = TokenUsage(input_tokens=5, output_tokens=4, cached_tokens=2)
    loop, provider = make_loop(workspace, [
        ModelTurn(tool_calls=[write_call("completed.txt")], stop_reason="tool_use", usage=usage),
        ModelTurn(text="unfinished next step", tool_calls=[write_call()], stop_reason="max_tokens", usage=usage),
    ])
    result = asyncio.run(loop.run("two related steps"))
    assert result.stop_reason == "max_tokens" and result.turns == provider.calls == 2
    assert result.usage == TokenUsage(input_tokens=10, output_tokens=8, cached_tokens=4)
    assert (workspace / "completed.txt").exists() and not (workspace / "blocked.txt").exists()
    events = loop.transcript.replay(loop.session_id)
    assert sum(event.type == EventType.tool_use for event in events) == 1
    assert sum(event.type == EventType.tool_result for event in events) == 1
    assert events[-1].payload["stop_reason"] == "max_tokens"


@pytest.mark.parametrize("reason,without_tools,with_tools", [
    (None, "end_turn", "tool_use"), ("stop", "stop", "tool_use"),
    ("end_turn", "end_turn", "tool_use"), ("tool_calls", "tool_use", "tool_use"),
    ("length", "max_tokens", "max_tokens"), ("max_tokens", "max_tokens", "max_tokens"),
    ("content_filter", "content_filter", "content_filter"), ("server_error", "server_error", "server_error"),
])
@pytest.mark.parametrize("has_tools", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_provider_finish_reason_normalization_matches_stream_and_nonstream_and_never_retries_a_terminal_response(agent_home, reason, without_tools, with_tools, has_tools, stream):
    requests = []
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 3}}
    arguments = '{"path":"blocked.txt","content":"truncated' if reason in ("length", "max_tokens") else '{"path":"blocked.txt","content":"complete"}'
    call = {"id": "call_pending", "type": "function", "function": {"name": "write_file", "arguments": arguments}}
    message = {"content": "partial answer", **({"tool_calls": [call]} if has_tools else {})}

    def handler(request):
        requests.append(json.loads(request.content))
        if stream:
            delta = {**message, **({"tool_calls": [{**call, "index": 0}]} if has_tools else {})}
            chunks = [{"choices": [{"delta": delta}]}, {"choices": [{"delta": {}, "finish_reason": reason}]}, {"choices": [], "usage": usage}]
            data = "\n\n".join("data: " + json.dumps(chunk) for chunk in chunks) + "\n\ndata: [DONE]\n\n"
            return httpx.Response(200, content=data.encode("utf-8"), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": reason}], "usage": usage})

    provider = OpenAICompatProvider(api_key="test-only", base_url="http://mock.invalid/v1", model="fixture-model",
                                    transport=httpx.MockTransport(handler), max_retries=2)

    async def collect():
        try:
            if stream:
                chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "task"}], [])]
                assert "".join(chunk.text_delta for chunk in chunks) == "partial answer"
                return chunks[-1].turn
            return await provider.chat([{"role": "user", "content": "task"}], [])
        finally:
            await provider.aclose()

    turn = asyncio.run(collect())
    assert len(requests) == 1
    assert turn.stop_reason == (with_tools if has_tools else without_tools)
    assert turn.text == "partial answer"
    assert turn.usage == TokenUsage(input_tokens=11, output_tokens=7, cached_tokens=3)
    assert len(turn.tool_calls) == int(has_tools)
    if has_tools:
        assert turn.tool_calls[0].id == "call_pending"
        assert turn.tool_calls[0].name == "write_file"
        if reason in ("length", "max_tokens"):
            assert turn.tool_calls[0].arguments == {"_raw": arguments}


@pytest.mark.parametrize("reason", ["max_tokens", "content_filter"])
def test_team_never_accepts_partial_model_output_as_completed_or_pending_audit(team_env, reason):
    e = team_env
    lead, _ = nodes(e)

    async def main():
        async def handler(*_args):
            return LoopResult(text="partial output, work remains", stop_reason=reason, turns=1, usage=TokenUsage())
        e.manager.handler = handler
        worker = await e.team.start(lead["session_id"], e.owner, "limited model response")
        await settled(e, [worker["session_id"]])
        descriptor = e.team.descriptor(worker["session_id"])
        assert descriptor["state"] == "failed" and descriptor["audit"]["decision"] == "none"
        assert reason in descriptor["error"]
        note = e.manager.get_runtime(lead["session_id"], e.owner).notes[-1]
        assert note["audit_required"] is False and note["state"] == "failed"
        assert e.db.query_all("SELECT * FROM project_agent_audits") == []
        await e.team.aclose()

    asyncio.run(main())
