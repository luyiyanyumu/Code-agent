"""Exercise the packaged JDT/JDK against disposable projects, without models.

Run with the packaged Python: python -B -X utf8 verify_bundled_java.py
  --app-root <payload> --output <new evidence directory>
The caller's PATH/JAVA_HOME and installed language servers are not prerequisites.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time


def emit(stage, **details):
    print(json.dumps({"stage": stage, **details}, ensure_ascii=False), flush=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sanitized_environment(home):
    for key in list(os.environ):
        if re.match(r"^(?:_?JAVA|JDK|OPENAI|DEEPSEEK|CODE_AGENT|CH_AGENT|PYTHON|VIRTUAL_ENV|NODE_)", key, re.I):
            del os.environ[key]
    system = Path(os.environ["SystemRoot"])
    os.environ.update(CODE_AGENT_HOME=str(home), CODE_AGENT_PROVIDER="offline",
                      CODE_AGENT_AUTH_ENABLED="false", CODE_AGENT_SKIP_MIGRATION="1",
                      PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                      PATH=os.pathsep.join(map(str, (system / "System32", system))))
    assert shutil.which("java") is None and shutil.which("jdtls") is None


def descendants(parent):
    """Read native process identity; never launch or terminate unrelated processes."""
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    rows, entry = [], Entry()
    entry.size = ctypes.sizeof(entry)
    try:
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            rows.append((int(entry.pid), int(entry.parent), entry.name))
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    owned, result = {parent}, []
    for _ in rows:
        added = [(pid, name) for pid, ppid, name in rows if ppid in owned and pid not in owned]
        if not added:
            break
        owned.update(pid for pid, _ in added)
        result.extend(added)
    return result


def process_path(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        size, text = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
        return text.value if kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(size)) else None
    finally:
        kernel.CloseHandle(handle)


async def wait_for(action, label, timeout=60):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            result = action()
            result = await result if hasattr(result, "__await__") else result
            if result:
                return result
        except Exception as error:
            last = error
        await asyncio.sleep(.25)
    raise AssertionError(f"{label} timed out" + (f": {last}" if last else ""))


def project(root, project_name, sources, jars=()):
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "bin").mkdir()
    (root / ".project").write_text(
        '<?xml version="1.0"?><projectDescription><name>' + project_name + '</name><comment/><projects/>'
        '<buildSpec><buildCommand><name>org.eclipse.jdt.core.javabuilder</name><arguments/></buildCommand></buildSpec>'
        '<natures><nature>org.eclipse.jdt.core.javanature</nature></natures></projectDescription>', encoding="utf-8")
    libraries = "".join(f'<classpathentry kind="lib" path="lib/{jar.name}"/>' for jar in jars)
    (root / ".classpath").write_text('<?xml version="1.0"?><classpath><classpathentry kind="src" path="src"/>'
        '<classpathentry kind="con" path="org.eclipse.jdt.launching.JRE_CONTAINER"/>' + libraries +
        '<classpathentry kind="output" path="bin"/></classpath>', encoding="utf-8")
    if jars:
        (root / "lib").mkdir()
        for jar in jars:
            shutil.copyfile(jar, root / "lib" / jar.name)
    for name, text in sources.items():
        (root / "src" / name).write_text(text, encoding="utf-8")
    return {"id": project_name, "root_path": str(root)}


def params(path, text, needle, offset=0):
    index = text.index(needle) + offset
    return {"textDocument": {"uri": path.as_uri()}, "position": {
        "line": text[:index].count("\n"), "character": len(text[:index].rsplit("\n", 1)[-1])}}


async def exercise(args, report):
    from code_agent.codeintel import bundled_java, lsp_catalog
    from code_agent.codeintel.lsp import LspManager
    from code_agent.codeintel.uri import normalize_uri
    imported = Path(sys.modules["code_agent.codeintel.lsp"].__file__).resolve()
    assert imported.is_relative_to(args.app_root), imported
    tools = bundled_java.bundled_tools()
    assert tools and tools["source"] == "bundled"
    assert Path(tools["command"]).is_relative_to(args.app_root / "runtimes/jdtls")
    assert Path(tools["java"]).is_relative_to(args.app_root / "runtimes/java")
    catalog = next(row for row in lsp_catalog.detect(languages={"java": 1}) if row["id"] == "jdtls")
    assert catalog["installed"] and catalog["resolved_command"] == tools["command"]
    report["checks"].append("packaged Python imports packaged code and detects its own JDT/JDK with Java/JDT absent from PATH")
    report["module"] = str(imported)
    manager = LspManager(config_path=args.output / "isolated-home" / "missing-lsp.json")
    assert manager.documentation_translator.provider_factory() is None
    bundle_tree = args.app_root / "runtimes/jdtls"
    bundle_before = {str(path.relative_to(bundle_tree)): digest(path) for path in bundle_tree.rglob("*") if path.is_file()}
    owned, snapshots = [], {}
    def check_configuration(session):
        index = session.args.index("-configuration")
        configuration = Path(session.args[index + 1]).resolve()
        workspace = Path(session.environment["workspace_data"]["path"]).resolve()
        assert configuration == workspace / "configuration", configuration
        assert configuration.is_relative_to(args.output / "isolated-home"), configuration
        assert not configuration.is_relative_to(args.app_root), configuration
        report.setdefault("isolatedConfigurations", []).append(str(configuration))
    start = time.monotonic()
    try:
        if not args.only_lombok:
            source = '''public class Main {
    static int twice(int value) { return value * 2; }
    void use() {
        String text = "hello";
        int result = twice(3);
        text.substring(1);
    }
}
'''
            basic = project(args.output / "Java project 空格", "bundled_java_basic", {"Main.java": source})
            path = Path(basic["root_path"]) / "src/Main.java"
            snapshots[path] = digest(path)
            emit("basic-start")
            session = await manager.session_for(basic, "java", server_id="jdtls")
            assert session.alive and tools["java"] in session.args
            check_configuration(session)
            emit("basic-initialized", milliseconds=session.initialization_ms)
            pid = session.client._proc.pid
            java = [(pid, name) for pid, name in descendants(pid) if name.lower() == "java.exe"]
            assert java and all(Path(process_path(pid)).resolve() == Path(tools["java"]) for pid, _ in java)
            owned.extend(pid for pid, _ in java)
            report["javaPids"] = list(owned)
            report["initializationMilliseconds"] = session.initialization_ms
            await manager.open_document(session, path.as_uri(), source)
            async def completion():
                value = await session.client.request("textDocument/completion", params(path, source, "text.substring", len("text.sub")), timeout=15)
                values = value.get("items", []) if isinstance(value, dict) else value or []
                return values if any("substring" in row.get("label", "") for row in values) else None
            completed = await wait_for(completion, "String.substring completion")
            report["completionItems"] = len(completed)
            report["checks"].append("real JDT completion resolves Java standard-library String.substring")
            at_call = params(path, source, "twice(3)", 2)
            hover = await session.client.request("textDocument/hover", at_call, timeout=20)
            assert "twice" in json.dumps(hover), hover
            definition = await session.client.request("textDocument/definition", at_call, timeout=20)
            assert any((row.get("targetUri") or row.get("uri", "")).endswith("Main.java") for row in definition or []), definition
            renamed = await session.client.request("textDocument/rename", {**at_call, "newName": "doubleValue"}, timeout=20)
            manager._check_workspace_edit(session, renamed)
            assert json.dumps(renamed).count("doubleValue") >= 2, renamed
            assert digest(path) == snapshots[path]
            report["checks"].append("real hover, definition and cross-reference rename suggestions succeed without changing disk files")
            uri = path.as_uri()
            def diagnostics():
                return next((items for key, items in session.diagnostics.items() if normalize_uri(key) == normalize_uri(uri)), None)
            await wait_for(lambda: diagnostics() is not None and not any(row.get("severity") == 1 for row in diagnostics()), "basic clean diagnostics")
            broken = source.replace("int result = twice(3);", 'int result = "wrong";')
            await manager.change_document(session, uri, broken)
            bad = await wait_for(lambda: [row for row in diagnostics() or [] if row.get("severity") == 1], "type-mismatch diagnostics")
            assert any("String" in row.get("message", "") and "int" in row.get("message", "") for row in bad), bad
            await manager.change_document(session, uri, source)
            await wait_for(lambda: diagnostics() is not None and not any(row.get("severity") == 1 for row in diagnostics()), "repaired diagnostics")
            assert digest(path) == snapshots[path]
            report["checks"].append("in-memory String-to-int error publishes diagnostics and restoring the source clears them; disk remains unchanged")
            emit("basic-passed", checks=len(report["checks"]))
            await manager.stop(session.key)

        cache = args.maven_cache
        jars = [cache / "org/projectlombok/lombok/1.18.20/lombok-1.18.20.jar",
                cache / "org/slf4j/slf4j-api/1.7.36/slf4j-api-1.7.36.jar"]
        assert all(jar.is_file() for jar in jars), "Pass a Maven cache with the two documented fixture dependencies"
        lombok_source = '''import lombok.Data;
import lombok.extern.slf4j.Slf4j;
@Data
@Slf4j
public class OldModel {
    private String name;
    public String summary() {
        log.info("name={}", getName());
        return getName();
    }
}
'''
        lombok = project(args.output / "Old Lombok project 空格", "bundled_old_lombok", {"OldModel.java": lombok_source}, jars)
        pom = Path(lombok["root_path"]) / "metadata/pom.xml"
        pom.parent.mkdir()
        pom.write_text('<project><modelVersion>4.0.0</modelVersion><groupId>fixture</groupId><artifactId>old-lombok</artifactId><version>1</version>'
            '<dependencies><dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>1.18.20</version></dependency></dependencies></project>', encoding="utf-8")
        path = Path(lombok["root_path"]) / "src/OldModel.java"
        snapshots.update({path: digest(path), pom: digest(pom)})
        emit("old-lombok-start")
        session = await manager.session_for(lombok, "java", server_id="jdtls")
        assert session.alive and tools["java"] in session.args
        check_configuration(session)
        report["lombokInitializationMilliseconds"] = session.initialization_ms
        state = session.environment["lombok"]
        assert state["enabled"] and state["source"] == "bundled-ide-agent" and state["version"] == "1.18.48"
        assert state["project_versions"] == ["1.18.20"] and state["path"] == tools["lombok"]
        java = [(pid, name) for pid, name in descendants(session.client._proc.pid) if name.lower() == "java.exe"]
        assert java and all(Path(process_path(pid)).resolve() == Path(tools["java"]) for pid, _ in java)
        owned.extend(pid for pid, _ in java)
        report["lombok"] = {key: state[key] for key in ("enabled", "source", "version", "project_versions")}
        await manager.open_document(session, path.as_uri(), lombok_source)
        async def getter_hover():
            result = await session.client.request("textDocument/hover", params(path, lombok_source, "getName()", 3), timeout=15)
            return result if "getName" in json.dumps(result) else None
        await wait_for(getter_hover, "old Lombok generated getter hover")
        async def logger_hover():
            result = await session.client.request("textDocument/hover", params(path, lombok_source, "log.info", 1), timeout=15)
            return result if "Logger" in json.dumps(result) else None
        await wait_for(logger_hover, "old Lombok generated logger hover")
        uri = path.as_uri()
        def diagnostics():
            return next((items for key, items in session.diagnostics.items() if normalize_uri(key) == normalize_uri(uri)), None)
        await wait_for(lambda: diagnostics() is not None and not any(row.get("severity") == 1 for row in diagnostics()), "old Lombok clean diagnostics")
        report["checks"].append("old Lombok 1.18.20 @Data getter and @Slf4j logger resolve using the bundled IDE agent; project dependency remains 1.18.20")
        report["sourceUnchanged"] = all(digest(path) == before for path, before in snapshots.items())
        assert report["sourceUnchanged"]
        report["fixtureSourceSha256"] = {str(path.relative_to(args.output)): before for path, before in snapshots.items()}
        emit("old-lombok-passed")
    finally:
        await manager.stop_all()
        await wait_for(lambda: all(process_path(pid) is None for pid in owned), "owned Java processes stop", timeout=15)
        report["ownedJavaProcessesExited"] = True
        report["javaPids"] = owned
        report["elapsedSeconds"] = round(time.monotonic() - start, 2)
    bundle_after = {str(path.relative_to(bundle_tree)): digest(path) for path in bundle_tree.rglob("*") if path.is_file()}
    report["bundleJavaToolsUnchanged"] = bundle_before == bundle_after
    assert report["bundleJavaToolsUnchanged"], "JDT wrote into its installation tree"
    report["checks"].append("OSGi configuration stays in the isolated user workspace and every bundled JDT file remains unchanged")
    report["checks"].append("normal LSP shutdown closes every captured owned Java process")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only-lombok", action="store_true", help="Reuse prior basic capability proof and retest the Lombok/configuration path")
    parser.add_argument("--maven-cache", type=Path, default=Path.home() / ".m2/repository")
    args = parser.parse_args()
    args.app_root, args.output = args.app_root.resolve(), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if any((args.output / name).exists() for name in ("Java project 空格", "Old Lombok project 空格")):
        raise ValueError("Use a new evidence output directory")
    sanitized_environment(args.output / "isolated-home")
    sys.path.insert(0, str(args.app_root))
    report = {"passed": False, "appRoot": str(args.app_root), "checks": [], "modelRequests": 0}
    try:
        asyncio.run(exercise(args, report))
        report["passed"] = True
    except Exception as error:
        import traceback
        report["error"] = traceback.format_exc()
        emit("failed", kind=type(error).__name__, message=str(error))
    finally:
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
