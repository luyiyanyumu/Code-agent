"""IDE runtime endpoints. Only an explicit HTTP start request launches a process."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from code_agent.api.deps import CurrentUser, get_current_user, get_ws_user
from code_agent.codeintel.runtime import IdeRuntime, RuntimeBusy, RuntimeUnavailable


class TerminalStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shell: str = Field(default="", max_length=32)
    cwd: str = Field(default="", max_length=4096)
    cols: int = Field(default=80, ge=2, le=500)
    rows: int = Field(default=24, ge=2, le=300)


class RunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["command", "python", "node", "npm", "pytest", "maven", "gradle"]
    path: str = Field(default="", max_length=4096)
    script: str = Field(default="", max_length=512)
    command: str = Field(default="", max_length=8192)
    cwd: str = Field(default="", max_length=4096)
    name: str = Field(default="", max_length=200)


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc).strip("'\""))
    if isinstance(exc, RuntimeUnavailable):
        return HTTPException(503, str(exc))
    if isinstance(exc, RuntimeBusy):
        return HTTPException(409, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, str(exc))
    return HTTPException(500, f"{type(exc).__name__}: {exc}")


def mount_runtime_api(app: FastAPI, project_repo) -> IdeRuntime:
    service = IdeRuntime(project_repo)
    app.state.ide_runtime = service
    prefix = "/api/v1/code/projects/{project_id}"

    async def call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:
            raise _err(exc) from exc

    @app.get(prefix + "/terminals")
    async def terminals(project_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(service.list, project_id, user.user_id, "terminal")

    @app.post(prefix + "/terminals", status_code=201)
    async def create_terminal(project_id: str, body: TerminalStart, user: CurrentUser = Depends(get_current_user)):
        return await call(service.create_terminal, project_id, user.user_id, **body.model_dump())

    @app.delete(prefix + "/terminals/{terminal_id}")
    async def close_terminal(project_id: str, terminal_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(service.stop, project_id, user.user_id, terminal_id, "terminal")

    @app.get(prefix + "/run-configs")
    async def run_configs(project_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(service.run_configs, project_id, user.user_id)

    @app.get(prefix + "/runs")
    async def runs(project_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(service.list, project_id, user.user_id, "run")

    @app.post(prefix + "/runs", status_code=201)
    async def create_run(project_id: str, body: RunStart, user: CurrentUser = Depends(get_current_user)):
        return await call(service.create_run, project_id, user.user_id, **body.model_dump())

    @app.get(prefix + "/runs/{run_id}")
    async def run_detail(project_id: str, run_id: str, after_seq: int = Query(0, ge=0), user: CurrentUser = Depends(get_current_user)):
        return await call(service.snapshot, project_id, user.user_id, run_id, "run", after_seq)

    @app.post(prefix + "/runs/{run_id}/stop")
    async def stop_run(project_id: str, run_id: str, user: CurrentUser = Depends(get_current_user)):
        return await call(service.stop, project_id, user.user_id, run_id, "run")

    async def stream(ws: WebSocket, project_id: str, id: str, category: str):
        user = get_ws_user(ws)
        if not user:
            await ws.close(code=4401)
            return
        try:
            raw_after = ws.query_params.get("after_seq", "0")
            if len(raw_after) > 20:
                raise ValueError("after_seq 不合法")
            after = int(raw_after)
            if after < 0:
                raise ValueError("after_seq 必须大于等于 0")
            initial = service.snapshot(project_id, user.user_id, id, category, after)
        except KeyError:
            await ws.close(code=4404)
            return
        except ValueError:
            await ws.close(code=4422)
            return
        await ws.accept()
        await ws.send_json({"type": "ready", **{key: value for key, value in initial.items() if key != "events"}})

        async def send():
            nonlocal after
            while True:
                snapshot = service.snapshot(project_id, user.user_id, id, category, after)
                if snapshot["truncated"]:
                    await ws.send_json({"type": "gap", "first_seq": snapshot["first_seq"], "next_seq": snapshot["next_seq"]})
                    after = snapshot["first_seq"] - 1
                for event in snapshot["events"]:
                    await ws.send_json(event)
                    after = event["seq"]
                if snapshot["status"] != "running" and after >= snapshot["next_seq"]:
                    await ws.close(code=1000)
                    return
                await asyncio.sleep(0.04)

        async def receive():
            while True:
                try:
                    frame = await ws.receive_json()
                    if not isinstance(frame, dict):
                        raise ValueError("帧必须是 JSON object")
                    kind = frame.get("type")
                    if category != "terminal":
                        raise ValueError("运行输出流是只读的；停止请调用 stop")
                    if kind == "input":
                        await asyncio.to_thread(service.input, project_id, user.user_id, id, frame.get("data"))
                    elif kind == "resize":
                        await asyncio.to_thread(service.resize, project_id, user.user_id, id, frame.get("cols"), frame.get("rows"))
                    else:
                        raise ValueError("仅支持 input/resize 帧")
                except (ValueError, RuntimeBusy, OSError) as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError, KeyError, OSError):
            pass
        finally:
            for task in tasks:
                task.cancel()  # Only socket coroutines; subprocess readers never cancelled.
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await ws.close()

    @app.websocket(prefix + "/terminals/{terminal_id}/stream")
    async def terminal_stream(ws: WebSocket, project_id: str, terminal_id: str):
        await stream(ws, project_id, terminal_id, "terminal")

    @app.websocket(prefix + "/runs/{run_id}/stream")
    async def run_stream(ws: WebSocket, project_id: str, run_id: str):
        await stream(ws, project_id, run_id, "run")

    return service
