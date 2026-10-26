"""记忆系统单测（s10/s11/s12）：读写、隔离、检索。"""

from code_agent.memory.retriever import retrieve
from code_agent.memory.store import UserMemory, WorkspaceMemory
from code_agent.memory.tools_save import SaveMemoryTool
from code_agent.tools.base import ToolContext


def test_workspace_daily_log_and_curated(agent_home, workspace):
    wm = WorkspaceMemory(workspace)
    wm.log_daily("跑了部署演练")
    wm.log_daily("修复压缩 bug")
    wm.save_fact("本项目用 SQLite 存储")
    log = list((workspace / "memory").glob("*.md"))
    assert len(log) == 2  # daily + MEMORY.md
    curated = wm.read_curated()
    assert "SQLite" in curated
    entries = wm.entries()
    assert entries == ["本项目用 SQLite 存储"]


def test_user_memory_isolated(agent_home):
    UserMemory("alice").save_fact("偏好简体中文回答")
    UserMemory("bob").save_fact("喜欢表格输出")
    assert "简体中文" in UserMemory("alice").read_curated()
    assert "表格" not in UserMemory("alice").read_curated()
    assert "表格" in UserMemory("bob").read_curated()


def test_retriever_ranking():
    entries = [
        ("user", "用户偏好 DeepSeek 模型"),
        ("workspace", "项目使用 SQLite 存储数据"),
        ("user", "部署目标是内网 Linux 服务器"),
    ]
    hits = retrieve(entries, "服务器部署用什么系统", top_k=2)
    assert len(hits) <= 2
    assert hits and "部署" in hits[0].text or "服务器" in hits[0].text
    # 无关 query 不返回
    assert retrieve(entries, "量子纠缠的薛定谔方程", top_k=3) == []


def test_save_memory_tool(agent_home, workspace):
    ctx = ToolContext(session_id="s1", workspace=workspace, user_id="alice")
    tool = SaveMemoryTool()
    r1 = tool and __import__("asyncio").run(tool.execute({"scope": "user", "content": "用户偏好表格输出"}, ctx))
    assert "用户记忆" in r1
    r2 = __import__("asyncio").run(tool.execute({"scope": "workspace", "content": "工作区约定"}, ctx))
    assert "工作区记忆" in r2
    assert "表格输出" in UserMemory("alice").read_curated()
    assert "工作区约定" in WorkspaceMemory(workspace).read_curated()
    # 空内容拒绝
    r3 = __import__("asyncio").run(tool.execute({"content": "  "}, ctx))
    assert "不能为空" in r3
