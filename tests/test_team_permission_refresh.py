"""An already running child loop observes its current inherited permissions."""

import asyncio

import pytest

from code_agent.agent.project_team import ProjectTeam
from code_agent.agent.router import ModelRouter
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, TaskRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import WriteFileTool
from code_agent.tools.registry import ToolRegistry


@pytest.mark.parametrize("task_tree", [False, True])
def test_running_child_rechecks_lead_permission_without_changing_task_tree_policy(agent_home, monkeypatch, task_tree):
    monkeypatch.setattr("code_agent.agent.project_team.settings.permission_mode", lambda: "workspace-write")
    root = agent_home / "project"
    root.mkdir()
    db = Database(agent_home / "team.db")
    user_id = UserRepo(db).create("owner", "test-only")["id"]
    projects = ProjectRepo(db)
    project = projects.create(user_id, "project", str(root), str(root))
    projects.set_flags(project["id"], allow_direct_write=True)
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    manager = SessionManager(
        transcript=TranscriptStore(agent_home / "transcripts"), session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db), router=ModelRouter(provider_name="offline"),
        registry=registry, project_repo=projects,
    )
    team = ProjectTeam(manager)
    manager.agent_team = team
    lead = manager.create_project_session(user_id, project["id"], permission_mode="workspace-write")
    tree = "main"
    if task_tree:
        root = agent_home / "isolated"
        root.mkdir()
        task = TaskRepo(db).create(
            project_id=project["id"], user_id=user_id, title="isolated", requirement="work",
            base_branch="main", branch="isolated", worktree=str(root),
        )
        tree = f"task:{task['id']}"
    child = team._create_worker(project, team._member(lead["id"], user_id),
                                tree=tree, role="subagent", title="live permission refresh")
    assert child["role"] == "subagent" and child["node_id"] == ""
    runtime = manager.get_runtime(child["session_id"], user_id)
    approvals = []

    async def reject(request):
        approvals.append(request)
        return False

    runtime._server_approver = reject

    async def verify():
        entered, release = asyncio.Event(), asyncio.Event()

        class PausedProvider(OfflineProvider):
            async def chat(self, messages, tools):
                if self.calls == 1:
                    entered.set()
                    await release.wait()
                return await super().chat(messages, tools)

        provider = PausedProvider(script=[
            OfflineProvider.tool_call_turn("write_file", {"path": "result.txt", "content": "first", "_approval": {
                "purpose": "验证原先可写权限下的第一次写入", "effects": ["在临时 result.txt 写入 first"]}}),
            OfflineProvider.tool_call_turn("write_file", {"path": "result.txt", "content": "second", "_approval": {
                "purpose": "验证 Lead 权限变化在下一次写入前重新检查", "effects": ["尝试把临时 result.txt 更新为 second"]}}),
            OfflineProvider.text_turn("finished"),
        ])
        original_build = runtime._build_loop
        runtime._build_loop = lambda **kwargs: original_build(**{**kwargs, "provider": provider})
        await team.followup(lead["id"], user_id, child["session_id"], "write twice", write_paths=["result.txt"])
        running = team._starts[child["session_id"]]
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert runtime.permission_mode == "workspace-write"
            assert team.descriptor(child["session_id"])["state"] == "running"
            assert team.descriptor(child["session_id"])["write_paths"] == ["result.txt"]
            assert (root / "result.txt").read_text(encoding="utf-8") == "first"
            manager.set_permission_mode(lead["id"], user_id, "read-only")
            assert runtime.permission_mode == "workspace-write"  # Existing child snapshot stays stale.
            release.set()
            result = await asyncio.wait_for(running, 3)
            if task_tree:
                assert result.text == "finished" and result.stop_reason == "end_turn"
            else:
                assert result.stop_reason == "stalled"
                assert "编辑被权限或审批拒绝" in result.text
            assert len(approvals) == (0 if task_tree else 1)
            assert (root / "result.txt").read_text(encoding="utf-8") == ("second" if task_tree else "first")
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await team.aclose()

    try:
        asyncio.run(verify())
    finally:
        db.close()
