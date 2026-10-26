"""代码模式 P0：项目登记 / 路径安全 / 会话绑定 / 目录树与文件读取。

重点覆盖"越界"这一类：代码模式的工作区是用户的真实仓库，越界写不是回答
错误而是删别人的代码，所以边界用例必须比正常用例更多。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.api.security import hash_password
from code_agent.config import projects_root


@pytest.fixture
def app(agent_home):
    application = create_app(provider_name="offline")
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """一个"既有仓库"目录：带 gitignore 的少量文件（在 agent_home 之外）。"""
    root = tmp_path / "myrepo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("x\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00\x01\x02\x03")
    (root / "big.txt").write_text("line\n" * 120000, encoding="utf-8")  # >512KB
    return root


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _open(client, token, path):
    return client.post("/api/v1/code/projects", json={"mode": "open", "path": str(path)}, headers=_auth(token))


# ---------------------------------------------------------------------------
# 基础：打开 / 重复打开 / 列表 / 元信息
# ---------------------------------------------------------------------------
def test_open_project_and_browse(client, proj):
    token = _login(client)
    r = _open(client, token, proj)
    assert r.status_code == 200, r.text
    project = r.json()
    assert project["name"] == "myrepo"
    assert project["origin"] == "open"
    assert project["available"] is True
    pid = project["id"]

    # 列表可见
    rows = client.get("/api/v1/code/projects", headers=_auth(token)).json()
    assert [p["id"] for p in rows] == [pid]

    # 根目录树：忽略 node_modules，保留 .env（打 secret 标记）
    tree = client.get(f"/api/v1/code/projects/{pid}/tree", headers=_auth(token)).json()
    dir_names = [d["name"] for d in tree["dirs"]]
    assert "src" in dir_names and "node_modules" not in dir_names
    file_names = {f["name"]: f for f in tree["files"]}
    assert file_names[".env"]["secret"] is True
    assert file_names["README.md"]["lang"] == "markdown"

    # 懒加载子目录
    sub = client.get(
        f"/api/v1/code/projects/{pid}/tree", params={"path": "src"}, headers=_auth(token)
    ).json()
    assert sub["files"][0]["path"] == "src/main.py"

    # 读文件
    f = client.get(
        f"/api/v1/code/projects/{pid}/file", params={"path": "src/main.py"}, headers=_auth(token)
    ).json()
    assert "def main" in f["text"] and f["lang"] == "python" and f["sha1"]

    # 行区间
    part = client.get(
        f"/api/v1/code/projects/{pid}/file",
        params={"path": "src/main.py", "start": 2, "end": 2},
        headers=_auth(token),
    ).json()
    assert part["text"].strip() == "return 1" and part["range"] is True


def test_secret_flags_and_template_exception(client, proj):
    """.env 是密钥文件，.env.example 是模板——后者必须能被正常读到。"""
    token = _login(client)
    pid = _open(client, token, proj).json()["id"]
    tree = client.get(f"/api/v1/code/projects/{pid}/tree", headers=_auth(token)).json()
    flags = {f["name"]: f["secret"] for f in tree["files"]}
    assert flags[".env"] is True
    assert flags[".env.example"] is False

    detail = client.get(
        f"/api/v1/code/projects/{pid}/file", params={"path": ".env.example"}, headers=_auth(token)
    ).json()
    assert detail["secret"] is False


def test_open_same_dir_is_idempotent(client, proj):
    token = _login(client)
    first = _open(client, token, proj).json()
    second = _open(client, token, proj).json()
    assert first["id"] == second["id"]
    assert len(client.get("/api/v1/code/projects", headers=_auth(token)).json()) == 1


def test_open_same_dir_with_different_case_is_same_project(client, proj):
    """Windows 大小写不敏感：同一目录不能登记成两个项目。"""
    token = _login(client)
    first = _open(client, token, proj).json()
    second = _open(client, token, str(proj).upper()).json()
    assert first["id"] == second["id"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "      ",
        "does-not-exist-dir-xyz",
    ],
)
def test_open_rejects_bad_paths(client, bad):
    token = _login(client)
    r = client.post("/api/v1/code/projects", json={"mode": "open", "path": bad}, headers=_auth(token))
    assert r.status_code == 422


def test_open_rejects_file_not_dir(client, proj):
    token = _login(client)
    r = _open(client, token, proj / "README.md")
    assert r.status_code == 422
    assert "不是目录" in r.json()["detail"]


def test_open_rejects_agent_home(client, agent_home):
    token = _login(client)
    r = _open(client, token, agent_home)
    assert r.status_code == 422
    assert "数据目录" in r.json()["detail"]


def test_open_rejects_home_dir(client):
    token = _login(client)
    r = _open(client, token, Path.home())
    assert r.status_code == 422
    assert "主目录" in r.json()["detail"]


def test_open_rejects_filesystem_root(client):
    token = _login(client)
    root = Path.home().anchor or "/"
    r = _open(client, token, root)
    assert r.status_code == 422


def test_open_rejects_system_dir(client):
    import platform

    if platform.system() != "Windows":
        pytest.skip("系统目录用例按 Windows 语义编写")
    token = _login(client)
    r = _open(client, token, "C:\\Windows")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 越界：路径穿越 / 绝对路径
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_rel", ["../", "../../etc/passwd", "src/../../outside", "/etc/hosts", "C:\\Windows\\win.ini"])
def test_tree_and_file_reject_escape(client, proj, bad_rel):
    token = _login(client)
    pid = _open(client, token, proj).json()["id"]
    t = client.get(
        f"/api/v1/code/projects/{pid}/tree", params={"path": bad_rel}, headers=_auth(token)
    )
    f = client.get(
        f"/api/v1/code/projects/{pid}/file", params={"path": bad_rel}, headers=_auth(token)
    )
    assert t.status_code == 422, t.text
    assert f.status_code == 422, f.text


# ---------------------------------------------------------------------------
# 文件读取防护：二进制 / 超大文件
# ---------------------------------------------------------------------------
def test_file_rejects_binary(client, proj):
    token = _login(client)
    pid = _open(client, token, proj).json()["id"]
    r = client.get(
        f"/api/v1/code/projects/{pid}/file", params={"path": "binary.bin"}, headers=_auth(token)
    )
    assert r.status_code == 422
    assert "二进制" in r.json()["detail"]


def test_large_file_requires_range(client, proj):
    token = _login(client)
    pid = _open(client, token, proj).json()["id"]
    full = client.get(
        f"/api/v1/code/projects/{pid}/file", params={"path": "big.txt"}, headers=_auth(token)
    )
    assert full.status_code == 422
    assert "过大" in full.json()["detail"]

    # 带区间就能读：这是"大文件用区间看"的正确用法
    part = client.get(
        f"/api/v1/code/projects/{pid}/file",
        params={"path": "big.txt", "start": 10, "end": 12},
        headers=_auth(token),
    )
    assert part.status_code == 200, part.text
    assert part.json()["start_line"] == 10 and part.json()["end_line"] == 12


# ---------------------------------------------------------------------------
# 会话绑定：主会话是只读的，且工作区 = 项目根
# ---------------------------------------------------------------------------
def test_project_session_binds_workspace_to_project_root(app, client, proj):
    token = _login(client)
    pid = _open(client, token, proj).json()["id"]

    r = client.post(f"/api/v1/code/projects/{pid}/session", headers=_auth(token))
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["project_id"] == pid
    assert meta["tree_id"] == "main"
    assert meta["kind"] == "main"
    assert meta["permission_mode"] == "read-only"  # 主会话不给写权限
    assert meta["folder"] == "myrepo"  # 侧栏分组 = 项目名

    # 幂等：第二次取到同一个会话
    again = client.post(f"/api/v1/code/projects/{pid}/session", headers=_auth(token)).json()
    assert again["id"] == meta["id"]

    # 运行时工作区必须是项目根目录，而不是 workspaces/<sid>
    user_id = client.get("/api/v1/auth/me", headers=_auth(token)).json()["id"]
    runtime = app.state.sessions.get_runtime(meta["id"], user_id)
    assert Path(runtime.workspace).resolve() == proj.resolve()
    assert runtime.permission_mode == "read-only"

    # 项目详情里回带 session_id，刷新页面能直接接上
    detail = client.get(f"/api/v1/code/projects/{pid}", headers=_auth(token)).json()
    assert detail["session_id"] == meta["id"]


# ---------------------------------------------------------------------------
# 新建项目：模板骨架 / 平台目录下才能删文件
# ---------------------------------------------------------------------------
def test_create_project_with_template_and_delete_files(client, agent_home):
    token = _login(client)
    r = client.post(
        "/api/v1/code/projects",
        json={"mode": "create", "name": "demo-py", "template": "python", "git_init": False},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    project = r.json()
    root = Path(project["root_path"])
    assert root == projects_root() / "demo-py"
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "main.py").is_file()
    assert (root / "tests" / "test_main.py").is_file()

    # 平台自建的项目可以连目录一起删
    d = client.delete(f"/api/v1/code/projects/{project['id']}?delete_files=true", headers=_auth(token))
    assert d.status_code == 200 and d.json()["removed_dir"] is True
    assert not root.exists()
    assert client.get("/api/v1/code/projects", headers=_auth(token)).json() == []


def test_delete_git_project_removes_readonly_objects(client, agent_home):
    """带 git 仓库的项目要能删干净：git 的 object 文件是只读的。

    真机踩过：`shutil.rmtree(ignore_errors=True)` 静默跳过只读文件，项目注销后
    磁盘上还留着 `.git/objects/*` 空壳。
    """
    token = _login(client)
    r = client.post(
        "/api/v1/code/projects",
        json={"mode": "create", "name": "demo-git", "template": "python", "git_init": True},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    project = r.json()
    root = Path(project["root_path"])
    assert (root / ".git").is_dir()
    assert any((root / ".git" / "objects").rglob("*")), "初始提交没产生 object"

    d = client.delete(f"/api/v1/code/projects/{project['id']}?delete_files=true", headers=_auth(token))
    assert d.status_code == 200, d.text
    assert d.json()["removed_dir"] is True
    assert not root.exists(), f"残留：{[p.name for p in root.rglob('*')][:5]}"


def test_create_project_rejects_bad_name(client):
    token = _login(client)
    for bad in ["", "a/b", "..", "x" * 80]:
        r = client.post(
            "/api/v1/code/projects",
            json={"mode": "create", "name": bad, "template": "empty"},
            headers=_auth(token),
        )
        assert r.status_code == 422, bad


def test_delete_open_project_keeps_files(client, proj):
    """打开的项目只能"移除"，文件绝不能动。"""
    token = _login(client)
    pid = _open(client, token, proj).json()["id"]
    d = client.delete(f"/api/v1/code/projects/{pid}?delete_files=true", headers=_auth(token))
    assert d.status_code == 422
    assert "只能「移除」" in d.json()["detail"]

    ok = client.delete(f"/api/v1/code/projects/{pid}", headers=_auth(token))
    assert ok.status_code == 200 and ok.json()["removed_dir"] is False
    assert (proj / "src" / "main.py").is_file()  # 文件还在


# ---------------------------------------------------------------------------
# 归属隔离：别人的项目看不到也打不开
# ---------------------------------------------------------------------------
def test_project_is_private_to_owner(app, client, proj):
    admin_token = _login(client)
    pid = _open(client, admin_token, proj).json()["id"]

    app.state.users.create("bob", hash_password("bob123"), role="user")
    bob_token = _login(client, "bob", "bob123")

    assert client.get("/api/v1/code/projects", headers=_auth(bob_token)).json() == []
    assert client.get(f"/api/v1/code/projects/{pid}", headers=_auth(bob_token)).status_code == 404
    assert (
        client.get(f"/api/v1/code/projects/{pid}/tree", headers=_auth(bob_token)).status_code == 404
    )
    assert (
        client.post(f"/api/v1/code/projects/{pid}/session", headers=_auth(bob_token)).status_code
        == 404
    )
    assert client.delete(f"/api/v1/code/projects/{pid}", headers=_auth(bob_token)).status_code == 404
    # 管理员自己仍然正常
    assert client.get(f"/api/v1/code/projects/{pid}", headers=_auth(admin_token)).status_code == 200


def test_code_api_requires_auth(client):
    assert client.get("/api/v1/code/projects").status_code == 401
    assert client.get("/api/v1/code/meta").status_code == 401
    assert client.get("/api/v1/code/fs/list").status_code == 401


# ---------------------------------------------------------------------------
# 目录选择器
# ---------------------------------------------------------------------------
def test_fs_list_lists_dirs_only(client, proj):
    token = _login(client)
    r = client.get("/api/v1/code/fs/list", params={"path": str(proj)}, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    names = [d["name"] for d in body["dirs"]]
    assert "src" in names and "node_modules" not in names
    assert str(proj) == body["path"]
    assert body["suggested_name"] == "myrepo"


def test_fs_list_can_be_disabled(client, proj, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_CODE_BROWSE", "0")
    token = _login(client)
    r = client.get("/api/v1/code/fs/list", params={"path": str(proj)}, headers=_auth(token))
    assert r.status_code == 422
    assert "已关闭" in r.json()["detail"]


def test_fs_list_offers_drives_and_quick_locations(client, proj):
    """选择器必须让人能选到自己的仓库，而不是只能在一个 C:\\ 里转。

    这条是用户反馈驱动的：Windows 上必须给出**盘符**，否则换盘只能手敲路径；
    主目录与平台工程目录也要能一键到达。
    """
    token = _login(client)
    body = client.get("/api/v1/code/fs/list", headers=_auth(token)).json()
    assert body["roots"], "没有给出任何根位置"
    assert all({"name", "path", "kind"} <= set(r) for r in body["roots"])
    kinds = {q["kind"] for q in body["quick"]}
    assert "home" in kinds and "projects" in kinds
    # 默认起点不该恰好落在盘符根（那是最没用的位置）
    assert Path(body["path"]) != Path(body["path"]).anchor


def test_fs_list_quick_includes_recent_projects(client, proj):
    token = _login(client)
    opened = _open(client, token, proj)
    assert opened.status_code == 200, opened.text
    body = client.get("/api/v1/code/fs/list", headers=_auth(token)).json()
    recent = [q for q in body["quick"] if q["kind"] == "recent"]
    assert recent, "最近打开的项目没有出现在快捷位置里"
    assert any(str(proj) == str(Path(q["path"])) for q in recent)


def test_fs_list_hides_system_and_hidden_dirs(client, tmp_path):
    """`$Recycle.Bin` 这类目录在选择器里只有噪音，默认不列；可显式打开。"""
    root = tmp_path / "browse-root"
    (root / "workspace-a").mkdir(parents=True)
    (root / "$Recycle.Bin").mkdir()
    (root / ".hidden").mkdir()
    token = _login(client)

    body = client.get("/api/v1/code/fs/list", params={"path": str(root)}, headers=_auth(token)).json()
    names = [d["name"] for d in body["dirs"]]
    assert names == ["workspace-a"]
    assert body["hidden_count"] >= 1 and body["show_hidden"] is False

    shown = client.get(
        "/api/v1/code/fs/list", params={"path": str(root), "hidden": 1}, headers=_auth(token)
    ).json()
    shown_names = [d["name"] for d in shown["dirs"]]
    assert "$Recycle.Bin" in shown_names and shown["show_hidden"] is True


def test_fs_list_returns_native_dialog_fields(client, proj):
    """对话框要画得出来：面包屑、快速访问、每行的修改时间与"是不是工程"。"""
    token = _login(client)
    body = client.get("/api/v1/code/fs/list", params={"path": str(proj)}, headers=_auth(token)).json()

    # 面包屑从盘符/根开始，最后一段就是当前目录
    crumbs = body["breadcrumbs"]
    assert crumbs and crumbs[-1]["path"] == str(proj)
    assert all({"name", "path"} <= set(c) for c in crumbs)

    # 快速访问：主目录与平台工程目录必须在（标准文件夹存在才给，所以不硬编码桌面/下载）
    places = {p["kind"] for p in body["places"]}
    assert "home" in places and "projects" in places
    assert all({"name", "path", "kind"} <= set(p) for p in body["places"])

    # 每行都带时间戳；src 目录不是工程，但目录本身是工程（有 .gitignore 不算，用 pyproject 试）
    src = next(d for d in body["dirs"] if d["name"] == "src")
    assert isinstance(src["mtime"], (int, float)) and src["mtime"] > 0
    assert src["is_project"] is False
    (Path(proj) / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    again = client.get(
        "/api/v1/code/fs/list", params={"path": str(proj)}, headers=_auth(token)
    ).json()
    assert again["is_project"] is True


def test_browse_default_never_lands_on_drive_root(tmp_path):
    """主目录恰好是盘符根时，起点要落到平台工程目录（而不是 C:\\ 这种地方）。"""
    from code_agent.codeintel.projects import _default_browse_path, _drives

    drive_root = Path(Path.home().anchor or "C:/")
    projects = tmp_path / "projects"
    projects.mkdir()
    picked = _default_browse_path(drive_root, projects, _drives())
    assert picked == projects


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------
def test_code_meta(client):
    token = _login(client)
    body = client.get("/api/v1/code/meta", headers=_auth(token)).json()
    assert body["projects_root"] == str(projects_root())
    assert set(body["templates"]) == {"empty", "python", "node"}
    # 能力位如实上报：报的是"服务是否真的接上了"，不是阶段编号
    assert body["features"]["map"] is True
    assert body["features"]["lsp"] is True
    assert body["features"]["git_write"] is True    # 测试环境的 git 是必备依赖
    assert body["features"]["tasks"] is True
    assert body["features"]["parallel"] is True
