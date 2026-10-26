"""Fail-closed restart guard. Only stdlib, read-only SQLite and inspection GETs.

Exit 0: every inspected executor is idle; 12: work/waiters or a concurrent
change was seen; 13: idleness could not be confirmed. Output contains counts
and fixed error codes only, never commands, prompts, results or credentials.
The launcher must recheck immediately before stopping its verified process.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


BUSY_STATES = frozenset({"queued", "running", "waiting", "planning", "merging"})
TEAM_STATES = frozenset({"idle", "queued", "running", "waiting", "completed", "failed", "cancelled", "retired"})
WORK_STATES = TEAM_STATES | frozenset({"planning", "planned", "plan_failed", "review", "merged", "discarded", "merging", "partial", "conflict"})
JOB_STATES = frozenset({"queued", "running", "done", "failed", "killed"})
IDLE = 0
BUSY = 12
UNKNOWN = 13


class CannotConfirm(Exception):
    def __init__(self, code: str, *, http_status: int | None = None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


def default_db_path(*, environ=None, cwd=None, home=None, repo=None) -> Path:
    """Resolve only path configuration; importing config would run migration.

    Real environment wins, including its legacy HOME alias. Read only the first
    existing .env (the same search order as the server), without logging it or
    loading unrelated secrets into the child process environment.
    """
    env = os.environ if environ is None else environ
    if "CODE_AGENT_HOME" in env or "CH_AGENT_HOME" in env:
        return Path(env.get("CODE_AGENT_HOME", env.get("CH_AGENT_HOME", ""))) / "code_agent.db"
    root = Path(repo) if repo is not None else Path(__file__).resolve().parents[1]
    work = Path(cwd) if cwd is not None else Path.cwd()
    candidates = ([Path(env["CODE_AGENT_ENV_FILE"])] if env.get("CODE_AGENT_ENV_FILE") else [])
    candidates.extend((work / ".env", root / ".env"))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        values = {}
        try:
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, sep, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if not sep or key not in ("CODE_AGENT_HOME", "CH_AGENT_HOME") or key in values:
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
        except (OSError, UnicodeError) as exc:
            raise CannotConfirm("path_config_unreadable") from exc
        if "CODE_AGENT_HOME" in values or "CH_AGENT_HOME" in values:
            return Path(values.get("CODE_AGENT_HOME", values.get("CH_AGENT_HOME", ""))) / "code_agent.db"
        break
    return (Path(home) if home is not None else Path.home()) / ".code-agent" / "code_agent.db"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class InspectionClient:
    def __init__(self, base_url: str, timeout: float = 10):
        parts = urlsplit(base_url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or parts.path not in ("", "/")):
            raise CannotConfirm("invalid_base_url")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def __call__(self, path: str) -> Any:
        try:
            # Explicit GET, no inherited proxies, cookies, credentials or redirects.
            opener = build_opener(ProxyHandler({}), NoRedirect())
            request = Request(self.base_url + path, headers={"Accept": "application/json"}, method="GET")
            with opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise CannotConfirm("inspection_http_status", http_status=response.status)
                payload = response.read(8 * 1024 * 1024 + 1)
                if len(payload) > 8 * 1024 * 1024:
                    raise CannotConfirm("inspection_response_too_large")
                return json.loads(payload)
        except HTTPError as exc:
            raise CannotConfirm("inspection_http_status", http_status=exc.code) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise CannotConfirm("inspection_unreachable") from exc
        except (ValueError, UnicodeError) as exc:
            raise CannotConfirm("inspection_invalid_json") from exc


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise CannotConfirm("inspection_missing_identifier")
    return value


def _list(value: Any) -> list:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise CannotConfirm("inspection_invalid_list")
    return value


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise CannotConfirm("inspection_invalid_count")
    return value


def _pending(value: Any) -> int:
    if value is None or value == {}:
        return 0
    if isinstance(value, dict):
        if not isinstance(value.get("approval_id"), str) or not value["approval_id"]:
            raise CannotConfirm("inspection_invalid_approval")
        return 1
    if isinstance(value, list):
        return sum(_pending(item) for item in value)
    raise CannotConfirm("inspection_invalid_approval")


def _snapshot(con: sqlite3.Connection) -> dict:
    # Read every active session; GET /sessions is limited to the most recent 50.
    sessions = dict(con.execute("SELECT id,user_id FROM sessions WHERE status='active'"))
    statuses = dict(con.execute("SELECT id,status FROM sessions"))
    agents = tuple(con.execute(
        "SELECT session_id,project_id,tree_id,user_id,state,activation_id,updated_at "
        "FROM project_agents WHERE state!='retired' ORDER BY session_id"))
    if any(row[4] not in TEAM_STATES for row in agents):
        raise CannotConfirm("database_unknown_team_state")
    projects = dict(con.execute("SELECT id,user_id FROM projects"))
    approvals = tuple(con.execute("SELECT id,session_id,user_id FROM approvals WHERE status='pending' ORDER BY id"))
    # Old isolated planners/runners and batch merging can outlive a model turn.
    tasks = tuple(con.execute("SELECT id,user_id,status FROM tasks WHERE status IN ('planning','queued','running','waiting','merging') ORDER BY id"))
    batches = tuple(con.execute("SELECT id,user_id,status FROM task_batches WHERE status IN ('planning','queued','running','waiting','merging') ORDER BY id"))
    return {"sessions": sessions, "session_statuses": statuses, "agents": agents, "projects": projects,
            "approvals": approvals, "tasks": tasks, "batches": batches}


def _session_state(get: Callable[[str], Any], sid: str) -> tuple[int, int, int, int]:
    prefix = "/api/v1/sessions/" + quote(sid, safe="")
    checkpoint = get(prefix + "/checkpoints")
    if not isinstance(checkpoint, dict) or type(checkpoint.get("busy")) is not bool:
        raise CannotConfirm("inspection_busy_state_missing")
    jobs = get(prefix + "/jobs")
    if not isinstance(jobs, dict):
        raise CannotConfirm("inspection_jobs_missing")
    rows = _list(jobs.get("jobs"))
    if any(not isinstance(row.get("status"), str) or row["status"] not in JOB_STATES for row in rows):
        raise CannotConfirm("inspection_unknown_job_state")
    running = sum(row["status"] == "running" for row in rows)
    if _count(jobs.get("running")) != running:
        raise CannotConfirm("inspection_job_count_mismatch")
    active_jobs = sum(row["status"] in ("queued", "running") for row in rows)
    questions = get(prefix + "/questions/pending")
    if not isinstance(questions, dict):
        raise CannotConfirm("inspection_questions_missing")
    pending_questions = len(_list(questions.get("questions")))
    pending_approvals = _pending(get(prefix + "/approvals/pending"))
    return int(checkpoint["busy"]), active_jobs, pending_questions, pending_approvals


def _work_state(value: Any, pid: str) -> tuple[int, int]:
    if (not isinstance(value, dict) or value.get("project_id") != pid
            or not isinstance(value.get("counts"), dict)
            or not isinstance(value.get("capabilities"), dict)
            or value["capabilities"].get("team") is not True):
        raise CannotConfirm("inspection_work_items_missing")
    active = _count(value["counts"].get("active"))
    # Only inspect fixed fields; do not traverse arbitrary result/prompt payloads.
    pending = 0
    lead = value.get("lead")
    if lead is not None and (not isinstance(lead, dict) or not isinstance(lead.get("state"), str)
                             or lead["state"] not in TEAM_STATES):
        raise CannotConfirm("inspection_team_state_missing")
    agents = [lead] if lead else []
    stack = list(_list(value.get("items")))
    while stack:
        item = stack.pop()
        if not isinstance(item.get("state"), str) or item["state"] not in WORK_STATES:
            raise CannotConfirm("inspection_work_state_missing")
        if item.get("kind") != "agent" and type(item.get("active")) is not bool:
            raise CannotConfirm("inspection_work_active_missing")
        active += int(item.get("active", False) or item["state"] in BUSY_STATES)
        pending += len(_list(item.get("pending_questions", [])))
        pending += _pending(item.get("pending_approval"))
        agents.extend(_list(item.get("agents", [])))
        stack.extend(_list(item.get("children", [])))
    for agent in agents:
        if not isinstance(agent.get("state"), str) or agent["state"] not in TEAM_STATES:
            raise CannotConfirm("inspection_team_state_missing")
        active += int(agent["state"] in BUSY_STATES)
        pending += len(_list(agent.get("pending_questions", [])))
        pending += _pending(agent.get("pending_approval"))
    # Counts can overlap (task + descendants); only zero/nonzero is significant.
    return active, pending


def check_idle(db_path: Path, get: Callable[[str], Any], *, workers: int = 6) -> dict:
    try:
        con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True,
                              timeout=5, isolation_level=None)
        con.execute("PRAGMA query_only=ON")
        try:
            version = con.execute("PRAGMA data_version").fetchone()[0]
            before = _snapshot(con)
            user = get("/api/v1/auth/me")
            if not isinstance(user, dict):
                raise CannotConfirm("inspection_owner_missing")
            owner = _identifier(user.get("id"))
            if any(uid != owner for uid in before["sessions"].values()):
                raise CannotConfirm("other_owner_active_sessions")
            for sid, pid, _tree, uid, state, _activation, _updated in before["agents"]:
                if sid not in before["sessions"]:
                    # close/archive shuts down its runtime before storing status.
                    # It preserves idle owners as well as completed history.
                    # Missing rows and busy owners remain unsafe.
                    if (state not in ("idle", "completed", "failed", "cancelled")
                            or before["session_statuses"].get(sid) not in ("closed", "archived")):
                        raise CannotConfirm("team_session_not_active")
                elif before["sessions"][sid] != uid or before["projects"].get(pid) != uid:
                    raise CannotConfirm("team_owner_mismatch")
            listed = _list(get("/api/v1/sessions"))
            session_ids = set(before["sessions"])
            for row in listed:
                if row.get("status") == "active":
                    session_ids.add(_identifier(row.get("id")))
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                states = list(pool.map(lambda sid: _session_state(get, sid), sorted(session_ids)))
            projects = _list(get("/api/v1/code/projects"))
            project_ids = {_identifier(row.get("id")) for row in projects}
            if project_ids != {pid for pid, uid in before["projects"].items() if uid == owner}:
                raise CannotConfirm("inspection_project_list_mismatch")
            work_states = [_work_state(get("/api/v1/code/projects/" + quote(pid, safe="") + "/work-items?limit=100"), pid)
                           for pid in sorted(project_ids)]
            after = _snapshot(con)
            changed = after != before or con.execute("PRAGMA data_version").fetchone()[0] != version
            report = {
                "idle": False, "checked_sessions": len(session_ids), "checked_projects": len(project_ids),
                "checked_team_trees": len({(row[1], row[2] or "main") for row in before["agents"]}),
                "busy_sessions": sum(row[0] for row in states), "active_jobs": sum(row[1] for row in states),
                "pending_questions": sum(row[2] for row in states),
                "pending_approvals": max(sum(row[3] for row in states), len(after["approvals"])),
                "db_active_agents": sum(row[4] in BUSY_STATES for row in after["agents"]),
                "db_active_tasks": len(after["tasks"]), "db_active_batches": len(after["batches"]),
                "work_active": sum(row[0] for row in work_states), "work_pending": sum(row[1] for row in work_states),
                "state_changed": changed,
            }
            report["idle"] = not any(value for key, value in report.items()
                                     if key not in ("idle", "checked_sessions", "checked_projects", "checked_team_trees"))
            return report
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise CannotConfirm("database_unavailable_or_schema_missing") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        report = check_idle(args.db if args.db is not None else default_db_path(), InspectionClient(args.base_url))
        print(json.dumps(report, ensure_ascii=True))
        return IDLE if report["idle"] else BUSY
    except CannotConfirm as exc:
        report = {"idle": False, "error": exc.code}
        if exc.http_status is not None:
            report["http_status"] = exc.http_status
        print(json.dumps(report))
        return UNKNOWN
    except (OSError, ValueError) as exc:
        # Exception strings can contain paths or response content; never print them.
        print(json.dumps({"idle": False, "error": "inspection_unavailable"}))
        return UNKNOWN


if __name__ == "__main__":
    raise SystemExit(main())
