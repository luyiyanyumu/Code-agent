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
| `CODE_AGENT_SEARCH_API_KEY` | —— | 独立检索 key，可用于显式配置的检索端点 |
| `DEEPSEEK_API_KEY` | —— | 官方检索端点可复用；也支持设置页保存的官方 DeepSeek 连接 |
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

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


def _setting(name: str, default: str, *, values: dict | None = None) -> str:
    """config.json 的 search 段优先，其次环境变量，最后默认值。"""
    try:
        from code_agent.settings import load

        cfg = (load() if values is None else values).get("search") or {}
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


def _setting_int(name: str, default: int, *, values: dict | None = None) -> int:
    try:
        value = int(_setting(name, str(default), values=values))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def search_config(*, _values: dict | None = None) -> dict[str, Any]:
    """当前检索配置（不含 key）。"""
    return {
        "base_url": _setting("CODE_AGENT_SEARCH_BASE_URL", DEFAULT_BASE_URL, values=_values),
        "model": _setting("CODE_AGENT_SEARCH_MODEL", DEFAULT_MODEL, values=_values),
        "api_version": _setting("CODE_AGENT_SEARCH_API_VERSION", DEFAULT_API_VERSION, values=_values),
        "max_uses": _setting_int("CODE_AGENT_SEARCH_MAX_USES", DEFAULT_MAX_USES, values=_values),
        "max_results": _setting_int("CODE_AGENT_SEARCH_MAX_RESULTS", DEFAULT_MAX_RESULTS, values=_values),
    }


def _official_deepseek(url: str, *, empty=False) -> bool:
    if not url:
        return empty
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _valid_endpoint(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and parsed.port != 0 and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def _credential(cfg: dict[str, Any], values: dict[str, Any]) -> tuple[str, str]:
    """Resolve fresh credentials without ever sending a chat key to a gateway."""
    try:
        from code_agent.settings import model_config

        search = values.get("search") if isinstance(values.get("search"), dict) else {}
        for value, source in ((search.get("api_key"), "search_settings"),
                              (values.get("search_api_key"), "search_settings"),
                              (os.environ.get("CODE_AGENT_SEARCH_API_KEY"), "search_environment")):
            if isinstance(value, str) and value.strip():
                return value.strip(), source
        if not _official_deepseek(cfg["base_url"]):
            return "", ""
        value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if value:
            return value, "deepseek_environment"
        current = model_config()
        if current.get("provider") == "deepseek" and _official_deepseek(
                str(current.get("base_url") or "").strip(), empty=True):
            value = str(current.get("api_key") or "").strip()
            if value:
                return value, "model_settings"
    except Exception:
        # Capability errors must never include configuration or credential text.
        return "", ""
    return "", ""


def api_key() -> str:
    """Reuse the same current, endpoint-scoped credential as capability/search."""
    return _configuration()[1]


def _configuration() -> tuple[dict[str, Any], str, dict[str, Any]]:
    from code_agent.settings import load

    values = load()
    cfg = search_config(_values=values)
    if not _valid_endpoint(cfg["base_url"]):
        return cfg, "", {**cfg, "base_url": "", "available": False,
            "reason_code": "WEB_PROVIDER_CONFIG_INVALID", "credential_source": "",
            "reason": "联网检索地址无效，请配置不含账号、密码或查询参数的 HTTP/HTTPS 检索地址。"}
    key, source = _credential(cfg, values)
    return cfg, key, {**cfg, "available": bool(key), "credential_source": source,
        "reason_code": "" if key else "WEB_PROVIDER_CREDENTIAL_MISSING",
        "reason": "" if key else "未配置可用于联网检索的 API key。请在模型设置中配置官方 DeepSeek 连接，或配置独立检索密钥。"}


def availability() -> dict[str, Any]:
    """联网能力是否可用 + 为什么不可用（界面据此禁用开关并给出提示）。"""
    return _configuration()[2]


class DeepSeekSearchProvider:
    """DeepSeek 原生联网检索（Anthropic 兼容 Messages + `web_search_20250305`）。"""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def search(self, queries: list[str], *, max_results: int = 0) -> SearchResult:
        clean = [q.strip() for q in queries if str(q).strip()][:4]
        if not clean:
            raise WebError("没有有效的检索词", "WEB_BAD_REQUEST")
        cfg, key, info = _configuration()
        if not info["available"]:
            raise WebError(info["reason"], info["reason_code"])

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
