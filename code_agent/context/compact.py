"""Deterministic context clipping and sourced work checkpoints.

Keep system instructions, the initial task and complete recent tool bundles.
Older material remains bounded, explicitly labeled excerpts. Checkpoints index
persisted goals, declared decisions, actual tool returns and pending work; no
external model is called and no inferred result is presented as verified fact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from code_agent.models.events import ProviderPayload

TOOL_OMIT_MARKER = "[compact] 更早的工具结果已省略"
HISTORY_OMIT_MARKER = "[compact] 更早的对话已压缩省略"
WORK_CHECKPOINT_VERSION = 1
_CONSTRAINT = re.compile(r"必须|只[能读写]|不要|不得|禁止|保持|权限|验收|限制|约束|must|do not|only|permission", re.I)
_DECISION = re.compile(r"决定|采用|选择|计划|结论|待处理|下一步|尚未|未验证|decision|next step|pending|not verified", re.I)


def _excerpt(value: Any, limit: int = 220) -> str:
    """Bound a verbatim excerpt; keep the tail where errors/evidence often sit."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if limit < 32:
        return text[:limit]
    marker = " …[摘录省略]… "
    available = limit - len(marker)
    head = available * 2 // 3
    return text[:head] + marker + text[-(available - head):]


def build_work_checkpoint(
    events: Iterable[Any], previous: dict[str, Any] | None = None, *, max_chars: int = 3000,
) -> dict[str, Any]:
    """Extract a bounded work record from persisted events, without a model.

    This is evidence indexing, not inferred semantic understanding: user goals
    and constraints are excerpts, assistant decisions remain labeled reports,
    tool returns never become a claim that the task passed validation. Every
    entry retains its transcript seq. Pass only events after previous.source_seq
    for incremental updates; replaying the full transcript is also supported.
    Persist this object as session_meta(kind=work_checkpoint) in the caller.
    """
    keys = ("goals", "constraints", "decisions", "results", "pending")
    old = previous if isinstance(previous, dict) and previous.get("version") == WORK_CHECKPOINT_VERSION else {}
    record: dict[str, Any] = {"version": WORK_CHECKPOINT_VERSION, "source_seq": int(old.get("source_seq") or 0)}
    for key in keys:
        record[key] = [dict(item) for item in old.get(key, []) if isinstance(item, dict)]
    record["omitted"] = int(old.get("omitted") or 0)
    cursor = record["source_seq"]
    pending = {str(item.get("call_id")): item for item in record["pending"] if item.get("call_id")}
    todos = [item for item in record["pending"] if item.get("kind") == "todo"]

    for event in events:
        if isinstance(event, dict):
            seq, kind, payload = int(event.get("seq") or 0), event.get("type"), event.get("payload") or {}
        else:
            seq, kind, payload = int(getattr(event, "seq", 0)), getattr(event, "type", ""), getattr(event, "payload", {})
        kind = getattr(kind, "value", kind)
        if seq <= cursor:
            continue
        if kind not in ("user_message", "assistant_message", "done", "tool_use", "tool_result", "approval_request", "approval_decided"):
            # Checkpoint metadata is derived output. Counting it as fresh source
            # would create a new checkpoint on every preparation of unchanged
            # context, with its own seq perpetually moving the source cursor.
            continue
        record["source_seq"] = max(record["source_seq"], seq)
        if kind == "user_message" and not payload.get("auto"):
            text = str(payload.get("text") or "").strip()
            if text:
                record["goals"].append({"seq": seq, "text": _excerpt(text, 280)})
                for line in text.splitlines():
                    if _CONSTRAINT.search(line):
                        record["constraints"].append({"seq": seq, "text": _excerpt(line, 160)})
            todos = []  # todo_write is explicitly scoped to each real prompt.
        elif kind in ("assistant_message", "done"):
            for line in str(payload.get("text") or "").splitlines():
                if _DECISION.search(line):
                    record["decisions"].append({"seq": seq, "text": _excerpt(line, 190), "status": "assistant_report"})
        elif kind == "tool_use":
            call = payload.get("call") or {}
            call_id = str(call.get("id") or "")
            if call_id:
                item = {"seq": seq, "kind": "tool", "call_id": call_id,
                        "tool": str(call.get("name") or ""), "status": "no_result"}
                args = call.get("arguments") or {}
                if args.get("path"):
                    item["target"] = _excerpt(args["path"], 100)
                if item["tool"] == "todo_write" and isinstance(args.get("todos"), list):
                    item["todos"] = [
                        {"seq": seq, "kind": "todo", "text": _excerpt(todo.get("content"), 120),
                         "status": todo.get("status"), "reported_by": "assistant"}
                        for todo in args["todos"][:40] if isinstance(todo, dict)
                        and todo.get("status") in ("pending", "in_progress") and todo.get("content")
                    ]
                pending[call_id] = item
        elif kind == "approval_request":
            call_id = str(payload.get("call_id") or "")
            if call_id in pending:
                pending[call_id]["status"] = "waiting_approval"
        elif kind == "approval_decided":
            call_id = str(payload.get("call_id") or "")
            if call_id in pending:
                pending[call_id]["status"] = "approved_awaiting_result" if payload.get("approved") else "denied_awaiting_result"
        elif kind == "tool_result":
            call_id = str(payload.get("call_id") or "")
            call = pending.pop(call_id, {})
            text = str(payload.get("result") or "")
            failed = text.startswith(("[tool error]", "[permission denied]"))
            if call.get("tool") == "todo_write" and not failed:
                todos = call.get("todos", [])
            result = {"seq": seq, "call_id": call_id, "tool": str(payload.get("name") or call.get("tool") or ""),
                      "status": "failed" if failed else "returned", "text": _excerpt(text)}
            if call.get("target"):
                result["target"] = call["target"]
            record["results"].append(result)

        # Keep extraction memory bounded even on an entire long transcript.
        for key, count in (("goals", 4), ("constraints", 5), ("decisions", 5), ("results", 8)):
            items = record[key]
            if len(items) > count:
                record["omitted"] += len(items) - count
                record[key] = ([items[0]] + items[-count + 1:]) if key == "goals" else items[-count:]
        if len(pending) > 16:
            for call_id in list(pending)[:-16]:
                pending.pop(call_id)
                record["omitted"] += 1

    record["pending"] = list(pending.values()) + todos
    _bound_checkpoint(record, max_chars=max(320, int(max_chars)))
    return record


def _bound_checkpoint(record: dict[str, Any], max_chars: int) -> None:
    def size() -> int:
        return len(json.dumps(record, ensure_ascii=False, separators=(",", ":")))

    # Pending calls need correlation, not a duplicate unbounded todo payload.
    for item in record["pending"]:
        item.pop("todos", None)
    # Retain the initial goal; prefer current pending work over old reports.
    while size() > max_chars:
        removable = next((key for key in ("results", "decisions", "constraints", "goals", "pending")
                          if len(record[key]) > (1 if key in ("goals", "pending") else 0)), None)
        if removable:
            index = 1 if removable == "goals" else 0
            record[removable].pop(index)
            record["omitted"] += 1
            continue
        shortened = False
        for key in ("goals", "pending"):
            for item in record[key]:
                for field in ("text", "target"):
                    if len(str(item.get(field) or "")) > 48:
                        item[field] = _excerpt(item[field], 32)
                        shortened = True
        if shortened:
            continue
        # Extreme tiny budget: preserve the goal and source cursor first.
        if record["pending"]:
            record["pending"].pop(0)
            record["omitted"] += 1
        else:
            break


def work_checkpoint_message(checkpoint: dict[str, Any]) -> dict[str, str]:
    """Render the saved evidence as a low-trust data block, never a system rule."""
    from code_agent.context.safety import data_message

    labels = (("goals", "用户目标摘录"), ("constraints", "用户约束摘录"),
              ("decisions", "智能体声明的决定（尚需结合证据）"), ("results", "实际工具返回摘录（不等于验收通过）"),
              ("pending", "未返回的调用 / 智能体记录的待办"))
    lines = [f"工作检查点，来源轨迹截至 seq {checkpoint.get('source_seq', 0)}。这是确定性摘录，不是模型推断。"]
    for key, label in labels:
        if not checkpoint.get(key):
            continue
        lines.append(label + "：")
        for item in checkpoint[key]:
            text = item.get("text") or item.get("target") or item.get("tool") or item.get("call_id") or ""
            detail = " · ".join(str(item.get(field) or "") for field in ("tool", "status") if item.get(field))
            lines.append(f"- [seq {item.get('seq', 0)}{'; ' + detail if detail else ''}] {text}")
    if checkpoint.get("omitted"):
        lines.append(f"预算内省略 {checkpoint['omitted']} 条旧记录；完整证据保留在会话轨迹中。")
    return data_message("\n".join(lines), label="工作检查点")


def message_size_chars(msg: dict[str, Any]) -> int:
    """Estimate visible and private protocol text, never a billed token count."""
    c = msg.get("content")
    content = len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False)) if c else 0
    calls = msg.get("tool_calls")
    payload = msg.get("_provider_payload")
    private = payload.size_chars if isinstance(payload, ProviderPayload) else 0
    return content + (len(json.dumps(calls, ensure_ascii=False)) if calls else 0) + private


def messages_size_chars(messages: Iterable[dict[str, Any]]) -> int:
    return sum(message_size_chars(message) for message in messages)


_content_len = message_size_chars
_TOOL_SLICE_MARKER = "\n…[首尾摘录]…\n"
_TOOL_LOCATOR_MARKER = "\n[全文定位]\n"
_TOOL_SLICE_END = f"…{TOOL_OMIT_MARKER}"
_COORDINATION_TOOLS = frozenset({"team_list", "wait_agents"})
_COORDINATION_MAX_BYTES = 16 * 1024


def _bounded_coordination_result(name: str, content: Any) -> bool:
    """Recognize the bounded roster contract, not arbitrary JSON/tool output."""
    if name not in _COORDINATION_TOOLS or not isinstance(content, str):
        return False
    if len(content) > _COORDINATION_MAX_BYTES or len(content.encode("utf-8")) > _COORDINATION_MAX_BYTES:
        return False
    try:
        result = json.loads(content)
    except (ValueError, TypeError):
        return False
    reader = result.get("execution_reader") if isinstance(result, dict) else None
    return (isinstance(reader, dict) and reader.get("tool") == "read_agent_execution"
            and reader.get("parameters_from") == ["session_id", "activation_id"])


def _recent_coordination_results(messages: list[dict[str, Any]], keep_last: int = 6) -> set[int]:
    """Bind recent result indices to their actual assistant function calls."""
    tool_indices = [i for i, message in enumerate(messages) if message.get("role") == "tool"]
    recent = set(tool_indices[-keep_last:]) if keep_last > 0 else set()
    names: dict[str, str] = {}
    protected = set()
    for i, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            names = {str(call.get("id") or ""): str((call.get("function") or {}).get("name") or "")
                     for call in message.get("tool_calls") or [] if isinstance(call, dict)}
        elif role == "user":
            names = {}
        elif role == "tool" and i in recent:
            name = names.get(str(message.get("tool_call_id") or ""), "")
            if _bounded_coordination_result(name, message.get("content")):
                protected.add(i)
    return protected


def _tool_excerpt(content: str, body_chars: int, locator: str = "") -> str:
    """Retain literal head/tail evidence and complete externalized locators.

    The body allowance excludes the honest clipping marker and bounded source
    locator. A generated excerpt is stable at the same allowance: repeatedly
    fitting a loop cannot add markers or consume its own evidence preview.
    """
    body_chars = max(0, body_chars)
    clipped = content.endswith(_TOOL_SLICE_END)
    body = content[:-len(_TOOL_SLICE_END)] if clipped else content
    if clipped and _TOOL_LOCATOR_MARKER in body:
        body = body.rsplit(_TOOL_LOCATOR_MARKER, 1)[0]
    body = body.replace(_TOOL_SLICE_MARKER, "") if clipped else body
    if len(body) <= body_chars:
        return content

    # Only complete, bounded original locator lines are retained. A shortened
    # path must never be presented as a usable pointer to the full tool output.
    locators = [line for line in content.splitlines()
                if line.startswith(("全文已存盘: ", "full_roster_path: ")) and len(line) <= 512]
    locator = locator if locator and len(locator) <= 512 else locators[0] if locators else ""
    from code_agent.context.diagnostics import failed_command_excerpt, is_failed_command_output

    if body_chars >= 32 and is_failed_command_output(body):
        excerpt = failed_command_excerpt(body, body_chars)
    elif body_chars < 32:
        excerpt = body[:body_chars]
    else:
        head = (body_chars * 2 + 2) // 3
        tail = body_chars - head
        excerpt = body[:head] + _TOOL_SLICE_MARKER + body[-tail:] if tail else body[:head]
    if locator and locator not in excerpt:
        excerpt += _TOOL_LOCATOR_MARKER + locator
    return excerpt + _TOOL_SLICE_END


def prune_old_tool_results(
    messages: list[dict[str, Any]], keep_last: int = 6, max_keep_chars: int = 2000,
    head_chars: int = 160, *, preserve_tool_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Bound old tool evidence without losing diagnostic tails or source paths.

    Short results remain verbatim; recent long results get a larger head/tail
    preview. The same output can pass through fitting repeatedly unchanged.
    ``head_chars`` is the legacy name for the old result's total body allowance.
    A caller may retain current working-source results verbatim by their actual
    tool call IDs. The enclosing compactor still enforces its overall budget.
    """
    preserved = frozenset(str(value) for value in preserve_tool_ids)
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    keep = set(tool_idx[-keep_last:]) if keep_last > 0 else set()
    coordination = _recent_coordination_results(messages, keep_last)
    out = []
    names: dict[str, str] = {}
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            names = {str(call.get("id") or ""): str((call.get("function") or {}).get("name") or "")
                     for call in m.get("tool_calls") or [] if isinstance(call, dict)}
        elif m.get("role") == "user":
            names = {}
        if m.get("role") != "tool":
            out.append(m)
            continue
        content = m.get("content") or ""
        if i in coordination or str(m.get("tool_call_id") or "") in preserved:
            out.append(m)
            continue
        locator = ""
        name = names.get(str(m.get("tool_call_id") or ""), "")
        if _bounded_coordination_result(name, content):
            path = json.loads(content).get("full_roster_path")
            if isinstance(path, str) and path:
                locator = "full_roster_path: " + path
        excerpt = _tool_excerpt(content, max_keep_chars if i in keep else head_chars, locator)
        out.append(m if excerpt == content else {**m, "content": excerpt})
    return out


def compact_messages(
    messages: list[dict[str, Any]],
    max_chars: int, *, preserve_tool_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Keep real user instructions and recent complete bundles within budget.

    Assistant calls and their results are atomic: trimming between two parallel
    results is as invalid as an orphan result. Interrupted bundles are omitted,
    while plain assistant text can remain. User data blocks are not pinned as
    instructions. Fixed system/user instructions are never silently shortened:
    if they alone exceed the allowance, the caller must reject the oversized
    request rather than trade away user constraints. Sources are never mutated.
    Current source reads named by the caller are preferred over ordinary history,
    within the same budget; they remain paired tool evidence, never instructions.
    """
    preserved = frozenset(str(value) for value in preserve_tool_ids)
    system = [m for m in messages if m.get("role") == "system"]
    rest = prune_old_tool_results([m for m in messages if m.get("role") != "system"],
                                 preserve_tool_ids=preserved)
    groups = _complete_message_groups(rest)
    clean = [m for group in groups for m in group]
    if sum(_content_len(m) for m in clean) <= max_chars:
        return system + clean

    from code_agent.context.safety import _DATA_START

    pinned = {i for i, group in enumerate(groups)
              if group[0].get("role") == "user"
              and group[0].get("_context_source") != "auto"
              and not str(group[0].get("content") or "").startswith(_DATA_START)}
    coordination = {id(rest[i]) for i in _recent_coordination_results(rest)}
    latest_roster = next((i for i in reversed(range(len(groups)))
                          if any(id(message) in coordination for message in groups[i])), None)
    if latest_roster is not None:
        # Coordination JSON already has its own strict UTF-8 output allowance.
        # Retain the latest complete bundle even when fixed user instructions
        # crowd the soft history cap. It remains tool evidence; the provider's
        # hard capacity preflight still decides whether this request can run.
        pinned.add(latest_roster)
    fixed = sum(messages_size_chars(groups[i]) for i in pinned)
    budget = max(0, max_chars - fixed)
    kept: dict[int, list[dict[str, Any]]] = {}
    preferred = [i for i, group in enumerate(groups) if i not in pinned and any(
        message.get("role") == "tool" and str(message.get("tool_call_id") or "") in preserved
        for message in group)]
    # Keep whole exchanges, including all parallel call results. Reserve summary
    # space afterwards: an excerpt of discarded history must not evict source
    # that fits in full. Oversized reads fall through to normal bounded clipping.
    for index in reversed(preferred):
        cost = messages_size_chars(groups[index])
        if cost <= budget:
            kept[index] = groups[index]
            budget -= cost
    reserve = min(600, max(0, budget // 5))
    remaining = budget - reserve
    recent_kept = False
    for index in reversed(range(len(groups))):
        if index in pinned or index in kept:
            continue
        group = groups[index]
        cost = messages_size_chars(group)
        if cost > remaining:
            if not recent_kept:
                # Preserve the most recent complete exchange where its fixed
                # tool-call metadata fits. Trim only content with an honest marker.
                group = _fit_latest_group(group, remaining)
                if group:
                    kept[index] = group
            break
        kept[index] = group
        remaining -= cost
        recent_kept = True
    discarded_indices = [i for i in range(len(groups)) if i not in pinned and i not in kept]
    discarded = [groups[i] for i in discarded_indices]
    summary_message = None
    if discarded and reserve:
        from code_agent.context.safety import data_message

        wrapper_cost = len(data_message("", label="历史压缩摘录")["content"])
        if reserve > wrapper_cost + len(HISTORY_OMIT_MARKER):
            # Old tool output remains low-trust data, including after clipping.
            summary = _history_excerpt(discarded, reserve - wrapper_cost)
            summary_message = data_message(summary, label="历史压缩摘录")
        else:
            # This is a runtime notice, not a model-generated assistant turn.
            # Thinking providers require real reasoning on *every* assistant
            # message when tools are present, including messages with no calls.
            # If the complete low-trust wrapper cannot fit, omit the notice
            # along with the old group. A partial wrapper or a fabricated
            # assistant/reasoning pair is not a valid fallback.
            summary_message = None
    retained = []
    summary_index = discarded_indices[-1] if discarded_indices else -1
    for i, group in enumerate(groups):
        if i in pinned or i in kept:
            retained.extend(group if i in pinned else kept[i])
        if i == summary_index and summary_message:
            retained.append(summary_message)
    return system + retained


def _complete_message_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if message.get("role") == "tool":
            i += 1
            continue
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            groups.append([message])
            i += 1
            continue
        expected = [str(call.get("id") or "") for call in calls]
        j = i + 1
        results: dict[str, dict[str, Any]] = {}
        while j < len(messages) and messages[j].get("role") == "tool":
            call_id = str(messages[j].get("tool_call_id") or "")
            if call_id in expected and call_id not in results:
                results[call_id] = messages[j]
            j += 1
        if all(expected) and len(set(expected)) == len(expected) and all(call_id in results for call_id in expected):
            groups.append([message] + [results[call_id] for call_id in expected])
        elif message.get("content"):
            # This public text really came from the model. Keep its matching
            # memory-only protocol state when incomplete calls are removed;
            # rebuilding a plain dict here used to silently discard reasoning.
            groups.append([{key: value for key, value in message.items() if key != "tool_calls"}])
        i = j
    return groups


def _fit_latest_group(group: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    from code_agent.context.safety import _DATA_END, _DATA_START

    fixed = sum(_content_len({**message, "content": ""}) for message in group)
    available = budget - fixed
    if available < 32 * len(group):
        return []
    per_message = available // len(group)
    fitted = []
    for message in group:
        content = str(message.get("content") or "")
        if message.get("role") == "user" and content.startswith(_DATA_START) and len(content) > per_message:
            header, separator, body = content.partition("\n")
            if not separator or not body.endswith(_DATA_END):
                return []
            wrapper_chars = len(header) + len(_DATA_END) + 2
            if per_message < wrapper_chars + 32:
                return []
            body = body[:-len(_DATA_END)].rstrip("\n")
            excerpt = header + "\n" + _excerpt(body, per_message - wrapper_chars) + "\n" + _DATA_END
        elif message.get("role") != "tool" or len(content) <= per_message:
            excerpt = _excerpt(content, per_message)
        else:
            # A source path must remain complete even in the final bundle. If
            # its metadata cannot fit, omit the complete bundle instead of
            # emitting a fabricated/truncated locator or orphan result.
            locator = next((line for line in content.splitlines()
                            if line.startswith(("全文已存盘: ", "full_roster_path: ")) and len(line) <= 512), "")
            overhead = len(_TOOL_SLICE_END) + len(_TOOL_SLICE_MARKER)
            if locator:
                overhead += len(_TOOL_LOCATOR_MARKER) + len(locator)
            body_budget = per_message - overhead
            if body_budget < 1:
                return []
            excerpt = _tool_excerpt(content, body_budget)
            if len(excerpt) > per_message:
                return []
        fitted.append({**message, "content": excerpt})
    return fitted


def _history_excerpt(groups: list[list[dict[str, Any]]], limit: int) -> str:
    from code_agent.context.safety import _DATA_START

    count = sum(len(group) for group in groups)
    lines = [f"{HISTORY_OMIT_MARKER}（{count} 条）。以下仅为原文摘录，工具返回不等于验收通过："]
    # Work checkpoints in the caller retain durable sourced records; this local
    # excerpt carries a few recent facts through in-segment clipping as well.
    for group in groups[-3:]:
        for message in group:
            content = str(message.get("content") or "")
            if not content:
                continue
            role = message.get("role") or ""
            label = ("工具返回" if role == "tool" else "自动续跑提示" if message.get("_context_source") == "auto"
                     else "外部数据原文" if role == "user" and content.startswith(_DATA_START)
                     else "用户原文" if role == "user" else "智能体原文")
            call_id = message.get("tool_call_id")
            lines.append(f"- {label}{' ' + str(call_id) if call_id else ''}：{_excerpt(content, 100)}")
    return _excerpt("\n".join(lines), limit)
