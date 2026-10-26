"""功能导航（M33）：把一个项目拆成 能力域 → 功能 → 细分功能 → … → 原子步骤。

第一版做错了（只列入口清单：路由/钩子/工具各归一堆），测试也跟着定了几条纪律，
现在这版要同时满足它们：

1. **功能是"能干什么"，不是"文件在哪"**：域按二级目录分，但域里的功能要能被读懂——
   功能名优先用作者写的 docstring 首句（`失败指纹：工具名 + 归一化参数` → `失败指纹`）；
   没 doc 的类仍然算功能（一个类就是一块能力：`AgentLoop` = agent 循环），但要标出「无说明」；
2. **装配层透明**：`mount_code_api` / `create_app` 这种"把东西挂起来"的函数不是功能层次，
   真机踩到——65 个路由全躲在 `mount_code_api` 里，第一屏只剩 6 条；
3. **能一直往下拆**：类拆成员、函数拆"它调用的东西"（按源码顺序 = 流程），
   拆不动就明说"这是原子步骤"，**不能留一个空箭头让人反复点**；
4. **环要挡住**：静态调用图里有递归（`walk → walk`），子节点标 `cyclic` 且不再展开；
5. **证据三态**（direct / file / none）——不把文件级说成函数级，缺测试照实说。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from code_agent.codeintel import features as features_mod
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.vcs import git_env


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**git_env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git failed: {proc.stdout}{proc.stderr}")
    return proc.stdout.strip()


class FakeProjects:
    def __init__(self, row: dict) -> None:
        self.row = row

    def get(self, pid: str) -> dict | None:
        return self.row if pid == self.row["id"] else None

    def find_by_root(self, root_key: str) -> dict | None:
        return self.row if self.row["root_key"] == root_key else None

    def set_scan(self, pid: str, summary_json: str) -> None:
        pass


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """小仓库：装配型路由、类+成员、调用链、递归、无 doc 的类、命令入口、测试证据。"""
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text('"""应用包：把接口和服务装起来。"""\n', encoding="utf-8")
    (root / "app" / "api.py").write_text(
        '"""HTTP 接口。"""\n\n'
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        "def mount_api() -> None:\n"
        '    @app.get("/api/v1/things/{thing_id}")\n'
        "    def get_thing(thing_id: str) -> dict:\n"
        '        """查一个东西。"""\n'
        '        return {"id": thing_id}\n\n'
        '    @app.post("/api/v1/things")\n'
        "    def create_thing(payload: dict) -> dict:\n"
        "        return payload\n",
        encoding="utf-8",
    )
    (root / "app" / "service.py").write_text(
        '"""问候服务。"""\n\n\n'
        "class Greeter:\n"
        '    """按名字打招呼。"""\n\n'
        "    def greet(self, name: str) -> str:\n"
        '        """组装问候语。"""\n'
        "        return self.prefix() + normalize(name)\n\n"
        "    def prefix(self) -> str:\n"
        '        """固定前缀。"""\n'
        '        return "hi "\n\n\n'
        "class Bare:\n"
        "    def only(self) -> int:\n"
        "        return 1\n\n\n"
        "def normalize(name: str) -> str:\n"
        '    """把名字里的空白折叠掉。"""\n'
        '    return " ".join(name.split())\n\n\n'
        "def leaf_only() -> int:\n"
        '    """什么都不调用。"""\n'
        "    return 1\n\n\n"
        "def walk(node: int) -> int:\n"
        '    """递归到底。"""\n'
        "    if node <= 0:\n"
        "        return 0\n"
        "    return walk(node - 1)\n",
        encoding="utf-8",
    )
    (root / "app" / "tools.py").write_text(
        '"""工具实现。"""\n\nfrom code_agent.tools.base import Tool\n\n\n'
        "class EchoTool(Tool):\n"
        '    """把输入原样返回。"""\n\n'
        '    name = "echo"\n\n'
        "    async def execute(self, arguments, ctx):\n"
        "        return arguments\n",
        encoding="utf-8",
    )
    # 子包：能力域要能嵌套（app → app/inner），而不是和父包在同一屏并列
    (root / "app" / "inner").mkdir()
    (root / "app" / "inner" / "__init__.py").write_text(
        '"""内层包：只用来验证能力域会嵌套。"""\n', encoding="utf-8"
    )
    (root / "app" / "inner" / "deep.py").write_text(
        '"""内层功能。"""\n\n\ndef deep_thing(value: int) -> int:\n'
        '    """内层的一件事。"""\n    return value\n',
        encoding="utf-8",
    )
    (root / "scripts").mkdir()
    (root / "scripts" / "run.py").write_text(
        '"""命令行入口。"""\n\n\n'
        "def main() -> int:\n"
        '    """跑起来。"""\n'
        "    return 0\n\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_api.py").write_text(
        '"""接口的测试。"""\n\n'
        "from app.api import get_thing\n\n\n"
        "def test_get_thing_returns_id():\n"
        "    assert get_thing('a')['id'] == 'a'\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def env(repo: Path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {
        "id": "p_nav", "name": "proj", "root_path": str(repo),
        "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin",
    }
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    return {"row": row, "intel": intel, "repo": repo}


def _features(nav: dict) -> list[dict]:
    """第一屏里所有功能（含子域里的）。"""
    out: list[dict] = []

    def walk(domain: dict) -> None:
        out.extend(domain.get("features") or [])
        for child in domain.get("children") or []:
            walk(child)

    for domain in nav["domains"]:
        walk(domain)
    return out


def _domain(nav: dict, name: str) -> dict:
    """按完整目录名找能力域（第一屏里任意一层）。"""
    found: list[dict] = []

    def walk(domain: dict) -> None:
        found.append(domain)
        for child in domain.get("children") or []:
            walk(child)

    for domain in nav["domains"]:
        walk(domain)
    for domain in found:
        if domain["name"] == name:
            return domain
    raise AssertionError(f"没有这个能力域：{name}，实际有 {[d['name'] for d in found]}")


def _find(nav: dict, *, name: str = "", label: str = "") -> dict:
    for node in _features(nav):
        if name and node["name"] == name:
            return node
        if label and node["label"] == label:
            return node
    raise AssertionError(f"第一屏没有这个功能：{name or label}")


def _kids(env, node_id: str, visited: tuple[str, ...] = ()) -> dict:
    return env["intel"].feature_children(env["row"], node_id=node_id, visited=visited)


# ---------------------------------------------------------------------------
# 小工具：归域与命名
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected",
    [
        ("code_agent/agent/breaker.py", ["code_agent", "code_agent/agent"]),
        (
            "code_agent/codeintel/parsers/js_parser.py",
            ["code_agent", "code_agent/codeintel", "code_agent/codeintel/parsers"],
        ),
        ("code_agent/settings.py", ["code_agent"]),
        ("scripts/run.py", ["scripts"]),
        ("setup.py", []),
    ],
)
def test_parent_chain_follows_the_package_tree(path: str, expected: list[str]) -> None:
    """能力域跟着包的嵌套走：整条目录链都要保留，不在"前两段"截断。"""
    assert features_mod.parent_chain(path) == expected


@pytest.mark.parametrize(
    "doc,expected",
    [
        ("失败指纹：工具名 + 归一化参数（键排序）。", "失败指纹"),
        ("查一个东西。", "查一个东西"),
        ("Agent loop（s01）：一个", "Agent loop（s01）"),
        ("见：太短的冒号前不切", "见：太短的冒号前不切"),
        ("", ""),
    ],
)
def test_doc_label_prefers_the_name_before_the_colon(doc: str, expected: str) -> None:
    assert features_mod.doc_label(doc) == expected


# ---------------------------------------------------------------------------
# 第一屏：能力域 → 功能
# ---------------------------------------------------------------------------
def test_domains_are_directories_and_tests_are_not_features(env) -> None:
    nav = env["intel"].features(env["row"])
    names = [d["name"] for d in nav["domains"]]
    # 顶层就是顶层目录：`app`（下面还有 app/inner）、`scripts`
    assert names == ["app", "scripts"], names
    assert all("tests" not in n for n in names)
    assert nav["totals"]["features"] == len(_features(nav))
    # 上限没触发时：列出来的就是全部
    assert nav["totals"]["found"] == nav["totals"]["features"]


def test_domains_nest_with_the_package_tree(env) -> None:
    """能力域跟着包嵌套：`app/inner` 是 `app` 的子域，不在同一屏并列。"""
    nav = env["intel"].features(env["row"])
    assert [d["name"] for d in nav["domains"]] == ["app", "scripts"]
    app = _domain(nav, "app")
    inner = _domain(nav, "app/inner")
    # 子域挂在父域下面，而不是单独一项
    assert inner["name"] in [c["name"] for c in app["children"]]
    assert inner["label"] == "inner"
    # 父域的"共"包含子域，"直属"只数它自己目录下的功能
    assert app["count"] > app["own"]
    assert app["count"] == app["own"] + sum(c["count"] for c in app["children"])
    assert any(f["name"] == "deep_thing" for f in inner["features"])
    # 子域自己的说明来自 app/inner/__init__.py
    assert inner["doc"] == "内层包：只用来验证能力域会嵌套"
    assert inner["doc_path"] == "app/inner/__init__.py"
    # 子域里的功能只出现一次（不会既挂在父域又挂在子域）
    ids = [f["id"] for f in _features(nav)]
    assert len(ids) == len(set(ids))


def test_domain_doc_says_where_it_came_from(env) -> None:
    nav = env["intel"].features(env["row"])
    app = _domain(nav, "app")
    # 说明取自 app/__init__.py（包的自我描述），并且把来源一起给出来，不假装是官方描述
    assert app["doc"] == "应用包：把接口和服务装起来"
    assert app["doc_path"] == "app/__init__.py"


def test_class_is_a_feature_even_without_doc(env) -> None:
    nav = env["intel"].features(env["row"])
    greeter = _find(nav, name="Greeter")
    assert greeter["label"] == "按名字打招呼" and greeter["has_doc"] is True
    bare = _find(nav, name="Bare")
    # 没 doc 也要在（一个类就是一块能力），但要如实标「无说明」
    assert bare["label"] == "Bare" and bare["has_doc"] is False
    assert bare["child_count"] == 1


def test_assembly_container_is_transparent(env) -> None:
    """`mount_api` 只是把路由挂起来，路由要直接出现在该域的第一屏。"""
    nav = env["intel"].features(env["row"])
    assert all(node["name"] != "mount_api" for node in _features(nav))
    route = _find(nav, name="/api/v1/things/{thing_id}")
    assert route["source"] == "route" and route["label"] == "查一个东西"
    assert route["kind"] == "route"


def test_command_entrypoint_is_a_feature(env) -> None:
    nav = env["intel"].features(env["row"])
    commands = _domain(nav, "scripts")
    run = [f for f in commands["features"] if f["path"] == "scripts/run.py"]
    assert len(run) == 1 and run[0]["id"].startswith("sym:scripts/run.py#main")
    assert run[0]["source"] == "command" and run[0]["label"] == "跑起来"


def test_evidence_is_three_state_and_not_inflated(env) -> None:
    nav = env["intel"].features(env["row"])
    get_route = _find(nav, name="/api/v1/things/{thing_id}")
    assert get_route["test_state"] == "direct" and get_route["tests"] == ["tests/test_api.py"]
    create_route = _find(nav, name="/api/v1/things")
    # 没被测试提到 → 只能给文件级（测试 import 了 app.api），**不能**说它被测试盯着
    assert create_route["test_state"] == "file" and create_route["test_note"]
    assert nav["totals"]["direct_tests"] >= 1
    assert (
        nav["totals"]["direct_tests"] + nav["totals"]["file_tests"] + nav["totals"]["no_tests"]
        == nav["totals"]["features"]
    )


def test_limit_is_reported_not_hidden(env) -> None:
    nav = env["intel"].features(env["row"], limit=1)
    assert all(d["shown"] == 1 for d in nav["domains"])
    assert any(d["count"] > d["shown"] for d in nav["domains"])
    assert nav["truncated"] is True
    assert nav["method"] and nav["note"]


# ---------------------------------------------------------------------------
# 往下拆：细分功能 → …（这才是"功能导航"）
# ---------------------------------------------------------------------------
def test_class_expands_to_members(env) -> None:
    nav = env["intel"].features(env["row"])
    greeter = _find(nav, name="Greeter")
    payload = _kids(env, greeter["id"])
    labels = [c["label"] for c in payload["children"]]
    assert labels == ["组装问候语", "固定前缀"], payload["children"]
    assert payload["leaf"] is False
    assert all(c["source"] == "member" for c in payload["children"])


def test_function_expands_to_what_it_calls_in_source_order(env) -> None:
    nav = env["intel"].features(env["row"])
    greet = _kids(env, _find(nav, name="Greeter")["id"])["children"][0]
    payload = _kids(env, greet["id"], visited=(_find(nav, name="Greeter")["id"],))
    # 源码顺序：先 prefix() 再 normalize()
    assert [c["name"] for c in payload["children"]] == ["prefix", "normalize"]
    assert payload["leaf"] is False
    # 有下一层时不塞 summary（那是叶子才需要解释的东西），省一次解析
    assert "summary" not in payload


def test_leaf_says_it_is_atomic(env) -> None:
    nav = env["intel"].features(env["row"])
    leaf = _find(nav, name="leaf_only")
    payload = _kids(env, leaf["id"])
    assert payload["children"] == [] and payload["leaf"] is True
    # 拆不动时必须说明"为什么到此为止"，不能留一个空箭头
    assert "原子步骤" in payload["note"]
    # 而且**第一屏就该知道**它是原子：不需要点一下才发现
    assert leaf["has_children"] is False and leaf["children_known"] is True
    assert leaf["child_count"] == 0


def test_caret_promise_is_exact(env) -> None:
    """界面上的 `▸` 是一句承诺：**有下一层才给箭头**。

    旧版对所有函数都乐观地画箭头，用户点开才发现是空的——"是不是原子"必须先点一下
    才知道。所以这条测试盯的不是"有没有箭头"，而是**箭头和点开结果一致**：
    凡是 `has_children`，点开必须真有孩子；凡是 `children_known` 且没有，点开必须为空。
    """
    nav = env["intel"].features(env["row"])
    feats = _features(nav)
    assert feats
    checked = 0
    for node in feats:
        kids = _kids(env, node["id"])
        assert node["has_children"] == bool(kids["children"]), (
            f"{node['label']} 的箭头与事实不符：has_children={node['has_children']} "
            f"但点开拿到 {len(kids['children'])} 个"
        )
        if node["children_known"]:
            assert node["child_count"] == len(kids["children"]), node["label"]
        checked += 1
    assert checked >= 10

    # 有下一层的例子：调用链的顺序就是源码顺序
    greet = _kids(env, _find(nav, name="Greeter")["id"])["children"][0]
    assert greet["has_children"] is True and greet["child_count"] == 2


def test_children_are_also_precomputed(env) -> None:
    """点开一层时顺手把**儿子们**的下一层也算准：否则第二层又退回"点了才知道"。"""
    nav = env["intel"].features(env["row"])
    greeter = _find(nav, name="Greeter")
    members = _kids(env, greeter["id"])["children"]
    by_name = {m["name"]: m for m in members}
    # `greet` 调用了两个函数 → 有箭头、数量准确
    assert by_name["greet"]["has_children"] is True
    assert by_name["greet"]["child_count"] == 2
    # `prefix` 只返回字符串字面量 → 没有下一层，第一眼就是原子
    assert by_name["prefix"]["has_children"] is False
    assert by_name["prefix"]["children_known"] is True
    assert by_name["prefix"]["child_count"] == 0


def test_recursion_is_marked_cyclic(env) -> None:
    nav = env["intel"].features(env["row"])
    walk = _find(nav, name="walk")
    first = _kids(env, walk["id"])
    assert [c["name"] for c in first["children"]] == ["walk"]
    # 只有"当前路径上已经出现过"才算环：第一层还不算，再往下一层就该挡住了
    assert first["children"][0].get("cyclic") is not True
    deeper = _kids(env, first["children"][0]["id"], visited=(walk["id"], first["children"][0]["id"]))
    assert deeper["children"][0]["cyclic"] is True
    assert deeper["children"][0]["has_children"] is False


def test_unknown_node_is_an_error_not_a_fake_tree(env) -> None:
    with pytest.raises(ValueError):
        _kids(env, "sym:app/nope.py#ghost")


def test_empty_repo_is_empty_not_fake(tmp_path: Path, agent_home, monkeypatch) -> None:
    root = tmp_path / "empty"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text('"""没有功能。"""\n', encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {
        "id": "p_empty", "name": "empty", "root_path": str(root),
        "root_key": str(root).replace("\\", "/").lower(), "user_id": "u_admin",
    }
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    nav = intel.features(row)
    assert nav["domains"] == [] and nav["totals"]["features"] == 0
    assert nav["truncated"] is False
