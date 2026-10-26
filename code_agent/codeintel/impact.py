"""静态可达范围与风险分级（M18 · P6）。

纪律参考自 archify（tt-a1i/archify，MIT）的设计文档，两条最要紧的：

1. **Truth before spectacle**：每个数字都要能指出它的证据来源；
2. **不要把图上的可达性说成运行时影响**：archify 明确写着
   "Don't call graph reachability runtime impact, blast radius, or breakage
   without independent code-analysis evidence"。我们没有运行时/依赖注入/反射
   的证据，所以对外一律说**静态可达（按 import / 引用边）**，并在载荷里
   显式带上 `method` / `basis` / `not_runtime_impact`，界面照抄这句话。

用户要的是"改这里会惊动谁、值不值得现在改"，所以仍然算两样东西：

1. **静态可达（reach）**：从被改的符号/文件出发沿**反向**边（谁 import / 谁引用）
   做传递闭包，得到会被波及的文件与模块——可枚举、可复核；
2. **风险分级（risk）**：把若干**可判定**的信号加权成 0-100 分与等级，
   **每个信号带一句人话理由**，理由里同时点明它的依据（静态/规则/约定）。

刻意不做的事：
- 不预测运行时影响、崩溃面或"破坏性"（没有证据就不说）；
- 不把风险当合并门禁（硬门禁只由架构规则给出，见 architecture.py）。

分级标准（分数可调，理由链条不能省）：

| 信号 | 含义 | 依据 |
|---|---|---|
| 扇入 / 波及文件数 / 跨模块数 | 静态可达范围 | 索引里的 import 与引用边 |
| 公共面 | 路由 / 导出符号 / 非下划线模块级符号 | 解析结果（确定） |
| 保护路径 | 架构规则圈了"改这里要小心" | 规则文件（人写的） |
| 无关联测试 | 找不到任何关联测试 | 命名约定 + import 事实 |
| 低置信解析 | 启发式解析（JS/Vue）下的符号 | 解析器自报置信度 |

风险等级：`<25` low · `<50` medium · `<75` high · `>=75` critical。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from code_agent.codeintel import graph

#: 反向依赖传递闭包的深度上限（超过就不再往下追，避免一次点击变成全图遍历）
DEFAULT_DEPTH = 3
#: 影响面最多列多少个文件（超出的记在 dropped 里，如实上报而不是静默截断）
MAX_FILES = 200
MAX_SYMBOLS = 400

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


# ---------------------------------------------------------------------------
# 关联测试
# ---------------------------------------------------------------------------
_TEST_DIR_HINTS = ("test", "tests", "spec", "__tests__", "e2e")
_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test|spec_[^/]+|[^/]+\.(test|spec))\.\w+$")


def is_test_path(rel: str) -> bool:
    """这个路径看起来是测试吗（按目录名 + 文件名约定，两种都常见）。"""
    rel = (rel or "").replace("\\", "/")
    if not rel:
        return False
    if _TEST_FILE_RE.search(rel):
        return True
    parts = rel.split("/")[:-1]
    return any(part.lower() in _TEST_DIR_HINTS for part in parts)


def related_tests_many(index: Any, files: list[str]) -> dict[str, list[str]]:
    """一次算一批文件的关联测试。

    为什么要有批量版：`related_tests` 每个文件都要遍历一遍全部文件与边，
    720 个文件的项目上就是 720×720 次字符串比较（真机实测覆盖率要 9.7s）。
    这里改成"先把测试文件、import 边、符号归属各扫一遍"，再逐文件查表。
    """
    known = index.file_rows()
    targets = [str(f).replace("\\", "/").strip("/") for f in files if str(f).strip()]

    test_by_stem: dict[str, list[str]] = {}
    for rel in known:
        if not is_test_path(rel):
            continue
        stem = Path(rel).stem
        stripped = re.sub(r"(_test|\.test|\.spec)$", "", re.sub(r"^(test_|spec_)", "", stem))
        test_by_stem.setdefault(stripped, []).append(rel)

    imported_by_tests: dict[str, set[str]] = {}      # 文件 → 引用它的测试文件
    imports_of: dict[str, set[str]] = {}             # 文件 → 它引用的测试文件
    for edge in index.edges(kind="imports"):
        src = str(edge["src"]).replace("file:", "")
        dst = str(edge["dst"]).replace("file:", "")
        if src and dst:
            imports_of.setdefault(src, set()).add(dst)
            imported_by_tests.setdefault(dst, set()).add(src)

    tests_of_symbol: dict[str, set[str]] = {}        # 符号 id → 引用它的测试文件
    for edge in index.edges(kind="referenced_by"):
        src = str(edge["src"]).replace("file:", "")
        if src and is_test_path(src):
            tests_of_symbol.setdefault(str(edge["dst"]), set()).add(src)

    symbols_by_path: dict[str, list[str]] = {}
    for row in index.all_symbols():
        symbols_by_path.setdefault(row["path"], []).append(row["id"])

    out: dict[str, list[str]] = {}
    for rel in targets:
        hits: set[str] = set(test_by_stem.get(Path(rel).stem, []))
        hits |= imported_by_tests.get(rel, set())
        hits |= {t for t in imports_of.get(rel, set()) if is_test_path(t)}
        for sid in symbols_by_path.get(rel, []):
            hits |= tests_of_symbol.get(sid, set())
        out[rel] = sorted(hits)[:60]
    return out


def related_tests(index: Any, files: list[str]) -> list[str]:
    """与这些文件相关的测试：命名约定命中 + 真的引用过它们。

    两种都要：`tests/test_x.py` 覆盖 `pkg/x.py` 是约定；而"某个测试 import 了它"
    是事实。只有约定会漏掉命名不规范的仓库，只有事实会漏掉还没写 import 的用例。
    """
    targets = [str(f) for f in files if f]
    if not targets:
        return []
    merged: set[str] = set()
    for hits in related_tests_many(index, targets).values():
        merged |= set(hits)
    return sorted(merged)[:60]


# ---------------------------------------------------------------------------
# 影响面
# ---------------------------------------------------------------------------
def _seed(index: Any, *, symbol_ids: list[str], files: list[str]) -> tuple[set[str], set[str]]:
    """起点符号与起点文件。给了文件就把它的顶层符号也算进起点（改文件=改里面的东西）。"""
    symbols: set[str] = set()
    seeds: set[str] = set()
    for sid in symbol_ids or []:
        if str(sid).strip():
            symbols.add(str(sid))
            seeds.add(str(sid))
    for rel in files or []:
        rel = str(rel).replace("\\", "/").strip("/")
        if not rel:
            continue
        seeds.add(f"file:{rel}")
        for row in index.query_all("SELECT id FROM symbols WHERE path=?", (rel,)):
            symbols.add(row["id"])
            seeds.add(row["id"])
    return symbols, seeds


def reach(
    index: Any,
    *,
    symbol_ids: list[str] | None = None,
    files: list[str] | None = None,
    depth: int = DEFAULT_DEPTH,
) -> dict:
    """静态可达范围：沿**反向**边（谁 import / 谁引用）做传递闭包。

    返回层级结构（level 1 = 直接引用者）："直接改它的三个地方"和"第三层传下去的
    三十个地方"对决策的含义完全不同。

    载荷里带 `basis` 一句话——界面直接显示它，避免读者把静态可达读成运行时影响。
    """
    importers: dict[str, list[str]] = {}          # 直接引用关系（按层级看"谁引了谁"）
    start_symbols, seeds = _seed(index, symbol_ids=symbol_ids or [], files=files or [])
    starts = set(seeds)
    for sid in start_symbols:
        row = index.symbol(sid)
        if row:
            starts.add(f"file:{row['path']}")

    frontier = set(starts)
    seen = set(starts)
    levels: list[list[str]] = []
    truncated = False
    for _ in range(max(1, min(int(depth or DEFAULT_DEPTH), 6))):
        nxt: set[str] = set()
        for node in frontier:
            neighbours: set[str] = set()
            if node.startswith("file:"):
                rel = node[5:]
                for edge in index.edges(kind="imports", dst=node):
                    neighbours.add(str(edge["src"]))
                for sid in index.query_all("SELECT id FROM symbols WHERE path=?", (rel,)):
                    for edge in index.edges(kind="referenced_by", dst=sid["id"]):
                        neighbours.add(str(edge["src"]))
            else:
                for edge in index.edges(kind="referenced_by", dst=node):
                    neighbours.add(str(edge["src"]))
                row = index.symbol(node)
                if row:
                    for edge in index.edges(kind="imports", dst=f"file:{row['path']}"):
                        neighbours.add(str(edge["src"]))
            fresh = {n for n in neighbours if n and n not in seen}
            importers[node] = sorted(neighbours)
            nxt |= fresh
            if len(seen) + len(nxt) > MAX_SYMBOLS:
                truncated = True
                nxt = set(sorted(nxt)[: max(0, MAX_SYMBOLS - len(seen))])
                break
        if not nxt:
            break
        seen |= nxt
        levels.append(sorted(nxt))
        frontier = nxt

    hit_files: set[str] = set()
    hit_modules: set[str] = set()
    symbols_out: list[dict] = []
    for node in sorted(seen - starts):
        if node.startswith("file:") or node.startswith("mod:"):
            rel = node.split(":", 1)[1]
            hit_files.add(rel)
            hit_modules.add(graph.module_of(rel))
            continue
        row = index.symbol(node)
        if row is None:
            continue
        hit_files.add(row["path"])
        hit_modules.add(graph.module_of(row["path"]))
        if len(symbols_out) < MAX_SYMBOLS:
            symbols_out.append(graph.symbol_summary(row))

    own_files = {str(f).replace("\\", "/").strip("/") for f in (files or [])}
    own_files |= {
        (index.symbol(sid) or {}).get("path", "") for sid in start_symbols
    }
    own_files = {f for f in own_files if f}
    own_modules = {graph.module_of(f) for f in own_files}
    outer_files = sorted(hit_files - own_files)
    return {
        "levels": levels,
        "importers": importers,
        "symbols": symbols_out,
        "files": outer_files[:MAX_FILES],
        "files_total": len(outer_files),
        "dropped_files": max(0, len(outer_files) - MAX_FILES),
        "modules": sorted(hit_modules - own_modules),
        "own_files": sorted(own_files),
        "own_modules": sorted(own_modules),
        "fan_in": len(symbols_out) + len(outer_files),
        "depth": len(levels),
        "depth_reached": bool(levels),
        "truncated": truncated,
        # 说清楚这个数字是什么证据算出来的（archify 的 Truth before spectacle）
        "method": "static-import",
        "basis": "沿索引里的 import / 引用边做反向传递闭包（静态可达）",
        "not_runtime_impact": True,
    }


# ---------------------------------------------------------------------------
# 风险分级
# ---------------------------------------------------------------------------
def is_public_api(row: dict) -> tuple[bool, str]:
    """"对外承诺"的判定。

    判定口径（宁可先判成公共面再让规则圈路径，也不要默认"都不算"）：
    路由与组件天然对外；其余按 Python 的约定看——**非下划线命名的模块级符号**
    （不是类成员、不在测试文件里、模块名也不以 `_` 开头）就是别人可以直接 import 的。
    类成员是否对外取决于导出方式，这里不猜。
    """
    kind = str(row.get("kind") or "")
    name = str(row.get("name") or "")
    path = str(row.get("path") or "").replace("\\", "/")
    if kind == "route":
        return True, "HTTP 路由（对外契约）"
    if kind == "component":
        return True, "UI 组件（跨页面复用）"
    if kind in ("variable", "constant", "import"):
        return False, ""
    if not name or name.startswith("_"):
        return False, ""
    if str(row.get("parent") or ""):
        return False, ""                      # 类成员：是否对外取决于导出方式，不猜
    if path.endswith("__init__.py"):
        return True, "包导出（__init__ 里的公共符号）"
    if is_test_path(path):
        return False, ""
    if Path(path).stem.startswith("_"):
        return False, ""
    return True, "模块级公共符号"


def grade(score: int) -> str:
    if score >= 75:
        return "critical"
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def risk_of(
    *,
    reach_report: dict,
    symbols: list[dict],
    related_test_files: list[str],
    protected: list[dict] | None = None,
    cross_layer: list[dict] | None = None,
) -> dict:
    """把信号加权成分数与理由。**每条理由都要能被指着说"凭什么"。"""
    reasons: list[str] = []
    score = 8
    fan_in = int(reach_report.get("fan_in") or 0)
    files = int(reach_report.get("files_total") or 0)
    modules = len(reach_report.get("modules") or [])

    if fan_in >= 20:
        score += 30
        reasons.append(f"静态可达范围很大：{fan_in} 个符号/文件会读到它")
    elif fan_in >= 8:
        score += 20
        reasons.append(f"静态可达范围不小：{fan_in} 个符号/文件会读到它")
    elif fan_in >= 3:
        score += 10
        reasons.append(f"有 {fan_in} 处会读到它（静态可达）")
    elif fan_in:
        reasons.append(f"静态可达 {fan_in} 处（范围小）")

    if files >= 10:
        score += 12
        reasons.append(f"跨 {files} 个文件（按 import / 引用边）")
    if modules >= 3:
        score += 12
        reasons.append(f"跨 {modules} 个模块（改动不局部）")

    publics = [s for s in symbols if is_public_api(s)[0]]
    if publics:
        score += 18
        kinds = sorted({is_public_api(s)[1] for s in publics})
        reasons.append(
            f"动了对外承诺的部分：{kinds[0]}" + (f"（共 {len(publics)} 处）" if len(publics) > 1 else "")
        )

    if not related_test_files:
        score += 14
        reasons.append("没有找到关联测试（改动缺少可执行证据）")

    for item in protected or []:
        score += 15
        reasons.append(f"保护路径：{item.get('glob')}（{item.get('reason') or '规则要求谨慎'}）")

    for item in cross_layer or []:
        score += 10
        reasons.append(
            f"跨层依赖：{item.get('from_layer')} → {item.get('to_layer')}（{item.get('message') or '架构规则'}）"
        )

    low_confidence = [s for s in symbols if (s.get("confidence") or "high") != "high"]
    if low_confidence:
        score += 5
        reasons.append(f"其中 {len(low_confidence)} 个符号来自启发式解析（结构本身不确定）")

    score = max(0, min(100, score))
    return {
        "score": score,
        "level": grade(score),
        "reasons": reasons,
        "signals": {
            "fan_in": fan_in,
            "files": files,
            "modules": modules,
            "public_api": len(publics),
            "tests": len(related_test_files),
            "protected": len(protected or []),
            "cross_layer": len(cross_layer or []),
            "low_confidence": len(low_confidence),
        },
        # 风险评的是"改动的静态特征"，不是"运行时会不会出事"——界面照抄这句
        "basis": "静态特征加权：静态可达范围 / 公共面 / 架构规则 / 关联测试 / 解析置信度",
        "not_runtime_impact": True,
    }


def analyze(
    index: Any,
    *,
    symbol_ids: list[str] | None = None,
    files: list[str] | None = None,
    depth: int = DEFAULT_DEPTH,
    protected: list[dict] | None = None,
    cross_layer: list[dict] | None = None,
) -> dict:
    """一次算完静态可达 + 关联测试 + 风险（界面与门禁共用同一份事实）。"""
    symbols_rows: list[dict] = []
    for sid in symbol_ids or []:
        row = index.symbol(str(sid))
        if row is not None:
            symbols_rows.append(row)
    for rel in files or []:
        rel = str(rel).replace("\\", "/").strip("/")
        if not rel:
            continue
        symbols_rows.extend(index.query_all("SELECT * FROM symbols WHERE path=?", (rel,)))
    # 去重（文件与其成员会重叠）
    seen: set[str] = set()
    uniq: list[dict] = []
    for row in symbols_rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        uniq.append(row)

    summaries = [graph.symbol_summary(row) for row in uniq]
    reach_report = reach(index, symbol_ids=symbol_ids, files=files, depth=depth)
    tests = related_tests(index, sorted({row["path"] for row in uniq} | set(files or [])))
    risk = risk_of(
        reach_report=reach_report, symbols=summaries, related_test_files=tests,
        protected=protected, cross_layer=cross_layer,
    )
    return {
        "targets": {"symbol_ids": list(symbol_ids or []), "files": list(files or [])},
        "symbols": summaries[:MAX_SYMBOLS],
        "reach": reach_report,
        "tests": tests,
        "test_coverage": "covered" if tests else "none",
        "risk": risk,
        "basis": reach_report["basis"],
        "not_runtime_impact": True,
    }
