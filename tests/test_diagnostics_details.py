"""Problems 面板读取已存诊断：概览兼容、完整明细、筛选与限制。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent.api.code_api import mount_code_api
from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel.projects import ProjectService
from code_agent.codeintel.service import CodeIntelService
from code_agent.storage.db import Database, ProjectRepo


@pytest.fixture
def diagnostics_fixture(agent_home, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    db = Database(agent_home / "api.db")
    projects = ProjectRepo(db)
    project = projects.create("owner", "repo", str(root), str(root))
    intel = CodeIntelService(projects)
    index = intel.index(project["id"])
    index.set_diagnostics(rel="src/a.js", uri="file:///a.js", items=[
        {"line": 3, "character": 0, "end_line": 3, "end_character": 5, "severity": 1, "message": "bad import", "source": "ts"},
        {"line": 7, "character": 4, "end_line": 7, "end_character": 8, "severity": 2, "message": "unused value", "source": "lint"},
    ])
    index.set_diagnostics(rel="src/b.js", uri="file:///b.js", items=[
        {"line": 2, "character": 9, "severity": 1, "message": "missing name", "source": "ts"},
    ])
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("owner", "owner", "admin")
    mount_code_api(app, project_repo=projects, manager=None, service=ProjectService(projects), intel=intel)
    with TestClient(app) as client:
        yield client, project, intel
    intel.close()
    db.close()


def test_summary_stays_light_and_details_return_monaco_positions(diagnostics_fixture):
    client, project, _ = diagnostics_fixture
    url = f"/api/v1/code/projects/{project['id']}/diagnostics"
    summary = client.get(url).json()
    assert summary["items"] == []
    assert summary["counts"]["error"] == 2 and summary["counts"]["warning"] == 1
    details = client.get(url, params={"details": 1}).json()
    assert details["counts"] == summary["counts"]
    assert details["total"] == 3 and details["truncated"] is False
    first = details["items"][0]
    assert first["line"] == 3 and first["character"] == 0
    assert first["column"] == 1 and first["end_column"] == 6
    assert first["source"] == "ts"


def test_diagnostic_details_filter_limit_and_legacy_file_detail(diagnostics_fixture):
    client, project, _ = diagnostics_fixture
    url = f"/api/v1/code/projects/{project['id']}/diagnostics"
    filtered = client.get(url, params={"details": 1, "severity": 1, "limit": 1}).json()
    assert len(filtered["items"]) == 1 and filtered["total"] == 2 and filtered["truncated"] is True
    assert filtered["items"][0]["severity"] == 1
    single_file = client.get(url, params={"details": 1, "path": "src/a.js", "severity": 2}).json()
    assert single_file["total"] == 1 and single_file["items"][0]["column"] == 5
    assert single_file["files_detail"]["src/a.js"] == single_file["items"]
    legacy = client.get(url, params={"path": "src/a.js", "severity": 2}).json()
    assert legacy["items"] == [] and len(legacy["files_detail"]["src/a.js"]) == 1
    assert legacy["files_detail"]["src/a.js"][0]["severity"] == 2
    assert client.get(url, params={"details": 1, "severity": 8}).status_code == 422


def test_diagnostic_details_are_owner_scoped_and_do_not_scan(diagnostics_fixture, monkeypatch):
    client, project, intel = diagnostics_fixture
    monkeypatch.setattr(intel, "scan", lambda *args, **kwargs: pytest.fail("诊断读取不得扫描"))
    monkeypatch.setattr(intel, "ensure_indexed", lambda *args, **kwargs: pytest.fail("诊断读取不得索引"))
    url = f"/api/v1/code/projects/{project['id']}/diagnostics"
    assert client.get(url, params={"details": 1}).status_code == 200
    client.app.dependency_overrides[get_current_user] = lambda: CurrentUser("other", "other", "user")
    assert client.get(url, params={"details": 1}).status_code == 404
