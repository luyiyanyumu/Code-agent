"""Project summaries reuse the map index without repeating work or mixing snapshots."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from code_agent.codeintel import graph
from code_agent.codeintel.parsers import parse_file
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.store import META_SCHEMA_VERSION, SCHEMA_VERSION, SymbolIndex


SOURCE = "class Engine:\n    def run(self):\n        return 1\n"


def apply_source(index, source=SOURCE):
    return index.apply_parse(
        rel="core.py", lang="python", loc=len(source.splitlines()), size=len(source),
        mtime=0, sha1=source, parsed=parse_file("core.py", "python", source),
        edges=[], source=source,
    )


@pytest.fixture
def service(agent_home):
    service = CodeIntelService(projects=None)
    yield service
    service.close()


@pytest.fixture
def project(tmp_path):
    return {"id": "fixture", "name": "Fixture", "root_path": str(tmp_path)}


@pytest.fixture
def digest_calls(monkeypatch):
    calls = []
    original = graph.build_digest

    def counted(index, project, **kwargs):
        calls.append((index, dict(project)))
        return original(index, project, **kwargs)

    monkeypatch.setattr(graph, "build_digest", counted)
    return calls


def test_existing_database_adds_parent_index_without_rebuilding(tmp_path):
    path = tmp_path / "existing.db"
    index = SymbolIndex(path)
    apply_source(index)
    before = (index.version(), index.counts(), index.all_symbols(), index.query_all("SELECT * FROM changes"))
    index.close()
    # Simulate the schema shipped before this fix, retaining its map and history.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_symbols_parent")
    for _ in range(2):
        reopened = SymbolIndex(path)
        try:
            assert reopened.get_meta(META_SCHEMA_VERSION) == str(SCHEMA_VERSION)
            assert (reopened.version(), reopened.counts(), reopened.all_symbols(),
                    reopened.query_all("SELECT * FROM changes")) == before
            plan = reopened.query_all(
                "EXPLAIN QUERY PLAN SELECT s.id, "
                "(SELECT COUNT(*) FROM symbols c WHERE c.parent=s.id) AS members "
                "FROM symbols s WHERE s.kind='class' ORDER BY members DESC LIMIT 60"
            )
            assert any("idx_symbols_parent" in row["detail"] and "SEARCH" in row["detail"] for row in plan)
        finally:
            reopened.close()


def test_same_version_is_cached_before_building(service, project, digest_calls):
    apply_source(service.index(project["id"]))
    first = service.session_context(project)
    assert "Engine" in first[1] and "[1 成员]" in first[1]
    assert service.session_context(dict(reversed(list(project.items())))) == first
    assert service.session_context({**project, "last_scan_at": "2026-10-11T00:00:00Z"}) == first
    assert len(digest_calls) == 1


def test_changed_map_refreshes_summary(service, project, digest_calls):
    index = service.index(project["id"])
    apply_source(index)
    before = service.session_context(project)
    apply_source(index, SOURCE + "\ndef added():\n    return 2\n")
    after = service.session_context(project)
    assert after[0] > before[0]
    assert "added" in after[1] and "added" not in before[1]
    assert service.session_context(project) == after
    assert len(digest_calls) == 2


def test_line_only_changes_refresh_without_semantic_delta(service, project, digest_calls):
    index = service.index(project["id"])
    apply_source(index)
    before = service.session_context(project)
    assert apply_source(index, "\n\n" + SOURCE) == []
    after = service.session_context(project)
    assert after[0] == before[0]
    assert "core.py:1" in before[1] and "core.py:3" in after[1]
    assert len(digest_calls) == 2


def test_project_metadata_refreshes_same_version(service, project, digest_calls):
    first = service.session_context(project)
    changed = {**project, "name": "Renamed", "root_path": project["root_path"] + "/moved"}
    after = service.session_context(changed)
    assert after[0] == first[0]
    assert "Renamed" in after[1] and "/moved" in after[1]
    assert "Renamed" not in first[1]
    assert len(digest_calls) == 2


def test_project_and_tree_caches_are_isolated(service, project, digest_calls):
    second_project = {**project, "id": "second", "name": "Second"}
    for item, tree, name in ((project, "main", "Main"), (project, "task", "Task"),
                             (second_project, "main", "Other")):
        apply_source(service.index(item["id"], tree), f"class {name}:\n    pass\n")
    expected = [service.session_context(project), service.session_context(project, tree_id="task"),
                service.session_context(second_project)]
    assert "class Main" in expected[0][1]
    assert "class Task" in expected[1][1]
    assert "class Other" in expected[2][1]
    assert [service.session_context(project), service.session_context(project, tree_id="task"),
            service.session_context(second_project)] == expected
    assert len(digest_calls) == 3


def test_diagnostics_and_stat_updates_do_not_rebuild_summary(service, project, digest_calls):
    index = service.index(project["id"])
    apply_source(index)
    before = service.session_context(project)
    index.set_diagnostics(rel="core.py", uri="file:///core.py", items=[{"message": "fixture"}])
    index.touch_file("core.py", len(SOURCE), 99)
    assert service.session_context(project) == before
    assert len(digest_calls) == 1


def test_repeated_skipped_file_registration_keeps_summary_cache(service, project, digest_calls):
    index = service.index(project["id"])
    skipped = {"rel": ".env", "lang": "", "size": 10, "mtime": 1, "sha1": "", "skipped": "secret"}
    index.mark_file(**skipped)
    before = service.session_context(project)
    index.mark_file(**{**skipped, "size": 20, "mtime": 2})
    assert service.session_context(project) == before
    assert index.file_row(".env")["size"] == 20
    assert index.file_row(".env")["mtime"] == 2
    assert len(digest_calls) == 1


@pytest.mark.parametrize("change", [{"skipped": "too_large"}, {"lang": "python"}])
def test_changed_skipped_file_facts_refresh_summary(service, project, digest_calls, change):
    index = service.index(project["id"])
    skipped = {"rel": "data.bin", "lang": "", "size": 10, "mtime": 1, "sha1": "", "skipped": "binary"}
    index.mark_file(**skipped)
    before = service.session_context(project)
    index.mark_file(**{**skipped, **change})
    after = service.session_context(project)
    assert after[0] == before[0]  # Keep the existing semantic-delta contract.
    assert len(digest_calls) == 2
    assert service.session_context(project) == after
    assert len(digest_calls) == 2


def test_concurrent_requests_build_once_without_blocking_other_projects(service, project, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = graph.build_digest
    calls = []

    def blocked(index, item, **kwargs):
        calls.append(item["id"])
        if item["id"] == project["id"]:
            started.set()
            assert release.wait(5)
        return original(index, item, **kwargs)

    monkeypatch.setattr(graph, "build_digest", blocked)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(service.session_context, project)
        try:
            assert started.wait(3)
            second = pool.submit(service.session_context, project)
            other = pool.submit(service.session_context, {**project, "id": "other", "name": "Other"})
            assert "Other" in other.result(timeout=3)[1]
            assert not first.done() and not second.done()
        finally:
            release.set()
        assert first.result(timeout=3) == second.result(timeout=3)
    assert calls.count(project["id"]) == 1


def test_index_update_during_build_cannot_cache_mixed_versions(service, project, monkeypatch):
    index = service.index(project["id"])
    apply_source(index)
    version = index.version()
    started, release, writer_started = threading.Event(), threading.Event(), threading.Event()
    original = graph.build_digest

    def blocked(handle, item, **kwargs):
        started.set()
        assert release.wait(5)
        return original(handle, item, **kwargs)

    def update():
        writer_started.set()
        apply_source(index, "class Updated:\n    pass\n")

    monkeypatch.setattr(graph, "build_digest", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.session_context, project)
        try:
            assert started.wait(3)
            writing = pool.submit(update)
            assert writer_started.wait(3)
            assert not writing.done()
        finally:
            release.set()
        before = first.result(timeout=3)
        writing.result(timeout=3)
    assert before[0] == version and "class Engine" in before[1]
    after = service.session_context(project)
    assert after[0] > before[0] and "class Updated" in after[1]


@pytest.mark.parametrize("scope", ["tree", "project", "all"])
def test_close_waits_for_snapshot_and_clears_cache(service, project, monkeypatch, scope):
    started, release = threading.Event(), threading.Event()
    original = graph.build_digest

    def blocked(index, item, **kwargs):
        started.set()
        assert release.wait(5)
        return original(index, item, **kwargs)

    monkeypatch.setattr(graph, "build_digest", blocked)
    close = {
        "tree": lambda: service.close_tree(project["id"], "main"),
        "project": lambda: service.close(project["id"]),
        "all": service.close,
    }[scope]
    with ThreadPoolExecutor(max_workers=2) as pool:
        building = pool.submit(service.session_context, project)
        try:
            assert started.wait(3)
            closing = pool.submit(close)
            assert not closing.done()
        finally:
            release.set()
        assert "Fixture" in building.result(timeout=3)[1]
        closing.result(timeout=3)
    assert not service._session_context_cache
    assert not service._indexes


def test_reopened_index_at_same_version_rebuilds(service, project, digest_calls):
    apply_source(service.index(project["id"]))
    before = service.session_context(project)
    service.close_tree(project["id"], "main")
    assert service.session_context(project) == before
    assert len(digest_calls) == 2
    assert digest_calls[0][0] is not digest_calls[1][0]


def test_failed_build_does_not_poison_cache(service, project, monkeypatch):
    original = graph.build_digest

    def failed(*args, **kwargs):
        raise ValueError("fixture failure")

    monkeypatch.setattr(graph, "build_digest", failed)
    with pytest.raises(ValueError, match="fixture failure"):
        service.session_context(project)
    assert not service._session_context_cache
    monkeypatch.setattr(graph, "build_digest", original)
    assert "Fixture" in service.session_context(project)[1]
