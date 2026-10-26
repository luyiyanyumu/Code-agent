"""Conversation coordinators keep independent identities and durable summaries."""

from __future__ import annotations

import sqlite3

import pytest

from code_agent.agent.project_team import ProjectTeam
from test_project_agent_team import team_env


def chat_root(env, title="independent chat"):
    meta = env.manager.session_repo.create(env.owner, title=title, project_id=env.pid, tree_id="main", kind="chat")
    return env.team.bind_conversation(meta)


def worker(env, parent, title):
    return env.team._create_worker(env.project, parent, tree="main", role="subagent", title=title)


def bindings(env, identities):
    return {identity: env.db.query_one("SELECT * FROM project_agents WHERE session_id=?", (identity,)) for identity in identities}


def complete(env, member, result, *, updated_at="2000-01-01T00:00:00+00:00"):
    sid = member["session_id"]
    activation = env.team._begin_activation(sid, f"offline result for {result}", "running")
    env.team._finish(sid, activation, "completed", result=result)
    env.db.execute("UPDATE project_agents SET updated_at=? WHERE session_id=?", (updated_at, sid))


@pytest.mark.parametrize("operation", ["restore", "replace"])
def test_canonical_lead_restore_or_replacement_never_reparents_chat_descendants(team_env, operation):
    e = team_env
    canonical = e.team.sync(e.pid, e.owner)["lead"]
    canonical_child = worker(e, canonical, "default conversation child")
    first, second = chat_root(e, "first root"), chat_root(e, "second root")
    child = worker(e, first, "first root child")
    grandchild = worker(e, child, "first root grandchild")
    second_child = worker(e, second, "second root child")
    chat_ids = [first["session_id"], second["session_id"], child["session_id"], grandchild["session_id"], second_child["session_id"]]
    before = bindings(e, chat_ids)

    if operation == "restore":
        e.db.execute("UPDATE project_agents SET state='retired' WHERE session_id=?", (canonical["session_id"],))
        restored = e.team.bind_lead(e.manager.session_repo.get(canonical["session_id"]))
        assert restored["session_id"] == canonical["session_id"]
    else:
        e.manager.session_repo.close(canonical["session_id"])
        replacement = e.manager.create_project_session(e.owner, e.pid)
        restored = e.team.bind_lead(replacement)
        assert restored["session_id"] != canonical["session_id"]
        assert e.team.descriptor(canonical["session_id"])["state"] == "retired"

    assert bindings(e, chat_ids) == before
    assert e.manager.project_repo.get(e.pid)["session_id"] == restored["session_id"]
    own_default = e.team.descriptor(canonical_child["session_id"])
    assert own_default["parent_session_id"] == restored["session_id"]
    assert own_default["lead_session_id"] == restored["session_id"]
    assert e.team._subtree(first["session_id"]) == sorted([child["session_id"], grandchild["session_id"]])
    assert e.team._subtree(second["session_id"]) == [second_child["session_id"]]
    assert e.manager._runtimes == {}, "rebinding identities must not start providers or runtimes"


def test_multiple_root_sql_uniqueness_and_repeated_team_initialization_are_idempotent(team_env):
    e = team_env
    canonical = e.team.sync(e.pid, e.owner)["lead"]
    chats = [chat_root(e, name) for name in ("first", "second", "third")]
    all_ids = [canonical["session_id"], *(row["session_id"] for row in chats)]
    before = bindings(e, all_ids)
    assert len(e.db.query_all("SELECT * FROM project_agents WHERE project_id=? AND role='lead' AND state!='retired'", (e.pid,))) == 4
    assert [row["name"] for row in e.db.query_all("PRAGMA index_info(project_agents_lead)")] == ["project_id", "lead_session_id"]
    with pytest.raises(sqlite3.IntegrityError):
        e.db.execute("UPDATE project_agents SET lead_session_id=? WHERE session_id=?", (chats[0]["session_id"], chats[1]["session_id"]))
    assert bindings(e, all_ids) == before

    for _ in range(3):
        e.team = ProjectTeam(e.manager)
        e.manager.team = e.team
        e.team.bind_lead(e.manager.session_repo.get(canonical["session_id"]))
        for row in chats:
            e.team.bind_conversation(e.manager.session_repo.get(row["session_id"]))
        assert bindings(e, all_ids) == before
        assert e.manager.project_repo.get(e.pid)["session_id"] == canonical["session_id"]
    assert len(e.db.query_all("SELECT * FROM sessions WHERE project_id=?", (e.pid,))) == 4
    assert e.manager._runtimes == {}
    assert e.db.query_all("SELECT * FROM project_agent_runs") == []


def test_summary_filters_other_conversation_roots_before_the_sql_limit(team_env):
    e = team_env
    own, other = chat_root(e, "own root"), chat_root(e, "unrelated root")
    own_child = worker(e, own, "old own child")
    complete(e, own_child, "ONLY_OWN_ROOT_RESULT")
    interference = []
    for index in range(50):
        member = worker(e, other, f"unrelated result {index}")
        complete(e, member, f"OTHER_ROOT_RESULT_{index}", updated_at="2099-01-01T00:00:00+00:00")
        interference.append(member["session_id"])

    summary = e.team._result_context(e.team._member(own["session_id"], e.owner))
    assert "ONLY_OWN_ROOT_RESULT" in summary and own_child["session_id"] in summary
    assert "OTHER_ROOT_RESULT_" not in summary
    assert all(identity not in summary for identity in interference)
    inverse = e.team._result_context(e.team._member(other["session_id"], e.owner))
    assert "OTHER_ROOT_RESULT_" in inverse and "ONLY_OWN_ROOT_RESULT" not in inverse


def test_summary_contains_strict_recursive_descendants_only(team_env):
    e = team_env
    own, other = chat_root(e, "own root"), chat_root(e, "other root")
    caller = worker(e, own, "caller")
    child = worker(e, caller, "child")
    grandchild = worker(e, child, "grandchild")
    sibling = worker(e, own, "sibling")
    foreign = worker(e, other, "foreign")
    members = [own, caller, child, grandchild, sibling, foreign]
    for index, member in enumerate(members):
        complete(e, member, f"DISTINCT_RESULT_{index}")
    summary = e.team._result_context(e.team._member(caller["session_id"], e.owner))
    assert e.team._subtree(caller["session_id"]) == sorted([child["session_id"], grandchild["session_id"]])
    for index, member in enumerate(members):
        if member in (child, grandchild):
            assert f"DISTINCT_RESULT_{index}" in summary and member["session_id"] in summary
        else:
            assert f"DISTINCT_RESULT_{index}" not in summary
