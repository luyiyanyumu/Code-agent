"""会话生命周期补充：彻底删除 + 上下文占用估算。

删除是硬删除（DB 行清掉、轨迹进 .trash），与 close（终止）/archive（归档）
这两个"保留可查"的软状态不同——测试必须把三者区分开。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from code_agent.context.prompt import dynamic_history_budget, history_budget
from code_agent.sessions.manager import _context_window, _est_tokens
from code_agent.storage.jsonl import TranscriptStore


# ----------------------------------------------------------------------
# 动态历史预算
# ----------------------------------------------------------------------
def test_dynamic_budget_scales_with_window() -> None:
    """同一份 system/tools，窗口越大历史预算越大；小窗口不低于下限。"""
    small = dynamic_history_budget(32_768, system_chars=2_000, tools_chars=3_000)
    big = dynamic_history_budget(1_000_000, system_chars=2_000, tools_chars=3_000)
    assert big > small
    assert small >= 4_000                     # 下限兜底
    # 32K 窗口 × 50% − 固定开销 ≈ 13.6K token ≈ 30K 字符量级
    assert 10_000 < small < 60_000
    # 1M 窗口接近 50% × 2.2 ≈ 1.1M 字符
    assert big > 500_000


def test_dynamic_budget_falls_back_without_window(monkeypatch) -> None:
    monkeypatch.setenv("CODE_AGENT_HISTORY_BUDGET", "24000")
    assert dynamic_history_budget(None) == history_budget()
    assert dynamic_history_budget(0) == history_budget()


def test_dynamic_budget_subtracts_fixed_overhead() -> None:
    """system/tools 越大，留给历史的越少。"""
    base = dynamic_history_budget(32_768, system_chars=0, tools_chars=0)
    heavy = dynamic_history_budget(32_768, system_chars=6_000, tools_chars=10_000)
    assert heavy < base



# ----------------------------------------------------------------------
# 估算函数
# ----------------------------------------------------------------------
def test_est_tokens_scales_with_bytes() -> None:
    assert _est_tokens("") == 0
    assert _est_tokens("hello world, plain english") > 0
    cn = _est_tokens("这是一段中文文本" * 20)
    en = _est_tokens("a" * 200)
    assert 0 < cn < 10_000 and 0 < en < 10_000


@pytest.mark.parametrize(
    "model,expected",
    [
        ("deepseek-chat", 131_072),
        ("deepseek-v4-pro", 1_000_000),
        ("qwen3:8b", 32_768),
        ("kimi-k2-turbo-preview", 131_072),
        ("unknown-model", 131_072),  # 兜底
    ],
)
def test_context_window_mapping(model: str, expected: int) -> None:
    assert _context_window(model) == expected


# ----------------------------------------------------------------------
# 轨迹回收目录
# ----------------------------------------------------------------------
def test_transcript_discard_moves_to_trash(tmp_path) -> None:
    store = TranscriptStore(tmp_path)
    store.append("s_del", "user_message", {"text": "hi"})
    path = store._path("s_del")
    assert path.exists()

    store.discard("s_del")
    assert not path.exists()
    trashed = list((tmp_path / ".trash").glob("s_del-*.jsonl"))
    assert len(trashed) == 1
    assert json.loads(trashed[0].read_text(encoding="utf-8").splitlines()[0])["type"] == "user_message"


def test_transcript_discard_missing_file_is_noop(tmp_path) -> None:
    store = TranscriptStore(tmp_path)
    store.discard("s_never_existed")  # 不抛异常即可


# ----------------------------------------------------------------------
# API 级：创建 → 对话（offline）→ 上下文 → 删除
# ----------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    # 必须隔离 CODE_AGENT_HOME：否则直接连到真实 ~/.code-agent，
    # 与正在运行的服务实例抢同一把 SQLite 锁（database is locked）。
    home = tmp_path / "code-agent-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("CODE_AGENT_HOME", str(home))
    for key in ("CODE_AGENT_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from code_agent.api.app import create_app

    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_context_usage_after_prompt(client) -> None:
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    r = client.post(
        f"/api/v1/sessions/{sid}/prompt", json={"text": "你好"}, headers=h
    )
    assert r.status_code == 200, r.text

    ctx = client.get(f"/api/v1/sessions/{sid}/context", headers=h)
    assert ctx.status_code == 200, ctx.text
    body = ctx.json()
    assert body["system"] > 0          # 系统提示词
    assert body["tools"] > 0           # 工具定义
    assert body["messages"] > 0        # 至少有这一轮对话
    assert body["total"] == body["system"] + body["tools"] + body["messages_raw"]
    # used_pct 按"实际发送量"（压缩后）计算，弹窗展示口径 = 真实请求口径
    assert body["effective"] == body["system"] + body["tools"] + body["messages"]
    assert 0 < body["used_pct"] <= 100
    assert body["window"] > 0
    assert body["budget_tokens"] > 0
    # 未超预算时 messages 不该被"压缩"
    if not body["compressed"]:
        assert body["messages"] == body["messages_raw"]


def test_context_usage_requires_own_session(client) -> None:
    h = _login(client)
    from code_agent.api.security import hash_password

    client.app.state.users.create("bob", hash_password("pw123456"))
    r = client.post("/api/v1/auth/login", json={"username": "bob", "password": "pw123456"})
    bob = {"Authorization": f"Bearer {r.json()['token']}"}
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    assert client.get(f"/api/v1/sessions/{sid}/context", headers=bob).status_code in (403, 404)


def test_delete_session_purges_everything(client, tmp_path) -> None:
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    client.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "记一笔"}, headers=h)

    r = client.delete(f"/api/v1/sessions/{sid}", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True
    # 会话没了，上下文估算与事件回放都应 404
    assert client.get(f"/api/v1/sessions/{sid}/context", headers=h).status_code == 404
    assert client.get(f"/api/v1/sessions/{sid}/events", headers=h).status_code in (403, 404)
    assert client.get(f"/api/v1/sessions/{sid}", headers=h).status_code == 404
    # 列表里也不再出现
    assert all(s["id"] != sid for s in client.get("/api/v1/sessions", headers=h).json())


def test_delete_session_twice_returns_404(client) -> None:
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    assert client.delete(f"/api/v1/sessions/{sid}", headers=h).status_code == 200
    assert client.delete(f"/api/v1/sessions/{sid}", headers=h).status_code == 404
