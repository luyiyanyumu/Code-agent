"""Relocatable Java analysis discovery; no JVM, network or user settings writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest

from code_agent.codeintel import bundled_java, lsp_catalog
from code_agent.codeintel.java_lsp_launch import jdtls_process_command, prepare_jdtls_launch


def write(path: Path, value: str = "fixture only; never execute") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def jdt(directory: Path) -> Path:
    launcher = write(directory / "bin/jdtls.bat", '@echo off\npython "%~dp0/jdtls" %*\npause\n')
    write(directory / "bin/jdtls")
    write(directory / "bin/jdtls.py")
    for platform in ("win", "linux", "mac"):
        write(directory / f"config_{platform}/config.ini")
    write(directory / "plugins/org.eclipse.equinox.launcher_1.8.fixture.jar")
    write(directory / "plugins/org.eclipse.jdt.ls.core_1.61.fixture.jar")
    return launcher if os.name == "nt" else directory / "bin/jdtls"


def java(directory: Path, version="21.0.12.1") -> Path:
    write(directory / "release", f'JAVA_VERSION="{version}"\n')
    return write(directory / "bin/java.exe")


def lombok(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\nPremain-Class: lombok.launch.Agent\r\n")
    return path


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / "安装 app with spaces & symbols"
    root.mkdir()
    java(root / "runtimes/java")
    jdt(root / "runtimes/jdtls")
    lombok(root / "runtimes/lombok/lombok.jar")
    manifest = {"schemaVersion": 1, "java": "runtimes/java/bin/java.exe", "jdtls": "runtimes/jdtls",
                "lombok": "runtimes/lombok/lombok.jar", "runtimeVersions": {"lombok": "1.18.48"}}
    write(root / "desktop-runtime.json", json.dumps(manifest))
    home = tmp_path / "用户 home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODE_AGENT_HOME", str(home / "AppData/data"))
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.delenv("JAVA_TOOL_OPTIONS", raising=False)
    monkeypatch.delenv("JDK_JAVA_OPTIONS", raising=False)
    monkeypatch.delenv("_JAVA_OPTIONS", raising=False)
    monkeypatch.setattr(bundled_java, "application_root", lambda: root)
    monkeypatch.setattr(bundled_java.shutil, "which", lambda name: None)
    lsp_catalog.clear_resolve_cache()
    yield root, manifest, home
    lsp_catalog.clear_resolve_cache()


def java_entry(config=None):
    return next(entry for entry in lsp_catalog.merged_servers(config) if entry["id"] == "jdtls")


def test_complete_relocated_bundle_is_detected_and_prepared_without_spawning_or_writing(bundle, monkeypatch):
    root, _, _ = bundle
    project = root.parent / "项目 with spaces & % 不执行"
    project.mkdir()
    write(project / "pom.xml", "<project><dependencies><dependency><groupId>org.projectlombok</groupId>"
          "<artifactId>lombok</artifactId><version>1.18.20</version></dependency></dependencies></project>")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery and preparation may not launch a JVM or installer")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    entry = java_entry()
    expected_java = str(root / "runtimes/java/bin/java.exe")
    assert entry["args"] == ["--java-executable", expected_java]
    detected = next(item for item in lsp_catalog.detect(languages={"java": 1}, roots=(project,)) if item["id"] == "jdtls")
    assert detected["installed"] and detected["resolved_command"] == entry["command"]
    args, metadata = prepare_jdtls_launch({"id": "fixture", "root_path": str(project)}, "main", entry, entry["command"])
    agent = str(root / "runtimes/lombok/lombok.jar")
    assert f"--jvm-arg=-javaagent:{agent}" in args
    assert args[args.index("--java-executable") + 1] == expected_java
    assert metadata["lombok"]["source"] == "bundled-ide-agent"
    assert metadata["lombok"]["version"] == "1.18.48"
    assert metadata["lombok"]["project_versions"] == ["1.18.20"]
    configuration = metadata["configuration_data"]
    assert configuration["isolated"] and configuration["source"] == "default"
    assert Path(configuration["path"]) == Path(metadata["workspace_data"]["path"]) / "configuration"
    assert args[args.index("-configuration") + 1] == configuration["path"]
    assert not Path(configuration["path"]).is_relative_to(root)
    assert not Path(configuration["path"]).exists()
    assert "保持不变" in metadata["lombok"]["reason"]
    assert "1.18.20" in (project / "pom.xml").read_text()
    command, prepared = jdtls_process_command(entry["command"], args)
    if os.name == "nt":
        assert prepared[0] == str(root / "runtimes/jdtls/bin/jdtls")
        assert prepared[1:] == args
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}
    assert not Path(metadata["workspace_data"]["path"]).exists()


@pytest.mark.parametrize("key,value", [("java", "../outside/java.exe"), ("java", "C:/outside/java.exe"),
                                        ("jdtls", "runtimes/../../outside"), ("schemaVersion", 2)])
def test_manifest_cannot_redirect_automatic_executables(bundle, key, value):
    root, manifest, _ = bundle
    manifest[key] = value
    write(root / "desktop-runtime.json", json.dumps(manifest))
    assert bundled_java.bundled_tools() is None
    assert java_entry()["command"] == "jdtls"


@pytest.mark.parametrize("missing", ["runtimes/java/bin/java.exe", "runtimes/java/release", "runtimes/jdtls/bin/jdtls",
    "runtimes/jdtls/bin/jdtls.py", "runtimes/jdtls/plugins/org.eclipse.equinox.launcher_1.8.fixture.jar",
    "runtimes/jdtls/plugins/org.eclipse.jdt.ls.core_1.61.fixture.jar"])
def test_incomplete_bundle_is_not_advertised_as_installed(bundle, missing):
    root, _, _ = bundle
    (root / missing).unlink()
    assert bundled_java.bundled_tools() is None
    detected = next(row for row in lsp_catalog.detect(languages={"java": 1}) if row["id"] == "jdtls")
    assert detected["installed"] is False


def test_pre_java21_and_missing_platform_config_are_rejected(bundle):
    root, _, _ = bundle
    write(root / "runtimes/java/release", 'JAVA_VERSION="17.0.12"\n')
    assert bundled_java.bundled_tools() is None
    java(root / "runtimes/java")
    config = "win" if os.name == "nt" else "mac" if bundled_java.sys.platform == "darwin" else "linux"
    (root / f"runtimes/jdtls/config_{config}/config.ini").unlink()
    assert bundled_java.bundled_tools() is None


def test_user_command_arguments_and_java_environment_take_precedence(bundle):
    root, _, _ = bundle
    config = {"jdtls": {"command": "custom-jdt.exe", "args": ["--custom"], "env": {"JAVA_HOME": "user-owned-jdk"}}}
    original = json.dumps(config)
    entry = java_entry(config)
    assert entry["command"] == "custom-jdt.exe" and entry["args"] == ["--custom"]
    assert json.dumps(config) == original
    entry = java_entry({"jdtls": {"args": ["--java-executable=user-selected-java", "--custom"]}})
    assert entry["args"] == ["--java-executable=user-selected-java", "--custom"]
    entry = java_entry({"jdtls": {"env": {"JAVA_HOME": "user-owned-jdk", "API_TOKEN": "fixture-private"}}})
    assert entry["args"] == [] and entry["env"]["JAVA_HOME"] == "user-owned-jdk"
    assert Path(entry["command"]).is_relative_to(root)


@pytest.mark.parametrize("legacy", [".code-agent", ".ch-agent", "AppData/data"])
def test_managed_legacy_directory_survives_data_home_change(bundle, monkeypatch, legacy):
    root, _, home = bundle
    (root / "desktop-runtime.json").unlink()
    local_java = java(home / "sdk with spaces")
    monkeypatch.setenv("JAVA_HOME", str(local_java.parent.parent))
    launcher = jdt(home / legacy / "ls/jdtls")
    entry = java_entry()
    assert entry["command"] == str(launcher.resolve())
    assert entry["args"] == ["--java-executable", str(local_java.resolve())]
    assert not (home / "AppData/data/lsp.json").exists()


def test_path_launcher_is_not_replaced_by_legacy_and_incomplete_bundle_can_use_legacy(bundle, monkeypatch):
    root, _, home = bundle
    (root / "runtimes/java/release").unlink()
    java_home = home / "jdk"
    java(java_home)
    monkeypatch.setenv("JAVA_HOME", str(java_home))
    launcher = jdt(home / ".code-agent/ls/jdtls")
    assert java_entry()["command"] == str(launcher.resolve())
    monkeypatch.setattr(bundled_java.shutil, "which", lambda name: "system-jdtls" if name == "jdtls" else None)
    assert java_entry()["command"] == "jdtls"


@pytest.mark.parametrize("option", ["disable", "explicit", "existing"])
def test_bundled_ide_agent_respects_explicit_user_controls(bundle, option):
    root, _, home = bundle
    project = home / "project"
    project.mkdir()
    write(project / "pom.xml", "<project><dependencies><dependency><groupId>org.projectlombok</groupId>"
          "<artifactId>lombok</artifactId><version>1.18.20</version></dependency></dependencies></project>")
    custom = lombok(project / "custom/agent.jar")
    entry = java_entry()
    if option == "disable":
        entry["lombok_enabled"] = False
    elif option == "explicit":
        entry["lombok_jar"] = str(custom)
    else:
        entry["args"].append(f"--jvm-arg=-javaagent:{custom}")
    args, metadata = prepare_jdtls_launch({"id": "fixture", "root_path": str(project)}, "main", entry, entry["command"])
    assert str(root / "runtimes/lombok/lombok.jar") not in "\n".join(args)
    assert metadata["lombok"]["source"] != "bundled-ide-agent"
    if option == "disable":
        assert not any("javaagent:" in arg for arg in args)
    else:
        assert len([arg for arg in args if "javaagent:" in arg]) == 1


def test_bundled_osgi_configuration_is_stable_and_unique_per_project_and_tree(bundle):
    _, _, home = bundle
    entry = java_entry()
    project = {"id": "one", "root_path": str(home / "project")}

    def prepared(project=project, tree="main"):
        return prepare_jdtls_launch(project, tree, entry, entry["command"])
    first, metadata = prepared()
    assert prepared() == (first, metadata)
    paths = {metadata["configuration_data"]["path"]}
    for other, tree in (({**project, "id": "two"}, "main"), (project, "branch/one"),
                        ({**project, "root_path": str(home / "other project")}, "main")):
        paths.add(prepared(other, tree)[1]["configuration_data"]["path"])
    assert len(paths) == 4
    assert all(Path(path).is_relative_to(home / "AppData/data") and not Path(path).exists() for path in paths)


@pytest.mark.parametrize("arguments", [
    ["-configuration", "user config with spaces"], ["-configuration=user config"],
    ["--jvm-arg=-Dosgi.configuration.area=user config"], ["-Dosgi.configuration.area=user config"],
])
def test_bundled_osgi_preserves_explicit_config_arguments(bundle, arguments):
    _, _, home = bundle
    entry = java_entry({"jdtls": {"args": arguments}})
    args, metadata = prepare_jdtls_launch({"id": "one", "root_path": str(home / "project")}, "main", entry, entry["command"])
    assert args[:len(arguments)] == arguments
    assert metadata["configuration_data"]["source"] == "configured"
    assert not metadata["configuration_data"]["isolated"]
    if "-configuration" not in arguments:
        assert "-configuration" not in args


@pytest.mark.parametrize("key", ["JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"])
@pytest.mark.parametrize("host", [False, True])
def test_bundled_osgi_preserves_environment_config_without_exposing_values(bundle, monkeypatch, key, host):
    _, _, home = bundle
    env = {key: '"-Dosgi.configuration.area=C:/user private/cache" -Dprivate.token=fixture-do-not-print'}
    if host:
        monkeypatch.setenv(key, env[key])
    entry = java_entry({"jdtls": {"env": {} if host else env}})
    args, metadata = prepare_jdtls_launch({"id": "one", "root_path": str(home / "project")}, "main", entry, entry["command"])
    assert "-configuration" not in args
    assert metadata["configuration_data"]["source"] == "configured"
    assert "fixture-do-not-print" not in json.dumps(metadata) and "user private" not in json.dumps(metadata)


def test_user_environment_can_clear_an_ambient_osgi_configuration(bundle, monkeypatch):
    _, _, home = bundle
    monkeypatch.setenv("_JAVA_OPTIONS", "-Dosgi.configuration.area=ambient-cache")
    entry = java_entry({"jdtls": {"env": {"_JAVA_OPTIONS": ""}}})
    args, metadata = prepare_jdtls_launch({"id": "one", "root_path": str(home / "project")}, "main", entry, entry["command"])
    assert "-configuration" in args
    assert metadata["configuration_data"]["isolated"] is True


def test_shared_workspace_opt_out_still_has_separate_osgi_config_and_custom_commands_are_unchanged(bundle):
    _, _, home = bundle
    configured_data = str(home / "shared state")
    entry = java_entry({"jdtls": {"args": ["-data", configured_data], "isolateWorkspace": False}})
    project = {"id": "one", "root_path": str(home / "project")}
    args, metadata = prepare_jdtls_launch(project, "main", entry, entry["command"])
    assert args[:2] == ["-data", configured_data]
    assert metadata["workspace_data"]["isolated"] is False
    assert metadata["configuration_data"]["isolated"] is True
    assert Path(metadata["configuration_data"]["path"]).is_relative_to(home / "AppData/data")
    entry = java_entry({"jdtls": {"command": "custom-jdt.exe", "args": ["--custom"]}})
    args, metadata = prepare_jdtls_launch(project, "main", entry, entry["command"])
    assert args[0] == "--custom" and "-configuration" not in args
    assert "configuration_data" not in metadata
