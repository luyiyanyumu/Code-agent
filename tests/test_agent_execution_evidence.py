"""Independent Lead evidence reads are bounded, attributable and side-effect free."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from code_agent.agent.project_team import TeamError
from code_agent.jobs.registry import DONE, Job, JobRegistry
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.agent_team_tools import ReadAgentExecutionTool
from test_project_agent_team import nodes, team_env


@pytest.fixture
def evidence(team_env):
    e = team_env
    e.manager.transcript = TranscriptStore(e.tmp_path / "transcripts")
    e.lead, e.nodes = nodes(e)
    e.target = e.nodes["verification"]["session_id"]
    return e


def record(e, label, *, markers=True, finish=True, output=None):
    activation = e.team._begin_activation(e.target, label, "running")
    store = e.manager.transcript
    if markers:
        store.append(e.target, "session_meta", {"agent_activation": {
            "activation_id": activation, "phase": "started", "workspace": str(e.root),
        }})
    call = {"id": f"tc_{activation}", "name": "bash", "arguments": {"command": label}}
    use = store.append(e.target, "tool_use", {"call": call})
    result = store.append(e.target, "tool_result", {"call_id": call["id"], "name": "bash", "result": output or f"{label}\n[exit code: 7]"})
    store.append(e.target, "done", {"text": f"report-{label}", "stop_reason": "end_turn"})
    e.team._finish(e.target, activation, "completed", result=f"report-{label}")
    if markers and finish:
        store.append(e.target, "session_meta", {"agent_activation": {"activation_id": activation, "phase": "finished"}})
    return activation, use, result


def read(e, activation, **kwargs):
    return e.team.read_execution(e.lead["session_id"], e.owner, e.target, activation_id=activation, **kwargs)


def test_exact_activation_reads_persisted_calls_results_and_workspace(evidence):
    e = evidence
    old, _, _ = record(e, "old command")
    new, _, result = record(e, "new command")
    response = read(e, new)
    assert response["boundary_status"] == "complete"
    assert response["workspace"] == str(e.root)
    assert response["reported_result"] == "report-new command"
    assert response["events"][1]["payload"] == result.payload
    assert "old command" not in json.dumps(response)
    assert "new command" not in json.dumps(read(e, old))
    assert e.manager._runtimes == {}  # evidence reads never create or execute a runtime


def test_legacy_run_without_boundary_does_not_guess_from_timestamps(evidence):
    e = evidence
    legacy, _, _ = record(e, "legacy", markers=False)
    record(e, "new")
    response = read(e, legacy)
    assert response["boundary_status"] == "missing"
    assert response["events"] == [] and response["available_job_ids"] == []


def test_interrupted_boundary_stops_before_next_activation(evidence):
    e = evidence
    old, _, _ = record(e, "interrupted", finish=False)
    record(e, "different")
    response = read(e, old)
    assert response["boundary_status"] == "interrupted"
    assert "different" not in json.dumps(response)


def test_long_payload_pages_reconstruct_exact_original_without_losing_tail(evidence):
    e = evidence
    original = '日志\\路径\n"中文"' * 2000 + "\n[exit code: 19]"
    activation, _, result = record(e, "compile", output=original)
    fragments = []
    seen = []
    cursor = ""
    for _ in range(80):
        page = read(e, activation, cursor=cursor, limit=1, max_chars=1000)
        assert len(page["events"]) <= 1
        for event in page["events"]:
            if event["seq"] == result.seq:
                fragments.append(event["payload_json_fragment"])
            seen.append(event["seq"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("pagination did not converge")
    assert json.loads("".join(fragments)) == result.payload
    assert len(set(seen)) == 3


def test_ownership_ancestry_project_and_tree_are_checked_before_transcript(evidence, monkeypatch):
    e = evidence
    activation, _, _ = record(e, "private")
    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized read reached the transcript")
    monkeypatch.setattr(e.manager.transcript, "iter_events", forbidden)
    with pytest.raises(KeyError):
        e.team.read_execution(e.lead["session_id"], e.stranger, e.target, activation_id=activation)
    for caller in (e.target, e.nodes["requirements"]["session_id"]):
        with pytest.raises(TeamError):
            e.team.read_execution(caller, e.owner, e.target, activation_id=activation)
    other_root = e.tmp_path / "other"
    other_root.mkdir()
    project = e.manager.project_repo.create(e.owner, "other", str(other_root), str(other_root))
    other = e.team.sync(project["id"], e.owner)["lead"]["session_id"]
    with pytest.raises(KeyError):
        e.team.read_execution(other, e.owner, e.target, activation_id=activation)


def test_worker_can_read_descendant_but_not_a_different_tree(evidence):
    e = evidence
    parent = e.nodes["requirements"]
    child = e.manager.session_repo.create(e.owner, project_id=e.pid, tree_id="main", kind="subagent")
    e.team._insert(child, parent=parent["session_id"], lead=e.lead["session_id"], role="subagent", depth=2, title="child")
    e.target = child["id"]
    activation, _, _ = record(e, "child command")
    response = e.team.read_execution(parent["session_id"], e.owner, e.target, activation_id=activation)
    assert response["events"][0]["payload"]["call"]["arguments"]["command"] == "child command"
    e.db.execute("UPDATE project_agents SET tree_id='task:other' WHERE session_id=?", (e.target,))
    e.db.execute("UPDATE sessions SET tree_id='task:other' WHERE id=?", (e.target,))
    with pytest.raises(TeamError, match="工作树"):
        e.team.read_execution(parent["session_id"], e.owner, e.target, activation_id=activation)


def test_background_snapshot_is_linked_to_run_and_does_not_consume_worker_state(evidence):
    e = evidence
    activation = e.team._begin_activation(e.target, "background test", "running")
    store = e.manager.transcript
    store.append(e.target, "session_meta", {"agent_activation": {"activation_id": activation, "phase": "started", "workspace": str(e.root)}})
    registry = JobRegistry()
    job = Job(id="job_evidence", session_id=e.target, kind="bash", label="compile", status=DONE, result="exit code: 3")
    job._max_ring = 1024
    job.append(b"x" * 1500 + b"verified output")
    registry._jobs[job.id] = job
    registry._order.append(job.id)
    e.manager._runtimes[e.target] = SimpleNamespace(jobs=registry)
    store.append(e.target, "tool_use", {"call": {"id": "bg", "name": "bash", "arguments": {"command": "compile", "run_in_background": True}}})
    store.append(e.target, "tool_result", {"call_id": "bg", "name": "bash", "result": f"started background job {job.id}\n"})
    before = (job._cursor, job.result_read, job.awaited, job.notified, job.killed_by_model)
    first = read(e, activation, job_id=job.id, max_chars=1000)
    second = read(e, activation, job_id=job.id, job_offset=first["job"]["next_offset"], max_chars=1000)
    assert first["job"]["dropped_bytes"] == 491
    assert second["job"]["fragments"]["result"]["text"] == "exit code: 3"
    assert second["job"]["next_offset"] is None
    assert (job._cursor, job.result_read, job.awaited, job.notified, job.killed_by_model) == before
    with pytest.raises(TeamError, match="真实启动记录"):
        read(e, activation, job_id="job_unrelated")
    job.session_id = "other-session"
    assert read(e, activation, job_id=job.id)["job"]["status"] == "unavailable"
    assert registry.peek("", job.id) is None


def test_background_id_in_model_report_does_not_authorize_output(evidence):
    e = evidence
    activation, _, _ = record(e, "echo", output="started background job job_invented\n")
    with pytest.raises(TeamError, match="真实启动记录"):
        read(e, activation, job_id="job_invented")


def test_newest_jobs_remain_visible_and_older_recorded_jobs_stay_authorized(evidence):
    e = evidence
    activation = e.team._begin_activation(e.target, "many background jobs", "running")
    store = e.manager.transcript
    store.append(e.target, "session_meta", {"agent_activation": {"activation_id": activation, "phase": "started", "workspace": str(e.root)}})
    registry = JobRegistry()
    e.manager._runtimes[e.target] = SimpleNamespace(jobs=registry)
    for index in range(55):
        job_id = f"job_many_{index}"
        call_id = f"call_many_{index}"
        store.append(e.target, "tool_use", {"call": {"id": call_id, "name": "bash", "arguments": {"command": f"check {index}", "run_in_background": True}}})
        store.append(e.target, "tool_result", {"call_id": call_id, "name": "bash", "result": f"started background job {job_id}\n"})
        job = Job(id=job_id, session_id=e.target, kind="bash", label=f"check {index}", status="running" if index == 0 else DONE)
        registry._jobs[job_id] = job
        registry._order.append(job_id)
    registry._prune(e.target)
    result = read(e, activation)
    assert len(result["available_job_ids"]) == 50
    assert result["available_job_ids"] == [f"job_many_{index}" for index in range(5, 55)]
    assert read(e, activation, job_id="job_many_54")["job"]["status"] == DONE
    # A long-running old job can outlive the displayed last 50. The display
    # bound must not be (incorrectly) reused as its authorization boundary.
    assert read(e, activation, job_id="job_many_0")["job"]["status"] == "running"


@pytest.mark.parametrize("threshold", [None, 8000])
def test_json_cursor_pages_fit_utf8_budget_without_externalizing(evidence, monkeypatch, threshold):
    from code_agent.context.externalize import externalize_result, window_for

    e = evidence
    if threshold is None:
        monkeypatch.delenv("CODE_AGENT_EXTERNALIZE_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("CODE_AGENT_EXTERNALIZE_THRESHOLD", str(threshold))
    activation, _, result = record(e, "中文验证", output="中文日志😀" * 6000 + "\nexit_code=11")
    tool = ReadAgentExecutionTool(e.team)
    ctx = ToolContext(e.lead["session_id"], e.root, e.owner)
    cursor, fragments = "", []
    for page_number in range(80):
        output = asyncio.run(tool.execute({"session_id": e.target, "activation_id": activation, "cursor": cursor, "max_chars": 32000}, ctx))
        assert len(output.encode("utf-8")) <= sum(window_for(tool.name))
        assert externalize_result(output, "lead", f"page_{page_number}", root=e.tmp_path / "externalized", tool=tool.name) == output
        page = json.loads(output)
        assert output.index('"next_cursor"') < output.index('"events"')
        for event in page["events"]:
            if event["seq"] == result.seq:
                fragments.append(event["payload_json_fragment"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("UTF-8 pagination did not converge")
    assert json.loads("".join(fragments)) == result.payload
    assert not (e.tmp_path / "externalized").exists()


def test_operator_budget_too_small_returns_explicit_error(evidence, monkeypatch):
    e = evidence
    monkeypatch.setenv("CODE_AGENT_EXTERNALIZE_THRESHOLD", "100")
    activation, _, _ = record(e, "check", output="证据" * 3000)
    tool = ReadAgentExecutionTool(e.team)
    output = asyncio.run(tool.execute({"session_id": e.target, "activation_id": activation}, ToolContext(e.lead["session_id"], e.root, e.owner)))
    assert len(output.encode("utf-8")) <= 100
    assert "budget" in json.loads(output)["error"]


def test_runtime_persists_activation_boundaries_for_actual_runs(evidence):
    e = evidence
    runtime = e.manager.get_runtime(e.target, e.owner)
    async def handler(runtime, prompt, attachments, code_context, kwargs):
        e.manager.transcript.append(runtime.session_id, "tool_use", {"call": {"id": "check", "name": "read_file", "arguments": {"path": "source.txt"}}})
        e.manager.transcript.append(runtime.session_id, "tool_result", {"call_id": "check", "name": "read_file", "result": "untouched"})
        return SimpleNamespace(text="checked", stop_reason="end_turn")
    e.manager.handler = handler
    asyncio.run(runtime.submit("inspect"))
    response = read(e, e.team.descriptor(e.target)["activation_id"])
    assert response["boundary_status"] == "complete"
    assert response["workspace"] == str(e.root)
    assert [item["type"] for item in response["events"]] == ["tool_use", "tool_result"]


@pytest.mark.parametrize("extra", [{"activation_id": True}, {"limit": 0}, {"max_chars": 999}, {"cursor": "../other"}, {"job_offset": -1}, {"unknown": 1}])
def test_tool_validates_arguments(evidence, extra):
    e = evidence
    activation, _, _ = record(e, "check")
    tool = ReadAgentExecutionTool(e.team)
    args = {"session_id": e.target, "activation_id": activation, **extra}
    with pytest.raises(ToolError):
        asyncio.run(tool.execute(args, ToolContext(e.lead["session_id"], e.root, e.owner)))


def test_tool_reads_off_event_loop_and_returns_original_payload(evidence, monkeypatch):
    e = evidence
    activation, _, result = record(e, "check")
    tool = ReadAgentExecutionTool(e.team)
    calling_thread = threading.get_ident()
    reader = e.team.read_execution
    def checked_reader(*args, **kwargs):
        assert threading.get_ident() != calling_thread
        return reader(*args, **kwargs)
    monkeypatch.setattr(e.team, "read_execution", checked_reader)
    output = asyncio.run(tool.execute({"session_id": e.target, "activation_id": activation}, ToolContext(e.lead["session_id"], e.root, e.owner)))
    assert json.loads(output)["events"][1]["payload"] == result.payload
