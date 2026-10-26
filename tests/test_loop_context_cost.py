"""Working-context regressions with recorded fake payloads, no network/models."""

import asyncio
import copy
import json

from code_agent.agent.loop import AgentLoop
from code_agent.context.compact import messages_size_chars
from code_agent.context.prompt import assemble, build_history, dynamic_history_budget, provider_messages
from code_agent.models.events import Event, EventType, ModelTurn, ToolCall, assistant_message, tool_message, user_message
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.tools.registry import ToolRegistry


def make_loop(workspace, **kwargs):
    return AgentLoop(provider=OfflineProvider(script=[OfflineProvider.text_turn("finished")]),
                     registry=ToolRegistry(), permissions=PermissionManager(workspace),
                     transcript=TranscriptStore(workspace.parent / "cost-transcripts"),
                     workspace=workspace, **kwargs)


def test_large_capacity_is_ceiling_not_working_context_target(monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "24000")
    assert dynamic_history_budget(1_000_000, system_chars=10_000, tools_chars=20_000) == 24_000
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "48000")
    assert dynamic_history_budget(1_000_000) == 48_000
    assert dynamic_history_budget(1000, system_chars=220, max_output_tokens=800) <= 220
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "0")
    assert dynamic_history_budget(1_000_000) == 0


def test_tool_argument_cost_triggers_fit_without_double_counting_system(workspace):
    loop = make_loop(workspace, history_max_chars=4000)
    messages = [{"role": "system", "content": "fixed instructions " * 1000}, user_message("Task; keep file A unchanged")]
    for index in range(10):
        call = ToolCall(id=f"c{index}", name="read_file", arguments={"path": "file", "selection": "x" * 600})
        messages += [assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, "ok")]
    original = copy.deepcopy(messages)
    fitted = loop._fit_messages(messages)
    assert messages_size_chars(m for m in messages if m["role"] != "system") > 4000
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 4000
    assert fitted[0] == messages[0]
    assert original == messages
    short = messages[:2]
    assert loop._fit_messages(short) is short


def test_automatic_history_is_bounded_without_muting_generated_instruction(workspace):
    history = build_history([
        Event(seq=1, type=EventType.user_message, payload={"text": "Original task: no database edits"}),
        Event(seq=2, type=EventType.user_message, payload={"text": "Continue from known evidence " * 200, "auto": True}),
        Event(seq=3, type=EventType.user_message, payload={"text": "Also preserve the public API"}),
    ])
    assert history[1]["_context_source"] == "auto"
    loop = make_loop(workspace, history_max_chars=1000)
    fitted = loop._fit_messages(assemble("system", [], history, "Continue actual task", max_chars=1000, automatic=True))
    assert any(m.get("content") == "Original task: no database edits" for m in fitted)
    assert any(m.get("content") == "Also preserve the public API" for m in fitted)
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 1000
    outgoing = provider_messages(fitted)
    assert all("_context_source" not in m for m in outgoing)
    assert [m.get("role") for m in outgoing] == [m.get("role") for m in fitted]


def test_source_bookkeeping_never_reaches_provider(workspace):
    loop = make_loop(workspace)
    received = []
    original_stream = loop.provider.stream

    async def record(messages, tools):
        received.append(copy.deepcopy(messages))
        async for chunk in original_stream(messages, tools):
            yield chunk

    loop.provider.stream = record
    result = asyncio.run(loop.run("Finish the original task", context_meta={"auto": True}))
    assert result.text == "finished"
    assert received and all("_context_source" not in m for m in received[0])
    assert received[0][-1]["role"] == "user"
    assert received[0][-1]["content"] == "Finish the original task"


def test_rich_image_evidence_is_not_converted_to_a_string(workspace):
    loop = make_loop(workspace, history_max_chars=100)
    content = [{"type": "text", "text": "external evidence"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200}}]
    messages = [user_message("Inspect this evidence"), {"role": "user", "content": content, "_context_source": "image"}]
    fitted = loop._fit_messages(messages)
    assert fitted[-1]["content"] == content
    assert provider_messages(fitted)[-1] == {"role": "user", "content": content}


def test_synthetic_long_loop_payload_is_smaller_and_keeps_constraints(workspace, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "24000")
    messages = [user_message("Change only one module. Keep authentication and approvals.")]
    for index in range(40):
        call = ToolCall(id=f"c{index}", name="read_file", arguments={"path": f"file{index}"})
        messages += [assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, "source evidence\n" + "details " * 1800 + "\nexit_code=0")]
    loop = make_loop(workspace, history_max_chars=dynamic_history_budget(1_000_000))
    before = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    fitted = loop._fit_messages(messages)
    after = len(json.dumps(provider_messages(fitted), ensure_ascii=False).encode("utf-8"))
    assert after < before // 8
    assert messages[0] in fitted
    assert any("exit_code=0" in (m.get("content") or "") for m in fitted)


def _development_source_history(workspace, *, budget):
    from code_agent.agent.task_progress import TaskProgress

    loop = make_loop(workspace, history_max_chars=budget)
    loop.task_progress = TaskProgress(workspace, "修复 pricing.py 的计算错误并运行测试。")
    source = "def price(value):\n" + "    # existing implementation detail\n" * 160 + "    return value * 2\n"
    args = {"path": "pricing.py"}
    assert loop.task_progress.before_tool("read_file", args) is None
    loop.task_progress.observe("read_file", args, source)
    for _ in range(loop.task_progress.MUTATION_FOCUS_TURNS):
        loop.task_progress.finish_turn()
    call = ToolCall(id="source", name="read_file", arguments=args)
    messages = [user_message("修复 pricing.py 的计算错误并运行测试。"),
                assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, source)]
    for index in range(8):
        call = ToolCall(id=f"log{index}", name="bash", arguments={"command": "test diagnostic"})
        messages += [assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, "diagnostic\n" * 900)]
    return loop, messages, source


def test_active_source_survives_history_fitting_as_original_tool_evidence(workspace):
    loop, messages, source = _development_source_history(workspace, budget=14000)
    original = copy.deepcopy(messages)
    fitted = loop._fit_messages(messages)
    assert next(m for m in fitted if m.get("tool_call_id") == "source")["content"] == source
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 14000
    assert source not in "".join(str(m.get("content", "")) for m in fitted if m["role"] == "system")
    assert original == messages
    assert loop.task_progress.before_tool("read_file", {"path": "pricing.py", "offset": 30, "limit": 40})


def test_budget_eviction_allows_only_one_bounded_source_restore(workspace):
    loop, messages, source = _development_source_history(workspace, budget=1600)
    before = loop.task_progress.turns_without_delivery
    fitted = loop._fit_messages(messages)
    assert all(m.get("content") != source for m in fitted if m["role"] == "tool")
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 1600
    assert loop.task_progress.before_tool("read_file", {"path": "pricing.py"})
    args = {"path": "pricing.py", "offset": 40, "limit": 60}
    assert loop.task_progress.before_tool("read_file", args) is None
    loop.task_progress.observe("read_file", args, "pricing.py 第40行\n40\t    return value * 2")
    loop._fit_messages(messages)  # Reprocessing the same evicted receipt grants no new allowance.
    assert loop.task_progress.before_tool("read_file", args)
    assert loop.task_progress.turns_without_delivery == before


def test_actual_denial_survives_history_compaction_as_untrusted_feedback(workspace):
    from code_agent.agent.task_progress import TaskProgress

    loop = make_loop(workspace, history_max_chars=4000)
    loop.task_progress = TaskProgress(workspace, "修复计算错误并实际验证。")
    loop.tool_feedback.observe("bash", {"command": "cat ../private.py"},
                               "[permission denied] 请用 read_file/grep 读取和定位源码")
    messages = [user_message("修复计算错误并实际验证。")]
    for index in range(10):
        call = ToolCall(id=f"noise{index}", name="grep", arguments={"pattern": "x", "path": "."})
        messages += [assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, "evidence " * 900)]
    fitted = loop._fit_messages(messages)
    context = next(m['content'] for m in fitted if m.get('_execution_progress'))
    assert '本任务实际工具调用反馈' in context and 'native_read' in context
    assert '【数据块开始' in context and '【数据块结束' in context
    assert messages_size_chars(m for m in fitted if m['role'] != 'system') <= 4000


def test_feedback_and_failed_validation_share_bounded_request_context(workspace):
    from code_agent.agent.task_progress import TaskProgress

    loop = make_loop(workspace, history_max_chars=12000)
    task = loop.task_progress = TaskProgress(workspace, "修复 a.py 的计算错误并测试。")
    task.observe('read_file', {'path':'a.py'}, 'x = 1')
    task.record_validation_failure(['a.py'], 'exit_code=1\nFile "a.py", line 4\nSyntaxError: invalid syntax')
    loop.tool_feedback.observe('bash', {'command':'cat a.py'}, '[permission denied] 请用 read_file/grep 读取和定位源码')
    fitted = loop._fit_messages([user_message('修复 a.py 的计算错误并测试。')])
    context = next(m['content'] for m in fitted if m.get('_execution_progress'))
    assert 'SyntaxError' in context and '本任务实际工具调用反馈' in context
    assert '[开发交付]' in context and '相关完整验收真实通过' in context
    assert context.count('【数据块开始') == context.count('【数据块结束')
    assert len(context) <= 2200


def test_old_source_version_does_not_displace_current_source_when_fitting(workspace):
    from code_agent.agent.task_progress import TaskProgress

    loop = make_loop(workspace, history_max_chars=8000)
    task = loop.task_progress = TaskProgress(workspace, "修复 pricing.py 并测试。")
    old = 'def old_price():\n' + '    # old implementation\n' * 220
    new = 'def fixed_price():\n' + '    # actual new implementation\n' * 130
    task.observe('read_file', {'path':'pricing.py'}, old)
    old_call = ToolCall(id='old',name='read_file',arguments={'path':'pricing.py'})
    key = task._path('pricing.py')
    loop._source_versions['old'] = (key,0)
    task.observe('edit_file', {'path':'pricing.py','old_string':old,'new_string':new},'已修改',changed_paths=['pricing.py'])
    task.observe('read_file', {'path':'pricing.py'}, new)
    loop._source_versions['new'] = (key,1)
    new_call = ToolCall(id='new',name='read_file',arguments={'path':'pricing.py'})
    messages=[user_message('修复 pricing.py 并测试。'),assistant_message(ModelTurn(tool_calls=[old_call])),tool_message('old',old),
              assistant_message(ModelTurn(tool_calls=[new_call])),tool_message('new',new)]
    for i in range(8):
        call=ToolCall(id=f'check{i}',name='grep',arguments={'pattern':'diagnostic','path':'pricing.py'})
        messages += [assistant_message(ModelTurn(tool_calls=[call])),tool_message(call.id,'diagnostic '*1200)]
    fitted=loop._fit_messages(messages)
    assert next(m for m in fitted if m.get('tool_call_id')=='new')['content']==new
    assert all(m.get('content')!=old for m in fitted if m.get('role')=='tool')
    assert 'old' not in loop._task_read_context(messages)


def test_small_native_new_file_write_remains_current_source_evidence(workspace):
    from code_agent.agent.task_progress import TaskProgress
    from code_agent.tools.base import ToolContext

    loop=make_loop(workspace,history_max_chars=7000)
    loop.task_progress=TaskProgress(workspace,'新增 quote.py 的报价功能并测试。')
    source='def quote():\n'+'    # implementation\n'*180+'    return 42\n'
    call=ToolCall(id='write',name='write_file',arguments={'path':'quote.py','content':source})
    loop._tool_effects[call.id]=(('quote.py',),())
    loop._record_tool_result(call,'已写入 quote.py',ToolContext(loop.session_id,workspace))
    assert loop.task_progress.before_tool('edit_file',{'path':'quote.py','old_string':'return 42','new_string':'return 43'}) is None
    messages=[user_message('新增 quote.py 的报价功能并测试。'),assistant_message(ModelTurn(tool_calls=[call])),tool_message(call.id,'已写入 quote.py')]
    for i in range(8):
        noise=ToolCall(id=f'noise{i}',name='grep',arguments={'pattern':'x','path':'.'})
        messages += [assistant_message(ModelTurn(tool_calls=[noise])),tool_message(noise.id,'evidence '*1000)]
    fitted=loop._fit_messages(messages)
    outgoing=next(c for m in fitted for c in m.get('tool_calls',[]) if c['id']=='write')
    assert json.loads(outgoing['function']['arguments'])['content']==source


def test_evicted_long_native_write_keeps_two_bounded_restore_windows(workspace):
    from code_agent.agent.task_progress import TaskProgress
    from code_agent.tools.builtin.tools_builtin import WriteFileTool

    loop = make_loop(workspace, history_max_chars=1800)
    loop.task_progress = task = TaskProgress(workspace, "新增 generated.py 实现功能并实际验证。")
    loop.registry.register(WriteFileTool())
    source = "\n".join(f"value_{i} = {i}  # generated implementation detail" for i in range(1, 401)) + "\n"
    assert len(source) > 10000
    call = ToolCall(id="large_write", name="write_file", arguments={"path": "generated.py", "content": source})
    ctx = ToolContext(loop.session_id, workspace)
    result = asyncio.run(loop._execute_tool(call, ctx))
    assert not result.startswith(("[tool error]", "[permission denied]"))
    loop._record_tool_result(call, result, ctx)
    messages = [user_message("新增 generated.py 实现功能并实际验证。"),
                assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, result)]
    assert loop._task_read_context(messages) == {"large_write": "generated.py"}
    fitted = loop._fit_messages(_pressure_history(messages))
    assert messages_size_chars(m for m in fitted if m.get("role") != "system") <= 1800
    assert not any(c["id"] == "large_write" for m in fitted for c in m.get("tool_calls", []))
    key = task._path("generated.py")
    assert task._context_restore_reads[key] == 2
    before = task.turns_without_delivery
    head = _read_reference_receipt(loop, workspace / "generated.py", "head", offset=1, limit=80)
    assert not head[-1]["content"].startswith("[tool error]")
    tail = _read_reference_receipt(loop, workspace / "generated.py", "tail", offset=121, limit=70)
    assert "190\tvalue_190" in tail[-1]["content"]
    loop._fit_messages(messages)
    assert not task._context_restore_reads.get(key)
    assert task.before_tool("read_file", {"path": "generated.py", "offset": 251, "limit": 50})
    assert task.turns_without_delivery == before and not task.ready_to_finish


def test_repeated_team_calls_keep_valid_roster_and_deliver_separate_notice(workspace):
    from code_agent.agent.repeat_reminder import RepeatReminder
    from code_agent.permissions.policy import ReadOnlyPolicy
    from code_agent.tools.base import Tool

    class TeamFixture(Tool):
        name = "team_list"
        async def execute(self, arguments, ctx):
            return json.dumps({"agents": [{"session_id": "worker", "activation_id": 3, "state": "completed"}],
                               "execution_reader": {"tool": "read_agent_execution", "parameters_from": ["session_id", "activation_id"]}})

    class CapturedProvider(OfflineProvider):
        received = []
        async def chat(self, messages, tools):
            self.received.append(copy.deepcopy(messages))
            return await super().chat(messages, tools)

    script = [OfflineProvider.tool_call_turn("team_list", {}) for _ in range(3)] + [OfflineProvider.text_turn("finished")]
    loop = make_loop(workspace, repeat_reminder=RepeatReminder(thresholds=(3,)))
    loop.provider = CapturedProvider(script=script)
    loop.registry.register(TeamFixture())
    loop.permissions.register_policy("team_list", ReadOnlyPolicy())
    assert asyncio.run(loop.run("Check existing team status")).text == "finished"
    results = [ev.payload["result"] for ev in loop.transcript.replay(loop.session_id) if ev.type == EventType.tool_result]
    assert len(results) == 3 and all(json.loads(text)["agents"][0]["session_id"] == "worker" for text in results)
    outgoing = loop.provider.received[-1]
    assert any(m.get("role") == "user" and "连续第 3 次" in m.get("content", "") for m in outgoing)
    assert all("_context_source" not in message for message in outgoing)
    rebuilt = build_history(loop.transcript.replay(loop.session_id))
    assert len([m for m in rebuilt if m.get("role") == "tool"]) == 3


def _read_reference_receipt(loop, path, call_id, **window):
    """Exercise the real native read and permission boundary, not a fake grant."""
    from code_agent.tools.base import ToolContext
    from code_agent.tools.builtin.tools_builtin import ReadFileTool

    if loop.registry.get("read_file") is None:
        loop.registry.register(ReadFileTool())
    call = ToolCall(id=call_id, name="read_file", arguments={"path": str(path), **window})
    ctx = ToolContext(loop.session_id, loop.workspace)
    result = asyncio.run(loop._execute_tool(call, ctx))
    result = loop._record_tool_result(call, result, ctx)
    return [assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, result)]


def _pressure_history(messages):
    for index in range(9):
        noise = ToolCall(id=f"pressure{index}", name="bash", arguments={"command": "test diagnostic"})
        messages += [assistant_message(ModelTurn(tool_calls=[noise])), tool_message(noise.id, "diagnostic " * 900)]
    return messages


def test_successful_external_reference_survives_pressure_without_becoming_a_write_target(workspace):
    from code_agent import config
    from code_agent.agent.task_progress import TaskProgress

    loop = make_loop(workspace, history_max_chars=14000)
    task = loop.task_progress = TaskProgress(workspace, "只修改 implementation.py 修复计算错误并验证。")
    targets = dict(task.targets)
    path = config.skills_dir() / "reference.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    source = "def expected_behavior():\n" + "    # immutable reference behavior\n" * 160 + "    return 42\n"
    path.write_text(source, encoding="utf-8")
    messages = [user_message("只修改 implementation.py 修复计算错误并验证。")]
    messages += _read_reference_receipt(loop, path, "reference")
    assert not messages[-1]["content"].startswith("[tool error]")
    received = messages[-1]["content"]
    assert received.replace("\r\n", "\n") == source
    fitted = loop._fit_messages(_pressure_history(messages))
    assert next(m for m in fitted if m.get("tool_call_id") == "reference")["content"] == received
    assert task.context_reference_targets == (task._path(str(path)),)
    assert dict(task.targets) == targets and not task.ready_to_finish
    assert task.before_tool("edit_file", {"path": str(path)})
    assert not task.state()["changed"] and not task.state()["verified"]
    assert source not in "".join(str(m.get("content", "")) for m in fitted if m["role"] == "system")
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 14000


def test_external_reference_slot_is_single_and_large_source_prefers_bounded_current_windows(workspace):
    from code_agent import config
    from code_agent.agent.task_progress import TaskProgress

    loop = make_loop(workspace, history_max_chars=13000)
    task = loop.task_progress = TaskProgress(workspace, "修复 implementation.py 并测试。")
    folder = config.skills_dir()
    folder.mkdir(parents=True, exist_ok=True)
    first, latest = folder / "first.py", folder / "latest.py"
    first.write_text("first reference\n" * 100, encoding="utf-8")
    latest.write_text("line = 'reference behavior'\n" * 900, encoding="utf-8")
    messages = [user_message("修复 implementation.py 并测试。")]
    messages += _read_reference_receipt(loop, first, "first")
    messages += _read_reference_receipt(loop, latest, "large")
    messages += _read_reference_receipt(loop, latest, "window1", offset=100, limit=30)
    window1 = messages[-1]["content"]
    messages += _read_reference_receipt(loop, latest, "window2", offset=200, limit=30)
    window2 = messages[-1]["content"]
    assert task.context_reference_targets == (task._path(str(latest)),)
    fitted = loop._fit_messages(_pressure_history(messages))
    assert next(m for m in fitted if m.get("tool_call_id") == "window1")["content"] == window1
    assert next(m for m in fitted if m.get("tool_call_id") == "window2")["content"] == window2
    assert all(m.get("content") != latest.read_text(encoding="utf-8") for m in fitted if m["role"] == "tool")
    assert messages_size_chars(m for m in fitted if m["role"] != "system") <= 13000


def test_evicted_external_reference_restores_once_without_bypassing_read_permissions(workspace):
    from code_agent import config
    from code_agent.agent.task_progress import TaskProgress

    loop = make_loop(workspace, history_max_chars=1800)
    task = loop.task_progress = TaskProgress(workspace, "修复 implementation.py 并验证。")
    folder = config.skills_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "reference.py"
    path.write_text("value = 42\n" * 700, encoding="utf-8")
    messages = [user_message("修复 implementation.py 并验证。")]
    messages += _read_reference_receipt(loop, path, "reference")
    for _ in range(task.MUTATION_FOCUS_TURNS):
        task.finish_turn()
    before = task.turns_without_delivery
    loop._fit_messages(_pressure_history(messages))
    assert task.before_tool("read_file", {"path": str(path)})
    assert task._context_restore_reads.get(task._path(str(path))) == 2
    restored = _read_reference_receipt(loop, path, "restored", offset=25, limit=40)
    assert not restored[-1]["content"].startswith("[tool error]")
    loop._fit_messages(messages)  # Same omitted receipt cannot replenish credit.
    assert task.before_tool("read_file", {"path": str(path), "offset": 25, "limit": 40})
    assert task.turns_without_delivery == before
    denied = workspace.parent / "unapproved-private.py"
    denied.write_text("private = True\n", encoding="utf-8")
    outcome = _read_reference_receipt(loop, denied, "denied")[-1]["content"]
    assert outcome.startswith("[tool error]") and "拒绝" in outcome
    assert task._path(str(denied)) not in task.context_read_targets
    assert task.context_reference_targets == (task._path(str(path)),)


def test_compacted_whole_read_cannot_displace_intact_restore_across_repeated_fits(workspace):
    from code_agent.agent.task_progress import TaskProgress
    from code_agent.context.compact import prune_old_tool_results

    loop = make_loop(workspace, history_max_chars=8000)
    task = loop.task_progress = TaskProgress(workspace, "修复 implementation.py 并实际验证。")
    path = workspace / "implementation.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def price():\n" + "    # existing implementation details\n" * 190 + "    return 42\n", encoding="utf-8")
    messages = [user_message("修复 implementation.py 并实际验证。")]
    messages += _read_reference_receipt(loop, path, "whole")
    # Reproduce an already-clipped whole receipt left by a previous request.
    messages = prune_old_tool_results(messages, keep_last=0, head_chars=180)
    assert "[compact]" in messages[-1]["content"]
    task.mark_compacted_reads("whole", str(path))
    for _ in range(task.MUTATION_FOCUS_TURNS):
        task.finish_turn()
    restored = _read_reference_receipt(loop, path, "restored", offset=60, limit=35)
    original = restored[-1]["content"]
    assert "[tool error]" not in original
    messages += restored
    for turn in range(3):
        noise = ToolCall(id=f"later{turn}", name="grep", arguments={"pattern": "diagnostic", "path": "implementation.py"})
        messages += [assistant_message(ModelTurn(tool_calls=[noise])), tool_message(noise.id, "diagnostic " * 1700)]
        messages = loop._fit_messages(messages)
        assert next(m for m in messages if m.get("tool_call_id") == "restored")["content"] == original
        assert messages_size_chars(m for m in messages if m["role"] != "system") <= 8000


def test_external_reference_cannot_evict_parallel_production_bundle_with_private_protocol_cost(workspace):
    from code_agent import config
    from code_agent.agent.task_progress import TaskProgress
    from code_agent.models.events import ProviderPayload

    loop = make_loop(workspace, history_max_chars=24000)
    task = loop.task_progress = TaskProgress(workspace, "只修改 implementation.py 并新增 tests/new.py，参考 tests/original.py 验证。")
    source, tests = workspace / "implementation.py", workspace / "tests/original.py"
    tests.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def price():\n" + "    # original implementation\n" * 210 + "    return 42\n", encoding="utf-8")
    tests.write_text("# immutable existing test expectations\n" * 190, encoding="utf-8")
    first = _read_reference_receipt(loop, source, "production")
    second = _read_reference_receipt(loop, tests, "existing_tests")
    source_body, test_body = first[-1]["content"], second[-1]["content"]
    calls = [ToolCall(id="production", name="read_file", arguments={"path": str(source)}),
             ToolCall(id="existing_tests", name="read_file", arguments={"path": str(tests)})]
    bundled = assistant_message(ModelTurn(tool_calls=calls))
    bundled["_provider_payload"] = ProviderPayload("opaque protocol state " * 140)
    messages = [user_message("只修改 implementation.py 并新增 tests/new.py，参考 tests/original.py 验证。"),
                bundled, first[-1], second[-1]]
    reference = config.skills_dir() / "acceptance_reference.py"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text("# immutable acceptance requirement\n" * 265, encoding="utf-8")
    messages += _read_reference_receipt(loop, reference, "external")
    assert len(messages[-1]["content"]) < 10000
    for turn in range(3):
        noise = ToolCall(id=f"pressure{turn}", name="grep", arguments={"pattern": "x", "path": "implementation.py"})
        messages += [assistant_message(ModelTurn(tool_calls=[noise])), tool_message(noise.id, "diagnostic " * 1600)]
        messages = loop._fit_messages(messages)
        assert next(m for m in messages if m.get("tool_call_id") == "production")["content"] == source_body
        assert next(m for m in messages if m.get("tool_call_id") == "existing_tests")["content"] == test_body
        assert messages_size_chars(m for m in messages if m["role"] != "system") <= 24000
    assert not task.state()["changed"] and not task.state()["verified"]


def test_three_source_protocol_pressure_allows_two_distinct_restores_to_reach_file_tail(workspace):
    from code_agent.agent.task_progress import TaskProgress
    from code_agent.models.events import ProviderPayload

    loop = make_loop(workspace, history_max_chars=24000)
    task = loop.task_progress = TaskProgress(workspace, "只修改 source.py、api.py，参考 tests/original.py 修复搜索错误。")
    source, tests, api = workspace / "source.py", workspace / "tests/original.py", workspace / "api.py"
    tests.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("\n".join(f"v{i} = {i} # implementation detail" for i in range(1, 171)) + "\n", encoding="utf-8")
    tests.write_text("# existing original test expectations\n" * 200, encoding="utf-8")
    api.write_text("# existing API input and output behavior\n" * 500, encoding="utf-8")
    first = _read_reference_receipt(loop, source, "source")
    second = _read_reference_receipt(loop, tests, "original_tests")
    calls = [ToolCall(id="source", name="read_file", arguments={"path": str(source)}),
             ToolCall(id="original_tests", name="read_file", arguments={"path": str(tests)})]
    bundled = assistant_message(ModelTurn(tool_calls=calls))
    bundled["_provider_payload"] = ProviderPayload("opaque source protocol " * 150)
    messages = [user_message("只修改 source.py、api.py，参考 tests/original.py 修复搜索错误。"),
                bundled, first[-1], second[-1]]
    third = _read_reference_receipt(loop, api, "api", offset=420, limit=80)
    third[0]["_provider_payload"] = ProviderPayload("opaque API protocol " * 350)
    messages += third
    # Exercise the fallback for source that has no independent bounded cache
    # (large/unsupported reads). Native cache retention has separate integration
    # coverage and correctly avoids granting restore credits when still visible.
    loop._edit_source_memory.clear()
    loop._source_memory_stats.clear()
    for turn in range(3):
        noise = ToolCall(id=f"later_source_pressure{turn}", name="grep", arguments={"pattern": "x", "path": "api.py"})
        messages += [assistant_message(ModelTurn(tool_calls=[noise])), tool_message(noise.id, "diagnostic " * 1600)]
        messages = loop._fit_messages(messages)
        assert messages_size_chars(m for m in messages if m["role"] != "system") <= 24000
    assert not any(m.get("content") == first[-1]["content"] for m in messages if m["role"] == "tool")
    assert task._context_restore_reads.get(task._path(str(source))) == 2
    for _ in range(task.MUTATION_FOCUS_TURNS):
        task.finish_turn()
    before = task.turns_without_delivery
    head = _read_reference_receipt(loop, source, "head", offset=1, limit=120)
    assert not head[-1]["content"].startswith("[tool error]")
    # Repeating the head neither consumes nor bypasses the reserved tail read.
    assert task.before_tool("read_file", {"path": str(source), "offset": 1, "limit": 120})
    assert task._context_restore_reads.get(task._path(str(source))) == 1
    tail = _read_reference_receipt(loop, source, "tail", offset=119, limit=52)
    assert not tail[-1]["content"].startswith("[tool error]")
    assert "170\tv170 = 170" in tail[-1]["content"]
    loop._fit_messages(messages)
    assert task.before_tool("read_file", {"path": str(source), "offset": 141, "limit": 30})
    assert task.turns_without_delivery == before
    assert not task.ready_to_finish and not task.state()["changed"]


def test_focused_menu_savings_retain_real_source_and_reference_without_increasing_total_allowance(workspace, monkeypatch):
    from code_agent import config
    from code_agent.agent.task_progress import TaskProgress
    from code_agent.models.events import ProviderPayload

    monkeypatch.delenv("CODE_AGENT_HISTORY_BUDGET", raising=False)
    loop = make_loop(workspace, history_max_chars=24000, reuse_schema_budget=True)
    prompt = "只修改 implementation.py、api.py 修复搜索功能，参考 tests/original.py 并验证。"
    loop.task_progress = TaskProgress(workspace, prompt)
    sources = [workspace / "implementation.py", workspace / "tests/original.py", workspace / "api.py",
               config.skills_dir() / "immutable_reference.py"]
    messages = [user_message(prompt)]
    originals = {}
    for index, path in enumerate(sources):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((f"# source {index} existing behavior and expectations\n" * 140), encoding="utf8")
        pair = _read_reference_receipt(loop, path, f"evidence{index}")
        assert not pair[-1]["content"].startswith("[tool error]")
        pair[0]["_provider_payload"] = ProviderPayload("actual private protocol " * 75)
        originals[f"evidence{index}"] = pair[-1]["content"]
        messages += pair
    from code_agent.tools.builtin.tools_builtin import WriteFileTool

    loop.registry.register(WriteFileTool())
    actual = loop.registry.schemas()
    extras = [{"type": "function", "function": {"name": name, "description": "optional capability " * 600,
                                                "parameters": {"type": "object", "properties": {}}}}
              for name in ("present", "skill_install", "feature_brief")]
    monkeypatch.setattr(loop.registry, "schemas", lambda: actual + extras)
    full = loop.registry.schemas()
    focused = loop.task_progress.tool_schemas(full)
    original_menu_cost = len(json.dumps(full, ensure_ascii=False))
    focused_menu_cost = len(json.dumps(focused, ensure_ascii=False))
    assert focused_menu_cost < original_menu_cost
    budget = loop._working_history_budget()
    assert 24000 < budget <= 48000
    assert budget + focused_menu_cost <= 24000 + original_menu_cost
    for turn in range(3):
        call = ToolCall(id=f"latest{turn}", name="bash", arguments={"command": "python -m pytest"})
        messages += [assistant_message(ModelTurn(tool_calls=[call])), tool_message(call.id, "diagnostic " * 2500)]
        messages = loop._fit_messages(messages)
        retained = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
        assert all(retained.get(call_id) == body for call_id, body in originals.items())
        assert messages_size_chars(m for m in messages if m["role"] != "system") <= budget
    assert all(count == 1 for count in loop.task_progress._full_reads.values())
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "24000")
    assert loop._working_history_budget() == 24000
    monkeypatch.delenv("CODE_AGENT_HISTORY_BUDGET")
    loop.reuse_schema_budget = False
    assert loop._working_history_budget() == 24000
