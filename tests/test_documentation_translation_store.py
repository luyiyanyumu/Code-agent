"""Permanent translated prose is keyed only by source/language/protocol."""
from __future__ import annotations

from code_agent.codeintel.documentation_translation_store import DocumentationTranslationStore, translation_key


def test_default_store_is_isolated_memory_without_implicit_user_paths():
    first, second = DocumentationTranslationStore(), DocumentationTranslationStore()
    try:
        first.put("Returns the element.", "返回该元素。")
        assert first.path is None and not first.stats()["persistent"]
        assert second.get("Returns the element.") is None
    finally:
        first.close()
        second.close()


def test_disk_store_reopens_and_success_has_no_expiry(tmp_path):
    path = tmp_path / "private-platform-data" / "documentation.sqlite3"
    first = DocumentationTranslationStore(path)
    first.put("Returns the element.", "返回该元素。")
    first.close()
    second = DocumentationTranslationStore(path)
    try:
        assert second.get("Returns the element.") == "返回该元素。"
        assert second.stats()["entries"] == 1 and second.stats()["persistent"]
        assert "expires" not in second.peek()[0]
    finally:
        second.close()


def test_source_language_and_protocol_change_keys_and_preserve_old_translations(tmp_path):
    store = DocumentationTranslationStore(tmp_path / "cache.sqlite3")
    try:
        store.put("Returns the element.", "返回该元素。")
        store.put("Returns the set.", "返回该集合。")
        store.put("Returns the element.", "返回元素新版。", version="future-protocol")
        store.put("Returns the element.", "繁體譯文。", language="zh-TW")
        assert store.get("Returns the element.") == "返回该元素。"
        assert store.get("Returns the element.", version="future-protocol") == "返回元素新版。"
        assert store.get("Returns the element.", language="zh-TW") == "繁體譯文。"
        assert store.get("Changed source.") is None
        assert store.stats()["entries"] == 4
        assert translation_key("a") != translation_key("b")
    finally:
        store.close()


def test_batch_retrieval_stats_and_peek_are_bounded_and_read_only():
    store = DocumentationTranslationStore()
    try:
        store.put_many((f"Source paragraph {n}.", f"中文说明{n}。") for n in range(240))
        store.put("Source paragraph 0.", "不覆盖已验证译文。")
        assert len(store.get_many(f"Source paragraph {n}." for n in range(250))) == 240
        stats = store.stats()
        assert stats["entries"] == 240 and stats["source_characters"] > 240
        assert len(store.peek(limit=999)) == 200
        assert store.peek(limit=-1) == []
        record = store.peek(limit=1)[0]
        record["translated"] = "mutated"
        assert store.get(record["source"]) != "mutated"
        assert store.get("Source paragraph 0.") == "中文说明0。"
    finally:
        store.close()


def test_independent_connections_observe_committed_translations(tmp_path):
    path = tmp_path / "cache.sqlite3"
    first, second = DocumentationTranslationStore(path), DocumentationTranslationStore(path)
    try:
        first.put("Original paragraph.", "原始段落。")
        assert second.get("Original paragraph.") == "原始段落。"
        second.put("Second paragraph.", "第二段落。")
        assert first.stats()["entries"] == 2
    finally:
        first.close()
        second.close()
