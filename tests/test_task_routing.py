"""任务类型的轻量工具建议与代码执行规程。

这里断言的是这条提示的三个性质：
- 分类规则**优先级明确**（"改代码并跑测试"是编辑任务，不是测试任务）；
- 提示里**只出现真实可用的工具**（联网关掉时不许提示 web_search，
  否则等于诱导模型去调一个 schema 里不存在的工具）；
- 提示真的进了 system prompt，而且**只加提示、不摘工具**。
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.agent.routing import DEFAULT_ROUTE, ROUTES, classify, route_hint
from code_agent.agent.loop import DEFAULT_SYSTEM_PROMPT
from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.config import transcripts_dir
from code_agent.codeintel.context import LEAD_PROTOCOL, PROTOCOL, role_session_suffix, session_suffix
from code_agent.context.safety import ANTI_INJECTION_RULE
from code_agent.models.approval import APPROVAL_INSTRUCTION
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore


def test_classify_picks_the_more_specific_task_type() -> None:
    """规则顺序即优先级：**具体动作词**优先于**不带动作的提问词**。

    "解释一下这个架构" 判成定位而不是问答——它要看代码才能解释，提示"先看地图
    再读代码"比提示"不用调工具"有用得多。
    """
    assert classify("帮我改一下 pkg/mathx.py 的 add 函数并跑测试").kind == "edit"
    assert classify("这个用例为什么挂了？").kind == "verify"
    assert classify("搜一下最新的 hui 图表文档").kind == "research"
    assert classify("add 函数在哪定义的？谁调用它？").kind == "locate"
    assert classify("解释一下这个架构").kind == "locate"
    assert classify("解释一下这段正则是什么意思").kind == "answer"
    assert classify("").kind == DEFAULT_ROUTE.kind


def test_route_hint_lists_only_available_tools() -> None:
    """提示里出现一个被摘掉的工具，等于诱导模型去调不存在的工具。"""
    line = route_hint(ROUTES["research"], ["web_search", "read_file"])
    assert "web_search" in line and "read_file" in line
    assert "web_fetch" not in line and "deliver_file" not in line

    line = route_hint(ROUTES["research"], [])
    assert line.startswith("[工具路由｜联网调研]")
    assert "web_search" not in line, "一个可用的都没有时就不列工具，只留一句提示"


def test_answer_tasks_get_no_tool_list() -> None:
    line = route_hint(ROUTES["answer"], ["bash", "read_file"])
    assert "bash" not in line
    assert "不需要调工具" in line


@pytest.mark.parametrize("kind", ["edit", "verify"])
def test_lead_routes_recommend_available_execution_tools_before_delegation(kind: str) -> None:
    available = ["code_map", "grep", "read_file", "edit_file", "write_file", "bash", "list_dir",
                 "delegate_agent", "wait_agents", "audit_agent"]
    line = route_hint(ROUTES[kind], available, role="lead")
    assert "bash" in line
    if kind == "edit":
        assert "edit_file" in line and "write_file" in line
    assert "直接实施、验证后交付" in line
    assert "责任范围独立的子任务才按需委派" in line
    assert "已有充分验证证据时不机械重跑" in line
    assert "只读规划" in line
    assert "delegate_agent" not in line and "audit_agent" not in line


def test_lead_route_does_not_invent_tools_when_execution_is_filtered() -> None:
    # Iterable is part of the API: consume a generator once, rather than once
    # per suggested tool. Read-only/planning registries must stay truthful.
    available = (name for name in ("read_file", "grep"))
    line = route_hint(ROUTES["edit"], available, role="lead")
    assert "按需可用：grep、read_file。" in line
    for absent in ("code_map", "edit_file", "write_file", "bash", "delegate_agent", "audit_agent"):
        assert absent not in line
    assert "当前工具、权限、只读规划和工作树边界" in line


def test_lead_code_protocol_prefers_delivery_and_preserves_map_and_planning_limits() -> None:
    shared_map = "\n\n[项目地图] pkg/mathx.py:12 · add"
    result = role_session_suffix(PROTOCOL + shared_map, "lead")
    assert result == LEAD_PROTOCOL + shared_map
    assert "由你直接完成修改、相关验证和交付" in result
    assert "复杂任务或存在可独立推进、责任范围不同的子任务时再委派" in result
    assert "已有当前版本的充分验证结果时无需机械重跑" in result
    assert "只读规划阶段仅阅读事实并产出计划" in result
    assert "不修改文件、提交代码或执行会写入项目的命令" in result
    assert "当前工具表中可用的工具，遵守权限与单次审批" in result
    assert "Lead 负责直接只读核查" not in result
    assert "未应用前不能说文件已修改" in result
    assert role_session_suffix(PROTOCOL + shared_map, "subagent") == PROTOCOL + shared_map
    assert role_session_suffix("custom project context", "lead") == "custom project context"


def test_worker_code_protocol_uses_authorized_direct_work_without_mandatory_proposals_or_checklists() -> None:
    # Exercise the actual suffix returned for ordinary code sessions and kept
    # for workers, rather than only the Lead-specific replacement.
    suffix = session_suffix(None, {"id": "protocol-only", "root_path": "."})
    assert role_session_suffix(suffix, "node") == suffix
    assert "用户已授权修改且当前权限允许时直接实施、验证和交付" in suffix
    assert "只有用户要求提案或当前权限仅允许提案时才提供可审查提案" in suffix
    assert "不要求用户额外说一遍“直接写入”" in suffix
    assert "简单任务直接完成最小实现和相关验证，不强制创建验收清单" in suffix
    assert "复杂任务先明确简短、有序的完成标准" in suffix
    assert "已有当前版本、相关范围的有效验证结果可以复用" in suffix
    assert "acceptance_set" not in suffix and "acceptance_check" not in suffix
    assert "人工应用后再运行" not in suffix


def test_worker_code_protocol_retains_planning_approval_and_evidence_boundaries() -> None:
    suffix = session_suffix(None, {"id": "protocol-only", "root_path": "."})
    for required in (
        "只读规划阶段仅阅读事实并产出计划，不修改文件、提交代码或执行会写入项目的命令",
        "不绕过权限或单次审批",
        "当前授权项目与工作树内的文件",
        "未应用提案不能说文件已修改",
        "没有运行的命令不许说通过",
        "说明具体原因和未验证范围",
    ):
        assert required in suffix


def test_code_guidance_uses_known_context_and_recovers_without_fixed_attempt_limits() -> None:
    suffix = session_suffix(None, {"id": "protocol-only", "root_path": "."})
    assert "已知文件或坐标时直接读相关内容" in suffix
    assert "能用已有准确上下文完成的事直接推进" in suffix
    assert "不因固定尝试次数就交回用户" in suffix
    for removed in ("先用 code_map", "read_file 必须带 offset/limit", "同一种失败连续两轮", "一次修复只动一个"):
        assert removed not in suffix
    for kind in ("edit", "verify", "locate"):
        hint = route_hint(ROUTES[kind], ["read_file", "grep", "bash"])
        assert "按需可用" in hint
        assert "先看地图再读代码" not in hint
        assert "code_map" not in hint
    assert "已有坐标和上下文" in route_hint(ROUTES["edit"])
    assert "已有文件和坐标" in route_hint(ROUTES["locate"])


def test_default_guidance_prioritizes_delivery_with_the_existing_security_instructions() -> None:
    assert "直接使用当前工具推进到可交付结果" in DEFAULT_SYSTEM_PROMPT
    assert "遇到失败根据真实输出调整方法" in DEFAULT_SYSTEM_PROMPT
    assert "不把内部自查或运行提示当作用户的新问题" in DEFAULT_SYSTEM_PROMPT
    assert APPROVAL_INSTRUCTION in DEFAULT_SYSTEM_PROMPT
    assert ANTI_INJECTION_RULE in DEFAULT_SYSTEM_PROMPT


@pytest.fixture
def manager(agent_home):
    db = Database(agent_home / "route.db")
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )


def test_route_hint_reaches_the_system_prompt_without_removing_tools(manager) -> None:
    db = manager.session_repo.db
    uid = UserRepo(db).create("u_route", "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    rt = manager.get_runtime(meta["id"], uid)
    seen: list[tuple[str, list[str]]] = []

    class SpyProvider(OfflineProvider):
        async def chat(self, messages, tools):
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            seen.append((system, [t["name"] for t in tools]))
            return OfflineProvider.text_turn("好。")

    rt.router.provider_override = SpyProvider()
    asyncio.run(rt.submit("帮我改一下 add 函数"))

    system, tools = seen[0]
    assert "[工具路由｜改代码]" in system
    assert "edit_file" in system.split("[工具路由")[1]
    # 只加提示：工具集一个都没少（edit_file / bash 仍在 schema 里）
    assert "edit_file" in tools and "bash" in tools


def test_route_hint_omits_web_tools_when_web_is_off(manager) -> None:
    """联网关着时 web_search 不在 schema 里，提示里也不许出现。"""
    db = manager.session_repo.db
    uid = UserRepo(db).create("u_route2", "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    rt = manager.get_runtime(meta["id"], uid)
    seen: list[str] = []

    class SpyProvider(OfflineProvider):
        async def chat(self, messages, tools):
            seen.append(next((m["content"] for m in messages if m["role"] == "system"), ""))
            return OfflineProvider.text_turn("好。")

    rt.router.provider_override = SpyProvider()
    asyncio.run(rt.submit("搜一下最新的图表库文档"))

    hint = seen[0].split("[工具路由")[1]
    assert "web_search" not in hint, hint
    assert "web_fetch" not in hint, hint
