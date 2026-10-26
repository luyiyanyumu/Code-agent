"""Delivery hints reduce generation without replacing verification decisions."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from code_agent.agent.delivery_guidance import delivery_guidance
from code_agent.agent.task_progress import TaskProgress


@pytest.mark.parametrize("task", [None, SimpleNamespace(enabled=False, annotation=False),
                                 SimpleNamespace(enabled=True, annotation=True)])
def test_non_development_tasks_receive_no_extra_delivery_layer(task):
    assert delivery_guidance(task) == ""


def test_development_guidance_is_short_and_keeps_minimum_sufficient_regression_and_approval(tmp_path):
    task = TaskProgress(tmp_path, "修复 src/search.py 并新增必要回归测试")
    before = deepcopy(task.state())
    guidance = delivery_guidance(task)
    assert 0 < len(guidance) <= 500
    assert "覆盖缺口" in guidance and "用户明确测试要求" in guidance
    assert "不复制整套现成验收" in guidance and "参数化" in guidance
    assert "最小必要增量" in guidance and "不另写只重复现成验收的校验器" in guidance
    assert "优先复用已有fixture/helper" in guidance
    assert "只为当前新增覆盖生成必要代码" in guidance
    assert "不另造泛化测试框架或重复整个参数矩阵" in guidance
    assert "先完成同一需求的本批实现与必要回归" in guidance
    assert "独立编辑和新建必要测试可同轮提交，验证置于写入之后" in guidance
    assert "依赖未返回读结果的编辑留到下一轮" in guidance
    assert "优先局部精确编辑" in guidance and "不重生成无关源码" in guidance
    assert "purpose/effects" in guidance and "basis确有必要才写" in guidance
    assert "审批必填字段、权限检查和日志" in guidance
    assert task.state() == before
    assert not task.ready_to_finish


def test_successful_focused_check_cannot_be_promoted_to_complete_acceptance(tmp_path):
    task = TaskProgress(tmp_path, "修复 src/search.py 的搜索范围错误")
    task.observe("read_file", {"path": "src/search.py"}, "def search():\n    return []\n")
    task.observe("edit_file", {"path": "src/search.py"}, "changed", changed_paths=["src/search.py"])
    # This ledger receipt deliberately proves only a focused check. The hint
    # cannot turn the boolean into a claim that all user requirements passed.
    task.observe("bash", {"command": "python -m pytest tests/test_one_case.py"},
                 "exit_code=0", verified_paths=["src/search.py"])
    assert task.ready_to_finish
    before = deepcopy(task.state())
    guidance = delivery_guidance(task)
    assert "定点测试通过只证明已运行范围" in guidance
    assert "只有当前版本覆盖需求且相关完整验收真实通过，才交付完成结果" in guidance
    assert "先核对其覆盖范围" in guidance and "足够则直接交付" in guidance
    assert "不再新增同构测试、整理无关imports或重复验证" in guidance
    assert "确有需求缺口或新错误才继续" in guidance
    assert "已通过完整验收" not in guidance
    assert task.state() == before


def test_real_failure_guidance_keeps_recovery_and_user_scope(tmp_path):
    task = TaskProgress(tmp_path, "只修改 src/search.py 修复搜索错误")
    task.observe("read_file", {"path": "src/search.py"}, "def search():\n    return []\n")
    task.observe("edit_file", {"path": "src/search.py"}, "changed", changed_paths=["src/search.py"])
    task.record_validation_failure(["src/search.py"], "AssertionError: wrong search range")
    before = deepcopy(task.state())
    guidance = delivery_guidance(task)
    assert "直接依据现有诊断修复当前需求的根因" in guidance
    assert "不再复跑已知失败基线" in guidance
    assert "独立编辑和新建必要测试可同轮提交，验证置于写入之后" in guidance
    assert "受影响的完整验收" in guidance and "不顺手扩大范围" in guidance
    assert "足够则直接交付" not in guidance
    assert task.state() == before and not task.ready_to_finish


@pytest.mark.parametrize("phase", ["investigate", "implement", "recover"])
def test_real_520_character_instruction_budget_preserves_all_unfinished_phase_guidance(phase):
    task = SimpleNamespace(enabled=True, annotation=False, phase=phase, ready_to_finish=False)
    guidance = delivery_guidance(task, max_chars=520)
    assert len(guidance) <= 520
    for condition in ("只有当前版本覆盖需求且相关完整验收真实通过", "独立编辑和新建必要测试可同轮提交",
                      "验证置于写入之后", "依赖未返回读结果的编辑留到下一轮", "最小必要增量",
                      "遵守用户明确测试要求", "不复制整套现成验收", "不另写只重复现成验收的校验器",
                      "优先复用已有fixture/helper", "只为当前新增覆盖生成必要代码", "不另造泛化测试框架或重复整个参数矩阵",
                      "优先局部精确编辑", "不重生成无关源码", "保留需求范围", "purpose/effects",
                      "basis确有必要才写", "审批必填字段、权限检查和日志"):
        assert condition in guidance
    if phase == "recover":
        assert "已有初始失败复现后，直接依据现有诊断修复" in guidance
        assert "不再复跑已知失败基线" in guidance
    else:
        assert "先完成同一需求的本批实现与必要回归" in guidance


def test_successful_delivery_does_not_prompt_another_implementation_batch():
    task = SimpleNamespace(enabled=True, annotation=False, phase="implement", ready_to_finish=True)
    guidance = delivery_guidance(task, max_chars=520)
    assert "足够则直接交付" in guidance
    assert "独立编辑和新建必要测试可同轮提交" not in guidance


@pytest.mark.parametrize("limit", [0, 10, 60, 100, 150, 250, 400, 720])
def test_small_budgets_keep_whole_acceptance_condition_or_omit_the_hint(limit):
    task = SimpleNamespace(enabled=True, annotation=False, phase="implement", ready_to_finish=True)
    guidance = delivery_guidance(task, max_chars=limit)
    assert len(guidance) <= limit
    if guidance:
        assert "只有当前版本覆盖需求且相关完整验收真实通过，才交付完成结果。" in guidance
        assert guidance.endswith("。")
        assert all(line.endswith("。") for line in guidance.splitlines()[1:])


def test_guidance_has_no_io_or_receipt_side_effects(tmp_path, monkeypatch):
    from pathlib import Path

    task = TaskProgress(tmp_path, "实现 src/export.py 的导出功能")

    def forbidden(*args, **kwargs):
        raise AssertionError("delivery guidance must not read or modify project files")

    for method in ("open", "read_text", "read_bytes", "write_text", "write_bytes"):
        monkeypatch.setattr(Path, method, forbidden)
    assert delivery_guidance(task)
    assert not task.state()["changed"] and not task.state()["verified"]
