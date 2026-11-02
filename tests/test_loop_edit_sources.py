"""Native edit receipts and stale-read safety through the official core.

Legacy source-slot/restore-credit assertions were retired with the Python loop.
These probes use real disk tools and the normal permission/observation boundary.
"""
import asyncio

from code_agent.agent.loop import AgentLoop
from code_agent.models.events import EventType, ModelTurn
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy, Decision, PolicyResult
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import ReadFileTool
from code_agent.tools.builtin.code_tools import EditFileTool, EditManyTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


def make_loop(workspace, turns, **kwargs):
    registry=ToolRegistry()
    for tool in (ReadFileTool(), EditFileTool(), EditManyTool()): registry.register(tool)
    permission=PermissionManager(workspace_root=workspace)
    for name in registry.names(): permission.register_policy(name, AllowPolicy())
    loop=AgentLoop(OfflineProvider(script=turns), registry, permission,
                   TranscriptStore(workspace.parent / "native-source-transcripts"), workspace=workspace,
                   fs_observation=FsObservation(), **kwargs)
    return loop


def test_real_local_edit_returns_only_actual_receipt_and_model_keeps_evidence(workspace):
    path=workspace / "calculator.py"
    path.write_text("def total():\n    return 1\n", encoding="utf8")
    loop=make_loop(workspace, [
        OfflineProvider.tool_call_turn("read_file", {"path":"calculator.py"}),
        OfflineProvider.tool_call_turn("edit_file", {"path":"calculator.py", "old_string":"return 1", "new_string":"return 2"}),
        OfflineProvider.text_turn("已完成局部修改。"),
    ])
    result=asyncio.run(loop.run("修改 calculator.py 的返回值"))
    assert result.stop_reason == "end_turn" and result.turns == 3
    assert path.read_text(encoding="utf8") == "def total():\n    return 2\n"
    receipts=[e.payload for e in loop.transcript.replay(loop.session_id) if e.type == EventType.tool_result]
    assert len(receipts) == 2 and receipts[1]["result"].startswith("已修改")
    assert "完整当前源码" not in receipts[1]["result"]


def test_external_change_after_read_is_not_overwritten_by_native_edit(workspace):
    path=workspace / "calculator.py"
    path.write_text("result = 1\n", encoding="utf8")
    class Interleaved(OfflineProvider):
        async def chat(self, messages, tools):
            if self.calls == 1: path.write_text("result = 9\n",encoding="utf8")
            return await super().chat(messages,tools)
    loop=make_loop(workspace, [])
    loop.provider=Interleaved(script=[
        OfflineProvider.tool_call_turn("read_file",{"path":"calculator.py"}),
        OfflineProvider.tool_call_turn("edit_file",{"path":"calculator.py","old_string":"result = 1","new_string":"result = 2"}),
        OfflineProvider.text_turn("文件已在外部变化，本次未覆盖。"),
    ])
    result=asyncio.run(loop.run("局部修改 calculator.py"))
    assert result.stop_reason == "end_turn" and path.read_text(encoding="utf8") == "result = 9\n"
    receipts=[e.payload["result"] for e in loop.transcript.replay(loop.session_id) if e.type == EventType.tool_result]
    assert receipts[-1].startswith(("[tool error]", "[permission denied]"))


def test_permission_denial_is_not_replaced_with_automatic_source_preview(workspace):
    path=workspace / "calculator.py"
    path.write_text("PRIVATE_CONTENT = 1\n",encoding="utf8")
    loop=make_loop(workspace,[OfflineProvider.tool_call_turn("read_file",{"path":"calculator.py"}),
                              OfflineProvider.text_turn("读取被拒绝。")])
    class DenyRead:
        def evaluate(self,args,ctx): return PolicyResult(Decision.deny,"read disabled")
    loop.permissions.register_policy("read_file",DenyRead())
    result=asyncio.run(loop.run("解释 calculator.py"))
    assert result.stop_reason == "end_turn"
    receipts=[e.payload["result"] for e in loop.transcript.replay(loop.session_id) if e.type == EventType.tool_result]
    assert len(receipts) == 1 and receipts[0].startswith("[permission denied]")
    assert "PRIVATE_CONTENT" not in receipts[0]
