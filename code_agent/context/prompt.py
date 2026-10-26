"""prompt 组装（s15）：Prompt 是运行时组装出来的。

组装顺序（预算内优先级从高到低）：
1. system prompt（角色 + 安全规则）
2. 记忆块（检索命中的 workspace/user 记忆条目）
3. 历史消息（从 JSONL 轨迹重建，压缩到预算内）
4. 本轮 user prompt

budget：默认 24000 字符（约 8-12k token），可用 CODE_AGENT_HISTORY_BUDGET 覆盖。
"""

from __future__ import annotations

import os
from typing import Any

from code_agent.context.compact import compact_messages
from code_agent.context.safety import data_message, wrap_untrusted
from code_agent.memory.retriever import MemoryEntry, retrieve
from code_agent.models.events import (
    Event,
    ModelTurn,
    ProviderPayload,
    ToolCall,
    assistant_message,
    tool_message,
    user_message,
)


def history_budget() -> int:
    return int(os.environ.get("CODE_AGENT_HISTORY_BUDGET", "24000"))


def compose_prompt_with_attachments(prompt: str, attachments: list[dict[str, Any]] | None) -> str:
    """把附件清单拼进发给模型的 prompt（展示层仍用原文，见 loop.run）。

    附件已由 API 层落到会话工作区的 uploads/ 下——这里只负责告诉模型
    "有什么、在哪、怎么读"。注意这是**用户数据**，走 wrap_untrusted 的理由
    不成立（它本来就是给模型的指令素材），但路径列表由服务端生成，不含
    用户可控的注入面。
    """
    if not attachments:
        return prompt
    lines = ["", "[本次消息附带文件]（已保存到会话工作区的 uploads/ 目录）"]
    for a in attachments:
        name = str(a.get("name") or "未命名")
        path = str(a.get("path") or "")
        size = a.get("size")
        size_txt = f"（{size} 字节）" if isinstance(size, int) else ""
        lines.append(f"- {name} → {path}{size_txt}")
    lines.append("需要内容时用工具读取上述路径；二进制文件可先判断类型再决定怎么处理。")
    return prompt + "\n\n" + "\n".join(lines)


def dynamic_history_budget(
    window_tokens: int | None,
    system_chars: int = 0,
    tools_chars: int = 0,
    fraction: float = 0.5,
    max_output_tokens: int | None = None,
) -> int:
    """按模型上下文窗口动态计算历史字符预算（字符级，与 compact_messages 的 max_chars 同单位）。

    预算 = 窗口 × fraction − system/tools 固定开销，token 换回字符按
    1 token ≈ 2.2 UTF-8 字节（与 manager._est_tokens 同口径）。
    配置输出上限时为输出预留空间。窗口未知时退回固定 history_budget()。
    """
    if not window_tokens or window_tokens <= 0:
        return history_budget()
    fixed_tokens = (system_chars + tools_chars) / 2.2
    input_ceiling = max(0, window_tokens - (max_output_tokens or 0))
    usable = max(0.0, min(window_tokens * fraction, input_ceiling) - fixed_tokens)
    # A 4000-character floor must never consume space reserved for output/schema.
    available = max(0, int((input_ceiling - fixed_tokens) * 2.2))
    # Capacity is a hard ceiling, not a target for ordinary working memory.
    # A large model window must not expand every loop into hundreds of thousands
    # of history characters. The explicit history setting remains the soft cap.
    return min(available, max(0, history_budget()), max(4_000, int(usable * 2.2)))


# ---------------------------------------------------------------------------
# 从 JSONL 事件重建对话历史（会话恢复 / 跨 prompt 上下文）
# ---------------------------------------------------------------------------

def build_history(events: list[Event], *, include_incomplete_evidence: bool = True) -> list[dict[str, Any]]:
    """把轨迹事件重建成规范消息序列。

    只取对模型有意义的事件：user_message / assistant_message / tool_result。
    assistant_message 事件里带 tool_calls，tool_result 按 call_id 对齐。
    """
    messages: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    results: dict[str, str] = {}

    def flush_tools() -> None:
        nonlocal pending, results
        if pending is None:
            return
        calls = pending.get("tool_calls", [])
        if all(call["id"] in results for call in calls):
            messages.append(pending)
            messages.extend(tool_message(call["id"], results[call["id"]]) for call in calls)
        else:
            # Interrupted parallel batches must not leave invalid tool pairs.
            # Keep completed facts as data so resuming cannot silently forget
            # a successful write and execute it a second time.
            if pending.get("content"):
                messages.append(assistant_message(ModelTurn(text=pending["content"])))
            facts = []
            for call in calls:
                if call["id"] in results:
                    facts.append(f"已返回 {call['function']['name']} ({call['id']}): {results[call['id']]}")
                else:
                    facts.append(f"未收到结果 {call['function']['name']} ({call['id']})；执行状态需核查")
            if include_incomplete_evidence:
                messages.append(data_message("工具结果尚未收齐，可能仍在执行或轮次已中断；先核查实际状态再决定是否重试。\n" + "\n".join(facts),
                                             label="尚未收齐结果的工具证据"))
        pending, results = None, {}

    for ev in events:
        p = ev.payload
        if ev.type.value == "user_message":
            flush_tools()
            # Older runtimes appended closure questions as automatic user turns.
            # They are internal bookkeeping, not fresh user work to repeat on
            # resume. Keep the durable events and any combined delivery notice.
            closure_kind = p.get("closure_kind")
            closures = p.get("closures")
            legacy_closure = (isinstance(closure_kind, str) and bool(closure_kind.strip())) or (
                type(closures) in (int, float) and closures > 0
            )
            carries_notice = "inbox_ids" in p or "job_notice" in p or p.get("kind") == "job_notice"
            if p.get("auto") is True and legacy_closure and not carries_notice:
                continue
            message = user_message(p.get("text", ""))
            if p.get("auto"):
                message["_context_source"] = "auto"
            messages.append(message)
        elif ev.type.value == "assistant_message":
            flush_tools()
            turn = ModelTurn(
                text=p.get("text") or None,
                tool_calls=[
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments", {}))
                    for tc in p.get("tool_calls", [])
                ],
            )
            message = assistant_message(turn)
            if turn.tool_calls:
                pending = message
            else:
                messages.append(message)
        elif ev.type.value == "tool_result":
            call_id = p.get("call_id", "")
            if pending is not None and any(call["id"] == call_id for call in pending.get("tool_calls", [])):
                results.setdefault(call_id, p.get("result", ""))
        elif ev.type.value == "done":
            flush_tools()
            # 最终回答（无 tool_calls 的收口轮只发 done 不发 assistant_message）
            text = p.get("text") or ""
            if text:
                messages.append(assistant_message(ModelTurn(text=text)))
    flush_tools()
    return messages


# ---------------------------------------------------------------------------
# 记忆块
# ---------------------------------------------------------------------------

def memory_blocks(
    workspace_entries: list,
    user_entries: list,
    query: str,
    top_k: int = 5,
    max_chars: int = 4000,
) -> list[dict[str, Any]]:
    """按 query 检索两层记忆，命中才注入。

    M6 安全加固：记忆来自外部可写文件（不可信），一律降权为 user 角色
    的『数据块』，绝不塞进 system 高权区（防提示词注入）。
    """
    budget = max(0, int(max_chars))
    intro = "以下是当前任务的记忆摘录。置顶条目优先，其余按关键词检索；事实可能过时，请结合实际代码核查：\n"
    fixed = len(data_message(intro, label="历史记忆")["content"])
    if budget <= fixed:
        return []
    hits: list[MemoryEntry] = retrieve(workspace_entries + user_entries, query, top_k=top_k, max_chars=budget - fixed)
    if not hits:
        return []
    labels = {"user": "个人", "workspace": "工作区", "project": "项目", "session": "会话"}
    lines = []
    remaining = budget - fixed
    for hit in hits:
        origin = " · ".join(part for part in (labels.get(hit.scope, hit.scope), hit.label,
                          hit.id, hit.category, hit.source, "置顶" if hit.pinned else "关键词命中") if part)
        prefix = f"- ({origin}) "
        # Include source attribution in the same budget as the text, never cut
        # the untrusted-data boundary markers to make a long entry fit.
        available = remaining - len(prefix) - (1 if lines else 0)
        if available <= 24:
            continue
        content = hit.text
        if len(content) > available:
            marker = "…[记忆摘录省略]"
            content = content[:max(0, available - len(marker))] + marker
        line = prefix + content
        remaining -= len(line) + (1 if lines else 0)
        lines.append(line)
    if not lines:
        return []
    body = intro + "\n".join(lines)
    return [data_message(body, label="历史记忆")]


def knowledge_blocks(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """把知识库检索命中转成 user 角色的数据块（M12）。

    与 memory_blocks 同一套降权防注入策略：知识库来自外部可写文件（不可信），
    一律降权为 user 角色的『数据块』，绝不进 system 高权区。

    M34：带上来源与**归因**（`why`）——"这条是因为字面命中还是语义相近被捞上来的"
    直接影响模型该不该采信：字面命中（符号名/路径/标签）几乎不可能巧合，
    而语义相近只是"可能相关"。不写清归因，等于把判断成本丢给模型猜。
    """
    if not hits:
        return []
    lines: list[str] = []
    for h in hits:
        origin = h.get("origin") or ""
        tag = f"·{origin}" if origin else ""
        why = "、".join(h.get("why") or [])
        meta = h.get("meta") or {}
        loc = meta.get("path") or ""
        suffix = ""
        if loc:
            suffix += f"（来源 {loc}）"
        if why:
            suffix += f"［{why}］"
        lines.append(f"- 《{h['title']}》{tag}(相关度 {h.get('score', 0)}) {suffix} {h['text']}")
    intro = f"以下是按当前问题「{query}」从企业知识库检索到的片段，可能相关，请作为背景参考：\n"
    body = intro + "\n".join(lines)
    return [data_message(body, label="知识库")]


# ---------------------------------------------------------------------------
# 总组装
# ---------------------------------------------------------------------------

def assemble(
    system_prompt: str,
    memory: list[dict[str, Any]],
    history: list[dict[str, Any]],
    prompt: str,
    max_chars: int | None = None,
    *, automatic: bool = False,
) -> list[dict[str, Any]]:
    """产出最终发给 provider 的 messages。"""
    budget = max_chars if max_chars is not None else history_budget()
    compacted = compact_messages(history, max_chars=budget)
    current = user_message(prompt)
    if automatic:
        current["_context_source"] = "auto"
    return [{"role": "system", "content": system_prompt}] + memory + compacted + [current]


def provider_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip local bookkeeping while retaining opaque memory-only protocol state.

    The HTTP adapter, rather than request diagnostics, unwraps ProviderPayload.
    A JSON-restored empty placeholder is not a genuine reasoning receipt.
    """
    return [{key: value for key, value in message.items()
             if key not in ("_context_source", "_execution_progress")
             and (key != "_provider_payload" or isinstance(value, ProviderPayload))}
            for message in messages]
