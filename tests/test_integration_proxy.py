"""Real project HTTP/preview contracts without models or user workspaces."""
from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.api.frontend_design_api import mount_frontend_design_api
from code_agent.api.integration_proxy_api import mount_integration_proxy_api
from code_agent.codeintel.frontend_design import FrontendDesignService
from code_agent.codeintel.integration_proxy import IntegrationProxy, IntegrationProxyError, MAX_REQUEST, MAX_RESPONSE, _origin_url


class Runtime:
    def __init__(self, root):
        self.root = root
        self.status = "running"
        self.session = "session-one"
        self.config = {"frontend_url": "http://localhost:54123/", "backend_url": "http://127.0.0.1:54124/base/", "api_prefix": "/api", "strip_api_prefix": False}

    def owned_project(self, pid, uid):
        if uid != "owner" or pid not in {"p", "q"}:
            raise KeyError(pid)
        return {"id": pid, "user_id": uid, "root_path": str(self.root)}

    def context(self, pid, uid, session_id=None):
        self.owned_project(pid, uid)
        session = self.session + "-" + pid
        if session_id is not None and session_id != session:
            raise KeyError(session_id)
        if self.status not in {"running", "starting", "degraded"}:
            raise IntegrationProxyError("会话已停止", 409)
        return {"session_id": session, "status": self.status, "config": dict(self.config), **({"services": self.services} if hasattr(self, "services") else {})}


@pytest.fixture
def fixture(tmp_path):
    runtime = Runtime(tmp_path)
    calls = []

    async def transport(request):
        calls.append(request)
        if request.url.port == 54123:
            return httpx.Response(200, text='<html><head><script type="module" src="/app.js"></script></head><body><h1>项目联调</h1></body></html>', headers={"content-type": "text/html"})
        assert request.url.port == 54124
        if request.url.path.endswith("/slow"):
            raise httpx.ReadTimeout("private-token-never-repeated")
        if request.url.path.endswith("/redirect"):
            return httpx.Response(302, headers={"Location": "http://outside.invalid/"})
        if request.url.path.endswith("/big"):
            return httpx.Response(200, content=b"x" * (MAX_RESPONSE + 1), headers={"content-type": "text/plain"})
        if request.url.path.endswith("/login"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"token": "response-private-token", "user": body["user"]}, headers={"Set-Cookie": "sid=" + body["user"] + "; HttpOnly; Path=/"})
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, json={"logged_out": True}, headers={"Set-Cookie": "sid=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/"})
        return httpx.Response(418 if request.url.path.endswith("/error") else 200,
                              json={"method": request.method, "path": request.url.path, "query": dict(request.url.params), "body": request.content.decode(),
                                    "authorization": request.headers.get("authorization", ""), "cookie": request.headers.get("cookie", "")}, headers={"X-Total-Count": "42"})

    mock = httpx.MockTransport(transport)
    design = FrontendDesignService(transport=mock)
    proxy = IntegrationProxy(runtime, design_service=design, transport=mock)
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("owner", "fixture", "user")
    projects = SimpleNamespace(get_owned=lambda uid, pid: runtime.owned_project(pid, uid))
    mount_frontend_design_api(app, service=projects, design=design)
    mount_integration_proxy_api(app, proxy)
    with TestClient(app) as client:
        yield SimpleNamespace(runtime=runtime, calls=calls, proxy=proxy, app=app, client=client)


def send(fixture, **payload):
    return fixture.client.post("/api/v1/code/projects/p/integration/request", json=payload)


def preview(fixture):
    response = fixture.client.post("/api/v1/code/projects/p/integration/preview", json={})
    assert response.status_code == 200, response.text
    return response.json()


def test_live_preview_needs_no_html_entry_and_keeps_design_contract(fixture):
    result = preview(fixture)
    assert result["session_id"] == "session-one-p"
    assert result["frontend_url"] == "http://127.0.0.1:54123/"
    assert result["api_proxy_url"] in result["html"]
    assert result["asset_prefix"] in result["html"]
    assert not result["hmr"]
    assert fixture.client.get(result["asset_prefix"] + "app.js").status_code == 200
    assert not list(fixture.runtime.root.iterdir()), "Preview must never create an index.html or rewrite a project"


def test_service_urls_keep_verified_https_and_frontend_query_contract():
    assert _origin_url("https://localhost:54123/app/?locale=zh", set(), base=False) == "https://127.0.0.1:54123/app/?locale=zh"
    assert _origin_url("https://127.0.0.1/backend", set()) == "https://127.0.0.1:443/backend/"
    with pytest.raises(IntegrationProxyError):
        _origin_url("http://127.0.0.1:54124/?access=secret", set())


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_manual_methods_preserve_path_status_body_and_timing(fixture, method):
    response = send(fixture, method=method, path="/admin/items?name=测试", body=None if method in {"GET", "HEAD"} else {"value": 42})
    assert response.status_code == 200
    result = response.json()
    assert result["method"] == method and result["status_code"] == 200
    assert result["source"] == "manual" and result["duration_ms"] >= 0
    assert fixture.calls[-1].url.path == "/base/admin/items"
    assert fixture.calls[-1].url.params["name"] == "测试"
    if method not in {"GET", "HEAD"}:
        assert json.loads(fixture.calls[-1].content) == {"value": 42}


def test_capability_frontend_mapping_prefix_and_absolute_backend_marker(fixture):
    result = preview(fixture)
    endpoint = result["api_proxy_url"]
    response = fixture.client.post(endpoint + "api/items", content='{"label":"one"}', headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert response.headers["x-total-count"] == "42"
    assert "x-total-count" in response.headers["access-control-expose-headers"].lower()
    assert response.headers["x-code-agent-request-id"]
    assert fixture.calls[-1].url.path == "/base/api/items"
    fixture.client.get(endpoint + "__backend__/admin/items")
    assert fixture.calls[-1].url.path == "/base/admin/items"
    fixture.runtime.config["strip_api_prefix"] = True
    changed = preview(fixture)
    fixture.client.get(changed["api_proxy_url"] + "api/items")
    assert fixture.calls[-1].url.path == "/base/items"
    assert fixture.client.get(endpoint + "api/items").status_code == 410


def test_project_credentials_are_explicit_and_app_auth_never_forwards(fixture):
    result = preview(fixture)
    fixture.client.cookies.set("platform_session", "private-platform-cookie")
    first = fixture.client.get(result["api_proxy_url"] + "api/items", headers={"Authorization": "Bearer platform-secret", "X-Api-Key": "platform-secret"})
    assert first.status_code == 200
    assert "authorization" not in fixture.calls[-1].headers and "cookie" not in fixture.calls[-1].headers
    assert "x-api-key" not in fixture.calls[-1].headers
    explicit = quote(json.dumps({"Authorization": "Bearer project-only", "X-Label": "中文"}), safe="")
    second = fixture.client.get(result["api_proxy_url"] + "api/items", headers={"X-Code-Agent-Project-Headers": explicit})
    assert second.status_code == 200
    assert fixture.calls[-1].headers["authorization"] == "Bearer project-only"
    assert fixture.calls[-1].headers["x-label"] == "中文"


def test_cookie_jar_isolates_project_and_restarted_session(fixture):
    send(fixture, method="POST", path="login", body={"user": "alice"})
    send(fixture, path="whoami")
    assert fixture.calls[-1].headers["cookie"] == "sid=alice"
    fixture.client.post("/api/v1/code/projects/q/integration/request", json={"path": "whoami"})
    assert "cookie" not in fixture.calls[-1].headers
    fixture.runtime.session = "session-two"
    send(fixture, path="whoami")
    assert "cookie" not in fixture.calls[-1].headers


def test_cookie_logout_clears_backend_session_and_history_does_not_store_cookie(fixture):
    send(fixture, method="POST", path="login", body={"user": "alice"})
    send(fixture, method="POST", path="logout")
    send(fixture, path="whoami")
    assert "cookie" not in fixture.calls[-1].headers


def test_role_detach_stops_preview_and_requests_without_stopping_other_role(fixture):
    fixture.runtime.services = {"frontend": {"status": "running"}, "backend": {"status": "running"}}
    endpoint = preview(fixture)["api_proxy_url"]
    fixture.runtime.services["backend"]["status"] = "stopped"
    before = len(fixture.calls)
    assert send(fixture, path="items").status_code == 409
    assert fixture.client.get(endpoint + "api/items").status_code == 410
    assert len(fixture.calls) == before
    fixture.runtime.services["backend"]["status"] = "running"
    fixture.runtime.services["frontend"]["status"] = "stopped"
    assert send(fixture, path="items").status_code == 200
    assert fixture.client.post("/api/v1/code/projects/p/integration/preview", json={}).status_code == 409


def test_unhealthy_probe_does_not_revoke_active_preview_or_hide_backend_error(fixture):
    fixture.runtime.status = "degraded"
    fixture.runtime.services = {"frontend": {"status": "unhealthy"}, "backend": {"status": "unhealthy"}}
    endpoint = preview(fixture)["api_proxy_url"]
    preflight = fixture.client.options(endpoint + "api/error", headers={"Access-Control-Request-Method": "GET"})
    assert preflight.status_code == 204
    response = fixture.client.get(endpoint + "api/error")
    assert response.status_code == 418
    assert fixture.calls[-1].url.path == "/base/api/error"
    manual = send(fixture, path="error").json()
    assert manual["status_code"] == 418
    assert fixture.runtime.services["backend"]["status"] == "unhealthy", "Requests must not rewrite health evidence"
    fixture.runtime.services["backend"]["status"] = "stopped"
    before = len(fixture.calls)
    assert fixture.client.options(endpoint + "api/error", headers={"Access-Control-Request-Method": "GET"}).status_code == 410
    assert fixture.client.get(endpoint + "api/error").status_code == 410
    assert len(fixture.calls) == before


def test_queued_write_cannot_start_after_session_stop(tmp_path):
    runtime = Runtime(tmp_path)
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})
    proxy = IntegrationProxy(runtime, transport=httpx.MockTransport(handler))
    async def exercise():
        proxy._slots = asyncio.Semaphore(0)
        operation = asyncio.create_task(proxy.request("p", "owner", {"method": "POST", "path": "items", "body": "write"}))
        await asyncio.sleep(.05)
        runtime.status = "stopped"
        proxy._slots.release()
        with pytest.raises(IntegrationProxyError):
            await operation
        assert calls == []
    asyncio.run(exercise())


def test_history_is_bounded_and_old_details_expire(fixture, monkeypatch):
    from code_agent.codeintel import integration_proxy
    monkeypatch.setattr(integration_proxy, "MAX_HISTORY", 3)
    first = send(fixture, path="items").json()
    for _ in range(4):
        send(fixture, path="items")
    rows = fixture.client.get("/api/v1/code/projects/p/integration/requests").json()["requests"]
    assert len(rows) == 3
    assert fixture.client.get("/api/v1/code/projects/p/integration/requests/" + first["id"]).status_code == 404


def test_history_detail_redacts_secrets_but_keeps_results_after_stop(fixture):
    result = send(fixture, method="POST", path="login?access_token=url-secret", headers={"Authorization": "Bearer header-secret"}, body={"user": "alice", "password": "body-secret"}).json()
    assert result["status_code"] == 200
    fixture.runtime.status = "stopped"
    history = fixture.client.get("/api/v1/code/projects/p/integration/requests")
    assert history.status_code == 200 and history.json()["requests"][0]["id"] == result["id"]
    detail = fixture.client.get("/api/v1/code/projects/p/integration/requests/" + result["id"])
    assert detail.status_code == 200
    encoded = detail.text
    assert all(secret not in encoded for secret in ("url-secret", "header-secret", "body-secret", "response-private-token", "sid=alice"))
    assert "[已隐藏]" in encoded and "alice" in encoded
    assert fixture.client.get("/api/v1/code/projects/q/integration/requests/" + result["id"]).status_code == 404
    fixture.app.dependency_overrides[get_current_user] = lambda: CurrentUser("intruder", "other", "user")
    assert fixture.client.get("/api/v1/code/projects/p/integration/requests").status_code == 404


def test_stop_expiration_and_session_replacement_invalidate_ticket(fixture):
    endpoint = preview(fixture)["api_proxy_url"]
    fixture.runtime.status = "stopping"
    before = len(fixture.calls)
    assert fixture.client.post(endpoint + "api/items", content="write").status_code == 410
    assert len(fixture.calls) == before
    fixture.runtime.status = "running"
    fixture.runtime.session = "replacement"
    assert fixture.client.get(endpoint + "api/items").status_code == 410
    fixture.proxy.clock = lambda: 10**20
    fresh = preview(fixture)["api_proxy_url"]
    fixture.proxy.clock = lambda: 10**21
    assert fixture.client.get(fresh + "api/items").status_code == 410


@pytest.mark.parametrize("path", ["http://outside.invalid/", "//outside.invalid/", "../escape", "foo/%2e%2e/escape", "foo/%252e%252e/escape", "foo\\bar", "foo#fragment"])
def test_path_cannot_escape_fixed_backend(fixture, path):
    before = len(fixture.calls)
    assert send(fixture, path=path).status_code == 422
    assert len(fixture.calls) == before


@pytest.mark.parametrize("url", ["http://external.invalid/", "http://169.254.169.254/", "http://127.0.0.1:8765/", "http://127.0.0.1:5175/", "http://user:pass@localhost:54124/", "http://localhost:54124/base?token=x", "http://localhost:54124/%2e%2e/", "ftp://localhost:54124/"])
def test_backend_url_revalidation(url):
    with pytest.raises(IntegrationProxyError):
        _origin_url(url, {8765, 5175})


def test_status_redirect_timeout_and_response_bound(fixture):
    assert send(fixture, path="error").json()["status_code"] == 418
    redirect = send(fixture, path="redirect").json()
    assert redirect["status_code"] == 302 and redirect["error_kind"] == "redirect"
    assert all(call.url.host != "outside.invalid" for call in fixture.calls)
    timeout = send(fixture, path="slow").json()
    assert timeout["error_kind"] == "timeout" and "private-token" not in json.dumps(timeout)
    oversized = send(fixture, path="big").json()
    assert oversized["truncated"] and oversized["error_kind"] == "size"
    assert len(oversized["body"]) <= MAX_RESPONSE
    endpoint = preview(fixture)["api_proxy_url"]
    assert fixture.client.get(endpoint + "api/redirect").status_code == 502
    assert fixture.client.get(endpoint + "api/slow").status_code == 504
    assert fixture.client.get(endpoint + "api/big").status_code == 413


def test_request_limits_and_preflight_are_bounded(fixture):
    assert send(fixture, method="TRACE", path="items").status_code == 422
    assert send(fixture, method="GET", path="items", body="not-empty").status_code == 422
    assert send(fixture, path="items", headers={"X-Name": "bad\r\nInjected: value"}).status_code == 422
    assert send(fixture, path="items", timeout=31).status_code == 422
    assert send(fixture, method="POST", path="items", body="x" * (MAX_REQUEST + 1)).status_code == 413
    endpoint = preview(fixture)["api_proxy_url"]
    before = len(fixture.calls)
    response = fixture.client.options(endpoint + "api/items", headers={"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-code-agent-project-headers"})
    assert response.status_code == 204 and response.headers["access-control-allow-origin"] == "*"
    assert len(fixture.calls) == before
    assert fixture.client.post(endpoint + "api/items", content=b"x" * (MAX_REQUEST + 1)).status_code == 413


def test_real_http_crud_and_login(tmp_path):
    rows = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.answer(200, {"items": list(rows.values()), "cookie": self.headers.get("Cookie", "")})
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            rows["one"] = data
            self.answer(201, {"id": "one", **data}, cookie="sid=real-project; HttpOnly; Path=/")
        def do_PATCH(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            rows["one"].update(data)
            self.answer(200, rows["one"])
        def do_DELETE(self):
            rows.pop("one", None)
            self.answer(200, {"deleted": True})
        def answer(self, status, data, cookie=""):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = Runtime(tmp_path)
    runtime.config["backend_url"] = f"http://127.0.0.1:{server.server_port}/"
    proxy = IntegrationProxy(runtime)
    async def exercise():
        async def request(**body):
            return await proxy.request("p", "owner", body)
        created = await request(path="admin/items", method="POST", body={"name": "first"})
        assert created["status_code"] == 201
        loaded = await request(path="admin/items")
        assert json.loads(loaded["body"])["cookie"] == "sid=real-project"
        changed = await request(path="admin/items/one", method="PATCH", body={"name": "second"})
        assert json.loads(changed["body"])["name"] == "second"
        deleted = await request(path="admin/items/one", method="DELETE")
        assert deleted["status_code"] == 200 and rows == {}
        assert len((await proxy.requests("p", "owner"))["requests"]) == 4
    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
