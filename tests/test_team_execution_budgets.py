"""Budgets are checked against actual local ledger usage and wall-clock time."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent import settings
from code_agent.api.agent_budget_api import mount_agent_budget_api
from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.models.events import TokenUsage
from code_agent.sessions.team_budget import ExecutionBudgetExceeded, TeamBudget
from code_agent.storage.db import Database, SessionRepo, UserRepo


@pytest.fixture
def ledger(agent_home):
    db = Database(agent_home / "budget.db")
    user = UserRepo(db).create("budget", "test-only")["id"]
    sessions = SessionRepo(db)
    yield db, [sessions.create(user_id=user)["id"] for _ in range(3)]
    db.close()


def test_disabled_defaults_and_strict_persisted_limits(agent_home):
    assert settings.agent_team_budget_config() == {"task_tokens": 0, "task_seconds": 0, "team_tokens": 0, "team_seconds": 0}
    assert settings.save_agent_team_budgets({"task_tokens": 100})["task_tokens"] == 100
    for body in ({"task_tokens": True}, {"team_seconds": -1}, {"team_tokens": 100_000_001}, {"unknown": 1}):
        with pytest.raises(ValueError):
            settings.save_agent_team_budgets(body)
    assert settings.agent_team_budget_config()["task_tokens"] == 100


def test_global_budget_clamps_delegation_and_counts_cached_tokens_once(ledger):
    db, ids = ledger
    settings.save_agent_team_budgets({"task_tokens": 100, "task_seconds": 30})
    task = TeamBudget(db, ids[0])
    task.begin("p", 1, {"max_tokens": 500, "max_seconds": 100})
    assert task.current["max_tokens"] == 100 and task.current["max_seconds"] == 30
    task.consume(TokenUsage(input_tokens=80, output_tokens=20, cached_tokens=60))
    assert task.status()["tokens"] == 100
    with pytest.raises(ExecutionBudgetExceeded, match="任务 Token"):
        task.check()


def test_descendants_share_chain_budget_new_user_task_gets_new_scope(ledger):
    db, ids = ledger
    settings.save_agent_team_budgets({"team_tokens": 150})
    lead, child, grandchild = [TeamBudget(db, sid) for sid in ids]
    lead.begin("p", 1, None)
    child.begin("p", 1, None, parent=lead.current)
    grandchild.begin("p", 1, None, parent=child.current)
    assert lead.current["scope_id"] == child.current["scope_id"] == grandchild.current["scope_id"]
    child.consume(TokenUsage(input_tokens=50, output_tokens=30))
    grandchild.consume(TokenUsage(input_tokens=50, output_tokens=20))
    with pytest.raises(ExecutionBudgetExceeded, match="团队任务 Token"):
        lead.check()
    prior_scope = lead.current["scope_id"]
    lead.begin("p", 2, None, continuation=True)
    assert lead.current["scope_id"] == prior_scope  # Completion wake doesn't reset the tree limit.
    lead.begin("p", 3, None)
    assert lead.current["scope_id"] != prior_scope and lead.status()["team_tokens"] == 0
    lead.check()


def test_duration_budget_expires_and_cross_project_parent_cannot_share_scope(ledger):
    db, ids = ledger
    first, second = [TeamBudget(db, sid) for sid in ids[:2]]
    first.begin("p", 1, {"max_seconds": 1})
    first.current["started_at"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(ExecutionBudgetExceeded, match="耗时预算"):
        first.check()
    second.begin("other-project", 1, None, parent=first.current)
    assert second.current["scope_id"] != first.current["scope_id"]


def test_budget_endpoint_checks_admin_and_leaves_hierarchy_settings_unchanged(agent_home):
    app = FastAPI()
    user = CurrentUser(user_id="owner", username="owner", role="admin")
    app.dependency_overrides[get_current_user] = lambda: user
    mount_agent_budget_api(app)
    with TestClient(app) as client:
        path = "/api/v1/settings/agent-team/budgets"
        assert client.get(path).json()["team_tokens"] == 0
        assert client.put(path, json={"team_tokens": 300}).json()["team_tokens"] == 300
        assert client.put(path, json={"task_seconds": True}).status_code == 400
        user.role = "user"
        assert client.put(path, json={"team_tokens": 500}).status_code == 403
    assert settings.agent_team_config() == {"max_depth": 3, "max_parallel": 4}
