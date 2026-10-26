import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from chat_agent.models import Models
from chat_agent.providers import OfflineProvider, OpenAICompatibleProvider, ProviderError, create_provider, validate_base_url


def config(home, **overrides):
    return SimpleNamespace(home=home, provider="offline", api_base="", api_key="", model="", request_timeout=30, **overrides)


def test_independent_profiles_mask_persist_activate_and_delete(tmp_path):
    models = Models(config(tmp_path))
    assert models.public_settings()["provider"] == "offline"
    secret = "private-test-fixture"
    profile = models.save({"name": "Local compatible", "provider": "openai-compatible", "base_url": "http://localhost:9999/v1/", "model": "local-chat", "api_key": secret, "supports_images": True})
    assert secret not in json.dumps(profile)
    assert "api_key" not in profile and profile["has_key"]
    assert models.current()["provider"] == "offline", "saving alone does not switch provider"
    result = models.activate(profile["id"])
    assert result["active_model"] == "local-chat" and result["supports_images"]
    assert secret not in json.dumps(result)
    reloaded = Models(config(tmp_path))
    assert reloaded.current()["api_key"] == secret
    edited = reloaded.save({"id": profile["id"], "name": "Renamed", "api_key": ""})
    assert edited["has_key"], "empty password fields preserve the existing credential"
    assert reloaded.current()["api_key"] == secret
    reloaded.save({"id": profile["id"], "clear_key": True})
    assert not reloaded.current()["api_key"]
    reloaded.delete(profile["id"])
    assert reloaded.current()["provider"] == "offline"
    assert not list(tmp_path.glob("*.tmp"))


def test_environment_profiles_are_private_and_readonly(tmp_path):
    cfg = SimpleNamespace(home=tmp_path, provider="deepseek", api_base="", api_key="fixture-secret", model="deepseek-chat", request_timeout=8)
    models = Models(cfg)
    assert models.current()["id"] == "environment"
    assert models.current()["request_timeout"] == 8
    assert "fixture-secret" not in json.dumps(models.profiles())
    for builtin in ("offline", "environment"):
        with pytest.raises(ValueError):
            models.save({"id": builtin})
        with pytest.raises(ValueError):
            models.delete(builtin)
    models.activate("offline")
    assert Models(cfg).current()["provider"] == "offline", "explicit saved selection overrides environment default"
    assert "fixture-secret" not in (tmp_path / "models.json").read_text(), "env credentials are not copied to saved profiles"


@pytest.mark.parametrize("bad", ["file:///tmp/service", "https://name:password@example.com/v1", "https://example.com/v1?api_key=secret", "https://example.com/v1#secret", "https://example.com:invalid/v1", "not a URL"])
def test_base_url_rejects_unsafe_or_invalid_credential_carriers(bad):
    with pytest.raises(ValueError):
        validate_base_url(bad)


def test_saved_validation_rejects_bad_profiles_without_mutating_file(tmp_path):
    models = Models(config(tmp_path))
    for payload in ({"provider": "unknown"}, {"name": "test", "model": "x", "base_url": "http://localhost", "api_key": 7}, {"name": "test", "model": "x", "base_url": "http://localhost", "api_key": "bad\nkey"}, {"name": "test", "model": "x", "base_url": "http://localhost", "supports_images": "false"}):
        with pytest.raises(ValueError):
            models.save(payload)
    assert not (tmp_path / "models.json").exists()


def test_profile_mutations_roll_back_when_atomic_replacement_fails(tmp_path, monkeypatch):
    models = Models(config(tmp_path))
    profile = models.save({"name": "fixture", "base_url": "http://localhost/v1", "model": "local"})
    before_bytes = (tmp_path / "models.json").read_bytes()
    before_public = models.profiles()
    def denied(*_):
        raise OSError("fixture denied")
    monkeypatch.setattr("chat_agent.models.os.replace", denied)
    for change in (lambda: models.activate(profile["id"]), lambda: models.delete(profile["id"]), lambda: models.save({"id": profile["id"], "name": "changed"})):
        with pytest.raises(OSError):
            change()
        assert models.profiles() == before_public
        assert (tmp_path / "models.json").read_bytes() == before_bytes
        assert not list(tmp_path.glob("*.tmp"))


def test_offline_stream_is_honest_deterministic_and_cancellable():
    async def run():
        provider = create_provider({"provider": "offline"})
        assert isinstance(provider, OfflineProvider)
        chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "你好"}], [])]
        turn = chunks[-1].turn
        assert "本地演示模式" in turn.text and "未调用 AI 模型" in turn.text
        assert "你好" in turn.text
        assert "".join(chunk.text_delta for chunk in chunks) == turn.text
        assert not turn.tool_calls
        stream = provider.stream([{"role": "user", "content": "x" * 300}], [])
        await anext(stream)
        await stream.aclose()
        return turn.text
    assert asyncio.run(run()) == asyncio.run(run())


def sse(events, *, ending=True):
    text = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return text + ("data: [DONE]\n\n" if ending else "")


def test_compatible_sse_assembles_text_reasoning_tools_and_usage_without_network():
    captured = []
    events = [
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"delta": {"content": "Hello "}}]},
        {"choices": [{"delta": {"content": "world", "tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "deliver_file", "arguments": '{"filename":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"note.txt", "content":"hi"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 4}}},
    ]
    def handler(request):
        captured.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, text=sse(events))
    async def run():
        provider = OpenAICompatibleProvider({"base_url": "https://fixture.invalid/v1", "model": "fixture", "api_key": "test-private"}, transport=httpx.MockTransport(handler))
        return [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}], [{"name": "deliver_file", "description": "output", "parameters": {"type": "object"}}])]
    chunks = asyncio.run(run())
    assert "".join(chunk.thinking_delta for chunk in chunks) == "thinking"
    assert "".join(chunk.text_delta for chunk in chunks) == "Hello world"
    turn = chunks[-1].turn
    assert turn.text == "Hello world" and turn.stop_reason == "tool_use"
    assert turn.tool_calls[0].arguments == {"filename": "note.txt", "content": "hi"}
    assert turn.usage == {"input_tokens": 30, "output_tokens": 20, "cached_tokens": 4}
    assert str(captured[0].url) == "https://fixture.invalid/v1/chat/completions"
    assert captured[0].headers["authorization"] == "Bearer test-private"
    body = json.loads(captured[0].content)
    assert body["tools"][0]["function"]["name"] == "deliver_file"
    assert body["stream"] is True


@pytest.mark.parametrize("response", [
    httpx.Response(401, text="private-test-key"),
    httpx.Response(200, headers={"content-type": "application/json"}, json={"secret": "private-test-key"}),
    httpx.Response(200, headers={"content-type": "text/event-stream"}, text='data: {"error": "private-test-key"}\n\n'),
    httpx.Response(200, headers={"content-type": "text/event-stream"}, text='data: INVALID private-test-key\n\n'),
    httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse([{"choices": [{"delta": {"content": "partial"}}]}])),
])
def test_provider_failures_are_explicit_and_do_not_expose_remote_secrets(response):
    async def run():
        provider = OpenAICompatibleProvider({"base_url": "https://fixture.invalid/v1", "model": "x", "api_key": "private-test-key"}, transport=httpx.MockTransport(lambda _: response))
        return [chunk async for chunk in provider.stream([], [])]
    with pytest.raises(ProviderError) as caught:
        asyncio.run(run())
    assert "private-test-key" not in str(caught.value)


def test_invalid_tool_json_fails_before_tool_execution():
    async def run():
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse([{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "write_file", "arguments": "INVALID"}}]}, "finish_reason": "tool_calls"}]}]))
        provider = OpenAICompatibleProvider({"base_url": "https://fixture.invalid/v1", "model": "x"}, transport=httpx.MockTransport(lambda _: response))
        return [chunk async for chunk in provider.stream([], [])]
    with pytest.raises(ProviderError, match="工具参数"):
        asyncio.run(run())


def test_provider_timeout_is_sanitized_and_cancellation_propagates():
    def timeout(request):
        raise httpx.ReadTimeout("private-test-key", request=request)
    async def run(transport):
        provider = OpenAICompatibleProvider({"base_url": "https://fixture.invalid/v1", "model": "x"}, transport=transport)
        return [chunk async for chunk in provider.stream([], [])]
    with pytest.raises(ProviderError, match="超时") as caught:
        asyncio.run(run(httpx.MockTransport(timeout)))
    assert "private-test-key" not in str(caught.value)
    async def cancel(request):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run(httpx.MockTransport(cancel)))


def test_sse_done_stops_without_waiting_for_connection_eof_and_closes_stream():
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield sse([{"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]}]).encode()
            raise AssertionError("client must stop at [DONE], even if peer holds connection open")
        async def aclose(self):
            self.closed = True
    stream = Stream()
    async def run():
        provider = OpenAICompatibleProvider({"base_url": "https://fixture.invalid/v1", "model": "x"}, transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)))
        return [chunk async for chunk in provider.stream([], [])]
    assert asyncio.run(run())[-1].turn.text == "complete"
    assert stream.closed


@pytest.mark.parametrize("text", [sse([{"choices": [{"delta": {}, "finish_reason": "stop"}]}]), "data: " + "x" * (2 * 1024 * 1024 + 1)], ids=["empty-finish", "oversized-line"])
def test_empty_completed_response_and_unterminated_oversized_lines_fail(text):
    async def run():
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)
        provider = OpenAICompatibleProvider({"base_url": "https://fixture.invalid/v1", "model": "x"}, transport=httpx.MockTransport(lambda _: response))
        return [chunk async for chunk in provider.stream([], [])]
    with pytest.raises(ProviderError):
        asyncio.run(run())
