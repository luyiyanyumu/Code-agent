"""Memory management preserves legacy facts, concurrent writes, and ownership."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.config import code_dir
import code_agent.memory.store as memory_store
from code_agent.memory.store import CuratedMemory, MemoryConflict, WorkspaceMemory


def test_legacy_import_preserves_file_and_stable_ids(tmp_path):
    memory = CuratedMemory(tmp_path / "legacy")
    original = "# 项目约定\n\n- 使用 SQLite\n- 多行约定\n  下一行\n自由文本事实\n- 使用 SQLite\n"
    memory.memory_md.write_text(original, encoding="utf-8")
    first = memory.snapshot()
    assert first["entries"] == ["使用 SQLite", "多行约定\n下一行", "自由文本事实"]
    assert memory.memory_md.read_text(encoding="utf-8") == original
    assert memory.snapshot() == first
    record = first["records"][0]
    memory.update_record(record["id"], {"pinned": True, "enabled": False}, revision=first["revision"])
    memory.memory_md.write_text(memory.memory_md.read_text(encoding="utf-8") + "- 新补充事实\n", encoding="utf-8")
    imported = memory.snapshot()
    assert imported["records"][0]["id"] == record["id"]
    assert imported["records"][0]["pinned"] is True
    assert imported["records"][0]["enabled"] is False
    assert imported["entries"][-1] == "新补充事实"
    assert "# 项目约定" in memory.read_curated()


def test_multiline_one_record_dedupe_and_metadata(tmp_path):
    memory = CuratedMemory(tmp_path)
    record = memory.save_fact("步骤一\n步骤二\n\n# 内部标题\n- 内部列表", category="convention", source="agent", source_session_id="s1")
    assert memory.entries() == [record["content"]]
    assert memory.save_fact("步骤一 步骤二 # 内部标题 - 内部列表")["id"] == record["id"]
    revision = memory.snapshot()["revision"]
    memory.update_record(record["id"], {"content": "新内容", "enabled": False}, revision=revision)
    current = memory.records()[0]
    assert current["id"] == record["id"]
    assert current["created_at"] == record["created_at"]
    assert current["source_session_id"] == "s1"
    assert memory.active_records() == []
    assert memory.entries() == ["新内容"]


def test_stale_revision_content_and_duplicate_edits(tmp_path):
    memory = CuratedMemory(tmp_path)
    first = memory.save_fact("旧内容")
    second = memory.save_fact("另一条")
    revision = memory.snapshot()["revision"]
    memory.update_record(first["id"], {"content": "新内容"}, revision=revision)
    with pytest.raises(MemoryConflict):
        memory.remove_record(first["id"], revision=revision)
    with pytest.raises(MemoryConflict):
        memory.remove_entry(0, expected_content="旧内容")
    with pytest.raises(MemoryConflict):
        memory.update_record(second["id"], {"content": "新内容"})
    assert memory.entries() == ["新内容", "另一条"]


def test_external_edit_during_transaction_is_not_overwritten(tmp_path, monkeypatch):
    memory = CuratedMemory(tmp_path)
    first = memory.save_fact("初始事实")
    commit = memory._commit

    def external_commit(state):
        memory.memory_md.write_text("- 初始事实\n- 外部新增事实\n", encoding="utf-8")
        commit(state)

    monkeypatch.setattr(memory, "_commit", external_commit)
    with pytest.raises(MemoryConflict):
        memory.update_record(first["id"], {"content": "界面修改"})
    assert memory.entries() == ["初始事实", "外部新增事实"]


@pytest.mark.parametrize("failure", ["mirror", "final_metadata"])
def test_interrupted_mirror_commit_preserves_disabled_record(tmp_path, monkeypatch, failure):
    memory = CuratedMemory(tmp_path)
    first = memory.save_fact("停用的旧内容", enabled=False, pinned=True, category="decision", source="agent", source_session_id="s1")
    revision = memory.snapshot()["revision"]
    atomic = memory_store._atomic
    json_writes = 0

    def fail_projection(path, text):
        nonlocal json_writes
        if path == memory.records_file:
            json_writes += 1
        if (failure == "mirror" and path == memory.memory_md) or (failure == "final_metadata" and path == memory.records_file and json_writes == 2):
            raise OSError("simulated interrupted projection")
        atomic(path, text)

    monkeypatch.setattr(memory_store, "_atomic", fail_projection)
    with pytest.raises(OSError):
        memory.update_record(first["id"], {"content": "停用的新内容"}, revision=revision)
    monkeypatch.setattr(memory_store, "_atomic", atomic)
    recovered = CuratedMemory(tmp_path).snapshot()
    assert recovered["revision"] == revision + 1
    assert recovered["entries"] == ["停用的新内容"]
    record = recovered["records"][0]
    assert record["id"] == first["id"]
    assert record["created_at"] == first["created_at"]
    assert record["enabled"] is False
    assert record["pinned"] is True
    assert record["category"] == "decision"
    assert record["source"] == "agent"
    assert record["source_session_id"] == "s1"
    assert memory.active_records() == []
    assert memory.read_curated() == "- 停用的新内容\n"


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_external_append_after_interrupted_commit_is_preserved(tmp_path, monkeypatch, operation):
    memory = CuratedMemory(tmp_path)
    first = memory.save_fact("停用的旧内容", enabled=False, pinned=True)
    atomic = memory_store._atomic

    def fail_mirror(path, text):
        if path == memory.memory_md:
            raise OSError("simulated interrupted projection")
        atomic(path, text)

    monkeypatch.setattr(memory_store, "_atomic", fail_mirror)
    with pytest.raises(OSError):
        if operation == "edit":
            memory.update_record(first["id"], {"content": "停用的新内容"})
        else:
            memory.remove_record(first["id"])
    monkeypatch.setattr(memory_store, "_atomic", atomic)
    memory.memory_md.write_text("- 停用的旧内容\n- 外部新增事实\n", encoding="utf-8")
    recovered = CuratedMemory(tmp_path).snapshot()
    assert "外部新增事实" in recovered["entries"]
    assert "停用的旧内容" not in recovered["entries"]
    if operation == "edit":
        record = next(row for row in recovered["records"] if row["id"] == first["id"])
        assert record["content"] == "停用的新内容"
        assert record["enabled"] is False
        assert record["pinned"] is True
    else:
        assert all(row["id"] != first["id"] for row in recovered["records"])
    assert [row["content"] for row in memory.active_records()] == ["外部新增事实"]


def test_parallel_instances_do_not_lose_facts(tmp_path):
    def save(index):
        CuratedMemory(tmp_path).save_fact(f"并发事实 {index}")
        CuratedMemory(tmp_path).save_fact("共享重复事实")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(24)))
    memory = CuratedMemory(tmp_path)
    assert len(memory.entries()) == 25
    assert len({row["id"] for row in memory.records()}) == 25


def test_cross_process_writes_use_same_os_lock(tmp_path):
    script = "from pathlib import Path;import sys;from code_agent.memory.store import CuratedMemory;m=CuratedMemory(Path(sys.argv[1]));[m.save_fact(f'{sys.argv[2]} {i}') for i in range(8)]"
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(index)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for index in range(3)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    assert len(CuratedMemory(tmp_path).entries()) == 24


@pytest.fixture
def client(agent_home):
    with TestClient(create_app(provider_name="offline")) as client:
        yield client


def auth(client, name="admin", password="admin123"):
    result = client.post("/api/v1/auth/login", json={"username": name, "password": password})
    assert result.status_code == 200
    return {"Authorization": f"Bearer {result.json()['token']}"}


def test_api_record_lifecycle_and_conflict(client):
    headers = auth(client)
    added = client.post("/api/v1/memory", json={"content": "第一行\n第二行", "category": "decision"}, headers=headers)
    assert added.status_code == 200, added.text
    first = added.json()
    record_id = first["record"]["id"]
    assert first["entries"] == ["第一行\n第二行"]
    changed = client.patch(f"/api/v1/memory/{record_id}", json={"scope_id": "user", "revision": first["revision"], "enabled": False, "pinned": True}, headers=headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["record"]["enabled"] is False
    assert changed.json()["record"]["source"] == "manual"
    stale = client.delete("/api/v1/memory", params={"entry_id": record_id, "revision": first["revision"]}, headers=headers)
    assert stale.status_code == 409
    assert client.delete("/api/v1/memory", params={"entry_id": record_id, "revision": changed.json()["revision"]}, headers=headers).status_code == 200
    assert client.get("/api/v1/memory", headers=headers).json()["user"]["records"] == []


def test_external_append_during_json_commit_returns_409_and_preserves_facts(client, monkeypatch):
    headers = auth(client)
    first = client.post("/api/v1/memory", json={"content": "停用的旧内容", "enabled": False, "pinned": True}, headers=headers).json()
    record_id = first["record"]["id"]
    atomic = memory_store._atomic
    appended = False

    def append_during_commit(path, text):
        nonlocal appended
        atomic(path, text)
        if path.name == "memory-records.json" and '"pending_mirror"' in text and not appended:
            appended = True
            mirror = path.parent / "MEMORY.md"
            mirror.write_text(mirror.read_text(encoding="utf-8") + "- 外部新增事实\n", encoding="utf-8")

    monkeypatch.setattr(memory_store, "_atomic", append_during_commit)
    changed = client.patch(f"/api/v1/memory/{record_id}", json={"scope_id": "user", "revision": first["revision"], "content": "停用的新内容"}, headers=headers)
    assert changed.status_code == 409, changed.text
    recovered = client.get("/api/v1/memory", headers=headers).json()["user"]
    assert recovered["entries"] == ["停用的新内容", "外部新增事实"]
    record = next(row for row in recovered["records"] if row["id"] == record_id)
    assert record["enabled"] is False
    assert record["pinned"] is True
    assert record["source"] == "manual"
    assert recovered["revision"] > first["revision"]


def test_api_validates_scope_and_guards(client):
    headers = auth(client)
    first = client.post("/api/v1/memory", json={"content": "原文"}, headers=headers).json()
    record_id = first["record"]["id"]
    for fields in ({"enabled": "false"}, {"pinned": 1}, {"category": []}, {"content": 123}, {"revision": True, "content": "修改"}):
        assert client.patch(f"/api/v1/memory/{record_id}", json=fields, headers=headers).status_code == 422
    assert client.patch(f"/api/v1/memory/{record_id}", json={"enabled": False}, headers=headers).status_code == 422
    assert client.delete("/api/v1/memory", params={"entry_id": record_id}, headers=headers).status_code == 422
    assert client.post("/api/v1/memory", json={"scope": "unknown", "content": "原文"}, headers=headers).status_code == 422
    assert client.delete("/api/v1/memory", params={"index": 0, "expected_content": "另一条"}, headers=headers).status_code == 409


def test_project_shared_root_and_private_session_scope(client, tmp_path):
    headers = auth(client)
    repository = tmp_path / "project"
    repository.mkdir()
    project = client.post("/api/v1/code/projects", json={"mode": "open", "path": str(repository)}, headers=headers).json()
    pid = project["id"]
    # Shared task/Lead memories are one project scope, irrespective of session count.
    user_id = client.app.state.projects.get(pid)["user_id"]
    first = client.app.state.sessions_repo.create(user_id, project_id=pid, title="任务一")
    second = client.app.state.sessions_repo.create(user_id, project_id=pid, title="任务二")
    shared = client.post("/api/v1/memory", json={"scope": "workspace", "session_id": first["id"], "content": "项目公共约定"}, headers=headers)
    assert shared.status_code == 200, shared.text
    assert shared.json()["scope_id"] == f"project:{pid}"
    assert WorkspaceMemory(code_dir() / pid).entries() == ["项目公共约定"]
    private = client.post("/api/v1/memory", json={"scope": "session", "session_id": second["id"], "content": "只属于任务二"}, headers=headers)
    assert private.status_code == 200, private.text
    body = client.get("/api/v1/memory", headers=headers).json()
    project_rows = [row for row in body["scopes"] if row["id"] == f"project:{pid}"]
    assert len(project_rows) == 1
    assert project_rows[0]["entries"] == ["项目公共约定"]
    second_row = next(row for row in body["scopes"] if row["id"] == f"session:{second['id']}")
    assert second_row["entries"] == ["只属于任务二"]
    assert not (repository / "memory").exists()


def test_project_session_ownership_and_traversal(client, tmp_path):
    from code_agent.api.security import hash_password

    headers = auth(client)
    repository = tmp_path / "private-project"
    repository.mkdir()
    project = client.post("/api/v1/code/projects", json={"mode": "open", "path": str(repository)}, headers=headers).json()
    sid = client.post("/api/v1/sessions", json={}, headers=headers).json()["id"]
    client.app.state.users.create("bob", hash_password("pw123456"))
    bob = auth(client, "bob", "pw123456")
    for scope_id in (f"project:{project['id']}", f"session:{sid}", "project:../users/admin", "session:../users/admin"):
        assert client.post("/api/v1/memory", json={"scope_id": scope_id, "content": "越权内容"}, headers=bob).status_code == 404
        assert client.delete("/api/v1/memory", params={"scope_id": scope_id, "entry_id": "m_missing", "revision": "0"}, headers=bob).status_code == 404
    body = client.get("/api/v1/memory", headers=bob).json()
    assert [scope["id"] for scope in body["scopes"]] == ["user"]
