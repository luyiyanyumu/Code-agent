"""Bounded, durable completion handoffs using only local runtimes/providers."""

from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from types import SimpleNamespace

import pytest

from code_agent.agent.project_team import ProjectTeam
from code_agent.agent.router import ModelRouter
from code_agent.config import tool_results_dir
from code_agent.models.events import EventType
from code_agent.providers.offline import OfflineProvider
from code_agent.providers.openai_compat import LLMError
from code_agent.sessions.manager import MAX_JOB_NOTICE_CHARS, SessionManager
from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin.tools_builtin import ReadFileTool
from code_agent.tools.registry import ToolRegistry


@pytest.fixture(params=[False, True], ids=["ordinary", "project-lead"])
def environment(agent_home, monkeypatch, request):
    monkeypatch.setattr("code_agent.sessions.manager.MAX_CONSECUTIVE_WAKES", 3)
    db = Database(agent_home / "receipt-recovery.db")
    user = UserRepo(db).create("receipt-recovery", "test-only")["id"]
    projects = ProjectRepo(db)
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    manager = SessionManager(transcript=TranscriptStore(agent_home / "transcripts"),
        session_repo=SessionRepo(db), approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"), registry=registry, project_repo=projects)
    team = None
    if request.param:
        root = agent_home / "project"
        root.mkdir()
        project = projects.create(user, "receipt-recovery", str(root), str(root))
        team = ProjectTeam(manager)
        manager.agent_team = team
        meta = manager.create_project_session(user, project["id"], permission_mode="read-only")
    else:
        meta = manager.create(user, permission_mode="read-only")
    runtime = manager.get_runtime(meta["id"], user)
    (runtime.workspace / "source.txt").write_text("source evidence", encoding="utf-8")
    yield SimpleNamespace(runtime=runtime, manager=manager, team=team)
    runtime.shutdown()
    db.close()


def note(activation, *, size=1600):
    return {"session_id": "s_receipt_child", "activation_id": activation,
            "title": "Local child", "state": "completed",
            "result": f"receipt-{activation:03d}-start|" + "x" * size + f"|receipt-{activation:03d}-end",
            "result_truncated": False, "error": "", "error_truncated": False,
            "audit_required": False, "audit_available": True,
            "execution_ref": {"session_id": "s_receipt_child", "activation_id": activation}}


def assign_provider(runtime, provider):
    original = runtime._build_loop
    runtime._build_loop = lambda **kwargs: original(**{**kwargs, "provider": provider})


def deliveries(runtime):
    return [event for event in runtime.transcript.replay(runtime.session_id)
            if event.type == EventType.user_message and event.payload.get("inbox_ids")]


class RecordingProvider(OfflineProvider):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime
        self.messages = []
        self.boundaries = []
        self.called_at = []
        self.last_seq = 0

    async def chat(self, messages, tools):
        self.calls += 1
        self.called_at.append(asyncio.get_running_loop().time())
        self.messages.append(deepcopy(messages))
        fresh = [event for event in deliveries(self.runtime) if event.seq > self.last_seq]
        self.boundaries.append(fresh)
        if fresh:
            self.last_seq = fresh[-1].seq
        return self.text_turn("已收到回执并核验")


async def idle(runtime):
    # Give both the finishing task and its call_soon handoff an opportunity to
    # run; an empty pending inbox alone can precede the provider's final return.
    async with asyncio.timeout(5):
        while runtime.busy or runtime._pending_team_receipts():
            await asyncio.sleep(0.001)
        await asyncio.sleep(0)


def assert_delivery_once(runtime, provider, identities):
    events = deliveries(runtime)
    assert Counter(identity for event in events for identity in event.payload["inbox_ids"]) == Counter(identities)
    for messages, fresh in zip(provider.messages, provider.boundaries):
        content = [str(message.get("content") or "") for message in messages]
        # Every acknowledged receipt really reaches the same provider boundary
        # verbatim, including the end of each result, after history compaction.
        assert all(event.payload["text"] in content for event in fresh)
        assert sum(len(event.payload["text"]) for event in fresh) + max(0, len(fresh) - 1) * 2 <= MAX_JOB_NOTICE_CHARS


def test_many_receipts_reach_the_model_in_bounded_batches_without_loss(environment):
    runtime = environment.runtime
    provider = RecordingProvider(runtime)
    assign_provider(runtime, provider)

    async def scenario():
        for activation in range(1, 26):
            runtime.enqueue_team_note(note(activation))
        identities = [row["id"] for row in runtime._team_inbox.pending()]
        assert sum(len(runtime._team_inbox.text(row)) for row in runtime._team_inbox.pending()) > 40_000
        await asyncio.sleep(0)
        await idle(runtime)
        assert provider.calls == runtime._consecutive_wakes == 2
        assert_delivery_once(runtime, provider, identities)
        assert len(provider.boundaries[0]) < 25
        assert len(deliveries(runtime)) == 25
        if environment.team:
            await environment.team.aclose()

    asyncio.run(scenario())


def test_completion_wake_also_delivers_queued_messages_within_the_same_bound(environment):
    runtime = environment.runtime
    provider = RecordingProvider(runtime)
    assign_provider(runtime, provider)

    async def scenario():
        message = runtime.enqueue_agent_message({"message": "Keep the existing public interface"})
        await asyncio.sleep(0)
        assert not runtime.busy and provider.calls == 0
        runtime.enqueue_team_note(note(1))
        identities = [row["id"] for row in runtime._team_inbox.pending()]
        await asyncio.sleep(0)
        await idle(runtime)
        assert not runtime._team_inbox.pending()
        assert provider.calls == runtime._consecutive_wakes == 1
        assert_delivery_once(runtime, provider, identities)
        assert any(message["message_id"] in event.payload["inbox_ids"] for event in provider.boundaries[0])
        if environment.team:
            await environment.team.aclose()

    asyncio.run(scenario())


def test_wake_budget_preserves_remaining_batches_until_explicit_submission(environment, request):
    runtime = environment.runtime
    provider = RecordingProvider(runtime)
    assign_provider(runtime, provider)

    async def scenario():
        # Exercise more than three real 40k batches with fewer durable inbox
        # transactions. The separate 25-receipt test covers many-message load;
        # this case verifies wake-budget exhaustion and explicit recovery.
        for activation in range(1, 9):
            runtime.enqueue_team_note(note(activation, size=16_000))
        identities = [row["id"] for row in runtime._team_inbox.pending()]
        assert sum(len(runtime._team_inbox.text(row)) for row in runtime._team_inbox.pending()) > 3 * MAX_JOB_NOTICE_CHARS
        started = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(5):
                while runtime._consecutive_wakes < 3 or runtime.busy:
                    await asyncio.sleep(0.001)
        except TimeoutError:
            member = environment.team.descriptor(runtime.session_id) if environment.team else {}
            pytest.fail(f"wake handoff did not settle: calls={provider.calls}, wakes={runtime._consecutive_wakes}, "
                        f"busy={runtime.busy}, phase={runtime.phase}, state={member.get('state')}, error={member.get('error')}, "
                        f"remaining={len(runtime._team_inbox.pending())}, "
                        f"model_boundaries_seconds={[round(stamp - started, 3) for stamp in provider.called_at]}")
        for _ in range(5):
            await asyncio.sleep(0)
        request.node.user_properties.extend([
            ("automatic_model_boundaries_seconds", [round(stamp - started, 3) for stamp in provider.called_at]),
            ("automatic_handoff_seconds", round(asyncio.get_running_loop().time() - started, 3)),
            ("automatic_terminal", f"calls={provider.calls}, wakes={runtime._consecutive_wakes}, busy={runtime.busy}"),
        ])
        remaining = runtime._team_inbox.pending()
        assert provider.calls == runtime._consecutive_wakes == 3
        assert 0 < len(remaining) < len(identities)
        assert len(deliveries(runtime)) + len(remaining) == len(identities)
        await runtime.submit("继续核验尚未处理的子任务回执")
        await idle(runtime)
        assert runtime._consecutive_wakes <= 3
        assert_delivery_once(runtime, provider, identities)
        if environment.team:
            await environment.team.aclose()

    asyncio.run(scenario())


def test_busy_model_boundary_also_bounds_receipts_and_schedules_the_remainder(environment):
    runtime = environment.runtime

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class BusyProvider(RecordingProvider):
            async def chat(self, messages, tools):
                result = await super().chat(messages, tools)
                if self.calls == 1:
                    entered.set()
                    await release.wait()
                    return self.tool_call_turn("read_file", {"path": "source.txt"})
                return result

        provider = BusyProvider(runtime)
        assign_provider(runtime, provider)
        task = asyncio.create_task(runtime.submit("检查 source.txt 并核验子任务结果"))
        await asyncio.wait_for(entered.wait(), 3)
        for activation in range(1, 26):
            runtime.enqueue_team_note(note(activation))
        identities = [row["id"] for row in runtime._team_inbox.pending()]
        release.set()
        await task
        await idle(runtime)
        assert provider.calls == 3 and runtime._consecutive_wakes == 1
        assert_delivery_once(runtime, provider, identities)
        if environment.team:
            await environment.team.aclose()

    asyncio.run(scenario())


def test_receipt_arriving_before_transient_failure_wakes_after_exception(environment):
    runtime = environment.runtime

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class FailOnce(RecordingProvider):
            async def chat(self, messages, tools):
                result = await super().chat(messages, tools)
                if self.calls == 1:
                    entered.set()
                    await release.wait()
                    raise LLMError("HTTP 503: temporarily unavailable")
                return result

        provider = FailOnce(runtime)
        assign_provider(runtime, provider)
        task = asyncio.create_task(runtime.submit("等待子任务结果"))
        await asyncio.wait_for(entered.wait(), 3)
        runtime.enqueue_team_note(note(1))
        identities = [row["id"] for row in runtime._team_inbox.pending()]
        release.set()
        with pytest.raises(LLMError, match="503"):
            await task
        await idle(runtime)
        assert provider.calls == 2 and runtime._consecutive_wakes == 1
        assert not runtime._provider_wake_blocked
        assert_delivery_once(runtime, provider, identities)
        if environment.team:
            await environment.team.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["cancel", "interrupt_current_turn"])
def test_deliberate_stop_does_not_wake_for_a_late_receipt(environment, action):
    runtime = environment.runtime

    async def scenario():
        entered = asyncio.Event()

        class Block(RecordingProvider):
            async def chat(self, messages, tools):
                await super().chat(messages, tools)
                entered.set()
                await asyncio.Event().wait()

        provider = Block(runtime)
        assign_provider(runtime, provider)
        task = asyncio.create_task(runtime.submit("等待子任务结果"))
        await asyncio.wait_for(entered.wait(), 3)
        runtime.enqueue_team_note(note(1))
        getattr(runtime, action)()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(10):
            await asyncio.sleep(0)
        runtime._wake_for_jobs()
        assert not runtime.busy and runtime._team_wake_blocked
        assert provider.calls == 1 and runtime._consecutive_wakes == 0
        assert len(runtime._team_inbox.pending()) == 1 and not deliveries(runtime)
        if environment.team:
            await environment.team.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("error", ["HTTP 402: Insufficient Balance", 'HTTP 429: {"code":"insufficient_quota"}'])
def test_account_failure_preserves_busy_receipt_without_retrying(environment, error):
    runtime = environment.runtime

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockThenFail(RecordingProvider):
            async def chat(self, messages, tools):
                await super().chat(messages, tools)
                entered.set()
                await release.wait()
                raise LLMError(error)

        provider = BlockThenFail(runtime)
        assign_provider(runtime, provider)
        task = asyncio.create_task(runtime.submit("等待子任务结果"))
        await asyncio.wait_for(entered.wait(), 3)
        runtime.enqueue_team_note(note(1))
        release.set()
        with pytest.raises(LLMError):
            await task
        for _ in range(10):
            await asyncio.sleep(0)
        runtime._wake_for_jobs()
        assert not runtime.busy and runtime._provider_wake_blocked
        assert provider.calls == 1 and runtime._consecutive_wakes == 0
        assert len(runtime._team_inbox.pending()) == 1 and not deliveries(runtime)
        if environment.team:
            await environment.team.aclose()

    asyncio.run(scenario())


def test_one_oversized_legacy_receipt_delivers_a_bounded_excerpt_with_full_source(environment):
    runtime = environment.runtime
    provider = RecordingProvider(runtime)
    assign_provider(runtime, provider)

    async def scenario():
        runtime.enqueue_team_note(note(1, size=100_000))
        row = runtime._team_inbox.pending()[0]
        full_text = str(runtime._team_inbox.text(row))
        await asyncio.sleep(0)
        await idle(runtime)
        assert provider.calls == 1
        assert_delivery_once(runtime, provider, [row["id"]])
        text = deliveries(runtime)[0].payload["text"]
        assert "[外部化]" in text and "全文已存盘:" in text and "省略" in text
        saved = list((tool_results_dir() / runtime.session_id).glob("*.txt"))
        assert len(saved) == 1 and saved[0].read_text(encoding="utf-8") == full_text
        assert str(saved[0]) in text
        if environment.team:
            await environment.team.aclose()

    asyncio.run(scenario())


def test_cancel_before_first_model_boundary_keeps_the_selected_batch_pending(environment, monkeypatch):
    runtime = environment.runtime
    provider = RecordingProvider(runtime)
    assign_provider(runtime, provider)
    prepare = runtime._prepare_context

    async def scenario():
        entered = asyncio.Event()

        async def blocked_prepare(prompt):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(runtime, "_prepare_context", blocked_prepare)
        runtime.enqueue_team_note(note(1))
        await asyncio.wait_for(entered.wait(), 3)
        wake = runtime._task
        assert runtime._wake_batch and len(runtime._team_inbox.pending()) == 1
        runtime.interrupt_current_turn()
        with pytest.raises(asyncio.CancelledError):
            await wake
        for _ in range(5):
            await asyncio.sleep(0)
        assert provider.calls == 0 and len(runtime._team_inbox.pending()) == 1
        assert not deliveries(runtime) and runtime._wake_batch is None
        monkeypatch.setattr(runtime, "_prepare_context", prepare)
        await runtime.submit("继续核验未接收的回执")
        await idle(runtime)
        assert provider.calls == 1 and len(deliveries(runtime)) == 1
        if environment.team:
            await environment.team.aclose()

    asyncio.run(scenario())
