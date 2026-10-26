"""Configuration for the standalone chat server.

The server reads only its own CHAT_AGENT_* namespace. It never discovers the
coding application's .env files, credentials, database, or migration state.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError("Invalid session identifier")
    return value


@dataclass
class Config:
    home: Path
    provider: str = "offline"
    api_base: str = ""
    api_key: str = field(default="", repr=False)
    model: str = ""
    access_token: str = field(default="", repr=False)
    max_upload_bytes: int = 10 * 1024 * 1024
    max_rounds: int = 8
    request_timeout: float = 90
    default_permission: str = "workspace-write"
    host: str = "127.0.0.1"
    port: int = 8766

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser().resolve()
        if not 1 <= self.port <= 65535:
            raise ValueError("CHAT_AGENT_PORT must be between 1 and 65535")
        if self.max_upload_bytes < 1 or self.max_rounds < 1 or self.request_timeout <= 0:
            raise ValueError("Upload, round, and timeout limits must be positive")
        if self.default_permission not in {"read-only", "workspace-write"}:
            raise ValueError("CHAT_AGENT_DEFAULT_PERMISSION must be read-only or workspace-write")

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        return cls(
            home=Path(env.get("CHAT_AGENT_HOME") or str(Path.home() / ".chat-agent")),
            provider=env.get("CHAT_AGENT_PROVIDER") or "offline",
            api_base=env.get("CHAT_AGENT_API_BASE", ""),
            api_key=env.get("CHAT_AGENT_API_KEY", ""),
            model=env.get("CHAT_AGENT_MODEL", ""),
            access_token=env.get("CHAT_AGENT_ACCESS_TOKEN", ""),
            max_upload_bytes=int(env.get("CHAT_AGENT_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
            max_rounds=int(env.get("CHAT_AGENT_MAX_ROUNDS", "8")),
            request_timeout=float(env.get("CHAT_AGENT_REQUEST_TIMEOUT", "90")),
            default_permission=env.get("CHAT_AGENT_DEFAULT_PERMISSION", "workspace-write"),
            host=env.get("CHAT_AGENT_HOST", "127.0.0.1"),
            port=int(env.get("CHAT_AGENT_PORT", "8766")),
        )

    @property
    def db_path(self) -> Path:
        return self.home / "chat.sqlite3"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        for name in ("workspaces", "artifacts"):
            base = self.home / name
            if base.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(base):
                raise ValueError("Session storage directories cannot be filesystem links")
            base.mkdir(exist_ok=True)

    def _scoped_dir(self, kind: str, sid: str) -> Path:
        base = self.home / kind
        path = base / validate_id(sid)
        for candidate in (base, path):
            if candidate.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(candidate):
                raise ValueError("Session storage directories cannot be filesystem links")
        if not path.resolve().is_relative_to(base) or not base.resolve().is_relative_to(self.home):
            raise ValueError("Session storage path escapes the data directory")
        return path

    def session_dir(self, sid: str) -> Path:
        return self._scoped_dir("workspaces", sid)

    def artifact_dir(self, sid: str) -> Path:
        return self._scoped_dir("artifacts", sid)
