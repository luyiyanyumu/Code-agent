"""人工审查入口；模型只能创建提案，应用入口不作为模型工具暴露。"""
import asyncio
import json
import sqlite3

from fastapi import Depends, HTTPException, Request
from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel.change_review import MAX_TEXT, ReviewConflict, ReviewPersistenceError

MAX_PROPOSAL_BODY = MAX_TEXT * 12 + 64 * 1024
MAX_DECISION_BODY = 16 * 1024
MAX_BATCH_BODY = 2_000_000 * 12 + 64 * 1024


async def _body(request: Request, limit: int):
    declared = request.headers.get("content-length")
    if declared:
        try:
            size = int(declared)
            if size < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(422, "Content-Length 不合法") from exc
        if size > limit:
            raise HTTPException(413, "审查请求过大，请拆分提案")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, "审查请求过大，请拆分提案")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(422, "请求必须是有效 JSON object") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "请求必须是 JSON object")
    return value


def mount_change_review_api(app, *, service, reviews, intel):
    prefix = "/api/v1/code/projects/{project_id}/change-proposals"

    def project_for(user, pid):
        try:
            return service.get_owned(user.user_id, pid)
        except KeyError as exc:
            raise HTTPException(404, "项目不存在") from exc

    def error(exc):
        if isinstance(exc, ReviewConflict):
            return HTTPException(409, str(exc))
        if isinstance(exc, (KeyError, FileNotFoundError)):
            return HTTPException(404, str(exc).strip("'\""))
        if isinstance(exc, (sqlite3.Error, ReviewPersistenceError)):
            return HTTPException(503, str(exc) if isinstance(exc, ReviewPersistenceError) else "审查存储暂不可用，请重试")
        return HTTPException(422, str(exc))

    async def call(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except (ValueError, KeyError, OSError, sqlite3.Error, ReviewPersistenceError) as exc:
            raise error(exc) from exc

    batches = "/api/v1/code/projects/{project_id}/change-batches"

    @app.get(batches)
    async def batch_listing(project_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(reviews.list_batches, project_for(user, project_id))

    @app.post(batches)
    async def batch_create(project_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
        project = project_for(user, project_id)
        return await call(reviews.create_batch, project, await _body(request, MAX_BATCH_BODY))

    @app.get(batches + "/{batch_id}")
    async def batch_detail(project_id: str, batch_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(reviews.detail_batch, project_for(user, project_id), batch_id)

    @app.post(batches + "/{batch_id}/{action}")
    async def batch_decide(project_id: str, batch_id: str, action: str, request: Request, user: CurrentUser = Depends(get_current_user)):
        project = project_for(user, project_id)
        if action not in ("apply", "reject", "rollback"):
            raise HTTPException(404, "未知重构审查操作")
        body = await _body(request, MAX_DECISION_BODY)
        if body:
            raise HTTPException(422, "文件重构必须整体审查，不支持按块应用")
        result = await call(reviews.decide_batch, project, batch_id, action)
        if result["changed"]:
            # Disk transaction has completed. Index work must not delay the
            # user's save acknowledgement or turn a durable write into an error.
            if hasattr(intel, "request_refresh"):
                try:
                    # Unlike scan(), this increments the dirty generation even
                    # if another scan is already running. Its worker will rerun.
                    result["index_refresh"] = await asyncio.to_thread(intel.request_refresh, project)
                except Exception:
                    result["warning"] = (result.get("warning", "") + " 项目索引暂未排队，文件重构已保存，请稍后刷新。").strip()
            else:
                async def refresh():
                    try:
                        await intel.scan(project)
                    except Exception:
                        pass
                asyncio.create_task(refresh())
        return result

    @app.get(prefix)
    async def listing(project_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(reviews.list, project_for(user, project_id))

    @app.get(prefix + "/{proposal_id}")
    async def detail(project_id: str, proposal_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(reviews.detail, project_for(user, project_id), proposal_id)

    @app.post(prefix)
    async def create(project_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
        project = project_for(user, project_id)
        body = await _body(request, MAX_PROPOSAL_BODY)
        return await call(reviews.create, project, body)

    @app.post(prefix + "/{proposal_id}/{action}")
    async def decide(project_id: str, proposal_id: str, action: str, request: Request, user: CurrentUser = Depends(get_current_user)):
        project = project_for(user, project_id)
        if action not in ("apply", "reject", "rollback"):
            raise HTTPException(404, "未知审查操作")
        body = await _body(request, MAX_DECISION_BODY)
        ids = body.get("hunk_ids")
        if ids is not None and (not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(v, str) or len(v) > 32 for v in ids)):
            raise HTTPException(422, "hunk_ids 必须是差异块 ID 数组")
        result = await call(reviews.decide, project, proposal_id, action, ids)
        if result["changed"]:
            try:
                await intel.scan(project)
            except Exception:
                result["warning"] = (result.get("warning", "") + " 索引暂未刷新，文件修改已保存。").strip()
        return result
