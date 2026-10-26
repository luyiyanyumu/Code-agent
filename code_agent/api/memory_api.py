"""Authenticated management of personal, shared project, and private session memory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.config import code_dir
from code_agent.memory.store import MemoryConflict, UserMemory, WorkspaceMemory


def mount_memory_api(app: FastAPI, manager, sessions_repo, projects_repo) -> None:
    def project_for(project_id: str, user_id: str) -> dict:
        row = projects_repo.get(project_id)
        if row is None or row["user_id"] != user_id:
            raise HTTPException(status_code=404, detail="项目不存在或无权访问")
        return row

    def session_for(session_id: str, user_id: str) -> dict:
        try:
            return manager.get_meta(session_id, user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="会话不存在或无权访问") from exc

    def platform_path(base: Path, identifier: str) -> Path:
        candidate = (base / identifier).resolve()
        if not identifier or identifier in (".", "..") or any(char in identifier for char in "/\\\0:"):
            raise HTTPException(status_code=404, detail="记忆范围不存在")
        if not candidate.is_relative_to(base.resolve()):
            raise HTTPException(status_code=404, detail="记忆范围不存在")
        return candidate

    def resolve(user_id: str, *, scope: str = "user", scope_id: str = "",
                project_id: str = "", session_id: str = ""):
        if scope_id:
            if scope_id == "user":
                scope = "user"
            elif scope_id.startswith("project:"):
                scope, project_id = "project", scope_id.partition(":")[2]
            elif scope_id.startswith("session:"):
                scope, session_id = "session", scope_id.partition(":")[2]
            else:
                raise HTTPException(status_code=422, detail="未知记忆范围")
        if scope == "workspace":
            meta = session_for(session_id, user_id)
            if meta.get("project_id"):
                scope, project_id = "project", meta["project_id"]
            else:
                scope = "session"
        if scope == "user":
            return UserMemory(user_id), {"id": "user", "kind": "user", "title": "个人记忆"}
        if scope == "project":
            if not project_id:
                raise HTTPException(status_code=422, detail="项目记忆需要 project_id")
            project = project_for(project_id, user_id)
            return WorkspaceMemory(platform_path(code_dir(), project_id)), {
                "id": f"project:{project_id}", "kind": "project", "project_id": project_id,
                "title": project.get("name") or "未命名项目", "updated_at": project.get("updated_at") or "",
            }
        if scope == "session":
            if not session_id:
                raise HTTPException(status_code=422, detail="会话记忆需要 session_id")
            meta = session_for(session_id, user_id)
            return WorkspaceMemory(platform_path(manager.workspaces_root, session_id)), {
                "id": f"session:{session_id}", "kind": "session", "session_id": session_id,
                "project_id": meta.get("project_id") or "", "title": meta.get("title") or "未命名会话",
                "updated_at": meta.get("updated_at") or "",
            }
        raise HTTPException(status_code=422, detail="scope 只能是 user/project/session/workspace")

    def revision_value(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not str(value).isdigit():
            raise HTTPException(status_code=422, detail="revision 必须是非负整数")
        return int(value)

    def check_fields(body: dict, *, adding: bool = False) -> dict:
        fields = {}
        if "content" in body or adding:
            value = body.get("content")
            if not isinstance(value, str) or not value.strip():
                raise HTTPException(status_code=422, detail="content 不能为空")
            if len(value) > 1000:
                raise HTTPException(status_code=422, detail="content 过长（>1000 字符）")
            fields["content"] = value.strip()
        for key in ("enabled", "pinned"):
            if key in body:
                if not isinstance(body[key], bool):
                    raise HTTPException(status_code=422, detail=f"{key} 必须是布尔值")
                fields[key] = body[key]
        if "category" in body:
            if not isinstance(body["category"], str) or len(body["category"]) > 80:
                raise HTTPException(status_code=422, detail="category 必须是 80 字符内的文本")
            fields["category"] = body["category"].strip() or "general"
        return fields

    def from_body(body: dict, user_id: str):
        values = {}
        for key in ("scope", "scope_id", "project_id", "session_id"):
            value = body.get(key, "user" if key == "scope" else "")
            if not isinstance(value, str):
                raise HTTPException(status_code=422, detail=f"{key} 必须是文本")
            values[key] = value
        return resolve(user_id, **values)

    def mutation_result(memory, descriptor: dict, entry_id: str = "") -> dict:
        snapshot = memory.snapshot()
        result = {"ok": True, "scope": descriptor["kind"], "scope_id": descriptor["id"],
                  **{key: descriptor[key] for key in ("project_id", "session_id") if key in descriptor}, **snapshot}
        if entry_id:
            result["record"] = next((row for row in snapshot["records"] if row["id"] == entry_id), None)
        return result

    @app.get("/api/v1/memory")
    def list_memory(limit: int = Query(100, ge=1, le=1000), user: CurrentUser = Depends(get_current_user)) -> dict:
        personal, descriptor = resolve(user.user_id)
        personal_scope = {**descriptor, **personal.snapshot()}
        scopes = [personal_scope]
        seen_roots = {str(personal.root.resolve()).casefold()}
        for project in projects_repo.list_for_user(user.user_id):
            memory, descriptor = resolve(user.user_id, scope="project", project_id=project["id"])
            key = str(memory.root.resolve()).casefold()
            if key not in seen_roots:
                scopes.append({**descriptor, **memory.snapshot()})
                seen_roots.add(key)
        sessions = []
        for meta in sessions_repo.list_for_user(user.user_id, limit=limit):
            memory, descriptor = resolve(user.user_id, scope="session", session_id=meta["id"])
            key = str(memory.root.resolve()).casefold()
            if key in seen_roots:
                continue
            row = {**descriptor, **memory.snapshot()}
            scopes.append(row)
            seen_roots.add(key)
            if row["entries"]:
                sessions.append(row)
        return {"user": personal_scope, "sessions": sessions, "projects": [row for row in scopes if row["kind"] == "project"], "scopes": scopes}

    @app.post("/api/v1/memory")
    def add_memory(body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        fields = check_fields(body, adding=True)
        memory, descriptor = from_body(body, user.user_id)
        try:
            record = memory.save_fact(fields.pop("content"), **fields, source="manual", revision=revision_value(body.get("revision")))
        except MemoryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return mutation_result(memory, descriptor, record["id"])

    @app.patch("/api/v1/memory/{entry_id}")
    def update_memory(entry_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        fields = check_fields(body)
        if not fields:
            raise HTTPException(status_code=422, detail="需要提供 content/enabled/pinned/category 之一")
        revision = revision_value(body.get("revision"))
        expected = body.get("expected_content")
        if expected is not None and not isinstance(expected, str):
            raise HTTPException(status_code=422, detail="expected_content 必须是文本")
        if revision is None and expected is None:
            raise HTTPException(status_code=422, detail="编辑需要 revision 或 expected_content，防止覆盖新修改")
        memory, descriptor = from_body(body, user.user_id)
        try:
            record = memory.update_record(entry_id, fields, revision=revision, expected_content=expected)
        except MemoryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="记忆条目不存在")
        return mutation_result(memory, descriptor, entry_id)

    @app.delete("/api/v1/memory")
    def delete_memory(scope: str = "user", scope_id: str = "", project_id: str = "", session_id: str = "",
                      index: int = -1, entry_id: str = "", revision: str | None = None,
                      expected_content: str | None = None, user: CurrentUser = Depends(get_current_user)) -> dict:
        expected_revision = revision_value(revision)
        if entry_id and expected_revision is None and expected_content is None:
            raise HTTPException(status_code=422, detail="删除需要 revision 或 expected_content，防止误删新修改")
        memory, descriptor = resolve(user.user_id, scope=scope, scope_id=scope_id, project_id=project_id, session_id=session_id)
        try:
            if entry_id:
                removed = memory.remove_record(entry_id, revision=expected_revision, expected_content=expected_content)
            else:
                removed = memory.remove_entry(index, revision=expected_revision, expected_content=expected_content)
        except MemoryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="记忆条目不存在")
        return mutation_result(memory, descriptor)
