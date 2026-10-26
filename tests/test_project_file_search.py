"""IDE 文件目录与文字查找：只读、归属隔离、边界及明确的查询上限。"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent.api.code_api import mount_code_api
from code_agent.api.security import create_token
from code_agent.codeintel import file_search
from code_agent.codeintel.projects import ProjectService
from code_agent.codeintel.store import SymbolIndex
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel import parsers
from code_agent.storage.db import Database, ProjectRepo, UserRepo


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "App.vue").write_text("Needle\n", encoding="utf-8")
    (root / "README.md").write_text("Needle\n", encoding="utf-8")
    (root / "src/main.py").write_text(
        "Needle needle NEEDLE\nconst markup = 'a.*b'\n😀needle尾\n", encoding="utf-8"
    )
    (root / "src/widget.vue").write_text("needle\n", encoding="utf-8")
    (root / ".env").write_text("Needle=secret\n", encoding="utf-8")
    (root / "secrets.json").write_text('{"value":"needle"}', encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00needle")
    (root / "binary.txt").write_bytes(b"\x00needle")
    (root / "large.txt").write_bytes(b"needle" * (file_search.MAX_PARSE_BYTES // 6 + 1))
    for directory in ("node_modules", "dist", "target", ".cache"):
        (root / directory).mkdir()
        (root / directory / "ignored.js").write_text("needle\n", encoding="utf-8")
    return root


def test_catalog_filter_and_page_exclude_unsafe_files(repo):
    all_files = file_search.files_catalog(repo)
    assert {item["path"] for item in all_files["files"]} == {
        "App.vue", "README.md", "src/main.py", "src/widget.vue"
    }
    assert all_files["total"] == 4 and all_files["truncated"] is False
    filtered = file_search.files_catalog(repo, query="MAIN")
    assert filtered["files"][0]["path"] == "src/main.py"
    assert filtered["files"][0]["lang"] == "python"
    page = file_search.files_catalog(repo, limit=1)
    assert len(page["files"]) == 1 and page["total"] == 4 and page["truncated"] is True


def test_literal_case_glob_and_monaco_columns(repo):
    insensitive = file_search.search_text(repo, query="needle")
    assert len(insensitive["matches"]) == 7
    assert insensitive["files_scanned"] == 4 and insensitive["truncated"] is False
    exact = file_search.search_text(repo, query="Needle", case_sensitive=True)
    assert len(exact["matches"]) == 3
    special = file_search.search_text(repo, query="a.*b")
    assert len(special["matches"]) == 1
    unicode_match = next(row for row in insensitive["matches"] if row["line"] == 3)
    assert unicode_match["column"] == 3 and unicode_match["end_column"] == 9
    only_vue = file_search.search_text(repo, query="needle", include="**/*.vue")
    assert {row["path"] for row in only_vue["matches"]} == {"App.vue", "src/widget.vue"}
    assert only_vue["files_scanned"] == 2


def test_search_page_and_long_line_preview(repo):
    limited = file_search.search_text(repo, query="needle", limit=2)
    assert len(limited["matches"]) == 2
    assert limited["truncated"] is True and limited["truncation_reason"] == "matches"
    exact_limit = file_search.search_text(repo, query="needle", limit=7)
    assert len(exact_limit["matches"]) == 7 and exact_limit["truncated"] is False
    (repo / "long.txt").write_text("x" * 20000 + "find-me" + "x" * 20000, encoding="utf-8")
    row = file_search.search_text(repo, query="find-me")["matches"][0]
    assert row["column"] == 20001 and row["end_column"] == 20008
    assert "find-me" in row["text"] and len(row["text"]) <= 500
    assert row["text_truncated"] is True and row["text_start_column"] > 1


def test_search_read_budget_and_file_budget_are_reported(repo, monkeypatch):
    monkeypatch.setattr(file_search, "MAX_SEARCH_FILES", 1)
    bounded = file_search.search_text(repo, query="not-found")
    assert bounded["truncated"] is True and bounded["truncation_reason"] == "files"
    monkeypatch.setattr(file_search, "MAX_SEARCH_FILES", 5000)
    monkeypatch.setattr(file_search, "MAX_SEARCH_BYTES", 1)
    bounded = file_search.search_text(repo, query="not-found")
    assert bounded["truncated"] is True and bounded["truncation_reason"] == "bytes"


@pytest.fixture
def api_client(agent_home, repo):
    db = Database(agent_home / "test-api.db")
    users = UserRepo(db)
    owner = users.create("owner", "not-used", role="admin")
    other = users.create("other", "not-used")
    projects = ProjectRepo(db)
    project = projects.create(owner["id"], "repo", str(repo), str(repo))
    app = FastAPI()
    app.state.auth_enabled = True
    mount_code_api(app, project_repo=projects, manager=None, service=ProjectService(projects))
    headers = {"Authorization": "Bearer " + create_token(owner["id"], owner["username"], "admin")}
    other_headers = {"Authorization": "Bearer " + create_token(other["id"], other["username"], "user")}
    with TestClient(app) as client:
        yield client, project["id"], headers, other_headers
    db.close()


def test_api_project_isolation_auth_and_no_index_or_parser(api_client, agent_home, monkeypatch):
    client, pid, headers, other_headers = api_client

    def forbidden(*args, **kwargs):
        raise AssertionError("文件目录/搜索不能创建索引、扫描或解析代码")

    monkeypatch.setattr(CodeIntelService, "scan", forbidden)
    monkeypatch.setattr(CodeIntelService, "ensure_indexed", forbidden)
    monkeypatch.setattr(SymbolIndex, "__init__", forbidden)
    monkeypatch.setattr(parsers, "parse_file", forbidden)
    for suffix, params in (("files", {}), ("search", {"query": "needle"})):
        url = f"/api/v1/code/projects/{pid}/{suffix}"
        assert client.get(url, params=params, headers=headers).status_code == 200
        assert client.get(url, params=params, headers=other_headers).status_code == 404
        assert client.get(url, params=params).status_code == 401
        assert client.get(url.replace(pid, "missing"), params=params, headers=headers).status_code == 404
    assert not (agent_home / "code").exists()


@pytest.mark.parametrize("suffix,params", [
    ("files", {"limit": 0}), ("files", {"limit": 2001}),
    ("files", {"query": "x" * 513}), ("files", {"query": "\x00"}),
    ("search", {}), ("search", {"query": ""}),
    ("search", {"query": "x" * 513}), ("search", {"query": "x\ny"}),
    ("search", {"query": "x", "limit": 1001}),
    ("search", {"query": "x", "case_sensitive": "invalid"}),
    ("search", {"query": "(a+)+$", "regex": "true"}),
    ("search", {"query": "x", "include": "../*"}),
    ("search", {"query": "x", "include": "/tmp/*"}),
    ("search", {"query": "x", "include": "C:\\Users\\*"}),
])
def test_api_rejects_invalid_or_unbounded_queries(api_client, suffix, params):
    client, pid, headers, _ = api_client
    assert client.get(f"/api/v1/code/projects/{pid}/{suffix}", params=params, headers=headers).status_code == 422


def test_forged_inventory_path_cannot_read_outside_project(repo, tmp_path, monkeypatch):
    from code_agent.codeintel.scanner import FileEntry, ScanResult

    (tmp_path / "outside.txt").write_text("private-needle", encoding="utf-8")
    fake = ScanResult(files=[FileEntry("../outside.txt", str(tmp_path / "outside.txt"), 14, 0, "text")])
    monkeypatch.setattr(file_search, "walk_project", lambda root: fake)
    assert file_search.files_catalog(repo)["files"] == []
    assert file_search.search_text(repo, query="private-needle")["matches"] == []
