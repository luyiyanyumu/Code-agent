"""Offline annotation delivery through real tools, permissions and disk changes.

The provider is scripted; no model service or user project is touched. Validation
checks the actual temporary source's syntax, Chinese docstring and behavior.
"""

from __future__ import annotations

import ast
import asyncio
import copy

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.models.events import EventType, ModelTurn, TokenUsage, ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool
from code_agent.tools.builtin import ReadFileTool
from code_agent.tools.builtin.code_tools import EditFileTool, EditManyTool, GrepTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


TARGET = "arithmetic.py"
SOURCE = "def add(left, right):\n    return left + right\n"
DECLARATION = "def add(left, right):\n"
ANNOTATED = DECLARATION + '    """返回两个数的和，保持输入相加的行为。"""\n'
USAGE = TokenUsage(input_tokens=11, output_tokens=7, cached_tokens=3)


def tool_turn(name, *, call_id=None, stop_reason="tool_use", **arguments):
    details = {"name": name, "arguments": arguments}
    if call_id is not None:
        details["id"] = call_id
    return ModelTurn(tool_calls=[ToolCall(**details)], stop_reason=stop_reason, usage=USAGE)


def read_turn(**arguments):
    return tool_turn("read_file", path=TARGET, **arguments)


def edit_turn(**arguments):
    return tool_turn("edit_many", edits=[{
        "path": TARGET, "old_string": DECLARATION, "new_string": ANNOTATED,
    }], **arguments)


def truncated_edit(call_id):
    # Even a syntactically complete native call on a truncated response must
    # never be executed; its replacement deliberately violates the fixture.
    return tool_turn("edit_many", call_id=call_id, stop_reason="max_tokens", edits=[{
        "path": TARGET, "old_string": "return left + right",
        "new_string": "return 'TRUNCATED CALL MUST NOT RUN'",
    }])


def done_turn():
    return ModelTurn(text="中文注释已写入，语法与行为验证通过。", stop_reason="end_turn", usage=USAGE)


class RecordingProvider(OfflineProvider):
    def __init__(self, turns):
        super().__init__(script=turns)
        self.requests = []

    async def chat(self, messages, tools):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        assert self._script, "The loop unexpectedly requested an extra model round"
        return await super().chat(messages, tools)


class FixtureValidation(Tool):
    name = "bash"
    parameters = {
        "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
    }

    def __init__(self):
        self.commands = []
        self.validated = []

    async def execute(self, arguments, ctx):
        command = arguments["command"]
        self.commands.append(command)
        words = command.split()
        assert words[:3] == ["python", "-m", "py_compile"] and len(words) == 4
        target = words[3]
        text = (ctx.workspace / target).read_text(encoding="utf-8")
        tree = ast.parse(text, filename=target)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
        assert "返回两个数的和" in (ast.get_docstring(function) or "")
        namespace = {}
        exec(compile(tree, target, "exec"), namespace)
        for left, right in ((2, 3), (-4, 7), (0, 0), (1.5, 2.25)):
            assert namespace["add"](left, right) == left + right
        self.validated.append(text)
        return "exit_code=0\nSyntax, Chinese docstring and 4 fixture behavior checks passed"


class RecordingGrep(GrepTool):
    def __init__(self):
        super().__init__()
        self.executions = 0

    async def execute(self, arguments, ctx):
        self.executions += 1
        return await super().execute(arguments, ctx)


@pytest.fixture
def annotation_workspace(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / TARGET).write_text(SOURCE, encoding="utf-8")
    return workspace


def make_loop(workspace, turns, **kwargs):
    provider = RecordingProvider(turns)
    validation, grep = FixtureValidation(), RecordingGrep()
    registry = ToolRegistry()
    for tool in (ReadFileTool(), EditFileTool(), EditManyTool(), validation, grep):
        registry.register(tool)
    permissions = PermissionManager(workspace_root=workspace)
    permissions.register_policy("bash", AllowPolicy())
    loop = AgentLoop(
        provider=provider, registry=registry, permissions=permissions,
        transcript=TranscriptStore(workspace.parent / "annotation-transcripts"), workspace=workspace,
        fs_observation=FsObservation(), **kwargs,
    )
    return loop, provider, validation, grep


def events(loop, kind):
    return [event for event in loop.transcript.replay(loop.session_id) if event.type == kind]


def edit_cap(request):
    schema = next(tool for tool in request["tools"] if tool.get("name") == "edit_many")
    return schema["parameters"]["properties"]["edits"]["maxItems"]


def test_truncated_annotation_edit_recovers_once_with_small_schema_and_real_delivery(annotation_workspace):
    loop, provider, validation, _ = make_loop(annotation_workspace, [
        read_turn(), truncated_edit("discard-first-truncated-edit"), edit_turn(),
        tool_turn("bash", command=f"python -m py_compile {TARGET}"), done_turn(),
    ])

    result = asyncio.run(loop.run(f"给 {TARGET} 的函数加中文注释并验证"))

    assert result.stop_reason == "end_turn" and provider.calls == 5
    assert [edit_cap(request) for request in provider.requests] == [5, 5, 1, 5, 5]
    assert all(request["tools"] for request in provider.requests)
    assert validation.commands == [f"python -m py_compile {TARGET}"]
    assert len(validation.validated) == 1
    assert (annotation_workspace / TARGET).read_text(encoding="utf-8") == SOURCE.replace(DECLARATION, ANNOTATED)
    executed = [event.payload["call"] for event in events(loop, EventType.tool_use)]
    assert [call["name"] for call in executed] == ["read_file", "edit_many", "bash"]
    assert "discard-first-truncated-edit" not in {call["id"] for call in executed}
    assert loop.task_progress.ready_to_finish
    assert not loop.task_progress.compact_editing
    notices = [event for event in events(loop, EventType.error)
               if event.payload.get("code") == "edit_output_truncated"]
    assert len(notices) == 1 and notices[0].payload["discarded_tool_calls"] == 1
    assert result.usage == TokenUsage(input_tokens=55, output_tokens=35, cached_tokens=15)


def test_second_truncated_annotation_response_stops_without_executing_or_replaying_partial_calls(annotation_workspace):
    loop, provider, validation, _ = make_loop(annotation_workspace, [
        read_turn(), truncated_edit("first-incomplete"), truncated_edit("second-incomplete"), edit_turn(),
    ])

    result = asyncio.run(loop.run(f"给 {TARGET} 加中文注释"))

    assert result.stop_reason == "max_tokens" and provider.calls == 3
    assert [edit_cap(request) for request in provider.requests] == [5, 5, 1]
    assert (annotation_workspace / TARGET).read_text(encoding="utf-8") == SOURCE
    assert not validation.commands
    assert [event.payload["call"]["name"] for event in events(loop, EventType.tool_use)] == ["read_file"]
    terminal = events(loop, EventType.done)
    assert len(terminal) == 1 and terminal[0].payload["stop_reason"] == "max_tokens"
    assert terminal[0].payload["discarded_tool_calls"] == 1
    assert len(provider._script) == 1, "The second truncation must not replay or consume a normal edit"


def test_segment_continuation_keeps_the_single_output_recovery_budget(annotation_workspace):
    loop, provider, validation, _ = make_loop(annotation_workspace, [
        read_turn(), truncated_edit("before-segment-boundary"),
        truncated_edit("after-segment-boundary"), edit_turn(),
    ], max_turns=2)

    async def scenario():
        first = await loop.run(f"给 {TARGET} 加中文注释")
        assert first.stop_reason == "max_turns" and first.segment_boundary
        assert loop._output_repair_used and loop.task_progress.compact_editing
        progress = loop.task_progress
        loop.max_turns = 4
        second = await loop.run("继续执行未完成任务", context_meta={"auto": True, "continuation": 1})
        assert second.stop_reason == "max_tokens"
        assert loop.task_progress is progress

    asyncio.run(scenario())
    assert provider.calls == 3 and len(provider._script) == 1
    assert [edit_cap(request) for request in provider.requests] == [5, 5, 1]
    assert (annotation_workspace / TARGET).read_text(encoding="utf-8") == SOURCE
    assert not validation.commands
    assert [event.payload["call"]["name"] for event in events(loop, EventType.tool_use)] == ["read_file"]


def test_annotation_scope_and_implementation_phase_block_discovery_but_allow_edit_recovery(annotation_workspace):
    other = annotation_workspace / "other.py"
    other.write_text("def unrelated():\n    return 10\n", encoding="utf-8")
    original_other = other.read_bytes()
    loop, provider, validation, grep = make_loop(annotation_workspace, [
        read_turn(), read_turn(offset=1, limit=5),
        tool_turn("edit_many", call_id="outside-task-target", edits=[{
            "path": "other.py", "old_string": "return 10", "new_string": "return 20",
        }]),
        tool_turn("grep", call_id="late-investigation", path=".", pattern="def"),
        read_turn(call_id="late-full-read"),
        tool_turn("edit_file", call_id="failed-exact-edit", path=TARGET,
                  old_string="def add_with_wrong_name(left, right):", new_string=ANNOTATED),
        read_turn(call_id="bounded-error-recovery", offset=1, limit=5),
        edit_turn(), tool_turn("bash", command=f"python -m py_compile {TARGET}"), done_turn(),
    ])

    result = asyncio.run(loop.run(f"只给 {TARGET} 加中文注释并验证"))

    assert result.stop_reason == "end_turn" and provider.calls == 10
    assert other.read_bytes() == original_other
    assert grep.executions == 0, "The implementation phase must refuse investigation before tool dispatch"
    results = {event.payload["call_id"]: event.payload["result"]
               for event in events(loop, EventType.tool_result)}
    assert "[task scope]" in results["outside-task-target"] and "超出本次目标范围" in results["outside-task-target"]
    assert "[task scope]" in results["late-investigation"] and "实施阶段" in results["late-investigation"]
    assert "[task scope]" in results["late-full-read"] and "不再重读已知源码" in results["late-full-read"]
    assert results["failed-exact-edit"].startswith("[tool error]")
    assert "[tool error]" not in results["bounded-error-recovery"]
    assert len(validation.validated) == 1 and loop.task_progress.ready_to_finish
    assert (annotation_workspace / TARGET).read_text(encoding="utf-8") == SOURCE.replace(DECLARATION, ANNOTATED)
    assert not loop._edit_failure


def test_revalidating_delivered_version_does_not_mask_pending_file_but_new_version_can_be_verified(annotation_workspace):
    second = "other_arithmetic.py"
    (annotation_workspace / second).write_text(SOURCE, encoding="utf-8")
    both_reads = ModelTurn(tool_calls=[
        ToolCall(name="read_file", arguments={"path": TARGET}),
        ToolCall(name="read_file", arguments={"path": second}),
    ], stop_reason="tool_use", usage=USAGE)
    revised_docstring = ANNOTATED.replace("保持输入相加的行为", "支持整数、负数与小数相加")
    turns = [
        both_reads, edit_turn(),
        tool_turn("bash", call_id="first-a-validation", command=f"python -m py_compile {TARGET}"),
        *[tool_turn("bash", call_id=f"duplicate-a-validation-{index}",
                    command=f"python -m py_compile {TARGET}") for index in range(1, 4)],
        tool_turn("edit_file", call_id="new-a-version", path=TARGET,
                  old_string=ANNOTATED, new_string=revised_docstring),
        tool_turn("bash", call_id="new-a-validation", command=f"python -m py_compile {TARGET}"),
        tool_turn("edit_many", edits=[{
            "path": second, "old_string": DECLARATION, "new_string": ANNOTATED,
        }]),
        tool_turn("bash", call_id="b-validation", command=f"python -m py_compile {second}"),
        done_turn(),
    ]
    loop, provider, validation, _ = make_loop(annotation_workspace, turns)
    progress_before_requests = []
    provider_chat = provider.chat

    async def snapshot_chat(messages, tools):
        progress_before_requests.append(copy.deepcopy(loop.task_progress.state()))
        return await provider_chat(messages, tools)

    provider.chat = snapshot_chat
    result = asyncio.run(loop.run(f"给 {TARGET} 和 {second} 的函数加中文注释并验证"))

    assert result.stop_reason == "end_turn" and provider.calls == 11
    results = {event.payload["call_id"]: event.payload
               for event in events(loop, EventType.tool_result)}
    first_receipt = results["first-a-validation"]["execution_effects"]["verified_paths"]
    new_receipt = results["new-a-validation"]["execution_effects"]["verified_paths"]
    assert first_receipt == new_receipt and len(first_receipt) == 1
    assert first_receipt[0].replace("\\", "/").lower().endswith("/" + TARGET)
    for index in range(1, 4):
        duplicate = results[f"duplicate-a-validation-{index}"]
        assert duplicate["result"].startswith("exit_code=0"), "The duplicate check actually ran successfully"
        assert "execution_effects" not in duplicate, "An unchanged verified version is not new delivery"
        # Snapshot on the following model request includes finish_turn's count.
        state = progress_before_requests[3 + index]
        assert state["turns_without_delivery"] == index
        assert not state["ready_to_finish"], "File B remains unfinished while A is repeatedly checked"
        assert any(path.endswith("/" + second) for path in state["active"])
    assert progress_before_requests[7]["turns_without_delivery"] == 0, "A real new edit resets the delivery budget"
    assert results["new-a-version"]["execution_effects"]["changed_paths"]
    assert len(validation.validated) == 6, "Each successful check inspects the actual temporary file"
    assert loop.task_progress.ready_to_finish
    assert (annotation_workspace / TARGET).read_text(encoding="utf-8") == SOURCE.replace(DECLARATION, revised_docstring)
    assert (annotation_workspace / second).read_text(encoding="utf-8") == SOURCE.replace(DECLARATION, ANNOTATED)
