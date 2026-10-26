"""文件交付（s20）：做完要交付 —— artifacts 目录 + deliver_file 工具。"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from code_agent.tools.base import Tool, ToolContext, ToolError
from code_agent.tools.builtin.tools_builtin import _WorkspaceBound

#: 一次最多交付几份。多了会变成一堵墙，用户反而挑不出重点。
MAX_PRESENT_FILES = 4


def _safe_filename(name: str) -> str:
    """防路径穿越：只保留文件名部分，白名单字符。"""
    name = Path(name).name
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", name)
    return cleaned or "artifact.txt"


class ArtifactsStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        d = self.root / _safe_filename(session_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, session_id: str, filename: str, content: str) -> Path:
        path = self._session_dir(session_id) / _safe_filename(filename)
        path.write_text(content, encoding="utf-8")
        return path

    def stage(self, session_id: str, source: Path, *, description: str = "") -> Path:
        """把**已存在**的文件原样搬进交付目录（二进制安全，不经过文本/字符编码）。

        与 `save` 的分工：`save` 收的是模型写在参数里的**文本内容**，适合报告、SVG
        这类几百 KB 的小产物；`stage` 收的是磁盘上的**文件路径**，适合 `bash` 跑脚本
        生成的 xlsx / PDF / 图片 / 大 CSV——那些东西根本没法当字符串塞进工具参数
        （base64 会把请求体撑爆，而且模型也不该为了交付把二进制读一遍）。

        重名不覆盖：同名文件已经交付过就加一个时间戳前缀。交付物是"给用户的东西"，
        静默盖掉上一份比多出一个文件严重。
        """
        src = Path(source)
        target_dir = self._session_dir(session_id)
        name = _safe_filename(src.name)
        target = target_dir / name
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = target_dir / f"{stamp}-{name}"
        target.write_bytes(src.read_bytes())
        if description:
            # 说明落一个同名 .txt 旁注：交付目录的文件清单是给用户看的，
            # 而 `list()` 只读文件名与大小，不需要为一句说明改数据结构。
            with contextlib.suppress(OSError):
                target.with_suffix(target.suffix + ".desc.txt").write_text(
                    description, encoding="utf-8"
                )
        return target

    def list(self, session_id: str) -> list[dict[str, Any]]:
        d = self._session_dir(session_id)
        out = []
        for f in sorted(d.iterdir()):
            if f.is_file():
                stat = f.stat()
                out.append(
                    {
                        "filename": f.name,
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    }
                )
        return out


class DeliverFileTool(Tool):
    name = "deliver_file"
    description = (
        "Save a deliverable file (report, svg, data, etc.) for the user. "
        "Returns a pointer the user can access via the artifacts API."
    )
    description_zh = (
        "为用户产出一份交付物文件（报告、SVG、数据等）。返回一个指针，"
        "用户可通过交付物接口访问下载。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "File name (e.g. report.md, chart.svg)"},
            "content": {"type": "string", "description": "Full text content to deliver"},
        },
        "required": ["filename", "content"],
    }

    def __init__(self, store: ArtifactsStore):
        self._store = store

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        filename = (arguments.get("filename") or "").strip()
        content = arguments.get("content")
        if not filename:
            raise ToolError("filename 不能为空")
        if not isinstance(content, str) or not content:
            raise ToolError("content 不能为空")
        if len(content) > 10 * 1024 * 1024:
            raise ToolError("content 超过 10MB 上限")
        path = self._store.save(ctx.session_id, filename, content)
        return f"[artifact] 已交付 {path.name}（{len(content)} 字符）-> {path}"


class PresentTool(Tool):
    """把**已经存在**的文件声明成交付物（对齐 `dsh-tool-present`）。

    与 `deliver_file` 的分工——这就是这个工具存在的唯一理由：

        deliver_file(filename, content)   模型把内容当参数交进来：适合报告 / SVG / 小数据
        present(files=[{path}])           模型只给**磁盘上的路径**：适合脚本产物、
                                          二进制、大文件（xlsx / PDF / 图片 / 大 CSV）

    没有它的时候，模型要交付一个由 `bash` 跑出来的 xlsx 只剩两条路：把二进制 base64
    塞进工具参数（请求体会爆），或者干脆不交付（用户拿不到东西）。两条都不行。

    与 DSH 的一处**有意不同**：DSH 的 present 允许绝对路径指向工作区之外（/tmp、
    Downloads）；code-agent 这里沿用 `read_file` 的读取白名单（工作区 / 系统
    tool_results / 技能目录）。因为 M6 把"工作区外不读"写成了**硬边界**，而交付工具
    不能变成绕开它的后门——否则模型可以把任何它读得到的文件摆到用户的下载列表里。
    沙箱里落进私有 /tmp 的产物，先让脚本写到工作区内再 present。
    """

    name = "present"
    description = (
        "Present one or more EXISTING files on disk as deliverables for the user "
        "(e.g. a spreadsheet or PDF produced by a script, an image, a large CSV). "
        "Pass file paths, not contents — this is the right tool when the file already exists "
        "and is too big/binary to put in a tool argument. "
        "Only files inside the workspace (or the system tool_results / skills dirs) can be "
        "presented; directories and symlinks are refused. "
        f"At most {MAX_PRESENT_FILES} files per call."
    )
    description_zh = (
        "把**已存在**的文件声明成交付物（脚本产出的 xlsx/PDF、图片、大 CSV 用这个）。"
        "传路径而不是内容——文件已经躺在磁盘上、又大又是二进制时就用它。"
        "只允许工作区内（或系统 tool_results / 技能目录）的文件；目录与符号链接会被拒。"
        f"一次最多 {MAX_PRESENT_FILES} 份。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": f"Files to present (1~{MAX_PRESENT_FILES}), most important first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path (workspace-relative)"},
                        "description": {"type": "string", "description": "Optional one-line note shown with the deliverable"},
                    },
                    "required": ["path"],
                },
            }
        },
        "required": ["files"],
    }

    def __init__(self, store: ArtifactsStore):
        self._store = store

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        raw = arguments.get("files")
        if not isinstance(raw, list) or not raw:
            raise ToolError("files 必须是非空数组（每项 {path, description?}）")
        if len(raw) > MAX_PRESENT_FILES:
            raise ToolError(
                f"一次最多交付 {MAX_PRESENT_FILES} 份（收到 {len(raw)} 份）："
                "只挑最重要的那几份"
            )

        lines: list[str] = []
        for idx, entry in enumerate(raw, start=1):
            if isinstance(entry, str):
                entry = {"path": entry}
            if not isinstance(entry, dict):
                raise ToolError(f"第 {idx} 项必须是对象或字符串")
            raw_path = str(entry.get("path") or "").strip()
            if not raw_path:
                raise ToolError(f"第 {idx} 项缺 path")
            description = str(entry.get("description") or "").strip()[:300]

            # 符号链接检查必须走**解析前**的原始路径：`_WorkspaceBound.resolve_read`
            # 内部会 `resolve()`，链接信息在那一步就被抹掉了（解析后的路径是个普通文件，
            # `is_symlink()` 恒为假——写成解析后检查就是一段永远不执行的死分支）。
            # 为什么值得拦：一个叫 `report.pdf` 的链接完全可能指向 `.env`，
            # 交付它等于把用户没打算交出去的那份内容摆进下载列表。
            unsymlinked = Path(raw_path)
            if not unsymlinked.is_absolute():
                unsymlinked = Path(ctx.workspace) / unsymlinked
            try:
                is_link = unsymlinked.is_symlink()
            except OSError:
                is_link = False
            if is_link:
                raise ToolError(
                    f"第 {idx} 项是符号链接，拒绝交付: {raw_path}"
                    "（链接指向哪儿不取决于它的名字，交付等于把目标也交出去）"
                )

            source = _WorkspaceBound.resolve_read(ctx.workspace, raw_path)
            if not source.exists():
                raise ToolError(f"第 {idx} 项不存在: {raw_path}")
            if source.is_dir():
                raise ToolError(f"第 {idx} 项是目录，present 只能交付文件: {raw_path}")

            target = self._store.stage(ctx.session_id, source, description=description)
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            note = f"：{description}" if description else ""
            lines.append(f"- {raw_path} -> {target.name}（{size} 字节）{note}")

        head = f"[present] 已交付 {len(lines)} 份文件（用户在会话交付物里可下载/打开）"
        return head + "\n" + "\n".join(lines)
