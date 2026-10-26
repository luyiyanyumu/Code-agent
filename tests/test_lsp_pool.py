"""Language-service pool lifecycle tests; every process is an in-memory fake."""
from __future__ import annotations

import asyncio
import sys
import threading
from collections import Counter
from types import SimpleNamespace

import pytest

from code_agent.codeintel import lsp as lsp_module
from code_agent.codeintel.lsp import LspManager
from code_agent.extensions.jsonrpc_stdio import JsonRpcError


def pool(tmp_path, monkeypatch, *, limit=3):
    state = SimpleNamespace(
        clients=[], attempts=Counter(), initialize_gates={}, stop_gates={},
        started={}, stop_entered={}, failures=Counter(), stop_failures=Counter(), max_alive=0,
    )

    class Client:
        def __init__(self, *args, name, **kwargs):
            self.server = name.removeprefix("lsp:")
            self.running = False
            self.on_notification = None
            self.on_request = None
            self.closed = None
            state.clients.append(self)

        @property
        def alive(self):
            return self.running

        def set_on_closed(self, callback):
            self.closed = callback

        def stderr_tail(self, *, limit):
            return []

        async def start(self):
            state.attempts[self.server] += 1
            self.running = True
            state.max_alive = max(state.max_alive, sum(client.alive for client in state.clients))
            state.started.setdefault(self.server, asyncio.Event()).set()

        async def request(self, method, params, **kwargs):
            assert method == "initialize"
            gate = state.initialize_gates.get(self.server)
            if gate is not None:
                await gate.wait()
            if state.failures[self.server]:
                state.failures[self.server] -= 1
                raise JsonRpcError("fixture initialize failure")
            return {"capabilities": {"hoverProvider": True, "completionProvider": {}}}

        async def notify(self, method, params):
            pass

        async def stop(self):
            state.stop_entered.setdefault(self.server, asyncio.Event()).set()
            gate = state.stop_gates.get(self.server)
            if gate is not None:
                await gate.wait()
            if state.stop_failures[self.server]:
                state.stop_failures[self.server] -= 1
                raise JsonRpcError("fixture process would not exit")
            self.running = False

    monkeypatch.setattr(lsp_module, "JsonRpcStdio", Client)
    monkeypatch.setattr(lsp_module, "MAX_SESSIONS", limit)
    manager = LspManager()
    monkeypatch.setattr(manager, "servers_for_language", lambda lang, project: [{
        "id": lang, "name": lang, "command": sys.executable, "args": [],
        "install_hint": "", "languages": [lang], "enabled": True,
    }])
    project = {"id": "fixture", "root_path": str(tmp_path), "name": "fixture"}
    return manager, project, state


async def entered(events, key):
    # Timeout protects a hung regression; correctness is decided by gates/order,
    # not a wall-clock performance threshold.
    event = events.setdefault(key, asyncio.Event())
    await asyncio.wait_for(event.wait(), timeout=3)


def test_slow_initialize_does_not_block_existing_or_independent_service(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch)
        existing = await manager.session_for(project, "fixture-a")
        gate = state.initialize_gates["fixture-b"] = asyncio.Event()
        slow = asyncio.create_task(manager.session_for(project, "fixture-b"))
        try:
            await entered(state.started, "fixture-b")
            assert await asyncio.wait_for(manager.session_for(project, "fixture-a"), 3) is existing
            independent = await asyncio.wait_for(manager.session_for(project, "fixture-c"), 3)
            assert independent.alive and not gate.is_set() and not slow.done()
            assert state.max_alive == 3
            gate.set()
            assert (await slow).alive
        finally:
            gate.set()
            await manager.stop_all()
    asyncio.run(flow())


def test_same_key_shares_startup_and_one_waiter_cancellation_isolated(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch)
        gate = state.initialize_gates["fixture-a"] = asyncio.Event()
        first = asyncio.create_task(manager.session_for(project, "fixture-a"))
        try:
            await entered(state.started, "fixture-a")
            second = asyncio.create_task(manager.session_for(project, "fixture-a"))
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert state.clients[0].alive
            gate.set()
            session = await second
            assert state.attempts["fixture-a"] == 1
            assert await manager.session_for(project, "fixture-a") is session
            summary = session.summary()
            assert summary["capabilities"] == {"hoverProvider": True, "completionProvider": {}}
            assert summary["initialization_ms"] >= 0
            assert manager.status()["starting"] == []
        finally:
            gate.set()
            await manager.stop_all()
    asyncio.run(flow())


def test_failed_initialize_releases_capacity_and_retries(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch, limit=1)
        state.failures["fixture-a"] = 1
        try:
            with pytest.raises(JsonRpcError, match="fixture initialize failure"):
                await manager.session_for(project, "fixture-a")
            assert not manager._starting and not manager._sessions
            assert not any(client.alive for client in state.clients)
            assert (await manager.session_for(project, "fixture-a")).alive
            assert state.attempts["fixture-a"] == 2 and state.max_alive == 1
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_stop_cancels_initialization_and_can_retry_without_leaking_process(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch, limit=1)
        gate = state.initialize_gates["fixture-a"] = asyncio.Event()
        pending = asyncio.create_task(manager.session_for(project, "fixture-a"))
        await entered(state.started, "fixture-a")
        key = "fixture:main:fixture-a:fixture-a"
        await asyncio.gather(manager.stop(key), manager.stop(key))
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not manager._starting and not any(client.alive for client in state.clients)
        gate.set()
        try:
            assert (await manager.session_for(project, "fixture-a")).alive
            assert state.max_alive == 1
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_stop_releases_startup_cancelled_before_first_execution(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch, limit=1)
        entry = manager.servers_for_language("fixture-a", project)[0]
        key = "fixture:main:fixture-a:fixture-a"
        task = asyncio.create_task(manager._start_reserved(project, "fixture-a", "main", key, entry, 0))
        manager._starting[key] = task
        manager._starting_info[key] = {"key": key}
        task.add_done_callback(lambda done: manager._start_finished(key, done))
        await manager.stop(key)
        assert task.cancelled() and not manager._starting and not manager.status()["starting"]
        assert not state.clients
        try:
            assert (await manager.session_for(project, "fixture-a")).alive
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_capacity_protects_subscribers_and_waits_for_evicted_process_exit(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch, limit=2)
        a = await manager.session_for(project, "fixture-a")
        b = await manager.session_for(project, "fixture-b")
        off_a = manager.subscribe(a, lambda message: None)
        off_b = manager.subscribe(b, lambda message: None)
        stop_gate = state.stop_gates["fixture-b"] = asyncio.Event()
        try:
            with pytest.raises(JsonRpcError, match="并发上限 2 已满"):
                await manager.session_for(project, "fixture-c")
            assert a.alive and b.alive and state.attempts["fixture-c"] == 0
            off_b()
            pending = asyncio.create_task(manager.session_for(project, "fixture-c"))
            await entered(state.stop_entered, "fixture-b")
            assert state.attempts["fixture-c"] == 0 and b.alive
            assert await asyncio.wait_for(manager.session_for(project, "fixture-a"), 3) is a
            stop_gate.set()
            assert (await pending).alive
            assert not b.alive and state.max_alive == 2
        finally:
            off_a()
            off_b()
            stop_gate.set()
            await manager.stop_all()
    asyncio.run(flow())


def test_starting_process_reserves_capacity_and_shutdown_cleans_all(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch, limit=1)
        state.initialize_gates["fixture-a"] = asyncio.Event()
        pending = asyncio.create_task(manager.session_for(project, "fixture-a"))
        await entered(state.started, "fixture-a")
        assert manager.status()["starting"] == [{
            "key": "fixture:main:fixture-a:fixture-a", "server": "fixture-a",
            "lang": "fixture-a", "state": "starting",
        }]
        with pytest.raises(JsonRpcError, match="并发上限 1 已满"):
            await manager.session_for(project, "fixture-b")
        await asyncio.gather(manager.stop_all(), manager.stop_all())
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not manager._starting and not manager._sessions and not manager._stopping
        assert not any(client.alive for client in state.clients)
        assert state.attempts["fixture-b"] == 0
    asyncio.run(flow())


def test_failed_process_exit_keeps_capacity_reserved(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch, limit=1)
        session = await manager.session_for(project, "fixture-a")
        state.stop_failures["fixture-a"] = 1
        try:
            with pytest.raises(JsonRpcError, match="无法释放并发容量"):
                await manager.session_for(project, "fixture-b")
            assert session.alive and manager._sessions[session.key] is session
            assert state.attempts["fixture-b"] == 0 and state.max_alive == 1
            assert (await manager.session_for(project, "fixture-b")).alive
            assert not session.alive and state.max_alive == 1
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_failed_initialize_cleanup_keeps_live_process_reserved(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch, limit=1)
        state.failures["fixture-a"] = 1
        state.stop_failures["fixture-a"] = 1
        try:
            with pytest.raises(JsonRpcError, match="fixture initialize failure"):
                await manager.session_for(project, "fixture-a")
            assert len(manager._sessions) == 1 and state.clients[0].alive
            assert (await manager.session_for(project, "fixture-b")).alive
            assert not state.clients[0].alive and state.max_alive == 1
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_shutdown_rejects_old_request_still_resolving_command(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch)
        real = manager.servers_for_language
        resolving, release = threading.Event(), threading.Event()
        def delayed(lang, project):
            resolving.set()
            assert release.wait(timeout=3)
            return real(lang, project)
        monkeypatch.setattr(manager, "servers_for_language", delayed)
        pending = asyncio.create_task(manager.session_for(project, "fixture-a"))
        try:
            assert await asyncio.to_thread(resolving.wait, 3)
            await manager.stop_all()
            release.set()
            with pytest.raises(JsonRpcError, match="会话已关闭"):
                await pending
            assert not state.clients
            monkeypatch.setattr(manager, "servers_for_language", real)
            assert (await manager.session_for(project, "fixture-a")).alive
        finally:
            release.set()
            await manager.stop_all()
    asyncio.run(flow())


def test_idle_reaping_waits_until_subscriber_disconnects(tmp_path, monkeypatch):
    async def flow():
        manager, project, state = pool(tmp_path, monkeypatch)
        session = await manager.session_for(project, "fixture-a")
        off = manager.subscribe(session, lambda message: None)
        try:
            assert await manager.reap_idle(idle=-1) == []
            assert session.alive
            off()
            assert await manager.reap_idle(idle=-1) == [session.key]
            assert not session.alive and not manager._stopping
        finally:
            off()
            await manager.stop_all()
    asyncio.run(flow())
