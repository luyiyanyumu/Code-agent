"""FastAPI 依赖注入：当前用户解析。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, WebSocket

from code_agent.api.security import AuthError, decode_token


@dataclass
class CurrentUser:
    user_id: str
    username: str
    role: str


def _user_from_payload(payload: dict) -> CurrentUser:
    return CurrentUser(
        user_id=payload.get("sub", ""),
        username=payload.get("username", ""),
        role=payload.get("role", "user"),
    )


def get_current_user(request: Request) -> CurrentUser:
    if not request.app.state.auth_enabled:
        return request.app.state.local_user
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    token = auth.removeprefix("Bearer ").strip()
    try:
        payload = decode_token(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return _user_from_payload(payload)


def get_ws_user(websocket: WebSocket) -> CurrentUser | None:
    """本地模式直接访问；认证模式通过 ?token= 查询参数验证。"""
    if not websocket.app.state.auth_enabled:
        return websocket.app.state.local_user
    token = websocket.query_params.get("token", "")
    if not token:
        return None
    try:
        return _user_from_payload(decode_token(token))
    except AuthError:
        return None
