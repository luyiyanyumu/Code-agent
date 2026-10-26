"""Removed execution modes cannot be reactivated by old settings or clients."""

from __future__ import annotations

import json

import pytest

from code_agent import settings
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import Database, SessionRepo, UserRepo
from test_project_team_integration import integration, roster


def test_old_settings_are_inert_and_cleanup_preserves_other_options(agent_home):
    path = settings.settings_file()
    original = {"loop_mode": "cage", "permission_mode": "full-access", "provider": "offline",
                "agent_team": {"max_depth": 2, "max_parallel": 3}}
    path.write_text(json.dumps(original), encoding="utf-8-sig")
    assert "loop_mode" not in settings.load()
    assert settings.permission_mode() == "full-access"
    assert settings.legacy_model_catalog_purge() is True
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == {key: value for key, value in original.items() if key != "loop_mode"}
    assert settings.legacy_model_catalog_purge() is False
    with pytest.raises(ValueError, match="已移除"):
        settings.save({"loop_mode": "cage", "permission_mode": "read-only"})
    assert json.loads(path.read_text(encoding="utf-8")) == stored


def test_legacy_session_modes_are_normalized_without_deleting_sessions_or_reordering_history(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = Database(path)
    user = UserRepo(db).create("owner", "test-hash")
    repo = SessionRepo(db)
    session = repo.create(user["id"], title="retained", project_id="p_old", tree_id="main", kind="chat",
                          permission_mode="full-access", web_enabled=1)
    sid = session["id"]
    db.execute("UPDATE sessions SET loop_mode='cage' WHERE id=?", (sid,))
    before = db.query_one("SELECT * FROM sessions WHERE id=?", (sid,))
    assert "loop_mode" not in repo.get(sid), "legacy columns must not leak through metadata"
    db.close()

    db = Database(path)
    try:
        after = db.query_one("SELECT * FROM sessions WHERE id=?", (sid,))
        assert after == {**before, "loop_mode": "classic"}
        repo = SessionRepo(db)
        assert repo.get(sid) == session
        for rows in (repo.list_for_user(user["id"]), repo.list_by_project("p_old"),
                     repo.list_project_conversations(user["id"], "p_old", sid)):
            assert len(rows) == 1 and rows[0] == session
        assert not hasattr(repo, "set_loop_mode")
    finally:
        db.close()


def test_removed_routes_and_legacy_create_payload_cannot_enable_a_mode(integration):
    client, app, auth, project, _ = integration
    before = app.state.sessions.session_repo.list_for_user(project["user_id"])
    for value in ("cage", "classic", None):
        response = client.post("/api/v1/sessions", headers=auth, json={"loop_mode": value})
        assert response.status_code == 422 and "已移除" in response.json()["detail"]
    assert app.state.sessions.session_repo.list_for_user(project["user_id"]) == before
    sid = roster(integration)["lead"]["session_id"]
    for value in ("cage", "classic", None):
        response = client.post(f"/api/v1/code/projects/{project['id']}/session", headers=auth,
                               json={"loop_mode": value})
        assert response.status_code == 422 and "已移除" in response.json()["detail"]
    response = client.post(f"/api/v1/code/projects/{project['id']}/session", headers=auth, json={})
    assert response.status_code == 200 and response.json()["id"] == sid
    assert "loop_mode" not in response.json()
    paths = {route.path for route in app.routes}
    assert "/api/v1/settings/loop-mode" not in paths
    assert "/api/v1/sessions/{session_id}/loop-mode" not in paths
    assert client.get("/api/v1/settings/loop-mode", headers=auth).status_code == 404
    assert client.put("/api/v1/settings/loop-mode", headers=auth, json={"mode": "cage"}).status_code == 404
    assert client.post(f"/api/v1/sessions/{sid}/loop-mode", headers=auth, json={"mode": "cage"}).status_code == 404
    assert "loop_mode" not in client.get(f"/api/v1/sessions/{sid}", headers=auth).json()
    assert all("loop_mode" not in row for row in client.get("/api/v1/sessions", headers=auth).json())


def test_legacy_metadata_restores_only_the_ordinary_runtime_and_preserves_transcripts(integration):
    _, app, _, project, _ = integration
    manager = app.state.sessions
    sid, user_id = roster(integration)["lead"]["session_id"], project["user_id"]
    app.state.db.execute("UPDATE sessions SET loop_mode='cage' WHERE id=?", (sid,))
    manager.transcript.append(sid, "user_message", {"text": "keep this conversation"})
    events = manager.transcript.replay(sid)
    rebuilt = SessionManager(transcript=manager.transcript, session_repo=manager.session_repo,
                             approval_repo=manager.approval_repo, router=manager.router,
                             registry=manager.registry, project_repo=manager.project_repo,
                             code_intel=manager.code_intel, workspaces_root=manager.workspaces_root)
    runtime = rebuilt.get_runtime(sid, user_id)
    assert not hasattr(runtime, "loop_mode")
    assert manager.transcript.replay(sid) == events
    assert not hasattr(rebuilt, "set_loop_mode")
