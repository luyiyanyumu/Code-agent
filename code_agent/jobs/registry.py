"""后台任务注册表（对齐 DSH 的 `dsh-jobs` + `dsh-tool-jobs`）。

解决的具体问题：`bash` 过去是**完全阻塞**的——一条 `npm install` / `docker build` /
训练脚本要么把整轮卡住，要么撞 30 秒超时被杀掉，模型于是把它拆成"起个进程 +
反复轮询"的笨办法，而轮询的每一次都要烧一轮模型调用。

这里给每个会话一张任务表：长命令可以**先挂到后台**拿到 id，模型接着干别的，
需要时读输出、不需要时停掉；任务自己结束时，会话会收到一条完成通知（见
`drain_notices`，由 SessionRuntime 决定"繁忙就下一步注入、空闲就唤醒一轮"）。

四条刻意的设计约束（每条都对应一种真实的坑）：

1. **输出环按字节保留尾部，且有上限**。跑飞的后台进程能产出几个 G 的日志，
   全留在内存里会把整个 sidecar 拖垮。环满就丢**最旧**的块，并且如实告诉模型
   "有 N 字节因为内存上限被丢弃、那段读不回来了"——有损读取必须说自己是有的损的，
   否则模型会把"我看到的"当成"全部"。
2. **杀停靠杀进程树，不靠取消协程**。见 `sandbox/executor.py` 文件头两条结论：
   取消正在读管道的协程会让 transport / 管道永久无法回收。所以 `kill()` 调的是
   运行方注册的 `kill` 回调（对 bash 就是"杀进程树"事件），与超时走同一条路。
3. **结算通知只在"没人收"的时候发**。已经被 `job_output(wait=true)` 收走的、
   或模型自己 `job_kill` 掉的结算，**不再**唤醒会话——否则会出现"我刚读完它，
   它又来叫醒我一次"这种纯噪音（DSH 同款：注册表把释放了存活 wait 的结算报告为
   `awaited`，插件记着模型杀停过的任务）。
4. **生命周期跟着会话运行时而活**，不落库、不跨进程。运行时重建（重启/重开）
   即清空——"我以为那个后台任务还在跑"恰恰是最危险的假设。

内存里的表是**进程内**的：`SessionRuntime` 持有一个，会话结束随之释放。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from code_agent.models.events import new_id

#: 每个任务保留的输出上限（字节）。超出丢最旧的，并如实报告丢了多少。
MAX_RING_BYTES = 256 * 1024
#: 每个会话最多保留多少个任务（结算最久的先淘汰）。
MAX_JOBS_PER_SESSION = 50
#: `job_output(wait=true)` 的默认/最大等待（毫秒）。
DEFAULT_WAIT_MS = 30_000
MAX_WAIT_MS = 300_000

#: 任务状态
RUNNING = "running"
DONE = "done"
FAILED = "failed"
KILLED = "killed"
SETTLED = (DONE, FAILED, KILLED)

Runner = Callable[["Job"], Awaitable[str]]


class JobStopped(Exception):
    """运行方报告"我是被停掉的，不是自己跑完的"。

    为什么需要这个信号：`run_shell` 收到杀停事件后是**正常返回**的（杀进程 → 管道 EOF
    → 返回带"进程被外部终止"的文本）。如果任务层只看"有没有抛异常"，被杀停的任务会被
    结算成 `done`——于是模型读到的是一条"成功"的假象，而它明明被停了。
    """

    def __init__(self, text: str = "") -> None:
        super().__init__("job stopped")
        self.text = text


@dataclass
class Job:
    """一个后台任务。输出按字节存在环里，模型侧只有一个消费游标。"""

    id: str
    session_id: str
    kind: str
    label: str
    status: str = RUNNING
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    #: 结算文本（bash 的最终输出；subagent 的最终回答）
    result: str = ""
    #: 杀停原因（谁杀的、为什么）
    kill_reason: str = ""
    #: 已被 `job_output(wait=true)` 的等待收走 → 结算时**不再**通知
    awaited: bool = False
    #: 结算通知已经发过（避免重复唤醒）
    notified: bool = False
    #: 结算文本是否已经交给模型读过一次（DSH：结算后的第一次读取恰好携带一次）
    result_read: bool = False
    #: 由模型自己 `job_kill` 杀停 → 结算时不再通知（它知道自己杀了）
    killed_by_model: bool = False

    _ring: bytearray = field(default_factory=bytearray)
    _total: int = 0
    _dropped: int = 0
    _cursor: int = 0
    #: 本任务的输出环上限（默认取模块常量；注册表可整体调小，测试用得上）
    _max_ring: int = MAX_RING_BYTES
    _task: asyncio.Task | None = None
    kill: Callable[[], None] | None = None

    # -- 输出环 -------------------------------------------------------------
    def append(self, chunk: bytes) -> None:
        """追加输出。环满丢最旧的块，并记账丢了多少字节。"""
        if not chunk:
            return
        self._ring.extend(chunk)
        self._total += len(chunk)
        overflow = len(self._ring) - self._max_ring
        if overflow > 0:
            del self._ring[:overflow]
            self._dropped += overflow

    def take_new(self) -> tuple[str, int]:
        """取"自上次读取以来的新输出"，返回 (文本, 因丢块而永远读不到的字节数)。

        有损这件事必须显式返回：模型看到的是尾部窗口，而它天然会假设"这就是全部"。
        """
        base = self._total - len(self._ring)          # 环里最旧字节的绝对偏移
        start = max(self._cursor, base)
        missed = start - self._cursor
        data = bytes(self._ring[start - base:])
        self._cursor = self._total
        return data.decode("utf-8", errors="replace"), missed

    @property
    def buffered_bytes(self) -> int:
        return len(self._ring)

    @property
    def dropped_bytes(self) -> int:
        return self._dropped

    @property
    def settled(self) -> bool:
        return self.status in SETTLED


@dataclass
class JobRead:
    """一次 `job_output` 的结果。"""

    job: Job
    text: str
    status: str
    #: 只给了一次的结算文本（供调用方拼装）
    result_delivered: bool = False


class JobRegistry:
    """一个会话的后台任务表。"""

    def __init__(
        self,
        *,
        on_settle: Callable[[Job], None] | None = None,
        max_ring_bytes: int = MAX_RING_BYTES,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._settle_events: dict[str, asyncio.Event] = {}
        self._max_ring = max(1024, int(max_ring_bytes))
        #: 结算回调（SessionRuntime 用它决定"下一步注入还是唤醒一轮"）。
        #: 回调抛异常绝不影响任务本身——它只是通知，不是任务的一部分。
        self.on_settle = on_settle

    # -- 起任务 -------------------------------------------------------------
    def start(
        self,
        *,
        session_id: str,
        kind: str,
        label: str,
        runner: Runner,
        kill: Callable[[], None] | None = None,
    ) -> Job:
        """登记并启动一个后台任务，**立即**返回 Job（不等它跑完）。"""
        job = Job(id=new_id("job_"), session_id=session_id, kind=kind, label=label, kill=kill)
        job._max_ring = self._max_ring
        self._jobs[job.id] = job
        self._order.append(job.id)
        self._prune(session_id)
        # 用 `get_running_loop().create_task` 而**不是** `asyncio.ensure_future`：
        # 后者在拿不到运行中的循环时会退到"隐式事件循环"，悄悄把任务挂到一个永远不会
        # 跑的循环上（真机表现：任务永远停在 running，外加一句 "coroutine was never
        # awaited" 的警告）。这里宁可**显式失败**——没有循环就没有后台任务。
        job._task = asyncio.get_running_loop().create_task(self._run(job, runner))
        return job

    async def _run(self, job: Job, runner: Runner) -> None:
        try:
            job.result = await runner(job)
            job.status = DONE
        except JobStopped as stop:
            # 运行方明确报告"我被停掉了"——不能因为它是正常返回就记成 done
            job.status = KILLED
            job.result = stop.text or ""
        except asyncio.CancelledError:
            # 取消**原样上抛**（同 tools/builtin/ask_tools.py:130 的纪律）：
            # 收尾状态先写好，但绝不把这个异常吞掉——吞掉会让"停止"变成假动作。
            job.status = KILLED
            raise
        except Exception as exc:  # noqa: BLE001 —— 任务失败是结果，不是崩溃
            job.status = FAILED
            job.result = f"[job failed] {type(exc).__name__}: {exc}"
        finally:
            job.finished_at = time.time()
            # 杀停原因统一在这里补一次前缀（三条路径都过这里，所以只写一处）。
            # 已经以 [stopped 开头的就不再叠一层——重复的标记比没有标记更让人困惑。
            if job.kill_reason and job.status != DONE and not job.result.startswith("[stopped"):
                job.result = f"[stopped: {job.kill_reason}]\n{job.result}".strip()
            elif job.status == KILLED and not job.result:
                job.result = f"[stopped: {job.kill_reason or 'cancelled'}]"
            event = self._settle_events.get(job.id)
            if event is not None:
                event.set()
            self._fire_settle(job)

    def _fire_settle(self, job: Job) -> None:
        if self.on_settle is None:
            return
        with contextlib.suppress(Exception):
            self.on_settle(job)

    # -- 读 / 列 / 停 -------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, session_id: str) -> list[Job]:
        """本会话可见的任务：自己的 + 无主的（对齐 DSH 的所有者围栏）。"""
        return [
            self._jobs[jid]
            for jid in self._order
            if jid in self._jobs
            and self._jobs[jid].session_id in (session_id, "")
        ]

    def peek(self, session_id: str, job_id: str, *, offset: int = 0, limit: int = 16_000) -> dict[str, Any] | None:
        """Read a bounded observer snapshot without consuming output or notices.

        Strict ownership is intentional: ownerless jobs are not descendant
        evidence. Offsets apply to this snapshot's retained output + result;
        a running job's ring may change, which the returned byte counts expose.
        """
        job = self._jobs.get(job_id)
        if job is None or job.session_id != session_id:
            return None
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32_000:
            raise ValueError("invalid observer output range")
        output = bytes(job._ring).decode("utf-8", errors="replace")
        sections = {"output": output, "result": job.result}
        remaining, skip, fragments = limit, offset, {}
        total = sum(len(value) for value in sections.values())
        for name, value in sections.items():
            if skip >= len(value):
                skip -= len(value)
                continue
            part = value[skip:skip + remaining]
            if part:
                fragments[name] = {"text": part, "offset": skip, "total_chars": len(value)}
                remaining -= len(part)
            skip = 0
            if not remaining:
                break
        return {
            **as_dict(job), "session_id": session_id, "fragments": fragments,
            "snapshot_total_chars": total, "total_output_bytes": job._total,
            "next_offset": offset + limit - remaining if offset + limit - remaining < total else None,
            "snapshot_notice": "只读内存快照；运行中输出可能变化，重启或任务淘汰后不可恢复。dropped_bytes 是已丢失输出。",
        }

    async def read(
        self, job_id: str, *, wait: bool = False, timeout_ms: int | None = None
    ) -> JobRead | None:
        """读一个任务的输出。`wait=True` 时最多等到超时（超时后任务**仍然活着**）。"""
        job = self._jobs.get(job_id)
        if job is None:
            return None

        if wait and not job.settled:
            budget = min(timeout_ms or DEFAULT_WAIT_MS, MAX_WAIT_MS) / 1000
            event = self._settle_events.setdefault(job.id, asyncio.Event())
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=budget)
            if job.settled:
                # 已经用等待把它收走了 → 结算不必再通知（DSH 的 `awaited` 语义）
                job.awaited = True

        parts: list[str] = []
        fresh, missed = job.take_new()
        if fresh.strip():
            parts.append(fresh.rstrip("\n"))
        if missed:
            parts.append(
                f"（注意：还有 {missed} 字节输出在读到之前就因为内存上限被丢弃了，"
                "那段读不回来——这不是命令没输出）"
            )
        delivered = False
        if job.settled and not job.result_read:
            job.result_read = True
            delivered = True
            if job.result.strip():
                parts.append(f"--- 任务结果 ---\n{job.result.rstrip()}")
        parts.append(f"[status: {job.status}]")
        return JobRead(job=job, text="\n".join(parts), status=job.status, result_delivered=delivered)

    def kill(self, job_id: str, reason: str = "", *, by_model: bool = False) -> str:
        """请求停掉一个任务。返回 "ok" / "already-finished" / "not-found"。

        只**请求**停：真正停没停看它有没有结算（对齐 DSH："取消只有在工作停止后才结算"）。
        杀停动作交给运行方注册的 `kill` 回调（bash = 杀进程树），**不 cancel 协程**。
        """
        job = self._jobs.get(job_id)
        if job is None:
            return "not-found"
        if job.settled:
            return "already-finished"
        job.kill_reason = reason or "cancelled by the model"
        if by_model:
            job.killed_by_model = True
        if job.kill is not None:
            with contextlib.suppress(Exception):
                job.kill()
        else:
            # 没有杀停通道（例如纯 asyncio 任务）：退化成取消，但要说清这是退路。
            if job._task is not None:
                job._task.cancel()
        return "ok"

    # -- 结算通知 -----------------------------------------------------------
    def drain_notices(self, session_id: str) -> list[Job]:
        """取走"该通知这个会话"的结算任务并标记已通知。

        三条豁免（都是 DSH 的实测结论，不是洁癖）：
        - 已被等待收走的（`awaited`）→ 通知是重复的；
        - 模型自己杀掉的（`killed_by_model`）→ 它当然知道；
        - 已通知过的 → 一条只叫一次。
        """
        out: list[Job] = []
        for job in self.list(session_id):
            if not job.settled or job.notified or job.awaited or job.killed_by_model:
                continue
            job.notified = True
            out.append(job)
        return out

    def snapshot(self, session_id: str) -> list[dict[str, Any]]:
        """本会话的任务快照（给人与前端看）。**不含输出正文** —— 见 `as_dict`。"""
        return [as_dict(job) for job in self.list(session_id)]

    # -- 容量 ---------------------------------------------------------------
    def _prune(self, session_id: str) -> None:
        """每个会话只保留最近 MAX_JOBS_PER_SESSION 个任务，先淘汰结算最久的。

        跑飞的任务表同样是内存风险：一个会话起几百个后台命令，谁都记得住。
        正在跑的**绝不淘汰**（那会让 `job_output` 突然找不到活着的任务）。
        """
        mine = [self._jobs[jid] for jid in self._order if jid in self._jobs
                and self._jobs[jid].session_id == session_id]
        if len(mine) <= MAX_JOBS_PER_SESSION:
            return
        settled = sorted(
            (j for j in mine if j.settled), key=lambda j: j.finished_at or j.created_at
        )
        for job in settled[: len(mine) - MAX_JOBS_PER_SESSION]:
            self._jobs.pop(job.id, None)
            self._settle_events.pop(job.id, None)
            with contextlib.suppress(ValueError):
                self._order.remove(job.id)


# ----------------------------------------------------------------------
# 展示（工具层与通知共用同一套措辞，避免两处各写一份）
# ----------------------------------------------------------------------
def describe(job: Job) -> str:
    """一行摘要：`<id> [<kind>] <status> — <label>`（DSH 同款形状）。"""
    label = job.label if len(job.label) <= 80 else job.label[:77] + "…"
    return f"{job.id} [{job.kind}] {job.status} — {label}"


def notice_text(job: Job) -> str:
    """完成通知的正文（会作为一条 `[系统]` 消息进入下一步或唤醒的一轮）。"""
    return (
        f"[系统] 后台任务 {job.id}（{job.kind}：{job.label}）已结束 [{job.status}]。"
        f"用 job_output 读它的输出；不需要了就用 job_kill 停掉。"
        "不需要在下一步立刻处理它——除非你正等着它的结果。"
    )


def as_dict(job: Job) -> dict[str, Any]:
    """给 API / 前端用的形状。**刻意不含输出正文。**

    正文走 `job_output`（那是**模型**的通道，带消费游标与有损说明）；给人看的面
    只需要"有哪些任务、什么状态、产出了多少字节、有没有丢块"。
    把正文也塞进列表接口的话，一个跑飞的后台进程就能让一个 GET 返回几百 MB——
    那正是输出环当初要防的事（`MAX_RING_BYTES` 是 256 KiB，光这一条就够呛）。

    时间戳用 `finished_at=None` 表示"还在跑"，前端据此显示"进行中"。
    """
    return {
        "id": job.id,
        "kind": job.kind,
        "label": job.label,
        "status": job.status,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "buffered_bytes": job.buffered_bytes,
        "dropped_bytes": job.dropped_bytes,
        "kill_reason": job.kill_reason,
    }
