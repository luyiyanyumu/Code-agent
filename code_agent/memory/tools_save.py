"""save_memory 工具：模型显式写入持久记忆（s10/s11 的写入通道）。

写入策略（防噪音）：
- 默认写 user scope（跨会话偏好/事实）
- project/workspace scope follows the runtime's platform project memory root;
  session memory follows its private platform root. CLI callers can use their
  explicit workspace when no runtime bridge exists.
- 上下文里永远只描述"已保存"，MEMORY.md 内容不回灌 —— 下轮经检索按需注入
"""

from __future__ import annotations

import asyncio
from typing import Any

from pathlib import Path
from code_agent.memory.store import UserMemory, WorkspaceMemory
from code_agent.tools.base import Tool, ToolContext


class SaveMemoryTool(Tool):
    name = "save_memory"
    description = (
        "Save a durable fact or preference to long-term memory. "
        "Use for stable facts only (user preferences, project decisions), "
        "never for transient conversation content."
    )
    description_zh = (
        "把持久的事实或偏好写入长期记忆。仅用于稳定事实（用户偏好、项目决策），"
        "不要用来记临时对话内容。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["user", "project", "session", "workspace"],
                "description": "user = 个人跨项目偏好（默认）；project = 当前项目共享约定/决策；session = 本会话；workspace = 当前项目或会话的兼容别名。",
            },
            "content": {"type": "string", "description": "要记住的一句话事实/偏好"},
            "category": {"type": "string", "enum": ["general", "preference", "convention", "decision", "context"],
                         "description": "事实分类；不要将临时错误、权限拒绝或未验证推测保存为长期事实。"},
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        if not isinstance(arguments, dict) or set(arguments) - {"scope", "content", "category"}:
            return "[save_memory] 参数必须是对象且不能包含未知字段"
        raw = arguments.get("content")
        if not isinstance(raw, str):
            return "[save_memory] content 必须是字符串"
        content = raw.strip()
        if not content:
            return "[save_memory] content 不能为空"
        if len(content) > 1000:
            return "[save_memory] content 超过 1000 字符，请精炼后重试"

        scope = arguments.get("scope") or "user"
        category = arguments.get("category") or "general"
        if category not in ("general", "preference", "convention", "decision", "context"):
            return "[save_memory] category 不受支持"
        metadata = {"category": category, "source": "agent", "source_session_id": ctx.session_id}
        if scope in ("workspace", "project", "session"):
            if scope == "project" and not ctx.extra.get("project_id"):
                return "[save_memory] 当前会话未绑定项目，不能保存项目记忆"
            root = ctx.extra.get("session_memory_root") if scope == "session" else ctx.extra.get("memory_root")
            memory = WorkspaceMemory(Path(root) if root is not None else ctx.workspace)
            await asyncio.to_thread(memory.save_fact, content, **metadata)
            label = {"workspace": "工作区记忆", "project": "项目记忆", "session": "会话记忆"}[scope]
            return f"[save_memory] 已写入{label}: {content}"
        if scope == "user":
            await asyncio.to_thread(UserMemory(ctx.user_id).save_fact, content, **metadata)
            return f"[save_memory] 已写入用户记忆: {content}"
        return f"[save_memory] 未知 scope: {scope}"


def load_user_memory(user_id: str) -> str:
    from code_agent.memory.store import UserMemory as UM

    return UM(user_id).read_curated()
