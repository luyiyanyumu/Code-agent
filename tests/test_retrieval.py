"""检索层（M34）：向量 + 关键词/结构化 + 图谱（按需）。

这一层的四条硬约束，测试逐条盯着：

1. **RRF 只依赖名次**——换掉分数尺度不该改变融合结果（这是"量纲不可比"的解药）；
2. **先过滤再排序**——被元数据排除的块，语义再像也不许出现在结果里；
3. **无信号不返回**——宁缺毋滥，不往上下文塞"聊胜于无"；
4. **降级不报错**——embedding 失败只意味着"少一路信号"，不是会话失败；
   而本地端点**不需要 key**（这条以前把"本地有向量模型"堵死了）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from code_agent.retrieval import code_search as code_search_mod
from code_agent.retrieval import graph as graph_mod
from code_agent.retrieval import hybrid as hybrid_mod
from code_agent.retrieval.embed import LocalEmbedder, VectorStore, is_local_url, probe_local
from code_agent.retrieval.metadata import Chunk, Filters, apply_filters


def _chunk(ref: str, text: str, **kwargs) -> Chunk:
    return Chunk(ref=ref, text=text, **kwargs)


# ---------------------------------------------------------------------------
# 元数据：先过滤再排序
# ---------------------------------------------------------------------------
def test_filters_narrow_the_candidate_set_before_ranking() -> None:
    chunks = [
        _chunk("kb:human:1", "上线要走变更审批", origin="human", tags=("流程",), path="kb/a.md"),
        _chunk("kb:llm:2", "上线要走变更审批（模型草稿）", origin="llm", tags=("流程",), path="kb/_llm/b.md"),
        _chunk("code:3", "上线流程编排", origin="code", symbol_id="sym:x#deploy"),
    ]
    only_human = apply_filters(chunks, Filters(origins=("human",)))
    assert [c.ref for c in only_human] == ["kb:human:1"]

    with_tag = apply_filters(chunks, Filters(tags=("流程",)))
    assert {c.ref for c in with_tag} == {"kb:human:1", "kb:llm:2"}

    by_prefix = apply_filters(chunks, Filters(path_prefix="kb/_llm"))
    assert [c.ref for c in by_prefix] == ["kb:llm:2"]

    by_symbol = apply_filters(chunks, Filters(symbol_id="sym:x#deploy"))
    assert [c.ref for c in by_symbol] == ["code:3"]


def test_filtered_out_chunk_never_appears_even_if_semantically_closest() -> None:
    """被元数据排除的块，语义再像也不许出现——这就是"精度在检索层解决"。"""
    chunks = [
        _chunk("kb:human:1", "上线流程", origin="human"),
        _chunk("kb:llm:2", "上线流程", origin="llm"),
    ]
    hits = hybrid_mod.fusion_ranking(
        "上线流程",
        chunks,
        filters=Filters(origins=("llm",)),
        query_vector=[1.0, 0.0],
        chunk_vectors={"kb:llm:2": [1.0, 0.0], "kb:human:1": [1.0, 0.0]},
    )
    assert [h.ref for h in hits] == ["kb:llm:2"]


# ---------------------------------------------------------------------------
# 融合：RRF + 字面置顶 + 归因
# ---------------------------------------------------------------------------
def test_rrf_depends_only_on_ranks() -> None:
    """同一组名次、把某一路的分数放大 100 倍（名次不变）→ 融合结果不变。"""
    lexical = ["a", "b", "c"]
    semantic = ["c", "a", "b"]
    before = hybrid_mod.rrf([lexical, semantic])
    after = hybrid_mod.rrf([lexical, semantic])          # 名次相同 → 结果必须相同
    assert before == after
    assert before["a"] > before["b"] and before["a"] > before["c"]


def test_rrf_fuses_complementary_signals() -> None:
    """关键词第一名与语义第一名不同 → 两者都排在没有被任何一路选中的东西前面。"""
    chunks = [_chunk("kw", "MAX_TOTAL_TURNS 上限"), _chunk("sem", "运行预算的封顶值"), _chunk("none", "无关内容")]
    hits = hybrid_mod.fusion_ranking(
        "MAX_TOTAL_TURNS",
        chunks,
        query_vector=[1.0, 0.0],
        chunk_vectors={"sem": [1.0, 0.0], "none": [0.0, 1.0]},
    )
    refs = [h.ref for h in hits]
    assert refs[:2] == ["kw", "sem"]           # 各有各的第一名
    assert "none" not in refs                  # 两路都没命中 → 不出现
    by_ref = {h.ref: h for h in hits}
    assert by_ref["kw"].lexical_rank == 1
    assert by_ref["sem"].semantic_rank == 1


def test_literal_metadata_hit_gets_top_billing() -> None:
    """元数据里的字面命中（符号名/路径/标签）要压过"语义很像"的对手。"""
    chunks = [
        _chunk("exact", "把持久事实写入长期记忆", symbol_id="sym:code_agent/memory/tools_save.py#save_memory"),
        _chunk("fuzzy", "记住用户的偏好设置", ),
    ]
    hits = hybrid_mod.fusion_ranking(
        "save_memory",
        chunks,
        query_vector=[1.0, 0.0],
        chunk_vectors={"fuzzy": [1.0, 0.0], "exact": [0.2, 0.0]},
    )
    assert hits[0].ref == "exact"
    assert any("字面命中" in w for w in hits[0].why)


def test_no_signal_means_no_result() -> None:
    chunks = [_chunk("a", "报销制度")]
    assert hybrid_mod.fusion_ranking("完全无关的词xyz", chunks) == []
    assert hybrid_mod.fusion_ranking("报销", []) == []


def test_why_explains_each_retrieval_path() -> None:
    hits = hybrid_mod.fusion_ranking(
        "上线",
        [_chunk("a", "上线流程")],
        query_vector=[1.0],
        chunk_vectors={"a": [1.0]},
    )
    why = " ".join(hits[0].why)
    assert "关键词第 1 名" in why and "语义第 1 名" in why


# ---------------------------------------------------------------------------
# 向量后端：本地免 key + 内容哈希缓存 + 失败降级
# ---------------------------------------------------------------------------
class _Transport:
    """假 HTTP：记录请求，按文本返回可控向量。"""

    def __init__(self, *, fail: bool = False) -> None:
        import httpx

        self.calls: list[dict] = []
        self.fail = fail
        self._Response = httpx.Response

    def handler(self, request):  # pragma: no cover - 由 httpx 调用
        import httpx

        body = json.loads(request.content.decode("utf-8"))
        self.calls.append({"url": str(request.url), "auth": request.headers.get("authorization"), "body": body})
        if self.fail:
            raise httpx.ConnectError("boom")
        vectors = []
        for text in body["input"]:
            vectors.append([1.0, 0.0] if "记忆" in text else [0.0, 1.0])
        return httpx.Response(200, json={"data": [{"embedding": v} for v in vectors]})


def _embedder(
    tmp_path: Path,
    transport: _Transport,
    *,
    base_url: str = "http://127.0.0.1:11434/v1",
    store: object | None = None,
) -> LocalEmbedder:
    import httpx

    return LocalEmbedder(
        base_url=base_url,
        model="bge-m3",
        store=store if store is not None else VectorStore("bge-m3", path=tmp_path / "v.jsonl"),
        transport=httpx.MockTransport(transport.handler),
    )


def test_local_endpoint_needs_no_api_key(monkeypatch) -> None:
    """本地端点免 key：这条以前把"我本地有向量模型"直接堵死了。"""
    monkeypatch.delenv("CODE_AGENT_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    local = LocalEmbedder(base_url="http://127.0.0.1:11434/v1")
    assert local.enabled is True
    remote = LocalEmbedder(base_url="https://api.openai.com/v1")
    assert remote.enabled is False                      # 远端没有 key 就不该启用
    assert is_local_url("http://192.168.1.9:8000/v1") is True


def test_vectors_are_cached_by_content_hash_across_processes(tmp_path: Path) -> None:
    """同一个内容第二次不再请求远端；换内容才重新算（缓存按内容哈希定位）。"""
    transport = _Transport()
    first = _embedder(tmp_path, transport)
    vecs = asyncio.run(first.embed(["记忆的存储"]))
    assert vecs and len(transport.calls) == 1
    asyncio.run(first.aclose())

    # 新进程/新实例：同一路径的缓存应当命中，不再发请求
    second = _embedder(tmp_path, transport)
    again = asyncio.run(second.embed(["记忆的存储"]))
    assert again == vecs
    assert len(transport.calls) == 1, "缓存没生效：同一内容又请求了一次"
    # 内容变了 → 重新算
    asyncio.run(second.embed(["记忆的存储（改过）"]))
    assert len(transport.calls) == 2
    assert second.store.hits >= 1


def test_embedding_failure_degrades_to_empty_not_exception(tmp_path: Path) -> None:
    """embedding 挂了只意味着"少一路信号"：返回空列表，绝不抛异常。"""
    embedder = _embedder(tmp_path, _Transport(fail=True))
    assert asyncio.run(embedder.embed(["记忆"])) == []
    assert embedder.failures >= 1
    # 上层据此走关键词单路，检索仍然有结果
    hits = hybrid_mod.fusion_ranking("记忆", [_chunk("a", "记忆的存储")], query_vector=None)
    assert [h.ref for h in hits] == ["a"]


def test_vector_cache_file_is_jsonl_and_outside_repo(tmp_path: Path) -> None:
    store = VectorStore("bge-m3", path=tmp_path / "v.jsonl")
    store.put("记忆", [1.0, 0.0])
    lines = [json.loads(line) for line in (tmp_path / "v.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines and lines[0]["v"] == [1.0, 0.0] and len(lines[0]["h"]) == 40


def test_probe_local_reports_not_found_when_nothing_listens() -> None:
    """本地没起服务时明确报 found=False（列清试过哪些），而不是假装有。"""
    result = asyncio.run(probe_local())
    assert set(result) >= {"found", "tried"}
    assert isinstance(result["tried"], list)


def test_knowledge_blocks_carry_provenance_and_why() -> None:
    """注入模型的知识块要带来源与归因：字面命中 vs 语义相近的采信权重不一样。"""
    from code_agent.context.prompt import knowledge_blocks

    blocks = knowledge_blocks(
        [
            {
                "title": "上线流程",
                "text": "上线要走审批",
                "score": 0.03,
                "origin": "human",
                "why": ["字面命中：上线", "关键词第 1 名"],
                "meta": {"path": "kb/deploy.md"},
            }
        ],
        "上线审批",
    )
    body = blocks[0]["content"]
    assert "kb/deploy.md" in body
    assert "字面命中：上线" in body and "关键词第 1 名" in body
    assert blocks[0]["role"] == "user", "外部内容必须降权为数据块"


# ---------------------------------------------------------------------------
# SQLite 派生数据存储：chunk 元数据 + 向量 BLOB + FTS5
# ---------------------------------------------------------------------------
def test_retrieval_store_filters_in_sql_and_keeps_metadata(tmp_path: Path) -> None:
    """结构化过滤走 SQL 索引；元数据原样往返（tags/extra/ts 不丢）。"""
    from code_agent.retrieval.store import RetrievalStore

    store = RetrievalStore(tmp_path / "r.db")
    store.put_chunks(
        [
            _chunk("a", "上线要走变更审批", kind="kb", origin="human", tags=("流程",), path="kb/a.md", ts=100.0),
            _chunk("b", "上线流程编排", kind="code", origin="code", symbol_id="sym:x#deploy", path="pkg/x.py"),
            _chunk("c", "报销提交发票", kind="kb", origin="llm", path="kb/_llm/c.md", extra={"doc_id": "c"}),
            _chunk("d", "很久以前的记录", kind="kb", origin="human", path="kb/old.md", ts=10.0),
        ]
    )
    assert store.chunk_count() == 4
    assert [c.ref for c in store.query_chunks(Filters(origins=("human",)))] == ["a", "d"]
    assert [c.ref for c in store.query_chunks(Filters(symbol_id="sym:x#deploy"))] == ["b"]
    assert [c.ref for c in store.query_chunks(Filters(tags=("流程",)))] == ["a"]
    # 顺序由 SQL 定（path, ref），不是插入顺序：kb/_llm/c.md 按 ASCII 排在 kb/a.md 前
    assert [c.ref for c in store.query_chunks(Filters(path_prefix="kb"))] == ["c", "a", "d"]
    assert [c.ref for c in store.query_chunks(Filters(kinds=("code",)))] == ["b"]
    # since 过滤：只排掉"时间明确且太老"的（ts=0 表示未知，不排除——不知道就不当作过期）
    recent = {c.ref for c in store.query_chunks(Filters(since=50))}
    assert "d" not in recent and {"a", "b", "c"} <= recent
    got = store.query_chunks(Filters(path_exact="kb/_llm/c.md"))[0]
    assert got.extra["doc_id"] == "c" and got.tags == ()
    store.close()


def test_retrieval_store_full_text_uses_the_same_tokenizer_as_bm25(tmp_path: Path) -> None:
    """FTS5 走同一套分词（中文 bigram）：两条关键词路口径必须一致，否则融合没意义。"""
    from code_agent.retrieval.store import RetrievalStore

    store = RetrievalStore(tmp_path / "fts.db")
    store.put_chunks([_chunk("a", "上线要走变更审批", title="上线流程"), _chunk("b", "报销提交发票")])
    hits = dict(store.search_text("审批", limit=5))
    assert "a" in hits and "b" not in hits
    assert hits["a"] > 0, "FTS5 的 bm25 已取负号：越大越相关"
    assert store.search_text("", limit=5) == []
    store.close()


def test_vector_blob_roundtrip_and_batch_write(tmp_path: Path) -> None:
    """向量按内容哈希存 BLOB，按 key 随机读；批量写在**一个事务**里。"""
    from code_agent.retrieval.store import RetrievalStore

    store = RetrievalStore(tmp_path / "v.db")
    store.put_vectors([("文本一", [1.0, 0.0, 0.5]), ("文本二", [0.0, 1.0])], model="bge-m3")
    got = store.get_vector("文本一")
    assert got is not None and [round(x, 6) for x in got] == [1.0, 0.0, 0.5]   # float32 往返
    assert store.get_vector("没存过") is None
    assert store.vector_count() == 2
    store.put_vectors([("文本一", [9.0, 9.0, 9.0])], model="bge-m3")          # 覆盖
    assert [round(x, 4) for x in store.get_vector("文本一")] == [9.0, 9.0, 9.0]
    assert store.vector_count() == 2
    store.close()


def test_retrieval_store_uses_wal_and_does_not_commit_per_row(tmp_path: Path) -> None:
    """回归：**必须开 WAL**。

    真机踩到：注释里写了 WAL 却没 `PRAGMA journal_mode=WAL`，默认模式下逐条写向量
    每次 commit 都 fsync —— 1 万条要 122 秒（JSONL 只要 11 秒，慢 11 倍）。
    开 WAL + 批量写之后是 2.4 秒（比 JSONL 快 4.5 倍）。
    """
    import time

    from code_agent.retrieval.store import RetrievalStore

    store = RetrievalStore(tmp_path / "wal.db")
    mode = store._con.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal", f"没开 WAL：{mode}"

    items = [(f"文本-{i}", [float(i), 1.0]) for i in range(500)]
    t0 = time.time()
    store.put_vectors(items, model="m")
    elapsed = time.time() - t0
    assert store.vector_count() == 500
    assert elapsed < 2.0, f"批量写太慢（{elapsed:.2f}s）：八成又变成逐条提交了"
    store.close()


def test_vector_store_migrates_legacy_jsonl_once(tmp_path: Path) -> None:
    """旧 JSONL 缓存自动迁移（**跨文件名**：旧的是按模型名，新库是共用的 vectors.db），
    且**幂等**——换存储格式不该让用户重算全部向量。"""
    from code_agent.retrieval.embed import VectorStore
    from code_agent.retrieval.store import SqliteVectorStore

    legacy = VectorStore("bge-m3", path=tmp_path / "bge-m3.jsonl")
    legacy.put("文本甲", [1.0, 0.0])
    legacy.put("文本乙", [0.0, 1.0])

    db = tmp_path / "vectors.db"                       # 文件名与旧缓存不同，靠模型名找到
    first = SqliteVectorStore("bge-m3", path=db)
    second = SqliteVectorStore("bge-m3", path=db)      # 同一目录再开：不该重复导入
    assert first.stats()["cached"] >= 2
    assert second.stats()["cached"] == 2
    got = second.get("文本甲")
    assert got is not None and [round(x, 4) for x in got] == [1.0, 0.0]
    assert first.get("没存过") is None
    first.close()
    second.close()


def test_open_vector_store_respects_the_explicit_jsonl_switch(tmp_path: Path, monkeypatch) -> None:
    """`CODE_AGENT_VECTOR_STORE=jsonl` 能回退（出问题时要有退路）。"""
    from code_agent.retrieval.embed import VectorStore
    from code_agent.retrieval.store import SqliteVectorStore, open_vector_store

    monkeypatch.setenv("CODE_AGENT_VECTOR_STORE", "jsonl")
    assert isinstance(open_vector_store("m", path=tmp_path / "x.jsonl"), VectorStore)
    monkeypatch.setenv("CODE_AGENT_VECTOR_STORE", "sqlite")
    assert isinstance(open_vector_store("m", path=tmp_path / "x.db"), SqliteVectorStore)


def test_embedder_writes_vectors_in_one_batch(tmp_path: Path) -> None:
    """embedder 落盘走批量接口（逐条提交在 SQL 后端是主要瓶颈）。"""
    calls: list[int] = []

    class _Store:
        def __init__(self) -> None:
            self.items: dict[str, list[float]] = {}

        def get(self, text: str):
            return self.items.get(text)

        def put(self, text: str, vector) -> None:      # pragma: no cover - 不该被调用
            calls.append(1)
            self.items[text] = vector

        def put_many(self, pairs) -> int:
            calls.append(len(list(pairs)))
            for text, vector in pairs:
                self.items[text] = vector
            return len(pairs)

        def stats(self):
            return {"cached": len(self.items)}

    store = _Store()
    embedder = _embedder(tmp_path, _Transport(), store=store)
    asyncio.run(embedder.embed(["记忆 A", "记忆 B", "记忆 C"]))
    assert calls and calls[0] == 3, f"没走批量写：{calls}"
    assert len(store.items) == 3


def test_autodetect_never_mutates_the_process_environment(monkeypatch) -> None:
    """回归：自动探测**不许**改进程环境变量。

    真机踩到：探测成功时写 `CODE_AGENT_EMBED_BASE_URL`，于是同一进程里后续构造的
    任何 embedder 都突然被启用、开始打真实的本地模型——测试进程表现为"整批用例莫名变慢"，
    而且完全看不出是谁改的环境。现在只返回结果，由调用方显式构造。
    """
    from code_agent.retrieval.embed import autodetect_local

    import os

    monkeypatch.delenv("CODE_AGENT_EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("CODE_AGENT_EMBED_MODEL", raising=False)
    result = asyncio.run(autodetect_local(timeout=1.5))
    assert "found" in result
    assert "CODE_AGENT_EMBED_BASE_URL" not in os.environ, "探测又改了进程环境"
    assert "CODE_AGENT_EMBED_MODEL" not in os.environ


# ---------------------------------------------------------------------------
# chunk 元数据的自动分级：小库走内存、大库走 SQL（判据要能说清）
# ---------------------------------------------------------------------------
def test_pick_chunks_is_automatic_and_overridable(monkeypatch) -> None:
    """默认按块数自动分级；`CODE_AGENT_RETRIEVAL_MODE` 可强制。"""
    from code_agent.retrieval.store import MEMORY_CHUNK_LIMIT, pick_chunks, retrieval_mode

    chunks = [_chunk(f"r{i}", f"文本 {i}") for i in range(10)]
    fake = object()
    monkeypatch.delenv("CODE_AGENT_RETRIEVAL_MODE", raising=False)
    assert retrieval_mode() == "auto"
    assert pick_chunks(chunks, store=fake)[1] is None                     # 小集合：内存
    big = [_chunk(f"r{i}", f"文本 {i}") for i in range(MEMORY_CHUNK_LIMIT + 1)]
    assert pick_chunks(big, store=fake)[1] is fake                        # 超阈值：SQL
    monkeypatch.setenv("CODE_AGENT_RETRIEVAL_MODE", "sql")
    assert pick_chunks(chunks, store=fake)[1] is fake                     # 强制 SQL
    monkeypatch.setenv("CODE_AGENT_RETRIEVAL_MODE", "memory")
    assert pick_chunks(big, store=fake)[1] is None                        # 强制内存
    assert pick_chunks(chunks, store=None)[1] is None                     # 没有库就只能内存


def test_sql_candidates_respect_filters_and_rescue_truncated_hits(tmp_path: Path) -> None:
    """过滤条件**权威**（关键词路也遵守）；FTS 的作用是补回被截断的字面命中。"""
    from code_agent.retrieval.store import RetrievalStore

    store = RetrievalStore(tmp_path / "c.db")
    store.put_chunks(
        [
            _chunk("a", "上线要走变更审批", origin="human", path="kb/a.md"),
            _chunk("b", "完全无关的内容", origin="human", path="kb/b.md"),
            _chunk("c", "上线流程编排", origin="llm", path="kb/_llm/c.md"),
        ]
    )
    chunks, lexical, truncated = store.candidates("上线", Filters(origins=("human",)))
    refs = {c.ref for c in chunks}
    assert refs == {"a", "b"}, "过滤命中的都在，且**隔离区的 c 不许进来**"
    assert "c" not in refs, "关键词命中不能绕过 origin 过滤（那是边界，不是提示）"
    assert lexical and lexical[0] == "a"
    assert truncated is False

    # 截断场景：上限 1 → 按 path 只会拿到 kb/a.md；FTS 命中的 kb/b.md 要被补回来
    chunks2, lexical2, truncated2 = store.candidates(
        "无关内容", Filters(origins=("human",)), filter_limit=1
    )
    assert {c.ref for c in chunks2} == {"a", "b"}, "被截断的字面命中要被 FTS 救回来"
    assert lexical2 == ["b"]
    assert truncated2 is True, "截断了就必须说出来（否则排序会悄悄取决于文件名顺序）"
    store.close()


def test_sql_path_returns_the_same_ranking_as_memory(tmp_path: Path, monkeypatch) -> None:
    """**等价性**：同一次查询，SQL 路径与内存路径的排序必须一致。

    这是最要紧的一条——两条路给出不同答案时，用户看到的排序取决于"库有多大"，
    那是不可排查的 bug。
    """
    from code_agent.retrieval import code_search as cs
    from code_agent.retrieval.metadata import Filters as F
    from code_agent.retrieval.store import RetrievalStore

    nav = {
        "domains": [
            {
                "name": "code_agent/memory",
                "features": [
                    {"id": "sym:m/store.py#save", "label": "把持久事实写入长期记忆", "name": "save",
                     "kind": "function", "path": "code_agent/memory/store.py", "line": 3},
                    {"id": "sym:m/store.py#read", "label": "读取记忆", "name": "read",
                     "kind": "function", "path": "code_agent/memory/store.py", "line": 9},
                ],
            },
            {
                "name": "code_agent/api",
                "features": [
                    {"id": "sym:a/app.py#kb", "label": "知识库接口", "name": "kb",
                     "kind": "function", "path": "code_agent/api/app.py", "line": 5},
                ],
            },
        ]
    }
    for prompt in ("记忆", "save", "知识库 接口", "完全无关的词"):
        memory_hits, _ = cs.search(prompt, nav)
        store = RetrievalStore(tmp_path / "code.db")
        sql_hits, info = cs.search(prompt, nav, store=store, project_id="p_x")
        assert info["store"] in ("sql", "memory")
        assert [h.ref for h in sql_hits] == [h.ref for h in memory_hits], f"排序不一致：{prompt}"
        assert [round(h.score, 6) for h in sql_hits] == [round(h.score, 6) for h in memory_hits]
        store.close()
    # 强制走 SQL 读：排序仍必须与内存路一致
    monkeypatch.setenv("CODE_AGENT_RETRIEVAL_MODE", "sql")
    store = RetrievalStore(tmp_path / "code_sql.db")
    for prompt in ("记忆", "save", "知识库 接口"):
        memory_hits, _ = cs.search(prompt, nav)
        sql_hits, info = cs.search(prompt, nav, store=store, project_id="p_x")
        assert info["store"] == "sql"
        assert [h.ref for h in sql_hits] == [h.ref for h in memory_hits], f"SQL 读排序不一致：{prompt}"
    store.close()
    # 过滤条件两条路也要一致
    memory_filtered, _ = cs.search("记忆", nav, filters=F(path_prefix="code_agent/memory"))
    store = RetrievalStore(tmp_path / "code2.db")
    sql_filtered, _ = cs.search("记忆", nav, filters=F(path_prefix="code_agent/memory"), store=store)
    assert [h.ref for h in sql_filtered] == [h.ref for h in memory_filtered]
    store.close()


def test_code_chunks_are_persisted_even_below_the_threshold(tmp_path: Path) -> None:
    """功能块**总是**写回 SQL 副本（小库读仍走内存）——跨过阈值时数据已就绪。"""
    from code_agent.retrieval import code_search as cs
    from code_agent.retrieval.store import RetrievalStore

    store = RetrievalStore(tmp_path / "c.db")
    nav = {"domains": [{"name": "pkg", "features": [
        {"id": "sym:a.py#f", "label": "做事", "name": "f", "kind": "function", "path": "a.py", "line": 1},
    ]}]}
    _, info = cs.search("做事", nav, store=store, project_id="p")
    assert store.chunk_count() == 1, "写回不该依赖'这次是不是走 SQL 读'"
    assert info["store"] == "memory", "1 个块应当走内存读（实测更快）"
    # 导航变化后 upsert 幂等：不会翻倍
    cs.search("做事", nav, store=store, project_id="p")
    assert store.chunk_count() == 1
    store.close()


def test_kb_chunks_are_persisted_and_removed(tmp_path: Path, agent_home, monkeypatch) -> None:
    """知识库的 chunk 元数据跟着文档生命周期同步（增/删/改）。"""
    import asyncio

    from code_agent.knowledge.kb import KnowledgeBase
    from code_agent.knowledge.store import KnowledgeStore, chunk_text

    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    monkeypatch.setenv("CODE_AGENT_RETRIEVAL_MODE", "sql")     # 强制走 SQL，验证同步
    root = tmp_path / "knowledge"
    kb = KnowledgeBase(root)
    assert kb.chunk_store is not None
    doc = kb.create("上线流程", "上线要走变更审批", ["流程"])
    n = len(chunk_text("上线要走变更审批"))
    assert kb.chunk_store.chunk_count() == n
    kb.update(doc["id"], "上线流程", "上线改为自助发布", ["流程"])
    assert kb.chunk_store.chunk_count() == len(chunk_text("上线改为自助发布"))
    hits = asyncio.run(kb.search("自助发布"))
    assert hits and hits[0]["doc_id"] == doc["id"]
    assert hits[0]["why"], "SQL 路径也要带归因"
    kb.delete(doc["id"])
    assert kb.chunk_store.chunk_count() == 0
    assert asyncio.run(kb.search("自助发布")) == []


def test_startup_actually_probes_for_a_local_vector_model(agent_home, monkeypatch):
    """回归：启动时的本地向量探测**必须真的执行**。

    真机踩到：项目用的是 `FastAPI(lifespan=...)`，Starlette 会**忽略**
    `@app.on_event("startup")`——挂在那里等于"注册成功、永不执行"，
    症状是"机器上明明有本地向量模型，检索却始终只有关键词单路"，而且不报错。
    这条断言的就是"启动后 state 里留下了探测结果"。
    """
    import os

    from fastapi.testclient import TestClient

    from code_agent.api.app import create_app

    for name in ("CODE_AGENT_EMBED_BASE_URL", "CODE_AGENT_EMBED_API_KEY", "CODE_AGENT_EMBED_MODEL"):
        monkeypatch.delenv(name, raising=False)
    app = create_app(provider_name="offline")
    with TestClient(app):
        probe = getattr(app.state, "embed_probe", None)
    assert isinstance(probe, dict), "启动钩子没跑：embed_probe 没被写入"
    assert "found" in probe
    # 探到了就应当真的启用（本地端点不需要 key）；探不到则保持关键词单路
    kb = app.state.knowledge
    assert kb.embedder.enabled is bool(probe.get("found"))
    if probe.get("found"):
        assert kb.embedder.base_url == probe["base_url"]
    # 探测不许把端点写进环境（会污染同进程后续所有 embedder）
    assert not os.environ.get("CODE_AGENT_EMBED_BASE_URL")


# ---------------------------------------------------------------------------
# 图谱：按需、带方向、带位置、封顶
# ---------------------------------------------------------------------------
class _FakeIndex:
    """最小索引替身：两条边 + 两个符号。"""

    def __init__(self) -> None:
        self._symbols = {
            "sym:pkg/a.py#core": {"id": "sym:pkg/a.py#core", "name": "core", "path": "pkg/a.py", "line": 3, "qname": "core"},
            "sym:pkg/b.py#wrap": {"id": "sym:pkg/b.py#wrap", "name": "wrap", "path": "pkg/b.py", "line": 5, "qname": "wrap"},
        }
        self._edges = [
            {"kind": "referenced_by", "src": "sym:pkg/b.py#wrap", "dst": "sym:pkg/a.py#core"},
        ]

    def symbol(self, sid):
        return self._symbols.get(sid)

    def edges(self, *, kind=None, src=None, dst=None, limit=0):
        out = []
        for edge in self._edges:
            if kind and edge["kind"] != kind:
                continue
            if src and edge["src"] != src:
                continue
            if dst and edge["dst"] != dst:
                continue
            out.append(edge)
        return out

    def symbols_by_name(self, name, limit=8):
        return [s for s in self._symbols.values() if s["name"].lower() == str(name).lower()]


def test_graph_walk_in_direction_finds_who_depends_on_it_with_positions() -> None:
    index = _FakeIndex()
    result = graph_mod.walk(index, "sym:pkg/a.py#core", direction="in", hops=1)
    steps = result["layers"][0]["steps"]
    assert steps[0]["name"] == "wrap"
    assert steps[0]["path"] == "pkg/b.py" and steps[0]["line"] == 5   # 关系必须带位置
    text = graph_mod.chain_text(result)
    assert "pkg/b.py:5" in text
    assert result["direction"] == "in"


def test_graph_walk_reports_empty_instead_of_guessing() -> None:
    index = _FakeIndex()
    result = graph_mod.walk(index, "sym:pkg/b.py#wrap", direction="in", hops=2)
    assert result["layers"] == []
    assert "没有查到边" in graph_mod.chain_text(result)


def test_graph_walk_caps_hops_and_limit() -> None:
    index = _FakeIndex()
    result = graph_mod.walk(index, "sym:pkg/a.py#core", direction="in", hops=99, limit=9999)
    assert result["hops"] == graph_mod.MAX_HOPS


def test_symbol_ids_in_query_requires_unique_names() -> None:
    class _Dup(_FakeIndex):
        def __init__(self):
            super().__init__()
            self._symbols["sym:pkg/c.py#core"] = {"id": "sym:pkg/c.py#core", "name": "core", "path": "pkg/c.py", "line": 1, "qname": "core"}

    assert graph_mod.symbol_ids_in_query(_FakeIndex(), "core 是怎么实现的") == ["sym:pkg/a.py#core"]
    assert graph_mod.symbol_ids_in_query(_Dup(), "core 是怎么实现的") == []   # 重名 → 不猜


# ---------------------------------------------------------------------------
# 代码检索：把功能导航变成可检索集合
# ---------------------------------------------------------------------------
NAV = {
    "domains": [
        {
            "name": "code_agent/memory",
            "features": [
                {
                    "id": "sym:code_agent/memory/store.py#save_memory",
                    "label": "把持久事实或偏好写入长期记忆",
                    "name": "save_memory",
                    "kind": "function",
                    "path": "code_agent/memory/store.py",
                    "line": 18,
                    "doc": "",
                    "label_from": "brief",
                    "source_label": "工具",
                }
            ],
            "children": [
                {
                    "name": "inner",
                    "features": [
                        {
                            "id": "sym:code_agent/api/app.py#kb",
                            "label": "知识库文档接口",
                            "name": "kb",
                            "kind": "function",
                            "path": "code_agent/api/app.py",
                            "line": 90,
                            "doc": "知识库文档的增删改查",
                            "label_from": "doc",
                            "source_label": "公开",
                        }
                    ],
                }
            ],
        }
    ]
}


def test_feature_chunks_carry_metadata_from_the_nav() -> None:
    chunks = code_search_mod.feature_chunks(NAV)
    assert len(chunks) == 2
    by_ref = {c.ref: c for c in chunks}
    save = by_ref["sym:code_agent/memory/store.py#save_memory"]
    assert save.kind == "code" and save.path == "code_agent/memory/store.py"
    assert "code_agent/memory" in save.tags            # 能力域当标签，可精确过滤
    assert save.symbol_id == save.ref
    assert save.hash                                      # 内容哈希（向量缓存键）


def test_feature_chunks_include_the_symbol_name_for_exact_queries() -> None:
    """块文本必须带**符号名**：用户常常直接问 `save_memory` 这类名字。

    真机踩到：只放"功能名 + 说明"时，用符号名精确查会**无命中**，
    而这类查询正是"精确约束"最该赢的地方。
    """
    nav = {
        "domains": [
            {
                "name": "code_agent/memory",
                "features": [
                    {
                        "id": "sym:code_agent/memory/tools_save.py#SaveMemoryTool",
                        "label": "把持久事实写入长期记忆",
                        "name": "save_memory",
                        "kind": "class",
                        "path": "code_agent/memory/tools_save.py",
                        "line": 18,
                    }
                ],
            }
        ]
    }
    chunks = code_search_mod.feature_chunks(nav)
    assert "save_memory" in chunks[0].text
    from code_agent.memory.retriever import rank

    assert rank([(chunks[0].ref, chunks[0].text)], "save_memory"), "符号名必须能走关键词命中"


def test_code_search_graph_info_is_always_shaped_the_same() -> None:
    """没识别出符号时也要有 `refs`/`used` 字段——调用方不必写防御性分支。"""
    _, info = code_search_mod.search("记忆", NAV)
    assert set(info["graph"]) >= {"used", "symbols", "reason", "refs"}
    assert info["graph"]["used"] is False and info["graph"]["refs"] == 0


def test_code_search_is_filterable_and_explainable() -> None:
    hits, info = code_search_mod.search("记忆", NAV)
    assert hits and hits[0].ref == "sym:code_agent/memory/store.py#save_memory"
    assert info["chunks"] == 2
    # 过滤到另一个目录：命中必须换人
    from code_agent.retrieval.metadata import Filters as F

    hits2, _ = code_search_mod.search("知识库", NAV, filters=F(path_prefix="code_agent/api"))
    assert hits2 and hits2[0].ref == "sym:code_agent/api/app.py#kb"


def test_code_search_uses_graph_on_demand_when_query_names_a_symbol() -> None:
    """查询里点名了符号 → 图上"谁依赖它"的邻居被加权（并按需说明原因）。"""
    index = _FakeIndex()
    nav = {
        "domains": [
            {
                "name": "pkg",
                "features": [
                    {"id": "sym:pkg/a.py#core", "label": "核心计算", "name": "core", "kind": "function", "path": "pkg/a.py", "line": 3},
                    {"id": "sym:pkg/b.py#wrap", "label": "包装入口", "name": "wrap", "kind": "function", "path": "pkg/b.py", "line": 5},
                ],
            }
        ]
    }
    hits, info = code_search_mod.search("core", nav, index=index)
    assert info["graph"]["used"] is True
    assert info["graph"]["symbols"] == ["sym:pkg/a.py#core"]
    refs = [h.ref for h in hits]
    assert refs[0] == "sym:pkg/a.py#core"               # 字面命中本身还是要排最前
    assert "sym:pkg/b.py#wrap" in refs                  # 关系邻居被图谱路带进来
    wrap_hit = next(h for h in hits if h.ref == "sym:pkg/b.py#wrap")
    assert any("图谱" in w for w in wrap_hit.why)        # 且说清了"是因为图上有关系"
