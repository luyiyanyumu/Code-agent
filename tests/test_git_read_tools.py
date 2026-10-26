"""Real-repository coverage for the Lead's shell-free Git inspection tool."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin import git_read_tools as module
from code_agent.tools.builtin.git_read_tools import GitReadTool


GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(not GIT, reason="Git is required")


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        [GIT, "-c", "core.autocrlf=false", *args], cwd=root, stderr=subprocess.STDOUT,
    ).decode("utf-8", errors="replace")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Read Tool Test")
    _git(root, "config", "user.email", "read@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "source.txt").write_text("before\n", encoding="utf-8")
    _git(root, "add", "source.txt")
    _git(root, "commit", "-m", "initial read-tool fixture")
    return root


def _read(root: Path, **args) -> dict:
    return json.loads(asyncio.run(GitReadTool().execute(args, ToolContext("test", root))))


def test_status_diffs_history_and_show_are_read_only(repo: Path):
    index = repo / ".git" / "index"
    (repo / "source.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    (repo / "source.txt").write_text("working\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    before = index.read_bytes(), index.stat().st_mtime_ns
    status = _read(repo, operation="status")
    assert "MM source.txt" in status["output"]
    assert "?? untracked.txt" in status["output"]
    unstaged = _read(repo, operation="diff")["output"]
    assert "-staged" in unstaged and "+working" in unstaged
    staged = _read(repo, operation="diff", scope="staged")["output"]
    assert "-before" in staged and "+staged" in staged
    assert "+working" in _read(repo, operation="diff", scope="head")["output"]
    assert "initial read-tool fixture" in _read(repo, operation="log", limit=1)["output"]
    assert "+before" in _read(repo, operation="show", revision="HEAD")["output"]
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert (repo / "source.txt").read_text() == "working\n"


@pytest.mark.parametrize("args", [
    {"operation": "commit"}, {"operation": "fetch"}, {"operation": "status", "command": "reset --hard"},
    {"operation": "log", "revision": "--output=owned"},
    {"operation": "show", "revision": "HEAD:source.txt"},
    {"operation": "show", "revision": "HEAD; echo bad"},
    {"operation": "log", "limit": True}, {"operation": "log", "limit": 1000},
    {"operation": "diff", "scope": "--output=owned"},
    {"operation": "status", "paths": ["../outside.txt"]},
    {"operation": "status", "paths": [".git/config"]},
    {"operation": "status", "paths": ["/etc/passwd"]},
])
def test_rejects_write_operations_and_argument_or_path_injection(repo: Path, args: dict):
    with pytest.raises(ToolError):
        _read(repo, **args)


def test_paths_are_literal_and_scoped(repo: Path):
    (repo / "source.txt").write_text("changed\n", encoding="utf-8")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    status = _read(repo, operation="status", paths=["source.txt"])["output"]
    assert "source.txt" in status and "other.txt" not in status
    wildcard = _read(repo, operation="diff", paths=["*.txt"])["output"]
    assert not wildcard


def test_workspace_cannot_inspect_parent_repository(repo: Path):
    child = repo / "nested"
    child.mkdir()
    with pytest.raises(ToolError, match="root must match"):
        _read(child, operation="status")


def test_symlink_escape_is_denied(repo: Path):
    target = repo.parent / "outside.txt"
    target.write_text("private\n")
    link = repo / "outside-link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Host does not permit symlinks")
    with pytest.raises(ToolError, match="outside the workspace"):
        _read(repo, operation="diff", paths=["outside-link"])


def test_disables_external_diff_textconv_filters_fsmonitor_and_pager(repo: Path):
    marker = repo / "EXECUTED"
    command = "echo executed >> EXECUTED"
    _git(repo, "config", "diff.external", command)
    _git(repo, "config", "diff.custom.textconv", command)
    _git(repo, "config", "filter.custom.clean", command)
    _git(repo, "config", "filter.custom.process", command)
    _git(repo, "config", "filter.custom.required", "true")
    _git(repo, "config", "core.fsmonitor", command)
    _git(repo, "config", "core.pager", command)
    (repo / ".gitattributes").write_text("*.txt diff=custom filter=custom\n", encoding="utf-8")
    (repo / "source.txt").write_text("changed\n", encoding="utf-8")
    assert "+changed" in _read(repo, operation="diff")["output"]
    assert "source.txt" in _read(repo, operation="status")["output"]
    assert "+before" in _read(repo, operation="show")["output"]
    assert not marker.exists()


def test_ignores_inherited_git_directory_and_config_injection(repo: Path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(repo.parent / "missing"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "echo bad >> EXECUTED")
    assert "initial read-tool fixture" in _read(repo, operation="log")["output"]
    assert not (repo / "EXECUTED").exists()


def test_workspace_git_wrapper_is_not_selected(repo: Path, monkeypatch):
    wrapper = repo / ("git.exe" if os.name == "nt" else "git")
    wrapper.write_text("this is not Git")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(repo) + os.pathsep + os.environ["PATH"])
    executable, path = module._git_executable(repo)
    assert Path(executable) != wrapper
    assert str(repo) not in path.split(os.pathsep)
    assert "initial read-tool fixture" in _read(repo, operation="log")["output"]


def test_large_output_is_stopped_and_explicitly_truncated(repo: Path, monkeypatch):
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 2048)
    (repo / "source.txt").write_text("changed\n" * 20000, encoding="utf-8")
    result = _read(repo, operation="diff")
    assert result["truncated"] is True
    assert len(result["output"].encode("utf-8")) <= 2048
    assert "query was stopped" in result["hint"]


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_terminate_running_process(repo: Path, monkeypatch, cancel: bool):
    real_create = asyncio.create_subprocess_exec
    created = []

    async def slow_create(*args, **kwargs):
        process = await real_create(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_create)
    monkeypatch.setattr(module, "QUERY_TIMEOUT_SECONDS", 0.2)

    async def run():
        task = asyncio.create_task(GitReadTool().execute({"operation": "status"}, ToolContext("t", repo)))
        if cancel:
            while not created:
                await asyncio.sleep(0.01)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ToolError):
            await task
        assert created and all(process.returncode is not None for process in created)

    asyncio.run(run())
