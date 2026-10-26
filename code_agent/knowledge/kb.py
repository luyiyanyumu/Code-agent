"""知识库门面（M12/M14，M34 起检索走融合）：双区文档 CRUD + 融合检索 + LLM 沉淀。

**M34 之前**是"要么向量、要么 BM25"的二选一：配了 embedding 就只做余弦，
没配就只做关键词。它有两个真实问题：

1. 向量路会**漏掉字面精确命中**——用户问 `MAX_TOTAL_TURNS` 或某个符号名时，
   语义相近但名字不对的条目可能排在真正那条前面；
2. 两套分数（cosine 0~1 / BM25 无界）不可比，所以没法"各取所长"。

现在交给 `retrieval.hybrid`：**元数据先过滤，再 RRF 融合关键词 + 向量**（+ 按需图谱），
每条结果带 `why`（为什么被召回：字面命中/关键词第几名/语义第几名）。
embedding 不可用时融合自然退化成关键词单路——**不是降级成"差一点的模式"，
而是同一套融合少了一路**，行为一致、可解释。

双区（LLM wiki）：
- 正式区 origin=human  人工维护的权威知识
- 隔离区 origin=llm    模型 kb_write 沉淀、LLM 编译产物；检索可见、可采纳(→正式区)、可删除

索引策略：分块向量由 `retrieval.embed.VectorStore` **按内容哈希持久缓存**
（进程重启不再重算，内容一变旧向量自动不再被用）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from code_agent.knowledge.embed import Embedder
from code_agent.knowledge.store import AREA_HUMAN, AREA_LLM, KnowledgeStore, chunk_text
from code_agent.retrieval import hybrid as hybrid_mod
from code_agent.retrieval.metadata import Chunk, Filters, apply_filters as metadata_apply_filters
from code_agent.retrieval.store import chunk_store, pick_chunks

DEFAULT_TOP_K = 5


def _cosine(a: list[float], b: list[float]) -> float:
    """保留旧名（外部有引用）；实现已收敛到 retrieval.hybrid 的一处口径。"""
    return hybrid_mod.cosine(a, b)


class KnowledgeBase:
    def __init__(self, root: Path, embedder: Embedder | None = None):
        self.store = KnowledgeStore(root)
        self.embedder = embedder or Embedder()
        # 分块索引：每块 {doc_id, title, origin, chunk, key, text, tags, source, ts}
        self._chunks: list[dict[str, Any]] = []
        # M34：chunk 元数据的 SQL 副本（按块数自动决定是否真正启用；见 pick_chunks）
        self.chunk_store = chunk_store("kb")
        self._rebuild()

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------
    def _chunk_dicts(self, doc: dict[str, Any]) -> list[dict[str, Any]]:
        origin = doc.get("origin", AREA_HUMAN)
        doc_id = doc["id"]
        return [
            {
                "doc_id": doc_id,
                "title": doc["title"],
                "origin": origin,
                "chunk": i,
                "key": f"{origin}:{doc_id}#{i}",
                "text": text,
                # M34 元数据：来源、标签、时间——先过滤再排序用的就是它们
                "tags": tuple(doc.get("tags") or ()),
                "source": str(doc.get("source") or ""),
                "ts": float(doc.get("ts") or 0) or time.time(),
            }
            for i, text in enumerate(chunk_text(doc["content"]))
        ]

    def _add_doc_chunks(self, doc: dict[str, Any]) -> None:
        """（重建）一篇文档的全部块；同时同步到 SQL 副本。"""
        origin = doc.get("origin", AREA_HUMAN)
        doc_id = doc["id"]
        stale = [c["key"] for c in self._chunks if c["doc_id"] == doc_id and c["origin"] == origin]
        self._chunks = [c for c in self._chunks if not (c["doc_id"] == doc_id and c["origin"] == origin)]
        fresh = self._chunk_dicts(doc)
        self._chunks.extend(fresh)
        self._sync_store(fresh, stale)

    def _sync_store(self, fresh: list[dict[str, Any]], stale: list[str]) -> None:
        """把内存索引的变更同步进 SQL 副本（派生数据，失败不影响主流程）。"""
        if self.chunk_store is None:
            return
        try:
            if stale:
                self.chunk_store.delete_chunks(stale)
            if fresh:
                self.chunk_store.put_chunks([self._to_chunk(c) for c in fresh])
        except Exception:  # noqa: BLE001 —— SQL 副本只是加速结构，坏了就退内存
            self.chunk_store = None

    def _rebuild(self) -> None:
        """重扫两区磁盘重建分块索引；SQL 副本整份重建（派生数据，不做增量迁移）。"""
        self._chunks = []
        if self.chunk_store is not None:
            try:
                self.chunk_store.clear(kind="kb")
            except Exception:  # noqa: BLE001
                self.chunk_store = None
        for doc in self.store.list():
            full = self.store.get(doc["id"], doc["origin"])
            if full:
                fresh = self._chunk_dicts(full)
                self._chunks.extend(fresh)
                self._sync_store(fresh, [])

    # 兼容旧名
    _upsert_doc_chunks = _add_doc_chunks

    # ------------------------------------------------------------------
    # 文档 CRUD
    # ------------------------------------------------------------------
    def list_docs(self, origin: str | None = None) -> list[dict[str, Any]]:
        return self.store.list(origin)

    def get_doc(self, doc_id: str, origin: str | None = None) -> dict[str, Any] | None:
        return self.store.get(doc_id, origin)

    def create(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        origin: str = AREA_HUMAN,
        source: str = "",
    ) -> dict[str, Any]:
        doc = self.store.create(title, content, tags or [], origin, source)
        self._add_doc_chunks(doc)
        return doc

    def update(
        self,
        doc_id: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        origin: str | None = None,
    ) -> dict[str, Any] | None:
        existing = self.store.get(doc_id, origin)
        if existing is None:
            return None
        doc = self.store.save(
            doc_id,
            title,
            content,
            tags or [],
            existing["origin"],
            existing.get("source", ""),
        )
        self._add_doc_chunks(doc)
        return doc

    def delete(self, doc_id: str, origin: str | None = None) -> bool:
        ok = self.store.delete(doc_id, origin)
        if ok:
            stale = [
                c["key"]
                for c in self._chunks
                if c["doc_id"] == doc_id and (origin is None or c["origin"] == origin)
            ]
            self._chunks = [
                c
                for c in self._chunks
                if not (c["doc_id"] == doc_id and (origin is None or c["origin"] == origin))
            ]
            self._sync_store([], stale)          # 同步 SQL 副本：删掉的块不能再被召回
        return ok

    def promote(self, doc_id: str) -> dict[str, Any] | None:
        """采纳：把隔离区条目移入正式区（重建该条目的索引；SQL 副本一起换）。"""
        doc = self.store.move(doc_id, AREA_LLM, AREA_HUMAN)
        if doc is None:
            return None
        stale = [
            c["key"] for c in self._chunks if c["doc_id"] == doc_id and c["origin"] == AREA_LLM
        ]
        self._chunks = [
            c for c in self._chunks if not (c["doc_id"] == doc_id and c["origin"] == AREA_LLM)
        ]
        fresh = self._chunk_dicts(doc)
        self._chunks.extend(fresh)
        self._sync_store(fresh, stale)
        return doc

    def import_dir(self, directory: str) -> dict[str, Any]:
        result = self.store.import_dir(directory)
        self._rebuild()
        return result

    def import_file(self, filename: str, content: str) -> dict[str, Any]:
        doc = self.store.import_file(filename, content)
        self._add_doc_chunks(doc)
        return doc

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    async def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        origin: str | None = None,
        *,
        tags: tuple[str, ...] = (),
        filters: Filters | None = None,
        graph_refs: tuple[str, ...] = (),
        graph_reason: str = "",
    ) -> list[dict[str, Any]]:
        """融合检索：元数据先过滤，再 RRF 融合关键词 + 向量（+ 按需图谱）。

        `origin` 是旧接口（两区过滤），保留；`filters` 是 M34 的结构化过滤
        （标签/来源/路径/时间），两者可同时给。
        """
        query = (query or "").strip()
        if not query:
            return []
        chunks = self._chunks if origin is None else [c for c in self._chunks if c["origin"] == origin]
        if not chunks:
            return []
        top_k = max(1, int(top_k))

        effective = filters or Filters()
        if origin and not effective.origins:
            effective = Filters(
                kinds=effective.kinds,
                origins=(origin,),
                tags=effective.tags,
                path_prefix=effective.path_prefix,
                path_exact=effective.path_exact,
                symbol_id=effective.symbol_id,
                contract_id=effective.contract_id,
                since=effective.since,
            )
        if tags and not effective.tags:
            effective.tags = tuple(tags)

        records = [self._to_chunk(c) for c in chunks]
        # M34：chunk 元数据**总是**同步进 SQL 副本（写很便宜，也让阈值切换时数据已经就绪）；
        # 但**读取**是否走 SQL 由块数自动决定——小库内存更快（2 万块实测 1.9 ms vs 7.8 ms）。
        records, active = pick_chunks(records, store=self.chunk_store)
        lexical_refs: list[str] | None = None
        truncated = False
        if active is not None:
            records, lexical_refs, truncated = active.candidates(query, effective)

        vectors: dict[str, list[float]] = {}
        query_vector: list[float] | None = None
        if self.embedder.enabled:
            # 只给"要参与排序的块"算向量：被元数据排除的根本不该花 embedding 的时间。
            # SQL 路径下 records 已经裁过，这一步是幂等的（同一套判据）。
            wanted = metadata_apply_filters(records, effective)
            texts = [c.text for c in wanted]
            got = await self.embedder.embed(texts) if texts else []
            if got:
                for chunk, vector in zip(wanted, got):
                    if vector:
                        vectors[chunk.ref] = vector
            query_embed = await self.embedder.embed([query])
            query_vector = query_embed[0] if query_embed else None

        hits = hybrid_mod.fusion_ranking(
            query,
            records,
            filters=effective,
            query_vector=query_vector,
            chunk_vectors=vectors,
            graph_refs=graph_refs,
            graph_reason=graph_reason,
            lexical_refs=lexical_refs,
        )
        by_ref = {c.ref: c for c in records}
        out: list[dict[str, Any]] = []
        for hit in hits[:top_k]:
            chunk = by_ref.get(hit.ref)
            if chunk is None:
                continue
            out.append(self._hit_dict(hit, chunk))
        if truncated and out:
            # 截断必须说出来：否则排序会悄悄取决于"文件名顺序"，那是没法排查的 bug
            out[0]["truncated"] = True
            out[0]["note"] = (
                "候选集按路径截断（块数超过上限）：字面精确命中不受影响，"
                "语义召回可能不全。加个过滤条件（域/路径/标签）可以缩小到完整范围。"
            )
        return out

    def _to_chunk(self, chunk: dict[str, Any]) -> Chunk:
        """块 → 检索层记录（元数据在这里对齐，只此一处）。"""
        return Chunk(
            ref=chunk["key"],
            text=chunk["text"],
            kind="kb",
            title=chunk.get("title") or "",
            origin=chunk.get("origin") or AREA_HUMAN,
            path=str(chunk.get("source") or ""),
            tags=tuple(chunk.get("tags") or ()),
            ts=float(chunk.get("ts") or 0),
            extra={"doc_id": chunk.get("doc_id"), "chunk": chunk.get("chunk")},
        )

    @staticmethod
    def _hit_dict(hit: Any, chunk: Chunk) -> dict[str, Any]:
        """结果形状（界面/API 依赖）：doc_id 与块序号从 `extra` 取，SQL 路径下也在。"""
        extra = (hit.meta.get("extra") or {}) if hit.meta else {}
        return {
            "doc_id": extra.get("doc_id") or "",
            "title": chunk.title,
            "origin": chunk.origin or AREA_HUMAN,
            "chunk": int(extra.get("chunk") or 0),
            "score": round(hit.score, 4),
            "text": chunk.text,
            "why": list(hit.why or []),
            "meta": hit.meta,
        }

    def stats(self) -> dict[str, Any]:
        human = self.store.list(AREA_HUMAN)
        pending = self.store.list(AREA_LLM)
        return {
            "docs": len(human),
            "pending": len(pending),  # 隔离区待审核条目
            "chunks": len(self._chunks),
            "embedding": self.embedder.enabled,
            "model": self.embedder.model if self.embedder.enabled else None,
            # M34：检索是融合的，把"融合了什么、缓存多少向量"如实报出来
            "retrieval": "fused(rrf: lexical+semantic+graph)",
            "vector_cache": self.embedder.stats() if hasattr(self.embedder, "stats") else {},
        }

    async def aclose(self) -> None:
        await self.embedder.aclose()
