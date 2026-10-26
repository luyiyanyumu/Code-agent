"""Read-only project work list. Execution, acceptance and integration stay distinct."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

ACTIVE = frozenset({"queued", "running", "waiting", "planning", "merging"})
ATTENTION = frozenset({"planned", "plan_failed", "review", "failed", "conflict", "partial"})
COMPLETED = frozenset({"completed", "merged", "discarded", "cancelled"})


def task_workspace_available(row: dict) -> bool:
    return row.get("status") not in ("merged", "discarded") and bool(row.get("worktree")) and Path(row["worktree"]).is_dir()


def pending_questions_for(agents: list[dict]) -> list[dict]:
    """Keep every live group bound to its actual executor and workspace tree."""
    groups = []
    seen = set()
    for agent in agents:
        if agent.get("state") not in ACTIVE:
            continue
        for group in agent.get("pending_questions") or []:
            if not isinstance(group, dict):
                continue
            question_id = group.get("question_id") or group.get("id")
            key = (agent["session_id"], question_id)
            if not question_id or key in seen:
                continue
            seen.add(key)
            groups.append({**group, "id": question_id, "question_id": question_id,
                           "session_id": agent["session_id"], "tree_id": agent.get("tree_id") or "main"})
    return groups


def tree_agents(team: Any, project: dict, tree: str) -> list[dict]:
    if team is None:
        return []
    rows = team.db.query_all(
        "SELECT session_id FROM project_agents WHERE project_id=? AND user_id=? AND tree_id=? AND state!='retired'",
        (project["id"], project["user_id"], tree),
    )
    values = [value for row in rows if (value := team.descriptor(row["session_id"]))]
    for value in values:
        value["pending_questions"] = pending_questions_for([value])
    return values


def _review(row: dict) -> dict:
    try:
        value = json.loads(row.get("review_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def verification_of(row: dict) -> dict | None:
    review = _review(row)
    value = review.get("verification")
    if isinstance(value, list) and value:
        result = {"ok": all(command.get("ok") is True for command in value), "results": value}
    elif isinstance(value, dict):
        result = dict(value)
    else:
        return None
    result["stale"] = bool(review.get("verification_stale"))
    if result["stale"]:
        result["ok"] = None
    return result


def agent_item(agent: dict) -> dict:
    agent = {**agent, "pending_questions": pending_questions_for([agent])}
    return {
        "id": f"agent:{agent['session_id']}", "kind": "agent", "execution_mode": "team",
        "title": agent.get("title") or "团队任务", "state": agent["state"],
        "tree_id": agent.get("tree_id") or "main", "session_id": agent["session_id"],
        "task_id": "", "batch_id": "", "agent": agent,
        "execution_managed": True,
        **{key: agent.get(key) for key in (
            "parent_session_id", "activation_id", "created_at", "updated_at", "error", "result",
            "phase", "current_tool", "started_at", "elapsed_seconds", "last_progress_at",
            "blocked_reason", "pending_approval", "pending_questions", "audit", "budget",
        )},
        "progress": {key: agent.get(key) for key in (
            "phase", "current_tool", "elapsed_seconds", "started_at", "last_progress_at", "blocked_reason",
        )},
        "verification": None,
    }


def task_item(row: dict, view: dict, agents: list[dict]) -> dict:
    worker = next((a for a in agents if a["session_id"] == row.get("session_id")), None)
    busy = [a for a in agents if a.get("state") in ACTIVE]
    approvals = [a["pending_approval"] for a in busy if a.get("pending_approval")]
    questions = pending_questions_for(busy)
    current = (next((a for a in busy if a.get("pending_approval")), None)
               or next((a for a in busy if a.get("pending_questions")), None)
               or next(iter(busy), None) or worker)
    item = {
        "id": f"task:{row['id']}", "kind": "task", "execution_mode": "isolated",
        "title": row.get("title") or row.get("requirement") or "隔离任务",
        "state": row["status"], "tree_id": f"task:{row['id']}",
        "task_id": row["id"], "batch_id": row.get("batch_id") or "",
        "session_id": row.get("session_id") or "", "task": view,
        "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
        "error": row.get("error") or (worker or {}).get("error") or "",
        "result": (worker or {}).get("result") or "", "agent": worker,
        "agents": agents, "pending_approvals": approvals,
        "pending_questions": questions,
        "pending_approval": approvals[0] if approvals else None,
        "verification": verification_of(row),
        "audit": (worker or {}).get("audit"), "execution_state": (worker or {}).get("state"),
        "integration_state": row["status"], "active": bool(busy) or row["status"] in ACTIVE,
        "workspace_available": task_workspace_available(row),
        "execution_managed": worker is not None,
    }
    for key in ("phase", "current_tool", "started_at", "elapsed_seconds", "last_progress_at", "blocked_reason",
                "parent_session_id", "activation_id", "budget"):
        item[key] = (current or {}).get(key)
    item["progress"] = {key: item.get(key) for key in ("phase", "current_tool", "started_at", "elapsed_seconds", "last_progress_at", "blocked_reason")}
    return item


def project_work_items(*, project: dict, manager: Any, tasks: Any, batches: Any, batch_service: Any,
                       task_view: Callable[[dict], dict], limit: int = 100) -> dict:
    """No sync, dispatch or migration on reads, including reads of older work."""
    team = getattr(manager, "agent_team", None)
    descriptors: list[dict] = []
    if team is not None:
        rows = team.db.query_all(
            "SELECT session_id FROM project_agents WHERE project_id=? AND user_id=? AND state!='retired' "
            "ORDER BY CASE WHEN state IN ('queued','running','waiting') THEN 0 ELSE 1 END, updated_at DESC LIMIT ?",
            (project["id"], project["user_id"], max(limit * 3, 100)),
        )
        descriptors = [value for row in rows if (value := team.descriptor(row["session_id"]))]
        for value in descriptors:
            # Full execution evidence remains in the session/execution endpoint.
            # Polling the list should not transfer forty kilobytes per result.
            value["result"] = str(value.get("result") or "")[:2000]
            value["error"] = str(value.get("error") or "")[:1000]
            value["pending_questions"] = pending_questions_for([value])
    by_tree: dict[str, list[dict]] = {}
    for agent in descriptors:
        by_tree.setdefault(agent.get("tree_id") or "main", []).append(agent)
    lead = next((a for a in descriptors if a.get("role") == "lead" and a["session_id"] == project.get("session_id")), None)
    if team is not None and lead is None:
        # The bounded recent-work query may omit an idle default conversation
        # after many chats. Resolve that identity directly; never pick a chat's
        # coordinator just because it is the newest Lead in the project.
        row = team._row(project.get("session_id") or "")
        if (row and row["project_id"] == project["id"] and row["user_id"] == project["user_id"]
                and row["role"] == "lead" and row["state"] != "retired"):
            lead = team.descriptor(row["session_id"])
    # Fetch batch lanes separately so recent lanes do not push ordinary tasks out of the list.
    task_rows = tasks.db.query_all(
        "SELECT * FROM tasks WHERE project_id=? AND user_id=? AND batch_id='' ORDER BY updated_at DESC LIMIT ?",
        (project["id"], project["user_id"], limit),
    ) if tasks else []
    batch_rows = batches.db.query_all(
        "SELECT * FROM task_batches WHERE project_id=? AND user_id=? ORDER BY updated_at DESC LIMIT ?",
        (project["id"], project["user_id"], limit),
    ) if batches and batch_service else []
    items = [task_item(row, task_view(row), by_tree.get(f"task:{row['id']}", [])) for row in task_rows]
    root_sessions = {row.get("session_id") for row in task_rows}
    for batch in batch_rows:
        planners = [agent for agent in descriptors if agent.get("batch_id") == batch["id"]]
        root_sessions.update(agent["session_id"] for agent in planners)
        lanes = [row for row in tasks.list_for_batch(batch["id"])
                 if row.get("project_id") == project["id"] and row.get("user_id") == project["user_id"]]
        view = batch_service.view(batch, lanes)
        children = [task_item(row, task_view(row), by_tree.get(f"task:{row['id']}", [])) for row in lanes]
        root_sessions.update(row.get("session_id") for row in lanes)
        for lane in view.get("lanes", []):
            child = next((c for c in children if c["task_id"] == lane["task_id"]), {})
            lane.update({key: child.get(key) for key in ("session_id", "agent", "phase", "pending_approval", "pending_approvals", "pending_questions", "verification", "workspace_available", "execution_managed", "active", "agents")})
        approvals = [approval for child in children for approval in child.get("pending_approvals", [])]
        approvals.extend(agent["pending_approval"] for agent in planners if agent.get("pending_approval"))
        questions = [group for child in children for group in child.get("pending_questions", [])]
        questions.extend(pending_questions_for(planners))
        verifications = [child["verification"] for child in children if child.get("verification")]
        failures = sum(value.get("ok") is False for value in verifications)
        stale = any(value.get("stale") for value in verifications)
        verify_ok = False if failures else (True if children and len(verifications) == len(children)
                                            and not stale and all(v.get("ok") is True for v in verifications) else None)
        items.append({
            "id": f"batch:{batch['id']}", "kind": "batch", "execution_mode": "parallel",
            "title": batch.get("title") or batch.get("requirement") or "并行任务",
            "state": batch["status"], "tree_id": "main", "session_id": "", "task_id": "",
            "batch_id": batch["id"], "batch": view, "children": children,
            "agents": planners,
            "created_at": batch.get("created_at"), "updated_at": batch.get("updated_at"),
            "error": batch.get("error") or "", "result": "", "progress": view.get("progress"),
            "pending_approvals": approvals, "pending_approval": approvals[0] if approvals else None,
            "pending_questions": questions,
            "verification": {"ok": verify_ok, "stale": stale, "total": len(children),
                             "verified": len(verifications), "failed": failures},
            "active": batch["status"] in ACTIVE or any(c.get("active") for c in children),
        })
    for agent in descriptors:
        # The primary isolated worker already appears on its owning task/lane.
        if agent["session_id"] in root_sessions or (not agent.get("activation_id") and agent["state"] not in ACTIVE):
            continue
        item = agent_item(agent)
        item["workspace_available"] = True
        tree = agent.get("tree_id") or "main"
        if tree.startswith("task:"):
            item["task_id"] = tree[5:]
            row = tasks.get(item["task_id"]) if tasks else None
            if row and row.get("project_id") == project["id"] and row.get("user_id") == project["user_id"]:
                item["batch_id"] = row.get("batch_id") or ""
                item["workspace_available"] = task_workspace_available(row)
            else:
                item["workspace_available"] = False
        elif agent.get("batch_id"):
            item["batch_id"] = agent["batch_id"]
        items.append(item)
    items.sort(key=lambda item: (bool(item.get("active") or item["state"] in ACTIVE), item.get("updated_at") or ""), reverse=True)
    counts = {
        "total": len(items),
        "active": sum(bool(i.get("active") or i["state"] in ACTIVE) for i in items),
        "attention": sum(bool(i.get("pending_approval") or i.get("pending_questions") or i["state"] in ATTENTION
                              or (i.get("audit") or {}).get("decision") == "pending") for i in items),
        "completed": sum(i["state"] in COMPLETED for i in items),
    }
    return {"project_id": project["id"], "lead": lead, "limits": team.limits() if team else {},
            "items": items, "counts": counts, "capabilities": {"team": team is not None,
            "tasks": tasks is not None, "parallel": batch_service is not None}}
