from code_agent.providers.base import Provider, StreamChunk
from code_agent.providers.offline import OfflineProvider
from code_agent.providers.openai_compat import LLMError, OpenAICompatProvider

__all__ = ["Provider", "StreamChunk", "OfflineProvider", "OpenAICompatProvider", "LLMError"]


def create_provider(name: str, **kwargs) -> Provider:
    """工厂：按名称创建 provider。

    deepseek / openai / openai_compat 共用同一套 OpenAI 兼容实现，
    差别只在默认 base_url 与环境变量名——换内网网关只需改 base_url。
    """
    if name == "offline":
        return OfflineProvider(**kwargs)
    if name == "deepseek":
        from code_agent.providers.deepseek import DeepSeekProvider

        return DeepSeekProvider(**kwargs)
    if name in ("openai", "openai_compat"):
        return OpenAICompatProvider(**kwargs)
    raise ValueError(
        f"unknown provider: {name!r} (available: offline, deepseek, openai, openai_compat)"
    )
