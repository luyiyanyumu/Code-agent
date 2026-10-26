"""Inline checks may consume source; their presence never permits source dumps."""

from pathlib import Path

import pytest

from code_agent.agent.task_progress import TaskProgress
from code_agent.agent.tool_effects import verification_paths


def progress_for(annotation):
    prompt = "给 a.py 添加中文注释" if annotation else "修复 a.py 并测试"
    progress = TaskProgress(Path("/repo"), prompt)
    assert progress.enabled
    assert progress.before_tool("read_file", {"path": "a.py"}) is None
    progress.observe("read_file", {"path": "a.py"}, "1\tx = 1")
    threshold = progress.ANNOTATION_WARN_TURNS if annotation else progress.MUTATION_FOCUS_TURNS
    for _ in range(threshold):
        progress.finish_turn()
    assert progress.phase == "implement"
    return progress


@pytest.mark.parametrize("annotation", [False, True], ids=["mutation", "annotation"])
@pytest.mark.parametrize("command", [
    'python -c "from pathlib import Path; assert Path(\'a.py\').read_text() == \'x = 1\'; print(\'passed\')"',
    'python -c "assert len(open(\'a.py\').read()) > 0"',
    'python -c "import ast; ast.parse(open(\'a.py\').read())"',
    'python -c "from pathlib import Path as P; compile(P(\'a.py\').read_bytes(), \'a.py\', \'exec\')"',
])
def test_inline_content_or_syntax_validation_can_run_without_counting_as_delivery(annotation, command):
    progress = progress_for(annotation)
    assert progress.before_tool("bash", {"command": command}) is None
    before = progress.turns_without_delivery
    progress.observe("bash", {"command": command}, "exit_code=0\npassed")
    progress.finish_turn()
    assert progress.turns_without_delivery == before + 1
    assert not progress.ready_to_finish


@pytest.mark.parametrize("annotation", [False, True], ids=["mutation", "annotation"])
@pytest.mark.parametrize("command", [
    'python -c "from pathlib import Path; print(Path(\'a.py\').read_text())"',
    'python -c "assert True; print(open(\'a.py\').read())"',
    'python -c "assert print(open(\'a.py\').read()) is None"',
    'python -c "assert True or open(\'a.py\').read() == \'x = 1\'"',
    'python -c "assert open(\'a.py\').read() == \'x = 1\'; print(open(\'a.py\').read())"',
    'python -c "assert open(\'a.py\').read() == \'x = 1\'" && cat a.py',
    'python -O -c "assert open(\'a.py\').read() == \'x = 1\'"',
    'python -c "from pathlib import Path; assert [p.read_text() for p in Path(\'.\').rglob(\'*\')] == []"',
    'python -c "from builtins import print as len; assert len(open(\'a.py\').read()) == 0"',
    'python -c "assert open(\'a\\x00.py\').read() == \'fixed\'"',
    'cat a.py && python -m pytest tests',
])
def test_source_reads_cannot_be_disguised_as_inline_validation(annotation, command):
    progress = progress_for(annotation)
    assert "read_file" in progress.before_tool("bash", {"command": command})


@pytest.mark.parametrize("result", ["exit_code=0\npassed", "exit_code=1\nAssertionError"])
def test_inline_receipts_require_process_success_and_cover_only_explicit_candidate_paths(tmp_path, result):
    command = 'python -c "assert open(\'a.py\').read() == \'fixed\'"'
    first, untouched, outside = tmp_path / "a.py", tmp_path / "b.py", tmp_path.parent / "outside.py"
    expected = (str(first),) if result.startswith("exit_code=0") else ()
    assert verification_paths(command, result, tmp_path, [str(first), str(untouched), str(outside)]) == expected
    assert verification_paths(command, result, tmp_path, [str(untouched)]) == ()


def test_inline_receipts_do_not_accept_outside_workspace_or_unexecuted_branches(tmp_path):
    command = 'python -c "assert open(\'../outside.py\').read() == \'fixed\'"'
    outside = tmp_path.parent / "outside.py"
    assert verification_paths(command, "exit_code=0", tmp_path, [str(outside)]) == ()
    command = 'python -c "assert (open(\'a.py\').read() if False else \'fixed\') == \'fixed\'"'
    assert verification_paths(command, "exit_code=0", tmp_path, ["a.py"]) == ()

