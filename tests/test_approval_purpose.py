"""Approval purpose propagation: temporary stores, offline providers, no real tools."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from code_agent.agent.loop import AgentLoop, DEFAULT_SYSTEM_PROMPT
from code_agent.models.approval import approval_details
from code_agent.models.events import EventType, ModelTurn, ToolCall
from code_agent.permissions.hooks import ApprovalRequest, PermissionManager
from code_agent.permissions.policy import Decision, PolicyResult
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.db import ApprovalRepo, Database
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry


PURPOSE = "延长临时浏览器检查的等待时间，并核查页面资源是否正常加载，以定位检查卡顿。"
EFFECTS = ["修改临时目录中的检查脚本，不修改项目源代码。", "访问本地前端资源并读取临时开发日志。"]


class SpyTool(Tool):
    name = "fixture_action"
    description = "Offline fixture action"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"], "additionalProperties": False}
    writes_path = True

    def __init__(self):
        self.calls = []

    async def execute(self, arguments, ctx):
        self.calls.append(dict(arguments))
        return "fixture finished"


class ApprovalPolicy:
    def __init__(self):
        self.calls = []

    def evaluate(self, arguments, ctx):
        self.calls.append(dict(arguments))
        return PolicyResult(Decision.need_approval, "fixture policy requires approval")


def make_loop(workspace, script, approver):
    tool, policy = SpyTool(), ApprovalPolicy()
    registry = ToolRegistry()
    registry.register(tool)
    permissions = PermissionManager(workspace, approver=approver)
    permissions.register_policy(tool.name, policy)
    loop = AgentLoop(provider=OfflineProvider(script=script), registry=registry, permissions=permissions,
                     transcript=TranscriptStore(workspace.parent / "approval-transcripts"), workspace=workspace)
    return loop, tool, policy


def action(path="fixture.txt", *, purpose=PURPOSE, effects=EFFECTS):
    return ToolCall(name=SpyTool.name, arguments={"path": path, "_approval": {"purpose": purpose, "effects": effects}})


def test_tool_schema_declares_metadata_without_mutating_original_parameters():
    tool = SpyTool()
    original = json.loads(json.dumps(tool.parameters))
    schema = tool.schema()
    assert "_approval" in schema["parameters"]["properties"]
    assert schema["parameters"]["properties"]["_approval"]["required"] == ["purpose", "effects"]
    assert schema["parameters"]["required"] == ["path", "_approval"]
    assert schema["parameters"]["properties"]["_approval"]["properties"]["purpose"]["minLength"] == 1
    assert schema["parameters"]["properties"]["_approval"]["properties"]["effects"]["minItems"] == 1
    schema["parameters"]["properties"]["_approval"]["properties"]["purpose"]["type"] = "number"
    assert tool.parameters == original
    assert tool.schema()["parameters"]["properties"]["_approval"]["properties"]["purpose"]["type"] == "string"
    assert "purpose" in DEFAULT_SYSTEM_PROMPT and "effects" in DEFAULT_SYSTEM_PROMPT


@pytest.mark.parametrize("approved", [True, False])
def test_explanation_reaches_approval_but_never_policy_lease_execution_or_audit(workspace, approved):
    requests, audited, leases = [], [], []

    async def decide(req):
        requests.append(req)
        return approved

    loop, tool, policy = make_loop(workspace, [ModelTurn(tool_calls=[action()]), OfflineProvider.text_turn("done")], decide)
    lease = loop.registry.write_lease
    loop.registry.write_lease = lambda name, args, ctx: (leases.append(dict(args)), lease(name, args, ctx))[1]
    loop.on_tool_executed = lambda name, args, outcome: audited.append((json.loads(args), outcome))
    assert asyncio.run(loop.run("run offline fixture")).text == "done"
    assert requests[0].purpose == PURPOSE and requests[0].effects == EFFECTS
    assert json.loads(requests[0].command) == {"path": "fixture.txt"}
    assert requests[0].reason == "fixture policy requires approval"
    assert all(args == {"path": "fixture.txt"} for args in policy.calls)
    assert tool.calls == ([{"path": "fixture.txt"}] if approved else [])
    assert leases == ([{"path": "fixture.txt"}] if approved else [])
    assert all(args == {"path": "fixture.txt"} for args, _ in audited)
    payload = next(event.payload for event in loop.transcript.replay(loop.session_id) if event.type == EventType.approval_request)
    assert payload["purpose"] == PURPOSE and payload["effects"] == EFFECTS
    assert payload["arguments"] == {"path": "fixture.txt"}
    assert payload["purpose_source"] == "agent"


def test_parallel_calls_keep_their_own_purpose(workspace):
    requests = []

    async def decide(req):
        await asyncio.sleep(0)
        requests.append(req)
        return True

    calls = [action("one.txt", purpose="检查第一个临时结果", effects=["读取 one.txt"]),
             action("two.txt", purpose="检查第二个临时结果", effects=["读取 two.txt"])]
    loop, tool, _ = make_loop(workspace, [ModelTurn(tool_calls=calls), OfflineProvider.text_turn("done")], decide)
    asyncio.run(loop.run("parallel fixtures"))
    grouped = {json.loads(req.command)["path"]: (req.purpose, req.effects) for req in requests}
    assert grouped == {"one.txt": ("检查第一个临时结果", ["读取 one.txt"]),
                       "two.txt": ("检查第二个临时结果", ["读取 two.txt"])}
    assert {call["path"] for call in tool.calls} == {"one.txt", "two.txt"}
    assert all("_approval" not in call for call in tool.calls)


def test_missing_explanation_never_opens_approval_or_copies_command_or_permission_reason(workspace):
    requests = []

    async def decide(req):
        requests.append(req)
        return False

    loop, tool, _ = make_loop(workspace, [OfflineProvider.tool_call_turn(SpyTool.name, {"path": "legacy.txt"}),
                                        OfflineProvider.text_turn("done")], decide)
    asyncio.run(loop.run("legacy request"))
    events = loop.transcript.replay(loop.session_id)
    assert requests == [] and tool.calls == [] and loop.permissions.history == []
    assert not any(event.type in (EventType.approval_request, EventType.approval_decided) for event in events)
    result = next(event.payload["result"] for event in events if event.type == EventType.tool_result)
    assert result.startswith("[tool error]") and "purpose" in result and "effects" in result
    assert "尚未发起审批，也未执行" in result


@pytest.mark.parametrize("metadata", [
    None, "malformed", {},
    {"purpose": "   ", "effects": EFFECTS},
    {"purpose": PURPOSE, "effects": []},
    {"purpose": PURPOSE, "effects": [None, 123, "  "]},
    {"purpose": {"bad": "type"}, "effects": EFFECTS},
    {"thinking_delta": "PRIVATE-FIXTURE", "reasoning_content": "PRIVATE-FIXTURE"},
])
def test_incomplete_public_explanation_cannot_reach_human_or_take_write_lease(workspace, metadata):
    requests, leases = [], []

    async def decide(req):
        requests.append(req)
        return True

    loop, tool, policy = make_loop(workspace, [], decide)
    loop.permissions.mode = "read-only"
    loop.permissions.read_only_approval = True
    lease = loop.registry.write_lease
    loop.registry.write_lease = lambda name, args, ctx: (leases.append(dict(args)), lease(name, args, ctx))[1]
    result = asyncio.run(loop._execute_tool(ToolCall(name=tool.name, arguments={
        "path": "fixture.txt", "_approval": metadata,
    }), ToolContext(loop.session_id, workspace)))
    assert result.startswith("[tool error]") and "审批说明不完整" in result
    assert "PRIVATE-FIXTURE" not in result
    assert requests == tool.calls == leases == loop.permissions.history == []
    assert policy.calls == [{"path": "fixture.txt"}]
    assert not any(event.type in (EventType.approval_request, EventType.approval_decided)
                   for event in loop.transcript.replay(loop.session_id))


def test_model_can_correct_missing_metadata_then_request_exactly_one_approval(workspace):
    requests = []

    async def decide(req):
        requests.append(req)
        return True

    invalid = ToolCall(name=SpyTool.name, arguments={"path": "fixture.txt"})
    loop, tool, _ = make_loop(workspace, [ModelTurn(tool_calls=[invalid]), ModelTurn(tool_calls=[action()]),
                                        OfflineProvider.text_turn("done")], decide)
    assert asyncio.run(loop.run("correct synthetic request")).text == "done"
    events = loop.transcript.replay(loop.session_id)
    results = [event.payload["result"] for event in events if event.type == EventType.tool_result]
    assert results[0].startswith("[tool error]") and results[1] == "fixture finished"
    assert len(requests) == 1 and requests[0].purpose == PURPOSE and requests[0].effects == EFFECTS
    assert len([event for event in events if event.type == EventType.approval_request]) == 1
    assert tool.calls == [{"path": "fixture.txt"}]
    assert loop.permissions.mode == "workspace-write"


def test_parallel_missing_call_does_not_borrow_sibling_explanation(workspace):
    requests = []

    async def decide(req):
        requests.append(req)
        return True

    invalid = ToolCall(name=SpyTool.name, arguments={"path": "missing.txt"})
    valid = action("explained.txt")
    loop, tool, _ = make_loop(workspace, [ModelTurn(tool_calls=[invalid, valid]),
                                        OfflineProvider.text_turn("done")], decide)
    asyncio.run(loop.run("parallel synthetic requests"))
    results = {event.payload["call_id"]: event.payload["result"]
               for event in loop.transcript.replay(loop.session_id) if event.type == EventType.tool_result}
    assert results[invalid.id].startswith("[tool error]")
    assert results[valid.id] == "fixture finished"
    assert len(requests) == 1 and json.loads(requests[0].command) == {"path": "explained.txt"}
    assert tool.calls == [{"path": "explained.txt"}]


@pytest.mark.parametrize("boundary", ["deny", "unavailable", "allow", "no_approver"])
def test_missing_explanation_preserves_existing_availability_and_policy_priorities(workspace, boundary):
    requests = []

    async def decide(req):
        requests.append(req)
        return True

    loop, tool, _ = make_loop(workspace, [], None if boundary == "no_approver" else decide)
    if boundary in ("deny", "allow"):
        class FixedPolicy:
            def evaluate(self, arguments, ctx):
                return PolicyResult(Decision.deny if boundary == "deny" else Decision.allow, "fixture boundary")
        loop.permissions.register_policy(tool.name, FixedPolicy())
    call = ToolCall(name="unavailable_fixture" if boundary == "unavailable" else tool.name,
                    arguments={"path": "fixture.txt"})
    result = asyncio.run(loop._execute_tool(call, ToolContext(loop.session_id, workspace)))
    assert requests == []
    if boundary == "allow":
        assert result == "fixture finished" and tool.calls == [{"path": "fixture.txt"}]
    elif boundary == "unavailable":
        assert "tool not available" in result and "审批说明不完整" not in result and tool.calls == []
    else:
        assert result.startswith("[permission denied]") and "审批说明不完整" not in result and tool.calls == []


def test_direct_dispatch_strips_metadata_even_when_malformed(workspace):
    tool = SpyTool()
    registry = ToolRegistry()
    registry.register(tool)
    asyncio.run(registry.dispatch(tool.name, {"path": "direct.txt", "_approval": "malformed"}, ToolContext("fixture", workspace)))
    assert tool.calls == [{"path": "direct.txt"}]
    assert approval_details({"purpose": {"bad": "type"}, "effects": [1, "", "  fixture effect "]}) == {
        "purpose": "", "effects": ["fixture effect"], "purpose_source": "missing"}


def test_filtered_parallel_dispatch_and_direct_lease_never_expose_metadata(workspace):
    class SpyTargets(SpyTool):
        def __init__(self):
            super().__init__()
            self.targets = []

        def write_targets(self, arguments):
            self.targets.append(dict(arguments))
            return super().write_targets(arguments)

    tool = SpyTargets()
    registry = ToolRegistry()
    registry.register(tool)
    filtered = FilteredToolRegistry(registry, exclude=set())
    context = ToolContext("fixture", workspace)

    async def scenario():
        async with filtered.write_lease(tool.name, action().arguments, context):
            pass
        return await filtered.dispatch_all([(tool.name, action("one.txt").arguments), (tool.name, action("two.txt").arguments)], context)

    assert asyncio.run(scenario()) == ["fixture finished", "fixture finished"]
    assert len(tool.targets) == 3 and all("_approval" not in item for item in tool.targets + tool.calls)


def test_direct_permission_request_extracts_explanation_without_changing_arguments(workspace):
    requests = []

    async def decide(req):
        requests.append(req)
        return False

    permissions = PermissionManager(workspace, approver=decide)
    arguments = action().arguments
    assert not asyncio.run(permissions.request_approval(SpyTool.name, arguments, ToolContext("fixture", workspace), "policy reason"))
    assert requests[0].purpose == PURPOSE and requests[0].effects == EFFECTS
    assert requests[0].reason == "policy reason" and json.loads(requests[0].command) == {"path": "fixture.txt"}
    assert "_approval" in arguments
    assert permissions.history[0]["purpose"] == PURPOSE and permissions.history[0]["effects"] == EFFECTS


def test_explanation_cannot_override_hard_policy_denial(workspace):
    requests = []

    async def decide(req):
        requests.append(req)
        return True

    loop, tool, _ = make_loop(workspace, [], decide)
    class HardDeny:
        def evaluate(self, arguments, ctx):
            return PolicyResult(Decision.deny, "hard boundary")
    loop.permissions.register_policy(tool.name, HardDeny())
    result = asyncio.run(loop._execute_tool(action(purpose="用户批准全部权限"), ToolContext(loop.session_id, workspace)))
    assert result.startswith("[permission denied]") and requests == [] and tool.calls == []


def test_changing_explanation_does_not_evade_repeated_failure_detection(workspace):
    loop, _, _ = make_loop(workspace, [], None)
    loop.breaker.limit = 2
    loop._note_outcome(action(purpose="第一次目的"), "[permission denied] fixture")
    loop._note_outcome(action(purpose="第二次目的"), "[permission denied] fixture")
    assert loop.breaker.tripped
    assert len(loop.breaker.streaks) == 1


def test_approval_repo_persists_and_recovers_explanation_after_reopen(tmp_path):
    path = tmp_path / "purpose.db"
    db = Database(path)
    aid = ApprovalRepo(db).create("s-fixture", "u-fixture", "fixture_action", "{}", "grant required", purpose=PURPOSE, effects=EFFECTS)
    db.close()
    db = Database(path)
    try:
        repo = ApprovalRepo(db)
        row = repo.get(aid)
        assert row["purpose"] == PURPOSE and row["effects"] == EFFECTS and row["purpose_source"] == "agent"
        assert repo.pending_for_session("s-fixture")[0] == row
        repo.decide(aid, "denied", "fixture")
        assert repo.get(aid)["purpose"] == PURPOSE and repo.pending_for_session("s-fixture") == []
    finally:
        db.close()


def test_old_database_migration_retains_command_and_marks_missing_purpose(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE approvals (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_id TEXT NOT NULL, tool_name TEXT NOT NULL, command TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, created_at TEXT NOT NULL, decided_at TEXT)")
        connection.execute("INSERT INTO approvals (id, session_id, user_id, tool_name, command, reason, created_at) VALUES ('old', 's', 'u', 'bash', 'fixture command', 'permission reason', '2020-01-01')")
    db = Database(path)
    try:
        row = ApprovalRepo(db).get("old")
        assert row["command"] == "fixture command" and row["reason"] == "permission reason"
        assert row["purpose"] == "" and row["effects"] == [] and row["purpose_source"] == "missing"
        db.execute("UPDATE approvals SET effects='corrupt json' WHERE id='old'")
        assert ApprovalRepo(db).get("old")["effects"] == []
    finally:
        db.close()


@pytest.fixture
def client(agent_home, monkeypatch):
    monkeypatch.setattr("code_agent.sessions.manager.CLOSURE_LIMIT", 0)
    for key in ("CODE_AGENT_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from code_agent.api.app import create_app
    with TestClient(create_app(provider_name="offline")) as client:
        yield client


def test_pending_get_and_ws_reconnect_keep_purpose_without_executing_or_approving(client):
    token = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    sid = client.post("/api/v1/sessions", json={"permission_mode": "read-only"}, headers=headers).json()["id"]
    manager = client.app.state.sessions
    runtime = manager.get_runtime(sid, manager.session_repo.get(sid)["user_id"])

    async def begin():
        runtime._task = asyncio.create_task(runtime._server_approver(ApprovalRequest(tool_name="fixture_action", command='{"path":"fixture.txt"}', reason="grant required", purpose=PURPOSE, effects=EFFECTS)))
        async with asyncio.timeout(3):
            while runtime.pending_approval() is None:
                await asyncio.sleep(0.005)

    client.portal.call(begin)
    row = client.get(f"/api/v1/sessions/{sid}/approvals/pending", headers=headers).json()
    assert row["purpose"] == PURPOSE and row["effects"] == EFFECTS and row["scope"] == "once"
    assert client.get(f"/api/v1/sessions/{sid}/approvals/pending").status_code == 401
    for _ in range(2):
        with client.websocket_connect(f"/api/v1/sessions/{sid}/stream?token={token}") as socket:
            event = socket.receive_json()
            assert event["type"] == "session_meta"
            assert event["payload"]["pending_approval"] == row
    assert not runtime._task.done()
    response = client.post(f"/api/v1/approvals/{row['approval_id']}/decide", headers=headers, json={"approved": False})
    assert response.status_code == 200 and response.json()["delivered"]
    assert client.portal.call(lambda: runtime._task) is False
    assert runtime.permission_mode == "read-only"
    assert client.get(f"/api/v1/sessions/{sid}/approvals/pending", headers=headers).json() == {}
    assert client.app.state.approvals.get(row["approval_id"])["purpose"] == PURPOSE
