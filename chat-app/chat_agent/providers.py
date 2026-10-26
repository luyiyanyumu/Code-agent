"""Conversation providers owned by the standalone chat application."""

from __future__ import annotations

import asyncio
import codecs
import json
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Turn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=dict)


@dataclass
class Chunk:
    text_delta: str = ""
    thinking_delta: str = ""
    turn: Turn | None = None


class ProviderError(RuntimeError):
    """A public error without credentials or remote response bodies."""


def validate_base_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        # Accessing port also validates malformed URL ports.
        parts.port
    except ValueError as exc:
        raise ValueError("API 地址格式无效") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("API 地址必须是完整的 http 或 https 地址")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("API 地址不能含用户名、密码、查询参数或片段；密钥请填写在专用字段")
    return url


class Provider:
    async def stream(self, messages: list, tools: list) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover


class OfflineProvider(Provider):
    """A deterministic, network-free demonstration, never presented as an LLM."""

    async def stream(self, messages: list, tools: list) -> AsyncIterator[Chunk]:
        prompt = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, list):
                    prompt = "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text")
                else:
                    prompt = str(content or "")
                break
        text = "本地演示模式（未调用 AI 模型）。\n\n已收到：" + (prompt[:2000] or "你的消息")
        for start in range(0, len(text), 24):
            await asyncio.sleep(0)
            yield Chunk(text_delta=text[start:start + 24])
        yield Chunk(turn=Turn(text=text, usage={"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}))


class OpenAICompatibleProvider(Provider):
    def __init__(self, profile: dict, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = validate_base_url(profile.get("base_url", ""))
        self.api_key = str(profile.get("api_key", ""))
        self.model = str(profile.get("model", ""))
        if not self.model:
            raise ValueError("请配置模型名称")
        self.timeout = float(profile.get("request_timeout", 120))
        self.transport = transport

    async def _lines(self, response: httpx.Response) -> AsyncIterator[str]:
        """Bound response/line buffers even when a peer omits SSE newlines."""
        received = 0
        pending = ""
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        async for data in response.aiter_bytes():
            received += len(data)
            if received > 8 * 1024 * 1024:
                raise ProviderError("模型响应超过大小限制")
            pending += decoder.decode(data)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                if len(line) > 2 * 1024 * 1024:
                    raise ProviderError("模型流式事件超过大小限制")
                yield line.removesuffix("\r")
            if len(pending) > 2 * 1024 * 1024:
                raise ProviderError("模型流式事件超过大小限制")
        pending += decoder.decode(b"", final=True)
        if pending:
            yield pending.removesuffix("\r")

    async def stream(self, messages: list, tools: list) -> AsyncIterator[Chunk]:
        payload = {"model": self.model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = [tool if tool.get("type") == "function" else {"type": "function", "function": tool} for tool in tools]
        headers = {"Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        texts: list[str] = []
        calls: dict[int, dict] = {}
        usage: dict = {}
        finish = "end_turn"
        saw_finish = False
        max_event_chars = 2 * 1024 * 1024
        event_lines: list[str] = []
        event_chars = 0

        def decode_event(lines: list[str]) -> dict | None:
            data = "\n".join(lines)
            if not data or data == "[DONE]":
                return None
            try:
                event = json.loads(data)
            except (ValueError, TypeError) as exc:
                raise ProviderError("模型服务返回了无效的流式数据") from exc
            if not isinstance(event, dict):
                raise ProviderError("模型服务返回了无效的流式数据")
            if "error" in event:
                raise ProviderError("模型服务返回错误，请检查模型、访问权限与服务状态")
            return event

        def parse_event(event: dict):
            nonlocal usage, finish, saw_finish
            remote_usage = event.get("usage")
            if isinstance(remote_usage, dict):
                details = remote_usage.get("prompt_tokens_details") or {}
                usage = {
                    "input_tokens": int(remote_usage.get("prompt_tokens", 0) or 0),
                    "output_tokens": int(remote_usage.get("completion_tokens", 0) or 0),
                    "cached_tokens": int(details.get("cached_tokens", remote_usage.get("prompt_cache_hit_tokens", 0)) or 0),
                }
            choices = event.get("choices") or []
            if not choices:
                return []
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ProviderError("模型服务返回了无效的消息")
            if choice.get("finish_reason") is not None:
                saw_finish = True
                reason = choice["finish_reason"]
                finish = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens"}.get(reason, str(reason))
            delta = choice.get("delta") or {}
            chunks = []
            text = delta.get("content")
            if isinstance(text, str) and text:
                texts.append(text)
                chunks.append(Chunk(text_delta=text))
            thinking = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(thinking, str) and thinking:
                chunks.append(Chunk(thinking_delta=thinking))
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                if not isinstance(index, int) or index < 0 or index > 100:
                    raise ProviderError("模型服务返回了无效的工具调用")
                current = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if len(calls) > 16:
                    raise ProviderError("模型一次请求的工具超过数量限制")
                if call.get("id"):
                    current["id"] = str(call["id"])
                fn = call.get("function") or {}
                current["name"] += str(fn.get("name") or "")
                current["arguments"] += str(fn.get("arguments") or "")
                if len(current["id"]) > 256 or len(current["name"]) > 256 or len(current["arguments"]) > 2 * 1024 * 1024:
                    raise ProviderError("模型工具参数超过大小限制")
            return chunks

        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport, follow_redirects=False) as client:
                async with client.stream("POST", self.base_url + "/chat/completions", headers=headers, json=payload) as response:
                    if not response.is_success:
                        raise ProviderError(f"模型服务请求失败（HTTP {response.status_code}），请检查连接设置")
                    if "text/event-stream" not in response.headers.get("content-type", "").lower():
                        raise ProviderError("模型服务没有返回流式响应，请检查 API 地址是否为兼容 Chat Completions 的入口")
                    async for line in self._lines(response):
                        if line == "":
                            if "\n".join(event_lines) == "[DONE]":
                                event_lines = []
                                break
                            event = decode_event(event_lines)
                            event_lines = []
                            event_chars = 0
                            if event is not None:
                                for chunk in parse_event(event):
                                    yield chunk
                        elif line.startswith("data:"):
                            part = line[5:]
                            if part.startswith(" "):
                                part = part[1:]
                            event_chars += len(part)
                            if event_chars > max_event_chars:
                                raise ProviderError("模型流式事件超过大小限制")
                            event_lines.append(part)
                    # Some compatible servers omit the final blank separator.
                    if event_lines:
                        event = decode_event(event_lines)
                        if event is not None:
                            for chunk in parse_event(event):
                                yield chunk
        except httpx.TimeoutException as exc:
            raise ProviderError("模型服务响应超时，请稍后重试或检查连接设置") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("无法连接模型服务，请检查 API 地址和网络连接") from exc
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            raise ProviderError("模型服务返回的数据不符合兼容接口格式") from exc
        if not saw_finish:
            raise ProviderError("模型响应意外中断，请重试；已收到的内容会保留")
        if not texts and not calls:
            raise ProviderError("模型服务返回了空响应，请重试或检查模型配置")
        tool_calls = []
        for index, call in sorted(calls.items()):
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except ValueError as exc:
                raise ProviderError("模型返回的工具参数不是有效 JSON") from exc
            if not isinstance(arguments, dict) or not call["name"]:
                raise ProviderError("模型返回的工具调用无效")
            tool_calls.append(ToolCall(id=call["id"] or f"call_{index}", name=call["name"], arguments=arguments))
        yield Chunk(turn=Turn(text="".join(texts), tool_calls=tool_calls, stop_reason=finish, usage=usage))


def create_provider(profile: dict) -> Provider:
    provider = profile.get("provider", "offline")
    if provider == "offline":
        return OfflineProvider()
    if provider in {"openai-compatible", "deepseek"}:
        return OpenAICompatibleProvider(profile)
    raise ValueError("未知的模型服务类型")
