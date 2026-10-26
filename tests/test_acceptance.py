"""有序验收清单（M29 · 借自 AgentFlow 的 verifier）。

AgentFlow 的 verifier 每次只回答两件事：证据够了吗（STOP / CONTINUE）、
**第一个**未满足的判据是什么。摊开整张清单会让模型挑好做的那条做，顺序
（也就是真正的依赖关系）就丢了。这里断言的就是这套口径：

- 判据可校验（`tool:` / `patch:` / `file_exists:` / `file_contains:` / `no_failures`），
  不合法的一律不收——门禁不能建立在一条坏判据上；
- 判定给出 ρ=ℓ/n 的真实进度，但详述**只有第一个未满足项**；
- 清单条数有上限（这就是"计划要短"的机械保证）；
- 执行自然收口，清单继续记录真实未满足项，不合成用户追问。
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.agent import loop as loop_module
from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.config import transcripts_dir
from code_agent.models.events import EventType
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.acceptance import (
    MAX_ITEMS,
    AcceptanceList,
    Evidence,
    collect_evidence,
    failure_count,
    valid_check,
)
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore


# ---------------------------------------------------------------------------
# 判据与判定
# ---------------------------------------------------------------------------
def test_valid_check_rejects_broken_criteria() -> None:
    assert valid_check("tool:bash")
    assert valid_check("patch:pkg/mathx.py")
    assert valid_check("file_exists:README.md")
    assert valid_check("file_contains:pkg/mathx.py::def add")
    assert valid_check("no_failures")
    # 坏判据必须在**写入前**就被拒：门禁不该建立在一句它读不懂的话上
    assert not valid_check("")
    assert not valid_check("tool:")
    assert not valid_check("跑测试")                    # 自然语言不是判据
    assert not valid_check("command:pytest -q")         # 刻意不支持跑命令
    assert not valid_check("file_contains:pkg/mathx.py")  # 少了 ::文本


def test_parse_keeps_order_and_truncates() -> None:
    lst = AcceptanceList.parse(
        [
            {"label": "先定位", "check": "tool:grep"},
            "patch:pkg/mathx.py",                       # 字符串形式也认
            {"label": "坏的", "check": "随便写点什么"},
            *[{"label": f"多余{i}", "check": "tool:bash"} for i in range(10)],
        ]
    )
    labels = [i.label for i in lst.items]
    assert labels[0] == "先定位" and labels[1] == "patch:pkg/mathx.py"
    assert "坏的" not in labels
    assert len(lst.items) == MAX_ITEMS, "条数上限就是「计划要短」的机械保证"


def test_report_reports_only_the_first_unsatisfied_item() -> None:
    lst = AcceptanceList.parse(
        [
            {"label": "改过 mathx", "check": "patch:pkg/mathx.py"},
            {"label": "跑过测试", "check": "tool:bash"},
            {"label": "没有遗留失败", "check": "no_failures"},
        ]
    )
    report = lst.evaluate(Evidence(ok_tools={"bash"}, patched=set(), failures=2))
    assert (report.done, report.total) == (1, 3)
    assert report.rho == round(1 / 3, 3)
    assert not report.passed
    assert report.first_unsatisfied is not None
    assert report.first_unsatisfied.item.label == "改过 mathx"

    text = report.digest()
    assert "1/3 已满足" in text and "ρ=0.33" in text
    assert "当前只做第 1 项：改过 mathx" in text
    # 第 2、3 项**不许**展开原因（展开等于把顺序交回给模型挑）
    assert "还没有成功执行过 `bash`" not in text
    assert "1 条更靠后的未满足项" in text


def test_file_checks_read_the_workspace_but_cannot_escape(tmp_path) -> None:
    (tmp_path / "a.py").write_text("def add():\n    return 1\n", encoding="utf-8")
    lst = AcceptanceList.parse(
        [
            {"label": "文件在", "check": "file_exists:a.py"},
            {"label": "内容对", "check": "file_contains:a.py::def add"},
            {"label": "越界探测", "check": "file_exists:../../etc/passwd"},
        ]
    )
    report = lst.evaluate(Evidence(workspace=tmp_path))
    ok = {v.item.label: v.ok for v in report.verdicts}
    assert ok["文件在"] is True and ok["内容对"] is True
    assert ok["越界探测"] is False, "清单里的路径不能用来探测工作区之外"


def test_collect_evidence_only_counts_successes() -> None:
    """证据口径必须与模型看到的一致：失败的结果不算"做过"。"""
    events = [
        _tool_use("c1", "bash", {"command": "pytest -q"}),
        _tool_result("c1", "全部通过"),
        _tool_use("c2", "bash", {"command": "pytest -q"}),
        _tool_result("c2", "[tool error] 退出码 1"),
        _tool_use("c3", "edit_file", {"path": "pkg\\mathx.py"}),
        _tool_result("c3", "已改 3 行"),
    ]
    ev = collect_evidence(events)
    assert ev.ok_tools == {"bash", "edit_file"}
    assert ev.patched == {"pkg/mathx.py"}, "路径要归一化（反斜杠 → 斜杠）"


@pytest.mark.parametrize("result,metadata", [
    ("[tool deferred] validation was not executed", {}),
    ("  [tool deferred] legacy deferred result", {}),
    ("validation deferred", {"deferred": True}),
    ("exit_code=0\nmisleading cached text", {"executed": False}),
    ("[tool deferred] still not executed", {"executed": True, "deferred": False}),
])
def test_deferred_results_never_supply_execution_or_patch_evidence_or_add_failures(result, metadata):
    events = [_tool_use("test", "bash", {"command": "python -m pytest"}),
              _tool_result("test", result),
              _tool_use("write", "edit_file", {"path": "source.py"}),
              _tool_result("write", result)]
    for event in (events[1], events[3]):
        event.payload.update(metadata)
    events = [event.model_copy(update={"seq": index + 1}) for index, event in enumerate(events)]
    evidence = collect_evidence(events, failures=3)
    assert not evidence.ok_tools and not evidence.patched
    assert not AcceptanceList.parse(["tool:bash"]).evaluate(evidence).passed
    assert not AcceptanceList.parse(["patch:source.py"]).evaluate(evidence).passed
    assert evidence.failures == 3
    assert failure_count(events) == 0


def test_real_success_after_deferred_result_keeps_legacy_evidence_and_actual_failure_count():
    events = [_tool_use("deferred", "bash", {"command": "python -m pytest"}),
              _tool_result("deferred", "[tool deferred] not executed"),
              _tool_use("failed", "bash", {"command": "python -m pytest"}),
              _tool_result("failed", "[tool error] actual executed failure"),
              _tool_use("actual", "bash", {"command": "python -m pytest"}),
              _tool_result("actual", "exit_code=0\n1 passed"),
              _tool_use("actual-write", "edit_file", {"path": "source.py"}),
              _tool_result("actual-write", "changed source.py")]
    events = [event.model_copy(update={"seq": index + 1}) for index, event in enumerate(events)]
    assert collect_evidence(events).ok_tools == {"bash", "edit_file"}
    assert collect_evidence(events).patched == {"source.py"}
    assert failure_count(events) == 1


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def test_acceptance_roundtrip_on_disk(agent_home) -> None:
    lst = AcceptanceList.parse([{"label": "一条", "check": "tool:bash"}], note="说明")
    lst.save("s_roundtrip")
    loaded = AcceptanceList.load("s_roundtrip")
    assert loaded is not None
    assert [i.label for i in loaded.items] == ["一条"]
    assert loaded.note == "说明"
    assert AcceptanceList.clear("s_roundtrip") is True
    assert AcceptanceList.load("s_roundtrip") is None


def test_broken_json_is_treated_as_no_checklist(agent_home) -> None:
    """清单文件坏了 = 没有清单，绝不因此拦住主流程。"""
    path = AcceptanceList.path_for("s_broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{不是 json", encoding="utf-8")
    assert AcceptanceList.load("s_broken") is None


# ---------------------------------------------------------------------------
# 接到会话上
# ---------------------------------------------------------------------------
@pytest.fixture
def manager(agent_home):
    db = Database(agent_home / "acc.db")
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )


def _runtime(manager: SessionManager):
    db = manager.session_repo.db
    uid = UserRepo(db).create("u_acc", "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    return manager.get_runtime(meta["id"], uid)






def test_classic_delivers_without_manufacturing_a_verification_interview(manager, monkeypatch) -> None:
    """文件工具返回后继续模型，模型结束就交付，不伪造用户追问。"""
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", 8)
    rt = _runtime(manager)

    class EditThenClaimProvider(OfflineProvider):
        def __init__(self) -> None:
            super().__init__(script=[])
            self.n = 0

        async def chat(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return OfflineProvider.tool_call_turn("write_file", {"path": "a.py", "content": "x = 1\n"})
            return OfflineProvider.text_turn("已经改好了。")

    rt.router.provider_override = EditThenClaimProvider()
    result = asyncio.run(rt.submit("给 a.py 加一行"))

    msgs = [
        e.payload.get("text", "")
        for e in rt.transcript.replay(rt.session_id)
        if e.type == EventType.user_message
    ]
    assert msgs == ["给 a.py 加一行"], msgs
    assert rt.router.provider_override.n == 2
    assert (rt.workspace / "a.py").read_text() == "x = 1\n"
    assert result.stop_reason == "end_turn" and result.text == "已经改好了。"


def test_classic_closure_does_not_nag_when_verification_ran(manager, monkeypatch) -> None:
    """改完又跑了验证 → 收口时不再追问（不制造噪音）。"""
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", 8)
    rt = _runtime(manager)

    class EditVerifyProvider(OfflineProvider):
        def __init__(self) -> None:
            super().__init__(script=[])
            self.n = 0

        async def chat(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return OfflineProvider.tool_call_turn("write_file", {"path": "a.py", "content": "x = 1\n"})
            if self.n == 2:
                return OfflineProvider.tool_call_turn("bash", {"command": "echo ok"})
            return OfflineProvider.text_turn("改完并验证过了。")

    rt.router.provider_override = EditVerifyProvider()
    asyncio.run(rt.submit("改 a.py 并验证"))

    msgs = [
        e.payload.get("text", "")
        for e in rt.transcript.replay(rt.session_id)
        if e.type == EventType.user_message
    ]
    assert len(msgs) == 1, msgs


def test_classic_checklist_stays_pending_without_reopening_a_completed_model_turn(manager, monkeypatch) -> None:
    """验收记录如实保留，普通完成不自动变成显式门禁任务。"""
    monkeypatch.setattr(loop_module, "DEFAULT_MAX_TURNS", 8)
    rt = _runtime(manager)
    # 清单里第 1 项是 tool:bash —— 下面这个 provider 只写文件、从不跑 bash，
    # 所以清单永远不会全绿；同时它又构成"改了没验"。
    rt.set_acceptance([{"label": "要跑过测试", "check": "tool:bash"}])

    class EditOnlyProvider(OfflineProvider):
        def __init__(self) -> None:
            super().__init__(script=[])
            self.n = 0

        async def chat(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return OfflineProvider.tool_call_turn("write_file", {"path": "a.py", "content": "x = 1\n"})
            return OfflineProvider.text_turn("我做完了。")

    rt.router.provider_override = EditOnlyProvider()
    result = asyncio.run(rt.submit("改 a.py"))

    msgs = [
        e.payload.get("text", "")
        for e in rt.transcript.replay(rt.session_id)
        if e.type == EventType.user_message
    ]
    assert msgs == ["改 a.py"], msgs
    assert rt.router.provider_override.n == 2
    assert rt.acceptance()["passed"] is False
    assert result.stop_reason == "end_turn", result.stop_reason
    # 追问事件带得出"问的是哪一类"，便于回放时看清这一段为什么又跑了一轮
    kinds = [
        e.payload.get("closure_kind")
        for e in rt.transcript.replay(rt.session_id)
        if e.payload.get("auto")
    ]
    assert kinds == [], kinds


@pytest.mark.parametrize("stop_reason", ["max_tokens", "content_filter", "max_turns", "provider_interrupted"])
def test_abnormal_model_stop_never_starts_a_repair_round(manager, stop_reason):
    from code_agent.models.events import ModelTurn, TokenUsage

    rt = _runtime(manager)
    provider = OfflineProvider(script=[ModelTurn(text="partial output", stop_reason=stop_reason,
                                                usage=TokenUsage(input_tokens=7, output_tokens=2))])
    rt.router.provider_override = provider
    result = asyncio.run(rt.submit("完成当前任务"))
    assert result.stop_reason == stop_reason
    assert result.text == "partial output"
    assert provider.calls == 1
    assert result.usage.input_tokens == 7 and result.usage.output_tokens == 2
    events = rt.transcript.replay(rt.session_id)
    assert len([event for event in events if event.type == EventType.user_message]) == 1
    assert len([event for event in events if event.type == EventType.done]) == 1


# ---------------------------------------------------------------------------
# 小工具：造事件
# ---------------------------------------------------------------------------
def _tool_use(call_id: str, name: str, args: dict):
    from code_agent.models.events import Event, ToolCall

    call = ToolCall(id=call_id, name=name, arguments=args)
    return Event(seq=0, type=EventType.tool_use, payload={"call": call.model_dump()})


def _tool_result(call_id: str, result: str):
    from code_agent.models.events import Event

    return Event(seq=0, type=EventType.tool_result, payload={"call_id": call_id, "result": result})
