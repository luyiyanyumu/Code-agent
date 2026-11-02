"""Bounded source-driven growth, with real Harness/native file edits."""

import asyncio
from types import SimpleNamespace

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.agent.router import ModelRouter
from code_agent.agent.source_context import SourceContext
from code_agent.context.compact import messages_size_chars
from code_agent.context.prompt import dynamic_history_budget
from code_agent.models.events import ModelTurn, ToolCall
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionRuntime
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin import ReadFileTool
from code_agent.tools.builtin.code_tools import EditFileTool
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry


def files(workspace):
    sizes = {"implementation.py": 6350, "tests.py": 7540, "acceptance.py": 9999,
             "api.py": 5000, "paths.py": 3000, "conftest.py": 2111}
    sources = {}
    for index, (name, size) in enumerate(sizes.items()):
        start = f"value_{index} = 1\n# "
        sources[name] = start + chr(65 + index) * (size - len(start))
        (workspace / name).write_text(sources[name], encoding="utf8")
    assert sum(map(len, sources.values())) == 34_000
    return sources


def loop_for(workspace, provider, **kwargs):
    registry = ToolRegistry()
    for tool in (ReadFileTool(), EditFileTool()):
        registry.register(tool)
    permission = PermissionManager(workspace)
    for name in registry.names():
        permission.register_policy(name, AllowPolicy())
    return AgentLoop(provider, registry, permission, TranscriptStore(workspace.parent / "adaptive-events"),
                     workspace=workspace, fs_observation=FsObservation(), max_turns=5, **kwargs)


def test_actual_harness_reads_six_files_then_edits_on_next_model_call(workspace, monkeypatch):
    monkeypatch.delenv("CODE_AGENT_HISTORY_BUDGET", raising=False)
    sources = files(workspace)

    class Reader(OfflineProvider):
        async def chat(self, messages, tools):
            if self.calls == 1:
                body = "\n".join(str(m.get("content") or "") for m in messages).replace("\r\n", "\n")
                assert all(source in body for source in sources.values())
                assert messages_size_chars(m for m in messages if m.get("role") != "system") <= 48_000
            return await super().chat(messages, tools)

    provider = Reader(script=[ModelTurn(tool_calls=[ToolCall(name="read_file", arguments={"path": path})
                                                   for path in sources], stop_reason="tool_use"),
                              OfflineProvider.tool_call_turn("edit_file", {"path": "implementation.py",
                                  "old_string": "value_0 = 1", "new_string": "value_0 = 2"}),
                              OfflineProvider.text_turn("已精确修改并保留读取证据")])
    observed = []
    loop = loop_for(workspace, provider, history_max_chars=24_000, history_ceiling_chars=48_000,
                    on_history_budget=observed.append)
    result = asyncio.run(loop.run("Edit the existing value in implementation.py using the six related files."))
    assert result.stop_reason == "end_turn" and result.turns == 3
    assert (workspace / "implementation.py").read_text(encoding="utf8").startswith("value_0 = 2")
    assert observed[0] == 24_000 and 34_000 < max(observed) <= 48_000
    assert loop.selected_history_chars == observed[-1]


def receipts(workspace, context, sources):
    messages = [{"role": "user", "content": "Implement and verify this batch."}]
    for index, path in enumerate(sources):
        call = ToolCall(id=f"read-{index}", name="read_file", arguments={"path": path})
        before = context.begin(call)
        result = asyncio.run(ReadFileTool().execute(call.arguments, ToolContext("adaptive", workspace)))
        context.capture(call, result, before)
        context.delivered(call.id, str(result))
        messages += [{"role": "assistant", "content": None, "tool_calls": [{"id": call.id, "type": "function",
                       "function": {"name": call.name, "arguments": '{}'}}]},
                     {"role": "tool", "tool_call_id": call.id, "content": str(result)}]
    return messages


def test_explicit_budget_and_no_ceiling_do_not_grow(workspace, monkeypatch):
    sources = files(workspace)
    context = SourceContext(workspace)
    messages = receipts(workspace, context, sources)
    loop = loop_for(workspace, OfflineProvider(), history_max_chars=24_000, history_ceiling_chars=48_000)
    loop._source_context = context
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "24000")
    fitted = loop._fit_messages(messages)
    assert loop.selected_history_chars == 24_000
    assert messages_size_chars(fitted) <= 24_000
    monkeypatch.delenv("CODE_AGENT_HISTORY_BUDGET")
    loop.history_ceiling_chars = None
    assert messages_size_chars(loop._fit_messages(messages)) <= 24_000
    assert loop.selected_history_chars == 24_000


def test_repeated_read_history_does_not_grow_source_budget(workspace, monkeypatch):
    monkeypatch.delenv("CODE_AGENT_HISTORY_BUDGET", raising=False)
    sources = files(workspace)
    context = SourceContext(workspace)
    messages = receipts(workspace, context, sources)
    context.fit(messages, 24_000, ceiling_chars=48_000)
    first = context.selected_budget
    for index in range(35):
        call = ToolCall(id=f"repeat-{index}", name="read_file", arguments={"path": "implementation.py"})
        result = asyncio.run(ReadFileTool().execute(call.arguments, ToolContext("adaptive", workspace)))
        context.capture(call, result, context.begin(call))
        context.delivered(call.id, str(result))
        messages += [{"role": "assistant", "content": None, "tool_calls": [{"id": call.id, "type": "function",
                       "function": {"name": call.name, "arguments": '{}'}}]},
                     {"role": "tool", "tool_call_id": call.id, "content": str(result)}]
        context.fit(messages, 24_000, ceiling_chars=48_000)
        assert context.selected_budget == first
        assert len(context.receipts) <= 24


def test_runtime_ceiling_requires_known_capacity_and_obeys_reservations(monkeypatch):
    monkeypatch.delenv("CODE_AGENT_HISTORY_BUDGET", raising=False)
    provider = SimpleNamespace(model="deepseek-flash", context_window=None)
    capacity = {"context_window": 131_072, "max_output_tokens": 8192}
    runtime = SimpleNamespace(router=SimpleNamespace(model_capacity=lambda _: capacity,
        resolve_default=lambda: provider))
    runtime._history_budget = lambda *args, **kwargs: SessionRuntime._history_budget(runtime, *args, **kwargs)
    registry = SimpleNamespace(schemas=lambda: [])
    kwargs = {"system_prompt": "system", "registry": registry}
    assert SessionRuntime._history_ceiling(runtime, provider, **kwargs) == 48_000
    provider.model = "unrecognized-model"
    assert SessionRuntime._history_ceiling(runtime, provider, **kwargs) is None
    provider.context_window = 1000
    capacity.update(context_window=1000, max_output_tokens=800)
    assert SessionRuntime._history_ceiling(runtime, provider, **kwargs) <= 440
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "24000")
    assert SessionRuntime._history_ceiling(runtime, provider, **kwargs) is None
    assert dynamic_history_budget(1_000_000, budget_cap=48_000) == 24_000
    monkeypatch.delenv("CODE_AGENT_HISTORY_BUDGET")
    assert dynamic_history_budget(None, budget_cap=48_000) == 24_000


def test_context_usage_uses_selected_budget_bounded_by_current_capacity(agent_home, monkeypatch):
    from code_agent.agent import router as router_module
    from code_agent.api.app import build_registry
    from code_agent.sessions.manager import SessionManager
    from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo

    monkeypatch.delenv("CODE_AGENT_HISTORY_BUDGET", raising=False)
    monkeypatch.setattr(router_module, "model_config", lambda: {"provider": "deepseek", "model_default": "deepseek-chat",
        "context_window": 131_072, "max_output_tokens": 8192})
    db = Database(agent_home / "adaptive-display.db")
    user = UserRepo(db).create("adaptive-display", "test-only")
    manager = SessionManager(transcript=TranscriptStore(agent_home / "events"), session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db), router=ModelRouter(), registry=build_registry())
    meta = manager.session_repo.create(user["id"])
    runtime = manager.get_runtime(meta["id"], user["id"])
    loop = runtime._build_loop()
    assert loop.history_max_chars == 24_000 and loop.history_ceiling_chars == 48_000
    loop.on_history_budget(43_000)
    assert manager.context_usage(meta["id"], user["id"])["budget_tokens"] == int(43_000 / 2.2)
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "24000")
    assert manager.context_usage(meta["id"], user["id"])["budget_tokens"] == int(24_000 / 2.2)
    runtime.shutdown()
    db.close()
