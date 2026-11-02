"""Packaged LSP catalogs stay relocatable and never install or mutate settings."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from code_agent.codeintel import bundled_lsp, lsp_catalog
from code_agent.codeintel.lsp import LspManager


def write(path: Path, value="fixture only; never execute") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def record(**extra):
    return {"id": "typescript", "version": "fixture-1", "command": "runtimes/node/node.exe",
            "args": [{"path": "runtimes/language-servers/node/server.js"}, "--stdio"],
            "requiredFiles": ["runtimes/language-servers/node/package.json"],
            "requiredDirectories": ["runtimes/language-servers/node/tsdk"],
            "env": {"PATH": {"paths": ["runtimes/language-servers/sdk/bin"], "inherit": True},
                    "GOROOT": {"path": "runtimes/language-servers/sdk"},
                    "GOCACHE": {"dataPath": "go/cache"}, "FIXTURE_LITERAL": "preserved"},
            "initializationOptions": {"typescript": {"tsdk": {"path": "runtimes/language-servers/node/tsdk"}},
                                      "flags": {"enabled": True, "mode": "default"}}, **extra}


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / "应用 with spaces & 中文"
    home = tmp_path / "data 空格"
    write(root / "desktop-runtime.json", json.dumps({"schemaVersion": 1, "languageTools": bundled_lsp.REGISTRY_PATH}))
    write(root / "runtimes/node/node.exe")
    write(root / "runtimes/language-servers/node/server.js")
    write(root / "runtimes/language-servers/node/package.json", "{}")
    write(root / "runtimes/language-servers/node/tsdk/typescript.js")
    write(root / "runtimes/language-servers/sdk/bin/tool.exe")
    registry = {"schemaVersion": 1, "servers": [record()]}
    write(root / bundled_lsp.REGISTRY_PATH, json.dumps(registry))
    monkeypatch.setattr(bundled_lsp, "application_root", lambda: root)
    monkeypatch.setenv("CODE_AGENT_HOME", str(home))
    monkeypatch.setenv("PATH", "fixture-host-path")
    monkeypatch.setattr(lsp_catalog.shutil, "which", lambda name: None)
    lsp_catalog.clear_resolve_cache()
    yield root, home, registry
    lsp_catalog.clear_resolve_cache()


def update(bundle, registry):
    write(bundle[0] / bundled_lsp.REGISTRY_PATH, json.dumps(registry))


def server(config=None, sid="typescript"):
    return next(entry for entry in lsp_catalog.merged_servers(config) if entry["id"] == sid)


def test_complete_bundle_resolves_arguments_sdks_cache_and_nested_options_without_writes(bundle, monkeypatch):
    root, home, _ = bundle
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}
    original_env = os.environ.copy()
    def forbidden(*args, **kwargs):
        raise AssertionError("Language discovery must not launch or install anything")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    entry = server()
    assert entry["command"] == str(root / "runtimes/node/node.exe")
    assert entry["args"] == [str(root / "runtimes/language-servers/node/server.js"), "--stdio"]
    assert entry["initialization_options"]["typescript"]["tsdk"] == str(root / "runtimes/language-servers/node/tsdk")
    assert entry["env"]["GOROOT"] == str(root / "runtimes/language-servers/sdk")
    assert entry["env"]["GOCACHE"] == str(home / "language-servers/go/cache")
    assert entry["env"]["PATH"] == str(root / "runtimes/language-servers/sdk/bin") + os.pathsep + "fixture-host-path"
    detected = next(row for row in lsp_catalog.detect(languages={"typescript": 1}) if row["id"] == "typescript")
    assert detected["installed"] and detected["source"] == "bundled" and detected["version"] == "fixture-1"
    assert detected["resolved_command"] == entry["command"]
    assert "env" not in detected and "initialization_options" not in detected
    assert os.environ == original_env and not home.exists()
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}


def test_installed_app_can_be_relocated_without_saved_absolute_paths(bundle, tmp_path, monkeypatch):
    root, _, _ = bundle
    destination = tmp_path / "moved installation 新位置"
    shutil.copytree(root, destination)
    monkeypatch.setattr(bundled_lsp, "application_root", lambda: destination)
    entry = server()
    assert entry["command"] == str(destination / "runtimes/node/node.exe")
    assert entry["args"][0].startswith(str(destination))
    assert str(root) not in json.dumps(entry, ensure_ascii=False)


def test_explicit_user_launcher_is_not_combined_with_bundle_options_or_environment(bundle):
    configured = {"typescript": {"command": "my-server", "args": ["--custom"], "env": {"TOKEN": "fixture-private"},
                                 "initializationOptions": {"custom": True}}}
    before = copy.deepcopy(configured)
    entry = server(configured)
    assert entry["command"] == "my-server" and entry["args"] == ["--custom"]
    assert entry["env"] == {"TOKEN": "fixture-private"}
    assert entry["initialization_options"] == {"custom": True}
    assert "source" not in entry and configured == before


def test_user_arguments_environment_and_nested_options_override_only_chosen_values(bundle):
    root, _, _ = bundle
    configured = {"typescript": {"args": [], "env": {"PATH": "user-only", "GOROOT": "user-go"},
                                 "initializationOptions": {"flags": {"mode": "user"}}}}
    before = copy.deepcopy(configured)
    entry = server(configured)
    assert entry["command"] == str(root / "runtimes/node/node.exe") and entry["args"] == []
    assert entry["env"]["PATH"] == "user-only" and entry["env"]["GOROOT"] == "user-go"
    assert entry["env"]["FIXTURE_LITERAL"] == "preserved"
    assert entry["initialization_options"]["flags"] == {"enabled": True, "mode": "user"}
    assert "typescript" in entry["initialization_options"]
    assert configured == before


@pytest.mark.parametrize("relative", [
    "runtimes/node/node.exe", "runtimes/language-servers/node/server.js",
    "runtimes/language-servers/node/package.json", "runtimes/language-servers/node/tsdk/typescript.js",
])
def test_missing_runtime_or_declared_dependency_is_not_advertised(bundle, relative):
    root, _, _ = bundle
    if relative.endswith("typescript.js"):
        shutil.rmtree(root / "runtimes/language-servers/node/tsdk")
    else:
        (root / relative).unlink()
    assert bundled_lsp.launches() == {}
    entry = server()
    assert entry["command"] == "typescript-language-server" and "source" not in entry
    assert not next(row for row in lsp_catalog.detect() if row["id"] == "typescript")["installed"]


@pytest.mark.parametrize("field,value", [
    ("command", "../outside.exe"), ("command", "C:/outside.exe"), ("command", "code_agent/server.py"),
    ("args", [{"path": "runtimes/../../outside.js"}]),
    ("args", [{"path": "C:\\outside.js"}]),
    ("env", {"GOROOT": {"path": "/outside"}}),
    ("env", {"GOCACHE": {"dataPath": "../../outside"}}),
    ("env", {"PATH": {"paths": ["runtimes/node"], "inherit": "true"}}),
    ("env", {"BAD=ENV": "1"}),
    ("initializationOptions", {"sdk": {"path": "runtimes/../../outside"}}),
    ("requiredFiles", ["runtimes/missing"]),
])
def test_invalid_automatic_paths_and_environment_cannot_escape_the_bundle(bundle, field, value):
    _, _, registry = bundle
    registry["servers"][0][field] = value
    update(bundle, registry)
    assert bundled_lsp.launches() == {} and server()["command"] == "typescript-language-server"


def test_one_incomplete_server_does_not_hide_other_complete_languages_and_duplicates_fail_closed(bundle):
    _, _, registry = bundle
    registry["servers"].append(record(id="yaml", command="runtimes/not-installed.exe"))
    update(bundle, registry)
    assert list(bundled_lsp.launches()) == ["typescript"]
    registry["servers"].append(record())
    update(bundle, registry)
    assert bundled_lsp.launches() == {}


@pytest.mark.parametrize("value", ["{broken", "[]", '{"schemaVersion":2,"servers":[]}',
                                   '{"schemaVersion":1,"servers":{}}', " " * (1024 * 1024 + 1)],
                         ids=["broken-json", "not-object", "schema", "servers-not-list", "oversized"])
def test_invalid_or_oversized_registry_keeps_the_normal_catalog(bundle, value):
    write(bundle[0] / bundled_lsp.REGISTRY_PATH, value)
    assert bundled_lsp.launches() == {} and server()["command"] == "typescript-language-server"


def test_runtime_manifest_must_explicitly_name_the_known_registry(bundle):
    root, _, _ = bundle
    write(root / "desktop-runtime.json", json.dumps({"schemaVersion": 1, "languageTools": "../outside.json"}))
    assert bundled_lsp.launches() == {}


def test_source_detects_removal_and_refresh_without_retaining_a_stale_bundle(bundle):
    root, _, _ = bundle
    assert server().get("source") == "bundled"
    (root / "runtimes/language-servers/node/server.js").unlink()
    assert "source" not in server()
    write(root / "runtimes/language-servers/node/server.js")
    assert server().get("source") == "bundled"


def test_legacy_data_prefix_is_resolved_within_the_same_user_cache(bundle):
    _, home, registry = bundle
    registry["servers"][0]["env"]["GOCACHE"] = {"dataPath": "language-servers/go/cache"}
    update(bundle, registry)
    assert server()["env"]["GOCACHE"] == str(home / "language-servers/go/cache")


def test_typed_prefix_and_classpath_wildcard_keep_validated_paths_inside_literal_argv(bundle):
    root, home, registry = bundle
    registry["servers"][0]["args"] = ["-cp", {"path": "runtimes/language-servers/node", "suffix": "/*"},
        {"dataPath": "lua/log", "prefix": "--logpath="}]
    registry["servers"][0]["initializationOptions"] = {
        "sdk": {"path": "runtimes/language-servers/node/tsdk", "prefix": "flag="}}
    update(bundle, registry)
    entry = server()
    assert entry["args"] == ["-cp", str(root / "runtimes/language-servers/node") + "/*",
                             "--logpath=" + str(home / "language-servers/lua/log")]
    assert entry["initialization_options"]["sdk"] == "flag=" + str(root / "runtimes/language-servers/node/tsdk")
    assert not home.exists()


def test_lsp_options_named_path_can_contain_an_inner_typed_path(bundle):
    root, _, registry = bundle
    registry["servers"][0]["initializationOptions"] = {
        "tsserver": {"path": {"path": "runtimes/language-servers/node/server.js"}}}
    update(bundle, registry)
    assert server()["initialization_options"]["tsserver"]["path"] == str(root / "runtimes/language-servers/node/server.js")


def test_json_automatic_selection_uses_its_dedicated_server_when_all_tools_are_bundled(bundle, tmp_path):
    _, _, registry = bundle
    registry["servers"].append(record(id="json", name="JSON"))
    update(bundle, registry)
    project = tmp_path / "json project"
    project.mkdir()
    manager = LspManager(config_path=tmp_path / "no-lsp-config.json")
    definitions = manager.servers_for_language("json", {"id": "p", "root_path": str(project)})
    assert [entry["id"] for entry in definitions] == ["json"]
    assert definitions[0]["source"] == "bundled"


@pytest.mark.parametrize("decoration", [{"prefix": "x\n"}, {"suffix": "\\..\\outside"},
                                         {"suffix": ":evil"}, {"prefix": "x" * 513}, {"suffix": 12}],
                         ids=["newline", "parent", "drive", "oversized", "not-string"])
def test_typed_decorations_reject_controls_overflow_and_path_traversal(bundle, decoration):
    _, _, registry = bundle
    registry["servers"][0]["args"] = [{"path": "runtimes/language-servers/node", **decoration}]
    update(bundle, registry)
    assert bundled_lsp.launches() == {}


def test_automatic_server_passes_its_sdk_and_cache_environment_to_its_own_client(bundle, tmp_path, monkeypatch):
    import code_agent.codeintel.lsp as lsp_module
    _, _, registry = bundle
    registry["servers"][0]["id"] = "sql"
    update(bundle, registry)
    captured = {}
    class Client:
        alive = True
        def __init__(self, command, args, **kwargs):
            captured.update(command=command, args=args, **kwargs)
        def set_on_closed(self, callback):
            self.closed = callback
        async def start(self):
            pass
        async def request(self, method, params, **kwargs):
            assert method == "initialize"
            return {"capabilities": {"hoverProvider": True}}
        async def notify(self, method, params):
            pass
        async def stop(self):
            self.alive = False
    monkeypatch.setattr(lsp_module, "JsonRpcStdio", Client)
    project = tmp_path / "project"
    project.mkdir()
    original_env = os.environ.copy()
    async def run():
        manager = LspManager(config_path=tmp_path / "no-user-config.json")
        session = await manager._start({"id": "p", "root_path": str(project)}, "sql", "main", "p:main:sql:sql", server(sid="sql"))
        try:
            assert captured["env"]["GOROOT"] == server(sid="sql")["env"]["GOROOT"]
            assert captured["env"]["GOCACHE"] == server(sid="sql")["env"]["GOCACHE"]
            assert session.environment == {"source": "bundled", "version": "fixture-1"}
            assert os.environ == original_env
        finally:
            await session.client.stop()
            await manager.stop_all()
    asyncio.run(run())
