"""Project context must not occupy the event loop or masquerade as model work."""

import asyncio
import threading

import pytest

from code_agent.agent.router import ModelRouter
from code_agent.models.events import EventType
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.registry import ToolRegistry


@pytest.fixture
def runtime(agent_home):
    db = Database(agent_home / "context-progress.db")
    user = UserRepo(db).create("context-progress", "test-only")
    router = ModelRouter(provider_name="offline")
    router.provider_override = OfflineProvider(script=[OfflineProvider.text_turn("finished")])
    manager = SessionManager(
        transcript=TranscriptStore(agent_home / "transcripts"), session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db), router=router, registry=ToolRegistry(),
    )
    meta = manager.create(user["id"])
    yield manager.get_runtime(meta["id"], user["id"])
    db.close()


def test_context_preparation_runs_off_loop_and_precedes_model(runtime):
    provider = runtime.router.resolve_default()
    threads = []
    queue = runtime.subscribe()

    def context():
        threads.append(threading.get_ident())
        assert provider.calls == 0
        assert runtime.phase == "preparing_context"
        return 1, "project map"

    runtime.code_context_provider = context
    runtime._code_context_provider_fn = lambda _context: context()[1]

    async def run():
        loop_thread = threading.get_ident()
        result = await runtime.submit("hello", code_context={"path": "a.py"})
        assert result.text == "finished"
        assert threads and all(thread != loop_thread for thread in threads)
        assert not runtime.busy and runtime.phase == ""

    asyncio.run(run())
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    phases = [event.payload for event in events
              if event.type == EventType.session_meta and "phase" in event.payload]
    assert next(i for i, phase in enumerate(phases) if phase["phase"] == "preparing_context") < next(
        i for i, phase in enumerate(phases) if phase["phase"] == "model"
    )
    assert phases[-1] == {"phase": "", "busy": False}
    assert all(event.type != EventType.session_meta or "phase" not in event.payload
               for event in runtime.transcript.replay(runtime.session_id))


def test_cancel_during_context_preparation_never_calls_provider(runtime):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    provider = runtime.router.resolve_default()

    def context():
        entered.set()
        try:
            assert release.wait(5), "test failed to release context worker"
            return 1, "map completed after cancellation"
        finally:
            finished.set()

    runtime.code_context_provider = context

    async def run():
        task = asyncio.create_task(runtime.submit("hello"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert runtime.busy and runtime.phase == "preparing_context"
            assert provider.calls == 0
            runtime.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert runtime.phase == "" and not runtime.busy
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.sleep(0)
        assert provider.calls == 0 and runtime.phase == ""

    asyncio.run(run())


def test_provider_error_clears_live_phase(runtime, monkeypatch):
    provider = runtime.router.resolve_default()

    async def fail(*_args):
        assert runtime.phase == "model"
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(provider, "chat", fail)

    async def run():
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await runtime.submit("hello")
        assert runtime.phase == "" and not runtime.busy

    asyncio.run(run())


def test_same_version_context_text_updates(runtime):
    runtime.code_context_provider = lambda: (4, "map before")
    assert runtime.refresh_code_context() == "map before"
    runtime.code_context_provider = lambda: (4, "map after line move")
    assert runtime.refresh_code_context() == "map after line move"


@pytest.mark.parametrize("tree_id,isolated", [("", False), ("task:isolated", True)])
def test_context_wiring_uses_current_metadata_and_the_session_tree(runtime, tree_id, isolated):
    from types import SimpleNamespace

    row = {"id": "p_test", "user_id": runtime.user_id, "name": "before", "root_path": "original-root"}
    calls = []

    class ContextService:
        def session_context(self, project, *, tree_id):
            calls.append(("session", project, tree_id))
            return 7, project["name"]

        def message_context(self, project, selection, *, tree_id):
            calls.append(("message", project, tree_id))
            return project["name"] + selection["path"]

    manager = SimpleNamespace(project_repo=SimpleNamespace(get=lambda _pid: row), code_intel=ContextService())
    meta = {"project_id": "p_test", "tree_id": tree_id,
            "workspace_override": str(runtime.workspace) if isolated else ""}
    SessionManager._wire_code_context(manager, runtime, meta)
    assert runtime.refresh_code_context() == "before"
    row.update(name="after", root_path="renamed-root")
    assert runtime.refresh_code_context() == "after"
    assert runtime._code_context_provider_fn({"path": "/a.py"}) == "after/a.py"
    for _kind, project, selected_tree in calls[-2:]:
        assert selected_tree == (tree_id or "main")
        assert project["name"] == "after"
        assert project["root_path"] == (str(runtime.workspace) if isolated else "renamed-root")
