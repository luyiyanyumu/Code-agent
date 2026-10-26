"""LLM 编译（M14）：把原始资料编译成结构化 wiki 页面。

区别于"原文分块入库"：这里先用 LLM 把长文**重写**成 wiki 形态——按主题拆页、
每页带摘要、页面之间用 [[链接]] 交叉引用、去掉寒暄与重复。编译产物进隔离区
（origin=llm），人工审核后「采纳」进正式区。

纯逻辑层：provider 由调用方注入，便于测试（传 mock provider）。
"""

from __future__ import annotations

import re
from typing import Any

#: 分页标记：===PAGE: 标题===
_PAGE_RE = re.compile(
    r"^\s*={2,}\s*PAGE\s*[:：]\s*(.+?)\s*={2,}\s*$", re.MULTILINE | re.IGNORECASE
)

COMPILE_SYSTEM = """你是企业知识库的编辑，负责把原始资料编译成结构化的 wiki 页面。

输出格式（严格遵守）：
===PAGE: 页面标题===
（2-3 句摘要，独立成段）
（正文：保留关键事实、数字、步骤、约束；删掉寒暄、重复与无关内容）

规则：
1. 按主题拆分：每个独立主题一个页面，标题简短明确
2. 相关页面之间用 [[页面标题]] 建立交叉链接
3. 只写资料中真实存在的信息，不要编造补全
4. 直接输出页面，不要解释你的处理过程，不要输出额外前后缀"""


def parse_pages(text: str) -> list[dict[str, str]]:
    """解析 `===PAGE: 标题===` 分页格式。无分页标记则整篇当一页。"""
    text = (text or "").strip()
    if not text:
        return []
    matches = list(_PAGE_RE.finditer(text))
    if not matches:
        return [{"title": "编译稿", "content": text}]
    pages: list[dict[str, str]] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            pages.append({"title": title or f"页面{i + 1}", "content": body})
    return pages


async def compile_document(
    source: str, provider: Any, title: str = ""
) -> list[dict[str, str]]:
    """调 LLM 把 source 编译成 wiki 页面列表（[{title, content}]）。"""
    source = (source or "").strip()
    if not source:
        return []
    messages = [
        {"role": "system", "content": COMPILE_SYSTEM},
        {"role": "user", "content": f"原始资料标题：{title or '（无）'}\n\n{source}"},
    ]
    turn = await provider.chat(messages, [])
    return parse_pages(turn.text or "")
