"""Capacity settings affect wire requests and next-loop context budgets offline."""

import asyncio
import json

import httpx
import pytest

from code_agent.agent import router as router_module
from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.context.prompt import dynamic_history_budget
from code_agent.providers.capacity import effective_model_capacity
from code_agent.providers.deepseek import DeepSeekProvider
from code_agent.providers.openai_compat import LLMError, OpenAICompatProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore


@pytest.mark.parametrize("model,window", [
    ("deepseek-chat", 131_072), ("deepseek-v4-pro", 1_000_000),
    ("qwen3:8b", 32_768), ("custom-local-model", 131_072),
])
def test_automatic_capacity_retains_compatibility_estimates(model, window):
    assert effective_model_capacity(model) == {"context_window": window, "max_output_tokens": None}


@pytest.mark.parametrize("value", [True, False, 2.5, "8192", 0, -1, 10_000_001])
def test_capacity_rejects_invalid_direct_provider_settings(value):
    with pytest.raises(ValueError):
        OpenAICompatProvider(context_window=value)
    with pytest.raises(ValueError):
        OpenAICompatProvider(max_output_tokens=value)


def test_output_must_leave_room_for_input_with_automatic_window():
    with pytest.raises(ValueError, match="小于"):
        effective_model_capacity("qwen3:8b", max_output_tokens=32_768)


@pytest.mark.parametrize("provider_type", [OpenAICompatProvider, DeepSeekProvider])
@pytest.mark.parametrize("stream", [False, True])
def test_output_limit_sent_for_streaming_and_nonstreaming(provider_type, stream):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if stream:
            chunks = [
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=payload, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    provider = provider_type(
        api_key="test-only", model="test-model", context_window=8192, max_output_tokens=1024,
        transport=httpx.MockTransport(handler), max_retries=0,
    )

    async def run():
        messages = [{"role": "user", "content": "hello"}]
        if stream:
            result = [chunk async for chunk in provider.stream(messages, [])]
            assert result[-1].turn.text == "ok"
        else:
            assert (await provider.chat(messages, [])).text == "ok"
        await provider.aclose()

    asyncio.run(run())
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 1024
    assert "context_window" not in requests[0]


def test_auto_output_does_not_send_new_protocol_parameter():
    provider = OpenAICompatProvider(model="custom-local-model")
    body = provider._body([{"role": "user", "content": "hello"}], [], False)
    assert "max_tokens" not in body


@pytest.mark.parametrize("large_piece", ["prompt", "schema"])
def test_explicit_capacity_overflow_fails_before_network(large_piece):
    sent = []
    provider = OpenAICompatProvider(
        api_key="test-only", context_window=1000, max_output_tokens=100,
        transport=httpx.MockTransport(lambda request: sent.append(request)), max_retries=0,
    )
    messages = [{"role": "user", "content": "任务" * 1000 if large_piece == "prompt" else "hello"}]
    tools = [{"name": "read", "description": "工具说明" * 1000, "parameters": {}}] if large_piece == "schema" else []

    with pytest.raises(LLMError, match="上下文预算"):
        asyncio.run(provider.chat(messages, tools))
    assert sent == []


def test_output_reservation_and_schema_cost_reduce_history_budget():
    window = 10_000
    previous = dynamic_history_budget(window, system_chars=220, tools_chars=440)
    reserved = dynamic_history_budget(window, system_chars=220, tools_chars=440, max_output_tokens=8000)
    assert reserved < previous
    assert reserved == int((2000 - 300) * 2.2)
    assert dynamic_history_budget(100, system_chars=220, max_output_tokens=50) == 0
    assert dynamic_history_budget(100, max_output_tokens=99) <= 2


def test_router_reload_preserves_captured_provider_settings(monkeypatch):
    cfg = {"provider": "deepseek", "model_default": "deepseek-chat", "context_window": 131_072, "max_output_tokens": 8192}
    monkeypatch.setattr(router_module, "model_config", lambda: dict(cfg))
    router = ModelRouter()
    old = router.resolve_default()
    cfg.update(context_window=262_144, max_output_tokens=32_768)
    router.reload()
    new = router.resolve_default()
    assert new is not old
    assert old.context_window == 131_072 and old.max_output_tokens == 8192
    assert new.context_window == 262_144 and new.max_output_tokens == 32_768
    assert router.model_capacity(old)["context_window"] == 131_072
    assert router.model_capacity(new)["max_output_tokens"] == 32_768


def test_loop_budget_and_context_report_use_same_model_capacity_and_actual_tools(agent_home, monkeypatch):
    cfg = {"provider": "deepseek", "model_default": "deepseek-chat", "context_window": 65_536, "max_output_tokens": 40_000}
    monkeypatch.setattr(router_module, "model_config", lambda: dict(cfg))
    db = Database(agent_home / "capacity.db")
    user = UserRepo(db).create("capacity-test", "test-only")
    manager = SessionManager(
        transcript=TranscriptStore(agent_home / "transcripts"), session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db), router=ModelRouter(), registry=build_registry(),
    )
    meta = manager.session_repo.create(user["id"])
    runtime = manager.get_runtime(meta["id"], user["id"])
    runtime.system_suffix = "项目说明" * 500
    runtime.code_context_provider = lambda: (1, "代码地图" * 800)
    loop = runtime._build_loop()
    expected = dynamic_history_budget(
        65_536, system_chars=len(loop.system_prompt.encode("utf-8")),
        tools_chars=len(json.dumps(loop.registry.schemas(), ensure_ascii=False).encode("utf-8")),
        max_output_tokens=40_000,
    )
    assert loop.history_max_chars == expected
    context = manager.context_usage(meta["id"], user["id"])
    assert context["window"] == 65_536
    assert context["max_output_tokens"] == 40_000
    assert context["input_limit"] == 25_536
    assert context["budget_tokens"] == int(expected / 2.2)
    assert context["system"] > 1000
