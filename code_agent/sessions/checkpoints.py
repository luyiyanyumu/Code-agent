"""成对检查点与"回退到第 k 步"（M29 · 借自 AgentRewind 的 paired checkpoints）。

AgentRewind 的核心机制是**成对**的检查点：一个检查点不只是"对话到哪儿了"，而是
同一时刻的 **对话位置 + 工作区状态**。只回退对话（模型以为自己没改过，文件却已经
改了）或只回退文件（代码回去了，模型还记着"我已经改好了"）都会导致更糟的重复劳动。
所以一条检查点记两份东西：

    keep_seq   轨迹位置：seq <= keep_seq 的事件是这一步的"事实来源"
    patches    这一步落下的改动（patch 台账 id）——回退时按**倒序逐条反向应用**

为什么用 patch 台账做"工作区状态"而不是给工作区拍快照：快照要么很大（整个仓库），
要么很快就不再可信（快照之后用户自己又改了几笔）。台账里的每条改动都带"改前全文 +
改后 sha1"，倒序反向应用**天然得到检查点那一刻的内容**，而且沿途会命中安全前提
（文件当前内容必须仍等于那次改动之后的内容）——别人动过就拒绝自动回退，交给人判断。

一个诚实的边界（论文里也明说了）：工作区之外的影响（联网、外部服务、被删掉又
重建的临时文件）是**不可逆**的，回退不会假装能撤销它们。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

#: 一个会话最多保留多少条检查点（超过就丢最老的：它们是导航用的，不是账本）
MAX_CHECKPOINTS = int(os.environ.get("CODE_AGENT_MAX_CHECKPOINTS", "200"))

#: 检查点类型（前端据此决定在哪一行显示"回退"入口）
KINDS = ("prompt", "segment", "round", "rewind", "manual")


def store_dir() -> Path:
    p = Path(os.environ.get("CODE_AGENT_HOME", str(Path.home() / ".code-agent"))) / "checkpoints"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CheckpointStore:
    """单会话的检查点流水（JSONL 追加写，读时按 seq 排序）。"""

    def __init__(self, session_id: str, root: Path | None = None):
        self.session_id = session_id
        self.root = Path(root) if root else store_dir()
        self.path = self.root / f"{session_id}.jsonl"

    # ------------------------------------------------------------------
    def record(
        self,
        *,
        keep_seq: int,
        kind: str = "segment",
        label: str = "",
        anchor_seq: int = 0,
        patches: Iterable[dict[str, Any]] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """记一条检查点。keep_seq 是"回退到这里时保留到哪个 seq"。"""
        item = {
            "id": f"ck_{secrets.token_hex(5)}",
            "ts": _now(),
            "kind": kind if kind in KINDS else "segment",
            "keep_seq": int(keep_seq),
            "anchor_seq": int(anchor_seq or keep_seq),
            "label": (label or "")[:300],
            "note": (note or "")[:400],
            "patches": [
                {
                    "id": str(p.get("id") or ""),
                    "path": str(p.get("path") or ""),
                    "session_id": str(p.get("session_id") or ""),
                }
                for p in (patches or [])
                if p.get("id")
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("检查点落盘失败（不影响主流程）")
        self._trim()
        return item

    def _trim(self) -> None:
        """超过上限就重写文件，只留最新的 MAX_CHECKPOINTS 条。"""
        items = self.list(limit=0)
        if len(items) <= MAX_CHECKPOINTS:
            return
        keep = items[-MAX_CHECKPOINTS:]
        try:
            with self.path.open("w", encoding="utf-8") as f:
                for item in keep:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("检查点清理失败（忽略）")

    def list(self, *, limit: int = 0) -> list[dict[str, Any]]:
        """按时间正序返回检查点（limit>0 时只返回最近 limit 条）。"""
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return out[-limit:] if limit and limit > 0 else out

    def get(self, checkpoint_id: str) -> dict[str, Any] | None:
        for item in self.list():
            if item.get("id") == checkpoint_id:
                return item
        return None

    def drop_after(self, keep_seq: int) -> int:
        """回退后清理：丢掉 keep_seq 之后的检查点（含目标点本身保留）。返回丢掉几条。

        不清理的话，前端会列出一些"回退过去就再也回不来"的幻影入口——
        点下去只会报"该检查点已失效"，不如直接消失。
        """
        items = self.list()
        keep = [i for i in items if int(i.get("keep_seq") or 0) <= int(keep_seq)]
        dropped = len(items) - len(keep)
        if not dropped:
            return 0
        try:
            with self.path.open("w", encoding="utf-8") as f:
                for item in keep:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("检查点清理失败（忽略）")
            return 0
        return dropped


# ---------------------------------------------------------------------------
# 回退（把"对话位置 + 工作区状态"一起退回去）
# ---------------------------------------------------------------------------
def rewind_to(
    checkpoint: dict[str, Any],
    *,
    checkpoints: list[dict[str, Any]] | None = None,
    transcript: Any,
    session_id: str,
    revert_patch: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """回退到某条检查点。返回一份如实的报告（做了什么、没做成什么）。

    "回退到第 k 步"要撤的是**第 k 步之后的一切**，不是第 k 步自己的改动：
    所以撤销清单是后面所有检查点里的 patch 的**并集**，再倒序逐条反向应用。

    顺序很重要：**先撤文件、再截轨迹**。反过来的话，截完轨迹若撤文件失败，
    轨迹已经退回去了、文件却停在后面——模型与工作区立刻不一致，而这是最难查的
    一种状态。反过来（文件撤了、轨迹没截）至少模型能看到"我改过"，还能自救。

    revert_patch 为 None（普通会话没有 patch 台账）时只回退对话，并在报告里
    明确写"文件改动未回退"——不写这一句，用户会以为 Git 里的改动也没了。
    """
    keep_seq = int(checkpoint.get("keep_seq") or 0)
    reverted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    # 1) 反向逐条撤销该检查点之后的改动（从后往前：后一次改动依赖前一次的结果）
    later = [
        ck for ck in (checkpoints if checkpoints is not None else [checkpoint])
        if int(ck.get("keep_seq") or 0) > keep_seq
    ]
    later.sort(key=lambda ck: (int(ck.get("keep_seq") or 0), str(ck.get("ts") or "")))
    patch_ids: list[str] = []
    for ck in later:
        for item in ck.get("patches") or []:
            pid = str(item.get("id") or "")
            if pid and pid not in patch_ids:
                patch_ids.append(pid)
    for pid in reversed(patch_ids):
        if revert_patch is None:
            failed.append({"id": pid, "path": _patch_path_from(later, pid), "reason": "本次会话没有改动台账"})
            continue
        try:
            result = revert_patch(pid) or {}
            reverted.append({"id": pid, "path": str(result.get("path") or _patch_path_from(later, pid))})
        except Exception as exc:
            # 拒绝回退（别人动过这个文件）是**正确行为**，如实记下来交给人
            failed.append({"id": pid, "path": _patch_path_from(later, pid), "reason": str(exc)[:200]})

    # 2) 截轨迹：把这一步之后的事件拿掉（返回给调用方做归档 + 回退记忆）
    dropped = transcript.truncate(session_id, keep_seq)
    return {
        "checkpoint": checkpoint,
        "keep_seq": keep_seq,
        "reverted": reverted,
        "failed": failed,
        "dropped_events": len(dropped),
        "events": dropped,
    }


def _patch_path_from(checkpoints: list[dict[str, Any]], patch_id: str) -> str:
    for ck in checkpoints:
        for item in ck.get("patches") or []:
            if item.get("id") == patch_id:
                return str(item.get("path") or "")
    return ""


def report_text(result: dict[str, Any], *, note: str = "") -> str:
    """把回退结果写成一屏可读的话（谁做了什么、哪些没做成）。"""
    ck = result.get("checkpoint") or {}
    lines = [
        f"[回退] 已退回到检查点 {ck.get('id')}（{ck.get('ts')}，{ck.get('label') or ck.get('kind')}）。",
        f"· 对话记录：撤掉 {result.get('dropped_events', 0)} 条事件（保留到 seq {result.get('keep_seq')}）。",
    ]
    reverted = result.get("reverted") or []
    failed = result.get("failed") or []
    if reverted:
        files = "、".join(sorted({r["path"] for r in reverted if r.get("path")}))
        lines.append(f"· 已撤回文件改动 {len(reverted)} 处：{files}")
    if failed:
        lines.append(f"· **未撤回** {len(failed)} 处（需要你处理）：")
        for f in failed[:5]:
            lines.append(f"    - {f.get('path') or f.get('id')}：{f.get('reason')}")
    if note:
        lines += ["", note]
    return "\n".join(lines)
