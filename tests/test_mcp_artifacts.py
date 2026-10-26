"""MCP 连接器（s17）与交付（s20）测试：fake server 全链路 + trust 门禁 + artifacts。"""

import asyncio
import json
import sys
from pathlib import Path

from code_agent.extensions.artifacts import ArtifactsStore, DeliverFileTool
from code_agent.extensions.mcp_client import McpManager
from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import AllowPolicy, Decision
from code_agent.tools.base import ToolContext

# 用 __file__ 定位 fake server：以前这里写死了仓库的绝对路径（E:\...\tests\fixtures\...），
# 仓库目录一改名/换机器就找不到文件，而失败现象是"连上了但 0 个工具"，很难查。
FAKE_SERVER = str(Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py")


def _mcp_config(agent_home, trusted: bool):
    cfg = {
        "mcpServers": {
            "fake": {
                "command": sys.executable,
                "args": [FAKE_SERVER],
                "trusted": trusted,
            }
        }
    }
    p = agent_home / "mcp.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def _ctx(root):
    return ToolContext(session_id="s", workspace=root)


def test_mcp_connect_list_call(agent_home):
    """注意：subprocess transport 绑定事件循环，全流程必须单 asyncio.run。"""

    async def flow():
        mgr = McpManager(_mcp_config(agent_home, trusted=False))
        try:
            tools = await mgr.connect_all()
            assert len(tools) == 1
            tool, trusted = tools[0]
            assert tool.name == "mcp__fake__echo"
            assert not trusted
            assert "[mcp:fake]" in tool.description
            result = await tool.execute({"text": "你好MCP"}, _ctx(agent_home))
            assert result == "echo: 你好MCP"
        finally:
            await mgr.stop_all()

    asyncio.run(flow())


def test_mcp_untrusted_needs_approval(agent_home):
    """未信任 MCP 工具：无专用策略 -> need_approval（信任门禁生效）。"""

    async def flow():
        mgr = McpManager(_mcp_config(agent_home, trusted=False))
        try:
            tools = await mgr.connect_all()
            tool, _ = tools[0]
            pm = PermissionManager(workspace_root=agent_home)
            r = await pm.evaluate(tool.name, {"text": "x"}, _ctx(agent_home))
            assert r.decision == Decision.need_approval
        finally:
            await mgr.stop_all()

    asyncio.run(flow())


def test_mcp_trusted_allow_policy(agent_home):
    """trusted=true：AllowPolicy 直通。"""

    async def flow():
        mgr = McpManager(_mcp_config(agent_home, trusted=True))
        try:
            tools = await mgr.connect_all()
            tool, trusted = tools[0]
            assert trusted
            pm = PermissionManager(workspace_root=agent_home)
            pm.register_policy(tool.name, AllowPolicy())
            r = await pm.evaluate(tool.name, {"text": "x"}, _ctx(agent_home))
            assert r.decision == Decision.allow
        finally:
            await mgr.stop_all()

    asyncio.run(flow())


def test_mcp_missing_server_skipped(agent_home):
    """连不上的 server 跳过，不阻塞启动。"""
    p = agent_home / "mcp.json"
    p.write_text(
        json.dumps({"mcpServers": {"dead": {"command": "no_such_binary_xyz", "args": []}}}),
        encoding="utf-8",
    )
    mgr = McpManager(p)
    tools = asyncio.run(mgr.connect_all())
    assert tools == []


# ---------------------------------------------------------------------------
# artifacts（s20）
# ---------------------------------------------------------------------------

def test_artifacts_store_save_and_list(agent_home):
    store = ArtifactsStore(agent_home / "artifacts")
    path = store.save("s1", "报告.md", "# 内容")
    assert path.exists()
    rows = store.list("s1")
    assert rows[0]["filename"] == "报告.md"
    assert rows[0]["size"] > 0


def test_artifacts_filename_sanitized(agent_home):
    store = ArtifactsStore(agent_home / "artifacts")
    path = store.save("s1", "../../etc/passwd", "x")
    # 路径穿越被拦：落在 session 目录内
    assert path.parent == store.root / "s1"
    assert path.name == "passwd"


def test_deliver_file_tool(agent_home, workspace):
    store = ArtifactsStore(agent_home / "artifacts")
    tool = DeliverFileTool(store)
    ctx = _ctx(workspace)
    out = asyncio.run(tool.execute({"filename": "out.svg", "content": "<svg></svg>"}, ctx))
    assert "[artifact]" in out
    rows = store.list(ctx.session_id)
    assert rows and rows[0]["filename"] == "out.svg"
    # 空 content 拒绝
    try:
        asyncio.run(tool.execute({"filename": "a.txt", "content": ""}, ctx))
        assert False
    except Exception as e:
        assert "不能为空" in str(e)
