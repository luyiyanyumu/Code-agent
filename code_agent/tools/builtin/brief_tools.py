"""功能描述工具（M33）：让 code-agent **自己**给没写 docstring 的符号补一句人话。

为什么是工具而不是"界面里调用一次模型"：

- **生成方是 code-agent 自己的 agent**：它看得见代码、跑得过测试、有审批链；
  工具只负责"存得住、失效得掉、标得清"，不替它编内容。
- **不是一次性**：描述按「符号 id + 内容哈希」缓存（`code_agent/codeintel/briefs.py`），
  代码没改就一直复用；代码一改哈希就变，旧描述自动失效——不是每次开图都重新生成。
- **作者优先**：只补**没写 docstring** 的符号；作者写了就用作者的，工具不会去覆盖它。

一次典型用法（模型自己走）：

    feature_brief(action="missing")            # 看还缺多少、都是谁
    feature_brief(action="write", id="sym:...", text="按关键词给 prompt 分类")
    feature_brief(action="list")               # 回头检查（含过期的）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_agent.tools.base import Tool, ToolContext, ToolError


class FeatureBriefTool(Tool):
    name = "feature_brief"
    description = (
        "Read or write the one-line 'what is this for' description that the feature map shows "
        "for symbols whose author wrote no docstring. Descriptions are cached by the symbol's "
        "content hash, so they stay valid until the code changes. Actions: missing (what still "
        "lacks a description), write (id + text), list (what exists, incl. stale), forget (drop one). "
        "Never overwrites an author's docstring."
    )
    description_zh = "给没写 docstring 的符号补/查功能描述（按内容哈希缓存，代码改了自动失效）。"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["missing", "write", "list", "forget"],
                "description": "missing=缺描述的清单 / write=写一条 / list=已有的 / forget=删一条",
            },
            "id": {"type": "string", "description": "符号 id，如 sym:code_agent/agent/routing.py#classify"},
            "text": {
                "type": "string",
                "description": "一句话说明它是干什么的（≤200 字，导航里要能一眼读完）",
            },
            "limit": {"type": "integer", "description": "missing/list 返回条数上限（默认 30）"},
            "project": {
                "type": "string",
                "description": "项目 id（默认按当前工作区自动找）",
            },
        },
        "required": ["action"],
    }

    def __init__(self, intel: Any = None) -> None:
        self.intel = intel

    # ------------------------------------------------------------------
    def _project(self, ctx: ToolContext, *, project: str = "") -> dict:
        if self.intel is None:
            raise ToolError("代码索引服务不可用：功能描述只能在代码模式下使用")
        projects = getattr(self.intel, "projects", None)
        row = None
        if project:
            row = projects.get(project) if projects is not None else None
        elif projects is not None:
            root = str(Path(ctx.workspace)).replace("\\", "/").lower()
            row = projects.find_by_root(root)
        if not row:
            raise ToolError(
                "找不到这个工作区对应的代码项目：请先在「代码」页打开它（功能描述挂在项目上）"
            )
        return row

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        action = str(arguments.get("action") or "").strip()
        project = self._project(ctx, project=str(arguments.get("project") or ""))
        limit = max(1, min(int(arguments.get("limit") or 30), 200))

        if action == "missing":
            data = self.intel.feature_brief_missing(project, limit=limit)
            if not data["total"]:
                return "[feature_brief] 没有缺描述的功能：作者都写了 docstring，或已经补过。"
            lines = [
                f"[feature_brief] 还缺描述的功能 {data['total']} 条"
                + (f"（只列前 {len(data['items'])}）" if data["truncated"] else "")
                + "："
            ]
            for item in data["items"]:
                lines.append(f"- {item['id']}\n  {item['path']}:{item['line']}  ({item['domain']})")
            lines.append("")
            lines.append("补法：feature_brief(action=\"write\", id=<上面的 id>, text=\"一句话说明它是干什么的\")")
            lines.append("要求：写「它是干什么的」，不要复述函数名；≤200 字；一次只写一条。")
            return "\n".join(lines)

        if action == "list":
            data = self.intel.feature_briefs(project)
            if not data["count"]:
                return "[feature_brief] 这个项目还没有补过描述。"
            lines = [
                f"[feature_brief] 已有 {data['count']} 条描述"
                + (f"，其中 {data['stale']} 条因为代码改过已失效" if data["stale"] else "")
                + "："
            ]
            for item in data["items"][:limit]:
                flag = "" if item["fresh"] else "（已失效）"
                lines.append(f"- {item['symbol_id']}{flag}\n  {item['text']}")
            return "\n".join(lines)

        if action == "forget":
            sid = str(arguments.get("id") or "").strip()
            if not sid:
                raise ToolError("forget 需要 id")
            removed = self.intel.feature_brief_forget(project, symbol_id=sid)
            return f"[feature_brief] {'已删除' if removed else '没有这条描述'}：{sid}"

        if action == "write":
            sid = str(arguments.get("id") or "").strip()
            text = str(arguments.get("text") or "").strip()
            if not sid or not text:
                raise ToolError("write 需要 id 和 text")
            try:
                item = self.intel.feature_brief_write(
                    project,
                    symbol_id=sid,
                    text=text,
                    model=str(ctx.extra.get("model") or "") if isinstance(ctx.extra, dict) else "",
                    session_id=ctx.session_id,
                )
            except ValueError as exc:              # 符号不存在
                raise ToolError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 —— 描述太长等输入问题，直接告诉模型
                raise ToolError(str(exc)) from exc
            # 作者已经写了 docstring 的符号：如实说明这条描述**不会**被采用
            row = self.intel.index(project["id"], "main").symbol(sid) or {}
            note = ""
            if str(row.get("doc") or "").strip():
                note = "（注意：这个符号作者写了 docstring，界面上以作者的话为准，这条只留档）"
            return f"[feature_brief] 已记录 {sid}：{item['text']}{note}"

        raise ToolError(f"不认识的动作：{action}（可选 missing / write / list / forget）")


def build_brief_tools(intel: Any = None) -> list[Tool]:
    return [FeatureBriefTool(intel=intel)]
