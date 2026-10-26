"""节点决策线程（M18 · P6）。

代码里最难传下去的东西不是实现，而是**当时为什么这么决定**。提交信息记不住，
注释容易被删，聊天记录搜不到——于是同一个坑会被反复踩。这里给地图上的每个
节点（符号/文件/模块）挂一条**线程**：决定、已知风险、待办、备注，谁写的、
哪次会话/任务写的、什么时候写的。

存储：`~/.code-agent/code/<pid>/decisions.jsonl`（JSONL 追加写，删除时重写）。
为什么用 JSONL 而不是 SQLite：它天生是"只增不改的叙事"，用户可以 grep / 直接看，
也能随项目导出；决策量级（几十到几千条）远达不到需要索引的规模。

目标标识沿用地图的节点 id（`sym:` / `file:` / `mod:`），点节点就能拉到它的线程，
不需要第二套坐标。裸路径按文件处理（`a/b.py` → `file:a/b.py`）。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 允许的条目类型（界面按这个分组；未知类型一律落到 note，而不是拒绝写入）
KINDS = ("decision", "risk", "todo", "note")
KIND_LABELS = {
    "decision": "决定",
    "risk": "已知风险",
    "todo": "待办",
    "note": "备注",
}
MAX_TEXT = 4000
MAX_ITEMS = 5000


class DecisionError(Exception):
    """不可接受的输入（例如空文本）。"""


def decisions_path(project_id: str) -> Path:
    from code_agent.config import code_dir

    return code_dir() / project_id / "decisions.jsonl"


def normalize_target(target: str) -> str:
    """把目标归一成节点 id。裸路径当文件；空的返回空串（列表查询用）。"""
    text = str(target or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith(("sym:", "file:", "mod:")):
        return text
    return f"file:{text.strip('/')}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DecisionStore:
    """一个项目的决策线程（追加写 JSONL，读取时按目标过滤）。"""

    def __init__(self, project_id: str, path: Path | str | None = None) -> None:
        self.project_id = project_id
        self.path = Path(path) if path else decisions_path(project_id)

    # ------------------------------------------------------------------
    def record(
        self,
        *,
        target: str,
        text: str,
        kind: str = "note",
        author: str = "",
        session_id: str = "",
        task_id: str = "",
        batch_id: str = "",
        meta: dict | None = None,
    ) -> dict:
        body = str(text or "").strip()
        if not body:
            raise DecisionError("内容不能为空")
        node = normalize_target(target)
        if not node:
            raise DecisionError("必须指定目标节点（符号或文件）")
        item = {
            "id": f"d_{secrets.token_hex(6)}",
            "ts": _now(),
            "target": node,
            "kind": kind if kind in KINDS else "note",
            "text": body[:MAX_TEXT],
            "author": str(author or "")[:60],
            "session_id": str(session_id or ""),
            "task_id": str(task_id or ""),
            "batch_id": str(batch_id or ""),
            "meta": meta or {},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        return item

    def all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out: list[dict] = []
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue          # 坏行跳过：一条坏记录不该让整条线程消失
            if isinstance(item, dict) and item.get("id"):
                out.append(item)
        return out

    def list(
        self, *, target: str = "", limit: int = 50, kinds: list[str] | None = None, prefix: bool = False
    ) -> list[dict]:
        """取线程。`prefix=True` 时连带子节点：文件也带出它内部符号的决定，模块带出文件。"""
        node = normalize_target(target)
        items = self.all()
        if node:
            scheme, _, ident = node.partition(":")

            def _match(item: dict) -> bool:
                other = str(item.get("target") or "")
                if other == node:
                    return True
                if not prefix:
                    return False
                if scheme == "file":
                    return other.startswith(f"sym:{ident}#")
                if scheme == "mod":
                    return other.startswith((f"file:{ident}/", f"sym:{ident}/"))
                return False

            items = [item for item in items if _match(item)]
        if kinds:
            wanted = {k for k in kinds if k in KINDS}
            items = [item for item in items if item.get("kind") in wanted]
        items.sort(key=lambda i: str(i.get("ts") or ""), reverse=True)
        return items[: max(1, int(limit))]

    def summary(self, *, limit: int = 400) -> dict:
        """按目标统计（给地图角标：这个节点上有一条决定）。"""
        counts: dict[str, dict] = {}
        for item in self.all():
            target = str(item.get("target") or "")
            bucket = counts.setdefault(target, {"total": 0, "kinds": {}})
            bucket["total"] += 1
            kind = str(item.get("kind") or "note")
            bucket["kinds"][kind] = bucket["kinds"].get(kind, 0) + 1
        trimmed = dict(sorted(counts.items(), key=lambda kv: -kv[1]["total"])[:limit])
        return {"total": sum(v["total"] for v in counts.values()), "targets": trimmed}

    def delete(self, decision_id: str) -> bool:
        items = self.all()
        keep = [item for item in items if item.get("id") != decision_id]
        if len(keep) == len(items):
            return False
        self._rewrite(keep)
        return True

    def delete_target(self, target: str) -> int:
        node = normalize_target(target)
        items = self.all()
        keep = [item for item in items if item.get("target") != node]
        removed = len(items) - len(keep)
        if removed:
            self._rewrite(keep)
        return removed

    def _rewrite(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items[-MAX_ITEMS:]),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    @staticmethod
    def view(item: dict) -> dict:
        kind = str(item.get("kind") or "note")
        return {
            "id": item.get("id") or "",
            "ts": item.get("ts") or "",
            "target": item.get("target") or "",
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "text": item.get("text") or "",
            "author": item.get("author") or "",
            "session_id": item.get("session_id") or "",
            "task_id": item.get("task_id") or "",
            "batch_id": item.get("batch_id") or "",
            "meta": item.get("meta") or {},
        }
