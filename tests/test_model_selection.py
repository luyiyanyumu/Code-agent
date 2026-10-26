"""M36：模型目录（每条配置档案自己的模型清单）与"端点全部可用模型"。

背景：档位（lite/default/craft）已去掉；模型由"当前生效的配置档案"决定。
档案可以带一份自己的模型清单（探测端点拉回来、挑几个留着），用户从里面选一个
作为该档案的默认模型——所以模型名必须能按名字解析成 provider（见下面路由那节）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.api.app import create_app
from code_agent.agent.router import ModelRouter


@pytest.fixture()
def client(agent_home, monkeypatch) -> TestClient:
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        c.app = app
        yield c


def _login(client: TestClient) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}

# ----------------------------------------------------------------------
# 路由：默认模型 + 按具体模型名取 provider
# ----------------------------------------------------------------------
def test_router_resolves_a_concrete_model_name(agent_home, monkeypatch) -> None:
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    router = ModelRouter(provider_name="openai_compat")

    monkeypatch.setattr(
        "code_agent.agent.router.model_config",
        lambda: {"provider": "openai_compat", "base_url": "http://127.0.0.1:1/v1",
                 "api_key": "k", "model_default": "def-x"},
    )
    provider = router.resolve_name("Doubao-Seedream-5.0-lite")
    assert getattr(provider, "model", "") == "Doubao-Seedream-5.0-lite"
    # 具体模型与默认模型分开缓存，互不覆盖
    assert router.resolve_name("Doubao-Seedream-5.0-lite") is provider
    assert router.resolve_default() is not provider
    assert router.model_name() == "def-x"
    assert router.default_model() == "def-x"
    with pytest.raises(ValueError):
        router.resolve_name("   ")


# ----------------------------------------------------------------------
# 每条档案自己的模型清单（M40：模型目录从全局搬到档案下面）
# ----------------------------------------------------------------------
def _make_profile(client, h, **over) -> dict:
    body = {"name": "测试档案", "provider": "openai_compat",
            "base_url": "https://gw.test/v1", "model_default": "glm-4.7-flash"}
    body.update(over)
    r = client.post("/api/v1/model-profiles", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_profile_starts_with_an_empty_model_list(client) -> None:
    h = _login(client)
    prof = _make_profile(client, h)
    assert prof["models"] == [], "还没探测过就该是空清单（只有默认模型）"


def test_save_and_reset_profile_models(client) -> None:
    h = _login(client)
    prof = _make_profile(client, h)
    pid = prof["id"]
    items = [
        {"name": "GLM 快速版", "id": "glm-4.7-flash"},
        {"name": "MiniMax", "id": "MiniMax-M3"},
        {"name": "重复项应被去掉", "id": "MiniMax-M3"},
        {"name": "", "id": ""},          # 空项应被丢掉
        "ernie-5.1",                      # 纯字符串也接受
    ]
    saved = client.put(f"/api/v1/model-profiles/{pid}/models", json={"items": items}, headers=h).json()
    assert [i["id"] for i in saved["items"]] == ["glm-4.7-flash", "MiniMax-M3", "ernie-5.1"]
    assert saved["items"][0]["name"] == "GLM 快速版"

    # 清单跟着档案一起返回（设置页展开档案时直接读它）
    listed = {p["id"]: p for p in client.get("/api/v1/model-profiles", headers=h).json()}
    assert [i["id"] for i in listed[pid]["models"]] == ["glm-4.7-flash", "MiniMax-M3", "ernie-5.1"]

    reset = client.post(f"/api/v1/model-profiles/{pid}/models/reset", headers=h).json()
    assert reset["items"] == [], "恢复默认模型 = 清掉这份自定义清单"


def test_profile_models_are_per_profile(client) -> None:
    """两条档案各有各的清单，互不影响。"""
    h = _login(client)
    a = _make_profile(client, h, name="甲")
    b = _make_profile(client, h, name="乙")
    client.put(f"/api/v1/model-profiles/{a['id']}/models",
               json={"items": [{"id": "only-in-a"}]}, headers=h)
    profs = {p["name"]: p for p in client.get("/api/v1/model-profiles", headers=h).json()}
    assert [i["id"] for i in profs["甲"]["models"]] == ["only-in-a"]
    assert profs["乙"]["models"] == []


def test_editing_a_profile_keeps_its_models(client) -> None:
    """编辑档案（改地址/密钥）不该顺手把模型清单清空。"""
    h = _login(client)
    prof = _make_profile(client, h)
    client.put(f"/api/v1/model-profiles/{prof['id']}/models",
               json={"items": [{"id": "keep-me"}]}, headers=h)
    edited = client.put(f"/api/v1/model-profiles/{prof['id']}",
                        json={"name": "改过名", "provider": "openai_compat",
                              "base_url": "https://gw2.test/v1", "model_default": "keep-me"},
                        headers=h).json()
    assert edited["name"] == "改过名"
    assert [i["id"] for i in edited["models"]] == ["keep-me"]


def test_unknown_profile_is_404(client) -> None:
    h = _login(client)
    assert client.put("/api/v1/model-profiles/mp_nope/models",
                      json={"items": []}, headers=h).status_code == 404
    assert client.post("/api/v1/model-profiles/mp_nope/models/reset", headers=h).status_code == 404


def test_sanitize_is_bounded_and_forgiving() -> None:
    # 上限 200 条
    many = [{"name": f"n{i}", "id": f"m{i}"} for i in range(300)]
    assert len(settings.sanitize_models(many)) == settings.MAX_CATALOG
    # 非列表 → 空（不抛）
    assert settings.sanitize_models(None) == []
    assert settings.sanitize_models({"a": 1}) == []
    # 名字缺失时用 id 兜底，长度各自受限（名字 80 / id 120）
    got = settings.sanitize_models([{"id": "x" * 500}])
    assert got[0]["name"] == "x" * 80 and len(got[0]["id"]) == 120


def test_legacy_global_catalog_is_purged(agent_home, monkeypatch) -> None:
    """老 config.json 里的全局 model_catalog 会被清掉（它已经没有任何读取方）。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    settings.save({"provider": "openai_compat", "model_catalog": [{"name": "old", "id": "old"}]})
    assert "model_catalog" in settings.load()
    assert settings.legacy_model_catalog_purge() is True
    assert "model_catalog" not in settings.load()
    # 再调一次不报错、也不再"清掉东西"
    assert settings.legacy_model_catalog_purge() is False
