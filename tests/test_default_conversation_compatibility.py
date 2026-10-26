"""Deleting the default conversation keeps existing project entry points usable."""

from __future__ import annotations

import asyncio

from code_agent.providers.offline import OfflineProvider
from code_agent.storage.db import TaskRepo
from test_project_team_integration import integration, roster


def promote_chat(env):
    client, app, auth, project, _ = env
    old = roster(env)["lead"]["session_id"]
    app.state.projects.set_flags(project["id"], allow_direct_write=True)
    app.state.sessions.set_permission_mode(old, project["user_id"], "workspace-write")
    app.state.sessions.set_web_enabled(old, project["user_id"], True)
    response = client.post(f"/api/v1/code/projects/{project['id']}/conversations", headers=auth,
                           json={"title": "正在使用的对话", "from_session_id": old})
    assert response.status_code == 200, response.text
    chat = response.json()
    runtime = app.state.sessions.get_runtime(chat["id"], project["user_id"])
    original = app.state.sessions.get_meta(chat["id"], project["user_id"])
    app.state.transcript.append(chat["id"], "user_message", {"text": "保留现有对话"})
    deleted = client.delete(f"/api/v1/code/projects/{project['id']}/conversations/{old}", headers=auth)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["default_session_id"] == chat["id"]
    return chat, runtime, original


def test_reopening_project_reuses_promoted_chat_without_reconfiguring_cached_runtime(integration, monkeypatch):
    client, app, auth, project, root = integration
    chat, runtime, original = promote_chat(integration)
    monkeypatch.setattr(app.state.router, "resolve_default", lambda: (_ for _ in ()).throw(AssertionError("Navigation must not call a model")))
    response = client.post(f"/api/v1/code/projects/{project['id']}/session", headers=auth, json={})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == chat["id"]
    assert app.state.sessions.get_meta(chat["id"], project["user_id"]) == original
    assert app.state.sessions.get_runtime(chat["id"], project["user_id"]) is runtime
    assert runtime._project_permission_provider() == "workspace-write"
    assert [event.payload["text"] for event in app.state.transcript.replay(chat["id"])] == ["保留现有对话"]
    assert (root / "source.txt").read_text() == "original"


def test_default_roster_workflow_and_work_items_follow_the_promoted_root(integration):
    client, app, auth, project, _ = integration
    chat, _, _ = promote_chat(integration)
    team = app.state.project_agent_team
    assert roster(integration)["lead"]["session_id"] == chat["id"]
    assert team._lead(project["id"], project["user_id"])["session_id"] == chat["id"]
    work = client.get(f"/api/v1/code/projects/{project['id']}/work-items", headers=auth)
    assert work.status_code == 200, work.text
    assert work.json()["lead"]["session_id"] == chat["id"]
    document = app.state.sop_workflow_store.get(project["id"], "main")
    revision = document.pop("revision")
    document.pop("updated_at")
    response = client.put(f"/api/v1/code/projects/{project['id']}/workflow", headers=auth,
                          json={"expected_revision": revision, "workflow": document})
    assert response.status_code == 200, response.text
    assert roster(integration)["agents"] == []
    next_chat = client.post(f"/api/v1/code/projects/{project['id']}/conversations", headers=auth, json={})
    assert next_chat.status_code == 200, next_chat.text
    assert next_chat.json()["permission_mode"] == chat["permission_mode"]
    assert next_chat.json()["web_enabled"] == chat["web_enabled"]
    assert app.state.projects.get(project["id"])["session_id"] == chat["id"]


def test_new_isolated_task_binds_to_promoted_default_without_reparenting_old_history(integration, tmp_path):
    _, app, _, project, _ = integration
    chat, _, _ = promote_chat(integration)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    task = TaskRepo(app.state.db).create(project_id=project["id"], user_id=project["user_id"],
        title="隔离任务", requirement="本地夹具", base_branch="main", branch="fixture", worktree=str(isolated))
    app.state.router.resolve_default = lambda: OfflineProvider(script=[OfflineProvider.text_turn("隔离读取完成")])
    asyncio.run(app.state.project_agent_team.run_isolated_task(project["id"], project["user_id"], task["id"],
        "完成离线读取", permission_mode="workspace-write"))
    session_id = TaskRepo(app.state.db).get(task["id"])["session_id"]
    member = app.state.project_agent_team.descriptor(session_id)
    assert member["parent_session_id"] == member["lead_session_id"] == chat["id"]
    assert member["tree_id"] == f"task:{task['id']}"
    assert app.state.sessions.get_runtime(session_id, project["user_id"]).workspace == isolated
    assert app.state.projects.get(project["id"])["session_id"] == chat["id"]
