"""Automatic functionality descriptions share the journal used by all views."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path

import pytest

from code_agent.codeintel.service import CodeIntelService
from code_agent.models.events import ModelTurn


class Projects:
    def get(self, pid):
        return None

    def set_scan(self, pid, summary):
        pass


class Provider:
    name = "fake"
    model = "fake-function-model"

    def __init__(self):
        self.calls = []

    async def chat(self, messages, tools):
        assert tools == []
        entries = re.findall(r"(?m)^(\d+)\. (.*?)（", messages[-1]["content"])
        self.calls.append(entries)
        return ModelTurn(text=json.dumps({"briefs": [{"n": int(n), "text": f"功能：{name}"}
                                                     for n, name in entries]}, ensure_ascii=False))


@pytest.fixture
def env(tmp_path, agent_home):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Counter.java").write_text(
        "package demo;\nclass Counter {\n int base = 10;\n int add(int count) { return base + count; }\n}\n",
        encoding="utf-8",
    )
    project = {"id": "automatic-functions", "root_path": str(root), "name": "demo", "root_key": str(root)}
    intel = CodeIntelService(Projects())
    asyncio.run(intel.scan(project))
    yield intel, project, root
    intel.close()


async def drain(intel, project, tree="main"):
    for _ in range(500):
        if not intel.brief_fill_status(project, tree)["running"]:
            return intel.brief_fill_status(project, tree)
        await asyncio.sleep(0.01)
    raise AssertionError(intel.brief_fill_status(project, tree))


def test_existing_index_automatically_covers_all_java_declarations_and_all_views(env):
    intel, project, _ = env
    provider = Provider()
    intel.set_brief_provider(lambda: provider)

    async def scenario():
        started = intel.maybe_autofill_briefs(project)
        assert started["started"] and started["state"] == "queued"
        return await drain(intel, project)

    status = asyncio.run(scenario())
    assert status["state"] == "complete" and status["missing"] == 0
    assert status["coverage_total"] == status["described"] == status["written"] == 7
    assert status["revision_epoch"] == intel.brief_fill_status(project)["revision_epoch"]
    assert 0 < status["revision"] < 2**53
    rows = intel.index(project["id"]).all_symbols()
    assert {row["kind"] for row in rows} == {"class", "field", "method", "parameter"}
    for row in rows:
        shared = intel.symbol_business(project, row["id"])
        assert shared["business"]["business_meaning"] == f"功能：{row['name']}"
        assert shared["business"]["status"] == "unreviewed"
        assert shared["business"]["evidence"]
        assert intel.symbol_purpose(project, row["id"])["business"] == shared["business"]
        entity = shared["entity"]
        hover = intel.resolve_symbol_business(project, path=entity["path"], name=entity["name"],
                                               line=entity["line"], column=entity["column"])
        assert hover["business"] == shared["business"]
    nav = intel.features(project)
    feature = next(node for domain in nav["domains"] for node in domain["features"] if node["name"] == "Counter")
    assert feature["business"] == intel.symbol_business(project, feature["id"])["business"]
    prompt = provider.calls[0]
    assert any(name == "count" for _, name in prompt)


def test_scan_and_save_refresh_incrementally_reuse_unchanged_entities(env):
    intel, project, root = env
    provider = Provider()
    intel.set_brief_provider(lambda: provider)

    async def scenario():
        intel.maybe_autofill_briefs(project)
        await drain(intel, project)
        original = dict(intel.briefs(project))
        before = len(provider.calls)
        for _ in range(30):
            assert not intel.maybe_autofill_briefs(project)["started"]
        assert len(provider.calls) == before
        await intel.scan(project)
        await drain(intel, project)
        assert len(provider.calls) == before
        (root / "src" / "Extra.java").write_text("class Extra { int get() { return 2; } }\n", encoding="utf-8")
        await intel.scan(project)
        await drain(intel, project)
        assert len(provider.calls) > before
        latest = intel.briefs(project)
        for sid, item in original.items():
            if sid.startswith("sym:"):
                assert latest[sid]["id"] == item["id"]
        source = root / "src" / "Extra.java"
        text = source.read_text(encoding="utf-8").replace("return 2", "return 3")
        await intel.save_file(project, "src/Extra.java", text)
        for _ in range(500):
            if not intel.refresh_status(project["id"])["pending"]:
                break
            await asyncio.sleep(0.01)
        await drain(intel, project)
        sid = next(row["id"] for row in intel.index(project["id"]).all_symbols() if row["name"] == "get")
        assert intel.symbol_business(project, sid)["business"]["status"] == "unreviewed"
        assert intel.brief_fill_status(project)["state"] == "complete"

    asyncio.run(scenario())


def test_module_hash_uses_readme_and_config_inventory_without_repeated_model_calls(env):
    intel, project, root = env
    (root / "README.md").write_text("# Counter\nCounts inputs.\n", encoding="utf-8")
    (root / "src" / "settings.custom").write_text('{"limit": 10}\n', encoding="utf-8")
    (root / "src" / "settings.json").write_text('{"limit": 10}\n', encoding="utf-8")
    asyncio.run(intel.scan(project))
    provider = Provider()
    intel.set_brief_provider(lambda: provider)

    async def scenario():
        intel.maybe_autofill_briefs(project)
        await drain(intel, project)
        before = len(provider.calls)
        for target in ("mod:", "mod:src"):
            assert intel.symbol_business(project, target)["business"]["status"] == "unreviewed"
        assert not intel.maybe_autofill_briefs(project)["started"]
        assert len(provider.calls) == before

    asyncio.run(scenario())


def test_missing_provider_is_truthful_and_has_cooldown_between_status_polls(env, monkeypatch):
    from code_agent.codeintel import service as module

    intel, project, _ = env
    clock = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    factory_calls = []

    def unavailable():
        factory_calls.append(True)
        return None

    intel.set_brief_provider(unavailable)

    async def scenario():
        intel.maybe_autofill_briefs(project)
        status = await drain(intel, project)
        assert status["state"] == "waiting" and status["written"] == 0
        assert status["missing"] == 7 and status["coverage_known"]
        assert "没有可用的模型" in status["note"]
        for _ in range(100):
            assert not intel.maybe_autofill_briefs(project)["started"]
        assert len(factory_calls) == 1
        clock[0] += module.BRIEF_RETRY_COOLDOWN + 1
        assert intel.maybe_autofill_briefs(project)["started"]
        await drain(intel, project)
        assert len(factory_calls) == 2

    asyncio.run(scenario())


def test_collection_and_model_writes_do_not_block_event_loop(env, monkeypatch):
    intel, project, _ = env
    entered, released = threading.Event(), threading.Event()
    original = intel._collect_targets

    def slow_collection(*args, **kwargs):
        entered.set()
        assert released.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(intel, "_collect_targets", slow_collection)
    intel.set_brief_provider(lambda: Provider())

    async def scenario():
        started = intel.start_brief_fill(project, auto=True)
        assert started["state"] == "queued"
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.3)
        assert intel.brief_fill_status(project)["running"]
        assert not intel.maybe_autofill_briefs(project)["started"]
        released.set()
        assert (await drain(intel, project))["state"] == "complete"

    try:
        asyncio.run(scenario())
    finally:
        released.set()


def test_delayed_model_cannot_overwrite_manual_function_and_revision_changes(env):
    intel, project, _ = env
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider(Provider):
        async def chat(self, messages, tools):
            entered.set()
            await release.wait()
            return await super().chat(messages, tools)

    intel.set_brief_provider(lambda: SlowProvider())

    async def scenario():
        first_revision = intel.brief_fill_status(project)["revision"]
        intel.maybe_autofill_briefs(project)
        await asyncio.wait_for(entered.wait(), 3)
        row = next(row for row in intel.index(project["id"]).all_symbols() if row["kind"] == "class")
        saved = intel.write_symbol_business(project, row["id"], {"business_meaning": "人工确认的计数功能"})
        assert intel.brief_fill_status(project)["revision"] > first_revision
        release.set()
        status = await drain(intel, project)
        assert status["state"] == "complete" and status["written"] == 6 and status["done"] == 7
        assert intel.symbol_business(project, row["id"])["business"] == saved["business"]
        revision = status["revision"]
        assert intel.feature_brief_forget(project, symbol_id=row["id"])
        assert intel.brief_fill_status(project)["revision"] > revision

    asyncio.run(scenario())


def test_late_result_is_discarded_after_source_changes(env):
    intel, project, root = env
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowProvider(Provider):
        async def chat(self, messages, tools):
            entered.set()
            await release.wait()
            return await super().chat(messages, tools)

    intel.set_brief_provider(lambda: SlowProvider())

    async def scenario():
        intel.maybe_autofill_briefs(project)
        await asyncio.wait_for(entered.wait(), 3)
        source = root / "src" / "Counter.java"
        source.write_text(source.read_text(encoding="utf-8").replace("return base + count", "return count"), encoding="utf-8")
        release.set()
        status = await drain(intel, project)
        assert status["failed"] > 0
        assert "源码已变化" in status["error"]
        rows = intel.index(project["id"]).all_symbols()
        for row in rows:
            if row["kind"] in {"class", "method", "parameter"}:
                assert row["id"] not in intel.briefs(project)
        intel.set_brief_provider(lambda: Provider())
        await intel.scan(project)
        assert (await drain(intel, project))["state"] == "complete"

    asyncio.run(scenario())


def test_worktree_automatic_records_and_progress_are_isolated(env, tmp_path):
    intel, project, root = env
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "Counter.java").write_text(
        (root / "src" / "Counter.java").read_text(encoding="utf-8").replace("base = 10", "base = 50"), encoding="utf-8")
    task = {**project, "root_path": str(worktree)}
    intel.set_brief_provider(lambda: Provider())

    async def scenario():
        intel.maybe_autofill_briefs(project)
        main = await drain(intel, project)
        await intel.scan(task, tree_id="task:isolated")
        status = await drain(intel, task, "task:isolated")
        assert status["state"] == "complete" and status["tree"] == "task:isolated"
        assert intel.brief_fill_status(project)["written"] == main["written"]
        sid = next(row["id"] for row in intel.index(project["id"]).all_symbols() if row["name"] == "base")
        main_item = intel.briefs(project)[sid]
        task_item = intel.briefs(task, "task:isolated")[sid]
        assert main_item["body_hash"] != task_item["body_hash"]
        assert intel.symbol_business(project, sid)["business"]["status"] == "unreviewed"
        assert intel.symbol_business(task, sid, tree_id="task:isolated")["business"]["status"] == "unreviewed"

    asyncio.run(scenario())


def test_no_index_never_claims_descriptions_are_complete(tmp_path, agent_home):
    intel = CodeIntelService(Projects())
    project = {"id": "no-index", "root_path": str(tmp_path), "name": "empty"}
    intel.set_brief_provider(lambda: Provider())

    async def scenario():
        intel.maybe_autofill_briefs(project)
        status = await drain(intel, project)
        assert status["state"] == "waiting" and not status["coverage_known"]
        assert status["coverage_total"] == status["missing"] == 0

    try:
        asyncio.run(scenario())
    finally:
        intel.close()


def test_successful_retry_does_not_leave_a_failure_status(env):
    intel, project, _ = env

    class RetryProvider(Provider):
        attempts = 0

        async def chat(self, messages, tools):
            self.attempts += 1
            if self.attempts == 1:
                return ModelTurn(text="invalid JSON")
            return await super().chat(messages, tools)

    intel.set_brief_provider(lambda: RetryProvider())

    async def scenario():
        intel.maybe_autofill_briefs(project)
        status = await drain(intel, project)
        assert status["state"] == "complete"
        assert status["missing"] == status["failed"] == 0
        assert status["error"] == "" and "重试后" in status["note"]

    asyncio.run(scenario())


@pytest.mark.parametrize("tree", ["main", "task:isolated"])
def test_closed_project_or_tree_discards_late_provider_result(env, monkeypatch, tree):
    intel, project, _ = env
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class StubbornProvider(Provider):
        async def chat(self, messages, tools):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return await super().chat(messages, tools)

    worker = intel._brief_fill_worker

    async def observed_worker(*args, **kwargs):
        try:
            return await worker(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(intel, "_brief_fill_worker", observed_worker)

    async def scenario():
        if tree != "main":
            await intel.scan(project, tree_id=tree)
        intel.set_brief_provider(lambda: StubbornProvider())
        store = intel._brief_store(project, tree)
        before = store.stamp()
        intel.maybe_autofill_briefs(project, tree_id=tree)
        await asyncio.wait_for(entered.wait(), 3)
        if tree == "main":
            await asyncio.to_thread(intel.close, project["id"])
        else:
            await asyncio.to_thread(intel.close_tree, project["id"], tree)
        release.set()
        await asyncio.wait_for(finished.wait(), 3)
        assert store.stamp() == before and store.latest() == {}
        assert f"{project['id']}:{tree}" not in intel._indexes

    asyncio.run(scenario())


def test_completed_batch_updates_cached_map_while_remaining_batches_are_running(env):
    intel, project, root = env
    source = root / "src" / "Counter.java"
    source.write_text("class Counter {\n" + "\n".join(f" int m{i}() {{ return {i}; }}" for i in range(20)) + "\n}\n", encoding="utf-8")
    asyncio.run(intel.scan(project))
    before = intel.features(project)
    assert next(node for domain in before["domains"] for node in domain["features"])["label_from"] == "name"
    blocked, release = asyncio.Event(), asyncio.Event()

    class BatchProvider(Provider):
        attempts = 0

        async def chat(self, messages, tools):
            self.attempts += 1
            if self.attempts == 2:
                blocked.set()
                await release.wait()
            return await super().chat(messages, tools)

    intel.set_brief_provider(lambda: BatchProvider())

    async def scenario():
        revision = intel.brief_fill_status(project)["revision"]
        intel.maybe_autofill_briefs(project)
        await asyncio.wait_for(blocked.wait(), 3)
        status = intel.brief_fill_status(project)
        assert status["running"] and status["written"] == 12 and status["revision"] > revision
        node = next(node for domain in intel.features(project)["domains"] for node in domain["features"])
        assert node["business"]["business_meaning"] == "功能：Counter"
        release.set()
        assert (await drain(intel, project))["state"] == "complete"

    asyncio.run(scenario())
