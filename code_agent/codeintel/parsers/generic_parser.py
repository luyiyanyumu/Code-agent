"""通用语言降级解析器（M18 · P1）：只保证"看得见结构"，不假装精确。

覆盖 java/c#/c/cpp/go/rust/kotlin/swift/php/ruby/scala/dart/lua…：
按每行正则捞**顶层**类型与函数（缩进 ≤2 视为顶层），置信度一律 low，
地图上以虚线样式呈现。这样做的价值是：一个 Java 仓库也能画出模块与类骨架，
代价是成员/参数基本没有；Java 的显式 import 单独保留，包通配符不猜目标。
其余语言仍不产出依赖边——标清楚比硬编造更有用。
"""

from __future__ import annotations

import re
from typing import Any

from code_agent.codeintel.parsers.js_parser import _LineState, _clean_line

#: (正则, kind)。分组 1 = 名字。按顺序匹配，先命中先算。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*(?:public|private|protected|internal|final|abstract|sealed|open|data|static|export)?\s*"
                r"(?:class|struct|record|union|trait|object|interface|enum\s+class|enum|protocol)\s+"
                r"([A-Za-z_][\w]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:pub\s+)?(?:func|fun|fn|def|function|sub|procedure)\s+"
                r"([A-Za-z_][\w]*)"), "function"),
    (re.compile(r"^\s*(?:public|private|protected|static|inline|virtual|override|extern|constexpr|\s)*"
                r"(?:[A-Za-z_][\w:<>,.\[\]?*&\s]{0,60}?)\s+([A-Za-z_][\w]*)\s*\([^;]*\)\s*(?:const)?\s*\{"), "function"),
    (re.compile(r"^\s*typedef\s+(?:struct|enum|union)?\s*\{?[^}]*\}\s*([A-Za-z_][\w]*)"), "type"),
    (re.compile(r"^\s*(?:export\s+)?(?:type|alias|typedef)\s+([A-Za-z_][\w]*)"), "type"),
)

#: 顶层判定：缩进超过这个宽度就不当顶层（避免把 if/for 内部误认成声明）
_MAX_TOP_INDENT = 2

_JAVA_IMPORT = re.compile(
    r"^\s*import\s+(static\s+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)(\.\*)?\s*;"
)


def parse(rel_path: str, text: str) -> dict[str, Any]:
    lines = text.split("\n")
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    import_state = _LineState()
    for idx, raw in enumerate(lines, start=1):
        import_line = _clean_line(raw, import_state)
        if rel_path.endswith(".java"):
            match = _JAVA_IMPORT.match(import_line)
            if match:
                static, module, wildcard = match.groups()
                if static and not wildcard:
                    module = module.rsplit(".", 1)[0]
                elif wildcard and not static:
                    module += ".*"  # 包通配符不猜依赖到哪个文件。
                imports.append({"module": module, "level": 0, "names": [], "raw": raw.strip()[:160]})
        stripped = raw.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#")):
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        if indent > _MAX_TOP_INDENT:
            continue
        for regex, kind in _PATTERNS:
            m = regex.match(raw)
            if not m:
                continue
            name = m.group(1)
            if name in ("if", "for", "while", "switch", "return", "new", "catch", "sizeof"):
                break
            symbols.append(
                {
                    "id": f"sym:{rel_path}#{name}",
                    "kind": kind,
                    "name": name,
                    "qname": name,
                    "line": idx,
                    "end_line": idx,
                    "parent": "",
                    "confidence": "low",
                    "signature": stripped[:200],
                    "doc": "",
                    "params": [],
                    "returns": "",
                    "decorators": [],
                    "bases": [],
                    "complexity": 0,
                }
            )
            break
    return {
        "symbols": symbols,
        "imports": imports,
        "entrypoint": False,
        "error": "",
        "confidence": "low",
    }
