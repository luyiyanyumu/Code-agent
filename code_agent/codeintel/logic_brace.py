"""非 Python 的"实现步骤"：按行 + 括号层级的启发式扫描（M18 · P11）。

为什么要有它：本仓 2629 个可调用符号里 **617 个（23.5%）在 .vue/.js 里**，
而 `function_logic` 对它们一律返回"只有 Python 有语句级解析"——于是"实现方法"
这件事在前端一半代码上是空的，而用户要看的恰恰是"这个函数按什么顺序做了什么"。

**它不是解析器**（我们不会为了这个把 Babel/TS 编译器塞进侧车）。做法是：

1. 把注释与字符串字面量**替换成等长空白**（跨行注释/模板字符串用状态机跟着走），
   这样花括号计数不会被 `"{ }"`、`// }}`、模板串里的 `${}` 骗到；
2. 按**花括号层级**得到嵌套深度（函数体起点为 0），按**小括号/中括号是否闭合**
   判断"这一行是不是一条新语句的开头"（多行表达式不会被切碎）；
3. 用一组"行首关键字"把语句分类（if/else/for/while/switch/case/try/catch/return/
   throw/await/yield/赋值/调用/内嵌函数…），标签与 Python 那条路径共用一套词汇，
   界面不需要为两套解析写两种渲染。

因为不是解析器，这个模块**必须**把不确定性说清楚：`confidence` 一律 `medium`
（抽屉会显示"启发式解析"）、`basis` 写明做法、`note` 列出已知失真（多行表达式、
嵌套模板、`case` 穿透…）。宁可标"启发式"，也不假装和 AST 一样可靠。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from code_agent.codeintel.logic import STEP_LABELS, LogicError

#: 支持的语言：C 系的大括号语言（Vue 取的是它的 <script> 段，行号是 .vue 的绝对行号）
BRACE_EXTS = (
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".swift", ".scala", ".php",
)

MAX_STEPS = 400
MAX_DEPTH = 8
MAX_CANDIDATE_BYTES = 512 * 1024

#: 语句关键字 → (kind, 说明)。kind 沿用 Python 那套（前端 STEP_CLS 直接可用）
_LABELS = dict(STEP_LABELS)
_LABELS.update({"throw": "抛错", "case": "分支", "default": "默认", "export": "导出", "new": "构造"})

_KEYWORD_KIND: tuple[tuple[str, str], ...] = (
    ("else if", "elif"),
    ("else", "else"),
    ("if", "if"),
    ("for each", "for"),
    ("for", "for"),
    ("while", "while"),
    ("do", "while"),
    ("switch", "match"),
    ("case", "case"),
    ("default:", "default"),
    ("try", "try"),
    ("catch", "except"),
    ("finally", "finally"),
    ("return", "return"),
    ("throw", "raise"),
    ("await", "await"),
    ("yield", "yield"),
    ("assert", "assert"),
    ("break", "break"),
    ("continue", "continue"),
    ("delete", "del"),
    ("class ", "nested"),
    ("function", "nested"),
    ("export", "export"),
    ("import", "import"),
    ("const ", "assign"),
    ("let ", "assign"),
    ("var ", "assign"),
)

_CALL_RE = re.compile(r"(?:[\w$]+\.)*([A-Za-z_$][\w$]*)\s*\(")
_ASSIGN_RE = re.compile(r"^[A-Za-z_$][\w$.[\]'\"-]*\s*(?::[^=]+)?=($|[^=])")
_AUG_RE = re.compile(r"\+\+|--|\+=|-=|\*=|/=")
_ARROW_RE = re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[:=][^=]*=>")
_FUNC_RE = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")
_METHOD_RE = re.compile(r"^(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")

#: 行首是这些 → 它是上一句的续行（链式 `.replace(...)`、跨行三元、`&&` 连接），
#: 不能算成新语句。真机例：`html = html` 后面跟 6 行 `.replace(...)`，
#: 不合并就会把一条赋值拆成 7 条"表达式"步骤。
_CONTINUATION_RE = re.compile(r"^(?:[.?:]|\|\||&&|\+|\*|/|%)")

#: 不是"调用"的关键字
_NOT_CALLS = {
    "if", "for", "while", "switch", "catch", "return", "function", "typeof", "new",
    "await", "yield", "throw", "delete", "in", "of", "do", "else", "case", "super",
    "constructor", "get", "set", "async", "void", "instanceof",
}


def supports(rel: str) -> bool:
    """这个文件走"括号扫描"这条路吗（Vue 走它的 <script> 段）。"""
    return str(rel or "").lower().endswith(BRACE_EXTS)


# ---------------------------------------------------------------------------
# 预处理：把注释与字符串换成空白（保留长度与括号位置）
# ---------------------------------------------------------------------------
def scrub_lines(lines: list[str]) -> list[str]:
    """注释 → 空白、字符串 → 空白。跨行状态（块注释 / 模板串）在函数内维护。

    这一步是"括号计数对不对"的全部前提：`const s = "{ }"` 和 `// }` 都不能算进层级。
    """
    out: list[str] = []
    in_block = False
    quote: str | None = None
    for raw in lines:
        chars: list[str] = []
        i = 0
        n = len(raw)
        while i < n:
            ch = raw[i]
            nxt = raw[i + 1] if i + 1 < n else ""
            if in_block:
                if ch == "*" and nxt == "/":
                    in_block = False
                    chars.append("  ")
                    i += 2
                    continue
                chars.append(" ")
                i += 1
                continue
            if quote:
                if ch == "\\":
                    chars.append("  ")
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                    chars.append(" ")
                    i += 1
                    continue
                chars.append(" ")
                i += 1
                continue
            if ch == "/" and nxt == "/":
                break  # 行注释：后面整段丢掉
            if ch == "/" and nxt == "*":
                in_block = True
                chars.append("  ")
                i += 2
                continue
            if ch in "\"'`":
                quote = ch
                chars.append(" ")
                i += 1
                continue
            chars.append(ch)
            i += 1
        out.append("".join(chars))
    return out


def _depth_of(code: str) -> int:
    return code.count("{") - code.count("}")


def _leading_closes(stripped: str) -> int:
    n = 0
    for ch in stripped:
        if ch == "}":
            n += 1
        elif ch in " \t":
            continue
        else:
            break
    return n


def _classify(stripped: str, prev_kind: str) -> tuple[str, str]:
    """行首 → (kind, 标签)。认不出来就当"表达式/调用"。"""
    text = stripped.lstrip("} \t")
    if not text:
        return "unknown", _LABELS["unknown"]
    if text.startswith("else if") or text.startswith("else  if"):
        return "elif", _LABELS["elif"]
    for prefix, kind in _KEYWORD_KIND:
        if text.startswith(prefix):
            # `const x = () => {` 是内嵌函数，不是普通赋值
            if kind == "assign" and _ARROW_RE.match(text):
                return "nested", _LABELS["nested"]
            if kind == "nested" and _METHOD_RE.match(text) and prev_kind not in ("call", "assign"):
                return "nested", _LABELS["nested"]
            return kind, _LABELS.get(kind, kind)
    if _ARROW_RE.match(text):
        return "nested", _LABELS["nested"]
    if _AUG_RE.search(text) and _ASSIGN_RE.match(text.replace("++", "=").replace("--", "=")):
        return "augassign", _LABELS["augassign"]
    if _ASSIGN_RE.match(text):
        return "assign", _LABELS["assign"]
    if text.endswith("{") and _METHOD_RE.match(text):
        return "nested", _LABELS["nested"]
    if _CALL_RE.match(text):
        return "call", _LABELS["call"]
    if text.startswith("return") or text.startswith("("):
        return "expr", _LABELS["expr"]
    return "expr", _LABELS["expr"]


def _calls_in(code: str) -> list[str]:
    """这一行里调用了哪些名字（`foo(` 与 `obj.foo(` 都算，`foo` 取最后一段）。"""
    out: list[str] = []
    for match in _CALL_RE.finditer(code):
        name = match.group(1)
        if name and name not in _NOT_CALLS:
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# 主体：给一个函数符号产出一串有序步骤
# ---------------------------------------------------------------------------
def brace_logic(index: Any, *, read_text: Callable[[str], str], symbol_id: str) -> dict:
    """一个非 Python 函数的实现步骤 + 调用关系。拿不准就如实不可用。"""
    row = index.symbol(symbol_id)
    if row is None:
        raise LogicError(f"符号不存在：{symbol_id}")
    rel = str(row.get("path") or "")
    line = int(row.get("line") or 0)
    end_line = int(row.get("end_line") or line)
    base = {
        "symbol_id": symbol_id,
        "name": row.get("name") or "",
        "kind": row.get("kind") or "",
        "path": rel,
        "line": line,
        "end_line": end_line,
        "confidence": "medium",
        # 前端看到 medium 会显示"启发式解析"，这是刻意的
        "parser": "brace-scan",
    }
    if not supports(rel):
        return {
            **base,
            "available": False,
            "reason": f"{rel.rsplit('.', 1)[-1] or '这个类型'} 既不是 Python 也不是大括号语言，没有可用的步骤解析",
            "steps": [],
            "calls": [],
            "callers": [],
            "counts": {},
        }

    try:
        source = read_text(rel)
    except Exception as exc:  # noqa: BLE001
        raise LogicError(f"文件读不到：{rel}（{exc}）") from exc

    lines = source.split("\n")
    if line < 1 or line > len(lines):
        return {
            **base, "available": False, "reason": "符号行号超出文件范围（可能刚被改动，索引稍后刷新）",
            "steps": [], "calls": [], "callers": [], "counts": {},
        }
    body = lines[line - 1: end_line if end_line >= line else line]
    if not body:
        return {
            **base, "available": False, "reason": "取不到函数体",
            "steps": [], "calls": [], "callers": [], "counts": {},
        }

    scrubbed = scrub_lines(body)
    # 函数体起点：签名行里第一个 `{` 之后才是 body（`=>` 单表达式函数没有 `{`）
    steps: list[dict] = []
    paren = 0
    bracket = 0
    depth = 0
    body_started = "{" not in scrubbed[0] and "=>" not in body[0]
    #: 函数自己的 `{` 会把层级抬 1——步骤深度统一减掉它，让"函数体第一层"= 0（缩进从 0 开始）
    body_base: int | None = 0 if body_started else None
    prev_kind = ""
    counts = {"steps": 0, "branches": 0, "loops": 0, "returns": 0, "raises": 0}
    truncated = False
    call_names: list[str] = []

    for i, code in enumerate(scrubbed):
        raw_line = i + line
        raw_text = body[i].strip()
        stripped = code.strip()
        start_paren = paren
        start_bracket = bracket
        lead_close = _leading_closes(stripped)
        step_depth = max(0, min(MAX_DEPTH, depth - lead_close - (body_base or 0)))

        if i == 0 and not body_started:
            # 第一行是 `function f(a) {` 或 `(a) => {`：不产生步骤，只记账
            pass
        elif stripped and start_paren <= 0 and start_bracket <= 0:
            if _CONTINUATION_RE.match(stripped) and steps:
                # 续行：并进上一条，别把一条语句拆成七八条
                prev = steps[-1]
                if len(prev["text"]) < 200:
                    prev["text"] = f"{prev['text']} {raw_text}"[:200]
                prev["end_line"] = raw_line
                prev["calls"].extend(_calls_in(code))
            else:
                kind, label = _classify(stripped, prev_kind)
                if kind not in ("unknown",):
                    prev_kind = kind
                    if len(steps) < MAX_STEPS:
                        steps.append(
                            {
                                "kind": kind,
                                "label": label,
                                "line": raw_line,
                                "end_line": raw_line,
                                "depth": step_depth,
                                "text": raw_text[:140],
                                "calls": _calls_in(code),
                                "names": [],
                                "condition": "",
                                "children": [],
                            }
                        )
                        counts["steps"] += 1
                        if kind in ("if", "elif", "else", "case", "default", "match"):
                            counts["branches"] += 1
                        elif kind in ("for", "while"):
                            counts["loops"] += 1
                        elif kind == "return":
                            counts["returns"] += 1
                        elif kind in ("throw", "raise"):
                            counts["raises"] += 1
                    else:
                        truncated = True
        call_names.extend(_calls_in(code))
        paren += code.count("(") - code.count(")")
        bracket += code.count("[") - code.count("]")
        depth += _depth_of(code)
        if i == 0 and body_base is None:
            body_base = depth          # 签名行结束时函数体已经在 `{` 里面了

    # 名字 → 索引里的符号（同名多处定义不认，避免乱连）
    by_name: dict[str, list[str]] = {}
    for other in index.all_symbols():
        name = str(other["name"] or "")
        if name:
            by_name.setdefault(name, []).append(other["id"])
    seen: set[str] = set()
    calls: list[dict] = []
    unresolved: list[str] = []
    for name in call_names:
        if name in seen:
            continue
        seen.add(name)
        ids = by_name.get(name) or []
        if len(ids) == 1:
            target = index.symbol(ids[0]) or {}
            calls.append(
                {
                    "id": ids[0],
                    "name": name,
                    "path": target.get("path") or "",
                    "line": int(target.get("line") or 0),
                    "kind": target.get("kind") or "",
                }
            )
        elif not ids:
            unresolved.append(name)
    calls.sort(key=lambda c: (c["path"], c["line"]))

    callers = _callers(index, read_text=read_text, rel=rel, name=str(row.get("name") or ""))

    return {
        **base,
        "available": True,
        "steps": steps[:MAX_STEPS],
        "calls": calls[:60],
        "unresolved_calls": unresolved[:40],
        "callers": callers,
        "counts": counts,
        "truncated": truncated or len(steps) > MAX_STEPS,
        "note": "按行 + 花括号层级扫描的启发式结果：多行表达式与链式调用的续行已并入上一条语句，"
                "但嵌套模板字符串、switch 穿透仍可能被切错；字符串与注释已被剔除，"
                "所以花括号层级本身是可靠的",
        "basis": "源码按行扫描 + 注释/字符串剔除 + 花括号层级 + 行首关键字分类（非 AST）",
    }


def _callers(index: Any, *, read_text: Callable[[str], str], rel: str, name: str) -> list[dict]:
    """谁调了它：候选集 = 导入了本文件的文件 + 同目录文件，按 `name(` 文本匹配。"""
    if not name:
        return []
    import posixpath

    from code_agent.codeintel.store import file_id

    candidates: list[str] = []
    for edge in index.edges(kind="imports", dst=file_id(rel)):
        src = str(edge["src"]).replace("file:", "")
        if src and src != rel and src not in candidates:
            candidates.append(src)
    folder = posixpath.dirname(rel)
    for row in index.query_all("SELECT DISTINCT path FROM files WHERE path LIKE ? LIMIT 40", (f"{folder}/%",)):
        p = row["path"]
        if p != rel and p not in candidates:
            candidates.append(p)
    candidates = candidates[:24]

    pat = re.compile(rf"(?<![\w$]){re.escape(name)}\s*\(")
    out: list[dict] = []
    for cand in candidates:
        try:
            other = read_text(cand)
        except Exception:  # noqa: BLE001
            continue
        if len(other) > MAX_CANDIDATE_BYTES:
            continue
        for idx, text in enumerate(other.split("\n"), start=1):
            if pat.search(text):
                out.append({"path": cand, "line": idx})
                break
    return out[:20]
