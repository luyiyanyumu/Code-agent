"""代码情报 P1：解析器 / 扫描器 / 符号索引 / 增量 / delta / 保真度。

这一层的测试重点不在"能不能解析"，而在**三件容易悄悄坏掉的事**：
1. 行号口径（地图跳转全靠它，错一行用户就找不到代码）；
2. 增量语义（未改的文件必须复用，改动的文件必须精确报变更，行号平移**不算**变更）；
3. 保真度校验必须能真的发现漂移（一个永远返回 1.0 的校验器等于没有）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from code_agent.codeintel import graph
from code_agent.codeintel.parsers import parse_file
from code_agent.codeintel.scanner import walk_project
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.store import SymbolIndex, file_id, symbol_id

# ---------------------------------------------------------------------------
# 夹具：一个语义明确的小仓库（Python + JS + Vue + 噪声文件）
# ---------------------------------------------------------------------------
PY_SERVICE = '''"""示例服务模块。

这个 docstring 应当被用作模块用途。
"""

import json
from pathlib import Path

from .helper import helper_fn

MAX_ITEMS = 10
cache: dict = {}


class Base:
    """基类。"""

    def ping(self) -> str:
        return "pong"


class Service(Base):
    """主服务：处理核心逻辑。"""

    def __init__(self, name: str, limit: int = 5):
        self.name = name
        self.limit = limit

    @property
    def label(self) -> str:
        return f"{self.name}:{self.limit}"

    def run(self, items: list[int]) -> int:
        total = 0
        for item in items:
            if item > self.limit:
                continue
            total += item
        return total

    async def load(self, path: Path) -> dict:
        return json.loads(path.read_text())


def helper_top(a, b=2, *args, **kwargs) -> int:
    return a + b


def _private(x):
    def inner():
        return x
    return inner()


if __name__ == "__main__":
    print(helper_top(1))
'''

PY_HELPER = '''"""辅助模块。"""


def helper_fn(x: int) -> int:
    """把数字翻倍。"""
    return x * 2
'''

PY_ROUTES = '''from fastapi import FastAPI

app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/v1/items")
def create_item(body: dict):
    return body
'''

JS_API = """import { getToken } from './auth.js'

export const TIMEOUT = 30

export function fetchJson(url, opts = {}) {
  return fetch(url, opts).then((r) => r.json())
}

export const request = async (method, path) => {
  const merged = { method }
  return fetchJson(path, merged)
}

export class ApiClient {
  constructor(base) {
    this.base = base
  }

  get(path) {
    return request('GET', `${this.base}${path}`)
  }
}

export default ApiClient
"""

VUE_VIEW = """<template>
  <div>{{ title }}</div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  title: { type: String, required: true },
  count: Number,
})

const emit = defineEmits(['save', 'cancel'])

const doubled = computed(() => props.count * 2)

function handleSave() {
  emit('save', doubled.value)
}
</script>
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text('"""包用途。"""\n', encoding="utf-8")
    (root / "pkg" / "service.py").write_text(PY_SERVICE, encoding="utf-8")
    (root / "pkg" / "helper.py").write_text(PY_HELPER, encoding="utf-8")
    (root / "pkg" / "routes.py").write_text(PY_ROUTES, encoding="utf-8")
    (root / "web" / "src").mkdir(parents=True)
    (root / "web" / "src" / "api.js").write_text(JS_API, encoding="utf-8")
    (root / "web" / "src" / "View.vue").write_text(VUE_VIEW, encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname="demo"\nversion="0.1.0"\n[tool.pytest.ini_options]\ntestpaths=["tests"]\n',
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_service.py").write_text(
        "from pkg.service import helper_top\n\n\ndef test_top():\n    assert helper_top(1) == 3\n", encoding="utf-8"
    )
    # 噪声：依赖目录、密钥、二进制、超大文件 —— 都应被正确处置
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("export const x = 1\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
    (root / "huge.py").write_text("x = 1\n" * 200000, encoding="utf-8")
    return root


@pytest.fixture
def project_row(repo: Path) -> dict:
    return {"id": "p_test", "name": "repo", "root_path": str(repo), "root_key": str(repo).lower()}


class FakeProjects:
    def __init__(self) -> None:
        self.summaries: dict[str, dict] = {}

    def set_scan(self, project_id: str, summary_json: str) -> None:
        self.summaries[project_id] = json.loads(summary_json)


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------
def test_python_parser_symbols_and_lines():
    result = parse_file("pkg/service.py", "python", PY_SERVICE)
    assert result["error"] == ""
    assert result["entrypoint"] is True

    by_qname = {s["qname"]: s for s in result["symbols"]}
    cls = by_qname["Service"]
    assert cls["kind"] == "class"
    assert cls["bases"] == ["Base"]
    assert cls["doc"].startswith("主服务")
    assert cls["line"] < cls["end_line"]

    # 方法：限定名带类名前缀，kind=method，返回注解被解析出来
    run = by_qname["Service.run"]
    assert run["kind"] == "method" and run["parent"] == cls["id"]
    assert run["params"] == ["self", "items"]
    assert run["returns"] == "int"
    assert run["complexity"] >= 2  # for + if

    assert by_qname["Service.label"]["kind"] == "property"
    assert by_qname["Service.load"]["params"] == ["self", "path"]
    assert by_qname["Service.load"]["returns"] == "dict"

    # 模块级常量 vs 变量
    assert by_qname["MAX_ITEMS"]["kind"] == "constant"
    assert by_qname["cache"]["kind"] == "variable"
    assert by_qname["Base.ping"]["kind"] == "method"

    # 顶层函数带默认值/可变参数；嵌套函数是 parent=外层函数的 function
    top = by_qname["helper_top"]
    assert top["params"] == ["a", "b", "args", "kwargs"]
    inner = by_qname["_private.inner"]
    assert inner["kind"] == "function" and inner["parent"] == by_qname["_private"]["id"]

    # 行号必须精确（地图跳转依赖它）
    source_lines = PY_SERVICE.split("\n")
    assert source_lines[cls["line"] - 1].startswith("class Service")
    assert source_lines[run["line"] - 1].strip().startswith("def run")
    assert source_lines[run["end_line"] - 1].strip().startswith("return total")

    # 导入（原始语句，供 graph 解析成文件边）
    modules = {i["module"] for i in result["imports"]}
    assert {"json", "pathlib", "helper"} <= modules
    relative = [i for i in result["imports"] if i["module"] == "helper"]
    assert relative[0]["level"] == 1


def test_python_parser_routes():
    result = parse_file("pkg/routes.py", "python", PY_ROUTES)
    routes = [s for s in result["symbols"] if s["kind"] == "route"]
    assert {(r["route_method"], r["route_path"]) for r in routes} == {
        ("GET", "/healthz"),
        ("POST", "/api/v1/items"),
    }
    # 路由的 qname 是处理函数名（路径只作展示名），便于命名与引用
    assert {r["qname"] for r in routes} == {"healthz", "create_item"}
    assert all(r["name"].startswith("/") for r in routes)


def test_python_parser_reports_syntax_error_without_raising():
    result = parse_file("bad.py", "python", "def broken(:\n")
    assert result["error"]
    assert result["symbols"] == []


def test_js_parser_symbols_and_lines():
    result = parse_file("web/src/api.js", "javascript", JS_API)
    by_name = {s["name"]: s for s in result["symbols"] if not s["parent"]}
    assert by_name["TIMEOUT"]["kind"] == "constant"
    assert by_name["fetchJson"]["kind"] == "function"
    assert by_name["request"]["kind"] == "function"
    assert by_name["ApiClient"]["kind"] == "class"
    assert "constructor" in {s["name"] for s in result["symbols"] if s["parent"]}
    assert by_name["fetchJson"]["confidence"] == "high"
    # 导入边必须能拿到（字符串清理不能把 from '...' 一起清掉）
    assert [i["module"] for i in result["imports"]] == ["./auth.js"]

    lines = JS_API.split("\n")
    cls = by_name["ApiClient"]
    assert lines[cls["line"] - 1].startswith("export class ApiClient")
    assert lines[cls["end_line"] - 1] == "}"
    # 函数体内声明的变量不能跑到顶层（多行模板字符串曾把深度算成负数）
    assert "merged" not in by_name and "base" not in by_name


def test_vue_parser_component_props_emits():
    result = parse_file("web/src/View.vue", "vue", VUE_VIEW)
    component = next(s for s in result["symbols"] if s["kind"] == "component")
    assert component["name"] == "View"
    props = {p["name"]: p for p in component["props"]}
    assert props["title"]["required"] is True
    assert props["count"]["required"] is False
    assert component["emits"] == ["save", "cancel"]
    member_kinds = {s["kind"] for s in result["symbols"] if s["parent"] == component["id"]}
    assert {"hook", "function"} <= member_kinds
    assert [i["module"] for i in result["imports"]] == ["vue"]


def test_generic_parser_is_low_confidence():
    src = "package main\n\nfunc main() {\n}\n\ntype Server struct {\n}\n"
    result = parse_file("main.go", "go", src)
    assert result["confidence"] == "low"
    assert {s["name"] for s in result["symbols"]} == {"main", "Server"}
    assert all(s["confidence"] == "low" for s in result["symbols"])


def test_unknown_language_returns_file_only():
    result = parse_file("data.json", "json", '{"a": 1}')
    assert result["symbols"] == []


# ---------------------------------------------------------------------------
# 扫描器
# ---------------------------------------------------------------------------
def test_scanner_ignore_rules_and_skips(repo: Path):
    walk = walk_project(repo)
    rels = {entry.rel for entry in walk.files}
    assert "pkg/service.py" in rels
    assert not any("node_modules" in rel for rel in rels)

    skipped = {entry.rel: entry.skipped for entry in walk.files if entry.skipped}
    assert skipped[".env"] == "secret"
    assert skipped["logo.png"] == "binary"
    assert skipped["huge.py"] == "too_large"
    # 未跳过的条目里不应有隐藏文件/目录（.env 只以"跳过"形态出现）
    assert not any(rel.startswith(".") for rel in rels - set(skipped))
    summary = walk.summary()
    assert summary["files"] >= 8
    assert summary["skipped"]["secret"] == 1
    assert all(bucket["name"] != "node_modules" for bucket in summary["languages"])


# ---------------------------------------------------------------------------
# 索引 / 地图 / 增量
# ---------------------------------------------------------------------------
def test_scan_builds_index_and_map(project_row, repo, tmp_path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    service = CodeIntelService(FakeProjects())
    result = service.scan(project_row, full=True) if False else __import__("asyncio").run(
        service.scan(project_row)
    )
    assert result["status"] == "done"
    assert result["symbols"] > 15
    assert result["files"] >= 8

    # .env / 二进制 / 超大文件：留文件节点但不解析（skipped 明确标注）
    index = service.index("p_test")
    assert index.file_row(".env")["skipped"] == "secret"
    assert index.symbols_for(".env") == []

    payload = service.map_payload(project_row, depth=3)
    assert payload["schema_version"] == 1
    assert payload["project"]["name"] == "repo"
    # 结构树：目录 → 文件 → 符号
    dirs = {c["name"]: c for c in payload["structure"]["children"] if c["kind"] == "dir"}
    assert "pkg" in dirs
    pkg_files = [c for c in dirs["pkg"]["children"] if c["kind"] == "file"]
    service_file = next(c for c in pkg_files if c["name"] == "service.py")
    symbol_names = {s["name"] for s in service_file["symbols"]}
    assert {"Service", "Base", "helper_top"} <= symbol_names
    # 模块聚合与用途（来自 __init__.py 的 docstring）
    modules = {m["path"]: m for m in payload["modules"]}
    assert modules["pkg"]["purpose"] == "包用途。"
    assert modules["pkg"]["files"] >= 4
    # 入口 / 路由 / 命令
    assert any(e["path"].endswith("routes.py") for e in payload["entrypoints"]) is False
    assert {r["path"] for r in payload["routes"]} == {"/healthz", "/api/v1/items"}
    assert payload["commands"]["test"] == ["python -m pytest"]
    # 依赖边：routes.py → fastapi 是外部包，不进图，但 externals 计数保留
    assert payload["edges"]["imports"], payload["edges"]
    assert isinstance(payload["stats"]["kinds"]["class"], int)


def test_map_depth_controls_semantics(project_row, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    service = CodeIntelService(FakeProjects())
    __import__("asyncio").run(service.scan(project_row))
    shallow = service.map_payload(project_row, depth=1)
    deep = service.map_payload(project_row, depth=3)

    def collect_symbols(node: dict, out: list) -> None:
        for child in node.get("children", []):
            if child["kind"] == "file":
                out.extend(child.get("symbols", []))
            else:
                collect_symbols(child, out)

    shallow_symbols: list = []
    deep_symbols: list = []
    collect_symbols(shallow["structure"], shallow_symbols)
    collect_symbols(deep["structure"], deep_symbols)
    assert shallow_symbols == []          # depth=1 只到文件
    assert deep_symbols, "depth=3 必须能看到文件下的符号"
    assert any(s["parent"] for s in deep_symbols), "depth=3 必须包含成员（如方法）"

    only = service.map_payload(project_row, kinds=["class"])
    names: list[str] = []
    collect_symbols(only["structure"], names)
    assert {s["kind"] for s in names} == {"class"}


def test_incremental_scan_reuses_unchanged_and_reports_precise_changes(project_row, repo, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    service = CodeIntelService(FakeProjects())
    import asyncio

    first = asyncio.run(service.scan(project_row))
    assert first["scan"]["parsed"] >= 8
    assert first["scan"]["reused"] == 0
    seq_after_first = service.index("p_test").max_change_seq()

    # 第二次扫描（无改动）：全部复用，零变更
    second = asyncio.run(service.scan(project_row))
    assert second["scan"]["reused"] >= 8
    assert second["scan"]["parsed"] == 0
    assert service.index("p_test").max_change_seq() == seq_after_first

    # 加一个方法：只该出现"该文件 + 该符号"的增量
    src = (repo / "pkg" / "service.py").read_text(encoding="utf-8")
    (repo / "pkg" / "service.py").write_text(
        src.replace("    def run(", "    def audit(self) -> None:\n        pass\n\n    def run("),
        encoding="utf-8",
    )
    third = asyncio.run(service.scan(project_row))
    assert third["scan"]["parsed"] == 1

    delta = service.delta(project_row, seq_after_first)
    assert delta["truncated"] is False
    added = {n["name"] for n in delta["added"]}
    assert "audit" in added
    # 行号平移不算变更：run/Service 只是往后挪了几行，不该出现在 updated/added 里
    assert "run" not in added
    assert delta["files"] == ["pkg/service.py"]
    assert delta["to_seq"] > seq_after_first

    # 删除文件：文件与它的符号都要报 removed，且不留指向它的悬空边
    (repo / "pkg" / "helper.py").unlink()
    asyncio.run(service.scan(project_row))
    index = service.index("p_test")
    assert index.file_row("pkg/helper.py") is None
    assert index.edges(dst=file_id("pkg/helper.py")) == []
    assert not any(e["dst"].startswith("sym:pkg/helper.py#") for e in index.edges())


def test_delta_from_zero_returns_full_history_capped(project_row, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    service = CodeIntelService(FakeProjects())
    import asyncio

    asyncio.run(service.scan(project_row))
    delta = service.delta(project_row, 0)
    assert delta["added"], "首次全量扫描应当产生可消费的增量（前端首屏用它建树）"
    assert all(n["id"].startswith("sym:") for n in delta["added"][:5])
    assert delta["edges_dirty"] is True


def test_symbol_detail_includes_members_and_importers(project_row, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    service = CodeIntelService(FakeProjects())
    import asyncio

    asyncio.run(service.scan(project_row))
    sid = symbol_id("pkg/service.py", "Service")
    detail = service.symbol_detail(project_row, sid)
    assert detail["kind"] == "class"
    assert {m["name"] for m in detail["members"]} >= {"run", "load", "label"}
    assert detail["doc"].startswith("主服务")
    assert detail["bases"] == ["Base"]
    source = service.text_slice(project_row, "pkg/service.py", detail["line"], detail["line"] + 1)
    assert source.startswith("class Service")
    with pytest.raises(KeyError):
        service.symbol_detail(project_row, "sym:pkg/service.py#Nope")


def test_verify_detects_drift_and_is_clean_after_scan(project_row, repo, agent_home, monkeypatch):
    """保真度校验必须真的能发现漂移，否则它就是自证清白。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    service = CodeIntelService(FakeProjects())
    import asyncio

    asyncio.run(service.scan(project_row))
    clean = service.verify(project_row)
    assert clean["ok"] is True and clean["fidelity"] == 1.0

    # 外部改动（模拟别人 IDE / git checkout）：校验要发现，且**不能**顺手修好
    (repo / "pkg" / "helper.py").write_text(
        "def helper_fn(x):\n    return x * 3\n\n\ndef extra():\n    return 1\n", encoding="utf-8"
    )
    drifted = service.verify(project_row)
    assert drifted["ok"] is False
    assert any("helper.py" in d["path"] for d in drifted["drift"])
    assert drifted["fidelity"] < 1.0
    # 仍然没被写回：再验一次还是同样结果（证明 verify 是只读的）
    again = service.verify(project_row)
    assert again["ok"] is False

    # 扫描之后恢复干净
    asyncio.run(service.scan(project_row))
    assert service.verify(project_row)["ok"] is True


def test_ensure_indexed_scans_once_then_reuses(project_row, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    service = CodeIntelService(FakeProjects())
    import asyncio

    first = asyncio.run(service.ensure_indexed(project_row))
    assert first["indexed"] is True
    assert (first["scan"] or {}).get("parsed", 0) >= 8
    seq_first = first["change_seq"]

    # 第二次：新鲜度校验命中，不应再产生任何索引变更（也不应重解析）
    second = asyncio.run(service.ensure_indexed(project_row))
    assert second["change_seq"] == seq_first
    assert second["last_scan_at"] == first["last_scan_at"]
    # 摘要保持首次扫描的结果（没有被第二轮覆盖）
    assert second["scan"]["parsed"] == first["scan"]["parsed"]


def test_digest_budget_and_content(project_row, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    service = CodeIntelService(FakeProjects())
    import asyncio

    asyncio.run(service.scan(project_row))
    text = service.digest(project_row, max_chars=1200)
    assert len(text) <= 1200
    assert "项目地图" in text and "模块：" in text
    assert "pkg" in text and "class Service" in text
    assert "对外接口" in text
    small = service.digest(project_row, max_chars=300)
    assert len(small) <= 300
    assert "截断" in small  # 超预算时明确说截断了，而不是给半句话


def test_index_is_isolated_per_project_root(project_row, tmp_path, agent_home, monkeypatch):
    """索引落盘在 ~/.code-agent/code/<pid>/，绝不写进用户仓库。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    service = CodeIntelService(FakeProjects())
    import asyncio

    asyncio.run(service.scan(project_row))
    index_path = service.index_path("p_test")
    assert index_path.parent == agent_home / "code" / "p_test"
    assert index_path.is_file()
    repo_files = list(Path(project_row["root_path"]).rglob("*"))
    assert not any(p.name.startswith("index-") for p in repo_files)
    service.close("p_test")


def test_index_handles_reset_and_versions(project_row, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    index = SymbolIndex(agent_home / "code" / "x" / "index-main.db")
    assert index.version() == 0
    index.apply_parse(
        rel="a.py", lang="python", loc=3, size=10, mtime=1.0, sha1="deadbeef",
        parsed={"symbols": [{"id": "sym:a.py#f", "kind": "function", "name": "f", "qname": "f",
                             "line": 1, "end_line": 2, "signature": "def f()"}],
                "entrypoint": False, "confidence": "high", "error": "", "strategy": "ast"},
        edges=[(file_id("a.py"), file_id("b.py"), "imports", 1)],
    )
    assert index.version() == 1
    assert index.file_row("a.py")["sha1"] == "deadbeef"
    assert index.counts() == {"files": 1, "symbols": 1, "edges": 1}
    index.reset()
    assert index.counts()["symbols"] == 0
    assert index.version() > 1
    index.close()


def test_import_edge_resolution_covers_python_and_js(repo: Path):
    known = {
        "pkg/service.py", "pkg/helper.py", "pkg/__init__.py",
        "web/src/api.js", "web/src/View.vue",
    }
    assert graph.resolve_import("pkg/service.py", "helper", 1, known) == "pkg/helper.py"
    assert graph.resolve_import("pkg/service.py", "pkg.helper", 0, known) == "pkg/helper.py"
    assert graph.resolve_import("pkg/__init__.py", "helper", 1, known) == "pkg/helper.py"
    assert graph.resolve_import("web/src/api.js", "./auth.js", 0, known) is None  # 外部/不存在
    assert graph.resolve_import("web/src/api.js", "./View.vue", 0, known) == "web/src/View.vue"
    assert graph.resolve_import("pkg/service.py", "json", 0, known) is None  # 标准库不入图

    edges, externals = graph.import_edges(
        "pkg/service.py",
        [{"module": "helper", "level": 1}, {"module": "json", "level": 0}, {"module": "helper", "level": 1}],
        known,
    )
    assert edges == [(file_id("pkg/service.py"), file_id("pkg/helper.py"), "imports", 2)]
    assert externals == 1
