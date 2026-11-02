"""Published reasoning capabilities and their Chat Completions wire mapping.

The IDs/default mirror DeepSeek Harness's model-info contract. Unknown models
receive no inferred controls: accepting an arbitrary effort can silently leave
reasoning enabled on gateways that ignore unsupported fields.
"""
from __future__ import annotations

from typing import Any


# Current DeepSeek catalog plus its documented Flash aliases. Legacy
# deepseek-chat/reasoner are deliberately absent: their contract differs.
_DEEPSEEK_MODELS = frozenset({
    "deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
})
_EFFORTS = (
    ("off", "关闭", "适合无需推理的简单任务"),
    ("low", "低", "适合常规任务，优先响应速度"),
    ("high", "高", "默认强度，平衡质量与速度"),
    ("max", "最高", "适合复杂任务，优先回答质量"),
)


def reasoning_metadata(provider: str, model: str) -> dict[str, Any] | None:
    """Return a fresh browser-safe capability object for a verified model."""
    if provider not in {"deepseek", "openai", "openai_compat"} or model not in _DEEPSEEK_MODELS:
        return None
    return {"efforts": [{"id": key, "name": name, "description": description}
                        for key, name, description in _EFFORTS],
            "defaultEffort": "high"}


def resolve_reasoning_effort(provider: str, model: str, effort: str | None = None) -> str | None:
    """Validate explicit selection or capture the same advertised default."""
    metadata = reasoning_metadata(provider, model)
    if metadata is None:
        if effort is not None:
            raise ValueError("该模型未声明可选择的推理强度")
        return None
    if effort is None:
        return metadata["defaultEffort"]
    if effort not in {entry["id"] for entry in metadata["efforts"]}:
        raise ValueError("该模型不支持所选推理强度，请重新选择")
    return effort


def reasoning_request_fields(effort: str | None) -> dict[str, Any]:
    """Translate Harness's 'off' ID to the documented DeepSeek Chat API."""
    if effort is None:
        return {}
    if effort == "off":
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
