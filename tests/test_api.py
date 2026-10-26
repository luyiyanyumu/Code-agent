"""API 集成测试：登录 → 会话 → REST prompt → 轨迹 → 审批 → 关闭。

全程 offline provider，无 key。
"""

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app


@pytest.fixture
def client(agent_home):
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_and_me(client):
    token = _login(client)
    r = client.get("/api/v1/auth/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["username"] == "admin"
    assert r.json()["role"] == "admin"


def test_login_wrong_password(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_me_unauthorized(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_full_session_flow(client):
    token = _login(client)
    h = _auth(token)

    # 创建会话（M40：会话不带模型参数——用什么模型由当前生效的配置档案决定）
    r = client.post("/api/v1/sessions", json={}, headers=h)
    assert r.status_code == 200
    sid = r.json()["id"]
    assert "model" not in r.json()

    # REST prompt：offline provider 默认触发 bash echo（白名单放行）
    r = client.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "跑个 echo"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stop_reason"] == "end_turn"
    assert "hello-from-code-agent" in body["text"]

    # 轨迹可查：user_message + tool_use + tool_result + done
    events = client.get(f"/api/v1/sessions/{sid}/events", headers=h).json()
    types = [e["type"] for e in events]
    assert "user_message" in types
    assert "tool_use" in types
    assert "tool_result" in types
    assert "done" in types

    # after_seq 断点续拉
    tail = client.get(
        f"/api/v1/sessions/{sid}/events", params={"after_seq": 3}, headers=h
    ).json()
    assert all(e["seq"] > 3 for e in tail)

    # 会话列表与关闭
    rows = client.get("/api/v1/sessions", headers=h).json()
    assert any(s["id"] == sid for s in rows)
    assert client.post(f"/api/v1/sessions/{sid}/close", headers=h).status_code == 200
    # 关闭后再提交 -> 409
    r = client.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "x"}, headers=h)
    assert r.status_code == 409


def test_session_ownership(client):
    token = _login(client)
    h = _auth(token)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    # 第二个用户
    from code_agent.api.security import hash_password

    client.app.state.users.create("mallory", hash_password("pw123456"))
    token2 = _login(client, "mallory", "pw123456")
    h2 = _auth(token2)

    assert client.get(f"/api/v1/sessions/{sid}", headers=h2).status_code == 404
    assert client.get(f"/api/v1/sessions/{sid}", headers=h).status_code == 200


def test_approval_decide_unknown_id_404(client):
    token = _login(client)
    h = _auth(token)
    r = client.post("/api/v1/approvals/ap_nonexist/decide", json={"approved": True}, headers=h)
    assert r.status_code == 404
