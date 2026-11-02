"""Explicit IDE actions for a project's frontend/backend development session."""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.api.runtime_api import _err


class IntegrationServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["launch", "attach"] = "launch"
    kind: Literal["command", "python", "node", "npm", "maven", "gradle"] = "command"
    cwd: str = Field(default="", max_length=4096)
    path: str = Field(default="", max_length=4096)
    script: str = Field(default="", max_length=512)
    command: str = Field(default="", max_length=8192)
    args: list[str] = Field(default_factory=list, max_length=128)
    env: dict[str, str] = Field(default_factory=dict, max_length=64)


class IntegrationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="前后端联调", max_length=200)
    frontend: IntegrationServiceConfig = Field(default_factory=IntegrationServiceConfig)
    backend: IntegrationServiceConfig = Field(default_factory=IntegrationServiceConfig)
    frontend_url: str = Field(default="http://127.0.0.1:5173/", max_length=4096)
    backend_url: str = Field(default="http://127.0.0.1:8080/", max_length=4096)
    health_url: str = Field(default="", max_length=4096)
    api_prefix: str = Field(default="/api", max_length=512)
    strip_api_prefix: bool = False
    startup_timeout: float = Field(default=60, ge=1, le=300)


class IntegrationRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["all", "frontend", "backend"] = "all"


def mount_integration_api(app: FastAPI, *, runtime, project_service, design_service=None, integration=None, proxy=None):
    from code_agent.codeintel.integration_runtime import IntegrationRuntime
    from code_agent.codeintel.integration_proxy import IntegrationProxy
    from code_agent.api.integration_proxy_api import mount_integration_proxy_api

    integration = integration or IntegrationRuntime(runtime)
    proxy = proxy or IntegrationProxy(integration, design_service=design_service, project_repo=project_service)
    app.state.ide_integration = integration
    app.state.integration_proxy = proxy
    prefix = "/api/v1/code/projects/{project_id}/integration"

    async def call(method, project_id, user, *args, **kwargs):
        try:
            return await asyncio.to_thread(method, project_id, user.user_id, *args, **kwargs)
        except Exception as exc:
            raise _err(exc) from exc

    @app.get(prefix)
    async def state(project_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(integration.snapshot, project_id, user)

    @app.get(prefix + "/config")
    async def configuration(project_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(integration.configuration, project_id, user)

    @app.put(prefix + "/config")
    async def save_configuration(project_id: str, body: IntegrationConfig, user: CurrentUser = Depends(get_current_user)):
        return await call(integration.save_config, project_id, user, body.model_dump())

    @app.post(prefix + "/start", status_code=202)
    async def start(project_id: str, body: IntegrationConfig | None = None, user: CurrentUser = Depends(get_current_user)):
        return await call(integration.start, project_id, user, body.model_dump() if body else None)

    @app.post(prefix + "/stop", status_code=202)
    async def stop(project_id: str, body: IntegrationRole, user: CurrentUser = Depends(get_current_user)):
        return await call(integration.stop, project_id, user, role=body.role)

    @app.post(prefix + "/restart", status_code=202)
    async def restart(project_id: str, body: IntegrationRole, user: CurrentUser = Depends(get_current_user)):
        return await call(integration.restart, project_id, user, role=body.role)

    mount_integration_proxy_api(app, proxy)
    return integration
