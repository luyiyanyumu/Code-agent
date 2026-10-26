"""回退记忆（M29 · 借自 AgentRewind 的 rewind memory）。

AgentRewind 的对照实验里，**有回退 + 没有回退记忆**是最差的一档（51.2% vs 43.9%）：
把状态退回去之后，模型对"刚才为什么失败"一无所知，于是**原样再走一遍**那条已被
证伪的路线——回退等于白退，还多花了一遍 token。

所以回退时必须把"被删掉的那段经历"总结成一条记忆，注回下一次上下文：

    被撤回的改动（哪些文件、几处） + 被证伪的失败（指纹 + 原因） + 走到哪儿就停了

刻意的取舍：**不额外调模型做这次总结**。AgentRewind 用模型写反思，我们用的是
已经结构化的事实（工具结果前缀、patch 台账、事件类型）——确定性、零成本、可测试，
而且不会把"我反思出来的错误结论"当成事实注入。代价是总结不如模型写的自然，
但内容全是硬的。

落盘位置：`~/.code-agent/rewind/<session>.jsonl`（**不进工作区**，用户仓库里不该
冒出平台自己的文件）。注入时与其他记忆一样降权成"user 数据块"。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: 最多保留多少条回退记忆（超出丢最老的）
MAX_NOTES = int(os.environ.get("CODE_AGENT_MAX_REWIND_NOTES", "20"))
#: 注入上下文时取最近几条、总字符上限
INJECT_NOTES = 3
INJECT_CHARS = 2000


def store_dir() -> Path:
    p = Path(os.environ.get("CODE_AGENT_HOME", str(Path.home() / ".code-agent"))) / "rewind"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def summarize(
    events: Iterable[Any],
    *,
    reverted: Iterable[dict[str, Any]] = (),
    failed: Iterable[dict[str, Any]] = (),
    keep_seq: int = 0,
) -> str:
    """把被撤掉的一段事实总结成一条"不要重走"的记忆。纯函数，可单测。"""
    ev_list = list(events)
    results: dict[str, str] = {}
    for ev in ev_list:
        payload = getattr(ev, "payload", None) or {}
        if isinstance(payload, dict) and payload.get("call_id"):
            results[str(payload.get("call_id"))] = str(payload.get("result") or "")

    failures: dict[str, int] = {}
    tools_used: dict[str, int] = {}
    last_text = ""
    for ev in ev_list:
        payload = getattr(ev, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        call = payload.get("call")
        if isinstance(call, dict):
            name = str(call.get("name") or "")
            if name:
                tools_used[name] = tools_used.get(name, 0) + 1
            args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            result = results.get(str(call.get("id") or ""), "")
            if result.startswith("[tool error]") or result.startswith("[permission denied]"):
                key = f"{name}({_brief_args(args)})"
                failures[key] = failures.get(key, 0) + 1
        text = payload.get("text")
        if isinstance(text, str) and text.strip() and payload.get("stop_reason") != "max_turns":
            last_text = text.strip()

    lines = ["[回退记忆] 上一次尝试到这里被打断并被撤回，下面这些**已经试过且不成立**，不要重走："]
    rev = list(reverted)
    if rev:
        files: dict[str, int] = {}
        for r in rev:
            files[str(r.get("path") or "?")] = files.get(str(r.get("path") or "?"), 0) + 1
        detail = "、".join(f"{p}（{n} 处）" for p, n in sorted(files.items()))
        lines.append(f"· 已撤回的改动：{detail}")
    fails = list(failed)
    if fails:
        lines.append("· 撤回失败的改动（可能是别人也动过这些文件，需要人来判断）：")
        for f in fails[:4]:
            lines.append(f"    - {f.get('path') or f.get('id')}：{f.get('reason')}")
    if failures:
        lines.append("· 反复失败、已被证伪的做法：")
        for key, n in sorted(failures.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"    - {key} ×{n}")
    if tools_used:
        top = sorted(tools_used.items(), key=lambda kv: -kv[1])[:6]
        lines.append("· 已经用过的工具：" + "、".join(f"{k}×{v}" for k, v in top))
    if last_text:
        lines.append(f"· 被打断前最后给出的结论（原文截断）：{last_text[:200]}")
    lines.append(
        f"（回退点：对话保留到 seq {keep_seq}；工作区之外的影响——联网查询、外部服务调用）"
        "不可撤销，如果依赖它们请重新确认一次。"
    )
    lines.append("下一步：换一条不同的路线（不同的定位/不同的改法），或先说明卡在哪里。")
    return "\n".join(lines)


def _brief_args(args: dict[str, Any]) -> str:
    """参数摘要：只留最能识别这次调用的片段。"""
    for key in ("command", "path", "pattern", "query", "url", "name"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:60]
    return ",".join(sorted(args)[:3])


class RewindNotes:
    """会话级的回退记忆（追加写，读最近几条）。"""

    def __init__(self, session_id: str, root: Path | None = None):
        self.session_id = session_id
        self.root = Path(root) if root else store_dir()
        self.path = self.root / f"{session_id}.jsonl"

    def append(self, text: str, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        item = {"ts": _now(), "text": text[:4000], **(meta or {})}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("回退记忆落盘失败（不影响主流程）")
        self._trim()
        return item

    def _trim(self) -> None:
        items = self.list(limit=0)
        if len(items) <= MAX_NOTES:
            return
        try:
            with self.path.open("w", encoding="utf-8") as f:
                for item in items[-MAX_NOTES:]:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("回退记忆清理失败（忽略）")

    def list(self, *, limit: int = 0) -> list[dict[str, Any]]:
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

    def context_block(self, *, chars: int = INJECT_CHARS, limit: int = INJECT_NOTES) -> str:
        """给模型的注入块。没有记忆时返回 ''（不产生空块噪音）。"""
        items = self.list(limit=limit)
        if not items:
            return ""
        parts: list[str] = []
        used = 0
        for item in reversed(items):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if used + len(text) > chars:
                break
            parts.append(text)
            used += len(text)
        if not parts:
            return ""
        return "\n\n".join(reversed(parts))
