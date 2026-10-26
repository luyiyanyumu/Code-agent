"""Java DAP transport, ownership and launch boundary fixtures; no user JVM runs."""
from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from code_agent.api.debug_api import mount_debug_api
from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel.debug_runtime import DebugError, DebugRuntime
from code_agent.codeintel.java_debug import JavaDebugProvider, JavaDebugSession, debug_bundles, validate_java_options
from code_agent.codeintel.paths import ProjectPathError


def bundle(path: Path, identity="com.microsoft.java.debug.plugin") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\r\nBundle-SymbolicName: {identity}\r\n\r\n")
    return path


@pytest.fixture
def java_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODE_AGENT_HOME", str(home / ".code-agent"))
    monkeypatch.delenv("CODE_AGENT_JAVA_DEBUG_BUNDLE", raising=False)
    bundle(home / ".code-agent/ls/java-debug/com.microsoft.java.debug.plugin-1.0.jar")
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "Main.java").write_text("class Main { public static void main(String[] args) {} }\n", encoding="utf-8")
    return root


def test_bundle_discovery_validates_identity_and_chooses_one_version(java_project):
    directory = Path.home() / ".code-agent/ls/java-debug"
    expected = bundle(directory / "com.microsoft.java.debug.plugin-1.10.jar")
    bundle(directory / "com.microsoft.java.debug.plugin-9.0.jar", identity="unrelated.bundle")
    assert debug_bundles() == [str(expected.resolve())]


@pytest.mark.parametrize("options", [
    {"request": "evil"}, {"request": "attach", "host": "192.168.1.20", "port": 5005},
    {"request": "attach", "host": "localhost", "port": True}, {"request": "attach", "port": 65536},
    {"main_class": "Main;rm -rf"}, {"project_name": "p\nsecret"}, {"stop_on_entry": 1},
    {"javaExec": "external.exe"}, {"port": 5005}, [],
])
def test_java_launch_options_reject_remote_and_uncontrolled_inputs(options):
    with pytest.raises(ProjectPathError):
        validate_java_options(options)


class AdapterFixture:
    def __init__(self, root):
        self.root = root
        self.commands = []
        self.launch = None
        self.writer = None
        self.disconnect = None
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self._seq = 0

    async def write(self, message):
        self._seq += 1
        message["seq"] = self._seq
        blob = json.dumps(message).encode()
        self.writer.write(f"Content-Length: {len(blob)}\r\n\r\n".encode() + blob)
        await self.writer.drain()

    async def event(self, event, body=None):
        await self.write({"type": "event", "event": event, "body": body or {}})

    async def response(self, request, body=None):
        await self.write({"type": "response", "request_seq": request["seq"], "command": request["command"], "success": True, "body": body or {}})

    async def handle(self, reader, writer):
        self.writer = writer
        self.connected.set()
        try:
            while True:
                header = await reader.readuntil(b"\r\n\r\n")
                length = int(header.split(b":")[1].strip())
                request = json.loads(await reader.readexactly(length))
                self.commands.append(request)
                command, arguments = request["command"], request.get("arguments", {})
                if command in {"launch", "attach"}:
                    self.launch = request
                    await self.event("initialized")
                    continue
                if command == "setBreakpoints":
                    body = {"breakpoints": [{"id": index + 1, "verified": True, "line": bp["line"]} for index, bp in enumerate(arguments["breakpoints"])]}
                elif command == "threads":
                    body = {"threads": [{"id": 7, "name": "main"}]}
                elif command == "stackTrace":
                    body = {"stackFrames": [{"id": 11, "name": "Main.main", "line": 1, "column": 1, "source": {"path": str(self.root / "Main.java"), "name": "Main.java"}}]}
                elif command == "scopes":
                    body = {"scopes": [{"name": "Local", "variablesReference": 12, "expensive": False}]}
                elif command == "variables":
                    body = {"variables": [{"name": "number", "type": "int", "value": "3", "variablesReference": 0}]}
                else:
                    body = {}
                await self.response(request, body)
                if command == "configurationDone":
                    await self.response(self.launch)
                    await self.event("stopped", {"reason": "entry", "threadId": 7})
                elif command in {"next", "stepIn", "stepOut"}:
                    await self.event("continued", {"threadId": 7})
                    await self.event("stopped", {"reason": "step", "threadId": 7})
                elif command == "continue":
                    await self.event("continued", {"threadId": 7})
                elif command == "disconnect":
                    self.disconnect = arguments
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.closed.set()


async def wait_paused(session):
    for _ in range(200):
        if session.state["status"] == "paused" and session.state["stack"]:
            return session.snapshot()
        await asyncio.sleep(.01)
    raise AssertionError(session.snapshot())


@pytest.mark.parametrize("mode", ["launch", "attach"])
def test_real_tcp_dap_launch_or_attach_steps_and_cleanup(java_project, mode):
    async def exercise():
        adapter = AdapterFixture(java_project)
        server = await asyncio.start_server(adapter.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        invocations, leases, released = [], [], []

        async def command(owner, project, root, name, arguments, timeout):
            assert (owner, project, root) == ("owner", "project", java_project)
            invocations.append((name, arguments))
            if name == "vscode.java.resolveMainMethod":
                return [{"mainClass": "Main", "projectName": "fixture", "filePath": str(root / "Main.java")}]
            if name == "vscode.java.resolveClasspath":
                return [[], [str(root / "classes")]]
            if name == "vscode.java.resolveJavaExecutable":
                return sys.executable  # Existing file; fake adapter never executes it.
            if name == "vscode.java.startDebugSession":
                return port
            raise AssertionError(name)

        async def acquire(*args):
            token = object()
            leases.append(token)
            return token

        async def release(token):
            released.append(token)

        provider = JavaDebugProvider(command, acquire=acquire, release=release)
        runtime = DebugRuntime(java_provider=provider)
        try:
            assert "java" in runtime.meta()["languages"]
            options = {"request": mode, **({"host": "localhost", "port": 5005} if mode == "attach" else {})}
            session = await runtime.start("owner", "project", java_project, path="Main.java", args=["space value", 'quoted"value', "反斜线\\"], breakpoints=[{"path": "Main.java", "line": 1}], java=options)
            state = await wait_paused(session)
            assert state["language"] == "java" and "python" not in state
            assert state["stack"][0]["path"] == "Main.java"
            assert state["variables"][0]["value"] == "3"
            assert state["breakpoints"][0]["verified"]
            with pytest.raises(KeyError):
                runtime.get("other", "project", session.id)
            if mode == "launch":
                config = adapter.launch["arguments"]
                assert config["mainClass"] == "Main" and config["projectName"] == "fixture"
                assert config["classPaths"] == [str(java_project / "classes")]
                assert config["args"] == ""
                assert session.args == ["space value", 'quoted"value', "反斜线\\"]
            else:
                assert adapter.launch["arguments"]["hostName"] == "127.0.0.1"
                assert not any("resolveClasspath" in name for name, _ in invocations)
            old_generation = state["pause_seq"]
            await session.action("next")
            for _ in range(100):
                if session.state["pause_seq"] > old_generation:
                    break
                await asyncio.sleep(.01)
            await wait_paused(session)
            with pytest.raises(DebugError, match="失效"):
                await session.variables(12, old_generation)
            await runtime.update_breakpoints(session, [])
            assert session.state["breakpoints"] == []
            await session.stop()
            await session.stop()
            await asyncio.wait_for(adapter.closed.wait(), 2)
            assert adapter.disconnect == {"terminateDebuggee": mode == "launch"}
            assert released == leases and len(released) == 1
            assert session.state["status"] == "stopped"
            assert session.process is None  # Never kill the shared JDT process.
        finally:
            await runtime.close()
            server.close()
            await server.wait_closed()
    asyncio.run(exercise())


def test_failed_java_resolution_releases_capacity_and_lease(java_project):
    async def exercise():
        released = []
        async def command(*args):
            return []
        async def acquire(*args):
            return "fixture lease"
        async def release(token):
            released.append(token)
        runtime = DebugRuntime(java_provider=JavaDebugProvider(command, acquire=acquire, release=release))
        with pytest.raises(DebugError, match="main"):
            await runtime.start("owner", "project", java_project, path="Main.java")
        failed = next(iter(runtime.sessions.values()))
        assert failed._closing and failed.state["status"] == "error"
        assert released == ["fixture lease"]
        await runtime.close()
        with pytest.raises(DebugError, match="关闭"):
            await runtime.start("owner", "project", java_project, path="Main.java")
    asyncio.run(exercise())


def test_stop_during_jdt_acquisition_releases_late_lease_without_launch(java_project):
    async def exercise():
        entered, finish = asyncio.Event(), asyncio.Event()
        released = []
        async def acquire(*args):
            entered.set()
            await finish.wait()
            return "late lease"
        async def release(token):
            released.append(token)
        async def command(*args):
            raise AssertionError("Stopped startup reached JDT command")
        session = JavaDebugSession("owner", "p", java_project, java_project / "Main.java", java_project, [], JavaDebugProvider(command, acquire=acquire, release=release), validate_java_options({}))
        startup = asyncio.create_task(session.start({}))
        await entered.wait()
        await session.stop()
        finish.set()
        with pytest.raises(DebugError, match="取消"):
            await startup
        assert released == ["late lease"]
        assert session.state["status"] == "stopped"
        assert session.debuggee_process is None
    asyncio.run(exercise())


def test_private_reverse_launch_owns_process_and_preserves_argument_array(java_project):
    async def exercise():
        async def command(*args):
            raise AssertionError("No JDT command in transport fixture")
        script = "import sys,json,time;sys.stdout.buffer.write(json.dumps(sys.argv[1:],ensure_ascii=False).encode('utf-8'));sys.stdout.flush();time.sleep(60)"
        values = ["space value", 'quote"value', "C:\\folder\\", "中文", ""]
        session = JavaDebugSession("owner", "project", java_project, java_project / "Main.java", java_project, values, JavaDebugProvider(command), validate_java_options({}))
        session._java_exec = Path(sys.executable).resolve()
        session.state["main_class"] = script  # Fake trusted Java command is a disposable Python child.
        responses = []
        async def write(message):
            responses.append(message)
        session._write = write
        try:
            await session._handle_client_request({"seq": 1, "command": "runInTerminal", "arguments": {"args": [sys.executable, "-c", script], "cwd": str(java_project)}})
            assert responses[-1]["success"] and session.debuggee_process
            for _ in range(200):
                output = "".join(item["output"] for item in session.state["console"])
                if output:
                    break
                await asyncio.sleep(.01)
            assert json.loads(output) == values
            owned = session.debuggee_process
            await session.stop()
            assert owned.returncode is not None
            assert session.process is None
        finally:
            await session.stop()
    asyncio.run(exercise())


@pytest.mark.parametrize("invalid", ["executable", "cwd", "main_class", "environment", "duplicate", "attached"])
def test_reverse_request_cannot_spawn_unapproved_program_or_location(java_project, invalid):
    async def exercise():
        async def command(*args):
            raise AssertionError("No JDT command")
        options = {"request": "attach", "port": 5005} if invalid == "attached" else {}
        session = JavaDebugSession("owner", "p", java_project, java_project / "Main.java", java_project, [], JavaDebugProvider(command), validate_java_options(options))
        session._java_exec = Path(sys.executable).resolve()
        session.state["main_class"] = "Main"
        arguments = {"args": [sys.executable, "Main"], "cwd": str(java_project)}
        if invalid == "executable":
            arguments["args"][0] = str(java_project / "external.exe")
        elif invalid == "cwd":
            arguments["cwd"] = str(java_project.parent)
        elif invalid == "main_class":
            arguments["args"][-1] = "DifferentMain"
        elif invalid == "environment":
            arguments["env"] = {"KEY": "bad\x00value"}
        elif invalid == "duplicate":
            session._closing = True
        responses = []
        async def write(message):
            responses.append(message)
        session._write = write
        await session._handle_client_request({"seq": 1, "command": "runInTerminal", "arguments": arguments})
        assert responses[-1]["success"] is False
        assert session.debuggee_process is None
        session._closing = False
        await session.stop()
    asyncio.run(exercise())


def test_java_path_and_breakpoints_are_project_and_language_scoped(java_project):
    async def exercise():
        async def command(*args):
            raise AssertionError("Invalid payload reached JDT")
        runtime = DebugRuntime(java_provider=JavaDebugProvider(command))
        (java_project / "main.py").write_text("pass\n")
        for payload in [
            {"path": "../Main.java"}, {"path": "Main.java", "cwd": "../"},
            {"path": "Main.java", "breakpoints": [{"path": "main.py", "line": 1}]},
            {"path": "main.py", "language": "java"}, {"path": "Main.java", "language": "python"},
            {"path": "Main.java", "java": {"request": "attach", "host": "example.com", "port": 5005}},
        ]:
            with pytest.raises(ProjectPathError):
                await runtime.start("owner", "p", java_project, **payload)
        assert not runtime.sessions
        with pytest.raises(DebugError, match="允许列表"):
            await runtime.java_provider.execute("owner", "p", java_project, "evil.command", [])
    asyncio.run(exercise())


def test_java_api_forwards_scoped_configuration_and_reports_prerequisite(java_project):
    async def exercise():
        class Projects:
            def get_owned(self, owner, project):
                if owner != "owner" or project != "project":
                    raise KeyError(project)
                return {"root_path": str(java_project)}
            def root_of(self, project):
                return java_project
        app, runtime = FastAPI(), DebugRuntime()
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="owner", username="test", role="user")
        mount_debug_api(app, service=Projects(), runtime=runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            prefix = "/api/v1/code/projects/project/debug"
            unavailable = await client.post(prefix + "/sessions", json={"path": "Main.java", "java": {"request": "attach", "port": 5005}})
            assert unavailable.status_code == 503 and "Java" in unavailable.text
            foreign = await client.post(prefix.replace("project/debug", "other/debug") + "/sessions", json={"path": "Main.java"})
            assert foreign.status_code == 404
            invalid = await client.post(prefix + "/sessions", json={"path": "Main.java", "java": {"request": "attach", "host": "remote", "port": 5005}})
            assert invalid.status_code == 422
        await runtime.close()
    asyncio.run(exercise())
