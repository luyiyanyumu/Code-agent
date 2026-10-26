"""取网页正文（M19）：`web_fetch` 的实现。

参考 deepseek harness 的 `web-fetch-http`：HTTP GET → HTML 转成模型可读的文本 →
**有界截断**（并如实标注被截断），而不是把整个页面塞进上下文。

三个刻意的约束：

1. **SSRF 防护**：只允许 http/https，解析出的 IP 落在回环 / 私有 / 链路本地 / 保留段
   一律拒绝。模型可以自由构造 URL，不加这道闸就等于给"读取内网服务"开了口子
   （`http://127.0.0.1:8765/...`、云元数据 `169.254.169.254` 都是典型目标）。
2. **有界**：重定向次数、总字节数、超时都有上限；超限时返回 `truncated=True`，
   而不是静默半截。
3. **不做 JS 渲染**：拿到什么给什么。静态 HTML 之外的内容（SPA 页面）会缺内容——
   如实写进 `note`，不让模型以为"这个页面是空的"。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from code_agent.web.search import USER_AGENT

#: 正文上限（字符）：超过就截断并标注
MAX_TEXT_CHARS = 20000
#: 下载上限（字节）：超过直接停，避免拿一个几百 MB 的文件
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 30.0

#: 常见的内容容器：优先正文，避免把导航/页脚一起吞进来
_CONTENT_HINTS = ("<article", "<main", 'id="content"', 'class="content"', "<body")


class FetchError(Exception):
    """取正文失败。"""


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int
    content_type: str
    title: str
    text: str
    truncated: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "content_type": self.content_type,
            "title": self.title,
            "text": self.text,
            "truncated": self.truncated,
            "note": self.note,
        }


def _is_public_host(host: str) -> tuple[bool, str]:
    """SSRF 闸门：只放行公网地址。

    `socket.getaddrinfo` 会解析 DNS，所以"域名指向 127.0.0.1"这种也拦得住。
    """
    if not host:
        return False, "URL 里没有主机名"
    if host.lower() in ("localhost", "localhost.localdomain"):
        return False, "拒绝访问本机地址"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        return False, f"域名解析失败：{exc}"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False, f"拒绝访问非公网地址（{addr}）"
    return True, ""


_TAG_RE = re.compile(r"<(script|style|noscript|svg|template)[^>]*>.*?</\1>", re.S | re.I)
_BLOCK_RE = re.compile(
    r"</(p|div|section|article|li|tr|h[1-6]|blockquote|pre|table)>|<br\s*/?>", re.I
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_TAG_ANY = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> tuple[str, str]:
    """(标题, 正文文本)。够用就好：去脚本/样式 → 块级换行 → 去标签 → 收空白。"""
    title = ""
    match = _TITLE_RE.search(html)
    if match:
        title = _TAG_ANY.sub("", match.group(1)).strip()
    body = html
    # 优先截取正文容器，减少导航噪声
    for hint in _CONTENT_HINTS:
        idx = body.lower().find(hint)
        if idx > 0:
            body = body[idx:]
            break
    body = _TAG_RE.sub(" ", body)
    body = _BLOCK_RE.sub("\n", body)
    body = _TAG_ANY.sub("", body)
    body = (
        body.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in body.split("\n")]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text


async def fetch_page(url: str, *, max_chars: int = MAX_TEXT_CHARS) -> FetchedPage:
    """取一个公网 URL 的正文（HTML → 文本），有界 + SSRF 防护。"""
    raw = str(url or "").strip()
    if not raw:
        raise FetchError("url 不能为空")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"只支持 http/https，收到 {parsed.scheme or '(无协议)'}")
    ok, reason = await asyncio.to_thread(_is_public_host, parsed.hostname or "")
    if not ok:
        raise FetchError(reason)

    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=False, headers={"user-agent": USER_AGENT}
        ) as client:
            current = raw
            for _ in range(MAX_REDIRECTS + 1):
                response = await client.get(current)
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                    nxt = str(response.headers["location"])
                    current = str(httpx.URL(current).join(nxt))
                    ok, reason = await asyncio.to_thread(_is_public_host, urlparse(current).hostname or "")
                    if not ok:
                        raise FetchError(f"重定向被拒绝：{reason}")
                    continue
                break
            else:
                raise FetchError(f"重定向次数超过 {MAX_REDIRECTS}")
    except httpx.HTTPError as exc:
        raise FetchError(f"请求失败：{exc}") from exc

    content_type = str(response.headers.get("content-type") or "")
    body = response.content[:MAX_BYTES]
    truncated = len(response.content) > MAX_BYTES
    charset = response.encoding or "utf-8"
    try:
        decoded = body.decode(charset, errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")

    if "html" in content_type.lower() or decoded.lstrip().lower().startswith("<!doctype html"):
        title, text = html_to_text(decoded)
    else:
        title, text = "", decoded.strip()

    note = ""
    if truncated:
        note = f"内容超过 {MAX_BYTES // 1024}KB，只取了前一部分"
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
        note = (note + "；" if note else "") + f"正文超过 {max_chars} 字，已截断"
    if not text.strip():
        note = (note + "；" if note else "") + "没有抽到正文（可能是需要 JS 渲染的页面）"

    return FetchedPage(
        url=raw,
        final_url=str(response.url),
        status=response.status_code,
        content_type=content_type,
        title=title,
        text=text,
        truncated=truncated,
        note=note,
    )
