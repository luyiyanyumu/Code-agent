"""M22：技能/专家（pack）与 MCP 的"启用停用 + 删除"，与工具一键禁用同一套语义。"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_agent.extensions.packs import MarkdownPack


def _skills(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / ".code-agent"))
    import os

    root = Path(os.environ["CODE_AGENT_HOME"]) / "skills"
    for name, desc in (("alpha", "甲技能"), ("beta", "乙技能")):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n正文 {name}\n", encoding="utf-8"
        )
    return root


# ---------------------------------------------------------------------------
# 技能/专家：停用 = 不进目录 + load 不到；但**仍然列出来**（否则没法再启用回来）
# ---------------------------------------------------------------------------
def test_pack_disable_semantics(tmp_path, monkeypatch):
    root = _skills(tmp_path, monkeypatch)
    pack = MarkdownPack(root, "SKILL.md", "skill")

    assert pack.names() == ["alpha", "beta"]
    assert "alpha" in pack.catalog_text()

    pack.set_disabled({"alpha"})
    # 1) 目录里没有它（system prompt 不会提）
    assert "alpha" not in pack.catalog_text()
    # 2) load 读不到（fail closed：模型就算猜名字也没用）
    assert pack.load("alpha") is None
    assert pack.load("beta")
    # 3) 列表里仍在，带 enabled=false —— 页面要能再启用回来
    items = {i["name"]: i for i in pack.list()}
    assert items["alpha"]["enabled"] is False and items["beta"]["enabled"] is True
    assert [i["name"] for i in pack.list(include_disabled=False)] == ["beta"]
    # 4) names() 是"可用"口径
    assert pack.names() == ["beta"]

    pack.set_disabled(set())
    assert pack.names() == ["alpha", "beta"] and pack.load("alpha")


def test_pack_disable_accepts_dir_name(tmp_path, monkeypatch):
    """停用清单里记的可能是目录名而不是 frontmatter 的 name（两者常不一样）。"""
    root = _skills(tmp_path, monkeypatch)
    d = root / "dir-name"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: real-name\n---\n正文\n", encoding="utf-8")
    pack = MarkdownPack(root, "SKILL.md", "skill")
    pack.set_disabled({"dir-name"})
    assert pack.load("real-name") is None
    assert "real-name" not in pack.catalog_text()


def test_delete_pack_moves_to_trash(agent_home, monkeypatch):
    """删除 = 移进 .trash（可找回），不是真删。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    from fastapi.testclient import TestClient

    from code_agent.api.app import create_app
    from code_agent.config import skills_dir

    d = skills_dir() / "temp-skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: temp-skill\n---\n正文\n", encoding="utf-8")

    client = TestClient(create_app())
    tok = _login(client)
    h = {"Authorization": f"Bearer {tok}"}

    r = client.get("/api/v1/tools", headers=h)
    assert r.status_code == 200
    assert any(p["name"] == "temp-skill" for p in r.json()["packs"])

    r = client.delete("/api/v1/packs/skill/temp-skill", headers=h)
    assert r.status_code == 200, r.text
    moved = Path(r.json()["moved_to"])
    assert not d.exists() and moved.is_dir() and (moved / "SKILL.md").is_file()
    # 回收站不会被当成技能扫出来
    assert "temp-skill" not in client.get("/api/v1/tools", headers=h).json().get("packs", [])[0:1] or [""]
    names = [p["name"] for p in client.get("/api/v1/tools", headers=h).json()["packs"]]
    assert "temp-skill" not in names


def _login(client) -> str:
    r = client.post("/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin123"})
    if r.status_code >= 400:
        r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["token"]


def test_api_disable_then_reenable(agent_home, monkeypatch):
    """停用 → 再启用必须成功。

    真机踩到：存在性判断以前用 names()（只有启用的），停用后再启用会 404，
    也就是"关得掉、开不回来"。
    """
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    from fastapi.testclient import TestClient

    from code_agent.api.app import create_app
    from code_agent.config import skills_dir

    d = skills_dir() / "toggle-skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: toggle-skill\ndescription: 演示\n---\n正文\n", encoding="utf-8")

    client = TestClient(create_app())
    h = {"Authorization": f"Bearer {_login(client)}"}

    r = client.post("/api/v1/packs/skill/toggle-skill/enabled", json={"enabled": False}, headers=h)
    assert r.status_code == 200, r.text
    assert "skill:toggle-skill" in r.json()["disabled_packs"]

    r = client.post("/api/v1/packs/skill/toggle-skill/enabled", json={"enabled": True}, headers=h)
    assert r.status_code == 200, r.text
    assert "skill:toggle-skill" not in r.json()["disabled_packs"]

    # 不存在的名字仍然要 404（别把"拼错"当成"成功"）
    r = client.post("/api/v1/packs/skill/nope/enabled", json={"enabled": False}, headers=h)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# MCP：停用不启动子进程、也不算"连接失败"；配置里落 enabled=false
# ---------------------------------------------------------------------------
def test_mcp_enabled_flag_roundtrip(tmp_path):
    from code_agent.extensions.authoring import normalize_mcp_servers

    clean = normalize_mcp_servers({"srv": {"command": "python", "args": ["-m", "x"], "enabled": False}})
    assert clean["srv"]["enabled"] is False
    # 不写 enabled 时保持默认（视为启用），避免旧配置被改写
    assert "enabled" not in normalize_mcp_servers({"srv": {"command": "python"}})["srv"]


def test_mcp_disabled_server_is_skipped(tmp_path):
    import asyncio
    import json

    from code_agent.extensions.mcp_client import McpManager

    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"off": {"command": "definitely-not-a-real-binary", "enabled": False}}}),
        encoding="utf-8",
    )
    mgr = McpManager(cfg)
    tools = asyncio.run(mgr.connect_all())
    assert tools == []
    # 停用 ≠ 失败：不该在 UI 上显示成"连不上"
    assert mgr.failures == {}
    assert mgr.disabled == {"off"}
    item = mgr.catalog()[0]
    assert item["enabled"] is False and item["connected"] is False


def test_mcp_api_toggle_and_delete(agent_home, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    from fastapi.testclient import TestClient

    from code_agent.api.app import create_app
    from code_agent.config import mcp_config_path
    from code_agent.extensions.authoring import save_mcp_config

    save_mcp_config(mcp_config_path(), {"demo": {"command": "python", "args": ["-m", "demo"]}})
    client = TestClient(create_app())
    h = {"Authorization": f"Bearer {_login(client)}"}

    r = client.post("/api/v1/mcp/demo/enabled", json={"enabled": False}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False and r.json()["restart_required"] is True
    # 落盘了才算数
    from code_agent.extensions.authoring import load_mcp_config

    assert load_mcp_config(mcp_config_path())["demo"]["enabled"] is False

    r = client.delete("/api/v1/mcp/demo", headers=h)
    assert r.status_code == 200, r.text
    assert "demo" not in load_mcp_config(mcp_config_path())

    # 不存在 / 拼错：要 404，不能静默成功
    assert client.delete("/api/v1/mcp/nope", headers=h).status_code == 404
