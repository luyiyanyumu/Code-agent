"""延迟加载（s03）：工具先列目录，用到再展开完整 schema。

机制：
- DeferredToolRegistry 包一层 ToolRegistry，注册时标记 deferred=True；
- schemas() 只暴露 name + description（摘要），完整 schema 不进上下文；
- 内置 tool_search 工具：模型按名字检索，命中后激活 —— 激活的工具
  下一轮起出现在 schemas() 里。
"""

from __future__ import annotations

from typing import Any

from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.registry import ToolRegistry


class ToolSearchTool(Tool):
    name = "tool_search"
    description = (
        "Search available tools by name/exact match and activate them. "
        "Returns the full JSON schema of matched tools; activated tools "
        "become callable in the next turn."
    )
    description_zh = "按名称检索并激活可用工具。返回命中工具的完整 JSON schema，激活后下一轮即可调用。"
    parameters = {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tool names to activate",
            }
        },
        "required": ["names"],
    }

    def __init__(self, registry: "DeferredToolRegistry"):
        self._registry = registry

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        names = arguments.get("names") or []
        if not names:
            return "[tool_search] names 不能为空"
        activated, missing = self._registry.activate(names)
        schemas = [self._registry.get(n).schema() for n in activated]  # type: ignore[union-attr]
        import json

        lines = [f"activated: {activated or []}"]
        if missing:
            lines.append(f"not found: {missing}")
        if schemas:
            lines.append(json.dumps(schemas, ensure_ascii=False))
        return "\n".join(lines)


class DeferredToolRegistry(ToolRegistry):
    def __init__(self) -> None:
        super().__init__()
        self._deferred: set[str] = set()
        # tool_search 自身永远可见
        super().register(ToolSearchTool(self))

    def register_deferred(self, tool: Tool) -> None:
        super().register(tool)
        self._deferred.add(tool.name)

    def activate(self, names: list[str]) -> tuple[list[str], list[str]]:
        """激活工具（下一轮起进入 schemas），返回 (激活的, 未找到的)。"""
        activated, missing = [], []
        for n in names:
            if n in self._tools:
                self._deferred.discard(n)
                activated.append(n)
            else:
                missing.append(n)
        return activated, missing

    def schemas(self) -> list[dict[str, Any]]:
        """未激活的 deferred 工具只给摘要，激活后给完整 schema。"""
        out: list[dict[str, Any]] = []
        for t in self._tools.values():
            if t.name in self._deferred:
                out.append(
                    {
                        "name": t.name,
                        "description": f"[deferred] {t.description}",
                        "parameters": {
                            "type": "object",
                            "properties": {"_use_tool_search": {"type": "boolean"}},
                        },
                    }
                )
            else:
                out.append(t.schema())
        return out
