"""Public action explanations, separate from executable arguments and reasoning."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


APPROVAL_ARGUMENT = "_approval"
APPROVAL_INSTRUCTION = (
    "每次工具调用都在工具参数 _approval 中提供面向用户的简短操作说明："
    "purpose 用一句话说明本次操作目的，basis 可用一句话概括已知事实或判断依据，"
    "effects 简短列出将检查或修改的文件、运行的程序、访问的服务及预期获得的结果。"
    "说明必须针对本次调用和当前任务，不展开内部思考或逐步推理链，不复制推理流，"
    "不能仅写‘需要权限’或复制命令；不能把预期结果说成已经验证的事实。"
    "_approval 仅用于向用户解释，不能授予权限或改变实际工具行为。"
    "需人工审批的操作必须同时提供非空 purpose 和至少一条具体 effects，"
    "缺少时不会发起审批；收到说明缺失错误后，先补齐说明再重试本次调用。"
)
APPROVAL_SCHEMA = {
    "type": "object",
    "description": "Public action explanation; metadata only.",
    "properties": {
        "purpose": {"type": "string", "minLength": 1, "maxLength": 4000,
                    "description": "Purpose of this call."},
        "basis": {"type": "string", "maxLength": 800,
                  "description": "Known facts, not private reasoning."},
        "effects": {"type": "array", "minItems": 1, "maxItems": 12,
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "description": "Expected effects, not verified results."},
    },
    "required": ["purpose", "effects"],
    "additionalProperties": False,
}


def approval_details(value: Any) -> dict[str, Any]:
    """Keep explicit explanations only; never infer intent from commands."""
    value = value if isinstance(value, dict) else {}
    purpose = value.get("purpose")
    purpose = purpose.strip()[:4000] if isinstance(purpose, str) else ""
    effects = value.get("effects")
    if isinstance(effects, str):
        effects = [effects]
    effects = [item.strip()[:2000] for item in effects if isinstance(item, str) and item.strip()][:12] if isinstance(effects, list) else []
    return {"purpose": purpose, "effects": effects,
            "purpose_source": "agent" if purpose else "missing"}


def tool_explanation_details(value: Any) -> dict[str, Any]:
    """Persist only bounded, explicitly public fields; never inspect thinking streams."""
    value = value if isinstance(value, dict) else {}
    approval = approval_details(value)
    purpose = approval["purpose"][:800]
    basis = value.get("basis")
    basis = basis.strip()[:800] if isinstance(basis, str) else ""
    effects = [item[:400] for item in approval["effects"][:6]]
    return {"purpose": purpose, "basis": basis, "effects": effects,
            "source": "agent" if purpose or basis or effects else "missing"}


def executable_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Reserved metadata never reaches policies, leases or tool execution."""
    return {key: value for key, value in arguments.items() if key != APPROVAL_ARGUMENT}


def missing_approval_fields(explanation: dict[str, Any]) -> list[str]:
    """Check normalized public fields; no inferred intent or private trace fallback."""
    return [key for key in ("purpose", "effects") if not explanation.get(key)]


def approval_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Decorate a schema copy without mutating tool/MCP input definitions."""
    schema = deepcopy(parameters)
    if schema.get("type", "object") == "object":
        schema.setdefault("properties", {})[APPROVAL_ARGUMENT] = deepcopy(APPROVAL_SCHEMA)
        required = schema.setdefault("required", [])
        if APPROVAL_ARGUMENT not in required:
            required.append(APPROVAL_ARGUMENT)
    return schema
