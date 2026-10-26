"""代码检索（M34）：把"项目能干什么"变成**可检索**的集合，并允许按需挂图谱。

为什么代码也要进检索层：知识库/记忆回答的是"这个团队怎么做事"，而
"这个功能在哪、它和谁有关系"只有代码索引能答。以前这件事是
`code_map`（结构）+ `code_features`（关键词过滤）两步手工拼，模型得自己兜——
现在统一走融合：**元数据先过滤（区域/路径/符号/契约），再 RRF 融合
字面 + 语义 + 图谱**，每条结果带 `why`。

分块来源是功能导航（M33）：一条功能 = 一个块，元数据带
`symbol_id / path / domain / kind / label_from`，所以：

- "X 功能在哪个板块"→ 字面命中功能名（作者写的 docstring 首句）或语义相近；
- "改了 X 会影响谁"→ 查询里的符号名映射到 id，取**反向依赖**邻居，作为图谱路加权；
- "只要 agent 那个域的"→ `Filters.domain/path_prefix` 精确过滤，不靠模型自我约束。

**不做**的事：不改功能导航本身（它仍是确定性产物），这里只读它。
"""

from __future__ import annotations

from typing import Any

from code_agent.retrieval import graph as graph_mod
from code_agent.retrieval import hybrid as hybrid_mod
from code_agent.retrieval.metadata import Chunk, Filters

#: 一次最多看多少条功能（导航本身有上限，这里再兜一层）
MAX_CHUNKS = 4000


def feature_chunks(
    nav: dict[str, Any],
    *,
    briefs: dict[str, dict] | None = None,
    limit: int = MAX_CHUNKS,
) -> list[Chunk]:
    """功能导航 → 检索块（只读它的字段，不重算任何东西）。"""
    out: list[Chunk] = []

    def walk(domain: dict[str, Any]) -> None:
        if len(out) >= limit:
            return
        for node in domain.get("features") or []:
            if len(out) >= limit:
                return
            label = str(node.get("label") or node.get("name") or "")
            doc = str(node.get("doc") or "")
            name = str(node.get("name") or "")
            # 文本 = **符号名** + 功能名 + 说明。
            # 符号名必须进来：用户/模型常常直接问 `save_memory`、`MAX_TOTAL_TURNS` 这种名字，
            # 只放"功能名 + 说明"会让字面路完全命中不到（真机踩到：查 save_memory 关键词单路无命中）。
            text = " ".join(part for part in (name, label, doc) if part).strip()
            if not text:
                continue
            out.append(
                Chunk(
                    ref=str(node.get("id") or ""),
                    text=text,
                    kind="code",
                    title=label,
                    origin="code",
                    path=str(node.get("path") or ""),
                    symbol_id=str(node.get("id") or ""),
                    tags=(str(domain.get("name") or ""), str(node.get("kind") or "")),
                    extra={
                        "name": name,
                        "label_from": str(node.get("label_from") or ""),
                        "source": str(node.get("source_label") or ""),
                        "domain": str(domain.get("name") or ""),
                        "line": int(node.get("line") or 0),
                    },
                )
            )
        for child in domain.get("children") or []:
            walk(child)

    for domain in nav.get("domains") or []:
        walk(domain)
    return out


def search(
    query: str,
    nav: dict[str, Any],
    *,
    index: Any = None,
    embedder: Any = None,
    filters: Filters | None = None,
    query_vector: list[float] | None = None,
    chunk_vectors: dict[str, list[float]] | None = None,
    use_graph: bool = True,
    graph_hops: int = 1,
    with_vectors: bool = True,
    store: Any = None,
    project_id: str = "",
) -> tuple[list[hybrid_mod.Hit], dict[str, Any]]:
    """在功能集合上做融合检索。返回 (命中, 元信息)。

    `store` 给定时（且块数超过阈值）走 SQL 路径：候选集在索引里裁，不再全量进 Python。
    功能块同时会**写回**该 store（`kind=code`），所以下次查询不必重建——但重建本身很便宜
    （441 块约 1 ms），真正省的是大仓库的内存与 FTS 索引。
    """
    chunks = feature_chunks(nav)
    info: dict[str, Any] = {
        "chunks": len(chunks),
        "graph": {"used": False, "symbols": [], "reason": "", "refs": 0},
        "store": "memory",
        "truncated": False,
    }
    active = None
    if store is not None:
        # 总是同步（幂等 upsert，很便宜）：这样跨过阈值的那一刻数据已经就绪，
        # 不需要"第一次大查询顺手重建全库"这种偶发慢路径。
        try:
            store.put_chunks(chunks)
            _, active = store_and_chunks(chunks, store)
            if active is not None:
                info["store"] = "sql"
        except Exception:  # noqa: BLE001 —— SQL 副本坏了就退内存，检索不该不可用
            active = None

    graph_refs: tuple[str, ...] = ()
    reason = ""
    if use_graph and index is not None:
        symbols = graph_mod.symbol_ids_in_query(index, query)
        if symbols:
            refs, reason = graph_mod.related_for_query(
                index, symbols, direction="in", hops=max(1, int(graph_hops))
            )
            graph_refs = tuple(refs)
            info["graph"] = {"used": bool(refs), "symbols": symbols, "reason": reason, "refs": len(refs)}

    lexical_refs: list[str] | None = None
    if active is not None:
        chunks, lexical_refs, truncated = active.candidates(query, filters)
        info["truncated"] = bool(truncated)
        if truncated:
            info["note"] = (
                "候选集按路径截断（块数超过检索上限）：字面精确命中不受影响，"
                "语义召回可能不全；加过滤条件（域/路径/标签）可缩小到完整范围。"
            )

    hits = hybrid_mod.fusion_ranking(
        query,
        chunks,
        filters=filters,
        query_vector=query_vector,
        chunk_vectors=chunk_vectors,
        graph_refs=graph_refs,
        graph_reason=reason,
        lexical_refs=lexical_refs,
    )
    return hits, info


def store_and_chunks(chunks: list[Chunk], store: Any) -> tuple[list[Chunk], Any]:
    """自动分级：块数超过阈值才用 SQL（小集合内存更快，实测 2 万块 1.9 ms vs 7.8 ms）。"""
    from code_agent.retrieval.store import pick_chunks

    return pick_chunks(chunks, store=store)


async def embedding_vectors(
    chunks: list[Chunk], query: str, embedder: Any
) -> tuple[dict[str, list[float]], list[float] | None]:
    """给这批块与 query 取向量（带内容哈希缓存；失败返回空，让关键词单路继续）。

    只对**要参与排序的块**取向量：被元数据过滤掉的块不该花 embedding 的时间。
    """
    if embedder is None or not getattr(embedder, "enabled", False) or not chunks:
        return {}, None
    vectors: dict[str, list[float]] = {}
    got = await embedder.embed([c.text for c in chunks])
    if got:
        for chunk, vector in zip(chunks, got):
            if vector:
                vectors[chunk.ref] = vector
    query_embed = await embedder.embed([query])
    return vectors, (query_embed[0] if query_embed else None)
