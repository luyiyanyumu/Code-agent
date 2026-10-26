"""融合排序（M34）：RRF 把三种信号合起来，且**说得清每条为什么被召回**。

为什么用 RRF（Reciprocal Rank Fusion）而不是加权求和：

- 关键词分（BM25，IDF 加权，无界）与余弦相似度（0~1）**量纲不可比**，
  直接相加等于让"哪个分数大"决定权重——换个模型、换个语料长度，权重就变了。
- RRF 只用**名次**：`score = Σ 1/(k + rank_i)`。它不关心各信号的分数量纲，
  因此"换 embedding 模型"不会让融合比例失衡。这是工程上最稳的融合方式。

三种信号（缺哪个就少一路，不用补齐）：

1. **lexical** 关键词：字面命中（符号名、常量名、路径、术语）——精确约束的落点；
2. **semantic** 向量：换述/同义（"记忆" ↔ "长期偏好"）——关键词的补集；
3. **graph** 图谱：查询里出现了关系词（谁调用/影响/依赖）时，把图上的邻居加进来——
   这部分是**按需**的，由调用方先查 `retrieval.graph`，把结果作为额外一路。

另外两条明确规则：

- **字面精确命中置顶**：query 里出现的符号名/路径/标签，若在某条的元数据里**原样出现**，
  给它一个 rank-0 的加权——因为这几乎不可能是巧合。向量再像也压不过"名字对上了"。
- **无信号就不返回**：三路都没命中 → 空列表。宁缺毋滥，不往上下文塞"聊胜于无"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from code_agent.retrieval.metadata import Chunk, Filters, apply_filters

#: RRF 常数：越大越"平权"，越小越偏向各自的第一名。60 是文献里的常用默认值。
RRF_K = 60
#: 字面精确命中的加权次数（相当于再拿一个 rank-0 的第一名）
EXACT_BOOST = 3.0

_TOKEN_RE = re.compile(r"[A-Za-z0-9_./#-]{2,}|[\u4e00-\u9fff]{2,}")


@dataclass
class Hit:
    """一条检索结果：内容 + 元数据 + 归因（谁把它捞上来的）。"""

    ref: str
    text: str
    score: float
    why: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    lexical_rank: int = 0
    semantic_rank: int = 0
    graph_rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "text": self.text,
            "score": round(self.score, 6),
            "why": self.why,
            "meta": self.meta,
            "lexical_rank": self.lexical_rank,
            "semantic_rank": self.semantic_rank,
            "graph_rank": self.graph_rank,
        }


def rrf(rankings: Sequence[Sequence[str]], *, k: int = RRF_K) -> dict[str, float]:
    """多路名次 → 融合分。`rankings[i]` 是第 i 路按相关性排序的 ref 列表。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for index, ref in enumerate(ranking):
            scores[ref] = scores.get(ref, 0.0) + 1.0 / (k + index + 1)
    return scores


def query_terms(query: str) -> list[str]:
    """从查询里切出"可能精确命中"的词：符号名、路径、中文词（长度 >= 2）。

    只用于**字面命中判定**（要不要置顶），不用于打分本身——打分交给 BM25。
    """
    seen: list[str] = []
    for token in _TOKEN_RE.findall(query or ""):
        lowered = token.lower()
        if lowered not in seen:
            seen.append(lowered)
    return seen


def exact_matches(query: str, chunk: Chunk) -> list[str]:
    """query 里的词是否**原样**出现在这条的元数据里（名字/路径/标签/标题）。

    为什么只认元数据不认正文：正文里的偶然重合太多（"的"、"函数"），
    而"符号名/文件路径/标签原样出现"几乎不可能是巧合——这才是精确约束。
    """
    terms = query_terms(query)
    if not terms:
        return []
    fields = " ".join(
        [
            chunk.title,
            chunk.path,
            chunk.symbol_id,
            chunk.contract_id,
            " ".join(chunk.tags),
            chunk.extra.get("name", "") if isinstance(chunk.extra, dict) else "",
        ]
    ).lower()
    return [t for t in terms if t and t in fields]


def lexical_ranking(query: str, chunks: list[Chunk]) -> list[str]:
    """关键词路（复用记忆检索的 BM25 口径，保证"同一个打分器"）。"""
    from code_agent.memory.retriever import rank

    by_ref = {c.ref: c for c in chunks}
    hits = rank([(c.ref, c.text) for c in chunks], query)
    return [ref for ref, _score in hits if ref in by_ref]


def semantic_ranking(vectors: Iterable[tuple[str, list[float]]], query_vector: list[float] | None) -> list[str]:
    """向量路：按余弦相似度排序（只保留正相似度）。"""
    if not query_vector:
        return []
    scored: list[tuple[float, str]] = []
    for ref, vector in vectors:
        score = cosine(query_vector, vector)
        if score > 0:
            scored.append((score, ref))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [ref for _score, ref in scored]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def fusion_ranking(
    query: str,
    chunks: list[Chunk],
    *,
    filters: Filters | None = None,
    query_vector: list[float] | None = None,
    chunk_vectors: dict[str, list[float]] | None = None,
    graph_refs: Sequence[str] = (),
    graph_reason: str = "",
    lexical_refs: Sequence[str] | None = None,
) -> list[Hit]:
    """先按元数据裁剪候选，再做三路融合，最后给出带归因的结果（未截断）。

    `lexical_refs` 给定时用它当关键词路的名次（例如来自 SQLite FTS5 的 `bm25()`），
    否则在本函数内用同一套 BM25 现算——两条路**口径必须一致**（都走
    `memory.retriever._tokenize`），否则融合的名次没意义。
    """
    candidates = apply_filters(chunks, filters)
    if not candidates:
        return []
    by_ref = {c.ref: c for c in candidates}

    lexical = (
        [ref for ref in lexical_refs if ref in by_ref]
        if lexical_refs is not None
        else lexical_ranking(query, candidates)
    )
    semantic = semantic_ranking(
        [(c.ref, (chunk_vectors or {}).get(c.ref) or []) for c in candidates], query_vector
    )
    graph = [ref for ref in graph_refs if ref in by_ref]

    rankings = [ranking for ranking in (lexical, semantic, graph) if ranking]
    if not rankings:
        return []
    scores = rrf(rankings)

    # 字面精确命中：再给一个 rank-0 的加权（相当于"又多拿了一路第一名"）
    exact: dict[str, list[str]] = {}
    for chunk in candidates:
        terms = exact_matches(query, chunk)
        if terms:
            exact[chunk.ref] = terms
            scores[chunk.ref] = scores.get(chunk.ref, 0.0) + EXACT_BOOST / (RRF_K + 1)

    lex_rank = {ref: i + 1 for i, ref in enumerate(lexical)}
    sem_rank = {ref: i + 1 for i, ref in enumerate(semantic)}
    grp_rank = {ref: i + 1 for i, ref in enumerate(graph)}

    hits: list[Hit] = []
    for ref, score in scores.items():
        chunk = by_ref[ref]
        why: list[str] = []
        if ref in exact:
            why.append("字面命中：" + "、".join(exact[ref][:4]))
        if ref in lex_rank:
            why.append(f"关键词第 {lex_rank[ref]} 名")
        if ref in sem_rank:
            why.append(f"语义第 {sem_rank[ref]} 名")
        if ref in grp_rank:
            why.append(f"图谱{('（' + graph_reason + '）') if graph_reason else ''}第 {grp_rank[ref]} 名")
        hits.append(
            Hit(
                ref=ref,
                text=chunk.text,
                score=score,
                why=why,
                meta=chunk.meta(),
                lexical_rank=lex_rank.get(ref, 0),
                semantic_rank=sem_rank.get(ref, 0),
                graph_rank=grp_rank.get(ref, 0),
            )
        )
    hits.sort(key=lambda h: (-h.score, h.ref))
    return hits
