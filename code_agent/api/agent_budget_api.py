"""Opt-in local team budgets, independent from hierarchy limit settings."""

from fastapi import Depends, FastAPI, HTTPException

from code_agent import settings
from code_agent.api.deps import CurrentUser, get_current_user


def mount_agent_budget_api(app: FastAPI) -> None:
    path = "/api/v1/settings/agent-team/budgets"

    @app.get(path)
    def read(user: CurrentUser = Depends(get_current_user)) -> dict:
        return settings.agent_team_budget_config()

    @app.put(path)
    def save(body: dict, user: CurrentUser = Depends(get_current_user)) -> dict:
        if user.role != "admin":
            raise HTTPException(403, "只有管理员可以修改团队预算")
        try:
            return settings.save_agent_team_budgets(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
