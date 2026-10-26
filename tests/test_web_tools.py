"""联网（M19）：检索/抓取工具 + 会话级开关。

离线跑（不打真实网络）：
- 用假 httpx client 驱动 `DeepSeekSearchProvider`，验证它**只认结构化结果块**、
  摘要按 URL 关联、去重与截断；
- `web_fetch` 的 SSRF 闸门与 HTML→文本；
- 开关语义：关着时 `web_search`/`web_fetch` 不在 schema 里，开着才在，
  而且从模型可见到执行全程一致（FilteredToolRegistry 要真拦得住）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.web_tools import (
    WEB_SYSTEM_SUFFIX,
    WEB_TOOL_NAMES,
    WebFetchTool,
    WebSearchTool,
    format_search,
)
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry
from code_agent.web import fetch as fetch_mod
from code_agent.web import search as search_mod

# ---------------------------------------------------------------------------
# 假 httpx：把"服务端返回什么"完全握在测试手里（不打真实网络）
# ---------------------------------------------------------------------------
SEARCH_PAYLOAD = {
    "content": [
        {"type": "text", "text": "DeepSeek 的联网检索返回结构化结果块。",
         "citations": [{"url": "https://example.com/a", "cited_text": "A 的摘要文本"}]},
        {
            "type": "web_search_tool_result",
            "content": [
                {"type": "web_search_result", "url": "https://example.com/a",
                 "title": "A 标题", "page_age": "2026-01-02"},
                {"type": "web_search_result", "url": "https://example.com/b", "title": "B 标题"},
                {"type": "web_search_result", "url": "https://example.com/a", "title": "A 重复"},
            ],
        },
    ]
}


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {"content-type": "application/json"}
        self.content = self.text.encode("utf-8")
        self.encoding = "utf-8"
        self.url = "https://api.deepseek.com/anthropic/v1/messages"

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """记录请求，返回预设响应。"""

    def __init__(self, payload: dict | None = None, status: int = 200) -> None:
        self.payload = payload if payload is not None else SEARCH_PAYLOAD
        self.status = status
        self.posts: list[dict] = []

    async def post(self, url, headers=None, content=None, follow_redirects=True):
        self.posts.append({"url": url, "headers": dict(headers or {}), "body": json.loads(content)})
        return FakeResponse(self.payload, self.status)

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


def test_provider_sends_native_web_search_tool():
    client = FakeClient()
    provider = search_mod.DeepSeekSearchProvider(client=client)
    result = asyncio.run(provider.search(["最新版本是多少"]))
    assert len(client.posts) == 1
    body = client.posts[0]["body"]
    tool = body["tools"][0]
    assert tool["type"] == "web_search_20250305" and tool["name"] == "web_search"
    assert "最新版本是多少" in body["messages"][0]["content"][0]["text"]
    headers = client.posts[0]["headers"]
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["authorization"] == "Bearer test-key"
    # 结果：按 URL 去重 + 摘要来自 citations
    assert [s.url for s in result.sources] == ["https://example.com/a", "https://example.com/b"]
    assert result.sources[0].snippet == "A 的摘要文本"
    assert result.sources[0].published_at == "2026-01-02"
    assert result.content.startswith("DeepSeek 的联网检索")


def test_provider_errors_without_result_block():
    """没有结果块就是错误：不做"抓正文凑一段"的兜底（DSH 的取舍）。"""
    client = FakeClient(payload={"content": [{"type": "text", "text": "我查不到"}]})
    provider = search_mod.DeepSeekSearchProvider(client=client)
    with pytest.raises(search_mod.WebError) as err:
        asyncio.run(provider.search(["x"]))
    assert "web_search_tool_result" in str(err.value)


def test_provider_surfaces_http_error_detail():
    client = FakeClient(payload={"error": {"message": "invalid api key"}}, status=401)
    provider = search_mod.DeepSeekSearchProvider(client=client)
    with pytest.raises(search_mod.WebError) as err:
        asyncio.run(provider.search(["x"]))
    assert "401" in str(err.value) and "invalid api key" in str(err.value)


def test_provider_requires_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CODE_AGENT_SEARCH_API_KEY", "")
    info = search_mod.availability()
    assert info["available"] is False
    assert "API key" in info["reason"]
    with pytest.raises(search_mod.WebError):
        asyncio.run(search_mod.DeepSeekSearchProvider(client=FakeClient()).search(["x"]))


def test_multi_query_merges_and_dedupes():
    client = FakeClient()
    provider = search_mod.DeepSeekSearchProvider(client=client)
    result = asyncio.run(provider.search(["q1", "q2"]))
    assert len(client.posts) == 2, "每条查询一次调用，便于按查询归属"
    assert [s.url for s in result.sources] == ["https://example.com/a", "https://example.com/b"]
    assert result.queries == ["q1", "q2"]


def test_search_tool_output_is_wrapped_as_untrusted():
    """外部内容必须包裹成"数据块"：这是 context/safety 的威胁模型要求。"""
    tool = WebSearchTool()
    output = format_search(
        search_mod.SearchResult(
            sources=[search_mod.Source(url="https://e.com", title="T", snippet="S")],
            content="摘要",
        )
    )
    assert "数据块开始" in output and "数据块结束" in output
    assert "不可信数据" in output
    assert "https://e.com" in output
    # 参数校验：空查询直接拒绝，不发请求
    with pytest.raises(Exception):
        asyncio.run(tool.execute({"queries": []}, ToolContext(session_id="s", workspace=Path("."))))


def test_web_tool_schemas_are_function_definitions():
    reg = ToolRegistry()
    for tool in (WebSearchTool(), WebFetchTool()):
        reg.register(tool)
    schemas = {s["name"]: s for s in reg.schemas()}
    assert set(WEB_TOOL_NAMES) <= set(schemas)
    assert schemas["web_search"]["parameters"]["required"] == ["queries", "_approval"]
    assert schemas["web_fetch"]["parameters"]["required"] == ["url", "_approval"]


def test_filtered_registry_hides_and_blocks_web_tools():
    """开关关掉时：schema 里没有，直接调也拦得住（不能只藏不拦）。"""
    inner = ToolRegistry()
    for tool in (WebSearchTool(), WebFetchTool()):
        inner.register(tool)
    filtered = FilteredToolRegistry(inner, exclude=WEB_TOOL_NAMES)
    assert filtered.names() == []
    assert filtered.schemas() == []
    assert filtered.get("web_search") is None
    with pytest.raises(Exception):
        asyncio.run(filtered.dispatch("web_search", {"queries": ["x"]}, ToolContext("s", Path("."))))
    # 未排除的工具照常可用（透传）
    assert inner.get("web_fetch") is not None
    assert filtered.names() == []


def test_web_system_suffix_states_untrusted_data_rule():
    assert "web_search" in WEB_SYSTEM_SUFFIX
    assert "不可信数据" in WEB_SYSTEM_SUFFIX


# ---------------------------------------------------------------------------
# 取正文：SSRF 闸门 + HTML → 文本
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/api/v1/health",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "file:///C:/Windows/win.ini",
    ],
)
def test_fetch_blocks_non_public_targets(url):
    with pytest.raises(fetch_mod.FetchError):
        asyncio.run(fetch_mod.fetch_page(url))


def test_html_to_text_strips_scripts_and_extracts_title():
    html = """
    <html><head><title>标题 - 站点</title><style>body{color:red}</style></head>
    <body><nav>导航</nav><script>alert('不要执行我')</script>
    <article><h1>大标题</h1><p>第一段&nbsp;正文。</p><p>第二段。</p></article></body></html>
    """
    title, text = fetch_mod.html_to_text(html)
    assert title == "标题 - 站点"
    assert "不要执行我" not in text
    assert "第一段 正文。" in text
    assert "导航" not in text, "正文容器外的导航应被裁掉"


# ---------------------------------------------------------------------------
# 会话级开关：落到运行时（模型看到什么 = 能调什么）
# ---------------------------------------------------------------------------
def _runtime(agent_home, web: bool = False):
    from code_agent.agent.router import ModelRouter
    from code_agent.sessions.manager import SessionManager
    from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
    from code_agent.storage.jsonl import TranscriptStore

    from code_agent.cli import build_registry
    from code_agent.config import transcripts_dir

    db = Database(agent_home / "web.db")
    manager = SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )
    users = UserRepo(db)
    uid = users.create("u_web", "x", role="user")["id"]
    meta = manager.session_repo.create(uid, web_enabled=1 if web else 0)
    return manager, manager.get_runtime(meta["id"], uid), meta["id"], uid


def test_session_toggle_decides_tool_exposure(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    manager, runtime, sid, uid = _runtime(agent_home, web=False)
    off = {s["name"] for s in runtime._build_loop().registry.schemas()}
    assert not (set(WEB_TOOL_NAMES) & off), "关着联网时不该把联网工具暴露给模型"
    assert "联网已开启" not in runtime._build_loop().system_prompt

    # 打开：落库 + 热推到运行时（不重建）
    meta = manager.set_web_enabled(sid, uid, True)
    assert meta["web_enabled"] == 1
    on_loop = runtime._build_loop()
    on = {s["name"] for s in on_loop.registry.schemas()}
    assert set(WEB_TOOL_NAMES) <= on
    assert "联网已开启" in on_loop.system_prompt

    # 关了再开：状态要能来回切
    manager.set_web_enabled(sid, uid, False)
    assert not (set(WEB_TOOL_NAMES) & {s["name"] for s in runtime._build_loop().registry.schemas()})


def test_rebuilt_runtime_keeps_web_flag(agent_home, monkeypatch):
    """进程重启后运行时是重建的：开关必须从库里恢复，而不是悄悄回到关闭。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    manager, _runtime_obj, sid, uid = _runtime(agent_home, web=True)
    manager._runtimes.clear()                      # 模拟重启：丢掉内存里的运行时
    rebuilt = manager.get_runtime(sid, uid)
    assert rebuilt.web_enabled is True
    assert set(WEB_TOOL_NAMES) <= {s["name"] for s in rebuilt._build_loop().registry.schemas()}


def test_submit_web_flag_overrides_session_setting(agent_home, monkeypatch):
    """前端每次发消息都带开关：显式值优先，避免"刚点开关就发送"的竞态。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    _, runtime, _sid, _uid = _runtime(agent_home, web=False)
    assert runtime.web_enabled is False
    runtime.web_enabled = True          # 等价于 submit(web=True) 里的那一行
    assert runtime.web_enabled is True
