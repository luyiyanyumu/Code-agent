"""工具级审批策略（M15）测试：页面可改 + 持久化 + 热生效 + 权限校验。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.api.app import create_app
from code_agent.api.security import hash_password


@pytest.fixture
def client(agent_home):
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _auth(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _policy(client, h, name):
    cat = client.get("/api/v1/tools", headers=h).json()
    return next(t["policy"] for t in cat["tools"] if t["name"] == name)


def test_permissions_list(client):
    h = _auth(client)
    body = client.get("/api/v1/permissions", headers=h).json()
    assert body["can_edit"] is True
    assert [m["id"] for m in body["modes"]] == ["allow", "approval", "policy"]
    names = {t["name"] for t in body["tools"]}
    assert "bash" in names and "read_file" in names


def test_set_policy_allow(client, agent_home):
    h = _auth(client)
    # 样本换成 `skill_install`：它落的是 default（装软件是写操作，默认需人工审批）。
    # 这里原本拿 `save_memory` 当"默认需审批"的样本，但它现在有显式策略——
    # 写的是平台用户记忆目录与工作区 `memory/`，与 `todo_write` 同性质，
    # 默认不该打断人（自动的失败反思 `_remember_failure` 更是完全不经过权限层）。
    assert _policy(client, h, "skill_install") == "approval"
    r = client.put("/api/v1/permissions/skill_install", json={"mode": "allow"}, headers=h)
    assert r.status_code == 200, r.text
    assert _policy(client, h, "skill_install") == "allow"
    assert settings.tool_policies()["skill_install"] == "allow"  # 已持久化


def test_set_policy_approval_overrides_allow(client, agent_home):
    h = _auth(client)
    # kb_write 默认在基础放行名单里（免审批）
    assert _policy(client, h, "kb_write") == "allow"
    client.put("/api/v1/permissions/kb_write", json={"mode": "approval"}, headers=h)
    assert _policy(client, h, "kb_write") == "approval"  # 用户覆盖优先


def test_set_policy_back_to_builtin(client, agent_home):
    h = _auth(client)
    client.put("/api/v1/permissions/bash", json={"mode": "approval"}, headers=h)
    assert _policy(client, h, "bash") == "approval"
    # policy = 清除覆盖，回退内置（bash 内置是「按参数判定」）
    r = client.put("/api/v1/permissions/bash", json={"mode": "policy"}, headers=h)
    assert r.status_code == 200
    assert _policy(client, h, "bash") == "policy"
    assert "bash" not in settings.tool_policies()


def test_set_policy_requires_admin(client):
    h = _auth(client)
    client.app.state.users.create("bob", hash_password("pw123456"))
    bob = {
        "Authorization": f"Bearer {client.post('/api/v1/auth/login', json={'username': 'bob', 'password': 'pw123456'}).json()['token']}"
    }
    assert (
        client.put("/api/v1/permissions/bash", json={"mode": "allow"}, headers=bob).status_code
        == 403
    )
    # 普通用户仍可只读查看
    assert client.get("/api/v1/permissions", headers=bob).json()["can_edit"] is False


def test_set_policy_validation(client):
    h = _auth(client)
    assert (
        client.put("/api/v1/permissions/bash", json={"mode": "bogus"}, headers=h).status_code
        == 422
    )
    assert (
        client.put("/api/v1/permissions/nope_tool", json={"mode": "allow"}, headers=h).status_code
        == 404
    )


def test_tools_catalog_can_edit_flag(client):
    h = _auth(client)
    assert client.get("/api/v1/tools", headers=h).json()["can_edit"] is True
    client.app.state.users.create("carol", hash_password("pw123456"))
    carol = {
        "Authorization": f"Bearer {client.post('/api/v1/auth/login', json={'username': 'carol', 'password': 'pw123456'}).json()['token']}"
    }
    assert client.get("/api/v1/tools", headers=carol).json()["can_edit"] is False
