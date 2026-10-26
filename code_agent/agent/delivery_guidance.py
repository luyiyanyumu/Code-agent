"""Concise model guidance; this module grants no execution or success receipt.

TaskProgress records version-specific checks, not complete semantic coverage of
the user's request. Even a ready ledger therefore must not make a focused test
look like a fully accepted delivery. The loop owns evidence and all decisions.
"""

from __future__ import annotations

from typing import Any


_HEADER = "[开发交付]\n"
_ACCEPTANCE = "定位性检查或定点测试通过只证明已运行范围；只有当前版本覆盖需求且相关完整验收真实通过，才交付完成结果。"
_BATCH = (
    "已有上下文的独立编辑和新建必要测试可同轮提交，验证置于写入之后；依赖未返回读结果的编辑留到下一轮。"
    "编辑回执已含当前源码时直接复用，不再读相同片段确认写入；验证靠实际测试。"
)
_REGRESSION = (
    "新增回归以覆盖缺口和独特缺陷/边界为最小必要增量，遵守用户明确测试要求；"
    "优先复用已有fixture/helper或参数化，只为当前新增覆盖生成必要代码；"
    "不复制整套现成验收，不另写只重复现成验收的校验器，也不另造泛化测试框架或重复整个参数矩阵。"
)
_GENERATE = (
    "明确文件写入请求优先局部精确编辑；仅确需新建或整体替换时生成完整文件，不重生成无关源码。"
    "少复述计划和重复进展，保留需求范围；完成回复简短报告主要改动、真实检查及剩余限制。"
)
_EXPLANATION = "工具说明的purpose/effects写简短具体，basis确有必要才写；保留审批必填字段、权限检查和日志。"


def delivery_guidance(task: Any, *, max_chars: int = 720) -> str:
    """Return bounded ordinary-development guidance without changing task state.

    Only complete sentences are included. The acceptance condition is mandatory
    whenever any guidance fits, so a tight context budget cannot turn a
    conditional delivery instruction into an unconditional success claim.
    """
    if task is None or not getattr(task, "enabled", False) or getattr(task, "annotation", False):
        return ""
    limit = max(0, int(max_chars))
    mandatory = _HEADER + _ACCEPTANCE
    if len(mandatory) > limit:
        return ""

    recovering = getattr(task, "phase", "") == "recover"
    ready = bool(getattr(task, "ready_to_finish", False))
    if recovering:
        current = (
            "已有初始失败复现后，直接依据现有诊断修复当前需求的根因，不再复跑已知失败基线；"
            "完成相关实现和必要测试后，再运行受影响的完整验收，不顺手扩大范围。"
        )
    elif ready:
        current = (
            "已有成功验证记录时先核对其覆盖范围；足够则直接交付，不再新增同构测试、整理无关imports或重复验证，也不再仅为补行号查询；"
            "确有需求缺口或新错误才继续。"
        )
    else:
        current = (
            "先完成同一需求的本批实现与必要回归，再批量运行相关完整验收；"
            "失败按真实诊断修正，不在每处小编辑后机械跑全套。"
        )
    text = mandatory
    # Initial repro failures legitimately keep a development task in recovery.
    # Efficient batching therefore belongs to every unfinished phase, rather
    # than only the first implementation branch.
    batch = (_BATCH,) if recovering or not ready else ()
    for paragraph in (current, *batch, _REGRESSION, _GENERATE, _EXPLANATION):
        if len(text) + 1 + len(paragraph) <= limit:
            text += "\n" + paragraph
    return text
