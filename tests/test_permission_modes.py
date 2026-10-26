"""三档权限模式（read-only / workspace-write / full-access）+ 侧栏工作区分组。

档位的语义边界是这个功能的全部价值所在，所以逐个档位断言到 decision 层：
- read-only：写工具、bash、未知工具一律 deny（保守 fail closed）
- workspace-write：走原有策略（越界写 = 审批）
- full-access：连黑名单（sudo）都放行——用户显式选择了免审批档

分组（folder）只是会话元数据的搬运，但必须验证「落库 → 列表可见 → 前端可读」，
以及非法档位在 API 层被 422 挡住（不能靠前端下拉自觉）。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from code_agent.permissions.hooks import PERMISSION_LABELS, PERMISSION_MODES, PermissionManager
from code_agent.permissions.policy import Decision, ReadOnlyPolicy
from code_agent.tools.base import ToolContext


def _ctx(workspace) -> ToolContext:
    return ToolContext(session_id="s_test", workspace=workspace, user_id="u1")


# ----------------------------------------------------------------------
# 档位常量表
# ----------------------------------------------------------------------
def test_permission_modes_and_labels_are_consistent() -> None:
    """每个档位都必须有展示文案——否则前端下拉会出现空白项。"""
    assert PERMISSION_MODES == ("read-only", "workspace-write", "full-access")
    assert set(PERMISSION_LABELS) == set(PERMISSION_MODES)
    for label, desc in PERMISSION_LABELS.values():
        assert label and desc


def test_unknown_mode_falls_back_to_workspace_write(tmp_path) -> None:
    """构造器收到脏值不炸，收敛到最安全可用的档位。"""
    pm = PermissionManager(workspace_root=tmp_path, mode="yolo")
    assert pm.mode == "workspace-write"


# ----------------------------------------------------------------------
# read-only
# ----------------------------------------------------------------------
def test_read_only_denies_write_and_bash(tmp_path) -> None:
    pm = PermissionManager(workspace_root=tmp_path, mode="read-only")
    for tool, args in (
        ("write_file", {"path": "a.txt", "content": "x"}),
        ("bash", {"command": "echo hi"}),
        ("delete_everything", {}),          # 未知工具也不能漏
    ):
        res = asyncio.run(pm.evaluate(tool, args, _ctx(tmp_path)))
        assert res.decision is Decision.deny, f"{tool} 在只读档下必须被拒"
        assert "只读" in res.reason


def test_read_only_allows_read_tools(tmp_path) -> None:
    """只读档下，只读类工具必须**真的放行**（allow），而不只是"没被拒"。

    这条断言以前是 `is not Decision.deny`——于是 `need_approval` 也能过。而白名单里
    那批"没有内置策略"的工具（当时写作 knowledge_search）恰好全都落到 DefaultPolicy
    = need_approval：只读会话里点一次"列技能/查知识库"就要人工审批，而**无人值守时
    直接失败**。测试写着"放行"，运行时却在等人——断言太弱，谎就藏住了。

    现在白名单从 `BUILTIN_TOOL_POLICIES` 派生（`knowledge_search` 那个幽灵名字已
    改成真实工具 `kb_search`），所以这里可以断言到 allow。
    """
    pm = PermissionManager(workspace_root=tmp_path, mode="read-only")
    assert isinstance(pm._policies["read_file"], ReadOnlyPolicy)
    for tool in ("read_file", "kb_search", "web_search", "skill_list", "skill_search"):
        res = asyncio.run(pm.evaluate(tool, {"path": "a.txt"}, _ctx(tmp_path)))
        assert res.decision is Decision.allow, f"{tool} 应当只读放行，实际 {res.decision}: {res.reason}"


# ----------------------------------------------------------------------
# 会话级状态工具（M29/M30）：不碰用户仓库的工具不该等人工审批
# ----------------------------------------------------------------------
def test_session_state_tools_never_wait_for_approval(tmp_path) -> None:
    """回归：任务清单 / 验收清单**不许**走"默认人工确认"。

    真机踩到：这两个工具没注册策略，落到 DefaultPolicy → workspace-write 下要人工审批、
    严格/无人值守模式下直接 `[permission denied] 人工审批未通过`。结果是
    "模型写了清单，界面什么都没有"，而且每次写都白烧一轮。
    """
    pm = PermissionManager(workspace_root=tmp_path, mode="workspace-write")
    for tool in ("todo_write", "acceptance_set", "acceptance_check"):
        res = asyncio.run(pm.evaluate(tool, {}, _ctx(tmp_path)))
        assert res.decision is Decision.allow, f"{tool} 不该需要审批：{res.reason}"


def test_read_only_keeps_todo_but_denies_checklist(tmp_path) -> None:
    """只读档的边界要说得清楚：

    - `todo_write` 是**纯函数**（只把整份清单当参数，loop 自己落轨迹）→ 放行；
    - `acceptance_set` 会写平台自己的文件（`~/.code-agent/acceptance/...`）→ 只读档拒绝，
      因为"只读 = 不产生任何写入"这条语义不为例外开口子。
    """
    pm = PermissionManager(workspace_root=tmp_path, mode="read-only")
    assert asyncio.run(pm.evaluate("todo_write", {}, _ctx(tmp_path))).decision is not Decision.deny
    for tool in ("acceptance_set", "acceptance_check"):
        res = asyncio.run(pm.evaluate(tool, {}, _ctx(tmp_path)))
        assert res.decision is Decision.deny, f"{tool} 在只读档下应被拒"


def test_session_state_policy_does_not_open_the_repo_boundary(tmp_path) -> None:
    """放行会话状态工具**不能**顺带放宽真正碰用户仓库的操作（边界不能被稀释）。"""
    pm = PermissionManager(workspace_root=tmp_path, mode="workspace-write")
    outside = asyncio.run(
        pm.evaluate(
            "write_file",
            {"path": str(tmp_path.parent / "escape.txt"), "content": "x"},
            _ctx(tmp_path),
        )
    )
    assert outside.decision is Decision.deny


def test_out_of_workspace_write_does_not_wake_the_approver(tmp_path) -> None:
    """越界写不许弹审批框——因为"批了"也做不到。

    工具有一条硬边界（`_WorkspaceBound.resolve_write`，越界抛 ToolError）。以前策略层
    返回 need_approval，于是用户被叫来点一次确认，然后拿到一个错误：白打扰一次。
    现在两边口径统一为 deny，所以审批回调**一次都不该被调到**。
    """
    calls: list[str] = []

    async def approver(req):
        calls.append(req.tool_name)
        return True

    pm = PermissionManager(workspace_root=tmp_path, approver=approver, mode="workspace-write")
    res = asyncio.run(
        pm.evaluate(
            "write_file",
            {"path": str(tmp_path.parent / "escape.txt"), "content": "x"},
            _ctx(tmp_path),
        )
    )
    assert res.decision is Decision.deny
    assert "硬边界" in res.reason, res.reason
    assert calls == [], "越界写不该走审批（批准了工具也会拒）"


def test_tool_hard_boundary_and_policy_agree(tmp_path) -> None:
    """策略说"拒"，工具也真的拒——两边不能各说各话。"""
    from code_agent.tools.base import ToolError
    from code_agent.tools.builtin.tools_builtin import WriteFileTool

    with pytest.raises(ToolError):
        asyncio.run(
            WriteFileTool().execute(
                {"path": str(tmp_path.parent / "escape.txt"), "content": "x"},
                _ctx(tmp_path),
            )
        )


# ----------------------------------------------------------------------
# workspace-write（默认档）
# ----------------------------------------------------------------------
def test_workspace_write_keeps_original_policy(tmp_path) -> None:
    """默认档 = 既有行为：区内写直通、越界写**直接拒**、sudo 直接拒。

    越界从"需审批"改成"直接拒"：工具的硬边界决定了批准也写不进去，
    留着审批只是白叫人点一次（见 test_out_of_workspace_write_does_not_wake_the_approver）。
    """
    pm = PermissionManager(workspace_root=tmp_path, mode="workspace-write")
    assert pm.mode == "workspace-write"

    inside = asyncio.run(
        pm.evaluate("write_file", {"path": "ok.txt", "content": "x"}, _ctx(tmp_path))
    )
    assert inside.decision is Decision.allow

    outside = asyncio.run(
        pm.evaluate(
            "write_file",
            {"path": str(tmp_path.parent / "escape.txt"), "content": "x"},
            _ctx(tmp_path),
        )
    )
    assert outside.decision is Decision.deny

    sudo = asyncio.run(pm.evaluate("bash", {"command": "sudo rm -rf /"}, _ctx(tmp_path)))
    assert sudo.decision is Decision.deny


# ----------------------------------------------------------------------
# full-access
# ----------------------------------------------------------------------
def test_full_access_bypasses_everything(tmp_path) -> None:
    """完全权限档连 sudo 黑名单都放行——这是用户显式选择的结果。"""
    pm = PermissionManager(workspace_root=tmp_path, mode="full-access")
    for tool, args in (
        ("bash", {"command": "sudo rm -rf /"}),
        ("write_file", {"path": str(tmp_path.parent / "far.txt"), "content": "x"}),
        ("whatever", {}),
    ):
        res = asyncio.run(pm.evaluate(tool, args, _ctx(tmp_path)))
        assert res.decision is Decision.allow, f"{tool} 在完全权限档下应直通"


def test_full_access_never_asks_approver(tmp_path) -> None:
    """免审批 = 真的不会调到 approver（否则 UI 还会弹审批框）。"""
    calls: list[str] = []

    async def approver(req):
        calls.append(req.tool_name)
        return True

    pm = PermissionManager(workspace_root=tmp_path, approver=approver, mode="full-access")
    res = asyncio.run(pm.evaluate("bash", {"command": "sudo whoami"}, _ctx(tmp_path)))
    assert res.decision is Decision.allow
    assert calls == []


# ----------------------------------------------------------------------
# API 级 fixture
# ----------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / "code-agent-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("CODE_AGENT_HOME", str(home))
    for key in ("CODE_AGENT_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from code_agent.api.app import create_app

    app = create_app(provider_name="offline")
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ----------------------------------------------------------------------
# API：创建时落档位
# ----------------------------------------------------------------------
def test_create_session_records_permission_mode(client) -> None:
    h = _login(client)
    sid = client.post(
        "/api/v1/sessions", json={"permission_mode": "read-only"}, headers=h
    ).json()["id"]

    meta = client.get(f"/api/v1/sessions/{sid}", headers=h).json()
    assert meta["permission_mode"] == "read-only"


def test_runtime_built_at_create_honours_permission_mode(client) -> None:
    """回归：新建会话时**构造**出来的运行时必须带上档位。

    曾漏掉 get_runtime 里的传参，导致会话落库是 read-only、运行时却是
    workspace-write——API 读得到档位但门禁不生效，只测"读 meta"根本发现不了。
    所以这里断言的是运行时真正的 PermissionManager.mode。
    """
    h = _login(client)
    for mode in ("read-only", "full-access", "workspace-write"):
        sid = client.post(
            "/api/v1/sessions", json={"permission_mode": mode}, headers=h
        ).json()["id"]
        runtime = client.app.state.sessions._runtimes[sid]
        assert runtime.permission_mode == mode
        assert runtime._make_permissions().mode == mode


def test_create_session_defaults_from_global_setting(client) -> None:
    """不传档位 → 取全局默认；改全局默认后新会话跟着变（存量不受影响）。"""
    h = _login(client)
    s1 = client.post("/api/v1/sessions", json={}, headers=h).json()
    assert s1["permission_mode"] == "workspace-write"

    r = client.put("/api/v1/settings/permission", json={"mode": "full-access"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "full-access"

    s2 = client.post("/api/v1/sessions", json={}, headers=h).json()
    assert s2["permission_mode"] == "full-access"
    # 存量会话保持自己的档位
    assert client.get(f"/api/v1/sessions/{s1['id']}", headers=h).json()[
        "permission_mode"
    ] == "workspace-write"

    client.put("/api/v1/settings/permission", json={"mode": "workspace-write"}, headers=h)


def test_create_session_rejects_unknown_mode(client) -> None:
    h = _login(client)
    r = client.post("/api/v1/sessions", json={"permission_mode": "yolo"}, headers=h)
    assert r.status_code == 422
    assert "yolo" in r.text


# ----------------------------------------------------------------------
# 任务 / 车道会话的档位：跟随全局默认，不许硬编码
# ----------------------------------------------------------------------
def test_task_permission_mode_follows_the_global_default(agent_home) -> None:
    """真机踩到：用户在设置里选「完全权限」，从代码页跑的任务**仍然每条命令都要审批**。

    根因是 `tasks.py` / `parallel.py` 两处把任务会话的档位**硬编码**成 workspace-write，
    用户选什么都不影响它。这里钉住"跟随全局默认"这条语义。
    """
    from code_agent import settings

    settings.save_permission_mode("full-access")
    assert settings.task_permission_mode() == "full-access"

    settings.save_permission_mode("workspace-write")
    assert settings.task_permission_mode() == "workspace-write"

    # 下限：只读档不传给执行会话——任务要在工作树里写文件，只读会让它第一步就卡死
    settings.save_permission_mode("read-only")
    assert settings.task_permission_mode() == "workspace-write"


def test_task_sessions_do_not_hardcode_a_permission_mode() -> None:
    """源码级守卫：这两处**不许**再出现写死的档位字符串。

    为什么用源码断言而不是行为断言：真正要防的回归是"以后又有人顺手写死一个档位"，
    而那在行为测试里只有跑完一整个任务才会暴露（要建工作树、起会话，很重）。
    """
    from pathlib import Path

    for rel in ("code_agent/codeintel/tasks.py", "code_agent/codeintel/parallel.py"):
        src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        assert 'permission_mode="workspace-write"' not in src, f"{rel} 又硬编码了档位"
        assert "task_permission_mode" in src, f"{rel} 没有用 task_permission_mode()"


def test_existing_task_session_mode_is_hot_pushed(agent_home, monkeypatch) -> None:
    """车道会话是复用的：设置改了之后，已存在的任务会话也要跟着改档位。"""
    import asyncio

    from code_agent.codeintel import tasks as tasks_mod

    pushed: list[tuple[str, str, str]] = []

    class FakeSessions:
        def create_task_session(self, *a, **kw):
            return {"id": "s_task"}

        def get_runtime(self, session_id, user_id):
            class RT:
                async def submit(self, prompt):
                    return type("R", (), {"text": "ok", "stop_reason": "end_turn", "turns": 1})()

            return RT()

        def set_permission_mode(self, session_id, user_id, mode):
            pushed.append((session_id, user_id, mode))

    svc = tasks_mod.TaskService.__new__(tasks_mod.TaskService)
    svc._task_operations = {}
    svc.sessions = FakeSessions()
    svc.tasks = type("T", (), {"update": staticmethod(lambda *a, **kw: None)})()
    task = {"id": "t1", "user_id": "u1", "title": "x", "worktree": str(agent_home), "session_id": "s_task"}
    asyncio.run(svc.run_agent({"id": "p1"}, task, "干活", permission_mode="full-access"))
    assert pushed == [("s_task", "u1", "full-access")], pushed


def test_permission_settings_exposes_modes(client) -> None:
    h = _login(client)
    body = client.get("/api/v1/settings/permission", headers=h).json()
    assert body["mode"] in PERMISSION_MODES
    assert [m["id"] for m in body["modes"]] == list(PERMISSION_MODES)
    assert all(m["label"] and m["desc"] for m in body["modes"])


def test_switch_session_permission_mode(client) -> None:
    """切换档位落库，并且真的换了运行时的 PermissionManager。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]

    r = client.post(f"/api/v1/sessions/{sid}/permission", json={"mode": "full-access"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["permission_mode"] == "full-access"
    assert client.get(f"/api/v1/sessions/{sid}", headers=h).json()["permission_mode"] == "full-access"

    # 运行时热更新（不重建）：下一次发消息立刻生效
    runtime = client.app.state.sessions._runtimes[sid]
    assert runtime.permission_mode == "full-access"
    assert runtime._make_permissions().mode == "full-access"

    bad = client.post(f"/api/v1/sessions/{sid}/permission", json={"mode": "god"}, headers=h)
    assert bad.status_code == 422


def test_switch_permission_mode_requires_own_session(client) -> None:
    h = _login(client)
    from code_agent.api.security import hash_password

    client.app.state.users.create("bob2", hash_password("pw123456"))
    bob = {
        "Authorization": "Bearer "
        + client.post(
            "/api/v1/auth/login", json={"username": "bob2", "password": "pw123456"}
        ).json()["token"]
    }
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    r = client.post(f"/api/v1/sessions/{sid}/permission", json={"mode": "read-only"}, headers=bob)
    assert r.status_code in (403, 404)


# ----------------------------------------------------------------------
# API：工作区分组
# ----------------------------------------------------------------------
def test_move_session_into_folder(client) -> None:
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    assert client.get(f"/api/v1/sessions/{sid}", headers=h).json()["folder"] == ""

    r = client.post(f"/api/v1/sessions/{sid}/folder", json={"folder": "支付重构"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["folder"] == "支付重构"

    listed = {s["id"]: s for s in client.get("/api/v1/sessions", headers=h).json()}
    assert listed[sid]["folder"] == "支付重构"

    # 空串 = 移回默认工作区
    back = client.post(f"/api/v1/sessions/{sid}/folder", json={"folder": ""}, headers=h)
    assert back.json()["folder"] == ""


def test_create_session_with_folder(client) -> None:
    h = _login(client)
    meta = client.post("/api/v1/sessions", json={"folder": "  数据平台  "}, headers=h).json()
    # 前后空白要去掉，否则侧栏会分出「数据平台」和「  数据平台  」两组
    assert meta["folder"] == "数据平台"


def test_move_folder_requires_own_session(client) -> None:
    h = _login(client)
    from code_agent.api.security import hash_password

    client.app.state.users.create("bob3", hash_password("pw123456"))
    bob = {
        "Authorization": "Bearer "
        + client.post(
            "/api/v1/auth/login", json={"username": "bob3", "password": "pw123456"}
        ).json()["token"]
    }
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    assert (
        client.post(f"/api/v1/sessions/{sid}/folder", json={"folder": "x"}, headers=bob).status_code
        in (403, 404)
    )


def test_old_db_gets_permission_mode_column(tmp_path, monkeypatch) -> None:
    """半迁移防回归：老库已有 folder 列时，permission_mode 仍必须被补上。"""
    import sqlite3

    from code_agent.storage.db import Database

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL DEFAULT 'active', model_tier TEXT NOT NULL DEFAULT 'default',"
        " folder TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT)"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    cols = {r["name"] for r in db.query_all("PRAGMA table_info(sessions)")}
    assert "permission_mode" in cols
    assert "folder" in cols
    # M36/M40：迁移顺手把老库的档位列删掉（列在这里正好是"旧库"的样子），
    # 会话级模型也一并去掉——模型现在由"当前生效的配置档案"决定
    assert "model_tier" not in cols
    assert "model" not in cols


# ----------------------------------------------------------------------
# API：工作区（新建 / 重命名 / 删除 / 空工作区可见）
# ----------------------------------------------------------------------
def test_create_empty_workspace_is_listed(client) -> None:
    """空工作区必须出现在列表里——否则用户点了「新建工作区」看不到任何反馈。"""
    h = _login(client)
    assert client.get("/api/v1/workspaces", headers=h).json() == []

    r = client.post("/api/v1/workspaces", json={"name": "支付重构"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "支付重构"

    ws = client.get("/api/v1/workspaces", headers=h).json()
    assert [w["name"] for w in ws] == ["支付重构"]
    assert ws[0]["count"] == 0


def test_create_workspace_rejects_duplicate_and_reserved(client) -> None:
    h = _login(client)
    client.post("/api/v1/workspaces", json={"name": "数据平台"}, headers=h)

    dup = client.post("/api/v1/workspaces", json={"name": "数据平台"}, headers=h)
    assert dup.status_code == 422 and "已存在" in dup.text

    reserved = client.post("/api/v1/workspaces", json={"name": "默认工作区"}, headers=h)
    assert reserved.status_code == 422 and "保留" in reserved.text

    assert client.post("/api/v1/workspaces", json={"name": "   "}, headers=h).status_code == 422
    assert client.post("/api/v1/workspaces", json={"name": "a/b"}, headers=h).status_code == 422


def test_move_session_auto_registers_workspace(client) -> None:
    """移到工作区时自动登记——用户不必先建分组再搬会话。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={}, headers=h).json()["id"]
    client.post(f"/api/v1/sessions/{sid}/folder", json={"folder": "临时实验"}, headers=h)

    ws = {w["name"]: w for w in client.get("/api/v1/workspaces", headers=h).json()}
    assert ws["临时实验"]["count"] == 1


def test_workspace_count_and_listing_order(client) -> None:
    h = _login(client)
    a = client.post("/api/v1/sessions", json={"folder": "A"}, headers=h).json()["id"]
    client.post("/api/v1/sessions", json={"folder": "A"}, headers=h)
    client.post("/api/v1/sessions", json={"folder": "B"}, headers=h)

    ws = {w["name"]: w for w in client.get("/api/v1/workspaces", headers=h).json()}
    assert ws["A"]["count"] == 2
    assert ws["B"]["count"] == 1
    # 默认工作区里的会话不产生工作区条目
    client.post("/api/v1/sessions", json={}, headers=h)
    assert "" not in ws

    # 搬走一个后计数跟着变，工作区本身仍在（空分组也允许存在）
    client.post(f"/api/v1/sessions/{a}/folder", json={"folder": ""}, headers=h)
    ws2 = {w["name"]: w for w in client.get("/api/v1/workspaces", headers=h).json()}
    assert ws2["A"]["count"] == 1


def test_rename_workspace_keeps_sessions_in_sync(client) -> None:
    """重命名后会话的 folder 必须跟着改，否则会话会掉进一个不存在的分组。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={"folder": "旧名"}, headers=h).json()["id"]

    r = client.put("/api/v1/workspaces/旧名", json={"name": "新名"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "新名"

    names = [w["name"] for w in client.get("/api/v1/workspaces", headers=h).json()]
    assert names == ["新名"]
    assert client.get(f"/api/v1/sessions/{sid}", headers=h).json()["folder"] == "新名"

    assert client.put("/api/v1/workspaces/不存在", json={"name": "x"}, headers=h).status_code == 404


def test_delete_workspace_moves_sessions_back_not_deletes(client) -> None:
    """删分组 ≠ 删数据：会话退回默认工作区且仍然存在。"""
    h = _login(client)
    sid = client.post("/api/v1/sessions", json={"folder": "待删"}, headers=h).json()["id"]

    r = client.delete("/api/v1/workspaces/待删", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True

    assert client.get("/api/v1/workspaces", headers=h).json() == []
    meta = client.get(f"/api/v1/sessions/{sid}", headers=h)
    assert meta.status_code == 200
    assert meta.json()["folder"] == ""
    assert client.delete("/api/v1/workspaces/待删", headers=h).status_code == 404


def test_workspaces_are_per_user(client) -> None:
    h = _login(client)
    from code_agent.api.security import hash_password

    client.post("/api/v1/workspaces", json={"name": "私有空间"}, headers=h)

    client.app.state.users.create("bob4", hash_password("pw123456"))
    bob = {
        "Authorization": "Bearer "
        + client.post(
            "/api/v1/auth/login", json={"username": "bob4", "password": "pw123456"}
        ).json()["token"]
    }
    assert client.get("/api/v1/workspaces", headers=bob).json() == []
    # 同名工作区在别人那里可以再建一个（按 user 隔离）
    assert client.post("/api/v1/workspaces", json={"name": "私有空间"}, headers=bob).status_code == 200
