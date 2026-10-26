"""Paid response accounting survives interrupted prompts without replay doubles."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from code_agent.agent import loop as loop_module
from code_agent.agent.router import ModelRouter
from code_agent.models.events import EventType, ModelTurn, TokenUsage, ToolCall
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager, SessionRuntime
from code_agent.sessions.team_budget import ExecutionBudgetExceeded, TeamBudget
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UsageRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin.tools_builtin import ReadFileTool
from code_agent.tools.registry import ToolRegistry


@pytest.fixture
def environment(agent_home, monkeypatch):
    monkeypatch.setattr("code_agent.sessions.manager.CLOSURE_LIMIT", 0)
    monkeypatch.setattr("code_agent.sessions.manager.MAX_CONTINUATIONS", 3)
    monkeypatch.setattr("code_agent.settings.agent_team_budget_config", lambda: {
        "task_tokens": 0, "task_seconds": 0, "team_tokens": 0, "team_seconds": 0,
    })
    db = Database(agent_home / "usage.db")
    user = UserRepo(db).create("usage-fixture", "temporary-only")["id"]
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    manager = SessionManager(
        transcript=TranscriptStore(agent_home / "transcripts"), session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db), router=ModelRouter(provider_name="offline"),
        registry=registry, usage_repo=UsageRepo(db),
    )
    sid = manager.session_repo.create(user)["id"]
    runtime = manager.get_runtime(sid, user)
    (runtime.workspace / "fixture.txt").write_text("offline evidence", encoding="utf-8")
    yield SimpleNamespace(db=db, manager=manager, runtime=runtime)
    runtime.shutdown()
    db.close()


def response(tokens=10, *, final=False):
    return ModelTurn(
        text="finished" if final else None,
        tool_calls=[] if final else [ToolCall(name="read_file", arguments={"path": "fixture.txt"})],
        usage=TokenUsage(input_tokens=tokens, output_tokens=2, cached_tokens=3),
    )


class ScriptedProvider(OfflineProvider):
    def __init__(self, script):
        super().__init__()
        self.script = iter(script)

    async def chat(self, messages, tools):
        self.calls += 1
        item = next(self.script)
        if isinstance(item, BaseException):
            raise item
        return item


def install(runtime, script):
    provider = ScriptedProvider(script)
    original = runtime._build_loop
    runtime._build_loop = lambda **kwargs: original(**{**kwargs, "provider": provider})
    return provider


def recorded(e):
    return e.db.query_all("SELECT input_tokens, output_tokens, cached_tokens FROM usage WHERE session_id=?",
                          (e.runtime.session_id,))


def events(e):
    return e.manager.transcript.replay(e.runtime.session_id)


def reconciliation(e):
    return [event.payload["usage_reconciliation"] for event in events(e)
            if "usage_reconciliation" in event.payload]


def displayed_usage(e):
    """The UI adds old terminal totals and only new reconciliation deltas."""
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    for event in events(e):
        usage = (event.payload.get("usage") if event.type in (EventType.done, EventType.error)
                 else (event.payload.get("usage_reconciliation") or {}).get("delta"))
        for field in totals:
            totals[field] += (usage or {}).get(field, 0)
    return totals


def test_final_response_recorded_once_and_terminal_not_doubled(environment):
    e = environment
    install(e.runtime, [response(10), response(20, final=True)])
    result = asyncio.run(e.runtime.submit("offline bounded task"))
    assert result.usage.model_dump() == {"input_tokens": 30, "output_tokens": 4, "cached_tokens": 6}
    assert recorded(e) == [result.usage.model_dump()]
    assert displayed_usage(e) == result.usage.model_dump()
    assert len(reconciliation(e)) == 1
    assert reconciliation(e)[0]["delta"] == dict.fromkeys(result.usage.model_dump(), 0)
    assert reconciliation(e)[0]["outcome"] == "completed"
    assert reconciliation(e)[0]["model_responses"] == 2
    assert reconciliation(e)[0]["duration_seconds"] >= 0


def test_provider_error_keeps_paid_response_before_error(environment):
    e = environment
    provider = install(e.runtime, [response(10), RuntimeError("offline provider failure")])
    with pytest.raises(RuntimeError, match="provider failure"):
        asyncio.run(e.runtime.submit("offline error case"))
    expected = {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 3}
    assert provider.calls == 2
    assert recorded(e) == [expected]
    assert displayed_usage(e) == expected
    assert reconciliation(e)[0]["usage"] == expected
    assert reconciliation(e)[0]["outcome"] == "error"


def test_error_after_segment_reconciles_only_unreported_tail(environment, monkeypatch):
    e = environment
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", 2)
    provider = install(e.runtime, [response(10), response(20), response(30), RuntimeError("tail failure")])
    with pytest.raises(RuntimeError, match="tail failure"):
        asyncio.run(e.runtime.submit("offline multi segment case"))
    expected = {"input_tokens": 60, "output_tokens": 6, "cached_tokens": 9}
    assert provider.calls == 4
    assert recorded(e) == [expected]
    assert displayed_usage(e) == expected
    assert reconciliation(e)[0]["delta"] == {"input_tokens": 30, "output_tokens": 2, "cached_tokens": 3}


def test_successful_segments_write_one_complete_prompt_usage(environment, monkeypatch):
    e = environment
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", 1)
    install(e.runtime, [response(10), response(20), response(30, final=True)])
    result = asyncio.run(e.runtime.submit("offline completed multi segment task"))
    expected = {"input_tokens": 60, "output_tokens": 6, "cached_tokens": 9}
    assert result.usage.model_dump() == expected
    assert recorded(e) == [expected]
    assert displayed_usage(e) == expected
    assert reconciliation(e)[0]["delta"] == dict.fromkeys(expected, 0)


def test_budget_stop_keeps_usage_and_never_requests_another_response(environment):
    e = environment
    ledger = TeamBudget(e.db, e.runtime.session_id)
    ledger.begin("fixture-project", 1, {"max_tokens": 12})
    e.runtime._execution_budget = ledger
    provider = install(e.runtime, [response(10), response(20, final=True)])
    with pytest.raises(ExecutionBudgetExceeded, match="Token"):
        asyncio.run(e.runtime.submit("offline task budget case"))
    expected = {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 3}
    assert provider.calls == 1
    assert recorded(e) == [expected]
    assert displayed_usage(e) == expected
    assert ledger.status()["tokens"] == ledger.status()["team_tokens"] == 12
    assert ledger.status()["turns"] == 1


def test_budget_consume_failure_accounts_paid_response_before_failing_closed(environment):
    e = environment

    def fail(usage):
        raise RuntimeError("offline local meter unavailable")

    e.runtime._execution_budget = SimpleNamespace(
        consume=fail, check=lambda: None, remaining_seconds=lambda: None,
    )
    provider = install(e.runtime, [response(10), response(20, final=True)])
    with pytest.raises(RuntimeError, match="meter unavailable"):
        asyncio.run(e.runtime.submit("offline ledger failure"))
    expected = {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 3}
    assert provider.calls == 1
    assert recorded(e) == [expected]
    assert displayed_usage(e) == expected
    assert not any(event.type == EventType.tool_use for event in events(e))


def test_cancellation_during_tool_keeps_usage_without_waiting_for_tool(environment):
    e = environment

    async def scenario():
        entered = asyncio.Event()

        class BlockRead(ReadFileTool):
            async def execute(self, args, ctx):
                entered.set()
                await asyncio.Event().wait()

        registry = ToolRegistry()
        registry.register(BlockRead())
        e.runtime.registry = registry
        provider = install(e.runtime, [response(10), response(20, final=True)])
        task = asyncio.create_task(e.runtime.submit("offline cancellation case"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.calls == 1

    asyncio.run(scenario())
    expected = {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 3}
    assert recorded(e) == [expected]
    assert displayed_usage(e) == expected
    assert reconciliation(e)[0]["outcome"] == "cancelled"


def test_final_checkpoint_failure_records_once_without_recounting_done(environment, monkeypatch):
    e = environment
    install(e.runtime, [response(10, final=True)])
    original = e.runtime._record_checkpoint

    def checkpoint(**kwargs):
        if kwargs.get("label") == "本轮结束":
            raise RuntimeError("offline final checkpoint failure")
        return original(**kwargs)

    monkeypatch.setattr(e.runtime, "_record_checkpoint", checkpoint)
    with pytest.raises(RuntimeError, match="checkpoint failure"):
        asyncio.run(e.runtime.submit("offline final checkpoint case"))
    expected = {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 3}
    assert recorded(e) == [expected]
    assert displayed_usage(e) == expected
    assert reconciliation(e)[0]["delta"] == dict.fromkeys(expected, 0)


def test_two_prompts_have_separate_ledgers_and_replay_does_not_charge_budget(environment):
    e = environment
    ledger = TeamBudget(e.db, e.runtime.session_id)
    ledger.begin("fixture-project", 1, None)
    e.runtime._execution_budget = ledger
    install(e.runtime, [response(10, final=True), response(20, final=True)])

    async def scenario():
        await e.runtime.submit("offline task one")
        await e.runtime.submit("offline task two")
        # Reconnect/replay may rebroadcast old persisted events, never re-meter.
        for event in events(e):
            e.runtime.broadcast(event)

    asyncio.run(scenario())
    assert len(recorded(e)) == 2
    assert len({item["prompt_id"] for item in reconciliation(e)}) == 2
    assert displayed_usage(e) == {"input_tokens": 30, "output_tokens": 4, "cached_tokens": 6}
    assert ledger.status()["tokens"] == ledger.status()["team_tokens"] == 34
    assert ledger.status()["turns"] == 2


def test_failure_before_first_response_records_zero_without_fabricating_usage(environment):
    e = environment
    install(e.runtime, [RuntimeError("offline no response")])
    with pytest.raises(RuntimeError, match="no response"):
        asyncio.run(e.runtime.submit("offline initial failure"))
    zero = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    assert recorded(e) == [zero]
    assert displayed_usage(e) == zero
    assert reconciliation(e)[0]["usage"] == zero


def test_execution_deadline_keeps_prior_paid_response(environment):
    e = environment

    class HeldSecondResponse(OfflineProvider):
        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return response(10)
            await asyncio.Event().wait()

    budget = SimpleNamespace(check=lambda: None, consume=lambda usage: None,
                             remaining_seconds=lambda: 0.5)
    e.runtime._execution_budget = budget
    provider = HeldSecondResponse()
    original = e.runtime._build_loop
    e.runtime._build_loop = lambda **kwargs: original(**{**kwargs, "provider": provider})
    with pytest.raises(ExecutionBudgetExceeded, match="耗时预算"):
        asyncio.run(e.runtime.submit("offline wall-clock budget case"))
    expected = {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 3}
    assert provider.calls == 2
    assert recorded(e) == [expected]
    assert displayed_usage(e) == expected
    assert reconciliation(e)[0]["outcome"] == "error"


def test_old_terminal_replay_during_new_prompt_cannot_hide_new_failed_usage(environment):
    e = environment
    install(e.runtime, [response(10, final=True)])
    asyncio.run(e.runtime.submit("offline first completed task"))
    old_events = events(e)

    class ReplayBeforeFail(OfflineProvider):
        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                for event in old_events:
                    e.runtime.broadcast(event)
                return response(20)
            raise RuntimeError("offline new failure")

    provider = ReplayBeforeFail()
    e.runtime._build_loop = lambda **kwargs: SessionRuntime._build_loop(
        e.runtime, **{**kwargs, "provider": provider})
    with pytest.raises(RuntimeError, match="new failure"):
        asyncio.run(e.runtime.submit("offline new task"))
    expected = {"input_tokens": 20, "output_tokens": 2, "cached_tokens": 3}
    assert recorded(e)[-1] == expected
    assert reconciliation(e)[-1]["delta"] == expected
    assert displayed_usage(e) == {"input_tokens": 30, "output_tokens": 4, "cached_tokens": 6}
