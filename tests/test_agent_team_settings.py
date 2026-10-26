"""Hierarchy limit persistence and authenticated API use isolated config files."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.api.agent_settings_api import mount_agent_settings_api
from code_agent.api.deps import CurrentUser, get_current_user


def test_defaults_do_not_create_config(agent_home):
    first = settings.agent_team_config()
    assert first == {"max_depth": 3, "max_parallel": 4}
    first["max_depth"] = 1
    assert settings.agent_team_config()["max_depth"] == 3
    assert not settings.settings_file().exists()


def test_limits_roundtrip_and_partial_edit_preserve_other_settings(agent_home):
    settings.save({"provider": "offline", "api_key": "fixture-only-secret", "other": {"keep": True}})
    assert settings.save_agent_team_config({"max_depth": 8, "max_parallel": 16}) == {
        "max_depth": 8, "max_parallel": 16,
    }
    assert settings.save_agent_team_config({"max_depth": 1}) == {"max_depth": 1, "max_parallel": 16}
    assert settings.agent_team_config() == {"max_depth": 1, "max_parallel": 16}
    stored = settings.load()
    assert stored["provider"] == "offline"
    assert stored["api_key"] == "fixture-only-secret"
    assert stored["other"] == {"keep": True}
    assert stored["agent_team"] == {"max_depth": 1, "max_parallel": 16}


@pytest.mark.parametrize("field, invalid", [
    (field, value)
    for field in ("max_depth", "max_parallel")
    for value in (True, False, 0, -1, 1.5, 3.0, "3", None, [], {}, 17)
] + [("max_depth", 9)])
def test_invalid_limits_do_not_modify_file(agent_home, field, invalid):
    settings.save({"other": "keep"})
    before = settings.settings_file().read_bytes()
    with pytest.raises(ValueError, match="整数"):
        settings.save_agent_team_config({field: invalid})
    assert settings.settings_file().read_bytes() == before


@pytest.mark.parametrize("body", [[], None, "settings", {"max_children": 4}, {"max_depth": 2, "provider": "offline"}])
def test_unknown_fields_and_nonobjects_are_rejected_before_write(agent_home, body):
    with pytest.raises(ValueError):
        settings.save_agent_team_config(body)
    assert not settings.settings_file().exists()


@pytest.mark.parametrize("raw, expected", [
    (None, {"max_depth": 3, "max_parallel": 4}),
    ([1, 2], {"max_depth": 3, "max_parallel": 4}),
    ({"max_depth": True, "max_parallel": 2}, {"max_depth": 3, "max_parallel": 2}),
    ({"max_depth": 4, "max_parallel": "8"}, {"max_depth": 4, "max_parallel": 4}),
    ({"max_depth": 0, "max_parallel": 17}, {"max_depth": 3, "max_parallel": 4}),
    ({"max_depth": 8, "max_parallel": 16, "junk": True}, {"max_depth": 8, "max_parallel": 16}),
])
def test_defensive_read_uses_field_defaults_without_rewriting(agent_home, raw, expected):
    settings.settings_file().write_text(json.dumps({"agent_team": raw}), encoding="utf-8")
    before = settings.settings_file().read_bytes()
    assert settings.agent_team_config() == expected
    assert settings.settings_file().read_bytes() == before


@pytest.fixture
def client(agent_home):
    app = FastAPI()
    app.state.auth_enabled = True
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("owner", "owner", "admin")
    mount_agent_settings_api(app)
    with TestClient(app) as client:
        yield client, app


def test_api_reads_and_updates_bounded_settings_without_secret_exposure(client):
    http, _ = client
    settings.save({"api_key": "fixture-only-secret"})
    assert http.get("/api/v1/settings/agent-team").json() == {"max_depth": 3, "max_parallel": 4}
    response = http.put("/api/v1/settings/agent-team", json={"max_depth": 2, "max_parallel": 7})
    assert response.status_code == 200
    assert response.json() == {"max_depth": 2, "max_parallel": 7}
    assert http.put("/api/v1/settings/agent-team", json={"max_depth": 1}).json()["max_parallel"] == 7
    assert "fixture-only-secret" not in response.text


@pytest.mark.parametrize("body", [
    {"max_depth": True}, {"max_parallel": 2.0}, {"max_depth": 9},
    {"max_parallel": 17}, {"model_default": "ignored"}, [], None,
])
def test_api_invalid_update_returns_422_without_mutation(client, body):
    http, _ = client
    response = http.put("/api/v1/settings/agent-team", json=body)
    assert response.status_code == 422
    assert not settings.settings_file().exists()


def test_api_reads_require_auth_and_writes_require_admin(client):
    http, app = client
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("member", "member", "user")
    assert http.get("/api/v1/settings/agent-team").status_code == 200
    assert http.put("/api/v1/settings/agent-team", json={"max_depth": 2}).status_code == 403
    app.dependency_overrides.clear()
    assert http.get("/api/v1/settings/agent-team").status_code == 401
    assert not settings.settings_file().exists()


def test_api_disk_failure_returns_actionable_error(client, monkeypatch):
    http, _ = client
    def fail(_):
        raise OSError("fixture disk failure")
    monkeypatch.setattr(settings, "save_agent_team_config", fail)
    response = http.put("/api/v1/settings/agent-team", json={"max_parallel": 3})
    assert response.status_code == 500
    assert response.json()["detail"] == "智能体协作设置保存失败，请重试"
