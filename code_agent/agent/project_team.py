"""Persistent project teams and a bounded, cooperative descendant scheduler.

Prompts describe responsibilities; ownership, ancestry, depth, permissions,
admission and audit identities are enforced here. Merely synchronising a graph
creates session metadata, never a provider request or a workspace file.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import hashlib
import json
import math
import os
import re
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from code_agent import settings
from code_agent.context.safety import wrap_untrusted
from code_agent.agent.file_ownership import Claim, FileOwnership, actual_target, contains, normalize_paths, overlaps, scope_targets


TEAM_TOOL_NAMES = frozenset({"team_list", "delegate_agent", "send_agent_message", "followup_agent", "interrupt_agent",
                             "wait_agents", "stop_agent", "audit_agent", "read_agent_execution"})
BUSY_STATES = frozenset({"queued", "running", "waiting"})
_START_ID: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar("project_agent_start", default=None)
_TREE = re.compile(r"^(?:main|task:[A-Za-z0-9_-]{1,96})$")


class TeamError(ValueError):
    """A collaboration request violates the project team's contract."""


class _ReadmissionFailed(BaseException):
    """Abort an activation, bypassing recoverable tool-error handlers.

    Losing scheduler admission is not a failed individual tool call. The run
    boundary converts this control-flow signal into a normal failed run.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _string(value: Any, label: str, *, required: bool = True, limit: int = 40_000) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise TeamError(f"{label}必须是{'非空' if required else ''}字符串，最多 {limit} 个字符")
    return value.strip()


class _Capacity:
    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.active: set[str] = set()
        self.queue: deque[str] = deque()


class ProjectTeam:
    def __init__(self, manager: Any, workflow_store: Any):
        self.manager = manager
        self.workflow_store = workflow_store
        self.db = manager.session_repo.db
        self._lock = threading.RLock()
        self._capacities: dict[str, _Capacity] = {}
        self._starts: dict[str, asyncio.Task] = {}
        self._executions: dict[str, asyncio.Task] = {}
        self._cancelling: set[str] = set()
        self._interrupting: set[tuple[str, int]] = set()
        self._approval_suspensions: dict[tuple[str, int], int] = {}
        self._task_operations: set[tuple[str, str]] = set()
        self._closed = False
        self._file_ownership = FileOwnership()
        self._tool_write_locks: dict[str, asyncio.Lock] = {}
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS project_agents ("
            "session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE, "
            "project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, "
            "user_id TEXT NOT NULL, parent_session_id TEXT NOT NULL DEFAULT '', "
            "lead_session_id TEXT NOT NULL, tree_id TEXT NOT NULL, node_id TEXT NOT NULL DEFAULT '', "
            "role TEXT NOT NULL CHECK(role IN ('lead','node','subagent')), depth INTEGER NOT NULL, "
            "title TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'idle', result TEXT NOT NULL DEFAULT '', "
            "error TEXT NOT NULL DEFAULT '', activation_id INTEGER NOT NULL DEFAULT 0, "
            "audit_decision TEXT NOT NULL DEFAULT 'none', audit_reason TEXT NOT NULL DEFAULT '', "
            "audit_reviewer TEXT NOT NULL DEFAULT '', audit_activation_id INTEGER NOT NULL DEFAULT 0, "
            "audit_updated_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        # Each user conversation owns a coordinator and an independent subtree.
        # Migrate the old project-wide Lead/node uniqueness without losing rows.
        self.db.execute("DROP INDEX IF EXISTS project_agents_lead")
        self.db.execute("DROP INDEX IF EXISTS project_agents_node")
        self.db.execute(
            "CREATE UNIQUE INDEX project_agents_lead "
            "ON project_agents(project_id,lead_session_id) WHERE role='lead' AND state!='retired'"
        )
        self.db.execute(
            "CREATE UNIQUE INDEX project_agents_node "
            "ON project_agents(project_id,lead_session_id,tree_id,node_id) WHERE role='node'"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS project_agent_runs ("
            "session_id TEXT NOT NULL REFERENCES project_agents(session_id) ON DELETE CASCADE, "
            "activation_id INTEGER NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL, "
            "result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', "
            "audit_decision TEXT NOT NULL DEFAULT 'pending', audit_reason TEXT NOT NULL DEFAULT '', "
            "audit_reviewer TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "PRIMARY KEY(session_id,activation_id))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS project_agent_audits ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES project_agents(session_id) ON DELETE CASCADE, "
            "activation_id INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, "
            "reviewer_session_id TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS project_agent_scopes ("
            "session_id TEXT NOT NULL, activation_id INTEGER NOT NULL, spec TEXT NOT NULL, "
            "PRIMARY KEY(session_id,activation_id), "
            "FOREIGN KEY(session_id,activation_id) REFERENCES project_agent_runs(session_id,activation_id) ON DELETE CASCADE)"
        )
        # A process restart cannot resume an asyncio task. Preserve its transcript
        # and partial report, and explicitly record interruption instead of lying
        # that an old worker is still running.
        stamp = _now()
        self.db.execute(
            "UPDATE project_agents SET state='cancelled', error=?, updated_at=? "
            "WHERE state IN ('queued','running','waiting')",
            ("服务已重启，之前的执行已中断，请重新委派", stamp),
        )
        self.db.execute(
            "UPDATE project_agent_runs SET state='cancelled', error=?, updated_at=? "
            "WHERE state IN ('queued','running','waiting')",
            ("服务已重启，之前的执行已中断，请重新委派", stamp),
        )

    @staticmethod
    def limits() -> dict[str, int]:
        return settings.agent_team_config()

    def _row(self, session_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM project_agents WHERE session_id=?", (session_id,))

    @staticmethod
    def _public(row: dict) -> dict:
        return {
            key: row[key] for key in (
                "session_id", "project_id", "parent_session_id", "lead_session_id", "tree_id", "node_id",
                "role", "depth", "title", "state", "result", "error", "activation_id", "created_at", "updated_at",
            )
        } | {"audit": {
            "decision": row["audit_decision"], "reason": row["audit_reason"],
            "reviewer_session_id": row["audit_reviewer"], "activation_id": row["audit_activation_id"],
            "updated_at": row["audit_updated_at"],
        }}

    def _scope(self, row: dict) -> dict:
        saved = self.db.query_one("SELECT spec FROM project_agent_scopes WHERE session_id=? AND activation_id=?",
                                  (row["session_id"], row["activation_id"]))
        if saved:
            return json.loads(saved["spec"])
        # Historical records remain visibly undeclared. New internal callers
        # are conservatively serialised if they still omit a file contract.
        workspace = self.manager.workspace_for(self.manager.session_repo.get(row["session_id"]))
        writable = self._effective_permission(row) != "read-only"
        return {"write_paths": ["."] if writable else [], "scope_mode": "legacy",
                "delegation_reason": "", "depends_on": [], "workspace": str(workspace.resolve())}

    def _prepare_scope(self, caller: dict, workspace: Path, write_paths: list[str] | None,
                       reason: str, depends_on: list[str] | None, *, previous: dict | None = None) -> dict:
        reason = _string(reason, "派工理由", required=False, limit=2000)
        if write_paths is None:
            paths = previous["write_paths"] if previous else (["."] if self._effective_permission(caller) != "read-only" else [])
            mode = previous["scope_mode"] if previous else "legacy"
        else:
            paths = write_paths
            mode = "scoped_write"
        try:
            paths = normalize_paths(workspace, paths, infer_directories=write_paths is not None)
            if write_paths is not None:
                mode = "read_only" if not paths else "workspace_write" if "." in paths else "scoped_write"
            wanted = scope_targets(workspace, paths)
            if caller["role"] != "lead":
                parent = self._scope(caller)
                if parent["workspace"] != str(workspace.resolve()):
                    raise TeamError("子任务必须继承父执行者已确认的工作区")
                allowed = scope_targets(workspace, parent["write_paths"])
                if any(not any(contains(limit, target) for limit in allowed) for target in wanted):
                    raise TeamError("子任务的写入范围不能超出当前执行者的文件责任；扩大范围须由主代理重新派工")
        except ValueError as exc:
            raise TeamError(str(exc)) from exc
        if depends_on is None:
            dependencies = []
        else:
            if (not isinstance(depends_on, list) or len(depends_on) > 16
                    or any(not isinstance(sid, str) or not sid or len(sid) > 64 for sid in depends_on)):
                raise TeamError("depends_on 必须是最多 16 项的智能体 ID 数组")
            dependencies = []
            for sid in dict.fromkeys(depends_on):
                dependency = self._member(sid, caller["user_id"], live=False)
                self._authorize_target(caller, dependency)
                if dependency["activation_id"] < 1:
                    raise TeamError("依赖必须指向已有执行编号的子任务")
                dependencies.append({"session_id": sid, "activation_id": dependency["activation_id"]})
        return {"write_paths": paths, "scope_mode": mode, "delegation_reason": reason,
                "depends_on": dependencies, "workspace": str(workspace.resolve())}

    def _save_scope(self, row: dict, activation: int, spec: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO project_agent_scopes (session_id,activation_id,spec) VALUES (?,?,?)",
                        (row["session_id"], activation, json.dumps(spec, ensure_ascii=False)))

    def _scope_targets(self, row: dict):
        spec = self._scope(row)
        workspace = self.manager.workspace_for(self.manager.session_repo.get(row["session_id"]))
        if str(workspace.resolve()) != spec["workspace"]:
            raise TeamError("工作区已变化，请重新派工确认文件责任范围")
        try:
            return scope_targets(workspace, spec["write_paths"])
        except ValueError as exc:
            raise TeamError(str(exc)) from exc

    def _dependencies_pending(self, row: dict) -> list[dict]:
        pending = []
        for dependency in self._scope(row)["depends_on"]:
            run = self.db.query_one("SELECT state FROM project_agent_runs WHERE session_id=? AND activation_id=?",
                                    (dependency["session_id"], dependency["activation_id"]))
            if not run or run["state"] not in BUSY_STATES | {"completed"}:
                raise TeamError("前置子任务未成功完成，依赖任务不能开始：" + dependency["session_id"])
            if run["state"] != "completed":
                pending.append(dependency)
        return pending

    def _file_blockers(self, row: dict) -> list[dict]:
        targets = self._scope_targets(row)
        return self._public_blockers(row, self._file_ownership.blockers(row["session_id"], targets), targets)

    def _public_blockers(self, row: dict, claims: list[Claim], targets) -> list[dict]:
        result = []
        for item in claims:
            owner = self._row(item.session_id)
            visible = owner and owner["user_id"] == row["user_id"] and owner["lead_session_id"] == row["lead_session_id"]
            result.append({"session_id": item.session_id if visible else "",
                           "title": item.title if visible else "其他对话中的执行者",
                           "paths": [target.path for target in item.targets if any(overlaps(target, wanted) for wanted in targets)] if visible
                                    else [target.path for target in targets]})
        return result

    def _release_files(self, row: dict, activation: int | None = None) -> None:
        self._file_ownership.release((row["session_id"], row["activation_id"] if activation is None else activation))

    def _scope_context(self, row: dict) -> str:
        spec = self._scope(row)
        paths, remaining = [], 4000
        for path in spec["write_paths"]:
            if len(path) > remaining:
                break
            paths.append(path)
            remaining -= len(path)
        brief = {"write_paths": paths, "scope_mode": spec["scope_mode"],
                 "delegation_reason": spec["delegation_reason"][:500], "depends_on": spec["depends_on"]}
        omitted = len(spec["write_paths"]) - len(paths)
        if omitted:
            brief.update(omitted_paths=omitted, scope_lookup="team_list 返回完整责任；超大响应按 full_roster_path 读取")
        return "\n本轮文件责任：" + wrap_untrusted(json.dumps(brief, ensure_ascii=False), "本轮派工契约")

    def _tool_scope_targets(self, row: dict, name: str, arguments: dict, ctx: Any, tool: Any):
        from code_agent.agent.team_tool_safety import classify_team_tool

        workspace = self.manager.workspace_for(self.manager.session_repo.get(row["session_id"]))
        if ctx.session_id != row["session_id"] or Path(ctx.workspace).resolve() != workspace.resolve():
            raise TeamError("工具上下文与服务端文件责任身份不一致")
        if row["state"] != "running":
            raise TeamError("当前执行尚未取得运行资格，不能调用工具")
        kind = classify_team_tool(name, tool, arguments)
        if kind == "read":
            return kind, workspace, ()
        if kind in {"shell", "opaque"}:
            if row["role"] != "lead" and "." not in self._scope(row)["write_paths"]:
                raise TeamError("当前子任务只读或仅负责指定文件，不能执行任意 Shell/未知副作用工具；读取请用原生只读工具，验证交主代理或全工作区执行者")
            if arguments.get("background") or arguments.get("run_in_background"):
                raise TeamError("团队文件责任期间不能启动脱离本轮的后台写入；请使用前台执行，以便完成后释放文件责任")
            targets = (actual_target(workspace, ".", directory=True),)
        else:
            raws = tool.write_targets(arguments)
            if not raws:
                raise TeamError("写入工具没有提供可核查的目标路径，已拒绝执行")
            targets = tuple(actual_target(workspace, str(raw)) for raw in raws)
            if row["role"] != "lead":
                allowed = self._scope_targets(row)
                if any(not any(contains(limit, target) for limit in allowed) for target in targets):
                    raise TeamError("目标文件超出本轮负责范围：" + "、".join(str(raw) for raw in raws))
        if row["role"] != "lead" and not self._file_ownership.covers((row["session_id"], row["activation_id"]), targets):
            raise TeamError("本轮尚未持有这些文件的写入责任，或路径已变化；请等待重新取得责任后再写入")
        return kind, workspace, targets

    def check_tool_scope(self, session_id: str, user_id: str, name: str, arguments: dict, ctx: Any, tool: Any) -> None:
        """Reject impossible requests before asking for one-use approval."""
        from code_agent.tools.base import ToolError

        if not self._row(session_id):
            return
        try:
            row = self._member(session_id, user_id)
            kind, _, targets = self._tool_scope_targets(row, name, arguments, ctx, tool)
            if kind != "read" and row["role"] == "lead":
                owners = self._file_ownership.blockers(session_id, targets)
                if owners:
                    public = self._public_blockers(row, owners, targets)
                    raise TeamError("文件正由子任务负责，请等待其完成后再修改：" + "、".join(item["title"] or item["session_id"] for item in public))
        except TeamError as exc:
            raise ToolError(str(exc)) from exc

    @contextlib.asynccontextmanager
    async def tool_write_guard(self, session_id: str, user_id: str, name: str, arguments: dict, ctx: Any, tool: Any):
        """Final execution boundary; neither approval nor full-access bypasses it."""
        from code_agent.tools.base import ToolError

        row = self._row(session_id)
        if not row:
            yield
            return
        serial_lock = None
        try:
            row = self._member(session_id, user_id)
            kind, workspace, targets = self._tool_scope_targets(row, name, arguments, ctx, tool)
            if kind != "read":
                permission_before_wait = self._effective_permission(row)
                lock = self._tool_write_locks.setdefault(session_id, asyncio.Lock())
                await lock.acquire()
                serial_lock = lock
                current = self._member(session_id, user_id)
                if current["activation_id"] != row["activation_id"] or current["state"] != "running":
                    raise TeamError("等待工具期间执行编号或运行资格已变化")
                if self._effective_permission(current) != permission_before_wait:
                    raise TeamError("等待文件工具期间权限已变化，请在当前权限下重新尝试或申请单次授权")
                if self.manager.workspace_for(self.manager.session_repo.get(session_id)).resolve() != workspace.resolve():
                    raise TeamError("等待文件工具期间工作区已变化，请重新派工")
                _, _, targets = self._tool_scope_targets(current, name, arguments, ctx, tool)
            if kind == "read":
                yield
                return
            if row["role"] != "lead":
                yield
                return
            key = object()
            if not self._file_ownership.claim(key, Claim(session_id, row["activation_id"], targets, row["title"])):
                owners = self._public_blockers(row, self._file_ownership.blockers(session_id, targets), targets)
                raise TeamError("文件正由子任务负责，请等待其完成后再修改：" + "、".join(owner["title"] or owner["session_id"] for owner in owners))
            try:
                yield
            finally:
                self._file_ownership.release(key)
                await self._notify_changed(row["project_id"])
        except TeamError as exc:
            raise ToolError(str(exc)) from exc
        finally:
            if serial_lock is not None:
                serial_lock.release()

    def descriptor(self, session_id: str) -> dict | None:
        row = self._row(session_id)
        if not row:
            return None
        result = self._public(row)
        runtime = getattr(self.manager, "_runtimes", {}).get(session_id)
        busy = row["state"] in BUSY_STATES
        phase = str(getattr(runtime, "phase", "") or "") if busy else ""
        phase = phase or {"queued": "queued", "waiting": "waiting_children", "running": "tools"}.get(row["state"], row["state"])
        if row["state"] == "queued":
            phase = "queued"
        elif row["state"] == "waiting":
            phase = "queued" if (session_id, row["activation_id"]) in self._approval_suspensions else "waiting_children"
        pending = None
        callback = getattr(runtime, "pending_approval", None)
        if busy and callback:
            pending = callback()
        if pending:
            phase = "waiting_approval"
            pending = {**pending, "session_id": session_id}
        questions_callback = getattr(runtime, "pending_questions", None)
        questions = questions_callback() if busy and questions_callback else []
        if questions:
            phase = "waiting_question"
        blocked_reason = ""
        if phase == "waiting_question":
            blocked_reason = "等待你回答问题"
        elif phase == "waiting_approval":
            blocked_reason = str((pending or {}).get("reason") or "等待单次权限审批")
        elif phase == "queued":
            blocked_reason = "等待团队执行名额"
        elif phase == "waiting_children":
            blocked_reason = "等待子智能体结果"
        meta = self.manager.session_repo.get(session_id) or {}
        effective = "read-only"
        with contextlib.suppress(TeamError, KeyError):
            effective = self._effective_permission(row)
        run = self.db.query_one("SELECT prompt,created_at,updated_at FROM project_agent_runs WHERE session_id=? AND activation_id=?",
                                (session_id, row["activation_id"]))
        elapsed = 0.0
        if run:
            with contextlib.suppress(ValueError, TypeError):
                start = datetime.fromisoformat(run["created_at"])
                end = datetime.now(timezone.utc) if busy else datetime.fromisoformat(run["updated_at"])
                elapsed = max(0.0, (end - start).total_seconds())
        budget_status = getattr(runtime, "execution_budget_status", None)
        spec = self._scope(row)
        blockers: list[dict] = []
        if row["state"] == "queued":
            with contextlib.suppress(TeamError):
                blockers = self._file_blockers(row)
                if self._dependencies_pending(row):
                    phase, blocked_reason = "waiting_dependencies", "等待前置子任务成功完成"
                elif blockers:
                    phase = "waiting_files"
                    blocked_reason = "等待文件负责人：" + "、".join(item["title"] or item["session_id"] for item in blockers)
        return result | {
            "write_paths": spec["write_paths"], "scope_mode": spec["scope_mode"],
            "delegation_reason": spec["delegation_reason"], "depends_on": spec["depends_on"],
            "file_ownership": {"held": bool(self._file_ownership.held_by(session_id, row["activation_id"])), "waiting_for": blockers},
            "batch_id": (meta.get("task_id") or "") if row["tree_id"] == "main" and str(meta.get("task_id") or "").startswith("b_") else "",
            "phase": phase, "blocked_reason": blocked_reason, "pending_approval": pending,
            "pending_questions": questions,
            "configured_permission": meta.get("permission_mode") or "read-only", "effective_permission": effective,
            "current_tool": str(getattr(runtime, "current_tool", "") or "") if busy else "",
            "last_progress_at": str(getattr(runtime, "last_progress_at", "") or row["updated_at"]),
            "started_at": run["created_at"] if run else "", "elapsed_seconds": round(elapsed, 1),
            "last_task_message": run["prompt"][:2000] if run else "",
            "budget": budget_status() if budget_status else None,
        }

    def _changed(self, session_id: str) -> None:
        """Publish a small invalidation; the manager handles cross-loop delivery."""
        callback = getattr(self.manager, "notify_team_changed", None)
        if callback:
            callback(session_id)

    def _owned_project(self, project_id: str, user_id: str) -> dict:
        row = self.manager.project_repo.get(project_id) if self.manager.project_repo else None
        if not row or row["user_id"] != user_id:
            raise KeyError("项目不存在或无权访问")
        return row

    def _member(self, session_id: str, user_id: str, *, live: bool = True) -> dict:
        row = self._row(session_id)
        meta = self.manager.session_repo.get(session_id)
        if not row or row["user_id"] != user_id or not meta or meta["user_id"] != user_id:
            raise KeyError("智能体不存在或无权访问")
        self._owned_project(row["project_id"], user_id)
        if meta.get("project_id") != row["project_id"] or meta.get("tree_id", "main") != row["tree_id"]:
            raise TeamError("智能体会话绑定已改变，请重新打开项目")
        if live and (row["state"] == "retired" or meta.get("status") != "active"):
            raise TeamError("智能体已退出工作流，不能执行或委派任务")
        if row["role"] == "lead" and (row["parent_session_id"] or row["lead_session_id"] != session_id):
            raise TeamError("主智能体会话归属无效")
        if live and row["role"] != "lead":
            # Checking only the immediate parent and Lead leaves deeper
            # descendants executable after an intermediate ancestor is closed.
            # Walk iteratively so corrupted cycles also fail without recursion.
            current, seen = row, {session_id}
            while current["role"] != "lead":
                parent_id = current["parent_session_id"]
                if parent_id in seen:
                    raise TeamError("智能体父级关系异常")
                seen.add(parent_id)
                parent = self._row(parent_id)
                parent_meta = self.manager.session_repo.get(parent_id)
                if not parent or not parent_meta or parent["state"] == "retired" or parent_meta.get("status") != "active":
                    raise TeamError("父智能体或祖先已不可用，请重新打开项目或重新委派")
                if (parent["project_id"] != row["project_id"] or parent["user_id"] != user_id
                        or parent["lead_session_id"] != row["lead_session_id"]
                        or parent_meta.get("user_id") != user_id
                        or parent_meta.get("project_id") != row["project_id"]
                        or parent_meta.get("tree_id", "main") != parent["tree_id"]
                        or (parent["role"] != "lead" and parent["tree_id"] != current["tree_id"])):
                    raise TeamError("子智能体不属于当前对话或会话绑定已改变")
                current = parent
            if current["session_id"] != row["lead_session_id"] or current["parent_session_id"]:
                raise TeamError("主智能体会话归属无效")
        return row

    def _insert(self, meta: dict, *, parent: str, lead: str, role: str, depth: int, title: str, node_id: str = "") -> dict:
        stamp = _now()
        self.db.execute(
            "INSERT INTO project_agents (session_id,project_id,user_id,parent_session_id,lead_session_id,"
            "tree_id,node_id,role,depth,title,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (meta["id"], meta["project_id"], meta["user_id"], parent, lead, meta["tree_id"], node_id,
             role, depth, title[:120], stamp, stamp),
        )
        self._changed(meta["id"])
        return self.descriptor(meta["id"])  # type: ignore[return-value]

    def bind_lead(self, meta: dict) -> dict:
        """Bind the canonical project main session. Never create a second Lead."""
        project = self._owned_project(meta.get("project_id", ""), meta["user_id"])
        if meta.get("tree_id") not in ("", "main") or meta.get("kind") not in ("", "main", "chat") or meta.get("task_id"):
            raise TeamError("项目 Lead 必须绑定主工作区会话")
        with self._lock:
            # Legacy projects may have several historical main conversations.
            # Whichever one is opened first after an upgrade must not steal the
            # role from the project's current, valid main-session pointer.
            canonical = self.manager.session_repo.get(project.get("session_id") or "")
            if self._valid_main(canonical, project):
                meta = canonical
            if not self._valid_main(meta, project):
                raise TeamError("项目 Lead 会话绑定无效")
            # A surviving chat can become the default without changing its
            # persisted binding or restarting its already-running coordinator.
            if meta.get("kind") == "chat":
                return self.bind_conversation(meta)
            existing = self.db.query_one(
                "SELECT a.* FROM project_agents a JOIN sessions s ON s.id=a.session_id "
                "WHERE a.project_id=? AND a.role='lead' AND a.state!='retired' AND s.kind IN ('','main') "
                "AND (a.session_id=? OR s.status='closed') ORDER BY (a.session_id=?) DESC LIMIT 1",
                (project["id"], meta["id"], meta["id"]),
            )
            if existing:
                live_meta = self.manager.session_repo.get(existing["session_id"])
                if self._valid_main(live_meta, project) and existing["session_id"] == meta["id"]:
                    if project.get("session_id") != meta["id"]:
                        self.manager.project_repo.set_session(project["id"], meta["id"])
                    return self.descriptor(existing["session_id"])
                self.cancel_for(existing["session_id"])
                self._cancel_members([existing["session_id"]], retired=True)
                self.db.execute("UPDATE project_agents SET state='retired', updated_at=? WHERE session_id=?", (_now(), existing["session_id"]))
            if not meta.get("tree_id"):
                self.manager.session_repo.set_project_binding(meta["id"], project["id"], "main", "", "main")
                meta = self.manager.session_repo.get(meta["id"])
            previous = self._row(meta["id"])
            if previous:
                if previous["role"] != "lead":
                    raise TeamError("现有节点或子智能体不能改成项目 Lead")
                self.db.execute("UPDATE project_agents SET parent_session_id='',lead_session_id=?,state='idle',activation_id=activation_id+1,"
                                "audit_decision='none',audit_activation_id=0,audit_reason='',audit_reviewer='',audit_updated_at='',error='',updated_at=? WHERE session_id=?",
                                (meta["id"], _now(), meta["id"]))
                lead = self.descriptor(meta["id"])
            else:
                lead = self._insert(meta, parent="", lead=meta["id"], role="lead", depth=0, title=f"{project['name']} · Lead")
            self.manager.project_repo.set_session(project["id"], meta["id"])
            # Existing nodes retain identity and transcripts if the old Lead was
            # closed and replaced. Recursive parent links remain unchanged.
            if existing:
                self.db.execute("UPDATE project_agents SET parent_session_id=? WHERE project_id=? AND lead_session_id=? AND depth=1",
                                (meta["id"], project["id"], existing["session_id"]))
                self.db.execute("UPDATE project_agents SET lead_session_id=? WHERE project_id=? AND lead_session_id=? AND role!='lead'",
                                (meta["id"], project["id"], existing["session_id"]))
            self._changed(meta["id"])
            return lead

    def bind_conversation(self, meta: dict) -> dict:
        """Bind a chat's own coordinator without replacing the project default."""
        if meta.get("kind") != "chat":
            return self.bind_lead(meta)
        project = self._owned_project(meta.get("project_id", ""), meta["user_id"])
        if meta.get("status") != "active" or meta.get("tree_id") != "main" or meta.get("task_id"):
            raise TeamError("项目对话绑定无效")
        with self._lock:
            current = self._row(meta["id"])
            if current:
                if (current["role"] != "lead" or current["lead_session_id"] != meta["id"]
                        or current["parent_session_id"] or current["project_id"] != project["id"]
                        or current["user_id"] != meta["user_id"]):
                    raise TeamError("现有智能体不能接管其他对话")
                if current["state"] == "retired":
                    self.db.execute("UPDATE project_agents SET state='idle',activation_id=activation_id+1,updated_at=? WHERE session_id=?",
                                    (_now(), meta["id"]))
                return self.descriptor(meta["id"])
            return self._insert(meta, parent="", lead=meta["id"], role="lead", depth=0,
                                title=meta.get("title") or "项目对话")

    def conversation_members(self, session_id: str, user_id: str) -> list[str]:
        """Return one owned conversation and its descendants, including archived roots."""
        meta = self.manager.session_repo.get(session_id)
        if not meta or meta["user_id"] != user_id:
            raise KeyError("会话不存在或无权访问")
        project = self._owned_project(meta.get("project_id") or "", user_id)
        is_main = meta.get("kind") in ("", "main") and meta.get("tree_id") in ("", "main")
        is_chat = meta.get("kind") == "chat" and meta.get("tree_id") == "main"
        if (meta.get("task_id") or not (is_main or is_chat)
                or (is_main and meta["id"] != project.get("session_id") and meta.get("status") != "archived")):
            raise TeamError("目标不是用户对话")
        row = self._row(session_id)
        if row is None:
            return [session_id]
        self._member(session_id, user_id, live=False)
        if row["role"] != "lead":
            raise TeamError("目标不是用户对话")
        return self._subtree(session_id, include_self=True)

    @staticmethod
    def _valid_main(meta: dict | None, project: dict) -> bool:
        return bool(meta and meta.get("project_id") == project["id"] and meta.get("user_id") == project["user_id"]
                    and meta.get("status") == "active" and not meta.get("task_id")
                    and meta.get("tree_id") in ("", "main")
                    and (meta.get("kind") in ("", "main")
                         or (meta.get("kind") == "chat" and meta["id"] == project.get("session_id") and meta.get("tree_id") == "main")))

    def _lead(self, project_id: str, user_id: str) -> dict:
        project = self._owned_project(project_id, user_id)
        meta = self.manager.session_repo.get(project.get("session_id") or "")
        if self._valid_main(meta, project):
            return self.bind_lead(meta)
        meta = self.manager.create_project_session(user_id, project_id, permission_mode="read-only", reuse=True)
        return self.bind_lead(meta)

    def _tree_workspace(self, project: dict, tree: str, lead: dict) -> tuple[str, str]:
        if not isinstance(tree, str) or not _TREE.fullmatch(tree):
            raise TeamError("工作树标识无效")
        if tree == "main":
            meta = self.manager.session_repo.get(lead["session_id"])
            path = self.manager.workspace_for(meta)
            if not path.is_dir():
                raise TeamError("项目工作区目录不可用")
            return str(path), ""
        task_id = tree.removeprefix("task:")
        if (project["id"], task_id) in self._task_operations:
            raise TeamError("任务工作树正在审查、验证或回收，等待操作结束后再派工")
        task = self.db.query_one("SELECT * FROM tasks WHERE id=? AND project_id=? AND user_id=?", (task_id, project["id"], project["user_id"]))
        if not task or not str(task.get("worktree") or "").strip():
            raise KeyError("任务工作树不存在或无权访问")
        if task["status"] in ("merged", "discarded"):
            raise TeamError("任务工作树已结束，不能建立执行智能体")
        path = Path(task["worktree"])
        if not path.is_dir():
            raise TeamError("任务工作树目录不可用")
        return str(path), task_id

    def _create_worker(self, project: dict, parent: dict, *, tree: str, role: str, title: str, node_id: str = "") -> dict:
        if (project["id"], tree.removeprefix("task:")) in self._task_operations:
            raise TeamError("任务工作树正在审查、验证或回收，不能创建执行智能体")
        parent_meta = self.manager.session_repo.get(parent["session_id"])
        if role == "node" or parent["role"] == "lead":
            workspace, task_id = self._tree_workspace(project, tree, parent)
            mode = (parent_meta.get("permission_mode") or "read-only") if tree == "main" else settings.permission_mode()
        else:
            if parent["tree_id"] != tree:
                raise TeamError("子智能体必须继承父智能体的工作树")
            workspace = str(self.manager.workspace_for(parent_meta))
            task_id = str(parent_meta.get("task_id") or "")
            mode = parent_meta.get("permission_mode") or "workspace-write"
            # Main-tree node grants follow Lead by policy, even if their saved
            # initial configuration predates a later Lead grant. Copy the
            # configured ancestor grant, never a temporary runtime clamp.
            ancestor = self._row(parent["parent_session_id"])
            if tree == "main" and ancestor and ancestor["role"] == "lead":
                mode = (self.manager.session_repo.get(ancestor["session_id"]) or {}).get("permission_mode") or "read-only"
        meta = self.manager.session_repo.create(
            user_id=project["user_id"], title=f"[智能体] {title}"[:100], folder=project["name"][:60],
            permission_mode=mode,
            project_id=project["id"], tree_id=tree, task_id=task_id, kind="node" if role == "node" else "subagent",
            workspace_override=workspace, web_enabled=int(bool(parent_meta.get("web_enabled"))),
        )
        return self._insert(meta, parent=parent["session_id"], lead=parent["lead_session_id"], role=role,
                            depth=parent["depth"] + 1, title=title, node_id=node_id)

    def validate_tree(self, project_id: str, user_id: str, tree: str = "main") -> dict:
        """Check ownership and workspace before committing a workflow revision."""
        with self._lock:
            project = self._owned_project(project_id, user_id)
            lead = self._lead(project_id, user_id)
            workspace, task_id = self._tree_workspace(project, tree, lead)
            return {"tree_id": tree, "task_id": task_id, "workspace": workspace, "lead_session_id": lead["session_id"]}

    def sync(self, project_id: str, user_id: str, tree: str = "main", conversation_id: str = "") -> dict:
        """Expose only tasks created on demand for the selected conversation."""
        with self._lock:
            project = self._owned_project(project_id, user_id)
            if conversation_id:
                meta = self.manager.session_repo.get(conversation_id)
                if not meta or meta.get("user_id") != user_id or meta.get("project_id") != project_id:
                    raise KeyError("对话不存在或无权访问")
                lead = self.bind_conversation(meta)
                if lead["session_id"] != conversation_id:
                    raise TeamError("历史主对话不能接管当前会话")
            else:
                lead = self._lead(project_id, user_id)
            self._tree_workspace(project, tree, lead)
            # Legacy fixed-stage owners are retired, with transcripts retained.
            # Saving or reading a workflow never creates another agent.
            legacy = self.db.query_all("SELECT session_id FROM project_agents WHERE project_id=? AND lead_session_id=? AND role='node' AND state!='retired'",
                                       (project_id, lead["session_id"]))
            for old in legacy:
                self._cancel_members(self._subtree(old["session_id"], include_self=True), retired=True)
            return self._snapshot(project_id, tree, lead)

    def _snapshot(self, project_id: str, tree: str, lead: dict | None = None) -> dict:
        if lead is None:
            project = self.manager.project_repo.get(project_id)
            lead_row = self._row((project or {}).get("session_id") or "")
            lead = self.descriptor(lead_row["session_id"]) if lead_row else None
        elif lead:
            lead = self.descriptor(lead["session_id"])
        root_id = lead["session_id"] if lead else ""
        rows = self.db.query_all(
            "SELECT * FROM project_agents WHERE project_id=? AND lead_session_id=? AND role='subagent' AND state!='retired' "
            "ORDER BY depth,created_at,session_id",
            (project_id, root_id),
        )
        all_states = rows
        return {"lead": lead, "agents": [self.descriptor(row["session_id"]) for row in rows], "limits": self.limits(),
                "running": sum(row["state"] == "running" for row in all_states),
                "queued": sum(row["state"] == "queued" for row in all_states)}

    def _subtree(self, session_id: str, *, include_self: bool = False) -> list[str]:
        root = self._row(session_id)
        if root is None:
            return []
        rows = self.db.query_all("SELECT session_id,parent_session_id FROM project_agents WHERE project_id=? AND lead_session_id=?",
                                 (root["project_id"], root["lead_session_id"]))
        children: dict[str, list[str]] = {}
        for row in rows:
            children.setdefault(row["parent_session_id"], []).append(row["session_id"])
        found: set[str] = {session_id}
        pending = [session_id]
        while pending:
            for child in children.get(pending.pop(), []):
                if child not in found:
                    found.add(child)
                    pending.append(child)
        if not include_self:
            found.remove(session_id)
        return sorted(found)

    def _authorize_target(self, caller: dict, target: dict, *, allow_self: bool = False) -> None:
        if (caller["project_id"] != target["project_id"] or caller["user_id"] != target["user_id"]
                or caller["lead_session_id"] != target["lead_session_id"]):
            raise KeyError("智能体不存在或无权访问")
        if allow_self and caller["session_id"] == target["session_id"]:
            return
        if target["session_id"] not in self._subtree(caller["session_id"]):
            raise TeamError("只能管理自己的后代智能体，不能控制父级、同级或其他项目")

    def list(self, caller_session_id: str, user_id: str, tree: str | None = None) -> dict:
        caller = self._member(caller_session_id, user_id)
        tree = tree or caller["tree_id"]
        if caller["role"] == "lead":
            return self.sync(caller["project_id"], user_id, tree, conversation_id=caller_session_id)
        if tree != caller["tree_id"]:
            raise TeamError("子智能体只能查看自己的工作树")
        snapshot = self._snapshot(caller["project_id"], tree, self._row(caller["lead_session_id"]))
        allowed = set(self._subtree(caller_session_id, include_self=True))
        snapshot["agents"] = [row for row in snapshot["agents"] if row["session_id"] in allowed]
        # Lead identity is useful for reporting, but workers never receive its
        # private result or audit details through this tool.
        snapshot["lead"] = {key: snapshot["lead"][key] for key in ("session_id", "title", "role", "depth")} if snapshot["lead"] else None
        return snapshot

    async def start(self, caller_session_id: str, user_id: str, prompt: str, *, node_id: str = "", parent_id: str = "",
                    title: str = "", tree: str | None = None, context_strategy: str = "task_brief", budget: dict | None = None,
                    write_paths: list[str] | None = None, delegation_reason: str = "", depends_on: list[str] | None = None) -> dict:
        if self._closed:
            raise TeamError("智能体调度器已关闭")
        prompt = _string(prompt, "任务指令")
        node_id = _string(node_id, "节点 ID", required=False, limit=64)
        parent_id = _string(parent_id, "父智能体 ID", required=False, limit=64)
        title = _string(title, "任务标题", required=False, limit=120)
        self._context_strategy(context_strategy)
        budget = self._budget(budget)
        caller = self._member(caller_session_id, user_id)
        project = self._owned_project(caller["project_id"], user_id)
        if caller["role"] != "lead":
            if tree is not None and tree != caller["tree_id"]:
                raise TeamError("子智能体不能跨工作树委派任务")
            if node_id or (parent_id and parent_id != caller_session_id):
                raise TeamError("子智能体只能在自身下面创建子任务，不能接管工作流节点或同级智能体")
            tree = caller["tree_id"]
            parent = caller
        else:
            tree = tree or "main"
            if node_id and parent_id:
                raise TeamError("工作流节点和父智能体只能选择一项")
            parent = self._member(parent_id, user_id) if parent_id else caller
            self._authorize_target(caller, parent, allow_self=True)
            if parent["role"] != "lead":
                tree = parent["tree_id"]
        if node_id:
            # Legacy callers may pass a workflow step as a task brief, but this
            # no longer preallocates or reuses a persistent stage conversation.
            document = self.workflow_store.get(project["id"], tree)
            node = next((item for item in document["nodes"] if item["id"] == node_id), None)
            if not node:
                raise TeamError("工作流节点不存在，请先保存该节点")
            title = title or node["title"]
        if parent["depth"] + 1 > self.limits()["max_depth"]:
            raise TeamError("已达到最大递归深度，不能再创建子智能体")
        workspace, _ = self._tree_workspace(project, tree, self._row(parent["lead_session_id"]))
        spec = self._prepare_scope(parent, Path(workspace), write_paths, delegation_reason, depends_on)
        target = self._create_worker(project, parent, tree=tree, role="subagent", title=title or prompt[:60])
        if target["depth"] > self.limits()["max_depth"]:
            raise TeamError("该智能体超过当前最大递归深度")
        return self._dispatch(target, user_id, prompt, caller_session_id, context_strategy, budget, scope=spec)

    @staticmethod
    def _context_strategy(strategy: str) -> str:
        if strategy not in ("task_brief", "none", "all"):
            raise TeamError("context_strategy 必须是 task_brief、none 或 all")
        return strategy

    @staticmethod
    def _budget(budget: dict | None) -> dict | None:
        if budget is None:
            return None
        if not isinstance(budget, dict) or set(budget) - {"max_tokens", "max_seconds"}:
            raise TeamError("budget 必须是仅含 max_tokens、max_seconds 的对象")
        result = {"max_tokens": budget.get("max_tokens", 0), "max_seconds": budget.get("max_seconds", 0)}
        for name, ceiling in (("max_tokens", 10_000_000), ("max_seconds", 86_400)):
            if type(result[name]) is not int or not 0 <= result[name] <= ceiling:
                raise TeamError(f"budget.{name} 必须是 0 到 {ceiling} 的整数；0 表示不单独限制")
        return result

    def _dispatch(self, target: dict, user_id: str, prompt: str, context_parent: str, context_strategy: str, budget: dict | None = None,
                  *, scope: dict | None = None) -> dict:
        sid = target["session_id"]
        if (target["project_id"], target["tree_id"].removeprefix("task:")) in self._task_operations:
            raise TeamError("任务工作树正在审查、验证或回收，不能开始新一轮执行")
        runtime = self.manager.get_runtime(sid, user_id)
        if target["state"] in BUSY_STATES or sid in self._starts or sid in self._executions or getattr(runtime, "busy", False):
            raise TeamError("该智能体正在执行或排队；补充信息请使用 send_agent_message，继续派工须等待或中断当前轮次")
        budget_callback = getattr(runtime, "set_execution_budget", None)
        if budget is not None and budget_callback is None:
            raise TeamError("当前运行时不支持执行预算，不能保证此预算生效")
        callback = getattr(runtime, "set_delegation_context", None)
        if callback:
            callback(context_parent, context_strategy, prompt)
        if target["tree_id"].startswith("task:") and self._effective_permission(self._member(sid, user_id)) != "read-only":
            # New writable activity makes a previous test run provisional even
            # before a user refreshes the review snapshot.
            task_id = target["tree_id"].removeprefix("task:")
            task = self.db.query_one("SELECT review_json FROM tasks WHERE id=? AND project_id=?", (task_id, target["project_id"]))
            with contextlib.suppress(TypeError, json.JSONDecodeError):
                review = json.loads(task.get("review_json") or "{}") if task else {}
                if isinstance(review, dict) and review.get("verification"):
                    review["verification_stale"] = True
                    self.db.execute("UPDATE tasks SET review_json=? WHERE id=?", (json.dumps(review, ensure_ascii=False), task_id))
        activation = self._begin_activation(sid, prompt, "queued")
        if scope is not None:
            self._save_scope(target, activation, scope)
        if budget_callback:
            try:
                budget_callback(budget, activation_id=activation, parent_session_id=context_parent)
            except Exception as exc:
                self._finish(sid, activation, "failed", error=f"执行预算初始化失败：{exc}"[:4000])
                raise
        task = asyncio.create_task(self._submit_started(runtime, prompt, activation))
        self._starts[sid] = task
        # Cancelling before a coroutine first starts skips its try/finally.
        # A done callback must still clear that queued reservation, otherwise
        # this persistent node could never be delegated to again.
        def settled(done: asyncio.Task) -> None:
            if self._starts.get(sid) is done:
                self._starts.pop(sid, None)
            if done.cancelled():
                self._finish(sid, activation, "cancelled", error="执行已停止")
        task.add_done_callback(settled)
        return self.descriptor(sid)  # type: ignore[return-value]

    async def followup(self, caller_session_id: str, user_id: str, target_session_id: str, prompt: str, *,
                       context_strategy: str = "task_brief", budget: dict | None = None, write_paths: list[str] | None = None,
                       delegation_reason: str = "", depends_on: list[str] | None = None) -> dict:
        """Reuse one owned descendant's workspace/history, with a new run id."""
        if self._closed:
            raise TeamError("智能体调度器已关闭")
        prompt = _string(prompt, "后续任务")
        self._context_strategy(context_strategy)
        budget = self._budget(budget)
        caller = self._member(caller_session_id, user_id)
        target = self._member(target_session_id, user_id)
        self._authorize_target(caller, target)
        if target["depth"] > self.limits()["max_depth"]:
            raise TeamError("该智能体超过当前最大递归深度")
        if depends_on and target_session_id in depends_on:
            raise TeamError("任务不能依赖自己的执行")
        ancestor = self._row(target["parent_session_id"])
        while ancestor:
            if depends_on and ancestor["session_id"] in depends_on:
                raise TeamError("任务不能依赖仍负责协调它的祖先，否则会造成相互等待")
            ancestor = self._row(ancestor["parent_session_id"]) if ancestor["parent_session_id"] else None
        workspace = self.manager.workspace_for(self.manager.session_repo.get(target_session_id))
        spec = self._prepare_scope(caller, workspace, write_paths, delegation_reason, depends_on, previous=self._scope(target))
        parent = self._row(target["parent_session_id"])
        if parent and parent["role"] != "lead":
            if any(not any(contains(limit, item) for limit in self._scope_targets(parent))
                   for item in scope_targets(workspace, spec["write_paths"])):
                raise TeamError("继续派工不能越过实际父执行者的文件责任；请先调整父任务，或由主代理创建直接子任务")
        return self._dispatch(target, user_id, prompt, caller_session_id, context_strategy, budget, scope=spec)

    async def run_isolated_task(self, project_id: str, user_id: str, task_id: str, prompt: str, *,
                                permission_mode: str) -> dict:
        """Run an isolated task through the same Lead-owned descendant scheduler.

        Historical task conversations are adopted only when their fixed owner,
        tree and workspace match and their runtime is idle. Reading a task list
        never migrates sessions, and an already executing legacy task is left
        alone. Results belong to the dispatched activation, not the agent's
        latest mutable descriptor (a completion receipt may start another turn).
        """
        if self._closed:
            raise TeamError("智能体调度器已关闭")
        prompt = _string(prompt, "任务指令")
        if permission_mode not in ("read-only", "workspace-write", "full-access"):
            raise TeamError("任务权限档位无效")
        with self._lock:
            project = self._owned_project(project_id, user_id)
            lead = self._lead(project_id, user_id)
            tree = f"task:{task_id}"
            workspace, _ = self._tree_workspace(project, tree, lead)
            task = self.db.query_one("SELECT * FROM tasks WHERE id=? AND project_id=? AND user_id=?",
                                     (task_id, project_id, user_id))
            session_id = task.get("session_id") or ""
            normalize = lambda value: os.path.normcase(os.path.realpath(str(value)))
            if session_id:
                meta = self.manager.session_repo.get(session_id)
                if (not meta or meta.get("user_id") != user_id or meta.get("project_id") != project_id
                        or meta.get("task_id") != task_id or meta.get("tree_id") != tree
                        or meta.get("kind") not in ("task", "subagent", "planner") or meta.get("status") != "active"
                        or normalize(self.manager.workspace_for(meta)) != normalize(workspace)):
                    raise TeamError("任务会话的归属或工作树绑定无效，不能接管历史会话")
                cached = getattr(self.manager, "_runtimes", {}).get(session_id)
                previous = self._row(session_id)
                if ((cached is not None and getattr(cached, "busy", False)) or session_id in self._starts
                        or session_id in self._executions or (previous and previous["state"] in BUSY_STATES)):
                    raise TeamError("任务会话仍在执行，等待本轮结束后再派工；不会迁移或中断已有执行")
                if cached is not None and normalize(cached.workspace) != normalize(workspace):
                    raise TeamError("任务运行时工作区与隔离工作树不一致")
                if previous:
                    if (previous["role"] != "subagent" or previous["parent_session_id"] != lead["session_id"]
                            or previous["lead_session_id"] != lead["session_id"] or previous["tree_id"] != tree):
                        raise TeamError("任务会话已绑定其他智能体职责，不能改派")
                    target = self._member(session_id, user_id)
                else:
                    target = self._insert(meta, parent=lead["session_id"], lead=lead["session_id"],
                                          role="subagent", depth=1, title=task["title"])
            else:
                if self.limits()["max_depth"] < 1:
                    raise TeamError("已达到最大递归深度，不能创建任务智能体")
                meta = self.manager.create_task_session(
                    user_id, project_id, task_id=task_id, title=f"[任务] {task['title']}",
                    workspace=Path(workspace), permission_mode=permission_mode,
                )
                session_id = meta["id"]
                self.db.execute("UPDATE tasks SET session_id=?,updated_at=? WHERE id=?", (session_id, _now(), task_id))
                target = self._insert(meta, parent=lead["session_id"], lead=lead["session_id"],
                                      role="subagent", depth=1, title=task["title"])
            if target["depth"] > self.limits()["max_depth"]:
                raise TeamError("该智能体超过当前最大递归深度")
            self.manager.session_repo.set_project_binding(session_id, project_id, tree, task_id,
                                                          "planner" if permission_mode == "read-only" else "task")
            # Planning is read-only; an approved execution may use the current
            # task grant. This never changes the project Lead's permission.
            self.manager.set_permission_mode(session_id, user_id, permission_mode)
            runtime = self.manager.get_runtime(session_id, user_id)
            runtime.agent_team = self
            self._validate_workspace(runtime, self._member(session_id, user_id))
            dispatch = self._dispatch(target, user_id, prompt, lead["session_id"], "task_brief")
            activation = dispatch["activation_id"]
            execution = self._starts[session_id]
        return await self._isolated_result(session_id, activation, execution)

    async def run_batch_planner(self, project_id: str, user_id: str, batch_id: str, prompt: str) -> dict:
        """Plan on main with a separately scheduled, fixed read-only grant.

        A busy Lead retains its conversation and permissions. Planner history
        is persistent and linked to the batch, and recursively delegated reads
        cannot inherit the Lead's stronger main-workspace grant.
        """
        if self._closed:
            raise TeamError("智能体调度器已关闭")
        prompt = _string(prompt, "批次规划指令")
        with self._lock:
            project = self._owned_project(project_id, user_id)
            batch = self.db.query_one("SELECT * FROM task_batches WHERE id=? AND project_id=? AND user_id=?",
                                      (batch_id, project_id, user_id))
            if not batch or batch["status"] in ("merged", "discarded"):
                raise TeamError("批次不存在或已经结束，不能开始规划")
            self.assert_batch_planner_idle(project_id, user_id, batch_id)
            for lane in self.db.query_all("SELECT id FROM tasks WHERE project_id=? AND user_id=? AND batch_id=? AND status NOT IN ('merged','discarded')",
                                          (project_id, user_id, batch_id)):
                self.assert_isolated_task_idle(project_id, user_id, lane["id"])
            lead = self._lead(project_id, user_id)
            workspace, _ = self._tree_workspace(project, "main", lead)
            meta = self.db.query_one("SELECT * FROM sessions WHERE project_id=? AND user_id=? AND kind='planner' AND tree_id='main' AND task_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
                                     (project_id, user_id, batch_id))
            if meta:
                target = self._member(meta["id"], user_id)
                if target["parent_session_id"] != lead["session_id"] or target["role"] != "subagent":
                    raise TeamError("批次规划会话绑定无效")
            else:
                if self.limits()["max_depth"] < 1:
                    raise TeamError("已达到最大递归深度，不能创建规划智能体")
                lead_meta = self.manager.session_repo.get(lead["session_id"])
                meta = self.manager.session_repo.create(
                    user_id, title=f"[规划] {batch['title']}"[:100], folder=project["name"][:60],
                    project_id=project_id, tree_id="main", task_id=batch_id, kind="planner",
                    workspace_override=workspace, permission_mode="read-only",
                    web_enabled=int(bool(lead_meta.get("web_enabled"))),
                )
                target = self._insert(meta, parent=lead["session_id"], lead=lead["session_id"],
                                      role="subagent", depth=1, title=f"{batch['title']} · 规划")
            runtime = self.manager.get_runtime(meta["id"], user_id)
            runtime.agent_team = self
            self._validate_workspace(runtime, self._member(meta["id"], user_id))
            self._apply_permissions(runtime, self._member(meta["id"], user_id))
            dispatch = self._dispatch(target, user_id, prompt, lead["session_id"], "task_brief")
            execution = self._starts[meta["id"]]
        return await self._isolated_result(meta["id"], dispatch["activation_id"], execution)

    async def _isolated_result(self, session_id: str, activation: int, execution: asyncio.Task) -> dict:
        result = await execution
        run = self.db.query_one("SELECT state,result,error FROM project_agent_runs WHERE session_id=? AND activation_id=?",
                                (session_id, activation))
        if not run or run["state"] != "completed":
            raise TeamError((run or {}).get("error") or "任务本轮执行未完成，不能进入审查")
        return {"text": run["result"], "stop_reason": getattr(result, "stop_reason", "end_turn"),
                "turns": getattr(result, "turns", 0), "session_id": session_id,
                "activation_id": activation, "state": run["state"]}

    def assert_isolated_task_idle(self, project_id: str, user_id: str, task_id: str) -> None:
        """All descendants and historical runtimes must settle before cleanup."""
        self._owned_project(project_id, user_id)
        task = self.db.query_one("SELECT id FROM tasks WHERE id=? AND project_id=? AND user_id=?",
                                 (task_id, project_id, user_id))
        if not task:
            raise KeyError("任务不存在或无权访问")
        tree = f"task:{task_id}"
        rows = self.db.query_all("SELECT session_id,state FROM project_agents WHERE project_id=? AND user_id=? AND tree_id=?",
                                 (project_id, user_id, tree))
        if any(row["state"] in BUSY_STATES for row in rows):
            raise TeamError("任务工作树仍有智能体执行、排队或等待审批/子任务，暂不能操作")
        sessions = self.db.query_all("SELECT id FROM sessions WHERE project_id=? AND user_id=? AND tree_id=?",
                                     (project_id, user_id, tree))
        for meta in sessions:
            sid = meta["id"]
            pending = self._starts.get(sid) or self._executions.get(sid)
            runtime = getattr(self.manager, "_runtimes", {}).get(sid)
            approval = getattr(runtime, "pending_approval", None)
            if ((pending and not pending.done()) or getattr(runtime, "busy", False) or (approval and approval())):
                raise TeamError("任务工作树仍有执行或待审批命令，等待结束后再操作")

    def assert_batch_planner_idle(self, project_id: str, user_id: str, batch_id: str) -> None:
        self._owned_project(project_id, user_id)
        members = self.db.query_all(
            "SELECT s.id,a.state FROM sessions s LEFT JOIN project_agents a ON a.session_id=s.id "
            "WHERE s.project_id=? AND s.user_id=? AND s.tree_id='main' AND s.task_id=?", (project_id, user_id, batch_id),
        )
        for member in members:
            runtime = getattr(self.manager, "_runtimes", {}).get(member["id"])
            approval = getattr(runtime, "pending_approval", None)
            if member.get("state") in BUSY_STATES or getattr(runtime, "busy", False) or (approval and approval()):
                raise TeamError("批次规划或规划子任务仍在执行，等待结束后再操作")

    @contextlib.contextmanager
    def isolated_task_maintenance(self, project_id: str, user_id: str, task_id: str):
        """Reserve an idle tree across awaited Git/verification operations."""
        key = (project_id, task_id)
        with self._lock:
            if key in self._task_operations:
                raise TeamError("任务工作树正在进行其他操作，请等待完成")
            self.assert_isolated_task_idle(project_id, user_id, task_id)
            self._task_operations.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._task_operations.discard(key)

    async def send_message(self, caller_session_id: str, user_id: str, target_session_id: str, message: str) -> dict:
        """Queue a durable message for an owned agent; never start an idle turn."""
        message = _string(message, "补充消息")
        caller = self._member(caller_session_id, user_id)
        target = self._member(target_session_id, user_id)
        self._authorize_target(caller, target)
        runtime = self.manager.get_runtime(target_session_id, user_id)
        callback = getattr(runtime, "enqueue_agent_message", None)
        if callback is None:
            raise TeamError("当前运行时不支持补充消息")
        payload = {"sender_session_id": caller_session_id, "session_id": target_session_id,
                   "activation_id": target["activation_id"], "message": message, "created_at": _now()}
        receipt = callback(payload)
        if inspect.isawaitable(receipt):
            receipt = await receipt
        self._changed(target_session_id)
        return {"queued": True, "session_id": target_session_id, "activation_id": target["activation_id"],
                "receipt": receipt if isinstance(receipt, dict) else None, "starts_turn": False}

    async def _submit_started(self, runtime: Any, prompt: str, activation: int) -> Any:
        sid = runtime.session_id
        token = _START_ID.set((sid, activation))
        try:
            return await runtime.submit(prompt)
        except asyncio.CancelledError:
            self._finish(sid, activation, "cancelled", error="执行已停止")
        except Exception as exc:
            self._finish(sid, activation, "failed", error=str(exc)[:4000])
        finally:
            _START_ID.reset(token)
            if self._starts.get(sid) is asyncio.current_task():
                self._starts.pop(sid, None)
            await self._notify_changed(self._row(sid)["project_id"] if self._row(sid) else "")

    def _begin_activation(self, session_id: str, prompt: str, state: str) -> int:
        with self._lock:
            row = self._row(session_id)
            if not row or row["state"] in BUSY_STATES or row["state"] == "retired":
                raise TeamError("智能体当前不可执行")
            activation = row["activation_id"] + 1
            # Lead owns user turns, not a delegated file contract. Its writes
            # acquire short responsibility in tool_write_guard. Avoid another
            # durable write for each automatic receipt-processing wake.
            spec = {**self._scope(row), "depends_on": []} if row["role"] != "lead" else None
            stamp = _now()
            self.db.execute(
                "UPDATE project_agents SET activation_id=?,state=?,result='',error='',audit_decision='none',audit_reason='',"
                "audit_reviewer='',audit_activation_id=0,audit_updated_at='',updated_at=? WHERE session_id=?",
                (activation, state, stamp, session_id),
            )
            self.db.execute("INSERT INTO project_agent_runs (session_id,activation_id,prompt,state,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                            (session_id, activation, prompt, state, stamp, stamp))
            if spec is not None:
                self._save_scope(row, activation, spec)
            self._changed(session_id)
            return activation

    def _capacity(self, project_id: str) -> _Capacity:
        if project_id not in self._capacities:
            self._capacities[project_id] = _Capacity()
        return self._capacities[project_id]

    def _queue_predecessor(self, capacity: _Capacity, sid: str, targets) -> bool:
        """Keep order for overlapping scopes without blocking unrelated files."""
        for earlier in capacity.queue:
            if earlier == sid:
                return False
            candidate = self._row(earlier)
            if not candidate or candidate["state"] not in BUSY_STATES:
                continue
            try:
                if self._dependencies_pending(candidate):
                    continue
                earlier_targets = self._scope_targets(candidate)
                if (not self._file_ownership.blockers(earlier, earlier_targets)
                        or any(overlaps(a, b) for a in targets for b in earlier_targets)):
                    return True
            except TeamError:
                continue
        return False

    async def _acquire(self, row: dict, activation: int) -> None:
        capacity = self._capacity(row["project_id"])
        sid = row["session_id"]
        async with capacity.condition:
            if sid not in capacity.queue:
                capacity.queue.append(sid)
            try:
                while True:
                    current = self._member(sid, row["user_id"])
                    if current["activation_id"] != activation or current["state"] == "cancelled":
                        raise asyncio.CancelledError
                    if current["depth"] > self.limits()["max_depth"]:
                        raise TeamError("该智能体超过当前最大递归深度")
                    targets = self._scope_targets(current)
                    ready = not self._dependencies_pending(current) and not self._queue_predecessor(capacity, sid, targets)
                    claim = Claim(sid, activation, targets, current["title"])
                    if (ready and len(capacity.active) < self.limits()["max_parallel"]
                            and self._file_ownership.claim((sid, activation), claim)):
                        capacity.queue.remove(sid)
                        capacity.active.add(sid)
                        self.db.execute("UPDATE project_agents SET state='running',updated_at=? WHERE session_id=? AND activation_id=?",
                                        (_now(), sid, activation))
                        self.db.execute("UPDATE project_agent_runs SET state='running',updated_at=? WHERE session_id=? AND activation_id=?",
                                        (_now(), sid, activation))
                        self._changed(sid)
                        capacity.condition.notify_all()
                        return
                    # Dynamic limits may be increased in Settings while all
                    # slots are occupied. Recheck without requiring another run
                    # to complete to wake this queue.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(capacity.condition.wait(), 0.5)
            finally:
                if sid in capacity.queue:
                    capacity.queue.remove(sid)
                    capacity.condition.notify_all()

    async def _release(self, project_id: str, session_id: str) -> None:
        capacity = self._capacity(project_id)
        async with capacity.condition:
            capacity.active.discard(session_id)
            row = self._row(session_id)
            if row:
                self._release_files(row)
            else:
                self._file_ownership.release_session(session_id)
            if session_id in capacity.queue:
                capacity.queue.remove(session_id)
            capacity.condition.notify_all()

    async def _readmit(self, runtime: Any, row: dict, activation: int) -> None:
        """Reacquire a released slot or terminate the entire activation."""
        sid = row["session_id"]
        try:
            self.db.execute("UPDATE project_agents SET state='queued',updated_at=? WHERE session_id=? AND activation_id=? AND state='waiting'",
                            (_now(), sid, activation))
            self.db.execute("UPDATE project_agent_runs SET state='queued',updated_at=? WHERE session_id=? AND activation_id=? AND state='waiting'",
                            (_now(), sid, activation))
            self._changed(sid)
            await self._acquire(row, activation)
            current = self._member(sid, row["user_id"])
            if current["activation_id"] != activation or current["state"] != "running":
                raise asyncio.CancelledError
            if current["depth"] > self.limits()["max_depth"]:
                raise TeamError("该智能体超过当前最大递归深度")
            if runtime is not None:
                self._validate_workspace(runtime, current)
                self._apply_permissions(runtime, current)
        except Exception as exc:
            # Team tools, permission hooks and the parallel tool executor all
            # turn ordinary Exceptions into results. No model/tool may resume
            # without the slot, even if it would recover from a tool failure.
            raise _ReadmissionFailed(f"重新取得团队执行名额失败：{exc}") from exc

    async def _notify_changed(self, project_id: str) -> None:
        if project_id:
            capacity = self._capacity(project_id)
            async with capacity.condition:
                capacity.condition.notify_all()

    def _finish(self, session_id: str, activation: int, state: str, *, result: str = "", error: str = "") -> None:
        stamp = _now()
        row = self._row(session_id)
        needs_audit = state == "completed" and row and row["role"] != "lead"
        # Retired or superseded workers can settle their old run, but must never
        # overwrite the reactivated node's state or a newer output's audit.
        self.db.execute("UPDATE project_agent_runs SET state=?,result=?,error=?,updated_at=? WHERE session_id=? AND activation_id=?",
                        (state, result[:40_000], error[:4000], stamp, session_id, activation))
        self.db.execute(
            "UPDATE project_agents SET state=?,result=?,error=?,audit_decision=?,audit_activation_id=?,updated_at=? "
            "WHERE session_id=? AND activation_id=? AND state!='retired'",
            (state, result[:40_000], error[:4000], "pending" if needs_audit else "none",
             activation if needs_audit else 0, stamp, session_id, activation),
        )
        self._changed(session_id)

    async def run(self, runtime: Any, prompt: str, attachments: Any = None, code_context: Any = None, *, auto_meta: Any = None) -> Any:
        """SessionRuntime.submit calls this for every managed direct/delegated run."""
        if self._closed:
            raise TeamError("智能体调度器已关闭")
        row = self._member(runtime.session_id, runtime.user_id)
        self._validate_workspace(runtime, row)
        self._apply_permissions(runtime, row)
        sid, pid = row["session_id"], row["project_id"]
        if sid in self._executions:
            raise TeamError("智能体正在执行")
        if row["depth"] > self.limits()["max_depth"]:
            raise TeamError("该智能体超过当前最大递归深度")
        reserved = _START_ID.get()
        if reserved and reserved == (sid, row["activation_id"]) and row["state"] == "queued":
            activation = row["activation_id"]
        else:
            activation = self._begin_activation(sid, _string(prompt, "任务指令"), "running" if row["role"] == "lead" else "queued")
        self._executions[sid] = asyncio.current_task()
        evidence_started = False
        try:
            budget_callback = getattr(runtime, "set_execution_budget", None)
            budget_status = getattr(runtime, "execution_budget_status", None)
            if budget_callback:
                exact = (budget_status() or {}).get("activation_id") == activation if budget_status else bool(reserved == (sid, activation))
                if not exact:
                    # Direct user submissions get a fresh run/scope before
                    # admission; delegated runs already have exact budgets.
                    budget_callback(None, activation_id=activation, continuation=bool(auto_meta and auto_meta.get("auto")))
            if row["role"] != "lead":
                remaining_callback = getattr(runtime, "remaining_execution_seconds", None)
                remaining = remaining_callback() if remaining_callback else None
                deadline = asyncio.timeout(max(0, remaining)) if remaining is not None else None
                try:
                    if deadline is None:
                        await self._acquire(row, activation)
                    else:
                        async with deadline:
                            await self._acquire(row, activation)
                except TimeoutError:
                    if deadline is not None and deadline.expired():
                        from code_agent.sessions.team_budget import ExecutionBudgetExceeded

                        raise ExecutionBudgetExceeded("等待团队执行名额时，任务或团队总耗时预算已达到上限") from None
                    raise
            # Admission can wait behind other workers for a long time. Changes
            # to permissions, the workflow or an isolated worktree during that
            # wait must take effect before constructing the actual AgentLoop.
            current = self._member(sid, runtime.user_id)
            if current["activation_id"] != activation or current["state"] != "running":
                raise asyncio.CancelledError
            if current["depth"] > self.limits()["max_depth"]:
                raise TeamError("该智能体超过当前最大递归深度")
            self._validate_workspace(runtime, current)
            self._apply_permissions(runtime, current)
            transcript = getattr(self.manager, "transcript", None)
            if transcript is not None:
                transcript.append(sid, "session_meta", {"agent_activation": {
                    "activation_id": activation, "phase": "started", "workspace": str(runtime.workspace),
                }})
                evidence_started = True
            if auto_meta is None:
                result = await runtime._run_loop(prompt, attachments, code_context)
            else:
                result = await runtime._run_loop(prompt, attachments, code_context, auto_meta=auto_meta)
            text = str(getattr(result, "text", "") or "")
            reason = str(getattr(result, "stop_reason", "end_turn") or "end_turn")
            failed = reason not in ("end_turn", "completed", "stop") or not text.strip()
            self._finish(sid, activation, "failed" if failed else "completed", result=text,
                         error=f"执行未完成（{reason}）" if failed else "")
            return result
        except _ReadmissionFailed as exc:
            self._finish(sid, activation, "failed", error=str(exc)[:4000])
            raise TeamError(str(exc)) from exc
        except asyncio.CancelledError:
            self._finish(sid, activation, "cancelled", error="执行已停止")
            # Stop owns the subtree; a deliberate current-turn interruption
            # leaves children alive so this persistent agent can continue later.
            if (sid, activation) not in self._interrupting:
                self.cancel_for(sid)
            raise
        except Exception as exc:
            self._finish(sid, activation, "failed", error=str(exc)[:4000])
            raise
        finally:
            if evidence_started:
                # A storage failure must not strand scheduler capacity. A
                # missing end marker is exposed as interrupted evidence.
                with contextlib.suppress(OSError):
                    self.manager.transcript.append(sid, "session_meta", {"agent_activation": {
                        "activation_id": activation, "phase": "finished",
                    }})
            self._executions.pop(sid, None)
            self._interrupting.discard((sid, activation))
            self._approval_suspensions.pop((sid, activation), None)
            if row["role"] != "lead":
                await self._release(pid, sid)
            else:
                await self._notify_changed(pid)
            await self._notify_parent(sid, activation)

    async def _notify_parent(self, session_id: str, activation: int) -> None:
        row = self._row(session_id)
        if not row or row["role"] == "lead" or row["state"] == "retired" or row["activation_id"] != activation:
            return
        parent = self._row(row["parent_session_id"])
        if not parent or parent["state"] == "retired":
            return
        reviewable = (row["state"] == "completed" and row["audit_decision"] == "pending"
                      and row["audit_activation_id"] == activation)
        meta = self.manager.session_repo.get(session_id) or {}
        note = {"session_id": session_id, "activation_id": activation, "title": row["title"], "state": row["state"],
                "result": row["result"][:1600], "result_truncated": len(row["result"]) > 1600,
                "error": row["error"][:400], "error_truncated": len(row["error"]) > 400,
                # Ordinary background results return directly to the current
                # conversation. Keep explicit task/planner review compatible,
                # without making every dynamic child start an audit handoff.
                "audit_required": bool(reviewable and meta.get("kind") != "subagent"),
                "audit_available": bool(reviewable),
                "execution_ref": {"session_id": session_id, "activation_id": activation}}
        try:
            runtime = self.manager.get_runtime(parent["session_id"], row["user_id"])
            callback = getattr(runtime, "enqueue_team_note", None)
            if callback:
                pending = callback(note)
                if inspect.isawaitable(pending):
                    await pending
        except Exception:
            # The completed run is durable even when the parent is closed or a
            # notification consumer is unavailable. team_list/wait still return it.
            return

    @staticmethod
    def _wait_cursor(descriptor: dict) -> str:
        """A phase/run receipt, independent of changing display elapsed time."""
        identity = [descriptor["session_id"], descriptor["activation_id"], descriptor["state"], descriptor["updated_at"],
                    descriptor["phase"], descriptor["audit"], (descriptor.get("pending_approval") or {}).get("approval_id"),
                    [question.get("question_id") or question.get("id") for question in descriptor.get("pending_questions") or []]]
        return "v1:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]

    async def wait(self, caller_session_id: str, user_id: str, session_ids: list[str], timeout: float = 30, *,
                   mode: str = "all", after_cursors: dict[str, str] | None = None, progress: bool = True) -> dict:
        caller = self._member(caller_session_id, user_id)
        if not isinstance(session_ids, list) or not 1 <= len(session_ids) <= 64 or any(not isinstance(s, str) or not s for s in session_ids):
            raise TeamError("等待目标必须是 1 到 64 个智能体 ID")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise TeamError("等待时间必须在 0 到 60 秒之间")
        ids = list(dict.fromkeys(session_ids))
        if mode not in ("any", "all"):
            raise TeamError("等待模式必须是 any 或 all")
        after_cursors = {} if after_cursors is None else after_cursors
        if (not isinstance(after_cursors, dict) or set(after_cursors) - set(ids)
                or any(not isinstance(cursor, str) or not re.fullmatch(r"v1:[0-9a-f]{32}", cursor) for cursor in after_cursors.values())):
            raise TeamError("after_cursors 必须是等待目标对应的上次游标")
        for sid in ids:
            self._authorize_target(caller, self._member(sid, user_id, live=False))
        capacity = self._capacity(caller["project_id"])
        suspended = False
        timed_out = False
        activation = caller["activation_id"]
        runtime = getattr(self.manager, "_runtimes", {}).get(caller_session_id)
        set_phase = getattr(runtime, "_set_phase", None) if progress and getattr(runtime, "busy", False) else None
        previous_phase = str(getattr(runtime, "phase", "") or "")
        if set_phase:
            set_phase("waiting_children")
        try:
            async with capacity.condition:
                if caller_session_id in capacity.active:
                    capacity.active.remove(caller_session_id)
                    self._release_files(caller)
                    suspended = True
                    self.db.execute("UPDATE project_agents SET state='waiting',updated_at=? WHERE session_id=? AND activation_id=?",
                                    (_now(), caller_session_id, activation))
                    self.db.execute("UPDATE project_agent_runs SET state='waiting',updated_at=? WHERE session_id=? AND activation_id=?",
                                    (_now(), caller_session_id, activation))
                    self._changed(caller_session_id)
                    capacity.condition.notify_all()
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    descriptors = [self.descriptor(sid) for sid in ids]
                    cursors = {item["session_id"]: self._wait_cursor(item) for item in descriptors}
                    ready = [item["session_id"] for item in descriptors
                             if (item["state"] not in BUSY_STATES or (mode == "any" and (item.get("pending_approval") or item.get("pending_questions"))))
                             and cursors[item["session_id"]] != after_cursors.get(item["session_id"])]
                    all_settled = all(item["state"] not in BUSY_STATES for item in descriptors)
                    if ready and (mode == "any" or all_settled):
                        break
                    left = deadline - asyncio.get_running_loop().time()
                    if left <= 0:
                        timed_out = True
                        break
                    try:
                        # Phase/approval changes can originate on a different
                        # loop; periodically re-read while durable state wakes
                        # use the cooperative condition for prompt delivery.
                        await asyncio.wait_for(capacity.condition.wait(), min(left, 0.5))
                    except asyncio.TimeoutError:
                        continue
        finally:
            current = self._row(caller_session_id)
            task = asyncio.current_task()
            if suspended and not task.cancelling():
                if not current or current["activation_id"] != activation or current["state"] != "waiting":
                    raise asyncio.CancelledError
                if set_phase:
                    set_phase("queued")
                await self._readmit(runtime, current, activation)
            if set_phase and not task.cancelling():
                set_phase(previous_phase)
        descriptors = [self.descriptor(sid) for sid in ids]
        cursors = {item["session_id"]: self._wait_cursor(item) for item in descriptors}
        changed = [sid for sid in ids if cursors[sid] != after_cursors.get(sid)]
        ready = [item["session_id"] for item in descriptors
                 if (item["state"] not in BUSY_STATES or (mode == "any" and (item.get("pending_approval") or item.get("pending_questions")))) and item["session_id"] in changed]
        for item in descriptors:
            if item["session_id"] not in changed:
                item.update(result="", error="", unchanged=True)
        return {"agents": descriptors, "timed_out": timed_out, "mode": mode, "cursors": cursors,
                "changed_session_ids": changed, "ready_session_ids": ready}

    async def suspend_for_approval(self, runtime: Any) -> bool:
        """Release a waiting descendant's slot, without granting permission."""
        row = self._member(runtime.session_id, runtime.user_id)
        if row["role"] == "lead":
            return False
        key = (row["session_id"], row["activation_id"])
        capacity = self._capacity(row["project_id"])
        async with capacity.condition:
            if key in self._approval_suspensions:
                self._approval_suspensions[key] += 1
                return True
            if row["session_id"] not in capacity.active:
                return False
            capacity.active.remove(row["session_id"])
            self._release_files(row)
            self._approval_suspensions[key] = 1
            self.db.execute("UPDATE project_agents SET state='waiting',updated_at=? WHERE session_id=? AND activation_id=?",
                            (_now(), *key))
            self.db.execute("UPDATE project_agent_runs SET state='waiting',updated_at=? WHERE session_id=? AND activation_id=?",
                            (_now(), *key))
            self._changed(row["session_id"])
            capacity.condition.notify_all()
        return True

    async def resume_after_approval(self, runtime: Any, suspended: bool) -> None:
        if not suspended:
            return
        row = self._row(runtime.session_id)
        if not row:
            raise asyncio.CancelledError
        key = (row["session_id"], row["activation_id"])
        count = self._approval_suspensions.get(key, 0)
        if count > 1:
            self._approval_suspensions[key] = count - 1
            return
        self._approval_suspensions.pop(key, None)
        task = asyncio.current_task()
        if task and task.cancelling():
            return
        if row["state"] != "waiting":
            raise asyncio.CancelledError
        await self._readmit(runtime, row, row["activation_id"])

    def _cancel_members(self, ids: list[str], *, retired: bool = False) -> None:
        fresh = set(ids) - self._cancelling
        if not fresh:
            return
        self._cancelling.update(fresh)
        current_task = None
        with contextlib.suppress(RuntimeError):
            current_task = asyncio.current_task()
        try:
            for sid in fresh:
                row = self._row(sid)
                if not row:
                    continue
                state = "retired" if retired else "cancelled"
                if retired or row["state"] in BUSY_STATES:
                    self.db.execute("UPDATE project_agents SET state=?,error=?,updated_at=? WHERE session_id=?",
                                    (state, "工作流节点已删除" if retired else "执行已停止", _now(), sid))
                    if row["activation_id"]:
                        self.db.execute("UPDATE project_agent_runs SET state='cancelled',error=?,updated_at=? WHERE session_id=? AND activation_id=? AND state IN ('queued','running','waiting')",
                                        ("执行已停止", _now(), sid, row["activation_id"]))
                    self._changed(sid)
                for task in (self._starts.get(sid), self._executions.get(sid)):
                    if task and not task.done() and task is not current_task:
                        self._cancel_task(task)
                runtime = getattr(self.manager, "_runtimes", {}).get(sid)
                if runtime:
                    task = getattr(runtime, "_task", None)
                    owner_loop = task.get_loop() if task is not None else None
                    running_loop = None
                    with contextlib.suppress(RuntimeError):
                        running_loop = asyncio.get_running_loop()
                    if owner_loop is not None and owner_loop.is_running() and owner_loop is not running_loop:
                        owner_loop.call_soon_threadsafe(self._cancel_runtime, runtime, sid)
                    else:
                        self._cancel_runtime(runtime, sid)
                with contextlib.suppress(RuntimeError):
                    asyncio.get_running_loop().create_task(self._notify_changed(row["project_id"]))
        finally:
            self._cancelling.difference_update(fresh)

    @staticmethod
    def _cancel_task(task: asyncio.Task) -> None:
        owner_loop = task.get_loop()
        running_loop = None
        with contextlib.suppress(RuntimeError):
            running_loop = asyncio.get_running_loop()
        if owner_loop.is_running() and owner_loop is not running_loop:
            owner_loop.call_soon_threadsafe(task.cancel)
        elif not owner_loop.is_closed():
            task.cancel()

    def _cancel_runtime(self, runtime: Any, session_id: str) -> None:
        # Runtime.cancel calls cancel_for again. A marshalled callback runs after
        # the original batch guard may have ended, so guard this callback too.
        self._cancelling.add(session_id)
        try:
            runtime.cancel()
        finally:
            self._cancelling.discard(session_id)

    def cancel_for(self, session_id: str) -> None:
        """Cancel descendants only; safe when invoked by runtime.cancel itself."""
        if session_id in self._cancelling:
            return
        self._cancel_members(self._subtree(session_id))

    def cancel_project(self, project_id: str) -> list[str]:
        """Stop all owned runtimes before a validated project deletion.

        This lifecycle hook deliberately includes the Lead and every workflow
        tree. Database cascade alone cannot stop tasks which already hold their
        runtime and workspace in memory. Caller performs project ownership and
        deletion validation before using this trusted hook.
        """
        ids = [row["session_id"] for row in self.db.query_all(
            "SELECT session_id FROM project_agents WHERE project_id=?", (project_id,)
        )]
        self._cancel_members(ids)
        return ids

    async def stop(self, caller_session_id: str, user_id: str, target_session_id: str, *, allow_self: bool = False) -> dict:
        caller = self._member(caller_session_id, user_id)
        target = self._member(target_session_id, user_id, live=False)
        self._authorize_target(caller, target, allow_self=allow_self)
        ids = self._subtree(target_session_id, include_self=True)
        self._cancel_members(ids)
        await self._notify_changed(caller["project_id"])
        await asyncio.sleep(0)
        return {"stopped": ids, "agents": [self.descriptor(sid) for sid in ids]}

    async def interrupt(self, caller_session_id: str, user_id: str, target_session_id: str, *, allow_self: bool = False) -> dict:
        """Interrupt this turn only; descendants and transcript remain usable."""
        caller = self._member(caller_session_id, user_id)
        target = self._member(target_session_id, user_id)
        self._authorize_target(caller, target, allow_self=allow_self)
        key = (target_session_id, target["activation_id"])
        interrupted = target["state"] in BUSY_STATES
        runtime = getattr(self.manager, "_runtimes", {}).get(target_session_id)
        if interrupted:
            self._interrupting.add(key)
            callback = getattr(runtime, "interrupt_current_turn", None)
            if callback:
                callback()
            # A reservation may not have reached runtime.submit yet. Cancel
            # only this agent's task, never the recursive cancellation helper.
            tasks = {task for task in (self._starts.get(target_session_id), self._executions.get(target_session_id))
                     if task and not task.done()}
            for task in tasks:
                if task and not task.done():
                    self._cancel_task(task)
            self._finish(target_session_id, target["activation_id"], "cancelled", error="当前轮次已中断，可继续派工")
            await self._notify_changed(target["project_id"])
            # Allow cooperative cancellation/finalizers to settle before an
            # immediate follow-up. A tool that ignores cancellation must not
            # hang this control operation indefinitely or overlap a new run.
            if tasks:
                await asyncio.wait(tasks, timeout=1)
            await asyncio.sleep(0)
            if target_session_id not in self._executions and target_session_id not in self._starts:
                self._interrupting.discard(key)
        return {"interrupted": interrupted, "session_id": target_session_id, "agent": self.descriptor(target_session_id),
                "descendants_preserved": True,
                "ready_for_followup": target_session_id not in self._starts and target_session_id not in self._executions
                                      and not getattr(runtime, "busy", False)}

    def read_execution(self, caller_session_id: str, user_id: str, target_session_id: str, *, activation_id: int,
                       cursor: str = "", limit: int = 20, max_chars: int = 16_000,
                       job_id: str = "", job_offset: int = 0) -> dict:
        """Read actual persisted tool evidence for one authorized descendant run.

        Never infer activation boundaries from timestamps or a model's report.
        Old transcripts lacking runtime-written boundaries are explicitly
        unavailable. Pagination can split a large JSON payload without losing
        its original characters; it never merges another activation's events.
        """
        caller = self._member(caller_session_id, user_id)
        target = self._member(target_session_id, user_id, live=False)
        self._authorize_target(caller, target)
        if caller["role"] != "lead" and caller["tree_id"] != target["tree_id"]:
            raise TeamError("子智能体只能读取自己工作树中后代的执行证据")
        for value, low, high, name in ((activation_id, 1, 2**63 - 1, "activation_id"),
                                       (limit, 1, 50, "limit"), (max_chars, 1000, 32_000, "max_chars"),
                                       (job_offset, 0, 2**63 - 1, "job_offset")):
            if type(value) is not int or not low <= value <= high:
                raise TeamError(f"{name} 必须是 {low} 到 {high} 的整数")
        cursor = _string(cursor, "cursor", required=False, limit=80)
        match = re.fullmatch(r"([0-9]{1,20}):([0-9]{1,20})", cursor) if cursor else None
        if cursor and match is None:
            raise TeamError("cursor 必须使用上一页返回的 next_cursor")
        cursor_seq, cursor_offset = (map(int, match.groups()) if match else (0, 0))
        job_id = _string(job_id, "job_id", required=False, limit=64)
        run = self.db.query_one("SELECT * FROM project_agent_runs WHERE session_id=? AND activation_id=?",
                                (target_session_id, activation_id))
        if not run:
            raise TeamError("找不到指定 activation_id 的执行记录")
        response = {
            "session_id": target_session_id, "activation_id": activation_id,
            "tree_id": target["tree_id"], "state": run["state"],
            "created_at": run["created_at"], "updated_at": run["updated_at"],
            "reported_result": run["result"][:1000], "reported_result_truncated": len(run["result"]) > 1000,
            "run_error": run["error"][:1000], "next_cursor": None, "events": [],
            "source": "persisted_transcript", "available_job_ids": [],
            "evidence_notice": "reported_result 是智能体自述，不是运行证据。events 是实际落盘工具记录；历史工具输出可能已截断，未记录退出码时不能推断成功。",
        }
        transcript = getattr(self.manager, "transcript", None)
        if transcript is None:
            return response | {"boundary_status": "unavailable", "evidence_notice": "当前没有持久执行轨迹，不能核验该次执行。"}
        found, ended, remaining, page_full = False, False, max_chars, False
        background_calls: set[str] = set()
        available_jobs: list[str] = []
        requested_job_recorded = False
        for event in transcript.iter_events(target_session_id):
            marker = event.payload.get("agent_activation") if event.type.value == "session_meta" else None
            if isinstance(marker, dict):
                if marker.get("phase") == "started":
                    if found:
                        break
                    if marker.get("activation_id") == activation_id:
                        found = True
                        response["workspace"] = marker.get("workspace", "")
                        response["start_seq"] = event.seq
                    continue
                if found and marker.get("phase") == "finished" and marker.get("activation_id") == activation_id:
                    ended = True
                    response["end_seq"] = event.seq
                    break
            if not found:
                continue
            kind = event.type.value
            if kind == "tool_use":
                call = event.payload.get("call", {})
                if isinstance(call, dict) and call.get("name") == "bash" and isinstance(call.get("arguments"), dict) and call["arguments"].get("run_in_background") is True:
                    background_calls.add(str(call.get("id", "")))
            elif kind == "tool_result" and event.payload.get("call_id") in background_calls and event.payload.get("name") == "bash":
                launched = re.match(r"^started background job (job_[a-zA-Z0-9_-]+)\n", str(event.payload.get("result", "")))
                if launched:
                    requested_job_recorded = requested_job_recorded or launched[1] == job_id
                    if launched[1] in available_jobs:
                        available_jobs.remove(launched[1])
                    available_jobs.append(launched[1])
                    available_jobs = available_jobs[-50:]
            if job_id or kind not in {"tool_use", "tool_result", "done", "error"} or event.seq < cursor_seq:
                continue
            if page_full:
                if response["next_cursor"] is None:
                    response["next_cursor"] = f"{event.seq}:0"
                continue
            payload_json = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
            offset = cursor_offset if event.seq == cursor_seq else 0
            if offset >= len(payload_json):
                raise TeamError("cursor 超出当前事件长度；轨迹可能已回退，请从第一页重新读取")
            part = payload_json[offset:offset + remaining]
            complete = offset + len(part) == len(payload_json)
            item = {"seq": event.seq, "type": kind, "ts": event.ts}
            if offset == 0 and complete:
                item["payload"] = event.payload
            else:
                item.update(payload_json_fragment=part, payload_offset=offset,
                            payload_total_chars=len(payload_json), payload_complete=complete)
            response["events"].append(item)
            remaining -= len(part)
            if not complete:
                response["next_cursor"] = f"{event.seq}:{offset + len(part)}"
            page_full = not complete or remaining == 0 or len(response["events"]) >= limit
        if not found:
            return response | {"boundary_status": "missing", "evidence_notice": "该次执行没有持久 activation 起止标记（旧版本记录、尚未开始或已回退）；不能可靠归属工具记录，未混入其他执行。"}
        response["boundary_status"] = "complete" if ended else "open" if run["state"] in BUSY_STATES else "interrupted"
        response["available_job_ids"] = available_jobs
        if job_id:
            if not requested_job_recorded:
                raise TeamError("此后台任务没有该 activation_id 的真实启动记录，不能读取")
            runtime = getattr(self.manager, "_runtimes", {}).get(target_session_id)
            registry = getattr(runtime, "jobs", None)
            snapshot = registry.peek(target_session_id, job_id, offset=job_offset, limit=max_chars) if registry else None
            response["job"] = snapshot or {"id": job_id, "status": "unavailable", "reason": "任务不在当前会话内存中；可能已重启或淘汰。持久启动记录不代表当前仍运行。"}
        return response

    def audit(self, caller_session_id: str, user_id: str, target_session_id: str, *, activation_id: int,
              decision: str, reason: str) -> dict:
        caller = self._member(caller_session_id, user_id)
        if caller["role"] != "lead":
            raise TeamError("只有项目 Lead 可以做最终审计")
        target = self._member(target_session_id, user_id)
        self._authorize_target(caller, target)
        if type(activation_id) is not int or not 1 <= activation_id <= 2**63 - 1:
            raise TeamError("审计必须指定有效的执行编号")
        if decision not in ("accepted", "rejected"):
            raise TeamError("审计结果必须是 accepted 或 rejected")
        reason = _string(reason, "审计依据", limit=4000)
        descendants = self._subtree(target_session_id)
        if any((self._row(sid) or {}).get("state") in BUSY_STATES for sid in descendants):
            raise TeamError("该任务还有运行或排队的子任务，不能做最终审计")
        stamp = _now()
        cursor = self.db.execute(
            "UPDATE project_agents SET audit_decision=?,audit_reason=?,audit_reviewer=?,audit_activation_id=?,audit_updated_at=?,updated_at=? "
            "WHERE session_id=? AND activation_id=? AND state='completed'",
            (decision, reason, caller_session_id, activation_id, stamp, stamp, target_session_id, activation_id),
        )
        if cursor.rowcount != 1:
            raise TeamError("该执行尚未完成或已有新结果，不能审计旧的输出")
        self.db.execute("UPDATE project_agent_runs SET audit_decision=?,audit_reason=?,audit_reviewer=?,updated_at=? WHERE session_id=? AND activation_id=? AND state='completed'",
                        (decision, reason, caller_session_id, stamp, target_session_id, activation_id))
        self.db.execute("INSERT INTO project_agent_audits (session_id,activation_id,decision,reason,reviewer_session_id,created_at) VALUES (?,?,?,?,?,?)",
                        (target_session_id, activation_id, decision, reason, caller_session_id, stamp))
        self._changed(target_session_id)
        return self.descriptor(target_session_id)  # type: ignore[return-value]

    def effective_permission(self, session_id: str, user_id: str) -> str:
        """Resolve live inherited rights before each tool invocation."""
        return self._effective_permission(self._member(session_id, user_id))

    def planning_authority(self, session_id: str, user_id: str) -> bool:
        """Planning and its descendants cannot acquire one-off write grants."""
        row = self._member(session_id, user_id)
        seen: set[str] = set()
        while row:
            if row["session_id"] in seen:
                raise TeamError("智能体父级关系异常")
            seen.add(row["session_id"])
            meta = self.manager.session_repo.get(row["session_id"])
            if meta and meta.get("kind") == "planner":
                return True
            if row["role"] == "lead":
                return False
            parent = self._row(row["parent_session_id"])
            if not parent or parent["project_id"] != row["project_id"] or parent["user_id"] != user_id:
                raise TeamError("规划权限的父级关系无效")
            row = parent
        return False

    def _effective_permission(self, row: dict, seen: set[str] | None = None) -> str:
        """Recheck ancestor grants while preserving independent task trees."""
        rank = {"read-only": 0, "workspace-write": 1, "full-access": 2}
        meta = self.manager.session_repo.get(row["session_id"])
        mode = meta.get("permission_mode") if meta else "read-only"
        mode = mode if mode in rank else "read-only"
        if meta and meta.get("kind") == "planner":
            return "read-only"
        project = self._owned_project(row["project_id"], row["user_id"])
        if row["tree_id"] == "main" and not project.get("allow_direct_write"):
            return "read-only"
        if row["role"] == "lead":
            return mode
        parent = self._row(row["parent_session_id"])
        if not parent or parent["project_id"] != row["project_id"] or parent["user_id"] != row["user_id"]:
            raise TeamError("父智能体已不存在，不能继承执行权限")
        if parent["role"] == "lead":
            if row["tree_id"] == "main":
                return self._effective_permission(parent)
            return mode
        seen = set(seen or ())
        if row["session_id"] in seen:
            raise TeamError("智能体父级关系异常")
        seen.add(row["session_id"])
        parent_mode = self._effective_permission(parent, seen)
        return mode if rank[mode] <= rank[parent_mode] else parent_mode

    def _apply_permissions(self, runtime: Any, row: dict) -> None:
        mode = self._effective_permission(row)
        changed = runtime.permission_mode != mode
        runtime.permission_mode = mode
        # Session metadata is the configured grant. Persisting an ancestor's
        # temporary read-only clamp here permanently destroyed that grant,
        # preventing recursive descendants from recovering after restoration.
        if changed:
            self._changed(row["session_id"])

    def _validate_workspace(self, runtime: Any, row: dict) -> None:
        """Persistent metadata and the cached runtime must inherit one workspace."""
        if (row["project_id"], row["tree_id"].removeprefix("task:")) in self._task_operations:
            raise TeamError("任务工作树正在审查、验证或回收，不能执行")
        meta = self.manager.session_repo.get(row["session_id"])
        own = self.manager.workspace_for(meta)
        if row["role"] == "lead":
            expected = own
        else:
            parent = self._row(row["parent_session_id"])
            if parent["role"] == "lead":
                project = self._owned_project(row["project_id"], row["user_id"])
                workspace, _ = self._tree_workspace(project, row["tree_id"], parent)
                expected = Path(workspace)
            else:
                expected = self.manager.workspace_for(self.manager.session_repo.get(parent["session_id"]))
        normalize = lambda path: os.path.normcase(os.path.abspath(str(path)))
        if not own.is_dir() or normalize(own) != normalize(expected) or normalize(runtime.workspace) != normalize(expected):
            raise TeamError("智能体工作区与父级或工作树不一致，请重新打开项目")

    def role_context(self, runtime: Any, meta: dict | None = None) -> str:
        row = self._row(runtime.session_id)
        if not row:
            return ""
        limits = self.limits()
        header = f"\n\n[当前对话的后台协作]\n项目 {row['project_id']}；主对话 {row['lead_session_id']}；身份 {row['role']}；会话 {row['session_id']}；深度 {row['depth']} / {limits['max_depth']}；项目子智能体并行上限 {limits['max_parallel']}。\n"
        header += (
            "要求用户点击审批前，必须以最新 team_list / wait_agents 返回的 pending_approval 为准，核对目标智能体的 session_id 与 approval_id。"
            "pending_approval 仅是查询时快照，生成回复期间也可能超时，不能承诺用户当前必有审批卡；以实际‘核对审批’结果为准。"
            "历史输出中的 ap_ 编号、旧的待审批描述或已超时请求不等于当前待审批。"
            "pending_approval=null 或 approval_pending=false 表示该快照没有待审批，不得声称仍卡在审批，也不能据此推断用户批准或拒绝。"
            "若摘要只有 approval_pending=true，先用 wait_agents 针对该 session_id 核对完整 pending_approval，再请求用户操作。"
            "说明准确目标及入口：当前主对话的后台子任务区会聚合该目标的审批卡；不要要求用户切换智能体分支，仍须以界面实际收到的当前请求为准。"
            "用户说看不到审批卡时，先刷新核对实时状态与目标会话；没有当前 pending_approval 就不要让用户寻找或点击旧审批。"
            "无卡反馈不代表用户取消了安全审批，更不构成批准或绕过审批的授权。\n"
        )
        if row["role"] == "lead":
            summary = self._result_context(row)
            return header + (
                "你是当前对话的协调者（Lead），既可以协调后台子任务，也可以独立执行任务，以解决问题、完成相关验证并交付结果为优先。"
                "用户始终在这个主对话与你交流，不按需求、方案、实现等阶段创建固定智能体分支；其他新建对话拥有各自的协调者和子任务。"
                "简单、边界明确、连续的任务直接查明问题、修改、运行相关验证并交付，不以 team_list、delegate_agent 或额外审计轮次为前置步骤。"
                "没有并行收益时，不为读取、修改、测试分别创建智能体，也不把本可完成的步骤交给用户。\n"
                "使用当前实际可用的工具，遵守会话当前权限、项目写入开关、工作树与文件边界；角色说明不授予额外权限，不自动提升任何保存权限。"
                "只读和规划阶段继续只读，遇到需要审批的操作由实际执行者申请，不能通过委派或切换角色绕过底层限制。\n"
                "复杂任务或存在可独立推进、责任范围不同的并行工作时再拆分；用 delegate_agent 按需创建后台子智能体，"
                "委派明确的目标、输入、负责的文件范围、产出与验证标准，不要求固定 node_id，也不把工作流阶段当作派工前置条件。"
                "派工前确认三点：能独立交付、写入责任能分开、并行节省时间超过交接与合并成本；每次填写 delegation_reason 说明收益。"
                "write_paths=[] 表示只读；具体文件是唯一写入责任，目录用末尾 /，['.'] 表示整个工作区独占。"
                "后台只读或限定文件任务不能执行任意 Shell/未知副作用工具，测试可由你在整合后统一运行，或安排全工作区执行者。"
                "同文件已有执行者时优先 followup_agent 复用其上下文，其他智能体只读协助；需要前置产出时用 depends_on 绑定准确执行并排队。"
                "文件重叠由后端排队；等待子任务或审批时暂交还文件持有权，恢复必须重新取得，写入前需要重读变过的文件。"
                "需要了解已有子任务、状态和限制时再用 team_list；它只查看当前对话的子任务，不启动模型。"
                "共享同一修改点或前后依赖的工作复用已有执行者，独立工作区的一次任务可使用界面已有的隔离单任务执行方式，避免重复交接。"
                "补充已有任务信息使用 send_agent_message（不会启动空闲会话）；继续同一智能体的任务使用 followup_agent，保留其工作区与历史。"
                "只中断目标当前轮次使用 interrupt_agent；停止目标及整棵后代树使用 stop_agent，二者不可混用。"
                "独立任务可以并行，有依赖的任务先取得可审查产出。用 wait_agents 等待，默认等待 60 秒；无独立工作可做时不要反复短等待或 team_list 轮询。"
                "独立任务可用 mode=any 在首个结果或待审批出现时继续；保存 cursors 并在下次传 after_cursors，避免重复读取同一结果。"
                "子任务按准确 session_id 与 activation_id 核对实际改动和验证收据；已有有效收据一次核验即可，复用已接受的结果，不重复派审计或机械重跑。"
                "只有修复产生新变化、执行身份变化或证据缺口时才补充相关验证，可以自己执行，也可以交给合适的现有执行者。"
                "需要读取后代真实工具记录、退出码或后台任务证据时，使用 read_agent_execution 并指定准确 session_id 与 activation_id，按 next_cursor 继续取证。"
                "验证收据应包含工作目录、实际退出码和关键输出；记录缺失或截断时如实说明，不能把节点自述、源码推断或历史其他批次成功说成本次测试通过。"
                "子任务完成后在当前对话整合结果并交付，不要求用户转到子任务对话收取产出。完成通知本身不等于测试通过；"
                "如需正式记录接受或驳回，可按精确 activation_id 调用 audit_agent，它不是普通交付的必经步骤，也不需要额外派出审计智能体。"
                "没有所需验证证据时如实说明并补充必要检查；不得把模型完成报告当成工作流节点已验收，节点进度仍遵守工作流约束。"
            ) + summary
        detail = ""
        if (meta or self.manager.session_repo.get(runtime.session_id) or {}).get("kind") == "planner":
            detail = "\n当前是只读规划阶段：只读取事实并产出计划，不修改文件、提交代码或执行会写入项目的命令。委派的后代同样继承只读权限；计划获批准后才由隔离任务智能体实施。"
        if row["node_id"]:
            document = self.workflow_store.get(row["project_id"], row["tree_id"])
            node = next((item for item in document["nodes"] if item["id"] == row["node_id"]), None)
            if node:
                detail = "\n" + wrap_untrusted(json.dumps({key: node[key] for key in ("id", "type", "title", "objective", "deliverable", "acceptance", "status")}, ensure_ascii=False), "负责的工作流节点资料")
        return header + (
            f"你负责父智能体 {row['parent_session_id']} 委派的后台任务；所属主对话是 {row['lead_session_id']}。"
            "以执行和交付为优先，只完成当前节点/任务的责任范围，遵守继承的工作树、权限和文件边界。简单、连续的步骤由自己完成并运行相关验证；"
            "复杂或可独立推进、责任范围不同的子任务才通过 delegate_agent 分给后代，不为每个步骤重复派工；"
            "派工须填写 write_paths 与 delegation_reason，后代写入范围不能超出你本轮范围；同文件工作优先复用现有执行者，依赖用 depends_on 明确。"
            "等待子任务或审批时会交还文件持有权，恢复前重新排队；只读或限定文件任务使用原生工具，Shell验证交主代理或全工作区执行者。"
            "深度与并行上限由运行时强制执行，不能通过提示词绕过。不得控制父级、同级、其他主对话或其他项目的智能体。"
            "等待后代结果使用 wait_agents；它会释放等待期间的运行名额。已有有效收据一次核验即可，修复产生新变化或证据缺口时才复验；"
            "需要补证时用 read_agent_execution 按精确 activation_id 读取后代真实执行轨迹，不把自述当成验证。"
            "向自己的后代补充信息用 send_agent_message，继续派工用 followup_agent；只中断当前轮次用 interrupt_agent，停止整棵树用 stop_agent。"
            "最终向父级提供改动、工作目录、实际退出码和关键验证输出、实际产出及未解决问题，由所属主对话汇总交付；不要求用户切换到本智能体聊天。"
            "不得自行宣称 Lead 已接受结果，也不能擅自标记节点验收完成。" + detail + self._result_context(row)
        ) + self._scope_context(row)

    def _result_context(self, caller: dict) -> str:
        """Durable summaries survive a lost notification queue/server restart."""
        descendants = set(self._subtree(caller["session_id"]))
        recent = self.db.query_all(
            "SELECT session_id,parent_session_id,node_id,title,state,activation_id,result,error,audit_decision,audit_reason "
            "FROM project_agents WHERE project_id=? AND lead_session_id=? AND state IN ('completed','failed','cancelled') AND activation_id>0 ORDER BY updated_at DESC LIMIT 48",
            (caller["project_id"], caller["lead_session_id"]),
        )
        items = []
        remaining = 10_000
        for row in recent:
            if row["session_id"] not in descendants:
                continue
            # Reviewed reports remain durable, but no longer repeat their body
            # in every new activation's system context. Exact run identities
            # remain available for evidence lookup and stale-run audit checks.
            accepted = row["audit_decision"] == "accepted"
            limit = 0 if accepted else 800
            row = {**row, "result": row["result"][:limit], "result_truncated": len(row["result"]) > limit,
                   "error": row["error"][:400], "audit_reason": row["audit_reason"][:300],
                   "execution_ref": {"session_id": row["session_id"], "activation_id": row["activation_id"]}}
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > remaining:
                break
            items.append(encoded)
            remaining -= len(encoded)
            if len(items) >= 12:
                break
        return "\n" + wrap_untrusted("\n".join(items), "最近子任务记录；摘要不是验证，read_agent_execution 按 execution_ref 取证") if items else ""

    async def aclose(self) -> None:
        self._closed = True
        ids = list(set(self._starts) | set(self._executions))
        tasks = set(self._starts.values()) | set(self._executions.values())
        self._cancel_members(ids)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

