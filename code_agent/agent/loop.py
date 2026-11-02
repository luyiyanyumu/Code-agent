"""Code-agent adapters around the official DeepSeek Harness driver.

The pinned dsh-agent-loop owns turn/step and tool scheduling. This module adapts
model requests, permission-checked Python tools and product events. There is no
alternate Python agent loop or automatic forced-answer/retry policy.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from code_agent.agent.text_tool_output import is_text_tool_call
from code_agent.agent.source_context import SourceContext
from code_agent.agent.tool_runtime import ToolRuntimeMixin
from code_agent.context.images import ImageInbox
from code_agent.context.prompt import assemble, compose_prompt_with_attachments, history_budget, provider_messages
from code_agent.context.safety import ANTI_INJECTION_RULE
from code_agent.harness import run_harness
from code_agent.models.approval import APPROVAL_ARGUMENT, APPROVAL_INSTRUCTION, executable_arguments, tool_explanation_details
from code_agent.models.events import Event, EventType, ModelTurn, ProviderPayload, TokenUsage, ToolCall, assistant_message, new_id
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.base import Provider, GenerationOptions, current_generation_options, generation_options
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.tools.registry import ToolRegistry

DEFAULT_MAX_TURNS = max(0, int(os.environ.get("CODE_AGENT_MAX_TURNS", "0")))
TOOL_PARALLEL = os.environ.get("CODE_AGENT_TOOL_PARALLEL", "1") != "0"
TOOL_TIMEOUT_DEFAULT = float(os.environ.get("CODE_AGENT_TOOL_TIMEOUT_DEFAULT", "0"))
REMEMBER_FAILURES = os.environ.get("CODE_AGENT_REMEMBER_FAILURES", "1") != "0"
DEFAULT_SYSTEM_PROMPT = (
    "你是 Code-agent，一个运行在用户本地/服务器上的智能体助手。"
    "用户要求做事时，在授权范围内直接使用工具推进到可交付结果。"
    "按需读取、精确编辑和验证，复用已有结果；独立读取可以并行。"
    "根据真实工具输出处理失败，不绕过权限和审批，不虚构成功。"
    "执行修改后运行相关验证，根据结果交付；无法继续时说明具体阻碍与未完成项。"
    "用简体中文简明报告实际进展和结果。" + APPROVAL_INSTRUCTION + ANTI_INJECTION_RULE
)
_PARALLEL_READS = frozenset({
    "read_file", "glob", "grep", "read_image", "code_map", "code_search", "code_graph",
    "code_features", "code_symbols", "kb_search", "web_search", "web_fetch",
    "git_status", "git_diff", "git_log", "read_agent_execution", "team_list", "job_status", "job_output",
})


@dataclass
class LoopResult:
    text: str
    stop_reason: str
    turns: int
    usage: TokenUsage
    events: list[Event] = field(default_factory=list)
    segment_boundary: bool = False


class AgentLoop(ToolRuntimeMixin):
    """Keep the product API while the official JS engine owns execution."""

    def __init__(
        self, provider: Provider, registry: ToolRegistry, permissions: PermissionManager,
        transcript: TranscriptStore, session_id: str | None = None, workspace: Path | None = None,
        max_turns: int = DEFAULT_MAX_TURNS, system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        on_event=None, on_tool_executed=None, history_max_chars: int | None = None,
        reuse_schema_budget: bool = False, breaker=None, on_question=None, fs_observation=None,
        jobs=None, on_pending_notes=None, repeat_reminder=None, shells=None,
        before_tool_dispatch=None, after_tool_dispatch=None, before_model_turn=None,
        on_model_usage=None, memory_root: Path | None = None, session_memory_root: Path | None = None,
        project_id: str = "", execution_progress=None, code_question=None,
        history_ceiling_chars: int | None = None, on_history_budget=None,
    ):
        self.provider, self.registry, self.permissions = provider, registry, permissions
        self.transcript, self.session_id = transcript, session_id or new_id("s_")
        self.workspace = Path(workspace) if workspace is not None else Path.cwd()
        self.max_turns, self.system_prompt = max_turns, system_prompt
        self.on_event, self.on_tool_executed = on_event, on_tool_executed
        self.history_max_chars = history_max_chars
        self.history_ceiling_chars, self.on_history_budget = history_ceiling_chars, on_history_budget
        self.selected_history_chars = history_max_chars if history_max_chars is not None else history_budget()
        self.reuse_schema_budget = reuse_schema_budget
        self.on_question, self.fs_observation = on_question, fs_observation
        self.jobs, self.on_pending_notes, self.shells = jobs, on_pending_notes, shells
        self.before_tool_dispatch, self.after_tool_dispatch = before_tool_dispatch, after_tool_dispatch
        self.before_model_turn, self.on_model_usage = before_model_turn, on_model_usage
        self.memory_root = Path(memory_root) if memory_root is not None else self.workspace
        self.session_memory_root = Path(session_memory_root) if session_memory_root is not None else self.memory_root
        self.project_id = project_id
        self.tool_timeout_default, self.remember_failures = TOOL_TIMEOUT_DEFAULT, REMEMBER_FAILURES
        # Old task controllers remain inert; source evidence only supplies
        # bounded context and reminders, never a second execution loop.
        self._source_context = SourceContext(self.workspace)
        self.continuation_history: list[dict[str, Any]] = []
        self._protocol_state: dict[tuple[str, ...], ProviderPayload] = {}

    def _working_history_budget(self) -> int:
        return self.history_max_chars if self.history_max_chars is not None else history_budget()

    def _fit_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ceiling = self.history_ceiling_chars if "CODE_AGENT_HISTORY_BUDGET" not in os.environ else None
        fitted = self._source_context.fit(messages, max_chars=self._working_history_budget(), ceiling_chars=ceiling)
        self.selected_history_chars = self._source_context.selected_budget
        if self.on_history_budget is not None:
            self.on_history_budget(self.selected_history_chars)
        return fitted

    async def _dispatch_tool_leased(self, tc: ToolCall, ctx: ToolContext, **kwargs) -> str:
        before = self._source_context.begin(tc)
        result = await super()._dispatch_tool_leased(tc, ctx, **kwargs)
        # Native metadata is available before the common adapter converts it to
        # a public string and possibly externalizes the result.
        self._source_context.capture(tc, result, before)
        return result

    def _restore_protocol(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        restored = []
        for original in messages:
            message = dict(original)
            ids = tuple(call.get("id", "") for call in message.get("tool_calls", []))
            payload = message.get("_provider_payload")
            if ids and isinstance(payload, ProviderPayload):
                self._protocol_state[ids] = payload
            elif ids and ids in self._protocol_state:
                message["_provider_payload"] = self._protocol_state[ids]
            restored.append(message)
        return restored

    @staticmethod
    def _wire_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Private reasoning stays in Python memory, never in RPC/session logs.
        return [{key: value for key, value in message.items()
                 if not key.startswith("_") or key == "_context_source"}
                for message in messages]

    def _execution_modes(self) -> dict[str, str]:
        modes = {}
        for name in self.registry.names():
            tool = self.registry.get(name)
            safe = getattr(tool, "execution_mode", None) == "parallel" or name in _PARALLEL_READS
            modes[name] = "parallel" if TOOL_PARALLEL and safe and not getattr(tool, "writes_path", False) else "exclusive"
        return modes

    async def run(
        self, prompt: str, user_id: str = "anonymous", history: list[dict[str, Any]] | None = None,
        memory: list[dict[str, Any]] | None = None, attachments: list[dict[str, Any]] | None = None,
        context_blocks: list[str] | None = None, context_meta: dict[str, Any] | None = None,
    ) -> LoopResult:
        if not (context_meta or {}).get("continuation"):
            self._protocol_state.clear()
            self._source_context = SourceContext(self.workspace)
        self._images = ImageInbox()
        self._emitted_results: set[str] = set()
        self._calls: dict[str, ToolCall] = {}
        self._usage, self._turns = TokenUsage(), 0
        ctx = ToolContext(session_id=self.session_id, workspace=self.workspace, user_id=user_id,
                          extra=self._tool_extra(), fs_observation=self.fs_observation)
        self._emit(ctx, EventType.user_message, {"text": prompt, "attachments": attachments or [], **(context_meta or {})})
        model_prompt = compose_prompt_with_attachments(prompt, attachments)
        blocks = [block for block in (context_blocks or []) if block]
        if blocks:
            model_prompt += "\n\n" + "\n\n".join(blocks)
        base = assemble(self.system_prompt, memory or [], [], model_prompt,
                        self._working_history_budget(), automatic=bool((context_meta or {}).get("auto")))
        messages = self._fit_messages(base[:-1] + self._restore_protocol(history or []) + base[-1:])
        self.continuation_history = []

        async def request(method: str, frame: dict[str, Any]):
            if method == "model":
                raw = self._restore_protocol(frame.get("messages", []))
                self._reconcile_results(raw, ctx)
                extra: list[dict[str, Any]] = []
                self._inject_pending_notes(extra, ctx)
                self._attach_pending_images(extra, ctx)
                current = self._fit_messages(raw + extra)
                reminder = self._source_context.reminder(current, self._calls)
                if reminder:
                    extra.append({"role": "user", "content": reminder, "_context_source": "auto"})
                    current = self._fit_messages(raw + extra)
                    self._emit(ctx, EventType.session_meta, {"kind": "source_read_reminder", "text": reminder})
                self.continuation_history = current
                if self.before_model_turn is not None:
                    await self.before_model_turn()
                turn = await self._next_turn(current, ctx, frame.get("tools", self.registry.schemas()))
                self._turns += 1
                if self.on_model_usage is not None:
                    self.on_model_usage(turn.usage)
                self._usage.input_tokens += turn.usage.input_tokens
                self._usage.output_tokens += turn.usage.output_tokens
                self._usage.cached_tokens += turn.usage.cached_tokens
                if is_text_tool_call(turn.text):
                    turn = turn.model_copy(update={"text": "模型返回了无效的文本工具调用，任务未完成。", "stop_reason": "error", "tool_calls": []})
                stored = assistant_message(turn)
                ids = tuple(call.id for call in turn.tool_calls)
                if ids and isinstance(stored.get("_provider_payload"), ProviderPayload):
                    self._protocol_state[ids] = stored["_provider_payload"]
                normal = turn.stop_reason in ("stop", "end_turn", "tool_use", "tool_calls", "completed")
                if normal and turn.tool_calls:
                    self._emit(ctx, EventType.assistant_message, {
                        "text": turn.text or "", "tool_calls": [call.model_copy(update={"arguments": executable_arguments(call.arguments)}).model_dump() for call in turn.tool_calls],
                    })
                    for call in turn.tool_calls:
                        self._calls[call.id] = call
                        self._emit(ctx, EventType.tool_use, {
                            "call": call.model_copy(update={"arguments": executable_arguments(call.arguments)}).model_dump(),
                            "explanation": tool_explanation_details(call.arguments.get(APPROVAL_ARGUMENT)),
                        })
                self.continuation_history = current + [stored]
                return {**turn.model_dump(), "contextMessages": self._wire_messages(extra)}
            call = ToolCall.model_validate(frame["call"])
            result = await self._execute_tool(call, ctx)
            self._record_result(call.id, call.name, result, ctx)
            return result

        try:
            terminal = await run_harness({
                "sessionId": self.session_id, "cwd": str(self.workspace), "systemPrompt": self.system_prompt,
                "messages": self._wire_messages(messages), "tools": self.registry.schemas(),
                "maxTurns": self.max_turns, "maxParallelToolCalls": 10 if TOOL_PARALLEL else 1,
                "executionModes": self._execution_modes(),
            }, request)
        except Exception as exc:
            self._emit(ctx, EventType.error, {"message": str(exc), "code": "harness_error"})
            raise
        terminal_messages = self._restore_protocol(terminal.get("messages") or self.continuation_history)
        self._reconcile_results(terminal_messages, ctx)
        self.continuation_history = [message for message in terminal_messages if message.get("role") != "system"]
        reason = terminal.get("stop_reason", "end_turn")
        if reason in ("stop", "completed", "tool_use", "tool_calls"):
            reason = "end_turn"
        if reason in ("length", "max-tokens"):
            reason = "max_tokens"
        text = str(terminal.get("text") or "")
        boundary = reason == "max_turns"
        if boundary:
            text = f"[loop] 本轮已达检查点（{self.max_turns} 轮），交回调度"
            self._emit(ctx, EventType.error, {"message": text, "quiet": True, "code": "segment_boundary", "usage": self._usage.model_dump()})
        else:
            self._emit(ctx, EventType.done, {"text": text, "stop_reason": reason, "turns": self._turns, "usage": self._usage.model_dump()})
        return LoopResult(text=text, stop_reason=reason, turns=self._turns, usage=self._usage, segment_boundary=boundary)

    def _record_result(self, call_id: str, name: str, result: str, ctx: ToolContext):
        if call_id in self._emitted_results:
            return
        self._emitted_results.add(call_id)
        self._source_context.delivered(call_id, result)
        self._emit(ctx, EventType.tool_result, {"call_id": call_id, "name": name, "result": result,
                   "result_total_chars": len(result), "result_truncated": False})

    def _reconcile_results(self, messages: list[dict[str, Any]], ctx: ToolContext):
        # Publish official synthetic unknown-tool/interruption receipts once.
        for message in messages:
            call_id = message.get("tool_call_id")
            if message.get("role") == "tool" and call_id in self._calls:
                self._record_result(call_id, self._calls[call_id].name, str(message.get("content") or ""), ctx)

    async def _next_turn(self, messages: list[dict[str, Any]], ctx: ToolContext, schemas=None) -> ModelTurn:
        self._emit(ctx, EventType.session_meta, {"phase": "model", "busy": True}, persist=False)
        # Providers own OpenAI wire encoding; their contract is flat function
        # schemas, while the JS adapter sends OpenAI function wrappers.
        schemas = self.registry.schemas() if schemas is None else [row.get("function", row) for row in schemas]
        final = None
        prefix, started, suppressed = "", False, False
        options = replace(current_generation_options() or GenerationOptions(), stream_retry_reset=True)
        with generation_options(options):
            async for chunk in self.provider.stream(provider_messages(messages), schemas):
                if chunk.reset:
                    prefix, started, suppressed = "", False, False
                    self._emit(ctx, EventType.session_meta, {"kind": "model_stream_reset",
                        "attempt": chunk.retry_attempt}, persist=False)
                if chunk.thinking_delta:
                    self._emit(ctx, EventType.thinking_delta, {"text": chunk.thinking_delta}, persist=False)
                if chunk.text_delta and not suppressed:
                    if started:
                        self._emit(ctx, EventType.assistant_delta, {"text": chunk.text_delta}, persist=False)
                    else:
                        prefix += chunk.text_delta
                        body = re.sub(r"^[\s\ufeff]+", "", prefix)
                        if is_text_tool_call(prefix):
                            suppressed, prefix = True, ""
                        elif body and (not body.startswith("<") or len(body) > 256):
                            self._emit(ctx, EventType.assistant_delta, {"text": prefix}, persist=False)
                            started, prefix = True, ""
                if chunk.turn is not None:
                    final = chunk.turn
        if final is None:
            raise RuntimeError("provider.stream 未产出终结 chunk（turn）")
        if not started and final.text and not is_text_tool_call(final.text):
            self._emit(ctx, EventType.assistant_delta, {"text": final.text}, persist=False)
        return final
