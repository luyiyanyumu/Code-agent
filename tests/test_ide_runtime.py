"""Real fixture processes only; no registered user projects, indexing or models."""
import os
import sys
import time
import venv
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from code_agent.api.deps import CurrentUser
from code_agent.api.runtime_api import mount_runtime_api
from code_agent.api.security import create_token
from code_agent.codeintel import runtime
from code_agent.codeintel.runtime import IdeRuntime, RuntimeBusy


class Projects:
    def __init__(self, root):
        self.rows = {"p": {"id": "p", "user_id": "owner", "root_path": str(root)},
                     "q": {"id": "q", "user_id": "owner", "root_path": str(root)}}

    def get(self, id):
        return self.rows.get(id)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "fixture-project"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "hello.py").write_text("print('真实输出 😀', flush=True)\nprint('\\x1b[31mfixture-ansi\\x1b[0m', flush=True)\n", encoding="utf-8")
    (root / "hello.js").write_text("console.log('fixture-node')", encoding="utf-8")
    (root / "package.json").write_text('{"scripts":{"test":"node hello.js","dev":"node hello.js"}}', encoding="utf-8")
    (root / "test_small.py").write_text("def test_pass():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.py").write_text("raise RuntimeError('must not run')", encoding="utf-8")
    return root


@pytest.fixture
def service(project):
    service = IdeRuntime(Projects(project))
    try:
        yield service
    finally:
        service.close()


@pytest.fixture
def api(project):
    app = FastAPI()
    app.state.auth_enabled = False
    app.state.local_user = CurrentUser("owner", "owner", "admin")
    service = mount_runtime_api(app, Projects(project))
    with TestClient(app) as client:
        try:
            yield client, service
        finally:
            service.close()


def wait_done(service, id, category="run", timeout=15):
    record = service._record("p", "owner", id, category)
    assert record.finished.wait(timeout), "fixture process did not exit"
    return service.snapshot("p", "owner", id, category)


def wait_text(service, id, text, category="run", timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = service.snapshot("p", "owner", id, category)
        if text in "".join(event.get("data", "") for event in result["events"]):
            return result
        time.sleep(0.03)
    pytest.fail(f"fixture output never contained {text!r}")


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        # Linux zombies have terminated and cannot retain a pipe.
        stat = Path(f"/proc/{pid}/stat")
        return not stat.exists() or stat.read_text().split()[2] != "Z"
    except ProcessLookupError:
        return False


def test_configs_are_read_only_and_skip_artifacts(service, monkeypatch):
    from code_agent.codeintel import parsers
    from code_agent.codeintel.service import CodeIntelService
    from code_agent.codeintel.store import SymbolIndex

    def forbidden(*args, **kwargs):
        raise AssertionError("discovery must not spawn, scan index, parse or call models")
    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden)
    monkeypatch.setattr(CodeIntelService, "scan", forbidden)
    monkeypatch.setattr(CodeIntelService, "ensure_indexed", forbidden)
    monkeypatch.setattr(SymbolIndex, "__init__", forbidden)
    monkeypatch.setattr(parsers, "parse_file", forbidden)
    result = service.run_configs("p", "owner")
    configs = result["configs"]
    assert {item["kind"] for item in configs} == {"python", "node", "npm", "pytest"}
    assert {item["script"] for item in configs if item["kind"] == "npm"} == {"test", "dev"}
    assert all("node_modules" not in item.get("path", "") for item in configs)
    assert service.list("p", "owner", "run")["runs"] == []


def test_real_python_unicode_exit_and_cursor(service):
    created = service.create_run("p", "owner", kind="python", path="hello.py")
    result = wait_done(service, created["id"])
    assert result["status"] == "exited" and result["exit_code"] == 0
    output = "".join(event.get("data", "") for event in result["events"])
    assert "真实输出 😀" in output and "\x1b[31mfixture-ansi\x1b[0m" in output
    assert result["events"][-1] == {"type": "exit", "code": 0, "seq": result["next_seq"]}
    assert service.snapshot("p", "owner", created["id"], "run", result["next_seq"])["events"] == []
    assert created["command"][:2] == [sys.executable, "-u"]
    assert not alive(created["pid"])


def test_pytest_fixture_real_run(service):
    result = wait_done(service, service.create_run("p", "owner", kind="pytest", path="test_small.py")["id"])
    assert result["exit_code"] == 0
    assert "1 passed" in "".join(event.get("data", "") for event in result["events"])


def test_real_command_exit_code(service):
    command = "Write-Output 'fixture-command 中文'; Write-Output ([char]27 + '[31mANSI[0m'); exit 7" if os.name == "nt" else "printf 'fixture-command 中文\\n\\033[31mANSI[0m\\n'; exit 7"
    result = wait_done(service, service.create_run("p", "owner", kind="command", command=command)["id"])
    assert result["exit_code"] == 7
    text = "".join(event.get("data", "") for event in result["events"])
    assert "fixture-command 中文" in text and "ANSI[0m" in text


def test_node_and_npm_fixture_when_available(service):
    if not runtime.shutil.which("node") or not runtime.shutil.which("npm"):
        pytest.skip("Node/npm not installed")
    for kwargs in ({"kind": "node", "path": "hello.js"}, {"kind": "npm", "script": "test"}):
        result = wait_done(service, service.create_run("p", "owner", **kwargs)["id"])
        assert result["exit_code"] == 0
        assert "fixture-node" in "".join(event.get("data", "") for event in result["events"])


def test_stop_and_shutdown_kill_whole_tree(service, project):
    (project / "tree.py").write_text(
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "print('tree-ready', flush=True)\n"
        "time.sleep(60)\n", encoding="utf-8")
    for closing in (False, True):
        created = service.create_run("p", "owner", kind="python", path="tree.py")
        wait_text(service, created["id"], "tree-ready")
        child = int((project / "child.pid").read_text())
        assert alive(child)
        if closing:
            service.close()
        else:
            service.stop("p", "owner", created["id"], "run")
        result = wait_done(service, created["id"])
        assert result["status"] == "stopped"
        assert not alive(child) and not alive(created["pid"])
        assert not service._records[created["id"]].reader.is_alive()


def test_parent_exit_closes_remaining_child_pipe(service, project):
    (project / "orphan.py").write_text(
        "import subprocess, sys, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "print('parent-exit', flush=True)\n", encoding="utf-8")
    result = wait_done(service, service.create_run("p", "owner", kind="python", path="orphan.py")["id"])
    assert result["exit_code"] == 0
    assert not alive(int((project / "child.pid").read_text()))


def test_output_buffer_reports_dropped_history(project):
    service = IdeRuntime(Projects(project), max_output_bytes=128, max_output_events=5)
    (project / "noisy.py").write_text("print('x' * 20000)\n", encoding="utf-8")
    try:
        result = wait_done(service, service.create_run("p", "owner", kind="python", path="noisy.py")["id"])
        assert result["truncated"] and result["first_seq"] > 1
        assert len(result["events"]) <= 5
        assert sum(len(event.get("data", "").encode()) for event in result["events"]) <= 128
        assert result["events"][-1]["type"] == "exit"
    finally:
        service.close()


def test_true_pty_input_resize_ws_and_replay(api):
    client, service = api
    info = client.get("/api/v1/code/projects/p/terminals").json()
    if not info["available"]:
        pytest.skip(info["reason"])
    response = client.post("/api/v1/code/projects/p/terminals", json={"cols": 90, "rows": 30})
    assert response.status_code == 201, response.text
    created = response.json()
    id = created["id"]
    marker_command = "Write-Output 'pty-fixture 中文'; exit 0\r" if os.name == "nt" else "printf 'pty-fixture 中文\\n'; exit 0\n"
    with client.websocket_connect(f"/api/v1/code/projects/p/terminals/{id}/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "resize", "cols": 100, "rows": 35})
        ws.send_json({"type": "input", "data": marker_command})
        result = wait_done(service, id, "terminal")
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "exit":
                break
    assert result["status"] == "exited" and result["exit_code"] == 0
    assert result["cols"] == 100 and result["rows"] == 35
    assert "pty-fixture 中文" in "".join(frame.get("data", "") for frame in frames)
    with client.websocket_connect(f"/api/v1/code/projects/p/terminals/{id}/stream?after_seq={result['next_seq'] - 1}") as ws:
        assert ws.receive_json()["type"] == "ready"
        assert ws.receive_json() == result["events"][-1]
    assert not alive(created["pid"])


def test_terminal_close_cleans_reader(service):
    if not runtime.terminal_capabilities()["available"]:
        pytest.skip("PTY dependency unavailable")
    created = service.create_terminal("p", "owner")
    result = service.stop("p", "owner", created["id"], "terminal")
    assert result["status"] == "closed"
    assert not alive(created["pid"])
    assert not service._records[created["id"]].reader.is_alive()


def test_missing_pty_is_explicit_and_does_not_spawn(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(runtime, "terminal_capabilities", lambda: {"available": False, "backend": "conpty", "reason": "missing pywinpty", "shells": []})
    assert client.get("/api/v1/code/projects/p/terminals").json()["available"] is False
    result = client.post("/api/v1/code/projects/p/terminals", json={})
    assert result.status_code == 503 and "pywinpty" in result.text


@pytest.mark.parametrize("body", [
    {"kind": "unknown"}, {"kind": "command", "command": ""},
    {"kind": "python", "path": "../hello.py"}, {"kind": "python", "path": "missing.py"},
    {"kind": "python", "path": "hello.py", "cwd": "../"},
    {"kind": "npm", "script": "not-a-script"},
])
def test_bad_run_parameters(api, body):
    client, _ = api
    assert client.post("/api/v1/code/projects/p/runs", json=body).status_code == 422
    assert client.get("/api/v1/code/projects/p/runs?after_seq=-1").status_code == 200


def test_api_auth_ownership_and_foreign_ids(api):
    client, service = api
    app = client.app
    app.state.auth_enabled = True
    owner = {"Authorization": "Bearer " + create_token("owner", "owner", "admin")}
    other = {"Authorization": "Bearer " + create_token("other", "other", "user")}
    for endpoint in ("terminals", "runs", "run-configs"):
        assert client.get(f"/api/v1/code/projects/p/{endpoint}").status_code == 401
        assert client.get(f"/api/v1/code/projects/p/{endpoint}", headers=other).status_code == 404
        assert client.get(f"/api/v1/code/projects/missing/{endpoint}", headers=owner).status_code == 404
    started = client.post("/api/v1/code/projects/p/runs", headers=owner, json={"kind": "python", "path": "hello.py"}).json()
    id = started["id"]
    wait_done(service, id)
    assert client.get(f"/api/v1/code/projects/q/runs/{id}", headers=owner).status_code == 404
    assert client.post(f"/api/v1/code/projects/p/runs/{id}/stop", headers=other).status_code == 404
    assert client.get(f"/api/v1/code/projects/p/runs/{id}?after_seq=-1", headers=owner).status_code == 422
    with pytest.raises(WebSocketDisconnect) as missing:
        with client.websocket_connect(f"/api/v1/code/projects/p/runs/{id}/stream"):
            pass
    assert missing.value.code == 4401
    token = other["Authorization"].split()[1]
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect(f"/api/v1/code/projects/p/runs/{id}/stream?token={token}"):
            pass
    assert denied.value.code == 4404


def test_symlink_cwd_cannot_escape_project(service, project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (project / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        service.create_run("p", "owner", kind="command", command="echo fixture", cwd="link")


def test_closed_service_rejects_new_process(service):
    service.close()
    with pytest.raises(RuntimeBusy):
        service.create_run("p", "owner", kind="python", path="hello.py")


def test_config_only_pytest_and_invalid_package(service, project):
    (project / "test_small.py").unlink()
    (project / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts="-q"\n', encoding="utf-8")
    (project / "package.json").write_text('{"scripts":null}', encoding="utf-8")
    assert any(row["kind"] == "pytest" for row in service.run_configs("p", "owner")["configs"])
    with pytest.raises(ValueError, match="script"):
        service.create_run("p", "owner", kind="npm", script="test")


def test_runs_use_project_virtualenv_and_read_only_pytest_capability(service, project):
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(project / ".venv")
    library = project / ".venv" / "Lib" / "site-packages" if os.name == "nt" else next((project / ".venv/lib").glob("python*/site-packages"))
    (library / "fixture_runtime_dependency.py").write_text("VALUE = 'project-venv-only'\n", encoding="utf-8")
    (project / "hello.py").write_text("from fixture_runtime_dependency import VALUE\nprint(VALUE)\n", encoding="utf-8")
    created = service.create_run("p", "owner", kind="python", path="hello.py")
    result = wait_done(service, created["id"])
    assert ".venv" in created["command"][0] and result["exit_code"] == 0
    assert "project-venv-only" in "".join(event.get("data", "") for event in result["events"])
    assert next(row for row in service.run_configs("p", "owner")["configs"] if row["kind"] == "pytest")["available"] is False
    assert ".venv" in service._argv(project, project, kind="pytest")[0]


def test_terminal_api_validation_and_foreign_close(api):
    client, _ = api
    base = "/api/v1/code/projects/p/terminals"
    for body in ({"cols": 0}, {"rows": 301}, {"cwd": "../"}, {"shell": "arbitrary-shell"}):
        assert client.post(base, json=body).status_code == 422
    assert client.delete(base + "/missing").status_code == 404


@pytest.mark.skipif(os.name == "nt", reason="POSIX controlling-terminal check")
def test_posix_pty_controls_job_and_interrupt(service):
    created = service.create_terminal("p", "owner")
    id = created["id"]
    service.input("p", "owner", id, "test -t 0 && test -t 1 && echo real-tty; sleep 30\n")
    wait_text(service, id, "real-tty", "terminal")
    service.input("p", "owner", id, "\x03")
    service.input("p", "owner", id, "echo interrupted; exit 0\n")
    result = wait_done(service, id, "terminal")
    assert result["exit_code"] == 0
    assert "interrupted" in "".join(event.get("data", "") for event in result["events"])
