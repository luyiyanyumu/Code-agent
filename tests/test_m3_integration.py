"""M3 集成测试：跨 prompt 历史、记忆注入、外部化、日志落盘 —— 全走 SessionRuntime。"""

from datetime import datetime

import pytest

from code_agent.agent.router import ModelRouter
from code_agent.api.app import build_registry
from code_agent.config import transcripts_dir
from code_agent.memory.store import UserMemory
from code_agent.providers.offline import OfflineProvider
from code_agent.sessions.manager import SessionManager
from code_agent.storage.db import ApprovalRepo, Database, SessionRepo, UserRepo
from code_agent.storage.jsonl import TranscriptStore


class RecordingProvider(OfflineProvider):
    """记录每次 chat 收到的完整 messages，用于断言上下文内容。"""

    def __init__(self, script):
        super().__init__(script)
        self.seen: list[list[dict]] = []

    async def chat(self, messages, tools):
        import copy

        self.seen.append(copy.deepcopy(messages))
        return await super().chat(messages, tools)


@pytest.fixture
def manager(agent_home):
    db = Database(agent_home / "t.db")
    return SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name="offline"),
        registry=build_registry(),
    )


def _make_runtime(manager, user_id="u_test"):
    db = manager.session_repo.db
    users = UserRepo(db)
    existing = db.query_one("SELECT id FROM users WHERE username=?", (user_id,))
    uid = existing["id"] if existing else users.create(user_id, "x", role="user")["id"]
    meta = manager.session_repo.create(uid)
    return manager.get_runtime(meta["id"], uid)


def _install_recording(runtime, script) -> RecordingProvider:
    rec = RecordingProvider(script)
    runtime.router.provider_override = rec
    return rec


import asyncio


def test_cross_prompt_history_carried(manager, agent_home):
    runtime = _make_runtime(manager)
    rec = _install_recording(
        runtime, [OfflineProvider.text_turn("第一答"), OfflineProvider.text_turn("第二答")]
    )

    asyncio.run(runtime.submit("第一问"))
    asyncio.run(runtime.submit("第二问"))

    second = rec.seen[1]
    contents = [m.get("content") for m in second]
    # 第二轮上下文里必须包含第一轮的完整交换 + 新 prompt
    assert "第一问" in contents
    assert "第一答" in contents
    assert contents[-1] == "第二问"


def test_user_memory_injected_by_retrieval(manager, agent_home):
    runtime = _make_runtime(manager, user_id="u_mem")
    # 记忆以 user_id（JWT sub）为键，与运行时一致
    UserMemory(runtime.user_id).save_fact("用户偏好表格输出格式")
    rec = _install_recording(runtime, [OfflineProvider.text_turn("好的")])

    asyncio.run(runtime.submit("表格输出怎么格式化"))

    # M6 安全契约：记忆降权为 user 角色数据块（不进 system 高权区，防注入）
    user_blocks = [m["content"] for m in rec.seen[0] if m["role"] == "user"]
    assert any("用户偏好表格输出格式" in c for c in user_blocks)
    # 系统区不应包含外部记忆内容
    system_blocks = [m["content"] for m in rec.seen[0] if m["role"] == "system"]
    assert not any("用户偏好表格输出格式" in c for c in system_blocks)


def test_tool_result_externalized_in_transcript(manager, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_EXTERNALIZE_THRESHOLD", "100")
    runtime = _make_runtime(manager)
    _install_recording(
        runtime,
        [
            OfflineProvider.tool_call_turn("bash", {"command": "echo " + "x" * 500}),
            OfflineProvider.text_turn("done"),
        ],
    )

    asyncio.run(runtime.submit("输出一堆x"))

    events = manager.transcript.replay(runtime.session_id)
    tool_results = [e for e in events if e.type.value == "tool_result"]
    assert tool_results
    result = tool_results[0].payload["result"]
    assert "[外部化]" in result
    assert "read_file" in result


def test_daily_log_written_after_run(manager, agent_home):
    runtime = _make_runtime(manager)
    _install_recording(runtime, [OfflineProvider.text_turn("完成")])

    asyncio.run(runtime.submit("写个日志测试"))

    today = datetime.now().strftime("%Y-%m-%d")
    log = runtime.workspace / "memory" / f"{today}.md"
    assert log.exists()
    assert "写个日志测试" in log.read_text(encoding="utf-8")
