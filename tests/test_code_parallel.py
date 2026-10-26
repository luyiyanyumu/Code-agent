"""代码模式 P5：并行车道 / 写租约 / 并发执行 / 串行合并队列。

并行最容易被写坏的地方不是"跑不起来"，而是**悄悄丢改动**：
两条车道改同一个文件时，后合并的那次要么冲突、要么被"解决"掉一条车道的成果。
所以这里的测试盯住四条不变量：

1. **写租约**：同一个文件在任意时刻只有一条车道拥有；抢同一文件的车道必须等前置落地。
2. **真的并行**：互不依赖的车道同时执行（实测并发峰值 ≥ 2），而不是排成一队。
3. **落地串行且按计划顺序**：主干一次只接一条车道，顺序 = 计划顺序。
4. **不丢改动**：所有车道跑完后，每一条的改动都能在主干里找到（含被串行化的那条）。

用真实临时 git 仓库 + 真实工作树，注入一个"假 runner"代替模型（它会真的往
车道工作树里写文件，并记录并发峰值）——这样并行/隔离/合并都是真的。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from code_agent.codeintel.parallel import (
    BatchError,
    BatchService,
    compute_leases,
    extract_batch_plan,
    waves_of,
)
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.tasks import TaskService
from code_agent.codeintel.vcs import git_env
from code_agent.storage.db import BatchRepo, Database, TaskRepo


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**git_env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {proc.stdout}{proc.stderr}")
    return proc.stdout.strip()


class FakeProjects:
    def __init__(self, row: dict) -> None:
        self.row = row

    def get(self, pid: str) -> dict | None:
        return self.row if pid == self.row["id"] else None

    def find_by_root(self, root_key: str) -> dict | None:
        return self.row if self.row["root_key"] == root_key else None

    def set_scan(self, pid: str, summary_json: str) -> None:
        pass


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text('"""示例包。"""\n', encoding="utf-8")
    (root / "pkg" / "mathx.py").write_text(
        '"""数学工具。"""\n\n\ndef add(a: int, b: int) -> int:\n    """相加。"""\n    return a + b\n',
        encoding="utf-8",
    )
    (root / "pkg" / "text.py").write_text(
        '"""文本工具。"""\n\n\ndef shout(text: str) -> str:\n    return text.upper()\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


class RecordingRunner:
    """替代模型执行：往车道工作树里写内容，并记录并发峰值与每次调用。

    `writes[lane][rel]` 用 `"追加内容"` 或 `["旧串", "新串"]`（精确替换，模拟 edit_file）。
    """

    def __init__(self, writes: dict[str, dict[str, object]] | None = None, *, delay: float = 0.05) -> None:
        self.writes: dict[str, dict[str, object]] = writes or {}
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.calls: list[str] = []
        self.plan_text = ""

    async def __call__(self, project: dict, task: dict, prompt: str, permission_mode: str) -> dict:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if permission_mode == "read-only":
                self.calls.append("plan")
                return {"text": self.plan_text, "stop_reason": "ok", "turns": 1}
            lane = (task.get("lane") or "")
            self.calls.append(lane)
            wt = Path(task["worktree"])
            for rel, action in (self.writes.get(lane) or {}).items():
                target = wt / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                body = target.read_text(encoding="utf-8") if target.exists() else ""
                if isinstance(action, str):
                    body += action
                else:
                    old, new = action                       # type: ignore[misc]
                    assert old in body, f"{lane} 的替换锚点不在 {rel} 里"
                    body = body.replace(old, new, 1)         # type: ignore[arg-type]
                target.write_text(body, encoding="utf-8")
            return {"text": f"{lane} done", "stop_reason": "completed", "turns": 2}
        finally:
            self.active -= 1


@pytest.fixture
def env(repo: Path, tmp_path: Path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {
        "id": "p_p5", "name": "proj", "root_path": str(repo),
        "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin",
    }
    projects = FakeProjects(row)
    intel = CodeIntelService(projects)
    asyncio.run(intel.scan(row, full=True))
    db = Database(tmp_path / "batches.db")
    tasks = TaskRepo(db)
    batches = BatchRepo(db)
    task_service = TaskService(projects=projects, tasks=tasks, intel=intel, sessions=None)
    runner = RecordingRunner()
    service = BatchService(
        project_repo=projects, batches=batches, tasks=tasks,
        task_service=task_service, sessions=None, runner=runner,
    )
    return {
        "row": row, "intel": intel, "tasks": tasks, "batches": batches,
        "task_service": task_service, "service": service, "repo": repo, "runner": runner,
    }


def _plan(*lanes: dict) -> dict:
    return {"goal": "并行改造", "lanes": list(lanes), "notes": "测试计划"}


def _lane(lane_id: str, files: list[str], **extra) -> dict:
    return {"id": lane_id, "title": f"改 {', '.join(files)}", "files": files,
            "symbol_ids": [], "risk": "low", "verify": [], **extra}


# ---------------------------------------------------------------------------
# 兜底：没有工作树的车道绝不能把"当前目录"当成它的工作树
# （P5 冒烟抓到的真机 bug：排队中的车道审查出 3087 个"新增符号"——
#   那是把平台自己的仓库当成了它的改动）
# ---------------------------------------------------------------------------
def test_lane_without_worktree_never_falls_back_to_cwd(env):
    service, row = env["service"], env["row"]

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="排队车道不许碰平台目录",
            plan=_plan(_lane("L1", ["pkg/mathx.py"]),
                       _lane("L2", ["pkg/mathx.py"], verify=['python -c "print(1)"'])),
        )
        lanes = {lane["lane"]: lane for lane in env["tasks"].list_for_batch(batch["id"])}
        queued = env["tasks"].get(lanes["L2"]["id"])
        assert queued["worktree"] == ""

        review = await env["task_service"].review(row, queued)
        assert review["changed_files"] == [] and review["symbols"]["counts"] == {}
        assert await env["task_service"].changes(queued) == []
        assert await env["task_service"].diff_text(queued) == ""
        assert await env["task_service"].ensure_committed(queued) is False
        verify = await env["task_service"].verify(queued)
        assert verify["ok"] is False and "工作树不存在" in verify["note"]

    asyncio.run(flow())


def test_lane_runs_once_and_reuses_session_with_current_permission(env, monkeypatch):
    """Parallel lanes use ordinary execution; session reuse still updates grants."""
    monkeypatch.setenv("CODE_AGENT_CODE_LANE_GATE", "1")  # Retired configuration has no effect.
    calls: list[dict] = []

    class FakeSessions:
        def create_task_session(self, user_id, project_id, **kwargs):
            calls.append({"kind": "create", **kwargs})
            return {"id": "s_lane"}

        def set_permission_mode(self, session_id, user_id, mode):
            calls.append({"kind": "permission", "session_id": session_id, "mode": mode})

        def get_runtime(self, session_id, user_id):
            class _RT:
                async def submit(self, prompt):
                    calls.append({"kind": "submit", "prompt": prompt})
                    class _R:
                        text = "ok"
                        stop_reason = "end_turn"
                        turns = 1
                    return _R()
            return _RT()

    service, row = env["service"], env["row"]
    task_service = env["task_service"]
    task_service.sessions = FakeSessions()

    async def flow():
        task = await task_service.create(row, user_id="u_admin", requirement="普通执行")
        await task_service.run_agent(
            row, task, "干活", permission_mode="workspace-write"
        )
        assert "loop_mode" not in calls[0]
        assert calls[0]["permission_mode"] == "workspace-write"
        row2 = env["tasks"].get(task["id"]) or task
        await task_service.run_agent(
            row, {**row2, "session_id": "s_lane"}, "再干一点",
            permission_mode="full-access",
        )
        assert [call for call in calls if call["kind"] == "permission"] == [
            {"kind": "permission", "session_id": "s_lane", "mode": "full-access"},
        ]
        assert [call["prompt"] for call in calls if call["kind"] == "submit"] == ["干活", "再干一点"]
        assert len([call for call in calls if call["kind"] == "create"]) == 1

    asyncio.run(flow())


def test_parallel_lane_uses_normal_execution_without_mode_override(env):
    calls = []

    class Tasks:
        sessions = None

        async def run_agent(self, project, task, prompt, *, permission_mode):
            calls.append((project, task, prompt, permission_mode))
            return {"text": "done", "stop_reason": "end_turn", "turns": 1}

    tasks = Tasks()
    service = BatchService(
        project_repo=env["service"].projects, batches=env["batches"], tasks=env["tasks"],
        task_service=tasks,
    )
    outcome = asyncio.run(service._run_lane(env["row"], {"id": "lane"}, "执行"))
    assert outcome["stop_reason"] == "end_turn"
    assert len(calls) == 1 and calls[0][:3] == (env["row"], {"id": "lane"}, "执行")


def test_scan_rejects_empty_or_relative_root(env):
    """项目根为空/相对路径时必须报错：不能退化成"扫描服务进程的当前目录"。"""
    intel = env["intel"]
    for bad in ("", ".", "pkg"):
        with pytest.raises(ValueError):
            intel._scan_sync({"id": "p_probe", "root_path": bad}, False, "main")


# ---------------------------------------------------------------------------
# API 层：批次接口（走真实 app，不依赖模型）
# ---------------------------------------------------------------------------
@pytest.fixture
def client(agent_home):
    from fastapi.testclient import TestClient

    from code_agent.api.app import create_app

    with TestClient(create_app(provider_name="offline")) as c:
        yield c


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_batch_api_lifecycle(client, repo):
    """手工计划 → 详情/列表（含租约与队列）→ 手改拆分 → 串行合并 → 越权 404。"""
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=h)

    created = client.post(
        f"/api/v1/code/projects/{pid}/batches",
        json={
            "requirement": "两个模块各加一个函数",
            "plan_first": False,
            "plan": _plan(_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/text.py"]),
                          _lane("L3", ["pkg/mathx.py"])),
        },
        headers=h,
    ).json()
    assert created["status"] == "planned"
    batch_id = created["id"]
    assert [lane["lane"] for lane in created["lanes"]] == ["L1", "L2", "L3"]
    # L3 与 L1 抢同一个文件 → 进队列但不该被立刻建树
    assert created["conflicts"] == [
        {"path": "pkg/mathx.py", "lanes": ["L1", "L3"], "winner": "L1"}
    ]
    assert created["queue"] == ["L1", "L2", "L3"]
    assert created["waves"] == [["L1", "L2"], ["L3"]]
    lanes = {lane["lane"]: lane for lane in created["lanes"]}
    assert lanes["L3"]["status"] == "deferred" and lanes["L3"]["wait_for"] == ["L1"]

    listed = client.get(f"/api/v1/code/projects/{pid}/batches", headers=h).json()
    assert [b["id"] for b in listed] == [batch_id]
    assert listed[0]["progress"]["total"] == 3

    # 在两个独立车道的工作树里各写一处（模拟 AI 产出）
    for lane_id, rel, snippet in (("L1", "pkg/mathx.py", "\ndef sub(a, b):\n    return a - b\n"),
                                  ("L2", "pkg/text.py", "\ndef quiet(t):\n    return t.lower()\n")):
        wt = Path(lanes[lane_id]["worktree"]) / rel
        wt.write_text(wt.read_text(encoding="utf-8") + snippet, encoding="utf-8")

    review = client.get(f"/api/v1/code/projects/{pid}/batches/{batch_id}/review", headers=h).json()
    assert review["total"]["added"] >= 2
    assert {item["lane"] for item in review["lanes"]} >= {"L1", "L2"}

    merged = client.post(
        f"/api/v1/code/projects/{pid}/batches/{batch_id}/merge", json={}, headers=h
    ).json()
    assert merged["ok"] is True and merged["status"] == "merged"
    assert merged["merged"] == ["L1", "L2", "L3"]          # L3 在队列里被建树+执行+落地
    assert "def sub" in (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")
    assert "def quiet" in (repo / "pkg" / "text.py").read_text(encoding="utf-8")

    detail = client.get(f"/api/v1/code/projects/{pid}/batches/{batch_id}", headers=h).json()
    assert detail["status"] == "merged" and detail["progress"]["merged"] == 3

    # 改拆分：非法状态（已合并）不许再改
    bad = client.put(
        f"/api/v1/code/projects/{pid}/batches/{batch_id}/plan",
        json={"plan": _plan(_lane("L1", ["pkg/mathx.py"]))},
        headers=h,
    )
    assert bad.status_code in (409, 422)

    # 越权：别人的批次看不见
    from code_agent.api.security import hash_password

    client.app.state.users.create("eve", hash_password("e1"), role="user")
    eve = _login(client, "eve", "e1")
    assert client.get(f"/api/v1/code/projects/{pid}/batches", headers=_auth(eve)).status_code == 404
    assert (
        client.get(
            f"/api/v1/code/projects/{pid}/batches/{batch_id}", headers=_auth(eve)
        ).status_code
        == 404
    )


def test_batch_api_discard_and_validation(client, repo):
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]

    # 空需求：明确 422，而不是建出一个空批次
    bad = client.post(
        f"/api/v1/code/projects/{pid}/batches",
        json={"requirement": "  ", "plan_first": False},
        headers=h,
    )
    assert bad.status_code == 422

    created = client.post(
        f"/api/v1/code/projects/{pid}/batches",
        json={"requirement": "放弃整批", "plan_first": False,
              "plan": _plan(_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/text.py"]))},
        headers=h,
    ).json()
    batch_id = created["id"]
    worktrees = [Path(lane["worktree"]) for lane in created["lanes"] if lane["worktree"]]
    assert all(wt.is_dir() for wt in worktrees)

    discarded = client.post(
        f"/api/v1/code/projects/{pid}/batches/{batch_id}/discard", headers=h
    ).json()
    assert discarded["ok"] is True
    assert discarded["batch"]["status"] == "discarded"
    assert not any(wt.exists() for wt in worktrees)
    lane_rows = client.get(f"/api/v1/code/projects/{pid}/tasks", headers=h).json()
    assert {row["status"] for row in lane_rows} == {"discarded"}

# ---------------------------------------------------------------------------
# 计划归一化 / 写租约（纯函数）
# ---------------------------------------------------------------------------
def test_extract_batch_plan_normalizes_lanes():
    text = "看一下…\n```json\n" + json.dumps(
        {"goal": "拆两半", "lanes": [
            {"id": "L1", "title": "a", "files": ["pkg/mathx.py", "./pkg/mathx.py"], "risk": "low"},
            {"title": "b", "files": ["pkg/text.py"], "verify": ["pytest"]},
        ]}, ensure_ascii=False) + "\n```\n好了"
    plan = extract_batch_plan(text, max_lanes=4)
    assert plan and plan["goal"] == "拆两半"
    assert [lane["id"] for lane in plan["lanes"]] == ["L1", "L2"]   # 缺 id 自动补
    assert plan["lanes"][0]["files"] == ["pkg/mathx.py"]            # 去重 + 去掉 ./


def test_extract_batch_plan_accepts_single_task_shape():
    """模型按单任务 Plan 的形状输出时，就当成"一条车道的批次"。"""
    text = json.dumps({"goal": "g", "subtasks": [{"id": "s1", "files": ["a.py"]}]})
    plan = extract_batch_plan(text)
    assert plan and plan["lanes"][0]["id"] == "s1"


def test_extract_batch_plan_caps_lane_count_and_rejects_garbage():
    many = {"lanes": [{"id": f"L{i}", "files": [f"f{i}.py"]} for i in range(10)]}
    plan = extract_batch_plan(json.dumps(many), max_lanes=3)
    assert len(plan["lanes"]) == 3
    assert extract_batch_plan("这只是一段解释，没有 JSON") is None
    assert extract_batch_plan(json.dumps({"goal": "没有车道"})) is None


def test_leases_serialize_overlapping_files():
    lanes = [_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/text.py"]),
             _lane("L3", ["pkg/mathx.py", "README.md"])]
    lease = compute_leases(lanes)
    by_lane = {l["lane"]: l for l in lease["leases"]}
    assert by_lane["L1"]["files"] == ["pkg/mathx.py"]          # 先到者拥有
    assert by_lane["L3"]["files"] == ["README.md"]             # 被占住的不再发给它
    assert lease["wait_for"]["L3"] == ["L1"]                   # 必须等 L1
    assert lease["conflicts"] == [{"path": "pkg/mathx.py", "lanes": ["L1", "L3"], "winner": "L1"}]
    assert waves_of(lease["lane_ids"], lease["wait_for"]) == [["L1", "L2"], ["L3"]]


def test_leases_respect_explicit_after_and_cycles_degrade_to_serial():
    lanes = [_lane("L2", ["b.py"], after=["L1"]), _lane("L1", ["a.py"])]
    lease = compute_leases(lanes)
    assert lease["wait_for"]["L2"] == ["L1"]
    assert waves_of(lease["lane_ids"], lease["wait_for"]) == [["L1"], ["L2"]]

    # 成环：无法排波次 → 全部串行（宁可慢，也不能让两条车道同时写同一片区域）
    cyc = compute_leases([_lane("L1", ["a.py"], after=["L2"]), _lane("L2", ["b.py"], after=["L1"])])
    waves = waves_of(cyc["lane_ids"], cyc["wait_for"])
    assert waves == [["L1", "L2"]]


# ---------------------------------------------------------------------------
# 建批次：车道即任务，各自一棵工作树
# ---------------------------------------------------------------------------
def test_create_batch_materializes_lanes_with_own_worktrees(env):
    service, row = env["service"], env["row"]

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="两个文件各改一处",
            plan=_plan(_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/text.py"])),
        )
        assert batch["status"] == "planned"
        lanes = env["tasks"].list_for_batch(batch["id"])
        assert [lane["lane"] for lane in lanes] == ["L1", "L2"]
        branches = {lane["branch"] for lane in lanes}
        worktrees = {lane["worktree"] for lane in lanes}
        assert len(branches) == 2 and len(worktrees) == 2      # 一人一棵树一条分支
        for lane in lanes:
            assert Path(lane["worktree"]).is_dir()
        view = service.view(batch, lanes)
        assert view["queue"] == ["L1", "L2"] and view["conflicts"] == []
        assert view["progress"]["total"] == 2

    asyncio.run(flow())


def test_overlapping_lane_is_deferred_until_front_lands(env):
    """抢同一个文件的车道不能立刻建树：它必须等前置落地后从新 HEAD 出发。"""
    service, row = env["service"], env["row"]

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="同一文件两条车道",
            plan=_plan(_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/mathx.py"])),
        )
        lanes = {lane["lane"]: lane for lane in env["tasks"].list_for_batch(batch["id"])}
        assert lanes["L1"]["status"] == "planned"
        assert lanes["L2"]["status"] == "deferred" and lanes["L2"]["worktree"] == ""
        view = service.view(env["batches"].get(batch["id"]) or batch, list(lanes.values()))
        assert view["conflicts"] == [
            {"path": "pkg/mathx.py", "lanes": ["L1", "L2"], "winner": "L1"}
        ]
        assert view["waves"] == [["L1"], ["L2"]]
        assert [l["wait_for"] for l in view["leases"] if l["lane"] == "L2"] == [["L1"]]

        # 执行阶段只跑 L1；L2 明确报告为 deferred（而不是静默漏掉）
        run = await service.run(row, env["batches"].get(batch["id"]) or batch)
        assert [r["lane"] for r in run["lanes"] if not r.get("deferred")] == ["L1"]
        assert run["deferred"] == ["L2"]

    asyncio.run(flow())


# ---------------------------------------------------------------------------
# 并发执行
# ---------------------------------------------------------------------------
def test_independent_lanes_run_concurrently_and_stay_isolated(env):
    service, row, repo = env["service"], env["row"], env["repo"]
    env["runner"].writes = {
        "L1": {"pkg/mathx.py": "\n# L1 的改动\n"},
        "L2": {"pkg/text.py": "\n# L2 的改动\n"},
        "L3": {"README.md": "\nL3 的改动\n"},
    }

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="三个文件同时改",
            plan=_plan(_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/text.py"]),
                       _lane("L3", ["README.md"])),
        )
        run = await service.run(row, env["batches"].get(batch["id"]) or batch, max_workers=3)
        assert run["ok"] is True, run
        assert run["max_concurrent"] >= 2, f"没有真的并行：{run}"
        assert run["waves"] == [["L1", "L2", "L3"]]
        # 三条车道都跑过，且各自只写自己的文件
        assert sorted(env["runner"].calls) == ["L1", "L2", "L3"]
        lanes = {lane["lane"]: lane for lane in env["tasks"].list_for_batch(batch["id"])}
        assert "L1 的改动" in (Path(lanes["L1"]["worktree"]) / "pkg" / "mathx.py").read_text(encoding="utf-8")
        assert "L2 的改动" not in (Path(lanes["L1"]["worktree"]) / "pkg" / "text.py").read_text(encoding="utf-8")
        # 主干此时一个字节都没变（并行≠直接落地）
        assert "L1 的改动" not in (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")
        assert _git(repo, "status", "--porcelain") == ""

    asyncio.run(flow())


def test_worker_limit_serializes_beyond_capacity(env):
    service, row = env["service"], env["row"]
    env["runner"].writes = {"L1": {"a.txt": "1\n"}, "L2": {"b.txt": "2\n"}, "L3": {"c.txt": "3\n"}}

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="限流",
            plan=_plan(_lane("L1", ["a.txt"]), _lane("L2", ["b.txt"]), _lane("L3", ["c.txt"])),
        )
        run = await service.run(row, env["batches"].get(batch["id"]) or batch, max_workers=1)
        assert run["max_concurrent"] == 1 and run["ok"] is True

    asyncio.run(flow())


# ---------------------------------------------------------------------------
# 串行合并队列
# ---------------------------------------------------------------------------
def test_merge_queue_lands_every_lane_in_plan_order(env):
    service, row, repo = env["service"], env["row"], env["repo"]
    env["runner"].writes = {
        "L1": {"pkg/mathx.py": "\n# L1 的改动\n"},
        "L2": {"pkg/text.py": "\n# L2 的改动\n"},
        "L3": {"README.md": "\nL3 的改动\n"},
    }

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="串行落地",
            plan=_plan(_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/text.py"]),
                       _lane("L3", ["README.md"])),
        )
        await service.run(row, env["batches"].get(batch["id"]) or batch)
        result = await service.merge_queue(row, env["batches"].get(batch["id"]) or batch)
        assert result["ok"] is True and result["status"] == "merged"
        assert result["merged"] == ["L1", "L2", "L3"]      # 计划顺序 = 落地顺序
        assert [entry["queued"] for entry in result["log"]] == [True, True, True]
        # 三条车道的改动都在主干里 → 没有任何一条被丢掉
        assert "L1 的改动" in (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")
        assert "L2 的改动" in (repo / "pkg" / "text.py").read_text(encoding="utf-8")
        assert "L3 的改动" in (repo / "README.md").read_text(encoding="utf-8")
        # 合并之后工作树与分支都回收了
        for lane in env["tasks"].list_for_batch(batch["id"]):
            assert lane["status"] == "merged"
            assert not Path(lane["worktree"]).exists()
        assert _git(repo, "status", "--porcelain") == ""

    asyncio.run(flow())


def test_deferred_lane_is_rebuilt_from_new_head_so_nothing_is_lost(env):
    """同一个文件两条车道：第二条在队列里重做，两处改动最终都在主干里。"""
    service, row, repo = env["service"], env["row"], env["repo"]
    env["runner"].writes = {
        "L1": {"pkg/mathx.py": "\n# L1 追加\n"},
        "L2": {"pkg/mathx.py": "\n# L2 追加\n"},
    }

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="同一文件两条车道",
            plan=_plan(_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/mathx.py"])),
        )
        await service.run(row, env["batches"].get(batch["id"]) or batch)
        result = await service.merge_queue(row, env["batches"].get(batch["id"]) or batch)
        assert result["ok"] is True

    asyncio.run(flow())
    body = (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")
    assert "L1 追加" in body and "L2 追加" in body, body
    # 顺序上 L2 是在 L1 之后跑的（因为它要从 L1 落地后的 HEAD 出发）
    assert env["runner"].calls.index("L1") < env["runner"].calls.index("L2")
    assert _git(repo, "status", "--porcelain") == ""


def test_merge_queue_reports_conflict_and_restores_main(env):
    """真的撞车时：主干必须被恢复到合并前的状态，且如实报出是哪条车道。"""
    service, row, repo = env["service"], env["row"], env["repo"]
    env["runner"].writes = {
        "L1": {"pkg/mathx.py": "\n# L1\n"},
        # L2 改的正是主干接下来也要改的同一行 → 3 路合并必冲突
        "L2": {"pkg/text.py": ["return text.upper()", 'return text.upper() + "!"']},
    }

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="人工制造冲突",
            plan=_plan(_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/text.py"])),
        )
        await service.run(row, env["batches"].get(batch["id"]) or batch)
        lanes = {lane["lane"]: lane for lane in env["tasks"].list_for_batch(batch["id"])}
        # 在主干里改同一行（模拟别人先合了同区域的改动）
        (repo / "pkg" / "text.py").write_text(
            '"""文本工具。"""\n\n\ndef shout(text: str) -> str:\n    return "<b>" + text.upper() + "</b>"\n',
            encoding="utf-8",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "主干：同一行换个写法")
        head_before = _git(repo, "rev-parse", "HEAD")

        result = await service.merge_queue(
            row, env["batches"].get(batch["id"]) or batch, lanes=["L2"], stop_on_conflict=True
        )
        assert result["ok"] is False and result["merged"] == []
        assert result["log"][0]["error"]
        assert (env["tasks"].get(lanes["L2"]["id"]) or {})["status"] == "conflict"
        # 主干没有停在半合并状态
        assert _git(repo, "status", "--porcelain") == ""
        assert _git(repo, "rev-parse", "HEAD") == head_before
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    asyncio.run(flow())


def test_batch_discard_removes_all_lane_worktrees(env):
    service, row, repo = env["service"], env["row"], env["repo"]

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="放弃整批",
            plan=_plan(_lane("L1", ["pkg/mathx.py"]), _lane("L2", ["pkg/text.py"]),
                       _lane("L3", ["pkg/mathx.py"])),   # L3 是 deferred，没有工作树
        )
        lanes = env["tasks"].list_for_batch(batch["id"])
        result = await service.discard(row, env["batches"].get(batch["id"]) or batch)
        assert result["ok"] is True
        assert (env["batches"].get(batch["id"]) or {})["status"] == "discarded"
        for lane in lanes:
            assert (env["tasks"].get(lane["id"]) or {})["status"] == "discarded"
            if lane["worktree"]:
                assert not Path(lane["worktree"]).exists()
        assert "pkg/mathx.py" in _git(repo, "ls-files")

    asyncio.run(flow())
    assert _git(repo, "status", "--porcelain") == ""


def test_replace_plan_discards_removed_lane_and_adds_new_one(env):
    service, row = env["service"], env["row"]

    async def flow():
        batch = await service.create(
            row, user_id="u_admin", requirement="改拆分",
            plan=_plan(_lane("L1", ["pkg/mathx.py"])),
        )
        first = {lane["lane"]: lane for lane in env["tasks"].list_for_batch(batch["id"])}
        assert Path(first["L1"]["worktree"]).is_dir()

        await service.replace_plan(
            row, env["batches"].get(batch["id"]) or batch,
            _plan(_lane("L2", ["pkg/text.py"])),
        )
        lanes = {lane["lane"]: lane for lane in env["tasks"].list_for_batch(batch["id"])}
        assert lanes["L1"]["status"] == "discarded"          # 计划外的写者必须被回收
        assert lanes["L2"]["status"] == "planned"
        assert Path(lanes["L2"]["worktree"]).is_dir()
        assert not Path(lanes["L1"]["worktree"]).exists()

    asyncio.run(flow())


def test_batch_requires_requirement_and_plan(env):
    service, row = env["service"], env["row"]

    async def flow():
        with pytest.raises(BatchError):
            await service.create(row, user_id="u_admin", requirement="   ")
        batch = await service.create(row, user_id="u_admin", requirement="没给计划")
        with pytest.raises(BatchError):
            await service.run(row, batch)
        with pytest.raises(BatchError):
            await service.merge_queue(row, batch)

    asyncio.run(flow())


def test_plan_with_model_stores_lanes(env):
    service, row = env["service"], env["row"]
    env["runner"].plan_text = json.dumps({"goal": "模型拆分", "lanes": [
        {"id": "L1", "files": ["pkg/mathx.py"]}, {"id": "L2", "files": ["pkg/text.py"]}]})

    async def flow():
        batch = await service.create(row, user_id="u_admin", requirement="让模型拆")
        assert (env["batches"].get(batch["id"]) or {})["status"] == "planning"
        result = await service.plan_with_model(row, env["batches"].get(batch["id"]) or batch)
        assert result["ok"] is True
        view = service.view(env["batches"].get(batch["id"]) or batch,
                            env["tasks"].list_for_batch(batch["id"]))
        assert [lane["lane"] for lane in view["lanes"]] == ["L1", "L2"]
        assert view["queue"] == ["L1", "L2"]

        # 模型没给出可解析 JSON：状态落到 plan_failed，且不装作成功
        env["runner"].plan_text = "我建议先读代码……"
        failed = await service.plan_with_model(row, env["batches"].get(batch["id"]) or batch)
        assert failed["ok"] is False and failed["status"] == "plan_failed"

    asyncio.run(flow())
