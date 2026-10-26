"""派生检索数据的 SQLite 存储（M34）：chunk 元数据 + 向量 BLOB + FTS5 关键词索引。

**为什么这部分该进 SQL，而别处不该**——先划清界线，避免"什么都塞数据库"：

| 数据 | 存哪 | 为什么 |
|---|---|---|
| 知识文档 / 记忆（权威内容） | Markdown 文件 | 人可编辑、可 diff、可 git；塞进 DB 就丢了这三点 |
| 轨迹 / 决策 / 检查点 | JSONL | 只增不改的叙事，可 grep、可导出 |
| 代码索引 / 台账 / 平台状态 | **已经是 SQLite** | 关系查询 + 事务 |
| **检索派生数据（本模块）** | **SQLite** | **可重建**、查询是带条件的集合运算、规模随仓库线性涨 |

三条具体的工程理由（都是实测过的数字，不是"理论上更好"）：

1. **JSONL 必须整份解析**：882 条向量 11.65 MB → 载入 243 ms；按这个斜率 1 万条约 2.7 s、
   10 万条约 27 s。而"按 key 随机读一条"在 SQLite 里是 O(1)（BLOB 列 + 主键索引）。
2. **过滤是集合运算**：按 origin/path/symbol/契约 过滤，SQL 用索引做 `WHERE`，
   比在 Python 里遍历列表既快又省内存（候选集不进内存）。
3. **FTS5 是内置的**：`sqlite3 3.53` 自带 FTS5，还自带 `bm25()` 排序函数——
   不需要新依赖就能把关键词路从"扫全表 + 自己算 IDF"换成索引检索。
   中文：把文本按**与 BM25 同一套分词**（中文 bigram）落进 `tokens` 列，
   查询侧同样分词，保证两条路口径一致（口径不一致的混合排序等于随机）。

**刻意不做**：SQLite 不索引向量相似度（那要 `sqlite-vec` 这类扩展）。
本模块只解决"存与取"，相似度仍由上层算——但因为过滤已经把候选裁小，
上层算的是"几百条"而不是"全库"，这与 `hybrid.py` 的"先过滤再排序"是同一套思路。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from array import array
from pathlib import Path
from typing import Any, Iterable

from code_agent.retrieval.metadata import Chunk, Filters, text_hash

#: 表结构版本：换结构时改它（旧库会被重建，派生数据不需要迁移工程）
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chunks (
    ref         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT '',
    origin      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL DEFAULT '',
    symbol_id   TEXT NOT NULL DEFAULT '',
    contract_id TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '',      -- 逗号分隔：过滤用 LIKE，展示用 split
    ts          REAL NOT NULL DEFAULT 0,
    hash        TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    extra       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chunks_origin ON chunks(origin);
CREATE INDEX IF NOT EXISTS idx_chunks_path   ON chunks(path);
CREATE INDEX IF NOT EXISTS idx_chunks_symbol ON chunks(symbol_id);
CREATE INDEX IF NOT EXISTS idx_chunks_kind   ON chunks(kind);
CREATE TABLE IF NOT EXISTS vectors (
    hash  TEXT PRIMARY KEY,
    model TEXT NOT NULL DEFAULT '',
    dim   INTEGER NOT NULL DEFAULT 0,
    vec   BLOB NOT NULL                        -- float32 打包；比分数字典序小一个数量级
);
CREATE INDEX IF NOT EXISTS idx_vectors_model ON vectors(model);
-- 关键词索引：tokens 列存"与 BM25 同一套分词"的结果，查询侧同样分词
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    ref UNINDEXED, tokens, tokenize='unicode61'
);
"""


def _pack(vector: list[float]) -> bytes:
    """向量 → BLOB：float32 足够（相似度排序对 1e-7 的误差不敏感），比 float64 省一半。"""
    return array("f", [float(v) for v in vector]).tobytes()


def _unpack(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return list(values)


def _tokens_for(text: str) -> str:
    """与 `memory.retriever` 同一套分词（中文 bigram + 英文单词），否则两路口径不一致。"""
    from code_agent.memory.retriever import _tokenize

    return " ".join(_tokenize(text or ""))


#: FTS5 查询里最多用多少个词（长问题不必每个 bigram 都进索引查询）
FTS_MAX_TERMS = 64


def _fts_query(text: str) -> str:
    """把文本变成 FTS5 查询串：**词之间是 OR**，不是默认的 AND。

    这是一个真会咬人的坑：FTS5 里 `MATCH 'a b'` 是 **a AND b**，而 `memory.retriever`
    的 BM25 是把每个词的贡献**相加**（等价于"命中任意一个就有分"）。若照搬空格连接，
    中文 bigram 会让几乎所有查询都变成"必须命中全部 bigram"——SQL 路的关键词召回
    会比内存路严格得多，而两条路的排序本该一致。所以显式写成 `OR`。
    """
    tokens = [t for t in _tokens_for(text).split(" ") if t][:FTS_MAX_TERMS]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


class RetrievalStore:
    """检索派生数据的 SQLite 存储（连接按需打开，WAL 模式，允许并发读）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        # WAL + NORMAL：**必须显式设**。默认 journal 模式下每次 commit 都要 fsync，
        # 于是"逐条写向量"会慢到不可接受（实测 1 万条 122 s vs JSONL 11 s，慢 11 倍）。
        # WAL 允许读写并发，NORMAL 在 WAL 下不会丢已提交事务（只可能丢最后一条崩溃前的）。
        try:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass                                   # 只读介质/网络盘：退默认值，不致命
        with self._con:
            self._con.executescript(_SCHEMA)
        version = self._get_meta("schema_version")
        if version and version != str(SCHEMA_VERSION):
            # 派生数据可以重建：版本不一致就清表重来，不做迁移工程
            with self._con:
                self._con.executescript("DELETE FROM chunks; DELETE FROM vectors; DELETE FROM chunks_fts;")
        self._set_meta("schema_version", str(SCHEMA_VERSION))

    # -- meta ---------------------------------------------------------
    def _get_meta(self, key: str) -> str:
        row = self._con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else ""

    def _set_meta(self, key: str, value: str) -> None:
        with self._con:
            self._con.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # -- chunks -------------------------------------------------------
    def put_chunks(self, chunks: Iterable[Chunk]) -> int:
        rows = []
        fts = []
        for chunk in chunks:
            rows.append(
                (
                    chunk.ref,
                    chunk.kind,
                    chunk.origin,
                    chunk.title,
                    chunk.path,
                    chunk.symbol_id,
                    chunk.contract_id,
                    ",".join(chunk.tags),
                    float(chunk.ts),
                    chunk.hash,
                    chunk.text,
                    json.dumps(chunk.extra or {}, ensure_ascii=False),
                )
            )
            fts.append((chunk.ref, _tokens_for(f"{chunk.title} {chunk.text}")))
        with self._con:
            self._con.executemany(
                "INSERT INTO chunks(ref, kind, origin, title, path, symbol_id, contract_id, tags, ts, hash, text, extra)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(ref) DO UPDATE SET kind=excluded.kind, origin=excluded.origin,"
                " title=excluded.title, path=excluded.path, symbol_id=excluded.symbol_id,"
                " contract_id=excluded.contract_id, tags=excluded.tags, ts=excluded.ts,"
                " hash=excluded.hash, text=excluded.text, extra=excluded.extra",
                rows,
            )
            for ref, _tokens in fts:
                self._con.execute("DELETE FROM chunks_fts WHERE ref=?", (ref,))
            self._con.executemany("INSERT INTO chunks_fts(ref, tokens) VALUES(?, ?)", fts)
        return len(rows)

    def _filter_sql(self, filters: Filters | None, *, prefix: str = "") -> tuple[str, list[Any]]:
        """把 Filters 翻译成 SQL 条件（`prefix` 用于 JOIN 时的表别名）。"""
        if filters is None or filters.empty:
            return "", []
        col = lambda name: f"{prefix}{name}"  # noqa: E731
        where: list[str] = []
        params: list[Any] = []
        if filters.kinds:
            where.append(f"{col('kind')} IN ({', '.join('?' for _ in filters.kinds)})")
            params.extend(filters.kinds)
        if filters.origins:
            where.append(f"{col('origin')} IN ({', '.join('?' for _ in filters.origins)})")
            params.extend(filters.origins)
        if filters.path_exact:
            where.append(f"{col('path')} = ?")
            params.append(filters.path_exact.replace("\\", "/"))
        if filters.path_prefix:
            prefix_path = filters.path_prefix.replace("\\", "/").rstrip("/")
            where.append(f"({col('path')} = ? OR {col('path')} LIKE ?)")
            params.extend([prefix_path, prefix_path + "/%"])
        if filters.symbol_id:
            where.append(f"{col('symbol_id')} = ?")
            params.append(filters.symbol_id)
        if filters.contract_id:
            where.append(f"{col('contract_id')} = ?")
            params.append(filters.contract_id)
        if filters.since:
            where.append(f"({col('ts')} = 0 OR {col('ts')} >= ?)")
            params.append(float(filters.since))
        if filters.tags:
            where.append("(" + " OR ".join(f"{col('tags')} LIKE ?" for _ in filters.tags) + ")")
            params.extend(f"%{tag}%" for tag in filters.tags)
        return (" AND ".join(where), params) if where else ("", [])

    def query_chunks(self, filters: Filters | None = None, *, limit: int = 0) -> list[Chunk]:
        """带条件的集合查询（索引命中，不需要把全表读进 Python）。"""
        where, params = self._filter_sql(filters)
        sql = "SELECT * FROM chunks"
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY path, ref"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._row_to_chunk(row) for row in self._con.execute(sql, tuple(params))]

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> Chunk:
        tags = tuple(t for t in str(row["tags"] or "").split(",") if t)
        try:
            extra = json.loads(row["extra"] or "{}")
        except json.JSONDecodeError:
            extra = {}
        return Chunk(
            ref=str(row["ref"]),
            text=str(row["text"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            origin=str(row["origin"]),
            path=str(row["path"]),
            symbol_id=str(row["symbol_id"]),
            contract_id=str(row["contract_id"]),
            tags=tags,
            ts=float(row["ts"] or 0),
            extra=extra if isinstance(extra, dict) else {},
        )

    def chunk_count(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])

    # -- 关键词（FTS5 内置 bm25）---------------------------------------
    def search_text(
        self, query: str, *, limit: int = 20, filters: Filters | None = None
    ) -> list[tuple[str, float]]:
        """FTS5 检索（内置 `bm25()`），返回 [(ref, 分数)]（分数取 -bm25，越大越相关）。

        **过滤在 SQL 里 JOIN 完成**（不是先物化允许集再筛）：过滤条件是权威边界
        （例如 `origin=human` 表示"不要隔离区草稿"），关键词路同样必须遵守它——
        否则"字面命中"会变成一个绕过过滤的口子。
        """
        tokens = _fts_query(query)
        if not tokens:
            return []
        where, params = self._filter_sql(filters, prefix="c.")
        sql = (
            "SELECT f.ref AS ref, bm25(chunks_fts) AS score FROM chunks_fts f"
            " JOIN chunks c ON c.ref = f.ref"
            " WHERE chunks_fts MATCH ?"
        )
        if where:
            sql += " AND " + where
        sql += " ORDER BY score LIMIT ?"
        rows = self._con.execute(sql, (tokens, *params, max(1, int(limit)))).fetchall()
        return [(str(row["ref"]), -float(row["score"])) for row in rows]

    def count_chunks(self, filters: Filters | None = None) -> int:
        """满足过滤条件的块数（判断是否被截断要用它，而不是结果长度）。"""
        where, params = self._filter_sql(filters)
        sql = "SELECT COUNT(*) AS n FROM chunks"
        if where:
            sql += " WHERE " + where
        return int(self._con.execute(sql, tuple(params)).fetchone()["n"])

    # -- 向量 ---------------------------------------------------------
    def get_vector(self, text: str) -> list[float] | None:
        row = self._con.execute("SELECT vec FROM vectors WHERE hash=?", (text_hash(text),)).fetchone()
        return _unpack(row["vec"]) if row is not None else None

    def put_vector(self, text: str, vector: list[float], *, model: str = "") -> None:
        if not vector:
            return
        with self._con:
            self._con.execute(
                "INSERT INTO vectors(hash, model, dim, vec) VALUES(?,?,?,?)"
                " ON CONFLICT(hash) DO UPDATE SET model=excluded.model, dim=excluded.dim, vec=excluded.vec",
                (text_hash(text), str(model or ""), len(vector), _pack(vector)),
            )

    def put_vectors(self, items: Iterable[tuple[str, list[float]]], *, model: str = "") -> int:
        """批量写（**一个事务**）。逐条写会慢一个数量级——每次 commit 都要落盘。"""
        rows = [
            (text_hash(text), str(model or ""), len(vector), _pack(vector))
            for text, vector in items
            if vector
        ]
        if not rows:
            return 0
        with self._con:
            self._con.executemany(
                "INSERT INTO vectors(hash, model, dim, vec) VALUES(?,?,?,?)"
                " ON CONFLICT(hash) DO UPDATE SET model=excluded.model, dim=excluded.dim, vec=excluded.vec",
                rows,
            )
        return len(rows)

    def vector_count(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) AS n FROM vectors").fetchone()["n"])

    def import_jsonl(self, path: Path | str, *, model: str = "") -> int:
        """从旧的 JSONL 向量缓存迁移（一行一个 {h, v}）。

        为什么要迁移而不是让用户重算：本地模型重算 882 条要几十秒，10 万条要几十分钟——
        换存储格式不该让用户付这个代价。
        """
        source = Path(path)
        if not source.is_file():
            return 0
        existing = {row["hash"] for row in self._con.execute("SELECT hash FROM vectors")}
        batch: list[tuple[str, str, int, bytes]] = []
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            key, vec = item.get("h"), item.get("v")
            if not isinstance(key, str) or not isinstance(vec, list) or not vec or key in existing:
                continue
            batch.append((key, model, len(vec), _pack(vec)))
            existing.add(key)
        if batch:
            with self._con:
                self._con.executemany(
                    "INSERT OR REPLACE INTO vectors(hash, model, dim, vec) VALUES(?,?,?,?)", batch
                )
        return len(batch)

    # -- 维护 ---------------------------------------------------------
    # -- 候选集：先裁再算（M34 第二步）---------------------------------
    def candidates(
        self,
        query: str,
        filters: Filters | None = None,
        *,
        fts_limit: int = 200,
        filter_limit: int = 2000,
    ) -> tuple[list[Chunk], list[str], bool]:
        """给检索层取候选集：**过滤条件权威**，在此前提下并入 FTS 的字面命中。

        返回 `(候选块, 关键词名次, 是否被截断)`。

        - 过滤条件（origin/path/symbol/契约/时间）是**边界**，关键词路也必须遵守：
          不能让"字面命中"变成绕过"不要隔离区草稿"的口子。所以 FTS 也带过滤。
        - 那并集还有什么用：结构化过滤的行数上限（`filter_limit`）是按 path 截断的，
          被截掉的那部分里可能有字面命中——**FTS 命中会被单独补回来**，
          于是"截断只影响向量路的召回，不影响字面精确命中"。
        - `truncated=True` 表示过滤命中数超过上限：调用方必须把这件事说出来
          （否则排序会悄悄取决于文件名顺序，那是没法排查的 bug）。
        """
        lexical = [ref for ref, _score in self.search_text(query, limit=fts_limit, filters=filters)]
        total = self.count_chunks(filters)
        rows = self.query_chunks(filters, limit=filter_limit)
        seen = {c.ref for c in rows}
        for ref in lexical:
            if ref in seen:
                continue
            chunk = self.get_chunk(ref)
            if chunk is not None:
                rows.append(chunk)
                seen.add(ref)
        return rows, lexical, total > filter_limit

    def get_chunk(self, ref: str) -> Chunk | None:
        row = self._con.execute("SELECT * FROM chunks WHERE ref=?", (ref,)).fetchone()
        return self._row_to_chunk(row) if row is not None else None

    def delete_chunks(self, refs: Iterable[str]) -> int:
        """删块（文档被删/功能消失时必须同步，否则检索会召回已不存在的东西）。"""
        items = [str(r) for r in refs if str(r)]
        if not items:
            return 0
        with self._con:
            self._con.executemany("DELETE FROM chunks WHERE ref=?", [(r,) for r in items])
            self._con.executemany("DELETE FROM chunks_fts WHERE ref=?", [(r,) for r in items])
        return len(items)

    def clear(self, *, kind: str = "") -> int:
        """清空（可按 kind）：重建索引用，派生数据不需要增量迁移工程。"""
        if kind:
            refs = [r["ref"] for r in self._con.execute("SELECT ref FROM chunks WHERE kind=?", (kind,))]
            self.delete_chunks(refs)
        else:
            with self._con:
                self._con.executescript("DELETE FROM chunks; DELETE FROM chunks_fts;")
        return self.chunk_count()

    def stats(self) -> dict[str, Any]:
        return {
            "backend": "sqlite",
            "path": str(self.path),
            "chunks": self.chunk_count(),
            "vectors": self.vector_count(),
            "size_mb": round(self.path.stat().st_size / 1024 / 1024, 2) if self.path.is_file() else 0.0,
        }

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # noqa: BLE001
            pass


class SqliteVectorStore:
    """`VectorStore`（JSONL）的 SQLite 版：接口相同，随机读、不整份解析。"""

    def __init__(self, model: str, path: Path | str | None = None) -> None:
        from code_agent.config import code_dir

        self.model = model
        self.path = Path(path) if path else code_dir() / "embeddings" / "vectors.db"
        self._store = RetrievalStore(self.path)
        self.hits = 0
        self.misses = 0
        self.imported = self._migrate_legacy(model)

    def _migrate_legacy(self, model: str) -> int:
        """把**同目录下的旧 JSONL 缓存**迁进来（幂等：已在库里的跳过）。

        为什么要找多个候选名：旧格式是"一个模型一个文件"（`bge-m3.jsonl`），
        新库是共用的 `vectors.db`——**文件名不同、目录相同**。
        只按"同名换后缀"去找（`vectors.jsonl`）会漏掉真实存在的那份，
        表现为"换了后端就要重算全部向量"（本地模型 10 万条要几十分钟）。
        """
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(model))[:60]
        candidates = [
            self.path.with_suffix(".jsonl"),        # 同名换后缀
            self.path.parent / f"{safe}.jsonl",     # 旧格式：按模型名
        ]
        total = 0
        for candidate in candidates:
            if candidate.is_file():
                total += self._store.import_jsonl(candidate, model=model)
        return total

    def get(self, text: str) -> list[float] | None:
        vector = self._store.get_vector(text)
        if vector is None:
            self.misses += 1
        else:
            self.hits += 1
        return vector

    def put(self, text: str, vector: list[float]) -> None:
        self._store.put_vector(text, vector, model=self.model)

    def put_many(self, items: Iterable[tuple[str, list[float]]]) -> int:
        return self._store.put_vectors(items, model=self.model)

    def import_jsonl(self, path: Path | str) -> int:
        self.imported += self._store.import_jsonl(path, model=self.model)
        return self.imported

    def stats(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "cached": self._store.vector_count(),
            "path": str(self.path),
            "hits": self.hits,
            "misses": self.misses,
            "imported_from_jsonl": getattr(self, "imported", 0),
            "backend": "sqlite",
        }

    def close(self) -> None:
        self._store.close()


def open_vector_store(model: str, path: Path | str | None = None) -> Any:
    """按配置选向量后端：默认 SQLite（随机读、可索引），`CODE_AGENT_VECTOR_STORE=jsonl` 回退。

    默认切到 SQLite 的理由是"载入耗时与规模无关"：JSONL 每次进程启动都要整份解析，
    10 万条就是几十秒；而 SQLite 只读用到的那几行。旧 JSONL 会自动迁移。
    """
    backend = (os.environ.get("CODE_AGENT_VECTOR_STORE") or "sqlite").strip().lower()
    if backend == "jsonl":
        from code_agent.retrieval.embed import VectorStore

        return VectorStore(model, path=path)
    if path is not None and str(path).endswith(".jsonl"):
        from code_agent.retrieval.embed import VectorStore

        return VectorStore(model, path=path)      # 显式给了 jsonl 路径就尊重它
    return SqliteVectorStore(model, path=path)


# ---------------------------------------------------------------------------
# chunk 元数据的自动分级（M34 第二步）
# ---------------------------------------------------------------------------
#: 内存路径的上限。**这个数是量出来的，不是拍的**：
#: 本机实测 2 万块时"内存过滤 1.9 ms vs SQL 7.8 ms"，所以在这个量级内存更快；
#: 真正的分水岭不是查询快慢，而是"**启动/首次访问要不要把全量载入内存**"
#: （JSONL/内存路线是 O(N)，SQL 是 O(候选)）。默认 5000 块：常见项目仍在内存里，
#: 大仓库自动转 SQL，不需要人做决定。
MEMORY_CHUNK_LIMIT = int(os.environ.get("CODE_AGENT_RETRIEVAL_MEMORY_LIMIT", "5000"))


def retrieval_mode() -> str:
    """`memory` / `sql` / `auto`（默认）。auto = 按块数自动分级。"""
    mode = (os.environ.get("CODE_AGENT_RETRIEVAL_MODE") or "auto").strip().lower()
    return mode if mode in ("memory", "sql", "auto") else "auto"


def chunk_store(namespace: str, path: Path | str | None = None) -> "RetrievalStore | None":
    """按命名空间开 chunk 库：`kb` 一个，代码按项目一个（`code/<pid>`）。

    为什么按项目分库：符号 id（`sym:路径#名字`）只在项目内有意义，
    混在一个库里会让两个仓库的同名文件互相串味。而**向量库是全局的**——
    它按内容哈希索引，内容相同就该复用，这是内容寻址带来的好处。
    """
    from code_agent.config import code_dir

    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(namespace))[:60].strip("_")
    if not safe:
        return None
    target = Path(path) if path else code_dir() / "retrieval" / f"{safe}.db"
    try:
        return RetrievalStore(target)
    except sqlite3.Error:
        return None                                # 打不开就退内存路线，检索不该因此不可用


def pick_chunks(
    chunks: list[Any],
    *,
    store: "RetrievalStore | None",
) -> tuple[list[Any], "RetrievalStore | None"]:
    """自动分级：块数超过阈值才启用 SQL。

    返回 `(候选块, 生效的 store)`——`store=None` 表示"这次走内存"。
    把它放在一处，是为了让 KB 与代码检索**用同一套判据**（两处各写一遍迟早不一致）。
    """
    mode = retrieval_mode()
    if store is None or mode == "memory":
        return chunks, None
    if mode == "sql":
        return chunks, store
    return (chunks, None) if len(chunks) <= MEMORY_CHUNK_LIMIT else (chunks, store)
