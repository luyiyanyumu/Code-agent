"""Bounded, source-attributed context snapshots for local subagents."""

from __future__ import annotations

from typing import Any

from code_agent.context.prompt import build_history


CONTEXT_STRATEGIES = frozenset({"task_brief", "none", "all"})


def delegation_snapshot(events: list, *, strategy: str, prompt: str, parent_session_id: str) -> dict:
    if strategy not in CONTEXT_STRATEGIES:
        raise ValueError("上下文策略必须为 task_brief、none 或 all")
    snapshot: dict[str, Any] = {
        "strategy": strategy, "parent_session_id": parent_session_id,
        "source_seq": max((event.seq for event in events), default=0),
        "task": prompt, "history": [],
    }
    if strategy == "none":
        return snapshot
    messages = build_history(events, include_incomplete_evidence=False)
    if strategy == "all":
        # Actual paired message objects are preserved; lifecycle and approval
        # events never enter build_history, nor do unresolved tool bundles.
        snapshot["history"] = messages
        return snapshot
    constraints = [event.payload["text"] for event in events
                   if getattr(event.type, "value", event.type) == "user_message"
                   and not event.payload.get("auto") and event.payload.get("text")]
    reports = [m["content"] for m in messages if m.get("role") == "assistant" and m.get("content")]
    outcomes = [m["content"] for m in messages if m.get("role") == "tool" and m.get("content")]
    snapshot["brief"] = {
        "initial_request": (constraints[0][:1600] if constraints else ""),
        "recent_user_constraints": [s[:700] for s in constraints[-3:]],
        "assistant_reports_unverified": [s[:700] for s in reports[-2:]],
        "recent_tool_evidence": [s[:600] for s in outcomes[-3:]],
    }
    return snapshot
