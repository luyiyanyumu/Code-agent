"""Provider-scoped proxy modes; transports are mocked and no chat is sent."""
from __future__ import annotations

import os
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.agent.router import ModelRouter
from code_agent.api.app import create_app
from code_agent.providers import create_provider
from code_agent.providers.openai_compat import LLMError
from code_agent.providers.network import connection_error_detail, normalize_proxy_mode


def profile(**overrides):
    return settings.save_profile({"name": "DeepSeek", "provider": "deepseek", "base_url": "https://api.deepseek.com", "model_default": "deepseek-flash", **overrides})


def test_old_config_and_profile_default_to_system(agent_home):
    assert settings.model_config()["proxy_mode"] == "system"
    p = profile()
    assert p["proxy_mode"] == "system"
    stored = settings._read_profiles()
    stored["profiles"][0].pop("proxy_mode")
    settings._write_profiles(stored)
    assert settings.list_profiles()[0]["proxy_mode"] == "system"


def test_profile_mode_persists_when_old_clients_omit_it(agent_home):
    p = profile(proxy_mode="direct", api_key="test-key")
    edited = settings.save_profile({**p, "proxy_mode": "direct"})
    legacy = {key: value for key, value in edited.items() if key != "proxy_mode"}
    assert settings.save_profile(legacy)["proxy_mode"] == "direct"
    settings.activate_profile(p["id"])
    assert settings.model_config()["proxy_mode"] == "direct"


def test_legacy_profile_activation_resets_a_previously_direct_config(agent_home):
    p = profile()
    settings.save({"proxy_mode": "direct"})
    settings.activate_profile(p["id"])
    assert settings.model_config()["proxy_mode"] == "system"


def test_network_only_profile_update_preserves_connection_and_model(agent_home):
    p = profile(provider="openai_compat", base_url="https://gateway.example/v1",
                model_default="qwen", api_key="test-preserved-key")
    edited = settings.save_profile({"id": p["id"], "proxy_mode": "direct"})
    assert edited["provider"] == p["provider"]
    assert edited["base_url"] == p["base_url"]
    assert edited["model_default"] == p["model_default"]
    settings.activate_profile(p["id"])
    cfg = settings.model_config()
    assert cfg["api_key"] == "test-preserved-key"
    assert cfg["proxy_mode"] == "direct"


def test_explicit_empty_profile_fields_can_still_clear_connection_defaults(agent_home):
    p = profile(proxy_mode="direct")
    edited = settings.save_profile({"id": p["id"], "base_url": "", "model_default": ""})
    assert edited["base_url"] == edited["model_default"] == ""
    assert edited["proxy_mode"] == "direct"


@pytest.mark.parametrize("invalid", ["auto", "DIRECT", True, 42])
def test_invalid_proxy_mode_is_rejected_before_config_write(agent_home, invalid):
    with pytest.raises(ValueError, match="网络代理"):
        settings.save({"proxy_mode": invalid})
    assert not settings.settings_file().exists()
    with pytest.raises(ValueError, match="网络代理"):
        profile(proxy_mode=invalid)


@pytest.mark.parametrize("value", [None, "", "system"])
def test_proxy_mode_system_defaults(value):
    assert normalize_proxy_mode(value) == "system"


@pytest.mark.parametrize("mode,expected", [("system", True), ("direct", False)])
def test_provider_factory_passes_scoped_trust_env_and_reuses_pool(monkeypatch, mode, expected):
    constructors = []
    class Client:
        def __init__(self, **options): constructors.append(options)
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    before = dict(os.environ)
    provider = create_provider("deepseek", api_key="test-key", proxy_mode=mode)
    assert provider._get_client() is provider._get_client()
    assert len(constructors) == 1
    assert constructors[0]["trust_env"] is expected
    assert dict(os.environ) == before


def test_router_reload_rebuilds_with_new_proxy_mode(agent_home):
    settings.save({"provider": "deepseek", "proxy_mode": "system", "model_default": "deepseek-flash"})
    router = ModelRouter()
    old = router.resolve_default()
    settings.save({"proxy_mode": "direct"})
    router.reload()
    new = router.resolve_default()
    assert new is not old
    assert old.proxy_mode == "system"
    assert new.proxy_mode == "direct"
    assert router.resolve_name("deepseek-v4-pro").proxy_mode == "direct"


def test_connection_error_describes_local_proxy_without_credentials(monkeypatch):
    monkeypatch.setattr("code_agent.providers.network.getproxies", lambda: {"https": "http://hidden-user:hidden-password@127.0.0.1:17689"})
    monkeypatch.setattr("code_agent.providers.network.proxy_bypass", lambda _: False)
    message = connection_error_detail(httpx.ConnectError("All connection attempts failed"), "https://api.deepseek.com")
    assert "127.0.0.1:17689" in message
    assert "直接连接" in message
    assert "hidden-user" not in message and "hidden-password" not in message
    direct = connection_error_detail(httpx.ConnectError("failed"), "https://api.deepseek.com", "direct")
    assert "17689" not in direct


def test_no_proxy_bypass_does_not_blame_local_proxy(monkeypatch):
    monkeypatch.setattr("code_agent.providers.network.getproxies", lambda: {"https": "http://127.0.0.1:17689"})
    monkeypatch.setattr("code_agent.providers.network.proxy_bypass", lambda _: True)
    assert "17689" not in connection_error_detail(httpx.ConnectError("failed"), "https://api.deepseek.com")


@pytest.fixture
def client(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    with TestClient(create_app(provider_name="offline")) as value:
        yield value


def test_current_settings_and_active_profile_updates_rebuild_provider(client):
    p = client.post("/api/v1/model-profiles", json={"name": "Direct", "provider": "deepseek", "model_default": "deepseek-flash", "proxy_mode": "direct"}).json()
    active = client.post(f"/api/v1/model-profiles/{p['id']}/activate").json()
    assert active["proxy_mode"] == "direct"
    assert client.app.state.router.resolve_default().proxy_mode == "direct"
    response = client.put(f"/api/v1/model-profiles/{p['id']}", json={"provider": "deepseek", "model_default": "deepseek-flash", "proxy_mode": "system"})
    assert response.status_code == 200
    assert client.get("/api/v1/settings/model").json()["proxy_mode"] == "system"
    assert client.app.state.router.resolve_default().proxy_mode == "system"
    assert client.put("/api/v1/settings/model", json={"provider": "deepseek", "proxy_mode": "direct"}).status_code == 200
    assert client.app.state.router.resolve_default().proxy_mode == "direct"


@pytest.mark.parametrize("update_profile", [False, True])
def test_existing_session_uses_new_network_mode_without_interrupting_current_loop(client, update_profile):
    """Resolve through the actual runtime, without sending a prompt or a model request."""
    p = client.post("/api/v1/model-profiles", json={
        "name": "Session network", "provider": "deepseek",
        "model_default": "deepseek-flash", "proxy_mode": "system",
    }).json()
    assert client.post(f"/api/v1/model-profiles/{p['id']}/activate").status_code == 200
    meta = client.post("/api/v1/sessions", json={}).json()
    manager = client.app.state.sessions
    runtime = manager.get_runtime(meta["id"], meta["user_id"])
    current_loop = runtime._build_loop()
    old_provider = current_loop.provider
    assert old_provider.proxy_mode == "system"

    if update_profile:
        response = client.put(f"/api/v1/model-profiles/{p['id']}", json={"proxy_mode": "direct"})
    else:
        response = client.put("/api/v1/settings/model", json={"proxy_mode": "direct"})
    assert response.status_code == 200

    same_runtime = manager.get_runtime(meta["id"], meta["user_id"])
    next_loop = same_runtime._build_loop()
    assert same_runtime is runtime
    assert next_loop.provider is not old_provider
    assert next_loop.provider.proxy_mode == "direct"
    assert next_loop.provider.model == old_provider.model == "deepseek-flash"
    assert current_loop.provider is old_provider
    assert old_provider.proxy_mode == "system"
    assert not manager.transcript.replay(meta["id"])


def test_api_rejects_unknown_proxy_modes(client):
    assert client.put("/api/v1/settings/model", json={"proxy_mode": "invalid"}).status_code == 422
    assert client.post("/api/v1/model-profiles", json={"name": "Invalid", "proxy_mode": "invalid"}).status_code == 422
    assert client.post("/api/v1/model-profiles/probe", json={"base_url": "https://api.deepseek.com", "proxy_mode": "invalid"}).status_code == 422


def fake_probe(monkeypatch):
    seen = []
    class Client:
        def __init__(self, **options): self.options = options
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, url, headers):
            seen.append({"url": url, "headers": headers, **self.options})
            return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return seen


def test_models_probe_uses_profile_mode_key_and_default_endpoint(client, monkeypatch):
    p = profile(proxy_mode="direct", base_url="", api_key="test-stored-key")
    seen = fake_probe(monkeypatch)
    response = client.post("/api/v1/model-profiles/probe", json={"profile_id": p["id"]})
    assert response.status_code == 200
    assert response.json()["models"] == ["deepseek-flash"]
    assert seen[0]["url"] == "https://api.deepseek.com/models"
    assert seen[0]["trust_env"] is False
    assert seen[0]["headers"]["Authorization"] == "Bearer test-stored-key"
    assert "test-stored-key" not in response.text
    client.post("/api/v1/model-profiles/probe", json={"profile_id": p["id"], "proxy_mode": "system"})
    assert seen[-1]["trust_env"] is True


def test_models_probe_current_config_uses_saved_key_without_returning_it(client, monkeypatch):
    settings.save({"provider": "deepseek", "api_key": "test-current-key", "base_url": "https://api.deepseek.com", "proxy_mode": "direct"})
    client.app.state.router.reload()
    seen = fake_probe(monkeypatch)
    response = client.post("/api/v1/model-profiles/probe", json={"use_current": True, "base_url": "https://api.deepseek.com"})
    assert response.status_code == 200
    assert seen[0]["trust_env"] is False
    assert seen[0]["headers"]["Authorization"] == "Bearer test-current-key"
    assert "test-current-key" not in response.text


def test_failed_profile_reload_restores_mode_and_active_marker(client, monkeypatch):
    settings.save({"provider": "offline"})
    before = settings.load()
    p = profile(proxy_mode="direct", api_key="test-only-key")
    def fail(): raise ValueError("injected reload failure")
    monkeypatch.setattr(client.app.state.router, "reload", fail)
    response = client.post(f"/api/v1/model-profiles/{p['id']}/activate")
    assert response.status_code == 422
    assert settings.load() == before
    assert not settings.list_profiles()[0]["active"]


@pytest.mark.parametrize("stream", [False, True])
def test_mocked_provider_connection_failure_has_proxy_guidance(monkeypatch, stream):
    monkeypatch.setattr("code_agent.providers.network.getproxies", lambda: {"https": "http://127.0.0.1:17689"})
    monkeypatch.setattr("code_agent.providers.network.proxy_bypass", lambda _: False)
    def fail(_): raise httpx.ConnectError("All connection attempts failed")
    provider = create_provider("deepseek", api_key="test-only-key", transport=httpx.MockTransport(fail), max_retries=0)
    async def check():
        try:
            with pytest.raises(LLMError, match="17689.*直接连接"):
                if stream:
                    async for _ in provider.stream([], []): pass
                else:
                    await provider.chat([], [])
        finally: await provider.aclose()
    asyncio.run(check())
