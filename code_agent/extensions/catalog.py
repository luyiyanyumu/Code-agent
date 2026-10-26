"""能力目录（M10）：把散落各处的「工具与扩展」聚合成一份可展示、可审计的清单。

平台的能力来源有五个，此前只有分散的查询入口（registry.names()、skills.list()、
mcp.clients…），谁也说不全「这个智能体现在到底会些什么」。这里做统一聚合：

    内置工具  builtin   平台自带（bash / read_file / write_file / 记忆 / 交付 / 加载技能）
    MCP 工具  mcp       外部 MCP server 提供的工具，名字带 mcp__<server>__ 前缀
                        （server 由插件声明时也仍算 mcp，origin 记为 plugin:<name>）
    插件工具  plugin    插件包 tools.py 提供的工具（默认关闭，见 plugins.py）
    技能      skill     SKILL.md，进 system prompt 目录 + load_skill 懒加载
    专家      expert    EXPERT.md，机制同上
    插件清单  plugin    插件本身（含声明式能力：内嵌技能 / MCP / 放行名单 / prompt）

输出结构稳定，前端「工具」页面直接消费。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_agent.permissions.hooks import BUILTIN_POLICY_KINDS

#: 工具来源的展示名
SOURCE_LABELS = {
    "builtin": "内置工具",
    "mcp": "MCP 连接器",
    "plugin": "插件",
    "skill": "技能",
    "expert": "专家",
}

#: 内置工具的策略文案**不在这里维护**——它是 `permissions/hooks.py` 里
#: `BUILTIN_TOOL_POLICIES` 登记表的派生结果（`BUILTIN_POLICY_KINDS`）。
#:
#: 此前这里长着一份只覆盖 3 个工具（bash / read_file / write_file）的手工映射，
#: 而 PermissionManager 实际注册了 27 个：于是「工具」页对 list_dir / grep /
#: code_* / todo_write / ask_user_question 等十几个**免审批**的工具，一律显示
#: 「默认策略（需人工审批）」。界面在撒谎，用户据此以为每次调用都要点一下。
#: 文案与策略分居两处就一定会漂，所以现在只剩一个真源。
_FALLBACK_LABEL = ("approval", "默认策略（需人工审批）")


def tool_source(tool: Any) -> tuple[str, str]:
    """判定一个工具的来源，返回 (source, origin)。

    插件代码工具在加载时被打上 origin=plugin:<name> 标记；MCP 工具带 origin 属性。

    注意判定顺序：MCP 工具**先**按名字前缀认领，即使它的 server 是插件声明的
    （此时 origin=plugin:<name>），来源依然算 mcp——它是 MCP 协议工具，只是
    接入点来自插件；origin 已经把"谁带来的"记清楚了。
    """
    origin = str(getattr(tool, "origin", "") or "")
    name = str(getattr(tool, "name", ""))
    if name.startswith("mcp__") or hasattr(tool, "raw_name"):
        return "mcp", origin or "mcp.json"
    if origin.startswith("plugin:"):
        return "plugin", origin
    return "builtin", origin or "platform"


def policy_label(
    tool: Any,
    extra_policies: dict[str, Any] | None = None,
    policy_kinds: dict[str, tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """该工具走什么权限。返回 (kind, 可读说明)。

    kind: allow（免审批）/ approval（需人工审批）/ policy（按参数判定）

    判定顺序：
      1. 覆盖表（插件放行名单 / 可信 MCP / 用户在「工具」页的设置）优先——
         它才是运行时真正生效的那一层；
      2. 其余查内置登记表的派生文案 `BUILTIN_POLICY_KINDS`（与 PermissionManager 同源）。
    """
    name = str(getattr(tool, "name", ""))
    extra = extra_policies or {}
    pol = extra.get(name)
    if pol is not None:
        # 覆盖表里既可能是放行，也可能是用户显式改成的「需审批」
        if type(pol).__name__ == "DefaultPolicy":
            return "approval", "需人工审批（页面设置）"
        return "allow", "免审批放行（显式声明）"
    kinds = BUILTIN_POLICY_KINDS if policy_kinds is None else policy_kinds
    if name in kinds:
        return kinds[name]
    return _FALLBACK_LABEL


def _scan_plugin_skills(plugin_path: Path) -> list[str]:
    d = plugin_path / "skills"
    if not d.is_dir():
        return []
    return sorted(p.parent.name for p in d.glob("*/SKILL.md"))


def build_catalog(
    *,
    registry: Any,
    skills: Any,
    experts: Any,
    plugins: Any,
    mcp: Any,
    extra_policies: dict[str, Any] | None = None,
    policy_kinds: dict[str, tuple[str, str]] | None = None,
    code_tool_status: dict[str, str] | None = None,
    code_tool_errors: list[str] | None = None,
) -> dict[str, Any]:
    """聚合全部能力来源。所有入参都可为 None（便于局部调用与测试）。

    `policy_kinds` 允许调用方传入**运行时**的策略语义（如
    `PermissionManager.builtin_policy_kinds()`，它含契约闸门包装后的真实结果）；
    不传就用登记表的派生结果 `BUILTIN_POLICY_KINDS`。
    """
    extra = extra_policies or {}

    tools: list[dict[str, Any]] = []
    if registry is not None:
        for tool in getattr(registry, "_tools", {}).values():
            source, origin = tool_source(tool)
            kind, label = policy_label(tool, extra, policy_kinds)
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    # 界面用的中文介绍（没写则为空，前端退回英文描述）
                    "description_zh": getattr(tool, "description_zh", "") or "",
                    "source": source,
                    "origin": origin,
                    "policy": kind,
                    "policy_label": label,
                    "parameters": getattr(tool, "parameters", {}),
                }
            )
    tools.sort(key=lambda t: (t["source"], t["name"]))

    packs: list[dict[str, Any]] = []
    for kind, pack in (("skill", skills), ("expert", experts)):
        if pack is None:
            continue
        for item in pack.list():
            packs.append(
                {
                    "kind": kind,
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "origin": item.get("origin", "local"),
                    # M22：停用的技能/专家仍然列出来（带 enabled=false），页面才能再启用回来；
                    # 它们不在 system prompt 目录里、load 也读不到
                    "enabled": bool(item.get("enabled", True)),
                    # 来自插件的技能：删除要去停用/删除插件，页面上据此禁用"删除"
                    "deletable": not str(item.get("origin", "")).startswith("plugin:"),
                }
            )
    packs.sort(key=lambda p: (p["kind"], p["name"]))

    plugin_list: list[dict[str, Any]] = []
    if plugins is not None:
        for manifest in plugins.list():
            plugin_list.append(manifest.to_dict(skills_found=_scan_plugin_skills(manifest.path)))

    mcp_servers = mcp.catalog() if mcp is not None else []

    by_source: dict[str, int] = {}
    for tool in tools:
        by_source[tool["source"]] = by_source.get(tool["source"], 0) + 1

    packs_by_kind: dict[str, int] = {}
    for pack in packs:
        packs_by_kind[pack["kind"]] = packs_by_kind.get(pack["kind"], 0) + 1

    enabled_plugins = [p for p in plugin_list if p["enabled"] and not p["error"]]

    return {
        "summary": {
            "tools_total": len(tools),
            "by_source": by_source,
            "packs_total": len(packs),
            "packs_by_kind": packs_by_kind,
            "plugins": {
                "total": len(plugin_list),
                "enabled": len(enabled_plugins),
                "with_error": sum(1 for p in plugin_list if p["error"]),
            },
            "mcp": {
                "configured": len(mcp_servers),
                "connected": sum(1 for s in mcp_servers if s["connected"]),
                "tools": sum(s["tools"] for s in mcp_servers),
            },
        },
        "tools": tools,
        "packs": packs,
        "plugins": plugin_list,
        "mcp": mcp_servers,
        "code_tools": {
            "status": code_tool_status or {},
            "errors": code_tool_errors or [],
        },
        "labels": SOURCE_LABELS,
    }


def find_tool(catalog: dict[str, Any], name: str) -> dict[str, Any] | None:
    for tool in catalog.get("tools", []):
        if tool["name"] == name:
            return tool
    return None
