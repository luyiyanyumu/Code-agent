"""Owned project team inspection and human stop/audit operations."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from code_agent.agent.project_team import ProjectTeam, TeamError
from code_agent.api.deps import CurrentUser, get_current_user


Tree = Annotated[str, Query(min_length=1, max_length=101, pattern=r"^(?:main|task:[A-Za-z0-9_-]{1,96})$")]
WritePath = Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
DependencyId = Annotated[str, Field(strict=True, min_length=1, max_length=64)]


class AgentScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(strict=True, max_length=64, default="")


class AgentAudit(AgentScope):
    activation_id: StrictInt = Field(ge=1, le=2**63 - 1)
    decision: Literal["accepted", "rejected"]
    reason: str = Field(strict=True, min_length=1, max_length=4000)


class AgentMessage(AgentScope):
    message: str = Field(strict=True, min_length=1, max_length=40000)


class AgentBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_tokens: StrictInt = Field(ge=0, le=10000000, default=0)
    max_seconds: StrictInt = Field(ge=0, le=86400, default=0)


class AgentFollowup(AgentScope):
    prompt: str = Field(strict=True, min_length=1, max_length=40000)
    context_strategy: Literal["task_brief", "none", "all"] = "task_brief"
    budget: AgentBudget | None = None
    write_paths: list[WritePath] | None = Field(default=None, max_length=64)
    delegation_reason: str = Field(strict=True, max_length=2000, default="")
    depends_on: list[DependencyId] | None = Field(default=None, max_length=16)


class AgentWait(AgentScope):
    session_ids: list[str] = Field(min_length=1, max_length=64)
    timeout_seconds: float = Field(strict=True, ge=0, le=60, allow_inf_nan=False, default=30)
    mode: Literal["any", "all"] = "all"
    after_cursors: dict[str, str] | None = None


def mount_agent_team_api(app: FastAPI, *, team: ProjectTeam, service: object) -> None:
    from code_agent.api.agent_budget_api import mount_agent_budget_api

    app.state.project_agent_team = team
    mount_agent_budget_api(app)
    prefix = "/api/v1/code/projects/{project_id}/agents"

    def owned(project_id: str, user: CurrentUser) -> None:
        try:
            service.get_owned(user.user_id, project_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc

    def conversation_root(project_id: str, conversation_id: str, user: CurrentUser) -> dict:
        snapshot = team.sync(project_id, user.user_id, "main", conversation_id=conversation_id)
        lead = snapshot.get("lead")
        if not lead or lead["project_id"] != project_id:
            raise KeyError("对话不存在或无权访问")
        team._member(lead["session_id"], user.user_id)
        return lead

    def target(project_id: str, session_id: str, user: CurrentUser, conversation_id: str = "") -> dict:
        owned(project_id, user)
        descriptor = team.descriptor(session_id)
        row = team._row(session_id)
        if not descriptor or descriptor["project_id"] != project_id or not row or row["user_id"] != user.user_id:
            raise HTTPException(404, "智能体不存在或无权访问")
        if conversation_id:
            lead = conversation_root(project_id, conversation_id, user)
            if descriptor["lead_session_id"] != lead["session_id"]:
                raise KeyError("智能体不属于当前对话")
        return descriptor

    @app.get(prefix)
    def agents(project_id: str, tree: Tree = "main", conversation_id: str = Query(default="", max_length=64),
               user: CurrentUser = Depends(get_current_user)) -> dict:
        # Resolve the owning conversation in a thread pool, keeping streams and
        # stop responsive while any root metadata is bound on first access.
        owned(project_id, user)
        try:
            if conversation_id:
                return team.sync(project_id, user.user_id, tree, conversation_id=conversation_id)
            return team.sync(project_id, user.user_id, tree)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except TeamError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(prefix + "/{session_id}/stop")
    async def stop(project_id: str, session_id: str, body: AgentScope | None = None,
                   user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            row = target(project_id, session_id, user, body.conversation_id if body else "")
            return await team.stop(row["lead_session_id"], user.user_id, session_id, allow_self=True)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except TeamError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(prefix + "/{session_id}/interrupt")
    async def interrupt(project_id: str, session_id: str, body: AgentScope | None = None,
                        user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            row = target(project_id, session_id, user, body.conversation_id if body else "")
            return await team.interrupt(row["lead_session_id"], user.user_id, session_id, allow_self=True)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except TeamError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(prefix + "/{session_id}/message")
    async def message(project_id: str, session_id: str, body: AgentMessage, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            row = target(project_id, session_id, user, body.conversation_id)
            return await team.send_message(row["lead_session_id"], user.user_id, session_id, body.message)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except TeamError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(prefix + "/{session_id}/followup")
    async def followup(project_id: str, session_id: str, body: AgentFollowup, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            row = target(project_id, session_id, user, body.conversation_id)
            # Omitted scope/dependency fields preserve the previous activation's
            # contract; [] remains an explicit read-only scope/dependency reset.
            return await team.followup(row["lead_session_id"], user.user_id, session_id,
                                       **body.model_dump(exclude={"conversation_id"}, exclude_unset=True))
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except TeamError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(prefix + "/wait")
    async def wait(project_id: str, body: AgentWait, user: CurrentUser = Depends(get_current_user)) -> dict:
        owned(project_id, user)
        try:
            if body.conversation_id:
                lead = conversation_root(project_id, body.conversation_id, user)
            else:
                first = target(project_id, body.session_ids[0], user)
                lead = team._member(first["lead_session_id"], user.user_id)
            for session_id in body.session_ids:
                row = target(project_id, session_id, user)
                if row["lead_session_id"] != lead["session_id"]:
                    raise KeyError("智能体不属于当前对话")
            return await team.wait(lead["session_id"], user.user_id, body.session_ids, body.timeout_seconds,
                                   mode=body.mode, after_cursors=body.after_cursors, progress=False)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except TeamError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(prefix + "/{session_id}/audit")
    async def audit(project_id: str, session_id: str, body: AgentAudit, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            row = target(project_id, session_id, user, body.conversation_id)
            return team.audit(row["lead_session_id"], user.user_id, session_id, **body.model_dump(exclude={"conversation_id"}))
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except TeamError as exc:
            raise HTTPException(409, str(exc)) from exc

