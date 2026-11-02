"""Owned-session model catalogs and selection, independent of global activation."""
from __future__ import annotations

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from code_agent.api.deps import CurrentUser, get_current_user


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(default="", max_length=160)
    model: str = Field(default="", max_length=160)
    reasoningEffort: str | None = Field(default=None, max_length=32)

    @field_validator("provider", "model", "reasoningEffort")
    @classmethod
    def clean(cls, value):
        if value is None:
            return None
        value = value.strip()
        if any(ord(char) < 32 for char in value):
            raise ValueError("模型标识不能包含控制字符")
        return value


def mount_model_selection_api(app, manager):
    def call(method, session_id, user, *args):
        try:
            return method(session_id, user.user_id, *args)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="会话不存在或无权访问") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/sessions/{session_id}/models")
    def session_models(session_id: str, user: CurrentUser = Depends(get_current_user)):
        return call(manager.model_options, session_id, user)

    @app.put("/api/v1/sessions/{session_id}/model")
    def select_session_model(session_id: str, body: ModelSelection, user: CurrentUser = Depends(get_current_user)):
        return call(manager.select_model, session_id, user, body.provider, body.model, body.reasoningEffort)
