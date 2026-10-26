"""JSONL 轨迹存储（s09）：追加写入，崩溃可恢复。

- append:   追加一条 Event，seq 会话内单调递增（每会话独立计数）
- replay:   读取全部事件（断线补齐 / 崩溃后回放的基础）
- last_seq: 当前最大 seq，客户端断线重连后从这里续
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from code_agent.models.events import Event, EventType


class TranscriptStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # 会话 seq 内存缓存：append 不再每次重读整个 JSONL 文件（O(n²)→O(n)）
        self._last_seq: dict[str, int] = {}

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"

    def last_seq(self, session_id: str) -> int:
        if session_id in self._last_seq:
            return self._last_seq[session_id]
        path = self._path(session_id)
        last = 0
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = max(last, int(json.loads(line).get("seq", 0)))
                    except json.JSONDecodeError:
                        # 半行（崩溃时写一半）：忽略，追加写语义下安全
                        continue
        self._last_seq[session_id] = last
        return last

    def append(self, session_id: str, type: EventType | str, payload: dict) -> Event:
        seq = self.last_seq(session_id) + 1
        self._last_seq[session_id] = seq
        event = Event(
            seq=seq,
            type=EventType(type),
            payload=payload,
        )
        path = self._path(session_id)
        # 崩溃容错：若上次写入残留了不带换行符的半行，先补一个换行，
        # 避免新事件拼接到损坏行上被一起丢掉
        needs_newline = False
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as f:
                f.seek(-1, 2)
                needs_newline = f.read(1) != b"\n"
        with path.open("a", encoding="utf-8") as f:
            if needs_newline:
                f.write("\n")
            f.write(event.model_dump_json() + "\n")
            f.flush()
        return event

    def discard(self, session_id: str) -> None:
        """删除会话时移走轨迹文件：进 .trash 而不是直接删（人工可找回）。

        不用 shutil.rmtree / os.remove——部分运行环境对批量删除有守卫，
        rename 是最稳的"删除"。
        """
        self._last_seq.pop(session_id, None)
        path = self._path(session_id)
        if not path.exists():
            return
        trash = self.root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            path.rename(trash / f"{session_id}-{stamp}.jsonl")
        except OSError:
            pass  # 移不动就先留着，DB 行已删，不影响功能

    def truncate(self, session_id: str, keep_seq: int) -> list[Event]:
        """把轨迹截到 seq <= keep_seq，**返回被截掉的那些事件**。

        "回退到第 k 步"（M29）靠它实现：轨迹是这个系统的唯一事实来源，
        回退就是"把事实来源退回去"，而不是在内存里假装忘了。

        三个刻意的取舍：
        - 用**写临时文件再 replace**，不用原地重写：中途崩了顶多留个临时文件，
          不会留下半截轨迹（轨迹坏了等于整个会话不可回放）。
        - `_last_seq` 缓存放回 `keep_seq`：后续 append 从 keep_seq+1 continue，
          seq 仍然单调，前端"after_seq 增量拉取"的语义不用改。
        - 被截掉的事件**返回给调用方**，由它归档并转成"回退记忆"——删掉的东西
          必须留下痕迹，否则模型会重新走一遍已经被证伪的路线。
        """
        import os
        import tempfile

        path = self._path(session_id)
        if not path.exists():
            self._last_seq[session_id] = 0
            return []
        kept: list[str] = []
        dropped: list[Event] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    ev = Event.model_validate_json(stripped)
                except Exception:
                    continue                       # 半行：丢弃（与 replay 一致）
                if ev.seq <= keep_seq:
                    kept.append(ev.model_dump_json())
                else:
                    dropped.append(ev)

        directory = path.parent
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), suffix=".jsonl.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for line in kept:
                    f.write(line + "\n")
                f.flush()
            os.replace(tmp_name, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        self._last_seq[session_id] = int(keep_seq)
        return dropped

    def replay(self, session_id: str, after_seq: int = 0) -> list[Event]:
        """回放 seq > after_seq 的事件（断线补齐用）。"""
        return list(self.iter_events(session_id, after_seq))

    def iter_events(self, session_id: str, after_seq: int = 0) -> Iterator[Event]:
        """Stream persisted events without retaining an entire transcript in memory."""
        path = self._path(session_id)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = Event.model_validate_json(line)
                except Exception:
                    continue
                if ev.seq > after_seq:
                    yield ev
