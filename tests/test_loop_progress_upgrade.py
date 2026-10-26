"""Offline runtime probes for immediate progress, leases and cancellation."""

from __future__ import annotations

import asyncio
import copy

import pytest

from code_agent.agent import loop as loop_module
from code_agent.agent.loop import AgentLoop
from code_agent.models.events import EventType, ModelTurn, TokenUsage, ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy, Decision, PolicyResult
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool
from code_agent.tools.registry import ToolRegistry


class CaptureProvider(OfflineProvider):
    def __init__(self, calls):
        super().__init__(script=[ModelTurn(tool_calls=calls), self.text_turn("done")])
        self.requests = []

    async def chat(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        return await super().chat(messages, tools)


def make_loop(workspace, registry, calls, *, permissions=None, on_event=None, **kwargs):
    permissions = permissions or PermissionManager(workspace_root=workspace)
    if permissions.approver is None:
        for name in registry.names():
            permissions.register_policy(name, AllowPolicy())
    return AgentLoop(
        provider=CaptureProvider(calls), registry=registry, permissions=permissions,
        transcript=TranscriptStore(workspace.parent / "transcripts"), workspace=workspace,
        on_event=on_event, **kwargs,
    )


def test_deferred_validation_preserves_real_failure_streak(workspace):
    from code_agent.agent.breaker import FailureBreaker, fingerprint

    loop = make_loop(workspace, ToolRegistry(), [], breaker=FailureBreaker(limit=3))
    call = ToolCall(name="bash", arguments={"command": "pytest -q"})
    key = fingerprint(call.name, call.arguments)
    loop._note_outcome(call, "[tool error] actual validation launch failed")
    before = dict(loop.breaker.streaks)
    totals = dict(loop.breaker.totals)
    loop._note_outcome(call, "[tool deferred] 验证未执行：先完成写入")
    assert loop.breaker.streaks == before == {key: 1}
    assert loop.breaker.totals == totals
    loop._note_outcome(call, "exit_code=0\n1 passed")
    assert key not in loop.breaker.streaks


def test_parallel_progress_arrives_before_slow_tool_and_model_history_stays_ordered(workspace):
    async def scenario():
        slow_started, release_slow, fast_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Probe(Tool):
            name = "probe"

            async def execute(self, args, ctx):
                if args["slow"]:
                    slow_started.set()
                    await release_slow.wait()
                    return "slow result"
                return "fast result"

        reg = ToolRegistry()
        reg.register(Probe())
        calls = [ToolCall(id="slow", name="probe", arguments={"slow": True}),
                 ToolCall(id="fast", name="probe", arguments={"slow": False})]

        def event(ev):
            if ev.type == EventType.tool_result and ev.payload["call_id"] == "fast":
                fast_seen.set()

        loop = make_loop(workspace, reg, calls, on_event=event)
        run = asyncio.create_task(loop.run("parallel task"))
        await asyncio.wait_for(slow_started.wait(), 1)
        await asyncio.wait_for(fast_seen.wait(), 1)
        assert not run.done()
        replay = loop.transcript.replay(loop.session_id)
        results = [ev.payload["call_id"] for ev in replay if ev.type == EventType.tool_result]
        assert results == ["fast"]
        assert len(loop.provider.requests) == 1, "No incomplete tool batch may reach the model"
        release_slow.set()
        await run
        results = [ev.payload["call_id"] for ev in loop.transcript.replay(loop.session_id) if ev.type == EventType.tool_result]
        assert results == ["fast", "slow"]
        model_results = [msg for msg in loop.provider.requests[-1] if msg["role"] == "tool"]
        assert [msg["tool_call_id"] for msg in model_results] == ["slow", "fast"]
        assert [msg["content"] for msg in model_results] == ["slow result", "fast result"]

    asyncio.run(scenario())


@pytest.mark.parametrize("parallel", [True, False])
def test_single_call_sessions_share_write_lease_even_with_parallel_disabled(workspace, monkeypatch, parallel):
    monkeypatch.setattr(loop_module, "TOOL_PARALLEL", parallel)

    async def scenario():
        first_started, release_first, second_before = asyncio.Event(), asyncio.Event(), asyncio.Event()
        executed = []

        class Writer(Tool):
            name = "writer"
            writes_path = True

            async def execute(self, args, ctx):
                executed.append(ctx.session_id)
                if len(executed) == 1:
                    first_started.set()
                    await release_first.wait()
                return "written"

        async def before_second():
            second_before.set()

        reg = ToolRegistry()
        reg.register(Writer())
        first = make_loop(workspace, reg, [ToolCall(name="writer", arguments={"path": "same.txt"})])
        second = make_loop(workspace, reg, [ToolCall(name="writer", arguments={"path": "./same.txt"})], before_tool_dispatch=before_second)
        task1 = asyncio.create_task(first.run("first"))
        await asyncio.wait_for(first_started.wait(), 1)
        task2 = asyncio.create_task(second.run("second"))
        await asyncio.wait_for(second_before.wait(), 1)
        assert executed == [first.session_id]
        assert reg._write_lease.size() == 1
        release_first.set()
        await asyncio.gather(task1, task2)
        assert executed == [first.session_id, second.session_id]
        assert reg._write_lease.size() == 0

    asyncio.run(scenario())


def test_pending_approval_holds_no_file_lease(workspace):
    async def scenario():
        approval_started, approve = asyncio.Event(), asyncio.Event()
        written = []

        class Writer(Tool):
            name = "writer"
            writes_path = True

            async def execute(self, args, ctx):
                written.append(ctx.session_id)
                return "written"

        async def approver(req):
            approval_started.set()
            await approve.wait()
            return True

        reg = ToolRegistry()
        reg.register(Writer())
        first = make_loop(workspace, reg, [ToolCall(id="approved-call", name="writer", arguments={"path": "same.txt", "_approval": {
            "purpose": "保存模拟写入结果", "effects": ["写入 same.txt"]}})],
                          permissions=PermissionManager(workspace_root=workspace, approver=approver))
        second = make_loop(workspace, reg, [ToolCall(name="writer", arguments={"path": "same.txt"})])
        task = asyncio.create_task(first.run("requires approval"))
        await asyncio.wait_for(approval_started.wait(), 1)
        assert reg._write_lease.size() == 0
        await asyncio.wait_for(second.run("authorized writer"), 1)
        assert written == [second.session_id]
        approve.set()
        await task
        assert written == [second.session_id, first.session_id]
        approval_events = [ev for ev in first.transcript.replay(first.session_id)
                           if ev.type in (EventType.approval_request, EventType.approval_decided)]
        assert all(ev.payload["call_id"] == "approved-call" for ev in approval_events)

    asyncio.run(scenario())


def test_fast_parallel_result_is_persisted_while_sibling_waits_for_approval(workspace):
    async def scenario():
        approval_started, approve, fast_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        executed = []

        class Probe(Tool):
            name = "probe"

            async def execute(self, args, ctx):
                executed.append(args["approval"])
                return "approved return" if args["approval"] else "fast return"

        class Policy:
            def evaluate(self, arguments, ctx):
                return PolicyResult(Decision.need_approval if arguments["approval"] else Decision.allow, "test policy")

        async def approver(req):
            approval_started.set()
            await approve.wait()
            return True

        def on_event(event):
            if event.type == EventType.tool_result and event.payload["call_id"] == "fast":
                fast_seen.set()

        reg = ToolRegistry()
        reg.register(Probe())
        pm = PermissionManager(workspace_root=workspace, approver=approver)
        pm.register_policy("probe", Policy())
        loop = make_loop(workspace, reg, [ToolCall(id="approval", name="probe", arguments={"approval": True, "_approval": {
            "purpose": "核查需要审批的模拟检查项", "effects": ["运行模拟 probe 检查"]}}),
                                         ToolCall(id="fast", name="probe", arguments={"approval": False})],
                         permissions=pm, on_event=on_event)
        task = asyncio.create_task(loop.run("one approval and one fast tool"))
        await asyncio.wait_for(approval_started.wait(), 1)
        await asyncio.wait_for(fast_seen.wait(), 1)
        assert executed == [False]
        assert not task.done()
        approve.set()
        await task
        assert executed == [False, True]
        assert [msg["tool_call_id"] for msg in loop.provider.requests[-1] if msg["role"] == "tool"] == ["approval", "fast"]

    asyncio.run(scenario())


def test_cancelling_a_single_waiting_writer_does_not_execute_or_release_another_owner(workspace):
    async def scenario():
        occupied, release, second_before = asyncio.Event(), asyncio.Event(), asyncio.Event()
        written, hooks = [], []

        class Writer(Tool):
            name = "writer"
            writes_path = True

            async def execute(self, args, ctx):
                written.append(ctx.session_id)
                occupied.set()
                await release.wait()
                return "written"

        async def before_second():
            hooks.append("before")
            second_before.set()

        async def after_second():
            hooks.append("after")

        reg = ToolRegistry()
        reg.register(Writer())
        first = make_loop(workspace, reg, [ToolCall(name="writer", arguments={"path": "same.txt"})])
        second = make_loop(workspace, reg, [ToolCall(name="writer", arguments={"path": "same.txt"})],
                           before_tool_dispatch=before_second, after_tool_dispatch=after_second)
        owner = asyncio.create_task(first.run("owner"))
        await asyncio.wait_for(occupied.wait(), 1)
        waiting = asyncio.create_task(second.run("waiting writer"))
        await asyncio.wait_for(second_before.wait(), 1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert written == [first.session_id]
        assert hooks == ["before", "after"]
        assert reg._write_lease.size() == 1
        release.set()
        await owner
        assert reg._write_lease.size() == 0
        assert written == [first.session_id]

    asyncio.run(scenario())


def test_permission_is_rechecked_after_waiting_for_a_write_lease(workspace):
    async def scenario():
        occupied, release, second_before = asyncio.Event(), asyncio.Event(), asyncio.Event()
        written = []
        mode = {"value": "workspace-write"}

        class Writer(Tool):
            name = "writer"
            writes_path = True

            async def execute(self, args, ctx):
                written.append(ctx.session_id)
                occupied.set()
                await release.wait()
                return "written"

        async def before_second():
            second_before.set()

        reg = ToolRegistry()
        reg.register(Writer())
        first = make_loop(workspace, reg, [ToolCall(name="writer", arguments={"path": "same.txt"})])
        second = make_loop(workspace, reg, [ToolCall(name="writer", arguments={"path": "same.txt"})],
                           permissions=PermissionManager(workspace_root=workspace, mode_provider=lambda: mode["value"]),
                           before_tool_dispatch=before_second)
        task1 = asyncio.create_task(first.run("first"))
        await asyncio.wait_for(occupied.wait(), 1)
        task2 = asyncio.create_task(second.run("second"))
        await asyncio.wait_for(second_before.wait(), 1)
        mode["value"] = "read-only"
        release.set()
        await asyncio.gather(task1, task2)
        assert written == [first.session_id]
        result = next(ev.payload["result"] for ev in second.transcript.replay(second.session_id) if ev.type == EventType.tool_result)
        assert result.startswith("[permission denied]")
        assert reg._write_lease.size() == 0

    asyncio.run(scenario())


def test_parallel_cancellation_drains_tools_and_pairs_runtime_hooks(workspace):
    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()
        hooks = []
        executed = []

        class Probe(Tool):
            name = "probe"
            writes_path = True

            async def execute(self, args, ctx):
                executed.append(args["path"])
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        async def before():
            hooks.append("before")

        async def after():
            hooks.append("after")

        reg = ToolRegistry()
        reg.register(Probe())
        loop = make_loop(workspace, reg, [ToolCall(name="probe", arguments={"path": "same.txt"}),
                                         ToolCall(name="probe", arguments={"path": "pending.txt"})],
                         before_tool_dispatch=before, after_tool_dispatch=after)
        task = asyncio.create_task(loop.run("cancel writers"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert executed == ["same.txt"]
        assert hooks.count("before") == hooks.count("after") == 1
        assert reg._write_lease.size() == 0
        assert not any(ev.type == EventType.tool_result for ev in loop.transcript.replay(loop.session_id))
        assert len(loop.provider.requests) == 1

    asyncio.run(scenario())


def test_self_cancelled_tool_does_not_leave_a_parallel_sibling_running(workspace):
    async def scenario():
        sibling_started, sibling_cancelled = asyncio.Event(), asyncio.Event()

        class Probe(Tool):
            name = "probe"

            async def execute(self, args, ctx):
                if args.get("cancel"):
                    await sibling_started.wait()
                    raise asyncio.CancelledError()
                sibling_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    sibling_cancelled.set()

        reg = ToolRegistry()
        reg.register(Probe())
        loop = make_loop(workspace, reg, [ToolCall(name="probe", arguments={"cancel": True}),
                                         ToolCall(name="probe", arguments={})])
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(loop.run("self cancellation"), 1)
        assert sibling_cancelled.is_set()

    asyncio.run(scenario())


def test_wait_tools_start_after_ordinary_results_and_skip_dispatch_hooks(workspace):
    async def scenario():
        observed = []

        class Ordinary(Tool):
            name = "ordinary"

            async def execute(self, args, ctx):
                observed.append("ordinary")
                return "ordinary returned"

        class Wait(Tool):
            name = "wait"
            suspends_session = True

            async def execute(self, args, ctx):
                assert observed == ["before", "ordinary", "after", "ordinary result"]
                observed.append("wait")
                return "child returned"

        async def before():
            observed.append("before")

        async def after():
            observed.append("after")

        def event(ev):
            if ev.type == EventType.tool_result:
                observed.append(ev.payload["name"] + " result")

        reg = ToolRegistry()
        reg.register(Ordinary())
        reg.register(Wait())
        loop = make_loop(workspace, reg, [ToolCall(name="wait", arguments={}), ToolCall(name="ordinary", arguments={})],
                         on_event=event, before_tool_dispatch=before, after_tool_dispatch=after)
        await loop.run("wait after local calls")
        assert observed[-2:] == ["wait", "wait result"]
        tools = [msg for msg in loop.provider.requests[-1] if msg["role"] == "tool"]
        assert [msg["content"] for msg in tools] == ["child returned", "ordinary returned"]

    asyncio.run(scenario())


def test_pending_inbox_notes_emit_only_server_generated_safe_ids(workspace):
    class InboxNote(str):
        def __new__(cls, value, inbox_ids):
            note = super().__new__(cls, value)
            note.inbox_ids = inbox_ids
            return note

    notes = [InboxNote("server generated note with quoted [team-inbox:unrelated]", ("am_one", "am_one", "am_two", "bad id", 5)),
             "plain string mentions [team-inbox:untrusted]", InboxNote("invalid id container", "am_invalid")]
    loop = make_loop(workspace, ToolRegistry(), [], on_pending_notes=lambda: notes)
    asyncio.run(loop.run("read pending messages"))
    events = [event for event in loop.transcript.replay(loop.session_id)
              if event.type == EventType.user_message and event.payload.get("auto")]
    assert events[0].payload["inbox_ids"] == ["am_one", "am_two"]
    assert "inbox_ids" not in events[1].payload
    assert "inbox_ids" not in events[2].payload


def test_model_turn_hook_runs_before_each_request_and_can_cancel_before_provider(workspace):
    async def scenario():
        checks = []

        class Probe(Tool):
            name = "probe"

            async def execute(self, args, ctx):
                return "result"

        async def before_model():
            checks.append(len(loop.provider.requests))

        reg = ToolRegistry()
        reg.register(Probe())
        loop = make_loop(workspace, reg, [ToolCall(name="probe")], before_model_turn=before_model)
        await loop.run("budget check per turn")
        assert checks == [0, 1]

        async def cancel():
            raise asyncio.CancelledError

        blocked = make_loop(workspace, reg, [], before_model_turn=cancel)
        with pytest.raises(asyncio.CancelledError):
            await blocked.run("no model request")
        assert blocked.provider.requests == []

    asyncio.run(scenario())


def test_usage_meter_charges_each_response_once_before_tools_and_includes_final_turn(workspace):
    metered = []

    class Probe(Tool):
        name = "probe"

        async def execute(self, args, ctx):
            assert metered == [{"input_tokens": 3, "output_tokens": 4, "cached_tokens": 1}]
            return "result"

    reg = ToolRegistry()
    reg.register(Probe())
    loop = make_loop(workspace, reg, [], on_model_usage=lambda usage: metered.append(usage.model_dump()))
    loop.provider = OfflineProvider(script=[
        ModelTurn(tool_calls=[ToolCall(name="probe")], usage=TokenUsage(input_tokens=3, output_tokens=4, cached_tokens=1)),
        ModelTurn(text="done", usage=TokenUsage(input_tokens=8, output_tokens=2, cached_tokens=2)),
    ])
    result = asyncio.run(loop.run("meter usage"))
    assert metered == [{"input_tokens": 3, "output_tokens": 4, "cached_tokens": 1},
                       {"input_tokens": 8, "output_tokens": 2, "cached_tokens": 2}]
    assert result.usage.model_dump() == {"input_tokens": 11, "output_tokens": 6, "cached_tokens": 3}
    done = next(event for event in loop.transcript.replay(loop.session_id) if event.type == EventType.done)
    assert done.payload["usage"] == result.usage.model_dump()
    assert len(metered) == 2, "The aggregated done payload must never be charged again"


def test_usage_meter_failure_stops_before_dispatch(workspace):
    executed = []

    class Probe(Tool):
        name = "probe"

        async def execute(self, args, ctx):
            executed.append(True)
            return "result"

    def fail(usage):
        raise RuntimeError("local budget ledger unavailable")

    reg = ToolRegistry()
    reg.register(Probe())
    loop = make_loop(workspace, reg, [ToolCall(name="probe")], on_model_usage=fail)
    with pytest.raises(RuntimeError, match="budget ledger unavailable"):
        asyncio.run(loop.run("fail closed"))
    assert executed == []
    assert len(loop.provider.requests) == 1
