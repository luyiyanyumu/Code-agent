"""运行时依赖位置查询（对齐 DSH 的 `load_workspace_dependencies`）。

模型要跑 `python` / `node` / `pnpm` 时，过去只能靠猜：猜 PATH 里有没有、猜虚拟环境在
哪个目录、猜 `python` 是系统的那只还是本仓 `.venv` 里那只。猜错的代价是实打实的——
用系统 Python 装依赖会污染全局，用错 Node 版本会得到一个和 CI 不一样的结果。

这个工具把**绝对路径**直接摊开，让它不必猜。三条纪律：

1. **只报告，不执行**。不去跑 `node --version`：那是一次进程启动（还可能过策略
   与沙箱链路），而"路径在哪"本身不需要执行任何东西就能答。Python 的版本例外——
   它是当前进程自己的事实（`sys.version_info`），零成本。
2. **找不到就说找不到**。假路径比没有路径更坏：模型会照着调，然后拿到一个
   与真实原因无关的报错（"命令没找到"常常被它解释成"这个任务做不了"）。
3. **区分"哪一只"**。同一台机器上可能同时有系统 Python 与工作区 `.venv`，
   工具会把两者分开列，并标注哪个是**当前 agent 进程自己在用的那只**。
"""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from code_agent.tools.base import Tool, ToolContext

#: 工作区里可能的虚拟环境（Windows 与 POSIX 两种布局）
_VENV_PYTHONS = (
    Path(".venv") / "Scripts" / "python.exe",
    Path(".venv") / "bin" / "python",
    Path("venv") / "Scripts" / "python.exe",
    Path("venv") / "bin" / "python",
)
#: node 包管理器在仓库内的常见落点（PATH 里没有时的兜底）
_LOCAL_BIN = (
    Path("node_modules") / ".bin" / "pnpm.cmd",
    Path("node_modules") / ".bin" / "pnpm",
    Path("node_modules") / ".bin" / "npm.cmd",
    Path("node_modules") / ".bin" / "npm",
)


def _first_existing(root: Path, candidates: tuple[Path, ...]) -> Path | None:
    for rel in candidates:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


class LoadWorkspaceDependenciesTool(Tool):
    name = "load_workspace_dependencies"
    description = (
        "Report the absolute paths of the runtimes available for this workspace "
        "(the agent's own Python, the workspace virtualenv Python, Node.js, pnpm/npm). "
        "Call this before running language tooling so you use the right interpreter instead of "
        "guessing from PATH. Reports only — it does not execute anything. Read-only."
    )
    description_zh = (
        "报告本工作区可用的运行时**绝对路径**（agent 自身 Python、工作区虚拟环境 Python、"
        "Node.js、pnpm/npm）。跑语言工具链之前先调它，不要靠 PATH 猜。"
        "只报告不执行任何程序；只读。"
    )
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        workspace = Path(ctx.workspace)
        lines: list[str] = [
            f"工作区：{workspace}",
            f"平台：{platform.system()} {platform.release()}（{platform.machine()}）",
        ]

        # —— Python ——
        lines.append("")
        lines.append("Python：")
        lines.append(
            f"- 当前 agent 进程自己在用的：{sys.executable}"
            f"（{platform.python_version()}）"
        )
        venv_python = _first_existing(workspace, _VENV_PYTHONS)
        if venv_python is not None:
            note = "（与上面是同一只）" if _same(venv_python, sys.executable) else ""
            lines.append(f"- 工作区虚拟环境：{venv_python}{note}")
        else:
            lines.append("- 工作区虚拟环境：未找到（本工作区没有 .venv / venv 目录）")

        # —— Node / 包管理器 ——
        lines.append("")
        lines.append("Node.js / 包管理器：")
        node = shutil.which("node")
        lines.append(f"- node：{node}" if node else "- node：未找到（PATH 里没有 node）")
        for name in ("pnpm", "npm"):
            found = shutil.which(name)
            if found:
                lines.append(f"- {name}：{found}")
                continue
            local = _first_existing(workspace, _LOCAL_BIN)
            if local is not None and name in local.name:
                lines.append(f"- {name}：{local}（来自 node_modules/.bin，不是全局安装）")
            else:
                lines.append(f"- {name}：未找到（PATH 里没有，工作区 node_modules/.bin 里也没有）")

        lines.append("")
        available = (ctx.extra or {}).get("available_tools", ())
        if "bash" in available:
            usage = "用 `bash` 调用时记得给路径加引号，并遵守当前执行权限。"
        elif "delegate_agent" in available:
            usage = (
                "当前会话没有 bash；这些路径可直接用于核查环境。"
                "确需运行版本检查、脚本或构建时，委派给执行节点并附上所需绝对路径。"
                "读取执行结果使用 read_agent_execution（若当前工具列表可用），不要申请恢复 bash。"
            )
        else:
            usage = "仅具备命令执行能力的会话才能调用这些运行时；路径存在不代表程序已经执行或验证通过。"
        lines.append(
            "用法：调用运行时应使用上面的**绝对路径**（PATH 里可能有别的版本，"
            "也可能什么都没有）。" + usage
        )
        return "\n".join(lines)


def _same(a: Path, b: str) -> bool:
    """两个路径是否指向同一个文件（Windows 上大小写不敏感）。"""
    try:
        import os

        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return False


def build_env_tools() -> list[Tool]:
    """运行时依赖位置工具（由 build_registry 注册）。"""
    return [LoadWorkspaceDependenciesTool()]
