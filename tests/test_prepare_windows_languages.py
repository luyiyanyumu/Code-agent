from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

spec = spec_from_file_location("prepare_windows_languages", Path(__file__).parents[1] / "scripts/prepare_windows_languages.py")
module = module_from_spec(spec)
spec.loader.exec_module(module)


def test_catalog_merge_rejects_duplicates_and_missing_services():
    fragment = {"schemaVersion": 1, "servers": [{"id": "one"}]}
    with pytest.raises(ValueError, match="Duplicate"):
        module.merge_fragments({"a": fragment, "b": fragment}, {"one"})
    with pytest.raises(ValueError, match="coverage"):
        module.merge_fragments({"a": fragment}, {"one", "two"})


def test_bash_integration_keeps_source_fragment_immutable():
    original = {"schemaVersion": 1, "servers": [{"id": "bash", "args": ["start"]}]}
    merged = module.merge_fragments({"node": original}, {"bash"})
    bash = merged["servers"][0]
    assert bash["env"]["PATH"]["paths"] == ["runtimes/language-servers/shell/bin"]
    assert bash["requiredFiles"][-1].endswith("shfmt.exe")
    assert "env" not in original["servers"][0]


def test_copy_reuses_partial_tree_but_preserves_conflicting_files(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir(); target.mkdir()
    (source / "old.txt").write_text("same")
    (source / "new.txt").write_text("new")
    (target / "old.txt").write_text("same")
    module.copy_owned(source, target, "compare")
    assert (target / "new.txt").read_text() == "new"
    (target / "old.txt").write_text("user")
    with pytest.raises(ValueError, match="Conflicting"):
        module.copy_owned(source, target, "compare")
    assert (target / "old.txt").read_text() == "user"


def test_copy_rejects_unexpected_files_and_nested_destination(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir(); target.mkdir()
    (source / "app.txt").write_text("app")
    (target / "unexpected.txt").write_text("preserve")
    with pytest.raises(ValueError, match="unexpected"):
        module.copy_owned(source, target, "compare")
    with pytest.raises(ValueError, match="overlap"):
        module.copy_owned(source, source / "nested", "compare")


def test_fresh_and_resume_restore_manifest_without_preparing_groups_twice(tmp_path, monkeypatch):
    from code_agent.codeintel import lsp_catalog
    payload = tmp_path / "payload"
    (payload / "runtimes/node").mkdir(parents=True)
    (payload / "runtimes/node/node.exe").write_bytes(b"fixture")
    manifest = payload / "desktop-runtime.json"
    manifest.write_text('{"schemaVersion":1,"version":"fixture"}')
    ids = dict(zip(module.GROUPS, ("pyright", "sql", "lua", "gopls", "php")))
    monkeypatch.setattr(lsp_catalog, "catalog", lambda: [SimpleNamespace(id=sid) for sid in ids.values()])
    calls = []
    def prepared(name, args):
        calls.append(name)
        source = tmp_path / name
        for owner in module.GROUPS[name][0]:
            (source / owner).mkdir(parents=True)
            (source / owner / "runtime.txt").write_text(owner)
        module.write(source / module.GROUPS[name][1], {"schemaVersion": 1, "servers": [
            {"id": ids[name], "command": "runtimes/node/node.exe", "args": []}]})
        return source
    monkeypatch.setattr(module, "prepare_group", prepared)
    monkeypatch.setattr(module, "validate", lambda *args: {"javascriptValidated": True})
    args = SimpleNamespace(payload=payload, cache=tmp_path / "cache", node=None, python=Path(sys.executable),
                           group_root=[], resume=False, merge_existing=False, existing_policy="compare")
    assert module.prepare(args)["servers"] == 5
    assert calls == list(module.GROUPS)
    # copy-assets resets this file on each resumed payload build.
    manifest.write_text('{"schemaVersion":1,"version":"fixture"}')
    args.resume = True
    module.prepare(args)
    assert calls == list(module.GROUPS)
    assert json.loads(manifest.read_text())["languageTools"] == module.REGISTRY
