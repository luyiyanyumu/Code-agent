"""Read-only sessions can grant one operation without changing their permission mode."""

import asyncio

import pytest

from code_agent import settings
from code_agent.agent.router import ModelRouter
from code_agent.models.events import EventType, ModelTurn, ToolCall
from code_agent.permissions.policy import Decision
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions import manager as manager_mod
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.tools_builtin import WriteFileTool
from code_agent.tools.registry import ToolRegistry


@pytest.fixture
def runtime_factory(agent_home, monkeypatch):
    # These scenarios exercise tool authorization, independently of closure checks.
    monkeypatch.setattr(manager_mod, "CLOSURE_LIMIT", 0)
    settings.save_permission_mode("workspace-write")
    databases = []

    def create(script, *, strict=False):
        ordinal = len(databases)
        db = Database(agent_home / f"permission-requests-{ordinal}.db")
        databases.append(db)
        user = UserRepo(db).create(f"permission-requests-{ordinal}", "test-only")
        router = ModelRouter(provider_name="offline")
        router.provider_override = OfflineProvider(script=script)
        registry = ToolRegistry()
        registry.register(WriteFileTool())
        manager = SessionManager(
            transcript=TranscriptStore(agent_home / f"transcripts-{ordinal}"),
            session_repo=SessionRepo(db), approval_repo=ApprovalRepo(db),
            router=router, registry=registry,
        )
        # Automation creates a fresh strict runtime directly from its row;
        # manager.create() eagerly creates an interactive runtime instead.
        meta = (manager.session_repo.create(user_id=user["id"], permission_mode="read-only")
                if strict else manager.create(user["id"], permission_mode="read-only"))
        runtime = manager.get_runtime(meta["id"], user["id"], strict=strict)
        return runtime, manager

    yield create
    for db in databases:
        db.close()


def write_turn(path, content="approved content"):
    return OfflineProvider.tool_call_turn("write_file", {"path": path, "content": content, "_approval": {
        "purpose": "保存本次模拟文件结果", "effects": [f"写入 {path}，可能替换已有内容"]}})


async def next_request(queue, events):
    async with asyncio.timeout(3):
        while True:
            event = await queue.get()
            events.append(event)
            if event.type == EventType.approval_request and event.payload.get("approval_id"):
                return event.payload


def drain(queue, events):
    while not queue.empty():
        events.append(queue.get_nowait())


def decisions(events, approval_id):
    return [event.payload for event in events
            if event.type == EventType.approval_decided and event.payload.get("approval_id") == approval_id]


async def cleanup(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_read_only_grant_writes_once_and_next_operation_requests_again(runtime_factory):
    runtime, manager = runtime_factory([
        write_turn("first.txt"), write_turn("second.txt"), OfflineProvider.text_turn("finished"),
    ])
    queue, events = runtime.subscribe(), []

    async def scenario():
        task = asyncio.create_task(runtime.submit("write two files"))
        try:
            first = await next_request(queue, events)
            assert first["scope"] == "once"
            assert not (runtime.workspace / "first.txt").exists()
            assert not (runtime.workspace / "second.txt").exists()
            assert runtime.pending_approval()["approval_id"] == first["approval_id"]
            assert runtime.phase == "waiting_approval"
            assert manager.decide_approval(runtime.user_id, first["approval_id"], True)

            second = await next_request(queue, events)
            assert second["approval_id"] != first["approval_id"]
            assert (runtime.workspace / "first.txt").read_text(encoding="utf-8") == "approved content"
            assert not (runtime.workspace / "second.txt").exists()
            assert runtime.permission_mode == "read-only"
            assert manager.get_meta(runtime.session_id, runtime.user_id)["permission_mode"] == "read-only"
            assert settings.permission_mode() == "workspace-write"
            assert runtime.decide_approval(second["approval_id"], False)
            assert (await asyncio.wait_for(task, 3)).text == "finished"
        finally:
            await cleanup(task)
        drain(queue, events)
        assert decisions(events, first["approval_id"])[-1]["approved"] is True
        assert decisions(events, second["approval_id"])[-1]["approved"] is False
        assert not (runtime.workspace / "second.txt").exists()
        assert runtime.pending_approval() is None
        assert not runtime.busy and runtime.phase == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["denied", "timeout", "cancelled"])
def test_unapproved_operations_never_write(runtime_factory, monkeypatch, outcome):
    if outcome == "timeout":
        monkeypatch.setattr(manager_mod, "APPROVAL_TIMEOUT", 0.05)
    runtime, _ = runtime_factory([write_turn("blocked.txt"), OfflineProvider.text_turn("finished")])
    queue, events = runtime.subscribe(), []

    async def scenario():
        task = asyncio.create_task(runtime.submit("write a file"))
        try:
            request = await next_request(queue, events)
            assert not (runtime.workspace / "blocked.txt").exists()
            if outcome == "denied":
                assert runtime.decide_approval(request["approval_id"], False)
            elif outcome == "cancelled":
                runtime.cancel()
            if outcome == "cancelled":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
                assert runtime.router.resolve_default().calls == 1
            else:
                await asyncio.wait_for(task, 3)
        finally:
            await cleanup(task)
        drain(queue, events)
        assert not (runtime.workspace / "blocked.txt").exists()
        assert runtime.pending_approval() is None
        assert not runtime.approval_repo.pending_for_session(runtime.session_id)
        assert decisions(events, request["approval_id"])[-1]["approved"] is False
        assert decisions(events, request["approval_id"])[-1]["status"] == outcome
        assert not runtime.decide_approval(request["approval_id"], True)
        assert not runtime.busy and runtime.phase == ""
        assert runtime.permission_mode == "read-only"

    asyncio.run(scenario())


def test_parallel_operations_request_one_at_a_time_and_old_decisions_do_not_clear_next(runtime_factory):
    runtime, _ = runtime_factory([
        ModelTurn(tool_calls=[
            write_turn("parallel-a.txt", "A").tool_calls[0],
            write_turn("parallel-b.txt", "B").tool_calls[0],
        ], stop_reason="tool_use"),
        OfflineProvider.text_turn("finished"),
    ])
    queue, events = runtime.subscribe(), []

    async def scenario():
        task = asyncio.create_task(runtime.submit("write files in parallel"))
        try:
            first = await next_request(queue, events)
            await asyncio.sleep(0)
            drain(queue, events)
            assert len([event for event in events if event.type == EventType.approval_request
                        and event.payload.get("approval_id")]) == 1
            assert len(runtime.approval_repo.pending_for_session(runtime.session_id)) == 1
            # Activity elsewhere must not hide the still-live approval card.
            runtime._set_phase("model")
            assert runtime.phase == "waiting_approval"
            runtime._set_phase("preparing_context")
            assert runtime.phase == "waiting_approval"
            assert runtime.decide_approval(first["approval_id"], True)
            second = await next_request(queue, events)
            assert second["approval_id"] != first["approval_id"]
            assert runtime.pending_approval()["approval_id"] == second["approval_id"]
            assert not runtime.decide_approval(first["approval_id"], False)
            assert runtime.approval_repo.get(first["approval_id"])["status"] == "approved"
            assert runtime.pending_approval()["approval_id"] == second["approval_id"]
            assert runtime.phase == "waiting_approval"
            assert len(list(runtime.workspace.glob("parallel-*.txt"))) == 1
            assert runtime.decide_approval(second["approval_id"], False)
            await asyncio.wait_for(task, 3)
        finally:
            await cleanup(task)
        drain(queue, events)
        assert decisions(events, first["approval_id"])[-1]["approved"] is True
        assert decisions(events, second["approval_id"])[-1]["approved"] is False
        assert len(list(runtime.workspace.glob("parallel-*.txt"))) == 1
        assert runtime.pending_approval() is None and runtime.phase == ""

    asyncio.run(scenario())


def test_cancel_clears_active_and_queued_parallel_requests(runtime_factory):
    runtime, _ = runtime_factory([
        ModelTurn(tool_calls=[
            write_turn("cancel-a.txt", "A").tool_calls[0],
            write_turn("cancel-b.txt", "B").tool_calls[0],
        ], stop_reason="tool_use"),
        OfflineProvider.text_turn("should not run"),
    ])
    queue, events = runtime.subscribe(), []

    async def scenario():
        task = asyncio.create_task(runtime.submit("write files in parallel"))
        try:
            first = await next_request(queue, events)
            runtime.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            await cleanup(task)
        drain(queue, events)
        assert not list(runtime.workspace.glob("cancel-*.txt"))
        assert len([event for event in events if event.type == EventType.approval_request
                    and event.payload.get("approval_id")]) == 1
        assert decisions(events, first["approval_id"])[-1]["approved"] is False
        assert runtime.pending_approval() is None
        assert not runtime.approval_repo.pending_for_session(runtime.session_id)
        assert runtime.phase == "" and not runtime.busy
        assert runtime.router.resolve_default().calls == 1

    asyncio.run(scenario())


def test_existing_loop_reads_changed_runtime_permission_mode(runtime_factory):
    runtime, manager = runtime_factory([OfflineProvider.text_turn("unused")])
    loop = runtime._build_loop()
    ctx = ToolContext(session_id=runtime.session_id, user_id=runtime.user_id, workspace=runtime.workspace)
    arguments = {"path": "example.txt", "content": "example"}

    async def scenario():
        assert (await loop.permissions.evaluate("write_file", arguments, ctx)).decision == Decision.need_approval
        manager.set_permission_mode(runtime.session_id, runtime.user_id, "workspace-write")
        assert (await loop.permissions.evaluate("write_file", arguments, ctx)).decision == Decision.allow
        manager.set_permission_mode(runtime.session_id, runtime.user_id, "read-only")
        assert (await loop.permissions.evaluate("write_file", arguments, ctx)).decision == Decision.need_approval

    asyncio.run(scenario())


def test_strict_read_only_runtime_declines_without_waiting(runtime_factory):
    runtime, _ = runtime_factory([write_turn("strict.txt"), OfflineProvider.text_turn("finished")], strict=True)
    queue, events = runtime.subscribe(), []

    async def scenario():
        result = await asyncio.wait_for(runtime.submit("write file"), 3)
        assert result.text == "finished"
        assert not (runtime.workspace / "strict.txt").exists()
        assert runtime.pending_approval() is None and runtime.phase == ""
        assert not runtime.approval_repo.pending_for_session(runtime.session_id)

    asyncio.run(scenario())
    drain(queue, events)
    assert not [event for event in events if event.type == EventType.approval_request
                and event.payload.get("approval_id")]


def test_stale_database_request_without_live_future_is_not_restored(runtime_factory):
    runtime, _ = runtime_factory([OfflineProvider.text_turn("unused")])
    runtime.approval_repo.create(
        session_id=runtime.session_id, user_id=runtime.user_id,
        tool_name="write_file", command="stale operation", reason="old process",
    )
    assert runtime.pending_approval() is None


def test_request_future_exists_before_request_is_broadcast(runtime_factory, monkeypatch):
    runtime, _ = runtime_factory([write_turn("immediate.txt"), OfflineProvider.text_turn("finished")])
    broadcast = runtime.broadcast
    immediate_results = []

    def immediate_reply(event):
        if event.type == EventType.approval_request and event.payload.get("approval_id"):
            approval_id = event.payload["approval_id"]
            assert runtime.pending_approval()["approval_id"] == approval_id
            immediate_results.append(runtime.decide_approval(approval_id, True))
        broadcast(event)

    monkeypatch.setattr(runtime, "broadcast", immediate_reply)

    async def scenario():
        await asyncio.wait_for(runtime.submit("write file"), 3)
        assert immediate_results == [True]
        assert (runtime.workspace / "immediate.txt").read_text(encoding="utf-8") == "approved content"
        assert runtime.pending_approval() is None and runtime.phase == ""

    asyncio.run(scenario())
