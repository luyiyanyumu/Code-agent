"""功能描述（M33）：给**没写 docstring** 的符号补一句"它是干什么的"。

为什么要有它，以及三条硬约束：

1. **不是一次性生成**。描述按「符号 id + 内容哈希」缓存：代码没变就一直复用，
   代码一改哈希就变、旧描述**自动失效**——而不是每次打开地图都重新问一遍模型。
2. **作者优先，绝不混**。作者写了 docstring 就用作者的；模型补的只在缺 docstring 时顶上，
   而且必须能被认出来（`text` 之外还带 `model`，界面上标「AI补」）。
3. **不进用户仓库**。存在 code-agent 自己的目录里
   （`~/.code-agent/feature-briefs/<仓库根哈希>.jsonl`），用户的 git 不会多出一堆文件。

存储用 JSONL 追加写（与决策线程同一套理由：只增不改的叙事，可以 grep、可以直接看、
可以随项目导出）。**按仓库根路径**而不是项目 id 定位：同一个仓库重新登记成新项目时，
这些描述仍然有效。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

#: 一条描述的字符上限。导航里的功能名只取首句，这里留够一整句话的余地。
MAX_TEXT = 200
#: 每个仓库最多保留多少条（超出时保留最新的）
MAX_ITEMS = 4000


class BriefError(Exception):
    """不可接受的输入（空文本、描述太长、符号不存在…）。"""


def root_key_of(workspace: Path | str) -> str:
    """仓库根路径 → 稳定 key（大小写与分隔符归一后取 sha1 前 16 位）。"""
    text = str(workspace or "").replace("\\", "/").rstrip("/").lower()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def briefs_path(root_key: str) -> Path:
    from code_agent.config import code_dir

    return code_dir() / "feature-briefs" / f"{root_key}.jsonl"


def autofill_mark_path(root_key: str) -> Path:
    from code_agent.config import code_dir

    return code_dir() / "feature-briefs" / f"{root_key}.autofilled"


def autofill_marked(root_key: str) -> bool:
    """这个仓库的一次性"导入后自动补全"跑过了吗。

    用 `code_dir()` 下的标记文件而不是项目表字段：同一个仓库重新登记成新项目时
    不该再自动烧一遍模型调用；删掉这个文件就等于允许再自动补一次。
    """
    return autofill_mark_path(root_key).is_file()


def mark_autofill(root_key: str, *, detail: str = "") -> Path:
    path = autofill_mark_path(root_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{_now()}\n{detail}\n", encoding="utf-8")
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BriefStore:
    """一个仓库的功能描述（追加写；同一符号以**最后一条**为准）。"""

    def __init__(self, root_key: str, path: Path | str | None = None) -> None:
        self.root_key = root_key
        self.path = Path(path) if path else briefs_path(root_key)
        self._lock = threading.RLock()
        self._latest_stamp: tuple[int, int] | None = None
        self._latest_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    def write(
        self,
        *,
        symbol_id: str,
        text: str,
        content_hash: str = "",
        body_hash: str = "",
        path_rel: str = "",
        name: str = "",
        model: str = "",
        session_id: str = "",
        business: dict | None = None,
    ) -> dict:
        body = " ".join(str(text or "").split())
        if not body:
            raise BriefError("描述不能为空")
        if len(body) > MAX_TEXT:
            raise BriefError(f"描述太长（{len(body)} 字，最多 {MAX_TEXT} 字）：导航里要的是能一眼读完的一句")
        sid = str(symbol_id or "").strip()
        if not sid:
            raise BriefError("必须指定符号 id（`sym:文件#符号`）")
        if not (content_hash or body_hash):
            raise BriefError("缺少内容哈希：描述必须挂在「某一份具体内容」上，否则失效判断无从谈起")
        item = {
            "id": f"b_{secrets.token_hex(6)}",
            "ts": _now(),
            "symbol_id": sid,
            "text": body,
            # 两个哈希都要：content_hash 跟签名/声明走，body_hash 跟实现走。
            # 描述的是"它干什么"，所以**实现变了也必须失效**——只存前者就会漏掉这种改动。
            "content_hash": str(content_hash or ""),
            "body_hash": str(body_hash or ""),
            "path": str(path_rel or ""),
            "name": str(name or ""),
            "model": str(model or ""),
            "session_id": str(session_id or ""),
        }
        from code_agent.codeintel.business import validated_fields, version_of, is_current, source_of

        fields = validated_fields(business or {"business_meaning": body})
        item.update(fields)
        item["source"] = "human" if model == "human" else "model"
        item["status"] = "current" if model == "human" else "unreviewed"
        item["version"] = version_of(item)
        with self._lock:
            existing = self.latest().get(sid)
            if model != "human" and existing and source_of(existing) == "human" and is_current(existing, item):
                # A delayed model fill cannot overwrite a manual explanation
                # submitted while its request was in flight.
                return existing
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            self._latest_stamp = None
        return item

    def rows(self) -> list[dict]:
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
                continue                      # 坏行跳过：一条坏记录不该让整批描述消失
            if isinstance(item, dict) and item.get("symbol_id") and (item.get("text") or item.get("business_meaning")):
                out.append(item)
        return out

    def latest(self) -> dict[str, dict]:
        """符号 id → 最后一条描述（这就是"当前生效"的那条）。"""
        with self._lock:
            try:
                stat = self.path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                return {}
            if stamp != self._latest_stamp:
                out: dict[str, dict] = {}
                for item in self.rows():
                    out[str(item["symbol_id"])] = item
                self._latest_cache, self._latest_stamp = out, stamp
            return dict(self._latest_cache)

    def stamp(self) -> tuple[int, int]:
        """Cheap shared-journal change token, including external/manual edits."""
        with self._lock:
            try:
                stat = self.path.stat()
                return stat.st_mtime_ns, stat.st_size
            except OSError:
                return 0, 0

    def forget(self, symbol_id: str) -> bool:
        sid = str(symbol_id or "").strip()
        with self._lock:
            items = self.rows()
            keep = [item for item in items if str(item.get("symbol_id")) != sid]
            if len(keep) == len(items):
                return False
            self._rewrite(keep)
            return True

    def summary(self) -> dict:
        latest = self.latest()
        return {
            "count": len(latest),
            "path": str(self.path),
            "items": [
                {
                    "symbol_id": sid,
                    "text": item.get("text", ""),
                    "model": item.get("model", ""),
                    "ts": item.get("ts", ""),
                    "path": item.get("path", ""),
                    "name": item.get("name", ""),
                }
                for sid, item in sorted(latest.items())
            ],
        }

    def _rewrite(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items[-MAX_ITEMS:]),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self._latest_stamp = None
