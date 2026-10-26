"""Offline harvesting, dirty-buffer, persistence and cancellation regressions."""
from __future__ import annotations

import asyncio
import copy
import re
from pathlib import Path

import pytest

from code_agent.codeintel.documentation_prewarm import (
    DocumentationPrewarmer, apply_changes, candidate_positions, dependency_fingerprint,
)


HOVER = {"contents": {"kind": "markdown", "value": "Returns the current value."}}


class FakeSession:
    def __init__(self, root, uri, text, *, lang="java", hover=True):
        self.root = Path(root)
        self.server_id = f"fixture-{lang}"
        self.command = "fixture-language-server"
        self.args = ["--stdio"]
        self.lang = lang
        self.project_id = "fixture-project"
        self.tree_id = "main"
        self.alive = True
        self.documents = {uri: 1}
        self.document_texts = {uri: text}
        self.capabilities = {"hoverProvider": hover}

    def supports(self, name):
        return bool(self.capabilities.get(name))


class FakeTranslator:
    def __init__(self, *, failure=False, gate=None):
        self.failure = failure
        self.gate = gate
        self.started = asyncio.Event()
        self.calls = []

    async def translate_hover(self, result, *, scope=""):
        self.calls.append((copy.deepcopy(result), scope))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failure:
            return result
        output = copy.deepcopy(result)
        output["contents"] = {"kind": "markdown", "value": "返回当前值。"}
        output["documentation_translation"] = {"language": "zh-CN", "source": "fixture"}
        return output


class FakeManager:
    def __init__(self, *, translator=None, response=HOVER, pause_after=None):
        self._generation = 0
        self.documentation_translator = translator if translator is not None else FakeTranslator()
        self.response = response
        self.pause_after = pause_after
        self.request_gate = asyncio.Event()
        self.request_started = asyncio.Event()
        self.requests = []
        self.notifications = []

    async def request(self, session, method, params, *, timeout):
        assert method == "textDocument/hover" and timeout > 0
        self.requests.append({"uri": params["textDocument"]["uri"], "position": copy.deepcopy(params["position"]),
                              "text": session.document_texts.get(params["textDocument"]["uri"])})
        self.request_started.set()
        if self.pause_after is not None and len(self.requests) > self.pause_after:
            await self.request_gate.wait()
        if isinstance(self.response, Exception):
            raise self.response
        return copy.deepcopy(self.response)

    def _publish(self, session, message):
        self.notifications.append(copy.deepcopy(message))


async def until(condition, timeout=2):
    async def wait():
        while not condition():
            await asyncio.sleep(0.002)
    await asyncio.wait_for(wait(), timeout)


async def close(warmer):
    await warmer.close()
    warmer.db.close()


def test_candidates_prioritize_calls_keep_shadowed_occurrences_and_count_utf16():
    text = "😀 call() value\r\nvalue = 目标()"
    assert list(candidate_positions(text)) == [
        {"line": 0, "character": 3}, {"line": 1, "character": 8},
        {"line": 0, "character": 10}, {"line": 1, "character": 0},
    ]
    assert list(candidate_positions("same(); same(); same")) == [
        {"line": 0, "character": 0}, {"line": 0, "character": 8}, {"line": 0, "character": 16},
    ]


@pytest.mark.parametrize("text,expected", [
    ("List<String> users = factory.create();", {"List", "String", "users", "factory", "create"}),
    ("def read(value): return value.name", {"def", "read", "value", "return", "name"}),
    ("const $value = factory.make();", {"const", "value", "factory", "make"}),
    ("fn main() { crate::run(); let value = source; }", {"fn", "main", "crate", "run", "let", "value", "source"}),
    ("func main() { result := service.Call() }", {"func", "main", "result", "service", "Call"}),
    ("var value = service.Read();", {"var", "value", "service", "Read"}),
    ("结果 = 服务.读取(参数)", {"结果", "服务", "读取", "参数"}),
    ("𐐀name.call()", {"𐐀name", "call"}),
])
def test_candidates_cover_identifiers_without_a_java_only_word_list(text, expected):
    lines = text.splitlines()
    words = []
    for position in candidate_positions(text):
        suffix = lines[position["line"]].encode("utf-16-le")[position["character"] * 2:].decode("utf-16-le")
        words.append(re.match(r"\w+", suffix, re.UNICODE)[0])
    assert set(words) == expected


def test_apply_changes_uses_sequential_unsaved_text_with_non_bmp_and_crlf():
    text = "a😀b\r\nsecond\r\n"
    changes = [
        {"range": {"start": {"line": 0, "character": 1}, "end": {"line": 0, "character": 3}}, "text": "中"},
        {"range": {"start": {"line": 0, "character": 2}, "end": {"line": 1, "character": 3}}, "text": "X\nY"},
    ]
    assert apply_changes(text, changes) == "a中X\nYond\r\n"
    assert apply_changes(None, [{"text": "unsaved😀\r\nvalue"}, {
        "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}}, "text": "changed",
    }]) == "unsaved😀\r\nchanged"
    assert apply_changes("source", [{"text": "first"}, {"text": "second"}]) == "second"


@pytest.mark.parametrize("text,changes", [
    ("a😀b", [{"range": {"start": {"line": 0, "character": 2}, "end": {"line": 0, "character": 3}}, "text": "x"}]),
    ("abc", [{"range": {"start": {"line": 0, "character": 3}, "end": {"line": 0, "character": 1}}, "text": "x"}]),
    ("abc", [{"range": {"start": {"line": 4, "character": 0}, "end": {"line": 4, "character": 0}}, "text": "x"}]),
    (None, [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}, "text": "x"}]),
    ("abc", [{"range": {}, "text": "x"}]),
])
def test_apply_changes_rejects_unusable_positions_without_inventing_buffer_text(text, changes):
    assert apply_changes(text, changes) is None


@pytest.mark.parametrize("lang,text", [("java", "value.call()"), ("python", "value.read()"), ("rust", "value.run()"), ("typescript", "value.make()")])
def test_scheduling_harvests_and_translates_without_an_interactive_hover(tmp_path, lang, text):
    async def flow():
        uri = (tmp_path / f"dirty-{lang}").as_uri()
        # Deliberately different on-disk bytes: only the synchronized editor
        # snapshot may supply query positions or the content checkpoint.
        (tmp_path / f"dirty-{lang}").write_text("diskOnly", encoding="utf-8")
        manager = FakeManager()
        session = FakeSession(tmp_path, uri, text, lang=lang)
        warmer = DocumentationPrewarmer(manager, debounce=0)
        try:
            warmer.schedule(session, uri, text)
            await asyncio.wait_for(warmer.task, 2)
            assert [item["position"] for item in manager.requests] == list(candidate_positions(text))
            assert all(item["text"] == text for item in manager.requests)
            assert len(manager.documentation_translator.calls) == len(manager.requests)
            assert warmer.status("fixture-project", "main")["ready"] == 1
            assert warmer.status("other-project")["files"] == []
            notification = manager.notifications[-1]
            assert notification["method"] == "code-agent/documentationUpdated"
            assert notification["params"]["version"] == 1 and notification["params"]["status"] == "ready"
            assert notification["params"]["prewarm"]["translated"] == len(manager.requests)
        finally:
            await close(warmer)
    asyncio.run(flow())


def test_completed_sqlite_checkpoint_survives_instances_but_changed_source_is_reharvested(tmp_path):
    async def flow():
        uri, text = (tmp_path / "a.py").as_uri(), "read(value)"
        path = tmp_path / "warmup.sqlite3"
        first_manager = FakeManager()
        first = DocumentationPrewarmer(first_manager, path, debounce=0)
        first.schedule(FakeSession(tmp_path, uri, text, lang="python"), uri, text)
        await asyncio.wait_for(first.task, 2)
        count = len(first_manager.requests)
        assert count == 2
        await close(first)

        manager = FakeManager()
        session = FakeSession(tmp_path, uri, text, lang="python")
        second = DocumentationPrewarmer(manager, path, debounce=0)
        try:
            second.schedule(session, uri, text)
            assert second.status()["ready"] == 1 and not manager.requests
            changed = "read(other)"
            session.documents[uri] = 2
            session.document_texts[uri] = changed
            second.schedule(session, uri, changed)
            await asyncio.wait_for(second.task, 2)
            assert len(manager.requests) == 2
            assert second.db.execute("SELECT COUNT(*) FROM lsp_documentation_warmup WHERE completed=1").fetchone()[0] == 2
        finally:
            await close(second)
    asyncio.run(flow())


def test_partial_sqlite_checkpoint_resumes_at_first_unprocessed_position(tmp_path):
    async def flow():
        uri, text = (tmp_path / "a.py").as_uri(), "first(); second(); third"
        positions = list(candidate_positions(text))
        path = tmp_path / "warmup.sqlite3"
        manager = FakeManager(pause_after=1)
        first = DocumentationPrewarmer(manager, path, debounce=0)
        first.schedule(FakeSession(tmp_path, uri, text), uri, text)
        await until(lambda: len(manager.requests) == 2)
        await close(first)

        manager = FakeManager()
        second = DocumentationPrewarmer(manager, path, debounce=0)
        try:
            second.schedule(FakeSession(tmp_path, uri, text), uri, text)
            await asyncio.wait_for(second.task, 2)
            assert [item["position"] for item in manager.requests] == positions[1:]
            assert second.status()["ready"] == 1
        finally:
            await close(second)
    asyncio.run(flow())


@pytest.mark.parametrize("failure", ["translation", "request"])
def test_failed_prose_is_not_permanently_marked_complete_and_next_instance_retries(tmp_path, failure):
    async def flow():
        path, uri, text = tmp_path / "warmup.sqlite3", (tmp_path / "a.py").as_uri(), "value"
        translator = FakeTranslator(failure=failure == "translation")
        manager = FakeManager(translator=translator, response=RuntimeError("fixture error") if failure == "request" else HOVER)
        first = DocumentationPrewarmer(manager, path, debounce=0)
        first.schedule(FakeSession(tmp_path, uri, text), uri, text)
        await until(lambda: any(item["params"]["status"] == "partial" for item in manager.notifications))
        row = first.db.execute("SELECT cursor, completed FROM lsp_documentation_warmup").fetchone()
        assert tuple(row) == (0, 0)
        await close(first)

        recovered_manager = FakeManager()
        second = DocumentationPrewarmer(recovered_manager, path, debounce=0)
        try:
            second.schedule(FakeSession(tmp_path, uri, text), uri, text)
            await asyncio.wait_for(second.task, 2)
            assert len(recovered_manager.requests) == 1 and second.status()["ready"] == 1
        finally:
            await close(second)
    asyncio.run(flow())


@pytest.mark.parametrize("stage", ["request", "translation"])
@pytest.mark.parametrize("invalidate", ["cancel", "version", "close-document", "dead-session", "generation", "changed-text"])
def test_invalidated_background_work_never_notifies_or_completes_late(tmp_path, stage, invalidate):
    async def flow():
        uri, text = (tmp_path / "a.py").as_uri(), "value"
        gate = asyncio.Event()
        translator = FakeTranslator(gate=gate if stage == "translation" else None)
        manager = FakeManager(translator=translator, pause_after=0 if stage == "request" else None)
        session = FakeSession(tmp_path, uri, text)
        warmer = DocumentationPrewarmer(manager, debounce=0)
        try:
            warmer.schedule(session, uri, text)
            await asyncio.wait_for(manager.request_started.wait() if stage == "request" else translator.started.wait(), 1)
            if invalidate == "cancel":
                warmer.cancel(session, uri)
            elif invalidate == "version":
                session.documents[uri] += 1
            elif invalidate == "close-document":
                session.documents.pop(uri)
                session.document_texts.pop(uri)
            elif invalidate == "dead-session":
                session.alive = False
            elif invalidate == "generation":
                manager._generation += 1
            elif invalidate == "changed-text":
                session.document_texts[uri] = "other"
            manager.request_gate.set()
            gate.set()
            await asyncio.wait_for(warmer.task, 2)
            assert not manager.notifications
            assert warmer.db.execute("SELECT COUNT(*) FROM lsp_documentation_warmup WHERE completed=1").fetchone()[0] == 0
        finally:
            await close(warmer)
    asyncio.run(flow())


def test_close_cancels_an_inflight_translation_and_clears_background_tasks(tmp_path):
    async def flow():
        uri, text = (tmp_path / "a.py").as_uri(), "value"
        gate = asyncio.Event()
        translator = FakeTranslator(gate=gate)
        manager = FakeManager(translator=translator)
        warmer = DocumentationPrewarmer(manager, debounce=0)
        warmer.schedule(FakeSession(tmp_path, uri, text), uri, text)
        await asyncio.wait_for(translator.started.wait(), 1)
        task = warmer.task
        try:
            await warmer.close()
            gate.set()
            await asyncio.sleep(0)
            assert task.cancelled() and warmer.task is None
            assert not warmer.pending and not warmer.states and not manager.notifications
        finally:
            warmer.db.close()
    asyncio.run(flow())


def test_unsupported_hover_and_unknown_buffer_do_not_start_a_worker(tmp_path):
    async def flow():
        uri, text = (tmp_path / "a.py").as_uri(), "value"
        manager = FakeManager()
        warmer = DocumentationPrewarmer(manager, debounce=0)
        try:
            warmer.schedule(FakeSession(tmp_path, uri, text, hover=False), uri, text)
            warmer.schedule(FakeSession(tmp_path, uri, text), uri, None)
            assert warmer.task is None and not warmer.states and not manager.requests
        finally:
            await close(warmer)
    asyncio.run(flow())


def test_dependency_fingerprint_tracks_lockfiles_nested_manifests_and_new_projects(tmp_path):
    original = dependency_fingerprint(tmp_path)
    (tmp_path / "pom.xml").write_text("<version>1</version>", encoding="utf-8")
    first = dependency_fingerprint(tmp_path)
    assert first != original
    (tmp_path / "pom.xml").write_text("<version>2</version>", encoding="utf-8")
    second = dependency_fingerprint(tmp_path)
    assert second != first
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package-lock.json").write_text('{"version":1}', encoding="utf-8")
    third = dependency_fingerprint(tmp_path)
    assert third != second
    (tmp_path / "Example.csproj").write_text("<Project />", encoding="utf-8")
    assert dependency_fingerprint(tmp_path) != third


def test_dependency_changes_in_a_new_server_session_invalidate_completed_checkpoint(tmp_path):
    async def flow():
        uri, text = (tmp_path / "a.py").as_uri(), "value"
        (tmp_path / "requirements.txt").write_text("library==1", encoding="utf-8")
        manager = FakeManager()
        warmer = DocumentationPrewarmer(manager, tmp_path / "warmup.sqlite3", debounce=0)
        try:
            warmer.schedule(FakeSession(tmp_path, uri, text), uri, text)
            await asyncio.wait_for(warmer.task, 2)
            (tmp_path / "requirements.txt").write_text("library==2", encoding="utf-8")
            warmer.schedule(FakeSession(tmp_path, uri, text), uri, text)
            await asyncio.wait_for(warmer.task, 2)
            assert len(manager.requests) == 2
            assert warmer.db.execute("SELECT COUNT(*) FROM lsp_documentation_warmup WHERE completed=1").fetchone()[0] == 2
        finally:
            await close(warmer)
    asyncio.run(flow())


def test_dependency_changes_in_the_live_session_invalidate_completed_checkpoint(tmp_path):
    async def flow():
        uri, text = (tmp_path / "a.py").as_uri(), "value"
        (tmp_path / "requirements.txt").write_text("library==1", encoding="utf-8")
        manager = FakeManager()
        session = FakeSession(tmp_path, uri, text)
        warmer = DocumentationPrewarmer(manager, debounce=0)
        try:
            warmer.schedule(session, uri, text)
            await asyncio.wait_for(warmer.task, 2)
            (tmp_path / "requirements.txt").write_text("library==2", encoding="utf-8")
            warmer.schedule(session, uri, text, force=True)
            await asyncio.wait_for(warmer.task, 2)
            assert len(manager.requests) == 2
        finally:
            await close(warmer)
    asyncio.run(flow())


@pytest.mark.parametrize("resume", ["same-session-force", "new-instance"])
def test_empty_indexing_reply_is_not_a_permanent_no_documentation_checkpoint(tmp_path, resume):
    async def flow():
        path, uri, text = tmp_path / "warmup.sqlite3", (tmp_path / "a.py").as_uri(), "value"
        manager = FakeManager(response=None)
        session = FakeSession(tmp_path, uri, text)
        warmer = DocumentationPrewarmer(manager, path, debounce=0)
        try:
            warmer.schedule(session, uri, text)
            await asyncio.wait_for(warmer.task, 2)
            assert tuple(warmer.db.execute("SELECT cursor, completed FROM lsp_documentation_warmup").fetchone()) == (0, 0)
            assert not manager.documentation_translator.calls
            if resume == "new-instance":
                await close(warmer)
                manager = FakeManager()
                session = FakeSession(tmp_path, uri, text)
                warmer = DocumentationPrewarmer(manager, path, debounce=0)
            else:
                manager.response = HOVER
            previous = len(manager.requests)
            warmer.schedule(session, uri, text, force=True)
            await asyncio.wait_for(warmer.task, 2)
            assert len(manager.requests) == previous + 1
            assert tuple(warmer.db.execute("SELECT cursor, completed FROM lsp_documentation_warmup").fetchone()) == (1, 1)
        finally:
            await close(warmer)
    asyncio.run(flow())


def test_new_editor_revision_replaces_pending_source_without_late_old_notification(tmp_path):
    async def flow():
        uri, original, changed = (tmp_path / "a.py").as_uri(), "oldValue", "newCall()"
        manager = FakeManager(pause_after=0)
        session = FakeSession(tmp_path, uri, original)
        warmer = DocumentationPrewarmer(manager, debounce=0)
        try:
            warmer.schedule(session, uri, original)
            await asyncio.wait_for(manager.request_started.wait(), 1)
            session.documents[uri] = 2
            session.document_texts[uri] = changed
            warmer.cancel(session, uri)
            warmer.schedule(session, uri, changed)
            manager.request_gate.set()
            await asyncio.wait_for(warmer.task, 2)
            assert [item["text"] for item in manager.requests] == [original, changed]
            assert len(manager.documentation_translator.calls) == 1
            assert manager.notifications and all(item["params"]["version"] == 2 for item in manager.notifications)
            assert warmer.db.execute("SELECT COUNT(*) FROM lsp_documentation_warmup WHERE completed=1").fetchone()[0] == 1
        finally:
            await close(warmer)
    asyncio.run(flow())


@pytest.mark.parametrize("change", ["command", "args"])
def test_language_server_configuration_changes_invalidate_completed_checkpoint(tmp_path, change):
    async def flow():
        uri, text = (tmp_path / "a.py").as_uri(), "value"
        manager = FakeManager()
        session = FakeSession(tmp_path, uri, text)
        warmer = DocumentationPrewarmer(manager, debounce=0)
        try:
            warmer.schedule(session, uri, text)
            await asyncio.wait_for(warmer.task, 2)
            if change == "command":
                session.command = "other-fixture-language-server"
            else:
                session.args = ["--stdio", "--different-library-version"]
            warmer.schedule(session, uri, text, force=True)
            await asyncio.wait_for(warmer.task, 2)
            assert len(manager.requests) == 2
        finally:
            await close(warmer)
    asyncio.run(flow())
