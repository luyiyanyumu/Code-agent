"""DeepSeek provider —— 就是 OpenAI 兼容协议的一个预设。

DeepSeek 原生支持 OpenAI 兼容的 function calling，所以这里不做任何
协议转换工作，固定 base_url / 环境变量名 / 默认模型，复用父类的
归一化、重试退避与流式解析。仅答复请求显式禁用工具选择。
"""

from __future__ import annotations

from typing import Any

from code_agent.providers.openai_compat import OpenAICompatProvider

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class DeepSeekProvider(OpenAICompatProvider):
    name = "deepseek"

    env_api_key = "DEEPSEEK_API_KEY"
    env_base_url = "DEEPSEEK_BASE_URL"
    env_model = "DEEPSEEK_MODEL"

    default_base_url = DEFAULT_BASE_URL
    default_model = DEFAULT_MODEL

    def _body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], stream: bool) -> dict[str, Any]:
        body = super()._body(messages, tools, stream)
        if not tools:
            body["tool_choice"] = "none"
        return body
