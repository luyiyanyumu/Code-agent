"""File-backed durable memories with stable records and a readable Markdown mirror.

The JSON sidecar stores management metadata. Existing MEMORY.md files are imported
on demand, and external Markdown edits are reconciled without discarding unchanged
records. Every read/modify/write transaction uses the same process and OS lock.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata
import uuid

from code_agent.config import users_dir


class MemoryConflict(ValueError):
    """The entry or collection changed after the caller read it."""


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic(path: Path, text: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class CuratedMemory:
    """Durable facts; entries() keeps the legacy string-list contract.

    Prompt retrieval should use active_records(): disabled facts stay visible in
    management and in the editable Markdown mirror, but are excluded from recall.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def memory_md(self) -> Path:
        return self.root / "MEMORY.md"

    @property
    def records_file(self) -> Path:
        return self.root / "memory-records.json"

    @contextmanager
    def _locked(self):
        key = os.path.normcase(str(self.root.resolve()))
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock:
            with (self.root / ".memory.lock").open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _parse_legacy(self, text: str) -> list[str]:
        items: list[str] = []
        current: list[str] = []

        def finish():
            if current:
                value = "\n".join(current).strip()
                if value:
                    items.append(value)
                current.clear()

        for line in text.splitlines():
            # Our mirror indents continuation lines, including headings and bullets.
            if current and (line.startswith("  ") or line.startswith("\t")):
                current.append(line[2:] if line.startswith("  ") else line[1:])
            elif line.lstrip().startswith("#"):
                finish()
            elif re.match(r"^\s*[-*]\s+", line):
                finish()
                current.append(re.sub(r"^\s*[-*]\s+", "", line, count=1))
            elif line.strip():
                finish()
                current.append(line.strip())
            else:
                finish()
        finish()
        return items

    def _load(self) -> dict:
        markdown = _read(self.memory_md)
        state = None
        if self.records_file.is_file():
            # Corrupt structured data must never be silently overwritten.
            state = json.loads(_read(self.records_file))
            if not isinstance(state, dict) or not isinstance(state.get("records"), list):
                raise ValueError("记忆数据格式无效，请保留原文件后修复")
        if state and state.get("pending_mirror"):
            pending = state["pending_mirror"]
            current_hash = _digest(markdown)
            if current_hash == state["markdown_hash"] or current_hash == pending["previous_hash"]:
                if current_hash != state["markdown_hash"]:
                    if _digest(_read(self.memory_md)) != current_hash:
                        raise MemoryConflict("记忆文件已被外部修改，请刷新后重试")
                    _atomic(self.memory_md, pending["markdown"])
                del state["pending_mirror"]
                _atomic(self.records_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
                return state
            # A mirror edit arrived while a canonical transaction was interrupted.
            # Preserve canonical metadata/edits/deletions and import new external
            # facts. Old facts from the previous mirror must not be resurrected.
            known = {_normalized(row["content"]) for row in state["records"]}
            previous = {_normalized(text) for text in pending["previous_contents"]}
            timestamp = _now()
            for content in self._parse_legacy(markdown):
                normalized = _normalized(content)
                if normalized in known or normalized in previous:
                    continue
                known.add(normalized)
                state["records"].append({
                    "id": "m_" + uuid.uuid5(uuid.NAMESPACE_URL, str(self.root.resolve()) + "\0" + normalized).hex,
                    "content": content, "created_at": timestamp, "updated_at": timestamp,
                    "enabled": True, "pinned": False, "category": "general", "source": "legacy",
                })
            del state["pending_mirror"]
            state["markdown_hash"] = current_hash
            self._commit(state)
            return state
        if state and state.get("markdown_hash") == _digest(markdown):
            return state
        if state is None and not markdown:
            return {"version": 1, "revision": 0, "records": [], "markdown_hash": _digest("")}

        old = {_normalized(row["content"]): row for row in (state or {}).get("records", [])}
        records = []
        seen = set()
        timestamp = _now()
        for content in self._parse_legacy(markdown):
            normalized = _normalized(content)
            if normalized in seen:
                continue
            seen.add(normalized)
            record = old.get(normalized)
            if record is None:
                record = {
                    "id": "m_" + uuid.uuid5(uuid.NAMESPACE_URL, str(self.root.resolve()) + "\0" + normalized).hex,
                    "content": content, "created_at": timestamp, "updated_at": timestamp,
                    "enabled": True, "pinned": False, "category": "general", "source": "legacy",
                }
            elif record["content"] != content:
                record = {**record, "content": content, "updated_at": timestamp}
            records.append(record)
        state = {"version": 1, "revision": int((state or {}).get("revision", 0)) + 1,
                 "records": records, "markdown_hash": _digest(markdown)}
        # Import metadata only: the original Markdown stays byte-for-byte intact.
        _atomic(self.records_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        return state

    def _commit(self, state: dict) -> None:
        previous = _read(self.memory_md)
        if _digest(previous) != state["markdown_hash"]:
            raise MemoryConflict("记忆文件已被外部修改，请刷新后重试")
        headings = [line for line in previous.splitlines() if line.startswith("#")]
        parts = ["\n".join(headings) + "\n\n"] if headings else []
        for row in state["records"]:
            lines = row["content"].split("\n")
            parts.append("- " + lines[0] + "\n")
            parts.extend("  " + line + "\n" for line in lines[1:])
        markdown = "".join(parts)
        state["revision"] += 1
        state["markdown_hash"] = _digest(markdown)
        state["pending_mirror"] = {"previous_hash": _digest(previous),
                                   "previous_contents": self._parse_legacy(previous),
                                   "markdown": markdown}
        # Metadata is canonical: a failed mirror write cannot reactivate a disabled
        # fact or replace its stable ID. The next read finishes the projection.
        _atomic(self.records_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        # An external editor does not use our lock and may write while JSON fsyncs.
        # Keep the pending transaction for reconciliation instead of overwriting it.
        if _digest(_read(self.memory_md)) != state["pending_mirror"]["previous_hash"]:
            raise MemoryConflict("记忆文件已被外部修改，请刷新后重试")
        _atomic(self.memory_md, markdown)
        del state["pending_mirror"]
        _atomic(self.records_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    def snapshot(self) -> dict:
        with self._locked():
            state = self._load()
            records = deepcopy(state["records"])
            return {"revision": state["revision"], "records": records,
                    "entries": [record["content"] for record in records]}

    def records(self) -> list[dict]:
        return self.snapshot()["records"]

    def active_records(self) -> list[dict]:
        rows = [row for row in self.records() if row.get("enabled", True)]
        return sorted(rows, key=lambda row: not row.get("pinned", False))

    def entries(self) -> list[str]:
        return self.snapshot()["entries"]

    def read_curated(self) -> str:
        with self._locked():
            return _read(self.memory_md)

    def _check_revision(self, state: dict, revision: int | None) -> None:
        if revision is not None and revision != state["revision"]:
            raise MemoryConflict("记忆已更新，请刷新后重试")

    def save_fact(self, text: str, *, category: str = "general", source: str = "tool",
                  source_session_id: str = "", enabled: bool = True, pinned: bool = False,
                  revision: int | None = None) -> dict:
        content = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            raise ValueError("content 不能为空")
        with self._locked():
            state = self._load()
            self._check_revision(state, revision)
            for row in state["records"]:
                if _normalized(row["content"]) == _normalized(content):
                    return deepcopy(row)
            timestamp = _now()
            record = {"id": "m_" + uuid.uuid4().hex, "content": content,
                      "created_at": timestamp, "updated_at": timestamp,
                      "enabled": bool(enabled), "pinned": bool(pinned),
                      "category": str(category), "source": str(source),
                      "source_session_id": str(source_session_id)}
            state["records"].append(record)
            self._commit(state)
            return deepcopy(record)

    def update_record(self, entry_id: str, changes: dict, *, revision: int | None = None,
                      expected_content: str | None = None) -> dict | None:
        with self._locked():
            state = self._load()
            self._check_revision(state, revision)
            row = next((row for row in state["records"] if row["id"] == entry_id), None)
            if row is None:
                return None
            if expected_content is not None and row["content"] != expected_content:
                raise MemoryConflict("记忆内容已变化，请刷新后重试")
            candidate = {**row, **{key: changes[key] for key in ("content", "enabled", "pinned", "category") if key in changes}}
            candidate["content"] = str(candidate["content"]).replace("\r\n", "\n").replace("\r", "\n").strip()
            if not candidate["content"]:
                raise ValueError("content 不能为空")
            if any(other["id"] != entry_id and _normalized(other["content"]) == _normalized(candidate["content"]) for other in state["records"]):
                raise MemoryConflict("已有相同记忆，请保留一条")
            if candidate != row:
                candidate["updated_at"] = _now()
                row.update(candidate)
                self._commit(state)
            return deepcopy(row)

    def remove_record(self, entry_id: str, *, revision: int | None = None,
                      expected_content: str | None = None) -> bool:
        with self._locked():
            state = self._load()
            self._check_revision(state, revision)
            row = next((row for row in state["records"] if row["id"] == entry_id), None)
            if row is None:
                return False
            if expected_content is not None and row["content"] != expected_content:
                raise MemoryConflict("记忆内容已变化，请刷新后重试")
            state["records"].remove(row)
            self._commit(state)
            return True

    def remove_entry(self, index: int, *, revision: int | None = None,
                     expected_content: str | None = None) -> bool:
        """Legacy index deletion; newer callers supply revision/content guards."""
        with self._locked():
            state = self._load()
            self._check_revision(state, revision)
            if index < 0 or index >= len(state["records"]):
                return False
            row = state["records"][index]
            if expected_content is not None and row["content"] != expected_content:
                raise MemoryConflict("记忆内容已变化，请刷新后重试")
            del state["records"][index]
            self._commit(state)
            return True

    def set_entries(self, items: list[str]) -> None:
        with self._locked():
            state = self._load()
            old = {_normalized(row["content"]): row for row in state["records"]}
            records = []
            seen = set()
            for text in items:
                content = str(text).strip()
                normalized = _normalized(content)
                if not content or normalized in seen:
                    continue
                seen.add(normalized)
                timestamp = _now()
                records.append(old.get(normalized) or {"id": "m_" + uuid.uuid4().hex,
                    "content": content, "created_at": timestamp, "updated_at": timestamp,
                    "enabled": True, "pinned": False, "category": "general", "source": "tool"})
            state["records"] = records
            self._commit(state)


class WorkspaceMemory(CuratedMemory):
    """Project/session memory underneath its runtime's platform memory root."""

    def __init__(self, workspace: Path):
        super().__init__(Path(workspace) / "memory")

    def log_daily(self, text: str) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        path = self.root / f"{today}.md"
        with self._locked():
            body = _read(path) or f"# {today}\n"
            _atomic(path, body + f"- {text}\n")


class UserMemory(CuratedMemory):
    """User-level preferences and constraints shared across that user's sessions."""

    def __init__(self, user_id: str):
        # IDs come from authenticated storage; reject direct untrusted path segments.
        if not user_id or user_id in (".", "..") or any(char in user_id for char in "/\\\0"):
            raise ValueError("无效用户标识")
        super().__init__(users_dir() / user_id)
