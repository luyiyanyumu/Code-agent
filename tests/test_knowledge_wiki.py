"""LLM wiki（M14）测试：双区存储 + 模型沉淀 + 采纳 + LLM 编译。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.api.security import hash_password
from code_agent.config import knowledge_dir
from code_agent.knowledge import (
    AREA_HUMAN,
    AREA_LLM,
    KbWriteTool,
    KnowledgeBase,
    compile_document,
    parse_pages,
)
from code_agent.knowledge.store import KnowledgeStore
from code_agent.tools.base import ToolContext


@pytest.fixture
def no_embed(monkeypatch):
    for key in (
        "CODE_AGENT_EMBED_BASE_URL",
        "CODE_AGENT_EMBED_API_KEY",
        "CODE_AGENT_EMBED_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def kb(agent_home, no_embed):
    return KnowledgeBase(knowledge_dir())


# ----------------------------------------------------------------------
# 双区存储
# ----------------------------------------------------------------------
def test_store_two_areas(agent_home):
    store = KnowledgeStore(knowledge_dir())
    h = store.create("人工文档", "人工内容", [], AREA_HUMAN)
    m = store.create("模型文档", "模型内容", [], AREA_LLM)
    assert h["origin"] == "human" and m["origin"] == "llm"
    # 隔离区落在 _llm 子目录，正式区在根
    assert (knowledge_dir() / "_llm" / f"{m['id']}.md").is_file()
    assert (knowledge_dir() / f"{h['id']}.md").is_file()

    assert {d["origin"] for d in store.list()} == {"human", "llm"}
    assert [d["id"] for d in store.list(AREA_LLM)] == [m["id"]]
    assert [d["id"] for d in store.list(AREA_HUMAN)] == [h["id"]]


def test_store_move_promotes(agent_home):
    store = KnowledgeStore(knowledge_dir())
    m = store.create("草稿", "内容", [], AREA_LLM)
    moved = store.move(m["id"], AREA_LLM, AREA_HUMAN)
    assert moved["origin"] == "human"
    assert store.get(m["id"], AREA_LLM) is None
    assert store.get(m["id"], AREA_HUMAN) is not None


# ----------------------------------------------------------------------
# 双区检索
# ----------------------------------------------------------------------
def test_search_spans_both_areas(kb):
    kb.create("产品手册", "登录默认密码是 admin123", [], AREA_HUMAN)
    kb.create("运维笔记", "服务器重启命令是 systemctl restart app", [], AREA_LLM)
    hits = asyncio.run(kb.search("systemctl restart"))
    assert hits and hits[0]["origin"] == "llm"


def test_search_origin_filter(kb):
    kb.create("人工知识", "关键词甲乙丙", [], AREA_HUMAN)
    kb.create("模型知识", "关键词甲乙丙丁", [], AREA_LLM)
    hits = asyncio.run(kb.search("关键词", origin=AREA_HUMAN))
    assert hits and all(h["origin"] == "human" for h in hits)


def test_promote_reindexes(kb):
    doc = kb.create("待审核", "独特关键词xyzzy", [], AREA_LLM)
    hits = asyncio.run(kb.search("xyzzy"))
    assert hits and hits[0]["origin"] == "llm"
    kb.promote(doc["id"])
    hits2 = asyncio.run(kb.search("xyzzy"))
    assert hits2 and hits2[0]["origin"] == "human"


def test_stats_counts_pending(kb):
    kb.create("正式", "a", [], AREA_HUMAN)
    kb.create("隔离", "b", [], AREA_LLM)
    s = kb.stats()
    assert s["docs"] == 1 and s["pending"] == 1


# ----------------------------------------------------------------------
# kb_write（模型沉淀 → 隔离区）
# ----------------------------------------------------------------------
def test_kb_write_goes_to_staging(kb, agent_home):
    tool = KbWriteTool(kb)
    ctx = ToolContext(session_id="s1", workspace=agent_home, user_id="u")
    out = asyncio.run(tool.execute({"title": "新知识", "content": "这是模型沉淀的内容"}, ctx))
    assert "隔离区" in out
    docs = kb.list_docs(AREA_LLM)
    assert len(docs) == 1
    assert docs[0]["title"] == "新知识"
    assert docs[0]["source"] == "session:s1"


def test_kb_write_rejects_empty(kb, agent_home):
    tool = KbWriteTool(kb)
    ctx = ToolContext(session_id="s", workspace=agent_home, user_id="u")
    out = asyncio.run(tool.execute({"title": "", "content": "x"}, ctx))
    assert "不能为空" in out


# ----------------------------------------------------------------------
# LLM 编译
# ----------------------------------------------------------------------
def test_parse_pages():
    text = "===PAGE: 甲===\n摘要甲\n\n正文甲\n===PAGE: 乙===\n摘要乙\n正文乙"
    pages = parse_pages(text)
    assert [p["title"] for p in pages] == ["甲", "乙"]
    assert "正文甲" in pages[0]["content"]


def test_parse_pages_fallback():
    pages = parse_pages("没有分页标记的纯文本")
    assert len(pages) == 1 and pages[0]["title"] == "编译稿"
    assert parse_pages("") == []


def test_compile_document_with_mock_provider():
    class FakeTurn:
        text = "===PAGE: 主题A===\n摘要\n\n内容A\n===PAGE: 主题B===\n内容B"

    class FakeProvider:
        async def chat(self, messages, tools):
            assert "wiki" in messages[0]["content"]
            return FakeTurn()

    pages = asyncio.run(compile_document("原始资料", FakeProvider(), title="资料"))
    assert [p["title"] for p in pages] == ["主题A", "主题B"]


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@pytest.fixture
def client(agent_home, no_embed):
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _auth(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_api_origin_filter_and_promote(client):
    h = _auth(client)
    kb = KnowledgeBase(knowledge_dir())
    doc = kb.create("待审核条目", "内容", [], AREA_LLM)

    assert client.get("/api/v1/knowledge?origin=llm", headers=h).json()[0]["id"] == doc["id"]
    assert client.get("/api/v1/knowledge?origin=human", headers=h).json() == []

    r = client.post(f"/api/v1/knowledge/{doc['id']}/promote", headers=h)
    assert r.status_code == 200 and r.json()["origin"] == "human"
    assert client.get("/api/v1/knowledge?origin=llm", headers=h).json() == []


def test_api_stats_has_pending(client):
    h = _auth(client)
    KnowledgeBase(knowledge_dir()).create("隔离", "x", [], AREA_LLM)
    assert client.get("/api/v1/knowledge/stats", headers=h).json()["pending"] == 1


def test_api_compile(client, monkeypatch):
    h = _auth(client)
    doc_id = client.post(
        "/api/v1/knowledge", json={"title": "原始", "content": "原始资料内容"}, headers=h
    ).json()["id"]

    async def fake_compile(source, provider, title=""):
        return [{"title": "编译页1", "content": "内容1"}, {"title": "编译页2", "content": "内容2"}]

    monkeypatch.setattr("code_agent.api.app.compile_document", fake_compile)
    r = client.post(f"/api/v1/knowledge/{doc_id}/compile", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["pages"] == 2

    llm_docs = client.get("/api/v1/knowledge?origin=llm", headers=h).json()
    assert {d["title"] for d in llm_docs} == {"编译页1", "编译页2"}


def test_api_compile_requires_admin(client):
    h = _auth(client)
    doc_id = client.post(
        "/api/v1/knowledge", json={"title": "x", "content": "y"}, headers=h
    ).json()["id"]

    client.app.state.users.create("bob", hash_password("pw123456"))
    bob = {"Authorization": f"Bearer {client.post('/api/v1/auth/login', json={'username': 'bob', 'password': 'pw123456'}).json()['token']}"}
    assert client.post(f"/api/v1/knowledge/{doc_id}/compile", headers=bob).status_code == 403
    assert client.post(f"/api/v1/knowledge/{doc_id}/promote", headers=bob).status_code == 403
