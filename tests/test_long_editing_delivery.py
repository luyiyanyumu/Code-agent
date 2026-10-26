"""Independent large-fixture checks and a real SessionRuntime delivery run."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from types import SimpleNamespace

import pytest

from code_agent.permissions.policy import Decision
from code_agent.models.events import Event, EventType
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin import ReadFileTool
from scripts.verify_long_editing_delivery import (
    FUNCTIONS_PER_MODULE,
    MODULES,
    TARGETS,
    DeliveryObserver,
    FixtureShellPolicy,
    comment_for,
    external_verify,
    function_name,
    fixture_script_command,
    make_fixture,
    module_source,
    run_probe,
)


NODE = pytest.mark.skipif(shutil.which("node") is None, reason="Independent verifier requires real Node.js")


def annotate(workspace, modules=range(MODULES)):
    for module in modules:
        path = TARGETS[module]
        source = module_source(module)
        for number in range(FUNCTIONS_PER_MODULE):
            declaration = f"export function {function_name(module, number)}("
            source = source.replace(declaration, comment_for(module, number) + declaration)
        (workspace / path).write_text(source, encoding="utf-8")


def observed_result(observer, name, arguments, result):
    observer.provider.calls += 1
    number = observer.provider.calls
    call = {"id": f"tc-{number}", "name": name, "arguments": arguments}
    observer.on_event(Event(seq=number * 2, type=EventType.tool_use, payload={"call": call}))
    observer.on_event(Event(seq=number * 2 + 1, type=EventType.tool_result,
                            payload={"call_id": call["id"], "name": name, "result": result}))


def observed_read(observer, path):
    try:
        result = asyncio.run(ReadFileTool().execute({"path": path}, ToolContext("observer", observer.workspace)))
    except ToolError as exc:
        result = f"[tool error] ToolError: {exc}"
    observed_result(observer, "read_file", {"path": path}, result)


def observe_real_node_verification(observer, paths):
    check = external_verify(observer.workspace, paths)
    assert check["exit_code"] == 0
    observed_result(observer, "bash", {"command": "node ../verify_long.mjs " + " ".join(paths)},
                    f"exit_code=0\nLONG_VERIFY {json.dumps(check['result'])}\n")


@NODE
def test_large_fixture_has_real_scale_dependencies_and_initial_missing_comments(tmp_path):
    workspace, manifest = make_fixture(tmp_path)
    assert len(TARGETS) >= 12 and MODULES * FUNCTIONS_PER_MODULE >= 120
    assert sum(len((workspace / path).read_text(encoding="utf-8").splitlines()) for path in TARGETS) >= 1500
    assert "precedingAggregate" in (workspace / TARGETS[-1]).read_text(encoding="utf-8")
    assert (workspace / "unrelated-project" / "autostart.mjs").is_file()
    assert (tmp_path / "other-project" / "module_00.mjs").is_file()
    assert len(manifest["functions"]) == MODULES

    check = external_verify(workspace, [TARGETS[0]])

    assert check["exit_code"] != 0 and not check["result"]["ok"]
    assert "Missing Chinese comment" in check["result"]["errors"][0]["message"]


@NODE
def test_independent_verifier_checks_all_120_functions_and_thousands_of_assertions(tmp_path):
    workspace, _manifest = make_fixture(tmp_path)
    annotate(workspace)
    # Exercise the native Windows newline shape without modifying baseline bytes.
    path = workspace / TARGETS[0]
    path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))

    check = external_verify(workspace)

    assert check["exit_code"] == 0 and check["result"]["ok"]
    assert check["result"]["functions"] == check["result"]["comments"] == 120
    assert check["result"]["assertions"] == 120 * 32
    assert check["result"]["token_unchanged"]
    assert set(check["result"]["verified"]) == set(TARGETS)


@NODE
@pytest.mark.parametrize("damage,expected_error", [
    ("runtime-token", "Runtime tokens changed"),
    ("missing-second-comment", "Missing Chinese comment"),
    ("missing-parameter", "Missing parameter/return field precision"),
    ("missing-chinese", "Missing Chinese comment"),
    ("missing-return", "Missing return description"),
    ("missing-empty-boundary", "Missing empty-input boundary"),
    ("missing-invalid-boundary", "Missing invalid-array boundary"),
    ("missing-finite-handling", "Missing finite-number handling"),
    ("missing-weight-description", "Missing weighting description"),
    ("missing-precision-boundary", "Missing precision boundary"),
    ("weight-three-cannot-replace-precision-boundary", "Missing precision boundary"),
])
def test_independent_verifier_rejects_semantic_edits_and_incomplete_comment_coverage(tmp_path, damage, expected_error):
    workspace, _manifest = make_fixture(tmp_path)
    annotate(workspace)
    path = workspace / TARGETS[0]
    source = path.read_text(encoding="utf-8")
    if damage == "runtime-token":
        source = source.replace("const weight = 1;", "const weight = 999;", 1)
    elif damage == "missing-second-comment":
        source = source.replace(comment_for(0, 1), "", 1)
    elif damage == "missing-parameter":
        source = source.replace("options.precision 限制", "小数位限制", 1)
    elif damage == "weight-three-cannot-replace-precision-boundary":
        original = comment_for(0, 2)
        assert "权重 3" in original
        source = source.replace(original, original.replace("0 至 3", "有效"), 1)
    else:
        original = comment_for(0, 0)
        replacements = {
            "missing-return": original.replace("@returns", "@result").replace("返回", "结果"),
            "missing-empty-boundary": original.replace("空数组", "无元素数组"),
            "missing-invalid-boundary": original.replace("抛出 TypeError", "由调用方处理"),
            "missing-finite-handling": original.replace("有限", "输入").replace("过滤", "处理"),
            "missing-weight-description": original.replace("权重", "常量").replace("加权", "数值"),
            "missing-precision-boundary": original.replace("0 至 3", "有效"),
            "missing-chinese": re.sub(r"[\u4e00-\u9fff]", "x", original),
        }
        source = source.replace(original, replacements[damage], 1)
    path.write_text(source, encoding="utf-8")

    check = external_verify(workspace, [TARGETS[0]])

    assert check["exit_code"] != 0 and not check["result"]["ok"]
    assert expected_error in check["result"]["errors"][0]["message"]


def test_probe_shell_policy_accepts_composed_scoped_validation_and_rejects_scope_escape(tmp_path):
    policy = FixtureShellPolicy()
    ctx = ToolContext("fixture", tmp_path)
    command = f"echo checking && node --check {TARGETS[0]} && node ../verify_long.mjs {TARGETS[0]} {TARGETS[1]}"
    assert policy.evaluate({"command": command}, ctx).decision == Decision.need_approval
    for command in (
        "node -e 'require(\"fs\").writeFileSync(\"outside\",\"bad\")'",
        "node ../other-project/module_00.mjs", "cat unrelated-project/autostart.mjs",
        "node ../verify_long.mjs src/module_00.mjs > changed.txt",
        "node ../verify_long.mjs ../../outside.mjs", "cat src/module_00.mjs | sh",
    ):
        assert policy.evaluate({"command": command}, ctx).decision == Decision.deny


def test_probe_shell_policy_accepts_explicit_batch_edit_scripts_under_real_bash_approval(tmp_path):
    policy = FixtureShellPolicy()
    ctx = ToolContext("fixture", tmp_path)
    command = fixture_script_command(TARGETS[:3])
    assert policy.evaluate({"command": command}, ctx).decision == Decision.need_approval
    assert policy.evaluate({"command": command.replace("node -e ", "node --eval ", 1)}, ctx).decision == Decision.need_approval
    rejected = [
        fixture_script_command(TARGETS[:4]),
        command.replace(TARGETS[0], "src/unknown.mjs"),
        command.replace(TARGETS[0], "../baseline/src/module_00.mjs"),
        command.replace(TARGETS[0], "unrelated-project/autostart.mjs"),
        command.replace('"node:fs"', '"node:child_process"'),
        command.replace("const next =", 'process.exit(0); const next ='),
        command.replace("const next =", 'fetch("https://example.invalid"); const next ='),
        command.replace("fs.readFileSync", "fs.readdirSync"),
        command.replace("fs.writeFileSync(file,", 'fs.writeFileSync("forbidden",'),
        command.replace("for (const file of files)", 'files.push("forbidden"); for (const file of files)'),
        command + " && echo should-not-be-combined",
        command.replace("const next =", 'const escaped = "$(whoami)"; const next ='),
    ]
    for item in rejected:
        assert policy.evaluate({"command": item}, ctx).decision == Decision.deny, item


@NODE
def test_observer_does_not_reopen_delivered_unchanged_files_on_reread(tmp_path):
    workspace, _manifest = make_fixture(tmp_path)
    observer = DeliveryObserver(workspace, SimpleNamespace(calls=0))
    for path in TARGETS[:3]:
        observed_read(observer, path)
    annotate(workspace, range(3))
    observed_result(observer, "edit_many", {"edits": [{"path": path} for path in TARGETS[:3]]}, "已修改三个文件")
    observe_real_node_verification(observer, TARGETS[:3])

    observed_read(observer, TARGETS[0])
    for path in TARGETS[3:6]:
        observed_read(observer, path)
    summary = observer.summary()

    assert not summary["batch_order_violations"], "A verified same-version reread is not a fourth batch member"
    assert summary["unverified_active_paths"] == list(TARGETS[3:6])
    assert summary["model_verified_paths"] == list(TARGETS[:3])


@NODE
def test_observer_reopens_a_previously_verified_file_after_real_version_change(tmp_path):
    workspace, _manifest = make_fixture(tmp_path)
    observer = DeliveryObserver(workspace, SimpleNamespace(calls=0))
    observed_read(observer, TARGETS[0])
    annotate(workspace, [0])
    observed_result(observer, "edit_file", {"path": TARGETS[0]}, "已添加注释")
    observe_real_node_verification(observer, TARGETS[:1])
    path = workspace / TARGETS[0]
    path.write_text(path.read_text(encoding="utf-8") + "\n// 新增说明，需要验证当前版本。\n", encoding="utf-8")
    observed_result(observer, "edit_file", {"path": TARGETS[0]}, "已更新说明")

    summary = observer.summary()

    assert summary["model_verified_paths"] == []
    assert summary["unverified_active_paths"] == [TARGETS[0]]


def test_observer_distinguishes_successful_scope_access_from_blocked_attempt(tmp_path):
    workspace, _manifest = make_fixture(tmp_path)
    observer = DeliveryObserver(workspace, SimpleNamespace(calls=0))
    # The real file tool refuses the outside verifier before returning its bytes.
    observed_read(observer, "../verify_long.mjs")
    blocked = observer.summary()
    assert blocked["out_of_scope_accesses"] == []
    assert len(blocked["blocked_attempts"]) == 1
    assert blocked["blocked_attempts"][0]["path"] == "../verify_long.mjs"

    # This file is readable inside the workspace, but outside this task's scope.
    observed_read(observer, "unrelated-project/autostart.mjs")
    actual = observer.summary()
    assert actual["out_of_scope_accesses"] == [
        {"turn": 2, "tool": "read_file", "path": "unrelated-project/autostart.mjs"},
    ]
    assert len(actual["blocked_attempts"]) == 1


@NODE
def test_offline_provider_delivers_large_task_through_real_session_runtime_and_production_tools(tmp_path, agent_home):
    summary = asyncio.run(run_probe(tmp_path, timeout=120))

    assert summary["passed"], summary
    assert summary["mode"] == "offline" and summary["target_functions"] == 120
    assert summary["initial_source_lines"] >= 1500
    assert summary["external_verification"]["result"]["assertions"] >= 300
    assert len(summary["production_registry_tools"]) >= 30
    assert {"read_file", "grep", "glob", "list_dir", "code_map", "edit_file", "edit_many", "bash"} <= set(
        summary["production_registry_tools"])
    assert summary["native_read_calls"] == 12 and summary["shell_calls"] == 5
    assert summary["first_write_turn"] == 2 and summary["turns"] == 34
    assert len(summary["successful_batch_verifications"]) == 4
    assert set(summary["model_verified_paths"]) == set(TARGETS)
    assert not summary["out_of_scope_changes"] and not summary["out_of_scope_accesses"]
    assert summary["independent_verifier_unchanged"]


@NODE
def test_real_session_runtime_supports_scoped_node_script_batches_and_independent_validation(tmp_path, agent_home):
    summary = asyncio.run(run_probe(tmp_path, timeout=120, scripted_edits=True))

    assert summary["passed"], summary
    assert summary["turns"] == 14 and summary["first_write_turn"] == 2
    assert summary["native_read_calls"] == 12 and summary["shell_calls"] == 9
    assert set(summary["changed_targets"]) == set(TARGETS)
    assert len(summary["successful_batch_verifications"]) == 4
    assert summary["external_verification"]["result"]["comments"] == 120
    assert summary["external_verification"]["result"]["assertions"] == 3840
    assert summary["independent_verifier_unchanged"]
    assert not summary["batch_order_violations"] and not summary["out_of_scope_accesses"]
