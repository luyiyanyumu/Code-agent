"""Documentation localization must retain the LSP revision/cancel boundary."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from code_agent.codeintel.lsp import LspManager, LspSession
from code_agent.codeintel.uri import file_uri


HOVER = {"contents": [
    {"language": "java", "value": "<String> Set<String> java.util.Set.of(String e1, String e2)"},
    {"kind": "markdown", "value": "Returns an unmodifiable set containing two elements."},
], "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 2}}}


class Client:
    alive = True

    def __init__(self):
        self.notifications = []
        self.requests = []

    async def request(self, method, params, *, timeout, on_request_id):
        self.requests.append((method, deepcopy(params)))
        if on_request_id:
            on_request_id(73)
        return deepcopy(HOVER)

    async def notify(self, method, params):
        self.notifications.append((method, params))


class Translation:
    def __init__(self, *, wait=False):
        self.wait = wait
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.scopes = []
        self.closed = False

    async def translate_hover(self, result, *, scope):
        self.scopes.append(scope)
        self.started.set()
        if self.wait:
            await self.release.wait()
        result = deepcopy(result)
        result["contents"][1]["value"] = "返回一个包含两个元素的不可修改集合。"
        result["documentation_translation"] = {"language": "zh-CN", "source": "configured-model"}
        return result

    async def close(self):
        self.closed = True


def context(tmp_path, *, wait=False):
    client = Client()
    session = LspSession(key="fixture", server_id="fixture", lang="java", project_id="p", tree_id="main",
                         root=tmp_path, command="fixture", args=[], client=client,
                         capabilities={"hoverProvider": True, "completionProvider": True})
    uri = file_uri(tmp_path / "Example.java")
    session.documents[uri] = 1
    manager = LspManager()
    translation = Translation(wait=wait)
    manager.documentation_translator = translation
    message = {"id": "hover-1", "method": "textDocument/hover",
               "params": {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 0}}}
    return manager, session, translation, message, uri


def test_hover_translation_is_wired_and_preserves_signature_range_and_scope(tmp_path):
    async def flow():
        manager, session, translation, message, _ = context(tmp_path)
        response = await manager.handle_client_request(session, message)
        assert response["result"]["contents"][0] == HOVER["contents"][0]
        assert response["result"]["range"] == HOVER["range"]
        assert "不可修改集合" in response["result"]["contents"][1]["value"]
        assert translation.scopes == ["p:main:fixture"]
        await manager.stop_all()
        assert translation.closed
    asyncio.run(flow())


@pytest.mark.parametrize("close_document", [False, True])
def test_translation_cannot_revive_a_changed_or_closed_document(tmp_path, close_document):
    async def flow():
        manager, session, translation, message, uri = context(tmp_path, wait=True)
        task = asyncio.create_task(manager.handle_client_request(session, message))
        await translation.started.wait()
        if close_document:
            session.documents.pop(uri)
        else:
            session.documents[uri] = 2
        translation.release.set()
        response = await task
        assert response["error"]["code"] == -32801
        assert "result" not in response
        await manager.stop_all()
    asyncio.run(flow())


def test_cancellation_during_translation_still_cancels_the_lsp_request(tmp_path):
    async def flow():
        manager, session, translation, message, _ = context(tmp_path, wait=True)
        task = asyncio.create_task(manager.handle_client_request(session, message))
        await translation.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.client.notifications == [("$/cancelRequest", {"id": 73})]
        await manager.stop_all()
    asyncio.run(flow())


def test_other_lsp_requests_do_not_trigger_document_translation(tmp_path):
    async def flow():
        manager, session, translation, message, _ = context(tmp_path)
        response = await manager.handle_client_request(session, {**message, "method": "textDocument/completion"})
        assert response["result"] == HOVER
        assert translation.scopes == []
        await manager.stop_all()
    asyncio.run(flow())


def test_translated_resolve_payload_is_restored_before_forwarding(tmp_path):
    async def flow():
        manager, session, _, _, _ = context(tmp_path)
        translated = {"label": "value", "documentation": "返回当前值。",
                      "_code_agent_original": {"documentation": "Returns the current value."},
                      "data": {"server": "opaque"}}
        await manager.handle_client_request(session, {"id": 8, "method": "completionItem/resolve", "params": translated})
        assert session.client.requests[-1] == ("completionItem/resolve", {
            "label": "value", "documentation": "Returns the current value.", "data": {"server": "opaque"}})
        assert translated["documentation"] == "返回当前值。"
        await manager.stop_all()
    asyncio.run(flow())


def test_empty_diagnostics_cannot_be_revived_by_a_late_translation(tmp_path):
    async def flow():
        manager, session, _, _, uri = context(tmp_path)

        class Slow:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def translate_contents(self, values, **kwargs):
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()  # emulate a non-cooperative adapter
                return ["该方法已弃用。" for value in values]

            async def close(self):
                pass

        slow = Slow()
        manager.documentation_translator = slow
        frames = []
        manager.subscribe(session, frames.append)
        span = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 2}}
        manager._on_notification(session, {"method": "textDocument/publishDiagnostics", "params": {
            "uri": uri, "version": 1, "diagnostics": [{"range": span, "message": "The method is deprecated.", "severity": 2}]}})
        await slow.started.wait()
        prior = list(session.localization_tasks.values())
        manager._on_notification(session, {"method": "textDocument/publishDiagnostics", "params": {
            "uri": uri, "version": 1, "diagnostics": []}})
        assert session.diagnostics[uri] == []
        slow.release.set()
        await asyncio.gather(*prior, return_exceptions=True)
        assert session.diagnostics[uri] == []
        assert len(frames) == 1 and frames[0]["params"]["diagnostics"] == []
        await manager.stop_all()
    asyncio.run(flow())


def test_stopping_one_server_also_stops_project_harvesting(tmp_path):
    async def flow():
        manager = LspManager()
        started = asyncio.Event()

        async def harvest():
            started.set()
            await asyncio.Event().wait()

        job = asyncio.create_task(harvest())
        manager._documentation_jobs[("p", "main")] = job
        manager._documentation_progress[("p", "main")] = {"status": "running"}
        await started.wait()
        await manager.stop("p:main:java:jdtls")
        assert job.cancelled()
        assert manager._documentation_progress[("p", "main")]["status"] == "stopped"
        await manager.stop_all()
    asyncio.run(flow())


def test_failed_notification_translation_retries_without_a_new_server_event(tmp_path, monkeypatch):
    monkeypatch.setattr("code_agent.codeintel.lsp.DOCUMENTATION_RETRY_DELAY", 0)

    async def flow():
        manager, session, _, _, uri = context(tmp_path)

        class Flaky:
            calls = 0

            async def translate_contents(self, values, **kwargs):
                self.calls += 1
                return values if self.calls == 1 else ["该方法已弃用。" for _ in values]

            async def close(self):
                pass

        flaky = Flaky()
        manager.documentation_translator = flaky
        frames = []
        manager.subscribe(session, frames.append)
        span = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 2}}
        manager._on_notification(session, {"method": "textDocument/publishDiagnostics", "params": {
            "uri": uri, "version": 1, "diagnostics": [{"range": span, "message": "The method is deprecated.", "severity": 2}]}})
        await asyncio.wait_for(asyncio.gather(*session.localization_tasks.values()), 2)
        assert flaky.calls == 2
        assert frames[-1]["params"]["diagnostics"][0]["message"] == "该方法已弃用。"
        assert session.diagnostics[uri][0]["message"] == "该方法已弃用。"
        await manager.stop_all()
    asyncio.run(flow())
