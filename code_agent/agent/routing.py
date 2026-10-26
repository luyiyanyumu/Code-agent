"""任务类型的轻量工具建议，不是每项任务都必须执行的流程。

关键词分类可能出错，因此只提示当前真实可用的相关工具，不过滤工具或增设门禁。
已有上下文足够时直接推进；只有缺少事实才查询，有独立工作才分派。
权限、规划只读、联网开关与用户禁用设置仍由运行时执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Route:
    """一类任务：中文名 + 推荐工具子集 + 一句提示。"""

    kind: str
    label: str
    tools: tuple[str, ...]
    hint: str


#: 规则顺序即优先级：越具体的任务类型越靠前（"改代码并跑测试"应判成编辑，
#: 而不是判成"跑测试"——跑是手段，改才是任务）。
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "edit",
        re.compile(
            r"改|修改|修复|实现|新增|删除|重构|优化|补上|加上|fix|refactor|implement"
            r"|update|rewrite|patch|rename",
            re.I,
        ),
    ),
    (
        "verify",
        re.compile(r"测试|跑一下|验证|回归|复现|失败|报错|为什么挂|test|verify|reproduce|debug", re.I),
    ),
    (
        "research",
        re.compile(r"搜索|查一下|最新|资料|调研|联网|官网|文档站|search|look ?up|browse|latest", re.I),
    ),
    (
        "locate",
        re.compile(
            r"在哪|哪里定义|谁调用|影响面|架构|地图|梳理|定位|找一下|结构|调用链"
            r"|where|which file|who calls|impact|map|locate|explore",
            re.I,
        ),
    ),
    ("answer", re.compile(r"^\s*(?:解释|说明|为什么|是什么|怎么理解|举例|对比|区别|what|why|how|explain)", re.I)),
)

ROUTES: dict[str, Route] = {
    "edit": Route(
        "edit",
        "改代码",
        ("code_map", "grep", "read_file", "edit_file", "write_file", "bash"),
        "利用已有坐标和上下文完成最小必要修改；缺少事实时再查找，随后验证受影响的行为并交付。",
    ),
    "verify": Route(
        "verify",
        "验证/排错",
        ("bash", "read_file", "grep", "list_dir"),
        "选择与问题最相关的验证或复现，按真实输出定位并解决问题；已有当前版本有效结果时复用，"
        "只有新改动、失败或未解决疑点才补充验证。",
    ),
    "research": Route(
        "research",
        "联网调研",
        ("web_search", "web_fetch", "read_file", "deliver_file"),
        "先搜后读：搜索拿候选，抓取只读真正要引用的那几篇；结论要能对上来源，"
        "不确定的写不确定。",
    ),
    "locate": Route(
        "locate",
        "定位/看结构",
        ("code_map", "grep", "list_dir", "read_file"),
        "优先使用已有文件和坐标；需要结构或入口线索时再搜索或查地图，足够回答问题即可，避免无关的整仓库调查。",
    ),
    "answer": Route("answer", "问答/解释", (), "这类问题通常不需要调工具：直接答，"
                    "不知道的部分就说不知道，不要为了显得在干活而翻仓库。"),
}

#: 默认路由（分类不出来时用）：代码模式会话最常见的是"定位→改→验证"
DEFAULT_ROUTE = ROUTES["edit"]

#: 给模型的提示里最多列几个工具（列全了就变成"工具说明书"，失去提示的意义）
MAX_HINT_TOOLS = 6


def classify(prompt: str) -> Route:
    """按关键词给 prompt 分类。分不出来时给默认路由（不返回 None，避免调用点分叉）。"""
    text = (prompt or "").strip()
    if not text:
        return DEFAULT_ROUTE
    for kind, pattern in RULES:
        if pattern.search(text):
            return ROUTES[kind]
    return DEFAULT_ROUTE


def route_hint(route: Route, available: Iterable[str] | None = None, *, role: str = "") -> str:
    """把路由写成一行给模型的提示。

    `available` 是**当前真实可用**的工具名：只提示其中存在的那些，避免提示里出现
    一个被用户禁用/联网关闭的工具——那样提示反而在诱导模型去调不存在的工具。
    提示正文里刻意**不写工具名**（名字只出现在上面那段推荐列表里）：正文写了名字，
    过滤就漏了，模型照样会去调一个不在 schema 里的工具。
    """
    tools, hint = route.tools, route.hint
    if role == "lead" and route.kind in ("edit", "verify"):
        hint = (
            "简单、边界明确的任务直接实施、验证后交付。" + route.hint
            + "复杂任务或责任范围独立的子任务才按需委派；当前版本已有充分验证证据时不机械重跑。"
            "遵守当前工具、权限、只读规划和工作树边界。"
        )
    available_names = None if available is None else set(available)
    names = [n for n in tools if available_names is None or n in available_names]
    names = names[:MAX_HINT_TOOLS]
    head = f"[工具路由｜{route.label}]"
    if not names:
        return f"{head} {hint}"
    return f"{head} 按需可用：{'、'.join(names)}。{hint}"
