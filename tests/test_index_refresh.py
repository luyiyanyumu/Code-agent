"""Coalesced indexing uses temporary projects and controlled fixture workers."""
import asyncio
import threading
from pathlib import Path

import pytest

from code_agent.codeintel.hashing import sha1_file_bytes, sha1_text
from code_agent.codeintel.service import CodeIntelService


class _Projects:
    def set_scan(self, *args): pass


@pytest.fixture
def project(tmp_path, agent_home):
    root = tmp_path / "fixture-index-refresh"
    root.mkdir()
    (root / "a.py").write_text("def before():\n    return 1\n", encoding="utf-8")
    return {"id": "fixture", "name": "fixture", "root_path": str(root), "root_key": str(root), "user_id": "owner"}


def test_saves_during_scan_coalesce_to_one_final_pass(project, monkeypatch):
    async def scenario():
        service = CodeIntelService(_Projects())
        scanned, release = threading.Event(), threading.Event()
        original = service._scan_sync
        calls = []
        active, peak = 0, 0

        def blocked(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                result = original(*args)
                calls.append(result)
                if len(calls) == 1:
                    scanned.set()
                    assert release.wait(5)
                return result
            finally:
                active -= 1

        monkeypatch.setattr(service, "_scan_sync", blocked)
        first = asyncio.create_task(service.scan(project))
        try:
            assert await asyncio.to_thread(scanned.wait, 3)
            saved = await service.save_file(project, "a.py", "def middle():\n    return 2\n", sha1_text("def before():\n    return 1\n"))
            latest = await service.save_file(project, "a.py", "def latest():\n    return 3\n", saved["sha1"])
            assert latest["index_refresh"]["requested_generation"] == 3
            assert latest["index_refresh"]["completed_generation"] == 0
            assert latest["index_refresh"]["pending"]
            assert len(calls) == 1 and peak == 1
            release.set()
            result = await asyncio.wait_for(first, 3)
            status = service.refresh_status(project["id"])
            assert result["status"] == "done" and not result["scanning"]
            assert status["state"] == "done" and not status["pending"]
            assert status["completed_generation"] == status["requested_generation"] == 3
            assert len(calls) == 2 and peak == 1
            index = service.index(project["id"])
            assert index.file_row("a.py")["sha1"] == sha1_file_bytes(Path(project["root_path"]) / "a.py")
            assert [item["name"] for item in index.symbols_for("a.py")] == ["latest"]
            assert service.status(project)["index_refresh"] == status
            assert service.map_payload(project)["index_refresh"] == status
        finally:
            release.set()
            await service.aclose()
    asyncio.run(scenario())


def test_cancelled_scan_caller_does_not_release_single_flight(project, monkeypatch):
    async def scenario():
        service = CodeIntelService(_Projects())
        started, release = threading.Event(), threading.Event()
        original = service._scan_sync
        calls = []

        def blocked(*args):
            calls.append(1)
            started.set()
            assert release.wait(5)
            return original(*args)

        monkeypatch.setattr(service, "_scan_sync", blocked)
        first = asyncio.create_task(service.scan(project))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert service.refresh_status(project["id"])["pending"]
            assert (await service.scan(project))["status"] == "busy"
            assert len(calls) == 1
            closing = asyncio.create_task(service.aclose())
            await asyncio.sleep(0.05)
            assert not closing.done()
            with pytest.raises(RuntimeError, match="关闭"):
                service.request_refresh(project)
            release.set()
            await asyncio.wait_for(closing, 3)
            assert not service._indexes and not service._scan_futures and not service._scanning
        finally:
            release.set()
            await service.aclose()
    asyncio.run(scenario())


def test_close_tree_waits_for_worker_before_closing_connection(project, monkeypatch):
    async def scenario():
        service = CodeIntelService(_Projects())
        started, release = threading.Event(), threading.Event()
        original = service._scan_sync

        def blocked(*args):
            service.index(project["id"], "fixture-tree").set_meta("before", "still-open")
            started.set()
            assert release.wait(5)
            assert service.index(project["id"], "fixture-tree").get_meta("before") == "still-open"
            return original(*args)

        monkeypatch.setattr(service, "_scan_sync", blocked)
        scan = asyncio.create_task(service.scan(project, tree_id="fixture-tree"))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            closing = asyncio.create_task(asyncio.to_thread(service.close_tree, project["id"], "fixture-tree"))
            await asyncio.sleep(0.05)
            assert not closing.done()
            with pytest.raises(RuntimeError, match="关闭"):
                service.request_refresh(project, tree_id="fixture-tree")
            release.set()
            await asyncio.wait_for(scan, 3)
            await asyncio.wait_for(closing, 3)
            assert f"{project['id']}:fixture-tree" not in service._indexes
            assert not service._scan_futures
        finally:
            release.set()
            await service.aclose()
    asyncio.run(scenario())


def test_failed_refresh_is_visible_and_manual_retry_recovers(project, monkeypatch):
    async def scenario():
        service = CodeIntelService(_Projects())
        original = service._scan_sync
        monkeypatch.setattr(service, "_scan_sync", lambda *args: (_ for _ in ()).throw(OSError("fixture index failure")))
        try:
            with pytest.raises(OSError, match="fixture index failure"):
                await service.scan(project)
            failed = service.refresh_status(project["id"])
            assert failed["state"] == "error" and not failed["pending"]
            assert failed["completed_generation"] == 0 and failed["requested_generation"] == 1
            monkeypatch.setattr(service, "_scan_sync", original)
            assert (await service.scan(project))["status"] == "done"
            recovered = service.refresh_status(project["id"])
            assert recovered["state"] == "done" and not recovered["error"]
            assert recovered["completed_generation"] == recovered["requested_generation"] == 2
        finally:
            await service.aclose()
    asyncio.run(scenario())


def test_cancelled_save_caller_still_refreshes_committed_write(project, monkeypatch):
    async def scenario():
        service = CodeIntelService(_Projects())
        prepared, release = threading.Event(), threading.Event()
        original = service._save_file_sync

        def blocked(*args):
            prepared.set()
            assert release.wait(5)
            return original(*args)

        monkeypatch.setattr(service, "_save_file_sync", blocked)
        save = asyncio.create_task(service.save_file(project, "a.py", "def committed():\n    return 2\n"))
        try:
            assert await asyncio.to_thread(prepared.wait, 3)
            save.cancel()
            with pytest.raises(asyncio.CancelledError):
                await save
            release.set()
            # The shielded write owns refresh scheduling even without a caller.
            for _ in range(100):
                if service.refresh_status(project["id"])["state"] == "done":
                    break
                await asyncio.sleep(0.01)
            status = service.refresh_status(project["id"])
            assert status["state"] == "done" and status["completed_generation"] == 1
            assert [item["name"] for item in service.index(project["id"]).symbols_for("a.py")] == ["committed"]
        finally:
            release.set()
            await service.aclose()
    asyncio.run(scenario())


def test_parse_error_remains_visible_when_unchanged_file_is_reused(project):
    async def scenario():
        service = CodeIntelService(_Projects())
        target = Path(project["root_path"]) / "a.py"
        target.write_text("def invalid(:\n", encoding="utf-8")
        try:
            first = await service.scan(project)
            assert first["scan"]["errors"] and first["index_refresh"]["state"] == "error"
            reused = await service.scan(project)
            assert reused["scan"]["parsed"] == 0 and reused["scan"]["errors"]
            assert reused["index_refresh"]["state"] == "error"
            saved = await service.save_file(project, "a.py", "def valid():\n    return 1\n")
            assert saved["ok"]
            for _ in range(100):
                if not service.refresh_status(project["id"])["pending"]:
                    break
                await asyncio.sleep(0.01)
            status = service.refresh_status(project["id"])
            assert status["state"] == "done" and not status["error"]
            assert status["completed_generation"] == status["requested_generation"] == 3
        finally:
            await service.aclose()
    asyncio.run(scenario())


def test_read_hash_belongs_to_returned_text_when_external_writer_races(project, monkeypatch):
    from code_agent.codeintel.projects import ProjectService

    target = Path(project["root_path"]) / "a.py"
    original = target.read_text(encoding="utf-8")
    read_text = Path.read_text

    def read_then_external_write(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        if path == target:
            target.write_bytes(b"def external():\n    return 2\n")
        return text

    monkeypatch.setattr(Path, "read_text", read_then_external_write)
    opened = ProjectService(_Projects()).read_file(project, "a.py")
    assert opened["text"] == original
    assert opened["sha1"] == sha1_text(original)
    assert opened["sha1"] != sha1_text("def external():\n    return 2\n")


def test_task_cleanup_waits_off_the_event_loop(project, monkeypatch):
    from code_agent.codeintel.tasks import TaskService

    async def scenario():
        started, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        worker_threads = []

        class ClosingIntel:
            def close_tree(self, project_id, tree_id):
                worker_threads.append(threading.get_ident())
                started.set()
                assert release.wait(5)

        tasks = TaskService(projects=_Projects(), tasks=None, intel=ClosingIntel())

        async def no_git(*args, **kwargs):
            return 0, ""

        monkeypatch.setattr(tasks, "_git_internal", no_git)
        cleanup = asyncio.create_task(tasks._cleanup(project, {"id": "fixture-task", "project_id": project["id"], "user_id": project["user_id"]}))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await asyncio.sleep(0)
            assert not cleanup.done()
            assert worker_threads == [worker_threads[0]] and worker_threads[0] != loop_thread
        finally:
            release.set()
            await asyncio.wait_for(cleanup, 3)
    asyncio.run(scenario())
