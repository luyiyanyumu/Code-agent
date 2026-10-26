"""本地 / 内网 OpenAI 兼容后端（Ollama、LM Studio、vLLM）支持测试。

覆盖三件事：
1. is_local_base_url 的地址判定边界（判宽了会把远程服务当本地放过）；
2. 本地后端无需 API Key，但远程后端仍然必须给（否则安全网被捅破）；
3. 本地档案必须显式给模型名——openai_compat 的 default_model 是 gpt-4o-mini，
   打到 Ollama 上是必然的 404，早失败比线上报错好。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.api.app import create_app
from code_agent.providers.base import is_local_base_url
from code_agent.providers.openai_compat import LLMError, OpenAICompatProvider


def _ok_body(*, content: str = "hi") -> dict:
    return {
        "model": "local-model",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }


def _provider(base_url: str, api_key: str = "", handler=None) -> OpenAICompatProvider:
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    return OpenAICompatProvider(
        api_key=api_key,
        base_url=base_url,
        model="local-model",
        transport=httpx.MockTransport(handler or default_handler),
        max_retries=0,
    )


# ----------------------------------------------------------------------
# 地址判定
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:11434/v1", True),      # Ollama 默认
        ("http://localhost:1234/v1", True),       # LM Studio 默认
        ("http://[::1]:8000/v1", True),
        ("http://0.0.0.0:8000/v1", True),
        ("http://192.168.1.9:8000/v1", True),
        ("http://10.1.2.3/v1", True),
        ("http://172.16.0.4/v1", True),           # 172.16/12 私有段起点
        ("http://172.31.255.1/v1", True),         # 私有段终点
        ("http://172.32.0.4/v1", False),          # 越过 172.31 就是公网
        ("http://gw.internal/v1", True),
        ("https://api.deepseek.com", False),
        ("https://api.openai.com/v1", False),
        ("https://ark.cn-beijing.volces.com/api/v3", False),
        ("", False),
        ("not-a-url", False),
    ],
)
def test_is_local_base_url(url: str, expected: bool) -> None:
    assert is_local_base_url(url) is expected


# ----------------------------------------------------------------------
# key 校验
# ----------------------------------------------------------------------
def test_local_backend_runs_without_api_key(monkeypatch) -> None:
    """本地后端不填 key 也能发请求，且 Authorization 头仍然带上（个别网关缺头就 400）。"""
    # 生产环境可能留着 OPENAI_API_KEY，会兜底进 provider（本机服务忽略该头）；
    # 这里清掉才能测到"真的没 key"这条路径。
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_ok_body())

    turn = asyncio.run(
        _provider("http://127.0.0.1:11434/v1", api_key="", handler=handler)
        .chat([{"role": "user", "content": "x"}], [])
    )
    assert turn.text == "hi"
    assert seen["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert seen["auth"] == "Bearer not-needed"


def test_remote_backend_still_requires_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = _provider("https://api.openai.com/v1", api_key="")
    with pytest.raises(LLMError) as ei:
        asyncio.run(provider.chat([{"role": "user", "content": "x"}], []))
    assert "OPENAI_API_KEY" in str(ei.value)


def test_local_flag_exposed_on_provider(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _provider("http://127.0.0.1:11434/v1").local_backend is True
    assert _provider("https://api.deepseek.com").local_backend is False


# ----------------------------------------------------------------------
# 流式 usage（token 统计 / 缓存命中率的唯一来源）
# ----------------------------------------------------------------------
def test_stream_body_requests_usage() -> None:
    body = _provider("http://127.0.0.1:11434/v1")._body(
        [{"role": "user", "content": "x"}], [], stream=True
    )
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def test_stream_parses_final_usage_chunk() -> None:
    """索要 usage 后，带用量的 chunk 的 choices 是空数组——必须照样解析到，
    否则真机上就是"本地模型能聊天，但 token 统计恒为 0"。"""
    lines = [
        'data: {"choices":[{"delta":{"content":"你"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":30,"completion_tokens":24,'
        '"prompt_tokens_details":{"cached_tokens":25}}}',
        "data: [DONE]",
    ]
    sse = ("\n".join(lines) + "\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    async def collect():
        return [
            c
            async for c in _provider("http://127.0.0.1:11434/v1", handler=handler).stream(
                [{"role": "user", "content": "x"}], []
            )
        ]

    chunks = asyncio.run(collect())
    assert "".join(c.text_delta for c in chunks) == "你好"
    turn = chunks[-1].turn
    assert turn.text == "你好"
    assert turn.usage.input_tokens == 30
    assert turn.usage.output_tokens == 24
    assert turn.usage.cached_tokens == 25  # OpenAI 风格 usage，本地后端也这么报


def test_stream_options_auto_dropped_on_legacy_gateway() -> None:
    """不认 stream_options 的老网关：自动去掉该字段重发，而不是把用户挡在门外。"""
    seen: list[dict] = []
    sse = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "unknown field stream_options"}})
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    async def collect():
        return [
            c
            async for c in _provider("http://10.0.0.9:8000/v1", handler=handler).stream(
                [{"role": "user", "content": "x"}], []
            )
        ]

    chunks = asyncio.run(collect())
    assert len(seen) == 2
    assert "stream_options" in seen[0]
    assert "stream_options" not in seen[1]
    assert chunks[-1].turn.text == "ok"
    assert chunks[-1].turn.usage.input_tokens == 0  # 这轮拿不到 usage，但不该炸


def test_stream_tool_calls_without_index() -> None:
    """Ollama 流式不给 tool_calls[].index（ollama#7881）：参数续传必须归到同一次
    调用，而第二个调用不能被并进第一个槽。"""
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_a","function":'
        '{"name":"get_time","arguments":""}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"function":'
        '{"arguments":"{\\"tz\\":\\"UTC\\"}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_b","function":'
        '{"name":"echo","arguments":"{\\"msg\\":\\"hi\\"}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    sse = ("\n".join(lines) + "\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    async def collect():
        return [
            c
            async for c in _provider("http://127.0.0.1:11434/v1", handler=handler).stream(
                [{"role": "user", "content": "x"}], []
            )
        ]

    turn = asyncio.run(collect())[-1].turn
    assert turn.stop_reason == "tool_use"
    assert [tc.name for tc in turn.tool_calls] == ["get_time", "echo"]
    assert turn.tool_calls[0].arguments == {"tz": "UTC"}
    assert turn.tool_calls[1].arguments == {"msg": "hi"}
    assert turn.tool_calls[1].id == "call_b"


def test_stream_tool_arguments_as_object_not_crash() -> None:
    """有的后端把 arguments 当对象塞进来，不能因为 += 直接 TypeError。"""
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"c1","function":'
        '{"name":"f","arguments":{"a":1}}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    sse = ("\n".join(lines) + "\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    async def collect():
        return [
            c
            async for c in _provider("http://127.0.0.1:11434/v1", handler=handler).stream(
                [{"role": "user", "content": "x"}], []
            )
        ]

    turn = asyncio.run(collect())[-1].turn
    assert turn.tool_calls[0].arguments == {"a": 1}


def test_tool_call_id_filled_when_backend_omits_it() -> None:
    """Ollama 不给 tool_call id：必须补一个。id 不是装饰——assistant 消息里的
    tool_calls[].id 与 tool 消息的 tool_call_id 配不上对，严格实现直接 400。"""
    from code_agent.models.events import assistant_message, tool_message

    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"function":'
        '{"name":"get_time","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"function":'
        '{"name":"echo","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    sse = ("\n".join(lines) + "\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    async def collect():
        return [
            c
            async for c in _provider("http://127.0.0.1:11434/v1", handler=handler).stream(
                [{"role": "user", "content": "x"}], []
            )
        ]

    turn = asyncio.run(collect())[-1].turn
    assert [tc.id for tc in turn.tool_calls] == ["call_0", "call_1"]
    assert [t["id"] for t in assistant_message(turn)["tool_calls"]] == ["call_0", "call_1"]
    assert tool_message(turn.tool_calls[1].id, "ok")["tool_call_id"] == "call_1"


def test_nonstream_tool_call_id_filled_too() -> None:
    """非流式走的是另一条归一化路径，同样要补 id。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {"type": "function", "function": {"name": "f", "arguments": "{}"}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    turn = asyncio.run(
        _provider("http://127.0.0.1:11434/v1", handler=handler).chat(
            [{"role": "user", "content": "x"}], []
        )
    )
    assert turn.tool_calls[0].id == "call_0"
    assert turn.stop_reason == "tool_use"


def test_stream_reasoning_goes_to_thinking_channel() -> None:
    """qwen3 的思考流走 delta.reasoning：必须单独成流，不能混进正文，
    也不能把它当成"已输出正文"影响后续判断。"""
    lines = [
        'data: {"choices":[{"delta":{"content":"","reasoning":"好的"}}]}',
        'data: {"choices":[{"delta":{"content":"","reasoning":"，用户想查天气"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"c1","function":'
        '{"name":"get_weather","arguments":"{\\"city\\":\\"北京\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    sse = ("\n".join(lines) + "\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    async def collect():
        return [
            c
            async for c in _provider("http://127.0.0.1:11434/v1", handler=handler).stream(
                [{"role": "user", "content": "x"}], []
            )
        ]

    chunks = asyncio.run(collect())
    assert "".join(c.thinking_delta for c in chunks) == "好的，用户想查天气"
    assert not any(c.text_delta for c in chunks)  # 思考流不得混进正文
    turn = chunks[-1].turn
    assert not turn.text
    assert turn.tool_calls[0].name == "get_weather"
    assert turn.tool_calls[0].arguments == {"city": "北京"}


def test_nonstream_reasoning_not_in_text() -> None:
    """非流式（Ollama 0.34+ 的 /v1）把思考放 message.reasoning，不进 content。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "", "reasoning": "想一下"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    turn = asyncio.run(
        _provider("http://127.0.0.1:11434/v1", handler=handler).chat(
            [{"role": "user", "content": "x"}], []
        )
    )
    assert turn.text == ""  # content 为空就是空，不把 reasoning 混进来


# ----------------------------------------------------------------------
# 档案层
# ----------------------------------------------------------------------
def test_builtin_local_presets(agent_home) -> None:
    by_name = {p["name"]: p for p in settings.BUILTIN_PRESETS}
    expected = {
        "本地 Ollama": "http://127.0.0.1:11434/v1",
        "本地 LM Studio": "http://127.0.0.1:1234/v1",
        "本地 vLLM": "http://127.0.0.1:8000/v1",
    }
    for name, url in expected.items():
        preset = by_name[name]
        assert preset["provider"] == "openai_compat"
        assert preset["base_url"] == url
        # 模型名留空：各人本机模型不同，靠「探测模型」拉取后填入
        assert preset["model_default"] == ""


def test_save_local_profile_requires_model_name(agent_home) -> None:
    with pytest.raises(ValueError, match="模型名"):
        settings.save_profile(
            {
                "name": "本地测试",
                "provider": "openai_compat",
                "base_url": "http://127.0.0.1:11434/v1",
                "model_default": "",
            }
        )

    view = settings.save_profile(
        {
            "name": "本地测试",
            "provider": "openai_compat",
            "base_url": "http://127.0.0.1:11434/v1",
            "model_default": "aura-7b:latest",
        }
    )
    assert view["local"] is True
    assert view["api_key_set"] is False  # 本地后端本来就没有 key


def test_remote_profile_still_allows_empty_model(agent_home) -> None:
    """远程档案留空模型名 = 用 provider 默认（gpt-4o-mini），这是既有行为，不能误伤。"""
    view = settings.save_profile(
        {"name": "远程自建", "provider": "openai", "base_url": "https://api.openai.com/v1"}
    )
    assert view["local"] is False
    assert view["model_default"] == ""


# ----------------------------------------------------------------------
# 探测可用模型（API）
# ----------------------------------------------------------------------
@pytest.fixture
def client(agent_home, monkeypatch):
    for key in ("CODE_AGENT_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> str:
    r = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_probe_models_lists_backend_models(client, monkeypatch) -> None:
    """探测 = GET {base_url}/models，把 id 列表回给前端；这里用假 client 拦截。"""
    seen: dict[str, str] = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"data": [{"id": "aura-7b:latest"}, {"id": "qwen3:8b"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def get(self, url: str, headers=None) -> FakeResponse:
            seen["url"] = url
            seen["auth"] = (headers or {}).get("Authorization", "")
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    token = _login(client)
    r = client.post(
        "/api/v1/model-profiles/probe",
        json={"base_url": "http://127.0.0.1:11434/v1/", "api_key": ""},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["aura-7b:latest", "qwen3:8b"]
    assert seen["url"] == "http://127.0.0.1:11434/v1/models"  # 末尾斜杠已归一
    assert seen["auth"] == "Bearer not-needed"


def test_probe_models_rejects_bad_url(client) -> None:
    token = _login(client)
    r = client.post(
        "/api/v1/model-profiles/probe",
        json={"base_url": "127.0.0.1:11434"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


def test_probe_models_surfaces_connection_error(client, monkeypatch) -> None:
    class BoomClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "BoomClient":
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def get(self, url: str, headers=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", BoomClient)
    token = _login(client)
    r = client.post(
        "/api/v1/model-profiles/probe",
        json={"base_url": "http://127.0.0.1:11434/v1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 502
    assert "连接失败" in r.json()["detail"]
