"""Reviewed, bounded multi-file transactions with compare-before-write and compensation.

The portable filesystem has no multi-path atomic rename/CAS. Service requests are
serialized, every path is checked before the first write and again before each
mutation; failures restore only versions created by this transaction. External
edits are preserved instead of being overwritten during compensation.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

from code_agent.codeintel import change_review as C
from code_agent.codeintel.projects import is_secret_file

MAX_FILES = 50
MAX_CHARS = 2_000_000
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", *(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³"))}


def _store(service, pid):
    conn = service._store(pid)
    conn.execute("CREATE TABLE IF NOT EXISTS workspace_refactors (id TEXT PRIMARY KEY, ts TEXT, title TEXT, source TEXT, session_id TEXT, state TEXT, files TEXT, paths TEXT NOT NULL DEFAULT '[]', file_count INTEGER NOT NULL DEFAULT 0)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_refactors)")}
    for name, definition in (("paths", "TEXT NOT NULL DEFAULT '[]'"), ("file_count", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            conn.execute(f"ALTER TABLE workspace_refactors ADD COLUMN {name} {definition}")
    backfills = list(conn.execute("SELECT id,files FROM workspace_refactors WHERE file_count=0"))
    for row in backfills:
        paths = [item["path"] for item in json.loads(row["files"])]
        conn.execute("UPDATE workspace_refactors SET paths=?,file_count=? WHERE id=?", (json.dumps(paths, ensure_ascii=False), len(paths), row["id"]))
    if backfills:
        conn.commit()
    return conn


def _target(root, rel):
    if not isinstance(rel, str) or not rel or len(rel) > 4096 or rel != rel.strip() or "\\" in rel:
        raise ValueError("重构路径必须是项目内规范的相对路径")
    parts = rel.split("/")
    if any(not part or part in (".", "..") or part.endswith((" ", ".")) or any(ch in '<>:"|?*' or ord(ch) < 32 for ch in part) for part in parts):
        raise ValueError("重构文件路径无效")
    if any(part.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES for part in parts):
        raise ValueError("重构路径不能使用 Windows 设备名称")
    if any(part.lower() in (".git", ".hg", ".svn") for part in parts) or is_secret_file(parts[-1]):
        raise ValueError("重构不允许修改版本库内部文件或密钥文件")
    base = Path(root).resolve()
    if not base.is_dir():
        raise C.ReviewConflict("项目目录不可用")
    target = C.safe_child(base, rel)
    # Even links pointing inside the project are disallowed: path identity must
    # not alias a second selected file or change while staging the transaction.
    current = base
    for part in parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("重构不支持符号链接或目录联接")
    if target.exists() and not target.is_file():
        raise ValueError("重构仅支持文本文件，不支持目录操作")
    return target


def _pack(version):
    return None if version is None else {"data": base64.b64encode(version[0]).decode("ascii"), "mode": version[2]}


def _bytes(snapshot):
    return None if snapshot is None else base64.b64decode(snapshot["data"], validate=True)


def _same(version, snapshot):
    return (version is None) if snapshot is None else version is not None and version[0] == _bytes(snapshot) and version[2] == snapshot["mode"]


def _get(service, pid, batch_id):
    row = _store(service, pid).execute("SELECT * FROM workspace_refactors WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise KeyError("重构批次不存在")
    return dict(row)


def _view(row, detail=False):
    result = {key: row[key] for key in ("id", "ts", "title", "source", "session_id", "state")}
    result["paths"] = json.loads(row["paths"])
    result["file_count"] = row["file_count"]
    if detail:
        files = json.loads(row["files"])
        result["files"] = []
        for item in files:
            before = C._text((_bytes(item["before"]), (), 0)) if item["before"] else ""
            after = C._text((_bytes(item["after"]), (), 0)) if item["after"] else ""
            result["files"].append({"path": item["path"], "kind": "create" if item["before"] is None else "delete" if item["after"] is None else "edit", "before_exists": item["before"] is not None, "after_exists": item["after"] is not None, "before_text": before, "after_text": after, "hunks": C._hunks(before, after)})
    return result


def create(service, project, body):
    with service._lock:
        files = body.get("files") if isinstance(body, dict) else None
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise ValueError("重构批次需要 1–50 个文件")
        root = Path(project["root_path"]).resolve()
        captured, seen, size, versions = [], set(), 0, {}
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("重构文件描述必须是对象")
            rel = item.get("path")
            target = _target(root, rel)
            key = os.path.normcase(str(target))
            if key in seen:
                raise ValueError("重构批次存在重复或大小写冲突路径")
            seen.add(key)
            if type(item.get("before_exists")) is not bool or type(item.get("after_exists")) is not bool:
                raise ValueError("重构需要明确文件的存在状态")
            before, after = item.get("before_text"), item.get("after_text")
            for text in (before, after):
                if not isinstance(text, str) or "\x00" in text or len(text) > C.MAX_TEXT:
                    raise ValueError("重构仅支持完整 UTF-8 文本，每个文件最多 200000 字符")
                text.encode("utf-8")
            if (not item["before_exists"] and before) or (not item["after_exists"] and after):
                raise ValueError("不存在的文件不能包含内容")
            before, after = before.replace("\r\n", "\n").replace("\r", "\n"), after.replace("\r\n", "\n").replace("\r", "\n")
            size += len(before) + len(after)
            if size > MAX_CHARS:
                raise ValueError("重构批次最多 2000000 字符，请缩小范围")
            version = C._version(target)
            if (version is not None) != item["before_exists"] or C._text(version) != before:
                raise C.ReviewConflict(f"{rel} 的磁盘内容或存在状态已变化，请重新生成重构")
            if item["before_exists"] == item["after_exists"] and before == after:
                raise ValueError("重构批次包含无变化文件")
            C._hunks(before, after)  # Bound diff materialization before storing.
            versions[rel] = version
            captured.append({"path": rel, "before": _pack(version), "after": {"data": base64.b64encode(C._disk_bytes(after)).decode("ascii"), "mode": version[2] if version else (0o666 if os.name == "nt" else 0o644)} if item["after_exists"] else None})
        for item, captured_item in zip(files, captured):
            source = item.get("mode_from")
            if source is not None:
                _target(root, source)
                version = versions.get(source)
                if version is None or captured_item["after"] is None:
                    raise ValueError("文件移动的来源必须包含在同一批次中")
                captured_item["after"]["mode"] = version[2]
                if C._text(version) == item["after_text"].replace("\r\n", "\n").replace("\r", "\n"):
                    captured_item["after"]["data"] = base64.b64encode(version[0]).decode("ascii")
        keys = [os.path.normcase(item["path"]).replace("\\", "/") for item in captured]
        if any(other.startswith(path + "/") for path in keys for other in keys if other != path):
            raise ValueError("重构文件路径不能互为父目录")
        for item in captured:
            target = _target(root, item["path"])
            C._assert_version(root, item["path"], target, versions[item["path"]])
        conn = _store(service, project["id"])
        if conn.execute("SELECT count(*) FROM workspace_refactors WHERE state='pending'").fetchone()[0] >= 100:
            raise ValueError("待审重构已达 100 条，请先完成审查")
        batch_id = f"wr_{secrets.token_hex(8)}"
        try:
            conn.execute("INSERT INTO workspace_refactors (id,ts,title,source,session_id,state,files,paths,file_count) VALUES (?,?,?,?,?,?,?,?,?)", (batch_id, datetime.now(timezone.utc).isoformat(), str(body.get("title") or "项目重构")[:200], "lsp", "", "pending", json.dumps(captured, ensure_ascii=False), json.dumps([item["path"] for item in captured], ensure_ascii=False), len(captured)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _view(_get(service, project["id"], batch_id), detail=True)


def detail(service, project, batch_id):
    with service._lock:
        return _view(_get(service, project["id"], batch_id), detail=True)


def listing(service, project):
    with service._lock:
        rows = _store(service, project["id"]).execute("SELECT id,ts,title,source,session_id,state,paths,file_count FROM workspace_refactors ORDER BY ts DESC,rowid DESC LIMIT 100").fetchall()
        return {"batches": [_view(dict(row)) for row in rows]}


def _write(root, rel, target, snapshot, expected, on_written=None):
    _target(root, rel)
    if snapshot is None:
        C._remove(root, rel, target, expected)
        if on_written:
            on_written(None)
        return None
    blob = _bytes(snapshot)
    written = C._replace_bytes(root, rel, target, blob, expected) if expected else C._create_bytes(root, rel, target, blob)
    if on_written:
        on_written(written)
    if written[2] != snapshot["mode"]:
        C._assert_version(root, rel, target, written)
        os.chmod(target, snapshot["mode"])
        observed = C._written_version(target, blob, snapshot["mode"])
        # Keep our intended bytes/mode even if an external writer races this
        # post-chmod stat; compensation must never adopt their content as ours.
        written = (blob, observed[1], snapshot["mode"])
        if on_written:
            on_written(written)
    return written


def decide(service, project, batch_id, action):
    with service._lock:
        row = _get(service, project["id"], batch_id)
        expected_state = "applied" if action == "rollback" else "pending"
        if action not in ("apply", "reject", "rollback"):
            raise ValueError("未知重构审查操作")
        if row["state"] != expected_state:
            raise C.ReviewConflict("重构批次已处理，请刷新")
        files = json.loads(row["files"])
        root = Path(project["root_path"]).resolve()
        forward = action != "rollback"
        originals, written, targets = {}, {}, {}
        if action != "reject":
            for item in files:
                target = _target(root, item["path"])
                current = C._version(target)
                baseline = item["before" if forward else "after"]
                if not _same(current, baseline):
                    raise C.ReviewConflict(f"{item['path']} 已发生其他改动，整个批次未应用，当前草稿与磁盘内容已保留")
                targets[item["path"]] = target
                originals[item["path"]] = current
        conn = _store(service, project["id"])
        state = {"apply": "applied", "rollback": "reverted", "reject": "rejected"}[action]
        try:
            if action != "reject":
                # Create destination files before removing rename sources.
                ordered = sorted(files, key=lambda item: item["after" if forward else "before"] is None)
                for item in ordered:
                    rel = item["path"]
                    _target(root, rel)
                    C._assert_version(root, rel, targets[rel], originals[rel])
                    _write(root, rel, targets[rel], item["after" if forward else "before"], originals[rel], lambda current, path=rel: written.__setitem__(path, current))
            conn.execute("UPDATE workspace_refactors SET state=? WHERE id=?", (state, batch_id))
            conn.commit()
        except Exception as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            failures = []
            for rel, own_version in reversed(list(written.items())):
                try:
                    _target(root, rel)
                    C._assert_version(root, rel, targets[rel], own_version)
                    _write(root, rel, targets[rel], _pack(originals[rel]), own_version)
                except Exception:
                    failures.append(rel)
            if failures:
                raise C.ReviewPersistenceError("重构中断；恢复期间检测到外部改动，已保留磁盘现状。请检查：" + ", ".join(failures)) from exc
            if written and isinstance(exc, C.ReviewConflict):
                raise C.ReviewConflict("重构目标在写入期间变化，已恢复本批次写入，请重新生成提案") from exc
            if written:
                raise C.ReviewPersistenceError("重构未完成，已恢复本批次写入，原提案可重试") from exc
            raise
        warning = ""
        if action != "reject":
            for item in files:
                try:
                    service.intel.record_patch(workspace=root, rel=item["path"], session_id="", tool="workspace_refactor_" + action, before=C._text(originals[item["path"]]), after=C._text(written[item["path"]]), note=batch_id)
                except Exception:
                    warning = "重构已保存，台账记录暂不可用；仍可按批次回滚。"
        return {"changed": action != "reject", "paths": [item["path"] for item in files], "warning": warning, "batch": _view(_get(service, project["id"], batch_id), detail=True)}
