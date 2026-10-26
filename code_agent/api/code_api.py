"""代码模式 REST 接口（M18 · P0）。

挂在 create_app 里（依赖 db / SessionManager / ProjectService 已经就位）。
分成两个函数只是为了让 app.py 的 diff 足够小：`mount_code_api` 负责路由，
`_err` 负责把领域异常翻成 HTTP 语义。

错误语义（前端按这个写提示，不要靠猜）：
- 404 项目/会话不存在或不属于当前用户（不区分"不存在"与"不是你的"）
- 422 入参/路径不合法（越界、不存在、目录不是目录、文件是二进制…）
- 409 目录不可用（项目被外部移动/删除）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.agent.project_team import TeamError
from code_agent.codeintel.architecture import ArchitectureError
from code_agent.codeintel.decisions import KINDS as DECISION_KINDS
from code_agent.codeintel.decisions import DecisionError
from code_agent.codeintel.interpreter import interpreter_info as lsp_environment
from code_agent.codeintel.file_search import files_catalog, search_text
from code_agent.codeintel.logic import LogicError
from code_agent.codeintel.parallel import BatchError
from code_agent.codeintel.paths import ProjectPathError, safe_child
from code_agent.codeintel.projects import TEMPLATES, ProjectService
from code_agent.codeintel.service import FileSaveConflict
from code_agent.codeintel.tasks import TaskError
from code_agent.codeintel.vcs import check_command, probe_repo, run_git
from code_agent.codeintel.work_items import agent_item, project_work_items, task_item, tree_agents
from code_agent.sessions.manager import SessionClosed
from code_agent.extensions.jsonrpc_stdio import JsonRpcError


class ProjectRegistration(BaseModel):
    """所有打开入口共用的登记参数；不把 JSON 对象或布尔字符串转成路径/开关。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["open", "create"] = "open"
    path: str = Field(default="", strict=True, max_length=4096)
    location: str = Field(default="", strict=True, max_length=4096)
    name: str = Field(default="", strict=True, max_length=80)
    template: Literal["empty", "python", "node"] = "empty"
    git_init: StrictBool = True

    @field_validator("mode", "template", mode="before")
    @classmethod
    def normalize_choice(cls, value: Any, info: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower() or ("open" if info.field_name == "mode" else "empty")
        return value

    @field_validator("path", "location", "name")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("路径或项目名不能包含控制字符")
        return value


class ProjectConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", strict=True, max_length=100)
    from_session_id: str = Field(default="", strict=True, max_length=256)

    @field_validator("title", "from_session_id")
    @classmethod
    def clean_text(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("对话标题或来源不能包含控制字符")
        return value.strip()


class EditorFileSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(strict=True, min_length=1, max_length=4096)
    content: str = Field(strict=True, max_length=2 * 1024 * 1024)
    expected_sha1: str = Field(default="", strict=True, pattern=r"^(?:[0-9a-f]{40})?$")
    session_id: str = Field(default="", strict=True, max_length=256)

    @field_validator("path", "session_id")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("路径或会话 ID 不能包含控制字符")
        return value


class WorkItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(strict=True, min_length=1, max_length=4000)
    title: str = Field(default="", strict=True, max_length=120)
    execution_mode: Literal["team", "isolated", "parallel"] = "team"
    tree: str = Field(default="main", strict=True, pattern=r"^(?:main|task:[A-Za-z0-9_-]{1,96})$")
    plan_first: StrictBool = True
    git_init: StrictBool = False
    plan: dict[str, Any] | None = None
    base_branch: str = Field(default="", strict=True, max_length=256)

    @field_validator("requirement")
    @classmethod
    def nonempty_requirement(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请描述任务需求")
        return value.strip()

    @model_validator(mode="after")
    def matching_manual_plan(self) -> WorkItemCreate:
        if self.plan is not None:
            if self.execution_mode == "team":
                raise ValueError("团队任务请直接描述需求，手动计划用于隔离执行")
            key = "subtasks" if self.execution_mode == "isolated" else "lanes"
            rows = self.plan.get(key)
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"手动计划的 {key} 必须是对象列表")
            if key == "lanes" and not rows:
                raise ValueError("并行计划至少需要一个子任务")
        return self


def task_view(row: dict) -> dict:
    """任务视图（列表与详情共用）：不把整份 plan/review JSON 塞回给前端列表。"""
    try:
        plan = json.loads(row.get("plan_json") or "{}")
    except json.JSONDecodeError:
        plan = {}
    try:
        review = json.loads(row.get("review_json") or "{}")
    except json.JSONDecodeError:
        review = {}
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "title": row.get("title") or "",
        "requirement": row.get("requirement") or "",
        "status": row.get("status") or "",
        "base_branch": row.get("base_branch") or "",
        "branch": row.get("branch") or "",
        "worktree": row.get("worktree") or "",
        "session_id": row.get("session_id") or "",
        "error": row.get("error") or "",
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
        "merged_at": row.get("merged_at") or "",
        "plan_goal": plan.get("goal") or "",
        "plan_files": plan.get("files") or [],
        "plan_subtasks": len(plan.get("subtasks") or []),
        "changed_files": len(review.get("changed_files") or []),
        "symbol_counts": (review.get("symbols") or {}).get("counts") or {},
        "plan_scope_ok": review.get("plan_scope_ok", None),
        "verification": review.get("verification"),
    }


async def git_overview(project: dict) -> dict:
    """git 面板的总览：分支/远端/ahead-behind/改动清单。"""
    root = Path(project["root_path"])
    info = await probe_repo(root, with_status=True)
    overview: dict[str, Any] = {
        "available": info.git_available,
        "is_repo": info.is_repo,
        "branch": info.branch,
        "remote": info.remote,
        "head": info.head,
        "dirty": info.dirty,
        "error": info.error,
        "changes": [],
        "ahead": 0,
        "behind": 0,
    }
    if not info.is_repo:
        return overview
    code, out = await run_git(root, ["status", "--porcelain", "--untracked-files=all"])
    if code == 0:
        for line in out.splitlines():
            if len(line) < 3:
                continue
            path = line[2:].strip()
            if not path:
                continue
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            overview["changes"].append({"status": line[:2].strip(), "path": path.replace("\\", "/")})
    code, out = await run_git(root, ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
    if code == 0 and out:
        parts = out.split()
        if len(parts) == 2:
            overview["behind"], overview["ahead"] = int(parts[0] or 0), int(parts[1] or 0)
    return overview

def _err(exc: Exception) -> HTTPException:
    """领域异常 → HTTP 状态码。集中一处，避免每个接口各写一遍 try/except 文案。"""
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'\""))
    if isinstance(exc, ProjectPathError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, SessionClosed):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, TeamError):
        return HTTPException(status_code=409, detail=str(exc))
    # 任务/批次的领域错误都是"用户做了一件当前状态不允许的事"：
    # 必须给 4xx，否则前端拿到 500 只会显示"服务器内部错误"，
    # 真正有用的原因（例如"任务当前状态（merged）不可合并"）就被埋了。
    if isinstance(exc, (TaskError, BatchError, ArchitectureError, DecisionError, LogicError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


def mount_code_api(
    app: FastAPI,
    *,
    project_repo: Any,
    manager: Any,
    service: ProjectService,
    intel: Any = None,
    lsp: Any = None,
    tasks: Any = None,
    task_service: Any = None,
    batches: Any = None,
    batch_service: Any = None,
) -> None:
    """注册 /api/v1/code/* 路由。

    intel  = CodeIntelService（P1）
    lsp    = LspManager（P3）
    tasks/task_service = TaskRepo / TaskService（P4，隔离工作树与审批合并）
    batches/batch_service = BatchRepo / BatchService（P5，并行车道与串行合并队列）
    """

    def _owned(user: CurrentUser, project_id: str) -> dict:
        return service.get_owned(user.user_id, project_id)

    from code_agent.api.frontend_design_api import mount_frontend_design_api
    mount_frontend_design_api(app, service=service, intel=intel)

    def _tree_owned(user: CurrentUser, project_id: str, tree: str = "main") -> dict:
        project = _owned(user, project_id)
        if tree == "main":
            return project
        if not tree.startswith("task:") or tasks is None:
            raise KeyError("工作树不存在或无权访问")
        row = tasks.get(tree[len("task:"):])
        if row is None or row.get("project_id") != project_id or row.get("user_id") != user.user_id:
            raise KeyError("工作树不存在或无权访问")
        from code_agent.codeintel.tasks import worktree_of

        workspace = worktree_of(row)
        if workspace is None:
            raise KeyError("工作树目录已不可用")
        return {**project, "root_path": str(workspace)}

    def _intel():
        if intel is None:
            raise HTTPException(status_code=503, detail="代码情报服务未启用")
        return intel

    def _kinds(raw: str | None) -> list[str] | None:
        if not raw:
            return None
        items = [k.strip() for k in raw.split(",") if k.strip()]
        return items or None

    # ------------------------------------------------------------------
    # 环境能力（前端据此决定按钮是否可用，而不是点了才报错）
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/meta")
    def code_meta(request: Request, user: CurrentUser = Depends(get_current_user)) -> dict:
        from code_agent.codeintel.vcs import git_available
        from code_agent.api.native_dialog_api import picker_capabilities

        return {
            "git_available": git_available(),
            "browse_enabled": os.environ.get("CODE_AGENT_CODE_BROWSE", "1") != "0",
            "native_picker": picker_capabilities(request),
            "projects_root": str(service.roots()),
            "templates": list(TEMPLATES),
            "features": {
                # 能力位：如实上报，前端不做假按钮。
                # 注意这里报的是**服务是否真的接上了**，不是"阶段编号"——
                # 全 false 会让前端把已经能用的入口藏起来（比假按钮更糟）。
                "map": intel is not None,
                "lsp": lsp is not None,
                "git_write": git_available(),
                "tasks": task_service is not None,
                "parallel": batch_service is not None,
                # 功能视图（M33）：和地图同源，索引在就可用
                "feature_map": intel is not None,
            },
        }

    # ------------------------------------------------------------------
    # 项目
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/projects")
    async def list_projects(user: CurrentUser = Depends(get_current_user)) -> list[dict]:
        return await service.list_projects(user.user_id)

    @app.post("/api/v1/code/projects")
    async def create_or_open_project(
        body: ProjectRegistration, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """打开既有目录，或在 location 父目录（默认平台工程目录）下新建。"""
        try:
            if body.mode == "open":
                return await service.open_project(user.user_id, body.path, body.name)
            return await service.create_project(
                user.user_id, body.name, body.template, git_init=body.git_init, location=body.location
            )
        except HTTPException:
            raise
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="项目登记存储暂时不可用，请重试") from exc
        except OSError as exc:
            raise HTTPException(status_code=422, detail=f"项目目录无法访问或创建：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 —— 统一翻译成 HTTP 语义
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}")
    async def get_project(project_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            row = _owned(user, project_id)
            return await service.view(row)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.delete("/api/v1/code/projects/{project_id}")
    def delete_project(
        project_id: str,
        delete_files: bool = False,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """注销项目。默认只删登记记录——用户自己的仓库文件永远不动。"""
        try:
            _owned(user, project_id)
            team = getattr(manager, "agent_team", None)
            if team is not None:
                team.cancel_project(project_id)
            return service.delete_project(user.user_id, project_id, delete_files=delete_files)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    # ------------------------------------------------------------------
    # 项目 ↔ 会话
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/projects/{project_id}/conversations")
    def project_conversations(project_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            return manager.list_project_conversations(user.user_id, project_id)
        except Exception as exc:
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/conversations")
    def create_project_conversation(project_id: str, body: ProjectConversationCreate | None = None,
                                    user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            request = body or ProjectConversationCreate()
            return manager.create_project_conversation(user.user_id, project_id, title=request.title, from_session_id=request.from_session_id)
        except Exception as exc:
            raise _err(exc) from exc

    @app.delete("/api/v1/code/projects/{project_id}/conversations/{session_id}")
    async def delete_project_conversation(project_id: str, session_id: str,
                                          user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            return manager.delete_project_conversation(user.user_id, project_id, session_id)
        except Exception as exc:
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/session")
    def project_session(
        project_id: str, body: dict[str, Any] | None = None, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """取（或建）项目主会话。默认只读——写代码需要显式打开项目开关（P4 前为直改）。"""
        body = body or {}
        if "loop_mode" in body:
            raise HTTPException(status_code=422, detail="循环模式设置已移除，请更新客户端")
        requested = str(body.get("permission_mode") or "read-only")
        try:
            project = _owned(user, project_id)
            if requested == "workspace-write" and not bool(project.get("allow_direct_write")):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "该项目未开启「允许直接改主干」：请先在代码页顶栏显式打开"
                        "（P4 起会改为默认在隔离工作树里改，不需要这个开关）"
                    ),
                )
            meta = manager.create_project_session(
                user.user_id,
                project_id,
                permission_mode=requested,
            )
            # Opening an existing default conversation must retain its grant;
            # only an explicit permission change reconfigures that conversation.
            if "permission_mode" in body and meta.get("permission_mode") != requested:
                try:
                    meta = manager.set_permission_mode(meta["id"], user.user_id, requested)
                except Exception:
                    pass
            return meta
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    # ------------------------------------------------------------------
    # 浏览：目录树 / 文件文本 / 目录选择器
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/projects/{project_id}/tree")
    def project_tree(
        project_id: str, path: str = "", user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        try:
            return service.tree(_owned(user, project_id), path)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/file")
    def project_file(
        project_id: str,
        path: str,
        start: int | None = None,
        end: int | None = None,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        try:
            return service.read_file(_owned(user, project_id), path, start, end)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/files")
    def project_files_catalog(
        project_id: str,
        query: str = Query("", max_length=512),
        limit: int = Query(500, ge=1, le=2000),
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        try:
            project = _owned(user, project_id)
            return files_catalog(service.root_of(project), query=query, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/search")
    def project_text_search(
        project_id: str,
        query: str = Query(..., min_length=1, max_length=512),
        case_sensitive: bool = False,
        regex: bool = False,
        include: str = Query("", max_length=512),
        limit: int = Query(200, ge=1, le=1000),
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        try:
            project = _owned(user, project_id)
            return search_text(
                service.root_of(project), query=query, case_sensitive=case_sensitive,
                regex=regex, include=include, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.put("/api/v1/code/projects/{project_id}/file")
    async def save_project_file(
        project_id: str, body: EditorFileSave, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """编辑器保存（M18 · P2）：**带乐观锁**。

        `expected_sha1` 是打开文件时拿到的哈希；不匹配说明期间有人（AI 或别的工具）
        改过这个文件——此时返回 409 让用户决定覆盖还是先看差异，绝不静默覆盖。
        保存本身进入 patch 台账；索引在后台合并刷新，失败不会误报文件保存失败。
        """
        try:
            project = _owned(user, project_id)
            handle = _intel()
            return await handle.save_file(project, body.path, body.content, body.expected_sha1, body.session_id)
        except FileSaveConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/fs/list")
    def browse_dirs(
        path: str = "",
        hidden: int = 0,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """目录选择器：只列目录（带盘符/快捷位置；默认隐藏系统与隐藏目录）。"""
        try:
            return service.browse(path, user_id=user.user_id, show_hidden=bool(hidden))
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    # ------------------------------------------------------------------
    # 索引与地图（M18 · P1）
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/projects/{project_id}/index")
    def index_status(
        project_id: str,
        tree: str = "main",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """索引状态：version / change_seq / 计数 / 是否正在扫描。前端据此决定是否刷新。"""
        try:
            handle = _intel()
            project = _tree_owned(user, project_id, tree)
            # 标记"有人在看"：后台 watcher 只盯最近被查看的项目
            handle.touch(project_id)
            body = handle.status(project, tree, cached_if_busy=True)
            # 补描述的进度搭同一个轮询：地图本来就在轮询这里，不必再开一条通道
            if not body.get("scanning"):
                handle.maybe_autofill_briefs(project, tree_id=tree)
            body["briefs"] = handle.brief_fill_status(project, tree, cached_if_busy=True)
            return body
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/scan")
    async def scan_project(
        project_id: str,
        full: bool = False,
        tree: str = "main",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """扫描/重建索引。同步返回结果（文件 IO 在线程池里跑）；已在扫描返回 status=busy。

        单飞是刻意的：并发扫描会让同一棵索引的 version/changes 序列错乱。
        """
        try:
            project = _tree_owned(user, project_id, tree)
            handle = _intel()
            handle.touch(project_id)
            result = await handle.scan(project, full=full, tree_id=tree)
            return result
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/map")
    async def project_map(
        project_id: str,
        tree: str = "main",
        depth: int = 3,
        module: str = "",
        kinds: str = "",
        edges: int = 1,
        ensure: int = 0,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """思维导图数据。`ensure=1` 时先保证索引新鲜（首次打开项目用）。"""
        try:
            project = _owned(user, project_id)
            handle = _intel()
            handle.touch(project_id)
            if ensure:
                await handle.ensure_indexed(project, tree)
            return handle.map_payload(
                project,
                tree_id=tree,
                depth=max(1, min(int(depth or 3), 4)),
                module=module or None,
                kinds=_kinds(kinds),
                with_edges=bool(edges),
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/map/delta")
    def project_map_delta(
        project_id: str,
        after: int = 0,
        tree: str = "main",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """自 after 起的增量（地图实时更新）：新增/更新/删除的节点 + 是否需重拉边。"""
        try:
            handle = _intel()
            project = _owned(user, project_id)
            handle.touch(project_id)
            return handle.delta(project, int(after or 0), tree_id=tree)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/symbol")
    def symbol_detail(
        project_id: str,
        id: str,
        tree: str = "main",
        with_source: int = 0,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """单个符号详情（含成员、反向依赖、可选源码片段）。"""
        try:
            project = _owned(user, project_id)
            handle = _intel()
            detail = handle.symbol_detail(project, id, tree_id=tree)
            if with_source:
                start = detail["line"] or 1
                end = detail["end_line"] or start
                detail["source"] = handle.text_slice(project, detail["path"], start, min(end, start + 400))
            return detail
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/reindex")
    def reindex_project(
        project_id: str,
        tree: str = "main",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """保真度校验：重解析磁盘并与索引比对（不写入）。这是"地图完全映射代码"的度量。"""
        try:
            return _intel().verify(_owned(user, project_id), tree_id=tree)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/digest")
    def project_digest(
        project_id: str,
        tree: str = "main",
        max_chars: int = 6000,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """给模型看的紧凑地图文本（P2 起注入 system prompt；也可供人直接查看）。"""
        try:
            project = _owned(user, project_id)
            text = _intel().digest(project, tree_id=tree, max_chars=max(500, min(int(max_chars), 20000)))
            return {"text": text, "chars": len(text)}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    # ------------------------------------------------------------------
    # LSP（M18 · P3）：语言服务器目录、状态、JSON-RPC 代理与诊断
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/lsp")
    async def lsp_status(
        project_id: str = "", user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """LSP 概览：目录/安装情况 + 运行中的会话 + 最近失败原因。

        传入 project_id 时按该项目的真实语言给"该装哪些"的建议（这才是用户想看的）；
        不带时只报全局安装状态——脱离项目谈"探测"没有意义。
        """
        if lsp is None:
            raise HTTPException(status_code=503, detail="LSP 代理未启用")
        if project_id:
            project = _owned(user, project_id)
            servers = await lsp.servers_for_project(project)
        else:
            servers = await asyncio.to_thread(lsp.servers)
        return {"servers": servers, **lsp.status()}

    @app.get("/api/v1/code/projects/{project_id}/lsp")
    async def project_lsp(
        project_id: str,
        tree: str = "main",
        refresh: int = 0,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """某项目的语言服务器探测结果（按项目里真实存在的语言推荐）。

        `refresh=1` 会清掉命令解析缓存再探测——用户刚装完服务器时不必重启后端。
        """
        if lsp is None:
            raise HTTPException(status_code=503, detail="LSP 代理未启用")
        try:
            project = _owned(user, project_id)
            if refresh:
                from code_agent.codeintel import lsp_catalog

                lsp_catalog.clear_resolve_cache()
            servers = await lsp.servers_for_project(project)
            status = lsp.status()
            running = [
                s for s in status["sessions"] if s["key"].startswith(f"{project_id}:{tree}:")
            ]
            return {
                "servers": servers,
                "sessions": running,
                "starting": [s for s in status.get("starting", []) if s["key"].startswith(f"{project_id}:{tree}:")],
                "max_sessions": status["max_sessions"],
                "failures": status["failures"],
                "refreshed": bool(refresh),
                "environment": await asyncio.to_thread(lsp_environment, project["root_path"]),
            }
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/diagnostics")
    def project_diagnostics(
        project_id: str,
        tree: str = "main",
        path: str = "",
        severity: int | None = Query(None, ge=1, le=4),
        limit: int = 200,
        details: bool = False,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """诊断概览；details=1 返回项目问题列表，line/column 为 1-based。

        地图只画角标（计数），消息体走这里——两者分开是为了让地图载荷保持小。
        """
        try:
            return _intel().diagnostics(
                _owned(user, project_id), tree_id=tree, path=path or None,
                severity=severity, limit=max(1, min(int(limit), 1000)), details=details,
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/lsp/diagnostics")
    async def collect_diagnostics(
        project_id: str,
        body: dict[str, Any] | None = None,
        tree: str = "main",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """按项目批量拉诊断：把若干文件 didOpen 给语言服务器，换取全项目问题清单。

        编辑器只在打开文件时才连 LSP，于是"没打开过的文件有错"在地图上看不到。
        这个接口是"地图显示哪块编译不过"的入口（前端打开项目时调一次）。
        诊断是异步推回的：这里返回"已交给谁"，前端照常轮询 /diagnostics。
        """
        body = body or {}
        if lsp is None:
            raise HTTPException(status_code=503, detail="LSP 代理未启用")
        try:
            project = _tree_owned(user, project_id, tree)
            handle = _intel()
            handle.touch(project_id)
            result = await lsp.open_workspace(
                project,
                lang=str(body.get("lang") or "") or None,
                limit=max(1, min(int(body.get("limit") or 40), 200)),
                tree_id=tree,
            )
            documentation = lsp.start_documentation_prewarm(project, tree_id=tree)
            return {**result, "counts": handle.diagnostics(project, tree_id=tree)["counts"],
                    "documentation": documentation}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/lsp/documentation-status")
    def documentation_status(project_id: str, tree: str = "main", user: CurrentUser = Depends(get_current_user)) -> dict:
        if lsp is None:
            raise HTTPException(503, "LSP 代理未启用")
        try:
            _tree_owned(user, project_id, tree)
            return lsp.documentation_status(project_id, tree)
        except Exception as exc:
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/lsp/documentation-prewarm")
    async def documentation_prewarm(project_id: str, tree: str = "main", user: CurrentUser = Depends(get_current_user)) -> dict:
        if lsp is None:
            raise HTTPException(503, "LSP 代理未启用")
        try:
            project = _tree_owned(user, project_id, tree)
            return lsp.start_documentation_prewarm(project, tree_id=tree)
        except Exception as exc:
            raise _err(exc) from exc

    @app.websocket("/api/v1/code/projects/{project_id}/lsp/stream")
    async def lsp_stream(websocket: WebSocket, project_id: str) -> None:
        """浏览器 ↔ 语言服务器的 JSON-RPC 通道。

        浏览器发标准 LSP 消息（didOpen/完成/悬停…），服务端转发；服务端把语言
        服务器的通知（主要是 publishDiagnostics）原样回推。文档状态与诊断都在
        LspManager 里维护，这里只做鉴权、路由与错误包装。
        """
        from code_agent.api.deps import get_ws_user

        user = get_ws_user(websocket)
        if user is None:
            await websocket.close(code=4401, reason="unauthorized")
            return
        if lsp is None:
            await websocket.close(code=4503, reason="lsp disabled")
            return
        tree = (websocket.query_params.get("tree") or "main").strip() or "main"
        try:
            project = _tree_owned(user, project_id, tree)
        except KeyError:
            await websocket.close(code=4404, reason="project not found")
            return

        lang = (websocket.query_params.get("lang") or "").strip()
        server_id = (websocket.query_params.get("server") or "").strip() or None
        await websocket.accept()
        if not lang:
            await websocket.send_json(
                {"jsonrpc": "2.0", "method": "code-agent/error", "params": {"message": "缺少 lang 参数"}}
            )
            await websocket.close(code=4400, reason="missing lang")
            return
        try:
            session = await lsp.session_for(project, lang, tree_id=tree, server_id=server_id)
        except Exception as exc:  # noqa: BLE001 —— 启动失败要如实告诉前端（含安装提示）
            from code_agent.codeintel.lsp_localization import localize_notification
            failure = await localize_notification(lsp.documentation_translator,
                {"jsonrpc": "2.0", "method": "code-agent/error", "params": {"message": str(exc)}})
            await websocket.send_json(
                failure
            )
            await websocket.close(code=4500, reason="lsp start failed")
            return

        # 建立时先把该语言服务器已有的诊断推给前端（刷新页面后也能立刻看到红线）
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "code-agent/ready",
                "params": {
                    "server": session.server_id,
                    "capabilities": session.capabilities,
                    "environment": session.environment,
                    "initialization_ms": session.initialization_ms,
                    "document_versions": dict(session.documents),
                    "diagnostics": {
                        uri: items for uri, items in session.diagnostics.items()
                        if items and (session.diagnostic_versions.get(uri) is None
                                      or session.diagnostic_versions[uri] == session.documents.get(uri))
                    },
                    "diagnostic_versions": dict(session.diagnostic_versions),
                },
            }
        )
        # 之后语言服务器的新诊断实时扇出到这条连接
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        requests: dict[int | str, asyncio.Task] = {}
        actual_ids: dict[int | str, int] = {}
        overflow = False

        def _enqueue(message: dict) -> None:
            nonlocal overflow
            if overflow:
                return
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                overflow = True
                queue.get_nowait()
                queue.put_nowait({"jsonrpc": "2.0", "method": "code-agent/error",
                                 "params": {"message": "语言服务输出过多，请重新连接", "reconnect": True}})

        unsubscribe = lsp.subscribe(session, _enqueue)

        async def _respond(message: dict) -> None:
            rid = message["id"]
            try:
                response = await lsp.handle_client_request(
                    session, message, on_request_id=lambda actual: actual_ids.__setitem__(rid, actual)
                )
                _enqueue(response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                from code_agent.codeintel.lsp_localization import localize_error
                error = await localize_error(lsp.documentation_translator, {"code": -32603, "message": str(exc)})
                _enqueue({"jsonrpc": "2.0", "id": rid, "error": error})

        def _finished(rid: int | str, task: asyncio.Task) -> None:
            if requests.get(rid) is task:
                requests.pop(rid, None)
                actual_ids.pop(rid, None)
            if task.cancelled():
                _enqueue({"jsonrpc": "2.0", "id": rid, "error": {"code": -32800, "message": "请求已取消"}})

        async def _write() -> None:
            while True:
                await websocket.send_json(await queue.get())
                if overflow:
                    await websocket.close(code=1013, reason="lsp output overflow")
                    return

        async def _read() -> None:
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "$/cancelRequest" and message.get("id") is None:
                    params = message.get("params") or {}
                    rid = params.get("id") if isinstance(params, dict) else None
                    if not isinstance(rid, bool) and isinstance(rid, (int, str)):
                        task = requests.get(rid)
                        if task is not None:
                            task.cancel()  # Manager maps this connection's request to the actual stdio id.
                    continue
                rid = message.get("id")
                if rid is not None:
                    if isinstance(rid, bool) or not isinstance(rid, (int, str)):
                        _enqueue({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "请求 id 无效"}})
                    elif rid in requests or len(requests) >= 64:
                        _enqueue({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": "重复 id 或待处理请求过多"}})
                    else:
                        task = asyncio.create_task(_respond(message))
                        requests[rid] = task
                        task.add_done_callback(lambda finished, request_id=rid: _finished(request_id, finished))
                else:
                    try:
                        await lsp.handle_client_notification(session, message)
                    except (JsonRpcError, ValueError, TypeError, OSError, RecursionError) as exc:
                        _enqueue({"jsonrpc": "2.0", "method": "code-agent/error", "params": {"message": str(exc)}})

        reader = asyncio.create_task(_read())
        writer = asyncio.create_task(_write())
        try:
            done, _ = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                    task.result()
        except asyncio.CancelledError:
            # ASGI/TestClient cancels the connection scope when its socket closes.
            pass
        finally:
            unsubscribe()
            pending = [reader, writer, *requests.values()]
            for task in pending:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    @app.get("/api/v1/code/projects/{project_id}/work-items")
    def work_items(project_id: str, limit: int = Query(default=100, ge=1, le=100),
                   user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            project = _owned(user, project_id)
            return project_work_items(project=project, manager=manager, tasks=tasks, batches=batches,
                                      batch_service=batch_service, task_view=task_view, limit=limit)
        except Exception as exc:
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/work-items")
    async def create_work_item(project_id: str, body: WorkItemCreate,
                               user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            project = _owned(user, project_id)
            if body.execution_mode == "team":
                team = getattr(manager, "agent_team", None)
                if team is None:
                    raise HTTPException(503, "项目团队调度未启用")
                lead = team._lead(project_id, user.user_id)
                # ProjectTeam validates ownership and the isolated tree before dispatch.
                agent = await team.start(lead["session_id"], user.user_id, body.requirement,
                                         title=body.title, tree=body.tree)
                return {"item": agent_item(agent)}
            if body.tree != "main":
                raise HTTPException(422, "请在主工作区创建新的隔离任务")
            payload = body.model_dump(exclude={"execution_mode", "tree"})
            if body.execution_mode == "isolated":
                view = await create_task(project_id, payload, user)
                key = f"task:{view['id']}"
            else:
                view = await create_batch(project_id, payload, user)
                key = f"batch:{view['id']}"
            snapshot = project_work_items(project=project, manager=manager, tasks=tasks, batches=batches,
                                          batch_service=batch_service, task_view=task_view)
            return {"item": next(item for item in snapshot["items"] if item["id"] == key)}
        except HTTPException:
            raise
        except Exception as exc:
            raise _err(exc) from exc

    # 任务（M18 · P4）：隔离工作树 + Plan + 审查 + 合并
    # ------------------------------------------------------------------
    def _task_owned(user: CurrentUser, project_id: str, task_id: str) -> dict:
        """取任务并校验归属（任务必须属于该项目与该用户）。"""
        project = service.get_owned(user.user_id, project_id)
        row = tasks.get(task_id)
        if row is None or row["project_id"] != project_id or row["user_id"] != user.user_id:
            raise KeyError("任务不存在或无权访问")
        return {"project": project, "task": row}

    def _task_execution(project: dict, row: dict) -> dict:
        execution = task_item(row, task_view(row), tree_agents(getattr(manager, "agent_team", None), project, f"task:{row['id']}"))
        return {key: execution.get(key) for key in ("agent", "agents", "execution_state", "pending_approval", "pending_approvals", "pending_questions",
                                                  "workspace_available", "phase", "current_tool", "verification", "active", "progress", "execution_managed")}

    @app.get("/api/v1/code/projects/{project_id}/tasks")
    def list_tasks(
        project_id: str, limit: int = 30, user: CurrentUser = Depends(get_current_user)
    ) -> list[dict]:
        """任务列表（含当前状态与计划摘要，供代码页的任务面板展示）。"""
        try:
            _owned(user, project_id)
            rows = tasks.list_for_project(project_id, limit=max(1, min(int(limit), 100)))
            return [task_view(row) for row in rows]
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/tasks")
    async def create_task(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """建任务：隔离工作树 + 计划阶段。

        `plan_first=true`（默认）先让模型产出结构化 Plan，用户批准后再执行；
        直接给 `plan` 可以手工指定计划（也用于"模型没产出可解析的计划"时兜底）。
        """
        if task_service is None:
            raise HTTPException(status_code=503, detail="任务服务未启用")
        try:
            project = _owned(user, project_id)
            row = await task_service.create(
                project,
                user_id=user.user_id,
                requirement=str(body.get("requirement") or ""),
                title=str(body.get("title") or ""),
                base_branch=str(body.get("base_branch") or ""),
                git_init=bool(body.get("git_init")),
            )
            plan = body.get("plan")
            if isinstance(plan, dict) and plan.get("subtasks") is not None:
                task_service._write_plan(row, plan)
                row = tasks.get(row["id"]) or row
            elif body.get("plan_first", True):
                await _run_planner(project, row)
                row = tasks.get(row["id"]) or row
            return task_view(row)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    async def _run_planner(project: dict, task: dict) -> dict:
        """跑计划阶段：让模型产出 Plan JSON（失败进 plan_failed，用户可手改）。"""
        try:
            outcome = await task_service.run_agent(
                project, task, task_service.plan_prompt(task["requirement"]),
                permission_mode="read-only",
            )
        except Exception as exc:  # noqa: BLE001
            tasks.update(task["id"], status="plan_failed", error=f"计划阶段失败：{exc}")
            return {"ok": False, "status": "plan_failed", "error": str(exc)}
        return task_service.store_plan(tasks.get(task["id"]) or task, outcome.get("text") or "")

    @app.get("/api/v1/code/projects/{project_id}/tasks/{task_id}")
    def task_detail(
        project_id: str, task_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        try:
            ctx = _task_owned(user, project_id, task_id)
            task = ctx["task"]
            view = task_view(task)
            view["plan"] = task_service.plan_of(task) if task_service else {}
            view["review"] = task_service.review_of(task) if task_service else {}
            view.update(_task_execution(ctx["project"], task))
            return view
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.put("/api/v1/code/projects/{project_id}/tasks/{task_id}/plan")
    def update_plan(
        project_id: str,
        task_id: str,
        body: dict[str, Any],
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """手改计划（模型产不出可解析 JSON 时的兜底；也用于用户自己收敛范围）。"""
        if task_service is None:
            raise HTTPException(status_code=503, detail="任务服务未启用")
        try:
            ctx = _task_owned(user, project_id, task_id)
            plan = body.get("plan")
            if not isinstance(plan, dict):
                raise HTTPException(status_code=422, detail="plan 必须是对象")
            task_service._write_plan(ctx["task"], plan)
            return task_view(tasks.get(task_id) or ctx["task"])
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/tasks/{task_id}/approve")
    async def approve_task(
        project_id: str, task_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """批准计划并执行（AI 在隔离工作树里改代码）。"""
        if task_service is None:
            raise HTTPException(status_code=503, detail="任务服务未启用")
        try:
            ctx = _task_owned(user, project_id, task_id)
            result = await task_service.execute(ctx["project"], ctx["task"])
            return {**task_view(tasks.get(task_id) or ctx["task"]), "outcome": result.get("outcome", {}),
                    "review_pending": bool(result.get("review_pending"))}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/tasks/{task_id}/review")
    async def task_review(
        project_id: str, task_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """审查快照：文件级变更 + 符号级差异 + 越界清单 + 诊断。"""
        if task_service is None:
            raise HTTPException(status_code=503, detail="任务服务未启用")
        try:
            ctx = _task_owned(user, project_id, task_id)
            return await task_service.review(ctx["project"], ctx["task"])
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/tasks/{task_id}/diff")
    async def task_diff(
        project_id: str,
        task_id: str,
        path: str = "",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """任务相对基准分支的文本差异（整树或单个文件）。"""
        if task_service is None:
            raise HTTPException(status_code=503, detail="任务服务未启用")
        try:
            ctx = _task_owned(user, project_id, task_id)
            task = ctx["task"]
            if path:
                from code_agent.codeintel.paths import safe_child
                from code_agent.codeintel.uri import file_uri

                wt = Path(task["worktree"])
                safe_child(wt, path)  # 越界直接拒绝
                code, out = await task_service._git(
                    wt, ["diff", task["base_branch"] or "HEAD", "--", path], timeout=60.0
                )
                before = after = ""
                with contextlib.suppress(Exception):
                    before = (
                        await task_service._git(wt, ["show", f"{task['base_branch']}:{path}"], timeout=30.0)
                    )[1]
                with contextlib.suppress(Exception):
                    after = (wt / path).read_text(encoding="utf-8", errors="replace")
                return {
                    "path": path,
                    "diff": out,
                    "before": before,
                    "after": after,
                    "uri": file_uri(wt / path),
                }
            return {"diff": await task_service.diff_text(task)}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/tasks/{task_id}/verify")
    async def task_verify(
        project_id: str, task_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """在工作树里跑计划建议的验证命令（用户点按钮触发，不由 AI 自主执行）。"""
        if task_service is None:
            raise HTTPException(status_code=503, detail="任务服务未启用")
        try:
            ctx = _task_owned(user, project_id, task_id)
            return await task_service.verify(ctx["task"])
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/tasks/{task_id}/merge")
    async def merge_task(
        project_id: str, task_id: str, body: dict[str, Any] | None = None,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """合并到主干：给 files 则只应用这些文件（不自动提交），否则整体合并。

        踩到架构规则（error 级）时会拒绝，除非 `force=true` 且给了 `reason`——
        理由会写进被改文件的决策线程（"强制"必须留痕）。
        """
        if task_service is None:
            raise HTTPException(status_code=503, detail="任务服务未启用")
        body = body or {}
        try:
            ctx = _task_owned(user, project_id, task_id)
            files = body.get("files")
            payload = [str(f) for f in files] if isinstance(files, list) and files else None
            result = await task_service.merge(
                ctx["project"], ctx["task"], files=payload,
                force=bool(body.get("force")), reason=str(body.get("reason") or ""),
            )
            return {**result, "task": task_view(tasks.get(task_id) or ctx["task"])}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/tasks/{task_id}/discard")
    async def discard_task(
        project_id: str, task_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """放弃任务：删工作树与分支，主干不受影响。"""
        if task_service is None:
            raise HTTPException(status_code=503, detail="任务服务未启用")
        try:
            ctx = _task_owned(user, project_id, task_id)
            result = await task_service.discard(ctx["project"], ctx["task"])
            return {**result, "task": task_view(tasks.get(task_id) or ctx["task"])}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    def batch_view(batch: dict) -> dict:
        """批次视图：批次 + 车道（含租约、等待关系、合并队列顺序）。"""
        lanes = tasks.list_for_batch(batch["id"]) if tasks is not None else []
        view = batch_service.view(batch, lanes)
        project = project_repo.get(batch["project_id"])
        by_id = {row["id"]: row for row in lanes}
        if project:
            for lane in view.get("lanes", []):
                row = by_id.get(lane["task_id"])
                if row:
                    lane.update(_task_execution(project, row))
                    lane["session_id"] = row.get("session_id") or ""
        return view

    def _batch_owned(user: CurrentUser, project_id: str, batch_id: str) -> dict:
        project = service.get_owned(user.user_id, project_id)
        row = batches.get(batch_id)
        if row is None or row["project_id"] != project_id or row["user_id"] != user.user_id:
            raise KeyError("批次不存在或无权访问")
        return {"project": project, "batch": row}

    # ------------------------------------------------------------------
    # 并行批次（M18 · P5）：一个需求 → N 条互不重叠的车道 → 串行合并
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/projects/{project_id}/batches")
    def list_batches(
        project_id: str, limit: int = 30, user: CurrentUser = Depends(get_current_user)
    ) -> list[dict]:
        """批次列表（含车道进度；不含每条车道的完整计划）。"""
        if batch_service is None:
            raise HTTPException(status_code=503, detail="并行批次服务未启用")
        try:
            _owned(user, project_id)
            rows = batches.list_for_project(project_id, limit=max(1, min(int(limit), 100)))
            out: list[dict] = []
            for row in rows:
                view = batch_view(row)
                out.append(
                    {
                        "id": view["id"], "title": view["title"], "status": view["status"],
                        "requirement": view["requirement"], "error": view["error"],
                        "created_at": view["created_at"], "updated_at": view["updated_at"],
                        "goal": (view["plan"] or {}).get("goal") or "",
                        "progress": view["progress"], "conflicts": len(view["conflicts"]),
                        "queue": view["queue"],
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/batches")
    async def create_batch(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """建批次。

        给 `plan`（{lanes:[…]}）= 手工拆分；`plan_first=true` = 让模型拆（只读规划）。
        互不重叠的车道**立刻**建隔离工作树；与前面车道抢同一个文件的车道会停在
        `deferred`（等前置落地后从最新 HEAD 出发再跑，见合并队列）。
        """
        if batch_service is None:
            raise HTTPException(status_code=503, detail="并行批次服务未启用")
        try:
            project = _owned(user, project_id)
            plan = body.get("plan")
            batch = await batch_service.create(
                project,
                user_id=user.user_id,
                requirement=str(body.get("requirement") or ""),
                title=str(body.get("title") or ""),
                base_branch=str(body.get("base_branch") or ""),
                git_init=bool(body.get("git_init")),
                plan=plan if isinstance(plan, dict) and plan.get("lanes") else None,
            )
            if not batch_service.plan_of(batch) and body.get("plan_first", True):
                await batch_service.plan_with_model(project, batch)
            return batch_view(batches.get(batch["id"]) or batch)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/batches/{batch_id}")
    def batch_detail(
        project_id: str, batch_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        if batch_service is None:
            raise HTTPException(status_code=503, detail="并行批次服务未启用")
        try:
            ctx = _batch_owned(user, project_id, batch_id)
            return batch_view(ctx["batch"])
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.put("/api/v1/code/projects/{project_id}/batches/{batch_id}/plan")
    async def update_batch_plan(
        project_id: str, batch_id: str, body: dict[str, Any],
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """手改车道拆分：新增的车道会建工作树，删掉的车道会被放弃。

        为什么允许改：模型的拆分经常"太乐观"（同一文件拆到两条车道里）。
        人工收敛范围后重排租约，比整批重来便宜得多。
        """
        if batch_service is None:
            raise HTTPException(status_code=503, detail="并行批次服务未启用")
        try:
            ctx = _batch_owned(user, project_id, batch_id)
            plan = body.get("plan") if isinstance(body.get("plan"), dict) else body
            await batch_service.replace_plan(ctx["project"], ctx["batch"], plan)
            return batch_view(batches.get(batch_id) or ctx["batch"])
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/batches/{batch_id}/approve")
    async def approve_batch(
        project_id: str, batch_id: str, body: dict[str, Any] | None = None,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """批准并执行：所有车道并发跑（同波内同时执行，波次由写租约决定）。"""
        if batch_service is None:
            raise HTTPException(status_code=503, detail="并行批次服务未启用")
        body = body or {}
        try:
            ctx = _batch_owned(user, project_id, batch_id)
            run = await batch_service.run(
                ctx["project"], ctx["batch"],
                max_workers=int(body.get("max_workers") or 0),
                stop_on_failure=bool(body.get("stop_on_failure")),
            )
            return {**run, "batch": batch_view(batches.get(batch_id) or ctx["batch"])}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/batches/{batch_id}/review")
    async def review_batch(
        project_id: str, batch_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """刷新整批审查：每条车道的符号级差异 + 整批合计。"""
        if batch_service is None:
            raise HTTPException(status_code=503, detail="并行批次服务未启用")
        try:
            ctx = _batch_owned(user, project_id, batch_id)
            result = await batch_service.review_all(ctx["project"], ctx["batch"])
            return {**result, "batch": batch_view(batches.get(batch_id) or ctx["batch"])}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/batches/{batch_id}/merge")
    async def merge_batch(
        project_id: str, batch_id: str, body: dict[str, Any] | None = None,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """串行合并队列：一次一条进主干；失败即恢复主干并停队列。

        P6：每条车道落地前同样过架构门禁；`force=true` + `reason` 才能绕过。
        """
        if batch_service is None:
            raise HTTPException(status_code=503, detail="并行批次服务未启用")
        body = body or {}
        try:
            ctx = _batch_owned(user, project_id, batch_id)
            lanes = body.get("lanes")
            result = await batch_service.merge_queue(
                ctx["project"], ctx["batch"],
                lanes=[str(x) for x in lanes] if isinstance(lanes, list) and lanes else None,
                stop_on_conflict=bool(body.get("stop_on_conflict", True)),
                force=bool(body.get("force")), reason=str(body.get("reason") or ""),
            )
            return {**result, "batch": batch_view(batches.get(batch_id) or ctx["batch"])}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/batches/{batch_id}/discard")
    async def discard_batch(
        project_id: str, batch_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """放弃整批：删掉各车道的工作树与分支，主干一字不动。"""
        if batch_service is None:
            raise HTTPException(status_code=503, detail="并行批次服务未启用")
        try:
            ctx = _batch_owned(user, project_id, batch_id)
            result = await batch_service.discard(ctx["project"], ctx["batch"])
            return {**result, "batch": batch_view(batches.get(batch_id) or ctx["batch"])}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    # ------------------------------------------------------------------
    # 源代码管理（M18 · P4）：状态 / 差异 / 提交 / 拉取推送
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/projects/{project_id}/git")
    async def git_status(project_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        """工作区状态：分支、远端、ahead/behind、改动清单。"""
        try:
            project = _owned(user, project_id)
            return await git_overview(project)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/git/commit")
    async def git_commit(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """提交：`add -A`（可选指定文件）+ commit。提交信息必填。"""
        try:
            project = _owned(user, project_id)
            message = str(body.get("message") or "").strip()
            if not message:
                raise HTTPException(status_code=422, detail="提交信息不能为空")
            files = body.get("files")
            root = Path(project["root_path"])
            if isinstance(files, list) and files:
                code, out = await run_git(root, ["add", "--", *[str(f) for f in files]], timeout=60.0)
            else:
                code, out = await run_git(root, ["add", "-A"], timeout=60.0)
            if code != 0:
                raise HTTPException(status_code=422, detail=f"add 失败：{out[:300]}")
            code, out = await run_git(
                root,
                ["-c", "user.name=Code-agent", "-c", "user.email=code-agent@local", "commit", "-m", message],
                timeout=60.0,
            )
            if code != 0:
                raise HTTPException(status_code=422, detail=f"提交失败：{out[:300]}")
            await _intel().scan(project)
            return {"ok": True, "output": out[:500], **await git_overview(project)}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/git/action")
    async def git_action(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """受控 git 动作：fetch / pull(--ff-only) / push / stash。

        强制推送、reset --hard、历史重写等在 `vcs.DENY_PATTERNS` 里被硬拒——
        这些操作能不可逆地破坏用户的东西，平台不提供按钮。
        """
        try:
            project = _owned(user, project_id)
            action = str(body.get("action") or "").strip()
            root = Path(project["root_path"])
            ssh_key = str(body.get("ssh_key") or "")
            if action == "fetch":
                args = ["fetch", "--prune"]
            elif action == "pull":
                # 只允许快进：否则会把冲突/合并策略的复杂度塞给一个按钮
                args = ["pull", "--ff-only"]
            elif action == "push":
                args = ["push"]
                branch = str(body.get("branch") or "").strip()
                if branch:
                    args += ["-u", "origin", branch]
            elif action == "stash":
                args = ["stash", "push", "-m", str(body.get("message") or "code-agent 暂存")[:80]]
            else:
                raise HTTPException(status_code=422, detail=f"不支持的动作：{action}")
            deny = check_command(args)
            if deny:
                raise HTTPException(status_code=403, detail=f"该操作被平台禁止：{deny}")
            code, out = await run_git(root, args, timeout=120.0, ssh_key=ssh_key)
            return {
                "ok": code == 0,
                "output": out[:2000],
                "action": action,
                **(await git_overview(project) if code == 0 else {}),
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/git/branches")
    async def git_branches(project_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            project = _owned(user, project_id)
            root = Path(project["root_path"])
            code, out = await run_git(root, ["branch", "--format=%(refname:short)%09%(upstream:short)"])
            branches = []
            if code == 0:
                for line in out.splitlines():
                    name, _, upstream = line.partition("\t")
                    if name.strip():
                        branches.append({"name": name.strip(), "upstream": upstream.strip()})
            return {"branches": branches}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/git/checkout")
    async def git_checkout(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """切分支：工作区不干净时拒绝（避免把改动带到别的分支上）。"""
        try:
            project = _owned(user, project_id)
            branch = str(body.get("branch") or "").strip()
            if not branch:
                raise HTTPException(status_code=422, detail="branch 不能为空")
            root = Path(project["root_path"])
            info = await probe_repo(root, with_status=True)
            if info.dirty:
                raise HTTPException(
                    status_code=409,
                    detail="工作区有未提交改动：请先提交或暂存（stash）后再切分支",
                )
            code, out = await run_git(root, ["checkout", branch], timeout=60.0)
            if code != 0:
                raise HTTPException(status_code=422, detail=f"切换失败：{out[:300]}")
            await _intel().scan(project)
            return {"ok": True, "output": out[:500], **await git_overview(project)}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    # ------------------------------------------------------------------
    # 修改台账（M18 · P2）：AI 的每次写入都可审查、可回滚
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/projects/{project_id}/patches")
    def list_patches(
        project_id: str,
        limit: int = 50,
        path: str = "",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        try:
            return _intel().list_patches(
                _owned(user, project_id), limit=max(1, min(int(limit), 200)), path=path or None
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/patches/{patch_id}")
    def patch_detail(
        project_id: str, patch_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """单条改动详情（含 unified diff）。"""
        try:
            return _intel().patch_detail(_owned(user, project_id), patch_id)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/patches/{patch_id}/revert")
    async def revert_patch(
        project_id: str, patch_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """回滚一次 AI 改动，并立刻重建索引让地图跟上。

        平台会拒绝"改动之后文件又被改过"的回滚（避免覆盖别人的工作），
        这条拒绝以 409 返回，前端照原文提示即可。
        """
        try:
            project = _owned(user, project_id)
            handle = _intel()
            result = await asyncio.to_thread(handle.revert_patch, project, patch_id)
            await handle.scan(project)
            return {**result, **handle.status(project)}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    # ------------------------------------------------------------------
    # P6：影响面 / 架构规则与门禁 / 决策线程 / 改动溯源
    # ------------------------------------------------------------------
    @app.post("/api/v1/code/projects/{project_id}/impact")
    def code_impact(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """影响面 + 关联测试 + 风险分级：改这里会惊动谁。

        入参 `{symbol_ids?: [...], files?: [...], tree?: "main", depth?: 3}`。
        这两个入口（符号 / 文件）覆盖了地图上能点到的一切节点。
        """
        try:
            project = _owned(user, project_id)
            return _intel().impact(
                project,
                tree_id=str(body.get("tree") or "main"),
                symbol_ids=[str(s) for s in (body.get("symbol_ids") or [])],
                files=[str(f) for f in (body.get("files") or [])],
                depth=int(body.get("depth") or 3),
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/architecture")
    def get_architecture(project_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        """架构规则（分层 / 禁环 / 公共面 / 保护路径）与规则自检结果。"""
        try:
            project = _owned(user, project_id)
            return _intel().architecture(project)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.put("/api/v1/code/projects/{project_id}/architecture")
    def put_architecture(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """保存架构规则。规则写坏了直接 422（宁可不生效，也不要按坏规则拦人）。"""
        try:
            project = _owned(user, project_id)
            rules = body.get("rules") if isinstance(body.get("rules"), dict) else body
            if body.get("rules") is None and not isinstance(body, dict):
                raise ValueError("请求体必须是规则对象或 {rules: {...}}")
            return _intel().save_architecture(project, rules or {})
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/gate")
    def code_gate(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """对一组文件跑架构门禁（改动预演：还没落地就先看会不会被拦）。"""
        try:
            project = _owned(user, project_id)
            files = [str(f) for f in (body.get("files") or [])]
            symbols = [s for s in (body.get("symbols") or []) if isinstance(s, dict)]
            return _intel().gate(
                project, tree_id=str(body.get("tree") or "main"), files=files, changed_symbols=symbols
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/decisions")
    def list_decisions(
        project_id: str,
        target: str = "",
        limit: int = 50,
        kinds: str = "",
        prefix: bool = False,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """节点决策线程：这个符号/文件上依次发生过什么决定、有什么已知风险与待办。"""
        try:
            project = _owned(user, project_id)
            store = _intel().decisions(project["id"])
            kinds_list = [k.strip() for k in kinds.split(",") if k.strip()]
            items = store.list(target=target, limit=limit, kinds=kinds_list or None, prefix=prefix)
            return {
                "target": target,
                "items": [store.view(i) for i in items],
                "summary": store.summary(),
                "kinds": list(DECISION_KINDS),
                "path": str(store.path),
            }
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/decisions")
    def add_decision(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """记一条决策/风险/待办。目标必须是地图节点（符号或文件）。"""
        try:
            project = _owned(user, project_id)
            store = _intel().decisions(project["id"])
            item = store.record(
                target=str(body.get("target") or ""),
                text=str(body.get("text") or ""),
                kind=str(body.get("kind") or "note"),
                author=user.username or user.user_id,
                session_id=str(body.get("session_id") or ""),
                task_id=str(body.get("task_id") or ""),
                batch_id=str(body.get("batch_id") or ""),
            )
            return {"ok": True, "item": store.view(item)}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.delete("/api/v1/code/projects/{project_id}/decisions/{decision_id}")
    def delete_decision(
        project_id: str, decision_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        try:
            project = _owned(user, project_id)
            removed = _intel().decisions(project["id"]).delete(decision_id)
            if not removed:
                raise KeyError(f"决策不存在：{decision_id}")
            return {"ok": True, "removed": removed}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/provenance")
    async def code_provenance(
        project_id: str,
        path: str = "",
        symbol_id: str = "",
        tree: str = "main",
        limit: int = 80,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """改动溯源：台账 + 任务/批次 + 决策 + git 提交合成一条时间线。"""
        try:
            project = _owned(user, project_id)
            return await _intel().provenance(
                project, tree_id=tree, path=path, symbol_id=symbol_id,
                tasks=tasks, limit=max(1, min(int(limit), 200)),
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    # ------------------------------------------------------------------
    # P7：测试即规格（行为卡片 / 覆盖率）与图即文档
    # ------------------------------------------------------------------
    @app.get("/api/v1/code/projects/{project_id}/symbol/logic")
    def symbol_logic(
        project_id: str,
        id: str,
        tree: str = "main",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """实现步骤：一个函数按什么顺序做了什么事、它调用了谁。

        只有 Python 有语句级解析（AST），其它语言如实返回 `available: false` + 原因。
        """
        try:
            project = _owned(user, project_id)
            if not id:
                raise HTTPException(status_code=422, detail="缺少 id")
            return _intel().function_logic(project, id, tree_id=tree)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/symbol/purpose")
    def symbol_purpose(
        project_id: str,
        id: str,
        tree: str = "main",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """符号的"意义与作用"：它是什么、拿来干什么、谁真的在用。

        只有 Python 有 AST 级语义；其它语言如实降级为"结构事实 + 命名提示"。
        """
        try:
            project = _tree_owned(user, project_id, tree)
            if not id:
                raise HTTPException(status_code=422, detail="缺少 id")
            return _intel().symbol_purpose(project, id, tree_id=tree)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/symbol/business")
    def symbol_business(project_id: str, id: str, tree: str = "main",
                        user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            return _intel().symbol_business(_tree_owned(user, project_id, tree), id, tree_id=tree)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.put("/api/v1/code/projects/{project_id}/symbol/business")
    def put_symbol_business(project_id: str, payload: dict,
                            user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            tree = str(payload.get("tree") or "main")
            target = str(payload.get("id") or "")
            if not target:
                raise ValueError("缺少 id")
            if not isinstance(payload.get("version"), str) or not re.fullmatch(r"[0-9a-f]{40}", payload["version"]):
                raise ValueError("必须提供读取业务说明时取得的 version，请刷新后再保存")
            return _intel().write_symbol_business(_tree_owned(user, project_id, tree), target, payload, tree_id=tree)
        except FileSaveConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/symbol/business/resolve")
    def resolve_symbol_business(project_id: str, payload: dict,
                                user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            tree = str(payload.get("tree") or "main")
            return _intel().resolve_symbol_business(
                _tree_owned(user, project_id, tree), tree_id=tree,
                path=str(payload.get("path") or ""), line=payload.get("line"), column=payload.get("column"),
                name=str(payload.get("name") or ""), text=payload.get("text"),
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/spec/behaviors")
    def spec_behaviors(
        project_id: str,
        path: str = "",
        module: str = "",
        target: str = "",
        tree: str = "main",
        limit: int = 500,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """行为卡片：把测试读成"一条测试 = 一条可读的行为声明"。

        `target` 传符号 id 或文件路径 → 反向查"谁在保护这段代码"。
        """
        try:
            project = _owned(user, project_id)
            return _intel().behaviors(
                project, tree_id=tree, path=path, module=module, target=target,
                limit=max(1, min(int(limit), 1000)),
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/spec/coverage")
    def spec_coverage(
        project_id: str, tree: str = "main", user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """覆盖率三态（直接 / 文件级 / 无）+ 未覆盖的公共符号（地图上色用）。"""
        try:
            project = _owned(user, project_id)
            return _intel().coverage(project, tree_id=tree)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/contracts")
    def project_contracts(
        project_id: str,
        path: str = "",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """契约层（M32）：把 `*.CONTRACT.md` 与实现的对照结果给到界面。

        - 带 `path`：检查那一个实现文件（没有对应契约时如实返回 `has_contract: false`）；
        - 不带 `path`：列出项目里所有契约的汇总（每份的义务数/覆盖率/是否通过）。

        为什么放在"按需查一个文件"的形态上：`NodeDetail` 本来就是点谁查谁，
        契约角标也该是同一节奏——不然每次打开详情都要扫全仓库。
        """
        try:
            project = _owned(user, project_id)
            root = Path(project["root_path"])
            from code_agent.contract import contract_check
            from code_agent.contract.check import list_contracts
            from code_agent.contract.parse import SUFFIX

            if not path.strip():
                items = []
                for contract_file in list_contracts(root, limit=200):
                    rel = contract_file.relative_to(root).as_posix()
                    module = f"{rel[: -len(SUFFIX)]}.py"
                    module_path = root / module
                    if not module_path.is_file():
                        items.append({"module": module, "contract": rel, "ok": False,
                                      "note": "契约存在但找不到对应的实现文件"})
                        continue
                    rep = contract_check(module_path, module_label=module)
                    items.append({
                        "module": module,
                        "contract": rel,
                        "ok": rep.ok,
                        "rho": rep.rho,
                        "obligation_count": rep.obligation_count,
                        "evidence_count": rep.evidence_count,
                        "coverage": rep.coverage,
                        "first_unsatisfied": (rep.first_unsatisfied or ("", ""))[0],
                    })
                return {"count": len(items), "items": items}

            rel = path.strip().replace("\\", "/")
            target = safe_child(root, rel)

            def _summary(module_path: Path, rel_module: str) -> dict:
                rep = contract_check(module_path, module_label=rel_module)
                return {
                    "module": rel_module,
                    "contract": str(Path(rep.contract)) if rep.contract else "",
                    "ok": rep.ok,
                    "rho": rep.rho,
                    "obligation_count": rep.obligation_count,
                    "evidence_count": rep.evidence_count,
                    "coverage": rep.coverage,
                    "declared_gaps": rep.declared_gaps,
                    "first_unsatisfied": (rep.first_unsatisfied or ("", ""))[0],
                }

            if target.is_dir():
                # **目录也要能查**：地图上的"模块节点"（`mod:...`）传进来的就是目录。
                # 真机踩到：这里直接 404 之后，前端把它当"后端是旧版本"（api.js 的 404 启发式），
                # 用户看到的是"请重启后端"——而其实只是资源不存在。
                # 语义定为"这个模块下所有实现的契约汇总"，正好是第一版漂移审计视图。
                items: list[dict] = []
                for contract_file in list_contracts(target, limit=200):
                    rel_module = contract_file.relative_to(root).as_posix()[: -len(SUFFIX)] + ".py"
                    module_path = root / rel_module
                    if module_path.is_file():
                        items.append(_summary(module_path, rel_module))
                    else:
                        items.append({
                            "module": rel_module,
                            "contract": contract_file.relative_to(root).as_posix(),
                            "ok": False,
                            "note": "契约存在但找不到对应的实现文件",
                        })
                python_files = sum(1 for p in target.rglob("*.py") if ".venv" not in p.parts)
                return {
                    "has_contract": bool(items),
                    "is_dir": True,
                    "dir": rel,
                    "count": len(items),
                    "items": items,
                    "python_files": python_files,
                    "summary": (
                        f"{rel}：{len(items)}/{python_files} 个实现有契约" if items
                        else f"{rel} 下没有契约（`*.CONTRACT.md`）"
                    ),
                }

            if not target.is_file():
                raise KeyError(f"文件不存在：{rel}")
            rep = contract_check(target, module_label=rel)
            body = rep.to_dict()
            body["has_contract"] = bool(rep.contract)
            body["summary"] = rep.summary()
            return body
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/features")
    def project_features(
        project_id: str,
        tree: str = "main",
        limit: int = 0,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """功能导航（M33）第一屏：能力域 → 功能。

        能力域 = 二级目录；功能 = 入口（路由/命令/工具/钩子）+ 有 docstring 的公开符号，
        功能名优先用作者写的 docstring 首句。更深的层按需取（`/features/children`），
        所以这里只画第一屏，点一个功能才往下拆——可以一直拆到原子步骤。
        """
        try:
            project = _tree_owned(user, project_id, tree)
            return _intel().features(project, tree_id=tree, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/features/children")
    def project_feature_children(
        project_id: str,
        id: str,
        tree: str = "main",
        visited: str = "",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """一个功能的**细分功能**（点开才取）：类拆成成员，函数拆成它调用的东西。

        `visited` 是当前路径上的祖先 id（逗号分隔）：静态调用图里有环
        （`submit → loop → submit`），出现在里面的子节点会被标 `cyclic` 且不再展开，
        避免前端无限拆下去。拆不到下一层就直接说"这是原子步骤 + 实现页有几步"。
        """
        try:
            project = _tree_owned(user, project_id, tree)
            if not id.strip():
                raise HTTPException(status_code=422, detail="缺少 id")
            path = tuple(x.strip() for x in visited.split(",") if x.strip())
            return _intel().feature_children(
                project, node_id=id, tree_id=tree, visited=path[:40]
            )
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/feature-briefs/fill")
    def start_feature_brief_fill(
        project_id: str,
        limit: int = 0,
        tree: str = "main",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """开始补功能描述（**后台跑**，立刻返回）。

        走的是"批量问模型 + 服务端落库"这条路：**不带任何工具**，模型只产出文本，
        所以任何权限档下都能跑，也不需要给它仓库写权限。进度看 `index` 的 `briefs` 字段。
        """
        try:
            project = _tree_owned(user, project_id, tree)
            return _intel().start_brief_fill(project, limit=limit, tree_id=tree)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/feature-briefs/fill")
    def feature_brief_fill_status(
        project_id: str,
        tree: str = "main",
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """补描述的进度：正在补多少条、成功多少、失败多少。"""
        try:
            project = _tree_owned(user, project_id, tree)
            return _intel().brief_fill_status(project, tree)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/feature-briefs")
    def project_feature_briefs(
        project_id: str,
        missing: int = 0,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """功能描述（M33）：模型给"没写 docstring 的符号"补的一句话。

        - 不带 `missing`：列出已有的描述（含因为代码改过而**失效**的，`fresh=false`）；
        - `missing=1`：列出还缺描述的顶层功能（给 agent 与界面看"还差多少"）。

        描述按「符号 id + 内容哈希」缓存：代码不改就一直复用，改了自动失效——
        不是每次开图都重新生成，也不会让旧描述盖在改过的代码上。
        """
        try:
            project = _owned(user, project_id)
            body = _intel().feature_briefs(project)
            if missing:
                body["missing"] = _intel().feature_brief_missing(project, limit=max(1, missing))
            return body
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.put("/api/v1/code/projects/{project_id}/feature-briefs")
    def put_feature_brief(
        project_id: str,
        payload: dict,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """人工写/改一条功能描述（与 agent 用 `feature_brief` 工具写的是同一份存储）。"""
        try:
            project = _owned(user, project_id)
            symbol_id = str((payload or {}).get("id") or "").strip()
            text = str((payload or {}).get("text") or "").strip()
            if not symbol_id:
                raise HTTPException(status_code=422, detail="缺少 id")
            if not text:
                raise HTTPException(status_code=422, detail="缺少 text")
            item = _intel().feature_brief_write(
                project, symbol_id=symbol_id, text=text, model="human"
            )
            return {"ok": True, "item": item}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.delete("/api/v1/code/projects/{project_id}/feature-briefs")
    def delete_feature_brief(
        project_id: str,
        id: str,
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """删掉一条功能描述（删了图上就回到"无说明"）。"""
        try:
            project = _owned(user, project_id)
            return {"ok": _intel().feature_brief_forget(project, symbol_id=id)}
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @app.get("/api/v1/code/projects/{project_id}/evidence")
    def code_evidence(
        project_id: str, tree: str = "main", user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """证据角标（SRC n）：每个节点上有多少条**已记录**的证据（台账 + 决策）。

        只读视图，不进任何导出物；数字的含义写在 `basis` 里，界面照抄。
        """
        try:
            project = _owned(user, project_id)
            return _intel().evidence(project, tree_id=tree)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    # ------------------------------------------------------------------
    # 项目设置（M18）：直改主干是**显式开关**，默认关闭
    # ------------------------------------------------------------------
    @app.post("/api/v1/code/projects/{project_id}/settings")
    async def update_settings(
        project_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """`allow_direct_write`：是否允许主会话直改主干。

        P4 之前 AI 没有隔离工作树，所以"能写"必须由用户显式打开；
        打开后主会话权限可切到 workspace-write，改动全部进 patch 台账。
        """
        try:
            project = _owned(user, project_id)
            if "allow_direct_write" in body:
                project_repo.set_flags(
                    project_id, allow_direct_write=bool(body.get("allow_direct_write"))
                )
                project = _owned(user, project_id)
                # 关闭直改时，把已打开写权限的主会话压回只读（否则开关是摆设）
                if not bool(project.get("allow_direct_write")) and project.get("session_id"):
                    try:
                        manager.set_permission_mode(project["session_id"], user.user_id, "read-only")
                    except Exception:
                        pass
            return await service.view(project)
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc
