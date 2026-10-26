"""Model capacity persistence/API tests use isolated storage and no LLM calls."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.api.app import create_app


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch):
    for key in ("CODE_AGENT_PROVIDER", "CODE_AGENT_MODEL_DEFAULT", "OPENAI_MODEL"):
        monkeypatch.delenv(key, raising=False)


def profile(**overrides):
    return settings.save_profile({
        "name": "Single model", "provider": "openai_compat",
        "base_url": "https://gateway.example/v1", "model_default": "qwen3",
        **overrides,
    })


def test_config_limits_round_trip_omit_preserves_and_blank_clears(agent_home):
    settings.save({"provider": "openai_compat", "model_default": "qwen3",
                   "context_window": 65_536, "max_output_tokens": 8192, "other": "keep"})
    settings.save({"proxy_mode": "direct"})
    cfg = settings.model_config()
    assert (cfg["context_window"], cfg["max_output_tokens"]) == (65_536, 8192)
    settings.save({"context_window": None, "max_output_tokens": " "})
    assert settings.model_config()["context_window"] is None
    assert settings.model_config()["max_output_tokens"] is None
    assert not set(settings.MODEL_TOKEN_FIELDS) & settings.load().keys()
    assert settings.load()["other"] == "keep"


@pytest.mark.parametrize("field", settings.MODEL_TOKEN_FIELDS)
@pytest.mark.parametrize("invalid", [True, False, 0, -1, 1.5, 8192.0, float("nan"), float("inf"), "8192", [], {}, 10_000_001])
def test_invalid_limit_rejected_before_config_or_profile_write(agent_home, field, invalid):
    with pytest.raises(ValueError, match="整数"):
        settings.save({field: invalid})
    assert not settings.settings_file().exists()
    with pytest.raises(ValueError, match="整数"):
        profile(**{field: invalid})
    assert not settings._profiles_file().exists()


@pytest.mark.parametrize("output", [32768, 40_000])
def test_output_validated_against_automatic_model_window(agent_home, output):
    settings.save({"provider": "openai_compat", "model_default": "qwen3"})
    before = settings.settings_file().read_bytes()
    with pytest.raises(ValueError, match="小于上下文窗口"):
        settings.save({"max_output_tokens": output})
    assert settings.settings_file().read_bytes() == before
    with pytest.raises(ValueError, match="小于上下文窗口"):
        profile(max_output_tokens=output)
    assert not settings._profiles_file().exists()


def test_small_context_update_checks_previously_saved_output(agent_home):
    settings.save({"context_window": 65_536, "max_output_tokens": 8192})
    before = settings.load()
    with pytest.raises(ValueError, match="小于上下文窗口"):
        settings.save({"context_window": 8192})
    assert settings.load() == before


def test_profile_edits_preserve_limits_key_and_catalog(agent_home):
    p = profile(context_window=65_536, max_output_tokens=8192,
                api_key="test-preserved-key", models=[{"id": "qwen3", "name": "Qwen"}])
    edited = settings.save_profile({"id": p["id"], "name": "Renamed", "proxy_mode": "direct"})
    assert (edited["context_window"], edited["max_output_tokens"]) == (65_536, 8192)
    assert edited["effective_context_window"] == 65_536
    assert edited["effective_max_output_tokens"] == 8192
    assert edited["models"] == p["models"]
    assert "test-preserved-key" not in str(edited)
    settings.activate_profile(p["id"])
    assert settings.model_config()["api_key"] == "test-preserved-key"
    assert settings.model_config()["max_output_tokens"] == 8192


def test_invalid_partial_profile_update_preserves_file(agent_home):
    p = profile(context_window=65_536, max_output_tokens=8192)
    before = settings._profiles_file().read_bytes()
    with pytest.raises(ValueError, match="小于上下文窗口"):
        settings.save_profile({"id": p["id"], "context_window": 4096})
    assert settings._profiles_file().read_bytes() == before


def test_legacy_profile_activation_clears_previous_limits(agent_home):
    p = profile()
    store = settings._read_profiles()
    for field in settings.MODEL_TOKEN_FIELDS:
        store["profiles"][0].pop(field, None)
    settings._write_profiles(store)
    settings.save({"context_window": 1_000_000, "max_output_tokens": 32_768})
    settings.activate_profile(p["id"])
    cfg = settings.model_config()
    assert cfg["context_window"] is cfg["max_output_tokens"] is None
    assert settings.list_profiles()[0]["effective_context_window"] == 32_768


@pytest.fixture
def client(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    async def no_embedding_probe(**_):
        return {"found": False}
    monkeypatch.setattr("code_agent.retrieval.embed.autodetect_local", no_embedding_probe)
    with TestClient(create_app(provider_name="offline")) as value:
        yield value


def test_settings_api_partial_update_preserves_provider_and_reports_limits(client):
    response = client.put("/api/v1/settings/model", json={
        "context_window": 65_536, "max_output_tokens": 8192,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "offline"
    assert body["context_window"] == body["effective_context_window"] == 65_536
    assert body["max_output_tokens"] == body["effective_max_output_tokens"] == 8192
    assert client.put("/api/v1/settings/model", json={"proxy_mode": "direct"}).status_code == 200
    assert client.get("/api/v1/settings/model").json()["max_output_tokens"] == 8192
    response = client.put("/api/v1/settings/model", json={
        "context_window": None, "max_output_tokens": None,
    })
    assert response.status_code == 200
    assert response.json()["context_window"] is response.json()["max_output_tokens"] is None
    assert response.json()["effective_context_window"] == 131_072


@pytest.mark.parametrize("invalid", [True, 0, 1.25, "8192", 10_000_001])
def test_api_invalid_limits_do_not_write_settings(client, invalid):
    before = settings.load()
    response = client.put("/api/v1/settings/model", json={"max_output_tokens": invalid})
    assert response.status_code == 422, response.text
    assert settings.load() == before
    response = client.post("/api/v1/model-profiles", json={"name": "Bad", "context_window": invalid})
    assert response.status_code == 422
    assert not settings.list_profiles()


def test_active_profile_api_edits_hot_reload_limits_without_exposing_key(client):
    response = client.post("/api/v1/model-profiles", json={
        "name": "Model budgets", "provider": "deepseek", "model_default": "deepseek-chat",
        "context_window": 65_536, "max_output_tokens": 8192, "api_key": "test-only-key",
    })
    assert response.status_code == 200, response.text
    p = response.json()
    response = client.post(f"/api/v1/model-profiles/{p['id']}/activate")
    assert response.status_code == 200, response.text
    before = client.app.state.router.resolve_default()
    response = client.put(f"/api/v1/model-profiles/{p['id']}", json={"max_output_tokens": 4096})
    assert response.status_code == 200, response.text
    cfg = client.get("/api/v1/settings/model").json()
    assert (cfg["context_window"], cfg["max_output_tokens"]) == (65_536, 4096)
    assert "test-only-key" not in str(cfg)
    assert client.app.state.router.resolve_default() is not before


def test_settings_reload_failure_restores_capacity_and_other_fields(client, monkeypatch):
    settings.save({"provider": "offline", "context_window": 65_536, "max_output_tokens": 8192,
                   "other": "keep"})
    before = settings.load()
    def fail():
        raise ValueError("injected reload failure")
    monkeypatch.setattr(client.app.state.router, "reload", fail)
    response = client.put("/api/v1/settings/model", json={"context_window": 131_072})
    assert response.status_code == 422
    assert settings.load() == before


def test_profile_reload_failure_restores_previous_limits_and_active_marker(client, monkeypatch):
    settings.save({"provider": "offline", "context_window": 65_536, "max_output_tokens": 8192})
    before = settings.load()
    p = profile(context_window=131_072, max_output_tokens=16_384)
    def fail():
        raise ValueError("injected reload failure")
    monkeypatch.setattr(client.app.state.router, "reload", fail)
    response = client.post(f"/api/v1/model-profiles/{p['id']}/activate")
    assert response.status_code == 422
    assert settings.load() == before
    assert not settings.list_profiles()[0]["active"]


def test_active_profile_reload_failure_restores_saved_profile_limits(client, monkeypatch):
    p = profile(context_window=65_536, max_output_tokens=8192)
    assert client.post(f"/api/v1/model-profiles/{p['id']}/activate").status_code == 200
    before_config = settings.load()
    before_profile = settings.list_profiles()[0]
    def fail():
        raise ValueError("injected reload failure")
    monkeypatch.setattr(client.app.state.router, "reload", fail)
    response = client.put(f"/api/v1/model-profiles/{p['id']}", json={"max_output_tokens": 4096})
    assert response.status_code == 422
    assert settings.load() == before_config
    assert settings.list_profiles()[0] == before_profile
