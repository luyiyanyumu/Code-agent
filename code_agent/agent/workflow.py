"""确定性工作流（M14）：把固定流程建模为有序步骤，逐步执行。

与自由 ReAct 循环互补：Automation 这类「步骤固定、要精确控制路径」的任务，
用工作流比自由循环更可控、更可审计——每步有明确边界（独立收口、独立轨迹），
前一步结果显式串成上下文给下一步，而不是让模型在一条长上下文里自由发挥。

设计要点：
- 纯编排层：不依赖 AgentLoop 的具体构造，只接收一个 `run_step` 回调，
  由调用方（SessionManager）注入每步的执行逻辑（通常是一个聚焦的 loop.run）。
- 步骤失败不中断：单步异常被 run_step 内部兜底（loop 已把错误转成文本），
  工作流只负责「顺序 + 上下文串联」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

#: automation prompt 里的步骤分隔符：出现即视为「多步骤工作流」
STEP_SEPARATOR = "\n---step---\n"


@dataclass
class WorkflowStep:
    name: str
    prompt: str


def parse_steps(prompt: str) -> list[WorkflowStep]:
    """把 workflow 格式的 prompt 拆成有序步骤。

    格式：用 `---step---` 分隔多个步骤；每步可选地以 `# 名称` 开头命名。
    无分隔符时返回单个步骤（退化为普通 prompt，行为与旧版一致）。
    """
    prompt = (prompt or "").strip()
    parts = [p.strip() for p in prompt.split(STEP_SEPARATOR)]
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return [WorkflowStep(name="main", prompt=prompt)]

    steps: list[WorkflowStep] = []
    for i, part in enumerate(parts, 1):
        name = f"step{i}"
        body = part
        lines = part.splitlines()
        if lines and lines[0].startswith("# "):
            name = lines[0][2:].strip() or name
            body = "\n".join(lines[1:]).strip()
        steps.append(WorkflowStep(name=name, prompt=body))
    return steps


RunStep = Callable[[str, str], Awaitable[dict[str, Any]]]


async def run_workflow(steps: list[WorkflowStep], run_step: RunStep) -> list[dict[str, Any]]:
    """顺序执行步骤，把前一步结果串成上下文给下一步。返回每步结果列表。

    run_step: async (prompt, context) -> dict，其中 context 是前一步的最终文本。
    返回的 dict 建议含 text / stop_reason / turns / usage 等字段。
    """
    results: list[dict[str, Any]] = []
    context = ""
    for step in steps:
        full_prompt = (
            f"前序步骤结果：\n{context}\n\n当前步骤：{step.prompt}"
            if context
            else step.prompt
        )
        r = await run_step(full_prompt, context)
        if not isinstance(r, dict):
            r = {"text": str(r)}
        context = str(r.get("text") or "")
        results.append({"name": step.name, **r})
    return results
