"""会话运行时（s07）：逻辑会话可恢复，运行时必须重建。

SessionRuntime —— 单个活跃会话的运行时状态：
- 事件订阅：WS 客户端 subscribe() 拿 asyncio.Queue，loop 事件实时扇出
- 忙碌保护：同一会话同时只允许一个 prompt 在跑（否则事件流交错）
- 审批桥接：loop 的 approver 回调 -> 落 approvals 表 -> 广播 approval_request
  （带 approval_id）-> 挂在 asyncio.Future 上等 REST 决策 -> 超时 fail closed

SessionManager —— 全局注册表：
- 会话元数据在 DB，运行时在内存；进程重启后按需惰性重建（s07 核心机制）
- 所有权校验：用户只能碰自己的会话
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from code_agent.agent import loop as loop_module
from code_agent.agent.breaker import FailureBreaker
from code_agent.agent.code_question import match_code_question
from code_agent.agent.execution_progress import ExecutionProgress
from code_agent.agent.loop import DEFAULT_SYSTEM_PROMPT, AgentLoop, LoopResult
from code_agent.agent.repeat_reminder import RepeatReminder
from code_agent.agent.router import ModelRouter
from code_agent.agent.routing import classify as route_task
from code_agent.agent.routing import route_hint
from code_agent.agent.workflow import WorkflowStep
from code_agent.config import agent_home, artifacts_dir, code_dir
from code_agent.models.events import Event, EventType, TokenUsage, new_id
from code_agent.providers.capacity import (
    context_window_for_model as _context_window,
    estimate_tokens as _est_tokens,
)
from code_agent.permissions.hooks import PERMISSION_MODES, ApprovalRequest, PermissionManager
from code_agent.jobs.registry import JobRegistry, notice_text
from code_agent.sandbox.persistent_shell import PersistentShells
from code_agent.tools.fs_observation import FsObservation
from code_agent.sessions.acceptance import (
    AcceptanceList,
    collect_evidence,
    failure_count,
    since_last_prompt,
)
from code_agent.sessions.checkpoints import CheckpointStore, report_text, rewind_to
from code_agent.sessions.rewind import RewindNotes, summarize as rewind_summary
from code_agent.storage.db import ApprovalRepo, SessionRepo
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.settings import disabled_tools
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry
from code_agent.tools.builtin.web_tools import WEB_SYSTEM_SUFFIX, WEB_TOOL_NAMES

logger = logging.getLogger(__name__)


def _provider_requires_attention(exc: Exception) -> bool:
    """Stop automatic calls for provider account errors, not tool failures.

    Older compatible providers expose only the HTTP status in LLMError text.
    Keep that fallback narrow until their errors carry structured metadata.
    """
    from code_agent.providers.openai_compat import LLMError

    if not isinstance(exc, LLMError):
        return False
    for name in ("status_code", "http_status", "status"):
        if getattr(exc, name, None) in (401, 402, 403):
            return True
    message = str(exc)
    return bool(re.search(r"\bHTTP\s+(?:401|402|403)(?!\d)", message, re.IGNORECASE)
                or re.search(r"(?<!\w)insufficient_quota(?!\w)", message, re.IGNORECASE))


#: 审批超时（秒）
APPROVAL_TIMEOUT = float(os.environ.get("CODE_AGENT_APPROVAL_TIMEOUT", "120"))
#: 提问超时（秒）：比审批长得多——审批是"能不能执行这条命令"，看一眼就能点；
#: 提问往往是要人读三四个选项、想清楚再答（截图里那种）。超时不是"拒绝"，
#: 而是"没得到回答"：工具会如实告诉模型没拿到答案，让它把问题写进结论。
QUESTION_TIMEOUT = float(os.environ.get("CODE_AGENT_QUESTION_TIMEOUT", "600"))
# Only used when operators opt into positive-length execution segments.
# The default core loop is continuous, like DSH; external budgets still apply.
MAX_CONTINUATIONS = int(os.environ.get("CODE_AGENT_MAX_CONTINUATIONS", "40"))
#: 硬上限：单次用户消息最多跑多少轮（跨段累计）。真跑飞了也不会无限烧钱。
#: 设 0 表示不设上限（与 DSH 一致）。
MAX_TOTAL_TURNS = int(os.environ.get("CODE_AGENT_MAX_TOTAL_TURNS", "1200"))
# Compatibility for integrations that used to disable the removed closure hook.
# Classic execution now ends naturally; CODE_AGENT_CLOSURE_LIMIT is ignored.
CLOSURE_LIMIT = 0
#: 空闲会话被"后台任务完成"唤醒一轮的**连续**次数上限（M45，对齐 DSH 的
#: `maxConsecutiveWakes`）。预算按用户消息重置——用户一旦自己说话，就重新允许为他的任务唤醒。
#:
#: **0 = 完全禁用自动唤醒**（不是"不限"）：通知仍然不会丢，只是改为在下次模型调用时
#: 注入。想要"绝不让会话自己动起来"的部署把它设 0 即可。这个 0 的读法与
#: `MAX_TOTAL_TURNS` 的"0 = 不设上限"**不同**，是刻意的：那里的 0 表示"我不设闸"，
#: 这里的 0 表示"我不要这个行为"——后者更符合"关掉它"的直觉。
MAX_CONSECUTIVE_WAKES = int(os.environ.get("CODE_AGENT_MAX_CONSECUTIVE_WAKES", "3"))
# ProjectTeam accepts at most 40,000 characters per activation. Apply the same
# bound at ordinary model boundaries, rather than injecting the entire inbox.
MAX_JOB_NOTICE_CHARS = 40_000
#: 每轮自动注入的知识库片段数（M12）
KB_TOP_K = int(os.environ.get("CODE_AGENT_KB_TOP_K", "4"))

# Team members share execution tools; live permission policies and project
# boundaries authorize each invocation. Planning remains strictly read-only.
from code_agent.agent.project_team import TEAM_TOOL_NAMES


def _trash_dir(path: Path) -> None:
    """把会话级目录移入同级 `.trash/`（人工可找回），而不是递归硬删。

    与轨迹回收（TranscriptStore.discard）同一套做法：rename 最稳，
    也天然避开部分运行环境对批量删除的守卫；移不动就留着，不影响功能。
    """
    path = Path(path)
    if not path.is_dir():
        return
    trash = path.parent / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = trash / f"{path.name}-{stamp}"
    n = 1
    while target.exists():
        target = trash / f"{path.name}-{stamp}-{n}"
        n += 1
    with contextlib.suppress(OSError):
        path.rename(target)

#: 默认工作区的显示名——是 folder="" 的**展示别名**，不能作为真实工作区名
#: （否则会出现「默认工作区」和 "" 两个看起来一样的分组）
DEFAULT_WORKSPACE_NAME = "默认工作区"


def _clean_workspace_name(name: str) -> str:
    """工作区名校验：去空白、限长、禁占位名与路径分隔符。"""
    clean = (name or "").strip()
    if not clean:
        raise ValueError("工作区名称不能为空")
    if clean == DEFAULT_WORKSPACE_NAME:
        raise ValueError(f"「{DEFAULT_WORKSPACE_NAME}」是保留名称，请换一个")
    if len(clean) > 60:
        raise ValueError("工作区名称不能超过 60 个字符")
    if any(ch in clean for ch in "/\\"):
        raise ValueError("工作区名称不能包含斜杠")
    return clean


class SessionBusy(Exception):
    pass


class SessionClosed(Exception):
    pass


class _JobWakePrompt(str):
    """Carry the selected notes until the first, post-compaction boundary."""

    def __new__(cls, notices: list[str]):
        prompt = super().__new__(cls, "\n\n".join(notices))
        prompt.notices = notices
        return prompt


class SessionRuntime:
    def __init__(
        self,
        session_id: str,
        user_id: str,
        transcript: TranscriptStore,
        session_repo: SessionRepo,
        approval_repo: ApprovalRepo,
        router: ModelRouter,
        registry: ToolRegistry,
        workspace: Path,
        extra_policies: dict[str, Any] | None = None,
        usage_repo: Any | None = None,   # UsageRepo，M5 s21
        audit_repo: Any | None = None,   # AuditRepo，M5 s22
        strict: bool = False,            # 严格模式：自动化用，审批一律 fail closed
        system_suffix: str = "",         # 技能/专家目录等附加 prompt（M10 修复：此前从未传入）
        knowledge: Any | None = None,    # KnowledgeBase，M12 知识库检索注入
        permission_mode: str = "workspace-write",  # 权限档位，随会话创建时选定
        memory_root: Path | None = None,           # 记忆落盘位置（代码模式放在仓库之外）
        session_memory_root: Path | None = None,
        web_enabled: bool = False,                 # 联网开关（M19）：关着时联网工具不暴露给模型
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.transcript = transcript
        self.session_repo = session_repo
        self.approval_repo = approval_repo
        self.router = router
        self.registry = registry
        self.agent_team: Any | None = None
        self._project_permission_provider: Callable[[], str] | None = None
        self._team_wake_blocked = False
        self._provider_wake_blocked = False
        self.workspace = workspace
        #: 工作区记忆的落盘根目录。**代码模式下必须与 workspace 解耦**：
        #: workspace 是用户的仓库/工作树，把平台的 MEMORY.md 与 daily log 写进去
        #: 会污染用户的 git 状态（真机上表现为任务审查里冒出"计划外：memory/xxx.md"）。
        self.memory_root = Path(memory_root) if memory_root else Path(workspace)
        self.session_memory_root = Path(session_memory_root) if session_memory_root else self.memory_root
        self.extra_policies: dict[str, Any] = dict(extra_policies or {})
        self.usage_repo = usage_repo
        self.audit_repo = audit_repo
        self.strict = strict
        self.knowledge = knowledge
        self.permission_mode = permission_mode if permission_mode in PERMISSION_MODES else "workspace-write"
        #: 联网开关（M19）。默认关：联网要花检索配额、还会把网页内容带进上下文，
        #: 必须由用户在输入框旁边显式打开——不做"默认替我上网"。
        self.web_enabled = bool(web_enabled)
        #: 文件观察记录（M41，对齐 `dsh-fs-observation-policy`）：「本会话读过哪些文件的
        #: 哪个版本」。持有在 **runtime** 上而不是 AgentLoop 上——loop 每段边界都重建，
        #: 挂在 loop 上等于每段丢一次记录、模型每段都要重读目标文件。
        #:
        #: 生命周期刻意与 DSH 一致：**只在内存里、不持久化**。runtime 重建
        #: （进程重启 / 重新打开会话）即清空，恢复后的会话必须重新读一遍——
        #: "我以为我记得那个文件长什么样"恰恰是这里最危险的假设。
        self.fs_observation = FsObservation()
        #: 后台任务表（M45，对齐 `dsh-jobs`）。持有在 runtime 上：会话级、进程内、**不落库**。
        #: 运行时重建（重启/重开）即清空——"我以为那个后台任务还在跑"是最危险的假设。
        #: 结算回调只做调度（繁忙留给下一步注入、空闲起一轮唤醒），不在这里等待。
        self.jobs = JobRegistry(on_settle=self._on_job_settled)
        #: 重复调用提醒（M47）。在 `_run_loop` 开头**重新建一个**（每条用户消息一次），
        #: 但跨段共用同一个实例——段边界会重建 AgentLoop，挂在 loop 上等于每段清零。
        self.repeat_reminder = RepeatReminder()
        self.execution_progress = ExecutionProgress(self.workspace)
        #: 持久 shell（M48）。会话级：跨调用保留 cwd / 导出的变量 / 函数。
        #: 懒起（第一次用到 `bash_persistent` 才 spawn）；停止会话时杀掉当前那只
        #: （`cancel`），运行时被释放时彻底关闭（`shutdown`）。
        self.shells = PersistentShells(cwd=self.workspace)
        #: 连续唤醒预算（M45）：用户自己发一条消息就重置（对齐 DSH 的
        #: "领取用户撰写的消息会重置该所有者的预算"）。
        self._consecutive_wakes = 0
        #: 唤醒预算用尽时暂存的通知（不能丢：否则"任务完成了"这件事只剩用户自己知道）。
        #: 下一次模型调用（或用户下一条消息触发的轮次）会把它带上。
        self._wake_notices: list[str] = []
        self._wake_batch: list[str] | None = None

        self._queues: list[asyncio.Queue] = []
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        # Live execution state, kept outside the transcript so reconnects show
        # the current stage rather than replaying an obsolete preparation state.
        self.phase = ""
        self.current_tool = ""
        self.last_progress_at = ""
        self._tool_calls: dict[str, str] = {}
        self._active_dispatches = 0
        self._dispatch_lock = asyncio.Lock()
        self._approval_capacity_suspended = False
        self._team_changed_callback: Any | None = None
        self._execution_budget = None
        # Successful provider responses are paid even if cancellation, a local
        # budget failure, or checkpoint persistence interrupts the prompt later.
        # This ledger belongs to one _run_loop invocation across all segments.
        self._prompt_usage: dict[str, Any] | None = None
        self._team_inbox = None
        if getattr(session_repo, "db", None) is not None:
            from code_agent.sessions.team_inbox import TeamInbox
            self._team_inbox = TeamInbox(session_repo.db, session_id)
            saved_events = self.transcript.replay(session_id)
            self._team_inbox.reconcile(saved_events)
            saved_budget = next((event.payload["execution_budget"] for event in reversed(saved_events)
                                 if event.type == EventType.session_meta and "execution_budget" in event.payload), None)
            if saved_budget:
                from code_agent.sessions.team_budget import TeamBudget
                restored_budget = TeamBudget(session_repo.db, session_id)
                meta = session_repo.get(session_id) or {}
                if restored_budget.restore(saved_budget, project_id=meta.get("project_id") or ""):
                    self._execution_budget = restored_budget
        self._pending_approvals: dict[str, asyncio.Future] = {}
        # The UI presents one concrete action at a time, including when a model
        # requests several tools in parallel. Queued requests start their own
        # approval timeout only after the previous card has been resolved.
        self._approval_lock = asyncio.Lock()
        #: 挂起中的提问（M31）：question_id -> Future（答案列表 / True 放弃）
        self._pending_questions: dict[str, asyncio.Future] = {}
        self._pending_question_payloads: dict[str, dict[str, Any]] = {}
        # 技能/专家目录等附加 prompt。M10 修复：此前这里恒为 ""，
        # 因为 SessionManager.get_runtime() 忘了把 system_suffix 传进来，
        # 导致 Skills/Experts 目录从未真正进过 system prompt。
        self._system_suffix = system_suffix or ""
        # M18 代码模式：会话级附加（作业规程 + 项目地图 digest）。
        # provider 返回 (索引版本, 文本)；版本变了才重建——地图随改动刷新，
        # 但不必每轮都重算 digest（那会白白花掉一次数据库查询 + 若干毫秒）。
        self.code_context_provider: Any | None = None
        #: 消息级上下文渲染回调（(code_context) -> str），由 SessionManager 注入
        self._code_context_provider_fn: Any | None = None
        #: 代码模式的项目句柄（M29 回退用）：成对检查点要能反向应用 patch 台账，
        #: 这就需要"这个会话属于哪个项目"以及 code_intel 服务。普通会话两者都是 None，
        #: 回退时只退对话、并如实说明"文件改动未回退"。
        self.code_project: dict[str, Any] | None = None
        self.code_intel: Any | None = None
        #: 任务类型路由（M29）：每条 prompt 重新分类，就近附着在 system prompt 上
        self._route_obj: Any | None = None
        self._code_question = None
        #: 本轮已记录的检查点（回退后要按需清理）
        self._checkpoint_seq: int = 0
        #: 本次 prompt 的起点时间（patch 台账按它过滤"这一轮的改动"，M29）
        self._prompt_started_at: str = ""
        #: 本次 prompt 的起始 seq，用于本轮事件归属。
        self._prompt_start_seq: int = 0
        self._code_suffix = ""
        self._code_suffix_version = -1

    @property
    def system_suffix(self) -> str:
        return self._system_suffix

    @property
    def model_name(self) -> str:
        """当前实际使用的模型名（M8：前端会话头显示 + 冒烟校验）。

        M36：会话指定了具体模型就用它，否则是设置里的默认模型。
        """
        try:
            return self.router.model_name()
        except Exception:  # 模型名只是展示信息，不能影响会话可用性
            return ""

    @system_suffix.setter
    def system_suffix(self, value: str) -> None:
        self._system_suffix = value or ""

    # ------------------------------------------------------------------
    # 代码模式上下文（M18 · P2）
    # ------------------------------------------------------------------
    def refresh_code_context(self, *, force: bool = False) -> str:
        """按需重建"作业规程 + 项目地图"。返回当前会话级附加文本。"""
        if self.code_context_provider is None:
            return self._code_suffix
        try:
            version, text = self.code_context_provider()
        except Exception:  # 地图取不到不影响对话（例如项目目录被移走）
            return self._code_suffix
        if force or version != self._code_suffix_version or text != self._code_suffix:
            self._code_suffix = text or ""
            self._code_suffix_version = version
        return self._code_suffix

    # ------------------------------------------------------------------
    # 事件订阅（WS 扇出）
    # ------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        with contextlib.suppress(RuntimeError):
            self._event_loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def broadcast(self, event: Event) -> None:
        owner_loop = self._event_loop
        if owner_loop is not None and owner_loop.is_running():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is not owner_loop:
                owner_loop.call_soon_threadsafe(self.broadcast, event)
                return
        old_phase = self.phase
        significant = False
        ledger = self._prompt_usage
        if (ledger is not None and event.seq > ledger["start_seq"]
                and event.type in (EventType.done, EventType.error)
                and isinstance(event.payload.get("usage"), dict)
                and event.seq not in ledger["reported_seqs"]):
            ledger["reported_seqs"].add(event.seq)
            reported = ledger["reported"]
            for name in ("input_tokens", "output_tokens", "cached_tokens"):
                setattr(reported, name, getattr(reported, name)
                        + max(0, int(event.payload["usage"].get(name, 0) or 0)))
        if event.type == EventType.session_meta and "phase" in event.payload:
            if event.payload.get("busy", True) and self.pending_questions():
                event = event.model_copy(update={"payload": {**event.payload, "phase": "waiting_question"}})
            elif event.payload.get("busy", True) and any(
                not future.done() for future in self._pending_approvals.values()
            ):
                event = event.model_copy(update={"payload": {**event.payload, "phase": "waiting_approval"}})
            self.phase = event.payload["phase"] or ""
            significant = self.phase != old_phase
        elif event.type == EventType.tool_use:
            call = event.payload.get("call") or {}
            self._tool_calls[str(call.get("id") or "")] = str(call.get("name") or "")
            self.current_tool = str(call.get("name") or "")
            self.phase = "waiting_question" if self.pending_questions() else "tools"
            significant = True
        elif event.type == EventType.tool_result:
            self._tool_calls.pop(str(event.payload.get("call_id") or ""), None)
            self.current_tool = next(iter(self._tool_calls.values()), "")
            significant = True
        elif event.type in (EventType.approval_request, EventType.approval_decided,
                            EventType.question_request, EventType.question_answered):
            significant = True
        elif event.type == EventType.user_message and self._team_inbox is not None:
            self._team_inbox.acknowledge(event.payload.get("inbox_ids") or [], event.seq)
        if event.type in (EventType.assistant_delta, EventType.thinking_delta) or significant:
            self.last_progress_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        for q in list(self._queues):
            q.put_nowait(event)
        if significant and self._team_changed_callback is not None:
            self._team_changed_callback(self.session_id)

    def _set_phase(self, phase: str, *, busy: bool = True) -> None:
        self.broadcast(Event(seq=0, type=EventType.session_meta,
                             payload={"phase": phase, "busy": busy}))

    # ------------------------------------------------------------------
    # prompt 执行
    # ------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def submit(
        self,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
        code_context: dict[str, Any] | None = None,
        web: bool | None = None,
    ) -> LoopResult:
        """提交一轮对话。attachments 是已上传到工作区的附件清单（API 层负责落盘）。

        code_context（M18）是"用户此刻盯着什么"的结构坐标：前端只传 id/行区间，
        事实由服务端从符号索引解析成文本块，只进发给模型的 prompt。

        web（M19）是本轮的「联网」开关：显式给了就以它为准（前端每次发消息都带，
        避免"刚点开关就发送"的竞态），没给则沿用会话当前设置。
        """
        if self.busy:
            raise SessionBusy("会话正在处理上一个请求")
        self._event_loop = asyncio.get_running_loop()
        self._team_wake_blocked = False
        # Explicit submissions/follow-ups may retry after the account is fixed.
        # Completion receipts alone must not clear a provider account failure.
        self._provider_wake_blocked = False
        self._tool_calls.clear()
        self.current_tool = ""
        if web is not None:
            self.web_enabled = bool(web)
        # M45：用户自己说话了 → 唤醒预算重置（对齐 DSH：用户撰写的消息重置所有者预算）
        self._consecutive_wakes = 0
        self.session_repo.touch(self.session_id, title=prompt[:40] if prompt else None)
        run = self.agent_team.run(self, prompt, attachments, code_context) if self.agent_team is not None else self._run_loop(prompt, attachments, code_context)
        self._task = asyncio.create_task(run)
        task = self._task
        self._set_phase("", busy=True)
        try:
            result = await task
        finally:
            if self._task is task:
                self._set_phase("", busy=False)
                # A receipt can arrive while the provider is busy and then the
                # provider can fail. The exception must not skip its handoff.
                if (not task.cancelled() and not self._team_wake_blocked
                        and not self._provider_wake_blocked
                        and (self._wake_notices or self._pending_team_receipts())):
                    asyncio.get_running_loop().call_soon(self._wake_for_jobs)
        return result

    def cancel(self) -> None:
        """用户主动停止：杀掉执行任务，并把挂起中的审批与提问一并收掉。

        不清 _pending_approvals 的话，future 悬着——任务取消后审批表里那条
        记录永远停在 pending，前端恢复弹窗的逻辑会被它误触发。
        提问同理：任务被 cancel 时 `_ask_user` 里的 await 会收到 CancelledError
        并落一条 `status=cancelled` 的回答事件（界面上那张卡随之消失），
        这里不 pre-set 结果——让取消按正常路径往上走比硬塞一个答案更干净。
        """
        self._team_wake_blocked = True
        if self.agent_team is not None:
            self._wake_notices.clear()
            self.agent_team.cancel_for(self.session_id)
        if self.busy and self._task is not None:
            self._task.cancel()
        for approval_id, fut in list(self._pending_approvals.items()):
            if not fut.done():
                fut.set_result(False)
                self.approval_repo.decide(approval_id, "denied", decided_by=f"{self.user_id} (cancel)")
        self._pending_approvals.clear()
        self._pending_questions.clear()
        self._pending_question_payloads.clear()
        # M48：持久 shell 是**会话级**的长驻进程。这里只杀"当前这一只"——
        # 正在跑的那条命令必须立刻停（否则等于停止没生效），但会话没结束，
        # 用户下一条消息里仍应能用持久 shell。**永久释放**走 shutdown()。
        self.shells.kill_nowait()

    def interrupt_current_turn(self) -> None:
        """Cancel this session turn without recursively stopping descendants."""
        self._team_wake_blocked = True
        if self.busy and self._task is not None:
            self._task.cancel()
        for approval_id, future in list(self._pending_approvals.items()):
            if not future.done():
                future.set_result(False)
                self.approval_repo.decide(approval_id, "denied", decided_by=f"{self.user_id} (interrupt)")
        self._pending_approvals.clear()
        self._pending_questions.clear()
        self._pending_question_payloads.clear()
        self.shells.kill_nowait()

    def set_delegation_context(self, parent_session_id: str, strategy: str, prompt: str) -> None:
        from code_agent.sessions.delegation_context import delegation_snapshot
        parent = self.session_repo.get(parent_session_id)
        child = self.session_repo.get(self.session_id)
        if not parent or not child or parent["user_id"] != self.user_id or parent.get("project_id") != child.get("project_id"):
            raise ValueError("委派上下文来源不属于当前项目")
        snapshot = delegation_snapshot(self.transcript.replay(parent_session_id), strategy=strategy,
                                       prompt=prompt, parent_session_id=parent_session_id)
        self.transcript.append(self.session_id, EventType.session_meta, {"delegation_context": snapshot})

    def enqueue_agent_message(self, message: dict[str, Any]) -> dict:
        """Persist a message; it is consumed at a model boundary, never wakes idle agents."""
        if self._team_inbox is None:
            raise RuntimeError("当前会话没有持久消息存储")
        row = self._team_inbox.enqueue("agent_message", message)
        self.broadcast(self.transcript.append(self.session_id, EventType.session_meta,
                                              {"agent_message": {**message, "message_id": row["id"], "status": "queued"}}))
        return {"message_id": row["id"], "status": "delivered" if row["delivered_seq"] else "queued"}

    def _pending_team_receipts(self) -> bool:
        return bool(self._team_inbox is not None and self._team_inbox.pending(receipts_only=True))

    def set_execution_budget(self, budget: dict | None, *, activation_id: int, parent_session_id: str = "",
                             continuation: bool = False) -> None:
        from code_agent.sessions.team_budget import TeamBudget
        if self.agent_team is None:
            return
        member = self.agent_team._member(self.session_id, self.user_id)
        if member["activation_id"] != activation_id:
            raise ValueError("任务预算必须绑定当前执行轮次")
        if self._execution_budget is None:
            self._execution_budget = TeamBudget(self.session_repo.db, self.session_id)
        parent = None
        if parent_session_id:
            parent_member = self.agent_team._member(parent_session_id, self.user_id)
            parent_runtime = self.agent_team.manager._runtimes.get(parent_session_id)
            if (parent_member["project_id"] == member["project_id"]
                    and parent_member["lead_session_id"] == member["lead_session_id"]
                    and self.session_id in self.agent_team._subtree(parent_session_id)
                    and parent_runtime is not None
                    and parent_runtime.busy and parent_runtime._execution_budget is not None):
                candidate = parent_runtime._execution_budget.current
                if candidate and candidate["activation_id"] == parent_member["activation_id"]:
                    parent = candidate
        record = self._execution_budget.begin(member["project_id"], activation_id, budget, parent=parent,
                                              continuation=continuation)
        self.transcript.append(self.session_id, EventType.session_meta, {"execution_budget": record})

    def execution_budget_status(self) -> dict:
        return self._execution_budget.status() if self._execution_budget is not None else {}

    def remaining_execution_seconds(self) -> float | None:
        return self._execution_budget.remaining_seconds() if self._execution_budget else None

    async def before_model_turn(self) -> None:
        if self._execution_budget is not None:
            self._execution_budget.check()

    def on_model_usage(self, usage: TokenUsage) -> None:
        # Account for the response before a budget-ledger write can fail closed.
        # Reporting/replaying this ledger never consumes the execution budget.
        ledger = self._prompt_usage
        if ledger is not None:
            ledger["responses"] += 1
            total = ledger["total"]
            for name in ("input_tokens", "output_tokens", "cached_tokens"):
                setattr(total, name, getattr(total, name)
                        + max(0, int(getattr(usage, name, 0) or 0)))
        if self._execution_budget is not None:
            self._execution_budget.consume(usage)

    def _settle_prompt_usage(self, ledger: dict, result: LoopResult | None,
                             prompt: str, outcome: str) -> None:
        """Record once and publish only usage missing from segment terminals."""
        if ledger["settled"]:
            return
        ledger["settled"] = True
        total = ledger["total"]
        if not ledger["responses"]:
            # Preserve custom/fake loop compatibility when the response callback
            # was not used. Actual AgentLoop responses always use the ledger.
            fallback = result.usage if result is not None else ledger["reported"]
            total = fallback.model_copy(deep=True)
        if result is not None:
            result.usage = total.model_copy(deep=True)
        recorded = result or LoopResult(text="", stop_reason=outcome,
                                        turns=ledger["responses"], usage=total)
        self._record_usage(recorded, prompt)
        delta = {name: max(0, getattr(total, name) - getattr(ledger["reported"], name))
                 for name in ("input_tokens", "output_tokens", "cached_tokens")}
        try:
            self._emit_persisted(EventType.session_meta, {"usage_reconciliation": {
                "prompt_id": ledger["id"], "usage": total.model_dump(),
                "delta": delta, "outcome": outcome,
                "model_responses": ledger["responses"],
                "duration_seconds": round(max(0.0, time.monotonic() - ledger["started_at"]), 3),
            }})
        except Exception:
            # Telemetry must not replace the cancellation/provider failure that
            # caused the prompt to finish; its DB usage was already recorded.
            logger.warning("Could not publish prompt usage reconciliation")

    async def _suspend_approval_capacity_locked(self) -> None:
        if self.agent_team is not None and self._pending_approvals and not self._active_dispatches and not self._approval_capacity_suspended:
            self._approval_capacity_suspended = await self.agent_team.suspend_for_approval(self)

    async def before_tool_dispatch(self) -> None:
        async with self._dispatch_lock:
            if self._approval_capacity_suspended and self.agent_team is not None:
                try:
                    await self.agent_team.resume_after_approval(self, True)
                finally:
                    self._approval_capacity_suspended = False
            self._active_dispatches += 1

    async def after_tool_dispatch(self) -> None:
        async with self._dispatch_lock:
            self._active_dispatches = max(0, self._active_dispatches - 1)
            task = asyncio.current_task()
            if task is None or not task.cancelling():
                await self._suspend_approval_capacity_locked()

    def shutdown(self) -> None:
        """永久释放运行时持有的长驻资源（会话被关闭/归档/删除/改绑定时调用）。

        与 `cancel()` 的区别是**语义**而非程度：cancel 是"停这一次"，
        shutdown 是"这个运行时不会再被用了"——所以持久 shell 在这里彻底关掉，
        之后再调 `bash_persistent` 会如实说"会话运行时已释放"。
        """
        self._team_wake_blocked = True
        self.shells.close_nowait()

    def pending_approval(self) -> dict | None:
        """当前挂起中的审批（前端刷新后恢复弹窗用）。

        _server_approver 广播的 approval_request 是 seq=0 不落盘的，刷新后
        重放不到；而 loop 落盘的那条不带 approval_id 点不了按钮——所以
        用 approvals 表恢复展示信息，并核对仍有活着的等待者。重启后遗留的
        pending 记录和已超时请求不能成为可点击的授权。
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=APPROVAL_TIMEOUT)
        ).isoformat(timespec="seconds")
        fresh = [
            row
            for row in self.approval_repo.pending_for_session(self.session_id)
            if (row.get("created_at") or "") >= cutoff
            and (future := self._pending_approvals.get(row["id"])) is not None
            and not future.done()
        ]
        if not fresh:
            return None
        row = fresh[-1]
        # 字段形状与 _server_approver 广播的 payload 保持一致，
        # 前端恢复弹窗时不用区分"实时收到"还是"刷新兜底"
        return {
            "approval_id": row["id"],
            "tool": row.get("tool_name") or "",
            "arguments": row.get("command") or "",
            "reason": row.get("reason") or "",
            "purpose": row.get("purpose") or "",
            "effects": row.get("effects") or [],
            "purpose_source": row.get("purpose_source") or "missing",
            "status": row.get("status") or "pending",
            "scope": "once",
            "timeout": APPROVAL_TIMEOUT,
        }

    async def _run_loop(
        self,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
        code_context: dict[str, Any] | None = None,
        auto_meta: dict[str, Any] | None = None,
    ) -> LoopResult:
        ledger = {"id": new_id("prompt_usage_"), "total": TokenUsage(),
                  "reported": TokenUsage(), "reported_seqs": set(),
                  "start_seq": self.transcript.last_seq(self.session_id),
                  "responses": 0, "settled": False, "started_at": time.monotonic()}
        if self._prompt_usage is not None:
            raise SessionBusy("会话正在处理上一个请求")
        self._prompt_usage = ledger
        deadline = None
        result = None
        outcome = "error"
        try:
            if self.agent_team is not None:
                member = self.agent_team._member(self.session_id, self.user_id)
                current = self._execution_budget.current if self._execution_budget else None
                if not current or current["activation_id"] != member["activation_id"]:
                    self.set_execution_budget(None, activation_id=member["activation_id"],
                                              continuation=bool(auto_meta and auto_meta.get("auto")))
            remaining = self._execution_budget.remaining_seconds() if self._execution_budget else None
            deadline = asyncio.timeout(max(0, remaining)) if remaining is not None else None
            if deadline is None:
                result = await self._run_loop_impl(prompt, attachments, code_context, auto_meta)
            else:
                async with deadline:
                    result = await self._run_loop_impl(prompt, attachments, code_context, auto_meta)
            outcome = "completed"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TimeoutError:
            if deadline is not None and deadline.expired():
                from code_agent.sessions.team_budget import ExecutionBudgetExceeded
                self._emit_persisted(EventType.error, {"message": "任务或团队总耗时预算已达到上限", "code": "execution_budget_exceeded"})
                raise ExecutionBudgetExceeded("任务或团队总耗时预算已达到上限") from None
            raise
        except Exception as exc:
            if _provider_requires_attention(exc):
                self._provider_wake_blocked = True
            raise
        finally:
            try:
                self._settle_prompt_usage(ledger, result, prompt, outcome)
            finally:
                self._prompt_usage = None

    async def _run_loop_impl(
        self,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
        code_context: dict[str, Any] | None = None,
        auto_meta: dict[str, Any] | None = None,
    ) -> LoopResult:
        """一条 prompt 的执行入口：准备上下文并执行普通模型/工具循环。

        M29 在这里挂三样东西，它们都是"一条 prompt"的生命周期：
        · **任务类型路由**：按 prompt 分类，把一句"这类任务通常只需要哪些工具"写进
          system prompt（只提示，不摘工具）；
        · **熔断器**：跨段/跨轮共用同一个计数，否则每段重置等于没熔断；
        · **起始检查点**：记在 prompt 落轨迹**之前**，所以"回退到这条消息"能把它一起撤掉。

        `auto_meta`（M45）用于**系统自己发起**的轮次（后台任务完成唤醒）：带上
        `auto/quiet` 之后，界面上那条消息渲染成一行淡字而不是"用户说的话"，
        同时它仍然落轨迹、刷新后仍在。
        """
        self._event_loop = asyncio.get_running_loop()
        self._set_phase("preparing_context")
        self._route_for_prompt(prompt)
        self._code_question = (match_code_question(prompt)
                               if not auto_meta and not attachments else None)
        # M47：重复调用提醒**每条用户消息重置一次**（跨段共用，见 __init__ 的说明）。
        # 用户新说一句话就是新的起点，把上一条消息里的重复计数带过来会误报。
        self.repeat_reminder = RepeatReminder()
        self.execution_progress = ExecutionProgress(self.workspace)
        #: 本次 prompt 的起点（patch 台账的时间过滤条件）。台账 ts 是秒级 ISO 串，
        #: 用 >= 比较，所以同秒内落下的改动也归本轮。
        self._prompt_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        #: 保存本次 prompt 的事件起点，不把自动通知当作新用户任务。
        self._prompt_start_seq = self.transcript.last_seq(self.session_id)
        self._record_checkpoint(kind="prompt", label=f"发消息前：{prompt[:60]}", before_append=True)
        breaker = FailureBreaker()
        history, memory = await self._prepare_context(prompt)
        context_blocks, context_meta = await asyncio.to_thread(self._code_context_blocks, prompt, code_context)
        if auto_meta:
            context_meta = {**(context_meta or {}), **auto_meta}
        result = await self._run_execution(
            prompt, history, memory, attachments, context_blocks, context_meta, breaker
        )
        self._record_checkpoint(kind="segment", label="本轮结束")
        return result

    # ------------------------------------------------------------------
    # M45 后台任务：完成通知的两条投递路（下一步注入 / 空闲唤醒）
    # ------------------------------------------------------------------
    def _drain_job_notes(self, *, receipts_only: bool = False,
                         max_chars: int = MAX_JOB_NOTICE_CHARS,
                         exclude_inbox_ids: frozenset[str] = frozenset()) -> list[str]:
        """取"该告诉模型的完成通知"。由 AgentLoop 在**每次模型调用之前**调用。

        与空闲唤醒共用 `jobs.drain_notices`（它会把任务标记成已通知），
        所以同一条通知只可能走两条路之一，绝不会既注入又唤醒。
        """
        # A wake uses a short trigger prompt. Deliver its exact selected batch
        # here, after AgentLoop has compacted history, so no receipt is silently
        # shortened and acknowledged before the model can see it. Only use the
        # remaining allowance for messages/jobs that arrived in the meantime.
        if self._wake_batch is not None:
            batch, self._wake_batch = self._wake_batch, None
            used = sum(len(note) for note in batch) + max(0, len(batch) - 1) * 2
            identities = frozenset(identity for note in batch for identity in getattr(note, "inbox_ids", []))
            return batch + self._drain_job_notes(receipts_only=receipts_only,
                max_chars=max(0, max_chars - used - 2), exclude_inbox_ids=identities)
        if max_chars <= 0:
            return []
        try:
            fresh = [notice_text(job) for job in self.jobs.drain_notices(self.session_id)]
        except Exception:  # noqa: BLE001 —— 通知失败不该打断这一轮
            logger.exception("后台任务通知收集失败（忽略）")
            fresh = []
        self._wake_notices.extend(fresh)
        # Durable notices are acknowledged by broadcast only once their data
        # has been appended to the model history. Keep them pending across a
        # cancelled wake, a restart, and exhausted automatic wake budgets.
        pending = [text for text in self._wake_notices if not getattr(text, "inbox_ids", None)]
        inbox = [self._team_inbox.text(row) for row in self._team_inbox.pending(receipts_only=receipts_only)
                 if row["id"] not in exclude_inbox_ids] if self._team_inbox else []
        selected: list[str] = []
        used = taken = 0
        for index, original in enumerate(pending + inbox):
            note = original
            if len(note) > MAX_JOB_NOTICE_CHARS:
                # Legacy inbox rows/job labels can exceed the current bounds.
                # Keep their complete text in readable system storage and send
                # an explicitly labelled excerpt, preserving delivery identity.
                from code_agent.context.externalize import externalize_result
                from code_agent.context.safety import wrap_untrusted
                from code_agent.sessions.team_inbox import InboxNote

                identities = getattr(note, "inbox_ids", [])
                try:
                    excerpt = externalize_result(str(note), self.session_id,
                        identities[0] if identities else "job_notice",
                        threshold=MAX_JOB_NOTICE_CHARS // 2)
                except OSError:
                    logger.exception("超长后台通知保存失败，保留待下次投递")
                    break
                rendered = wrap_untrusted(excerpt, "超长后台通知摘录，完整内容见存盘路径")
                if identities:
                    note = InboxNote(rendered, identities[0])
                    note.inbox_ids = list(identities)
                else:
                    note = rendered
            cost = len(note) + (2 if selected else 0)
            if used + cost > max_chars:
                break
            selected.append(note)
            used += cost
            if index < len(pending):
                taken += 1
        self._wake_notices = pending[taken:]
        return selected

    def _restore_job_notes(self, notices: list[str]) -> None:
        # Durable notes were only read, never removed from their inbox.
        self._wake_notices[:0] = [note for note in notices if not getattr(note, "inbox_ids", None)]

    def enqueue_team_note(self, notice: dict[str, Any]) -> None:
        """Keep a completion receipt and deliver it at a boundary or idle wake."""
        if not isinstance(notice, dict):
            return
        from code_agent.context.safety import wrap_untrusted

        text = wrap_untrusted(json.dumps(notice, ensure_ascii=False), "子智能体执行回执")
        if self._team_inbox is not None:
            key = f"receipt:{notice.get('session_id', '')}:{notice.get('activation_id', '')}:{notice.get('state', '')}"
            row = self._team_inbox.enqueue("team_notice", notice, dedupe_key=key)
            if not row["inserted"]:
                return
        event = self.transcript.append(self.session_id, EventType.session_meta, {"team_notice": notice})
        self.broadcast(event)
        if self._team_inbox is None:
            self._wake_notices.append("[智能体完成通知 · 结合当前任务核验并交付，复用已有有效证据]\n" + text)
        if not self.busy and not self._team_wake_blocked:
            try:
                asyncio.get_running_loop().call_soon(self._wake_for_jobs)
            except RuntimeError:
                pass

    def _on_job_settled(self, job) -> None:
        """任务结算回调（在任务自己的协程里被**同步**调用）。只调度，不等待。"""
        if job.awaited or job.killed_by_model or job.notified:
            return
        if self.busy:
            # 繁忙：loop 会在下一个轮次边界取走（见 _drain_job_notes），这里不去抢。
            # 抢的后果是把通知塞进一个正在跑的轮次的中间，顺序会乱。
            return
        self._wake_for_jobs()

    def _wake_for_jobs(self) -> None:
        """空闲会话收到完成通知 → 自己起一轮（对齐 DSH 的 follow-up 唤醒）。

        为什么空闲也必须叫醒：**没人收的结算 = 模型永远不会知道的完成**。
        用户下一条消息里等的是一个早就跑完的任务，而模型还以为它在跑。
        """
        if self.busy or self._provider_wake_blocked or self._team_wake_blocked:
            return
        if self.agent_team is not None:
            member = self.agent_team.descriptor(self.session_id)
            if member and member["state"] in ("queued", "running", "waiting", "retired"):
                return
        notices = self._drain_job_notes(receipts_only=True)
        if not notices:
            return
        if self._consecutive_wakes >= MAX_CONSECUTIVE_WAKES:
            # 预算用尽（含 MAX_CONSECUTIVE_WAKES=0 的"禁用自动唤醒"）：不再自动起轮
            # （否则会话会自己烧钱），但通知**不能消失**——暂存起来，下次模型调用时带上。
            self._restore_job_notes(notices)
            self.broadcast(
                Event(
                    seq=0,
                    type=EventType.error,
                    payload={
                        "message": (
                            f"[jobs] 后台任务已完成，但自动唤醒已达上限（{MAX_CONSECUTIVE_WAKES} 次），"
                            "不再自动起轮。下次你发消息时会带上这些通知。"
                        ),
                        "code": "job_wake_budget_exhausted",
                        "quiet": True,
                    },
                )
            )
            return
        try:
            loop = asyncio.get_running_loop()
            inbox_ids = [identity for note in notices for identity in getattr(note, "inbox_ids", [])]
            prompt = _JobWakePrompt(notices)
            wake = self._run_wake(prompt, inbox_ids=inbox_ids) if inbox_ids else self._run_wake(prompt)
            self._task = loop.create_task(wake)
            self._consecutive_wakes += 1
        except RuntimeError:
            # 没有运行中的事件循环（任务在循环外结算）：留给下一次模型调用
            self._restore_job_notes(notices)

    async def _run_wake(self, prompt: str, *, inbox_ids: list[str] | None = None) -> LoopResult | None:
        """跑一轮"系统自己发起"的唤醒轮（带 auto/quiet，界面渲染成一行淡字）。"""
        batch = getattr(prompt, "notices", None)
        if batch is not None:
            self._wake_batch = list(batch)
            prompt = "后台任务已完成，请结合当前任务核验并交付，复用已有有效证据。"
        try:
            metadata = {"auto": True, "quiet": True, "kind": "job_notice",
                        "source": {"kind": "tool-jobs", "form": "notice", "summary": "后台执行结果已更新"}}
            if inbox_ids and batch is None:
                metadata["inbox_ids"] = inbox_ids
            if self.agent_team is not None:
                return await self.agent_team.run(self, prompt, auto_meta=metadata)
            return await self._run_loop(prompt, auto_meta=metadata)
        except Exception:  # noqa: BLE001 —— 唤醒轮失败不该把会话搞崩
            logger.exception("后台任务唤醒轮失败（忽略）")
            return None
        finally:
            if self._wake_batch is not None:
                self._restore_job_notes(self._wake_batch)
                self._wake_batch = None
            self._set_phase("", busy=False)
            task = asyncio.current_task()
            if ((self._wake_notices or self._pending_team_receipts())
                    and not (task and task.cancelling())
                    and not self._team_wake_blocked and not self._provider_wake_blocked):
                asyncio.get_running_loop().call_soon(self._wake_for_jobs)

    # ------------------------------------------------------------------
    # M29 任务类型路由 / 检查点 / 回退
    # ------------------------------------------------------------------
    def _route_for_prompt(self, prompt: str) -> None:
        """按 prompt 分类，把 Route 记在实例上。

        渲染放在 `_build_loop`：只有那里知道**哪些工具真的可用**（联网开关、一键
        禁用都会摘工具），提示里绝不能出现模型根本调不到的工具名。
        分类失败一律当作"没有提示"，不影响主流程。
        """
        try:
            self._route_obj = route_task(prompt)
        except Exception:
            logger.exception("任务类型路由失败（忽略）")
            self._route_obj = None

    def checkpoints(self, limit: int = 200) -> list[dict[str, Any]]:
        """本会话的检查点（前端按 anchor_seq 挂在对应那一行上）。"""
        return CheckpointStore(self.session_id).list(limit=limit)

    def _record_checkpoint(
        self, *, kind: str, label: str = "", before_append: bool = False
    ) -> dict[str, Any] | None:
        """记一条检查点：对话位置 + 这一步落下的改动。

        `before_append=True` 用于"发消息前"：此时轨迹里还没有这条 prompt，
        keep_seq 指向它的前一条，所以回退能把这条 prompt 一起撤掉；此时**不记改动**
        （还没开始干活），这一步的改动由本轮结束时的 segment 检查点登记。
        """
        try:
            store = CheckpointStore(self.session_id)
            keep_seq = self.transcript.last_seq(self.session_id)
            patches = [] if before_append else self._session_patches()
            anchor_seq = keep_seq + 1 if before_append else keep_seq
            item = store.record(
                keep_seq=keep_seq,
                kind=kind,
                label=label,
                anchor_seq=anchor_seq,
                patches=patches,
            )
            self._checkpoint_seq = keep_seq
            return item
        except Exception:
            logger.exception("检查点记录失败（忽略）")
            return None

    def _session_patches(self) -> list[dict[str, Any]]:
        """本次 prompt 期间该会话落下的改动（从 patch 台账里取）。

        用台账而不是"自己记一笔"：edit_file 记录的是**真实写盘**，任何旁路
        （插件工具、回滚本身）都会被台账看见；自己记一笔必然漏。
        """
        if self.code_intel is None or self.code_project is None or not self._prompt_started_at:
            return []
        try:
            store = self.code_intel.patches(self.code_project["id"])
            return store.list_by_session(self.session_id, since=self._prompt_started_at)
        except Exception:
            logger.exception("读取会话改动台账失败（回退将只撤对话）")
            return []

    def rewind(self, checkpoint_id: str) -> dict[str, Any]:
        """回退到某条检查点：先撤文件改动，再截轨迹，最后写"回退记忆"。

        不在这里广播事件——广播交给 API 层（它知道要不要落轨迹）。但回退本身
        必须留下**可回放**的痕迹，所以 API 层会补一条 `rewind` 事件。
        """
        if self.busy:
            raise SessionBusy("会话正在处理请求，先停止再回退")
        store = CheckpointStore(self.session_id)
        target = store.get(checkpoint_id)
        if target is None:
            raise KeyError(f"检查点不存在或已失效：{checkpoint_id}")
        all_ck = store.list()

        def _revert(patch_id: str) -> dict[str, Any]:
            if self.code_intel is None or self.code_project is None:
                raise ValueError("本次会话没有改动台账")
            return self.code_intel.revert_patch(self.code_project, patch_id)

        result = rewind_to(
            target,
            checkpoints=all_ck,
            transcript=self.transcript,
            session_id=self.session_id,
            revert_patch=_revert if (self.code_intel and self.code_project) else None,
        )
        note = rewind_summary(
            result.get("events") or [],
            reverted=result.get("reverted") or [],
            failed=result.get("failed") or [],
            keep_seq=result.get("keep_seq") or 0,
        )
        try:
            RewindNotes(self.session_id).append(
                note,
                meta={"checkpoint": checkpoint_id, "kind": target.get("kind") or ""},
            )
        except Exception:
            logger.exception("回退记忆落盘失败（忽略）")
        store.drop_after(int(target.get("keep_seq") or 0))
        result["note"] = note
        result["report"] = report_text(result)
        return result

    def acceptance(self) -> dict[str, Any]:
        """当前验收清单 + 最新一次判定（前端展示"做到哪儿了"）。"""
        lst = AcceptanceList.load(self.session_id)
        if lst is None:
            return {"declared": False, "items": []}
        events = self.transcript.replay(self.session_id)
        report = lst.evaluate(
            collect_evidence(
                events,
                workspace=self.workspace,
                # 本轮失败数以真实工具轨迹为准。
                failures=failure_count(events, since_seq=since_last_prompt(events)),
            )
        )
        return {"declared": True, "note": lst.note, "created_at": lst.created_at, **report.to_dict()}

    def set_acceptance(self, items: Any, note: str = "") -> dict[str, Any]:
        """写入有序验收清单（用户在界面/接口上直接声明，模型也可以用 acceptance_set）。"""
        lst = AcceptanceList.parse(items, note=note)
        if not lst.items:
            raise ValueError("没有一条合法判据（check 类型见 acceptance_check 的说明）")
        lst.save(self.session_id)
        return self.acceptance()

    def clear_acceptance(self) -> bool:
        return AcceptanceList.clear(self.session_id)

    def _code_context_blocks(
        self, prompt: str, code_context: dict[str, Any] | None
    ) -> tuple[list[str], dict[str, Any]]:
        """准备发给模型的上下文块（只进 model-facing prompt）。

        - 会话级（规程 + 地图）挂在 system prompt 上，见 `_build_loop`；
        - 消息级（当前坐标）在这里渲染，随本轮 prompt 一起发。
        两处都不改展示文本，只在事件里留 `code_context` 元数据供回放。
        """
        blocks: list[str] = []
        meta: dict[str, Any] = {}
        if code_context:
            meta["code_context"] = code_context
            provider = getattr(self, "_code_context_provider_fn", None)
            if provider is not None:
                try:
                    block = provider(code_context)
                except Exception:
                    block = ""
                if block:
                    blocks.append(block)
            if self.agent_team is not None:
                from code_agent.context.safety import wrap_untrusted

                member = self.agent_team.descriptor(self.session_id)
                node_id = code_context.get("workflow_node_id")
                tree = code_context.get("workflow_tree_id") or "main"
                if member and member["role"] == "lead" and isinstance(node_id, str) and isinstance(tree, str) and re.fullmatch(r"(?:main|task:[A-Za-z0-9_-]{1,96})", tree):
                    document = self.agent_team.workflow_store.get(member["project_id"], tree)
                    node = next((item for item in document["nodes"] if item["id"] == node_id), None)
                    if node:
                        intent = {"workflow_node_id": node_id, "workflow_tree_id": tree, "node": node}
                        blocks.append(wrap_untrusted(json.dumps(intent, ensure_ascii=False), "用户选择的工作流节点任务资料：由当前对话主助手按需直接执行或委派"))
        return blocks, meta

    async def _prepare_context(self, prompt: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """M3 上下文管线：历史重建 -> 记忆检索 -> 知识库注入。"""
        from code_agent.context.prompt import build_history, knowledge_blocks, memory_blocks
        from code_agent.context.safety import data_message
        from code_agent.memory.store import UserMemory, WorkspaceMemory

        # M3 上下文管线：历史重建 -> 记忆检索 -> 交给 loop 组装
        prior_events = self.transcript.replay(self.session_id)
        if self._team_inbox is not None:
            self._team_inbox.reconcile(prior_events)
        history = build_history(prior_events)

        def read_memory_records():
            meta = self.session_repo.get(self.session_id) or {}
            project_id = meta.get("project_id") or ""
            kind = "project" if project_id else "session"
            label = (self.code_project or {}).get("name") or ("当前项目" if project_id else meta.get("title") or "当前会话")
            workspace_records = [{**entry, "scope": kind, "label": label}
                                 for entry in WorkspaceMemory(self.memory_root).active_records()]
            if self.session_memory_root.resolve() != self.memory_root.resolve():
                workspace_records += [{**entry, "scope": "session", "label": meta.get("title") or "当前会话"}
                                      for entry in WorkspaceMemory(self.session_memory_root).active_records()]
            personal = [{**entry, "scope": "user", "label": "个人偏好"}
                        for entry in UserMemory(self.user_id).active_records()]
            return workspace_records, personal

        ws_entries, user_entries = await asyncio.to_thread(read_memory_records)
        memory = memory_blocks(ws_entries, user_entries, query=prompt)

        snapshot = next((event.payload["delegation_context"] for event in reversed(prior_events)
                         if event.type == EventType.session_meta and "delegation_context" in event.payload), None)
        if snapshot:
            strategy = snapshot.get("strategy", "none")
            if strategy == "all":
                history = list(snapshot.get("history") or []) + history
                memory.append(data_message(json.dumps({key: snapshot.get(key) for key in ("strategy", "parent_session_id", "source_seq", "task")},
                                                     ensure_ascii=False), label="委派上下文来源"))
            elif strategy == "task_brief":
                memory.append(data_message(json.dumps(snapshot, ensure_ascii=False), label="父智能体任务摘要"))

        # Long conversations get a persistent, source-attributed work record.
        # It is deterministic evidence extraction; full transcripts remain the
        # source of truth and no extra model call is needed.
        from code_agent.context.compact import build_work_checkpoint, work_checkpoint_message
        from code_agent.context.prompt import history_budget
        previous = next((event.payload["work_checkpoint"] for event in reversed(prior_events)
                         if event.type == EventType.session_meta and "work_checkpoint" in event.payload), None)
        budget = getattr(self, "_context_history_budget", history_budget())
        if previous or sum(len(message.get("content") or "") for message in history) > budget:
            source_seq = max((event.seq for event in prior_events if event.type != EventType.session_meta), default=0)
            if previous and previous.get("source_seq", 0) > source_seq:
                previous = None
            checkpoint = build_work_checkpoint(prior_events, previous=previous)
            if checkpoint != previous:
                self.transcript.append(self.session_id, EventType.session_meta, {"work_checkpoint": checkpoint})
            memory.append(work_checkpoint_message(checkpoint))

        # M12：知识库检索注入（与记忆一样降权为 user 数据块；失败不影响主流程）
        if self.knowledge is not None:
            try:
                kb_hits = await self.knowledge.search(prompt, top_k=KB_TOP_K)
                memory = memory + knowledge_blocks(kb_hits, prompt)
            except Exception:
                pass

        # M29 回退记忆：被撤掉的那段经历必须注回来，否则模型会**原样重走**已经
        # 被证伪的路线（AgentRewind 的对照实验里那一档是最差的）。
        #
        # 刻意**不走记忆检索**（memory_blocks 是按 query 取 top-k）：回退记忆是
        # "刚发生的事"，必须无条件在场。走检索就会因为没命中关键词而被丢掉——
        # 那正好丢掉了最该看的那一条。
        try:
            note = RewindNotes(self.session_id).context_block()
        except Exception:
            note = ""
        if note:
            memory = memory + [data_message(note, label="回退记忆")]

        return history, memory

    def _loop_context(self) -> tuple[str, Any]:
        """The prompt and tool definitions shared by next-loop budgeting and usage."""
        from code_agent.codeintel.context import role_session_suffix

        member = self.agent_team.descriptor(self.session_id) if self.agent_team is not None else None
        role = member["role"] if member else ""
        # 代码模式：每轮都把"作业规程 + 地图 digest"按最新索引版本刷新一次
        code_suffix = self.refresh_code_context() if self.code_context_provider and self._code_question is None else ""
        code_suffix = role_session_suffix(code_suffix, role)
        parts = [DEFAULT_SYSTEM_PROMPT]
        if self._system_suffix:
            parts.append(self._system_suffix)
        if code_suffix:
            parts.append(code_suffix)
        # 联网（M19）+ 一键禁用（M21）：被摘掉的工具**根本不在 schema 里**——
        # 模型看不到就不会去调，也就不会浪费一轮拿一个"工具不存在"的错误。
        # 每轮都重读禁用清单（_build_loop 每次 run 都会调用），所以页面上点一下即生效。
        excluded: set[str] = set()
        if self.web_enabled:
            parts.append(WEB_SYSTEM_SUFFIX)
        else:
            excluded |= set(WEB_TOOL_NAMES)
        excluded |= set(disabled_tools())
        if member:
            parts.append(self.agent_team.role_context(self))
            if self.agent_team.planning_authority(self.session_id, self.user_id):
                from code_agent.permissions.hooks import READ_ONLY_TOOLS

                excluded |= set(self.registry.names()) - READ_ONLY_TOOLS
        else:
            excluded |= TEAM_TOOL_NAMES
        registry: Any = (
            FilteredToolRegistry(self.registry, exclude=tuple(sorted(excluded)))
            if excluded
            else self.registry
        )
        if self._route_obj is not None:
            # M29 任务类型路由：一句"这类任务通常只需要哪些工具"。**只提示不摘工具**——
            # 分类必然有错，而摘错工具的代价（模型去调 schema 里不存在的工具、
            # 更容易编造结果）远大于多一行提示字的代价。提示里只列**真实可用**的工具。
            parts.append(route_hint(self._route_obj, registry.names(), role=role))
        return "\n\n".join(parts), registry

    async def _build_loop_async(self, **overrides: Any) -> AgentLoop:
        """Prepare project maps off the event loop; cancellation stays responsive.

        A cancelled worker may finish its read-only context calculation, but it
        cannot start a provider request or emit a later execution phase.
        """
        self._set_phase("preparing_context")
        prepared_context = await asyncio.to_thread(self._loop_context)
        return self._build_loop(prepared_context=prepared_context, **overrides)

    def _build_loop(self, *, prepared_context: tuple[str, Any] | None = None, **overrides: Any) -> AgentLoop:
        """按会话配置构造 AgentLoop。"""
        system_prompt, registry = prepared_context if prepared_context is not None else self._loop_context()
        system_prompt = overrides.get("system_prompt", system_prompt)
        registry = overrides.get("registry", registry)
        provider = overrides.get("provider") or self.router.resolve_default()
        params: dict[str, Any] = dict(
            # 模型由当前生效配置（默认模型）决定——会话级指定已去掉
            provider=provider,
            registry=registry,
            permissions=self._make_permissions(),
            transcript=self.transcript,
            session_id=self.session_id,
            workspace=self.workspace,
            on_event=self.broadcast,
            # 段长在**调用时**取模块常量，而不是靠 AgentLoop 的默认参数（那是 import 期就固定的）：
            # 这样 `CODE_AGENT_MAX_TURNS` 之外还能被运行期覆盖（测试要把段缩短到 2~3 轮，
            # 否则每跑一条续跑用例都要真跑 60 轮，测试会慢到不可用）。
            max_turns=loop_module.DEFAULT_MAX_TURNS,
            system_prompt=system_prompt,
            on_tool_executed=self._audit_tool,
            history_max_chars=self._history_budget(provider, system_prompt=system_prompt, registry=registry),
            reuse_schema_budget=True,
            # M31 提问通道：工具通过它阻塞等待用户回答（CLI/自动化没有这个桥，
            # 工具会如实报错而不是假装问过）
            on_question=self._ask_user,
            # M41 文件观察：会话级记录，跨段共用（loop 每段重建，记录不能跟着重置）
            fs_observation=self.fs_observation,
            # M45 后台任务表 + 轮次边界的通知注入：繁忙的会话在"下一步"收到完成通知
            jobs=self.jobs,
            on_pending_notes=self._drain_job_notes,
            before_tool_dispatch=self.before_tool_dispatch,
            after_tool_dispatch=self.after_tool_dispatch,
            before_model_turn=self.before_model_turn,
            on_model_usage=self.on_model_usage,
            memory_root=self.memory_root,
            session_memory_root=self.session_memory_root,
            project_id=(self.session_repo.get(self.session_id) or {}).get("project_id") or "",
            # M47 重复调用提醒（与熔断器互补：它管"成功但没用的重复"）
            repeat_reminder=self.repeat_reminder,
            execution_progress=self.execution_progress,
            code_question=self._code_question,
            # M48 持久 shell：跨调用保留 cwd / 变量 / 函数（挂在会话上，不随段重建）
            shells=self.shells,
        )
        params.update(overrides)
        self._context_history_budget = params["history_max_chars"]
        loop = AgentLoop(**params)
        if self.agent_team is not None:
            original_extra = loop._tool_extra

            def guarded_extra() -> dict[str, Any]:
                extra = original_extra()

                def team_write_guard(name, arguments, ctx, tool):
                    # Resolve the live team on every dispatch. The caller cannot
                    # supply session ownership or a wider scope in tool args.
                    team = self.agent_team
                    if team is None:
                        from code_agent.tools.base import ToolError

                        raise ToolError("团队文件责任校验已失效，已拒绝执行")
                    return team.tool_write_guard(self.session_id, self.user_id, name, arguments, ctx, tool)

                def team_write_precheck(name, arguments, ctx, tool):
                    team = self.agent_team
                    if team is None:
                        from code_agent.tools.base import ToolError

                        raise ToolError("团队文件责任校验已失效，已拒绝执行")
                    return team.check_tool_scope(self.session_id, self.user_id, name, arguments, ctx, tool)

                extra["team_write_guard"] = team_write_guard
                extra["team_write_precheck"] = team_write_precheck
                return extra

            loop._tool_extra = guarded_extra
        return loop

    async def _run_execution(
        self,
        prompt: str,
        history: list[dict[str, Any]],
        memory: list[dict[str, Any]],
        attachments: list[dict[str, Any]] | None = None,
        context_blocks: list[str] | None = None,
        context_meta: dict[str, Any] | None = None,
        breaker: FailureBreaker | None = None,
    ) -> LoopResult:
        """Run model/tool steps to a natural stop, with external budget limits.

        Positive segment settings remain resumable. A completed model turn is
        final: edits and old checklists never manufacture another user request.
        """
        from code_agent.context.prompt import build_history

        def turn_limit(used: int) -> int:
            segment = max(0, int(loop_module.DEFAULT_MAX_TURNS))
            if MAX_TOTAL_TURNS > 0:
                remaining = max(0, MAX_TOTAL_TURNS - used)
                return min(segment, remaining) if segment else remaining
            return segment

        loop = await self._build_loop_async(breaker=breaker, max_turns=turn_limit(0))
        result = await loop.run(
            prompt,
            user_id=self.user_id,
            history=history,
            memory=memory,
            attachments=attachments,
            context_blocks=context_blocks,
            context_meta=context_meta,
        )

        continuations = 0
        total_turns = int(getattr(result, "turns", 0) or 0)
        #: 跨段累加的用量。**必须自己累加**：每段各自发一个带 usage 的事件
        #: （正常收口是 done、撞检查点是 code=segment_boundary 的 error），
        #: 而 `loop.run` 每次返回的都是**本段**用量——只抄最后一段会让
        #: usage 表（前端「Token 用量」页）少记前面所有段。
        total_usage = TokenUsage(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cached_tokens=result.usage.cached_tokens,
        )
        while True:
            if result.stop_reason == "max_turns" and result.segment_boundary:
                if MAX_TOTAL_TURNS and total_turns >= MAX_TOTAL_TURNS:
                    self.broadcast(
                        Event(
                            seq=0,
                            type=EventType.error,
                            payload={
                                "message": f"[loop] 已达硬上限 {MAX_TOTAL_TURNS} 轮（本轮共 {total_turns} 轮），"
                                           f"停止以免失控。可以再发一条消息继续。",
                                "code": "max_total_turns",
                            },
                        )
                    )
                    break
                if continuations >= MAX_CONTINUATIONS:
                    break
                continuations += 1
                note = (
                    f"[系统] 单段轮数已达检查点，自动继续（第 {continuations} 次，累计 {total_turns} 轮）。"
                    "请从中断处接着完成任务：**先复用你已经查到/改过的结论，不要重新调研或重复劳动**；"
                    "如果任务实际已经完成，直接给出结论即可。"
                )
            else:
                # Natural completion and abnormal provider stops are final.
                # No verification interview or implicit acceptance round.
                break

            # 重新取历史：上一段执行的工具调用/结果已写入轨迹。
            # build_history + assemble(在 loop 里)会做裁剪与预算压缩。
            prior_events = self.transcript.replay(self.session_id)
            # Provider-only protocol state must survive automatic segments.
            # Durable history intentionally omits private reasoning payloads.
            history = loop.continuation_history or build_history(prior_events)
            # 这条"重新锚定"提示**只发一次**：交给 loop.run 落成一条 user_message
            # （带 context_meta 标记），而不是自己再 broadcast 一条 seq=0。
            # 以前是两条（seq=0 的漂亮提示 + loop 落的原始 user 消息），
            # 结果是：实时看一遍、刷新后又以"用户气泡"的样子看一遍（还带 ** 号）。
            # 现在一条，落轨迹、刷新后仍在，界面按 quiet 渲染成一行淡字。
            self._record_checkpoint(
                kind="segment",
                label=f"检查点：第 {continuations} 次续跑前",
            )
            loop.max_turns = turn_limit(total_turns)
            result = await loop.run(
                note,
                user_id=self.user_id,
                history=history,
                memory=memory,
                context_meta={
                    "auto": True,
                    "quiet": True,
                    "continuation": continuations,
                    "total_turns": total_turns,
                    "source": {"kind": "runtime-continuation", "form": "notice", "summary": "继续执行未完成的任务"},
                },
            )
            total_turns += int(getattr(result, "turns", 0) or 0)
            total_usage.input_tokens += result.usage.input_tokens
            total_usage.output_tokens += result.usage.output_tokens
            total_usage.cached_tokens += result.usage.cached_tokens

        if continuations:
            # REST 返回值与 usage 表都取**跨段总和**；turns 保持"最后一段"口径
            # （test_continuity 钉的就是每段都跑满，改口径会破坏那条语义）。
            result = LoopResult(
                text=result.text,
                stop_reason=result.stop_reason,
                turns=result.turns,
                usage=total_usage,
                segment_boundary=result.segment_boundary,
            )
        return result

    def _emit_persisted(self, type: EventType, payload: dict[str, Any]) -> None:
        """落轨迹 + 实时扇出，供通知、审批与提问恢复使用。"""
        try:
            ev = self.transcript.append(self.session_id, type, payload)
        except Exception:
            ev = Event(seq=0, type=type, payload=payload)
        self.broadcast(ev)

    def _record_usage(self, result: LoopResult, prompt: str) -> None:
        """M5 s21 用量落库 + 工作区日志（失败不影响主流程）。"""
        from code_agent.memory.store import WorkspaceMemory

        if self.usage_repo is not None:
            try:
                self.usage_repo.record(
                    self.session_id, self.user_id, self.model_name,
                    result.usage.input_tokens, result.usage.output_tokens,
                    result.usage.cached_tokens,
                )
            except Exception:
                pass
        # 自动落工作区日志（便宜、不进上下文；MEMORY.md 只能显式写）
        try:
            WorkspaceMemory(self.memory_root).log_daily(
                f"prompt={prompt[:80]!r} -> {result.text[:120]!r}"
            )
        except Exception:
            pass

    def _audit_tool(self, tool_name: str, arguments_json: str, outcome: str) -> str:
        """M5 s22：工具执行审计（含 deny/error 全出口）。失败不影响主流程。"""
        if self.audit_repo is None:
            return
        try:
            self.audit_repo.append(self.session_id, self.user_id, tool_name, arguments_json, outcome)
        except Exception:
            pass

    def _history_budget(
        self, provider=None, *, system_prompt: str | None = None, registry=None,
    ) -> int:
        """动态历史字符预算：窗口的 50% 与预留输出后的空间取小值。

        与 context_usage 共用同一口径——弹窗展示的"实际发送量"必须等于
        真正发给模型的量，否则 UI 就是摆设。
        """
        from code_agent.context.prompt import dynamic_history_budget

        capacity = self.router.model_capacity(provider)
        if system_prompt is None or registry is None:
            system_prompt, registry = self._loop_context()
        output_cap = capacity["max_output_tokens"]
        if self._code_question is not None and not getattr(self._code_question, "detailed", False):
            output_cap = min(output_cap, 3072) if output_cap is not None else 3072
        return dynamic_history_budget(
            capacity["context_window"],
            system_chars=len(system_prompt.encode("utf-8")),
            tools_chars=(0 if self._code_question is not None else
                         len(json.dumps(registry.schemas(), ensure_ascii=False).encode("utf-8"))),
            max_output_tokens=output_cap,
        )

    # ------------------------------------------------------------------
    # 审批桥接（M1 CLI y/n 的服务端等价物）
    # ------------------------------------------------------------------
    def _current_permission_mode(self) -> str:
        project_mode = self._project_permission_provider() if self._project_permission_provider is not None else None
        if self.agent_team is not None:
            return self.agent_team.effective_permission(self.session_id, self.user_id)
        return project_mode if project_mode is not None else self.permission_mode

    def _make_permissions(self) -> PermissionManager:
        from code_agent.settings import contract_guard

        approver = self._strict_approver if self.strict else self._server_approver
        pm = PermissionManager(
            workspace_root=self.workspace,
            approver=approver,
            mode=self.permission_mode,
            read_only_approval=not self.strict and not (
                self.agent_team is not None and self.agent_team.planning_authority(self.session_id, self.user_id)
            ),
            mode_provider=self._current_permission_mode,
            # M32 契约闸门（R7）：契约文件对 AI 只读。每轮重读设置（默认开）——
            # 页面上关掉之后下一次模型响应就生效，不用重建会话。
            contract_guard=contract_guard(),
        )
        for tool_name, policy in self.extra_policies.items():
            pm.register_policy(tool_name, policy)
        return pm

    async def _strict_approver(self, req: ApprovalRequest) -> bool:
        """自动化（strict）模式：无人值守审批一律拒绝并留痕——fail closed。"""
        approval_id = self.approval_repo.create(
            session_id=self.session_id,
            user_id=self.user_id,
            tool_name=req.tool_name,
            command=req.command,
            reason=f"[automation strict] {req.reason}",
            purpose=req.purpose, effects=req.effects,
        )
        self.approval_repo.decide(approval_id, "denied", decided_by="automation")
        return False

    async def _server_approver(self, req: ApprovalRequest) -> bool:
        async with self._approval_lock:
            return await self._wait_for_approval(req)

    async def _wait_for_approval(self, req: ApprovalRequest) -> bool:
        approval_id = self.approval_repo.create(
            session_id=self.session_id,
            user_id=self.user_id,
            tool_name=req.tool_name,
            command=req.command,
            reason=req.reason,
            purpose=req.purpose, effects=req.effects,
        )
        # Register before publishing, so an immediate click can never miss its
        # waiter. The single-use approval never changes the session's mode.
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[approval_id] = fut
        approved = False
        status = "denied"
        self._set_phase("waiting_approval")
        self.broadcast(
            Event(
                seq=0,  # 占位：轨迹以 loop 事件为准，这里只做实时扇出
                type=EventType.approval_request,
                payload={
                    "approval_id": approval_id,
                    "tool": req.tool_name,
                    "arguments": req.command,
                    "reason": req.reason,
                    "purpose": req.purpose,
                    "effects": req.effects,
                    "purpose_source": "agent" if req.purpose else "missing",
                    "timeout": APPROVAL_TIMEOUT,
                    "scope": "once",
                },
            )
        )
        try:
            async with self._dispatch_lock:
                await self._suspend_approval_capacity_locked()
            approved = bool(await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT))
            status = "approved" if approved else "denied"
        except asyncio.TimeoutError:
            status = "timeout"
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            self._pending_approvals.pop(approval_id, None)
            self.approval_repo.decide(
                approval_id, "approved" if approved and status == "approved" else "denied",
                decided_by=f"{self.user_id} ({status})",
            )
            self.broadcast(Event(seq=0, type=EventType.approval_decided, payload={
                "approval_id": approval_id, "tool": req.tool_name,
                "approved": approved and status == "approved", "status": status,
            }))
            self._set_phase("tools" if self._tool_calls else "", busy=self.busy)
            async with self._dispatch_lock:
                try:
                    if status != "cancelled" and self._approval_capacity_suspended and self.agent_team is not None:
                        await self.agent_team.resume_after_approval(self, True)
                finally:
                    self._approval_capacity_suspended = False
        return approved

    def decide_approval(self, approval_id: str, approved: bool) -> bool:
        """REST 审批决策入口。返回是否命中挂起中的请求（future）。

        没命中也要把 DB 行关掉：future 可能已经消失（等待超时后 approver
        已 return、任务被取消、服务重启），否则这条 pending 会永远挂在
        表里，被 pending_approval() 当成"等待中"误弹给用户。
        """
        fut = self._pending_approvals.get(approval_id)
        if fut is not None:
            if fut.done():
                return False
            fut.set_result(approved)
            return True
        self.approval_repo.decide(
            approval_id, "denied", decided_by=f"{self.user_id} (expired)"
        )
        self._pending_approvals.pop(approval_id, None)
        return False

    # ------------------------------------------------------------------
    # 提问桥接（M31）：模型停下来问人，人答完答案作为普通 tool result 回去
    # ------------------------------------------------------------------
    def pending_questions(self) -> list[dict[str, Any]]:
        """Return only groups with a live answer waiter, never stale history."""
        pending = []
        for question_id, future in list(self._pending_questions.items()):
            if future.done():
                continue
            payload = self._pending_question_payloads.get(question_id)
            if payload is None:
                # A pre-upgrade in-process waiter may not have the payload map.
                # History alone is insufficient: require the live Future above.
                event = next((event for event in reversed(self.transcript.replay(self.session_id))
                              if event.type == EventType.question_request
                              and event.payload.get("question_id") == question_id), None)
                if event is None:
                    continue
                payload = {**event.payload, "ts": event.ts}
            pending.append(deepcopy({**payload, "id": question_id, "question_id": question_id,
                                     "session_id": self.session_id, "status": "pending"}))
        return pending

    async def _ask_user(self, questions: list[dict[str, Any]]) -> Any:
        """把一组问题交给用户并**等**回答。

        返回值契约（工具侧据此写回给模型）：
        - `list[dict]`：用户答完了（可能是部分跳过 → 那些题 selected 为空）
        - `None`：超时（没人回答）
        - `False`：用户放弃整组提问

        问与答都落轨迹（question_request / question_answered）供历史展示。
        刷新后的可回答状态以进程中仍存活的 Future 为准；历史记录不能
        在服务重启后恢复已经消失的等待者。
        """
        question_id = new_id("q_")
        now = datetime.now(timezone.utc)
        payload = {"question_id": question_id, "id": question_id, "session_id": self.session_id,
                   "questions": deepcopy(questions), "timeout": QUESTION_TIMEOUT, "status": "pending",
                   "ts": now.timestamp(), "expires_at": (now + timedelta(seconds=QUESTION_TIMEOUT)).isoformat()}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_questions[question_id] = fut
        self._pending_question_payloads[question_id] = payload
        timed_out = False
        try:
            # Register before broadcasting, so a quick answer cannot miss its waiter.
            self._emit_persisted(EventType.question_request, payload)
            self._set_phase("waiting_question")
            answers = await asyncio.wait_for(fut, timeout=QUESTION_TIMEOUT)
        except asyncio.TimeoutError:
            timed_out = True
            answers = None
        except asyncio.CancelledError:
            # 用户停止了这一轮：收尾并如实记录，然后让取消继续往上走
            self._emit_persisted(
                EventType.question_answered,
                {"question_id": question_id, "status": "cancelled", "answers": []},
            )
            raise
        finally:
            self._pending_questions.pop(question_id, None)
            self._pending_question_payloads.pop(question_id, None)
            task = asyncio.current_task()
            if task is None or not task.cancelling():
                phase = "waiting_question" if self.pending_questions() else (
                    "waiting_approval" if any(not future.done() for future in self._pending_approvals.values()) else "tools")
                self._set_phase(phase)

        if timed_out:
            self._emit_persisted(
                EventType.question_answered,
                {"question_id": question_id, "status": "timeout", "answers": []},
            )
            return None
        if answers is False:
            self._emit_persisted(
                EventType.question_answered,
                {"question_id": question_id, "status": "dismissed", "answers": []},
            )
            return False
        self._emit_persisted(
            EventType.question_answered,
            {"question_id": question_id, "status": "answered", "answers": answers or []},
        )
        return answers or []

    def answer_question(self, question_id: str, answers: Any) -> bool:
        """REST 回答入口。返回是否命中了正在等待的提问。

        没命中（服务重启 / 已经超时 / 用户停止了这一轮）时返回 False：调用方据此
        给前端一个明确的 409，而不是假装答案被收下了——那种"答案丢了但界面说成功"
        的体验最糟。
        """
        fut = self._pending_questions.get(question_id)
        if fut is None or fut.done():
            return False
        fut.set_result(answers)
        return True


class SessionManager:
    def __init__(
        self,
        transcript: TranscriptStore,
        session_repo: SessionRepo,
        approval_repo: ApprovalRepo,
        router: ModelRouter,
        registry: ToolRegistry,
        workspaces_root: Path | None = None,
        system_suffix: str = "",
        extra_policies: dict[str, Any] | None = None,
        usage_repo: Any | None = None,
        audit_repo: Any | None = None,
        knowledge: Any | None = None,   # KnowledgeBase，M12
        workspace_repo: Any | None = None,   # WorkspaceRepo，M14 工作区分组
        project_repo: Any | None = None,     # ProjectRepo，M18 代码模式
        code_intel: Any | None = None,       # CodeIntelService，M18 符号索引/地图/台账
    ):
        self.transcript = transcript
        self.session_repo = session_repo
        self.approval_repo = approval_repo
        self.router = router
        self.registry = registry
        self.system_suffix = system_suffix
        self.extra_policies: dict[str, Any] = dict(extra_policies or {})
        self.usage_repo = usage_repo
        self.audit_repo = audit_repo
        self.knowledge = knowledge
        self.workspace_repo = workspace_repo
        self.project_repo = project_repo
        self.code_intel = code_intel
        self.agent_team: Any | None = None
        self.workspaces_root = workspaces_root or (agent_home() / "workspaces")
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self._runtimes: dict[str, SessionRuntime] = {}
        self._conversation_lock = threading.RLock()

    def notify_team_changed(self, session_id: str) -> None:
        """Lightweight project notifications over existing session sockets.

        No per-token roster reads: this is called for phase and tool transitions
        only. Clients coalesce changes and use polling to recover missed events.
        """
        meta = self.session_repo.get(session_id)
        if not meta or not meta.get("project_id"):
            return
        row = self.agent_team._row(session_id) if self.agent_team is not None else None
        if not row or row["user_id"] != meta["user_id"] or row["project_id"] != meta["project_id"]:
            return
        lead_session_id = row["lead_session_id"]
        event = Event(seq=0, type=EventType.session_meta, payload={"team_changed": {
            "session_id": session_id, "project_id": meta["project_id"], "activation_id": row["activation_id"],
            "lead_session_id": lead_session_id,
        }})
        for runtime in list(self._runtimes.values()):
            bound = self.session_repo.get(runtime.session_id)
            member = self.agent_team._row(runtime.session_id)
            if (bound and bound["user_id"] == meta["user_id"] and bound.get("project_id") == meta["project_id"]
                    and member and member["lead_session_id"] == lead_session_id):
                runtime.broadcast(event)

    def create(
        self,
        user_id: str,
        folder: str = "",
        permission_mode: str | None = None,
    ) -> dict:
        """创建会话；permission_mode 缺省取全局默认（config.json）。"""
        from code_agent import settings as _settings

        mode = permission_mode or _settings.permission_mode()
        if mode not in PERMISSION_MODES:
            raise ValueError(f"未知权限档位：{mode}（可选：{', '.join(PERMISSION_MODES)}）")
        meta = self.session_repo.create(
            user_id=user_id,
            folder=(folder or "").strip()[:60],
            permission_mode=mode,
        )
        if meta.get("folder") and self.workspace_repo is not None:
            self.workspace_repo.create(user_id, meta["folder"])
        self.get_runtime(meta["id"], meta["user_id"])
        return meta

    def set_folder(self, session_id: str, user_id: str, folder: str) -> dict:
        """移动会话到工作区分组（侧栏按 folder 分组展示；空串 = 默认工作区）。"""
        self.get_meta(session_id, user_id)
        folder = (folder or "").strip()[:60]
        self.session_repo.set_folder(session_id, folder)
        if folder and self.workspace_repo is not None:
            # 移进新名字时自动登记工作区，否则分组要等下次创建才出现
            self.workspace_repo.create(user_id, folder)
        return self.get_meta(session_id, user_id)

    # ------------------------------------------------------------------
    # 工作区（侧栏分组，M14）
    # ------------------------------------------------------------------
    def list_workspaces(self, user_id: str) -> list[dict]:
        """工作区清单（含会话数）。

        会话里出现但注册表没有的名字会**自动补登**——兼容旧库、手工改过的
        folder 以及「先有会话后建分组」的历史数据，避免"有会话却看不到分组"。
        """
        if self.workspace_repo is None:
            return []
        names = {w["name"]: w for w in self.workspace_repo.list_for_user(user_id)}
        counts = self.workspace_repo.counts(user_id)
        for name in counts:
            if name not in names:
                names[name] = self.workspace_repo.create(user_id, name)
        out = [
            {
                "name": name,
                "count": counts.get(name, 0),
                "created_at": row.get("created_at") or "",
                "updated_at": row.get("updated_at") or "",
            }
            for name, row in names.items()
        ]
        out.sort(key=lambda w: w["updated_at"], reverse=True)
        return out

    def create_workspace(self, user_id: str, name: str) -> dict:
        """新建工作区（可以没有会话——空分组也要看得见）。"""
        if self.workspace_repo is None:
            raise ValueError("工作区功能未启用")
        clean = _clean_workspace_name(name)
        if self.workspace_repo.exists(user_id, clean):
            raise ValueError(f"工作区「{clean}」已存在")
        row = self.workspace_repo.create(user_id, clean)
        return {"name": clean, "count": 0, **(row or {})}

    def rename_workspace(self, user_id: str, old: str, new: str) -> dict:
        """重命名工作区：连同其下会话的 folder 一起改（两边必须一致）。"""
        if self.workspace_repo is None:
            raise ValueError("工作区功能未启用")
        old = _clean_workspace_name(old)
        clean = _clean_workspace_name(new)
        if not self.workspace_repo.exists(user_id, old):
            raise KeyError(f"工作区「{old}」不存在")
        if clean != old and self.workspace_repo.exists(user_id, clean):
            raise ValueError(f"工作区「{clean}」已存在")
        self.workspace_repo.rename(user_id, old, clean)
        return {"name": clean}

    def delete_workspace(self, user_id: str, name: str) -> None:
        """删除工作区：其下会话退回默认工作区，**不删会话**（删分组 ≠ 删数据）。

        会话退回由 WorkspaceRepo.delete 一并完成，这里只做存在性校验，
        避免两处各改一遍 folder 出现不一致。
        """
        if self.workspace_repo is None:
            raise ValueError("工作区功能未启用")
        clean = _clean_workspace_name(name)
        if not self.workspace_repo.exists(user_id, clean):
            raise KeyError(f"工作区「{clean}」不存在")
        self.workspace_repo.delete(user_id, clean)

    def set_permission_mode(self, session_id: str, user_id: str, mode: str) -> dict:
        """切换会话权限档位：落库 + 热推到已存在的运行时（下一次工具判定生效）。

        不重建运行时——重建会丢掉内存里的审批等待与取消状态。
        注意：只改这一个会话，**不动全局默认**（改默认走 settings 端点）。
        """
        mode = (mode or "").strip()
        if mode not in PERMISSION_MODES:
            raise ValueError(f"未知权限档位：{mode}（可选：{', '.join(PERMISSION_MODES)}）")
        self.get_meta(session_id, user_id)
        self.session_repo.set_permission_mode(session_id, mode)
        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            runtime.permission_mode = mode
        self.notify_team_changed(session_id)
        return self.get_meta(session_id, user_id)


    def set_web_enabled(self, session_id: str, user_id: str, enabled: bool) -> dict:
        """切换会话「联网」开关：落库 + 热推到运行时（下一条消息起生效）。

        与 permission_mode 同一套语义：只改这一个会话。
        联网工具是否出现在模型可见的 schema 里由它决定——关着的时候模型看不到
        `web_search` / `web_fetch`，也就不会浪费一轮去调它。
        """
        self.get_meta(session_id, user_id)
        self.session_repo.set_web_enabled(session_id, enabled)
        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            runtime.web_enabled = bool(enabled)
        return self.get_meta(session_id, user_id)

    def get_meta(self, session_id: str, user_id: str) -> dict:
        meta = self.session_repo.get(session_id)
        if meta is None or meta["user_id"] != user_id:
            raise KeyError("会话不存在或无权访问")
        return meta

    def workspace_for(self, meta: dict) -> Path:
        """解析会话的工作区。

        优先级：
        1. `workspace_override`（任务会话：工作树路径，落库以保证重启后仍然隔离）；
        2. 项目根目录（代码模式主会话）；
        3. `workspaces/<session_id>`（普通会话）。

        代码模式绝不自动创建项目/工作树目录——凭空 mkdir 出一个"看起来正常"的空目录，
        比直接报错危险得多。
        """
        override = (meta.get("workspace_override") or "").strip()
        if override:
            return Path(override)
        pid = (meta.get("project_id") or "").strip()
        if pid and self.project_repo is not None:
            row = self.project_repo.get(pid)
            if row is None:
                raise ValueError("会话所属项目已不存在，请在「代码」页重新打开项目")
            return Path(row["root_path"])
        return self.workspaces_root / meta["id"]

    def _project_permission_mode(self, session_id: str, user_id: str, *, workspace: Path | None = None,
                                 binding: tuple[str, ...] | None = None) -> str:
        """Validate a live project binding before every tool, without saving a clamp."""
        meta = self.get_meta(session_id, user_id)
        if meta.get("status") != "active":
            raise SessionClosed("会话已关闭或已归档")
        current_binding = tuple(meta.get(key) or "" for key in ("project_id", "tree_id", "task_id", "kind", "workspace_override"))
        if binding is not None and current_binding != binding:
            raise SessionClosed("会话项目或工作树已变化，请重新打开对话")
        project = self._conversation_project(meta.get("project_id") or "", user_id)
        resolved = self.workspace_for(meta)
        normalize = lambda path: os.path.normcase(os.path.abspath(str(path)))
        if not resolved.is_dir() or (workspace is not None and normalize(resolved) != normalize(workspace)):
            raise ValueError("会话工作区已变化或不可用，请重新打开对话")
        mode = meta.get("permission_mode") or "read-only"
        mode = mode if mode in PERMISSION_MODES else "read-only"
        if meta.get("kind") == "planner" or ((meta.get("tree_id") or "main") == "main" and not project.get("allow_direct_write")):
            return "read-only"
        return mode

    def get_runtime(self, session_id: str, user_id: str, *, strict: bool = False) -> SessionRuntime:
        """惰性重建：内存没有就从 DB 元数据重建运行时（s07）。strict 供自动化使用。"""
        meta = self.get_meta(session_id, user_id)
        if meta["status"] != "active":
            raise SessionClosed("会话已关闭或已归档")
        if meta.get("project_id"):
            self._project_permission_mode(session_id, user_id)
        if self.agent_team is not None and meta.get("project_id") and meta.get("kind") in ("", "main") and meta.get("tree_id") in ("", "main"):
            lead = self.agent_team.bind_lead(meta)
            if lead["session_id"] != session_id:
                raise SessionClosed("这是项目的历史主会话，请在代码页打开该项目的 Lead；原对话记录仍可查看")
        if self.agent_team is not None and meta.get("project_id") and meta.get("kind") == "chat":
            self.agent_team.bind_conversation(meta)
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            runtime = self._new_runtime(meta, strict=strict)
            self._runtimes[session_id] = runtime
            self._wire_code_context(runtime, meta)
        if self.agent_team is not None and self.agent_team.descriptor(session_id):
            runtime.agent_team = self.agent_team
        if runtime._project_permission_provider is not None:
            runtime._project_permission_provider()
        return runtime

    def _wire_code_context(self, runtime: SessionRuntime, meta: dict) -> None:
        """给代码模式会话挂上"作业规程 + 地图"与"结构坐标"两个 provider。

        用 provider 而不是直接把文本塞进运行时：地图会随代码变化，
        会话却可能活很久——每轮对话按索引版本决定是否重建，才能既新鲜又不浪费。
        """
        pid = (meta.get("project_id") or "").strip()
        if not pid or self.code_intel is None:
            return
        row = self.project_repo.get(pid) if self.project_repo is not None else None
        if row is None:
            return
        tree_id = (meta.get("tree_id") or "main").strip() or "main"
        has_workspace_override = bool((meta.get("workspace_override") or "").strip())

        def _context_project() -> dict:
            if runtime._project_permission_provider is not None:
                runtime._project_permission_provider()
            current = self.project_repo.get(pid)
            if current is None or current.get("user_id") != runtime.user_id:
                raise KeyError("会话所属项目不存在或无权访问")
            project = dict(current)
            # A task's index and source-coordinate reads belong to its isolated
            # workspace, even when the owning project's metadata changes.
            if has_workspace_override:
                project["root_path"] = str(runtime.workspace)
            return project

        def _session_context() -> tuple[int, str]:
            return self.code_intel.session_context(_context_project(), tree_id=tree_id)

        def _message_context(code_context: dict) -> str:
            return self.code_intel.message_context(_context_project(), code_context, tree_id=tree_id)

        runtime.code_context_provider = _session_context
        runtime._code_context_provider_fn = _message_context
        # M29 回退：成对检查点要能反向应用这个项目的 patch 台账，所以把项目句柄与
        # code_intel 一起挂在运行时上（普通会话两者为 None，回退时只退对话）。
        runtime.code_intel = self.code_intel
        runtime.code_project = _context_project()

    # ------------------------------------------------------------------
    # 代码模式（M18）：项目 ↔ 会话绑定
    # ------------------------------------------------------------------
    def _conversation_project(self, project_id: str, user_id: str) -> dict:
        if self.project_repo is None:
            raise ValueError("代码模式未启用")
        project = self.project_repo.get(project_id)
        if project is None or project.get("user_id") != user_id:
            raise KeyError("项目不存在或无权访问")
        if not Path(project["root_path"]).is_dir():
            raise ValueError("项目目录不可用，请重新打开项目")
        return project

    @staticmethod
    def _is_project_conversation(meta: dict | None, project: dict) -> bool:
        if not meta or meta.get("user_id") != project["user_id"] or meta.get("project_id") != project["id"] or meta.get("status") != "active" or meta.get("task_id"):
            return False
        if meta.get("kind") == "chat":
            return meta.get("tree_id") == "main"
        return meta["id"] == project.get("session_id") and meta.get("kind") in ("", "main") and meta.get("tree_id") in ("", "main")

    def _conversation_view(self, meta: dict) -> dict:
        runtime = self._runtimes.get(meta["id"])
        busy = bool(runtime and runtime.user_id == meta["user_id"] and runtime.busy)
        try:
            effective_permission = self._project_permission_mode(meta["id"], meta["user_id"])
        except (KeyError, ValueError, SessionClosed):
            # Metadata and soft deletion remain available if a project folder
            # was moved. A view must never fabricate an executable write grant.
            effective_permission = "read-only"
        return {**meta, "busy": busy, "phase": runtime.phase if busy else "",
                "effective_permission": effective_permission}

    def list_project_conversations(self, user_id: str, project_id: str) -> dict:
        """List only user-facing active conversations, without loading history or runtimes."""
        project = self._conversation_project(project_id, user_id)
        canonical = self.session_repo.get(project.get("session_id") or "")
        default_id = canonical["id"] if self._is_project_conversation(canonical, project) else ""
        rows = self.session_repo.list_project_conversations(user_id, project_id, default_id)
        return {"default_session_id": default_id, "conversations": [self._conversation_view(row) for row in rows]}

    def create_project_conversation(self, user_id: str, project_id: str, *, title: str = "", from_session_id: str = "") -> dict:
        """Create an independent project window; never replace or reconfigure its Lead."""
        for value, maximum in ((title, 100), (from_session_id, 256)):
            if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("对话标题或来源格式无效")
        with self._conversation_lock:
            project = self._conversation_project(project_id, user_id)
            source_id = from_session_id.strip() or project.get("session_id") or ""
            source = self.session_repo.get(source_id)
            if not from_session_id.strip() and (not source or source.get("status") != "active"):
                source = self.create_project_session(user_id, project_id, permission_mode="read-only")
                project = self._conversation_project(project_id, user_id)
            if not self._is_project_conversation(source, project):
                raise KeyError("来源对话不存在或无权访问")
            mode = source.get("permission_mode") or "read-only"
            mode = mode if mode in PERMISSION_MODES else "read-only"
            number = 1 + sum(row.get("kind") == "chat" and row.get("user_id") == user_id for row in self.session_repo.list_by_project(project_id))
            meta = self.session_repo.create(user_id=user_id, project_id=project_id, tree_id="main", task_id="", kind="chat",
                                            title=title.strip() or f"新对话 {number}", folder=source.get("folder") or project["name"][:60],
                                            permission_mode=mode, web_enabled=int(bool(source.get("web_enabled"))))
            self.get_runtime(meta["id"], user_id)
            return self._conversation_view(meta)

    def delete_project_conversation(self, user_id: str, project_id: str, session_id: str) -> dict:
        """Archive one conversation and its descendants, retaining history and project files."""
        with self._conversation_lock, self.agent_team._lock if self.agent_team is not None else contextlib.nullcontext():
            project = self.project_repo.get(project_id) if self.project_repo is not None else None
            if project is None or project.get("user_id") != user_id:
                raise KeyError("项目不存在或无权访问")
            meta = self.get_meta(session_id, user_id)
            is_main = meta.get("kind") in ("", "main") and meta.get("tree_id") in ("", "main")
            is_chat = meta.get("kind") == "chat" and meta.get("tree_id") == "main"
            deleting_default = session_id == project.get("session_id")
            if (meta.get("project_id") != project_id or meta.get("task_id") or not (is_main or is_chat)
                    or (is_main and not deleting_default and meta.get("status") != "archived")):
                raise KeyError("对话不存在或无权访问")

            ids = [session_id]
            if self.agent_team is not None:
                ids = self.agent_team.conversation_members(session_id, user_id)
            ids = list(dict.fromkeys(ids))
            # Validate the complete cancellation boundary before changing any session.
            for sid in ids:
                child = self.get_meta(sid, user_id)
                if child.get("project_id") != project_id or (sid != session_id and sid == project.get("session_id")):
                    raise ValueError("对话子任务归属不一致，无法删除")

            replacement = None
            if deleting_default:
                candidates = self.session_repo.list_project_conversations(user_id, project_id, "")
                replacement = next((item for item in candidates if item["id"] not in ids), None)
                if replacement is None:
                    mode = meta.get("permission_mode") or "read-only"
                    # Empty metadata only: no model, copied history, workspace
                    # creation, or dependency on an available project directory.
                    replacement = self.session_repo.create(
                        user_id=user_id, project_id=project_id, tree_id="main", task_id="", kind="main",
                        title="新对话", folder=meta.get("folder") or project["name"][:60],
                        permission_mode=mode if mode in PERMISSION_MODES else "read-only",
                        web_enabled=int(bool(meta.get("web_enabled"))),
                    )
            for sid in ids:
                runtime = self._runtimes.get(sid)
                if runtime is not None:
                    runtime._team_wake_blocked = True
                    runtime._provider_wake_blocked = True
            # Retiring the entire old root before moving the pointer prevents
            # main-session repair from reparenting its historical descendants.
            if self.agent_team is not None:
                self.agent_team._cancel_members(ids, retired=True)
            if replacement is not None:
                self.project_repo.set_session(project_id, replacement["id"])
                project = self.project_repo.get(project_id)
                if self.agent_team is not None:
                    self.agent_team.bind_conversation(replacement)
            for sid in ids:
                runtime = self._runtimes.get(sid)
                if runtime is not None:
                    # Block settle callbacks before stopping background work: deleting
                    # a window must not schedule a completion wake for that window.
                    runtime._team_wake_blocked = True
                    runtime._provider_wake_blocked = True
                    for job in runtime.jobs.list(sid):
                        if job.session_id == sid and not job.settled:
                            runtime.jobs.kill(job.id, "对话已删除")
                self.archive(sid, user_id)
                # A process restart can leave persisted pending approvals without
                # their in-memory Future. Preserve those rows as denied audit data.
                for approval in self.approval_repo.pending_for_session(sid):
                    self.approval_repo.decide(approval["id"], "denied", decided_by=f"{user_id} (conversation-delete)")

            canonical = self.session_repo.get(project.get("session_id") or "")
            default_id = canonical["id"] if self._is_project_conversation(canonical, project) else ""
            fallback = default_id
            if not fallback:
                remaining = self.session_repo.list_project_conversations(user_id, project_id, "")
                fallback = remaining[0]["id"] if remaining else ""
            return {"deleted": True, "session_id": session_id, "fallback_session_id": fallback,
                    "default_session_id": default_id,
                    "default_conversation": self._conversation_view(canonical) if default_id else None}

    def create_project_session(
        self,
        user_id: str,
        project_id: str,
        *,
        permission_mode: str = "read-only",
        tree_id: str = "main",
        task_id: str = "",
        kind: str = "main",
        reuse: bool = True,
    ) -> dict:
        """取项目的常驻会话（不存在则建）。

        默认 `read-only`：主会话是"看地图 + 问答 + 提需求"的窗口，写代码属于
        任务会话（P4 隔离工作树）。这样"AI 把主仓库改坏"在权限层就被消掉，
        而不是靠模型自觉。
        """
        if self.project_repo is None:
            raise ValueError("代码模式未启用")
        row = self.project_repo.get(project_id)
        if row is None or row["user_id"] != user_id:
            raise KeyError("项目不存在或无权访问")
        if not Path(row["root_path"]).is_dir():
            raise ValueError(f"项目目录不可用：{row['root_path']}")

        if (reuse or self.agent_team is not None) and kind == "main":
            sid = (row.get("session_id") or "").strip()
            if sid:
                meta = self.session_repo.get(sid)
                if self._is_project_conversation(meta, row):
                    self.project_repo.touch(project_id)
                    if self.agent_team is not None:
                        lead = self.agent_team.bind_lead(meta)
                        meta = self.session_repo.get(lead["session_id"])
                    return meta

        mode = permission_mode if permission_mode in PERMISSION_MODES else "read-only"
        meta = self.session_repo.create(
            user_id=user_id,
            title=f"[代码] {row['name']}",
            folder=row["name"][:60],
            permission_mode=mode,
            project_id=project_id,
            tree_id=tree_id or "main",
            task_id=task_id,
            kind=kind or "main",
        )
        if self.workspace_repo is not None:
            # 代码会话也进侧栏工作区分组（分组名 = 项目名），便于从「对话」页找回
            self.workspace_repo.create(user_id, meta["folder"])
        if kind == "main":
            self.project_repo.set_session(project_id, meta["id"])
            if self.agent_team is not None:
                self.agent_team.bind_lead(meta)
        self.get_runtime(meta["id"], meta["user_id"])
        return meta

    def create_task_session(
        self,
        user_id: str,
        project_id: str,
        *,
        task_id: str,
        title: str = "",
        workspace: Path | None = None,
        permission_mode: str = "workspace-write",
    ) -> dict:
        """为隔离任务创建会话：**工作区 = 任务工作树**（不是主干）。

        与主会话的关键差别：
        - 工作区由调用方给定（工作树路径），AI 的读写天然被限制在隔离树里；
        - 权限默认 workspace-write（主会话是只读）；
        - 绑定 task_id 与 tree_id=task:<id>，地图可以按树叠加差异。
        """
        if self.project_repo is None:
            raise ValueError("代码模式未启用")
        row = self.project_repo.get(project_id)
        if row is None or row["user_id"] != user_id:
            raise KeyError("项目不存在或无权访问")
        mode = permission_mode if permission_mode in PERMISSION_MODES else "workspace-write"
        meta = self.session_repo.create(
            user_id=user_id,
            title=title[:100] or f"[任务] {row['name']}",
            folder=row["name"][:60],
            permission_mode=mode,
            project_id=project_id,
            tree_id=f"task:{task_id}",
            task_id=task_id,
            kind="task",
            # 落库：重启后重建运行时仍必须落在工作树里，否则会退回主干（隔离失效）
            workspace_override=str(workspace or ""),
        )
        if self.workspace_repo is not None:
            self.workspace_repo.create(user_id, meta["folder"])
        runtime = self._new_runtime(meta, workspace=workspace)
        self._runtimes[meta["id"]] = runtime
        self._wire_code_context(runtime, meta)
        return meta

    def _new_runtime(
        self, meta: dict, *, workspace: Path | None = None, strict: bool = False
    ) -> SessionRuntime:
        """构造运行时；workspace 显式给出时优先（任务工作树）。

        代码模式（项目/任务会话）的记忆根放在 `~/.code-agent/code/<pid>/memory`：
        绝不把平台的 MEMORY.md 与 daily log 写进用户的仓库——那会污染 git 状态，
        也会在任务审查里冒出"计划外改动"。
        """
        resolved = workspace or self.workspace_for(meta)
        project_id = (meta.get("project_id") or "").strip()
        if project_id:
            if not resolved.is_dir():
                raise ValueError(f"项目目录不可用：{resolved}")
            memory_root = code_dir() / project_id
        else:
            resolved.mkdir(parents=True, exist_ok=True)
            memory_root = None
        runtime = SessionRuntime(
            session_id=meta["id"],
            user_id=meta["user_id"],
            transcript=self.transcript,            session_repo=self.session_repo,
            approval_repo=self.approval_repo,
            router=self.router,
            registry=self.registry,
            workspace=resolved,
            extra_policies=self.extra_policies,
            usage_repo=self.usage_repo,
            audit_repo=self.audit_repo,
            strict=strict,
            system_suffix=self.system_suffix,
            knowledge=self.knowledge,
            permission_mode=meta.get("permission_mode") or "workspace-write",
            memory_root=memory_root,
            session_memory_root=self.workspaces_root / meta["id"],
            web_enabled=bool(meta.get("web_enabled") or 0),
        )
        if project_id:
            binding = tuple(meta.get(key) or "" for key in ("project_id", "tree_id", "task_id", "kind", "workspace_override"))
            runtime._project_permission_provider = lambda: self._project_permission_mode(runtime.session_id, runtime.user_id, workspace=runtime.workspace, binding=binding)
        if self.agent_team is not None and self.agent_team.descriptor(meta["id"]):
            runtime.agent_team = self.agent_team
        runtime._team_changed_callback = self.notify_team_changed
        return runtime

    def bind_project(
        self,
        session_id: str,
        user_id: str,
        project_id: str,
        *,
        tree_id: str = "main",
        task_id: str = "",
        kind: str = "main",
    ) -> dict:
        """把已存在的会话绑到项目（含运行时热替换：下一条消息即在新工作区执行）。"""
        meta = self.get_meta(session_id, user_id)
        if self.agent_team is not None and self.agent_team.descriptor(session_id):
            raise ValueError("项目智能体的归属固定，请打开目标项目的 Lead 会话")
        if self.project_repo is not None:
            row = self.project_repo.get(project_id)
            if row is None or row["user_id"] != user_id:
                raise KeyError("项目不存在或无权访问")
        self.session_repo.set_project_binding(session_id, project_id, tree_id, task_id, kind)
        # 运行时里缓存着旧 workspace：不重建就会继续在旧目录里读写（静默错区）
        runtime = self._runtimes.pop(session_id, None)
        if runtime is not None:
            runtime.cancel()
            runtime.shutdown()   # M48：工作区变了，旧 shell 的 cwd 已经不对了
        new_meta = self.get_meta(session_id, user_id)
        self.get_runtime(session_id, user_id)
        return new_meta

    def apply_extensions(
        self,
        *,
        system_suffix: str = "",
        extra_policies: dict[str, Any] | None = None,
    ) -> int:
        """热更新扩展状态（M10：插件启停后调用）。

        不仅改 SessionManager 自己的字段，还要推到**已存在**的会话运行时上——
        否则用户正在用的会话要等重建才生效，表现为「装了插件却没反应」。

        返回受影响的活跃会话数。

        注意：MCP 服务器的新增/移除不在热更新范围内（要起/停子进程），
        需重启服务；此处仅覆盖技能目录、prompt 片段与权限放行名单。
        """
        self.system_suffix = system_suffix
        self.extra_policies = dict(extra_policies or {})
        for runtime in self._runtimes.values():
            runtime.system_suffix = self.system_suffix
            runtime.extra_policies = dict(self.extra_policies)
        return len(self._runtimes)

    async def run_workflow(
        self, user_id: str, name: str, steps: list["WorkflowStep"]
    ) -> list[dict[str, Any]]:
        """确定性工作流执行（M14）：每个步骤一个严格会话，顺序执行、上下文串联。

        与自由循环互补：步骤固定、要精确控制路径的自动化任务用工作流，
        每步有独立会话/轨迹/审计，结果显式传给下一步。
        """
        from code_agent.agent.workflow import run_workflow as _run_workflow

        async def run_step(prompt: str, _context: str) -> dict[str, Any]:
            meta = self.session_repo.create(user_id=user_id, title=f"[auto] {name}")
            runtime = self.get_runtime(meta["id"], user_id, strict=True)
            try:
                result = await runtime.submit(prompt)
                return {
                    "text": result.text,
                    "stop_reason": result.stop_reason,
                    "turns": result.turns,
                    "usage": result.usage.model_dump(),
                }
            finally:
                self.session_repo.close(meta["id"])

        return await _run_workflow(steps, run_step)

    async def run_automation(self, user_id: str, name: str, prompt: str) -> Any:
        """自动化执行入口（s23）：专用会话 + strict 运行时（审批 fail closed）。

        M14：prompt 含 `---step---` 分隔符时按「确定性工作流」逐步执行
        （每步一个严格会话、上下文串联），否则走单 prompt 自由循环。
        """
        from code_agent.agent.workflow import parse_steps

        steps = parse_steps(prompt)
        if len(steps) > 1:
            return await self.run_workflow(user_id, name, steps)

        meta = self.session_repo.create(user_id=user_id, title=f"[auto] {name}")
        runtime = self.get_runtime(meta["id"], user_id, strict=True)
        try:
            return await runtime.submit(prompt)
        finally:
            self.session_repo.close(meta["id"])

    def close(self, session_id: str, user_id: str) -> None:
        self.get_meta(session_id, user_id)
        if self.agent_team is not None:
            self.agent_team.cancel_for(session_id)
        runtime = self._runtimes.pop(session_id, None)
        if runtime:
            runtime.cancel()
            runtime.shutdown()   # M48：会话不再被用 → 连持久 shell 一起释放
        self.session_repo.close(session_id)

    def rename(self, session_id: str, user_id: str, title: str) -> dict:
        """重命名会话（只改标题，不动轨迹与状态）。"""
        title = (title or "").strip()
        if not title:
            raise ValueError("标题不能为空")
        self.get_meta(session_id, user_id)
        self.session_repo.rename(session_id, title[:100])
        return self.get_meta(session_id, user_id)

    def archive(self, session_id: str, user_id: str) -> None:
        """归档：退出默认列表、不可再发消息；轨迹与用量保留可查，与 close（终止）不同。"""
        self.get_meta(session_id, user_id)
        if self.agent_team is not None:
            self.agent_team.cancel_for(session_id)
        runtime = self._runtimes.pop(session_id, None)
        if runtime:
            runtime.cancel()
            runtime.shutdown()   # M48：会话不再被用 → 连持久 shell 一起释放
        self.session_repo.set_status(session_id, "archived")

    def delete(self, session_id: str, user_id: str) -> None:
        """删除会话：停运行时、清 DB 行（含用量/审批），并清理磁盘残留。

        磁盘残留一律移入各自 `.trash/` 回收目录（可人工找回），包括：
        会话轨迹、**交付物**（artifacts）与**会话工作区**（含 memory）。
        此前只清了轨迹，交付物会变成"孤儿文件"一直留在 artifacts/ 下。
        """
        self.get_meta(session_id, user_id)
        if self.agent_team is not None:
            self.agent_team.cancel_for(session_id)
        runtime = self._runtimes.pop(session_id, None)
        if runtime:
            runtime.cancel()
            runtime.shutdown()   # M48：会话不再被用 → 连持久 shell 一起释放
        self.session_repo.delete(session_id)
        self.transcript.discard(session_id)                    # 轨迹
        _trash_dir(artifacts_dir() / session_id)               # 交付物
        _trash_dir(self.workspaces_root / session_id)          # 工作区（含 memory）

    def context_usage(self, session_id: str, user_id: str) -> dict:
        """估算当前会话下一次请求的上下文占用（字符级粗估，UI 以 ~ 展示）。

        分三段：系统提示词 / 工具定义 / 对话消息，与主流 agent 产品一致。
        记忆/知识库注入块随 prompt 变化，量级小且不可提前确定，不计入。
        """
        meta = self.get_meta(session_id, user_id)
        from code_agent.context.prompt import build_history

        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            system, registry = runtime._loop_context()
        else:
            system, registry = DEFAULT_SYSTEM_PROMPT, self.registry
        tools_json = json.dumps(registry.schemas(), ensure_ascii=False)
        history = build_history(self.transcript.replay(session_id))
        msgs_json = "\n".join(json.dumps(m, ensure_ascii=False) for m in history)

        system_t, tools_t, msgs_t = (
            _est_tokens(system),
            _est_tokens(tools_json),
            _est_tokens(msgs_json),
        )
        total = system_t + tools_t + msgs_t
        model = self.router.model_name()
        provider = self.router.resolve_default()
        capacity = self.router.model_capacity(provider)
        window = capacity["context_window"]
        # 实际发送量：历史超过动态预算时会被 compact_messages 截到预算内
        from code_agent.context.prompt import dynamic_history_budget

        if runtime is not None:
            budget = runtime._history_budget(provider, system_prompt=system, registry=registry)
        else:
            budget = dynamic_history_budget(
                window, system_chars=len(system.encode("utf-8")),
                tools_chars=len(tools_json.encode("utf-8")),
                max_output_tokens=capacity["max_output_tokens"],
            )
        budget_tokens = int(budget / 2.2)
        effective_msgs = min(msgs_t, budget_tokens)
        effective = system_t + tools_t + effective_msgs
        return {
            "model": model,
            "window": window,
            "max_output_tokens": capacity["max_output_tokens"],
            "input_limit": window - (capacity["max_output_tokens"] or 0),
            "system": system_t,
            "tools": tools_t,
            "messages": effective_msgs,
            "messages_raw": msgs_t,
            "compressed": msgs_t > budget_tokens,
            "budget_tokens": budget_tokens,
            "total": total,
            "effective": effective,
            "used_pct": round(effective / window * 100, 1) if window else 0.0,
        }

    def fork(self, session_id: str, user_id: str) -> dict:
        """分叉：复制原会话完整轨迹到一个新会话，从原对话继续聊。

        跳过 approval_request/approval_decided——旧会话的审批流不应在新会话里
        复活（审批状态以原会话 DB 为准，复制事件只会在前端弹出过期审批卡）。
        """
        meta = self.get_meta(session_id, user_id)
        skip = {EventType.session_meta, EventType.approval_request, EventType.approval_decided}
        src = [e for e in self.transcript.replay(session_id) if e.type not in skip]
        base_title = (meta["title"] or "").strip()
        new_meta = self.session_repo.create(
            user_id=user_id,
            title=(f"[分叉] {base_title}" if base_title else "[分叉] 未命名会话")[:100],
        )
        for ev in src:
            self.transcript.append(new_meta["id"], ev.type, ev.payload)
        self.get_runtime(new_meta["id"], user_id)
        return new_meta

    def decide_approval(self, user_id: str, approval_id: str, approved: bool) -> bool:
        row = self.approval_repo.get(approval_id)
        if row is None or row["user_id"] != user_id:
            raise KeyError("审批不存在或无权访问")
        runtime = self._runtimes.get(row["session_id"])
        if runtime is None:
            # 运行时不在了，直接把 DB 行关掉，避免悬挂 pending
            self.approval_repo.decide(approval_id, "denied", f"{user_id} (expired)")
            return False
        return runtime.decide_approval(approval_id, approved)
