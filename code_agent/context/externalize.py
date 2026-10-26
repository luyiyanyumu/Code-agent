"""大输出外部化 + **按工具的输出预算**（对齐 DSH 的 `dsh-output-retention` / `dsh-spill-policy`）。

心法不变：上下文窗口是 RAM，tool_results 目录是磁盘；模型要完整内容时再按路径读。
这一版把"一刀切的阈值 + 固定 400 字预览"换成三件更准的事：

1. **按字节，不按字符**。上下文成本是 token，token 与字节正相关；按字符算会让中文
   输出"看起来没超"却实际占了近三倍空间。`len(str)` 与 `len(str.encode())` 在中文上
   差 3 倍——这正是最容易漏掉的一种"预算失守"。
2. **按工具选窗口**（`head` / `tail` / `head_tail`）。不同工具的超长输出，有价值的部分
   在不同位置：`grep`/`glob` 是结果列表（开头最相关），命令输出是报错（**结尾**），
   而 `bash` 的退出码在**开头**、诊断在结尾——所以它两头都要留。一刀切"留开头"
   会把命令的报错整个切掉，模型于是看不到真正的失败原因。
3. **省略必须说清三件事**：这是**预算**事实（不是上游没输出）、省了多少字节、
   怎么拿到全部（落盘路径 + 用 `read_file` 的 offset/limit 或 `grep` 该路径）。

两条硬细节：

- **切在 UTF-8 边界**：按字节切会切断多字节字符，解码后是一个替换字符（乱码方块）。
  模型会把乱码当成文件内容的一部分——宁可少留两个字节，也不给一个坏字符。
- **落盘的文件是完整原文**：被裁的只是喂给模型的那一份。模型随时能自己捞回来。

运维/测试旋钮：`CODE_AGENT_EXTERNALIZE_THRESHOLD`（字节）。**显式设了就覆盖所有工具
的按工具预算**（只留开头）——这是给"我想统一收紧/放宽"和测试用的，不是默认路径。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from code_agent.config import tool_results_dir

#: 默认窗口：留开头 16 KiB。
DEFAULT_WINDOW = (16 * 1024, 0)

#: 工具名 → (开头字节, 结尾字节)。没列到的工具用 DEFAULT_WINDOW。
TOOL_WINDOWS: dict[str, tuple[int, int]] = {
    # 退出码在开头（`exit_code=...`）、报错在结尾 → 两头都要；中间的过程日志最不重要
    "bash": (1024, 15 * 1024),
    # 结果列表：开头最相关（地图/功能/令牌清单都是从重要的往下排）
    "grep": (12 * 1024, 0),
    "glob": (8 * 1024, 0),
    "code_map": (16 * 1024, 0),
    "code_features": (16 * 1024, 0),
    "code_search": (16 * 1024, 0),
    "code_graph": (16 * 1024, 0),
    "kb_search": (12 * 1024, 0),
    "read_file": (32 * 1024, 0),
    # 网页/检索：正文往往前有导语后有结论，两头都留一点
    "web_fetch": (6 * 1024, 6 * 1024),
    "web_search": (12 * 1024, 0),
    "job_output": (8 * 1024, 8 * 1024),
    # Structured cursor pages are already shrunk to this UTF-8 budget by the
    # evidence tool, preserving JSON and pagination instead of spilling them.
    "read_agent_execution": (32 * 1024, 0),
}

#: 环境变量：显式设置后覆盖所有工具的窗口（只留开头）。0/未设 = 用按工具预算。
ENV_THRESHOLD = "CODE_AGENT_EXTERNALIZE_THRESHOLD"


def window_for(tool: str = "", threshold: int | None = None) -> tuple[int, int]:
    """算出某个工具这次该留多少（开头字节, 结尾字节）。

    优先级：显式 `threshold` 参数 > `CODE_AGENT_EXTERNALIZE_THRESHOLD` > 按工具表 > 默认。

    `threshold <= 0` 一律当作"没配"并回退到按工具表——**不是**当成"窗口为 0"。
    后者会变成一个安静的坑：任何非空输出都超预算、落到盘上、而喂回模型的那一份
    **一个字节内容都没有**（只剩指针），模型拿到一条"什么都没有"的结果却不知道
    全文在哪一类的地方还能捞回来。与 `MAX_TOTAL_TURNS=0`（不设上限）同一读法。
    """
    if threshold is not None and int(threshold) > 0:
        return (int(threshold), 0)
    raw = (os.environ.get(ENV_THRESHOLD) or "").strip()
    if raw:
        try:
            value = int(float(raw))
        except ValueError:
            value = 0
        if value > 0:
            return (value, 0)
    return TOOL_WINDOWS.get(tool, DEFAULT_WINDOW)


def _trim_partial_tail(data: bytes) -> bytes:
    """去掉结尾被切断的多字节字符（最多回退 3 字节）。

    按字节切 UTF-8 会留下半个字符，解码出来是 U+FFFD。少给两个字节 vs 给一个乱码方块，
    后者更糟：模型会把乱码当成内容的一部分去引用。
    """
    for cut in range(4):
        candidate = data[: len(data) - cut] if cut else data
        try:
            candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return candidate
    return data[:-1] if data else data


def _head(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return _trim_partial_tail(data[:limit]).decode("utf-8", errors="replace")


def _tail(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    window = data[len(data) - limit :]
    # 开头落在字符中间时，头部是续字节（0x80–0xBF）→ 丢掉它们
    while window and (window[0] & 0xC0) == 0x80:
        window = window[1:]
    return window.decode("utf-8", errors="replace")


def externalize_result(
    result: str,
    session_id: str,
    call_id: str,
    threshold: int | None = None,
    root: Path | None = None,
    tool: str = "",
    diagnostic_chars: int | None = None,
) -> str:
    """结果超出该工具的预算时落盘，返回"按窗口裁剪 + 指针 + 诚实的省略说明"。

    没超预算就**原样返回**（一个字节都不动）——绝大多数调用走这条快路径。
    """
    head_limit, tail_limit = window_for(tool, threshold)
    if diagnostic_chars is not None:
        diagnostic_chars = min(diagnostic_chars, head_limit + tail_limit)
    data = result.encode("utf-8")
    concise_failure = (diagnostic_chars is not None and diagnostic_chars > 0
                       and tool in {"bash", "bash_persistent"}
                       and result.startswith("exit_code=")
                       and not result.startswith("exit_code=0")
                       and len(result) > diagnostic_chars)
    if len(data) <= head_limit + tail_limit and not concise_failure:
        return result

    base = root or tool_results_dir()
    target_dir = base / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()[:10]
    path = target_dir / f"{call_id}_{digest}.txt"
    path.write_text(result, encoding="utf-8")

    if concise_failure:
        from code_agent.context.diagnostics import failed_command_excerpt

        excerpt = failed_command_excerpt(result, max_chars=diagnostic_chars)
        return (excerpt + f"\n全文已存盘: {path}\n"
                "（失败诊断摘录；全文保持原样。仅缺少具体信息时按行读取对应日志，不必为查看输出格式重复执行未修改的失败版本。）")

    head_text = _head(result, head_limit)
    tail_text = _tail(result, tail_limit) if tail_limit else ""
    kept = len(head_text.encode("utf-8")) + len(tail_text.encode("utf-8"))
    omitted = len(data) - kept

    where = []
    if head_limit and tail_limit:
        where.append(f"保留开头 {head_limit} + 结尾 {tail_limit}")
    elif head_limit:
        where.append(f"保留开头 {head_limit}")
    elif tail_limit:
        where.append(f"保留结尾 {tail_limit}")
    scope = f"`{tool}`" if tool else "本次"
    lines = [
        f"[外部化] {scope} 输出 {len(data)} 字节，超出本工具的预算"
        f"（{head_limit + tail_limit} 字节：{' + '.join(where) or '无窗口'}）。",
        f"全文已存盘: {path}",
    ]
    if head_text:
        lines += ["", f"--- 开头 {head_limit} 字节 ---", head_text]
    if tail_text:
        lines += ["", f"--- 结尾 {tail_limit} 字节 ---", tail_text]
    # 省略的**位置**要说对：只留开头时省的是"后面"，只留结尾时省的是"前面"，
    # 两头都留才是"中间"。说错位置听起来是小事，但模型会据此判断"该往哪边找"——
    # 而"往哪边找"正是它下一步动作的依据。
    if head_limit and tail_limit:
        where_gone = "中间"
    elif head_limit:
        where_gone = "后面"
    elif tail_limit:
        where_gone = "前面"
    else:
        where_gone = "全部"

    lines += [
        "",
        f"（省略了{where_gone} {omitted} 字节。这是**输出预算**造成的截断，**不是**工具没有输出——"
        "不要把「没看到」当成「不存在」。要完整内容：用 read_file 读上面的路径"
        "（可带 offset/limit 分页），或 grep 该路径在全文里搜。）",
    ]
    return "\n".join(lines)
