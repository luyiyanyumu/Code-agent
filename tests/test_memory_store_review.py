"""Interrupted mirror projection must preserve concurrent external edits."""

import json

from code_agent.memory import store as memory_store


def test_external_edit_between_canonical_commit_and_mirror_is_recovered(tmp_path, monkeypatch):
    memory = memory_store.CuratedMemory(tmp_path / "memory")
    record = memory.save_fact("Existing project convention")
    original_atomic = memory_store._atomic
    injected = False

    def external_edit_after_canonical_write(path, text):
        nonlocal injected
        original_atomic(path, text)
        if path == memory.records_file and not injected and json.loads(text).get("pending_mirror"):
            injected = True
            memory.memory_md.write_text(
                "- Existing project convention\n- External editor addition\n", encoding="utf-8"
            )

    monkeypatch.setattr(memory_store, "_atomic", external_edit_after_canonical_write)
    try:
        memory.update_record(record["id"], {"enabled": False})
    except memory_store.MemoryConflict:
        # A conflict is an acceptable visible outcome. The canonical transaction
        # must retain metadata while its next read recovers the edited mirror.
        pass

    snapshot = memory.snapshot()
    by_content = {row["content"]: row for row in snapshot["records"]}
    assert injected
    assert "External editor addition" in by_content
    assert by_content["Existing project convention"]["id"] == record["id"]
    assert by_content["Existing project convention"]["enabled"] is False
    assert "External editor addition" in memory.memory_md.read_text(encoding="utf-8")
