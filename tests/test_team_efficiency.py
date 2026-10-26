"""Bounded model collaboration payloads; stores and user cards stay complete."""

import asyncio
import json
from types import SimpleNamespace

from code_agent.context.externalize import window_for
from code_agent.models.events import Event, EventType
from code_agent.sessions.delegation_context import delegation_snapshot
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.agent_team_tools import TeamListTool, WaitAgentsTool, _model_team_response, _serialize_model_team_response
from test_project_agent_team import nodes, team_env


def complete(env, member, text, *, state="completed", accepted=False):
    activation = env.team._begin_activation(member["session_id"], "synthetic bounded task", "running")
    env.team._finish(member["session_id"], activation, state, result=text, error="actual simulated failure" if state == "failed" else "")
    if accepted:
        lead, _ = nodes(env)
        env.team.audit(lead["session_id"], env.owner, member["session_id"], activation_id=activation,
                       decision="accepted", reason="synthetic evidence was independently checked")
    return activation


def test_model_roster_retains_all_nodes_and_exact_evidence_refs_without_copying_reports(team_env):
    e = team_env
    lead, owners = nodes(e)
    report = "synthetic evidence; " * 2000
    for member in owners.values():
        complete(e, member, report, accepted=True)
    before = e.team.list(lead["session_id"], e.owner)
    encoded = asyncio.run(TeamListTool(e.team).execute({}, ToolContext(lead["session_id"], e.root, e.owner)))
    after = json.loads(encoded)
    assert [row["session_id"] for row in after["agents"]] == [row["session_id"] for row in before["agents"]]
    assert len(encoded.encode()) < len(json.dumps(before, ensure_ascii=False).encode()) // 10
    assert len(encoded.encode()) <= sum(window_for("team_list"))
    for row in after["agents"]:
        assert len(row["result"]) <= 300 and row["result_truncated"]
        assert row["session_id"] and row["activation_id"] > 0
        assert row["state"] == "completed" and row["audit"]["decision"] == "accepted"
        assert "last_task_message" not in row and "elapsed_seconds" not in row
        # Full HTTP/UI descriptors and journal reports were not rewritten.
        assert e.team.descriptor(row["session_id"])["result"] == report
    assert e.team.list(lead["session_id"], e.owner) == before
    assert after["execution_reader"] == {"tool": "read_agent_execution", "parameters_from": ["session_id", "activation_id"]}


def test_utf8_and_json_escape_costs_leave_room_for_later_roster_identities(team_env):
    e = team_env
    lead, owners = nodes(e)
    for member in owners.values():
        complete(e, member, '模拟结果"\\\n' * 5000)
    raw = asyncio.run(e.team.wait(lead["session_id"], e.owner, [row["session_id"] for row in owners.values()], 0))
    projected = _model_team_response(raw, "wait_agents")
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode()) <= sum(window_for("wait_agents"))
    assert [row["session_id"] for row in projected["agents"]] == [row["session_id"] for row in raw["agents"]]
    assert projected["cursors"] == raw["cursors"]
    assert projected["ready_session_ids"] == raw["ready_session_ids"]
    assert projected["changed_session_ids"] == raw["changed_session_ids"]
    assert all(row["result_truncated"] for row in projected["agents"])


def test_projection_keeps_all_64_targets_and_cursor_receipts():
    rows = [{"session_id": f"s_{i:022d}", "activation_id": i + 1, "state": "completed", "result": "x" * 40_000,
             "audit": {"decision": "pending", "activation_id": i + 1}} for i in range(64)]
    source = {"agents": rows, "cursors": {r["session_id"]: "v1:" + "a" * 32 for r in rows},
              "ready_session_ids": [r["session_id"] for r in rows], "changed_session_ids": [r["session_id"] for r in rows]}
    projected = _model_team_response(source, "wait_agents")
    assert len(projected["agents"]) == 64
    assert [r["session_id"] for r in projected["agents"]] == source["ready_session_ids"]
    folded_cursors = projected.get("cursors") or {r["session_id"]: r["cursor"] for r in projected["agents"]}
    assert folded_cursors == source["cursors"]
    assert sum(len(r["result"]) for r in projected["agents"]) <= 4000
    assert all(r["activation_id"] == i + 1 for i, r in enumerate(projected["agents"]))
    assert all((row["approval_pending"] is False) if "approval_pending" in row else
               ("pending_approval" in row and row["pending_approval"] is None) for row in projected["agents"])
    assert projected["execution_reader"]["tool"] == "read_agent_execution"
    assert len(json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode()) <= sum(window_for("wait_agents"))
    if "after_cursors_from" in projected:
        assert all(r.get("ready", projected["default_ready"]) for r in projected["agents"])
        assert all(r.get("changed", projected["default_changed"]) for r in projected["agents"])
    assert rows[-1]["result"] == "x" * 40_000


def test_maximum_length_node_ids_use_complete_status_table_under_existing_window(tmp_path):
    rows = [{"session_id": f"s_{i:022d}", "activation_id": i + 1, "node_id": f"n{i:03d}" + "a" * 60,
             "state": "completed", "phase": "completed", "result": "synthetic report" * 2000,
             "audit": {"decision": "pending", "activation_id": i + 1}} for i in range(64)]
    source = {"agents": rows, "cursors": {r["session_id"]: "v1:" + "a" * 32 for r in rows},
              "ready_session_ids": [r["session_id"] for r in rows], "changed_session_ids": [r["session_id"] for r in rows]}
    encoded = _serialize_model_team_response(source, "wait_agents", ToolContext("fake", tmp_path))
    projected = json.loads(encoded)
    assert "agent_columns" in projected
    decoded = [dict(zip(projected["agent_columns"], row)) for row in projected["agents"]]
    assert [row["session_id"] for row in decoded] == source["ready_session_ids"]
    assert [row["node_id"] for row in decoded] == [row["node_id"] for row in rows]
    assert {row["session_id"]: row["cursor"] for row in decoded} == source["cursors"]
    assert all(row["changed"] and row["ready"] for row in decoded)
    assert all(row["approval_pending"] is False for row in decoded)
    assert len(encoded.encode()) <= sum(window_for("wait_agents"))


def test_oversized_team_roster_spills_complete_valid_status_data_instead_of_losing_members(agent_home):
    from pathlib import Path
    rows = [{"session_id": f"s_{i:022d}", "activation_id": i + 1, "node_id": f"n{i:03d}" + "a" * 60,
             "state": "completed", "phase": "completed", "result": "synthetic report" * 2000,
             "audit": {"decision": "pending", "activation_id": i + 1}} for i in range(128)]
    encoded = _serialize_model_team_response({"agents": rows}, "team_list", ToolContext("fake", agent_home))
    projected = json.loads(encoded)
    assert projected["member_count"] == 128 and projected["state_counts"] == {"completed": 128}
    path = Path(projected["full_roster_path"])
    assert path.is_relative_to(agent_home)
    full = json.loads(path.read_text(encoding="utf-8"))
    decoded = [dict(zip(full["agent_columns"], row)) for row in full["agents"]]
    assert [row["session_id"] for row in decoded] == [row["session_id"] for row in rows]
    assert all(row["audit_activation_id"] == row["activation_id"] for row in decoded)
    assert len(encoded.encode()) <= sum(window_for("team_list"))


def test_model_pending_forms_keep_ids_and_purpose_but_user_form_stays_complete():
    source = {"session_id": "s_child", "activation_id": 9, "state": "waiting", "phase": "waiting_approval",
              "result": "", "pending_approval": {"approval_id": "a9", "session_id": "s_child", "tool": "write_file",
              "purpose": "修复接口返回类型", "effects": ["改一个文件"], "reason": "只读需批准", "arguments": {"content": "x" * 50000}},
              "pending_questions": [{"question_id": "q9", "session_id": "s_child", "questions": [{"question": "按哪条方案继续？"}]}],
              "budget": {"tokens": 12, "team_tokens": 25, "team_max_tokens": 100, "scope_id": "scope", "started_at": "old"}}
    projected = _model_team_response(source, "wait_agents")
    assert projected["pending_approval"]["purpose"] == source["pending_approval"]["purpose"]
    assert projected["pending_approval"]["approval_id"] == "a9"
    assert projected["pending_approval"]["details_in_user_card"]
    assert "arguments" not in projected["pending_approval"]
    assert projected["pending_questions"][0]["question_id"] == "q9"
    assert projected["budget"] == {"tokens": 12, "team_tokens": 25, "team_max_tokens": 100}
    assert len(source["pending_approval"]["arguments"]["content"]) == 50000


def test_new_model_descriptor_explicitly_clears_an_expired_approval_without_rewriting_history():
    pending = {"session_id": "s_child", "activation_id": 4, "state": "waiting", "phase": "waiting_approval",
               "pending_approval": {"approval_id": "ap_old", "session_id": "s_child", "tool": "bash"}}
    previous = _model_team_response(pending, "wait_agents")
    assert previous["pending_approval"]["approval_id"] == "ap_old"
    current = {"session_id": "s_child", "activation_id": 4, "state": "running", "phase": "tools",
               "pending_approval": None, "result": "A historical report mentioned ap_old."}
    projected = _model_team_response(current, "wait_agents")
    assert "pending_approval" in projected and projected["pending_approval"] is None
    assert previous["pending_approval"]["approval_id"] == "ap_old"
    assert current["result"] == "A historical report mentioned ap_old."
    # List rows use the same explicit absence even when an older record omitted
    # the field entirely; there is no invented approval ID or user decision.
    listed = _model_team_response({"agents": [{"session_id": "s_done", "state": "completed"}]}, "team_list")
    assert listed["agents"][0]["pending_approval"] is None


def test_none_context_does_not_build_parent_model_history(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("none must not reconstruct the parent history")
    monkeypatch.setattr("code_agent.sessions.delegation_context.build_history", unexpected)
    snapshot = delegation_snapshot([Event(seq=7, type=EventType.user_message, payload={"text": "private parent"})],
                                   strategy="none", prompt="independent task", parent_session_id="s_parent")
    assert snapshot == {"strategy": "none", "parent_session_id": "s_parent", "source_seq": 7,
                        "task": "independent task", "history": []}


def test_accepted_context_keeps_audit_identity_instead_of_repeating_report(team_env):
    e = team_env
    lead, owners = nodes(e)
    accepted = owners["requirements"]
    activation = complete(e, accepted, "ACCEPTED_REPORT_BODY" * 2000, accepted=True)
    complete(e, owners["implementation"], "PENDING_REPORT_BODY" * 2000)
    complete(e, owners["verification"], "FAILED_REPORT_BODY" * 2000, state="failed")
    summary = e.team._result_context(e.team._row(lead["session_id"]))
    assert "ACCEPTED_REPORT_BODY" not in summary
    assert accepted["session_id"] in summary and f'"activation_id":{activation}' in summary
    assert '"audit_decision":"accepted"' in summary and '"execution_ref":' in summary
    assert "PENDING_REPORT_BODY" in summary and "FAILED_REPORT_BODY" in summary
    assert "不可信外部数据" in summary
    assert e.team.descriptor(accepted["session_id"])["result"] == "ACCEPTED_REPORT_BODY" * 2000


def test_completion_receipt_bounds_text_and_keeps_exact_execution_identity(team_env):
    e = team_env
    lead, owners = nodes(e)
    member = owners["requirements"]
    report = "synthetic report" * 1000
    activation = complete(e, member, report)
    asyncio.run(e.team._notify_parent(member["session_id"], activation))
    note = e.manager.get_runtime(lead["session_id"], e.owner).notes[-1]
    assert note["result"] == report[:1600] and note["result_truncated"]
    assert note["execution_ref"] == {"session_id": member["session_id"], "activation_id": activation}
    assert note["audit_required"] is False and note["audit_available"] is True
    assert e.db.query_one("SELECT result FROM project_agent_runs WHERE session_id=? AND activation_id=?",
                          (member["session_id"], activation))["result"] == report


def test_reviewed_completion_receipts_do_not_request_another_audit_but_new_activations_stay_pending(team_env):
    e = team_env
    lead, owners = nodes(e)
    member = owners["requirements"]
    parent = e.manager.get_runtime(lead["session_id"], e.owner)
    for decision in ["accepted", "rejected"]:
        activation = complete(e, member, "completed work with a recorded review")
        e.team.audit(lead["session_id"], e.owner, member["session_id"], activation_id=activation,
                     decision=decision, reason="existing verification receipt was checked once")
        logs = e.db.query_all("SELECT activation_id,decision,reason FROM project_agent_audits WHERE session_id=?", (member["session_id"],))
        asyncio.run(e.team._notify_parent(member["session_id"], activation))
        note = parent.notes[-1]
        assert note["audit_required"] is False
        assert note["audit_available"] is False
        assert note["execution_ref"] == {"session_id": member["session_id"], "activation_id": activation}
        assert e.team.descriptor(member["session_id"])["audit"]["decision"] == decision
        assert e.db.query_all("SELECT activation_id,decision,reason FROM project_agent_audits WHERE session_id=?", (member["session_id"],)) == logs
    fresh = complete(e, member, "a new result has not been verified")
    asyncio.run(e.team._notify_parent(member["session_id"], fresh))
    assert parent.notes[-1]["audit_required"] is False
    assert parent.notes[-1]["audit_available"] is True
    assert parent.notes[-1]["execution_ref"]["activation_id"] == fresh
    assert e.team.descriptor(member["session_id"])["audit"]["decision"] == "pending"
    assert len(e.db.query_all("SELECT id FROM project_agent_audits WHERE session_id=?", (member["session_id"],))) == 2
    failed = complete(e, member, "failed work cannot be accepted", state="failed")
    asyncio.run(e.team._notify_parent(member["session_id"], failed))
    assert parent.notes[-1]["audit_required"] is False
    assert parent.notes[-1]["audit_available"] is False


def test_wait_tool_defaults_to_long_wait_and_preserves_explicit_short_wait(tmp_path):
    waits = []
    async def wait(caller, user, ids, timeout, **kwargs):
        waits.append((timeout, kwargs))
        return {"agents": [], "timed_out": True, "cursors": {}, "ready_session_ids": []}
    tool = WaitAgentsTool(SimpleNamespace(wait=wait))
    ctx = ToolContext("lead", tmp_path, "owner")
    asyncio.run(tool.execute({"session_ids": ["child"]}, ctx))
    asyncio.run(tool.execute({"session_ids": ["child"], "timeout_seconds": 0,
                             "mode": "any", "after_cursors": {"child": "v1:" + "a" * 32}}, ctx))
    assert tool.parameters["properties"]["timeout_seconds"]["default"] == 60
    assert waits[0][0] == 60 and waits[1][0] == 0
    assert waits[1][1]["after_cursors"] == {"child": "v1:" + "a" * 32}


def test_evidence_pages_and_message_delivery_contract_are_not_compacted():
    evidence = {"session_id": "s", "state": "completed", "events": [{"result": "evidence" * 1000}], "next_cursor": "3:4"}
    assert _model_team_response(evidence, "read_agent_execution") is evidence
    receipt = {"message_id": "m", "status": "queued", "starts_turn": False}
    assert _model_team_response(receipt, "send_agent_message") == receipt


def test_role_guidance_prioritizes_direct_delivery_and_reuses_valid_receipts_within_current_permissions(team_env):
    e = team_env
    lead, owners = nodes(e)
    text = e.team.role_context(e.manager.get_runtime(lead["session_id"], e.owner))
    assert "既可以协调后台子任务，也可以独立执行任务" in text
    assert "直接查明问题、修改、运行相关验证并交付" in text
    assert "不以 team_list、delegate_agent 或额外审计轮次为前置步骤" in text
    assert "复杂任务或存在可独立推进、责任范围不同的并行工作时再拆分" in text
    assert "隔离单任务" in text and "复用已有执行者" in text and "默认等待 60 秒" in text
    assert "已有有效收据一次核验即可" in text and "不重复派审计或机械重跑" in text
    assert "修复产生新变化、执行身份变化或证据缺口" in text
    assert "audit_agent，它不是普通交付的必经步骤" in text
    assert "不需要额外派出审计智能体" in text
    assert "不自动提升任何保存权限" in text and "只读和规划阶段继续只读" in text
    assert "不亲自修改文件" not in text and "bash、文件修改和提案工具不在 Lead" not in text
    worker = e.team.role_context(e.manager.get_runtime(owners["implementation"]["session_id"], e.owner))
    assert "简单、连续的步骤由自己完成" in worker and "不能擅自标记节点验收完成" in worker
    assert "已有有效收据一次核验即可" in worker and "不把自述当成验证" in worker
    assert "工作目录、实际退出码和关键验证输出" in worker


def test_all_team_roles_require_current_approval_identity_and_recheck_when_the_user_sees_no_card(team_env):
    e = team_env
    lead, owners = nodes(e)
    for member in [lead, owners["implementation"]]:
        context = e.team.role_context(e.manager.get_runtime(member["session_id"], e.owner))
        assert "最新 team_list / wait_agents 返回的 pending_approval" in context
        assert "session_id 与 approval_id" in context
        assert "历史输出中的 ap_" in context and "已超时请求不等于当前待审批" in context
        assert "pending_approval=null 或 approval_pending=false" in context
        assert "不得声称仍卡在审批" in context
        assert "准确目标及入口" in context and "当前主对话的后台子任务区" in context
        assert "不要要求用户切换智能体分支" in context and "以界面实际收到的当前请求为准" in context
        assert "用户说看不到审批卡时，先刷新核对实时状态与目标会话" in context
        assert "查询时快照，生成回复期间也可能超时" in context
        assert "不能承诺用户当前必有审批卡" in context and "核对审批" in context
        assert "无卡反馈不代表用户取消了安全审批" in context
