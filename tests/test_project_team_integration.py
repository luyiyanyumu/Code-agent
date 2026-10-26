"""Actual app/session/loop integration, with deterministic local providers only."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import _clean_code_context, create_app
from code_agent.models.events import ModelTurn, TokenUsage, ToolCall
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionClosed, TEAM_TOOL_NAMES
from code_agent.tools.base import Tool, ToolContext, ToolError


@pytest.fixture
def integration(agent_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_CODE_WATCH", "0")
    monkeypatch.setattr("code_agent.sessions.manager.MAX_CONSECUTIVE_WAKES", 0)
    async def no_embedding_probe():
        return {"found": False}
    monkeypatch.setattr("code_agent.retrieval.embed.autodetect_local", no_embedding_probe)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.txt").write_text("original", encoding="utf-8")
    app = create_app(provider_name="offline")
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        auth = {"Authorization": "Bearer " + login.json()["token"]}
        view = client.post("/api/v1/code/projects", headers=auth, json={"mode": "open", "path": str(root)}).json()
        project = app.state.projects.get(view["id"])
        yield client, app, auth, project, root


def roster(env):
    client, _, auth, project, _ = env
    response = client.get(f"/api/v1/code/projects/{project['id']}/agents", headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def worker(env, root_id=None, *, title="测试后台任务"):
    """Explicit test identity; roster/workflow reads never allocate workers."""
    _, app, _, project, _ = env
    team = app.state.project_agent_team
    lead = team._row(root_id or roster(env)["lead"]["session_id"])
    return team._create_worker(project, lead, title=title, tree="main", role="subagent")


def conversation_root(env, kind):
    if kind == "main":
        return roster(env)["lead"]["session_id"]
    client, _, auth, project, _ = env
    response = client.post(f"/api/v1/code/projects/{project['id']}/conversations", headers=auth, json={})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_app_mounts_settings_without_precreating_branches_or_writing(integration):
    client, app, auth, project, root = integration
    first = roster(integration)
    second = roster(integration)
    assert first == second
    assert first["agents"] == []
    assert first["lead"]["session_id"] == app.state.projects.get(project["id"])["session_id"]
    assert all(agent["state"] == "idle" for agent in first["agents"])
    assert app.state.db.query_all("SELECT * FROM project_agent_runs") == []
    assert list(root.iterdir()) == [root / "source.txt"]
    assert client.get("/api/v1/settings/agent-team", headers=auth).json() == {"max_depth": 3, "max_parallel": 4}
    assert client.put("/api/v1/settings/agent-team", headers=auth, json={"max_depth": 2, "max_parallel": 1}).status_code == 200
    assert roster(integration)["limits"] == {"max_depth": 2, "max_parallel": 1}


def test_successful_workflow_save_does_not_allocate_or_retire_dynamic_workers(integration):
    client, app, auth, project, _ = integration
    first = roster(integration)
    child = worker(integration)
    document = app.state.sop_workflow_store.get(project["id"], "main")
    document.pop("revision"); document.pop("updated_at")
    document["nodes"].append({**document["nodes"][0], "id": "custom-check", "type": "custom", "title": "独立检查"})
    path = f"/api/v1/code/projects/{project['id']}/workflow"
    response = client.put(path, headers=auth, json={"expected_revision": 0, "workflow": document})
    assert response.status_code == 200, response.text
    row = app.state.db.query_one("SELECT * FROM project_agents WHERE project_id=? AND node_id='custom-check'", (project["id"],))
    assert row is None
    document["nodes"] = document["nodes"][:-1]
    assert client.put(path, headers=auth, json={"expected_revision": 1, "workflow": document}).status_code == 200
    assert app.state.project_agent_team.descriptor(child["session_id"])["state"] == "idle"
    assert app.state.db.query_all("SELECT * FROM project_agent_runs") == []
    assert first["agents"] == []
    assert [row["session_id"] for row in roster(integration)["agents"]] == [child["session_id"]]


def test_lead_execution_tools_keep_disabled_tools_and_plugin_permissions(integration, monkeypatch):
    from code_agent.permissions.policy import Decision

    _, app, _, project, root = integration
    lead = roster(integration)["lead"]["session_id"]
    app.state.projects.set_flags(project["id"], allow_direct_write=True)
    app.state.sessions.set_permission_mode(lead, project["user_id"], "workspace-write")

    class PluginMutator(Tool):
        name = "unknown_plugin_mutator"
        async def execute(self, arguments, ctx):
            raise AssertionError("Unapproved plugin must not execute")

    app.state.registry.register(PluginMutator())
    monkeypatch.setattr("code_agent.sessions.manager.disabled_tools", lambda: ["bash_persistent"])
    runtime = app.state.sessions.get_runtime(lead, project["user_id"])
    runtime.web_enabled = False
    prompt, registry = runtime._loop_context()
    assert "当前对话的协调者" in prompt
    assert TEAM_TOOL_NAMES <= set(registry.names())
    for name in ("write_file", "edit_file", "edit_many", "bash", "propose_edit", "unknown_plugin_mutator"):
        assert registry.get(name) is not None
    for name in ("bash_persistent", "web_search", "web_fetch"):
        assert registry.get(name) is None
        with pytest.raises(ToolError):
            asyncio.run(registry.dispatch(name, {}, ToolContext(lead, root, runtime.user_id)))
    permissions = runtime._make_permissions()
    ctx = ToolContext(lead, root, project["user_id"])
    assert asyncio.run(permissions.evaluate("write_file", {"path": "source.txt"}, ctx)).decision is Decision.allow
    assert asyncio.run(permissions.evaluate("unknown_plugin_mutator", {}, ctx)).decision is Decision.need_approval
    assert asyncio.run(permissions.evaluate("write_file", {"path": "../outside.txt"}, ctx)).decision is Decision.deny
    assert (root / "source.txt").read_text() == "original"


@pytest.mark.parametrize("root_kind", ["main", "chat"])
def test_lead_directly_edits_verifies_and_delivers_without_delegation(integration, monkeypatch, root_kind):
    client, app, auth, project, root = integration
    lead = conversation_root(integration, root_kind)
    app.state.projects.set_flags(project["id"], allow_direct_write=True)
    app.state.sessions.set_permission_mode(lead, project["user_id"], "workspace-write")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    command = "python -c \"from pathlib import Path; assert Path('source.txt').read_text() == 'delivered'; print('direct verification passed')\""
    provider = OfflineProvider(script=[
        OfflineProvider.tool_call_turn("read_file", {"path": "source.txt", "offset": 1, "limit": 5}),
        OfflineProvider.tool_call_turn("edit_file", {"path": "source.txt", "old_string": "original", "new_string": "delivered"}),
        OfflineProvider.tool_call_turn("bash", {"command": command}),
        OfflineProvider.text_turn("已修改 source.txt，相关验证通过。"),
    ])
    app.state.router.resolve_default = lambda: provider
    response = client.post(f"/api/v1/sessions/{lead}/prompt", headers=auth, json={"text": "修改 source.txt 后验证并交付"})
    assert response.status_code == 200, response.text
    assert (root / "source.txt").read_text() == "delivered"
    events = app.state.transcript.replay(lead)
    results = [event.payload for event in events if event.type.value == "tool_result"]
    assert [item["name"] for item in results] == ["read_file", "edit_file", "bash"]
    assert "direct verification passed" in results[-1]["result"]
    assert "exit_code=0" in results[-1]["result"]
    assert provider.calls == 4, "Verified direct delivery needs no additional review turn"
    assert not any(event.type.value == "approval_request" for event in events)
    assert all(row["activation_id"] == 0 for row in roster(integration)["agents"])
    audit = [row for row in app.state.audit_repo.list_entries() if row["session_id"] == lead]
    assert [(row["tool_name"], row["outcome"]) for row in audit] == [
        ("read_file", "ok"), ("edit_file", "ok"), ("bash", "ok"),
    ]
    assert app.state.audit_repo.verify()["ok"]


def test_read_only_lead_approval_is_single_use_and_denial_is_logged(integration):
    _, app, _, project, root = integration
    lead = roster(integration)["lead"]["session_id"]
    manager = app.state.sessions
    manager.set_permission_mode(lead, project["user_id"], "read-only")
    runtime = manager.get_runtime(lead, project["user_id"])
    provider = OfflineProvider(script=[
        OfflineProvider.tool_call_turn("read_file", {"path": "source.txt"}),
        *(OfflineProvider.tool_call_turn("write_file", {"path": "source.txt", "content": value, "_approval": {
            "purpose": "验证 Lead 单次授权", "effects": ["更新临时 source.txt"],
        }}) for value in ("approved once", "must stay denied")),
        OfflineProvider.tool_call_turn("write_file", {"path": "../outside.txt", "content": "must not escape"}),
        OfflineProvider.text_turn("第二次写入与越界操作被拒绝。"),
    ])
    app.state.router.resolve_default = lambda: provider

    async def verify():
        running = asyncio.create_task(runtime.submit("验证单次审批边界"))
        try:
            for approved in (True, False):
                async def next_approval():
                    while not any(not future.done() for future in runtime._pending_approvals.values()):
                        if running.done():
                            pytest.fail("Lead exited before requesting the expected approval")
                        await asyncio.sleep(0.01)
                    return next(key for key, future in runtime._pending_approvals.items() if not future.done())
                identity = await asyncio.wait_for(next_approval(), 5)
                assert runtime.decide_approval(identity, approved)
                # Allow the decided waiter to retire before looking for the next one.
                while identity in runtime._pending_approvals:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(running, 5)
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(verify())
    assert (root / "source.txt").read_text() == "approved once"
    assert not (root.parent / "outside.txt").exists()
    assert app.state.sessions_repo.get(lead)["permission_mode"] == "read-only"
    assert not app.state.projects.get(project["id"])["allow_direct_write"]
    decisions = app.state.db.query_all("SELECT status FROM approvals WHERE session_id=? ORDER BY created_at,id", (lead,))
    assert [row["status"] for row in decisions].count("approved") == 1
    assert [row["status"] for row in decisions].count("denied") == 1
    writes = [row for row in app.state.audit_repo.list_entries() if row["tool_name"] == "write_file"]
    assert [row["outcome"] for row in writes] == ["ok", "denied_approval", "denied"]
    assert app.state.audit_repo.verify()["ok"]


@pytest.mark.parametrize("prompt_text,route_kind", [("修复问题并测试", "edit"), ("验证构建结果", "verify")])
def test_final_model_prompt_routes_lead_to_execution_and_preserves_coordination(integration, prompt_text, route_kind):
    from code_agent.agent.routing import ROUTES
    from code_agent.codeintel.context import LEAD_PROTOCOL, PROTOCOL, session_suffix

    _, app, _, project, _ = integration
    manager = app.state.sessions
    members = roster(integration)
    lead_id = members["lead"]["session_id"]
    node_id = worker(integration)["session_id"]
    manager.set_permission_mode(lead_id, project["user_id"], "full-access")
    suffix = session_suffix(None, project) + "\n\n[项目地图] source.txt:1"
    captured = []

    class PromptInspected(Exception):
        """Stop at the provider boundary; this fixture does no implementation."""

    class InspectProvider(OfflineProvider):
        async def chat(self, messages, tools):
            captured.append((messages[0]["content"], {tool["name"] for tool in tools}))
            raise PromptInspected

    app.state.router.resolve_default = lambda: InspectProvider()

    async def verify():
        for session_id in (lead_id, node_id):
            runtime = manager.get_runtime(session_id, project["user_id"])
            runtime.code_context_provider = lambda: (1, suffix)
            # The edit route correctly rejects a text-only completion without
            # changes. This test owns prompt assembly, not a pretend delivery.
            with pytest.raises(PromptInspected):
                await runtime.submit(prompt_text)
            # Role-specific assembly must not rewrite the shared cached source.
            assert runtime.refresh_code_context() == suffix

    asyncio.run(verify())
    assert len(captured) == 2
    lead_prompt, lead_tools = captured[0]
    node_prompt, node_tools = captured[1]
    assert LEAD_PROTOCOL in lead_prompt and PROTOCOL not in lead_prompt
    assert PROTOCOL in node_prompt and LEAD_PROTOCOL not in node_prompt
    assert "[项目地图] source.txt:1" in lead_prompt and "[项目地图] source.txt:1" in node_prompt
    assert ROUTES[route_kind].hint in lead_prompt
    assert ROUTES[route_kind].hint in node_prompt
    assert {"delegate_agent", "wait_agents", "audit_agent"} <= lead_tools
    assert {"bash", "write_file", "propose_edit"} <= lead_tools
    assert {"bash", "write_file", "propose_edit"} <= node_tools
    assert "直接实施" in lead_prompt and "实际退出码" in lead_prompt
    assert "无需机械重跑" in lead_prompt


def test_ordinary_chat_excludes_team_tools_but_keeps_existing_tools(integration):
    client, app, auth, _, _ = integration
    meta = client.post("/api/v1/sessions", headers=auth, json={}).json()
    runtime = app.state.sessions.get_runtime(meta["id"], meta["user_id"])
    _, registry = runtime._loop_context()
    assert not TEAM_TOOL_NAMES.intersection(registry.names())
    assert registry.get("bash") and registry.get("write_file")
    assert runtime.agent_team is None


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "full-access"])
def test_lead_can_inspect_directly_in_every_permission_mode(integration, mode):
    from code_agent.permissions.policy import Decision

    _, app, _, project, root = integration
    lead = roster(integration)["lead"]["session_id"]
    manager = app.state.sessions
    manager.set_permission_mode(lead, project["user_id"], mode)
    runtime = manager.get_runtime(lead, project["user_id"])
    prompt, registry = runtime._loop_context()
    ctx = ToolContext(lead, root, project["user_id"])
    permissions = runtime._make_permissions()
    for name in ("read_file", "git_read", "read_agent_execution", "load_workspace_dependencies"):
        assert registry.get(name) is not None
        assert asyncio.run(permissions.evaluate(name, {}, ctx)).decision is Decision.allow
    assert registry.get("bash") is not None and registry.get("write_file") is not None
    assert "read_agent_execution" in prompt


def test_lead_planning_context_cannot_expose_execution_or_request_write_approval(integration):
    from code_agent.permissions.policy import Decision

    _, app, _, project, root = integration
    lead = roster(integration)["lead"]["session_id"]
    app.state.projects.set_flags(project["id"], allow_direct_write=True)
    app.state.sessions.set_permission_mode(lead, project["user_id"], "full-access")
    # Planning authority must take precedence over a role and a saved grant.
    app.state.db.execute("UPDATE sessions SET kind='planner' WHERE id=?", (lead,))
    # A cached runtime must reject a changed binding before a cold planner
    # runtime is constructed with the new (more restrictive) authority.
    with pytest.raises(SessionClosed):
        app.state.sessions.get_runtime(lead, project["user_id"])
    app.state.sessions._runtimes.pop(lead).shutdown()
    runtime = app.state.sessions.get_runtime(lead, project["user_id"])
    _, registry = runtime._loop_context()
    assert registry.get("read_file") is not None
    for name in ("write_file", "edit_file", "bash", "bash_persistent"):
        assert registry.get(name) is None
    permissions = runtime._make_permissions()
    result = asyncio.run(permissions.evaluate("write_file", {"path": "source.txt"}, ToolContext(lead, root, runtime.user_id)))
    assert result.decision is Decision.deny
    assert not permissions.read_only_approval
    assert app.state.sessions_repo.get(lead)["permission_mode"] == "full-access"


def test_read_only_lead_reads_real_worker_evidence_without_approval(integration):
    _, app, _, project, root = integration
    members = roster(integration)
    lead_id = members["lead"]["session_id"]
    worker_id = worker(integration)["session_id"]
    content = "BEGIN " + "x" * 4000 + " FINAL ACTUAL OUTPUT"
    (root / "source.txt").write_text(content, encoding="utf-8")
    manager = app.state.sessions
    manager.set_permission_mode(lead_id, project["user_id"], "read-only")

    async def inspect():
        app.state.router.resolve_default = lambda: OfflineProvider(script=[
            OfflineProvider.tool_call_turn("read_file", {"path": "source.txt"}),
            OfflineProvider.text_turn("读取已完成"),
        ])
        await manager.get_runtime(worker_id, project["user_id"]).submit("读取已有文件")
        activation = app.state.project_agent_team.descriptor(worker_id)["activation_id"]
        app.state.router.resolve_default = lambda: OfflineProvider(script=[
            OfflineProvider.tool_call_turn("read_agent_execution", {"session_id": worker_id, "activation_id": activation}),
            OfflineProvider.text_turn("已核对原始输出"),
        ])
        await manager.get_runtime(lead_id, project["user_id"]).submit("查看节点原始执行记录")

    asyncio.run(inspect())
    events = app.state.transcript.replay(lead_id)
    assert not any(event.type.value == "approval_request" for event in events)
    result = next(event.payload["result"] for event in events
                  if event.type.value == "tool_result" and event.payload["name"] == "read_agent_execution")
    evidence = json.loads(result)
    assert evidence["boundary_status"] == "complete"
    assert evidence["workspace"] == str(root)
    actual = next(item["payload"]["result"] for item in evidence["events"] if item["type"] == "tool_result")
    assert actual == content
    assert (root / "source.txt").read_text(encoding="utf-8") == content


def test_existing_project_main_migrates_without_losing_transcript_and_old_duplicates_cannot_run(integration):
    _, app, _, project, _ = integration
    manager = app.state.sessions
    owner = project["user_id"]
    legacy = app.state.sessions_repo.create(owner, project_id=project["id"], tree_id="main", kind="main")
    canonical = app.state.sessions_repo.create(owner, project_id=project["id"], tree_id="main", kind="main")
    app.state.projects.set_session(project["id"], canonical["id"])
    app.state.transcript.append(canonical["id"], "user_message", {"text": "已有项目对话"})
    with pytest.raises(SessionClosed):
        manager.get_runtime(legacy["id"], owner)
    assert manager.create_project_session(owner, project["id"], reuse=False)["id"] == canonical["id"]
    assert app.state.project_agent_team.descriptor(canonical["id"])["role"] == "lead"
    assert app.state.transcript.replay(canonical["id"])[0].payload["text"] == "已有项目对话"


def test_workflow_node_intent_is_cleaned_and_resolved_by_server(integration):
    _, app, _, project, _ = integration
    lead = roster(integration)["lead"]["session_id"]
    runtime = app.state.sessions.get_runtime(lead, project["user_id"])
    context = _clean_code_context({"code_context": {"workflow_node_id": "requirements", "workflow_tree_id": "main", "role": "admin"}})
    assert context == {"workflow_node_id": "requirements", "workflow_tree_id": "main"}
    blocks, metadata = runtime._code_context_blocks("处理这个节点", context)
    assert metadata["code_context"] == context
    assert any("需求确认" in text and "按需直接执行或委派" in text for text in blocks)
    assert any("数据块开始" in text for text in blocks)
    assert _clean_code_context({"code_context": {"workflow_node_id": "requirements", "workflow_tree_id": "../other"}}) is None


@pytest.mark.parametrize("root_kind", ["main", "chat"])
def test_real_loop_delegates_executes_reports_and_audits_without_marking_sop_done(integration, root_kind):
    client, app, auth, project, root = integration
    lead = conversation_root(integration, root_kind)
    owner = project["user_id"]
    app.state.projects.set_flags(project["id"], allow_direct_write=True)
    app.state.sessions.set_permission_mode(lead, owner, "workspace-write")
    calls = defaultdict(int)
    child = {}

    class CollaborationProvider(OfflineProvider):
        async def chat(self, messages, tools):
            system = messages[0]["content"]
            role, sid = re.search(r"身份 (lead|node|subagent)；会话 ([^；]+)；", system).groups()
            index = calls[sid]; calls[sid] += 1
            def invoke(name, arguments):
                return ModelTurn(tool_calls=[ToolCall(name=name, arguments=arguments)], stop_reason="tool_use", usage=TokenUsage(input_tokens=1, output_tokens=1))
            if role == "lead":
                if index == 0:
                    return invoke("delegate_agent", {"node_id": "requirements", "prompt": "读 source.txt，写入 verified，执行 echo 确认，并回报证据",
                        "write_paths": ["."], "delegation_reason": "独立完成写入及命令验证，Lead 同时准备核验证据"})
                if index == 1:
                    latest = next(message for message in reversed(messages) if message.get("role") == "tool")
                    child.update(json.loads(latest["content"]))
                    return invoke("wait_agents", {"session_ids": [child["session_id"]], "timeout_seconds": 5})
                if index == 2:
                    return invoke("read_file", {"path": "source.txt"})
                if index == 3:
                    return invoke("audit_agent", {"session_id": child["session_id"], "activation_id": child["activation_id"], "decision": "accepted", "reason": "已读取 source.txt 确认 verified；子任务命令 echo 已成功，轨迹有验证输出"})
                return self.text_turn("Lead 已核对证据并接受本次产出")
            if index == 0:
                return invoke("read_file", {"path": "source.txt"})
            if index == 1:
                return invoke("write_file", {"path": "source.txt", "content": "verified"})
            if index == 2:
                return invoke("bash", {"command": "echo verified-agent-team"})
            return self.text_turn("source.txt 已写入 verified；echo verified-agent-team 成功，验证输出在工具轨迹中")

    provider = CollaborationProvider()
    app.state.router.resolve_default = lambda: provider
    response = client.post(f"/api/v1/sessions/{lead}/prompt", headers=auth, json={"text": "完成需求节点交办的小任务并审计"})
    assert response.status_code == 200, response.text
    assert (root / "source.txt").read_text() == "verified"
    completed = app.state.project_agent_team.descriptor(child["session_id"])
    assert completed["state"] == "completed"
    assert completed["audit"]["decision"] == "accepted"
    assert app.state.sop_workflow_store.get(project["id"], "main")["nodes"][0]["status"] == "pending"
    assert any(event.payload.get("team_notice", {}).get("session_id") == child["session_id"] for event in app.state.transcript.replay(lead))
    assert app.state.db.query_one("SELECT COUNT(*) n FROM project_agent_audits")["n"] == 1


@pytest.mark.parametrize("root_kind", ["main", "chat"])
def test_main_conversation_delegates_two_independent_tasks_and_delivers_without_audit(integration, root_kind):
    client, app, auth, project, root = integration
    canonical = roster(integration)["lead"]["session_id"]
    current = conversation_root(integration, root_kind)
    calls, children = defaultdict(int), []

    class BackgroundProvider(OfflineProvider):
        async def chat(self, messages, tools):
            role, sid = re.search(r"身份 (lead|node|subagent)；会话 ([^；]+)；", messages[0]["content"]).groups()
            index = calls[sid]
            calls[sid] += 1
            def invoke(name, arguments):
                return ModelTurn(tool_calls=[ToolCall(name=name, arguments=arguments)], stop_reason="tool_use")
            if role == "lead":
                if index in (1, 2):
                    latest = next(message for message in reversed(messages) if message.get("role") == "tool")
                    children.append(json.loads(latest["content"])["session_id"])
                if index < 2:
                    return invoke("delegate_agent", {"title": f"独立检查 {index + 1}", "prompt": "读取 source.txt 并报告内容",
                        "write_paths": [], "delegation_reason": "并行只读检查，各自回传独立证据"})
                if index == 2:
                    return invoke("wait_agents", {"session_ids": children, "timeout_seconds": 5})
                return self.text_turn("两项后台检查均已完成，source.txt 内容为 original。")
            if index == 0:
                return invoke("read_file", {"path": "source.txt"})
            return self.text_turn("已读取 source.txt，内容为 original。")

    app.state.router.resolve_default = lambda: BackgroundProvider()
    response = client.post(f"/api/v1/sessions/{current}/prompt", headers=auth, json={"text": "并行完成两项独立读取并汇总结果"})
    assert response.status_code == 200, response.text
    assert len(children) == len(set(children)) == 2
    snapshot = client.get(f"/api/v1/code/projects/{project['id']}/agents", params={"conversation_id": current}, headers=auth).json()
    assert snapshot["lead"]["session_id"] == current
    assert {row["session_id"] for row in snapshot["agents"]} == set(children)
    assert all(row["role"] == "subagent" and row["node_id"] == "" and row["state"] == "completed" for row in snapshot["agents"])
    assert calls[current] == 4, "No additional audit/handoff model round is required"
    assert app.state.db.query_one("SELECT COUNT(*) n FROM project_agent_audits")["n"] == 0
    events = app.state.transcript.replay(current)
    notices = [event.payload["team_notice"] for event in events if event.payload.get("team_notice")]
    assert {note["session_id"] for note in notices} == set(children)
    assert all(note["audit_required"] is False and note["audit_available"] for note in notices)
    assert any("两项后台检查" in event.payload.get("text", "") for event in events)
    assert app.state.projects.get(project["id"])["session_id"] == canonical
    assert (root / "source.txt").read_text() == "original"
    if current != canonical:
        assert roster(integration)["agents"] == []
        assert app.state.transcript.replay(canonical) == []


def test_idle_lead_is_woken_to_audit_a_late_child_result(integration, monkeypatch):
    _, app, _, project, _ = integration
    team = app.state.project_agent_team
    lead = roster(integration)["lead"]["session_id"]
    monkeypatch.setattr("code_agent.sessions.manager.MAX_CONSECUTIVE_WAKES", 3)

    async def verify():
        entered, release = asyncio.Event(), asyncio.Event()
        calls, child = defaultdict(int), {}
        class LateResultProvider(OfflineProvider):
            async def chat(self, messages, tools):
                role, sid = re.search(r"身份 (lead|node|subagent)；会话 ([^；]+)；", messages[0]["content"]).groups()
                index = calls[sid]; calls[sid] += 1
                def invoke(name, arguments):
                    return ModelTurn(tool_calls=[ToolCall(name=name, arguments=arguments)], stop_reason="tool_use")
                if role != "lead":
                    entered.set()
                    await release.wait()
                    return self.text_turn("source.txt 存在，交由 Lead 读取验证")
                if index == 0:
                    return invoke("delegate_agent", {"node_id": "requirements", "prompt": "检查输入并回传证据",
                        "write_paths": [], "delegation_reason": "独立检查输入，Lead 可继续整合已有结果"})
                if index == 1:
                    latest = next(message for message in reversed(messages) if message.get("role") == "tool")
                    child.update(json.loads(latest["content"]))
                    return self.text_turn("已委派，结果回传后再审计")
                if index == 2:
                    return invoke("read_file", {"path": "source.txt"})
                if index == 3:
                    return invoke("audit_agent", {"session_id": child["session_id"], "activation_id": child["activation_id"], "decision": "accepted", "reason": "已读取输入确认内容 original，子任务结果与证据一致"})
                return self.text_turn("晚到的节点结果已核对并审计")
        app.state.router.resolve_default = lambda: LateResultProvider()
        runtime = app.state.sessions.get_runtime(lead, project["user_id"])
        await runtime.submit("检查输入并在完成后审计")
        await asyncio.wait_for(entered.wait(), 3)
        assert not runtime.busy
        assert team.descriptor(child["session_id"])["state"] == "running"
        release.set()
        async def wait_for_audit():
            while team.descriptor(child["session_id"])["audit"]["decision"] != "accepted" or runtime.busy:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_audit(), 5)
        assert calls[lead] == 5
        assert runtime._consecutive_wakes == 1
        assert not runtime._wake_notices
        assert not team._capacities[project["id"]].active
        events = app.state.transcript.replay(lead)
        assert any(event.type.value == "user_message" and event.payload.get("auto") for event in events)
    asyncio.run(verify())


def test_busy_completion_wake_preserves_task_owner_and_does_not_drain_notice(integration):
    _, app, _, project, _ = integration
    lead = roster(integration)["lead"]["session_id"]
    runtime = app.state.sessions.get_runtime(lead, project["user_id"])
    async def verify():
        blocker = asyncio.Event()
        task = asyncio.create_task(blocker.wait())
        runtime._task = task
        runtime._wake_notices = ["待审通知"]
        runtime._wake_for_jobs()
        assert runtime._task is task
        assert runtime._wake_notices == ["待审通知"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(verify())


def test_reserved_worker_wake_does_not_take_the_delegated_activation(integration):
    _, app, _, project, _ = integration
    child = worker(integration)["session_id"]
    runtime = app.state.sessions.get_runtime(child, project["user_id"])
    app.state.db.execute("UPDATE project_agents SET state='queued' WHERE session_id=?", (child,))
    runtime._wake_notices = ["子任务产出等待处理"]
    runtime._wake_for_jobs()
    assert runtime._task is None
    assert runtime._wake_notices == ["子任务产出等待处理"]


@pytest.mark.parametrize("action", ["close", "archive", "delete"])
def test_cold_lead_lifecycle_stops_running_descendants_without_cached_lead(integration, action):
    _, app, _, project, _ = integration
    team = app.state.project_agent_team
    manager = app.state.sessions
    members = roster(integration)
    lead = members["lead"]["session_id"]
    child = worker(integration)["session_id"]
    old_runtime = manager._runtimes.pop(lead, None)
    if old_runtime:
        old_runtime.shutdown()
    assert lead not in manager._runtimes

    async def verify():
        entered = asyncio.Event()
        runtime = manager.get_runtime(child, project["user_id"])
        async def blocked_loop(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        runtime._run_loop = blocked_loop
        task = asyncio.create_task(runtime.submit("保持执行，等待生命周期停止"))
        await asyncio.wait_for(entered.wait(), 3)
        assert team.descriptor(child)["state"] == "running"
        await asyncio.to_thread(getattr(manager, action), lead, project["user_id"])
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert team.descriptor(child)["state"] == "cancelled"
        assert not team._capacities[project["id"]].active
    asyncio.run(verify())


def test_invalid_collaboration_tree_cannot_commit_a_workflow_revision(integration):
    client, app, auth, project, _ = integration
    workflow = app.state.sop_workflow_store.get(project["id"], "task:missing")
    workflow.pop("revision"); workflow.pop("updated_at")
    response = client.put(f"/api/v1/code/projects/{project['id']}/workflow?tree=task:missing", headers=auth, json={"expected_revision": 0, "workflow": workflow})
    assert response.status_code == 404
    assert app.state.sop_workflow_store.get(project["id"], "task:missing")["revision"] == 0


def test_wait_tool_cannot_release_capacity_while_same_turn_tools_still_execute(agent_home, tmp_path):
    from code_agent.agent.loop import AgentLoop
    from code_agent.permissions.hooks import PermissionManager
    from code_agent.storage.jsonl import TranscriptStore
    from code_agent.tools.registry import ToolRegistry

    async def verify():
        started, completed, observations = set(), set(), []
        both_started = asyncio.Event()
        class OrdinaryTool(Tool):
            def __init__(self, name):
                self.name = name
            async def execute(self, arguments, ctx):
                started.add(self.name)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), 2)
                completed.add(self.name)
                return self.name
        class SuspendingTool(Tool):
            name = "wait_fixture"
            suspends_session = True
            async def execute(self, arguments, ctx):
                observations.append(set(completed))
                published = [event.payload["result"] for event in loop.transcript.replay(loop.session_id)
                             if event.type.value == "tool_result"]
                assert set(published) == {"parallel_a", "parallel_b"}
                assert len(published) == 2, "Ordinary completion events must arrive before the wait releases capacity"
                return "waited"
        class HistoryProvider(OfflineProvider):
            async def chat(self, messages, tools):
                self.model_results = [message["content"] for message in messages if message["role"] == "tool"]
                return await super().chat(messages, tools)
        registry = ToolRegistry()
        for tool in (OrdinaryTool("parallel_a"), OrdinaryTool("parallel_b"), SuspendingTool()):
            registry.register(tool)
        provider = HistoryProvider(script=[ModelTurn(tool_calls=[ToolCall(name="wait_fixture"), ToolCall(name="parallel_a"), ToolCall(name="parallel_b")], stop_reason="tool_use"), OfflineProvider.text_turn("done")])
        loop = AgentLoop(provider, registry, PermissionManager(workspace_root=tmp_path, mode="full-access"), TranscriptStore(agent_home / "fixture-transcripts"), workspace=tmp_path)
        result = await loop.run("exercise concurrent tools")
        assert result.stop_reason == "end_turn"
        assert observations == [{"parallel_a", "parallel_b"}]
        tool_results = [event.payload["result"] for event in loop.transcript.replay(loop.session_id) if event.type.value == "tool_result"]
        assert len(tool_results) == 3
        assert set(tool_results[:-1]) == {"parallel_a", "parallel_b"}
        assert tool_results[-1] == "waited", "Completion events follow execution; the wait is always last"
        assert provider.model_results == ["waited", "parallel_a", "parallel_b"], "Model history follows declared call order"
    asyncio.run(verify())
