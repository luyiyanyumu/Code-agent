"""改动溯源（M18 · P6）：这个文件/符号"被谁在什么时候因为什么改过"。

线索本来就散在三个地方，只是没人把它们接起来：

| 来源 | 回答什么 | 落在哪 |
|---|---|---|
| 改动台账（P2） | 每一次精确编辑：谁改的（AI/编辑器）、改前改后 sha1、是否被回滚 | `patches.db` |
| 任务/批次（P4/P5） | 这次改动属于哪个需求：任务标题、计划目标、审查结论、是否落地 | `tasks` / `task_batches` |
| 决策线程（P6） | 当时为什么这么定、接受了什么风险 | `decisions.jsonl` |
| git 历史 | 最终进了主干的那几笔提交（消息/作者/时间） | 仓库（**只读**） |

于是"这段代码为什么长这样"变成一条可按时间读的线，而不是靠猜。
刻意不做：不推断"谁写的"（作者只有台账与 git 记录里有的才写），不合并同一次任务的
多次编辑（它们是不同的动作，分开看才知道中间发生了什么）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_agent.codeintel.vcs import run_git

#: 一次最多返回多少条（时间线是给人读的，不是给人翻的）
MAX_ITEMS = 80


def _patch_items(index: Any, project: dict, *, path: str, tree_id: str, limit: int) -> list[dict]:
    try:
        payload = index.list_patches(project, limit=limit, path=path or None)
        rows = payload.get("patches") or []
    except Exception:  # noqa: BLE001 —— 台账不可用不该让溯源整体失败
        return []
    out: list[dict] = []
    for row in rows:
        tool = row.get("tool") or ""
        who = "AI" if tool in ("edit_file", "write_file") else ("编辑器" if tool == "editor" else tool or "未知")
        out.append(
            {
                "ts": row.get("ts") or "",
                "source": "patch",
                "title": f"{who}改动（{tool or '-'}）",
                "detail": f"{row.get('before_len') or 0} → {row.get('after_len') or 0} 字节",
                "who": who,
                "path": row.get("path") or "",
                "reverted": bool(row.get("reverted_at")),
                "refs": {
                    "patch_id": row.get("id") or "",
                    "session_id": row.get("session_id") or "",
                    "before_sha1": row.get("before_sha1") or "",
                    "after_sha1": row.get("after_sha1") or "",
                },
            }
        )
    return out


def _task_items(tasks: list[dict], *, path: str, symbol_id: str) -> list[dict]:
    """任务/批次：命中"审查里的改动文件"或"符号差异里出现过这个符号"。"""
    out: list[dict] = []
    for row in tasks:
        try:
            review = json.loads(row.get("review_json") or "{}")
        except json.JSONDecodeError:
            review = {}
        if not isinstance(review, dict):
            review = {}
        files = [str(f) for f in (review.get("changed_files") or [])]
        symbols = review.get("symbols") or {}
        hit_symbol = bool(symbol_id) and any(
            (item.get("id") == symbol_id)
            for key in ("added", "modified", "removed")
            for item in (symbols.get(key) or [])
        )
        hit_file = bool(path) and path in files
        if not (hit_file or hit_symbol):
            continue
        plan = {}
        try:
            plan = json.loads(row.get("plan_json") or "{}")
        except json.JSONDecodeError:
            plan = {}
        gate = review.get("gate") or {}
        status = row.get("status") or ""
        out.append(
            {
                "ts": row.get("merged_at") or row.get("updated_at") or row.get("created_at") or "",
                "source": "batch" if row.get("batch_id") else "task",
                "title": f"任务「{row.get('title') or row.get('requirement','')[:40]}」（{status}）",
                "detail": (plan.get("goal") if isinstance(plan, dict) else "") or row.get("requirement") or "",
                "who": "AI 任务",
                "path": path,
                "reverted": False,
                "refs": {
                    "task_id": row.get("id") or "",
                    "batch_id": row.get("batch_id") or "",
                    "lane": row.get("lane") or "",
                    "session_id": row.get("session_id") or "",
                    "branch": row.get("branch") or "",
                    "gate_ok": (gate.get("ok") if isinstance(gate, dict) else None),
                },
                "matched": "symbol" if hit_symbol else "file",
            }
        )
    return out


def _decision_items(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in items:
        kind = item.get("kind") or "note"
        out.append(
            {
                "ts": item.get("ts") or "",
                "source": "decision",
                "title": f"{kind}：{str(item.get('text') or '')[:60]}",
                "detail": item.get("text") or "",
                "who": item.get("author") or "",
                "path": item.get("target") or "",
                "reverted": False,
                "refs": {
                    "decision_id": item.get("id") or "",
                    "kind": kind,
                    "task_id": item.get("task_id") or "",
                    "session_id": item.get("session_id") or "",
                },
            }
        )
    return out


async def _git_items(root: Path, *, path: str, limit: int) -> list[dict]:
    """该文件的提交历史（只读）。git 不在/不是仓库时返回空，绝不让溯源报错。"""
    if not path:
        return []
    code, out = await run_git(
        root,
        ["log", f"-{max(1, min(limit, 30))}", "--date=iso-strict", "--pretty=%h|%ad|%an|%s", "--", path],
        timeout=15.0,
    )
    if code != 0 or not out.strip():
        return []
    items: list[dict] = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        items.append(
            {
                "ts": parts[1].strip(),
                "source": "commit",
                "title": f"提交 {parts[0]}：{parts[3].strip()[:80]}",
                "detail": "",
                "who": parts[2].strip(),
                "path": path,
                "reverted": False,
                "refs": {"commit": parts[0].strip()},
            }
        )
    return items


async def timeline(
    *,
    project: dict,
    index: Any,
    tasks: Any = None,
    decisions: Any = None,
    path: str = "",
    symbol_id: str = "",
    tree_id: str = "main",
    limit: int = MAX_ITEMS,
) -> dict:
    """把四个来源合到一条时间线上（按时间倒序）。"""
    rel = str(path or "").replace("\\", "/").strip("/")
    if not rel and symbol_id:
        row = None
        try:
            row = index.symbol_detail(project, str(symbol_id), tree_id=tree_id)
        except Exception:  # noqa: BLE001 —— 允许传入裸 SymbolIndex（只有 symbol()）
            getter = getattr(index, "symbol", None)
            if getter is not None:
                try:
                    row = getter(str(symbol_id))
                except Exception:  # noqa: BLE001
                    row = None
        if isinstance(row, dict):
            rel = str(row.get("path") or "")
    items: list[dict] = []
    items += _patch_items(index, project, path=rel, tree_id=tree_id, limit=limit)
    if tasks is not None and rel:
        try:
            rows = tasks.list_for_project(project["id"], limit=60)
        except Exception:  # noqa: BLE001
            rows = []
        items += _task_items(rows, path=rel, symbol_id=str(symbol_id or ""))
    if decisions is not None:
        target = str(symbol_id or (f"file:{rel}" if rel else ""))
        if target:
            try:
                items += _decision_items(decisions.list(target=target, limit=30, prefix=not symbol_id))
            except Exception:  # noqa: BLE001
                pass
    try:
        items += await _git_items(Path(project["root_path"]), path=rel, limit=20)
    except Exception:  # noqa: BLE001
        pass

    items.sort(key=lambda i: str(i.get("ts") or ""), reverse=True)
    sources: dict[str, int] = {}
    for item in items:
        sources[item["source"]] = sources.get(item["source"], 0) + 1
    return {
        "path": rel,
        "symbol_id": str(symbol_id or ""),
        "items": items[:limit],
        "counts": sources,
        "total": len(items),
    }
