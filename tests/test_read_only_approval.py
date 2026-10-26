"""Interactive read-only grants authorize one available tool invocation only."""

import asyncio

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import Decision
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.builtin import WriteFileTool
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry


class RecordingTool(Tool):
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def execute(self, arguments, ctx):
        self.calls.append(arguments)
        return "executed"


def _loop(workspace, permissions, registry, calls):
    return AgentLoop(
        provider=OfflineProvider(script=[
            *(OfflineProvider.tool_call_turn(name, args) for name, args in calls),
            OfflineProvider.text_turn("finished"),
        ]),
        registry=registry,
        permissions=permissions,
        transcript=TranscriptStore(workspace.parent / "transcripts"),
        workspace=workspace,
    )


def _events(loop, kind):
    return [event for event in loop.transcript.replay(loop.session_id) if event.type.value == kind]


def test_approval_grants_one_write_and_rejection_leaves_previous_content(workspace):
    requests = []

    async def approve_once(request):
        requests.append(request)
        assert "仅允许本次操作" in request.reason
        assert "会话保持只读" in request.reason
        return len(requests) == 1

    permissions = PermissionManager(
        workspace_root=workspace, mode="read-only", approver=approve_once, read_only_approval=True,
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    loop = _loop(workspace, permissions, registry, [
        ("write_file", {"path": "result.txt", "content": "approved", "_approval": {
            "purpose": "保存本次模拟结果", "effects": ["写入 result.txt"]}}),
        ("write_file", {"path": "result.txt", "content": "must not replace", "_approval": {
            "purpose": "保存第二次模拟结果", "effects": ["覆盖 result.txt"]}}),
    ])
    assert asyncio.run(loop.run("write twice")).text == "finished"
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "approved"
    assert len(requests) == 2
    assert [event.payload["approved"] for event in _events(loop, "approval_decided")] == [True, False]
    assert "[permission denied]" in _events(loop, "tool_result")[-1].payload["result"]
    assert permissions.mode == "read-only"
    next_decision = asyncio.run(permissions.evaluate(
        "write_file", {"path": "later.txt"}, ToolContext("next", workspace),
    ))
    assert next_decision.decision == Decision.need_approval


@pytest.mark.parametrize("interactive,has_approver", [(False, True), (True, False)])
def test_strict_default_or_missing_channel_never_emits_read_only_approval(workspace, interactive, has_approver):
    requests = []

    async def approve(request):
        requests.append(request)
        return True

    permissions = PermissionManager(
        workspace_root=workspace, mode="read-only", read_only_approval=interactive,
        approver=approve if has_approver else None,
    )
    tool = RecordingTool("bash")
    registry = ToolRegistry()
    registry.register(tool)
    loop = _loop(workspace, permissions, registry, [("bash", {"command": "echo harmless"})])
    asyncio.run(loop.run("execute"))
    assert requests == [] and tool.calls == []
    assert not _events(loop, "approval_request")
    assert "[permission denied]" in _events(loop, "tool_result")[0].payload["result"]


@pytest.mark.parametrize("name,args,reason", [
    ("bash", {"command": "sudo whoami"}, "黑名单"),
    ("write_file", {"path": "../outside.txt", "content": "x"}, "硬边界"),
    ("write_file", {"path": "design.CONTRACT.md", "content": "x"}, "契约文件"),
])
def test_hard_policy_denial_cannot_become_an_approval(workspace, name, args, reason):
    requests = []

    async def approve(request):
        requests.append(request)
        return True

    permissions = PermissionManager(
        workspace_root=workspace, mode="read-only", approver=approve,
        read_only_approval=True, contract_guard=True,
    )
    tool = RecordingTool(name)
    registry = ToolRegistry()
    registry.register(tool)
    loop = _loop(workspace, permissions, registry, [(name, args)])
    asyncio.run(loop.run("hard boundary"))
    assert requests == [] and tool.calls == []
    assert not _events(loop, "approval_request")
    result = _events(loop, "tool_result")[0].payload["result"]
    assert result.startswith("[permission denied]") and reason in result


@pytest.mark.parametrize("mode", ["read-only", "full-access"])
@pytest.mark.parametrize("unavailable", ["filtered", "unknown"])
@pytest.mark.parametrize("delegation_enabled", [False, True])
def test_unavailable_tool_is_rejected_before_approval(workspace, mode, unavailable, delegation_enabled):
    requests = []

    async def approve(request):
        requests.append(request)
        return True

    permissions = PermissionManager(
        workspace_root=workspace, mode=mode, approver=approve, read_only_approval=True,
    )
    tool = RecordingTool("bash")
    registry = ToolRegistry()
    registry.register(tool)
    if delegation_enabled:
        registry.register(RecordingTool("delegate_agent"))
    if unavailable == "filtered":
        registry = FilteredToolRegistry(registry, exclude=("bash",))
    name = "bash" if unavailable == "filtered" else "unknown_tool"
    loop = _loop(workspace, permissions, registry, [(name, {"command": "echo harmless"})])
    asyncio.run(loop.run("not available"))
    assert requests == [] and tool.calls == []
    assert not _events(loop, "approval_request")
    result = _events(loop, "tool_result")[0].payload["result"]
    assert "not available" in result
    assert ("委派" in result) is delegation_enabled
    assert "申请权限不能使它可用" in result


def test_switch_to_read_only_changes_the_next_tool_in_the_same_loop(workspace):
    mode = ["workspace-write"]
    requests = []

    class FirstWriteSwitchesMode(WriteFileTool):
        async def execute(self, arguments, ctx):
            result = await super().execute(arguments, ctx)
            mode[0] = "read-only"
            return result

    async def reject(request):
        requests.append(request)
        return False

    permissions = PermissionManager(
        workspace_root=workspace, mode="workspace-write", mode_provider=lambda: mode[0],
        approver=reject, read_only_approval=True,
    )
    registry = ToolRegistry()
    registry.register(FirstWriteSwitchesMode())
    loop = _loop(workspace, permissions, registry, [
        ("write_file", {"path": "first.txt", "content": "allowed before switch"}),
        ("write_file", {"path": "second.txt", "content": "requires approval after switch", "_approval": {
            "purpose": "保存切换权限后的模拟结果", "effects": ["写入 second.txt"]}}),
    ])
    asyncio.run(loop.run("switch while running"))
    assert (workspace / "first.txt").exists()
    assert not (workspace / "second.txt").exists()
    assert len(requests) == 1 and "second.txt" in requests[0].command
    assert len(_events(loop, "approval_request")) == 1


@pytest.mark.parametrize("callback", [lambda: "invalid", lambda: 1 / 0])
def test_live_permission_failure_does_not_reuse_stale_full_access(workspace, callback):
    permissions = PermissionManager(
        workspace_root=workspace, mode="full-access", mode_provider=callback, read_only_approval=True,
    )
    result = asyncio.run(permissions.evaluate("bash", {"command": "echo harmless"}, ToolContext("s", workspace)))
    assert result.decision == Decision.deny


def test_interactive_option_keeps_reads_and_full_access_unchanged(workspace):
    async def approve(_request):
        raise AssertionError("read/full-access must not request approval")

    permissions = PermissionManager(
        workspace_root=workspace, mode="read-only", approver=approve, read_only_approval=True,
    )
    ctx = ToolContext("s", workspace)
    assert asyncio.run(permissions.evaluate("read_file", {"path": "a.txt"}, ctx)).decision == Decision.allow
    permissions.mode = "full-access"
    assert asyncio.run(permissions.evaluate("bash", {"command": "sudo whoami"}, ctx)).decision == Decision.allow
