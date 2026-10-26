"""Workflow persistence, DAG gates, ownership, and concurrent save behavior."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.api.workflow_api import mount_workflow_api
from code_agent.codeintel.projects import ProjectService
from code_agent.codeintel.sop_workflow import (
    MAX_DOCUMENT_BYTES, WorkflowConflict, WorkflowDocument, WorkflowStore, default_workflow,
)
from code_agent.storage.db import Database, ProjectRepo


@pytest.fixture
def workflow_api(tmp_path):
    project_root = tmp_path / "user-project"
    project_root.mkdir()
    (project_root / "readme.txt").write_text("untouched", encoding="utf-8")
    db = Database(tmp_path / "platform" / "state.db")
    projects = ProjectRepo(db)
    project = projects.create("owner", "project", str(project_root), str(project_root))
    other = projects.create("owner", "other", str(tmp_path / "other"), str(tmp_path / "other"))
    store = WorkflowStore(db)
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("owner", "owner", "user")
    mount_workflow_api(app, service=ProjectService(projects), store=store)
    with TestClient(app) as client:
        yield client, app, store, db, project["id"], other["id"], project_root
    db.close()


def path(pid, tree="main"):
    return f"/api/v1/code/projects/{pid}/workflow?tree={tree}"


def editable(document=None):
    document = copy.deepcopy(document or default_workflow())
    document.pop("revision", None)
    document.pop("updated_at", None)
    return document


def save(client, pid, document, revision=0, tree="main"):
    return client.put(path(pid, tree), json={"expected_revision": revision, "workflow": document})


def test_default_get_is_read_only_and_returns_fresh_eight_stage_sop(workflow_api):
    client, _, _, db, pid, _, root = workflow_api
    first = client.get(path(pid))
    assert first.status_code == 200
    body = first.json()
    assert body["revision"] == 0 and body["updated_at"] == ""
    assert len(body["nodes"]) == 8 and len(body["edges"]) == 7
    assert [node["title"] for node in body["nodes"]] == [
        "需求确认", "UI与流程", "技术方案", "任务拆分", "开发实现", "测试验收", "发布交付", "维护迭代",
    ]
    assert all(node["status"] == "pending" for node in body["nodes"])
    assert [(node["x"], node["y"]) for node in body["nodes"]] == [
        (40, 60), (384, 60), (728, 60), (1072, 60), (1416, 60), (1760, 60), (2104, 60), (2448, 60),
    ]
    assert db.query_all("SELECT * FROM sop_workflows") == []
    body["nodes"][0]["title"] = "changed locally"
    assert client.get(path(pid)).json()["nodes"][0]["title"] == "需求确认"
    assert list(root.iterdir()) == [root / "readme.txt"]
    assert (root / "readme.txt").read_text() == "untouched"


def test_save_roundtrip_project_and_tree_isolation_and_revision(workflow_api):
    client, _, _, db, pid, other_pid, root = workflow_api
    document = editable()
    document["name"] = "项目 A"
    document["viewport"] = {"x": -125.5, "y": 76.25, "zoom": 0.75}
    document["nodes"][0].update(status="done", output="已确认需求与验收清单。")
    document["nodes"][1]["status"] = "active"
    response = save(client, pid, document)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["revision"] == 1 and saved["updated_at"]
    assert editable(saved) == document
    assert client.get(path(pid)).json() == saved
    assert client.get(path(other_pid)).json()["revision"] == 0
    assert client.get(path(pid, "task:t_1")).json()["revision"] == 0
    task = editable()
    task["name"] = "任务 SOP"
    assert save(client, pid, task, tree="task:t_1").status_code == 200
    assert client.get(path(pid, "task:t_1")).json()["name"] == "任务 SOP"
    assert client.get(path(pid)).json()["name"] == "项目 A"
    document["name"] = "项目 A 第二版"
    assert save(client, pid, document, revision=1).json()["revision"] == 2
    assert len(db.query_all("SELECT * FROM sop_workflows")) == 2
    assert list(root.iterdir()) == [root / "readme.txt"]


def test_persistence_survives_database_reopen(workflow_api, tmp_path):
    client, _, _, _, pid, _, _ = workflow_api
    document = editable()
    document["nodes"][0]["output"] = "重启后仍保留的阶段记录"
    assert save(client, pid, document).status_code == 200
    reopened = Database(tmp_path / "platform" / "state.db")
    try:
        persisted = WorkflowStore(reopened).get(pid, "main")
        assert persisted["revision"] == 1
        assert persisted["nodes"][0]["output"] == "重启后仍保留的阶段记录"
    finally:
        reopened.close()


@pytest.mark.parametrize("zoom", [0.05, 2.5])
def test_zoom_boundaries_roundtrip(workflow_api, zoom):
    client, _, _, _, pid, _, _ = workflow_api
    document = editable()
    document["viewport"]["zoom"] = zoom
    response = save(client, pid, document)
    assert response.status_code == 200, response.text
    assert client.get(path(pid)).json()["viewport"]["zoom"] == zoom


def test_owner_and_unknown_project_cannot_read_or_write(workflow_api):
    client, app, _, _, pid, _, _ = workflow_api
    for project in ("missing", pid):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser("other-user", "other-user", "admin")
        assert client.get(path(project)).status_code == 404
        assert save(client, project, editable()).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("owner", "owner", "user")
    assert client.get(path(pid)).json()["revision"] == 0


def test_stale_revision_does_not_overwrite_or_create(workflow_api):
    client, _, _, db, pid, _, _ = workflow_api
    document = editable()
    assert save(client, pid, document, revision=5).status_code == 409
    assert db.query_all("SELECT * FROM sop_workflows") == []
    assert save(client, pid, document).status_code == 200
    document["name"] = "stale"
    conflict = save(client, pid, document)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_revision"] == 1
    assert client.get(path(pid)).json()["name"] == "项目开发 SOP"


def test_simultaneous_revision_zero_saves_only_one_winner(workflow_api, tmp_path):
    _, _, _, _, pid, _, _ = workflow_api
    databases = [Database(tmp_path / "platform" / "state.db") for _ in range(2)]
    stores = [WorkflowStore(db) for db in databases]

    def write(index):
        document = editable()
        document["name"] = f"writer-{index}"
        try:
            return stores[index].save(pid, "main", WorkflowDocument.model_validate(document), 0)
        except WorkflowConflict:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, range(2)))
        winners = [result for result in results if result]
        assert len(winners) == 1
        assert stores[0].get(pid, "main") == winners[0]
    finally:
        for database in databases:
            database.close()


@pytest.mark.parametrize("case", [
    "duplicate_node", "duplicate_edge_id", "duplicate_edge_pair", "self_edge", "dangling_edge", "cycle",
    "unknown_type", "unknown_status", "empty_title", "empty_name", "unexpected_field", "schema_bool",
    "schema_unknown", "coordinate_bool", "coordinate_string", "coordinate_bound", "zoom_low", "zoom_high",
    "output_too_long", "node_limit", "edge_limit", "done_without_output", "done_with_pending_predecessor",
    "active_with_pending_predecessor",
])
def test_invalid_documents_are_rejected_without_persisting(workflow_api, case):
    client, _, _, db, pid, _, _ = workflow_api
    doc = editable()
    if case == "duplicate_node":
        doc["nodes"].append(copy.deepcopy(doc["nodes"][0]))
    elif case == "duplicate_edge_id":
        doc["edges"][1]["id"] = doc["edges"][0]["id"]
    elif case == "duplicate_edge_pair":
        doc["edges"].append({**doc["edges"][0], "id": "extra"})
    elif case == "self_edge":
        doc["edges"][0]["target"] = doc["edges"][0]["source"]
    elif case == "dangling_edge":
        doc["edges"][0]["source"] = "missing"
    elif case == "cycle":
        doc["edges"].append({"id": "back", "source": "maintenance", "target": "requirements"})
    elif case == "unknown_type":
        doc["nodes"][0]["type"] = "shell"
    elif case == "unknown_status":
        doc["nodes"][0]["status"] = "executing"
    elif case == "empty_title":
        doc["nodes"][0]["title"] = "   "
    elif case == "empty_name":
        doc["name"] = "   "
    elif case == "unexpected_field":
        doc["nodes"][0]["command"] = "echo should never run"
    elif case == "schema_bool":
        doc["schema_version"] = True
    elif case == "schema_unknown":
        doc["schema_version"] = 2
    elif case == "coordinate_bool":
        doc["nodes"][0]["x"] = True
    elif case == "coordinate_string":
        doc["nodes"][0]["x"] = "12"
    elif case == "coordinate_bound":
        doc["nodes"][0]["x"] = 100001
    elif case == "zoom_low":
        doc["viewport"]["zoom"] = 0.049
    elif case == "zoom_high":
        doc["viewport"]["zoom"] = 10
    elif case == "output_too_long":
        doc["nodes"][0]["output"] = "x" * 40001
    elif case == "node_limit":
        doc["nodes"] = [{**doc["nodes"][0], "id": f"node_{i}"} for i in range(129)]
        doc["edges"] = []
    elif case == "edge_limit":
        doc["edges"] *= 74
    elif case == "done_without_output":
        doc["nodes"][0].update(status="done", output="  ")
    elif case == "done_with_pending_predecessor":
        doc["nodes"][1].update(status="done", output="完成记录")
    elif case == "active_with_pending_predecessor":
        doc["nodes"][1]["status"] = "active"
    response = save(client, pid, doc)
    assert response.status_code == 422, response.text
    assert db.query_all("SELECT * FROM sop_workflows") == []


@pytest.mark.parametrize("revision", [True, -1, "0", 1.5, 2**53])
def test_revision_is_strict_nonnegative_safe_integer(workflow_api, revision):
    client, _, _, _, pid, _, _ = workflow_api
    assert save(client, pid, editable(), revision).status_code == 422


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_json_number_is_clean_validation_error(workflow_api, value):
    client, _, _, db, pid, _, _ = workflow_api
    payload = json.dumps({"expected_revision": 0, "workflow": editable()})
    payload = payload.replace('"x": 40', f'"x": {value}', 1)
    response = client.put(path(pid), content=payload, headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert db.query_all("SELECT * FROM sop_workflows") == []


@pytest.mark.parametrize("tree", ["", "../main", "main/other", "task:", "task:../outside", "a" * 102])
def test_invalid_tree_identifiers(workflow_api, tree):
    client, _, _, _, pid, _, _ = workflow_api
    assert client.get(path(pid, tree)).status_code == 422
    assert save(client, pid, editable(), tree=tree).status_code == 422


def test_empty_graph_is_allowed_and_ready_branches_can_both_be_active(workflow_api):
    client, _, _, _, pid, _, _ = workflow_api
    document = editable()
    document["edges"] = []
    document["nodes"][0]["status"] = document["nodes"][1]["status"] = "active"
    assert save(client, pid, document).status_code == 200
    document["nodes"] = []
    assert save(client, pid, document, 1).status_code == 200
    assert client.get(path(pid)).json()["nodes"] == []


def test_fork_and_join_require_every_predecessor_to_finish(workflow_api):
    client, _, _, _, pid, _, _ = workflow_api
    document = editable()
    document["nodes"] = document["nodes"][:4]
    document["edges"] = [
        {"id": "a", "source": "requirements", "target": "ui_design"},
        {"id": "b", "source": "requirements", "target": "architecture"},
        {"id": "c", "source": "ui_design", "target": "planning"},
        {"id": "d", "source": "architecture", "target": "planning"},
    ]
    document["nodes"][0].update(status="done", output="需求已确认")
    document["nodes"][1]["status"] = document["nodes"][2]["status"] = "active"
    assert save(client, pid, document).status_code == 200
    document["nodes"][1].update(status="done", output="UI 已确认")
    document["nodes"][3]["status"] = "active"
    assert save(client, pid, document, 1).status_code == 422
    document["nodes"][2].update(status="done", output="技术方案已确认")
    assert save(client, pid, document, 1).status_code == 200


def test_removing_project_cascades_platform_workflow_data(workflow_api):
    client, _, _, db, pid, _, root = workflow_api
    assert save(client, pid, editable()).status_code == 200
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    assert db.query_all("SELECT * FROM sop_workflows") == []
    assert root.is_dir() and (root / "readme.txt").read_text() == "untouched"


def test_reopening_predecessor_requires_downstream_status_reset(workflow_api):
    client, _, _, _, pid, _, _ = workflow_api
    document = editable()
    document["nodes"][0].update(status="done", output="已确认需求")
    document["nodes"][1].update(status="done", output="已确认界面")
    document["nodes"][2]["status"] = "active"
    assert save(client, pid, document).status_code == 200
    document["nodes"][0]["status"] = "pending"
    assert save(client, pid, document, 1).status_code == 422
    assert client.get(path(pid)).json()["revision"] == 1
    document["nodes"][1]["status"] = document["nodes"][2]["status"] = "pending"
    saved = save(client, pid, document, 1)
    assert saved.status_code == 200
    assert saved.json()["nodes"][1]["output"] == "已确认界面"


def test_request_limit_and_malformed_json(workflow_api):
    client, _, _, _, pid, _, _ = workflow_api
    assert client.put(path(pid), content="{").status_code == 422
    assert client.put(path(pid), content="[" * 2000 + "]" * 2000).status_code == 422
    assert client.put(path(pid), content=b" " * (MAX_DOCUMENT_BYTES + 1)).status_code == 413


def test_main_application_mounts_workflow_api(agent_home):
    from code_agent.api.app import create_app

    app = create_app(provider_name="offline")
    with TestClient(app) as client:
        token = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        root = agent_home.parent / "mount-project"
        root.mkdir()
        project = client.post("/api/v1/code/projects", json={"mode": "open", "path": str(root)}, headers=headers)
        assert project.status_code == 200, project.text
        response = client.get(path(project.json()["id"]), headers=headers)
        assert response.status_code == 200, response.text
        assert len(response.json()["nodes"]) == 8


@pytest.mark.parametrize("stage", ["read_owner", "read_store", "save_owner", "validate_tree", "save_store", "sync_team"])
def test_waiting_for_workflow_storage_or_team_keeps_other_requests_responsive(workflow_api, monkeypatch, stage):
    """A DB/team lock wait must not occupy the ASGI event loop."""
    _, _, store, db, pid, _, _ = workflow_api
    service = ProjectService(ProjectRepo(db))
    entered = threading.Event()
    release = threading.Event()
    timed_out = threading.Event()

    class Team:
        def validate_tree(self, *_args):
            return {}

        def sync(self, *_args):
            return {}

    team = Team()
    targets = {
        "read_owner": (service, "get_owned"), "save_owner": (service, "get_owned"),
        "read_store": (store, "get"), "save_store": (store, "save"),
        "validate_tree": (team, "validate_tree"), "sync_team": (team, "sync"),
    }
    target, name = targets[stage]
    original = getattr(target, name)

    def wait_for_lock(*args, **kwargs):
        entered.set()
        # This finite fallback makes the regression fail cleanly when an old
        # async handler blocks the loop, instead of deadlocking the test suite.
        if not release.wait(1.0):
            timed_out.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, wait_for_lock)
    app = FastAPI()

    async def owner():
        return CurrentUser("owner", "owner", "user")

    app.dependency_overrides[get_current_user] = owner
    mount_workflow_api(app, service=service, store=store, team=team)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            if stage.startswith("read"):
                operation = asyncio.create_task(client.get(path(pid)))
            else:
                operation = asyncio.create_task(client.put(path(pid), json={
                    "expected_revision": 0, "workflow": editable(),
                }))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(0.005)
                assert not timed_out.is_set(), "workflow lock blocked the event loop"
                assert not operation.done()
                response = await asyncio.wait_for(client.get("/probe"), timeout=0.25)
                assert response.json() == {"ok": True}
                assert not operation.done(), "blocked workflow should still be waiting"
            finally:
                release.set()
                result = await operation
            assert result.status_code == 200, result.text

    asyncio.run(exercise())


def test_read_workflow_does_not_initialize_or_sync_agent_team(workflow_api):
    _, _, store, db, pid, _, _ = workflow_api

    class UnusedTeam:
        def validate_tree(self, *_args):
            raise AssertionError("a workflow read must not validate execution sessions")

        def sync(self, *_args):
            raise AssertionError("a workflow read must not create workflow agents")

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("owner", "owner", "user")
    mount_workflow_api(app, service=ProjectService(ProjectRepo(db)), store=store, team=UnusedTeam())
    with TestClient(app) as client:
        assert client.get(path(pid)).status_code == 200
    assert db.query_all("SELECT * FROM sop_workflows") == []
