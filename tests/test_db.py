"""storage/db.py 单测：三张表 + Repo 基本行为。"""

import asyncio

from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo


def test_user_repo(tmp_path):
    db = Database(tmp_path / "t.db")
    users = UserRepo(db)
    assert users.count() == 0
    users.create("alice", "salt$hash")
    users.create("bob", "salt$hash", role="admin")
    assert users.count() == 2
    row = users.get_by_username("alice")
    assert row and row["role"] == "user"
    assert users.get_by_username("ghost") is None


def test_session_repo(tmp_path):
    db = Database(tmp_path / "t.db")
    users = UserRepo(db)
    sessions = SessionRepo(db)
    u = users.create("alice", "x")
    s1 = sessions.create(u["id"], folder="A")
    s2 = sessions.create(u["id"], folder="B")
    rows = sessions.list_for_user(u["id"])
    assert len(rows) == 2
    # M40：会话不再有模型相关列（模型由当前生效的配置档案决定）
    assert {r["folder"] for r in rows} == {"A", "B"}
    assert "model" not in rows[0] and "model_tier" not in rows[0]
    sessions.touch(s1["id"], title="新标题")
    assert sessions.get(s1["id"])["title"] == "新标题"
    sessions.close(s2["id"])
    assert sessions.get(s2["id"])["status"] == "closed"
    assert sessions.get(s2["id"])["closed_at"]


def test_approval_repo(tmp_path):
    db = Database(tmp_path / "t.db")
    approvals = ApprovalRepo(db)
    aid = approvals.create("s1", "u1", "bash", "make all", "不在白名单")
    row = approvals.get(aid)
    assert row["status"] == "pending"
    assert len(approvals.pending_for_session("s1")) == 1
    approvals.decide(aid, "approved", "u1")
    assert approvals.get(aid)["status"] == "approved"
    assert approvals.get(aid)["decided_at"]
    assert len(approvals.pending_for_session("s1")) == 0
    # 重复决策无效（幂等保护：只允许从 pending 迁移）
    approvals.decide(aid, "denied", "u1")
    assert approvals.get(aid)["status"] == "approved"
