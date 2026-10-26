"""Permanent, content-addressed documentation translations, outside repositories.

No provider, project, credential or expiry belongs to a successful translation
key. A caller must explicitly supply a file path for persistence; tests and
embedded users otherwise receive an isolated in-memory SQLite database.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


LANGUAGE = "zh-CN"
PROTOCOL_VERSION = "documentation-v3"


def translation_key(source: str, *, language: str = LANGUAGE, version: str = PROTOCOL_VERSION) -> str:
    payload = json.dumps([language, version, source], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DocumentationTranslationStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None and str(path) != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()
        self._connection = sqlite3.connect(str(self.path) if self.path is not None else ":memory:",
                                           timeout=1.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA busy_timeout=1000")
            if self.path is not None:
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("""CREATE TABLE IF NOT EXISTS documentation_translations (
                key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                translated TEXT NOT NULL,
                language TEXT NOT NULL,
                version TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")

    def get_many(self, sources: Iterable[str], *, language: str = LANGUAGE,
                 version: str = PROTOCOL_VERSION) -> dict[str, str]:
        wanted = {translation_key(text, language=language, version=version): text for text in sources}
        found = {}
        # Chunk the query to remain below SQLite's portable parameter limit.
        keys = list(wanted)
        with self._guard:
            for start in range(0, len(keys), 200):
                batch = keys[start:start + 200]
                rows = self._connection.execute(
                    "SELECT key, source, translated, language, version FROM documentation_translations WHERE key IN ("
                    + ",".join("?" for _ in batch) + ")", batch).fetchall()
                for row in rows:
                    if (row["source"] == wanted[row["key"]] and row["language"] == language
                            and row["version"] == version and row["translated"]):
                        found[row["source"]] = row["translated"]
        return found

    def get(self, source: str, *, language: str = LANGUAGE, version: str = PROTOCOL_VERSION) -> str | None:
        return self.get_many([source], language=language, version=version).get(source)

    def put_many(self, translations: Iterable[tuple[str, str]], *, language: str = LANGUAGE,
                 version: str = PROTOCOL_VERSION, replace: bool = False) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = []
        for source, translated in translations:
            if not isinstance(source, str) or not isinstance(translated, str) or not source.strip() or not translated.strip():
                continue
            rows.append((translation_key(source, language=language, version=version), source, translated,
                         language, version, now))
        with self._guard, self._connection:
            statement = """INSERT INTO documentation_translations
                (key, source, translated, language, version, created_at) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) """
            statement += "DO UPDATE SET translated=excluded.translated" if replace else "DO NOTHING"
            self._connection.executemany(statement, rows)

    def put(self, source: str, translated: str, *, language: str = LANGUAGE,
            version: str = PROTOCOL_VERSION) -> None:
        self.put_many([(source, translated)], language=language, version=version)

    def stats(self) -> dict:
        with self._guard:
            row = self._connection.execute("""SELECT COUNT(*) AS entries,
                COALESCE(SUM(LENGTH(source)), 0) AS source_characters,
                COALESCE(SUM(LENGTH(translated)), 0) AS translated_characters
                FROM documentation_translations""").fetchone()
        return {**dict(row), "persistent": self.path is not None}

    def peek(self, *, limit: int = 20) -> list[dict]:
        with self._guard:
            rows = self._connection.execute("""SELECT source, translated, language, version, created_at
                FROM documentation_translations ORDER BY rowid DESC LIMIT ?""", (max(0, min(200, int(limit))),)).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._guard:
            self._connection.close()
