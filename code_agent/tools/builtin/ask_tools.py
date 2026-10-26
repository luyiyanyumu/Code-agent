"""提问工具（M31）：让模型**停下工作**问人一句，拿到答案再继续。

原理（照 deepseek harness 的 `dsh-tool-ask-user` 抄的作业）：

    execute 里 await 一个"回答"（和审批走同一套挂起/送达机制）
        → 模型这一轮就停在这一次工具调用上（不是"问完继续猜"）
        → 人答完，答案作为**普通 tool result** 喂回 loop，模型接着干

四件事必须做对，缺一件这个功能就会变成噪音或撒谎：

1. **阻塞而不是并行**：模型问完必须等；否则它会一边"等答案"一边继续改代码，
   答案回来时改动已经做完了——那这个提问就是白问。
2. **答案原样回给模型**：选中项的 label **原封不动**（包括 `(Recommended)` 后缀），
   自定义答案走 `custom` 字段；单选自定义会**覆盖**选择，多选则两者都给。
3. **没有提问通道时如实报错**，不降级：CLI / 自动化 / 无人值守场景没人能回答，
   这时工具返回一条明确的错误，让模型把问题写进结论里，而不是假装问过。
4. **问与答都落轨迹**：刷新页面之后那张提问卡还在（由轨迹推导），
   答过的则显示成了什么——这是"改动完全显性"的一部分。

推荐项的约定沿用 DSH：**推荐项放第一个，并在 label 末尾加 `(Recommended)`**。
界面会把它渲染成一个小徽标，但**答案值保持原样**（模型看到的就是它自己写的那串）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from code_agent.tools.base import Tool, ToolContext, ToolError

#: 一次最多问几道题（问题卡是"一题一屏"的分页，太多题会变成问卷）
MAX_QUESTIONS = 8
#: 每题最多几个选项
MAX_OPTIONS = 8
#: 题干/选项/说明的长度上限（一行问得清楚的才是好问题）
MAX_TEXT = 500
MAX_LABEL = 200
MAX_DESCRIPTION = 400

#: 回给模型时的说明前缀：告诉它这条结果是"人的回答"而不是工具输出
NO_CHANNEL = (
    "[ask_user_question] 当前环境没有提问通道（CLI / 自动化 / 无人值守），没人能回答这个问题。"
    "请不要假装问过：把你的问题与可选项写进最终结论，让用户回答后再继续。"
)
#: 没拿到回答时的说明（区分"超时"与"用户放弃"：模型的处理方式不一样）
TIMEOUT = "[ask_user_question] 用户没有在时限内回答（超时），未获得回答。"
DISMISSED = (
    "[ask_user_question] 用户放弃了整组提问，没有得到任何答案。"
    "不要反复追问：把你的判断与理由写进结论，或换一种不依赖这个答案的做法。"
)


class AskUserQuestionTool(Tool):
    name = "ask_user_question"
    description = (
        "Ask the user a concise question when you need confirmation, a choice, or missing "
        "information before proceeding. Send one or more questions, each with a stable id that "
        "will be echoed in the answer. This call BLOCKS until the user answers (or the turn is "
        "cancelled) — use it instead of guessing. Each question may carry options to choose from; "
        'if you recommend one option, put it FIRST and append "(Recommended)" to that label. '
        "The user may also type a custom answer, and may skip a question (then it is returned with "
        "an empty `selected`)."
    )
    description_zh = "就「继续之前需要确认的选择」问用户一句：阻塞等待回答，答案以普通工具结果喂回。"
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": (
                    f"Questions to ask the user before continuing (at most {MAX_QUESTIONS})."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Stable id for this question; echoed in the answer.",
                        },
                        "question": {
                            "type": "string",
                            "description": "The specific question to ask the user.",
                        },
                        "header": {
                            "type": "string",
                            "description": 'Optional short heading, such as "Confirm" or "Choose Mode".',
                        },
                        "options": {
                            "type": "array",
                            "description": (
                                "Optional choices to show the user. If you recommend one, put it "
                                'first and append "(Recommended)" to that label.'
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Short user-facing option label.",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "One sentence explaining the tradeoff or impact.",
                                    },
                                },
                                "required": ["label"],
                            },
                        },
                        "multi_select": {
                            "type": "boolean",
                            "description": "Whether the user may select more than one option. Defaults to false.",
                        },
                    },
                    "required": ["id", "question"],
                },
            }
        },
        "required": ["questions"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        questions = normalize_questions(arguments.get("questions"))
        ask = (ctx.extra or {}).get("ask")
        if ask is None:
            # 不降级：没有通道就说清楚，让模型把问题写进结论（DSH 同款语义）
            return NO_CHANNEL
        try:
            outcome = await ask(questions)
        except asyncio.CancelledError:
            # **绝不能吞掉取消**。真机踩到：这里 `return` 一个字符串把 CancelledError
            # 吃了下去 → 用户按"停止"之后这一轮并没有停，循环又发起一次提问、
            # 再次阻塞，直到把 QUESTION_TIMEOUT 耗完（表现为"点了停止没反应，界面卡住"）。
            # 取消要原样往上走；"这题被取消"这件事由桥接层落 `status=cancelled` 事件，
            # 不需要工具再回一句给模型（那一轮本来就已经不存在了）。
            raise
        except Exception as exc:                     # 通道故障也不许假装问过
            return f"[ask_user_question] 提问通道异常：{type(exc).__name__}: {exc}"

        if outcome is None:                          # 超时
            return TIMEOUT
        if outcome is False:                         # 用户放弃了整组提问
            return DISMISSED
        answers = outcome if isinstance(outcome, list) else []
        # 紧凑 JSON（与 DSH 的 render 一致）：模型要的就是"哪个 id 选了哪几个"
        return json.dumps(
            {"answers": [compact_answer(a) for a in answers]},
            ensure_ascii=False,
        )


def compact_answer(answer: dict[str, Any]) -> dict[str, Any]:
    """回给模型的答案形状：{id, selected, custom?}。"""
    out: dict[str, Any] = {
        "id": str(answer.get("id") or ""),
        "selected": [str(s) for s in (answer.get("selected") or [])],
    }
    custom = str(answer.get("custom") or "").strip()
    if custom:
        out["custom"] = custom
    return out


def normalize_questions(raw: Any) -> list[dict[str, Any]]:
    """校验并规范化问题列表。坏输入报错而不是静默丢题。

    为什么不容错：问题卡是"给人看的承诺"，静默丢掉一题，人是不会知道少了一题的
    （占位只剩 2/3），而模型以为问了 3 个。宁可报错让模型重发。
    """
    if not isinstance(raw, list) or not raw:
        raise ToolError("questions 必须是非空数组")
    if len(raw) > MAX_QUESTIONS:
        raise ToolError(f"一次最多问 {MAX_QUESTIONS} 个问题（收到 {len(raw)} 个）")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ToolError("每个问题必须是对象")
        qid = str(entry.get("id") or "").strip()
        text = str(entry.get("question") or "").strip()
        if not qid:
            raise ToolError("每个问题都要有稳定的 id（答案会按 id 回给模型）")
        if qid in seen:
            raise ToolError(f"问题 id 重复：{qid}")
        if not text:
            raise ToolError(f"问题 {qid} 的 question 不能为空")
        if len(text) > MAX_TEXT:
            raise ToolError(f"问题 {qid} 太长（>{MAX_TEXT} 字）")
        seen.add(qid)
        item: dict[str, Any] = {"id": qid, "question": text}
        header = str(entry.get("header") or "").strip()
        if header:
            item["header"] = header[:MAX_LABEL]
        options_raw = entry.get("options")
        if options_raw is not None:
            if not isinstance(options_raw, list) or not options_raw:
                raise ToolError(f"问题 {qid} 的 options 必须是非空数组（不想要选项就别传）")
            if len(options_raw) > MAX_OPTIONS:
                raise ToolError(f"问题 {qid} 的选项最多 {MAX_OPTIONS} 个")
            options: list[dict[str, str]] = []
            for opt in options_raw:
                if isinstance(opt, str):
                    opt = {"label": opt}
                if not isinstance(opt, dict):
                    raise ToolError(f"问题 {qid} 的选项必须是对象或字符串")
                label = str(opt.get("label") or "").strip()
                if not label:
                    raise ToolError(f"问题 {qid} 有选项缺 label")
                if len(label) > MAX_LABEL:
                    raise ToolError(f"问题 {qid} 的选项 label 太长（>{MAX_LABEL} 字）")
                one = {"label": label}
                desc = str(opt.get("description") or "").strip()
                if desc:
                    one["description"] = desc[:MAX_DESCRIPTION]
                options.append(one)
            item["options"] = options
        if entry.get("multi_select") is not None:
            item["multi_select"] = bool(entry.get("multi_select"))
        out.append(item)
    return out


def build_ask_tools() -> list[Tool]:
    return [AskUserQuestionTool()]
