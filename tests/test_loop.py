"""Agent loop 集成测试：离线脚本化 provider 跑通完整链路。"""

import asyncio

from code_agent.agent.loop import AgentLoop
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import BashTool, ReadFileTool, WriteFileTool
from code_agent.tools.registry import ToolRegistry


def _make_loop(provider, workspace, approver=None, on_event=None, max_turns=None):
    reg = ToolRegistry()
    reg.register(BashTool())
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    pm = PermissionManager(workspace_root=workspace, approver=approver)
    store = TranscriptStore(workspace.parent / "transcripts")
    kwargs = {} if max_turns is None else {"max_turns": max_turns}
    return AgentLoop(
        provider=provider,
        registry=reg,
        permissions=pm,
        transcript=store,
        workspace=workspace,
        on_event=on_event,
        **kwargs,
    )


def test_full_loop_with_tool_call(workspace):
    """提问 -> tool_use -> tool_result -> 最终回答，事件全部落轨迹。"""
    provider = OfflineProvider(
        script=[
            OfflineProvider.tool_call_turn("bash", {"command": "echo smoke-test"}),
            OfflineProvider.text_turn("任务完成"),
        ]
    )
    loop = _make_loop(provider, workspace)
    result = asyncio.run(loop.run("跑个冒烟"))
    assert result.text == "任务完成"
    assert result.stop_reason == "end_turn"
    assert result.turns == 2

    events = loop.transcript.replay(loop.session_id)
    types = [e.type for e in events]
    assert types[0].value == "user_message"
    assert "tool_use" in types
    assert "tool_result" in types
    assert types[-1].value == "done"


def test_denied_tool_does_not_execute(workspace):
    """黑名单命令：deny -> 不执行，结果写回 denied，循环仍能收口。"""
    executed = []

    class SpyBash(BashTool):
        async def execute(self, arguments, ctx):
            executed.append(arguments)
            return await super().execute(arguments, ctx)

    provider = OfflineProvider(
        script=[
            OfflineProvider.tool_call_turn("bash", {"command": "sudo rm -rf /"}),
            OfflineProvider.text_turn("被拒绝了，我换个方式"),
        ]
    )
    reg = ToolRegistry()
    reg.register(SpyBash())
    pm = PermissionManager(workspace_root=workspace)
    store = TranscriptStore(workspace.parent / "transcripts")
    loop = AgentLoop(provider=provider, registry=reg, permissions=pm, transcript=store, workspace=workspace)
    result = asyncio.run(loop.run("危险操作"))
    assert result.text == "被拒绝了，我换个方式"
    assert executed == []  # 危险命令从未真正执行
    tool_results = [e for e in loop.transcript.replay(loop.session_id) if e.type.value == "tool_result"]
    assert "denied" in tool_results[0].payload["result"]


def test_approval_flow_allow_and_deny(workspace):
    """need_approval -> 审批通过则执行 / 拒绝则返回 denied。"""
    provider = OfflineProvider(
        script=[
            OfflineProvider.tool_call_turn("bash", {"command": "make all", "_approval": {
                "purpose": "验证当前项目构建是否成功", "effects": ["运行 make all，可能生成构建产物"]}}),
            OfflineProvider.text_turn("done"),
        ]
    )

    async def approve(req):
        return True

    loop = _make_loop(provider, workspace, approver=approve)
    result = asyncio.run(loop.run("跑任务"))
    assert result.text == "done"
    events = loop.transcript.replay(loop.session_id)
    kinds = [e.type.value for e in events]
    assert "approval_request" in kinds
    assert "approval_decided" in kinds

    provider2 = OfflineProvider(
        script=[
            OfflineProvider.tool_call_turn("bash", {"command": "make all", "_approval": {
                "purpose": "验证当前项目构建是否成功", "effects": ["运行 make all，可能生成构建产物"]}}),
            OfflineProvider.text_turn("reject path"),
        ]
    )

    async def reject(req):
        return False

    loop2 = _make_loop(provider2, workspace, approver=reject)
    result2 = asyncio.run(loop2.run("跑任务"))
    assert result2.text == "reject path"
    tr = [e for e in loop2.transcript.replay(loop2.session_id) if e.type.value == "tool_result"]
    assert "denied" in tr[0].payload["result"]


def test_tool_error_does_not_crash_loop(workspace):
    """工具抛异常 -> 错误文本回传模型，循环继续。"""
    provider = OfflineProvider(
        script=[
            OfflineProvider.tool_call_turn("read_file", {"path": "not-exist.txt"}),
            OfflineProvider.text_turn("文件确实不存在"),
        ]
    )
    loop = _make_loop(provider, workspace)
    result = asyncio.run(loop.run("读个不存在的文件"))
    assert result.text == "文件确实不存在"


def test_done_event_carries_usage(workspace):
    """done 事件 payload 带 usage：前端据此累计会话级 Token 用量。"""
    provider = OfflineProvider(script=[OfflineProvider.text_turn("hi")])
    loop = _make_loop(provider, workspace)
    asyncio.run(loop.run("x"))
    done = [e for e in loop.transcript.replay(loop.session_id) if e.type.value == "done"][0]
    u = done.payload.get("usage")
    assert u is not None
    assert u.get("input_tokens", 0) >= 0
    assert u.get("output_tokens", 0) >= 0


def test_max_turns_guard(workspace):
    """provider 一直要求调工具 -> max_turns 强制收口。

    段长显式给小（3 轮）而不是用生产默认（60）：这条用例验证的是"到点收口"这个行为，
    用 bash 跑 60 轮会真起 60 次子进程（实测每轮 ~2s，整条用例 2 分钟）。
    """
    provider = OfflineProvider(script=[])

    # 空脚本时 _respond 会走规则路径：有 tool 消息后返回文本，不会死循环；
    # 构造一个永远 tool_use 的脚本 provider
    class LoopProvider(OfflineProvider):
        async def chat(self, messages, tools):
            return OfflineProvider.tool_call_turn("bash", {"command": "echo loop"})

    loop = _make_loop(LoopProvider(), workspace, max_turns=3)
    result = asyncio.run(loop.run("无限循环测试"))
    assert result.stop_reason == "max_turns"
    assert result.turns == 3
    # 文案已改：这不再是"强制结束"，而是"到检查点、交回调度"（是否续跑由 SessionRuntime 定）
    assert "检查点" in result.text
    # 而且它是 quiet 事件：界面不该把它当红色故障气泡铺开
    boundary = [e for e in loop.transcript.replay(loop.session_id) if e.payload.get("code") == "segment_boundary"]
    assert boundary and boundary[0].payload.get("quiet") is True
