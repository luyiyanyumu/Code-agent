"""Large/slow filesystem queries must not freeze other agent sessions."""

import asyncio
import os
import threading
from pathlib import Path

import pytest

from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.code_tools import GlobTool, GrepTool, ListDirTool, _iter_files


def test_grep_literal_file_does_not_scan_siblings_or_subdirectories(tmp_path, monkeypatch):
    target = tmp_path / "target.py"
    target.write_text("needle in target\n", encoding="utf-8")
    (tmp_path / "sibling.py").write_text("needle in sibling\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.py").write_text("needle below\n", encoding="utf-8")

    def forbidden_scan(path):
        pytest.fail(f"A literal file search must not enumerate a directory: {path}")

    monkeypatch.setattr(os, "scandir", forbidden_scan)
    context = ToolContext(session_id="query-test", workspace=tmp_path)
    result = asyncio.run(GrepTool().execute({"path": "target.py", "pattern": "needle"}, context))
    assert "匹配 1 处（扫描 1 个文件" in result
    assert "target.py:1\n1: needle in target" in result
    assert "sibling" not in result and "deep.py" not in result


@pytest.mark.parametrize("path", ["missing.py", "missing/subdirectory"])
def test_grep_missing_path_errors_without_searching_parent(tmp_path, monkeypatch, path):
    (tmp_path / "sibling.py").write_text("needle\n", encoding="utf-8")

    def forbidden_scan(directory):
        pytest.fail(f"A missing path must not enumerate its parent: {directory}")

    monkeypatch.setattr(os, "scandir", forbidden_scan)
    context = ToolContext(session_id="query-test", workspace=tmp_path)
    with pytest.raises(ToolError, match="路径不存在"):
        asyncio.run(GrepTool().execute({"path": path, "pattern": "needle"}, context))


@pytest.mark.parametrize("max_files,cancel,exists", [(0, False, True), (-1, False, True), (1, True, True), (1, False, False)])
def test_single_file_scan_respects_budget_cancel_and_absence(tmp_path, max_files, cancel, exists):
    target = tmp_path / "target.py"
    if exists:
        target.write_text("needle\n", encoding="utf-8")
    cancelled = threading.Event()
    if cancel:
        cancelled.set()
    assert _iter_files(tmp_path, target, max_files=max_files, cancelled=cancelled) == []


@pytest.mark.parametrize("folder", ["node_modules", ".private"])
def test_single_file_scan_does_not_bypass_ignored_directory(tmp_path, folder):
    target = tmp_path / folder / "target.py"
    target.parent.mkdir()
    target.write_text("needle\n", encoding="utf-8")
    assert _iter_files(tmp_path, target) == []


def test_single_file_scan_rejects_outside_file_and_symlink(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("needle\n", encoding="utf-8")
    assert _iter_files(root, outside) == []
    link = root / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("File symlinks are unavailable on this Windows installation")
    assert _iter_files(root, link) == []
    context = ToolContext(session_id="query-test", workspace=root)
    with pytest.raises(ToolError, match="读取路径不在允许范围"):
        asyncio.run(GrepTool().execute({"path": "linked.py", "pattern": "needle"}, context))


def test_scan_prunes_ignored_directories_before_opening_them(tmp_path, monkeypatch):
    for name in ["node_modules", ".git", "target", ".private", "src"]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "example.java").write_text("class Example {}", encoding="utf-8")
    original = os.scandir
    visited = []

    def scan(path):
        path = Path(path)
        visited.append(path.relative_to(tmp_path).as_posix())
        assert path == tmp_path or path.name == "src", "Ignored trees must never be opened"
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    assert _iter_files(tmp_path, tmp_path) == [tmp_path / "src" / "example.java"]
    assert visited == [".", "src"]
    assert _iter_files(tmp_path, tmp_path / "node_modules") == []


def test_file_budget_stops_before_visiting_later_subtrees(tmp_path, monkeypatch):
    for name in ["a", "b"]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "file.py").write_text("pass", encoding="utf-8")
    original = os.scandir
    visited = []

    def scan(path):
        visited.append(Path(path).name)
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    assert _iter_files(tmp_path, tmp_path, max_files=1) == [tmp_path / "a" / "file.py"]
    assert "b" not in visited


@pytest.mark.parametrize("tool,arguments", [
    (GlobTool, {"pattern": "*.rs"}),
    (GrepTool, {"pattern": "only in final file"}),
])
def test_queries_report_scan_limit_instead_of_claiming_file_absence(tmp_path, monkeypatch, tool, arguments):
    monkeypatch.setattr("code_agent.tools.builtin.code_tools.MAX_SCAN_FILES", 2)
    for name in ["a.py", "b.py", "z.rs"]:
        (tmp_path / name).write_text("only in final file" if name == "z.rs" else "example", encoding="utf-8")
    ctx = ToolContext(session_id="query-test", workspace=tmp_path)
    result = asyncio.run(tool().execute(arguments, ctx))
    assert "2 个文件的扫描上限" in result
    assert "结果可能不完整" in result
    assert "本次扫描范围内没有匹配" in result if tool is GlobTool else "匹配 0 处" in result


def test_glob_does_not_resolve_each_walked_path_again(tmp_path, monkeypatch):
    for i in range(20):
        (tmp_path / f"example{i}.py").write_text("pass", encoding="utf-8")
    original = Path.resolve
    resolved_files = []

    def resolve(path, *args, **kwargs):
        if path.suffix == ".py":
            resolved_files.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    ctx = ToolContext(session_id="query-test", workspace=tmp_path)
    result = asyncio.run(GlobTool().execute({"pattern": "*.py"}, ctx))
    assert "20 个文件" in result
    assert resolved_files == []


@pytest.mark.parametrize("tool,arguments", [
    (GlobTool, {"pattern": "*.py"}),
    (GrepTool, {"pattern": "hello"}),
    (ListDirTool, {"depth": 2}),
])
@pytest.mark.parametrize("cancel", [False, True])
def test_slow_directory_io_leaves_event_loop_responsive(tmp_path, monkeypatch, tool, arguments, cancel):
    (tmp_path / "sample.py").write_text("hello", encoding="utf-8")
    context = ToolContext(session_id="query-test", user_id="test", workspace=tmp_path)
    original = os.scandir
    release = threading.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def scan(path):
            if Path(path) == tmp_path:
                loop.call_soon_threadsafe(entered.set)
                # A finite guard avoids hanging the test on the old synchronous implementation.
                release.wait(2)
            return original(path)

        monkeypatch.setattr(os, "scandir", scan)
        task = asyncio.create_task(tool().execute(arguments, context))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not task.done(), "A filesystem call blocked the server event loop"
            heartbeat = asyncio.Event()
            loop.call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 0.2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 0.2)
            else:
                release.set()
                assert "sample.py" in await asyncio.wait_for(task, 1)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_cancelled_scan_does_not_descend_into_another_directory(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "file.py").write_text("pass", encoding="utf-8")
    cancelled = threading.Event()
    original = os.scandir
    visited = []

    def scan(path):
        visited.append(Path(path))
        cancelled.set()
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    assert _iter_files(tmp_path, tmp_path, cancelled=cancelled) == []
    assert visited == [tmp_path]
