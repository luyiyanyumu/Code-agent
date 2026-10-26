"""导入依赖的精确解析与文件清单变化后的增量关系恢复。"""

import asyncio
from pathlib import Path

from code_agent.codeintel import graph
from code_agent.codeintel import service as service_module
from code_agent.codeintel.parsers import parse_file
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.store import file_id


def test_python_package_submodule_imports_are_kept():
    known = {"pkg/__init__.py", "pkg/service.py", "pkg/helper.py"}
    for source in ("from . import helper", "from pkg import helper"):
        parsed = parse_file("pkg/service.py", "python", source)
        edges, external = graph.import_edges("pkg/service.py", parsed["imports"], known)
        assert (file_id("pkg/service.py"), file_id("pkg/helper.py"), "imports", 1) in edges
        assert external == 0
    assert graph.resolve_import("pkg/service.py", "helper", 4, {"helper.py"}) is None


def test_js_multiline_imports_and_reexports_ignore_comments():
    source = '''
/* import Fake from './fake.js' */
import {
  // from './fake.js'
  First,
  Second,
} from './shared.js'
export {
  First,
} from './shared.js'
import './style.css'
'''
    parsed = parse_file("src/main.js", "javascript", source)
    assert [item["module"] for item in parsed["imports"]] == [
        "./shared.js", "./shared.js", "./style.css"
    ]
    edges, external = graph.import_edges(
        "src/main.js", parsed["imports"], {"src/main.js", "src/shared.js", "src/style.css"}
    )
    assert (file_id("src/main.js"), file_id("src/shared.js"), "imports", 2) in edges
    assert external == 0


def test_java_explicit_and_static_imports_resolve_full_package_paths():
    rel = "backend/src/main/java/org/example/api/Controller.java"
    target = "backend/src/main/java/org/example/core/Service.java"
    source = '''
package org.example.api;
/*
import org.example.Fake;
*/
import org.example.core.Service;
import static org.example.core.Service.create;
import static org.example.core.Service.*;
import org.example.core.*;
public class Controller {}
'''
    parsed = parse_file(rel, "java", source)
    assert [item["module"] for item in parsed["imports"]] == [
        "org.example.core.Service", "org.example.core.Service",
        "org.example.core.Service", "org.example.core.*",
    ]
    edges, external = graph.import_edges(rel, parsed["imports"], {rel, target})
    assert edges == [(file_id(rel), file_id(target), "imports", 3)]
    assert external == 1
    assert graph.resolve_import(rel, "org.example.core.Service", 0, {
        target, "another/src/org/example/core/Service.java"
    }) is None
    assert graph.resolve_import(rel, "Service", 0, {target}) is None


class _Projects:
    def set_scan(self, *args):
        pass


def test_new_and_recreated_target_updates_unchanged_importer(tmp_path: Path, agent_home):
    repo = tmp_path / "repo"
    repo.mkdir()
    importer = repo / "main.js"
    importer.write_text("import { value } from './shared.js'\nexport const x = 1\n", encoding="utf-8")
    project = {"id": "p_import", "name": "repo", "root_path": str(repo)}
    service = CodeIntelService(_Projects())
    try:
        asyncio.run(service.scan(project))
        index = service.index(project["id"])
        assert index.edges(kind="imports") == []
        original_fingerprint = (index.file_row("main.js")["mtime"], index.file_row("main.js")["sha1"])
        seq = index.max_change_seq()
        target = repo / "shared.js"
        target.write_text("export const value = 2\n", encoding="utf-8")
        asyncio.run(service.scan(project))
        assert index.edges(src=file_id("main.js"), dst=file_id("shared.js"))
        assert service.delta(project, seq)["edges_dirty"] is True
        assert (index.file_row("main.js")["mtime"], index.file_row("main.js")["sha1"]) == original_fingerprint
        target.unlink()
        asyncio.run(service.scan(project))
        assert index.edges(kind="imports") == []
        target.write_text("export const value = 3\n", encoding="utf-8")
        asyncio.run(service.scan(project))
        assert index.edges(src=file_id("main.js"), dst=file_id("shared.js"))
        unchanged = asyncio.run(service.scan(project))
        assert unchanged["scan"]["parsed"] == 0
    finally:
        service.close()


def test_import_only_edit_emits_delta(tmp_path: Path, agent_home):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("a.js", "b.js"):
        (repo / name).write_text("export const value = 1\n", encoding="utf-8")
    importer = repo / "main.js"
    importer.write_text("import { value } from './a.js'\nexport const x = 1\n", encoding="utf-8")
    project = {"id": "p_import_only", "name": "repo", "root_path": str(repo)}
    service = CodeIntelService(_Projects())
    try:
        asyncio.run(service.scan(project))
        index = service.index(project["id"])
        seq = index.max_change_seq()
        importer.write_text("import { value } from './b.js'\nexport const x = 1\n", encoding="utf-8")
        asyncio.run(service.scan(project))
        assert index.edges(src=file_id("main.js"), dst=file_id("b.js"))
        assert not index.edges(src=file_id("main.js"), dst=file_id("a.js"))
        delta = service.delta(project, seq)
        assert delta["edges_dirty"] is True
        assert delta["files"] == ["main.js"]
        assert delta["added"] == [] and delta["updated"] == []
    finally:
        service.close()


def test_old_import_revision_refreshes_edges_once_without_rebuilding_symbols(
    tmp_path: Path, agent_home, monkeypatch
):
    repo = tmp_path / "repo"
    package = repo / "backend/src/main/java/org/example"
    package.mkdir(parents=True)
    (package / "Service.java").write_text("public class Service {}\n", encoding="utf-8")
    (package / "Controller.java").write_text(
        "import org.example.Service;\npublic class Controller {}\n", encoding="utf-8"
    )
    project = {"id": "p_upgrade", "name": "repo", "root_path": str(repo)}
    service = CodeIntelService(_Projects())
    try:
        asyncio.run(service.scan(project))
        index = service.index(project["id"])
        # 模拟旧版已扫描、文件指纹命中，但 Java 导入边尚不存在的索引。
        index.execute("DELETE FROM edges WHERE kind='imports'")
        index.set_meta("imports_revision", "1")
        original_files = index.file_rows()
        original_symbols = index.all_symbols()
        parsed_paths = []
        original_parse = service_module.parse_file

        def track_parse(rel, lang, text):
            parsed_paths.append(rel)
            return original_parse(rel, lang, text)

        monkeypatch.setattr(service_module, "parse_file", track_parse)
        upgraded = asyncio.run(service.scan(project))
        assert upgraded["scan"]["parsed"] == 0
        assert upgraded["scan"]["import_edges_refreshed"] == 1
        assert len(parsed_paths) == 2
        assert index.edges(kind="imports")
        assert index.get_meta("imports_revision") == graph.IMPORTS_REVISION
        assert index.file_rows() == original_files
        assert index.all_symbols() == original_symbols
        parsed_paths.clear()
        unchanged = asyncio.run(service.scan(project))
        assert unchanged["scan"]["parsed"] == 0
        assert unchanged["scan"]["import_edges_refreshed"] == 0
        assert parsed_paths == []
    finally:
        service.close()
