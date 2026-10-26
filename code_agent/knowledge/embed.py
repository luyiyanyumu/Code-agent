"""知识库向量化（M12；M34 起实现收敛到 `retrieval.embed`）。

历史：本模块最早自带一套 `Embedder`，要求 `CODE_AGENT_EMBED_BASE_URL` **与**
`CODE_AGENT_EMBED_API_KEY` 同时存在才启用，向量只放内存。两个后果：

1. **本地向量模型用不了**——Ollama / LM Studio / vLLM 的 `/embeddings` 不需要 key，
   却被那条判据挡在门外，正好把"我本地有向量模型"这条路堵死；
2. **进程一重启全部重算**——本地模型慢，重启一次要等几十秒到几分钟。

现在这里只是**兼容别名**：真正的实现在 `code_agent/retrieval/embed.py`
（本地优先、免 key、内容哈希持久缓存、失败必降级）。留这一层是因为
`knowledge/__init__.py` 与既有调用方（以及单测）都从 `knowledge.embed` 取 `Embedder`。
"""

from __future__ import annotations

from code_agent.retrieval.embed import (  # noqa: F401
    DEFAULT_MODEL,
    LOCAL_MODELS,
    LOCAL_PRESETS,
    LocalEmbedder,
    VectorStore,
    is_local_url,
    probe_local,
)

#: 兼容名：`from code_agent.knowledge.embed import Embedder`
Embedder = LocalEmbedder

#: 旧常量（有外部引用），保留指向本地默认模型
DEFAULT_EMBED_MODEL = DEFAULT_MODEL
TIMEOUT_SECONDS = 60.0

__all__ = [
    "Embedder",
    "LocalEmbedder",
    "VectorStore",
    "DEFAULT_EMBED_MODEL",
    "LOCAL_PRESETS",
    "LOCAL_MODELS",
    "TIMEOUT_SECONDS",
    "is_local_url",
    "probe_local",
]
