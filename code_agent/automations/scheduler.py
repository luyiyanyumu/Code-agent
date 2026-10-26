"""自动化调度器（s23）：asyncio 周期 tick + 到期触发。

设计：
- tick 每 20s 扫一次 `due()`，触发即先写回 next_run_at 再执行（防重复触发）
- 执行走 SessionManager 的 **strict runtime**：审批一律 fail closed（拒绝），
  自动化无人值守，任何需要人工确认的操作都直接拒绝——安全不变量不因自动化放宽
- 一次型执行完置 completed；recurring 算下一次时间，算不出则暂停并留痕
- 单个任务异常不拖垮调度循环
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from code_agent.automations.rrule_lite import next_occurrence
from code_agent.storage.db import AutomationRepo

logger = logging.getLogger(__name__)

TICK_SECONDS = 20


class Scheduler:
    def __init__(self, automations: AutomationRepo, submit_fn, anchor_by_user: dict[str, str] | None = None):
        """
        submit_fn: async (user_id, name, prompt) -> None —— 由 app 层注入，
        负责为自动化创建 strict 会话并提交 prompt。
        """
        self.automations = automations
        self._submit = submit_fn
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("scheduler tick 异常（忽略，下轮继续）")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        now_s = now.isoformat(timespec="seconds")
        due_rows = self.automations.due(now_s)
        if not due_rows:
            return
        # 先推下一次触发时间/收尾状态（防重复触发），再并发执行
        tasks = []
        for row in due_rows:
            next_at = self._advance(row, now)
            tasks.append(asyncio.create_task(self._run_safely(row, next_at)))
        await asyncio.gather(*tasks)

    def _advance(self, row: dict, now: datetime) -> str | None:
        """计算并写回下一次触发时间；once 或算不出 -> completed/paused。"""
        if row["schedule_type"] == "once" or not row["rrule"]:
            self.automations.mark_run(row["id"], None, status="completed")
            return None
        try:
            anchor = datetime.fromisoformat(row["created_at"])
            nxt = next_occurrence(row["rrule"], now, anchor)
        except ValueError:
            nxt = None
        if nxt is None:
            logger.warning("automation %s rrule 无法推进，暂停: %r", row["id"], row["rrule"])
            self.automations.mark_run(row["id"], None, status="paused")
            return None
        nxt_s = nxt.isoformat(timespec="seconds")
        self.automations.mark_run(row["id"], nxt_s, status="active")
        return nxt_s

    async def _run_safely(self, row: dict, _next_at: str | None) -> None:
        try:
            await self._submit(row["user_id"], row["name"], row["prompt"])
        except Exception:
            logger.exception("automation %s 执行失败", row["id"])


def compute_initial_next_run(
    schedule_type: str, rrule: str | None, scheduled_at: str | None, created_at: str
) -> str | None:
    """创建自动化时计算首次触发时间（app 层 CRUD 用）。"""
    now = datetime.now(timezone.utc)
    if schedule_type == "once":
        return scheduled_at
    if not rrule:
        return None
    try:
        anchor = datetime.fromisoformat(scheduled_at or created_at)
    except ValueError:
        return None
    nxt = next_occurrence(rrule, now, anchor)
    return nxt.isoformat(timespec="seconds") if nxt else None
