"""Development delivery through real editing, permissions and Python processes.

The model is scripted here; the separate project probe runs the configured model
against the real search service/API. These tests isolate loop recovery behavior.
"""

from __future__ import annotations

import asyncio
import copy
import sys

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.models.events import EventType, ModelTurn, ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import BashTool, ReadFileTool, WriteFileTool
from code_agent.tools.builtin.code_tools import EditFileTool, EditManyTool, GrepTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


BEFORE = "def total(subtotal, discount=0):\n    return subtotal * (1 - discount // 100)\n"
FIXED = "def total(subtotal, discount=0, tax=0):\n    return round(subtotal * (1 - discount / 100) * (1 + tax / 100), 2)\n"
QUOTE = "from pricing import total\n\ndef quote(subtotal, discount=0, tax=0):\n    return {'amount': total(subtotal, discount, tax), 'currency': 'CNY'}\n"
QUOTE_TEST = (
    "from quote import quote\n\n"
    "def test_tax_and_discount():\n"
    "    assert quote(100, 10, 20) == {'amount': 108.0, 'currency': 'CNY'}\n"
    "    assert quote(100) == {'amount': 100.0, 'currency': 'CNY'}\n"
    "    assert quote(0, 10, 20)['amount'] == 0\n"
)


def tool(name, **args):
    return ModelTurn(tool_calls=[ToolCall(name=name, arguments=args)], stop_reason="tool_use")


def done():
    return ModelTurn(text="实现及回归验证完成。", stop_reason="end_turn")


class Provider(OfflineProvider):
    def __init__(self, turns):
        super().__init__(script=turns)
        self.requests = []

    async def chat(self, messages, tools):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        assert self._script, "A completed implementation must not trigger another audit interview"
        return await super().chat(messages, tools)


@pytest.fixture
def project(workspace):
    (workspace / "service" / "tests").mkdir(parents=True)
    (workspace / "service" / "pricing.py").write_text(BEFORE, encoding="utf-8")
    (workspace / "service" / "settings.toml").write_text('currency = "CNY"\n', encoding="utf-8")
    (workspace / "service" / "tests" / "test_pricing.py").write_text(
        "from pricing import total\n\ndef test_discount():\n"
        "    assert total(100, 10) == 90\n"
        "    assert total(40, 25) == 30\n", encoding="utf-8",
    )
    return workspace


def make_loop(project, turns):
    registry = ToolRegistry()
    for item in (ReadFileTool(), WriteFileTool(), EditFileTool(), EditManyTool(), GrepTool(), BashTool()):
        registry.register(item)
    permission = PermissionManager(workspace_root=project)
    permission.register_policy("bash", AllowPolicy())
    provider = Provider(turns)
    loop = AgentLoop(provider=provider, registry=registry, permissions=permission,
                     transcript=TranscriptStore(project.parent / "development-transcripts"),
                     workspace=project, fs_observation=FsObservation())
    return loop, provider


def check(extra=""):
    interpreter = str(sys.executable).replace("\\", "/")
    return tool("bash", command=f'cd service && "{interpreter}" -m pytest -q tests/test_pricing.py {extra}'.strip())


def results(loop):
    return [event.payload for event in loop.transcript.replay(loop.session_id) if event.type == EventType.tool_result]


@pytest.mark.parametrize("parallel", [True, False])
def test_same_model_batch_writes_feature_and_regression_before_real_full_validation(project, monkeypatch, parallel):
    from code_agent.agent import loop as loop_module

    monkeypatch.setattr(loop_module, "TOOL_PARALLEL", parallel)
    command = check("tests/test_quote.py").tool_calls[0].arguments["command"]
    batch = ModelTurn(tool_calls=[
        ToolCall(id="fix", name="edit_file", arguments={"path": "service/pricing.py", "old_string": BEFORE, "new_string": FIXED}),
        ToolCall(id="quote", name="write_file", arguments={"path": "service/quote.py", "content": QUOTE}),
        ToolCall(id="regression", name="write_file", arguments={"path": "service/tests/test_quote.py", "content": QUOTE_TEST}),
        ToolCall(id="verify", name="bash", arguments={"command": command}),
    ], stop_reason="tool_use")
    loop, provider = make_loop(project, [tool("read_file", path="service/pricing.py"), batch, done()])
    prompt = ("只修改 service/pricing.py，并新建 service/quote.py、service/tests/test_quote.py，修复折扣并增加含税报价和回归测试。\n"
              "完整测试命令：" + command)
    result = asyncio.run(loop.run(prompt))
    assert result.stop_reason == "end_turn" and provider.calls == 3
    assert loop.task_progress.ready_to_finish
    assert loop.task_progress.verification_plan.has_current_full_result
    completed = results(loop)
    assert [item["call_id"] for item in completed[-4:]] == ["fix", "quote", "regression", "verify"]
    assert completed[-1]["result"].startswith("exit_code=0") and "2 passed" in completed[-1]["result"]
    assert len(completed[-1]["execution_effects"]["verified_paths"]) == 3


def test_feature_and_bug_fix_create_files_and_cannot_finish_before_real_subproject_tests(project):
    settings_before = (project / "service" / "settings.toml").read_bytes()
    baseline_before = (project / "service" / "tests" / "test_pricing.py").read_bytes()
    loop, provider = make_loop(project, [
        tool("read_file", path="service/pricing.py"),
        tool("read_file", path="service/settings.toml"),
        tool("edit_file", path="service/pricing.py", old_string=BEFORE, new_string=FIXED),
        tool("write_file", path="service/quote.py", content=QUOTE),
        tool("write_file", path="service/tests/test_quote.py", content=QUOTE_TEST),
        done(),  # A native write is not yet a tested delivery.
        check("tests/test_quote.py"), done(),
    ])
    result = asyncio.run(loop.run("修复订单折扣计算错误，并实现包含税费的报价展示，新增回归测试并实际验证。"))
    assert result.stop_reason == "end_turn" and provider.calls == 8
    assert loop.task_progress.ready_to_finish and not loop.task_progress.annotation
    assert (project / "service" / "settings.toml").read_bytes() == settings_before
    assert (project / "service" / "tests" / "test_pricing.py").read_bytes() == baseline_before
    checks = [item for item in results(loop) if item["name"] == "bash"]
    assert len(checks) == 1 and checks[0]["result"].startswith("exit_code=0")
    assert "2 passed" in checks[0]["result"]
    assert len(checks[0]["execution_effects"]["verified_paths"]) == 4  # Includes the unchanged config read.
    assert len(loop.task_progress.state()["verified"]) == 3


def test_real_failing_test_keeps_tools_and_allows_targeted_bug_repair(project):
    wrong = FIXED.replace("discount / 100", "discount / 10")
    loop, _ = make_loop(project, [
        tool("read_file", path="service/pricing.py"),
        tool("edit_file", path="service/pricing.py", old_string=BEFORE, new_string=wrong),
        check(),
        done(),  # A failing test cannot be silently presented as completion.
        tool("grep", path="service/pricing.py", pattern="return"),
        tool("read_file", path="service/pricing.py", offset=1, limit=40),
        tool("edit_file", path="service/pricing.py", old_string="discount / 10", new_string="discount / 100"),
        check(), done(),
    ])
    result = asyncio.run(loop.run("修复订单折扣计算并运行相关测试。"))
    assert result.stop_reason == "end_turn" and loop.task_progress.ready_to_finish
    checks = [item for item in results(loop) if item["name"] == "bash"]
    assert len(checks) == 2
    assert checks[0]["result"].startswith("exit_code=1") and "execution_effects" not in checks[0]
    assert checks[1]["result"].startswith("exit_code=0") and checks[1]["execution_effects"]["verified_paths"]
    assert not loop.task_progress.state()["validation_failures"]
    failure_context = "\n".join(str(m.get("content", "")) for m in loop.provider.requests[3]["messages"])
    assert "AssertionError" in failure_context
    assert "实际失败验证的诊断" in failure_context and "【数据块开始" in failure_context


def test_truncated_feature_edit_is_discarded_and_recovers_with_implementation_body(project):
    fragment = tool("edit_many", edits=[{"path": "service/pricing.py", "old_string": BEFORE,
                                         "new_string": "THIS_PARTIAL_IMPLEMENTATION_MUST_NOT_EXECUTE"}])
    fragment.stop_reason = "max_tokens"
    loop, provider = make_loop(project, [
        tool("read_file", path="service/pricing.py"), fragment,
        tool("edit_many", edits=[{"path": "service/pricing.py", "old_string": BEFORE, "new_string": FIXED}]),
        check(), done(),
    ])
    result = asyncio.run(loop.run("修复订单折扣计算错误并实现税费支持，运行测试验证。"))
    assert result.stop_reason == "end_turn" and provider.calls == 5
    assert (project / "service" / "pricing.py").read_text(encoding="utf-8") == FIXED
    assert all("THIS_PARTIAL" not in item["result"] for item in results(loop))
    editing = next(item for item in provider.requests[2]["tools"] if item.get("name") == "edit_many")
    assert editing["parameters"]["properties"]["edits"]["maxItems"] == 1
    assert "Annotation task" not in editing.get("description", "")
    assert loop.task_progress.ready_to_finish


def test_changing_public_explanations_cannot_reopen_the_same_investigation(project):
    repeated = [tool("grep", path="service/pricing.py", pattern="return", _approval={
        "purpose": f"定位折扣计算，第 {number} 次说明", "effects": ["只读源码片段"],
    }) for number in range(3)]
    loop, _ = make_loop(project, [
        tool("read_file", path="service/pricing.py"), *repeated,
        tool("edit_file", path="service/pricing.py", old_string=BEFORE, new_string=FIXED),
        check(), done(),
    ])
    result = asyncio.run(loop.run("修复折扣计算错误并实际验证。"))
    assert result.stop_reason == "end_turn"
    queries = [item for item in results(loop) if item["name"] == "grep"]
    assert len(queries) == 3
    assert all("[task scope]" not in item["result"] for item in queries[:2])
    assert "[task scope]" in queries[2]["result"]
    assert loop.task_progress.ready_to_finish


def test_real_baseline_failure_can_recover_without_forcing_reference_files_to_change(project):
    original_settings = (project / "service" / "settings.toml").read_bytes()
    original_tests = (project / "service" / "tests" / "test_pricing.py").read_bytes()
    loop, provider = make_loop(project, [
        tool("read_file", path="service/pricing.py"),
        tool("read_file", path="service/settings.toml"),
        check(),  # A real reproduction before the first edit must allow recovery.
        tool("read_file", path="service/pricing.py", offset=1, limit=40),
        tool("edit_file", path="service/pricing.py", old_string=BEFORE, new_string=FIXED),
        check(), done(),
    ])
    result = asyncio.run(loop.run("修复订单折扣 bug，先复现再修改并运行测试。"))
    assert result.stop_reason == "end_turn" and provider.calls == 7
    assert loop.task_progress.ready_to_finish
    assert not loop.task_progress.state()["validation_failures"]
    assert (project / "service" / "settings.toml").read_bytes() == original_settings
    assert (project / "service" / "tests" / "test_pricing.py").read_bytes() == original_tests
    checks = [item for item in results(loop) if item["name"] == "bash"]
    assert checks[0]["result"].startswith("exit_code=1")
    assert checks[1]["result"].startswith("exit_code=0")


def test_repeating_same_passing_check_has_no_new_execution_effect(project):
    loop, _ = make_loop(project, [
        tool("read_file", path="service/pricing.py"),
        tool("read_file", path="service/settings.toml"),
        tool("edit_file", path="service/pricing.py", old_string=BEFORE, new_string=FIXED),
        check(), check(), done(),
    ])
    result = asyncio.run(loop.run("修复折扣计算并实际验证。"))
    assert result.stop_reason == "end_turn" and loop.task_progress.ready_to_finish
    checks = [item for item in results(loop) if item["name"] == "bash"]
    assert len(checks) == 2 and all(item["result"].startswith("exit_code=0") for item in checks)
    assert checks[0]["execution_effects"]["verified_paths"]
    assert "execution_effects" not in checks[1]
