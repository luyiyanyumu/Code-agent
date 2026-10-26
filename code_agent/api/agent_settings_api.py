"""Authenticated configuration for project Lead and recursive node agents."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from code_agent import settings
from code_agent.api.deps import CurrentUser, get_current_user


def mount_agent_settings_api(app: FastAPI) -> None:
    @app.get("/api/v1/settings/agent-team")
    def get_agent_team_settings(
        user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, int]:
        return settings.agent_team_config()

    @app.put("/api/v1/settings/agent-team")
    def put_agent_team_settings(
        body: dict[str, Any], user: CurrentUser = Depends(get_current_user),
    ) -> dict[str, int]:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")
        try:
            return settings.save_agent_team_config(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="智能体协作设置保存失败，请重试") from exc
