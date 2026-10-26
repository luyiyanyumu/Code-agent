"""会话删除（M16）：删会话时交付物与会话工作区一并移入 .trash，不留孤儿文件。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app


@pytest.fixture
def client(agent_home):
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _auth(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_delete_session_trashes_artifacts_and_workspace(client, agent_home):
    h = _auth(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    # 造出该会话的交付物与会话工作区文件
    art = agent_home / "artifacts" / sid
    art.mkdir(parents=True, exist_ok=True)
    (art / "report.md").write_text("交付内容", encoding="utf-8")
    ws = agent_home / "workspaces" / sid
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "note.txt").write_text("工作区文件", encoding="utf-8")

    r = client.delete(f"/api/v1/sessions/{sid}", headers=h)
    assert r.status_code == 200, r.text

    # 原目录不再存在（没有孤儿残留）
    assert not art.exists(), "artifacts 目录应被移走"
    assert not ws.exists(), "工作区目录应被移走"

    # 都进了各自的 .trash 回收站（可人工找回）
    art_trash = list((agent_home / "artifacts" / ".trash").iterdir())
    ws_trash = list((agent_home / "workspaces" / ".trash").iterdir())
    assert any(sid in p.name for p in art_trash)
    assert any(sid in p.name for p in ws_trash)


def test_delete_session_without_side_dirs_is_safe(client, agent_home):
    """没有交付物/工作区时删会话不应报错（路径不存在要静默跳过）。"""
    h = _auth(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    assert client.delete(f"/api/v1/sessions/{sid}", headers=h).status_code == 200


def test_delete_unknown_session_404(client):
    h = _auth(client)
    assert client.delete("/api/v1/sessions/s_nope", headers=h).status_code == 404
