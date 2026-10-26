"""代码模式 P3：JSON-RPC 传输层 / LSP 目录与探测 / LSP 代理与诊断落图。

本机通常没装任何语言服务器，所以用 `tests/fixtures/fake_lsp_server.py`
（Content-Length 帧、含"服务端主动请求"陷阱）做确定性验证；真实语言服务器
（pyright）在浏览器冒烟里单独验证。

覆盖的要点：
1. 两种帧格式的编解码（含"一个 chunk 里多条消息""头被切开"这类边界）；
2. 客户端必须响应服务端发来的请求——不响应则 fake server 不发诊断，测试会失败；
3. 诊断 → 索引 → 符号归属 → 地图角标（含 didClose 清空、文件删除清理）；
4. LSP WS 桥的鉴权、就绪帧、请求响应与诊断扇出。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_agent.api.app import create_app
from code_agent.codeintel import lsp_catalog
from code_agent.codeintel.lsp import LspManager, LspSession
from code_agent.codeintel.service import CodeIntelService
from code_agent.codeintel.store import SymbolIndex
from code_agent.codeintel.uri import file_uri, uri_to_path, uri_to_rel
from code_agent.extensions.jsonrpc_stdio import (
    JsonRpcError,
    JsonRpcStdio,
    _Decoder,
    child_env,
    encode_message,
)

FAKE_LSP = str(Path(__file__).parent / "fixtures" / "fake_lsp_server.py")


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------
def test_encoder_and_decoder_roundtrip_both_framings():
    for framing in ("newline", "content-length"):
        decoder = _Decoder(framing)
        payloads = [{"jsonrpc": "2.0", "id": 1, "method": "a"}, {"jsonrpc": "2.0", "id": 2, "result": {"x": "你好"}}]
        blob = b"".join(encode_message(p, framing) for p in payloads)
        assert decoder.feed(blob) == payloads


def test_content_length_decoder_handles_split_and_junk():
    decoder = _Decoder("content-length")
    message = {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": "file:///a"}}
    blob = encode_message(message, "content-length")
    # 头被切开、体被切开：都要能在后续 feed 里补齐
    assert decoder.feed(blob[:8]) == []
    assert decoder.feed(blob[8:20]) == []
    assert decoder.feed(blob[20:]) == [message]
    # 服务器往 stdout 写了非协议内容（只有长度头、没有体）时不能卡死
    assert decoder.feed(b"Content-Length: 12\r\n\r\n") == []
    assert decoder.feed(b"") == []


def test_newline_decoder_ignores_non_json_lines():
    decoder = _Decoder("newline")
    junk = "日志行\n".encode("utf-8")  # 服务器往 stdout 写日志：不能让它卡住解析
    assert decoder.feed(b'{"jsonrpc":"2.0","id":1}\n' + junk + b'{"jsonrpc":"2.0","id":2}\n') == [
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 2},
    ]


def test_child_env_forces_utf8_but_respects_override():
    env = child_env()
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    overridden = child_env({"PYTHONIOENCODING": "gbk", "MY_FLAG": "1"})
    assert overridden["PYTHONIOENCODING"] == "gbk" and overridden["MY_FLAG"] == "1"


def test_jsonrpc_client_reports_start_failure():
    async def flow():
        client = JsonRpcStdio("definitely-not-a-real-binary-xyz", [], name="nope")
        with pytest.raises(JsonRpcError) as exc:
            await client.start()
        assert "无法启动" in str(exc.value)

    asyncio.run(flow())


# ---------------------------------------------------------------------------
# 目录与探测
# ---------------------------------------------------------------------------
def test_uri_helpers_roundtrip(tmp_path: Path):
    target = tmp_path / "包 层" / "mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    uri = file_uri(target)
    assert uri.startswith("file:///") and " " not in uri
    assert uri_to_path(uri) == target.resolve()
    assert uri_to_rel(uri, tmp_path) == "包 层/mod.py"
    assert uri_to_rel("file:///other/place.py", tmp_path) is None


def test_catalog_merge_and_detect(tmp_path: Path):
    config = {
        "pyright": {"command": "custom-pyright", "args": ["--stdio", "--extra"], "enabled": True},
        "mylang": {"command": sys.executable, "languages": ["python"], "name": "自定义"},
        "disabled": {"command": "nothing-here", "enabled": False},
    }
    merged = {s["id"]: s for s in lsp_catalog.merged_servers(config)}
    assert merged["pyright"]["command"] == "custom-pyright"
    assert merged["pyright"]["args"] == ["--stdio", "--extra"]
    assert merged["mylang"]["name"] == "自定义" and merged["mylang"]["builtin"] is False
    # 显式关掉的服务器仍要出现在清单里（否则用户没有任何入口把它打开）
    assert merged["disabled"]["enabled"] is False

    detected = lsp_catalog.detect(
        languages={"python": 12, "markdown": 3},
        present_files={"pyproject.toml", "README.md"},
        config=config,
        roots=(tmp_path,),
    )
    by_id = {s["id"]: s for s in detected}
    assert by_id["pyright"]["detected"] is True
    assert by_id["pyright"]["installed"] is False  # custom-pyright 不在 PATH
    assert by_id["pyright"]["state"] == "missing"
    assert by_id["pyright"]["install_hint"]
    assert by_id["mylang"]["installed"] is True  # 用的是 sys.executable
    assert by_id["gopls"]["detected"] is False  # 项目里没有 go


def test_resolve_command_finds_project_local_bin(tmp_path: Path):
    local = tmp_path / "node_modules" / ".bin"
    local.mkdir(parents=True)
    script = local / "typescript-language-server"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    resolved = lsp_catalog.resolve_command("typescript-language-server", tmp_path)
    assert resolved is not None and Path(resolved).name.startswith("typescript-language-server")
    assert lsp_catalog.resolve_command("绝对不存在的东西-xyz", tmp_path) is None


def test_resolve_command_is_cached_and_can_be_cleared(tmp_path: Path, monkeypatch):
    """解析必须缓存：Windows 上每次 `which` 要遍历 PATH × PATHEXT，
    28 个预设逐个解析要 2 秒多（真机实测），项目一打开、面板一点就卡一下。
    """
    lsp_catalog.clear_resolve_cache()
    calls = {"n": 0}
    real_which = lsp_catalog.shutil.which

    def counting_which(name):
        calls["n"] += 1
        return real_which(name)

    monkeypatch.setattr(lsp_catalog.shutil, "which", counting_which)
    first = lsp_catalog.resolve_command("python", tmp_path)
    after_first = calls["n"]
    second = lsp_catalog.resolve_command("python", tmp_path)
    assert first == second and after_first >= 1
    assert calls["n"] == after_first, "第二次不该再走 which（缓存没生效）"

    lsp_catalog.clear_resolve_cache()
    lsp_catalog.resolve_command("python", tmp_path)
    assert calls["n"] > after_first, "clear 之后应该重新解析"


def test_detect_marks_missing_servers_with_install_hint(tmp_path: Path):
    """探测结果要能直接指导用户：未安装的必须带 state=missing 与安装命令。"""
    lsp_catalog.clear_resolve_cache()
    servers = lsp_catalog.detect(
        languages={"python": 3}, present_files={"pyproject.toml"},
        config={}, roots=(tmp_path,),
    )
    pyright = next(s for s in servers if s["id"] == "pyright")
    assert pyright["detected"] is True and pyright["language_hits"] == {"python": 3}
    assert pyright["state"] in ("installed", "missing")
    if pyright["state"] == "missing":
        assert "pyright" in pyright["install_hint"]
    # 与项目无关的服务器也要如实给出状态（UI 里按 detected 排序，不是隐藏）
    assert all(s["state"] in ("installed", "missing", "unavailable") for s in servers)
    assert all("install_hint" in s for s in servers)


def test_catalog_args_match_the_real_cli():
    """预设里的 args 必须匹配真实 CLI——写错了用户装了也起不来。

    真机踩过三处：pylsp 不接受 `--stdio`（它默认就走 stdio）；ruff-lsp 与当前
    lsprotocol 不兼容（改用 ruff 自带的 `ruff server`）；npm 版 taplo 不含 LSP。
    这条测试把"命令 + 参数 + 提示"一起钉住。
    """
    presets = {p["id"]: p for p in lsp_catalog.merged_servers({})}
    assert presets["pylsp"]["args"] == [] or presets["pylsp"]["args"] == (), (
        "pylsp 默认 stdio，多余的参数会直接报错"
    )
    assert presets["ruff"]["command"] == "ruff" and tuple(presets["ruff"]["args"]) == ("server",)
    assert "ruff" in presets["ruff"]["install_hint"]
    assert tuple(presets["pyright"]["args"]) == ("--stdio",)
    assert tuple(presets["toml"]["args"]) == ("lsp", "stdio")
    # npm 版 taplo 不含 LSP：提示必须说清楚，否则用户装完仍然用不了
    assert "LSP" in presets["toml"]["install_hint"] and "npm" in presets["toml"]["install_hint"]
    # 每个预设都要给得出安装办法（面板上"未安装"那行直接显示它）
    assert all(p["install_hint"] for p in presets.values())


def test_catalog_covers_the_common_languages():
    """常见语言得有预设（不然用户根本看不到"该装什么"）。"""
    ids = {p["id"] for p in lsp_catalog.merged_servers({})}
    assert {"pyright", "typescript", "vue", "json", "css", "html", "yaml", "bash",
            "php", "sql", "dockerfile", "lua", "zig", "kotlin"} <= ids


# ---------------------------------------------------------------------------
# 代理 + 诊断
# ---------------------------------------------------------------------------
class FakeProjects:
    def __init__(self, row: dict) -> None:
        self.row = row

    def get(self, project_id: str) -> dict | None:
        return self.row if project_id == self.row["id"] else None

    def find_by_root(self, root_key: str) -> dict | None:
        return self.row if self.row["root_key"] == root_key else None

    def set_scan(self, project_id: str, summary_json: str) -> None:
        pass


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "core.py").write_text(
        '"""核心。"""\n\n\ndef compute(a, b):\n    unused_value = a\n    return a + b\n',
        encoding="utf-8",
    )
    return root


@pytest.fixture
def env(repo, agent_home, monkeypatch) -> tuple[CodeIntelService, LspManager, dict]:
    monkeypatch.setenv("CODE_AGENT_HOME", str(agent_home))
    row = {
        "id": "p_lsp", "name": "proj", "root_path": str(repo),
        "root_key": str(repo).replace("\\", "/").lower(), "user_id": "u_admin",
    }
    projects = FakeProjects(row)
    intel = CodeIntelService(projects)
    asyncio.run(intel.scan(row, full=True))
    # 只启用 fake server：把内置 python 服务器指向夹具脚本
    config = agent_home / "lsp.json"
    config.write_text(
        json.dumps(
            {
                "servers": {
                    "pyright": {"enabled": False},
                    "pylsp": {"enabled": False},
                    "ruff": {"enabled": False},
                    "fake": {
                        "name": "Fake LSP",
                        "command": sys.executable,
                        "args": [FAKE_LSP],
                        "languages": ["python"],
                        "rootMarkers": ["*.py"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    lsp = LspManager(
        projects, intel=intel, config_path=config,
        on_diagnostics=lambda project, uri, items: intel.record_diagnostics(project, uri, items),
    )
    return intel, lsp, row


def test_lsp_session_diagnostics_and_requests(env, repo):
    intel, lsp, row = env

    async def flow():
        session = await lsp.session_for(row, "python")
        try:
            assert session.server_id == "fake"
            assert session.supports("completionProvider") and session.supports("hoverProvider")
            uri = file_uri(repo / "pkg" / "core.py")
            text = (repo / "pkg" / "core.py").read_text(encoding="utf-8")

            await lsp.handle_client_notification(
                session,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}
                    },
                },
            )
            # 诊断是异步推来的（fake server 先等我们的 progress 响应）
            for _ in range(50):
                if session.diagnostics.get(uri):
                    break
                await asyncio.sleep(0.1)
            items = session.diagnostics.get(uri) or []
            assert items, "没有收到诊断：客户端可能没响应服务端的 progress 请求"
            severities = sorted(item["severity"] for item in items)
            assert severities == [2, 3]  # unused → warning，另有一条 info

            # 落到索引：按严重级计数 + 归到最内层符号
            summary = intel.diagnostics(row)
            assert summary["counts"]["warning"] == 1 and summary["counts"]["info"] == 1
            assert summary["files"]["pkg/core.py"]["warning"] == 1
            symbol_ids = set(summary["symbols"])
            assert any("#compute" in sid for sid in symbol_ids), symbol_ids

            # 请求转发：补全/悬停/定义
            completion = await lsp.handle_client_request(
                session,
                {"jsonrpc": "2.0", "id": 1, "method": "textDocument/completion",
                 "params": {"textDocument": {"uri": uri}, "position": {"line": 4, "character": 4}}},
            )
            assert completion["result"]["items"][0]["label"] == "fake_symbol"
            hover = await lsp.handle_client_request(
                session,
                {"jsonrpc": "2.0", "id": 2, "method": "textDocument/hover",
                 "params": {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 1}}},
            )
            assert "fake hover" in hover["result"]["contents"]["value"]
            definition = await lsp.handle_client_request(
                session,
                {"jsonrpc": "2.0", "id": 3, "method": "textDocument/definition",
                 "params": {"textDocument": {"uri": uri}, "position": {"line": 4, "character": 4}}},
            )
            assert definition["result"]["range"]["start"]["line"] == 2

            # didChange：把 unused 换成错误 → 计数随之变化
            await lsp.handle_client_notification(
                session,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didChange",
                    "params": {
                        "textDocument": {"uri": uri, "version": 2},
                        "contentChanges": [{"text": text.replace("unused_value", "TODO_ERR_value")}],
                    },
                },
            )
            for _ in range(50):
                if (session.diagnostics.get(uri) or [{}])[0].get("severity") == 1:
                    break
                await asyncio.sleep(0.1)
            summary = intel.diagnostics(row)
            assert summary["counts"]["error"] == 1 and summary["counts"]["warning"] == 0

            # didClose：诊断清空（地图上不该留住已关闭文件的错误）
            await lsp.handle_client_notification(
                session,
                {"jsonrpc": "2.0", "method": "textDocument/didClose",
                 "params": {"textDocument": {"uri": uri}}},
            )
            assert session.diagnostics.get(uri) in (None, [])
            assert intel.diagnostics(row)["counts"]["error"] == 0
        finally:
            await lsp.stop_all()

    asyncio.run(flow())


def _language_without_server(lsp, row) -> str | None:
    """找一个**本机确实没装**服务器的语言。

    环境会变（用户装了 gopls 之后，写死 "go 一定没装" 的断言就不再成立——
    真机踩到：gopls 装在 C:\\Users\\dyh\\go\\bin 后这个用例失败）。所以按当时环境挑语言。
    """
    from code_agent.codeintel import lsp_catalog

    root = Path(row["root_path"])
    langs: set[str] = set()
    for entry in lsp_catalog.merged_servers(lsp.config()):
        if entry["enabled"]:
            langs |= set(entry["languages"])
    for lang in sorted(langs):
        entries = lsp.servers_for_language(lang, row)
        if entries and all(
            not lsp_catalog.resolve_command(e["command"], root, root.parent) for e in entries
        ):
            return lang
    return None


def test_lsp_unavailable_language_reports_reason(env):
    intel, lsp, row = env

    async def flow():
        # 目录里有、但本机没装：错误信息必须带安装提示（而不是"补全没反应"）
        lang = _language_without_server(lsp, row)
        if lang is None:
            pytest.skip("这台机器把配置里的语言服务器都装了，无法验证「未安装」分支")
        with pytest.raises(JsonRpcError) as missing:
            await lsp.session_for(row, lang)
        assert "未安装" in str(missing.value)
        # 提示里要有可安装的命令名（哪个服务器）
        assert any(
            e["command"] and e["command"].split()[0] in str(missing.value)
            for e in lsp.servers_for_language(lang, row)
        ), str(missing.value)

        # 目录里根本没有这种语言：要说清"没配置"，别让人去装不存在的东西
        with pytest.raises(JsonRpcError) as unconfigured:
            await lsp.session_for(row, "cobol")
        assert "没有为 cobol 配置语言服务器" in str(unconfigured.value)

        # 优先用已安装的：servers_for_language 决定"起哪个"，fake 已装、pyright 被关掉
        preferred = lsp.servers_for_language("python", row)
        assert preferred and preferred[0]["id"] == "fake"

        # 探测清单（给界面看的）：关掉的如实标 unavailable，fake 是 installed + detected
        servers = await lsp.servers_for_project(row)
        by_id = {s["id"]: s for s in servers}
        assert by_id["pyright"]["state"] == "unavailable"
        assert by_id["fake"]["installed"] is True and by_id["fake"]["detected"] is True

    asyncio.run(flow())


def test_diagnostics_cleared_when_file_removed(env, repo):
    intel, lsp, row = env

    async def flow():
        uri = file_uri(repo / "pkg" / "core.py")
        intel.record_diagnostics(
            row, uri,
            [{"line": 2, "severity": 1, "message": "boom", "source": "fake"}],
        )
        assert intel.diagnostics(row)["counts"]["error"] == 1
        (repo / "pkg" / "core.py").unlink()
        await intel.scan(row)
        assert intel.diagnostics(row)["counts"]["error"] == 0

    asyncio.run(flow())


def test_diagnostics_reject_uri_outside_project(env):
    intel, lsp, row = env
    assert intel.record_diagnostics(row, "file:///C:/Windows/system32/x.py", [
        {"line": 1, "severity": 1, "message": "nope"}
    ]) == 0
    assert intel.diagnostics(row)["counts"]["error"] == 0
    assert intel.record_diagnostics(row, "untitled:Untitled-1", []) == 0


def test_symbol_attribution_picks_innermost_symbol():
    index = SymbolIndex(":memory:")
    index.apply_parse(
        rel="a.py", lang="python", loc=20, size=100, mtime=1.0, sha1="x",
        parsed={
            "symbols": [
                {"id": "sym:a.py#Cls", "kind": "class", "name": "Cls", "qname": "Cls", "line": 1, "end_line": 20},
                {"id": "sym:a.py#Cls.m", "kind": "method", "name": "m", "qname": "Cls.m", "line": 5,
                 "end_line": 9, "parent": "sym:a.py#Cls"},
            ],
            "entrypoint": False, "confidence": "high", "error": "",
        },
        edges=[],
    )
    assert index.symbol_at("a.py", 7) == "sym:a.py#Cls.m"
    assert index.symbol_at("a.py", 15) == "sym:a.py#Cls"
    assert index.symbol_at("a.py", 30) == ""
    assert index.symbol_at("b.py", 1) == ""
    index.close()


# ---------------------------------------------------------------------------
# API + WS 桥
# ---------------------------------------------------------------------------
@pytest.fixture
def client(agent_home, monkeypatch):
    with TestClient(create_app(provider_name="offline")) as c:
        yield c


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _write_lsp_config(agent_home: Path) -> None:
    for sid in ("pyright", "pylsp", "ruff", "typescript"):
        pass
    (agent_home / "lsp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "pyright": {"enabled": False},
                    "pylsp": {"enabled": False},
                    "ruff": {"enabled": False},
                    "fake": {
                        "name": "Fake LSP",
                        "command": sys.executable,
                        "args": [FAKE_LSP],
                        "languages": ["python"],
                        "rootMarkers": ["*.py"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )


def test_lsp_endpoints_and_ws_bridge(client, repo, agent_home):
    _write_lsp_config(agent_home)
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]
    client.get(f"/api/v1/code/projects/{pid}/map", params={"ensure": 1}, headers=h)

    # 全站概览：目录 + 状态（内置 pyright 被关掉 → 如实显示 unavailable）
    overview = client.get("/api/v1/code/lsp", headers=h).json()
    ids = {s["id"]: s for s in overview["servers"]}
    assert ids["pyright"]["state"] == "unavailable"
    assert ids["fake"]["installed"] is True
    assert "sessions" in overview

    # 带 project_id 的概览才谈"探测"（要知道项目里实际有哪些语言）
    scoped = client.get(
        f"/api/v1/code/lsp", params={"project_id": pid}, headers=h
    ).json()
    scoped_ids = {s["id"]: s for s in scoped["servers"]}
    assert scoped_ids["fake"]["detected"] is True and scoped_ids["fake"]["installed"] is True
    assert scoped_ids["gopls"]["detected"] is False

    project_view = client.get(f"/api/v1/code/projects/{pid}/lsp", headers=h).json()
    assert any(s["id"] == "fake" for s in project_view["servers"])

    # WS 桥：就绪帧 → 请求响应 → 诊断推回 → 诊断接口可查
    with client.websocket_connect(
        f"/api/v1/code/projects/{pid}/lsp/stream?lang=python&token={token}"
    ) as ws:
        ready = ws.receive_json()
        assert ready["method"] == "code-agent/ready"
        assert ready["params"]["server"] == "fake"
        assert "completionProvider" in ready["params"]["capabilities"]

        uri = file_uri(repo / "pkg" / "core.py")
        text = (repo / "pkg" / "core.py").read_text(encoding="utf-8")
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}},
            }
        )
        diagnostics = None
        for _ in range(20):
            message = ws.receive_json()
            if message.get("method") == "textDocument/publishDiagnostics":
                diagnostics = message["params"]
                break
        assert diagnostics is not None and diagnostics["uri"] == uri
        assert any(item["severity"] == 2 for item in diagnostics["diagnostics"])

        # 请求走 JSON-RPC：id 原样回
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "textDocument/completion",
                "params": {"textDocument": {"uri": uri}, "position": {"line": 4, "character": 4}},
            }
        )
        reply = None
        for _ in range(10):
            message = ws.receive_json()
            if message.get("id") == 42:
                reply = message
                break
        assert reply and reply["result"]["items"][0]["label"] == "fake_symbol"

        # 不支持的能力：回空结果而不是报错（编辑器里不该弹红框）
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 43,
                "method": "textDocument/rename",
                "params": {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 0},
                           "newName": "x"},
            }
        )
        for _ in range(10):
            message = ws.receive_json()
            if message.get("id") == 43:
                assert message["result"] is None
                break
        else:
            raise AssertionError("rename 请求没有响应")

    counts = client.get(f"/api/v1/code/projects/{pid}/diagnostics", headers=h).json()
    assert counts["counts"]["warning"] >= 1
    assert counts["files"]["pkg/core.py"]["warning"] >= 1
    assert counts["seq"] > 0
    detail = client.get(
        f"/api/v1/code/projects/{pid}/diagnostics", params={"path": "pkg/core.py"}, headers=h
    ).json()
    assert detail["files_detail"]["pkg/core.py"][0]["message"].startswith("fake:")

    # 地图统计里带上诊断概览
    payload = client.get(f"/api/v1/code/projects/{pid}/map", headers=h).json()
    assert payload["stats"]["diagnostics"]["warning"] >= 1

    # 越权：别人的项目连不上 LSP 通道
    client.app.state.users.create(
        "mallory",
        __import__("code_agent.api.security", fromlist=["hash_password"]).hash_password("m123"),
        role="user",
    )
    other = _login(client, "mallory", "m123")
    assert client.get(f"/api/v1/code/projects/{pid}/diagnostics", headers=_auth(other)).status_code == 404
    assert client.get(f"/api/v1/code/projects/{pid}/lsp", headers=_auth(other)).status_code == 404


def test_lsp_ws_requires_auth_and_lang(client, repo, agent_home):
    _write_lsp_config(agent_home)
    token = _login(client)
    h = _auth(token)
    pid = client.post(
        "/api/v1/code/projects", json={"mode": "open", "path": str(repo)}, headers=h
    ).json()["id"]
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/v1/code/projects/{pid}/lsp/stream?lang=python") as ws:
            ws.receive_json()

    with client.websocket_connect(
        f"/api/v1/code/projects/{pid}/lsp/stream?token={token}"
    ) as ws:
        message = ws.receive_json()
        assert message["method"] == "code-agent/error"
        assert "lang" in message["params"]["message"]


# Fast IDE bridge regression fixtures: no application workers/index scan/model.
class _RecordingStdio(JsonRpcStdio):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []
        self.running = False
        self.fail_send = False
        self.results = {}

    @property
    def alive(self):
        return self.running

    async def start(self, **kwargs):
        self.running = True

    async def stop(self, **kwargs):
        self.running = False

    async def send_raw(self, payload):
        if self.fail_send:
            raise JsonRpcError("fixture send failure")
        self.sent.append(payload)
        method = payload.get("method")
        rid = payload.get("id")
        params = payload.get("params") or {}
        if method == "test/wait":
            self.on_notification({"jsonrpc": "2.0", "method": "test/started",
                                  "params": {"actual_id": rid, "tag": params.get("tag")}})
            return
        if method == "test/release":
            self._dispatch({"jsonrpc": "2.0", "id": params["target"], "result": params.get("value")})
        if method in ("textDocument/didOpen", "textDocument/didChange"):
            document = params["textDocument"]
            self.on_notification({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {
                "uri": document["uri"], "version": document["version"], "diagnostics": []}})
        if rid is not None:
            result = self.results.get(method)
            if method == "initialize":
                result = {"capabilities": {"completionProvider": {"resolveProvider": True}, "hoverProvider": True,
                    "codeActionProvider": {"resolveProvider": True}, "signatureHelpProvider": {},
                    "renameProvider": {"prepareProvider": True}, "documentFormattingProvider": True}}
            self._dispatch({"jsonrpc": "2.0", "id": rid, "result": result})


@pytest.fixture
def fast_lsp(repo, monkeypatch):
    import code_agent.codeintel.lsp as lsp_module

    monkeypatch.setattr(lsp_module, "JsonRpcStdio", _RecordingStdio)
    row = {"id": "p_fast", "root_path": str(repo), "name": "fixture", "user_id": "u_local"}
    manager = LspManager(FakeProjects(row))
    definitions = [{"id": sid, "name": sid, "command": sys.executable, "args": [], "enabled": True,
                    "languages": ["python"], "install_hint": "", "settings": {}}
                   for sid in ("fake", "ruff")]
    monkeypatch.setattr(manager, "servers_for_language", lambda lang, project: definitions if lang == "python" else [])

    async def servers(project):
        return [{"id": entry["id"], "languages": entry["languages"], "installed": True} for entry in definitions]

    monkeypatch.setattr(manager, "servers_for_project", servers)
    return manager, row


def _open(uri, version=1, text="x = 1"):
    return {"method": "textDocument/didOpen", "params": {"textDocument": {
        "uri": uri, "version": version, "languageId": "python", "text": text}}}


def _change(uri, version, text="x = 2"):
    return {"method": "textDocument/didChange", "params": {
        "textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]}}


def test_ide_lsp_environment_capabilities_and_server_selection(fast_lsp, repo):
    import os
    manager, row = fast_lsp
    executable = repo / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture interpreter probe only", encoding="utf-8")
    if os.name != "nt":
        executable.chmod(0o755)

    async def flow():
        try:
            a, b = await asyncio.gather(manager.session_for(row, "python"), manager.session_for(row, "python", server_id="fake"))
            assert a is b
            assert a.environment == {"python": str(executable), "interpreter_source": "project-venv"}
            assert a.settings["python"]["pythonPath"] == str(executable)
            assert a.settings["pylsp"]["plugins"]["jedi"]["environment"] == str(executable)
            init = next(msg["params"] for msg in a.client.sent if msg.get("method") == "initialize")
            capabilities = init["capabilities"]["textDocument"]
            assert capabilities["completion"]["completionItem"]["insertReplaceSupport"]
            assert capabilities["completion"]["completionItem"]["resolveSupport"]["properties"] == ["documentation", "detail", "additionalTextEdits"]
            assert capabilities["rename"]["prepareSupport"]
            assert capabilities["codeAction"]["resolveSupport"] == {"properties": ["edit"]}
            assert capabilities["publishDiagnostics"]["versionSupport"]
            assert capabilities["signatureHelp"]["signatureInformation"]["parameterInformation"]["labelOffsetSupport"]
            semantic = capabilities["semanticTokens"]
            assert semantic["requests"] == {"full": True, "range": True}
            assert semantic["formats"] == ["relative"]
            assert {"class", "method", "parameter", "variable", "property"} <= set(semantic["tokenTypes"])
            assert {"annotationMember", "record", "recordComponent"} <= set(semantic["tokenTypes"])
            assert {"readonly", "static", "defaultLibrary", "deprecated"} <= set(semantic["tokenModifiers"])
            assert {"public", "private", "protected", "constructor", "typeArgument"} <= set(semantic["tokenModifiers"])
            assert semantic["overlappingTokenSupport"] is False and semantic["multilineTokenSupport"] is False
            assert init["capabilities"]["workspace"]["workspaceEdit"]["resourceOperations"] == ["create", "rename", "delete"]
            for capability in ("callHierarchy", "typeHierarchy", "codeLens", "inlayHint"):
                assert capabilities[capability]["dynamicRegistration"] is False
            ruff = await manager.session_for(row, "python", server_id="ruff")
            assert ruff is not a and ruff.key.endswith(":python:ruff")
            assert await manager.session_for(row, "python", server_id="ruff") is ruff
            for language, server in (("python", "unknown"), ("javascript", "ruff")):
                with pytest.raises(JsonRpcError):
                    await manager.session_for(row, language, server_id=server)
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_ide_lsp_document_versions_reconnect_and_uri_boundary(fast_lsp, repo):
    manager, row = fast_lsp

    async def flow():
        session = await manager.session_for(row, "python")
        try:
            uri = file_uri(repo / "pkg/core.py")
            await manager.handle_client_notification(session, _open(uri, 7))
            count = len(session.client.sent)
            await manager.handle_client_notification(session, _open(uri, 7))
            assert len(session.client.sent) == count
            await manager.handle_client_notification(session, _open(uri, 9, "offline edits"))
            assert session.client.sent[-1]["method"] == "textDocument/didChange"
            assert session.documents[uri] == 9
            for message in (_change(uri, 8), _change(uri, 9), _open(uri, 8), _open(uri, 9, "different same version"),
                            _open(file_uri(repo.parent / "outside.py")), _open("untitled:secret"),
                            _open(file_uri(repo)), _open("file://remote-server" + uri.removeprefix("file://"))):
                with pytest.raises(JsonRpcError):
                    await manager.handle_client_notification(session, message)
            assert session.documents[uri] == 9
            await manager.handle_client_notification(session, _change(uri, 10))
            await manager.close_document(session, uri)
            assert uri not in session.documents and uri not in session.document_hashes
            await manager.handle_client_notification(session, _open(uri, 1))
            assert session.documents[uri] == 1
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_ide_lsp_diagnostics_filter_stale_versions_and_preserve_data(fast_lsp, repo):
    manager, row = fast_lsp
    saved, received_a, received_b = [], [], []
    manager._on_diagnostics = lambda project, uri, items: saved.append((uri, items))

    async def flow():
        session = await manager.session_for(row, "python")
        try:
            uri = file_uri(repo / "pkg/core.py")
            await manager.handle_client_notification(session, _open(uri, 7))
            unsubscribe = manager.subscribe(session, received_a.append)
            manager.subscribe(session, received_b.append)
            diagnostic = {"range": {"start": {"line": 0, "character": 2}, "end": {"line": 0, "character": 3}},
                          "message": "warning", "severity": 2, "code": 42, "tags": [1], "data": {"fix": "preserved"}}

            def publish(version, target=uri):
                manager._on_notification(session, {"method": "textDocument/publishDiagnostics", "params": {
                    "uri": target, "version": version, "diagnostics": [diagnostic]}})

            publish(7)
            assert session.diagnostic_versions[uri] == 7
            assert session.diagnostics[uri][0]["data"] == {"fix": "preserved"}
            assert session.diagnostics[uri][0]["code"] == 42
            assert session.diagnostics[uri][0]["line"] == 1
            await manager.handle_client_notification(session, _change(uri, 8))
            count = len(saved), len(received_a), len(received_b)
            publish(7)
            publish(9)
            publish(8, file_uri(repo.parent / "outside.py"))
            assert (len(saved), len(received_a), len(received_b)) == count
            publish(8)
            assert session.diagnostic_versions[uri] == 8
            unsubscribe()
            publish(8)
            assert len(received_b) == len(received_a) + 1
            await manager.close_document(session, uri)
            count = len(received_b)
            publish(None)
            publish(8)
            assert len(received_b) == count and uri not in session.diagnostics
            assert saved[-1] == (uri, [])
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_ide_lsp_cancellation_timeout_send_failure_and_stale_response(fast_lsp, repo):
    manager, row = fast_lsp

    async def flow():
        session = await manager.session_for(row, "python")
        try:
            uri = file_uri(repo / "pkg/core.py")
            await manager.handle_client_notification(session, _open(uri))
            assigned = []
            request = {"id": "browser-id", "method": "test/wait", "params": {"textDocument": {"uri": uri}}}
            task = asyncio.create_task(manager.handle_client_request(session, request, on_request_id=assigned.append))
            await asyncio.sleep(0)
            assert assigned and assigned[0] != request["id"]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert session.client.sent[-1] == {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": assigned[0]}}
            assert not session.client._pending
            session.client._dispatch({"id": assigned[0], "result": "late ignored"})
            assigned.clear()
            task = asyncio.create_task(manager.handle_client_request(session, request, on_request_id=assigned.append))
            await asyncio.sleep(0)
            await manager.handle_client_notification(session, _change(uri, 2))
            session.client._dispatch({"id": assigned[0], "result": "stale edits"})
            assert (await task)["error"]["code"] == -32801
            timed = await manager.handle_client_request(session, {**request, "timeout": 0.001})
            assert "请求超时" in timed["error"]["message"]
            assert not session.client._pending and session.client.sent[-1]["method"] == "$/cancelRequest"
            session.client.fail_send = True
            failed = await manager.handle_client_request(session, {"id": 2, "method": "test/ping"})
            assert "fixture send failure" in failed["error"]["message"] and not session.client._pending
        finally:
            await manager.stop_all()
    asyncio.run(flow())


@pytest.mark.parametrize("capability, mode, forwarded", [
    (None, "full", False),
    ({"legend": {"tokenTypes": [], "tokenModifiers": []}}, "range", False),
    ({"full": True}, "full", True),
    ({"full": {}}, "full", True),
    ({"full": {"delta": True}}, "full", True),
    ({"full": True}, "range", False),
    ({"range": True}, "range", True),
    ({"range": {}}, "range", True),
    ({"range": False}, "range", False),
])
def test_ide_semantic_tokens_obey_server_capability(fast_lsp, repo, capability, mode, forwarded):
    manager, row = fast_lsp

    async def flow():
        session = await manager.session_for(row, "python")
        try:
            if capability is not None:
                session.capabilities["semanticTokensProvider"] = capability
            uri = file_uri(repo / "pkg/core.py")
            await manager.handle_client_notification(session, _open(uri, 2))
            method = f"textDocument/semanticTokens/{mode}"
            payload = {"data": [0, 0, 1, 8, 4], "resultId": "fixture-v2"}
            session.client.results[method] = payload
            params = {"textDocument": {"uri": uri}}
            if mode == "range":
                params["range"] = {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 0}}
            result = await manager.handle_client_request(session, {"id": "semantic", "method": method, "params": params})
            assert result["id"] == "semantic" and result["result"] == (payload if forwarded else None)
            sent = [message for message in session.client.sent if message.get("method") == method]
            assert bool(sent) is forwarded
            if forwarded:
                assert sent[0]["params"] == params
            rejected = await manager.handle_client_request(session, {"id": "outside", "method": method,
                "params": {"textDocument": {"uri": file_uri(repo.parent / "outside.py")}}})
            assert rejected["error"]["code"] == -32602
        finally:
            await manager.stop_all()

    asyncio.run(flow())


def test_ide_lsp_workspace_edits_require_review_and_never_write(fast_lsp, repo):
    manager, row = fast_lsp

    async def flow():
        session = await manager.session_for(row, "python")
        try:
            target = repo / "pkg/core.py"
            original = target.read_bytes()
            uri = file_uri(target)
            await manager.handle_client_notification(session, _open(uri, 3))
            edits = [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "newText": "proposal"}]
            safe = {"documentChanges": [{"textDocument": {"uri": uri, "version": 3}, "edits": edits}]}
            notices = []
            manager.subscribe(session, notices.append)
            result = manager._on_server_request(session, {"id": 9002, "method": "workspace/applyEdit", "params": {"edit": safe}})
            assert result["applied"] is False
            assert notices[0]["method"] == "code-agent/reviewEdit" and notices[0]["params"]["edit"] == safe
            for unsafe in ({"changes": {file_uri(repo.parent / "outside.py"): edits}},
                           {"documentChanges": [{"kind": "create", "uri": uri, "options": {"overwrite": True}}]},
                           {"documentChanges": [{"textDocument": {"uri": uri, "version": 2}, "edits": edits}]}):
                result = manager._on_server_request(session, {"method": "workspace/applyEdit", "params": {"edit": unsafe}})
                assert result["applied"] is False and len(notices) == 1
                session.client.results["textDocument/rename"] = unsafe
                rejected = await manager.handle_client_request(session, {"id": 1, "method": "textDocument/rename", "params": {"textDocument": {"uri": uri}}})
                assert "error" in rejected
            blocked = await manager.handle_client_request(session, {"id": 4, "method": "workspace/executeCommand", "params": {"command": "anything"}})
            assert blocked["error"]["code"] == -32602
            assert not any(msg.get("method") == "workspace/executeCommand" for msg in session.client.sent)
            assert target.read_bytes() == original
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_advanced_features_resource_edits_and_review_command(fast_lsp, repo):
    manager, row = fast_lsp

    async def flow():
        session = await manager.session_for(row, "python")
        try:
            uri = file_uri(repo / "pkg/core.py")
            await manager.handle_client_notification(session, _open(uri, 4))
            for method, capability in (
                ("textDocument/prepareCallHierarchy", "callHierarchyProvider"),
                ("textDocument/prepareTypeHierarchy", "typeHierarchyProvider"),
                ("textDocument/codeLens", "codeLensProvider"),
                ("textDocument/inlayHint", "inlayHintProvider"),
            ):
                session.capabilities.pop(capability, None)
                message = {"id": 11, "method": method, "params": {"textDocument": {"uri": uri}}}
                before = len(session.client.sent)
                assert (await manager.handle_client_request(session, message))["result"] is None
                assert len(session.client.sent) == before
                session.capabilities[capability] = True
                session.client.results[method] = []
                assert (await manager.handle_client_request(session, message))["result"] == []
                assert session.client.sent[-1]["method"] == method
            new_uri = file_uri(repo / "pkg/moved.py")
            resources = {"documentChanges": [
                {"kind": "rename", "oldUri": uri, "newUri": new_uri},
                {"textDocument": {"uri": new_uri, "version": None}, "edits": []},
                {"kind": "create", "uri": file_uri(repo / "pkg/new.py")},
                {"kind": "delete", "uri": new_uri},
            ]}
            assert manager._check_workspace_edit(session, resources) == resources
            assert not (repo / "pkg/moved.py").exists()
            for invalid in (
                {"kind": "rename", "oldUri": uri, "newUri": file_uri(repo.parent / "outside.py")},
                {"kind": "delete", "uri": uri, "options": {"recursive": True}},
            ):
                with pytest.raises(JsonRpcError):
                    manager._check_workspace_edit(session, {"documentChanges": [invalid]})
            session.server_id = "jdtls"
            session.capabilities["executeCommandProvider"] = {"commands": ["java.edit.organizeImports", "dangerous"]}
            command = {"id": 12, "method": "workspace/executeCommand", "params": {
                "command": "java.edit.organizeImports", "arguments": [uri]}}
            session.client.results["workspace/executeCommand"] = resources
            assert (await manager.handle_client_request(session, command))["result"] == resources
            for params in (None, {"command": "dangerous", "arguments": [uri]},
                           {"command": "java.edit.organizeImports", "arguments": [uri, "extra"]},
                           {"command": "java.edit.organizeImports", "arguments": [file_uri(repo.parent / "outside.py")]}):
                assert "error" in await manager.handle_client_request(session, {**command, "params": params})
            session.client.results["workspace/executeCommand"] = {"changes": {file_uri(repo.parent / "outside.py"): []}}
            assert "error" in await manager.handle_client_request(session, command)
            messages = []
            manager.subscribe(session, messages.append)
            assert manager._on_server_request(session, {"id": 15, "method": "workspace/codeLens/refresh"}) is None
            assert messages[-1]["method"] == "workspace/codeLens/refresh"
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_java_primary_type_rename_requires_a_file_move_without_misclassifying_references(fast_lsp, repo):
    manager, row = fast_lsp

    async def flow():
        session = await manager.session_for(row, "python")
        try:
            session.server_id = "jdtls"
            session.capabilities.update({"renameProvider": True, "documentSymbolProvider": True, "definitionProvider": True})
            main, caller = file_uri(repo / "Main.java"), file_uri(repo / "Use.java")
            declaration = {"start": {"line": 0, "character": 13}, "end": {"line": 0, "character": 17}}
            top = {"name": "Main", "kind": 5, "selectionRange": declaration}
            change = {"textDocument": {"uri": main, "version": None}, "edits": [{"range": declaration, "newText": "RenamedMain"}]}
            request = {"id": 21, "method": "textDocument/rename", "params": {"textDocument": {"uri": caller}, "position": {"line": 2, "character": 4}, "newName": "RenamedMain"}}
            session.client.results["textDocument/rename"] = {"documentChanges": [change]}
            session.client.results["textDocument/definition"] = [{"uri": main, "range": declaration}]
            session.client.results["textDocument/documentSymbol"] = [top]
            rejected = await manager.handle_client_request(session, request)
            assert rejected["error"]["code"] == -32000
            assert "文件移动" in rejected["error"]["message"]
            assert not (repo / "RenamedMain.java").exists()

            complete = {"documentChanges": [change, {"kind": "rename", "oldUri": main, "newUri": file_uri(repo / "RenamedMain.java")}]}
            session.client.results["textDocument/rename"] = complete
            assert (await manager.handle_client_request(session, request))["result"] == complete

            # A compressed edit in Use.java can cover its class declaration
            # while changing references only. Definition points to Main.java.
            session.client.results["textDocument/rename"] = {"changes": {caller: [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 5, "character": 0}}, "newText": "public class Use { RenamedMain ref; }"}]}}
            session.client.results["textDocument/documentSymbol"] = [{"name": "Use", "kind": 5, "selectionRange": {"start": {"line": 0, "character": 13}, "end": {"line": 0, "character": 16}}}]
            assert "result" in await manager.handle_client_request(session, request)

            # A method edit at a child selection range must remain usable.
            method_range = {"start": {"line": 1, "character": 15}, "end": {"line": 1, "character": 24}}
            session.client.results["textDocument/documentSymbol"] = [{**top, "children": [{"name": "increment", "kind": 6, "selectionRange": method_range}]}]
            session.client.results["textDocument/definition"] = [{"uri": main, "range": method_range}]
            session.client.results["textDocument/rename"] = {"changes": {main: [{"range": method_range, "newText": "incrementValue"}]}}
            request["params"]["newName"] = "incrementValue"
            assert "result" in await manager.handle_client_request(session, request)

            # Whole-file edits still identify a primary type through the
            # server's definition; no Java source regex is involved.
            session.client.results["textDocument/definition"] = [{"uri": main, "range": declaration}]
            session.client.results["textDocument/rename"] = {"changes": {main: [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 5, "character": 0}}, "newText": "public class RenamedMain {}"}]}}
            request["params"]["newName"] = "RenamedMain"
            assert "error" in await manager.handle_client_request(session, request)
        finally:
            await manager.stop_all()

    asyncio.run(flow())


def test_java_type_rename_validation_cancels_its_active_query_and_rejects_stale_documents(fast_lsp, repo):
    manager, row = fast_lsp

    async def flow():
        session = await manager.session_for(row, "python")
        original_request = manager.request
        try:
            session.server_id = "jdtls"
            session.capabilities.update({"renameProvider": True, "documentSymbolProvider": True, "definitionProvider": False})
            uri = file_uri(repo / "Main.java")
            await manager.handle_client_notification(session, _open(uri, 1))
            declaration = {"start": {"line": 0, "character": 13}, "end": {"line": 0, "character": 17}}
            session.client.results["textDocument/rename"] = {"changes": {uri: [{"range": declaration, "newText": "NewMain"}]}}
            message = {"id": 22, "method": "textDocument/rename", "params": {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 14}, "newName": "NewMain"}}
            entered = asyncio.Event()

            async def held_query(connection, method, params, **options):
                if method == "textDocument/documentSymbol":
                    options["on_request_id"](700)
                    entered.set()
                    await asyncio.Event().wait()
                return await original_request(connection, method, params, **options)

            manager.request = held_query
            task = asyncio.create_task(manager.handle_client_request(session, message))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            cancelled = [sent for sent in session.client.sent if sent.get("method") == "$/cancelRequest"]
            assert cancelled[-1]["params"]["id"] == 700

            async def stale_query(connection, method, params, **options):
                if method == "textDocument/documentSymbol":
                    session.documents[uri] = 2
                    return [{"name": "Main", "kind": 5, "selectionRange": declaration}]
                return await original_request(connection, method, params, **options)

            manager.request = stale_query
            rejected = await manager.handle_client_request(session, message)
            assert "文档已变化" in rejected["error"]["message"]
            assert not (repo / "NewMain.java").exists()
        finally:
            manager.request = original_request
            await manager.stop_all()

    asyncio.run(flow())


def test_ide_lsp_windows_uri_aliases_are_forwarded_as_editor_uri(fast_lsp, repo):
    import os
    manager, row = fast_lsp

    async def flow():
        session = await manager.session_for(row, "python")
        try:
            uri = file_uri(repo / "pkg/core.py")
            alias = uri.replace("core.py", "core%2Epy")
            if os.name == "nt":
                drive = repo.drive
                alias = alias.replace(drive, drive[0].lower() + "%3A")
            await manager.handle_client_notification(session, _open(uri, 3))
            frames = []
            manager.subscribe(session, frames.append)
            manager._on_notification(session, {"method": "textDocument/publishDiagnostics", "params": {
                "uri": alias, "version": 3, "diagnostics": []}})
            assert frames[-1]["params"]["uri"] == uri
            edit = {"changes": {alias: [{"range": {"start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1}}, "newText": "y"}]}}
            session.client.results["textDocument/rename"] = edit
            result = await manager.handle_client_request(session, {"id": 1, "method": "textDocument/rename",
                "params": {"textDocument": {"uri": uri}}})
            assert list(result["result"]["changes"]) == [uri]
            manager._on_server_request(session, {"id": 5, "method": "workspace/applyEdit", "params": {"edit": edit}})
            assert list(frames[-1]["params"]["edit"]["changes"]) == [uri]
            assert list(edit["changes"]) == [alias]  # Opaque server response is not mutated.
        finally:
            await manager.stop_all()
    asyncio.run(flow())


def test_ide_lsp_companion_diagnostics_aggregate_close_and_stop(fast_lsp, repo):
    manager, row = fast_lsp
    snapshots = []
    manager._on_diagnostics = lambda project, uri, items: snapshots.append(list(items))

    async def flow():
        try:
            primary = await manager.session_for(row, "python")
            companion = await manager.session_for(row, "python", server_id="ruff")
            uri = file_uri(repo / "pkg/core.py")
            for session in (primary, companion):
                await manager.handle_client_notification(session, _open(uri))

            def publish(session, title):
                manager._on_notification(session, {"method": "textDocument/publishDiagnostics", "params": {
                    "uri": uri, "version": 1, "diagnostics": [{"message": title, "source": session.server_id,
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}]}})

            publish(primary, "type error")
            publish(companion, "unused import")
            assert {item["source"] for item in snapshots[-1]} == {"fake", "ruff"}
            await manager.close_document(companion, uri)
            assert [item["message"] for item in snapshots[-1]] == ["type error"]
            await manager.handle_client_notification(companion, _open(uri))
            publish(companion, "unused import restored")
            assert len(snapshots[-1]) == 2
            await manager.stop(primary.key)
            assert [item["message"] for item in snapshots[-1]] == ["unused import restored"]
            await manager.stop(companion.key)
            assert snapshots[-1] == []
        finally:
            await manager.stop_all()
    asyncio.run(flow())


@pytest.fixture
def fast_lsp_api(fast_lsp):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from code_agent.api.code_api import mount_code_api
    from code_agent.api.deps import CurrentUser
    manager, row = fast_lsp

    class OwnedService:
        def get_owned(self, user_id, project_id):
            if user_id != row["user_id"] or project_id != row["id"]:
                raise KeyError("not owned")
            return row

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await manager.stop_all()

    app = FastAPI(lifespan=lifespan)
    app.state.auth_enabled = False
    app.state.local_user = CurrentUser(user_id="u_local", username="local", role="admin")
    mount_code_api(app, project_repo=None, manager=None, service=OwnedService(), lsp=manager)
    with TestClient(app) as client:
        yield client, manager, row


def _receive_until(ws, predicate):
    for _ in range(30):
        message = ws.receive_json()
        if predicate(message):
            return message
    raise AssertionError("expected frame not received")


def test_ide_lsp_ws_concurrent_change_cancel_and_subscriber_isolation(fast_lsp_api, repo):
    client, manager, row = fast_lsp_api
    endpoint = f"/api/v1/code/projects/{row['id']}/lsp/stream?lang=python"
    uri = file_uri(repo / "pkg/core.py")
    with client.websocket_connect(endpoint) as first:
        assert first.receive_json()["params"]["environment"]["python"] == sys.executable
        first.send_json(_open(uri))
        _receive_until(first, lambda m: m.get("method") == "textDocument/publishDiagnostics")
        with client.websocket_connect(endpoint) as second:
            second.receive_json()
            first.send_json({"id": "same-id", "method": "test/wait", "params": {"tag": "first"}})
            a = _receive_until(first, lambda m: m.get("method") == "test/started")["params"]["actual_id"]
            second.send_json({"id": "same-id", "method": "test/wait", "params": {"tag": "second"}})
            b = _receive_until(second, lambda m: m.get("method") == "test/started" and m["params"]["tag"] == "second")["params"]["actual_id"]
            assert a != b
            first.send_json(_change(uri, 2))
            diag = _receive_until(first, lambda m: m.get("method") == "textDocument/publishDiagnostics")
            assert diag["params"]["version"] == 2  # Slow requests never block didChange.
            first.send_json({"method": "$/cancelRequest", "params": {"id": "same-id"}})
            assert _receive_until(first, lambda m: m.get("id") == "same-id")["error"]["code"] == -32800
            second.send_json({"id": "release", "method": "test/release", "params": {"target": b, "value": "second survives"}})
            assert _receive_until(second, lambda m: m.get("id") == "same-id")["result"] == "second survives"
            session = next(iter(manager._sessions.values()))
            cancellations = [msg["params"]["id"] for msg in session.client.sent if msg.get("method") == "$/cancelRequest"]
            assert cancellations == [a]
        first.send_json(_change(uri, 3))
        assert _receive_until(first, lambda m: m.get("method") == "textDocument/publishDiagnostics")["params"]["version"] == 3
        first.send_json(_change(uri, 10, "refresh pending draft"))
        assert _receive_until(first, lambda m: m.get("method") == "textDocument/publishDiagnostics")["params"]["version"] == 10
    assert session.alive and session.documents[uri] == 10 and not session.listeners
    with client.websocket_connect(endpoint) as reconnected:
        ready = reconnected.receive_json()["params"]
        assert ready["diagnostic_versions"][uri] == 10
        assert ready["document_versions"][uri] == 10
        reconnected.send_json(_open(uri, 11, "fresh page content"))
        assert _receive_until(reconnected, lambda m: m.get("method") == "textDocument/publishDiagnostics")["params"]["version"] == 11
        reconnected.send_json({"id": 9, "method": "test/ping"})
        assert _receive_until(reconnected, lambda m: m.get("id") == 9)["result"] is None
    assert len(manager._sessions) == 1 and not session.client._pending


def test_ide_lsp_ws_selector_metadata_invalid_uri_and_owner(fast_lsp_api, repo):
    from starlette.websockets import WebSocketDisconnect
    from code_agent.api.deps import CurrentUser
    client, manager, row = fast_lsp_api
    base = f"/api/v1/code/projects/{row['id']}/lsp"
    assert client.get(base).json()["environment"]["interpreter_source"] == "backend"
    with client.websocket_connect(base + "/stream?lang=python&server=ruff") as ws:
        assert ws.receive_json()["params"]["server"] == "ruff"
        ws.send_json(_open(file_uri(repo.parent / "outside.py")))
        assert ws.receive_json()["method"] == "code-agent/error"
        ws.send_json({"id": 1, "method": "textDocument/hover", "params": {"textDocument": {"uri": file_uri(repo.parent / "outside.py")}}})
        assert ws.receive_json()["error"]["code"] == -32602
    with client.websocket_connect(base + "/stream?lang=javascript&server=ruff") as ws:
        assert ws.receive_json()["method"] == "code-agent/error"
    client.app.state.local_user = CurrentUser(user_id="other-owner", username="other", role="user")
    assert client.get(base).status_code == 404
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(base + "/stream?lang=python") as ws:
            ws.receive_json()


def test_ide_lsp_real_stdio_cancel_uses_server_id_and_cleans_pending(tmp_path):
    script = tmp_path / "cancel_lsp.py"
    script.write_text('''import json, sys
cancelled = []
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line: sys.exit(0)
        if line in (b"\\r\\n", b"\\n"): break
        key, _, val = line.decode().partition(":")
        headers[key.lower()] = val.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    method = message.get("method")
    if method == "$/cancelRequest": cancelled.append(message["params"]["id"])
    elif "id" in message and method != "test/wait":
        result = cancelled if method == "test/cancelled" else None
        body = json.dumps({"jsonrpc":"2.0", "id":message["id"], "result": result}).encode()
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\\r\\n\\r\\n" + body)
        sys.stdout.buffer.flush()
''', encoding="utf-8")

    async def flow():
        client = JsonRpcStdio(sys.executable, [str(script)], framing="content-length", name="cancel-fixture")
        session = LspSession(key="fixture", server_id="fixture", lang="python", project_id="p", tree_id="main",
                             root=tmp_path, command=sys.executable, args=[str(script)], client=client)
        manager = LspManager()
        await client.start()
        try:
            actual = []
            task = asyncio.create_task(manager.handle_client_request(session, {"id": "browser-99", "method": "test/wait"}, on_request_id=actual.append))
            await asyncio.sleep(0)
            await client.request("test/barrier", timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await client.request("test/cancelled", timeout=3) == actual
            assert len(actual) == 1 and actual[0] != "browser-99" and not client._pending
        finally:
            await client.stop()
    asyncio.run(flow())
