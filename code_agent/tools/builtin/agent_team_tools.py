"""Model-facing project delegation tools; execution guards live in ProjectTeam."""

from __future__ import annotations

import json
import math
import asyncio
from typing import Any

from code_agent.agent.project_team import ProjectTeam, TeamError
from code_agent.tools.base import Tool, ToolContext, ToolError


_BUDGET_SCHEMA = {"type": "object", "properties": {
    "max_tokens": {"type": "integer", "minimum": 0, "maximum": 10000000, "default": 0},
    "max_seconds": {"type": "integer", "minimum": 0, "maximum": 86400, "default": 0},
}, "additionalProperties": False,
    "description": "Optional per-activation execution budget, clamped by team settings. 0 disables this local cap; token checks occur at model boundaries."}

_WRITE_PATHS_SCHEMA = {"type": "array", "maxItems": 64,
    "items": {"type": "string", "minLength": 1, "maxLength": 1024},
    "description": "Explicit write ownership: [] is read-only; exact files or directory prefixes ending in /; ['.'] exclusively owns the workspace. Paths are relative to the inherited workspace. Narrow/read-only tasks cannot run arbitrary shells or opaque plugins; use the Lead or an isolated worktree task with ['.'] for tests."}
_REASON_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 2000,
    "description": "Explain the independently deliverable result and parallel benefit, or why continuing this existing agent reuses useful context."}
_DEPENDS_SCHEMA = {"type": "array", "maxItems": 16,
    "items": {"type": "string", "minLength": 1, "maxLength": 64},
    "description": "Existing descendant session IDs. The runtime binds their current activation IDs and queues this task until all complete successfully; failure fails this task. [] clears prior dependencies."}
_CONTRACT_FIELDS = ("write_paths", "scope_mode", "delegation_reason", "file_ownership", "depends_on")


def _object(arguments: Any, allowed: set[str]) -> dict:
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise ToolError("工具参数必须是对象，不能包含未知字段")
    return arguments


def _text(args: dict, name: str, *, required: bool = False, limit: int = 40_000) -> str:
    value = args.get(name, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ToolError(f"{name} 必须是{'非空' if required else ''}字符串，最多 {limit} 个字符")
    return value.strip()


def _string_list(args: dict, name: str, *, required: bool = False, maximum: int, item_limit: int) -> list[str] | None:
    if name not in args and not required:
        return None
    value = args.get(name)
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, str) or not item.strip() or len(item) > item_limit for item in value)):
        raise ToolError(f"{name} 必须是最多 {maximum} 项的字符串数组，每项非空且最多 {item_limit} 个字符")
    return [item.strip() for item in value]


def _agent_for_model(row: dict, *, report_chars: int) -> dict:
    """Project a UI descriptor, without changing stored reports or HTTP APIs."""
    # Keep every member identity, including idle nodes at the end of a large
    # list. Reports, prior prompts and display timers are not a team roster.
    fields = ("session_id", "activation_id", "node_id", "tree_id", "parent_session_id",
              "role", "depth", "title", "state", "phase", "effective_permission", "current_tool", "unchanged")
    value = {key: row[key] for key in fields if key in row and row[key] not in ("", None)}
    # Empty write_paths/depends_on are meaningful: read-only and explicitly no
    # dependencies must survive every status projection, including compact rows.
    value.update({key: row[key] for key in _CONTRACT_FIELDS if key in row})
    result = str(row.get("result") or "")
    error = str(row.get("error") or "")
    value.update(result=result[:report_chars], result_truncated=len(result) > report_chars,
                 error=error[:400], error_truncated=len(error) > 400)
    audit = row.get("audit")
    if isinstance(audit, dict):
        value["audit"] = {key: audit[key] for key in ("decision", "activation_id") if key in audit}
        if audit.get("reason"):
            value["audit"]["reason"] = str(audit["reason"])[:300]
    budget = row.get("budget")
    if isinstance(budget, dict) and budget:
        value["budget"] = {key: budget[key] for key in (
            "tokens", "turns", "max_tokens", "max_seconds", "team_tokens", "team_max_tokens", "team_max_seconds"
        ) if key in budget}
    approval = row.get("pending_approval")
    # An explicit clear prevents a coordinator from carrying forward an older
    # approval ID when this newer descriptor reports no live waiter.
    value["pending_approval"] = None
    if isinstance(approval, dict):
        value["pending_approval"] = {key: approval[key] for key in ("approval_id", "session_id", "tool", "purpose_source") if key in approval}
        value["pending_approval"].update(
            purpose=str(approval.get("purpose") or "")[:800],
            reason=str(approval.get("reason") or "")[:400],
            details_in_user_card=True,
        )
    questions = row.get("pending_questions")
    if isinstance(questions, list) and questions:
        # The user card keeps the full questions/options; a coordinator needs
        # their identities and wait state, not another copy of those forms.
        value["pending_questions"] = [{key: item[key] for key in ("question_id", "id", "session_id") if key in item}
                                      | {"details_in_user_card": True} for item in questions if isinstance(item, dict)]
    if row.get("blocked_reason"):
        value["blocked_reason"] = str(row["blocked_reason"])[:300]
    return value


def _model_team_response(result: dict, tool_name: str) -> dict:
    """Bound repeated report text, retaining all rows, cursors and run ids."""
    if tool_name == "read_agent_execution":
        return result  # Actual evidence already has exact, lossless cursor pages.
    from code_agent.context.externalize import window_for

    report_chars = 300 if tool_name == "team_list" else 1200
    rows: list[tuple[dict, dict]] = []

    def project(row: dict) -> dict:
        value = _agent_for_model(row, report_chars=0)
        rows.append((row, value))
        return value

    if "session_id" in result and "state" in result:
        value = project(result)
    else:
        value = dict(result)
        if isinstance(result.get("lead"), dict):
            value["lead"] = project(result["lead"])
        if isinstance(result.get("agents"), list):
            value["agents"] = [project(row) for row in result["agents"]]
    if rows:
        # Each row already owns the exact two parameters. Repeating a second
        # identity dictionary for every member adds nothing to its evidence.
        value["execution_reader"] = {"tool": "read_agent_execution", "parameters_from": ["session_id", "activation_id"]}
    output_budget = sum(window_for(tool_name))
    base_bytes = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if base_bytes > output_budget and isinstance(value.get("agents"), list):
        # Large waits otherwise repeat each identity in agents, cursors, changed
        # and ready lists. Fold those receipts into the same complete roster.
        cursors = value.pop("cursors", {})
        changed = set(value.pop("changed_session_ids", []))
        ready = set(value.pop("ready_session_ids", []))
        member_ids = [target["session_id"] for target in value["agents"]]
        changed_default = sum(sid in changed for sid in member_ids) * 2 >= len(member_ids)
        ready_default = sum(sid in ready for sid in member_ids) * 2 >= len(member_ids)
        small_rows = []
        for source, target in rows:
            small = {key: target[key] for key in ("session_id", "activation_id", "node_id", "state", "phase") if key in target}
            small.update({key: target[key] for key in _CONTRACT_FIELDS if key in target})
            if source.get("audit"):
                small["audit"] = {key: source["audit"][key] for key in ("decision", "activation_id") if key in source["audit"]}
            small["approval_pending"] = bool(source.get("pending_approval"))
            if source.get("pending_questions"):
                small["questions_pending"] = True
            small.update(result="", result_truncated=bool(source.get("result")))
            sid = source["session_id"]
            if sid in cursors:
                small["cursor"] = cursors[sid]
                if (sid in changed) != changed_default:
                    small["changed"] = sid in changed
                if (sid in ready) != ready_default:
                    small["ready"] = sid in ready
            small_rows.append((source, small))
        if isinstance(value.get("lead"), dict):
            value["lead"] = small_rows[0][1]
            value["agents"] = [target for _, target in small_rows[1:]]
        else:
            value["agents"] = [target for _, target in small_rows]
        rows = small_rows
        value["details_omitted"] = "Status only; full details remain in user cards."
        if cursors:
            value.update(default_changed=changed_default, default_ready=ready_default)
            value["after_cursors_from"] = "Map agent session_id:cursor. Missing ready/changed use defaults."
    # Budget UTF-8 JSON bytes, including quotes/backslashes. This leaves room
    # for every identity/cursor rather than spending it on the first report.
    base_bytes = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if base_bytes > output_budget and isinstance(value.get("agents"), list):
        # Extremely long node ids still deserve complete identities. Column
        # names once plus positional rows avoid repeating twelve JSON keys for
        # every member. Only this documented status table omits report bodies.
        columns = ["session_id", "activation_id", "node_id", "state", "phase", "audit_decision",
                   "audit_activation_id", "cursor", "approval_pending", "questions_pending", "changed", "ready"]
        contract_columns = [key for key in _CONTRACT_FIELDS if any(key in row for row in value["agents"])]
        columns.extend(contract_columns)
        table = []
        for row in value["agents"]:
            audit = row.get("audit") or {}
            table.append([row.get("session_id"), row.get("activation_id", 0), row.get("node_id", ""),
                          row.get("state"), row.get("phase", ""), audit.get("decision", "none"),
                          audit.get("activation_id", 0), row.get("cursor", ""), bool(row.get("approval_pending")),
                          bool(row.get("questions_pending")), row.get("changed", value.get("default_changed")),
                          row.get("ready", value.get("default_ready")), *[row.get(key) for key in contract_columns]])
        value["agent_columns"], value["agents"] = columns, table
        value["details_omitted"] = "Status table; all members retained. Read rows using agent_columns."
        return value
    remaining_bytes = max(0, output_budget - base_bytes - 64)
    remaining_chars = 4000
    for row, target in rows:
        full = str(row.get("result") or "")
        candidate = full[:min(report_chars, remaining_chars)]
        while candidate:
            used = len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) - 2
            if used <= remaining_bytes:
                break
            candidate = candidate[:max(0, int(len(candidate) * remaining_bytes / used) - 1)]
        target.update(result=candidate, result_truncated=len(candidate) < len(full))
        remaining_bytes -= len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) - 2
        remaining_chars -= len(candidate)
    return value


def _serialize_model_team_response(result: dict, tool_name: str, ctx: ToolContext) -> str:
    value = _model_team_response(result, tool_name)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if "agent_columns" not in value and not any(key in value for key in _CONTRACT_FIELDS):
        return encoded
    from code_agent.context.externalize import window_for

    if len(encoded.encode("utf-8")) <= sum(window_for(tool_name)):
        return encoded
    # An operator can set an arbitrarily small window, and team_list can grow
    # beyond wait's 64-target cap. Preserve the complete valid status JSON in
    # the established tool-result cache; never silently remove later members.
    import hashlib
    from code_agent.config import tool_results_dir

    encoded_bytes = encoded.encode("utf-8")
    directory = tool_results_dir() / ctx.session_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"team_roster_{hashlib.sha256(encoded_bytes).hexdigest()[:16]}.json"
    path.write_text(encoded, encoding="utf-8")
    columns = value.get("agent_columns")
    agents = value.get("agents") if isinstance(value.get("agents"), list) else [value]
    counts: dict[str, int] = {}
    for row in agents:
        state = str(row[columns.index("state")] if columns else row.get("state", ""))
        counts[state] = counts.get(state, 0) + 1
    summary = {"member_count": len(agents), "state_counts": counts,
               "full_roster_path": str(path), "full_roster_bytes": len(encoded_bytes),
               "execution_reader": value.get("execution_reader"),
               "notice": "Complete member identities, audit ids and wait cursors remain in full_roster_path; no member was dropped. Status is not verification evidence."}
    if any(key in (columns or value) for key in _CONTRACT_FIELDS):
        summary["contract_fields"] = list(_CONTRACT_FIELDS)
        summary["contract_notice"] = "Exact write scopes, ownership and bound dependencies are preserved in full_roster_path; read them before changing or continuing delegated work."
    for key in ("session_id", "activation_id", "state"):
        if key in value:
            summary[key] = value[key]
    for key in ("lead", "mode", "timed_out"):
        if key in value:
            summary[key] = value[key]
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


class _TeamTool(Tool):
    def __init__(self, team: ProjectTeam):
        self.team = team

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        try:
            result = await self.perform(arguments, ctx)
            return _serialize_model_team_response(result, self.name, ctx)
        except (TeamError, KeyError) as exc:
            raise ToolError(str(exc).strip("'\"")) from exc

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        raise NotImplementedError


class TeamListTool(_TeamTool):
    name = "team_list"
    description = ("Inspect the current conversation's Lead and its background descendant task states. This never starts execution. "
                   "Workers can see only their own subtree. Reports are bounded summaries; use read_agent_execution with "
                   "the exact agent session_id and activation_id for evidence. Large status tables use agent_columns; "
                   "full_roster_path preserves oversized complete rosters. Use wait_agents when blocked, rather than polling this list.")
    description_zh = "查看当前对话及其后台子智能体状态，不会启动执行，也不会访问其他对话。"
    parameters = {"type": "object", "properties": {"tree": {"type": "string", "description": "Lead may choose main or task:<id>; workers inherit their tree."}}, "additionalProperties": False}

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        args = _object(arguments, {"tree"})
        tree = _text(args, "tree", limit=101) or None
        return self.team.list(ctx.session_id, ctx.user_id, tree)


class DelegateAgentTool(_TeamTool):
    name = "delegate_agent"
    description = (
        "Delegate independently deliverable work only when parallel progress is useful; simple continuous steps stay with the current agent. "
        "Provide prompt, explicit write_paths and delegation_reason; overlapping declared write scopes queue instead of running together. "
        "Create a background child task under the current conversation; workers create descendants only. "
        "The main assistant owns the user-facing conversation and integrates child results. "
        "Returns immediately with a persistent session_id and activation_id. Creating/delegating does not mean completion. "
        "Depth and project-wide descendant parallelism are enforced by the runtime."
    )
    description_zh = "为当前对话按需创建后台子智能体，主助手统一交付；深度与并行上限由运行时约束。"
    parameters = {
        "type": "object", "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 40000},
            "write_paths": _WRITE_PATHS_SCHEMA,
            "delegation_reason": _REASON_SCHEMA,
            "depends_on": _DEPENDS_SCHEMA,
            "parent_session_id": {"type": "string", "maxLength": 64, "description": "Lead may choose an existing descendant parent; workers can only choose themselves."},
            "title": {"type": "string", "maxLength": 120},
            "tree": {"type": "string", "maxLength": 101},
            "context_strategy": {"type": "string", "enum": ["task_brief", "none", "all"], "default": "task_brief",
                                 "description": "task_brief transfers a bounded task/context summary; none omits inherited parent context; all inherits a safe completed conversation snapshot."},
            "budget": _BUDGET_SCHEMA,
        }, "required": ["prompt", "write_paths", "delegation_reason"], "additionalProperties": False,
    }

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        args = _object(arguments, {"prompt", "parent_session_id", "title", "tree", "context_strategy", "budget", "write_paths", "delegation_reason", "depends_on"})
        return await self.team.start(
            ctx.session_id, ctx.user_id, _text(args, "prompt", required=True),
            parent_id=_text(args, "parent_session_id", limit=64),
            title=_text(args, "title", limit=120), tree=_text(args, "tree", limit=101) or None,
            context_strategy=args.get("context_strategy", "task_brief"),
            budget=args.get("budget"),
            write_paths=_string_list(args, "write_paths", required=True, maximum=64, item_limit=1024),
            delegation_reason=_text(args, "delegation_reason", required=True, limit=2000),
            depends_on=_string_list(args, "depends_on", maximum=16, item_limit=64),
        )


class SendAgentMessageTool(_TeamTool):
    name = "send_agent_message"
    description = ("Queue supplementary instructions for an existing owned descendant, delivered at its next model boundary or next turn. "
                   "This never starts an idle agent, cancels execution, or creates another agent. Use followup_agent to continue an idle task.")
    description_zh = "向已有后代发送补充信息，下个模型边界或轮次读取，不启动空闲会话。"
    parameters = {"type": "object", "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "message": {"type": "string", "minLength": 1, "maxLength": 40000},
    }, "required": ["session_id", "message"], "additionalProperties": False}

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        args = _object(arguments, {"session_id", "message"})
        return await self.team.send_message(ctx.session_id, ctx.user_id, _text(args, "session_id", required=True, limit=64),
                                            _text(args, "message", required=True))


class FollowupAgentTool(_TeamTool):
    name = "followup_agent"
    description = ("Continue an existing idle/completed/interrupted owned descendant using its same workspace and conversation. "
                   "Creates a new activation_id, not a new agent. Explain its reuse value in delegation_reason. "
                   "Omit write_paths to inherit its last scope, or supply a new explicit scope. Busy agents reject follow-ups; send_agent_message supplements a running task.")
    description_zh = "复用已有空闲或已结束的后代继续任务，保留历史与工作区并生成新的执行编号。"
    parameters = {"type": "object", "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 40000},
        "write_paths": _WRITE_PATHS_SCHEMA,
        "delegation_reason": _REASON_SCHEMA,
        "depends_on": _DEPENDS_SCHEMA,
        "context_strategy": {"type": "string", "enum": ["task_brief", "none", "all"], "default": "task_brief"},
        "budget": _BUDGET_SCHEMA,
    }, "required": ["session_id", "prompt", "delegation_reason"], "additionalProperties": False}

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        args = _object(arguments, {"session_id", "prompt", "context_strategy", "budget", "write_paths", "delegation_reason", "depends_on"})
        return await self.team.followup(ctx.session_id, ctx.user_id, _text(args, "session_id", required=True, limit=64),
                                       _text(args, "prompt", required=True), context_strategy=args.get("context_strategy", "task_brief"),
                                       budget=args.get("budget"),
                                       write_paths=_string_list(args, "write_paths", maximum=64, item_limit=1024),
                                       delegation_reason=_text(args, "delegation_reason", required=True, limit=2000),
                                       depends_on=_string_list(args, "depends_on", maximum=16, item_limit=64))


class InterruptAgentTool(_TeamTool):
    name = "interrupt_agent"
    description = ("Interrupt only an owned descendant's current turn, preserving its descendants, workspace and history for followup_agent. "
                   "Use stop_agent when the whole descendant tree must stop. Cannot interrupt yourself, ancestors, siblings or foreign agents.")
    description_zh = "只中断后代当前轮次，保留其子任务，便于继续派工。"
    parameters = {"type": "object", "properties": {"session_id": {"type": "string", "minLength": 1, "maxLength": 64}},
                  "required": ["session_id"], "additionalProperties": False}

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        args = _object(arguments, {"session_id"})
        return await self.team.interrupt(ctx.session_id, ctx.user_id, _text(args, "session_id", required=True, limit=64))


class WaitAgentsTool(_TeamTool):
    name = "wait_agents"
    suspends_session = True
    description = (
        "Wait for your descendant agents, up to 60 seconds, and read their latest actual results. "
        "mode=all waits until all targets stop executing; mode=any returns on the first fresh settled target or live pending approval. "
        "Defaults to 60 seconds; pass a shorter timeout only when you have other work to do. "
        "Pass returned cursors as after_cursors to avoid immediately receiving the same completed output again. "
        "Large rosters put cursor/changed/ready on each agent; map session_id to cursor for after_cursors. "
        "Status tables use agent_columns; full_roster_path preserves oversized complete rosters and cursors. "
        "Reports are bounded, unverified summaries; use each agent's exact session_id and activation_id with read_agent_execution for evidence. "
        "Use this when blocked on results instead of polling team_list. A waiting worker releases its execution slot "
        "so children can run even when max_parallel=1, then reacquires a slot before resuming."
    )
    description_zh = "等待后代智能体结果，等待期间释放运行名额，避免递归任务互相堵塞。"
    parameters = {"type": "object", "properties": {
        "session_ids": {"type": "array", "minItems": 1, "maxItems": 64, "items": {"type": "string", "minLength": 1}},
        "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 60, "default": 60},
        "mode": {"type": "string", "enum": ["any", "all"], "default": "all"},
        "after_cursors": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 35}},
    }, "required": ["session_ids"], "additionalProperties": False}

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        args = _object(arguments, {"session_ids", "timeout_seconds", "mode", "after_cursors"})
        timeout = args.get("timeout_seconds", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ToolError("timeout_seconds 必须是 0 到 60 的有限数值")
        return await self.team.wait(ctx.session_id, ctx.user_id, args.get("session_ids"), timeout,
                                    mode=args.get("mode", "all"), after_cursors=args.get("after_cursors"))


class StopAgentTool(_TeamTool):
    name = "stop_agent"
    description = "Stop a descendant agent and its owned descendants. Cannot stop yourself, ancestors, siblings or another project's agent. Preserves transcripts and reports."
    description_zh = "停止自己的后代智能体及其子任务，保留执行记录。"
    parameters = {"type": "object", "properties": {"session_id": {"type": "string", "minLength": 1, "maxLength": 64}}, "required": ["session_id"], "additionalProperties": False}

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        args = _object(arguments, {"session_id"})
        return await self.team.stop(ctx.session_id, ctx.user_id, _text(args, "session_id", required=True, limit=64))


class AuditAgentTool(_TeamTool):
    name = "audit_agent"
    description = (
        "Lead only: accept or reject a completed descendant's exact activation_id with concrete evidence or a rejection reason. "
        "A completion report is not proof; inspect changes and verification first. Stale run ids cannot approve newer output. "
        "This records an audit decision and does not merge code or publish."
    )
    description_zh = "Lead 按执行编号核对证据并接受或驳回产出。"
    parameters = {"type": "object", "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "activation_id": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1},
        "decision": {"type": "string", "enum": ["accepted", "rejected"]},
        "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
    }, "required": ["session_id", "activation_id", "decision", "reason"], "additionalProperties": False}

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        args = _object(arguments, {"session_id", "activation_id", "decision", "reason"})
        activation = args.get("activation_id")
        if type(activation) is not int or not 1 <= activation <= 2**63 - 1:
            raise ToolError("activation_id 必须是正整数")
        return self.team.audit(
            ctx.session_id, ctx.user_id, _text(args, "session_id", required=True, limit=64), activation_id=activation,
            decision=_text(args, "decision", required=True, limit=8), reason=_text(args, "reason", required=True, limit=4000),
        )


class ReadAgentExecutionTool(_TeamTool):
    name = "read_agent_execution"
    description = (
        "Read persisted tool calls, actual results and completion evidence for one exact descendant activation. "
        "Uses session_id and activation_id from delegation/team_list. Lead may inspect its current conversation descendants; "
        "workers may inspect only descendants in their own tree. This never executes commands or accepts an audit. "
        "Follow next_cursor for more events or JSON payload fragments; reported_result alone is not verification. "
        "Optional job_id reads a non-consuming live output snapshot only for a background job actually started in this activation."
    )
    description_zh = "按精确执行编号读取后代真实工具轨迹、运行证据及关联后台输出，不执行命令，不消耗节点输出游标。"
    parameters = {"type": "object", "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "activation_id": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1},
        "cursor": {"type": "string", "maxLength": 80, "description": "Exact next_cursor returned by the previous page. Omit for the first page."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "max_chars": {"type": "integer", "minimum": 1000, "maximum": 32000, "default": 16000},
        "job_id": {"type": "string", "maxLength": 64, "description": "Choose an available_job_ids entry to read its live snapshot instead of transcript events."},
        "job_offset": {"type": "integer", "minimum": 0, "description": "Job snapshot next_offset; output may change while running."},
    }, "required": ["session_id", "activation_id"], "additionalProperties": False}

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        from code_agent.context.externalize import window_for

        # Character limits alone do not bound UTF-8 JSON (Chinese, emoji and
        # escaped fragments cost more). Keep cursor pages parseable under the
        # actual downstream budget, including an explicit operator override.
        args = _object(arguments, {"session_id", "activation_id", "cursor", "limit", "max_chars", "job_id", "job_offset"})
        result = await super().execute(args, ctx)
        budget = sum(window_for(self.name))
        chars = args.get("max_chars", 16000)
        while len(result.encode("utf-8")) > budget and chars > 1000:
            chars = max(1000, int(chars * budget / len(result.encode("utf-8")) * 0.75))
            result = await super().execute({**args, "max_chars": chars}, ctx)
        if len(result.encode("utf-8")) > budget:
            # An extremely low operator override cannot fit even the minimum
            # evidence page; say so rather than expose a broken JSON fragment.
            return json.dumps({"error": "Output budget too small for evidence", "budget_bytes": budget}, ensure_ascii=False)
        return result

    async def perform(self, arguments: dict, ctx: ToolContext) -> dict:
        args = _object(arguments, {"session_id", "activation_id", "cursor", "limit", "max_chars", "job_id", "job_offset"})
        return await asyncio.to_thread(
            self.team.read_execution, ctx.session_id, ctx.user_id,
            _text(args, "session_id", required=True, limit=64), activation_id=args.get("activation_id"),
            cursor=_text(args, "cursor", limit=80), limit=args.get("limit", 20), max_chars=args.get("max_chars", 16000),
            job_id=_text(args, "job_id", limit=64), job_offset=args.get("job_offset", 0),
        )


def register_agent_team_tools(registry: Any, team: ProjectTeam) -> None:
    for cls in (TeamListTool, DelegateAgentTool, SendAgentMessageTool, FollowupAgentTool, InterruptAgentTool,
                WaitAgentsTool, StopAgentTool, AuditAgentTool, ReadAgentExecutionTool):
        registry.register(cls(team))

