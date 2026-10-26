"""契约层（M32）：解析 / 形状快照 / 一致性检查 / 契约工具 / R7 闸门。

这一层不是"发明一门新语言"，而是把阶段 0 实验量出来的三件事机械化
（`docs/contract-first-phase0-report.md`）：

1. **形状闸门**：实验里 4 次独立实现全部 PASS（契约层一字未改），它既能拦住违约、
   又不妨碍"实现可丢弃"；
2. **缺口可见**：实验里 8/10 条义务有歧义、16/25 处文案漂移**没有任何闸门报警**——
   所以检查必须把"没证据的义务"和"无主的实现"枚举出来；
3. **诚实的缺口 ≠ 遗漏**：契约可以明确声明"这条没有独立证据"（实验里实现者
   自己就标出了这类），机器只拦**静默缺口**——否则会逼人编一条假证据来凑检查。

另外两条这里没做（明确记下来，不要以为已覆盖）：**契约 §3 的数据值比对**、
**证据哈希钉死**（现在只做"证据名能否在 tests/ 里找到"）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from code_agent.contract import check as check_mod
from code_agent.contract import contract_check, find_contract, list_contracts, parse_contract
from code_agent.contract.parse import SUFFIX
from code_agent.contract.shape import ShapeError, shape_of_source
from code_agent.permissions.policy import ContractGuardPolicy, Decision, is_evidence_path
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.contract_tools import ContractCheckTool

REPO = Path(__file__).resolve().parents[1]
REAL_MODULE = REPO / "code_agent" / "agent" / "routing.py"
REAL_CONTRACT = REPO / "code_agent" / "agent" / "routing.CONTRACT.md"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def test_real_contract_parses_shape_obligations_and_markers() -> None:
    c = parse_contract(REAL_CONTRACT)
    assert c.module == "routing.py"
    # 形状段：带字段的 class、两个函数、四个常量都要拿到
    assert {"Route", "classify", "route_hint", "RULES", "ROUTES", "DEFAULT_ROUTE", "MAX_HINT_TOOLS"} <= set(c.symbols)
    assert len(c.obligations) == 10
    by_id = {o.id: o for o in c.obligations}
    assert by_id["OBL1"].has_evidence
    assert by_id["OBL1"].evidence == ["test_classify_picks_the_more_specific_task_type"]
    # "无独立证据"必须被识别成**已声明的缺口**，而不是遗漏
    assert by_id["OBL5"].no_evidence_declared and not by_id["OBL5"].has_evidence
    assert by_id["OBL5"].settled
    assert c.declared_no_evidence == ["OBL5", "OBL10"]
    assert c.silent_gaps == []
    assert c.problems == [], c.problems      # 这份契约自身应当是干净的
    assert c.data_blocks > 0 and c.topology > 0


def test_parse_reports_silent_and_malformed_rows(tmp_path) -> None:
    """静默缺口与坏行都要被报出来——静默丢一条义务是这层最危险的错。"""
    path = tmp_path / "m.CONTRACT.md"
    path.write_text(
        "# 契约\n\n## 1. 形状（shape）\n\n```python\ndef f(a: int) -> int: ...\n```\n\n"
        "## 2. 义务（obligations）\n\n| id | 义务 | 证据 |\n|---|---|---|\n"
        "| OBL1 | 有证据的 | test_something |\n"
        "| OBL2 | 没写证据也没声明 | — |\n"
        "| OBL2 | 重复 id | test_x |\n"
        "| 随手写的 | id 形状不对 | test_y |\n",
        encoding="utf-8",
    )
    c = parse_contract(path)
    assert [o.id for o in c.obligations] == ["OBL1", "OBL2"]
    assert c.silent_gaps == ["OBL2"]
    assert any("重复" in p for p in c.problems)
    assert any("形状不对" in p for p in c.problems)


def test_parse_handles_contract_without_python_block(tmp_path) -> None:
    path = tmp_path / "x.CONTRACT.md"
    path.write_text("## 1. 形状（shape）\n\n没有代码块\n", encoding="utf-8")
    c = parse_contract(path)
    assert c.symbols == []
    assert any("没有解析到任何符号" in p for p in c.problems)


# ---------------------------------------------------------------------------
# 形状快照
# ---------------------------------------------------------------------------
def test_shape_detects_signature_and_import_changes() -> None:
    base = "def f(a: int) -> int:\n    return a\n"
    same = shape_of_source(base)
    mutated = shape_of_source("def f(a: int, b: int = 0) -> int:\n    return a\n")
    problems = same.diff(mutated)
    assert any("形状被改动" in p for p in problems), problems

    imported = shape_of_source("import json\n\ndef f(a: int) -> int:\n    return a\n")
    assert same.diff(imported) == [], "契约没声明 import 时不该报 import 问题"
    declared = shape_of_source("import os\n")
    assert any("新增 import" in p for p in declared.diff(imported))


def test_shape_marks_unimplemented_bodies() -> None:
    empty = shape_of_source("def f():\n    raise NotImplementedError('x')\n\ndef g():\n    pass\n")
    assert empty.implemented == set()
    done = shape_of_source("def f():\n    return 1\n")
    assert done.implemented == {"f"}


def test_shape_raises_on_syntax_error() -> None:
    with pytest.raises(ShapeError):
        shape_of_source("def f(:\n    pass\n")


def test_shape_of_declarations_tolerates_prose_and_decorators() -> None:
    from code_agent.contract.shape import shape_of_declarations

    lines = [
        "@dataclass(frozen=True)",
        "class Route:\n    kind: str\n    label: str",
        "MAX: int = 6",
        "def f(a: int) -> str: ...",
        "def g(a): ",                      # 没有函数体
        "def 这不是 python 声明的自然语言 ...",
    ]
    shape = shape_of_declarations(lines)
    assert {"Route", "MAX", "f", "g"} <= set(shape.symbols)
    assert shape.symbols["MAX"] == "const"


# ---------------------------------------------------------------------------
# 一致性检查（拿仓库里真实的 routing 契约做端到端）
# ---------------------------------------------------------------------------
def test_real_module_passes_its_contract() -> None:
    r = contract_check(REAL_MODULE, module_label="agent/routing.py")
    assert r.ok, r.summary()
    assert r.rho == 1.0
    assert r.obligation_count == 10 and r.evidence_count == 8
    assert r.coverage == 0.8
    assert r.declared_gaps == ["OBL5", "OBL10"]
    assert r.missing_evidence == []
    assert "别以为它们被验收了" in r.summary()


def test_check_flags_a_changed_signature(tmp_path) -> None:
    """改签名 = 契约变更：这是 R1/R2 的核心断言。"""
    module = tmp_path / "code_agent" / "agent"
    module.mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (module / "routing.CONTRACT.md").write_text(REAL_CONTRACT.read_text(encoding="utf-8"), encoding="utf-8")
    src = REAL_MODULE.read_text(encoding="utf-8").replace(
        "def classify(prompt: str) -> Route:", "def classify(prompt: str, strict: bool = False) -> Route:"
    )
    (module / "routing.py").write_text(src, encoding="utf-8")

    r = contract_check(module / "routing.py")
    assert not r.ok
    assert not r.shape_ok
    assert r.first_unsatisfied is not None
    assert "形状未被改动" in r.first_unsatisfied[0]
    assert "strict" in r.first_unsatisfied[1]


def test_check_flags_missing_evidence_and_unowned_functions(tmp_path) -> None:
    root = tmp_path
    (root / "tests").mkdir()
    (root / "tests" / "test_keep.py").write_text("def test_keep():\n    pass\n", encoding="utf-8")
    (root / "m.CONTRACT.md").write_text(
        "# 契约\n\n## 1. 形状（shape）\n\n```python\ndef declared() -> int: ...\n```\n\n"
        "## 2. 义务（obligations）\n\n| id | 义务 | 证据 |\n|---|---|---|\n"
        "| OBL1 | 有证据 | test_keep |\n"
        "| OBL2 | 证据找不到 | test_does_not_exist |\n"
        "| OBL3 | 静默缺口 | — |\n",
        encoding="utf-8",
    )
    (root / "m.py").write_text(
        "def declared() -> int:\n    return 1\n\n\ndef somebody_else():\n    return 2\n",
        encoding="utf-8",
    )
    r = contract_check(root / "m.py")
    assert not r.ok
    names = {n for n, ok, _ in r.checks if not ok}
    assert "没有无主的实现（R3）" in names
    assert "每条义务都有证据或明确声明无证据（R4）" in names
    assert "证据可定位（R6·弱化版）" in names
    assert r.unowned == ["somebody_else"]
    assert r.missing_evidence == ["test_does_not_exist"]


def test_check_without_contract_says_so(tmp_path) -> None:
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    r = contract_check(tmp_path / "m.py")
    assert not r.ok
    assert r.total == 1 and "契约存在" in r.checks[0][0]
    assert find_contract(tmp_path / "m.py") is None


def test_list_contracts_skips_vendor_dirs(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "m.CONTRACT.md").write_text("# x\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "n.CONTRACT.md").write_text("# y\n", encoding="utf-8")
    found = [p.name for p in list_contracts(tmp_path)]
    assert found == ["m.CONTRACT.md"]
    assert (tmp_path / "a" / "m.py").with_suffix(SUFFIX).name == "m.CONTRACT.md"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def test_tool_reports_progress_and_first_gap(tmp_path) -> None:
    module = tmp_path / "code_agent" / "agent"
    module.mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_routing.py").write_text(
        "def test_classify_picks_the_more_specific_task_type():\n    pass\n"
        "def test_route_hint_lists_only_available_tools():\n    pass\n"
        "def test_answer_tasks_get_no_tool_list():\n    pass\n"
        "def test_route_hint_reaches_the_system_prompt_without_removing_tools():\n    pass\n",
        encoding="utf-8",
    )
    (module / "routing.CONTRACT.md").write_text(REAL_CONTRACT.read_text(encoding="utf-8"), encoding="utf-8")
    (module / "routing.py").write_text(REAL_MODULE.read_text(encoding="utf-8"), encoding="utf-8")

    tool = ContractCheckTool()
    ctx = ToolContext(session_id="s", workspace=tmp_path)
    out = asyncio.run(tool.execute({"path": "code_agent/agent/routing.py"}, ctx))
    assert "契约检查" in out and "ρ=" in out
    assert "7/7 通过" in out, out
    assert "OBL5, OBL10" in out, "已声明的缺口必须显示出来"


def test_tool_lists_contracts_and_refuses_escape(tmp_path) -> None:
    (tmp_path / "m.CONTRACT.md").write_text("# x\n", encoding="utf-8")
    tool = ContractCheckTool()
    ctx = ToolContext(session_id="s", workspace=tmp_path)
    out = asyncio.run(tool.execute({"list_only": True}, ctx))
    assert "1 份契约" in out

    with pytest.raises(ToolError, match="越出工作区"):
        asyncio.run(tool.execute({"path": "../outside.py"}, ctx))
    with pytest.raises(ToolError):
        asyncio.run(tool.execute({}, ctx))


# ---------------------------------------------------------------------------
# R7 闸门
# ---------------------------------------------------------------------------
def test_contract_guard_denies_contract_writes_only(tmp_path) -> None:
    guard = ContractGuardPolicy()
    ctx = ToolContext(session_id="s", workspace=tmp_path)
    denied = guard.evaluate({"path": "code_agent/agent/routing.CONTRACT.md"}, ctx)
    assert denied.decision is Decision.deny
    assert "R7" in denied.reason
    # 普通实现文件不受影响（闸门只认契约后缀）
    assert guard.evaluate({"path": "code_agent/agent/routing.py"}, ctx).decision is not Decision.deny
    # 反斜杠路径也要认（Windows 上模型常写反斜杠）
    assert guard.evaluate({"path": "code_agent\\agent\\routing.CONTRACT.md"}, ctx).decision is Decision.deny


def test_evidence_guard_is_opt_in_and_narrow(tmp_path) -> None:
    ctx = ToolContext(session_id="s", workspace=tmp_path)
    loose = ContractGuardPolicy()
    assert loose.evaluate({"path": "tests/test_a.py"}, ctx).decision is not Decision.deny

    strict = ContractGuardPolicy(protect_evidence=True)
    assert strict.evaluate({"path": "tests/test_a.py"}, ctx).decision is Decision.deny
    assert strict.evaluate({"path": "tests/data.json"}, ctx).decision is not Decision.deny
    assert is_evidence_path("tests/test_a.py") is True
    assert is_evidence_path("web/src/tests/x.py") is False
    assert is_evidence_path("tests/conftest.py") is True
    assert is_evidence_path("code_agent/permissions/hooks.py") is False


def test_guard_wraps_inner_policy(tmp_path) -> None:
    """闸门要能包住原策略：非契约文件仍然走原来那一套。"""
    from code_agent.permissions.policy import ReadOnlyPolicy

    guard = ContractGuardPolicy(ReadOnlyPolicy())
    ctx = ToolContext(session_id="s", workspace=tmp_path)
    inner = guard.evaluate({"path": "a.py"}, ctx)
    assert inner.decision is Decision.allow and "只读" in inner.reason


# ---------------------------------------------------------------------------
# API：地图/节点详情靠这个端点渲染契约角标（M32）
# ---------------------------------------------------------------------------
@pytest.fixture
def client(agent_home, monkeypatch):
    for key in ("CODE_AGENT_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from fastapi.testclient import TestClient

    from code_agent.api.app import create_app

    with TestClient(create_app(provider_name="offline")) as c:
        yield c


def _login(client) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _project_with_contract(client, home) -> dict:
    """造一个"项目"：一个实现文件 + 同目录契约 + tests/ 里的证据。"""
    root = Path(home) / "proj"
    module = root / "agent"
    module.mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests" / "test_evidence.py").write_text(
        "def test_documented_behaviour():\n    pass\n", encoding="utf-8"
    )
    (module / "thing.py").write_text(
        "MAX: int = 3\n\n\ndef twice(x: int) -> int:\n    return x * 2\n", encoding="utf-8"
    )
    (module / "thing.CONTRACT.md").write_text(
        "# 契约\n\n## 1. 形状（shape）\n\n```python\nMAX: int = 3\n\ndef twice(x: int) -> int: ...\n```\n\n"
        "## 2. 义务（obligations）\n\n| id | 义务 | 证据 |\n|---|---|---|\n"
        "| OBL1 | 返回两倍 | test_documented_behaviour |\n"
        "| OBL2 | 不抛异常 | 无独立证据（未判定） |\n\n"
        "## 3. 数据（data）\n\nMAX = 3\n",
        encoding="utf-8",
    )
    from code_agent.codeintel.paths import root_key

    uid = client.get("/api/v1/auth/me", headers=_login(client)).json()["id"]
    project = client.app.state.projects.create(uid, "proj", str(root), root_key(root))
    return project


def test_contracts_endpoint_reports_one_module(client, agent_home) -> None:
    """端点的字段就是界面要渲染的那些——少一个前端就会显示空白。"""
    h = _login(client)
    project = _project_with_contract(client, agent_home)
    r = client.get(
        f"/api/v1/code/projects/{project['id']}/contracts",
        params={"path": "agent/thing.py"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_contract"] is True
    assert body["ok"] is True and body["rho"] == 1.0
    assert body["obligation_count"] == 2 and body["evidence_count"] == 1
    assert body["coverage"] == 0.5
    assert body["declared_gaps"] == ["OBL2"]
    assert body["first_unsatisfied"] == ""
    # 界面渲染 checks / detail / declared_gaps 的形状
    assert {"name", "ok", "detail"} <= set(body["checks"][0])
    assert any(c["name"].startswith("形状未被改动") for c in body["checks"])
    assert "契约检查" in body["summary"]


def test_contracts_endpoint_without_contract(client, agent_home) -> None:
    h = _login(client)
    project = _project_with_contract(client, agent_home)
    body = client.get(
        f"/api/v1/code/projects/{project['id']}/contracts",
        params={"path": "agent/thing.py".replace("thing.py", "other.py")},
        headers=h,
    )
    # 文件不存在 → 404（不是"没有契约"）
    assert body.status_code == 404

    (Path(agent_home) / "proj" / "agent" / "other.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    body2 = client.get(
        f"/api/v1/code/projects/{project['id']}/contracts",
        params={"path": "agent/other.py"},
        headers=h,
    ).json()
    assert body2["has_contract"] is False and body2["total"] == 1
    assert body2["first_unsatisfied"] == "契约存在"


def test_contracts_endpoint_lists_project_contracts_and_guards_paths(client, agent_home) -> None:
    h = _login(client)
    project = _project_with_contract(client, agent_home)

    listing = client.get(f"/api/v1/code/projects/{project['id']}/contracts", headers=h).json()
    assert listing["count"] == 1
    assert listing["items"][0]["module"] == "agent/thing.py"
    assert listing["items"][0]["coverage"] == 0.5

    escape = client.get(
        f"/api/v1/code/projects/{project['id']}/contracts",
        params={"path": "../../../etc/passwd"},
        headers=h,
    )
    assert escape.status_code == 422, escape.text


def test_contracts_endpoint_accepts_a_directory(client, agent_home) -> None:
    """**目录也要能查**（地图上的模块节点传进来的就是目录）。

    真机踩到：这里原来直接 404，而前端把 `/api/v1/code/` 下的 404 一律说成
    "后端是旧版本，请重启" → 用户点一个模块节点就被引去重启服务，重启也没用。
    现在的语义是"这个模块下所有实现的契约汇总"，顺带就是第一版漂移审计视图。
    """
    h = _login(client)
    project = _project_with_contract(client, agent_home)

    body = client.get(
        f"/api/v1/code/projects/{project['id']}/contracts",
        params={"path": "agent"},
        headers=h,
    )
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["is_dir"] is True and data["dir"] == "agent"
    assert data["count"] == 1 and data["items"][0]["module"] == "agent/thing.py"
    assert data["python_files"] == 1
    assert "1/1 个实现有契约" in data["summary"]

    empty = client.get(
        f"/api/v1/code/projects/{project['id']}/contracts",
        params={"path": "tests"},
        headers=h,
    ).json()
    assert empty["count"] == 0 and empty["has_contract"] is False
    assert "没有契约" in empty["summary"]


def test_resource_level_404_is_not_a_route_level_404(client, agent_home) -> None:
    """给前端的约定：**资源不存在**的 404 必须带具体 detail，不能是路由级的 `Not Found`。

    前端的错误提示按这个区分：`detail == "Not Found"` 才是"后端是旧版本"，
    其余 404 一律显示后端的原话。这条断言就是那个约定的机器化版本。
    """
    h = _login(client)
    project = _project_with_contract(client, agent_home)

    missing = client.get(
        f"/api/v1/code/projects/{project['id']}/contracts",
        params={"path": "agent/nope.py"},
        headers=h,
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] != "Not Found"
    assert "文件不存在" in missing.json()["detail"]

    # 未知路由（模拟旧版本后端）才是路由级的 Not Found —— 前端靠它说"请重启"
    unknown = client.get("/api/v1/code/projects/x/definitely-not-a-route")
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Not Found"


def test_contract_layer_is_visible_to_agents_and_guarded(agent_home) -> None:
    """两条产品级不变量：工具在注册表里；契约闸门默认开。"""
    from code_agent.api.app import build_registry
    from code_agent.permissions.hooks import PermissionManager
    from code_agent import settings

    assert "contract_check" in build_registry().names()
    assert settings.contract_guard() is True, "R7 默认必须是开的（否则等于靠 prompt 自觉）"

    pm = PermissionManager(workspace_root=Path(agent_home), contract_guard=True)
    ctx = ToolContext(session_id="s", workspace=Path(agent_home))
    denied = asyncio.run(pm.evaluate("edit_file", {"path": "x.CONTRACT.md", "content": "y"}, ctx))
    assert denied.decision is Decision.deny
    allowed = asyncio.run(pm.evaluate("edit_file", {"path": "x.py", "content": "y"}, ctx))
    assert allowed.decision is Decision.allow
