"""Memory scope and retrieval regressions through offline code runtimes.

Every data directory and provider is local to the test: no user memories,
running application, model credentials, or external services are touched.
"""

from __future__ import annotations

import asyncio

import pytest

from code_agent.agent.router import ModelRouter
from code_agent.context.prompt import memory_blocks
from code_agent.memory.retriever import retrieve
from code_agent.memory.store import UserMemory, WorkspaceMemory
from code_agent.memory.tools_save import SaveMemoryTool
from code_agent.models.events import ToolCall
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.tools.registry import ToolRegistry


@pytest.fixture
def memory_environment(agent_home):
    db = Database(agent_home / "memory-runtime.db")
    users = UserRepo(db)
    alice = users.create("memory-alice", "test-only")["id"]
    bob = users.create("memory-bob", "test-only")["id"]
    projects = ProjectRepo(db)
    registry = ToolRegistry()
    registry.register(SaveMemoryTool())
    manager = SessionManager(
        transcript=TranscriptStore(agent_home / "transcripts"),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=registry,
        project_repo=projects,
    )

    def create_project(user, name):
        repo = agent_home / name
        repo.mkdir()
        (repo / "main.py").write_text("pass\n", encoding="utf-8")
        project = projects.create(user, name, str(repo), str(repo))
        meta = manager.create_project_session(user, project["id"], permission_mode="workspace-write")
        return manager.get_runtime(meta["id"], user), project

    primary, project = create_project(alice, "primary")
    other, _ = create_project(alice, "other")
    bob_runtime, _ = create_project(bob, "bob-project")
    task_repo = agent_home / "primary-task"
    task_repo.mkdir()
    task_meta = manager.create_task_session(alice, project["id"], task_id="branch-task", workspace=task_repo)
    task = manager.get_runtime(task_meta["id"], alice)
    yield {"manager": manager, "primary": primary, "task": task, "other": other,
           "bob": bob_runtime, "alice": alice, "bob_user": bob}
    db.close()


def save_via_loop(runtime, scope, content):
    provider = OfflineProvider(script=[
        OfflineProvider.tool_call_turn("save_memory", {"scope": scope, "content": content}),
        OfflineProvider.text_turn("finished"),
    ])
    loop = runtime._build_loop(provider=provider)
    return asyncio.run(loop.run("保存稳定约定", user_id=runtime.user_id))


def injected_text(runtime, query):
    _, memory = asyncio.run(runtime._prepare_context(query))
    return "\n".join(message.get("content", "") for message in memory)


def test_workspace_alias_saves_retrievable_project_memory_outside_repository(memory_environment):
    runtime = memory_environment["primary"]
    save_via_loop(runtime, "workspace", "订单数据库使用 SQLite")
    facts = WorkspaceMemory(runtime.memory_root).snapshot()["records"]
    assert len(facts) == 1 and facts[0]["content"] == "订单数据库使用 SQLite"
    assert facts[0]["source_session_id"] == runtime.session_id
    assert "SQLite" in injected_text(runtime, "订单数据库")
    assert not (runtime.workspace / "memory").exists()


def test_explicit_project_scope_is_shared_with_isolated_task(memory_environment):
    runtime, task = memory_environment["primary"], memory_environment["task"]
    save_via_loop(task, "project", "订单模块部署到 Kubernetes")
    assert runtime.memory_root == task.memory_root
    assert "Kubernetes" in injected_text(runtime, "订单模块部署")
    assert "Kubernetes" in injected_text(task, "订单模块部署")
    assert not (runtime.workspace / "memory").exists()
    assert not (task.workspace / "memory").exists()


def test_session_scope_remains_private_in_shared_project(memory_environment):
    runtime, task = memory_environment["primary"], memory_environment["task"]
    save_via_loop(task, "session", "任务验收采用蓝色样例集")
    assert "蓝色样例集" in injected_text(task, "任务验收样例")
    assert "蓝色样例集" not in injected_text(runtime, "任务验收样例")
    assert not WorkspaceMemory(runtime.memory_root).snapshot()["records"]
    assert not (task.workspace / "memory").exists()


def test_project_and_personal_memories_do_not_cross_user_or_project_boundaries(memory_environment):
    primary = memory_environment["primary"]
    other = memory_environment["other"]
    bob = memory_environment["bob"]
    save_via_loop(primary, "project", "订单部署使用 ALICE_PROJECT_ONLY")
    save_via_loop(primary, "user", "部署汇报使用 ALICE_USER_ONLY")
    assert "ALICE_PROJECT_ONLY" in injected_text(primary, "订单部署")
    assert "ALICE_PROJECT_ONLY" not in injected_text(other, "订单部署")
    assert "ALICE_USER_ONLY" in injected_text(other, "部署汇报")
    assert "ALICE_PROJECT_ONLY" not in injected_text(bob, "订单部署")
    assert "ALICE_USER_ONLY" not in injected_text(bob, "部署汇报")


def test_disable_and_pin_take_effect_on_next_context_preparation(memory_environment):
    runtime = memory_environment["primary"]
    personal = UserMemory(runtime.user_id)
    pinned = personal.save_fact("所有答复使用简体中文", pinned=True, category="preference", source="user")
    disabled = personal.save_fact("订单汇报包含 DISABLED_PERSONAL", enabled=False, pinned=True)
    project_store = WorkspaceMemory(runtime.memory_root)
    enabled = project_store.save_fact("订单汇报包含 ENABLED_PROJECT")
    assert "简体中文" in injected_text(runtime, "订单汇报")
    assert "ENABLED_PROJECT" in injected_text(runtime, "订单汇报")
    assert "DISABLED_PERSONAL" not in injected_text(runtime, "订单汇报")
    project_store.update_record(enabled["id"], {"enabled": False})
    personal.update_record(pinned["id"], {"pinned": False})
    text = injected_text(runtime, "订单汇报")
    assert "ENABLED_PROJECT" not in text and "简体中文" not in text
    assert disabled["enabled"] is False


def test_readonly_denial_does_not_save_fact_or_write_repository(memory_environment):
    runtime = memory_environment["primary"]
    memory_environment["manager"].set_permission_mode(runtime.session_id, runtime.user_id, "read-only")
    runtime.strict = True  # No human approval wait in this offline denial test.
    save_via_loop(runtime, "workspace", "订单数据库禁止保存此约定")
    events = runtime.transcript.replay(runtime.session_id)
    tool_results = [event.payload.get("result", "") for event in events if event.type.value == "tool_result"]
    assert any("[permission denied]" in text.lower() for text in tool_results), tool_results
    assert WorkspaceMemory(runtime.memory_root).snapshot()["records"] == []
    assert not (runtime.workspace / "memory").exists()


def test_transient_failures_are_logs_not_shared_durable_facts(memory_environment, monkeypatch):
    monkeypatch.setattr("code_agent.agent.loop.REMEMBER_FAILURES", True)
    runtime = memory_environment["task"]
    loop = runtime._build_loop(provider=OfflineProvider(script=[OfflineProvider.text_turn("finished")]))
    loop._remember_failure(ToolCall(name="read_file", arguments={"path": "missing.py"}), "temporary unavailable")
    project_memory = WorkspaceMemory(runtime.memory_root)
    assert project_memory.snapshot()["records"] == []
    logs = [path for path in project_memory.root.glob("*.md") if path.name != "MEMORY.md"]
    assert logs and any("temporary unavailable" in path.read_text(encoding="utf-8") for path in logs)
    assert "temporary unavailable" not in injected_text(runtime, "temporary unavailable")
    assert not (runtime.workspace / "memory").exists()


def test_cli_workspace_fallback_remains_supported(agent_home, workspace):
    ctx = ToolContext("standalone", workspace, "alice")
    result = asyncio.run(SaveMemoryTool().execute({"scope": "workspace", "content": "本地工作区使用 SQLite"}, ctx))
    assert "工作区记忆" in result
    assert WorkspaceMemory(workspace).entries() == ["本地工作区使用 SQLite"]


def test_explicit_project_scope_without_bound_project_does_not_write(agent_home, workspace):
    ctx = ToolContext("standalone", workspace, "alice")
    result = asyncio.run(SaveMemoryTool().execute({"scope": "project", "content": "不应写入任何项目"}, ctx))
    assert "项目" in result
    assert not (workspace / "memory").exists()
    assert UserMemory("alice").entries() == []


@pytest.mark.parametrize("query", ["file storage", "file_storage", "FileStorage"])
def test_retrieval_matches_identifier_components(query):
    hits = retrieve([("project", "FileStorageService handles uploaded documents")], query)
    assert len(hits) == 1 and "FileStorageService" in hits[0].text


def test_zero_budget_and_top_k_produce_no_memory_hits():
    records = [{"scope": "user", "content": "Pinned preference", "pinned": True, "enabled": True}]
    assert retrieve(records, "preference", top_k=0) == []
    assert retrieve(records, "preference", max_chars=0) == []


def test_disabled_pin_cannot_bypass_retrieval_filter():
    records = [
        {"scope": "user", "content": "DO_NOT_INCLUDE", "pinned": True, "enabled": False},
        {"scope": "user", "content": "Always use short answers", "pinned": True, "enabled": True},
        {"scope": "project", "content": "Unrelated volcanic minerals", "enabled": True},
    ]
    hits = retrieve(records, "repair an editor")
    assert [hit.text for hit in hits] == ["Always use short answers"]


def test_duplicates_prefer_current_session_provenance():
    entries = [
        {"scope": "user", "id": "personal", "content": "部署使用 SQLite", "enabled": True},
        {"scope": "project", "id": "project", "content": "部署使用 SQLite", "enabled": True},
        {"scope": "session", "id": "current", "content": "部署使用 SQLite", "enabled": True},
    ]
    hits = retrieve(entries, "部署")
    assert len(hits) == 1 and hits[0].scope == "session"


def test_memory_injection_is_bounded_and_keeps_untrusted_provenance():
    records = [
        {"id": f"record-{index}", "scope": "project", "label": "订单服务", "source": "user",
         "category": "decision", "enabled": True, "pinned": True,
         "content": f"订单持久决策 {index}: " + "数据库使用 SQLite。" * 90}
        for index in range(8)
    ]
    messages = memory_blocks(records, [], query="订单数据库", max_chars=600)
    assert sum(len(message["content"]) for message in messages) <= 600
    assert all(message["role"] == "user" for message in messages)
    if messages:
        text = messages[0]["content"]
        assert "数据块开始" in text and "数据块结束" in text
        assert "record-" in text and "订单服务" in text
