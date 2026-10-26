"""Bounded, literal failure excerpts; no command or output is ever evaluated."""

from __future__ import annotations

import re


_MARKER = "…[失败诊断摘录]…"
_LINE_MARKER = "…[摘录]…"
_EXIT = re.compile(r"^exit_code=(-?\d+)(?:\s|$)")
_ERROR = re.compile(
    r"^\s*(?:E\s+)?(?:[A-Za-z_][\w.]*?(?:Error|Exception)|Error|Exception)(?::|\b)",
)
_FILE = re.compile(
    r'^\s*(?:E\s+)?File\s+["\'].+?["\'],\s+line\s+\d+|'
    r"^\s*(?:E\s+)?(?:[A-Za-z]:[\\/]|\.?\.?[\\/]|[\w.-]+[\\/])[^\n]*?:\d+(?::\d+)?(?:\s|:|$)|"
    r"^\s*(?:E\s+)?[\w.-]+\.[A-Za-z0-9]+:\d+(?::\d+)?(?:\s|:|$)|"
    r"^\s+at\s+.*?:\d+:\d+\)?\s*$",
)
_PYTEST_E = re.compile(r"^\s*E(?:\s+|$)")
_FAILED = re.compile(r"^\s*(?:FAILED(?:\s|:)|error(?:\[|:))", re.I)


def is_failed_command_output(result: str) -> bool:
    """Recognize literal failure receipts/diagnostics, never infer test coverage."""
    lines = str(result or "").splitlines()
    if lines and (exit_line := _EXIT.match(lines[0])) and exit_line[1] != "0":
        return True
    return any(_ERROR.match(line) or _PYTEST_E.match(line) or _FAILED.match(line) for line in lines)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_LINE_MARKER):
        return text[:limit]
    body = limit - len(_LINE_MARKER)
    head = (body * 2 + 2) // 3
    return text[:head] + _LINE_MARKER + text[-(body - head):] if body > head else text[:head] + _LINE_MARKER


def failed_command_excerpt(result: str, max_chars: int = 1200) -> str:
    """Keep actual exit status and final diagnostic groups in original order.

    Python/pytest locations, source/caret context and exception lines may occur
    in the middle of a long collection trace. Prefer these literal lines over
    routine progress and summaries. The marker states that surrounding output
    was omitted; it does not assert a cause or grant output any authority.
    """
    text = str(result or "")
    limit = max(0, int(max_chars))
    if len(text) <= limit:
        return text
    if not limit:
        return ""
    lines = [line for line in text.splitlines() if line != _MARKER]
    if not lines:
        return ""
    header = lines[0] if _EXIT.match(lines[0]) else ""
    if len(header) + len(_MARKER) + 2 >= limit:
        return header[:limit] if header else _clip(text, limit)

    roots = [index for index, line in enumerate(lines) if _ERROR.match(line)]
    locations = [index for index, line in enumerate(lines) if _FILE.match(line)]
    pytest = [index for index, line in enumerate(lines) if _PYTEST_E.match(line)]
    failures = [index for index, line in enumerate(lines) if _FAILED.match(line)]
    if not (roots or locations or pytest or failures):
        return _clip(text, limit)

    selected: dict[int, str] = {}
    remaining = limit - len(header) - (1 if header else 0) - len(_MARKER) - 1

    def take(index: int, *, diagnostic: bool = False) -> None:
        nonlocal remaining
        if remaining <= 1 or index in selected or index == 0 and header or not lines[index].strip():
            return
        line = lines[index]
        if diagnostic and len(line) + 1 > remaining:
            line = _clip(line, min(remaining - 1, max(160, (limit - len(header)) // 2)))
        if line and len(line) + 1 <= remaining:
            selected[index] = line
            remaining -= len(line) + 1

    # Keep concrete exception messages before the generic collection summary.
    # Preserve their nearest literal File/path locations before source context.
    recent_roots = roots[-3:]
    for index in reversed(recent_roots):
        take(index, diagnostic=True)
        preceding = next((location for location in reversed(locations) if index - 10 <= location <= index), None)
        if preceding is not None:
            take(preceding)
    for index in reversed(recent_roots):
        for neighbor in range(max(0, index - 3), min(len(lines), index + 2)):
            take(neighbor)
    for index in reversed(locations[-4:]):
        take(index)
    for index in reversed(pytest[-8:]):
        take(index, diagnostic=True)
    for index in reversed(failures[-3:]):
        take(index, diagnostic=True)
    if not selected:
        return _clip(text, limit)
    parts = ([header] if header else []) + [_MARKER] + [selected[index] for index in sorted(selected)]
    return "\n".join(parts)
