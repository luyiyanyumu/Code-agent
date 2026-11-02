"""Direct Windows process API regressions, plus isolated real listener stops."""

from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "launcher_process.py"
spec = importlib.util.spec_from_file_location("fast_launcher_process", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
CREATED = "2026-10-01T01:02:03.0000001Z"
WORKSPACE = r"C:\workspace with spaces"
PYTHON = WORKSPACE + r"\.venv\Scripts\python.exe"
BACKEND = [PYTHON, "-m", "code_agent.server", "--host", "127.0.0.1", "--port", "8765"]
FRONTEND = [r"C:\Program Files\nodejs\node.exe", WORKSPACE + r"\npm\launcher-preview.mjs", "--port", "5175", "--backend-port", "8765"]


class FakeWindows:
    def __init__(self):
        self.snapshots = [[(8765, 222), (5175, 333)]]
        self.reads = 0
        self.stops = []
        self.closed = []
        self.handles = []
        self.created_values = [CREATED]
        self.argument_values = [BACKEND]
        self.image_value = r"C:\base\python.exe"
        self.locked = False
        self.fail_open = False
        self.lock_busy = False

    def listeners(self):
        value = self.snapshots[min(self.reads, len(self.snapshots) - 1)]
        self.reads += 1
        return value

    @contextmanager
    def process(self, pid, *, terminate=False):
        if self.fail_open:
            raise helper.CannotVerify("fixture unavailable")
        handle = object()
        self.handles.append((pid, terminate, handle))
        try:
            yield handle
        finally:
            self.closed.append(handle)

    def created(self, handle):
        return self.created_values.pop(0) if len(self.created_values) > 1 else self.created_values[0]

    def arguments(self, handle):
        return self.argument_values.pop(0) if len(self.argument_values) > 1 else self.argument_values[0]

    def image(self, handle):
        return self.image_value

    @contextmanager
    def lock(self, api_port, web_port):
        assert (api_port, web_port) == (8765, 5175)
        if self.lock_busy:
            raise helper.CannotVerify("fixture locked")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def terminate(self, handle):
        assert self.locked
        assert handle is self.handles[-1][2]
        self.stops.append(handle)


def inspector(windows):
    return helper.Inspector(windows, WORKSPACE, PYTHON, 8765, 5175)


@pytest.mark.parametrize("flags", [[], ["-I", "-X", "utf8", "-u"], ["-I", "-B", "-X", "utf8"]])
def test_exact_backend_and_runtime_flags(flags):
    args = BACKEND[:1] + flags + BACKEND[1:]
    assert helper.backend_arguments(args, PYTHON, 8765)
    assert helper.backend_arguments(args + ["--provider", "offline"], PYTHON, 8765)


@pytest.mark.parametrize("args", [
    ["python.exe"] + BACKEND[1:],
    [r"C:\other\python.exe"] + BACKEND[1:],
    BACKEND + ["--reload"],
    [value.replace("127.0.0.1", "0.0.0.0") for value in BACKEND],
    [value.replace("code_agent.server", "other.server") for value in BACKEND],
    BACKEND + ["--provider", "unknown"],
    [value.replace("8765", "18765") for value in BACKEND],
])
def test_unrelated_backend_arguments_rejected(args):
    assert not helper.backend_arguments(args, PYTHON, 8765)


def test_frontend_exact_root_and_argument_set():
    assert helper.frontend_arguments(FRONTEND, WORKSPACE, 5175, 8765)
    assert helper.frontend_arguments(["node"] + FRONTEND[1:], WORKSPACE, 5175, 8765)
    vite = ["node", WORKSPACE + r"\web\node_modules\.bin\..\vite\bin\vite.js", "--host", "127.0.0.1", "--port", "5175", "--strictPort"]
    assert helper.frontend_arguments(vite, WORKSPACE, 5175, 8765)
    assert not helper.frontend_arguments([FRONTEND[0], r"C:\other\npm\launcher-preview.mjs"] + FRONTEND[2:], WORKSPACE, 5175, 8765)
    assert not helper.frontend_arguments(FRONTEND + ["--extra"], WORKSPACE, 5175, 8765)
    assert not helper.frontend_arguments(["not-node.exe"] + FRONTEND[1:], WORKSPACE, 5175, 8765)


def test_creation_format_preserves_seventh_digit():
    assert helper.created_string(0) == "1601-01-01T00:00:00.0000000Z"
    assert helper.created_string(1234567) == "1601-01-01T00:00:00.1234567Z"


def test_inspect_rejects_missing_multiple_unreadable_and_wrong_executable():
    windows = FakeWindows()
    windows.snapshots = [[]]
    assert inspector(windows).inspect()["backend"] == {"pid": None, "created": None, "owned": False, "detail": "not-listening"}
    windows.snapshots = [[(8765, 222), (8765, 777)]]
    assert inspector(windows).inspect()["backend"]["detail"] == "multiple-listener-owners"
    windows.snapshots = [[(8765, 222)]]
    windows.fail_open = True
    assert inspector(windows).inspect()["backend"]["detail"] == "identity-unavailable"
    windows.fail_open = False
    windows.image_value = r"C:\other\evil.exe"
    assert inspector(windows).inspect()["backend"]["owned"] is False
    assert len(windows.closed) == len(windows.handles)


def test_stop_rechecks_exact_target_twice_and_terminates_retained_handle():
    windows = FakeWindows()
    windows.snapshots = [[(8765, 222), (8765, 222), (18765, 999), (5175, 333)]]
    result = inspector(windows).stop("backend", 222, CREATED)
    assert result == {"stopped": True, "service": "backend", "pid": 222, "created": CREATED}
    assert windows.reads == 2
    assert [(pid, writable) for pid, writable, _ in windows.handles] == [(222, True)]
    assert windows.stops == windows.closed
    assert not windows.locked


@pytest.mark.parametrize("scenario", ["new_pid", "recycled_pid", "changed_args", "replaced_image", "multiple", "missing", "locked", "unavailable", "changed_creation_before_terminate"])
def test_unsafe_changes_do_not_stop_any_process(scenario):
    windows = FakeWindows()
    if scenario == "new_pid":
        windows.snapshots += [[(8765, 777), (5175, 333)]]
    elif scenario == "recycled_pid":
        windows.created_values = [CREATED.replace("0000001", "0000002")]
    elif scenario == "changed_args":
        windows.argument_values = [BACKEND, BACKEND + ["--reload"]]
    elif scenario == "replaced_image":
        windows.image_value = r"C:\other\evil.exe"
    elif scenario == "multiple":
        windows.snapshots += [[(8765, 222), (8765, 777)]]
    elif scenario == "missing":
        windows.snapshots += [[]]
    elif scenario == "locked":
        windows.lock_busy = True
    elif scenario == "unavailable":
        windows.fail_open = True
    elif scenario == "changed_creation_before_terminate":
        windows.created_values = [CREATED, CREATED, CREATED.replace("0000001", "0000002")]
    with pytest.raises(helper.CannotVerify):
        inspector(windows).stop("backend", 222, CREATED)
    assert not windows.stops
    assert windows.closed == [handle for _, _, handle in windows.handles]
    assert not windows.locked


def test_frontend_stop_never_queries_or_stops_backend():
    windows = FakeWindows()
    windows.argument_values = [FRONTEND]
    windows.image_value = FRONTEND[0]
    assert inspector(windows).stop("frontend", 333, CREATED)["pid"] == 333
    assert windows.handles[0][0] == 333
    assert len(windows.stops) == 1


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def run_helper(workspace, python, api_port, web_port, *args, runner=None):
    result = subprocess.run([str(runner or sys._base_executable), "-I", "-S", "-X", "utf8", str(SCRIPT),
        "-Root", str(workspace), "-BackendPython", str(python), "-BackendPort", str(api_port), "-FrontendPort", str(web_port), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=10)
    return result.returncode, json.loads(result.stdout)


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("node"), reason="Windows and Node required")
def test_real_direct_api_inspection_and_stop_only_own_listener(tmp_path):
    # An isolated server module uses the exact source-launch command line but
    # imports none of the actual application, database, or user configuration.
    python = Path(sys._base_executable).resolve()
    module = tmp_path / "code_agent"
    module.mkdir()
    (module / "__init__.py").write_text("")
    (module / "server.py").write_text("import argparse,socket,time\np=argparse.ArgumentParser();p.add_argument('--host');p.add_argument('--port',type=int);a=p.parse_args()\ns=socket.socket();s.bind((a.host,a.port));s.listen();time.sleep(60)\n")
    preview = tmp_path / "npm" / "launcher-preview.mjs"
    preview.parent.mkdir()
    preview.write_text("import net from 'node:net'; net.createServer().listen(Number(process.argv[3]),'127.0.0.1');\n")
    api_port, web_port = free_port(), free_port()
    backend = subprocess.Popen([str(python), "-m", "code_agent.server", "--host", "127.0.0.1", "--port", str(api_port)], cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frontend = subprocess.Popen([shutil.which("node"), str(preview), "--port", str(web_port), "--backend-port", str(api_port)], cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while True:
            code, state = run_helper(tmp_path, python, api_port, web_port, "-Action", "Inspect")
            if code == 0 and state["backend"]["owned"] and state["frontend"]["owned"]:
                break
            assert time.monotonic() < deadline, state
            time.sleep(.05)
        assert state["backend"]["pid"] == backend.pid
        assert state["frontend"]["pid"] == frontend.pid
        assert state["backend"]["created"].endswith("Z")
        # Bad creation time is refused; both listeners remain running.
        code, denied = run_helper(tmp_path, python, api_port, web_port, "-Action", "Stop", "-Service", "backend", "-ExpectedPid", str(backend.pid), "-ExpectedCreated", "2000-01-01T00:00:00.0000000Z")
        assert code == 1 and "error" in denied
        assert backend.poll() is None and frontend.poll() is None
        # The same named mutex as the legacy launcher prevents cross-launcher
        # changes, without waiting on or terminating that other launcher.
        with helper.Windows().lock(api_port, web_port):
            code, _ = run_helper(tmp_path, python, api_port, web_port, "-Action", "Stop", "-Service", "backend", "-ExpectedPid", str(backend.pid), "-ExpectedCreated", state["backend"]["created"])
            assert code == 1 and backend.poll() is None
        code, stopped = run_helper(tmp_path, python, api_port, web_port, "-Action", "Stop", "-Service", "backend", "-ExpectedPid", str(backend.pid), "-ExpectedCreated", state["backend"]["created"])
        assert code == 0 and stopped["stopped"] is True
        backend.wait(timeout=5)
        assert frontend.poll() is None
        code, stopped = run_helper(tmp_path, python, api_port, web_port, "-Action", "Stop", "-Service", "frontend", "-ExpectedPid", str(frontend.pid), "-ExpectedCreated", state["frontend"]["created"])
        assert code == 0 and stopped["stopped"] is True
        frontend.wait(timeout=5)
    finally:
        for child in (backend, frontend):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("node"), reason="Windows and Node required")
def test_real_ipv6_listener_and_portable_python_preserve_unrecognized_process(tmp_path):
    portable = ROOT / ".tmp/windows-close-fix-payload-20261030/runtimes/python/python.exe"
    if not portable.exists():
        pytest.skip("Prepared portable runtime required for this integration")
    try:
        with socket.socket(socket.AF_INET6) as server:
            server.bind(("::1", 0))
            web_port = server.getsockname()[1]
    except OSError:
        pytest.skip("IPv6 loopback is unavailable")
    api_port = free_port()
    python = Path(sys._base_executable).resolve()
    preview = tmp_path / "npm" / "launcher-preview.mjs"
    preview.parent.mkdir()
    preview.write_text("import net from 'node:net'; net.createServer().listen(Number(process.argv[3]),'::1');\n")
    unknown = subprocess.Popen([str(python), "-c", f"import socket,time;s=socket.socket();s.bind(('127.0.0.1',{api_port}));s.listen();time.sleep(60)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frontend = subprocess.Popen([shutil.which("node"), str(preview), "--port", str(web_port), "--backend-port", str(api_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while True:
            code, state = run_helper(tmp_path, python, api_port, web_port, "-Action", "Inspect", runner=portable)
            if code == 0 and state["backend"]["pid"] == unknown.pid and state["frontend"]["owned"]:
                break
            assert time.monotonic() < deadline, state
            time.sleep(.05)
        assert state["backend"]["owned"] is False
        assert state["backend"]["detail"] == "unrecognized-process"
        assert state["frontend"]["pid"] == frontend.pid
        # Even an exact PID and creation time never authorize an unknown argv.
        code, _ = run_helper(tmp_path, python, api_port, web_port, "-Action", "Stop", "-Service", "backend", "-ExpectedPid", str(unknown.pid), "-ExpectedCreated", state["backend"]["created"], runner=portable)
        assert code == 1 and unknown.poll() is None and frontend.poll() is None
        code, stopped = run_helper(tmp_path, python, api_port, web_port, "-Action", "Stop", "-Service", "frontend", "-ExpectedPid", str(frontend.pid), "-ExpectedCreated", state["frontend"]["created"], runner=portable)
        assert code == 0 and stopped["stopped"] is True
        frontend.wait(timeout=5)
        assert unknown.poll() is None
    finally:
        for child in (unknown, frontend):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
