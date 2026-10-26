"""SQLite 持久化（s21 前置）：WAL 模式 + 三张 M2 核心表 + DAO。

设计约束（设计文档 §3）：
- 业务层禁止写裸 SQL，只能走 Repo —— 将来切 PostgreSQL 只改这里
- M2 表：users / sessions / approvals（usage / automations 在 M5 加）
- sqlite3 同步调用 + 线程锁：M2 负载下足够，耗时 SQL 出现时再迁线程池
"""

from __future__ import annotations

import secrets
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user',
  disabled      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id),
  title      TEXT NOT NULL DEFAULT '',
  status     TEXT NOT NULL DEFAULT 'active',
  folder     TEXT NOT NULL DEFAULT '',
  permission_mode TEXT NOT NULL DEFAULT 'workspace-write',
  loop_mode  TEXT NOT NULL DEFAULT 'classic', -- Legacy storage only; never exposed or used for execution.
  web_enabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);

-- M14：工作区（侧栏分组）。sessions.folder 存名字，这里存"这个工作区存在"，
-- 否则空工作区没有会话承载就会凭空消失——用户点了「新建工作区」却看不到东西。
CREATE TABLE IF NOT EXISTS workspaces (
  user_id    TEXT NOT NULL,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS approvals (
  id         TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  tool_name  TEXT NOT NULL,
  command    TEXT NOT NULL,
  reason     TEXT NOT NULL DEFAULT '',
  purpose    TEXT NOT NULL DEFAULT '',
  effects    TEXT NOT NULL DEFAULT '[]',
  status     TEXT NOT NULL DEFAULT 'pending',
  decided_by TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_session ON approvals(session_id, status);

-- M5 s21：用量记录（每次 loop.run 一行）
-- M36：记**实际模型名**而不是档位——档位已去掉，而"哪个模型花了多少 token"
-- 本来就是这个页面要回答的问题。
CREATE TABLE IF NOT EXISTS usage (
  id             TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL,
  user_id        TEXT NOT NULL,
  model          TEXT NOT NULL DEFAULT '',
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  cached_tokens  INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id, created_at);

-- M5 s22：审计日志（append-only hash chain，防篡改）
CREATE TABLE IF NOT EXISTS audit_log (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  session_id TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  tool_name  TEXT NOT NULL,
  arguments  TEXT NOT NULL,
  outcome    TEXT NOT NULL,
  prev_hash  TEXT NOT NULL,
  entry_hash TEXT NOT NULL
);

-- M18：代码模式项目。项目 = 一个真实存在的目录（用户的仓库或平台新建的工程）。
-- sessions.project_id 指向这里；会话的工作区由项目根目录决定，而不是
-- workspaces/<session_id>——这是"代码模式复用整条 agent 链路"的关键接线。
-- root_key 是归一化后的路径（小写 + 正斜杠），Windows 大小写不一致不会冒出
-- 两个看起来一样的项目；唯一性判定也走它，不依赖 root_path 的原始拼写。
CREATE TABLE IF NOT EXISTS projects (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  name              TEXT NOT NULL,
  root_path         TEXT NOT NULL,
  root_key          TEXT NOT NULL,
  origin            TEXT NOT NULL DEFAULT 'open',   -- open | create
  template          TEXT NOT NULL DEFAULT '',       -- empty | python | node
  session_id        TEXT NOT NULL DEFAULT '',       -- 主会话（只读问答）
  base_branch       TEXT NOT NULL DEFAULT '',
  allow_direct_write INTEGER NOT NULL DEFAULT 0,    -- 直改主干开关（默认关，需显式开）
  git_mode          TEXT NOT NULL DEFAULT '',       -- git | none
  last_scan_at      TEXT,
  scan_summary      TEXT NOT NULL DEFAULT '{}',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_root ON projects(user_id, root_key);

-- M18 · P4：代码模式任务（AI 在隔离工作树里改代码，用户审批后合并）
-- worktree 是相对 ~/.code-agent 的？不——存绝对路径，因为用户可能配置过；真实路径以它为准。
CREATE TABLE IF NOT EXISTS tasks (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  requirement TEXT NOT NULL DEFAULT '',
  base_branch TEXT NOT NULL DEFAULT '',
  branch      TEXT NOT NULL DEFAULT '',
  worktree    TEXT NOT NULL DEFAULT '',
  session_id  TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'planning',   -- planning|planned|plan_failed|running|review|merged|discarded|failed
  plan_json   TEXT NOT NULL DEFAULT '{}',
  review_json TEXT NOT NULL DEFAULT '{}',
  error       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  merged_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, updated_at DESC);

-- M18 · P5：并行批次（一个需求拆成 N 条互不重叠的车道，各自一棵隔离工作树）
-- 车道本身就是一条 task（复用 P4 的工作树/审查/合并），batch_id 只负责把它们归组，
-- 于是"并行"这件事不需要第二套任务机制；leases/queue 是批次上的派生态，不入库。
CREATE TABLE IF NOT EXISTS task_batches (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  requirement TEXT NOT NULL DEFAULT '',
  base_branch TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'planning',   -- planning|planned|running|review|merging|merged|partial|failed|discarded
  plan_json   TEXT NOT NULL DEFAULT '{}',
  error       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  merged_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_batches_project ON task_batches(project_id, updated_at DESC);

-- M5 s23：自动化任务
CREATE TABLE IF NOT EXISTS automations (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL REFERENCES users(id),
  name          TEXT NOT NULL,
  prompt        TEXT NOT NULL,
  schedule_type TEXT NOT NULL,            -- once | recurring
  rrule         TEXT,
  scheduled_at  TEXT,
  status        TEXT NOT NULL DEFAULT 'active',  -- active | paused | completed
  last_run_at   TEXT,
  next_run_at   TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automations_due ON automations(status, next_run_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """单连接 + 锁的轻量封装。WAL 允许读并发，写串行对本负载足够。"""

    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # 轻量迁移：给旧库补列（列已存在则忽略）。
        # 每条 ALTER 独立 try——同一块里第一条报错会连带跳过后续，导致
        # "folder 已加过、但 permission_mode 永远加不上"的隐蔽半迁移。
        for ddl in (
            "ALTER TABLE usage ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE approvals ADD COLUMN purpose TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE approvals ADD COLUMN effects TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE sessions ADD COLUMN folder TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'workspace-write'",
            "ALTER TABLE sessions ADD COLUMN model TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN loop_mode TEXT NOT NULL DEFAULT 'classic'",
            # M18 代码模式：会话绑定到项目与工作树（main = 主干；task:<id> = 隔离工作树）
            "ALTER TABLE sessions ADD COLUMN project_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN tree_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN task_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT ''",
            # M18 · P4：任务会话的工作树路径。必须落库：进程重启后运行时会被重建，
            # 若工作区退回"项目根"，AI 就会直接写主干（隔离形同虚设）
            "ALTER TABLE sessions ADD COLUMN workspace_override TEXT NOT NULL DEFAULT ''",
            # M18 · P5：任务归属的并行批次与车道名（每条车道 = 一棵工作树 = 一条 task）
            "ALTER TABLE tasks ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tasks ADD COLUMN lane TEXT NOT NULL DEFAULT ''",
            # M19 联网：会话级「联网」开关（工具是否暴露给模型由它决定）
            "ALTER TABLE sessions ADD COLUMN web_enabled INTEGER NOT NULL DEFAULT 0",
            # M36 去掉三档（lite/default/craft）：旧库把两处 model_tier 列删掉，
            # 用量改成记**模型名**。SQLite 3.35+ 支持 DROP COLUMN（本机 3.53），
            # 两列都没进索引/视图，所以直接删即可（失败也只当"已经是新库"）。
            "ALTER TABLE usage ADD COLUMN model TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sessions DROP COLUMN model_tier",
            "ALTER TABLE usage DROP COLUMN model_tier",
            # 会话级指定模型的入口已去掉（模型由"配置档案"决定），列也一起删掉：
            # 留着一列没人写的字段，下一个人会以为它还有用。
            "ALTER TABLE sessions DROP COLUMN model",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # Old sessions keep their histories, identities and timestamps. Their
        # obsolete execution mode cannot reactivate after an upgrade.
        self._conn.execute("UPDATE sessions SET loop_mode='classic' WHERE loop_mode <> 'classic'")
        self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self._conn.close()


class UserRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, username: str, password_hash: str, role: str = "user") -> dict:
        uid = f"u_{secrets.token_hex(8)}"
        self.db.execute(
            "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            (uid, username, password_hash, role, now_iso()),
        )
        return self.get(uid)  # type: ignore[return-value]

    def get(self, user_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM users WHERE id=?", (user_id,))

    def get_by_username(self, username: str) -> dict | None:
        return self.db.query_one("SELECT * FROM users WHERE username=?", (username,))

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM users")
        return int(row["c"]) if row else 0


class SessionRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        user_id: str,
        title: str = "",
        folder: str = "",
        permission_mode: str = "workspace-write",
        project_id: str = "",
        tree_id: str = "",
        task_id: str = "",
        kind: str = "",
        workspace_override: str = "",
        web_enabled: int = 0,
    ) -> dict:
        sid = f"s_{secrets.token_hex(8)}"
        ts = now_iso()
        self.db.execute(
            "INSERT INTO sessions "
            "(id, user_id, title, folder, permission_mode, "
            " project_id, tree_id, task_id, kind, workspace_override, web_enabled, "
            " created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid, user_id, title, folder, permission_mode,
                project_id, tree_id, task_id, kind, workspace_override, int(bool(web_enabled)),
                ts, ts,
            ),
        )
        return self.get(sid)  # type: ignore[return-value]

    def get(self, session_id: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
        return self._metadata(row) if row is not None else None

    @staticmethod
    def _metadata(row: dict) -> dict:
        row.pop("loop_mode", None)
        return row

    def list_for_user(self, user_id: str, limit: int = 50) -> list[dict]:
        return [self._metadata(row) for row in self.db.query_all(
            "SELECT * FROM sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        )]

    def touch(self, session_id: str, title: str | None = None) -> None:
        if title:
            self.db.execute(
                "UPDATE sessions SET updated_at=?, title=? WHERE id=?",
                (now_iso(), title, session_id),
            )
        else:
            self.db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_iso(), session_id))

    def rename(self, session_id: str, title: str) -> None:
        self.db.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
            (title, now_iso(), session_id),
        )

    def set_status(self, session_id: str, status: str) -> None:
        """状态流转（active | closed | archived）。归档不删轨迹，只退出发送与默认列表。"""
        self.db.execute(
            "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
            (status, now_iso(), session_id),
        )

    def close(self, session_id: str) -> None:
        self.db.execute(
            "UPDATE sessions SET status='closed', closed_at=?, updated_at=? WHERE id=?",
            (now_iso(), now_iso(), session_id),
        )

    def set_folder(self, session_id: str, folder: str) -> None:
        self.db.execute(
            "UPDATE sessions SET folder=?, updated_at=? WHERE id=?",
            (folder[:60], now_iso(), session_id),
        )

    def set_permission_mode(self, session_id: str, mode: str) -> None:
        self.db.execute(
            "UPDATE sessions SET permission_mode=?, updated_at=? WHERE id=?",
            (mode, now_iso(), session_id),
        )

    def set_web_enabled(self, session_id: str, enabled: bool) -> None:
        """会话级「联网」开关（M19）。落库：进程重启后运行时重建也要保持。"""
        self.db.execute(
            "UPDATE sessions SET web_enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, now_iso(), session_id),
        )

    def set_project_binding(
        self,
        session_id: str,
        project_id: str,
        tree_id: str = "main",
        task_id: str = "",
        kind: str = "main",
    ) -> None:
        """把会话绑到项目/工作树（代码模式）。落库是为了重启后仍能重建正确的工作区。"""
        self.db.execute(
            "UPDATE sessions SET project_id=?, tree_id=?, task_id=?, kind=?, updated_at=? WHERE id=?",
            (project_id, tree_id, task_id, kind, now_iso(), session_id),
        )

    def list_by_project(self, project_id: str) -> list[dict]:
        return [self._metadata(row) for row in self.db.query_all(
            "SELECT * FROM sessions WHERE project_id=? ORDER BY updated_at DESC",
            (project_id,),
        )]

    def list_project_conversations(self, user_id: str, project_id: str, default_session_id: str) -> list[dict]:
        """User-facing project windows; task/agent histories are not conversations."""
        return [self._metadata(row) for row in self.db.query_all(
            "SELECT * FROM sessions WHERE user_id=? AND project_id=? AND status='active' AND task_id='' "
            "AND ((kind='chat' AND tree_id='main') OR (id=? AND kind IN ('','main') AND tree_id IN ('','main'))) "
            "ORDER BY updated_at DESC,created_at DESC,rowid DESC",
            (user_id, project_id, default_session_id),
        )]

    def delete(self, session_id: str) -> None:
        """硬删除：会话行与关联用量/审批一并清掉。轨迹文件由 manager 移入 .trash。"""
        self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.db.execute("DELETE FROM usage WHERE session_id=?", (session_id,))
        self.db.execute("DELETE FROM approvals WHERE session_id=?", (session_id,))


class WorkspaceRepo:
    """工作区（侧栏分组）注册表。名字即 sessions.folder 的值，不引入第二套 id。"""

    def __init__(self, db: Database):
        self.db = db

    def list_for_user(self, user_id: str) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM workspaces WHERE user_id=? ORDER BY updated_at DESC", (user_id,)
        )

    def exists(self, user_id: str, name: str) -> bool:
        return (
            self.db.query_one(
                "SELECT 1 FROM workspaces WHERE user_id=? AND name=?", (user_id, name)
            )
            is not None
        )

    def create(self, user_id: str, name: str) -> dict:
        ts = now_iso()
        self.db.execute(
            "INSERT OR IGNORE INTO workspaces (user_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, name, ts, ts),
        )
        return self.db.query_one(
            "SELECT * FROM workspaces WHERE user_id=? AND name=?", (user_id, name)
        )

    def rename(self, user_id: str, old: str, new: str) -> None:
        """重命名工作区：连同其下会话的 folder 一起改，保持两边一致。"""
        self.db.execute(
            "UPDATE workspaces SET name=?, updated_at=? WHERE user_id=? AND name=?",
            (new, now_iso(), user_id, old),
        )
        self.db.execute(
            "UPDATE sessions SET folder=? WHERE user_id=? AND folder=?", (new, user_id, old)
        )

    def delete(self, user_id: str, name: str) -> None:
        self.db.execute("DELETE FROM workspaces WHERE user_id=? AND name=?", (user_id, name))
        # 会话不随之删除，退回默认工作区（删分组≠删数据）
        self.db.execute(
            "UPDATE sessions SET folder='' WHERE user_id=? AND folder=?", (user_id, name)
        )

    def counts(self, user_id: str) -> dict[str, int]:
        rows = self.db.query_all(
            "SELECT folder, COUNT(*) AS n FROM sessions "
            "WHERE user_id=? AND folder<>'' GROUP BY folder",
            (user_id,),
        )
        return {r["folder"]: r["n"] for r in rows}


class ProjectRepo:
    """M18 代码模式：项目注册表。

    只登记"哪个目录属于哪个用户"，**不碰目录里的任何文件**——删项目只删这行
    记录（除非 origin='create' 且调用方显式要求连目录一起删，见 service 层）。
    """

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        user_id: str,
        name: str,
        root_path: str,
        root_key: str,
        origin: str = "open",
        template: str = "",
        git_mode: str = "",
        base_branch: str = "",
    ) -> dict:
        pid = f"p_{secrets.token_hex(8)}"
        ts = now_iso()
        self.db.execute(
            "INSERT INTO projects "
            "(id, user_id, name, root_path, root_key, origin, template, git_mode, base_branch, "
            " created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, user_id, name[:80], root_path, root_key, origin, template, git_mode, base_branch, ts, ts),
        )
        return self.get(pid)  # type: ignore[return-value]

    def register(
        self,
        user_id: str,
        name: str,
        root_path: str,
        root_key: str,
        origin: str = "open",
        template: str = "",
        git_mode: str = "",
        base_branch: str = "",
        *,
        existing_only: bool = False,
    ) -> dict:
        """原子登记或取回当前用户的已有项目；复用时仅更新访问时间。

        唯一索引同时覆盖多线程和不同 SQLite 连接，不能先查后插，也不能
        用不带 user_id 的路径查询复用其他用户的项目。
        """
        pid = f"p_{secrets.token_hex(8)}"
        ts = now_iso()
        with self.db._lock:
            try:
                if existing_only:
                    self.db._conn.execute(
                        "UPDATE projects SET updated_at=? WHERE user_id=? AND root_key=?",
                        (ts, user_id, root_key),
                    )
                else:
                    self.db._conn.execute(
                        "INSERT INTO projects "
                        "(id, user_id, name, root_path, root_key, origin, template, git_mode, base_branch, "
                        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(user_id, root_key) DO UPDATE SET updated_at=excluded.updated_at",
                        (pid, user_id, name[:80], root_path, root_key, origin, template,
                         git_mode, base_branch, ts, ts),
                    )
                row = self.db._conn.execute(
                    "SELECT * FROM projects WHERE user_id=? AND root_key=?", (user_id, root_key)
                ).fetchone()
                if row is None:
                    raise KeyError("项目登记已移除，请重新打开目录")
                self.db._conn.commit()
            except Exception:
                self.db._conn.rollback()
                raise
        assert row is not None
        return dict(row)

    def get(self, project_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM projects WHERE id=?", (project_id,))

    def get_by_root(self, user_id: str, root_key: str) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM projects WHERE user_id=? AND root_key=?", (user_id, root_key)
        )

    def find_by_root(self, root_key: str) -> dict | None:
        """按归一化路径反查项目（不限定用户）。

        供代码模式工具使用：工具只知道自己的**工作区**（= 项目根目录），
        由路径反查项目 id 才能拿到索引。能在这个工作区里跑，就已经说明
        会话本身被授权过；调用方若知道 user_id，仍应再校验一次归属。
        """
        return self.db.query_one("SELECT * FROM projects WHERE root_key=? LIMIT 1", (root_key,))

    def list_for_user(self, user_id: str) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC", (user_id,)
        )

    def touch(self, project_id: str) -> None:
        self.db.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), project_id)
        )

    def rename(self, project_id: str, name: str) -> None:
        self.db.execute(
            "UPDATE projects SET name=?, updated_at=? WHERE id=?",
            (name[:80], now_iso(), project_id),
        )

    def set_session(self, project_id: str, session_id: str) -> None:
        self.db.execute(
            "UPDATE projects SET session_id=?, updated_at=? WHERE id=?",
            (session_id, now_iso(), project_id),
        )

    def set_flags(
        self,
        project_id: str,
        *,
        base_branch: str | None = None,
        allow_direct_write: bool | None = None,
        git_mode: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if base_branch is not None:
            sets.append("base_branch=?")
            params.append(base_branch)
        if allow_direct_write is not None:
            sets.append("allow_direct_write=?")
            params.append(1 if allow_direct_write else 0)
        if git_mode is not None:
            sets.append("git_mode=?")
            params.append(git_mode)
        if not sets:
            return
        sets.append("updated_at=?")
        params.extend([now_iso(), project_id])
        self.db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", tuple(params))

    def set_scan(self, project_id: str, summary_json: str) -> None:
        self.db.execute(
            "UPDATE projects SET scan_summary=?, last_scan_at=?, updated_at=? WHERE id=?",
            (summary_json, now_iso(), now_iso(), project_id),
        )

    def delete(self, project_id: str) -> None:
        self.db.execute("DELETE FROM projects WHERE id=?", (project_id,))


class TaskRepo:
    """M18 · P4：代码模式任务。

    一个任务 = 一棵隔离工作树 + 一个 AI 会话 + 一份 Plan + 一次审批合并。
    状态机：planning → planned → running → review → merged | discarded | failed
    （plan_failed 表示模型没能产出可解析的 Plan，用户可手改后再批准）
    """

    ACTIVE_STATUSES = ("planning", "planned", "plan_failed", "running", "review", "deferred")

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        *,
        project_id: str,
        user_id: str,
        title: str,
        requirement: str,
        base_branch: str,
        branch: str,
        worktree: str,
        status: str = "planning",
        batch_id: str = "",
        lane: str = "",
    ) -> dict:
        tid = f"t_{secrets.token_hex(8)}"
        ts = now_iso()
        self.db.execute(
            "INSERT INTO tasks (id, project_id, user_id, title, requirement, base_branch, branch,"
            " worktree, status, batch_id, lane, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid, project_id, user_id, title[:120], requirement[:4000], base_branch, branch,
                worktree, status, batch_id, lane, ts, ts,
            ),
        )
        return self.get(tid)  # type: ignore[return-value]

    def get(self, task_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def list_for_project(self, project_id: str, limit: int = 30) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM tasks WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
            (project_id, limit),
        )

    def list_active_for_user(self, user_id: str) -> list[dict]:
        marks = ",".join("?" for _ in self.ACTIVE_STATUSES)
        return self.db.query_all(
            f"SELECT * FROM tasks WHERE user_id=? AND status IN ({marks}) ORDER BY updated_at DESC",
            (user_id, *self.ACTIVE_STATUSES),
        )

    def list_for_batch(self, batch_id: str) -> list[dict]:
        """批次里的车道（按创建顺序：车道的顺序就是计划里的顺序与合并队列顺序）。"""
        return self.db.query_all(
            "SELECT * FROM tasks WHERE batch_id=? ORDER BY created_at ASC, rowid ASC", (batch_id,)
        )

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        plan_json: str | None = None,
        review_json: str | None = None,
        session_id: str | None = None,
        branch: str | None = None,
        worktree: str | None = None,
        error: str | None = None,
        merged_at: str | None = None,
        title: str | None = None,
        batch_id: str | None = None,
        lane: str | None = None,
        base_branch: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("plan_json", plan_json),
            ("review_json", review_json),
            ("session_id", session_id),
            ("branch", branch),
            ("worktree", worktree),
            ("error", error),
            ("merged_at", merged_at),
            ("title", title),
            ("batch_id", batch_id),
            ("lane", lane),
            ("base_branch", base_branch),
        ):
            if value is None:
                continue
            sets.append(f"{column}=?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        params.extend([now_iso(), task_id])
        self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(params))

    def delete(self, task_id: str) -> None:
        self.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))


class BatchRepo:
    """M18 · P5：并行批次。

    批次自己不执行任何东西——它只是"一个需求 → N 条车道（task）"的归组与状态机，
    真正干活的是每条车道复用的 P4 任务机制。这样并行化不会分叉出第二套合并逻辑。
    """

    ACTIVE_STATUSES = ("planning", "planned", "running", "review", "merging")

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        *,
        project_id: str,
        user_id: str,
        title: str,
        requirement: str,
        base_branch: str,
        status: str = "planning",
    ) -> dict:
        bid = f"b_{secrets.token_hex(8)}"
        ts = now_iso()
        self.db.execute(
            "INSERT INTO task_batches (id, project_id, user_id, title, requirement, base_branch,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (bid, project_id, user_id, title[:120], requirement[:4000], base_branch, status, ts, ts),
        )
        return self.get(bid)  # type: ignore[return-value]

    def get(self, batch_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM task_batches WHERE id=?", (batch_id,))

    def list_for_project(self, project_id: str, limit: int = 30) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM task_batches WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
            (project_id, limit),
        )

    def update(
        self,
        batch_id: str,
        *,
        status: str | None = None,
        plan_json: str | None = None,
        error: str | None = None,
        title: str | None = None,
        merged_at: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("plan_json", plan_json),
            ("error", error),
            ("title", title),
            ("merged_at", merged_at),
        ):
            if value is None:
                continue
            sets.append(f"{column}=?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        params.extend([now_iso(), batch_id])
        self.db.execute(f"UPDATE task_batches SET {', '.join(sets)} WHERE id=?", tuple(params))

    def delete(self, batch_id: str) -> None:
        self.db.execute("DELETE FROM task_batches WHERE id=?", (batch_id,))


class ApprovalRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, session_id: str, user_id: str, tool_name: str, command: str, reason: str,
               *, purpose: str = "", effects: list[str] | None = None) -> str:
        from code_agent.models.approval import approval_details

        explanation = approval_details({"purpose": purpose, "effects": effects})
        aid = f"ap_{secrets.token_hex(8)}"
        self.db.execute(
            "INSERT INTO approvals (id, session_id, user_id, tool_name, command, reason, purpose, effects, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, session_id, user_id, tool_name, command, reason, explanation["purpose"],
             json.dumps(explanation["effects"], ensure_ascii=False), now_iso()),
        )
        return aid

    @staticmethod
    def _explanation(row: dict | None) -> dict | None:
        if row is None:
            return None
        from code_agent.models.approval import approval_details

        try:
            effects = json.loads(row.get("effects") or "[]")
        except (TypeError, ValueError):
            effects = []
        return {**row, **approval_details({"purpose": row.get("purpose"), "effects": effects})}

    def get(self, approval_id: str) -> dict | None:
        return self._explanation(self.db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,)))

    def decide(self, approval_id: str, status: str, decided_by: str = "") -> None:
        self.db.execute(
            "UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'",
            (status, decided_by, now_iso(), approval_id),
        )

    def pending_for_session(self, session_id: str) -> list[dict]:
        return [self._explanation(row) for row in self.db.query_all(
            "SELECT * FROM approvals WHERE session_id=? AND status='pending' ORDER BY created_at",
            (session_id,),
        )]


class UsageRepo:
    """M5 s21：用量统计。只记总量，不记明细 prompt（隐私最小化）。"""

    def __init__(self, db: Database):
        self.db = db

    def record(
        self, session_id: str, user_id: str, model: str,
        input_tokens: int, output_tokens: int, cached_tokens: int = 0,
    ) -> str:
        """记一行用量。`model` 是**实际用的模型名**（M36 起不再是档位）。"""
        uid = f"us_{secrets.token_hex(8)}"
        self.db.execute(
            "INSERT INTO usage (id, session_id, user_id, model, input_tokens, output_tokens, cached_tokens, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, session_id, user_id, str(model or "")[:120], input_tokens, output_tokens, cached_tokens, now_iso()),
        )
        return uid

    def summary_for_user(self, user_id: str) -> dict:
        row = self.db.query_one(
            "SELECT COUNT(*) AS prompts, COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cached_tokens),0) AS cached_tokens "
            "FROM usage WHERE user_id=?",
            (user_id,),
        )
        # 按模型分组的用量（M36）：原来按档位分组，档位没了就按模型分——
        # "哪个模型花了多少"本来就是这一页要回答的问题。
        by_model = self.db.query_all(
            "SELECT model, COUNT(*) AS prompts, COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens "
            "FROM usage WHERE user_id=? GROUP BY model ORDER BY SUM(input_tokens) DESC",
            (user_id,),
        )
        return {**row, "by_model": by_model}  # type: ignore[operator]


GENESIS_HASH = "0" * 64


def entry_hash(seq: int, ts: str, session_id: str, user_id: str,
               tool_name: str, arguments: str, outcome: str, prev_hash: str) -> str:
    """审计链节点哈希：seq 参与，防重排/防删除中间节点。"""
    import hashlib

    material = f"{seq}|{ts}|{session_id}|{user_id}|{tool_name}|{arguments}|{outcome}|{prev_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditRepo:
    """M5 s22：append-only 审计 hash chain。

    不变量：第 N 行的 prev_hash 必须等于第 N-1 行的 entry_hash（genesis 为全 0）。
    任何篡改/删除都会让 verify() 断链。
    """

    def __init__(self, db: Database):
        self.db = db

    def append(self, session_id: str, user_id: str, tool_name: str,
               arguments: str, outcome: str) -> dict:
        ts = now_iso()
        prev = self.db.query_one("SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1")
        prev_hash = prev["entry_hash"] if prev else GENESIS_HASH
        # AUTOINCREMENT 由 SQLite 分配；先占位插入再回填 hash（seq 是链材料的一部分）
        cur = self.db.execute(
            "INSERT INTO audit_log (ts, session_id, user_id, tool_name, arguments, outcome, prev_hash, entry_hash) "
            "VALUES (?,?,?,?,?,?,?,'')",
            (ts, session_id, user_id, tool_name, arguments, outcome, prev_hash),
        )
        seq = cur.lastrowid
        h = entry_hash(int(seq or 0), ts, session_id, user_id, tool_name, arguments, outcome, prev_hash)
        self.db.execute("UPDATE audit_log SET entry_hash=? WHERE seq=?", (h, seq))
        return {"seq": seq, "ts": ts, "entry_hash": h}

    def list_entries(self, limit: int = 200, after_seq: int = 0) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM audit_log WHERE seq>? ORDER BY seq LIMIT ?",
            (after_seq, limit),
        )

    def verify(self) -> dict:
        """重放整条链校验。返回 {ok, broken_at}（broken_at 为首个断链 seq）。"""
        prev_hash = GENESIS_HASH
        for row in self.db.query_all("SELECT * FROM audit_log ORDER BY seq"):
            expected = entry_hash(
                row["seq"], row["ts"], row["session_id"], row["user_id"],
                row["tool_name"], row["arguments"], row["outcome"], prev_hash,
            )
            if row["prev_hash"] != prev_hash or row["entry_hash"] != expected:
                return {"ok": False, "broken_at": row["seq"]}
            prev_hash = row["entry_hash"]
        return {"ok": True, "broken_at": None}


class AutomationRepo:
    """M5 s23：自动化任务 CRUD + 到期扫描。"""

    def __init__(self, db: Database):
        self.db = db

    def create(self, user_id: str, name: str, prompt: str,
               schedule_type: str, rrule: str | None, scheduled_at: str | None) -> dict:
        aid = f"au_{secrets.token_hex(8)}"
        self.db.execute(
            "INSERT INTO automations (id, user_id, name, prompt, schedule_type, rrule, scheduled_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (aid, user_id, name, prompt, schedule_type, rrule, scheduled_at, now_iso()),
        )
        return self.get(aid)  # type: ignore[return-value]

    def get(self, automation_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM automations WHERE id=?", (automation_id,))

    def list_for_user(self, user_id: str) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM automations WHERE user_id=? ORDER BY created_at DESC", (user_id,)
        )

    def update_status(self, automation_id: str, status: str) -> None:
        self.db.execute("UPDATE automations SET status=? WHERE id=?", (status, automation_id))

    def set_schedule(self, automation_id: str, rrule: str | None, scheduled_at: str | None) -> None:
        self.db.execute(
            "UPDATE automations SET rrule=?, scheduled_at=? WHERE id=?",
            (rrule, scheduled_at, automation_id),
        )

    def set_next_run(self, automation_id: str, next_run_at: str | None) -> None:
        self.db.execute("UPDATE automations SET next_run_at=? WHERE id=?", (next_run_at, automation_id))

    def mark_run(self, automation_id: str, next_run_at: str | None, status: str = "active") -> None:
        self.db.execute(
            "UPDATE automations SET last_run_at=?, next_run_at=?, status=? WHERE id=?",
            (now_iso(), next_run_at, status, automation_id),
        )

    def due(self, now_iso_value: str, limit: int = 10) -> list[dict]:
        """到期任务：active 且 next_run_at 非空且 <= now。"""
        return self.db.query_all(
            "SELECT * FROM automations WHERE status='active' AND next_run_at IS NOT NULL "
            "AND next_run_at<=? ORDER BY next_run_at LIMIT ?",
            (now_iso_value, limit),
        )
