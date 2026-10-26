"""HTTP and WebSocket interface for the independent conversation service."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field

from .config import Config
from .models import Models
from .runtime import BusyError, PendingNotFound, RuntimeManager
from .storage import Store

PERMISSIONS = [
    {"id": "read-only", "label": "逐次审批", "desc": "读取会话文件；生成文件前申请本次授权"},
    {"id": "workspace-write", "label": "生成文件", "desc": "可读取附件、生成会话产物；所有文件操作限于本会话"},
]


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str = Field(default="", max_length=160)
    permission_mode: str | None = None


class Prompt(BaseModel):
    text: str = Field(default="", max_length=100_000)
    attachments: list[dict] = Field(default_factory=list, max_length=10)
    web: bool | None = None


class Upload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content_b64: str


class Name(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class Title(BaseModel):
    title: str = Field(min_length=1, max_length=160)


class Folder(BaseModel):
    folder: str = Field(default="", max_length=160)


class Mode(BaseModel):
    mode: str


class Enabled(BaseModel):
    enabled: bool


class Decision(BaseModel):
    approved: bool


class Rewind(BaseModel):
    checkpoint_id: str


def create_app(config: Config | None = None, *, static_dir: Path | None = None) -> FastAPI:
    config = config or Config.from_env()
    config.ensure_dirs()
    store = Store(config)
    models = Models(config, store)
    manager = RuntimeManager(config, store, models)
    access_token = config.access_token
    user_id = "local"
    origins = {"http://127.0.0.1:5174", "http://localhost:5174", "http://127.0.0.1:4174", "http://localhost:4174"}
    origins.update(x.strip().rstrip("/") for x in os.environ.get("CHAT_AGENT_ALLOWED_ORIGINS", "").split(",") if x.strip())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await manager.shutdown()
        store.close()

    app = FastAPI(title="Chat-agent", version="0.1.0", lifespan=lifespan)
    if not access_token:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.state.config, app.state.store, app.state.models, app.state.manager = config, store, models, manager

    def authorized(value: str) -> bool:
        return not access_token or hmac.compare_digest(value.encode("utf-8"), access_token.encode("utf-8"))

    def origin_allowed(origin: str, host: str, scheme: str) -> bool:
        return not origin or origin.rstrip("/") in origins or origin.rstrip("/") == f"{scheme}://{host}"

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            header = request.headers.get("authorization", "")
            token = header[7:] if header.startswith("Bearer ") else ""
            if not authorized(token):
                return JSONResponse({"detail": "需要此对话服务的访问令牌"}, status_code=401)
            if not origin_allowed(request.headers.get("origin", ""), request.headers.get("host", ""), request.url.scheme):
                return JSONResponse({"detail": "来源未获授权"}, status_code=403)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "无效的请求长度"}, status_code=400)
            # Bound both chunked and regular bodies before JSON/base64 parsing.
            limit = int(config.max_upload_bytes * 4 / 3) + 64_000
            if length < 0 or length > limit:
                return JSONResponse({"detail": "请求超过上传大小上限"}, status_code=413)
            if request.method in {"POST", "PUT", "PATCH"}:
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        return JSONResponse({"detail": "请求超过上传大小上限"}, status_code=413)
                    chunks.append(chunk)
                request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "会话或记录不存在"}, status_code=404)

    @app.exception_handler(BusyError)
    async def busy_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(PendingNotFound)
    async def pending_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def session(sid: str, *, idle=False, active=False):
        row = store.get_session(sid, user_id)
        if active and row["status"] != "active":
            raise HTTPException(409, "会话已归档")
        if idle and manager.busy(sid):
            raise HTTPException(409, "会话仍在执行，请先停止")
        return row

    def permission(mode: str):
        if mode not in {x["id"] for x in PERMISSIONS}:
            raise ValueError("无效的对话权限模式")
        return mode

    def check_folder(folder: str):
        folder = folder.strip()
        if folder and not any(w["name"] == folder for w in store.list_workspaces(user_id)):
            raise ValueError("请先创建会话分组")
        return folder

    @app.get("/healthz")
    def health():
        return {"ok": True, "service": "chat-agent", "version": "0.1.0"}

    @app.get("/api/v1/auth/me")
    def me():
        return {"id": user_id, "username": "本地用户", "role": "admin"}

    @app.get("/api/v1/sessions")
    def sessions():
        return store.list_sessions(user_id, include_archived=True)

    @app.post("/api/v1/sessions")
    def create(body: NewSession):
        return store.create_session(user_id, check_folder(body.folder), permission(body.permission_mode or config.default_permission))

    @app.get("/api/v1/sessions/{sid}")
    async def get(sid: str):
        session(sid)
        manager.recover_session(sid)
        return {**session(sid), **manager.meta(sid)}

    @app.post("/api/v1/sessions/{sid}/rename")
    def rename(sid: str, body: Title):
        session(sid)
        if not body.title.strip():
            raise ValueError("名称不能为空")
        return store.update_session(sid, title=body.title.strip())

    @app.post("/api/v1/sessions/{sid}/folder")
    def move(sid: str, body: Folder):
        session(sid)
        return store.update_session(sid, folder=check_folder(body.folder))

    @app.post("/api/v1/sessions/{sid}/permission")
    async def set_permission(sid: str, body: Mode):
        session(sid, idle=True)
        return store.update_session(sid, permission_mode=permission(body.mode))

    @app.post("/api/v1/sessions/{sid}/archive")
    async def archive(sid: str):
        session(sid, idle=True)
        return store.update_session(sid, status="archived")

    @app.delete("/api/v1/sessions/{sid}")
    async def delete(sid: str):
        session(sid, idle=True)
        # Config validates the ID and constrains both paths to this service's home.
        for path in (config.session_dir(sid), config.artifact_dir(sid)):
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        store.delete_session(sid)
        return {"deleted": True, "id": sid}

    @app.post("/api/v1/sessions/{sid}/fork")
    async def fork(sid: str):
        session(sid, idle=True)
        result = store.fork_session(sid, user_id)
        # Only scoped regular files are copied; symlinks never follow into other workspaces.
        for source, target in ((config.session_dir(sid), config.session_dir(result["id"])), (config.artifact_dir(sid), config.artifact_dir(result["id"]))):
            for item in source.rglob("*") if source.exists() else []:
                if item.is_symlink() or not item.is_file() or any(p.is_symlink() for p in item.parents if p != source.parent):
                    continue
                dest = target / item.relative_to(source)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
        return result

    @app.get("/api/v1/workspaces")
    def workspaces():
        return store.list_workspaces(user_id)

    @app.post("/api/v1/workspaces")
    def workspace_create(body: Name):
        return store.create_workspace(body.name, user_id)

    @app.delete("/api/v1/workspaces/{name}")
    def workspace_delete(name: str):
        store.delete_workspace(name, user_id)
        return {"deleted": True, "name": name}

    @app.get("/api/v1/sessions/{sid}/events")
    async def events(sid: str, after_seq: int = 0):
        session(sid)
        manager.recover_session(sid)
        if after_seq < 0:
            raise ValueError("无效的事件序号")
        return store.events(sid, after_seq)

    @app.post("/api/v1/sessions/{sid}/prompt")
    async def prompt(sid: str, body: Prompt):
        session(sid, active=True)
        return manager.start(sid, user_id, body.text, body.attachments, body.web)

    @app.post("/api/v1/sessions/{sid}/cancel")
    async def cancel(sid: str):
        session(sid)
        return manager.cancel(sid, user_id)

    @app.get("/api/v1/sessions/{sid}/checkpoints")
    def checkpoints(sid: str):
        session(sid)
        return {"checkpoints": store.checkpoints(sid), "busy": manager.busy(sid)}

    @app.post("/api/v1/sessions/{sid}/rewind")
    async def rewind(sid: str, body: Rewind):
        session(sid, idle=True)
        report = store.rewind(sid, body.checkpoint_id)
        manager.recover_session(sid, force=True)
        manager.emit_event(sid, "assistant_message", {"text": "已回退到所选消息之前。此前生成的文件仍保留。", "rewind": True})
        return report

    @app.get("/api/v1/sessions/{sid}/acceptance")
    def acceptance(sid: str):
        session(sid)
        return {"declared": False}

    @app.get("/api/v1/sessions/{sid}/context")
    def context(sid: str):
        session(sid)
        history = store.events(sid)
        return {"events": len(history), "messages": sum(e["type"] in {"user_message", "assistant_message"} for e in history), "model": models.public_settings()["active_model"], "window": 0, "note": "历史对话随请求发送；模型上下文容量由服务商决定"}

    @app.post("/api/v1/sessions/{sid}/files")
    async def upload(sid: str, body: Upload):
        session(sid, idle=True, active=True)
        name = body.name.strip()
        if name in {".", ".."} or any(c in name for c in ('/', '\\', ':', '\x00')) or name.endswith((".", " ")) or any(ord(c) < 32 for c in name):
            raise ValueError("请使用普通文件名")
        if len(body.content_b64) > (config.max_upload_bytes + 2) // 3 * 4:
            raise HTTPException(413, "附件超过大小上限")
        try:
            content = base64.b64decode(body.content_b64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("附件不是有效的 Base64 数据") from None
        if len(content) > config.max_upload_bytes:
            raise HTTPException(413, "附件超过大小上限")
        relative = f"uploads/{uuid.uuid4().hex[:12]}-{name}"
        root = config.session_dir(sid)
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or path.parent.is_symlink():
            raise ValueError("附件路径越界")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"name": name, "path": relative, "size": len(content)}

    @app.get("/api/v1/sessions/{sid}/artifacts/{filename:path}")
    def artifact(sid: str, filename: str):
        session(sid)
        root = config.artifact_dir(sid).resolve()
        path = root / filename
        if not filename or '\\' in filename or ':' in filename or not path.resolve().is_relative_to(root) or path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
            raise HTTPException(404, "文件不存在")
        if not path.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.get("/api/v1/sessions/{sid}/approvals/pending")
    async def pending(sid: str):
        session(sid)
        manager.recover_session(sid)
        return manager.pending_approval(sid)

    @app.post("/api/v1/approvals/{aid}/decide")
    async def decide(aid: str, body: Decision):
        return manager.decide_approval(aid, user_id, body.approved)

    @app.post("/api/v1/sessions/{sid}/questions/{qid}/answer")
    async def answer(sid: str, qid: str, body: dict):
        session(sid)
        return manager.answer_question(sid, qid, user_id, body)

    @app.get("/api/v1/settings/permission")
    def permissions():
        return {"mode": config.default_permission, "modes": PERMISSIONS}

    @app.get("/api/v1/web")
    def web():
        return {"available": False, "reason": "当前独立对话后端尚未提供网络检索工具", "how_to": "可上传资料供模型阅读"}

    @app.post("/api/v1/sessions/{sid}/web")
    def session_web(sid: str, body: Enabled):
        session(sid)
        if body.enabled:
            raise ValueError("当前后端尚未提供联网检索")
        return store.update_session(sid, web_enabled=False)

    @app.get("/api/v1/tools")
    def tools():
        from .tools import tool_schemas
        return {"tools": tool_schemas(), "packs": [], "plugins": [], "mcp": []}

    @app.get("/api/v1/settings/model")
    def model_settings():
        return models.public_settings()

    @app.get("/api/v1/model-profiles")
    def profiles():
        return models.profiles()

    @app.post("/api/v1/model-profiles")
    def profile_save(body: dict):
        return models.save(body)

    @app.post("/api/v1/model-profiles/{pid}/activate")
    def activate(pid: str):
        return models.activate(pid)

    @app.delete("/api/v1/model-profiles/{pid}")
    def profile_delete(pid: str):
        models.delete(pid)
        return {"deleted": True, "id": pid}

    @app.websocket("/api/v1/sessions/{sid}/stream")
    async def stream(ws: WebSocket, sid: str):
        if not authorized(ws.query_params.get("token", "")) or not origin_allowed(ws.headers.get("origin", ""), ws.headers.get("host", ""), "https" if ws.url.scheme == "wss" else "http"):
            await ws.close(code=4401)
            return
        try:
            session(sid, active=True)
            after = int(ws.query_params.get("after_seq", "0"))
            if after < 0:
                raise ValueError("无效的事件序号")
        except KeyError:
            await ws.close(code=4404)
            return
        except (ValueError, HTTPException):
            await ws.close(code=4409)
            return
        await ws.accept()
        manager.recover_session(sid)
        queue = manager.subscribe(sid)
        history = store.events(sid, after)
        last_sent = after
        try:
            for event in history:
                await ws.send_json(event)
                last_sent = event["seq"]
            await ws.send_json({"seq": 0, "type": "session_meta", "ts": 0, "payload": manager.meta(sid)})

            async def sender():
                nonlocal last_sent
                while True:
                    event = await queue.get()
                    if event.get("type") == "stream_reset":
                        await ws.close(code=1013, reason="replay required")
                        return
                    if event.get("seq", 0) and event["seq"] <= last_sent:
                        continue
                    if event.get("seq", 0):
                        last_sent = event["seq"]
                    await ws.send_json(event)

            async def receiver():
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=90)
                        if not isinstance(msg, dict):
                            raise ValueError("会话消息必须是 JSON 对象")
                        kind = msg.get("type")
                        if kind == "prompt":
                            body = Prompt.model_validate(msg)
                            manager.start(sid, user_id, body.text, body.attachments, body.web)
                        elif kind == "cancel":
                            manager.cancel(sid, user_id)
                        elif kind == "ping":
                            await queue.put({"seq": 0, "type": "session_meta", "ts": 0, "payload": {**manager.meta(sid), "pong": True}})
                        else:
                            raise ValueError("未知的会话消息类型")
                    except (ValueError, KeyError) as exc:
                        await queue.put({"seq": 0, "type": "error", "ts": 0, "payload": {"code": "busy" if isinstance(exc, BusyError) else "invalid_request", "message": str(exc)}})

            tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, RuntimeError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            manager.unsubscribe(sid, queue)

    if static_dir is not None and (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
    return app
