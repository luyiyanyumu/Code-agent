"""Real SDK-backed LSP semantic checks with isolated projects and no models."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from code_agent.codeintel.lsp import client_capabilities
from code_agent.codeintel.uri import normalize_uri
from code_agent.extensions.jsonrpc_stdio import JsonRpcStdio


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def emit(stage, **details):
    print(json.dumps({"stage": stage, **details}), flush=True)


async def wait(check, label, timeout=100):
    end, error = time.monotonic() + timeout, None
    while time.monotonic() < end:
        try:
            value = await check() if asyncio.iscoroutinefunction(check) else check()
            if value:
                return value
        except Exception as failure:
            error = failure
        await asyncio.sleep(.25)
    raise AssertionError(label + ": " + str(error or "timeout"))


def base_env():
    result = {key: value for key, value in os.environ.items() if not re.match(
        r"^(?:CODE_AGENT_|CH_AGENT_|GO(?:PATH|ROOT|ENV|FLAGS|TOOLCHAIN|MODCACHE|CACHE|PROXY|SUMDB)|CARGO|RUST|DOTNET|MSBUILD|NUGET|OPENAI_|DEEPSEEK_|PYTHONPATH|VIRTUAL_ENV|NODE_PATH)", key, re.I)}
    result["PATH"] = os.pathsep.join([str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"]])
    result.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return result


def position(path, text, token, offset=1):
    index = text.index(token) + offset
    return {"textDocument": {"uri": path.as_uri()}, "position": {
        "line": text[:index].count("\n"), "character": len(text[:index].rsplit("\n", 1)[-1])}}


async def probe(name, command, argv, env, project, path, source, token, expected, report):
    uri = normalize_uri(path.as_uri())
    diagnostics = {}
    server_messages = []
    client = JsonRpcStdio(str(command), list(map(str, argv)), framing="content-length", cwd=str(project), env=env, name=name)
    def notification(message):
        if message.get("method") == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            diagnostics[normalize_uri(params.get("uri") or "")] = params.get("diagnostics") or []
        elif message.get("method") in ("window/logMessage", "window/showMessage"):
            server_messages.append(message.get("params"))
            del server_messages[:-40]
    client.on_notification = notification
    def request(message):
        if message.get("method") == "workspace/configuration":
            return [{} for _ in (message.get("params") or {}).get("items", [])]
        if message.get("method") == "workspace/workspaceFolders":
            return [{"uri": project.as_uri(), "name": project.name}]
        if message.get("method") == "workspace/applyEdit":
            return {"applied": False, "failureReason": "Read-only semantic verification"}
        return None
    client.on_request = request
    before = sha(path)
    started = time.monotonic()
    try:
        await client.start()
        emit("process-started", language=name)
        initialized = await client.request("initialize", {"processId": os.getpid(),
            "rootUri": project.as_uri(), "workspaceFolders": [{"uri": project.as_uri(), "name": project.name}],
            "capabilities": client_capabilities(), "initializationOptions": (
                {"checkOnSave": True, "cargo": {"buildScripts": {"enable": False}},
                 "diagnostics": {"experimental": {"enable": True}}} if name == "rust" else {})}, timeout=90)
        emit("initialized", language=name)
        await client.notify("initialized", {})
        await client.notify("textDocument/didOpen", {"textDocument": {
            "uri": path.as_uri(), "languageId": {"gopls": "go", "rust": "rust", "csharp": "csharp"}[name],
            "version": 1, "text": source}})
        async def hover():
            result = await client.request("textDocument/hover", position(path, source, token), timeout=20)
            return result if expected.lower() in json.dumps(result).lower() else None
        hover_result = await wait(hover, name + " standard library hover")
        emit("stdlib-hover", language=name)
        await wait(lambda: uri in diagnostics and any(item.get("severity") == 1 for item in diagnostics[uri]), name + " type error diagnostic")
        emit("type-diagnostic", language=name)
        fixed = source.replace('"bad"', "7")
        await client.notify("textDocument/didChange", {"textDocument": {"uri": path.as_uri(), "version": 2}, "contentChanges": [{"text": fixed}]})
        # rust-analyzer delegates compiler diagnostics to cargo check. An IDE
        # save writes the document before didSave; keep that real contract in
        # this disposable fixture instead of expecting cargo to read RAM.
        if name == "rust":
            path.write_text(fixed, encoding="utf-8", newline="")
        await client.notify("textDocument/didSave", {"textDocument": {"uri": path.as_uri()}})
        await wait(lambda: uri in diagnostics and not any(item.get("severity") == 1 for item in diagnostics[uri]), name + " repaired diagnostics", timeout=100)
        assert sha(path) == (hashlib.sha256(fixed.encode("utf-8")).hexdigest() if name == "rust" else before)
        report["checks"].append({"language": name, "stdlibHover": expected,
            "typeDiagnosticAndRepair": True, "sourceUnchanged": name != "rust", "savedIsolatedFixture": name == "rust",
            "elapsedSeconds": round(time.monotonic() - started, 2), "command": str(command),
            "hover": hover_result, "capabilities": {key: bool(initialized.get("capabilities", {}).get(key)) for key in ("hoverProvider", "completionProvider", "definitionProvider")}})
        emit("passed", language=name)
    except Exception:
        report["failedServer"] = {"language": name, "diagnostics": diagnostics,
                                  "messages": server_messages, "stderr": client.stderr_tail(40)}
        raise
    finally:
        await client.stop()
        assert not client.alive


async def run(args, report):
    roots = args.root / "runtimes/language-servers"
    names = [args.only] if args.only else ["gopls", "rust", "csharp"]
    for name in names:
        project = args.output / (name + " project 中文")
        project.mkdir()
        home = args.output / (name + "-data")
        home.mkdir()
        env = base_env()
        if name == "gopls":
            sdk = roots / "go"
            env.update(GOROOT=str(sdk), GOCACHE=str(home / "cache"), GOPATH=str(home / "path"), GOMODCACHE=str(home / "modules"), GOTOOLCHAIN="local", CGO_ENABLED="0", GOTELEMETRY="off", GOMAXPROCS="4")
            env["PATH"] = str(sdk / "bin") + os.pathsep + env["PATH"]
            (project / "go.mod").write_text("module sdkfixture\n\ngo 1.27.0\n")
            source = 'package main\nimport "fmt"\nfunc main() {\n fmt.Println("ok")\n var value int = "bad"\n _ = value\n}\n'
            path = project / "main.go"
            command, argv, token, expected = roots / "gopls/gopls.exe", [], "Println", "Println"
        elif name == "rust":
            sdk = roots / "rust"
            env.update(RUST_SRC_PATH=str(sdk / "lib/rustlib/src/rust/library"), RUSTC=str(sdk / "bin/rustc.exe"), CARGO=str(sdk / "bin/cargo.exe"), CARGO_HOME=str(home / "cargo"), CARGO_TARGET_DIR=str(home / "target"))
            env["PATH"] = str(sdk / "bin") + os.pathsep + env["PATH"]
            (project / "Cargo.toml").write_text('[package]\nname="sdkfixture"\nversion="0.1.0"\nedition="2024"\n')
            (project / "src").mkdir()
            source = 'fn main() {\n let mut values: Vec<String> = Vec::new();\n values.push(String::from("ok"));\n let value: usize = "bad";\n println!("{} {}", values.len(), value);\n}\n'
            path = project / "src/main.rs"
            command, argv, token, expected = sdk / "bin/rust-analyzer.exe", [], "Vec<String>", "Vec"
        else:
            sdk = args.dotnet_root or roots / "dotnet"
            csharp = args.csharp_root or roots / "csharp"
            env.update(DOTNET_ROOT=str(sdk), DOTNET_MULTILEVEL_LOOKUP="0", DOTNET_CLI_HOME=str(home / "cli"), NUGET_PACKAGES=str(home / "packages"), DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1", DOTNET_NOLOGO="1", DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE="true")
            env["PATH"] = str(sdk) + os.pathsep + env["PATH"]
            (project / "Example.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>')
            (project / "NuGet.Config").write_text('<configuration><packageSources><clear /></packageSources></configuration>')
            restore = subprocess.run([str(sdk / "dotnet.exe"), "restore", "--configfile", str(project / "NuGet.Config"), "--ignore-failed-sources"], cwd=project, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
            (args.output / "csharp-offline-restore.log").write_text(restore.stdout + restore.stderr, encoding="utf-8")
            assert restore.returncode == 0, "Offline .NET reference-pack restore failed"
            source = 'using System;\nclass Sample {\n string Echo(string input) { return input.Trim(); }\n int Count() { return "bad"; }\n}\n'
            path = project / "Sample.cs"
            command, argv, token, expected = sdk / "dotnet.exe", [csharp / "CSharpLanguageServer.dll", "--loglevel", "trace", "--rpclog", args.output / "csharp-rpc.log"], "Trim()", "Trim"
        path.write_text(source, encoding="utf-8")
        emit("start", language=name)
        await probe(name, command, argv, env, project, path, source, token, expected, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", choices=["gopls", "rust", "csharp"])
    parser.add_argument("--dotnet-root", type=Path)
    parser.add_argument("--csharp-root", type=Path)
    args = parser.parse_args()
    args.root, args.output = args.root.resolve(), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    for name in ("dotnet_root", "csharp_root"):
        if getattr(args, name):
            setattr(args, name, getattr(args, name).resolve())
    # JsonRpcStdio intentionally merges its extra environment with its host.
    # Sanitize this disposable verifier process too, so removed system-SDK
    # variables cannot reappear during that merge.
    sanitized = base_env()
    os.environ.clear()
    os.environ.update(sanitized)
    report = {"passed": False, "checks": [], "modelRequests": 0}
    try:
        asyncio.run(run(args, report))
        report["passed"] = True
    except Exception:
        import traceback
        report["error"] = traceback.format_exc()
    finally:
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
