"""Actual project isolation, immutable acceptance and delivery runtime checks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from code_agent.agent.code_question import requires_mutation
from code_agent.permissions.policy import Decision
from code_agent.models.events import Event, EventType
from code_agent.providers.offline import OfflineProvider
from code_agent.tools.base import ToolContext
from scripts.verify_project_development_delivery import (
    ACCEPTANCE, ACCEPTANCE_PATH, API, BUG_TEST, NEW_TEST, ORIGINAL_TEST, PYTHON, REPO, SOURCE, TARGETS,
    ProjectGrepTool, ProjectObserver, ProjectReadFileTool, ProjectRecordingProvider, ProjectShellPolicy, ProjectWritePolicy,
    acceptance_source, canonical_python_command, configure_probe_tools, external_verify, is_complete_project_check, make_fixture,
    pytest_command, reference_edits, resolve_probe_provider, run_probe, scenario_targets, snapshot, task_prompt,
    timed_shell_execute, pytest_run_details, test_workflow_metrics as workflow_metrics,
    edit_payload_details, edit_workflow_metrics, measured_edit_execute, post_delivery_metrics,
)


def test_fixture_copies_actual_project_and_preserves_original_tests(tmp_path):
    workspace, initial = make_fixture(tmp_path)
    assert len(initial) >= 170
    for path in (SOURCE, API, ORIGINAL_TEST, "tests/conftest.py", "code_agent/agent/routing.CONTRACT.md"):
        assert (workspace / path).read_bytes() == (REPO / path).read_bytes()
    assert not (workspace / NEW_TEST).exists()
    assert not list(workspace.rglob(".env"))
    assert requires_mutation(task_prompt())
    assert "whole_word" in task_prompt() and "src/nested/deep.py" in task_prompt()
    assert "def _matches_include" not in task_prompt()
    assert (tmp_path / "acceptance/test_search_delivery.py").is_file()


def test_acceptance_reproduces_missing_feature_and_real_glob_bug_then_checks_solution(tmp_path):
    workspace, _ = make_fixture(tmp_path)
    existing = external_verify(workspace, include_new=False, original_only=True, label="unit-existing")
    assert existing["exit_code"] == 0 and existing["tests"] >= 20
    baseline = external_verify(workspace, include_new=False, label="unit-baseline")
    assert baseline["exit_code"] != 0 and baseline["failures"] >= 15 and baseline["errors"] == 0
    failures = " ".join(test["name"] for test in baseline["failing_tests"])
    assert "test_path_segment_glob_semantics" in failures and "test_whole_word_unicode" in failures
    for relative, source in reference_edits(workspace).items():
        (workspace / relative).write_text(source, encoding="utf8")
    solved = external_verify(workspace, label="unit-solved")
    assert solved["exit_code"] == 0 and solved["tests"] >= 60
    assert solved["suites"]["acceptance"] >= 40
    assert solved["suites"]["existing_regression"] == existing["tests"]
    assert solved["suites"]["new_tests"] == 2
    assert solved["skipped"] == 0


def test_write_policy_keeps_old_tests_and_non_targets_immutable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = ToolContext("project-probe", workspace)
    single = ProjectWritePolicy(workspace)
    batch = ProjectWritePolicy(workspace, batch=True)
    assert single.evaluate({"path": SOURCE}, ctx).decision == Decision.allow
    assert single.evaluate({"path": NEW_TEST}, ctx).decision == Decision.allow
    for forbidden in (ORIGINAL_TEST, "tests/conftest.py", "code_agent/config.py", "../acceptance/test_search_delivery.py",
                      "code_agent/agent/routing.CONTRACT.md"):
        assert single.evaluate({"path": forbidden}, ctx).decision == Decision.deny
        assert batch.evaluate({"edits": [{"path": SOURCE, "old_string": "a", "new_string": "b"},
                                          {"path": forbidden, "old_string": "x", "new_string": "y"}]}, ctx).decision == Decision.deny
    assert batch.evaluate({"edits": [{"path": path, "old_string": "a", "new_string": "b"} for path in TARGETS]}, ctx).decision == Decision.allow


@pytest.mark.parametrize("command", [
    "python -m pytest -q -c ../pytest.ini ../acceptance/test_search_delivery.py tests/test_project_file_search.py",
    "python -m pytest -q tests/test_search_whole_word.py",
    "python -m py_compile code_agent/codeintel/file_search.py code_agent/api/code_api.py",
])
def test_project_shell_permits_real_local_checks(tmp_path, command):
    assert ProjectShellPolicy().evaluate({"command": command}, ToolContext("probe", tmp_path)).decision == Decision.allow
    assert ProjectShellPolicy().evaluate({"command": pytest_command()}, ToolContext("probe", tmp_path)).decision == Decision.allow


@pytest.mark.parametrize("command", [
    "python -m pytest --override-ini=python_functions=fake tests/test_search_whole_word.py",
    "python -m pytest -p malicious tests/test_search_whole_word.py",
    "python -m pytest ../acceptance/test_search_delivery.py ; echo passed",
    "python -m pytest ../acceptance/test_search_delivery.py > fake.txt",
    "python -c 'from pathlib import Path; Path(\"../acceptance/test_search_delivery.py\").write_text(\"pass\")'",
    "python -c 'import pathlib; pathlib.Path(\"tests/test_project_file_search.py\").write_text(\"pass\")'",
    "python -c 'import os; os.system(\"echo passed\")'",
    "python -c 'exec(\"print(1)\")'",
    "python -c 'from pathlib import Path; p=Path(\"code_agent/codeintel/file_search.py\"); p.replace(\"bad.py\")'",
    "python -c 'from pathlib import Path; Path(\"code_agent/codeintel/file_search.py\").parent.unlink()'",
])
def test_project_shell_rejects_verifier_tampering_and_unbounded_scripts(tmp_path, command):
    assert ProjectShellPolicy().evaluate({"command": command}, ToolContext("probe", tmp_path)).decision == Decision.deny


def test_project_shell_permits_native_style_scoped_pathlib_edit(tmp_path):
    code = ('from pathlib import Path; p=Path("code_agent/codeintel/file_search.py"); '
            's=p.read_text(encoding="utf8"); p.write_text(s.replace("old", "new"), encoding="utf8")')
    command = shlex.quote(PYTHON) + " -c " + shlex.quote(code)
    assert ProjectShellPolicy().evaluate({"command": command}, ToolContext("probe", tmp_path)).decision == Decision.allow


def test_model_receipt_requires_all_real_project_suites_at_current_versions(tmp_path):
    for relative in TARGETS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source", encoding="utf8")
    observer = ProjectObserver(tmp_path, SimpleNamespace(calls=1), snapshot(tmp_path))
    def record(command):
        observer.on_event(Event(seq=1, type=EventType.tool_use, payload={"call": {
            "id": "check", "name": "bash", "arguments": {"command": command}}}))
        observer.on_event(Event(seq=2, type=EventType.tool_result, payload={
            "call_id": "check", "name": "bash", "result": "exit_code=0\n20 passed in 1.00s\n"}))
    record("python -m pytest -q tests/test_search_whole_word.py")
    assert not observer.summary()["model_verified_paths"]
    record(pytest_command())
    assert set(observer.summary()["model_verified_paths"]) == set(TARGETS)
    (tmp_path / SOURCE).write_text("changed after tests", encoding="utf8")
    observer.on_event(Event(seq=3, type=EventType.tool_result, payload={
        "call_id": "edit", "name": "edit_file", "result": "edited"}))
    assert SOURCE not in observer.summary()["model_verified_paths"]


def test_live_profile_is_read_only_and_safe_metadata_does_not_expose_credentials(tmp_path, monkeypatch):
    from code_agent import settings, providers
    profiles_file = tmp_path / "providers.json"
    default_file = tmp_path / "config.json"
    default_file.write_text(json.dumps({"provider": "openai_compat", "model_default": "unchanged-default"}), encoding="utf8")
    selected = {"id": "existing-official", "provider": "deepseek", "model_default": "test-model",
                "api_key": "private-existing-profile-key", "base_url": "https://independent-provider.invalid/v1",
                "proxy_mode": "direct", "context_window": 123456, "max_output_tokens": 4096,
                "model_lite": "legacy-do-not-clean-up"}
    profiles_file.write_text(json.dumps({"active": "different-active-profile", "profiles": [selected]}), encoding="utf8")
    before_profiles, before_default = profiles_file.read_bytes(), default_file.read_bytes()
    monkeypatch.setattr(settings, "_profiles_file", lambda: profiles_file)
    monkeypatch.setattr(settings, "settings_file", lambda: default_file)
    def forbidden(*args, **kwargs):
        raise AssertionError("Probe must never activate, save or migrate a profile")
    for name in ("_read_profiles", "_write_profiles", "activate_profile", "save", "save_profile"):
        monkeypatch.setattr(settings, name, forbidden)
    captured = {}
    def create(name, **kwargs):
        captured.update(provider=name, **kwargs)
        return SimpleNamespace(name=name, **kwargs)
    monkeypatch.setattr(providers, "create_provider", create)
    router, delegate, metadata = resolve_probe_provider(tmp_path, live=True, profile_id="existing-official")
    assert router.provider_name == "deepseek" and delegate.model == "test-model"
    assert captured == {"provider": "deepseek", "api_key": selected["api_key"], "model": selected["model_default"],
                        "base_url": selected["base_url"], "proxy_mode": "direct", "context_window": 123456,
                        "max_output_tokens": 4096}
    assert metadata == {"profile_id": "existing-official", "provider": "deepseek", "model": "test-model"}
    assert selected["api_key"] not in json.dumps(metadata) and selected["base_url"] not in json.dumps(metadata)
    assert profiles_file.read_bytes() == before_profiles and default_file.read_bytes() == before_default


def test_profile_selection_requires_live_and_never_falls_back_to_default(tmp_path, monkeypatch):
    from code_agent import settings
    with pytest.raises(ValueError, match="requires --live"):
        resolve_probe_provider(tmp_path, live=False, profile_id="anything")
    with pytest.raises(ValueError, match="requires --live"):
        asyncio.run(run_probe(tmp_path / "no-side-effects", live=False, profile_id="anything"))
    assert not (tmp_path / "no-side-effects").exists()
    path = tmp_path / "providers.json"
    path.write_text('{"profiles": []}', encoding="utf8")
    monkeypatch.setattr(settings, "_profiles_file", lambda: path)
    with pytest.raises(ValueError, match="not found"):
        resolve_probe_provider(tmp_path, live=True, profile_id="missing")


def test_existing_offline_default_path_does_not_read_profiles(tmp_path, monkeypatch):
    from code_agent import settings
    workspace, _ = make_fixture(tmp_path)
    monkeypatch.setattr(settings, "_profiles_file", lambda: pytest.fail("Unselected profile storage must not be read"))
    router, delegate, metadata = resolve_probe_provider(workspace, live=False)
    assert router.provider_name == "offline" and isinstance(delegate, OfflineProvider)
    assert metadata["profile_id"] is None and metadata["provider"] == "offline"


def test_real_runtime_offline_development_delivers_feature_bug_and_tests(tmp_path):
    summary = asyncio.run(run_probe(tmp_path, live=False, timeout=250, max_turns=20))
    assert summary["passed"], summary
    assert summary["stop_reason"] == "end_turn"
    assert summary["mode"] == "offline"
    assert summary["baseline_existing"]["exit_code"] == 0
    assert summary["baseline_acceptance"]["failures"] >= 15
    assert summary["external_verification"]["tests"] >= 60
    assert summary["external_verification"]["errors"] == 0
    assert set(summary["changed_paths"]) == set(TARGETS)
    assert set(summary["model_verified_paths"]) == set(TARGETS)
    assert summary["independent_verifier_unchanged"]
    assert summary["original_dependencies_and_tests_unchanged"]
    assert summary["new_regression_tests_authored"]
    assert not summary["out_of_scope_changes"]
    assert "code_search" in summary["production_registry_tools"]
    assert (tmp_path / "requests/turn-001.json").is_file()


@pytest.mark.parametrize("scenario", ["feature", "bug"])
def test_focused_scenarios_reproduce_and_repair_only_the_requested_obligation(tmp_path, scenario):
    workspace, initial = make_fixture(tmp_path, scenario=scenario)
    baseline = external_verify(workspace, include_new=False, label="focused-baseline", scenario=scenario)
    assert baseline["exit_code"] != 0 and baseline["failures"] > 0 and baseline["errors"] == 0
    assert baseline["suites"]["existing_regression"] == 20
    if scenario == "feature":
        assert "test_path_segment_glob_semantics" not in acceptance_source(scenario)
        assert "test_api_glob_semantics_and_whole_word_combination" not in acceptance_source(scenario)
        assert "test_whole_word_unicode_literal_and_case" in acceptance_source(scenario)
        assert len(scenario_targets(scenario)) == 3
    else:
        assert "whole_word" not in acceptance_source(scenario)
        assert "test_api_glob_reaches_real_service" in acceptance_source(scenario)
        assert scenario_targets(scenario) == (SOURCE, BUG_TEST)
    for relative, source in reference_edits(workspace, scenario=scenario).items():
        (workspace / relative).write_text(source, encoding="utf8")
    final = external_verify(workspace, label="focused-final", scenario=scenario)
    assert final["exit_code"] == 0 and final["errors"] == 0 and final["skipped"] == 0
    assert final["tests"] == baseline["tests"] + 2
    assert final["suites"]["new_tests"] == 2
    current = snapshot(workspace)
    assert all(current[path] == value for path, value in initial.items() if path not in scenario_targets(scenario))
    assert requires_mutation(task_prompt(scenario))


def test_combined_acceptance_is_preserved_exactly():
    assert acceptance_source() == ACCEPTANCE
    # The old real benchmark remains directly comparable after splitting tasks.
    import hashlib
    assert hashlib.sha256(ACCEPTANCE.encode()).hexdigest() == "c9de9bea8efc1654d1dacc2f7d099666123dfbfa477dac522fedf8d73d4d8b36"


@pytest.mark.parametrize("command", [
    "python -m pytest -q --tb=no -k 'path_segment and not API' ../acceptance/test_search_delivery.py",
    "python -m pytest --tb=line --maxfail=1 tests/test_search_whole_word.py::test_feature",
    "python -m pytest --collect-only tests/test_project_file_search.py",
    "python -m pytest --disable-warnings --maxfail 2 tests/test_project_file_search.py -k unicode",
])
def test_intermediate_checks_allow_normal_focused_pytest_diagnosis(tmp_path, command):
    assert ProjectShellPolicy().evaluate({"command": command}, ToolContext("probe", tmp_path)).decision == Decision.allow
    assert not is_complete_project_check(command)


@pytest.mark.parametrize("extra", ["-k feature", "--collect-only", "tests/test_search_whole_word.py::test_feature"])
def test_focused_checks_cannot_become_full_delivery_receipts(tmp_path, extra):
    observer = ProjectObserver(tmp_path, SimpleNamespace(calls=1), {}, minimum_tests=69)
    command = pytest_command() + " " + extra
    observer.on_event(Event(seq=1, type=EventType.tool_use, payload={"call": {
        "id": "filtered", "name": "bash", "arguments": {"command": command}}}))
    observer.on_event(Event(seq=2, type=EventType.tool_result, payload={
        "call_id": "filtered", "name": "bash", "result": "exit_code=0\n69 passed in 1.00s\n"}))
    assert observer.summary()["successful_model_test_runs"] == 0


def test_full_receipt_also_requires_real_suite_size_and_no_skips(tmp_path):
    for relative in TARGETS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source", encoding="utf8")
    observer = ProjectObserver(tmp_path, SimpleNamespace(calls=1), snapshot(tmp_path), minimum_tests=69)
    def record(text):
        observer.on_event(Event(seq=1, type=EventType.tool_use, payload={"call": {
            "id": "full", "name": "bash", "arguments": {"command": pytest_command()}}}))
        observer.on_event(Event(seq=2, type=EventType.tool_result, payload={
            "call_id": "full", "name": "bash", "result": text}))
    record("exit_code=0\n2 passed in 0.10s\n")
    assert not observer.summary()["model_verified_paths"]
    record("exit_code=0\n69 passed, 1 skipped in 0.10s\n")
    assert not observer.summary()["model_verified_paths"]
    record("exit_code=0\n69 passed in 0.10s\n")
    assert set(observer.summary()["model_verified_paths"]) == set(TARGETS)


def test_acceptance_is_readable_only_through_the_exact_read_only_fixture_path(tmp_path):
    from code_agent.tools.base import ToolError
    workspace, _ = make_fixture(tmp_path)
    tool, ctx = ProjectReadFileTool(workspace), ToolContext("probe", workspace)
    contents = asyncio.run(tool.execute({"path": ACCEPTANCE_PATH}, ctx))
    assert contents.replace("\r\n", "\n") == ACCEPTANCE
    absolute = (workspace / ACCEPTANCE_PATH).resolve().as_posix()
    assert asyncio.run(tool.execute({"path": absolute}, ctx)) == contents
    assert absolute in tool.description
    assert asyncio.run(tool.execute({"path": SOURCE}, ctx)).replace("\r\n", "\n") == (workspace / SOURCE).read_text(encoding="utf8")
    # Neither another parent file nor the parent itself is exposed by this probe.
    for forbidden in ("../source-manifest.json", "../../secret.txt", "../pytest.ini"):
        with pytest.raises(ToolError, match="读取路径不在允许范围"):
            asyncio.run(tool.execute({"path": forbidden}, ctx))
    assert ProjectWritePolicy(workspace).evaluate({"path": ACCEPTANCE_PATH}, ctx).decision == Decision.deny


def test_bug_policy_cannot_change_the_unrequested_api_or_feature_test(tmp_path):
    workspace, _ = make_fixture(tmp_path, scenario="bug")
    policy = ProjectWritePolicy(workspace, scenario="bug")
    ctx = ToolContext("probe", workspace)
    assert policy.evaluate({"path": SOURCE}, ctx).decision == Decision.allow
    assert policy.evaluate({"path": BUG_TEST}, ctx).decision == Decision.allow
    for path in (API, NEW_TEST, ORIGINAL_TEST):
        assert policy.evaluate({"path": path}, ctx).decision == Decision.deny


def test_observer_distinguishes_unchanged_duplicate_reads_from_post_edit_recovery(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf8")
    provider = SimpleNamespace(calls=1)
    observer = ProjectObserver(tmp_path, provider, snapshot(tmp_path))
    counter = 0
    def record(name, arguments, result):
        nonlocal counter
        counter += 1
        provider.calls = counter
        observer.on_event(Event(seq=counter * 2, type=EventType.tool_use, payload={"call": {
            "id": str(counter), "name": name, "arguments": arguments}}))
        observer.on_event(Event(seq=counter * 2 + 1, type=EventType.tool_result, payload={
            "call_id": str(counter), "name": name, "result": result}))
    record("read_file", {"path": "sample.py"}, path.read_text())
    record("read_file", {"path": "sample.py", "offset": 2, "limit": 1}, "2 two")
    record("grep", {"path": "sample.py", "pattern": "one", "_approval": {"purpose": "first"}}, "one")
    record("grep", {"path": "sample.py", "pattern": "one", "_approval": {"purpose": "changed"}}, "one")
    path.write_text("changed\ntwo\nthree\n", encoding="utf8")
    record("read_file", {"path": "sample.py", "offset": 2, "limit": 1}, "2 two")
    record("read_file", {"path": "missing.py"}, "[tool error] absent")
    record("bash", {"command": "prohibited"}, "[permission denied] denied")
    record("read_file", {"path": "sample.py"}, "[tool error] [task scope] [任务推进] already visible")
    summary = observer.summary()
    assert summary["repeated_read_count"] == 1
    assert summary["repeated_query_count"] == 1
    assert summary["repeated_read_lines"] == 1
    assert summary["read_error_count"] == 2 and summary["permission_error_count"] == 1
    assert summary["task_discipline_block_count"] == 1
    assert summary["first_write_turn"] == 5


def test_recording_provider_records_real_usage_and_latency():
    from code_agent.models.events import ModelTurn, TokenUsage
    provider = ProjectRecordingProvider(OfflineProvider(script=[ModelTurn(
        text="done", usage=TokenUsage(input_tokens=123, cached_tokens=40, output_tokens=17))]))
    asyncio.run(provider.chat([], []))
    assert provider.request_summaries[0]["usage"] == {"input_tokens": 123, "cached_tokens": 40, "output_tokens": 17}
    assert provider.request_summaries[0]["elapsed_seconds"] >= 0


def test_native_grep_has_the_same_exact_read_only_acceptance_boundary(tmp_path):
    from code_agent.tools.base import ToolError
    from code_agent.tools.builtin.code_tools import GrepTool
    production_description = GrepTool().description
    workspace, _ = make_fixture(tmp_path)
    tool, ctx = ProjectGrepTool(workspace), ToolContext("probe", workspace)
    absolute = (workspace / ACCEPTANCE_PATH).resolve().as_posix()
    for path in (ACCEPTANCE_PATH, absolute):
        result = asyncio.run(tool.execute({"path": path, "pattern": "def test_whole_word_unicode", "context": 1}, ctx))
        assert "匹配 1 处" in result and ACCEPTANCE_PATH + ":" in result
    assert absolute in tool.description
    for forbidden in ("../acceptance", "../pytest.ini", "../source-manifest.json", "../"):
        with pytest.raises(ToolError, match="读取路径不在允许范围"):
            asyncio.run(tool.execute({"path": forbidden, "pattern": "."}, ctx))
    assert GrepTool().description == production_description
    assert ProjectWritePolicy(workspace).evaluate({"path": absolute}, ctx).decision == Decision.deny


@pytest.mark.parametrize("flag", ["-l", "--showlocals"])
def test_pytest_current_workspace_cd_and_showlocals_share_complete_receipt_parser(tmp_path, flag):
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    ctx = ToolContext("probe", workspace)
    command = "cd " + shlex.quote(workspace.as_posix()) + " && " + pytest_command() + " " + flag
    assert ProjectShellPolicy().evaluate({"command": command}, ctx).decision == Decision.allow
    assert is_complete_project_check(command, workspace=workspace)
    parsed = canonical_python_command(command, workspace=workspace)
    assert parsed[1:3] == ["-m", "pytest"]
    focused = command + " -k whole_word"
    assert ProjectShellPolicy().evaluate({"command": focused}, ctx).decision == Decision.allow
    assert not is_complete_project_check(focused, workspace=workspace)


def test_absolute_pytest_suite_paths_are_the_same_fixed_suites(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    suites = [(workspace / path).resolve().as_posix() for path in (ACCEPTANCE_PATH, ORIGINAL_TEST, NEW_TEST)]
    command = shlex.quote(PYTHON) + " -m pytest -l -q -c " + shlex.quote((workspace.parent / "pytest.ini").as_posix()) + " " + " ".join(shlex.quote(path) for path in suites)
    assert ProjectShellPolicy().evaluate({"command": command}, ToolContext("probe", workspace)).decision == Decision.allow
    assert is_complete_project_check(command, workspace=workspace)
    unauthorized = command + " " + shlex.quote((workspace.parent / "other.py").as_posix())
    assert ProjectShellPolicy().evaluate({"command": unauthorized}, ToolContext("probe", workspace)).decision == Decision.deny
    assert not is_complete_project_check(unauthorized, workspace=workspace)


@pytest.mark.parametrize("template", [
    "cd .. && {check}",
    "cd . && cd . && {check}",
    "cd . ; {check}",
    "cd . && {check} && echo passed",
    "{check} 2>&1 | tail -50",
    "{check} > result.txt",
    "{check} || echo passed",
    "cd . && python -m py_compile code_agent/codeintel/file_search.py",
    "cd . && python -c 'print(1)'",
])
def test_cwd_or_masking_commands_cannot_pass_policy_or_become_full_receipts(tmp_path, template):
    command = template.format(check=pytest_command())
    assert ProjectShellPolicy().evaluate({"command": command}, ToolContext("probe", tmp_path)).decision == Decision.deny
    assert not is_complete_project_check(command, workspace=tmp_path)


@pytest.mark.skipif(__import__("os").name != "nt", reason="Git Bash drive spelling is Windows-only")
def test_git_bash_drive_spelling_resolves_only_to_current_workspace(tmp_path):
    literal = tmp_path.as_posix()
    drive_spelling = "/" + literal[0].lower() + "/" + literal[3:]
    command = "cd " + shlex.quote(drive_spelling) + " && " + pytest_command()
    assert ProjectShellPolicy().evaluate({"command": command}, ToolContext("probe", tmp_path)).decision == Decision.allow
    assert is_complete_project_check(command, workspace=tmp_path)


def test_probe_shell_guidance_changes_only_fresh_probe_instances(tmp_path):
    from code_agent.tools.builtin.tools_builtin import BashTool, BashPersistentTool
    from code_agent.tools.registry import ToolRegistry
    original, original_persistent = BashTool().description, BashPersistentTool().description
    registry = ToolRegistry()
    registry.register(BashTool())
    registry.register(BashPersistentTool())
    configure_probe_tools(registry, tmp_path)
    for name, expected in (("bash", original), ("bash_persistent", original_persistent)):
        description = registry.get(name).description
        assert description.startswith(expected)
        assert PYTHON in description and "No background execution, pipelines" in description
        assert "native read_file/grep/list_dir/glob/git_read" in description
    assert BashTool().description == original
    assert BashPersistentTool().description == original_persistent
    assert isinstance(registry.get("read_file"), ProjectReadFileTool)
    assert isinstance(registry.get("grep"), ProjectGrepTool)


@pytest.mark.parametrize("scenario", ["combined", "feature", "bug"])
def test_task_prompt_clearly_separates_writable_and_readonly_references(scenario):
    prompt = task_prompt(scenario)
    writable, remaining = prompt.split("\n可写范围：", 1)[1].split("\n只读参考：", 1)
    assert "只允许修改" in writable
    for path in scenario_targets(scenario):
        assert path in writable
    assert ACCEPTANCE_PATH not in writable and ORIGINAL_TEST not in writable
    assert ACCEPTANCE_PATH in remaining and ORIGINAL_TEST in remaining


@pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
def test_native_shell_timer_preserves_execution_and_records_actual_duration(monkeypatch, outcome):
    import asyncio
    from code_agent.tools.base import ToolError
    from scripts import verify_project_development_delivery as probe
    times = iter((10.0, 12.5))
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: next(times)))
    records, calls = [], []
    failure = ToolError("original failure") if outcome == "error" else asyncio.CancelledError()
    async def execute(arguments, ctx):
        calls.append((arguments, ctx))
        if outcome != "return":
            raise failure
        return "exit_code=1\n3 passed, 1 failed in 2.1s\n"
    timed = timed_shell_execute("bash", execute, records)
    arguments = {"command": "python -m pytest", "_approval": {"purpose": "public explanation"}}
    ctx = ToolContext("probe", Path("."))
    async def check():
        if outcome == "return":
            assert await timed(arguments, ctx) == "exit_code=1\n3 passed, 1 failed in 2.1s\n"
        else:
            with pytest.raises(type(failure)) as caught:
                await timed(arguments, ctx)
            assert caught.value is failure
    asyncio.run(check())
    assert calls == [(arguments, ctx)] and len(records) == 1
    assert records[0]["execution_seconds"] == 2.5
    assert "_approval" not in records[0]["signature"]
    if outcome == "return":
        assert records[0]["exit_code"] == 1 and records[0]["native_passed_count"] == 3
        assert records[0]["native_failed_count"] == 1
    else:
        assert records[0]["outcome"] == type(failure).__name__


def test_observer_reports_early_and_same_version_full_runs_without_granting_receipts(tmp_path):
    (tmp_path / "tests").mkdir()
    for relative in (SOURCE, API):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original", encoding="utf8")
    provider = SimpleNamespace(calls=1, request_summaries=[
        {"turn": 1, "usage": {"input_tokens": 100, "cached_tokens": 40, "output_tokens": 20}},
        {"turn": 2, "usage": {"input_tokens": 200, "cached_tokens": 80, "output_tokens": 30}},
    ])
    observer = ProjectObserver(tmp_path, provider, snapshot(tmp_path), minimum_tests=69)
    def record(turn, name, arguments, result, *, native_seconds=None):
        provider.calls = turn
        call_id = str(turn)
        observer.on_event(Event(seq=turn * 2, type=EventType.tool_use, payload={"call": {
            "id": call_id, "name": name, "arguments": arguments}}))
        if native_seconds is not None:
            observer.shell_executions.append({"tool": name, "signature": json.dumps(arguments, sort_keys=True),
                                               "outcome": "returned", "execution_seconds": native_seconds,
                                               "exit_code": 0 if result.startswith("exit_code=0") else 1})
        observer.on_event(Event(seq=turn * 2 + 1, type=EventType.tool_result, payload={
            "call_id": call_id, "name": name, "result": result}))
    baseline, full = pytest_command(include_new=False), pytest_command()
    record(1, "bash", {"command": baseline}, "exit_code=1\n2 failed, 65 passed", native_seconds=10)
    (tmp_path / SOURCE).write_text("implementation", encoding="utf8")
    record(2, "edit_file", {"path": SOURCE}, "edited")
    record(3, "bash", {"command": baseline}, "exit_code=0\n67 passed", native_seconds=11)
    (tmp_path / NEW_TEST).write_text("first tests", encoding="utf8")
    record(4, "write_file", {"path": NEW_TEST}, "created")
    record(5, "bash", {"command": full}, "exit_code=1\n2 failed, 67 passed", native_seconds=12)
    record(6, "bash", {"command": full.replace("--tb=short", "--tb=no")}, "exit_code=1\n2 failed, 67 passed", native_seconds=13)
    record(7, "bash", {"command": full + " -k boundary"}, "exit_code=1\n2 failed", native_seconds=4)
    (tmp_path / NEW_TEST).write_text("correct regression tests", encoding="utf8")
    record(8, "edit_file", {"path": NEW_TEST}, "changed tests")
    record(9, "bash", {"command": full}, "exit_code=0\n69 passed", native_seconds=14)
    summary = observer.summary()
    assert summary["pytest_run_count"] == 6
    assert summary["full_project_test_runs"] == 3 and summary["baseline_full_test_runs"] == 2
    assert summary["focused_test_runs"] == 1
    assert summary["same_version_repeated_full_test_runs"] == 1
    assert summary["post_edit_full_before_regression_creation"] == 1
    assert summary["test_creations_after_full"] == 1 and summary["test_rewrites_after_full"] == 1
    assert summary["test_creations_after_successful_full"] == 1 and summary["test_rewrites_after_successful_full"] == 1
    assert summary["target_change_calls_after_successful_full"] == 2
    assert summary["implementation_change_calls_after_successful_full"] == 0
    assert summary["post_edit_full_runs_superseded_by_later_writes"] == 3
    assert summary["pytest_execution_seconds"] == 64 and summary["shell_execution_seconds"] == 64
    assert summary["shell_execution_count"] == 6 and summary["unreported_shell_executions"] == 0
    assert all(row["timing_source"] == "native_execution" for row in summary["test_run_timeline"])
    assert summary["successful_model_test_runs"] == 1
    assert set(summary["model_verified_paths"]) == set(TARGETS)
    assert summary["model_tokens_through_first_write"] == {"input_tokens": 300, "cached_tokens": 120, "output_tokens": 50}


def test_saved_legacy_runs_keep_version_and_timing_classification(tmp_path):
    versions = {SOURCE: "implementation-v1", API: "api-v1", NEW_TEST: "tests-v1"}
    steps = [
        {"turn": 1, "tool": "bash", "arguments": {"command": pytest_command()}, "versions": versions,
         "changed": [], "tool_seconds": 5.5, "successful_test": True},
        {"turn": 2, "tool": "bash", "arguments": {"command": pytest_command() + " --showlocals"}, "versions": versions,
         "changed": [], "tool_seconds": 6.5, "successful_test": True},
        {"turn": 3, "tool": "bash", "arguments": {"command": pytest_command()}, "versions": versions,
         "changed": [], "error": True, "tool_seconds": 0.1},
    ]
    metrics = workflow_metrics(steps, workspace=tmp_path)
    assert metrics["pytest_run_count"] == 2 and metrics["same_version_repeated_full_test_runs"] == 1
    assert metrics["pytest_execution_seconds"] == 12
    assert all(row["timing_source"] == "tool_event_legacy" for row in metrics["test_run_timeline"])


@pytest.mark.parametrize("command,expected", [
    (pytest_command(), "full_project"),
    (pytest_command(include_new=False), "baseline_full"),
    (pytest_command() + " -k boundary", "focused"),
    ("python -m pytest tests/test_search_whole_word.py::test_feature", "focused"),
    ("python -m pytest --collect-only tests/test_search_whole_word.py", "collection"),
])
def test_test_scheduling_classification_is_distinct_from_delivery_acceptance(tmp_path, command, expected):
    assert pytest_run_details(command, workspace=tmp_path)["kind"] == expected


def test_deferred_calls_are_visible_but_never_counted_as_executed_tests(tmp_path):
    observer = ProjectObserver(tmp_path, SimpleNamespace(calls=1), {})
    cases = [
        ("bash", {"command": pytest_command()}, "[tool deferred] [verification plan] 未执行完整测试"),
        ("bash", {"command": "python -m py_compile " + SOURCE}, "[tool deferred] 前序编辑失败，未执行"),
        ("edit_file", {"path": SOURCE}, "[tool deferred] 前序编辑失败，未执行"),
        ("bash", {"command": pytest_command()}, "[permission denied] 不允许"),
    ]
    for number, (name, arguments, result) in enumerate(cases, start=1):
        observer.on_event(Event(seq=number * 2, type=EventType.tool_use, payload={"call": {
            "id": str(number), "name": name, "arguments": arguments}}))
        observer.on_event(Event(seq=number * 2 + 1, type=EventType.tool_result, payload={
            "call_id": str(number), "name": name, "result": result}))
    summary = observer.summary()
    assert summary["deferred_tool_count"] == 3
    assert summary["deferred_validation_count"] == 2
    assert summary["tool_error_count"] == 1 and summary["permission_error_count"] == 1
    assert summary["pytest_run_count"] == 0 and summary["shell_execution_count"] == 0
    assert summary["successful_model_test_runs"] == 0
    assert summary["shell_execution_seconds"] == 0 and not summary["model_verified_paths"]


def test_edit_payload_uses_actual_source_instead_of_changed_line_receipt(tmp_path):
    import hashlib
    from code_agent.tools.builtin.code_tools import EditFileTool
    source = "".join(f"value_{index} = {index}\n" for index in range(1700))
    path = tmp_path / "large.py"
    path.write_text(source, encoding="utf8")
    before = path.read_bytes()
    arguments = {"path": "large.py", "old_string": "value_10 = 10\n", "new_string": "# 局部说明\nvalue_10 = 10\n"}
    records = []
    wrapped = measured_edit_execute("edit_file", EditFileTool().execute, records)
    asyncio.run(wrapped(arguments, ToolContext("probe", tmp_path)))
    record = records[0]
    operation = record["operations"][0]
    assert record["old_utf8_bytes"] == len(arguments["old_string"].encode("utf8"))
    assert record["new_utf8_bytes"] == len(arguments["new_string"].encode("utf8"))
    assert operation["old_lines"] == 1 and operation["new_lines"] == 2
    assert operation["source_text_lines"] == 1700
    assert operation["source_disk_sha256"] == hashlib.sha256(before).hexdigest()
    assert operation["whole_file_replacement"] is False and operation["matching_occurrences"] == 1
    assert path.read_text(encoding="utf8") == source.replace(arguments["old_string"], arguments["new_string"])


def test_full_file_payload_is_proven_against_crlf_disk_and_native_normalized_text(tmp_path):
    import hashlib
    from code_agent.tools.builtin.code_tools import EditFileTool
    path = tmp_path / "sample.py"
    disk = b"alpha\r\nbeta\r\n"
    path.write_bytes(disk)
    old, new = "alpha\nbeta\n", "alpha\nbeta_changed\n"
    records = []
    arguments = {"path": "sample.py", "old_string": old, "new_string": new}
    wrapped = measured_edit_execute("edit_file", EditFileTool().execute, records)
    asyncio.run(wrapped(arguments, ToolContext("probe", tmp_path)))
    operation = records[0]["operations"][0]
    assert operation["whole_file_replacement"] is True
    assert operation["source_disk_sha256"] == hashlib.sha256(disk).hexdigest()
    assert operation["source_text_sha256"] == hashlib.sha256(old.encode()).hexdigest()
    assert operation["source_disk_bytes"] > operation["source_text_bytes"] == len(old.encode())
    metrics = edit_workflow_metrics([{"turn": 1, "tool": "edit_file", "edit_executed": True, "edit_payload": records[0]}])
    assert metrics["whole_file_replacement_attempts"] == metrics["successful_whole_file_replacement_operations"] == 1


def test_edit_many_metrics_follow_the_real_sequential_batch_state(tmp_path):
    from code_agent.tools.builtin.code_tools import EditManyTool
    path = tmp_path / "sample.py"
    path.write_text("alpha\nbeta\n", encoding="utf8")
    arguments = {"edits": [
        {"path": "sample.py", "old_string": "alpha", "new_string": "alpha_one"},
        {"path": "sample.py", "old_string": "alpha_one\nbeta\n", "new_string": "omega\n"},
    ]}
    records = []
    wrapped = measured_edit_execute("edit_many", EditManyTool().execute, records)
    asyncio.run(wrapped(arguments, ToolContext("probe", tmp_path)))
    first, second = records[0]["operations"]
    assert first["source_state"] == "disk_text" and first["whole_file_replacement"] is False
    assert second["source_state"] == "batch_trial" and second["whole_file_replacement"] is True
    assert first["source_disk_sha256"] == second["source_disk_sha256"]
    assert first["source_text_sha256"] != second["source_text_sha256"]
    assert path.read_text(encoding="utf8") == "omega\n"


def test_edit_instrumentation_does_not_grant_source_read_freshness(tmp_path):
    from code_agent.tools.base import ToolError
    from code_agent.tools.builtin.code_tools import EditFileTool
    from code_agent.tools.fs_observation import FsObservation
    path = tmp_path / "sample.py"
    path.write_text("alpha\n", encoding="utf8")
    records = []
    wrapped = measured_edit_execute("edit_file", EditFileTool().execute, records)
    with pytest.raises(ToolError, match="file has not been read"):
        asyncio.run(wrapped({"path": "sample.py", "old_string": "alpha\n", "new_string": "beta\n"},
                            ToolContext("probe", tmp_path, fs_observation=FsObservation())))
    assert path.read_text(encoding="utf8") == "alpha\n"
    metrics = edit_workflow_metrics([{"turn": 1, "tool": "edit_file", "edit_executed": True, "edit_payload": records[0]}])
    assert metrics["whole_file_replacement_attempts"] == 1
    assert metrics["successful_whole_file_replacement_operations"] == 0


def test_observer_collects_only_real_edit_execution_payloads(tmp_path):
    from code_agent.tools.builtin.code_tools import EditFileTool
    path = tmp_path / "sample.py"
    path.write_text("alpha\n", encoding="utf8")
    observer = ProjectObserver(tmp_path, SimpleNamespace(calls=1), snapshot(tmp_path))
    arguments = {"path": "sample.py", "old_string": "alpha", "new_string": "beta"}
    def record(call_id, result):
        observer.on_event(Event(seq=1, type=EventType.tool_use, payload={"call": {
            "id": call_id, "name": "edit_file", "arguments": arguments}}))
        observer.on_event(Event(seq=2, type=EventType.tool_result, payload={
            "call_id": call_id, "name": "edit_file", "result": result}))
    record("deferred", "[tool deferred] prior write failed")
    assert observer.summary()["native_edit_execution_count"] == 0
    wrapped = measured_edit_execute("edit_file", EditFileTool().execute, observer.edit_executions)
    result = asyncio.run(wrapped(arguments, ToolContext("probe", tmp_path)))
    record("real", result)
    summary = observer.summary()
    assert summary["native_edit_execution_count"] == 1 and summary["native_edit_operation_count"] == 1
    assert summary["max_edit_call_replacement_utf8_bytes"] == len(b"alpha") + len(b"beta")
    assert summary["whole_file_replacement_attempts"] == 0


def test_post_delivery_inspection_metrics_require_a_current_complete_receipt():
    versions = {SOURCE: "a", API: "b", NEW_TEST: "c"}
    steps = [
        {"turn": 4, "tool": "bash", "complete_project_test": True, "versions": versions},
        {"turn": 5, "tool": "read_file", "versions": versions},
        {"turn": 5, "tool": "grep", "versions": versions},
        {"turn": 6, "tool": "git_read", "versions": versions},
        {"turn": 6, "tool": "bash", "complete_project_test": True, "versions": versions},
    ]
    metrics = post_delivery_metrics(steps, model_calls=7)
    assert metrics == {"post_delivery_readonly_tool_calls": 3, "post_delivery_read_file_calls": 1,
                       "post_delivery_grep_calls": 1, "post_delivery_model_turns": 3}
    metrics = post_delivery_metrics(steps, model_calls=7, current_versions={**versions, SOURCE: "changed"})
    assert metrics["post_delivery_readonly_tool_calls"] is None
    assert metrics["post_delivery_model_turns"] is None
