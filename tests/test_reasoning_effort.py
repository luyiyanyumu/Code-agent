"""Reasoning selection reaches actual HTTP requests without paid API calls."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

from fastapi.testclient import TestClient
import httpx
import pytest

from code_agent import settings
from code_agent.api.app import create_app
from code_agent.providers import create_provider
from code_agent.providers.base import GenerationOptions, generation_options
from code_agent.providers.reasoning import reasoning_metadata
from code_agent.sessions.model_selection import decode_selection


def completion(body):
    if body.get("stream"):
        chunk = {"choices": [{"delta": {"content": "已完成"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "已完成"}, "finish_reason": "stop"}],
    })


@pytest.mark.parametrize("provider", ["deepseek", "openai_compat"])
@pytest.mark.parametrize("effort", ["off", "low", "high", "max"])
@pytest.mark.parametrize("stream", [False, True])
def test_chat_and_stream_honor_effort_even_for_simple_final_answer(provider, effort, stream):
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return completion(captured[-1])

    resolved = create_provider(provider, model="deepseek-flash", reasoning_effort=effort,
        base_url="http://localhost/v1", transport=httpx.MockTransport(handle), max_retries=0)

    async def run():
        with generation_options(GenerationOptions(simple_answer=True)):
            if stream:
                chunks = [chunk async for chunk in resolved.stream([{"role": "user", "content": "直接回答"}], [])]
                assert chunks[-1].turn.text == "已完成"
            else:
                assert (await resolved.chat([{"role": "user", "content": "直接回答"}], [])).text == "已完成"
        await resolved.aclose()

    asyncio.run(run())
    assert len(captured) == 1
    body = captured[0]
    assert body["thinking"] == {"type": "disabled" if effort == "off" else "enabled"}
    if effort == "off":
        assert "reasoning_effort" not in body
    else:
        assert body["reasoning_effort"] == effort
    assert "output_config" not in body  # Harness Messages API field is not a Chat API field.


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"])
def test_advertised_defaults_match_captured_wire_for_verified_ids(model):
    metadata = reasoning_metadata("deepseek", model)
    assert [entry["id"] for entry in metadata["efforts"]] == ["off", "low", "high", "max"]
    resolved = create_provider("deepseek", model=model)
    assert resolved._body([], [], False)["reasoning_effort"] == metadata["defaultEffort"] == "high"


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner", "custom-deepseek-flash", "vendor/deepseek-flash", "gpt-4o-mini"])
def test_unknown_and_legacy_models_do_not_infer_capabilities(model):
    assert reasoning_metadata("openai_compat", model) is None
    resolved = create_provider("openai_compat", model=model)
    with generation_options(GenerationOptions(simple_answer=True)):
        body = resolved._body([], [], False)
    assert "thinking" not in body and "reasoning_effort" not in body
    with pytest.raises(ValueError, match="未声明"):
        create_provider("openai_compat", model=model, reasoning_effort="high")


@pytest.fixture
def client(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    monkeypatch.setenv("CODE_AGENT_CODE_WATCH", "0")
    settings.save({"provider": "openai_compat", "base_url": "http://127.0.0.1:1/v1", "model_default": "global-model"})
    with TestClient(create_app()) as result:
        yield result


def profile():
    return settings.save_profile({"name": "DeepSeek fixture", "provider": "deepseek",
        "base_url": "http://127.0.0.1:1/v1", "model_default": "deepseek-flash",
        "models": [{"id": model, "name": model} for model in ["deepseek-flash", "deepseek-v4-pro", "custom-model"]]})


def new_session(client):
    result = client.post("/api/v1/sessions", json={})
    assert result.status_code == 200, result.text
    return result.json()["id"]


def choose(client, sid, pid, model="deepseek-flash", **extra):
    result = client.put(f"/api/v1/sessions/{sid}/model", json={"provider": pid, "model": model, **extra})
    assert result.status_code == 200, result.text
    return result.json()


def test_real_rest_harness_http_keeps_inflight_effort_and_next_turn_changes(client, monkeypatch):
    started, release = threading.Event(), threading.Event()
    bodies = []

    async def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            started.set()
            await asyncio.to_thread(release.wait, 15)
        return completion(body)

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr("code_agent.agent.router.create_provider",
        lambda name, **kwargs: create_provider(name, transport=transport, max_retries=0, **kwargs))
    p = profile()
    a, b = new_session(client), new_session(client)
    before = settings.load().copy()
    state = choose(client, a, p["id"], reasoningEffort="max")
    assert state["current"]["reasoningEffort"] == "max"
    manager = client.app.state.sessions
    user = client.app.state.local_user.user_id
    runtime = manager.get_runtime(a, user)
    captured = runtime.router.resolve_default()

    def prompt(sid):
        result = client.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "直接回复已完成，不调用工具"})
        assert result.status_code == 200, result.text
        assert "已完成" in result.json()["text"]

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(prompt, a)
        try:
            assert started.wait(15), "REST prompt must reach the real Harness provider HTTP boundary"
            choose(client, a, p["id"], reasoningEffort="low")
            assert captured.reasoning_effort == "max"
            assert runtime.router.resolve_default() is not captured
            assert bodies[0]["reasoning_effort"] == "max" and len(bodies) == 1
        finally:
            release.set()
        first.result(timeout=20)
    prompt(a)
    assert bodies[-1]["reasoning_effort"] == "low"
    # IDs-only model changes reset the old effort to the new model's default.
    state = choose(client, a, p["id"], "deepseek-v4-pro")
    assert state["current"]["reasoningEffort"] == "high"
    manager._runtimes.pop(a).shutdown()
    assert manager.get_runtime(a, user).router.resolve_default().reasoning_effort == "high"
    assert decode_selection(manager.session_repo.get(a)["model_selection"])["reasoningEffort"] == "high"
    prompt(a)
    assert bodies[-1]["model"] == "deepseek-v4-pro" and bodies[-1]["reasoning_effort"] == "high"
    choose(client, a, p["id"], reasoningEffort="off")
    prompt(a)
    assert bodies[-1]["thinking"] == {"type": "disabled"} and "reasoning_effort" not in bodies[-1]
    choose(client, a, p["id"], "custom-model")
    prompt(a)
    assert "thinking" not in bodies[-1] and "reasoning_effort" not in bodies[-1]
    prompt(b)
    assert bodies[-1]["model"] == "global-model" and "reasoning_effort" not in bodies[-1]
    choose(client, a, "", "")
    assert manager.get_runtime(a, user).router.resolve_default().reasoning_effort is None
    assert settings.load() == before and not settings.list_profiles()[0]["active"]
    assert len(bodies) == 6  # Exactly one model request per prompt, no repair/retry generation.


@pytest.mark.parametrize("model,effort", [
    ("deepseek-flash", "xhigh"), ("deepseek-flash", ""), ("deepseek-flash", "MAX"),
    ("custom-model", "high"), ("custom-model", "off"), ("deepseek-flash", "hi\ngh"),
])
def test_rest_rejects_unpublished_efforts_without_changing_saved_selection(client, model, effort):
    p, sid = profile(), new_session(client)
    choose(client, sid, p["id"], reasoningEffort="low")
    manager = client.app.state.sessions
    before = manager.session_repo.get(sid)["model_selection"]
    result = client.put(f"/api/v1/sessions/{sid}/model", json={"provider": p["id"], "model": model, "reasoningEffort": effort})
    assert result.status_code == 422
    assert manager.session_repo.get(sid)["model_selection"] == before


def test_catalog_legacy_selection_and_unavailable_effort_are_transparent(client):
    p, sid = profile(), new_session(client)
    manager = client.app.state.sessions
    raw = {"provider": p["id"], "model": "deepseek-flash"}
    manager.session_repo.set_model_selection(sid, json.dumps(raw))
    response = client.get(f"/api/v1/sessions/{sid}/models")
    state = response.json()
    assert state["available"] and state["current"]["reasoningEffort"] == "high"
    assert json.loads(manager.session_repo.get(sid)["model_selection"]) == raw  # GET never mutates data.
    assert "base_url" not in response.text and "api_key" not in response.text
    models = state["groups"][1]["models"]
    assert models[0]["reasoning"]["defaultEffort"] == "high" and "reasoning" not in models[-1]
    manager.session_repo.set_model_selection(sid, json.dumps({**raw, "reasoningEffort": "removed-value"}))
    state = client.get(f"/api/v1/sessions/{sid}/models").json()
    assert not state["available"] and "推理强度" in state["reason"]
    assert state["current"]["reasoningEffort"] == "removed-value"
    runtime = manager.get_runtime(sid, client.app.state.local_user.user_id)
    with pytest.raises(ValueError, match="推理强度"):
        runtime.router.resolve_default()


def test_inherited_default_and_reset_use_the_same_effort_as_the_displayed_model(client, monkeypatch):
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return completion(bodies[-1])

    monkeypatch.setattr("code_agent.agent.router.create_provider",
        lambda name, **kwargs: create_provider(name, transport=httpx.MockTransport(handle), max_retries=0, **kwargs))
    settings.save({"provider": "deepseek", "model_default": "deepseek-flash"})
    client.app.state.router.reload()
    sid = new_session(client)
    state = client.get(f"/api/v1/sessions/{sid}/models").json()
    assert state["inherited"] and state["current"]["reasoningEffort"] == state["default"]["reasoningEffort"] == "high"
    result = client.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "直接回复已完成，不调用工具"})
    assert result.status_code == 200, result.text
    assert bodies[-1]["thinking"] == {"type": "enabled"} and bodies[-1]["reasoning_effort"] == "high"
    choose(client, sid, "default", reasoningEffort="off")
    # Omission also resets a previously selected effort on the same model.
    state = choose(client, sid, "default")
    assert state["current"]["reasoningEffort"] == "high"
    choose(client, sid, "default", reasoningEffort="max")
    state = choose(client, sid, "", "")
    assert state["inherited"] and state["current"]["reasoningEffort"] == "high"
    manager = client.app.state.sessions
    runtime = manager.get_runtime(sid, client.app.state.local_user.user_id)
    assert runtime.router.resolve_default().reasoning_effort == "high"
