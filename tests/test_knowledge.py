"""知识库（M12）测试：文档 CRUD + 分块 + 检索（BM25 / 向量）+ API 权限。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.api.security import hash_password
from code_agent.config import knowledge_dir
from code_agent.knowledge import KnowledgeBase, KbSearchTool
from code_agent.knowledge.store import KnowledgeStore, chunk_text
from code_agent.tools.base import ToolContext


@pytest.fixture
def no_embed(monkeypatch):
    """清掉 embedding 相关环境变量，保证测试确定性走 BM25 降级。"""
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
# 分块
# ----------------------------------------------------------------------
def test_chunk_text_short_merges():
    chunks = chunk_text("第一段。\n\n第二段。\n\n第三段。")
    assert len(chunks) == 1
    assert "第一段" in chunks[0]


def test_chunk_text_long_splits():
    big = "。" * 2500
    chunks = chunk_text(big)
    assert len(chunks) >= 3


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


# ----------------------------------------------------------------------
# store CRUD
# ----------------------------------------------------------------------
def test_store_crud(agent_home):
    store = KnowledgeStore(knowledge_dir())
    doc = store.create("标题A", "正文内容", ["tag1", "tag2"])
    assert doc["id"].startswith("kb_")
    assert store.list()[0]["title"] == "标题A"

    got = store.get(doc["id"])
    assert got["content"] == "正文内容"
    assert got["tags"] == ["tag1", "tag2"]

    updated = store.save(doc["id"], "新标题", "新正文", [])
    assert updated["title"] == "新标题"

    assert store.delete(doc["id"]) is True
    assert store.get(doc["id"]) is None
    assert store.delete(doc["id"]) is False


def test_store_import_dir(agent_home, tmp_path):
    store = KnowledgeStore(knowledge_dir())
    (tmp_path / "a.md").write_text("---\ntitle: 甲\n---\n内容甲", encoding="utf-8")
    (tmp_path / "b.txt").write_text("内容乙", encoding="utf-8")
    (tmp_path / "skip.bin").write_text("忽略", encoding="utf-8")
    result = store.import_dir(str(tmp_path))
    assert result["imported"] == 2
    assert {d["title"] for d in store.list()} == {"甲", "b"}


# ----------------------------------------------------------------------
# KnowledgeBase 检索
# ----------------------------------------------------------------------
def test_kb_bm25_search(kb):
    kb.create("上线流程", "生产环境上线需要走变更审批，填写发布单。", ["流程"])
    kb.create("报销制度", "差旅费报销需要提交发票和行程单。", ["财务"])
    hits = asyncio.run(kb.search("上线 审批"))
    assert hits
    assert hits[0]["title"] == "上线流程"


def test_kb_search_no_match(kb):
    assert asyncio.run(kb.search("完全不存在的关键词xyz")) == []


def test_kb_delete_removes_from_index(kb):
    doc = kb.create("临时文档", "临时内容要删除", [])
    assert asyncio.run(kb.search("临时内容"))
    kb.delete(doc["id"])
    assert asyncio.run(kb.search("临时内容")) == []


def test_kb_update_reindexes(kb):
    doc = kb.create("旧文档", "旧内容", [])
    kb.update(doc["id"], "新文档", "新主题是关于云计算的", [])
    assert asyncio.run(kb.search("云计算"))[0]["title"] == "新文档"


def test_kb_stats(kb):
    kb.create("文档一", "内容一", [])
    s = kb.stats()
    assert s["docs"] == 1
    assert s["embedding"] is False  # no_embed 下必然降级


class _FakeEmbedder:
    """可控向量：命中「上线/审批」的文本向量贴近 query 向量。"""

    enabled = True
    model = "fake"

    async def embed(self, texts):
        out = []
        for t in texts:
            if "上线" in t or "审批" in t or "流程" in t:
                out.append([1.0, 0.0, 0.0])
            elif "报销" in t:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out

    async def aclose(self):
        pass


def test_kb_vector_search(agent_home, no_embed):
    kb = KnowledgeBase(knowledge_dir(), embedder=_FakeEmbedder())
    kb.create("上线流程", "上线要走审批", [])
    kb.create("报销制度", "报销提交发票", [])
    hits = asyncio.run(kb.search("上线流程审批"))
    assert hits and hits[0]["title"] == "上线流程"


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def test_kb_search_tool(kb, agent_home):
    kb.create("产品手册", "登录页面的默认密码是 admin123。", ["产品"])
    tool = KbSearchTool(kb)
    ctx = ToolContext(session_id="s", workspace=agent_home, user_id="u")
    out = asyncio.run(tool.execute({"query": "默认密码"}, ctx))
    assert "admin123" in out


def test_kb_search_tool_empty(kb, agent_home):
    tool = KbSearchTool(kb)
    ctx = ToolContext(session_id="s", workspace=agent_home, user_id="u")
    out = asyncio.run(tool.execute({"query": "x"}, ctx))
    assert "没有相关内容" in out


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@pytest.fixture
def client(agent_home, no_embed):
    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_kb_api_crud_and_permissions(client):
    token = _login(client)
    h = _auth(token)

    # 创建（admin）
    r = client.post(
        "/api/v1/knowledge",
        json={"title": "FAQ", "content": "如何重置密码？联系管理员。", "tags": ["账号"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]

    # 列表 / 单个 / 检索 / stats
    docs = client.get("/api/v1/knowledge", headers=h).json()
    assert len(docs) == 1 and docs[0]["title"] == "FAQ"
    assert client.get(f"/api/v1/knowledge/{doc_id}", headers=h).json()["content"].startswith("如何")
    hits = client.get("/api/v1/knowledge/search", params={"q": "重置密码"}, headers=h).json()
    assert hits and "重置密码" in hits[0]["text"]
    assert client.get("/api/v1/knowledge/stats", headers=h).json()["docs"] == 1

    # 更新
    r = client.put(
        f"/api/v1/knowledge/{doc_id}",
        json={"title": "FAQ", "content": "如何重置密码？找 IT。", "tags": []},
        headers=h,
    )
    assert r.status_code == 200

    # 删除
    assert client.delete(f"/api/v1/knowledge/{doc_id}", headers=h).status_code == 200
    assert client.get(f"/api/v1/knowledge/{doc_id}", headers=h).status_code == 404


def test_kb_api_write_requires_admin(client):
    token = _login(client)
    h = _auth(token)

    client.app.state.users.create("bob", hash_password("pw123456"))
    token2 = _login(client, "bob", "pw123456")
    h2 = _auth(token2)

    # 非 admin 写 -> 403
    assert (
        client.post("/api/v1/knowledge", json={"title": "x", "content": "y"}, headers=h2).status_code
        == 403
    )
    assert (
        client.post("/api/v1/knowledge/import", json={"dir": "/tmp"}, headers=h2).status_code
        == 403
    )
    # 非 admin 读与检索 -> 200
    assert client.get("/api/v1/knowledge", headers=h2).status_code == 200
    assert client.get("/api/v1/knowledge/search", params={"q": "x"}, headers=h2).status_code == 200


def test_kb_api_import_files(client):
    token = _login(client)
    h = _auth(token)

    import base64

    payload = {
        "files": [
            {"filename": "guide.md", "content_b64": base64.b64encode("# 指南\n这是指南内容".encode()).decode()}
        ]
    }
    r = client.post("/api/v1/knowledge/import", json=payload, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1
