"""M5 测试：rrule 子集、用量统计、审计 hash chain、自动化调度（含 strict fail-closed）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.automations import Scheduler, next_occurrence, parse_rrule
from code_agent.automations.scheduler import compute_initial_next_run
from code_agent.config import transcripts_dir
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import (
    ApprovalRepo,
    AuditRepo,
    AutomationRepo,
    Database,
    SessionRepo,
    UsageRepo,
    UserRepo,
)
from code_agent.storage.jsonl import TranscriptStore


# ----------------------------------------------------------------------
# rrule 子集
# ----------------------------------------------------------------------
def test_parse_rrule_basic():
    p = parse_rrule("FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=30")
    assert p["FREQ"] == "DAILY" and p["INTERVAL"] == "2"
    assert p["BYHOUR"] == "9" and p["BYMINUTE"] == "30"


def test_daily_next():
    anchor = datetime(2026, 9, 1, 9, 0)
    after = datetime(2026, 9, 8, 10, 0)
    nxt = next_occurrence("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", after, anchor)
    assert nxt == datetime(2026, 9, 9, 9, 0)


def test_daily_interval_two():
    anchor = datetime(2026, 9, 1, 9, 0)
    after = datetime(2026, 9, 4, 10, 0)
    nxt = next_occurrence("FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0", after, anchor)
    # 锚点 9/1，隔天：9/1, 9/3, 9/5 -> 下一个是 9/5
    assert nxt == datetime(2026, 9, 5, 9, 0)


def test_weekly_byday():
    anchor = datetime(2026, 9, 1, 9, 0)  # 周二
    after = datetime(2026, 9, 2, 10, 0)  # 周三 10:00（当天 9 点已过）
    nxt = next_occurrence("FREQ=WEEKLY;BYDAY=MO,WE;BYHOUR=9;BYMINUTE=0", after, anchor)
    assert nxt == datetime(2026, 9, 7, 9, 0)  # 下周一


def test_hourly_interval():
    anchor = datetime(2026, 9, 1, 0, 0)
    after = datetime(2026, 9, 1, 7, 30)
    nxt = next_occurrence("FREQ=HOURLY;INTERVAL=6", after, anchor)
    assert nxt == datetime(2026, 9, 1, 12, 0)


def test_monthly_bymonthday():
    anchor = datetime(2026, 1, 15, 9, 0)
    after = datetime(2026, 3, 2, 0, 0)
    nxt = next_occurrence("FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0", after, anchor)
    assert nxt == datetime(2026, 4, 1, 9, 0)


def test_bad_rrule_returns_none():
    assert next_occurrence("FREQ=WHENEVER", datetime(2026, 9, 8), datetime(2026, 9, 1)) is None


def test_compute_initial_next_run():
    now_s = "2026-09-08T01:00:00+00:00"
    once = compute_initial_next_run("once", None, "2026-09-09T09:00:00+00:00", now_s)
    assert once == "2026-09-09T09:00:00+00:00"
    rec = compute_initial_next_run("recurring", "FREQ=DAILY;BYHOUR=9;BYMINUTE=0", None, now_s)
    assert rec is not None and rec > now_s
    assert compute_initial_next_run("recurring", "GARBAGE", None, now_s) is None


# ----------------------------------------------------------------------
# 用量与审计（Repo 层）
# ----------------------------------------------------------------------
@pytest.fixture
def db(agent_home):
    return Database(agent_home / "t.db")


def test_usage_summary(db):
    """M36：用量按**模型**分组（原来按档位，档位已去掉）。"""
    repo = UsageRepo(db)
    repo.record("s1", "u1", "deepseek-v4-pro", 100, 50)
    repo.record("s2", "u1", "deepseek-v4-flash", 200, 80)
    repo.record("s3", "u2", "deepseek-v4-pro", 10, 5)
    s = repo.summary_for_user("u1")
    assert s["prompts"] == 2
    assert s["input_tokens"] == 300
    assert s["output_tokens"] == 130
    assert {t["model"] for t in s["by_model"]} == {"deepseek-v4-pro", "deepseek-v4-flash"}
    # 分组内按下行数排序：输入多的在前
    assert s["by_model"][0]["model"] == "deepseek-v4-flash"


def test_audit_chain_verify_and_tamper(db):
    repo = AuditRepo(db)
    for i in range(3):
        repo.append("s1", "u1", "bash", f'{{"command":"echo {i}"}}', "ok")

    assert repo.verify() == {"ok": True, "broken_at": None}

    # 篡改中间节点 -> 断链定位到 seq=2
    db.execute("UPDATE audit_log SET outcome='tampered' WHERE seq=2")
    result = repo.verify()
    assert result["ok"] is False
    assert result["broken_at"] == 2


def test_audit_chain_delete_breaks(db):
    """删中间行同样会断链（下一行的 prev_hash 对不上）。"""
    repo = AuditRepo(db)
    for i in range(3):
        repo.append("s1", "u1", "bash", f'{{"command":"echo {i}"}}', "ok")
    db.execute("DELETE FROM audit_log WHERE seq=2")
    result = repo.verify()
    assert result["ok"] is False and result["broken_at"] == 3


# ----------------------------------------------------------------------
# 运行时集成：用量落库 + 审计留痕
# ----------------------------------------------------------------------
class RecordingProvider(OfflineProvider):
    def __init__(self, script):
        super().__init__(script)
        self.seen = []

    async def chat(self, messages, tools):
        import copy

        self.seen.append(copy.deepcopy(messages))
        return await super().chat(messages, tools)


@pytest.fixture
def manager(db):
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
        usage_repo=UsageRepo(db),
        audit_repo=AuditRepo(db),
    )


def _make_user(db, username="u_test") -> str:
    users = UserRepo(db)
    existing = db.query_one("SELECT id FROM users WHERE username=?", (username,))
    return existing["id"] if existing else users.create(username, "x", role="user")["id"]


def _make_runtime(db, manager, username="u_test"):
    uid = _make_user(db, username)
    meta = manager.session_repo.create(uid)
    return manager.get_runtime(meta["id"], uid)


def _install(db, runtime, script):
    rec = RecordingProvider(script)
    runtime.router.provider_override = rec
    return rec


def test_usage_and_audit_recorded_on_tool_run(db, manager):
    runtime = _make_runtime(db, manager)
    _install(
        db,
        runtime,
        [OfflineProvider.tool_call_turn("bash", {"command": "echo hi"}), OfflineProvider.text_turn("done")],
    )
    result = asyncio.run(runtime.submit("跑个命令"))

    assert result.text == "done"
    usage = UsageRepo(db).summary_for_user(runtime.user_id)
    assert usage["prompts"] == 1 and usage["input_tokens"] > 0

    audit = AuditRepo(db)
    entries = audit.list_entries()
    assert [(e["tool_name"], e["outcome"]) for e in entries] == [("bash", "ok")]
    assert audit.verify()["ok"] is True


def test_strict_automation_denies_approval(db, manager):
    """strict 模式：非白名单命令审批一律拒绝并留痕（fail closed）。"""
    runtime = _make_runtime(db, manager)
    runtime.strict = True  # 模拟自动化 runtime
    _install(
        db,
        runtime,
        [OfflineProvider.tool_call_turn("bash", {
            "command": "touch x.txt",
            "_approval": {"purpose": "Create the test fixture file", "effects": ["Create x.txt"]},
        }), OfflineProvider.text_turn("never")],
    )
    result = asyncio.run(runtime.submit("建个文件"))

    # 脚本化收口不看工具结果，模型文本是 "never"；拒绝要靠审计与审批表验证
    assert result.text == "never"
    audit = AuditRepo(db)
    assert [e["outcome"] for e in audit.list_entries()] == ["denied_approval"]
    # approvals 表留下拒绝记录
    rows = db.query_all("SELECT status FROM approvals")
    assert rows and rows[-1]["status"] == "denied"


# ----------------------------------------------------------------------
# 调度器
# ----------------------------------------------------------------------
def _automation_db(db):
    return AutomationRepo(db)


def test_scheduler_once_run(db, manager):
    uid = _make_user(db, "u_auto")
    repo = _automation_db(db)
    row = repo.create(uid, "晨报", "生成今日简报", "once", None, "2026-09-08T00:00:00+00:00")
    repo.mark_run(row["id"], "2020-01-01T00:00:00+00:00", status="active")  # 已到期

    # 注入 recording provider 到 router 缓存（所有新会话共用）
    manager.router.provider_override = RecordingProvider([OfflineProvider.text_turn("auto done")])

    scheduler = Scheduler(repo, manager.run_automation)
    asyncio.run(scheduler._tick())

    assert repo.get(row["id"])["status"] == "completed"
    # 自动化确实通过 manager 跑了一轮（会话已创建、执行并关闭；
    # submit 会把 title 覆盖为 prompt 前 40 字符，属已知行为）
    sessions = db.query_all("SELECT title, status FROM sessions WHERE user_id=?", (uid,))
    assert any(s["status"] == "closed" for s in sessions)
    # 用量与审计也应各有一条
    assert UsageRepo(db).summary_for_user(uid)["prompts"] == 1
    assert AuditRepo(db).verify()["ok"] is True


def test_scheduler_recurring_advances(db, manager):
    uid = _make_user(db, "u_auto2")
    repo = _automation_db(db)
    row = repo.create(
        uid, "每日提醒", "提醒", "recurring", "FREQ=DAILY;BYHOUR=9;BYMINUTE=0", None
    )
    repo.mark_run(row["id"], "2020-01-01T00:00:00+00:00", status="active")

    manager.router.provider_override = RecordingProvider([OfflineProvider.text_turn("ok")])
    scheduler = Scheduler(repo, manager.run_automation)
    asyncio.run(scheduler._tick())

    after = repo.get(row["id"])
    assert after["status"] == "active"
    assert after["next_run_at"] is not None
    assert after["next_run_at"] > "2026"  # 推进到了未来
    assert after["last_run_at"] is not None


@pytest.mark.parametrize("prompt", ["完成一个普通任务", "# 检查\n读取数据\n---step---\n# 交付\n给出结论"])
def test_automation_submits_entire_prompt_once_without_step_orchestration(db, manager, prompt):
    uid = _make_user(db, "single_auto")
    manager.router.provider_override = RecordingProvider([OfflineProvider.text_turn("任务已完成")])
    result = asyncio.run(manager.run_automation(uid, "普通任务", prompt))
    assert result.text == "任务已完成"
    sessions = db.query_all("SELECT id,status FROM sessions WHERE user_id=?", (uid,))
    assert len(sessions) == 1 and sessions[0]["status"] == "closed"
    runtime = manager._runtimes[sessions[0]["id"]]
    assert runtime.strict is True
    prompts = [event.payload["text"] for event in runtime.transcript.replay(runtime.session_id) if event.type == "user_message"]
    assert prompts == [prompt]
    assert UsageRepo(db).summary_for_user(uid)["prompts"] == 1


def test_scheduler_recurring_bad_rrule_pauses(db):
    uid = _make_user(db, "u_x")
    repo = _automation_db(db)
    row = repo.create(uid, "坏规则", "x", "recurring", "FREQ=NOPE", None)
    repo.mark_run(row["id"], "2020-01-01T00:00:00+00:00", status="active")

    scheduler = Scheduler(repo, lambda *a: None)
    asyncio.run(scheduler._tick())
    assert repo.get(row["id"])["status"] == "paused"


def test_scheduler_no_duplicate_fire_on_crash(db, manager):
    """执行协程崩溃也不能重复触发：next_run_at 已先推进。"""
    uid = _make_user(db, "u_auto3")
    repo = _automation_db(db)
    row = repo.create(uid, "炸", "x", "once", None, "2026-01-01T00:00:00+00:00")
    repo.mark_run(row["id"], "2020-01-01T00:00:00+00:00", status="active")

    async def boom(*a):
        raise RuntimeError("执行炸了")

    scheduler = Scheduler(repo, boom)
    asyncio.run(scheduler._tick())
    # once 型已标记 completed，即使 submit_fn 抛异常
    assert repo.get(row["id"])["status"] == "completed"
