"""JSONL 轨迹单测（s09）：追加写、seq 单调、回放、崩溃半行容错。"""

from code_agent.models.events import EventType
from code_agent.storage.jsonl import TranscriptStore


def test_append_and_replay(agent_home):
    store = TranscriptStore(agent_home / "transcripts")
    e1 = store.append("s1", EventType.user_message, {"text": "hi"})
    e2 = store.append("s1", EventType.tool_use, {"call": {"name": "bash"}})
    assert e1.seq == 1 and e2.seq == 2
    events = store.replay("s1")
    assert len(events) == 2
    assert events[0].type == EventType.user_message
    assert events[1].payload["call"]["name"] == "bash"


def test_seq_monotonic_per_session(agent_home):
    store = TranscriptStore(agent_home / "transcripts")
    store.append("a", EventType.user_message, {})
    store.append("a", EventType.done, {})
    e3 = store.append("a", EventType.error, {})
    assert e3.seq == 3
    # 不同会话独立计数
    assert store.append("b", EventType.user_message, {}).seq == 1


def test_replay_after_seq(agent_home):
    store = TranscriptStore(agent_home / "transcripts")
    for i in range(5):
        store.append("s", EventType.user_message, {"i": i})
    events = store.replay("s", after_seq=3)
    assert [e.seq for e in events] == [4, 5]


def test_last_seq(agent_home):
    store = TranscriptStore(agent_home / "transcripts")
    assert store.last_seq("none") == 0
    store.append("s", EventType.done, {})
    store.append("s", EventType.done, {})
    assert store.last_seq("s") == 2


def test_tolerates_corrupt_half_line(agent_home):
    """模拟崩溃：文件末尾有半行损坏 JSON，追加与回放不受影响。"""
    store = TranscriptStore(agent_home / "transcripts")
    store.append("s", EventType.user_message, {"ok": 1})
    path = store._path("s")
    with path.open("a", encoding="utf-8") as f:
        f.write('{"seq": 2, "type": "done", "ts": 1.0, "payl')  # 半行
    assert store.last_seq("s") == 1
    store.append("s", EventType.done, {})  # 新事件 seq=2
    events = store.replay("s")
    assert [e.seq for e in events] == [1, 2]
