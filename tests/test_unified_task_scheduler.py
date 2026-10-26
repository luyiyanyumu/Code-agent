"""Isolated changes use the real project scheduler with deterministic workers.

Every workspace, database and budget ledger is temporary. No app server,
provider request or production session is started by these tests.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from code_agent.agent.project_team import ProjectTeam, TeamError
from code_agent.codeintel.parallel import BatchService
from code_agent.codeintel.parallel import BatchError
from code_agent.codeintel.tasks import TaskError, TaskService
from code_agent.codeintel.vcs import git_env
from code_agent.models.events import TokenUsage
from code_agent.sessions.team_budget import TeamBudget
from code_agent.sessions.manager import SessionRuntime
from code_agent.storage.db import BatchRepo, Database, ProjectRepo, SessionRepo, TaskRepo, UserRepo
from code_agent.permissions.policy import Decision
from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.registry import ToolRegistry


class LocalRuntime:
    def __init__(self, manager, meta):
        self.manager = manager
        self.session_id = meta["id"]
        self.user_id = meta["user_id"]
        self.workspace = manager.workspace_for(meta)
        self.permission_mode = meta["permission_mode"]
        self._project_permission_provider = None
        self.agent_team = None
        self._task = None
        self._execution_budget = None
        self.contexts = []
        self.notes = []

    @property
    def busy(self):
        return self._task is not None and not self._task.done()

    async def submit(self, prompt, attachments=None, code_context=None):
        if self.busy:
            raise TeamError("会话正在执行")
        self._task = asyncio.create_task(self.agent_team.run(self, prompt, attachments, code_context))
        return await self._task

    async def _run_loop(self, prompt, attachments=None, code_context=None):
        if self._execution_budget:
            self._execution_budget.check()
        if self.manager.handler:
            return await self.manager.handler(self, prompt)
        await asyncio.sleep(0)
        return SimpleNamespace(text=f"本轮完成：{prompt}", stop_reason="end_turn", turns=2)

    def set_delegation_context(self, parent, strategy, prompt):
        self.contexts.append((parent, strategy, prompt))

    def set_execution_budget(self, budget, *, activation_id, parent_session_id="", continuation=False):
        if self._execution_budget is None:
            self._execution_budget = TeamBudget(self.manager.session_repo.db, self.session_id)
        row = self.agent_team._member(self.session_id, self.user_id)
        parent_runtime = self.manager._runtimes.get(parent_session_id)
        parent = parent_runtime._execution_budget.current if parent_runtime and parent_runtime.busy and parent_runtime._execution_budget else None
        self._execution_budget.begin(row["project_id"], activation_id, budget, parent=parent, continuation=continuation)

    def execution_budget_status(self):
        return self._execution_budget.status() if self._execution_budget else {}

    def remaining_execution_seconds(self):
        return self._execution_budget.remaining_seconds() if self._execution_budget else None

    def enqueue_team_note(self, note):
        self.notes.append(note)

    def cancel(self):
        if self.busy:
            self._task.cancel()


class LocalManager:
    def __init__(self, db):
        self.session_repo = SessionRepo(db)
        self.project_repo = ProjectRepo(db)
        self._runtimes = {}
        self.agent_team = None
        self.handler = None

    def workspace_for(self, meta):
        return Path(meta.get("workspace_override") or self.project_repo.get(meta["project_id"])["root_path"])

    def create_project_session(self, user_id, project_id, *, permission_mode="read-only", reuse=True):
        project = self.project_repo.get(project_id)
        existing = self.session_repo.get(project.get("session_id") or "")
        if reuse and existing:
            return existing
        meta = self.session_repo.create(user_id, project_id=project_id, tree_id="main", kind="main", permission_mode=permission_mode)
        self.project_repo.set_session(project_id, meta["id"])
        return meta

    def create_task_session(self, user_id, project_id, *, task_id, title, workspace, permission_mode):
        return self.session_repo.create(
            user_id, project_id=project_id, tree_id=f"task:{task_id}", task_id=task_id,
            kind="task", title=title, workspace_override=str(workspace), permission_mode=permission_mode,
        )

    def get_runtime(self, session_id, user_id):
        meta = self.session_repo.get(session_id)
        if not meta or meta["user_id"] != user_id:
            raise KeyError("会话不存在或无权访问")
        if session_id not in self._runtimes:
            self._runtimes[session_id] = LocalRuntime(self, meta)
        runtime = self._runtimes[session_id]
        if self.agent_team.descriptor(session_id):
            runtime.agent_team = self.agent_team
        return runtime

    def set_permission_mode(self, session_id, user_id, mode):
        assert self.session_repo.get(session_id)["user_id"] == user_id
        self.session_repo.set_permission_mode(session_id, mode)
        if session_id in self._runtimes:
            self._runtimes[session_id].permission_mode = mode

@pytest.fixture
def isolated(agent_home, tmp_path, monkeypatch):
    db = Database(tmp_path / "scheduler.sqlite")
    owner = UserRepo(db).create("scheduler-owner", "unused")["id"]
    manager = LocalManager(db)
    root = tmp_path / "main"
    root.mkdir()
    (root / "main.txt").write_text("unchanged", encoding="utf-8")
    project = manager.project_repo.create(owner, "scheduler", str(root), str(root))
    limits = {"max_depth": 3, "max_parallel": 1}
    monkeypatch.setattr("code_agent.agent.project_team.settings.agent_team_config", lambda: dict(limits))
    monkeypatch.setattr("code_agent.agent.project_team.settings.permission_mode", lambda: "workspace-write")
    team = ProjectTeam(manager)
    manager.agent_team = team
    tasks = TaskRepo(db)
    batches = BatchRepo(db)
    service = TaskService(projects=manager.project_repo, tasks=tasks, intel=None, sessions=manager)
    yield SimpleNamespace(db=db, owner=owner, manager=manager, project=project, root=root, team=team,
                          tasks=tasks, batches=batches, service=service, limits=limits, tmp_path=tmp_path)
    db.close()


def task_for(e, *, lane="", batch_id="", deferred=False):
    workspace = e.tmp_path / ("worktree-" + lane if lane else "worktree-" + str(len(e.tasks.list_for_project(e.project["id"]))))
    if not deferred:
        workspace.mkdir()
    return e.tasks.create(project_id=e.project["id"], user_id=e.owner, title=lane or "隔离任务",
                          requirement="实施需求", base_branch="main", branch="test-branch", worktree="" if deferred else str(workspace),
                          status="deferred" if deferred else "planned", lane=lane, batch_id=batch_id)


def run(e, task, prompt="执行", permission="workspace-write"):
    return e.service.run_agent(e.project, task, prompt, permission_mode=permission)


def test_planning_and_execution_share_lead_owned_history_and_honor_grants(isolated):
    e = isolated
    task = task_for(e)
    seen = []

    async def handler(runtime, prompt):
        seen.append((runtime.workspace, runtime.permission_mode))
        return SimpleNamespace(text=f"结果：{prompt}", stop_reason="end_turn", turns=2)

    e.manager.handler = handler

    async def check():
        first = await run(e, task, "计划", "read-only")
        fresh = e.tasks.get(task["id"])
        second = await run(e, fresh, "执行", "full-access")
        assert first["session_id"] == second["session_id"] == fresh["session_id"]
        assert first["activation_id"] == 1 and second["activation_id"] == 2
        descriptor = e.team.descriptor(second["session_id"])
        assert descriptor["role"] == "subagent" and descriptor["depth"] == 1
        assert descriptor["tree_id"] == f"task:{task['id']}"
        assert descriptor["parent_session_id"] == descriptor["lead_session_id"]
        lead = e.manager.get_runtime(descriptor["lead_session_id"], e.owner)
        assert [(n["activation_id"], n["state"]) for n in lead.notes] == [(1, "completed"), (2, "completed")]
        assert e.manager.session_repo.get(lead.session_id)["permission_mode"] == "read-only"

    asyncio.run(check())
    assert seen == [(Path(task["worktree"]), "read-only"), (Path(task["worktree"]), "full-access")]
    assert (e.root / "main.txt").read_text() == "unchanged"


def test_idle_legacy_task_is_adopted_without_replacing_session_or_history(isolated):
    e = isolated
    task = task_for(e)
    meta = e.manager.create_task_session(e.owner, e.project["id"], task_id=task["id"], title="历史任务", workspace=Path(task["worktree"]), permission_mode="read-only")
    e.tasks.update(task["id"], session_id=meta["id"])
    e.manager.session_repo.touch(meta["id"], title="保留原会话")
    result = asyncio.run(run(e, e.tasks.get(task["id"])))
    assert result["session_id"] == meta["id"]
    assert e.team.descriptor(meta["id"])["parent_session_id"] == e.team._lead(e.project["id"], e.owner)["session_id"]
    assert e.manager.session_repo.get(meta["id"])["kind"] == "task"


def test_busy_legacy_task_is_not_migrated_reconfigured_or_cancelled(isolated):
    e = isolated
    task = task_for(e)
    meta = e.manager.create_task_session(e.owner, e.project["id"], task_id=task["id"], title="旧执行", workspace=Path(task["worktree"]), permission_mode="read-only")
    e.tasks.update(task["id"], session_id=meta["id"])

    async def check():
        runtime = e.manager.get_runtime(meta["id"], e.owner)
        runtime._task = asyncio.create_task(asyncio.Event().wait())
        try:
            with pytest.raises(TeamError, match="不会迁移或中断"):
                await run(e, e.tasks.get(task["id"]), permission="full-access")
            assert e.team.descriptor(meta["id"]) is None
            assert runtime.busy and not runtime._task.cancelling()
            assert runtime.agent_team is None and runtime.permission_mode == "read-only"
            assert e.manager.session_repo.get(meta["id"])["permission_mode"] == "read-only"
        finally:
            runtime._task.cancel()
            await asyncio.gather(runtime._task, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.parametrize("bad_field,bad_value", [("tree_id", "main"), ("task_id", "other"), ("workspace_override", "main"), ("kind", "main")])
def test_cannot_adopt_history_with_mismatched_fixed_bindings(isolated, bad_field, bad_value):
    e = isolated
    task = task_for(e)
    args = dict(project_id=e.project["id"], tree_id=f"task:{task['id']}", task_id=task["id"], workspace_override=task["worktree"], kind="task")
    args[bad_field] = str(e.root) if bad_field == "workspace_override" else bad_value
    meta = e.manager.session_repo.create(e.owner, **args)
    e.tasks.update(task["id"], session_id=meta["id"])
    with pytest.raises(TeamError, match="绑定无效"):
        asyncio.run(run(e, e.tasks.get(task["id"])))
    assert e.team.descriptor(meta["id"]) is None
    assert e.db.query_all("SELECT * FROM project_agent_runs") == []


def test_isolated_tasks_share_capacity_with_existing_project_workers(isolated):
    e = isolated
    first, second = task_for(e), task_for(e)
    active = 0
    peak = 0
    seen = []

    async def handler(runtime, prompt):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            seen.append(prompt)
            await asyncio.sleep(0.015)
            return SimpleNamespace(text=prompt, stop_reason="end_turn", turns=1)
        finally:
            active -= 1

    e.manager.handler = handler

    async def check():
        lead = e.team._lead(e.project["id"], e.owner)
        existing = await e.team.start(lead["session_id"], e.owner, "已有团队工作", tree="main")
        results = await asyncio.gather(run(e, first, "隔离一"), run(e, second, "隔离二"))
        await e.team.wait(lead["session_id"], e.owner, [existing["session_id"]], 1)
        assert all(result["state"] == "completed" for result in results)
        assert e.team._capacity(e.project["id"]).active == set()

    asyncio.run(check())
    assert peak == 1 and len(seen) == 3


def test_recursive_isolated_worker_wait_releases_capacity_and_budget_is_shared(isolated):
    e = isolated
    task = task_for(e)
    scopes = []

    async def handler(runtime, prompt):
        scopes.append(runtime._execution_budget.current["scope_id"])
        if prompt == "隔离父任务":
            child = await e.team.start(runtime.session_id, e.owner, "递归子任务")
            await e.team.wait(runtime.session_id, e.owner, [child["session_id"]], 1)
            child_runtime = e.manager.get_runtime(child["session_id"], e.owner)
            assert child_runtime.workspace == runtime.workspace
            assert child_runtime.permission_mode == "full-access"
            assert e.team.descriptor(child["session_id"])["state"] == "completed"
        return SimpleNamespace(text=prompt, stop_reason="end_turn", turns=1)

    e.manager.handler = handler
    result = asyncio.run(asyncio.wait_for(run(e, task, "隔离父任务", "full-access"), 3))
    assert result["state"] == "completed"
    assert len(scopes) == 2 and len(set(scopes)) == 1


def test_exact_activation_result_survives_newer_mutable_descriptor(isolated, monkeypatch):
    e = isolated
    task = task_for(e)
    original = e.team._notify_parent

    async def newer(session_id, activation):
        await original(session_id, activation)
        next_activation = e.team._begin_activation(session_id, "随后的一轮", "queued")
        e.team._finish(session_id, next_activation, "completed", result="另一次执行的结果")

    monkeypatch.setattr(e.team, "_notify_parent", newer)
    result = asyncio.run(run(e, task, "当前请求"))
    assert result["activation_id"] == 1 and result["text"] == "本轮完成：当前请求"
    assert e.team.descriptor(result["session_id"])["activation_id"] == 2
    assert e.team.descriptor(result["session_id"])["result"] == "另一次执行的结果"


@pytest.mark.parametrize("reason", ["max_turns", "cancelled", "token_budget", "repeated_failure", "provider_error"])
def test_incomplete_execution_never_advances_to_review(isolated, monkeypatch, reason):
    e = isolated
    task = task_for(e)
    review_calls = []

    async def handler(runtime, prompt):
        return SimpleNamespace(text="尚未完成的输出", stop_reason=reason, turns=1)

    async def review(*args):
        review_calls.append(args)
        raise AssertionError("failed execution must not enter review")

    e.manager.handler = handler
    monkeypatch.setattr(e.service, "review", review)
    with pytest.raises(TaskError, match="执行未完成"):
        asyncio.run(e.service.execute(e.project, task))
    assert review_calls == []
    assert e.tasks.get(task["id"])["status"] == "failed"
    assert e.db.query_all("SELECT state FROM project_agent_runs")[0]["state"] == "failed"


def test_cancelled_worker_receipt_does_not_become_a_successful_review(isolated, monkeypatch):
    e = isolated
    task = task_for(e)
    started = None

    async def handler(runtime, prompt):
        started.set_result(runtime.session_id)
        await asyncio.Event().wait()

    async def review(*args):
        raise AssertionError("cancelled worker must not enter review")

    e.manager.handler = handler
    monkeypatch.setattr(e.service, "review", review)

    async def check():
        nonlocal started
        started = asyncio.get_running_loop().create_future()
        pending = asyncio.create_task(e.service.execute(e.project, task))
        sid = await started
        lead = e.team._lead(e.project["id"], e.owner)
        await e.team.stop(lead["session_id"], e.owner, sid)
        with pytest.raises(TaskError, match="执行已停止"):
            await pending
        assert e.team.descriptor(sid)["state"] == "cancelled"
        assert not e.team._capacity(e.project["id"]).active

    asyncio.run(check())


def test_parallel_lane_uses_project_team_even_with_external_runner_and_no_false_review(isolated, monkeypatch):
    e = isolated
    batch = e.batches.create(project_id=e.project["id"], user_id=e.owner, title="批次", requirement="并行需求", base_branch="main")
    task = task_for(e, lane="one", batch_id=batch["id"])
    e.batches.update(batch["id"], plan_json='{"lanes":[{"id":"one","files":["one.txt"]}]}')

    async def bypass(*args):
        raise AssertionError("external runner must not bypass project scheduler")

    async def handler(runtime, prompt):
        return SimpleNamespace(text="预算用尽", stop_reason="token_budget", turns=2)

    async def review(*args):
        raise AssertionError("incomplete lane must not enter review")

    e.manager.handler = handler
    monkeypatch.setattr(e.service, "review", review)
    service = BatchService(project_repo=e.manager.project_repo, batches=e.batches, tasks=e.tasks,
                           task_service=e.service, sessions=e.manager, runner=bypass)
    result = asyncio.run(service.run(e.project, e.batches.get(batch["id"])))
    assert not result["ok"] and result["lanes"][0]["ok"] is False
    assert e.tasks.get(task["id"])["status"] == "failed"
    assert e.batches.get(batch["id"])["status"] == "failed"
    assert e.team.descriptor(e.tasks.get(task["id"])["session_id"])["tree_id"] == f"task:{task['id']}"


@pytest.mark.parametrize("operation", ["merge", "discard", "review", "verify"])
def test_busy_recursive_descendant_blocks_worktree_operations(isolated, monkeypatch, operation):
    e = isolated
    task = task_for(e)
    started = None

    async def handler(runtime, prompt):
        if prompt == "未结束的后代":
            started.set_result(runtime.session_id)
            await asyncio.Event().wait()
        return SimpleNamespace(text="父任务报告完成", stop_reason="end_turn", turns=1)

    e.manager.handler = handler

    async def check():
        nonlocal started
        started = asyncio.get_running_loop().create_future()
        parent = await run(e, task)
        child = await e.team.start(parent["session_id"], e.owner, "未结束的后代")
        await started
        fresh = e.tasks.get(task["id"])
        e.tasks.update(task["id"], status="review")
        try:
            with pytest.raises(TaskError, match="仍有智能体"):
                if operation == "verify":
                    await e.service.verify(fresh)
                else:
                    await getattr(e.service, operation)(e.project, fresh)
            assert Path(task["worktree"]).is_dir()
            assert e.tasks.get(task["id"])["status"] == "review"
            assert e.team.descriptor(parent["session_id"])["state"] == "completed"
            assert e.team.descriptor(child["session_id"])["state"] == "running"
        finally:
            lead = e.team._lead(e.project["id"], e.owner)
            await e.team.stop(lead["session_id"], e.owner, child["session_id"])

    asyncio.run(check())


def test_tree_reservation_blocks_new_dispatch_until_git_operation_finishes(isolated):
    e = isolated
    task = task_for(e)

    async def check():
        first = await run(e, task)
        fresh = e.tasks.get(task["id"])
        lead = e.team._lead(e.project["id"], e.owner)
        with e.service._task_maintenance(e.project, fresh):
            with pytest.raises(TaskError, match="正在审查"):
                await run(e, fresh)
            with pytest.raises(TeamError, match="正在审查"):
                await e.team.followup(lead["session_id"], e.owner, first["session_id"], "再次执行")
            with pytest.raises(TeamError, match="正在审查"):
                await e.team.start(lead["session_id"], e.owner, "另一个执行", tree=f"task:{task['id']}")
        second = await run(e, fresh)
        assert second["session_id"] == first["session_id"]
        assert second["activation_id"] == first["activation_id"] + 1

    asyncio.run(check())


def test_busy_unmanaged_legacy_runtime_also_blocks_cleanup(isolated):
    e = isolated
    task = task_for(e)
    meta = e.manager.create_task_session(e.owner, e.project["id"], task_id=task["id"], title="旧执行", workspace=Path(task["worktree"]), permission_mode="read-only")
    e.tasks.update(task["id"], session_id=meta["id"])

    async def check():
        runtime = e.manager.get_runtime(meta["id"], e.owner)
        runtime._task = asyncio.create_task(asyncio.Event().wait())
        try:
            with pytest.raises(TaskError, match="仍有执行"):
                await e.service.discard(e.project, e.tasks.get(task["id"]))
            assert Path(task["worktree"]).exists() and runtime.busy
            assert e.team.descriptor(meta["id"]) is None
        finally:
            runtime._task.cancel()
            await asyncio.gather(runtime._task, return_exceptions=True)

    asyncio.run(check())


def test_verification_evidence_survives_review_refresh_and_new_revision_is_stale(isolated, monkeypatch):
    e = isolated
    task = task_for(e)
    e.tasks.update(task["id"], plan_json='{"subtasks":[{"verify":["local-test-command"]}]}')
    head = "verified-revision"

    async def git(*args, **kwargs):
        return (0, head) if args[1][0] == "rev-parse" else (0, "")

    async def unchanged(*args, **kwargs):
        return []

    async def no_scan(*args, **kwargs):
        return None

    async def successful_command(*args, **kwargs):
        return "exit_code=0\nactual local fixture result"

    monkeypatch.setattr(e.service, "_git", git)
    monkeypatch.setattr(e.service, "ensure_committed", no_scan)
    monkeypatch.setattr(e.service, "changes", unchanged)
    monkeypatch.setattr(e.service, "symbol_diff", lambda *args: {})
    monkeypatch.setattr("code_agent.sandbox.executor.run_shell", successful_command)
    e.service.intel = SimpleNamespace(scan=no_scan, diagnostics=lambda *args, **kwargs: {})

    async def check():
        nonlocal head
        result = await e.service.verify(e.tasks.get(task["id"]))
        assert result["ok"] and result["results"][0]["command"] == "local-test-command"
        fresh = await e.service.review(e.project, e.tasks.get(task["id"]))
        assert fresh["verification"] == result["results"]
        assert fresh["verification_head"] == "verified-revision" and not fresh["verification_stale"]
        head = "new-revision-after-further-edits"
        changed = await e.service.review(e.project, e.tasks.get(task["id"]))
        assert changed["verification"] == result["results"] and changed["verification_stale"]

    asyncio.run(check())


def test_batch_planning_does_not_occupy_busy_lead_and_recursive_grant_stays_read_only(isolated):
    e = isolated
    batch = e.batches.create(project_id=e.project["id"], user_id=e.owner, title="并行规划", requirement="需求", base_branch="main")
    e.manager.project_repo.set_flags(e.project["id"], allow_direct_write=True)
    batch_service = BatchService(project_repo=e.manager.project_repo, batches=e.batches, tasks=e.tasks,
                                 task_service=e.service, sessions=e.manager)
    scopes, prompts = [], []
    lead_started, lead_release = None, None

    async def handler(runtime, prompt):
        prompts.append(prompt)
        scopes.append(runtime._execution_budget.current["scope_id"])
        if prompt == "Lead 当前工作":
            lead_started.set_result(runtime.session_id)
            await lead_release.wait()
        elif prompt == "规划需求":
            assert runtime.permission_mode == "read-only"
            assert e.team.descriptor(runtime.session_id)["batch_id"] == batch["id"]
            # Even a later configured grant cannot upgrade this planning role.
            e.manager.set_permission_mode(runtime.session_id, e.owner, "full-access")
            assert e.team.effective_permission(runtime.session_id, e.owner) == "read-only"
            child = await e.team.start(runtime.session_id, e.owner, "规划子读取")
            e.manager.set_permission_mode(child["session_id"], e.owner, "full-access")
            assert e.team.effective_permission(child["session_id"], e.owner) == "read-only"
            await e.team.wait(runtime.session_id, e.owner, [child["session_id"]], 1)
            return SimpleNamespace(text='{"lanes":[{"id":"one","files":["main.txt"]}]}', stop_reason="end_turn", turns=2)
        elif prompt == "规划子读取":
            assert runtime.permission_mode == "read-only" and runtime.workspace == e.root
        return SimpleNamespace(text="已完成只读工作", stop_reason="end_turn", turns=1)

    e.manager.handler = handler

    async def check():
        nonlocal lead_started, lead_release
        lead_started = asyncio.get_running_loop().create_future()
        lead_release = asyncio.Event()
        lead = e.team._lead(e.project["id"], e.owner)
        e.manager.set_permission_mode(lead["session_id"], e.owner, "full-access")
        runtime = e.manager.get_runtime(lead["session_id"], e.owner)
        pending = asyncio.create_task(runtime.submit("Lead 当前工作"))
        await lead_started
        text = await asyncio.wait_for(batch_service._ask(e.project, batch, "规划需求"), 3)
        assert json.loads(text)["lanes"][0]["id"] == "one"
        assert runtime.busy and e.team.descriptor(runtime.session_id)["activation_id"] == 1
        assert e.manager.session_repo.get(runtime.session_id)["permission_mode"] == "full-access"
        planners = e.db.query_all("SELECT id FROM sessions WHERE kind='planner'")
        assert len(planners) == 1
        assert e.team.descriptor(planners[0]["id"])["state"] == "completed"
        lead_release.set()
        await pending

    asyncio.run(check())
    assert prompts == ["Lead 当前工作", "规划需求", "规划子读取"]
    assert len(set(scopes)) == 1
    assert (e.root / "main.txt").read_text() == "unchanged"


def test_real_budget_exhaustion_fails_isolated_execution_before_review(isolated, monkeypatch):
    from code_agent import settings

    e = isolated
    task = task_for(e)
    settings.save_agent_team_budgets({"task_tokens": 10})

    async def handler(runtime, prompt):
        runtime._execution_budget.consume(TokenUsage(input_tokens=10, output_tokens=0))
        runtime._execution_budget.check()
        raise AssertionError("exhausted budget cannot continue")

    async def review(*args):
        raise AssertionError("exhausted budget cannot enter review")

    e.manager.handler = handler
    monkeypatch.setattr(e.service, "review", review)
    with pytest.raises(TaskError, match="Token"):
        asyncio.run(e.service.execute(e.project, task))
    assert e.tasks.get(task["id"])["status"] == "failed"
    assert e.db.query_all("SELECT state FROM project_agent_runs")[0]["state"] == "failed"


def test_parent_completion_with_live_children_preserves_result_and_pending_review_state(isolated):
    e = isolated
    task = task_for(e)
    child_release, child_id = None, ""

    async def handler(runtime, prompt):
        nonlocal child_id
        if prompt == "尚在读取的子任务":
            await child_release.wait()
            return SimpleNamespace(text="子读取完成", stop_reason="end_turn", turns=1)
        child = await e.team.start(runtime.session_id, e.owner, "尚在读取的子任务")
        child_id = child["session_id"]
        return SimpleNamespace(text="本轮父任务结果", stop_reason="end_turn", turns=2)

    e.manager.handler = handler

    async def check():
        nonlocal child_release
        child_release = asyncio.Event()
        result = await e.service.execute(e.project, task)
        assert not result["ok"] and result["review_pending"]
        fresh = e.tasks.get(task["id"])
        assert fresh["status"] == "review" and "审查尚未完成" in fresh["error"]
        outcome = e.service.review_of(fresh)["execution_outcome"]
        assert outcome["text"] == "本轮父任务结果" and outcome["activation_id"] == 1
        child_release.set()
        lead = e.team._lead(e.project["id"], e.owner)
        await e.team.wait(lead["session_id"], e.owner, [child_id], 1)
        assert e.team.descriptor(child_id)["state"] == "completed"
        assert Path(task["worktree"]).exists()

    asyncio.run(check())


@pytest.mark.parametrize("untracked", [False, True])
@pytest.mark.parametrize("leading_space", [False, True])
def test_uncommitted_contents_invalidate_verification_even_when_head_is_unchanged(isolated, monkeypatch, untracked, leading_space):
    e = isolated
    task = task_for(e)
    wt = Path(task["worktree"])
    env = {**git_env(), "GIT_AUTHOR_NAME": "local-test", "GIT_AUTHOR_EMAIL": "local@test",
           "GIT_COMMITTER_NAME": "local-test", "GIT_COMMITTER_EMAIL": "local@test"}

    def git(*args):
        result = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    source_name = " source.py" if leading_space else "source.py"
    (wt / source_name).write_text("value = 1\n", encoding="utf-8")
    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "fixture")
    target = wt / ((" new.py" if leading_space else "new.py") if untracked else source_name)
    if untracked:
        target.write_text("value = 1\n", encoding="utf-8")
    original_head = git("rev-parse", "HEAD")
    e.tasks.update(task["id"], plan_json='{"subtasks":[{"verify":["fixture-command"]}]}')

    async def successful_command(*args, **kwargs):
        return "exit_code=0\nfixture verification passed"

    async def no_scan(*args, **kwargs):
        return None

    async def changes(*args, **kwargs):
        return []

    monkeypatch.setattr("code_agent.sandbox.executor.run_shell", successful_command)
    monkeypatch.setattr(e.service, "changes", changes)
    monkeypatch.setattr(e.service, "symbol_diff", lambda *args: {})
    e.service.intel = SimpleNamespace(scan=no_scan, diagnostics=lambda *args, **kwargs: {})

    async def check():
        result = await e.service.verify(e.tasks.get(task["id"]))
        assert result["ok"]
        assert not await e.service.verification_stale(e.tasks.get(task["id"]))
        # The changed path inventory and HEAD are unchanged; the bytes differ.
        target.write_text("value = 999\n", encoding="utf-8")
        assert git("rev-parse", "HEAD") == original_head
        assert await e.service.verification_stale(e.tasks.get(task["id"]))
        refreshed = await e.service.review(e.project, e.tasks.get(task["id"]))
        assert refreshed["verification"] == result["results"]
        assert refreshed["verification_stale"]

    asyncio.run(check())


def test_new_writable_activation_marks_existing_verification_stale_before_review(isolated):
    e = isolated
    task = task_for(e)
    e.tasks.update(task["id"], review_json='{"verification":[{"ok":true}],"verification_stale":false,"verification_fingerprint":"old"}')
    asyncio.run(run(e, e.tasks.get(task["id"])))
    assert e.service.review_of(e.tasks.get(task["id"]))["verification_stale"]


@pytest.mark.parametrize("operation", ["discard", "merge_queue", "replace_plan"])
def test_batch_operation_cannot_hide_or_remove_a_busy_lane(isolated, operation):
    e = isolated
    batch = e.batches.create(project_id=e.project["id"], user_id=e.owner, title="批次", requirement="需求", base_branch="main")
    task = task_for(e, lane="one", batch_id=batch["id"])
    e.batches.update(batch["id"], plan_json='{"lanes":[{"id":"one","files":["one.txt"]}]}', status="planned")
    service = BatchService(project_repo=e.manager.project_repo, batches=e.batches, tasks=e.tasks,
                           task_service=e.service, sessions=e.manager)
    started = None

    async def handler(runtime, prompt):
        started.set_result(runtime.session_id)
        await asyncio.Event().wait()

    e.manager.handler = handler

    async def check():
        nonlocal started
        started = asyncio.get_running_loop().create_future()
        pending = asyncio.create_task(run(e, task))
        sid = await started
        before = e.batches.get(batch["id"])
        try:
            with pytest.raises(TaskError, match="仍有智能体"):
                if operation == "replace_plan":
                    await service.replace_plan(e.project, before, {"lanes": [{"id": "two", "files": ["other.txt"]}]})
                else:
                    await getattr(service, operation)(e.project, before)
            after = e.batches.get(batch["id"])
            assert after["status"] == before["status"] and after["plan_json"] == before["plan_json"]
            assert Path(task["worktree"]).exists() and e.team.descriptor(sid)["state"] == "running"
        finally:
            lead = e.team._lead(e.project["id"], e.owner)
            await e.team.stop(lead["session_id"], e.owner, sid)
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(check())


def test_discard_cannot_resurrect_batch_when_planning_is_still_running(isolated):
    e = isolated
    batch = e.batches.create(project_id=e.project["id"], user_id=e.owner, title="规划批次", requirement="需求", base_branch="main")
    service = BatchService(project_repo=e.manager.project_repo, batches=e.batches, tasks=e.tasks,
                           task_service=e.service, sessions=e.manager)
    started = None

    async def handler(runtime, prompt):
        started.set_result(runtime.session_id)
        await asyncio.Event().wait()

    e.manager.handler = handler

    async def check():
        nonlocal started
        started = asyncio.get_running_loop().create_future()
        pending = asyncio.create_task(service._ask(e.project, batch, "规划中"))
        sid = await started
        try:
            with pytest.raises(BatchError, match="规划.*仍在执行"):
                await service.discard(e.project, batch)
            assert e.batches.get(batch["id"])["status"] == "planning"
            assert e.team.descriptor(sid)["state"] == "running"
        finally:
            lead = e.team._lead(e.project["id"], e.owner)
            await e.team.stop(lead["session_id"], e.owner, sid)
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(check())


def test_parallel_local_limit_is_bounded_by_project_team_capacity(isolated, monkeypatch):
    e = isolated
    batch = e.batches.create(project_id=e.project["id"], user_id=e.owner, title="并发上限", requirement="需求", base_branch="main")
    first = task_for(e, lane="one", batch_id=batch["id"])
    second = task_for(e, lane="two", batch_id=batch["id"])
    e.batches.update(batch["id"], plan_json='{"lanes":[{"id":"one","files":["one.txt"]},{"id":"two","files":["two.txt"]}]}')
    service = BatchService(project_repo=e.manager.project_repo, batches=e.batches, tasks=e.tasks,
                           task_service=e.service, sessions=e.manager)
    active, peak = 0, 0

    async def handler(runtime, prompt):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return SimpleNamespace(text="真实调度已结束", stop_reason="end_turn", turns=1)
        finally:
            active -= 1

    async def review(project, task):
        e.tasks.update(task["id"], status="review", error="")
        return {"symbols": {"counts": {}}}

    e.manager.handler = handler
    monkeypatch.setattr(e.service, "review", review)
    result = asyncio.run(service.run(e.project, e.batches.get(batch["id"]), max_workers=5))
    assert result["ok"] and result["workers"] == result["max_concurrent"] == peak == 1
    assert {row["task_id"] for row in result["lanes"]} == {first["id"], second["id"]}


@pytest.mark.parametrize("batch_planning", [False, True])
def test_real_runtime_planner_schema_and_permission_policy_forbid_single_use_writes(isolated, batch_planning):
    e = isolated
    task = task_for(e)
    batch = e.batches.create(project_id=e.project["id"], user_id=e.owner, title="只读规划", requirement="需求", base_branch="main")
    checked = []

    class NamedTool(Tool):
        def __init__(self, name):
            self.name = name

        async def execute(self, arguments, ctx):
            raise AssertionError("policy test must not execute a tool")

    async def no_approvals(*args):
        raise AssertionError("planning cannot request single-use project writes")

    def configure_runtime(runtime):
        # Exercise the production SessionRuntime methods with a deterministic
        # scheduler worker, without constructing providers or an application.
        runtime.registry = ToolRegistry()
        for name in ("read_file", "git_read", "delegate_agent", "wait_agents", "write_file", "edit_file", "bash", "unknown_mutator"):
            runtime.registry.register(NamedTool(name))
        runtime.refresh_code_context = lambda: ""
        runtime.code_context_provider = None
        runtime._system_suffix = ""
        runtime.web_enabled = False
        runtime._route_obj = None
        runtime.strict = False
        runtime.extra_policies = {}
        runtime._strict_approver = no_approvals
        runtime._server_approver = no_approvals
        runtime._current_permission_mode = MethodType(SessionRuntime._current_permission_mode, runtime)

    async def inspect(runtime):
        configure_runtime(runtime)
        e.manager.set_permission_mode(runtime.session_id, e.owner, "full-access")
        assert e.team.planning_authority(runtime.session_id, e.owner)
        _, registry = SessionRuntime._loop_context(runtime)
        assert {"read_file", "git_read", "delegate_agent", "wait_agents"} <= set(registry.names())
        assert not {"write_file", "edit_file", "bash", "unknown_mutator"}.intersection(registry.names())
        permissions = SessionRuntime._make_permissions(runtime)
        assert not permissions.read_only_approval
        ctx = ToolContext(runtime.session_id, runtime.workspace, e.owner)
        assert (await permissions.evaluate("read_file", {"path": "main.txt"}, ctx)).decision is Decision.allow
        assert (await permissions.evaluate("write_file", {"path": "new.txt", "content": "bad"}, ctx)).decision is Decision.deny
        assert (await permissions.evaluate("bash", {"command": "echo bad"}, ctx)).decision is Decision.deny
        checked.append(runtime.session_id)

    async def handler(runtime, prompt):
        await inspect(runtime)
        if prompt == "只读根规划":
            child = await e.team.start(runtime.session_id, e.owner, "规划后代")
            await e.team.wait(runtime.session_id, e.owner, [child["session_id"]], 1)
        return SimpleNamespace(text="只读计划完成", stop_reason="end_turn", turns=1)

    e.manager.handler = handler

    async def check():
        if batch_planning:
            result = await e.team.run_batch_planner(e.project["id"], e.owner, batch["id"], "只读根规划")
        else:
            result = await run(e, task, "只读根规划", "read-only")
        assert result["state"] == "completed" and len(checked) == 2
        if not batch_planning:
            # Plan approval converts the existing session back into an
            # ordinary isolated executor, restoring its normal grant policy.
            async def execute(runtime, prompt):
                assert not e.team.planning_authority(runtime.session_id, e.owner)
                configure_runtime(runtime)
                assert SessionRuntime._make_permissions(runtime).read_only_approval
                _, registry = SessionRuntime._loop_context(runtime)
                assert registry.get("write_file") is not None
                return SimpleNamespace(text="批准后的执行已完成", stop_reason="end_turn", turns=1)

            e.manager.handler = execute
            approved = await run(e, e.tasks.get(task["id"]), "已批准执行", "workspace-write")
            assert approved["session_id"] == result["session_id"]
            assert e.manager.session_repo.get(approved["session_id"])["kind"] == "task"

    asyncio.run(check())
