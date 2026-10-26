"""MCP 连接器（s17）：外接工具要有标准协议。

最小可用的 stdio 传输客户端：
- JSON-RPC 2.0 换行分隔，与 MCP 官方 stdio transport 一致
- initialize 握手 -> tools/list 发现 -> tools/call 调用
- 配置：~/.code-agent/mcp.json {"mcpServers": {name: {command, args, trusted}}}
- 信任门禁：trusted=true 的 server 工具直接放行（AllowPolicy）；
  未信任的一律走人机审批（无专用策略 -> DefaultPolicy -> need_approval）

M18 · P3：传输层（进程 + 封帧 + 请求归位 + 读循环）已抽到
`code_agent/extensions/jsonrpc_stdio.py`，与 LSP 共用同一份实现——两者的帧格式
不同（换行 vs Content-Length），但会话语义一致，差异只该存在于封帧参数里。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from code_agent.extensions.jsonrpc_stdio import JsonRpcError, JsonRpcStdio
from code_agent.tools.base import Tool, ToolContext


class McpError(RuntimeError):
    pass


class McpStdioClient:
    """单个 MCP server 的 stdio 连接（换行分隔帧，薄封装 JsonRpcStdio）。"""

    def __init__(self, name: str, command: str, args: list[str] | None = None):
        self.name = name
        self.command = command
        self.args = args or []
        self._client = JsonRpcStdio(
            command, self.args, framing="newline", name=f"mcp:{name}"
        )

    @property
    def stderr_tail(self) -> str:
        return self._client.stderr_tail()

    async def start(self) -> None:
        try:
            await self._client.start()
            await self._client.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "code-agent", "version": "0.3.0"},
                },
                timeout=15.0,
            )
            await self._client.notify("notifications/initialized")
        except JsonRpcError as exc:
            raise McpError(str(exc)) from exc

    async def _request(self, method: str, params: dict | None = None, timeout: float = 15.0) -> Any:
        try:
            return await self._client.request(method, params, timeout=timeout)
        except JsonRpcError as exc:
            raise McpError(str(exc)) from exc

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list")
        return result.get("tools", []) if isinstance(result, dict) else []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        result = await self._request("tools/call", {"name": tool_name, "arguments": arguments})
        if not isinstance(result, dict):
            return ""
        parts = result.get("content") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(texts)

    async def stop(self) -> None:
        await self._client.stop()


class McpTool(Tool):
    """把 MCP server 的 tool 适配成 Code-agent 的 Tool 协议。"""

    def __init__(
        self,
        server: str,
        client: McpStdioClient,
        definition: dict[str, Any],
        origin: str = "mcp.json",
    ):
        self._server = server
        self._client = client
        self._def = definition
        self.name = f"mcp__{server}__{definition.get('name', '')}"
        self.description = f"[mcp:{server}] {definition.get('description', '')}"
        self.parameters = definition.get("inputSchema") or {
            "type": "object",
            "properties": {},
        }
        #: 来源标签（mcp.json / plugin:xxx），供「工具入口」展示
        self.origin = origin
        #: MCP 原始工具名（去掉前缀后的部分）
        self.raw_name = str(definition.get("name", ""))

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        try:
            return await self._client.call_tool(self._def["name"], arguments)
        except McpError as exc:
            return f"[mcp error] {exc}"


class McpManager:
    """加载 mcp.json（+ 插件声明的 server），连接所有 server，产出适配后的工具。"""

    def __init__(self, config_path: Path, extra_servers: dict[str, dict[str, Any]] | None = None):
        self.config_path = Path(config_path)
        #: 插件等外部来源声明的 server；同名时本地 mcp.json 优先
        self.extra_servers: dict[str, dict[str, Any]] = dict(extra_servers or {})
        self.clients: dict[str, McpStdioClient] = {}
        #: server -> 连接失败原因（前端要显示"配了但没连上"）
        self.failures: dict[str, str] = {}
        #: server -> 发现的工具数
        self.tool_counts: dict[str, int] = {}
        #: server -> 来源标签
        self.origins: dict[str, str] = {}
        #: 被页面一键停用的 server（配了 enabled=false）：不启动、也不算"连接失败"
        self.disabled: set[str] = set()

    def load_config(self) -> dict[str, dict[str, Any]]:
        local: dict[str, dict[str, Any]] = {}
        if self.config_path.exists():
            try:
                # utf-8-sig：Windows 用户手写的 mcp.json 常带 BOM，带 BOM 时
                # json.loads 会报错并把整份配置丢掉（表现为"配了却没生效"）
                data = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
                local = data.get("mcpServers", {}) or {}
            except (json.JSONDecodeError, OSError):
                local = {}
        merged: dict[str, dict[str, Any]] = {}
        for name, cfg in self.extra_servers.items():
            merged[name] = dict(cfg)
            self.origins.setdefault(name, str(cfg.get("origin") or "plugin"))
        for name, cfg in local.items():
            merged[name] = cfg
            self.origins[name] = "mcp.json"
        return merged

    def catalog(self) -> list[dict[str, Any]]:
        """所有已配置 server 的状态（含未连上的），供「工具入口」展示。"""
        out: list[dict[str, Any]] = []
        for name, cfg in self.load_config().items():
            enabled = cfg.get("enabled") is not False
            out.append(
                {
                    "name": name,
                    "origin": self.origins.get(name, "mcp.json"),
                    "connected": name in self.clients,
                    "tools": self.tool_counts.get(name, 0),
                    "error": self.failures.get(name, ""),
                    "enabled": enabled,
                    # 插件声明的 server 不能在页面上删（改插件清单去）
                    "deletable": not str(self.origins.get(name, "mcp.json")).startswith("plugin:"),
                }
            )
        return sorted(out, key=lambda x: x["name"])

    async def connect_all(self) -> list[tuple[Tool, bool]]:
        """连接全部 server，返回 [(适配工具, 是否信任)]。连不上的记进 failures 后跳过。"""
        tools: list[tuple[Tool, bool]] = []
        self.failures.clear()
        self.tool_counts.clear()
        self.disabled.clear()
        for name, cfg in self.load_config().items():
            if cfg.get("enabled") is False:
                # 页面上一键停用的 server：不启动子进程、不算失败（M22）
                self.disabled.add(name)
                continue
            command = cfg.get("command")
            if not command:
                self.failures[name] = "配置缺少 command"
                continue
            client = McpStdioClient(name, command, cfg.get("args") or [])
            try:
                await client.start()
            except (OSError, McpError, asyncio.TimeoutError, ValueError) as exc:
                # 连不上的 server 跳过，不阻塞平台启动；但记下原因让 UI 能显示
                self.failures[name] = f"{type(exc).__name__}: {exc}"
                continue
            self.clients[name] = client
            self.failures.pop(name, None)
            trusted = bool(cfg.get("trusted"))
            origin = self.origins.get(name, "mcp.json")
            definitions = await client.list_tools()
            self.tool_counts[name] = len(definitions)
            for definition in definitions:
                tools.append((McpTool(name, client, definition, origin), trusted))
        return tools

    async def stop_all(self) -> None:
        for client in self.clients.values():
            await client.stop()
        self.clients.clear()
