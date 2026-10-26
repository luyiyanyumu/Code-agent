"""One interpretation of a file read shared by tools and evidence ledgers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def has_read_window(arguments: Mapping[str, Any]) -> bool:
    """A limit alone requests the first lines, never an unbounded whole read."""
    return arguments.get("offset") is not None or arguments.get("limit") is not None


def read_window(arguments: Mapping[str, Any]) -> tuple[int, int] | None:
    """Return the bounded 1-based line window, or None for the original whole read.

    Keep the reader's historical clamps and defaults. Malformed values still
    raise for normal tool validation; evidence policies may conservatively fall
    back without changing the actual tool's argument validation.
    """
    if not has_read_window(arguments):
        return None
    return (max(1, int(arguments.get("offset") or 1)),
            max(1, min(int(arguments.get("limit") or 200), 2000)))
