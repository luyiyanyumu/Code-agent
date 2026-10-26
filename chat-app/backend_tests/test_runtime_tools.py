"""Behavioral tests run entirely offline with disposable conversation data."""

import asyncio
import json
from pathlib import Path

import pytest

from chat_agent.config import Config
from chat_agent.models import Models
from chat_agent.providers import Chunk, ToolCall, Turn
from chat_agent.runtime import BusyError, PendingNotFound, RuntimeManager
from chat_agent.storage import Store
from chat_agent.tools import ToolError, ToolExecutor, scoped_path, upload_path, validate_questions


@pytest.fixture
def service(tmp_path):
    config = Config(home=tmp_path, request_timeout=1, max_rounds=3, max_upload_bytes=100_000)
    store = Store(config)
    models = Models(config)
    manager = RuntimeManager(config, store, models)
    session = store.create_session(user_id="alice")
    yield config, store, manager, session["id"]
    store.close()


async def eventually(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("Conversation state did not settle")


class ScriptedProvider:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.contexts = []

    async def stream(self, messages, tools):
        self.contexts.append(json.loads(json.dumps(messages)))
        turn = next(self.turns)
        if turn.text:
            yield Chunk(text_delta=turn.text)
        yield Chunk(turn=turn)


class BlockingProvider:
    async def stream(self, messages, tools):
        await asyncio.sleep(30)
        yield Chunk(turn=Turn(text="never completes"))


def test_offline_round_replays_and_deltas_are_transient(service):
    _, store, manager, sid = service

    async def scenario():
        queue = manager.subscribe(sid)
        manager.start(sid, "alice", "你好")
        await eventually(lambda: not manager.busy(sid))
        events = store.events(sid)
        assert [event["type"] for event in events] == ["user_message", "assistant_message", "done"]
        assert "你好" in events[1]["payload"]["text"]
        assert events[-1]["payload"]["usage"]["input_tokens"] == 0
        assert store.checkpoints(sid)[0]["anchor_seq"] == events[0]["seq"]
        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait())
        assert any(frame["type"] == "assistant_delta" and frame["seq"] == 0 for frame in frames)
        assert frames[-1]["type"] == "session_meta" and frames[-1]["payload"]["busy"] is False
        assert manager.meta(sid)["phase"] == ""
        manager.unsubscribe(sid, queue)
        await manager.shutdown()

    asyncio.run(scenario())


def test_busy_reservation_and_immediate_cancel_are_consistent(service, monkeypatch):
    _, store, manager, sid = service
    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: BlockingProvider())

    async def scenario():
        manager.start(sid, "alice", "first")
        with pytest.raises(BusyError):
            manager.start(sid, "alice", "second")
        assert manager.cancel(sid, "alice")["cancelled"]
        await eventually(lambda: not manager.busy(sid))
        assert [e["payload"]["text"] for e in store.events(sid) if e["type"] == "user_message"] == ["first"]
        assert store.events(sid)[-1]["payload"]["stop_reason"] == "cancelled"
        assert manager.cancel(sid, "alice")["cancelled"] is False
        await manager.shutdown()

    asyncio.run(scenario())


def test_cancel_running_provider_and_timeout_release_session(service, monkeypatch):
    config, store, manager, sid = service
    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: BlockingProvider())
    config.request_timeout = 0.02

    async def scenario():
        manager.start(sid, "alice", "timeout")
        await eventually(lambda: not manager.busy(sid))
        assert store.events(sid)[-1]["payload"]["stop_reason"] == "timeout"
        assert any(e["type"] == "error" and e["payload"]["code"] == "timeout" for e in store.events(sid))
        config.request_timeout = 1
        manager.start(sid, "alice", "cancel running")
        await asyncio.sleep(0.005)
        manager.cancel(sid, "alice")
        await eventually(lambda: not manager.busy(sid))
        assert store.events(sid)[-1]["payload"]["stop_reason"] == "cancelled"
        await manager.shutdown()

    asyncio.run(scenario())


def test_partial_stream_survives_provider_failure(service, monkeypatch):
    _, store, manager, sid = service

    class BrokenProvider:
        async def stream(self, messages, tools):
            yield Chunk(text_delta="received partial text")
            raise RuntimeError("network interrupted")

    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: BrokenProvider())

    async def scenario():
        manager.start(sid, "alice", "message")
        await eventually(lambda: not manager.busy(sid))
        answer = next(event["payload"] for event in store.events(sid) if event["type"] == "assistant_message")
        assert answer == {"text": "received partial text", "incomplete": True, "stop_reason": "error"}
        assert store.events(sid)[-1]["payload"]["stop_reason"] == "error"
        await manager.shutdown()

    asyncio.run(scenario())


def test_ownership_and_invalid_prompt_do_not_append(service):
    _, store, manager, sid = service

    async def scenario():
        for user in ["bob", ""]:
            with pytest.raises(PendingNotFound):
                manager.start(sid, user, "bad user")
            with pytest.raises(PendingNotFound):
                manager.cancel(sid, user)
        with pytest.raises(ValueError, match="Web search"):
            manager.start(sid, "alice", "browse", web=True)
        with pytest.raises(ValueError):
            manager.start(sid, "alice", "")
        assert store.events(sid) == []
        store.update_session(sid, status="archived")
        with pytest.raises(ValueError, match="Archived"):
            manager.start(sid, "alice", "archived")
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("approved", [True, False])
def test_readonly_artifact_approval_is_owned_single_operation(service, monkeypatch, approved):
    config, store, manager, sid = service
    store.update_session(sid, permission_mode="read-only")
    provider = ScriptedProvider([
        Turn(tool_calls=[ToolCall("file1", "deliver_file", {"filename": "answer.txt", "content": "hello"})]),
        Turn(text="finished"),
    ])
    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: provider)

    async def scenario():
        manager.start(sid, "alice", "generate a file")
        await eventually(lambda: manager.pending_approval(sid) is not None)
        request = manager.pending_approval(sid)
        assert manager.meta(sid)["phase"] == "waiting_approval"
        assert not (config.artifact_dir(sid) / "answer.txt").exists()
        with pytest.raises(PendingNotFound):
            manager.decide_approval(request["approval_id"], "bob", True)
        manager.decide_approval(request["approval_id"], "alice", approved)
        with pytest.raises(PendingNotFound):
            manager.decide_approval(request["approval_id"], "alice", True)
        await eventually(lambda: not manager.busy(sid))
        assert (config.artifact_dir(sid) / "answer.txt").exists() is approved
        result = next(e["payload"] for e in store.events(sid) if e["type"] == "tool_result")
        assert result["is_error"] is (not approved)
        assert isinstance(result["result"], str)
        assert store.get_session(sid)["permission_mode"] == "read-only"
        assert manager.pending_approval(sid) is None
        await manager.shutdown()

    asyncio.run(scenario())


def test_approval_cancellation_persists_resolution_and_tool_result(service, monkeypatch):
    _, store, manager, sid = service
    store.update_session(sid, permission_mode="read-only")
    provider = ScriptedProvider([Turn(tool_calls=[ToolCall("file1", "deliver_file", {"filename": "a.txt", "content": "hello"})])])
    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: provider)

    async def scenario():
        manager.start(sid, "alice", "file")
        await eventually(lambda: manager.pending_approval(sid) is not None)
        aid = manager.pending_approval(sid)["approval_id"]
        manager.cancel(sid, "alice")
        await eventually(lambda: not manager.busy(sid))
        events = store.events(sid)
        assert any(e["type"] == "approval_decided" and e["payload"]["approval_id"] == aid and e["payload"]["status"] == "cancelled" for e in events)
        assert any(e["type"] == "tool_result" and e["payload"]["call_id"] == "file1" for e in events)
        assert events[-1]["payload"]["stop_reason"] == "cancelled"
        await manager.shutdown()

    asyncio.run(scenario())


def test_workspace_artifact_executes_without_approval_and_tools_enter_context(service, monkeypatch):
    config, store, manager, sid = service
    provider = ScriptedProvider([
        Turn(tool_calls=[ToolCall("file", "deliver_file", {"filename": "answer.txt", "content": "回答"})]),
        Turn(text="delivered", usage={"input_tokens": 12, "output_tokens": 3}),
    ])
    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: provider)

    async def scenario():
        manager.start(sid, "alice", "file")
        await eventually(lambda: not manager.busy(sid))
        assert (config.artifact_dir(sid) / "answer.txt").read_text(encoding="utf-8") == "回答"
        assert not any(e["type"] == "approval_request" for e in store.events(sid))
        assert provider.contexts[-1][-1]["role"] == "tool"
        assert provider.contexts[-1][-1]["tool_call_id"] == "file"
        assert store.events(sid)[-1]["payload"]["usage"]["input_tokens"] == 12
        await manager.shutdown()

    asyncio.run(scenario())


def test_questions_validate_answers_and_resume_model(service, monkeypatch):
    _, store, manager, sid = service
    provider = ScriptedProvider([
        Turn(tool_calls=[ToolCall("ask1", "ask_user_question", {"questions": [{"id": "color", "question": "Color?", "options": [{"label": "Blue"}, {"label": "Red"}]}]})]),
        Turn(text="thanks"),
    ])
    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: provider)

    async def scenario():
        manager.start(sid, "alice", "ask")
        await eventually(lambda: manager.meta(sid)["phase"] == "waiting_question")
        request = next(e["payload"] for e in store.events(sid) if e["type"] == "question_request")
        qid = request["question_id"]
        with pytest.raises(PendingNotFound):
            manager.answer_question(sid, qid, "bob", {"dismiss": True})
        with pytest.raises(ValueError, match="match"):
            manager.answer_question(sid, qid, "alice", {"answers": [{"id": "color", "selected": ["Green"]}]})
        assert manager.busy(sid)
        result = manager.answer_question(sid, qid, "alice", {"answers": [{"id": "color", "selected": ["Blue"]}]})
        assert result["status"] == "answered"
        await eventually(lambda: not manager.busy(sid))
        assert "Blue" in provider.contexts[-1][-1]["content"]
        with pytest.raises(PendingNotFound):
            manager.answer_question(sid, qid, "alice", {"dismiss": True})
        await manager.shutdown()

    asyncio.run(scenario())


def test_cancel_question_resolves_card(service, monkeypatch):
    _, store, manager, sid = service
    provider = ScriptedProvider([Turn(tool_calls=[ToolCall("ask1", "ask_user_question", {"questions": [{"id": "q", "question": "Why?"}]})])])
    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: provider)

    async def scenario():
        manager.start(sid, "alice", "ask")
        await eventually(lambda: manager.meta(sid)["phase"] == "waiting_question")
        manager.cancel(sid, "alice")
        await eventually(lambda: not manager.busy(sid))
        assert any(e["type"] == "question_answered" and e["payload"]["status"] == "cancelled" for e in store.events(sid))
        await manager.shutdown()

    asyncio.run(scenario())


def test_restart_recovery_is_idempotent_and_closes_model_tool_protocol(service):
    _, store, manager, sid = service
    store.append_event(sid, "user_message", {"text": "old unfinished"})
    store.append_event(sid, "tool_use", {"call": {"id": "c1", "name": "deliver_file", "arguments": {}}})
    store.append_event(sid, "approval_request", {"approval_id": "a_old"})
    store.append_event(sid, "question_request", {"question_id": "q_old", "questions": [{"id": "q", "question": "Why?"}]})
    manager.recover_session(sid)
    events = store.events(sid)
    assert events[-1]["type"] == "done" and events[-1]["payload"]["stop_reason"] == "cancelled"
    assert any(e["type"] == "approval_decided" and e["payload"]["approval_id"] == "a_old" for e in events)
    assert any(e["type"] == "question_answered" and e["payload"]["question_id"] == "q_old" for e in events)
    context = manager._messages(sid, {})
    assert context[-2]["role"] == "assistant" and context[-1]["role"] == "tool"
    assert context[-1]["tool_call_id"] == "c1"
    manager.recover_session(sid)
    assert store.events(sid) == events


def test_loop_bound_and_unknown_tools_finish_with_error(service, monkeypatch):
    config, store, manager, sid = service
    config.max_rounds = 2
    provider = ScriptedProvider([Turn(tool_calls=[ToolCall("x1", "bash", {"command": "no"})]), Turn(tool_calls=[ToolCall("x2", "bash", {})])])
    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: provider)

    async def scenario():
        manager.start(sid, "alice", "loop")
        await eventually(lambda: not manager.busy(sid))
        assert store.events(sid)[-1]["payload"]["stop_reason"] == "max_rounds"
        results = [e["payload"] for e in store.events(sid) if e["type"] == "tool_result"]
        assert len(results) == 2 and all(item["is_error"] for item in results)
        assert all("Unknown conversation tool" in item["result"] for item in results)
        await manager.shutdown()

    asyncio.run(scenario())


def test_uploaded_text_contents_are_supplied_and_external_paths_are_rejected(service, monkeypatch):
    config, store, manager, sid = service
    uploads = config.session_dir(sid) / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "paper.txt").write_text("actual uploaded contents", encoding="utf-8")
    provider = ScriptedProvider([Turn(text="read")])
    monkeypatch.setattr("chat_agent.runtime.create_provider", lambda _: provider)

    async def scenario():
        for reference in ["../../secret.txt", "C:/secret.txt", "uploads/../../secret.txt", "uploads\\paper.txt"]:
            with pytest.raises(ValueError):
                manager.start(sid, "alice", "read", [{"path": reference}])
        assert store.events(sid) == []
        manager.start(sid, "alice", "read", [{"name": "paper.txt", "path": "uploads/paper.txt", "size": 0}])
        await eventually(lambda: not manager.busy(sid))
        assert "actual uploaded contents" in provider.contexts[0][-1]["content"]
        assert store.events(sid)[0]["payload"]["attachments"][0]["size"] == 24
        await manager.shutdown()

    asyncio.run(scenario())


def test_unsupported_image_is_explicit_and_supported_image_has_pixels(service):
    config, _, manager, sid = service
    uploads = config.session_dir(sid) / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nexample")
    reference = {"name": "image.png", "path": "uploads/image.png"}
    text, image = manager._attachment_content(sid, reference, {"supports_images": False})
    assert "does not support image" in text and image is None
    _, image = manager._attachment_content(sid, reference, {"supports_images": True})
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_slow_subscriber_is_bounded_and_requests_reconnection(service):
    _, store, manager, sid = service
    queue = manager.subscribe(sid)
    for i in range(600):
        manager.emit_event(sid, "assistant_delta", {"text": str(i)}, persist=False)
    assert queue.qsize() == 1
    assert queue.get_nowait()["type"] == "stream_reset"
    assert store.events(sid) == []


@pytest.mark.parametrize("relative", ["../secret", "/secret", "C:/secret", "uploads/../secret", "uploads\\file", "file\x00", "", "./file"])
def test_path_validation_is_portable(tmp_path, relative):
    with pytest.raises(ToolError):
        scoped_path(tmp_path, relative)


@pytest.mark.parametrize("filename", ["../secret.txt", "C:/secret.txt", "a/b.txt", "NUL.txt", "CON", "answer.txt.", "a:b.txt", "bad?.txt"])
def test_artifact_filename_cannot_escape_or_use_windows_devices(service, filename):
    config, _, _, sid = service
    with pytest.raises(ToolError):
        ToolExecutor(config).execute(sid, "deliver_file", {"filename": filename, "content": "bad"})


def test_files_and_checklists_validate_content(service):
    config, _, _, sid = service
    uploads = config.session_dir(sid) / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "note.txt").write_text("notes", encoding="utf-8")
    (uploads / "binary.bin").write_bytes(b"\x00\x01")
    executor = ToolExecutor(config)
    assert executor.execute(sid, "read_file", {"path": "uploads/note.txt"}) == "notes"
    assert len(json.loads(executor.execute(sid, "list_files", {}))) == 2
    with pytest.raises(ToolError, match="Binary"):
        executor.execute(sid, "read_file", {"path": "uploads/binary.bin"})
    with pytest.raises(ToolError, match="At most one"):
        executor.execute(sid, "todo_write", {"todos": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]})
    with pytest.raises(ToolError):
        validate_questions([{"id": "same", "question": "one"}, {"id": "same", "question": "two"}])


def test_linked_upload_cannot_read_another_session(tmp_path, service):
    config, _, _, sid = service
    uploads = config.session_dir(sid) / "uploads"
    uploads.mkdir(parents=True)
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        (uploads / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is not available for this Windows user")
    with pytest.raises(ToolError):
        upload_path(config, sid, "uploads/linked.txt")


@pytest.mark.parametrize("area", ["uploads", "artifacts"])
def test_linked_session_root_cannot_read_or_write_outside_home(tmp_path, service, area):
    config, _, _, sid = service
    outside = tmp_path / "elsewhere"
    (outside / "uploads").mkdir(parents=True)
    (outside / "uploads" / "secret.txt").write_text("private", encoding="utf-8")
    target = config.session_dir(sid) if area == "uploads" else config.artifact_dir(sid)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation is not available for this Windows user")
    executor = ToolExecutor(config)
    with pytest.raises(ToolError):
        if area == "uploads":
            executor.execute(sid, "read_file", {"path": "uploads/secret.txt"})
        else:
            executor.execute(sid, "deliver_file", {"filename": "bad.txt", "content": "bad"})
    assert not (outside / "bad.txt").exists()
