"""Real background commands have no console; output/state/cleanup still work."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from code_agent.codeintel import vcs
from code_agent.sandbox import executor
from code_agent.sandbox import persistent_shell

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows console allocation regression")


def probe_command(tmp_path: Path, *, cmd=False) -> str:
    script = tmp_path / "console probe.py"
    script.write_text("import ctypes,json\nprint(json.dumps({'console':ctypes.windll.kernel32.GetConsoleWindow()}),flush=True)\n", encoding="utf-8")
    if cmd:
        return subprocess.list2cmdline([sys.executable, str(script)])
    return f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'


@pytest.mark.parametrize("kind", ["bash", "cmd"])
def test_one_shot_command_has_no_console_and_keeps_output(tmp_path, monkeypatch, kind):
    if kind == "bash" and executor.find_bash() is None:
        pytest.skip("Git Bash is unavailable")
    monkeypatch.setenv("CODE_AGENT_SANDBOX_MODE", "process")
    if kind == "cmd":
        monkeypatch.setattr(executor, "find_bash", lambda: None)
    output = asyncio.run(executor.run_shell(probe_command(tmp_path, cmd=kind == "cmd"), cwd=tmp_path, timeout=20))
    assert output.startswith("exit_code=0"), output
    assert json.loads(output.splitlines()[1])["console"] == 0, output


def test_persistent_shell_has_no_console_and_retains_state(tmp_path):
    if persistent_shell.unavailable_reason():
        pytest.skip(persistent_shell.unavailable_reason())

    async def scenario():
        shells = persistent_shell.PersistentShells(cwd=tmp_path)
        try:
            first = await shells.run(probe_command(tmp_path), timeout=20)
            assert first.startswith("exit_code=0"), first
            assert any(json.loads(line).get("console") == 0 for line in first.splitlines() if line.startswith("{")), first
            await shells.run("export BACKGROUND_TEST_STATE=retained", timeout=20)
            second = await shells.run("echo $BACKGROUND_TEST_STATE", timeout=20)
            assert "retained" in second, second
        finally:
            await shells.close()

    asyncio.run(scenario())


def test_git_background_process_and_timeout_cleanup_are_hidden(tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("Git is unavailable")
    calls = []
    create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return await create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    async def scenario():
        code, output = await vcs.run_git(tmp_path, ["--version"])
        assert code == 0 and output.startswith("git version"), output
        code, output = await vcs.run_git(tmp_path, [
            "-c", f"alias.console-probe=!{probe_command(tmp_path)}", "console-probe",
        ])
        assert code == 0 and json.loads(output)["console"] == 0, output
        # This child is owned by this test, and real taskkill must reap it.
        child = await create(sys.executable, "-c", "import time;time.sleep(60)",
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        try:
            await vcs._kill_tree(child)
            assert child.returncode is not None
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    asyncio.run(scenario())
    assert calls and calls[0][0][0] == "git"
    assert calls[0][1]["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    assert all(options.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW for _, options in calls)
    assert any(args[0] == "taskkill" for args, _ in calls)
