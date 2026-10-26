"""认证原语单测 + 模型路由单测。"""

import os

from code_agent.api.security import (
    AuthError,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)
from code_agent.agent.router import ModelRouter
from code_agent.providers.offline import OfflineProvider


def test_password_hash_roundtrip():
    stored = hash_password("s3cret!")
    assert stored != "s3cret!"
    assert "$" in stored
    assert verify_password("s3cret!", stored)
    assert not verify_password("wrong", stored)


def test_token_roundtrip(agent_home):
    token = create_token("u1", "alice", "user")
    payload = decode_token(token)
    assert payload["sub"] == "u1"
    assert payload["username"] == "alice"
    assert payload["role"] == "user"


def test_invalid_token(agent_home):
    try:
        decode_token("not.a.token")
        assert False
    except AuthError:
        pass


def test_router_default_model(agent_home, monkeypatch):
    """M36/M40：只有一个默认模型（三档已去掉，会话级指定也去掉）。"""
    monkeypatch.setenv("CODE_AGENT_PROVIDER", "offline")
    r = ModelRouter()
    p = r.resolve_default()
    assert isinstance(p, OfflineProvider)
    assert r.resolve_default() is p            # 缓存
    assert r.model_name() == "offline-mock"
    # 按名字取 provider 仍然要能用（配置档案里的模型名走这条路）
    named = r.resolve_name("glm-4.7-flash")
    assert isinstance(named, OfflineProvider)
    assert named is not p                      # 与默认 provider 分开缓存


def test_router_model_env(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_PROVIDER", "offline")
    monkeypatch.setenv("CODE_AGENT_MODEL_DEFAULT", "custom-model")
    r = ModelRouter(provider_name="deepseek")
    assert r.model_name() == "custom-model"
