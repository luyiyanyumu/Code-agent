"""离线 mock provider —— 无 API key 跑全链路（教程同款做法）。

两种用法：
1. 脚本化（测试用）：传入 ModelTurn 列表，按调用顺序弹出；
2. 规则化（CLI --demo 用）：根据用户输入里的关键词返回预置行为，
   确定性、可重复。

规则化模式认两个标记（都是为了"没有 key 也能演示完整链路"）：
- 用户消息里带 `[demo:ask]` → 先走一次**提问卡**流程（3 道题，含推荐项），
  答完再按答案收口——用来演示/回归 M31 的提问对话框；
- 其余情况：先调一次 bash 工具，再把结果收口成一句话。
"""

from __future__ import annotations

import json
from typing import Any

from code_agent.models.events import ModelTurn, TokenUsage, ToolCall
from code_agent.providers.base import Provider

_FAKE_USAGE = TokenUsage(input_tokens=42, output_tokens=17)

#: 触发提问演示的标记（刻意用显式标记而不是关键词猜：假阳性会让人莫名其妙被提问）
ASK_MARKER = "[demo:ask]"

#: 演示用的一组问题（形态对齐设计稿：带 header、带推荐项、含多选）
DEMO_QUESTIONS = [
    {
        "id": "del_dept",
        "header": "删部门时人怎么办",
        "question": "删除业务部门时，该部门下还有员工，怎么处理？",
        "options": [
            {"label": "A. 有员工就不让删，提示先把人调走 (Recommended)",
             "description": "最安全：不会产生孤儿数据，也不会误删还在用的部门。"},
            {"label": "B. 允许删，把人自动改为「未分配部门」",
             "description": "删得干净，但被影响的人自己不知道，需要给个提示。"},
            {"label": "C. 软删除：从下拉隐藏，历史单据保留",
             "description": "历史单据里的部门名不变（对已发生的报销很重要），但下拉里不再出现。"},
            {"label": "D. 其他（我说明）", "description": "你们实际情况不同，听你的。"},
        ],
    },
    {
        "id": "migrate",
        "header": "存量数据",
        "question": "已有的部门与员工关联要不要一起迁移？",
        "multi_select": True,
        "options": [
            {"label": "迁移历史单据里的部门名", "description": "只补名不改结构，风险低。"},
            {"label": "回填未分配部门", "description": "把已经失去部门的员工归到一处，便于后续处理。"},
        ],
    },
    {
        "id": "verify",
        "header": "验证范围",
        "question": "改完跑哪一档验证？",
        "options": [
            {"label": "只跑相关模块的测试", "description": "快，适合一边改一边跑。"},
            {"label": "跑全量测试 + 前端构建", "description": "慢，但发版前需要。"},
        ],
    },
]


class OfflineProvider(Provider):
    name = "offline"

    def __init__(self, script: list[ModelTurn] | None = None):
        # script 队列：每次 chat 弹出一个；耗尽后进入 echo 兜底模式
        self._script = list(script) if script else []
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        self.calls += 1

        if self._script:
            return self._script.pop(0)

        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "",
        )
        return self._respond(last_user, messages)

    # ------------------------------------------------------------------
    def _respond(self, user_text: str, messages: list[dict[str, Any]]) -> ModelTurn:
        """规则化兜底：演示"提问 → 调工具 → 回答"的标准链路。

        「本轮是否已经有工具结果」要按**最后一条用户消息之后**判断，不能看整段历史：
        否则同一个会话里的第二条消息永远走"直接收口"分支（历史里当然带着上一轮的工具结果），
        演示标记 `[demo:ask]` 也就永远不会触发第二次。
        """
        last_user = max(
            (i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1
        )
        current_turn = messages[last_user + 1 :]
        if any(m.get("role") == "tool" for m in current_turn):
            preview = ""
            for m in reversed(current_turn):
                if m.get("role") == "tool":
                    preview = m["content"][:300]
                    break
            return ModelTurn(
                text=f"[offline] 工具已执行，结果：{preview}",
                stop_reason="end_turn",
                usage=_FAKE_USAGE,
            )
        # 演示提问卡（M31）：模型先停下来问人，答完再收口
        if ASK_MARKER in (user_text or ""):
            return ModelTurn(
                text=None,
                tool_calls=[
                    ToolCall(name="ask_user_question", arguments={"questions": DEMO_QUESTIONS})
                ],
                stop_reason="tool_use",
                usage=_FAKE_USAGE,
            )
        # 未调过工具 -> 触发一次 bash 工具调用
        return ModelTurn(
            text=None,
            tool_calls=[ToolCall(name="bash", arguments={"command": "echo hello-from-code-agent"})],
            stop_reason="tool_use",
            usage=_FAKE_USAGE,
        )

    @staticmethod
    def tool_call_turn(name: str, arguments: dict[str, Any]) -> ModelTurn:
        """测试辅助：构造一轮 tool_use。"""
        return ModelTurn(
            tool_calls=[ToolCall(name=name, arguments=arguments)],
            stop_reason="tool_use",
            usage=_FAKE_USAGE,
        )

    @staticmethod
    def text_turn(text: str) -> ModelTurn:
        """测试辅助：构造一轮最终回答。"""
        return ModelTurn(text=text, stop_reason="end_turn", usage=_FAKE_USAGE)

    @staticmethod
    def dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False)
