"""Review API and filesystem/DB failure boundaries, entirely fixture-based."""
import asyncio
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent.api.change_review_api import MAX_PROPOSAL_BODY, mount_change_review_api
from code_agent.api.security import create_token
from code_agent.codeintel import change_review as C
from code_agent.codeintel.change_review import ChangeReviewService
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.propose_edit import ProposeEditTool
from code_agent.tools.fs_observation import FsObservation


@pytest.fixture
def api(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_bytes(b"original\n")
    projects = {id: {"id": id, "root_path": str(root), "user_id": "owner"} for id in ("p", "q")}
    projects["other"] = {"id": "other", "root_path": str(root), "user_id": "another"}

    def owned(user, pid):
        if pid not in projects or projects[pid]["user_id"] != user:
            raise KeyError("not owned")
        return projects[pid]
    patches, scans = [], []
    async def scan(project):
        scans.append(project["id"])
        return {}
    intel = SimpleNamespace(record_patch=lambda **kw: patches.append(kw) or {"id": str(len(patches))}, scan=scan,
                            project_for_workspace=lambda ws, uid: projects["p"] if uid == "owner" and Path(ws) == root else None)
    reviews = ChangeReviewService(intel, tmp_path / "review-store")
    intel.reviews = reviews
    app = FastAPI()
    app.state.auth_enabled = True
    mount_change_review_api(app, service=SimpleNamespace(get_owned=owned), reviews=reviews, intel=intel)
    headers = {"Authorization": "Bearer " + create_token("owner", "owner", "admin")}
    other = {"Authorization": "Bearer " + create_token("another", "another", "user")}
    with TestClient(app) as client:
        try:
            yield SimpleNamespace(client=client, root=root, reviews=reviews, project=projects["p"], intel=intel,
                                  headers=headers, other=other, scans=scans, patches=patches)
        finally:
            reviews.close()


def create(api, body=None):
    response = api.client.post("/api/v1/code/projects/p/change-proposals", headers=api.headers,
                               json=body or {"path": "a.txt", "content": "updated\n"})
    assert response.status_code == 200, response.text
    return response.json()


def decide(api, id, action, body=None):
    return api.client.post(f"/api/v1/code/projects/p/change-proposals/{id}/{action}", headers=api.headers, json=body or {})


class CommitFailure:
    def __init__(self, conn, callback=None):
        self.conn, self.callback = conn, callback

    def __getattr__(self, key):
        return getattr(self.conn, key)

    def commit(self):
        if self.callback:
            self.callback()
        raise sqlite3.OperationalError("fixture commit failure")


def fail_commit(api, callback=None):
    conn = api.reviews._store("p")
    proxy = CommitFailure(conn, callback)
    api.reviews._stores["p"] = proxy
    return conn


def test_create_only_stores_proposal_and_owner_scoping(api):
    p = create(api)
    assert p["state"] == "pending" and api.root.joinpath("a.txt").read_bytes() == b"original\n"
    assert api.scans == [] and api.patches == []
    base = "/api/v1/code/projects/p/change-proposals"
    assert api.client.get(base, headers=api.headers).json()["proposals"][0]["id"] == p["id"]
    assert api.client.get(base).status_code == 401
    assert api.client.post(base, json={"path": "a.txt", "content": "bad"}, headers=api.other).status_code == 404
    for method, path in (("get", base), ("get", base + "/" + p["id"]), ("post", base + "/" + p["id"] + "/apply")):
        kwargs = {"headers": api.other, **({"json": {}} if method == "post" else {})}
        assert getattr(api.client, method)(path, **kwargs).status_code == 404
    assert api.client.get(base.replace("/p/", "/q/") + "/" + p["id"], headers=api.headers).status_code == 404
    assert api.client.get(base.replace("/p/", "/other/"), headers=api.headers).status_code == 404


def test_hunk_apply_reject_rollback_and_scan(api):
    before = "".join(f"line {i}\n" for i in range(30))
    api.root.joinpath("a.txt").write_text(before, encoding="utf-8")
    p = create(api, {"path": "a.txt", "content": before.replace("line 1\n", "changed\n").replace("line 28\n", "last\n")})
    assert p["counts"]["total"] == 2
    assert decide(api, p["id"], "apply", {"hunk_ids": ["h2"]}).json()["changed"]
    result = decide(api, p["id"], "reject", {"hunk_ids": ["h1"]}).json()
    assert not result["changed"] and result["proposal"]["state"] == "reviewed"
    assert api.root.joinpath("a.txt").read_text() == before.replace("line 28\n", "last\n")
    result = decide(api, p["id"], "rollback").json()
    assert result["proposal"]["state"] == "reverted" and api.root.joinpath("a.txt").read_text() == before
    assert api.scans == ["p", "p"] and len(api.patches) == 2
    assert decide(api, p["id"], "apply").status_code == 409


def test_new_file_create_apply_rollback_crlf(api):
    p = create(api, {"path": "tests/new_test.py", "content": "value = 1\r\n"})
    assert p["new_file"] and not api.root.joinpath("tests").exists()
    result = decide(api, p["id"], "apply")
    assert result.status_code == 200 and api.root.joinpath(p["path"]).read_text() == "value = 1\n"
    result = decide(api, p["id"], "rollback")
    assert result.status_code == 200 and not api.root.joinpath(p["path"]).exists()


def test_scan_failure_reports_actual_saved_success(api):
    async def broken(project):
        raise OSError("fixture scan unavailable")
    api.intel.scan = broken
    p = create(api)
    result = decide(api, p["id"], "apply")
    assert result.status_code == 200
    assert result.json()["changed"] and "索引" in result.json()["warning"]
    assert result.json()["proposal"]["state"] == "applied"
    assert api.root.joinpath("a.txt").read_text() == "updated\n"


@pytest.mark.parametrize("body", [
    {"path": "../outside", "content": "bad"}, {"path": "", "content": "bad"},
    {"path": "a.txt", "content": "x" * (C.MAX_TEXT + 1)},
    {"path": "a.txt", "content": "bad", "expected_sha1": "invalid"},
    {"path": "a.txt", "content": "bad\x00data"},
    {"path": "a.txt", "edits": []}, {"path": "new.txt", "edits": [{"old_string": "x", "new_string": "y"}]},
])
def test_invalid_proposals_never_write(api, body):
    result = api.client.post("/api/v1/code/projects/p/change-proposals", headers=api.headers, json=body)
    assert result.status_code == 422
    assert api.root.joinpath("a.txt").read_bytes() == b"original\n"
    assert api.patches == [] and api.scans == []


def test_request_body_limits_bad_json_and_decision_validation(api):
    base = "/api/v1/code/projects/p/change-proposals"
    for value in ([], "not-json", 7):
        assert api.client.post(base, headers=api.headers, json=value).status_code == 422
    assert api.client.post(base, headers=api.headers, content=b"{").status_code == 422
    result = api.client.post(base, headers=api.headers, content=b"x" * (MAX_PROPOSAL_BODY + 1))
    assert result.status_code == 413
    p = create(api)
    for body in ({"hunk_ids": "h1"}, {"hunk_ids": [3]}, {"hunk_ids": ["x" * 33]}):
        assert decide(api, p["id"], "apply", body).status_code == 422
    assert decide(api, p["id"], "apply", {"hunk_ids": ["missing"]}).status_code == 409
    assert decide(api, p["id"], "unknown").status_code == 404


def test_commit_failure_restores_exact_bytes_and_pending_state(api):
    p = create(api)
    conn = fail_commit(api)
    result = decide(api, p["id"], "apply")
    assert result.status_code == 503 and "恢复" in result.text
    assert api.root.joinpath("a.txt").read_bytes() == b"original\n"
    assert api.reviews.detail(api.project, p["id"])["state"] == "pending"
    assert api.scans == [] and api.patches == []
    api.reviews._stores["p"] = conn
    assert decide(api, p["id"], "apply").status_code == 200


def test_commit_failure_does_not_overwrite_external_content(api):
    p = create(api)
    fail_commit(api, lambda: api.root.joinpath("a.txt").write_text("external edit", encoding="utf-8"))
    result = decide(api, p["id"], "apply")
    assert result.status_code == 503 and "保留" in result.text
    assert api.root.joinpath("a.txt").read_text() == "external edit"
    assert api.reviews.detail(api.project, p["id"])["state"] == "pending"


@pytest.mark.parametrize("rollback", [False, True])
def test_new_file_commit_failure_restores_existence(api, rollback):
    p = create(api, {"path": "new.py", "content": "value = 1\n"})
    target = api.root / "new.py"
    if rollback:
        assert decide(api, p["id"], "apply").status_code == 200
    fail_commit(api)
    result = decide(api, p["id"], "rollback" if rollback else "apply")
    assert result.status_code == 503 and target.exists() == rollback
    assert api.reviews.detail(api.project, p["id"])["state"] == ("applied" if rollback else "pending")
    if rollback:
        assert target.read_text() == "value = 1\n"


def test_create_commit_failure_is_rolled_back_without_writing(api):
    conn = api.reviews._store("p")
    api.reviews._stores["p"] = CommitFailure(conn)
    response = api.client.post("/api/v1/code/projects/p/change-proposals", headers=api.headers,
                               json={"path": "new.py", "content": "proposed"})
    assert response.status_code == 503
    assert api.reviews.list(api.project)["proposals"] == [] and not api.root.joinpath("new.py").exists()


def test_existing_file_changed_during_temp_write_is_preserved(api, monkeypatch):
    p = create(api)
    fsync = C.os.fsync
    changed = False
    def race(fd):
        nonlocal changed
        fsync(fd)
        if not changed:
            changed = True
            api.root.joinpath("a.txt").write_text("external during preparation", encoding="utf-8")
    monkeypatch.setattr(C.os, "fsync", race)
    result = decide(api, p["id"], "apply")
    assert result.status_code == 409
    assert api.root.joinpath("a.txt").read_text() == "external during preparation"
    assert not list(api.root.glob(".*.tmp")) and api.scans == []


def test_new_file_o_excl_wins_creation_race(api, monkeypatch):
    p = create(api, {"path": "new.py", "content": "proposed"})
    target, open_fd = api.root / "new.py", C.os.open
    def race(path, flags, *args, **kwargs):
        if Path(path) == target and flags & os.O_EXCL:
            target.write_text("external creator", encoding="utf-8")
        return open_fd(path, flags, *args, **kwargs)
    monkeypatch.setattr(C.os, "open", race)
    result = decide(api, p["id"], "apply")
    assert result.status_code == 409 and target.read_text() == "external creator"
    assert api.reviews.detail(api.project, p["id"])["state"] == "pending"


def test_failed_new_file_write_removes_only_its_partial_file(api, monkeypatch):
    p = create(api, {"path": "new.py", "content": "proposed"})
    def broken(fd):
        raise OSError("fixture fsync failure")
    monkeypatch.setattr(C.os, "fsync", broken)
    assert decide(api, p["id"], "apply").status_code == 422
    assert not api.root.joinpath("new.py").exists()
    assert api.reviews.detail(api.project, p["id"])["state"] == "pending"


def test_concurrent_apply_one_write_only(api):
    p = create(api)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decide(api, p["id"], "apply"), range(2)))
    assert sorted(result.status_code for result in results) == [200, 409]
    assert api.root.joinpath("a.txt").read_text() == "updated\n"
    assert api.scans == ["p"] and len(api.patches) == 1


def test_listing_never_decodes_full_diff_and_cache_status_is_fresh(api, monkeypatch):
    p = create(api)
    cached = api.reviews._hunk_cache[("p", p["id"])][0]
    assert "replacement" in cached[0] and "status" not in cached[0]
    decode = api.reviews._decode_hunks
    def forbidden(*args):
        raise AssertionError("listing must not decode full diff")
    monkeypatch.setattr(api.reviews, "_decode_hunks", forbidden)
    listing = api.client.get("/api/v1/code/projects/p/change-proposals", headers=api.headers).json()
    assert listing["proposals"][0]["counts"]["pending"] == 1
    assert "hunks" not in listing["proposals"][0]
    monkeypatch.setattr(api.reviews, "_decode_hunks", decode)
    assert decide(api, p["id"], "apply").json()["proposal"]["hunks"][0]["status"] == "applied"
    assert "replacement" in cached[0] and "status" not in cached[0]
    assert api.reviews._cache_bytes <= C.MAX_HUNK_CACHE_BYTES


def test_ai_observation_refusal_inside_snapshot_and_absolute_path(api):
    path = api.root / "a.txt"
    tool = ProposeEditTool(api.intel)
    observation = FsObservation()
    context = ToolContext(session_id="s", workspace=api.root, user_id="owner", fs_observation=observation)
    body = {"path": str(path), "edits": [{"old_string": "original", "new_string": "AI suggested"}]}
    with pytest.raises(ToolError, match="read"):
        asyncio.run(tool.execute(body, context))
    observation.record_read(path)
    result = json.loads(asyncio.run(tool.execute(body, context)))
    assert result["path"] == "a.txt" and path.read_bytes() == b"original\n"
    assert api.reviews.detail(api.project, result["proposal_id"])["source"] == "ai"
    calls = 0
    def fresh(target):
        nonlocal calls
        calls += 1
        return "changed since read" if calls > 1 else None
    observation.check_edit = fresh
    with pytest.raises(ToolError, match="changed since read"):
        asyncio.run(tool.execute(body, context))
    assert len(api.reviews.list(api.project)["proposals"]) == 1


def test_reopen_backfills_legacy_summary_columns(api):
    p = create(api)
    conn = api.reviews._store("p")
    conn.execute("UPDATE proposals SET before_sha1='',after_sha1='',hunk_count=0")
    conn.commit()
    directory = api.reviews.directory
    api.reviews.close()
    reopened = ChangeReviewService(api.intel, directory)
    try:
        listed = reopened.list(api.project)["proposals"][0]
        assert listed["counts"]["total"] == 1 and len(listed["before_sha1"]) == 40
        assert reopened.detail(api.project, p["id"])["hunks"][0]["status"] == "pending"
    finally:
        reopened.close()


def test_large_repeated_diff_is_bounded_before_json_materialization(api):
    api.root.joinpath("a.txt").write_bytes(b"x\n" * 20000)
    result = api.client.post("/api/v1/code/projects/p/change-proposals", headers=api.headers,
                             json={"path": "a.txt", "content": "y\n" * 20000})
    assert result.status_code == 422 and "20000" in result.text
    assert api.reviews.list(api.project)["proposals"] == []


def test_partial_apply_then_rollback_terminates_pending_hunks(api):
    before = "".join(f"line {i}\n" for i in range(30))
    api.root.joinpath("a.txt").write_text(before, encoding="utf-8")
    p = create(api, {"path": "a.txt", "content": before.replace("line 1\n", "first\n").replace("line 28\n", "last\n")})
    applied = decide(api, p["id"], "apply", {"hunk_ids": ["h1"]}).json()["proposal"]
    assert applied["counts"]["pending"] == 1
    reverted = decide(api, p["id"], "rollback").json()["proposal"]
    assert reverted["state"] == "reverted" and reverted["counts"]["pending"] == 0
    assert reverted["counts"]["applied"] == 1
    assert [h["status"] for h in reverted["hunks"]] == ["reverted", "reverted"]
    base = "/api/v1/code/projects/p/change-proposals"
    assert api.client.get(base + "/" + p["id"], headers=api.headers).json()["counts"]["pending"] == 0
    assert api.client.get(base, headers=api.headers).json()["proposals"][0]["counts"]["pending"] == 0
    assert decide(api, p["id"], "apply", {"hunk_ids": ["h2"]}).status_code == 409
    assert api.root.joinpath("a.txt").read_text() == before


def test_resource_batch_api_is_owner_scoped_and_reviewed_whole(api):
    base = "/api/v1/code/projects/p/change-batches"
    body = {"title": "Rename file", "files": [
        {"path": "a.txt", "before_exists": True, "before_text": "original\n", "after_exists": False, "after_text": ""},
        {"path": "renamed.txt", "before_exists": False, "before_text": "", "after_exists": True, "after_text": "original\n", "mode_from": "a.txt"},
    ]}
    assert api.client.post(base, headers=api.other, json=body).status_code == 404
    created = api.client.post(base, headers=api.headers, json=body)
    assert created.status_code == 200, created.text
    batch = created.json()
    assert batch["state"] == "pending" and api.root.joinpath("a.txt").exists()
    assert not api.root.joinpath("renamed.txt").exists() and api.patches == []
    assert api.client.get(base, headers=api.headers).json()["batches"][0]["id"] == batch["id"]
    assert api.client.get(base + "/" + batch["id"], headers=api.other).status_code == 404
    assert api.client.get(base.replace("/p/", "/q/") + "/" + batch["id"], headers=api.headers).status_code == 404
    path = base + "/" + batch["id"]
    assert api.client.post(path + "/apply", headers=api.headers, json={"hunk_ids": ["h1"]}).status_code == 422
    applied = api.client.post(path + "/apply", headers=api.headers, json={})
    assert applied.status_code == 200, applied.text
    assert applied.json()["paths"] == ["a.txt", "renamed.txt"]
    assert not api.root.joinpath("a.txt").exists() and api.root.joinpath("renamed.txt").read_bytes() == b"original\n"
    assert api.client.post(path + "/apply", headers=api.headers, json={}).status_code == 409
    restored = api.client.post(path + "/rollback", headers=api.headers, json={})
    assert restored.status_code == 200 and restored.json()["batch"]["state"] == "reverted"
    assert api.root.joinpath("a.txt").read_bytes() == b"original\n" and not api.root.joinpath("renamed.txt").exists()


def test_resource_batch_conflict_validation_and_persistence_errors(api):
    base = "/api/v1/code/projects/p/change-batches"
    file = {"path": "a.txt", "before_exists": True, "before_text": "original\n", "after_exists": True, "after_text": "changed\n"}
    for body in ({"files": []}, {"files": [dict(file, path="../outside")]}, {"files": [dict(file, before_exists=1)]}):
        assert api.client.post(base, headers=api.headers, json=body).status_code == 422
    created = api.client.post(base, headers=api.headers, json={"files": [file]}).json()
    path = base + "/" + created["id"]
    api.root.joinpath("a.txt").write_text("external", encoding="utf-8")
    assert api.client.post(path + "/apply", headers=api.headers, json={}).status_code == 409
    api.root.joinpath("a.txt").write_bytes(b"original\n")
    conn = fail_commit(api)
    result = api.client.post(path + "/apply", headers=api.headers, json={})
    assert result.status_code == 503 and "恢复" in result.text
    assert api.root.joinpath("a.txt").read_bytes() == b"original\n"
    assert api.reviews.detail_batch(api.project, created["id"])["state"] == "pending"
    api.reviews._stores["p"] = conn


def test_resource_batch_queues_new_index_generation_without_waiting_for_scan(api):
    queued = []
    def queue(project):
        queued.append(project["id"])
        return {"state": "running", "pending": True, "requested_generation": len(queued), "completed_generation": 0}
    api.intel.request_refresh = queue
    async def forbidden(project):
        raise AssertionError("batch commit must queue a new generation instead of joining or waiting for a running scan")
    api.intel.scan = forbidden
    base = "/api/v1/code/projects/p/change-batches"
    created = api.client.post(base, headers=api.headers, json={"files": [{"path": "new.py", "before_exists": False, "before_text": "", "after_exists": True, "after_text": "value = 1\n"}]}).json()
    path = base + "/" + created["id"]
    for generation, action in enumerate(("apply", "rollback"), 1):
        result = api.client.post(path + "/" + action, headers=api.headers, json={})
        assert result.status_code == 200 and result.json()["index_refresh"]["requested_generation"] == generation
    assert queued == ["p", "p"] and api.scans == []


def test_index_queue_failure_cannot_misreport_a_committed_resource_batch(api):
    def broken(project):
        raise RuntimeError("fixture unavailable index queue")
    api.intel.request_refresh = broken
    base = "/api/v1/code/projects/p/change-batches"
    created = api.client.post(base, headers=api.headers, json={"files": [{"path": "new.py", "before_exists": False, "before_text": "", "after_exists": True, "after_text": "value = 1\n"}]}).json()
    result = api.client.post(base + "/" + created["id"] + "/apply", headers=api.headers, json={})
    assert result.status_code == 200 and result.json()["changed"] and "已保存" in result.json()["warning"]
    assert result.json()["batch"]["state"] == "applied" and api.root.joinpath("new.py").exists()
