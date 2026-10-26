"""提问工具与提问卡（M31）：模型停下来问人一句，答案作为普通 tool result 回去。

原理照 deepseek harness 的 `dsh-tool-ask-user`。这里逐条钉住那四件事：

1. **阻塞而不是并行**：模型这一轮真的停在这次调用上（不是"问完继续猜"）；
2. **答案原样回给模型**：选中项的 label 原封不动（含 `(Recommended)` 后缀），
   自定义答案走 `custom`；单选自定义**覆盖**选择；
3. **没有通道时如实报错**，不降级成"假装问过"；
4. **问与答都落轨迹**（question_request / question_answered），刷新后那张卡还在。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.config import transcripts_dir
from code_agent.models.events import EventType
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import Decision
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions import manager as manager_module
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.ask_tools import (
    MAX_QUESTIONS,
    NO_CHANNEL,
    AskUserQuestionTool,
    normalize_questions,
)

ONE = [
    {
        "id": "del_dept",
        "header": "删部门时人怎么办",
        "question": "删除业务部门时，该部门下还有员工，怎么处理？",
        "options": [
            {"label": "A. 有员工就不让删，提示先把人调走 (Recommended)", "description": "最安全。"},
            {"label": "B. 允许删，把人自动改为「未分配部门」", "description": "删得干净。"},
        ],
    }
]


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def test_normalize_keeps_ids_and_order() -> None:
    qs = normalize_questions(
        [
            {"id": "q1", "question": "先做哪个？", "header": "范围", "options": ["A", "B"]},
            {"id": "q2", "question": "要不要跑全量测试？", "multi_select": True},
        ]
    )
    assert [q["id"] for q in qs] == ["q1", "q2"]
    assert qs[0]["header"] == "范围"
    assert qs[0]["options"] == [{"label": "A"}, {"label": "B"}]   # 字符串选项也认
    assert qs[1]["multi_select"] is True
    assert "options" not in qs[1]


def test_normalize_rejects_bad_input_loudly() -> None:
    """静默丢一题最危险：占位会变成 2/3，而模型以为问了 3 个。"""
    with pytest.raises(ToolError, match="非空数组"):
        normalize_questions([])
    with pytest.raises(ToolError, match="非空数组"):
        normalize_questions("问题")
    with pytest.raises(ToolError, match="id"):
        normalize_questions([{"question": "没有 id"}])
    with pytest.raises(ToolError, match="重复"):
        normalize_questions([{"id": "a", "question": "x"}, {"id": "a", "question": "y"}])
    with pytest.raises(ToolError, match="question 不能为空"):
        normalize_questions([{"id": "a", "question": "  "}])
    with pytest.raises(ToolError, match="选项最多"):
        normalize_questions([{"id": "a", "question": "x", "options": [f"O{i}" for i in range(20)]}])
    with pytest.raises(ToolError, match="最多"):
        normalize_questions([{"id": f"q{i}", "question": "x"} for i in range(MAX_QUESTIONS + 1)])


def test_no_channel_reports_instead_of_pretending() -> None:
    """CLI / 自动化场景没人能回答：返回明确错误，让模型把问题写进结论。"""
    tool = AskUserQuestionTool()
    ctx = ToolContext(session_id="s", workspace=None)
    out = asyncio.run(tool.execute({"questions": ONE}, ctx))
    assert out == NO_CHANNEL
    assert "不要假装问过" in out


def test_answers_are_echoed_verbatim() -> None:
    """答案里的 label 必须**原封不动**（含 (Recommended) 后缀）：前端剥它只影响显示。"""
    tool = AskUserQuestionTool()
    seen: dict = {}

    async def ask(questions):
        seen["questions"] = questions
        return [
            {"id": "del_dept", "selected": ["A. 有员工就不让删，提示先把人调走 (Recommended)"]},
        ]

    ctx = ToolContext(session_id="s", workspace=None, extra={"ask": ask})
    out = asyncio.run(tool.execute({"questions": ONE}, ctx))
    payload = json.loads(out)
    assert payload["answers"][0]["selected"][0].endswith("(Recommended)")
    assert "custom" not in payload["answers"][0], "没有自定义答案时不该带 custom 字段"
    assert seen["questions"][0]["header"] == "删部门时人怎么办"


def test_dismiss_and_timeout_are_stated_clearly() -> None:
    tool = AskUserQuestionTool()

    async def dismiss(_questions):
        return False

    async def timeout(_questions):
        return None

    for handler, needle in ((dismiss, "放弃"), (timeout, "超时")):
        ctx = ToolContext(session_id="s", workspace=None, extra={"ask": handler})
        out = asyncio.run(tool.execute({"questions": ONE}, ctx))
        assert needle in out, out


def test_tool_never_swallows_cancellation() -> None:
    """**回归**：工具不许把 CancelledError 吃掉。

    真机踩到：这里 `return` 一句字符串把取消吞了下去 → 用户按"停止"之后这一轮
    并没有停，循环又发起一次提问、再次阻塞，直到把 QUESTION_TIMEOUT 耗完
    （表现为"点了停止没反应，界面卡住"）。取消必须原样往上走。
    """
    tool = AskUserQuestionTool()

    async def hang(_questions):
        await asyncio.sleep(30)

    async def scenario():
        ctx = ToolContext(session_id="s", workspace=None, extra={"ask": hang})
        task = asyncio.create_task(tool.execute({"questions": ONE}, ctx))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_ask_tool_needs_no_approval(tmp_path, agent_home) -> None:
    """提问不该先要审批（"我能不能问你一句"需要审批是荒谬的）。"""
    pm = PermissionManager(workspace_root=tmp_path, mode="read-only")
    res = asyncio.run(pm.evaluate("ask_user_question", {}, ToolContext("s", tmp_path)))
    assert res.decision is not Decision.deny
    assert "ask_user_question" in build_registry().names()


# ---------------------------------------------------------------------------
# 接到会话上：真的阻塞、答案真的回到模型手里
# ---------------------------------------------------------------------------
@pytest.fixture
def manager(agent_home):
    db = Database(agent_home / "ask.db")
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )


def _runtime(manager: SessionManager):
    db = manager.session_repo.db
    uid = UserRepo(db).create("u_ask", "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    return manager.get_runtime(meta["id"], uid)


def _wait_for_question(rt, timeout=5.0) -> str:
    """等运行时挂起一个提问，返回 question_id。"""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = list(rt._pending_questions)
        if pending:
            return pending[0]
        time.sleep(0.02)
    raise AssertionError("运行时没有挂起提问（工具没走到提问通道？）")


def test_loop_blocks_on_the_question_until_answered(manager) -> None:
    """全链路：provider 提问 → 循环停住 → 用户答 → 答案进 messages → 模型接着收口。"""
    rt = _runtime(manager)
    seen_messages: list[str] = []

    class AskProvider(OfflineProvider):
        def __init__(self) -> None:
            super().__init__(script=[])
            self.n = 0

        async def chat(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return OfflineProvider.tool_call_turn("ask_user_question", {"questions": ONE})
            # 第二轮必须看得到答案（在 tool 消息里）
            seen_messages.extend(
                m.get("content") or "" for m in messages if m.get("role") == "tool"
            )
            return OfflineProvider.text_turn("按你选的 A 来做。")

    rt.router.provider_override = AskProvider()

    async def scenario():
        task = asyncio.create_task(rt.submit("改一下部门删除逻辑"))
        qid = await asyncio.to_thread(_wait_for_question, rt)
        assert rt.busy, "提问挂起时这一轮必须仍然是 busy（模型在等）"
        assert rt.answer_question(
            qid, [{"id": "del_dept", "selected": ["A. 有员工就不让删，提示先把人调走 (Recommended)"]}]
        )
        return await task

    result = asyncio.run(scenario())

    assert result.stop_reason == "end_turn" and result.text == "按你选的 A 来做。"
    joined = "\n".join(seen_messages)
    assert "有员工就不让删" in joined, "答案没有作为 tool result 回到模型"
    assert json.loads(joined)["answers"][0]["id"] == "del_dept"


def test_question_and_answer_are_persisted(manager) -> None:
    """问与答都落轨迹：刷新后那张卡还在（由轨迹推导，不靠前端状态）。"""
    rt = _runtime(manager)

    class AskProvider(OfflineProvider):
        def __init__(self) -> None:
            super().__init__(script=[])
            self.n = 0

        async def chat(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return OfflineProvider.tool_call_turn("ask_user_question", {"questions": ONE})
            return OfflineProvider.text_turn("好了。")

    rt.router.provider_override = AskProvider()
    seen: dict = {}

    async def scenario():
        task = asyncio.create_task(rt.submit("问一下"))
        qid = await asyncio.to_thread(_wait_for_question, rt)
        # 没答之前：轨迹里已经有 request（所以刷新能看到卡）
        asked = [e for e in rt.transcript.replay(rt.session_id)
                 if e.type == EventType.question_request]
        assert asked and asked[0].payload["question_id"] == qid
        assert asked[0].payload["questions"][0]["header"] == "删部门时人怎么办"
        assert rt.answer_question(qid, [{"id": "del_dept", "selected": []}])
        seen["qid"] = qid
        await task

    asyncio.run(scenario())

    events = rt.transcript.replay(rt.session_id)
    answered = [e for e in events if e.type == EventType.question_answered]
    assert answered, "回答没有落轨迹"
    assert answered[0].payload["status"] == "answered"
    assert answered[0].payload["question_id"] == seen["qid"]


def test_answering_a_stale_question_is_rejected(manager) -> None:
    """服务重启 / 已超时之后再点"提交"必须得到明确的失败，而不是假装收下了。"""
    rt = _runtime(manager)
    assert rt.answer_question("q_nope", [{"id": "x", "selected": []}]) is False


def test_question_timeout_returns_no_answer(manager, monkeypatch) -> None:
    """超时 = "没得到回答"，不是"用户拒绝"：工具要如实这么告诉模型。"""
    monkeypatch.setattr(manager_module, "QUESTION_TIMEOUT", 0.15)
    rt = _runtime(manager)

    class AskProvider(OfflineProvider):
        def __init__(self) -> None:
            super().__init__(script=[])
            self.n = 0
            self.tool_results: list[str] = []

        async def chat(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return OfflineProvider.tool_call_turn("ask_user_question", {"questions": ONE})
            self.tool_results.extend(
                m.get("content") or "" for m in messages if m.get("role") == "tool"
            )
            return OfflineProvider.text_turn("没等到回答，我先把问题列出来。")

    provider = AskProvider()
    rt.router.provider_override = provider
    asyncio.run(rt.submit("问一下"))

    assert any("超时" in t for t in provider.tool_results), provider.tool_results
    events = rt.transcript.replay(rt.session_id)
    statuses = [e.payload["status"] for e in events if e.type == EventType.question_answered]
    assert statuses == ["timeout"]


def test_cancel_marks_the_question_cancelled(manager) -> None:
    """用户停止这一轮：提问的 future 被取消，轨迹里留下 cancelled（卡随之消失）。"""
    rt = _runtime(manager)

    class AskProvider(OfflineProvider):
        def __init__(self) -> None:
            super().__init__(script=[])
            self.n = 0

        async def chat(self, messages, tools):
            self.n += 1
            return OfflineProvider.tool_call_turn("ask_user_question", {"questions": ONE})

    rt.router.provider_override = AskProvider()

    async def scenario():
        task = asyncio.create_task(rt.submit("问一下"))
        await asyncio.to_thread(_wait_for_question, rt)
        rt.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    events = rt.transcript.replay(rt.session_id)
    statuses = [e.payload["status"] for e in events if e.type == EventType.question_answered]
    assert statuses == ["cancelled"], statuses
    assert rt._pending_questions == {}


# ---------------------------------------------------------------------------
# API：浏览器怎么把答案送达
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / "code-agent-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("CODE_AGENT_HOME", str(home))
    for key in ("CODE_AGENT_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from code_agent.api.app import create_app

    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_answer_endpoint_rejects_bad_input_and_stale_questions(client) -> None:
    """错误路径必须明确：坏 body → 422；没有在等的提问 → **409**（绝不假装收下）。

    "成功送达"这条路径由浏览器端到端脚本打真服务验证（`web/.tmp/check-ask-card.mjs`）——
    那才是前端真正会走的路，比在这里拼一个假 future 更有说服力。
    """
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    assert client.post(
        f"/api/v1/sessions/{sid}/questions/q_nope/answer", json={}, headers=h
    ).status_code == 422

    stale = client.post(
        f"/api/v1/sessions/{sid}/questions/q_nope/answer",
        json={"answers": [{"id": "q1", "selected": ["A"]}]},
        headers=h,
    )
    assert stale.status_code == 409
    assert "已经结束" in stale.text

    assert client.post(
        "/api/v1/sessions/s_nope/questions/q1/answer",
        json={"answers": []},
        headers=h,
    ).status_code == 404

    # 放弃整组：同样走 409（没有在等的提问就没什么可放弃的）
    assert client.post(
        f"/api/v1/sessions/{sid}/questions/q_nope/answer", json={"dismiss": True}, headers=h
    ).status_code == 409
