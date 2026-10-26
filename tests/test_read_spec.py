"""Real reader windows and version-specific source evidence stay aligned."""

import asyncio
import pytest

from code_agent.agent.execution_progress import ExecutionProgress
from code_agent.agent.task_progress import TaskProgress
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.code_tools import EditFileTool
from code_agent.tools.builtin.tools_builtin import ReadFileTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.read_spec import has_read_window, read_window


@pytest.mark.parametrize("arguments,expected", [
    ({}, None), ({"max_bytes": 8}, None),
    ({"offset": None, "limit": None}, None),
    ({"limit": 3}, (1, 3)), ({"offset": None, "limit": 3}, (1, 3)),
    ({"offset": 4}, (4, 200)), ({"offset": -10, "limit": 9000}, (1, 2000)),
])
def test_shared_read_window_spec(arguments, expected):
    assert read_window(arguments) == expected
    assert has_read_window(arguments) == (expected is not None)


@pytest.mark.parametrize("extra", [{"limit": 3}, {"offset": None, "limit": 3}])
def test_limit_only_reads_exact_numbered_head_and_observes_fresh_version(tmp_path, extra):
    target = tmp_path / "source.py"
    target.write_text("\n".join(f"line_{n}" for n in range(1, 20)), encoding="utf-8")
    observation = FsObservation()
    ctx = ToolContext(session_id="read-spec", workspace=tmp_path, fs_observation=observation)
    result = asyncio.run(ReadFileTool().execute({"path": "source.py", **extra}, ctx))
    assert result.splitlines() == [
        "source.py 第 1-3 行（每行前缀是真实行号）",
        "1\tline_1", "2\tline_2", "3\tline_3",
    ]
    assert observation.check_edit(target) is None
    target.write_text("external change", encoding="utf-8")
    assert "changed since" in observation.check_edit(target)


def test_omitting_both_window_fields_preserves_original_whole_reader(tmp_path):
    source = "".join(f"line_{n}\n" for n in range(250))
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    ctx = ToolContext(session_id="read-spec", workspace=tmp_path)
    expected = (tmp_path / "source.py").read_bytes().decode("utf-8")
    assert asyncio.run(ReadFileTool().execute({"path": "source.py"}, ctx)) == expected
    assert asyncio.run(ReadFileTool().execute({"path": "source.py", "max_bytes": 6}, ctx)) == "line_0"


def test_limit_only_does_not_change_the_reader_path_boundary(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("secret = 1", encoding="utf-8")
    ctx = ToolContext(session_id="read-spec", workspace=tmp_path)
    with pytest.raises(ToolError, match="不在允许范围"):
        asyncio.run(ReadFileTool().execute({"path": str(outside), "limit": 1}, ctx))


def test_limit_only_evidence_shares_real_line_coordinates_without_false_progress(tmp_path):
    (tmp_path / "source.py").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    ctx = ToolContext(session_id="read-spec", workspace=tmp_path)
    result = asyncio.run(ReadFileTool().execute({"path": "source.py", "limit": 3}, ctx))
    execution = ExecutionProgress(tmp_path)
    execution.observe("read_file", {"path": "source.py", "limit": 3}, result)
    execution.finish_turn()
    for index in range(6):
        # Spelling the same actual window differently is still old evidence.
        args = {"path": "source.py", "limit": 3, **({"offset": 1} if index % 2 else {})}
        execution.observe("read_file", args, result)
        execution.finish_turn()
    assert execution.stalled_turns == 6
    assert "已见行 1-3" in execution.context()
    assert "第 1-3 行" not in execution.context()


def test_limit_only_task_coverage_is_partial_and_matches_the_returned_window(tmp_path):
    (tmp_path / "source.py").write_text("\n".join(f"line_{n}" for n in range(30)), encoding="utf-8")
    progress = TaskProgress(tmp_path, "修复 source.py 的错误")
    arguments = {"path": "source.py", "limit": 3}
    assert progress.before_tool("read_file", arguments) is None
    result = asyncio.run(ReadFileTool().execute(arguments, ToolContext(session_id="read-spec", workspace=tmp_path)))
    progress.observe("read_file", arguments, result)
    key = progress._path("source.py")
    assert progress._line_coverage[key] == {1, 2, 3}
    assert not progress.state()["complete_reads"]


def test_real_edit_reopens_only_bounded_current_version_windows(tmp_path):
    target = tmp_path / "source.py"
    target.write_text("def sample():\n    return 1\n" + "\n".join(f"line_{n}" for n in range(3, 301)), encoding="utf-8")
    ctx = ToolContext(session_id="read-spec", workspace=tmp_path, fs_observation=FsObservation())
    task = TaskProgress(tmp_path, "修复 source.py 的返回值错误")
    execution = ExecutionProgress(tmp_path)
    initial = {"path": "source.py"}
    assert task.before_tool("read_file", initial) is None
    result = asyncio.run(ReadFileTool().execute(initial, ctx))
    task.observe("read_file", initial, result)
    execution.observe("read_file", initial, result)
    execution.finish_turn()
    assert task.state()["complete_reads"]

    edit = {"path": "source.py", "old_string": "def sample():\n    return 1", "new_string": "def sample():\n    return 2"}
    edited = asyncio.run(EditFileTool(None).execute(edit, ctx))
    assert "return 2" in target.read_text(encoding="utf-8")
    task.observe("edit_file", edit, edited, changed_paths=["source.py"])
    task.finish_turn()
    execution.record_effect(["source.py"])
    execution.finish_turn()
    assert not task.state()["complete_reads"]
    assert task._edit_receipts[task._path("source.py")] == ["sample"]
    assert task.before_tool("read_file", initial)
    assert task.before_tool("read_file", {"path": "source.py", "limit": 121})

    for first in (1, 21):
        args = {"path": "source.py", "limit": 10, **({"offset": first} if first > 1 else {})}
        assert task.before_tool("read_file", args) is None
        fresh = asyncio.run(ReadFileTool().execute(args, ctx))
        task.observe("read_file", args, fresh)
        task.finish_turn()
        execution.observe("read_file", args, fresh)
        execution.finish_turn()
        assert task.before_tool("read_file", args)
        execution.observe("read_file", args, fresh)
        execution.finish_turn()
        assert execution.stalled_turns == 1
    assert task.before_tool("read_file", {"path": "source.py", "offset": 41, "limit": 10})
    assert task.turns_without_delivery == 2
    assert not task.ready_to_finish

