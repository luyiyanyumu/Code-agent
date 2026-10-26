"""Skills / Experts 系统（s16/s18）单测。"""

from code_agent.extensions.packs import ExpertsRegistry, LoadPackTool, SkillsRegistry
from code_agent.tools.base import ToolContext


def _make_skill(agent_home, name: str, description: str, body: str):
    d = agent_home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def _make_expert(agent_home, name: str, description: str, body: str):
    d = agent_home / "experts" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "EXPERT.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_skills_scan_and_lazy_load(agent_home):
    _make_skill(agent_home, "weekly-report", "生成周报的固定流程", "# 步骤\n1. 拉取数据")
    _make_skill(agent_home, "sql-tuning", "SQL 优化清单", "先看执行计划")

    reg = SkillsRegistry(agent_home / "skills")
    items = reg.list()
    assert {i["name"] for i in items} == {"weekly-report", "sql-tuning"}

    # lazy load：正文只在 load 时返回
    body = reg.load("weekly-report")
    assert "拉取数据" in body
    assert reg.load("ghost") is None


def test_skill_catalog_text(agent_home):
    _make_skill(agent_home, "demo", "演示技能", "内容")
    reg = SkillsRegistry(agent_home / "skills")
    text = reg.catalog_text()
    assert "demo" in text and "演示技能" in text
    # 空目录 -> 空目录文本
    assert SkillsRegistry(agent_home / "skills" / "none").catalog_text() == ""


def test_load_skill_tool(agent_home, workspace):
    _make_skill(agent_home, "demo", "演示", "技能正文ABC")
    reg = SkillsRegistry(agent_home / "skills")
    tool = LoadPackTool(reg)
    assert tool.name == "load_skill"
    ctx = ToolContext(session_id="s", workspace=workspace)
    import asyncio

    out = asyncio.run(tool.execute({"name": "demo"}, ctx))
    assert "技能正文ABC" in out
    out2 = asyncio.run(tool.execute({"name": "ghost"}, ctx))
    assert "未找到" in out2


def test_load_skill_lists_resources(agent_home, workspace):
    """技能可附带脚本 / 参考资料：load_skill 返回正文 + 资源清单。"""
    import asyncio

    _make_skill(agent_home, "with-res", "带资源", "正文见附带脚本")
    d = agent_home / "skills" / "with-res"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")
    (d / "references").mkdir(parents=True)
    (d / "references" / "guide.md").write_text("# 指南", encoding="utf-8")

    reg = SkillsRegistry(agent_home / "skills")
    assert set(reg.resource_files("with-res")) == {"scripts/run.py", "references/guide.md"}

    tool = LoadPackTool(reg)
    ctx = ToolContext(session_id="s", workspace=workspace)
    out = asyncio.run(tool.execute({"name": "with-res"}, ctx))
    assert "正文见附带脚本" in out
    assert "scripts/run.py" in out
    assert "references/guide.md" in out


def test_experts_registry(agent_home):
    _make_expert(agent_home, "cost-expert", "造价专家", "你是资深造价工程师")
    reg = ExpertsRegistry(agent_home / "experts")
    assert reg.names() == ["cost-expert"]
    assert "造价工程师" in reg.load("cost-expert")
    tool = LoadPackTool(reg)
    assert tool.name == "load_expert"
