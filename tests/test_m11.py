"""M11 测试：能力制作与上传——插件 zip / 技能 / MCP 配置读写。

安全用例是重点：zip-slip、越界删除、坏配置整份不落盘、admin-only。
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.extensions import (
    AuthoringError,
    extract_plugin_zip,
    extract_skill_zip,
    load_mcp_config,
    normalize_mcp_servers,
    save_mcp_config,
    write_skill_file,
)
from code_agent.extensions.authoring import NAME_RE


# ======================================================================
# 造 zip
# ======================================================================
def make_zip(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()


MANIFEST = json.dumps(
    {"name": "up-pack", "version": "1.0", "description": "上传测试", "prompt": "先给结论"},
    ensure_ascii=False,
)


# ======================================================================
# 命名规则
# ======================================================================
@pytest.mark.parametrize(
    "ok_name", ["a", "fin-pack", "Weekly_Report.2", "x" * 64, "9lives"]
)
def test_name_regex_accepts(ok_name):
    assert NAME_RE.match(ok_name)


@pytest.mark.parametrize(
    "bad_name", ["", "-lead", ".hide", "a/b", "a b", "名字", "x" * 65, ".."]
)
def test_name_regex_rejects(bad_name):
    assert not NAME_RE.match(bad_name)


# ======================================================================
# 插件 zip 解包
# ======================================================================
def test_extract_happy_path(tmp_path):
    data = make_zip(
        {
            "plugin.json": MANIFEST,
            "skills/demo/SKILL.md": "---\nname: demo\ndescription: d\n---\nb",
            "prompt.md": "来自 prompt.md",
        }
    )
    result = extract_plugin_zip(data, tmp_path / "plugins", zip_name="whatever.zip")
    assert result["name"] == "up-pack"  # 清单 name 优先于 zip 文件名
    assert result["files"] == 3
    assert (tmp_path / "plugins" / "up-pack" / "plugin.json").is_file()
    assert (tmp_path / "plugins" / "up-pack" / "skills" / "demo" / "SKILL.md").is_file()


def test_extract_strips_common_top_dir(tmp_path):
    data = make_zip(
        {
            "fin-pack-main/plugin.json": MANIFEST,
            "fin-pack-main/skills/demo/SKILL.md": "---\nname: demo\ndescription: d\n---\nb",
        }
    )
    result = extract_plugin_zip(data, tmp_path / "plugins")
    assert result["name"] == "up-pack"
    assert (tmp_path / "plugins" / "up-pack" / "skills" / "demo" / "SKILL.md").is_file()


def test_extract_missing_manifest(tmp_path):
    with pytest.raises(AuthoringError, match="plugin.json"):
        extract_plugin_zip(make_zip({"README.md": "x"}), tmp_path)


def test_extract_rejects_unexpected_top_level(tmp_path):
    data = make_zip({"plugin.json": MANIFEST, "run.sh": "rm -rf /"})
    with pytest.raises(AuthoringError, match="run.sh|顶层"):
        extract_plugin_zip(data, tmp_path)


def test_extract_blocks_zip_slip(tmp_path):
    data = make_zip({"plugin.json": MANIFEST, "../outside.txt": "越界"})
    with pytest.raises(AuthoringError, match="非法路径|越界"):
        extract_plugin_zip(data, tmp_path)
    assert not (tmp_path / "outside.txt").exists()


def test_extract_rejects_duplicate_without_overwrite(tmp_path):
    d = tmp_path / "plugins" / "up-pack"
    d.mkdir(parents=True)
    (d / "keep.txt").write_text("旧内容", encoding="utf-8")
    data = make_zip({"plugin.json": MANIFEST})
    with pytest.raises(AuthoringError, match="已存在"):
        extract_plugin_zip(data, tmp_path / "plugins")
    assert (d / "keep.txt").is_file()  # 失败不留半成品


def test_extract_overwrite_replaces(tmp_path):
    d = tmp_path / "plugins" / "up-pack"
    d.mkdir(parents=True)
    (d / "keep.txt").write_text("旧内容", encoding="utf-8")
    data = make_zip({"plugin.json": MANIFEST, "skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\nb"})
    extract_plugin_zip(data, tmp_path / "plugins", overwrite=True)
    assert not (d / "keep.txt").exists()  # 整目录替换，旧文件不残留
    assert (d / "skills" / "s" / "SKILL.md").is_file()


def test_extract_bad_zip(tmp_path):
    with pytest.raises(AuthoringError, match="zip"):
        extract_plugin_zip(b"not a zip", tmp_path)


def test_extract_no_files(tmp_path):
    with pytest.raises(AuthoringError, match="没有文件"):
        extract_plugin_zip(make_zip({}), tmp_path)


def test_extract_hidden_entries_rejected(tmp_path):
    with pytest.raises(AuthoringError, match="隐藏"):
        extract_plugin_zip(make_zip({"plugin.json": MANIFEST, ".git/config": "x"}), tmp_path)


# ======================================================================
# 技能写入
# ======================================================================
def test_write_skill_generates_frontmatter(tmp_path):
    result = write_skill_file(
        tmp_path, "weekly-report", "写周报的流程", "---\nname: old\ndescription: old\n---\n正文"
    )
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert text.startswith("---\nname: weekly-report\ndescription: 写周报的流程\n---\n")
    assert "old" not in text  # 贴进来的 frontmatter 被剥掉
    assert "正文" in text


def test_write_skill_requires_description(tmp_path):
    with pytest.raises(AuthoringError, match="description"):
        write_skill_file(tmp_path, "s", "  ", "正文")


def test_write_skill_rejects_bad_name(tmp_path):
    with pytest.raises(AuthoringError, match="不合法"):
        write_skill_file(tmp_path, "../escape", "d", "正文")


def test_write_skill_rejects_empty_body(tmp_path):
    with pytest.raises(AuthoringError, match="正文"):
        write_skill_file(tmp_path, "s", "d", "   ")


def test_write_skill_with_resources(tmp_path):
    result = write_skill_file(
        tmp_path,
        "s",
        "d",
        "正文",
        resources=[
            {"path": "scripts/run.py", "content": "print('hi')"},
            {"path": "references/guide.md", "content": "# 指南"},
            {"path": "assets/logo.png", "content_b64": base64.b64encode(b"PNGDATA").decode()},
        ],
    )
    assert set(result["files"]) == {
        "SKILL.md",
        "scripts/run.py",
        "references/guide.md",
        "assets/logo.png",
    }
    assert (tmp_path / "s" / "scripts" / "run.py").read_text(encoding="utf-8") == "print('hi')"
    assert (tmp_path / "s" / "assets" / "logo.png").read_bytes() == b"PNGDATA"


def test_write_skill_rejects_bad_resource_path(tmp_path):
    with pytest.raises(AuthoringError, match="非法"):
        write_skill_file(tmp_path, "s", "d", "正文", resources=[{"path": "../evil.py", "content": "x"}])
    with pytest.raises(AuthoringError, match="SKILL"):
        write_skill_file(tmp_path, "s", "d", "正文", resources=[{"path": "SKILL.md", "content": "x"}])
    with pytest.raises(AuthoringError, match="隐藏"):
        write_skill_file(tmp_path, "s", "d", "正文", resources=[{"path": ".git/config", "content": "x"}])


def test_read_file_reads_skill_resources(agent_home, workspace):
    """read_file 只读白名单扩展到 skills 目录：模型能读技能附带资源。"""
    import asyncio

    from code_agent.tools.base import ToolContext
    from code_agent.tools.builtin import ReadFileTool

    d = agent_home / "skills" / "s"
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.py").write_text("print('hi')", encoding="utf-8")

    tool = ReadFileTool()
    ctx = ToolContext(session_id="s", workspace=workspace, user_id="u")
    out = asyncio.run(tool.execute({"path": str(d / "run.py")}, ctx))
    assert "print('hi')" in out


def _skill_zip(files: dict[str, str] | None = None, name: str = "zipped-skill") -> bytes:
    base = {
        "SKILL.md": f"---\nname: {name}\ndescription: zip 导入的技能\n---\n# 正文\n见 scripts/run.py",
        "scripts/run.py": "print('hi')",
        "references/guide.md": "# 指南",
    }
    base.update(files or {})
    return make_zip(base)


def test_extract_skill_zip_happy(tmp_path):
    result = extract_skill_zip(_skill_zip(), tmp_path / "skills", zip_name="whatever.zip")
    assert result["name"] == "zipped-skill"  # frontmatter name 优先
    assert result["files"] == 3
    d = tmp_path / "skills" / "zipped-skill"
    assert (d / "SKILL.md").is_file()
    assert (d / "scripts" / "run.py").is_file()
    assert (d / "references" / "guide.md").is_file()


def test_extract_skill_zip_strips_common_top(tmp_path):
    data = make_zip(
        {
            "Weekly-Report-1.0/SKILL.md": "---\nname: weekly-report\ndescription: d\n---\nb",
            "Weekly-Report-1.0/scripts/gen.py": "x",
        }
    )
    result = extract_skill_zip(data, tmp_path / "skills")
    assert result["name"] == "weekly-report"
    assert (tmp_path / "skills" / "weekly-report" / "scripts" / "gen.py").is_file()


def test_extract_skill_zip_missing_skill(tmp_path):
    with pytest.raises(AuthoringError, match="SKILL.md"):
        extract_skill_zip(make_zip({"README.md": "x"}), tmp_path / "skills")


def test_extract_skill_zip_blocks_slip(tmp_path):
    data = make_zip(
        {"SKILL.md": "---\nname: s\ndescription: d\n---\nb", "../evil.txt": "x"}
    )
    with pytest.raises(AuthoringError):
        extract_skill_zip(data, tmp_path / "skills")
    assert not (tmp_path / "evil.txt").exists()


def test_extract_skill_zip_requires_overwrite(tmp_path):
    d = tmp_path / "skills" / "zipped-skill"
    d.mkdir(parents=True)
    (d / "old.txt").write_text("旧内容", encoding="utf-8")
    with pytest.raises(AuthoringError, match="已存在"):
        extract_skill_zip(_skill_zip(), tmp_path / "skills")
    result = extract_skill_zip(_skill_zip(), tmp_path / "skills", overwrite=True)
    assert result["name"] == "zipped-skill"
    assert not (d / "old.txt").exists()  # 整目录替换，旧文件不残留
    assert (tmp_path / "skills" / ".trash").is_dir()  # 旧目录进回收站


# ======================================================================
# MCP 配置
# ======================================================================
def test_normalize_valid_roundtrip():
    clean = normalize_mcp_servers(
        {"fetch": {"command": "npx", "args": ["-y", "mcp-fetch"], "env": {"K": "V"}, "trusted": True}}
    )
    assert clean == {"fetch": {"command": "npx", "args": ["-y", "mcp-fetch"], "env": {"K": "V"}, "trusted": True}}


def test_normalize_defaults_and_drops_empty():
    clean = normalize_mcp_servers({"fetch": {"command": " npx ", "args": [], "trusted": False}})
    assert clean == {"fetch": {"command": "npx"}}


def test_normalize_rejects_missing_command():
    with pytest.raises(AuthoringError, match="command"):
        normalize_mcp_servers({"bad": {"args": ["x"]}})


def test_normalize_rejects_unknown_field():
    with pytest.raises(AuthoringError, match="不认识的字段"):
        normalize_mcp_servers({"bad": {"command": "x", "TrustEd": True}})


def test_normalize_rejects_bad_args_and_env():
    with pytest.raises(AuthoringError, match="args"):
        normalize_mcp_servers({"bad": {"command": "x", "args": [1]}})
    with pytest.raises(AuthoringError, match="env"):
        normalize_mcp_servers({"bad": {"command": "x", "env": {"K": 1}}})


def test_normalize_rejects_bad_name():
    with pytest.raises(AuthoringError, match="不合法"):
        normalize_mcp_servers({"a b": {"command": "x"}})


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "mcp.json"
    clean = save_mcp_config(path, {"a": {"command": "x"}})
    assert load_mcp_config(path) == clean
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"] == clean


def test_load_missing_or_broken_returns_empty(tmp_path):
    assert load_mcp_config(tmp_path / "nope.json") == {}
    p = tmp_path / "broken.json"
    p.write_text("{ not json", encoding="utf-8")
    assert load_mcp_config(p) == {}


# ======================================================================
# API
# ======================================================================
def _auth(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def client(agent_home):
    with TestClient(create_app(provider_name="offline")) as c:
        yield c


def _make_admin_zip(name="up-pack", with_skill=True):
    files = {"plugin.json": json.dumps({"name": name, "description": "d", "prompt": "先给结论"})}
    if with_skill:
        files["skills/from-zip/SKILL.md"] = "---\nname: from-zip\ndescription: zip 带来的技能\n---\nb"
    return make_zip(files)


def test_upload_plugin_requires_auth_and_admin(client):
    assert client.post("/api/v1/plugins/upload", json={}).status_code == 401
    h = _auth(client)
    assert client.post("/api/v1/plugins/upload", json={}, headers=h).status_code == 422  # 空 body 校验失败
    # 普通用户 403
    from code_agent.api import security
    from code_agent.storage.db import UserRepo

    UserRepo(client.app.state.db).create("bob", security.hash_password("pw"), role="user")
    bh = _auth(client, "bob", "pw")
    r = client.post(
        "/api/v1/plugins/upload",
        json={"filename": "a.zip", "content_b64": base64.b64encode(_make_admin_zip()).decode()},
        headers=bh,
    )
    assert r.status_code == 403


def test_upload_and_delete_plugin_roundtrip(client, agent_home):
    h = _auth(client)
    b64 = base64.b64encode(_make_admin_zip()).decode()
    r = client.post(
        "/api/v1/plugins/upload", json={"filename": "up.zip", "content_b64": b64}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["plugin"]["name"] == "up-pack"

    # 目录里真的有文件，且目录聚合里能看到插件与内嵌技能
    assert (agent_home / "plugins" / "up-pack" / "plugin.json").is_file()
    cat = client.get("/api/v1/tools", headers=h).json()
    assert any(p["name"] == "up-pack" for p in cat["plugins"])
    assert any(p["name"] == "from-zip" and p["origin"] == "plugin:up-pack" for p in cat["packs"])
    assert "from-zip" in r.json()["skills"]

    # 重复上传被拒；勾选覆盖后成功
    r2 = client.post(
        "/api/v1/plugins/upload", json={"filename": "up.zip", "content_b64": b64}, headers=h
    )
    assert r2.status_code == 422
    r3 = client.post(
        "/api/v1/plugins/upload",
        json={"filename": "up.zip", "content_b64": b64, "overwrite": True},
        headers=h,
    )
    assert r3.status_code == 200

    # 删除（实为移入 .trash 回收站，可找回）
    r4 = client.delete("/api/v1/plugins/up-pack", headers=h)
    assert r4.status_code == 200
    assert r4.json()["trashed_as"].startswith("up-pack-")
    assert not (agent_home / "plugins" / "up-pack").exists()
    assert (agent_home / "plugins" / ".trash").is_dir()
    # 回收站目录本身不参与插件扫描（隐藏目录跳过）
    cat = client.get("/api/v1/tools", headers=h).json()
    assert not any(p["name"] == "up-pack" for p in cat["plugins"])
    assert not any(p["name"].startswith(".") for p in cat["plugins"])
    assert client.delete("/api/v1/plugins/up-pack", headers=h).status_code == 404


def test_upload_plugin_rejects_slip_and_missing_manifest(client, agent_home):
    h = _auth(client)
    bad = base64.b64encode(
        make_zip({"plugin.json": MANIFEST, "../evil.txt": "x"})
    ).decode()
    assert (
        client.post("/api/v1/plugins/upload", json={"filename": "e.zip", "content_b64": bad}, headers=h).status_code
        == 422
    )
    no_manifest = base64.b64encode(make_zip({"README.md": "x"})).decode()
    assert (
        client.post("/api/v1/plugins/upload", json={"filename": "e.zip", "content_b64": no_manifest}, headers=h).status_code
        == 422
    )
    # 失败后不落任何东西
    assert not any((agent_home / "plugins").iterdir())


def test_upload_skill_endpoint(client, agent_home):
    assert client.post("/api/v1/skills/upload", json={}).status_code == 401
    h = _auth(client)
    r = client.post(
        "/api/v1/skills/upload",
        json={"name": "api-skill", "description": "接口传的技能", "content": "# 步骤\n1. …"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert "api-skill" in r.json()["skills"]
    doc = agent_home / "skills" / "api-skill" / "SKILL.md"
    assert doc.is_file() and "description: 接口传的技能" in doc.read_text(encoding="utf-8")

    # 目录聚合能看到（origin=local）
    cat = client.get("/api/v1/tools", headers=h).json()
    hit = next(p for p in cat["packs"] if p["name"] == "api-skill")
    assert hit["origin"] == "local" and hit["kind"] == "skill"

    # 非法名 422
    r2 = client.post(
        "/api/v1/skills/upload",
        json={"name": "bad name", "description": "d", "content": "x"},
        headers=h,
    )
    assert r2.status_code == 422


def test_import_skill_zip_endpoint(client, agent_home):
    """整体上传技能包（zip）：解包到 skills/<name>/，目录聚合立即可见。"""
    assert client.post("/api/v1/skills/import", json={}).status_code == 401
    h = _auth(client)
    b64 = base64.b64encode(_skill_zip()).decode()
    r = client.post(
        "/api/v1/skills/import", json={"filename": "pack.zip", "content_b64": b64}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["skill"]["name"] == "zipped-skill"
    assert (agent_home / "skills" / "zipped-skill" / "SKILL.md").is_file()
    assert (agent_home / "skills" / "zipped-skill" / "scripts" / "run.py").is_file()

    # 目录聚合能看到（origin=local）
    cat = client.get("/api/v1/tools", headers=h).json()
    hit = next(p for p in cat["packs"] if p["name"] == "zipped-skill")
    assert hit["origin"] == "local" and hit["kind"] == "skill"

    # 坏包 422（缺 SKILL.md）
    bad = base64.b64encode(make_zip({"README.md": "x"})).decode()
    assert (
        client.post(
            "/api/v1/skills/import", json={"filename": "bad.zip", "content_b64": bad}, headers=h
        ).status_code
        == 422
    )


def test_mcp_config_endpoints(client):
    assert client.get("/api/v1/mcp/config").status_code == 401
    h = _auth(client)
    cfg = client.get("/api/v1/mcp/config", headers=h).json()
    assert cfg["servers"] == {} and cfg["from_plugins"] == []

    body = {"servers": {"fetch": {"command": "npx", "args": ["-y", "mcp-fetch"]}}}
    r = client.put("/api/v1/mcp/config", json=body, headers=h)
    assert r.status_code == 200 and r.json()["restart_required"] is True

    # 原样再存一次：没有变化就不要求重启
    r2 = client.put("/api/v1/mcp/config", json=body, headers=h)
    assert r2.json()["restart_required"] is False

    cfg = client.get("/api/v1/mcp/config", headers=h).json()
    assert cfg["servers"]["fetch"]["command"] == "npx"

    # 坏配置整份不写
    r3 = client.put(
        "/api/v1/mcp/config",
        json={"servers": {"good": {"command": "x"}, "bad": {"args": []}}},
        headers=h,
    )
    assert r3.status_code == 422
    assert client.get("/api/v1/mcp/config", headers=h).json()["servers"].keys() == {"fetch"}

    # 普通用户只读
    from code_agent.api import security
    from code_agent.storage.db import UserRepo

    UserRepo(client.app.state.db).create("bob", security.hash_password("pw"), role="user")
    bh = _auth(client, "bob", "pw")
    assert client.get("/api/v1/mcp/config", headers=bh).status_code == 200
    assert (
        client.put("/api/v1/mcp/config", json={"servers": {}}, headers=bh).status_code == 403
    )


def test_plugin_declared_server_shows_as_readonly(client, agent_home):
    h = _auth(client)
    zip_b64 = base64.b64encode(
        make_zip(
            {
                "plugin.json": json.dumps(
                    {
                        "name": "mcp-pack",
                        "description": "d",
                        "mcpServers": {"yahoo": {"command": "npx", "args": ["-y", "yahoo"]}},
                    }
                )
            }
        )
    ).decode()
    assert (
        client.post("/api/v1/plugins/upload", json={"filename": "m.zip", "content_b64": zip_b64}, headers=h).status_code
        == 200
    )
    cfg = client.get("/api/v1/mcp/config", headers=h).json()
    assert cfg["servers"] == {}          # 本地 mcp.json 仍为空
    assert cfg["from_plugins"] == ["yahoo"]  # 插件声明的走只读展示
