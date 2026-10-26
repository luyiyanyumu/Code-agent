"""代码模式工具集（M18 · P2）：让模型能"精确定位 + 最小改动"，而不是整文件覆盖。

工具设计的三条硬约束（每条都对应一种真实的翻车方式）：

1. **read_file 必须支持行区间**：地图给出的坐标是 `文件:行-行`，模型要能只读那一段。
   整文件读取在大文件上既费 token 又容易被截断成半截。
2. **edit_file 只做唯一匹配替换**：`old_string` 命中 0 处报错并给出原因，命中多处
   报错并列出候选项——**绝不"猜一个位置改"**。整文件覆盖（write_file）留给新建文件。
3. **每次写入都落 patch 台账**：改前/改后内容 + sha1，用户可一键回滚。
   这是"AI 改代码可审查、可撤销"的物理保证，而不是靠提示词请求模型小心。

所有路径都过 `_WorkspaceBound`：工作区 = 项目根目录，越界一律拒绝（与 read/write 同一套边界）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Any

from code_agent.codeintel.paths import is_ignored_dir
from code_agent.tools.base import Tool, ToolContext, ToolError
from code_agent.tools.builtin.tools_builtin import _WorkspaceBound

#: grep 单次返回的最大匹配数
MAX_GREP_RESULTS = 80
#: grep 单文件扫描上限（超大文件不全扫）
MAX_GREP_BYTES = 2 * 1024 * 1024
#: 二进制探测窗口
SNIFF_BYTES = 8192
#: glob 单次返回的路径数（默认 / 硬上限）。沿用 DSH 的 100，
#: 超出的部分明确告诉模型"这是条数上限，不是文件不存在"并给出收窄建议。
MAX_GLOB_RESULTS = 100
MAX_GLOB_RESULTS_HARD = 400
MAX_SCAN_FILES = 4000
# 注：这里曾定义过一个 `PATCH_INLINE_LIMIT = 200 * 1024`（"超过就不存 patch 全文"），
# 但它全仓无人引用——真正生效的同名阈值是 `code_agent/codeintel/patches.py` 里的
# `PatchStore.INLINE_LIMIT`，由 `PatchStore.record()` 在写台账时自己判定（超限只存
# old/new 片段 + sha1，回滚走反向替换）。同一个语义留两份常量迟早会分叉：工具侧
# 若按自己的副本先裁一刀，台账反而会丢掉本可保全的全文。所以删掉这份死副本，
# 让"要不要存全文"只由台账一处决定。


def _dump_capped(
    payload: dict[str, Any], *, list_key: str, budget: int, hint: str
) -> str:
    """把工具载荷序列化进字符预算——**宁可不给条目，也绝不给半截 JSON**。

    为什么不能序列化后按字符切片（旧实现的写法）：切片是按字符剁的，
    会剁在字符串中间、括号中间，模型收到的是一段**语法都不合法**的工具结果，
    连一个字段都读不出来——比少几条更糟，而且模型会以为"这就是全部"。

    这里的做法：
    1. 先整体序列化，**没超预算就原样返回**（逐字节等于旧输出，不额外加任何键）；
    2. 超预算就只动 `payload[list_key]` 这个列表：二分出"能容纳的最大前缀"，
       丢弃尾部条目，并在载荷里加 `truncated` = `{"kept", "omitted", "hint"}`；
    3. 说明只讲两件事——**预算事实**（丢了几条）与**恢复动作**（怎么拿到剩下的）。
       绝不暗示上游数据不完整：被丢的条目在索引里都真实存在，只是这次没塞进来；
    4. 连一条都放不下时，列表退化为空、`truncated` 如实说明，结果仍是合法 JSON。

    `list_key` 由调用点按各自载荷形状给出（`symbols`/`members`/`children`/...）。
    若该键缺失或不是列表，不做裁剪，但仍返回合法 JSON 并说明"没能裁"。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) <= budget:
        return text

    base: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {"payload": payload}
    items = base.get(list_key)
    # 上游可能已经用过 `truncated` 这个键（如 code_graph 的"每跳已截断"布尔量）：
    # 覆盖别人的事实不诚实，先留住，稍后放进说明的 `upstream` 字段。
    upstream = base.get("truncated")

    def note_for(kept: int, omitted: int, extra: str = "") -> dict[str, Any]:
        if omitted:
            hint_text = f"结果超出 {budget} 字符预算，已省略尾部 {omitted} 条（保留前 {kept} 条）"
        else:
            hint_text = f"结果超出 {budget} 字符预算，且列表里没有条目可省略"
        if extra:
            hint_text += f"；{extra}"
        note: dict[str, Any] = {
            "kept": kept,
            "omitted": omitted,
            "hint": f"{hint_text}。恢复动作：{hint}",
        }
        if upstream:
            note["upstream"] = upstream
        return note

    if not isinstance(items, list):
        # 没有可裁剪的列表（键缺失/形状不符）：不猜、不崩，原样返回合法 JSON + 说明。
        base["truncated"] = note_for(
            0, 0, f"载荷里没有可裁剪的列表（{list_key} 缺失或不是列表），已原样返回完整 JSON"
        )
        return json.dumps(base, ensure_ascii=False, indent=2)

    def render(kept: int, note: dict[str, Any]) -> str:
        candidate = {**base, list_key: items[:kept], "truncated": note}
        return json.dumps(candidate, ensure_ascii=False, indent=2)

    # 二分：找最大的"能连同说明一起放进预算"的前缀长度
    kept = 0
    low, high = 0, len(items)
    while low <= high:
        mid = (low + high) // 2
        if len(render(mid, note_for(mid, len(items) - mid))) <= budget:
            kept = mid
            low = mid + 1
        else:
            high = mid - 1

    omitted = len(items) - kept
    extra = ""
    if kept == 0 and items:
        extra = "剩下的条目单条就放不进预算（截断说明本身也占字符），已全部省略"
    elif kept == 0:
        extra = "该列表本身已为空，超出预算的是非列表字段"
    final_note = note_for(kept, omitted, extra)
    result = render(kept, final_note)
    if len(result) > budget:
        # 连"列表清空 + 说明"都放不下：说明非列表字段自身过大。宁可超预算返回，
        # 也绝不返回半截 JSON——合法性优先于预算。
        final_note["hint"] += (
            "；注意：该列表清空后整体仍超出预算（非列表字段自身过大），"
            "这里返回的是完整合法 JSON，请改用更具体的查询缩小非列表部分"
        )
        result = render(kept, final_note)
    return result


def _iter_files(
    root: Path, start: Path, *, max_files: int = MAX_SCAN_FILES, cancelled: ThreadEvent | None = None,
) -> list[Path]:
    """Honor file scope; prune ignored trees and stop at the scan budget."""
    out: list[Path] = []
    is_directory = start.is_dir()
    base = start if is_directory else start.parent
    try:
        parts = base.relative_to(root).parts
    except ValueError:
        return out
    if max_files <= 0 or any(part.startswith(".") or is_ignored_dir(part) for part in parts):
        return out

    def stopped() -> bool:
        return len(out) >= max_files or bool(cancelled and cancelled.is_set())

    if not is_directory:
        # A literal file must never widen into its parent tree. Keep the same
        # workspace/link boundary as the directory walker, including for callers
        # that have not already resolved their input with _WorkspaceBound.
        try:
            if not stopped() and start.is_file():
                start.resolve().relative_to(root)
                out.append(start)
        except (OSError, ValueError, RuntimeError):
            pass
        return out

    def walk(directory: Path) -> None:
        if stopped():
            return
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            if stopped():
                return
            try:
                path = Path(entry.path)
                # Windows junctions can point outside the workspace or back to
                # an ancestor. Do not follow them or directory symlinks.
                if getattr(entry, "is_junction", lambda: False)():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not entry.name.startswith(".") and not is_ignored_dir(entry.name):
                        walk(path)
                elif entry.is_file():
                    if entry.is_symlink():
                        try:
                            path.resolve().relative_to(root)
                        except (OSError, ValueError, RuntimeError):
                            continue
                    out.append(path)
            except OSError:
                continue

    walk(base)
    return out


class _FileQueryTool(Tool):
    """Keep filesystem traversal off the server loop and stop it on cancellation."""

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        cancelled = ThreadEvent()
        try:
            return await asyncio.to_thread(self._execute, arguments, ctx, cancelled)
        finally:
            cancelled.set()


class ListDirTool(_FileQueryTool):
    name = "list_dir"
    description = (
        "List files and directories under a path (relative to the project root). "
        "Ignored dirs (.git/node_modules/.venv/...) are skipped. Read-only."
    )
    description_zh = "列出目录内容（相对项目根）。跳过 .git/node_modules/.venv 等忽略目录，只读。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path relative to workspace (default '.')"},
            "depth": {"type": "integer", "description": "How deep to list (default 1, max 4)"},
            "max_entries": {"type": "integer", "description": "Max entries to return (default 200)"},
        },
    }

    def _execute(self, arguments: dict[str, Any], ctx: ToolContext, cancelled: ThreadEvent) -> str:
        raw = str(arguments.get("path") or ".").strip() or "."
        depth = max(1, min(int(arguments.get("depth") or 1), 4))
        max_entries = max(1, min(int(arguments.get("max_entries") or 200), 1000))
        root = Path(ctx.workspace).resolve()
        target = _WorkspaceBound.resolve_read(ctx.workspace, raw)
        if not target.exists():
            raise ToolError(f"目录不存在: {raw}")
        if not target.is_dir():
            raise ToolError(f"不是目录: {raw}")

        lines: list[str] = []
        truncated = False

        def walk(directory: Path, level: int) -> None:
            nonlocal truncated
            if cancelled.is_set() or level > depth or len(lines) >= max_entries:
                return
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError as exc:
                lines.append(f"{'  ' * (level - 1)}! 无法读取: {exc}")
                return
            for entry in entries:
                if cancelled.is_set():
                    return
                if len(lines) >= max_entries:
                    truncated = True
                    return
                name = entry.name
                if entry.is_dir():
                    if name.startswith(".") or is_ignored_dir(name):
                        continue
                    lines.append(f"{'  ' * (level - 1)}{name}/")
                    walk(entry, level + 1)
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{'  ' * (level - 1)}{name}  ({size}B)")

        walk(target, 1)
        head = f"目录 {raw}（深度 {depth}）：{len(lines)} 条"
        if truncated:
            head += f"（已截断到 {max_entries} 条）"
        return head + "\n" + "\n".join(lines) if lines else head + "\n(空目录)"


class GrepTool(_FileQueryTool):
    name = "grep"
    description = (
        "Search file contents with a regular expression inside the project. "
        "Returns file:line matches with optional context lines (up to 40 for one literal file, 5 for directories). Read-only."
    )
    description_zh = "在项目内用正则搜索文件内容，返回 文件:行 与上下文，只读。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "path": {"type": "string", "description": "File or directory to search (default '.'); a file searches only that file"},
            "glob": {"type": "string", "description": "Filename filter, e.g. '*.py' or '*.ts'"},
            "context": {"type": "integer", "minimum": 0, "maximum": 40,
                        "description": "Lines before/after each match (default 0); literal file max 40, directory max 5. Clamping is reported."},
            "max_results": {"type": "integer", "description": f"Max matches (default 40, max {MAX_GREP_RESULTS})"},
            "ignore_case": {"type": "boolean", "description": "Case-insensitive search"},
        },
        "required": ["pattern"],
    }

    def _execute(self, arguments: dict[str, Any], ctx: ToolContext, cancelled: ThreadEvent) -> str:
        pattern = str(arguments.get("pattern") or "")
        if not pattern:
            raise ToolError("pattern 不能为空")
        try:
            flags = re.IGNORECASE if arguments.get("ignore_case") else 0
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ToolError(f"正则不合法: {exc}") from exc

        root = Path(ctx.workspace).resolve()
        raw_path = str(arguments.get("path") or ".")
        target = _WorkspaceBound.resolve_read(ctx.workspace, raw_path)
        if not target.exists():
            raise ToolError(f"路径不存在: {raw_path}")
        glob_filter = str(arguments.get("glob") or "").strip()
        requested_context = int(arguments.get("context") or 0)
        context_cap = 40 if target.is_file() else 5
        context = max(0, min(requested_context, context_cap))
        max_results = max(1, min(int(arguments.get("max_results") or 40), MAX_GREP_RESULTS))

        files = _iter_files(root, target, max_files=MAX_SCAN_FILES, cancelled=cancelled)
        hits: list[str] = []
        match_count = 0
        scanned = 0
        skipped_binary = 0
        for path in files:
            if cancelled.is_set() or match_count >= max_results:
                break
            if glob_filter:
                import fnmatch

                if not fnmatch.fnmatch(path.name, glob_filter):
                    continue
            try:
                if path.stat().st_size > MAX_GREP_BYTES:
                    continue
                blob = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in blob[:SNIFF_BYTES]:
                skipped_binary += 1
                continue
            scanned += 1
            try:
                text = blob.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            lines = text.split("\n")
            rel = str(path.relative_to(root)).replace("\\", "/")
            groups: list[tuple[int, int, list[int]]] = []
            for idx, line in enumerate(lines):
                if cancelled.is_set():
                    break
                if not regex.search(line):
                    continue
                start = max(0, idx - context)
                end = min(len(lines), idx + context + 1)
                if groups and start <= groups[-1][1]:
                    first, previous_end, coordinates = groups[-1]
                    coordinates.append(idx + 1)
                    groups[-1] = (first, max(previous_end, end), coordinates)
                else:
                    groups.append((start, end, [idx + 1]))
                match_count += 1
                if match_count >= max_results:
                    break
            # Context can overlap many nearby matches. Keep every real match
            # coordinate, but emit each source line once within a file.
            for start, end, coordinates in groups:
                matches = "\n".join(f"{rel}:{line}" for line in coordinates)
                block = "\n".join(f"{n + 1}: {lines[n]}" for n in range(start, end))
                hits.append(f"{matches}\n{block}")

        head = f"匹配 {match_count} 处（扫描 {scanned} 个文件"
        if skipped_binary:
            head += f"，跳过 {skipped_binary} 个二进制"
        head += f"，上限 {max_results}）"
        if len(files) >= MAX_SCAN_FILES:
            head += f"（已达到 {MAX_SCAN_FILES} 个文件的扫描上限，结果可能不完整；请用 path 缩小目录范围）"
        if context != requested_context:
            head += (
                f"\n上下文已裁剪：requested_context={requested_context}, applied_context={context}"
                f"（{'单文件' if context_cap == 40 else '目录'}上限 {context_cap}）。"
                "如需更多上下文，按下方真实匹配的 file:line 调用 read_file，用 offset/limit 补齐该窗口。"
            )
        body = "\n---\n".join(hits) if hits else "(无匹配)"
        return f"{head}\n{body}"


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """把 glob 模式编译成正则（对 POSIX 风格的**相对路径**匹配）。

    规则（对齐 DSH 的 `glob`）：

    - 不含 `/` 的模式匹配**任意深度的基名**：`*.py` 等价于 `**/*.py`（`*` 是整棵树）；
    - `**` 匹配零个或多个路径段；
    - `*` 只在段内匹配（不跨 `/`），`?` 段内单字符；
    - `[...]` 字符类透传（`[!a]` 转成 `[^a]`）。

    为什么不直接用 `fnmatch`/`PurePath.match`：两者对 `**` 与"无斜杠模式"的语义都和
    这里要的**不一致**（`fnmatch` 的 `*` 会跨 `/`，`PurePath.match` 各版本行为还在变），
    而"模型给的模式要按可预期的规则解释"是这个工具的全部价值。折算成正则 ~25 行，
    而且能单独测（见 tests/test_glob_tool.py）。
    """
    pat = pattern.replace("\\", "/").strip()
    pat = pat.lstrip("./") or "**/*"
    if pat == "**":
        pat = "**/*"
    if "/" not in pat:
        pat = "**/" + pat

    parts: list[str] = []
    for seg in pat.split("/"):
        if seg in ("", "."):
            continue
        if seg == "**":
            # 零个或多个"段 + 分隔符"
            parts.append("(?:[^/]+/)*")
            continue
        parts.append(_glob_segment(seg) + "/")
    regex = "".join(parts)
    if regex.endswith("/"):
        regex = regex[:-1]
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile("^" + regex + "$", flags)


def _glob_segment(seg: str) -> str:
    """段内通配：`*` → 段内任意、`?` → 段内单字符、`[...]` → 字符类。"""
    out: list[str] = []
    i = 0
    while i < len(seg):
        ch = seg[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            close = seg.find("]", i + 1)
            if close == -1:
                out.append(re.escape(ch))
            else:
                body = seg[i + 1 : close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = close
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


class GlobTool(_FileQueryTool):
    """按**路径模式**找文件（对齐 `dsh-tool-fs-search` 的 `glob`）。

    与 `list_dir` / `grep` 的分工：
    - `list_dir` 看"这一层有什么"（深度 ≤ 4）；
    - `grep` 找"内容里有什么"（它的 `glob` 参数只是文件名过滤）；
    - `glob` 找"**符合路径模式的文件有哪些**"——`**/*.py`、`src/**/*.ts`、
      `tests/test_*.py` 这类问题以前只能靠 list_dir 一层层翻或让 grep 去猜关键词。

    忽略规则与 `grep`/`list_dir` 同一套（`is_ignored_dir` + 隐藏目录），**不**包含
    `node_modules` / `.venv` 里的文件。这一点与 DSH 有意不同：DSH 的 glob 会带上
    被忽略的目录（只排除 VCS 元数据），但在 code-agent 里"产品内三个发现工具的
    忽略规则必须一致"比"逐字节对齐 DSH"更重要——否则模型会看到同一棵树的两份答案。
    """

    name = "glob"
    description = (
        "Find files by path pattern, e.g. '**/*.py', 'src/**/*.ts', 'tests/test_*.py'. "
        "A pattern without '/' matches the file name at any depth ('*.md' = the whole tree). "
        "Results are newest-modified first. Ignored dirs (.git/node_modules/.venv/...) are "
        "skipped, same as grep/list_dir. Read-only."
    )
    description_zh = (
        "按路径模式找文件（如 `**/*.py`、`src/**/*.ts`、`tests/test_*.py`）。"
        "不含 `/` 的模式匹配任意深度的文件名。按修改时间倒序返回。"
        "忽略目录（.git/node_modules/.venv 等）跳过，与 grep/list_dir 同一套规则。只读。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
            "path": {"type": "string", "description": "Directory to search (default '.')"},
            "max_results": {
                "type": "integer",
                "description": f"Max paths to return (default {MAX_GLOB_RESULTS}, max {MAX_GLOB_RESULTS_HARD})",
            },
        },
        "required": ["pattern"],
    }

    def _execute(self, arguments: dict[str, Any], ctx: ToolContext, cancelled: ThreadEvent) -> str:
        pattern = str(arguments.get("pattern") or "").strip()
        if not pattern:
            raise ToolError("pattern 不能为空")
        raw_path = str(arguments.get("path") or ".").strip() or "."
        limit = max(
            1, min(int(arguments.get("max_results") or MAX_GLOB_RESULTS), MAX_GLOB_RESULTS_HARD)
        )

        root = Path(ctx.workspace).resolve()
        start = _WorkspaceBound.resolve_read(ctx.workspace, raw_path)
        if not start.exists():
            raise ToolError(f"路径不存在: {raw_path}")
        base = start if start.is_dir() else start.parent

        matcher = _glob_to_regex(pattern)
        matched: list[tuple[float, str]] = []
        files = _iter_files(root, base, max_files=MAX_SCAN_FILES, cancelled=cancelled)
        for path in files:
            if cancelled.is_set():
                break
            # The root was resolved once and the walker already excludes links
            # escaping it. Avoid thousands of redundant Windows resolve calls.
            rel = path.relative_to(root).as_posix()
            if matcher.match(rel):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                matched.append((mtime, rel))

        matched.sort(key=lambda item: (-item[0], item[1]))   # 新的在前
        shown = matched[:limit]
        head = f"匹配 {pattern}（根 {raw_path}）：{len(matched)} 个文件，按修改时间倒序"
        if len(files) >= MAX_SCAN_FILES:
            head += f"（已达到 {MAX_SCAN_FILES} 个文件的扫描上限，结果可能不完整；请用 path 缩小目录范围）"
        if not shown:
            return head + "\n(本次扫描范围内没有匹配的文件——检查模式写法，或用 list_dir 看看这一层有什么)"
        body = "\n".join(rel for _mtime, rel in shown)
        if len(matched) > len(shown):
            head += f"（只列前 {limit} 个）"
            body += (
                f"\n…（还有 {len(matched) - len(shown)} 个未列出：这是本次返回的**条数上限**，"
                f"不是文件不存在。请把 pattern 收窄或加 path 限定范围再看）"
            )
        return f"{head}\n{body}"


class CodeMapTool(Tool):
    """把项目地图分片喂给模型——这是"先看地图再读代码"作业规程的落点。"""

    name = "code_map"
    description = (
        "Query the project structure map (symbols index) by node id / depth / kind. "
        "Use it FIRST to locate things instead of guessing paths. Read-only."
    )
    description_zh = "查询项目结构地图（符号索引）：按节点 id / 深度 / 类型取分片。定位代码先用它，不要猜路径。"
    parameters = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Symbol id from the map, e.g. 'sym:pkg/a.py#Foo.bar'"},
            "path": {"type": "string", "description": "Filter by file path (relative)"},
            "kinds": {"type": "string", "description": "Comma separated kinds: class,function,method,route,component,variable"},
            "depth": {"type": "integer", "description": "1=modules, 2=top-level symbols, 3=with members (default 3)"},
            "limit": {"type": "integer", "description": "Max symbols to return (default 80)"},
        },
    }

    def __init__(self, service: Any = None) -> None:
        self.service = service

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        if self.service is None:
            raise ToolError("代码情报服务未启用")
        project = self.service.project_for_workspace(ctx.workspace)
        if project is None:
            raise ToolError(
                "当前工作区不是已登记的代码模式项目：请先在「代码」页打开该项目，"
                "否则没有可查询的地图索引"
            )
        index = self.service.index(project["id"])

        node_id = str(arguments.get("node_id") or "").strip()
        if node_id:
            row = index.symbol(node_id)
            if row is None:
                raise ToolError(
                    f"节点不存在: {node_id}（可用 path/kinds 先列出候选，或调用 code_map 不带 node_id 取总览）"
                )
            from code_agent.codeintel import graph as graph_mod

            detail = graph_mod.symbol_detail(row)
            children = [
                graph_mod.symbol_summary(child)
                for child in index.query_all(
                    "SELECT * FROM symbols WHERE parent=? ORDER BY line", (node_id,)
                )
            ]
            payload = {
                "symbol": {k: detail[k] for k in detail if k != "source"},
                "members": [{"kind": c["kind"], "name": c["name"], "line": c["line"]} for c in children],
            }
            return _dump_capped(
                payload,
                list_key="members",
                budget=8000,
                hint="要看其余成员：用 path/kinds 先列出候选符号再逐个查，"
                "或用 read_file 直接读该符号所在文件的区间",
            )

        depth = max(1, min(int(arguments.get("depth") or 3), 3))
        kinds = [k.strip() for k in str(arguments.get("kinds") or "").split(",") if k.strip()] or None
        limit = max(1, min(int(arguments.get("limit") or 80), 400))
        path_filter = str(arguments.get("path") or "").strip()

        rows = index.all_symbols(kinds=kinds, path=path_filter or None, limit=limit)
        if depth < 3:
            rows = [r for r in rows if not r["parent"]]
        from code_agent.codeintel import graph as graph_mod

        out = {
            "project": {"name": project["name"], "root": project["root_path"]},
            "counts": index.counts(),
            "kinds": index.kind_counts(),
            "modules": [
                {"path": m["path"], "files": m["files"], "loc": m["loc"], "purpose": m["purpose"]}
                for m in graph_mod.build_modules(index)
            ][:20],
            "symbols": [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "name": r["name"],
                    "qname": r["qname"],
                    "path": r["path"],
                    "line": r["line"],
                    "end_line": r["end_line"],
                    "signature": (r["signature"] or "")[:160],
                }
                for r in rows
            ],
        }
        return _dump_capped(
            out,
            list_key="symbols",
            budget=12000,
            hint="缩小范围再查：加 path/kinds 过滤，或把 limit 调小（默认 80）",
        )


class CodeFeaturesTool(Tool):
    """按"能干什么"查项目功能导航（M33）——回答"X 功能在哪个板块"就该用它。

    和 code_map 的分工：code_map 回答"结构长什么样"（模块/符号/成员），
    feature 导航回答"这个项目能干什么、这件事在哪实现"。用户问"记忆功能在哪个板块"
    这类问题时，按结构去搜名字往往搜不到（功能名常与实现名不同），必须走这一层。
    """

    name = "code_features"
    description = (
        "Search the project's capability/feature map: what this project can do and where each "
        "feature is implemented (entry points, routes, tools, classes, documented symbols). "
        "Use it when asked 'where is feature X' / 'which part does Y'. Read-only."
    )
    description_zh = (
        "按「能干什么」查功能导航：能力域 → 功能 → 细分功能，每条带 文件:行。"
        "问「X 功能在哪个板块 / 在哪实现」先用它（结构地图 code_map 搜不到功能名是常事）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词，匹配功能名/说明/路径/能力域（如 '记忆'）"},
            "domain": {"type": "string", "description": "只看某个能力域，如 'code_agent/agent'"},
            "id": {"type": "string", "description": "展开某条功能的细分功能（成员/它调用的函数），值来自前一次结果"},
            "limit": {"type": "integer", "description": "最多返回多少条（默认 20）"},
        },
    }

    #: 一次带上多少条功能（一条功能一行，够模型判断"是它吗"）
    MAX_ITEMS = 60

    def __init__(self, service: Any = None) -> None:
        self.service = service

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        if self.service is None:
            raise ToolError("代码情报服务未启用")
        project = self.service.project_for_workspace(ctx.workspace)
        if project is None:
            raise ToolError("当前工作区不是已登记的代码模式项目：请先在「代码」页打开该项目")

        node_id = str(arguments.get("id") or "").strip()
        if node_id:
            kids = self.service.feature_children(project, node_id=node_id)
            return _dump_capped(
                {
                    "id": kids.get("id"),
                    "note": kids.get("note") or "",
                    "leaf": kids.get("leaf"),
                    "children": [
                        {
                            "id": c.get("id"),
                            "kind": c.get("kind"),
                            "label": c.get("label"),
                            "path": c.get("path"),
                            "line": c.get("line"),
                            "source_label": c.get("source_label"),
                            "has_children": c.get("has_children"),
                        }
                        for c in (kids.get("children") or [])
                    ],
                },
                list_key="children",
                budget=12000,
                hint="要看其余细分功能：换一个更具体的 id 再展开，"
                "或用 code_map / read_file 直接看该类成员与实现位置",
            )

        limit = max(1, min(int(arguments.get("limit") or 20), self.MAX_ITEMS))
        query = str(arguments.get("query") or "").strip().lower()
        domain_filter = str(arguments.get("domain") or "").strip().lower()
        nav = self.service.features(project)

        flat: list[dict] = []

        def walk(domain: dict) -> None:
            if domain_filter and domain_filter not in str(domain.get("name") or "").lower():
                return
            for node in domain.get("features") or []:
                flat.append({**node, "domain": domain.get("name") or ""})
            for child in domain.get("children") or []:
                walk(child)

        for top in nav.get("domains") or []:
            walk(top)

        def matches(node: dict) -> bool:
            if not query:
                return True
            blob = " ".join(
                [
                    str(node.get("label") or ""),
                    str(node.get("name") or ""),
                    str(node.get("doc") or ""),
                    str(node.get("path") or ""),
                    str(node.get("domain") or ""),
                    str(node.get("source_label") or ""),
                ]
            ).lower()
            return query in blob

        hits = [n for n in flat if matches(n)]
        # 有 docstring / 有模型补的描述的排前面：它们是"作者自己说这是什么"的功能
        hits.sort(key=lambda n: (not n.get("has_doc"), n.get("path") or "", n.get("line") or 0))
        payload = {
            "project": project["name"],
            "totals": nav.get("totals") or {},
            "matched": len(hits),
            "shown": min(len(hits), limit),
            "domains": [
                {"name": d.get("name"), "count": d.get("count"), "doc": (d.get("doc") or "")[:80]}
                for d in (nav.get("domains") or [])[:20]
            ]
            if not query
            else [],
            "features": [
                {
                    "id": n.get("id"),
                    "label": n.get("label"),
                    "kind": n.get("kind"),
                    "path": n.get("path"),
                    "line": n.get("line"),
                    "domain": n.get("domain"),
                    "source_label": n.get("source_label"),
                    "label_from": n.get("label_from"),
                    "has_children": n.get("has_children"),
                    "doc": (n.get("doc") or "")[:160],
                }
                for n in hits[:limit]
            ],
        }
        if not hits:
            payload["hint"] = (
                "没有匹配的功能名。功能名优先用作者写的 docstring 首句，可能与实现名不同："
                "换更短的关键词（如只用一个词），或用 code_map/grep 按实现名找。"
            )
        else:
            payload["hint"] = "要看某条功能怎么拆（类成员 / 它调用了谁），用 id 再调一次 code_features。"
        return _dump_capped(
            payload,
            list_key="features",
            budget=12000,
            hint="缩小范围再查：换更短的关键词、加 domain 过滤，或把 limit 调小",
        )


class CodeSearchTool(Tool):
    """融合检索（M34）：向量 + 关键词 + 图谱（按需）+ 元数据过滤。

    和 `code_features` 的分工：`code_features` 是"按名字找功能"（精确、便宜），
    这里解决"换个说法也能找到"和"跟哪些东西有关系"——问"用户偏好怎么记下来的"，
    字面搜"记忆"未必命中，语义路能把它捞回来；问"改了这个谁受影响"，
    图谱路把反向依赖加权进来。每条结果都带 `why`，说明它是被哪一路捞上来的。
    """

    name = "code_search"
    description = (
        "Hybrid search over the project's capabilities: fuses keyword (exact names), vector "
        "(paraphrase) and graph (who depends on whom, on demand) with structured metadata filters. "
        "Every hit says WHY it was retrieved. Read-only."
    )
    description_zh = (
        "融合检索（向量 + 关键词 + 图谱按需 + 元数据过滤）：换个说法也能找到，"
        "还能按域/路径/标签过滤；每条结果都说明为什么被召回。只读。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要查的问题/关键词（自然语言或符号名都行）"},
            "path_prefix": {"type": "string", "description": "只看某个目录下的功能，如 code_agent/memory/"},
            "tags": {"type": "string", "description": "标签过滤（逗号分隔，命中任一即可；能力域名也算标签）"},
            "graph": {"type": "boolean", "description": "是否启用按需图谱（默认 true）"},
            "limit": {"type": "integer", "description": "返回条数（默认 12，最多 40）"},
        },
        "required": ["query"],
    }

    def __init__(self, service: Any = None) -> None:
        self.service = service

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        if self.service is None:
            raise ToolError("代码情报服务未启用")
        project = self.service.project_for_workspace(ctx.workspace)
        if project is None:
            raise ToolError("当前工作区不是已登记的代码模式项目：请先在「代码」页打开该项目")
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ToolError("缺少 query")
        limit = max(1, min(int(arguments.get("limit") or 12), 40))

        from code_agent.retrieval import code_search as code_search_mod
        from code_agent.retrieval.metadata import Filters

        filters = Filters.from_dict(
            {
                "path_prefix": arguments.get("path_prefix") or "",
                "tags": arguments.get("tags") or "",
            }
        )
        index = self.service.index(project["id"])
        nav = self.service.features(project)
        hits, info = code_search_mod.search(
            query,
            nav,
            index=index if bool(arguments.get("graph", True)) else None,
            filters=filters if not filters.empty else None,
            # M34：chunk 元数据的 SQL 副本（块数超过阈值才真正启用；小集合内存更快）
            store=self.service.retrieval_store(project),
            project_id=str(project["id"]),
        )
        out = {
            "project": project["name"],
            "query": query,
            "chunks": info["chunks"],
            "graph": info["graph"],
            "hits": [hit.to_dict() for hit in hits[:limit]],
        }
        if not hits:
            out["hint"] = (
                "融合后没有任何信号命中：换更短的词、去掉过滤条件，"
                "或用 code_features 按名字精确找、code_map 看结构。"
            )
        return _dump_capped(
            out,
            list_key="hits",
            budget=12000,
            hint="缩小范围再查：把 limit 调小，或用 path_prefix / tags 过滤",
        )


class CodeGraphTool(Tool):
    """按需图谱（M34）：关系问题走边。

    为什么单独一个工具：把整个图塞进上下文既贵又没用；"谁调用我 / 我调用谁 / 两跳之后
    碰到谁"是**带方向**的问题，只能沿边问。每条边都带 `文件:行`；图上没有的一律不猜。
    """

    name = "code_graph"
    description = (
        "Walk the code graph on demand: who depends on / calls a symbol (direction=in) or what it "
        "depends on (direction=out), up to a few hops. Every step carries file:line. Read-only."
    )
    description_zh = (
        "按需走代码图：谁依赖/调用了它（direction=in）或它依赖谁（out），可走多跳，"
        "每一步都带 文件:行；图上没有的关系不会编出来。只读。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "起点符号 id，如 sym:code_agent/memory/store.py#UserMemory"},
            "name": {"type": "string", "description": "或用短名（要求全仓唯一，重名会报错而不是猜）"},
            "direction": {
                "type": "string",
                "enum": ["in", "out"],
                "description": "in=谁依赖它（默认）/ out=它依赖谁",
            },
            "hops": {"type": "integer", "description": "跳数（默认 2，最多 4）"},
            "limit": {"type": "integer", "description": "每跳最多几条（默认 40，最多 200）"},
        },
    }

    def __init__(self, service: Any = None) -> None:
        self.service = service

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        if self.service is None:
            raise ToolError("代码情报服务未启用")
        project = self.service.project_for_workspace(ctx.workspace)
        if project is None:
            raise ToolError("当前工作区不是已登记的代码模式项目：请先在「代码」页打开该项目")
        index = self.service.index(project["id"])

        node_id = str(arguments.get("id") or "").strip()
        if not node_id:
            name = str(arguments.get("name") or "").strip()
            if not name:
                raise ToolError("需要 id 或 name 指定起点")
            rows = index.symbols_by_name(name)
            if not rows:
                raise ToolError(f"没有叫 {name} 的符号")
            if len(rows) > 1:
                where = "、".join(f"{r['path']}:{r['line']}" for r in rows[:5])
                raise ToolError(f"{name} 在索引里有 {len(rows)} 个同名符号（{where}）：请用 id 指定")
            node_id = str(rows[0]["id"])

        from code_agent.retrieval import graph as graph_mod

        result = graph_mod.walk(
            index,
            node_id,
            direction=str(arguments.get("direction") or "in"),
            hops=int(arguments.get("hops") or graph_mod.DEFAULT_HOPS),
            limit=int(arguments.get("limit") or graph_mod.DEFAULT_LIMIT),
        )
        if result.get("error"):
            raise ToolError(str(result["error"]))
        # 主导列表是 `layers`（每层带 steps；`text` 是它的紧凑渲染，属同一份事实）。
        # 注意 walk() 自己也有一个 `truncated` 布尔量（"每跳已达上限"），
        # `_dump_capped` 只在真的因预算丢层时才写 `truncated`，并把原布尔量放进
        # 说明的 `upstream` 字段——两条事实都留在结果里，不互相覆盖。
        return _dump_capped(
            {**result, "text": graph_mod.chain_text(result)},
            list_key="layers",
            budget=12000,
            hint="缩小范围再查：把 hops / limit 调小，或换一个更具体的起点 id 分次查询",
        )


class EditFileTool(Tool):
    """精确替换编辑。唯一匹配 + patch 台账 + 越界拒绝 + 先读后改。"""

    name = "edit_file"
    description = (
        "Replace an exact string in a file (must match exactly once unless replace_all). "
        "Minimal edits only — do NOT rewrite whole files; use write_file only for new files. "
        "The file must have been read in this session and must not have changed since — "
        "otherwise the edit is refused; read the file, then retry. "
        "Every edit is recorded so the user can review and revert it."
    )
    description_zh = (
        "在文件中做精确字符串替换（默认必须唯一匹配）。"
        "只做最小改动，不要整文件覆盖（新建文件才用 write_file）；"
        "编辑前必须已在本会话读过该文件、且它没被改过，否则拒绝。"
        "每次改动都会记录，用户可回滚。"
    )
    #: 参与同一轮内的写租约（见 tools/registry.py）。
    writes_path = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace"},
            "old_string": {"type": "string", "description": "Exact text to replace (include enough context to be unique)"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(self, service: Any = None) -> None:
        self.service = service

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        from code_agent.atomic_write import atomic_replace

        raw = str(arguments.get("path") or "")
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if not raw:
            raise ToolError("path 不能为空")
        if old is None or new is None:
            raise ToolError("old_string / new_string 必填")
        old_text = str(old)
        new_text = str(new)
        if old_text == new_text:
            raise ToolError("old_string 与 new_string 相同，无需修改")

        path = _WorkspaceBound.resolve_write(ctx.workspace, raw)
        if not path.is_file():
            raise ToolError(f"文件不存在: {raw}（新建文件请用 write_file）")
        # 观察门禁放在**唯一匹配判定之前**：没读过就改，old_string 多半也匹配不上，
        # 那时给"找不到片段"只会误导模型去抠空白；"先读再改"才是它该做的动作。
        if ctx.fs_observation is not None:
            refusal = ctx.fs_observation.check_edit(path)
            if refusal:
                raise ToolError(refusal)
        try:
            before = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolError(f"读取失败: {exc}") from exc

        occurrences = before.count(old_text)
        replace_all = bool(arguments.get("replace_all"))
        if occurrences == 0:
            raise ToolError(
                "old_string 在文件中找不到完全一致的片段：请用 read_file 精确读取该区间后"
                "原样复制（注意缩进与空行），不要凭印象拼写"
            )
        if occurrences > 1 and not replace_all:
            lines = [
                idx + 1
                for idx, line in enumerate(before.split("\n"))
                if old_text.split("\n")[0] and old_text.split("\n")[0] in line
            ][:8]
            raise ToolError(
                f"old_string 命中 {occurrences} 处，无法确定改哪一处："
                f"请扩大上下文使其唯一，或显式传 replace_all=true。候选行号：{lines}"
            )

        after = before.replace(old_text, new_text) if replace_all else before.replace(old_text, new_text, 1)
        try:
            # 原子替换：中途挂掉不会把原文件留成半截（原来这里是 path.write_text）
            atomic_replace(path, after)
        except OSError as exc:
            raise ToolError(f"写入失败: {exc}") from exc
        if ctx.fs_observation is not None:
            # 改完之后文件是新版本了：立刻记账，否则同一轮里紧接着的第二次 edit
            # 会因为"读完之后变了"被自己的上一次改动拒掉。
            # 读磁盘而不是用内存里的 after：文本模式有换行转换，摘要必须与磁盘一致。
            ctx.fs_observation.record_read(path)

        patch_id = ""
        if self.service is not None:
            try:
                record = self.service.record_patch(
                    workspace=ctx.workspace,
                    rel=_relative(ctx.workspace, path),
                    session_id=ctx.session_id,
                    tool=self.name,
                    before=before,
                    after=after,
                    old_string=old_text,
                    new_string=new_text,
                    replace_all=replace_all,
                )
                patch_id = record.get("id", "")
            except Exception:  # noqa: BLE001 —— 台账失败不能吞掉已经成功的写入
                patch_id = ""

        rel = _relative(ctx.workspace, path)
        from code_agent.tools.edit_receipts import build_edit_result, changed_line_range, source_readback_text

        extent = changed_line_range(before, source_readback_text(after))
        location = f"{rel}:{extent.new_start}-{max(extent.new_start, extent.new_end)}" if extent else rel
        summary = f"已修改 {rel}（替换 {occurrences if replace_all else 1} 处）\n修改范围：{location}"
        if patch_id:
            summary += f"\npatch: {patch_id}（用户可在代码页回滚）"
        return build_edit_result(summary, [(rel, before, after)])


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


#: 一次 `edit_many` 最多几处改动。再多就该拆成几次调用，或者干脆整文件 `write_file`。
MAX_EDITS_PER_CALL = 20


class EditManyTool(Tool):
    """一次多处精确替换（M52）。**DSH 没有这个工具**，是这边多出来的能力。

    为什么值得单独一个工具：改一个跨文件的字段重命名或补一批 docstring，用 `edit_file`
    要发 N 轮工具调用——每一轮都要重新装配上下文、都要一次模型往返。这里一次调用做完，
    而且**语义比"发 N 次 edit_file"更严格**：见下面的两段式。

    两段式（这是这个工具的核心取舍）：

        ① 先在**内存里按顺序推演**全部改动：路径边界、观察门禁、唯一匹配、
           old_string 必须存在、同文件的多处按顺序叠加（第二处可以依赖第一处的产物）；
        ② **全部通过之后**才逐个文件落盘（每文件一次原子替换 + 一条 patch 台账）。

    为什么不做"做一处算一处"：那样失败时留下的是**半个意图**——调用方（模型）收到
    一条错误，却不知道已经改掉了哪些，于是它要么重试（把已改的再改一遍 → old_string
    找不到，再报错），要么以为全没改（其实改了一半）。两段式把"要么全做、要么不做"
    变成默认，而跨文件真事务做不到这件事是诚实的：落盘阶段真失败时**如实报告**
    哪些写了、哪些没写，绝不声称成功。

    注意：第 ① 段是**内存推演**，不是"预演写盘"。所以它不能发现"写的时候磁盘满了"
    这类落盘期错误——那种只能在 ② 里如实报出来（这也是不假装事务的原因）。
    """

    name = "edit_many"
    description = (
        "Apply SEVERAL exact string replacements in one call, across one or more files. "
        "All edits are validated first (path boundary, read-before-write, unique match) and "
        "applied in order per file; if ANY edit is invalid, NOTHING is written. "
        f"At most {MAX_EDITS_PER_CALL} edits per call. "
        "Use it for multi-site changes (rename a field across files, add docstrings). "
        "Every touched file is recorded in the patch ledger, one entry per file."
    )
    description_zh = (
        "一次调用做**多处**精确替换（可跨文件）。"
        "先整批校验（路径边界 / 先读后改 / 唯一匹配），同文件的多处按顺序叠加；"
        "**任何一处不合法就一处都不写**。"
        f"一次最多 {MAX_EDITS_PER_CALL} 处。适合跨文件改名、批量补 docstring 这类多站点改动。"
        "每个被改到的文件在台账里记一条，可逐条回滚。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_EDITS_PER_CALL,
                "description": (
                    f"Edits to apply, in order (at most {MAX_EDITS_PER_CALL}). "
                    "Same file may appear multiple times; order matters and later edits see "
                    "earlier ones applied."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to the workspace"},
                        "old_string": {"type": "string", "description": "Exact text to replace"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                        "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            }
        },
        "required": ["edits"],
    }

    writes_path = True

    def write_targets(self, arguments: dict[str, Any]) -> list[str]:
        """**必须覆盖**：这个工具写的是 `edits[*].path`，不是 `arguments["path"]`。

        不覆盖的后果很具体：租约按路径取锁，而默认实现从 `arguments["path"]` 拿到的是
        `None` → 一把锁都不取 → `edit_many` 与 `edit_file` 并发改同一个文件时毫无保护，
        却还"看起来声明了 `writes_path`"（比不声明更危险：读代码的人会以为有保护）。
        """
        edits = arguments.get("edits") if isinstance(arguments, dict) else None
        if not isinstance(edits, list):
            return []
        out: list[str] = []
        for item in edits:
            if isinstance(item, dict):
                raw = str(item.get("path") or "").strip()
                if raw:
                    out.append(raw)
        return out

    def __init__(self, service: Any = None) -> None:
        self.service = service

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        from code_agent.atomic_write import atomic_replace

        raw = arguments.get("edits")
        if not isinstance(raw, list) or not raw:
            raise ToolError("edits 必须是非空数组（每项 {path, old_string, new_string, replace_all?}）")
        if len(raw) > MAX_EDITS_PER_CALL:
            raise ToolError(
                f"一次最多 {MAX_EDITS_PER_CALL} 处改动（收到 {len(raw)} 处）："
                "请拆成几次调用，或改用 write_file 重写整个文件"
            )

        # {rel: (path, 原始内容, 当前内容)}；`当前内容` 在推演中逐处前移
        plan: dict[str, list[Any]] = {}
        order: list[str] = []
        errors: list[str] = []
        obs = ctx.fs_observation

        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                errors.append(f"第 {index} 处不是对象")
                continue
            rel = str(item.get("path") or "").strip()
            old, new = item.get("old_string"), item.get("new_string")
            if not rel:
                errors.append(f"第 {index} 处缺 path")
                continue
            if old is None or new is None:
                errors.append(f"第 {index} 处缺 old_string / new_string")
                continue
            old_text, new_text = str(old), str(new)
            if old_text == new_text:
                errors.append(f"第 {index} 处（{rel}）old_string 与 new_string 相同")
                continue

            try:
                path = _WorkspaceBound.resolve_write(ctx.workspace, rel)
            except ToolError as exc:
                errors.append(f"第 {index} 处（{rel}）：{exc}")
                continue
            if not path.is_file():
                errors.append(f"第 {index} 处（{rel}）：文件不存在（新建文件请用 write_file）")
                continue

            key = _relative(ctx.workspace, path)
            if key not in plan:
                # 每个文件**只过一次观察门禁**（同文件的多处不该互相把自己判成陈旧）
                if obs is not None:
                    refusal = obs.check_edit(path)
                    if refusal:
                        errors.append(f"第 {index} 处（{rel}）：{refusal}")
                        continue
                try:
                    before = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    errors.append(f"第 {index} 处（{rel}）：读取失败 {exc}")
                    continue
                plan[key] = [path, before, before]
                order.append(key)

            current = plan[key][2]
            occurrences = current.count(old_text)
            replace_all = bool(item.get("replace_all"))
            if occurrences == 0:
                errors.append(
                    f"第 {index} 处（{rel}）：old_string 在这份内容里找不到完全一致的片段"
                    "（注意：同一文件的后面几处看到的是**前面几处已经改过**的内容）"
                )
                continue
            if occurrences > 1 and not replace_all:
                errors.append(
                    f"第 {index} 处（{rel}）：old_string 命中 {occurrences} 处，无法确定改哪一处"
                    "（扩大上下文使其唯一，或显式传 replace_all=true）"
                )
                continue
            plan[key][2] = (
                current.replace(old_text, new_text)
                if replace_all
                else current.replace(old_text, new_text, 1)
            )

        if errors:
            # ① 段失败 → 一处都不写（这正是"要么全做、要么不做"）
            raise ToolError(
                f"整批校验失败，**没有做任何改动**（共 {len(raw)} 处，{len(errors)} 处不合法）：\n"
                + "\n".join(f"- {e}" for e in errors)
            )

        written: list[str] = []
        failed: list[str] = []
        for key in order:
            path, before, after = plan[key]
            if before == after:
                continue  # 推演后没变化（例如 replace_all 命中 0 次已被上面拦掉）
            try:
                atomic_replace(path, after)
            except OSError as exc:
                failed.append(f"{key}: {exc}")
                continue
            if obs is not None:
                obs.record_read(path)
            if self.service is not None:
                try:
                    self.service.record_patch(
                        workspace=ctx.workspace,
                        rel=key,
                        session_id=ctx.session_id,
                        tool=self.name,
                        before=before,
                        after=after,
                        old_string="(edit_many 多处)",
                        new_string="(edit_many 多处)",
                        replace_all=False,
                    )
                except Exception:  # noqa: BLE001 —— 台账失败不能吞掉已成功的写入
                    pass
            written.append(key)

        changes = sum(
            1 for key in order if plan[key][1] != plan[key][2]
        )
        from code_agent.tools.edit_receipts import build_edit_result, changed_line_range, source_readback_text

        receipts = [(key, plan[key][1], plan[key][2]) for key in written]
        locations = []
        for key, before, after in receipts:
            extent = changed_line_range(before, source_readback_text(after))
            locations.append(f"- {key}:{extent.new_start}-{max(extent.new_start, extent.new_end)}" if extent else f"- {key}")
        if failed:
            # ② 段部分失败：**如实报**，绝不声称成功（跨文件真事务做不到）
            summary = (
                f"[edit_many] **部分失败**（{len(written)} 个文件已写入，{len(failed)} 个没写）：\n"
                + (f"已写入：{', '.join(written)}\n" if written else "")
                + "失败：\n"
                + "\n".join(f"- {item}" for item in failed)
                + "\n已写入的**不会自动回滚**：如需撤销，用改动台账逐条回滚。"
            )
            return build_edit_result(summary, receipts)
        summary = (
            f"已修改 {len(written)} 个文件、共 {len(raw)} 处替换"
            f"（{changes} 个文件内容有变化）\n"
            + "\n".join(locations)
            + "\n每个文件在改动台账里各一条，可逐条回滚。"
        )
        return build_edit_result(summary, receipts)
