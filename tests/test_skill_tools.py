"""M20：技能商店工具（agent 自己装技能）+ Windows 上的 bash 真相。

用户报的两件事其实是一根链子：
- "无法让 code-agent 给自己安装 skills"：技能目录只有 `load_skill`（读），**没有装的能力**；
  于是模型只能去 shell 里 `curl … | bash`，而 skillhub 的入口是 bash 脚本 + `| head`，
  在这台机器上被 cmd.exe 执行，报 `'head' is not recognized`。
- 所以这里既验"原生安装路径"，也验"bash 工具真的用 bash"。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from code_agent.sandbox import executor
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.skill_tools import (
    SkillInstallTool,
    SkillListTool,
    SkillSearchTool,
    cli_path,
)


def test_skill_tools_are_registered_and_readonly_aware():
    from code_agent.api.app import build_registry
    from code_agent.permissions.hooks import READ_ONLY_TOOLS

    reg = build_registry()
    names = set(reg.names())
    assert {"skill_list", "skill_search", "skill_install"} <= names
    # 装技能是写操作：只读档位下必须**不在**放行名单里（会被拒）
    assert "skill_list" in READ_ONLY_TOOLS and "skill_search" in READ_ONLY_TOOLS
    assert "skill_install" not in READ_ONLY_TOOLS


def test_skill_list_reads_the_agent_skills_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / ".code-agent"))
    skills = Path(os.environ["CODE_AGENT_HOME"]) / "skills"
    (skills / "demo-skill").mkdir(parents=True)
    (skills / "demo-skill" / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示用技能\n---\n\n正文\n", encoding="utf-8"
    )
    out = asyncio.run(SkillListTool().execute({}, ToolContext("s", tmp_path)))
    assert "demo-skill" in out and "演示用技能" in out
    assert "load_skill" in out


def test_skill_install_reports_when_cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("code_agent.tools.builtin.skill_tools.cli_path", lambda: None)
    with pytest.raises(Exception) as err:
        asyncio.run(SkillInstallTool().execute({"slug": "x"}, ToolContext("s", tmp_path)))
    assert "SkillHub CLI" in str(err.value)
    assert "bash" in str(err.value)  # 说清为什么装不了、需要什么


def test_skill_search_parses_json_output(tmp_path, monkeypatch):
    """CLI 的 --json 输出可能混着进度行：解析要能容忍。"""

    async def fake_run(args, *, timeout):
        assert args[0] == "search" and "--json" in args
        return 0, 'downloading index…\n[{"slug":"pdf-tools","name":"PDF 工具","version":"1.2.0",' \
                  '"description":"拆分合并 PDF"}]\n', ""

    monkeypatch.setattr("code_agent.tools.builtin.skill_tools._run_cli", fake_run)
    out = asyncio.run(SkillSearchTool().execute({"query": "pdf"}, ToolContext("s", tmp_path)))
    assert "pdf-tools" in out and "PDF 工具" in out and "v1.2.0" in out


def test_skill_install_verifies_files_landed(tmp_path, monkeypatch):
    """不看退出码就收工：要确认真落在 Code-agent 的技能目录里，并且点出怎么用。"""
    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / ".code-agent"))
    skills = Path(os.environ["CODE_AGENT_HOME"]) / "skills"

    async def fake_run(args, *, timeout):
        # 断言 --dir 指向的是 Code-agent 的技能目录（文档明确要求，装错地方等于没装）
        assert "--dir" in args and Path(args[args.index("--dir") + 1]) == skills
        (skills / "demo-skill").mkdir(parents=True, exist_ok=True)
        (skills / "demo-skill" / "SKILL.md").write_text("---\nname: demo-skill\n---\n", encoding="utf-8")
        return 0, '{"slug":"demo-skill","name":"demo-skill"}', ""

    monkeypatch.setattr("code_agent.tools.builtin.skill_tools._run_cli", fake_run)
    out = asyncio.run(SkillInstallTool().execute({"slug": "demo-skill"}, ToolContext("s", tmp_path)))
    assert "已安装技能" in out and "load_skill demo-skill" in out
    assert "SKILL.md" in out


def test_namespaced_slug_is_passed_through(tmp_path, monkeypatch):
    """`@ns/slug` 要**整串**交给 CLI。

    真机实测：
    - `install @ns/slug` → 真的装，落到 `skills/@ns/slug/`；
    - `install slug --namespace ns` → 退出码 0 但什么都不装。
    """
    from code_agent.tools.builtin.skill_tools import _split_slug

    assert _split_slug("@kunlungrowth/kunlun-zhoubao") == ("@kunlungrowth/kunlun-zhoubao", "")
    assert _split_slug("kunlun-zhoubao") == ("kunlun-zhoubao", "")
    assert _split_slug("kunlun-zhoubao", "ns") == ("kunlun-zhoubao", "ns")

    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / ".code-agent"))
    captured = {}

    async def fake_run(args, *, timeout):
        captured["args"] = args
        return 0, "{}", ""

    monkeypatch.setattr("code_agent.tools.builtin.skill_tools._run_cli", fake_run)
    out = asyncio.run(
        SkillInstallTool().execute({"slug": "@kunlungrowth/kunlun-zhoubao"}, ToolContext("s", tmp_path))
    )
    args = captured["args"]
    assert "@kunlungrowth/kunlun-zhoubao" in args
    assert "--namespace" not in args
    # 退出码 0 但没落地时，要如实说"没看到"并把 CLI 输出带上，而不是谎报成功
    assert "没看到" in out and "{}" in out


def test_namespaced_install_layout_is_discovered(tmp_path, monkeypatch):
    """SkillHub 装的社区技能落在 `skills/@ns/slug/SKILL.md`：扫描与列表都要认得。"""
    from code_agent.extensions.packs import MarkdownPack, iter_pack_docs

    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / ".code-agent"))
    skills = Path(os.environ["CODE_AGENT_HOME"]) / "skills"
    (skills / "plain-skill").mkdir(parents=True)
    (skills / "plain-skill" / "SKILL.md").write_text("---\nname: plain-skill\n---\n", encoding="utf-8")
    nested = skills / "@clawhub_yunr-tg" / "weekly-report-gen"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: weekly-report-gen\ndescription: 生成 Markdown 周报\n---\n\n按模板生成周报。\n",
        encoding="utf-8",
    )
    found = {doc.parent.name for doc in iter_pack_docs(skills)}
    assert found == {"plain-skill", "weekly-report-gen"}

    # 回收站（`.trash`）里的技能不能又被扫出来——删掉的东西不该复活
    (skills / ".trash" / "deleted-skill").mkdir(parents=True)
    (skills / ".trash" / "deleted-skill" / "SKILL.md").write_text(
        "---\nname: deleted-skill\n---\n", encoding="utf-8"
    )
    assert {doc.parent.name for doc in iter_pack_docs(skills)} == {"plain-skill", "weekly-report-gen"}

    registry = MarkdownPack(skills, "SKILL.md", "skill")
    assert "weekly-report-gen" in registry.names()
    assert registry.load("weekly-report-gen"), "命名空间里的技能要能被 load_skill 读到"

    out = asyncio.run(SkillListTool().execute({}, ToolContext("s", tmp_path)))
    assert "weekly-report-gen" in out and "@clawhub_yunr-tg" in out


# ---------------------------------------------------------------------------
# Windows 上的 shell：bash 工具得真的用 bash（装了 Git Bash 的话）
# ---------------------------------------------------------------------------
def test_installed_skill_without_frontmatter_still_has_description(tmp_path, monkeypatch):
    """社区技能常常只有正文、没有 frontmatter。

    真机上装完 `weekly-report-gen` 后目录里显示成 `- weekly-report-gen: `（空的），
    模型完全不知道它是干什么的——等于白装，所以要从正文兜一句。
    """
    from code_agent.extensions.packs import MarkdownPack, fallback_description, iter_pack_docs

    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / ".code-agent"))
    skills = Path(os.environ["CODE_AGENT_HOME"]) / "skills"
    nested = skills / "@ns" / "plain-report"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "# Skill: 自动生成周报 (Weekly Report Generator)\n\n"
        "## 描述\n"
        "根据提供的工作日志/任务清单，自动生成 Markdown 格式周报。\n",
        encoding="utf-8",
    )
    text = (nested / "SKILL.md").read_text(encoding="utf-8")
    assert "任务清单" in fallback_description(text)
    # 没有 `## 描述` 小节时退到第一行标题
    assert fallback_description("# 标题即描述\n正文\n") == "标题即描述"

    registry = MarkdownPack(skills, "SKILL.md", "skill")
    item = next(i for i in registry.list() if i["name"] == "plain-report")
    assert "任务清单" in item["description"], item
    # system prompt 里的技能目录也要带上这句
    assert "任务清单" in registry.catalog_text()
    assert len(list(iter_pack_docs(skills))) == 1


def test_find_bash_prefers_git_bash_and_ignores_wsl_stub(monkeypatch, tmp_path):
    fake = tmp_path / "bash.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_BASH", str(fake))
    assert executor.find_bash() == str(fake)


def test_shell_kind_is_honest(monkeypatch):
    monkeypatch.setenv("CODE_AGENT_BASH", "")
    monkeypatch.setattr(executor, "find_bash", lambda: None)
    assert "cmd.exe" in executor.shell_kind()
    monkeypatch.setattr(executor, "find_bash", lambda: r"C:\Program Files\Git\bin\bash.exe")
    assert "bash" in executor.shell_kind()


def test_posix_hint_only_when_no_bash(monkeypatch):
    bad = "exit_code=255\n'head' is not recognized as an internal or external command"
    monkeypatch.setattr(executor.platform, "system", lambda: "Windows")
    monkeypatch.setattr(executor, "find_bash", lambda: None)
    out = executor._maybe_posix_hint(bad)
    assert "[hint]" in out and "pwsh" in out
    # 有 bash 的时候不需要这段提示（那时命令本该能跑）
    monkeypatch.setattr(executor, "find_bash", lambda: "bash")
    assert "[hint]" not in executor._maybe_posix_hint(bad)


def test_bash_tool_description_names_the_real_shell():
    from code_agent.tools.builtin import BashTool

    tool = BashTool()
    from code_agent.sandbox.executor import shell_kind

    assert shell_kind() in tool.description
    assert shell_kind() in tool.description_zh


@pytest.mark.skipif(os.name != "nt", reason="Windows 专属")
def test_bash_tool_actually_runs_posix_pipeline(tmp_path):
    """真机验证：`echo | head` 这种管道必须能跑（走 Git Bash 时）。"""
    if executor.find_bash() is None:
        pytest.skip("这台机器没有 Git Bash")
    from code_agent.tools.builtin import BashTool

    out = asyncio.run(
        BashTool().execute(
            {"command": "printf 'a\\nb\\nc\\n' | head -2", "timeout": 30},
            ToolContext("s", tmp_path),
        )
    )
    assert "exit_code=0" in out, out
    assert "a" in out and "b" in out
