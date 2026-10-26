"""WebSocket 流式通道测试：prompt → 实时事件流 → done；审批 → REST 决策 → 恢复。"""

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.providers.offline import OfflineProvider


@pytest.fixture
def client(agent_home):
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["token"]


def _recv_until(ws, types, collect, payload_key=None):
    """持续接收直到收到指定类型（可选：要求 payload 含某键）。"""
    while True:
        ev = ws.receive_json()
        collect.append(ev)
        if ev["type"] in types:
            if payload_key is not None and payload_key not in ev.get("payload", {}):
                continue
            return ev


def test_ws_prompt_flow(client):
    token = _login(client)
    h = {"Authorization": f"Bearer {token}"}
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    with client.websocket_connect(f"/api/v1/sessions/{sid}/stream?token={token}") as ws:
        ws.receive_json()  # session_meta
        ws.send_json({"type": "prompt", "text": "跑个 echo"})
        events = []
        _recv_until(ws, {"done"}, events)

    types = [e["type"] for e in events]
    assert "user_message" in types
    assert "tool_use" in types
    assert "tool_result" in types
    assert events[-1]["type"] == "done"
    # 实时流里的事件与轨迹一致（seq 递增）
    seqs = [e["seq"] for e in events if e["seq"] > 0]
    assert seqs == sorted(seqs)


def test_ws_approval_via_rest(client):
    """非白名单命令 -> approval_request(带 id) -> REST 批准 -> 执行 -> done。"""
    token = _login(client)
    h = {"Authorization": f"Bearer {token}"}
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    # 覆盖会话的 provider 为脚本：白名单外命令 + 收口
    from code_agent.api.security import hash_password

    user = client.app.state.users.get_by_username("admin")
    runtime = client.app.state.sessions.get_runtime(sid, user["id"])
    runtime.router.provider_override = OfflineProvider(
        script=[
            OfflineProvider.tool_call_turn("bash", {
                "command": "make all",
                "_approval": {"purpose": "构建当前项目", "effects": ["在工作区执行 make all 构建命令"]},
            }),
            OfflineProvider.text_turn("approved run done"),
        ]
    )

    with client.websocket_connect(f"/api/v1/sessions/{sid}/stream?token={token}") as ws:
        ws.receive_json()  # session_meta
        ws.send_json({"type": "prompt", "text": "构建项目"})
        events = []
        req = _recv_until(ws, {"approval_request"}, events, payload_key="approval_id")
        approval_id = req["payload"]["approval_id"]
        assert req["payload"]["tool"] == "bash"

        # REST 决策（模拟另一个客户端/管理员批准）
        r = client.post(
            f"/api/v1/approvals/{approval_id}/decide",
            json={"approved": True},
            headers=h,
        )
        assert r.status_code == 200
        assert r.json()["delivered"] is True

        # 收 approval_decided（loop 发的轨迹事件）+ tool_result + done
        _recv_until(ws, {"tool_result"}, events)
        done = _recv_until(ws, {"done"}, events)

    assert "approved run done" in done["payload"]["text"]
    # DB 里审批记录已闭合
    approvals = client.app.state.approvals.pending_for_session(sid)
    assert approvals == []


def test_ws_denied_approval_fail_path(client):
    token = _login(client)
    h = {"Authorization": f"Bearer {token}"}
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    from code_agent.api.security import hash_password

    user = client.app.state.users.get_by_username("admin")
    runtime = client.app.state.sessions.get_runtime(sid, user["id"])
    runtime.router.provider_override = OfflineProvider(
        script=[
            OfflineProvider.tool_call_turn("bash", {
                "command": "make all",
                "_approval": {"purpose": "构建当前项目", "effects": ["在工作区执行 make all 构建命令"]},
            }),
            OfflineProvider.text_turn("after deny"),
        ]
    )

    with client.websocket_connect(f"/api/v1/sessions/{sid}/stream?token={token}") as ws:
        ws.receive_json()
        ws.send_json({"type": "prompt", "text": "构建项目"})
        events = []
        req = _recv_until(ws, {"approval_request"}, events, payload_key="approval_id")
        client.post(
            f"/api/v1/approvals/{req['payload']['approval_id']}/decide",
            json={"approved": False},
            headers=h,
        )
        _recv_until(ws, {"done"}, events)

    results = [e for e in events if e["type"] == "tool_result"]
    assert results and "denied" in results[0]["payload"]["result"]


def test_ws_unauthorized(client):
    # 无 token 直接拒
    with pytest.raises(Exception):
        with client.websocket_connect("/api/v1/sessions/whatever/stream") as ws:
            ws.receive_json()


def test_ws_idle_timeout_disconnects(monkeypatch, agent_home):
    """M6 超时兜底：无事件无消息空闲超过阈值，服务端主动断开，不无限挂起。"""
    # 用极短空闲超时，让测试不等待 120s
    monkeypatch.setenv("CODE_AGENT_WS_IDLE_TIMEOUT", "1")
    app = create_app(provider_name="offline")
    with TestClient(app) as client:
        token = _login(client)
        h = {"Authorization": f"Bearer {token}"}
        sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

        # 连接后不发送任何消息，静置
        with client.websocket_connect(f"/api/v1/sessions/{sid}/stream?token={token}") as ws:
            ws.receive_json()  # session_meta
            # 空闲超时后服务端主动断开：receive_json 抛出断开异常而非一直阻塞
            import time

            start = time.time()
            disconnected = False
            try:
                while True:
                    ws.receive_json()
            except Exception:
                disconnected = True  # 服务端关闭连接
            # 应在 1s 超时 + 心跳余量内断开，不会无限挂起
            elapsed = time.time() - start
            assert disconnected is True
            assert elapsed < 10, f"WS 未按时断开，耗时 {elapsed:.1f}s（可能挂起）"


def test_ws_context_preparation_allows_ping_reconnect_and_cancel(client):
    """A blocked map query must leave the socket and stop action responsive."""
    import threading

    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    sid = client.post("/api/v1/sessions", json={}, headers=headers).json()["id"]
    user = client.app.state.users.get_by_username("admin")
    runtime = client.app.state.sessions.get_runtime(sid, user["id"])
    provider = OfflineProvider(script=[OfflineProvider.text_turn("finished")])
    runtime.router.provider_override = provider
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def context():
        entered.set()
        try:
            assert release.wait(5), "test failed to release map worker"
            return 1, "project map"
        finally:
            finished.set()

    runtime.code_context_provider = context
    url = f"/api/v1/sessions/{sid}/stream?token={token}"
    try:
        with client.websocket_connect(url) as ws:
            initial = ws.receive_json()
            assert initial["payload"]["phase"] == ""
            ws.send_json({"type": "prompt", "text": "hello"})
            assert entered.wait(2), "context must start without occupying the socket loop"
            ws.send_json({"type": "ping"})
            pong = _recv_until(ws, {"session_meta"}, [], payload_key="pong")
            assert pong["payload"] == {"pong": True, "busy": True, "phase": "preparing_context"}
            assert provider.calls == 0
            with client.websocket_connect(url) as reconnected:
                snapshot = _recv_until(reconnected, {"session_meta"}, [], payload_key="session_id")
                assert snapshot["payload"]["busy"] is True
                assert snapshot["payload"]["phase"] == "preparing_context"
                reconnected.send_json({"type": "cancel"})
                while True:
                    event = reconnected.receive_json()
                    if event["type"] == "session_meta" and event["payload"].get("busy") is False:
                        assert event["payload"]["phase"] == ""
                        break
            assert provider.calls == 0
    finally:
        release.set()
        assert finished.wait(2)


def test_context_usage_query_does_not_block_socket_loop(client):
    """The synchronous context endpoint must stay in FastAPI's worker pool."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    sid = client.post("/api/v1/sessions", json={}, headers=headers).json()["id"]
    user = client.app.state.users.get_by_username("admin")
    runtime = client.app.state.sessions.get_runtime(sid, user["id"])
    entered, release = threading.Event(), threading.Event()

    def context():
        entered.set()
        assert release.wait(5), "test failed to release context estimate worker"
        return 1, "project map"

    runtime.code_context_provider = context
    with ThreadPoolExecutor(max_workers=1) as executor:
        with client.websocket_connect(f"/api/v1/sessions/{sid}/stream?token={token}") as ws:
            ws.receive_json()
            pending = executor.submit(client.get, f"/api/v1/sessions/{sid}/context", headers=headers)
            try:
                assert entered.wait(2)
                ws.send_json({"type": "ping"})
                pong = _recv_until(ws, {"session_meta"}, [], payload_key="pong")
                assert pong["payload"] == {"pong": True, "busy": False, "phase": ""}
            finally:
                release.set()
            assert pending.result(timeout=2).status_code == 200
