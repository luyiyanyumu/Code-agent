"""解析器入口（M18 · P1）：按语言分派，并**统一结果契约**。

统一契约（store / graph / service 都依赖它，改这里等于改数据面）：
```python
{
  "symbols": [ {id, kind, name, qname, line, end_line, parent, confidence,
                signature, doc, params, returns, decorators, bases, complexity,
                route_method?, route_path?, props?, emits?, extra?} ],
  "imports": [ {module, level, names, raw} ],
  "entrypoint": bool,
  "error": str,          # 解析失败原因（该文件退化为"只有文件节点"）
  "confidence": "high" | "medium" | "low",
}
```

语言 → 解析器映射刻意收窄：Python 用 `ast`（精确），JS/TS/Vue 用启发式，
Java 用保守声明词法索引（完整成员/重载身份，不声称编译器类型语义），
其余走"只看骨架"的降级解析器。**不认识的扩展名不猜**——直接返回空结构，
让地图上只出现文件节点（宁可少画，不要画错）。
"""

from __future__ import annotations

from typing import Any

from code_agent.codeintel.parsers import generic_parser, java_parser, js_parser, python_parser

#: 语言 → 解析器（键是 projects.detect_language 的取值）
PARSERS = {
    "python": ("ast", python_parser.parse),
    "javascript": ("heuristic", js_parser.parse),
    "typescript": ("heuristic", js_parser.parse),
    "vue": ("heuristic", js_parser.parse),
    "svelte": ("heuristic", js_parser.parse),
    "go": ("generic", generic_parser.parse),
    "rust": ("generic", generic_parser.parse),
    "java": (java_parser.STRATEGY, java_parser.parse),
    "c": ("generic", generic_parser.parse),
    "cpp": ("generic", generic_parser.parse),
    "csharp": ("generic", generic_parser.parse),
    "kotlin": ("generic", generic_parser.parse),
    "swift": ("generic", generic_parser.parse),
    "scala": ("generic", generic_parser.parse),
    "dart": ("generic", generic_parser.parse),
    "php": ("generic", generic_parser.parse),
    "ruby": ("generic", generic_parser.parse),
    "lua": ("generic", generic_parser.parse),
    "zig": ("generic", generic_parser.parse),
}

#: 只能拿到"文件"级别信息的语言（不解析符号，避免编造）
FILE_ONLY_LANGS = frozenset({"json", "yaml", "toml", "ini", "markdown", "text", "csv", "sql", "xml",
                             "html", "css", "scss", "less", "shell", "powershell", "bat", "makefile",
                             "dockerfile", "proto", "terraform", "plaintext"})

_EMPTY: dict[str, Any] = {"symbols": [], "imports": [], "entrypoint": False, "error": "", "confidence": "high"}


def parser_for(lang: str) -> tuple[str, Any] | None:
    return PARSERS.get(lang)


def is_strategy_current(lang: str, strategy: str) -> bool:
    """Force one normal incremental rebuild after a parser strategy upgrade.

    File-only languages and skipped files have no parser to migrate. This check
    does not read files or itself start a repository scan.
    """
    entry = PARSERS.get(lang)
    return entry is None or strategy == entry[0]


def parse_file(rel_path: str, lang: str, text: str) -> dict[str, Any]:
    """解析一个文件。任何异常都在这里兜住——单文件失败不能拖垮整次扫描。"""
    entry = PARSERS.get(lang)
    if entry is None:
        return dict(_EMPTY)
    strategy, func = entry
    try:
        result = func(rel_path, text)
    except Exception as exc:  # noqa: BLE001 —— 解析器 bug 只能降级，不能中断扫描
        result = dict(_EMPTY)
        result["error"] = f"parser[{strategy}]: {type(exc).__name__}: {exc}"
        result["confidence"] = "low"
        result["strategy"] = strategy
        return result
    result.setdefault("symbols", [])
    result.setdefault("imports", [])
    result.setdefault("entrypoint", False)
    result.setdefault("error", "")
    result.setdefault("confidence", "high")
    result.setdefault("module_doc", "")
    result["strategy"] = strategy
    return result


__all__ = ["PARSERS", "FILE_ONLY_LANGS", "parse_file", "parser_for", "is_strategy_current"]
