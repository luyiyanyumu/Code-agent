"""联网检索（M19）：把"模型可以上网查"做成一个显式开关的能力。

参考 deepseek harness 的做法（`dsh-web-search-deepseek` + `dsh-tool-web`）：

1. **不用"抓搜索结果页"**：走 DeepSeek 的 Anthropic 兼容 Messages 接口
   （默认 `https://api.deepseek.com/anthropic/v1/messages`）+ 原生服务端工具
   `web_search_20250305`。它直接返回结构化的 `web_search_tool_result` 块，
   不是让我们去解析 HTML——**拿不到结果块就是错误**，不做"抓正文凑一段"的兜底。
2. **两段式**：`web_search` 找候选（标题/URL/摘要），`web_fetch` 再取某一条的正文。
   摘要是 `text` 块 citations 里的 `cited_text`，按 URL 关联（首次出现者优先）。
3. **端点是独立配置**：检索说的是 Anthropic 兼容协议，和聊天用的
   OpenAI 兼容 base_url 不是同一个，所以**不复用** `DEEPSEEK_BASE_URL`——
   沿用 DSH 的取舍，只共用 key。

配置（config.json 未设时读环境变量）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `CODE_AGENT_SEARCH_BASE_URL` | `https://api.deepseek.com/anthropic/v1` | Anthropic 兼容 Messages 基址（会拼 `/messages`） |
| `CODE_AGENT_SEARCH_MODEL` | `deepseek-chat` | 发起检索用的模型名（需支持原生 web_search 工具） |
| `CODE_AGENT_SEARCH_API_VERSION` | `2023-06-01` | `anthropic-version` 头 |
| `CODE_AGENT_SEARCH_MAX_USES` | `5` | 单次请求内最多触发几次搜索 |
| `CODE_AGENT_SEARCH_MAX_RESULTS` | `8` | 合并后最多返回多少条来源 |
| `DEEPSEEK_API_KEY` | —— | 复用聊天用的 key；没有 key 时整个联网开关不可用 |
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

#: 归属头（便于对方定位来源；与包版本一起看）
USER_AGENT = "code-agent/1.0 (+web-search)"

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_API_VERSION = "2023-06-01"
DEFAULT_MAX_USES = 5
DEFAULT_MAX_RESULTS = 8
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 60.0


class WebError(Exception):
    """联网操作的失败（区别于"没有结果"）。"""

    def __init__(self, message: str, code: str = "WEB_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Source:
    url: str
    title: str = ""
    snippet: str = ""
    published_at: str = ""

    def as_dict(self) -> dict[str, str]:
        out = {"url": self.url}
        if self.title:
            out["title"] = self.title
        if self.snippet:
            out["snippet"] = self.snippet
        if self.published_at:
            out["publishedAt"] = self.published_at
        return out


@dataclass
class SearchResult:
    sources: list[Source] = field(default_factory=list)
    content: str = ""
    truncated: bool = False
    queries: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "queries": list(self.queries),
            "sources": [s.as_dict() for s in self.sources],
            "content": self.content,
            "truncated": self.truncated,
        }


def _setting(name: str, default: str) -> str:
    """config.json 的 search 段优先，其次环境变量，最后默认值。"""
    try:
        from code_agent.settings import load

        cfg = load().get("search") or {}
        if isinstance(cfg, dict):
            key = name.replace("CODE_AGENT_SEARCH_", "").lower()
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, int):
                return str(value)
    except Exception:  # noqa: BLE001 —— 配置坏了不该让工具直接崩
        pass
    return os.environ.get(name, default)


def _setting_int(name: str, default: int) -> int:
    try:
        value = int(_setting(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def search_config() -> dict[str, Any]:
    """当前检索配置（不含 key）。"""
    return {
        "base_url": _setting("CODE_AGENT_SEARCH_BASE_URL", DEFAULT_BASE_URL),
        "model": _setting("CODE_AGENT_SEARCH_MODEL", DEFAULT_MODEL),
        "api_version": _setting("CODE_AGENT_SEARCH_API_VERSION", DEFAULT_API_VERSION),
        "max_uses": _setting_int("CODE_AGENT_SEARCH_MAX_USES", DEFAULT_MAX_USES),
        "max_results": _setting_int("CODE_AGENT_SEARCH_MAX_RESULTS", DEFAULT_MAX_RESULTS),
    }


def api_key() -> str:
    """检索用的 key：优先 config.json 的 api_key，其次 DEEPSEEK_API_KEY。"""
    try:
        from code_agent.settings import load

        cfg = load()
        value = cfg.get("search_api_key") if isinstance(cfg, dict) else ""
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def availability() -> dict[str, Any]:
    """联网能力是否可用 + 为什么不可用（界面据此禁用开关并给出提示）。"""
    cfg = search_config()
    key = api_key()
    if not key:
        return {
            "available": False,
            "reason": "没有检索用的 API key：在 ~/.code-agent/config.json 设 search_api_key，"
                      "或设置环境变量 DEEPSEEK_API_KEY",
            **cfg,
        }
    return {"available": True, "reason": "", **cfg}


class DeepSeekSearchProvider:
    """DeepSeek 原生联网检索（Anthropic 兼容 Messages + `web_search_20250305`）。"""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def search(self, queries: list[str], *, max_results: int = 0) -> SearchResult:
        clean = [q.strip() for q in queries if str(q).strip()][:4]
        if not clean:
            raise WebError("没有有效的检索词", "WEB_BAD_REQUEST")
        cfg = search_config()
        key = api_key()
        if not key:
            raise WebError(availability()["reason"], "WEB_PROVIDER_CREDENTIAL_MISSING")

        endpoint = f"{cfg['base_url'].rstrip('/')}/messages"
        # 多条 query 一次请求：DSH 的做法是每条 query 一次调用再合并；
        # 这里同样分开调用，原因是原生 web_search 只认"这一次对话要查什么"，
        # 把多条塞进一句话会让服务端只搜到一部分（而且拿不到按 query 的归属）。
        merged: list[Source] = []
        seen: set[str] = set()
        answer = ""
        truncated = False
        errors: list[str] = []
        for query in clean:
            try:
                part = await self._search_one(endpoint, key, cfg, query)
            except WebError as exc:
                errors.append(f"{query}: {exc}")
                continue
            if part.content and not answer:
                answer = part.content
            for src in part.sources:
                if src.url in seen:
                    continue
                seen.add(src.url)
                merged.append(src)

        if not merged and errors:
            raise WebError("；".join(errors), "WEB_PROVIDER_ERROR")
        limit = max_results or cfg["max_results"]
        if len(merged) > limit:
            merged = merged[:limit]
            truncated = True
        return SearchResult(sources=merged, content=answer, truncated=truncated, queries=clean)

    async def _search_one(
        self, endpoint: str, key: str, cfg: dict[str, Any], query: str
    ) -> SearchResult:
        body = {
            "model": cfg["model"],
            "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": f"Perform a web search for the query: {query}"}],
                }
            ],
            "tools": [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": cfg["max_uses"]}
            ],
        }
        headers = {
            "x-api-key": key,
            "authorization": f"Bearer {key}",
            "anthropic-version": cfg["api_version"],
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": USER_AGENT,
        }
        client = self._client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        owns = self._client is None
        try:
            response = await client.post(
                endpoint, headers=headers, content=json.dumps(body).encode("utf-8"), follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise WebError(
                f"检索请求失败：{exc}（端点 {endpoint}；检索端点与聊天是两套配置，"
                f"可用 CODE_AGENT_SEARCH_BASE_URL 或 config.json 的 search.base_url 修改）",
                "WEB_PROVIDER_ERROR",
            ) from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                err = payload.get("error")
                detail = err if isinstance(err, str) else (err or {}).get("message", "") or payload.get("message", "")
            except Exception:  # noqa: BLE001
                detail = response.text[:200]
            raise WebError(
                f"检索接口返回 HTTP {response.status_code}"
                + (f"：{detail}" if detail else "")
                + f"（端点 {endpoint}，模型 {cfg['model']}）",
                "WEB_PROVIDER_ERROR",
            )
        try:
            return self._map(response.json())
        except WebError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WebError(f"检索响应无法解析：{exc}", "WEB_PROVIDER_ERROR") from exc

    @staticmethod
    def _map(payload: dict[str, Any]) -> SearchResult:
        blocks = payload.get("content") or []
        result_blocks = [b for b in blocks if b.get("type") == "web_search_tool_result"]
        if not result_blocks:
            raise WebError(
                "服务端没有返回 web_search_tool_result（可能这个模型/端点不支持原生联网工具）。"
                "按 DSH 的取舍：不做「抓正文凑一段」的兜底，以免把模型自述当成检索结果。",
                "WEB_PROVIDER_ERROR",
            )
        # 摘要来自 text 块的 citations：按 url 取首次出现的 cited_text
        snippets: dict[str, str] = {}
        for block in blocks:
            if block.get("type") != "text":
                continue
            for cite in block.get("citations") or []:
                url = cite.get("url")
                text = cite.get("cited_text")
                if url and text and url not in snippets:
                    snippets[url] = text
        answer = " ".join(
            str(b.get("text") or "") for b in blocks if b.get("type") == "text"
        ).strip()

        sources: list[Source] = []
        seen: set[str] = set()
        for block in result_blocks:
            for item in block.get("content") or []:
                url = str(item.get("url") or "")
                if item.get("type") != "web_search_result" or not url or url in seen:
                    continue
                seen.add(url)
                sources.append(
                    Source(
                        url=url,
                        title=str(item.get("title") or ""),
                        snippet=snippets.get(url, "") or str(item.get("snippet") or ""),
                        published_at=str(item.get("page_age") or ""),
                    )
                )
        return SearchResult(sources=sources, content=answer)
