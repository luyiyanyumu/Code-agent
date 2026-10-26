"""pytest fixtures：临时 CODE_AGENT_HOME 隔离 + 通用工具。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import code_agent.config as config


@pytest.fixture(autouse=True)
def authenticated_api_mode(monkeypatch):
    """现有多用户/权限测试继续显式验证可选 JWT 模式。"""
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "true")


@pytest.fixture
def agent_home(tmp_path: Path, monkeypatch) -> Path:
    """每个测试用独立的临时数据目录。"""
    home = tmp_path / "code-agent-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODE_AGENT_HOME", str(home))
    return home


@pytest.fixture
def workspace(agent_home: Path) -> Path:
    return config.workspace_root()


def run(coro):
    """无 pytest-asyncio 依赖跑协程。"""
    return asyncio.run(coro)
