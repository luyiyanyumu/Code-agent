"""统一事件与模型层。

设计要点（设计文档 §2）：
- Event 是 JSONL 轨迹与 WS 流共用的信封，seq 会话内单调递增，是断线重续的游标。
- ToolCall / ModelTurn 是 Provider 归一化模型：任何厂商响应先转成这两个，
  agent loop 只认这两个 —— "loop 稳定，provider 可换"。
- 消息历史使用 OpenAI Chat Completions 形状作为内部规范格式
  （DeepSeek / OpenAI-compatible 原生支持，其他 provider 在适配层转换）。
"""

from __future__ import annotations

import secrets
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from code_agent.models.approval import executable_arguments


def new_id(prefix: str = "") -> str:
    """生成短随机 id（tool_call / session / approval 共用）。"""
    return f"{prefix}{secrets.token_hex(5)}"


class EventType(str, Enum):
    user_message = "user_message"
    assistant_delta = "assistant_delta"    # M2 起流式输出用
    thinking_delta = "thinking_delta"      # 推理模型的思考流（qwen3/DeepSeek-R1 等），只实时展示、不落盘
    assistant_message = "assistant_message"
    tool_use = "tool_use"
    tool_result = "tool_result"
    approval_request = "approval_request"
    approval_decided = "approval_decided"
    #: 提问/回答（M31）：ask_user_question 工具的问与答都**落轨迹**，
    #: 这样"刷新后那张提问卡还在不在"由轨迹推导，不需要额外的待办表。
    question_request = "question_request"
    question_answered = "question_answered"
    error = "error"
    done = "done"
    session_meta = "session_meta"


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0  # 提示词缓存命中部分（含在 input_tokens 内）

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class ToolCall(BaseModel):
    """一次工具调用请求（由模型产生）。"""

    id: str = Field(default_factory=lambda: new_id("tc_"))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelTurn(BaseModel):
    """一轮模型响应的归一化结果。"""

    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | tool_use | max_turns | max_tokens | error
    usage: TokenUsage = Field(default_factory=TokenUsage)
    # Provider protocol state is memory-only. It must never become the model's
    # visible answer, an event payload, or a serialized session transcript.
    provider_payload: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class Event(BaseModel):
    """统一事件信封：JSONL 轨迹与 WS 流共用。"""

    seq: int
    type: EventType
    ts: float = Field(default_factory=time.time)
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 内部规范消息格式（OpenAI Chat Completions 形状）
# ---------------------------------------------------------------------------

class ProviderPayload(dict):
    """Opaque in-memory protocol state with a deliberately empty JSON shape.

    Request diagnostics routinely JSON-serialize message dictionaries before
    sending them to a provider. Storing private reasoning in an ordinary nested
    dict would persist it there. Only the provider's HTTP encoder opens this
    carrier; normal dict/repr/JSON operations see no reasoning bytes.
    """

    __slots__ = ("_reasoning_content",)

    def __init__(self, reasoning_content: str):
        super().__init__()
        self._reasoning_content = reasoning_content

    def openai_fields(self) -> dict[str, str]:
        return {"reasoning_content": self._reasoning_content}

    @property
    def size_chars(self) -> int:
        """Budget the full protocol state without exposing its contents."""
        return len(self._reasoning_content)

    def __deepcopy__(self, memo):
        copied = type(self)(self._reasoning_content)
        memo[id(self)] = copied
        return copied


def user_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def system_message(content: str) -> dict[str, Any]:
    return {"role": "system", "content": content}


def assistant_message(turn: ModelTurn) -> dict[str, Any]:
    """把归一化的 ModelTurn 转回规范 assistant 消息（含 tool_calls）。"""
    msg: dict[str, Any] = {"role": "assistant", "content": turn.text}
    reasoning = turn.provider_payload.get("reasoning_content")
    if isinstance(reasoning, str):
        msg["_provider_payload"] = ProviderPayload(reasoning)
    if turn.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json_dumps(executable_arguments(tc.arguments))},
            }
            for tc in turn.tool_calls
        ]
    return msg


def tool_message(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
