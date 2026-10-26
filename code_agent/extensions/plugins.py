"""插件包机制（M10）：把「技能 + MCP + 权限放行 + prompt 片段」打成一个可分发的包。

为什么要有这一层：Skills / Experts / MCP 各自已经能用，但**分发**很难——
把一个能力交给别人，得让人分别去 skills/ 放文件、去 mcp.json 加配置、
再口头交代哪些工具要放行。插件包把这几件事收进一个目录、一份清单。

目录结构：
    ~/.code-agent/plugins/<plugin-name>/
    ├── plugin.json        # 清单（唯一必需文件）
    ├── skills/            # 内嵌技能：skills/<skill-name>/SKILL.md
    ├── prompt.md          # 附加 system prompt 片段（可选，json 里的 prompt 优先）
    └── tools.py           # 代码式工具（可选，默认不加载，见下）

plugin.json：
    {
      "name": "fin-pack",
      "version": "1.0.0",
      "description": "金融数据能力包",
      "author": "…",
      "enabled": true,
      "skills": ["a-share"],          # 只启用这些内嵌技能；省略 = 全部
      "mcpServers": {                 # 与 mcp.json 同格式
        "yahoo": {"command": "npx", "args": ["-y", "..."], "trusted": false}
      },
      "allowTools": ["mcp__yahoo__quote"],   # 免审批放行的工具名
      "prompt": "……",                        # 追加到 system prompt
      "codeTools": false                     # 是否允许加载 tools.py
    }

安全边界（重要）：
- 声明式能力（技能 / MCP / 放行名单 / prompt）只走既有的信任门禁，插件本身
  不绕过任何一层防御。`allowTools` 是显式声明，不是插件自己给自己发权限。
- 代码式工具（tools.py）等于「让第三方代码在本进程内执行」，会旁路掉沙箱与
  权限链路。因此设了**双重开关**：全局 CODE_AGENT_PLUGIN_CODE=1 **且** 清单里
  codeTools=true 才加载，任一不满足即跳过并记录原因。
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_agent.tools.base import Tool

MANIFEST_NAME = "plugin.json"
CODE_TOOLS_FILE = "tools.py"
CODE_TOOLS_ENV = "CODE_AGENT_PLUGIN_CODE"

#: 清单里允许出现的键（多余键忽略，防止拼错字段却静默生效）
_KNOWN_KEYS = {
    "name",
    "version",
    "description",
    "author",
    "enabled",
    "skills",
    "mcpServers",
    "allowTools",
    "prompt",
    "codeTools",
}


@dataclass
class PluginManifest:
    """一个插件的清单。error 非空表示清单有问题（插件仍会列出，但不生效）。"""

    name: str
    path: Path
    version: str = ""
    description: str = ""
    author: str = ""
    enabled: bool = True
    skills: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    allow_tools: list[str] = field(default_factory=list)
    prompt: str = ""
    code_tools: bool = False
    error: str = ""

    @property
    def usable(self) -> bool:
        """清单没问题且已启用——只有这种插件才真正贡献能力。"""
        return self.enabled and not self.error

    def to_dict(self, *, skills_found: list[str] | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "enabled": self.enabled,
            "path": str(self.path),
            "skills": self.skills,
            "skills_found": skills_found if skills_found is not None else [],
            "mcp_servers": sorted(self.mcp_servers),
            "allow_tools": sorted(self.allow_tools),
            "has_prompt": bool(self.prompt),
            "code_tools": self.code_tools,
            "error": self.error,
        }


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


def _as_server_map(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, cfg in value.items():
        if isinstance(cfg, dict) and cfg.get("command"):
            out[str(key)] = cfg
    return out


def parse_manifest(directory: Path) -> PluginManifest:
    """解析一个插件目录。任何解析问题都落到 .error 而不是抛异常——
    坏插件不该让整个平台起不来。"""
    name = directory.name
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        return PluginManifest(name=name, path=directory, error=f"缺少 {MANIFEST_NAME}")

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return PluginManifest(name=name, path=directory, error=f"{MANIFEST_NAME} 不是合法 JSON: {exc}")
    except OSError as exc:
        return PluginManifest(name=name, path=directory, error=f"读取失败: {exc}")

    if not isinstance(raw, dict):
        return PluginManifest(name=name, path=directory, error=f"{MANIFEST_NAME} 顶层必须是对象")

    resolved = str(raw.get("name") or name).strip() or name
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        prompt_file = directory / "prompt.md"
        if prompt_file.is_file():
            try:
                prompt = prompt_file.read_text(encoding="utf-8").strip()
            except OSError:
                prompt = ""

    return PluginManifest(
        name=resolved,
        path=directory,
        version=str(raw.get("version") or ""),
        description=str(raw.get("description") or ""),
        author=str(raw.get("author") or ""),
        enabled=bool(raw.get("enabled", True)),
        skills=_as_str_list(raw.get("skills")),
        mcp_servers=_as_server_map(raw.get("mcpServers")),
        allow_tools=_as_str_list(raw.get("allowTools")),
        prompt=prompt,
        code_tools=bool(raw.get("codeTools", False)),
    )


class PluginRegistry:
    """扫描 plugins 目录，聚合成可被平台消费的几份配置。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._manifests: dict[str, PluginManifest] = {}
        self.scan()

    # ------------------------------------------------------------------
    # 扫描与查询
    # ------------------------------------------------------------------
    def scan(self) -> None:
        self._manifests.clear()
        if not self.root.exists():
            return
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue  # 隐藏目录（如 .trash 回收站）不参与扫描
            manifest = parse_manifest(child)
            # 同名插件：目录名优先，避免清单里的 name 互相覆盖
            self._manifests.setdefault(manifest.name, manifest)

    def list(self) -> list[PluginManifest]:
        return list(self._manifests.values())

    def get(self, name: str) -> PluginManifest | None:
        return self._manifests.get(name)

    def enabled(self) -> list[PluginManifest]:
        return [m for m in self._manifests.values() if m.usable]

    def skill_dirs(self) -> list[Path]:
        """所有启用插件里存在的 skills/ 子目录。"""
        out: list[Path] = []
        for m in self.enabled():
            d = m.path / "skills"
            if d.is_dir():
                out.append(d)
        return out

    def skill_sources(self) -> list[Any]:
        """启用插件的内嵌技能来源（PackSource），供 SkillsRegistry 并入目录。

        每个来源带 origin 标签与允许的技能白名单（清单里的 skills 字段）。
        """
        from code_agent.extensions.packs import PackSource

        out: list[Any] = []
        for m in self.enabled():
            d = m.path / "skills"
            if d.is_dir():
                out.append(PackSource(d, f"plugin:{m.name}", tuple(m.skills)))
        return out

    def allowed_skill_names(self) -> dict[str, list[str]]:
        """plugin name -> 该插件允许启用的技能名（空列表 = 全部）。"""
        return {m.name: list(m.skills) for m in self.enabled() if (m.path / "skills").is_dir()}

    def set_enabled(self, name: str, enabled: bool) -> PluginManifest:
        """启停插件并写回 plugin.json（保留其他字段，不重排注释结构）。"""
        manifest = self._manifests.get(name)
        if manifest is None:
            raise KeyError(f"插件不存在: {name}")
        if manifest.error:
            raise ValueError(f"插件清单有问题，无法启停: {manifest.error}")
        manifest_path = manifest.path / MANIFEST_NAME
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"无法写回清单: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("无法写回清单：顶层必须是对象")
        raw["enabled"] = bool(enabled)
        manifest_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        manifest.enabled = bool(enabled)
        return manifest

    # ------------------------------------------------------------------
    # 能力聚合（供 create_app 接线）
    # ------------------------------------------------------------------
    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """合并所有启用插件声明的 MCP 服务器（后加载的同名不覆盖先前配置）。"""
        out: dict[str, dict[str, Any]] = {}
        for m in self.enabled():
            for server_name, cfg in m.mcp_servers.items():
                merged = dict(cfg)
                merged.setdefault("origin", f"plugin:{m.name}")
                out.setdefault(server_name, merged)
        return out

    def allow_tools(self) -> list[str]:
        out: list[str] = []
        for m in self.enabled():
            out.extend(m.allow_tools)
        return sorted(set(out))

    def prompt_suffix(self) -> str:
        blocks = []
        for m in self.enabled():
            if m.prompt:
                blocks.append(f"【插件 {m.name}】\n{m.prompt}")
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # 代码式工具（默认关闭）
    # ------------------------------------------------------------------
    @staticmethod
    def code_tools_globally_enabled() -> bool:
        return os.environ.get(CODE_TOOLS_ENV, "").strip().lower() in ("1", "true", "yes", "on")

    def code_tool_plugins(self) -> list[PluginManifest]:
        """声明了 codeTools 且真正满足双开关的插件。"""
        if not self.code_tools_globally_enabled():
            return []
        return [
            m
            for m in self.enabled()
            if m.code_tools and (m.path / CODE_TOOLS_FILE).is_file()
        ]

    def code_tools_status(self) -> dict[str, str]:
        """插件名 -> 代码工具状态说明（供 UI 显示为什么没加载）。"""
        global_on = self.code_tools_globally_enabled()
        out: dict[str, str] = {}
        for m in self.list():
            if not m.code_tools:
                continue
            if m.error:
                out[m.name] = f"清单有问题：{m.error}"
            elif not m.enabled:
                out[m.name] = "插件已停用"
            elif not (m.path / CODE_TOOLS_FILE).is_file():
                out[m.name] = f"缺少 {CODE_TOOLS_FILE}"
            elif not global_on:
                out[m.name] = f"已跳过：需设置 {CODE_TOOLS_ENV}=1 才允许加载插件代码"
            else:
                out[m.name] = "已加载"
        return out

    def load_code_tools(self) -> tuple[list[Tool], list[str]]:
        """加载 tools.py 里的 build_tools()，返回 (工具列表, 错误说明)。

        单个插件加载失败只记录错误，不影响其他插件与平台启动。
        """
        tools: list[Tool] = []
        errors: list[str] = []
        for m in self.code_tool_plugins():
            module_path = m.path / CODE_TOOLS_FILE
            try:
                spec = importlib.util.spec_from_file_location(
                    f"code_agent_plugin_{m.name}", module_path
                )
                if spec is None or spec.loader is None:
                    errors.append(f"{m.name}: 无法构造模块 spec")
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                builder = getattr(module, "build_tools", None)
                if not callable(builder):
                    errors.append(f"{m.name}: {CODE_TOOLS_FILE} 未定义 build_tools()")
                    continue
                produced = builder() or []
                for tool in produced:
                    if not isinstance(tool, Tool) or not tool.name:
                        errors.append(f"{m.name}: build_tools() 返回了非 Tool 对象")
                        continue
                    # 打来源标记，供「工具入口」归类与审计
                    try:
                        tool.origin = f"plugin:{m.name}"
                    except (AttributeError, TypeError):
                        pass
                    tools.append(tool)
            except Exception as exc:  # 第三方代码，什么异常都可能
                errors.append(f"{m.name}: 加载失败 {type(exc).__name__}: {exc}")
        return tools, errors
