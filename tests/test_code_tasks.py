"""代码模式 P4：隔离工作树 / Plan 意图层 / 符号级差异 / 审批合并 / git 面板。

本层是"AI 改代码"能不能放心用的分水岭，测试盯住四条不变量：

1. **主干不受影响**：任务执行期间（含 AI 写入）主干工作区的内容与 HEAD 都不变；
2. **先计划后动手**：没有可解析的 Plan 就不能进入执行（plan_failed 要能被用户手改后批准）；
3. **越界可见**：改了计划未声明的文件，审查结果必须列出来（而不是静默放行）；
4. **合并才落地**：只有 merge 才会改动主干，且合并后主干索引立刻跟上。

用真实的临时 git 仓库（不是 mock），因为工作树/分支/合并这些语义只有真 git 才作数。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.tasks import TaskError, TaskService, extract_plan, slugify
from code_agent.codeintel.vcs import check_command, git_env
from code_agent.storage.db import Database, TaskRepo


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
    """一个真实的小仓库：两个模块 + 一个测试文件。"""
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
    (root / "tests").mkdir()
    (root / "tests" / "test_mathx.py").write_text(
        "from pkg.mathx import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def env(repo: Path, tmp_path: Path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {
        "id": "p_p4", "name": "proj", "root_path": str(repo),
        "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin",
    }
    projects = FakeProjects(row)
    intel = CodeIntelService(projects)
    asyncio.run(intel.scan(row, full=True))
    db = Database(tmp_path / "tasks.db")
    tasks = TaskRepo(db)
    service = TaskService(projects=projects, tasks=tasks, intel=intel, sessions=None)
    return {"row": row, "intel": intel, "tasks": tasks, "service": service, "repo": repo}


def _plan_payload() -> dict:
    return {
        "goal": "把 add 改成支持可变参数",
        "subtasks": [
            {
                "id": "s1",
                "title": "改造 add",
                "files": ["pkg/mathx.py"],
                "symbol_ids": ["sym:pkg/mathx.py#add"],
                "risk": "medium",
                "verify": ["python -m pytest tests/test_mathx.py"],
                "notes": "保持向后兼容",
            }
        ],
        "notes": "只动 mathx.py",
    }


# ---------------------------------------------------------------------------
# Plan 解析
# ---------------------------------------------------------------------------
def test_extract_plan_from_fenced_json():
    text = "先看一下…\n```json\n" + json.dumps(_plan_payload(), ensure_ascii=False) + "\n```\n完成"
    plan = extract_plan(text)
    assert plan and plan["goal"].startswith("把 add")
    assert plan["files"] == ["pkg/mathx.py"]
    assert plan["subtasks"][0]["risk"] == "medium"


def test_extract_plan_rejects_garbage():
    assert extract_plan("我是模型，我拒绝输出 JSON") is None
    assert extract_plan('{"goal": 1') is None          # 半截 JSON
    assert extract_plan("```json\n[1,2,3]\n```") is None  # 顶层不是对象


def test_slugify_is_branch_safe():
    assert slugify("Add support for variadic args!") == "add-support-for-variadic"
    assert slugify("fix bug") == "fix-bug"
    assert slugify("") == "task"
    assert slugify("中文需求") == "task"  # 纯中文压不出 ASCII：回落到 task


# ---------------------------------------------------------------------------
# 工作树生命周期
# ---------------------------------------------------------------------------
def test_create_task_makes_isolated_worktree(env):
    row, service, repo = env["row"], env["service"], env["repo"]

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="把 add 改成支持可变参数")
        assert task["status"] == "planning"
        wt = Path(task["worktree"])
        assert wt.is_dir() and (wt / "pkg" / "mathx.py").is_file()
        # 工作树自己在独立分支上；主干仍在原分支且干净
        assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == task["branch"]
        assert task["branch"].startswith("ai/")
        assert _git(repo, "status", "--porcelain") == ""
        # 任务树索引与主干索引是两棵树
        assert service.tree_id(task["id"]) in (
            f"task:{task['id']}",
        )
        assert env["intel"].index_path(row["id"], service.tree_id(task["id"])).is_file()
        return task

    task = asyncio.run(flow())
    assert task["base_branch"]  # 记录了基准分支


def test_create_task_requires_git_or_explicit_init(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    row = {"id": "p_plain", "name": "plain", "root_path": str(plain), "root_key": str(plain).lower(),
           "user_id": "u_admin"}
    projects = FakeProjects(row)
    intel = CodeIntelService(projects)
    db = Database(tmp_path / "t2.db")
    service = TaskService(projects=projects, tasks=TaskRepo(db), intel=intel, sessions=None)

    async def flow():
        with pytest.raises(TaskError) as exc:
            await service.create(row, user_id="u_admin", requirement="改点东西")
        assert "不是 git 仓库" in str(exc.value)
        # 显式同意初始化后：自动 git init + 基线提交，然后照常建工作树
        task = await service.create(
            row, user_id="u_admin", requirement="改点东西", git_init=True
        )
        assert Path(task["worktree"]).is_dir()
        assert (plain / ".git").is_dir()
        assert _git(plain, "status", "--porcelain") == ""

    asyncio.run(flow())


def test_execution_never_touches_main_tree(env):
    """核心不变量：AI 在工作树里写，主干文件与 HEAD 都不变。"""
    row, service, repo = env["row"], env["service"], env["repo"]
    before = (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")
    head_before = _git(repo, "rev-parse", "HEAD")

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="加个函数")
        service._write_plan(task, _plan_payload())
        task = env["tasks"].get(task["id"])
        # 模拟 AI 在工作树里写入（真实执行路径由 run_agent 完成，这里直接写文件）
        wt = Path(task["worktree"])
        (wt / "pkg" / "mathx.py").write_text(before + "\n\ndef mul(a, b):\n    return a * b\n", encoding="utf-8")
        review = await service.review(row, task)
        assert "pkg/mathx.py" in review["changed_files"]
        assert review["plan_scope_ok"] is True
        assert review["symbols"]["counts"]["added"] >= 1
        return task

    task = asyncio.run(flow())
    assert (repo / "pkg" / "mathx.py").read_text(encoding="utf-8") == before
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_review_flags_changes_outside_plan(env):
    row, service = env["row"], env["service"]

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="只改 mathx")
        service._write_plan(task, _plan_payload())
        task = env["tasks"].get(task["id"])
        wt = Path(task["worktree"])
        (wt / "pkg" / "mathx.py").write_text(
            (wt / "pkg" / "mathx.py").read_text(encoding="utf-8") + "\n# touch\n", encoding="utf-8"
        )
        (wt / "pkg" / "text.py").write_text('"""改了计划外的文件。"""\n', encoding="utf-8")
        review = await service.review(row, task)
        assert review["plan_scope_ok"] is False
        assert review["outside_plan"] == ["pkg/text.py"]

    asyncio.run(flow())


def test_symbol_diff_reports_added_modified_removed(env):
    row, service = env["row"], env["service"]

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="重组")
        task = env["tasks"].get(task["id"])
        wt = Path(task["worktree"])
        text = (wt / "pkg" / "mathx.py").read_text(encoding="utf-8")
        (wt / "pkg" / "mathx.py").write_text(
            text.replace("return a + b", "return a + b + 0")
            + "\n\ndef sub(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        # 删掉一个符号
        (wt / "pkg" / "text.py").write_text('"""文本工具（清空）。"""\n', encoding="utf-8")
        await service.intel.scan({**row, "root_path": str(wt)}, tree_id=service.tree_id(task["id"]))
        diff = service.symbol_diff(row, task)
        assert diff["counts"]["added"] >= 1
        assert diff["counts"]["modified"] >= 1
        assert diff["counts"]["removed"] >= 1
        assert "pkg/text.py" in diff["files"]
        assert all(item["kind"] for item in diff["modified"])

    asyncio.run(flow())


def test_merge_applies_to_main_and_cleans_up(env):
    row, service, repo = env["row"], env["service"], env["repo"]

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="整体合并")
        service._write_plan(task, _plan_payload())
        task = env["tasks"].get(task["id"])
        wt = Path(task["worktree"])
        (wt / "pkg" / "mathx.py").write_text(
            (wt / "pkg" / "mathx.py").read_text(encoding="utf-8") + "\n\ndef mul(a, b):\n    return a * b\n",
            encoding="utf-8",
        )
        await service.review(row, task)
        result = await service.merge(row, env["tasks"].get(task["id"]))
        assert result["ok"] is True
        merged = env["tasks"].get(task["id"])
        assert merged["status"] == "merged" and merged["merged_at"]
        # 主干拿到了改动，且合并提交在 HEAD
        assert "def mul" in (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")
        assert "merge:" in _git(repo, "log", "-1", "--pretty=%s")
        # 工作树与分支都已清理
        assert not Path(task["worktree"]).exists()
        assert task["branch"] not in _git(repo, "branch", "--format=%(refname:short)")
        # 合并后主干索引立刻跟上（新增符号可见）
        rows = {
            r["name"]
            for r in env["intel"].index(row["id"], "main").all_symbols(path="pkg/mathx.py")
        }
        assert "mul" in rows

    asyncio.run(flow())


def test_partial_merge_only_applies_selected_file(env):
    row, service, repo = env["row"], env["service"], env["repo"]

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="部分应用")
        task = env["tasks"].get(task["id"])
        wt = Path(task["worktree"])
        (wt / "pkg" / "mathx.py").write_text(
            (wt / "pkg" / "mathx.py").read_text(encoding="utf-8") + "\n# 只应用这个文件\n",
            encoding="utf-8",
        )
        (wt / "pkg" / "text.py").write_text('"""这个不该被应用。"""\n', encoding="utf-8")
        await service.review(row, task)

        with pytest.raises(TaskError):
            await service.merge(row, task, files=["pkg/nope.py"])

        result = await service.merge(row, task, files=["pkg/mathx.py"])
        assert result["files"] == ["pkg/mathx.py"]
        assert "只应用这个文件" in (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")
        # 未选中的文件保持主干原样（不自动提交，停在暂存区等用户决定）
        assert (repo / "pkg" / "text.py").read_text(encoding="utf-8").startswith('"""文本工具。')

    asyncio.run(flow())


def test_discard_removes_worktree_without_touching_main(env):
    row, service, repo = env["row"], env["service"], env["repo"]
    before = (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="放弃掉")
        wt = Path(task["worktree"])
        (wt / "pkg" / "mathx.py").write_text("全部推翻\n", encoding="utf-8")
        result = await service.discard(row, task)
        assert result["ok"] is True
        assert not wt.exists()
        assert (env["tasks"].get(task["id"]) or {})["status"] == "discarded"

    asyncio.run(flow())
    assert (repo / "pkg" / "mathx.py").read_text(encoding="utf-8") == before


def test_review_after_merge_returns_snapshot_not_error(env):
    """任务已合并（工作树已清）后再查审查：要回放台账里的快照，而不是报 422。

    前端面板在轮询审查，重算需要工作树；路径没了就会把 ProjectPathError 抛成
    422，用户看到的是刷屏报错，而此刻"当初审的是什么"才是有效信息。
    """
    row, service = env["row"], env["service"]

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="合并后还看审查")
        service._write_plan(task, _plan_payload())
        task = env["tasks"].get(task["id"])
        wt = Path(task["worktree"])
        (wt / "pkg" / "mathx.py").write_text(
            (wt / "pkg" / "mathx.py").read_text(encoding="utf-8") + "\n\ndef div(a, b):\n    return a / b\n",
            encoding="utf-8",
        )
        first = await service.review(row, env["tasks"].get(task["id"]))
        assert "pkg/mathx.py" in first["changed_files"]

        await service.merge(row, env["tasks"].get(task["id"]))
        merged = env["tasks"].get(task["id"])
        assert merged["status"] == "merged"
        assert not Path(merged["worktree"]).exists()

        again = await service.review(row, merged)
        assert again["changed_files"] == first["changed_files"]
        assert again["symbols"]["counts"] == first["symbols"]["counts"]

    asyncio.run(flow())


def test_verify_runs_plan_commands_in_worktree(env):
    row, service = env["row"], env["service"]

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="跑验证")
        plan = _plan_payload()
        plan["subtasks"][0]["verify"] = [f'"{sys.executable}" -c "print(123)"']
        service._write_plan(task, plan)
        task = env["tasks"].get(task["id"])
        result = await service.verify(task)
        assert result["ok"] is True
        assert result["results"][0]["ok"] is True
        assert "123" in result["results"][0]["output"]
        # 失败命令：停止后续（避免一条命令挂了还继续跑）
        plan["subtasks"][0]["verify"] = [
            f'"{sys.executable}" -c "import sys; sys.exit(2)"',
            f'"{sys.executable}" -c "print(999)"',
        ]
        service._write_plan(task, plan)
        failed = await service.verify(env["tasks"].get(task["id"]))
        assert failed["ok"] is False
        assert len(failed["results"]) == 1

    asyncio.run(flow())


# ---------------------------------------------------------------------------
# git 安全边界
# ---------------------------------------------------------------------------
def test_dangerous_git_commands_are_denied():
    assert check_command(["push", "--force", "origin", "main"])
    assert check_command(["reset", "--hard", "HEAD~1"])
    assert check_command(["clean", "-fdx"])
    assert check_command(["remote", "set-url", "origin", "http://evil"])
    assert check_command(["branch", "-D", "main"])
    assert check_command(["rebase", "main"])
    assert check_command(["push", "origin", "main"]) is None
    assert check_command(["pull", "--ff-only"]) is None


def test_git_env_disables_interactive_prompts():
    env = git_env()
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    with_key = git_env("/keys/deploy_key")
    assert "IdentitiesOnly=yes" in with_key["GIT_SSH_COMMAND"]


# ---------------------------------------------------------------------------
# API：任务与 git 面板
# ---------------------------------------------------------------------------
@pytest.fixture
def client(agent_home):
    with TestClient(create_app(provider_name="offline")) as c:
        yield c


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_task_api_lifecycle_with_manual_plan(client, repo):
    """不依赖模型：手工给计划 → 批准（离线 provider 不会真改）→ 审查 → 合并/放弃。"""
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=h)

    created = client.post(
        f"/api/v1/code/projects/{pid}/tasks",
        json={"requirement": "把 add 支持可变参数", "plan": _plan_payload(), "plan_first": False},
        headers=h,
    ).json()
    assert created["status"] == "planned"
    assert created["plan_goal"].startswith("把 add")
    assert created["plan_files"] == ["pkg/mathx.py"]
    task_id = created["id"]

    tasks = client.get(f"/api/v1/code/projects/{pid}/tasks", headers=h).json()
    assert [t["id"] for t in tasks] == [task_id]

    # 手改计划
    edited = client.put(
        f"/api/v1/code/projects/{pid}/tasks/{task_id}/plan",
        json={"plan": {**_plan_payload(), "goal": "改成可变参数（人工收敛范围）"}},
        headers=h,
    ).json()
    assert edited["plan_goal"].endswith("人工收敛范围）")

    # 在隔离工作树里造一处改动，再走审查
    detail = client.get(f"/api/v1/code/projects/{pid}/tasks/{task_id}", headers=h).json()
    wt = Path(detail["worktree"])
    (wt / "pkg" / "mathx.py").write_text(
        (wt / "pkg" / "mathx.py").read_text(encoding="utf-8") + "\n# 任务改动\n", encoding="utf-8"
    )
    review = client.get(f"/api/v1/code/projects/{pid}/tasks/{task_id}/review", headers=h).json()
    assert review["plan_scope_ok"] is True
    assert "pkg/mathx.py" in review["changed_files"]

    diffs = client.get(
        f"/api/v1/code/projects/{pid}/tasks/{task_id}/diff", headers=h
    ).json()
    assert "任务改动" in diffs["diff"]

    # 单文件差异（含改前/改后全文，供 Monaco diff）
    one = client.get(
        f"/api/v1/code/projects/{pid}/tasks/{task_id}/diff",
        params={"path": "pkg/mathx.py"},
        headers=h,
    ).json()
    assert "def add" in one["after"] and one["before"] and one["uri"].startswith("file://")

    merged = client.post(
        f"/api/v1/code/projects/{pid}/tasks/{task_id}/merge", json={"files": ["pkg/mathx.py"]}, headers=h
    ).json()
    assert merged["ok"] is True and merged["files"] == ["pkg/mathx.py"]
    assert "任务改动" in (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")

    # 越权
    client.app.state.users.create(
        "eve", __import__("code_agent.api.security", fromlist=["hash_password"]).hash_password("e1"), role="user"
    )
    eve = _login(client, "eve", "e1")
    assert client.get(f"/api/v1/code/projects/{pid}/tasks", headers=_auth(eve)).status_code == 404
    assert (
        client.post(f"/api/v1/code/projects/{pid}/tasks/{task_id}/discard", headers=_auth(eve)).status_code
        == 404
    )


def test_task_session_workspace_survives_runtime_rebuild(client, repo):
    """安全要点：任务会话的工作区必须落库——重启/重建运行时后仍指向工作树。

    否则运行时一旦被重建（进程重启、按需重建），工作区会退回"项目根"，
    AI 就会直接写主干，"隔离工作树"形同虚设。
    """
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=h)
    created = client.post(
        f"/api/v1/code/projects/{pid}/tasks",
        json={"requirement": "隔离性测试", "plan": _plan_payload(), "plan_first": False},
        headers=h,
    ).json()
    approved = client.post(
        f"/api/v1/code/projects/{pid}/tasks/{created['id']}/approve", headers=h
    )
    assert approved.status_code == 200, approved.text
    task = approved.json()
    session_id = task["session_id"]
    assert session_id, "任务执行后应绑定会话"

    manager = client.app.state.sessions
    user_id = client.get("/api/v1/auth/me", headers=h).json()["id"]
    runtime = manager.get_runtime(session_id, user_id)
    assert Path(runtime.workspace) == Path(task["worktree"])

    # 模拟进程重启：清掉内存里的运行时，再按 DB 元数据重建
    manager._runtimes.pop(session_id, None)
    rebuilt = manager.get_runtime(session_id, user_id)
    assert Path(rebuilt.workspace) == Path(task["worktree"])
    assert Path(rebuilt.workspace) != repo


def test_task_api_marks_plan_failure_for_manual_edit(client, repo):
    """离线 provider 产不出 Plan JSON：状态应是 plan_failed 且给出原文，而不是崩掉。"""
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=h)

    created = client.post(
        f"/api/v1/code/projects/{pid}/tasks",
        json={"requirement": "随便改点东西", "plan_first": True},
        headers=h,
    ).json()
    assert created["status"] == "plan_failed"
    assert "解析出计划" in created["error"]
    # 用户手改计划后可以继续
    fixed = client.put(
        f"/api/v1/code/projects/{pid}/tasks/{created['id']}/plan",
        json={"plan": _plan_payload()},
        headers=h,
    ).json()
    assert fixed["status"] == "planned"


def test_git_panel_endpoints(client, repo):
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]

    status = client.get(f"/api/v1/code/projects/{pid}/git", headers=h).json()
    assert status["is_repo"] is True and status["changes"] == []
    branches = client.get(f"/api/v1/code/projects/{pid}/git/branches", headers=h).json()["branches"]
    assert any(b["name"] for b in branches)

    # 造一个改动 → 提交 → 状态回到干净，且索引跟着更新
    (repo / "pkg" / "newmod.py").write_text("def brand_new():\n    return 1\n", encoding="utf-8")
    dirty = client.get(f"/api/v1/code/projects/{pid}/git", headers=h).json()
    assert dirty["dirty"] is True
    assert any(c["path"] == "pkg/newmod.py" for c in dirty["changes"])

    empty = client.post(f"/api/v1/code/projects/{pid}/git/commit", json={"message": "  "}, headers=h)
    assert empty.status_code == 422

    committed = client.post(
        f"/api/v1/code/projects/{pid}/git/commit",
        json={"message": "feat: 新增 newmod", "files": ["pkg/newmod.py"]},
        headers=h,
    ).json()
    assert committed["ok"] is True and committed["dirty"] is False

    # 受控动作白名单：未知动作 422；危险命令 403（平台不提供按钮）
    bad = client.post(
        f"/api/v1/code/projects/{pid}/git/action", json={"action": "rebase"}, headers=h
    )
    assert bad.status_code == 422
    denied = client.post(
        f"/api/v1/code/projects/{pid}/git/action", json={"action": "fetch"}, headers=h
    )
    # 没有配远端时 fetch 会失败（或空跑成功），关键是**不是 403**：
    # 403 只留给"平台禁止的操作"（强推/reset --hard 等）
    assert denied.status_code == 200
    assert denied.json()["action"] == "fetch"

    dangerous = client.post(
        f"/api/v1/code/projects/{pid}/git/action", json={"action": "fetch", "force": True}, headers=h
    )
    assert dangerous.status_code == 200  # 未知字段不影响受控动作

    # 平台禁止的 git 动作在任务服务层被硬拒（用户没有这个按钮）
    from code_agent.codeintel.tasks import TaskService  # noqa: F401

    from code_agent.codeintel.vcs import check_command

    assert check_command(["push", "--force", "origin", "main"])

    # 切分支：工作区不干净时 409
    (repo / "pkg" / "text.py").write_text('"""脏工作区。"""\n', encoding="utf-8")
    blocked = client.post(
        f"/api/v1/code/projects/{pid}/git/checkout", json={"branch": "main"}, headers=h
    )
    assert blocked.status_code == 409
    assert "未提交" in blocked.json()["detail"]
    _git(repo, "checkout", "--", "pkg/text.py")
