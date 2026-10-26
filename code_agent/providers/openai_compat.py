"""OpenAI 兼容 provider：任意 /chat/completions 协议后端都能接。

适用：DeepSeek、vLLM、Ollama(/v1)、One-API、内网自建网关、Azure OpenAI 兼容层。
只依赖协议，不依赖厂商——这正是"loop 稳定，provider 可换"的落点。

配置（构造参数优先于环境变量）：
    api_key   OPENAI_API_KEY     （DeepSeek 子类用 DEEPSEEK_API_KEY）
    base_url  OPENAI_BASE_URL    默认 https://api.openai.com/v1
    model     OPENAI_MODEL       默认 gpt-4o-mini

可靠性：
- 429 / 5xx / 超时 / 连接错误 -> 指数退避 + 抖动重试（默认 3 次）
- 其他 4xx（鉴权、参数错）不重试，直接抛错——重试无意义还浪费配额
- 尊重 Retry-After 响应头（上限封顶，防被超长等待挂死）
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any, AsyncIterator

import httpx

from code_agent.models.events import ModelTurn, ProviderPayload, TokenUsage, ToolCall
from code_agent.providers.base import (
    Provider, StreamChunk, current_generation_options, is_local_base_url,
)
from code_agent.providers.capacity import effective_model_capacity, estimate_tokens, normalize_capacity
from code_agent.providers.network import connection_error_detail, normalize_proxy_mode

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
TIMEOUT_SECONDS = 120.0
RETRY_MAX_HEADER_WAIT = 20.0

# 触发重试的状态码：限流 + 服务端错误
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Only these model IDs are known to accept the documented thinking switch.
_SIMPLE_ANSWER_MODELS = {"deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash"}


def _cached_tokens(usage: dict[str, Any]) -> int:
    """提取提示词缓存命中数，兼容两种上报格式：
    DeepSeek: prompt_cache_hit_tokens；OpenAI 风格: prompt_tokens_details.cached_tokens。"""
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is not None:
        return int(hit)
    details = usage.get("prompt_tokens_details") or {}
    return int(details.get("cached_tokens") or 0)


def _finish_reason(reason: Any, *, has_tool_calls: bool) -> str:
    """Tool fragments must never hide a truncated or filtered response."""
    if reason is None or reason == "":
        reason = "end_turn"  # Some compatible backends omit finish metadata.
    if not isinstance(reason, str):
        return "error"
    if reason in ("length", "max-tokens", "max_tokens"):
        return "max_tokens"
    if reason in ("stop", "end_turn", "tool_calls", "tool_use", "completed"):
        return "tool_use" if has_tool_calls or reason == "tool_calls" else reason
    return reason


class LLMError(RuntimeError):
    """模型调用失败（已包含重试后的最终原因）。"""


class RetryableStream(Exception):
    """流式请求撞上可重试状态码（429/5xx）。仅在尚未输出增量时才允许重试。"""

    def __init__(self, status: int, retry_after: float | None, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.retry_after = retry_after


class OpenAICompatProvider(Provider):
    name = "openai"
    supports_streaming = True

    #: 子类可覆盖，用于区分环境变量名
    env_api_key = "OPENAI_API_KEY"
    env_base_url = "OPENAI_BASE_URL"
    env_model = "OPENAI_MODEL"
    default_base_url = DEFAULT_BASE_URL
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        *,
        proxy_mode: str = "system",
        context_window: int | None = None,
        max_output_tokens: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int | None = None,
    ):
        self.api_key = api_key or os.environ.get(self.env_api_key, "")
        self.model = model or os.environ.get(self.env_model, self.default_model)
        self.base_url = (base_url or os.environ.get(self.env_base_url, self.default_base_url)).rstrip("/")
        self.proxy_mode = normalize_proxy_mode(proxy_mode)
        # Capture settings on this provider. A router reload only changes future loops.
        self.context_window = normalize_capacity(context_window, "context_window")
        self.max_output_tokens = normalize_capacity(max_output_tokens, "max_output_tokens")
        effective_model_capacity(self.model, self.context_window, self.max_output_tokens)
        # transport 供测试注入 MockTransport；生产为 None（httpx 默认）
        self._transport = transport
        self.max_retries = (
            max_retries if max_retries is not None
            else int(os.environ.get("CODE_AGENT_LLM_RETRIES", "3"))
        )
        # 惰性创建并复用的连接池：见 _get_client()
        self._client: httpx.AsyncClient | None = None
        # 本机 / 内网后端（Ollama、LM Studio、vLLM、内网网关）通常不校验 key
        self.local_backend = is_local_base_url(self.base_url)
        # 是否索取流式 usage（老网关不认 stream_options 会在首次 400 后自动关掉）
        self._stream_options = True

    # ------------------------------------------------------------------
    # 请求构造
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], stream: bool) -> dict[str, Any]:
        # Unseal provider-only state at the final HTTP boundary. Earlier request
        # logging, model dumps and durable transcripts contain no reasoning.
        wire_messages = []
        for message in messages:
            wire = {key: value for key, value in message.items() if key != "_provider_payload"}
            payload = message.get("_provider_payload")
            if message.get("role") == "assistant" and isinstance(payload, ProviderPayload):
                wire.update(payload.openai_fields())
            wire_messages.append(wire)
        messages = wire_messages
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        options = current_generation_options()
        output_cap = self.max_output_tokens
        if options is not None and options.max_output_tokens is not None:
            output_cap = min(output_cap, options.max_output_tokens) if output_cap is not None else options.max_output_tokens
        if output_cap is not None:
            body["max_tokens"] = output_cap
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if (
            options is not None and options.simple_answer and not tools
            and self.model.lower().rsplit("/", 1)[-1] in _SIMPLE_ANSWER_MODELS
        ):
            body["thinking"] = {"type": "disabled"}
            body["tool_choice"] = "none"
        if self.context_window is not None or output_cap is not None:
            capacity = effective_model_capacity(self.model, self.context_window, output_cap)
            # Pinned user instructions and the latest tool result are deliberately not
            # truncated by compaction. Explain an impossible request before sending it.
            input_tokens = estimate_tokens(json.dumps(
                {"messages": body["messages"], "tools": body.get("tools", [])}, ensure_ascii=False,
            ))
            if input_tokens + (output_cap or 0) > capacity["context_window"]:
                raise LLMError("当前消息与工具定义超出上下文预算，请缩短输入或调整上下文窗口和最大输出 Token")
        if stream:
            body["stream"] = True
            # 流式下多数后端只在最后一个 chunk 上报 usage，而且要显式索取：
            # Ollama 默认**完全不报**，DeepSeek/OpenAI 不索取时也可能省略。
            # 少了这段，token 用量与缓存命中率统计会恒为 0（真机上踩过）。
            if self._stream_options:
                body["stream_options"] = {"include_usage": True}
        return body

    def _get_client(self) -> httpx.AsyncClient:
        """惰性创建并复用同一个 AsyncClient，跨请求/跨重试共享连接池。

        每次调用都 new 一个 client 会丢掉 HTTP keep-alive 与连接池复用，
        多轮 loop 的每次 LLM 请求都要重做 TCP+TLS 握手；复用一个 client
        能显著降低时延（尤其流式重试，旧实现每次重试都重建 client）。
        """
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=self._transport, trust_env=self.proxy_mode == "system")
        return self._client

    async def aclose(self) -> None:
        """关闭底层连接池（进程退出 / 应用 shutdown 时调用）。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_key(self) -> None:
        if self.api_key:
            return
        if self.local_backend:
            # 本地服务不校验 key；但 Authorization 头仍要存在，个别网关缺头直接 400
            self.api_key = "not-needed"
            return
        raise LLMError(
            f"{self.env_api_key} 未配置（set {self.env_api_key}=... 或构造时传入 api_key）"
        )

    # ------------------------------------------------------------------
    # 非流式
    # ------------------------------------------------------------------
    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        self._require_key()
        url = f"{self.base_url}/chat/completions"
        body = self._body(messages, tools, stream=False)
        data = await self._post_with_retry(
            self._get_client(), url, self._headers(), body
        )
        return self._normalize(data)

    # ------------------------------------------------------------------
    # 流式（SSE）
    # ------------------------------------------------------------------
    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamChunk]:
        """按 SSE 解析增量；tool_calls 分片按 index 累积，最后合成 ModelTurn。

        重试规则（关键）：只在**尚未向调用方吐出任何增量**时重试。
        一旦已经 yield 过文本，重试会导致内容重复，此时宁可失败——
        这正是"流式不能无脑重试"与"非流式可以随便重试"的区别。
        """
        self._require_key()
        url = f"{self.base_url}/chat/completions"
        body = self._body(messages, tools, stream=True)
        client = self._get_client()

        attempt = 0
        while True:
            attempt += 1
            emitted = False
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            reasoning_seen = False
            tool_buf: dict[int, dict[str, Any]] = {}
            tool_seq = 0  # 后端不报 index 时的人工序号，见下方 tool_calls 解析
            usage = TokenUsage()
            finish_reason: str | None = None

            try:
                async with client.stream("POST", url, headers=self._headers(), json=body) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode("utf-8", errors="replace")
                        if (
                            resp.status_code == 400
                            and self._stream_options
                            and "stream_options" in raw
                        ):
                            # 老网关不认这个字段：关掉它重发一次。代价是这轮没有
                            # usage（统计显示 0），但对话能继续——比直接失败强。
                            self._stream_options = False
                            body = self._body(messages, tools, stream=True)
                            continue
                        if resp.status_code in RETRYABLE_STATUS:
                            raise RetryableStream(resp.status_code, self._retry_after(resp), raw)
                        raise LLMError(f"HTTP {resp.status_code}: {raw[:300]}")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            u = chunk["usage"]
                            usage = TokenUsage(
                                input_tokens=u.get("prompt_tokens", 0),
                                output_tokens=u.get("completion_tokens", 0),
                                cached_tokens=_cached_tokens(u),
                            )
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if isinstance(delta.get("reasoning_content"), str):
                                reasoning_seen = True
                                reasoning_parts.append(delta["reasoning_content"])
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                            if reasoning:
                                # 推理模型的思考流（qwen3 / DeepSeek-R1…）：单独成流，
                                # 不并入正文——它不是回答。已产出思考就不重试，语义同正文。
                                emitted = True
                                yield StreamChunk(thinking_delta=reasoning)
                            text = delta.get("content")
                            if text:
                                text_parts.append(text)
                                emitted = True
                                yield StreamChunk(text_delta=text)
                            for tc in delta.get("tool_calls") or []:
                                fn = tc.get("function") or {}
                                idx = tc.get("index")
                                if idx is None:
                                    # Ollama 的 OpenAI 兼容层不填 index（ollama#7881）。
                                    # 以 id / name 的出现作为"新一次调用"的分界，
                                    # 否则同一轮的多个工具调用会全挤进 0 号槽、参数串味。
                                    if tc.get("id") or fn.get("name"):
                                        idx = tool_seq
                                        tool_seq += 1
                                    else:
                                        idx = tool_seq - 1 if tool_seq else 0
                                slot = tool_buf.setdefault(
                                    idx, {"id": "", "name": "", "arguments": ""}
                                )
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                args = fn.get("arguments")
                                if isinstance(args, str):
                                    slot["arguments"] += args
                                elif args:
                                    # 个别后端把参数当对象直接塞进来（native 风格）
                                    slot["arguments"] = json.dumps(args, ensure_ascii=False)
            except (RetryableStream, httpx.TimeoutException, httpx.TransportError) as exc:
                if emitted:
                    raise LLMError(f"流式传输中断（已输出增量，不再重试）：{connection_error_detail(exc, self.base_url, self.proxy_mode)}") from exc
                if attempt > self.max_retries:
                    raise LLMError(f"模型请求失败（重试 {self.max_retries} 次后放弃）：{connection_error_detail(exc, self.base_url, self.proxy_mode)}") from exc
                retry_after = getattr(exc, "retry_after", None)
                await asyncio.sleep(retry_after or self._backoff(attempt))
                continue
            break

        tool_calls = [
            self._build_tool_call(slot, fallback_id=f"call_{idx}")
            for idx, slot in sorted(tool_buf.items())
        ]
        text = "".join(text_parts) or None
        yield StreamChunk(
            turn=ModelTurn(
                text=text,
                tool_calls=tool_calls,
                stop_reason=_finish_reason(finish_reason, has_tool_calls=bool(tool_calls)),
                usage=usage,
                provider_payload=({"reasoning_content": "".join(reasoning_parts)} if reasoning_seen else {}),
            )
        )

    # ------------------------------------------------------------------
    # 重试与归一化
    # ------------------------------------------------------------------
    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """带指数退避的 POST。只对瞬时故障重试，客户端错误立即失败。"""
        attempt = 0
        last_error: str = ""
        while True:
            attempt += 1
            try:
                resp = await client.post(url, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = connection_error_detail(exc, self.base_url, self.proxy_mode)
                if attempt > self.max_retries:
                    raise LLMError(f"模型请求失败（重试 {self.max_retries} 次后放弃）：{last_error}") from exc
                await asyncio.sleep(self._backoff(attempt))
                continue

            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise LLMError(f"响应不是合法 JSON: {resp.text[:200]}") from exc

            if resp.status_code not in RETRYABLE_STATUS:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if attempt > self.max_retries:
                raise LLMError(f"模型请求失败（重试 {self.max_retries} 次后放弃）：{last_error}")
            await asyncio.sleep(self._retry_after(resp) or self._backoff(attempt))

    def _backoff(self, attempt: int) -> float:
        """指数退避 + 抖动，避免重试风暴同时打同一后端。"""
        base = min(2.0 ** (attempt - 1), 8.0)
        return base + random.uniform(0, 0.4)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return min(float(raw), RETRY_MAX_HEADER_WAIT)
        except ValueError:
            return None

    def _normalize(self, data: dict[str, Any]) -> ModelTurn:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"响应缺少 choices: {json.dumps(data, ensure_ascii=False)[:200]}")
        choice = choices[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}

        tool_calls = [
            self._build_tool_call(tc, raw=True, fallback_id=f"call_{i}")
            for i, tc in enumerate(message.get("tool_calls") or [])
        ]
        return ModelTurn(
            text=message.get("content"),
            provider_payload=({"reasoning_content": message["reasoning_content"]}
                              if isinstance(message.get("reasoning_content"), str) else {}),
            tool_calls=tool_calls,
            stop_reason=_finish_reason(choice.get("finish_reason"), has_tool_calls=bool(tool_calls)),
            usage=TokenUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                cached_tokens=_cached_tokens(usage),
            ),
        )

    @staticmethod
    def _build_tool_call(
        slot: dict[str, Any], raw: bool = False, fallback_id: str = ""
    ) -> ToolCall:
        """把（流式累积的或原样的）tool_call 片段合成 ToolCall。

        fallback_id：后端不给 id 时补一个。id 不是装饰——assistant 消息里的
        tool_calls[].id 与 tool 消息的 tool_call_id 必须配成对，空 id 会被
        严格实现（OpenAI/vLLM）直接判 400。Ollama 就不怎么给 id。
        """
        if raw:  # 非流式：完整结构
            fn = slot.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            return ToolCall(
                id=slot.get("id") or fallback_id,
                name=fn.get("name", ""),
                arguments=_parse_args(args_raw),
            )
        return ToolCall(
            id=slot.get("id") or fallback_id,
            name=slot.get("name", ""),
            arguments=_parse_args(slot.get("arguments") or "{}"),
        )


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_raw": raw}
    except json.JSONDecodeError as exc:
        # Compatible backends can append a redundant closing bracket after
        # otherwise complete argument values. Remove only the reported,
        # structurally unmatched terminal closer; never invent missing JSON
        # or touch any quoted code/command content.
        if isinstance(raw, str) and exc.pos < len(raw) and raw[exc.pos] in "}]":
            if any(char not in "}] \t\r\n" for char in raw[exc.pos + 1:]):
                return {"_raw": raw}
            stack: list[str] = []
            quoted = escaped = False
            for char in raw[:exc.pos]:
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "{[":
                    stack.append(char)
                elif char in "}]":
                    if not stack or stack.pop() != ("{" if char == "}" else "["):
                        return {"_raw": raw}
            expected = "{" if raw[exc.pos] == "}" else "["
            if not quoted and (not stack or stack[-1] != expected):
                try:
                    repaired = json.loads(raw[:exc.pos] + raw[exc.pos + 1:])
                except (json.JSONDecodeError, TypeError):
                    pass
                else:
                    if isinstance(repaired, dict):
                        import logging

                        logging.getLogger(__name__).info(
                            "Removed one unmatched terminal JSON closer at position %d", exc.pos,
                        )
                        return repaired
        return {"_raw": raw}
    except TypeError:
        return {"_raw": raw}
