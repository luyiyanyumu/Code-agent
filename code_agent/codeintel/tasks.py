"""代码模式任务（M18 · P4）：AI 永远在隔离工作树里改，用户审批后才回主干。

这是"AI 改代码"从"能跑"到"敢用"的那一步，三条不可让步的规则：

1. **强制隔离**：任务在 `git worktree add` 出来的独立工作树 + 独立分支里进行。
   主工作区在整个任务期间保持不变（`workspace-write` 权限只发给工作树会话）。
2. **先计划后动手**：先产出结构化 Plan（目标、子任务、涉及文件/符号、风险、验证命令），
   用户批准才执行；执行后按"改动是否落在 Plan 声明的范围内"给出越界清单。
3. **逐项审查再合并**：审核页展示**符号级**差异（新增/修改/删除的类与函数，而不是只有
   文本 diff），支持按文件选择性应用或整体合并；合并才触碰主干，且会重建索引。

不做的事：不改写历史（rebase/filter-branch 直接拒绝）、不自动 push、
不替用户提交（按文件应用后由用户在源代码管理面板决定提交）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from code_agent.codeintel import graph
from code_agent.codeintel.vcs import check_command, run_git
from code_agent.config import code_dir
from code_agent.settings import task_permission_mode

#: 任务树在索引里的命名：tree:task:<id>
TASK_TREE_PREFIX = "task:"

#: Plan 的 JSON 提取：模型可能包在 ```json 里，也可能带解释文字
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

PLANNER_PROMPT = """你是代码变更的计划者。请先读代码，再给出**结构化变更计划**，不要直接改文件。

要求：
1. 先用 code_map / grep 定位相关符号与文件（地图坐标形如 `文件:行-行`）。
2. 只输出一个 JSON 对象，放在 ```json 代码块里，结构如下：
```json
{
  "goal": "这次变更要达成什么（一句话）",
  "subtasks": [
    {
      "id": "s1",
      "title": "子任务标题",
      "files": ["相对项目根的路径"],
      "symbol_ids": ["sym:<path>#<限定名>"],
      "risk": "low|medium|high",
      "verify": ["建议的验证命令，例如 python -m pytest tests/test_x.py"],
      "notes": "实现要点"
    }
  ],
  "notes": "整体说明与取舍"
}
```
3. files 必须是你确认存在的实际路径；risk 按"影响面（被谁引用）+ 是否有测试覆盖"判断。
4. 不要修改任何文件——本阶段只产出计划。

用户需求：
%s
"""

WORKER_PROMPT = """按批准的计划实施变更。计划如下：

```json
%s
```

要求：
1. 严格限制在计划声明的文件范围内；确实需要扩大范围的，先停下来说明原因，不要擅自改别的文件。
2. 用 edit_file 做最小精确替换（old_string 必须唯一命中）；新建文件才用 write_file。
3. 每完成一个子任务，用 `文件:行号` 说明改了什么。
4. 全部完成后给出总结：改了哪些文件/符号、为什么、建议跑哪些验证命令。
"""


class TaskError(Exception):
    """任务操作失败（对用户可见的原因）。"""


def slugify(text: str, limit: int = 28) -> str:
    """把需求文本压成分支名片段（只留字母数字与连字符）。

    截断时优先在连字符处断开：`add-support-for-variadic-args` 超长时得到
    `add-support-for-variadic` 而不是半个单词的 `…-arg`。
    """
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    if len(ascii_part) > limit:
        cut = ascii_part[:limit].rsplit("-", 1)[0]
        ascii_part = cut or ascii_part[:limit]
    return (ascii_part or "task").strip("-")


def extract_plan(text: str) -> dict | None:
    """从模型输出里抽 Plan JSON。抽不出或结构不对返回 None（由调用方降级处理）。"""
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
    subtasks = data.get("subtasks")
    if not isinstance(subtasks, list):
        subtasks = []
    cleaned: list[dict] = []
    for item in subtasks[:20]:
        if not isinstance(item, dict):
            continue
        files = [str(f).replace("\\", "/").strip("/") for f in (item.get("files") or []) if str(f).strip()]
        symbols = [str(s) for s in (item.get("symbol_ids") or []) if str(s).strip()]
        verify = [str(v) for v in (item.get("verify") or []) if str(v).strip()]
        cleaned.append(
            {
                "id": str(item.get("id") or f"s{len(cleaned) + 1}")[:30],
                "title": str(item.get("title") or "")[:200],
                "files": files[:40],
                "symbol_ids": symbols[:60],
                "risk": str(item.get("risk") or "medium")[:10],
                "verify": verify[:5],
                "notes": str(item.get("notes") or "")[:1000],
            }
        )
    return {
        "goal": str(data.get("goal") or "")[:500],
        "subtasks": cleaned,
        "notes": str(data.get("notes") or "")[:2000],
        # 计划里声明的文件集合：执行后用它判定"是否越界"
        "files": sorted({f for s in cleaned for f in s["files"]}),
    }


def worktree_of(task: dict) -> Path | None:
    """任务的工作树目录；没有、或已经被清理时返回 None。

    必须显式判空：`Path("")` 就是 `Path(".")`，在"服务器当前目录恰好存在"时会被
    当成一个有效工作树——于是 git 与扫描操作全都落到了**平台自己的目录**上。
    真机踩过：并行批次里一条还没有工作树的排队车道，审查时把整个平台仓库当成了
    "它的改动"（新增 3087 个符号），而且 `ensure_committed` 那条路径会去 commit
    平台自己的仓库。
    """
    raw = str(task.get("worktree") or "")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


class TaskService:
    """任务的生命周期编排（工作树 / 计划 / 执行 / 审查 / 合并）。"""

    def __init__(self, *, projects: Any, tasks: Any, intel: Any, sessions: Any = None) -> None:
        self.projects = projects
        self.tasks = tasks
        self.intel = intel
        self.sessions = sessions   # SessionManager（执行阶段用；测试里可为 None）
        self._task_operations: set[str] = set()

    def _assert_task_idle(self, project: dict, task: dict) -> None:
        if task["project_id"] != project["id"] or task["user_id"] != project["user_id"]:
            raise TaskError("任务不存在或无权访问")
        team = getattr(self.sessions, "agent_team", None)
        if team is not None:
            try:
                team.assert_isolated_task_idle(project["id"], task["user_id"], task["id"])
            except (ValueError, KeyError) as exc:
                raise TaskError(str(exc)) from exc
            return
        runtime = getattr(self.sessions, "_runtimes", {}).get(task.get("session_id") or "")
        approval = getattr(runtime, "pending_approval", None)
        if getattr(runtime, "busy", False) or (approval and approval()):
            raise TaskError("任务会话仍在执行或等待审批，等待结束后再操作")

    @contextlib.contextmanager
    def _task_maintenance(self, project: dict, task: dict):
        if task["id"] in self._task_operations:
            raise TaskError("任务工作树正在进行其他操作，请等待完成")
        self._assert_task_idle(project, task)
        team = getattr(self.sessions, "agent_team", None)
        reservation = team.isolated_task_maintenance(project["id"], task["user_id"], task["id"]) if team else contextlib.nullcontext()
        self._task_operations.add(task["id"])
        try:
            with reservation:
                yield
        finally:
            self._task_operations.discard(task["id"])

    # ------------------------------------------------------------------
    # 工作树
    # ------------------------------------------------------------------
    def worktree_path(self, project_id: str, task_id: str) -> Path:
        return code_dir() / project_id / "wt" / task_id

    async def _git(
        self, root: Path, args: list[str], *, timeout: float = 60.0, max_output: int = 64 * 1024
    ) -> tuple[int, str]:
        deny = check_command(args)
        if deny:
            raise TaskError(f"该 git 操作被平台禁止：{deny}")
        return await run_git(root, args, timeout=timeout, max_output=max_output)

    async def _git_internal(
        self, root: Path, args: list[str], *, timeout: float = 60.0, max_output: int = 64 * 1024
    ) -> tuple[int, str]:
        """平台内部使用的 git 动作（绕过用户侧黑名单）。

        清理自己的任务分支需要 `branch -D`（未合并的分支必须强删），而这条命令在
        **用户可见接口**里是被硬拒的——用户不该有"强删分支"的按钮，但平台的
        任务生命周期必须有办法回收自己建的分支。两者的边界就在这里：内部动作
        只对本任务自己创建的分支/工作树生效，且不暴露给 API。
        """
        return await run_git(root, args, timeout=timeout, max_output=max_output)

    async def create(
        self,
        project: dict,
        *,
        user_id: str,
        requirement: str,
        title: str = "",
        base_branch: str = "",
        git_init: bool = False,
    ) -> dict:
        """建任务：确保是 git 仓库 → 建分支与工作树 → 落库。"""
        root = Path(project["root_path"])
        requirement = (requirement or "").strip()
        if not requirement:
            raise TaskError("需求描述不能为空")
        if not root.is_dir():
            raise TaskError(f"项目目录不可用：{root}")

        code, out = await self._git(root, ["rev-parse", "--is-inside-work-tree"])
        is_repo = code == 0 and out.strip() == "true"
        if not is_repo:
            if not git_init:
                raise TaskError(
                    "该项目不是 git 仓库：隔离工作树依赖 git。请在弹窗里选择「初始化 git 并提交当前代码」"
                    "（会新增 .git 目录，不改动你的文件内容），或先手动 git init。"
                )
            await self._ensure_repo(root)

        code, out = await self._git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        current = out.strip() if code == 0 else ""
        base = (base_branch or current or "HEAD").strip()
        # 工作区分组名与分支名都用任务 id 保证唯一；分支再带上语义片段便于人读
        task_row = self.tasks.create(
            project_id=project["id"],
            user_id=user_id,
            title=title or requirement[:60],
            requirement=requirement,
            base_branch=base,
            branch="",
            worktree="",
            status="planning",
        )
        task_id = task_row["id"]
        await self.attach_worktree(project, task_row, base_branch=base)
        row = self.tasks.get(task_id) or task_row
        # 任务树索引：与主干分开，地图可以按 tree 切换/叠加
        self.intel.index(project["id"], self.tree_id(task_id))
        return row

    async def attach_worktree(
        self, project: dict, task: dict, *, base_branch: str = "", from_head: bool = False
    ) -> dict:
        """给一条任务建隔离工作树 + 分支。

        单独抽出来的原因：并行批次里的"冲突车道"不能在计划时就把工作树建出来——
        它必须等前面那条车道**落地之后**再从新的 HEAD 拉分支，否则两条车道各自
        基于旧 HEAD 改同一个文件，后合并的那条必然冲突（串行执行也救不了，
        因为冲突来自工作树的起点，不是来自并发）。
        """
        root = Path(project["root_path"])
        task_id = task["id"]
        if from_head or not base_branch:
            code, out = await self._git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
            base = (out.strip() if code == 0 else "") or (task.get("base_branch") or "HEAD")
        else:
            base = base_branch
        if task.get("worktree") and Path(task["worktree"]).is_dir():
            return self.tasks.get(task_id) or task
        branch = task.get("branch") or f"ai/{slugify(task.get('title') or task_id)}-{task_id[-6:]}"
        worktree = self.worktree_path(project["id"], task_id)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        code, out = await self._git(
            root, ["worktree", "add", "-b", branch, str(worktree), base], timeout=120.0
        )
        if code != 0:
            # 分支已存在时先回收同名分支再试一次（例如重启后重挂同一条车道）
            await self._git_internal(root, ["branch", "-D", branch])
            code, out = await self._git(
                root, ["worktree", "add", "-b", branch, str(worktree), base], timeout=120.0
            )
        if code != 0:
            self.tasks.update(task_id, status="failed", error=f"创建隔离工作树失败：{out[:400]}")
            raise TaskError(f"创建隔离工作树失败：{out[:400]}")
        self.tasks.update(task_id, branch=branch, worktree=str(worktree), base_branch=base)
        return self.tasks.get(task_id) or task

    async def _ensure_repo(self, root: Path) -> None:
        """把普通目录变成 git 仓库（含一次初始提交），用于隔离工作树。"""
        await self._git(root, ["init", "-q"])
        await self._git(root, ["add", "-A"])
        code, out = await self._git(
            root,
            [
                "-c", "user.name=Code-agent",
                "-c", "user.email=code-agent@local",
                "commit", "-q", "-m", "chore: 代码模式初始化（建立隔离工作树所需基线）",
            ],
        )
        if code != 0 and "nothing to commit" not in out:
            raise TaskError(f"初始化 git 失败：{out[:300]}")

    @staticmethod
    def tree_id(task_id: str) -> str:
        return f"{TASK_TREE_PREFIX}{task_id}"

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    def plan_prompt(self, requirement: str) -> str:
        return PLANNER_PROMPT % requirement

    def worker_prompt(self, plan: dict) -> str:
        return WORKER_PROMPT % json.dumps(plan, ensure_ascii=False, indent=2)

    def store_plan(self, task: dict, text: str) -> dict:
        """把模型输出解析成 Plan 并落库。解析失败进入 plan_failed（用户可手改）。"""
        plan = extract_plan(text)
        if plan is None:
            self.tasks.update(
                task["id"],
                status="plan_failed",
                error="没能从模型输出里解析出计划 JSON（可手动编辑计划后批准）",
            )
            return {"ok": False, "status": "plan_failed", "raw": (text or "")[:4000]}
        self._write_plan(task, plan)
        return {"ok": True, "status": "planned", "plan": plan}

    def _write_plan(self, task: dict, plan: dict) -> None:
        files = plan.get("files") or sorted(
            {f for s in plan.get("subtasks", []) for f in (s.get("files") or [])}
        )
        plan["files"] = files
        self.tasks.update(
            task["id"], status="planned", plan_json=json.dumps(plan, ensure_ascii=False), error=""
        )

    # ------------------------------------------------------------------
    # 变更与审查
    # ------------------------------------------------------------------
    async def changes(self, task: dict) -> list[dict]:
        """工作树里相对基准分支的文件级变更（git status + numstat）。"""
        wt = worktree_of(task)
        if wt is None:
            return []
        code, out = await self._git(wt, ["status", "--porcelain", "--untracked-files=all"])
        if code != 0:
            return []
        entries: list[dict] = []
        for line in out.splitlines():
            if len(line) < 3:
                continue
            # porcelain 是 "XY PATH"：状态两列 + 空格。用 line[2:] 再 strip，
            # 比固定切 3 个字符稳（`M  path` 与 `M path` 两种形态都能吃）
            status, path = line[:2].strip(), line[2:].strip()
            if not path:
                continue
            if " -> " in path:                      # rename 形式：旧 -> 新
                path = path.split(" -> ", 1)[1]
            entries.append({"path": path.replace("\\", "/"), "status": status or "M"})
        # 已被 commit 的改动也要算进来（与基准分支比较）
        code, out = await self._git(wt, ["diff", "--name-status", task["base_branch"] or "HEAD"])
        if code == 0:
            seen = {e["path"] for e in entries}
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                path = parts[-1].replace("\\", "/")
                if path not in seen:
                    entries.append({"path": path, "status": parts[0][:2]})
        return entries

    async def diff_text(self, task: dict, *, limit: int = 200_000) -> str:
        wt = worktree_of(task)
        if wt is None:
            return ""
        code, out = await self._git(
            wt, ["diff", task["base_branch"] or "HEAD"], timeout=60.0, max_output=limit
        )
        if code == 0 and out:
            return out
        # 未提交的新文件：diff 看不到内容，补一段 untracked 摘录
        code, status = await self._git(wt, ["status", "--porcelain", "--untracked-files=all"])
        untracked = [l[3:].strip() for l in status.splitlines() if l[:2].strip() == "??"]
        extra: list[str] = []
        for rel in untracked[:20]:
            try:
                body = (wt / rel).read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            extra.append(f"--- /dev/null\n+++ b/{rel}\n{body}")
        return out + ("\n" + "\n".join(extra) if extra else "")

    def symbol_diff(self, project: dict, task: dict) -> dict:
        """符号级差异：与主干索引比对（按 id + 内容哈希），产出地图叠加层。

        为什么值得单独算：用户要看的是"这次动了哪些类/函数"，而不是几十行文本 diff。
        """
        main = self.intel.index(project["id"], "main")
        task_index = self.intel.index(project["id"], self.tree_id(task["id"]))
        main_symbols = {row["id"]: row for row in main.all_symbols()}
        task_symbols = {row["id"]: row for row in task_index.all_symbols()}

        added = [sid for sid in task_symbols if sid not in main_symbols]
        removed = [sid for sid in main_symbols if sid not in task_symbols]

        def touched(sid: str) -> bool:
            """签名变了或实现体变了，都算"这个符号被改了"。

            只比 content_hash 会漏掉最常见的编辑（改函数体、签名不动），
            而那正是用户最想看到的"这次动了哪些函数"。
            """
            before, after = main_symbols[sid], task_symbols[sid]
            if before["content_hash"] != after["content_hash"]:
                return True
            if before["body_hash"] and after["body_hash"]:
                return before["body_hash"] != after["body_hash"]
            return False

        modified = [sid for sid in task_symbols if sid in main_symbols and touched(sid)]
        files = sorted(
            {task_symbols[sid]["path"] for sid in added + modified}
            | {main_symbols[sid]["path"] for sid in removed}
        )
        return {
            "added": [graph.symbol_summary(task_symbols[sid]) for sid in added[:500]],
            "modified": [graph.symbol_summary(task_symbols[sid]) for sid in modified[:500]],
            "removed": [graph.symbol_summary(main_symbols[sid]) for sid in removed[:500]],
            "files": files,
            "counts": {"added": len(added), "modified": len(modified), "removed": len(removed)},
        }

    async def review(self, project: dict, task: dict) -> dict:
        """审查快照：变更文件 + 符号级差异 + 越界检查（相对 Plan 声明的范围）。"""
        with self._task_maintenance(project, task):
            return await self._review_idle(project, task)

    async def _review_idle(self, project: dict, task: dict) -> dict:
        task = self._fresh(task)
        # 工作树已经被清掉（任务已合并/已放弃）：这里**不能重算**。
        # 重算要扫工作树，而路径已经不存在，会把 ProjectPathError 变成 422 抛给
        # 前端——面板正在轮询时就成了刷屏的报错。此时台账里那份快照才是有意义的答案。
        if worktree_of(task) is None:
            stored = self.review_of(task)
            if stored:
                return stored
            return {
                "changes": [], "changed_files": [], "outside_plan": [], "plan_scope_ok": True,
                "symbols": {"added": [], "modified": [], "removed": [], "files": [], "counts": {}},
                "diagnostics": {}, "verify_commands": [], "generated_at": "",
                "worktree_gone": True,
            }
        # 先把工作树的改动落进任务分支：审查看到的就是"提交后的任务状态"，
        # 后续合并才不会漏（否则 git 只认提交，未提交的改动会被静默丢掉）
        await self.ensure_committed(task)
        changes = await self.changes(task)
        # 先把工作树扫进任务索引，符号差异才有数据
        await self.intel.scan(
            {**project, "root_path": task["worktree"]}, tree_id=self.tree_id(task["id"])
        )
        try:
            sym = self.symbol_diff(project, task)
        except Exception:  # noqa: BLE001 —— 符号差异算不出来不该阻断审查
            sym = {"added": [], "modified": [], "removed": [], "files": [], "counts": {}}
        plan = self.plan_of(task)
        declared = set(plan.get("files") or [])
        changed = [c["path"] for c in changes]
        outside = [p for p in changed if declared and p not in declared]
        diagnostics = self.intel.diagnostics(
            {**project, "root_path": task["worktree"]}, tree_id=self.tree_id(task["id"])
        )
        # P6：门禁与影响面跟着审查一起给出来——用户在同一屏里看到
        # "这次动了什么 / 会惊动谁 / 踩了哪条架构规则"，而不是分三个地方拼。
        changed_symbols = list(sym.get("added") or []) + list(sym.get("modified") or [])
        gate = self._gate(project, changed, changed_symbols)
        impact = self._impact(project, changed)
        review = {
            "changes": changes,
            "changed_files": changed,
            "outside_plan": outside,
            "plan_scope_ok": not outside,
            "symbols": sym,
            "diagnostics": diagnostics.get("counts", {}),
            "gate": gate,
            "impact": impact,
            "verify_commands": [
                cmd for s in plan.get("subtasks", []) for cmd in (s.get("verify") or [])
            ][:5],
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        previous = self.review_of(task)
        if previous.get("execution_outcome"):
            review["execution_outcome"] = previous["execution_outcome"]
        if previous.get("verification"):
            # Verification belongs to a concrete committed revision. Keep it
            # across a refreshed snapshot, but expose that later changes need
            # another run rather than silently presenting old evidence as new.
            review["verification"] = previous["verification"]
            review["verification_head"] = previous.get("verification_head") or ""
            review["verification_fingerprint"] = previous.get("verification_fingerprint") or ""
            review["verification_stale"] = await self.verification_stale(task)
        self.tasks.update(task["id"], status="review", review_json=json.dumps(review, ensure_ascii=False), error="")
        return review

    def _gate(self, project: dict, files: list[str], changed_symbols: list[dict]) -> dict:
        """跑架构门禁。门禁本身出错不该阻断审查：如实降级成一条 error 说明。"""
        if not files or self.intel is None:
            return {"configured": False, "ok": True, "violations": [], "counts": {"error": 0, "warn": 0}}
        try:
            return self.intel.gate(project, files=files, changed_symbols=changed_symbols)
        except Exception as exc:  # noqa: BLE001
            return {
                "configured": True, "ok": True, "violations": [], "counts": {"error": 0, "warn": 0},
                "error": f"门禁没能跑起来（{type(exc).__name__}）：{exc}",
            }

    def _impact(self, project: dict, files: list[str]) -> dict:
        """影响面与风险。失败同样降级，不让审查整体报错。"""
        if not files or self.intel is None:
            return {}
        try:
            return self.intel.impact(project, files=files)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"影响面没能算出来（{type(exc).__name__}）：{exc}"}

    # ------------------------------------------------------------------
    # 架构门禁（P6）：合并前的硬约束 + 强制留痕
    # ------------------------------------------------------------------
    def _merge_gate(self, project: dict, task: dict, *, files: list[str] | None) -> dict:
        """合并前跑一次门禁：只看**本次要落地的文件**。"""
        review = self.review_of(task)
        changed = [str(p) for p in (review.get("changed_files") or [])]
        targets = [f for f in (files or changed) if f]
        symbols = list((review.get("symbols") or {}).get("added") or []) + list(
            (review.get("symbols") or {}).get("modified") or []
        )
        return self._gate(project, targets, symbols)

    @staticmethod
    def _gate_message(gate: dict) -> str:
        items = [v for v in (gate.get("violations") or []) if v.get("severity") == "error"]
        head = items[0].get("message") if items else "架构规则未通过"
        more = f"（共 {len(items)} 条）" if len(items) > 1 else ""
        return (
            f"架构门禁未通过：{head}{more}。"
            "要么按规则调整改动，要么在「强制落地」里写明理由（理由会记进决策线程）。"
        )

    def _record_force(self, project: dict, task: dict, gate: dict, reason: str) -> None:
        """强制落地必须留痕：一条决策挂到被改的文件上（列表页/时间线都能看到）。"""
        if self.intel is None:
            return
        review = self.review_of(task)
        changed = [str(p) for p in (review.get("changed_files") or [])] or [""]
        items = [v for v in (gate.get("violations") or []) if v.get("severity") == "error"]
        detail = "；".join(str(v.get("message") or "") for v in items[:4])
        for rel in changed[:3]:
            try:
                self.intel.decisions(project["id"]).record(
                    target=f"file:{rel}",
                    kind="risk",
                    text=f"[强制落地] {reason}（绕过：{detail or '架构规则'}）",
                    author=task.get("user_id") or "",
                    session_id=task.get("session_id") or "",
                    task_id=task["id"],
                    batch_id=task.get("batch_id") or "",
                    meta={"forced": True, "gate": {"counts": gate.get("counts") or {}}},
                )
            except Exception:  # noqa: BLE001 —— 留痕失败不该阻断合并
                pass

    async def ensure_committed(self, task: dict) -> bool:
        """把工作树里未提交的改动提交到任务分支。

        为什么必须：`git merge` / `git checkout <branch> -- <file>` 都只认**提交**，
        而 AI（以及编辑器）写在工作树里的改动默认是未提交的——不先提交，
        合并就会"什么都没发生"（真机踩过：合并成功但主干没有变化）。
        提交只发生在任务自己的分支上，且信息里写明来源，便于用户追溯。
        """
        wt = worktree_of(task)
        if wt is None:
            return False
        code, out = await self._git_internal(wt, ["status", "--porcelain", "--untracked-files=all"])
        if code != 0 or not out.strip():
            return False
        await self._git_internal(wt, ["add", "-A"], timeout=60.0)
        code, out = await self._git_internal(
            wt,
            [
                "-c", "user.name=Code-agent",
                "-c", "user.email=code-agent@local",
                "commit", "-q", "-m", f"ai: {task['title']}（Code-agent 任务 {task['id']}）",
            ],
            timeout=60.0,
        )
        return code == 0

    @staticmethod
    def plan_of(task: dict) -> dict:
        try:
            data = json.loads(task.get("plan_json") or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def review_of(task: dict) -> dict:
        try:
            data = json.loads(task.get("review_json") or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    async def _verification_fingerprint(self, task: dict) -> str:
        """Hash the committed revision and actual dirty/untracked contents.

        Git output contains paths only; changed file contents are streamed into
        a digest in a worker thread and never returned to the API. Missing or
        unstable snapshots fail closed so stale evidence cannot look current.
        """
        wt = worktree_of(task)
        if wt is None:
            return ""
        results = await asyncio.gather(
            self._git(wt, ["rev-parse", "HEAD"]),
            self._git(wt, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], max_output=4 * 1024 * 1024),
            self._git(wt, ["diff", "--raw", "--no-renames", "-z", "HEAD", "--"], max_output=4 * 1024 * 1024),
            self._git(wt, ["ls-files", "--others", "--exclude-standard", "-t", "-z"], max_output=4 * 1024 * 1024),
        )
        if any(code != 0 for code, _ in results):
            return ""
        head, status, changed, untracked = [body for _, body in results]
        # If the path inventory reached the wrapper's output ceiling there is
        # no complete snapshot, even if the process itself exited successfully.
        if any(len(body.encode("utf-8")) >= 4 * 1024 * 1024 for body in (status, changed, untracked)):
            return ""
        # These formats put a non-whitespace header before every path. The
        # shared Git wrapper strips outer whitespace, which would otherwise
        # corrupt a first filename that starts with a space.
        changed_parts = changed.split("\0")
        if changed_parts[-1] == "":
            changed_parts.pop()
        if len(changed_parts) % 2 or any(not header.startswith(":") for header in changed_parts[::2]):
            return ""
        untracked_parts = [record for record in untracked.split("\0") if record]
        if any(not record.startswith("? ") for record in untracked_parts):
            return ""
        paths = sorted(set(changed_parts[1::2]) | {record[2:] for record in untracked_parts})

        def digest_files() -> str:
            digest = hashlib.sha256()
            digest.update(head.strip().encode("utf-8"))
            digest.update(b"\0" + status.encode("utf-8"))
            for relative in paths:
                target = wt / relative
                try:
                    # Symlink targets may leave the task tree. Fingerprint the
                    # link itself instead of reading through its boundary.
                    digest.update(b"\0path\0" + relative.encode("utf-8"))
                    if target.is_symlink():
                        digest.update(b"\0link\0" + os.readlink(target).encode("utf-8"))
                        continue
                    if not target.exists():
                        digest.update(b"\0missing\0")
                        continue
                    if not target.resolve().is_relative_to(wt.resolve()) or not target.is_file():
                        return ""
                    before = target.stat()
                    with target.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    after = target.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        return ""
                except OSError:
                    return ""
            return digest.hexdigest()

        return await asyncio.to_thread(digest_files)

    async def verification_stale(self, task: dict) -> bool:
        """Read-only check for a task card; never commits or starts an agent."""
        previous = self.review_of(task)
        if not previous.get("verification"):
            return False
        if previous.get("verification_stale") or not previous.get("verification_fingerprint"):
            return True
        current = await self._verification_fingerprint(task)
        return not current or current != previous["verification_fingerprint"]

    async def verify(self, task: dict, *, timeout: float = 300.0) -> dict:
        task = self._fresh(task)
        project = self.projects.get(task["project_id"])
        if project is None:
            raise TaskError("项目不存在")
        with self._task_maintenance(project, task):
            return await self._verify_idle(task, timeout=timeout)

    async def _verify_idle(self, task: dict, *, timeout: float = 300.0) -> dict:
        """在工作树里跑计划里的验证命令（用户点按钮触发，不由 AI 自主执行）。

        刻意走"用户触发"而不是让 agent 自己跑：测试命令往往是长任务且可能改动产物，
        把它交给用户的一次明确点击，比塞进 agent 的工具循环更可控。
        """
        import time

        from code_agent.sandbox.executor import run_shell

        plan = self.plan_of(task)
        commands = [c for s in plan.get("subtasks", []) for c in (s.get("verify") or [])][:3]
        if not commands:
            return {"ok": False, "note": "计划里没有建议的验证命令", "results": []}
        wt = worktree_of(task)
        if wt is None:
            # 工作树不在就绝不能跑：命令会落到"当前目录"（也就是平台自己的仓库）上
            return {"ok": False, "note": "任务工作树不存在（可能是排队中的车道，或任务已结束）",
                    "results": []}
        results: list[dict] = []
        fingerprint = await self._verification_fingerprint(task)
        for cmd in commands:
            started = time.perf_counter()
            output = await run_shell(cmd, cwd=wt, timeout=timeout)
            results.append(
                {
                    "command": cmd,
                    "ok": output.startswith("exit_code=0"),
                    "duration_s": round(time.perf_counter() - started, 2),
                    "output": output[:4000],
                }
            )
            if not results[-1]["ok"]:
                break  # 失败即停：后续命令大概率也不成立
        review = self.review_of(task)
        review["verification"] = results
        code, head = await self._git(wt, ["rev-parse", "HEAD"])
        review["verification_head"] = head.strip() if code == 0 else ""
        review["verification_fingerprint"] = fingerprint
        current = await self._verification_fingerprint(task)
        review["verification_stale"] = not fingerprint or current != fingerprint
        self.tasks.update(task["id"], review_json=json.dumps(review, ensure_ascii=False))
        return {"ok": all(r["ok"] for r in results), "results": results}

    # ------------------------------------------------------------------
    # 合并 / 放弃
    # ------------------------------------------------------------------
    async def merge(
        self,
        project: dict,
        task: dict,
        *,
        files: list[str] | None = None,
        force: bool = False,
        reason: str = "",
    ) -> dict:
        task = self._fresh(task)
        with self._task_maintenance(project, task):
            return await self._merge_idle(project, task, files=files, force=force, reason=reason)

    async def _merge_idle(
        self, project: dict, task: dict, *, files: list[str] | None = None, force: bool = False, reason: str = "",
    ) -> dict:
        """把任务结果落到主干。

        - files 为空：整体合并（`git merge --no-ff`），产生一条合并提交；
        - files 非空：只把指定文件从任务分支取回主干工作区（**不自动提交**，
          由用户在源代码管理面板里审阅并提交）。
        无论哪种，都会清理工作树/分支并重建主干索引。

        `force=True` 表示**明知**踩了架构规则仍要落地：必须同时给 `reason`，
        理由会写进这个文件的决策线程——"强制"本身必须留痕，否则规则就白设了。
        """
        task = self._fresh(task)
        if task["status"] not in ("review", "planned", "plan_failed", "running"):
            raise TaskError(f"任务当前状态（{task['status']}）不可合并")
        gate = self._merge_gate(project, task, files=files)
        if not gate.get("ok", True):
            if not force:
                raise TaskError(self._gate_message(gate))
            note = (reason or "").strip()
            if not note:
                raise TaskError(
                    "踩了架构规则又选择强制落地，必须写明理由（会记进该文件的决策线程）"
                )
            self._record_force(project, task, gate, note)
        # 先落提交：merge 与 checkout <branch> -- <file> 都只认提交，
        # 未提交的改动会被无声地漏掉（合并"成功"但主干没变化）
        await self.ensure_committed(task)
        root = Path(project["root_path"])
        branch = task["branch"]
        applied: list[str] = []

        if files:
            declared = set(files)
            changes = {c["path"] for c in await self.changes(task)}
            unknown = declared - changes
            if unknown:
                raise TaskError(f"这些文件不在本次变更里：{', '.join(sorted(unknown))}")
            code, out = await self._git(root, ["checkout", branch, "--", *sorted(declared)], timeout=60.0)
            if code != 0:
                raise TaskError(f"应用文件失败：{out[:300]}")
            applied = sorted(declared)
        else:
            code, out = await self._git(
                root,
                ["-c", "user.name=Code-agent", "-c", "user.email=code-agent@local",
                 "merge", "--no-ff", "-m", f"merge: {task['title']}（Code-agent 任务 {task['id']}）", branch],
                timeout=120.0,
            )
            if code != 0:
                raise TaskError(
                    f"合并失败（可能有冲突）：{out[:400]}。可在工作树里手动解决后重试，或放弃该任务。"
                )
            applied = [c["path"] for c in await self.changes(task)]

        await self._cleanup(project, task)
        self.tasks.update(
            task["id"],
            status="merged",
            merged_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        await self.intel.scan(project, full=False)
        return {"ok": True, "files": applied}

    async def discard(self, project: dict, task: dict) -> dict:
        """放弃任务：删工作树与分支，主干不受影响。"""
        task = self._fresh(task)
        with self._task_maintenance(project, task):
            await self._cleanup(project, task)
            self.tasks.update(task["id"], status="discarded", error="")
        return {"ok": True}

    async def _cleanup(self, project: dict, task: dict) -> None:
        self._assert_task_idle(project, task)
        root = Path(project["root_path"])
        wt = Path(task["worktree"]) if task.get("worktree") else None
        if wt is not None and wt.exists():
            await self._git_internal(root, ["worktree", "remove", "--force", str(wt)], timeout=120.0)
        with contextlib.suppress(Exception):
            await self._git_internal(root, ["worktree", "prune"])
        if task.get("branch"):
            # 已合并的分支普通删除即可；未合并的（按文件应用/放弃）需要强删
            code, _ = await self._git_internal(root, ["branch", "-d", task["branch"]])
            if code != 0:
                await self._git_internal(root, ["branch", "-D", task["branch"]])
        # 任务树的索引文件留着没意义（工作树都没了），顺手关掉句柄；
        # 下一步 scan 会按需重建，地图切回 task 树时会看到空索引而不是陈旧的树
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.intel.close_tree, project["id"], self.tree_id(task["id"]))

    def _fresh(self, task: dict) -> dict:
        """重新取一次任务行：调用方手里的 row 可能在 review/merge 之间已经过期。"""
        return self.tasks.get(task["id"]) or task

    # ------------------------------------------------------------------
    # 执行（需要 SessionManager；测试可只用上面的机械部分）
    # ------------------------------------------------------------------
    async def run_agent(
        self,
        project: dict,
        task: dict,
        prompt: str,
        *,
        permission_mode: str,
    ) -> dict:
        """在任务工作树里跑一次 agent 会话。

        工作区 = 工作树（不是主干），权限 = `settings.task_permission_mode()`：
        AI 的每一次写入都发生在隔离树里，主干在这一步完全不受影响。
        档位跟随全局默认（下限 workspace-write）——**不许硬编码**：写死 workspace-write
        会让"用户在设置里选完全权限"对任务完全无效，表现就是任务里每条命令都要审批。
        """
        if self.sessions is None:
            raise TaskError("会话管理器不可用")
        if task["id"] in self._task_operations:
            raise TaskError("任务工作树正在审查、验证或回收，等待结束后再执行")
        team = getattr(self.sessions, "agent_team", None)
        if team is not None:
            # A task is a Lead-owned descendant in its existing isolated tree,
            # never a second execution pipeline with independent capacity.
            return await team.run_isolated_task(
                project["id"], task["user_id"], task["id"], prompt,
                permission_mode=permission_mode,
            )
        session_id = task.get("session_id") or ""
        if not session_id:
            meta = self.sessions.create_task_session(
                task["user_id"],
                project["id"],
                task_id=task["id"],
                title=f"[任务] {task['title']}",
                workspace=Path(task["worktree"]),
                permission_mode=permission_mode,
            )
            session_id = meta["id"]
            self.tasks.update(task["id"], session_id=session_id)
        else:
            # 车道会话是复用的：档位也要跟着设置走，否则用户把默认调成「完全权限」之后，
            # 已存在的任务会话仍然按老档位跑，每条命令都要人工审批。
            with contextlib.suppress(Exception):
                self.sessions.set_permission_mode(session_id, task["user_id"], permission_mode)
        runtime = self.sessions.get_runtime(session_id, task["user_id"])
        result = await runtime.submit(prompt)
        outcome = {"text": result.text, "stop_reason": result.stop_reason, "turns": result.turns}
        self.require_completed(outcome)
        return outcome

    @staticmethod
    def require_completed(outcome: dict) -> None:
        """Partial output from a cancelled/budget-limited turn is not success."""
        state = outcome.get("state")
        reason = outcome.get("stop_reason") or ""
        if ((state and state != "completed") or reason not in ("end_turn", "completed", "stop", "ok")
                or not str(outcome.get("text") or "").strip()):
            raise TaskError(outcome.get("error") or f"执行未完成（{reason or state or '缺少完成结果'}），不能进入审查")

    async def execute(self, project: dict, task: dict) -> dict:
        """按批准的计划执行：跑 agent → 生成审查快照。"""
        task = self._fresh(task)
        self._assert_task_idle(project, task)
        if task["status"] in ("merged", "discarded"):
            raise TaskError(f"任务已结束（{task['status']}），不能再次执行")
        plan = self.plan_of(task)
        self.tasks.update(task["id"], status="running", error="")
        try:
            outcome = await self.run_agent(
                project, task, self.worker_prompt(plan), permission_mode=task_permission_mode()
            )
            self.require_completed(outcome)
        except asyncio.CancelledError:
            self.tasks.update(task["id"], status="failed", error="执行已取消，尚未完成审查")
            raise
        except Exception as exc:  # noqa: BLE001 —— 执行失败要留状态与原因，而不是抛给用户一堆栈
            self.tasks.update(task["id"], status="failed", error=f"执行失败：{exc}")
            raise TaskError(f"执行失败：{exc}") from exc
        fresh = self.tasks.get(task["id"]) or task
        stored = self.review_of(fresh)
        stored["execution_outcome"] = outcome
        self.tasks.update(task["id"], review_json=json.dumps(stored, ensure_ascii=False))
        try:
            review = await self.review(project, self.tasks.get(task["id"]) or task)
        except Exception as exc:  # noqa: BLE001
            # The parent may have returned while its children remain active,
            # or a completion receipt may have triggered another activation.
            # Keep the exact result and an explicit recoverable review state.
            note = f"执行者本轮已结束，审查尚未完成，可在阻塞解除后刷新：{exc}"
            self.tasks.update(task["id"], status="review", error=note)
            return {"ok": False, "outcome": outcome, "review": self.review_of(self.tasks.get(task["id"]) or task), "review_pending": True}
        return {"ok": True, "outcome": outcome, "review": review}
