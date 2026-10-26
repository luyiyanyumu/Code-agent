"""Request-local quick-answer settings never mutate a shared provider."""

import asyncio
from dataclasses import FrozenInstanceError
import json

import httpx
import pytest

from code_agent.providers.base import (
    GenerationOptions, current_generation_options, generation_options,
)
from code_agent.providers.openai_compat import LLMError, OpenAICompatProvider


MESSAGES = [{"role": "user", "content": "Explain this file briefly."}]
TOOLS = [{"name": "read_file", "description": "Read a file", "parameters": {}}]


def response(stream=False):
    if stream:
        chunks = [
            {"choices": [{"delta": {"content": "short answer"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        text = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(200, text=text + "data: [DONE]\n\n")
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "short answer"}, "finish_reason": "stop"}],
    })


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model", [
    "deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash",
    "vendor/deepseek-flash", "org/vendor/deepseek-v4-pro",
])
def test_quick_answer_body_for_chat_and_stream(stream, model):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return response(stream)

    provider = OpenAICompatProvider(
        api_key="test-only", model=model, base_url="http://localhost/v1",
        max_output_tokens=32_768, transport=httpx.MockTransport(handler), max_retries=0,
    )

    async def run():
        with generation_options(GenerationOptions(max_output_tokens=3072, simple_answer=True)):
            if stream:
                chunks = [chunk async for chunk in provider.stream(MESSAGES, [])]
                assert chunks[-1].turn.text == "short answer"
            else:
                assert (await provider.chat(MESSAGES, [])).text == "short answer"
        await provider.aclose()

    asyncio.run(run())
    assert len(bodies) == 1
    body = bodies[0]
    assert body["max_tokens"] == 3072
    assert body["thinking"] == {"type": "disabled"}
    assert body["tool_choice"] == "none"
    assert body["messages"] == MESSAGES
    assert set(body) <= {
        "model", "messages", "max_tokens", "thinking", "tool_choice", "stream", "stream_options",
    }
    assert provider.max_output_tokens == 32_768
    assert current_generation_options() is None


@pytest.mark.parametrize("configured,override,expected", [
    (1024, 3072, 1024), (8192, 3072, 3072), (None, 3072, 3072),
    (1024, None, 1024), (None, None, None),
])
def test_request_cap_does_not_raise_configured_output_limit(configured, override, expected):
    provider = OpenAICompatProvider(api_key="test-only", model="deepseek-flash", max_output_tokens=configured)
    with generation_options(GenerationOptions(max_output_tokens=override)):
        assert provider._body(MESSAGES, [], False).get("max_tokens") == expected
    assert provider.max_output_tokens == configured


@pytest.mark.parametrize("model,tools,simple", [
    ("deepseek-flash", TOOLS, True), ("deepseek-flash", [], False),
    ("deepseek-chat", [], True), ("deepseek-reasoner", [], True),
    ("gpt-4o-mini", [], True), ("custom-deepseek-flash", [], True),
    ("deepseek-v4-pro-preview", [], True),
])
def test_thinking_switch_only_for_explicit_models_and_answer_only_request(model, tools, simple):
    provider = OpenAICompatProvider(api_key="test-only", model=model)
    with generation_options(GenerationOptions(max_output_tokens=512, simple_answer=simple)):
        body = provider._body(MESSAGES, tools, False)
    assert "thinking" not in body
    assert "tool_choice" not in body
    assert body.get("tools", []) == [{"type": "function", "function": t} for t in tools]


def test_regular_request_body_remains_unchanged():
    provider = OpenAICompatProvider(api_key="test-only", model="deepseek-flash", max_output_tokens=8192)
    expected = {
        "model": "deepseek-flash", "messages": MESSAGES, "max_tokens": 8192,
        "tools": [{"type": "function", "function": t} for t in TOOLS],
    }
    assert provider._body(MESSAGES, TOOLS, False) == expected
    with generation_options(GenerationOptions(max_output_tokens=512, simple_answer=True)):
        assert provider._body(MESSAGES, [], False)["max_tokens"] == 512
    assert provider._body(MESSAGES, TOOLS, False) == expected


def test_capacity_reserves_actual_request_output_cap():
    provider = OpenAICompatProvider(
        api_key="test-only", model="deepseek-flash", context_window=1200, max_output_tokens=1000,
    )
    messages = [{"role": "user", "content": "x" * 1000}]
    with pytest.raises(LLMError, match="上下文预算"):
        provider._body(messages, [], False)
    with generation_options(GenerationOptions(max_output_tokens=100, simple_answer=True)):
        assert provider._body(messages, [], False)["max_tokens"] == 100
        with pytest.raises(LLMError, match="上下文预算"):
            provider._body([{"role": "user", "content": "x" * 3000}], [], False)


def test_shared_provider_concurrent_requests_keep_independent_options():
    bodies = {}

    async def handler(request):
        body = json.loads(request.content)
        bodies[body["messages"][0]["content"]] = body
        await asyncio.sleep(0)
        return response()

    provider = OpenAICompatProvider(
        api_key="test-only", model="deepseek-flash", base_url="http://localhost/v1",
        max_output_tokens=8192, transport=httpx.MockTransport(handler), max_retries=0,
    )

    async def request(label, options):
        with generation_options(options):
            await asyncio.sleep(0)
            await provider.chat([{"role": "user", "content": label}], [])
            assert current_generation_options() == options
        assert current_generation_options() is None

    async def run():
        await asyncio.gather(
            request("quick", GenerationOptions(max_output_tokens=3072, simple_answer=True)),
            request("normal", None),
            request("bounded", GenerationOptions(max_output_tokens=512)),
        )
        await provider.aclose()

    asyncio.run(run())
    assert bodies["quick"]["max_tokens"] == 3072
    assert bodies["quick"]["thinking"] == {"type": "disabled"}
    assert bodies["normal"]["max_tokens"] == 8192
    assert bodies["bounded"]["max_tokens"] == 512
    assert "thinking" not in bodies["normal"]
    assert "thinking" not in bodies["bounded"]
    assert provider.max_output_tokens == 8192


def test_cancelled_request_cleans_options_before_next_request():
    bodies = []

    async def run():
        started = asyncio.Event()
        blocked = asyncio.Event()

        async def handler(request):
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                started.set()
                await blocked.wait()
            return response()

        provider = OpenAICompatProvider(
            api_key="test-only", model="deepseek-flash", base_url="http://localhost/v1",
            max_output_tokens=8192, transport=httpx.MockTransport(handler), max_retries=0,
        )
        reset = []

        async def quick():
            try:
                with generation_options(GenerationOptions(max_output_tokens=3072, simple_answer=True)):
                    await provider.chat(MESSAGES, [])
            finally:
                reset.append(current_generation_options())

        task = asyncio.create_task(quick())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reset == [None]
        assert current_generation_options() is None
        await provider.chat(MESSAGES, [])
        await provider.aclose()

    asyncio.run(run())
    assert bodies[0]["max_tokens"] == 3072
    assert bodies[1]["max_tokens"] == 8192
    assert "thinking" not in bodies[1]


def test_nested_options_restore_after_exception_and_are_immutable():
    outer = GenerationOptions(max_output_tokens=1024)
    inner = GenerationOptions(max_output_tokens=512, simple_answer=True)
    with pytest.raises(FrozenInstanceError):
        outer.max_output_tokens = 2048
    with generation_options(outer):
        with pytest.raises(RuntimeError):
            with generation_options(inner):
                assert current_generation_options() is inner
                raise RuntimeError("cancelled or failed call")
        assert current_generation_options() is outer
    assert current_generation_options() is None


@pytest.mark.parametrize("cap", [0, -1, True, "512", 10_000_001])
def test_invalid_request_cap_rejected(cap):
    with pytest.raises(ValueError):
        GenerationOptions(max_output_tokens=cap)
