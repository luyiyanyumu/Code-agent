"""Real stdio LSP checks for portable native tools, with an isolated HOME/PATH."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import subprocess

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from code_agent.extensions.jsonrpc_stdio import JsonRpcStdio
from code_agent.codeintel.lsp import client_capabilities
from code_agent.codeintel.uri import normalize_uri


CASES = {
    "clangd": ("main.cpp", "cpp", '#include <vector>\n#include <stdio.h>\nint main() { std::vector<int> values; return (int)values.size(); }\n',
               '#include <vector>\n#include <stdio.h>\nint main() { int value = "bad"; return value; }\n'),
    "kotlin": ("Main.kt", "kotlin", 'fun twice(input: Int): Int = input * 2\nval value: String = "hello"\n', 'val number: Int = "bad"\n'),
    "lua": ("main.lua", "lua", 'local number = 42\nprint(number)\n', 'local number = \n'),
    "terraform": ("main.tf", "terraform", 'variable "name" {\n  type = string\n  default = "example"\n}\n', 'variable "name" {\n  default =\n}\n'),
    "markdown": ("README.md", "markdown", '# Portable language tools\n\n## Example\n\nText.\n', '# Portable language tools\n\n[Missing](missing-document.md)\n'),
    "zig": ("main.zig", "zig", 'const std = @import("std");\npub fn main() void { const value: i32 = 1; std.debug.print("{d}", .{value}); }\n', 'pub fn main() void { const number: i32 = ; }\n'),
    "dart": ("main.dart", "dart", 'void main() { String message = "hello"; print(message); }\n', 'void main() { int number = "bad"; print(number); }\n'),
    "ruby": ("main.rb", "ruby", 'class Greeter\n  def hello\n    "hello"\n  end\nend\n', 'class Greeter\n  def hello(\nend\n'),
    "toml": ("example.toml", "toml", '[example]\nname = "portable"\nenabled = true\n', '[example\nname = "unfinished\n'),
}


def render(value, tools: Path, java: Path, data: Path):
    if isinstance(value, list):
        return [render(item, tools, java, data) for item in value]
    if isinstance(value, dict):
        if "path" in value:
            name = value["path"]
            target = java / name[len("runtimes/java/"):] if name.startswith("runtimes/java/") else java if name == "runtimes/java" else tools / name
            assert target.exists(), str(target)
            return value.get("prefix", "") + str(target.resolve()) + value.get("suffix", "")
        if "dataPath" in value:
            target = data / value["dataPath"]
            return value.get("prefix", "") + str(target) + value.get("suffix", "")
        if "paths" in value:
            paths = [render({"path": name}, tools, java, data) for name in value["paths"]]
            if value.get("inherit"):
                paths.append(os.environ["PATH"])
            return os.pathsep.join(paths)
        return {key: render(item, tools, java, data) for key, item in value.items()}
    return value


async def wait(predicate, label: str, timeout=35):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            value = await predicate() if asyncio.iscoroutinefunction(predicate) else predicate()
            if value:
                return value
        except Exception:
            pass
        await asyncio.sleep(.2)
    raise TimeoutError(label)


async def check(server: dict, args) -> dict:
    identity = server["id"]
    folder = args.output / identity
    folder.mkdir(parents=True)
    data = folder / "user-data"
    data.mkdir()
    name, language, good, bad = CASES[identity]
    file = folder / name
    file.write_text(good, encoding="utf-8")
    if identity == "zig":
        (folder / "build.zig").write_text('const std = @import("std");\npub fn build(b: *std.Build) void { _ = b; }\n', encoding="utf-8")
    if identity == "kotlin":
        (folder / "settings.gradle.kts").write_text('rootProject.name = "portable-fixture"\n', encoding="utf-8")
    if identity == "dart":
        (folder / "pubspec.yaml").write_text('name: portable_fixture\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n', encoding="utf-8")
    if identity == "markdown":
        # Marksman indexes multi-file workspaces marked by .git or this config.
        (folder / ".marksman.toml").write_text('[core]\ntext_sync = "full"\n', encoding="utf-8")
    command = render({"path": server["command"]}, args.tools, args.java, data)
    launch_args = render(server.get("args", []), args.tools, args.java, data)
    env = render(server.get("env", {}), args.tools, args.java, data)
    env.update({"HOME": str(data), "USERPROFILE": str(data), "APPDATA": str(data / "AppData/Roaming"),
                "LOCALAPPDATA": str(data / "AppData/Local")})
    for path in (data / "AppData/Roaming", data / "AppData/Local"):
        path.mkdir(parents=True)
    initialized_options = render(server.get("initializationOptions", {}), args.tools, args.java, data)
    diagnostics = []
    notifications = []
    client = JsonRpcStdio(command, launch_args, framing="content-length", cwd=str(folder), env=env, name="portable-" + identity)
    def notice(message):
        notifications.append(message)
        if message.get("method") == "textDocument/publishDiagnostics" and normalize_uri(message.get("params", {}).get("uri", "")) == normalize_uri(file.as_uri()):
            diagnostics[:] = message["params"].get("diagnostics") or []
    client.on_notification = notice
    settings = {"solargraph": {"diagnostics": True, "formatting": False}, "Lua": {"diagnostics": {"enable": True}}}
    settings.update(initialized_options.get("settings", {}))
    def request(message):
        if message.get("method") == "workspace/configuration":
            return [settings.get(item.get("section"), {}) for item in message.get("params", {}).get("items", [])]
        return None
    client.on_request = request
    result = {"id": identity, "passed": False}
    print(json.dumps({"stage": "lsp-start", "id": identity}), flush=True)
    started = time.monotonic()
    try:
        await client.start()
        initialized = await client.request("initialize", {"processId": os.getpid(), "rootUri": folder.as_uri(),
            "workspaceFolders": [{"uri": folder.as_uri(), "name": folder.name}], "capabilities": client_capabilities(),
            "initializationOptions": initialized_options}, timeout=60)
        result["initializeMilliseconds"] = round((time.monotonic() - started) * 1000)
        result["serverInfo"] = initialized.get("serverInfo")
        print(json.dumps({"stage": "lsp-ready", "id": identity, "milliseconds": result["initializeMilliseconds"]}), flush=True)
        await client.notify("initialized", {})
        await client.notify("workspace/didChangeConfiguration", {"settings": settings})
        await client.notify("textDocument/didOpen", {"textDocument": {"uri": file.as_uri(), "languageId": language, "version": 1, "text": good}})
        caps = initialized.get("capabilities", {})
        if caps.get("documentSymbolProvider"):
            async def symbols():
                return await client.request("textDocument/documentSymbol", {"textDocument": {"uri": file.as_uri()}}, timeout=15)
            found = await wait(symbols, identity + " document symbols", timeout=45)
            result["documentSymbols"] = len(found)
        diagnostics.clear()
        await client.notify("textDocument/didChange", {"textDocument": {"uri": file.as_uri(), "version": 2}, "contentChanges": [{"text": bad}]})
        def changed_diagnostics():
            found = list(diagnostics)
            if identity == "ruby" and not any("Syntax" in str(row.get("code")) or "unexpected" in row.get("message", "").lower() for row in found):
                return []
            return found
        observed = await wait(changed_diagnostics, identity + " real diagnostics", timeout=50)
        if identity == "clangd":
            assert not any("file not found" in row.get("message", "").lower() or "included file" in row.get("message", "").lower() for row in observed), observed
            assert any("int" in row.get("message", "").lower() and ("char" in row.get("message", "").lower() or "string" in row.get("message", "").lower()) for row in observed), observed
        if identity == "kotlin":
            assert not any("MISSING_BUILT_IN" in str(row.get("code")) for row in observed), observed
        result["diagnostics"] = [{"code": row.get("code"), "message": row.get("message")} for row in observed[:4]]
        result["passed"] = True
        result["sourceUnchanged"] = file.read_text(encoding="utf-8") == good
        assert result["sourceUnchanged"]
    except Exception as exc:
        result["error"] = repr(exc)
        result["stderr"] = client.stderr_tail(limit=15)
        result["lastNotifications"] = notifications[-5:]
        result["logMessages"] = [row.get("params") for row in notifications if row.get("method") == "window/logMessage"]
    finally:
        try:
            await client.request("shutdown", None, timeout=5)
            await client.notify("exit", None)
        except Exception:
            pass
        await client.stop()
        result["elapsedSeconds"] = round(time.monotonic() - started, 2)
    (folder / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


async def main(args):
    manifest = json.loads((args.registry or args.tools / "native-fragment.json").read_text(encoding="utf-8"))
    os.environ["PATH"] = os.pathsep.join([str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"]])
    for key in list(os.environ):
        if key.startswith(("JAVA_", "JDK_", "RUBY", "GEM_", "BUNDLE_", "DART_", "ZIG_")):
            del os.environ[key]
    selected = set(args.servers.split(",")) if args.servers else set(CASES)
    results = []
    for server in manifest["servers"]:
        if server["id"] in selected:
            results.append(await check(server, args))
    if args.shell_tools:
        binary = args.tools / "runtimes/language-servers/shell/bin"
        shellcheck = subprocess.run([str(binary / "shellcheck.exe"), "--format=json", "--shell=bash", "-"],
            input=b'#!/usr/bin/env bash\necho $unquoted\n', capture_output=True, timeout=20)
        rows = json.loads(shellcheck.stdout)
        lint_passed = shellcheck.returncode == 1 and any(row["code"] == 2086 for row in rows)
        results.append({"id": "shellcheck", "passed": lint_passed, "diagnostics": rows})
        shfmt = subprocess.run([str(binary / "shfmt.exe"), "-i", "2"],
            input=b"if true;then echo hello;fi\n", capture_output=True, timeout=20)
        formatted = shfmt.stdout.decode("utf-8").replace("\r\n", "\n")
        format_passed = shfmt.returncode == 0 and formatted == "if true; then echo hello; fi\n"
        results.append({"id": "shfmt", "passed": format_passed, "formatted": formatted})
    passed = bool(results) and all(row["passed"] for row in results)
    report = {"passed": passed, "checks": results, "modelRequests": 0}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--registry", type=Path, help="External native fragment when probing a relocated app payload")
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--servers", default="")
    parser.add_argument("--shell-tools", action="store_true")
    args = parser.parse_args()
    args.tools, args.java, args.output = args.tools.resolve(), args.java.resolve(), args.output.resolve()
    args.output.mkdir(parents=True)
    raise SystemExit(asyncio.run(main(args)))
