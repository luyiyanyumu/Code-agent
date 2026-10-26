"""契约工具（M32）：让 agent 自己能查"我的实现和契约对不对得上"。

一个工具：

    contract_check  检查某个实现文件与它的 `*.CONTRACT.md`

为什么必须有它（阶段 0 实验的直接结论）：

- 实验里 4 个独立实现全部**一次通过全部测试**，但其中 **16/25 的渲染文案漂移**测试一条都没抓到，
  另有 **2 个行为分歧**落在契约自己字面冲突的地方 → **证据的边界就是契约的边界**；
- 10 条被判定的义务里 **8 条**被实现者标为"只能靠猜"——**缺口必须被枚举出来**，
  否则它不会被任何人发现。

所以这个工具不判断"实现写得好不好"，只回答三件事：
**形状有没有被改（R1/R2）、有没有无主的实现（R3）、每条义务有没有落到证据上（R4/R6）**。

返回口径与 M29 的验收清单一致：给进度 ρ，并且**只详述第一个未满足项**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_agent.contract import check as check_mod
from code_agent.contract.check import list_contracts
from code_agent.contract.parse import SUFFIX
from code_agent.tools.base import Tool, ToolContext, ToolError


class ContractCheckTool(Tool):
    name = "contract_check"
    description = (
        "Check an implementation file against its contract (`<name>.CONTRACT.md` in the same "
        "directory): whether the declared shape (signatures/classes/constants/imports) was "
        "changed, whether any implemented function has no owning obligation, and whether every "
        "obligation has locatable evidence. Reports progress and ONLY the first unsatisfied item. "
        "Use it after filling in bodies; it never judges whether the implementation is *good*, "
        "only whether contract and implementation still agree."
    )
    description_zh = "对照契约检查实现：形状是否被改、有无无主实现、义务是否都有可定位的证据。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "实现文件路径（相对工作区），例如 code_agent/agent/routing.py",
            },
            "list_only": {
                "type": "boolean",
                "description": "只列出工作区里所有契约文件（不检查单个模块）",
            },
        },
        "required": [],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        workspace = Path(ctx.workspace)
        rel = str(arguments.get("path") or "").strip()

        if arguments.get("list_only") or not rel:
            if not rel and not arguments.get("list_only"):
                raise ToolError("要么给 path（要检查的实现文件），要么给 list_only=true（列出所有契约）")
            found = list_contracts(workspace)
            if not found:
                return (
                    f"[contract_check] 工作区里没有任何 `*{SUFFIX}` 契约文件。\n"
                    "契约层是**人写的设计**：先由人（或经人确认）在实现文件同目录写下契约，"
                    "AI 只写函数体。没有契约 = 这份实现不受契约层管辖。"
                )
            lines = [f"工作区里有 {len(found)} 份契约："]
            for path in found[:50]:
                try:
                    shown = path.relative_to(workspace)
                except ValueError:
                    shown = path
                lines.append(f"- {shown}")
            lines.append("")
            lines.append("逐份检查：contract_check(path=<实现文件>)")
            return "\n".join(lines)

        target = (workspace / rel).resolve()
        try:
            target.relative_to(workspace.resolve())
        except ValueError:
            raise ToolError(f"路径越出工作区：{rel}")
        if not target.is_file():
            raise ToolError(f"文件不存在：{rel}")

        report = check_mod.contract_check(target, module_label=rel)
        return report.summary()


def build_contract_tools() -> list[Tool]:
    return [ContractCheckTool()]
