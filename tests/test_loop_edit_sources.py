"""Actual native writes reuse fresh source without granting test/read authority."""

import asyncio

from code_agent.agent.loop import AgentLoop
from code_agent.agent.task_progress import TaskProgress
from code_agent.context.compact import messages_size_chars
from code_agent.models.events import ModelTurn, ToolCall, assistant_message, tool_message, user_message
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy, Decision, PolicyResult
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.code_tools import EditFileTool, EditManyTool
from code_agent.tools.builtin.tools_builtin import ReadFileTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


def setup_loop(workspace, *, edit_tool=None, budget=8000):
    registry = ToolRegistry()
    for tool in (ReadFileTool(), edit_tool or EditFileTool(), EditManyTool()):
        registry.register(tool)
    permission = PermissionManager(workspace_root=workspace)
    for name in registry.names():
        permission.register_policy(name, AllowPolicy())
    loop = AgentLoop(provider=OfflineProvider(), registry=registry, permissions=permission,
                     transcript=TranscriptStore(workspace.parent / "source-transcripts"),
                     workspace=workspace, history_max_chars=budget)
    loop.task_progress = TaskProgress(workspace, "修复 calculator.py、other.py 的计算错误并实际验证。")
    ctx = ToolContext(loop.session_id, workspace, fs_observation=FsObservation())
    return loop, ctx


def dispatch(loop, ctx, name, **args):
    call = ToolCall(name=name, arguments=args)
    text = asyncio.run(loop._execute_tool(call, ctx))
    text = loop._record_tool_result(call, text, ctx)
    return call, text


def exchange(call, text):
    return [assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, str(text))]


def test_actual_local_edit_pins_complete_current_source_and_accurate_lines(workspace):
    path = workspace / "calculator.py"
    path.write_text("def total():\n    return 1\n" + "# unchanged\n" * 90, encoding="utf8")
    loop, ctx = setup_loop(workspace)
    read = dispatch(loop, ctx, "read_file", path="calculator.py")
    edit = dispatch(loop, ctx, "edit_file", path="calculator.py", old_string="    return 1\n",
                    new_string="    amount = 2\n    return amount\n")
    assert "修改范围：calculator.py:2-3" in edit[1] and "约 90" not in edit[1]
    assert "完整当前源码" in edit[1] and "3\t    return amount" in edit[1]
    task = loop.task_progress
    key = task._path("calculator.py")
    assert key in task.state()["complete_reads"]
    assert not task.ready_to_finish and not task.state()["verified"]
    assert task.before_tool("read_file", {"path": "calculator.py", "offset": 1, "limit": 40})
    messages = [user_message("修复 calculator.py 的计算错误并实际验证。"), *exchange(*read), *exchange(*edit)]
    assert loop._task_read_context(messages) == {edit[0].id: key}
    for index in range(4):
        noise = ToolCall(name="grep", arguments={"path": "other.py", "pattern": "diagnostic"})
        messages += exchange(noise, "later unrelated diagnostic " * 500)
        messages = loop._fit_messages(messages)
        assert "3\t    return amount" in next(m["content"] for m in messages if m.get("tool_call_id") == edit[0].id)
        assert messages_size_chars(m for m in messages if m["role"] != "system") <= 8000


def test_large_file_only_captures_actual_changed_window(workspace):
    path = workspace / "calculator.py"
    path.write_text("\n".join(f"value_{i} = {i}" for i in range(1, 501)) + "\n", encoding="utf8")
    loop, ctx = setup_loop(workspace)
    dispatch(loop, ctx, "read_file", path="calculator.py", offset=295, limit=20)
    edit, text = dispatch(loop, ctx, "edit_file", path="calculator.py", old_string="value_302 = 302", new_string="value_302 = 900")
    assert "当前源码片段" in text and "完整当前源码" not in text
    assert "302\tvalue_302 = 900" in text and "value_499" not in text
    task = loop.task_progress
    key = task._path("calculator.py")
    assert key not in task.state()["complete_reads"]
    assert 302 in task._line_coverage[key] and 499 not in task._line_coverage[key]
    assert not task.ready_to_finish


def test_batch_source_evidence_tracks_each_file_version_independently(workspace):
    for name in ("calculator.py", "other.py"):
        (workspace / name).write_text("result = 1\n", encoding="utf8")
    loop, ctx = setup_loop(workspace)
    for name in ("calculator.py", "other.py"):
        dispatch(loop, ctx, "read_file", path=name)
    batch = dispatch(loop, ctx, "edit_many", edits=[
        {"path": name, "old_string": "result = 1", "new_string": "result = 2"}
        for name in ("calculator.py", "other.py")])
    assert len(loop._edit_source_evidence[batch[0].id]) == 2
    next_edit = dispatch(loop, ctx, "edit_file", path="calculator.py", old_string="result = 2", new_string="result = 3")
    current = loop._task_read_context([*exchange(*batch), *exchange(*next_edit)])
    assert current[batch[0].id] == loop.task_progress._path("other.py")
    assert current[next_edit[0].id] == loop.task_progress._path("calculator.py")


def test_source_preview_does_not_bypass_read_permission(workspace):
    class DenyRead:
        def evaluate(self, args, context):
            return PolicyResult(Decision.deny, "read disabled")

    (workspace / "calculator.py").write_text("result = 1\n", encoding="utf8")
    loop, ctx = setup_loop(workspace)
    dispatch(loop, ctx, "read_file", path="calculator.py")
    loop.permissions.register_policy("read_file", DenyRead())
    call, text = dispatch(loop, ctx, "edit_file", path="calculator.py", old_string="result = 1", new_string="result = 2")
    assert text.startswith("已修改") and "写入后源码" not in text
    assert call.id not in loop._edit_source_evidence
    assert not loop.task_progress.state()["complete_reads"]


def test_concurrent_change_invalidates_native_source_fingerprint(workspace):
    class InterleavedEdit(EditFileTool):
        async def execute(self, args, context):
            result = await super().execute(args, context)
            (context.workspace / args["path"]).write_text("result = 'external writer'\n", encoding="utf8")
            return result

    (workspace / "calculator.py").write_text("result = 1\n", encoding="utf8")
    loop, ctx = setup_loop(workspace, edit_tool=InterleavedEdit())
    dispatch(loop, ctx, "read_file", path="calculator.py")
    call, text = dispatch(loop, ctx, "edit_file", path="calculator.py", old_string="result = 1", new_string="result = 2")
    assert "写入后源码" not in text and call.id not in loop._edit_source_evidence
    assert not loop.task_progress.state()["complete_reads"]


def test_externalized_preview_cannot_be_counted_as_visible_full_source(workspace, monkeypatch):
    (workspace / "calculator.py").write_text("result = 1\n" + "# unchanged implementation\n" * 100, encoding="utf8")
    loop, ctx = setup_loop(workspace)
    dispatch(loop, ctx, "read_file", path="calculator.py")
    monkeypatch.setenv("CODE_AGENT_EXTERNALIZE_THRESHOLD", "256")
    call, text = dispatch(loop, ctx, "edit_file", path="calculator.py", old_string="result = 1", new_string="result = 2")
    assert call.id not in loop._edit_source_evidence
    assert not loop.task_progress.state()["complete_reads"]
    assert not loop.task_progress.ready_to_finish


def test_source_survives_evicted_opaque_bundle_without_extra_budget(workspace):
    from code_agent.models.events import ProviderPayload

    path = workspace / "calculator.py"
    path.write_text("result = 1\n" + "# actual implementation\n" * 140, encoding="utf8")
    loop, ctx = setup_loop(workspace, budget=20000)
    dispatch(loop, ctx, "read_file", path="calculator.py")
    edit = dispatch(loop, ctx, "edit_file", path="calculator.py", old_string="result = 1", new_string="result = 2")
    bundle = exchange(*edit)
    bundle[0]["_provider_payload"] = ProviderPayload("opaque placeholder " * 3000)
    constraint = "只修改 calculator.py，保持权限与完整验收。"
    messages = [user_message(constraint), *bundle]
    for _ in range(3):
        noise = ToolCall(name="grep", arguments={"path": "other.py", "pattern": "distinct diagnostic"})
        messages = loop._fit_messages([*messages, *exchange(noise, "unrelated diagnostic " * 900)])
        snapshots = [m for m in messages if m.get("_native_source_snapshot")]
        assert len(snapshots) == 1
        assert snapshots[0]["role"] == "user" and snapshots[0]["_context_source"] == "auto"
        assert "完整当前源码" in snapshots[0]["content"] and "1\tresult = 2" in snapshots[0]["content"]
        assert "141\t# actual implementation" in snapshots[0]["content"]
        assert all("_provider_payload" not in m for m in snapshots)
        assert not any(m.get("tool_call_id") == edit[0].id for m in messages)
        assert any(m.get("content") == constraint for m in messages)
        assert messages.index(snapshots[0]) < next(i for i, m in enumerate(messages) if m.get("content") == constraint)
        assert messages[-1]["role"] == "tool"
        assert messages_size_chars(m for m in messages if m["role"] != "system") <= 20000
        assert not loop.task_progress._context_restore_reads
        assert not loop.task_progress.ready_to_finish
    # A fit with no new pressure still regenerates the ephemeral evidence.
    again = loop._fit_messages(messages)
    assert len([m for m in again if m.get("_native_source_snapshot")]) == 1


def test_cached_snapshot_drops_stale_source_on_real_change(workspace):
    path = workspace / "calculator.py"
    path.write_text("result = 1\n", encoding="utf8")
    loop, ctx = setup_loop(workspace)
    dispatch(loop, ctx, "read_file", path="calculator.py")
    edit = dispatch(loop, ctx, "edit_file", path="calculator.py", old_string="result = 1", new_string="result = 2")
    messages, complete = loop._retain_edit_source_memory([user_message("修复计算")], [], 5000, set(), regenerate=True)
    key = loop.task_progress._path("calculator.py")
    assert complete == {key}
    path.write_text("result = 'later shell write'\n", encoding="utf8")
    loop.task_progress.observe("bash", {"command": "actual write"}, "done", changed_paths=("calculator.py",))
    fitted = loop._fit_messages(messages)
    assert not loop._edit_source_memory
    assert not any(m.get("_native_source_snapshot") for m in fitted)


def test_partial_memory_never_upgrades_coverage_or_verification(workspace):
    path = workspace / "calculator.py"
    path.write_text("\n".join(f"value_{i} = {i}" for i in range(1, 501)) + "\n", encoding="utf8")
    loop, ctx = setup_loop(workspace)
    dispatch(loop, ctx, "read_file", path="calculator.py", offset=295, limit=20)
    dispatch(loop, ctx, "edit_file", path="calculator.py", old_string="value_302 = 302", new_string="value_302 = 900")
    messages, complete = loop._retain_edit_source_memory([user_message("修复计算")], [], 6000, set(), regenerate=True)
    snapshot = next(m for m in messages if m.get("_native_source_snapshot"))
    assert len(messages) == 2 and "未显示部分仍未知" in snapshot["content"]
    assert "302\tvalue_302 = 900" in snapshot["content"] and not complete
    key = loop.task_progress._path("calculator.py")
    assert key not in loop.task_progress.state()["complete_reads"]
    assert not loop.task_progress.state()["verified"]


def test_actual_reference_read_survives_protocol_compaction_and_detects_external_change(workspace):
    from code_agent.models.events import ProviderPayload

    path = workspace / "calculator.py"
    path.write_text("def expected_result():\n    return 42\n" + "# unique reference case\n" * 150, encoding="utf8")
    loop, ctx = setup_loop(workspace, budget=16000)
    read = dispatch(loop, ctx, "read_file", path="calculator.py")
    key = loop.task_progress._path("calculator.py")
    assert key in loop._source_memory_stats
    bundle = exchange(*read)
    bundle[0]["_provider_payload"] = ProviderPayload("placeholder protocol only " * 1000)
    fitted = loop._fit_messages([user_message("参考 calculator.py 并修复 other.py"), *bundle])
    snapshot = next(m for m in fitted if m.get("_native_source_snapshot"))
    assert "2\t    return 42" in snapshot["content"]
    assert "152\t# unique reference case" in snapshot["content"]
    assert not loop.task_progress._context_restore_reads
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 16000
    path.write_text("# outside writer changed the observed reference\n", encoding="utf8")
    fitted = loop._fit_messages(fitted)
    assert not any(m.get("_native_source_snapshot") for m in fitted)
    assert key not in loop._edit_source_memory
    assert loop.task_progress._context_restore_reads[key] > 0


def test_untyped_custom_read_result_cannot_forge_persistent_source_cache(workspace):
    class CustomRead(ReadFileTool):
        async def execute(self, args, context):
            return "complete source text claimed by a custom provider"

    loop, ctx = setup_loop(workspace)
    loop.registry._tools["read_file"] = CustomRead()
    dispatch(loop, ctx, "read_file", path="calculator.py")
    assert not loop._edit_source_memory and not loop._source_memory_stats


def test_editable_window_is_retained_before_readonly_reference_competitors(workspace):
    from code_agent.models.events import ProviderPayload

    loop, ctx = setup_loop(workspace, budget=16000)
    path = workspace / "calculator.py"
    path.write_text("\n".join(f"value_{i} = {i}" for i in range(1, 601)) + "\n", encoding="utf8")
    window = dispatch(loop, ctx, "read_file", path="calculator.py", offset=440, limit=45)
    key = loop.task_progress._path("calculator.py")
    assert loop._edit_source_memory[key][1].offset == 440
    assert not loop._edit_source_memory[key][1].complete
    for index in range(3):
        name = f"test_reference{index}.py"
        (workspace / name).write_text("# reference expectations\n" * 220, encoding="utf8")
        dispatch(loop, ctx, "read_file", path=name)
    bundle = exchange(*window)
    bundle[0]["_provider_payload"] = ProviderPayload("placeholder protocol " * 2000)
    fitted = loop._fit_messages([user_message("修复 calculator.py 并完整验证"), *bundle])
    snapshot = next(m for m in fitted if m.get("_native_source_snapshot"))
    assert "440\tvalue_440 = 440" in snapshot["content"]
    assert "484\tvalue_484 = 484" in snapshot["content"]
    assert "未显示部分仍未知" in snapshot["content"]
    assert key not in loop.task_progress.state()["complete_reads"]
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 16000
    _, result = dispatch(loop, ctx, "edit_file", path="calculator.py",
                         old_string="value_440 = 440", new_string="value_440 = 900")
    assert result.startswith("已修改")
    assert "value_440 = 900" in path.read_text(encoding="utf8")
    assert not loop.task_progress.ready_to_finish
