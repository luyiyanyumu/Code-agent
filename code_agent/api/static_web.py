"""Optional same-origin hosting for the packaged workbench.

Only an explicitly configured build directory is served. Development keeps its
existing API-only behavior, while npm releases can run without Vite or nginx.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send


_HASHED_ASSET = re.compile(r"-[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9]+)+$")
_RESERVED_PATHS = {"api", "healthz", "docs", "redoc", "openapi.json"}


class WorkbenchFiles(StaticFiles):
    """Serve real build files, with a narrowly scoped HTML history fallback."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Existing WebSocket routes are registered before this mount. An
            # unknown socket must close normally rather than enter StaticFiles.
            await send({"type": "websocket.close", "code": 1000})
            return

        # Inspect the original decoded path before StaticFiles normalizes it.
        # In particular, Windows treats backslashes as directory separators.
        path = scope.get("path", "")
        if "\\" in path or "\0" in path or ".." in path.split("/"):
            raise HTTPException(status_code=404)
        first = self.get_path(scope).split(os.sep, 1)[0]
        if first in _RESERVED_PATHS:
            raise HTTPException(status_code=404)
        await super().__call__(scope, receive, send)

    async def get_response(self, path: str, scope: Scope) -> Response:
        # A symlink outside the build is an invalid path, even if its visible
        # name has no extension and would otherwise qualify for SPA fallback.
        try:
            directory = Path(self.directory).resolve()
            if not (directory / path).resolve().is_relative_to(directory):
                raise HTTPException(status_code=404)
        except (OSError, ValueError):
            raise HTTPException(status_code=404)
        served_path = path
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            # A missing script/image must never return HTML (including assets
            # with extensionless names). Only browser navigation can fall back.
            accept = dict(scope.get("headers", [])).get(b"accept", b"*/*")
            navigation = b"text/html" in accept or b"*/*" in accept
            asset = path == "assets" or path.startswith("assets" + os.sep)
            if exc.status_code != 404 or not navigation or asset or Path(path).suffix:
                raise
            served_path = "index.html"
            response = await super().get_response(served_path, scope)

        if Path(served_path).name == "index.html":
            response.headers["cache-control"] = "no-store"
        elif (served_path.startswith("assets" + os.sep)
              and _HASHED_ASSET.search(Path(served_path).name)):
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["cache-control"] = "no-cache"
        return response


def mount_workbench(app: FastAPI) -> None:
    """Mount last, and fail clearly if an explicitly configured build is absent."""
    configured = os.environ.get("CODE_AGENT_WEB_DIST", "").strip()
    if not configured:
        return
    directory = Path(configured).expanduser().resolve()
    if not directory.is_dir() or not (directory / "index.html").is_file():
        raise RuntimeError(f"CODE_AGENT_WEB_DIST must contain a built index.html: {directory}")
    app.mount("/", WorkbenchFiles(directory=directory), name="workbench")
