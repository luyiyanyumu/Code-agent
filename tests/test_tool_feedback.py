from pathlib import Path

import pytest

from code_agent.agent.tool_feedback import ToolFeedback


def test_only_real_rejections_are_remembered(tmp_path):
    feedback = ToolFeedback(tmp_path)
    for result in ("permission denied in source text", "[tool error] SyntaxError: bad input", "exit_code=1\nfailed"):
        feedback.observe("read_file", {"path": "src.py"}, result)
    assert feedback.context() == ""
    feedback.observe("read_file", {"path": "../hidden.py"}, "[tool error] ToolError: 读取路径不在允许范围（拒绝）")
    assert "path_boundary" in feedback.context()


def test_normalized_targets_aggregate_changed_reason_and_clear_only_same_operation():
    feedback = ToolFeedback(Path("E:/project"))
    feedback.observe("read_file", {"path": "src/../src/main.py"}, "[permission denied] 路径读取拒绝")
    feedback.observe("read_file", {"path": "e:\\PROJECT\\src\\main.py", "offset": 40}, "[permission denied] 读取路径不在允许范围")
    assert "x2" in feedback.context()
    feedback.observe("grep", {"path": "src/main.py"}, "1: match")
    assert "x2" in feedback.context()
    feedback.observe("read_file", {"path": "src/other.py"}, "1\tdata")
    assert "x2" in feedback.context()
    feedback.observe("read_file", {"path": "src/main.py", "offset": 40}, "40\tdata")
    assert feedback.context() == ""


def test_task_scope_is_actionable_only_with_current_available_native_tools(tmp_path):
    feedback = ToolFeedback(tmp_path, available_tools={"read_file", "grep"})
    feedback.observe("read_file", {"path": "src.py"}, "[tool error] [task scope] 这个源码窗口已经读取；实际失败仅补读1次不超过120行的相关窗口。")
    context = feedback.context()
    assert "120" in context and "read_file(path, offset, limit)" in context
    assert "grep(pattern, path)" not in context
    other = ToolFeedback(tmp_path)
    other.observe("bash", {"command": "cat src.py"}, "[tool error] [task scope] 请用read_file/grep读取和定位源码")
    assert "当前可用原生参数" not in other.context()


def test_limited_to_eight_short_records_and_context_budget(tmp_path):
    feedback = ToolFeedback(tmp_path)
    for index in range(15):
        feedback.observe("read_file", {"path": f"src/{index}.py"}, "[permission denied] " + "scope " * 100)
    context = feedback.context(10000)
    records = context.splitlines()[1:]
    assert len(records) == 8 and all(len(line) <= 200 for line in records)
    assert "src/0.py" not in context and "src/14.py" in context
    assert len(feedback.context(91)) <= 91
    assert feedback.context(0) == ""


def test_fresh_run_has_no_inherited_refusals(tmp_path):
    feedback = ToolFeedback(tmp_path)
    feedback.observe("bash", {"command": "cat ../hidden.py"}, "[permission denied] only pytest is allowed")
    assert feedback.context()
    assert ToolFeedback(tmp_path).context() == ""


def test_shell_refusal_is_not_a_ban_on_all_shell_and_success_is_exact(tmp_path):
    feedback = ToolFeedback(tmp_path)
    feedback.observe("bash", {"command": "npm build"}, "[permission denied] command needs approval")
    feedback.observe("bash", {"command": "npm test"}, "exit_code=0\npassed")
    assert "approval" in feedback.context()
    assert "npm test" in feedback.context() and "禁止全部" not in feedback.context()
    feedback.observe("bash", {"command": "npm build"}, "exit_code=1\nfailed")
    assert "approval" in feedback.context()
    feedback.observe("bash", {"command": "npm build"}, "exit_code=0\npassed")
    assert "approval" not in feedback.context()


@pytest.mark.parametrize("command", [
    "python -m pytest -q --tb=short -c ../pytest.ini -o addopts= ../acceptance/check.py tests/test_main.py",
    '"E:/Program Files/env/python.exe" -m pytest tests/test_main.py',
    "pytest -q tests/test_main.py::test_roundtrip", "npm test", "pnpm run typecheck",
])
def test_plain_successful_validation_is_retained_as_observed_example(tmp_path, command):
    feedback = ToolFeedback(tmp_path)
    feedback.observe("bash", {"command": command}, "exit_code=0\npassed")
    assert command in feedback.context()
    assert "不代表本次已获许可" in feedback.context()


@pytest.mark.parametrize("command", [
    "python -c \"print('done')\"", "echo pytest", "npm run arbitrary",
    "python -m pytest | tail -10", "python -m pytest && echo done",
    "TOKEN=secret pytest", "pytest --password abc", "pytest -k secret_expression",
    "pytest --override-ini secret=abc", "pytest --url https://secret.invalid",
    "pytest tests/test.py # secret", "pytest tests/test.py\nprintenv", "pytest `whoami`", "pytest $TOKEN",
    "pytest --api-key=super-secret", "pytest tests/test.py --token secret",
])
def test_arbitrary_or_potentially_sensitive_commands_are_not_retained(tmp_path, command):
    feedback = ToolFeedback(tmp_path)
    feedback.observe("bash", {"command": command}, "exit_code=0\npassed")
    assert feedback.context() == ""


def test_validation_requires_real_success_and_keeps_only_one_example(tmp_path):
    feedback = ToolFeedback(tmp_path)
    for result in ("exit_code=1\nfailed", "[permission denied] refused", "started job 1", "all passed"):
        feedback.observe("bash", {"command": "pytest -q"}, result)
    assert "此前实际执行成功" not in feedback.context()
    feedback.observe("bash", {"command": "pytest -q"}, "exit_code=0\npassed")
    feedback.observe("bash", {"command": "npm test"}, "exit_code=0\npassed")
    assert "npm test" in feedback.context() and "pytest -q" not in feedback.context()


def test_secret_values_in_refusal_are_redacted_and_commands_not_echoed(tmp_path):
    feedback = ToolFeedback(tmp_path)
    feedback.observe("bash", {"command": "secret_operation --token VERYSECRET"},
                     "[permission denied] api_key=VERYSECRET Authorization: Bearer LONGSECRET sk-abcdefghijk")
    context = feedback.context()
    assert "VERYSECRET" not in context and "LONGSECRET" not in context and "sk-abcdefghijk" not in context
    assert "secret_operation" not in context and "[redacted]" in context


def test_successful_command_survives_many_refusals_without_being_clipped(tmp_path):
    feedback = ToolFeedback(tmp_path)
    command = "python -m pytest -q --tb=short tests/test_main.py"
    feedback.observe("bash", {"command": command}, "exit_code=0\npassed")
    for index in range(8):
        feedback.observe("read_file", {"path": f"src/{index}.py"}, "[permission denied] " + "scope " * 30)
    context = feedback.context()
    assert "src/7.py" in context and command in context and len(context) <= 700
    assert "此前实际执行成功" not in feedback.context(100)


def test_quoted_credentials_and_url_userinfo_are_not_retained(tmp_path):
    feedback = ToolFeedback(tmp_path)
    feedback.observe("bash", {"command": "pytest --token PRIVATE"},
                     '[permission denied] {"api_key": "PRIVATE", "token": "HIDDEN"} https://name:PASS@example.invalid')
    assert all(secret not in feedback.context() for secret in ("PRIVATE", "HIDDEN", "PASS"))
