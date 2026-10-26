"""Retired workflow entry points stay removed without damaging ordinary tasks."""

import asyncio

from code_agent.agent.project_team import ProjectTeam
from code_agent.api.app import _clean_code_context
from code_agent.tools.builtin.agent_team_tools import DelegateAgentTool
from test_project_team_integration import integration, roster, worker


def test_workflow_routes_and_new_storage_are_removed_while_tasks_stay_available(integration):
    client, app, auth, project, _ = integration
    child = worker(integration)
    path = f"/api/v1/code/projects/{project['id']}/workflow"
    for suffix in ("", "?tree=task:missing"):
        assert client.get(path + suffix, headers=auth).status_code == 404
        assert client.put(path + suffix, headers=auth, json={"expected_revision": 0, "workflow": {}}).status_code == 404
    assert not hasattr(app.state, "sop_workflow_store")
    assert app.state.db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='sop_workflows'") is None
    assert [row["session_id"] for row in roster(integration)["agents"]] == [child["session_id"]]
    assert client.get(f"/api/v1/code/projects/{project['id']}/work-items", headers=auth).status_code == 200
    assert "node_id" not in DelegateAgentTool.parameters["properties"]


def test_deprecated_node_context_is_ignored_but_code_coordinates_are_preserved():
    assert _clean_code_context({"code_context": {"workflow_node_id": "implementation", "workflow_tree_id": "main"}}) is None
    assert _clean_code_context({"code_context": {"workflow_node_id": "implementation", "workflow_tree_id": "main",
                                               "path": "src/app.py", "start": 4, "node_id": "ast:call"}}) == {
        "path": "src/app.py", "start": 4, "node_id": "ast:call"}


def test_team_initialization_preserves_legacy_workflow_storage_and_existing_conversations(integration):
    _, app, _, project, _ = integration
    root_id = roster(integration)["lead"]["session_id"]
    child = worker(integration)
    app.state.db.execute("CREATE TABLE sop_workflows (project_id TEXT, document_json TEXT)")
    app.state.db.execute("INSERT INTO sop_workflows VALUES (?,?)", (project["id"], '{"legacy":"kept"}'))
    team = ProjectTeam(app.state.sessions)
    try:
        assert team.sync(project["id"], project["user_id"])["lead"]["session_id"] == root_id
        assert team.descriptor(child["session_id"])["state"] == "idle"
        assert app.state.db.query_one("SELECT document_json FROM sop_workflows")["document_json"] == '{"legacy":"kept"}'
    finally:
        asyncio.run(team.aclose())
