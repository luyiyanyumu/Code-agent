"""Bounded native metadata from an actual, stable whole or windowed read."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from code_agent.tools.edit_receipts import SourceView


class NativeReadResult(str):
    """Keep public text unchanged while carrying immutable read provenance."""

    __slots__ = ("_source_views", "_source_stat")

    def __new__(
        cls,
        summary: str,
        source_views: Iterable[SourceView] = (),
        source_stat: tuple[int, int] | None = None,
    ) -> NativeReadResult:
        result = super().__new__(cls, summary)
        object.__setattr__(result, "_source_views", tuple(source_views))
        object.__setattr__(result, "_source_stat", source_stat)
        return result

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("NativeReadResult metadata is read-only")

    def __reduce__(self):
        return type(self), (str(self), self.source_views, self.source_stat)

    @property
    def source_views(self) -> tuple[SourceView, ...]:
        return self._source_views

    @property
    def source_stat(self) -> tuple[int, int] | None:
        return self._source_stat


def build_read_result(
    summary: str,
    *,
    path: str,
    raw: bytes,
    max_bytes: int,
    stat_before: tuple[int, int] | None,
    stat_after: tuple[int, int] | None,
) -> NativeReadResult:
    """Cache only a small whole read with stable stat and reversible UTF-8.

    Use the existing strict less-than byte-limit completeness rule.  Text comes
    from these actual read bytes: unlike an edit receipt, no write/newline
    simulation is appropriate.  No extra file contents are read by this helper.
    """
    if (
        stat_before is None
        or stat_before != stat_after
        or len(raw) >= max_bytes
        or stat_before[1] != len(raw)
    ):
        return NativeReadResult(summary)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return NativeReadResult(summary)
    if len(text) > 12_000 or len(text.splitlines()) > 260 or text.encode("utf-8") != raw:
        return NativeReadResult(summary)
    fingerprint = hashlib.blake2b(raw, digest_size=16).hexdigest()
    view = SourceView(path, text, 1, True, fingerprint)
    return NativeReadResult(summary, (view,), stat_after)


def build_window_read_result(
    summary: str,
    *,
    path: str,
    text: str,
    offset: int,
    stat_before: tuple[int, int] | None,
    stat_after: tuple[int, int] | None,
) -> NativeReadResult:
    """Keep a stable actual line window without claiming a full-file hash/read.

    text is the original lines delivered by the existing universal-newline
    reader, including whitespace and line endings.  Oversized windows are
    skipped whole, never shortened.  Even a window reaching EOF stays partial.
    """
    if (
        stat_before is None
        or stat_before != stat_after
        or not text
        or offset < 1
        or len(text) > 6_000
        or len(text.splitlines()) > 120
    ):
        return NativeReadResult(summary)
    view = SourceView(path, text, offset, False, "")
    return NativeReadResult(summary, (view,), stat_after)
