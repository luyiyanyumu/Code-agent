"""Fresh run budgets and scheduler admission use the same exact activation."""

import asyncio

import pytest

from code_agent import settings
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.team_budget import ExecutionBudgetExceeded
from test_team_runtime_delivery import assign_provider, environment


def test_new_direct_prompt_does_not_inherit_expired_previous_timer(environment):
    manager, team, user, lead, _, _ = environment
    settings.save_agent_team_budgets({"task_seconds": 1})
    runtime = manager.get_runtime(lead, user)
    assign_provider(runtime, OfflineProvider(script=[OfflineProvider.text_turn("first"), OfflineProvider.text_turn("second")]))

    async def scenario():
        assert (await runtime.submit("first task")).text == "first"
        old = runtime.execution_budget_status()
        runtime._execution_budget.current["started_at"] = "2000-01-01T00:00:00+00:00"
        assert (await runtime.submit("new user task")).text == "second"
        fresh = runtime.execution_budget_status()
        assert fresh["activation_id"] == old["activation_id"] + 1
        assert fresh["scope_id"] != old["scope_id"]
        assert fresh["started_at"] != "2000-01-01T00:00:00+00:00"
        await team.aclose()

    asyncio.run(scenario())


def test_human_followup_does_not_inherit_idle_lead_expired_team_scope(environment):
    manager, team, user, lead, roster, _ = environment
    settings.save_agent_team_budgets({"team_seconds": 1})
    runtime = manager.get_runtime(lead, user)
    assign_provider(runtime, OfflineProvider(script=[OfflineProvider.text_turn("first")]))
    child = roster["agents"][0]["session_id"]
    child_runtime = manager.get_runtime(child, user)
    assign_provider(child_runtime, OfflineProvider(script=[OfflineProvider.text_turn("followup")]))

    async def scenario():
        await runtime.submit("completed user task")
        old_scope = runtime.execution_budget_status()["scope_id"]
        manager.session_repo.db.execute("UPDATE team_execution_scopes SET created_at=? WHERE id=?",
                                        ("2000-01-01T00:00:00+00:00", old_scope))
        assert not runtime.busy
        await team.followup(lead, user, child, "new human continuation")
        assert child_runtime.execution_budget_status()["scope_id"] != old_scope
        result = await team.wait(lead, user, [child], 1)
        assert result["agents"][0]["state"] == "completed"
        assert result["agents"][0]["result"] == "followup"
        await team.aclose()

    asyncio.run(scenario())


def test_direct_queued_submission_expires_before_any_model_request(environment, monkeypatch):
    manager, team, user, lead, roster, _ = environment
    monkeypatch.setattr("code_agent.agent.project_team.settings.agent_team_config", lambda: {"max_parallel": 1, "max_depth": 3})
    first, second = (row["session_id"] for row in roster["agents"][:2])
    assert first != second

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Hold(OfflineProvider):
            async def chat(self, messages, tools):
                entered.set()
                await release.wait()
                return self.text_turn("released")

        first_runtime = manager.get_runtime(first, user)
        second_runtime = manager.get_runtime(second, user)
        assign_provider(first_runtime, Hold())
        second_provider = OfflineProvider(script=[OfflineProvider.text_turn("must not execute")])
        assign_provider(second_runtime, second_provider)
        occupied = asyncio.create_task(first_runtime.submit("occupy unlimited slot"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            settings.save_agent_team_budgets({"task_seconds": 1})
            with pytest.raises(ExecutionBudgetExceeded, match="等待团队执行名额"):
                await asyncio.wait_for(second_runtime.submit("bounded direct queue"), 2)
            assert second_provider.calls == 0
            assert team.descriptor(second)["state"] == "failed"
            assert second not in team._capacity(roster["lead"]["project_id"]).queue
            assert first in team._capacity(roster["lead"]["project_id"]).active
            release.set()
            await occupied
        finally:
            release.set()
            await asyncio.gather(occupied, return_exceptions=True)
            await team.aclose()

    asyncio.run(scenario())


def test_deadline_during_approval_requeue_clears_suspension_for_next_activation(environment, monkeypatch):
    manager, team, user, lead, roster, root = environment
    monkeypatch.setattr(team, "limits", lambda: {"max_parallel": 1, "max_depth": 3})
    first, second = roster["agents"][:2]

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Hold(OfflineProvider):
            async def chat(self, messages, tools):
                entered.set()
                await release.wait()
                return self.text_turn("released")

        writer = manager.get_runtime(first["session_id"], user)
        occupier = manager.get_runtime(second["session_id"], user)
        writer_builder = writer._build_loop
        assign_provider(writer, OfflineProvider(script=[
            OfflineProvider.tool_call_turn("write_file", {"path": "queued-grant.txt", "content": "once", "_approval": {
                "purpose": "验证审批后重新排队受任务时间预算限制", "effects": ["尝试创建临时 queued-grant.txt"]}}),
            OfflineProvider.text_turn("finished"),
        ]))
        assign_provider(occupier, Hold())
        await team.followup(lead, user, first["session_id"], "bounded approval", budget={"max_seconds": 1})
        try:
            async with asyncio.timeout(2):
                while writer.pending_approval() is None or first["session_id"] in team._capacity(first["project_id"]).active:
                    await asyncio.sleep(0.002)
            await team.followup(lead, user, second["session_id"], "hold slot")
            await asyncio.wait_for(entered.wait(), 1)
            writer.decide_approval(writer.pending_approval()["approval_id"], True)
            async with asyncio.timeout(2):
                while team.descriptor(first["session_id"])["state"] in ("running", "queued", "waiting"):
                    await asyncio.sleep(0.005)
            assert team.descriptor(first["session_id"])["state"] == "failed"
            assert not (root / "queued-grant.txt").exists()
            assert not writer._pending_approvals
            release.set()
            await team.wait(lead, user, [second["session_id"]], 1)
            writer._build_loop = writer_builder
            assign_provider(writer, OfflineProvider(script=[
                OfflineProvider.tool_call_turn("write_file", {"path": "new-approval.txt", "content": "new task", "_approval": {
                    "purpose": "验证新执行编号能重新申请审批", "effects": ["尝试创建临时 new-approval.txt"]}}),
                OfflineProvider.text_turn("finished"),
            ]))
            await team.followup(lead, user, first["session_id"], "new activation approval")
            async with asyncio.timeout(2):
                while writer.pending_approval() is None:
                    await asyncio.sleep(0.002)
            assert first["session_id"] not in team._capacity(first["project_id"]).active
            writer.decide_approval(writer.pending_approval()["approval_id"], False)
            await team.wait(lead, user, [first["session_id"]], 1)
        finally:
            release.set()
            await team.aclose()

    asyncio.run(scenario())
