"""Lost admission ends real offline runs; all ancestors gate new execution."""

import asyncio

import pytest

from code_agent.agent.project_team import TeamError
from code_agent.providers.offline import OfflineProvider
from code_agent.tools.builtin.agent_team_tools import register_agent_team_tools
from test_team_budget_integration import assign, budget_runtime, returned


def descendant(e, parent, title):
    project = e.manager.project_repo.get(e.roster["lead"]["project_id"])
    return e.team._create_worker(project, e.team._member(parent["session_id"], e.user),
                                 tree="main", role="subagent", title=title)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.002)


def assert_released(e):
    capacity = e.team._capacity(e.roster["lead"]["project_id"])
    assert not capacity.active
    assert not capacity.queue
    assert not e.team._approval_suspensions
    assert not e.team._executions
    assert not e.team._starts


@pytest.mark.parametrize("invalidation", ["depth", "depth_after_admission", "workspace"])
def test_wait_readmission_failure_ends_activation_before_more_model_or_tools(budget_runtime, monkeypatch, invalidation):
    e = budget_runtime
    limits = {"max_depth": 3, "max_parallel": 2}
    monkeypatch.setattr(e.team, "limits", lambda: dict(limits))
    parent = descendant(e, e.workers[0], "depth two parent")
    child = descendant(e, parent, "depth three child")
    register_agent_team_tools(e.manager.registry, e.team)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Held(OfflineProvider):
            async def chat(self, messages, tools):
                entered.set()
                await release.wait()
                return await super().chat(messages, tools)

        child_runtime = e.manager.get_runtime(child["session_id"], e.user)
        assign(child_runtime, Held(script=[returned("child finished", 0)]))
        runtime = e.manager.get_runtime(parent["session_id"], e.user)
        provider = OfflineProvider(script=[
            OfflineProvider.tool_call_turn("wait_agents", {"session_ids": [child["session_id"]], "timeout_seconds": 3}),
            OfflineProvider.tool_call_turn("write_file", {"path": "after-wait.txt", "content": "must not execute"}),
            returned("must not complete", 0),
        ])
        assign(runtime, provider)
        try:
            await e.team.followup(e.lead, e.user, child["session_id"], "hold child", write_paths=[])
            child_task = e.team._starts[child["session_id"]]
            await asyncio.wait_for(entered.wait(), 2)
            await e.team.followup(e.lead, e.user, parent["session_id"], "wait then write", write_paths=["after-wait.txt"])
            parent_task = e.team._starts[parent["session_id"]]
            await until(lambda: e.team.descriptor(parent["session_id"])["state"] == "waiting")
            assert parent["session_id"] not in e.team._capacity(parent["project_id"]).active
            if invalidation == "depth":
                limits["max_depth"] = 1
            elif invalidation == "depth_after_admission":
                acquire = e.team._acquire

                async def lower_depth_after_admission(row, activation):
                    await acquire(row, activation)
                    if row["session_id"] == parent["session_id"]:
                        limits["max_depth"] = 1

                monkeypatch.setattr(e.team, "_acquire", lower_depth_after_admission)
            else:
                changed = e.root.parent / "changed-workspace"
                changed.mkdir()
                e.db.execute("UPDATE sessions SET workspace_override=? WHERE id=?", (str(changed), parent["session_id"]))
            release.set()
            await asyncio.wait_for(asyncio.gather(parent_task, child_task), 3)
            member = e.team.descriptor(parent["session_id"])
            assert member["state"] == "failed"
            assert "重新取得团队执行名额失败" in member["error"]
            assert ("最大递归深度" if invalidation.startswith("depth") else "工作区") in member["error"]
            assert provider.calls == 1
            assert not (e.root / "after-wait.txt").exists()
            assert not runtime.busy
            assert_released(e)
        finally:
            release.set()
            await e.team.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("approved", [True, False])
def test_approval_readmission_depth_failure_is_fatal_even_when_grant_is_denied(budget_runtime, monkeypatch, approved):
    e = budget_runtime
    limits = {"max_depth": 3, "max_parallel": 1}
    monkeypatch.setattr(e.team, "limits", lambda: dict(limits))
    e.manager.set_permission_mode(e.lead, e.user, "read-only")
    parent = descendant(e, e.workers[0], "approval at depth two")

    async def scenario():
        runtime = e.manager.get_runtime(parent["session_id"], e.user)
        provider = OfflineProvider(script=[
            OfflineProvider.tool_call_turn("write_file", {"path": "approved-after-lost-slot.txt", "content": "must not execute",
                "_approval": {"purpose": "验证审批恢复仍服从调度限制", "effects": ["尝试创建临时测试文件"]}}),
            OfflineProvider.tool_call_turn("write_file", {"path": "after-approval.txt", "content": "must not execute"}),
            returned("must not complete", 0),
        ])
        assign(runtime, provider)
        try:
            await e.team.followup(e.lead, e.user, parent["session_id"], "write with approval")
            task = e.team._starts[parent["session_id"]]
            await until(lambda: runtime.pending_approval() is not None
                        and parent["session_id"] not in e.team._capacity(parent["project_id"]).active)
            limits["max_depth"] = 1
            assert runtime.decide_approval(runtime.pending_approval()["approval_id"], approved)
            await asyncio.wait_for(task, 3)
            member = e.team.descriptor(parent["session_id"])
            assert member["state"] == "failed"
            assert "重新取得团队执行名额失败" in member["error"]
            assert "最大递归深度" in member["error"]
            assert provider.calls == 1
            assert not (e.root / "approved-after-lost-slot.txt").exists()
            assert not (e.root / "after-approval.txt").exists()
            assert not runtime._pending_approvals
            assert not runtime._approval_capacity_suspended
            assert_released(e)
        finally:
            await e.team.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["archive", "close", "retire"])
def test_all_execution_paths_reject_inactive_intermediate_ancestor_but_history_remains_readable(budget_runtime, monkeypatch, action):
    e = budget_runtime
    monkeypatch.setattr(e.team, "limits", lambda: {"max_depth": 4, "max_parallel": 2})
    ancestor = e.workers[0]
    middle = descendant(e, ancestor, "middle")
    leaf = descendant(e, middle, "deepest")
    runtime = e.manager.get_runtime(leaf["session_id"], e.user)
    provider = OfflineProvider(script=[
        OfflineProvider.tool_call_turn("write_file", {"path": "after-ancestor-ended.txt", "content": "must not execute"}),
        returned("must not complete", 0),
    ])
    assign(runtime, provider)
    if action == "retire":
        e.db.execute("UPDATE project_agents SET state='retired' WHERE session_id=?", (ancestor["session_id"],))
    else:
        getattr(e.manager, action)(ancestor["session_id"], e.user)

    async def scenario():
        try:
            with pytest.raises(TeamError, match="祖先已不可用"):
                await e.team.followup(e.lead, e.user, leaf["session_id"], "try followup")
            with pytest.raises(TeamError, match="祖先已不可用"):
                await e.team.start(leaf["session_id"], e.user, "try delegation")
            with pytest.raises(TeamError, match="祖先已不可用"):
                await e.team.start(e.lead, e.user, "try delegation under leaf", parent_id=leaf["session_id"])
            with pytest.raises(TeamError, match="祖先已不可用"):
                await runtime.submit("try direct submission")
            assert e.team._member(leaf["session_id"], e.user, live=False)["session_id"] == leaf["session_id"]
            history = await e.team.wait(e.lead, e.user, [leaf["session_id"]], 0)
            assert history["agents"][0]["activation_id"] == 0
            assert provider.calls == 0
            assert not (e.root / "after-ancestor-ended.txt").exists()
            assert_released(e)
        finally:
            await e.team.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["cycle", "binding", "wrong_lead"])
def test_intermediate_ancestor_corruption_fails_closed_without_blocking_history(budget_runtime, corruption):
    e = budget_runtime
    ancestor = e.workers[0]
    middle = descendant(e, ancestor, "middle")
    leaf = descendant(e, middle, "leaf")
    if corruption == "cycle":
        e.db.execute("UPDATE project_agents SET parent_session_id=? WHERE session_id=?",
                     (middle["session_id"], ancestor["session_id"]))
    elif corruption == "binding":
        e.db.execute("UPDATE sessions SET tree_id='task:changed' WHERE id=?", (ancestor["session_id"],))
    else:
        e.db.execute("UPDATE project_agents SET lead_session_id=? WHERE session_id=?",
                     (e.workers[1]["session_id"], ancestor["session_id"]))
    with pytest.raises(TeamError):
        e.team._member(leaf["session_id"], e.user)
    assert e.team._member(leaf["session_id"], e.user, live=False)["activation_id"] == 0
