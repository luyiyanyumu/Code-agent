"""写租约必须接在真实 AgentLoop 工具执行入口，并在会话之间共享。

同一模型批次的写入和 shell 是显式顺序屏障，不能用这类批次内串行证明租约
接线正确。这里用共享注册表的独立会话验证：同路径互斥、不同路径可同时执行；
另外保持同批次不同路径写入按模型顺序完成的屏障契约。
"""

from __future__ import annotations

import asyncio
import time

from code_agent.agent.loop import AgentLoop
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy
from code_agent.providers.base import ModelTurn
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry
from code_agent.models.events import ToolCall


def _overlap(intervals: list[tuple[float, float]]) -> float:
    """两段区间在时间轴上的重叠量（不重叠/紧邻 → <= 0）。"""
    assert len(intervals) == 2, intervals
    (a_enter, a_exit), (b_enter, b_exit) = intervals
    return min(a_exit, b_exit) - max(a_enter, b_enter)


def _run_two_calls(agent_home, arguments_pair, *, dwell: float = 0.05):
    """跑一个"一次发两个工具调用"的 turn，返回两次执行的 (enter, exit) 区间。"""
    intervals: list[tuple[float, float]] = []
    order = []

    class ProbeTool(Tool):
        name = "probe"
        description = "test-only writer"
        writes_path = True
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

        async def execute(self, arguments, ctx: ToolContext) -> str:
            enter = time.perf_counter()
            order.append((arguments["path"], "start"))
            await asyncio.sleep(dwell)          # 留出足够的窗口让并发显形
            intervals.append((enter, time.perf_counter()))
            order.append((arguments["path"], "end"))
            return "ok"

    workspace = agent_home / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    reg = ToolRegistry()
    reg.register(ProbeTool())
    pm = PermissionManager(workspace_root=workspace)
    pm.register_policy("probe", AllowPolicy())   # 否则会走到"需要审批"而根本不执行

    provider = OfflineProvider(
        script=[
            ModelTurn(tool_calls=[
                ToolCall(name="probe", arguments=arguments_pair[0]),
                ToolCall(name="probe", arguments=arguments_pair[1]),
            ]),
            OfflineProvider.text_turn("done"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=reg,
        permissions=pm,
        transcript=TranscriptStore(agent_home / "transcripts"),
        workspace=workspace,
    )
    asyncio.run(loop.run("两个写调用"))
    return intervals, order


def _run_independent_calls(agent_home, arguments_pair, *, rendezvous=False):
    """两个真实会话共享锁表；工具仅在内存中记录进入/退出。"""
    intervals = []
    workspace = agent_home / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()
    transcript = TranscriptStore(agent_home / "transcripts")

    async def scenario():
        model_gate, tool_gate = asyncio.Event(), asyncio.Event()
        model_arrivals = tool_arrivals = 0

        class ReadyProvider(OfflineProvider):
            async def chat(self, messages, tools):
                nonlocal model_arrivals
                if not self.calls:
                    model_arrivals += 1
                    if model_arrivals == 2:
                        model_gate.set()
                    await model_gate.wait()
                return await super().chat(messages, tools)

        class ProbeTool(Tool):
            name = "probe"
            writes_path = True

            async def execute(self, arguments, ctx):
                nonlocal tool_arrivals
                enter = time.perf_counter()
                tool_arrivals += 1
                if rendezvous:
                    # Different paths must enter together. A mistakenly global
                    # lock would time out instead of passing a timing heuristic.
                    if tool_arrivals == 2:
                        tool_gate.set()
                    await tool_gate.wait()
                await asyncio.sleep(0.05)
                intervals.append((enter, time.perf_counter()))
                return "ok"

        registry.register(ProbeTool())
        loops = []
        for index, arguments in enumerate(arguments_pair):
            permissions = PermissionManager(workspace_root=workspace)
            permissions.register_policy("probe", AllowPolicy())
            provider = ReadyProvider(script=[OfflineProvider.tool_call_turn("probe", arguments),
                                             OfflineProvider.text_turn("done")])
            # Session tool filtering must preserve the shared registry's locks.
            view = registry if index == 0 else FilteredToolRegistry(registry, exclude=("unrelated",))
            loops.append(AgentLoop(provider=provider, registry=view, permissions=permissions,
                                   transcript=transcript, workspace=workspace, session_id=f"s_lease_{index}"))
        results = await asyncio.wait_for(asyncio.gather(*(loop.run("独立写调用") for loop in loops)), 3)
        assert all(result.text == "done" for result in results)
        assert registry._write_lease.size() == 0

    asyncio.run(scenario())
    return intervals


def test_loop_serialises_two_writes_to_the_same_path(agent_home) -> None:
    """跨会话同路径互斥，不能由同一批次的顺序屏障代替租约。"""
    intervals = _run_independent_calls(
        agent_home,
        [{"path": "same.txt"}, {"path": "same.txt"}],
    )
    assert _overlap(intervals) <= 0, (
        f"同一路径的两次写重叠了 {_overlap(intervals):.4f}s —— "
        "租约没有接在 loop 的并行扇出上（只在 dispatch_all 里等于没接）"
    )


def test_loop_batch_keeps_different_path_writes_as_ordered_barriers(agent_home) -> None:
    """同一批次写入按模型顺序完成，即使它们修改不同路径。"""
    intervals, order = _run_two_calls(
        agent_home,
        [{"path": "a.txt"}, {"path": "b.txt"}],
    )
    assert _overlap(intervals) <= 0
    assert order == [("a.txt", "start"), ("a.txt", "end"), ("b.txt", "start"), ("b.txt", "end")]


def test_independent_sessions_still_write_different_paths_concurrently(agent_home) -> None:
    """不同会话写不同路径可同时进入，防止租约退化成全局锁。"""
    intervals = _run_independent_calls(agent_home, [{"path": "a.txt"}, {"path": "b.txt"}], rendezvous=True)
    assert _overlap(intervals) > 0, (
        f"不同路径的两次写没有重叠（overlap={_overlap(intervals):.4f}s）——"
        "租约退化成了全局锁，并行能力被吃掉了"
    )


def test_loop_serialises_same_path_even_with_different_spellings(agent_home) -> None:
    """跨会话同一文件的两种写法必须落到同一把锁上。"""
    intervals = _run_independent_calls(
        agent_home,
        [{"path": "a.txt"}, {"path": "./a.txt"}],
    )
    assert _overlap(intervals) <= 0, "路径写法不同就漏过了租约，键没归一"


def test_lease_applies_to_real_write_tools_only(agent_home) -> None:
    """只读工具不参与租约：两个 `read_file` 不该被串行化。

    这条守的是"租约不能变成全局串行"——`writes_path` 只有写类工具才为真。
    """
    from code_agent.tools.builtin.tools_builtin import ReadFileTool, WriteFileTool

    assert WriteFileTool.writes_path is True
    assert ReadFileTool.writes_path is False

    # 真值表本身：只读工具在注册表眼里拿不到租约（拿到的是空上下文）
    reg = ToolRegistry()
    reg.register(ReadFileTool())
    lease = reg.write_lease("read_file", {"path": "a.txt"}, ToolContext("s", agent_home))
    assert type(lease).__name__ == "nullcontext"
