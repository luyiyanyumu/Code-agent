"""后台任务的人可见面（M50）：`GET /api/v1/sessions/{sid}/jobs`。

M45 落地时 `jobs` 只有**模型**看得见（`job_list` / `job_output`），人这边一片空白：
用户看到会话"在忙"却不知道在忙什么——后台任务于是变成黑盒。这个端点把名册摊开。

三条断言是这个端点的全部价值所在：
1. **只读且要鉴权**（别人的会话看不了）；
2. **给名册**（有哪些任务、什么状态、产出多少字节、有没有丢块）；
3. **不给输出正文**——正文是模型的通道，而且可能很大（环上限 256 KiB/任务），
   把正文塞进列表接口等于让一个 GET 能返回几百 MB。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.api.security import hash_password
from code_agent.jobs.registry import DONE, RUNNING, JobRegistry, as_dict


@pytest.fixture
def client(agent_home):
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> dict:
    token = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _session(client: TestClient, h: dict) -> str:
    return client.post("/api/v1/sessions", json={}, headers=h).json()["id"]


def _runtime(client: TestClient, sid: str):
    user = client.app.state.users.get_by_username("admin")
    return client.app.state.sessions.get_runtime(sid, user["id"])


def _settle(client: TestClient, sid: str, *, label: str = "npm install", output: str = "") -> str:
    """在测试线程自己的事件循环里起并结算一个任务，返回 job_id。"""
    runtime = _runtime(client, sid)

    async def main():
        async def runner(job):
            if output:
                job.append(output.encode())
            return "exit_code=0"

        job = runtime.jobs.start(session_id=sid, kind="bash", label=label, runner=runner)
        await job._task
        return job.id

    return asyncio.run(main())


# ----------------------------------------------------------------------
# 鉴权与所有权
# ----------------------------------------------------------------------
def test_jobs_endpoint_requires_auth(client) -> None:
    assert client.get("/api/v1/sessions/s_x/jobs").status_code == 401


def test_jobs_endpoint_404_for_unknown_session(client) -> None:
    h = _login(client)
    assert client.get("/api/v1/sessions/s_missing/jobs", headers=h).status_code == 404


def test_jobs_endpoint_is_scoped_to_the_owner(client) -> None:
    h = _login(client)
    sid = _session(client, h)
    client.app.state.users.create("bob9", hash_password("pw123456"))
    bob = {
        "Authorization": "Bearer "
        + client.post(
            "/api/v1/auth/login", json={"username": "bob9", "password": "pw123456"}
        ).json()["token"]
    }
    assert client.get(f"/api/v1/sessions/{sid}/jobs", headers=bob).status_code in (403, 404)


# ----------------------------------------------------------------------
# 名册
# ----------------------------------------------------------------------
def test_empty_job_list(client) -> None:
    h = _login(client)
    sid = _session(client, h)
    body = client.get(f"/api/v1/sessions/{sid}/jobs", headers=h).json()
    assert body == {"jobs": [], "running": 0}


def test_settled_job_is_listed_with_its_metadata(client) -> None:
    h = _login(client)
    sid = _session(client, h)
    job_id = _settle(client, sid, label="npm run build", output="built ok\n")

    body = client.get(f"/api/v1/sessions/{sid}/jobs", headers=h).json()
    assert body["running"] == 0
    assert len(body["jobs"]) == 1
    job = body["jobs"][0]
    assert job["id"] == job_id
    assert job["kind"] == "bash"
    assert job["label"] == "npm run build"
    assert job["status"] == DONE
    assert job["finished_at"] is not None
    assert job["buffered_bytes"] > 0
    assert job["dropped_bytes"] == 0          # 没超环上限就不该报丢块
    assert job["kill_reason"] == ""


def test_response_does_not_leak_the_output_body(client) -> None:
    """**核心**：名册**不含输出正文**。

    正文是模型的通道（带消费游标与有损说明），而且可能很大（256 KiB/任务）。
    把正文塞进列表接口，一个跑飞的后台进程就能让一个 GET 返回几百 MB。
    """
    h = _login(client)
    sid = _session(client, h)
    secret = "SECRET_OUTPUT_MARKER_42"
    _settle(client, sid, output=f"{secret}\n")

    raw = client.get(f"/api/v1/sessions/{sid}/jobs", headers=h).text
    assert secret not in raw, "任务输出正文泄进了名册接口"
    # 但"产出了多少字节"要给出来（否则人不知道有没有输出）
    assert "buffered_bytes" in raw


def test_jobs_of_other_sessions_are_not_mixed_in(client) -> None:
    h = _login(client)
    one = _session(client, h)
    two = _session(client, h)
    _settle(client, one, label="only-in-one")

    assert client.get(f"/api/v1/sessions/{two}/jobs", headers=h).json()["jobs"] == []
    assert len(client.get(f"/api/v1/sessions/{one}/jobs", headers=h).json()["jobs"]) == 1


# ----------------------------------------------------------------------
# 快照形状（含"正在跑"这一种，HTTP 那侧不容易稳定构造）
# ----------------------------------------------------------------------
def test_snapshot_reports_a_running_job(agent_home) -> None:
    registry = JobRegistry()

    async def main():
        async def slow(job):
            await asyncio.sleep(5)

        job = registry.start(session_id="s1", kind="bash", label="long", runner=slow)
        snapshot = registry.snapshot("s1")
        assert snapshot[0]["status"] == RUNNING
        assert snapshot[0]["finished_at"] is None
        job._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job._task

    asyncio.run(main())


def test_as_dict_shape_is_stable(agent_home) -> None:
    """字段名是前端契约：少了/改名了前端就静默显示空白。"""

    async def main():
        async def runner(job):
            return "ok"

        registry = JobRegistry()
        job = registry.start(session_id="s1", kind="bash", label="x", runner=runner)
        await job._task
        data = as_dict(job)
        assert set(data) == {
            "id", "kind", "label", "status", "created_at",
            "finished_at", "buffered_bytes", "dropped_bytes", "kill_reason",
        }
        assert isinstance(data["created_at"], float)

    asyncio.run(main())
