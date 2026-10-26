"""Final-dispatch team guards and conservative tool identity classification."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from code_agent.agent.router import ModelRouter
from code_agent.agent.loop import AgentLoop
from code_agent.agent.team_tool_safety import classify_team_tool
from code_agent.models.events import ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool, ToolContext, ToolError
from code_agent.tools.builtin.code_tools import EditManyTool, ListDirTool
from code_agent.tools.builtin.propose_edit import ProposeEditTool
from code_agent.tools.builtin.tools_builtin import BashTool, BashPersistentTool, ReadFileTool, WriteFileTool
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry
from test_project_agent_team import team_env


class ScopeGuard:
    def __init__(self, allowed):
        self.allowed = set(allowed)
        self.entries = []
        self.exits = []

    @asynccontextmanager
    async def __call__(self, name, arguments, ctx, tool):
        targets = tuple(tool.write_targets(arguments))
        self.entries.append((name, targets))
        if set(targets) - self.allowed:
            raise ToolError("outside responsibility")
        try:
            yield
        finally:
            self.exits.append(name)


@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_final_guard_covers_direct_and_batch_dispatch_once(tmp_path, filtered, batch):
    inner = ToolRegistry()
    inner.register(WriteFileTool())
    registry = FilteredToolRegistry(inner, exclude=()) if filtered else inner
    guard = ScopeGuard({"owned.txt"})
    ctx = ToolContext("s_guard", tmp_path, extra={"team_write_guard": guard})

    async def scenario():
        allowed = {"path": "owned.txt", "content": "owned"}
        denied = {"path": "other.txt", "content": "must not write"}
        if batch:
            results = await asyncio.wait_for(registry.dispatch_all([
                ("write_file", allowed), ("write_file", denied),
            ], ctx), 2)
            assert "outside responsibility" in results[1]
        else:
            await registry.dispatch("write_file", allowed, ctx)
            with pytest.raises(ToolError, match="outside responsibility"):
                await registry.dispatch("write_file", denied, ctx)

    asyncio.run(scenario())
    assert (tmp_path / "owned.txt").read_text() == "owned"
    assert not (tmp_path / "other.txt").exists()
    assert len(guard.entries) == 2 and guard.exits == ["write_file"]
    assert inner._write_lease.size() == 0


def test_native_multi_path_edit_is_rejected_before_any_file_changes(tmp_path):
    for name in ("owned.txt", "other.txt"):
        (tmp_path / name).write_text("before", encoding="utf8")
    registry = ToolRegistry()
    registry.register(EditManyTool())
    guard = ScopeGuard({"owned.txt"})
    ctx = ToolContext("s_guard", tmp_path, extra={"team_write_guard": guard})
    arguments = {"edits": [{"path": name, "old_string": "before", "new_string": "after"}
                           for name in ("owned.txt", "other.txt")]}
    with pytest.raises(ToolError, match="outside responsibility"):
        asyncio.run(registry.dispatch("edit_many", arguments, ctx))
    assert guard.entries == [("edit_many", ("owned.txt", "other.txt"))]
    assert all((tmp_path / name).read_text() == "before" for name in ("owned.txt", "other.txt"))


def test_non_team_context_without_guard_keeps_normal_dispatch(tmp_path):
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    asyncio.run(registry.dispatch("write_file", {"path": "normal.txt", "content": "ok"},
                                  ToolContext("s_ordinary", tmp_path)))
    assert (tmp_path / "normal.txt").read_text() == "ok"


def test_invalid_guard_fails_closed(tmp_path):
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    ctx = ToolContext("s_guard", tmp_path, extra={"team_write_guard": False})
    with pytest.raises(ToolError, match="责任校验不可用"):
        asyncio.run(registry.dispatch("write_file", {"path": "denied.txt", "content": "no"}, ctx))
    assert not (tmp_path / "denied.txt").exists()


@pytest.mark.parametrize("cancel", [False, True])
def test_guard_releases_reservation_when_tool_fails_or_is_cancelled(tmp_path, cancel):
    class BlockingTool(Tool):
        name = "blocking"

        async def execute(self, arguments, ctx):
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise ToolError("execution failed")

    guard = ScopeGuard(set())
    registry = ToolRegistry()
    registry.register(BlockingTool())
    ctx = ToolContext("s_guard", tmp_path, extra={"team_write_guard": guard})

    async def scenario():
        nonlocal entered
        entered = asyncio.Event()
        task = asyncio.create_task(registry.dispatch("blocking", {}, ctx))
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ToolError):
            await task

    entered = None
    asyncio.run(scenario())
    assert guard.exits == ["blocking"]


@pytest.mark.parametrize("mode", ["full-access", "read-only"])
def test_runtime_bridge_denies_final_write_even_after_full_access_or_approval(agent_home, mode):
    db = Database(agent_home / "write-guard.db")
    user = UserRepo(db).create("write-guard", "test-only")["id"]
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    manager = SessionManager(TranscriptStore(agent_home / "transcripts"), SessionRepo(db), ApprovalRepo(db),
                             ModelRouter(provider_name="offline"), registry)
    meta = manager.create(user, permission_mode=mode)
    runtime = manager.get_runtime(meta["id"], user)
    guard = ScopeGuard(set())
    identities, prechecks, approvals = [], [], []

    def bridge(session_id, user_id, name, arguments, ctx, tool):
        identities.append((session_id, user_id, ctx.session_id, ctx.user_id))
        return guard(name, arguments, ctx, tool)

    def precheck(session_id, user_id, name, arguments, ctx, tool):
        prechecks.append((session_id, user_id, ctx.session_id, ctx.user_id))
        assert name == "write_file" and tool is registry.get(name)
        assert "_approval" not in arguments

    async def approve(request):
        approvals.append(request)
        return True

    runtime.agent_team = SimpleNamespace(tool_write_guard=bridge, check_tool_scope=precheck)
    runtime._make_permissions = lambda: PermissionManager(runtime.workspace, approve, mode,
                                                          read_only_approval=True)
    loop = runtime._build_loop(prepared_context=("test", registry))
    ctx = ToolContext(meta["id"], runtime.workspace, user, extra=loop._tool_extra())
    call = ToolCall(name="write_file", arguments={"path": "other.txt", "content": "must not write",
        "_approval": {"purpose": "write one test file", "effects": ["create other.txt"]}})
    try:
        result = asyncio.run(loop._execute_tool(call, ctx))
        assert "outside responsibility" in result
        assert not (runtime.workspace / "other.txt").exists()
        assert len(guard.entries) == 1
        assert identities == [(meta["id"], user, meta["id"], user)]
        assert prechecks == identities
        assert len(approvals) == (1 if mode == "read-only" else 0)
    finally:
        runtime.shutdown()
        db.close()


def test_classifier_uses_native_class_identity_and_write_effects():
    assert classify_team_tool("read_file", ReadFileTool(), {}) == "read"
    assert classify_team_tool("list_dir", ListDirTool(), {}) == "read"
    assert classify_team_tool("propose_edit", ProposeEditTool(), {}) == "read"
    assert classify_team_tool("write_file", WriteFileTool(), {}) == "write"
    assert classify_team_tool("edit_many", EditManyTool(), {"edits": []}) == "write"
    for name in ("bash", "bash_persistent"):
        assert classify_team_tool(name, SimpleNamespace(writes_path=False), {}) == "shell"

    class PretendRead(ReadFileTool):
        async def execute(self, arguments, ctx):
            raise AssertionError("plugin override must never be trusted")

    assert classify_team_tool("read_file", PretendRead(), {}) == "opaque"
    assert classify_team_tool("read_file", SimpleNamespace(writes_path=False), {}) == "opaque"
    patched = ReadFileTool()
    patched.execute = lambda *args: None
    assert classify_team_tool("read_file", patched, {}) == "opaque"
    declared = SimpleNamespace(writes_path=False, write_targets=lambda args: ["owned.txt"])
    assert classify_team_tool("read_file", declared, {}) == "write"


def test_classifier_keeps_real_team_coordination_available():
    from code_agent.tools.builtin.agent_team_tools import register_agent_team_tools

    registry = ToolRegistry()
    register_agent_team_tools(registry, SimpleNamespace())
    for name in registry.names():
        assert classify_team_tool(name, registry.get(name), {}) == "read", name


async def _until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.002)


def _writable_lead(env):
    lead = env.team.sync(env.pid, env.owner)["lead"]["session_id"]
    env.manager.project_repo.set_flags(env.pid, allow_direct_write=True)
    env.manager.session_repo.set_permission_mode(lead, "full-access")
    return lead


def _team_context(env, session_id):
    return ToolContext(session_id, env.root, env.owner, extra={
        "team_write_guard": lambda name, arguments, ctx, tool:
            env.team.tool_write_guard(session_id, env.owner, name, arguments, ctx, tool),
        "team_write_precheck": lambda name, arguments, ctx, tool:
            env.team.check_tool_scope(session_id, env.owner, name, arguments, ctx, tool),
    })


@pytest.mark.parametrize("second_kind", ["opaque", "native"])
def test_real_team_serializes_same_session_opaque_and_native_dispatch(team_env, second_kind):
    e = team_env
    lead = _writable_lead(e)
    observed = []

    async def scenario():
        first_entered, second_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class OpaqueProbe(Tool):
            name = "plugin_operation"

            async def execute(self, arguments, ctx):
                if arguments.get("tag") == "first":
                    first_entered.set()
                    await release.wait()
                    observed.append("first finished")
                else:
                    second_entered.set()
                    observed.append("second finished")
                return "done"

        class NativeProbe(Tool):
            name = "native_operation"
            writes_path = True

            async def execute(self, arguments, ctx):
                second_entered.set()
                (ctx.workspace / arguments["path"]).write_text("native finished", encoding="utf8")
                observed.append("second finished")
                return "done"

        registry = ToolRegistry()
        registry.register(OpaqueProbe())
        registry.register(NativeProbe())

        async def handler(runtime, prompt, *args):
            second = ("plugin_operation", {"tag": "second"}) if second_kind == "opaque" else (
                "native_operation", {"path": "shared.txt"})
            batch = asyncio.create_task(registry.dispatch_all([
                ("plugin_operation", {"tag": "first"}), second,
            ], _team_context(e, runtime.session_id)))
            try:
                await asyncio.wait_for(first_entered.wait(), 2)
                # The batch has scheduled both calls. The first owns the whole
                # workspace; a sibling call cannot enter until it releases it.
                await asyncio.sleep(0)
                assert not second_entered.is_set()
            finally:
                release.set()
                results = await asyncio.wait_for(batch, 2)
            assert results == ["done", "done"]
            return SimpleNamespace(text="completed guarded calls", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            child = await e.team.start(lead, e.owner, "exercise whole-workspace guard", write_paths=["."],
                                       delegation_reason="验证同一执行者未知工具和原生工具按顺序落盘")
            await _until(lambda: child["session_id"] not in e.team._starts
                         and child["session_id"] not in e.team._executions)
            assert e.team.descriptor(child["session_id"])["state"] == "completed"
            assert observed == ["first finished", "second finished"]
            assert second_entered.is_set()
            if second_kind == "native":
                assert (e.root / "shared.txt").read_text() == "native finished"
        finally:
            release.set()
            await e.team.aclose()
        assert not e.team._file_ownership._claims

    asyncio.run(scenario())


def test_real_lead_transient_write_ownership_blocks_child_admission(team_env):
    e = team_env
    lead = _writable_lead(e)

    async def scenario():
        tool_entered, child_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class HoldingWrite(Tool):
            name = "holding_write"
            writes_path = True

            async def execute(self, arguments, ctx):
                tool_entered.set()
                await release.wait()
                (ctx.workspace / arguments["path"]).write_text("Lead finished", encoding="utf8")
                return "written"

        registry = ToolRegistry()
        registry.register(HoldingWrite())

        async def handler(runtime, prompt, *args):
            child_entered.set()
            assert (e.root / "shared.txt").read_text() == "Lead finished"
            return SimpleNamespace(text="child observed final Lead write", stop_reason="end_turn")

        e.manager.handler = handler
        activation = e.team._begin_activation(lead, "Lead writes one file", "running")
        tool_task = asyncio.create_task(registry.dispatch("holding_write", {"path": "shared.txt"},
                                                          _team_context(e, lead)))
        try:
            await asyncio.wait_for(tool_entered.wait(), 2)
            child = await e.team.start(lead, e.owner, "continue same file", write_paths=["shared.txt"],
                                       delegation_reason="接收主代理完成后的文件并继续独立处理")
            await _until(lambda: e.team.descriptor(child["session_id"])["phase"] == "waiting_files")
            assert not child_entered.is_set()
            assert child["session_id"] not in e.team._capacity(e.pid).active
            blockers = e.team.descriptor(child["session_id"])["file_ownership"]["waiting_for"]
            assert blockers[0]["session_id"] == lead
            release.set()
            assert await asyncio.wait_for(tool_task, 2) == "written"
            await _until(lambda: child["session_id"] not in e.team._starts
                         and child["session_id"] not in e.team._executions)
            assert child_entered.is_set()
            assert e.team.descriptor(child["session_id"])["state"] == "completed"
            e.team._finish(lead, activation, "completed", result="Lead tool completed")
        finally:
            release.set()
            await asyncio.gather(tool_task, return_exceptions=True)
            await e.team.aclose()
        assert not e.team._file_ownership._claims

    asyncio.run(scenario())


def test_real_full_access_child_cannot_write_outside_native_multi_path_scope(team_env):
    e = team_env
    lead = _writable_lead(e)
    for name in ("owned.txt", "other.txt"):
        (e.root / name).write_text("before", encoding="utf8")
    results = []

    async def scenario():
        registry = ToolRegistry()
        registry.register(WriteFileTool())
        registry.register(EditManyTool())

        async def handler(runtime, prompt, *args):
            assert e.team.effective_permission(runtime.session_id, e.owner) == "full-access"
            loop = AgentLoop(OfflineProvider(), registry, PermissionManager(e.root, mode="full-access"),
                             TranscriptStore(e.tmp_path / "guard-transcripts"),
                             session_id=runtime.session_id, workspace=e.root)
            ctx = _team_context(e, runtime.session_id)
            before = {name: (e.root / name).read_bytes() for name in ("owned.txt", "other.txt")}
            for name, arguments in [
                ("write_file", {"path": "other.txt", "content": "must not write"}),
                ("edit_many", {"edits": [{"path": path, "old_string": "before", "new_string": "after"}
                                           for path in ("owned.txt", "other.txt")]}),
            ]:
                result = await loop._execute_tool(ToolCall(name=name, arguments=arguments), ctx)
                assert "超出本轮负责范围" in result
                assert all((e.root / path).read_bytes() == content for path, content in before.items())
                results.append(result)
            result = await loop._execute_tool(ToolCall(name="write_file", arguments={
                "path": "owned.txt", "content": "authorized change"}), ctx)
            assert not result.startswith("[tool error]")
            assert (e.root / "owned.txt").read_text() == "authorized change"
            return SimpleNamespace(text="full-access native writes remained scoped", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            child = await e.team.start(lead, e.owner, "write owned file", write_paths=["owned.txt"],
                                       delegation_reason="仅修改明确负责文件，拒绝同批次越界修改")
            await _until(lambda: child["session_id"] not in e.team._starts
                         and child["session_id"] not in e.team._executions)
            assert e.team.descriptor(child["session_id"])["state"] == "completed"
            assert len(results) == 2
            assert (e.root / "other.txt").read_text() == "before"
        finally:
            await e.team.aclose()
        assert not e.team._file_ownership._claims

    asyncio.run(scenario())


def test_real_guard_rejects_stale_permission_after_waiting_for_another_tool(team_env):
    e = team_env
    lead = _writable_lead(e)

    async def scenario():
        first_entered, second_authorized, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class OpaqueProbe(Tool):
            name = "opaque_permission_probe"

            async def execute(self, arguments, ctx):
                if arguments["tag"] == "first":
                    first_entered.set()
                    await release.wait()
                else:
                    (ctx.workspace / "after-revoke.txt").write_text("stale grant", encoding="utf8")
                return "done"

        class TrackedPermissions(PermissionManager):
            second_checks = 0

            async def evaluate(self, name, arguments, ctx):
                result = await super().evaluate(name, arguments, ctx)
                if arguments.get("tag") == "second":
                    self.second_checks += 1
                    if self.second_checks == 2:
                        second_authorized.set()
                return result

        registry = ToolRegistry()
        registry.register(OpaqueProbe())
        permissions = TrackedPermissions(e.root, mode="full-access",
            mode_provider=lambda: e.team.effective_permission(lead, e.owner))
        loop = AgentLoop(OfflineProvider(), registry, permissions,
                         TranscriptStore(e.tmp_path / "permission-transcripts"), session_id=lead, workspace=e.root)
        ctx = _team_context(e, lead)
        activation = e.team._begin_activation(lead, "permission race", "running")
        tasks = []
        try:
            tasks.append(asyncio.create_task(loop._execute_tool(ToolCall(name=OpaqueProbe.name,
                arguments={"tag": "first"}), ctx)))
            await asyncio.wait_for(first_entered.wait(), 2)
            tasks.append(asyncio.create_task(loop._execute_tool(ToolCall(name=OpaqueProbe.name,
                arguments={"tag": "second"}), ctx)))
            # Its initial and post-lease permission checks have both allowed
            # the second call; only the final team guard is still waiting.
            await asyncio.wait_for(second_authorized.wait(), 2)
            e.manager.session_repo.set_permission_mode(lead, "read-only")
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
            assert results[0] == "done"
            assert "权限" in results[1] and results[1].startswith("[tool error]")
            assert not (e.root / "after-revoke.txt").exists()
            e.team._finish(lead, activation, "completed", result="stale permission rejected")
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await e.team.aclose()
        assert not e.team._file_ownership._claims

    asyncio.run(scenario())


def test_real_scoped_child_keeps_single_approval_when_read_only_permission_stays_unchanged(team_env):
    e = team_env
    lead = _writable_lead(e)
    approvals = []

    async def scenario():
        registry = ToolRegistry()
        registry.register(WriteFileTool())

        async def approve(request):
            approvals.append(request)
            return True

        async def handler(runtime, prompt, *args):
            # The activation already owns its declared file. A stable read-only
            # grant can approve this exact call without enlarging that scope.
            e.manager.session_repo.set_permission_mode(lead, "read-only")
            permissions = PermissionManager(e.root, approve, "read-only", read_only_approval=True,
                mode_provider=lambda: e.team.effective_permission(runtime.session_id, e.owner))
            loop = AgentLoop(OfflineProvider(), registry, permissions,
                             TranscriptStore(e.tmp_path / "approval-transcripts"),
                             session_id=runtime.session_id, workspace=e.root)
            result = await loop._execute_tool(ToolCall(name="write_file", arguments={
                "path": "approved.txt", "content": "one approved scoped write",
                "_approval": {"purpose": "write the already assigned test file", "effects": ["create approved.txt"]},
            }), _team_context(e, runtime.session_id))
            assert not result.startswith("[tool error]") and not result.startswith("[permission denied]")
            assert e.team.effective_permission(runtime.session_id, e.owner) == "read-only"
            assert (e.root / "approved.txt").read_text() == "one approved scoped write"
            return SimpleNamespace(text="approved scoped write completed", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            child = await e.team.start(lead, e.owner, "scoped write before permission change",
                                       write_paths=["approved.txt"], delegation_reason="修改已独占的指定文件，审批不扩大范围")
            await _until(lambda: child["session_id"] not in e.team._starts
                         and child["session_id"] not in e.team._executions)
            assert e.team.descriptor(child["session_id"])["state"] == "completed"
            assert len(approvals) == 1
        finally:
            await e.team.aclose()
        assert not e.team._file_ownership._claims

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", [[], ["owned.txt"]], ids=["read-only-scope", "file-scope"])
@pytest.mark.parametrize("tool_name", ["bash", "bash_persistent", "write_file", "edit_many"])
def test_real_scope_precheck_rejects_impossible_actions_before_requesting_approval(team_env, scope, tool_name):
    e = team_env
    lead = _writable_lead(e)
    for name in ("owned.txt", "other.txt"):
        (e.root / name).write_text("before", encoding="utf8")
    before = {name: (e.root / name).read_bytes() for name in ("owned.txt", "other.txt")}
    approvals, executions, results = [], [], []

    async def scenario():
        registry = ToolRegistry()
        tool = {"bash": BashTool, "bash_persistent": BashPersistentTool,
                "write_file": WriteFileTool, "edit_many": EditManyTool}[tool_name]()

        async def forbidden_execute(arguments, ctx):
            executions.append(tool_name)
            raise AssertionError("a scope-forbidden operation must never execute")

        tool.execute = forbidden_execute
        registry.register(tool)

        async def approve(request):
            approvals.append(request)
            return True

        async def handler(runtime, prompt, *args):
            e.manager.session_repo.set_permission_mode(lead, "read-only")
            permissions = PermissionManager(e.root, approve, "read-only", read_only_approval=True,
                mode_provider=lambda: e.team.effective_permission(runtime.session_id, e.owner))
            loop = AgentLoop(OfflineProvider(), registry, permissions,
                             TranscriptStore(e.tmp_path / "precheck-transcripts"),
                             session_id=runtime.session_id, workspace=e.root)
            if tool_name in {"bash", "bash_persistent"}:
                arguments = {"command": "echo must-not-execute"}
            elif tool_name == "write_file":
                arguments = {"path": "other.txt", "content": "must not write"}
            else:
                arguments = {"edits": [{"path": path, "old_string": "before", "new_string": "after"}
                                       for path in ("owned.txt", "other.txt")]}
            arguments["_approval"] = {"purpose": "exercise the scope boundary",
                                      "effects": ["would run the requested operation if it were in scope"]}
            result = await loop._execute_tool(ToolCall(name=tool_name, arguments=arguments),
                                               _team_context(e, runtime.session_id))
            assert result.startswith("[tool error]")
            assert "超出" in result or "不能执行" in result
            assert not approvals and not executions
            assert all((e.root / path).read_bytes() == contents for path, contents in before.items())
            results.append(result)
            return SimpleNamespace(text="scope precheck rejected before approval", stop_reason="end_turn")

        e.manager.handler = handler
        try:
            child = await e.team.start(lead, e.owner, "scope precheck", write_paths=scope,
                                       delegation_reason="确认责任范围之外的操作不会打扰用户审批")
            await _until(lambda: child["session_id"] not in e.team._starts
                         and child["session_id"] not in e.team._executions)
            assert e.team.descriptor(child["session_id"])["state"] == "completed"
            assert len(results) == 1 and not approvals and not executions
        finally:
            await e.team.aclose()

    asyncio.run(scenario())
