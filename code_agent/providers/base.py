"""Provider 抽象基类。

契约（设计文档 §2）：
    async chat(messages, tools) -> ModelTurn
    async stream(messages, tools) -> AsyncIterator[StreamChunk]   （可选能力）

- messages: 内部规范格式（OpenAI Chat Completions 形状）
- tools:    [{"name", "description", "parameters"}] JSON schema 列表
- 返回:     归一化的 ModelTurn，loop 不感知任何厂商差异

流式设计：**默认实现降级**——不支持流式的 provider 直接跑一次 chat()，作为
单个终结 chunk 产出。这样 loop 只写一套消费逻辑（for chunk in stream(...)），
offline mock 与既有测试无需改动；支持 SSE 的 provider 覆盖 stream() 即可。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator
from urllib.parse import urlsplit

from code_agent.models.events import ModelTurn
from code_agent.providers.capacity import normalize_capacity


@dataclass(frozen=True)
class GenerationOptions:
    """Overrides for one logical model request, isolated from shared providers."""

    max_output_tokens: int | None = None
    simple_answer: bool = False

    def __post_init__(self) -> None:
        normalize_capacity(self.max_output_tokens, "max_output_tokens")


_generation_options: ContextVar[GenerationOptions | None] = ContextVar(
    "code_agent_generation_options", default=None,
)


def current_generation_options() -> GenerationOptions | None:
    return _generation_options.get()


@contextmanager
def generation_options(options: GenerationOptions | None) -> Iterator[GenerationOptions | None]:
    """Temporarily set request options, including across awaited streaming calls."""
    token = _generation_options.set(options)
    try:
        yield options
    finally:
        _generation_options.reset(token)

#: 本机 / 内网地址判定用常量（见 is_local_base_url）
_LOCAL_HOSTNAMES = {"localhost", "0.0.0.0", "::1", "host.docker.internal"}
_LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".internal")
_LOCAL_NET_PREFIXES = ("10.", "192.168.", "169.254.")


def is_local_base_url(url: str) -> bool:
    """base_url 是否指向本机 / 内网地址。

    用途：本机跑起来的 OpenAI 兼容后端（Ollama、LM Studio、vLLM、内网网关）
    普遍不校验 API Key，不该因为"没填 key"就把这套配置判成错误。
    判定只看 host，不看端口——本地服务端口可以随便改。
    """
    if not url:
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES or host.startswith("127."):
        return True
    if host.startswith(_LOCAL_NET_PREFIXES) or host.endswith(_LOCAL_HOST_SUFFIXES):
        return True
    if host.startswith("172."):  # 172.16.0.0/12 私有段
        parts = host.split(".")
        if len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return True
    return False


@dataclass
class StreamChunk:
    """流式增量：思考流 / 文本 delta 之一，或终结 chunk（携带完整 ModelTurn）。

    thinking_delta：推理模型（qwen3、DeepSeek-R1、o 系列…）的思考过程增量。
    走独立字段而不是混进 text_delta——它不是回答，前端要折叠展示，
    回放/统计也只认 text_delta 与终结 turn。
    """

    text_delta: str = ""
    thinking_delta: str = ""
    turn: ModelTurn | None = None


class Provider(ABC):
    name: str = "base"
    supports_streaming: bool = False

    @abstractmethod
    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        """发送一轮对话，返回归一化的模型响应。"""
        raise NotImplementedError

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamChunk]:
        """流式对话。默认降级：一次性调用 chat()，产出单个终结 chunk。"""
        turn = await self.chat(messages, tools)
        yield StreamChunk(turn=turn)
