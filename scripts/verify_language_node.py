"""Real, isolated Node/Python LSP acceptance. No model or global tool calls."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time


def emit(**value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def exercise(args, report):
    from code_agent.codeintel.bundled_lsp import _launch
    from code_agent.codeintel.lsp import client_capabilities, DEFAULT_SETTINGS, _deep_merge
    from code_agent.extensions.jsonrpc_stdio import JsonRpcStdio
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    cases = {
        "pyright": ("python", "demo.py", 'value: str = 42\nvalue.upper()\n', "hover", "value.upper", 1),
        "pylsp": ("python", "demo.py", 'def greet(name: str) -> str:\n    return name.upper()\n\ngreet("hi")\n', "hover", 'greet("hi")', 2),
        "ruff": ("python", "demo.py", 'import os\nvalue = 1\n', "diagnostics", "", 0),
        "typescript": ("typescript", "demo.ts", 'const value: string = 42;\nvalue.toUpperCase();\n', "hover", "value.toUpperCase", 1),
        "vue": ("vue", "App.vue", '<script setup lang="ts">\nconst greeting: string = 42;\n</script>\n<template>{{ greeting.toUpperCase() }}</template>\n', "hover", "greeting: string", 1),
        "json": ("json", "demo.json", '{"name": }\n', "diagnostics", "", 0),
        "css": ("css", "demo.css", '.box { colour: red; }\n', "diagnostics", "", 0),
        "html": ("html", "demo.html", '<div><h1>hello</h1></div>\n', "hover", "div>", 1),
        "eslint": ("javascript", "demo.js", 'var unused = 1;\n', "diagnostics", "", 0),
        "yaml": ("yaml", "demo.yaml", 'root:\n  [broken:\n', "diagnostics", "", 0),
        "bash": ("shellscript", "demo.sh", '#!/usr/bin/env bash\ngreet() { echo hello; }\ngreet\n', "symbols", "", 0),
        "dockerfile": ("dockerfile", "Dockerfile", 'FROM alpine\nRUN echo hello\n', "hover", "FROM", 1),
        "sql": ("sql", "demo.sql", 'SELECT FROM ;\n', "diagnostics", "", 0),
    }
    chosen = [row for row in registry["servers"] if row["id"] in cases and (not args.only or row["id"] in args.only)]
    for raw in chosen:
        sid = raw["id"]
        emit(stage="starting", server=sid)
        definition = _launch(args.app_root, raw)
        project = args.output / (sid + " project 中文")
        project.mkdir(parents=True, exist_ok=False)
        lang, filename, text, feature, needle, offset = cases[sid]
        path = project / filename
        path.write_text(text, encoding="utf-8")
        if sid in {"typescript", "vue"}:
            (project / "tsconfig.json").write_text('{"compilerOptions":{"strict":true,"target":"ES2022","module":"ESNext","moduleResolution":"Bundler"},"include":["**/*.ts","**/*.vue"]}', encoding="utf-8")
        if sid == "eslint":
            (project / "package.json").write_text('{"type":"module"}', encoding="utf-8")
            (project / "eslint.config.js").write_text('export default [{ rules: { "no-unused-vars": "error" } }];\n', encoding="utf-8")
        settings = _deep_merge(DEFAULT_SETTINGS, definition["initialization_options"].get("settings", {}))
        if sid == "eslint":
            settings["eslint"].update({"workingDirectory": {"directory": str(project), "changeProcessCWD": True}, "useFlatConfig": True,
                                       "codeAction": {"disableRuleComment": {"enable": True}, "showDocumentation": {"enable": True}},
                                       "codeActionOnSave": {"enable": False, "mode": "all"}, "format": False, "quiet": False,
                                       "packageManager": "npm", "experimental": {"useFlatConfig": False},
                                       "workspaceFolder": {"uri": project.as_uri(), "name": project.name}, "options": {},
                                       "problems": {"shortenToSingleLine": False}, "rulesCustomizations": []})
        diagnostics = []
        messages = []
        def notification(message):
            messages.append(message)
            if message.get("method") == "textDocument/publishDiagnostics":
                diagnostics[:] = (message.get("params") or {}).get("diagnostics") or []
        def server_request(message):
            method = message.get("method")
            if method == "workspace/configuration":
                result = []
                for item in (message.get("params") or {}).get("items") or []:
                    value = settings["eslint"] if sid == "eslint" and not item.get("section") else settings
                    section = item.get("section") or ""
                    for key in section.split(".") if section else []:
                        value = value.get(key, {}) if isinstance(value, dict) else {}
                    result.append(value)
                return result
            if method == "workspace/workspaceFolders":
                return [{"uri": project.as_uri(), "name": project.name}]
            return None
        client = JsonRpcStdio(definition["command"], definition["args"], framing="content-length", cwd=str(project),
                              env=definition["env"], name=sid, on_notification=notification, on_request=server_request)
        started = time.monotonic()
        try:
            await client.start()
            options = dict(definition["initialization_options"])
            options["settings"] = settings
            initialized = await client.request("initialize", {"processId": os.getpid(), "rootUri": project.as_uri(),
                "workspaceFolders": [{"uri": project.as_uri(), "name": project.name}], "capabilities": client_capabilities(),
                "initializationOptions": options}, timeout=40)
            await client.notify("initialized", {})
            await client.notify("workspace/didChangeConfiguration", {"settings": settings})
            await client.notify("textDocument/didOpen", {"textDocument": {"uri": path.as_uri(), "languageId": lang, "version": 1, "text": text}})
            result = None
            can_pull = bool((initialized.get("capabilities") or {}).get("diagnosticProvider"))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if feature == "diagnostics":
                    if diagnostics:
                        result = diagnostics
                        break
                    if can_pull:
                        try:
                            pulled = await client.request("textDocument/diagnostic", {"textDocument": {"uri": path.as_uri()}}, timeout=10)
                            if pulled.get("items"):
                                result = pulled["items"]
                                break
                        except Exception as error:
                            if "-32601" not in str(error):
                                raise
                            can_pull = False
                elif feature == "symbols":
                    result = await client.request("textDocument/documentSymbol", {"textDocument": {"uri": path.as_uri()}}, timeout=10)
                    if result:
                        break
                else:
                    index = text.index(needle) + offset
                    before = text[:index]
                    params = {"textDocument": {"uri": path.as_uri()}, "position": {"line": before.count("\n"), "character": len(before.rsplit("\n", 1)[-1])}}
                    result = await client.request("textDocument/hover", params, timeout=10)
                    if result and result.get("contents"):
                        break
                await asyncio.sleep(.2)
            assert result, f"{sid} initialized but produced no real {feature}: " + client.stderr_tail(limit=4)
            if sid == "vue":
                assert "string" in json.dumps(result), result
            if sid == "typescript":
                assert "string" in json.dumps(result), result
            row = {"id": sid, "passed": True, "feature": feature, "elapsedSeconds": round(time.monotonic() - started, 2),
                   "diagnostics": len(result) if feature == "diagnostics" else len(diagnostics),
                   "capabilities": list((initialized.get("capabilities") or {}).keys())}
            report["servers"].append(row)
            emit(stage="passed", **row)
        except Exception as error:
            report["servers"].append({"id": sid, "passed": False, "error": str(error), "stderr": client.stderr_tail(limit=6),
                                      "messages": [message for message in messages if message.get("method") in {"window/logMessage", "window/showMessage"}][-5:]})
            emit(stage="failed", server=sid, error=str(error))
        finally:
            try:
                if client.alive:
                    await client.request("shutdown", None, timeout=3)
                    await client.notify("exit", None)
            except Exception:
                pass
            await client.stop()
    report["passed"] = bool(chosen) and all(row["passed"] for row in report["servers"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    args.app_root, args.registry, args.output = args.app_root.resolve(), args.registry.resolve(), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.app_root))
    for name in list(os.environ):
        if name.startswith(("DEEPSEEK_", "OPENAI_", "CODE_AGENT_", "CH_AGENT_", "PYTHONPATH", "NODE_PATH", "NODE_OPTIONS")):
            del os.environ[name]
    isolated_home = args.output / "user-data"
    isolated_home.mkdir(exist_ok=True)
    os.environ.update(CODE_AGENT_HOME=str(isolated_home), HOME=str(isolated_home), USERPROFILE=str(isolated_home),
                      CODE_AGENT_PROVIDER="offline", PYTHONDONTWRITEBYTECODE="1",
                      PATH=os.pathsep.join((str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"])))
    report = {"passed": False, "appRoot": str(args.app_root), "servers": [], "modelRequests": 0}
    try:
        asyncio.run(exercise(args, report))
    finally:
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
