"""Recognize leaked DSML tool envelopes without parsing or executing them.

This guard is deliberately anchored to a response's beginning. Documentation,
quoted examples and fenced code are ordinary assistant text, not tool calls.
The caller should inspect the final assembled response, so stream chunk splits
do not affect recognition.
"""

from __future__ import annotations

import re


# DeepSeek uses U+FF5C FULLWIDTH VERTICAL LINE. Some compatible services render
# the same delimiter as one or two ASCII pipes with spaces between them.
_PIPE = r"(?:[|｜]\s*)+"
_ENVELOPE = re.compile(
    r"^<\s*/?\s*" + _PIPE + r"DSML\s*" + _PIPE + r"/?\s*"
    r"(?P<name>[A-Za-z_]*)(?P<tail>.*)\Z",
    re.IGNORECASE | re.DOTALL,
)
_NAMESPACE_ONLY = re.compile(
    r"^<\s*/?\s*" + _PIPE + r"DSML\s*\Z", re.IGNORECASE,
)
_CALL_TAGS = ("calls", "function_calls", "invoke")
_LEADING = re.compile(r"^[\s\ufeff]+")


def is_text_tool_call(text: str | None) -> bool:
    """Whether a response starts with an actual or truncated DSML call tag.

    This is a display/protocol guard only. It does not interpret a tool name,
    parameters, XML or commands and must never authorize their execution.
    """
    if not isinstance(text, str):
        return False
    body = _LEADING.sub("", text, count=1)
    if not body.startswith("<"):
        return False
    match = _ENVELOPE.match(body)
    if match is None:
        # A stream may end immediately after the recognizable namespace.
        return _NAMESPACE_ONLY.fullmatch(body) is not None
    name = match["name"].casefold()
    tail = match["tail"]
    if not name:
        return not tail.strip()
    if name in _CALL_TAGS:
        # Avoid treating a longer identifier such as callsign as a call tag.
        return not tail or tail[0].isspace() or tail[0] in ">/"
    # A partial tag is evidence only when the response actually ends there.
    return not tail.strip() and any(tag.startswith(name) for tag in _CALL_TAGS)
