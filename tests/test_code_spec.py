"""代码模式 P7：测试即规格（行为卡片 / 覆盖率）+ 图即文档 + 大仓加固。

三条纪律，测试逐条盯着：

1. **覆盖率必须诚实**：直接覆盖与"文件级覆盖"分开报——把后者说成"这条函数被测试了"
   是最常见的自欺；
2. **文档只写事实、缺了就说不全**：被截断的章节要显式标注，章节裁剪后编号要跟着重排；
3. **大仓不会把索引/地图/上下文撑爆**：单文件符号数有上限且**留痕**，地图装配有上限
   且给 warning，增量扫描不因为仓库变大而变慢（指纹命中不重解析）。
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from code_agent.codeintel import export as export_mod
from code_agent.codeintel import graph, spec
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.store import MAX_SYMBOLS_PER_FILE
from code_agent.codeintel.vcs import git_env


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
    """一个小仓库：mathx 被测试直接引用，text.py 只有"同模块测试"这一层。"""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text('"""示例包。"""\n', encoding="utf-8")
    (root / "pkg" / "mathx.py").write_text(
        '"""数学工具。"""\n\n\ndef add(a: int, b: int) -> int:\n    """相加。"""\n    return a + b\n',
        encoding="utf-8",
    )
    (root / "pkg" / "text.py").write_text(
        '"""文本工具。"""\n\n\ndef shout(text: str) -> str:\n    return text.upper()\n\n\n'
        'def quiet(text: str) -> str:\n    """测试文件里没提过这个名字。"""\n    return text.lower()\n',
        encoding="utf-8",
    )
    (root / "pkg" / "lonely.py").write_text(
        '"""没人测过。"""\n\n\ndef lonely(value: int) -> int:\n    return value\n',
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_mathx.py").write_text(
        '"""数学工具的测试。"""\n'
        "from pkg.mathx import add\n\n\n"
        "def test_add_positive_numbers():\n"
        '    """正数相加。"""\n'
        "    assert add(1, 2) == 3\n"
        "    assert add(0, 0) == 0\n\n\n"
        "def test_add_negative_numbers():\n"
        "    assert add(-1, -2) == -3\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_text.py").write_text(
        "from pkg.text import shout\n\n\n"
        "def test_shout_empty():\n"
        "    # 这条没有断言\n"
        "    shout('')\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def env(repo: Path, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {
        "id": "p_p7", "name": "proj", "root_path": str(repo),
        "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin",
    }
    projects = FakeProjects(row)
    intel = CodeIntelService(projects)
    asyncio.run(intel.scan(row, full=True))
    return {"row": row, "intel": intel, "repo": repo}


# ---------------------------------------------------------------------------
# 行为卡片
# ---------------------------------------------------------------------------
def test_is_test_symbol_and_humanize():
    assert spec.is_test_symbol({"path": "tests/test_a.py", "kind": "function", "name": "test_x"})
    # `*.spec.*` 文件里的函数本身就是行为（名字不叫 test_ 也算）
    assert spec.is_test_symbol({"path": "web/src/a.spec.js", "kind": "function", "name": "renders"})
    assert not spec.is_test_symbol({"path": "pkg/a.py", "kind": "function", "name": "helper_for_tests"})
    assert spec.humanize("test_add_positive_numbers") == "add positive numbers"
    assert spec.humanize("testLogin") == "Login"
    assert spec.count_asserts(["    assert x == 1", "    expect(y).toBe(2)", "    pass"]) == 2


def test_mentioned_symbols_ignores_ambiguous_names():
    """重名只靠"提到了这个名字"指不出是哪一条 → 不认（宁可漏，不可错）。"""
    names = {"add": ["sym:a#add", "sym:b#add"], "unique": ["sym:c#unique"]}
    hits = spec.mentioned_symbols("from a import add\nunique(1)\n", names)
    assert hits == ["sym:c#unique"]


def test_behaviors_read_tests_as_specifications(env):
    report = env["intel"].behaviors(env["row"], module="tests")
    names = {c["behavior"] for c in report["cards"]}
    assert "add positive numbers" in names and "add negative numbers" in names
    card = next(c for c in report["cards"] if c["behavior"] == "add positive numbers")
    assert card["asserts"] == 2 and card["doc"] == "正数相加。"
    assert "pkg/mathx.py" in card["covers_files"] or card["covers_symbols"]
    # 没有断言的测试要能被点出来（"看起来有测试，其实什么都没验证"）
    assert report["no_asserts"] >= 1


def test_behaviors_reverse_lookup_by_target(env):
    """反向查：谁在保护这条符号 / 这个文件。"""
    by_file = env["intel"].behaviors(env["row"], target="pkg/mathx.py")
    assert by_file["cards"], "文件级反向查应该能命中"
    assert all(c["path"].startswith("tests/") for c in by_file["cards"])

    sid = "sym:pkg/mathx.py#add"
    by_symbol = env["intel"].behaviors(env["row"], target=sid)
    assert by_symbol["cards"], "符号级反向查应该命中提到过 add 的测试"
    lonely = env["intel"].behaviors(env["row"], target="pkg/lonely.py")
    assert lonely["cards"] == []


# ---------------------------------------------------------------------------
# 覆盖率（三态）
# ---------------------------------------------------------------------------
def test_coverage_reports_three_states_honestly(env):
    cov = env["intel"].coverage(env["row"])
    totals = cov["totals"]
    assert totals["symbols"] >= 6 and totals["test_files"] == 2
    assert totals["direct"] + totals["file_level"] + totals["uncovered"] == totals["symbols"]
    # 直接覆盖：测试文件里按词边界提到过这个名字
    assert "sym:pkg/mathx.py#add" in cov["direct_symbols"]
    by_file = {item["path"]: item for item in cov["files"]}
    assert by_file["pkg/mathx.py"]["state"] == "direct"
    assert by_file["pkg/lonely.py"]["state"] == "none"
    # 未覆盖清单只收"公共面"，且必须能说清为什么算公共
    uncovered = {u["name"] for u in cov["uncovered"]}
    assert "lonely" in uncovered
    assert all(u.get("why_public") for u in cov["uncovered"])
    assert 0.0 <= totals["ratio"] <= 1.0
    assert "词边界" in cov["method"]


def test_coverage_marks_file_level_not_direct(env):
    """文件级覆盖不能被算成"这条函数被测试了"。"""
    cov = env["intel"].coverage(env["row"])
    by_file = {item["path"]: item for item in cov["files"]}
    text_file = by_file["pkg/text.py"]
    # 同一个文件里：shout 被测试提到过（直接），quiet 没有（文件级）
    assert "sym:pkg/text.py#shout" in cov["direct_symbols"]
    assert "sym:pkg/text.py#quiet" not in cov["direct_symbols"]
    assert text_file["state"] == "direct" and text_file["file"] >= 1


# ---------------------------------------------------------------------------
# 图即文档
# ---------------------------------------------------------------------------
def test_export_builds_markdown_with_facts_only(env):
    row = env["intel"]
    doc = export_mod.from_service(row, env["row"], sections=["overview", "modules", "specs", "architecture"])
    assert doc["sections"] == ["overview", "modules", "specs", "architecture"]
    text = doc["markdown"]
    assert text.startswith("# proj · 代码地图文档")
    assert "## 1. 概览" in text and "## 4. 架构约束" in text   # 章节裁剪后编号要连续
    assert "测试覆盖（按符号）" in text
    assert "行为卡片" in text
    assert doc["chars"] > 500 and doc["lines"] > 20


def test_export_marks_truncation_and_saves_off_repo(env, agent_home):
    doc = export_mod.from_service(env["intel"], env["row"], sections=["specs"])
    assert "（共" not in doc["markdown"] or "仅列" in doc["markdown"]
    path = export_mod.save(env["row"]["id"], doc["markdown"], name="map.md")
    saved = Path(path)
    assert saved.is_file() and saved.read_text(encoding="utf-8").startswith("#")
    # 导出物落在平台侧：用户仓库里不长平台文件
    assert saved.is_relative_to(Path(agent_home))
    assert not (env["repo"] / "exports").exists()
    assert not (env["repo"] / "map.md").exists()


def test_export_includes_decisions_and_changes(env):
    store = env["intel"].decisions(env["row"]["id"])
    store.record(target="file:pkg/lonely.py", text="暂时不测：等接口稳定", kind="risk", author="admin")
    doc = export_mod.from_service(env["intel"], env["row"], sections=["decisions", "changes", "entrypoints"])
    text = doc["markdown"]
    assert "暂时不测：等接口稳定" in text
    assert "## 1. 决策与已知风险" in text
    assert "还没有任务记录" in text


def test_export_receipt_makes_the_artifact_checkable(env):
    """回执要能回答"它由什么生成、检查了什么、哪一节被截断"——而不是给个分数。"""
    import hashlib

    doc = export_mod.from_service(env["intel"], env["row"], sections=["overview", "specs"])
    receipt = doc["receipt"]
    assert receipt["artifact"]["sha256"] == hashlib.sha256(doc["markdown"].encode("utf-8")).hexdigest()
    assert receipt["artifact"]["bytes"] == len(doc["markdown"].encode("utf-8"))
    assert receipt["artifact"]["sections"] == ["overview", "specs"]
    assert receipt["source"]["project_id"] == env["row"]["id"]
    assert receipt["source"]["index_version"] >= 0
    checks = {c["id"]: c for c in receipt["checks"]}
    assert set(checks) >= {"sections_present", "no_silent_truncation", "generated_at", "facts_only"}
    assert all(isinstance(c["ok"], bool) and c["detail"] for c in receipt["checks"])
    # 只读视图态不进导出物（Canonical Clean）
    assert "证据角标 SRC" in receipt["viewer_only_excluded"]
    assert "不进入导出物" in receipt["viewer_only_excluded_note"]


def test_export_receipt_reports_truncation_and_warnings(env):
    doc = export_mod.from_service(env["intel"], env["row"], sections=["modules"])
    receipt = doc["receipt"]
    # 小仓库不会被截断：回执必须如实说"没有章节被截断"
    assert receipt["truncated_sections"] == []
    assert any(c["id"] == "no_silent_truncation" and c["ok"] for c in receipt["checks"])

    # 索引为空时要有 warning（不假装文档里的规模数字可靠）
    row = {**env["row"], "id": "p_empty"}
    empty = export_mod.from_service(env["intel"], row, sections=["overview"])
    assert any("索引为空" in w for w in empty["receipt"]["warnings"])


def test_export_filename_carries_content_digest(agent_home, env):
    doc = export_mod.from_service(env["intel"], env["row"], sections=["overview"])
    path = Path(export_mod.save(env["row"]["id"], doc["markdown"]))
    digest = doc["receipt"]["artifact"]["sha256"][:8]
    assert path.name.endswith(f"-{digest}.md")      # 文件名能看出是不是同一版


# ---------------------------------------------------------------------------
# 大仓加固：单文件符号上限 + 地图上限 + 增量不退化
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def big_repo(tmp_path_factory) -> Path:
    """生成的"大仓"：200 个模块文件 × 25 个函数 ≈ 5200 个符号。"""
    root = tmp_path_factory.mktemp("big") / "big"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    for i in range(200):
        body = [f'"""模块 {i}。"""', ""]
        for j in range(25):
            body.append(f"def fn_{i}_{j}(value: int) -> int:")
            body.append(f'    """函数 {i}-{j}。"""')
            body.append("    return value + " + str(j))
            body.append("")
        (root / "pkg" / f"mod_{i}.py").write_text("\n".join(body), encoding="utf-8")
    for i in range(0, 40):
        (root / "tests" / f"test_mod_{i}.py").write_text(
            f"from pkg.mod_{i} import fn_{i}_0\n\n\ndef test_fn_{i}_0():\n"
            f"    assert fn_{i}_0(1) == 1\n",
            encoding="utf-8",
        )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_big_repo_scan_map_and_incremental_stay_within_budget(big_repo, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {
        "id": "p_big", "name": "big", "root_path": str(big_repo),
        "root_key": str(big_repo).lower(), "user_id": "u_admin",
    }
    projects = FakeProjects(row)
    intel = CodeIntelService(projects)

    started = time.perf_counter()
    asyncio.run(intel.scan(row, full=True))
    first = time.perf_counter() - started
    counts = intel.index(row["id"], "main").counts()
    assert counts["files"] >= 240 and counts["symbols"] >= 5000
    assert first < 30, f"首次全量扫描过慢：{first:.2f}s"

    # 增量：什么都没变 → 全部复用，不重解析
    started = time.perf_counter()
    asyncio.run(intel.scan(row, full=False))
    second = time.perf_counter() - started
    summary = intel.index(row["id"], "main").scan_summary()
    assert summary["parsed"] == 0 and summary["reused"] >= 240
    assert second < max(3.0, first), f"增量扫描没有复用：{second:.2f}s vs 全量 {first:.2f}s"

    # 地图装配：有上限、有 warning、不因为符号多而卡住
    started = time.perf_counter()
    payload = intel.map_payload(row, depth=2)
    assembly = time.perf_counter() - started
    assert assembly < 5, f"地图装配过慢：{assembly:.2f}s"
    assert payload["stats"]["truncated"] is True
    assert any("截断" in w for w in payload["warnings"])

    # 覆盖率与文档也要在有上限的前提下算完
    started = time.perf_counter()
    cov = intel.coverage(row)
    assert cov["totals"]["symbols"] >= 5000
    assert time.perf_counter() - started < 15
    doc = export_mod.from_service(intel, row, sections=["overview", "modules", "specs"])
    assert doc["chars"] > 500


def test_single_file_symbol_cap_is_enforced_and_recorded(repo, tmp_path, agent_home, monkeypatch):
    """生成代码（一个文件几万个函数）不许把索引撑爆，而且必须留痕。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    root = tmp_path / "gen"
    (root / "pkg").mkdir(parents=True)
    total = MAX_SYMBOLS_PER_FILE + 300
    body = ["# 生成代码", ""]
    for i in range(total):
        body.append(f"def gen_{i}(value: int) -> int:")
        body.append("    return value")
        body.append("")
    (root / "pkg" / "generated.py").write_text("\n".join(body), encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    row = {"id": "p_cap", "name": "gen", "root_path": str(root),
           "root_key": str(root).lower(), "user_id": "u_admin"}
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    index = intel.index(row["id"], "main")

    assert len(index.symbols_for("pkg/generated.py")) == MAX_SYMBOLS_PER_FILE
    file_row = index.file_row("pkg/generated.py")
    assert int(file_row["symbols_capped"] or 0) == 1
    summary = index.scan_summary()
    assert summary["symbols_capped_count"] == 1
    assert summary["symbols_capped"] == ["pkg/generated.py"]
    # 地图要提示"这里不全"，不能假装看全了
    payload = intel.map_payload(row, depth=2)
    assert any("截断" in w for w in payload["warnings"])


def test_parse_still_records_symbols_below_the_cap(repo, agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {"id": "p_small", "name": "proj", "root_path": str(repo),
           "root_key": str(repo).lower(), "user_id": "u_admin"}
    intel = CodeIntelService(FakeProjects(row))
    asyncio.run(intel.scan(row, full=True))
    index = intel.index(row["id"], "main")
    assert int(index.file_row("pkg/mathx.py")["symbols_capped"] or 0) == 0
    assert index.scan_summary().get("symbols_capped_count", 0) == 0
    assert not any("截断" in w for w in intel.map_payload(row)["warnings"])


def test_graph_helpers_used_by_export_match_index(env):
    """导出用的模块/入口/路由聚合必须来自索引（不是另算一套）。"""
    index = env["intel"].index(env["row"]["id"], "main")
    modules = graph.build_modules(index)
    assert any(m["path"] == "pkg" for m in modules)
    assert all("symbol_counts" in m for m in modules)
    doc = export_mod.from_service(env["intel"], env["row"], sections=["modules"])
    assert "pkg" in doc["markdown"]
