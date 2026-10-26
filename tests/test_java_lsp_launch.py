"""Offline Java analysis launch fixtures. No Java, Maven, LSP process, or network."""

from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from pathlib import Path

import pytest

from code_agent.codeintel import lsp_catalog
from code_agent.codeintel.java_lsp_launch import MAX_POM_BYTES, jdtls_process_command, prepare_jdtls_launch
from code_agent.codeintel.lsp import LspManager


def pom(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<project xmlns="http://maven.apache.org/POM/4.0.0">{content}</project>', encoding="utf-8")
    return path


def dependency(version: str = "") -> str:
    return ("<dependencies><dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId>"
            f"<version>{version}</version></dependency></dependencies>")


def jar(path: Path, *, agent: str = "lombok.launch.Agent") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\r\nPremain-Class: {agent}\r\n\r\n")
    return path


def cached_jar(repo: Path, version: str) -> Path:
    return jar(repo / "org/projectlombok/lombok" / version / f"lombok-{version}.jar")


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    # Isolate environment agent discovery from developer-machine settings.
    monkeypatch.delenv("JAVA_TOOL_OPTIONS", raising=False)
    monkeypatch.delenv("JDK_JAVA_OPTIONS", raising=False)
    root = tmp_path / "project with spaces"
    root.mkdir()
    repository = tmp_path / "Maven cache with spaces"
    project = {"id": "p_fixture", "root_path": str(root)}
    entry = {"id": "jdtls", "args": [], "env": {}, "maven_repository": str(repository)}
    data_root = tmp_path / "agent state"

    def prepare(*, project=project, tree="main", entry=entry, command="jdtls.bat"):
        return prepare_jdtls_launch(project, tree, entry, command, data_root=data_root)

    return root, repository, project, entry, prepare


def test_cached_maven_parent_matches_project_version_with_spaces(fixture):
    root, repository, project, entry, prepare = fixture
    pom(root / "backend/pom.xml", """
      <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>
      <version>3.5.fixture</version><relativePath/></parent>
    """ + dependency())
    pom(repository / "org/springframework/boot/spring-boot-starter-parent/3.5.fixture/spring-boot-starter-parent-3.5.fixture.pom", """
      <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-dependencies</artifactId>
      <version>3.5.fixture</version></parent>
    """)
    pom(repository / "org/springframework/boot/spring-boot-dependencies/3.5.fixture/spring-boot-dependencies-3.5.fixture.pom", """
      <properties><lombok.version>1.18.46</lombok.version></properties>
      <dependencyManagement>""" + dependency("${lombok.version}") + "</dependencyManagement>")
    matched = cached_jar(repository, "1.18.46")
    cached_jar(repository, "99.0.0")  # A newer unrelated cache must not win.
    original = json.dumps(entry)
    before = matched.stat().st_mtime_ns
    args, metadata = prepare()
    assert args.count(f"--jvm-arg=-javaagent:{matched.resolve()}") == 1
    assert metadata["lombok"]["enabled"] is True
    assert metadata["lombok"]["version"] == "1.18.46"
    assert metadata["lombok"]["source"] == "maven-cache"
    assert json.dumps(entry) == original and matched.stat().st_mtime_ns == before
    assert not Path(metadata["workspace_data"]["path"]).exists(), "Preparation must not create cache directories."


def test_child_property_overrides_inherited_managed_lombok(fixture):
    root, repository, _, _, prepare = fixture
    pom(root / "pom.xml", "<groupId>local</groupId><artifactId>parent</artifactId><version>1</version>"
        "<properties><lombok.version>1.18.32</lombok.version></properties>"
        "<dependencyManagement>" + dependency("${lombok.version}") + "</dependencyManagement>")
    pom(root / "backend/pom.xml", "<parent><groupId>local</groupId><artifactId>parent</artifactId><version>1</version></parent>"
        "<properties><lombok.version>1.18.46</lombok.version></properties>" + dependency())
    matched = cached_jar(repository, "1.18.46")
    args, metadata = prepare()
    assert f"--jvm-arg=-javaagent:{matched.resolve()}" in args
    assert metadata["lombok"]["version"] == "1.18.46"


def test_imported_cached_bom_and_annotation_processor_paths(fixture):
    root, repository, _, _, prepare = fixture
    pom(root / "pom.xml", """
      <dependencyManagement><dependencies><dependency><groupId>example</groupId><artifactId>bom</artifactId>
      <version>1</version><type>pom</type><scope>import</scope></dependency></dependencies></dependencyManagement>
      <build><plugins><plugin><configuration><annotationProcessorPaths><path>
      <groupId>org.projectlombok</groupId><artifactId>lombok</artifactId>
      </path></annotationProcessorPaths></configuration></plugin></plugins></build>
    """)
    pom(repository / "example/bom/1/bom-1.pom", "<properties><version.lombok>1.18.46</version.lombok></properties>"
        "<dependencyManagement>" + dependency("${version.lombok}") + "</dependencyManagement>")
    cached_jar(repository, "1.18.46")
    assert prepare()[1]["lombok"]["enabled"]


def test_unrelated_relative_parent_does_not_override_matching_cached_parent(fixture):
    root, repository, _, _, prepare = fixture
    pom(root / "pom.xml", "<groupId>other</groupId><artifactId>parent</artifactId><version>1</version>"
        "<properties><lombok.version>99.0.0</lombok.version></properties>"
        "<dependencyManagement>" + dependency("${lombok.version}") + "</dependencyManagement>")
    pom(root / "backend/pom.xml", "<parent><groupId>example</groupId><artifactId>parent</artifactId><version>1</version></parent>"
        + dependency())
    pom(repository / "example/parent/1/parent-1.pom", "<groupId>example</groupId><artifactId>parent</artifactId><version>1</version>"
        "<properties><lombok.version>1.18.46</lombok.version></properties>"
        "<dependencyManagement>" + dependency("${lombok.version}") + "</dependencyManagement>")
    cached_jar(repository, "99.0.0")
    matched = cached_jar(repository, "1.18.46")
    assert f"--jvm-arg=-javaagent:{matched.resolve()}" in prepare()[0]


def test_missing_matched_jar_preserves_java_launch_without_using_other_version(fixture):
    root, repository, _, entry, prepare = fixture
    pom(root / "pom.xml", dependency("1.18.46"))
    cached_jar(repository, "1.18.32")
    entry["args"] = ["-Xms1G"]
    args, metadata = prepare()
    assert args[0] == "-Xms1G" and not any("javaagent:" in value for value in args)
    assert metadata["lombok"]["status"] == "missing"
    assert metadata["lombok"]["version"] == "1.18.46"
    assert "未缓存" in metadata["lombok"]["reason"]


def test_missing_parent_does_not_guess_cached_lombok_version(fixture):
    root, repository, _, _, prepare = fixture
    pom(root / "pom.xml", "<parent><groupId>uncached</groupId><artifactId>parent</artifactId><version>1</version>"
        "<relativePath/></parent>" + dependency())
    cached_jar(repository, "1.18.46")
    args, metadata = prepare()
    assert not any("javaagent:" in value for value in args)
    assert metadata["lombok"]["status"] == "unresolved"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_entities_are_rejected_without_reading_or_exposing_payload(fixture, encoding, caplog):
    root, _, _, _, prepare = fixture
    secret = "fixture-secret-not-for-logs"
    xml = '<!DOCTYPE project [<!ENTITY stolen SYSTEM "file:///private-api-key">]><project>'
    xml += "<properties><lombok.version>&stolen;</lombok.version><api.key>" + secret + "</api.key></properties>"
    xml += dependency("${lombok.version}") + "</project>"
    (root / "pom.xml").write_bytes(xml.encode(encoding))
    args, metadata = prepare()
    assert not any("javaagent:" in value for value in args)
    assert metadata["lombok"]["status"] == "unresolved"
    assert secret not in json.dumps(metadata) + caplog.text
    assert "private-api-key" not in json.dumps(metadata) + caplog.text


def test_oversized_and_malformed_poms_fail_closed_without_blocking_java(fixture):
    root, _, _, _, prepare = fixture
    (root / "pom.xml").write_bytes(b" " * (MAX_POM_BYTES + 1))
    assert prepare()[1]["lombok"]["status"] == "unresolved"
    (root / "pom.xml").write_text("<project><api.key>secret-private</project>", encoding="utf-8")
    args, metadata = prepare()
    assert metadata["lombok"]["status"] == "unresolved"
    assert "secret-private" not in json.dumps(metadata)
    assert not any("javaagent:" in arg for arg in args)


def test_parent_cycle_and_ignored_build_directories_are_bounded(fixture):
    root, repository, _, _, prepare = fixture
    pom(root / "pom.xml", "<parent><groupId>local</groupId><artifactId>self</artifactId><version>1</version>"
        "<relativePath>pom.xml</relativePath></parent>")
    pom(root / "node_modules/plugin/pom.xml", dependency("1.18.46"))
    pom(root / "target/unrelated/pom.xml", dependency("1.18.46"))
    cached_jar(repository, "1.18.46")
    args, metadata = prepare()
    assert metadata["lombok"]["status"] == "not-required"
    assert not any("javaagent:" in arg for arg in args)


def test_conflicting_module_versions_require_explicit_selection(fixture):
    root, repository, _, _, prepare = fixture
    pom(root / "a/pom.xml", dependency("1.18.32"))
    pom(root / "b/pom.xml", dependency("1.18.46"))
    cached_jar(repository, "1.18.32")
    cached_jar(repository, "1.18.46")
    args, metadata = prepare()
    assert metadata["lombok"]["status"] == "unresolved"
    assert "不同" in metadata["lombok"]["reason"]
    assert not any("javaagent:" in value for value in args)


def test_explicit_relative_jar_works_without_maven_and_keeps_space_as_one_arg(fixture):
    root, _, _, entry, prepare = fixture
    target = jar(root / "tools with spaces/compiler-agent.jar")
    entry["lombok_jar"] = "tools with spaces/compiler-agent.jar"
    args, metadata = prepare()
    assert [arg for arg in args if "javaagent:" in arg] == [f"--jvm-arg=-javaagent:{target.resolve()}"]
    assert metadata["lombok"]["source"] == "configured" and metadata["lombok"]["enabled"]


@pytest.mark.parametrize("present", [False, True])
def test_bad_explicit_jar_never_falls_back_to_unrelated_cache(fixture, present):
    root, repository, _, entry, prepare = fixture
    pom(root / "pom.xml", dependency("1.18.46"))
    cached_jar(repository, "1.18.46")
    entry["lombok_jar"] = "tools/custom.jar"
    if present:
        jar(root / entry["lombok_jar"], agent="other.Agent")
    args, metadata = prepare()
    assert metadata["lombok"]["status"] == "missing" and metadata["lombok"]["source"] == "configured"
    assert not any("javaagent:" in arg for arg in args)


@pytest.mark.parametrize("form", ["--jvm-arg=-javaagent:", "-javaagent:"])
def test_existing_agent_with_custom_filename_is_not_duplicated(fixture, form):
    root, _, _, entry, prepare = fixture
    existing = jar(root / "tools with spaces/compiler-agent.jar")
    entry["args"] = [form + str(existing), "--jvm-arg=-Xmx2G"]
    entry["lombok_jar"] = "missing-should-not-be-inspected.jar"
    args, metadata = prepare()
    assert args[:2] == entry["args"]
    assert len([arg for arg in args if "javaagent:" in arg]) == 1
    assert metadata["lombok"]["status"] == "configured"


def test_existing_environment_lombok_agent_is_kept_and_env_never_leaks(fixture):
    _, _, _, entry, prepare = fixture
    entry["env"] = {"JAVA_TOOL_OPTIONS": '-javaagent:"C:/Lombok support/lombok.jar" -Dprivate.key=do-not-print',
                    "API_TOKEN": "never-print-this-token"}
    args, metadata = prepare()
    assert metadata["lombok"]["status"] == "configured"
    assert not any("javaagent:" in arg for arg in args)
    assert "do-not-print" not in json.dumps(metadata) and "never-print" not in json.dumps(metadata)


def test_environment_existing_agent_can_have_non_lombok_filename(fixture):
    root, _, _, entry, prepare = fixture
    existing = jar(root / "tools with spaces/agent.jar")
    entry["env"] = {"JDK_JAVA_OPTIONS": f'-Dunrelated=value -javaagent:"{existing}"'}
    args, metadata = prepare()
    assert metadata["lombok"]["status"] == "configured"
    assert not any("javaagent:" in arg for arg in args)


def test_auto_lombok_can_be_disabled(fixture):
    root, repository, _, entry, prepare = fixture
    pom(root / "pom.xml", dependency("1.18.46"))
    cached_jar(repository, "1.18.46")
    entry["lombok_enabled"] = False
    args, metadata = prepare()
    assert metadata["lombok"]["status"] == "disabled"
    assert not any("javaagent:" in arg for arg in args)


def test_direct_java_launcher_agent_precedes_jar(fixture):
    root, _, _, entry, prepare = fixture
    target = jar(root / "lombok.jar")
    entry.update(args=["-Xmx2G", "-jar", "launcher.jar", "-configuration", "config"], lombok_jar=str(target))
    args, metadata = prepare(command="java.exe")
    assert args.index(f"-javaagent:{target}") < args.index("-jar")
    assert args[args.index("-jar") + 1] == "launcher.jar"
    assert metadata["lombok"]["enabled"]


def test_unknown_launcher_is_not_given_incompatible_jvm_flags(fixture):
    root, _, _, entry, prepare = fixture
    entry["lombok_jar"] = str(jar(root / "lombok.jar"))
    args, metadata = prepare(command="custom-language-launcher.exe")
    assert metadata["lombok"]["status"] == "unsupported-launcher"
    assert not any("javaagent:" in arg for arg in args)


def test_workspace_data_is_stable_unique_for_project_tree_and_root(fixture):
    root, _, project, entry, prepare = fixture
    entry["args"] = ["-data", str(root / "explicit cache root"), "--jvm-arg=-Xmx2G"]
    original = list(entry["args"])
    a, meta = prepare()
    assert a == prepare()[0]
    alternatives = [prepare(tree="task/one")[0], prepare(project={**project, "id": "p_other"})[0],
                    prepare(project={**project, "root_path": str(root / "other")})[0]]
    assert len({a[a.index("-data") + 1], *(args[args.index("-data") + 1] for args in alternatives)}) == 4
    assert Path(meta["workspace_data"]["path"]).is_relative_to(root / "explicit cache root")
    assert meta["workspace_data"]["source"] == "configured-root" and meta["workspace_data"]["isolated"]
    assert entry["args"] == original
    assert a.count("-data") == 1


def test_explicit_workspace_opt_out_preserves_exact_arguments(fixture):
    root, _, _, entry, prepare = fixture
    entry.update(args=["-data=" + str(root / "exact data"), "--jvm-arg=-Xmx2G"], isolate_workspace=False)
    args, metadata = prepare()
    assert args == entry["args"]
    assert metadata["workspace_data"]["isolated"] is False


def test_catalog_retains_java_options_without_mutating_config(fixture):
    _, repository, _, _, _ = fixture
    config = {"jdtls": {"lombokJar": "tools/lombok.jar", "lombokEnabled": False,
                        "mavenRepository": str(repository), "isolateWorkspace": False}}
    original = json.dumps(config)
    java = next(entry for entry in lsp_catalog.merged_servers(config) if entry["id"] == "jdtls")
    assert java["lombok_jar"] == "tools/lombok.jar"
    assert java["maven_repository"] == str(repository)
    assert java["lombok_enabled"] is False and java["isolate_workspace"] is False
    assert json.dumps(config) == original


def test_official_windows_shim_uses_python_argv_without_shell(fixture):
    root, _, _, _, _ = fixture
    launcher = root / "official bin/jdtls.bat"
    launcher.parent.mkdir()
    launcher.write_text('@echo off\npython "%~dp0/jdtls" %*\npause\n', encoding="utf-8")
    script = launcher.parent / "jdtls"
    script.write_text("raise AssertionError('fixture must never execute')", encoding="utf-8")
    (launcher.parent / "jdtls.py").write_text("raise AssertionError('fixture must never execute')", encoding="utf-8")
    args = ["--jvm-arg=-javaagent:" + str(root / "space & caret ^ /lombok.jar"), "-data", str(root / "workspace & cache")]
    command, prepared = jdtls_process_command(str(launcher), args)
    assert command == sys.executable and prepared == [str(script), *args]
    assert args[0] in prepared, "The full space/metacharacter path must remain one argv element."
    assert script.exists()  # No process is launched by preparation.


def test_custom_windows_shim_is_preserved(fixture):
    root, _, _, _, _ = fixture
    launcher = root / "custom bin/jdtls.bat"
    launcher.parent.mkdir()
    launcher.write_text('@echo off\nset CUSTOM_FLAG=keep-me\npython "%~dp0/jdtls" %*\n', encoding="utf-8")
    (launcher.parent / "jdtls").write_text("fixture only", encoding="utf-8")
    (launcher.parent / "jdtls.py").write_text("fixture only", encoding="utf-8")
    args = ["--jvm-arg=-Xmx2G"]
    assert jdtls_process_command(str(launcher), args) == (str(launcher), args)


def test_manager_uses_prepared_args_and_safe_status_without_starting_process(fixture, monkeypatch, caplog):
    root, repository, project, entry, _ = fixture
    pom(root / "pom.xml", dependency("1.18.46"))
    matched = cached_jar(repository, "1.18.46")
    launcher = root / "fake launcher/jdtls.bat"
    launcher.parent.mkdir()
    launcher.write_text("this fixture must never be executed", encoding="utf-8")
    secret = "do-not-log-fixture-secret"
    config = {"jdtls": {"command": str(launcher), "mavenRepository": str(repository), "env": {"API_TOKEN": secret}}}
    java = next(value for value in lsp_catalog.merged_servers(config) if value["id"] == "jdtls")
    captured = []
    debug_bundle = str(root / "debug plugin.jar")
    monkeypatch.setattr("code_agent.codeintel.java_debug.debug_bundles", lambda: [debug_bundle])

    class FakeStdio:
        def __init__(self, command, args, **kwargs):
            captured.append((command, args, kwargs))
            self.alive = True

        def set_on_closed(self, callback):
            pass

        async def start(self):
            pass

        async def request(self, method, params, **kwargs):
            assert method == "initialize" and params["rootUri"].startswith("file:")
            assert params["initializationOptions"]["bundles"] == [debug_bundle]
            return {"capabilities": {"completionProvider": {}}}

        async def notify(self, method, params):
            assert method in {"initialized", "workspace/didChangeConfiguration"}

        def stderr_tail(self, **kwargs):
            return ""

    monkeypatch.setattr("code_agent.codeintel.lsp.JsonRpcStdio", FakeStdio)
    session = asyncio.run(LspManager()._start(project, "java", "main", "fixture:java", java))
    assert f"--jvm-arg=-javaagent:{matched.resolve()}" in captured[0][1] == session.args
    assert session.environment["lombok"]["enabled"]
    assert session.supports("completionProvider")
    assert secret not in json.dumps(session.summary()) + caplog.text
