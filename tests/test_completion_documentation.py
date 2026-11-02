"""Completion prose is lazy, cached and isolated from executable LSP payloads."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from code_agent.codeintel.documentation_translation import DocumentationTranslator
from code_agent.codeintel.documentation_translation_store import DocumentationTranslationStore
from code_agent.codeintel.lsp import LspManager, LspSession
from code_agent.codeintel.lsp_localization import localize_result, restore_protocol_text
from code_agent.codeintel.uri import file_uri


META = "_code_agent_original"
DOC = "Returns the current value."
OTHER = "Creates a new value."


class Provider:
    name, model, base_url = "fixture", "completion-docs", "https://offline.invalid"

    def __init__(self, *, gate=None, fail=False):
        self.calls = []
        self.started = asyncio.Event()
        self.gate, self.fail = gate, fail

    async def chat(self, messages, tools):
        assert tools == []
        self.calls.append(copy.deepcopy(messages))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("fixture failure")
        texts = json.loads(messages[-1]["content"])["texts"]
        translated = [value.replace(DOC, "返回当前值。").replace(OTHER, "创建新值。") for value in texts]
        return SimpleNamespace(text=json.dumps({"translations": translated}, ensure_ascii=False), tool_calls=[])


def item(label="getValue", documentation=DOC):
    return {"label": label, "labelDetails": {"detail": "(int index)", "description": "java.util.List"},
            "detail": "public String getValue(int index)", "documentation": {"kind": "markdown", "value": documentation},
            "insertText": "getValue(${1:index})", "filterText": label, "sortText": "0001",
            "insertTextFormat": 2, "commitCharacters": [";"], "textEdit": {"newText": "getValue(${1:index})", "range": {}},
            "additionalTextEdits": [{"newText": "import example.Value;", "range": {}}],
            "data": {"server": "opaque", META: {"documentation": "opaque-original"}},
            "command": {"title": "Server command title", "command": "not-executed", "arguments": [{"raw": "value"}]}}


class Client:
    alive = True

    def __init__(self, items):
        self.result = items
        self.resolved_result = None
        self.requests, self.notifications = [], []

    async def request(self, method, params, *, timeout, on_request_id):
        self.requests.append((method, copy.deepcopy(params)))
        if on_request_id:
            on_request_id(700 + len(self.requests))
        if method == "textDocument/completion":
            return copy.deepcopy(self.result)
        assert method == "completionItem/resolve"
        return copy.deepcopy(self.resolved_result if self.resolved_result is not None else params)

    async def notify(self, method, params):
        self.notifications.append((method, copy.deepcopy(params)))


def setup(tmp_path, *, provider=None, resolve=True, result=None, store=None, factory=None):
    provider = provider or Provider()
    source = result if result is not None else {"isIncomplete": False, "items": [item()], "itemDefaults": {"insertTextMode": 2}}
    client = Client(source)
    session = LspSession(key="fixture", server_id="fixture", lang="java", project_id="p", tree_id="main",
                         root=tmp_path, command="fixture", args=[], client=client,
                         capabilities={"completionProvider": {"resolveProvider": resolve}})
    uri = file_uri(tmp_path / "Example.java")
    session.documents[uri] = 1
    translator = DocumentationTranslator(factory or (lambda: provider), store=store)
    manager = LspManager()
    manager.documentation_translator = translator
    message = {"id": 1, "method": "textDocument/completion", "params": {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 0}}}
    return manager, session, translator, provider, message, uri


async def resolved(manager, session, value):
    return await manager.handle_client_request(session, {"id": 2, "method": "completionItem/resolve", "params": value})


def test_list_is_cache_only_and_focused_resolve_preserves_all_protocol_fields(tmp_path):
    async def flow():
        provider = Provider(gate=asyncio.Event())
        factories = []
        manager, session, translator, _, request, _ = setup(tmp_path, provider=provider,
            factory=lambda: factories.append(True) or provider)
        source = copy.deepcopy(session.client.result)
        listed = await asyncio.wait_for(manager.handle_client_request(session, request), 2)
        assert not provider.calls and factories == []
        entry = listed["result"]["items"][0]
        assert entry["documentation_translation"]["status"] == "untranslated"
        assert entry["documentation"] == source["items"][0]["documentation"]
        assert restore_protocol_text(listed["result"]) == source
        task = asyncio.create_task(resolved(manager, session, entry))
        await provider.started.wait()
        assert len(provider.calls) == 1
        provider.gate.set()
        output = (await task)["result"]
        assert output["documentation"]["value"] == "返回当前值。"
        assert output["documentation_translation"]["status"] == "ready"
        assert restore_protocol_text(output) == source["items"][0]
        assert session.client.requests[-1] == ("completionItem/resolve", source["items"][0])
        assert output[META]["_completion_context"] == entry[META]["_completion_context"]
        listed_again = await manager.handle_client_request(session, request)
        assert listed_again["result"]["items"][0]["documentation"]["value"] == "返回当前值。"
        assert len(provider.calls) == 1 and len(factories) == 1
        await manager.stop_all()
    asyncio.run(flow())


@pytest.mark.parametrize("wrapped", [False, True])
def test_servers_without_resolve_translate_only_selected_trusted_receipt_locally(tmp_path, wrapped):
    async def flow():
        original = item()
        original.pop("data")
        defaults = {"data": {"opaque": "inherited"}, "commitCharacters": ["."]}
        source = {"isIncomplete": True, "items": [original], "itemDefaults": defaults} if wrapped else [item()]
        manager, session, _, provider, request, _ = setup(tmp_path, resolve=False, result=source)
        response = await manager.handle_client_request(session, request)
        entry = response["result"]["items"][0] if wrapped else response["result"][0]
        # Display fields supplied by a caller never replace the stored server
        # receipt used by the local no-resolve path.
        selected = {**entry, "documentation": "Forged caller text.", "label": "forged"}
        output = (await resolved(manager, session, selected))["result"]
        expected = {**original, "data": defaults["data"]} if wrapped else source[0]
        assert output["documentation"]["value"] == "返回当前值。"
        assert restore_protocol_text(output) == expected
        assert [method for method, _ in session.client.requests] == ["textDocument/completion"]
        assert len(provider.calls) == 1
        assert restore_protocol_text(response["result"]) == source
        await manager.stop_all()
    asyncio.run(flow())


def test_partial_cache_hits_do_not_wait_for_or_translate_uncached_neighbours(tmp_path):
    async def flow():
        store = DocumentationTranslationStore(tmp_path / "cache.sqlite3")
        store.put(DOC, "返回当前值。")
        factories = []
        translator = DocumentationTranslator(lambda: factories.append(True), store=store)
        source = [item(), item("createValue", OTHER)]
        output = await localize_result(translator, "textDocument/completion", source, completion_cache_only=True)
        assert output[0]["documentation"]["value"] == "返回当前值。"
        assert output[0]["documentation_translation"]["status"] == "ready"
        assert output[1]["documentation"]["value"] == OTHER
        assert output[1]["documentation_translation"]["status"] == "untranslated"
        assert factories == [] and translator.stats()["persistent_hits"] == 1
        assert restore_protocol_text(output) == source
        await translator.close(); store.close()
    asyncio.run(flow())


def test_completion_translations_are_reused_after_translator_restart_without_provider(tmp_path):
    async def flow():
        path = tmp_path / "persistent.sqlite3"
        first_store = DocumentationTranslationStore(path)
        manager, session, _, provider, request, _ = setup(tmp_path, store=first_store)
        entry = (await manager.handle_client_request(session, request))["result"]["items"][0]
        assert (await resolved(manager, session, entry))["result"]["documentation"]["value"] == "返回当前值。"
        assert len(provider.calls) == 1
        await manager.stop_all(); first_store.close()
        second_store = DocumentationTranslationStore(path)
        def unavailable():
            raise AssertionError("A cache hit must not construct a provider")
        second, fresh, translator, _, request, _ = setup(tmp_path, store=second_store, factory=unavailable)
        output = (await second.handle_client_request(fresh, request))["result"]["items"][0]
        assert output["documentation"]["value"] == "返回当前值。"
        assert output["documentation_translation"]["status"] == "ready"
        assert translator.stats()["persistent_hits"] >= 1
        await second.stop_all(); second_store.close()
    asyncio.run(flow())


@pytest.mark.parametrize("real_resolve", [False, True])
def test_slow_translation_returns_resolved_edits_before_model_and_warms_persistent_cache(tmp_path, real_resolve):
    async def flow():
        path = tmp_path / "slow-completion.sqlite3"
        store = DocumentationTranslationStore(path)
        provider = Provider(gate=asyncio.Event())
        original = item()
        listed_item = copy.deepcopy(original)
        if real_resolve:
            # The auto-import only arrives from resolve, never the list.
            listed_item.pop("additionalTextEdits")
        manager, session, translator, _, request, _ = setup(tmp_path, provider=provider,
            store=store, resolve=real_resolve, result=[listed_item])
        if real_resolve:
            session.client.resolved_result = original
        entry = (await manager.handle_client_request(session, request))["result"][0]
        output = (await asyncio.wait_for(resolved(manager, session, entry), .5))["result"]
        assert provider.started.is_set() and not provider.gate.is_set()
        assert translator._inflight, "Only the request waiter ends; warming must continue"
        assert output["additionalTextEdits"] == original["additionalTextEdits"]
        assert output["documentation_translation"] == {
            "language": "zh-CN", "source": "configured-model", "status": "pending", "refresh_required": True}
        assert output["documentation"] == original["documentation"]
        assert output[META]["_completion_context"] == entry[META]["_completion_context"]
        assert restore_protocol_text(output) == original
        assert [method for method, _ in session.client.requests] == (
            ["textDocument/completion", "completionItem/resolve"] if real_resolve else ["textDocument/completion"])
        provider.gate.set()
        await asyncio.gather(*translator._inflight.values())
        cached = (await manager.handle_client_request(session, request))["result"][0]
        assert cached["documentation"]["value"] == "返回当前值。"
        assert cached["documentation_translation"]["status"] == "ready"
        assert "refresh_required" not in cached["documentation_translation"]
        assert len(provider.calls) == 1
        await manager.stop_all(); store.close()
        reopened = DocumentationTranslationStore(path)
        def unavailable():
            raise AssertionError("Background warming must survive translator restart")
        second, fresh, second_translator, _, second_request, _ = setup(tmp_path,
            store=reopened, factory=unavailable, result=[listed_item])
        recovered = (await second.handle_client_request(fresh, second_request))["result"][0]
        assert recovered["documentation"]["value"] == "返回当前值。"
        assert recovered["documentation_translation"]["status"] == "ready"
        assert second_translator.stats()["persistent_hits"] == 1
        await second.stop_all(); reopened.close()
    asyncio.run(flow())


def test_resolve_spent_deadline_does_not_start_or_claim_background_translation(tmp_path, monkeypatch):
    async def flow():
        manager, session, _, provider, request, _ = setup(tmp_path)
        entry = (await manager.handle_client_request(session, request))["result"]["items"][0]
        clock = SimpleNamespace(value=100.0)
        monkeypatch.setattr("code_agent.codeintel.lsp.time", SimpleNamespace(monotonic=lambda: clock.value))
        original_request = session.client.request
        async def spent_request(*args, **kwargs):
            response = await original_request(*args, **kwargs)
            clock.value += 1
            return response
        session.client.request = spent_request
        response = await manager.handle_client_request(session,
            {"id": 2, "method": "completionItem/resolve", "params": entry, "timeout": .001})
        output = response["result"]
        assert output["documentation_translation"]["status"] == "untranslated"
        assert "refresh_required" not in output["documentation_translation"]
        assert output["additionalTextEdits"] == item()["additionalTextEdits"]
        assert not provider.calls
        await manager.stop_all()
    asyncio.run(flow())


@pytest.mark.parametrize("real_resolve", [False, True])
def test_stale_unknown_and_expired_tickets_do_not_call_server_or_model(tmp_path, real_resolve, monkeypatch):
    async def flow():
        monkeypatch.setattr("code_agent.codeintel.lsp.MAX_COMPLETION_CONTEXTS", 2)
        manager, session, _, provider, request, uri = setup(tmp_path, resolve=real_resolve,
            result=[item(str(number)) for number in range(3)])
        values = (await manager.handle_client_request(session, request))["result"]
        assert len(session.completion_contexts) == 2
        for candidate in [values[0], {META: {"_completion_context": "unknown"}}, {META: {"_completion_context": []}}]:
            response = await resolved(manager, session, candidate)
            assert response["error"]["code"] == -32801
        session.documents[uri] = 2
        assert (await resolved(manager, session, values[-1]))["error"]["code"] == -32801
        assert [method for method, _ in session.client.requests] == ["textDocument/completion"]
        assert not provider.calls
        await manager.stop_all()
    asyncio.run(flow())


@pytest.mark.parametrize("real_resolve", [False, True])
@pytest.mark.parametrize("change", ["close-and-reopen", "version", "server-exit"])
def test_document_or_server_changes_cannot_revive_an_inflight_translation(tmp_path, real_resolve, change):
    async def flow():
        provider = Provider(gate=asyncio.Event())
        manager, session, _, _, request, uri = setup(tmp_path, provider=provider, resolve=real_resolve)
        entry = (await manager.handle_client_request(session, request))["result"]["items"][0]
        task = asyncio.create_task(resolved(manager, session, entry))
        await provider.started.wait()
        if change == "close-and-reopen":
            await manager.handle_client_notification(session, {"method": "textDocument/didClose", "params": {"textDocument": {"uri": uri}}})
            session.documents[uri] = 1
        elif change == "version":
            session.documents[uri] = 2
        else:
            session.client.alive = False
        provider.gate.set()
        assert (await task)["error"]["code"] == -32801
        if change == "close-and-reopen":
            assert not session.completion_contexts
        await manager.stop_all()
    asyncio.run(flow())


@pytest.mark.parametrize("real_resolve", [False, True])
def test_cancelling_focused_translation_isolated_from_server_and_shared_cache(tmp_path, real_resolve):
    async def flow():
        provider = Provider(gate=asyncio.Event())
        manager, session, translator, _, request, _ = setup(tmp_path, provider=provider, resolve=real_resolve)
        entry = (await manager.handle_client_request(session, request))["result"]["items"][0]
        task = asyncio.create_task(resolved(manager, session, entry))
        await provider.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.client.notifications == ([("$/cancelRequest", {"id": 702})] if real_resolve else [])
        provider.gate.set()
        await asyncio.gather(*translator._inflight.values())
        second = (await resolved(manager, session, entry))["result"]
        assert second["documentation"]["value"] == "返回当前值。"
        assert len(provider.calls) == 1
        await manager.stop_all()
    asyncio.run(flow())


def test_failed_resolve_keeps_original_and_explicit_failure_without_poisoning_cache(tmp_path):
    async def flow():
        manager, session, translator, provider, request, _ = setup(tmp_path, provider=Provider(fail=True), resolve=False)
        entry = (await manager.handle_client_request(session, request))["result"]["items"][0]
        output = (await resolved(manager, session, entry))["result"]
        assert output["documentation_translation"]["status"] == "failed"
        assert output[META]["documentation"] == item()["documentation"]
        assert restore_protocol_text(output) == item()
        assert translator.store.stats()["entries"] == 0
        again = (await resolved(manager, session, output))["result"]
        assert again["documentation_translation"]["status"] == "failed"
        assert len(provider.calls) == 1, "The existing failure cooldown prevents model retry loops"
        await manager.stop_all()
    asyncio.run(flow())
