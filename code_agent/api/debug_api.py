"""Authenticated project debugger routes, independently mountable by the app."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel.debug_runtime import DebugError, DebugRuntime
from code_agent.codeintel.paths import ProjectPathError
from code_agent.codeintel.projects import ProjectService


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, "项目或调试会话不存在")
    if isinstance(exc, (ProjectPathError, ValueError, TypeError)):
        return HTTPException(422, str(exc))
    if isinstance(exc, DebugError):
        return HTTPException(exc.status, str(exc))
    return HTTPException(500, "调试服务失败，请检查后端日志")


def mount_debug_api(app: FastAPI, *, service: ProjectService, runtime: DebugRuntime):
    prefix = "/api/v1/code/projects/{project_id}/debug"

    def owned(user: CurrentUser, project_id: str):
        return service.get_owned(user.user_id, project_id)

    def session(user: CurrentUser, project_id: str, session_id: str):
        owned(user, project_id)
        return runtime.get(user.user_id, project_id, session_id)

    @app.get(prefix + "/meta")
    async def meta(project_id: str, user: CurrentUser = Depends(get_current_user)):
        try:
            project = owned(user, project_id)
            return runtime.meta(service.root_of(project))
        except Exception as exc:
            raise _error(exc) from exc

    @app.get(prefix + "/sessions")
    async def sessions(project_id: str, user: CurrentUser = Depends(get_current_user)):
        try:
            owned(user, project_id)
            return {"sessions": runtime.list(user.user_id, project_id)}
        except Exception as exc:
            raise _error(exc) from exc

    @app.post(prefix + "/sessions")
    async def launch(project_id: str, body: dict, user: CurrentUser = Depends(get_current_user)):
        try:
            project = owned(user, project_id)
            root = service.root_of(project)
            started = await runtime.start(user.user_id, project_id, root, path=body.get("path"), args=body.get("args"), cwd=body.get("cwd") or "", breakpoints=body.get("breakpoints"), language=body.get("language"), java=body.get("java"))
            return started.snapshot()
        except Exception as exc:
            raise _error(exc) from exc

    @app.get(prefix + "/sessions/{session_id}")
    async def state(project_id: str, session_id: str, frame_id: int | None = None, after_console: int = Query(0, ge=0), user: CurrentUser = Depends(get_current_user)):
        try:
            current = session(user, project_id, session_id)
            if frame_id is not None:
                async with current._lock:
                    if current.state["status"] != "paused":
                        raise DebugError("程序暂停后才能查看调用栈帧")
                    if current._inspection and not current._inspection.done():
                        await current._inspection
                    if frame_id not in {frame["id"] for frame in current.state["stack"]}:
                        raise DebugError("调用栈帧已失效")
                    await current.inspect(current._generation, frame_id)
            return current.snapshot(after_console)
        except Exception as exc:
            raise _error(exc) from exc

    @app.post(prefix + "/sessions/{session_id}/action")
    async def action(project_id: str, session_id: str, body: dict, user: CurrentUser = Depends(get_current_user)):
        try:
            current = session(user, project_id, session_id)
            thread = body.get("thread_id")
            if thread is not None and (isinstance(thread, bool) or not isinstance(thread, int)):
                raise ValueError("thread_id 必须是整数")
            await current.action(body.get("action"), thread)
            return current.snapshot()
        except Exception as exc:
            raise _error(exc) from exc

    @app.post(prefix + "/sessions/{session_id}/breakpoints")
    async def breakpoints(project_id: str, session_id: str, body: dict, user: CurrentUser = Depends(get_current_user)):
        try:
            current = session(user, project_id, session_id)
            await runtime.update_breakpoints(current, body.get("breakpoints", []))
            return current.snapshot()
        except Exception as exc:
            raise _error(exc) from exc

    @app.get(prefix + "/sessions/{session_id}/variables")
    async def variables(project_id: str, session_id: str, reference: int = Query(..., ge=1), pause_seq: int | None = Query(None, ge=0), user: CurrentUser = Depends(get_current_user)):
        try:
            return await session(user, project_id, session_id).variables(reference, pause_seq)
        except Exception as exc:
            raise _error(exc) from exc
