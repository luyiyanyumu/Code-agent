"""`present` 与 `load_workspace_dependencies`（M43，对齐 `dsh-tool-present` /
`dsh-tool-workspace-dependencies`）。

`present` 存在的理由是**具体的**：`deliver_file` 只能收字符串内容，所以由 `bash`
跑脚本生成的 xlsx / PDF / 图片 / 大 CSV 要么 base64 塞进工具参数（请求体撑爆），
要么交付不了。这里钉住"给路径就能交付"，以及"不许拿它当读边界后门"。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from code_agent.extensions.artifacts import ArtifactsStore, PresentTool
from code_agent.tools.base import ToolContext, ToolError
from code_agent.tools.builtin.env_tools import LoadWorkspaceDependenciesTool


def _ctx(workspace: Path, session: str = "s_present") -> ToolContext:
    return ToolContext(session_id=session, workspace=workspace)


def _present(workspace: Path, files, *, session: str = "s_present") -> str:
    store = ArtifactsStore(workspace.parent / "artifacts")
    return asyncio.run(PresentTool(store).execute({"files": files}, _ctx(workspace, session)))


# ----------------------------------------------------------------------
# present：交付已存在的文件
# ----------------------------------------------------------------------
def test_present_copies_an_existing_file_byte_for_byte(tmp_path) -> None:
    """二进制也要一模一样——交付一个损坏的 xlsx 比不交付更糟。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = bytes(range(256)) * 4          # 含 0x00 等控制字节
    source = workspace / "report.xlsx"
    source.write_bytes(payload)

    out = _present(workspace, [{"path": "report.xlsx", "description": "月度报表"}])
    assert "已交付 1 份文件" in out

    staged = tmp_path / "artifacts" / "s_present" / "report.xlsx"
    assert staged.is_file()
    assert staged.read_bytes() == payload
    # 说明落成旁注文件（交付目录清单本身只读文件名与大小）
    assert (tmp_path / "artifacts" / "s_present" / "report.xlsx.desc.txt").read_text(
        encoding="utf-8"
    ) == "月度报表"


def test_present_accepts_a_bare_string_entry(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.csv").write_text("x\n", encoding="utf-8")
    out = _present(workspace, ["a.csv"])
    assert "已交付 1 份文件" in out
    assert (tmp_path / "artifacts" / "s_present" / "a.csv").is_file()


def test_present_does_not_overwrite_a_previous_deliverable(tmp_path) -> None:
    """同名第二次交付要另存，不能静默盖掉用户已经拿到的那一份。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out.csv").write_text("v1\n", encoding="utf-8")
    _present(workspace, ["out.csv"])

    (workspace / "out.csv").write_text("v2\n", encoding="utf-8")
    _present(workspace, ["out.csv"])

    delivered = sorted(p.name for p in (tmp_path / "artifacts" / "s_present").iterdir())
    assert "out.csv" in delivered
    assert any(name.endswith("-out.csv") for name in delivered), delivered
    assert (tmp_path / "artifacts" / "s_present" / "out.csv").read_text(encoding="utf-8") == "v1\n"


def test_present_refuses_a_directory(tmp_path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    with pytest.raises(ToolError) as err:
        _present(workspace, ["sub"])
    assert "目录" in str(err.value)


def test_present_refuses_a_symlink(tmp_path) -> None:
    """链接指向哪儿不取决于它的名字——交付链接等于把目标也交出去。

    两种指向都要拒：指向工作区外的（否则是读边界的后门）、以及指向工作区内的
    （一个叫 `report.pdf` 的链接完全可能指向 `.env`）。

    注意检查必须发生在 `resolve()` **之前**：`_WorkspaceBound.resolve_read` 会解析
    链接，解析后就是个普通文件，`is_symlink()` 恒为假——我第一版就是这么写的，
    于是拿到了一段永远不执行的死分支，测试当场抓到（DID NOT RAISE）。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()

    outside_target = tmp_path / "outside.txt"
    outside_target.write_text("secret\n", encoding="utf-8")
    outside_link = workspace / "to-outside.txt"
    inside_target = workspace / "inside.txt"
    inside_target.write_text("fine\n", encoding="utf-8")
    inside_link = workspace / "to-inside.txt"
    try:
        os.symlink(outside_target, outside_link)
        os.symlink(inside_target, inside_link)
    except (OSError, NotImplementedError):
        pytest.skip("本环境不允许创建符号链接")

    for name in ("to-outside.txt", "to-inside.txt"):
        with pytest.raises(ToolError) as err:
            _present(workspace, [name])
        assert "符号链接" in str(err.value), (name, str(err.value))


def test_present_refuses_paths_outside_the_read_boundary(tmp_path) -> None:
    """**不许变成读边界的后门**：工作区外的文件不给交付。

    DSH 的 present 允许绝对路径指向工作区外；code-agent 有意更严——M6 把
    "工作区外不读"写成了硬边界，交付工具不能绕过它把任意文件摆进用户的下载列表。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")

    with pytest.raises(ToolError):
        _present(workspace, [str(outside)])
    with pytest.raises(ToolError):
        _present(workspace, ["../secret.txt"])


def test_present_refuses_missing_file(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(ToolError) as err:
        _present(workspace, ["nope.csv"])
    assert "不存在" in str(err.value)


def test_present_rejects_empty_and_oversized_lists(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(ToolError):
        _present(workspace, [])
    with pytest.raises(ToolError) as err:
        _present(workspace, [f"f{i}.txt" for i in range(5)])
    assert "最多" in str(err.value)
    with pytest.raises(ToolError):
        _present(workspace, [{"description": "没有 path"}])


def test_present_handles_several_files_in_one_call(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for name in ("a.csv", "b.pdf", "c.png"):
        (workspace / name).write_bytes(b"data")
    out = _present(workspace, [{"path": "a.csv"}, {"path": "b.pdf"}, {"path": "c.png"}])
    assert "已交付 3 份文件" in out
    for name in ("a.csv", "b.pdf", "c.png"):
        assert (tmp_path / "artifacts" / "s_present" / name).is_file()


# ----------------------------------------------------------------------
# load_workspace_dependencies：报告路径，不执行程序
# ----------------------------------------------------------------------
def test_env_tool_reports_the_agent_python_and_the_workspace_venv(tmp_path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".venv" / "Scripts").mkdir(parents=True)
    fake = workspace / ".venv" / "Scripts" / "python.exe"
    fake.write_text("stub", encoding="utf-8")

    out = asyncio.run(
        LoadWorkspaceDependenciesTool().execute({}, _ctx(workspace))
    )
    import sys

    assert sys.executable in out                      # 当前 agent 自己那只
    assert str(fake) in out                           # 工作区那只
    assert "用法" in out


def test_env_tool_says_not_found_instead_of_inventing_a_path(tmp_path) -> None:
    """找不到就说找不到——假路径会换来一个和真实原因无关的报错。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    out = asyncio.run(LoadWorkspaceDependenciesTool().execute({}, _ctx(workspace)))
    assert "未找到" in out
    assert "node" in out and "pnpm" in out


def test_env_tool_takes_no_arguments() -> None:
    """零参数：schema 里没有 properties，模型不需要提供任何东西。"""
    tool = LoadWorkspaceDependenciesTool()
    assert tool.parameters == {"type": "object", "properties": {}}
    assert tool.timeout is None          # 不设超时：它只查路径，不会挂住
