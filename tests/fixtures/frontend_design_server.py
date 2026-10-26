"""Browser-test server for temporary design projects; no database or models.

Only fixture-a and fixture-b beneath a caller-created temporary directory are
accessible. This entrypoint never constructs the application's SessionManager.
"""
from __future__ import annotations

import argparse
import tempfile
import sys
from pathlib import Path

# Standalone execution does not require installing the workspace as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn
import httpx
from fastapi import FastAPI

from code_agent.api.deps import CurrentUser
from code_agent.api.frontend_design_api import mount_frontend_design_api
from code_agent.codeintel.frontend_design import FrontendDesignService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, default=5296)
    args = parser.parse_args()
    fixture_root = Path(args.root).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if not fixture_root.is_relative_to(temp_root) or fixture_root == temp_root:
        raise SystemExit("Fixture root must be a dedicated temporary directory")
    if args.port != 5296:
        raise SystemExit("Only the private fixture port is supported")

    class FixtureProjects:
        def get_owned(self, user_id: str, project_id: str) -> dict:
            if user_id != "frontend-design-fixture" or project_id not in {"fixture-a", "fixture-b"}:
                raise KeyError(project_id)
            root = fixture_root / "projects" / project_id
            if not root.is_dir():
                raise KeyError(project_id)
            return {"id": project_id, "user_id": user_id, "root_path": str(root), "name": project_id, "available": True}

    app = FastAPI()
    app.state.auth_enabled = False
    app.state.local_user = CurrentUser("frontend-design-fixture", "fixture", "user")
    api_calls: list[dict] = []

    async def fixture_transport(request: httpx.Request) -> httpx.Response:
        """Virtual API only; the sole real network target is our private Vite."""
        target = request.url
        if target.scheme == "http" and target.host == "127.0.0.1" and target.port == 5337 and target.path.startswith("/api/"):
            if request.method not in {"GET", "HEAD"}:
                raise AssertionError("A write method reached the virtual read-only API")
            status = 418 if target.path == "/api/status" else 200 if target.path == "/api/demo" else 404
            api_calls.append({"method": request.method, "path": target.path, "query": dict(target.params), "status": status, "cookie": "cookie" in request.headers, "authorization": "authorization" in request.headers})
            payload = {"label": "虚构项目接口", "status": status, "method": request.method, "query": dict(target.params)}
            return httpx.Response(status, json=payload, headers={"X-Fixture-Readonly": "true"})
        if target.scheme == "http" and target.host == "127.0.0.1" and target.port == 5199 and request.method in {"GET", "HEAD"} and not target.path.startswith("/api"):
            # Existing real-Vue checks use this caller-owned throwaway Vite,
            # never a real project API or another host/port.
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=8) as client:
                return await client.request(request.method, str(target), headers={"Accept": request.headers.get("accept", "*/*")})
        raise AssertionError("Fixture transport rejected an unknown target")

    design = FrontendDesignService(transport=httpx.MockTransport(fixture_transport))
    mount_frontend_design_api(app, service=FixtureProjects(), design=design)

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True, "fixture_only": True}

    @app.get("/__fixture_api_stats")
    def api_stats() -> dict:
        return {"calls": list(api_calls), "count": len(api_calls)}

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error")


if __name__ == "__main__":
    main()
