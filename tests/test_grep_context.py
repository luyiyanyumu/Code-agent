"""Native grep returns enough bounded context without duplicating source lines."""

import asyncio
import re

from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.code_tools import GrepTool
from code_agent.tools.fs_observation import FsObservation


def grep(workspace, **arguments):
    context = ToolContext(session_id="grep-context", workspace=workspace)
    return asyncio.run(GrepTool().execute(arguments, context))


def source(workspace, *, matches=(40,)):
    lines = [f"value_{n} = {n}" for n in range(1, 111)]
    for line in matches:
        lines[line - 1] = f"def needle_{line}():"
    path = workspace / "target.py"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path, lines


def returned_lines(result):
    return [int(match.group(1)) for line in result.splitlines()
            if (match := re.match(r"^(\d+): ", line))]


def test_literal_file_context_18_contains_the_whole_twelve_line_edit_anchor(tmp_path):
    _, lines = source(tmp_path)
    result = grep(tmp_path, path="target.py", pattern="def needle_40", context=18)

    assert "匹配 1 处（扫描 1 个文件" in result
    assert "target.py:40\n" in result
    anchor = "\n".join(f"{n}: {lines[n - 1]}" for n in range(40, 52))
    assert anchor in result.replace("\r\n", "\n")
    assert returned_lines(result) == list(range(22, 59))
    assert "上下文已裁剪" not in result


def test_directory_context_is_still_capped_at_five_and_reports_requested_and_applied(tmp_path):
    source(tmp_path)
    result = grep(tmp_path, path=".", pattern="def needle_40", context=18)

    assert "requested_context=18, applied_context=5" in result
    assert "目录上限 5" in result
    assert "真实匹配的 file:line 调用 read_file" in result
    assert "offset/limit" in result
    assert "target.py:40\n" in result
    assert returned_lines(result) == list(range(35, 46))
    schema = GrepTool.parameters["properties"]["context"]
    assert schema["minimum"] == 0 and schema["maximum"] == 40
    assert "literal file max 40, directory max 5" in schema["description"]


def test_literal_file_context_above_forty_reports_its_actual_window(tmp_path):
    source(tmp_path, matches=(60,))
    result = grep(tmp_path, path="target.py", pattern="def needle_60", context=70)

    assert "requested_context=70, applied_context=40" in result
    assert "单文件上限 40" in result
    assert "read_file" in result and "target.py:60\n" in result
    assert returned_lines(result) == list(range(20, 101))


def test_nearby_context_is_merged_but_every_match_coordinate_and_count_remain(tmp_path):
    source(tmp_path, matches=(20, 23, 27, 70))
    result = grep(tmp_path, path="target.py", pattern="def needle_", context=4)

    assert "匹配 4 处（扫描 1 个文件" in result
    for line in (20, 23, 27, 70):
        assert f"target.py:{line}\n" in result
    shown = returned_lines(result)
    assert shown == list(range(16, 32)) + list(range(66, 75))
    assert len(shown) == len(set(shown))


def test_merged_context_does_not_reduce_match_budget_or_authorize_file_writes(tmp_path):
    path, _ = source(tmp_path, matches=(20, 23, 27))
    (tmp_path / "z.py").write_text("needle in another file", encoding="utf-8")
    observation = FsObservation()
    context = ToolContext(session_id="grep-context", workspace=tmp_path, fs_observation=observation)
    result = asyncio.run(GrepTool().execute({"path": ".", "pattern": "needle", "context": 5,
                                          "max_results": 2}, context))

    assert "匹配 2 处（扫描 1 个文件" in result
    assert "target.py:20\n" in result and "target.py:23\n" in result
    assert "target.py:27\n" not in result and "z.py:" not in result
    assert len(returned_lines(result)) == len(set(returned_lines(result)))
    assert observation.state(path) == "unseen"
    assert observation.check_write(path) is not None
