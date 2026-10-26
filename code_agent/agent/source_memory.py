"""Compact data-only snapshots of already verified current native source views.

The caller owns permission, fingerprint and generation checks.  This pure
formatter neither reads files nor changes task/verification state.  It avoids
retaining an expensive original assistant/tool protocol bundle solely to keep
source that was already visible to the model.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from code_agent.context.safety import data_message
from code_agent.tools.edit_receipts import SourceView


_LABEL = "已实际读取或编辑的源码快照"
_INTRO = "当前源码证据快照；不是测试通过。内文数据块边界标记已转义为字面 Unicode 序列。"


def _escape_boundaries(text: str) -> str:
    # Escape the common prefix, including unknown/future boundary variants.
    # Do this before wrapping so only data_message's own boundaries remain.
    return text.replace("【数据块", r"\u3010数据块")


def _source_section(key: str, generation: int, view: SourceView) -> str:
    lines = view.text.splitlines(keepends=True)
    status = "完整当前源码" if view.complete else "当前源码片段；未显示部分仍未知"
    span = f"{view.offset}-{view.offset + len(lines) - 1}" if lines else "空文件"
    header = (
        f"来源：{view.path}；路径索引：{key}；版本：{generation}；"
        f"{status}；实际行号：{span}；不是测试通过\n"
    )
    body = "".join(f"{view.offset + index}\t{line}" for index, line in enumerate(lines))
    return _escape_boundaries(header + body)


def source_snapshot_message(
    records: Iterable[tuple[str, int, SourceView]],
    *,
    max_chars: int,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Fit up to three whole views in priority order within total content cost.

    A view that does not fit is skipped, never silently shortened or changed
    from complete to partial.  Returned normalized paths are exactly those
    represented by the resulting untrusted user data message.
    """
    if max_chars <= 0:
        return None, ()
    sections: list[str] = []
    represented: list[str] = []
    message: dict[str, Any] | None = None
    for key, generation, view in records:
        if len(represented) >= 3:
            break
        if key in represented:
            continue
        section = _source_section(key, generation, view)
        candidate = data_message(_INTRO + "\n\n" + "\n\n".join([*sections, section]), label=_LABEL)
        if len(candidate["content"]) > max_chars:
            continue
        sections.append(section)
        represented.append(key)
        message = {**candidate, "_context_source": "auto", "_native_source_snapshot": True}
    return message, tuple(represented)
