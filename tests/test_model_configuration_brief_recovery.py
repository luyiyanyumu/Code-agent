"""Model edits unblock description jobs without calling or cancelling a model."""
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.api.app import create_app


def instrument(client, monkeypatch):
    intel = client.app.state.code_intel
    seen = []
    original = intel.notify_brief_configuration_changed

    def changed():
        seen.append(client.app.state.router.provider_name)
        original()

    monkeypatch.setattr(intel, "notify_brief_configuration_changed", changed)
    return seen


def test_saved_model_notifies_only_after_successful_reload(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    with TestClient(create_app(provider_name="offline")) as client:
        seen = instrument(client, monkeypatch)
        result = client.put("/api/v1/settings/model", json={
            "provider": "deepseek", "api_key": "fixture-only-key", "model_default": "fixture-model"})
        assert result.status_code == 200
        assert seen == ["deepseek"]
        assert result.json()["api_key_set"] is True
        assert "fixture-only-key" not in result.text
        result = client.put("/api/v1/settings/model", json={"provider": "invalid"})
        assert result.status_code == 422
        assert seen == ["deepseek"]


def test_profile_activation_and_active_edit_notify_recovery(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    with TestClient(create_app(provider_name="offline")) as client:
        seen = instrument(client, monkeypatch)
        created = client.post("/api/v1/model-profiles", json={
            "name": "Local fixture", "provider": "openai_compat", "base_url": "http://127.0.0.1:1/v1",
            "model_default": "fixture-a"})
        assert created.status_code == 200
        assert seen == []
        pid = created.json()["id"]
        assert client.post(f"/api/v1/model-profiles/{pid}/activate").status_code == 200
        assert seen == ["openai_compat"]
        assert client.put(f"/api/v1/model-profiles/{pid}", json={"model_default": "fixture-b"}).status_code == 200
        assert seen == ["openai_compat", "openai_compat"]
        assert settings.model_config()["model_default"] == "fixture-b"


def test_failed_router_reload_does_not_unblock_jobs(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    with TestClient(create_app(provider_name="offline")) as client:
        seen = instrument(client, monkeypatch)
        monkeypatch.setattr(client.app.state.router, "reload", lambda: (_ for _ in ()).throw(ValueError("fixture refusal")))
        previous = settings.load()
        result = client.put("/api/v1/settings/model", json={"provider": "deepseek", "model_default": "fixture-model"})
        assert result.status_code == 422
        assert seen == []
        assert settings.load() == previous


def test_current_connection_can_become_a_provider_without_echoing_or_reentering_key(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    settings.save({"provider": "deepseek", "api_key": "fixture-preserved-secret", "model_default": "fixture-model"})
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/model-profiles", json={"use_current": True, "name": "Current connection",
            "api_key": "", "models": [{"id": "fixture-model"}, {"id": "fixture-other"}]})
        assert response.status_code == 200
        body = response.json()
        assert body["api_key_set"] is True
        assert "fixture-preserved-secret" not in response.text
        assert [model["id"] for model in body["models"]] == ["fixture-model", "fixture-other"]
        assert client.post(f"/api/v1/model-profiles/{body['id']}/activate").status_code == 200
        assert settings.model_config()["api_key"] == "fixture-preserved-secret"
        refused = client.post("/api/v1/model-profiles", json={"use_current": True, "name": "Other", "provider": "openai"})
        assert refused.status_code == 422
