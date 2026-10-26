"""确定性工作流（M14）测试：步骤解析 + 顺序执行 + 上下文串联。"""

import asyncio

from code_agent.agent.workflow import WorkflowStep, parse_steps, run_workflow


def test_parse_steps_single():
    steps = parse_steps("单个 prompt 无分隔符")
    assert len(steps) == 1
    assert steps[0].name == "main"
    assert steps[0].prompt == "单个 prompt 无分隔符"


def test_parse_steps_multi():
    prompt = "# 第一步\n收集数据\n---step---\n# 第二步\n生成报告\n---step---\n发送"
    steps = parse_steps(prompt)
    assert [s.name for s in steps] == ["第一步", "第二步", "step3"]
    assert steps[0].prompt == "收集数据"
    assert steps[2].prompt == "发送"  # 无 # 名称的步骤用默认名 + 原文


def test_parse_steps_ignores_empty():
    prompt = "A\n---step---\n\n---step---\nB"
    steps = parse_steps(prompt)
    assert len(steps) == 2  # 空段被丢弃


def test_run_workflow_sequential_context():
    calls = []

    async def run_step(prompt, context):
        calls.append((prompt, context))
        idx = len(calls)
        return {"text": f"结果{idx}", "stop_reason": "end_turn", "turns": 1, "usage": {}}

    steps = [WorkflowStep("a", "任务A"), WorkflowStep("b", "任务B"), WorkflowStep("c", "任务C")]
    results = asyncio.run(run_workflow(steps, run_step))

    assert [r["name"] for r in results] == ["a", "b", "c"]
    assert [r["text"] for r in results] == ["结果1", "结果2", "结果3"]
    # 第二步的 context 是第一步结果，第三步是第二步结果
    assert calls[0][1] == ""       # 第一步无前序
    assert calls[1][1] == "结果1"
    assert calls[2][1] == "结果2"
    # 第一步 prompt 不带前序，后续步骤带
    assert calls[0][0] == "任务A"
    assert "前序步骤结果" in calls[1][0]


def test_run_workflow_non_dict_result():
    async def run_step(prompt, context):
        return f"文本结果"

    results = asyncio.run(run_workflow([WorkflowStep("a", "x")], run_step))
    assert results == [{"name": "a", "text": "文本结果"}]
