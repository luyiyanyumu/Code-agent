"""The editor, feature map and framework details read one versioned journal."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.codeintel.business import business_for, declaration_at, source_of
from code_agent.codeintel.parsers import java_parser
from code_agent.codeintel.service import CodeIntelService, FileSaveConflict


JAVA = '''package demo;
/** 管理文档列表。 */
class Documents {
    /** 分页最大条数。 */
    int limit = 20;
    /** 列出指定分类中的文档。
     * @param categoryId 文档所属分类编号。
     */
    String list(Long categoryId) { return "ok"; }
    String list(String categoryId) { return categoryId; }
}
'''


class Projects:
    def get(self, pid):
        return None

    def set_scan(self, pid, summary):
        pass


@pytest.fixture
def env(tmp_path, agent_home):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Documents.java").write_text(JAVA, encoding="utf-8")
    project = {"id": "business-test", "root_path": str(root), "root_key": str(root), "name": "demo"}
    intel = CodeIntelService(Projects())
    asyncio.run(intel.scan(project))
    yield intel, project, root
    intel.close()


def find_row(intel, project, name, kind, tree="main"):
    rows = [row for row in intel.index(project["id"], tree).all_symbols()
            if row["name"] == name and row["kind"] == kind]
    return rows[0]


def resolve(intel, project, row, **kwargs):
    extra = json.loads(row["extra"]) if isinstance(row["extra"], str) else row["extra"]
    start = extra["selection_range"]["start"]
    return intel.resolve_symbol_business(project, path=row["path"], line=start["line"] + 1,
                                         column=start["character"] + 1, name=row["name"], **kwargs)


def test_manual_record_is_shared_and_fully_preserved(env):
    intel, project, _ = env
    row = find_row(intel, project, "Documents", "class")
    candidate = intel.symbol_business(project, row["id"])
    assert candidate["business"]["source"] == "comment"
    assert candidate["business"]["status"] == "unreviewed"
    saved = intel.write_symbol_business(project, row["id"], {
        "business_meaning": "为知识库提供文档查询能力。", "detail": "按分类过滤结果。",
        "inputs": [{"name": "categoryId", "meaning": "分类编号"}],
        "outputs": [{"name": "documents", "meaning": "当前分类的文档"}],
        "rules": ["省略分类时查询全部"], "effects": ["只读取数据"],
        "feature_refs": ["文档管理"], "source": "model", "status": "missing",
        "version": candidate["entity"]["version"],
    })
    business = saved["business"]
    assert business["source"] == "human" and business["status"] == "current"
    assert business["inputs"] == [{"name": "categoryId", "meaning": "分类编号"}]
    assert resolve(intel, project, row)["business"] == business
    assert intel.symbol_purpose(project, row["id"])["business"] == business
    nav = intel.features(project)
    nodes = [feature for domain in nav["domains"] for feature in domain.get("features", [])]
    node = next(feature for feature in nodes if feature["id"] == row["id"])
    assert node["business"] == business
    assert node["label_from"] == "business"
    # This is the same existing feature journal, not another product data store.
    assert len(intel._brief_store(project).rows()) == 1


def test_body_and_unsaved_draft_changes_hide_previous_business(env):
    intel, project, root = env
    row = find_row(intel, project, "list", "method")
    intel.write_symbol_business(project, row["id"], {"business_meaning": "查询文档"})
    draft = JAVA.replace('return "ok";', 'return "changed";')
    result = resolve(intel, project, row, text=draft)
    assert result["business"]["status"] == "stale"
    assert result["business"]["business_meaning"] == ""
    (root / "src" / "Documents.java").write_text(draft, encoding="utf-8")
    assert intel.symbol_business(project, row["id"])["business"]["status"] == "stale"


def test_exact_name_range_never_binds_reference_or_same_line_body(env):
    intel, project, _ = env
    rows = intel._current_business_symbols(project, "src/Documents.java")
    method = next(row for row in rows if row["kind"] == "method" and "String" in row["qname"])
    selection = method["extra"]["selection_range"]
    start = selection["start"]
    assert declaration_at(rows, start["line"] + 1, start["character"] + 1, "list") is method
    line = JAVA.splitlines()[start["line"]]
    column = line.index("return categoryId") + len("return ") + 1
    result = intel.resolve_symbol_business(project, path="src/Documents.java", line=start["line"] + 1,
                                           column=column, name="categoryId")
    assert result["available"] is False and result["entity"] is None
    assert intel.resolve_symbol_business(project, path="src/Documents.java", line=start["line"] + 1,
                                         column=start["character"] + 1, name="wrong")["available"] is False


def test_tree_and_repository_records_are_isolated(env, tmp_path):
    intel, project, root = env
    row = find_row(intel, project, "Documents", "class")
    intel.write_symbol_business(project, row["id"], {"business_meaning": "主树查询功能"})
    asyncio.run(intel.scan(project, tree_id="task:test"))
    assert intel.symbol_business(project, row["id"], tree_id="task:test")["business"]["source"] == "comment"
    intel.write_symbol_business(project, row["id"], {"business_meaning": "任务树新功能"}, tree_id="task:test")
    assert intel.symbol_business(project, row["id"])["business"]["business_meaning"] == "主树查询功能"
    other = tmp_path / "other"
    (other / "src").mkdir(parents=True)
    (other / "src" / "Documents.java").write_text(JAVA, encoding="utf-8")
    other_project = {**project, "id": "other", "root_path": str(other)}
    asyncio.run(intel.scan(other_project))
    assert intel.symbol_business(other_project, row["id"])["business"]["source"] == "comment"


def test_legacy_human_and_model_records_retain_provenance(env):
    intel, project, _ = env
    row = next(row for row in intel.index(project["id"]).all_symbols() if row["kind"] == "method" and not row["doc"])
    journal = intel._brief_store(project)
    for model, expected in (("human", "human"), ("fake-model", "model")):
        legacy = {"symbol_id": row["id"], "text": "旧功能说明", "model": model,
                  "content_hash": row["content_hash"], "body_hash": row["body_hash"]}
        journal.path.parent.mkdir(parents=True, exist_ok=True)
        journal.path.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")
        business = intel.symbol_business(project, row["id"])["business"]
        assert business["source"] == expected
        assert business["status"] == ("current" if expected == "human" else "unreviewed")
        assert source_of(legacy) == expected


def test_file_module_and_root_records_share_feature_domain_summary(env):
    intel, project, root = env
    for target in ("file:src/Documents.java", "mod:src", "mod:"):
        saved = intel.write_symbol_business(project, target, {"business_meaning": f"业务 {target}"})
        assert intel.symbol_business(project, target)["business"] == saved["business"]
    nav = intel.features(project)
    domain = next(item for item in nav["domains"] if item["id"] == "fdomain:src")
    assert domain["business"]["business_meaning"] == "业务 mod:src"
    (root / "src" / "new.py").write_text("a = 1\n", encoding="utf-8")
    assert intel.symbol_business(project, "mod:src")["business"]["status"] == "stale"


def test_version_conflict_and_bounded_plain_schema(env):
    intel, project, root = env
    row = find_row(intel, project, "limit", "field")
    original = intel.symbol_business(project, row["id"])["entity"]["version"]
    (root / "src" / "Documents.java").write_text(JAVA.replace("limit = 20", "limit = 50"), encoding="utf-8")
    with pytest.raises(FileSaveConflict):
        intel.write_symbol_business(project, row["id"], {"business_meaning": "最大条数", "version": original})
    with pytest.raises(ValueError):
        intel.write_symbol_business(project, row["id"], {"business_meaning": "最大条数", "rules": "bad"})
    with pytest.raises(ValueError):
        intel.resolve_symbol_business(project, path="src/Documents.java", line=1, column=1, text="x" * (2 * 1024 * 1024 + 1))


def test_missing_and_stale_do_not_expose_old_semantics():
    row = {"content_hash": "new", "body_hash": "body"}
    assert business_for(row)["status"] == "missing"
    result = business_for(row, {"text": "incorrect", "model": "human", "content_hash": "old"})
    assert result["status"] == "stale" and not result["business_meaning"]


def test_java_raw_comment_is_plain_and_summary_is_not_repeated(env):
    intel, project, root = env
    source = '''package demo;
class Documents {
    /**
     * **内联打开**原文（在线阅读）。
     *
     * <p>与 download 的区别就在 Content-Disposition：这里是 {@code inline} +
     * 按扩展名给正确的 Content-Type，于是 PDF 会交给浏览器自带的阅读器渲染、
     * 图片/文本/音视频直接在页面上显示，而不是弹一个下载框。
     * 资料库的"打开阅读"用它做画面，抽取出来的正文另走 detail 接口。
     */
    @GetMapping("/{id}/raw")
    public Resource raw(Long id) { return null; }
}
'''
    (root / "src" / "Documents.java").write_text(source, encoding="utf-8")
    asyncio.run(intel.scan(project))
    row = find_row(intel, project, "raw", "method")
    business = resolve(intel, project, row)["business"]
    assert business["business_meaning"] == "内联打开原文（在线阅读）。"
    assert business["detail"].startswith("与 download 的区别就在 Content-Disposition：这里是 inline +")
    assert '抽取出来的正文另走 detail 接口。' in business["detail"]
    assert business["business_meaning"] not in business["detail"]
    assert not any(marker in business["detail"] for marker in ("<p>", "{@code", "**"))
    assert business["source"] == "comment" and business["status"] == "unreviewed"
    assert intel.symbol_business(project, row["id"])["business"] == business
    assert intel.symbol_purpose(project, row["id"])["business"] == business


@pytest.mark.parametrize("doc,summary,detail", [
    ("**查询**文档。", "查询文档。", ""),
    ("查询文档。按分类过滤。", "查询文档。", "按分类过滤。"),
    ("[打开](https://example.test) 文档\n\n返回 `List<String>`。", "打开 文档", "返回 List<String>。"),
    ("Return documents. Limit the result.\n\nOnly visible entries.",
     "Return documents.", "Limit the result.\n\nOnly visible entries."),
    ("<b>返回列表。</b><p>类型为 <code>List&lt;String&gt;</code>，示例 {@literal <p>}。",
     "返回列表。", "类型为 List<String>，示例 <p>。"),
])
def test_comment_description_separates_summary_and_plain_details(doc, summary, detail):
    business = business_for({"doc": doc})
    assert business["business_meaning"] == summary
    assert business["detail"] == detail


def test_comment_parameter_and_return_markup_is_plain():
    source = '''class Documents {
    /** 查询文档。
     * @param categoryId 文档的 {@link Category 分类}编号，省略时使用 **全部**。
     * @return <code>List&lt;String&gt;</code> 文档列表。
     */
    String list(Long categoryId) { return null; }
}
'''
    row = next(row for row in java_parser.parse("Documents.java", source)["symbols"] if row["kind"] == "method")
    business = business_for(row)
    assert business["inputs"] == [{"name": "categoryId", "meaning": "文档的 分类编号，省略时使用 全部。"}]
    assert business["outputs"] == [{"name": "return", "meaning": "List<String> 文档列表。"}]


def test_saved_manual_function_text_is_not_comment_normalized():
    row = {"doc": "作者说明。", "content_hash": "same"}
    saved = {"business_meaning": "**人工确认**的功能", "detail": "保留 `value` 格式。",
             "content_hash": "same", "source": "human"}
    business = business_for(row, saved)
    assert business["business_meaning"] == saved["business_meaning"]
    assert business["detail"] == saved["detail"]


def test_delayed_model_fill_cannot_overwrite_current_manual_record(env):
    intel, project, _ = env
    row = find_row(intel, project, "Documents", "class")
    saved = intel.write_symbol_business(project, row["id"], {"business_meaning": "人工确认的功能"})
    intel.feature_brief_write(project, symbol_id=row["id"], text="迟到的模型猜测", model="fake-model")
    assert intel.symbol_business(project, row["id"])["business"] == saved["business"]
    assert len(intel._brief_store(project).rows()) == 1


def test_module_read_cache_expires_and_index_revision_invalidates(env, monkeypatch):
    from code_agent.codeintel import service as service_mod

    intel, project, root = env
    clock = [20.0]
    monkeypatch.setattr(service_mod, "monotonic", lambda: clock[0])
    original_walk = service_mod.walk_project
    calls = []

    def tracked_walk(path):
        calls.append(path)
        return original_walk(path)

    monkeypatch.setattr(service_mod, "walk_project", tracked_walk)
    first = intel.symbol_business(project, "mod:src")
    assert intel.symbol_business(project, "mod:src") == first
    assert len(calls) == 1
    source = root / "src" / "Documents.java"
    source.write_text(JAVA.replace("limit = 20", "limit = 21"), encoding="utf-8")
    clock[0] += 2.1
    changed = intel.symbol_business(project, "mod:src")
    assert changed["entity"]["version"] != first["entity"]["version"]
    assert len(calls) == 2
    asyncio.run(intel.scan(project))
    calls.clear()
    intel.symbol_business(project, "mod:src")
    assert len(calls) == 1  # change_seq defeats an otherwise valid short TTL.


def test_module_write_bypasses_read_cache_for_version_check(env):
    intel, project, root = env
    first = intel.symbol_business(project, "mod:src")
    (root / "src" / "Documents.java").write_text(JAVA.replace("limit = 20", "limit = 40"), encoding="utf-8")
    with pytest.raises(FileSaveConflict):
        intel.write_symbol_business(project, "mod:src", {"business_meaning": "旧版本业务", "version": first["entity"]["version"]})


def test_api_is_owned_and_document_endpoint_removed(tmp_path, agent_home):
    root = tmp_path / "api-repo"
    root.mkdir()
    (root / "Documents.java").write_text(JAVA, encoding="utf-8")
    with TestClient(create_app(provider_name="offline")) as client:
        token = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        project = client.post("/api/v1/code/projects", json={"mode": "open", "path": str(root)}, headers=headers).json()
        base = f"/api/v1/code/projects/{project['id']}"
        client.post(base + "/scan", headers=headers)
        sid = next(row["id"] for row in client.app.state.code_intel.index(project["id"]).all_symbols() if row["kind"] == "class")
        version = client.get(base + "/symbol/business", params={"id": sid}, headers=headers).json()["entity"]["version"]
        assert client.put(base + "/symbol/business", json={"id": sid, "business_meaning": "绕过版本"}, headers=headers).status_code == 422
        saved = client.put(base + "/symbol/business", json={"id": sid, "business_meaning": "文档查询", "version": version}, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()["business"]["source"] == "human"
        entity = saved.json()["entity"]
        resolved = client.post(base + "/symbol/business/resolve", json={"path": entity["path"], "line": entity["line"],
                               "column": entity["column"], "name": entity["name"]}, headers=headers)
        assert resolved.json()["business"] == saved.json()["business"]
        assert client.get(base + "/symbol/business", params={"id": sid, "tree": "task:missing"}, headers=headers).status_code == 404
        workspace = tmp_path / "task-worktree"
        workspace.mkdir()
        (workspace / "Documents.java").write_text(JAVA.replace("limit = 20", "limit = 50"), encoding="utf-8")
        owned = client.app.state.projects.get(project["id"])
        task = client.app.state.tasks.create(project_id=project["id"], user_id=owned["user_id"], title="task",
                                            requirement="test", base_branch="main", branch="task", worktree=str(workspace))
        tree = f"task:{task['id']}"
        asyncio.run(client.app.state.code_intel.scan({**owned, "root_path": str(workspace)}, tree_id=tree))
        scoped = client.get(base + "/symbol/business", params={"id": sid, "tree": tree}, headers=headers)
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["business"]["source"] == "comment"
        saved_task = client.put(base + "/symbol/business", json={"id": sid, "tree": tree, "business_meaning": "任务树查询", "version": scoped.json()["entity"]["version"]}, headers=headers)
        assert saved_task.status_code == 200, saved_task.text
        assert client.get(base + "/symbol/business", params={"id": sid}, headers=headers).json()["business"]["business_meaning"] == "文档查询"
        other_user = client.app.state.users.create("other", "irrelevant-hash")
        foreign = client.app.state.tasks.create(project_id=project["id"], user_id=other_user["id"], title="foreign",
                                               requirement="test", base_branch="main", branch="foreign", worktree=str(workspace))
        assert client.get(base + "/symbol/business", params={"id": sid, "tree": f"task:{foreign['id']}"}, headers=headers).status_code == 404
        assert client.get(base + "/doc", headers=headers).status_code == 404
        assert client.put(base + "/symbol/business", json={"id": sid, "business_meaning": ""}, headers=headers).status_code == 422
