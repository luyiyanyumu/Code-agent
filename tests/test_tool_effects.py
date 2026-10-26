from pathlib import Path

import pytest

from code_agent.agent.tool_effects import annotation_satisfied, changed_files, file_versions, verification_paths


@pytest.mark.parametrize("command,expected", [
    ("node --check a.mjs", ["a.mjs"]),
    ("node --check a.mjs && node --check b.mjs", ["a.mjs", "b.mjs"]),
    ("echo checking && node --check a.mjs && echo done", ["a.mjs"]),
    ("node ../verify_long.mjs a.mjs", ["a.mjs"]),
    ("node ../verify_long.mjs --all", ["a.mjs", "b.mjs"]),
    ("python -m py_compile a.mjs", ["a.mjs"]),
    ("python -m pytest tests/test_code.py", ["a.mjs", "b.mjs"]),
    ("npm run build", ["a.mjs", "b.mjs"]),
    ("echo passed", []),
    ("node writer.mjs", []),
    ("node -e 'console.log(0)'", []),
    ("node --check other.mjs", []),
    ("node --check a.mjs; echo passed", []),
    ("node --check a.mjs || true", []),
    ("node --check a.mjs > receipt.txt", []),
    ("node --check $(echo a.mjs)", []),
    ("cd .. && node --check a.mjs", []),
])
def test_only_real_check_commands_receive_path_receipts(tmp_path, command, expected):
    candidates = [str(tmp_path / path) for path in ("a.mjs", "b.mjs")]
    result = verification_paths(command, "exit_code=0\n(无输出)", tmp_path, candidates)
    assert result == tuple(str(tmp_path / path) for path in expected)
    assert verification_paths(command, "exit_code=1\nfailed", tmp_path, candidates) == ()


def test_actual_file_changes_and_missing_file_creation_are_detected(tmp_path):
    (tmp_path / "a.mjs").write_text("original", encoding="utf-8")
    before = file_versions(tmp_path, ["a.mjs", "created.mjs", "../private.mjs"])
    assert str(tmp_path.parent / "private.mjs") not in before
    assert changed_files(before, file_versions(tmp_path, before)) == ()
    (tmp_path / "a.mjs").write_text("changed", encoding="utf-8")
    (tmp_path / "created.mjs").write_text("new", encoding="utf-8")
    assert set(changed_files(before, file_versions(tmp_path, before))) == {str(tmp_path / "a.mjs"), str(tmp_path / "created.mjs")}


def test_subproject_validation_uses_literal_cwd_without_crediting_other_subprojects(tmp_path):
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "backend").mkdir()
    front = str(tmp_path / "frontend" / "src" / "view.mjs")
    back = str(tmp_path / "backend" / "api.py")
    candidates = [front, back]
    assert verification_paths("cd frontend && npm test", "exit_code=0\npassed", tmp_path, candidates) == (front,)
    assert verification_paths("cd frontend && node --check src/view.mjs", "exit_code=0\n", tmp_path, candidates) == (front,)
    assert verification_paths("cd frontend && cd .. && python -m pytest", "exit_code=0\n", tmp_path, candidates) == (front, back)
    assert verification_paths("cd frontend && node --check view.mjs", "exit_code=0\n", tmp_path, candidates) == ()
    assert verification_paths("cd ../outside && npm test", "exit_code=0\n", tmp_path, candidates) == ()
    assert verification_paths("cd frontend && npm test || true", "exit_code=0\n", tmp_path, candidates) == ()


def test_validation_recognizes_the_real_venv_interpreter_path(tmp_path):
    source = str(tmp_path / "search.py")
    interpreter = str(tmp_path / "env with spaces" / "Scripts" / "python.exe").replace("\\", "/")
    command = f'"{interpreter}" -m pytest tests/test_search.py'
    assert verification_paths(command, "exit_code=0\npassed", tmp_path, [source]) == (source,)
    assert verification_paths(command, "exit_code=1\nfailed", tmp_path, [source]) == ()


@pytest.mark.parametrize("source,expected", [
    ("/** 中文说明 */\nexport function one() { return 1; }\n", True),
    ("// 中文说明\nexport function one() { return 1; }\n", True),
    ("// 中文说明\nexport function one() { return 1; }\nexport function two() { return 2; }\n", False),
    ("// English only\nexport function one() { return 1; }\n", False),
    ("const value = '中文';\nexport function one() { return 1; }\n", False),
    ("const one = () => 1;\n", False),
])
def test_noop_requires_chinese_comments_for_every_recognized_function(tmp_path, source, expected):
    (tmp_path / "a.mjs").write_text(source, encoding="utf-8")
    assert annotation_satisfied(tmp_path, "a.mjs") is expected
