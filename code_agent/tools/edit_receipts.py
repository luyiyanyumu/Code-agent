"""Bounded, in-memory source evidence from successful native edits.

The string result remains the tool's ordinary public summary.  Source views are
Python metadata, never recovered from model-controlled result text or read back
from disk.  A consumer must compare each fingerprint with its post-tool file
observation before using a view as current source evidence.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass, field


_FULL_CHARS = 10_000
_FULL_LINES = 240
_WINDOW_CHARS = 6_000
_WINDOW_LINES = 120
_RESULT_CHARS = 20_000
_RESULT_FILES = 3
_CONTEXT_LINES = 3


@dataclass(frozen=True, slots=True)
class ChangeRange:
    """Inclusive, one-based old/new ranges; an empty range has end=start-1."""

    old_start: int
    old_end: int
    new_start: int
    new_end: int

    @property
    def old_count(self) -> int:
        return self.old_end - self.old_start + 1

    @property
    def new_count(self) -> int:
        return self.new_end - self.new_start + 1

    @property
    def changed_lines(self) -> int:
        """Size of the bounded change, without counting the shifted suffix."""
        return max(self.old_count, self.new_count)


def changed_line_range(before: str, after: str) -> ChangeRange | None:
    """Find the changed span in linear time by retaining a common head/tail.

    Line endings stay attached so adding/removing the final newline is a real
    one-line change.  Several disjoint edits produce their enclosing span; this
    deliberately avoids claiming that all enclosed lines individually changed.
    """
    old_lines = before.splitlines(keepends=True)
    new_lines = after.splitlines(keepends=True)
    head = 0
    shortest = min(len(old_lines), len(new_lines))
    while head < shortest and old_lines[head] == new_lines[head]:
        head += 1
    if head == len(old_lines) == len(new_lines):
        return None
    tail = 0
    while (
        tail < shortest - head
        and old_lines[len(old_lines) - tail - 1] == new_lines[len(new_lines) - tail - 1]
    ):
        tail += 1
    return ChangeRange(head + 1, len(old_lines) - tail, head + 1, len(new_lines) - tail)


def source_fingerprint(text: str, *, linesep: str | None = None) -> str:
    """Fingerprint UTF-8 bytes produced by an ordinary newline=None text write.

    TextIOWrapper translates each LF to os.linesep, including LF in an existing
    CRLF sequence.  The optional separator makes that Windows rule testable on
    every platform without reading a file or altering global platform state.
    """
    separator = os.linesep if linesep is None else linesep
    written = text.replace("\n", separator) if separator != "\n" else text
    return hashlib.blake2b(written.encode("utf-8"), digest_size=16).hexdigest()


def source_readback_text(after: str, *, linesep: str | None = None) -> str:
    """Simulate atomic text-write translation followed by universal-newline read.

    On Windows an embedded CRLF becomes CRCRLF during newline=None writing;
    reading that back yields two LF lines.  Views and their offsets must use
    this current readback text, while fingerprints still use raw write bytes.
    """
    separator = os.linesep if linesep is None else linesep
    written = after.replace("\n", separator) if separator != "\n" else after
    return written.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True, slots=True)
class SourceView:
    path: str
    text: str = field(repr=False)
    offset: int
    complete: bool
    expected_fingerprint: str


class NativeEditResult(str):
    """A normal string with immutable, non-JSON source metadata attached."""

    __slots__ = ("_source_views",)

    def __new__(cls, summary: str, source_views: Iterable[SourceView] = ()) -> NativeEditResult:
        result = super().__new__(cls, summary)
        views: list[SourceView] = []
        paths: set[str] = set()
        remaining = _RESULT_CHARS
        for view in source_views:
            if len(views) >= _RESULT_FILES or remaining <= 0:
                break
            if view.path in paths:
                continue
            # Keep a constructor supplied view bounded too.  If clipped, it is
            # necessarily partial; no substring may masquerade as whole source.
            cap_lines = _FULL_LINES if view.complete else _WINDOW_LINES
            cap_chars = min(remaining, _FULL_CHARS if view.complete else _WINDOW_CHARS)
            lines = view.text.splitlines(keepends=True)
            selected = _whole_lines(lines, cap_chars, cap_lines)
            preview = "".join(selected)
            if not preview and view.text:
                continue
            complete = view.complete and view.offset == 1 and preview == view.text
            if not complete and (len(preview) > _WINDOW_CHARS or len(selected) > _WINDOW_LINES):
                selected = _whole_lines(lines, min(remaining, _WINDOW_CHARS), _WINDOW_LINES)
                preview = "".join(selected)
            views.append(SourceView(view.path, preview, view.offset, complete, view.expected_fingerprint))
            paths.add(view.path)
            remaining -= len(preview)
        object.__setattr__(result, "_source_views", tuple(views))
        return result

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("NativeEditResult metadata is read-only")

    @property
    def source_views(self) -> tuple[SourceView, ...]:
        return self._source_views


def _whole_lines(lines: list[str], max_chars: int, max_lines: int) -> list[str]:
    result: list[str] = []
    used = 0
    for line in lines[:max_lines]:
        if used + len(line) > max_chars:
            break
        result.append(line)
        used += len(line)
    return result


def _source_view(path: str, before: str, after: str, remaining: int) -> SourceView | None:
    fingerprint = source_fingerprint(after)
    current = source_readback_text(after)
    lines = current.splitlines(keepends=True)
    if len(current) <= min(_FULL_CHARS, remaining) and len(lines) <= _FULL_LINES:
        return SourceView(path, current, 1, True, fingerprint)

    change = changed_line_range(before, current)
    anchor = change.new_start - 1 if change else 0
    start = max(0, anchor - _CONTEXT_LINES)
    # For a deletion, the empty new range still anchors a window around the
    # join.  At EOF start may equal len(lines); the preceding context remains.
    changed_end = max(anchor, change.new_end) if change else anchor
    end = min(len(lines), changed_end + _CONTEXT_LINES)
    selected = _whole_lines(lines[start:end], min(remaining, _WINDOW_CHARS), _WINDOW_LINES)
    preview = "".join(selected)
    if not preview:
        # A single overlong line cannot be represented without cutting a line.
        return None
    return SourceView(path, preview, start + 1, False, fingerprint)


def build_edit_result(
    summary: str,
    changes: Iterable[tuple[str, str, str]],
) -> NativeEditResult:
    """Attach at most three current, bounded views from actual successful writes.

    Pass only successful writes as (path, before, after).  Repeated edits to one
    path are coalesced to its first before and final after.  UTF-8-unencodable
    strings cannot describe a successful UTF-8 write, so receive no source view.
    """
    combined: dict[str, tuple[str, str]] = {}
    for path, before, after in changes:
        original = combined[path][0] if path in combined else before
        combined[path] = (original, after)
    views: list[SourceView] = []
    remaining = _RESULT_CHARS
    for path, (before, after) in combined.items():
        if len(views) >= _RESULT_FILES or remaining <= 0:
            break
        try:
            view = _source_view(path, before, after, remaining)
        except UnicodeError:
            continue
        if view is not None:
            views.append(view)
            remaining -= len(view.text)
    return NativeEditResult(summary, views)
