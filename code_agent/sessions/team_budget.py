"""Local per-activation limits and shared task-chain usage, without model calls."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from code_agent import settings
from code_agent.models.events import new_id


class ExecutionBudgetExceeded(RuntimeError):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _clamp(requested: int, configured: int) -> int:
    return min(requested, configured) if requested and configured else requested or configured


class TeamBudget:
    def __init__(self, db: Any, session_id: str):
        self.db, self.session_id = db, session_id
        self.current: dict | None = None
        self.db.execute("CREATE TABLE IF NOT EXISTS team_execution_scopes ("
                        "id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL, "
                        "max_tokens INTEGER NOT NULL, max_seconds INTEGER NOT NULL, spent_tokens INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS team_execution_meter ("
                        "session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, "
                        "activation_id INTEGER NOT NULL, scope_id TEXT NOT NULL REFERENCES team_execution_scopes(id), "
                        "tokens INTEGER NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0, "
                        "PRIMARY KEY(session_id,activation_id))")
        if not any(column["name"] == "spent_tokens" for column in self.db.query_all("PRAGMA table_info(team_execution_scopes)")):
            self.db.execute("ALTER TABLE team_execution_scopes ADD COLUMN spent_tokens INTEGER NOT NULL DEFAULT 0")
            self.db.execute("UPDATE team_execution_scopes SET spent_tokens=(SELECT COALESCE(SUM(tokens),0) "
                            "FROM team_execution_meter WHERE scope_id=team_execution_scopes.id)")

    def restore(self, record: dict, *, project_id: str) -> bool:
        """Restore continuation identity, never resume an old execution task."""
        if not isinstance(record, dict) or type(record.get("activation_id")) is not int:
            return False
        scope = self.db.query_one("SELECT * FROM team_execution_scopes WHERE id=?", (record.get("scope_id"),))
        meter = self.db.query_one("SELECT scope_id FROM team_execution_meter WHERE session_id=? AND activation_id=?",
                                  (self.session_id, record["activation_id"]))
        if not scope or scope["project_id"] != project_id or not meter or meter["scope_id"] != scope["id"]:
            return False
        if any(type(record.get(key)) is not int or record[key] < 0 for key in ("max_tokens", "max_seconds")):
            return False
        try:
            datetime.fromisoformat(record["started_at"])
        except (ValueError, TypeError, KeyError):
            return False
        self.current = {**record, "team_max_tokens": scope["max_tokens"], "team_max_seconds": scope["max_seconds"],
                        "team_started_at": scope["created_at"]}
        return True

    def begin(self, project_id: str, activation_id: int, requested: dict | None, *, parent: dict | None = None,
              continuation: bool = False) -> dict:
        configured = settings.agent_team_budget_config()
        request = requested or {}
        for field, upper in (("max_tokens", 10_000_000), ("max_seconds", 86_400)):
            value = request.get(field, 0)
            if type(value) is not int or not 0 <= value <= upper:
                raise ValueError("任务预算必须为非负整数")
        current = self.current if continuation else None
        scope_id = (parent or current or {}).get("scope_id")
        scope = self.db.query_one("SELECT * FROM team_execution_scopes WHERE id=?", (scope_id,)) if scope_id else None
        if not scope or scope["project_id"] != project_id:
            scope_id = new_id("scope_")
            self.db.execute("INSERT INTO team_execution_scopes (id,project_id,created_at,max_tokens,max_seconds) VALUES (?,?,?,?,?)",
                            (scope_id, project_id, _stamp(), configured["team_tokens"], configured["team_seconds"]))
            scope = self.db.query_one("SELECT * FROM team_execution_scopes WHERE id=?", (scope_id,))
        self.current = {
            "activation_id": activation_id, "scope_id": scope_id, "started_at": _stamp(),
            "max_tokens": _clamp(request.get("max_tokens", 0), configured["task_tokens"]),
            "max_seconds": _clamp(request.get("max_seconds", 0), configured["task_seconds"]),
            "team_max_tokens": scope["max_tokens"], "team_max_seconds": scope["max_seconds"],
            "team_started_at": scope["created_at"],
        }
        self.db.execute("INSERT OR IGNORE INTO team_execution_meter (session_id,activation_id,scope_id) VALUES (?,?,?)",
                        (self.session_id, activation_id, scope_id))
        return dict(self.current)

    def consume(self, usage: Any) -> None:
        if not self.current:
            return
        tokens = max(0, int(usage.input_tokens)) + max(0, int(usage.output_tokens))
        # Both totals commit together. Paid usage belongs to the task chain and
        # must not disappear when a completed child session is later deleted.
        with self.db._lock:
            with self.db._conn:
                changed = self.db._conn.execute(
                    "UPDATE team_execution_meter SET tokens=tokens+?,turns=turns+1 WHERE session_id=? AND activation_id=?",
                    (tokens, self.session_id, self.current["activation_id"]),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("任务用量记录已不可用，已停止执行")
                self.db._conn.execute("UPDATE team_execution_scopes SET spent_tokens=spent_tokens+? WHERE id=?",
                                      (tokens, self.current["scope_id"]))

    def status(self) -> dict:
        if not self.current:
            return {}
        local = self.db.query_one("SELECT tokens,turns FROM team_execution_meter WHERE session_id=? AND activation_id=?",
                                  (self.session_id, self.current["activation_id"])) or {}
        scope = self.db.query_one("SELECT * FROM team_execution_scopes WHERE id=?", (self.current["scope_id"],)) or {}
        return {**self.current, "tokens": local.get("tokens", 0), "turns": local.get("turns", 0),
                "team_tokens": scope.get("spent_tokens", 0), "team_max_tokens": scope.get("max_tokens", 0),
                "team_max_seconds": scope.get("max_seconds", 0), "team_started_at": scope.get("created_at", "")}

    def remaining_seconds(self) -> float | None:
        if not self.current or not (self.current.get("max_seconds") or self.current.get("team_max_seconds")):
            return None
        record = self.status()
        remaining = []
        now = datetime.now(timezone.utc)
        for limit, stamp in ((record.get("max_seconds"), record.get("started_at")),
                             (record.get("team_max_seconds"), record.get("team_started_at"))):
            if limit and stamp:
                remaining.append(float(limit) - (now - datetime.fromisoformat(stamp)).total_seconds())
        return min(remaining) if remaining else None

    def check(self) -> None:
        if not self.current or not any(self.current.get(key) for key in
                                       ("max_tokens", "max_seconds", "team_max_tokens", "team_max_seconds")):
            return
        record = self.status()
        if record.get("max_tokens") and record["tokens"] >= record["max_tokens"]:
            raise ExecutionBudgetExceeded("任务 Token 预算已达到上限，已停止请求下一轮模型")
        if record.get("team_max_tokens") and record["team_tokens"] >= record["team_max_tokens"]:
            raise ExecutionBudgetExceeded("本次团队任务 Token 总预算已达到上限，已停止请求下一轮模型")
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise ExecutionBudgetExceeded("任务或团队总耗时预算已达到上限")
