"""Offline integration regressions for evidence-based AgentLoop convergence.

Tools still pass through the real registry, permission checks, output budgeting
and transcript. Shell responses are deterministic fixture reads: these probes
must neither contact a model service nor launch hundreds of shell processes.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.models.events import EventType, ModelTurn, TokenUsage, ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy, Decision, PolicyResult
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool
from code_agent.tools.builtin import ReadFileTool, WriteFileTool
from code_agent.tools.builtin.code_tools import EditFileTool
from code_agent.tools.registry import ToolRegistry


SOURCE = "AdaptiveToolWorkflow.java"
USAGE = TokenUsage(input_tokens=11, output_tokens=7, cached_tokens=3)
ANSWER = (
    "AdaptiveToolWorkflow.java:2-4 将参数解析后交给 execute。"
    "这是依据已读取源码作出的静态解释；未运行项目测试，无法确认运行时行为。"
)


def call(name, **arguments):
    return ModelTurn(tool_calls=[ToolCall(name=name, arguments=arguments)],
                     stop_reason="tool_use", usage=USAGE)


def read(offset=1, limit=60, path=SOURCE):
    return call("read_file", path=path, offset=offset, limit=limit)


class ScriptProvider(OfflineProvider):
    """A cooperative provider answers exactly once when asked to stop reading."""

    def __init__(self, turns, *, stubborn=False, final=ANSWER, before_request=None):
        super().__init__(script=turns)
        self.requests = []
        self.stubborn = stubborn
        self.final = final
        self.before_request = before_request

    async def chat(self, messages, tools):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        if self.before_request is not None:
            self.before_request(len(self.requests))
        if not tools:
            self.calls += 1
            if self.stubborn:
                return read()
            return ModelTurn(text=self.final, stop_reason="end_turn", usage=USAGE)
        if not self._script:
            self.calls += 1
            return ModelTurn(text=self.final, stop_reason="end_turn", usage=USAGE)
        return await super().chat(messages, tools)


class CapturedLoop(AgentLoop):
    async def _next_turn(self, messages, ctx, *args, **kwargs):
        self.internal_requests.append(copy.deepcopy(messages))
        return await super()._next_turn(messages, ctx, *args, **kwargs)


class FixtureBash(Tool):
    """Faithful shell-output shape, with fixture-only read and validation code."""

    name = "bash"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    def __init__(self):
        self.commands = []

    async def execute(self, args, ctx):
        command = args["command"]
        self.commands.append(command)
        path = ctx.workspace / SOURCE
        if command in (f"cat {SOURCE}", f"Get-Content {SOURCE}"):
            return "exit_code=0\n" + path.read_text(encoding="utf-8")
        cat_window = re.fullmatch(r"cat -n " + re.escape(SOURCE) + r" \| sed -n '(\d+),(\d+)p'", command)
        awk_window = re.fullmatch(
            r'''awk 'NR>=(\d+) && NR<=(\d+) \{printf "%d\\t%s\\n", NR, \$0\}' ''' + re.escape(SOURCE),
            command,
        )
        if window := cat_window or awk_window:
            first, last = map(int, window.groups())
            # Reproduce cat -n / printf's real visible numbering for only the
            # fixture lines selected. No shell process or requested-but-unseen
            # line range is used as a substitute for the actual returned bytes.
            lines = path.read_text(encoding="utf-8").splitlines()
            selected = [(number, line) for number, line in enumerate(lines, 1) if first <= number <= last]
            body = "\n".join((f"{number:6}\t{line}" if cat_window else f"{number}\t{line}")
                             for number, line in selected)
            return "exit_code=0\n" + body + ("\n" if body else "")
        if command.startswith("pytest "):
            # This is a deterministic validation of the file just written,
            # not an invented successful test based on its command name.
            value = re.search(r"version = (\d+)", path.read_text(encoding="utf-8"))
            expected = int(command.split("version-")[1])
            actual = int(value.group(1)) if value else None
            if actual != expected:
                return f"exit_code=1\nversion check failed: actual={actual}, expected={expected}"
            return f"exit_code=0\n1 passed: version {actual} matches the requested fixture contract"
        raise AssertionError(f"Unexpected test shell command: {command}")


class FixtureGrep(Tool):
    name = "grep"
    parameters = {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}}

    async def execute(self, args, ctx):
        text = (ctx.workspace / args["path"]).read_text(encoding="utf-8")
        return "\n".join(f"{args['path']}:{index}:{line}" for index, line in enumerate(text.splitlines(), 1)
                         if args["pattern"] in line)


def make_loop(workspace, provider, *, extra_tools=(), permissions=None, **kwargs):
    registry = ToolRegistry()
    bash = FixtureBash()
    for tool in (ReadFileTool(), WriteFileTool(), bash, FixtureGrep(), *extra_tools):
        registry.register(tool)
    permissions = permissions or PermissionManager(workspace_root=workspace)
    for name in registry.names():
        if name != "read_file":
            permissions.register_policy(name, AllowPolicy())
    result = CapturedLoop(
        provider=provider, registry=registry, permissions=permissions,
        transcript=TranscriptStore(workspace.parent / "convergence-transcripts"),
        workspace=workspace, **kwargs,
    )
    result.internal_requests = []
    result.fixture_bash = bash
    return result


@pytest.fixture
def source(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / SOURCE).write_text(
        "class AdaptiveToolWorkflow {\n"
        "  int version = 0;\n"
        "  Object resolve(Object input) { return execute(input); }\n"
        "  Object execute(Object input) { return input; }\n"
        + "".join(f"  // section {index}: preserves input {index}\n" for index in range(5, 61))
        + "}\n", encoding="utf-8",
    )
    return workspace


def tool_events(loop):
    return [event for event in loop.transcript.replay(loop.session_id) if event.type == EventType.tool_use]




def test_failed_reads_are_reported_as_failure_without_inventing_success_evidence(source):
    missing = "not-present.java"
    final = "文件不存在，无法据此解释逻辑；未读取到源码，也未运行测试。"
    provider = ScriptProvider([read(path=missing)], final=final)
    loop = make_loop(source, provider)
    result = asyncio.run(loop.run("解释不存在的源码文件"))

    assert result.stop_reason == "end_turn" and result.text == final
    assert provider.calls == 2
    results = [event.payload["result"] for event in loop.transcript.replay(loop.session_id)
               if event.type == EventType.tool_result]
    assert results and all(result.startswith("[tool error]") for result in results)



def test_permission_refusal_is_not_replayed_or_treated_as_a_successful_read(source):
    class DenyRead:
        def evaluate(self, arguments, ctx):
            return PolicyResult(Decision.deny, "fixture private file is not authorized")

    permissions = PermissionManager(workspace_root=source)
    permissions.register_policy("read_file", DenyRead())
    provider = ScriptProvider([read()], final="读取被权限拒绝，无法核查源码；未执行验证。")
    executed = []
    loop = make_loop(source, provider, permissions=permissions,
                     on_tool_executed=lambda *args: executed.append(args))
    result = asyncio.run(loop.run("解释被拒绝读取的文件"))

    assert result.stop_reason == "end_turn" and provider.calls == 2
    assert not [entry for entry in executed if entry[-1] == "ok"]
    results = [event.payload["result"] for event in loop.transcript.replay(loop.session_id)
               if event.type == EventType.tool_result]
    assert results and all(result.startswith("[permission denied]") for result in results)



def test_waiting_for_approval_does_not_trigger_a_model_request_or_skip_permission(source):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class ApprovedWrite(WriteFileTool):
            name = "approved_write"

        class NeedsApproval:
            def evaluate(self, arguments, ctx):
                return PolicyResult(Decision.need_approval, "fixture approval")

        async def approve(request):
            started.set()
            await release.wait()
            return True

        permissions = PermissionManager(workspace_root=source, approver=approve)
        approved_content = (source / SOURCE).read_text(encoding="utf-8").replace("int version = 0;", "int version = 1;")
        provider = ScriptProvider([read()] * 6 + [call("approved_write", path=SOURCE, content=approved_content,
                                                      _approval={"purpose": "保存离线测试修改", "effects": ["写入 fixture 文件"]}),
                                                   read(), call("bash", command="pytest fixture-version-1")])
        loop = make_loop(source, provider, extra_tools=[ApprovedWrite()], permissions=permissions)
        permissions.register_policy("approved_write", NeedsApproval())
        task = asyncio.create_task(loop.run("等待用户授权后保存修改并读取"))
        await asyncio.wait_for(started.wait(), 2)
        assert not task.done() and provider.calls == 7
        assert (source / SOURCE).read_text(encoding="utf-8").startswith("class AdaptiveToolWorkflow")
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.stop_reason == "end_turn" and provider.calls == 10
        assert all(request["tools"] for request in provider.requests)
        assert (source / SOURCE).read_text(encoding="utf-8") == approved_content
        assert loop.fixture_bash.commands == ["pytest fixture-version-1"]
        approvals = [event for event in loop.transcript.replay(loop.session_id)
                     if event.type in (EventType.approval_request, EventType.approval_decided)]
        assert [event.type for event in approvals] == [EventType.approval_request, EventType.approval_decided]

    asyncio.run(scenario())



def test_cancel_during_approval_never_executes_the_pending_change(source):
    async def scenario():
        started = asyncio.Event()

        class ApprovedWrite(WriteFileTool):
            name = "approved_write"

        class NeedsApproval:
            def evaluate(self, arguments, ctx):
                return PolicyResult(Decision.need_approval, "fixture approval")

        async def approve(request):
            started.set()
            await asyncio.Event().wait()

        permissions = PermissionManager(workspace_root=source, approver=approve)
        provider = ScriptProvider([read()] * 6 + [call("approved_write", path=SOURCE, content="must not execute",
                                                      _approval={"purpose": "保存离线测试修改", "effects": ["写入 fixture 文件"]})])
        loop = make_loop(source, provider, extra_tools=[ApprovedWrite()], permissions=permissions)
        permissions.register_policy("approved_write", NeedsApproval())
        original = (source / SOURCE).read_text(encoding="utf-8")
        task = asyncio.create_task(loop.run("等待批准的修改"))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (source / SOURCE).read_text(encoding="utf-8") == original
        assert provider.calls == 7
        assert not [event for event in loop.transcript.replay(loop.session_id) if event.type == EventType.done]

    asyncio.run(scenario())



def test_external_budget_hook_runs_before_every_native_model_request(source):
    provider = ScriptProvider([read()] * 8)
    checks = []

    async def budget():
        checks.append(len(provider.requests))
        if len(provider.requests) == 7:
            raise RuntimeError("fixture external execution budget exhausted")

    loop = make_loop(source, provider, before_model_turn=budget)
    with pytest.raises(RuntimeError, match="external execution budget exhausted"):
        asyncio.run(loop.run("解释源码"))
    assert provider.calls == 7 and len(checks) == 8
    assert all(request["tools"] for request in provider.requests)
    assert len(tool_events(loop)) == 7
    assert not [event for event in loop.transcript.replay(loop.session_id) if event.type == EventType.done]



def test_provider_truncation_keeps_true_stop_reason_and_usage(source):
    provider = ScriptProvider([read(), ModelTurn(text="源码目前只能确认", stop_reason="max_tokens", usage=USAGE)])
    loop = make_loop(source, provider)
    result = asyncio.run(loop.run("解释源码"))
    assert result.stop_reason == "max_tokens" and result.text == "源码目前只能确认"
    assert provider.calls == 2 and result.usage.input_tokens == 22
    done = [event for event in loop.transcript.replay(loop.session_id) if event.type == EventType.done]
    assert len(done) == 1 and done[0].payload["stop_reason"] == "max_tokens"


def test_usage_meter_failure_on_final_response_is_not_reported_as_done(source):
    provider = ScriptProvider([read()])
    billed = []
    def meter(usage):
        billed.append(usage)
        if provider.calls == 2:
            raise RuntimeError("fixture accounting service rejected the response")
    loop = make_loop(source, provider, on_model_usage=meter)
    with pytest.raises(RuntimeError, match="accounting service rejected"):
        asyncio.run(loop.run("解释源码"))
    assert provider.calls == 2 and len(billed) == 2
    assert len(tool_events(loop)) == 1
    assert not [event for event in loop.transcript.replay(loop.session_id) if event.type == EventType.done]
