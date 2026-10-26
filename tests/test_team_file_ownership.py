"""File responsibility is admitted with capacity, then checked at dispatch."""

import asyncio
from types import SimpleNamespace

import pytest

from code_agent.agent.file_ownership import Claim, FileOwnership, actual_target, contains, normalize_paths, scope_targets
from code_agent.agent.project_team import TeamError
from code_agent.tools.base import Tool, ToolContext, ToolError
from code_agent.tools.builtin import WriteFileTool
from test_project_agent_team import team_env


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.002)


def prepare(e):
    lead = e.team.sync(e.pid, e.owner)["lead"]
    return lead["session_id"]


async def start(e, lead, title, paths, **kwargs):
    return await e.team.start(lead, e.owner, title, title=title, write_paths=paths,
                              delegation_reason="独立完成并交付这个范围内的任务", **kwargs)


def test_conflicting_queue_does_not_occupy_capacity_or_block_other_files(team_env):
    e = team_env
    lead = prepare(e)

    async def scenario():
        release = asyncio.Event()
        entered = []

        async def handler(runtime, prompt, *args):
            entered.append(prompt)
            if prompt == "first":
                await release.wait()
            return SimpleNamespace(text="done", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            first = await start(e, lead, "first", ["same.txt"])
            await until(lambda: "first" in entered)
            blocked = await start(e, lead, "blocked", ["same.txt"])
            independent = await start(e, lead, "independent", ["other.txt"])
            await until(lambda: "independent" in entered)
            d = e.team.descriptor(blocked["session_id"])
            assert d["state"] == "queued" and d["phase"] == "waiting_files"
            assert d["file_ownership"]["waiting_for"][0]["session_id"] == first["session_id"]
            assert blocked["session_id"] not in e.team._capacity(e.pid).active
            assert "blocked" not in entered
            release.set()
            await until(lambda: e.team.descriptor(blocked["session_id"])["state"] == "completed")
            assert entered.index("blocked") > entered.index("independent")
            assert not e.team._file_ownership._claims
        finally:
            release.set()
            await e.team.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_holder", [False, True])
def test_cancellation_releases_only_settled_owner_and_queue_is_reusable(team_env, cancel_holder):
    e = team_env
    lead = prepare(e)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        e.manager.handler = None

        async def handler(runtime, prompt, *args):
            if prompt == "holder":
                entered.set()
                await release.wait()
            return SimpleNamespace(text="done", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            first = await start(e, lead, "holder", ["shared.txt"])
            await entered.wait()
            second = await start(e, lead, "waiter", ["shared.txt"])
            sid = first["session_id"] if cancel_holder else second["session_id"]
            await e.team.stop(lead, e.owner, sid)
            await until(lambda: sid not in e.team._executions)
            if not cancel_holder:
                assert e.team._file_ownership.held((first["session_id"], first["activation_id"]))
                release.set()
            third = await start(e, lead, "next", ["shared.txt"])
            await until(lambda: e.team.descriptor(third["session_id"])["state"] == "completed")
        finally:
            release.set()
            await e.team.aclose()
        assert not e.team._file_ownership._claims
        assert not e.team._capacity(e.pid).queue

    asyncio.run(scenario())


def test_parent_can_handoff_to_child_while_waiting_then_reacquire(team_env):
    e = team_env
    lead = prepare(e)
    observed = []

    async def scenario():
        async def handler(runtime, prompt, *args):
            if prompt == "parent":
                child = await start(e, runtime.session_id, "child", ["shared.txt"])
                assert e.team.descriptor(child["session_id"])["state"] == "queued"
                await e.team.wait(runtime.session_id, e.owner, [child["session_id"]], timeout=2)
                assert e.team.descriptor(runtime.session_id)["file_ownership"]["held"]
                observed.append("parent resumed")
            else:
                observed.append("child ran")
            return SimpleNamespace(text="done", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            parent = await start(e, lead, "parent", ["shared.txt"])
            await until(lambda: e.team.descriptor(parent["session_id"])["state"] == "completed")
            assert observed == ["child ran", "parent resumed"]
        finally:
            await e.team.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_dependencies_bind_exact_activation_and_wait_without_capacity(team_env, failure):
    e = team_env
    lead = prepare(e)

    async def scenario():
        release = asyncio.Event()
        observed = []

        async def handler(runtime, prompt, *args):
            observed.append(prompt)
            if prompt == "dependency":
                await release.wait()
                if failure:
                    raise RuntimeError("dependency failed")
            return SimpleNamespace(text="done", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            dep = await start(e, lead, "dependency", [])
            await until(lambda: "dependency" in observed)
            waiter = await start(e, lead, "dependent", [], depends_on=[dep["session_id"]])
            await until(lambda: e.team.descriptor(waiter["session_id"])["phase"] == "waiting_dependencies")
            assert waiter["session_id"] not in e.team._capacity(e.pid).active
            assert waiter["depends_on"] == [{"session_id": dep["session_id"], "activation_id": 1}]
            release.set()
            expected = "failed" if failure else "completed"
            await until(lambda: e.team.descriptor(waiter["session_id"])["state"] == expected)
            assert ("dependent" in observed) is not failure
        finally:
            release.set()
            await e.team.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("paths", [["../bad"], ["E:/absolute"], ["/absolute"], ["*.py"], [""]])
def test_invalid_scope_is_rejected_before_creating_child(team_env, paths):
    e = team_env
    lead = prepare(e)
    before = e.db.query_one("SELECT COUNT(*) AS n FROM sessions")["n"]
    with pytest.raises(TeamError):
        asyncio.run(start(e, lead, "invalid", paths))
    assert e.db.query_one("SELECT COUNT(*) AS n FROM sessions")["n"] == before


def test_native_multifile_guard_scope_and_lead_conflict(team_env):
    e = team_env
    lead = prepare(e)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(runtime, prompt, *args):
            ctx = ToolContext(runtime.session_id, e.root, e.owner)
            async with e.team.tool_write_guard(runtime.session_id, e.owner, "write_file", {"path": "owned.txt"}, ctx, WriteFileTool()):
                pass
            with pytest.raises(ToolError, match="超出"):
                async with e.team.tool_write_guard(runtime.session_id, e.owner, "write_file", {"path": "other.txt"}, ctx, WriteFileTool()):
                    pytest.fail("out of scope executed")
            entered.set()
            await release.wait()
            return SimpleNamespace(text="done", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            owner = await start(e, lead, "owner", ["owned.txt"])
            await entered.wait()
            activation = e.team._begin_activation(lead, "lead tool", "running")
            assert e.db.query_one("SELECT spec FROM project_agent_scopes WHERE session_id=? AND activation_id=?", (lead, activation)) is None
            ctx = ToolContext(lead, e.root, e.owner)
            with pytest.raises(ToolError, match="子任务负责"):
                async with e.team.tool_write_guard(lead, e.owner, "write_file", {"path": "owned.txt"}, ctx, WriteFileTool()):
                    pytest.fail("Lead ignored worker ownership")
            async with e.team.tool_write_guard(lead, e.owner, "write_file", {"path": "free.txt"}, ctx, WriteFileTool()):
                assert e.team.descriptor(owner["session_id"])["file_ownership"]["held"]
            e.team._finish(lead, activation, "completed", result="done")
        finally:
            release.set()
            await e.team.aclose()
        assert not e.team._file_ownership._claims

    asyncio.run(scenario())


@pytest.mark.parametrize("paths", [[], ["owned.txt"], ["."]])
def test_shell_is_restricted_and_true_background_parameter_is_blocked(team_env, paths):
    e = team_env
    lead = prepare(e)

    async def scenario():
        async def handler(runtime, prompt, *args):
            ctx = ToolContext(runtime.session_id, e.root, e.owner)
            args = {"command": "unused", "run_in_background": True}
            with pytest.raises(ToolError):
                async with e.team.tool_write_guard(runtime.session_id, e.owner, "bash", args, ctx, object()):
                    pytest.fail("unbounded/background shell executed")
            return SimpleNamespace(text="done", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            child = await start(e, lead, "guard", paths)
            await until(lambda: e.team.descriptor(child["session_id"])["state"] == "completed")
        finally:
            await e.team.aclose()

    asyncio.run(scenario())


def test_canonical_paths_conflict_across_workspaces_but_isolated_roots_do_not(tmp_path):
    first, isolated = tmp_path / "first", tmp_path / "isolated"
    first.mkdir()
    isolated.mkdir()
    table = FileOwnership()
    a = actual_target(first, "src/", directory=True)
    assert table.claim("a", Claim("one", 1, (a,)))
    assert not table.claim("b", Claim("two", 1, (actual_target(first, "src/file.py"),)))
    assert table.claim("c", Claim("three", 1, (actual_target(isolated, "src/file.py"),)))
    assert normalize_paths(first, ["./src/", "src/"]) == ["src/"]


def test_file_authority_does_not_expand_if_path_later_becomes_directory(tmp_path):
    declared = normalize_paths(tmp_path, ["later"])
    (tmp_path / "later").mkdir()
    scope = scope_targets(tmp_path, declared)[0]
    assert not scope.directory
    assert not contains(scope, actual_target(tmp_path, "later/child.py"))
    assert not contains(scope, actual_target(tmp_path, "later/", directory=True))


@pytest.mark.parametrize("root_alias", ["./", ".\\"])
def test_workspace_scope_mode_uses_normalized_root_alias(team_env, root_alias):
    e = team_env
    lead = e.team._row(prepare(e))
    spec = e.team._prepare_scope(lead, e.root, [root_alias], "全工作区集成", None)
    assert spec["write_paths"] == ["."] and spec["scope_mode"] == "workspace_write"


def test_scope_context_is_bounded_without_truncating_persisted_authority(team_env):
    e = team_env
    lead = prepare(e)

    async def scenario():
        paths = [f"dir/{i}-" + "a" * 200 + ".py" for i in range(64)]
        child = await start(e, lead, "many files", paths)
        row = e.team._row(child["session_id"])
        brief = e.team._scope_context(row)
        assert len(brief) < 6000 and "omitted_paths" in brief and "full_roster_path" in brief
        assert e.team.descriptor(child["session_id"])["write_paths"] == paths
        await e.team.aclose()

    asyncio.run(scenario())


def test_cancelled_owner_keeps_files_until_actual_cleanup_finishes(team_env):
    e = team_env
    lead = prepare(e)

    async def scenario():
        entered, cleaning, finish_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []

        async def handler(runtime, prompt, *args):
            seen.append(prompt)
            if prompt == "holder":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleaning.set()
                    await finish_cleanup.wait()
                    raise
            return SimpleNamespace(text="done", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            first = await start(e, lead, "holder", ["shared.txt"])
            await entered.wait()
            second = await start(e, lead, "after cleanup", ["shared.txt"])
            await e.team.stop(lead, e.owner, first["session_id"])
            await cleaning.wait()
            assert e.team.descriptor(first["session_id"])["state"] == "cancelled"
            assert e.team.descriptor(first["session_id"])["file_ownership"]["held"]
            await asyncio.sleep(0.02)
            assert "after cleanup" not in seen
            finish_cleanup.set()
            await until(lambda: e.team.descriptor(second["session_id"])["state"] == "completed")
        finally:
            finish_cleanup.set()
            await e.team.aclose()

    asyncio.run(scenario())


def test_followup_inherits_scope_but_dependency_uses_original_activation(team_env):
    e = team_env
    lead = prepare(e)

    async def scenario():
        release, second_entered, dependent_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(runtime, prompt, *args):
            if prompt == "second activation":
                second_entered.set()
                await release.wait()
            if prompt == "dependent":
                await second_entered.wait()
                dependent_entered.set()
            return SimpleNamespace(text="done", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            dep = await start(e, lead, "first activation", [])
            await until(lambda: e.team.descriptor(dep["session_id"])["state"] == "completed")
            dependent = await start(e, lead, "dependent", [], depends_on=[dep["session_id"]])
            again = await e.team.followup(lead, e.owner, dep["session_id"], "second activation")
            assert again["activation_id"] == 2 and again["write_paths"] == []
            await asyncio.wait_for(dependent_entered.wait(), 2)
            assert dependent["depends_on"] == [{"session_id": dep["session_id"], "activation_id": 1}]
            assert not release.is_set()
        finally:
            release.set()
            await e.team.aclose()

    asyncio.run(scenario())


def test_descendant_cannot_expand_parent_scope_and_ancestor_dependency_is_rejected(team_env):
    e = team_env
    lead = prepare(e)

    async def scenario():
        parent = await start(e, lead, "parent", ["one.txt"])
        await until(lambda: e.team.descriptor(parent["session_id"])["state"] == "completed")
        with pytest.raises(TeamError, match="不能超出"):
            await start(e, parent["session_id"], "wide child", ["."])
        child = await start(e, parent["session_id"], "narrow child", ["one.txt"])
        await until(lambda: e.team.descriptor(child["session_id"])["state"] == "completed")
        with pytest.raises(TeamError, match="父执行者"):
            await e.team.followup(lead, e.owner, child["session_id"], "wide again", write_paths=["."])
        with pytest.raises(TeamError, match="祖先"):
            await e.team.followup(lead, e.owner, child["session_id"], "wait parent", depends_on=[parent["session_id"]])
        await e.team.aclose()

    asyncio.run(scenario())
