"""Restart guard tests: temporary SQLite and synthetic inspection endpoints only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


spec = importlib.util.spec_from_file_location("backend_idle_check", Path(__file__).parents[1] / "scripts/backend_idle_check.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "code_agent.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE sessions(id TEXT PRIMARY KEY,user_id TEXT,status TEXT);
            CREATE TABLE projects(id TEXT PRIMARY KEY,user_id TEXT);
            CREATE TABLE project_agents(session_id TEXT,project_id TEXT,tree_id TEXT,user_id TEXT,state TEXT,activation_id INTEGER,updated_at TEXT);
            CREATE TABLE approvals(id TEXT,session_id TEXT,user_id TEXT,status TEXT);
            CREATE TABLE tasks(id TEXT,user_id TEXT,status TEXT);
            CREATE TABLE task_batches(id TEXT,user_id TEXT,status TEXT);
            INSERT INTO sessions VALUES('s1','owner','active');
            INSERT INTO projects VALUES('p1','owner');
            INSERT INTO project_agents VALUES('s1','p1','main','owner','completed',1,'t0');
        """)
    return path


class Inspections:
    def __init__(self, *, listed=None, mutate=None):
        self.paths = []
        self.mutate = mutate
        self.responses = {
            "/api/v1/auth/me": {"id": "owner"},
            "/api/v1/sessions": listed if listed is not None else [{"id": "s1", "status": "active"}],
            "/api/v1/code/projects": [{"id": "p1"}],
            "/api/v1/code/projects/p1/work-items?limit=100": {
                "project_id": "p1", "capabilities": {"team": True}, "counts": {"active": 0},
                "lead": {"state": "completed", "pending_questions": [], "pending_approval": None},
                "items": [{"kind": "agent", "state": "completed", "pending_questions": [], "pending_approval": None}],
            },
        }

    def __call__(self, path):
        self.paths.append(path)
        if self.mutate:
            self.mutate(path)
        if path in self.responses:
            return self.responses[path]
        suffix = path.rsplit("/", 1)[1]
        if suffix == "checkpoints":
            return {"busy": False, "checkpoints": [{"private": "do not print"}]}
        if suffix == "jobs":
            return {"jobs": [], "running": 0}
        if path.endswith("/questions/pending"):
            return {"questions": []}
        if path.endswith("/approvals/pending"):
            return {}
        raise AssertionError("Unexpected endpoint: " + path)


def test_idle_uses_only_inspection_routes_and_reports_counts(database):
    get = Inspections()
    report = guard.check_idle(database, get)
    assert report["idle"] is True
    assert report["checked_sessions"] == 1
    assert report["checked_team_trees"] == 1
    assert not any(path.endswith("/agents") or "/index" in path for path in get.paths)
    assert all(isinstance(value, (int, bool)) for value in report.values())


def test_all_sessions_beyond_the_http_list_cap_are_checked(database):
    with sqlite3.connect(database) as con:
        con.executemany("INSERT INTO sessions VALUES(?, 'owner', 'active')", [(f"s{i}",) for i in range(2, 54)])
    get = Inspections(listed=[{"id": f"s{i}", "status": "active"} for i in range(1, 51)])
    get.responses["/api/v1/sessions/s53/checkpoints"] = {"busy": True}
    report = guard.check_idle(database, get)
    assert report["idle"] is False
    assert report["checked_sessions"] == 53
    assert report["busy_sessions"] == 1
    assert "/api/v1/sessions/s53/jobs" in get.paths


@pytest.mark.parametrize("status", ["running", "queued"])
def test_independent_job_prevents_restart_when_session_is_idle(database, status):
    get = Inspections()
    get.responses["/api/v1/sessions/s1/jobs"] = {"jobs": [{"status": status}], "running": int(status == "running")}
    report = guard.check_idle(database, get)
    assert report["busy_sessions"] == 0
    assert report["active_jobs"] == 1
    assert report["idle"] is False


@pytest.mark.parametrize("state", ["queued", "running", "waiting"])
def test_full_database_team_covers_trees_missing_from_http_work_list(database, state):
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO sessions VALUES('old-hidden','owner','active')")
        con.execute("INSERT INTO project_agents VALUES('old-hidden','p1','task:old','owner',?,1,'t0')", (state,))
    report = guard.check_idle(database, Inspections())
    assert report["db_active_agents"] == 1
    assert report["checked_team_trees"] == 2
    assert report["idle"] is False


def test_pending_human_question_and_approval_are_not_answered(database):
    get = Inspections()
    get.responses["/api/v1/sessions/s1/questions/pending"] = {"questions": [{"question": "secret question"}]}
    get.responses["/api/v1/sessions/s1/approvals/pending"] = {"approval_id": "a1", "command": "private command"}
    report = guard.check_idle(database, get)
    assert report["pending_questions"] == report["pending_approvals"] == 1
    assert report["idle"] is False
    assert "secret" not in json.dumps(report) and "private" not in json.dumps(report)


def test_legacy_tasks_batch_merge_and_other_owner_pending_approval_block(database):
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO tasks VALUES('t1','owner','planning')")
        con.execute("INSERT INTO task_batches VALUES('b1','owner','merging')")
        con.execute("INSERT INTO approvals VALUES('a1','other-session','other','pending')")
    report = guard.check_idle(database, Inspections())
    assert report["db_active_tasks"] == report["db_active_batches"] == report["pending_approvals"] == 1
    assert report["idle"] is False


def test_other_account_active_session_fails_closed(database):
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO sessions VALUES('other','other-owner','active')")
    get = Inspections()
    with pytest.raises(guard.CannotConfirm, match="other_owner_active_sessions"):
        guard.check_idle(database, get)
    assert get.paths == ["/api/v1/auth/me"]


@pytest.mark.parametrize("status,state,allowed", [
    ("closed", "completed", True), ("archived", "failed", True),
    ("closed", "cancelled", True), ("closed", "running", False),
    ("archived", "waiting", False), ("closed", "idle", True),
    ("archived", "idle", True),
])
def test_closed_terminal_team_history_does_not_permanently_block(database, status, state, allowed):
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO sessions VALUES('historical','owner',?)", (status,))
        con.execute("INSERT INTO project_agents VALUES('historical','p1','task:old','owner',?,1,'t0')", (state,))
    if allowed:
        report = guard.check_idle(database, Inspections())
        assert report["idle"] is True
        assert report["checked_sessions"] == 1
    else:
        with pytest.raises(guard.CannotConfirm, match="team_session_not_active"):
            guard.check_idle(database, Inspections())


def test_orphan_terminal_team_member_remains_unknown(database):
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO project_agents VALUES('missing','p1','task:old','owner','completed',1,'t0')")
    with pytest.raises(guard.CannotConfirm, match="team_session_not_active"):
        guard.check_idle(database, Inspections())


@pytest.mark.parametrize("response,error", [
    (None, "inspection_busy_state_missing"),
    ({"busy": "false"}, "inspection_busy_state_missing"),
])
def test_unknown_busy_state_fails_closed(database, response, error):
    get = Inspections()
    get.responses["/api/v1/sessions/s1/checkpoints"] = response
    with pytest.raises(guard.CannotConfirm, match=error):
        guard.check_idle(database, get)


def test_job_count_mismatch_or_unknown_state_fails_closed(database):
    get = Inspections()
    for response in ({"jobs": [{"status": "running"}], "running": 0},
                     {"jobs": [{"status": ["running"]}], "running": 0}):
        get.responses["/api/v1/sessions/s1/jobs"] = response
        with pytest.raises(guard.CannotConfirm):
            guard.check_idle(database, get)


def test_new_session_or_activation_started_during_inspection_blocks(database):
    def mutate(path):
        if path == "/api/v1/code/projects":
            with sqlite3.connect(database) as con:
                con.execute("INSERT INTO sessions VALUES('started-now','owner','active')")
                con.execute("UPDATE project_agents SET activation_id=2,updated_at='t1'")
    report = guard.check_idle(database, Inspections(mutate=mutate))
    assert report["state_changed"] is True
    assert report["idle"] is False


def test_readonly_db_rejects_absent_database_without_creating_it(tmp_path):
    absent = tmp_path / "absent.db"
    with pytest.raises(guard.CannotConfirm, match="database_unavailable_or_schema_missing"):
        guard.check_idle(absent, Inspections())
    assert not absent.exists()


def test_cli_exit_contract_and_errors_never_echo_sensitive_payload(database, monkeypatch, capsys):
    get = Inspections()
    monkeypatch.setattr(guard, "InspectionClient", lambda *args: get)
    assert guard.main(["--db", str(database)]) == 0
    get.responses["/api/v1/sessions/s1/checkpoints"] = {"busy": True}
    assert guard.main(["--db", str(database)]) == 12
    def missing(_path):
        raise guard.CannotConfirm("inspection_http_status", http_status=401)
    monkeypatch.setattr(guard, "InspectionClient", lambda *args: missing)
    assert guard.main(["--db", str(database)]) == 13
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1]) == {"idle": False, "error": "inspection_http_status", "http_status": 401}
    assert "private" not in " ".join(lines)


def test_path_configuration_env_first_and_first_dotenv_only(tmp_path):
    cwd = tmp_path / "cwd"
    repo = tmp_path / "repo"
    cwd.mkdir()
    repo.mkdir()
    (cwd / ".env").write_text('unrelated_secret=do-not-read\nexport CODE_AGENT_HOME="configured home"\nCH_AGENT_HOME=legacy\n', encoding="utf-8")
    (repo / ".env").write_text("CODE_AGENT_HOME=second-env", encoding="utf-8")
    common = {"cwd": cwd, "repo": repo, "home": tmp_path}
    assert guard.default_db_path(environ={}, **common) == Path("configured home/code_agent.db")
    assert guard.default_db_path(environ={"CH_AGENT_HOME": "old-env"}, **common) == Path("old-env/code_agent.db")
    assert guard.default_db_path(environ={"CH_AGENT_HOME": "old", "CODE_AGENT_HOME": "new"}, **common) == Path("new/code_agent.db")
    explicit = tmp_path / "special.env"
    explicit.write_text("CH_AGENT_HOME='explicit path'", encoding="utf-8")
    assert guard.default_db_path(environ={"CODE_AGENT_ENV_FILE": str(explicit)}, **common) == Path("explicit path/code_agent.db")


def test_http_client_get_only_json_status_and_no_redirect_or_proxy(monkeypatch):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            seen.append((self.command, self.path))
            if self.path == "/ok":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"busy": false}')
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
            else:
                self.send_response(402)
                self.end_headers()
                self.wfile.write(b"private provider error")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    try:
        get = guard.InspectionClient(f"http://127.0.0.1:{server.server_port}")
        assert get("/ok") == {"busy": False}
        for path, status in (("/redirect", 302), ("/quota", 402)):
            with pytest.raises(guard.CannotConfirm) as error:
                get(path)
            assert error.value.http_status == status
            assert "private" not in str(error.value)
        assert seen == [("GET", "/ok"), ("GET", "/redirect"), ("GET", "/quota")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
