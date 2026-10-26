import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.tools_builtin import ReadFileTool
from code_agent.tools.read_receipts import NativeReadResult, build_read_result, build_window_read_result


def receipt(raw, *, max_bytes=65_536, before=None, after=None):
    stable = (123, len(raw))
    return build_read_result(
        raw.decode("utf-8", errors="replace") or "(空文件)", path="a.py", raw=raw,
        max_bytes=max_bytes, stat_before=stable if before is None else before,
        stat_after=stable if after is None else after,
    )


def test_native_metadata_is_readonly_json_plain_and_deepcopy_safe():
    result = receipt(b"value = 1\n")
    view, = result.source_views
    assert isinstance(result, str)
    assert str(result) == "value = 1\n"
    assert repr(result) == repr("value = 1\n")
    assert json.loads(json.dumps({"result": result})) == {"result": "value = 1\n"}
    assert view.expected_fingerprint == hashlib.blake2b(b"value = 1\n", digest_size=16).hexdigest()
    assert (view.path, view.text, view.offset, view.complete) == ("a.py", "value = 1\n", 1, True)
    assert result.source_stat == (123, 10)
    restored = copy.deepcopy(result)
    assert type(restored) is NativeReadResult
    assert restored == result
    assert restored.source_views == result.source_views
    assert restored.source_stat == result.source_stat
    with pytest.raises(AttributeError):
        result.source_views = ()
    with pytest.raises(AttributeError):
        result.source_stat = None


@pytest.mark.parametrize(
    ("text", "cached"),
    [("x" * 12_000, True), ("x" * 12_001, False), ("line\n" * 260, True), ("line\n" * 261, False)],
)
def test_character_and_line_boundaries_keep_whole_or_skip_entire_view(text, cached):
    result = receipt(text.encode("utf-8"))
    assert bool(result.source_views) is cached
    assert result == text
    if cached:
        assert result.source_views[0].text == text
        assert result.source_views[0].complete is True
    else:
        assert result.source_stat is None


@pytest.mark.parametrize(("raw", "max_bytes", "cached"), [(b"abc", 4, True), (b"abc", 3, False), (b"ab", 2, False), (b"", 0, False), (b"", 1, True)])
def test_byte_limit_exact_boundary_is_never_claimed_complete(raw, max_bytes, cached):
    assert bool(receipt(raw, max_bytes=max_bytes).source_views) is cached


@pytest.mark.parametrize("raw", [b"invalid\xff\n", b"cut multibyte \xe4\xb8"])
def test_decode_replacement_output_remains_visible_but_is_not_cached(raw):
    result = receipt(raw)
    assert "\ufffd" in result
    assert result.source_views == ()
    assert result.source_stat is None


def test_valid_replacement_character_is_utf8_reversible_not_a_decode_failure():
    result = receipt("valid \ufffd\n".encode("utf-8"))
    assert result.source_views[0].text == "valid \ufffd\n"


def test_read_crlf_is_original_decoded_bytes_without_write_simulation():
    raw = "中文\r\nvalue\r\n".encode("utf-8")
    result = receipt(raw)
    view, = result.source_views
    assert view.text == raw.decode("utf-8")
    assert "\r\r\n" not in view.text
    assert view.expected_fingerprint == hashlib.blake2b(raw, digest_size=16).hexdigest()


@pytest.mark.parametrize(
    ("before", "after"),
    [((123, 3), (124, 3)), ((123, 3), (123, 4)), ((123, 9), (123, 9)), (None, (123, 3)), ((123, 3), None)],
)
def test_unstable_missing_or_inconsistent_stat_never_creates_cache(before, after):
    result = build_read_result("abc", path="a.py", raw=b"abc", max_bytes=100, stat_before=before, stat_after=after)
    assert result == "abc"
    assert result.source_views == () and result.source_stat is None


class Observation:
    def __init__(self):
        self.reads = []

    def record_read(self, path, **kwargs):
        self.reads.append((path, kwargs))


def read(tmp_path, args, observation=None):
    return asyncio.run(ReadFileTool().execute(args, ToolContext("read-test", tmp_path, fs_observation=observation)))


@pytest.mark.parametrize("raw", [b"a\r\nb\r\n", b"", "中文\n".encode("utf-8")])
def test_actual_whole_file_read_display_and_observation_remain_unchanged(tmp_path, raw):
    path = tmp_path / "a.py"
    path.write_bytes(raw)
    obs = Observation()
    result = read(tmp_path, {"path": "a.py"}, obs)
    assert result == (raw.decode("utf-8") or "(空文件)")
    assert type(result) is NativeReadResult
    view, = result.source_views
    assert view.text == raw.decode("utf-8") and view.complete
    assert result.source_stat == (path.stat().st_mtime_ns, len(raw))
    assert obs.reads == [(path, {"raw": raw})]


def test_actual_truncated_read_keeps_cheap_observation_and_original_display(tmp_path):
    path = tmp_path / "a.py"
    path.write_bytes(b"abcdef")
    obs = Observation()
    result = read(tmp_path, {"path": "a.py", "max_bytes": 3}, obs)
    assert result == "abc"
    assert result.source_views == () and result.source_stat is None
    assert obs.reads == [(path, {"cheap": True})]


def test_exact_file_byte_limit_still_has_no_full_metadata(tmp_path):
    (tmp_path / "a.py").write_bytes(b"abc")
    result = read(tmp_path, {"path": "a.py", "max_bytes": 3})
    assert result == "abc" and result.source_views == ()


@pytest.mark.parametrize("args", [{"path": "a.py", "limit": 1}, {"path": "a.py", "offset": 2, "limit": 1}])
def test_window_read_keeps_display_and_cheap_observation_and_caches_only_window(tmp_path, args):
    (tmp_path / "a.py").write_bytes(b"first\nsecond\n")
    obs = Observation()
    result = read(tmp_path, args, obs)
    assert isinstance(result, str)
    wanted = "1\tfirst" if args.get("offset", 1) == 1 else "2\tsecond"
    assert result.splitlines()[1] == wanted
    view, = result.source_views
    assert view.offset == args.get("offset", 1)
    assert view.text == ("first\n" if view.offset == 1 else "second\n")
    assert view.complete is False and view.expected_fingerprint == ""
    assert obs.reads == [(tmp_path / "a.py", {"cheap": True})]


def test_concurrent_change_during_real_read_is_not_cached_and_content_read_once(tmp_path, monkeypatch):
    path = tmp_path / "a.py"
    path.write_bytes(b"old\n")
    original = Path.read_bytes
    calls = []

    def racing_read(self):
        data = original(self)
        if self == path:
            calls.append(self)
            self.write_bytes(b"changed while reading\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", racing_read)
    obs = Observation()
    result = read(tmp_path, {"path": "a.py"}, obs)
    assert result == "old\n"
    assert result.source_views == () and result.source_stat is None
    assert calls == [path]
    assert obs.reads == [(path, {"raw": b"old\n"})]


def test_optional_stat_failure_does_not_change_successful_read_result(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_bytes(b"value\n")
    monkeypatch.setattr(ReadFileTool, "_source_stat", lambda *args: None)
    result = read(tmp_path, {"path": "a.py"})
    assert result == "value\n" and result.source_views == ()


@pytest.mark.parametrize(
    ("text", "cached"),
    [("x" * 5_999 + "\n", True), ("x" * 6_000 + "\n", False), ("line\n" * 120, True), ("line\n" * 121, False), ("", False)],
)
def test_window_size_boundaries_keep_whole_partial_or_skip(text, cached):
    result = build_window_read_result(
        "unchanged display", path="a.py", text=text, offset=95,
        stat_before=(123, 50_000), stat_after=(123, 50_000),
    )
    assert result == "unchanged display"
    assert bool(result.source_views) is cached
    if cached:
        view, = result.source_views
        assert view.text == text and view.offset == 95
        assert not view.complete and view.expected_fingerprint == ""
        assert result.source_stat == (123, 50_000)
    else:
        assert result.source_stat is None


@pytest.mark.parametrize(("before", "after"), [((123, 10), (124, 10)), ((123, 10), (123, 11)), (None, (123, 10)), ((123, 10), None)])
def test_window_unstable_or_missing_stat_never_caches(before, after):
    result = build_window_read_result(
        "original", path="a.py", text="read line\n", offset=40,
        stat_before=before, stat_after=after,
    )
    assert result == "original" and result.source_views == () and result.source_stat is None


def test_actual_crlf_window_preserves_read_whitespace_and_never_becomes_full(tmp_path):
    path = tmp_path / "a.py"
    path.write_bytes(b"first \t\r\nsecond  \r\nthird\r\n")
    result = read(tmp_path, {"path": "a.py", "offset": 2, "limit": 2})
    assert result == "a.py 第 2-3 行（每行前缀是真实行号）\n2\tsecond\n3\tthird"
    view, = result.source_views
    assert view.text == "second  \nthird\n" and view.offset == 2
    assert not view.complete and view.expected_fingerprint == ""
    assert result.source_stat == (path.stat().st_mtime_ns, path.stat().st_size)
    entire_window = read(tmp_path, {"path": "a.py", "limit": 120})
    assert entire_window.source_views[0].complete is False


def test_actual_oversized_window_keeps_all_display_without_caching(tmp_path):
    path = tmp_path / "a.py"
    path.write_bytes(b"line\n" * 121)
    result = read(tmp_path, {"path": "a.py", "limit": 121})
    assert len(result.splitlines()) == 122
    assert result.endswith("121\tline")
    assert result.source_views == () and result.source_stat is None


def test_empty_or_beyond_eof_window_keeps_original_message_without_source_claim(tmp_path):
    (tmp_path / "a.py").write_bytes(b"")
    result = read(tmp_path, {"path": "a.py", "offset": 95, "limit": 2})
    assert result == "（第 95 行起没有内容；文件共约 0 行）"
    assert not hasattr(result, "source_views")


def test_window_concurrent_change_drops_metadata_with_one_content_open(tmp_path, monkeypatch):
    path = tmp_path / "a.py"
    path.write_bytes(b"first\nsecond\n")
    original_stat = ReadFileTool._source_stat
    original_open = Path.open
    stats = []
    opened = []

    def stat_with_change(file):
        if stats:
            file.write_bytes(b"changed between read and stat\n")
        stats.append(file)
        return original_stat(file)

    def count_open(file, *args, **kwargs):
        if file == path and args and args[0] == "r":
            opened.append(file)
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(ReadFileTool, "_source_stat", staticmethod(stat_with_change))
    monkeypatch.setattr(Path, "open", count_open)
    obs = Observation()
    result = read(tmp_path, {"path": "a.py", "offset": 2, "limit": 1}, obs)
    assert result.endswith("2\tsecond")
    assert result.source_views == () and result.source_stat is None
    assert opened == [path]
    assert obs.reads == [(path, {"cheap": True})]
