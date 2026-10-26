"""Git ignore rules exclude generated copies without hiding legal source names."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from code_agent.codeintel import scanner
from code_agent.codeintel.service import CodeIntelService


class Projects:
    def set_scan(self, pid, summary):
        pass

    def get(self, pid):
        return None


def git(root: Path, *args: str):
    if not shutil.which("git"):
        pytest.skip("Git is unavailable")
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "--quiet")
    return root


def source(root: Path, rel: str, text: str = "class Source { int get() { return 1; } }\n"):
    file = root / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    return file


def scanned(root: Path) -> set[str]:
    return {entry.rel for entry in scanner.walk_project(root).files}


def test_gitignore_prunes_generated_directories_before_scandir(tmp_path, monkeypatch):
    root = repository(tmp_path)
    source(root, ".gitignore", "/output/\n/tmp/\n*.cache\n")
    source(root, "output/packaging/Copy.java")
    source(root, "tmp/Copy.java")
    source(root, "src/Source.java")
    source(root, "src/model.cache")
    original = scanner.os.scandir
    visited = []

    def inspect(path):
        visited.append(Path(path).relative_to(root).as_posix())
        return original(path)

    monkeypatch.setattr(scanner.os, "scandir", inspect)
    rels = scanned(root)
    assert rels == {".gitignore", "src/Source.java"}
    assert "output" not in visited and "tmp" not in visited


def test_force_tracked_ignored_files_also_prune_their_ignored_parent(tmp_path, monkeypatch):
    root = repository(tmp_path)
    source(root, ".gitignore", "/output/\n")
    source(root, "output/copies/Tracked.java")
    source(root, "output/other/Untracked.java")
    source(root, "src/Source.java")
    git(root, "add", "-f", "output/copies/Tracked.java")
    original = scanner.os.scandir

    def inspect(path):
        assert Path(path) != root / "output", "Ignored tracked trees must be pruned before traversal"
        return original(path)

    monkeypatch.setattr(scanner.os, "scandir", inspect)
    assert scanned(root) == {".gitignore", "src/Source.java"}


def test_nested_rules_and_negations_remain_exact_when_scanning_subdirectory(tmp_path):
    root = repository(tmp_path)
    source(root, ".gitignore", "*.cache\n/src/generated/*\n!/src/generated/keep/\n")
    source(root, "src/.gitignore", "*.tmp\n!important.tmp\n")
    source(root, "src/generated/discard/Copy.java")
    source(root, "src/generated/keep/Source.java")
    source(root, "src/cache.cache")
    source(root, "src/log.tmp")
    source(root, "src/important.tmp")
    source(root, "src/Source.java")
    expected = {".gitignore", "generated/keep/Source.java", "important.tmp", "Source.java"}
    assert scanned(root / "src") == expected
    assert {rel.removeprefix("src/") for rel in scanned(root) if rel.startswith("src/")} == expected


def test_unicode_spaces_and_escaped_gitignore_patterns(tmp_path):
    root = repository(tmp_path)
    source(root, ".gitignore", "/发布 产物/\n\\[generated\\].java\n")
    source(root, "发布 产物/副本.java")
    source(root, "[generated].java")
    source(root, "源码/正常 类.java")
    assert scanned(root) == {".gitignore", "源码/正常 类.java"}


def test_non_git_project_keeps_legitimate_output_source(tmp_path):
    root = tmp_path / "plain-project"
    source(root, "output/Result.java")
    source(root, "tmp/Temporary.java")
    assert scanned(root) == {"output/Result.java", "tmp/Temporary.java"}


def test_static_safety_policy_cannot_be_reincluded_by_gitignore(tmp_path):
    root = repository(tmp_path)
    source(root, ".gitignore", "!node_modules/\n!dist/\n")
    source(root, "node_modules/dependency.js")
    source(root, "dist/bundle.js")
    source(root, "src/Source.java")
    assert scanned(root) == {".gitignore", "src/Source.java"}


@pytest.mark.parametrize("failure", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 10)])
def test_missing_or_timed_out_git_preserves_static_scan(tmp_path, monkeypatch, failure):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    source(root, "output/Source.java")
    source(root, "dist/Copy.java")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(scanner.subprocess, "run", fail)
    assert scanned(root) == {"output/Source.java"}


def test_incremental_scan_removes_old_copies_after_new_ignore_rule(tmp_path, agent_home):
    root = repository(tmp_path)
    source(root, "output/Copy.java", "class Copy { int copied() { return 1; } }\n")
    source(root, "src/Source.java")
    project = {"id": "ignored-copy", "root_path": str(root), "name": "temporary", "root_key": str(root)}
    intel = CodeIntelService(Projects())
    try:
        asyncio.run(intel.scan(project))
        index = intel.index(project["id"])
        assert index.file_row("output/Copy.java")
        assert index.symbols_for("output/Copy.java")
        source(root, ".gitignore", "/output/\n")
        asyncio.run(intel.scan(project))
        summary = index.scan_summary()
        assert summary["mode"] == "incremental" and summary["removed"] == 1
        assert index.file_row("output/Copy.java") is None
        assert index.symbols_for("output/Copy.java") == []
        assert index.symbols_for("src/Source.java")
    finally:
        intel.close()


def test_module_live_version_matches_repository_index_with_inherited_gitignore(tmp_path, agent_home):
    root = repository(tmp_path)
    source(root, ".gitignore", "*.cache\n/src/generated/\n")
    source(root, "src/Source.java")
    source(root, "src/generated/Copy.java")
    source(root, "src/ignored.cache")
    project = {"id": "ignored-module", "root_path": str(root), "name": "temporary", "root_key": str(root)}
    intel = CodeIntelService(Projects())
    try:
        asyncio.run(intel.scan(project))
        targets = {target.id: target for target in intel._collect_targets(project, limit=0)}
        for sid in ("mod:", "mod:src"):
            current = intel._business_row(project, sid, "main", refresh=True)
            assert current["content_hash"] == targets[sid].content_hash
            assert current["body_hash"] == targets[sid].body_hash
        assert "file:src/generated/Copy.java" not in targets
        assert "file:src/ignored.cache" not in targets
    finally:
        intel.close()
