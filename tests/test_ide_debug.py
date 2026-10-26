"""Real DAP acceptance with disposable Python scripts; no user project is run."""
from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import sys
import venv
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from code_agent.api.debug_api import mount_debug_api
from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel.debug_runtime import DebugError, DebugRuntime
from code_agent.codeintel.paths import ProjectPathError
from code_agent.codeintel.interpreter import interpreter_info, select_python


def run(coro):
    return asyncio.run(coro)


async def wait_state(session, predicate, timeout=15):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = session.snapshot()
        if predicate(state):
            return state
        if state["status"] == "error":
            pytest.fail(str(state))
        await asyncio.sleep(.05)
    pytest.fail(f"Timed out: {session.snapshot()}")


@pytest.fixture
def debug_script(tmp_path):
    path = tmp_path / "main.py"
    path.write_text(
        "def increment(value):\n"
        "    return value + 1\n"
        "\n"
        "def compute():\n"
        "    number = 3\n"
        "    nested = {'items': [1, 2]}\n"
        "    result = increment(number)\n"
        "    print('debug-result', result)\n"
        "    return result\n"
        "\n"
        "value = compute()\n", encoding="utf-8",
    )
    return path


requires_debugpy = pytest.mark.skipif(importlib.util.find_spec("debugpy") is None, reason="debugpy is not installed")


def process_alive(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def assert_process_ended(pid):
    deadline = asyncio.get_running_loop().time() + 5
    while process_alive(pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(.05)
    assert not process_alive(pid), f"Owned debuggee {pid} survived debugger cleanup"


@requires_debugpy
def test_real_breakpoint_steps_stack_locals_and_output(debug_script):
    async def exercise():
        runtime = DebugRuntime()
        try:
            session = await runtime.start("owner", "project", debug_script.parent, path="main.py", breakpoints=[{"path": "main.py", "line": 7}])
            entry = await wait_state(session, lambda s: s["status"] == "paused" and bool(s["stack"]))
            assert entry["reason"] == "entry"
            assert entry["stack"][0]["path"] == "main.py"
            assert entry["breakpoints"][0]["verified"] is True
            await session.action("continue")
            stopped = await wait_state(session, lambda s: s["status"] == "paused" and s["pause_seq"] > entry["pause_seq"] and bool(s["stack"]))
            assert stopped["reason"] == "breakpoint"
            assert stopped["stack"][0]["name"] == "compute"
            assert stopped["stack"][0]["line"] == 7
            assert len(stopped["stack"]) >= 2
            locals_by_name = {v["name"]: v for v in stopped["variables"]}
            assert locals_by_name["number"]["value"] == "3"
            reference = locals_by_name["nested"]["variablesReference"]
            nested = await session.variables(reference, stopped["pause_seq"])
            items = next(v for v in nested["variables"] if v["name"] == "'items'")
            assert items["variablesReference"] > 0
            array = await session.variables(items["variablesReference"], stopped["pause_seq"])
            assert any(v["value"] == "2" for v in array["variables"])
            await session.action("stepIn")
            entered = await wait_state(session, lambda s: s["status"] == "paused" and s["pause_seq"] > stopped["pause_seq"] and bool(s["stack"]))
            assert entered["stack"][0]["name"] == "increment"
            with pytest.raises(DebugError, match="失效"):
                await session.variables(reference, stopped["pause_seq"])
            await session.action("stepOut")
            returned = await wait_state(session, lambda s: s["status"] == "paused" and s["pause_seq"] > entered["pause_seq"] and bool(s["stack"]))
            assert returned["stack"][0]["name"] == "compute"
            await session.action("next")
            advanced = await wait_state(session, lambda s: s["status"] == "paused" and s["pause_seq"] > returned["pause_seq"] and bool(s["stack"]))
            assert advanced["stack"][0]["line"] > returned["stack"][0]["line"]
            await runtime.update_breakpoints(session, [])
            assert session.snapshot()["breakpoints"] == []
            await session.action("continue")
            ended = await wait_state(session, lambda s: s["status"] == "stopped")
            assert ended["exit_code"] == 0
            assert "debug-result 4" in "".join(item["output"] for item in ended["console"])
            assert all(item["seq"] > 0 for item in ended["console"])
            assert session.snapshot(ended["console_seq"])["console"] == []
            await session.stop()
            assert session.process.returncode is not None
        finally:
            await runtime.close()
    run(exercise())


@requires_debugpy
def test_real_pause_stop_and_runtime_cleanup(tmp_path):
    (tmp_path / "loop.py").write_text("import time\nnumber = 0\nwhile True:\n    number += 1\n    time.sleep(.02)\n", encoding="utf-8")
    async def exercise():
        runtime = DebugRuntime()
        try:
            session = await runtime.start("owner", "project", tmp_path, path="loop.py")
            entry = await wait_state(session, lambda s: s["status"] == "paused" and bool(s["stack"]))
            await session.action("continue")
            await wait_state(session, lambda s: s["status"] == "running")
            await asyncio.sleep(.1)
            await session.action("pause")
            paused = await wait_state(session, lambda s: s["status"] == "paused" and s["pause_seq"] > entry["pause_seq"] and bool(s["stack"]))
            assert paused["reason"] == "pause"
            assert session.debuggee_pid
            await session.action("stop")
            assert session.snapshot()["status"] == "stopped"
            assert session.process.returncode is not None
            await assert_process_ended(session.debuggee_pid)
            await session.action("stop")  # Idempotent.
        finally:
            await runtime.close()
        with pytest.raises(DebugError, match="关闭"):
            await runtime.start("owner", "project", tmp_path, path="loop.py")
    run(exercise())


@requires_debugpy
def test_adapter_crash_does_not_leave_the_owned_debuggee_running(tmp_path):
    (tmp_path / "loop.py").write_text("import time\nwhile True:\n    time.sleep(.03)\n", encoding="utf-8")
    async def exercise():
        runtime = DebugRuntime()
        try:
            session = await runtime.start("owner", "project", tmp_path, path="loop.py")
            await wait_state(session, lambda s: s["status"] == "paused" and bool(s["stack"]))
            await session.action("continue")
            await wait_state(session, lambda s: s["status"] == "running")
            pid = session.debuggee_pid
            session.process.kill()  # Crash only this disposable session's own adapter.
            await session.process.wait()
            deadline = asyncio.get_running_loop().time() + 8
            while not session._closing and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.05)
            await session.stop()
            await assert_process_ended(pid)
            assert session.snapshot()["status"] == "error"
        finally:
            await runtime.close()
    run(exercise())


def test_paths_arguments_unavailable_and_owner_guard(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "App.vue").write_text("<template />", encoding="utf-8")
    async def exercise():
        runtime = DebugRuntime()
        for payload in ({"path": "../escape.py"}, {"path": "App.vue"}, {"path": "main.py", "cwd": "../"}, {"path": "main.py", "args": "--invalid"}, {"path": "main.py", "breakpoints": [{"path": "main.py", "line": True}]}):
            with pytest.raises(ProjectPathError):
                await runtime.start("owner", "p", tmp_path, **payload)
        assert not runtime.sessions
        monkeypatch.setattr(runtime, "meta", lambda: {"available": False})
        with pytest.raises(DebugError) as unavailable:
            await runtime.start("owner", "p", tmp_path, path="main.py")
        assert unavailable.value.status == 503
        with pytest.raises(KeyError):
            runtime.get("other", "p", "missing")
    run(exercise())


def test_adapter_spawn_failure_is_closed_instead_of_consuming_an_active_slot(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    async def broken_spawn(*args, **kwargs):
        raise OSError("fixture adapter spawn failure")
    async def exercise():
        runtime = DebugRuntime()
        monkeypatch.setattr(runtime, "meta", lambda: {"available": True})
        monkeypatch.setattr(asyncio, "create_subprocess_exec", broken_spawn)
        with pytest.raises(OSError, match="fixture"):
            await runtime.start("owner", "project", tmp_path, path="main.py")
        session = next(iter(runtime.sessions.values()))
        assert session.state["status"] == "error"
        assert session._closing is True
        await runtime.close()
    run(exercise())


def test_project_interpreter_probe_falls_back_and_does_not_resolve_venv_python_symlink(tmp_path):
    assert select_python(tmp_path) == Path(sys.executable)
    assert interpreter_info(tmp_path)["interpreter_source"] == "backend"
    bin_dir = tmp_path / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    candidate = bin_dir / ("python.exe" if os.name == "nt" else "python")
    if os.name == "nt":
        candidate.write_bytes(b"fixture only; never executed")
    else:
        candidate.symlink_to(sys.executable)
    assert select_python(tmp_path) == candidate
    assert interpreter_info(tmp_path)["interpreter_source"] == "project-venv"


def test_interpreter_ignores_other_platform_and_nonexecutable_files(tmp_path):
    directory, name = ("bin", "python") if os.name == "nt" else ("Scripts", "python.exe")
    foreign = tmp_path / ".venv" / directory / name
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"foreign-platform fixture; never executed")
    assert select_python(tmp_path) == Path(sys.executable)
    if os.name != "nt":
        candidate = tmp_path / ".venv" / "bin" / "python"
        candidate.parent.mkdir(parents=True)
        candidate.write_text("non executable fixture", encoding="utf-8")
        candidate.chmod(0o600)
        assert select_python(tmp_path) == Path(sys.executable)


@requires_debugpy
def test_real_project_virtualenv_uses_its_dependencies_without_installing_debugpy(tmp_path):
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(tmp_path / ".venv")
    libraries = (tmp_path / ".venv" / "Lib" / "site-packages") if os.name == "nt" else next((tmp_path / ".venv" / "lib").glob("python*/site-packages"))
    libraries.mkdir(parents=True, exist_ok=True)
    (libraries / "fixture_debug_dep.py").write_text("VALUE = 'project-environment-only'\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("import fixture_debug_dep\nprint(fixture_debug_dep.VALUE)\n", encoding="utf-8")
    async def exercise():
        runtime = DebugRuntime()
        try:
            expected = str(select_python(tmp_path))
            assert runtime.meta(tmp_path)["python"] == expected
            session = await runtime.start("owner", "project", tmp_path, path="main.py")
            assert session.snapshot()["python"] == expected
            assert session.snapshot()["interpreter_source"] == "project-venv"
            await wait_state(session, lambda s: s["status"] == "paused" and bool(s["stack"]))
            await session.action("continue")
            ended = await wait_state(session, lambda s: s["status"] == "stopped")
            assert ended["exit_code"] == 0
            assert "project-environment-only" in "".join(item["output"] for item in ended["console"])
        finally:
            await runtime.close()
    run(exercise())


@requires_debugpy
def test_api_real_session_owner_and_state(debug_script):
    async def exercise():
        class Projects:
            def get_owned(self, owner, project):
                if owner != "owner" or project != "project":
                    raise KeyError(project)
                return {"root_path": str(debug_script.parent)}
            def root_of(self, project):
                return Path(project["root_path"])
        app, runtime = FastAPI(), DebugRuntime()
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="owner", username="test", role="user")
        mount_debug_api(app, service=Projects(), runtime=runtime)
        prefix = "/api/v1/code/projects/project/debug"
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                assert (await client.get(prefix + "/meta")).json()["languages"] == ["python"]
                assert (await client.get(prefix.replace("project/debug", "other/debug") + "/meta")).status_code == 404
                invalid = await client.post(prefix + "/sessions", json={"path": "../escape.py"})
                assert invalid.status_code == 422
                started = await client.post(prefix + "/sessions", json={"path": "main.py", "breakpoints": [{"path": "main.py", "line": 7}]})
                assert started.status_code == 200, started.text
                sid = started.json()["id"]
                session = runtime.get("owner", "project", sid)
                paused = await wait_state(session, lambda s: s["status"] == "paused" and bool(s["stack"]))
                state = await client.get(prefix + "/sessions/" + sid, params={"frame_id": paused["stack"][0]["id"]})
                assert state.status_code == 200 and state.json()["frame_id"] == paused["stack"][0]["id"]
                with pytest.raises(KeyError):
                    runtime.get("other", "project", sid)
                assert len((await client.get(prefix + "/sessions")).json()["sessions"]) == 1
                action = await client.post(prefix + "/sessions/" + sid + "/action", json={"action": "evaluate"})
                assert action.status_code == 422
                stale = await client.get(prefix + "/sessions/" + sid + "/variables?reference=999999")
                assert stale.status_code == 409
                stopped = await client.post(prefix + "/sessions/" + sid + "/action", json={"action": "stop"})
                assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
        finally:
            await runtime.close()
    run(exercise())
