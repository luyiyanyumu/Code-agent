"""修改台账（M18 · P2）：每一次写入都留痕，可审查、可回滚。

为什么单独一个 SQLite 库（`~/.code-agent/code/<pid>/patches.db`）而不是一个 JSON 目录：
列表页要按时间倒序取最近 N 条，JSON 目录每次都要把所有文件读进来解析
（每条还带着改前改后的全文）——那是几十 MB 的无效 IO。库表把"元数据"与
"全文"分开存，列表只 SELECT 元数据列。

回滚的安全前提（很重要）：**只有当文件当前内容仍等于该次改动的结果时**才允许自动回滚。
否则说明改动之后又有人（或另一个 AI 会话）动过这个文件，自动写回会静默覆盖别人的工作。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 哈希口径统一由 hashing 提供（此处保留同名再导出，历史调用点无需改）
from code_agent.codeintel.hashing import sha1_file_text, sha1_text  # noqa: F401

SCHEMA = """
CREATE TABLE IF NOT EXISTS patches (
  id          TEXT PRIMARY KEY,
  ts          TEXT NOT NULL,
  tree_id     TEXT NOT NULL DEFAULT 'main',
  path        TEXT NOT NULL,
  session_id  TEXT NOT NULL DEFAULT '',
  tool        TEXT NOT NULL DEFAULT '',
  old_string  TEXT NOT NULL DEFAULT '',
  new_string  TEXT NOT NULL DEFAULT '',
  replace_all INTEGER NOT NULL DEFAULT 0,
  before_sha1 TEXT NOT NULL DEFAULT '',
  after_sha1  TEXT NOT NULL DEFAULT '',
  before_text TEXT NOT NULL DEFAULT '',
  after_text  TEXT NOT NULL DEFAULT '',
  inline      INTEGER NOT NULL DEFAULT 1,
  reverted_at TEXT NOT NULL DEFAULT '',
  note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_patches_ts ON patches(ts DESC);
CREATE INDEX IF NOT EXISTS idx_patches_path ON patches(path);
"""

#: 超过这个大小就不存全文（只留 old/new 片段与 sha1，回滚走反向替换）
INLINE_LIMIT = 200 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PatchStore:
    """单项目的修改台账。"""

    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _query_one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    def record(
        self,
        *,
        rel: str,
        session_id: str,
        tool: str,
        before: str,
        after: str,
        old_string: str = "",
        new_string: str = "",
        replace_all: bool = False,
        tree_id: str = "main",
        note: str = "",
    ) -> dict:
        """登记一次写入。返回元数据（不含全文）。"""
        pid = f"pt_{secrets.token_hex(6)}"
        inline = len(before) <= INLINE_LIMIT and len(after) <= INLINE_LIMIT
        with self._lock:
            self._conn.execute(
                "INSERT INTO patches (id, ts, tree_id, path, session_id, tool, old_string, new_string,"
                " replace_all, before_sha1, after_sha1, before_text, after_text, inline, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pid, _now(), tree_id, rel, session_id, tool,
                    old_string[:20000], new_string[:20000], 1 if replace_all else 0,
                    sha1_text(before), sha1_text(after),
                    before if inline else "", after if inline else "",
                    1 if inline else 0, note,
                ),
            )
            self._conn.commit()
        return self.meta(self.get(pid))

    def get(self, patch_id: str) -> dict | None:
        return self._query_one("SELECT * FROM patches WHERE id=?", (patch_id,))

    def list(self, *, limit: int = 50, path: str | None = None) -> list[dict]:
        sql = (
            "SELECT id, ts, tree_id, path, session_id, tool, replace_all, before_sha1, after_sha1,"
            " inline, reverted_at, note, length(before_text) AS before_len, length(after_text) AS after_len"
            " FROM patches"
        )
        params: list[Any] = []
        if path:
            sql += " WHERE path=?"
            params.append(path)
        sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        return [self.meta(row) for row in self._query_all(sql, tuple(params))]

    def list_by_session(self, session_id: str, *, since: str = "") -> list[dict]:
        """某会话在 `since` 之后落下的改动（M29 成对检查点用它认领"这一步改了什么"）。

        为什么用时间而不是"让工具自己报"：`edit_file` 记录的是**真实写盘**，
        任何旁路（插件工具、回滚自己）都会被台账看见；让工具自己报必然漏。
        ts 是秒级 ISO 串，所以用 `>=` 比较——同一秒内落下的改动也归当轮。
        """
        sql = (
            "SELECT id, ts, tree_id, path, session_id, tool, replace_all, before_sha1, after_sha1,"
            " inline, reverted_at, note, length(before_text) AS before_len, length(after_text) AS after_len"
            " FROM patches WHERE session_id=?"
        )
        params: list[Any] = [session_id]
        if since:
            sql += " AND ts>=?"
            params.append(since)
        sql += " ORDER BY ts ASC, rowid ASC"
        return [self.meta(row) for row in self._query_all(sql, tuple(params))]

    def mark_reverted(self, patch_id: str, *, at: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE patches SET reverted_at=? WHERE id=?", (at or _now(), patch_id)
            )
            self._conn.commit()

    def counts(self) -> dict:
        row = self._query_one(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN reverted_at='' THEN 1 ELSE 0 END) AS active"
            " FROM patches"
        )
        return {"total": int(row["total"] or 0) if row else 0,
                "active": int(row["active"] or 0) if row else 0}

    def counts_by_path(self, *, limit: int = 500) -> dict[str, int]:
        """每个文件被改过几次（地图上的 SRC 角标用它）。

        只数**没被回滚**的记录：回滚掉的那次改动已经不是当前状态的证据了。
        """
        rows = self._query_all(
            "SELECT path, COUNT(*) AS n FROM patches WHERE reverted_at=''"
            " GROUP BY path ORDER BY n DESC LIMIT ?",
            (int(limit),),
        )
        return {row["path"]: int(row["n"] or 0) for row in rows}

    @staticmethod
    def meta(row: dict | None) -> dict:
        """元数据视图（不含改前改后全文）。"""
        if not row:
            return {}
        return {
            "id": row["id"],
            "ts": row["ts"],
            "tree_id": row.get("tree_id") or "main",
            "path": row["path"],
            "session_id": row.get("session_id") or "",
            "tool": row.get("tool") or "",
            "replace_all": bool(row.get("replace_all")),
            "before_sha1": row.get("before_sha1") or "",
            "after_sha1": row.get("after_sha1") or "",
            "inline": bool(row.get("inline")),
            "reverted_at": row.get("reverted_at") or "",
            "note": row.get("note") or "",
            "before_len": int(row.get("before_len") or len(row.get("before_text") or "")),
            "after_len": int(row.get("after_len") or len(row.get("after_text") or "")),
        }

    @staticmethod
    def detail_view(row: dict, *, context: int = 3) -> dict:
        """改动详情：**直接给改前/改后全文**（小文件），让前端用原生 diff 渲染。

        早先只给 unified diff，前端再去反推左右两侧——那是有损的：
        纯新增/删除行会被错放到两边，看起来"两边一样"，反而让人怀疑改动是否生效。
        """
        meta = PatchStore.meta(row)
        meta["diff"] = PatchStore.diff_text(row, context=context)
        meta["before_text"] = row.get("before_text") or ""
        meta["after_text"] = row.get("after_text") or ""
        meta["old_string"] = (row.get("old_string") or "")[:4000]
        meta["new_string"] = (row.get("new_string") or "")[:4000]
        return meta

    @staticmethod
    def diff_text(row: dict, *, context: int = 3) -> str:
        """生成可读 diff（无第三方 diff 库：用 difflib，标准库）。"""
        import difflib

        before = row.get("before_text") or ""
        after = row.get("after_text") or ""
        if not before and not after:
            return "（本次改动未保存全文：文件过大，仅记录了替换片段）\n" + json.dumps(
                {"old": row.get("old_string", "")[:2000], "new": row.get("new_string", "")[:2000]},
                ensure_ascii=False,
                indent=2,
            )
        diff = difflib.unified_diff(
            before.split("\n"),
            after.split("\n"),
            fromfile=f"a/{row['path']}",
            tofile=f"b/{row['path']}",
            n=context,
            lineterm="",
        )
        return "\n".join(diff)
