"""逐工具协作式超时（对齐 DSH 的 `dsh-tool-call-timeout-policy`）。

要钉住四件事：

1. **默认零行为变化**：没声明 `timeout` 的工具永远不被截断——全局兜底
   （`CODE_AGENT_TOOL_TIMEOUT_DEFAULT`）默认也是关的；
2. 声明了期限的工具到点后 loop **停止等待**，回一条说清"哪个工具 / 多大预算 /
   怎么恢复"的结果，而不是把一轮对话挂死；
3. 超时对审计与失败日志**可见**（outcome=`timeout`），并且按失败计入熔断
   ——同一个调用连着超时就是原地打转；
4. 超时机制**不碰取消语义**：`CancelledError` 照旧原样上抛（用户点"停止"必须真停），
   工具**自己**抛的 `TimeoutError` 也不会被谎报成"超过协作式预算"。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from code_agent.agent import loop as loop_mod
from code_agent.agent.breaker import FailureBreaker, fingerprint
from code_agent.agent.loop import AgentLoop
from code_agent.models.events import ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.builtin import BashTool, ReadFileTool
from code_agent.tools.builtin.web_tools import WebFetchTool, WebSearchTool
from code_agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# 假工具与跑 loop 的脚手架（照 tests/test_loop.py 的写法）
# ---------------------------------------------------------------------------
class FakeTool(Tool):
    """可控的假工具：delay 秒后返回 text；`timeout` 由子类自己声明。"""

    name = "fake_tool"
    description = "test-only fake tool"

    def __init__(self, delay: float = 0.0, text: str = "假工具结果") -> None:
        self.delay = delay
        self.text = text
        self.calls = 0

    async def execute(self, arguments, ctx):  # type: ignore[override]
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.text


def _make_loop(workspace, tools, script, *, on_tool_executed=None,
               breaker=None, max_turns=None):
    reg = ToolRegistry()
    pm = PermissionManager(workspace_root=workspace)
    for tool in tools:
        reg.register(tool)
        # 没有专用策略的（假）工具默认走 DefaultPolicy = need_approval，
        # 而这里没有审批通道（fail closed）→ 会变成 denied，测不到超时。显式放行。
        pm.register_policy(tool.name, AllowPolicy())
    store = TranscriptStore(workspace.parent / "transcripts")
    kwargs = {}
    if max_turns is not None:
        kwargs["max_turns"] = max_turns
    loop = AgentLoop(
        provider=OfflineProvider(script=script),
        registry=reg,
        permissions=pm,
        transcript=store,
        workspace=workspace,
        on_tool_executed=on_tool_executed,
        breaker=breaker,
        **kwargs,
    )
    return loop


def _tool_results(loop) -> list[str]:
    """轨迹里回给模型的工具结果文本（按顺序）。"""
    return [
        str(e.payload.get("result"))
        for e in loop.transcript.replay(loop.session_id)
        if e.type.value == "tool_result"
    ]


# ---------------------------------------------------------------------------
# 1. 默认零行为变化
# ---------------------------------------------------------------------------
def test_tool_without_declared_timeout_is_never_cut_off(workspace):
    """没声明 timeout 的工具：预算解析为 None（不设限），慢一点也不截断。"""
    tool = FakeTool(delay=0.2, text="慢是慢，但没声明期限")
    loop = _make_loop(
        workspace,
        [tool],
        [OfflineProvider.tool_call_turn("fake_tool", {}), OfflineProvider.text_turn("收到")],
    )
    assert loop._tool_timeout("fake_tool") is None, "默认必须是「不设限」"

    started = time.monotonic()
    result = asyncio.run(loop.run("跑个慢工具"))
    elapsed = time.monotonic() - started

    assert result.text == "收到"
    assert _tool_results(loop)[0] == "慢是慢，但没声明期限", "结果必须原样回给模型"
    assert elapsed >= 0.2, "没期限就该真的等它跑完"


def test_global_default_is_off_and_tool_declaration_wins(monkeypatch):
    """全局兜底默认关闭；开了也只兜"没声明"的工具，工具自己的声明优先。"""

    class PlainTool(FakeTool):
        name = "plain_tool"

    class BudgetTool(FakeTool):
        name = "budget_tool"
        timeout = 5.0

    loop = AgentLoop.__new__(AgentLoop)          # 只测解析：不必构造 provider
    loop.registry = ToolRegistry()
    loop.registry.register(PlainTool())
    loop.registry.register(BudgetTool())

    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_DEFAULT", 0.0)
    assert loop._tool_timeout("plain_tool") is None, "默认全局兜底必须是关的"
    assert loop._tool_timeout("budget_tool") == 5.0

    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_DEFAULT", 0.25)
    assert loop._tool_timeout("plain_tool") == 0.25
    assert loop._tool_timeout("budget_tool") == 5.0, "工具自己的声明优先于全局兜底"


def test_registry_without_get_falls_back_to_no_timeout(monkeypatch):
    """鸭子类型注册表没有 .get：探测失败一律不设限（宁可不管，也不乱管）。"""
    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_DEFAULT", 0.25)

    class DuckRegistry:
        async def dispatch(self, name, arguments, ctx):     # pragma: no cover - 不该被调到
            return "ok"

    loop = AgentLoop.__new__(AgentLoop)
    loop.registry = DuckRegistry()
    assert loop._tool_timeout("anything") is None


def test_builtin_tools_are_untouched_and_web_tools_declare_upstream_30s():
    """既有工具零变化；只有联网这两个按上游 `dsh-tool-web` 声明 30s。"""
    assert BashTool.timeout is None, "bash 的超时在自己的执行器里，别在这里再套一层"
    assert ReadFileTool.timeout is None
    assert Tool.timeout is None, "默认值必须是不设限"
    assert WebSearchTool.timeout == 30.0
    assert WebFetchTool.timeout == 30.0


# ---------------------------------------------------------------------------
# 2. 到点：绝不挂死，回一条可恢复的结果
# ---------------------------------------------------------------------------
def test_slow_tool_hits_the_deadline_and_says_how_to_recover(workspace):
    """timeout=0.05 的工具睡 1s：loop 不等满 1s，回超时结果并继续收口。"""

    class SlowTool(FakeTool):
        name = "slow_tool"
        timeout = 0.05

    slow = SlowTool(delay=1.0)
    loop = _make_loop(
        workspace,
        [slow],
        [OfflineProvider.tool_call_turn("slow_tool", {}), OfflineProvider.text_turn("换个方式")],
    )

    started = time.monotonic()
    result = asyncio.run(loop.run("跑个慢工具"))
    elapsed = time.monotonic() - started

    assert elapsed < 0.7, f"没在 deadline 处收手，等了 {elapsed:.2f}s"
    assert result.text == "换个方式", "一次超时不该终止整轮对话"
    assert slow.calls == 1, "工具只该被调用一次"

    text = _tool_results(loop)[0]
    assert "[tool timeout]" in text
    assert "slow_tool" in text, "超时结果必须点名是哪个工具"
    assert "0.05s" in text, "超时结果必须给出预算"
    assert "不要原样重试" in text, "超时结果必须给出恢复动作"
    assert text.startswith("[tool error]"), (
        "刻意按失败分类（见 loop.py 里的理由）：熔断器/验收/回退/前端都按这两个前缀判失败"
    )


def test_fast_tool_with_generous_timeout_is_untouched(workspace):
    """声明了 5s、却马上返回的工具：结果原样透传，路径与没设限时一致。"""

    class FastTool(FakeTool):
        name = "fast_tool"
        timeout = 5.0

    loop = _make_loop(
        workspace,
        [FastTool(delay=0.0, text="快得很")],
        [OfflineProvider.tool_call_turn("fast_tool", {}), OfflineProvider.text_turn("收口")],
    )
    result = asyncio.run(loop.run("跑个快工具"))
    assert result.text == "收口"
    assert _tool_results(loop)[0] == "快得很"


# ---------------------------------------------------------------------------
# 3. 审计 / 失败日志 / 熔断看得见
# ---------------------------------------------------------------------------
def test_timeout_is_visible_to_audit_failure_log_and_breaker(workspace):
    """超时仍写审计和失败日志，并按失败计入熔断。"""
    audits: list[tuple[str, str]] = []

    class SlowTool(FakeTool):
        name = "slow_tool"
        timeout = 0.05

    loop = _make_loop(
        workspace,
        [SlowTool(delay=1.0)],
        [OfflineProvider.tool_call_turn("slow_tool", {}), OfflineProvider.text_turn("换个方式")],
        on_tool_executed=lambda name, args, outcome: audits.append((name, outcome)),
    )
    asyncio.run(loop.run("跑个慢工具"))

    assert ("slow_tool", "timeout") in audits, f"审计没记到超时：{audits}"
    logs = [path.read_text(encoding="utf-8") for path in (workspace / "memory").glob("*.md")]
    assert any("slow_tool" in str(log) and "超时" in str(log) for log in logs), logs
    # 超时算一次失败，避免把失败结果计成成功。
    assert loop.breaker.totals.get(fingerprint("slow_tool", {})) == 1


def test_repeated_identical_timeouts_trip_the_breaker(workspace):
    """同一个调用连着超时 = 原地打转：必须熔断，而不是把剩余轮数烧在必超时的调用上。

    这是"超时结果用 `[tool error]` 前缀"这个选择的**行为依据**（见 loop.py 的注释）。
    """

    class SlowTool(FakeTool):
        name = "slow_tool"
        timeout = 0.05

    class AlwaysCallProvider(OfflineProvider):
        async def chat(self, messages, tools):
            return OfflineProvider.tool_call_turn("slow_tool", {"q": "同一次调用"})

    loop = _make_loop(
        workspace,
        [SlowTool(delay=1.0)],
        script=[],
        breaker=FailureBreaker(limit=3),
        max_turns=10,
    )
    loop.provider = AlwaysCallProvider()

    result = asyncio.run(loop.run("一直重试同一个调用"))
    assert result.stop_reason == "repeated_failure", "连着超时没有熔断"
    assert "熔断" in result.text


# ---------------------------------------------------------------------------
# 4. 取消语义与"工具自己的超时"
# ---------------------------------------------------------------------------
def test_external_cancellation_is_not_turned_into_a_timeout(workspace):
    """用户点"停止"必须真停：外部取消要原样上抛，不许被吞、也不许改写成超时结果。"""

    class BlockingTool(FakeTool):
        name = "blocking_tool"
        timeout = 30.0

        async def execute(self, arguments, ctx):  # type: ignore[override]
            await asyncio.Event().wait()          # 永远挂着，只等外部取消

    loop = _make_loop(workspace, [BlockingTool()], script=[])
    tc = ToolCall(name="blocking_tool", arguments={})
    ctx = ToolContext(session_id=loop.session_id, workspace=workspace)

    async def scenario() -> None:
        task = asyncio.create_task(loop._execute_tool(tc, ctx))
        await asyncio.sleep(0.05)                 # 在 30s 的 deadline 之前取消
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_tool_own_timeout_error_is_not_reported_as_the_policy_deadline(workspace):
    """工具自己抛的 TimeoutError（socket 之类）：按普通执行异常回报，不谎报预算。

    3.11+ 内建 `TimeoutError` 就是 `asyncio.TimeoutError`，而且是 `OSError` 的子类，
    所以这一条是必需的：只有本策略真正到期（`deadline.expired()`）才算超时裁定。
    """

    class SelfTimeoutTool(FakeTool):
        name = "self_timeout_tool"
        timeout = 5.0                          # 有预算，但它是自己先炸的

        async def execute(self, arguments, ctx):  # type: ignore[override]
            raise TimeoutError("底层 socket 超时")

    class NoBudgetTool(SelfTimeoutTool):
        name = "no_budget_tool"
        timeout = None                         # 压根没有 deadline

    audits: list[tuple[str, str]] = []
    loop = _make_loop(
        workspace,
        [SelfTimeoutTool(), NoBudgetTool()],
        [
            OfflineProvider.tool_call_turn("self_timeout_tool", {}),
            OfflineProvider.tool_call_turn("no_budget_tool", {}),
            OfflineProvider.text_turn("收口"),
        ],
        on_tool_executed=lambda name, args, outcome: audits.append((name, outcome)),
    )
    asyncio.run(loop.run("跑两个自己会超时的工具"))

    for text in _tool_results(loop):
        assert "[tool timeout]" not in text, f"把自己的超时谎报成策略超时：{text}"
        assert text.startswith("[tool error] TimeoutError:"), text
    assert ("self_timeout_tool", "error:TimeoutError") in audits
    assert ("no_budget_tool", "error:TimeoutError") in audits
