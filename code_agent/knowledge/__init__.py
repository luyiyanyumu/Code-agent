"""知识库（M12/M14）：双区文档 CRUD + 向量/BM25 检索 + LLM wiki。

- KnowledgeStore  文档文件 CRUD（正式区 knowledge/、隔离区 knowledge/_llm/）
- Embedder        OpenAI 兼容 /embeddings 向量化（未配置时自动降级）
- KnowledgeBase   门面：双区分块索引 + 向量/BM25 检索 + 采纳(promote)
- KbSearchTool    模型主动检索工具（kb_search）
- KbWriteTool     模型沉淀工具（kb_write → 隔离区，待审核）
- compile_document 用 LLM 把原始资料编译成结构化 wiki 页面
"""

from code_agent.knowledge.compile import compile_document, parse_pages
from code_agent.knowledge.embed import Embedder
from code_agent.knowledge.kb import KnowledgeBase
from code_agent.knowledge.store import (
    AREA_HUMAN,
    AREA_LLM,
    KnowledgeStore,
    chunk_text,
)
from code_agent.knowledge.tools import KbSearchTool, KbWriteTool

__all__ = [
    "AREA_HUMAN",
    "AREA_LLM",
    "Embedder",
    "KnowledgeBase",
    "KnowledgeStore",
    "KbSearchTool",
    "KbWriteTool",
    "chunk_text",
    "compile_document",
    "parse_pages",
]
