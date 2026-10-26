"""Model/API delegation inputs and status projections retain the task contract."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from code_agent.api.agent_team_api import mount_agent_team_api
from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.agent_team_tools import (
    DelegateAgentTool, FollowupAgentTool, _model_team_response, _serialize_model_team_response,
)


def recorder():
    calls = []

    async def dispatch(*args, **kwargs):
        calls.append((args, kwargs))
        return {"session_id": "child", "activation_id": 2, "state": "queued"}

    return SimpleNamespace(start=dispatch, followup=dispatch), calls


def test_model_schema_requires_explicit_new_scope_and_reason_but_followup_inherits_scope():
    assert set(DelegateAgentTool.parameters["required"]) == {"prompt", "write_paths", "delegation_reason"}
    assert set(FollowupAgentTool.parameters["required"]) == {"session_id", "prompt", "delegation_reason"}
    for tool in (DelegateAgentTool, FollowupAgentTool):
        properties = tool.parameters["properties"]
        assert properties["write_paths"]["maxItems"] == 64
        assert properties["depends_on"]["maxItems"] == 16
        assert properties["write_paths"].get("minItems", 0) == 0


@pytest.mark.parametrize("patch", [
    {"write_paths": None}, {"write_paths": "src/a.py"}, {"write_paths": [2]},
    {"write_paths": [""]}, {"write_paths": ["a" * 1025]}, {"write_paths": ["a"] * 65},
    {"delegation_reason": " "}, {"delegation_reason": 3}, {"delegation_reason": "x" * 2001},
    {"depends_on": None}, {"depends_on": "child"}, {"depends_on": [""]},
    {"depends_on": [3]}, {"depends_on": ["a" * 65]}, {"depends_on": ["a"] * 17},
])
def test_model_rejects_bad_contract_before_any_dispatch(tmp_path, patch):
    team, calls = recorder()
    arguments = {"prompt": "independent work", "write_paths": [], "delegation_reason": "independent evidence", **patch}
    with pytest.raises(ToolError):
        asyncio.run(DelegateAgentTool(team).execute(arguments, ToolContext("lead", tmp_path, "owner")))
    assert calls == []


@pytest.mark.parametrize("field", ["write_paths", "delegation_reason"])
def test_missing_new_delegate_contract_has_no_side_effect(tmp_path, field):
    team, calls = recorder()
    arguments = {"prompt": "work", "write_paths": [], "delegation_reason": "independent output"}
    arguments.pop(field)
    with pytest.raises(ToolError):
        asyncio.run(DelegateAgentTool(team).execute(arguments, ToolContext("lead", tmp_path, "owner")))
    assert not calls


@pytest.mark.parametrize("caller", ["lead", "recursive_worker"])
@pytest.mark.parametrize("paths", [[], ["src/a.py", "docs/"], ["."]])
def test_delegate_forwards_actor_scope_reason_and_dependency_contract(tmp_path, caller, paths):
    team, calls = recorder()
    arguments = {"prompt": "bounded task", "parent_session_id": "existing_parent", "write_paths": paths,
                 "delegation_reason": "separate deliverable can progress alongside implementation", "depends_on": ["dependency"]}
    result = asyncio.run(DelegateAgentTool(team).execute(arguments, ToolContext(caller, tmp_path, "owner")))
    assert json.loads(result)["session_id"] == "child"
    args, options = calls[0]
    assert args == (caller, "owner", "bounded task")
    assert options["parent_id"] == "existing_parent"
    assert options["write_paths"] == paths
    assert options["delegation_reason"] == arguments["delegation_reason"]
    assert options["depends_on"] == ["dependency"]


def test_followup_requires_reason_and_preserves_omitted_scope_or_explicit_clear(tmp_path):
    team, calls = recorder()
    tool, ctx = FollowupAgentTool(team), ToolContext("parent", tmp_path, "owner")
    with pytest.raises(ToolError):
        asyncio.run(tool.execute({"session_id": "child", "prompt": "continue"}, ctx))
    assert not calls
    base = {"session_id": "child", "prompt": "continue", "delegation_reason": "reuse verified context"}
    asyncio.run(tool.execute(base, ctx))
    assert calls[-1][0] == ("parent", "owner", "child", "continue")
    assert calls[-1][1]["write_paths"] is None
    assert calls[-1][1]["depends_on"] is None
    asyncio.run(tool.execute({**base, "write_paths": [], "depends_on": []}, ctx))
    assert calls[-1][1]["write_paths"] == []
    assert calls[-1][1]["depends_on"] == []


def api_client():
    team, calls = recorder()
    row = {"session_id": "child", "project_id": "project", "user_id": "owner", "lead_session_id": "lead"}
    team.descriptor = lambda sid: row if sid == "child" else None
    team._row = lambda sid: row if sid == "child" else None
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("owner", "owner", "user")
    mount_agent_team_api(app, team=team, service=SimpleNamespace(get_owned=lambda user, pid: {}))
    return TestClient(app), calls


def test_http_followup_omission_preserves_previous_contract_and_explicit_empty_arrays_clear():
    client, calls = api_client()
    path = "/api/v1/code/projects/project/agents/child/followup"
    with client:
        assert client.post(path, json={"prompt": "continue"}).status_code == 200
        assert calls[-1] == (("lead", "owner", "child"), {"prompt": "continue"})
        contract = {"prompt": "continue", "write_paths": [], "depends_on": [], "delegation_reason": "reuse context"}
        assert client.post(path, json=contract).status_code == 200
        assert calls[-1][1] == contract
        contract = {"prompt": "continue", "write_paths": ["src/"], "depends_on": ["dependency"]}
        assert client.post(path, json=contract).status_code == 200
        assert calls[-1][1] == contract


@pytest.mark.parametrize("patch", [{"write_paths": [4]}, {"write_paths": ["a"] * 65}, {"depends_on": ["a"] * 17},
                                    {"depends_on": [False]}, {"delegation_reason": 7}])
def test_http_rejects_bad_contract_without_calling_team(patch):
    client, calls = api_client()
    with client:
        response = client.post("/api/v1/code/projects/project/agents/child/followup", json={"prompt": "continue", **patch})
    assert response.status_code == 422
    assert not calls


def contract_row(index):
    return {"session_id": f"s_{index:022d}", "activation_id": index + 1, "state": "queued", "phase": "queued",
            "node_id": f"n{index:03d}" + "a" * 60, "result": "report" * 10000,
            "write_paths": [] if index % 2 else [f"src/file_{index}.py", "docs/"],
            "scope_mode": "explicit", "delegation_reason": "independent deliverable " * 30,
            "file_ownership": {"state": "waiting", "blockers": ["s_owner"]},
            "depends_on": [{"session_id": "s_dependency", "activation_id": 7}]}


CONTRACT = ("write_paths", "scope_mode", "delegation_reason", "file_ownership", "depends_on")


@pytest.mark.parametrize("count", [1, 64])
def test_normal_compact_and_table_receipts_preserve_exact_contract(count):
    rows = [contract_row(index) for index in range(count)]
    before = deepcopy(rows)
    projected = _model_team_response({"agents": rows}, "wait_agents")
    decoded = ([dict(zip(projected["agent_columns"], row)) for row in projected["agents"]]
               if "agent_columns" in projected else projected["agents"])
    assert len(decoded) == count
    for original, output in zip(rows, decoded):
        assert all(output[key] == original[key] for key in CONTRACT)
    assert rows == before


@pytest.mark.parametrize("single", [False, True])
def test_oversized_contract_keeps_complete_authority_and_bound_dependencies_in_explicit_cache(agent_home, single):
    rows = [contract_row(index) for index in range(64)]
    if single:
        rows = [contract_row(0)]
        rows[0]["write_paths"] = [f"directory_{i}/" + "a" * 980 for i in range(64)]
    original = rows[0] if single else {"agents": rows}
    encoded = _serialize_model_team_response(original, "delegate_agent" if single else "team_list", ToolContext("lead", agent_home))
    summary = json.loads(encoded)
    assert summary["contract_fields"] == list(CONTRACT)
    full = json.loads(Path(summary["full_roster_path"]).read_text(encoding="utf-8"))
    decoded = ([full] if single else [dict(zip(full["agent_columns"], row)) for row in full["agents"]])
    assert len(decoded) == len(rows)
    assert all(output[key] == source[key] for source, output in zip(rows, decoded) for key in CONTRACT)
