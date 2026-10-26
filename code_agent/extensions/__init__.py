"""扩展生态（s16-s20）：Skills / Experts / MCP / 交付 / 插件包（M10）/ 制作上传（M11）。"""

from code_agent.extensions.artifacts import ArtifactsStore, DeliverFileTool, PresentTool
from code_agent.extensions.authoring import (
    AuthoringError,
    delete_plugin_dir,
    extract_plugin_zip,
    extract_skill_zip,
    load_mcp_config,
    normalize_mcp_servers,
    save_mcp_config,
    write_skill_file,
)
from code_agent.extensions.catalog import build_catalog, find_tool, tool_source
from code_agent.extensions.mcp_client import McpManager, McpStdioClient, McpTool
from code_agent.extensions.packs import (
    ExpertsRegistry,
    LoadPackTool,
    MarkdownPack,
    PackSource,
    SkillsRegistry,
)
from code_agent.extensions.plugins import PluginManifest, PluginRegistry, parse_manifest

__all__ = [
    "SkillsRegistry",
    "ExpertsRegistry",
    "MarkdownPack",
    "PackSource",
    "LoadPackTool",
    "McpManager",
    "McpStdioClient",
    "McpTool",
    "ArtifactsStore",
    "DeliverFileTool",
    "PresentTool",
    "PluginRegistry",
    "PluginManifest",
    "parse_manifest",
    "build_catalog",
    "find_tool",
    "tool_source",
    "AuthoringError",
    "extract_plugin_zip",
    "extract_skill_zip",
    "delete_plugin_dir",
    "write_skill_file",
    "load_mcp_config",
    "normalize_mcp_servers",
    "save_mcp_config",
]
