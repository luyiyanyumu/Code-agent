"""成对检查点 + 回退 + 回退记忆（M29 · 借自 AgentRewind）。

AgentRewind 的核心是**成对**检查点：对话位置与工作区状态必须一起退回去，
只退一边比根本不退更糟。这里断言的就是这条：

- 检查点记两份东西（keep_seq + 这一步的 patch），回退时**先撤文件、再截轨迹**；
- 回退撤的是"第 k 步之后的一切"，不是第 k 步自己；
- 撤不动（文件被别人动过）时**拒绝自动回退**并如实报告，不静默覆盖；
- 没有 patch 台账的会话只退对话，报告里必须写明"文件改动未回退"；
- 被撤掉的那段经历要变成**回退记忆**注回下一轮上下文（否则模型会原样重走）；
- 回退本身落一条可回放事件，刷新后不会显得"那段凭空消失了"。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.config import transcripts_dir
from code_agent.models.events import EventType
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.checkpoints import CheckpointStore, report_text, rewind_to
from code_agent.sessions.manager import SessionManager
from code_agent.sessions.rewind import RewindNotes, summarize
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore


# ---------------------------------------------------------------------------
# 检查点存储
# ---------------------------------------------------------------------------
def test_checkpoint_store_roundtrip(agent_home) -> None:
    store = CheckpointStore("s_ck")
    a = store.record(keep_seq=3, kind="prompt", label="发消息前", anchor_seq=4)
    b = store.record(keep_seq=9, kind="segment", label="本轮结束", patches=[{"id": "pt_1", "path": "a.py"}])
    assert [c["id"] for c in store.list()] == [a["id"], b["id"]]
    assert store.get(b["id"])["patches"][0]["path"] == "a.py"
    assert store.get("ck_nope") is None


def test_checkpoint_drop_after_keeps_the_target(agent_home) -> None:
    """回退之后，目标点本身要留下（它就是新的"最新状态"），之后的入口全清掉。"""
    store = CheckpointStore("s_drop")
    store.record(keep_seq=3)
    target = store.record(keep_seq=9)
    store.record(keep_seq=15)
    assert store.drop_after(9) == 1
    assert [c["keep_seq"] for c in store.list()] == [3, 9]
    assert store.get(target["id"]) is not None


# ---------------------------------------------------------------------------
# 轨迹截断
# ---------------------------------------------------------------------------
def test_transcript_truncate_returns_the_dropped_tail(agent_home) -> None:
    ts = TranscriptStore(transcripts_dir())
    for i in range(5):
        ts.append("s_trunc", EventType.assistant_message, {"text": f"第{i}条"})
    dropped = ts.truncate("s_trunc", 2)
    assert [e.seq for e in dropped] == [3, 4, 5]
    assert [e.seq for e in ts.replay("s_trunc")] == [1, 2]
    # seq 仍然单调：后续 append 从 3 继续，前端"增量拉取"的语义不用改
    ev = ts.append("s_trunc", EventType.assistant_message, {"text": "新的一条"})
    assert ev.seq == 3


# ---------------------------------------------------------------------------
# 回退编排：撤的是"目标点之后的一切"
# ---------------------------------------------------------------------------
def test_rewind_reverts_everything_after_the_target(agent_home) -> None:
    ts = TranscriptStore(transcripts_dir())
    for i in range(6):
        ts.append("s_rw", EventType.assistant_message, {"text": f"第{i}条"})
    checkpoints = [
        {"id": "ck_1", "keep_seq": 0, "ts": "2024-01-01T00:00:00+00:00", "patches": []},
        {"id": "ck_2", "keep_seq": 2, "ts": "2024-01-01T00:00:01+00:00",
         "patches": [{"id": "pt_1", "path": "a.py"}]},
        {"id": "ck_3", "keep_seq": 4, "ts": "2024-01-01T00:00:02+00:00",
         "patches": [{"id": "pt_2", "path": "b.py"}]},
    ]
    calls: list[str] = []

    def fake_revert(patch_id: str) -> dict:
        calls.append(patch_id)
        return {"path": {"pt_1": "a.py", "pt_2": "b.py"}[patch_id]}

    result = rewind_to(
        checkpoints[1],           # 回到第 2 条：pt_1 是它自己的改动，**不该撤**
        checkpoints=checkpoints,
        transcript=ts,
        session_id="s_rw",
        revert_patch=fake_revert,
    )

    assert calls == ["pt_2"], "只撤目标点之后的改动，从后往前"
    assert [r["path"] for r in result["reverted"]] == ["b.py"]
    assert result["keep_seq"] == 2
    assert [e.seq for e in ts.replay("s_rw")] == [1, 2]
    assert result["dropped_events"] == 4


def test_rewind_reports_refusals_instead_of_forcing(agent_home) -> None:
    """撤不动就撤不动：拒绝是正确行为，必须如实写进报告交给人。"""
    ts = TranscriptStore(transcripts_dir())
    ts.append("s_ref", EventType.assistant_message, {"text": "x"})
    target = {"id": "ck_1", "keep_seq": 0, "ts": "2024-01-01T00:00:00+00:00", "patches": []}
    later = {"id": "ck_2", "keep_seq": 1, "ts": "2024-01-01T00:00:01+00:00",
             "patches": [{"id": "pt_9", "path": "a.py"}]}

    def refuse(patch_id: str) -> dict:
        raise ValueError("a.py 在这次改动之后又被修改过，拒绝自动回滚")

    result = rewind_to(target, checkpoints=[target, later], transcript=ts,
                       session_id="s_ref", revert_patch=refuse)
    assert result["reverted"] == []
    assert len(result["failed"]) == 1
    assert "拒绝自动回滚" in result["failed"][0]["reason"]
    text = report_text(result)
    assert "**未撤回** 1 处" in text and "a.py" in text


def test_rewind_without_a_patch_ledger_says_so(agent_home) -> None:
    """普通会话没有改动台账：只退对话，但必须写明文件没退。"""
    ts = TranscriptStore(transcripts_dir())
    ts.append("s_nol", EventType.assistant_message, {"text": "x"})
    target = {"id": "ck_1", "keep_seq": 0, "ts": "2024-01-01T00:00:00+00:00", "patches": []}
    later = {"id": "ck_2", "keep_seq": 1, "ts": "2024-01-01T00:00:01+00:00",
             "patches": [{"id": "pt_9", "path": "a.py"}]}
    result = rewind_to(target, checkpoints=[target, later], transcript=ts,
                       session_id="s_nol", revert_patch=None)
    assert result["failed"][0]["reason"] == "本次会话没有改动台账"


def test_rewind_restores_file_content_through_the_real_patch_ledger(agent_home, tmp_path) -> None:
    """撤文件这一步走**真的 patch 台账**：内容必须真的回到改动前。

    前面的用例用替身验证编排顺序，这一条验证"确实把文件写回去了"——
    编排对但写盘错，等于回退了个寂寞。
    """
    from code_agent.codeintel.service import CodeIntelService

    root = tmp_path / "repo"
    root.mkdir()
    target = root / "pkg"
    target.mkdir()
    (target / "mathx.py").write_text("def add():\n    return 2\n", encoding="utf-8")

    intel = CodeIntelService(projects=None)
    store = intel.patches("p_real")
    before = "def add():\n    return 1\n"
    store.record(rel="pkg/mathx.py", session_id="s_real", tool="edit_file",
                 before=before, after="def add():\n    return 2\n")

    ts = TranscriptStore(transcripts_dir())
    ts.append("s_real", EventType.assistant_message, {"text": "改完了"})
    ck_target = {"id": "ck_1", "keep_seq": 0, "ts": "2024-01-01T00:00:00+00:00", "patches": []}
    ck_later = {
        "id": "ck_2", "keep_seq": 1, "ts": "2024-01-01T00:00:01+00:00",
        "patches": [{"id": "pt_real", "path": "pkg/mathx.py"}],
    }
    # 让台账里的这条记录就是上面那条（id 换成真实的）
    real_id = intel.patches("p_real").list(limit=1)[0]["id"]
    ck_later["patches"] = [{"id": real_id, "path": "pkg/mathx.py"}]

    project = {"id": "p_real", "root_path": str(root)}
    result = rewind_to(
        ck_target,
        checkpoints=[ck_target, ck_later],
        transcript=ts,
        session_id="s_real",
        revert_patch=lambda pid: intel.revert_patch(project, pid),
    )

    assert result["reverted"] and result["reverted"][0]["path"] == "pkg/mathx.py"
    assert (target / "mathx.py").read_text(encoding="utf-8") == before
    assert ts.replay("s_real") == []
    # 回滚本身也进台账（历史里能看到"谁在什么时候回了什么"）
    assert any(p["tool"] == "revert_patch" for p in intel.patches("p_real").list(limit=5))


def test_rewind_refuses_when_the_file_changed_again(agent_home, tmp_path) -> None:
    """文件在那次改动之后又被改过 → 拒绝写回（不覆盖别人的工作）。"""
    from code_agent.codeintel.service import CodeIntelService

    root = tmp_path / "repo2"
    root.mkdir()
    (root / "a.py").write_text("HUMAN EDIT\n", encoding="utf-8")   # 已经不是"改动之后"的内容
    intel = CodeIntelService(projects=None)
    store = intel.patches("p_conflict")
    store.record(rel="a.py", session_id="s_conf", tool="edit_file", before="OLD\n", after="NEW\n")
    pid = intel.patches("p_conflict").list(limit=1)[0]["id"]

    ts = TranscriptStore(transcripts_dir())
    ts.append("s_conf", EventType.assistant_message, {"text": "x"})
    target = {"id": "ck_1", "keep_seq": 0, "ts": "2024-01-01T00:00:00+00:00", "patches": []}
    later = {"id": "ck_2", "keep_seq": 1, "ts": "2024-01-01T00:00:01+00:00",
             "patches": [{"id": pid, "path": "a.py"}]}
    project = {"id": "p_conflict", "root_path": str(root)}

    result = rewind_to(target, checkpoints=[target, later], transcript=ts, session_id="s_conf",
                       revert_patch=lambda x: intel.revert_patch(project, x))

    assert result["reverted"] == []
    assert "拒绝自动回滚" in result["failed"][0]["reason"]
    assert (root / "a.py").read_text(encoding="utf-8") == "HUMAN EDIT\n", "拒绝时绝不许动文件"


# ---------------------------------------------------------------------------
# 回退记忆
# ---------------------------------------------------------------------------
def test_rewind_summary_lists_falsified_routes() -> None:
    from code_agent.models.events import Event, ToolCall

    events = [
        Event(seq=1, type=EventType.tool_use,
              payload={"call": ToolCall(id="c1", name="bash", arguments={"command": "pytest -q"}).model_dump()}),
        Event(seq=2, type=EventType.tool_result, payload={"call_id": "c1", "result": "[tool error] 退出码 1"}),
        Event(seq=3, type=EventType.assistant_message, payload={"text": "改错了，我再试一次", "stop_reason": "end_turn"}),
    ]
    text = summarize(events, reverted=[{"path": "pkg/mathx.py"}], keep_seq=0)
    assert "回退记忆" in text and "不要重走" in text
    assert "pkg/mathx.py（1 处）" in text
    assert "bash(pytest -q) ×1" in text
    assert "改错了，我再试一次" in text
    assert "不可撤销" in text, "工作区之外的影响要说明不可逆，不能假装全撤了"


def test_rewind_notes_inject_the_last_few(agent_home) -> None:
    notes = RewindNotes("s_notes")
    assert notes.context_block() == "", "没有记忆时不该产生空块噪音"
    for i in range(6):
        notes.append(f"第 {i} 次回退的教训")
    block = notes.context_block(limit=2)
    assert "第 5 次" in block and "第 4 次" in block and "第 0 次" not in block


# ---------------------------------------------------------------------------
# 接到会话上
# ---------------------------------------------------------------------------
class _StubIntel:
    """最小的 patch 台账替身：只实现回退编排真正用到的两个方法。"""

    def __init__(self) -> None:
        self.reverted: list[str] = []

    def patches(self, project_id: str):
        outer = self

        class _Store:
            @staticmethod
            def list_by_session(session_id: str, since: str = ""):
                return [{"id": "pt_1", "path": "a.py", "session_id": session_id}]

        return _Store()

    def revert_patch(self, project: dict, patch_id: str) -> dict:
        self.reverted.append(patch_id)
        return {"ok": True, "path": "a.py", "patch_id": patch_id}


@pytest.fixture
def manager(agent_home):
    db = Database(agent_home / "ck.db")
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )


def _runtime(manager: SessionManager):
    db = manager.session_repo.db
    uid = UserRepo(db).create("u_ck", "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    return manager.get_runtime(meta["id"], uid)


def test_runtime_records_paired_checkpoints_and_rewinds(manager) -> None:
    rt = _runtime(manager)
    intel = _StubIntel()
    rt.code_intel = intel
    rt.code_project = {"id": "p_x", "root_path": str(rt.workspace)}
    rt.router.provider_override = OfflineProvider(
        script=[OfflineProvider.text_turn("第一轮结论"), OfflineProvider.text_turn("第二轮结论")]
    )

    asyncio.run(rt.submit("第一件事"))
    cks = rt.checkpoints()
    kinds = [c["kind"] for c in cks]
    assert kinds[:2] == ["prompt", "segment"], kinds
    assert cks[1]["patches"] and cks[1]["patches"][0]["id"] == "pt_1"
    assert cks[0]["patches"] == [], "发消息前的检查点不该认领任何改动"

    first_ck = cks[0]
    result = rt.rewind(first_ck["id"])

    assert intel.reverted == ["pt_1"], "回退要撤掉目标点之后的改动"
    # 轨迹退回：第一条 prompt 本身也被撤掉（它就在那个检查点之后）
    remaining = rt.transcript.replay(rt.session_id)
    assert [e.seq for e in remaining] == []
    assert result["keep_seq"] == 0
    # 回退记忆已落盘，下一轮会被注回上下文
    assert "回退记忆" in RewindNotes(rt.session_id).context_block()
    # 目标点留下、之后的入口清掉
    assert [c["keep_seq"] for c in rt.checkpoints()] == [0]


def test_rewind_memory_is_injected_into_the_next_prompt(manager) -> None:
    """回退之后的那一轮，模型必须看得到"哪些路线已经被证伪"。"""
    rt = _runtime(manager)
    RewindNotes(rt.session_id).append(
        "[回退记忆] 上一次尝试到这里被打断：bash(pytest -q) 已经失败过 3 次，不要重走。"
    )
    seen: list[str] = []

    class SpyProvider(OfflineProvider):
        async def chat(self, messages, tools):
            seen.append("\n".join(m.get("content") or "" for m in messages))
            return OfflineProvider.text_turn("换一条路。")

    rt.router.provider_override = SpyProvider()
    asyncio.run(rt.submit("再试一次"))

    assert any("不要重走" in text for text in seen), "回退记忆没有进到发给模型的上下文里"


# ---------------------------------------------------------------------------
# API
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


def test_checkpoint_and_rewind_endpoints(client) -> None:
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    client.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "第一件事"}, headers=h)
    body = client.get(f"/api/v1/sessions/{sid}/checkpoints", headers=h).json()
    assert body["busy"] is False
    assert [c["kind"] for c in body["checkpoints"]][:2] == ["prompt", "segment"]
    first = body["checkpoints"][0]

    events_before = client.get(f"/api/v1/sessions/{sid}/events", headers=h).json()
    assert events_before, "先确认轨迹里确实有东西可撤"

    r = client.post(
        f"/api/v1/sessions/{sid}/rewind", json={"checkpoint_id": first["id"]}, headers=h
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["keep_seq"] == 0 and out["dropped_events"] > 0
    assert "回退" in out["report"]

    # 轨迹已截断，但回退本身留下了一条可回放事件（刷新后不会显得凭空消失）
    events_after = client.get(f"/api/v1/sessions/{sid}/events", headers=h).json()
    assert len(events_after) == 1
    assert events_after[0]["payload"].get("rewind") is True

    # 失效的检查点入口已被清掉
    after = client.get(f"/api/v1/sessions/{sid}/checkpoints", headers=h).json()
    assert [c["keep_seq"] for c in after["checkpoints"]] == [0]


def test_rewind_rejects_unknown_checkpoint(client) -> None:
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    assert client.post(
        f"/api/v1/sessions/{sid}/rewind", json={"checkpoint_id": "ck_nope"}, headers=h
    ).status_code == 404
    assert client.post(
        f"/api/v1/sessions/{sid}/rewind", json={}, headers=h
    ).status_code == 422


def test_acceptance_endpoints(client) -> None:
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    assert client.get(f"/api/v1/sessions/{sid}/acceptance", headers=h).json()["declared"] is False

    body = client.put(
        f"/api/v1/sessions/{sid}/acceptance",
        json={"items": [{"label": "跑过测试", "check": "tool:bash"}], "note": "短计划"},
        headers=h,
    ).json()
    assert body["declared"] is True and body["total"] == 1 and body["done"] == 0
    assert body["first_unsatisfied"] == "跑过测试"
    assert body["rho"] == 0.0
    assert body["note"] == "短计划"

    # 全是坏判据 → 422（不是静默写一张空清单）
    assert client.put(
        f"/api/v1/sessions/{sid}/acceptance", json={"items": [{"label": "x", "check": "随手写"}]}, headers=h
    ).status_code == 422

    assert client.delete(f"/api/v1/sessions/{sid}/acceptance", headers=h).json()["ok"] is True
    assert client.get(f"/api/v1/sessions/{sid}/acceptance", headers=h).json()["declared"] is False
