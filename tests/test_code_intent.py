"""代码模式 P6：影响面与风险分级 / 架构建模约束 / 节点决策线程 / 改动溯源。

这一层的价值不在"多几个数字"，而在**让改动可被判定、让决定可被追溯**：

1. **影响面可枚举**：沿反向依赖算传递闭包——"改这里会惊动谁"是事实，不是直觉。
2. **风险有理由**：每个信号都带一句人话；只给分数的风险提示没法指导决策。
3. **架构门禁能拦人**：反向依赖、模块成环、悄悄改公共面——这些**可以被判定**的事故
   才配当硬门禁；其余只做提示（"被 12 处引用"不该禁止你改公共函数）。
4. **强制必须留痕**：绕过门禁需要写明理由，理由进决策线程。
5. **溯源有据**：台账 + 任务 + 决策 + git 提交合成一条时间线。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.codeintel import architecture as arch
from code_agent.codeintel import graph, impact as impact_mod
from code_agent.codeintel.decisions import DecisionError, DecisionStore, normalize_target
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.tasks import TaskError, TaskService
from code_agent.codeintel.vcs import git_env
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
        raise AssertionError(f"git failed: {proc.stdout}{proc.stderr}")
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
    """一个小仓库：mathx 被 text / app / tests 三处引用。"""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text('"""示例包。"""\n', encoding="utf-8")
    (root / "pkg" / "mathx.py").write_text(
        '"""数学工具。"""\n\n\ndef add(a: int, b: int) -> int:\n    """相加。"""\n    return a + b\n',
        encoding="utf-8",
    )
    (root / "pkg" / "text.py").write_text(
        '"""文本工具。"""\n\nfrom pkg.mathx import add\n\n\ndef shout(text: str) -> str:\n'
        '    return text.upper() * add(1, 1)\n',
        encoding="utf-8",
    )
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text(
        "from pkg.mathx import add\nfrom pkg.text import shout\n\n\n"
        "def run() -> str:\n    return shout('x') + str(add(1, 2))\n",
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
        "id": "p_p6", "name": "proj", "root_path": str(repo),
        "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin",
    }
    projects = FakeProjects(row)
    intel = CodeIntelService(projects)
    asyncio.run(intel.scan(row, full=True))
    db = Database(tmp_path / "p6.db")
    tasks = TaskRepo(db)
    task_service = TaskService(projects=projects, tasks=tasks, intel=intel, sessions=None)
    return {
        "row": row, "intel": intel, "tasks": tasks,
        "task_service": task_service, "repo": repo, "home": agent_home,
    }


# ---------------------------------------------------------------------------
# 影响面 / 风险分级
# ---------------------------------------------------------------------------
def test_reach_follows_reverse_dependencies(env):
    index = env["intel"].index(env["row"]["id"], "main")
    report = impact_mod.reach(index, files=["pkg/mathx.py"])
    # mathx 被 text.py / app/main.py / tests/test_mathx.py 引用 → 都在影响面里
    assert "pkg/text.py" in report["files"]
    assert "app/main.py" in report["files"]
    assert "tests/test_mathx.py" in report["files"]
    assert report["fan_in"] >= 3
    assert report["levels"] and len(report["levels"]) >= 1
    assert "pkg" in report["own_modules"] and "app" in report["modules"]
    # 起点文件自己不算"被波及"
    assert "pkg/mathx.py" not in report["files"]


def test_related_tests_by_convention_and_by_import(env):
    index = env["intel"].index(env["row"]["id"], "main")
    tests = impact_mod.related_tests(index, ["pkg/mathx.py"])
    assert "tests/test_mathx.py" in tests
    assert impact_mod.related_tests(index, ["app/main.py"]) == []


def test_risk_reasons_are_explicit_and_never_a_bare_number(env):
    index = env["intel"].index(env["row"]["id"], "main")
    covered = env["intel"].impact(env["row"], files=["pkg/mathx.py"])
    assert covered["risk"]["reasons"], "风险必须给理由"
    assert covered["risk"]["signals"]["tests"] >= 1
    assert covered["risk"]["level"] in ("low", "medium", "high", "critical")

    # 保护路径：分数更高，且理由里点名
    risky = impact_mod.analyze(
        index, files=["pkg/mathx.py"],
        protected=[{"glob": "pkg/mathx.py", "reason": "核心算法"}],
    )
    assert risky["risk"]["score"] > covered["risk"]["score"]
    joined = " ".join(risky["risk"]["reasons"])
    assert "保护路径" in joined and "核心算法" in joined


def test_impact_marks_public_api(env):
    index = env["intel"].index(env["row"]["id"], "main")
    rows = index.all_symbols(path="pkg/mathx.py")
    add_row = next(r for r in rows if r["name"] == "add")
    public, why = impact_mod.is_public_api(add_row)
    assert public is True and why
    report = impact_mod.risk_of(
        reach_report={"fan_in": 0, "files_total": 0, "modules": []},
        symbols=[graph.symbol_summary(add_row)],
        related_test_files=["tests/test_mathx.py"],
    )
    assert any("对外承诺" in reason for reason in report["reasons"])


def test_grade_thresholds():
    assert impact_mod.grade(0) == "low"
    assert impact_mod.grade(24) == "low"
    assert impact_mod.grade(25) == "medium"
    assert impact_mod.grade(49) == "medium"
    assert impact_mod.grade(50) == "high"
    assert impact_mod.grade(75) == "critical"


# ---------------------------------------------------------------------------
# 架构规则：自检与匹配
# ---------------------------------------------------------------------------
def test_validate_rules_rejects_broken_rules():
    errors = arch.validate_rules({"layers": [{"name": "ui"}, {"name": "ui", "paths": ["a/**"]}]})
    assert any("缺少 paths" in e for e in errors)
    assert any("重复" in e for e in errors)
    assert any(
        "不存在" in e
        for e in arch.validate_rules({"layers": [{"name": "ui", "paths": ["a/**"], "allow": ["nope"]}]})
    )
    assert any(".." in e for e in arch.validate_rules({"protected_paths": [{"glob": "../x"}]}))
    assert arch.validate_rules({"no_cycles": "yes"})
    assert arch.validate_rules({"layers": [{"name": "ui", "paths": ["web/**"], "allow": []}]}) == []


def test_glob_semantics():
    assert arch._glob_match("web/**", "web/src/a.js")
    assert arch._glob_match("web/**", "web")
    assert not arch._glob_match("web/**", "webs/a.js")
    assert arch._glob_match("*.py", "pkg/a.py")          # 不发明新语法：* 跨 /
    assert arch._glob_match("**/x.py", "a/b/x.py")


def test_rules_roundtrip_and_bad_json(env):
    row = env["row"]
    saved = env["intel"].save_architecture(
        row, {"layers": [{"name": "lib", "paths": ["pkg/**"], "allow": []}]}
    )
    assert saved["layers"] == ["lib"] and saved["errors"] == []
    assert env["intel"].architecture(row)["rules"]["layers"][0]["name"] == "lib"

    # 规则文件写坏了：如实报错，而不是当空规则放行
    arch.rules_path(row["id"]).write_text("{ 这不是 JSON", encoding="utf-8")
    broken = env["intel"].architecture(row)
    assert broken["rules"] == {} and broken["errors"]

    with pytest.raises(arch.ArchitectureError):
        env["intel"].save_architecture(row, {"layers": [{"name": "x"}]})


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------
def test_gate_blocks_forbidden_layer_direction(env):
    row = env["row"]
    index = env["intel"].index(row["id"], "main")
    rules = {
        "layers": [
            {"name": "spec", "paths": ["tests/**"], "allow": []},
            {"name": "lib", "paths": ["pkg/**"], "allow": []},
        ]
    }
    # tests → pkg 不被允许：改 tests 里的文件就会踩线
    gate = arch.evaluate(index, rules, files=["tests/test_mathx.py"])
    assert gate["ok"] is False
    assert gate["counts"]["error"] >= 1
    assert any(v["rule"] == "layer" and v["to_layer"] == "lib" for v in gate["violations"])
    assert "不允许依赖" in gate["violations"][0]["message"]

    # 反过来（lib 不依赖 spec）没问题
    assert arch.evaluate(index, rules, files=["pkg/mathx.py"])["ok"] is True

    # allow 里写上就放行
    allowed = arch.evaluate(
        index,
        {"layers": [{"name": "spec", "paths": ["tests/**"], "allow": ["lib"]},
                    {"name": "lib", "paths": ["pkg/**"], "allow": []}]},
        files=["tests/test_mathx.py"],
    )
    assert allowed["ok"] is True


def test_gate_reports_cycles_only_around_changed_files(tmp_path):
    """整仓的环是历史包袱；只报与本次改动有关的环，门禁才不会被当成噪音。"""
    root = tmp_path / "cyc"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    (root / "a" / "__init__.py").write_text("", encoding="utf-8")
    (root / "b" / "__init__.py").write_text("", encoding="utf-8")
    (root / "a" / "x.py").write_text(
        "import b.y\n\n\ndef ax():\n    return b.y.by()\n", encoding="utf-8"
    )
    (root / "b" / "y.py").write_text("import a.x\n\n\ndef by():\n    return 1\n", encoding="utf-8")
    (root / "c").mkdir()
    (root / "c" / "z.py").write_text("def z():\n    return 0\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    projects = FakeProjects({"id": "p_cyc", "name": "cyc", "root_path": str(root),
                             "root_key": str(root).lower(), "user_id": "u_admin"})
    intel = CodeIntelService(projects)
    asyncio.run(intel.scan(projects.row, full=True))
    index = intel.index("p_cyc", "main")

    hit = arch.evaluate(index, {"no_cycles": True}, files=["a/x.py"])
    assert hit["ok"] is False
    assert any(v["rule"] == "cycle" for v in hit["violations"])

    # 改一个与环无关的文件：不该被这个环牵连
    clean = arch.evaluate(index, {"no_cycles": True}, files=["c/z.py"])
    assert clean["ok"] is True and clean["violations"] == []


def test_gate_public_api_and_protected_paths(env):
    index = env["intel"].index(env["row"]["id"], "main")
    rows = index.all_symbols(path="pkg/mathx.py")
    rules = {
        "public_api": {"paths": ["pkg/**"], "require_ack": True},
        "protected_paths": [{"glob": "pkg/mathx.py", "reason": "核心算法"}],
    }
    gate = arch.evaluate(
        index, rules, files=["pkg/mathx.py"],
        changed_symbols=[graph.symbol_summary(r) for r in rows],
    )
    assert gate["ok"] is False
    assert any(v["rule"] == "public_api" for v in gate["violations"])
    assert any(v["rule"] == "protected" and v["reason"] == "核心算法" for v in gate["violations"])

    # require_ack=False → 公共面降级为 warn，不拦
    soft = arch.evaluate(
        index, {"public_api": {"paths": ["pkg/**"], "require_ack": False}},
        files=["pkg/mathx.py"], changed_symbols=[graph.symbol_summary(r) for r in rows],
    )
    assert soft["ok"] is True and soft["counts"]["warn"] >= 1


def test_gate_without_rules_is_not_a_fake_pass(env):
    gate = env["intel"].gate(env["row"], files=["pkg/mathx.py"])
    assert gate["configured"] is False and gate["ok"] is True
    assert gate["violations"] == []


def test_reach_is_labelled_as_static_not_runtime_impact(env):
    """别把图上的可达性说成运行时影响（archify 的明确禁令）。

    载荷必须自带口径：方法、依据、以及"不是运行时影响"的显式标记——
    界面直接照抄，读者不必猜这个数字是怎么来的。
    """
    report = env["intel"].impact(env["row"], files=["pkg/mathx.py"])
    assert report["reach"]["method"] == "static-import"
    assert "静态可达" in report["reach"]["basis"]
    assert report["reach"]["not_runtime_impact"] is True
    assert report["not_runtime_impact"] is True and "静态" in report["basis"]
    assert report["risk"]["not_runtime_impact"] is True
    assert any("静态可达" in reason for reason in report["risk"]["reasons"]) or report["risk"]["reasons"]
    # 措辞里不出现"破坏面/运行时影响"这类没有证据的说法
    joined = " ".join([report["basis"], report["reach"]["basis"], *report["risk"]["reasons"]])
    assert "破坏" not in joined and "运行时" not in joined.replace("不是运行时影响或故障预测", "")


def test_evidence_beacon_counts_patches_and_decisions(env):
    """SRC 角标只数"平台自己记下来的证据"：未回滚的台账 + 决策；回滚掉的不算。"""
    row = env["row"]
    intel = env["intel"]
    empty = intel.evidence(row)
    assert empty["totals"]["items"] == 0 and empty["viewer_only"] is True
    assert "台账" in empty["basis"] and "决策" in empty["basis"]

    first = intel.record_patch(
        workspace=env["repo"], rel="pkg/mathx.py", session_id="s", tool="edit_file",
        before="def add(a, b):\n    return a + b\n", after="def add(a, b):\n    return a + b + 0\n",
    )
    intel.decisions(row["id"]).record(target="sym:pkg/mathx.py#add", text="签名保持不变", kind="decision")
    intel.decisions(row["id"]).record(target="file:pkg/mathx.py", text="准备补测试", kind="todo")

    got = intel.evidence(row)
    assert got["paths"]["pkg/mathx.py"] == {"patches": 1, "decisions": 1}
    assert got["symbols"] == {"pkg/mathx.py#add": 1}
    assert got["totals"] == {"patches": 1, "decisions": 2, "nodes": 2, "items": 3}

    # 回滚有前置条件：文件当前内容必须等于该次改动之后的内容（P2 的防覆盖规则），
    # 所以先把 "after" 落到磁盘上，模拟"这次改动之后没人再动过"。
    (env["repo"] / "pkg" / "mathx.py").write_text(
        "def add(a, b):\n    return a + b + 0\n", encoding="utf-8"
    )
    intel.revert_patch(row, first["id"])

    # 被回滚的那条不再计入证据（但"回滚"这个动作本身是一条新的、未被回滚的记录）
    store = intel.patches(row["id"])
    entries = store.list(limit=10, path="pkg/mathx.py")
    reverted = [r for r in entries if r["reverted_at"]]
    assert len(reverted) == 1 and reverted[0]["id"] == first["id"]
    assert store.counts_by_path()["pkg/mathx.py"] == len(entries) - len(reverted) == 1
    after = intel.evidence(row)
    assert after["totals"]["patches"] == 1
    assert after["paths"]["pkg/mathx.py"]["decisions"] == 1   # 决策不受回滚影响


# ---------------------------------------------------------------------------
# 合并门禁：拒绝 / 强制留痕
# ---------------------------------------------------------------------------
def test_merge_is_blocked_by_architecture_gate_and_force_needs_reason(env):
    row, service, repo = env["row"], env["task_service"], env["repo"]
    env["intel"].save_architecture(
        row, {"protected_paths": [{"glob": "pkg/mathx.py", "reason": "核心算法"}]}
    )

    async def flow():
        task = await service.create(row, user_id="u_admin", requirement="改核心算法")
        service._write_plan(task, {"goal": "改它", "subtasks": [
            {"id": "s1", "title": "改 mathx", "files": ["pkg/mathx.py"], "verify": []}]})
        task = env["tasks"].get(task["id"])
        (Path(task["worktree"]) / "pkg" / "mathx.py").write_text(
            '"""数学工具。"""\n\n\ndef add(a: int, b: int) -> int:\n    return a + b + 0\n',
            encoding="utf-8",
        )
        await service.review(row, env["tasks"].get(task["id"]))

        with pytest.raises(TaskError) as err:
            await service.merge(row, env["tasks"].get(task["id"]))
        assert "架构门禁未通过" in str(err.value)
        assert "核心算法" in str(err.value)

        # 强制但不给理由：仍然拒绝（"强制"必须留痕）
        with pytest.raises(TaskError) as err2:
            await service.merge(row, env["tasks"].get(task["id"]), force=True)
        assert "必须写明理由" in str(err2.value)

        # 强制 + 理由：落地，并留下一条 risk 决策
        result = await service.merge(
            row, env["tasks"].get(task["id"]), force=True, reason="本次只改注释（已知风险自担）"
        )
        assert result["ok"] is True

    asyncio.run(flow())
    assert "return a + b + 0" in (repo / "pkg" / "mathx.py").read_text(encoding="utf-8")
    items = env["intel"].decisions(row["id"]).list(target="file:pkg/mathx.py")
    assert items and items[0]["kind"] == "risk"
    assert "[强制落地]" in items[0]["text"] and "已知风险自担" in items[0]["text"]


# ---------------------------------------------------------------------------
# 决策线程
# ---------------------------------------------------------------------------
def test_decision_store_record_list_and_delete(env):
    store: DecisionStore = env["intel"].decisions(env["row"]["id"])
    first = store.record(
        target="sym:pkg/mathx.py#add", text="保持向后兼容", kind="decision", author="admin"
    )
    store.record(target="sym:pkg/mathx.py#add", text="暂时不加类型校验", kind="risk")
    store.record(target="file:pkg/mathx.py", text="准备补测试", kind="todo")
    store.record(target="file:pkg/text.py", text="另外一处", kind="note")

    exact = store.list(target="sym:pkg/mathx.py#add")
    assert len(exact) == 2 and {i["kind"] for i in exact} == {"decision", "risk"}
    # 文件级查询带出它内部符号的决定（prefix）
    assert len(store.list(target="file:pkg/mathx.py", prefix=True)) == 3
    assert [i["text"] for i in store.list(target="file:pkg/mathx.py")] == ["准备补测试"]
    assert len(store.list(kinds=["todo"])) == 1
    summary = store.summary()
    assert summary["total"] == 4 and summary["targets"]["sym:pkg/mathx.py#add"]["total"] == 2

    assert store.delete(first["id"]) is True
    assert len(store.all()) == 3
    assert store.delete("d_nope") is False
    assert store.delete_target("file:pkg/text.py") == 1
    assert len(store.all()) == 2


def test_decision_store_rejects_empty_text_or_target(env):
    store = env["intel"].decisions(env["row"]["id"])
    with pytest.raises(DecisionError):
        store.record(target="file:a.py", text="   ")
    with pytest.raises(DecisionError):
        store.record(target="", text="有内容但没目标")
    assert normalize_target("pkg/a.py") == "file:pkg/a.py"
    assert normalize_target("sym:x#y") == "sym:x#y"
    assert normalize_target("") == ""


# ---------------------------------------------------------------------------
# 改动溯源
# ---------------------------------------------------------------------------
def test_provenance_merges_patch_task_decision_and_commit(env):
    row = env["row"]
    # 1) 台账：记一次精确编辑
    env["intel"].record_patch(
        workspace=env["repo"], rel="pkg/mathx.py", session_id="s_demo", tool="edit_file",
        before="def add(a, b):\n    return a + b\n",
        after="def add(a, b):\n    return a + b + 0\n",
    )
    # 2) 决策
    env["intel"].decisions(row["id"]).record(
        target="file:pkg/mathx.py", text="加 0 是为了触发一次改动", kind="note", author="admin"
    )
    # 3) 任务：审查快照里含这个文件
    task = env["tasks"].create(
        project_id=row["id"], user_id="u_admin", title="调整 add", requirement="调 add",
        base_branch="master", branch="ai/x", worktree="", status="merged", lane="L1",
        batch_id="b_demo",
    )
    env["tasks"].update(
        task["id"],
        review_json=json.dumps({"changed_files": ["pkg/mathx.py"], "gate": {"ok": True}}),
        plan_json=json.dumps({"goal": "让 add 稳定"}),
    )

    async def flow():
        return await env["intel"].provenance(row, path="pkg/mathx.py", tasks=env["tasks"])

    report = asyncio.run(flow())
    sources = {item["source"] for item in report["items"]}
    assert {"patch", "decision", "batch", "commit"} <= sources
    batch_item = next(i for i in report["items"] if i["source"] == "batch")
    assert batch_item["refs"]["lane"] == "L1" and "让 add 稳定" in batch_item["detail"]
    ts = [item["ts"] for item in report["items"]]
    assert ts == sorted(ts, reverse=True)      # 时间倒序


def test_provenance_by_symbol_and_empty_for_unknown(env):
    row = env["row"]

    async def flow():
        by_symbol = await env["intel"].provenance(
            row, symbol_id="sym:pkg/mathx.py#add", tasks=env["tasks"]
        )
        unknown = await env["intel"].provenance(row, path="nope/none.py", tasks=env["tasks"])
        return by_symbol, unknown

    by_symbol, unknown = asyncio.run(flow())
    assert by_symbol["path"] == "pkg/mathx.py"      # 从符号回推出文件
    assert unknown["path"] == "nope/none.py" and unknown["total"] == 0


# ---------------------------------------------------------------------------
# API
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


def test_p6_api_endpoints(client, repo):
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=h)

    # 影响面
    report = client.post(
        f"/api/v1/code/projects/{pid}/impact", json={"files": ["pkg/mathx.py"]}, headers=h
    ).json()
    assert report["reach"]["fan_in"] >= 3
    assert report["risk"]["reasons"]
    assert "tests/test_mathx.py" in report["tests"]

    # 架构规则：坏规则 422
    bad = client.put(
        f"/api/v1/code/projects/{pid}/architecture",
        json={"rules": {"layers": [{"name": "ui"}]}},
        headers=h,
    )
    assert bad.status_code == 422 and "缺少 paths" in bad.json()["detail"]

    saved = client.put(
        f"/api/v1/code/projects/{pid}/architecture",
        json={"rules": {"layers": [
            {"name": "spec", "paths": ["tests/**"], "allow": []},
            {"name": "lib", "paths": ["pkg/**"], "allow": []},
        ]}},
        headers=h,
    ).json()
    assert saved["layers"] == ["spec", "lib"] and saved["errors"] == []

    # 门禁预演
    gate = client.post(
        f"/api/v1/code/projects/{pid}/gate", json={"files": ["tests/test_mathx.py"]}, headers=h
    ).json()
    assert gate["ok"] is False and gate["counts"]["error"] >= 1

    # 决策线程
    created = client.post(
        f"/api/v1/code/projects/{pid}/decisions",
        json={"target": "sym:pkg/mathx.py#add", "text": "保持签名不变", "kind": "decision"},
        headers=h,
    ).json()
    assert created["item"]["kind_label"] == "决定"
    listed = client.get(
        f"/api/v1/code/projects/{pid}/decisions",
        params={"target": "sym:pkg/mathx.py#add"},
        headers=h,
    ).json()
    assert listed["items"][0]["text"] == "保持签名不变"
    assert listed["summary"]["total"] == 1
    assert client.delete(
        f"/api/v1/code/projects/{pid}/decisions/{created['item']['id']}", headers=h
    ).json()["ok"] is True
    assert client.delete(f"/api/v1/code/projects/{pid}/decisions/d_nope", headers=h).status_code == 404

    # 溯源
    prov = client.get(
        f"/api/v1/code/projects/{pid}/provenance", params={"path": "pkg/mathx.py"}, headers=h
    ).json()
    assert prov["path"] == "pkg/mathx.py" and "items" in prov

    # 越权
    from code_agent.api.security import hash_password

    client.app.state.users.create("eve", hash_password("e1"), role="user")
    eve = _login(client, "eve", "e1")
    assert client.post(
        f"/api/v1/code/projects/{pid}/impact", json={"files": ["pkg/mathx.py"]}, headers=_auth(eve)
    ).status_code == 404
    assert client.get(
        f"/api/v1/code/projects/{pid}/architecture", headers=_auth(eve)
    ).status_code == 404


def test_task_review_carries_gate_and_impact(client, repo):
    """审查快照里要同时带上"踩了哪条规则"和"会惊动谁"（同一屏看完）。"""
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=h)
    client.put(
        f"/api/v1/code/projects/{pid}/architecture",
        json={"rules": {"protected_paths": [{"glob": "pkg/mathx.py", "reason": "核心算法"}]}},
        headers=h,
    )
    created = client.post(
        f"/api/v1/code/projects/{pid}/tasks",
        json={
            "requirement": "改核心算法",
            "plan_first": False,
            "plan": {"goal": "改 mathx", "subtasks": [
                {"id": "s1", "title": "改它", "files": ["pkg/mathx.py"], "verify": []}]},
        },
        headers=h,
    ).json()
    task_id = created["id"]
    detail = client.get(f"/api/v1/code/projects/{pid}/tasks/{task_id}", headers=h).json()
    target = Path(detail["worktree"]) / "pkg" / "mathx.py"
    target.write_text(
        '"""数学工具。"""\n\n\ndef add(a: int, b: int) -> int:\n    return a + b + 0\n',
        encoding="utf-8",
    )
    review = client.get(f"/api/v1/code/projects/{pid}/tasks/{task_id}/review", headers=h).json()
    assert review["gate"]["ok"] is False
    assert any(v["rule"] == "protected" for v in review["gate"]["violations"])
    assert review["impact"]["risk"]["reasons"]
    assert "pkg/mathx.py" in review["changed_files"]

    # 门禁拦住合并：422 + 人话
    blocked = client.post(f"/api/v1/code/projects/{pid}/tasks/{task_id}/merge", json={}, headers=h)
    assert blocked.status_code == 422
    assert "架构门禁未通过" in blocked.json()["detail"]

    # 强制 + 理由 → 放行，并留痕
    ok = client.post(
        f"/api/v1/code/projects/{pid}/tasks/{task_id}/merge",
        json={"force": True, "reason": "评审已口头同意"},
        headers=h,
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True
    decisions = client.get(
        f"/api/v1/code/projects/{pid}/decisions", params={"target": "file:pkg/mathx.py"}, headers=h
    ).json()
    assert any("[强制落地]" in item["text"] for item in decisions["items"])
