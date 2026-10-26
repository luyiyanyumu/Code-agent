"""记忆板块（M15）测试：用户级 + 会话级记忆的读写删。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.memory.store import WorkspaceMemory


@pytest.fixture
def client(agent_home):
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _auth(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_memory_empty(client):
    h = _auth(client)
    body = client.get("/api/v1/memory", headers=h).json()
    assert body["user"]["entries"] == []
    assert body["user"]["records"] == []
    assert body["sessions"] == []
    assert [scope["id"] for scope in body["scopes"]] == ["user"]


def test_user_memory_add_list_delete(client):
    h = _auth(client)
    assert client.post("/api/v1/memory", json={"content": "偏好简洁回答"}, headers=h).status_code == 200
    client.post("/api/v1/memory", json={"content": "项目用 Python 3.13"}, headers=h)

    entries = client.get("/api/v1/memory", headers=h).json()["user"]["entries"]
    assert entries == ["偏好简洁回答", "项目用 Python 3.13"]

    # 删除第一条
    r = client.delete("/api/v1/memory?scope=user&index=0", headers=h)
    assert r.status_code == 200
    assert r.json()["entries"] == ["项目用 Python 3.13"]
    assert client.get("/api/v1/memory", headers=h).json()["user"]["entries"] == ["项目用 Python 3.13"]


def test_memory_validation(client):
    h = _auth(client)
    assert client.post("/api/v1/memory", json={"content": "   "}, headers=h).status_code == 422
    assert client.post("/api/v1/memory", json={"content": "x" * 1001}, headers=h).status_code == 422
    assert client.delete("/api/v1/memory?scope=user&index=5", headers=h).status_code == 404


def test_session_memory(client, agent_home):
    h = _auth(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    ws_root = client.app.state.sessions.workspaces_root
    WorkspaceMemory(ws_root / sid).save_fact("这个会话里的重要决定")

    body = client.get("/api/v1/memory", headers=h).json()
    assert len(body["sessions"]) == 1
    row = body["sessions"][0]
    assert row["session_id"] == sid
    assert row["entries"] == ["这个会话里的重要决定"]

    # 按会话删除
    r = client.delete(f"/api/v1/memory?scope=workspace&session_id={sid}&index=0", headers=h)
    assert r.status_code == 200
    assert client.get("/api/v1/memory", headers=h).json()["sessions"] == []


def test_memory_is_per_user(client, agent_home):
    h = _auth(client)
    client.post("/api/v1/memory", json={"content": "admin 的私有记忆"}, headers=h)

    from code_agent.api.security import hash_password

    client.app.state.users.create("bob", hash_password("pw123456"))
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": "pw123456"}
    ).json()["token"]
    bob = {"Authorization": f"Bearer {token}"}
    # bob 看不到 admin 的记忆，admin 自己的还在
    assert client.get("/api/v1/memory", headers=bob).json()["user"]["entries"] == []
    assert client.get("/api/v1/memory", headers=h).json()["user"]["entries"] == ["admin 的私有记忆"]


def test_memory_requires_auth(client):
    assert client.get("/api/v1/memory").status_code == 401
