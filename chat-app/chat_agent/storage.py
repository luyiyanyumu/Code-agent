"""Standalone SQLite persistence; no dependency on an IDE or coding service."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .config import Config, validate_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
  folder TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
  permission_mode TEXT NOT NULL, loop_mode TEXT NOT NULL,
  web_enabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_seq INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS events (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, type TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS checkpoints (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  anchor_seq INTEGER NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checkpoints_session ON checkpoints(session_id, anchor_seq);
CREATE TABLE IF NOT EXISTS workspaces (
  user_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, name)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


class Store:
    def __init__(self, config: Config):
        self.config = config
        config.ensure_dirs()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(config.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        # Retain legacy sessions/events and timestamps, while making their old
        # execution-mode column inert. It is excluded from all session metadata.
        self._conn.execute("UPDATE sessions SET loop_mode='classic' WHERE loop_mode <> 'classic'")
        self._conn.execute("DELETE FROM settings WHERE key='loop_mode'")
        self._conn.commit()

    @staticmethod
    def _session(row: sqlite3.Row | dict) -> dict:
        result = dict(row)
        result.pop("next_seq", None)
        result.pop("loop_mode", None)
        result["web_enabled"] = bool(result["web_enabled"])
        result["project_id"] = None
        return result

    def create_session(self, user_id: str = "local", folder: str = "", permission_mode: str = "workspace-write",
                       title: str = "新对话") -> dict:
        sid, now = new_id("s"), now_iso()
        with self._lock, self._conn:
            if folder:
                self.create_workspace(folder, user_id)
            self._conn.execute(
                "INSERT INTO sessions (id,user_id,title,folder,permission_mode,loop_mode,created_at,updated_at) VALUES (?,?,?,?,?,'classic',?,?)",
                (sid, user_id, str(title), folder, permission_mode, now, now),
            )
            return self.get_session(sid)

    def get_session(self, sid: str, user_id: str | None = None) -> dict:
        validate_id(sid)
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row or (user_id is not None and row["user_id"] != user_id):
                raise KeyError(sid)
            return self._session(row)

    def list_sessions(self, user_id: str = "local", include_archived: bool = False) -> list[dict]:
        with self._lock:
            archive_filter = "" if include_archived else " AND status!='archived'"
            rows = self._conn.execute(
                f"SELECT * FROM sessions WHERE user_id=?{archive_filter} ORDER BY updated_at DESC, id", (user_id,)
            ).fetchall()
            return [self._session(row) for row in rows]

    def update_session(self, sid: str, **fields: Any) -> dict:
        allowed = {"title", "folder", "status", "permission_mode", "web_enabled"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported session fields: {', '.join(sorted(unknown))}")
        with self._lock, self._conn:
            session = self.get_session(sid)
            if fields.get("folder"):
                self.create_workspace(fields["folder"], session["user_id"])
            if "web_enabled" in fields:
                fields["web_enabled"] = int(bool(fields["web_enabled"]))
            fields["updated_at"] = now_iso()
            assignments = ", ".join(f"{field}=?" for field in fields)
            self._conn.execute(f"UPDATE sessions SET {assignments} WHERE id=?", (*fields.values(), sid))
            return self.get_session(sid)

    def delete_session(self, sid: str) -> None:
        with self._lock, self._conn:
            self.get_session(sid)
            self._conn.execute("DELETE FROM sessions WHERE id=?", (sid,))

    def append_event(self, sid: str, type: str, payload: dict) -> dict:
        # Serializes before allocating a sequence, so invalid payloads cannot leave a hole.
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self._lock, self._conn:
            self.get_session(sid)
            seq = self._conn.execute("SELECT next_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
            ts = time.time()
            self._conn.execute("INSERT INTO events VALUES (?,?,?,?,?)", (sid, seq, str(type), ts, serialized))
            self._conn.execute("UPDATE sessions SET next_seq=?, updated_at=? WHERE id=?", (seq + 1, now_iso(), sid))
            return {"seq": seq, "type": str(type), "ts": ts, "payload": json.loads(serialized)}

    def events(self, sid: str, after_seq: int = 0) -> list[dict]:
        with self._lock:
            self.get_session(sid)
            rows = self._conn.execute("SELECT seq,type,ts,payload FROM events WHERE session_id=? AND seq>? ORDER BY seq", (sid, int(after_seq))).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def checkpoints(self, sid: str) -> list[dict]:
        with self._lock:
            self.get_session(sid)
            rows = self._conn.execute("SELECT id,anchor_seq,kind,created_at FROM checkpoints WHERE session_id=? ORDER BY anchor_seq, created_at", (sid,)).fetchall()
            return [dict(row) for row in rows]

    def add_checkpoint(self, sid: str, anchor_seq: int, kind: str = "prompt") -> dict:
        if not isinstance(anchor_seq, int) or anchor_seq < 1:
            raise ValueError("Checkpoint anchor must be a positive sequence")
        with self._lock, self._conn:
            self.get_session(sid)
            next_seq = self._conn.execute("SELECT next_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
            if anchor_seq > next_seq:
                raise ValueError("Checkpoint anchor is beyond the transcript")
            checkpoint = {"id": new_id("ck"), "anchor_seq": anchor_seq, "kind": str(kind), "created_at": now_iso()}
            self._conn.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?)", (checkpoint["id"], sid, anchor_seq, str(kind), checkpoint["created_at"]))
            return checkpoint

    def rewind(self, sid: str, checkpoint_id: str) -> dict:
        with self._lock, self._conn:
            self.get_session(sid)
            checkpoint = self._conn.execute("SELECT * FROM checkpoints WHERE session_id=? AND id=?", (sid, checkpoint_id)).fetchone()
            if checkpoint is None:
                raise KeyError(checkpoint_id)
            anchor = checkpoint["anchor_seq"]
            deleted = self._conn.execute("DELETE FROM events WHERE session_id=? AND seq>=?", (sid, anchor)).rowcount
            self._conn.execute("DELETE FROM checkpoints WHERE session_id=? AND anchor_seq>=?", (sid, anchor))
            self._conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_iso(), sid))
            return {"session_id": sid, "checkpoint_id": checkpoint_id, "anchor_seq": anchor, "deleted_events": deleted, "events": self.events(sid)}

    def fork_session(self, sid: str, user_id: str) -> dict:
        with self._lock, self._conn:
            source = self.get_session(sid, user_id)
            fork = self.create_session(user_id=user_id, folder=source["folder"], permission_mode=source["permission_mode"], title=source["title"] + "（副本）")
            self._conn.execute("INSERT INTO events SELECT ?,seq,type,ts,payload FROM events WHERE session_id=?", (fork["id"], sid))
            next_seq = self._conn.execute("SELECT next_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
            self._conn.execute("UPDATE sessions SET next_seq=?, web_enabled=? WHERE id=?", (next_seq, int(source["web_enabled"]), fork["id"]))
            for item in self.checkpoints(sid):
                self._conn.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?)", (new_id("ck"), fork["id"], item["anchor_seq"], item["kind"], item["created_at"]))
            return self.get_session(fork["id"])

    @staticmethod
    def _workspace_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(char) < 32 for char in name):
            raise ValueError("Workspace name must contain 1–100 visible characters")
        return name.strip()

    def list_workspaces(self, user_id: str = "local") -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute("SELECT name,created_at,updated_at FROM workspaces WHERE user_id=? ORDER BY name", (user_id,)).fetchall()]

    def create_workspace(self, name: str, user_id: str = "local") -> dict:
        name, now = self._workspace_name(name), now_iso()
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO workspaces VALUES (?,?,?,?) ON CONFLICT(user_id,name) DO NOTHING", (user_id, name, now, now))
            return dict(self._conn.execute("SELECT name,created_at,updated_at FROM workspaces WHERE user_id=? AND name=?", (user_id, name)).fetchone())

    def delete_workspace(self, name: str, user_id: str = "local") -> None:
        name = self._workspace_name(name)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM workspaces WHERE user_id=? AND name=?", (user_id, name))
            self._conn.execute("UPDATE sessions SET folder='', updated_at=? WHERE user_id=? AND folder=?", (now_iso(), user_id, name))

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        if key == "loop_mode":
            raise ValueError("Execution mode settings have been removed")
        value_json = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value_json))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
