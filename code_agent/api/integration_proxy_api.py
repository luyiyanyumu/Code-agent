"""Authenticated request tools plus session-scoped development API capability."""
from __future__ import annotations

import json
from urllib.parse import unquote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.codeintel.frontend_design import DesignError
from code_agent.codeintel.integration_proxy import IntegrationProxyError, MAX_REQUEST, METHODS


async def _body(request, *, json_body=False):
    output = bytearray()
    async for chunk in request.stream():
        if len(output) + len(chunk) > MAX_REQUEST + (16384 if json_body else 0):
            raise HTTPException(413, "联调请求体过大")
        output.extend(chunk)
    if not json_body:
        return bytes(output)
    try:
        value = json.loads(output or b"{}")
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(422, "联调请求必须是 JSON 对象") from exc


def _error(exc):
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (IntegrationProxyError, DesignError)):
        return HTTPException(exc.status, str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(404, "项目或联调会话不存在")
    if isinstance(exc, PermissionError):
        return HTTPException(403, "无权访问此联调会话")
    if type(exc).__name__ == "RuntimeBusy":
        return HTTPException(409, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, str(exc))
    return HTTPException(500, "联调请求处理失败")


def mount_integration_proxy_api(app: FastAPI, proxy):
    prefix = "/api/v1/code/projects/{project_id}/integration"

    def platform_ports(request):
        server = request.scope.get("server")
        proxy.blocked_ports.update({request.url.port or 80})
        if server:
            proxy.blocked_ports.add(server[1])

    @app.post(prefix + "/preview")
    async def preview(project_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
        platform_ports(request)
        payload = await _body(request, json_body=True)
        try:
            return await proxy.preview(project_id, user.user_id, api_base=str(request.base_url), entry_path=str(payload.get("entry_path", "")))
        except Exception as exc:
            raise _error(exc) from exc

    @app.post(prefix + "/request")
    async def send_request(project_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
        platform_ports(request)
        payload = await _body(request, json_body=True)
        try:
            return await proxy.request(project_id, user.user_id, payload)
        except Exception as exc:
            raise _error(exc) from exc

    @app.get(prefix + "/requests")
    async def requests(project_id: str, user: CurrentUser = Depends(get_current_user)):
        try:
            return await proxy.requests(project_id, user.user_id)
        except Exception as exc:
            raise _error(exc) from exc

    @app.get(prefix + "/requests/{request_id}")
    async def detail(project_id: str, request_id: str, user: CurrentUser = Depends(get_current_user)):
        try:
            return await proxy.detail(project_id, user.user_id, request_id)
        except Exception as exc:
            raise _error(exc) from exc

    @app.api_route("/api/v1/code/integration-api/{token}/{path:path}", methods=sorted(METHODS))
    async def project_api(token: str, path: str, request: Request):
        cors = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'"}
        try:
            await proxy.capability(token)
            if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
                verb = request.headers["access-control-request-method"].upper()
                if verb not in METHODS:
                    raise IntegrationProxyError("联调接口不支持该请求方法", 405)
                requested = request.headers.get("access-control-request-headers", "")
                if len(requested) > 2048 or any(char not in "!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz, " for char in requested):
                    raise IntegrationProxyError("联调预检请求头不合法")
                cors.update({"Access-Control-Allow-Methods": ", ".join(sorted(METHODS)), "Access-Control-Allow-Headers": requested or "Content-Type, Authorization"})
                return Response(status_code=204, headers=cors)
            # Deliberately omit outer Cookie and Authorization. A project's
            # explicit headers are sent in a namespaced envelope by the bridge.
            forwarded = {key: value for key, value in request.headers.items() if key.lower() in {"accept", "content-type"}}
            envelope = request.headers.get("x-code-agent-project-headers")
            if envelope:
                if len(envelope) > 16384:
                    raise IntegrationProxyError("项目请求头过大")
                try:
                    explicit = json.loads(unquote(envelope, errors="strict"))
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise IntegrationProxyError("项目请求头不合法") from exc
                if not isinstance(explicit, dict):
                    raise IntegrationProxyError("项目请求头不合法")
                forwarded.update(explicit)
            result = await proxy.project_api(token, path, request.url.query, method=request.method, headers=forwarded, body=await _body(request))
            cors["X-Code-Agent-Request-Id"] = result["id"]
            cors["Access-Control-Expose-Headers"] = "X-Code-Agent-Request-Id"
            if result["error_kind"] in {"connection", "timeout", "redirect", "size"}:
                code = {"connection": 502, "timeout": 504, "redirect": 502, "size": 413}[result["error_kind"]]
                return JSONResponse({"detail": result["error"]}, status_code=code, headers=cors)
            # Preserve application pagination/auth/trace headers. The browser
            # gets no Set-Cookie: only this session's private jar handles it.
            response_headers = {key: value for key, value in result["headers"].items()
                                if key.lower() not in {"set-cookie", "location", "content-encoding"}
                                and not key.lower().startswith("access-control-")}
            response_headers.update(cors)
            response_headers["Access-Control-Expose-Headers"] = ", ".join(response_headers)
            return Response(content=b"" if request.method == "HEAD" else result["_body_bytes"], status_code=result["status_code"], headers=response_headers)
        except Exception as exc:
            error = _error(exc)
            return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=cors)

    return proxy
