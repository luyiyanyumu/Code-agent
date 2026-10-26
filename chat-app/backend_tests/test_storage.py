import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chat_agent.config import Config
from chat_agent.migration import import_legacy
from chat_agent.storage import Store


@pytest.fixture
def store(tmp_path):
    result = Store(Config(home=tmp_path / "chat"))
    yield result
    result.close()


def test_configuration_is_independent(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / "coding"))
    monkeypatch.setenv("CODE_AGENT_API_KEY", "legacy-secret")
    monkeypatch.setenv("CHAT_AGENT_HOME", str(tmp_path / "chat"))
    monkeypatch.delenv("CHAT_AGENT_API_KEY", raising=False)
    config = Config.from_env()
    assert config.home == tmp_path / "chat"
    assert config.api_key == ""
    assert config.provider == "offline"
    assert config.port == 8766
    config.ensure_dirs()
    assert not (tmp_path / "coding").exists()
    for unsafe in ("../escape", "a/b", "a\\b", "", ".", ".."): 
        with pytest.raises(ValueError):
            config.session_dir(unsafe)


def test_session_ownership_folder_archive_and_delete(store):
    session = store.create_session(user_id="owner", folder="项目讨论")
    assert session["project_id"] is None
    assert session["permission_mode"] == "workspace-write"
    with pytest.raises(KeyError):
        store.get_session(session["id"], "other")
    store.delete_workspace("项目讨论", "other")
    assert store.get_session(session["id"])["folder"] == "项目讨论"
    store.delete_workspace("项目讨论", "owner")
    assert store.get_session(session["id"])["folder"] == ""
    store.update_session(session["id"], status="archived")
    assert store.list_sessions("owner") == []
    with pytest.raises(ValueError):
        store.update_session(session["id"], user_id="other")
    store.delete_session(session["id"])
    with pytest.raises(KeyError):
        store.events(session["id"])


def test_concurrent_events_rewind_and_reopen_are_monotonic(store):
    sid = store.create_session()["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: store.append_event(sid, "assistant_delta", {"text": str(n)}), range(64)))
    assert [event["seq"] for event in store.events(sid)] == list(range(1, 65))
    checkpoint = store.add_checkpoint(sid, 20)
    store.rewind(sid, checkpoint["id"])
    assert len(store.events(sid)) == 19
    assert store.append_event(sid, "done", {})["seq"] == 65
    with pytest.raises(KeyError):
        store.rewind(store.create_session()["id"], checkpoint["id"])
    with pytest.raises(ValueError):
        store.add_checkpoint(sid, 99)
    assert store.events(sid, after_seq=64)[0]["seq"] == 65


def test_fork_keeps_history_and_owns_new_checkpoints(store):
    sid = store.create_session(user_id="owner")["id"]
    checkpoint = store.add_checkpoint(sid, 1)
    store.append_event(sid, "user_message", {"text": "你好"})
    store.append_event(sid, "done", {})
    with pytest.raises(KeyError):
        store.fork_session(sid, "other")
    fork = store.fork_session(sid, "owner")
    assert store.events(sid) == store.events(fork["id"])
    assert store.checkpoints(fork["id"])[0]["id"] != checkpoint["id"]
    store.rewind(fork["id"], store.checkpoints(fork["id"])[0]["id"])
    assert len(store.events(sid)) == 2
    assert store.append_event(fork["id"], "user_message", {"text": "分支"})["seq"] == 3


def test_settings_json_and_no_coding_tables(store):
    assert store.get_setting("missing", {"fallback": True}) == {"fallback": True}
    store.set_setting("model", {"provider": "offline"})
    assert store.get_setting("model") == {"provider": "offline"}
    connection = sqlite3.connect(store.config.db_path)
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    assert names == {"sessions", "events", "checkpoints", "workspaces", "settings"}


def test_removed_modes_cannot_return_after_reopening_legacy_sessions(tmp_path):
    config = Config(home=tmp_path / "legacy-chat")
    store = Store(config)
    sid = store.create_session(permission_mode="read-only")["id"]
    checkpoint = store.add_checkpoint(sid, 1)
    event = store.append_event(sid, "user_message", {"text": "keep this history"})
    with store._lock, store._conn:
        store._conn.execute("UPDATE sessions SET loop_mode='cage' WHERE id=?", (sid,))
        store._conn.execute("INSERT INTO settings VALUES ('loop_mode','\"cage\"')")
        before = dict(store._conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
    store.close()
    store = Store(config)
    try:
        after = dict(store._conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
        assert after == {**before, "loop_mode": "classic"}
        assert "loop_mode" not in store.get_session(sid)
        assert all("loop_mode" not in row for row in store.list_sessions())
        assert store.events(sid) == [event] and store.checkpoints(sid)[0]["id"] == checkpoint["id"]
        assert store.get_setting("loop_mode") is None
        with pytest.raises(ValueError, match="Unsupported session fields"):
            store.update_session(sid, loop_mode="cage")
        with pytest.raises(ValueError, match="removed"):
            store.set_setting("loop_mode", "cage")
        fork = store.fork_session(sid, "local")
        assert "loop_mode" not in fork and fork["permission_mode"] == "read-only"
        assert store.events(fork["id"]) == [event]
    finally:
        store.close()


def make_legacy(home: Path, *, multi_user=False):
    home.mkdir()
    connection = sqlite3.connect(home / "code_agent.db")
    connection.executescript("CREATE TABLE sessions(id TEXT,user_id TEXT,title TEXT,folder TEXT,project_id TEXT,permission_mode TEXT,status TEXT); CREATE TABLE settings(key TEXT,value TEXT);")
    connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", ("s_general", "u1", "旧对话", "日常", "", "read-only", "active"))
    connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", ("s_code", "u1", "编码", "", "p1", "full-access", "active"))
    connection.execute("INSERT INTO settings VALUES ('api_key','DO NOT IMPORT')")
    if multi_user:
        connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", ("s_other", "u2", "其他人", "", "", "read-only", "active"))
    connection.execute("ALTER TABLE sessions ADD COLUMN loop_mode TEXT NOT NULL DEFAULT 'cage'")
    connection.commit()
    connection.close()
    transcripts = home / "transcripts"
    transcripts.mkdir()
    (transcripts / "s_general.jsonl").write_text(json.dumps({"seq": 7, "type": "user_message", "ts": 123.0, "payload": {"text": "从旧会话来"}}, ensure_ascii=False) + "\n{incomplete", encoding="utf-8")
    artifact = home / "artifacts" / "s_general"
    artifact.mkdir(parents=True)
    (artifact / "note.txt").write_text("saved", encoding="utf-8")


def test_opt_in_import_is_general_only_idempotent_and_leaves_source(store, tmp_path):
    source = tmp_path / "legacy"
    make_legacy(source)
    before = (source / "code_agent.db").read_bytes()
    report = import_legacy(source, store)
    assert report["imported"] == 1
    assert report["skipped_events"] == 1
    assert report["copied_files"] == 1
    sid = report["sessions"][0]["id"]
    assert "loop_mode" not in store.get_session(sid)
    assert store.events(sid)[0]["payload"]["text"] == "从旧会话来"
    assert store.events(sid)[0]["ts"] == 123.0
    assert store.config.artifact_dir(sid).joinpath("note.txt").read_text() == "saved"
    assert store.get_setting("api_key") is None
    assert (source / "code_agent.db").read_bytes() == before
    assert import_legacy(source, store)["already_imported"] == 1
    assert len(store.list_sessions()) == 1


def test_import_requires_user_selection_for_multiple_owners(store, tmp_path):
    source = tmp_path / "legacy"
    make_legacy(source, multi_user=True)
    with pytest.raises(ValueError, match="multiple users"):
        import_legacy(source, store)
    assert store.list_sessions() == []
    assert import_legacy(source, store, source_user_id="u1")["imported"] == 1
