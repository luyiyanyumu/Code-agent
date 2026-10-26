"""知识库工具（M12/M14）：kb_search 检索 + kb_write 沉淀（LLM wiki）。

- kb_search：模型需要更多/更精准的知识时主动二次检索（与 loop 的自动注入互补）。
- kb_write ：模型把新学到的知识沉淀进知识库的**隔离区**（origin=llm），
  供后续检索复用；隔离区不混入人工维护的正式库，管理员审核后「采纳」。
"""

from __future__ import annotations

from typing import Any

from code_agent.knowledge.kb import DEFAULT_TOP_K, KnowledgeBase
from code_agent.knowledge.store import AREA_HUMAN, AREA_LLM
from code_agent.tools.base import Tool, ToolContext

MAX_KB_CONTENT = 20000


class KbSearchTool(Tool):
    name = "kb_search"
    description = (
        "Search the enterprise knowledge base for relevant content. "
        "Use when the answer may come from uploaded/imported knowledge documents "
        "or from knowledge saved earlier."
    )
    description_zh = (
        "检索企业知识库中的相关内容。当答案可能来自上传/导入的知识文档、"
        "或此前沉淀的知识时使用（隔离区内容会标注「待审核」）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "top_k": {"type": "integer", "description": f"Number of results (default {DEFAULT_TOP_K})"},
        },
        "required": ["query"],
    }

    def __init__(self, kb: KnowledgeBase):
        self._kb = kb

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        query = (arguments.get("query") or "").strip()
        if not query:
            return "[kb_search] query 不能为空"
        top_k = int(arguments.get("top_k") or DEFAULT_TOP_K)
        hits = await self._kb.search(query, top_k=top_k)
        if not hits:
            return "[kb_search] 知识库中没有相关内容"
        lines = [f"知识库检索到 {len(hits)} 条相关内容："]
        for i, h in enumerate(hits, 1):
            area = "" if h.get("origin") == AREA_HUMAN else "（隔离区·待审核）"
            lines.append(f"\n[{i}] 《{h['title']}》{area}(相关度 {h['score']})\n{h['text'][:1500]}")
        return "\n".join(lines)


class KbWriteTool(Tool):
    name = "kb_write"
    description = (
        "Save a durable piece of knowledge into the knowledge base staging area. "
        "Use when you learned something reusable (a fact, procedure, or decision) "
        "that would help answer similar questions later. A human reviews it before it "
        "becomes authoritative."
    )
    description_zh = (
        "把一条可复用的知识沉淀进知识库的隔离区。当你学到值得记住的事实、流程或决策、"
        "日后能帮上类似问题时使用；内容先进隔离区，人工审核后才转正式区。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short page title"},
            "content": {"type": "string", "description": "Knowledge body in Markdown"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags",
            },
        },
        "required": ["title", "content"],
    }

    def __init__(self, kb: KnowledgeBase):
        self._kb = kb

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        title = (arguments.get("title") or "").strip()
        content = (arguments.get("content") or "").strip()
        if not title or not content:
            return "[kb_write] title 与 content 都不能为空"
        if len(content) > MAX_KB_CONTENT:
            return f"[kb_write] content 过长（>{MAX_KB_CONTENT} 字符），请拆成多条"
        tags = arguments.get("tags") or []
        doc = self._kb.create(
            title,
            content,
            tags,
            origin=AREA_LLM,
            source=f"session:{ctx.session_id}" if ctx.session_id else "llm",
        )
        return (
            f"[kb_write] 已沉淀到知识库隔离区（待人工审核后采纳）："
            f"{doc['id']}《{title}》"
        )
