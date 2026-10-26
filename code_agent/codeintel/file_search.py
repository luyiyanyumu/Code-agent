"""只读文件目录与文本查找：从磁盘取现状，不建立索引或启动语言服务。"""

from __future__ import annotations

import fnmatch
import re
from bisect import bisect_left
from pathlib import Path

from code_agent.codeintel import paths as P
from code_agent.codeintel.scanner import MAX_FILES, MAX_PARSE_BYTES, walk_project

MAX_QUERY_CHARS = 512
MAX_CATALOG_RESULTS = 2000
MAX_SEARCH_RESULTS = 1000
MAX_SEARCH_FILES = 5000
MAX_SEARCH_BYTES = 32 * 1024 * 1024
MAX_RESULT_LINE_CHARS = 500


def _query(value: str, *, required: bool = False) -> str:
    if len(value) > MAX_QUERY_CHARS:
        raise ValueError(f"query 不能超过 {MAX_QUERY_CHARS} 个字符")
    if required and not value:
        raise ValueError("query 不能为空")
    if any(ch in value for ch in ("\x00", "\n", "\r")):
        raise ValueError("query 仅支持单行文本")
    return value


def _limit(value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit 必须在 1 到 {maximum} 之间")
    return value


def _include(value: str) -> str:
    pattern = str(value or "").strip().replace("\\", "/")
    if len(pattern) > MAX_QUERY_CHARS or any(ch in pattern for ch in ("\x00", "\n", "\r")):
        raise ValueError("include 仅支持不超过 512 字符的相对路径模式")
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:", pattern) or ".." in pattern.split("/"):
        raise P.ProjectPathError("include 只接受项目内相对路径模式，不允许 .. 或绝对路径")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    return pattern


def _matches_include(path: str, pattern: str) -> bool:
    if not pattern:
        return True
    if fnmatch.fnmatchcase(path, pattern):
        return True
    # **/ 也应匹配零层目录，允许 **/*.vue 命中根目录 App.vue。
    return pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])


def _inventory(root: Path, *, path_query: str = "", include: str = ""):
    root = Path(root)
    if not root.is_absolute() or not root.is_dir():
        raise P.ProjectPathError("项目目录不可用")
    inventory = walk_project(root)
    entries = []
    for entry in inventory.files:
        if entry.skipped:
            continue
        if path_query and path_query not in entry.rel.casefold():
            continue
        if not _matches_include(entry.rel, include):
            continue
        try:
            target = P.safe_child(root, entry.rel)
            with target.open("rb") as stream:
                if P.is_probably_binary(stream.read(8192)):
                    continue
        except (OSError, P.ProjectPathError):
            continue
        entries.append(entry)
    entries.sort(key=lambda entry: (entry.rel.casefold(), entry.rel))
    return root, inventory, entries


def files_catalog(root: Path, *, query: str = "", limit: int = 500) -> dict:
    """可打开的文件列表；total 是有界 inventory 内的匹配总数。"""
    query = _query(query).casefold()
    limit = _limit(limit, MAX_CATALOG_RESULTS)
    _, inventory, matches = _inventory(root, path_query=query)
    return {
        "files": [{"path": entry.rel, "lang": entry.lang, "size": entry.size} for entry in matches[:limit]],
        "total": len(matches),
        "truncated": inventory.truncated or len(matches) > limit,
        "inventory_truncated": inventory.truncated,
        "limit": limit,
        "inventory_limit": MAX_FILES,
    }


def search_text(
    root: Path, *, query: str, case_sensitive: bool = False, regex: bool = False,
    include: str = "", limit: int = 200,
) -> dict:
    """单行普通文本搜索。列为 1-based UTF-16 单位，直接对应 Monaco 光标。"""
    query = _query(query, required=True)
    limit = _limit(limit, MAX_SEARCH_RESULTS)
    if regex:
        raise ValueError("暂不支持正则搜索，请使用普通文本")
    pattern = _include(include)
    root, inventory, entries = _inventory(root, include=pattern)
    # 用户输入始终转义为字面量；没有任意正则或灾难性回溯。
    needle = re.compile(re.escape(query), flags=0 if case_sensitive else re.IGNORECASE)
    matches = []
    files_scanned = 0
    bytes_scanned = 0
    files_checked = 0
    reason = "inventory" if inventory.truncated else ""
    for entry in entries:
        if files_checked >= MAX_SEARCH_FILES:
            reason = "files"
            break
        if bytes_scanned + entry.size > MAX_SEARCH_BYTES:
            reason = "bytes"
            break
        files_checked += 1
        try:
            target = P.safe_child(root, entry.rel)
            with target.open("rb") as stream:
                data = stream.read(MAX_PARSE_BYTES + 1)
        except (OSError, P.ProjectPathError):
            continue
        bytes_scanned += len(data)
        if len(data) > MAX_PARSE_BYTES or P.is_probably_binary(data):
            continue
        if bytes_scanned > MAX_SEARCH_BYTES:
            reason = "bytes"
            break
        files_scanned += 1
        for line_number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            non_bmp = None
            for found in needle.finditer(line):
                if len(matches) >= limit:
                    reason = "matches"
                    break
                first = max(0, found.start() - 80)
                last = min(len(line), max(first + MAX_RESULT_LINE_CHARS, found.end()))
                if non_bmp is None:
                    non_bmp = [idx for idx, char in enumerate(line) if ord(char) > 0xFFFF]
                # 长行不糊满响应，返回预览偏移供前端高亮；跳转列仍对应源文件。
                column = found.start() + bisect_left(non_bmp, found.start()) + 1
                end_column = found.end() + bisect_left(non_bmp, found.end()) + 1
                matches.append({
                    "path": entry.rel, "line": line_number,
                    "column": column, "end_column": end_column,
                    "text": line[first:last],
                    "text_start_column": first + bisect_left(non_bmp, first) + 1,
                    "text_truncated": first > 0 or last < len(line),
                })
            if reason == "matches":
                break
        if reason == "matches":
            break
    return {
        "matches": matches,
        "truncated": bool(reason),
        "truncation_reason": reason,
        "inventory_truncated": inventory.truncated,
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "limit": limit,
        "limits": {"files": MAX_SEARCH_FILES, "bytes": MAX_SEARCH_BYTES, "file_bytes": MAX_PARSE_BYTES},
    }
