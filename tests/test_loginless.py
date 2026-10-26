"""本地免登录：默认访问、旧身份复用、WebSocket，以及可选 JWT 模式。"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from code_agent.api.app import create_app
from code_agent.api.security import hash_password
from code_agent.config import auth_enabled
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.db import Database, UserRepo


def test_auth_defaults_to_local_mode(monkeypatch):
    monkeypatch.delenv("CODE_AGENT_AUTH_ENABLED", raising=False)
    assert auth_enabled() is False
    for value in ("true", "1", "YES", "on"):
        monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", value)
        assert auth_enabled() is True
    for value in ("false", "0", "NO", "off", ""):
        monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", value)
        assert auth_enabled() is False
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "treu")
    with pytest.raises(ValueError, match="CODE_AGENT_AUTH_ENABLED"):
        auth_enabled()


@pytest.fixture
def local_client(agent_home, monkeypatch):
    monkeypatch.delenv("CODE_AGENT_AUTH_ENABLED", raising=False)
    with TestClient(create_app(provider_name="offline")) as client:
        yield client


def test_local_http_access_and_stale_token(local_client):
    identity = local_client.get("/api/v1/auth/me")
    assert identity.status_code == 200
    assert identity.json()["role"] == "admin"
    # 浏览器遗留的失效 token 不应重新挡住本地工作台。
    assert local_client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer expired-token"}
    ).json() == identity.json()
    session = local_client.post("/api/v1/sessions", json={})
    assert session.status_code == 200
    sid = session.json()["id"]
    assert any(row["id"] == sid for row in local_client.get("/api/v1/sessions").json())
    assert local_client.get("/api/v1/admin/audit").status_code == 200
    assert local_client.get("/api/v1/tools").status_code == 200


@pytest.mark.parametrize("suffix", ["", "?token=expired-token"])
def test_local_websocket_access(local_client, suffix):
    sid = local_client.post("/api/v1/sessions", json={}).json()["id"]
    with local_client.websocket_connect(f"/api/v1/sessions/{sid}/stream{suffix}") as ws:
        meta = ws.receive_json()
        assert meta["type"] == "session_meta"
        assert meta["payload"]["busy"] is False


def test_local_websocket_metadata_restores_running_state(local_client):
    sid = local_client.post("/api/v1/sessions", json={}).json()["id"]
    user_id = local_client.get("/api/v1/auth/me").json()["id"]
    runtime = local_client.app.state.sessions.get_runtime(sid, user_id)
    runtime.router.provider_override = OfflineProvider(script=[
        OfflineProvider.tool_call_turn("bash", {"command": "make all"}),
        OfflineProvider.text_turn("finished"),
    ])
    with local_client.websocket_connect(f"/api/v1/sessions/{sid}/stream") as ws:
        ws.receive_json()
        ws.send_json({"type": "prompt", "text": "request approval"})
        while ws.receive_json()["type"] != "approval_request":
            pass
        try:
            with local_client.websocket_connect(f"/api/v1/sessions/{sid}/stream") as reconnect:
                while True:
                    meta = reconnect.receive_json()
                    if meta["type"] == "session_meta":
                        assert meta["payload"]["busy"] is True
                        break
        finally:
            ws.send_json({"type": "cancel"})


def test_local_identity_preserves_existing_admin_sessions(agent_home, monkeypatch):
    # 先以认证模式创建旧用户数据，再切换免登录，默认用户名与旧用户名不同。
    monkeypatch.setenv("CODE_AGENT_ADMIN_USER", "existing-admin")
    with TestClient(create_app(provider_name="offline")) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "existing-admin", "password": "admin123"}
        ).json()
        headers = {"Authorization": f"Bearer {login['token']}"}
        sid = client.post("/api/v1/sessions", json={}, headers=headers).json()["id"]
        original_id = login["user"]["id"]
    monkeypatch.delenv("CODE_AGENT_ADMIN_USER")
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    for _ in range(2):
        with TestClient(create_app(provider_name="offline")) as client:
            assert client.get("/api/v1/auth/me").json()["id"] == original_id
            assert client.get(f"/api/v1/sessions/{sid}").status_code == 200
            assert client.app.state.users.count() == 1


def test_local_mode_creates_reusable_admin_when_import_has_only_users(agent_home, monkeypatch):
    db = Database(agent_home / "code_agent.db")
    UserRepo(db).create("admin", hash_password("old-password"), role="user")
    db.close()
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "false")
    identities = []
    for _ in range(2):
        with TestClient(create_app(provider_name="offline")) as client:
            user = client.get("/api/v1/auth/me").json()
            assert user["role"] == "admin"
            assert client.app.state.users.count() == 2
            identities.append(user["id"])
    assert identities[0] == identities[1]


def test_opt_in_auth_still_requires_http_and_websocket_token(agent_home):
    with TestClient(create_app(provider_name="offline")) as client:
        assert client.get("/api/v1/auth/me").status_code == 401
        token = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        sid = client.post("/api/v1/sessions", json={}, headers=headers).json()["id"]
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/api/v1/sessions/{sid}/stream"):
                pass
        assert exc.value.code == 4401
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
