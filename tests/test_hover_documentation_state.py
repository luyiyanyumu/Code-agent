"""Foreground hover state uses stub models, real SQLite and event gates only."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from code_agent.codeintel.documentation_translation import DocumentationTranslator
from code_agent.codeintel.documentation_translation_store import DocumentationTranslationStore
from code_agent.codeintel.lsp import LspManager, LspSession
from code_agent.codeintel.lsp_localization import restore_protocol_text
from code_agent.codeintel.uri import file_uri


SOURCE = "Returns the current value."
OTHER = "Creates a new value."


class Provider:
    name, model, base_url = "fixture", "hover-state", "https://offline.invalid"

    def __init__(self, *, gate=None, fail=False, invalid=False):
        self.gate, self.fail, self.invalid = gate, fail, invalid
        self.started = asyncio.Event()
        self.calls = 0

    async def chat(self, messages, tools):
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("secret-token PRIVATE_CONFIG untrusted-error")
        texts = json.loads(messages[-1]["content"])["texts"]
        values = [] if self.invalid else [value.replace(SOURCE, "返回当前值。").replace(OTHER, "创建新值。") for value in texts]
        return SimpleNamespace(text=json.dumps({"translations": values}, ensure_ascii=False), tool_calls=[])


class Client:
    alive = True

    def __init__(self):
        self.requests, self.notifications = [], []
        self.gate = None
        self.started = asyncio.Event()

    async def request(self, method, params, *, timeout, on_request_id):
        self.requests.append((method, copy.deepcopy(params)))
        if on_request_id:
            on_request_id(len(self.requests))
        self.started.set()
        if self.gate:
            await self.gate.wait()
        return {"contents": {"kind": "markdown", "value": SOURCE if params["position"]["character"] != 9 else OTHER},
                "range": {"start": params["position"], "end": params["position"]}}

    async def notify(self, method, params):
        self.notifications.append((method, params))


def setup(tmp_path, *, provider=None, store=None, factory=None, timeout=1):
    client = Client()
    session = LspSession(key="fixture", server_id="fixture", lang="java", project_id="p", tree_id="main",
        root=tmp_path, command="fixture", args=[], client=client, capabilities={"hoverProvider": True})
    uri = file_uri(tmp_path / "Example.java")
    session.documents[uri] = 1
    provider = provider or Provider()
    manager = LspManager()
    manager.documentation_translator = DocumentationTranslator(factory or (lambda: provider), store=store, timeout=timeout)
    frames = []
    manager.subscribe(session, frames.append)
    return manager, session, provider, uri, frames


async def hover(manager, session, uri, *, retry=False, character=0):
    return await manager.handle_client_request(session, {"id": "hover", "method": "textDocument/hover", "params": {
        "textDocument": {"uri": uri}, "position": {"line": 0, "character": character},
        **({"codeAgentRetryTranslation": True} if retry else {})}})


async def finish(manager, *, canceled=False):
    await asyncio.gather(*list(manager._foreground_hover_jobs), return_exceptions=canceled)


def test_auto_pending_ready_notification_and_persistent_reuse(tmp_path):
    async def flow():
        path = tmp_path / "docs.sqlite3"
        store = DocumentationTranslationStore(path)
        gate = asyncio.Event()
        manager, session, provider, uri, frames = setup(tmp_path, store=store, provider=Provider(gate=gate))
        first = (await asyncio.wait_for(hover(manager, session, uri), .3))["result"]
        assert first["contents"]["value"] == SOURCE
        assert first["documentation_translation"]["status"] == "pending"
        await provider.started.wait()
        assert provider.calls == 1 and not gate.is_set() and not frames
        gate.set()
        await finish(manager)
        assert frames == [{"jsonrpc": "2.0", "method": "code-agent/documentationUpdated", "params": {
            "uri": uri, "version": 1, "position": {"line": 0, "character": 0}, "status": "ready"}}]
        current = (await hover(manager, session, uri))["result"]
        assert current["contents"]["value"] == "返回当前值。"
        assert current["documentation_translation"]["status"] == "ready"
        assert restore_protocol_text(current)["contents"]["value"] == SOURCE
        assert provider.calls == 1 and store.get(SOURCE) == "返回当前值。"
        await manager.stop_all(); store.close()
        reopened = DocumentationTranslationStore(path)
        def unavailable():
            raise AssertionError("A permanent cache hit cannot instantiate the model")
        second, fresh, _, new_uri, new_frames = setup(tmp_path, store=reopened, factory=unavailable)
        cached = (await hover(second, fresh, new_uri))["result"]
        assert cached["documentation_translation"]["status"] == "ready"
        assert cached["contents"]["value"] == "返回当前值。"
        assert not new_frames and not second._foreground_hover_jobs
        await second.stop_all(); reopened.close()
    asyncio.run(flow())


def test_failure_is_terminal_sanitized_and_retry_is_scoped_and_deduplicated(tmp_path):
    async def flow():
        store = DocumentationTranslationStore(tmp_path / "retry.sqlite3")
        manager, session, provider, uri, frames = setup(tmp_path, store=store, provider=Provider(fail=True))
        await hover(manager, session, uri)
        await finish(manager)
        failed = (await hover(manager, session, uri))["result"]
        status = failed["documentation_translation"]
        assert status["status"] == "failed" and status["error_code"] == "provider_error" and status["retryable"]
        assert not any(value in json.dumps(failed) for value in ("secret-token", "PRIVATE_CONFIG", "untrusted-error"))
        assert frames[-1]["params"]["status"] == "failed"
        for _ in range(3):
            await hover(manager, session, uri)
        assert provider.calls == 1 and not manager._foreground_hover_jobs
        # Workspace warmup must not bypass a foreground failure after the
        # ordinary translation cooldown expires and keep retrying forever.
        manager.documentation_translator.clock = lambda: 10**15
        await manager.documentation_translator.translate_hover({"contents": SOURCE})
        assert provider.calls == 1
        await hover(manager, session, uri, character=9)
        await finish(manager)
        assert provider.calls == 2
        provider.fail = False
        provider.gate = asyncio.Event()
        repeated = await asyncio.gather(*(hover(manager, session, uri, retry=True) for _ in range(6)))
        assert all(value["result"]["documentation_translation"]["status"] == "pending" for value in repeated)
        await asyncio.sleep(0)
        assert provider.calls == 3
        assert all("codeAgentRetryTranslation" not in params for _, params in session.client.requests)
        provider.gate.set()
        await finish(manager)
        assert (await hover(manager, session, uri))["result"]["contents"]["value"] == "返回当前值。"
        other = (await hover(manager, session, uri, character=9))["result"]
        assert other["documentation_translation"]["status"] == "failed"
        assert store.get(SOURCE) == "返回当前值。" and store.get(OTHER) is None
        assert provider.calls == 3
        await manager.stop_all(); store.close()
    asyncio.run(flow())


def test_concurrent_hover_positions_share_model_and_update_each_current_source(tmp_path):
    async def flow():
        gate = asyncio.Event()
        manager, session, provider, uri, frames = setup(tmp_path, provider=Provider(gate=gate))
        responses = await asyncio.gather(*(hover(manager, session, uri, character=n) for n in range(4)))
        await provider.started.wait()
        assert provider.calls == 1
        assert all(value["result"]["documentation_translation"]["status"] == "pending" for value in responses)
        gate.set(); await finish(manager)
        assert len(frames) == 4 and all(frame["params"]["status"] == "ready" for frame in frames)
        await manager.stop_all()
    asyncio.run(flow())


@pytest.mark.parametrize("change", ["version", "close-reopen", "server-exit", "shutdown"])
def test_changed_closed_or_stopped_sources_cannot_publish_late_ready(tmp_path, change):
    async def flow():
        gate = asyncio.Event()
        manager, session, provider, uri, frames = setup(tmp_path, provider=Provider(gate=gate))
        await hover(manager, session, uri)
        await provider.started.wait()
        if change == "version":
            session.documents[uri] = 2
        elif change == "close-reopen":
            await manager.handle_client_notification(session, {"method": "textDocument/didClose", "params": {"textDocument": {"uri": uri}}})
            session.documents[uri] = 1
            assert not session.hover_contexts
        elif change == "server-exit":
            session.client.alive = False
        else:
            await manager.stop_all()
        gate.set(); await finish(manager, canceled=True)
        assert not frames
        await manager.stop_all()
    asyncio.run(flow())


def test_canceled_lsp_request_cannot_start_foreground_translation(tmp_path):
    async def flow():
        manager, session, provider, uri, frames = setup(tmp_path)
        session.client.gate = asyncio.Event()
        task = asyncio.create_task(hover(manager, session, uri))
        await session.client.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.calls == 0 and not session.hover_contexts and not frames
        assert session.client.notifications == [("$/cancelRequest", {"id": 1})]
        await manager.stop_all()
    asyncio.run(flow())


@pytest.mark.parametrize("kind,code", [("timeout", "timeout"), ("invalid", "invalid_response"), ("offline", "provider_unavailable")])
def test_failure_codes_are_explicit_without_automatic_model_retries(tmp_path, kind, code):
    async def flow():
        provider = Provider(gate=asyncio.Event()) if kind == "timeout" else Provider(invalid=kind == "invalid")
        manager, session, provider, uri, _ = setup(tmp_path, provider=provider, timeout=.01,
            factory=(lambda: None) if kind == "offline" else None)
        await hover(manager, session, uri); await finish(manager)
        result = (await hover(manager, session, uri))["result"]
        assert result["documentation_translation"]["status"] == "failed"
        assert result["documentation_translation"]["error_code"] == code
        calls = provider.calls
        await hover(manager, session, uri); await finish(manager)
        assert provider.calls == calls
        await manager.stop_all()
    asyncio.run(flow())


@pytest.mark.parametrize("contents", [[], "", "返回当前值。", {"language": "java", "value": "String getValue();"}])
def test_no_translatable_prose_never_claims_pending(tmp_path, contents):
    async def flow():
        manager, session, provider, uri, frames = setup(tmp_path)
        original = session.client.request
        async def empty(*args, **kwargs):
            result = await original(*args, **kwargs)
            result["contents"] = contents
            return result
        session.client.request = empty
        response = (await hover(manager, session, uri))["result"]
        assert response["documentation_translation"]["status"] == "untranslated"
        assert response["documentation_translation"]["required"] is False
        assert response["documentation_translation"]["retryable"] is False
        assert "error_code" not in response["documentation_translation"]
        assert response["contents"] == contents
        assert not provider.calls and not manager._foreground_hover_jobs and not frames
        await manager.stop_all()
    asyncio.run(flow())
