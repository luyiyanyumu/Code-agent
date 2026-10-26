"""Packaged UI behavior, API isolation, and installed-server boot ordering."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from code_agent import __version__
from code_agent.api.static_web import mount_workbench


@pytest.fixture
def web_dist(tmp_path, monkeypatch):
    directory = tmp_path / "web dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<html>Packaged workbench</html>", encoding="utf-8")
    (directory / "assets" / "app-abc12345.js").write_text("console.log('packaged')", encoding="utf-8")
    (directory / "brand.svg").write_text("<svg></svg>", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_WEB_DIST", str(directory))
    return directory


@pytest.fixture
def client(web_dist):
    app = FastAPI()

    @app.get("/api/v1/example")
    def example():
        return {"ok": True}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.websocket("/api/v1/example/stream")
    async def stream(socket: WebSocket):
        await socket.accept()
        await socket.send_text("connected")
        await socket.close()

    mount_workbench(app)
    with TestClient(app) as browser:
        yield browser


@pytest.mark.parametrize("path", ["/", "/index.html", "/projects/demo/editor"])
def test_ui_navigation_never_caches_index(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "Packaged workbench" in response.text
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"


def test_hashed_assets_are_immutable_but_unhashed_files_revalidate(client):
    asset = client.get("/assets/app-abc12345.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text
    assert "javascript" in asset.headers["content-type"]
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/brand.svg").headers["cache-control"] == "no-cache"
    head = client.head("/index.html")
    assert head.status_code == 200 and not head.content
    assert head.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/missing"),
    ("POST", "/api/v1/missing"),
    ("GET", "/api"),
    ("GET", "/assets/missing.js"),
    ("GET", "/assets/missing"),
    ("GET", "/missing.css"),
    ("GET", "/docs/missing"),
    ("GET", "/healthz/missing"),
])
def test_missing_apis_and_assets_keep_404(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_json_requests_and_unsupported_methods_do_not_receive_spa(client):
    assert client.get("/missing", headers={"accept": "application/json"}).status_code == 404
    assert client.post("/projects/demo/editor").status_code == 405


@pytest.mark.parametrize("path", [
    "/%2e%2e/outside.txt",
    "/assets/%2e%2e/index.html",
    "/assets/%5c..%5coutside.txt",
    "/%00",
])
def test_path_traversal_is_rejected_before_normalization(client, web_dist, path):
    (web_dist.parent / "outside.txt").write_text("private", encoding="utf-8")
    response = client.get(path)
    assert response.status_code == 404
    assert "private" not in response.text
    assert "Packaged workbench" not in response.text


def test_symlinks_cannot_escape_static_directory(client, web_dist):
    outside = web_dist.parent / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        (web_dist / "outside.txt").symlink_to(outside)
        (web_dist / "outside").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host")
    assert client.get("/outside.txt").status_code == 404
    assert client.get("/outside").status_code == 404


def test_http_api_and_websockets_precede_static_mount(client):
    assert client.get("/api/v1/example").json() == {"ok": True}
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/openapi.json").json()["openapi"]
    assert client.get("/docs").status_code == 200
    with client.websocket_connect("/api/v1/example/stream") as stream:
        assert stream.receive_text() == "connected"
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect("/api/v1/missing/stream"):
            pass
    assert disconnected.value.code == 1000


def test_api_only_default_and_explicit_invalid_dist(tmp_path, monkeypatch):
    monkeypatch.delenv("CODE_AGENT_WEB_DIST", raising=False)
    app = FastAPI()
    mount_workbench(app)
    assert TestClient(app).get("/").status_code == 404
    monkeypatch.setenv("CODE_AGENT_WEB_DIST", str(tmp_path))
    with pytest.raises(RuntimeError, match="CODE_AGENT_WEB_DIST must contain"):
        mount_workbench(app)


def test_reserved_paths_remain_protected_under_an_asgi_prefix(web_dist):
    inner = FastAPI()
    mount_workbench(inner)
    outer = FastAPI()
    outer.mount("/workbench", inner)
    browser = TestClient(outer)
    assert browser.get("/workbench/projects/demo").status_code == 200
    assert browser.get("/workbench/api/v1/missing").status_code == 404


def test_real_application_mount_is_opt_in(agent_home, web_dist, monkeypatch):
    from code_agent.api.app import create_app

    app = create_app(provider_name="offline")
    browser = TestClient(app)
    assert browser.get("/").status_code == 200
    assert browser.get("/healthz").json() == {"ok": True, "version": __version__}
    assert browser.get("/api/v1/does-not-exist").status_code == 404
    assert "/api/v1/sessions" in browser.get("/openapi.json").json()["paths"]
    monkeypatch.delenv("CODE_AGENT_WEB_DIST")
    assert TestClient(create_app(provider_name="offline")).get("/").status_code == 404


def test_server_loads_dotenv_before_importing_runtime(tmp_path):
    home = tmp_path / "user data"
    (tmp_path / ".env").write_text(
        "CODE_AGENT_FAILURE_STREAK=17\nCODE_AGENT_PROVIDER=offline\n"
        f"CODE_AGENT_HOME={home.as_posix()}\nCODE_AGENT_AUTH_ENABLED=false\n",
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CODE_AGENT_", "CH_AGENT_"))}
    environment["CODE_AGENT_SKIP_MIGRATION"] = "1"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    script = """
import json, sys
import code_agent.server as server
assert 'code_agent.api.app' not in sys.modules
def capture(app, **options):
    from code_agent.agent.breaker import DEFAULT_STREAK
    assert DEFAULT_STREAK == 17
    assert app.state.router.provider_name == 'offline'
    print('RESULT=' + json.dumps({'version': app.version, **options}))
server.uvicorn.run = capture
sys.argv = ['code-agent-server', '--port', '18966']
server.main()
"""
    process = subprocess.run([sys.executable, "-c", script], cwd=tmp_path,
                             env=environment, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=40)
    assert process.returncode == 0, process.stderr
    result = json.loads(next(line.removeprefix("RESULT=") for line in process.stdout.splitlines()
                             if line.startswith("RESULT=")))
    assert result == {"version": __version__, "host": "127.0.0.1", "port": 18966, "log_level": "info"}
    assert (home / "code_agent.db").is_file()
