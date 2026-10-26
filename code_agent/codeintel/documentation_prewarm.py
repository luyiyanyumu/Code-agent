"""Proactive LSP documentation harvesting, with resumable content checkpoints.

The scanner supplies query positions only; all documentation remains actual LSP
evidence. Work uses synchronized editor text, never an on-disk replacement for a
dirty buffer. A single background worker leaves capacity for interactive calls.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import re
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path


def apply_changes(text: str | None, changes: list[dict]) -> str | None:
    def offset(value, point):
        lines = value.splitlines(keepends=True)
        line, column = point["line"], point["character"]
        if line == len(lines) and column == 0:
            return len(value)
        if line < 0 or line >= len(lines) or column < 0:
            raise ValueError("invalid position")
        encoded = lines[line].encode("utf-16-le")
        if column * 2 > len(encoded):
            raise ValueError("invalid column")
        prefix = encoded[:column * 2].decode("utf-16-le")
        return sum(map(len, lines[:line])) + len(prefix)

    try:
        for change in changes:
            if "range" not in change:
                text = change["text"]
            elif text is not None:
                span = change["range"]
                start, end = offset(text, span["start"]), offset(text, span["end"])
                if start > end:
                    return None
                text = text[:start] + change["text"] + text[end:]
        return text
    except (KeyError, TypeError, ValueError, UnicodeError):
        return None


def candidate_positions(text: str):
    """Calls first, then every remaining identifier, across programming languages.

    No deduplication by name: shadowed bindings and overloads can have different
    documentation. Offsets are UTF-16 as advertised in initialize.
    """
    word = re.compile(r"[^\W\d]\w*", re.UNICODE)
    for calls in (True, False):
        for number, line in enumerate(text.splitlines()):
            for match in word.finditer(line):
                is_call = bool(re.match(r"\s*\(", line[match.end():]))
                if calls != is_call:
                    continue
                yield {"line": number, "character": len(line[:match.start()].encode("utf-16-le")) // 2}


def dependency_fingerprint(root: Path) -> str:
    names = ("pom.xml", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "Cargo.lock", "Cargo.toml", "go.mod", "go.sum", "pyproject.toml", "poetry.lock",
             "requirements.txt", "uv.lock", "composer.lock", "Gemfile.lock", "*.csproj")
    digest = hashlib.sha256()
    for folder in (root, root / "backend", root / "frontend", root / "web"):
        for name in names:
            paths = sorted(folder.glob(name)) if "*" in name else [folder / name]
            for path in paths:
                try:
                    if path.stat().st_size > 4 * 1024 * 1024:
                        continue
                    digest.update(str(path.relative_to(root)).encode())
                    digest.update(path.read_bytes())
                except OSError:
                    continue
    return digest.hexdigest()


class DocumentationPrewarmer:
    def __init__(self, manager, path: Path | None = None, *, debounce=0.75):
        self.manager = manager
        self.debounce = debounce
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False, timeout=1)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS lsp_documentation_warmup "
                        "(key TEXT PRIMARY KEY, cursor INTEGER NOT NULL, completed INTEGER NOT NULL)")
        self.db.commit()
        self.pending = OrderedDict()
        self.states = {}
        self.task = None
        self._paused = False
        self._wake = asyncio.Event()

    def _identity(self, session, uri, text):
        root = str(session.root)
        epoch = dependency_fingerprint(session.root)
        return hashlib.sha256(json.dumps([root, session.server_id, session.command, session.args, uri, text, epoch,
                                         "harvest-v1"], ensure_ascii=False).encode()).hexdigest()

    def schedule(self, session, uri, text, *, force=False):
        if self._paused or not session.supports("hoverProvider") or not isinstance(text, str):
            return
        key = (id(session), uri)
        identity = self._identity(session, uri, text)
        existing = self.states.get(key)
        if not force and existing and existing["identity"] == identity and existing["version"] == session.documents.get(uri):
            return
        row = self.db.execute("SELECT cursor, completed FROM lsp_documentation_warmup WHERE key=?", (identity,)).fetchone()
        cursor, complete = row if row else (0, 0)
        state = {"identity": identity, "uri": uri, "version": session.documents.get(uri),
                 "status": "ready" if complete else "queued", "processed": cursor,
                 "translated": 0, "failed": 0, "session": session,
                 "positions": itertools.islice(candidate_positions(text), cursor, None),
                 "text": text, "generation": self.manager._generation, "retry_at": 0,
                 "first_failed": None, "first_empty": None, "empty": 0, "last_notify": 0}
        self.states[key] = state
        self.pending.pop(key, None)
        if not complete:
            self.pending[key] = state
            self.pending.move_to_end(key, last=False)  # current editor before older workspace files
            self._wake.set()
            self._ensure_worker()

    def _ensure_worker(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    def cancel(self, session, uri=None):
        for key, state in list(self.states.items()):
            if state["session"] is session and (uri is None or state["uri"] == uri):
                state["status"] = "cancelled"
                self.pending.pop(key, None)
                self.states.pop(key, None)

    def current(self, state):
        session, uri = state["session"], state["uri"]
        return (not self._paused and session.alive and state["generation"] == self.manager._generation
                and self.states.get((id(session), uri)) is state
                and session.documents.get(uri) == state["version"]
                and session.document_texts.get(uri) == state["text"])

    def _checkpoint(self, state, *, completed=False):
        self.db.execute("INSERT OR REPLACE INTO lsp_documentation_warmup VALUES (?, ?, ?)",
                        (state["identity"], min(n for n in (state["first_failed"], state["first_empty"], state["processed"])
                                                if n is not None), int(completed)))
        self.db.commit()

    def _notify(self, state):
        session = state["session"]
        state["last_notify"] = time.monotonic()
        self.manager._publish(session, {"jsonrpc": "2.0", "method": "code-agent/documentationUpdated", "params": {
            "uri": state["uri"], "version": state["version"], "status": state["status"],
            "prewarm": self._view(state)}})

    @staticmethod
    def _view(state):
        return {key: state[key] for key in ("uri", "version", "status", "processed", "translated", "failed", "empty")}

    def status(self, project_id=None, tree_id=None):
        states = [state for state in self.states.values()
                  if (project_id is None or state["session"].project_id == project_id)
                  and (tree_id is None or state["session"].tree_id == tree_id)]
        return {"files": [self._view(state) for state in states], "queued": sum(s["status"] == "queued" for s in states),
                "running": sum(s["status"] == "running" for s in states),
                "ready": sum(s["status"] == "ready" for s in states),
                "failed": sum(s["failed"] for s in states), "processed": sum(s["processed"] for s in states)}

    async def _run(self):
        await asyncio.sleep(self.debounce)
        while self.pending and not self._paused:
            key, state = self.pending.popitem(last=False)
            if not self.current(state):
                continue
            if state["retry_at"] > asyncio.get_running_loop().time():
                self.pending[key] = state
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                continue
            position = next(state["positions"], None)
            if position is None:
                state["status"] = "ready" if not state["failed"] else "partial"
                # An empty reply may be a server still indexing. Never persist
                # it as proof that no documentation exists for this source.
                self._checkpoint(state, completed=not state["failed"] and not state["empty"])
                self._notify(state)
                if state["first_failed"] is not None:
                    state["processed"] = state["first_failed"]
                    state["positions"] = itertools.islice(candidate_positions(state["text"]), state["processed"], None)
                    state["first_failed"] = None
                    state["first_empty"] = None
                    state["empty"] = 0
                    state["failed"] = 0
                    state["retry_at"] = asyncio.get_running_loop().time() + 60
                    self.pending[key] = state
                continue
            state["status"] = "running"
            produced = False
            try:
                result = await self.manager.request(state["session"], "textDocument/hover", {
                    "textDocument": {"uri": state["uri"]}, "position": position}, timeout=5)
                if not self.current(state):
                    continue
                if result:
                    translated = await self.manager.documentation_translator.translate_hover(result, scope="prewarm")
                    if not self.current(state):
                        continue
                    if translated.get("documentation_translation") or translated != result:
                        state["translated"] += 1
                        produced = True
                    else:
                        from code_agent.codeintel.documentation_translation import (
                            needs_translation,
                        )
                        if needs_translation(result.get("contents")):
                            state["failed"] += 1
                            if state["first_failed"] is None:
                                state["first_failed"] = state["processed"]
                else:
                    state["empty"] += 1
                    if state["first_empty"] is None:
                        state["first_empty"] = state["processed"]
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- best-effort harvesting must not stop the editor
                if not self.current(state):
                    continue
                state["failed"] += 1
                if state["first_failed"] is None:
                    state["first_failed"] = state["processed"]
            if not self.current(state):
                continue
            state["processed"] += 1
            self._checkpoint(state)
            if produced and time.monotonic() - state["last_notify"] >= 2:
                self._notify(state)
            self.pending[key] = state
            await asyncio.sleep(0.02)

    async def close(self):
        self._paused = True
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.task = None
        self.pending.clear()
        self.states.clear()
        self._paused = False
