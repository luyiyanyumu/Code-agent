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


def test_explicit_annotation_edit_and_validation_deliver_without_extra_model_round(annotation_workspace):
    loop, provider, validation, _ = make_loop(annotation_workspace, [
        read_turn(), edit_turn(), tool_turn("bash", command=f"python -m py_compile {TARGET}"), done_turn(),
    ])
    result = asyncio.run(loop.run(f"给 {TARGET} 加中文注释并验证"))
    assert result.stop_reason == "end_turn" and provider.calls == 4
    assert validation.commands == [f"python -m py_compile {TARGET}"]
    assert len(validation.validated) == 1
    assert (annotation_workspace / TARGET).read_text(encoding="utf8") == SOURCE.replace(DECLARATION, ANNOTATED)
    assert [event.payload["call"]["name"] for event in events(loop, EventType.tool_use)] == ["read_file", "edit_many", "bash"]


def test_truncated_annotation_stops_without_automatic_retry_or_partial_execution(annotation_workspace):
    loop, provider, validation, _ = make_loop(annotation_workspace, [read_turn(), truncated_edit("partial-edit")])
    result = asyncio.run(loop.run(f"给 {TARGET} 添加中文注释"))
    assert result.stop_reason == "max_tokens" and provider.calls == 2
    assert (annotation_workspace / TARGET).read_text(encoding="utf8") == SOURCE
    assert not validation.commands
    assert [event.payload["call"]["name"] for event in events(loop, EventType.tool_use)] == ["read_file"]


def test_natural_annotation_answer_does_not_manufacture_repair_round(annotation_workspace):
    loop, provider, validation, _ = make_loop(annotation_workspace, [ModelTurn(text="尚未修改，缺少所需信息。")])
    result = asyncio.run(loop.run(f"给 {TARGET} 添加中文注释"))
    assert result.text == "尚未修改，缺少所需信息。" and provider.calls == 1
    assert (annotation_workspace / TARGET).read_text(encoding="utf8") == SOURCE
    assert not validation.commands and not events(loop, EventType.tool_use)
