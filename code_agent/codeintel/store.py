"""符号索引（M18 · P1）：每个项目一棵 SQLite 索引。

为什么是 SQLite 而不是一个大 JSON：
1. **增量**：按文件 upsert/删除，不用整图重写；
2. **子图查询**：UI 按需取一层/一类，模型按需取一个符号，payload 可控；
3. **增量订阅**：`changes` 表就是"地图实时 delta"的序列源（seq 单调），前端带
   `after_seq` 拉增量即可，不需要服务端维护每个客户端的状态。

存放位置：`~/.code-agent/code/<project_id>/index-<tree_id>.db`。
放这里而不是主库的原因：索引是可随时重建的派生数据，体量可能远大于业务库，
且 P4 的每个隔离工作树要各有一份 —— 混进主库会让备份/迁移变成灾难。

内容哈希的口径（决定 delta 噪声大小）：**不含行号**。
只改一行的插入会让后续所有符号行号平移，若把行号算进哈希，一次小改动就会
报"几百个符号变了"，地图上的改动高亮将彻底失效。行号单独存，供跳转使用。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  path       TEXT PRIMARY KEY,
  lang       TEXT NOT NULL DEFAULT '',
  loc        INTEGER NOT NULL DEFAULT 0,
  size       INTEGER NOT NULL DEFAULT 0,
  mtime      REAL NOT NULL DEFAULT 0,
  sha1       TEXT NOT NULL DEFAULT '',
  skipped    TEXT NOT NULL DEFAULT '',
  entrypoint INTEGER NOT NULL DEFAULT 0,
  confidence TEXT NOT NULL DEFAULT 'high',
  error      TEXT NOT NULL DEFAULT '',
  strategy   TEXT NOT NULL DEFAULT '',
  module_doc TEXT NOT NULL DEFAULT '',
  -- 单文件符号数被截断（生成代码 / 压缩代码）：地图与模型上下文都要知道"这里不全"
  symbols_capped INTEGER NOT NULL DEFAULT 0,
  parsed_at  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS symbols (
  id           TEXT PRIMARY KEY,
  path         TEXT NOT NULL,
  parent       TEXT NOT NULL DEFAULT '',
  kind         TEXT NOT NULL DEFAULT '',
  name         TEXT NOT NULL DEFAULT '',
  qname        TEXT NOT NULL DEFAULT '',
  line         INTEGER NOT NULL DEFAULT 0,
  end_line     INTEGER NOT NULL DEFAULT 0,
  signature    TEXT NOT NULL DEFAULT '',
  doc          TEXT NOT NULL DEFAULT '',
  params       TEXT NOT NULL DEFAULT '[]',
  returns      TEXT NOT NULL DEFAULT '',
  decorators   TEXT NOT NULL DEFAULT '[]',
  bases        TEXT NOT NULL DEFAULT '[]',
  complexity   INTEGER NOT NULL DEFAULT 0,
  confidence   TEXT NOT NULL DEFAULT 'high',
  content_hash TEXT NOT NULL DEFAULT '',
  body_hash    TEXT NOT NULL DEFAULT '',
  extra        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent);
CREATE TABLE IF NOT EXISTS edges (
  src   TEXT NOT NULL,
  dst   TEXT NOT NULL,
  kind  TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS changes (
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,
  kind    TEXT NOT NULL,
  node_id TEXT NOT NULL,
  path    TEXT NOT NULL DEFAULT '',
  detail  TEXT NOT NULL DEFAULT ''
);
-- M18 · P3：语言服务器诊断。**不参与地图版本号**：诊断会随打字频繁更新，
-- 若把它算进 version，地图会被反复整取；这里用独立的 diag_seq 通知前端。
CREATE TABLE IF NOT EXISTS diagnostics (
  path          TEXT NOT NULL,
  line          INTEGER NOT NULL DEFAULT 1,
  character     INTEGER NOT NULL DEFAULT 0,
  end_line      INTEGER NOT NULL DEFAULT 1,
  end_character INTEGER NOT NULL DEFAULT 0,
  severity      INTEGER NOT NULL DEFAULT 1,
  message       TEXT NOT NULL DEFAULT '',
  source        TEXT NOT NULL DEFAULT '',
  code          TEXT NOT NULL DEFAULT '',
  symbol_id     TEXT NOT NULL DEFAULT '',
  uri           TEXT NOT NULL DEFAULT '',
  ts            TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (path, line, character, message, source)
);
CREATE INDEX IF NOT EXISTS idx_diag_path ON diagnostics(path);
CREATE INDEX IF NOT EXISTS idx_diag_symbol ON diagnostics(symbol_id);
"""

#: 元信息键
META_SCHEMA_VERSION = "schema_version"
META_VERSION = "version"
META_LAST_SCAN = "last_scan_at"
META_ROOT_KEY = "root_key"
META_TREE = "tree_id"
META_SCAN_SUMMARY = "scan_summary"
#: 诊断变更计数（与地图 version 分开：诊断随打字更新，不该触发整图重取）
META_DIAG_SEQ = "diag_seq"

SCHEMA_VERSION = 5

#: 单文件符号上限。生成代码（protobuf stub / minified bundle）一个文件能出几万个符号，
#: 一次扫描就把索引、地图载荷与模型上下文全撑爆。截断**并留痕**（symbols_capped），
#: 让界面能显示"这个文件我们只索引了一部分"，而不是假装看全了。
MAX_SYMBOLS_PER_FILE = 2000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def symbol_id(rel_path: str, qname: str) -> str:
    return f"sym:{rel_path}#{qname}"


def file_id(rel_path: str) -> str:
    return f"file:{rel_path}"


def module_id(rel_dir: str) -> str:
    return f"mod:{rel_dir}"


def _content_hash(kind: str, payload: dict[str, Any]) -> str:
    """语义哈希：只覆盖"改了这个符号本身"才该变的东西。

    刻意**不含实现体**：一次插入会让下面的行号平移，若把整段源码算进来，
    小小的改动就会让一堆符号"变了"，地图的变更高亮与增量 delta 会被噪声淹没。
    实现体的变化用 `body_hash` 单独记录（审查/符号级 diff 用它）。
    """
    material = {"kind": kind}
    for key in ("qname", "signature", "params", "returns", "doc", "decorators", "bases"):
        if key in payload:
            material[key] = payload[key]
    extra = payload.get("extra") or {}
    if extra.get("lexical_identity"):
        material["java"] = {key: extra[key] for key in ("java_kind", "type", "type_signature", "parameters", "return_doc", "modifiers", "resolution_context_hash") if key in extra}
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _body_hash(source_lines: list[str], line: int, end_line: int) -> str:
    """符号实现体的哈希（按行切片，不含前后空白差异也要保留——它们是实现的一部分）。"""
    if not source_lines or line <= 0:
        return ""
    start = max(0, line - 1)
    stop = max(start + 1, min(len(source_lines), end_line or line))
    body = "\n".join(source_lines[start:stop])
    return hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()[:16]


def symbol_hashes(symbol: dict[str, Any], source: str) -> tuple[str, str]:
    """Use exact lexical declaration spans where available; preserve old parsers.

    Parameters use their owning callable as the business context: changing the
    implementation must invalidate a cached explanation of that parameter.
    Ranges are only used for hashing, never included in the content identity.
    """
    extra = symbol.get("extra") or {}
    span = extra.get("business_context_range") or extra.get("source_range")
    if isinstance(span, dict) and isinstance(span.get("start"), int) and isinstance(span.get("end"), int) and 0 <= span["start"] <= span["end"] <= len(source):
        body_hash = hashlib.sha1(source[span["start"]:span["end"]].encode("utf-8", errors="replace")).hexdigest()[:16]
    else:
        body_hash = _body_hash(source.split("\n"), int(symbol.get("line") or 0), int(symbol.get("end_line") or 0))
    return _content_hash(symbol.get("kind") or "variable", symbol), body_hash


class SymbolIndex:
    """单棵树（主干或某个隔离工作树）的符号索引。线程内同步使用。"""

    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 可重入锁：写路径会用"一个事务多语句"的形式持有锁，期间若再有查询
        # 走公共方法就会自锁（`remove_files` 踩过）。RLock 让这类嵌套不至于死锁。
        self._lock = threading.RLock()
        # 行号/模块说明会影响摘要，但不属于地图的语义 delta。
        self._digest_revision = 0
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # schema 版本不符：索引是**纯派生数据**，直接重建比写迁移脚本划算得多
        # （也能避免"新列没加、查询静默返回空"这类最难查的问题）
        stored = self.get_meta(META_SCHEMA_VERSION)
        if stored is not None and stored != str(SCHEMA_VERSION):
            with self._lock:
                for table in ("symbols", "edges", "files", "changes"):
                    self._conn.execute(f"DROP TABLE IF EXISTS {table}")
                self._conn.executescript(SCHEMA)
                self._conn.commit()
            stored = None
        if stored is None:
            self.set_meta(META_SCHEMA_VERSION, str(SCHEMA_VERSION))
        if self.get_meta(META_VERSION) is None:
            self.set_meta(META_VERSION, "0")

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def read_snapshot(self) -> Iterator[tuple[int, int]]:
        """在同一索引版本下完成多次读取；扫描写入和关闭会等待读取结束。"""
        with self._lock:
            yield self.version(), self._digest_revision

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    # ---- 事务内批量写（不逐条 commit）----
    # 性能事实：首次全量扫描一个 180 文件的仓库会产生 ~2500 条 changes。
    # 每条一次 commit = 每次一次 WAL fsync，实测把 12s 花在这上面（不是解析）。
    # 因此 apply_parse/mark_file/remove_files 全程只提交一次。
    def _change_row(self, kind: str, node_id: str, path: str, detail: str = "") -> tuple:
        return (now_iso(), kind, node_id, path, detail)

    def _bump_nc(self) -> None:
        """在已持有锁的事务内递增版本。"""
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (META_VERSION,)).fetchone()
        try:
            version = int(row["value"]) if row else 0
        except (TypeError, ValueError):
            version = 0
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (META_VERSION, str(version + 1)),
        )

    def query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # meta / version
    # ------------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def version(self) -> int:
        try:
            return int(self.get_meta(META_VERSION) or 0)
        except (TypeError, ValueError):
            return 0

    def _bump(self) -> int:
        version = self.version() + 1
        self.set_meta(META_VERSION, str(version))
        return version

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def file_rows(self) -> dict[str, dict]:
        return {row["path"]: row for row in self.query_all("SELECT * FROM files")}

    def file_row(self, rel: str) -> dict | None:
        return self.query_one("SELECT * FROM files WHERE path=?", (rel,))

    def all_symbols(self, *, kinds: list[str] | None = None, path: str | None = None, limit: int = 0) -> list[dict]:
        sql = "SELECT * FROM symbols"
        params: list[Any] = []
        where: list[str] = []
        if kinds:
            where.append(f"kind IN ({', '.join('?' for _ in kinds)})")
            params.extend(kinds)
        if path:
            where.append("path = ?")
            params.append(path)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY path, line"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query_all(sql, tuple(params))

    def symbol(self, sid: str) -> dict | None:
        return self.query_one("SELECT * FROM symbols WHERE id=?", (sid,))

    def symbols_by_name(self, name: str, *, limit: int = 8) -> list[dict]:
        """按**短名**精确查符号（大小写不敏感）。

        检索层"按需图谱"用它把查询里的词映射成符号：只认唯一命中，
        重名一律当"说不准"（与 logic 的调用解析同一原则）。
        """
        return self.query_all(
            "SELECT * FROM symbols WHERE name = ? COLLATE NOCASE ORDER BY path, line LIMIT ?",
            (str(name or "").strip(), max(1, int(limit))),
        )

    def symbols_for(self, rel: str) -> list[dict]:
        return self.query_all("SELECT * FROM symbols WHERE path=? ORDER BY line", (rel,))

    def edges(self, *, kind: str | None = None, src: str | None = None, dst: str | None = None,
              limit: int = 0) -> list[dict]:
        sql = "SELECT * FROM edges"
        params: list[Any] = []
        where: list[str] = []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if src:
            where.append("src=?")
            params.append(src)
        if dst:
            where.append("dst=?")
            params.append(dst)
        if where:
            sql += " WHERE " + " AND ".join(where)
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query_all(sql, tuple(params))

    def counts(self) -> dict[str, int]:
        out = {"files": 0, "symbols": 0, "edges": 0}
        row = self.query_one("SELECT COUNT(*) AS n FROM files WHERE skipped=''")
        out["files"] = int(row["n"]) if row else 0
        row = self.query_one("SELECT COUNT(*) AS n FROM symbols")
        out["symbols"] = int(row["n"]) if row else 0
        row = self.query_one("SELECT COUNT(*) AS n FROM edges")
        out["edges"] = int(row["n"]) if row else 0
        return out

    def kind_counts(self) -> dict[str, int]:
        rows = self.query_all("SELECT kind, COUNT(*) AS n FROM symbols GROUP BY kind ORDER BY n DESC")
        return {r["kind"]: int(r["n"]) for r in rows}

    def max_change_seq(self) -> int:
        row = self.query_one("SELECT COALESCE(MAX(seq), 0) AS s FROM changes")
        return int(row["s"]) if row else 0

    def changes_since(self, after_seq: int, limit: int = 2000) -> list[dict]:
        return self.query_all(
            "SELECT * FROM changes WHERE seq > ? ORDER BY seq LIMIT ?", (int(after_seq), int(limit))
        )

    def symbols_by_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        out: list[dict] = []
        for chunk in range(0, len(ids), 400):  # SQLite 变量上限保护
            part = ids[chunk : chunk + 400]
            out.extend(
                self.query_all(
                    f"SELECT * FROM symbols WHERE id IN ({', '.join('?' for _ in part)})", tuple(part)
                )
            )
        return out

    # ------------------------------------------------------------------
    # 写：按文件应用解析结果
    # ------------------------------------------------------------------
    def apply_parse(
        self,
        *,
        rel: str,
        lang: str,
        loc: int,
        size: int,
        mtime: float,
        sha1: str,
        parsed: dict[str, Any],
        edges: list[tuple[str, str, str, int]],
        skipped: str = "",
        source: str | None = None,
        max_symbols: int = MAX_SYMBOLS_PER_FILE,
    ) -> list[dict]:
        """把一次解析结果写入索引，返回本次产生的变更列表。

        edges 是 (src, dst, kind, count) 元组列表；只替换**以本文件为源**的边，
        反向边（别人 import 我）由对方的 apply_parse 负责——否则一次保存就要重算全图。

        整个方法在**一个事务**里完成（符号/边/文件行/变更日志/版本一起提交）：
        逐条 commit 会让首次全量扫描退化到十几秒（每条都是一次 WAL fsync）。
        """
        changes: list[dict] = []
        existed = self.file_row(rel) is not None
        old_symbols = {row["id"]: row for row in self.symbols_for(rel)}
        old_imports = {
            (row["src"], row["dst"], row["kind"], int(row["count"]))
            for row in self.edges(src=file_id(rel), kind="imports")
        }

        parsed_symbols = parsed.get("symbols", [])
        capped = 1 if len(parsed_symbols) > max_symbols else 0
        rows: list[tuple] = []
        seen: set[str] = set()
        for sym in parsed_symbols[:max_symbols]:
            sid = sym.get("id") or symbol_id(rel, sym.get("qname") or sym.get("name") or "")
            if sid in seen:  # 解析器可能对同一种子产生重复（如 TS 重载签名）
                continue
            seen.add(sid)
            kind = sym.get("kind") or "variable"
            chash, bhash = symbol_hashes(sym, source) if source is not None else (_content_hash(kind, sym), "")
            extra = dict(sym.get("extra") or {})
            extra.update({
                k: sym[k]
                for k in ("route_method", "route_path", "props", "emits", "hook")
                if k in sym
            })
            rows.append(
                (
                    sid, rel, sym.get("parent") or "", kind, sym.get("name") or "",
                    sym.get("qname") or sym.get("name") or "",
                    int(sym.get("line") or 0), int(sym.get("end_line") or sym.get("line") or 0),
                    (sym.get("signature") or "")[:400], (sym.get("doc") or "")[:1000],
                    json.dumps(sym.get("params") or [], ensure_ascii=False),
                    sym.get("returns") or "",
                    json.dumps(sym.get("decorators") or [], ensure_ascii=False),
                    json.dumps(sym.get("bases") or [], ensure_ascii=False),
                    int(sym.get("complexity") or 0), sym.get("confidence") or "high",
                    chash, bhash, json.dumps(extra, ensure_ascii=False),
                )
            )
            old = old_symbols.get(sid)
            if old is None:
                changes.append({"kind": "sym_added", "node_id": sid, "path": rel})
            elif old["content_hash"] != chash or (extra.get("lexical_identity") and old["body_hash"] != bhash):
                changes.append({"kind": "sym_updated", "node_id": sid, "path": rel})
            # 内容没变但行号变了：不算变更（否则一次插入会淹没 delta）
        for sid in old_symbols:
            if sid not in seen:
                changes.append({"kind": "sym_removed", "node_id": sid, "path": rel})

        if not existed:
            changes.insert(0, {"kind": "file_added", "node_id": file_id(rel), "path": rel})
        elif changes or old_imports != set(edges):
            changes.insert(0, {"kind": "file_updated", "node_id": file_id(rel), "path": rel})

        with self._lock:
            conn = self._conn
            conn.execute("DELETE FROM symbols WHERE path=?", (rel,))
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO symbols "
                    "(id, path, parent, kind, name, qname, line, end_line, signature, doc, params, returns, "
                    " decorators, bases, complexity, confidence, content_hash, body_hash, extra) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
            conn.execute("DELETE FROM edges WHERE src=? OR src LIKE ?", (file_id(rel), f"sym:{rel}#%"))
            if edges:
                conn.executemany(
                    "INSERT OR REPLACE INTO edges (src, dst, kind, count) VALUES (?,?,?,?)", edges
                )
            conn.execute(
                "INSERT INTO files (path, lang, loc, size, mtime, sha1, skipped, entrypoint, confidence, "
                " error, strategy, module_doc, symbols_capped, parsed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET lang=excluded.lang, loc=excluded.loc, size=excluded.size, "
                " mtime=excluded.mtime, sha1=excluded.sha1, skipped=excluded.skipped, "
                " entrypoint=excluded.entrypoint, confidence=excluded.confidence, error=excluded.error, "
                " strategy=excluded.strategy, module_doc=excluded.module_doc, "
                " symbols_capped=excluded.symbols_capped, parsed_at=excluded.parsed_at",
                (
                    rel, lang, loc, size, mtime, sha1, skipped,
                    1 if parsed.get("entrypoint") else 0,
                    parsed.get("confidence") or "high", (parsed.get("error") or "")[:500],
                    parsed.get("strategy") or "", (parsed.get("module_doc") or "")[:400],
                    capped, now_iso(),
                ),
            )
            if changes:
                conn.executemany(
                    "INSERT INTO changes (ts, kind, node_id, path, detail) VALUES (?,?,?,?,?)",
                    [self._change_row(c["kind"], c["node_id"], rel) for c in changes],
                )
                self._bump_nc()
            conn.commit()
            self._digest_revision += 1
        return changes

    def replace_import_edges(self, rel: str, edges: list[tuple[str, str, str, int]]) -> bool:
        """文件清单变化后刷新未改动文件的导入边，不重写符号/指纹。"""
        src = file_id(rel)
        with self._lock:
            conn = self._conn
            old = {
                (row["src"], row["dst"], row["kind"], int(row["count"]))
                for row in conn.execute("SELECT * FROM edges WHERE src=? AND kind='imports'", (src,))
            }
            if old == set(edges):
                return False
            conn.execute("DELETE FROM edges WHERE src=? AND kind='imports'", (src,))
            if edges:
                conn.executemany("INSERT INTO edges (src, dst, kind, count) VALUES (?,?,?,?)", edges)
            conn.execute(
                "INSERT INTO changes (ts, kind, node_id, path, detail) VALUES (?,?,?,?,?)",
                self._change_row("file_updated", src, rel),
            )
            self._bump_nc()
            conn.commit()
        return True

    def touch_file(self, rel: str, size: int, mtime: float) -> None:
        """指纹变了但内容哈希没变（如 git checkout 同内容）：只更新 stat，不产生变更。"""
        self.execute("UPDATE files SET size=?, mtime=?, parsed_at=? WHERE path=?", (size, mtime, now_iso(), rel))

    def mark_file(self, *, rel: str, lang: str, size: int, mtime: float, sha1: str, skipped: str) -> None:
        """登记"不解析"的文件（secret / too_large / binary）：只留文件节点，无符号。"""
        with self._lock:
            old = self.file_row(rel)
            existed = old is not None
            removed = self.symbols_for(rel)
            changed = not existed or bool(removed)
            digest_changed = changed or any(
                old[field] != value
                for field, value in (("lang", lang), ("skipped", skipped), ("loc", 0), ("module_doc", ""))
            )
            conn = self._conn
            conn.execute("DELETE FROM symbols WHERE path=?", (rel,))
            removed_edges = conn.execute(
                "DELETE FROM edges WHERE src=? OR src LIKE ?", (file_id(rel), f"sym:{rel}#%")
            ).rowcount
            conn.execute(
                "INSERT INTO files (path, lang, loc, size, mtime, sha1, skipped, entrypoint, confidence, "
                " error, strategy, module_doc, symbols_capped, parsed_at) "
                "VALUES (?,?,0,?,?,?,?,0,'high','','skip','',0,?) "
                "ON CONFLICT(path) DO UPDATE SET lang=excluded.lang, size=excluded.size, mtime=excluded.mtime, "
                " sha1=excluded.sha1, skipped=excluded.skipped, loc=0, error='', strategy='skip', "
                " module_doc='', symbols_capped=0, parsed_at=excluded.parsed_at",
                (rel, lang, size, mtime, sha1, skipped, now_iso()),
            )
            if changed:
                change_rows = [
                    self._change_row("file_added" if not existed else "file_updated", file_id(rel), rel)
                ]
                change_rows += [self._change_row("sym_removed", sym["id"], rel) for sym in removed]
                conn.executemany(
                    "INSERT INTO changes (ts, kind, node_id, path, detail) VALUES (?,?,?,?,?)", change_rows
                )
                self._bump_nc()
            conn.commit()
            # 跳过文件没有内容哈希，后台扫描可能反复登记；stat 更新不使摘要失效。
            if digest_changed or removed_edges:
                self._digest_revision += 1

    def remove_files(self, rels: list[str]) -> list[dict]:
        """删除文件及其符号，并清掉**指向它**的边（否则地图上会出现指向幽灵节点的线）。"""
        changes: list[dict] = []
        change_rows: list[tuple] = []
        # 先把待删符号查出来：查询会加锁，不能放在持锁的事务块里
        # （threading.Lock 不可重入，那样会直接死锁——曾经真的把测试挂死了）
        gone_by_rel = {rel: self.symbols_for(rel) for rel in rels}
        with self._lock:
            conn = self._conn
            for rel in rels:
                gone = gone_by_rel[rel]
                conn.execute("DELETE FROM symbols WHERE path=?", (rel,))
                conn.execute("DELETE FROM edges WHERE src=? OR src LIKE ?", (file_id(rel), f"sym:{rel}#%"))
                conn.execute("DELETE FROM edges WHERE dst=? OR dst LIKE ?", (file_id(rel), f"sym:{rel}#%"))
                conn.execute("DELETE FROM files WHERE path=?", (rel,))
                # 文件没了，它的诊断也必须一起清掉（否则地图上留着幽灵错误）
                conn.execute("DELETE FROM diagnostics WHERE path=?", (rel,))
                changes.append({"kind": "file_removed", "node_id": file_id(rel), "path": rel})
                change_rows.append(self._change_row("file_removed", file_id(rel), rel))
                for sym in gone:
                    changes.append({"kind": "sym_removed", "node_id": sym["id"], "path": rel})
                    change_rows.append(self._change_row("sym_removed", sym["id"], rel))
            if change_rows:
                conn.executemany(
                    "INSERT INTO changes (ts, kind, node_id, path, detail) VALUES (?,?,?,?,?)", change_rows
                )
                self._bump_nc()
            conn.commit()
        return changes

    def reset(self) -> None:
        """清空索引（全量重建前用）。version 与 changes 序列继续保留单调性。"""
        with self._lock:
            conn = self._conn
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM edges")
            conn.execute("DELETE FROM files")
            conn.execute(
                "INSERT INTO changes (ts, kind, node_id, path, detail) VALUES (?,?,?,?,?)",
                self._change_row("index_reset", "", ""),
            )
            self._bump_nc()
            conn.commit()

    def set_scan_summary(self, summary: dict) -> None:
        self.set_meta(META_LAST_SCAN, now_iso())
        self.set_meta(META_SCAN_SUMMARY, json.dumps(summary, ensure_ascii=False))

    def scan_summary(self) -> dict:
        raw = self.get_meta(META_SCAN_SUMMARY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------
    # 诊断（M18 · P3）
    # ------------------------------------------------------------------
    def diag_seq(self) -> int:
        try:
            return int(self.get_meta(META_DIAG_SEQ) or 0)
        except (TypeError, ValueError):
            return 0

    def set_diagnostics(self, *, rel: str, uri: str, items: list[dict]) -> int:
        """整文件替换诊断（语言服务器每次 publish 都是该文件的完整集合）。"""
        with self._lock:
            conn = self._conn
            conn.execute("DELETE FROM diagnostics WHERE path=?", (rel,))
            if items:
                conn.executemany(
                    "INSERT OR REPLACE INTO diagnostics "
                    "(path, line, character, end_line, end_character, severity, message, source, code,"
                    " symbol_id, uri, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            rel,
                            int(item.get("line") or 1),
                            int(item.get("character") or 0),
                            int(item.get("end_line") or item.get("line") or 1),
                            int(item.get("end_character") or 0),
                            int(item.get("severity") or 1),
                            str(item.get("message") or "")[:500],
                            str(item.get("source") or "")[:60],
                            str(item.get("code") or "")[:40],
                            str(item.get("symbol_id") or ""),
                            uri,
                            item.get("ts") or now_iso(),
                        )
                        for item in items
                    ],
                )
            row = conn.execute("SELECT value FROM meta WHERE key=?", (META_DIAG_SEQ,)).fetchone()
            try:
                seq = int(row["value"]) if row else 0
            except (TypeError, ValueError):
                seq = 0
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (META_DIAG_SEQ, str(seq + 1)),
            )
            conn.commit()
        return len(items)

    def diagnostics(
        self, *, path: str | None = None, severity: int | None = None, limit: int = 0
    ) -> list[dict]:
        sql = "SELECT * FROM diagnostics"
        params: list[Any] = []
        where: list[str] = []
        if path:
            where.append("path=?")
            params.append(path)
        if severity is not None:
            where.append("severity=?")
            params.append(int(severity))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY path, line, character"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query_all(sql, tuple(params))

    def diagnostic_summary(self) -> dict:
        """按严重级 + 按文件/符号聚合的计数（地图角标用；不含消息体，故很小）。"""
        counts = {"error": 0, "warning": 0, "info": 0, "hint": 0}
        for row in self.query_all("SELECT severity, COUNT(*) AS n FROM diagnostics GROUP BY severity"):
            key = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(int(row["severity"]), "error")
            counts[key] += int(row["n"])
        files: dict[str, dict[str, int]] = {}
        for row in self.query_all(
            "SELECT path, severity, COUNT(*) AS n FROM diagnostics GROUP BY path, severity"
        ):
            key = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(int(row["severity"]), "error")
            files.setdefault(row["path"], {})[key] = int(row["n"])
        symbols: dict[str, dict[str, int]] = {}
        for row in self.query_all(
            "SELECT symbol_id, severity, COUNT(*) AS n FROM diagnostics "
            "WHERE symbol_id <> '' GROUP BY symbol_id, severity"
        ):
            key = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(int(row["severity"]), "error")
            symbols.setdefault(row["symbol_id"], {})[key] = int(row["n"])
        return {"seq": self.diag_seq(), "counts": counts, "files": files, "symbols": symbols}

    def symbol_at(self, rel: str, line: int) -> str:
        """把某一行归到最内层符号（诊断挂到地图节点上时用）。"""
        row = self.query_one(
            "SELECT id FROM symbols WHERE path=? AND line<=? AND end_line>=? "
            "ORDER BY (end_line - line) ASC, line DESC LIMIT 1",
            (rel, int(line), int(line)),
        )
        return row["id"] if row else ""

    def clear_diagnostics(self, *, path: str | None = None) -> None:
        if path:
            self.execute("DELETE FROM diagnostics WHERE path=?", (path,))
        else:
            self.execute("DELETE FROM diagnostics")
