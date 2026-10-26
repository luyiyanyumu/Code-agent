"""待审修改只保存提案；人工按块应用后才写项目，写入保留台账与版本检查。"""
from __future__ import annotations

import contextlib
import difflib
import json
import os
import secrets
import sqlite3
import stat
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from code_agent.codeintel.hashing import sha1_text
from code_agent.codeintel.paths import safe_child
from code_agent.config import code_dir

MAX_TEXT = 200_000
MAX_DIFF_LINES = 20_000
MAX_HUNK_CACHE_BYTES = 4 * 1024 * 1024
MAX_HUNK_CACHE_ENTRIES = 16


class ReviewConflict(ValueError):
    pass


class ReviewPersistenceError(RuntimeError):
    pass


def _stamp(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _version(path: Path):
    """Bounded raw snapshot, with an fstat check against mid-read changes."""
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return None
    with stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("提案路径必须是文本文件")
        blob = stream.read(MAX_TEXT * 4 + 1)
        after = os.fstat(stream.fileno())
    if _stamp(before) != _stamp(after):
        raise ReviewConflict("读取期间文件发生变化，请刷新后重试")
    if len(blob) > MAX_TEXT * 4:
        raise ValueError("文件过大，请拆分修改")
    return (blob, _stamp(after), stat.S_IMODE(after.st_mode))


def _text(version):
    if version is None:
        return ""
    try:
        text = version[0].decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as exc:
        raise ValueError("提案仅支持 UTF-8 文本文件") from exc
    if "\x00" in text or len(text) > MAX_TEXT:
        raise ValueError("文件不是可审查的文本或超过字符上限")
    return text


def _assert_version(root, rel, target, expected):
    if safe_child(root, rel) != target or _version(target) != expected:
        raise ReviewConflict("写入前文件已被其他改动更新，请刷新后重试")


def _create_bytes(root, rel, target, data):
    """O_EXCL is the atomic existence check; no concurrent file gets replaced."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if safe_child(root, rel) != target:
        raise ReviewConflict("文件路径在写入前发生变化")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise ReviewConflict("文件已由其他操作创建，请刷新后重试") from exc
    identity = _stamp(os.fstat(fd))[:2]
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # Remove only the failed file we created, never a replacement file or
        # content written by someone else while the write was failing.
        with contextlib.suppress(OSError, ValueError):
            current = _version(target)
            if current and current[1][:2] == identity and current[0] == data[:len(current[0])]:
                target.unlink()
        raise
    return _written_version(target, data, 0o644)


def _written_version(target, data, mode):
    # An external writer may change/delete the path immediately after our
    # write. Never mistake their bytes for ours on a failed DB commit.
    try:
        current = _version(target)
    except (OSError, ValueError):
        current = None
    return (data, current[1] if current else (), current[2] if current else mode)


def _replace_bytes(root, rel, target, data, expected):
    """Prepare fully, then recheck immediately before the atomic replacement.

    Other service requests are serialized. External writers cannot participate
    in a portable file CAS, so a final compare/replace window still exists.
    """
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if expected:
            os.chmod(temporary, expected[2])
        _assert_version(root, rel, target, expected)
        os.replace(temporary, target)
        return _written_version(target, data, expected[2] if expected else 0o644)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _remove(root, rel, target, expected):
    _assert_version(root, rel, target, expected)
    target.unlink()


def _disk_bytes(text):
    # Preserve the existing atomic_write newline convention, including CRLF
    # on Windows; failed DB commits restore the *original* bytes instead.
    return text.replace("\n", os.linesep).encode("utf-8")


def _hunks(before: str, after: str) -> list[dict]:
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    result = []
    displayed = 0
    # Large repeated files are quadratic when autojunk is disabled. Keep exact
    # matching for small files and the documented heuristic for larger diffs.
    for number, group in enumerate(difflib.SequenceMatcher(None, old, new, autojunk=True).get_grouped_opcodes(3)):
        displayed += sum(b - a if tag == "equal" else b - a + d - c for tag, a, b, c, d in group)
        if number >= 200 or displayed > MAX_DIFF_LINES:
            raise ValueError("差异超过 200 个块或 20000 行，请拆分提案")
        lines = []
        for tag, a, b, c, d in group:
            if tag == "equal":
                lines.extend({"kind": "context", "old_line": i + 1, "new_line": c + i - a + 1, "text": old[i]} for i in range(a, b))
            else:
                lines.extend({"kind": "remove", "old_line": i + 1, "new_line": None, "text": old[i]} for i in range(a, b))
                lines.extend({"kind": "add", "old_line": None, "new_line": i + 1, "text": new[i]} for i in range(c, d))
        first, last = group[0], group[-1]
        result.append({"id": f"h{number + 1}", "old_start": first[1], "old_end": last[2],
                       "new_start": first[3], "new_end": last[4], "replacement": new[first[3]:last[4]], "lines": lines})
    if len(result) > 200:
        raise ValueError("修改超过 200 个差异块，请拆分提案")
    return result


def _compose(before: str, hunks: list[dict], applied: set[str]) -> str:
    lines = before.splitlines(keepends=True)
    for hunk in reversed(hunks):
        if hunk["id"] in applied:
            lines[hunk["old_start"]:hunk["old_end"]] = hunk["replacement"]
    return "".join(lines)


class ChangeReviewService:
    def __init__(self, intel, directory: Path | None = None):
        self.intel = intel
        self.directory = directory or code_dir()
        self._stores = {}
        self._lock = threading.RLock()
        self._hunk_cache = OrderedDict()
        self._cache_bytes = 0

    def _store(self, pid):
        if pid not in self._stores:
            directory = self.directory / pid
            directory.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(directory / "change_reviews.db", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, ts TEXT, path TEXT, title TEXT, source TEXT, session_id TEXT, state TEXT, before_text TEXT, after_text TEXT, hunks TEXT, applied TEXT, rejected TEXT)")
                columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)")}
                for name, definition in (("before_exists", "INTEGER NOT NULL DEFAULT 1"), ("before_sha1", "TEXT NOT NULL DEFAULT ''"),
                                         ("after_sha1", "TEXT NOT NULL DEFAULT ''"), ("hunk_count", "INTEGER NOT NULL DEFAULT 0")):
                    if name not in columns:
                        conn.execute(f"ALTER TABLE proposals ADD COLUMN {name} {definition}")
                # Backfill old proposals once; listings subsequently fetch only
                # small metadata, rather than reading 100 complete large diffs.
                for row in conn.execute("SELECT id,before_text,after_text,hunks FROM proposals WHERE before_sha1='' OR after_sha1=''"):
                    conn.execute("UPDATE proposals SET before_sha1=?,after_sha1=?,hunk_count=? WHERE id=?",
                                 (sha1_text(row["before_text"]), sha1_text(row["after_text"]), len(json.loads(row["hunks"])), row["id"]))
                conn.commit()
            except Exception:
                conn.close()
                raise
            self._stores[pid] = conn
        return self._stores[pid]

    def close(self):
        with self._lock:
            for conn in self._stores.values():
                conn.close()
            self._stores.clear()
            self._hunk_cache.clear()
            self._cache_bytes = 0

    def _get(self, pid, proposal_id):
        row = self._store(pid).execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError("修改提案不存在")
        return dict(row)

    def _decode_hunks(self, pid, row):
        key = (pid, row["id"])
        if key in self._hunk_cache:
            self._hunk_cache.move_to_end(key)
            return self._hunk_cache[key][0]
        hunks = json.loads(row["hunks"])
        # JSON's in-memory dict representation exceeds serialized size.
        size = len(row["hunks"].encode("utf-8")) * 8
        if size <= MAX_HUNK_CACHE_BYTES:
            while self._hunk_cache and (self._cache_bytes + size > MAX_HUNK_CACHE_BYTES or len(self._hunk_cache) >= MAX_HUNK_CACHE_ENTRIES):
                _, (_, removed) = self._hunk_cache.popitem(last=False)
                self._cache_bytes -= removed
            self._hunk_cache[key] = (hunks, size)
            self._cache_bytes += size
        return hunks

    def _view(self, row, *, detail=False, pid=""):
        applied, rejected = set(json.loads(row["applied"])), set(json.loads(row["rejected"]))
        view = {key: row[key] for key in ("id", "ts", "path", "title", "source", "session_id", "state")}
        view["new_file"] = not row["before_exists"]
        total = row["hunk_count"]
        view.update(before_sha1=row["before_sha1"], after_sha1=row["after_sha1"],
                    counts={"total": total, "applied": len(applied), "rejected": len(rejected), "pending": 0 if row["state"] == "reverted" else total - len(applied | rejected)})
        if detail:
            hunks = [{**{key: value for key, value in h.items() if key != "replacement"},
                      "status": "reverted" if row["state"] == "reverted" and h["id"] not in rejected else "applied" if h["id"] in applied else "rejected" if h["id"] in rejected else "pending"}
                     for h in self._decode_hunks(pid, row)]
            view.update(hunks=hunks, before_text=row["before_text"], after_text=row["after_text"])
        return view

    def list(self, project):
        with self._lock:
            rows = self._store(project["id"]).execute("SELECT id,ts,path,title,source,session_id,state,before_exists,before_sha1,after_sha1,hunk_count,applied,rejected FROM proposals ORDER BY ts DESC, rowid DESC LIMIT 100").fetchall()
            return {"proposals": [self._view(dict(row)) for row in rows]}

    def detail(self, project, proposal_id):
        with self._lock:
            return self._view(self._get(project["id"], proposal_id), detail=True, pid=project["id"])

    def create_batch(self, project, body):
        from code_agent.codeintel.workspace_refactor import create
        return create(self, project, body)

    def detail_batch(self, project, batch_id):
        from code_agent.codeintel.workspace_refactor import detail
        return detail(self, project, batch_id)

    def list_batches(self, project):
        from code_agent.codeintel.workspace_refactor import listing
        return listing(self, project)

    def decide_batch(self, project, batch_id, action):
        from code_agent.codeintel.workspace_refactor import decide
        return decide(self, project, batch_id, action)

    def create(self, project, body, *, source="manual", session_id="", precondition=None):
        with self._lock:
            if not isinstance(body, dict):
                raise ValueError("提案必须是 JSON object")
            if not isinstance(body.get("path"), str) or not body["path"].strip() or len(body["path"]) > 4096 or "\x00" in body["path"]:
                raise ValueError("path 必须是项目内文件相对路径")
            root = Path(project["root_path"]).resolve()
            if not root.is_dir():
                raise ReviewConflict("项目目录不可用")
            path = safe_child(root, body["path"])
            version = _version(path)
            exists = version is not None
            if not exists and "content" not in body:
                raise ValueError("新建文件提案需要完整 content")
            if exists and source == "ai" and "content" in body:
                raise ValueError("已有文件请使用 edits 精确替换，新建文件才使用 content")
            before = _text(version)
            expected = body.get("expected_sha1")
            if expected is not None and (not isinstance(expected, str) or len(expected) != 40 or any(ch not in "0123456789abcdef" for ch in expected)):
                raise ValueError("expected_sha1 必须是 40 位 SHA-1")
            if expected and expected != sha1_text(before):
                raise ReviewConflict("文件已更新，请基于最新磁盘内容重新生成提案")
            if "content" in body:
                if not isinstance(body["content"], str):
                    raise ValueError("content 必须是文本")
                after = body["content"]
            else:
                edits = body.get("edits")
                if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
                    raise ValueError("需要 content 或 1–100 处精确替换 edits")
                after = before
                for edit in edits:
                    if not isinstance(edit, dict):
                        raise ValueError("edits 中每项必须是对象")
                    old, new = edit.get("old_string"), edit.get("new_string")
                    if not isinstance(old, str) or not old or not isinstance(new, str):
                        raise ValueError("替换需要非空 old_string 和文本 new_string")
                    if len(old) > MAX_TEXT or len(new) > MAX_TEXT:
                        raise ValueError("替换文本过大，请拆分修改")
                    if after.count(old) != 1:
                        raise ReviewConflict("old_string 必须唯一匹配，请重新读取该区间")
                    after = after.replace(old, new, 1)
                    if len(after) > MAX_TEXT:
                        raise ValueError("提案过大，请拆分修改")
            after = after.replace("\r\n", "\n").replace("\r", "\n")
            if len(after) > MAX_TEXT:
                raise ValueError("提案过大，请拆分修改")
            if "\x00" in after:
                raise ValueError("提案仅支持文本，不允许 NUL 字节")
            after.encode("utf-8")
            hunks = _hunks(before, after)
            if not hunks:
                raise ValueError("提案没有任何修改")
            if precondition:
                refusal = precondition()
                if refusal:
                    raise ReviewConflict(refusal)
            _assert_version(root, path.relative_to(root).as_posix(), path, version)
            conn = self._store(project["id"])
            pending = conn.execute("SELECT count(*) FROM proposals WHERE state IN ('pending','partial')").fetchone()[0]
            if pending >= 100:
                raise ValueError("待审提案已达 100 条，请先完成审查")
            proposal_id = f"cr_{secrets.token_hex(8)}"
            try:
                conn.execute("INSERT INTO proposals (id,ts,path,title,source,session_id,state,before_text,after_text,hunks,applied,rejected,before_exists,before_sha1,after_sha1,hunk_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    proposal_id, datetime.now(timezone.utc).isoformat(), path.relative_to(root).as_posix(),
                    str(body.get("title") or "修改建议")[:200], source, session_id, "pending", before, after,
                    json.dumps(hunks, ensure_ascii=False), "[]", "[]", int(exists), sha1_text(before), sha1_text(after), len(hunks)))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return self._view(self._get(project["id"], proposal_id), detail=True, pid=project["id"])

    def decide(self, project, proposal_id, action, ids=None):
        with self._lock:
            row = self._get(project["id"], proposal_id)
            hunks = self._decode_hunks(project["id"], row)
            applied, rejected = set(json.loads(row["applied"])), set(json.loads(row["rejected"]))
            pending = {h["id"] for h in hunks} - applied - rejected
            if row["state"] == "reverted":
                raise ReviewConflict("此提案已回滚，请重新创建提案")
            picked = pending if ids is None else set(ids)
            if action != "rollback" and (not picked or not picked <= pending):
                raise ReviewConflict("差异块已被处理或不存在，请刷新提案")
            changed, patch_id, warning = False, "", ""
            target, current, existed, version, written = None, None, False, None, None
            if action in ("apply", "rollback"):
                if action == "rollback" and not applied:
                    raise ReviewConflict("没有已应用的修改可回滚")
                target = safe_child(project["root_path"], row["path"])
                version = _version(target)
                existed = version is not None
                expected_exists = bool(row["before_exists"] or applied)
                if existed != expected_exists:
                    raise ReviewConflict("文件的存在状态已变化，请重新生成提案")
                current = _text(version)
                baseline = _compose(row["before_text"], hunks, applied)
                if current != baseline:
                    raise ReviewConflict("磁盘内容已被其他改动更新；草稿与提案均保留，请重新生成提案")
                after = row["before_text"] if action == "rollback" else _compose(row["before_text"], hunks, applied | picked)
                if action == "rollback" and not row["before_exists"]:
                    _remove(project["root_path"], row["path"], target, version)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if existed:
                        written = _replace_bytes(project["root_path"], row["path"], target, _disk_bytes(after), version)
                    else:
                        written = _create_bytes(project["root_path"], row["path"], target, _disk_bytes(after))
                changed = current != after or not row["before_exists"]
                if action == "apply":
                    applied |= picked
            elif action == "reject":
                rejected |= picked
            else:
                raise ValueError("未知审查操作")
            state = "reverted" if action == "rollback" else "applied" if len(applied) == len(hunks) else "rejected" if len(rejected) == len(hunks) else "reviewed" if len(applied | rejected) == len(hunks) else "partial" if applied or rejected else "pending"
            conn = self._store(project["id"])
            try:
                conn.execute("UPDATE proposals SET state=?, applied=?, rejected=? WHERE id=?", (state, json.dumps(sorted(applied)), json.dumps(sorted(rejected)), proposal_id))
                conn.commit()
            except Exception as exc:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                if target is not None and changed:
                    try:
                        if written is None:  # rollback deleted a newly created file
                            _create_bytes(project["root_path"], row["path"], target, version[0])
                        elif existed:
                            _replace_bytes(project["root_path"], row["path"], target, version[0], written)
                        else:
                            _remove(project["root_path"], row["path"], target, written)
                    except Exception as restore_exc:
                        raise ReviewPersistenceError("提案状态保存失败；文件随后发生其他改动，已保留磁盘现状，请刷新后重新审查") from restore_exc
                message = "提案状态保存失败，文件已恢复，原提案仍可重试" if changed else "提案状态保存失败，原提案仍可重试"
                raise ReviewPersistenceError(message) from exc
            if changed:
                try:
                    record = self.intel.record_patch(workspace=Path(project["root_path"]), rel=row["path"],
                        session_id=row["session_id"], tool="proposal_apply" if action == "apply" else "proposal_rollback",
                        before=current, after=after, note=f"{proposal_id}：{','.join(sorted(picked if action == 'apply' else applied))}")
                    patch_id = record.get("id", "")
                except Exception:
                    warning = "修改已保存，台账记录暂不可用。" + ("仍可在此提案回滚。" if action == "apply" else "")
            return {"changed": changed, "path": row["path"], "patch_id": patch_id, "warning": warning, "proposal": self._view(self._get(project["id"], proposal_id), detail=True, pid=project["id"])}
