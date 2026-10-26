"""代码模式 P2：精确编辑工具 / 修改台账与回滚 / 上下文注入。

这一层的关键不在"能不能改"，而在四条安全与正确性约束：
1. `edit_file` 命中 0 处或 2 处都必须**拒绝**并说明原因，绝不猜位置；
2. 每次写入都留台账，且**文件之后又被改过时拒绝自动回滚**（不覆盖别人的工作）；
3. 展示文本 ≠ 发给模型的 prompt：上下文块只进后者，事件里留元数据供回放；
4. 越界写、只读档位仍然一律拒绝（新工具不能成为绕过边界的口子）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.codeintel import context as ctx_mod
from code_agent.codeintel.service import CodeIntelService
from code_agent.permissions.hooks import PermissionManager
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.code_tools import CodeMapTool, EditFileTool, GrepTool, ListDirTool
from code_agent.tools.builtin.tools_builtin import ReadFileTool


class FakeProjects:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.summaries: dict[str, dict] = {}

    def set_scan(self, project_id: str, summary_json: str) -> None:
        self.summaries[project_id] = json.loads(summary_json)

    def get(self, project_id: str) -> dict | None:
        return self.rows.get(project_id)

    def find_by_root(self, root_key: str) -> dict | None:
        for row in self.rows.values():
            if row["root_key"] == root_key:
                return row
        return None


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text('"""示例包。"""\n', encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        '"""核心。"""\n\n\nclass Engine:\n    """引擎。"""\n\n    def run(self, n: int) -> int:\n'
        "        return n * 2\n\n\ndef helper(a, b=1):\n    return a + b\n",
        encoding="utf-8",
    )
    (root / "pkg" / "util.py").write_text(
        "from .core import helper\n\n\ndef wrap(x):\n    return helper(x)\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# demo\n\n用法见 pkg/core.py\n", encoding="utf-8")
    return root


@pytest.fixture
def service(repo: Path, agent_home, monkeypatch) -> CodeIntelService:
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    fake = FakeProjects()
    row = {
        "id": "p_p2", "name": "proj", "root_path": str(repo),
        "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin",
    }
    fake.rows["p_p2"] = row
    svc = CodeIntelService(fake)
    asyncio.run(svc.scan(row, full=True))
    return svc


def _ctx(workspace: Path, session_id: str = "s_test") -> ToolContext:
    return ToolContext(session_id=session_id, workspace=workspace, user_id="u_admin")


# ---------------------------------------------------------------------------
# 定位工具
# ---------------------------------------------------------------------------
def test_list_dir_skips_ignored_dirs(repo: Path):
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("x\n", encoding="utf-8")
    result = asyncio.run(ListDirTool().execute({"path": ".", "depth": 2}, _ctx(repo)))
    assert "pkg/" in result and "core.py" in result
    assert "node_modules" not in result


def test_grep_returns_file_line_and_respects_glob(repo: Path):
    tool = GrepTool()
    hit = asyncio.run(tool.execute({"pattern": "def helper", "glob": "*.py"}, _ctx(repo)))
    assert "pkg/core.py:11" in hit
    assert "def helper" in hit

    scoped = asyncio.run(tool.execute({"pattern": "core", "glob": "*.md"}, _ctx(repo)))
    assert "README.md:3" in scoped
    assert "core.py:11" not in scoped  # glob 生效：没有 Python 文件进结果

    empty = asyncio.run(tool.execute({"pattern": "不存在的符号"}, _ctx(repo)))
    assert "匹配 0 处" in empty

    with pytest.raises(ToolError):
        asyncio.run(tool.execute({"pattern": "([", "path": "."}, _ctx(repo)))
    with pytest.raises(ToolError):
        asyncio.run(tool.execute({"pattern": "x", "path": "../.."}, _ctx(repo)))


def test_read_file_range_is_line_numbered(repo: Path):
    result = asyncio.run(
        ReadFileTool().execute({"path": "pkg/core.py", "offset": 11, "limit": 2}, _ctx(repo))
    )
    assert "第 11-12 行" in result
    assert "11\tdef helper" in result


def test_code_search_and_graph_tools_are_read_only_and_answer_relations(service, repo: Path):
    """M34 的两个只读工具：融合检索（带 why）与按需图谱（带 文件:行）。"""
    from code_agent.tools.builtin.code_tools import CodeGraphTool, CodeSearchTool

    search = CodeSearchTool(service)
    out = json.loads(asyncio.run(search.execute({"query": "引擎"}, _ctx(repo))))
    assert out["hits"], out
    assert out["hits"][0]["why"], "每条结果都要说明为什么被召回"
    assert out["hits"][0]["meta"]["kind"] == "code"

    graph = CodeGraphTool(service)
    walked = json.loads(asyncio.run(graph.execute({"name": "helper", "direction": "in"}, _ctx(repo))))
    assert walked["start"].endswith("#helper")
    assert walked["note"], "图上没有的边要说清'不猜'"

    with pytest.raises(ToolError):
        asyncio.run(graph.execute({}, _ctx(repo)))
    with pytest.raises(ToolError):
        asyncio.run(graph.execute({"name": "不存在的符号"}, _ctx(repo)))

    # 两个工具都必须能在只读档下用（问关系/问功能都不该要写权限）
    from code_agent.permissions.hooks import ReadOnlyPolicy

    pm = PermissionManager(workspace_root=repo, mode="read-only")
    for name in ("code_search", "code_graph"):
        assert isinstance(pm._policies[name], ReadOnlyPolicy)


def test_code_map_queries_index(service, repo: Path):
    tool = CodeMapTool(service)
    overview = json.loads(asyncio.run(tool.execute({}, _ctx(repo))))
    assert overview["counts"]["symbols"] > 0
    names = {s["name"] for s in overview["symbols"]}
    assert {"Engine", "helper"} <= names

    detail = json.loads(
        asyncio.run(tool.execute({"node_id": "sym:pkg/core.py#Engine"}, _ctx(repo)))
    )
    assert detail["symbol"]["kind"] == "class"
    assert any(m["name"] == "run" for m in detail["members"])

    with pytest.raises(ToolError):
        asyncio.run(tool.execute({"node_id": "sym:pkg/core.py#Nope"}, _ctx(repo)))
    # 工作区不是已登记项目时：明确报错，而不是返回空地图
    with pytest.raises(ToolError):
        asyncio.run(CodeMapTool(service).execute({}, _ctx(repo.parent)))


def test_code_features_answers_where_a_capability_lives(service, repo: Path):
    """code 助手被问"X 功能在哪个板块"时走这条：#FIXME 用功能导航而不是结构地图。

    用户真机就问过"记忆功能在哪个板块"——结构地图按符号名搜常常搜不到，
    因为功能名用的是作者写的 docstring 首句。
    """
    from code_agent.tools.builtin.code_tools import CodeFeaturesTool

    tool = CodeFeaturesTool(service)
    overview = json.loads(asyncio.run(tool.execute({}, _ctx(repo))))
    assert overview["totals"]["features"] >= 1
    assert any(d["name"] == "pkg" for d in overview["domains"])

    # 关键词命中：功能的说明里带"引擎"，答案要落到 文件:行
    hit = json.loads(asyncio.run(tool.execute({"query": "引擎"}, _ctx(repo))))
    assert hit["matched"] >= 1
    top = hit["features"][0]
    assert top["path"] == "pkg/core.py" and top["line"] > 0
    assert top["domain"] == "pkg"

    # 展开一条功能：拿 id 再问一次，得到它的细分功能
    kids = json.loads(asyncio.run(tool.execute({"id": top["id"]}, _ctx(repo))))
    assert kids["id"] == top["id"]
    assert isinstance(kids["children"], list)

    # 搜不到时给可执行的下一步，而不是空结果
    miss = json.loads(asyncio.run(tool.execute({"query": "zzz-不存在"}, _ctx(repo))))
    assert miss["matched"] == 0 and "hint" in miss

    # 只读档下也能用（问"在哪个板块"不该要求写权限）
    policy = PermissionManager(workspace_root=repo, mode="read-only")._policies["code_features"]
    from code_agent.permissions.hooks import ReadOnlyPolicy

    assert isinstance(policy, ReadOnlyPolicy)


# ---------------------------------------------------------------------------
# 精确编辑
# ---------------------------------------------------------------------------
def test_edit_file_replaces_unique_match_and_records_patch(service, repo: Path):
    tool = EditFileTool(service)
    out = asyncio.run(
        tool.execute(
            {
                "path": "pkg/core.py",
                "old_string": "    def run(self, n: int) -> int:\n        return n * 2",
                "new_string": "    def run(self, n: int) -> int:\n        \"\"\"翻倍。\"\"\"\n        return n * 2",
            },
            _ctx(repo),
        )
    )
    assert "已修改 pkg/core.py" in out and "patch: pt_" in out
    text = (repo / "pkg" / "core.py").read_text(encoding="utf-8")
    assert '"""翻倍。"""' in text

    patches = service.list_patches(service.projects.get("p_p2"))
    assert patches["counts"]["total"] == 1
    row = patches["patches"][0]
    assert row["path"] == "pkg/core.py" and row["tool"] == "edit_file"
    assert row["before_sha1"] != row["after_sha1"]

    detail = service.patch_detail(service.projects.get("p_p2"), row["id"])
    assert "--- a/pkg/core.py" in detail["diff"]
    assert '"""翻倍。"""' in detail["diff"]


def test_edit_file_rejects_zero_and_multiple_matches(service, repo: Path):
    tool = EditFileTool(service)
    with pytest.raises(ToolError) as zero:
        asyncio.run(tool.execute({"path": "pkg/core.py", "old_string": "def nope", "new_string": "x"}, _ctx(repo)))
    assert "找不到" in str(zero.value)

    (repo / "pkg" / "dup.py").write_text("value = 1\nvalue = 1\n", encoding="utf-8")
    with pytest.raises(ToolError) as multi:
        asyncio.run(
            tool.execute({"path": "pkg/dup.py", "old_string": "value = 1", "new_string": "value = 2"}, _ctx(repo))
        )
    assert "命中 2 处" in str(multi.value)

    # replace_all 显式指定才允许批量
    asyncio.run(
        tool.execute(
            {"path": "pkg/dup.py", "old_string": "value = 1", "new_string": "value = 2", "replace_all": True},
            _ctx(repo),
        )
    )
    assert (repo / "pkg" / "dup.py").read_text(encoding="utf-8").count("value = 2") == 2


def test_edit_file_rejects_escape_and_missing_file(service, repo: Path):
    tool = EditFileTool(service)
    with pytest.raises(ToolError):
        asyncio.run(tool.execute({"path": "../outside.py", "old_string": "a", "new_string": "b"}, _ctx(repo)))
    with pytest.raises(ToolError) as missing:
        asyncio.run(tool.execute({"path": "pkg/new.py", "old_string": "a", "new_string": "b"}, _ctx(repo)))
    assert "write_file" in str(missing.value)


def test_edit_file_requires_write_permission(repo: Path):
    """只读档位下 edit_file 必须被拒——新工具不能成为绕过权限的口子。"""
    pm = PermissionManager(workspace_root=repo, mode="read-only")
    decision = asyncio.run(
        pm.evaluate("edit_file", {"path": "pkg/core.py", "old_string": "a", "new_string": "b"}, _ctx(repo))
    )
    assert decision.decision.value == "deny"

    pm_write = PermissionManager(workspace_root=repo, mode="workspace-write")
    inside = asyncio.run(
        pm_write.evaluate("edit_file", {"path": "pkg/core.py", "old_string": "a", "new_string": "b"}, _ctx(repo))
    )
    assert inside.decision.value == "allow"
    outside = asyncio.run(
        pm_write.evaluate("edit_file", {"path": "../x.py", "old_string": "a", "new_string": "b"}, _ctx(repo))
    )
    # 越界 = 拒：edit_file 与 write_file 共用 WriteFilePolicy，而工具的硬边界
    # （_WorkspaceBound.resolve_write）决定了"批准也写不进去"，所以不再给审批岔路。
    assert outside.decision.value == "deny"

    for tool in ("list_dir", "grep", "code_map"):
        assert asyncio.run(pm.evaluate(tool, {}, _ctx(repo))).decision.value == "allow"


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------
def test_revert_restores_content_and_logs_itself(service, repo: Path):
    project = service.projects.get("p_p2")
    tool = EditFileTool(service)
    original = (repo / "pkg" / "core.py").read_text(encoding="utf-8")
    asyncio.run(
        tool.execute(
            {"path": "pkg/core.py", "old_string": "    return a + b", "new_string": "    return a + b + 1"},
            _ctx(repo),
        )
    )
    assert "a + b + 1" in (repo / "pkg" / "core.py").read_text(encoding="utf-8")

    patch_id = service.list_patches(project)["patches"][0]["id"]
    result = service.revert_patch(project, patch_id)
    assert result["ok"] is True and result["reverted"] is True
    assert (repo / "pkg" / "core.py").read_text(encoding="utf-8") == original

    listing = service.list_patches(project)
    assert listing["counts"]["total"] == 2  # 原改动 + 回滚本身都留痕
    assert listing["patches"][0]["tool"] == "revert_patch"
    # 重复回滚要被拒（幂等语义说清楚，而不是静默再写一次）
    with pytest.raises(ValueError):
        service.revert_patch(project, patch_id)


def test_revert_refuses_when_file_changed_afterwards(service, repo: Path):
    """文件在改动之后又被改过 → 拒绝回滚（不覆盖别人的工作）。"""
    project = service.projects.get("p_p2")
    asyncio.run(
        EditFileTool(service).execute(
            {"path": "pkg/core.py", "old_string": "    return a + b", "new_string": "    return a + b + 1"},
            _ctx(repo),
        )
    )
    patch_id = service.list_patches(project)["patches"][0]["id"]
    # 模拟"别人又改了同一个文件"
    path = repo / "pkg" / "core.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# 人工追加\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        service.revert_patch(project, patch_id)
    assert "又被修改过" in str(exc.value)
    assert "人工追加" in path.read_text(encoding="utf-8")  # 拒绝之后文件没被动过


# ---------------------------------------------------------------------------
# 上下文注入
# ---------------------------------------------------------------------------
def test_message_context_uses_index_facts(service, repo: Path):
    project = service.projects.get("p_p2")
    block = service.message_context(
        project,
        {"path": "pkg/core.py", "start": 4, "end": 9, "symbol_id": "sym:pkg/core.py#Engine"},
    )
    assert "[代码模式｜当前上下文]" in block
    assert "pkg/core.py:4-9" in block
    assert "class Engine" in block
    assert "method run:7" in block          # 成员与行号是服务端从索引里查出来的
    assert "被引用于" in block
    assert ctx_mod.PROTOCOL.split("\n")[0] in ctx_mod.session_suffix(service.index("p_p2"), project)

    # 空上下文 / 未知符号都不该抛错（上下文块只是锦上添花）
    assert service.message_context(project, None) == ""
    assert "已不存在" in service.message_context(project, {"symbol_id": "sym:x.py#Gone"})


def test_session_context_version_changes_with_index(service, repo: Path):
    project = service.projects.get("p_p2")
    version_a, text_a = service.session_context(project)
    assert version_a > 0 and "[代码模式｜你是这个项目的 code 助手]" in text_a
    assert "pkg" in text_a  # digest 里有模块

    (repo / "pkg" / "extra.py").write_text("def added():\n    return 1\n", encoding="utf-8")
    asyncio.run(service.scan(project))
    version_b, text_b = service.session_context(project)
    assert version_b > version_a
    assert "added" in text_b and "added" not in text_a


def test_clean_code_context_whitelist():
    from code_agent.api.app import _clean_code_context

    # 注意：清洗函数收的是**请求体**（内部取 body["code_context"]），不是上下文本身
    cleaned = _clean_code_context(
        {
            "text": "改一下",
            "code_context": {
                "path": "pkg/core.py", "start": 3, "end": "8", "symbol_id": "sym:x#y",
                "test_files": ["tests/a.py"], "map_hint": "hint",
                "evil": "ignored", "absolute": "/etc/passwd",
            },
        }
    )
    assert cleaned == {
        "path": "pkg/core.py", "start": 3, "end": 8, "symbol_id": "sym:x#y",
        "test_files": ["tests/a.py"], "map_hint": "hint",
    }
    assert _clean_code_context({"code_context": {"path": "../secret"}}) is None
    assert _clean_code_context({"code_context": {"path": "/etc/passwd"}}) is None
    assert _clean_code_context({"code_context": {"path": "a/b"}}) == {"path": "a/b"}
    assert _clean_code_context({"code_context": {"nothing": 1}}) is None
    assert _clean_code_context({}) is None
    assert _clean_code_context({"code_context": "not-a-dict"}) is None


# ---------------------------------------------------------------------------
# API：台账 / 回滚 / 直改开关 / 会话权限
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


def test_patch_api_and_direct_write_gate(client, repo):
    token = _login(client)
    h = _auth(token)
    pid = client.post("/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h).json()["id"]
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=h)

    # 默认不允许直改：请求可写主会话被拒（409），而不是"看起来给了权限其实写不了"
    blocked = client.post(
        f"/api/v1/code/projects/{pid}/session", json={"permission_mode": "workspace-write"}, headers=h
    )
    assert blocked.status_code == 409
    assert "允许直接改主干" in blocked.json()["detail"]
    readonly = client.post(f"/api/v1/code/projects/{pid}/session", headers=h).json()
    assert readonly["permission_mode"] == "read-only"

    # 显式打开开关后：会话可切到可写
    opened = client.post(
        f"/api/v1/code/projects/{pid}/settings", json={"allow_direct_write": True}, headers=h
    ).json()
    assert opened["allow_direct_write"] is True
    writable = client.post(
        f"/api/v1/code/projects/{pid}/session", json={"permission_mode": "workspace-write"}, headers=h
    ).json()
    assert writable["permission_mode"] == "workspace-write"

    # 用工具直接改一处（模拟 AI 写入），再走 API 回滚
    from code_agent.tools.base import ToolContext
    from code_agent.tools.builtin.code_tools import EditFileTool

    intel = client.app.state.code_intel
    asyncio.run(
        EditFileTool(intel).execute(
            {"path": "pkg/util.py", "old_string": "    return helper(x)", "new_string": "    return helper(x) * 2"},
            ToolContext(session_id=writable["id"], workspace=repo, user_id="u_admin"),
        )
    )
    patches = client.get(f"/api/v1/code/projects/{pid}/patches", headers=h).json()
    assert patches["counts"]["total"] == 1
    patch_id = patches["patches"][0]["id"]
    detail = client.get(f"/api/v1/code/projects/{pid}/patches/{patch_id}", headers=h).json()
    assert "return helper(x)" in detail["diff"]

    reverted = client.post(
        f"/api/v1/code/projects/{pid}/patches/{patch_id}/revert", headers=h
    ).json()
    assert reverted["ok"] is True
    assert "return helper(x)" in (repo / "pkg" / "util.py").read_text(encoding="utf-8")
    assert reverted["change_seq"] > 0  # 回滚后地图已重新同步

    # 关闭开关会把主会话压回只读（否则开关只是个装饰）
    closed = client.post(
        f"/api/v1/code/projects/{pid}/settings", json={"allow_direct_write": False}, headers=h
    ).json()
    assert closed["allow_direct_write"] is False
    again = client.post(f"/api/v1/code/projects/{pid}/session", headers=h).json()
    assert again["permission_mode"] == "read-only"

    # 越权：别人的项目看不到台账
    client.app.state.users.create("mallory", __import__("code_agent.api.security", fromlist=["hash_password"]).hash_password("m123"), role="user")
    other = _login(client, "mallory", "m123")
    assert client.get(f"/api/v1/code/projects/{pid}/patches", headers=_auth(other)).status_code == 404
    assert (
        client.post(
            f"/api/v1/code/projects/{pid}/patches/{patch_id}/revert", headers=_auth(other)
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/code/projects/{pid}/settings", json={"allow_direct_write": True}, headers=_auth(other)
        ).status_code
        == 404
    )
