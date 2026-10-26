"""设置（M13）测试：config.json 读写、模型配置优先级、热生效、API 权限。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.agent.router import ModelRouter
from code_agent.api.app import create_app
from code_agent.api.security import hash_password


@pytest.fixture
def no_model_env(monkeypatch):
    """清掉模型相关环境变量，保证 config.json 优先级可测。"""
    for key in (
        "CODE_AGENT_PROVIDER",
        "CODE_AGENT_MODEL_DEFAULT",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_settings_save_and_model_config(agent_home, no_model_env):
    settings.save(
        {
            "provider": "openai_compat",
            "api_key": "sk-test-1234567890",
            "base_url": "http://gw/v1",
            "model_default": "qwen",
        }
    )
    cfg = settings.model_config()
    assert cfg["provider"] == "openai_compat"
    assert cfg["api_key"] == "sk-test-1234567890"
    assert cfg["base_url"] == "http://gw/v1"
    assert cfg["model_default"] == "qwen"


def test_settings_env_fallback(agent_home, no_model_env, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_PROVIDER", "offline")
    assert settings.model_config()["provider"] == "offline"  # 未写 config.json -> env 兜底


def test_settings_save_clears_empty(agent_home, no_model_env):
    settings.save({"provider": "deepseek", "base_url": "http://x"})
    assert settings.model_config()["base_url"] == "http://x"
    settings.save({"base_url": ""})  # 空值 = 清除
    assert settings.model_config()["base_url"] == ""


def test_mask_key():
    assert settings.mask_key("sk-abcdefghijk") == "sk-******hijk"
    assert settings.mask_key("") == ""
    assert settings.mask_key("abc") == "ab*"


def test_router_reads_config(agent_home, no_model_env):
    settings.save(
        {
            "provider": "openai_compat",
            "model_default": "qwen2.5",
            "base_url": "http://gw/v1",
            "api_key": "k",
        }
    )
    r = ModelRouter()
    assert r.provider_name == "openai_compat"
    # M36：只有默认模型（原来是三档 model_lite/default/craft）
    assert r.model_name() == "qwen2.5"
    assert r.default_model() == "qwen2.5"


def test_router_reload(agent_home, no_model_env):
    settings.save({"provider": "offline"})
    r = ModelRouter()
    assert r.provider_name == "offline"
    settings.save({"provider": "deepseek"})
    r.reload()
    assert r.provider_name == "deepseek"


def test_router_rejects_unknown_provider(agent_home, no_model_env):
    settings.save({"provider": "not-a-provider"})
    with pytest.raises(ValueError):
        ModelRouter()


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@pytest.fixture
def client(agent_home, no_model_env):
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_settings_api_get_and_put(client):
    token = _login(client)
    h = _auth(token)

    cfg = client.get("/api/v1/settings/model", headers=h).json()
    assert cfg["provider"] == "offline"
    assert cfg["api_key_set"] is False

    r = client.put(
        "/api/v1/settings/model",
        json={
            "provider": "openai_compat",
            "base_url": "http://gw/v1",
            "api_key": "sk-secret-123456",
            "model_default": "qwen",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "openai_compat"
    assert body["api_key_set"] is True
    assert "sk-secret-123456" not in str(body)  # 打码，不回传完整 key
    assert "******" in body["api_key_hint"]

    cfg2 = client.get("/api/v1/settings/model", headers=h).json()
    assert cfg2["api_key_set"] is True
    assert "sk-secret-123456" not in str(cfg2)


def test_settings_api_put_requires_admin(client):
    token = _login(client)
    h = _auth(token)
    client.app.state.users.create("bob", hash_password("pw123456"))
    token2 = _login(client, "bob", "pw123456")
    h2 = _auth(token2)

    assert (
        client.put("/api/v1/settings/model", json={"provider": "offline"}, headers=h2).status_code
        == 403
    )
    assert client.get("/api/v1/settings/model", headers=h2).status_code == 200


def test_settings_api_put_invalid_provider(client):
    token = _login(client)
    h = _auth(token)
    r = client.put("/api/v1/settings/model", json={"provider": "bogus"}, headers=h)
    assert r.status_code == 422
