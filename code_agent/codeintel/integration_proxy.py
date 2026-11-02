"""Session-bound development preview and project HTTP requests.

The existing design preview remains read-only. These capabilities belong to an
explicitly started integration session and never inherit Code-agent credentials.
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import inspect
import ipaddress
import json
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import httpx

from code_agent.codeintel.frontend_design import FrontendDesignService, MAX_TEXT, rewrite_html

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
MAX_HISTORY = 200
MAX_HISTORY_BYTES = 16 * 1024 * 1024
MAX_TOKENS = 64
TOKEN_TTL = 15 * 60
API_ROUTE = "/api/v1/code/integration-api/"
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_SENSITIVE = re.compile(r"authorization|cookie|password|passwd|credential|secret|token|api.?key", re.I)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOP_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "upgrade", "proxy-authorization", "proxy-connection", "te", "trailer", "keep-alive"}


class IntegrationProxyError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _path(value: str, *, query: bool = True) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value) or "\\" in value:
        raise IntegrationProxyError("接口路径不合法")
    if value.startswith("//") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) or "#" in value:
        raise IntegrationProxyError("请填写相对后端地址的接口路径")
    raw, separator, parameters = value.partition("?")
    if separator and not query:
        raise IntegrationProxyError("服务地址不能包含查询参数")
    decoded = raw
    try:
        for _ in range(5):
            next_value = unquote(decoded, errors="strict")
            if next_value == decoded:
                break
            decoded = next_value
    except (UnicodeError, ValueError) as exc:
        raise IntegrationProxyError("接口路径编码不合法") from exc
    if "%" in decoded or "\\" in decoded or any(ord(c) < 32 for c in decoded):
        raise IntegrationProxyError("接口路径编码不合法")
    if any(part in {".", ".."} for part in decoded.split("/")) or "//" in decoded or ":" in decoded:
        raise IntegrationProxyError("接口路径不能越过选定的后端地址")
    return quote(decoded.lstrip("/"), safe="/!$&'()*+,;=@-._~"), parameters


def _origin_url(value: str, blocked_ports: set[int], *, base: bool = True) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None or (base and parsed.query) or parsed.fragment:
            raise ValueError()
        host = parsed.hostname or ""
        address = ipaddress.ip_address("127.0.0.1" if host.lower() == "localhost" else host)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not address.is_loopback or port == 0 or port in blocked_ports:
            raise ValueError()
        path, _ = _path(parsed.path, query=False)
        authority = f"[{address}]" if address.version == 6 else str(address)
        normalized_path = "/" + path.rstrip("/") + "/" if base and path else "/" + path if path else "/"
        return urlunsplit((parsed.scheme, f"{authority}:{port}", normalized_path, parsed.query if not base else "", ""))
    except (ValueError, TypeError) as exc:
        raise IntegrationProxyError("联调地址必须是当前平台之外、无凭据的本机 HTTP(S) 服务；后端根地址不能包含查询参数") from exc


def _headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 64:
        raise IntegrationProxyError("请求头需为不超过 64 项的对象")
    output = {}
    total = 0
    for key, item in value.items():
        if not isinstance(key, str) or not _HEADER_NAME.fullmatch(key) or not isinstance(item, str) or any(ord(c) < 32 or ord(c) == 127 for c in item):
            raise IntegrationProxyError("请求头包含无效名称或控制字符")
        total += len(key.encode()) + len(item.encode())
        if total > 16 * 1024 or len(item) > 8192:
            raise IntegrationProxyError("请求头超过大小限制")
        if key.lower() not in _HOP_HEADERS:
            output[key] = item
    return output


def _redact_object(value):
    if isinstance(value, dict):
        return {key: "[已隐藏]" if _SENSITIVE.search(str(key)) else _redact_object(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_object(item) for item in value]
    return value


def _redact_text(value: str, content_type: str = "") -> str:
    try:
        if "json" in content_type or value.lstrip().startswith(("{", "[")):
            return json.dumps(_redact_object(json.loads(value)), ensure_ascii=False)
        if "x-www-form-urlencoded" in content_type:
            return urlencode([(key, "[已隐藏]" if _SENSITIVE.search(key) else item) for key, item in parse_qsl(value, keep_blank_values=True)])
    except (ValueError, RecursionError):
        pass
    return re.sub(r'(?i)(\b(?:password|passwd|token|api[_-]?key|secret|authorization|cookie)\b["\s]*[:=]\s*)(["\'])(.*?)\2', r'\1"[已隐藏]"', value)


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    parameters = urlencode([(key, "[已隐藏]" if _SENSITIVE.search(key) else item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parameters, ""))


@dataclass
class _Capability:
    token: str
    project_id: str
    user_id: str
    session_id: str
    frontend_url: str
    backend_url: str
    api_prefix: str
    strip_api_prefix: bool
    expires: float


class IntegrationProxy:
    def __init__(self, runtime, *, design_service=None, project_repo=None, transport=None, blocked_ports=None, clock=time.monotonic):
        self.runtime = runtime
        self.design = design_service or FrontendDesignService(transport=transport)
        self.projects = project_repo
        self.transport = transport
        self.blocked_ports = set(blocked_ports or ()) | {8765, 5175}
        self.clock = clock
        self._tokens: OrderedDict[str, _Capability] = OrderedDict()
        self._cookies: OrderedDict[tuple[str, str, str], httpx.Cookies] = OrderedDict()
        self._history: OrderedDict[str, tuple[str, str, dict, int]] = OrderedDict()
        self._history_bytes = 0
        self._guard = threading.RLock()
        self._slots = asyncio.Semaphore(8)

    async def _invoke(self, function, *args):
        if inspect.iscoroutinefunction(function):
            return await function(*args)
        result = await asyncio.to_thread(function, *args)
        return await result if inspect.isawaitable(result) else result

    async def owned_project(self, project_id, user_id):
        if self.projects is not None:
            return await self._invoke(self.projects.get_owned, user_id, project_id)
        return await self._invoke(self.runtime.owned_project, project_id, user_id)

    async def _context(self, project_id, user_id, session_id=None):
        context = await self._invoke(self.runtime.context, project_id, user_id, session_id)
        if context.get("status") not in {"starting", "running", "degraded"} or not context.get("session_id"):
            raise IntegrationProxyError("请先启动前后端联调", 409)
        if session_id is not None and context["session_id"] != session_id:
            raise IntegrationProxyError("联调会话已停止或更换，请重新载入", 410)
        return context

    def _targets(self, config):
        frontend = _origin_url(config.get("frontend_url", ""), self.blocked_ports, base=False)
        backend = _origin_url(config.get("backend_url", ""), self.blocked_ports)
        prefix, _ = _path(config.get("api_prefix", "/api"), query=False)
        return frontend, backend, "/" + prefix.rstrip("/"), bool(config.get("strip_api_prefix", False))

    def _require_service(self, context, role):
        service = context.get("services", {}).get(role)
        # A failed health probe is useful debugging evidence, not a revoked
        # session. Let the bounded request report the backend's actual error;
        # explicit start/stop/failure states still prevent dispatch.
        if service is not None and service.get("status") not in {"running", "ready", "attached", "healthy", "unhealthy"}:
            raise IntegrationProxyError(("前端" if role == "frontend" else "后端") + "服务尚未就绪或已停止", 409)

    async def preview(self, project_id, user_id, *, api_base, entry_path=""):
        context = await self._context(project_id, user_id)
        self._require_service(context, "frontend")
        project = await self.owned_project(project_id, user_id)
        frontend, backend, prefix, strip = self._targets(context["config"])
        data, media_type = await self.design._fetch(frontend)
        if media_type not in {"text/html", "application/xhtml+xml"} or len(data) > MAX_TEXT:
            raise IntegrationProxyError("前端地址未返回可预览的 HTML 页面")
        await self._context(project_id, user_id, context["session_id"])
        # Live asset tickets do not read this virtual path; Next and other
        # server-rendered applications need no index.html on disk.
        ticket = self.design._new_ticket(project, Path(project["root_path"]).resolve(), entry_path or "index.html", api_base, frontend)
        capability = _Capability(secrets.token_urlsafe(32), project_id, user_id, context["session_id"], frontend, backend, prefix, strip, self.clock() + TOKEN_TTL)
        with self._guard:
            self._tokens = OrderedDict((key, item) for key, item in self._tokens.items() if item.expires > self.clock())
            while len(self._tokens) >= MAX_TOKENS:
                self._tokens.popitem(last=False)
            self._tokens[capability.token] = capability
        api_proxy = api_base.rstrip("/") + API_ROUTE + capability.token + "/"
        html = rewrite_html(data.decode("utf-8-sig"), urlsplit(frontend).path.lstrip("/"), ticket)
        # Add the independent API capability to both HTTP/meta CSP. No old
        # read-only design capability receives write permissions.
        html = html.replace("connect-src " + ticket.prefix, "connect-src " + ticket.prefix + " " + api_proxy)
        page_path = urlsplit(frontend).path.lstrip("/")
        directory = page_path.rsplit("/", 1)[0] + "/" if "/" in page_path else ""
        return {"html": html, "base_url": ticket.prefix + directory, "asset_prefix": ticket.prefix, "api_proxy_url": api_proxy,
                "frontend_url": frontend, "backend_url": backend, "api_prefix": prefix, "strip_api_prefix": strip,
                "session_id": context["session_id"], "hmr": False}

    async def capability(self, token):
        with self._guard:
            item = self._tokens.get(token)
        if item is None or item.expires <= self.clock():
            raise IntegrationProxyError("联调预览已过期，请重新载入", 410)
        try:
            context = await self._context(item.project_id, item.user_id, item.session_id)
            self._require_service(context, "frontend")
            self._require_service(context, "backend")
        except (KeyError, ValueError, RuntimeError) as exc:
            raise IntegrationProxyError("联调会话已停止或更换，请重新载入", 410) from exc
        frontend, backend, prefix, strip = self._targets(context["config"])
        if (frontend, backend, prefix, strip) != (item.frontend_url, item.backend_url, item.api_prefix, item.strip_api_prefix):
            raise IntegrationProxyError("联调地址已变更，请重新载入", 410)
        return item

    def _remember(self, project_id, user_id, record):
        stored = {key: value for key, value in record.items() if not key.startswith("_")}
        stored["url"] = _redact_url(stored["url"])
        stored["path"] = _redact_url(stored["path"])
        for name in ("headers", "request_headers"):
            stored[name] = {key: "[已隐藏]" if _SENSITIVE.search(key) else value for key, value in stored.get(name, {}).items()}
        for key, value in stored["headers"].items():
            if key.lower() == "location":
                stored["headers"][key] = _redact_url(value)
        stored["request_body"] = _redact_text(stored.get("request_body", ""), record.get("_request_content_type", ""))[:128 * 1024]
        stored["body"] = _redact_text(stored.get("body", ""), stored.get("content_type", ""))[:128 * 1024]
        size = len(json.dumps(stored, ensure_ascii=False).encode())
        with self._guard:
            self._history[record["id"]] = (project_id, user_id, stored, size)
            self._history_bytes += size
            while len(self._history) > MAX_HISTORY or self._history_bytes > MAX_HISTORY_BYTES:
                _, (_, _, _, removed) = self._history.popitem(last=False)
                self._history_bytes -= removed

    async def requests(self, project_id, user_id):
        await self.owned_project(project_id, user_id)
        fields = ("id", "source", "method", "path", "url", "status_code", "duration_ms", "content_type", "truncated", "error", "session_id", "created_at")
        with self._guard:
            rows = [{key: record.get(key) for key in fields} for pid, uid, record, _ in reversed(self._history.values()) if (pid, uid) == (project_id, user_id)]
        return {"requests": rows}

    async def detail(self, project_id, user_id, request_id):
        await self.owned_project(project_id, user_id)
        with self._guard:
            item = self._history.get(request_id)
        if item is None or item[:2] != (project_id, user_id):
            raise IntegrationProxyError("接口请求记录不存在", 404)
        return dict(item[2])

    async def request(self, project_id, user_id, payload):
        if not isinstance(payload, dict):
            raise IntegrationProxyError("接口请求需为对象")
        context = await self._context(project_id, user_id, payload.get("session_id"))
        self._require_service(context, "backend")
        _, backend, _, _ = self._targets(context["config"])
        result = await self._dispatch(project_id, user_id, context["session_id"], backend, payload, "manual")
        return {key: value for key, value in result.items() if not key.startswith("_")}

    async def project_api(self, token, path, query="", *, method="GET", headers=None, body=b""):
        item = await self.capability(token)
        clean, _ = _path(path, query=False)
        if clean.startswith("__backend__/"):
            mapped = clean[len("__backend__/"):]
        else:
            frontend_path = "/" + clean
            if not (frontend_path == item.api_prefix or frontend_path.startswith(item.api_prefix.rstrip("/") + "/")):
                raise IntegrationProxyError("前端请求不属于已配置的接口前缀")
            mapped = frontend_path[len(item.api_prefix):].lstrip("/") if item.strip_api_prefix else clean
        result = await self._dispatch(item.project_id, item.user_id, item.session_id, item.backend_url,
                                      {"path": mapped + ("?" + query if query else ""), "method": method, "headers": headers or {}, "body": body}, "preview")
        return result

    async def _dispatch(self, project_id, user_id, session_id, backend, payload, source):
        if not isinstance(payload, dict):
            raise IntegrationProxyError("接口请求需为对象")
        verb = str(payload.get("method", "GET")).upper()
        if verb not in METHODS:
            raise IntegrationProxyError("不支持此 HTTP 请求方法")
        path, parameters = _path(payload.get("path", ""))
        base = urlsplit(backend)
        target = urlunsplit((base.scheme, base.netloc, base.path + path, parameters, ""))
        explicit_headers = _headers(payload.get("headers"))
        body = payload.get("body")
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
            if not any(key.lower() == "content-type" for key in explicit_headers):
                explicit_headers["Content-Type"] = "application/json"
        if body is None:
            content = b""
        elif isinstance(body, str):
            content = body.encode("utf-8")
        elif isinstance(body, bytes):
            content = body
        else:
            raise IntegrationProxyError("请求体需为文本或 JSON")
        if len(content) > MAX_REQUEST:
            raise IntegrationProxyError("接口请求体不能超过 1MB", 413)
        if verb in {"GET", "HEAD"} and content:
            raise IntegrationProxyError("GET/HEAD 请求不能携带请求体")
        try:
            timeout = float(payload.get("timeout_ms", 10000)) / 1000 if "timeout" not in payload else float(payload["timeout"])
        except (ValueError, TypeError) as exc:
            raise IntegrationProxyError("接口超时设置不合法") from exc
        if not 0.1 <= timeout <= 30:
            raise IntegrationProxyError("接口超时需在 0.1 到 30 秒之间")
        record = {"id": secrets.token_urlsafe(12), "source": source, "method": verb, "path": "/" + path + ("?" + parameters if parameters else ""), "url": target,
                  "status_code": 0, "headers": {}, "body": "", "content_type": "", "duration_ms": 0, "truncated": False, "error": "", "error_kind": "",
                  "session_id": session_id, "created_at": time.time(), "request_headers": explicit_headers, "request_body": content.decode("utf-8", errors="replace"),
                  "_request_content_type": next((value for key, value in explicit_headers.items() if key.lower() == "content-type"), ""), "_body_bytes": b""}
        key = (user_id, project_id, session_id)
        with self._guard:
            cookies = httpx.Cookies(self._cookies.get(key))
        started = time.perf_counter()
        async with self._slots:
            # Waiting for another request must not let a queued write outlive
            # the session or a role that was stopped in the meantime.
            active = await self._context(project_id, user_id, session_id)
            self._require_service(active, "backend")
            if source == "preview":
                self._require_service(active, "frontend")
            try:
                async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False, timeout=timeout, cookies=cookies) as client:
                    async with client.stream(verb, target, headers=httpx.Headers(explicit_headers, encoding="utf-8"), content=content) as response:
                        record["status_code"] = response.status_code
                        record["headers"] = {name: value for name, value in response.headers.items() if name.lower() not in _HOP_HEADERS}
                        record["content_type"] = response.headers.get("content-type", "application/octet-stream")
                        output = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            remaining = MAX_RESPONSE + 1 - len(output)
                            output.extend(chunk[:remaining])
                            if len(output) > MAX_RESPONSE:
                                record["truncated"] = True
                                break
                        data = bytes(output[:MAX_RESPONSE])
                        record["_body_bytes"] = data
                        textual = any(kind in record["content_type"].lower() for kind in ("text/", "json", "xml", "javascript", "x-www-form-urlencoded"))
                        record["body_encoding"] = "utf-8" if textual else "base64"
                        record["body"] = data.decode("utf-8", errors="replace") if textual else base64.b64encode(data).decode()
                        if 300 <= response.status_code < 400:
                            record.update(error="接口返回重定向，联调不会自动跟随", error_kind="redirect")
                        if record["truncated"]:
                            record.update(error="接口响应超过 2MB，已截断", error_kind="size")
                        changed_cookies = httpx.Cookies(response.cookies)
                        deleted_cookies = []
                        for cookie_header in response.headers.get_list("set-cookie"):
                            parsed_cookie = SimpleCookie()
                            try:
                                parsed_cookie.load(cookie_header)
                                for morsel in parsed_cookie.values():
                                    expired = bool(morsel["max-age"]) and int(morsel["max-age"]) <= 0
                                    if not morsel["max-age"] and morsel["expires"]:
                                        expired = parsedate_to_datetime(morsel["expires"]).timestamp() <= time.time()
                                    if expired:
                                        deleted_cookies.append((morsel.key, morsel["path"] or urlsplit(target).path.rsplit("/", 1)[0] or "/"))
                            except (ValueError, TypeError, CookieError):
                                continue
                    with self._guard:
                        current_cookies = httpx.Cookies(self._cookies.get(key))
                        for name, cookie_path in deleted_cookies:
                            for cookie in list(current_cookies.jar):
                                if cookie.name == name and cookie.path == cookie_path:
                                    current_cookies.delete(name, domain=cookie.domain, path=cookie.path)
                        current_cookies.update(changed_cookies)
                        while len(current_cookies) > 128 or sum(len(cookie.name) + len(cookie.value or "") for cookie in current_cookies.jar) > 64 * 1024:
                            oldest = next(iter(current_cookies.jar))
                            current_cookies.delete(oldest.name, domain=oldest.domain, path=oldest.path)
                        self._cookies[key] = current_cookies
                        self._cookies.move_to_end(key)
                        while len(self._cookies) > MAX_TOKENS:
                            self._cookies.popitem(last=False)
            except httpx.TimeoutException:
                record.update(error="项目后端接口请求超时", error_kind="timeout")
            except httpx.HTTPError:
                record.update(error="无法连接项目后端接口，请检查服务状态和地址", error_kind="connection")
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        self._remember(project_id, user_id, record)
        return record
