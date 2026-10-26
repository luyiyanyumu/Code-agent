"""JVM runtime discovery and fixture wrappers; never execute Maven/Gradle builds."""
import os
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_agent.api.deps import CurrentUser
from code_agent.api.runtime_api import mount_runtime_api
from code_agent.codeintel import runtime
from code_agent.codeintel.runtime import IdeRuntime, RuntimeUnavailable


POM = '''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<build><plugins><plugin><groupId>org.springframework.boot</groupId>
<artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></project>'''


class _Projects:
    def __init__(self, root):
        self.root = root

    def get(self, project_id):
        return {"id": "p", "root_path": str(self.root), "user_id": "owner"} if project_id == "p" else None


@pytest.fixture
def jvm(tmp_path, monkeypatch):
    root = tmp_path / "fixture java %RUNTIME_JAVA_TEST% & 中文"
    root.mkdir()
    service = IdeRuntime(_Projects(root))
    tools = {"java": "fixture-java", "javac": "fixture-javac", "mvn": "fixture-mvn", "gradle": "fixture-gradle",
             "node": "fixture-node", "npm": "fixture-npm", "cmd": os.environ.get("COMSPEC") or shutil.which("cmd")}
    monkeypatch.setattr(service, "_tools", lambda: tools)
    try:
        yield service, root, tools
    finally:
        service.close()


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _wrapper(root, kind):
    name = ("mvnw.cmd" if os.name == "nt" else "mvnw") if kind == "maven" else ("gradlew.bat" if os.name == "nt" else "gradlew")
    text = "@echo off\necho fixture-jvm %*\nexit /b 0\n" if os.name == "nt" else '#!/bin/sh\nprintf "%s\\n" "fixture-jvm $*"\n'
    path = _write(root, name, text)
    if os.name != "nt":
        path.chmod(0o755)
    return path


def test_jvm_project_configs_precede_plain_files_and_discovery_never_spawns(jvm, monkeypatch):
    service, root, tools = jvm
    for index in range(130):
        _write(root, f"a-source/file{index:03}.js", "export const x = 1")
    _write(root, "a-source/ignored.test.js", "must not be a file runner")
    _write(root, "a-source/ignored.spec.mjs", "must not be a file runner")
    _write(root, "z-backend/pom.xml", POM)
    _write(root, "z-gradle/build.gradle.kts", 'plugins { id("org.springframework.boot") version "4.0.0" }')
    _write(root, "zz-web/package.json", '{"scripts":{"dev":"fixture","test":"fixture"}}')
    _write(root, "test_fixture.py", "def test_fixture(): pass")
    _write(root, "node_modules/hidden/pom.xml", POM)
    calls = []
    monkeypatch.setattr(service, "_tools", lambda: calls.append("tools") or tools)

    def forbidden(*args, **kwargs):
        raise AssertionError("configuration discovery must not launch a process")
    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden)
    result = service.run_configs("p", "owner")
    configs = result["configs"]
    assert calls == ["tools"]
    assert len(configs) == 100 and result["truncated"]
    project = [item for item in configs if item["source"] == "project"]
    assert {item["kind"] for item in project} == {"maven", "gradle", "npm", "pytest"}
    assert all(item["source"] == "project" for item in configs[:len(project)])
    assert {item["script"] for item in project if item["kind"] == "maven"} == {"compile", "test", "spring-boot:run"}
    assert {item["script"] for item in project if item["kind"] == "gradle"} == {"build", "test", "bootRun"}
    assert all("node_modules" not in item.get("manifest", "") for item in configs)
    assert all("ignored" not in item.get("path", "") for item in configs)
    assert service.list("p", "owner", "run") == {"runs": []}


@pytest.mark.parametrize(("content", "expected"), [
    ('plugins { id "org.springframework.boot" version "4" }', {"build", "test", "bootRun"}),
    ('plugins { id("org.springframework.boot") version "4"; application }', {"build", "test", "bootRun", "run"}),
    ('apply plugin: "application"', {"build", "test", "run"}),
    ('apply(plugin = "org.springframework.boot")', {"build", "test", "bootRun"}),
    ('plugins { id("application") }', {"build", "test", "run"}),
    ('plugins { id("org.springframework.boot") version "4" apply false }', {"build", "test"}),
    ('// plugins { id("org.springframework.boot") }\n/* apply plugin: "application" */', {"build", "test"}),
    ('val example = "plugins { id(\'org.springframework.boot\') }"', {"build", "test"}),
    ('dependencies { implementation("org.springframework.boot:spring-boot-starter") }', {"build", "test"}),
])
def test_gradle_only_enables_declared_applied_plugins(tmp_path, content, expected):
    manifest = _write(tmp_path, "build.gradle.kts", content)
    assert set(runtime._gradle_goals(manifest)) == expected


@pytest.mark.parametrize("content", [
    '<project><!-- spring-boot-maven-plugin --><dependencies><dependency><artifactId>spring-boot-maven-plugin</artifactId></dependency></dependencies></project>',
    '<project><build><pluginManagement><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></pluginManagement></build></project>',
    '<project><profiles><profile><build><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></profile></profiles></project>',
    '<!DOCTYPE project [<!ENTITY injected "spring-boot-maven-plugin">]><project>&injected;</project>',
    '<project>malformed',
])
def test_maven_does_not_infer_boot_from_inactive_or_invalid_xml(tmp_path, content):
    assert runtime._maven_goals(_write(tmp_path, "pom.xml", content)) == ["compile", "test"]


def test_jvm_manifest_read_is_bounded(jvm):
    service, root, _ = jvm
    _write(root, "pom.xml", " " * runtime.MAX_MANIFEST_BYTES + POM)
    _write(root, "build.gradle", " " * runtime.MAX_MANIFEST_BYTES + 'plugins { id("application") }')
    configs = service.run_configs("p", "owner")["configs"]
    assert {item["script"] for item in configs if item["kind"] == "maven"} == {"compile", "test"}
    assert {item["script"] for item in configs if item["kind"] == "gradle"} == {"build", "test"}
    assert runtime._manifest_text(root / "pom.xml") is None


def test_jvm_missing_tools_are_explicit_and_do_not_install(jvm):
    service, root, tools = jvm
    _write(root, "pom.xml", POM)
    _write(root, "build.gradle", 'plugins { application }')
    tools["mvn"], tools["gradle"] = None, None
    configs = service.run_configs("p", "owner")["configs"]
    assert configs and all(not item["available"] for item in configs)
    assert all("wrapper" in item["reason"] for item in configs)
    with pytest.raises(RuntimeUnavailable, match="mvn"):
        service._argv(root, root, kind="maven", script="compile")
    tools["mvn"] = "fixture-mvn"
    tools["java"] = None
    assert "JDK" in service.run_configs("p", "owner")["configs"][0]["reason"]


@pytest.mark.parametrize("kind", ["maven", "gradle"])
def test_jvm_nearest_wrapper_and_ancestor_fallback(jvm, kind):
    service, root, tools = jvm
    module = root / "backend"
    module.mkdir()
    root_wrapper = _wrapper(root, kind)
    selected, source = service._jvm_tool(root, module, kind, tools)
    assert selected == str(root_wrapper) and source == "wrapper"
    own_wrapper = _wrapper(module, kind)
    assert service._jvm_tool(root, module, kind, tools) == (str(own_wrapper), "wrapper")
    own_wrapper.unlink()
    root_wrapper.unlink()
    assert service._jvm_tool(root, module, kind, tools) == (tools["mvn" if kind == "maven" else "gradle"], "path")


def test_jvm_wrapper_symlink_cannot_escape_project(jvm, tmp_path):
    service, root, tools = jvm
    outside = _write(tmp_path, "outside-wrapper", "fixture")
    name = "mvnw.cmd" if os.name == "nt" else "mvnw"
    try:
        (root / name).symlink_to(outside)
    except OSError:
        pytest.skip("file symlink unavailable")
    assert service._jvm_tool(root, root, "maven", tools) == (tools["mvn"], "path")


@pytest.mark.parametrize("goal", ["compile && other", "test; other", "--help", "spring-boot:run -Dflag=x", "", "run", "test\nother"])
def test_maven_task_whitelist_rejects_injection_and_unconfigured_run(jvm, goal):
    service, root, _ = jvm
    _write(root, "pom.xml", "<project/>")
    with pytest.raises(ValueError, match="script"):
        service._argv(root, root, kind="maven", script=goal)


def test_jvm_typed_tasks_recheck_manifest_and_reject_java_service_path(jvm):
    service, root, _ = jvm
    _write(root, "pom.xml", POM)
    _write(root, "build.gradle", 'plugins { application }')
    assert service._argv(root, root, kind="maven", script="spring-boot:run")[-1] == "spring-boot:run"
    assert service._argv(root, root, kind="gradle", script="run")[-1] == "run"
    with pytest.raises(ValueError, match="script"):
        service._argv(root, root, kind="gradle", script="bootRun")
    with pytest.raises(ValueError, match="Java"):
        service._argv(root, root, kind="maven", script="test", path="AgentService.java")
    with pytest.raises(ValueError, match="cwd"):
        service._argv(root, root / "missing-module", kind="maven", script="compile")
    (root / "pom.xml").write_text("<project/>", encoding="utf-8")
    with pytest.raises(ValueError, match="script"):
        service._argv(root, root, kind="maven", script="spring-boot:run")


@pytest.mark.parametrize("kind", ["maven", "gradle"])
def test_real_fixture_wrapper_preserves_unicode_special_cwd_and_stream(kind, jvm, monkeypatch):
    service, root, tools = jvm
    if os.name == "nt" and not tools["cmd"]:
        pytest.skip("cmd.exe not installed")
    _wrapper(root, kind)
    module = root / "backend"
    manifest = "backend/pom.xml" if kind == "maven" else "backend/build.gradle"
    _write(root, manifest, POM if kind == "maven" else 'plugins { application }')
    monkeypatch.setenv("RUNTIME_JAVA_TEST", "must-not-expand")
    goal = "compile" if kind == "maven" else "build"
    created = service.create_run("p", "owner", kind=kind, script=goal, cwd="backend")
    record = service._record("p", "owner", created["id"], "run")
    assert record.finished.wait(10)
    result = service.snapshot("p", "owner", created["id"], "run")
    assert result["exit_code"] == 0 and result["status"] == "exited", result
    assert result["cwd"] == "backend"
    text = "".join(event.get("data", "") for event in result["events"])
    assert "fixture-jvm" in text and goal in text
    assert result["events"][-1]["type"] == "exit"
    assert "must-not-expand" not in text
    assert module.is_dir()


def test_jvm_api_schema_owner_validation_and_explicit_fixture_start(tmp_path, monkeypatch):
    root = tmp_path / "api-fixture"
    root.mkdir()
    _write(root, "pom.xml", POM)
    _wrapper(root, "maven")
    app = FastAPI()
    app.state.auth_enabled = False
    app.state.local_user = CurrentUser("owner", "owner", "admin")
    service = mount_runtime_api(app, _Projects(root))
    tools = IdeRuntime._tools()
    tools["java"], tools["javac"] = "fixture-java", "fixture-javac"
    monkeypatch.setattr(service, "_tools", lambda: tools)
    try:
        with TestClient(app) as client:
            base = "/api/v1/code/projects/p"
            assert any(config["kind"] == "maven" for config in client.get(base + "/run-configs").json()["configs"])
            for payload in ({"kind": "maven", "script": "test;bad"}, {"kind": "gradle", "script": "test"},
                            {"kind": "maven", "script": "compile", "cwd": "../"}, {"kind": "java", "path": "AgentService.java"}):
                assert client.post(base + "/runs", json=payload).status_code == 422
            assert service.list("p", "owner", "run")["runs"] == []
            started = client.post(base + "/runs", json={"kind": "maven", "script": "test"})
            assert started.status_code == 201, started.text
            record = service._record("p", "owner", started.json()["id"], "run")
            assert record.finished.wait(10)
            app.state.local_user = CurrentUser("other", "other", "user")
            assert client.get(base + "/run-configs").status_code == 404
            assert client.post(base + "/runs", json={"kind": "maven", "script": "compile"}).status_code == 404
    finally:
        service.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows PATH batch quoting")
def test_windows_path_batch_with_spaces_ampersand_and_unicode_is_exact(jvm, tmp_path):
    service, root, tools = jvm
    _write(root, "pom.xml", "<project/>")
    tool_dir = tmp_path / "tool path & 中文 ' literal"
    tools["mvn"] = str(_wrapper(tool_dir, "maven"))
    created = service.create_run("p", "owner", kind="maven", script="compile")
    record = service._record("p", "owner", created["id"], "run")
    assert record.finished.wait(10)
    result = service.snapshot("p", "owner", created["id"], "run")
    assert result["exit_code"] == 0, result["events"]
    assert "fixture-jvm" in "".join(event.get("data", "") for event in result["events"])


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd percent expansion")
def test_windows_percent_path_tool_is_rejected_before_spawn(jvm, tmp_path, monkeypatch):
    service, root, tools = jvm
    _write(root, "pom.xml", "<project/>")
    tools["mvn"] = str(_wrapper(tmp_path / "%PATH%tools", "maven"))

    def forbidden(*args, **kwargs):
        raise AssertionError("unsafe batch path must not launch")
    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden)
    assert service.run_configs("p", "owner")["configs"][0]["available"] is False
    with pytest.raises(RuntimeUnavailable, match="%"):
        service.create_run("p", "owner", kind="maven", script="compile")


@pytest.mark.skipif(os.name != "nt", reason="Windows job launch ownership")
def test_windows_program_cannot_execute_before_job_assignment(jvm, monkeypatch):
    import time
    service, root, _ = jvm
    marker = root / "started.marker"
    _write(root, "launch.py", "import pathlib,time\npathlib.Path('started.marker').write_text('started')\nprint('ready',flush=True)\ntime.sleep(60)\n")
    original = runtime._WindowsJob

    def delayed_job(pid):
        time.sleep(0.15)  # Make the former child-escape race deterministic.
        assert not marker.exists(), "program ran before entering its owned process tree"
        return original(pid)

    monkeypatch.setattr(runtime, "_WindowsJob", delayed_job)
    created = service.create_run("p", "owner", kind="python", path="launch.py")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "ready" in "".join(event.get("data", "") for event in service.snapshot("p", "owner", created["id"], "run")["events"]):
            break
        time.sleep(0.01)
    assert marker.read_text() == "started"
    result = service.stop("p", "owner", created["id"], "run")
    assert result["status"] == "stopped"
