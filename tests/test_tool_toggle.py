"""M21：工具一键启用/禁用（页面 + 后端）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry, apply_disabled


class Dummy(Tool):
    description_zh = ""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"dummy {name}"
        self.parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments, ctx):  # type: ignore[no-untyped-def]
        return f"{self.name} ok"


def _registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for n in names:
        reg.register(Dummy(n))
    return reg


# ---------------------------------------------------------------------------
# 设置持久化
# ---------------------------------------------------------------------------
def test_settings_roundtrip(agent_home, monkeypatch):
    from code_agent import settings

    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    assert settings.disabled_tools() == []
    assert settings.set_tool_enabled("bash", False) == ["bash"]
    assert settings.disabled_tools() == ["bash"]
    assert settings.set_tool_enabled("web_search", False) == ["bash", "web_search"]
    # 再启用一个：只去掉它，别的不受影响
    assert settings.set_tool_enabled("bash", True) == ["web_search"]
    # 重复禁用/启用幂等
    assert settings.set_tool_enabled("bash", True) == ["web_search"]
    # 批量：一键禁用全部
    assert settings.set_tools_enabled(["a", "b", "c"], False) == ["a", "b", "c", "web_search"]
    # 一键启用全部（空列表）＝清空
    assert settings.set_tools_enabled([], True) == []
    assert settings.disabled_tools() == []


def test_setting_unknown_tool_is_rejected_by_api(agent_home, monkeypatch):
    """拼错的工具名不能静默成功（否则用户以为禁用了，其实什么也没发生）。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    from fastapi.testclient import TestClient

    from code_agent.api.app import create_app

    client = TestClient(create_app())
    tok = _login(client)
    r = client.post(
        "/api/v1/tools/not_a_real_tool/enabled",
        json={"enabled": False},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 404, r.text


def _login(client) -> str:
    # 与其它用例一致：首次启动引导创建管理员
    r = client.post("/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin123"})
    if r.status_code >= 400:
        r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["token"]


# ---------------------------------------------------------------------------
# 注册表层：禁用 = 看不见 + 调不动
# ---------------------------------------------------------------------------
def test_apply_disabled_wraps_and_enforces(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    from code_agent import settings

    reg = _registry("bash", "read_file", "web_search")
    assert apply_disabled(reg) is reg, "没有禁用项时不该包一层代理"

    settings.set_tool_enabled("bash", False)
    wrapped = apply_disabled(reg)
    assert isinstance(wrapped, FilteredToolRegistry)
    assert wrapped.names() == ["read_file", "web_search"]
    assert wrapped.get("bash") is None, "禁用后不该再被找到"
    with pytest.raises(Exception):
        asyncio.run(wrapped.dispatch("bash", {}, ToolContext("s", Path("."))))
    # 没被禁的照常可用
    assert asyncio.run(wrapped.dispatch("read_file", {}, ToolContext("s", Path(".")))) == "read_file ok"


def test_session_loop_hides_disabled_tools(agent_home, monkeypatch):
    """会话侧：禁用的工具不出现在发给模型的 schema 里（联网开关同一套机制）。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    from code_agent import settings
    from code_agent.cli import build_registry
    from code_agent.sessions.manager import SessionRuntime

    monkeypatch.setenv("CODE_AGENT_PROVIDER", "offline")
    from code_agent.agent.router import ModelRouter
    from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
    from code_agent.storage.jsonl import TranscriptStore
    from code_agent.config import transcripts_dir

    db = Database(agent_home / "tools.db")
    users = UserRepo(db)
    uid = users.create("u_tools", "x", role="user")["id"]
    repo = SessionRepo(db)
    meta = repo.create(uid)
    rt = SessionRuntime(
        session_id=meta["id"],
        user_id=uid,
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=repo,
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
        workspace=Path(agent_home),
    )
    on = {s["name"] for s in rt._build_loop().registry.schemas()}
    assert "bash" in on

    settings.set_tool_enabled("bash", False)
    # 不重建运行时：下一轮 prompt 就生效（_build_loop 每轮重读禁用清单）
    off = {s["name"] for s in rt._build_loop().registry.schemas()}
    assert "bash" not in off
    assert "read_file" in off, "只禁用指定的那个，别误伤别的工具"

    settings.set_tool_enabled("bash", True)
    assert "bash" in {s["name"] for s in rt._build_loop().registry.schemas()}
