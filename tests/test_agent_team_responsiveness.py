"""Team polling must not block session streams on synchronous storage work."""

import asyncio
import threading
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from code_agent.api.agent_team_api import mount_agent_team_api
from code_agent.api.deps import CurrentUser, get_current_user


def test_team_storage_work_does_not_block_event_loop():
    entered, release = threading.Event(), threading.Event()
    threads = []

    def sync(project_id, user_id, tree):
        threads.append(threading.get_ident())
        entered.set()
        unblocked = release.wait(2)
        return {"project_id": project_id, "tree": tree, "released": unblocked}

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="owner", username="owner", role="admin")
    mount_agent_team_api(
        app, team=SimpleNamespace(sync=sync),
        service=SimpleNamespace(get_owned=lambda user_id, project_id: {"id": project_id}),
    )

    @app.get("/heartbeat")
    async def heartbeat():
        return {"ok": True}

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            request = asyncio.create_task(client.get("/api/v1/code/projects/project/agents"))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert (await client.get("/heartbeat")).json() == {"ok": True}
                assert len(threads) == 1 and threads[0] != threading.get_ident()
                assert not request.done()
            finally:
                release.set()
                response = await request
            assert response.status_code == 200
            assert response.json() == {"project_id": "project", "tree": "main", "released": True}

    asyncio.run(verify())
