"""Delivery regressions using real files, permissions, edit tools and Node.

Only the provider is scripted: no model API or user project is involved. The
batch-edit and shell results are produced by the same tools used by the app.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.models.events import EventType
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import BashTool, ReadFileTool
from code_agent.tools.builtin.code_tools import MAX_EDITS_PER_CALL, EditFileTool, EditManyTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


SOURCE = "autostart.mjs"
COUNT = MAX_EDITS_PER_CALL + 1
FINAL = "已为全部函数补充中文注释，Node 语法和行为检查通过。"


def _read():
    return OfflineProvider.tool_call_turn("read_file", {"path": SOURCE})


def _edit(index):
    old = f"export function part{index}(value) {{ return value + {index}; }}"
    return {"path": SOURCE, "old_string": old,
            "new_string": f"// 中文说明：返回输入值加 {index}，不修改输入。\n{old}"}


def _batch(edits):
    return OfflineProvider.tool_call_turn("edit_many", {"edits": edits})


def _shell(command):
    return OfflineProvider.tool_call_turn("bash", {
        "command": command,
        "_approval": {"purpose": "验证临时 fixture 的语法和行为",
                      "effects": ["仅运行临时工作区中的 Node 校验脚本"]},
    })


class RecordedProvider(OfflineProvider):
    def __init__(self, turns, before_request=None):
        super().__init__(script=turns)
        self.requests = []
        self.before_request = before_request

    async def chat(self, messages, tools):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        if self.before_request:
            self.before_request(len(self.requests))
        if not self._script:
            raise AssertionError("Loop requested an unplanned model round")
        return await super().chat(messages, tools)


@pytest.fixture
def source(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / SOURCE).write_text(
        "\n".join(_edit(index)["old_string"] for index in range(COUNT)) + "\n", encoding="utf-8",
    )
    (workspace / "verify.mjs").write_text(
        "import assert from 'node:assert/strict';\n"
        "import { readFileSync } from 'node:fs';\n"
        "import * as source from './autostart.mjs';\n"
        "const text = readFileSync(new URL('./autostart.mjs', import.meta.url), 'utf8');\n"
        f"for (let index = 0; index < {COUNT}; index++) {{\n"
        "  assert.ok(text.includes(`// 中文说明：返回输入值加 ${index}，不修改输入。`));\n"
        "  assert.equal(source[`part${index}`](7), 7 + index);\n"
        "}\n"
        f"console.log('VERIFIED {COUNT} Chinese comments and {COUNT} function results');\n",
        encoding="utf-8",
    )
    (workspace / "verify-one.mjs").write_text(
        "import assert from 'node:assert/strict';\n"
        "import { readFileSync } from 'node:fs';\n"
        "import { part0 } from './autostart.mjs';\n"
        "assert.ok(readFileSync(new URL('./autostart.mjs', import.meta.url), 'utf8').startsWith('// 中文说明'));\n"
        "assert.equal(part0(7), 7);\n"
        "console.log('VERIFIED first comment and function behavior');\n", encoding="utf-8",
    )
    return workspace


def _loop(source, provider, *, approver=None, mode="workspace-write", read_only_approval=False):
    registry = ToolRegistry()
    for tool in (ReadFileTool(), EditFileTool(), EditManyTool(), BashTool()):
        registry.register(tool)
    return AgentLoop(
        provider=provider, registry=registry,
        permissions=PermissionManager(source, approver=approver, mode=mode,
                                      read_only_approval=read_only_approval),
        transcript=TranscriptStore(source.parent / "delivery-transcripts"),
        workspace=source, fs_observation=FsObservation(),
    )


def _events(loop, kind):
    return [event for event in loop.transcript.replay(loop.session_id) if event.type == kind]


def _runtime_note(request):
    return "\n".join(str(message.get("content", "")) for message in request["messages"]
                     if message.get("role") == "system")


async def _approve_one_validation(request):
    assert request.tool_name == "bash" and json.loads(request.command)["command"] == "node verify-one.mjs"
    return True


@pytest.mark.skipif(shutil.which("node") is None, reason="Real delivery validation requires Node.js")
def test_oversized_batch_recovers_from_premature_answer_and_delivers_verified_comments(source):
    # A missing implementation must fail the same verifier used after editing.
    baseline = subprocess.run(["node", "verify.mjs"], cwd=source, capture_output=True, timeout=20)
    assert baseline.returncode != 0
    approvals = []

    async def approve_validation(request):
        approvals.append(request)
        assert request.tool_name == "bash"
        assert json.loads(request.command)["command"] in {"node --check autostart.mjs", "node verify.mjs"}
        return True

    def inspect_disk_before_request(number):
        text = (source / SOURCE).read_text(encoding="utf-8")
        if number in (3, 4):
            assert "中文说明" not in text, "Rejected oversized edits must not partially write"
        if number == 5:
            assert text.count("// 中文说明") == MAX_EDITS_PER_CALL

    edits = [_edit(index) for index in range(COUNT)]
    provider = RecordedProvider([
        _read(), _batch(edits),
        OfflineProvider.text_turn("未能写入注释，只能说明文件内容。"),
        _batch(edits[:MAX_EDITS_PER_CALL]), _batch(edits[MAX_EDITS_PER_CALL:]),
        _shell("node --check autostart.mjs"), _shell("node verify.mjs"),
        OfflineProvider.text_turn(FINAL),
    ], inspect_disk_before_request)
    loop = _loop(source, provider, approver=approve_validation)

    result = asyncio.run(loop.run("为 autostart.mjs 的所有函数添加中文注释，保持行为不变，运行 Node 验证。"))

    assert result.stop_reason == "end_turn" and result.text == FINAL
    assert result.turns == provider.calls == 8, "Successful editing must not add an audit-only model round"
    assert all(request["tools"] for request in provider.requests), "Recovery must retain native tools"
    assert "拆批" in _runtime_note(provider.requests[3])
    assert "最多" in _runtime_note(provider.requests[3])
    results = _events(loop, EventType.tool_result)
    failed = [event for event in results if event.payload["result"].startswith("[tool error]")]
    assert len(failed) == 1 and "最多" in failed[0].payload["result"]
    assert [event.payload["name"] for event in results] == [
        "read_file", "edit_many", "edit_many", "edit_many", "bash", "bash",
    ]
    shell_results = [event.payload["result"] for event in results if event.payload["name"] == "bash"]
    assert len(shell_results) == 2 and all("exit_code=0" in text for text in shell_results)
    assert f"VERIFIED {COUNT} Chinese comments and {COUNT} function results" in shell_results[-1]
    assert len(approvals) == 2, "Workspace batch edits must pass their nested path policy without approval"
    assert len(_events(loop, EventType.done)) == 1
    assert (source / SOURCE).read_text(encoding="utf-8").count("// 中文说明") == COUNT


@pytest.mark.skipif(shutil.which("node") is None, reason="Delivery validation requires Node.js")
def test_duplicate_read_threshold_retains_tools_and_allows_actual_edit(source):
    # Two windows may be read; subsequent identical reads are blocked while
    # native editing remains available before the ten-turn task guard stops it.
    provider = RecordedProvider([
        *[_read() for _ in range(7)], _batch([_edit(0)]), _shell("node verify-one.mjs"),
        OfflineProvider.text_turn("已补充函数注释。"),
    ])
    loop = _loop(source, provider, approver=_approve_one_validation)

    result = asyncio.run(loop.run("给 autostart.mjs 添加中文注释"))

    assert result.stop_reason == "end_turn" and provider.calls == 10
    assert all(request["tools"] for request in provider.requests)
    assert "任务推进" in _runtime_note(provider.requests[7])
    assert (source / SOURCE).read_text(encoding="utf-8").startswith("// 中文说明")
    assert not [event for event in _events(loop, EventType.error)
                if event.payload.get("code") == "edit_recovery"]


def test_unproductive_mutation_stops_explicitly_without_claiming_completion(source):
    original = (source / SOURCE).read_bytes()
    provider = RecordedProvider([_read() for _ in range(30)])
    loop = _loop(source, provider)

    result = asyncio.run(loop.run("给 autostart.mjs 添加中文注释"))

    assert result.stop_reason == "stalled" and "未完成" in result.text
    assert provider.calls == 10, "Annotation work stops after ten batches without delivery"
    assert all(request["tools"] for request in provider.requests)
    assert (source / SOURCE).read_bytes() == original
    done = _events(loop, EventType.done)
    assert len(done) == 1 and done[0].payload["stop_reason"] == "stalled"
    results = _events(loop, EventType.tool_result)
    assert len([event for event in results if _edit(0)["old_string"] in event.payload["result"]]) == 2
    assert all(any(reason in event.payload["result"] for reason in ("已经读取两次", "实施阶段不再重读源码"))
               for event in results[2:])


@pytest.mark.skipif(shutil.which("node") is None, reason="Delivery validation requires Node.js")
def test_text_protocol_correction_keeps_native_editing_available(source):
    provider = RecordedProvider([
        _read(),
        OfflineProvider.text_turn('<｜DSML｜calls><｜DSML｜invoke name="edit_many">not executable text'),
        _batch([_edit(0)]), _shell("node verify-one.mjs"), OfflineProvider.text_turn("已补充中文注释。"),
    ])
    loop = _loop(source, provider, approver=_approve_one_validation)

    result = asyncio.run(loop.run("给 autostart.mjs 添加中文注释"))

    assert result.stop_reason == "end_turn" and provider.calls == 5
    assert all(request["tools"] for request in provider.requests)
    assert "原生工具调用" in _runtime_note(provider.requests[2])
    assert [event.payload["call"]["name"] for event in _events(loop, EventType.tool_use)] == [
        "read_file", "edit_many", "bash",
    ], "A textual envelope must never be dispatched as a tool"
    assert (source / SOURCE).read_text(encoding="utf-8").startswith("// 中文说明")


def test_denied_edit_does_not_receive_automatic_retry_or_shell_bypass(source):
    original = (source / SOURCE).read_bytes()
    requests = []

    async def reject(request):
        requests.append(request)
        return False

    edit = _batch([_edit(0)])
    edit.tool_calls[0].arguments["_approval"] = {
        "purpose": "为临时 fixture 添加中文注释", "effects": ["修改 fixture 文件"]}
    provider = RecordedProvider([_read(), edit, OfflineProvider.text_turn("未获审批，未能修改。")])
    loop = _loop(source, provider, approver=reject, mode="read-only", read_only_approval=True)

    result = asyncio.run(loop.run("给 autostart.mjs 添加中文注释"))

    assert result.stop_reason == "stalled" and provider.calls == 3
    assert len(requests) == 1 and requests[0].tool_name == "edit_many"
    assert (source / SOURCE).read_bytes() == original
    assert [event.payload["call"]["name"] for event in _events(loop, EventType.tool_use)] == [
        "read_file", "edit_many",
    ]
    assert not [event for event in _events(loop, EventType.error)
                if event.payload.get("code") == "edit_recovery"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Real JSDoc verification requires Node.js")
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "Windows-CRLF"])
def test_live_fixture_verifier_accepts_multiline_chinese_jsdoc(tmp_path, newline):
    from scripts.verify_editing_delivery import COMMENTS, FUNCTIONS, SOURCE_TEXT, VERIFIER, node_check

    root = tmp_path / "jsdoc-verification"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    source = SOURCE_TEXT
    for name in FUNCTIONS:
        source = source.replace(
            f"export function {name}(",
            f"/**\n * {COMMENTS[name]}\n * @returns {{object|boolean}} 规范化后的结果。\n */\nexport function {name}(",
        )
    encoded = source.replace("\n", newline).encode("utf-8")
    (workspace / "autostart.mjs").write_bytes(encoded)
    (root / "verify.mjs").write_text(VERIFIER, encoding="utf-8")
    assert (b"\r\n" in encoded) == (newline == "\r\n")

    result = node_check(workspace, ["node", "../verify.mjs"])

    assert result["exit_code"] == 0 and result["verified_marker"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Delivery validation requires Node.js")
def test_pathless_malformed_batch_can_recover_through_real_single_file_edit(source):
    provider = RecordedProvider([
        _read(),
        _batch([{"old_string": _edit(0)["old_string"], "new_string": _edit(0)["new_string"]}]),
        OfflineProvider.tool_call_turn("edit_file", _edit(0)),
        _shell("node verify-one.mjs"),
        OfflineProvider.text_turn("已纠正参数并补充中文注释。"),
    ])
    loop = _loop(source, provider, approver=_approve_one_validation)

    result = asyncio.run(loop.run("给 autostart.mjs 添加中文注释"))

    assert result.stop_reason == "end_turn" and provider.calls == 5
    assert (source / SOURCE).read_text(encoding="utf-8").startswith("// 中文说明")
    results = _events(loop, EventType.tool_result)
    assert results[1].payload["result"].startswith("[permission denied]")
    assert "path" in results[1].payload["result"]
    assert results[2].payload["name"] == "edit_file"
    assert not results[2].payload["result"].startswith(("[tool error]", "[permission denied]"))
    assert loop._edit_failure == "" and not loop._edit_denied
    assert not [event for event in _events(loop, EventType.error)
                if event.payload.get("code") == "edit_recovery"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Real shell repair requires Node.js")
def test_authorized_shell_file_change_resolves_prior_edit_failure_and_verifies(source):
    edit = _edit(0)
    (source / "repair.mjs").write_text(
        "import assert from 'node:assert/strict';\n"
        "import { readFileSync, writeFileSync } from 'node:fs';\n"
        f"const oldText = {json.dumps(edit['old_string'], ensure_ascii=False)};\n"
        f"const newText = {json.dumps(edit['new_string'], ensure_ascii=False)};\n"
        f"const file = new URL('./{SOURCE}', import.meta.url);\n"
        "const before = readFileSync(file, 'utf8');\n"
        "assert.ok(before.includes(oldText));\n"
        "writeFileSync(file, before.replace(oldText, newText), 'utf8');\n"
        "console.log('REPAIRED fixture comment');\n", encoding="utf-8",
    )
    (source / "check-one.mjs").write_text(
        "import assert from 'node:assert/strict';\n"
        "import { readFileSync } from 'node:fs';\n"
        "import { part0 } from './autostart.mjs';\n"
        f"assert.ok(readFileSync(new URL('./{SOURCE}', import.meta.url), 'utf8').includes("
        f"{json.dumps(edit['new_string'], ensure_ascii=False)}));\n"
        "assert.equal(part0(7), 7);\n"
        "console.log('VERIFIED real shell comment and unchanged behavior');\n", encoding="utf-8",
    )
    approvals = []

    async def approve_fixture_command(request):
        command = json.loads(request.command)["command"]
        assert request.tool_name == "bash" and command in {"node repair.mjs", "node check-one.mjs"}
        approvals.append(command)
        return True

    provider = RecordedProvider([
        _read(), _batch([{**edit, "old_string": "missing fixture fragment"}]),
        _shell("node repair.mjs"), _shell("node check-one.mjs"),
        OfflineProvider.text_turn("已通过本机命令修复写入并验证。"),
    ])
    loop = _loop(source, provider, approver=approve_fixture_command)

    result = asyncio.run(loop.run("给 autostart.mjs 添加中文注释并实际验证"))

    assert result.stop_reason == "end_turn" and provider.calls == 5
    assert approvals == ["node repair.mjs", "node check-one.mjs"]
    assert (source / SOURCE).read_text(encoding="utf-8").startswith("// 中文说明")
    assert loop._edit_failure == "" and loop._has_successful_edit
    results = _events(loop, EventType.tool_result)
    assert results[1].payload["result"].startswith("[tool error]")
    assert all("exit_code=0" in event.payload["result"] for event in results[2:])
    assert "VERIFIED real shell comment and unchanged behavior" in results[-1].payload["result"]
    assert not [event for event in _events(loop, EventType.error)
                if event.payload.get("code") == "edit_recovery"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Real validation-only shell checks require Node.js")
@pytest.mark.parametrize("command", ["node --check autostart.mjs", "echo fixture-no-change"],
                         ids=["syntax-only", "echo-only"])
def test_successful_shell_without_file_change_does_not_resolve_edit_failure(source, command):
    original = (source / SOURCE).read_bytes()
    approvals = []

    async def approve_fixture_command(request):
        assert request.tool_name == "bash" and json.loads(request.command)["command"] == command
        approvals.append(command)
        return True

    provider = RecordedProvider([
        _read(), _batch([{**_edit(0), "old_string": "missing fixture fragment"}]),
        _shell(command), OfflineProvider.text_turn("命令成功，任务已完成。"),
        OfflineProvider.text_turn("没有进一步修改，结束。"),
    ])
    loop = _loop(source, provider, approver=approve_fixture_command)

    result = asyncio.run(loop.run("给 autostart.mjs 添加中文注释并实际验证"))

    assert result.stop_reason == "stalled" and "未完成" in result.text and provider.calls == 5
    assert (source / SOURCE).read_bytes() == original
    assert loop._edit_failure and not loop._has_successful_edit
    results = _events(loop, EventType.tool_result)
    assert results[1].payload["result"].startswith("[tool error]")
    assert results[2].payload["name"] == "bash" and "exit_code=0" in results[2].payload["result"]
    assert approvals == ([command] if command.startswith("node") else [])
    assert len([event for event in _events(loop, EventType.error)
                if event.payload.get("code") == "edit_recovery"]) == 1
