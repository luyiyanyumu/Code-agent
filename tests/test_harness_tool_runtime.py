"""The official driver must not bypass Code-agent's Python execution boundary."""

from __future__ import annotations

import asyncio

import pytest

from code_agent.agent.tool_runtime import ToolRuntimeMixin
from code_agent.models.events import ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool, ToolContext, ToolError
from code_agent.tools.registry import ToolRegistry


class Runtime(ToolRuntimeMixin):
    def __init__(self, tmp_path, tool=None, *, mode="full-access", approver=None):
        self.registry = ToolRegistry()
        if tool is not None:
            self.registry.register(tool)
        self.permissions = PermissionManager(workspace_root=tmp_path, mode=mode, approver=approver)
        self.transcript = TranscriptStore(tmp_path / "transcripts")
        self.session_id = "test-harness-safety"
        self.on_event = None
        self.audit = []
        self.on_tool_executed = lambda name, args, outcome: self.audit.append((name, args, outcome))
        self.memory_root = None
        self.session_memory_root = None
        self.project_id = ""
        self.on_question = self.jobs = self.shells = self.on_pending_notes = None
        self.before_tool_dispatch = self.after_tool_dispatch = None
        self.ctx = ToolContext(self.session_id, tmp_path)


class WriteProbe(Tool):
    name = "probe"
    writes_path = True

    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None

    def write_targets(self, arguments):
        return arguments.get("paths", [arguments.get("path", "shared.txt")])

    async def execute(self, arguments, ctx):
        self.calls.append(arguments)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return "actual tool result"


def call(arguments=None):
    return ToolCall(id="call-safety", name="probe", arguments=arguments or {"path": "shared.txt"})


def test_unavailable_tool_and_team_contract_are_checked_before_approval(tmp_path):
    async def scenario():
        approvals = []

        async def approve(request):
            approvals.append(request)
            return True

        runtime = Runtime(tmp_path, approver=approve)
        assert "not available" in await runtime._execute_tool(call(), runtime.ctx)
        tool = WriteProbe()
        runtime.registry.register(tool)

        def deny_contract(*args):
            raise ToolError("outside assigned files")

        runtime.ctx.extra["team_write_precheck"] = deny_contract
        assert "outside assigned files" in await runtime._execute_tool(call(), runtime.ctx)
        assert not tool.calls
        assert not approvals

    asyncio.run(scenario())


def test_approval_cannot_override_permission_changed_to_deny(tmp_path):
    async def scenario():
        tool = WriteProbe()
        runtime = Runtime(tmp_path, tool, mode="default")

        async def approve(request):
            runtime.permissions.mode = "read-only"
            return True

        runtime.permissions.approver = approve
        arguments = {"path": "shared.txt", "_approval": {
            "purpose": "Verify a narrowly scoped test write", "effects": ["Write shared.txt"],
        }}
        result = await runtime._execute_tool(call(arguments), runtime.ctx)
        assert result.startswith("[permission denied]")
        assert not tool.calls
        assert runtime.audit[-1][2] == "denied"

    asyncio.run(scenario())


def test_permission_is_rechecked_after_waiting_for_shared_write_lease(tmp_path):
    async def scenario():
        tool = WriteProbe()
        runtime = Runtime(tmp_path, tool)
        entered_capacity = asyncio.Event()

        async def before():
            entered_capacity.set()

        runtime.before_tool_dispatch = before
        async with runtime.registry.write_lease("probe", call().arguments, runtime.ctx):
            task = asyncio.create_task(runtime._execute_tool(call(), runtime.ctx))
            await entered_capacity.wait()
            runtime.permissions.mode = "read-only"
        assert (await task).startswith("[permission denied]")
        assert not tool.calls
        assert runtime.registry._write_lease.size() == 0

    asyncio.run(scenario())


def test_cancellation_releases_every_batch_path_and_paired_capacity_hook(tmp_path):
    async def scenario():
        tool = WriteProbe()
        tool.release = asyncio.Event()
        runtime = Runtime(tmp_path, tool)
        hooks = []

        async def before():
            hooks.append("enter")

        async def after():
            hooks.append("leave")

        runtime.before_tool_dispatch, runtime.after_tool_dispatch = before, after
        task = asyncio.create_task(runtime._execute_tool(call({"paths": ["a.txt", "b.txt"]}), runtime.ctx))
        await tool.entered.wait()
        assert runtime.registry._write_lease.size() == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert hooks == ["enter", "leave"]
        assert runtime.registry._write_lease.size() == 0
        assert not runtime.audit
        tool.release.set()
        assert await runtime._execute_tool(call({"paths": ["b.txt", "a.txt"]}), runtime.ctx) == "actual tool result"
        assert hooks == ["enter", "leave", "enter", "leave"]

    asyncio.run(scenario())


def test_cancellation_during_approval_never_dispatches(tmp_path):
    async def scenario():
        waiting = asyncio.Event()

        async def approve(request):
            waiting.set()
            await asyncio.Event().wait()

        tool = WriteProbe()
        runtime = Runtime(tmp_path, tool, mode="default", approver=approve)
        task = asyncio.create_task(runtime._execute_tool(call({"_approval": {
            "purpose": "Verify cancellation during approval", "effects": ["Write shared.txt"],
        }}), runtime.ctx))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not tool.calls
        assert runtime.registry._write_lease.size() == 0

    asyncio.run(scenario())


def test_declared_timeout_and_tool_raised_timeout_have_distinct_outcomes(tmp_path):
    async def scenario():
        tool = WriteProbe()
        tool.timeout = 0.01
        tool.release = asyncio.Event()
        runtime = Runtime(tmp_path, tool)
        assert "[tool timeout]" in await runtime._execute_tool(call(), runtime.ctx)
        assert runtime.audit[-1][2] == "timeout"
        assert runtime.registry._write_lease.size() == 0

        class NativeTimeout(WriteProbe):
            async def execute(self, arguments, ctx):
                raise TimeoutError("socket timed out")

        runtime.registry.register(NativeTimeout())
        result = await runtime._execute_tool(call(), runtime.ctx)
        assert result == "[tool error] TimeoutError: socket timed out"
        assert runtime.audit[-1][2] == "error:TimeoutError"

    asyncio.run(scenario())


def test_waiting_for_a_write_lease_does_not_consume_dispatch_timeout(tmp_path):
    async def scenario():
        tool = WriteProbe()
        tool.timeout = 0.01
        runtime = Runtime(tmp_path, tool)
        queued = asyncio.Event()

        async def before():
            queued.set()

        runtime.before_tool_dispatch = before
        async with runtime.registry.write_lease("probe", call().arguments, runtime.ctx):
            task = asyncio.create_task(runtime._execute_tool(call(), runtime.ctx))
            await queued.wait()
            await asyncio.sleep(0.03)
        assert await task == "actual tool result"
        assert runtime.audit[-1][2] == "ok"

    asyncio.run(scenario())


def test_approval_metadata_is_removed_from_execution_and_audit_arguments(tmp_path):
    async def scenario():
        tool = WriteProbe()
        runtime = Runtime(tmp_path, tool)
        arguments = {"path": "shared.txt", "_approval": {"purpose": "Only for approval", "effects": ["Test"]}}
        assert await runtime._execute_tool(call(arguments), runtime.ctx) == "actual tool result"
        assert tool.calls == [{"path": "shared.txt"}]
        assert runtime.audit == [("probe", '{"path": "shared.txt"}', "ok")]

    asyncio.run(scenario())
