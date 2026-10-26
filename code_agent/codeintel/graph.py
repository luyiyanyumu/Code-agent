"""图装配（M18 · P1）：把索引里的"符号 + 文件"变成一张可画、可查、可讲给模型的图。

边的两类生命周期（这是本模块最重要的设计决定）：
1. **import 边**：来源是单个文件，随该文件重解析而**增量替换**
   （`apply_parse` 只删 src 属于该文件的边）——改一个文件不该重算全图。
2. **跨文件边**（inherits / referenced_by）：来源是全局信息（谁能解析到基类、
   谁引用了这个符号），因此**整表重算**：几条 SQL 就够，省掉"反向边增量维护"
   这类容易出错的状态机。重算后若集合有变化才 bump 版本。

本模块**只读索引、不做文件 IO**：地图可以毫秒级反复重建，"投影保真度"校验
也因此能独立进行（保真度校验会真的重解析文件，那是 service 的事）。
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

from code_agent.codeintel.store import SymbolIndex, file_id, module_id

#: 模块聚合层级：顶层目录一层 + 根目录文件合成"(根目录)"
_MODULE_DEPTH = 1
_PURPOSE_LIMIT = 120
_DIGEST_SYMBOL_LIMIT = 60

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".json", ".css")
IMPORT_TARGET_EXTS = (".py", ".pyi", *_JS_EXTS, ".java")
# 改进导入解析后，旧索引应在下一次正常扫描轻量更新关系，保留符号与指纹。
IMPORTS_REVISION = "2"

#: 标识符（跨文件引用关系按**词**匹配，而不是子串匹配：`main` 不该命中 `domain`）
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _parse_json_obj(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# 导入解析
# ---------------------------------------------------------------------------
def _py_candidates(rel_path: str, module: str, level: int) -> list[str]:
    package_dir = posixpath.dirname(rel_path)
    if level:
        parts = package_dir.split("/") if package_dir else []
        up = level - 1
        if up > len(parts):
            return []
        base_parts = parts[: len(parts) - up] if up else parts
        base = "/".join(base_parts)
        tail = module.replace(".", "/") if module else ""
        rel_base = "/".join(p for p in (base, tail) if p)
    else:
        rel_base = module.replace(".", "/")
    if not rel_base:
        return []
    return [f"{rel_base}.py", f"{rel_base}/__init__.py", f"{rel_base}.pyi"]


def _js_candidates(rel_path: str, module: str) -> list[str]:
    if not module.startswith("."):
        return []
    base_dir = posixpath.dirname(rel_path)
    joined = posixpath.normpath(posixpath.join(base_dir, module)) if base_dir else posixpath.normpath(module)
    if joined.startswith(".."):
        return []
    out = [joined]
    out.extend(f"{joined}{ext}" for ext in _JS_EXTS)
    out.extend(f"{joined}/index{ext}" for ext in _JS_EXTS)
    return out


def resolve_import(rel_path: str, module: str, level: int, known: set[str]) -> str | None:
    """把一条 import 解析成项目内相对路径；外部依赖返回 None（不进图）。"""
    if not module and not level:
        return None
    if rel_path.endswith(".java"):
        if "." not in module or module.endswith(".*"):
            return None
        suffix = module.replace(".", "/") + ".java"
        # Java 包目录可位于 backend/src/main/java 等源根；只认完整包路径。
        matches = [path for path in known if path == suffix or path.endswith("/" + suffix)]
        return matches[0] if len(matches) == 1 else None
    candidates = (
        _py_candidates(rel_path, module, level)
        if (level or rel_path.endswith((".py", ".pyi")))
        else _js_candidates(rel_path, module)
    )
    for cand in candidates:
        if cand in known:
            return cand
    return None


def import_edges(
    rel: str, imports: list[dict], known: set[str], *, max_edges: int = 60
) -> tuple[list[tuple[str, str, str, int]], int]:
    """单文件的 import 边（聚合重复：同一目标 import 两次算 count=2）。

    返回 (边列表, 外部依赖条数)。外部依赖不进图，但计数会出现在地图的告警里——
    "这个模块依赖 12 个外部包"是有用信息，画成节点才是噪声。
    """
    counter: dict[str, int] = {}
    externals = 0
    for item in imports[:200]:
        module = str(item.get("module") or "").strip()
        level = int(item.get("level") or 0)
        if not module and not level:
            continue
        targets: set[str] = set()
        target = resolve_import(rel, module, level, known)
        if target:
            targets.add(target)
        if rel.endswith((".py", ".pyi")):
            # `from . import helper` 和 `from pkg import helper` 可能导入子模块。
            # 只加实际存在的文件；普通类/函数名不会凭空变成依赖节点。
            for name in item.get("names") or []:
                if not name or name == "*":
                    continue
                child = f"{module}.{name}" if module else str(name)
                resolved = resolve_import(rel, child, level, known)
                if resolved:
                    targets.add(resolved)
        if not targets:
            externals += 1
            continue
        for target in targets:
            if target != rel:
                counter[target] = counter.get(target, 0) + 1
    edges = [
        (file_id(rel), file_id(target), "imports", count)
        for target, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:max_edges]
    ]
    return edges, externals


# ---------------------------------------------------------------------------
# 跨文件边（整表重算）
# ---------------------------------------------------------------------------
def compute_cross_edges(index: SymbolIndex) -> list[tuple[str, str, str, int]]:
    """inherits（类 → 基类）与 referenced_by（文件 → 被它引用的符号）。"""
    items: list[tuple[str, str, str, int]] = []

    class_rows = index.query_all(
        "SELECT id, path, name, bases FROM symbols WHERE kind='class' AND bases <> '[]'"
    )
    by_name: dict[str, list[dict]] = {}
    for row in index.query_all("SELECT id, path, name FROM symbols WHERE kind IN ('class','interface','type')"):
        by_name.setdefault(row["name"], []).append(row)

    for row in class_rows:
        for base in _parse_json_list(row["bases"]):
            if not base or "." in base or "(" in base:
                continue  # 带模块前缀或泛型的基类不猜
            for target in by_name.get(base, []):
                if target["path"] != row["path"]:
                    items.append((row["id"], target["id"], "inherits", 1))
                    break

    # 引用关系：signature 里出现某个唯一命名的定义（启发式，medium 置信度）
    ranked = index.query_all(
        "SELECT id, name, path FROM symbols "
        "WHERE kind IN ('class','interface','type','enum','function','component') "
        "ORDER BY (end_line - line) DESC LIMIT 400"
    )
    seen_names: dict[str, list[dict]] = {}
    for row in ranked:
        seen_names.setdefault(row["name"], []).append(row)

    # 一次取出所有签名，在内存里建"文件 × 标识符 → 出现次数"的倒排表。
    # 早先是"每个唯一名字查一次 `signature LIKE %name%`"：1.5 万符号的仓库上
    # 等于 1.5 万次全表扫，首次扫描因此要 30s+（真机实测）。
    mentions: dict[tuple[str, str], int] = {}
    for row in index.query_all("SELECT path, signature FROM symbols"):
        words = set(_IDENT_RE.findall(row["signature"] or ""))
        for word in words:
            key = (row["path"], word)
            mentions[key] = mentions.get(key, 0) + 1

    counted: dict[tuple[str, str], int] = {}
    for name, rows in seen_names.items():
        if len(rows) != 1 or len(name) < 4:
            continue  # 同名多处定义 or 太短的名字（误报率高）不参与
        target = rows[0]
        for (path, word), hits in mentions.items():
            if word != name or path == target["path"]:
                continue
            counted[(file_id(path), target["id"])] = counted.get(
                (file_id(path), target["id"]), 0
            ) + hits

    items.extend((src, dst, "referenced_by", count) for (src, dst), count in counted.items())
    return items


def rebuild_cross_edges(index: SymbolIndex) -> tuple[list[tuple[str, str, str, int]], bool]:
    """整表重算跨文件边。返回 (边列表, 是否有变化)。"""
    old = {(r["src"], r["dst"], r["kind"], r["count"]) for r in index.edges() if r["kind"] != "imports"}
    items = compute_cross_edges(index)
    new = set(items)
    if old == new:
        return items, False
    index.execute("DELETE FROM edges WHERE kind <> 'imports'")
    if items:
        index.executemany("INSERT OR REPLACE INTO edges (src, dst, kind, count) VALUES (?,?,?,?)", items)
    return items, True


# ---------------------------------------------------------------------------
# 模块聚合与元信息
# ---------------------------------------------------------------------------
def module_of(rel_path: str) -> str:
    parts = rel_path.split("/")
    return "" if len(parts) == 1 else "/".join(parts[:_MODULE_DEPTH])


def build_modules(index: SymbolIndex) -> list[dict]:
    files = index.file_rows()
    buckets: dict[str, dict] = {}
    symbol_counts: dict[str, dict[str, int]] = {}
    for row in index.query_all("SELECT path, kind, COUNT(*) AS n FROM symbols GROUP BY path, kind"):
        mod = module_of(row["path"])
        symbol_counts.setdefault(mod, {})
        symbol_counts[mod][row["kind"]] = symbol_counts[mod].get(row["kind"], 0) + int(row["n"])

    for rel, row in files.items():
        mod = module_of(rel)
        bucket = buckets.setdefault(
            mod,
            {
                "id": module_id(mod), "path": mod, "name": mod.rsplit("/", 1)[-1] or "(根目录)",
                "files": 0, "loc": 0, "bytes": 0, "entry_points": [], "tests": [], "skipped": 0,
            },
        )
        if row["skipped"]:
            bucket["skipped"] += 1
            continue
        bucket["files"] += 1
        bucket["loc"] += int(row["loc"] or 0)
        bucket["bytes"] += int(row["size"] or 0)
        if row["entrypoint"]:
            bucket["entry_points"].append(rel)
        if "test" in rel.lower():
            bucket["tests"].append(rel)

    out: list[dict] = []
    for mod, bucket in buckets.items():
        purpose = _module_purpose(index, mod, bucket)
        out.append(
            {
                "id": bucket["id"], "path": bucket["path"], "name": bucket["name"],
                "files": bucket["files"], "loc": bucket["loc"], "bytes": bucket["bytes"],
                "symbol_counts": symbol_counts.get(mod, {}),
                "purpose": purpose["text"], "purpose_source": purpose["source"],
                "entry_points": bucket["entry_points"][:5], "tests": bucket["tests"][:10],
                "skipped_files": bucket["skipped"],
            }
        )
    out.sort(key=lambda m: (m["path"] != "", -m["loc"]))
    return out


def _module_purpose(index: SymbolIndex, mod: str, bucket: dict) -> dict:
    """模块用途：优先包入口文件的模块 docstring，其次模块内最大的 docstring。

    用"模块 docstring"而不是"最大的符号 docstring"是有意的：
    前者说的就是"这个包是干嘛的"，后者是"这个类实现了什么"——
    地图上的模块行要回答前一个问题。
    """
    prefix = f"{mod}/" if mod else ""
    for candidate in (f"{prefix}__init__.py", f"{prefix}index.ts", f"{prefix}index.js", f"{prefix}mod.rs"):
        row = index.query_one("SELECT module_doc FROM files WHERE path=?", (candidate,))
        if row and row.get("module_doc"):
            return {"text": row["module_doc"][:_PURPOSE_LIMIT], "source": "package-docstring"}
    row = index.query_one(
        "SELECT path, module_doc FROM files WHERE path LIKE ? AND module_doc <> '' "
        "ORDER BY size DESC LIMIT 1",
        (f"{prefix}%",),
    )
    if row and row.get("module_doc"):
        return {"text": row["module_doc"][:_PURPOSE_LIMIT], "source": "module-docstring"}
    row = index.query_one(
        "SELECT doc FROM symbols WHERE path LIKE ? AND doc<>'' "
        "ORDER BY (end_line - line) DESC LIMIT 1",
        (f"{prefix}%",),
    )
    if row and row["doc"]:
        return {"text": row["doc"][:_PURPOSE_LIMIT], "source": "docstring"}
    if bucket["entry_points"]:
        return {"text": f"入口模块（{posixpath.basename(bucket['entry_points'][0])}）", "source": "entrypoint"}
    return {"text": "", "source": ""}


def build_entrypoints(index: SymbolIndex) -> list[dict]:
    out: list[dict] = []
    for row in index.query_all("SELECT path FROM files WHERE entrypoint=1 ORDER BY path"):
        out.append({"path": row["path"], "kind": "python_main", "evidence": "__main__ 块"})
    for rel in ("web/src/App.vue", "src/App.vue", "src/App.jsx", "src/App.tsx",
                "web/src/main.js", "src/main.js", "src/main.ts"):
        if index.file_row(rel) and not any(e["path"] == rel for e in out):
            out.append({"path": rel, "kind": "spa_root", "evidence": "前端入口文件"})
    for rel in ("code_agent/server.py", "code_agent/api/app.py", "main.py", "app/main.py"):
        if index.file_row(rel) and not any(e["path"] == rel for e in out):
            out.append({"path": rel, "kind": "http_app", "evidence": "服务端入口文件"})
    return out[:20]


def build_routes(index: SymbolIndex) -> list[dict]:
    out: list[dict] = []
    for row in index.query_all(
        "SELECT id, path, name, line, extra FROM symbols WHERE kind='route' ORDER BY path, line"
    ):
        extra = _parse_json_obj(row["extra"])
        out.append(
            {
                "method": extra.get("route_method") or "GET",
                "path": extra.get("route_path") or row["name"],
                "file": row["path"],
                "line": int(row["line"] or 0),
                "symbol_id": row["id"],
            }
        )
    return out[:500]


def build_commands(index: SymbolIndex) -> dict:
    """从清单文件推断构建/测试命令。宁缺毋滥：推断不出就留空，让模型先看再说。"""
    commands: dict[str, list[str]] = {"test": [], "lint": [], "build": [], "run": []}
    sources: list[str] = []

    if index.file_row("pyproject.toml"):
        sources.append("pyproject.toml")
        has_tests = bool(index.query_one("SELECT 1 FROM files WHERE path LIKE 'tests/%' LIMIT 1"))
        if has_tests:
            commands["test"].append("python -m pytest")
        if index.file_row("ruff.toml") or index.file_row(".ruff.toml"):
            commands["lint"].append("ruff check .")

    if index.file_row("package.json"):
        sources.append("package.json")
        prefix = "npm --prefix web" if index.file_row("web/package.json") else "npm"
        commands["test"].append(f"{prefix} test")
        commands["build"].append(f"{prefix} run build")
        commands["run"].append(f"{prefix} run dev")

    for name in ("Makefile", "makefile"):
        if index.file_row(name):
            sources.append(name)
            commands["build"].append("make")
            break

    if index.file_row("requirements.txt") and "pyproject.toml" not in sources:
        sources.append("requirements.txt")
        commands["test"].append("python -m pytest")

    return {
        "test": list(dict.fromkeys(commands["test"]))[:3],
        "lint": list(dict.fromkeys(commands["lint"]))[:3],
        "build": list(dict.fromkeys(commands["build"]))[:3],
        "run": list(dict.fromkeys(commands["run"]))[:3],
        "source": sources,
    }


# ---------------------------------------------------------------------------
# 给模型的地图（digest）
# ---------------------------------------------------------------------------
def build_digest(index: SymbolIndex, project: dict, *, max_chars: int = 6000) -> str:
    """紧凑文本地图：模块 → 入口 → 接口 → 命令 → 关键符号索引。

    **字符预算是硬约束**：超了就按"整行裁剪"，绝不截断到半句话——
    半截的地图比没有地图更误导。
    """
    modules = build_modules(index)
    files = index.file_rows()
    counts = index.counts()
    kinds = index.kind_counts()
    languages: dict[str, int] = {}
    for row in files.values():
        if row["skipped"]:
            continue
        languages[row["lang"]] = languages.get(row["lang"], 0) + 1
    lang_text = "；".join(
        f"{name} {n} 文件" for name, n in sorted(languages.items(), key=lambda kv: -kv[1])[:5]
    )

    header: list[str] = [
        "[代码模式｜项目地图]（自动生成，静态分析，非模型臆测）",
        f"项目：{project['name']}  根目录：{project['root_path']}",
        f"规模：{counts['files']} 文件 / {counts['symbols']} 符号 / {counts['edges']} 条关系；"
        + ("符号构成：" + ", ".join(f"{k} {v}" for k, v in list(kinds.items())[:8]) if kinds else ""),
        f"语言：{lang_text}",
        "模块：",
    ]
    for mod in modules[:24]:
        purpose = f" — {mod['purpose']}" if mod["purpose"] else ""
        header.append(f"  {mod['path'] or '(根目录)'}：{mod['files']} 文件 / {mod['loc']} 行{purpose}")
    entries = build_entrypoints(index)
    if entries:
        header.append("入口：" + " · ".join(f"{e['path']}({e['kind']})" for e in entries[:6]))
    routes = build_routes(index)
    if routes:
        header.append(
            f"对外接口：{len(routes)} 个路由，例如 "
            + " · ".join(f"{r['method']} {r['path']}" for r in routes[:5])
        )
    commands = build_commands(index)
    command_text = " · ".join(f"{key}={val[0]}" for key, val in commands.items() if key != "source" and val)
    if command_text:
        header.append(f"命令：{command_text}")

    head_text = "\n".join(header)
    if len(head_text) >= max_chars:
        return head_text[: max_chars - 30] + "\n（地图超出预算，模块清单已截断）"

    body_head = "关键符号（按体量排序；要看细节用 code_map 取分片）："
    rows = index.query_all(
        "SELECT s.id, s.path, s.kind, s.name, s.line, s.end_line,"
        " (SELECT COUNT(*) FROM symbols c WHERE c.parent = s.id) AS members"
        " FROM symbols s WHERE s.kind IN ('class','interface','component','function','route','enum')"
        " ORDER BY (s.end_line - s.line) + members * 3 DESC, s.line LIMIT ?",
        (_DIGEST_SYMBOL_LIMIT,),
    )
    entries_text: list[str] = []
    for row in rows[:1]:
        entries_text.append(_digest_symbol_line(row))
    budget = max_chars - len(head_text) - len(body_head) - len(entries_text[0] if entries_text else "") - 4
    for row in rows[1:]:
        line = _digest_symbol_line(row)
        if len(line) + 1 > budget:
            entries_text.append(f"  …（其余 {len(rows) - len(entries_text)} 个符号未列出）")
            break
        entries_text.append(line)
        budget -= len(line) + 1
    if not entries_text:
        entries_text = ["  （未解析到符号）"]
    return head_text + "\n" + body_head + "\n" + "\n".join(entries_text)


def _digest_symbol_line(row: dict) -> str:
    line = f"  {row['kind']} {row['name']}（{row['path']}:{row['line']}）"
    if row["members"]:
        line += f" [{row['members']} 成员]"
    return line


# ---------------------------------------------------------------------------
# 结构树（语义缩放）
# ---------------------------------------------------------------------------
def build_structure(
    files: dict[str, dict],
    symbols_by_path: dict[str, list[dict]],
    *,
    depth: int = 3,
    module: str | None = None,
    root_name: str = "",
    only_paths: set[str] | None = None,
) -> dict:
    """目录 → 文件 → 符号（扁平 + parent）的结构树。

    depth 就是"语义缩放"的旋钮，而不是图形的放大倍数：
      1 → 只到文件（看模块骨架）
      2 → 到顶层符号（看每个文件的对外结构）
      3 → 到成员（看类里有哪些方法）
    符号用扁平列表 + parent 字段下发，前端按 parent 归位：比嵌套 JSON 小得多，
    也便于做"只显示某类符号"的过滤。

    `only_paths`（有类型过滤时才给）：**只保留这些文件**。不然"只看变量"会得到
    一张和原来一模一样的树——231 个文件节点里大部分一个变量都没有，过滤看起来没生效。
    """
    prefix = f"{module.strip('/')}/" if module else ""
    root: dict[str, Any] = {
        "id": module_id(module or ""),
        "kind": "dir",
        "name": (module.split("/")[-1] if module else (root_name or "(项目)")),
        "path": module or "",
        "loc": 0,
        "files": 0,
        "children": [],
        "symbol_count": 0,
    }
    dirs: dict[str, dict] = {"": root}

    def dir_node(rel_dir: str) -> dict:
        """取出（必要时创建）目录节点。"""
        if rel_dir in dirs:
            return dirs[rel_dir]
        parent_rel = rel_dir.rsplit("/", 1)[0] if "/" in rel_dir else ""
        parent = dir_node(parent_rel)
        node = {
            "id": module_id(rel_dir),
            "kind": "dir",
            "name": rel_dir.rsplit("/", 1)[-1],
            "path": rel_dir,
            "loc": 0,
            "files": 0,
            "children": [],
            "symbol_count": 0,
        }
        dirs[rel_dir] = node
        parent["children"].append(node)
        return node

    for rel, row in sorted(files.items()):
        if prefix and not rel.startswith(prefix):
            continue
        if only_paths is not None and rel not in only_paths:
            continue  # 类型过滤生效时：不画"一个匹配符号都没有"的文件
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        parent_dir = dir_node(rel_dir) if rel_dir else root
        # 目录链上的祖先也要计数（否则模块行数会与子树不符）
        chain: list[dict] = [parent_dir]
        cursor = rel_dir
        while cursor and "/" in cursor:
            cursor = cursor.rsplit("/", 1)[0]
            chain.append(dir_node(cursor))
        symbols = symbols_by_path.get(rel, [])
        #: 匹配到的符号数（过滤后），折叠状态下也能看到"这个文件里有几个"
        matched = len(symbols)
        if depth < 2:
            symbols = []  # depth=1 只看模块骨架：符号一律不画
        elif depth < 3 and only_paths is None:
            # 只在"没有类型过滤"时做顶层裁剪。有过滤时**必须**保留带 parent 的符号：
            # route/hook 全都挂在它们的处理函数/组件下（本仓 139 个 route 无一例外），
            # 按 depth=2 裁掉就等于"点了路由却一个路由都看不到"（自检抓到过）。
            symbols = [s for s in symbols if not s.get("parent")]
        file_node = {
            "id": file_id(rel),
            "kind": "file",
            "name": rel.rsplit("/", 1)[-1],
            "path": rel,
            "lang": row["lang"],
            "loc": int(row["loc"] or 0),
            "skipped": row["skipped"] or "",
            "entrypoint": bool(row["entrypoint"]),
            "error": (row["error"] or "")[:200],
            "symbol_count": matched,
            "symbols": symbols,
        }
        parent_dir["children"].append(file_node)
        for node in chain:
            node["loc"] += int(row["loc"] or 0)
            node["files"] += 1
            node["symbol_count"] += matched

    for node in dirs.values():
        node["children"].sort(key=lambda c: (c["kind"] != "dir", c["name"].lower()))
    return root


def symbol_summary(row: dict) -> dict:
    """符号 → 前端/模型的轻量表示。"""
    extra = _parse_json_obj(row.get("extra"))
    summary = {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "qname": row["qname"],
        "path": row["path"],
        "line": int(row["line"] or 0),
        "end_line": int(row["end_line"] or 0),
        "parent": row["parent"] or "",
        "signature": row["signature"] or "",
        "confidence": row["confidence"] or "high",
        "complexity": int(row["complexity"] or 0),
    }
    for key in ("route_method", "route_path", "props", "emits", "hook"):
        if key in extra:
            summary[key] = extra[key]
    return summary


def symbol_detail(row: dict) -> dict:
    detail = symbol_summary(row)
    detail.update(
        {
            "doc": row.get("doc") or "",
            "params": _parse_json_list(row.get("params")),
            "returns": row.get("returns") or "",
            "decorators": _parse_json_list(row.get("decorators")),
            "bases": _parse_json_list(row.get("bases")),
        }
    )
    return detail
