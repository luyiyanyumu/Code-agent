"""`read_image` 工具（M51）：把图片读进来，随下一次模型请求发出去。

机制在 `code_agent/context/images.py`（签名识别、尺寸解析、注入箱）。这里只负责
把模型的意图翻成那几步，并守住两条口径：

1. **不是图片 / 读不了 → 当正常结果返回**，不抛异常。模型要的是"这张图我读不了，
   为什么"，而不是一条被包成 `[tool error]` 的栈。
2. **没开图像输入就说清楚**。`CODE_AGENT_VISION` 没设成 1 时，图片字节**不会**发给
   模型——那就必须写成"以下是文件事实，不要当作我看过这张图"，而不是一句
   "已读入图片"。后者会让模型对着一段它从没看到的字节开始描述画面（这是最坏的一种
   假装：它编得越具体，人越难发现）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_agent.context.images import MAX_IMAGES_PER_RUN, read_pending, vision_enabled
from code_agent.tools.base import Tool, ToolContext, ToolError
from code_agent.tools.builtin.tools_builtin import _WorkspaceBound

#: 没有图片通道时的说法（嵌入式 / 未接 AgentLoop）
NO_CHANNEL = (
    "[read_image] 当前环境没有图片通道（嵌入式 / 未接 AgentLoop）：只能给出文件事实，"
    "**图片字节没有发给模型**。不要把它当作你看过这张图。"
)


class ReadImageTool(Tool):
    name = "read_image"
    description = (
        "Read an image file (PNG/JPEG/WebP/GIF) so it is attached to your NEXT model request. "
        "Format is detected by file signature, not by extension. "
        "Use it for design mockups, screenshots (errors, UI), charts and photos. "
        "Limits: one image ≤ 4 MiB, at most 3 per request. "
        "If this deployment has no image input enabled, the call returns the file facts and says "
        "plainly that the pixels were NOT sent — do not describe an image you did not receive."
    )
    description_zh = (
        "读入一张图片（PNG/JPEG/WebP/GIF），它会在**下一次模型请求**里一并发给模型；"
        "类型按**文件签名**判断，不看扩展名。设计稿、报错截图、图表、照片都用它。"
        "上限：单张 ≤ 4 MiB、一次请求 ≤ 3 张。"
        "如果本部署没开图像输入，它会给出文件事实并明确说明「像素没有发出去」——"
        "不要描述一张你并没收到的图。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Image path (relative to the workspace)"},
            "note": {
                "type": "string",
                "description": "Optional one-line note about what to look for in this image",
            },
        },
        "required": ["path"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        raw = str(arguments.get("path") or "").strip()
        if not raw:
            raise ToolError("path 不能为空")
        note = str(arguments.get("note") or "").strip()[:200]

        path = _WorkspaceBound.resolve_read(ctx.workspace, raw)
        if not path.is_file():
            raise ToolError(f"文件不存在: {raw}")

        image, error = read_pending(Path(path), note=note)
        if error is not None or image is None:
            # 不是图片 / 太大 / 读不动 —— 都是"正常结果"，如实回给模型
            return f"[read_image] {error}"

        inbox = (ctx.extra or {}).get("images")
        if inbox is None:
            return f"{NO_CHANNEL}\n- {image.describe()}"
        if not vision_enabled():
            available = (ctx.extra or {}).get("available_tools", ())
            if "bash" in available:
                fallback = "或者在当前权限允许时，用 bash 调用可用的二维码/OCR 工具提取文字。"
            elif "delegate_agent" in available:
                fallback = "如需二维码/OCR 文字识别，委派给具备执行能力的节点，并核对它返回的实际结果。"
            else:
                fallback = "当前工具未提供文字识别能力；如需 OCR，应交由具备对应能力的执行环境处理。"
            return (
                "[read_image] 本部署没有开启图像输入（CODE_AGENT_VISION 未设为 1），"
                "所以**图片字节没有发给模型**——下面是文件事实，不要当作你看过这张图：\n"
                f"- {image.describe()}\n"
                "要用图：请人设 `CODE_AGENT_VISION=1` 并确认所用模型支持图像输入；"
                + fallback
            )

        rejection = inbox.add(image)
        if rejection is not None:
            return f"[read_image] {rejection}"
        return (
            f"[read_image] 已读入 {image.describe()}；它会在**下一次模型请求**里"
            f"作为图片一并发出（一次请求最多 {MAX_IMAGES_PER_RUN} 张）。\n"
            "不要为了「再确认一下」重复读同一张图——那会重复占用上下文。"
        )


def build_image_tools() -> list[Tool]:
    """图片工具（由 build_registry 注册）。"""
    return [ReadImageTool()]
