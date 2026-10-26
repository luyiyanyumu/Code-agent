"""Coding-session question recovery; temporary stores and offline providers only."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from code_agent.agent.project_team import ProjectTeam
from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.api.security import hash_password
from code_agent.models.events import Event, EventType
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore


QUESTIONS = [{"id": "fixture-choice", "question": "Choose the fixture behavior?",
              "options": [{"label": "Keep (Recommended)"}, {"label": "Change"}]}]
ANSWERS = [{"id": "fixture-choice", "selected": ["Keep (Recommended)"]}]


@pytest.fixture
def environment(agent_home, monkeypatch):
    monkeypatch.setattr("code_agent.sessions.manager.CLOSURE_LIMIT", 0)
    monkeypatch.setattr("code_agent.sessions.manager.MAX_CONSECUTIVE_WAKES", 0)
    db = Database(agent_home / "questions.db")
    uid = UserRepo(db).create("question-fixture", "fixture-only")["id"]
    projects = ProjectRepo(db)
    root = agent_home / "project"
    root.mkdir()
    project = projects.create(uid, "questions", str(root), str(root))
    manager = SessionManager(transcript=TranscriptStore(agent_home / "transcripts"),
                             session_repo=SessionRepo(db), approval_repo=ApprovalRepo(db),
                             router=ModelRouter(provider_name="offline"), registry=build_registry(),
                             project_repo=projects)
    team = ProjectTeam(manager)
    manager.agent_team = team
    lead = manager.create_project_session(uid, project["id"], permission_mode="read-only")
    roster = team.sync(project["id"], uid)
    yield manager, team, uid, lead["id"], roster
    db.close()


async def wait_for_questions(runtime, count=1):
    async with asyncio.timeout(3):
        while len(runtime.pending_questions()) != count:
            await asyncio.sleep(0.005)
    return runtime.pending_questions()


def test_question_waiter_registered_before_request_is_broadcast(environment, monkeypatch):
    manager, _, user, lead, _ = environment
    runtime = manager.get_runtime(lead, user)
    emit = runtime._emit_persisted
    seen = []

    def fast_answer(kind, payload):
        if kind == EventType.question_request:
            seen.extend(runtime.pending_questions())
            assert runtime.answer_question(payload["question_id"], ANSWERS)
        return emit(kind, payload)

    monkeypatch.setattr(runtime, "_emit_persisted", fast_answer)
    assert asyncio.run(runtime._ask_user(QUESTIONS)) == ANSWERS
    assert len(seen) == 1 and seen[0]["status"] == "pending"
    assert seen[0]["session_id"] == lead
    assert datetime.fromisoformat(seen[0]["expires_at"]) > datetime.fromtimestamp(seen[0]["ts"], timezone.utc)
    assert runtime.pending_questions() == []
    assert runtime._pending_question_payloads == {}


def test_all_live_groups_restore_without_mutating_the_runtime(environment):
    manager, _, user, lead, _ = environment
    runtime = manager.get_runtime(lead, user)

    async def scenario():
        first = asyncio.create_task(runtime._ask_user(QUESTIONS))
        groups = await wait_for_questions(runtime)
        original = groups[0]["questions"][0]["question"]
        groups[0]["questions"][0]["question"] = "changed by client"
        assert runtime.pending_questions()[0]["questions"][0]["question"] == original
        second = asyncio.create_task(runtime._ask_user(QUESTIONS))
        groups = await wait_for_questions(runtime, 2)
        runtime.broadcast(Event(seq=0, type=EventType.session_meta, payload={"phase": "tools", "busy": True}))
        assert runtime.phase == "waiting_question"
        assert runtime.answer_question(groups[0]["question_id"], ANSWERS)
        assert await first == ANSWERS
        assert runtime.phase == "waiting_question"
        assert [g["question_id"] for g in runtime.pending_questions()] == [groups[1]["question_id"]]
        assert runtime.answer_question(groups[1]["question_id"], False)
        assert await second is False
        assert runtime.pending_questions() == [] and runtime.phase == "tools"
        assert not runtime.answer_question(groups[0]["question_id"], ANSWERS)

    asyncio.run(scenario())


@pytest.mark.parametrize("end", ["timeout", "cancel", "budget"])
def test_terminal_waiters_are_not_restored(environment, monkeypatch, end):
    manager, _, user, lead, _ = environment
    runtime = manager.get_runtime(lead, user)
    if end == "timeout":
        monkeypatch.setattr("code_agent.sessions.manager.QUESTION_TIMEOUT", 0.03)

    async def scenario():
        async def budgeted_wait():
            async with asyncio.timeout(0.03):
                return await runtime._ask_user(QUESTIONS)

        task = asyncio.create_task(budgeted_wait() if end == "budget" else runtime._ask_user(QUESTIONS))
        group = (await wait_for_questions(runtime))[0]
        if end == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif end == "budget":
            with pytest.raises(TimeoutError):
                await task
        else:
            assert await task is None
        assert runtime.pending_questions() == []
        assert runtime._pending_questions == {} and runtime._pending_question_payloads == {}
        assert not runtime.answer_question(group["question_id"], ANSWERS)
        statuses = [ev.payload["status"] for ev in manager.transcript.replay(lead)
                    if ev.type == EventType.question_answered]
        assert statuses == ["timeout" if end == "timeout" else "cancelled"]

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["stop", "interrupt"])
def test_team_stop_and_interrupt_clear_question_waiters(environment, action):
    manager, team, user, lead, roster = environment
    child = team._create_worker(manager.project_repo.get(roster["lead"]["project_id"]), team._member(lead, user),
                                tree="main", role="subagent", title="question recovery fixture")
    runtime = manager.get_runtime(child["session_id"], user)
    provider = OfflineProvider(script=[OfflineProvider.tool_call_turn("ask_user_question", {"questions": QUESTIONS})])
    build = runtime._build_loop
    runtime._build_loop = lambda **kwargs: build(**{**kwargs, "provider": provider})

    async def scenario():
        await team.followup(lead, user, child["session_id"], "fixture stop")
        group = (await wait_for_questions(runtime))[0]
        await getattr(team, action)(lead, user, child["session_id"])
        async with asyncio.timeout(3):
            while runtime.busy:
                await asyncio.sleep(0.005)
        assert runtime.pending_questions() == []
        assert runtime._pending_question_payloads == {} and runtime._pending_questions == {}
        assert not runtime.answer_question(group["question_id"], ANSWERS)
        assert any(ev.type == EventType.question_answered and ev.payload["status"] == "cancelled"
                   for ev in manager.transcript.replay(child["session_id"]))
        await team.aclose()

    asyncio.run(scenario())


def test_history_without_live_future_is_not_an_actionable_question(environment):
    manager, _, user, lead, _ = environment
    manager.transcript.append(lead, EventType.question_request, {"question_id": "q_stale", "questions": QUESTIONS})
    runtime = manager.get_runtime(lead, user)
    assert runtime.pending_questions() == []
    assert not runtime.answer_question("q_stale", ANSWERS)


def test_in_process_legacy_future_can_recover_its_saved_payload(environment):
    manager, _, user, lead, _ = environment
    runtime = manager.get_runtime(lead, user)
    manager.transcript.append(lead, EventType.question_request, {"question_id": "q_live", "questions": QUESTIONS})

    async def scenario():
        future = asyncio.get_running_loop().create_future()
        runtime._pending_questions["q_live"] = future
        assert runtime.pending_questions()[0]["question_id"] == "q_live"
        future.cancel()
        assert runtime.pending_questions() == []
        runtime._pending_questions.clear()

    asyncio.run(scenario())


def test_wait_any_reports_question_attention_and_cursor_changes(environment):
    manager, team, user, lead, roster = environment
    child = team._create_worker(manager.project_repo.get(roster["lead"]["project_id"]), team._member(lead, user),
                                tree="main", role="subagent", title="question attention fixture")
    runtime = manager.get_runtime(child["session_id"], user)
    provider = OfflineProvider(script=[OfflineProvider.tool_call_turn("ask_user_question", {"questions": QUESTIONS}),
                                       OfflineProvider.text_turn("fixture completed")])
    build = runtime._build_loop
    runtime._build_loop = lambda **kwargs: build(**{**kwargs, "provider": provider})

    async def scenario():
        await team.followup(lead, user, child["session_id"], "fixture question")
        group = (await wait_for_questions(runtime))[0]
        descriptor = team.descriptor(child["session_id"])
        assert descriptor["phase"] == "waiting_question"
        assert descriptor["blocked_reason"] == "等待你回答问题"
        assert descriptor["pending_questions"][0]["question_id"] == group["question_id"]
        assert child["session_id"] in team._capacity(child["project_id"]).active
        first = await team.wait(lead, user, [child["session_id"]], timeout=0, mode="any")
        assert first["ready_session_ids"] == [child["session_id"]] and not first["timed_out"]
        quiet = await team.wait(lead, user, [child["session_id"]], timeout=0, mode="any", after_cursors=first["cursors"])
        assert quiet["ready_session_ids"] == [] and quiet["timed_out"]
        all_mode = await team.wait(lead, user, [child["session_id"]], timeout=0, mode="all")
        assert all_mode["timed_out"] and all_mode["ready_session_ids"] == []
        assert runtime.answer_question(group["question_id"], ANSWERS)
        done = await team.wait(lead, user, [child["session_id"]], timeout=3, mode="all")
        assert done["agents"][0]["state"] == "completed"
        assert done["agents"][0]["pending_questions"] == []
        assert done["cursors"] != first["cursors"]
        await team.aclose()

    asyncio.run(scenario())


@pytest.fixture
def client(agent_home, monkeypatch):
    for key in ("CODE_AGENT_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from code_agent.api.app import create_app
    app = create_app(provider_name="offline")
    with TestClient(app) as client:
        yield client


def login(client, username="admin", password="admin123"):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}, token


def test_pending_api_enforces_ownership_and_ignores_old_history(client):
    headers, _ = login(client)
    session_id = client.post("/api/v1/sessions", json={}, headers=headers).json()["id"]
    manager = client.app.state.sessions
    manager.transcript.append(session_id, EventType.question_request,
                              {"question_id": "q_no_waiter", "questions": QUESTIONS})
    assert client.get(f"/api/v1/sessions/{session_id}/questions/pending", headers=headers).json() == {"questions": []}
    assert client.get(f"/api/v1/sessions/{session_id}/questions/pending").status_code == 401
    UserRepo(manager.session_repo.db).create("other-question-user", hash_password("fixture-only"))
    other, _ = login(client, "other-question-user", "fixture-only")
    assert client.get(f"/api/v1/sessions/{session_id}/questions/pending", headers=other).status_code == 404
    assert client.get("/api/v1/sessions/s_missing/questions/pending", headers=headers).status_code == 404


def test_pending_api_ws_snapshot_and_rest_answer_resume_live_waiter(client):
    headers, token = login(client)
    session_id = client.post("/api/v1/sessions", json={}, headers=headers).json()["id"]
    manager = client.app.state.sessions
    meta = manager.session_repo.get(session_id)
    runtime = manager.get_runtime(session_id, meta["user_id"])

    async def begin_question():
        runtime._task = asyncio.create_task(runtime._ask_user(QUESTIONS))
        await wait_for_questions(runtime)

    client.portal.call(begin_question)
    groups = client.get(f"/api/v1/sessions/{session_id}/questions/pending", headers=headers).json()["questions"]
    assert len(groups) == 1 and groups[0]["session_id"] == session_id
    with client.websocket_connect(f"/api/v1/sessions/{session_id}/stream?token={token}") as socket:
        while True:
            event = socket.receive_json()
            if event["type"] == "session_meta":
                assert event["payload"]["phase"] == "waiting_question"
                assert event["payload"]["pending_questions"] == groups
                break
        answered = client.post(f"/api/v1/sessions/{session_id}/questions/{groups[0]['question_id']}/answer",
                               headers=headers, json={"answers": ANSWERS})
        assert answered.status_code == 200
        assert client.portal.call(lambda: runtime._task) == ANSWERS
        assert client.get(f"/api/v1/sessions/{session_id}/questions/pending", headers=headers).json() == {"questions": []}
        assert client.post(f"/api/v1/sessions/{session_id}/questions/{groups[0]['question_id']}/answer",
                           headers=headers, json={"answers": ANSWERS}).status_code == 409

