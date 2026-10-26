"""Account failures pause automatic wakes; every provider and DB is local."""

from __future__ import annotations

import asyncio

import pytest

from code_agent.agent.project_team import ProjectTeam
from code_agent.agent.router import ModelRouter
from code_agent.codeintel.sop_workflow import WorkflowStore
from code_agent.models.events import EventType
from code_agent.providers.offline import OfflineProvider
from code_agent.providers.openai_compat import LLMError
from code_agent.sessions.manager import SessionManager, _provider_requires_attention
from code_agent.storage.db import (
    ApprovalRepo,
    Database,
    ProjectRepo,
    SessionRepo,
    UserRepo,
)
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.registry import ToolRegistry


class AccountProvider(OfflineProvider):
    def __init__(self, error: str):
        super().__init__()
        self.error = error
        self.messages = []

    async def chat(self, messages, tools):
        self.calls += 1
        self.messages.append(messages)
        if self.error:
            raise LLMError(self.error)
        return self.text_turn("已恢复，回执已读取")


@pytest.fixture(params=[False, True], ids=["ordinary", "project-lead"])
def environment(agent_home, monkeypatch, request):
    monkeypatch.setattr("code_agent.sessions.manager.CLOSURE_LIMIT", 0)
    monkeypatch.setattr("code_agent.sessions.manager.MAX_CONSECUTIVE_WAKES", 3)
    db = Database(agent_home / "wake-guard.db")
    user = UserRepo(db).create("wake-guard", "test-only")["id"]
    projects = ProjectRepo(db)
    manager = SessionManager(transcript=TranscriptStore(agent_home / "transcripts"),
        session_repo=SessionRepo(db), approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"), registry=ToolRegistry(), project_repo=projects)
    team = None
    if request.param:
        root = agent_home / "project"
        root.mkdir()
        project = projects.create(user, "wake-guard", str(root), str(root))
        team = ProjectTeam(manager, WorkflowStore(db))
        manager.agent_team = team
        meta = manager.create_project_session(user, project["id"], permission_mode="read-only")
    else:
        meta = manager.create(user, permission_mode="read-only")
    runtime = manager.get_runtime(meta["id"], user)
    provider = AccountProvider("HTTP 402: Insufficient Balance")
    original = runtime._build_loop
    runtime._build_loop = lambda **kwargs: original(**{**kwargs, "provider": provider})
    yield runtime, provider, manager, team
    db.close()


def notice(activation):
    return {"session_id": "s_local_receipt", "activation_id": activation,
            "state": "failed", "error": "模型账户待处理"}


async def wait_wake(runtime, provider, calls):
    async def ready():
        while provider.calls < calls or runtime.busy:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(ready(), 5)


def test_balance_failure_keeps_new_receipts_without_automatic_requests(environment):
    runtime, provider, manager, team = environment

    async def check():
        with pytest.raises(LLMError, match="HTTP 402"):
            await runtime.submit("完成当前任务")
        assert runtime._provider_wake_blocked and provider.calls == 1
        prior = manager.transcript.replay(runtime.session_id)
        runtime.enqueue_team_note(notice(1))
        runtime.enqueue_team_note(notice(2))
        await asyncio.sleep(0)
        runtime._wake_for_jobs()
        await asyncio.sleep(0)
        assert provider.calls == 1 and not runtime.busy
        assert len(runtime._team_inbox.pending(receipts_only=True)) == 2
        assert len([e for e in manager.transcript.replay(runtime.session_id) if e.type == EventType.user_message]) == len(
            [e for e in prior if e.type == EventType.user_message])
        if team:
            await team.aclose()

    asyncio.run(check())


def test_explicit_submission_recovers_and_delivers_preserved_receipts(environment):
    runtime, provider, _, team = environment

    async def check():
        with pytest.raises(LLMError):
            await runtime.submit("执行任务")
        runtime.enqueue_team_note(notice(1))
        await asyncio.sleep(0)
        assert provider.calls == 1
        provider.error = ""
        result = await runtime.submit("账户已恢复，继续任务")
        assert result.text and not runtime._provider_wake_blocked
        assert provider.calls == 2 and not runtime._team_inbox.pending(receipts_only=True)
        assert any("s_local_receipt" in str(message.get("content")) for message in provider.messages[-1])
        runtime.enqueue_team_note(notice(2))
        await wait_wake(runtime, provider, 3)
        assert not runtime._team_inbox.pending(receipts_only=True)
        if team:
            await team.aclose()

    asyncio.run(check())


def test_transient_provider_failure_keeps_future_completion_wakes_enabled(environment):
    runtime, provider, _, team = environment
    provider.error = "HTTP 503: temporarily unavailable"

    async def check():
        with pytest.raises(LLMError, match="HTTP 503"):
            await runtime.submit("执行任务")
        assert not runtime._provider_wake_blocked
        provider.error = ""
        runtime.enqueue_team_note(notice(1))
        await wait_wake(runtime, provider, 2)
        assert not runtime._team_inbox.pending(receipts_only=True)
        if team:
            await team.aclose()

    asyncio.run(check())


def test_failure_during_automatic_wake_blocks_later_receipts(environment):
    runtime, provider, _, team = environment

    async def check():
        runtime.enqueue_team_note(notice(1))
        await wait_wake(runtime, provider, 1)
        assert runtime._provider_wake_blocked
        runtime.enqueue_team_note(notice(2))
        await asyncio.sleep(0)
        assert provider.calls == 1 and not runtime.busy
        assert len(runtime._team_inbox.pending(receipts_only=True)) == 1
        if team:
            await team.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("message,expected", [
    ("HTTP 401: invalid api key", True),
    ("HTTP 402: Insufficient Balance", True),
    ("HTTP 403: permission denied", True),
    ('HTTP 429: {"code":"insufficient_quota"}', True),
    ("HTTP 429: rate limit exceeded", False),
    ("HTTP 503: temporary unavailable", False),
    ("HTTP 4010: unknown", False),
])
def test_classifier_only_blocks_account_failures(message, expected):
    assert _provider_requires_attention(LLMError(message)) is expected
    assert _provider_requires_attention(RuntimeError(message)) is False


def test_structured_status_is_supported_without_classifying_tool_errors():
    error = LLMError("Account unavailable")
    error.status_code = 402
    assert _provider_requires_attention(error)
    tool_error = RuntimeError("Account unavailable")
    tool_error.status_code = 402
    assert not _provider_requires_attention(tool_error)
