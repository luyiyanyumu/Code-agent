"""Real offline SessionRuntime/team execution with per-turn budget metering."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from code_agent.agent.project_team import ProjectTeam
from code_agent.agent.router import ModelRouter
from code_agent.models.events import EventType, ModelTurn, TokenUsage, ToolCall
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin.tools_builtin import ReadFileTool, WriteFileTool
from code_agent.tools.registry import ToolRegistry


@pytest.fixture
def budget_runtime(agent_home, monkeypatch):
    monkeypatch.setattr("code_agent.sessions.manager.CLOSURE_LIMIT", 0)
    monkeypatch.setattr("code_agent.sessions.manager.MAX_CONSECUTIVE_WAKES", 0)
    configured = {"task_tokens": 0, "task_seconds": 0, "team_tokens": 0, "team_seconds": 0}
    monkeypatch.setattr("code_agent.settings.agent_team_budget_config", lambda: dict(configured))
    root = agent_home / "project"
    root.mkdir()
    (root / "fixture.txt").write_text("offline evidence", encoding="utf-8")
    db = Database(agent_home / "budget-runtime.db")
    user = UserRepo(db).create("budget-runtime", "test-only")["id"]
    projects = ProjectRepo(db)
    project = projects.create(user, "budget-runtime", str(root), str(root))
    projects.set_flags(project["id"], allow_direct_write=True)
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    manager = SessionManager(transcript=TranscriptStore(agent_home / "transcripts"), session_repo=SessionRepo(db),
                             approval_repo=ApprovalRepo(db), router=ModelRouter(provider_name="offline"),
                             registry=registry, project_repo=projects)
    team = ProjectTeam(manager)
    manager.agent_team = team
    lead = manager.create_project_session(user, project["id"], permission_mode="full-access")["id"]
    roster = team.sync(project["id"], user)
    assert roster["agents"] == [], "sync must not precreate workflow branches"
    workers = [team._create_worker(project, team._member(lead, user), tree="main", role="subagent", title=f"budget worker {index}")
               for index in range(2)]
    yield SimpleNamespace(manager=manager, team=team, user=user, lead=lead, roster=roster,
                          workers=workers, root=root, db=db, configured=configured)
    db.close()


def assign(runtime, provider):
    original = runtime._build_loop
    runtime._build_loop = lambda **kwargs: original(**{**kwargs, "provider": provider})


def returned(text="finished", tokens=1):
    return ModelTurn(text=text, usage=TokenUsage(input_tokens=tokens, output_tokens=0))


def read_turn(tokens):
    return ModelTurn(tool_calls=[ToolCall(name="read_file", arguments={"path": "fixture.txt"})],
                     usage=TokenUsage(input_tokens=tokens, output_tokens=0))


class HeldParent(OfflineProvider):
    """Keep a real parent turn active while the child joins its task chain."""

    def __init__(self, tokens, *, wake_tokens=0):
        super().__init__(script=[read_turn(tokens), returned("parent finished", 0),
                                 returned("reviewed receipt", wake_tokens), returned("must not request", 1)])
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def chat(self, messages, tools):
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        return await super().chat(messages, tools)


async def start(e, worker, provider, *, budget=None, prompt="offline bounded task", write_paths=None):
    runtime = e.manager.get_runtime(worker["session_id"], e.user)
    assign(runtime, provider)
    record = await e.team.followup(e.lead, e.user, worker["session_id"], prompt, budget=budget, write_paths=write_paths)
    return runtime, e.team._starts[record["session_id"]]


def test_task_budget_stops_next_model_request_after_actual_tool_turn_usage(budget_runtime):
    e = budget_runtime

    async def scenario():
        provider = OfflineProvider(script=[read_turn(7), returned("must not request", 3)])
        runtime, task = await start(e, e.workers[0], provider, budget={"max_tokens": 7})
        await asyncio.wait_for(task, 3)
        assert provider.calls == 1
        assert runtime.execution_budget_status()["tokens"] == 7
        assert runtime.execution_budget_status()["turns"] == 1
        results = [event for event in e.manager.transcript.replay(runtime.session_id) if event.type == EventType.tool_result]
        assert len(results) == 1 and "offline evidence" in results[0].payload["result"]
        member = e.team.descriptor(runtime.session_id)
        assert member["state"] == "failed"
        assert "Token 预算" in member["error"]
        assert not e.team._capacity(member["project_id"]).active
        await e.team.aclose()

    asyncio.run(scenario())


def test_successful_done_does_not_double_charge_actual_turn_usage(budget_runtime):
    e = budget_runtime

    async def scenario():
        provider = OfflineProvider(script=[read_turn(6), returned(tokens=4)])
        runtime, task = await start(e, e.workers[0], provider)
        await asyncio.wait_for(task, 3)
        status = runtime.execution_budget_status()
        assert status["tokens"] == status["team_tokens"] == 10
        assert status["turns"] == provider.calls == 2
        done = next(event for event in e.manager.transcript.replay(runtime.session_id) if event.type == EventType.done)
        assert done.payload["usage"]["input_tokens"] == 10
        assert e.team.descriptor(runtime.session_id)["state"] == "completed"
        await e.team.aclose()

    asyncio.run(scenario())


def test_parent_and_child_consume_the_same_team_scope_and_stop_at_team_limit(budget_runtime):
    e = budget_runtime
    e.configured["team_tokens"] = 16

    async def scenario():
        parent = e.manager.get_runtime(e.lead, e.user)
        parent_provider = HeldParent(7)
        assign(parent, parent_provider)
        parent_task = asyncio.create_task(parent.submit("start one team task"))
        await asyncio.wait_for(parent_provider.entered.wait(), 2)
        scope_id = parent.execution_budget_status()["scope_id"]
        provider = OfflineProvider(script=[read_turn(9), returned("must not request", 2)])
        child, task = await start(e, e.workers[0], provider)
        await asyncio.wait_for(task, 3)
        child_status, parent_status = child.execution_budget_status(), parent.execution_budget_status()
        assert child_status["scope_id"] == scope_id
        assert parent_status["scope_id"] == scope_id
        assert child_status["tokens"] == 9 and parent_status["tokens"] == 7
        assert child_status["team_tokens"] == parent_status["team_tokens"] == 16
        assert provider.calls == 1
        assert "团队任务 Token 总预算" in e.team.descriptor(child.session_id)["error"]
        parent_provider.release.set()
        await asyncio.wait_for(parent_task, 2)
        await e.team.aclose()

    asyncio.run(scenario())


def test_queue_time_expiry_never_calls_provider_and_removes_reservation(budget_runtime, monkeypatch):
    e = budget_runtime
    monkeypatch.setattr(e.team, "limits", lambda: {"max_parallel": 1, "max_depth": 3})

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Blocking(OfflineProvider):
            async def chat(self, messages, tools):
                self.calls += 1
                entered.set()
                await release.wait()
                return returned(tokens=0)

        owner, task1 = await start(e, e.workers[0], Blocking())
        await asyncio.wait_for(entered.wait(), 2)
        provider = OfflineProvider(script=[returned("must not request", 1)])
        queued, task2 = await start(e, e.workers[1], provider, budget={"max_seconds": 1})
        try:
            await asyncio.wait_for(task2, 3)
            assert provider.calls == 0
            member = e.team.descriptor(queued.session_id)
            assert member["state"] == "failed"
            assert "耗时预算" in member["error"]
            assert queued.session_id not in e.team._capacity(member["project_id"]).queue
            assert queued.session_id not in e.team._capacity(member["project_id"]).active
            assert queued.session_id not in e.team._starts
        finally:
            release.set()
            await asyncio.wait_for(task1, 2)
            await e.team.aclose()

    asyncio.run(scenario())


def test_time_budget_cancels_pending_approval_and_cannot_write_after_expiry(budget_runtime):
    e = budget_runtime
    e.manager.set_permission_mode(e.lead, e.user, "read-only")

    async def scenario():
        provider = OfflineProvider(script=[
            ModelTurn(tool_calls=[ToolCall(name="write_file", arguments={
                "path": "expired.txt", "content": "must not write",
                "_approval": {"purpose": "Create the deadline test fixture", "effects": ["Create expired.txt"]},
            })],
                      usage=TokenUsage(input_tokens=4, output_tokens=2, cached_tokens=1)),
            returned("must not request", 1),
        ])
        runtime, task = await start(e, e.workers[0], provider, budget={"max_seconds": 1},
                                    prompt="Create expired.txt with the fixture text after permission is approved",
                                    write_paths=["expired.txt"])
        async with asyncio.timeout(2):
            while runtime.pending_approval() is None:
                await asyncio.sleep(0.005)
        approval_id = runtime.pending_approval()["approval_id"]
        await asyncio.wait_for(task, 3)
        assert provider.calls == 1
        assert runtime.execution_budget_status()["tokens"] == 6
        assert not runtime._pending_approvals
        assert not e.manager.approval_repo.pending_for_session(runtime.session_id)
        assert runtime.pending_approval() is None
        assert not e.manager.decide_approval(e.user, approval_id, True)
        assert not (e.root / "expired.txt").exists()
        assert "耗时预算" in e.team.descriptor(runtime.session_id)["error"]
        assert not e.team._capacity(e.roster["lead"]["project_id"]).active
        await e.team.aclose()

    asyncio.run(scenario())


def test_completion_wake_keeps_scope_and_cannot_reset_exhausted_team_budget(budget_runtime):
    e = budget_runtime
    e.configured["team_tokens"] = 10

    async def scenario():
        parent = e.manager.get_runtime(e.lead, e.user)
        provider = HeldParent(3, wake_tokens=5)
        assign(parent, provider)
        parent_task = asyncio.create_task(parent.submit("start task"))
        await asyncio.wait_for(provider.entered.wait(), 2)
        scope = parent.execution_budget_status()["scope_id"]
        _, child_task = await start(e, e.workers[0], OfflineProvider(script=[returned(tokens=4)]))
        await asyncio.wait_for(child_task, 3)
        assert parent.execution_budget_status()["team_tokens"] == 7
        provider.release.set()
        await asyncio.wait_for(parent_task, 2)
        await parent._run_wake("review completed child's actual evidence")
        status = parent.execution_budget_status()
        assert status["scope_id"] == scope
        assert status["team_tokens"] == 12
        assert status["tokens"] == 5
        assert provider.calls == 3
        await parent._run_wake("one more receipt after total limit")
        assert provider.calls == 3, "A wake must not create a fresh team budget"
        assert parent.execution_budget_status()["scope_id"] == scope
        assert parent.execution_budget_status()["team_tokens"] == 12
        await e.team.aclose()

    asyncio.run(scenario())


def test_new_explicit_prompt_gets_fresh_deadline_after_previous_task_expires(budget_runtime):
    e = budget_runtime
    e.configured["task_seconds"] = 1

    async def scenario():
        runtime = e.manager.get_runtime(e.lead, e.user)
        provider = OfflineProvider(script=[returned("first task", 3), returned("new task", 5)])
        assign(runtime, provider)
        await runtime.submit("first request")
        old = runtime.execution_budget_status()
        # Move the previous task start into the past; no wall-clock sleep needed.
        runtime._execution_budget.current["started_at"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
        result = await runtime.submit("new explicit request")
        assert result.text == "new task"
        assert provider.calls == 2
        new = runtime.execution_budget_status()
        assert new["activation_id"] > old["activation_id"]
        assert new["scope_id"] != old["scope_id"]
        assert new["tokens"] == 5
        await e.team.aclose()

    asyncio.run(scenario())


def test_deleting_completed_child_does_not_refund_consumed_team_tokens(budget_runtime):
    e = budget_runtime
    e.configured["team_tokens"] = 10

    async def scenario():
        parent = e.manager.get_runtime(e.lead, e.user)
        provider = HeldParent(3)
        assign(parent, provider)
        parent_task = asyncio.create_task(parent.submit("start task"))
        await asyncio.wait_for(provider.entered.wait(), 2)
        child, task = await start(e, e.workers[0], OfflineProvider(script=[returned(tokens=7)]))
        await asyncio.wait_for(task, 3)
        assert parent.execution_budget_status()["team_tokens"] == 10
        # Use the real repository's hard-delete behavior; no shell/file cleanup.
        e.manager.session_repo.delete(child.session_id)
        assert parent.execution_budget_status()["team_tokens"] == 10
        with pytest.raises(RuntimeError, match="团队任务 Token 总预算"):
            await parent.before_model_turn()
        provider.release.set()
        await asyncio.wait_for(parent_task, 2)
        await e.team.aclose()

    asyncio.run(scenario())


def test_automatic_wake_after_runtime_recreation_cannot_reset_spent_team_scope(budget_runtime):
    e = budget_runtime
    e.configured["team_tokens"] = 7

    async def scenario():
        original = e.manager.get_runtime(e.lead, e.user)
        assign(original, OfflineProvider(script=[returned("original task", 7)]))
        await original.submit("original explicit task")
        scope = original.execution_budget_status()["scope_id"]
        assert original.execution_budget_status()["team_tokens"] == 7
        e.manager._runtimes.pop(e.lead)
        rebuilt = e.manager.get_runtime(e.lead, e.user)
        provider = OfflineProvider(script=[returned("must not request", 1)])
        assign(rebuilt, provider)
        await rebuilt._run_wake("completion receipt from the same task chain")
        assert provider.calls == 0, "Runtime recreation cannot grant a fresh automatic task budget"
        assert rebuilt.execution_budget_status()["scope_id"] == scope
        assert rebuilt.execution_budget_status()["team_tokens"] == 7
        await e.team.aclose()

    asyncio.run(scenario())
