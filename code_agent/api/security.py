"""API 认证与安全原语。

- 密码：pbkdf2_hmac(SHA256, 200k 轮)，存 "salt$hash"（标准库，无编译依赖）
- Token：PyJWT HS256，payload 带 sub(user_id)/username/role，12h 过期
- Secret：优先 CODE_AGENT_SECRET 环境变量；否则持久化随机密钥到数据目录
  （重启后 token 不失效；生产环境务必用环境变量统一管理）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from code_agent.config import agent_home

PBKDF2_ITERATIONS = 200_000
TOKEN_EXPIRE_HOURS = 12
ALGORITHM = "HS256"


class AuthError(Exception):
    """认证失败（映射 401）。"""


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, digest_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def get_secret() -> str:
    secret = os.environ.get("CODE_AGENT_SECRET")
    if secret:
        return secret
    key_file = agent_home() / "secret.key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    key_file.write_text(key, encoding="utf-8")
    return key


def create_token(user_id: str, username: str, role: str, secret: str | None = None) -> str:
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, secret or get_secret(), algorithm=ALGORITHM)


def decode_token(token: str, secret: str | None = None) -> dict:
    try:
        return jwt.decode(token, secret or get_secret(), algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token 已过期") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("token 无效") from exc
