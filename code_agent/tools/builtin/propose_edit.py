"""模型提出修改，不直接写工作区；人工在 IDE 审查差异块。"""
import asyncio
import json
import sqlite3
from code_agent.tools.base import Tool, ToolError
from code_agent.tools.builtin.tools_builtin import _WorkspaceBound


class ProposeEditTool(Tool):
    name = "propose_edit"
    description = "Propose precise edits for human review in the IDE. Does NOT write project files. Use this for requested code modifications and generated tests; the user applies/rejects individual diff hunks. Read the file first."
    description_zh = "提出代码修改供人工逐块审查，不写入项目文件；支持只读 AI 会话。"
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"}, "title": {"type": "string"},
        "content": {"type": "string", "description": "Full text for a new file only; use edits for existing files."},
        "edits": {"type": "array", "items": {"type": "object", "properties": {"old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["old_string", "new_string"]}},
    }, "required": ["path"]}

    def __init__(self, intel=None):
        self.intel = intel

    async def execute(self, arguments, ctx):
        try:
            if not isinstance(arguments, dict):
                raise ToolError("修改参数必须是对象")
            reviews = getattr(self.intel, "reviews", None)
            project = self.intel.project_for_workspace(ctx.workspace, ctx.user_id) if self.intel else None
            if reviews is None or project is None or project.get("user_id") != ctx.user_id:
                raise ToolError("请在自己已登记项目的代码工作区提出修改")
            if not isinstance(arguments.get("path"), str):
                raise ToolError("path 必须是文件路径文本")
            target = _WorkspaceBound.resolve_write(ctx.workspace, arguments["path"])
            from pathlib import Path
            arguments = {**arguments, "path": target.relative_to(Path(project["root_path"]).resolve()).as_posix()}
            if target.exists() and "content" in arguments:
                raise ToolError("已有文件请使用 edits 精确替换，新建文件才使用 content")
            def fresh():
                if ctx.fs_observation is None:
                    return None
                return ctx.fs_observation.check_edit(target) if target.exists() else ctx.fs_observation.check_write(target)
            refusal = fresh()
            if refusal:
                raise ToolError(refusal)
            proposal = await asyncio.to_thread(reviews.create, project, arguments, source="ai", session_id=ctx.session_id, precondition=fresh)
        except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps({"proposal_id": proposal["id"], "path": proposal["path"], "hunks": proposal["counts"]["total"], "state": "pending", "message": "已生成待审差异，项目文件尚未修改。请用户在 IDE 修改面板逐块应用或拒绝。"}, ensure_ascii=False)
