"""实现逻辑抽取（M18 · P8）：把这个函数**实际怎么做的**画出来。

地图原来只到"有哪些类/函数"（框架），看不到"这个函数按什么顺序做了什么事"。
用户反馈"只有框架、没有实现逻辑"说的就是这里。这个模块把一段函数体拆成
**有序步骤**，并顺手给出**调用关系**（它调了谁），因此界面上能回答三件事：

1. 它分几步、每步是什么（赋值 / 条件分支 / 循环 / try / with / 返回 / 抛错 / 调用）；
2. 每一步在第几行、源码长什么样（点一下跳到那里）；
3. 它调用了谁、被谁调用（拼出调用链，而不是只看静态依赖）。

**只对 Python 做**（标准库 `ast`，事实确定）。JS/TS/Vue 的启发式解析拿不到语句级
结构，这里如实返回 `available: false` 与原因——宁可说"没做"，也不猜一个看起来
很漂亮的假流程图（archify 的 Truth before spectacle）。

不做的事：不画数据流（需要类型推断，我们没有）、不判"这步有没有副作用"、
不把 `if/for` 的嵌套压平成一条线（缩进深度如实保留，否则读者会误判控制流）。
"""

from __future__ import annotations

import ast
from typing import Any, Callable

#: 一次最多返回多少步（超长函数只给前 N 步，并标注截断）
MAX_STEPS = 400
#: 嵌套深度上限：再深的层级用一条 "…" 收口，避免生成畸形结构
MAX_DEPTH = 8

#: 语句类型 → 人话标签（界面直接显示，不用再翻译一次）
STEP_LABELS = {
    "assign": "赋值",
    "augassign": "自增/自减",
    "annassign": "带注解赋值",
    "if": "条件",
    "elif": "否则如果",
    "else": "否则",
    "for": "遍历",
    "while": "循环",
    "try": "尝试",
    "except": "捕获异常",
    "finally": "收尾",
    "with": "上下文",
    "return": "返回",
    "raise": "抛错",
    "call": "调用",
    "await": "等待",
    "yield": "产出",
    "assert": "断言",
    "import": "导入",
    "global": "全局声明",
    "pass": "占位",
    "break": "跳出",
    "continue": "继续",
    "match": "匹配",
    "del": "删除",
    "nested": "内嵌函数",
    "expr": "表达式",
    "docstring": "文档",
    "unknown": "语句",
}


class LogicError(Exception):
    """不可接受的目标（例如不是函数、或文件不可读）。"""


def _label(node: ast.AST) -> tuple[str, str]:
    """(kind, 人话标签)。"""
    mapping: list[tuple[type, str]] = [
        (ast.Assign, "assign"), (ast.AugAssign, "augassign"), (ast.AnnAssign, "annassign"),
        (ast.If, "if"), (ast.Try, "try"),
        (ast.For, "for"), (ast.AsyncFor, "for"), (ast.While, "while"),
        (ast.With, "with"), (ast.AsyncWith, "with"),
        (ast.Return, "return"), (ast.Raise, "raise"), (ast.Assert, "assert"),
        (ast.Global, "global"), (ast.Nonlocal, "global"), (ast.Pass, "pass"),
        (ast.Break, "break"), (ast.Continue, "continue"), (ast.Delete, "del"),
        (ast.Import, "import"), (ast.ImportFrom, "import"),
        (ast.FunctionDef, "nested"), (ast.AsyncFunctionDef, "nested"), (ast.ClassDef, "nested"),
    ]
    if hasattr(ast, "Match"):
        mapping.append((ast.Match, "match"))  # type: ignore[attr-defined]
    if hasattr(ast, "TryStar"):
        mapping.append((ast.TryStar, "try"))  # type: ignore[attr-defined]
    for cls, kind in mapping:
        if isinstance(node, cls):
            return kind, STEP_LABELS.get(kind, kind)
    if isinstance(node, ast.Try) or (hasattr(ast, "TryStar") and isinstance(node, getattr(ast, "TryStar"))):
        return "try", STEP_LABELS["try"]
    if isinstance(node, ast.Expr):
        value = node.value
        if isinstance(value, ast.Await):
            return "await", STEP_LABELS["await"]
        if isinstance(value, ast.Yield) or isinstance(value, ast.YieldFrom):
            return "yield", STEP_LABELS["yield"]
        if isinstance(value, ast.Call):
            return "call", STEP_LABELS["call"]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return "docstring", STEP_LABELS["docstring"]
        return "expr", STEP_LABELS["expr"]
    return "unknown", STEP_LABELS["unknown"]


def _target_names(node: ast.AST) -> list[str]:
    out: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            out.append(child.id)
        elif isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
            out.append(f"{child.value.id}.{child.attr}")
    return out


def _called_names(node: ast.AST) -> list[str]:
    """这一步里调用了哪些名字（`foo(...)` / `obj.bar(...)`）。"""
    out: list[str] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            out.append(func.id)
        elif isinstance(func, ast.Attribute):
            name = func.attr
            if isinstance(func.value, ast.Name):
                name = f"{func.value.id}.{func.attr}"
            out.append(name)
    return out


def _source_line(lines: list[str], lineno: int, *, limit: int = 160) -> str:
    idx = max(1, lineno) - 1
    if 0 <= idx < len(lines):
        text = lines[idx].strip()
        return text[:limit]
    return ""


def _steps_for_body(
    body: list[ast.stmt],
    lines: list[str],
    *,
    depth: int,
    steps: list[dict],
    branch_counter: list[int],
) -> None:
    """把一段语句列表按顺序拆成步骤（嵌套层级如实保留）。"""
    for node in body:
        if len(steps) >= MAX_STEPS:
            return
        kind, label = _label(node)
        entry: dict[str, Any] = {
            "kind": kind,
            "label": label,
            "line": int(getattr(node, "lineno", 0) or 0),
            "end_line": int(getattr(node, "end_lineno", 0) or getattr(node, "lineno", 0) or 0),
            "depth": min(depth, MAX_DEPTH),
            "text": _source_line(lines, int(getattr(node, "lineno", 0) or 0)),
            "names": [],
            "calls": [],
            "condition": "",
            "children": [],
        }
        if isinstance(node, (ast.If,)):
            entry["condition"] = _source_line(lines, node.lineno)
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            entry["condition"] = _source_line(lines, node.lineno)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            entry["names"] = _target_names(node)
        # 调用可能出现在任何语句里（赋值右侧、推导式、return 里）——一律收集，
        # 否则"它调用了谁"只在大括号风格的代码里才看得见。
        if kind not in ("docstring", "pass", "nested"):
            entry["calls"] = _called_names(node)
        if isinstance(node, ast.Return) and node.value is not None:
            entry["names"] = _target_names(node.value)
        steps.append(entry)
        if kind in ("nested",):
            continue                      # 内嵌函数不展开（它是另一张图）

        # 分支/循环的子块：显示为缩进更深的步骤，保留控制流结构
        branch_counter[0] += 1
        for attr, branch_kind in (
            ("body", kind if kind in ("if", "for", "while", "try", "with", "match") else "body"),
            ("orelse", "else" if kind == "if" else "body"),
            ("finalbody", "finally"),
            ("handlers", "except"),
        ):
            children = getattr(node, attr, None)
            if not children:
                continue
            if attr == "handlers":
                child_stmts: list[ast.stmt] = []
                for handler in children:
                    child_stmts.extend(getattr(handler, "body", []) or [])
                if child_stmts:
                    marker = {
                        "kind": "except",
                        "label": STEP_LABELS["except"],
                        "line": int(getattr(children[0], "lineno", 0) or 0),
                        "text": "",
                        "depth": min(depth + 1, MAX_DEPTH),
                        "names": [],
                        "calls": [],
                        "condition": "",
                        "children": [],
                    }
                    entry["children"].append(marker)
                    _steps_for_body(
                        child_stmts, lines, depth=depth + 2, steps=steps, branch_counter=branch_counter
                    )
                continue
            if not isinstance(children, list):
                continue
            marker = {
                "kind": "else" if branch_kind == "else" else ("finally" if branch_kind == "finally" else "if"),
                "label": STEP_LABELS.get(branch_kind, branch_kind),
                "line": int(getattr(children[0], "lineno", 0) or 0),
                "text": "",
                "depth": min(depth + 1, MAX_DEPTH),
                "names": [],
                "calls": [],
                "condition": "",
                "children": [],
            }
            if branch_kind in ("if", "for", "while", "try", "with", "match"):
                marker["label"] = STEP_LABELS[branch_kind]
            entry["children"].append(marker)
            _steps_for_body(
                children, lines, depth=depth + 2, steps=steps, branch_counter=branch_counter
            )


def _find_callable(tree: ast.AST, symbol: dict) -> ast.AST | None:
    """按符号的限定名/行号定位函数节点。

    先按行号（解析器记的就是定义行），再按 qname 的末段名字兜底——
    行号在文件被改动后会漂移，而名字不会。
    """
    target_line = int(symbol.get("line") or 0)
    qname = str(symbol.get("qname") or symbol.get("name") or "")
    short = qname.split(".")[-1]
    best: ast.AST | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if target_line and int(getattr(node, "lineno", 0) or 0) == target_line:
            return node
        if short and node.name == short and best is None:
            best = node
    return best


def function_logic(
    index: Any,
    *,
    read_text: Callable[[str], str],
    symbol_id: str,
) -> dict:
    """一个函数/类的实现步骤 + 调用关系。非 Python 或定位不到时**如实不可用**。"""
    row = index.symbol(symbol_id)
    if row is None:
        raise LogicError(f"符号不存在：{symbol_id}")
    rel = str(row.get("path") or "")
    base = {
        "symbol_id": symbol_id,
        "name": row.get("name") or "",
        "kind": row.get("kind") or "",
        "path": rel,
        "line": int(row.get("line") or 0),
        "end_line": int(row.get("end_line") or 0),
        "confidence": row.get("confidence") or "high",
    }
    if not rel.endswith(".py"):
        # 大括号语言（JS/TS/Vue/Java/C/Go…）走"按行 + 括号层级"的启发式扫描：
        # 本仓 617 个可调用符号在前端代码里，一条"只有 Python 有"的理由就把它们
        # 全判成空的，"实现方法"这件事等于没覆盖到一半（P11）。
        from code_agent.codeintel import logic_brace

        if logic_brace.supports(rel):
            return logic_brace.brace_logic(index, read_text=read_text, symbol_id=symbol_id)
        return {
            **base,
            "available": False,
            "reason": "只有 Python 有语句级解析（AST）；这个文件走的是启发式解析，步骤不可靠",
            "steps": [],
            "calls": [],
            "callers": [],
            "counts": {},
        }

    try:
        source = read_text(rel)
    except Exception as exc:  # noqa: BLE001
        raise LogicError(f"文件读不到：{rel}（{exc}）") from exc
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {
            **base,
            "available": False,
            "reason": f"这个文件当前不是合法 Python（{exc.msg}，第 {exc.lineno} 行）",
            "steps": [],
            "calls": [],
            "callers": [],
            "counts": {},
        }

    node = _find_callable(tree, row)
    if node is None:
        # 变量/常量本来就没有"步骤"这回事——别把这种情况说成"可能刚被改动"，
        # 那会让读者以为索引坏了（真机截图里就是这么提示的）。
        if base["kind"] not in ("function", "method", "hook", "route", "class",
                                "interface", "component", "enum", "nested"):
            kind_cn = {
                "variable": "变量", "constant": "常量", "property": "属性",
                "module": "模块", "unknown": "符号",
            }.get(base["kind"], "符号")
            return {
                **base,
                "available": False,
                # 界面上的前缀已经是"这个符号没有实现步骤："，所以这里不要再重复那半句
                "reason": f"它是{kind_cn}——步骤只对函数/方法/类有意义；它的作用见上方「作用」说明",
                "steps": [],
                "calls": [],
                "callers": [],
                "counts": {},
            }
        return {
            **base,
            "available": False,
            "reason": "在文件里没定位到这个符号（可能刚被改动，地图索引稍后会刷新）",
            "steps": [],
            "calls": [],
            "callers": [],
            "counts": {},
        }

    lines = source.split("\n")
    body: list[ast.stmt] = list(getattr(node, "body", []) or [])
    steps: list[dict] = []
    _steps_for_body(body, lines, depth=0, steps=steps, branch_counter=[0])
    truncated = len(steps) > MAX_STEPS or len(body) > len(steps)
    steps = steps[:MAX_STEPS]

    # 它调了谁：把步骤里出现的调用名解析成索引里的符号（同名多处定义不认，避免乱连）。
    # 私有函数（`_step`）也要参与：调用链不该因为下划线就断。
    by_name: dict[str, list[str]] = {}
    for other in index.all_symbols():
        name = str(other["name"] or "")
        if name:
            by_name.setdefault(name, []).append(other["id"])
    call_names: list[str] = []
    for step in steps:
        call_names.extend(step.get("calls") or [])
    seen: set[str] = set()
    calls: list[dict] = []
    unresolved: list[str] = []
    for name in call_names:
        short = name.split(".")[-1]
        if short in seen:
            continue
        seen.add(short)
        ids = by_name.get(short) or []
        if len(ids) == 1:
            target = index.symbol(ids[0]) or {}
            calls.append(
                {
                    "id": ids[0],
                    "name": short,
                    "path": target.get("path") or "",
                    "line": int(target.get("line") or 0),
                    "kind": target.get("kind") or "",
                }
            )
        elif not ids:
            unresolved.append(short)
    calls.sort(key=lambda c: (c["path"], c["line"]))

    # 谁调了它：先看索引里的引用边（签名级启发式），再在**候选文件**里按 `name(` 找调用点。
    # 为什么要第二步：引用边只认"签名里提到过这个名字"，而 `lookup(cached)` 出现在函数体里，
    # 靠签名边会漏掉真正的调用方。候选集收敛到"同一模块 + import 了这个文件的文件"，
    # 所以不会退化成全仓扫描。
    callers: list[dict] = []
    seen_callers: set[str] = set()

    def _add_caller(path: str, confidence: str, line: int = 0) -> None:
        key = f"{path}:{line}"
        if not path or key in seen_callers:
            return
        seen_callers.add(key)
        callers.append(
            {"path": path, "line": line, "kind": "call", "confidence": confidence}
        )

    for edge in index.edges(kind="imports", dst=f"file:{rel}"):
        src = str(edge["src"]).replace("file:", "")
        _add_caller(src, "medium")
    for edge in index.edges(kind="referenced_by", dst=symbol_id):
        src = str(edge["src"]).replace("file:", "")
        _add_caller(src, "medium")

    name = str(row.get("name") or "")
    if name and not name.startswith("__"):
        import re as _re

        pattern = _re.compile(rf"(?<![\w.]){_re.escape(name)}\s*\(")
        module = rel.rsplit("/", 1)[0] if "/" in rel else ""
        candidates = {rel}
        for other in index.file_rows():
            if other == rel:
                continue
            other_module = other.rsplit("/", 1)[0] if "/" in other else ""
            if other_module == module:
                candidates.add(other)
        for other in list(candidates)[:80]:
            try:
                text = read_text(other)
            except Exception:  # noqa: BLE001
                continue
            if len(text) > 512 * 1024:
                text = text[: 512 * 1024]
            for offset, raw in enumerate(text.split("\n"), start=1):
                if pattern.search(raw):
                    _add_caller(other, "high", offset)
                    break
    callers.sort(key=lambda c: (c["path"], c["line"]))

    counts = {
        "steps": len(steps),
        "branches": sum(1 for s in steps if s["kind"] in ("if", "elif", "else", "match")),
        "loops": sum(1 for s in steps if s["kind"] in ("for", "while")),
        "returns": sum(1 for s in steps if s["kind"] == "return"),
        "raises": sum(1 for s in steps if s["kind"] == "raise"),
        "calls": len(calls),
    }
    return {
        **base,
        "available": True,
        "steps": steps,
        "calls": calls[:40],
        "unresolved_calls": unresolved[:20],
        "callers": callers[:20],
        "counts": counts,
        "truncated": truncated,
        "basis": "步骤来自标准库 ast 的语句序列（嵌套层级如实保留）；"
                 "调用关系按名字唯一匹配（同名多处定义不连）；调用方来自引用边（启发式）",
        "not_runtime_trace": True,
        "note": "这是静态步骤，不是运行时调用栈：分支只表示代码里存在这些路径",
    }
