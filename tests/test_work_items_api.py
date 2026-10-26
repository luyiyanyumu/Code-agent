"""Unified task list/creation without a model or user workspace."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.api.security import hash_password


@pytest.fixture
def env(agent_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_CODE_WATCH", "0")
    async def no_probe():
        return {"found": False}
    monkeypatch.setattr("code_agent.retrieval.embed.autodetect_local", no_probe)
    app = create_app(provider_name="offline")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    with TestClient(app) as client:
        token = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
        auth = {"Authorization": "Bearer " + token}
        project = client.post("/api/v1/code/projects", json={"mode": "open", "path": str(root)}, headers=auth).json()
        pid = project["id"]
        owned = app.state.projects.get(pid)
        roster = client.get(f"/api/v1/code/projects/{pid}/agents", headers=auth).json()
        yield app, client, auth, owned, root, roster


def path(env):
    return f"/api/v1/code/projects/{env[3]['id']}/work-items"


def create_row(env, *, batch_id="", lane="", status="planned"):
    app, _, _, project, root, _ = env
    worktree = root.parent / (lane or "isolated")
    worktree.mkdir(exist_ok=True)
    return app.state.tasks.create(project_id=project["id"], user_id=project["user_id"],
        title=lane or "隔离修改", requirement="修复功能", base_branch="main", branch="test/isolated",
        worktree=str(worktree), status=status, batch_id=batch_id, lane=lane)


def test_empty_list_omits_unassigned_workflow_nodes_and_never_dispatches(env):
    app, client, auth, _, _, _ = env
    before = app.state.db.query_all("SELECT * FROM project_agent_runs")
    sessions = app.state.db.query_all("SELECT id FROM sessions")
    first = client.get(path(env), headers=auth)
    assert first.status_code == 200, first.text
    assert first.json()["items"] == []
    assert first.json()["lead"]["role"] == "lead"
    assert first.json()["capabilities"] == {"team": True, "tasks": True, "parallel": True}
    assert first.json() == client.get(path(env), headers=auth).json()
    assert app.state.db.query_all("SELECT * FROM project_agent_runs") == before
    assert app.state.db.query_all("SELECT id FROM sessions") == sessions


def test_list_unifies_team_isolated_and_batch_without_duplicate_workers(env):
    app, client, auth, project, _, roster = env
    team = app.state.project_agent_team
    lead = roster["lead"]
    ordinary = team._create_worker(project, lead, tree="main", role="subagent", title="普通功能开发")
    app.state.db.execute("UPDATE project_agents SET activation_id=1,state='completed' WHERE session_id=?", (ordinary["session_id"],))
    task = create_row(env, status="review")
    worker = team._create_worker(project, lead, tree=f"task:{task['id']}", role="subagent", title="隔离执行")
    app.state.db.execute("UPDATE project_agents SET activation_id=1,state='completed',audit_decision='pending' WHERE session_id=?", (worker["session_id"],))
    app.state.tasks.update(task["id"], session_id=worker["session_id"], review_json=json.dumps({"verification": {"ok": True, "results": []}}))
    batch = app.state.batches.create(project_id=project["id"], user_id=project["user_id"], title="前后端改动", requirement="开发上传", base_branch="main", status="planned")
    lane = create_row(env, batch_id=batch["id"], lane="backend")
    lane_worker = team._create_worker(project, lead, tree=f"task:{lane['id']}", role="subagent", title="后端车道")
    app.state.tasks.update(lane["id"], session_id=lane_worker["session_id"])
    app.state.db.execute("UPDATE project_agents SET activation_id=1,state='completed' WHERE session_id=?", (lane_worker["session_id"],))
    body = client.get(path(env), headers=auth).json()
    assert {i["id"] for i in body["items"]} == {f"agent:{ordinary['session_id']}", f"task:{task['id']}", f"batch:{batch['id']}"}
    item = next(i for i in body["items"] if i["kind"] == "task")
    assert item["state"] == "review" and item["execution_state"] == "completed"
    assert item["audit"]["decision"] == "pending" and item["integration_state"] == "review"
    assert item["verification"]["ok"] is True
    batch_item = next(i for i in body["items"] if i["kind"] == "batch")
    assert batch_item["children"][0]["task_id"] == lane["id"]
    assert batch_item["batch"]["lanes"][0]["session_id"] == lane_worker["session_id"]


def test_approval_on_recursive_child_keeps_actual_executor_identity(env):
    app, client, auth, project, _, roster = env
    team = app.state.project_agent_team
    task = create_row(env, status="running")
    worker = team._create_worker(project, roster["lead"], tree=f"task:{task['id']}", role="subagent", title="主执行")
    child = team._create_worker(project, worker, tree=f"task:{task['id']}", role="subagent", title="验证命令")
    app.state.tasks.update(task["id"], session_id=worker["session_id"])
    app.state.db.execute("UPDATE project_agents SET activation_id=1,state='running' WHERE session_id=?", (child["session_id"],))
    app.state.sessions._runtimes[child["session_id"]] = SimpleNamespace(phase="waiting_approval", pending_approval=lambda: {"approval_id": "a_current", "status": "pending"})
    body = client.get(path(env), headers=auth).json()
    item = next(i for i in body["items"] if i["kind"] == "task")
    assert item["active"] is True
    assert item["phase"] == "waiting_approval"
    assert item["pending_approval"]["session_id"] == child["session_id"]
    detail = client.get(f"/api/v1/code/projects/{project['id']}/tasks/{task['id']}", headers=auth).json()
    assert detail["pending_approval"]["session_id"] == child["session_id"]
    assert body["counts"]["attention"] >= 1
    app.state.sessions._runtimes.pop(child["session_id"])


def question_runtime(app, member, *question_ids):
    """Synthetic descriptors only; no provider dispatch or actual user questions."""
    groups = [{"id": question_id, "question_id": question_id, "session_id": "ignored-session",
               "tree_id": "ignored-tree", "status": "pending", "questions": [
                   {"id": "fixture", "question": "Choose the fixture behavior?"}]} for question_id in question_ids]
    app.state.db.execute("UPDATE project_agents SET activation_id=1,state='running' WHERE session_id=?", (member["session_id"],))
    app.state.sessions._runtimes[member["session_id"]] = SimpleNamespace(
        phase="waiting_question", current_tool="ask_user_question", pending_approval=lambda: None,
        pending_questions=lambda: groups, session_id=member["session_id"], broadcast=lambda event: None,
    )


def test_team_work_item_preserves_every_question_group_and_actual_tree(env):
    app, client, auth, project, _, roster = env
    worker = app.state.project_agent_team._create_worker(
        project, roster["lead"], tree="main", role="subagent", title="提问任务")
    question_runtime(app, worker, "q_first", "q_second")
    try:
        body = client.get(path(env), headers=auth).json()
        item = next(item for item in body["items"] if item["session_id"] == worker["session_id"])
        assert [group["question_id"] for group in item["pending_questions"]] == ["q_first", "q_second"]
        assert all(group["session_id"] == worker["session_id"] and group["tree_id"] == "main"
                   for group in item["pending_questions"])
        assert item["agent"]["pending_questions"] == item["pending_questions"]
        assert item["phase"] == "waiting_question" and item["execution_managed"] is True
        assert body["counts"]["attention"] == 1
    finally:
        app.state.sessions._runtimes.pop(worker["session_id"])


def test_recursive_isolated_question_groups_reach_task_list_and_detail(env):
    app, client, auth, project, _, roster = env
    team = app.state.project_agent_team
    task = create_row(env, status="running")
    tree = f"task:{task['id']}"
    worker = team._create_worker(project, roster["lead"], tree=tree, role="subagent", title="主执行")
    child = team._create_worker(project, worker, tree=tree, role="subagent", title="提问执行者")
    app.state.tasks.update(task["id"], session_id=worker["session_id"])
    app.state.db.execute("UPDATE project_agents SET activation_id=1,state='running' WHERE session_id=?", (worker["session_id"],))
    app.state.sessions._runtimes[worker["session_id"]] = SimpleNamespace(phase="model", pending_approval=lambda: None)
    question_runtime(app, child, "q_nested_one", "q_nested_two")
    try:
        body = client.get(path(env), headers=auth).json()
        item = next(item for item in body["items"] if item["kind"] == "task")
        detail = client.get(f"/api/v1/code/projects/{project['id']}/tasks/{task['id']}", headers=auth).json()
        assert len(item["pending_questions"]) == 2
        assert all(group["session_id"] == child["session_id"] and group["tree_id"] == tree
                   for group in item["pending_questions"])
        assert detail["pending_questions"] == item["pending_questions"]
        assert item["session_id"] == worker["session_id"]
        assert item["phase"] == "waiting_question" and detail["phase"] == "waiting_question"
        assert item["workspace_available"] is True and item["execution_managed"] is True
        assert body["counts"]["attention"] >= 1
    finally:
        for member in (worker, child):
            app.state.sessions._runtimes.pop(member["session_id"])


def test_parallel_questions_aggregate_planners_and_all_lane_executors(env):
    app, client, auth, project, _, roster = env
    team = app.state.project_agent_team
    batch = app.state.batches.create(project_id=project["id"], user_id=project["user_id"],
        title="并行提问", requirement="fixture", base_branch="main", status="running")
    planner = team._create_worker(project, roster["lead"], tree="main", role="subagent", title="规划")
    app.state.db.execute("UPDATE sessions SET task_id=? WHERE id=?", (batch["id"], planner["session_id"]))
    question_runtime(app, planner, "q_plan")
    first = create_row(env, batch_id=batch["id"], lane="question-first", status="running")
    second = create_row(env, batch_id=batch["id"], lane="question-second", status="running")
    first_worker = team._create_worker(project, roster["lead"], tree=f"task:{first['id']}", role="subagent", title="车道一")
    second_worker = team._create_worker(project, roster["lead"], tree=f"task:{second['id']}", role="subagent", title="车道二")
    child = team._create_worker(project, first_worker, tree=f"task:{first['id']}", role="subagent", title="车道一子任务")
    app.state.tasks.update(first["id"], session_id=first_worker["session_id"])
    app.state.tasks.update(second["id"], session_id=second_worker["session_id"])
    question_runtime(app, first_worker, "q_lane_one")
    question_runtime(app, child, "q_lane_child_a", "q_lane_child_b")
    question_runtime(app, second_worker, "q_lane_two")
    try:
        body = client.get(path(env), headers=auth).json()
        item = next(item for item in body["items"] if item["kind"] == "batch")
        assert {group["question_id"] for group in item["pending_questions"]} == {
            "q_plan", "q_lane_one", "q_lane_child_a", "q_lane_child_b", "q_lane_two"}
        by_question = {group["question_id"]: group for group in item["pending_questions"]}
        assert by_question["q_plan"]["session_id"] == planner["session_id"]
        assert by_question["q_plan"]["tree_id"] == "main"
        assert by_question["q_lane_child_a"]["session_id"] == child["session_id"]
        assert by_question["q_lane_child_a"]["tree_id"] == f"task:{first['id']}"
        lanes = {lane["task_id"]: lane for lane in item["batch"]["lanes"]}
        assert len(lanes[first["id"]]["pending_questions"]) == 3
        assert len(lanes[second["id"]]["pending_questions"]) == 1
        detail = client.get(f"/api/v1/code/projects/{project['id']}/batches/{batch['id']}", headers=auth).json()
        details = {lane["task_id"]: lane for lane in detail["lanes"]}
        for lane_id in (first["id"], second["id"]):
            assert {group["question_id"]: group for group in details[lane_id]["pending_questions"]} == {
                group["question_id"]: group for group in lanes[lane_id]["pending_questions"]}
        assert body["counts"]["attention"] >= 1
    finally:
        for member in (planner, first_worker, second_worker, child):
            app.state.sessions._runtimes.pop(member["session_id"])


def test_task_current_executor_prioritizes_approval_then_question(tmp_path):
    from code_agent.codeintel.work_items import agent_item, task_item

    row = {"id": "t_fixture", "session_id": "s_worker", "status": "running", "worktree": str(tmp_path)}
    busy = {"session_id": "s_worker", "tree_id": "task:t_fixture", "state": "running", "phase": "model"}
    question = {"session_id": "s_question", "tree_id": "task:t_fixture", "state": "running",
                "phase": "waiting_question", "pending_questions": [{"id": "q_fixture"}]}
    approval = {"session_id": "s_approval", "tree_id": "task:t_fixture", "state": "running",
                "phase": "waiting_approval", "pending_approval": {"approval_id": "a_fixture"}}
    item = task_item(row, {}, [busy, question])
    assert item["phase"] == "waiting_question" and item["pending_questions"][0]["session_id"] == "s_question"
    assert item["session_id"] == "s_worker" and item["execution_managed"] is True
    assert task_item(row, {}, [busy, question, approval])["phase"] == "waiting_approval"
    assert agent_item({**question, "state": "completed"})["pending_questions"] == []


def test_historical_cleaned_worktree_cannot_be_opened_for_execution(env):
    app, client, auth, project, _, roster = env
    team = app.state.project_agent_team
    task = create_row(env, status="review")
    worker = team._create_worker(project, roster["lead"], tree=f"task:{task['id']}", role="subagent", title="隔离主项")
    child = team._create_worker(project, worker, tree=f"task:{task['id']}", role="subagent", title="历史验证")
    app.state.tasks.update(task["id"], session_id=worker["session_id"], status="merged",
        review_json=json.dumps({"verification": [{"command": "test", "ok": True}], "verification_stale": True}))
    app.state.db.execute("UPDATE project_agents SET activation_id=1,state='completed' WHERE session_id IN (?,?)", (worker["session_id"], child["session_id"]))
    body = client.get(path(env), headers=auth).json()
    item = next(i for i in body["items"] if i["kind"] == "task")
    history = next(i for i in body["items"] if i["kind"] == "agent")
    assert item["state"] == "merged"
    assert item["workspace_available"] is False and history["workspace_available"] is False
    assert item["verification"]["ok"] is None and item["verification"]["stale"] is True
    assert item["verification"]["results"][0]["command"] == "test"


def test_list_requires_auth_and_owned_project(env):
    app, client, auth, _, _, _ = env
    assert client.get(path(env)).status_code == 401
    assert client.get(path(env).replace(env[3]["id"], "p_missing"), headers=auth).status_code == 404
    app.state.users.create(username="other_work_user", password_hash=hash_password("secret123"))
    token = client.post("/api/v1/auth/login", json={"username": "other_work_user", "password": "secret123"}).json()["token"]
    other = {"Authorization": "Bearer " + token}
    assert client.get(path(env), headers=other).status_code == 404
    assert client.post(path(env), json={"requirement": "越权派工"}, headers=other).status_code == 404


@pytest.mark.parametrize("payload", [
    {"requirement": " "}, {"requirement": True}, {"requirement": "x", "execution_mode": "unknown"},
    {"requirement": "x", "git_init": "false"}, {"requirement": "x", "unrecognized": True},
    {"requirement": "x", "tree": "../other"},
    {"requirement": "x", "execution_mode": "isolated", "git_init": True, "plan": {"lanes": []}},
    {"requirement": "x", "execution_mode": "parallel", "plan": {"lanes": []}},
    {"requirement": "x", "plan": {"subtasks": []}},
])
def test_create_rejects_invalid_inputs_before_dispatch(env, payload):
    app, client, auth, _, _, _ = env
    assert client.post(path(env), json=payload, headers=auth).status_code == 422
    assert app.state.db.query_all("SELECT * FROM project_agent_runs") == []


def test_create_team_uses_lead_dispatch(env, monkeypatch):
    app, client, auth, project, _, roster = env
    assert roster["agents"] == []
    worker = app.state.project_agent_team._create_worker(
        project, roster["lead"], tree="main", role="subagent", title="登录修复")
    calls = []
    async def start(caller, user, prompt, **kwargs):
        calls.append((caller, user, prompt, kwargs))
        return {**worker, "state": "queued", "activation_id": 1}
    monkeypatch.setattr(app.state.project_agent_team, "start", start)
    response = client.post(path(env), headers=auth, json={"requirement": "  修复登录并验证  ", "title": "登录修复"})
    assert response.status_code == 200, response.text
    assert response.json()["item"]["id"] == f"agent:{worker['session_id']}"
    assert calls[0][0] == roster["lead"]["session_id"]
    assert calls[0][2] == "修复登录并验证"
    assert calls[0][3] == {"title": "登录修复", "tree": "main"}


def test_create_isolated_and_parallel_preserves_manual_plans(env):
    _, client, auth, _, _, _ = env
    one = client.post(path(env), headers=auth, json={"requirement": "隔离修复", "execution_mode": "isolated", "git_init": True,
        "plan": {"goal": "修复", "subtasks": [{"id": "fix", "files": ["source.py"], "verify": []}]}})
    assert one.status_code == 200, one.text
    assert one.json()["item"]["kind"] == "task"
    assert one.json()["item"]["state"] == "planned"
    many = client.post(path(env), headers=auth, json={"requirement": "依赖改动", "execution_mode": "parallel", "plan": {
        "goal": "两步修改", "lanes": [{"id": "a", "files": ["source.py"]}, {"id": "b", "files": ["source.py"]}]}})
    assert many.status_code == 200, many.text
    item = many.json()["item"]
    assert item["kind"] == "batch" and len(item["children"]) == 2
    assert item["batch"]["lanes"][1]["status"] == "deferred"
    assert item["batch"]["lanes"][1]["wait_for"] == ["a"]
    assert item["batch"]["lanes"][1]["worktree"] == ""


def test_nested_isolated_creation_and_invalid_limit_are_rejected(env):
    app, client, auth, _, _, _ = env
    response = client.post(path(env), headers=auth, json={"requirement": "创建新树", "execution_mode": "isolated", "tree": "task:t_missing"})
    assert response.status_code == 422
    assert app.state.db.query_all("SELECT * FROM tasks") == []
    assert client.get(path(env) + "?limit=101", headers=auth).status_code == 422
