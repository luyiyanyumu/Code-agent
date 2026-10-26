"""Authenticated design actions plus short-lived asset capabilities."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel.frontend_design import DesignError, FrontendDesignService, preview_policy

MAX_PREVIEW_BODY = 16 * 1024
MAX_STYLES_BODY = 2 * 1024 * 1024 + 16 * 1024


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_path: str = Field(strict=True, min_length=1, max_length=4096)
    url: str = Field(default="", strict=True, max_length=4096)
    api_url: str = Field(default="", strict=True, max_length=4096)


class StylesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_path: str = Field(strict=True, min_length=1, max_length=4096)
    css: str = Field(strict=True, max_length=2 * 1024 * 1024)
    expected_sha1: str = Field(strict=True, pattern=r"^(?:[0-9a-f]{40})?$")
    expected_entry_sha1: str = Field(strict=True, pattern=r"^[0-9a-f]{40}$")

    @field_validator("entry_path")
    @classmethod
    def no_control(cls, value: str) -> str:
        if any(ord(c) < 32 for c in value):
            raise ValueError("入口路径不能包含控制字符")
        return value


async def _bounded_body(request: Request, limit: int, model: type[BaseModel]) -> Any:
    declared = request.headers.get("content-length")
    if declared:
        try:
            size = int(declared)
            if size < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(422, "Content-Length 不合法") from exc
        if size > limit:
            raise HTTPException(413, "前端设计请求过大，请精简样式后保存")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, "前端设计请求过大，请精简样式后保存")
        data.extend(chunk)

    def reject_constant(value: str) -> None:
        raise ValueError("JSON 数值必须有限")
    try:
        parsed = json.loads(data, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(422, "前端设计请求必须是有效 JSON") from exc
    try:
        return model.model_validate(parsed)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_input=False, include_context=False)) from exc


async def preview_body(request: Request) -> PreviewBody:
    return await _bounded_body(request, MAX_PREVIEW_BODY, PreviewBody)


async def styles_body(request: Request) -> StylesBody:
    return await _bounded_body(request, MAX_STYLES_BODY, StylesBody)


def mount_frontend_design_api(app: FastAPI, *, service: Any, intel: Any = None, design: FrontendDesignService | None = None) -> FrontendDesignService:
    design = design or FrontendDesignService(intel=intel)

    def owned(user: CurrentUser, project_id: str) -> dict:
        try:
            return service.get_owned(user.user_id, project_id)
        except KeyError as exc:
            raise HTTPException(404, "项目不存在或无权访问") from exc

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, DesignError):
            return HTTPException(exc.status, str(exc))
        if isinstance(exc, (ValueError, FileNotFoundError)):
            return HTTPException(422, str(exc))
        return HTTPException(500, "前端设计保存失败，请保留当前编辑后重试")

    @app.get("/api/v1/code/projects/{project_id}/frontend-design")
    async def discover(project_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        project = owned(user, project_id)
        try:
            return await asyncio.to_thread(design.discover, project)
        except Exception as exc:
            raise error(exc) from exc

    @app.post("/api/v1/code/projects/{project_id}/frontend-design/preview", openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": PreviewBody.model_json_schema()}}}})
    async def preview(project_id: str, request: Request, body: PreviewBody = Depends(preview_body), user: CurrentUser = Depends(get_current_user)) -> dict:
        project = owned(user, project_id)
        try:
            return await design.preview(project, entry_path=body.entry_path, url=body.url, api_url=body.api_url, api_base=str(request.base_url), blocked_ports={8765, 5175, request.url.port or 80})
        except Exception as exc:
            raise error(exc) from exc

    @app.put("/api/v1/code/projects/{project_id}/frontend-design/styles", openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": StylesBody.model_json_schema()}}}})
    async def styles(project_id: str, body: StylesBody = Depends(styles_body), user: CurrentUser = Depends(get_current_user)) -> dict:
        project = owned(user, project_id)
        try:
            # Once explicit saving starts, a disconnected caller cannot cancel
            # between its CSS write and entry link commit/rollback.
            operation = asyncio.create_task(asyncio.to_thread(design.save_styles, project, **body.model_dump()))
            operation.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            return await asyncio.shield(operation)
        except Exception as exc:
            raise error(exc) from exc

    @app.get("/api/v1/code/frontend-design-assets/{token}/{path:path}", operation_id="get_frontend_design_asset")
    @app.head("/api/v1/code/frontend-design-assets/{token}/{path:path}", operation_id="head_frontend_design_asset")
    async def asset(token: str, path: str, request: Request) -> Response:
        try:
            ticket = design.ticket(token)
            # Tickets remain bound to the originally owned project/root; project
            # deletion, reassignment or moving it invalidates the capability.
            project = service.get_owned(ticket.user_id, ticket.project_id)
            from pathlib import Path
            if Path(project["root_path"]).resolve() != ticket.root:
                raise DesignError("预览项目已变更，请刷新预览", 404)
            data, media_type = await design.asset(ticket, path, request.url.query, accept=request.headers.get("accept", "*/*"))
            return Response(content=b"" if request.method == "HEAD" else data, media_type=media_type, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Content-Length": str(len(data)), "Content-Security-Policy": preview_policy(ticket)})
        except KeyError as exc:
            raise HTTPException(404, "预览项目不可用") from exc
        except Exception as exc:
            raise error(exc) from exc

    @app.get("/api/v1/code/frontend-design-api/{token}/{path:path}", operation_id="get_frontend_design_project_api")
    @app.head("/api/v1/code/frontend-design-api/{token}/{path:path}", operation_id="head_frontend_design_project_api")
    @app.options("/api/v1/code/frontend-design-api/{token}/{path:path}", operation_id="options_frontend_design_project_api")
    async def project_api(token: str, path: str, request: Request) -> Response:
        headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'"}
        try:
            ticket = design.ticket(token)
            project = service.get_owned(ticket.user_id, ticket.project_id)
            from pathlib import Path
            if Path(project["root_path"]).resolve() != ticket.root:
                raise DesignError("预览项目已变更，请刷新预览", 404)
            if not ticket.api_target:
                raise DesignError("尚未配置项目后端接口，请填写接口地址后重新载入", 404)
            if request.method == "OPTIONS":
                from code_agent.codeintel.frontend_design import _api_parts, _api_query
                _api_parts(path)
                _api_query(request.url.query)
                requested_method = request.headers.get("access-control-request-method", "GET").upper()
                requested_headers = {value.strip().lower() for value in request.headers.get("access-control-request-headers", "").split(",") if value.strip()}
                if requested_method not in {"GET", "HEAD"} or not requested_headers <= {"accept", "content-type"}:
                    raise DesignError("项目接口预览只允许无凭据的 GET/HEAD 请求", 405)
                headers.update({"Access-Control-Allow-Methods": "GET, HEAD", "Access-Control-Allow-Headers": "Accept, Content-Type"})
                return Response(status_code=204, headers=headers)
            data, media_type, status = await design.project_api(ticket, path, request.url.query, method=request.method, accept=request.headers.get("accept", "*/*"))
            return Response(content=b"" if request.method == "HEAD" else data, status_code=status, media_type=media_type, headers=headers)
        except KeyError as exc:
            return JSONResponse({"detail": "预览项目不可用"}, status_code=404, headers=headers)
        except Exception as exc:
            failure = error(exc)
            return JSONResponse({"detail": failure.detail}, status_code=failure.status_code, headers=headers)

    return design
