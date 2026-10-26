import pytest

from code_agent.agent.verification_plan import VerificationPlan, parse_validation_command


@pytest.mark.parametrize("command,full", [
    ("python -m pytest -q", True),
    ("pytest tests", True),
    ("pytest tests/original.py tests/new.py", True),
    ("pytest tests/new.py", False),
    ("pytest tests/new.py::test_bug", False),
    ("pytest -k bug tests/original.py tests/new.py", False),
    ("cd service && python -m pytest -q", True),
    ('"E:/Program Files/env/python.exe" -m pytest -q --tb=short -c ../pytest.ini -o addopts= ../acceptance/check.py tests/new.py', True),
    ("npm test", True), ("pnpm run test", True), ("npm run test:unit", False),
    ("cargo test", True), ("cargo test login", False),
    ("go test ./...", True), ("go test -run Bug ./...", False),
    ("dotnet test", True), ("mvn test", True), ("gradlew test", True),
])
def test_common_validation_commands_distinguish_focused_and_broad_scope(command, full):
    parsed = parse_validation_command(command, "/repo")
    assert parsed is not None and parsed.full is full


@pytest.mark.parametrize("command", [
    "echo all passed", "python -c 'print(42)'", "pytest --collect-only", "npm run deploy",
    "pytest | tail -10", "pytest && echo passed", "TOKEN=secret pytest", "pytest $FLAGS",
    "pytest `whoami`", "pytest\nwhoami", "pytest --unknown-plugin-option secret",
])
def test_unknown_shell_and_collection_are_not_validation_receipts(command):
    assert parse_validation_command(command, "/repo") is None


@pytest.mark.parametrize("prompt,expected", [
    ("修复 a.py 并新增 tests/new.py。tests/original.py 只读，运行 pytest tests/original.py。", {"/repo/tests/new.py"}),
    ("Only edit a.py and create tests/new.py. Existing tests/original.py stays read-only.", {"/repo/tests/new.py"}),
    ("新增 `tests/new test.py` 和 `tests/component.v2.test.ts`，参考 tests/original.py。", {"/repo/tests/new test.py", "/repo/tests/component.v2.test.ts"}),
])
def test_requested_new_tests_exclude_later_reference_and_runner_paths(prompt, expected):
    plan = VerificationPlan("/repo", prompt)
    assert set(plan.pending_tests) == expected
    plan.record_changes(["a.py"])
    assert not plan.ready_to_finish
    plan.record_changes([path for path in expected])
    assert not plan.pending_tests


def test_abstract_regression_test_request_needs_a_real_test_change():
    plan = VerificationPlan("/repo", "修复登录错误并补齐回归测试。")
    plan.record_changes(["src/auth.py", "README.md"])
    assert plan.pending_tests
    plan.record_changes(["tests/test_auth.py"])
    assert not plan.pending_tests


@pytest.mark.parametrize("prompt", ["修复 a.py，不要新增回归测试。", "Fix a.py; do not add tests.", "修改 a.py，不需要全量测试。"])
def test_negated_test_or_full_request_does_not_create_an_obligation(prompt):
    plan = VerificationPlan("/repo", prompt)
    assert not plan.tests_requested and not plan.requires_full_validation


def test_first_reproduction_allowed_but_full_run_waits_for_missing_requested_tests():
    plan = VerificationPlan("/repo", "修复 a.py 并新增 tests/new.py，最后完整验收。")
    full = "pytest tests/original.py tests/new.py"
    assert plan.before_validation(full) is None
    plan.record_validation("pytest tests/original.py", "exit_code=1\nfailed")
    assert plan.before_validation(full)
    assert plan.before_validation("pytest tests/original.py::test_bug") is None
    assert plan.before_validation(full, explicit_full=True) is None
    plan.record_changes(["a.py", "tests/new.py"])
    assert plan.before_validation(full) is None


def test_full_run_after_implementation_is_blocked_even_without_a_prior_baseline():
    plan = VerificationPlan("/repo", "修复 a.py 并新增 tests/new.py。")
    plan.record_changes(["a.py"])
    assert plan.before_validation("pytest")
    assert plan.before_validation("go test ./...")
    assert plan.before_validation("pytest tests/original.py") is None
    assert plan.before_validation("unknown shell") is None


def test_final_scope_requires_all_explicit_suites_no_filters_and_real_checked_paths():
    plan = VerificationPlan("/repo", "修复 a.py 并新增 tests/new.py。\n完整命令：pytest ../acceptance/check.py tests/original.py tests/new.py\n")
    plan.record_changes(["a.py", "tests/new.py"])
    assert not plan.ready_to_finish
    assert not plan.record_validation("pytest tests/new.py", "exit_code=0\npassed", verified_paths=["a.py", "tests/new.py"])
    assert not plan.record_validation("pytest", "exit_code=0\npassed", verified_paths=["a.py", "tests/new.py"])
    full = "pytest ../acceptance/check.py tests/original.py tests/new.py"
    assert not plan.record_validation(full + " -k bug", "exit_code=0\npassed", verified_paths=["a.py"])
    assert not plan.record_validation(full, "all passed", verified_paths=["a.py"])
    assert not plan.record_validation(full, "exit_code=0\npassed")
    assert not plan.ready_to_finish
    assert plan.record_validation(full, "exit_code=0\npassed", verified_paths=["a.py", "tests/new.py"])
    assert plan.ready_to_finish
    assert "真实完整范围验证通过" in plan.note()
    # Any actual source/test/config/dependency change invalidates the receipt.
    for changed in ("settings.toml", "requirements.txt", "tests/new.py", "src/helper.py"):
        plan.record_changes([changed])
        assert not plan.ready_to_finish
        assert plan.record_validation(full, "exit_code=0\npassed", verified_paths=["a.py", "tests/new.py"])


def test_explicit_single_file_final_scope_is_not_confused_with_a_node_filter():
    plan = VerificationPlan("/repo", "修复 a.py，完整命令：pytest tests/test_all.py")
    assert plan.record_validation("pytest tests/test_all.py", "exit_code=0\npassed", verified_paths=["a.py"])
    assert plan.ready_to_finish
    assert not plan.record_validation("pytest tests/test_all.py::test_one", "exit_code=1\nfailed", verified_paths=["a.py"])
    assert not plan.ready_to_finish


@pytest.mark.parametrize("scenario", ["combined", "bug"])
def test_real_project_prompt_preserves_exact_requested_new_test_and_final_scope(tmp_path, scenario):
    # Static production-probe prompts only: no model/provider or test execution.
    from scripts.verify_project_development_delivery import pytest_command, scenario_targets, scenario_test, task_prompt

    prompt = task_prompt(scenario)
    plan = VerificationPlan(tmp_path, prompt)
    expected_test = str(tmp_path / scenario_test(scenario)).replace("\\", "/").casefold()
    assert plan.required_test_paths == (expected_test,)
    final = pytest_command(scenario=scenario)
    expected = parse_validation_command(final, tmp_path)
    assert expected is not None and plan.required_final_scope == expected.scope
    assert len(plan.required_final_scope) == 3
    original = pytest_command(include_new=False, scenario=scenario)
    plan.record_changes([scenario_targets(scenario)[0]])
    assert plan.before_validation(original)
    plan.record_changes([scenario_test(scenario)])
    assert not plan.record_validation(original, "exit_code=0\npassed", verified_paths=scenario_targets(scenario))
    assert not plan.ready_to_finish
    assert plan.record_validation(final, "exit_code=0\npassed", verified_paths=scenario_targets(scenario))
    assert plan.ready_to_finish


@pytest.mark.parametrize("declared", [
    "完整命令：\npython -m pytest tests/original.py tests/new.py",
    "完整交付验证命令：\n```powershell\npython -m pytest tests/original.py tests/new.py\n```",
    "Final validation command:\n```sh\npython -m pytest tests/original.py tests/new.py\n```",
])
def test_final_command_on_its_own_line_or_in_a_fence_has_exact_scope(declared):
    plan = VerificationPlan("/repo", "新增 tests/new.py。\n" + declared)
    assert set(plan.required_final_scope) == {"/repo/tests/original.py", "/repo/tests/new.py"}


def test_unstated_final_command_still_cannot_omit_the_requested_new_test():
    plan = VerificationPlan("/repo", "修复 a.py 并新增 tests/new.py，补回归测试并完整验收。")
    plan.record_changes(["a.py", "tests/new.py"])
    assert not plan.record_validation("pytest ../acceptance/check.py tests/original.py", "exit_code=0\n67 passed",
                                      verified_paths=["a.py", "tests/new.py"])
    assert not plan.ready_to_finish
    assert plan.record_validation("pytest tests/original.py tests/new.py", "exit_code=0\n71 passed",
                                  verified_paths=["a.py", "tests/new.py"])
    assert plan.ready_to_finish


@pytest.mark.parametrize("workspace", ["/tmp/test_feature_and_bug_fix_creat0/workspace", "C:/Temp/test_feature_and_bug_fix_creat0/workspace"])
def test_test_named_temporary_parent_does_not_make_production_and_config_files_tests(workspace):
    plan = VerificationPlan(workspace, "修复价格计算并新增回归测试。")
    plan.record_changes(["service/pricing.py", "service/quote.py", "service/settings.toml"])
    assert plan.pending_tests
    plan.record_changes(["service/tests/test_quote.py"])
    assert not plan.pending_tests
    full = "cd service && python -m pytest tests/test_pricing.py tests/test_quote.py"
    assert plan.record_validation(full, "exit_code=0\n2 passed", verified_paths=["service/pricing.py", "service/quote.py", "service/tests/test_quote.py"])
    assert plan.ready_to_finish


def test_absolute_explicit_production_path_inside_test_named_parent_is_not_a_test_obligation():
    root = "/tmp/test_fixture/project"
    plan = VerificationPlan(root, "新建 `/tmp/test_fixture/project/quote.py` 并新增回归测试。")
    assert not plan.required_test_paths
    plan.record_changes([root + "/quote.py"])
    assert plan.pending_tests
    plan.record_changes([root + "/tests/test_quote.py"])
    assert not plan.pending_tests


@pytest.mark.parametrize("command", ["mvn -q test", "mvn --batch-mode --quiet test", "./mvnw -q test", "mvn test -q"])
def test_maven_output_flags_do_not_hide_a_real_complete_test_command(command):
    parsed = parse_validation_command(command, "/repo")
    assert parsed is not None and parsed.full and not parsed.filtered
    plan = VerificationPlan("/repo", "修复价格计算，最终验证命令：" + command)
    plan.record_changes(["src/main/java/Price.java"])
    assert plan.record_validation(command, "exit_code=0\nBUILD SUCCESS", verified_paths=["src/main/java/Price.java"])
    assert plan.ready_to_finish


@pytest.mark.parametrize("command", ["mvn -q test -Dtest=PriceTest", "mvn -q test -DskipTests", "mvn -q -DskipTests test"])
def test_maven_test_selectors_and_skip_properties_are_not_full_validation(command):
    parsed = parse_validation_command(command, "/repo")
    assert parsed is None or not parsed.full
    plan = VerificationPlan("/repo", "修复 Price.java 并完整测试。")
    assert not plan.record_validation(command, "exit_code=0\nBUILD SUCCESS", verified_paths=["Price.java"])
    assert not plan.ready_to_finish
