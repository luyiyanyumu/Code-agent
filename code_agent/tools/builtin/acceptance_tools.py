"""验收清单工具（M29 · 借自 AgentFlow 的 verifier）。

两个工具，刻意做得很小：

    acceptance_set    声明有序验收清单（2~6 条，动手前先写）
    acceptance_check  按清单判定，只返回**第一个未满足项**与进度 ρ=ℓ/n

为什么让模型自己声明：验收标准是任务的语义，"做完"的定义只有提需求的人（以及
读懂需求的模型）知道。平台猜出来的标准只会变成噪音。清单有顺序、有条数上限，
所以它同时是"任务计划"——**计划要短**这条纪律在这里是硬的，不是劝告。
"""

from __future__ import annotations

from typing import Any

from code_agent.sessions.acceptance import (
    CHECK_KINDS,
    MAX_ITEMS,
    AcceptanceList,
    collect_evidence,
    failure_count,
    since_last_prompt,
)
from code_agent.tools.base import Tool, ToolContext


class AcceptanceSetTool(Tool):
    name = "acceptance_set"
    description = (
        "Declare an ordered acceptance checklist for the current task (2-6 items, in order). "
        "Each item is {label, check}. Supported checks: "
        "tool:<name> | patch:<path> | file_exists:<path> | "
        "file_contains:<path>::<text> | no_failures. "
        "Write it before making changes; it is also the task plan, so keep it short."
    )
    description_zh = "声明有序验收清单（2~6 条）：既是「做完的标准」，也是「够短的计划」。"
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": (
                    "Ordered acceptance items, each {label, check}. "
                    f"At most {MAX_ITEMS} items. Checks: {' | '.join(CHECK_KINDS)}. "
                    "file_contains uses <path>::<text>."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "人类可读的一条（一句话）"},
                        "check": {"type": "string", "description": "机器可判定的判据"},
                    },
                    "required": ["label", "check"],
                },
            },
            "note": {"type": "string", "description": "可选的补充说明（比如为什么要这个顺序）"},
        },
        "required": ["items"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        lst = AcceptanceList.parse(arguments.get("items"), note=str(arguments.get("note") or ""))
        if not lst.items:
            return (
                "[acceptance_set] 没有一条合法判据，清单未写入。\n"
                f"判据类型：{' | '.join(CHECK_KINDS)}\n"
                "例如：patch:pkg/mathx.py、tool:bash、file_contains:pkg/mathx.py::def add"
            )
        dropped = len(arguments.get("items") or []) - len(lst.items)
        lst.save(ctx.session_id)
        lines = [f"[acceptance_set] 已记录 {len(lst.items)} 条有序验收判据："]
        for i, item in enumerate(lst.items, 1):
            lines.append(f"{i}. {item.label}  ← `{item.check}`")
        if dropped > 0:
            lines.append(f"（另有 {dropped} 条被判据不合法或超出 {MAX_ITEMS} 条上限，未记录）")
        lines.append("")
        lines.append("按这个顺序推进；每完成一条用 acceptance_check 复核，不要跳过前面那条。")
        return "\n".join(lines)


class AcceptanceCheckTool(Tool):
    name = "acceptance_check"
    description = (
        "Evaluate the declared acceptance checklist against what has actually happened "
        "(tool results in this session + the workspace files), and report progress and "
        "ONLY the first unsatisfied item."
    )
    description_zh = "按验收清单判定：给出进度 ρ=ℓ/n，只返回第一个未满足项。"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, transcript: Any | None = None):
        self._transcript = transcript

    def _events(self, session_id: str) -> list[Any]:
        if self._transcript is None:
            from code_agent.config import transcripts_dir
            from code_agent.storage.jsonl import TranscriptStore

            self._transcript = TranscriptStore(transcripts_dir())
        try:
            return self._transcript.replay(session_id)
        except Exception:                      # 读不到轨迹不是"清单通过"
            return []

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        lst = AcceptanceList.load(ctx.session_id)
        if lst is None:
            return (
                "[acceptance_check] 本会话还没有验收清单。"
                "如果这个任务有明确的「做完标准」，先用 acceptance_set 写上 2~6 条。"
            )
        events = self._events(ctx.session_id)
        report = lst.evaluate(
            collect_evidence(
                events,
                workspace=ctx.workspace,
                # `no_failures` 判据的输入：本轮（最后一条真实用户消息之后）的失败数。
                # 不传的话这条判据恒为真——模型会拿着"全部满足"去收口。
                failures=failure_count(events, since_seq=since_last_prompt(events)),
            )
        )
        if report.passed:
            return report.summary() + "\n\n全部满足：可以收口给出结论了。"
        return report.digest()


def build_acceptance_tools(transcript: Any | None = None) -> list[Tool]:
    return [AcceptanceSetTool(), AcceptanceCheckTool(transcript)]
