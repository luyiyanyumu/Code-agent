"""Reset-aware generation retry never commits an interrupted tool request."""

import asyncio
import json

import httpx
import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy
from code_agent.providers.base import GenerationOptions, current_generation_options, generation_options
from code_agent.providers.openai_compat import LLMError, OpenAICompatProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool
from code_agent.tools.registry import ToolRegistry


def frame(delta=None, finish=None):
    return ('data: ' + json.dumps({'choices': [{'delta': delta or {}, 'finish_reason': finish}]}) + '\n\n').encode()


def provider(handler, **kwargs):
    result = OpenAICompatProvider(api_key='test-key', base_url='http://mock.local/v1', model='test-model',
                                  transport=httpx.MockTransport(handler), max_retries=kwargs.get('max_retries', 1))
    result._backoff = lambda attempt: 0
    return result


async def collect(client, messages=None):
    with generation_options(GenerationOptions(stream_retry_reset=True)):
        return [chunk async for chunk in client.stream(messages or [{'role': 'user', 'content': 'run'}], [])]


@pytest.mark.parametrize('clean_eof', [False, True])
def test_reset_aware_retry_discards_text_reasoning_and_partial_tools(clean_eof):
    bodies = []

    async def broken():
        yield frame({'content': 'discarded text', 'reasoning_content': 'discarded private', 'tool_calls': [
            {'index': 0, 'id': 'discarded', 'function': {'name': 'probe', 'arguments': '{"value":'}}]})
        if not clean_eof:
            raise httpx.RemoteProtocolError('connection closed')

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(200, content=broken())
        return httpx.Response(200, content=frame({'content': 'complete', 'reasoning_content': 'current private',
            'tool_calls': [{'index': 0, 'id': 'good', 'function': {'name': 'probe', 'arguments': '{"value":2}'}}]})
            + frame(finish='tool_calls') + b'data: [DONE]\n\n')

    chunks = asyncio.run(collect(provider(handler)))
    reset = [chunk for chunk in chunks if chunk.reset]
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert len(reset) == 1 and reset[0].retry_attempt == 1
    assert reset[0].text_delta == reset[0].thinking_delta == '' and reset[0].turn is None
    final = chunks[-1].turn
    assert final.text == 'complete' and final.tool_calls[0].id == 'good'
    assert final.tool_calls[0].arguments == {'value': 2}
    assert final.provider_payload.get('reasoning_content') == 'current private'
    assert current_generation_options() is None


def test_reset_aware_retries_remain_bounded_and_never_emit_partial_turn():
    attempts, chunks = [], []

    async def broken():
        yield frame({'content': 'incomplete'})
        raise httpx.ReadError('closed')

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, content=broken())

    async def run():
        with generation_options(GenerationOptions(stream_retry_reset=True)):
            async for chunk in provider(handler, max_retries=2).stream([], []):
                chunks.append(chunk)

    with pytest.raises(LLMError, match='重试 2 次'):
        asyncio.run(run())
    assert len(attempts) == 3
    assert len([chunk for chunk in chunks if chunk.reset]) == 2
    assert not any(chunk.turn for chunk in chunks)


@pytest.mark.parametrize('status', [400, 401, 403])
def test_reset_aware_auth_and_bad_requests_are_not_retried(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='not authorized')

    with pytest.raises(LLMError, match=f'HTTP {status}'):
        asyncio.run(collect(provider(handler, max_retries=3)))
    assert len(calls) == 1


def test_reset_aware_cancellation_is_not_retried():
    calls = []

    async def canceled():
        yield frame({'content': 'unfinished'})
        raise asyncio.CancelledError()

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=canceled())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(collect(provider(handler)))
    assert len(calls) == 1


@pytest.mark.parametrize('reason', ['stop', 'length', 'content_filter'])
def test_explicit_terminal_before_connection_failure_does_not_regenerate(reason):
    calls = []

    async def terminal():
        yield frame({'content': 'terminal'}, finish=reason)
        raise httpx.RemoteProtocolError('trailing usage lost')

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=terminal())

    chunks = asyncio.run(collect(provider(handler)))
    assert len(calls) == 1 and not any(chunk.reset for chunk in chunks)
    assert chunks[-1].turn.text == 'terminal'
    assert chunks[-1].turn.stop_reason == ('max_tokens' if reason == 'length' else reason)


def test_real_harness_only_dispatches_complete_retry_tool_once(workspace):
    requests, executed, events = [], [], []

    class Probe(Tool):
        name, description = 'probe', 'record a side effect'
        parameters = {'type': 'object', 'properties': {'value': {'type': 'integer'}}}

        async def execute(self, arguments, ctx):
            executed.append(arguments['value'])
            return 'done'

    async def broken():
        yield frame({'content': '<unfinished', 'reasoning_content': 'old private', 'tool_calls': [
            {'index': 0, 'id': 'old', 'function': {'name': 'probe', 'arguments': '{"value":1}'}}]})
        raise httpx.RemoteProtocolError('closed')

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, content=broken())
        if len(requests) == 2:
            return httpx.Response(200, content=frame({'content': 'working', 'reasoning_content': 'new private',
                'tool_calls': [{'index': 0, 'id': 'new', 'function': {'name': 'probe', 'arguments': '{"value":2}'}}]})
                + frame(finish='tool_calls'))
        return httpx.Response(200, content=frame({'content': 'complete'}, finish='stop'))

    registry = ToolRegistry()
    registry.register(Probe())
    permissions = PermissionManager(workspace_root=workspace)
    permissions.register_policy('probe', AllowPolicy())
    loop = AgentLoop(provider(handler), registry, permissions, TranscriptStore(workspace.parent/'stream-transcripts'),
                     workspace=workspace, on_event=events.append, max_turns=3)
    result = asyncio.run(loop.run('run probe once'))
    assert result.text == 'complete' and result.turns == 2
    assert len(requests) == 3 and requests[0] == requests[1]
    assert executed == [2]
    assert [m['tool_call_id'] for m in requests[2]['messages'] if m['role'] == 'tool'] == ['new']
    assert [e.payload['call']['id'] for e in events if e.type.value == 'tool_use'] == ['new']
    resets = [e for e in events if e.payload.get('kind') == 'model_stream_reset']
    assert len(resets) == 1 and resets[0].seq == 0 and resets[0].payload == {'kind': 'model_stream_reset', 'attempt': 1}
    assert 'new private' not in json.dumps([e.model_dump(mode='json') for e in loop.transcript.replay(loop.session_id)])
    assert 'old private' not in json.dumps([e.model_dump(mode='json') for e in loop.transcript.replay(loop.session_id)])

