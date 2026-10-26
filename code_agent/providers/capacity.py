"""Model capacity settings shared by persistence, routing, and context budgeting."""

from __future__ import annotations

from typing import Any


MAX_MODEL_TOKENS = 10_000_000

# Compatibility estimates used when a user has not supplied a model capacity.
_CONTEXT_WINDOW_RULES: tuple[tuple[str, int], ...] = (
    ("deepseek-v4", 1_000_000),
    ("deepseek", 131_072),
    ("kimi", 131_072),
    ("moonshot", 131_072),
    ("claude", 200_000),
    ("gpt-5", 400_000),
    ("gpt-4o", 128_000),
    ("qwen3", 32_768),
    ("qwen", 32_768),
    ("llama", 131_072),
    ("mistral", 32_768),
)


def context_window_for_model(model: str) -> int:
    name = (model or "").lower()
    return next((window for fragment, window in _CONTEXT_WINDOW_RULES if fragment in name), 131_072)


def normalize_capacity(value: Any, field: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if type(value) is not int or not 1 <= value <= MAX_MODEL_TOKENS:
        label = "上下文窗口" if field == "context_window" else "最大输出 Token"
        raise ValueError(f"{label}必须是 1 到 {MAX_MODEL_TOKENS} 之间的整数")
    return value


def effective_model_capacity(
    model: str, context_window: int | None = None, max_output_tokens: int | None = None,
) -> dict[str, int | None]:
    window = normalize_capacity(context_window, "context_window") or context_window_for_model(model)
    output = normalize_capacity(max_output_tokens, "max_output_tokens")
    if output is not None and output >= window:
        raise ValueError("最大输出 Token 必须小于上下文窗口")
    return {"context_window": window, "max_output_tokens": output}


def estimate_tokens(text: str) -> int:
    """Coarse mixed-language estimate, used for budgeting rather than billing."""
    return max(1, round(len(text.encode("utf-8")) / 2.2)) if text else 0
