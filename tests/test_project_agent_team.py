"""Project team identities, runtime guards, scheduling and audit evidence."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent.agent.project_team import ProjectTeam, TeamError
from code_agent.api.agent_team_api import mount_agent_team_api
from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel.projects import ProjectService
from code_agent.codeintel.sop_workflow import WorkflowDocument, WorkflowStore, default_workflow
from code_agent.storage.db import Database, ProjectRepo, SessionRepo, TaskRepo, UserRepo
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.agent_team_tools import register_agent_team_tools
from code_agent.tools.registry import ToolRegistry


class FakeRuntime:
    """Same submit hook as the real runtime; provider work is deterministic."""
    def __init__(self, manager, meta):
        self.manager = manager
        self.session_id = meta["id"]
        self.user_id = meta["user_id"]
        self.workspace = manager.workspace_for(meta)
        self.permission_mode = meta["permission_mode"]
        self._task = None
        self.notes = []
        self.prompts = []

    @property
    def busy(self):
        return self._task is not None and not self._task.done()

    async def submit(self, prompt, attachments=None, code_context=None):
        if self.busy:
            raise TeamError("会话正在处理上一个请求")
        self._task = asyncio.create_task(self.manager.team.run(self, prompt, attachments, code_context))
        return await self._task

    async def _run_loop(self, prompt, attachments=None, code_context=None, **kwargs):
        self.prompts.append(prompt)
        if self.manager.handler:
            return await self.manager.handler(self, prompt, attachments, code_context, kwargs)
        await asyncio.sleep(0)
        return SimpleNamespace(text=f"done: {prompt}", stop_reason="end_turn")

    def cancel(self):
        self.manager.team.cancel_for(self.session_id)
        if self.busy:
            self._task.cancel()

    def enqueue_team_note(self, note):
        self.notes.append(note)


class FakeManager:
    def __init__(self, db):
        self.session_repo = SessionRepo(db)
        self.project_repo = ProjectRepo(db)
        self._runtimes = {}
        self.handler = None
        self.team = None

    def workspace_for(self, meta):
        from pathlib import Path
        return Path(meta.get("workspace_override") or self.project_repo.get(meta["project_id"])["root_path"])

    def create_project_session(self, user_id, project_id, *, permission_mode="read-only", reuse=True):
        project = self.project_repo.get(project_id)
        meta = self.session_repo.get(project["session_id"]) if project["session_id"] else None
        if reuse and meta and meta["status"] == "active":
            return meta
        meta = self.session_repo.create(user_id, title="Lead", project_id=project_id, tree_id="main", kind="main", permission_mode=permission_mode)
        self.project_repo.set_session(project_id, meta["id"])
        return meta

    def get_runtime(self, session_id, user_id):
        meta = self.session_repo.get(session_id)
        if not meta or meta["user_id"] != user_id:
            raise KeyError("会话不存在或无权访问")
        if meta["status"] != "active":
            raise TeamError("会话已关闭")
        if session_id not in self._runtimes:
            self._runtimes[session_id] = FakeRuntime(self, meta)
        return self._runtimes[session_id]


@pytest.fixture
def team_env(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "source.txt").write_text("untouched", encoding="utf-8")
    db = Database(tmp_path / "platform" / "db.sqlite")
    users = UserRepo(db)
    owner = users.create("owner", "unused")["id"]
    stranger = users.create("stranger", "unused")["id"]
    manager = FakeManager(db)
    project = manager.project_repo.create(owner, "project", str(root), str(root))
    store = WorkflowStore(db)
    limits = {"max_depth": 3, "max_parallel": 2}
    monkeypatch.setattr("code_agent.agent.project_team.settings.agent_team_config", lambda: dict(limits))
    monkeypatch.setattr("code_agent.agent.project_team.settings.permission_mode", lambda: "workspace-write")
    team = ProjectTeam(manager, store)
    manager.team = team
    yield SimpleNamespace(root=root, db=db, manager=manager, project=project, pid=project["id"], owner=owner,
                          stranger=stranger, store=store, team=team, limits=limits, tmp_path=tmp_path)
    db.close()


def nodes(env, tree="main"):
    """Explicit task fixtures; sync itself must never preallocate workflow agents."""
    snapshot = env.team.sync(env.pid, env.owner, tree)
    owners = {row["node_id"]: row for row in snapshot["agents"]
              if row["role"] == "subagent" and row["tree_id"] == tree and row["node_id"]}
    for node in default_workflow()["nodes"]:
        if node["id"] not in owners:
            owners[node["id"]] = env.team._create_worker(
                env.project, snapshot["lead"], tree=tree, role="subagent", title=node["title"], node_id=node["id"])
    return snapshot["lead"], owners


async def fixture_task(env, caller, user, prompt, *, node_id, tree="main", **kwargs):
    """Reuse an explicitly prepared task for scheduling/permission regressions."""
    _, owners = nodes(env, tree)
    return await env.team.followup(caller, user, owners[node_id]["session_id"], prompt, **kwargs)


def save_nodes(env, document):
    data = copy.deepcopy(document)
    data.pop("revision", None)
    data.pop("updated_at", None)
    revision = env.store.get(env.pid, "main")["revision"]
    env.store.save(env.pid, "main", WorkflowDocument.model_validate(data), revision)


async def settled(env, session_ids):
    for _ in range(300):
        if all(env.team.descriptor(sid)["state"] not in ("queued", "running", "waiting") for sid in session_ids):
            return
        await asyncio.sleep(0.002)
    raise AssertionError("test worker did not settle")


def test_unique_lead_without_preallocated_branches_models_or_project_writes(team_env):
    e = team_env
    first = e.team.sync(e.pid, e.owner)
    second = e.team.sync(e.pid, e.owner)
    assert first == second
    assert first["lead"]["depth"] == 0
    assert first["agents"] == []
    assert e.manager._runtimes == {}
    assert e.db.query_all("SELECT * FROM project_agent_runs") == []
    assert list(e.root.iterdir()) == [e.root / "source.txt"]
    assert (e.root / "source.txt").read_text() == "untouched"


def test_extra_main_session_cannot_become_second_lead(team_env):
    e = team_env
    lead, _ = nodes(e)
    meta = e.manager.session_repo.create(e.owner, project_id=e.pid, tree_id="main", kind="main")
    assert e.team.bind_lead(meta)["session_id"] == lead["session_id"]
    assert len(e.db.query_all("SELECT * FROM project_agents WHERE role='lead' AND state!='retired'")) == 1
    assert e.team.descriptor(meta["id"]) is None


def test_cold_legacy_main_open_uses_valid_project_canonical_pointer(team_env):
    e = team_env
    legacy = e.manager.session_repo.create(e.owner, project_id=e.pid, tree_id="main", kind="main", title="older history")
    current = e.manager.session_repo.create(e.owner, project_id=e.pid, tree_id="main", kind="main", title="current project dialog")
    e.manager.project_repo.set_session(e.pid, current["id"])
    lead = e.team.bind_lead(legacy)
    assert lead["session_id"] == current["id"]
    assert e.team.descriptor(legacy["id"]) is None
    assert e.manager.project_repo.get(e.pid)["session_id"] == current["id"]


def test_wrong_project_session_pointer_cannot_change_lead_workspace(team_env):
    e = team_env
    other_root = e.tmp_path / "other"
    other_root.mkdir()
    other = e.manager.project_repo.create(e.owner, "other", str(other_root), str(other_root))
    wrong = e.manager.session_repo.create(e.owner, project_id=other["id"], tree_id="main", kind="main")
    intended = e.manager.session_repo.create(e.owner, project_id=e.pid, tree_id="main", kind="main")
    e.manager.project_repo.set_session(e.pid, wrong["id"])
    lead = e.team.bind_lead(intended)
    assert lead["session_id"] == intended["id"]
    assert e.manager.project_repo.get(e.pid)["session_id"] == intended["id"]
    assert e.team.descriptor(wrong["id"]) is None


def test_closed_lead_replacement_reparents_existing_node_identity(team_env):
    e = team_env
    lead, owners = nodes(e)
    e.manager.session_repo.close(lead["session_id"])
    new_lead, new_owners = nodes(e)
    assert new_lead["session_id"] != lead["session_id"]
    assert {key: row["session_id"] for key, row in owners.items()} == {key: row["session_id"] for key, row in new_owners.items()}
    assert all(row["parent_session_id"] == new_lead["session_id"] for row in new_owners.values())


def test_workflow_remove_and_undo_leave_dynamic_tasks_and_history_unchanged(team_env):
    e = team_env
    lead, owners = nodes(e)
    sid = owners["requirements"]["session_id"]
    runtime = e.manager.get_runtime(sid, e.owner)
    runtime.prompts.append("existing transcript")
    original = default_workflow()
    removed = copy.deepcopy(original)
    removed["nodes"] = [row for row in removed["nodes"] if row["id"] != "requirements"]
    removed["edges"] = [row for row in removed["edges"] if "requirements" not in (row["source"], row["target"])]
    save_nodes(e, removed)
    e.team.sync(e.pid, e.owner)
    assert e.team.descriptor(sid)["state"] == "idle"
    save_nodes(e, original)
    _, revived = nodes(e)
    assert revived["requirements"]["session_id"] == sid
    assert revived["requirements"]["state"] == "idle"
    assert revived["requirements"]["activation_id"] == owners["requirements"]["activation_id"]
    assert runtime.prompts == ["existing transcript"]


def test_workflow_rename_does_not_rename_or_replace_existing_dynamic_task(team_env):
    e = team_env
    _, owners = nodes(e)
    document = default_workflow()
    document["nodes"][0]["title"] = "新需求负责人"
    save_nodes(e, document)
    _, renamed = nodes(e)
    assert renamed["requirements"]["session_id"] == owners["requirements"]["session_id"]
    assert renamed["requirements"]["title"] == owners["requirements"]["title"]


def test_legacy_node_hint_starts_independent_tasks_and_reuse_requires_followup(team_env):
    e = team_env
    lead = e.team.sync(e.pid, e.owner)["lead"]

    async def main():
        first = await e.team.start(lead["session_id"], e.owner, "first requirement", node_id="requirements")
        await settled(e, [first["session_id"]])
        second = await e.team.start(lead["session_id"], e.owner, "another requirement", node_id="requirements")
        await settled(e, [second["session_id"]])
        assert first["session_id"] != second["session_id"]
        assert first["role"] == second["role"] == "subagent"
        assert first["node_id"] == second["node_id"] == ""
        assert first["activation_id"] == second["activation_id"] == 1
        reused = await e.team.followup(lead["session_id"], e.owner, first["session_id"], "continue first task")
        await settled(e, [reused["session_id"]])
        assert reused["session_id"] == first["session_id"] and reused["activation_id"] == 2
        await e.team.aclose()

    asyncio.run(main())


def test_delegation_is_nonblocking_and_reports_real_result_with_prompt(team_env):
    e = team_env
    lead, owners = nodes(e)

    async def main():
        gate = asyncio.Event()

        async def handler(runtime, prompt, *args):
            await gate.wait()
            return SimpleNamespace(text="tests passed: source.txt:1", stop_reason="end_turn")

        e.manager.handler = handler
        started = await fixture_task(e, lead["session_id"], e.owner, "verify source", node_id="verification")
        assert started["state"] == "queued" and started["activation_id"] == 1
        await asyncio.sleep(0)
        gate.set()
        await settled(e, [started["session_id"]])
        result = e.team.descriptor(started["session_id"])
        assert result["state"] == "completed" and "tests passed" in result["result"]
        assert result["audit"]["decision"] == "pending"
        assert e.manager.get_runtime(started["session_id"], e.owner).prompts == ["verify source"]
        notice = e.manager.get_runtime(lead["session_id"], e.owner).notes[0]
        assert notice["activation_id"] == 1
        assert notice["audit_required"] is False and notice["audit_available"] is True
        assert e.store.get(e.pid, "main")["nodes"][5]["status"] == "pending"
        assert e.db.query_one("SELECT prompt FROM project_agent_runs WHERE session_id=?", (started["session_id"],))["prompt"] == "verify source"
        await e.team.aclose()

    asyncio.run(main())


def test_global_parallel_cap_includes_direct_and_delegated_nodes(team_env):
    e = team_env
    lead, owners = nodes(e)

    async def main():
        gate = asyncio.Event()
        entered = []

        async def handler(runtime, *args):
            entered.append(runtime.session_id)
            assert len(e.team._capacity(e.pid).active) <= 2
            await gate.wait()
            return SimpleNamespace(text="ok", stop_reason="end_turn")

        e.manager.handler = handler
        ids = list(owners)
        first = await fixture_task(e, lead["session_id"], e.owner, "a", node_id=ids[0])
        direct_runtime = e.manager.get_runtime(owners[ids[1]]["session_id"], e.owner)
        direct = asyncio.create_task(direct_runtime.submit("direct REST-equivalent submit"))
        third = await fixture_task(e, lead["session_id"], e.owner, "c", node_id=ids[2])
        for _ in range(8):
            await asyncio.sleep(0)
        assert len(entered) == 2
        assert e.team.descriptor(third["session_id"])["state"] == "queued"
        gate.set()
        await direct
        await settled(e, [first["session_id"], third["session_id"]])
        assert len(entered) == 3
        assert e.team._capacity(e.pid).active == set()
        await e.team.aclose()

    asyncio.run(main())


def test_lead_does_not_consume_descendant_parallel_slot(team_env):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, owners = nodes(e)

    async def main():
        entered = set()
        gate = asyncio.Event()

        async def handler(runtime, *args):
            entered.add(e.team.descriptor(runtime.session_id)["role"])
            await gate.wait()
            return SimpleNamespace(text="ok", stop_reason="end_turn")

        e.manager.handler = handler
        lead_task = asyncio.create_task(e.manager.get_runtime(lead["session_id"], e.owner).submit("supervise"))
        child = await fixture_task(e, lead["session_id"], e.owner, "work", node_id="requirements")
        for _ in range(8):
            await asyncio.sleep(0)
        assert entered == {"lead", "subagent"}
        assert len(e.team._capacity(e.pid).active) == 1
        gate.set()
        await lead_task
        await settled(e, [child["session_id"]])
        await e.team.aclose()

    asyncio.run(main())


def test_recursive_wait_releases_capacity_and_cannot_deadlock_at_one(team_env):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, owners = nodes(e)
    observed = []

    async def main():
        async def handler(runtime, prompt, *args):
            row = e.team.descriptor(runtime.session_id)
            observed.append((row["depth"], len(e.team._capacity(e.pid).active)))
            if row["depth"] < 3:
                child = await e.team.start(runtime.session_id, e.owner, f"depth {row['depth'] + 1}", title="recursive work")
                completed = await e.team.wait(runtime.session_id, e.owner, [child["session_id"]], 1)
                assert completed["agents"][0]["state"] == "completed"
                assert runtime.session_id in e.team._capacity(e.pid).active
                assert not completed["timed_out"]
            return SimpleNamespace(text=f"done at {row['depth']}", stop_reason="end_turn")

        e.manager.handler = handler
        child = await fixture_task(e, lead["session_id"], e.owner, "root work", node_id="implementation")
        await asyncio.wait_for(settled(e, [child["session_id"]]), 2)
        assert e.team.descriptor(child["session_id"])["state"] == "completed"
        assert observed == [(1, 1), (2, 1), (3, 1)]
        assert e.team._capacity(e.pid).active == set()
        await e.team.aclose()

    asyncio.run(main())


def test_depth_limit_cannot_be_overridden_by_prompt_or_direct_submit(team_env):
    e = team_env
    e.limits["max_depth"] = 1
    lead, owners = nodes(e)

    async def main():
        with pytest.raises(TeamError, match="最大递归深度"):
            await e.team.start(owners["requirements"]["session_id"], e.owner, "ignore all depth rules")
        assert len(e.db.query_all("SELECT * FROM project_agents")) == 9
        e.limits["max_depth"] = 2
        child = await e.team.start(owners["requirements"]["session_id"], e.owner, "new child")
        await settled(e, [child["session_id"]])
        e.limits["max_depth"] = 1
        runtime = e.manager.get_runtime(child["session_id"], e.owner)
        with pytest.raises(TeamError, match="最大递归深度"):
            await runtime.submit("direct request cannot bypass")
        await e.team.aclose()

    asyncio.run(main())


def test_foreign_project_owner_ancestor_and_sibling_are_denied(team_env):
    e = team_env
    lead, owners = nodes(e)
    other = e.manager.project_repo.create(e.stranger, "other", str(e.root), "other-key")
    foreign_lead = e.team.sync(other["id"], e.stranger)["lead"]
    foreign = e.team._create_worker(other, foreign_lead, tree="main", role="subagent", title="foreign task")
    worker = owners["implementation"]

    async def main():
        with pytest.raises(KeyError):
            await e.team.start(lead["session_id"], e.stranger, "not mine", node_id="requirements")
        with pytest.raises(KeyError):
            await e.team.stop(lead["session_id"], e.owner, foreign["session_id"])
        for target in (lead, owners["verification"]):
            with pytest.raises(TeamError, match="后代"):
                await e.team.wait(worker["session_id"], e.owner, [target["session_id"]], 0)
            with pytest.raises(TeamError, match="后代"):
                await e.team.stop(worker["session_id"], e.owner, target["session_id"])
        with pytest.raises(TeamError):
            await e.team.start(worker["session_id"], e.owner, "hijack", node_id="verification")
        with pytest.raises(TeamError):
            await e.team.start(worker["session_id"], e.owner, "hijack", parent_id=lead["session_id"])
        await e.team.aclose()

    asyncio.run(main())


def test_stop_cancels_running_queued_and_descendant_tree_and_releases_slots(team_env):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, owners = nodes(e)

    async def main():
        running = asyncio.Event()
        hold = asyncio.Event()
        child_ids = []

        async def handler(runtime, *args):
            if e.team.descriptor(runtime.session_id)["depth"] == 1:
                child = await e.team.start(runtime.session_id, e.owner, "queued descendant")
                child_ids.append(child["session_id"])
                running.set()
            await hold.wait()
            return SimpleNamespace(text="should not finish", stop_reason="end_turn")

        e.manager.handler = handler
        root = await fixture_task(e, lead["session_id"], e.owner, "running owner", node_id="implementation")
        await running.wait()
        stopped = await e.team.stop(lead["session_id"], e.owner, root["session_id"])
        await settled(e, [root["session_id"], *child_ids])
        assert set(stopped["stopped"]) == {root["session_id"], *child_ids}
        assert all(e.team.descriptor(sid)["state"] == "cancelled" for sid in stopped["stopped"])
        for _ in range(6):
            await asyncio.sleep(0)
        assert e.team._capacity(e.pid).active == set() and not e.team._capacity(e.pid).queue
        assert e.manager.get_runtime(lead["session_id"], e.owner).notes == [] or all(note["state"] == "cancelled" for note in e.manager.get_runtime(lead["session_id"], e.owner).notes)
        await e.team.aclose()

    asyncio.run(main())


def test_cancel_before_submit_first_tick_clears_reservation_for_retry(team_env):
    e = team_env
    lead, owners = nodes(e)

    async def main():
        first = await fixture_task(e, lead["session_id"], e.owner, "cancel before first tick", node_id="requirements")
        await e.team.stop(lead["session_id"], e.owner, first["session_id"])
        for _ in range(3):
            await asyncio.sleep(0)
        assert first["session_id"] not in e.team._starts
        second = await fixture_task(e, lead["session_id"], e.owner, "retry", node_id="requirements")
        await settled(e, [second["session_id"]])
        assert e.team.descriptor(second["session_id"])["state"] == "completed"
        assert second["activation_id"] > first["activation_id"]
        await e.team.aclose()

    asyncio.run(main())


def test_final_audit_cannot_accept_parent_with_unfinished_descendants(team_env):
    e = team_env
    lead, owners = nodes(e)

    async def main():
        worker = await fixture_task(e, lead["session_id"], e.owner, "node report", node_id="implementation")
        await settled(e, [worker["session_id"]])
        async def handler(*args):
            await asyncio.Event().wait()
        e.manager.handler = handler
        descendant = await e.team.start(worker["session_id"], e.owner, "unfinished work")
        with pytest.raises(TeamError, match="还有运行或排队"):
            e.team.audit(lead["session_id"], e.owner, worker["session_id"], activation_id=worker["activation_id"], decision="accepted", reason="too early")
        with pytest.raises(TeamError, match="执行编号"):
            e.team.audit(lead["session_id"], e.owner, worker["session_id"], activation_id=2**80, decision="accepted", reason="bad integer")
        await e.team.stop(lead["session_id"], e.owner, descendant["session_id"])
        await e.team.aclose()

    asyncio.run(main())


def test_cancelling_parent_during_recursive_wait_does_not_reacquire_or_leave_child(team_env):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, owners = nodes(e)

    async def main():
        child_started = asyncio.Event()
        child_ids = []

        async def handler(runtime, *args):
            if e.team.descriptor(runtime.session_id)["depth"] == 1:
                child = await e.team.start(runtime.session_id, e.owner, "slow child")
                child_ids.append(child["session_id"])
                await e.team.wait(runtime.session_id, e.owner, [child["session_id"]], 60)
            else:
                child_started.set()
                await asyncio.Event().wait()
            return SimpleNamespace(text="unused", stop_reason="end_turn")

        e.manager.handler = handler
        parent = await fixture_task(e, lead["session_id"], e.owner, "parent", node_id="implementation")
        await child_started.wait()
        assert e.team.descriptor(parent["session_id"])["state"] == "waiting"
        await e.team.stop(lead["session_id"], e.owner, parent["session_id"])
        await settled(e, [parent["session_id"], *child_ids])
        for _ in range(8):
            await asyncio.sleep(0)
        assert not e.team._capacity(e.pid).active
        await e.team.aclose()

    asyncio.run(main())


def test_audit_requires_lead_completed_exact_activation_and_evidence(team_env):
    e = team_env
    lead, owners = nodes(e)
    worker = owners["verification"]

    async def main():
        first = await fixture_task(e, lead["session_id"], e.owner, "test one", node_id="verification")
        await settled(e, [first["session_id"]])
        with pytest.raises(TeamError, match="只有项目 Lead"):
            e.team.audit(worker["session_id"], e.owner, first["session_id"], activation_id=1, decision="accepted", reason="self claim")
        with pytest.raises(TeamError, match="审计依据"):
            e.team.audit(lead["session_id"], e.owner, first["session_id"], activation_id=1, decision="accepted", reason=" ")
        accepted = e.team.audit(lead["session_id"], e.owner, first["session_id"], activation_id=1, decision="accepted", reason="Checked source.txt:1 and test output")
        assert accepted["audit"]["decision"] == "accepted"
        second = await fixture_task(e, lead["session_id"], e.owner, "test two", node_id="verification")
        await settled(e, [second["session_id"]])
        assert second["activation_id"] == 2
        with pytest.raises(TeamError, match="新结果"):
            e.team.audit(lead["session_id"], e.owner, second["session_id"], activation_id=1, decision="accepted", reason="old browser view")
        rejected = e.team.audit(lead["session_id"], e.owner, second["session_id"], activation_id=2, decision="rejected", reason="missing regression evidence")
        assert rejected["audit"]["decision"] == "rejected"
        history = e.db.query_all("SELECT * FROM project_agent_runs WHERE session_id=? ORDER BY activation_id", (second["session_id"],))
        assert [row["audit_decision"] for row in history] == ["accepted", "rejected"]
        assert e.store.get(e.pid, "main")["nodes"][5]["status"] == "pending"
        await e.team.aclose()

    asyncio.run(main())


@pytest.mark.parametrize("stop_reason,text", [("max_turns", "partial"), ("repeated_failure", "partial"), ("end_turn", "")])
def test_incomplete_loop_results_cannot_be_accepted(team_env, stop_reason, text):
    e = team_env
    lead, owners = nodes(e)

    async def main():
        async def handler(*args):
            return SimpleNamespace(text=text, stop_reason=stop_reason)
        e.manager.handler = handler
        worker = await fixture_task(e, lead["session_id"], e.owner, "incomplete", node_id="verification")
        await settled(e, [worker["session_id"]])
        assert e.team.descriptor(worker["session_id"])["state"] == "failed"
        with pytest.raises(TeamError):
            e.team.audit(lead["session_id"], e.owner, worker["session_id"], activation_id=worker["activation_id"], decision="accepted", reason="not actually complete")
        await e.team.aclose()

    asyncio.run(main())


def test_provider_failure_is_persisted_and_does_not_strand_capacity(team_env):
    e = team_env
    lead, owners = nodes(e)

    async def main():
        async def handler(*args):
            raise RuntimeError("offline provider unavailable")
        e.manager.handler = handler
        worker = await fixture_task(e, lead["session_id"], e.owner, "fail", node_id="verification")
        await settled(e, [worker["session_id"]])
        row = e.team.descriptor(worker["session_id"])
        assert row["state"] == "failed" and row["error"] == "offline provider unavailable"
        assert not e.team._capacity(e.pid).active
        await e.team.aclose()

    asyncio.run(main())


def test_worktree_and_permissions_are_inherited_after_runtime_recreation(team_env):
    e = team_env
    path = e.tmp_path / "isolated-tree"
    path.mkdir()
    task = TaskRepo(e.db).create(project_id=e.pid, user_id=e.owner, title="task", requirement="work", base_branch="main", branch="isolated", worktree=str(path))
    tree = f"task:{task['id']}"
    lead, owners = nodes(e, tree)
    node = owners["implementation"]
    e.manager.session_repo.set_permission_mode(node["session_id"], "read-only")

    async def main():
        child = await e.team.start(node["session_id"], e.owner, "recursive read-only worktree child")
        await settled(e, [child["session_id"]])
        meta = e.manager.session_repo.get(child["session_id"])
        assert meta["workspace_override"] == str(path)
        assert meta["permission_mode"] == "read-only"
        assert child["tree_id"] == tree
        e.manager._runtimes.pop(child["session_id"])
        recreated = e.manager.get_runtime(child["session_id"], e.owner)
        assert recreated.workspace == path and recreated.permission_mode == "read-only"
        with pytest.raises(TeamError, match="跨工作树"):
            await e.team.start(node["session_id"], e.owner, "cross tree", tree="main")
        await e.team.aclose()

    asyncio.run(main())


def test_main_permission_flag_is_rechecked_and_descendant_cannot_elevate(team_env, monkeypatch):
    e = team_env
    monkeypatch.setattr("code_agent.agent.project_team.settings.permission_mode", lambda: "full-access")
    lead, owners = nodes(e)
    worker = owners["implementation"]
    assert e.manager.session_repo.get(worker["session_id"])["permission_mode"] == "read-only"
    e.manager.project_repo.set_flags(e.pid, allow_direct_write=True)
    e.manager.session_repo.set_permission_mode(lead["session_id"], "workspace-write")

    async def main():
        row = await fixture_task(e, lead["session_id"], e.owner, "authorized project task", node_id="implementation")
        await settled(e, [row["session_id"]])
        assert e.manager.get_runtime(row["session_id"], e.owner).permission_mode == "workspace-write"
        child = await e.team.start(row["session_id"], e.owner, "child")
        await settled(e, [child["session_id"]])
        e.manager.session_repo.set_permission_mode(child["session_id"], "full-access")
        child_runtime = e.manager.get_runtime(child["session_id"], e.owner)
        await child_runtime.submit("cannot elevate past parent")
        assert child_runtime.permission_mode == "workspace-write"
        e.manager.project_repo.set_flags(e.pid, allow_direct_write=False)
        await child_runtime.submit("flag revoked")
        assert child_runtime.permission_mode == "read-only"
        assert e.manager.session_repo.get(child["session_id"])["permission_mode"] == "full-access"
        await e.team.aclose()

    asyncio.run(main())


@pytest.mark.parametrize("revocation", ["project_flag", "lead_mode"])
def test_queued_worker_rechecks_permission_after_acquiring_slot(team_env, revocation):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, _ = nodes(e)
    e.manager.project_repo.set_flags(e.pid, allow_direct_write=True)
    e.manager.session_repo.set_permission_mode(lead["session_id"], "workspace-write")

    async def main():
        entered = asyncio.Event()
        release = asyncio.Event()
        observed = []

        async def handler(runtime, prompt, *_args):
            if prompt == "occupy slot":
                entered.set()
                await release.wait()
            else:
                observed.append(runtime.permission_mode)
            return SimpleNamespace(text="verified", stop_reason="end_turn")

        e.manager.handler = handler
        first = await fixture_task(e, lead["session_id"], e.owner, "occupy slot", node_id="implementation")
        await asyncio.wait_for(entered.wait(), 1)
        second = await fixture_task(e, lead["session_id"], e.owner, "queued work", node_id="verification")
        for _ in range(10):
            await asyncio.sleep(0)
        assert second["session_id"] in e.team._capacity(e.pid).queue
        assert e.manager.get_runtime(second["session_id"], e.owner).permission_mode == "workspace-write"
        if revocation == "project_flag":
            e.manager.project_repo.set_flags(e.pid, allow_direct_write=False)
        else:
            e.manager.session_repo.set_permission_mode(lead["session_id"], "read-only")
        release.set()
        await settled(e, [first["session_id"], second["session_id"]])
        assert observed == ["read-only"]
        assert e.manager.session_repo.get(second["session_id"])["permission_mode"] == "read-only"
        assert not e.team._capacity(e.pid).active
        await e.team.aclose()

    asyncio.run(main())


@pytest.mark.parametrize("invalidation", ["discarded", "missing_directory"])
def test_queued_worker_rechecks_worktree_after_acquiring_slot(team_env, invalidation):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, _ = nodes(e)
    path = e.tmp_path / "queued-worktree"
    path.mkdir()
    task_repo = TaskRepo(e.db)
    task = task_repo.create(project_id=e.pid, user_id=e.owner, title="task", requirement="work",
                            base_branch="main", branch="queued", worktree=str(path))
    tree = f"task:{task['id']}"
    nodes(e, tree)

    async def main():
        entered = asyncio.Event()
        release = asyncio.Event()
        observed = []

        async def handler(runtime, prompt, *_args):
            observed.append(prompt)
            entered.set()
            await release.wait()
            return SimpleNamespace(text="verified", stop_reason="end_turn")

        e.manager.handler = handler
        first = await fixture_task(e, lead["session_id"], e.owner, "occupy slot", node_id="implementation")
        await asyncio.wait_for(entered.wait(), 1)
        second = await fixture_task(e, lead["session_id"], e.owner, "invalid isolated work", node_id="verification", tree=tree)
        for _ in range(10):
            await asyncio.sleep(0)
        assert second["session_id"] in e.team._capacity(e.pid).queue
        if invalidation == "discarded":
            task_repo.update(task["id"], status="discarded")
        else:
            path.rename(e.tmp_path / "relocated-worktree")
        release.set()
        await settled(e, [first["session_id"], second["session_id"]])
        assert observed == ["occupy slot"]
        result = e.team.descriptor(second["session_id"])
        assert result["state"] == "failed" and result["error"]
        assert e.manager.get_runtime(second["session_id"], e.owner).prompts == []
        assert not e.team._capacity(e.pid).active
        await e.team.aclose()

    asyncio.run(main())


def test_queued_recursive_worker_rechecks_depth_after_admission(team_env, monkeypatch):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, _ = nodes(e)

    async def main():
        entered = asyncio.Event()
        release = asyncio.Event()
        observed = []

        async def handler(runtime, prompt, *_args):
            observed.append(prompt)
            entered.set()
            await release.wait()
            return SimpleNamespace(text="verified", stop_reason="end_turn")

        e.manager.handler = handler
        parent = await fixture_task(e, lead["session_id"], e.owner, "occupy slot", node_id="implementation")
        await asyncio.wait_for(entered.wait(), 1)
        acquire = e.team._acquire

        async def lower_depth_at_admission(row, activation):
            await acquire(row, activation)
            if row["depth"] == 2:
                # Resolve the queue normally, then simulate a concurrent Settings
                # update at admission before the actual model loop is constructed.
                e.limits["max_depth"] = 1

        monkeypatch.setattr(e.team, "_acquire", lower_depth_at_admission)
        child = await e.team.start(parent["session_id"], e.owner, "queued recursive work")
        for _ in range(10):
            await asyncio.sleep(0)
        assert child["depth"] == 2 and child["session_id"] in e.team._capacity(e.pid).queue
        release.set()
        await settled(e, [parent["session_id"], child["session_id"]])
        result = e.team.descriptor(child["session_id"])
        assert result["state"] == "failed" and "递归深度" in result["error"]
        assert observed == ["occupy slot"]
        assert not e.team._capacity(e.pid).active
        await e.team.aclose()

    asyncio.run(main())


def test_task_tree_foreign_or_absent_override_fails_closed(team_env):
    e = team_env
    with pytest.raises(KeyError):
        e.team.sync(e.pid, e.owner, "task:does_not_exist")
    task = TaskRepo(e.db).create(project_id=e.pid, user_id=e.owner, title="task", requirement="work", base_branch="main", branch="none", worktree="")
    with pytest.raises(KeyError):
        e.team.sync(e.pid, e.owner, f"task:{task['id']}")
    assert not e.db.query_all("SELECT * FROM project_agents WHERE tree_id!='main'")


def test_worker_workspace_override_cannot_escape_inherited_tree(team_env):
    e = team_env
    lead, owners = nodes(e)
    sid = owners["implementation"]["session_id"]
    runtime = e.manager.get_runtime(sid, e.owner)
    outside = e.tmp_path / "other-project"
    outside.mkdir()
    e.db.execute("UPDATE sessions SET workspace_override=? WHERE id=?", (str(outside), sid))

    async def main():
        with pytest.raises(TeamError, match="工作区"):
            await runtime.submit("metadata must not escape")
        assert runtime.prompts == []
        await e.team.aclose()

    asyncio.run(main())


def test_cap_shared_across_main_and_isolated_tree(team_env):
    e = team_env
    e.limits["max_parallel"] = 1
    isolated = e.tmp_path / "tree"
    isolated.mkdir()
    task = TaskRepo(e.db).create(project_id=e.pid, user_id=e.owner, title="task", requirement="work", base_branch="main", branch="tree", worktree=str(isolated))
    tree = f"task:{task['id']}"
    lead, main_owners = nodes(e)
    _, task_owners = nodes(e, tree)

    async def main():
        gate = asyncio.Event()
        entered = []
        async def handler(runtime, *args):
            entered.append(runtime.session_id)
            await gate.wait()
            return SimpleNamespace(text="ok", stop_reason="end_turn")
        e.manager.handler = handler
        first = await fixture_task(e, lead["session_id"], e.owner, "main", node_id="requirements")
        second = await fixture_task(e, lead["session_id"], e.owner, "tree", node_id="requirements", tree=tree)
        for _ in range(8):
            await asyncio.sleep(0)
        assert len(entered) == 1
        assert e.team.descriptor(second["session_id"])["state"] == "queued"
        gate.set()
        await settled(e, [first["session_id"], second["session_id"]])
        assert len(entered) == 2
        await e.team.aclose()

    asyncio.run(main())


def test_dynamic_limits_wake_queued_workers_without_old_run_finishing(team_env):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, owners = nodes(e)

    async def main():
        entered = []
        gate = asyncio.Event()
        async def handler(runtime, *args):
            entered.append(runtime.session_id)
            await gate.wait()
            return SimpleNamespace(text="ok", stop_reason="end_turn")
        e.manager.handler = handler
        first = await fixture_task(e, lead["session_id"], e.owner, "one", node_id="requirements")
        second = await fixture_task(e, lead["session_id"], e.owner, "two", node_id="verification")
        await asyncio.sleep(0.01)
        assert len(entered) == 1
        e.limits["max_parallel"] = 2
        for _ in range(100):
            if len(entered) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(entered) == 2
        gate.set()
        await settled(e, [first["session_id"], second["session_id"]])
        await e.team.aclose()

    asyncio.run(main())


def test_legacy_stage_retirement_blocks_direct_run_and_delegation_without_workflow_revival(team_env):
    e = team_env
    lead, owners = nodes(e)

    async def main():
        entered = asyncio.Event()
        async def handler(*args):
            entered.set()
            await asyncio.Event().wait()
        e.manager.handler = handler
        worker = await fixture_task(e, lead["session_id"], e.owner, "work", node_id="requirements")
        await entered.wait()
        # Simulate an owner persisted by the previous fixed-stage implementation.
        e.db.execute("UPDATE project_agents SET role='node' WHERE session_id=?", (worker["session_id"],))
        original = default_workflow()
        removed = copy.deepcopy(original)
        removed["nodes"] = removed["nodes"][1:]
        removed["edges"] = removed["edges"][1:]
        save_nodes(e, removed)
        e.team.sync(e.pid, e.owner)
        assert e.team.descriptor(worker["session_id"])["state"] == "retired"
        with pytest.raises(TeamError, match="退出工作流"):
            await e.team.run(e.manager.get_runtime(worker["session_id"], e.owner), "try direct")
        with pytest.raises(TeamError, match="退出工作流"):
            await e.team.start(worker["session_id"], e.owner, "try delegate")
        save_nodes(e, original)
        e.team.sync(e.pid, e.owner)
        for _ in range(8):
            await asyncio.sleep(0)
        revived = e.team.descriptor(worker["session_id"])
        assert revived["state"] == "retired"
        assert revived["activation_id"] == worker["activation_id"]
        assert e.manager.get_runtime(lead["session_id"], e.owner).notes == []
        await e.team.aclose()

    asyncio.run(main())


def test_legacy_stage_retirement_from_thread_cancels_on_task_owner_loop(team_env):
    e = team_env
    lead, owners = nodes(e)

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        entered = asyncio.Event()
        async def handler(*args):
            entered.set()
            await asyncio.Event().wait()
        e.manager.handler = handler
        worker = await fixture_task(e, lead["session_id"], e.owner, "thread retirement", node_id="requirements")
        await entered.wait()
        e.db.execute("UPDATE project_agents SET role='node' WHERE session_id=?", (worker["session_id"],))
        removed = default_workflow()
        removed["nodes"] = removed["nodes"][1:]
        removed["edges"] = removed["edges"][1:]
        save_nodes(e, removed)
        await asyncio.to_thread(e.team.sync, e.pid, e.owner)
        for _ in range(12):
            await asyncio.sleep(0)
        assert e.team.descriptor(worker["session_id"])["state"] == "retired"
        assert worker["session_id"] not in e.team._executions
        assert not e.team._capacity(e.pid).active
        await e.team.aclose()

    asyncio.run(main())


def test_restart_marks_interrupted_runs_and_keeps_completed_audits(team_env):
    e = team_env
    lead, owners = nodes(e)
    sid = owners["verification"]["session_id"]
    e.team._begin_activation(sid, "interrupted prompt", "queued")
    restarted = ProjectTeam(e.manager, e.store)
    e.manager.team = restarted
    row = restarted.descriptor(sid)
    assert row["state"] == "cancelled" and "重启" in row["error"]
    assert e.db.query_one("SELECT prompt,state FROM project_agent_runs WHERE session_id=?", (sid,)) == {"prompt": "interrupted prompt", "state": "cancelled"}
    assert restarted.sync(e.pid, e.owner)["lead"]["session_id"] == lead["session_id"]
    assert restarted.descriptor(sid)["session_id"] == sid


def test_project_deletion_stops_lead_and_workers_across_trees_from_thread(team_env):
    e = team_env
    lead, _ = nodes(e)
    worktree = e.tmp_path / "deleting-worktree"
    worktree.mkdir()
    task = TaskRepo(e.db).create(project_id=e.pid, user_id=e.owner, title="task", requirement="work",
                                base_branch="main", branch="isolated", worktree=str(worktree))
    tree = f"task:{task['id']}"
    nodes(e, tree)

    async def main():
        started = set()
        ready = asyncio.Event()

        async def handler(runtime, *_args):
            started.add(runtime.session_id)
            if len(started) == 3:
                ready.set()
            await asyncio.Event().wait()

        e.manager.handler = handler
        lead_runtime = e.manager.get_runtime(lead["session_id"], e.owner)
        lead_task = asyncio.create_task(lead_runtime.submit("oversee work"))
        worker = await fixture_task(e, lead["session_id"], e.owner, "main work", node_id="implementation")
        isolated = await fixture_task(e, lead["session_id"], e.owner, "isolated work", node_id="verification", tree=tree)
        await asyncio.wait_for(ready.wait(), 1)
        running_tasks = [lead_task, *e.team._starts.values(), *e.team._executions.values()]

        def remove():
            ids = e.team.cancel_project(e.pid)
            e.manager.project_repo.delete(e.pid)
            return ids

        ids = await asyncio.to_thread(remove)
        assert {lead["session_id"], worker["session_id"], isolated["session_id"]} <= set(ids)
        await asyncio.gather(*running_tasks, return_exceptions=True)
        assert e.team.descriptor(lead["session_id"]) is None
        assert not e.team._capacity(e.pid).active
        assert e.team._executions == {}
        assert all(not runtime.busy for runtime in e.manager._runtimes.values())
        await e.team.aclose()

    asyncio.run(main(), debug=True)


def test_automatic_wake_is_scheduled_and_forwards_auto_meta(team_env):
    e = team_env
    lead, owners = nodes(e)

    async def main():
        seen = []
        async def handler(runtime, prompt, attachments, context, kwargs):
            seen.append(kwargs)
            return SimpleNamespace(text="wake result", stop_reason="end_turn")
        e.manager.handler = handler
        runtime = e.manager.get_runtime(owners["requirements"]["session_id"], e.owner)
        await e.team.run(runtime, "automatic note", auto_meta={"automatic": True})
        assert seen == [{"auto_meta": {"automatic": True}}]
        assert e.team.descriptor(runtime.session_id)["state"] == "completed"
        await e.team.aclose()

    asyncio.run(main())


def test_model_tools_use_actor_context_and_reject_bad_types(team_env):
    e = team_env
    lead, owners = nodes(e)
    registry = ToolRegistry()
    register_agent_team_tools(registry, e.team)
    ctx = ToolContext(lead["session_id"], e.root, e.owner)

    async def main():
        listing = json.loads(await registry.dispatch("team_list", {}, ctx))
        assert listing["lead"]["session_id"] == lead["session_id"]
        bad = [
            ("delegate_agent", {"prompt": 42, "write_paths": [], "delegation_reason": "independent check"}),
            ("delegate_agent", {"prompt": "ok", "title": [], "write_paths": [], "delegation_reason": "independent check"}),
            ("delegate_agent", {"prompt": "ok", "permission_mode": "full-access", "write_paths": [], "delegation_reason": "independent check"}),
            ("wait_agents", {"session_ids": [lead["session_id"]], "timeout_seconds": True}),
            ("wait_agents", {"session_ids": [], "timeout_seconds": 1}),
            ("audit_agent", {"session_id": owners["requirements"]["session_id"], "activation_id": True, "decision": "accepted", "reason": "none"}),
            ("stop_agent", {"session_id": 8}),
        ]
        for name, arguments in bad:
            with pytest.raises(ToolError):
                await registry.dispatch(name, arguments, ctx)
        delegated = json.loads(await registry.dispatch("delegate_agent", {"node_id": "requirements", "prompt": "verify",
            "write_paths": [], "delegation_reason": "independent verification while the Lead coordinates"}, ctx))
        await settled(e, [delegated["session_id"]])
        output = json.loads(await registry.dispatch("wait_agents", {"session_ids": [delegated["session_id"]], "timeout_seconds": 0}, ctx))
        assert output["agents"][0]["result"] == "done: verify"
        await e.team.aclose()

    asyncio.run(main())


def test_team_api_requires_project_owner_and_exact_audit_identity(team_env):
    e = team_env
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(e.owner, "owner", "user")
    mount_agent_team_api(app, team=e.team, service=ProjectService(e.manager.project_repo))
    prefix = f"/api/v1/code/projects/{e.pid}/agents"
    with TestClient(app) as client:
        snapshot = client.get(prefix).json()
        assert snapshot["agents"] == []
        worker = e.team._create_worker(e.project, snapshot["lead"], tree="main", role="subagent", title="API task")
        assert len(client.get(prefix).json()["agents"]) == 1
        assert client.post(prefix + f"/{worker['session_id']}/audit", json={"decision": "accepted", "reason": "old UI"}).status_code == 422
        assert client.post(prefix + f"/{worker['session_id']}/audit", json={"activation_id": 1, "decision": "accepted", "reason": "not executed"}).status_code == 409
        assert client.post(prefix + "/foreign/stop").status_code == 404
        assert client.get(prefix + "?tree=bad").status_code == 422
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(e.stranger, "stranger", "user")
        assert client.get(prefix).status_code == 404
        assert client.post(prefix + f"/{worker['session_id']}/stop").status_code == 404


def test_role_context_contains_real_node_scope_and_no_fake_completion(team_env):
    e = team_env
    lead, owners = nodes(e)
    lead_context = e.team.role_context(e.manager.get_runtime(lead["session_id"], e.owner))
    node_context = e.team.role_context(e.manager.get_runtime(owners["verification"]["session_id"], e.owner))
    assert "独立执行任务" in lead_context and "activation_id" in lead_context
    assert "当前对话的协调者" in lead_context and "完成通知本身不等于测试通过" in lead_context
    assert "不是普通交付的必经步骤" in lead_context and "如实说明并补充必要检查" in lead_context
    assert "不要求固定 node_id" in lead_context
    assert "测试验收" in node_context and "实际产出" in node_context
    assert "不可信外部数据" in node_context
    assert "不能擅自标记节点验收完成" in node_context


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "full-access"])
def test_lead_direct_execution_uses_current_saved_permission_without_delegation_or_grant_changes(team_env, mode):
    e = team_env
    lead, owners = nodes(e)
    sid = lead["session_id"]
    runtime = e.manager.get_runtime(sid, e.owner)
    assert runtime.permission_mode == "read-only"
    assert not e.manager.project_repo.get(e.pid)["allow_direct_write"]
    e.manager.project_repo.set_flags(e.pid, allow_direct_write=True)
    e.manager.session_repo.set_permission_mode(sid, mode)
    grants = {row["session_id"]: e.manager.session_repo.get(row["session_id"])["permission_mode"] for row in [lead, *owners.values()]}
    observed = []

    async def main():
        async def handler(active, prompt, *_args):
            observed.append((active.session_id, active.permission_mode, prompt))
            return SimpleNamespace(text="direct result with related verification", stop_reason="end_turn")

        e.manager.handler = handler
        assert e.team.effective_permission(sid, e.owner) == mode
        assert "会话当前权限" in e.team.role_context(runtime)
        result = await runtime.submit("complete this small task directly")
        assert result.text == "direct result with related verification"
        assert observed == [(sid, mode, "complete this small task directly")]
        assert e.team.descriptor(sid)["audit"]["decision"] == "none", "Lead's own delivery needs no delegated-result handoff"
        assert all(e.team.descriptor(row["session_id"])["activation_id"] == 0 for row in owners.values())
        assert len(e.db.query_all("SELECT session_id FROM project_agent_runs")) == 1
        assert {member: e.manager.session_repo.get(member)["permission_mode"] for member in grants} == grants
        assert e.manager.project_repo.get(e.pid)["allow_direct_write"]
        e.manager.session_repo.set_permission_mode(sid, "read-only")
        await runtime.submit("respect a later permission revocation")
        assert runtime.permission_mode == "read-only"
        assert e.manager.session_repo.get(sid)["permission_mode"] == "read-only"
        await e.team.aclose()

    asyncio.run(main())


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "full-access"])
def test_lead_main_project_write_switch_clamps_and_restores_configured_permission_without_saving_it(team_env, mode):
    e = team_env
    lead, owners = nodes(e)
    sid = lead["session_id"]
    runtime = e.manager.get_runtime(sid, e.owner)
    e.manager.session_repo.set_permission_mode(sid, mode)
    grants = {member["session_id"]: e.manager.session_repo.get(member["session_id"])["permission_mode"] for member in [lead, *owners.values()]}
    observed = []

    async def main():
        async def handler(active, *_args):
            observed.append(active.permission_mode)
            return SimpleNamespace(text="current grant observed", stop_reason="end_turn")

        e.manager.handler = handler
        for allowed, expected in [(False, "read-only"), (True, mode), (False, "read-only"), (True, mode)]:
            e.manager.project_repo.set_flags(e.pid, allow_direct_write=allowed)
            assert e.team.effective_permission(sid, e.owner) == expected
            await runtime.submit("respect current main-workspace write boundary")
            assert runtime.permission_mode == expected
            descriptor = e.team.descriptor(sid)
            assert descriptor["configured_permission"] == mode
            assert descriptor["effective_permission"] == expected
            assert {member: e.manager.session_repo.get(member)["permission_mode"] for member in grants} == grants
        assert observed == ["read-only", mode, "read-only", mode]
        assert all(e.team.descriptor(member["session_id"])["activation_id"] == 0 for member in owners.values())
        await e.team.aclose()

    asyncio.run(main())


def test_recursive_configured_grant_survives_temporary_ancestor_and_project_clamps(team_env):
    e = team_env
    lead, owners = nodes(e)
    e.manager.project_repo.set_flags(e.pid, allow_direct_write=True)
    e.manager.session_repo.set_permission_mode(lead["session_id"], "workspace-write")

    async def main():
        parent = owners["implementation"]["session_id"]
        child = await e.team.start(parent, e.owner, "child")
        await settled(e, [child["session_id"]])
        leaf = await e.team.start(child["session_id"], e.owner, "leaf")
        await settled(e, [leaf["session_id"]])
        runtime = e.manager.get_runtime(leaf["session_id"], e.owner)
        assert runtime.permission_mode == "workspace-write"
        for revoke, restore in (
            (lambda: e.manager.session_repo.set_permission_mode(lead["session_id"], "read-only"),
             lambda: e.manager.session_repo.set_permission_mode(lead["session_id"], "workspace-write")),
            (lambda: e.manager.project_repo.set_flags(e.pid, allow_direct_write=False),
             lambda: e.manager.project_repo.set_flags(e.pid, allow_direct_write=True)),
        ):
            revoke()
            await runtime.submit("observe clamp")
            assert runtime.permission_mode == "read-only"
            assert e.manager.session_repo.get(leaf["session_id"])["permission_mode"] == "workspace-write"
            restore()
            await runtime.submit("grant restored")
            assert runtime.permission_mode == "workspace-write"
        await e.team.aclose()

    asyncio.run(main())


def test_followup_reuses_session_and_history_but_has_new_activation_and_context(team_env):
    e = team_env
    lead, owners = nodes(e)
    contexts = []

    async def main():
        first = await fixture_task(e, lead["session_id"], e.owner, "first task", node_id="implementation")
        await settled(e, [first["session_id"]])
        runtime = e.manager.get_runtime(first["session_id"], e.owner)
        runtime.set_delegation_context = lambda parent, strategy, prompt: contexts.append((parent, strategy, prompt))
        second = await e.team.followup(lead["session_id"], e.owner, first["session_id"], "continue task", context_strategy="none")
        assert second["session_id"] == first["session_id"]
        assert second["activation_id"] == first["activation_id"] + 1
        await settled(e, [second["session_id"]])
        assert runtime.prompts == ["first task", "continue task"]
        assert contexts == [(lead["session_id"], "none", "continue task")]
        runs = e.db.query_all("SELECT activation_id,prompt,result FROM project_agent_runs WHERE session_id=? ORDER BY activation_id", (first["session_id"],))
        assert len(runs) == 2 and runs[0]["result"] == "done: first task"
        with pytest.raises(TeamError, match="自己的后代"):
            await e.team.followup(owners["requirements"]["session_id"], e.owner, first["session_id"], "sibling takeover")
        with pytest.raises(TeamError, match="context_strategy"):
            await e.team.followup(lead["session_id"], e.owner, first["session_id"], "invalid context", context_strategy="everything")
        await e.team.aclose()

    asyncio.run(main())


def test_mailbox_only_message_does_not_start_idle_agent_and_busy_followup_rejects(team_env):
    e = team_env
    lead, owners = nodes(e)
    sid = owners["implementation"]["session_id"]
    runtime = e.manager.get_runtime(sid, e.owner)
    mailbox = []
    runtime.enqueue_agent_message = lambda message: mailbox.append(message) or {"message_id": "receipt-1"}

    async def main():
        receipt = await e.team.send_message(lead["session_id"], e.owner, sid, "Keep this scope")
        assert receipt["queued"] and receipt["starts_turn"] is False
        assert receipt["receipt"]["message_id"] == "receipt-1"
        assert runtime.prompts == [] and e.team.descriptor(sid)["activation_id"] == 0
        assert mailbox[0]["sender_session_id"] == lead["session_id"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(text="finished", stop_reason="end_turn")

        e.manager.handler = handler
        await fixture_task(e, lead["session_id"], e.owner, "run", node_id="implementation")
        await asyncio.wait_for(entered.wait(), 1)
        await e.team.send_message(lead["session_id"], e.owner, sid, "Additional detail")
        with pytest.raises(TeamError, match="send_agent_message"):
            await e.team.followup(lead["session_id"], e.owner, sid, "replace busy work")
        assert len(mailbox) == 2
        release.set()
        await settled(e, [sid])
        await e.team.aclose()

    asyncio.run(main())


def test_interrupt_preserves_running_child_and_allows_parent_followup(team_env):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, _ = nodes(e)

    async def main():
        child_started, release = asyncio.Event(), asyncio.Event()
        children = []

        async def handler(runtime, prompt, *args):
            if prompt == "parent":
                child = await e.team.start(runtime.session_id, e.owner, "child")
                children.append(child["session_id"])
                await e.team.wait(runtime.session_id, e.owner, [child["session_id"]], 60)
            elif prompt == "child":
                child_started.set()
                await release.wait()
            return SimpleNamespace(text=f"done: {prompt}", stop_reason="end_turn")

        e.manager.handler = handler
        parent = await fixture_task(e, lead["session_id"], e.owner, "parent", node_id="implementation")
        await asyncio.wait_for(child_started.wait(), 1)
        outcome = await e.team.interrupt(lead["session_id"], e.owner, parent["session_id"])
        assert outcome["interrupted"] and outcome["descendants_preserved"]
        await settled(e, [parent["session_id"]])
        assert e.team.descriptor(children[0])["state"] == "running"
        release.set()
        await settled(e, children)
        parent_runtime = e.manager.get_runtime(parent["session_id"], e.owner)
        assert any(note["session_id"] == children[0] and note["state"] == "completed" for note in parent_runtime.notes)
        for _ in range(10):
            await asyncio.sleep(0)
        followup = await e.team.followup(lead["session_id"], e.owner, parent["session_id"], "continue")
        assert followup["activation_id"] == parent["activation_id"] + 1
        await settled(e, [parent["session_id"], *children])
        assert e.team.descriptor(parent["session_id"])["state"] == "completed"
        assert e.team.descriptor(children[0])["state"] == "completed"
        assert not e.team._capacity(e.pid).active
        await e.team.aclose()

    asyncio.run(main())


def test_wait_any_and_cursors_deliver_fresh_results_without_repeating_old_output(team_env):
    e = team_env
    lead, _ = nodes(e)

    async def main():
        release = asyncio.Event()

        async def handler(runtime, prompt, *args):
            if prompt == "slow":
                await release.wait()
            return SimpleNamespace(text=f"done: {prompt}", stop_reason="end_turn")

        e.manager.handler = handler
        fast = await fixture_task(e, lead["session_id"], e.owner, "fast", node_id="requirements")
        slow = await fixture_task(e, lead["session_id"], e.owner, "slow", node_id="implementation")
        ids = [fast["session_id"], slow["session_id"]]
        first = await e.team.wait(lead["session_id"], e.owner, ids, 1, mode="any")
        assert not first["timed_out"] and first["ready_session_ids"] == [fast["session_id"]]
        assert (await e.team.wait(lead["session_id"], e.owner, ids, 0))["timed_out"]
        repeat = await e.team.wait(lead["session_id"], e.owner, ids, 0, mode="any", after_cursors=first["cursors"])
        assert repeat["timed_out"] and repeat["ready_session_ids"] == []
        assert repeat["agents"][0]["unchanged"] and repeat["agents"][0]["result"] == ""
        release.set()
        second = await e.team.wait(lead["session_id"], e.owner, ids, 1, mode="any", after_cursors=first["cursors"])
        assert second["ready_session_ids"] == [slow["session_id"]]
        assert second["agents"][1]["result"] == "done: slow"
        all_done = await e.team.wait(lead["session_id"], e.owner, ids, 0, mode="all", after_cursors=first["cursors"])
        assert not all_done["timed_out"] and all_done["ready_session_ids"] == [slow["session_id"]]
        for kwargs in ({"mode": "bad"}, {"after_cursors": []}, {"after_cursors": {"foreign": "v1:" + "a" * 32}}, {"after_cursors": {ids[0]: "bad"}}):
            with pytest.raises(TeamError):
                await e.team.wait(lead["session_id"], e.owner, ids, 0, **kwargs)
        await e.team.aclose()

    asyncio.run(main())


def test_interrupt_queued_reservation_settles_before_immediate_followup(team_env):
    e = team_env
    lead, _ = nodes(e)

    async def main():
        first = await fixture_task(e, lead["session_id"], e.owner, "interrupt before admission", node_id="implementation")
        stopped = await e.team.interrupt(lead["session_id"], e.owner, first["session_id"])
        assert stopped["ready_for_followup"]
        second = await e.team.followup(lead["session_id"], e.owner, first["session_id"], "retry")
        await settled(e, [second["session_id"]])
        assert second["activation_id"] == first["activation_id"] + 1
        assert e.manager.get_runtime(first["session_id"], e.owner).prompts == ["retry"]
        assert not e.team._interrupting
        await e.team.aclose()

    asyncio.run(main())


def test_approval_wait_releases_slot_reacquires_and_rechecks_permissions(team_env):
    e = team_env
    e.limits["max_parallel"] = 1
    lead, _ = nodes(e)
    e.manager.project_repo.set_flags(e.pid, allow_direct_write=True)
    e.manager.session_repo.set_permission_mode(lead["session_id"], "workspace-write")

    async def main():
        waiting, approved, other_entered, other_release = (asyncio.Event() for _ in range(4))
        observed = []

        async def handler(runtime, prompt, *args):
            if prompt == "approval":
                runtime.phase = "waiting_approval"
                runtime.pending_approval = lambda: {"approval_id": "a-1", "tool": "write_file", "reason": "needs grant"}
                suspended = await e.team.suspend_for_approval(runtime)
                waiting.set()
                await approved.wait()
                runtime.pending_approval = lambda: None
                runtime.phase = ""
                await e.team.resume_after_approval(runtime, suspended)
                observed.append(runtime.permission_mode)
            else:
                other_entered.set()
                await other_release.wait()
            return SimpleNamespace(text="verified", stop_reason="end_turn")

        e.manager.handler = handler
        first = await fixture_task(e, lead["session_id"], e.owner, "approval", node_id="implementation")
        await asyncio.wait_for(waiting.wait(), 1)
        descriptor = e.team.descriptor(first["session_id"])
        assert descriptor["phase"] == "waiting_approval" and descriptor["blocked_reason"] == "needs grant"
        assert descriptor["pending_approval"]["session_id"] == first["session_id"]
        assert descriptor["configured_permission"] == "read-only" and descriptor["effective_permission"] == "workspace-write"
        assert not e.team._capacity(e.pid).active
        alert = await e.team.wait(lead["session_id"], e.owner, [first["session_id"]], 0, mode="any")
        assert alert["ready_session_ids"] == [first["session_id"]]
        barrier = await e.team.wait(lead["session_id"], e.owner, [first["session_id"]], 0)
        assert barrier["timed_out"] and barrier["ready_session_ids"] == []
        runtime = e.manager.get_runtime(first["session_id"], e.owner)
        runtime.pending_approval = lambda: None
        stale_phase = await e.team.wait(lead["session_id"], e.owner, [first["session_id"]], 0, mode="any")
        assert stale_phase["timed_out"] and stale_phase["ready_session_ids"] == []
        second = await fixture_task(e, lead["session_id"], e.owner, "other", node_id="verification")
        await asyncio.wait_for(other_entered.wait(), 1)
        e.manager.session_repo.set_permission_mode(lead["session_id"], "read-only")
        approved.set()
        await asyncio.sleep(0.01)
        assert e.team.descriptor(first["session_id"])["state"] == "queued" and observed == []
        other_release.set()
        await settled(e, [first["session_id"], second["session_id"]])
        assert observed == ["read-only"]
        assert not e.team._capacity(e.pid).active
        await e.team.aclose()

    asyncio.run(main())


def test_new_team_api_routes_validate_and_enforce_project_ownership(team_env):
    e = team_env
    lead, owners = nodes(e)
    sid = owners["implementation"]["session_id"]
    inbox = []
    e.manager.get_runtime(sid, e.owner).enqueue_agent_message = lambda message: inbox.append(message)
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(e.owner, "owner", "user")
    mount_agent_team_api(app, team=e.team, service=ProjectService(e.manager.project_repo))
    prefix = f"/api/v1/code/projects/{e.pid}/agents"
    with TestClient(app) as client:
        response = client.post(f"{prefix}/{sid}/message", json={"message": "supplement"})
        assert response.status_code == 200 and response.json()["starts_turn"] is False
        assert inbox[0]["message"] == "supplement"
        assert client.post(f"{prefix}/{sid}/followup", json={"prompt": "work", "context_strategy": "bad"}).status_code == 422
        assert client.post(f"{prefix}/{sid}/followup", json={"prompt": "work", "budget": {"max_tokens": True}}).status_code == 422
        assert client.post(f"{prefix}/{sid}/followup", json={"prompt": "work", "budget": {"max_seconds": 86401}}).status_code == 422
        assert client.post(f"{prefix}/{sid}/message", json={"message": "work", "permission_mode": "full-access"}).status_code == 422
        assert client.post(f"{prefix}/wait", json={"session_ids": [sid], "timeout_seconds": True}).status_code == 422
        assert client.post(f"{prefix}/wait", json={"session_ids": [sid], "mode": "any", "timeout_seconds": 0}).status_code == 200
        assert client.post(f"{prefix}/{sid}/interrupt").json()["interrupted"] is False
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(e.stranger, "stranger", "user")
        assert client.post(f"{prefix}/{sid}/message", json={"message": "takeover"}).status_code == 404
        assert client.post(f"{prefix}/{sid}/followup", json={"prompt": "takeover"}).status_code == 404
        assert client.post(f"{prefix}/{sid}/interrupt").status_code == 404
        assert client.post(f"{prefix}/wait", json={"session_ids": [sid]}).status_code == 404


def test_lead_wait_reports_children_phase_and_restores_tool_phase(team_env):
    e = team_env
    lead, _ = nodes(e)

    async def main():
        lead_runtime = e.manager.get_runtime(lead["session_id"], e.owner)
        started, release = asyncio.Event(), asyncio.Event()
        phases = []
        lead_runtime.phase = "tools"

        def set_phase(value, **kwargs):
            phases.append(value)
            lead_runtime.phase = value

        lead_runtime._set_phase = set_phase

        async def handler(runtime, prompt, *args):
            if prompt == "lead":
                child = await e.team.start(runtime.session_id, e.owner, "child")
                await e.team.wait(runtime.session_id, e.owner, [child["session_id"]], 1)
            else:
                started.set()
                await release.wait()
            return SimpleNamespace(text="done", stop_reason="end_turn")

        e.manager.handler = handler
        running = asyncio.create_task(lead_runtime.submit("lead"))
        await asyncio.wait_for(started.wait(), 1)
        assert e.team.descriptor(lead["session_id"])["phase"] == "waiting_children"
        assert e.team.descriptor(lead["session_id"])["blocked_reason"] == "等待子智能体结果"
        release.set()
        await asyncio.wait_for(running, 1)
        assert phases == ["waiting_children", "tools"]
        await e.team.aclose()

    asyncio.run(main())


def test_delegated_budget_is_strict_binds_exact_activation_and_resets_on_followup(team_env):
    e = team_env
    lead, owners = nodes(e)
    sid = owners["implementation"]["session_id"]
    runtime = e.manager.get_runtime(sid, e.owner)
    budgets = []
    runtime.set_execution_budget = lambda budget, *, activation_id, parent_session_id: budgets.append((activation_id, budget, parent_session_id))

    async def main():
        for invalid in (False, [], {"other": 1}, {"max_tokens": True}, {"max_seconds": 1.5},
                        {"max_tokens": -1}, {"max_tokens": 10000001}, {"max_seconds": 86401}):
            with pytest.raises(TeamError, match="budget"):
                await fixture_task(e, lead["session_id"], e.owner, "invalid", node_id="implementation", budget=invalid)
        assert e.team.descriptor(sid)["activation_id"] == 0
        first = await fixture_task(e, lead["session_id"], e.owner, "bounded", node_id="implementation", budget={"max_tokens": 12000})
        assert budgets == [(first["activation_id"], {"max_tokens": 12000, "max_seconds": 0}, lead["session_id"])]
        await settled(e, [sid])
        second = await e.team.followup(lead["session_id"], e.owner, sid, "new task")
        await settled(e, [sid])
        assert budgets[-1] == (second["activation_id"], None, lead["session_id"])
        await e.team.aclose()

    asyncio.run(main())


def test_budget_initialization_failure_cannot_strand_queued_reservation(team_env):
    e = team_env
    lead, owners = nodes(e)
    sid = owners["implementation"]["session_id"]
    runtime = e.manager.get_runtime(sid, e.owner)

    def failure(*args, **kwargs):
        raise RuntimeError("budget store unavailable")

    runtime.set_execution_budget = failure

    async def main():
        with pytest.raises(RuntimeError, match="budget store unavailable"):
            await fixture_task(e, lead["session_id"], e.owner, "bounded", node_id="implementation", budget={"max_seconds": 30})
        assert e.team.descriptor(sid)["state"] == "failed"
        assert sid not in e.team._starts and runtime.prompts == []
        assert "预算初始化失败" in e.team.descriptor(sid)["error"]
        await e.team.aclose()

    asyncio.run(main())

