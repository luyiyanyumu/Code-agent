"""Explicit, one-way import of general conversations from a legacy home.

No import runs during server startup. This command opens the source database in
read-only mode, never reads model settings/credentials, and never deletes or
changes source sessions. Coding/project sessions are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .config import Config, validate_id
from .storage import Store


def _source_db(home: Path) -> Path:
    for name in ("code_agent.db", "ch_agent.db"):
        path = home / name
        if path.is_file():
            if not path.resolve().is_relative_to(home):
                raise ValueError("Source database must stay inside its home")
            return path
    raise FileNotFoundError("No code_agent.db or ch_agent.db in the source home")


def _transcript(home: Path, sid: str) -> tuple[list[dict], int]:
    path = home / "transcripts" / f"{sid}.jsonl"
    if not path.exists():
        return [], 0
    if path.is_symlink() or not path.resolve().is_relative_to(home):
        raise ValueError("Transcript escapes the source home")
    events, skipped = [], 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str) or not isinstance(event.get("payload", {}), dict):
                    raise ValueError("Invalid event envelope")
                events.append(event)
            except (ValueError, TypeError):
                skipped += 1
    return events, skipped


def _copy_session_files(source: Path, target: Path, source_home: Path, max_bytes: int) -> tuple[int, int]:
    copied, skipped = 0, 0
    if not source.exists():
        return copied, skipped
    if source.is_symlink() or not source.resolve().is_relative_to(source_home):
        return 0, 1
    # rglob does not traverse directory symlinks, and every file is checked again.
    for path in source.rglob("*"):
        if path.is_symlink():
            skipped += 1
            continue
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(source_home) or path.stat().st_size > max_bytes:
            skipped += 1
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            skipped += 1
            continue
        shutil.copy2(path, destination)
        copied += 1
    return copied, skipped


def import_legacy(source_home: Path | str, store: Store, *, source_user_id: str | None = None,
                  destination_user_id: str = "local") -> dict[str, Any]:
    source_home = Path(source_home).expanduser().resolve()
    if source_home == store.config.home:
        raise ValueError("Source and destination homes must differ")
    database = _source_db(source_home)
    # mode=ro intentionally retains SQLite's normal WAL handling so a live source
    # is read consistently; immutable=1 would silently omit its WAL records.
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        if not {"id", "user_id"}.issubset(columns):
            raise ValueError("Legacy database has no compatible sessions table")
        query = "SELECT * FROM sessions"
        if "project_id" in columns:
            query += " WHERE project_id IS NULL OR project_id=''"
        rows = [dict(row) for row in connection.execute(query)]
    finally:
        connection.close()
    users = {row["user_id"] for row in rows}
    if source_user_id is None and len(users) > 1:
        raise ValueError("Source has multiple users; select one with --source-user")
    if source_user_id is not None:
        rows = [row for row in rows if row["user_id"] == source_user_id]
    fingerprint = hashlib.sha256(str(database).encode("utf-8")).hexdigest()[:24]
    report: dict[str, Any] = {"imported": 0, "already_imported": 0, "skipped_events": 0, "copied_files": 0, "skipped_files": 0, "sessions": []}
    for row in rows:
        sid = validate_id(row["id"])
        map_key = f"legacy-import:{fingerprint}:{destination_user_id}:{sid}"
        previous = store.get_setting(map_key)
        if previous:
            report["already_imported"] += 1
            continue
        events, skipped_events = _transcript(source_home, sid)
        mode = row.get("permission_mode", "workspace-write")
        if mode not in {"read-only", "workspace-write"}:
            mode = "workspace-write"
        session = store.create_session(user_id=destination_user_id, folder=row.get("folder") or "", title=row.get("title") or "导入的对话", permission_mode=mode)
        destination_id = session["id"]
        try:
            # Sequence numbers are rebuilt without gaps; wall-clock time is retained.
            for event in events:
                envelope = store.append_event(destination_id, event["type"], event.get("payload", {}))
                timestamp = event.get("ts")
                if isinstance(timestamp, (int, float)):
                    with store._lock, store._conn:
                        store._conn.execute("UPDATE events SET ts=? WHERE session_id=? AND seq=?", (timestamp, destination_id, envelope["seq"]))
            for category, target in (("workspaces", store.config.session_dir(destination_id)), ("artifacts", store.config.artifact_dir(destination_id))):
                copied, skipped = _copy_session_files(source_home / category / sid, target, source_home, store.config.max_upload_bytes)
                report["copied_files"] += copied
                report["skipped_files"] += skipped
            if row.get("status") == "archived":
                store.update_session(destination_id, status="archived")
            # Record the mapping only after the full import; retries never duplicate
            # successfully copied sessions and cannot overwrite an existing session.
            store.set_setting(map_key, {"session_id": destination_id, "source_session_id": sid})
        except Exception:
            store.delete_session(destination_id)
            # Remove only roots created for this new random destination ID.
            for target in (store.config.session_dir(destination_id), store.config.artifact_dir(destination_id)):
                if target.exists() and target.resolve().is_relative_to(store.config.home):
                    shutil.rmtree(target)
            raise
        report["imported"] += 1
        report["skipped_events"] += skipped_events
        report["sessions"].append({"source_id": sid, "id": destination_id})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy legacy general conversations into the standalone chat server")
    parser.add_argument("--source-home", required=True, type=Path)
    parser.add_argument("--source-user", help="Required when the source contains general conversations for multiple users")
    parser.add_argument("--destination-home", type=Path, help="Defaults to CHAT_AGENT_HOME or ~/.chat-agent")
    arguments = parser.parse_args()
    config = Config.from_env()
    if arguments.destination_home:
        config = Config(home=arguments.destination_home)
    store = Store(config)
    try:
        print(json.dumps(import_legacy(arguments.source_home, store, source_user_id=arguments.source_user), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
