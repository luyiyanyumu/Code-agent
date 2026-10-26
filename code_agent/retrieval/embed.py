"""向量后端（M34）：**本地优先** + 按内容哈希的持久缓存 + 失败必降级。

三条约束，每条都对应一个真实的坑：

1. **本地优先，不强制要 API key**。原来 `Embedder.enabled` 要求 base_url 与 api_key
   同时存在——本地 Ollama / LM Studio / vLLM 的 `/embeddings` **不需要 key**，于是
   "我本地有向量模型"这条路被一个多余的判据堵死了。现在：本地地址（127.0.0.1/localhost/
   内网段）免 key；只给地址没给 key 时按本地处理。
2. **缓存按内容哈希落盘**。原来向量只存在内存里，进程一重启全部重算；本地模型慢，
   重启一次要等几十秒到几分钟。现在按 `hash(text) + 模型名` 存 JSONL，
   内容没变就直接复用，内容一变 hash 变、旧向量自然不再被用（不需要"记得清缓存"）。
3. **失败绝不上抛**：拿不到向量就返回空列表，上层走关键词路径。
   "embedding 故障拖垮会话"是最没必要的一种失败。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Iterable

import httpx

from code_agent.retrieval.metadata import text_hash

#: 本地常见 embedding 端点（Ollama 的 OpenAI 兼容层在 /v1）
LOCAL_PRESETS = (
    "http://127.0.0.1:11434/v1",     # Ollama
    "http://127.0.0.1:1234/v1",      # LM Studio
    "http://127.0.0.1:8000/v1",      # vLLM / TGI 常见端口
)
#: 本地常见 embedding 模型名（按顺序探测）
LOCAL_MODELS = ("bge-m3", "nomic-embed-text", "bge-large-zh", "text-embedding-3-small")
DEFAULT_MODEL = "bge-m3"                 # 本地默认（中文与代码都还算稳）
DEFAULT_MODEL_REMOTE = "text-embedding-3-small"  # 远端默认（OpenAI 兼容）
TIMEOUT_SECONDS = 60.0
#: 单次请求最多几段（本地模型批量太大会超时）
BATCH = 32
_LOCAL_HOSTS = ("127.", "localhost", "::1", "192.168.", "10.", "172.16.")


def is_local_url(url: str) -> bool:
    """本地地址判定：本地服务通常不需要 key，判据不能跟远端共用。"""
    text = str(url or "").lower()
    return any(host in text for host in _LOCAL_HOSTS)


def _default_store(model: str) -> Any:
    """默认向量后端（M34 起 SQLite：随机读，载入耗时与规模无关）。

    放在函数里 import 是为了避免 `embed ↔ store` 的循环导入。
    """
    from code_agent.retrieval.store import open_vector_store

    return open_vector_store(model)


class VectorStore:
    """向量缓存：`code_dir()/embeddings/<model-key>.jsonl`，一行一个 {h: 向量}。

    为什么用 JSONL 而不是 pickled 二进制：可以 grep、可以人工删某一行、坏了只丢一行。
    向量维度由模型决定，所以文件名带模型名——换模型不会拿旧向量算出乱码相似度。
    """

    def __init__(self, model: str, path: Path | str | None = None) -> None:
        from code_agent.config import code_dir

        key = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(model))[:60]
        self.model = model
        self.path = Path(path) if path else code_dir() / "embeddings" / f"{key}.jsonl"
        self._items: dict[str, list[float]] = {}
        self._loaded = False
        self.hits = 0
        self.misses = 0

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.is_file():
            return
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue                       # 坏行跳过：一条坏行不该让整批向量失效
            key, vec = item.get("h"), item.get("v")
            if isinstance(key, str) and isinstance(vec, list) and vec:
                self._items[key] = vec

    def get(self, text: str) -> list[float] | None:
        self._load()
        hit = self._items.get(text_hash(text))
        if hit is None:
            self.misses += 1
        else:
            self.hits += 1
        return hit

    def put(self, text: str, vector: list[float]) -> None:
        self.put_many([(text, vector)])

    def put_many(self, items: Iterable[tuple[str, list[float]]]) -> int:
        """批量追加：一次开文件写完（逐条开文件在小批量时看不出来，批量时是主要开销）。"""
        self._load()
        fresh: list[tuple[str, list[float]]] = []
        for text, vector in items:
            if not vector:
                continue
            key = text_hash(text)
            if key in self._items:
                continue
            self._items[key] = vector
            fresh.append((key, vector))
        if not fresh:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                for key, vector in fresh:
                    fh.write(json.dumps({"h": key, "v": vector}, ensure_ascii=False) + "\n")
        except OSError:
            pass                                # 落盘失败不影响本次检索
        return len(fresh)

    def stats(self) -> dict[str, Any]:
        self._load()
        return {
            "model": self.model,
            "cached": len(self._items),
            "path": str(self.path),
            "hits": self.hits,
            "misses": self.misses,
        }


class LocalEmbedder:
    """OpenAI 兼容 `/embeddings` 客户端（本地优先），带内容哈希缓存。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        store: VectorStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("CODE_AGENT_EMBED_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or ""
        ).rstrip("/")
        local = is_local_url(self.base_url)
        # 模型默认值随"本地/远端"而变：本地常见 bge-m3，远端（OpenAI 兼容）用 text-embedding-3-small。
        # 写死一个默认值会让另一半用户拿到"未知模型"错误，而这种错误看起来像网络问题。
        self.model = (
            model
            or os.environ.get("CODE_AGENT_EMBED_MODEL")
            or (DEFAULT_MODEL if local or not self.base_url else DEFAULT_MODEL_REMOTE)
        )
        self.api_key = api_key or os.environ.get("CODE_AGENT_EMBED_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        # 本地地址不需要 key：以前这条判据把"本地有向量模型"直接堵死了
        self.enabled = bool(self.base_url) and (bool(self.api_key) or local)
        self.store = store if store is not None else _default_store(self.model)
        self._client: httpx.AsyncClient | None = None
        self._transport = transport
        self.remote_calls = 0
        self.failures = 0

    # ------------------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=self._transport)
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """向量化一批文本；**失败返回空列表**（上层自动走关键词路径）。"""
        if not self.enabled or not texts:
            return []
        out: list[list[float]] = []
        pending: list[tuple[int, str]] = []
        for index, text in enumerate(texts):
            cached = self.store.get(text)
            out.append(cached or [])
            if cached is None:
                pending.append((index, text))

        for start in range(0, len(pending), BATCH):
            chunk = pending[start : start + BATCH]
            vectors = await self._embed_remote([t for _, t in chunk])
            if not vectors:
                return []                         # 远端失败：整批降级，不返回半截结果
            written: list[tuple[str, list[float]]] = []
            for (index, text), vector in zip(chunk, vectors):
                out[index] = vector
                written.append((text, vector))
            # 批量落盘：一条一提交在 SQL 后端会慢一个数量级（每次 commit 都要 fsync）
            if hasattr(self.store, "put_many"):
                self.store.put_many(written)
            else:
                for text, vector in written:
                    self.store.put(text, vector)
        return out

    async def _embed_remote(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in (1, 2):                # 本地模型偶发冷启动超时：重试一次
            try:
                self.remote_calls += 1
                resp = await self._get_client().post(
                    url, headers=headers, json={"model": self.model, "input": texts}
                )
                resp.raise_for_status()
                data = resp.json()
                vectors = [item["embedding"] for item in data.get("data") or []]
                if len(vectors) == len(texts):
                    return vectors
            except Exception:  # noqa: BLE001 —— 任何异常都只意味着"这次没有向量"
                if attempt == 1:
                    await asyncio.sleep(0.2)
        self.failures += 1
        return []

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "local": is_local_url(self.base_url),
            "base_url": self.base_url,
            "model": self.model,
            "remote_calls": self.remote_calls,
            "failures": self.failures,
            **self.store.stats(),
        }


async def probe_local(*, timeout: float = 1.5, models: tuple[str, ...] | None = None) -> dict[str, Any]:
    """探测本地有没有可用的 embedding 端点（配 `CODE_AGENT_EMBED_BASE_URL` 前先探）。

    返回第一个能拿到向量的 (base_url, model)——不猜、不写死，探到就用。
    `timeout` 刻意短：探测是"顺手确认"，不该让启动或首轮检索等一个不存在的服务。
    """
    tried: list[dict[str, Any]] = []
    # All candidates are fixed loopback HTTP addresses. Do not send discovery
    # through system proxies or repeatedly initialize environment CA bundles.
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        for base in LOCAL_PRESETS:
            for model in models or LOCAL_MODELS:
                connection_failed = False
                try:
                    resp = await client.post(
                        f"{base}/embeddings", json={"model": model, "input": ["ping"]}
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    vectors = [item["embedding"] for item in data.get("data") or []]
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    vectors = []
                    connection_failed = True
                except Exception:  # noqa: BLE001 —— 模型不存在等错误可继续试下个模型
                    vectors = []
                tried.append(
                    {"base_url": base, "model": model, "ok": bool(vectors), "dim": len(vectors[0]) if vectors else 0}
                )
                if vectors:
                    return {"found": True, "base_url": base, "model": model, "dim": len(vectors[0]), "tried": tried}
                if connection_failed:
                    # Changing a model cannot make an absent port reachable.
                    break
    return {"found": False, "tried": tried}


async def autodetect_local(*, timeout: float = 1.5) -> dict[str, Any]:
    """启动时用：没显式配端点就探一次本地，**只返回结果，不改进程环境**。

    为什么刻意不写环境变量：那是个隐蔽的全局副作用——同一个进程里后续构造的
    任何 embedder 都会突然"被启用"并开始打真实的本地模型（测试进程里表现为
    整批用例莫名变慢，而且看不出是谁改的环境）。调用方拿到 (base_url, model)
    自己显式构造即可，行为可追。
    """
    if os.environ.get("CODE_AGENT_EMBED_BASE_URL"):
        base = os.environ["CODE_AGENT_EMBED_BASE_URL"]
        model = os.environ.get("CODE_AGENT_EMBED_MODEL") or (
            DEFAULT_MODEL if is_local_url(base) else DEFAULT_MODEL_REMOTE
        )
        return {"found": True, "skipped": "已显式配置端点", "base_url": base, "model": model}
    return await probe_local(timeout=timeout)


def iter_origins(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(v).strip() for v in values if str(v).strip())
