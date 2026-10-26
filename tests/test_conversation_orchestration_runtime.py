"""Conversation-scoped background agents through real local APIs and runtimes."""

from __future__ import annotations

from collections import defaultdict

import pytest

from code_agent.api.security import hash_password
from code_agent.codeintel.work_items import project_work_items
from code_agent.sessions.manager import SessionRuntime, TEAM_TOOL_NAMES
from test_project_team_integration import integration, roster


def chat(env, title="独立对话"):
    client, _, auth, project, _ = env
    response = client.post(f"/api/v1/code/projects/{project['id']}/conversations", headers=auth, json={"title": title})
    assert response.status_code == 200, response.text
    return response.json()


def child(env, parent_id, title="后台任务"):
    _, app, _, project, _ = env
    team = app.state.project_agent_team
    return team._create_worker(project, team._member(parent_id, project["user_id"]),
                               tree="main", role="subagent", title=title)


def test_chat_runtime_gains_background_tools_without_moving_project_default_or_creating_sop_branches(integration):
    _, app, _, project, workspace = integration
    canonical = roster(integration)["lead"]["session_id"]
    first, second = chat(integration), chat(integration)
    manager = app.state.sessions
    assert app.state.projects.get(project["id"])["session_id"] == canonical
    for current in [first, second]:
        runtime = manager.get_runtime(current["id"], project["user_id"])
        assert runtime.agent_team is app.state.project_agent_team
        assert runtime.workspace == workspace
        assert TEAM_TOOL_NAMES <= set(runtime._loop_context()[1].names())
        member = app.state.project_agent_team._member(current["id"], project["user_id"])
        assert member["role"] == "lead" and member["lead_session_id"] == current["id"]
        assert not member["parent_session_id"]
    assert app.state.db.query_all("SELECT * FROM project_agent_runs") == []
    assert app.state.db.query_all("SELECT * FROM project_agents WHERE role='node'") == []


def test_roster_and_wait_are_scoped_to_selected_conversation_with_legacy_target_compatibility(integration):
    client, app, auth, project, _ = integration
    first, second = chat(integration), chat(integration)
    one, two = child(integration, first["id"]), child(integration, second["id"])
    prefix = f"/api/v1/code/projects/{project['id']}/agents"
    for current, expected in [(first, one), (second, two)]:
        response = client.get(prefix, headers=auth, params={"conversation_id": current["id"]})
        assert response.status_code == 200, response.text
        assert response.json()["lead"]["session_id"] == current["id"]
        assert {agent["session_id"] for agent in response.json()["agents"]} == {expected["session_id"]}
    body = {"conversation_id": first["id"], "session_ids": [one["session_id"]], "timeout_seconds": 0.0}
    response = client.post(prefix + "/wait", headers=auth, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["agents"][0]["session_id"] == one["session_id"]
    body["session_ids"] = [two["session_id"]]
    assert client.post(prefix + "/wait", headers=auth, json=body).status_code == 404
    body.pop("conversation_id")
    assert client.post(prefix + "/wait", headers=auth, json=body).status_code == 200
    body["session_ids"] = [one["session_id"], two["session_id"]]
    assert client.post(prefix + "/wait", headers=auth, json=body).status_code == 404
    response = client.post(prefix + f"/{one['session_id']}/message", headers=auth,
                           json={"conversation_id": first["id"], "message": "补充当前任务的输入"})
    assert response.status_code == 200, response.text
    assert client.post(prefix + f"/{one['session_id']}/stop", headers=auth,
                       json={"conversation_id": first["id"]}).status_code == 200
    assert client.post(prefix + f"/{two['session_id']}/stop", headers=auth).status_code == 200


@pytest.mark.parametrize("action,body", [
    ("stop", {}), ("interrupt", {}), ("message", {"message": "仅测试消息"}),
    ("followup", {"prompt": "仅测试继续任务"}),
    ("audit", {"activation_id": 1, "decision": "accepted", "reason": "仅测试范围"}),
])
def test_mutations_cannot_use_target_id_to_escape_the_current_conversation(integration, action, body):
    client, app, auth, project, _ = integration
    first, second = chat(integration), chat(integration)
    target = child(integration, second["id"])
    before = app.state.project_agent_team._row(target["session_id"])
    path = f"/api/v1/code/projects/{project['id']}/agents/{target['session_id']}/{action}"
    response = client.post(path, headers=auth, json={**body, "conversation_id": first["id"]})
    assert response.status_code == 404, response.text
    assert app.state.project_agent_team._row(target["session_id"]) == before
    assert app.state.db.query_all("SELECT * FROM project_agent_runs") == []


def test_explicit_roster_rejects_other_owner_worker_and_archived_roots(integration):
    client, app, auth, project, workspace = integration
    current = chat(integration)
    worker = child(integration, current["id"])
    archived = chat(integration)
    app.state.sessions.archive(archived["id"], project["user_id"])
    other = app.state.users.create("orchestration-other", hash_password("test-only"), role="user")
    foreign_project = app.state.projects.create(other["id"], "foreign", str(workspace), "foreign-key")
    foreign = app.state.sessions.session_repo.create(other["id"], project_id=foreign_project["id"], kind="chat", tree_id="main")
    path = f"/api/v1/code/projects/{project['id']}/agents"
    for session_id in [worker["session_id"], archived["id"], foreign["id"], "s_missing"]:
        response = client.get(path, headers=auth, params={"conversation_id": session_id})
        assert response.status_code in (404, 409), response.text
    assert app.state.project_agent_team._row(foreign["id"]) is None


def test_team_invalidations_only_reach_the_same_conversation_and_include_root_identity(integration, monkeypatch):
    _, app, _, project, _ = integration
    canonical = roster(integration)["lead"]["session_id"]
    first, second = chat(integration), chat(integration)
    one, two = child(integration, first["id"]), child(integration, second["id"])
    records = defaultdict(list)
    manager = app.state.sessions
    for session_id in [canonical, first["id"], second["id"], one["session_id"], two["session_id"]]:
        runtime = manager.get_runtime(session_id, project["user_id"])
        monkeypatch.setattr(runtime, "broadcast", lambda event, sid=session_id: records[sid].append(event))
    records.clear()
    manager.notify_team_changed(one["session_id"])
    assert set(records) == {first["id"], one["session_id"]}
    for events in records.values():
        note = events[0].payload["team_changed"]
        assert note["lead_session_id"] == first["id"] and note["session_id"] == one["session_id"]


def test_budget_sharing_requires_both_the_same_root_and_a_real_ancestor(integration, monkeypatch):
    _, app, _, project, _ = integration
    first, second = chat(integration), chat(integration)
    one = child(integration, first["id"])
    grandchild = child(integration, one["session_id"])
    sibling = child(integration, first["id"])
    foreign_child = child(integration, second["id"])
    manager, team = app.state.sessions, app.state.project_agent_team
    ids = [first["id"], second["id"], one["session_id"], grandchild["session_id"], sibling["session_id"], foreign_child["session_id"]]
    runtimes = {sid: manager.get_runtime(sid, project["user_id"]) for sid in ids}
    for sid in ids:
        app.state.db.execute("UPDATE project_agents SET activation_id=1 WHERE session_id=?", (sid,))
    busy = {first["id"], one["session_id"]}
    monkeypatch.setattr(SessionRuntime, "busy", property(lambda runtime: runtime.session_id in busy))
    try:
        runtimes[first["id"]].set_execution_budget(None, activation_id=1)
        scope = runtimes[first["id"]]._execution_budget.current["scope_id"]
        runtimes[one["session_id"]].set_execution_budget(None, activation_id=1, parent_session_id=first["id"])
        runtimes[grandchild["session_id"]].set_execution_budget(None, activation_id=1, parent_session_id=one["session_id"])
        assert runtimes[grandchild["session_id"]]._execution_budget.current["scope_id"] == scope
        for sid, parent in [(sibling["session_id"], one["session_id"]),
                            (foreign_child["session_id"], first["id"]), (second["id"], first["id"])]:
            runtimes[sid].set_execution_budget(None, activation_id=1, parent_session_id=parent)
            assert runtimes[sid]._execution_budget.current["scope_id"] != scope
        assert team._row(one["session_id"])["parent_session_id"] == first["id"]
    finally:
        busy.clear()


def test_project_work_items_uses_the_canonical_lead_even_when_a_chat_is_newer(integration):
    _, app, _, project, _ = integration
    canonical = roster(integration)["lead"]["session_id"]
    current = chat(integration)
    assert current["id"] != canonical
    project = app.state.projects.get(project["id"])
    result = project_work_items(project=project, manager=app.state.sessions,
                                tasks=None, batches=None, batch_service=None, task_view=lambda row: row)
    assert result["lead"]["session_id"] == canonical
