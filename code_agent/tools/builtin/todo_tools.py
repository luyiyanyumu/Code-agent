"""任务清单工具（M30）：模型自己维护一份**整份快照**，界面据此显示当前进度。

原理（照 deepseek harness 的 `dsh-tool-todo` 抄的作业，逐条对上）：

1. **整份替换，没有局部更新**。每次调用都发完整列表，服务端与界面只认最后一次
   → 天然幂等：不需要 diff、不需要合并、不用怕"漏了一条更新"，断线重连与刷新后
   按轨迹回放得到的都是同一份清单。
2. **状态由模型自己判定**（pending / in_progress / completed），并且"做完一项立刻
   标完成，不要攒着一起标"——攒着标等于把真实进度藏起来，界面上的进度条就成了假象。
3. **列表是短暂的过程视图**：它回答"这一轮做到第几步"，不是永久计划。
   界面只认**本轮**（最后一条真实用户消息之后）的那次写入，新的一轮从空开始。

与 M29 验收清单的分工（两者互补，别混）：

| | 任务列表（`todo_write`） | 验收清单（`acceptance_set`） |
| --- | --- | --- |
| 回答 | 现在做到第几步 | 做完的标准是什么 |
| 生命周期 | **每条 prompt 重置**（过程视图） | **跨 prompt 存续**（任务级契约） |
| 谁消费 | 人（看进度） | 门禁 / 收口自查（判据） |

这个工具**不落自己的事件类型**：`tool_use` 事件里就带着完整参数（`todos`），
轨迹本身就是唯一事实来源。多造一份并行状态只会多一个会走散的地方。
"""

from __future__ import annotations

from typing import Any

from code_agent.tools.base import Tool, ToolContext, ToolError

#: 合法状态（顺序即界面上的分组顺序）
STATUSES = ("pending", "in_progress", "completed")
#: 状态的中文名（喂回模型时用中文更省 token 也更清楚；界面自己有文案）
STATUS_ZH = {"pending": "待处理", "in_progress": "进行中", "completed": "已完成"}
#: 单条任务文本的长度上限（一行短句；超了是计划没想清楚，不是表达不够）
MAX_CONTENT = 120
#: 一次最多多少条（防止把整个路线图塞进来当"任务清单"）
MAX_ITEMS = 40


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "Record and update a structured task list for the current work. Send the ENTIRE list "
        "every call — it REPLACES the previous list (there are no partial updates, no per-item "
        "edits). Use it to plan multi-step work and show progress: add one todo per concrete step "
        "before you start. Keep AT MOST ONE todo `in_progress` at a time; while work remains, "
        "exactly one active task should be `in_progress`. Mark a todo `completed` the moment it is "
        "done (do not batch completions), and allow no `in_progress` item only once all work is "
        "complete. Skip the list for trivial single-step tasks. "
        "Statuses: `pending` (not started) | `in_progress` (being worked on now) | `completed`. "
        "Note: this is the short step list for the CURRENT message; acceptance criteria for the "
        "whole task are a different thing — use acceptance_set for those."
    )
    description_zh = "任务清单：每次发完整列表（整份替换），界面据此显示当前进度；每条 prompt 重置。"
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": (
                    "The COMPLETE task list, replacing any previous list. "
                    f"At most {MAX_ITEMS} items; each item is {{content, status}}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "What the task is — a short imperative line.",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(STATUSES),
                            "description": "pending (not started) | in_progress (now) | completed (done).",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        items = normalize_todos(arguments.get("todos"))
        counts = count_by_status(items)
        lines = ["任务清单已更新（整份替换）："]
        for item in items:
            mark = {"completed": "✅", "in_progress": "▶", "pending": "○"}[item["status"]]
            lines.append(f"{mark} {item['content']}")
        lines.append(
            f"（已完成 {counts['completed']} · 进行中 {counts['in_progress']} · 待处理 {counts['pending']}）"
        )
        if not items:
            lines.append("列表已清空。")
        return "\n".join(lines)


def count_by_status(items: list[dict[str, str]]) -> dict[str, int]:
    return {status: sum(1 for i in items if i["status"] == status) for status in STATUSES}


def normalize_todos(raw: Any) -> list[dict[str, str]]:
    """校验并规范化模型给的清单。**坏输入一律抛错**，不做静默清洗。

    为什么宁可报错也不"顺手修"：清单是给人看的进度承诺，静默丢掉一条或改掉一个状态，
    界面就会显示一份模型不认为存在的进度。报错会让模型下一轮自己改对（而 [tool error]
    的结果也让它知道刚才那次写入没生效）。
    """
    if raw is None:
        raise ToolError("todos 不能为空（要清空列表就传空数组 []）")
    if not isinstance(raw, list):
        raise ToolError("todos 必须是数组")
    if len(raw) > MAX_ITEMS:
        raise ToolError(f"任务最多 {MAX_ITEMS} 条（当前 {len(raw)} 条）：先想清楚要几步再写")

    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ToolError("每条任务必须是 {content, status} 对象")
        content = str(entry.get("content") or "").strip()
        if not content:
            raise ToolError("content 不能为空")
        if len(content) > MAX_CONTENT:
            raise ToolError(f"content 太长（>{MAX_CONTENT} 字）：写成一行短句")
        status = str(entry.get("status") or "").strip()
        if status not in STATUSES:
            raise ToolError(f"status 必须是 {'/'.join(STATUSES)} 之一（收到 {status!r}）")
        if content in seen:
            raise ToolError(f"任务重复：{content!r}（同一件事只写一条）")
        seen.add(content)
        items.append({"content": content, "status": status})

    active = sum(1 for i in items if i["status"] == "in_progress")
    if active > 1:
        raise ToolError(
            f"最多只能有 1 条 in_progress（当前 {active} 条）："
            "把真正在做的那条标成进行中，其余保持待处理"
        )
    return items


def build_todo_tools() -> list[Tool]:
    return [TodoWriteTool()]
