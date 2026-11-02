"""ESLint's pull-only diagnostics remain current through our push editor bridge."""
from __future__ import annotations

import asyncio
from pathlib import Path

from code_agent.codeintel.lsp import LspManager, LspSession
from code_agent.extensions.jsonrpc_stdio import JsonRpcError


DIAGNOSTIC = {"message": "unused", "severity": 1, "range": {
    "start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}


class Client:
    alive = True

    def __init__(self):
        self.calls = []
        self.replies = asyncio.Queue()

    async def notify(self, method, params):
        pass

    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        response = await self.replies.get()
        if isinstance(response, Exception):
            raise response
        return response

    async def stop(self):
        self.alive = False


def fixture(tmp_path):
    manager = LspManager(documentation_path=tmp_path / "docs.db")
    manager.documentation_prewarm.schedule = lambda *a, **kw: None
    manager.documentation_prewarm.cancel = lambda *a, **kw: None
    client = Client()
    session = LspSession(key="eslint", server_id="eslint", lang="javascript", project_id="p", tree_id="main",
                         root=tmp_path, command="node", args=[], client=client,
                         capabilities={"diagnosticProvider": {}})
    uri = (tmp_path / "demo.js").as_uri()
    messages = []
    manager.subscribe(session, messages.append)
    return manager, session, client, uri, messages


async def event(manager, session, uri, method="didOpen", version=1):
    document = {"uri": uri, "version": version, "languageId": "javascript", "text": "var unused;"}
    params = {"textDocument": document}
    if method == "didChange":
        params["contentChanges"] = [{"text": "var changed;"}]
    await manager.handle_client_notification(session, {"method": "textDocument/" + method, "params": params})


async def until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(.01)
    raise AssertionError("Expected asynchronous diagnostic event")


def test_eslint_empty_section_is_flattened_without_changing_other_servers(tmp_path):
    manager, session, _, _, _ = fixture(tmp_path)
    session.settings = {"eslint": {"quiet": True, "rulesCustomizations": [{"rule": "*", "severity": "warn"}]}, "other": 42}
    request = {"method": "workspace/configuration", "params": {"items": [{}, {"section": "eslint"}, {"section": "other"}]}}
    flat, configured, other = manager._on_server_request(session, request)
    assert flat["quiet"] is True and flat["nodePath"] is None
    assert flat["rulesCustomizations"] == session.settings["eslint"]["rulesCustomizations"]
    assert flat["workingDirectory"]["directory"] == str(tmp_path)
    assert flat["problems"] == {"shortenToSingleLine": False}
    assert "eslint" not in flat and configured == session.settings["eslint"] and other == 42
    session.server_id = "pyright"
    assert manager._on_server_request(session, request)[0] == {}


def test_open_pulls_asynchronously_and_publishes_diagnostics(tmp_path):
    async def flow():
        manager, session, client, uri, messages = fixture(tmp_path)
        await event(manager, session, uri)
        assert not client.calls  # didOpen did not wait for a server diagnostic response.
        await client.replies.put({"kind": "full", "items": [DIAGNOSTIC]})
        await until(lambda: bool(messages))
        assert session.diagnostics[uri][0]["message"] == "unused"
        assert session.diagnostic_versions[uri] == 1
        assert messages[-1]["method"] == "textDocument/publishDiagnostics"
        await until(lambda: not session.diagnostic_pull_tasks)
    asyncio.run(flow())


def test_stale_inflight_result_is_discarded_and_edits_coalesce(tmp_path):
    async def flow():
        manager, session, client, uri, messages = fixture(tmp_path)
        await event(manager, session, uri)
        await until(lambda: len(client.calls) == 1)
        for version in range(2, 8):
            await event(manager, session, uri, "didChange", version)
        await client.replies.put({"kind": "full", "items": [DIAGNOSTIC]})
        await until(lambda: len(client.calls) == 2)
        assert not messages and uri not in session.diagnostics
        await client.replies.put({"kind": "full", "items": []})
        await until(lambda: bool(messages))
        assert session.diagnostics[uri] == [] and session.diagnostic_versions[uri] == 7
        await until(lambda: not session.diagnostic_pull_tasks)
        assert len(client.calls) == 2
    asyncio.run(flow())


def test_rapid_changes_before_request_merge_to_one_pull(tmp_path):
    async def flow():
        manager, session, client, uri, messages = fixture(tmp_path)
        await event(manager, session, uri)
        for version in range(2, 12):
            await event(manager, session, uri, "didChange", version)
        await client.replies.put({"kind": "full", "items": [DIAGNOSTIC]})
        await until(lambda: bool(messages))
        await until(lambda: not session.diagnostic_pull_tasks)
        assert len(client.calls) == 1 and session.diagnostic_versions[uri] == 11
    asyncio.run(flow())


def test_close_cancels_pending_pull_and_prevents_late_publish(tmp_path):
    async def flow():
        manager, session, client, uri, messages = fixture(tmp_path)
        await event(manager, session, uri)
        await until(lambda: bool(client.calls))
        pending = session.diagnostic_pull_tasks[uri]
        await event(manager, session, uri, "didClose")
        await until(pending.done)
        assert pending.cancelled() and not session.diagnostic_pull_tasks
        assert uri not in session.documents and not messages
        manager._on_notification(session, {"method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "version": 1, "diagnostics": [DIAGNOSTIC]}})
        assert not messages and uri not in session.diagnostics
    asyncio.run(flow())


def test_method_not_found_disables_pull_without_retrying(tmp_path):
    async def flow():
        manager, session, client, uri, _ = fixture(tmp_path)
        await client.replies.put(JsonRpcError("server error -32601 Method not found"))
        await event(manager, session, uri)
        await until(lambda: session.diagnostic_pull_disabled)
        await event(manager, session, uri, "didChange", 2)
        manager._on_server_request(session, {"method": "workspace/diagnostic/refresh"})
        await asyncio.sleep(.12)
        assert len(client.calls) == 1 and not session.diagnostic_pull_tasks
    asyncio.run(flow())


def test_refresh_schedules_open_documents_and_retire_cancels(tmp_path):
    async def flow():
        manager, session, client, uri, _ = fixture(tmp_path)
        await event(manager, session, uri)
        await client.replies.put({"kind": "full", "items": []})
        await until(lambda: not session.diagnostic_pull_tasks)
        manager._on_server_request(session, {"method": "workspace/diagnostic/refresh"})
        await until(lambda: len(client.calls) == 2)
        pending = session.diagnostic_pull_tasks[uri]
        await manager._retire(session)
        assert pending.cancelled() and not session.diagnostic_pull_tasks and not client.alive
    asyncio.run(flow())


def test_non_eslint_push_servers_do_not_start_pulls(tmp_path):
    async def flow():
        manager, session, client, uri, _ = fixture(tmp_path)
        session.server_id = "json"
        await event(manager, session, uri)
        assert not session.diagnostic_pull_tasks and not client.calls
    asyncio.run(flow())
