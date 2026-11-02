"""DSH-style session selection never activates or overwrites global settings."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.api.app import create_app
from code_agent.api.model_selection_api import mount_model_selection_api
from code_agent.models.events import ModelTurn
from code_agent.providers.base import Provider
from code_agent.sessions.model_selection import decode_selection


@pytest.fixture
def api_client(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    monkeypatch.setenv("CODE_AGENT_CODE_WATCH", "0")
    settings.save({"provider": "openai_compat", "base_url": "http://127.0.0.1:1/v1", "model_default": "global-model"})
    app = create_app()
    if not any(route.path.endswith("/{session_id}/models") for route in app.routes):
        mount_model_selection_api(app, app.state.sessions)
    with TestClient(app) as client:
        yield client


def session(client):
    result = client.post("/api/v1/sessions", json={})
    assert result.status_code == 200, result.text
    return result.json()["id"]


def profile(**updates):
    return settings.save_profile({"name": "Local test provider", "provider": "openai_compat",
        "base_url": "http://127.0.0.1:1/v1", "model_default": "alpha",
        "models": [{"id": "alpha", "name": "Alpha"}, {"id": "beta", "name": "Beta"}], **updates})


def choose(client, sid, pid, model):
    response = client.put(f"/api/v1/sessions/{sid}/model", json={"provider": pid, "model": model})
    assert response.status_code == 200, response.text
    return response.json()


def test_catalog_is_grouped_sanitized_and_no_secret_or_endpoint(api_client):
    client = api_client
    p = profile(api_key="fixture-secret-never-return")
    sid = session(client)
    response = client.get(f"/api/v1/sessions/{sid}/models")
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["groups"]] == ["default", p["id"]]
    assert body["current"] == {"provider": "default", "model": "global-model"}
    assert body["inherited"] is True
    group = body["groups"][1]
    assert group["configured"] and group["models"] == [{"id": "alpha", "name": "Alpha"}, {"id": "beta", "name": "Beta"}]
    assert "fixture-secret-never-return" not in response.text
    assert "127.0.0.1" not in response.text and "api_key_hint" not in response.text


def test_selection_persists_per_session_without_global_activation(api_client):
    client = api_client
    p = profile()
    a, b = session(client), session(client)
    before = settings.load().copy()
    chosen = choose(client, a, p["id"], "beta")
    assert chosen["current"] == {"provider": p["id"], "model": "beta"}
    assert chosen["inherited"] is False and chosen["available"]
    assert settings.load() == before
    assert not settings.list_profiles()[0]["active"]
    assert client.get(f"/api/v1/sessions/{b}/models").json()["current"]["model"] == "global-model"
    manager = client.app.state.sessions
    user = client.app.state.local_user.user_id
    runtime = manager.get_runtime(a, user)
    assert runtime.model_name == "beta"
    manager._runtimes.pop(a).shutdown()
    assert manager.get_runtime(a, user).model_name == "beta"
    assert choose(client, a, "", "")["inherited"]
    assert manager.get_runtime(a, user).model_name == "global-model"


@pytest.mark.parametrize("payload", [
    {"provider": "missing", "model": "alpha"},
    {"provider": "default", "model": "unpublished"},
    {"provider": "default", "model": ""},
    {"provider": "default", "model": "global-model", "api_key": "not-allowed"},
])
def test_unknown_or_invalid_selection_does_not_change_state(api_client, payload):
    sid = session(api_client)
    response = api_client.put(f"/api/v1/sessions/{sid}/model", json=payload)
    assert response.status_code == 422
    assert api_client.get(f"/api/v1/sessions/{sid}/models").json()["inherited"]


def test_unconfigured_profile_and_removed_saved_route_never_fall_back(api_client):
    client = api_client
    missing = profile(name="Remote missing", base_url="https://fixture.invalid/v1")
    sid = session(client)
    assert client.put(f"/api/v1/sessions/{sid}/model", json={"provider": missing["id"], "model": "alpha"}).status_code == 422
    p = profile(name="Ready local")
    choose(client, sid, p["id"], "alpha")
    settings.delete_profile(p["id"])
    state = client.get(f"/api/v1/sessions/{sid}/models").json()
    assert state["current"] == {"provider": p["id"], "model": "alpha"}
    assert state["available"] is False
    runtime = client.app.state.sessions.get_runtime(sid, client.app.state.local_user.user_id)
    with pytest.raises(ValueError, match="提供方已移除"):
        runtime.router.resolve_default()


def test_foreign_session_cannot_read_or_modify_model_catalog(api_client):
    client = api_client
    other = client.app.state.users.create("other-model-user", "fixture-unused-password")
    foreign = client.app.state.sessions_repo.create(user_id=other["id"])
    for method, suffix, payload in [("GET", "models", None), ("PUT", "model", {"provider": "default", "model": "global-model"})]:
        response = client.request(method, f"/api/v1/sessions/{foreign['id']}/{suffix}", json=payload)
        assert response.status_code == 404


@pytest.mark.parametrize("change", ["missing_credentials", "model_removed"])
def test_saved_unavailable_model_retains_identity_and_never_resolves_fallback(api_client, monkeypatch, change):
    p = profile(base_url="https://fixture.invalid/v1", api_key="fixture-not-a-real-key")
    sid = session(api_client)
    choose(api_client, sid, p["id"], "beta")
    if change == "missing_credentials":
        store = settings._read_profiles()
        settings._find_profile(store, p["id"])["api_key"] = ""
        settings._write_profiles(store)
    else:
        settings.save_profile_models(p["id"], [{"id": "alpha", "name": "Alpha"}])
    monkeypatch.setattr("code_agent.agent.router.create_provider", lambda *a, **k: pytest.fail("Unavailable models must not construct a fallback provider"))
    state = api_client.get(f"/api/v1/sessions/{sid}/models").json()
    assert state["available"] is False and state["current"] == {"provider": p["id"], "model": "beta"}
    runtime = api_client.app.state.sessions.get_runtime(sid, api_client.app.state.local_user.user_id)
    with pytest.raises(ValueError):
        runtime.router.resolve_default()


def test_legacy_plain_model_is_retained_without_migration():
    assert decode_selection("") is None
    assert decode_selection("legacy-custom") == {"provider": "default", "model": "legacy-custom"}


def test_existing_database_upgrade_preserves_rows(agent_home):
    from code_agent.storage.db import Database, SessionRepo, UserRepo
    path = agent_home / "upgrade.db"
    db = Database(path)
    user = UserRepo(db).create("upgrade", "fixture-unused")
    original = SessionRepo(db).create(user["id"], title="Existing dialogue")
    db.execute("ALTER TABLE sessions DROP COLUMN model_selection")
    db.close()
    reopened = Database(path)
    try:
        row = SessionRepo(reopened).get(original["id"])
        assert row["model_selection"] == ""
        assert {key: value for key, value in row.items() if key != "model_selection"} == {
            key: value for key, value in original.items() if key != "model_selection"}
        assert "model" not in row
    finally:
        reopened.close()


def test_real_runtime_keeps_inflight_provider_and_next_prompt_uses_new_selection(api_client, monkeypatch):
    client = api_client
    started, release = threading.Event(), threading.Event()
    calls, closed = [], []

    class FakeProvider(Provider):
        name = "session-test"
        def __init__(self, model):
            self.model = model
        async def chat(self, messages, tools):
            calls.append(self.model)
            if self.model == "alpha" and not release.is_set():
                started.set()
                await asyncio.to_thread(release.wait, 10)
            return ModelTurn(text="完成 " + self.model)
        async def aclose(self):
            closed.append(self.model)

    monkeypatch.setattr("code_agent.agent.router.create_provider", lambda name, **kw: FakeProvider(kw.get("model") or "global-model"))
    p = profile()
    a, b = session(client), session(client)
    choose(client, a, p["id"], "alpha")
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, f"/api/v1/sessions/{a}/prompt", json={"text": "直接回复完成，不调用工具"})
        assert started.wait(15), "Fake provider must start in the actual runtime"
        choose(client, a, p["id"], "beta")
        assert calls == ["alpha"]
        release.set()
        result = first.result(timeout=20)
    assert result.status_code == 200, result.text
    assert "alpha" in result.json()["text"]
    second = client.post(f"/api/v1/sessions/{a}/prompt", json={"text": "再次直接回复完成"})
    assert second.status_code == 200, second.text
    assert "beta" in second.json()["text"] and calls == ["alpha", "beta"]
    assert client.get(f"/api/v1/sessions/{b}/models").json()["current"]["model"] == "global-model"
    assert settings.model_config()["model_default"] == "global-model"


def test_shared_pool_closes_retired_selected_clients(api_client, monkeypatch):
    closed = []
    class P:
        model = "alpha"
        async def aclose(self): closed.append(self)
    monkeypatch.setattr("code_agent.agent.router.create_provider", lambda *a, **k: P())
    p = profile()
    sid = session(api_client)
    choose(api_client, sid, p["id"], "alpha")
    manager = api_client.app.state.sessions
    runtime = manager.get_runtime(sid, api_client.app.state.local_user.user_id)
    first = runtime.router.resolve_default()
    manager.router.reload()
    second = runtime.router.resolve_default()
    assert first is not second
    asyncio.run(manager.router.aclose())
    assert first in closed and second in closed


def test_runtime_test_override_remains_explicitly_authoritative(api_client):
    sid = session(api_client)
    runtime = api_client.app.state.sessions.get_runtime(sid, api_client.app.state.local_user.user_id)
    override = object()
    runtime.router.provider_override = override
    assert runtime.router.resolve_default() is override
    assert api_client.app.state.router.provider_override is override


def test_captured_profile_limits_survive_reset_to_offline_and_another_selection(api_client, monkeypatch):
    class P:
        model = "alpha"
        context_window = 65000
        max_output_tokens = 8000
    monkeypatch.setattr("code_agent.agent.router.create_provider", lambda *a, **k: P())
    p = profile(context_window=65000, max_output_tokens=8000)
    sid = session(api_client)
    choose(api_client, sid, p["id"], "alpha")
    manager = api_client.app.state.sessions
    runtime = manager.get_runtime(sid, api_client.app.state.local_user.user_id)
    captured = runtime.router.resolve_default()
    before = runtime.router.model_capacity(captured)
    settings.save({"provider": "offline", "context_window": 4096, "max_output_tokens": 1024})
    manager.router.reload()
    choose(api_client, sid, "", "")
    assert runtime.router.model_capacity(captured) == before
    choose(api_client, sid, p["id"], "beta")
    assert runtime.router.model_capacity(captured) == before


def test_context_estimate_uses_session_profile_instead_of_global_config(api_client, monkeypatch):
    class P:
        model = "beta"
        context_window = 65000
        max_output_tokens = 8000
    monkeypatch.setattr("code_agent.agent.router.create_provider", lambda *a, **k: P())
    p = profile(context_window=65000, max_output_tokens=8000)
    sid = session(api_client)
    choose(api_client, sid, p["id"], "beta")
    state = api_client.get(f"/api/v1/sessions/{sid}/context")
    assert state.status_code == 200, state.text
    assert state.json()["model"] == "beta"
    assert state.json()["window"] == 65000
    assert state.json()["max_output_tokens"] == 8000
