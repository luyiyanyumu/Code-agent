import hashlib
import io
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from code_agent.atomic_write import atomic_replace
from code_agent.tools import edit_receipts
from code_agent.tools.edit_receipts import (
    ChangeRange,
    NativeEditResult,
    SourceView,
    build_edit_result,
    changed_line_range,
    source_fingerprint,
    source_readback_text,
)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("", "", None),
        ("a\nb\n", "a\nb\n", None),
        ("a\nb\n", "a\nx\nb\n", ChangeRange(2, 1, 2, 2)),
        ("a\nx\nb\n", "a\nb\n", ChangeRange(2, 2, 2, 1)),
        ("a\nb\nc\n", "a\nx\nc\n", ChangeRange(2, 2, 2, 2)),
        ("", "a\n", ChangeRange(1, 0, 1, 1)),
        ("a\n", "", ChangeRange(1, 1, 1, 0)),
        ("a\n", "a\nb\n", ChangeRange(2, 1, 2, 2)),
        ("a\nb\n", "a\n", ChangeRange(2, 2, 2, 1)),
        ("a", "a\n", ChangeRange(1, 1, 1, 1)),
        ("a\n", "a", ChangeRange(1, 1, 1, 1)),
        ("a\r\nb\r\n", "a\r\nx\r\nb\r\n", ChangeRange(2, 1, 2, 2)),
    ],
)
def test_changed_range_insertion_deletion_replacement_and_eof(before, after, expected):
    assert changed_line_range(before, after) == expected


def test_insert_does_not_count_1653_shifted_lines_as_changed():
    before = "".join(f"line {n}\n" for n in range(2_000))
    after = before.replace("line 465\n", "inserted\nline 465\n")
    change = changed_line_range(before, after)
    assert change == ChangeRange(466, 465, 466, 466)
    assert (change.old_count, change.new_count, change.changed_lines) == (0, 1, 1)


def test_disjoint_edits_report_enclosing_span_not_shifted_suffix():
    before = "a\nb\nc\nd\ne\nf\n"
    after = "a\nB\nc\nd\nE\nf\n"
    assert changed_line_range(before, after) == ChangeRange(2, 5, 2, 5)


def test_small_source_is_complete_and_metadata_is_not_serialized_or_repr():
    after = "# private source\nvalue = 2\n"
    result = build_edit_result("ordinary summary", [("a.py", "value = 1\n", after)])
    assert isinstance(result, str)
    assert str(result) == "ordinary summary"
    assert repr(result) == repr("ordinary summary")
    assert json.loads(json.dumps({"result": result})) == {"result": "ordinary summary"}
    view, = result.source_views
    assert (view.path, view.text, view.offset, view.complete) == ("a.py", after, 1, True)
    assert view.expected_fingerprint == source_fingerprint(after)
    assert "private source" not in repr(view)
    with pytest.raises(AttributeError):
        result.source_views = ()
    with pytest.raises(FrozenInstanceError):
        view.text = "changed"


@pytest.mark.parametrize(("chars", "lines", "complete"), [(10_000, 240, True), (10_001, 240, False), (9_999, 241, False)])
def test_full_source_exact_size_boundaries(chars, lines, complete):
    after = "a\n" * (lines - 1) + "b" * (chars - 2 * (lines - 1))
    result = build_edit_result("ok", [("a.py", "", after)])
    view, = result.source_views
    assert view.complete is complete
    if complete:
        assert view.text == after
    else:
        assert len(view.text) <= 6_000
        assert len(view.text.splitlines()) <= 120


def test_large_file_view_is_changed_range_plus_three_lines():
    lines = [f"line {n}\n" for n in range(1, 401)]
    before = "".join(lines)
    lines[199] = "updated\n"
    after = "".join(lines)
    view, = build_edit_result("ok", [("large.py", before, after)]).source_views
    assert (view.offset, view.complete) == (197, False)
    assert view.text == "".join(lines[196:203])
    assert view.expected_fingerprint == source_fingerprint(after)


def test_large_changed_span_is_whole_line_partial_with_both_caps():
    before = "original\n" * 500
    after = "replacement " + "x" * 60 + "\n"
    after *= 500
    view, = build_edit_result("ok", [("large.py", before, after)]).source_views
    assert not view.complete
    assert len(view.text) <= 6_000
    assert len(view.text.splitlines()) <= 120
    assert after.startswith(view.text)
    assert view.text.endswith("\n")


def test_line_cap_is_enforced_before_source_character_cap():
    after = "x\n" * 500
    view, = build_edit_result("ok", [("many.py", "", after)]).source_views
    assert len(view.text.splitlines()) == 120
    assert view.complete is False


def test_overlong_first_line_never_becomes_a_fake_whole_or_cut_line():
    result = build_edit_result("ok", [("one.py", "", "x" * 10_001)])
    assert result == "ok"
    assert result.source_views == ()


@pytest.mark.parametrize("deletion_at_eof", [False, True])
def test_large_deletion_window_is_exact_current_source(deletion_at_eof):
    lines = [f"line {n}\n" for n in range(400)]
    before = "".join(lines)
    del lines[-1 if deletion_at_eof else 199]
    after = "".join(lines)
    view, = build_edit_result("ok", [("a.py", before, after)]).source_views
    start = view.offset - 1
    assert view.text == "".join(lines[start:start + len(view.text.splitlines())])
    assert not view.complete
    assert 1 <= view.offset <= len(lines)


def test_at_most_three_files_and_total_preview_cap_with_partial_third():
    after = "x" * 99 + "\n"
    after *= 90  # 9000 chars; each of the first two views can remain complete.
    result = build_edit_result("ok", [(str(n), "", after) for n in range(5)])
    assert [v.path for v in result.source_views] == ["0", "1", "2"]
    assert sum(len(v.text) for v in result.source_views) == 20_000
    assert [v.complete for v in result.source_views] == [True, True, False]
    assert len(result.source_views[2].text.splitlines()) == 20


def test_repeated_same_path_uses_first_before_and_final_after():
    before = "a\n" * 400
    intermediate = "b\n" + before
    final = intermediate.replace("b\n", "c\n", 1)
    result = build_edit_result("ok", [("a.py", before, intermediate), ("a.py", intermediate, final)])
    view, = result.source_views
    assert view.path == "a.py"
    assert view.text == "c\na\na\na\n"
    assert view.expected_fingerprint == source_fingerprint(final)


@pytest.mark.parametrize("text", ["a\nb\n", "a\r\nb\r\n", "a\nb", "中文\n😀\n"])
@pytest.mark.parametrize("separator", ["\n", "\r\n"])
def test_fingerprint_matches_actual_text_write_translation(text, separator):
    target = io.BytesIO()
    stream = io.TextIOWrapper(target, encoding="utf-8", newline=separator)
    stream.write(text)
    stream.flush()
    expected = hashlib.blake2b(target.getvalue(), digest_size=16).hexdigest()
    assert source_fingerprint(text, linesep=separator) == expected
    readback = io.TextIOWrapper(io.BytesIO(target.getvalue()), encoding="utf-8", newline=None).read()
    assert source_readback_text(text, linesep=separator) == readback
    stream.detach()


def test_builder_windows_fingerprint_and_views_never_read_disk(monkeypatch):
    monkeypatch.setattr(edit_receipts.os, "linesep", "\r\n")

    def forbidden(*args, **kwargs):
        raise AssertionError("source receipts must use actual write memory")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    after = "中文\nvalue = 2\n"
    view, = build_edit_result("ok", [("a.py", "", after)]).source_views
    assert view.text == after
    assert view.expected_fingerprint == hashlib.blake2b(after.replace("\n", "\r\n").encode("utf-8"), digest_size=16).hexdigest()


def test_empty_file_still_has_complete_empty_current_evidence():
    view, = build_edit_result("ok", [("empty.py", "old\n", "")]).source_views
    assert (view.text, view.offset, view.complete) == ("", 1, True)
    assert view.expected_fingerprint == source_fingerprint("")


def test_unencodable_memory_cannot_claim_successful_utf8_source_evidence():
    result = build_edit_result("ok", [("invalid.py", "", "\ud800")])
    assert result.source_views == ()


def test_constructor_also_bounds_direct_views_and_does_not_upgrade_partial():
    views = [SourceView(str(n), "line\n" * 1_000, 17, False, "fingerprint") for n in range(5)]
    result = NativeEditResult("ok", views)
    assert len(result.source_views) == 3
    assert all(v.offset == 17 and not v.complete for v in result.source_views)
    assert all(len(v.text.splitlines()) <= 120 and len(v.text) <= 6_000 for v in result.source_views)


@pytest.mark.parametrize("after", ["a\r\nb\r\n", "a\r\nb\nc\r\n"])
def test_real_atomic_write_crlf_and_mixed_views_match_readback(tmp_path, after):
    path = tmp_path / "written.py"
    atomic_replace(path, after)
    actual = path.read_text(encoding="utf-8")
    view, = build_edit_result("ok", [("written.py", "", after)]).source_views
    assert view.text == actual == source_readback_text(after)
    assert (view.offset, view.complete) == (1, True)
    assert view.text.splitlines() == actual.splitlines()
    assert view.expected_fingerprint == hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()


def test_windows_150_crlf_lines_read_as_300_and_are_never_complete(monkeypatch):
    monkeypatch.setattr(edit_receipts.os, "linesep", "\r\n")
    after = "line\r\n" * 150
    current = source_readback_text(after)
    assert len(after.splitlines()) == 150
    assert len(current.splitlines()) == 300
    view, = build_edit_result("ok", [("many.py", "", after)]).source_views
    assert view.complete is False
    assert len(view.text.splitlines()) == 120
    assert current.startswith(view.text)
    assert view.expected_fingerprint == source_fingerprint(after)


def test_real_atomic_large_crlf_edit_view_has_current_readback_line_offsets(tmp_path):
    path = tmp_path / "large.py"
    before = "".join(f"line {n}\n" for n in range(400))
    atomic_replace(path, before)
    before_read = path.read_text(encoding="utf-8")
    after = before_read.replace("line 199\n", "UPDATED\r\nINSERTED\r\n")
    atomic_replace(path, after)
    current = path.read_text(encoding="utf-8")
    view, = build_edit_result("ok", [("large.py", before_read, after)]).source_views
    assert not view.complete
    assert view.offset == 197
    start = view.offset - 1
    actual_lines = current.splitlines(keepends=True)
    assert view.text == "".join(actual_lines[start:start + len(view.text.splitlines())])
    change = changed_line_range(before_read, current)
    assert view.text == "".join(actual_lines[change.new_start - 4:change.new_end + 3])
    assert view.expected_fingerprint == hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()


def test_readback_conversion_does_not_change_public_range_comparison_semantics():
    before = "a\nb\n"
    after = "a\r\nb\r\n"
    assert changed_line_range(before, after) == ChangeRange(1, 2, 1, 2)
    assert source_readback_text(after, linesep="\n") == before
