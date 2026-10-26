"""Independent project windows must not share execution, approval or history."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import pytest

from code_agent.providers.offline import OfflineProvider
from tests.test_project_team_integration import integration, roster


def new_dialog(env, source_id):
    client, _, auth, project, _ = env
    response = client.post(f"/api/v1/code/projects/{project['id']}/conversations", headers=auth,
                           json={"from_session_id": source_id})
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


async def pending_approval(runtime, running):
    async def inspect():
        while True:
            for identity, waiter in runtime._pending_approvals.items():
                if not waiter.done():
                    return identity
            if running.done():
                await running
                pytest.fail("Expected a pending single-use approval")
            await asyncio.sleep(0.01)
    return await asyncio.wait_for(inspect(), 5)


class SeparateTasks(OfflineProvider):
    def __init__(self):
        super().__init__()
        self.steps = defaultdict(int)
        self.histories = defaultdict(list)

    async def chat(self, messages, tools):
        task = next(message["content"] for message in reversed(messages)
                    if message["role"] == "user" and message["content"] in ("alpha request", "beta request"))
        self.steps[task] += 1
        self.histories[task].append(json.dumps(messages, ensure_ascii=False))
        if self.steps[task] == 1:
            return self.tool_call_turn("read_file", {"path": "source.txt"})
        if self.steps[task] == 2:
            name = "alpha" if task.startswith("alpha") else "beta"
            return self.tool_call_turn("write_file", {"path": f"{name}.txt", "content": f"only {name}",
                "_approval": {"purpose": "验证对话窗口隔离", "effects": [f"创建临时 {name}.txt"]}})
        return self.text_turn(f"{task} finished")


def test_other_window_can_finish_while_one_waits_for_approval_without_lead_wakeup(integration):
    _, app, _, project, root = integration
    lead = roster(integration)["lead"]["session_id"]
    lead_events = app.state.transcript.replay(lead)
    first_id, second_id = new_dialog(integration, lead), new_dialog(integration, lead)
    app.state.projects.set_flags(project["id"], allow_direct_write=True)
    manager = app.state.sessions
    manager.set_permission_mode(second_id, project["user_id"], "workspace-write")
    first, second = (manager.get_runtime(identity, project["user_id"]) for identity in (first_id, second_id))
    for identity, runtime in ((first_id, first), (second_id, second)):
        member = app.state.project_agent_team.descriptor(identity)
        assert runtime.agent_team is app.state.project_agent_team
        assert member["role"] == "lead" and member["lead_session_id"] == identity
        assert member["parent_session_id"] == "" and identity != lead
    provider = SeparateTasks()
    app.state.router.resolve_default = lambda: provider

    async def verify():
        running = asyncio.create_task(first.submit("alpha request"))
        try:
            identity = await pending_approval(first, running)
            result = await asyncio.wait_for(second.submit("beta request"), 5)
            assert result.text == "beta request finished"
            assert not running.done() and identity in first._pending_approvals
            assert not (root / "alpha.txt").exists()
            assert (root / "beta.txt").read_text() == "only beta"
            assert first.decide_approval(identity, True)
            assert (await asyncio.wait_for(running, 5)).text == "alpha request finished"
        finally:
            if not running.done():
                first.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(verify())
    assert (root / "alpha.txt").read_text() == "only alpha"
    for own, other in (("alpha", "beta"), ("beta", "alpha")):
        assert all(f"{other} request" not in history for history in provider.histories[f"{own} request"])
    assert app.state.transcript.replay(lead) == lead_events
    assert app.state.projects.get(project["id"])["session_id"] == lead
    runs = app.state.db.query_all("SELECT session_id,prompt,state FROM project_agent_runs WHERE session_id IN (?,?)", (first_id, second_id))
    assert {(row["session_id"], row["prompt"], row["state"]) for row in runs} == {
        (first_id, "alpha request", "completed"), (second_id, "beta request", "completed"),
    }
    assert len(runs) == 2
    assert not app.state.db.query_all("SELECT * FROM project_agent_runs WHERE session_id=?", (lead,))
    assert app.state.audit_repo.verify()["ok"]


def test_cancelling_one_window_does_not_reject_another_windows_approval(integration):
    _, app, _, project, root = integration
    lead = roster(integration)["lead"]["session_id"]
    lead_events = app.state.transcript.replay(lead)
    first_id, second_id = new_dialog(integration, lead), new_dialog(integration, lead)
    manager = app.state.sessions
    first, second = (manager.get_runtime(identity, project["user_id"]) for identity in (first_id, second_id))
    # A single shared provider tracks each prompt independently; no real model.
    provider = SeparateTasks()
    app.state.router.resolve_default = lambda: provider

    async def verify():
        first_task = asyncio.create_task(first.submit("alpha request"))
        second_task = asyncio.create_task(second.submit("beta request"))
        try:
            first_approval, second_approval = await asyncio.gather(
                pending_approval(first, first_task), pending_approval(second, second_task))
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first_task, 5)
            assert not second_task.done() and second_approval in second._pending_approvals
            assert app.state.approvals.get(first_approval)["status"] == "denied"
            assert app.state.approvals.get(second_approval)["status"] == "pending"
            assert second.decide_approval(second_approval, True)
            assert (await asyncio.wait_for(second_task, 5)).text == "beta request finished"
        finally:
            for runtime, task in ((first, first_task), (second, second_task)):
                if not task.done():
                    runtime.cancel()
            await asyncio.gather(first_task, second_task, return_exceptions=True)

    asyncio.run(verify())
    assert not (root / "alpha.txt").exists()
    assert (root / "beta.txt").read_text() == "only beta"
    assert app.state.sessions_repo.get(first_id)["status"] == "active"
    assert app.state.sessions_repo.get(second_id)["status"] == "active"
    runs = app.state.db.query_all("SELECT session_id,state FROM project_agent_runs WHERE session_id IN (?,?)", (first_id, second_id))
    assert {(row["session_id"], row["state"]) for row in runs} == {(first_id, "cancelled"), (second_id, "completed")}
    assert len(runs) == 2
    assert app.state.transcript.replay(lead) == lead_events
    assert app.state.projects.get(project["id"])["session_id"] == lead
