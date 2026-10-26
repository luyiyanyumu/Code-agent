"""Editor saves: fixture-only latency, ownership, conflicts and atomic writes."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from code_agent.api.code_api import mount_code_api
from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel import service as S
from code_agent.codeintel.hashing import sha1_file_text, sha1_text
from code_agent.codeintel.paths import root_key
from code_agent.codeintel.projects import ProjectService
from code_agent.codeintel.service import CodeIntelService


class FakeRepo:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = {row["id"]: row for row in rows}
        self.summaries: dict[str, dict] = {}

    def get(self, project_id: str) -> dict | None:
        return self.rows.get(project_id)

    def find_by_root(self, key: str) -> dict | None:
        return next((row for row in self.rows.values() if row["root_key"] == key), None)

    get_by_root = find_by_root

    def set_scan(self, project_id: str, summary_json: str) -> None:
        self.summaries[project_id] = json.loads(summary_json)


@pytest.fixture
def editor(tmp_path: Path, agent_home):
    roots = [tmp_path / "fixture 项目", tmp_path / "another-project"]
    rows = []
    for project_id, owner, root in zip(("p", "other"), ("owner", "another"), roots):
        root.mkdir()
        (root / "a.txt").write_bytes(b"original\r\n")
        (root / "source.py").write_bytes(b"def fixture():\n    return 1\n")
        rows.append({"id": project_id, "user_id": owner, "name": root.name,
                     "root_path": str(root), "root_key": root_key(root)})
    repo = FakeRepo(rows)
    intel = CodeIntelService(repo)
    release_events: list[threading.Event] = []

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            for event in release_events:
                event.set()
            await intel.aclose()

    app = FastAPI(lifespan=lifespan)

    def current_user(request: Request) -> CurrentUser:
        owner = request.headers.get("X-Fixture-User", "owner")
        return CurrentUser(user_id=owner, username=owner, role="user")

    app.dependency_overrides[get_current_user] = current_user
    mount_code_api(app, project_repo=repo, service=ProjectService(repo),
                   manager=SimpleNamespace(), intel=intel)
    with TestClient(app) as client:
        try:
            yield SimpleNamespace(client=client, root=roots[0], other_root=roots[1],
                                  target=roots[0] / "a.txt", repo=repo, intel=intel,
                                  project=rows[0], release_events=release_events)
        finally:
            for event in release_events:
                event.set()


def read(editor, *, path="a.txt", project_id="p", **kwargs):
    response = editor.client.get(f"/api/v1/code/projects/{project_id}/file",
                                 params={"path": path}, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def save(editor, content="updated\n", *, expected_sha1=None, project_id="p", **kwargs):
    body = {"path": "a.txt", "content": content, "session_id": "fixture-editor"}
    if expected_sha1 is not None:
        body["expected_sha1"] = expected_sha1
    return editor.client.put(f"/api/v1/code/projects/{project_id}/file", json=body, **kwargs)


def wait_refresh(editor, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = editor.intel.refresh_status("p")
        if predicate(status):
            return status
        time.sleep(0.01)
    raise AssertionError(f"refresh did not reach expected state: {editor.intel.refresh_status('p')}")


def block_scan(editor, monkeypatch):
    started, release, finished = (threading.Event() for _ in range(3))
    editor.release_events.append(release)
    original_scan = editor.intel._scan_sync

    def blocked(project, full, tree_id):
        started.set()
        try:
            if not release.wait(10):
                raise TimeoutError("fixture scan was not released")
            return original_scan(project, full, tree_id)
        finally:
            finished.set()

    monkeypatch.setattr(editor.intel, "_scan_sync", blocked)
    return started, release, finished


def test_large_save_returns_while_index_refresh_is_still_running(editor, monkeypatch):
    before = read(editor)
    content = "x" * (174 * 1024 - 1) + "\n"
    started, release, finished = block_scan(editor, monkeypatch)
    pool = ThreadPoolExecutor(max_workers=1)
    pending = pool.submit(save, editor, content, expected_sha1=before["sha1"])
    try:
        response = pending.result(timeout=3)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["ok"] is True and result["changed"] is True
        assert result["sha1"] == sha1_text(content) == sha1_file_text(editor.target)
        assert result["lines"] == 2 and result["path"] == "a.txt" and result["patch_id"]
        assert editor.target.read_text(encoding="utf-8") == content
        assert started.wait(3), "saving must enqueue the index refresh"
        assert not finished.is_set(), "saving must not wait for the whole project index"
        status = editor.intel.refresh_status("p")
        assert status["pending"] is True
        assert status["requested_generation"] > status["completed_generation"]
        assert isinstance(result["index_refresh"], dict)
        patches = editor.intel.list_patches(editor.project)["patches"]
        assert len(patches) == 1 and patches[0]["tool"] == "editor_save"
        assert patches[0]["session_id"] == "fixture-editor"
    finally:
        release.set()
        pending.result(timeout=5)
        pool.shutdown()
    status = wait_refresh(editor, lambda status: not status["pending"])
    assert status["requested_generation"] == status["completed_generation"]
    assert not status["error"]


def test_no_op_keeps_crlf_bytes_and_skips_patch_and_index_work(editor, monkeypatch):
    opened = read(editor)
    original = editor.target.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged save must not schedule an index refresh")

    monkeypatch.setattr(editor.intel, "request_refresh", forbidden)
    response = save(editor, opened["text"], expected_sha1=opened["sha1"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] and result["changed"] is False
    assert result["sha1"] == opened["sha1"] and editor.target.read_bytes() == original
    assert editor.intel.list_patches(editor.project)["counts"]["total"] == 0
    assert editor.repo.summaries == {}


@pytest.mark.parametrize("expected", [None, "returned", "stale"])
@pytest.mark.parametrize("same_text", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_non_utf8_save_is_rejected_even_without_hash_or_for_no_op(editor, expected, same_text, mixed):
    original = "旧编码中文\r\n".encode("gb18030")
    if mixed:
        original = "UTF8 中文\n".encode("utf-8") + original
    editor.target.write_bytes(original)
    opened = read(editor)
    assert opened["editable"] is False and opened["readonly_reason"] == "encoding"
    baseline = opened["sha1"] if expected == "returned" else ("0" * 40 if expected == "stale" else None)
    response = save(editor, opened["text"] if same_text else "must not overwrite\n", expected_sha1=baseline)
    assert response.status_code == 422, response.text
    assert "UTF-8" in response.json()["detail"]
    assert editor.target.read_bytes() == original
    assert editor.intel.list_patches(editor.project)["counts"]["total"] == 0
    assert editor.repo.summaries == {}
    assert editor.intel.refresh_status("p")["pending"] is False


def test_crlf_hash_matches_read_and_stale_save_returns_conflict(editor):
    opened = read(editor)
    assert opened["text"] == "original\n"
    assert opened["sha1"] == sha1_text(opened["text"])
    content = "updated 中文\n"
    response = save(editor, content, expected_sha1=opened["sha1"])
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["sha1"] == sha1_text(content) == read(editor)["sha1"]
    conflict = save(editor, "must not overwrite\n", expected_sha1=opened["sha1"])
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert detail["expected_sha1"] == opened["sha1"]
    assert detail["current_sha1"] == saved["sha1"]
    assert editor.target.read_text(encoding="utf-8") == content
    assert editor.intel.list_patches(editor.project)["counts"]["total"] == 1


def test_editor_save_is_owner_scoped(editor):
    original = editor.other_root.joinpath("a.txt").read_bytes()
    response = save(editor, "unauthorized\n", project_id="other")
    assert response.status_code == 404
    response = save(editor, "unauthorized\n", headers={"X-Fixture-User": "another"})
    assert response.status_code == 404
    assert editor.other_root.joinpath("a.txt").read_bytes() == original
    assert editor.target.read_bytes() == original
    assert editor.repo.summaries == {}
    assert editor.intel.list_patches(editor.project)["counts"]["total"] == 0


@pytest.mark.parametrize("body", [
    {}, {"path": "a.txt"}, {"path": "a.txt", "content": None},
    {"path": {}, "content": "bad"}, {"path": 12, "content": "bad"},
    {"path": "a.txt", "content": 12}, {"path": "a.txt", "content": ["bad"]},
    {"path": "a.txt", "content": "bad", "expected_sha1": False},
    {"path": "a.txt", "content": "bad", "expected_sha1": {}},
    {"path": "a.txt", "content": "bad", "session_id": False},
    {"path": "a.txt", "content": "bad", "unexpected": True},
    {"path": "a\x00.txt", "content": "bad"},
    {"path": "../outside.txt", "content": "bad"},
])
def test_invalid_editor_save_never_writes_or_enqueues(editor, body):
    original = editor.target.read_bytes()
    response = editor.client.put("/api/v1/code/projects/p/file", json=body)
    assert response.status_code == 422, response.text
    assert editor.target.read_bytes() == original
    assert editor.repo.summaries == {}
    assert editor.intel.list_patches(editor.project)["counts"]["total"] == 0
    assert editor.intel.refresh_status("p")["pending"] is False


def test_two_concurrent_saves_with_one_expected_hash_only_write_once(editor, monkeypatch):
    expected = read(editor)["sha1"]
    _, release, _ = block_scan(editor, monkeypatch)
    gate = threading.Barrier(2)

    def concurrent(content):
        gate.wait(timeout=3)
        return content, save(editor, content, expected_sha1=expected)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(concurrent, value) for value in ("first\n", "second\n")]
            results = [future.result(timeout=3) for future in pending]
        assert sorted(response.status_code for _, response in results) == [200, 409]
        winner = next(content for content, response in results if response.status_code == 200)
        assert editor.target.read_text(encoding="utf-8") == winner
        assert editor.intel.list_patches(editor.project)["counts"]["total"] == 1
    finally:
        release.set()


def test_index_failure_does_not_report_saved_file_as_failed(editor, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("fixture index unavailable")

    monkeypatch.setattr(editor.intel, "_scan_sync", broken)
    response = save(editor, "saved despite index failure\n", expected_sha1=read(editor)["sha1"])
    assert response.status_code == 200, response.text
    assert response.json()["ok"] and response.json()["changed"]
    assert editor.target.read_text(encoding="utf-8") == "saved despite index failure\n"
    assert editor.intel.list_patches(editor.project)["counts"]["total"] == 1
    status = wait_refresh(editor, lambda status: status["state"] == "error")
    assert not status["pending"] and "fixture index unavailable" in status["error"]


def test_external_change_during_temp_preparation_is_preserved(editor, monkeypatch):
    expected = read(editor)["sha1"]
    original_mkstemp = S.tempfile.mkstemp
    prepared = threading.Event()

    def race(*args, **kwargs):
        result = original_mkstemp(*args, **kwargs)
        editor.target.write_bytes(b"external edit during preparation\r\n")
        prepared.set()
        return result

    monkeypatch.setattr(S.tempfile, "mkstemp", race)
    response = save(editor, "must not overwrite\n", expected_sha1=expected)
    assert prepared.is_set(), "save must stage its write before replacing the existing file"
    assert response.status_code == 409, response.text
    assert editor.target.read_bytes() == b"external edit during preparation\r\n"
    assert {path.name for path in editor.root.iterdir()} == {"a.txt", "source.py"}
    assert editor.intel.list_patches(editor.project)["counts"]["total"] == 0
    assert editor.intel.refresh_status("p")["pending"] is False


def test_atomic_replace_failure_keeps_original_file_and_cleans_temp(editor, monkeypatch):
    expected = read(editor)["sha1"]
    original = editor.target.read_bytes()
    replace = S.os.replace
    attempted = threading.Event()

    def broken(source, destination, *args, **kwargs):
        if Path(destination) == editor.target:
            attempted.set()
            raise OSError("fixture replace unavailable")
        return replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(S.os, "replace", broken)
    response = save(editor, "must not destroy original\n", expected_sha1=expected)
    assert attempted.is_set()
    assert 400 <= response.status_code < 600, response.text
    assert editor.target.read_bytes() == original
    assert {path.name for path in editor.root.iterdir()} == {"a.txt", "source.py"}
    assert editor.intel.list_patches(editor.project)["counts"]["total"] == 0
    assert editor.intel.refresh_status("p")["pending"] is False


def test_index_poll_and_busy_scan_leave_save_responsive_under_index_lock(editor, monkeypatch):
    started, release = threading.Event(), threading.Event()
    editor.release_events.append(release)
    before = read(editor)["sha1"]
    known = editor.intel.status(editor.project)
    index = editor.intel.index("p")
    original = editor.intel._scan_sync

    def locked(*args):
        with index._lock:
            started.set()
            assert release.wait(5)
            return original(*args)

    monkeypatch.setattr(editor.intel, "_scan_sync", locked)
    editor.intel.request_refresh(editor.project)
    assert started.wait(3)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            poll = pool.submit(editor.client.get, "/api/v1/code/projects/p/index").result(timeout=2)
            assert poll.status_code == 200 and poll.json()["index_refresh"]["pending"]
            assert poll.json()["version"] == known["version"] and poll.json()["status_partial"]
            # A newly started backend also reports progress without waiting
            # for the first completed counts snapshot.
            with editor.intel._guard:
                editor.intel._scan_cache.clear()
            cold = pool.submit(editor.client.get, "/api/v1/code/projects/p/index").result(timeout=2)
            assert cold.status_code == 200 and cold.json()["version"] is None
            assert cold.json()["index_refresh"]["pending"] and cold.json()["status_partial"]
            busy = pool.submit(editor.client.post, "/api/v1/code/projects/p/scan")
            saving = pool.submit(save, editor, "saved while index is locked\n", expected_sha1=before)
            busy_response, saved_response = busy.result(timeout=2), saving.result(timeout=2)
            assert busy_response.status_code == 200 and busy_response.json()["status"] == "busy"
            assert saved_response.status_code == 200 and saved_response.json()["ok"]
            assert saved_response.json()["index_refresh"]["requested_generation"] == 2
            assert editor.target.read_text(encoding="utf-8") == "saved while index is locked\n"
    finally:
        release.set()
    status = wait_refresh(editor, lambda state: not state["pending"])
    assert status["completed_generation"] == status["requested_generation"] == 2
