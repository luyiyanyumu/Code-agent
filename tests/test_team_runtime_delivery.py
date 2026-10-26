"""Local inbox/context/approval integration; no external provider requests."""

import asyncio

import pytest

from code_agent.agent.project_team import ProjectTeam
from code_agent.agent.router import ModelRouter
from code_agent.codeintel.sop_workflow import WorkflowStore
from code_agent.context.prompt import build_history
from code_agent.models.events import Event, EventType, ModelTurn, ToolCall
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import Tool
from code_agent.tools.builtin.tools_builtin import WriteFileTool
from code_agent.tools.registry import ToolRegistry


@pytest.fixture
def environment(agent_home, monkeypatch):
    monkeypatch.setattr("code_agent.sessions.manager.CLOSURE_LIMIT", 0)
    monkeypatch.setattr("code_agent.sessions.manager.MAX_CONSECUTIVE_WAKES", 0)
    root = agent_home / "project"
    root.mkdir()
    db = Database(agent_home / "delivery.db")
    user = UserRepo(db).create("delivery", "test-only")["id"]
    projects = ProjectRepo(db)
    project = projects.create(user, "delivery", str(root), str(root))
    projects.set_flags(project["id"], allow_direct_write=True)
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    manager = SessionManager(transcript=TranscriptStore(agent_home / "transcripts"), session_repo=SessionRepo(db),
                             approval_repo=ApprovalRepo(db), router=ModelRouter(provider_name="offline"),
                             registry=registry, project_repo=projects)
    team = ProjectTeam(manager, WorkflowStore(db))
    manager.agent_team = team
    lead = manager.create_project_session(user, project["id"], permission_mode="read-only")
    roster = team.sync(project["id"], user)
    assert roster["agents"] == []
    for title in ("交付任务", "独立验证"):
        team._create_worker(project, roster["lead"], tree="main", role="subagent", title=title)
    roster = team.sync(project["id"], user)
    yield manager, team, user, lead["id"], roster, root
    db.close()


def assign_provider(runtime, provider):
    original = getattr(runtime, "_test_original_build_loop", runtime._build_loop)
    runtime._test_original_build_loop = original
    runtime._build_loop = lambda **kwargs: original(**{**kwargs, "provider": provider})


def test_message_does_not_wake_idle_and_survives_runtime_recreation(environment):
    manager, team, user, lead, roster, _ = environment
    child = roster["agents"][0]["session_id"]
    seen = []

    class Capture(OfflineProvider):
        async def chat(self, messages, tools):
            seen.append(messages)
            return self.text_turn("received")

    async def scenario():
        receipt = await team.send_message(lead, user, child, "保留现有接口，只补测试")
        runtime = manager.get_runtime(child, user)
        assert receipt["starts_turn"] is False and not runtime.busy
        assert len(runtime._team_inbox.pending()) == 1
        manager._runtimes.pop(child)
        runtime = manager.get_runtime(child, user)
        assign_provider(runtime, Capture())
        await runtime.submit("继续任务")
        assert not runtime._team_inbox.pending()
        assert any("保留现有接口，只补测试" in str(message.get("content")) for message in seen[0])
        notes = [event for event in manager.transcript.replay(child)
                 if event.type == EventType.user_message and event.payload.get("inbox_ids")]
        assert len(notes) == 1
        await runtime.submit("再看一次")
        notes = [event for event in manager.transcript.replay(child)
                 if event.type == EventType.user_message and event.payload.get("inbox_ids")]
        assert len(notes) == 1
        await team.aclose()

    asyncio.run(scenario())


def test_completion_receipt_is_durable_and_idempotent(environment):
    manager, team, user, lead, roster, _ = environment
    runtime = manager.get_runtime(lead, user)
    notice = {"session_id": roster["agents"][0]["session_id"], "activation_id": 7, "state": "completed", "result": "测试结果待核验"}
    runtime.enqueue_team_note(notice)
    runtime.enqueue_team_note(notice)
    assert len(runtime._team_inbox.pending()) == 1
    manager._runtimes.pop(lead)
    rebuilt = manager.get_runtime(lead, user)
    assert "测试结果待核验" in rebuilt._drain_job_notes()[0]
    assert len(rebuilt._team_inbox.pending()) == 1  # Reading is not delivery.
    assign_provider(rebuilt, OfflineProvider(script=[OfflineProvider.text_turn("reviewed")]))
    asyncio.run(rebuilt.submit("核验节点结果"))
    assert not rebuilt._team_inbox.pending()


def test_message_quotes_cannot_acknowledge_another_message_and_rewind_redelivers(environment):
    manager, _, user, lead, _, _ = environment
    runtime = manager.get_runtime(lead, user)
    first = runtime.enqueue_agent_message({"message": "first"})["message_id"]
    second = runtime.enqueue_agent_message({"message": f"引用 [team-inbox:{first}]"})["message_id"]
    event = manager.transcript.append(lead, EventType.user_message,
                                      {"text": f"引用 [team-inbox:{first}]", "inbox_ids": [second], "auto": True})
    runtime.broadcast(event)
    assert [row["id"] for row in runtime._team_inbox.pending()] == [first]
    manager.transcript.truncate(lead, event.seq - 1)
    asyncio.run(runtime._prepare_context("恢复"))
    assert {row["id"] for row in runtime._team_inbox.pending()} == {first, second}


def test_checkpoint_persists_and_rebuilds_after_rewind(environment):
    manager, _, user, lead, _, _ = environment
    runtime = manager.get_runtime(lead, user)
    runtime._context_history_budget = 100
    manager.transcript.append(lead, EventType.user_message, {"text": "目标：保留公开接口，只改内部实现。" * 20})
    manager.transcript.append(lead, EventType.done, {"text": "决定先补回归测试，再调整实现。"})
    _, memory = asyncio.run(runtime._prepare_context("继续"))
    checkpoints = [event for event in manager.transcript.replay(lead) if event.payload.get("work_checkpoint")]
    assert len(checkpoints) == 1
    assert "工作检查点" in str(memory)
    source_seq = checkpoints[0].payload["work_checkpoint"]["source_seq"]
    asyncio.run(runtime._prepare_context("继续"))
    assert len([event for event in manager.transcript.replay(lead) if event.payload.get("work_checkpoint")]) == 1
    manager.transcript.truncate(lead, source_seq - 1)
    asyncio.run(runtime._prepare_context("从回退继续"))
    saved = [event for event in manager.transcript.replay(lead) if event.payload.get("work_checkpoint")][-1]
    assert saved.payload["work_checkpoint"]["source_seq"] < source_seq


def test_cross_thread_team_change_reaches_subscriber_event_loop(environment):
    manager, _, user, lead, roster, _ = environment
    runtime = manager.get_runtime(lead, user)

    async def scenario():
        queue = runtime.subscribe()
        await asyncio.to_thread(manager.notify_team_changed, roster["agents"][0]["session_id"])
        event = await asyncio.wait_for(queue.get(), 1)
        assert event.payload["team_changed"]["project_id"] == roster["lead"]["project_id"]
        runtime.unsubscribe(queue)

    asyncio.run(scenario())


@pytest.mark.parametrize("strategy", ["none", "task_brief", "all"])
def test_delegated_context_has_explicit_source_and_valid_tool_pairs(environment, strategy):
    manager, _, user, lead, roster, _ = environment
    store = manager.transcript
    store.append(lead, EventType.user_message, {"text": "目标：修复导图；不要改接口"})
    store.append(lead, EventType.assistant_message, {"text": "决定复用描述", "tool_calls": [
        {"id": "first", "name": "read_file", "arguments": {"path": "a"}},
        {"id": "second", "name": "read_file", "arguments": {"path": "b"}},
    ]})
    store.append(lead, EventType.tool_result, {"call_id": "second", "result": "b evidence"})
    store.append(lead, EventType.tool_result, {"call_id": "first", "result": "a evidence"})
    store.append(lead, EventType.approval_request, {"approval_id": "old"})
    child = manager.get_runtime(roster["agents"][0]["session_id"], user)
    child.set_delegation_context(lead, strategy, "补齐共享描述")
    history, memory = asyncio.run(child._prepare_context("补齐共享描述"))
    text = str(history) + str(memory)
    assert "old" not in text
    if strategy == "none":
        assert "修复导图" not in text
    else:
        assert "修复导图" in text and lead in str(memory)
    if strategy == "all":
        assert [item["tool_call_id"] for item in history if item["role"] == "tool"] == ["first", "second"]


def test_interrupted_parallel_history_preserves_completed_write_as_evidence():
    events = [
        Event(seq=1, type="assistant_message", payload={"tool_calls": [
            {"id": "written", "name": "write_file", "arguments": {}},
            {"id": "missing", "name": "read_file", "arguments": {}},
        ]}),
        Event(seq=2, type="tool_result", payload={"call_id": "written", "result": "updated.txt 已写入"}),
        Event(seq=3, type="user_message", payload={"text": "继续"}),
    ]
    history = build_history(events)
    assert all(not item.get("tool_calls") and item["role"] != "tool" for item in history)
    assert "updated.txt 已写入" in str(history)
    assert "执行状态需核查" in str(history)


def test_approval_releases_single_slot_and_reacquires_before_write(environment, monkeypatch):
    manager, team, user, lead, roster, root = environment
    monkeypatch.setattr(team, "limits", lambda: {"max_parallel": 1, "max_depth": 3})
    first, second = roster["agents"][:2]
    pending = manager.get_runtime(first["session_id"], user)
    other = manager.get_runtime(second["session_id"], user)
    assign_provider(pending, OfflineProvider(script=[
        OfflineProvider.tool_call_turn("write_file", {"path": "granted.txt", "content": "once", "_approval": {
            "purpose": "验证审批后重新获得单一执行名额才写文件", "effects": ["在临时目录创建 granted.txt"]}}),
        OfflineProvider.text_turn("finished"),
    ]))
    assign_provider(other, OfflineProvider(script=[OfflineProvider.text_turn("independent finished")]))

    async def scenario():
        await team.followup(lead, user, first["session_id"], "write", write_paths=["granted.txt"])
        async with asyncio.timeout(3):
            while not pending.pending_approval() or pending.session_id in team._capacity(first["project_id"]).active:
                await asyncio.sleep(0.005)
        assert team.descriptor(first["session_id"])["phase"] == "waiting_approval"
        task = await team.followup(lead, user, second["session_id"], "independent", write_paths=[])
        result = await team.wait(lead, user, [task["session_id"]], timeout=3)
        assert result["agents"][0]["state"] == "completed"
        assert not (root / "granted.txt").exists()
        pending.decide_approval(pending.pending_approval()["approval_id"], True)
        result = await team.wait(lead, user, [first["session_id"]], timeout=3)
        assert result["agents"][0]["state"] == "completed"
        assert (root / "granted.txt").read_text() == "once"
        assert not team._capacity(first["project_id"]).active
        await team.aclose()

    asyncio.run(scenario())


def test_approval_does_not_release_while_parallel_tool_is_actually_running(environment, monkeypatch):
    manager, team, user, lead, roster, _ = environment
    monkeypatch.setattr(team, "limits", lambda: {"max_parallel": 1, "max_depth": 3})
    child = roster["agents"][0]
    runtime = manager.get_runtime(child["session_id"], user)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class HoldRead(Tool):
            name = "read_file"
            async def execute(self, arguments, ctx):
                entered.set()
                await release.wait()
                return "read returned"

        class ApprovalProbe(Tool):
            # Ordinary connector calls can really run alongside reads. Native
            # file writes are intentionally sequential barriers now.
            name = "parallel_approval_probe"
            async def execute(self, arguments, ctx):
                pytest.fail("Denied approval must never execute the probe")

        manager.registry.register(HoldRead())
        manager.registry.register(ApprovalProbe())

        class Mixed(OfflineProvider):
            async def chat(self, messages, tools):
                if self.calls:
                    return self.text_turn("finished")
                self.calls += 1
                return ModelTurn(tool_calls=[ToolCall(name="read_file"),
                    ToolCall(name="parallel_approval_probe", arguments={"_approval": {
                        "purpose": "验证并行读取未结束时审批等待仍保留名额", "effects": ["申请执行无副作用的本地测试探针"]}})])

        assign_provider(runtime, Mixed())
        # Both custom implementations are conservatively opaque even though
        # this fixture only records/waits in memory; do not bypass the guard by
        # pretending the read_file replacement is a trusted built-in reader.
        await team.followup(lead, user, child["session_id"], "mixed tools", write_paths=["."])
        await asyncio.wait_for(entered.wait(), 3)
        async with asyncio.timeout(3):
            while runtime.pending_approval() is None:
                await asyncio.sleep(0.005)
        assert child["session_id"] in team._capacity(child["project_id"]).active
        assert runtime._active_dispatches == 1
        release.set()
        async with asyncio.timeout(3):
            while child["session_id"] in team._capacity(child["project_id"]).active:
                await asyncio.sleep(0.005)
        runtime.decide_approval(runtime.pending_approval()["approval_id"], False)
        await team.wait(lead, user, [child["session_id"]], timeout=3)
        assert runtime._active_dispatches == 0
        await team.aclose()

    asyncio.run(scenario())
