"""联网能力（M19）：检索与取正文。

参考 deepseek harness 的分层：
- `search.DeepSeekSearchProvider`：走 DeepSeek 原生 `web_search_20250305`，不抓 HTML；
- `fetch.fetch_page`：拿到某条结果的正文（两段式的第二段）。

对上只暴露两件事：`availability()`（界面据此禁用开关 + 给原因）与两个工具。
"""

from __future__ import annotations

from code_agent.web.fetch import FetchError, fetch_page
from code_agent.web.search import (
    DeepSeekSearchProvider,
    SearchResult,
    Source,
    WebError,
    api_key,
    availability,
    search_config,
)

__all__ = [
    "DeepSeekSearchProvider",
    "FetchError",
    "SearchResult",
    "Source",
    "WebError",
    "api_key",
    "availability",
    "fetch_page",
    "search_config",
]
