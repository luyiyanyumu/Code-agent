"""Contract tests for the standalone chat service using disposable local data."""
import ast
import asyncio
import base64
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from chat_agent.api import create_app
from chat_agent.config import Config


@pytest.fixture
def client(tmp_path):
    app = create_app(Config(home=tmp_path / "chat"))
    with TestClient(app, base_url="http://127.0.0.1") as connection:
        yield connection


def create_session(client, **body):
    response = client.post("/api/v1/sessions", json=body)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def until_idle(client, sid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/sessions/{sid}")
        assert response.status_code == 200, response.text
        if not response.json().get("busy"):
            return client.get(f"/api/v1/sessions/{sid}/events").json()
        time.sleep(0.01)
    pytest.fail("Offline fixture did not become idle within five seconds")


def test_crud_workspace_archive_and_history(client):
    assert client.get("/healthz").json()["service"] == "chat-agent"
    assert client.get("/api/v1/auth/me").json()["id"] == "local"
    assert client.post("/api/v1/sessions", json={"folder": "missing"}).status_code == 400
    assert client.post("/api/v1/workspaces", json={"name": "日常"}).status_code == 200
    sid = create_session(client, folder="日常")
    client.app.state.store.append_event(sid, "user_message", {"text": "保留历史"})
    assert client.post(f"/api/v1/sessions/{sid}/rename", json={"title": "新名字"}).json()["title"] == "新名字"
    assert client.get(f"/api/v1/sessions/{sid}/events?after_seq=0").json()[0]["payload"]["text"] == "保留历史"
    assert client.get(f"/api/v1/sessions/{sid}/events?after_seq=-1").status_code == 400
    assert client.delete("/api/v1/workspaces/日常").status_code == 200
    assert client.get(f"/api/v1/sessions/{sid}").json()["folder"] == ""
    assert client.post(f"/api/v1/sessions/{sid}/archive").status_code == 200
    assert client.get("/api/v1/sessions").json()[0]["status"] == "archived"
    assert client.get(f"/api/v1/sessions/{sid}/events").json()
    assert client.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "归档"}).status_code == 409
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 200
    assert client.get(f"/api/v1/sessions/{sid}").status_code == 404


def test_removed_mode_routes_and_payloads_are_unavailable(client):
    sid = create_session(client)
    before = client.app.state.store.list_sessions()
    for value in ("cage", "classic", None):
        assert client.post("/api/v1/sessions", json={"loop_mode": value}).status_code == 422
    assert client.app.state.store.list_sessions() == before
    paths = {route.path for route in client.app.routes}
    assert "/api/v1/settings/loop-mode" not in paths
    assert "/api/v1/sessions/{sid}/loop-mode" not in paths
    assert client.get("/api/v1/settings/loop-mode").status_code == 404
    assert client.post(f"/api/v1/sessions/{sid}/loop-mode", json={"mode": "cage"}).status_code == 404
    assert "loop_mode" not in client.get(f"/api/v1/sessions/{sid}").json()
    assert all("loop_mode" not in row for row in client.get("/api/v1/sessions").json())


def test_fork_clones_attachment_artifact_and_transcript(client):
    sid = create_session(client)
    upload = client.post(f"/api/v1/sessions/{sid}/files", json={"name": "note.txt", "content_b64": base64.b64encode(b"attached").decode()}).json()
    config, store = client.app.state.config, client.app.state.store
    artifact = config.artifact_dir(sid) / "answer.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("generated", encoding="utf-8")
    store.append_event(sid, "user_message", {"text": "读附件", "attachments": [upload]})
    store.append_event(sid, "done", {"stop_reason": "end_turn"})
    fork = client.post(f"/api/v1/sessions/{sid}/fork")
    assert fork.status_code == 200, fork.text
    copied_id = fork.json()["id"]
    assert copied_id != sid
    assert client.get(f"/api/v1/sessions/{copied_id}/events").json() == store.events(sid)
    assert (config.session_dir(copied_id) / upload["path"]).read_bytes() == b"attached"
    response = client.get(f"/api/v1/sessions/{copied_id}/artifacts/answer.txt")
    assert response.status_code == 200
    assert response.text == "generated"
    assert response.headers["x-content-type-options"] == "nosniff"
    client.delete(f"/api/v1/sessions/{sid}")
    assert not config.session_dir(sid).exists()
    assert not config.artifact_dir(sid).exists()
    assert (config.session_dir(copied_id) / upload["path"]).exists()


def test_offline_rest_prompt_checkpoint_and_rewind(client):
    sid = create_session(client)
    response = client.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "hello offline"})
    assert response.status_code == 200, response.text
    events = until_idle(client, sid)
    assert any(event["type"] == "user_message" and event["payload"].get("text") == "hello offline" for event in events)
    assert any(event["type"] == "assistant_message" for event in events)
    assert any(event["type"] == "done" for event in events)
    checkpoints = client.get(f"/api/v1/sessions/{sid}/checkpoints").json()["checkpoints"]
    assert checkpoints
    response = client.post(f"/api/v1/sessions/{sid}/rewind", json={"checkpoint_id": checkpoints[0]["id"]})
    assert response.status_code == 200, response.text
    remaining = client.get(f"/api/v1/sessions/{sid}/events").json()
    assert not any(event["type"] == "user_message" for event in remaining)
    assert all(event["seq"] > events[-1]["seq"] for event in remaining)
    foreign = create_session(client)
    assert client.post(f"/api/v1/sessions/{foreign}/rewind", json={"checkpoint_id": checkpoints[0]["id"]}).status_code == 404


def test_websocket_stream_ping_and_cursor_replay(client):
    sid = create_session(client)
    seen = []
    with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream") as ws:
        assert ws.receive_json()["type"] == "session_meta"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["payload"]["pong"] is True
        ws.send_json({"type": "prompt", "text": "stream fixture"})
        for _ in range(100):
            event = ws.receive_json()
            if event["seq"]:
                seen.append(event)
            if event["type"] == "done":
                break
        else:
            pytest.fail("No completion event on the offline WebSocket stream")
    assert seen
    assert [event["seq"] for event in seen] == sorted({event["seq"] for event in seen})
    until_idle(client, sid)
    cursor = seen[0]["seq"]
    expected = client.get(f"/api/v1/sessions/{sid}/events?after_seq={cursor}").json()
    replay = []
    with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream?after_seq={cursor}") as ws:
        while True:
            event = ws.receive_json()
            if event["seq"] == 0:
                break
            replay.append(event)
    assert replay == expected


def test_subscribe_before_history_avoids_gap_and_duplicate(client, monkeypatch):
    sid = create_session(client)
    manager, store = client.app.state.manager, client.app.state.store
    original = manager.subscribe

    def race_subscribe(session_id):
        queue = original(session_id)
        event = store.append_event(session_id, "assistant_message", {"text": "during subscription"})
        queue.put_nowait(event)
        return queue

    monkeypatch.setattr(manager, "subscribe", race_subscribe)
    with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream") as ws:
        event = ws.receive_json()
        assert event["payload"]["text"] == "during subscription"
        assert ws.receive_json()["seq"] == 0
        ws.send_json({"type": "ping"})
        # The queued copy of the replayed event must be discarded before the pong.
        assert ws.receive_json()["payload"]["pong"] is True


def test_access_token_is_required_for_rest_and_websocket(tmp_path):
    app = create_app(Config(home=tmp_path / "auth", access_token="chat-private-test-token"))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 401
        assert client.get("/api/v1/auth/me", headers={"authorization": "Bearer wrong"}).status_code == 401
        headers = {"authorization": "Bearer chat-private-test-token"}
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
        sid = client.post("/api/v1/sessions", json={}, headers=headers).json()["id"]
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream"):
                pass
        assert rejected.value.code == 4401
        with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream?token=chat-private-test-token") as ws:
            assert ws.receive_json()["type"] == "session_meta"


def test_other_user_sessions_are_not_exposed(client):
    sid = client.app.state.store.create_session(user_id="other-user")["id"]
    for suffix in ("", "/events", "/checkpoints", "/context", "/artifacts/private.txt", "/approvals/pending"):
        assert client.get(f"/api/v1/sessions/{sid}{suffix}").status_code == 404
    for suffix, body in (("/fork", {}), ("/archive", {}), ("/prompt", {"text": "private"}), ("/files", {"name": "a.txt", "content_b64": "YQ=="})):
        assert client.post(f"/api/v1/sessions/{sid}{suffix}", json=body).status_code == 404
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 404
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream"):
            pass
    assert rejected.value.code == 4404


@pytest.mark.parametrize("name", ["../outside.txt", "a/b.txt", "a\\b.txt", "C:secret.txt", ".", "..", "bad\x00name"])
def test_upload_rejects_unsafe_names(client, name):
    sid = create_session(client)
    response = client.post(f"/api/v1/sessions/{sid}/files", json={"name": name, "content_b64": "YQ=="})
    assert response.status_code == 400


def test_upload_size_base64_and_artifact_traversal(tmp_path):
    app = create_app(Config(home=tmp_path / "uploads", max_upload_bytes=4))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        sid = create_session(client)
        endpoint = f"/api/v1/sessions/{sid}/files"
        assert client.post(endpoint, json={"name": "bad.txt", "content_b64": "%%%"}).status_code == 400
        assert client.post(endpoint, json={"name": "big.txt", "content_b64": base64.b64encode(b"12345").decode()}).status_code == 413
        assert client.post(endpoint, json={"name": "ok.txt", "content_b64": base64.b64encode(b"1234").decode()}).status_code == 200
        outside = app.state.config.home / "private.txt"
        outside.write_text("private", encoding="utf-8")
        for filename in ("..%2F..%2Fprivate.txt", "..%5C..%5Cprivate.txt", "C%3Aprivate.txt"):
            assert client.get(f"/api/v1/sessions/{sid}/artifacts/{filename}").status_code == 404


def test_artifact_and_upload_symlinks_stay_inside_session(client, tmp_path):
    sid = create_session(client)
    config = client.app.state.config
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    uploads = config.session_dir(sid) / "uploads"
    uploads.parent.mkdir(parents=True)
    artifact_root = config.artifact_dir(sid)
    artifact_root.mkdir(parents=True)
    try:
        uploads.symlink_to(outside, target_is_directory=True)
        (artifact_root / "secret.txt").symlink_to(outside / "secret.txt")
    except OSError:
        pytest.skip("OS does not permit creating symlinks for this test account")
    assert client.post(f"/api/v1/sessions/{sid}/files", json={"name": "hack.txt", "content_b64": "YQ=="}).status_code == 400
    assert client.get(f"/api/v1/sessions/{sid}/artifacts/secret.txt").status_code == 404
    assert sorted(path.name for path in outside.iterdir()) == ["secret.txt"]


def test_origins_and_unsupported_web_are_reported(client):
    assert client.get("/api/v1/auth/me", headers={"origin": "https://untrusted.example"}).status_code == 403
    assert client.get("/api/v1/auth/me", headers={"origin": "http://localhost:5174"}).status_code == 200
    assert client.get("/api/v1/auth/me", headers={"origin": "http://127.0.0.1"}).status_code == 200
    sid = create_session(client)
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream", headers={"origin": "https://untrusted.example"}):
            pass
    assert rejected.value.code == 4401
    assert client.get("/api/v1/web").json()["available"] is False
    assert client.post(f"/api/v1/sessions/{sid}/web", json={"enabled": True}).status_code == 400


def test_invalid_ids_are_bounded(client):
    assert client.get("/api/v1/sessions/" + "a" * 200).status_code == 400
    assert client.get("/api/v1/sessions/..%5Coutside").status_code == 400
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("ws://127.0.0.1/api/v1/sessions/invalid!/stream"):
            pass
    assert rejected.value.code == 4409


def test_backend_has_no_coding_package_imports():
    package = Path(__file__).resolve().parents[1] / "chat_agent"
    dependencies = set()
    for source in package.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                dependencies.add(node.module.split(".")[0])
    assert "code_agent" not in dependencies
    assert "ch_agent" not in dependencies


def test_busy_prompt_settings_rewind_and_cancel(client, monkeypatch):
    from chat_agent import runtime
    from chat_agent.providers import Chunk, Turn

    class BlockingProvider:
        async def stream(self, messages, tools):
            await asyncio.Event().wait()
            yield Chunk(turn=Turn(text="unreachable"))

    monkeypatch.setattr(runtime, "create_provider", lambda profile: BlockingProvider())
    sid = create_session(client)
    assert client.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "pending model"}).status_code == 200
    assert client.get(f"/api/v1/sessions/{sid}").json()["busy"] is True
    checkpoint = client.get(f"/api/v1/sessions/{sid}/checkpoints").json()["checkpoints"][0]
    for suffix, body in (("/prompt", {"text": "second prompt"}), ("/permission", {"mode": "read-only"}), ("/rewind", {"checkpoint_id": checkpoint["id"]}), ("/fork", {}), ("/archive", {}), ("/files", {"name": "busy.txt", "content_b64": "YQ=="})):
        assert client.post(f"/api/v1/sessions/{sid}{suffix}", json=body).status_code == 409
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 409
    assert client.post(f"/api/v1/sessions/{sid}/cancel").json()["cancelled"] is True
    events = until_idle(client, sid)
    assert [event["type"] for event in events].count("user_message") == 1
    assert events[-1]["type"] == "done"
    assert events[-1]["payload"]["stop_reason"] == "cancelled"
    assert client.post(f"/api/v1/sessions/{sid}/permission", json={"mode": "read-only"}).status_code == 200


def test_restart_cancels_orphan_approval_question_and_tool_once(tmp_path):
    from chat_agent.storage import Store

    config = Config(home=tmp_path / "restart")
    store = Store(config)
    sid = store.create_session()["id"]
    store.append_event(sid, "user_message", {"text": "before crash"})
    store.append_event(sid, "tool_use", {"call": {"id": "call_pending", "name": "deliver_file", "arguments": {"filename": "answer.txt", "content": "draft"}}})
    store.append_event(sid, "approval_request", {"approval_id": "approval_pending", "tool_name": "deliver_file"})
    store.append_event(sid, "question_request", {"question_id": "question_pending", "questions": [{"id": "choice", "question": "选择"}]})
    store.close()
    with TestClient(create_app(config), base_url="http://127.0.0.1") as client:
        assert client.get(f"/api/v1/sessions/{sid}").json()["busy"] is False
        events = client.get(f"/api/v1/sessions/{sid}/events").json()
        assert any(event["type"] == "approval_decided" and event["payload"].get("approved") is False for event in events)
        assert any(event["type"] == "question_answered" and event["payload"].get("status") == "cancelled" for event in events)
        assert any(event["type"] == "tool_result" and event["payload"].get("call_id") == "call_pending" for event in events)
        assert events[-1]["type"] == "done"
        assert client.get(f"/api/v1/sessions/{sid}/approvals/pending").json() is None
        assert client.post("/api/v1/approvals/approval_pending/decide", json={"approved": True}).status_code == 409
        assert client.get(f"/api/v1/sessions/{sid}/events").json() == events


def test_rewind_cannot_leave_historical_permission_cards_pending(client):
    sid = create_session(client)
    store = client.app.state.store
    store.append_event(sid, "user_message", {"text": "历史提问"})
    store.append_event(sid, "approval_request", {"approval_id": "old_approval"})
    store.append_event(sid, "question_request", {"question_id": "old_question", "questions": []})
    checkpoint = store.add_checkpoint(sid, 4)
    store.append_event(sid, "approval_decided", {"approval_id": "old_approval", "approved": False})
    store.append_event(sid, "question_answered", {"question_id": "old_question", "status": "cancelled", "answers": []})
    store.append_event(sid, "done", {"stop_reason": "cancelled"})
    client.get(f"/api/v1/sessions/{sid}")  # Cache this session as recovered.
    response = client.post(f"/api/v1/sessions/{sid}/rewind", json={"checkpoint_id": checkpoint["id"]})
    assert response.status_code == 200, response.text
    events = client.get(f"/api/v1/sessions/{sid}/events").json()
    assert any(event["type"] == "approval_decided" and event["payload"].get("approval_id") == "old_approval" for event in events)
    assert any(event["type"] == "question_answered" and event["payload"].get("question_id") == "old_question" for event in events)
    assert client.get(f"/api/v1/sessions/{sid}/approvals/pending").json() is None


def test_saved_model_credentials_never_enter_api_responses(client):
    profile = client.post("/api/v1/model-profiles", json={"name": "Test provider", "provider": "openai-compatible", "base_url": "https://models.example/v1", "model": "test-model", "api_key": "private-fixture-key"})
    assert profile.status_code == 200, profile.text
    assert "private-fixture-key" not in profile.text
    assert "private-fixture-key" not in client.get("/api/v1/model-profiles").text
    assert "private-fixture-key" not in client.get("/api/v1/settings/model").text


@pytest.mark.parametrize("message", [None, [], ["prompt"], 1, "prompt", {"type": "unsupported"}])
def test_invalid_websocket_messages_return_error_and_keep_stream_open(client, message):
    sid = create_session(client)
    with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream") as ws:
        assert ws.receive_json()["type"] == "session_meta"
        ws.send_json(message)
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "invalid_request"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["payload"]["pong"] is True


def test_malformed_websocket_json_returns_error_without_internal_exception(client):
    sid = create_session(client)
    with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream") as ws:
        assert ws.receive_json()["type"] == "session_meta"
        ws.send_text("{broken json")
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["payload"]["pong"] is True


def test_session_root_symlink_cannot_redirect_upload_or_delete(client, tmp_path):
    sid = create_session(client)
    root = client.app.state.config.session_dir(sid)
    outside = tmp_path / "external-session"
    outside.mkdir()
    (outside / "important.txt").write_text("preserve", encoding="utf-8")
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks for this test account")
    upload = client.post(f"/api/v1/sessions/{sid}/files", json={"name": "external.txt", "content_b64": "YQ=="})
    assert upload.status_code == 400
    deleted = client.delete(f"/api/v1/sessions/{sid}")
    assert deleted.status_code in {200, 400}
    assert (outside / "important.txt").read_text(encoding="utf-8") == "preserve"
    assert not (outside / "uploads").exists()


def test_artifact_root_symlink_never_serves_external_file(client, tmp_path):
    sid = create_session(client)
    root = client.app.state.config.artifact_dir(sid)
    outside = tmp_path / "external-artifacts"
    outside.mkdir()
    (outside / "private.txt").write_text("external-private-fixture", encoding="utf-8")
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks for this test account")
    response = client.get(f"/api/v1/sessions/{sid}/artifacts/private.txt")
    assert response.status_code in {400, 404}
    assert "external-private-fixture" not in response.text
    assert (outside / "private.txt").is_file()


def test_private_local_service_rejects_untrusted_host_header(client):
    assert client.get("/api/v1/auth/me", headers={"host": "untrusted.example"}).status_code == 400
    sid = create_session(client)
    with pytest.raises(WebSocketDenialResponse) as rejected:
        with client.websocket_connect(f"ws://127.0.0.1/api/v1/sessions/{sid}/stream", headers={"host": "untrusted.example"}):
            pass
    assert rejected.value.status_code == 400
