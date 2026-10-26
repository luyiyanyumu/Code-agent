"""代码情报 P1 的 API 层：索引状态 / 扫描 / 地图 / 增量 / 符号详情 / 保真度校验。

覆盖两件在产品上致命的事：
1. **首次打开项目要能自解释**（`ensure=1` 自动建索引），而不是回一张空地图让用户猜；
2. **越权访问一律 404**：索引里含着源码结构与行号，泄漏等于泄漏代码。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.api.security import hash_password
from code_agent.codeintel.vcs import git_available


@pytest.fixture
def app(agent_home, monkeypatch):
    # These tests exercise explicit indexing; the filesystem watcher has its
    # own tests and must not race the empty-index assertions here.
    monkeypatch.setenv("CODE_AGENT_CODE_WATCH", "0")
    async def no_embedding_probe():
        return {"found": False}
    monkeypatch.setattr("code_agent.retrieval.embed.autodetect_local", no_embedding_probe)
    return create_app(provider_name="offline")


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "svc"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text('"""服务包。"""\n', encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        '"""核心逻辑。"""\n\nimport json\n\nfrom .util import double\n\n\nclass Engine:\n'
        '    """引擎。"""\n\n    def run(self, n: int) -> int:\n        return double(n)\n\n\n'
        'def helper(a, b=1):\n    return a + b\n\n\nif __name__ == "__main__":\n    helper(1)\n',
        encoding="utf-8",
    )
    (root / "pkg" / "util.py").write_text(
        'def double(x: int) -> int:\n    """翻倍。"""\n    return x * 2\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text('[project]\nname="svc"\nversion="0.1.0"\n', encoding="utf-8")
    return root


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _open(client, token, path):
    r = client.post("/api/v1/code/projects", json={"mode": "open", "path": str(path)}, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_meta_reports_map_feature(client):
    token = _login(client)
    body = client.get("/api/v1/code/meta", headers=_auth(token)).json()
    # 能力位必须如实：报的是"服务是否真的接上了"，不是阶段编号
    assert body["features"]["map"] is True
    assert body["features"]["lsp"] is True
    assert body["features"]["tasks"] is True
    assert body["features"]["parallel"] is True
    assert body["features"]["git_write"] == git_available()
    # 功能视图（M33）与地图同源：索引在就可用
    assert body["features"]["feature_map"] is True


def test_index_requires_scan_then_map_has_content(client, repo):
    token = _login(client)
    pid = _open(client, token, repo)

    # 未扫描：索引状态"空但可用"，地图不会崩，只是带 warning
    status = client.get(f"/api/v1/code/projects/{pid}/index", headers=_auth(token)).json()
    assert status["indexed"] is False and status["symbols"] == 0
    empty = client.get(f"/api/v1/code/projects/{pid}/map", headers=_auth(token)).json()
    assert empty["stats"]["symbols"] == 0
    assert any("扫描" in w for w in empty["warnings"])

    # 扫描
    scan = client.post(f"/api/v1/code/projects/{pid}/scan", headers=_auth(token)).json()
    # A cold map may already have started the automatic scan. The explicit
    # request coalesces with that work rather than running a competing scan.
    if scan["status"] == "busy":
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = client.get(f"/api/v1/code/projects/{pid}/index", headers=_auth(token)).json()
            if current["indexed"]:
                scan = {**current, "status": "done"}
                break
            Event().wait(0.02)
    assert scan["status"] == "done", scan
    assert scan["symbols"] >= 4  # Engine + run + helper + double
    assert scan["scan"]["parsed"] >= 3

    status = client.get(f"/api/v1/code/projects/{pid}/index", headers=_auth(token)).json()
    assert status["indexed"] is True
    assert status["kinds"]["class"] >= 1
    assert status["change_seq"] > 0
    assert status["scanning"] is False

    # 增量再扫：全部复用
    again = client.post(f"/api/v1/code/projects/{pid}/scan", headers=_auth(token)).json()
    assert again["scan"]["reused"] >= 3 and again["scan"]["parsed"] == 0


def test_map_ensure_scans_on_demand(client, repo):
    token = _login(client)
    pid = _open(client, token, repo)
    body = client.get(
        f"/api/v1/code/projects/{pid}/map", params={"ensure": 1, "depth": 3}, headers=_auth(token)
    ).json()
    assert body["stats"]["symbols"] >= 4
    assert body["project"]["name"] == "svc"
    # 结构树里有 pkg 目录与 core.py，且符号带行号（前端跳转依赖）
    pkg = next(c for c in body["structure"]["children"] if c["name"] == "pkg")
    core = next(c for c in pkg["children"] if c["name"] == "core.py")
    engine = next(s for s in core["symbols"] if s["name"] == "Engine")
    assert engine["line"] < engine["end_line"]
    assert any(s["name"] == "run" and s["parent"] == engine["id"] for s in core["symbols"])
    # 模块用途来自 __init__.py 的模块 docstring
    modules = {m["path"]: m for m in body["modules"]}
    assert modules["pkg"]["purpose"] == "服务包。"
    # 导入边：core.py → util.py
    imports = body["edges"]["imports"]
    assert any(e["from"] == "file:pkg/core.py" and e["to"] == "file:pkg/util.py" for e in imports)


def test_map_filters_and_limits(client, repo):
    token = _login(client)
    pid = _open(client, token, repo)
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=_auth(token))

    shallow = client.get(
        f"/api/v1/code/projects/{pid}/map", params={"depth": 1}, headers=_auth(token)
    ).json()
    pkg = next(c for c in shallow["structure"]["children"] if c["name"] == "pkg")
    assert all(not c.get("symbols") for c in pkg["children"] if c["kind"] == "file")

    only_class = client.get(
        f"/api/v1/code/projects/{pid}/map", params={"kinds": "class,function"}, headers=_auth(token)
    ).json()
    # 过滤生效：返回的符号只有这两类；而 stats.kinds 仍是全量构成（图例用），并给出过滤条件
    returned: list[dict] = []
    stack = [only_class["structure"]]
    while stack:
        node = stack.pop()
        for child in node.get("children", []):
            if child["kind"] == "file":
                returned.extend(child.get("symbols", []))
            else:
                stack.append(child)
    assert {s["kind"] for s in returned} == {"class", "function"}
    assert only_class["stats"]["filter"]["kinds"] == ["class", "function"]
    assert "method" in only_class["stats"]["kinds"]  # 全量构成里仍有 method

    scoped = client.get(
        f"/api/v1/code/projects/{pid}/map", params={"module": "pkg"}, headers=_auth(token)
    ).json()
    assert scoped["structure"]["path"] == "pkg"
    assert any("pyproject" in w for w in scoped["warnings"]) is False

    no_edges = client.get(
        f"/api/v1/code/projects/{pid}/map", params={"edges": 0}, headers=_auth(token)
    ).json()
    assert no_edges["edges"] == {"imports": [], "inherits": [], "referenced_by": []}


def test_delta_endpoint_tracks_edits(client, repo):
    token = _login(client)
    pid = _open(client, token, repo)
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=_auth(token))
    base = client.get(f"/api/v1/code/projects/{pid}/index", headers=_auth(token)).json()["change_seq"]

    # 外部改动：新增一个函数
    core = repo / "pkg" / "core.py"
    core.write_text(core.read_text(encoding="utf-8") + "\n\ndef added():\n    return 1\n", encoding="utf-8")
    client.post(f"/api/v1/code/projects/{pid}/scan", headers=_auth(token))

    delta = client.get(
        f"/api/v1/code/projects/{pid}/map/delta", params={"after": base}, headers=_auth(token)
    ).json()
    assert {n["name"] for n in delta["added"]} == {"added"}
    assert delta["files"] == ["pkg/core.py"]
    assert delta["edges_dirty"] is True
    assert delta["to_seq"] > base

    # after 取最新序列：空增量（前端轮询常态）
    idle = client.get(
        f"/api/v1/code/projects/{pid}/map/delta", params={"after": delta["to_seq"]}, headers=_auth(token)
    ).json()
    assert idle["added"] == [] and idle["removed"] == [] and idle["changes"] == []


def test_symbol_detail_endpoint(client, repo):
    token = _login(client)
    pid = _open(client, token, repo)
    body = client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=_auth(token)).json()
    pkg = next(c for c in body["structure"]["children"] if c["name"] == "pkg")
    core = next(c for c in pkg["children"] if c["name"] == "core.py")
    engine = next(s for s in core["symbols"] if s["name"] == "Engine")

    detail = client.get(
        f"/api/v1/code/projects/{pid}/symbol",
        params={"id": engine["id"], "with_source": 1},
        headers=_auth(token),
    ).json()
    assert detail["doc"] == "引擎。"
    assert {m["name"] for m in detail["members"]} == {"run"}
    assert detail["source"].startswith("class Engine")

    # 反向依赖：core.py import 了 util.py，所以 double 的 importers 里应当有 core.py
    util = next(c for c in pkg["children"] if c["name"] == "util.py")
    double = next(s for s in util["symbols"] if s["name"] == "double")
    double_detail = client.get(
        f"/api/v1/code/projects/{pid}/symbol", params={"id": double["id"]}, headers=_auth(token)
    ).json()
    assert "file:pkg/core.py" in double_detail["importers"]
    # 而 Engine 没有被人 import：importers 必须为空（不能为了好看乱编关系）
    assert detail["importers"] == []

    missing = client.get(
        f"/api/v1/code/projects/{pid}/symbol", params={"id": "sym:pkg/core.py#Nope"}, headers=_auth(token)
    )
    assert missing.status_code == 404


def test_reindex_verifies_fidelity(client, repo):
    token = _login(client)
    pid = _open(client, token, repo)
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=_auth(token))

    clean = client.post(f"/api/v1/code/projects/{pid}/reindex", headers=_auth(token)).json()
    assert clean["ok"] is True
    assert clean["fidelity"] == 1.0

    (repo / "pkg" / "util.py").write_text("def triple(x):\n    return x * 3\n", encoding="utf-8")
    drifted = client.post(f"/api/v1/code/projects/{pid}/reindex", headers=_auth(token)).json()
    assert drifted["ok"] is False
    assert any("util.py" in d["path"] for d in drifted["drift"])
    # 校验是只读的：不会顺手把索引"修好"（否则它就是在自证清白）
    assert client.post(f"/api/v1/code/projects/{pid}/reindex", headers=_auth(token)).json()["ok"] is False


def test_digest_endpoint(client, repo):
    token = _login(client)
    pid = _open(client, token, repo)
    body = client.get(
        f"/api/v1/code/projects/{pid}/digest",
        params={"max_chars": 900, "tree": "main"},
        headers=_auth(token),
    ).json()
    assert body["chars"] == len(body["text"]) <= 900
    assert "项目地图" in body["text"]


def test_index_endpoints_are_owner_scoped(app, client, repo):
    admin = _login(client)
    pid = _open(client, admin, repo)
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=_auth(admin))

    app.state.users.create("eve", hash_password("eve123"), role="user")
    eve = _login(client, "eve", "eve123")
    for path in (
        f"/api/v1/code/projects/{pid}/index",
        f"/api/v1/code/projects/{pid}/map",
        f"/api/v1/code/projects/{pid}/map/delta",
        f"/api/v1/code/projects/{pid}/digest",
    ):
        assert client.get(path, headers=_auth(eve)).status_code == 404, path
    assert (
        client.get(
            f"/api/v1/code/projects/{pid}/symbol", params={"id": "x"}, headers=_auth(eve)
        ).status_code
        == 404
    )
    assert client.post(f"/api/v1/code/projects/{pid}/scan", headers=_auth(eve)).status_code == 404
    assert (
        client.post(f"/api/v1/code/projects/{pid}/reindex", headers=_auth(eve)).status_code == 404
    )


def test_index_endpoints_require_auth(client):
    assert client.get("/api/v1/code/projects/x/index").status_code == 401
    assert client.get("/api/v1/code/projects/x/map").status_code == 401
    assert client.post("/api/v1/code/projects/x/scan").status_code == 401


def test_scan_reports_busy_when_already_running(client, repo, monkeypatch):
    """单飞：同一项目并发扫描时第二次应回 busy，而不是叠一次全量工作。"""
    token = _login(client)
    pid = _open(client, token, repo)
    service = client.app.state.code_intel
    entered = Event()
    release = Event()
    scan_sync = service._scan_sync

    def blocked_scan(*args, **kwargs):
        entered.set()
        assert release.wait(10), "测试没有释放首次扫描"
        return scan_sync(*args, **kwargs)

    monkeypatch.setattr(service, "_scan_sync", blocked_scan)
    url = f"/api/v1/code/projects/{pid}/scan"
    with ThreadPoolExecutor(max_workers=1) as requests:
        first = requests.submit(client.post, url, headers=_auth(token))
        try:
            assert entered.wait(5), "首次 HTTP 请求没有进入后台扫描"
            response = client.post(url, headers=_auth(token))
            assert response.status_code == 200, response.text
            busy = response.json()
            assert busy["status"] == "busy"
            assert busy["scanning"] is True
            assert not first.done(), "首次扫描在释放前不应结束"
        finally:
            release.set()
            completed = first.result(timeout=10)
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "done"
    assert completed.json()["scanning"] is False


def test_feature_nav_lists_domains_and_drills_down(client, repo):
    """功能导航：第一屏是 能力域 → 功能，点一个功能能继续往下拆（细分功能）。"""
    token = _login(client)
    pid = _open(client, token, repo)
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=_auth(token))

    body = client.get(f"/api/v1/code/projects/{pid}/features", headers=_auth(token)).json()
    assert body["source"] == "auto"
    assert body["method"] and body["note"]
    domains = {d["name"]: d for d in body["domains"]}
    # `pkg/core.py` 里有 `if __name__ == "__main__"`，但那段是裸调用（`helper(1)`、没有 main），
    # 所以功能挂在文件上——有 main 才挂 main 的符号 id（点开才有实现流程），这条兜底规则要显性
    assert "pkg" in domains, list(domains)
    core = [f for f in domains["pkg"]["features"] if f["path"] == "pkg/core.py"]
    assert core, domains["pkg"]["features"]
    # 每条功能都要有代码入口与三态证据；计数自洽
    for domain in body["domains"]:
        for f in domain["features"]:
            assert f["id"], f
            assert f["test_state"] in {"direct", "file", "none"}, f
    assert body["totals"]["features"] == sum(d["shown"] for d in body["domains"])

    # 往下拆一层：拆不动必须说清"为什么到此为止"，而不是返回一个空树
    target = next(f for f in domains["pkg"]["features"] if f["has_children"])
    kids = client.get(
        f"/api/v1/code/projects/{pid}/features/children",
        params={"id": target["id"]},
        headers=_auth(token),
    ).json()
    assert kids["id"] == target["id"]
    assert isinstance(kids["children"], list)
    assert kids["leaf"] is (len(kids["children"]) == 0)
    if kids["leaf"]:
        # 拆不下去必须说明"为什么到此为止"，否则等于留一个空箭头让人反复点
        assert kids["note"], "原子节点必须有一句说明"
    for child in kids["children"]:
        assert child["id"] and child["label"]

    # 缺 id / 未知 id：一个是 422，一个是 422（"符号不存在"），都不能返回空树糊弄
    assert (
        client.get(
            f"/api/v1/code/projects/{pid}/features/children", headers=_auth(token)
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/code/projects/{pid}/features/children",
            params={"id": "sym:pkg/none.py#ghost"},
            headers=_auth(token),
        ).status_code
        == 422
    )


def test_feature_nav_is_owner_scoped_and_needs_auth(app, client, repo):
    admin = _login(client)
    pid = _open(client, admin, repo)
    app.state.users.create("eve3", hash_password("eve123"), role="user")
    eve = _login(client, "eve3", "eve123")
    for path in (
        f"/api/v1/code/projects/{pid}/features",
        f"/api/v1/code/projects/{pid}/features/children?id=sym:x",
    ):
        assert client.get(path, headers=_auth(eve)).status_code == 404, path
    assert client.get("/api/v1/code/projects/x/features").status_code == 401
    assert client.get("/api/v1/code/projects/x/features/children?id=sym:x").status_code == 401


# ---------------------------------------------------------------------------
# 功能描述（M33）：第一次导入自动补全部，之后靠固定按钮增量补
# ---------------------------------------------------------------------------
class _FakeProvider:
    """假模型：不联网也能验完"扫描 → 自动补 → 落库 → 导航变样"这条链。"""

    name = "fake"
    model = "fake-model"

    async def chat(self, messages, tools):
        import json as _json

        from code_agent.models.events import ModelTurn

        assert tools == [], "补描述这条路不该带任何工具"
        count = len(messages[-1]["content"].split("\n\n"))
        payload = {"briefs": [{"n": i, "text": f"自动补的第{i}条"} for i in range(1, count + 1)]}
        return ModelTurn(text=_json.dumps(payload, ensure_ascii=False))


def _classes_repo(tmp_path: Path) -> Path:
    """两个没有 docstring 的类：类一定进导航，所以是"待补"的典型。"""
    root = tmp_path / "briefs_repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        "class Alpha:\n    size: int = 1\n\n\nclass Beta:\n    size: int = 2\n",
        encoding="utf-8",
    )
    return root


def _wait_fill(client, token, pid, *, timeout: float = 15.0) -> dict:
    """等后台补描述跑完，返回最终进度。"""
    import time

    deadline = time.time() + timeout
    status: dict = {}
    while time.time() < deadline:
        status = client.get(
            f"/api/v1/code/projects/{pid}/feature-briefs/fill", headers=_auth(token)
        ).json()
        if not status["running"]:
            return status
        time.sleep(0.05)
    raise AssertionError(f"补描述没跑完：{status}")


def test_first_scan_autofills_all_then_remembers(app, client, tmp_path):
    """第一次导入（首次扫描完成）自动补全部；再扫描不再重复跑。"""
    token = _login(client)
    pid = _open(client, token, _classes_repo(tmp_path))
    app.state.code_intel.set_brief_provider(lambda: _FakeProvider())

    scan = client.post(f"/api/v1/code/projects/{pid}/scan", headers=_auth(token)).json()
    assert scan["briefs_autofill"]["started"] is True
    assert scan["briefs_autofill"]["state"] == "queued"

    status = _wait_fill(client, token, pid)
    assert status["written"] == 7 and status["failed"] == 0
    assert status["coverage_known"] and status["described"] == status["coverage_total"]

    body = client.get(f"/api/v1/code/projects/{pid}/feature-briefs", headers=_auth(token)).json()
    assert body["count"] == 7 and body["stale"] == 0
    # 描述落在平台目录里，不进用户仓库
    assert "feature-briefs" in body["path"]

    nav = client.get(f"/api/v1/code/projects/{pid}/features", headers=_auth(token)).json()
    nodes = [f for d in nav["domains"] for f in d["features"]]
    assert [n["name"] for n in nodes] == ["Alpha", "Beta"] or len(nodes) == 2
    assert all(n["label_from"] == "brief" for n in nodes), "补过之后功能名应当来自描述"
    assert all(n["brief"]["model"] == "fake-model" for n in nodes)
    assert not any(n["brief_missing"] for n in nodes)

    # 第二次扫描：不再自动跑（一个仓库只自动补一次），但如实说明原因
    again = client.post(f"/api/v1/code/projects/{pid}/scan", headers=_auth(token)).json()
    assert again["briefs_autofill"]["started"] is False
    assert "已检查" in again["briefs_autofill"]["reason"]

    # 进度搭在索引状态里（界面按它显示"正在补 3/7"）
    idx = client.get(f"/api/v1/code/projects/{pid}/index", headers=_auth(token)).json()
    assert idx["briefs"]["running"] is False
    assert idx["briefs"]["total"] == 7 and idx["briefs"]["written"] == 7


def test_fill_button_endpoints_start_and_report(app, client, tmp_path):
    """固定按钮用的一对接口：开始 + 查进度；没有缺的时如实说"不用补"。"""
    token = _login(client)
    pid = _open(client, token, _classes_repo(tmp_path))
    app.state.code_intel.set_brief_provider(None)
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=_auth(token))
    app.state.code_intel.set_brief_provider(lambda: _FakeProvider())

    started = client.post(
        f"/api/v1/code/projects/{pid}/feature-briefs/fill", headers=_auth(token)
    ).json()
    assert started == {"started": True, "total": 0, "auto": False, "state": "queued"}
    assert _wait_fill(client, token, pid)["written"] == 7

    empty = client.post(
        f"/api/v1/code/projects/{pid}/feature-briefs/fill", headers=_auth(token)
    ).json()
    assert empty["started"] is True
    status = _wait_fill(client, token, pid)
    assert status["state"] == "complete" and status["written"] == 0


def test_fill_endpoints_are_owner_scoped(app, client, tmp_path):
    """描述也是代码情报：别人的项目一律 404，未登录 401。"""
    admin = _login(client)
    pid = _open(client, admin, _classes_repo(tmp_path))
    app.state.users.create("eve4", hash_password("eve123"), role="user")
    eve = _login(client, "eve4", "eve123")
    for path in (
        f"/api/v1/code/projects/{pid}/feature-briefs",
        f"/api/v1/code/projects/{pid}/feature-briefs/fill",
    ):
        assert client.get(path, headers=_auth(eve)).status_code == 404, path
    # 补描述只有 POST 这一个入口（列表是 GET-only），越权同样 404
    assert client.post(
        f"/api/v1/code/projects/{pid}/feature-briefs/fill", headers=_auth(eve)
    ).status_code == 404
    assert client.get(f"/api/v1/code/projects/{pid}/feature-briefs").status_code == 401
    assert client.post(f"/api/v1/code/projects/{pid}/feature-briefs/fill").status_code == 401


def test_autofill_keeps_the_author_s_words(app, client, repo):
    """批量补只补"没写 docstring"的：作者写了的不会被模型的描述顶掉。"""
    token = _login(client)
    pid = _open(client, token, repo)
    app.state.code_intel.set_brief_provider(lambda: _FakeProvider())
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=_auth(token))
    client.post(f"/api/v1/code/projects/{pid}/feature-briefs/fill", headers=_auth(token))
    _wait_fill(client, token, pid)

    nav = client.get(f"/api/v1/code/projects/{pid}/features", headers=_auth(token)).json()
    nodes = [f for d in nav["domains"] for f in d["features"]]
    documented = [n for n in nodes if n["has_doc"]]
    assert documented, "样例仓库里应当有作者写了 docstring 的符号"
    for node in documented:
        assert node["label_from"] == "doc" and node["brief"] is None


def test_open_existing_index_schedules_automatic_fill_from_sync_status(app, client, tmp_path):
    """An editor opening an indexed project needs no scan or fill button."""
    token = _login(client)
    pid = _open(client, token, _classes_repo(tmp_path))
    app.state.code_intel.set_brief_provider(None)
    client.post(f"/api/v1/code/projects/{pid}/scan", headers=_auth(token))
    provider = _FakeProvider()
    app.state.code_intel.set_brief_provider(lambda: provider)
    body = client.get(f"/api/v1/code/projects/{pid}/index", headers=_auth(token)).json()
    assert body["briefs"]["state"] in {"queued", "running", "complete"}
    status = _wait_fill(client, token, pid)
    assert status["state"] == "complete" and status["missing"] == 0
    assert status["written"] == 7
    revision, epoch = status["revision"], status["revision_epoch"]
    for _ in range(3):
        again = client.get(f"/api/v1/code/projects/{pid}/index", headers=_auth(token)).json()["briefs"]
        assert not again["running"] and again["revision"] == revision and again["revision_epoch"] == epoch
