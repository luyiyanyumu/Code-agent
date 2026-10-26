"""权限策略一致性守卫（P0-a 回归）。

背景——这一波修的是一类**同一个病**的三个症状：策略真相散成三份平行清单，
彼此矛盾，而没有任何一条测试能发现：

1. `permissions/hooks.py` 的 `READ_ONLY_TOOLS` 白名单里躺着 **6 个没有任何类实现**
   的工具名（`list_files` / `get_file_info` / `search_files` / `list_artifacts` /
   `read_artifact` / `knowledge_search`）——其中 `knowledge_search` 连测试都在依赖。
   它读起来像"这个能力已被覆盖"，其实什么也没覆盖；而真正的知识库工具叫 `kb_search`。
2. `skill_list` / `skill_search` 的 docstring 承诺"只读，放行"，实际落到 `DefaultPolicy`
   = `need_approval`：默认档下每次点一下，无人值守时直接失败。
3. `extensions/catalog.py` 的文案表只覆盖 3 个工具，而运行时注册了 27 个——
   「工具」页对十几个**免审批**的工具显示「需人工审批」。

修法是"不要平行清单"：运行时策略、界面文案、只读放行名单全部从
`hooks.BUILTIN_TOOL_POLICIES` 派生。这个文件负责让它们**不能再次分叉**。
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.extensions.catalog import policy_label
from code_agent.permissions.hooks import (
    BUILTIN_POLICY_KINDS,
    BUILTIN_TOOL_POLICIES,
    READ_ONLY_TOOLS,
    PermissionManager,
)
from code_agent.permissions.policy import Decision
from code_agent.tools.base import ToolContext

#: 只在传入 KnowledgeBase 时才注册（见 api/app.py:139-141），所以不在这份默认清单里。
#: 之所以要显式列出来而不是放宽断言：放松了就抓不住幽灵工具名了，而这一波修的就是它。
from code_agent.agent.project_team import TEAM_TOOL_NAMES

CONDITIONALLY_REGISTERED = frozenset({"kb_search", "kb_write"}) | TEAM_TOOL_NAMES


def _ctx(workspace) -> ToolContext:
    return ToolContext(session_id="s_test", workspace=workspace, user_id="u1")


def _registered_builtin_names() -> set[str]:
    from code_agent.api.app import build_registry

    return set(build_registry().names())


# ----------------------------------------------------------------------
# 登记表本身
# ----------------------------------------------------------------------
def test_registry_table_has_no_duplicate_names() -> None:
    """重名会让后者悄悄覆盖前者——装配顺序一变行为就变。"""
    names = [name for name, _key, _ro in BUILTIN_TOOL_POLICIES]
    assert len(names) == len(set(names)), f"登记表里有重名：{names}"


def test_registry_table_covers_exactly_the_real_tools() -> None:
    """**这条就是抓幽灵工具名的那条。**

    表里每个名字都必须对应一个真的会被注册的工具；反过来，每个内置工具也都要在表里
    有明确的策略（不许落到 DefaultPolicy 靠"保守默认"蒙混）。
    """
    table = {name for name, _key, _ro in BUILTIN_TOOL_POLICIES}
    registered = _registered_builtin_names() | CONDITIONALLY_REGISTERED

    ghosts = table - registered
    assert not ghosts, (
        f"登记表里有不存在的工具名：{sorted(ghosts)}。"
        "它们会让『只读档放行名单』看起来覆盖了某个能力，实际什么也没覆盖。"
    )

    uncovered = registered - table
    assert not uncovered, (
        f"这些内置工具在登记表里没有策略（会落到 DefaultPolicy = 每次人工审批）：{sorted(uncovered)}"
    )


def test_every_table_row_has_a_ui_kind() -> None:
    """「工具」页文案必须覆盖整张表，否则页面又会出现"说不出这工具走什么权限"。"""
    missing = [name for name, _key, _ro in BUILTIN_TOOL_POLICIES if name not in BUILTIN_POLICY_KINDS]
    assert not missing, f"这些工具没有界面文案：{missing}"


# ----------------------------------------------------------------------
# 运行时策略
# ----------------------------------------------------------------------
def test_every_builtin_tool_gets_an_explicit_policy(tmp_path) -> None:
    """没有工具该靠 `DefaultPolicy` 兜底——那是"忘了配"的沉默表现，不是设计。"""
    pm = PermissionManager(workspace_root=tmp_path)
    registered = _registered_builtin_names()
    implicit = sorted(n for n in registered if n not in pm._policies)
    assert not implicit, f"这些内置工具没有专用策略，会走默认人工审批：{implicit}"


def test_read_only_allowlist_entries_really_resolve_to_allow(tmp_path) -> None:
    """只读档的"放行"必须是 allow，不能只是"没被拒"。

    这是症状 2 的根因：名单成员若落到 DefaultPolicy，返回的是 need_approval——
    不是 deny，所以旧的 `is not Decision.deny` 断言看不出来，而运行时在等人。
    """
    pm = PermissionManager(workspace_root=tmp_path, mode="read-only")
    for tool in sorted(READ_ONLY_TOOLS):
        res = asyncio.run(pm.evaluate(tool, {}, _ctx(tmp_path)))
        assert res.decision is Decision.allow, (
            f"{tool} 在只读档放行名单里，但解析成了 {res.decision}（{res.reason}）——"
            "名单在撒谎，无人值守场景下这个工具会直接失败"
        )


def test_read_only_denies_tools_that_are_not_read_only(tmp_path) -> None:
    """反向：名单外的工具在只读档必须被拒（含无策略的扩展工具，fail closed）。"""
    pm = PermissionManager(workspace_root=tmp_path, mode="read-only")
    for tool in ("write_file", "edit_file", "bash", "skill_install", "save_memory",
                 "deliver_file", "kb_write", "acceptance_set", "mcp__fs__write_file", "未知工具"):
        res = asyncio.run(pm.evaluate(tool, {}, _ctx(tmp_path)))
        assert res.decision is Decision.deny, f"{tool} 在只读档下应被拒，实际 {res.decision}"


def test_workspace_write_no_longer_prompts_for_readonly_and_platform_tools(tmp_path) -> None:
    """默认档下，只读工具与"只写平台自身数据"的工具不该弹审批。

    症状 2 的用户可见形态就是这一条：`skill_list` 在默认档下要人工点一下。
    """
    pm = PermissionManager(workspace_root=tmp_path, mode="workspace-write")
    for tool in ("read_file", "list_dir", "grep", "code_map", "code_search", "code_graph",
                 "skill_list", "skill_search", "kb_search", "load_skill", "load_expert",
                 "todo_write", "ask_user_question", "feature_brief", "acceptance_set",
                 "contract_check", "save_memory", "deliver_file", "web_search", "web_fetch"):
        res = asyncio.run(pm.evaluate(tool, {}, _ctx(tmp_path)))
        assert res.decision is Decision.allow, f"{tool} 默认档下不该需要审批：{res.reason}"


def test_contract_guard_wraps_the_write_policies(tmp_path) -> None:
    """契约闸门开关仍然只收紧"写契约"，不误伤工作区内的正常写入。"""
    from code_agent.permissions.policy import ContractGuardPolicy

    pm = PermissionManager(workspace_root=tmp_path, contract_guard=True)
    assert isinstance(pm._policies["write_file"], ContractGuardPolicy)
    assert isinstance(pm._policies["edit_file"], ContractGuardPolicy)
    # 闸门开着，但普通文件照常放行
    ok = asyncio.run(pm.evaluate("write_file", {"path": "src/a.py", "content": "x"}, _ctx(tmp_path)))
    assert ok.decision is Decision.allow
    # 契约文件命中即拒
    bad = asyncio.run(pm.evaluate("write_file", {"path": "a.CONTRACT.md", "content": "x"}, _ctx(tmp_path)))
    assert bad.decision is Decision.deny


# ----------------------------------------------------------------------
# 「工具」页文案与运行时同源（症状 3）
# ----------------------------------------------------------------------
class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


def test_catalog_label_agrees_with_runtime_policy(tmp_path) -> None:
    """界面文案必须与运行时判定一致——这是症状 3 的守卫。

    以前 catalog 自带一份只覆盖 3 个工具的文案表，于是十几个免审批工具在页面上
    被标成「需人工审批」。现在两边都读 `BUILTIN_TOOL_POLICIES`，这里把"一致"断言死。
    """
    pm = PermissionManager(workspace_root=tmp_path, mode="workspace-write")

    # 把运行时判定归一到 catalog 的三档词汇
    def runtime_kind(tool: str) -> str:
        return pm.policy_kind(tool)[0]

    for name in sorted(_registered_builtin_names()):
        page_kind, page_text = policy_label(_FakeTool(name))
        assert page_kind == runtime_kind(name), (
            f"「工具」页说 {name} 是 {page_kind}（{page_text}），运行时是 {runtime_kind(name)}"
        )
        assert page_text, f"{name} 的界面文案不能为空"


def test_catalog_label_prefers_the_page_override(tmp_path) -> None:
    """用户在「工具」页把某工具改成「需审批」时，页面要如实显示覆盖结果。"""
    from code_agent.permissions.policy import DefaultPolicy

    label = policy_label(_FakeTool("grep"), {"grep": DefaultPolicy()})
    assert label[0] == "approval" and "页面设置" in label[1]


def test_catalog_label_falls_back_for_extension_tools() -> None:
    """MCP/插件工具没有内置策略 → 页面如实显示"需人工审批"，不许猜成放行。"""
    kind, text = policy_label(_FakeTool("mcp__filesystem__search_files"))
    assert kind == "approval", text
