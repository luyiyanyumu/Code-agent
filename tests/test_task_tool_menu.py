"""Small development menus advertise useful, currently executable capabilities."""

from copy import deepcopy
from pathlib import Path

import pytest

from code_agent.agent.task_progress import TaskProgress
from code_agent.models.approval import APPROVAL_SCHEMA


CORE = {"read_file", "write_file", "edit_file", "edit_many", "grep", "glob", "list_dir", "bash"}
AUX = {"ask_user_question", "load_workspace_dependencies", "job_list", "job_output", "job_kill"}
OTHER = {"propose_edit", "acceptance_set", "acceptance_check", "contract_check", "feature_brief",
         "skill_install", "skill_list", "skill_search", "load_skill", "load_expert", "present", "deliver_file",
         "code_map", "code_search", "code_graph", "read_image", "bash_persistent", "web_search"}
COORDINATION = {"team_list", "delegate_agent", "send_agent_message", "followup_agent",
                "interrupt_agent", "wait_agents", "stop_agent", "read_agent_execution"}


def schemas(names=CORE | AUX | OTHER):
    return [{"name": name, "description": "available tool", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "_approval": deepcopy(APPROVAL_SCHEMA)},
        "required": ["_approval"],
    }} for name in sorted(names)]


def names(entries):
    return {(entry.get("function") or entry)["name"] for entry in entries}


@pytest.mark.parametrize("prompt,context", [
    ("修复 src/search.py 的路径错误", None),
    ("实现 src/search.py 的全词搜索并新增 tests/test_search.py", None),
    ("Fix src/search.py and add regression tests in tests/test_search.py", None),
    ("实现 src/new.py 的导出功能", None),
    ("只修改 src/search.py，实现全词匹配；无须通读整个项目。", None),
    ("修复当前文件的登录错误", {"path": "src/login.py"}),
])
def test_concrete_small_development_requests_receive_focused_native_menu(prompt, context):
    task = TaskProgress(Path("/repo"), prompt, context)
    original = schemas()
    before = deepcopy(original)
    assert task.focused_mutation_tools
    adapted = task.tool_schemas(original)
    assert names(adapted) == CORE | AUX
    assert original == before
    for schema in adapted:
        original_schema = next(item for item in original if item["name"] == schema["name"])
        assert schema["parameters"] == original_schema["parameters"]
    assert len(adapted) < len(original) / 2


def test_focused_menu_supports_wrapped_schemas_and_only_existing_role_tools():
    original = [{"type": "function", "function": schema} for schema in schemas({"read_file", "edit_file", "present"})]
    task = TaskProgress("/repo", "修复 src/search.py 的错误")
    adapted = task.tool_schemas(original)
    assert names(adapted) == {"read_file", "edit_file"}
    assert all(entry["type"] == "function" for entry in adapted)
    assert "bash" not in names(adapted)  # No permission or role capability is added.


def test_custom_role_keeps_its_only_native_editor_and_shell_alternatives():
    task = TaskProgress("/repo", "修复 src/search.py 的错误")
    original = schemas({"read_file", "apply_patch", "bash_persistent", "present"})
    assert names(task.tool_schemas(original)) == {"read_file", "apply_patch", "bash_persistent"}
    proposals = schemas({"read_file", "propose_edit", "code_map"})
    assert names(task.tool_schemas(proposals)) == {"read_file", "propose_edit"}


@pytest.mark.parametrize("prompt", [
    "修复整个项目的登录错误", "修复登录错误", "解释 src/search.py 的逻辑", "给 src/search.py 添加中文注释",
    "修复 a.py b.py c.py d.py 的错误", "修复 src/search.py 并使用技能分析", "修复 src/search.py 并安装插件",
    "修复 src/search.py 并展示运行结果", "修复 src/search.py 并附上截图", "修复 src/search.py 并联网查文档",
    "修复 src/search.py，搜索官方API文档", "修复 src/search.py 并委派子智能体检查", "修复 src/search.py 并行处理其他模块",
    "修复 src/search.py 并跨模块重构", "修复 src/search.py，在持久shell执行验证", "修复 src/search.py 并提交代码",
    "Fix src/search.py and browse official docs", "Fix src/search.py and delegate testing to another agent",
    "Fix src/search.py and use a skill", "Fix src/search.py and preview the interface", "Fix src/search.py and create a pull request",
])
def test_wider_or_explicit_capability_requests_keep_the_available_menu(prompt):
    task = TaskProgress("/repo", prompt)
    assert not task.focused_mutation_tools
    # Annotation has its own existing edit schema discipline; do not replace it.
    if not task.annotation:
        assert names(task.tool_schemas(schemas())) == CORE | AUX | OTHER


def test_negated_extra_capabilities_do_not_force_unnecessary_menu():
    task = TaskProgress("/repo", "修复 src/search.py，不要委派子智能体，不要使用技能")
    assert task.focused_mutation_tools
    assert names(task.tool_schemas(schemas())) == CORE | AUX


def test_focused_lead_retains_existing_coordination_and_background_capabilities():
    task = TaskProgress("/repo", "修复 src/search.py 的错误")
    adapted = task.tool_schemas(schemas(CORE | AUX | OTHER | COORDINATION))
    assert task.focused_mutation_tools
    assert names(adapted) == CORE | AUX | COORDINATION


def test_focused_menu_tracks_actual_targeted_investigation_and_failure_recovery_allowances():
    task = TaskProgress("/repo", "修复 src/search.py 的错误")
    task.observe("read_file", {"path": "src/search.py"}, "def sample():\n    return 1\n")
    task.observe("edit_file", {"path": "src/search.py"}, "changed", changed_paths=["src/search.py"])
    assert task.phase == "implement"
    for number in range(2):
        assert {"grep", "glob", "list_dir"} <= names(task.tool_schemas(schemas()))
        args = {"path": "src", "pattern": f"symbol_{number}"}
        assert task.before_tool("grep", args) is None
        task.observe("grep", args, "src/search.py:1\n1: def sample():")
    assert not {"grep", "glob", "list_dir"} & names(task.tool_schemas(schemas()))
    assert task.before_tool("grep", {"path": "src", "pattern": "another"})

    task.record_validation_failure(["src/search.py"], "AssertionError: wrong return value")
    assert task.phase == "recover"
    for number in range(3):
        assert "grep" in names(task.tool_schemas(schemas()))
        args = {"path": "src", "pattern": f"failure_symbol_{number}"}
        assert task.before_tool("grep", args) is None
        task.observe("grep", args, "src/search.py:1\n1: def sample():")
    assert not {"grep", "glob", "list_dir"} & names(task.tool_schemas(schemas()))
    task.record_validation_failure(["src/search.py"], "different text, same unchanged version")
    assert not {"grep", "glob", "list_dir"} & names(task.tool_schemas(schemas()))
    assert {"read_file", "edit_file", "edit_many", "write_file", "bash"} <= names(task.tool_schemas(schemas()))

