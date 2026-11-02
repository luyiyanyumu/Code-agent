"""Search capability and session toggles share isolated settings, never the network."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.api.app import create_app
from code_agent.tools.builtin.web_tools import WEB_TOOL_NAMES
from code_agent.web import search


@pytest.fixture(autouse=True)
def isolated(agent_home, monkeypatch):
    for name in ("CODE_AGENT_PROVIDER", "CODE_AGENT_MODEL_DEFAULT", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
                 "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODE_AGENT_SEARCH_API_KEY", "CODE_AGENT_SEARCH_BASE_URL",
                 "CODE_AGENT_SEARCH_MODEL", "CODE_AGENT_SEARCH_API_VERSION", "CODE_AGENT_SEARCH_MAX_USES",
                 "CODE_AGENT_SEARCH_MAX_RESULTS"):
        monkeypatch.delenv(name, raising=False)
    async def no_network(*args, **kwargs):
        raise AssertionError("Tests must not make a real model/search request")
    monkeypatch.setattr(httpx.AsyncClient, "request", no_network)


@pytest.fixture
def client():
    app = create_app(provider_name="offline")
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        yield client


def official(**extra):
    settings.save({"provider": "deepseek", "api_key": "fixture-model-key", "base_url": "https://api.deepseek.com", **extra})


def test_model_settings_key_is_reused_dynamically_without_secret_disclosure():
    assert search.availability()["available"] is False
    official()
    info = search.availability()
    assert info["available"] is True and info["credential_source"] == "model_settings"
    assert "fixture-model-key" not in json.dumps(info)
    assert search.api_key() == "fixture-model-key"
    settings.save({"api_key": "updated-fixture-key"})
    assert search.api_key() == "updated-fixture-key"
    settings.save({"api_key": ""})
    assert search.availability()["reason_code"] == "WEB_PROVIDER_CREDENTIAL_MISSING"


@pytest.mark.parametrize("provider,base", [
    ("openai", "https://api.openai.com/v1"),
    ("openai_compat", "https://gateway.invalid/v1"),
    ("deepseek", "https://gateway.invalid/v1"),
    ("deepseek", "https://api.deepseek.com.evil.invalid/v1"),
    ("deepseek", "http://api.deepseek.com"),
])
def test_other_provider_or_gateway_keys_are_never_sent_to_official_search(provider, base):
    settings.save({"provider": provider, "api_key": "fixture-other-service-key", "base_url": base,
                   "model_default": "deepseek-chat"})
    assert search.api_key() == ""
    info = search.availability()
    assert info["available"] is False and info["reason_code"] == "WEB_PROVIDER_CREDENTIAL_MISSING"
    assert "fixture-other-service-key" not in json.dumps(info)


def test_custom_search_requires_independent_key_and_respects_credential_priority(monkeypatch):
    official(search={"base_url": "https://search-gateway.invalid/anthropic/v1"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-env-chat-key")
    assert search.api_key() == "" and search.availability()["available"] is False
    monkeypatch.setenv("CODE_AGENT_SEARCH_API_KEY", "fixture-search-env-key")
    assert search.api_key() == "fixture-search-env-key"
    settings.save({"search_api_key": "fixture-search-legacy-key"})
    assert search.api_key() == "fixture-search-legacy-key"
    settings.save({"search": {"base_url": "https://search-gateway.invalid/anthropic/v1", "api_key": "fixture-search-nested-key"}})
    assert search.api_key() == "fixture-search-nested-key"


@pytest.mark.parametrize("endpoint", ["not-a-url", "ftp://search.invalid", "https://user:secret@search.invalid/v1",
    "https://search.invalid/v1?api_key=secret", "https://search.invalid:bad/v1"])
def test_invalid_search_endpoint_is_unavailable_and_safe(endpoint):
    settings.save({"search_api_key": "fixture-search-key", "search": {"base_url": endpoint}})
    info = search.availability()
    assert info["available"] is False and info["reason_code"] == "WEB_PROVIDER_CONFIG_INVALID"
    assert info["base_url"] == ""
    assert "secret" not in json.dumps(info) and "fixture-search-key" not in json.dumps(info)
    assert search.api_key() == ""


def test_actual_search_uses_same_model_settings_credential_and_structured_results():
    official()
    calls = []
    class FakeClient:
        async def post(self, endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            payload = {"content": [{"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "url": "https://example.invalid/docs", "title": "Fixture"}]}]}
            return SimpleNamespace(status_code=200, json=lambda: payload)
    result = asyncio.run(search.DeepSeekSearchProvider(client=FakeClient()).search(["fixture query"]))
    assert len(result.sources) == 1
    assert calls[0][0] == "https://api.deepseek.com/anthropic/v1/messages"
    assert calls[0][1]["headers"]["x-api-key"] == "fixture-model-key"
    assert json.loads(calls[0][1]["content"])["tools"][0]["type"] == "web_search_20250305"


def test_api_hot_model_configuration_enables_toggle_and_persists_schema(client):
    assert client.get("/api/v1/web").json()["available"] is False
    response = client.put("/api/v1/settings/model", json={"provider": "deepseek", "api_key": "fixture-model-key",
        "base_url": "https://api.deepseek.com", "model_default": "deepseek-chat"})
    assert response.status_code == 200
    capability = client.get("/api/v1/web").json()
    assert capability["available"] is True and "fixture-model-key" not in json.dumps(capability)
    sid = client.post("/api/v1/sessions", json={}).json()["id"]
    toggled = client.post(f"/api/v1/sessions/{sid}/web", json={"enabled": True})
    assert toggled.status_code == 200 and toggled.json()["web_enabled"] is True
    manager = client.app.state.sessions
    meta = manager.session_repo.get(sid)
    runtime = manager.get_runtime(sid, meta["user_id"])
    assert meta["web_enabled"] == 1 and runtime.web_enabled is True
    assert set(WEB_TOOL_NAMES) <= set(runtime._build_loop().registry.names())
    manager._runtimes.pop(sid)
    rebuilt = manager.get_runtime(sid, meta["user_id"])
    assert rebuilt.web_enabled is True
    off = client.post(f"/api/v1/sessions/{sid}/web", json={"enabled": False})
    assert off.status_code == 200 and off.json()["web_enabled"] is False
    assert not (set(WEB_TOOL_NAMES) & set(rebuilt._build_loop().registry.names()))


def test_api_rejects_unavailable_enable_and_invalid_bool_but_always_allows_disable(client):
    sid = client.post("/api/v1/sessions", json={}).json()["id"]
    unavailable = client.post(f"/api/v1/sessions/{sid}/web", json={"enabled": True})
    assert unavailable.status_code == 409 and "API key" in unavailable.json()["detail"]
    for enabled in ("false", "true", 0, 1, None, {}, []):
        invalid = client.post(f"/api/v1/sessions/{sid}/web", json={"enabled": enabled})
        assert invalid.status_code == 422
    assert client.post(f"/api/v1/sessions/{sid}/web", json={}).status_code == 422
    disabled = client.post(f"/api/v1/sessions/{sid}/web", json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json()["web_enabled"] is False
    assert client.app.state.sessions.session_repo.get(sid)["web_enabled"] == 0


def test_lost_capability_reconciles_saved_runtime_and_schema(client):
    official()
    sid = client.post("/api/v1/sessions", json={}).json()["id"]
    manager = client.app.state.sessions
    owner = manager.session_repo.get(sid)["user_id"]
    manager.set_web_enabled(sid, owner, True)
    runtime = manager.get_runtime(sid, owner)
    settings.save({"api_key": ""})
    assert manager.get_meta(sid, owner)["web_enabled"] == 0
    assert not runtime.web_enabled and manager.session_repo.get(sid)["web_enabled"] == 0
    assert not (set(WEB_TOOL_NAMES) & set(runtime._build_loop().registry.names()))


def test_prompt_web_override_is_validated_and_persisted_without_running_model(client):
    official()
    sid = client.post("/api/v1/sessions", json={}).json()["id"]
    manager = client.app.state.sessions
    owner = manager.session_repo.get(sid)["user_id"]
    runtime = manager.get_runtime(sid, owner)
    runtime.agent_team = None
    async def no_model(*args):
        return SimpleNamespace(text="fixture")
    runtime._run_loop = no_model
    asyncio.run(runtime.submit("fixture", web=True))
    assert runtime.web_enabled is True and manager.session_repo.get(sid)["web_enabled"] == 1
    asyncio.run(runtime.submit("fixture", web=False))
    assert runtime.web_enabled is False and manager.session_repo.get(sid)["web_enabled"] == 0
    settings.save({"api_key": ""})
    with pytest.raises(ValueError, match="API key"):
        asyncio.run(runtime.submit("fixture", web=True))
    assert manager.session_repo.get(sid)["web_enabled"] == 0
