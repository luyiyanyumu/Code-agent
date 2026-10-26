"""Deleting a default window changes a pointer, never another running chat."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from code_agent.sessions.manager import SessionClosed
from test_project_conversations import create_chat, endpoint
from test_project_team_integration import integration, roster


def test_delete_only_default_creates_an_empty_main_with_configured_options_and_no_model(integration, monkeypatch):
    client, app, auth, project, root = integration
    manager, team = app.state.sessions, app.state.project_agent_team
    owner = project["user_id"]
    original = roster(integration)["lead"]["session_id"]
    manager.set_permission_mode(original, owner, "full-access")
    manager.set_web_enabled(original, owner, True)
    child = team._create_worker(project, team._member(original, owner), tree="main", role="subagent", title="old child")
    ids = [original, child["session_id"]]
    approvals = {}
    for sid in ids:
        app.state.transcript.append(sid, "user_message", {"text": "retained history"})
        approvals[sid] = manager.approval_repo.create(sid, owner, "bash", "test", "test")
    before_files = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(app.state.router, "resolve_default", lambda: (_ for _ in ()).throw(AssertionError("Delete called a model")))
    response = client.delete(f"{endpoint(project)}/{original}", headers=auth)
    assert response.status_code == 200, response.text
    result = response.json()
    new_id = result["default_session_id"]
    assert new_id and new_id != original and result["fallback_session_id"] == new_id
    new_meta = result["default_conversation"]
    assert new_meta["id"] == new_id and new_meta["title"] == "新对话" and new_meta["kind"] == "main"
    assert (new_meta["permission_mode"], new_meta["web_enabled"]) == ("full-access", 1)
    assert "loop_mode" not in new_meta
    assert new_meta["busy"] is False and app.state.transcript.replay(new_id) == []
    assert new_id not in manager._runtimes, "replacement must be metadata-only until opened"
    assert team._subtree(new_id) == []
    for sid in ids:
        assert manager.get_meta(sid, owner)["status"] == "archived"
        assert team.descriptor(sid)["state"] == "retired"
        assert manager.approval_repo.get(approvals[sid])["status"] == "denied"
        assert app.state.transcript.replay(sid)[-1].payload["text"] == "retained history"
    assert team.descriptor(child["session_id"])["lead_session_id"] == original
    assert app.state.projects.get(project["id"])["session_id"] == new_id
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before_files
    assert client.delete(f"{endpoint(project)}/{original}", headers=auth).json() == result
    assert {row["id"] for row in client.get(endpoint(project), headers=auth).json()["conversations"]} == {new_id}
    with pytest.raises(SessionClosed):
        manager.get_runtime(original, owner)


def test_delete_default_promotes_a_busy_chat_without_changing_its_binding_or_descendants(integration, monkeypatch):
    client, app, auth, project, _ = integration
    manager, team = app.state.sessions, app.state.project_agent_team
    owner = project["user_id"]
    original = roster(integration)["lead"]["session_id"]
    remaining = create_chat(integration, title="running work")
    manager.set_permission_mode(remaining["id"], owner, "workspace-write")
    manager.set_web_enabled(remaining["id"], owner, True)
    runtime = manager.get_runtime(remaining["id"], owner)
    old_child = team._create_worker(project, team._member(original, owner), tree="main", role="subagent", title="deleted child")
    new_child = team._create_worker(project, team._member(remaining["id"], owner), tree="main", role="subagent", title="kept child")
    child_runtime = manager.get_runtime(new_child["session_id"], owner)
    before_meta = manager.get_meta(remaining["id"], owner)
    before_child = manager.get_meta(new_child["session_id"], owner)
    before_identity = team._row(remaining["id"])
    monkeypatch.setattr(app.state.router, "resolve_default", lambda: (_ for _ in ()).throw(AssertionError("Delete called a model")))

    async def scenario():
        async def waiting():
            await asyncio.Event().wait()
        tasks = [asyncio.create_task(waiting()), asyncio.create_task(waiting())]
        runtime._task, child_runtime._task = tasks
        runtime._set_phase("waiting_approval", busy=True)
        child_runtime._set_phase("tools", busy=True)
        aid = manager.approval_repo.create(remaining["id"], owner, "bash", "test", "test")
        future = asyncio.get_running_loop().create_future()
        runtime._pending_approvals[aid] = future
        try:
            result = manager.delete_project_conversation(owner, project["id"], original)
            assert result["default_session_id"] == remaining["id"]
            assert result["default_conversation"]["busy"] is True
            assert result["default_conversation"]["phase"] == "waiting_approval"
            assert manager.get_meta(remaining["id"], owner) == before_meta
            assert manager.get_meta(new_child["session_id"], owner) == before_child
            assert team._row(remaining["id"]) == before_identity
            assert manager._runtimes[remaining["id"]] is runtime
            assert manager._runtimes[new_child["session_id"]] is child_runtime
            assert not any(task.done() for task in tasks) and not future.done()
            assert manager.approval_repo.get(aid)["status"] == "pending"
            assert team._member(new_child["session_id"], owner)["lead_session_id"] == remaining["id"]
            assert manager.get_meta(original, owner)["status"] == "archived"
            assert manager.get_meta(old_child["session_id"], owner)["status"] == "archived"
        finally:
            runtime._pending_approvals.clear()
            runtime._set_phase("", busy=False)
            child_runtime._set_phase("", busy=False)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())
    listing = client.get(endpoint(project), headers=auth).json()
    assert listing["default_session_id"] == remaining["id"]
    assert [row["id"] for row in listing["conversations"]] == [remaining["id"]]


@pytest.mark.parametrize("keep_chat", [False, True])
def test_default_soft_delete_still_succeeds_after_project_folder_was_moved(integration, keep_chat):
    client, app, auth, project, root = integration
    original = roster(integration)["lead"]["session_id"]
    remaining = create_chat(integration) if keep_chat else None
    (root / "source.txt").unlink()
    root.rmdir()
    response = client.delete(f"{endpoint(project)}/{original}", headers=auth)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["default_conversation"]["effective_permission"] == "read-only"
    assert result["default_session_id"] != original
    if remaining:
        assert result["default_session_id"] == remaining["id"]
    assert not root.exists()
    assert client.delete(f"{endpoint(project)}/{original}", headers=auth).json() == result


def test_default_replacement_never_reparents_deleted_results_to_new_root(integration):
    _, app, _, project, _ = integration
    manager, team = app.state.sessions, app.state.project_agent_team
    owner = project["user_id"]
    original = roster(integration)["lead"]["session_id"]
    old_child = team._create_worker(project, team._member(original, owner), tree="main", role="subagent", title="old report")
    activation = team._begin_activation(old_child["session_id"], "old", "running")
    team._finish(old_child["session_id"], activation, "completed", result="OLD-ROOT-SECRET")
    result = manager.delete_project_conversation(owner, project["id"], original)
    new_id = result["default_session_id"]
    snapshot = team.sync(project["id"], owner)
    assert snapshot["lead"]["session_id"] == new_id and snapshot["agents"] == []
    child = team._create_worker(project, team._member(new_id, owner), tree="main", role="subagent", title="new report")
    activation = team._begin_activation(child["session_id"], "new", "running")
    team._finish(child["session_id"], activation, "completed", result="NEW-ROOT-RESULT")
    context = team._result_context(team._member(new_id, owner))
    assert "NEW-ROOT-RESULT" in context and "OLD-ROOT-SECRET" not in context
    assert team.descriptor(old_child["session_id"])["lead_session_id"] == original
    assert team.descriptor(old_child["session_id"])["state"] == "retired"
    with pytest.raises(KeyError):
        team._authorize_target(team._member(new_id, owner), team._member(old_child["session_id"], owner, live=False))


def test_a_promoted_default_chat_can_also_be_deleted_and_old_retries_keep_the_latest_default(integration):
    client, app, auth, project, _ = integration
    manager, team = app.state.sessions, app.state.project_agent_team
    owner = project["user_id"]
    original = roster(integration)["lead"]["session_id"]
    chat = create_chat(integration)
    first = client.delete(f"{endpoint(project)}/{original}", headers=auth).json()
    assert first["default_session_id"] == chat["id"]
    child = team._create_worker(project, team._member(chat["id"], owner), tree="main", role="subagent", title="promoted child")
    second = client.delete(f"{endpoint(project)}/{chat['id']}", headers=auth)
    assert second.status_code == 200, second.text
    new_id = second.json()["default_session_id"]
    assert new_id not in (original, chat["id"])
    assert second.json()["default_conversation"]["kind"] == "main"
    for sid in (original, chat["id"], child["session_id"]):
        assert manager.get_meta(sid, owner)["status"] == "archived"
        assert team.descriptor(sid)["state"] == "retired"
        retry = client.delete(f"{endpoint(project)}/{sid}", headers=auth)
        if sid == child["session_id"]:
            assert retry.status_code == 404
        else:
            assert retry.status_code == 200 and retry.json()["default_session_id"] == new_id
    assert app.state.projects.get(project["id"])["session_id"] == new_id
    assert len([row for row in manager.session_repo.list_by_project(project["id"]) if row["status"] == "active"]) == 1


def test_empty_default_replacement_does_not_adopt_an_unrelated_historical_main_root(integration):
    _, app, _, project, _ = integration
    manager, team = app.state.sessions, app.state.project_agent_team
    owner = project["user_id"]
    original = roster(integration)["lead"]["session_id"]
    legacy = manager.session_repo.create(owner, project_id=project["id"], kind="main", tree_id="main", title="historical main")
    team._insert(legacy, parent="", lead=legacy["id"], role="lead", depth=0, title="historical main")
    child = team._create_worker(project, team._member(legacy["id"], owner), tree="main", role="subagent", title="historical child")
    before = {sid: team._row(sid) for sid in (legacy["id"], child["session_id"])}
    result = manager.delete_project_conversation(owner, project["id"], original)
    assert result["default_session_id"] not in (original, legacy["id"])
    assert team._subtree(result["default_session_id"]) == []
    assert {sid: team._row(sid) for sid in before} == before
    assert manager.get_meta(legacy["id"], owner)["status"] == "active"


def test_concurrent_default_delete_retries_create_only_one_empty_replacement(integration):
    _, app, _, project, _ = integration
    manager = app.state.sessions
    original = roster(integration)["lead"]["session_id"]
    gate = Barrier(2)
    def delete_once():
        gate.wait(timeout=3)
        return manager.delete_project_conversation(project["user_id"], project["id"], original)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(delete_once) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert results[0] == results[1]
    replacement = results[0]["default_session_id"]
    assert app.state.projects.get(project["id"])["session_id"] == replacement
    active = [row["id"] for row in manager.session_repo.list_by_project(project["id"]) if row["status"] == "active"]
    assert active == [replacement]
