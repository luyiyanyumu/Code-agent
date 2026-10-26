"""重复调用提醒（M47，对齐 DSH 的 `dsh-repeat-tool-reminder`）。

它与熔断器**互补**，所以这个文件里有两条断言是专门用来钉住"两者不会互相吃掉"的：
提醒管"成功但没用的重复"，熔断器管"失败重复并终止"。
最关键的语义是 **成功不清零**——照抄熔断器的"成功即清零"会让提醒永远不触发。
"""

from __future__ import annotations

import asyncio

from code_agent.agent.breaker import FailureBreaker
from code_agent.agent.loop import AgentLoop
from code_agent.agent.repeat_reminder import DEFAULT_THRESHOLDS, RepeatReminder
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool, ToolError
from code_agent.tools.builtin import BashTool, ReadFileTool, WriteFileTool
from code_agent.tools.registry import ToolRegistry


def _fire(reminder: RepeatReminder, times: int, *, tool: str = "grep", args=None):
    """连续调用同一个调用 times 次，返回每次的提醒（None 表示没提醒）。"""
    args = args or {"pattern": "x"}
    return [reminder.observe(tool, args) for _ in range(times)]


# ----------------------------------------------------------------------
# 阈值语义
# ----------------------------------------------------------------------
def test_fires_exactly_at_the_thresholds() -> None:
    r = RepeatReminder(thresholds=(3, 5, 8))
    notes = _fire(r, 8)
    fired_on = [i + 1 for i, n in enumerate(notes) if n]
    assert fired_on == [3, 5, 8], f"阈值各提醒一次，实际 {fired_on}"


def test_success_does_not_reset_the_streak() -> None:
    """**与熔断器最关键的差别**：成功不清零。

    重复一个成功的动作（反复读同一个区间、反复 grep 同一个词）同样在烧轮次，
    而熔断器永远不会响。如果这里也"成功即清零"，提醒就永远不触发。
    """
    r = RepeatReminder(thresholds=(3,))
    assert _fire(r, 2)[-1] is None
    assert _fire(r, 1)[-1] is not None, "第 3 次必须提醒（哪怕前两次都成功了）"


def test_different_arguments_do_not_accumulate() -> None:
    r = RepeatReminder(thresholds=(3,))
    for i in range(6):
        assert r.observe("grep", {"pattern": f"p{i}"}) is None


def test_another_call_in_between_resets_the_streak() -> None:
    """`grep X → read_file → grep X` 不是"连续两次 grep X"。

    中间那一步很可能正是它在推进的证据；把它算成连续，就会误报。
    """
    r = RepeatReminder(thresholds=(3,))
    assert r.observe("grep", {"pattern": "x"}) is None
    assert r.observe("grep", {"pattern": "x"}) is None
    assert r.observe("read_file", {"path": "a.txt"}) is None
    assert r.observe("grep", {"pattern": "x"}) is None, "被打断后不应立刻到阈值"
    assert r.observe("grep", {"pattern": "x"}) is None
    assert r.observe("grep", {"pattern": "x"}) is not None, "重新数到 3 才提醒"


def test_excluded_tools_neither_count_nor_reset() -> None:
    """记录型工具穿插在循环里，不能靠它"重置计数"把循环掩盖掉（DSH 明确记下的一条）。"""
    r = RepeatReminder(thresholds=(3,), exclude=frozenset({"todo_write"}))
    assert r.observe("grep", {"pattern": "x"}) is None
    assert r.observe("todo_write", {"todos": []}) is None
    assert r.observe("grep", {"pattern": "x"}) is None
    assert r.observe("todo_write", {"todos": []}) is None
    note = r.observe("grep", {"pattern": "x"})
    assert note is not None, "被排除的工具不该把连续计数重置掉"


def test_include_filter_tracks_only_listed_tools() -> None:
    r = RepeatReminder(thresholds=(2,), include=frozenset({"grep"}))
    assert [r.observe("bash", {"command": "ls"}) for _ in range(3)] == [None, None, None]
    assert r.observe("grep", {"pattern": "x"}) is None
    assert r.observe("grep", {"pattern": "x"}) is not None


def test_volatile_arguments_do_not_hide_a_loop() -> None:
    """指纹复用熔断器的归一化：只有 timeout 这类易变键不同，仍算同一个调用。

    否则"每次换个 timeout 重试"就能绕过提醒——那正是最常见的一种空转。
    """
    r = RepeatReminder(thresholds=(2,))
    assert r.observe("bash", {"command": "npm test", "timeout": 30}) is None
    assert r.observe("bash", {"command": "npm test", "timeout": 60}) is not None


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
def test_env_can_disable_and_retune(monkeypatch) -> None:
    monkeypatch.setenv("CODE_AGENT_REPEAT_THRESHOLDS", "0")
    assert RepeatReminder().enabled is False
    assert _fire(RepeatReminder(), 10) == [None] * 10

    monkeypatch.setenv("CODE_AGENT_REPEAT_THRESHOLDS", "2")
    assert _fire(RepeatReminder(), 2)[-1] is not None

    monkeypatch.setenv("CODE_AGENT_REPEAT_THRESHOLDS", "垃圾")
    assert RepeatReminder().thresholds == DEFAULT_THRESHOLDS, (
        "写错了要回退默认——不能静默把这道保护网摘掉"
    )


def test_reminder_text_states_the_count_and_a_next_step() -> None:
    note = RepeatReminder().reminder_text("grep", 3)
    assert "`grep`" in note and "第 3 次" in note
    assert "换个动作" in note or "明说在等什么" in note


# ----------------------------------------------------------------------
# 接进 loop
# ----------------------------------------------------------------------
def _loop(workspace, home, script):
    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    reg.register(BashTool())
    return AgentLoop(
        provider=OfflineProvider(script=script),
        registry=reg,
        permissions=PermissionManager(workspace_root=workspace),
        transcript=TranscriptStore(home / "transcripts"),
        workspace=workspace,
    )


def test_loop_appends_the_reminder_to_the_third_identical_call(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")

    loop = _loop(
        workspace,
        tmp_path,
        [
            OfflineProvider.tool_call_turn("read_file", {"path": "a.txt"}),
            OfflineProvider.tool_call_turn("read_file", {"path": "a.txt"}),
            OfflineProvider.tool_call_turn("read_file", {"path": "a.txt"}),
            OfflineProvider.text_turn("好"),
        ],
    )
    asyncio.run(loop.run("读三次同一个文件"))

    results = [
        e for e in loop.transcript.replay(loop.session_id) if e.type.value == "tool_result"
    ]
    assert len(results) == 3
    assert "[提示]" not in results[0].payload["result"]
    assert "[提示]" not in results[1].payload["result"]
    assert "[提示]" in results[2].payload["result"], "第三次必须带上提醒"

    notes = [
        e for e in loop.transcript.replay(loop.session_id)
        if e.type.value == "error" and e.payload.get("code") == "repeat_tool_reminder"
    ]
    assert len(notes) == 1, "提醒要单独落一条事件（工具结果会被截到 2000 字）"
    assert notes[0].payload["quiet"] is True


class _BoomTool(Tool):
    name = "boom"
    description = "always fails"
    parameters = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}

    async def execute(self, arguments, ctx):  # noqa: ANN001
        raise ToolError("总是失败")


def test_reminder_does_not_break_the_failure_breaker(tmp_path) -> None:
    """两个机制共存：提醒追加在结果**之后**，所以熔断器的失败前缀判定不受影响。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reg = ToolRegistry()
    reg.register(_BoomTool())
    pm = PermissionManager(workspace_root=workspace)
    # 让工具真的**执行**并抛错（否则会卡在"无专用策略 → 需审批 → 被拒"，
    # 那验证的是审批链路，不是我要测的"提醒不改前缀"）
    from code_agent.permissions.policy import AllowPolicy

    pm.register_policy("boom", AllowPolicy())
    loop = AgentLoop(
        provider=OfflineProvider(
            script=[OfflineProvider.tool_call_turn("boom", {"x": 1}) for _ in range(6)]
        ),
        registry=reg,
        permissions=pm,
        transcript=TranscriptStore(tmp_path / "transcripts"),
        workspace=workspace,
        breaker=FailureBreaker(limit=5),
    )
    result = asyncio.run(loop.run("一直失败"))

    assert result.stop_reason == "repeated_failure", "熔断器必须照旧工作"
    results = [
        e for e in loop.transcript.replay(loop.session_id) if e.type.value == "tool_result"
    ]
    assert "[提示]" in results[2].payload["result"], "第 3 次应该已经有提醒"
    assert results[2].payload["result"].startswith("[tool error]"), (
        "提醒追加在末尾，绝不能改变开头的前缀（熔断器按前缀判失败）"
    )
