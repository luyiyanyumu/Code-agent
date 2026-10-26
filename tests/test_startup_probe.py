"""Optional embedding discovery must not gate the packaged workbench."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from code_agent.api.app import create_app


@pytest.fixture(autouse=True)
def no_preconfigured_embedding(monkeypatch):
    for key in ("CODE_AGENT_EMBED_BASE_URL", "CODE_AGENT_EMBED_MODEL", "CODE_AGENT_EMBED_API_KEY",
                "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def test_blocked_probe_does_not_delay_health_or_ui_and_later_enables_vectors(agent_home, tmp_path, monkeypatch):
    directory = tmp_path / "ui"
    directory.mkdir()
    (directory / "index.html").write_text("<html>Ready workbench</html>", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_WEB_DIST", str(directory))

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_probe():
            entered.set()
            await release.wait()
            return {"found": True, "base_url": "http://127.0.0.1:11434/v1", "model": "test-model", "dim": 2}

        monkeypatch.setattr("code_agent.retrieval.embed.autodetect_local", blocked_probe)
        app = create_app(provider_name="offline")
        async with asyncio.timeout(3):
            async with app.router.lifespan_context(app):
                await entered.wait()
                assert app.state.embed_probe == {"status": "pending", "found": False}
                assert app.state.knowledge.embedder.enabled is False
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
                    health, ui = await asyncio.wait_for(
                        asyncio.gather(browser.get("/healthz"), browser.get("/")), timeout=1,
                    )
                assert health.status_code == 200 and health.json()["ok"] is True
                assert ui.status_code == 200 and "Ready workbench" in ui.text
                assert not app.state.embed_probe_task.done()
                release.set()
                await app.state.embed_probe_task
                assert app.state.embed_probe["status"] == "completed"
                assert app.state.embed_probe["found"] is True
                assert app.state.knowledge.embedder.enabled is True
                assert app.state.knowledge.embedder.model == "test-model"
                assert app.state.knowledge.embedder.base_url == "http://127.0.0.1:11434/v1"
        assert app.state.embed_probe_task.done()

    asyncio.run(scenario())


def test_shutdown_cancels_discovery_before_closing_knowledge(agent_home, monkeypatch):
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked_probe():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr("code_agent.retrieval.embed.autodetect_local", blocked_probe)
        app = create_app(provider_name="offline")
        close = app.state.knowledge.aclose

        async def checked_close():
            assert cancelled.is_set(), "discovery can still replace an already closed embedder"
            await close()

        monkeypatch.setattr(app.state.knowledge, "aclose", checked_close)
        async with asyncio.timeout(3):
            async with app.router.lifespan_context(app):
                await entered.wait()
            assert cancelled.is_set()
            assert app.state.embed_probe_task.cancelled()
            assert app.state.embed_probe == {"status": "cancelled", "found": False}
            assert not any(task.get_name() == "code-agent-embedding-probe" for task in asyncio.all_tasks())

    asyncio.run(scenario())


def test_probe_failure_remains_observable_and_keyword_search_available(agent_home, monkeypatch):
    async def broken_probe():
        raise RuntimeError("discovery unavailable")

    monkeypatch.setattr("code_agent.retrieval.embed.autodetect_local", broken_probe)

    async def scenario():
        app = create_app(provider_name="offline")
        async with app.router.lifespan_context(app):
            await app.state.embed_probe_task
            assert app.state.embed_probe == {
                "status": "failed", "found": False, "error": "discovery unavailable",
            }
            assert app.state.knowledge.embedder.enabled is False
            app.state.knowledge.create(title="Startup", content="keyword search remains available")
            assert await app.state.knowledge.search("keyword search")

    asyncio.run(scenario())


def test_explicit_endpoint_keeps_its_model_without_discovery(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_EMBED_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("CODE_AGENT_EMBED_MODEL", "configured-model")

    async def unexpected_probe(**kwargs):
        raise AssertionError("an explicitly configured endpoint must not be probed")

    monkeypatch.setattr("code_agent.retrieval.embed.probe_local", unexpected_probe)

    async def scenario():
        app = create_app(provider_name="offline")
        async with app.router.lifespan_context(app):
            await app.state.embed_probe_task
            assert app.state.embed_probe["status"] == "completed"
            assert app.state.embed_probe["model"] == "configured-model"
            assert app.state.knowledge.embedder.enabled is True
            assert app.state.knowledge.embedder.model == "configured-model"

    asyncio.run(scenario())
