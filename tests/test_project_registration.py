"""Project registration boundaries; only temporary directories, no scan or models."""

import asyncio
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent.api.code_api import mount_code_api
from code_agent.api.security import create_token
from code_agent.codeintel import projects as P
from code_agent.codeintel.paths import root_key
from code_agent.codeintel.vcs import RepoInfo
from code_agent.storage.db import Database, ProjectRepo


@pytest.fixture
def registration(tmp_path, agent_home, monkeypatch):
    db = Database(tmp_path / "registration.db")
    repo = ProjectRepo(db)
    root = tmp_path / "项目 空间"
    root.mkdir()
    (root / "source.py").write_text("print('fixture')\n", encoding="utf-8")
    base = tmp_path / "created-projects"
    service = P.ProjectService(repo, roots=base)
    probes = []

    async def probe(path, *, with_status=False):
        probes.append((str(path), with_status))
        await asyncio.sleep(0)
        return RepoInfo(git_available=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("registration must not scan/ensure an index or call a model")

    monkeypatch.setattr(P, "probe_repo", probe)
    app = FastAPI()
    app.state.auth_enabled = True
    mount_code_api(app, project_repo=repo, service=service, manager=SimpleNamespace(),
                   intel=SimpleNamespace(scan=forbidden, ensure=forbidden))
    headers = {"Authorization": "Bearer " + create_token("owner", "owner", "admin")}
    other = {"Authorization": "Bearer " + create_token("other", "other", "user")}
    with TestClient(app) as client:
        try:
            yield SimpleNamespace(client=client, root=root, base=base, repo=repo, service=service,
                                  db=db, headers=headers, other=other, probes=probes)
        finally:
            db.close()


def open_project(api, **kwargs):
    return api.client.post("/api/v1/code/projects", headers=api.headers,
                           json={"path": str(api.root), **kwargs})


def create_project(api, name="new-project", **kwargs):
    return api.client.post("/api/v1/code/projects", headers=api.headers,
                           json={"mode": "create", "name": name, "git_init": False, **kwargs})


@pytest.mark.parametrize("changes", [
    {"mode": {}}, {"mode": False}, {"path": {}}, {"path": []}, {"path": 123}, {"path": None},
    {"path": "x\x00y"}, {"path": "x" * 4097}, {"name": {}}, {"name": 123},
    {"name": "a\nb"}, {"name": "x" * 81}, {"template": {}}, {"git_init": "false"},
    {"git_init": "true"}, {"git_init": 0}, {"git_init": 1}, {"unexpected": True},
    {"location": {}}, {"location": []}, {"location": None}, {"location": 1},
    {"location": False}, {"location": "x\x00y"}, {"location": "x" * 4097},
])
def test_invalid_json_fields_are_rejected_before_io(registration, changes):
    response = open_project(registration, **changes)
    assert response.status_code == 422, response.text
    assert registration.probes == []
    assert registration.repo.list_for_user("owner") == []
    assert not registration.base.exists()
    assert (registration.root / "source.py").read_text() == "print('fixture')\n"


def test_registration_normalizes_choices_and_quoted_unicode_path(registration):
    first = open_project(registration, mode=" OPEN ", path='"' + str(registration.root) + '"')
    assert first.status_code == 200, first.text
    second = open_project(registration, path=str(registration.root / "."))
    assert second.status_code == 200 and second.json()["id"] == first.json()["id"]
    assert Path(first.json()["root_path"]) == registration.root.resolve()
    created = create_project(registration, name="新建 项目", mode=" CREATE ", template=" PYTHON ")
    assert created.status_code == 200, created.text
    assert created.json()["template"] == "python"
    assert (registration.base / "新建 项目" / "src" / "main.py").is_file()
    assert not (registration.base / "新建 项目" / ".git").exists()
    assert all(with_status for _, with_status in registration.probes)


@pytest.mark.skipif(os.name != "nt", reason="Windows path case and separators")
def test_windows_path_spellings_use_one_owner_project(registration):
    first = open_project(registration).json()
    second = open_project(registration, path=str(registration.root).upper().replace("\\", "/") + "/")
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first["id"]
    assert len(registration.repo.list_for_user("owner")) == 1


def test_open_is_atomic_and_keeps_existing_metadata(registration):
    async def exercise():
        rows = await asyncio.gather(*(registration.service.open_project(
            "owner", registration.root, name=f"candidate-{i}"
        ) for i in range(24)))
        assert len({row["id"] for row in rows}) == 1
        pid = rows[0]["id"]
        registration.repo.rename(pid, "chosen name")
        registration.repo.set_session(pid, "stable-session")
        registration.repo.set_flags(pid, base_branch="release", allow_direct_write=True)
        registration.repo.set_scan(pid, '{"files": 7}')
        again = await registration.service.open_project("owner", registration.root, name="ignored")
        assert again["name"] == "chosen name"
        assert again["session_id"] == "stable-session"
        assert again["base_branch"] == "release"
        assert again["allow_direct_write"] is True
        assert again["scan_summary"] == {"files": 7}
        assert len(registration.repo.list_for_user("owner")) == 1

    asyncio.run(exercise())


def test_dao_register_is_atomic_across_connections(tmp_path):
    databases = [Database(tmp_path / "shared.db") for _ in range(2)]
    repos = [ProjectRepo(db) for db in databases]
    try:
        def register(i):
            return repos[i % 2].register("owner", f"name-{i}", "/fixture", "/fixture")

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(register, range(40)))
        assert len({row["id"] for row in rows}) == 1
        assert len(repos[0].list_for_user("owner")) == 1
        other = repos[1].register("other", "other", "/fixture", "/fixture")
        assert other["id"] != rows[0]["id"]
    finally:
        for db in databases:
            db.close()


def test_owner_never_reuses_another_users_project(registration):
    first = open_project(registration).json()
    other = registration.client.post("/api/v1/code/projects", headers=registration.other,
                                     json={"path": str(registration.root)}).json()
    assert first["id"] != other["id"]
    for headers, pid in [(registration.headers, other["id"]), (registration.other, first["id"])]:
        assert registration.client.get(f"/api/v1/code/projects/{pid}", headers=headers).status_code == 404
    assert [row["id"] for row in registration.repo.list_for_user("owner")] == [first["id"]]
    assert [row["id"] for row in registration.repo.list_for_user("other")] == [other["id"]]


def test_open_uses_one_complete_git_probe(registration, monkeypatch):
    calls = []

    async def probe(root, *, with_status=False):
        calls.append(with_status)
        return RepoInfo(git_available=True, is_repo=True, branch="main", dirty=True,
                        changed=["source.py"])

    monkeypatch.setattr(P, "probe_repo", probe)
    response = open_project(registration)
    assert response.status_code == 200, response.text
    assert calls == [True]
    assert response.json()["git"]["dirty"] is True
    assert response.json()["git"]["changed"] == ["source.py"]
    assert response.json()["base_branch"] == "main"


def test_open_revalidates_directory_after_git_probe(registration, monkeypatch):
    async def probe(root, *, with_status=False):
        (registration.root / "source.py").unlink()
        registration.root.rmdir()
        return RepoInfo(git_available=True)

    monkeypatch.setattr(P, "probe_repo", probe)
    response = open_project(registration)
    assert response.status_code == 422 and "路径不存在" in response.json()["detail"]
    assert registration.repo.list_for_user("owner") == []


def test_create_revalidates_existing_directory_after_git_probe(registration, monkeypatch):
    first = create_project(registration).json()
    root = Path(first["root_path"])

    async def probe(path, *, with_status=False):
        for child in root.iterdir():
            child.unlink()
        root.rmdir()
        return RepoInfo(git_available=True)

    monkeypatch.setattr(P, "probe_repo", probe)
    response = create_project(registration)
    assert response.status_code == 422 and "发生变化" in response.json()["detail"]
    assert registration.repo.get(first["id"]) is not None
    assert not root.exists()


def test_create_reuse_does_not_regrant_ownership_when_registration_was_removed(registration, monkeypatch):
    first = create_project(registration).json()
    root = Path(first["root_path"])
    (root / "keep.txt").write_text("keep")

    async def probe(path, *, with_status=False):
        registration.repo.delete(first["id"])
        return RepoInfo(git_available=True)

    monkeypatch.setattr(P, "probe_repo", probe)
    response = create_project(registration)
    assert response.status_code == 404
    assert registration.repo.list_for_user("owner") == []
    assert (root / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("kind", ["empty", "nonempty", "file"])
def test_create_cannot_take_over_existing_filesystem_entry(registration, kind):
    root = registration.base / "existing"
    registration.base.mkdir()
    if kind == "file":
        root.write_text("original")
    else:
        root.mkdir()
        if kind == "nonempty":
            (root / "original.txt").write_text("original")
    response = create_project(registration, "existing", template="python")
    assert response.status_code == 422, response.text
    assert "目录已存在" in response.json()["detail"]
    assert root.exists()
    assert registration.repo.list_for_user("owner") == []
    if kind == "file":
        assert root.read_text() == "original"
    elif kind == "empty":
        assert list(root.iterdir()) == []
    else:
        assert [entry.name for entry in root.iterdir()] == ["original.txt"]


def test_repeated_create_returns_own_project_without_changing_template(registration):
    first = create_project(registration, template="python").json()
    root = Path(first["root_path"])
    (root / "README.md").write_text("user content")
    registration.repo.set_session(first["id"], "session")
    second = create_project(registration, template="node").json()
    assert second["id"] == first["id"]
    assert second["template"] == "python" and second["origin"] == "create"
    assert second["session_id"] == "session"
    assert (root / "README.md").read_text() == "user content"
    assert not (root / "package.json").exists()
    # Another owner cannot reuse, scaffold or take deletion ownership of this directory.
    response = registration.client.post("/api/v1/code/projects", headers=registration.other,
        json={"mode": "create", "name": "new-project", "template": "node", "git_init": False})
    assert response.status_code == 422
    assert registration.repo.list_for_user("other") == []


def test_registered_missing_directory_is_not_silently_recreated(registration):
    root = registration.base / "missing"
    row = registration.repo.register("owner", "missing", str(root), root_key(root), origin="create")
    response = create_project(registration, "missing")
    assert response.status_code == 422 and "目录不可用" in response.json()["detail"]
    assert not root.exists()
    assert registration.repo.get(row["id"]) is not None


@pytest.mark.parametrize("name", ["CON", "NUL.txt", "COM1", "LPT9", "demo."])
def test_create_rejects_windows_reserved_names(registration, name):
    response = create_project(registration, name)
    assert response.status_code == 422
    assert not registration.base.exists()


def test_failed_scaffold_cleans_only_new_directory(registration, monkeypatch):
    registration.base.mkdir()
    sibling = registration.base / "existing"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep")

    def fail(root, name, template):
        (root / "partial.txt").write_text("partial")
        raise OSError("fixture write failed")

    monkeypatch.setattr(registration.service, "_scaffold", fail)
    response = create_project(registration)
    assert response.status_code == 422
    assert not (registration.base / "new-project").exists()
    assert (sibling / "keep.txt").read_text() == "keep"
    assert registration.repo.list_for_user("owner") == []


class CommitFailure:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        raise sqlite3.OperationalError("fixture commit failure")


def test_commit_failure_rolls_back_registration_and_cleans_owned_directory(registration, monkeypatch):
    monkeypatch.setattr(registration.db, "_conn", CommitFailure(registration.db._conn))
    response = create_project(registration, template="python")
    assert response.status_code == 503
    assert registration.repo.list_for_user("owner") == []
    assert not (registration.base / "new-project").exists()
    opened = open_project(registration)
    assert opened.status_code == 503
    assert (registration.root / "source.py").is_file()
    assert registration.repo.list_for_user("owner") == []


def test_failed_reuse_keeps_existing_project_and_files(registration, monkeypatch):
    first = create_project(registration).json()
    root = Path(first["root_path"])
    (root / "keep.txt").write_text("keep")
    monkeypatch.setattr(registration.db, "_conn", CommitFailure(registration.db._conn))
    response = create_project(registration)
    assert response.status_code == 503
    assert (root / "keep.txt").read_text() == "keep"
    assert registration.repo.get(first["id"])["origin"] == "create"


def test_cleanup_preserves_a_directory_replaced_by_an_external_writer(registration, monkeypatch):
    def fail(root, name, template):
        root.rename(root.with_name("original-owned-directory"))
        root.mkdir()
        (root / "keep.txt").write_text("external content")
        raise OSError("fixture replacement")

    monkeypatch.setattr(registration.service, "_scaffold", fail)
    response = create_project(registration)
    assert response.status_code == 422 and "目录已保留" in response.json()["detail"]
    assert (registration.base / "new-project" / "keep.txt").read_text() == "external content"
    assert (registration.base / "original-owned-directory").is_dir()
    assert registration.repo.list_for_user("owner") == []


def test_filesystem_preparation_runs_off_the_request_loop(registration, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = P.P.resolve_project_root

    def blocked(raw):
        entered.set()
        assert release.wait(2), "the event loop could not release filesystem work"
        return original(raw)

    monkeypatch.setattr(P.P, "resolve_project_root", blocked)

    async def exercise():
        task = asyncio.create_task(registration.service.open_project("owner", registration.root))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            release.set()
            assert (await task)["available"] is True
        finally:
            release.set()

    asyncio.run(exercise())


def test_list_probes_have_bounded_concurrency_and_preserve_status_and_order(registration, monkeypatch):
    for index in range(9):
        root = registration.root.parent / f"repo-{index}"
        root.mkdir()
        registration.repo.register("owner", str(index), str(root), root_key(root))
    expected = [row["id"] for row in registration.repo.list_for_user("owner")]
    active = maximum = 0

    async def probe(root, *, with_status=False):
        nonlocal active, maximum
        assert with_status is True
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return RepoInfo(git_available=True, is_repo=True, dirty=True, changed=["real-change.py"])

    monkeypatch.setattr(P, "probe_repo", probe)
    rows = asyncio.run(registration.service.list_projects("owner", with_git=True))
    assert [row["id"] for row in rows] == expected
    assert 1 < maximum <= 4
    assert all(row["git"]["dirty"] and row["git"]["changed"] == ["real-change.py"] for row in rows)


def test_workspace_list_and_detail_do_not_launch_git(registration, monkeypatch):
    row = registration.repo.register("owner", "repo", str(registration.root),
                                     root_key(registration.root), git_mode="git", base_branch="main")

    async def forbidden(*args, **kwargs):
        raise AssertionError("workspace metadata must not wait for Git subprocesses")

    monkeypatch.setattr(P, "probe_repo", forbidden)
    listing = registration.client.get("/api/v1/code/projects", headers=registration.headers)
    detail = registration.client.get(f"/api/v1/code/projects/{row['id']}", headers=registration.headers)
    assert listing.status_code == detail.status_code == 200
    assert listing.json()[0]["id"] == detail.json()["id"] == row["id"]
    for body in (listing.json()[0], detail.json()):
        assert body["available"] is True
        assert body["git"]["is_repo"] is True and body["git"]["branch"] == "main"
        assert body["git"]["dirty"] is None  # Not a misleading clean-worktree indicator.
        assert body["git"]["cached"] is True and body["git"]["stale"] is True
    assert registration.probes == []


def test_workspace_uses_snapshot_without_reprobe_and_checks_directory(registration, monkeypatch):
    async def probe(root, *, with_status=False):
        registration.probes.append((str(root), with_status))
        return RepoInfo(git_available=True, is_repo=True, branch="feature", dirty=True,
                        changed=["source.py"])

    monkeypatch.setattr(P, "probe_repo", probe)
    opened = open_project(registration).json()
    assert opened["git"]["cached"] is False and opened["git"]["stale"] is False
    listing = registration.client.get("/api/v1/code/projects", headers=registration.headers).json()
    detail = registration.client.get(f"/api/v1/code/projects/{opened['id']}", headers=registration.headers).json()
    assert len(registration.probes) == 1
    assert listing[0]["git"]["dirty"] is True and detail["git"]["branch"] == "feature"
    assert detail["git"]["cached"] is True
    row = registration.repo.get(opened["id"])
    first = asyncio.run(registration.service.view(row))
    first["git"]["changed"].append("client-mutation.py")
    assert asyncio.run(registration.service.view(row))["git"]["changed"] == ["source.py"]
    (registration.root / "source.py").unlink()
    registration.root.rmdir()
    missing = registration.client.get(f"/api/v1/code/projects/{opened['id']}", headers=registration.headers)
    assert missing.status_code == 200 and missing.json()["available"] is False
    assert len(registration.probes) == 1


def test_expired_git_snapshot_stays_explicit_until_requested_refresh(registration, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(P, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    row = registration.repo.register("owner", "repo", str(registration.root),
                                     root_key(registration.root), git_mode="git", base_branch="main")
    snapshot = RepoInfo(git_available=True, is_repo=True, branch="main", dirty=True, changed=["source.py"])
    asyncio.run(registration.service.view(row, repo_info=snapshot))
    clock[0] += 31
    cached = asyncio.run(registration.service.view(row))
    assert cached["git"]["cached"] is True and cached["git"]["stale"] is True
    assert cached["git"]["dirty"] is True and registration.probes == []

    async def changed_git(root, *, with_status=False):
        registration.probes.append(with_status)
        return RepoInfo(git_available=True, is_repo=True, branch="next-branch", dirty=False)

    monkeypatch.setattr(P, "probe_repo", changed_git)
    fresh = asyncio.run(registration.service.view(row, with_git=True))
    assert fresh["git"]["cached"] is False and fresh["git"]["stale"] is False
    assert fresh["git"]["branch"] == "next-branch" and fresh["git"]["dirty"] is False
    assert registration.probes == [True]


def test_cancelled_creation_waits_for_scaffold_then_cleans_owned_directory(registration, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = registration.service._scaffold

    def blocked(root, name, template):
        entered.set()
        assert release.wait(2)
        original(root, name, template)

    monkeypatch.setattr(registration.service, "_scaffold", blocked)

    async def exercise():
        task = asyncio.create_task(registration.service.create_project("owner", "cancelled", git_init=False))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(exercise())
    assert not (registration.base / "cancelled").exists()
    assert registration.repo.list_for_user("owner") == []


def test_create_in_selected_parent_keeps_parent_contents(registration):
    parent = registration.root.parent / "选择的父目录 空间"
    parent.mkdir()
    (parent / "keep.txt").write_text("parent content")
    response = create_project(registration, "子项目", template="python", location='"' + str(parent) + '"')
    assert response.status_code == 200, response.text
    project = response.json()
    assert Path(project["root_path"]) == parent / "子项目"
    assert project["origin"] == "create" and project["template"] == "python"
    assert (parent / "子项目" / "src" / "main.py").is_file()
    assert (parent / "keep.txt").read_text() == "parent content"
    assert not registration.base.exists()


def test_same_name_at_different_locations_creates_separate_projects(registration):
    parents = [registration.root.parent / name for name in ("one", "two")]
    for parent in parents:
        parent.mkdir()
    first = create_project(registration, location=str(parents[0])).json()
    second = create_project(registration, location=str(parents[1])).json()
    repeated = create_project(registration, location=str(parents[0])).json()
    assert first["id"] != second["id"] and repeated["id"] == first["id"]
    assert len(registration.repo.list_for_user("owner")) == 2


@pytest.mark.parametrize("location", ["", "   "])
def test_empty_location_keeps_default_behavior(registration, location):
    response = create_project(registration, location=location)
    assert response.status_code == 200, response.text
    assert Path(response.json()["root_path"]) == registration.base / "new-project"


def test_explicit_default_location_can_be_created_if_missing(registration, agent_home):
    default = agent_home / "projects"
    registration.service._roots = default
    assert not default.exists()
    response = create_project(registration, location=str(default))
    assert response.status_code == 200, response.text
    assert Path(response.json()["root_path"]) == default / "new-project"
    assert (default / "new-project" / "README.md").is_file()
    repeated = create_project(registration, location=str(default)).json()
    assert repeated["id"] == response.json()["id"]


@pytest.mark.skipif(os.name != "nt", reason="Windows path case and separators")
def test_explicit_default_location_uses_canonical_path_exception(registration, agent_home):
    default = agent_home / "projects"
    registration.service._roots = default
    default.mkdir()
    response = create_project(registration, location=str(default).upper().replace("\\", "/") + "/")
    assert response.status_code == 200, response.text
    assert Path(response.json()["root_path"]) == default / "new-project"


@pytest.mark.parametrize("kind", ["missing", "file", "relative", "url", "platform", "platform-child", "system"])
def test_invalid_parent_location_is_rejected_without_creating_it(registration, agent_home, kind):
    default = agent_home / "projects"
    default.mkdir()
    registration.service._roots = default
    missing = registration.root.parent / "missing-parent"
    if kind == "missing":
        location = str(missing)
    elif kind == "file":
        location = str(registration.root / "source.py")
    elif kind == "relative":
        location = "relative-location"
    elif kind == "url":
        location = "https://example.com/projects"
    elif kind == "platform":
        location = str(agent_home)
    elif kind == "platform-child":
        location = str(default / "other-parent")
        Path(location).mkdir()
    else:
        location = "C:\\Windows" if os.name == "nt" else "/usr"
    response = create_project(registration, location=location)
    assert response.status_code == 422, response.text
    assert registration.probes == []
    assert registration.repo.list_for_user("owner") == []
    assert not missing.exists()
    assert not (default / "new-project").exists()
    assert (registration.root / "source.py").read_text() == "print('fixture')\n"


def test_home_is_a_valid_parent_but_is_still_forbidden_as_a_project(registration, monkeypatch):
    home = registration.root.parent / "fake-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    response = create_project(registration, location=str(home))
    assert response.status_code == 200, response.text
    assert Path(response.json()["root_path"]) == home / "new-project"
    assert open_project(registration, path=str(home)).status_code == 422
    # An old/injected registration cannot let create reuse the home directory itself.
    registration.repo.register("owner", "fake-home", str(home), root_key(home))
    refused = create_project(registration, "fake-home", location=str(home.parent))
    assert refused.status_code == 422
    assert not (home / "README.md").exists()


def test_filesystem_root_can_be_a_parent_but_cannot_be_opened(registration):
    # Read-only validation; never create a real directory on the machine's drive root.
    drive = Path(registration.root.anchor)
    assert P.P.resolve_project_root(drive, for_creation_parent=True) == drive
    assert open_project(registration, path=str(drive)).status_code == 422


@pytest.mark.skipif(os.name != "nt", reason="Windows system directory")
def test_drive_parent_does_not_allow_system_directory_as_final_project(registration):
    response = create_project(registration, "Windows", location="C:\\")
    assert response.status_code == 422, response.text
    assert registration.repo.list_for_user("owner") == []
    assert registration.probes == []


@pytest.mark.parametrize("kind", ["empty", "nonempty", "file"])
def test_selected_location_does_not_take_over_existing_child(registration, kind):
    parent = registration.root.parent / "parent"
    parent.mkdir()
    target = parent / "new-project"
    if kind == "file":
        target.write_text("keep")
    else:
        target.mkdir()
        if kind == "nonempty":
            (target / "keep.txt").write_text("keep")
    response = create_project(registration, template="python", location=str(parent))
    assert response.status_code == 422, response.text
    assert target.exists() and registration.repo.list_for_user("owner") == []
    if kind == "file":
        assert target.read_text() == "keep"
    elif kind == "empty":
        assert list(target.iterdir()) == []
    else:
        assert [path.name for path in target.iterdir()] == ["keep.txt"]


def test_selected_location_reuses_only_own_project_without_claiming_opened_directory(registration):
    first = open_project(registration).json()
    body = {"mode": "create", "name": registration.root.name, "template": "node", "git_init": False,
            "location": str(registration.root.parent)}
    reused = registration.client.post("/api/v1/code/projects", json=body, headers=registration.headers)
    assert reused.status_code == 200, reused.text
    assert reused.json()["id"] == first["id"] and reused.json()["origin"] == "open"
    assert not (registration.root / "package.json").exists()
    refused = registration.client.post("/api/v1/code/projects", json=body, headers=registration.other)
    assert refused.status_code == 422
    assert registration.repo.list_for_user("other") == []


@pytest.mark.parametrize("failure", ["scaffold", "commit"])
def test_selected_parent_failure_cleanup_preserves_existing_content(registration, monkeypatch, failure):
    parent = registration.root.parent / "parent"
    parent.mkdir()
    (parent / "keep.txt").write_text("parent content")
    sibling = parent / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("sibling content")
    if failure == "scaffold":
        def fail(root, name, template):
            (root / "partial.txt").write_text("partial")
            raise OSError("fixture write failure")
        monkeypatch.setattr(registration.service, "_scaffold", fail)
    else:
        monkeypatch.setattr(registration.db, "_conn", CommitFailure(registration.db._conn))
    response = create_project(registration, template="python", location=str(parent))
    assert response.status_code == (422 if failure == "scaffold" else 503)
    assert not (parent / "new-project").exists()
    assert (parent / "keep.txt").read_text() == "parent content"
    assert (sibling / "keep.txt").read_text() == "sibling content"
    assert registration.repo.list_for_user("owner") == []


def test_selected_location_creation_is_atomic(registration):
    parent = registration.root.parent / "parent"
    parent.mkdir()

    async def exercise():
        results = await asyncio.gather(*(registration.service.create_project(
            "owner", "concurrent", location=parent, git_init=False
        ) for _ in range(8)), return_exceptions=True)
        successes = [row for row in results if isinstance(row, dict)]
        assert successes and len({row["id"] for row in successes}) == 1
        assert all(isinstance(row, (dict, P.P.ProjectPathError)) for row in results)
        opened = await registration.service.open_project("owner", parent / "concurrent")
        assert opened["id"] == successes[0]["id"]

    asyncio.run(exercise())
    assert len(registration.repo.list_for_user("owner")) == 1
    assert (parent / "concurrent" / "README.md").is_file()


def test_selected_location_is_used_for_git_initialization(registration, monkeypatch):
    parent = registration.root.parent / "parent"
    parent.mkdir()
    roots = []

    async def initialize(root):
        roots.append(root)
        return RepoInfo(git_available=True, is_repo=True, branch="main")

    monkeypatch.setattr(registration.service, "_git_init", initialize)
    monkeypatch.setattr(P, "git_available", lambda: True)
    response = create_project(registration, location=str(parent), git_init=True)
    assert response.status_code == 200, response.text
    assert roots == [parent / "new-project"]
    assert response.json()["git_mode"] == "git" and response.json()["git"]["branch"] == "main"


def test_custom_location_removal_keeps_files_and_does_not_expand_delete_permission(registration):
    parent = registration.root.parent / "parent"
    parent.mkdir()
    project = create_project(registration, location=str(parent)).json()
    root = Path(project["root_path"])
    url = f"/api/v1/code/projects/{project['id']}"
    denied = registration.client.delete(url, params={"delete_files": "true"}, headers=registration.headers)
    assert denied.status_code == 422
    assert registration.repo.get(project["id"]) is not None
    removed = registration.client.delete(url, headers=registration.headers)
    assert removed.status_code == 200 and removed.json()["removed_dir"] is False
    assert registration.repo.get(project["id"]) is None
    assert (root / "README.md").is_file()
