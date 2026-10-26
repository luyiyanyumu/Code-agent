import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_agent.codeintel import change_review as C
from code_agent.codeintel import workspace_refactor as W


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    project = {"id": "p", "root_path": str(root)}
    records = []
    intel = SimpleNamespace(record_patch=lambda **kw: records.append(kw) or {"id": str(len(records))})
    service = C.ChangeReviewService(intel, tmp_path / "reviews")
    yield root, project, service, records, intel
    service.close()


def file(path, before, after, **extra):
    return {"path": path, "before_exists": before is not None, "before_text": before or "", "after_exists": after is not None, "after_text": after or "", **extra}


def batch(service, project, files):
    return service.create_batch(project, {"title": "Extract class", "files": files})


def test_mixed_transaction_preview_apply_reopen_rollback_preserves_raw_bytes(fixture):
    root, project, service, records, intel = fixture
    (root / "old.java").write_bytes(b"class A {}\r\n")
    (root / "caller.java").write_bytes(b"A value;\n")
    (root / "obsolete.java").write_bytes(b"obsolete\r\n")
    mode = (root / "old.java").stat().st_mode & 0o777
    proposal = batch(service, project, [file("old.java", "class A {}\n", None), file("new.java", None, "class A {}\n", mode_from="old.java"), file("caller.java", "A value;\n", "A next;\n"), file("obsolete.java", "obsolete\n", None), file("empty.txt", None, "")])
    assert proposal["file_count"] == 5 and proposal["state"] == "pending"
    assert (root / "old.java").exists() and not (root / "new.java").exists()
    result = service.decide_batch(project, proposal["id"], "apply")
    assert result["changed"] and result["batch"]["state"] == "applied"
    assert (root / "new.java").read_bytes() == b"class A {}\r\n"
    assert (root / "new.java").stat().st_mode & 0o777 == mode
    assert not (root / "old.java").exists() and not (root / "obsolete.java").exists()
    assert (root / "empty.txt").exists()
    service.close()
    reopened = C.ChangeReviewService(intel, service.directory)
    try:
        assert reopened.list_batches(project)["batches"][0]["file_count"] == 5
        result = reopened.decide_batch(project, proposal["id"], "rollback")
        assert result["batch"]["state"] == "reverted"
        assert (root / "old.java").read_bytes() == b"class A {}\r\n"
        assert (root / "caller.java").read_bytes() == b"A value;\n"
        assert (root / "obsolete.java").read_bytes() == b"obsolete\r\n"
        assert not (root / "new.java").exists() and not (root / "empty.txt").exists()
    finally:
        reopened.close()
    assert len(records) == 10


@pytest.mark.parametrize("rollback", [False, True])
def test_one_conflict_blocks_entire_batch_before_any_write(fixture, rollback):
    root, project, service, records, _ = fixture
    (root / "a.py").write_bytes(b"a\n")
    (root / "b.py").write_bytes(b"b\n")
    proposal = batch(service, project, [file("a.py", "a\n", "new a\n"), file("b.py", "b\n", None)])
    if rollback:
        service.decide_batch(project, proposal["id"], "apply")
    (root / "b.py").write_bytes(b"external\n")
    before_a = (root / "a.py").read_bytes()
    with pytest.raises(C.ReviewConflict):
        service.decide_batch(project, proposal["id"], "rollback" if rollback else "apply")
    assert (root / "a.py").read_bytes() == before_a
    assert (root / "b.py").read_bytes() == b"external\n"
    assert len(records) == (2 if rollback else 0)


@pytest.mark.parametrize("files", [
    [file("../escape.txt", None, "bad")], [file(".git/config", None, "bad")], [file(".env", None, "bad")],
    [file("a.txt", None, "x"), file("a.txt", None, "y")],
    [file("a", None, "x"), file("a/b", None, "y")], [file("a.txt ", None, "x")],
    [file("nul.txt", None, "x\0y")], [file("big.txt", None, "x" * (C.MAX_TEXT + 1))],
    [file("a.txt", "wrong", "y")], [file("unchanged.txt", None, None)],
    [file("new.txt", None, "x", mode_from="outside.txt")],
    [file("CON.txt", None, "x")], [file("COM¹/log.txt", None, "x")],
])
def test_unsafe_batches_do_not_write_or_persist(fixture, files):
    root, project, service, _, _ = fixture
    with pytest.raises(ValueError):
        batch(service, project, files)
    assert service.list_batches(project)["batches"] == []
    assert list(root.iterdir()) == []


def test_directory_and_symlink_alias_rejected(fixture):
    root, project, service, _, _ = fixture
    (root / "directory").mkdir()
    with pytest.raises(ValueError, match="目录"):
        batch(service, project, [file("directory", "", None)])
    (root / "real.txt").write_text("real")
    try:
        (root / "alias.txt").symlink_to(root / "real.txt")
    except OSError:
        pytest.skip("host does not permit symlinks")
    with pytest.raises(ValueError, match="符号链接"):
        batch(service, project, [file("alias.txt", "real", None)])


class CommitFailure:
    def __init__(self, conn, callback=None):
        self.conn, self.callback = conn, callback

    def __getattr__(self, key):
        return getattr(self.conn, key)

    def commit(self):
        if self.callback:
            self.callback()
        raise sqlite3.OperationalError("fixture commit failure")


@pytest.mark.parametrize("rollback", [False, True])
def test_commit_failure_restores_every_path_and_state(fixture, rollback):
    root, project, service, _, _ = fixture
    (root / "old.txt").write_bytes(b"old\r\n")
    proposal = batch(service, project, [file("old.txt", "old\n", None), file("new.txt", None, "new\n")])
    if rollback:
        service.decide_batch(project, proposal["id"], "apply")
    conn = service._store("p")
    service._stores["p"] = CommitFailure(conn)
    with pytest.raises(C.ReviewPersistenceError, match="恢复"):
        service.decide_batch(project, proposal["id"], "rollback" if rollback else "apply")
    assert (root / "old.txt").exists() != rollback and (root / "new.txt").exists() == rollback
    assert service.detail_batch(project, proposal["id"])["state"] == ("applied" if rollback else "pending")
    if not rollback:
        assert (root / "old.txt").read_bytes() == b"old\r\n"
    service._stores["p"] = conn


def test_compensation_preserves_external_writer_instead_of_clobbering(fixture):
    root, project, service, _, _ = fixture
    (root / "a.txt").write_bytes(b"original\r\n")
    proposal = batch(service, project, [file("a.txt", "original\n", "changed\n"), file("new.txt", None, "created")])
    conn = service._store("p")
    service._stores["p"] = CommitFailure(conn, lambda: (root / "a.txt").write_text("external"))
    with pytest.raises(C.ReviewPersistenceError, match="外部改动"):
        service.decide_batch(project, proposal["id"], "apply")
    assert (root / "a.txt").read_text() == "external"
    assert not (root / "new.txt").exists()
    service._stores["p"] = conn


def test_post_chmod_snapshot_never_adopts_external_bytes_for_compensation(fixture, monkeypatch):
    root, _, _, _, _ = fixture
    own_versions = []
    original = W.os.chmod
    def race(target, mode):
        original(target, 0o666)
        Path(target).write_bytes(b"external after chmod")
    monkeypatch.setattr(W.os, "chmod", race)
    target = root / "new.txt"
    snapshot = {"data": W.base64.b64encode(b"ours").decode("ascii"), "mode": 0o444}
    W._write(root, "new.txt", target, snapshot, None, own_versions.append)
    with pytest.raises(C.ReviewConflict):
        C._assert_version(root, "new.txt", target, own_versions[-1])
    assert target.read_bytes() == b"external after chmod"


def test_collision_during_second_write_compensates_first_create(fixture, monkeypatch):
    root, project, service, _, _ = fixture
    proposal = batch(service, project, [file("first.txt", None, "first"), file("second.txt", None, "second")])
    original = C._create_bytes
    def racing(root, rel, target, data):
        if rel == "second.txt":
            target.write_text("external creator")
        return original(root, rel, target, data)
    monkeypatch.setattr(C, "_create_bytes", racing)
    with pytest.raises(C.ReviewConflict):
        service.decide_batch(project, proposal["id"], "apply")
    assert not (root / "first.txt").exists()
    assert (root / "second.txt").read_text() == "external creator"
    assert service.detail_batch(project, proposal["id"])["state"] == "pending"


def test_rejection_no_disk_mutation_and_double_decision_conflict(fixture):
    root, project, service, records, _ = fixture
    proposal = batch(service, project, [file("new.txt", None, "new")])
    assert service.decide_batch(project, proposal["id"], "reject")["changed"] is False
    assert records == [] and list(root.iterdir()) == []
    with pytest.raises(C.ReviewConflict):
        service.decide_batch(project, proposal["id"], "apply")


def test_concurrent_batch_decision_writes_once(fixture):
    root, project, service, records, _ = fixture
    proposal = batch(service, project, [file("new.txt", None, "new")])
    def apply(_):
        try:
            return service.decide_batch(project, proposal["id"], "apply")["batch"]["state"]
        except C.ReviewConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(apply, range(2))) == ["applied", "conflict"]
    assert len(records) == 1 and (root / "new.txt").read_text() == "new"


def test_batch_listing_does_not_materialize_full_file_snapshots(fixture, monkeypatch):
    _, project, service, _, _ = fixture
    proposal = batch(service, project, [file("new.txt", None, "x" * 100000)])
    decode = W.json.loads
    def bounded(value, *args, **kwargs):
        assert '"before"' not in value and '"after"' not in value
        return decode(value, *args, **kwargs)
    monkeypatch.setattr(W.json, "loads", bounded)
    listing = service.list_batches(project)
    assert listing["batches"][0]["id"] == proposal["id"]
    assert "files" not in listing["batches"][0]
