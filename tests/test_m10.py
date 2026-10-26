"""M10 测试：插件包机制、能力目录聚合、工具入口 API。

并锁住一个 M10 修掉的真 bug：Skills/Experts 目录此前从未真正进过 system prompt
（SessionManager.get_runtime 忘了把 system_suffix 传给 SessionRuntime）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.extensions import (
    ExpertsRegistry,
    PluginRegistry,
    SkillsRegistry,
    build_catalog,
    find_tool,
)
from code_agent.extensions.plugins import CODE_TOOLS_ENV, parse_manifest
from code_agent.tools.base import Tool
from code_agent.tools.registry import ToolRegistry


# ----------------------------------------------------------------------
# 造插件
# ----------------------------------------------------------------------
def make_plugin(
    home: Path, name: str, manifest_name: str | None = None, **manifest
) -> Path:
    """在 home/plugins/<name>/ 造一个插件。

    manifest_name 用来故意让清单里的 name 与目录名不一致（契约测试要用）。
    """
    d = home / "plugins" / name
    d.mkdir(parents=True, exist_ok=True)
    data = {"name": manifest_name or name, **manifest}
    (d / "plugin.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return d


def make_skill(root: Path, name: str, description: str = "desc", body: str = "正文") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8"
    )


@pytest.fixture
def plugins_root(agent_home: Path) -> Path:
    return agent_home / "plugins"


# ----------------------------------------------------------------------
# 清单解析
# ----------------------------------------------------------------------
def test_parse_full_manifest(plugins_root):
    d = make_plugin(
        plugins_root.parent,
        "p1",
        version="1.2.0",
        description="包描述",
        author="dyh",
        enabled=True,
        skills=["a", "b"],
        mcpServers={"srv": {"command": "npx", "args": ["-y", "x"], "trusted": True}},
        allowTools=["mcp__srv__q"],
        prompt="用工具",
        codeTools=False,
    )
    m = parse_manifest(d)
    assert m.name == "p1" and m.version == "1.2.0" and m.author == "dyh"
    assert m.skills == ["a", "b"]
    assert list(m.mcp_servers) == ["srv"]
    assert m.allow_tools == ["mcp__srv__q"]
    assert m.prompt == "用工具"
    assert m.usable


def test_missing_manifest_is_flagged_not_raised(plugins_root):
    d = plugins_root.parent / "plugins" / "broken"
    d.mkdir(parents=True)
    m = parse_manifest(d)
    assert m.error and "plugin.json" in m.error
    assert not m.usable


def test_invalid_json_is_flagged(plugins_root):
    d = make_plugin(plugins_root.parent, "bad")
    (d / "plugin.json").write_text("{ not json", encoding="utf-8")
    m = parse_manifest(d)
    assert "JSON" in m.error


def test_top_level_must_be_object(plugins_root):
    d = make_plugin(plugins_root.parent, "arr")
    (d / "plugin.json").write_text("[1,2,3]", encoding="utf-8")
    m = parse_manifest(d)
    assert "对象" in m.error


def test_prompt_md_fallback_and_json_priority(plugins_root):
    d = make_plugin(plugins_root.parent, "pm")
    (d / "prompt.md").write_text("来自 prompt.md", encoding="utf-8")
    assert parse_manifest(d).prompt == "来自 prompt.md"

    (d / "plugin.json").write_text(
        json.dumps({"name": "pm", "prompt": "来自 json"}), encoding="utf-8"
    )
    assert parse_manifest(d).prompt == "来自 json"


def test_mcp_entries_without_command_are_dropped(plugins_root):
    d = make_plugin(
        plugins_root.parent,
        "mcpfilter",
        mcpServers={"good": {"command": "x"}, "bad": {"args": []}, "worse": "not-a-dict"},
    )
    assert list(parse_manifest(d).mcp_servers) == ["good"]


def test_skills_accepts_string(plugins_root):
    d = make_plugin(plugins_root.parent, "s", skills="only-one")
    assert parse_manifest(d).skills == ["only-one"]


def test_directory_name_wins_on_duplicate(plugins_root):
    """两个目录声明了同一个 name：先扫到的（目录名序）占位，不互相覆盖。"""
    make_plugin(plugins_root.parent, "aaa", manifest_name="dup")
    make_plugin(plugins_root.parent, "bbb", manifest_name="dup")
    reg = PluginRegistry(plugins_root)
    assert len(reg.list()) == 1
    assert reg.get("dup").path.name == "aaa"


# ----------------------------------------------------------------------
# 启停
# ----------------------------------------------------------------------
def test_set_enabled_persists_and_keeps_other_fields(plugins_root):
    d = make_plugin(plugins_root.parent, "tog", version="9.9", allowTools=["x"])
    reg = PluginRegistry(plugins_root)
    reg.set_enabled("tog", False)

    raw = json.loads((d / "plugin.json").read_text(encoding="utf-8"))
    assert raw["enabled"] is False
    assert raw["version"] == "9.9"  # 其他字段必须保留
    assert raw["allowTools"] == ["x"]
    assert reg.get("tog").enabled is False
    assert reg.enabled() == []


def test_set_enabled_unknown_plugin(plugins_root):
    with pytest.raises(KeyError):
        PluginRegistry(plugins_root).set_enabled("ghost", True)


def test_set_enabled_refuses_broken_manifest(plugins_root):
    d = make_plugin(plugins_root.parent, "brk")
    (d / "plugin.json").write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError):
        PluginRegistry(plugins_root).set_enabled("brk", True)


# ----------------------------------------------------------------------
# 能力聚合
# ----------------------------------------------------------------------
def test_aggregations_only_include_enabled(plugins_root):
    on = make_plugin(
        plugins_root.parent,
        "on",
        allowTools=["b", "a"],
        prompt="启用中的片段",
        mcpServers={"srv-on": {"command": "x"}},
    )
    (on / "skills" / "sk-on").mkdir(parents=True)
    (on / "skills" / "sk-on" / "SKILL.md").write_text(
        "---\nname: sk-on\ndescription: d\n---\nb", encoding="utf-8"
    )
    off = make_plugin(
        plugins_root.parent,
        "off",
        enabled=False,
        allowTools=["should-not-appear"],
        prompt="不该出现",
        mcpServers={"srv-off": {"command": "x"}},
    )
    (off / "skills" / "sk-off").mkdir(parents=True)
    (off / "skills" / "sk-off" / "SKILL.md").write_text(
        "---\nname: sk-off\ndescription: d\n---\nb", encoding="utf-8"
    )

    reg = PluginRegistry(plugins_root)
    assert [m.name for m in reg.enabled()] == ["on"]
    assert reg.allow_tools() == ["a", "b"]
    assert "启用中的片段" in reg.prompt_suffix()
    assert "不该出现" not in reg.prompt_suffix()
    assert list(reg.mcp_servers()) == ["srv-on"]
    assert reg.mcp_servers()["srv-on"]["origin"] == "plugin:on"
    assert [s.root.name for s in reg.skill_sources()] == ["skills"]


def test_plugin_skill_merges_with_local_and_marks_origin(agent_home, plugins_root):
    p = make_plugin(plugins_root.parent, "pack")
    make_skill(p / "skills", "from-plugin", "插件带来的技能")
    make_skill(agent_home / "skills", "local-one", "本地技能")

    reg = PluginRegistry(plugins_root)
    skills = SkillsRegistry(agent_home / "skills", reg.skill_sources())

    assert set(skills.names()) == {"from-plugin", "local-one"}
    assert skills.origin("from-plugin") == "plugin:pack"
    assert skills.origin("local-one") == "local"
    assert "from-plugin" in skills.catalog_text()


def test_local_skill_wins_over_plugin_same_name(agent_home, plugins_root):
    p = make_plugin(plugins_root.parent, "pack")
    make_skill(p / "skills", "same", "来自插件", "插件正文")
    make_skill(agent_home / "skills", "same", "来自本地", "本地正文")

    reg = PluginRegistry(plugins_root)
    skills = SkillsRegistry(agent_home / "skills", reg.skill_sources())
    assert skills.load("same") == "本地正文"
    assert skills.origin("same") == "local"


def test_skills_allow_list_filters_plugin_skills(agent_home, plugins_root):
    p = make_plugin(plugins_root.parent, "pack", skills=["keep"])
    make_skill(p / "skills", "keep", "保留")
    make_skill(p / "skills", "drop", "过滤掉")

    reg = PluginRegistry(plugins_root)
    skills = SkillsRegistry(agent_home / "skills", reg.skill_sources())
    assert skills.names() == ["keep"]


def test_set_sources_hot_swap(agent_home, plugins_root):
    """复用同一实例热更新来源，外部持有的引用不能失效。"""
    reg = PluginRegistry(plugins_root)
    skills = SkillsRegistry(agent_home / "skills", reg.skill_sources())
    assert skills.names() == []

    p = make_plugin(plugins_root.parent, "late")
    make_skill(p / "skills", "later", "后加的")
    reg.scan()
    skills.set_sources(reg.skill_sources())
    assert skills.names() == ["later"]


# ----------------------------------------------------------------------
# 代码式工具：双重开关
# ----------------------------------------------------------------------
def _make_code_plugin(plugins_root: Path, name: str = "coded") -> Path:
    d = make_plugin(plugins_root.parent, name, codeTools=True)
    (d / "tools.py").write_text(
        "from code_agent.tools.base import Tool\n"
        "class HelloTool(Tool):\n"
        "    name = 'plugin_hello'\n"
        "    description = 'from plugin'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    async def execute(self, arguments, ctx):\n"
        "        return 'hi'\n"
        "def build_tools():\n"
        "    return [HelloTool()]\n",
        encoding="utf-8",
    )
    return d


def test_code_tools_off_by_default(plugins_root, monkeypatch):
    monkeypatch.delenv(CODE_TOOLS_ENV, raising=False)
    _make_code_plugin(plugins_root)
    reg = PluginRegistry(plugins_root)
    tools, errors = reg.load_code_tools()
    assert tools == [] and errors == []
    assert "已跳过" in reg.code_tools_status()["coded"]


def test_code_tools_need_both_switches(plugins_root, monkeypatch):
    """全局开关打开，但插件没声明 codeTools -> 仍不加载。"""
    monkeypatch.setenv(CODE_TOOLS_ENV, "1")
    make_plugin(plugins_root.parent, "nocode", skills=[])
    reg = PluginRegistry(plugins_root)
    tools, _ = reg.load_code_tools()
    assert tools == []
    assert reg.code_tools_status() == {}


def test_code_tools_loads_with_both_switches(plugins_root, monkeypatch):
    monkeypatch.setenv(CODE_TOOLS_ENV, "1")
    _make_code_plugin(plugins_root, "coded")
    reg = PluginRegistry(plugins_root)
    tools, errors = reg.load_code_tools()
    assert errors == []
    assert [t.name for t in tools] == ["plugin_hello"]
    assert tools[0].origin == "plugin:coded"  # 打上来源标记供归类
    assert reg.code_tools_status()["coded"] == "已加载"


def test_code_tools_missing_file(plugins_root, monkeypatch):
    monkeypatch.setenv(CODE_TOOLS_ENV, "1")
    make_plugin(plugins_root.parent, "nofile", codeTools=True)
    reg = PluginRegistry(plugins_root)
    assert reg.load_code_tools()[0] == []
    assert "缺少 tools.py" in reg.code_tools_status()["nofile"]


def test_code_tools_broken_module_does_not_crash(plugins_root, monkeypatch):
    monkeypatch.setenv(CODE_TOOLS_ENV, "1")
    d = make_plugin(plugins_root.parent, "boom", codeTools=True)
    (d / "tools.py").write_text("raise RuntimeError('插件炸了')", encoding="utf-8")
    reg = PluginRegistry(plugins_root)
    tools, errors = reg.load_code_tools()
    assert tools == []
    assert "插件炸了" in errors[0]


def test_code_tools_without_builder(plugins_root, monkeypatch):
    monkeypatch.setenv(CODE_TOOLS_ENV, "1")
    d = make_plugin(plugins_root.parent, "nob", codeTools=True)
    (d / "tools.py").write_text("X = 1", encoding="utf-8")
    reg = PluginRegistry(plugins_root)
    tools, errors = reg.load_code_tools()
    assert tools == [] and "build_tools" in errors[0]


# ----------------------------------------------------------------------
# 能力目录
# ----------------------------------------------------------------------
class _FakeTool(Tool):
    name = "fake"
    description = "假工具"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments, ctx):  # pragma: no cover
        return ""


def test_catalog_sources_and_policies(agent_home, plugins_root):
    make_plugin(plugins_root.parent, "pack")
    reg = ToolRegistry()

    class _Mcp(_FakeTool):
        name = "mcp__srv__quote"
        description = "[mcp:srv] 行情"

        def __init__(self):
            self.origin = "plugin:pack"
            self.raw_name = "quote"

    class _Plugin(_FakeTool):
        name = "plugin_hello"

        def __init__(self):
            self.origin = "plugin:pack"

    reg.register(_FakeTool())
    reg.register(_Mcp())
    reg.register(_Plugin())

    cat = build_catalog(
        registry=reg,
        skills=SkillsRegistry(agent_home / "skills"),
        experts=ExpertsRegistry(agent_home / "experts"),
        plugins=PluginRegistry(plugins_root),
        mcp=None,
        extra_policies={"plugin_hello": object()},
    )
    by_name = {t["name"]: t for t in cat["tools"]}
    assert by_name["fake"]["source"] == "builtin"
    assert by_name["mcp__srv__quote"]["source"] == "mcp"
    assert by_name["plugin_hello"]["source"] == "plugin"
    assert by_name["plugin_hello"]["origin"] == "plugin:pack"
    assert by_name["plugin_hello"]["policy"] == "allow"
    assert by_name["fake"]["policy"] == "approval"
    assert cat["summary"]["by_source"] == {"builtin": 1, "mcp": 1, "plugin": 1}
    assert cat["summary"]["tools_total"] == 3
    assert find_tool(cat, "fake")["name"] == "fake"
    assert find_tool(cat, "ghost") is None


def test_catalog_builtin_policy_labels(agent_home, plugins_root):
    from code_agent.tools.builtin import BashTool, ReadFileTool, WriteFileTool

    reg = ToolRegistry()
    for t in (BashTool(), ReadFileTool(), WriteFileTool()):
        reg.register(t)
    cat = build_catalog(
        registry=reg, skills=None, experts=None, plugins=None, mcp=None, extra_policies={}
    )
    by_name = {t["name"]: t for t in cat["tools"]}
    assert by_name["read_file"]["policy"] == "allow"
    assert by_name["bash"]["policy"] == "policy"
    assert by_name["write_file"]["policy"] == "policy"


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@pytest.fixture
def client(agent_home):
    with TestClient(create_app(provider_name="offline")) as c:
        yield c


@pytest.fixture
def seeded_client(agent_home):
    """先落盘插件与技能，再 create_app。

    硬约束：注册表（SkillsRegistry / PluginRegistry）在 create_app() 内部就完成扫描，
    之后再往目录里扔文件是看不见的（产品侧要靠 /plugins/reload 重扫，见下）。
    """
    make_plugin(agent_home, "pack", allowTools=["mcp__s::allow"])
    make_skill(agent_home / "skills", "demo", "演示技能")
    with TestClient(create_app(provider_name="offline")) as c:
        yield c


def _auth(client, password="admin123"):
    r = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": password}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_tools_endpoint_requires_auth(client):
    assert client.get("/api/v1/tools").status_code == 401
    assert client.get("/api/v1/plugins").status_code == 401


def test_tools_endpoint_shape(seeded_client, agent_home):
    h = _auth(seeded_client)
    cat = seeded_client.get("/api/v1/tools", headers=h).json()
    assert cat["summary"]["tools_total"] >= 3
    assert {"bash", "read_file", "write_file"} <= {t["name"] for t in cat["tools"]}
    assert any(p["name"] == "demo" for p in cat["packs"])
    assert any(p["name"] == "pack" for p in cat["plugins"])
    assert cat["code_tools"]["status"] == {}
    assert seeded_client.get("/api/v1/tools", headers=h).status_code == 200


def test_tool_detail_and_404(client):
    h = _auth(client)
    r = client.get("/api/v1/tools/bash", headers=h)
    assert r.status_code == 200
    assert r.json()["name"] == "bash"
    assert "properties" in r.json()["parameters"]
    assert client.get("/api/v1/tools/nope", headers=h).status_code == 404


def test_plugin_toggle_endpoint(agent_home):
    # 同上：插件必须先在 create_app() 之前落盘
    d = make_plugin(agent_home, "tog", allowTools=["t1"])
    make_skill(d / "skills", "tog-skill", "来自插件")

    with TestClient(create_app(provider_name="offline")) as client:
        h = _auth(client)
        assert any(
            p["name"] == "tog-skill" for p in client.get("/api/v1/tools", headers=h).json()["packs"]
        )

        r = client.post("/api/v1/plugins/tog/toggle", json={"enabled": False}, headers=h)
        assert r.status_code == 200
        assert r.json()["plugin"]["enabled"] is False
        assert not any(
            p["name"] == "tog-skill"
            for p in client.get("/api/v1/tools", headers=h).json()["packs"]
        )
        assert json.loads((d / "plugin.json").read_text(encoding="utf-8"))["enabled"] is False

        r = client.post("/api/v1/plugins/tog/toggle", json={"enabled": True}, headers=h)
        assert r.json()["plugin"]["enabled"] is True
        assert any(
            p["name"] == "tog-skill" for p in client.get("/api/v1/tools", headers=h).json()["packs"]
        )


def test_plugin_toggle_404(client):
    h = _auth(client)
    assert client.post("/api/v1/plugins/ghost/toggle", json={}, headers=h).status_code == 404


def test_plugin_reload_endpoint(client, agent_home):
    h = _auth(client)
    assert client.post("/api/v1/plugins/reload", json={}, headers=h).status_code == 200

    make_plugin(agent_home, "newbie")
    r = client.post("/api/v1/plugins/reload", json={}, headers=h).json()
    assert "newbie" in r["enabled"]
    assert "newbie" in r["added"]


def test_toggle_requires_admin(client, agent_home, monkeypatch):
    from code_agent.api import security

    make_plugin(agent_home, "pack")
    admin = _auth(client)
    # 建一个普通用户
    from code_agent.storage.db import UserRepo

    app = client.app
    UserRepo(app.state.db).create("bob", security.hash_password("bobpw"), role="user")

    r = client.post("/api/v1/auth/login", json={"username": "bob", "password": "bobpw"})
    bh = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post("/api/v1/plugins/pack/toggle", json={}, headers=bh).status_code == 403
    assert client.post("/api/v1/plugins/reload", json={}, headers=bh).status_code == 403
    # 但读取是允许的
    assert client.get("/api/v1/tools", headers=bh).status_code == 200


# ----------------------------------------------------------------------
# 回归：技能目录必须真的进 system prompt（M10 修掉的 bug）
# ----------------------------------------------------------------------
def test_skills_catalog_reaches_runtime_system_suffix(client, agent_home):
    """锁住 M4 那个静默失效：SessionManager 忘了把 system_suffix 传给 SessionRuntime。"""
    make_skill(agent_home / "skills", "report-skill", "写周报的固定流程")
    app2 = create_app(provider_name="offline")
    with TestClient(app2) as c:
        h = _auth(c)
        created = c.post("/api/v1/sessions", json={}, headers=h).json()
        manager = c.app.state.sessions
        user_id = c.app.state.users.get_by_username("admin")["id"]
        runtime = manager.get_runtime(created["id"], user_id)

        assert runtime.system_suffix, "system_suffix 不能为空——技能目录必须注入"
        assert "report-skill" in runtime.system_suffix


def test_apply_extensions_hot_updates_existing_runtime(client, agent_home):
    h = _auth(client)
    created = client.post("/api/v1/sessions", json={}, headers=h).json()
    manager = client.app.state.sessions
    user_id = client.app.state.users.get_by_username("admin")["id"]
    rt = manager.get_runtime(created["id"], user_id)
    before = rt.system_suffix

    make_skill(agent_home / "skills", "fresh-skill", "刚加的技能")
    manager.apply_extensions(system_suffix="新片段", extra_policies={})
    assert rt.system_suffix == "新片段" != before
