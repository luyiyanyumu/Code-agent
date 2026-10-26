"""后台任务工具（M45，对齐 DSH 的 `dsh-tool-jobs`）：`job_output` / `job_list` / `job_kill`。

三个工具都很薄——真正的机制在 `code_agent/jobs/registry.py`。这里只负责
"把模型的意图翻译成注册表调用"，并守住两条口径：

1. **没有任务表就说没有**。嵌入式 / 纯 CLI 场景拿不到注册表时，如实报错，
   而不是返回一句"任务已完成"之类的假话（同 M31 提问通道的纪律）。
   模型据此可以把结论写进回答，而不是继续等一个永远不会来的结果。
2. **摘要里带 `[status: …]`**。模型判断"该继续等还是该换路子"全靠它；
   没有状态行，它只能猜，而猜错的代价是又一轮空转。
"""

from __future__ import annotations

from typing import Any

from code_agent.jobs.registry import describe
from code_agent.tools.base import Tool, ToolContext

#: 没有任务表时的统一说法（三个工具共用，免得各写一份还不一致）
NO_REGISTRY = (
    "[job] 当前环境没有后台任务表（嵌入式 / 未接 SessionRuntime）：这个能力在这里不可用。"
    "别假装发起过任务——把结论直接写进回答里。"
)


def _registry(ctx: ToolContext) -> Any:
    return (ctx.extra or {}).get("jobs")


class JobOutputTool(Tool):
    name = "job_output"
    description = (
        "Read a background job's output. Returns only what arrived since your last read. "
        "Set wait=true to block (up to timeout_ms) until the job finishes — do that only when "
        "you are genuinely blocked on its result. Every response ends with [status: ...]."
    )
    description_zh = (
        "读一个后台任务的输出（只返回自上次读取以来的新增部分）。"
        "wait=true 会阻塞等到它结束（最多 timeout_ms）——只有真的被它挡住时才这么用。"
        "每次返回结尾都带 [status: …]。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id returned by bash(run_in_background=true)"},
            "wait": {"type": "boolean", "description": "Block until the job settles (default false)"},
            "timeout_ms": {
                "type": "integer",
                "description": "Max wait in milliseconds when wait=true (default 30000, max 300000)",
            },
        },
        "required": ["job_id"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        registry = _registry(ctx)
        if registry is None:
            return NO_REGISTRY
        job_id = str(arguments.get("job_id") or "").strip()
        if not job_id:
            return "[job_output] job_id 不能为空。可用任务见 job_list。"

        timeout_ms = arguments.get("timeout_ms")
        try:
            budget = int(timeout_ms) if timeout_ms is not None else None
        except (TypeError, ValueError):
            budget = None
        read = await registry.read(
            job_id,
            wait=bool(arguments.get("wait")),
            timeout_ms=budget,
        )
        if read is None:
            known = ", ".join(j.id for j in registry.list(ctx.session_id)) or "（无）"
            return f"[job_output] 找不到任务 {job_id}。本会话的任务：{known}"
        return read.text


class JobListTool(Tool):
    name = "job_list"
    description = (
        "List your background jobs: one line each, `<id> [<kind>] <status> — <label>`. "
        "Use it to remember what you started; do not poll it in a loop."
    )
    description_zh = "列出本会话的后台任务（每行 `<id> [<kind>] <status> — <label>`）。别拿它当轮询用。"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        registry = _registry(ctx)
        if registry is None:
            return NO_REGISTRY
        jobs = registry.list(ctx.session_id)
        if not jobs:
            return "（当前没有后台任务）"
        lines = [describe(job) for job in jobs]
        running = sum(1 for j in jobs if j.status == "running")
        head = f"后台任务 {len(jobs)} 个（运行中 {running} 个）"
        return head + "\n" + "\n".join(lines)


class JobKillTool(Tool):
    name = "job_kill"
    description = (
        "Stop a running background job. It settles as `killed` only after the work actually "
        "stopped (the process tree is killed). Optionally pass a reason, which is recorded."
    )
    description_zh = (
        "停掉一个正在跑的后台任务。**工作真的停下之后**才结算成 killed（杀的是进程树）。"
        "可以给一个 reason，会记进任务里。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id to stop"},
            "reason": {"type": "string", "description": "Optional short reason, recorded with the job"},
        },
        "required": ["job_id"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        registry = _registry(ctx)
        if registry is None:
            return NO_REGISTRY
        job_id = str(arguments.get("job_id") or "").strip()
        if not job_id:
            return "[job_kill] job_id 不能为空。可用任务见 job_list。"
        reason = str(arguments.get("reason") or "").strip()[:200]
        outcome = registry.kill(job_id, reason, by_model=True)
        if outcome == "not-found":
            known = ", ".join(j.id for j in registry.list(ctx.session_id)) or "（无）"
            return f"[job_kill] 找不到任务 {job_id}。本会话的任务：{known}"
        if outcome == "already-finished":
            job = registry.get(job_id)
            return f"[job_kill] {job_id} 已经结束了（{job.status if job else '?'}），无需再停。"
        return (
            f"[job_kill] 已请求停止 {job_id}"
            + (f"（原因：{reason}）" if reason else "")
            + "。它会在工作真正停止后结算成 killed——用 job_output 确认，"
            "不要假设它已经立刻停了。"
        )


def build_job_tools() -> list[Tool]:
    """三个后台任务工具（由 build_registry 注册）。"""
    return [JobOutputTool(), JobListTool(), JobKillTool()]


__all__ = ["JobKillTool", "JobListTool", "JobOutputTool", "build_job_tools"]
