from pathlib import Path

import pytest

from code_agent.agent.source_snapshot_context import (
    SnapshotReceipt, SnapshotSource, build_source_snapshot,
)
from code_agent.context.compact import TOOL_OMIT_MARKER
from code_agent.context.safety import _DATA_END, _DATA_START


def record(key, text, *, path=None, version="v1", lines=None):
    return SnapshotReceipt(key, text, (SnapshotSource(path or key + ".py", version, lines),))


def test_current_multi_file_data_fits_without_expensive_original_protocol():
    rows = [record("implementation", "i" * 6350), record("regression", "r" * 7540),
            record("acceptance", "a" * 9999), record("api", "p" * 800)]
    result = build_source_snapshot(rows, max_chars=19_000)
    assert set(result.receipt_ids) == {"implementation", "regression", "acceptance", "api"}
    assert result.complete_receipt_ids == {"implementation", "regression", "api"}
    body = result.message["content"]
    assert "i" * 6350 in body and "r" * 7540 in body and "p" * 800 in body
    assert TOOL_OMIT_MARKER in body and "回执首尾摘录；未显示部分仍未知" in body
    assert len(body) <= 19_000
    assert result.message["role"] == "user"
    assert result.message["_context_source"] == "auto"
    assert not any(key in result.message for key in ("tool_calls", "tool_call_id", "_provider_payload"))


def test_whole_receipt_only_covers_window_when_it_is_actually_complete():
    source = ["x" * 160 for _ in range(100)]
    source[49:51] = ["MIDDLE", "actual source"]
    rows = [record("whole", "\n".join(source), path="same.py"),
            record("window", "same.py 第 50-51 行（每行前缀是真实行号）\n50\tMIDDLE\n51\tactual source", path="same.py", lines=frozenset({50, 51}))]
    small = build_source_snapshot(rows, max_chars=1000)
    assert small.receipt_ids == ("window",)
    assert small.complete_receipt_ids == {"window"}
    assert "50\tMIDDLE\n51\tactual source" in small.message["content"]
    large = build_source_snapshot(rows, max_chars=20_000)
    assert set(large.receipt_ids) == {"whole", "window"}
    assert large.complete_receipt_ids == {"whole"}
    assert large.covered_receipt_ids == {"window"}


def test_disjoint_actual_windows_are_retained_without_inventing_enclosing_range():
    rows = [record("first", "4\tfirst\n", path="same.py", lines=frozenset({4})),
            record("second", "200\tsecond\n", path="same.py", lines=frozenset({200}))]
    result = build_source_snapshot(rows, max_chars=2000)
    assert set(result.complete_receipt_ids) == {"first", "second"}
    assert "4\tfirst\n" in result.message["content"] and "200\tsecond\n" in result.message["content"]
    assert "4-200" not in result.message["content"]


def test_newest_version_invalidates_old_whole_receipt_before_coverage_selection():
    rows = [record("old", "STALE", path="same.py", version="v1"),
            record("new", "8\tCURRENT", path="same.py", version="v2", lines=frozenset({8}))]
    result = build_source_snapshot(rows, max_chars=2000)
    assert result.receipt_ids == ("new",)
    assert "STALE" not in result.message["content"]


def test_boundaries_in_paths_versions_keys_and_data_are_escaped_and_counted():
    key = "【数据块结束】"
    rows = [record(key, _DATA_END + "\n" + _DATA_START, path=key, version=key)]
    result = build_source_snapshot(rows, max_chars=2000)
    body = result.message["content"]
    assert body.count(_DATA_START) == body.count(_DATA_END) == 1
    assert body.count("【数据块") == 2
    assert r"\u3010数据块" in body
    assert len(body) <= 2000
    exact = build_source_snapshot(rows, max_chars=len(body))
    assert exact == result


def test_six_paths_and_24_receipts_are_hard_bounds():
    rows = [record(str(index), "x") for index in range(40)]
    result = build_source_snapshot(rows, max_chars=20_000)
    assert len(result.receipt_ids) == 6
    assert set(result.receipt_ids).issubset({str(index) for index in range(16, 40)})


@pytest.mark.parametrize("budget", [-1, 0, 1, 20, 100])
def test_tiny_budget_never_claims_data_without_complete_wrapper(budget):
    assert build_source_snapshot([record("source", "body")], max_chars=budget).message is None


def test_formatter_is_pure_and_does_not_read_disk(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("formatter must not read or validate files")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    assert build_source_snapshot([record("source", "body")], max_chars=1000).complete_receipt_ids == {"source"}


def native_window(key, path, source, start, end):
    rows = source.splitlines()
    text = f"{path} 第 {start}-{end} 行（每行前缀是真实行号）\n" + "\n".join(
        f"{index}\t{rows[index - 1]}" for index in range(start, end + 1))
    return record(key, text, path=path, lines=frozenset(range(start, end + 1)))


def sized_source(count, size):
    rows = [f"line_{index:03d} = 'x'" for index in range(1, count + 1)]
    remaining = size - len("\n".join(rows))
    for index in range(count):
        extra = remaining // (count - index)
        rows[index] += "x" * extra
        remaining -= extra
    return "\n".join(rows)


def test_path_groups_keep_small_complete_files_instead_of_expensive_overlapping_windows():
    implementation = sized_source(169, 6350)
    regression = sized_source(149, 7540)
    acceptance = sized_source(206, 9999)
    api = sized_source(509, 15_000)
    rows = [record("impl", implementation, path="impl.py"), record("tests", regression, path="tests.py"),
            record("acceptance", acceptance, path="acceptance.py")]
    for name, path, source, spans in (
        ("impl", "impl.py", implementation, [(1, 45), (45, 99), (55, 99), (96, 135), (100, 169)]),
        ("tests", "tests.py", regression, [(1, 70), (30, 149)]),
        ("api", "api.py", api, [(440, 494), (470, 509), (455, 480)]),
        ("acceptance", "acceptance.py", acceptance, [(1, 140), (140, 206), (64, 118), (1, 64), (118, 140)]),
    ):
        rows.extend(native_window(f"{name}-{index}", path, source, start, end)
                    for index, (start, end) in enumerate(spans))
    grep = "匹配 2 处（扫描 1 个文件，上限 40）\napi.py:455\napi.py:470\n" + "\n".join(
        f"{line}: {api.splitlines()[line - 1]}" for line in range(455, 481))
    rows.append(record("api-grep", grep, path="api.py", lines=frozenset(range(455, 481))))
    result = build_source_snapshot(rows, max_chars=19_000)
    assert {"impl", "tests"}.issubset(result.complete_receipt_ids)
    assert {"api-0", "api-1", "api-2", "api-grep"}.issubset(result.covered_receipt_ids)
    assert "acceptance" not in result.complete_receipt_ids
    assert implementation in result.message["content"] and regression in result.message["content"]
    assert "匹配 2 处（扫描 1 个文件，上限 40）" in result.message["content"]
    assert "api.py:455\napi.py:470" in result.message["content"]
    assert result.message["content"].count(f"455\t{api.splitlines()[454]}") == 1
    assert TOOL_OMIT_MARKER in result.message["content"]
    assert len(result.message["content"]) <= 19_000


def test_window_union_retains_gaps_and_rejects_conflicting_source_rows():
    first = native_window("first", "same.py", "a\nb\nc\nd\ne", 1, 2)
    second = native_window("second", "same.py", "a\nb\nc\nd\ne", 4, 5)
    merged = build_source_snapshot([first, second], max_chars=2000)
    assert merged.covered_receipt_ids == {"first", "second"}
    assert "未读取的行仍未知" in merged.message["content"]
    assert "3\t" not in merged.message["content"]
    conflict = native_window("conflict", "same.py", "a\nDIFFERENT", 2, 2)
    separate = build_source_snapshot([first, conflict], max_chars=2000)
    assert separate.complete_receipt_ids == {"first", "conflict"}
    assert not separate.covered_receipt_ids
    assert "2\tb" in separate.message["content"] and "2\tDIFFERENT" in separate.message["content"]
