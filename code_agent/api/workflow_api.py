"""Project SOP workflow endpoints: persistence only, with no tool execution."""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import ValidationError

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.agent.project_team import TeamError
from code_agent.codeintel.projects import ProjectService
from code_agent.codeintel.sop_workflow import MAX_DOCUMENT_BYTES, WorkflowConflict, WorkflowSave, WorkflowStore

TreeId = Annotated[str, Query(min_length=1, max_length=101, pattern=r"^(?:main|task:[A-Za-z0-9_-]{1,96})$")]


async def workflow_body(request: Request) -> WorkflowSave:
    """Bound memory before decoding; reject JavaScript NaN/Infinity as invalid JSON."""
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_DOCUMENT_BYTES:
            raise HTTPException(413, "工作流内容过大，请精简节点记录")
        data.extend(chunk)

    def reject_constant(value: str) -> None:
        raise ValueError(f"JSON 不允许 {value}")

    try:
        parsed = json.loads(data, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(422, "工作流必须是有效的 JSON，数值必须有限") from exc
    try:
        return WorkflowSave.model_validate(parsed)
    except ValidationError as exc:
        # Input and exception context can include nonfinite values; returning only
        # locations/messages also avoids echoing users' full deliverables.
        raise HTTPException(422, exc.errors(include_input=False, include_context=False)) from exc


def mount_workflow_api(app: FastAPI, *, service: ProjectService, store: WorkflowStore, team: Any = None) -> None:
    app.state.sop_workflow_store = store
    prefix = "/api/v1/code/projects/{project_id}/workflow"

    def owned(user: CurrentUser, project_id: str) -> None:
        try:
            service.get_owned(user.user_id, project_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc

    @app.get(prefix)
    def get_workflow(project_id: str, tree: TreeId = "main", user: CurrentUser = Depends(get_current_user)) -> dict:
        # Ownership reads take the same SQLite lock as team/session writes. Run
        # the entire read in FastAPI's thread pool, not only store.get: waiting
        # for that lock on the event loop freezes all other requests and streams.
        try:
            owned(user, project_id)
            return store.get(project_id, tree)
        except sqlite3.Error as exc:
            raise HTTPException(503, "工作流存储暂时不可用，请重试") from exc

    @app.put(prefix)
    def save_workflow(project_id: str, body: WorkflowSave = Depends(workflow_body), tree: TreeId = "main", user: CurrentUser = Depends(get_current_user)) -> dict:
        # Tree validation and owner synchronization also use synchronous DB and
        # filesystem calls; none of them may run on the server event loop.
        try:
            owned(user, project_id)
            if team is not None:
                team.validate_tree(project_id, user.user_id, tree)
            saved = store.save(project_id, tree, body.workflow, body.expected_revision)
            if team is not None:
                team.sync(project_id, user.user_id, tree)
            return saved
        except WorkflowConflict as exc:
            raise HTTPException(409, {"message": str(exc), "current_revision": exc.current_revision}) from exc
        except TeamError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except sqlite3.Error as exc:
            raise HTTPException(503, "工作流存储暂时不可用，请重试") from exc
