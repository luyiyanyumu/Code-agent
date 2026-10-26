"""按需图谱查询（M34）：关系问题走边，不走向量。

为什么"按需"：把整个图塞进上下文既贵又没用；关系查询的价值在**方向**与**深度**——
"谁调用它"和"它调用谁"是两个问题，"三跳之后会碰到谁"更是。这些都只能在图上问。

两条硬约束：

1. **必须带 `文件:行`**：每条边都要能落到具体位置，否则模型只能凭印象转述关系，
   那就又回到幻觉。图上没有的边一律不猜（宁可说"没查到"）。
2. **必须封顶**：`hops` 小（默认 2）、每跳 `limit`、visited 去重、环检测。
   调用图在真实项目里是稠密的，不封顶就是指数展开。

数据来源是既有索引（不新造一套坐标）：`referenced_by`（谁引用/调用它）、
`imports`（谁导入它所在的文件）、以及 `logic.function_logic`（函数体里真正调了谁）。
"""

from __future__ import annotations

from typing import Any, Callable

#: 默认跳数与每跳上限。真实项目里 2 跳已经能回答"改了会影响谁"，3 跳开始爆炸。
DEFAULT_HOPS = 2
DEFAULT_LIMIT = 40
MAX_HOPS = 4
MAX_LIMIT = 200


def _short(ident: str) -> str:
    return str(ident or "").replace("file:", "")


def _edge_row(index: Any, edge: dict[str, Any], direction: str) -> dict[str, Any]:
    """把一条索引边翻译成"带位置"的一步（没有位置的边也会标出来，但不假装知道位置）。"""
    other = _short(edge.get("src") if direction == "in" else edge.get("dst"))
    row: dict[str, Any] = {
        "id": other,
        "kind": str(edge.get("kind") or ""),
        "direction": direction,
    }
    if other.startswith("sym:"):
        symbol = index.symbol(other)
        if symbol:
            row.update(
                {
                    "name": str(symbol.get("name") or ""),
                    "path": str(symbol.get("path") or ""),
                    "line": int(symbol.get("line") or 0),
                    "qname": str(symbol.get("qname") or ""),
                }
            )
    elif other.startswith("file:"):
        row.update({"path": other[len("file:") :], "line": 1})
    return row


def _neighbors(index: Any, node_id: str, *, direction: str, limit: int) -> list[dict[str, Any]]:
    """一跳邻居。`in` = 谁指向它（谁调用/引用了它）；`out` = 它指向谁。

    **绝不"没有入边就退化成出边"**：那会把"它依赖谁"当成"谁依赖它"报给模型，
    方向是反的、而且看起来完全正常——这正是"箭头骗人"那类错误。
    没有就是没有，如实回空。
    """
    if direction == "in":
        steps: list[dict[str, Any]] = []
        if node_id.startswith("file:"):
            # 文件节点的入边就是"谁导入了它"
            steps = [_edge_row(index, e, "in") for e in index.edges(kind="imports", dst=node_id)]
        else:
            steps = [_edge_row(index, e, "in") for e in index.edges(kind="referenced_by", dst=node_id)]
    else:
        steps = []
        for kind in ("referenced_by", "imports", "calls"):
            for edge in index.edges(kind=kind, src=node_id):
                steps.append(_edge_row(index, edge, "out"))
    # 去重（同一邻居可能有多条边），保留首次出现顺序
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for step in steps:
        ident = f"{step.get('id')}|{step.get('kind')}"
        if ident in seen:
            continue
        seen.add(ident)
        out.append(step)
        if len(out) >= limit:
            break
    return out


def walk(
    index: Any,
    node_id: str,
    *,
    direction: str = "in",
    hops: int = DEFAULT_HOPS,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """从 `node_id` 出发走 `hops` 跳，返回**分层**的边（每层都带位置）。

    `direction="in"`：谁依赖我（改动的爆炸半径）；`"out"`：我依赖谁（实现链路）。
    """
    hops = max(1, min(int(hops or DEFAULT_HOPS), MAX_HOPS))
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    start = str(node_id or "").strip()
    if not start:
        return {"error": "缺少起始节点 id", "layers": []}

    layers: list[dict[str, Any]] = []
    visited = {start}
    frontier = [start]
    truncated = False
    for depth in range(1, hops + 1):
        nxt: list[str] = []
        steps: list[dict[str, Any]] = []
        for node in frontier:
            for step in _neighbors(index, node, direction=direction, limit=limit):
                step["from"] = node
                step["depth"] = depth
                steps.append(step)
                target = str(step.get("id") or "")
                if target and target not in visited:
                    visited.add(target)
                    nxt.append(target)
            if len(steps) >= limit:
                truncated = True
                break
        if not steps:
            break
        layers.append({"depth": depth, "count": len(steps[:limit]), "steps": steps[:limit]})
        if truncated or not nxt:
            break
        frontier = nxt[:limit]

    return {
        "start": start,
        "direction": direction,
        "hops": hops,
        "nodes_seen": len(visited),
        "layers": layers,
        "truncated": truncated,
        "note": (
            "只列索引里真实存在的边（每条带 文件:行）。图上没有的依赖不会出现在这里；"
            "静态调用图不含运行时的动态分发，读到 `calls` 只代表静态可达。"
        ),
    }


def related_for_query(
    index: Any,
    symbol_ids: list[str],
    *,
    direction: str = "in",
    hops: int = 1,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[str], str]:
    """检索用的"按需图谱"：给定查询里识别出的符号，返回它们的关系邻居 id 列表。

    返回 `(邻居 id 列表, 归因文字)`——归因文字会进入检索结果的 `why`，
    让人看得见"这条是因为图上有关系才被捞上来的"。
    """
    refs: list[str] = []
    for symbol_id in symbol_ids:
        result = walk(index, symbol_id, direction=direction, hops=hops, limit=limit)
        for layer in result.get("layers") or []:
            for step in layer.get("steps") or []:
                ident = str(step.get("id") or "")
                if ident.startswith("sym:") and ident not in refs:
                    refs.append(ident)
    reason = f"与 {len(symbol_ids)} 个符号{'的反向依赖' if direction == 'in' else '的依赖'}相关"
    return refs, reason


def symbol_ids_in_query(index: Any, query: str, *, limit: int = 3) -> list[str]:
    """查询里出现的符号名 → 索引里的符号 id（用于按需图检索）。

    只在**名字唯一**时才认（与 codeintel.logic 的调用解析同一原则：
    重名就说不准，说不准就不要假装知道）。
    """
    from code_agent.retrieval.hybrid import query_terms

    found: list[str] = []
    for term in query_terms(query)[:12]:
        rows = index.symbols_by_name(term) if hasattr(index, "symbols_by_name") else []
        if len(rows) == 1:
            ident = str(rows[0].get("id") or "")
            if ident and ident not in found:
                found.append(ident)
        if len(found) >= limit:
            break
    return found


def chain_text(result: dict[str, Any]) -> str:
    """把 walk 的结果渲染成给模型看的紧凑文本（带 文件:行）。"""
    lines: list[str] = []
    for layer in result.get("layers") or []:
        lines.append(f"[{layer['depth']} 跳 · {layer['count']} 条]")
        for step in layer.get("steps") or []:
            loc = f"{step.get('path')}:{step.get('line')}" if step.get("path") else step.get("id", "")
            kind = step.get("kind") or "edge"
            name = step.get("name") or step.get("id") or ""
            lines.append(f"  {kind} → {name}（{loc}）")
    if not lines:
        lines.append("（没有查到边：可能是叶子节点，或索引里没有静态关系）")
    if result.get("truncated"):
        lines.append("（已达每跳上限，结果被截断；要看更远请缩小起点或分次查询）")
    return "\n".join(lines)


def as_fetch(index: Any) -> Callable[[list[str]], list[str]]:
    """给检索层的便捷闭包：给符号 id 列表，回关系邻居（默认反向一跳）。"""

    def _fetch(symbol_ids: list[str]) -> list[str]:
        refs, _reason = related_for_query(index, symbol_ids, direction="in", hops=1)
        return refs

    return _fetch
