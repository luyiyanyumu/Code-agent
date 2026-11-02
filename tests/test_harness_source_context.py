"""Source preservation and read-progress hints through the real Harness core."""

import asyncio
import os
import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.agent.source_context import SourceContext
from code_agent.context.compact import TOOL_OMIT_MARKER, compact_messages, messages_size_chars
from code_agent.models.events import ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.builtin import ReadFileTool
from code_agent.tools.builtin.code_tools import EditFileTool, GrepTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


def source_text():
    # Same scale as the reported 211-line, 8975-character Java read. The edit
    # anchor lies in the middle, outside the old 2000-character head/tail slice.
    rows = [f"# line {i:03d}: " + "current source evidence " for i in range(210)]
    rows[105] = "result = 1  # MIDDLE_EDIT_ANCHOR"
    source = "\n".join(rows) + "\n# "
    return source + "x" * (8975 - len(source))


def make_loop(workspace, provider, *extra, **kwargs):
    registry = ToolRegistry()
    for tool in (ReadFileTool(), EditFileTool(), *extra):
        registry.register(tool)
    permissions = PermissionManager(workspace_root=workspace)
    for name in registry.names():
        permissions.register_policy(name, AllowPolicy())
    return AgentLoop(provider, registry, permissions, TranscriptStore(workspace.parent / "source-context-events"),
                     workspace=workspace, fs_observation=FsObservation(), **kwargs)


def results(messages):
    return [str(m.get("content") or "") for m in messages if m.get("role") == "tool"]


@pytest.mark.parametrize("prompt", ["为当前文件添加中文注释", "实现 result 返回新值的功能", "修复 result 初始值错误"])
def test_real_harness_keeps_middle_anchor_and_edit_receipt_for_all_editing_tasks(workspace, prompt):
    source = source_text()
    assert len(source) == 8975 and len(source.splitlines()) == 211
    path = workspace / "current.py"
    path.write_text(source, encoding="utf8")

    class Reader(OfflineProvider):
        async def chat(self, messages, tools):
            if self.calls == 1:
                assert any(body.replace("\r\n", "\n") == source for body in results(messages))
            if self.calls == 2:
                assert any("编辑后源码" in body and "result = 2" in body for body in results(messages))
                assert any("源码版本已变化" in body for body in results(messages))
            return await super().chat(messages, tools)

    provider = Reader(script=[
        OfflineProvider.tool_call_turn("read_file", {"path": "current.py"}),
        OfflineProvider.tool_call_turn("edit_file", {"path": "current.py", "old_string": "result = 1", "new_string": "result = 2"}),
        OfflineProvider.text_turn("已完成局部修改。"),
    ])
    loop = make_loop(workspace, provider)
    completed = asyncio.run(loop.run(prompt))
    assert completed.turns == 3 and completed.stop_reason == "end_turn"
    compile(path.read_text(encoding="utf8"), str(path), "exec")
    receipts = [event.payload for event in loop.transcript.replay(loop.session_id) if event.type.value == "tool_result"]
    assert len(receipts) == 2 and receipts[1]["result"].startswith("已修改")
    assert "编辑后源码" not in receipts[1]["result"]  # Raw audit evidence is unchanged.


def test_stable_large_read_without_small_native_snapshot_is_still_preserved(workspace):
    source = "# ordinary source\n" * 700 + "final_value = 3\n"
    (workspace / "large.py").write_text(source, encoding="utf8")

    class Reader(OfflineProvider):
        async def chat(self, messages, tools):
            if self.calls == 1:
                assert any(body.replace("\r\n", "\n") == source for body in results(messages))
            return await super().chat(messages, tools)

    loop = make_loop(workspace, Reader(script=[OfflineProvider.tool_call_turn("read_file", {"path": "large.py"}), OfflineProvider.text_turn("已读完整源码")]))
    assert asyncio.run(loop.run("检查当前文件")).turns == 2


def test_current_source_survives_automatic_segment_but_not_a_new_user_task(workspace):
    source = source_text()
    (workspace / "current.py").write_text(source, encoding="utf8")

    class Reader(OfflineProvider):
        async def chat(self, messages, tools):
            if self.calls == 1:
                assert any(body.replace("\r\n", "\n") == source for body in results(messages))
            if self.calls == 2:
                assert not any(body.replace("\r\n", "\n") == source for body in results(messages))
            return await super().chat(messages, tools)

    loop = make_loop(workspace, Reader(script=[OfflineProvider.tool_call_turn("read_file", {"path": "current.py"}),
                                              OfflineProvider.text_turn("继续完成"), OfflineProvider.text_turn("新任务")]), max_turns=1)

    async def scenario():
        first = await loop.run("检查当前代码")
        assert first.segment_boundary
        second = await loop.run("继续", history=loop.continuation_history, context_meta={"continuation": True, "auto": True})
        assert second.text == "继续完成"
        await loop.run("换一个新任务", history=loop.continuation_history)

    asyncio.run(scenario())


def read_receipt(workspace, context, call_id="read", args=None):
    call = ToolCall(id=call_id, name="read_file", arguments=args or {"path": "current.py"})
    before = context.begin(call)
    result = asyncio.run(ReadFileTool().execute(call.arguments, ToolContext("source-context", workspace)))
    context.capture(call, result, before)
    context.delivered(call.id, str(result))
    messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": call.id, "type": "function",
                 "function": {"name": call.name, "arguments": '{"path":"current.py"}'}}]},
                {"role": "tool", "tool_call_id": call.id, "content": str(result)}]
    return call, messages


def test_equal_size_mtime_external_write_invalidates_preservation_and_new_read_recovers(workspace):
    path = workspace / "current.py"
    path.write_text(source_text(), encoding="utf8")
    context = SourceContext(workspace)
    _, messages = read_receipt(workspace, context)
    old = path.stat()
    path.write_bytes(path.read_bytes().replace(b"result = 1", b"result = 9"))
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    projected, preserved = context.project(messages)
    assert preserved == set()
    assert projected[1]["content"].startswith("[源码版本已变化]")
    _, fresh = read_receipt(workspace, context, "fresh")
    projected, preserved = context.project(messages + fresh)
    assert preserved == {"fresh"}
    assert "result = 9" in projected[-1]["content"]


def test_budget_is_still_enforced_and_omitted_middle_is_never_counted_as_known(workspace):
    (workspace / "current.py").write_text(source_text(), encoding="utf8")
    context = SourceContext(workspace)
    call, messages = read_receipt(workspace, context)
    projected, preserved = context.project(messages)
    clipped = compact_messages(projected, 1800, preserve_tool_ids=preserved)
    assert messages_size_chars(clipped) <= 1800
    assert TOOL_OMIT_MARKER in clipped[-1]["content"]
    assert context.reminder(clipped, {call.id: call}) is None
    assert not context.progress.has_observations


class ShellReader(Tool):
    name = "bash"
    description = "Read fixture output through the real tool dispatcher"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    async def execute(self, arguments, ctx):
        if arguments["command"] == "unknown-validation-command":
            return "exit_code=0\nvalidation finished\n"
        return "exit_code=0\n" + (ctx.workspace / "current.py").read_text(encoding="utf8")


def test_alternating_native_and_shell_reads_remind_without_blocking_tools_or_forcing_final(workspace):
    (workspace / "current.py").write_text("value = 1\n", encoding="utf8")
    script = [OfflineProvider.tool_call_turn("read_file", {"path": "current.py"}),
              OfflineProvider.tool_call_turn("bash", {"command": "cat current.py"}),
              OfflineProvider.tool_call_turn("bash", {"command": "unknown-validation-command"}),
              OfflineProvider.tool_call_turn("read_file", {"path": "./current.py"}),
              OfflineProvider.tool_call_turn("bash", {"command": "sed -n '1,1p' current.py"}),
              OfflineProvider.tool_call_turn("edit_file", {"path": "current.py", "old_string": "value = 1", "new_string": "value = 2"}),
              OfflineProvider.text_turn("已完成")]

    class Reader(OfflineProvider):
        async def chat(self, messages, tools):
            assert {row["name"] for row in tools} == {"bash", "read_file", "edit_file"}
            if self.calls == 5:
                assert any("[执行提示]" in str(message.get("content")) for message in messages)
            return await super().chat(messages, tools)

    loop = make_loop(workspace, Reader(script=script), ShellReader())
    result = asyncio.run(loop.run("实现功能并验证"))
    assert result.turns == 7 and result.text == "已完成"
    notices = [event for event in loop.transcript.replay(loop.session_id) if event.payload.get("kind") == "source_read_reminder"]
    assert len(notices) == 1
    assert (workspace / "current.py").read_text(encoding="utf8") == "value = 2\n"


def test_new_ranges_are_progress_and_denied_read_never_enters_preserved_source(workspace):
    (workspace / "current.py").write_text("\n".join(f"row_{n} = {n}" for n in range(30)), encoding="utf8")
    context = SourceContext(workspace)
    for index in range(5):
        call, messages = read_receipt(workspace, context, f"read-{index}", {"path": "current.py", "offset": 1 + index * 5, "limit": 5})
        assert context.reminder(messages, {call.id: call}) is None
        assert context.progress.stalled_turns == 0
    denied = ToolCall(id="denied", name="read_file", arguments={"path": "current.py"})
    context.capture(denied, "[permission denied] blocked", context.begin(denied))
    context.delivered(denied.id, "[permission denied] blocked")
    assert denied.id not in context.receipts


def test_same_version_repeated_full_reads_do_not_evict_other_current_source(workspace):
    source = source_text()
    (workspace / "current.py").write_text(source, encoding="utf8")
    (workspace / "other.py").write_text(source.replace("result = 1", "result = 7"), encoding="utf8")
    context = SourceContext(workspace)
    _, messages = read_receipt(workspace, context, "other", {"path": "other.py"})
    for index in range(4):
        _, pair = read_receipt(workspace, context, f"repeat-{index}")
        messages += pair
    fitted = context.fit(messages, 24_000)
    bodies = {m["tool_call_id"]: m["content"] for m in fitted if m.get("role") == "tool"}
    assert bodies["other"].replace("\r\n", "\n") == source.replace("result = 1", "result = 7")
    assert bodies["repeat-3"].replace("\r\n", "\n") == source
    assert sum("MIDDLE_EDIT_ANCHOR" in body for body in bodies.values()) == 2
    assert messages_size_chars(fitted) < 20_000


def test_more_than_24_repeated_windows_do_not_evict_independent_full_sources(workspace):
    (workspace / "current.py").write_text(source_text(), encoding="utf8")
    (workspace / "other.py").write_text("INDEPENDENT_SOURCE\n", encoding="utf8")
    context = SourceContext(workspace)
    read_receipt(workspace, context, "other", {"path": "other.py"})
    read_receipt(workspace, context, "whole")
    for index in range(43):
        read_receipt(workspace, context, f"window-{index}", {"path": "current.py", "offset": 105, "limit": 3})
        assert len(context.receipts) <= 24
    assert {"other", "whole", "window-42"}.issubset(context.receipts)


def test_independent_snapshot_keeps_real_multi_file_source_under_protocol_pressure(workspace):
    from code_agent.models.events import ProviderPayload
    from code_agent.agent.source_snapshot_context import is_source_snapshot

    sizes = {"implementation": 6350, "regression": 7540, "acceptance": 9999, "api": 800}
    context = SourceContext(workspace)
    messages = [{"role": "user", "content": "Implement, add regressions and verify. " * 36}]
    calls = {}
    for key, size in sizes.items():
        (workspace / f"{key}.py").write_text(key[0] * size, encoding="utf8")
        call, pair = read_receipt(workspace, context, key, {"path": f"{key}.py"})
        calls[key] = call
        pair[0]["_provider_payload"] = ProviderPayload("private protocol " * 100)
        messages += pair
    test_call = {"id": "verify", "type": "function", "function": {"name": "bash", "arguments": '{"command":"pytest -q"}'}}
    messages += [{"role": "assistant", "content": None, "tool_calls": [test_call],
                  "_provider_payload": ProviderPayload("verification protocol")},
                 {"role": "tool", "tool_call_id": "verify", "content": "69 passed in 1.0s"}]
    fitted = context.fit(messages, 24_000)
    snapshots = [m for m in fitted if is_source_snapshot(m)]
    assert len(snapshots) == 1
    assert {"api", "implementation", "regression"}.issubset(context.snapshot_complete)
    assert "acceptance" not in context.snapshot_complete
    assert "i" * 6350 in snapshots[0]["content"]
    assert "r" * 7540 in snapshots[0]["content"]
    assert TOOL_OMIT_MARKER in snapshots[0]["content"]
    assert any(m.get("tool_call_id") == "verify" and m["content"] == "69 passed in 1.0s" for m in fitted)
    assert messages_size_chars([m for m in fitted if m.get("role") != "system"]) <= 24_000
    context.reminder(fitted, calls)
    assert {"api", "implementation", "regression"}.issubset(context.seen)
    assert "acceptance" not in context.seen
    # An auto continuation only rebuilds one current snapshot; external changes
    # remove old source even if a previous auto data block is supplied again.
    (workspace / "implementation.py").write_text("CHANGED\n", encoding="utf8")
    rebuilt = context.fit(messages + snapshots, 24_000)
    assert sum(is_source_snapshot(m) for m in rebuilt) <= 1
    assert not any("i" * 6350 in m.get("content", "") for m in rebuilt if is_source_snapshot(m))
    assert "implementation" not in context.snapshot_complete


def test_complete_read_covers_window_without_claiming_disjoint_windows_are_whole(workspace):
    (workspace / "current.py").write_text(source_text(), encoding="utf8")
    context = SourceContext(workspace)
    _, messages = read_receipt(workspace, context, "whole")
    _, pair = read_receipt(workspace, context, "window", {"path": "current.py", "offset": 70, "limit": 90})
    fitted = context.fit(messages + pair, 24_000)
    bodies = {m["tool_call_id"]: m["content"] for m in fitted if m.get("role") == "tool"}
    assert bodies["whole"].replace("\r\n", "\n") == source_text()
    assert bodies["window"].startswith("[源码重复]")
    assert "whole" in bodies["window"]

    context = SourceContext(workspace)
    _, first = read_receipt(workspace, context, "first", {"path": "current.py", "offset": 1, "limit": 5})
    _, second = read_receipt(workspace, context, "second", {"path": "current.py", "offset": 100, "limit": 5})
    fitted = context.fit(first + second, 24_000)
    assert results(fitted) == results(first + second)
    assert not context._covers("first", "second") and not context._covers("second", "first")


def test_oversized_covering_read_falls_back_to_real_narrow_view_within_budget(workspace):
    (workspace / "current.py").write_text(source_text(), encoding="utf8")
    context = SourceContext(workspace)
    _, messages = read_receipt(workspace, context, "whole")
    _, window = read_receipt(workspace, context, "window", {"path": "current.py", "offset": 104, "limit": 5})
    fitted = context.fit(messages + window, 1100)
    assert messages_size_chars(fitted) <= 1100
    assert any(m.get("tool_call_id") == "window" and m["content"] == window[-1]["content"] for m in fitted)
    assert not any("[源码重复]" in body for body in results(fitted))


def test_byte_capped_read_does_not_cover_previously_read_missing_middle(workspace):
    (workspace / "current.py").write_text(source_text(), encoding="utf8")
    context = SourceContext(workspace)
    _, window = read_receipt(workspace, context, "middle", {"path": "current.py", "offset": 104, "limit": 5})
    _, capped = read_receipt(workspace, context, "capped", {"path": "current.py", "max_bytes": 100})
    fitted = context.fit(window + capped, 4000)
    assert not context._covers("capped", "middle")
    assert any(m.get("tool_call_id") == "middle" and m["content"] == window[-1]["content"] for m in fitted)


def test_single_file_grep_keeps_real_partial_source_and_directory_search_is_not_cached(workspace):
    (workspace / "current.py").write_text(source_text(), encoding="utf8")
    context = SourceContext(workspace)
    call = ToolCall(id="search", name="grep", arguments={"path": "current.py", "pattern": "MIDDLE_EDIT_ANCHOR", "context": 40})
    before = context.begin(call)
    result = asyncio.run(GrepTool().execute(call.arguments, ToolContext("source-search", workspace)))
    assert len(result) > 2000
    context.capture(call, result, before)
    context.delivered(call.id, str(result))
    pair = [{"role": "assistant", "content": None, "tool_calls": [{"id": call.id, "type": "function", "function": {"name": "grep", "arguments": '{}'}}]},
            {"role": "tool", "tool_call_id": call.id, "content": str(result)}]
    fitted = context.fit(pair, 24_000)
    assert results(fitted) == [str(result)]
    assert context.receipts[call.id].coverage[(workspace / "current.py").resolve()] == frozenset(range(66, 147))
    directory = ToolCall(id="directory", name="grep", arguments={"path": ".", "pattern": "MIDDLE_EDIT_ANCHOR", "context": 1})
    result = asyncio.run(GrepTool().execute(directory.arguments, ToolContext("source-search", workspace)))
    context.capture(directory, result, context.begin(directory))
    assert directory.id not in context.receipts


def test_folded_grep_retains_search_limits_clamping_and_actual_match_coordinates(workspace):
    (workspace / "current.py").write_text(source_text(), encoding="utf8")
    context = SourceContext(workspace)
    _, messages = read_receipt(workspace, context, "whole")
    call = ToolCall(id="search", name="grep", arguments={"path": "current.py", "pattern": "MIDDLE_EDIT_ANCHOR",
                    "context": 100, "max_results": 1})
    before = context.begin(call)
    result = asyncio.run(GrepTool().execute(call.arguments, ToolContext("source-search", workspace)))
    context.capture(call, result, before)
    context.delivered(call.id, str(result))
    pair = [{"role": "assistant", "content": None, "tool_calls": [{"id": call.id, "type": "function",
             "function": {"name": "grep", "arguments": '{}'}}]},
            {"role": "tool", "tool_call_id": call.id, "content": str(result)}]
    fitted = context.fit(messages + pair, 24_000)
    body = next(m["content"] for m in fitted if m.get("tool_call_id") == "search")
    assert body.startswith(str(result).splitlines()[0])
    assert "上限 1" in body and "requested_context=100, applied_context=40" in body
    assert "current.py:106" in body
    assert "[源码重复]" in body and "whole" in body
    assert "MIDDLE_EDIT_ANCHOR" not in body


def test_duplicate_folding_refills_budget_with_previously_dropped_source(workspace):
    (workspace / "current.py").write_text(source_text(), encoding="utf8")
    other = "# another source\n" + "x" * 5983
    (workspace / "other.py").write_text(other, encoding="utf8")
    context = SourceContext(workspace)
    _, old = read_receipt(workspace, context, "other", {"path": "other.py"})
    _, whole = read_receipt(workspace, context, "whole")
    _, window = read_receipt(workspace, context, "window", {"path": "current.py", "offset": 30, "limit": 130})
    # Parallel results are atomic; ordinary 2000-char pruning of this duplicate
    # used to keep the old independent source outside the 16k history budget.
    assistant = {**whole[0], "tool_calls": whole[0]["tool_calls"] + window[0]["tool_calls"]}
    messages = old + [assistant, whole[1], window[1]]
    fitted = context.fit(messages, 16_000)
    bodies = {m["tool_call_id"]: m["content"] for m in fitted if m.get("role") == "tool"}
    assert bodies["whole"].replace("\r\n", "\n") == source_text()
    assert bodies["other"].replace("\r\n", "\n") == other
    assert bodies["window"].startswith("[源码重复]") and "whole" in bodies["window"]
    assert messages_size_chars(fitted) <= 16_000
    assert len(fitted[-3]["tool_calls"]) == 2
