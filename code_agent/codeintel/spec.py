"""测试即规格（M18 · P7）：行为卡片 + 覆盖率上地图。

**为什么把测试当规格看**：用户真正想确认的不是"有没有测试文件"，而是
"**我关心的这条行为，有没有一条可执行的断言在守着它**"。于是这个模块把索引里的
测试符号整理成**行为卡片**（一条测试 = 一条可读的行为声明），并给出**覆盖率**。

覆盖率的判定必须诚实（否则就是个好看的数字）：

| 状态 | 判据 | 含义 |
|---|---|---|
| `direct` | 测试文件与符号之间有引用边（`referenced_by`，启发式） | 这条符号**很可能**被断言过 |
| `file` | 符号所在文件被测试文件 import，或测试文件按命名约定命中 | 该文件有测试，但**不保证**具体到这条符号 |
| `none` | 以上都没有 | 没有找到任何守着它的测试 |

`file` 与 `direct` 分开报，而不是合并成"覆盖"：把"文件级覆盖"说成"这条函数被测试了"
是常见的自欺——界面与文档都按三态显示。

刻意不做：不解析断言的内容（`assert x == 1` 想表达什么只有人能判断），
不按行覆盖率反推（那需要插桩执行，不是静态索引能给的），不把"行为卡片"写成
自动生成的测试文档（那是另一种自欺）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from code_agent.codeintel import graph
from code_agent.codeintel.impact import is_test_path, related_tests_many

#: 一次最多返回多少张行为卡片（文档导出与界面都用同一个上限）
MAX_BEHAVIORS = 500
#: 覆盖率计算最多看多少符号
MAX_COVERAGE_SYMBOLS = 20000
#: 未覆盖清单最多列多少条
MAX_UNCOVERED = 200
#: 单文件读取上限（数断言用，只读测试文件）
MAX_FILE_BYTES = 512 * 1024

#: 测试符号的命名约定（Python / JS / 常见 BDD）
_TEST_NAME_RE = re.compile(
    r"^(test_|test[A-Z]|it_|spec_|should_)|^it$|^describe$|^test$", re.IGNORECASE
)
#: `*.spec.ts` / `*.test.js` 这类文件里，函数本身就是行为（名字不叫 test_ 也算）
_SPEC_FILE_RE = re.compile(r"\.(test|spec)\.\w+$", re.IGNORECASE)
#: 断言的样子（只看"有没有"，不判断内容）
_ASSERT_RE = re.compile(
    r"\bassert\b|\bexpect\s*\(|\.to(Be|Equal|Match|Contain|Throw)|assertEqual|assertTrue|"
    r"assertRaises|pytest\.raises|self\.assert",
    re.IGNORECASE,
)
#: 源码里的标识符（用于"测试文件到底提到了哪些符号"）
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: 参与的符号名最短长度（太短的名字误报率极高：`add`、`run`、`get` 到处都是）
MIN_COVER_NAME = 3


def is_test_symbol(row: dict) -> bool:
    """这个符号看起来是一条测试/行为声明吗（先看文件，再看名字）。"""
    path = str(row.get("path") or "")
    if not is_test_path(path):
        return False
    if str(row.get("kind") or "") not in ("function", "method", "variable", "class"):
        return False
    if _SPEC_FILE_RE.search(path):
        return True                       # spec/test 文件里的函数天然是行为
    return bool(_TEST_NAME_RE.search(str(row.get("name") or "")))


def humanize(name: str) -> str:
    """`test_add_negative_numbers` → `add negative numbers`（给人读的行为描述）。"""
    text = re.sub(r"^(test_|spec_|it_|should_)", "", str(name or ""))
    text = re.sub(r"^test(?=[A-Z])", "", text)
    text = text.replace("_", " ").strip()
    return text or str(name or "")


def count_asserts(lines: list[str]) -> int:
    return sum(1 for line in lines if _ASSERT_RE.search(line))


def mentioned_symbols(text: str, names: dict[str, list[str]]) -> list[str]:
    """测试文件里**按词边界**提到过的符号 id。

    `names` 是 符号名 → 候选符号 id 列表。重名（`main` 在五个文件里都有）只在
    名字全局唯一时才认；否则"提到了 main"根本指不出是哪一条——宁可漏，不可错。
    """
    words = set(_WORD_RE.findall(text or ""))
    out: list[str] = []
    for word in words:
        ids = names.get(word)
        if ids and len(ids) == 1:
            out.append(ids[0])
    return out


def name_index(rows: list[dict]) -> dict[str, list[str]]:
    """符号名 → 符号 id 列表（只收够长的名字）。"""
    names: dict[str, list[str]] = {}
    for row in rows:
        name = str(row.get("name") or "")
        if len(name) < MIN_COVER_NAME or name.startswith("_"):
            continue
        names.setdefault(name, []).append(row["id"])
    return names


def imported_files(index: Any, rel: str) -> list[str]:
    return sorted(
        str(e["dst"]).replace("file:", "")
        for e in index.edges(kind="imports", src=f"file:{rel}")
    )


def _read_lines(read_text: Callable[[str], str], rel: str) -> list[str]:
    """读文件（带缓存的调用方自己管缓存）；读不到就返回空列表。"""
    try:
        text = read_text(rel)
    except Exception:  # noqa: BLE001 —— 读不到不该让行为卡片整体失败
        return []
    if len(text) > MAX_FILE_BYTES:
        text = text[:MAX_FILE_BYTES]
    return text.split("\n")


def behaviors(
    index: Any,
    *,
    read_text: Callable[[str], str],
    path: str = "",
    module: str = "",
    target: str = "",
    limit: int = MAX_BEHAVIORS,
) -> dict:
    """行为卡片：每条测试函数的意图、断言密度、它盯着谁。

    `target` 可以传符号 id 或文件路径，用于"谁在保护这段代码"（反向查）。
    """
    prefix = f"{module.strip('/')}/" if module else ""
    rows = index.all_symbols(limit=MAX_COVERAGE_SYMBOLS)
    tests = [row for row in rows if is_test_symbol(row)]
    if path:
        tests = [row for row in tests if row["path"] == path]
    if prefix:
        tests = [row for row in tests if str(row["path"]).startswith(prefix)]

    names = name_index(rows)
    test_files = sorted({row["path"] for row in tests})
    cache: dict[str, list[str]] = {}
    targets_by_file: dict[str, dict[str, list[str]]] = {}
    for rel in test_files:
        cache[rel] = _read_lines(read_text, rel)
        text = "\n".join(cache[rel])
        targets_by_file[rel] = {
            "symbols": mentioned_symbols(text, names),
            "files": imported_files(index, rel),
        }

    if target:
        wanted = str(target)
        wanted_file = wanted.replace("file:", "").strip("/")
        tests = [
            row for row in tests
            if wanted in (targets_by_file.get(row["path"], {}).get("symbols") or [])
            or wanted_file in (targets_by_file.get(row["path"], {}).get("files") or [])
        ]

    cards: list[dict] = []
    for row in tests[: max(1, int(limit))]:
        rel = row["path"]
        lines = cache.get(rel) or _read_lines(read_text, rel)
        start = max(1, int(row["line"] or 1))
        end = max(start, int(row["end_line"] or start))
        body = lines[start - 1 : end]
        targets = targets_by_file.get(rel, {"symbols": [], "files": []})
        cards.append(
            {
                "id": row["id"],
                "name": row.get("name") or "",
                "behavior": humanize(row.get("name") or ""),
                "path": rel,
                "line": start,
                "end_line": end,
                "doc": (row.get("doc") or "")[:300],
                "asserts": count_asserts(body),
                "lines": max(0, end - start + 1),
                "confidence": row.get("confidence") or "high",
                "covers_symbols": sorted(set(targets["symbols"]))[:20],
                "covers_files": sorted(set(targets["files"]))[:20],
            }
        )
    return {
        "cards": cards,
        "total": len(tests),
        "truncated": len(tests) > len(cards),
        "no_asserts": len([c for c in cards if c["asserts"] == 0]),
        "method": "符号名在测试文件里按词边界出现（重名不认），文件按 import 边与命名约定",
    }


def coverage(
    index: Any,
    *,
    read_text: Callable[[str], str] | None = None,
    files: list[str] | None = None,
    limit: int = MAX_COVERAGE_SYMBOLS,
) -> dict:
    """覆盖率：三态（直接 / 文件级 / 无），按文件与模块汇总，并列出未覆盖的公共面。

    `direct` 的判据是"**测试文件里按词边界提到过这个符号名**（且名字全局唯一）"，
    比"签名里恰好含这个名字"强得多，也说得清。没有 `read_text` 时退化为只按
    文件级判定（调用方拿不到源文件时的诚实降级）。
    """
    from code_agent.codeintel.impact import is_public_api

    all_files = files or sorted(index.file_rows())
    all_files = [str(f).replace("\\", "/").strip("/") for f in all_files][:limit]
    known = set(all_files)
    symbols = [row for row in index.all_symbols(limit=limit) if row["path"] in known]
    names = name_index(symbols)

    direct: dict[str, list[str]] = {}
    test_files = [rel for rel in all_files if is_test_path(rel)]
    for rel in test_files:
        if read_text is None:
            continue
        text = "\n".join(_read_lines(read_text, rel))
        for sid in mentioned_symbols(text, names):
            direct.setdefault(sid, []).append(rel)

    tests_for: dict[str, list[str]] = {}
    for rel, hits in related_tests_many(index, all_files).items():
        if hits:
            tests_for[rel] = hits

    by_file: dict[str, dict] = {}
    for row in symbols:
        rel = row["path"]
        bucket = by_file.setdefault(
            rel,
            {"path": rel, "module": graph.module_of(rel), "symbols": 0, "direct": 0, "file": 0,
             "none": 0, "tests": tests_for.get(rel, [])},
        )
        bucket["symbols"] += 1
        if direct.get(row["id"]):
            bucket["direct"] += 1
        elif tests_for.get(rel):
            bucket["file"] += 1
        else:
            bucket["none"] += 1

    # 文件级三态：只有直接覆盖的符号才算 direct；否则看该文件有没有测试
    for rel, bucket in by_file.items():
        if bucket["direct"]:
            bucket["state"] = "direct"
        elif tests_for.get(rel):
            bucket["state"] = "file"
        else:
            bucket["state"] = "none"

    uncovered: list[dict] = []
    direct_symbols: dict[str, list[str]] = {}
    for row in symbols:
        tests = direct.get(row["id"])
        if tests:
            direct_symbols[row["id"]] = sorted(set(tests))[:5]
            continue
        if tests_for.get(row["path"]):
            continue
        public, why = is_public_api(row)
        if public and len(uncovered) < MAX_UNCOVERED:
            summary = graph.symbol_summary(row)
            summary["why_public"] = why
            uncovered.append(summary)

    modules: dict[str, dict] = {}
    for bucket in by_file.values():
        mod = modules.setdefault(bucket["module"], {"module": bucket["module"], "files": 0, "symbols": 0,
                                                      "covered": 0})
        mod["files"] += 1
        mod["symbols"] += bucket["symbols"]
        mod["covered"] += bucket["direct"] + bucket["file"]

    total_symbols = sum(b["symbols"] for b in by_file.values())
    covered = sum(b["direct"] + b["file"] for b in by_file.values())
    test_files = sorted({t for hits in tests_for.values() for t in hits})
    return {
        "totals": {
            "files": len(all_files),
            "files_with_tests": len(tests_for),
            "symbols": total_symbols,
            "direct": sum(b["direct"] for b in by_file.values()),
            "file_level": sum(b["file"] for b in by_file.values()),
            "uncovered": sum(b["none"] for b in by_file.values()),
            "ratio": round(covered / total_symbols, 3) if total_symbols else 0.0,
            "test_files": len(test_files),
        },
        "by_module": sorted(modules.values(), key=lambda m: (-m["symbols"], m["module"]))[:100],
        "files": sorted(by_file.values(), key=lambda b: b["path"])[:2000],
        "test_files": test_files[:300],
        "direct_symbols": direct_symbols,
        "uncovered": uncovered,
        "truncated": len(all_files) >= limit or len(symbols) >= limit,
        "method": (
            "直接=符号名在测试文件里按词边界出现（重名不认）；"
            "文件级=被测试文件 import 或命名约定命中"
        ),
    }
