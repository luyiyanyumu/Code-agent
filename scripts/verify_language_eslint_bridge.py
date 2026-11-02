"""Real ESLint open/edit/close acceptance through the production LSP manager."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time


async def exercise(args):
    from code_agent.codeintel.bundled_lsp import _launch
    from code_agent.codeintel.lsp import LspManager
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    definition = _launch(args.app_root, next(row for row in registry["servers"] if row["id"] == "eslint"))
    project = args.output / "ESLint 真实项目"
    project.mkdir(parents=True, exist_ok=False)
    (project / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    (project / "eslint.config.js").write_text('export default [{ rules: { "no-unused-vars": "error" } }];\n', encoding="utf-8")
    path = project / "demo.js"
    path.write_text('var unused = 1;\n', encoding="utf-8")
    index_events, messages = [], []
    manager = LspManager(config_path=args.output / "lsp.json", documentation_path=args.output / "docs.db",
                         on_diagnostics=lambda p, u, d: index_events.append({"uri": u, "diagnostics": d}))
    entry = {**definition, "id": "eslint", "name": "Bundled ESLint", "install_hint": "bundled"}
    session = None
    started = time.monotonic()
    async def wait(predicate):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(.05)
        raise AssertionError("Production ESLint bridge did not deliver diagnostics: " + str(messages[-4:]))
    try:
        session = await manager._start({"id": "fixture", "root_path": str(project)}, "javascript", "main", "fixture:main:eslint", entry)
        manager._sessions[session.key] = session
        manager.subscribe(session, messages.append)
        await manager.open_document(session, path.as_uri(), path.read_text(encoding="utf-8"))
        await wait(lambda: any(item.get("code") == "no-unused-vars" for item in session.diagnostics.get(path.as_uri(), [])))
        assert index_events and any(m.get("method") == "textDocument/publishDiagnostics" for m in messages)
        first = session.diagnostic_versions[path.as_uri()]
        await manager.change_document(session, path.as_uri(), 'export const used = 1;\n')
        await wait(lambda: session.diagnostic_versions.get(path.as_uri()) == 2 and session.diagnostics[path.as_uri()] == [])
        await manager.close_document(session, path.as_uri())
        assert path.as_uri() not in session.documents and not session.diagnostic_pull_tasks
        return {"passed": True, "feature": "production ESLint open/edit/close diagnostics + index/listener publication",
                "initialVersion": first, "fixedVersion": 2, "elapsedSeconds": round(time.monotonic() - started, 2),
                "publishEvents": len(index_events), "modelRequests": 0}
    finally:
        if session:
            await manager.stop(session.key)
        await manager.stop_all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.app_root, args.registry, args.output = args.app_root.resolve(), args.registry.resolve(), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.app_root))
    for name in list(os.environ):
        if name.startswith(("DEEPSEEK_", "OPENAI_", "CODE_AGENT_", "CH_AGENT_", "PYTHONPATH", "NODE_PATH", "NODE_OPTIONS")):
            del os.environ[name]
    isolated = args.output / "user-data"
    isolated.mkdir(exist_ok=True)
    os.environ.update(CODE_AGENT_HOME=str(isolated), HOME=str(isolated), USERPROFILE=str(isolated), CODE_AGENT_PROVIDER="offline",
                      PATH=os.pathsep.join((str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"])))
    try:
        report = asyncio.run(exercise(args))
    except Exception as exc:
        report = {"passed": False, "error": repr(exc), "modelRequests": 0}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
