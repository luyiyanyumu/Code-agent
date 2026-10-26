"""`ModelRouter.provider_override` 必须**赢过**配置推导出来的默认模型（M44）。

为什么单独一个文件测这一件事：这个属性是"测试与嵌入"注入 provider 的唯一入口，
而它曾经把值存进 `_cache[DEFAULT_KEY]`。去档位（M36）之后 `resolve_default()` 变成
`self._provider(self.default_model() or None)`——默认模型名一旦非空，缓存键就成了
`model:<名字>`，`DEFAULT_KEY` 那个槽再也不会被查到：**覆盖静默失效**，
provider 换成按配置新建的那一只。

真实后果不是"某条用例报错"，而是最难查的那一类：注入的脚本化 provider 一次都
没被调用，于是
  · `test_task_routing` → `seen[0]` IndexError（provider 没跑）
  · `test_todo_tools` → 工具没落轨迹（`[] == ['in_progress','completed']`）
  · `test_ws` → 审批事件永远不来，243s 后 WebSocketDisconnect
三条用例**单独跑全绿**，只有整跑才红；而触发泄漏的是另一个文件
（`test_rename_compat.py` 里 `migrate_legacy_env()` 直接写 `os.environ`）。
"""

from __future__ import annotations

import asyncio

from code_agent.agent.router import ModelRouter
from code_agent.providers.offline import OfflineProvider


def test_override_wins_when_a_default_model_is_configured(agent_home, monkeypatch) -> None:
    """默认模型名非空时，覆盖仍然生效——这是那次污染的真正防线。"""
    monkeypatch.setenv("CODE_AGENT_MODEL_DEFAULT", "deepseek-v4-pro")
    router = ModelRouter(provider_name="offline")
    # 前提：配置确实生效（默认模型名不是空的，所以缓存键会走 model:<名字>）
    assert router.default_model() == "deepseek-v4-pro"

    injected = OfflineProvider()
    router.provider_override = injected
    assert router.resolve_default() is injected, (
        "provider_override 被配置推导出来的模型名绕过了——"
        "注入会静默失效，测试会跑到真 provider 上"
    )


def test_override_wins_when_no_default_model_is_configured(agent_home, monkeypatch) -> None:
    """默认模型名为空时也必须生效（两条路都要覆盖，否则修了一半）。"""
    monkeypatch.delenv("CODE_AGENT_MODEL_DEFAULT", raising=False)
    router = ModelRouter(provider_name="offline")
    assert router.default_model() == ""

    injected = OfflineProvider()
    router.provider_override = injected
    assert router.resolve_default() is injected


def test_without_override_the_configured_model_is_used(agent_home, monkeypatch) -> None:
    """没有覆盖时，仍然按配置的默认模型建 provider（别把正常路径堵死）。"""
    monkeypatch.setenv("CODE_AGENT_MODEL_DEFAULT", "deepseek-v4-pro")
    router = ModelRouter(provider_name="offline")
    assert router.provider_override is None
    assert isinstance(router.resolve_default(), OfflineProvider)


def test_override_survives_reload_and_is_dropped_by_aclose(agent_home, monkeypatch) -> None:
    """reload 不该丢掉注入（它清的是配置缓存）；aclose 必须把它一并关掉并清空。

    aclose 那条不是洁癖：覆盖值以前住在 `_cache` 里、由 aclose 关掉；独立出去之后
    若忘了带上它，那条 provider 的连接池就漏了（应用 shutdown 时才发现）。
    """
    monkeypatch.delenv("CODE_AGENT_MODEL_DEFAULT", raising=False)
    router = ModelRouter(provider_name="offline")
    injected = OfflineProvider()
    router.provider_override = injected

    router.reload()
    assert router.resolve_default() is injected, "reload 把注入一起清掉了"

    asyncio.run(router.aclose())
    assert router.provider_override is None
