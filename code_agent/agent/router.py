"""模型路由（s08 起；M36 去掉三档）。

**只有一个默认模型** + 可选的会话级具体模型：
    CODE_AGENT_MODEL_DEFAULT  默认模型名（未配置时用 provider 的默认表）
provider 名可用 CODE_AGENT_PROVIDER 覆盖（offline 用于测试/无 key 演示；
openai_compat 用于内网网关 / vLLM / Ollama 等 OpenAI 兼容后端）。

为什么去掉 lite/default/craft 三档：用户要的是"新建直接用默认，具体模型自己选"。
档位是**按成本猜测任务难度**的间接层——模型名才是用户真正关心的东西，
而"这次用便宜模型"这件事现在由"在输入区选一个具体模型"直接表达。
历史：档位到模型的映射曾由 CODE_AGENT_MODEL_LITE/DEFAULT/CRAFT 三个环境变量控制。
"""

from __future__ import annotations

from code_agent.providers import create_provider
from code_agent.providers.base import Provider
from code_agent.providers.capacity import effective_model_capacity
from code_agent.settings import model_config

#: 支持非 offline 的 provider 名（其余视为配置错误，早失败）
KNOWN_PROVIDERS = ("offline", "deepseek", "openai", "openai_compat")

#: 各 provider 的默认模型（仅在配置里没写模型名时使用）。
#: openai 家族留空 —— 交给 provider 自身的 default_model（gpt-4o-mini 等），
#: 避免把 DeepSeek 的模型名硬塞给内网网关 / vLLM / Ollama。
_MODEL_FALLBACK: dict[str, str] = {
    "deepseek": "deepseek-chat",
    "openai": "",
    "openai_compat": "",
}


class ModelRouter:
    #: 默认 provider 在缓存里的键（没有模型名时用它）
    DEFAULT_KEY = "_provider_default"

    def __init__(self, provider_name: str | None = None):
        # provider_name 显式指定 > config.json > 环境变量 > deepseek
        cfg = model_config()
        self.provider_name = provider_name or cfg["provider"]
        if self.provider_name not in KNOWN_PROVIDERS:
            raise ValueError(
                f"未知 provider：{self.provider_name!r}（可选：{', '.join(KNOWN_PROVIDERS)}）"
            )
        self._cache: dict[str, Provider] = {}
        #: 显式指定的"默认 provider"（测试 / 嵌入用）。**独立于 `_cache`**，
        #: 理由见 `provider_override`。
        self._override: Provider | None = None

    def default_model(self) -> str:
        """默认模型名：config.json 的 model_default > provider 默认表 > ""（用 provider 兜底）。"""
        cfg = model_config()
        return str(cfg.get("model_default") or "").strip() or _MODEL_FALLBACK.get(self.provider_name, "")

    def resolve_default(self) -> Provider:
        """默认模型的 provider。**显式覆盖优先于一切配置。**

        顺序不能反：覆盖是"测试/嵌入明确要求用这个 provider"，而下面按模型名建
        provider 是"从配置推出来的"；让配置赢，覆盖就会在默认模型名非空时静默失效
        （见 `provider_override` 里记的那次事故）。
        """
        if self._override is not None:
            return self._override
        return self._provider(self.default_model() or None)

    def _provider(self, model: str | None) -> Provider:
        """按模型名建/取 provider；`model` 为空 = **不传模型名**，用 provider 自身默认。

        这里必须区分"空"与"某个名字"：openai / openai_compat 家族配了空模型名时，
        应交给 provider 自己的 default_model（gpt-4o-mini 等）。早先我在空值上兜了
        一个字面量 "default"，结果内网网关/OpenAI 真去请求名为 "default" 的模型——
        这种"看起来兜住了其实兜错了"的写法比不兜更糟。
        """
        key = f"model:{model}" if model else self.DEFAULT_KEY
        if key not in self._cache:
            if self.provider_name == "offline":
                self._cache[key] = create_provider("offline")
                return self._cache[key]
            cfg = model_config()
            kwargs: dict[str, object] = {}
            kwargs["proxy_mode"] = cfg.get("proxy_mode", "system")
            kwargs["context_window"] = cfg.get("context_window")
            kwargs["max_output_tokens"] = cfg.get("max_output_tokens")
            if model:
                kwargs["model"] = model
            if cfg.get("api_key"):
                kwargs["api_key"] = cfg["api_key"]
            if cfg.get("base_url"):
                kwargs["base_url"] = cfg["base_url"]
            self._cache[key] = create_provider(self.provider_name, **kwargs)
        return self._cache[key]

    def resolve_name(self, model: str) -> Provider:
        """按**具体模型名**取 provider（配置档案里的模型名走这里）。

        缓存按模型名分：同一个模型名复用连接池，不同模型各自一份。
        """
        name = (model or "").strip()
        if not name:
            raise ValueError("模型名不能为空")
        return self._provider(name)

    @property
    def provider_override(self) -> Provider | None:
        """直接指定"默认 provider"（测试与嵌入用），读回当前覆盖值。

        为什么要有这个公开口子：测试原来写 `router._cache["default"] = Provider()`，
        去档位时缓存键一改，注入就**静默失效**——测试仍跑，只是跑到了真 provider 上，
        表现为一堆莫名其妙的断言差异。把覆盖做成公开属性，改名不会再静默漏掉。

        **为什么它必须独立于 `_cache`（M44 修）**：本属性原来把 provider 存进
        `_cache[DEFAULT_KEY]`，而 `resolve_default()` 走的是
        `self._provider(self.default_model() or None)`——默认模型名一旦非空
        （`CODE_AGENT_MODEL_DEFAULT` 或 config 的 `model_default`），缓存键就变成
        `model:<名字>`，`DEFAULT_KEY` 那个槽**再也不会被查到**：覆盖静默失效，
        provider 换成了按配置新建的那一只。这正是上面那段注释警告过的同一种病，
        去档位（M36）之后又踩了一次。
        真实后果（已定位到具体链路）：`os.environ` 里出现
        `CODE_AGENT_MODEL_DEFAULT=deepseek-v4-pro` 时，测试脚本注入的 provider
        一次都不会被调用——`tests/test_task_routing.py` / `tests/test_todo_tools.py` /
        `tests/test_ws.py` 的 5 条用例因此失败，而它们单独跑都是绿的。
        所以覆盖不再依赖"键刚好对得上"，而是一条独立短路。
        """
        return self._override

    @provider_override.setter
    def provider_override(self, provider: Provider) -> None:
        self._override = provider

    def reload(self) -> None:
        """重读配置并清空 provider 缓存（设置页保存后热生效）。

        SessionRuntime 共用本 router，每次构造下一段 AgentLoop 时重新 resolve，
        因而已有会话的下一次调用也会使用新配置。正在执行的 AgentLoop 仍持有
        原 provider；这里只移除缓存，不关闭它的连接池或中断正在执行的请求。
        """
        cfg = model_config()
        if cfg["provider"] not in KNOWN_PROVIDERS:
            raise ValueError(
                f"未知 provider：{cfg['provider']!r}（可选：{', '.join(KNOWN_PROVIDERS)}）"
            )
        self.provider_name = cfg["provider"]
        self._cache.clear()

    def model_name(self) -> str:
        """当前生效配置实际会用到的模型名（前端显示 + 冒烟校验）。"""
        if self.provider_name == "offline":
            return "offline-mock"
        return self.default_model() or getattr(self.resolve_default(), "model", "") or "unknown"

    def model_capacity(self, provider: Provider | None = None) -> dict[str, int | None]:
        """Use the same captured settings as a loop's provider, including test overrides."""
        resolved = provider if provider is not None else self.resolve_default()
        if self.provider_name == "offline" and self._override is None:
            cfg = model_config()
            return effective_model_capacity("offline-mock", cfg.get("context_window"), cfg.get("max_output_tokens"))
        return effective_model_capacity(
            getattr(resolved, "model", "") or self.model_name(),
            getattr(resolved, "context_window", None),
            getattr(resolved, "max_output_tokens", None),
        )

    async def aclose(self) -> None:
        """关闭所有缓存的 provider 连接池（应用 shutdown 时调用）。

        覆盖值也算在内：它以前就住在缓存里、会被这里关掉，独立出去之后
        必须显式带进来，否则那条连接池会漏。
        """
        providers = list(self._cache.values())
        if self._override is not None:
            providers.append(self._override)
        for provider in providers:
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()
        self._cache.clear()
        self._override = None
