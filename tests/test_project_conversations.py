"""Independent project conversations use real local runtime/API paths, never a live model."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from code_agent.agent.project_team import TeamError
from code_agent.api.security import hash_password
from code_agent.permissions.policy import Decision
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionClosed, SessionManager, TEAM_TOOL_NAMES
from code_agent.tools.base import ToolContext
from test_project_team_integration import integration, roster


def endpoint(project):
    return f"/api/v1/code/projects/{project['id']}/conversations"


def create_chat(env, **body):
    client, _, auth, project, _ = env
    response = client.post(endpoint(project), headers=auth, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_list_is_readonly_and_new_windows_leave_the_canonical_lead_and_old_history_untouched(integration, monkeypatch):
    client, app, auth, project, root = integration
    def no_model():
        raise AssertionError("Listing or creating windows must not resolve a model")
    monkeypatch.setattr(app.state.router, "resolve_default", no_model)
    before = app.state.sessions.session_repo.list_by_project(project["id"])
    listing = client.get(endpoint(project), headers=auth)
    assert listing.status_code == 200, listing.text
    assert listing.json() == {"default_session_id": "", "conversations": []}
    assert app.state.sessions.session_repo.list_by_project(project["id"]) == before
    first = create_chat(integration)
    canonical = app.state.projects.get(project["id"])["session_id"]
    assert canonical and canonical != first["id"]
    snapshot = app.state.project_agent_team.descriptor(canonical)
    second = create_chat(integration)
    assert first["title"] == "新对话 1" and second["title"] == "新对话 2"
    assert app.state.projects.get(project["id"])["session_id"] == canonical
    assert app.state.project_agent_team.descriptor(canonical) == snapshot
    for chat in [first, second]:
        assert chat["kind"] == "chat" and chat["tree_id"] == "main" and chat["task_id"] == ""
        runtime = app.state.sessions.get_runtime(chat["id"], project["user_id"])
        assert runtime.workspace == root and runtime.agent_team is app.state.project_agent_team
        assert set(runtime._loop_context()[1].names()) & TEAM_TOOL_NAMES
        identity = app.state.project_agent_team.descriptor(chat["id"])
        assert identity["role"] == "lead" and identity["lead_session_id"] == chat["id"] and identity["parent_session_id"] == ""
        assert app.state.project_agent_team._subtree(chat["id"]) == [], "new conversations must not precreate workflow branches"
        assert app.state.transcript.replay(chat["id"]) == []
    assert app.state.db.query_all("SELECT * FROM project_agent_runs") == []
    assert app.state.transcript.replay(canonical) == []
    assert list(root.iterdir()) == [root / "source.txt"]


def test_new_window_inherits_the_selected_conversation_without_resetting_or_upgrading_any_other_window(integration):
    client, app, auth, project, _ = integration
    sid = roster(integration)["lead"]["session_id"]
    manager = app.state.sessions
    app.state.projects.set_flags(project["id"], allow_direct_write=True)
    manager.set_permission_mode(sid, project["user_id"], "full-access")
    manager.set_web_enabled(sid, project["user_id"], True)
    source = create_chat(integration, title="  独立讨论  ", from_session_id=sid)
    assert source["title"] == "独立讨论"
    assert (source["permission_mode"], source["web_enabled"]) == ("full-access", 1)
    assert "loop_mode" not in source
    manager.set_permission_mode(source["id"], project["user_id"], "read-only")
    manager.set_web_enabled(source["id"], project["user_id"], False)
    child = create_chat(integration, from_session_id=source["id"])
    assert (child["permission_mode"], child["web_enabled"]) == ("read-only", 0)
    assert "loop_mode" not in child
    before = manager.get_meta(sid, project["user_id"])
    listing = client.get(endpoint(project), headers=auth).json()
    assert listing["default_session_id"] == sid
    assert manager.get_meta(sid, project["user_id"]) == before
    assert (before["permission_mode"], before["web_enabled"]) == ("full-access", 1)


def test_roster_excludes_workers_task_planner_old_main_and_inactive_history_without_loading_runtimes_or_transcripts(integration, monkeypatch):
    client, app, auth, project, _ = integration
    lead = roster(integration)["lead"]["session_id"]
    first, archived = create_chat(integration), create_chat(integration)
    repo = app.state.sessions.session_repo
    app.state.sessions.archive(archived["id"], project["user_id"])
    for kind, tree, task in [("main", "main", ""), ("planner", "main", ""), ("task", "task:t1", "t1"),
                             ("chat", "task:t1", ""), ("chat", "main", "t1")]:
        repo.create(project["user_id"], project_id=project["id"], tree_id=tree, task_id=task, kind=kind)
    before = dict(app.state.sessions._runtimes)
    monkeypatch.setattr(app.state.transcript, "replay", lambda *_: (_ for _ in ()).throw(AssertionError("List must not read history")))
    response = client.get(endpoint(project), headers=auth)
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()["conversations"]} == {lead, first["id"]}
    assert app.state.sessions._runtimes == before
    assert all(not any(field in row for field in ("history", "events", "messages")) for row in response.json()["conversations"])


@pytest.mark.parametrize("body", [{"title": 1}, {"title": "a" * 101}, {"title": "bad\nlabel"},
                                  {"from_session_id": False}, {"permission_mode": "full-access"}, {"tree_id": "task:private"}])
def test_create_rejects_nontext_and_authority_fields_without_creating_records(integration, body):
    client, app, auth, project, _ = integration
    before = app.state.sessions.session_repo.list_by_project(project["id"])
    response = client.post(endpoint(project), headers=auth, json=body)
    assert response.status_code == 422, response.text
    assert app.state.sessions.session_repo.list_by_project(project["id"]) == before


def test_ownership_and_source_validation_reject_foreign_projects_workers_and_archived_windows(integration):
    client, app, auth, project, root = integration
    team = roster(integration)
    worker = app.state.project_agent_team._create_worker(
        project, app.state.project_agent_team._member(team["lead"]["session_id"], project["user_id"]),
        tree="main", role="subagent", title="source validation worker")
    chat = create_chat(integration)
    app.state.sessions.archive(chat["id"], project["user_id"])
    other = app.state.users.create("conversation-other", hash_password("test-only"), role="user")
    token = client.post("/api/v1/auth/login", json={"username": "conversation-other", "password": "test-only"}).json()["token"]
    other_auth = {"Authorization": "Bearer " + token}
    other_project = app.state.projects.create(other["id"], "other", str(root), "other-project-key")
    foreign = app.state.sessions.session_repo.create(other["id"], project_id=other_project["id"], kind="chat", tree_id="main")
    before = app.state.sessions.session_repo.list_by_project(project["id"])
    for method in (client.get, client.post):
        assert method(endpoint(project)).status_code == 401
        assert method(endpoint(project), headers=other_auth).status_code == 404
    for source in [worker["session_id"], chat["id"], foreign["id"], "s_missing"]:
        response = client.post(endpoint(project), headers=auth, json={"from_session_id": source})
        assert response.status_code == 404, response.text
    assert app.state.sessions.session_repo.list_by_project(project["id"]) == before


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "full-access"])
def test_existing_chat_permissions_recheck_project_switch_and_saved_mode_without_changing_grants(integration, mode):
    client, app, auth, project, root = integration
    chat = create_chat(integration)
    manager = app.state.sessions
    manager.set_permission_mode(chat["id"], project["user_id"], mode)
    runtime = manager.get_runtime(chat["id"], project["user_id"])
    permissions = runtime._make_permissions()
    ctx = ToolContext(chat["id"], root, project["user_id"])
    for allowed, effective in [(False, "read-only"), (True, mode), (False, "read-only"), (True, mode)]:
        app.state.projects.set_flags(project["id"], allow_direct_write=allowed)
        assert runtime._current_permission_mode() == effective
        decision = asyncio.run(permissions.evaluate("write_file", {"path": "source.txt"}, ctx)).decision
        assert decision is (Decision.need_approval if effective == "read-only" else Decision.allow)
        listing = client.get(endpoint(project), headers=auth).json()
        selected = next(row for row in listing["conversations"] if row["id"] == chat["id"])
        assert selected["effective_permission"] == effective and selected["permission_mode"] == mode
        assert manager.get_meta(chat["id"], project["user_id"])["permission_mode"] == mode
    manager.session_repo.set_permission_mode(chat["id"], "read-only")
    assert runtime._current_permission_mode() == "read-only", "direct metadata revocation must not reuse a cached grant"


def test_live_chat_loop_observes_a_project_write_revocation_before_its_next_tool(integration):
    _, app, _, project, root = integration
    chat = create_chat(integration)
    manager = app.state.sessions
    manager.set_permission_mode(chat["id"], project["user_id"], "workspace-write")
    app.state.projects.set_flags(project["id"], allow_direct_write=True)
    runtime = manager.get_runtime(chat["id"], project["user_id"])

    async def scenario():
        paused, resume = asyncio.Event(), asyncio.Event()
        class PausedProvider(OfflineProvider):
            async def chat(self, messages, tools):
                if self.calls == 2:
                    paused.set()
                    await resume.wait()
                return await super().chat(messages, tools)
        calls = [OfflineProvider.tool_call_turn("read_file", {"path": "source.txt"})]
        calls += [OfflineProvider.tool_call_turn("write_file", {"path": "source.txt", "content": value, "_approval": {
            "purpose": "验证当前对话的实时写入权限", "effects": ["更新临时 source.txt"],
        }}) for value in ("first", "must not write")]
        calls.append(OfflineProvider.text_turn("revoked write remained denied"))
        provider = PausedProvider(script=calls)
        app.state.router.resolve_default = lambda: provider
        approvals = []
        async def reject(request):
            approvals.append(request)
            return False
        runtime._server_approver = reject
        running = asyncio.create_task(runtime.submit("two writes with a revocation between them"))
        try:
            await asyncio.wait_for(paused.wait(), 3)
            assert (root / "source.txt").read_text() == "first"
            app.state.projects.set_flags(project["id"], allow_direct_write=False)
            resume.set()
            result = await asyncio.wait_for(running, 3)
            assert result.stop_reason == "end_turn" and provider.calls == 4
            assert (root / "source.txt").read_text() == "first"
            assert len(approvals) == 1
            assert manager.get_meta(chat["id"], project["user_id"])["permission_mode"] == "workspace-write"
            assert runtime.agent_team is app.state.project_agent_team and runtime._wake_notices == []
        finally:
            resume.set()
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
    asyncio.run(scenario())


def test_cached_chat_denies_tools_when_owner_status_binding_or_project_becomes_invalid(integration):
    _, app, _, project, root = integration
    chat = create_chat(integration)
    manager, db = app.state.sessions, app.state.db
    manager.set_permission_mode(chat["id"], project["user_id"], "full-access")
    app.state.projects.set_flags(project["id"], allow_direct_write=True)
    runtime = manager.get_runtime(chat["id"], project["user_id"])
    permissions = runtime._make_permissions()
    ctx = ToolContext(chat["id"], root, project["user_id"])
    other = app.state.users.create("binding-other", hash_password("test-only"), role="user")
    changes = [
        ("UPDATE sessions SET status=? WHERE id=?", ("archived", chat["id"]), ("active", chat["id"])),
        ("UPDATE sessions SET user_id=? WHERE id=?", (other["id"], chat["id"]), (project["user_id"], chat["id"])),
        ("UPDATE projects SET user_id=? WHERE id=?", (other["id"], project["id"]), (project["user_id"], project["id"])),
        ("UPDATE sessions SET tree_id=? WHERE id=?", ("task:changed", chat["id"]), ("main", chat["id"])),
    ]
    for sql, changed, restored in changes:
        db.execute(sql, changed)
        assert asyncio.run(permissions.evaluate("write_file", {"path": "source.txt"}, ctx)).decision is Decision.deny
        with pytest.raises((KeyError, SessionClosed, TeamError)):
            manager.get_runtime(chat["id"], project["user_id"])
        db.execute(sql, restored)
        assert runtime._current_permission_mode() == "full-access"


def test_recreated_manager_restores_project_map_settings_history_and_its_own_conversation_root(integration):
    _, app, _, project, root = integration
    chat = create_chat(integration)
    manager = app.state.sessions
    manager.set_web_enabled(chat["id"], project["user_id"], True)
    app.state.transcript.append(chat["id"], "user_message", {"text": "only this window's history"})
    rebuilt = SessionManager(transcript=manager.transcript, session_repo=manager.session_repo, approval_repo=manager.approval_repo,
                             router=manager.router, registry=manager.registry, project_repo=manager.project_repo, code_intel=manager.code_intel,
                             workspaces_root=manager.workspaces_root)
    rebuilt.agent_team = app.state.project_agent_team
    runtime = rebuilt.get_runtime(chat["id"], project["user_id"])
    assert runtime.workspace == Path(root) and runtime.agent_team is app.state.project_agent_team
    assert runtime.web_enabled is True and not hasattr(runtime, "loop_mode")
    assert runtime.code_context_provider is not None and runtime._code_context_provider_fn is not None
    assert runtime.code_project["id"] == project["id"]
    assert [event.payload["text"] for event in rebuilt.transcript.replay(chat["id"])] == ["only this window's history"]
    canonical = app.state.projects.get(project["id"])["session_id"]
    assert rebuilt.transcript.replay(canonical) == []
    identity = app.state.project_agent_team.descriptor(chat["id"])
    assert identity["lead_session_id"] == chat["id"] and identity["parent_session_id"] == ""
    assert rebuilt.list_project_conversations(project["user_id"], project["id"])["default_session_id"] == canonical


def test_delete_chat_archives_history_and_approvals_without_touching_project_files_or_other_conversations(integration, monkeypatch):
    client, app, auth, project, root = integration
    deleted, remaining = create_chat(integration), create_chat(integration)
    manager = app.state.sessions
    canonical = app.state.projects.get(project["id"])["session_id"]
    for sid in (canonical, deleted["id"], remaining["id"]):
        app.state.transcript.append(sid, "user_message", {"text": f"history:{sid}"})
    approvals = {sid: manager.approval_repo.create(sid, project["user_id"], "bash", "test-only", "test-only")
                 for sid in (deleted["id"], remaining["id"])}
    before_files = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(app.state.router, "resolve_default", lambda: (_ for _ in ()).throw(AssertionError("Delete must not call a model")))
    response = client.delete(f"{endpoint(project)}/{deleted['id']}", headers=auth)
    assert response.status_code == 200, response.text
    result = response.json()
    assert {key: result[key] for key in ("deleted", "session_id", "fallback_session_id", "default_session_id")} == {
        "deleted": True, "session_id": deleted["id"], "fallback_session_id": canonical, "default_session_id": canonical}
    assert result["default_conversation"] == manager._conversation_view(manager.get_meta(canonical, project["user_id"]))
    assert manager.get_meta(deleted["id"], project["user_id"])["status"] == "archived"
    assert deleted["id"] not in manager._runtimes
    assert {row["id"] for row in client.get(endpoint(project), headers=auth).json()["conversations"]} == {canonical, remaining["id"]}
    assert app.state.projects.get(project["id"])["session_id"] == canonical
    for sid in (canonical, deleted["id"], remaining["id"]):
        assert app.state.transcript.replay(sid)[-1].payload["text"] == f"history:{sid}"
    denied = manager.approval_repo.get(approvals[deleted["id"]])
    assert denied["status"] == "denied" and "conversation-delete" in denied["decided_by"]
    assert manager.approval_repo.get(approvals[remaining["id"]])["status"] == "pending"
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before_files
    # A retry remains safe and never escalates into a destructive history delete.
    assert client.delete(f"{endpoint(project)}/{deleted['id']}", headers=auth).json() == response.json()
    with pytest.raises(SessionClosed):
        manager.get_runtime(deleted["id"], project["user_id"])


def test_delete_chat_rejects_workers_other_projects_and_foreign_owners_before_any_mutation(integration):
    client, app, auth, project, root = integration
    roster_data = roster(integration)
    canonical = roster_data["lead"]["session_id"]
    worker = app.state.project_agent_team._create_worker(
        project, app.state.project_agent_team._member(canonical, project["user_id"]),
        tree="main", role="subagent", title="delete validation worker")["session_id"]
    chat = create_chat(integration)
    other = app.state.users.create("delete-conversation-other", hash_password("test-only"), role="user")
    token = client.post("/api/v1/auth/login", json={"username": "delete-conversation-other", "password": "test-only"}).json()["token"]
    other_auth = {"Authorization": "Bearer " + token}
    other_project = app.state.projects.create(project["user_id"], "another", str(root), "another-delete-project-key")
    foreign_project = app.state.projects.create(other["id"], "foreign", str(root), "foreign-delete-project-key")
    foreign = app.state.sessions.session_repo.create(other["id"], project_id=foreign_project["id"], kind="chat", tree_id="main")
    old_main = app.state.sessions.session_repo.create(project["user_id"], project_id=project["id"], kind="main", tree_id="main")
    task = app.state.sessions.session_repo.create(project["user_id"], project_id=project["id"], kind="task", tree_id="main", task_id="test-task")
    planner = app.state.sessions.session_repo.create(project["user_id"], project_id=project["id"], kind="planner", tree_id="main")
    invalid = [worker, old_main["id"], task["id"], planner["id"], foreign["id"], "s_missing"]
    before = app.state.db.query_all("SELECT * FROM sessions ORDER BY id")
    for sid in invalid:
        assert client.delete(f"{endpoint(project)}/{sid}", headers=auth).status_code in (404, 422)
    assert client.delete(f"{endpoint(project)}/{chat['id']}").status_code == 401
    assert client.delete(f"{endpoint(project)}/{chat['id']}", headers=other_auth).status_code == 404
    assert client.delete(f"{endpoint(other_project)}/{chat['id']}", headers=auth).status_code == 404
    assert app.state.db.query_all("SELECT * FROM sessions ORDER BY id") == before


def test_delete_chat_can_archive_when_project_directory_is_unavailable(integration):
    client, app, auth, project, root = integration
    chat = create_chat(integration)
    (root / "source.txt").unlink()
    root.rmdir()
    response = client.delete(f"{endpoint(project)}/{chat['id']}", headers=auth)
    assert response.status_code == 200, response.text
    assert app.state.sessions.get_meta(chat["id"], project["user_id"])["status"] == "archived"
    assert not root.exists(), "archiving must not recreate an unavailable project directory"


def test_delete_chat_cancels_its_own_live_task_approval_and_background_job_only(integration):
    _, app, _, project, _ = integration
    deleted, remaining = create_chat(integration), create_chat(integration)
    manager = app.state.sessions
    first = manager.get_runtime(deleted["id"], project["user_id"])
    second = manager.get_runtime(remaining["id"], project["user_id"])

    async def scenario():
        entered = asyncio.Event()
        async def waiting():
            entered.set()
            await asyncio.Event().wait()
        first._task, second._task = asyncio.create_task(waiting()), asyncio.create_task(waiting())
        first._set_phase("waiting_approval", busy=True)
        second._set_phase("waiting_approval", busy=True)
        pending = []
        for runtime in (first, second):
            aid = manager.approval_repo.create(runtime.session_id, project["user_id"], "bash", "test-only", "test-only")
            future = asyncio.get_running_loop().create_future()
            runtime._pending_approvals[aid] = future
            pending.append((aid, future))
        killed = []
        async def runner(job):
            await asyncio.Event().wait()
        jobs = [runtime.jobs.start(session_id=runtime.session_id, kind="test", label="test", runner=runner,
                                   kill=lambda sid=runtime.session_id: killed.append(sid)) for runtime in (first, second)]
        await entered.wait()
        try:
            result = manager.delete_project_conversation(project["user_id"], project["id"], deleted["id"])
            await asyncio.sleep(0)
            assert result["deleted"] is True and first._task.cancelled()
            assert pending[0][1].result() is False and manager.approval_repo.get(pending[0][0])["status"] == "denied"
            assert not second._task.done() and not pending[1][1].done()
            assert manager.approval_repo.get(pending[1][0])["status"] == "pending"
            assert killed == [deleted["id"]]
            assert jobs[0].kill_reason == "对话已删除" and jobs[1].kill_reason == ""
            assert first._team_wake_blocked is True and first._provider_wake_blocked is True
        finally:
            for runtime in (first, second):
                runtime._task.cancel()
                runtime._pending_approvals.clear()
            for job in jobs:
                job._task.cancel()
            await asyncio.gather(first._task, second._task, *(job._task for job in jobs), return_exceptions=True)
            first._set_phase("", busy=False)
            second._set_phase("", busy=False)
    asyncio.run(scenario())


def test_delete_chat_archives_only_its_owned_descendants_and_preserves_other_roots(integration):
    client, app, auth, project, root = integration
    deleted, remaining = create_chat(integration), create_chat(integration)
    team, manager = app.state.project_agent_team, app.state.sessions
    owner = project["user_id"]
    canonical = app.state.projects.get(project["id"])["session_id"]
    parent = team._member(deleted["id"], owner)
    child = team._create_worker(project, parent, tree="main", role="subagent", title="owned child")
    grandchild = team._create_worker(project, team._member(child["session_id"], owner), tree="main", role="subagent", title="owned grandchild")
    other_child = team._create_worker(project, team._member(remaining["id"], owner), tree="main", role="subagent", title="other root child")
    own_ids = [deleted["id"], child["session_id"], grandchild["session_id"]]
    kept_ids = [canonical, remaining["id"], other_child["session_id"]]
    approvals = {}
    for sid in own_ids + kept_ids:
        manager.get_runtime(sid, owner)
        app.state.transcript.append(sid, "user_message", {"text": f"preserved:{sid}"})
        approvals[sid] = manager.approval_repo.create(sid, owner, "bash", "test-only", "test-only")
    before_files = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    response = client.delete(f"{endpoint(project)}/{deleted['id']}", headers=auth)
    assert response.status_code == 200, response.text
    for sid in own_ids:
        assert manager.get_meta(sid, owner)["status"] == "archived"
        assert sid not in manager._runtimes
        assert manager.approval_repo.get(approvals[sid])["status"] == "denied"
        assert app.state.transcript.replay(sid)[-1].payload["text"] == f"preserved:{sid}"
    for sid in kept_ids:
        assert manager.get_meta(sid, owner)["status"] == "active"
        assert sid in manager._runtimes
        assert manager.approval_repo.get(approvals[sid])["status"] == "pending"
    assert app.state.projects.get(project["id"])["session_id"] == canonical
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before_files
