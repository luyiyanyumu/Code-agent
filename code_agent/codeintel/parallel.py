"""代码模式并行批次（M18 · P5）：一个需求 → N 条互不重叠的车道 → 串行合并。

为什么要"并行"：一个稍大的需求改一个文件是不够的，而 AI 串行改 6 个文件时，
前 5 个的等待时间全花在"排队"上。把需求拆成若干**互不重叠**的子任务，
每条在自己的隔离工作树里同时推进，总耗时接近最慢的那一条。

三条不能让步的规则（否则并行就是灾难）：

1. **写租约（lease）**：一个文件在任意时刻只能有一条车道拥有。同一个文件被两条
   车道声明时，后声明的那条必须**等前一条合并完**再开始——不是"提示一下"，而是
   按依赖顺序执行；否则两个分支各自改同一个文件，后合并的那次要么冲突，
   要么用 -X theirs 之类的做法**静默丢掉一条车道的成果**。
2. **串行合并队列**：并行执行，**串行落地**。主干是共享资源，合并永远一次一条，
   按计划顺序进队列；任何一条合并失败立刻 `merge --abort` 把主干恢复原状，
   并停下队列（主干处于半合并状态时继续合并下一条是最危险的操作）。
3. **车道即任务**：每条车道就是一条 P4 任务（自己的 worktree/分支/审查/合并）。
   并行编排只负责拆解、租约、调度与队列，不另起一套写代码的机制。

刻意不做：不做跨车道的自动冲突解决（那需要"谁对"的判断，属于人），
不做 rebase 式同步（rebase 被平台硬拒，且改写历史不是这里的必要手段），
不做"拆不出来就硬拆"（拆解失败如实显示，让人补计划）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from code_agent.codeintel import paths as P
from code_agent.codeintel.tasks import worktree_of
from code_agent.settings import task_permission_mode

#: 一次批次最多几条车道。再多也不快：模型侧的规划质量与合并队列长度都会变差。
MAX_LANES = 6
#: 默认同时执行的车道数（可用 CODE_AGENT_CODE_PARALLEL 覆盖）
DEFAULT_WORKERS = 3

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

BATCH_PROMPT = """你是并行改造的调度者。请把需求拆成 **%d 条以内的互不重叠车道**，只输出计划，不要改文件。

硬约束：
1. **同一文件只能出现在一条车道里**。做不到就把相关改动并进同一条车道（车道数量少一点没关系）。
2. 每条车道必须能独立验证（有自己的验证命令）。
3. 车道的依赖关系要说明：确实需要前置改动的，用 `after` 指向前置车道的 id。
4. 先用 code_map / grep 定位文件与符号，files 必须是真实存在的相对路径。

只输出一个 JSON 对象（放在 ```json 代码块里）：
```json
{
  "goal": "整体目标（一句话）",
  "lanes": [
    {
      "id": "L1",
      "title": "这条车道做什么",
      "files": ["相对项目根的路径"],
      "symbol_ids": ["sym:<path>#<限定名>"],
      "risk": "low|medium|high",
      "verify": ["验证命令"],
      "after": ["前置车道 id"],
      "notes": "实现要点"
    }
  ],
  "notes": "整体说明与取舍"
}
```

用户需求：
%s
"""


class BatchError(Exception):
    """批次操作失败（对用户可见的原因）。"""


# ---------------------------------------------------------------------------
# 纯函数：计划归一化 / 写租约 / 执行波次（不依赖 git、不依赖模型，可单测）
# ---------------------------------------------------------------------------
def _clean_lane(item: dict, index: int) -> dict:
    """归一化一条车道：路径按项目相对路径处理（`./a.py` 与 `a.py` 是同一个文件）。"""
    files: list[str] = []
    for raw in item.get("files") or []:
        text = str(raw).strip()
        if not text:
            continue
        with contextlib.suppress(P.ProjectPathError):   # `..` 之类：丢掉这一条声明即可
            files.append(P.normalize_rel(text))
    symbols = [str(s) for s in (item.get("symbol_ids") or []) if str(s).strip()]
    verify = [str(v) for v in (item.get("verify") or []) if str(v).strip()]
    after = [str(a) for a in (item.get("after") or []) if str(a).strip()]
    return {
        "id": str(item.get("id") or f"L{index + 1}")[:30],
        "title": str(item.get("title") or "")[:200],
        "files": sorted({f for f in files if f})[:40],
        "symbol_ids": symbols[:60],
        "risk": str(item.get("risk") or "medium")[:10],
        "verify": verify[:5],
        "after": after[:6],
        "notes": str(item.get("notes") or "")[:1000],
    }


def _has_worktree(row: dict) -> bool:
    """车道是否已经建好工作树（判空必须显式：`Path("")` 是 `Path(".")`）。"""
    return worktree_of(row) is not None


def extract_batch_plan(text: str, *, max_lanes: int = MAX_LANES) -> dict | None:
    """从模型输出里抽车道计划。

    同时接受 `lanes` 与 `subtasks` 两种键名：单任务的 Plan 形状直接拿来当
    "一条车道的批次"也是合理的降级（模型没按新格式输出时不必重跑一次）。
    """
    candidate = ""
    match = _FENCE.search(text or "")
    if match:
        candidate = match.group(1)
    else:
        start = (text or "").find("{")
        end = (text or "").rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("lanes")
    if not isinstance(raw, list):
        raw = data.get("subtasks")
    if not isinstance(raw, list):
        return None
    lanes: list[dict] = []
    for item in raw[:max_lanes]:
        if isinstance(item, dict):
            lanes.append(_clean_lane(item, len(lanes)))
    if not lanes:
        return None
    return {
        "goal": str(data.get("goal") or "")[:500],
        "lanes": lanes,
        "notes": str(data.get("notes") or "")[:2000],
    }


def compute_leases(lanes: list[dict]) -> dict:
    """写租约：谁拥有哪个文件、谁必须等谁。

    返回值：
        leases   每条车道真正"独占"的文件（被前面车道占住的不在其中）
        conflicts 重叠清单（path + 涉及的车道 + 先到者），界面直接把这段摆出来
        wait_for 车道 → 必须等它合并完的前置车道（含显式 `after`）
        lane_ids 计划顺序（也是合并队列顺序）
    """
    owner: dict[str, str] = {}
    leases: list[dict] = []
    conflicts: list[dict] = []
    order: list[str] = []
    ids: list[str] = []
    for lane in lanes:
        lid = str(lane.get("id") or "")
        if lid and lid not in ids:
            ids.append(lid)
    for lane in lanes:
        lid = str(lane.get("id") or "")
        if lid not in ids:
            continue
        order.append(lid)
        wait: list[str] = [a for a in (lane.get("after") or []) if a in ids and a != lid]
        mine: list[str] = []
        for path in lane.get("files") or []:
            holder = owner.get(path)
            if holder is None:
                owner[path] = lid
                mine.append(path)
            else:
                if holder not in wait:
                    wait.append(holder)
                conflicts.append({"path": path, "lanes": [holder, lid], "winner": holder})
        leases.append({"lane": lid, "files": mine, "wait_for": wait})
    return {"leases": leases, "conflicts": conflicts, "wait_for": {l["lane"]: l["wait_for"] for l in leases},
            "lane_ids": order, "owners": owner}


def waves_of(lane_ids: list[str], wait_for: dict[str, list[str]]) -> list[list[str]]:
    """把车道分成执行波次：同一波内互不依赖 → 可以同时跑。

    依赖环（A 等 B、B 等 A）无法排波次，此时退化成"全部串行"并把环暴露出来——
    宁可慢，也不能让两条车道同时写同一个文件。
    """
    placed: list[str] = []
    result: list[list[str]] = []
    remaining = list(lane_ids)
    while remaining:
        ready = [
            lid for lid in remaining
            if all(dep in placed or dep not in remaining for dep in (wait_for.get(lid) or []))
        ]
        if not ready:                      # 依赖成环：剩下的全部按原顺序串行
            result.append(list(remaining))
            break
        result.append(ready)
        placed.extend(ready)
        remaining = [lid for lid in remaining if lid not in ready]
    return result


# ---------------------------------------------------------------------------
# 编排服务
# ---------------------------------------------------------------------------
PromptRunner = Callable[[dict, dict, str, str], Awaitable[dict]]


class BatchService:
    """并行批次的生命周期（拆解 / 租约 / 并发执行 / 串行合并）。"""

    def __init__(
        self,
        *,
        project_repo: Any,
        batches: Any,
        tasks: Any,
        task_service: Any,
        sessions: Any = None,
        runner: PromptRunner | None = None,
    ) -> None:
        self.projects = project_repo
        self.batches = batches
        self.tasks = tasks
        self.task_service = task_service
        self.sessions = task_service.sessions or sessions
        if task_service.sessions is None and self.sessions is not None:
            task_service.sessions = self.sessions
        #: 测试注入用：替代"在工作树里跑一次 agent"。签名 (project, task, prompt, mode) -> dict
        self.runner = runner
        self._merge_locks: dict[str, asyncio.Lock] = {}

    def _assert_planner_idle(self, project: dict, batch: dict) -> None:
        manager = self.task_service.sessions or self.sessions
        team = getattr(manager, "agent_team", None)
        if team is not None:
            try:
                team.assert_batch_planner_idle(project["id"], batch["user_id"], batch["id"])
            except (ValueError, KeyError) as exc:
                raise BatchError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    @staticmethod
    def max_lanes() -> int:
        try:
            return max(1, min(MAX_LANES, int(os.environ.get("CODE_AGENT_CODE_LANES", str(MAX_LANES)))))
        except ValueError:
            return MAX_LANES

    @staticmethod
    def max_workers() -> int:
        try:
            return max(1, int(os.environ.get("CODE_AGENT_CODE_PARALLEL", str(DEFAULT_WORKERS))))
        except ValueError:
            return DEFAULT_WORKERS

    def batch_prompt(self, requirement: str, *, max_lanes: int = 0) -> str:
        return BATCH_PROMPT % (max_lanes or self.max_lanes(), requirement)

    def plan_of(self, batch: dict) -> dict:
        try:
            data = json.loads(batch.get("plan_json") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    async def plan_with_model(self, project: dict, batch: dict) -> dict:
        """让模型给出车道拆分（只读会话；后续由用户批准）。"""
        prompt = self.batch_prompt(batch["requirement"])
        try:
            text = await self._ask(project, batch, prompt)
        except Exception as exc:  # noqa: BLE001
            self.batches.update(batch["id"], status="plan_failed", error=f"计划阶段失败：{exc}")
            return {"ok": False, "status": "plan_failed", "error": str(exc)}
        plan = extract_batch_plan(text, max_lanes=self.max_lanes())
        if plan is None:
            self.batches.update(
                batch["id"],
                status="plan_failed",
                error="没能从模型输出里解析出车道计划（可手动填写车道后执行）",
            )
            return {"ok": False, "status": "plan_failed", "raw": (text or "")[:4000]}
        self.set_plan(batch, plan)
        # 拆完立刻建车道工作树：计划不给完就不该占着工作树，但拆完了就该能直接跑
        await self.ensure_repo(project, git_init=False, user_id=batch["user_id"])
        await self._materialize(project, self.batches.get(batch["id"]) or batch,
                                user_id=batch["user_id"])
        return {"ok": True, "status": "planned", "plan": plan}

    async def _ask(self, project: dict, batch: dict, prompt: str) -> str:
        """跑一次只读规划。批次的规划发生在**主干**上（只读，不碰文件）。"""
        manager = self.task_service.sessions or self.sessions
        team = getattr(manager, "agent_team", None)
        if team is not None:
            result = await team.run_batch_planner(project["id"], batch["user_id"], batch["id"], prompt)
            self.task_service.require_completed(result)
            return str(result.get("text") or "")
        if self.runner is not None:
            result = await self.runner(project, {}, prompt, "read-only")
            return str(result.get("text") or "")
        if manager is None:
            raise BatchError("会话管理器不可用：请手工填写车道计划")
        session_id = project.get("session_id") or ""
        if not session_id:
            raise BatchError("项目主会话还没建立：先打开一次代码页，或手工填写车道计划")
        runtime = manager.get_runtime(session_id, batch["user_id"])
        result = await runtime.submit(prompt)
        return str(result.text or "")

    def set_plan(self, batch: dict, plan: dict) -> dict:
        """落计划并**为每条车道建隔离工作树**（车道即任务）。"""
        lanes = [_clean_lane(dict(lane), i) for i, lane in enumerate(plan.get("lanes") or [])]
        lanes = lanes[: self.max_lanes()]
        if not lanes:
            raise BatchError("车道计划为空：至少需要一条车道")
        normalized = {"goal": str(plan.get("goal") or "")[:500], "lanes": lanes,
                      "notes": str(plan.get("notes") or "")[:2000]}
        self.batches.update(
            batch["id"], plan_json=json.dumps(normalized, ensure_ascii=False), status="planned", error=""
        )
        return normalized

    async def create(
        self,
        project: dict,
        *,
        user_id: str,
        requirement: str,
        title: str = "",
        base_branch: str = "",
        git_init: bool = False,
        plan: dict | None = None,
    ) -> dict:
        """建批次：落库 → （可选）落计划 → 为可并行车道建工作树。

        与前置车道抢同一个文件的车道不会被立刻建树（状态 `deferred`）：
        它要等前面那条落地后，从新的 HEAD 出发再执行（见 merge_queue）。
        """
        requirement = (requirement or "").strip()
        if not requirement:
            raise BatchError("需求描述不能为空")
        row = self.batches.create(
            project_id=project["id"],
            user_id=user_id,
            title=title or requirement[:60],
            requirement=requirement,
            base_branch=base_branch,
            status="planning",
        )
        # 并行车道全都依赖 git worktree：不是仓库就在这里明确失败，
        # 而不是等第一条车道建到一半才发现（那时已经留下了半批工作树）。
        await self.ensure_repo(project, git_init=git_init, user_id=user_id)
        if plan and plan.get("lanes"):
            self.set_plan(row, plan)
            await self._materialize(project, self.batches.get(row["id"]) or row, user_id=user_id)
        return self.batches.get(row["id"]) or row

    async def ensure_repo(self, project: dict, *, git_init: bool, user_id: str) -> None:
        """确认项目是 git 仓库：不是就按用户选择初始化（复用任务层的同一套判断）。"""
        root = Path(project["root_path"])
        code, out = await self.task_service._git(root, ["rev-parse", "--is-inside-work-tree"])
        if code == 0 and out.strip() == "true":
            return
        if not git_init:
            raise BatchError(
                "该项目不是 git 仓库：并行车道依赖隔离工作树（git worktree）。"
                "请选择「初始化 git 并提交当前代码」，或先手动 git init。"
            )
        await self.task_service._ensure_repo(root)

    async def _materialize(self, project: dict, batch: dict, *, user_id: str) -> list[dict]:
        """按计划把车道变成任务。

        - **互不重叠的车道**：立刻建工作树（可以并行跑）；
        - **有依赖的车道**（和前面的车道抢同一个文件，或显式声明了 after）：
          只落库不建工作树（状态 `deferred`）。它必须等前置车道**落地之后**
          再从新的 HEAD 拉分支——两条车道各自基于旧 HEAD 改同一个文件时，
          后合并的那条一定冲突，这是"工作树起点"的问题，串行执行救不了。
        """
        plan = self.plan_of(batch)
        existing = {row.get("lane") or "": row for row in self.tasks.list_for_batch(batch["id"])}
        lease = compute_leases(plan.get("lanes") or [])
        wait_map = lease.get("wait_for") or {}
        lanes = plan.get("lanes") or []
        for lane in lanes:
            if lane["id"] in existing:
                continue
            subtask = {"goal": plan.get("goal") or batch["title"], "subtasks": [lane],
                       "notes": plan.get("notes") or "",
                       "files": lane["files"]}
            title = f"{lane['id']} · {lane['title'] or batch['title']}"[:60]
            waits = wait_map.get(lane["id"]) or []
            if waits:
                row = self.tasks.create(
                    project_id=project["id"], user_id=user_id, title=title,
                    requirement=f"[{lane['id']}] {lane['title'] or batch['requirement']}",
                    base_branch=batch.get("base_branch") or "", branch="", worktree="",
                    status="deferred", batch_id=batch["id"], lane=lane["id"],
                )
                self.tasks.update(row["id"], plan_json=json.dumps(subtask, ensure_ascii=False))
                continue
            task = await self.task_service.create(
                project,
                user_id=user_id,
                requirement=f"[{lane['id']}] {lane['title'] or batch['requirement']}",
                title=title,
                base_branch=batch.get("base_branch") or "",
                git_init=False,          # 仓库已在 _ensure_repo 里确认过
            )
            self.tasks.update(task["id"], plan_json=json.dumps(subtask, ensure_ascii=False))
            self.tasks.update(task["id"], status="planned", batch_id=batch["id"], lane=lane["id"])
        return self.tasks.list_for_batch(batch["id"])

    async def replace_plan(self, project: dict, batch: dict, plan: dict) -> dict:
        """手改车道拆分：新增车道建工作树，被删掉的车道放弃（工作树/分支一起回收）。

        为什么必须跟着同步车道：租约与合并队列都是从**计划**推出来的。
        计划里删掉一条车道而它的工作树还在，就会出现"计划外的写者"——
        它不在队列里、不受租约约束，却随时可能被合并。
        """
        self._assert_planner_idle(project, batch)
        status = batch.get("status") or ""
        if status in ("merged", "discarded", "merging"):
            raise BatchError(f"批次当前状态（{status}）不能再改拆分")
        current = self.plan_of(batch)
        old_ids = [lane["id"] for lane in (current.get("lanes") or [])]
        rows = {row.get("lane") or "": row for row in self.tasks.list_for_batch(batch["id"])}
        # A running lane must not vanish from the dependency plan while its
        # worktree is still owned by an executing (possibly recursive) worker.
        for row in rows.values():
            if row.get("status") not in ("merged", "discarded"):
                self.task_service._assert_task_idle(project, row)
        normalized = self.set_plan(batch, plan)
        new_ids = [lane["id"] for lane in normalized["lanes"]]
        fresh = self.batches.get(batch["id"]) or batch
        # 先回收被删掉的车道（必须在建新车道前，否则分支/工作树会互相打架）
        for lane_id in old_ids:
            if lane_id in new_ids:
                continue
            row = rows.get(lane_id)
            if row is None or (row.get("status") or "") in ("merged", "discarded"):
                continue
            try:
                await self.task_service.discard(project, row)
            except Exception as exc:  # noqa: BLE001
                self.batches.update(batch["id"], plan_json=batch.get("plan_json") or "{}", status=status,
                                    error=f"修改拆分失败，原计划已保留：{exc}")
                raise BatchError(f"回收移除的车道失败：{exc}") from exc
        if any(lid not in rows for lid in new_ids):
            await self._materialize(project, fresh, user_id=batch["user_id"])
        return self.batches.get(batch["id"]) or fresh

    def leases(self, batch: dict) -> dict:
        """当前计划下的写租约（含冲突与等待关系）。"""
        return compute_leases((self.plan_of(batch).get("lanes") or []))

    # ------------------------------------------------------------------
    # 执行（并行）
    # ------------------------------------------------------------------
    async def run(self, project: dict, batch: dict, *, max_workers: int = 0, stop_on_failure: bool = False) -> dict:
        """并发执行所有车道：按波次推进，同波内同时跑。

        波次由写租约决定：等待前置车道（因为它要用同一个文件）的车道不在同一波里，
        因此"同一文件同时被两条车道写"在调度层就不可能发生。
        """
        self._assert_planner_idle(project, batch)
        lanes = self.tasks.list_for_batch(batch["id"])
        if not lanes:
            raise BatchError("批次里还没有车道：先落计划（会自动建隔离工作树）")
        plan = self.plan_of(batch)
        order = [lane["id"] for lane in (plan.get("lanes") or [])]
        lease_info = self.leases(batch)
        wait_map = {k: v for k, v in (lease_info.get("wait_for") or {}).items()}
        lane_by_id = {row.get("lane") or "": row for row in lanes}
        ids = [lid for lid in order if lid in lane_by_id] or list(lane_by_id)
        # 有依赖的车道此刻还没有工作树（要等前置落地后才建），本轮只执行独立的那些
        runnable = [lid for lid in ids if _has_worktree(lane_by_id[lid])]
        deferred = [lid for lid in ids if lid not in runnable]
        limit = max(1, max_workers or self.max_workers())
        team = getattr(self.task_service.sessions, "agent_team", None)
        if team is not None:
            limit = min(limit, team.limits()["max_parallel"])
        for lane_id in runnable:
            self.task_service._assert_task_idle(project, lane_by_id[lane_id])
        sem = asyncio.Semaphore(limit)
        self.batches.update(batch["id"], status="running", error="")
        results: list[dict] = [
            {"lane": lid, "task_id": lane_by_id[lid]["id"], "ok": True, "deferred": True,
             "reason": f"等 {'、'.join(wait_map.get(lid) or []) or '前置车道'} 落地后自动执行（同一文件不能两条车道同时改）"}
            for lid in deferred
        ]
        active = 0
        peak = 0

        async def _one(lane_id: str) -> dict:
            nonlocal active, peak
            row = lane_by_id[lane_id]
            async with sem:
                active += 1
                peak = max(peak, active)
                completed = False
                try:
                    self.tasks.update(row["id"], status="running", error="")
                    prompt = self.task_service.worker_prompt(self.task_service.plan_of(row))
                    outcome = await self._run_lane(project, row, prompt)
                    self.task_service.require_completed(outcome)
                    completed = True
                    fresh = self.tasks.get(row["id"]) or row
                    stored = self.task_service.review_of(fresh)
                    stored["execution_outcome"] = outcome
                    self.tasks.update(row["id"], review_json=json.dumps(stored, ensure_ascii=False))
                    review = await self.task_service.review(project, self.tasks.get(row["id"]) or row)
                    return {
                        "lane": lane_id, "task_id": row["id"], "ok": True,
                        "stop_reason": outcome.get("stop_reason") or "",
                        "turns": outcome.get("turns") or 0,
                        "counts": (review.get("symbols") or {}).get("counts") or {},
                        "plan_scope_ok": review.get("plan_scope_ok", None),
                    }
                except Exception as exc:  # noqa: BLE001 —— 一条车道失败不该带走整批
                    self.tasks.update(row["id"], status="review" if completed else "failed",
                                      error=f"{'执行者本轮已结束，审查尚未完成，可稍后刷新' if completed else '执行失败'}：{exc}")
                    return {"lane": lane_id, "task_id": row["id"], "ok": False,
                            "review_pending": completed, "error": str(exc)}
                finally:
                    active -= 1

        waves = waves_of(runnable, wait_map)
        for wave in waves:
            batch_results = await asyncio.gather(*[_one(lid) for lid in wave])
            results.extend(batch_results)
            if stop_on_failure and any(not r["ok"] for r in batch_results):
                break

        ok = all(r["ok"] for r in results)
        failed = any(not r["ok"] and not r.get("review_pending") for r in results)
        self.batches.update(batch["id"], status="failed" if failed else "review",
                            error="" if ok else "有车道执行失败：见各车道状态" if failed else "执行者本轮已结束，部分车道仍在等待审查")
        return {
            "ok": ok, "waves": waves, "max_concurrent": peak,
            "workers": limit, "lanes": results, "deferred": deferred,
        }

    async def _run_lane(self, project: dict, task: dict, prompt: str) -> dict:
        # 档位跟随全局默认（下限 workspace-write），**不硬编码**：写死 workspace-write
        # 会让"用户选完全权限"对车道执行无效，表现就是并行任务里每条命令都要审批。
        mode = task_permission_mode()
        team = getattr(self.task_service.sessions, "agent_team", None)
        if self.runner is not None and team is None:
            return await self.runner(project, task, prompt, mode)
        return await self.task_service.run_agent(
            project, task, prompt,
            permission_mode=mode,
        )

    # ------------------------------------------------------------------
    # 串行合并队列
    # ------------------------------------------------------------------
    async def merge_queue(
        self,
        project: dict,
        batch: dict,
        *,
        lanes: list[str] | None = None,
        stop_on_conflict: bool = True,
        force: bool = False,
        reason: str = "",
    ) -> dict:
        """串行合并：一次一条，失败即 abort 并停队列。

        冲突车道（`deferred`）在这里被**建树 → 执行 → 审查 → 合并**：它等到了
        前置改动已经进主干，所以从新的 HEAD 出发不会再撞车。整批的落地顺序因此
        始终是"计划顺序"，而不是"谁先跑完谁先进"。
        """
        self._assert_planner_idle(project, batch)
        plan = self.plan_of(batch)
        order = [lane["id"] for lane in (plan.get("lanes") or [])]
        rows = {row.get("lane") or "": row for row in self.tasks.list_for_batch(batch["id"])}
        wanted = [lid for lid in order if lid in rows]
        if lanes:
            wanted = [lid for lid in wanted if lid in set(lanes)]
        if not wanted:
            raise BatchError("没有可合并的车道")
        for lane_id in wanted:
            if rows[lane_id].get("status") not in ("merged", "discarded"):
                self.task_service._assert_task_idle(project, rows[lane_id])

        lock = self._merge_locks.setdefault(project["id"], asyncio.Lock())
        log: list[dict] = []
        merged: list[str] = []
        self.batches.update(batch["id"], status="merging", error="")
        async with lock:
            for lane_id in wanted:
                row = self.tasks.get(rows[lane_id]["id"]) or rows[lane_id]
                if (row.get("status") or "") in ("merged", "discarded"):
                    log.append({"lane": lane_id, "queued": False, "reason": f"状态 {row['status']}，跳过"})
                    continue
                entry: dict[str, Any] = {"lane": lane_id, "queued": False}
                executing_deferred = False
                try:
                    if not _has_worktree(row):
                        # 冲突车道：**现在**才建工作树（起点是已落地的最新 HEAD），
                        # 跑完再合并——它就是"串行流水线"，不是"先并行再撞车"。
                        entry["deferred_run"] = True
                        executing_deferred = True
                        row = await self.task_service.attach_worktree(
                            project, row, base_branch="", from_head=True
                        )
                        self.tasks.update(row["id"], status="running", error="")
                        prompt = self.task_service.worker_prompt(self.task_service.plan_of(row))
                        outcome = await self._run_lane(project, row, prompt)
                        self.task_service.require_completed(outcome)
                        await self.task_service.review(project, self.tasks.get(row["id"]) or row)
                        executing_deferred = False
                    result = await self.task_service.merge(
                        project, self.tasks.get(row["id"]) or row, force=force, reason=reason
                    )
                except Exception as exc:  # noqa: BLE001
                    await self._abort_merge(project)
                    self.tasks.update(row["id"], status="failed" if executing_deferred else "conflict",
                                      error=f"{'执行失败' if executing_deferred else '合并冲突'}：{exc}")
                    entry["error"] = str(exc)
                    log.append(entry)
                    if stop_on_conflict:
                        break
                    continue
                merged.append(lane_id)
                entry["queued"] = True
                entry["files"] = result.get("files") or []
                log.append(entry)

        remaining = [lid for lid in wanted if lid not in merged]
        if not remaining:
            status = "merged"
        elif merged:
            status = "partial"
        else:
            status = "failed"
        self.batches.update(
            batch["id"],
            status=status,
            error="" if status == "merged" else "有车道没有落地：见队列日志与车道状态",
            merged_at=datetime.now(timezone.utc).isoformat(timespec="seconds") if merged else None,
        )
        return {"ok": status == "merged", "status": status, "merged": merged,
                "remaining": remaining, "queue": wanted, "log": log}

    async def _abort_merge(self, project: dict) -> bool:
        """把主干从"半合并"状态里救出来。

        合并冲突时 git 会留下 MERGE_HEAD 与冲突标记；不 abort 就继续做别的事，
        用户看到的是一个"莫名其妙带着冲突标记"的工作区。这条是**兜底**：
        失败也不抛，原因写进批次错误里。
        """
        root = Path(project["root_path"])
        with_errors: list[str] = []
        try:
            code, head = await self.task_service._git(root, ["rev-parse", "-q", "--verify", "MERGE_HEAD"])
            if code == 0:
                code, out = await self.task_service._git(root, ["merge", "--abort"])
                if code != 0:
                    with_errors.append(out[:200])
        except Exception as exc:  # noqa: BLE001
            with_errors.append(str(exc))
        return not with_errors

    # ------------------------------------------------------------------
    # 视图 / 放弃
    # ------------------------------------------------------------------
    def view(self, batch: dict, lanes: list[dict]) -> dict:
        plan = self.plan_of(batch)
        lease = self.leases(batch)
        rows = [self.lane_view(row, lease) for row in lanes]
        merged = [r for r in rows if r["status"] == "merged"]
        return {
            "id": batch["id"],
            "project_id": batch["project_id"],
            "title": batch.get("title") or "",
            "requirement": batch.get("requirement") or "",
            "status": batch.get("status") or "",
            "base_branch": batch.get("base_branch") or "",
            "error": batch.get("error") or "",
            "created_at": batch.get("created_at") or "",
            "updated_at": batch.get("updated_at") or "",
            "merged_at": batch.get("merged_at") or "",
            "plan": plan,
            "lanes": rows,
            "leases": lease.get("leases") or [],
            "conflicts": lease.get("conflicts") or [],
            "queue": lease.get("lane_ids") or [],
            "waves": waves_of(lease.get("lane_ids") or [], lease.get("wait_for") or {}),
            "progress": {"total": len(rows), "merged": len(merged),
                         "running": len([r for r in rows if r["status"] == "running"]),
                         "review": len([r for r in rows if r["status"] == "review"])},
        }

    def lane_view(self, row: dict, lease: dict) -> dict:
        """车道视图：任务视图 + 租约（独占哪些文件、等谁）。"""
        review = {}
        try:
            review = json.loads(row.get("review_json") or "{}")
        except json.JSONDecodeError:
            review = {}
        info = {l["lane"]: l for l in (lease.get("leases") or [])}.get(row.get("lane") or "", {})
        return {
            "lane": row.get("lane") or "",
            "task_id": row["id"],
            "title": row.get("title") or "",
            "status": row.get("status") or "",
            "branch": row.get("branch") or "",
            "worktree": row.get("worktree") or "",
            "error": row.get("error") or "",
            "files": (self.task_service.plan_of(row).get("files") or []) if self.task_service else [],
            "leased": info.get("files") or [],
            "wait_for": info.get("wait_for") or [],
            "symbol_counts": (review.get("symbols") or {}).get("counts") or {},
            "changed_files": len(review.get("changed_files") or []),
            "plan_scope_ok": review.get("plan_scope_ok", None),
        }

    async def review_all(self, project: dict, batch: dict) -> dict:
        """刷新所有车道的审查快照，并给出整批的符号合计。"""
        rows = self.tasks.list_for_batch(batch["id"])
        total = {"added": 0, "modified": 0, "removed": 0}
        per_lane: list[dict] = []
        for row in rows:
            try:
                review = await self.task_service.review(project, row)
            except Exception as exc:  # noqa: BLE001
                per_lane.append({"lane": row.get("lane"), "error": str(exc)})
                continue
            counts = (review.get("symbols") or {}).get("counts") or {}
            for key in total:
                total[key] += int(counts.get(key) or 0)
            per_lane.append({
                "lane": row.get("lane"), "task_id": row["id"], "counts": counts,
                "changed_files": review.get("changed_files") or [],
                "plan_scope_ok": review.get("plan_scope_ok", None),
            })
        return {"lanes": per_lane, "total": total}

    async def discard(self, project: dict, batch: dict) -> dict:
        """放弃整批：删掉每条车道的工作树与分支，主干一字不动。"""
        self._assert_planner_idle(project, batch)
        rows = self.tasks.list_for_batch(batch["id"])
        for row in rows:
            if row.get("status") not in ("merged", "discarded"):
                self.task_service._assert_task_idle(project, row)
        for row in rows:
            if (row.get("status") or "") in ("merged", "discarded"):
                continue
            try:
                await self.task_service.discard(project, row)
            except Exception as exc:  # noqa: BLE001
                self.batches.update(batch["id"], status="failed", error=f"车道回收失败，未放弃整批：{exc}")
                raise BatchError(f"车道 {row.get('lane') or row['id']} 回收失败：{exc}") from exc
        self.batches.update(batch["id"], status="discarded", error="")
        return {"ok": True, "lanes": [row["id"] for row in rows]}
