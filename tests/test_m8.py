"""M8 测试：provider 契约（请求构造/响应归一化/重试退避/SSE 流式）。

用 httpx.MockTransport 拦截请求——不需要真实 API key，也不发外网请求。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from code_agent.agent.router import ModelRouter
from code_agent.providers import create_provider
from code_agent.providers.deepseek import DeepSeekProvider
from code_agent.providers.openai_compat import LLMError, OpenAICompatProvider


def _client_provider(handler, **kwargs):
    """构造带 MockTransport 的 provider（不要真 key：显式传 api_key）。"""
    return OpenAICompatProvider(
        api_key="test-key",
        base_url="http://mock.local/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
        max_retries=kwargs.pop("max_retries", 0),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """隔离 config.json：ModelRouter 会读 settings（页面保存的大模型配置），
    必须指向临时 CODE_AGENT_HOME，否则会撞上用户真实 ~/.code-agent/config.json。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / "code-agent-home"))


# ----------------------------------------------------------------------
# 请求构造
# ----------------------------------------------------------------------
def test_request_shape_and_auth_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    provider = _client_provider(handler)
    tools = [{"name": "bash", "description": "run", "parameters": {"type": "object"}}]
    asyncio.run(provider.chat([{"role": "user", "content": "你好"}], tools))

    assert seen["url"] == "http://mock.local/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["messages"][0]["content"] == "你好"
    # tools 必须包成 OpenAI 的 {type: function, function: {...}}
    assert seen["body"]["tools"][0]["type"] == "function"
    assert seen["body"]["tools"][0]["function"]["name"] == "bash"
    assert "stream" not in seen["body"]  # 非流式不该带 stream


def test_no_tools_omits_field():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    asyncio.run(_client_provider(handler).chat([{"role": "user", "content": "x"}], []))
    assert "tools" not in seen["body"]


# ----------------------------------------------------------------------
# 响应归一化
# ----------------------------------------------------------------------
def test_normalize_text_usage_and_finish_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "答案"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        })

    turn = asyncio.run(_client_provider(handler).chat([{"role": "user", "content": "x"}], []))
    assert turn.text == "答案"
    assert turn.tool_calls == []
    assert turn.stop_reason == "stop"
    assert turn.usage.input_tokens == 11 and turn.usage.output_tokens == 7


def test_normalize_tool_calls_with_json_arguments():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command":"ls -la"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        })

    turn = asyncio.run(_client_provider(handler).chat([{"role": "user", "content": "x"}], []))
    assert turn.has_tool_calls
    assert turn.stop_reason == "tool_use"      # 归一化：有 tool_calls 就是 tool_use
    assert turn.tool_calls[0].name == "bash"
    assert turn.tool_calls[0].arguments == {"command": "ls -la"}


def test_broken_tool_arguments_do_not_crash():
    """模型偶尔吐出非法 JSON 参数：不能让整个 loop 崩。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"tool_calls": [
                {"id": "c1", "function": {"name": "bash", "arguments": "{not json"}}
            ]}}],
        })

    turn = asyncio.run(_client_provider(handler).chat([{"role": "user", "content": "x"}], []))
    assert turn.tool_calls[0].arguments == {"_raw": "{not json"}


# ----------------------------------------------------------------------
# 错误处理
# ----------------------------------------------------------------------
def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAICompatProvider(api_key="")
    with pytest.raises(LLMError) as ei:
        asyncio.run(provider.chat([{"role": "user", "content": "x"}], []))
    assert "OPENAI_API_KEY" in str(ei.value)


def test_client_error_not_retried():
    """401/400 这类客户端错误重试没意义——必须只请求一次。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "invalid key"})

    provider = _client_provider(handler, max_retries=3)
    with pytest.raises(LLMError) as ei:
        asyncio.run(provider.chat([{"role": "user", "content": "x"}], []))
    assert calls["n"] == 1
    assert "401" in str(ei.value)


def test_server_error_retried_then_succeeds(monkeypatch):
    """503 -> 重试后成功；验证重试次数与最终结果。"""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}}]})

    provider = _client_provider(handler, max_retries=3)
    turn = asyncio.run(provider.chat([{"role": "user", "content": "x"}], []))
    assert turn.text == "recovered"
    assert calls["n"] == 3


def test_retry_exhausted_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="rate limited")

    provider = _client_provider(handler, max_retries=2)
    with pytest.raises(LLMError) as ei:
        asyncio.run(provider.chat([{"role": "user", "content": "x"}], []))
    assert calls["n"] == 3  # 首次 + 2 次重试
    assert "429" in str(ei.value)


def test_timeout_retried(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = _client_provider(handler, max_retries=2)
    turn = asyncio.run(provider.chat([{"role": "user", "content": "x"}], []))
    assert turn.text == "ok" and calls["n"] == 2


def test_no_choices_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(LLMError) as ei:
        asyncio.run(_client_provider(handler).chat([{"role": "user", "content": "x"}], []))
    assert "choices" in str(ei.value)


def test_retry_after_header_respected(monkeypatch):
    """429 带 Retry-After：等待时长应取响应头，且被上限封顶。"""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "999"}, text="slow down")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = _client_provider(handler, max_retries=2)
    asyncio.run(provider.chat([{"role": "user", "content": "x"}], []))
    assert slept and slept[0] == 20.0  # RETRY_MAX_HEADER_WAIT 封顶


# ----------------------------------------------------------------------
# SSE 流式
# ----------------------------------------------------------------------
def _sse(*chunks: dict) -> bytes:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


def test_stream_text_deltas_and_final_turn():
    payload = _sse(
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=payload, headers={"Content-Type": "text/event-stream"})

    async def collect():
        deltas, final = [], None
        async for chunk in _client_provider(handler).stream([{"role": "user", "content": "x"}], []):
            if chunk.text_delta:
                deltas.append(chunk.text_delta)
            if chunk.turn is not None:
                final = chunk.turn
        return deltas, final

    deltas, final = asyncio.run(collect())
    assert deltas == ["你", "好"]
    assert final is not None
    assert final.text == "你好"
    assert final.stop_reason == "stop"
    assert final.usage.output_tokens == 2


def test_stream_tool_call_fragments_accumulated():
    """流式 tool_calls 是分片下发的，必须按 index 拼回完整参数。"""
    payload = _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_9", "function": {"name": "bash", "arguments": '{"comm'}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'and":"echo hi"}'}}
        ]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    async def collect():
        final = None
        async for chunk in _client_provider(handler).stream([{"role": "user", "content": "x"}], []):
            if chunk.turn is not None:
                final = chunk.turn
        return final

    turn = asyncio.run(collect())
    assert turn.has_tool_calls
    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls[0].id == "call_9"
    assert turn.tool_calls[0].name == "bash"
    assert turn.tool_calls[0].arguments == {"command": "echo hi"}


def test_stream_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def collect():
        async for _ in _client_provider(handler).stream([{"role": "user", "content": "x"}], []):
            pass

    with pytest.raises(LLMError) as ei:
        asyncio.run(collect())
    assert "500" in str(ei.value)


def test_stream_retries_retryable_status(monkeypatch):
    """流式路径同样要重试：503 未输出任何增量时重试是安全的。"""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}
    payload = _sse({"choices": [{"delta": {"content": "恢复"}}]})

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, content=payload, headers={"Content-Type": "text/event-stream"})

    async def collect():
        deltas, final = [], None
        async for chunk in _client_provider(handler, max_retries=3).stream(
            [{"role": "user", "content": "x"}], []
        ):
            if chunk.text_delta:
                deltas.append(chunk.text_delta)
            if chunk.turn is not None:
                final = chunk.turn
        return deltas, final

    deltas, final = asyncio.run(collect())
    assert calls["n"] == 3
    assert deltas == ["恢复"]
    assert final.text == "恢复"


def test_stream_retry_exhausted_raises(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="rate limited")

    async def collect():
        async for _ in _client_provider(handler, max_retries=2).stream(
            [{"role": "user", "content": "x"}], []
        ):
            pass

    with pytest.raises(LLMError) as ei:
        asyncio.run(collect())
    assert calls["n"] == 3  # 首次 + 2 次重试
    assert "429" in str(ei.value)


def test_stream_does_not_retry_after_first_delta(monkeypatch):
    """已经吐过增量再重试会导致内容重复 —— 必须直接失败。"""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    async def broken_stream():
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        raise httpx.ReadError("连接被掐断")

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, content=broken_stream(), headers={"Content-Type": "text/event-stream"}
        )

    seen: list[str] = []

    async def collect():
        async for chunk in _client_provider(handler, max_retries=3).stream(
            [{"role": "user", "content": "x"}], []
        ):
            if chunk.text_delta:
                seen.append(chunk.text_delta)

    with pytest.raises(LLMError) as ei:
        asyncio.run(collect())
    assert seen == ["A"]
    assert calls["n"] == 1  # 不能重发
    assert "已输出增量" in str(ei.value)


# ----------------------------------------------------------------------
# 工厂与路由
# ----------------------------------------------------------------------
def test_factory_openai_and_deepseek():
    assert isinstance(create_provider("openai"), OpenAICompatProvider)
    assert isinstance(create_provider("openai_compat"), OpenAICompatProvider)
    ds = create_provider("deepseek")
    assert isinstance(ds, DeepSeekProvider)
    assert isinstance(ds, OpenAICompatProvider)   # 复用同一实现
    assert ds.base_url == "https://api.deepseek.com"


def test_deepseek_uses_its_own_env_names(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-reasoner")
    ds = DeepSeekProvider()
    assert ds.api_key == "ds-key" and ds.model == "deepseek-reasoner"


def test_router_rejects_unknown_provider():
    with pytest.raises(ValueError) as ei:
        ModelRouter(provider_name="not-a-real-provider")
    assert "未知 provider" in str(ei.value)


def test_router_openai_compat_default_model(captured_env):
    r = ModelRouter(provider_name="openai_compat")
    provider = r.resolve_default()
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.model == "gpt-4o-mini"      # provider 自身默认，不再误用 deepseek 模型名
    assert r.model_name() == "gpt-4o-mini"


@pytest.fixture
def captured_env(monkeypatch):
    """确保默认模型环境变量未被外部污染，走 provider 默认。"""
    monkeypatch.delenv("CODE_AGENT_MODEL_DEFAULT", raising=False)
    return monkeypatch


async def _no_sleep(_seconds: float) -> None:
    return None


def _normalize_usage_response(usage: dict) -> "Any":
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": usage,
        })

    return asyncio.run(_client_provider(handler).chat([{"role": "user", "content": "x"}], []))


def test_normalize_cache_hit_deepseek_style():
    """DeepSeek 官方格式：prompt_cache_hit_tokens。"""
    turn = _normalize_usage_response({
        "prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 80,
    })
    assert turn.usage.input_tokens == 100
    assert turn.usage.cached_tokens == 80


def test_normalize_cache_hit_openai_style():
    """OpenAI 风格：prompt_tokens_details.cached_tokens。"""
    turn = _normalize_usage_response({
        "prompt_tokens": 50, "completion_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 33},
    })
    assert turn.usage.cached_tokens == 33


def test_normalize_cache_absent_defaults_zero():
    turn = _normalize_usage_response({"prompt_tokens": 10, "completion_tokens": 2})
    assert turn.usage.cached_tokens == 0


def _normalize_usage_response(usage: dict) -> "Any":
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": usage,
        })

    return asyncio.run(_client_provider(handler).chat([{"role": "user", "content": "x"}], []))


def test_normalize_cache_hit_deepseek_style():
    """DeepSeek 官方格式：prompt_cache_hit_tokens。"""
    turn = _normalize_usage_response({
        "prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 80,
    })
    assert turn.usage.input_tokens == 100
    assert turn.usage.cached_tokens == 80


def test_normalize_cache_hit_openai_style():
    """OpenAI 风格：prompt_tokens_details.cached_tokens。"""
    turn = _normalize_usage_response({
        "prompt_tokens": 50, "completion_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 33},
    })
    assert turn.usage.cached_tokens == 33


def test_normalize_cache_absent_defaults_zero():
    turn = _normalize_usage_response({"prompt_tokens": 10, "completion_tokens": 2})
    assert turn.usage.cached_tokens == 0
