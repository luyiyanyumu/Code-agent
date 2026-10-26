"""Batch edits use the real permission, approval and dispatch chain."""

from __future__ import annotations

import asyncio
import json

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import BatchWriteFilePolicy, Decision
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin import ReadFileTool
from code_agent.tools.builtin.code_tools import EditManyTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


def _edit(path="a.mjs", old="export const a = 1;", new="// a\nexport const a = 1;"):
    return {"path": path, "old_string": old, "new_string": new}


class RecordingEditMany(EditManyTool):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def execute(self, arguments, ctx):
        self.calls.append(arguments)
        return await super().execute(arguments, ctx)


def _loop(workspace, permissions, batches, *, read_paths=("a.mjs",)):
    registry = ToolRegistry()
    tool = RecordingEditMany()
    registry.register(ReadFileTool())
    registry.register(tool)
    script = [OfflineProvider.tool_call_turn("read_file", {"path": path}) for path in read_paths]
    for batch in batches:
        script.append(OfflineProvider.tool_call_turn("edit_many", {
            **batch, "_approval": {
                "purpose": "为离线 fixture 补充注释", "effects": ["修改 fixture 源文件的注释"],
            },
        }))
    script.append(OfflineProvider.text_turn("离线调用结束"))
    loop = AgentLoop(
        provider=OfflineProvider(script=script), registry=registry, permissions=permissions,
        transcript=TranscriptStore(workspace.parent / "transcripts"), workspace=workspace,
        fs_observation=FsObservation(),
    )
    return loop, tool


def _events(loop, kind):
    return [event for event in loop.transcript.replay(loop.session_id) if event.type.value == kind]


@pytest.fixture
def source(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.mjs").write_text("export const a = 1;\n", encoding="utf-8")
    (workspace / "b.mjs").write_text("export const b = 2;\n", encoding="utf-8")
    return workspace


def test_workspace_batch_writes_all_declared_paths_through_loop(source):
    permissions = PermissionManager(workspace_root=source, contract_guard=True)
    loop, tool = _loop(source, permissions, [{"edits": [
        _edit(), _edit("b.mjs", "export const b = 2;", "// b\nexport const b = 2;"),
    ]}], read_paths=("a.mjs", "b.mjs"))
    asyncio.run(loop.run("为 a.mjs 和 b.mjs 两个 fixture 添加注释"))

    assert len(tool.calls) == 1
    assert (source / "a.mjs").read_text(encoding="utf-8").startswith("// a\n")
    assert (source / "b.mjs").read_text(encoding="utf-8").startswith("// b\n")
    assert not _events(loop, "approval_request")
    assert permissions.policy_kind("edit_many")[0] == "policy"


def test_read_only_approval_covers_one_batch_and_is_not_cached(source):
    requests = []

    async def approve_once(request):
        requests.append(request)
        assert request.tool_name == "edit_many"
        assert "会话保持只读" in request.reason
        assert json.loads(request.command)["edits"][0]["path"] == "a.mjs"
        return len(requests) == 1

    permissions = PermissionManager(
        workspace_root=source, mode="read-only", approver=approve_once, read_only_approval=True,
    )
    first = _edit()
    loop, tool = _loop(source, permissions, [
        {"edits": [first]},
        {"edits": [_edit(old=first["new_string"], new="// must not be written")]},
    ])
    asyncio.run(loop.run("为 fixture 添加注释"))

    assert len(requests) == 2 and len(tool.calls) == 1
    assert (source / "a.mjs").read_text(encoding="utf-8") == first["new_string"] + "\n"
    assert [event.payload["approved"] for event in _events(loop, "approval_decided")] == [True, False]
    assert permissions.mode == "read-only"
    assert asyncio.run(permissions.evaluate(
        "edit_many", {"edits": [_edit()]}, ToolContext("later", source),
    )).decision == Decision.need_approval


@pytest.mark.parametrize("interactive,has_approver", [(False, True), (True, False)])
def test_read_only_without_interactive_channel_stays_denied(source, interactive, has_approver):
    async def approve(_request):
        raise AssertionError("Strict read-only must not request approval")

    permissions = PermissionManager(
        workspace_root=source, mode="read-only", read_only_approval=interactive,
        approver=approve if has_approver else None,
    )
    result = asyncio.run(permissions.evaluate(
        "edit_many", {"edits": [_edit()]}, ToolContext("strict", source),
    ))
    assert result.decision == Decision.deny and "只读" in result.reason


@pytest.mark.parametrize("arguments", [
    {}, {"path": "a.mjs"}, {"edits": None}, {"edits": []}, {"edits": {}},
    {"edits": [None]}, {"edits": ["a.mjs"]}, {"edits": [{}]},
    {"edits": [_edit(path=" ")]}, {"edits": [_edit(path=1)]},
    {"edits": [_edit(path="bad\x00.mjs")]},
    {"edits": [{"path": "a.mjs", "old_string": "old"}]},
    {"edits": [_edit(old=None)]}, {"edits": [_edit(new=3)]},
    {"edits": [{**_edit(), "replace_all": "true"}]},
])
def test_malformed_batch_is_denied_before_approval(source, arguments):
    async def approve(_request):
        raise AssertionError("Malformed arguments must not become approval requests")

    permissions = PermissionManager(
        workspace_root=source, mode="read-only", approver=approve, read_only_approval=True,
    )
    result = asyncio.run(permissions.evaluate("edit_many", arguments, ToolContext("invalid", source)))
    assert result.decision == Decision.deny
    assert permissions.history == []


@pytest.mark.parametrize("mode", ["workspace-write", "read-only"])
@pytest.mark.parametrize("invalid", ["outside", "contract", "malformed"])
@pytest.mark.parametrize("invalid_first", [False, True])
def test_one_invalid_target_blocks_every_write_and_cannot_be_approved(source, mode, invalid, invalid_first):
    (source / "design.CONTRACT.md").write_text("original contract", encoding="utf-8")
    outside = source.parent / "outside.mjs"
    outside.write_text("outside original", encoding="utf-8")
    bad = {
        "outside": _edit("../outside.mjs", "outside original", "bad"),
        "contract": _edit("design.CONTRACT.md", "original contract", "bad"),
        "malformed": _edit(path=""),
    }[invalid]
    edits = [bad, _edit()] if invalid_first else [_edit(), bad]

    async def approve(_request):
        raise AssertionError("Hard denial cannot be overridden by approval")

    permissions = PermissionManager(
        workspace_root=source, mode=mode, approver=approve, read_only_approval=True, contract_guard=True,
    )
    # A harmless top-level path must not mask the actual nested targets.
    loop, tool = _loop(source, permissions, [{"path": "a.mjs", "edits": edits}])
    asyncio.run(loop.run("批量修改离线 fixture"))

    assert tool.calls == [] and not _events(loop, "approval_request")
    result = _events(loop, "tool_result")[-1].payload["result"]
    assert result.startswith("[permission denied]")
    assert (source / "a.mjs").read_text(encoding="utf-8") == "export const a = 1;\n"
    assert outside.read_text(encoding="utf-8") == "outside original"
    assert (source / "design.CONTRACT.md").read_text(encoding="utf-8") == "original contract"


def test_nested_paths_are_rechecked_after_approval_before_dispatch(source):
    (source / "design.CONTRACT.md").write_text("original contract", encoding="utf-8")
    requests = []

    async def approve_then_enable_guard(request):
        requests.append(request)
        permissions.register_policy("edit_many", BatchWriteFilePolicy(source, contract_guard=True))
        return True

    permissions = PermissionManager(
        workspace_root=source, mode="read-only", approver=approve_then_enable_guard,
        read_only_approval=True, contract_guard=False,
    )
    loop, tool = _loop(source, permissions, [{"edits": [
        _edit(), _edit("design.CONTRACT.md", "original contract", "must not change"),
    ]}], read_paths=("a.mjs", "design.CONTRACT.md"))
    asyncio.run(loop.run("审批等待期间权限变化的离线 fixture"))

    assert len(requests) == 1 and tool.calls == []
    assert _events(loop, "approval_decided")[0].payload["approved"] is True
    assert "契约文件" in _events(loop, "tool_result")[-1].payload["result"]
    assert (source / "a.mjs").read_text(encoding="utf-8") == "export const a = 1;\n"
    assert (source / "design.CONTRACT.md").read_text(encoding="utf-8") == "original contract"


def test_contract_guard_toggle_preserves_single_and_batch_policy_semantics(source):
    ctx = ToolContext("guard", source)
    for guarded in (False, True):
        permissions = PermissionManager(workspace_root=source, contract_guard=guarded)
        single = asyncio.run(permissions.evaluate("edit_file", {"path": "design.CONTRACT.md"}, ctx))
        batch = asyncio.run(permissions.evaluate(
            "edit_many", {"edits": [_edit("design.CONTRACT.md")]}, ctx,
        ))
        assert single.decision == batch.decision == (Decision.deny if guarded else Decision.allow)


def test_batch_paths_use_current_context_workspace_and_resolve_parent_segments(source):
    nested = source / "nested"
    nested.mkdir()
    permissions = PermissionManager(workspace_root=source)
    allowed = asyncio.run(permissions.evaluate(
        "edit_many", {"edits": [_edit("../a.mjs")]}, ToolContext("nested", nested),
    ))
    denied = asyncio.run(permissions.evaluate(
        "edit_many", {"edits": [_edit("../../outside.mjs")]}, ToolContext("nested", nested),
    ))
    assert allowed.decision == Decision.allow and denied.decision == Decision.deny
