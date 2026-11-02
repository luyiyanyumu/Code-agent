"""The source-merged runtime must work without registry/bootstrap fallback."""

import asyncio
import json
from pathlib import Path
import shutil

import pytest

from code_agent.harness import runtime


@pytest.fixture
def copied_runtime(tmp_path, monkeypatch):
    source = Path(runtime.__file__).parent / "node"
    target = tmp_path / "merged-core"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("node_modules", "tests", "__pycache__"))
    monkeypatch.setenv("CODE_AGENT_HARNESS_NODE_DIR", str(target))
    monkeypatch.setattr(runtime, "_prepared", {})
    return target


def test_prepare_uses_shipped_bundle_without_npm(copied_runtime, monkeypatch):
    calls = []
    original = runtime._command

    async def observe(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime, "_command", observe)
    node, runner = asyncio.run(runtime.prepare_runtime())
    assert Path(runner) == copied_runtime / "runner.mjs"
    assert calls == [(node, str(runner), "--check")]
    assert not (copied_runtime / "node_modules").exists()


def test_changed_core_source_invalidates_verified_cache(copied_runtime):
    asyncio.run(runtime.prepare_runtime())
    manifest = json.loads((copied_runtime / "core-build.json").read_text(encoding="utf8"))
    source = next(name for name in manifest["inputs"] if "/agent-loop/src/" in name and name.endswith(".ts"))
    with (copied_runtime / source).open("a", encoding="utf8") as stream:
        stream.write("\n// Source changed since the shipped build.\n")
    with pytest.raises(runtime.HarnessError, match="build:harness"):
        asyncio.run(runtime.prepare_runtime())


def test_new_source_file_invalidates_verified_cache(copied_runtime):
    asyncio.run(runtime.prepare_runtime())
    (copied_runtime / "src" / "added-local-module.ts").write_text("export const changed = true;\n", encoding="utf8")
    with pytest.raises(runtime.HarnessError, match="build:harness"):
        asyncio.run(runtime.prepare_runtime())


def test_missing_bundle_never_falls_back_to_installed_harness(copied_runtime, monkeypatch):
    (copied_runtime / "core.mjs").unlink()

    async def forbidden(*args, **kwargs):
        pytest.fail("missing merged build must not probe old packages or run npm")

    monkeypatch.setattr(runtime, "_command", forbidden)
    with pytest.raises(runtime.HarnessError, match="构建产物"):
        asyncio.run(runtime.prepare_runtime())


def test_invalid_source_manifest_cannot_escape_runtime(copied_runtime, monkeypatch):
    manifest_path = copied_runtime / "core-build.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    manifest["inputs"]["../outside.txt"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf8")

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid source manifest should be rejected before starting a process")

    monkeypatch.setattr(runtime, "_command", forbidden)
    with pytest.raises(runtime.HarnessError, match="清单"):
        asyncio.run(runtime.prepare_runtime())
