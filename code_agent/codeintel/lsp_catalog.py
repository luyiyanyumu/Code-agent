"""语言服务器目录（M18 · P3）：内置主流清单 + 自动探测 + 配置合并。

三条设计原则：

1. **内置目录不等于内置二进制**。这里只声明"哪种语言该用哪个 server、怎么启动、
   靠什么文件判断适用"，二进制本身由用户按需安装（内网环境不该被我们偷偷下载）。
   没装就如实显示"未安装 + 安装提示"，而不是静默失效。
2. **自动探测**：只给"项目里真的有这种语言"的 server 提建议（按扩展名与根标记文件），
   避免一个 Python 项目被推荐装 20 个语言服务器。
3. **命令解析尽量贴合工程实际**：先查 PATH，再查项目内的 `node_modules/.bin`
   （很多项目把 `typescript-language-server`/`pyright` 装在本地依赖里）。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LspPreset:
    id: str
    name: str
    command: str
    args: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    root_markers: tuple[str, ...] = ()
    install_hint: str = ""
    initialization_options: dict[str, Any] = field(default_factory=dict)


_CATALOG: tuple[LspPreset, ...] = (
    LspPreset(
        "pyright", "Pyright", "pyright-langserver", ("--stdio",), ("python",),
        ("pyproject.toml", "setup.py", "requirements.txt", "*.py"),
        "npm i -g pyright  或  pip install pyright",
    ),
    LspPreset(
        "pylsp", "python-lsp-server", "pylsp", (), ("python",),
        ("pyproject.toml", "setup.py", "*.py"),
        "pip install python-lsp-server",
    ),
    LspPreset(
        # ruff 从 0.5 起自带语言服务器（`ruff server`）。早先这里用 ruff-lsp，
        # 但它和当前 lsprotocol 版本不兼容（ImportError: NotebookDocumentSyncOptions…），
        # 装了也起不来——已装 ruff 的话直接用内置的这条。
        "ruff", "Ruff LSP", "ruff", ("server",), ("python",), ("pyproject.toml", "*.py"),
        "pip install ruff  或  npm i -g ruff",
    ),
    LspPreset(
        "typescript", "TypeScript", "typescript-language-server", ("--stdio",),
        ("typescript", "javascript", "json"),
        ("tsconfig.json", "jsconfig.json", "package.json"),
        "npm i -g typescript-language-server typescript",
    ),
    LspPreset(
        "vue", "Vue (Volar)", "vue-language-server", ("--stdio",), ("vue",),
        ("package.json", "*.vue"),
        "npm i -g @vue/language-server",
    ),
    LspPreset(
        "json", "JSON", "vscode-json-language-server", ("--stdio",), ("json",), ("*.json",),
        "npm i -g vscode-langservers-extracted",
    ),
    LspPreset(
        "css", "CSS/LESS/SCSS", "vscode-css-language-server", ("--stdio",), ("css", "scss", "less"),
        ("*.css", "*.scss"), "npm i -g vscode-langservers-extracted",
    ),
    LspPreset(
        "html", "HTML", "vscode-html-language-server", ("--stdio",), ("html", "vue"),
        ("*.html",), "npm i -g vscode-langservers-extracted",
    ),
    LspPreset(
        "eslint", "ESLint", "vscode-eslint-language-server", ("--stdio",), ("javascript", "typescript"),
        (".eslintrc", ".eslintrc.js", ".eslintrc.json", "eslint.config.js", "package.json"),
        "npm i -g vscode-langservers-extracted",
    ),
    LspPreset(
        "yaml", "YAML", "yaml-language-server", ("--stdio",), ("yaml",), ("*.yaml", "*.yml"),
        "npm i -g yaml-language-server",
    ),
    LspPreset(
        "toml", "Taplo (TOML)", "taplo", ("lsp", "stdio"), ("toml",), ("*.toml",),
        # npm 版 @taplo/cli **不含 LSP**（跑起来会报 "the LSP is not part of this build"）：
        # 要 LSP 得装官方 release 或 cargo 版，这点必须写在安装提示里，否则用户装完还是用不了。
        "cargo install taplo-cli（含 LSP）或装官方 release；npm 版 @taplo/cli 不含 LSP",
    ),
    LspPreset(
        "bash", "Bash", "bash-language-server", ("start",), ("shell",), ("*.sh",),
        "npm i -g bash-language-server",
    ),
    LspPreset(
        "gopls", "gopls", "gopls", (), ("go",), ("go.mod", "go.sum"), "go install golang.org/x/tools/gopls@latest",
    ),
    LspPreset(
        "rust", "rust-analyzer", "rust-analyzer", (), ("rust",), ("Cargo.toml",),
        "rustup default stable 后 rustup component add rust-analyzer",
    ),
    LspPreset(
        "clangd", "clangd", "clangd", (), ("c", "cpp"), ("compile_commands.json", "*.c", "*.cpp"),
        # 独立包只有 ~30MB，比 winget 的 LLVM.LLVM（2-3GB 全家桶）划算得多；
        # macOS/Linux 上 brew/apt 装的就是这个独立包。
        "解压 github.com/clangd/clangd/releases 的 clangd-<平台>-*.zip（约 30MB）",
    ),
    LspPreset(
        "jdtls", "Eclipse JDT (Java)", "jdtls", (), ("java",), ("pom.xml", "build.gradle", "*.java"),
        "解压 eclipse.jdt.ls 的里程碑包（需要 Java 21+），或 brew install jdtls",
    ),
    LspPreset(
        "csharp", "C#", "csharp-ls", (), ("csharp",), ("*.csproj", "*.sln"),
        # 包只带 tools/net10.0；.NET 9 SDK 会报"找不到 DotnetToolSettings.xml"（真机踩过）
        "dotnet tool install --global csharp-ls（需要 .NET 10 SDK）",
    ),
    LspPreset(
        "php", "Intelephense", "intelephense", ("--stdio",), ("php",), ("composer.json", "*.php"),
        "npm i -g intelephense",
    ),
    LspPreset(
        "ruby", "Solargraph", "solargraph", ("stdio",), ("ruby",), ("Gemfile", "*.rb"),
        "gem install solargraph",
    ),
    LspPreset(
        "kotlin", "Kotlin", "kotlin-language-server", (), ("kotlin",), ("build.gradle.kts", "*.kt"),
        # 服务端不含 JRE，需要本机 Java 17+（jdtls 同理）
        "解压 github.com/fwcd/kotlin-language-server/releases 的 server.zip（需要 Java 17+）",
    ),
    LspPreset(
        "swift", "SourceKit-LSP", "sourcekit-lsp", (), ("swift",), ("Package.swift", "*.swift"),
        "随 Swift 工具链自带",
    ),
    LspPreset(
        "dart", "Dart", "dart", ("language-server", "--protocol=lsp"), ("dart",), ("pubspec.yaml",),
        "随 Dart SDK 自带",
    ),
    LspPreset(
        "lua", "Lua", "lua-language-server", (), ("lua",), ("*.lua", ".luarc.json"),
        "解压 github.com/LuaLS/lua-language-server 的发行包（免运行时），或 brew/apt 安装",
    ),
    LspPreset(
        "sql", "SQL", "sql-language-server", ("up", "--method", "stdio"), ("sql",), ("*.sql",),
        # npm 版目前在 Node 24 上装了也起不来（依赖 vscode-languageserver-protocol 的
        # 子路径导出不兼容）。要 SQL 支持建议用 Go 写的 sqls。
        "npm i -g sql-language-server（当前版本在 Node 24 上起不来，建议改用 sqls）",
    ),
    LspPreset(
        "terraform", "Terraform", "terraform-ls", ("serve",), ("terraform",), ("*.tf",),
        "解压 github.com/hashicorp/terraform-ls/releases 的 zip（免运行时）",
    ),
    LspPreset(
        "dockerfile", "Dockerfile", "docker-langserver", ("--stdio",), ("dockerfile",),
        ("Dockerfile",), "npm i -g dockerfile-language-server-nodejs",
    ),
    LspPreset(
        "markdown", "Markdown (marksman)", "marksman", ("server",), ("markdown",), ("*.md",),
        # marksman 直接发裸 exe，没有 zip
        "下载 github.com/artempyanykh/marksman/releases 的 marksman.exe（免运行时）",
    ),
    LspPreset(
        "zig", "Zig", "zls", (), ("zig",), ("build.zig", "*.zig"),
        "解压 github.com/zigtools/zls/releases 的 zip（需先装 Zig 工具链）",
    ),
)

#: 用户配置里可覆盖/新增的字段
_OVERRIDABLE = ("name", "command", "args", "languages", "rootMarkers", "initializationOptions", "env", "enabled",
                "lombokJar", "lombokEnabled", "mavenRepository", "isolateWorkspace")


def catalog() -> list[LspPreset]:
    return list(_CATALOG)


def preset(server_id: str) -> LspPreset | None:
    return next((p for p in _CATALOG if p.id == server_id), None)


def config_path() -> Path:
    from code_agent.config import agent_home

    return agent_home() / "lsp.json"


def load_config(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """读 ~/.code-agent/lsp.json。损坏时返回空配置（不让平台起不来）。

    用 `utf-8-sig` 读：Windows 上 PowerShell 的 `Set-Content -Encoding UTF8`、
    记事本"UTF-8 带 BOM"都会写入 BOM，而 `json.loads` 遇到 BOM 会直接报错——
    结果是用户的配置被静默忽略、退回内置目录（真机上就踩到过：
    明明配了 pyright，界面一直说"未安装"）。
    """
    path = Path(path) if path else config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


#: `resolve_command` 的结果缓存（TTL 秒）。为什么必须有：
#: Windows 上每次 `shutil.which` 要遍历 PATH × PATHEXT（十几个目录 × 十来个扩展名），
#: 28 个预设逐个解析要 2 秒多——项目一打开、面板一点就卡一下。
#: 缓存只记"某命令在某个根下解析到哪"，装完服务器后最多等 TTL 或重启即可刷新。
_RESOLVE_TTL = 120.0
_RESOLVE_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, str | None]] = {}
_RESOLVE_CACHE_MAX = 256


def clear_resolve_cache() -> None:
    """清空解析缓存（例如用户刚装完服务器，点「重新探测」时调用）。"""
    _RESOLVE_CACHE.clear()


def resolve_command(command: str, *roots: Path, use_cache: bool = True) -> str | None:
    """解析可执行文件：PATH → 项目内 node_modules/.bin → .venv/Scripts（Windows）/bin。"""
    if not command:
        return None
    import time

    key = (str(command), tuple(str(r) for r in roots))
    now = time.monotonic()
    if use_cache:
        hit = _RESOLVE_CACHE.get(key)
        if hit is not None and now - hit[0] < _RESOLVE_TTL:
            return hit[1]
    value = _resolve_uncached(command, *roots)
    if use_cache:
        if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
            _RESOLVE_CACHE.clear()
        _RESOLVE_CACHE[key] = (now, value)
    return value


def _resolve_uncached(command: str, *roots: Path) -> str | None:
    candidate = Path(command)
    if candidate.is_absolute() and candidate.exists():
        return str(candidate)
    found = shutil.which(command)
    if found:
        return found
    suffixes = (".cmd", ".exe", ".bat", "") if Path(command).suffix == "" else ("",)
    for root in roots:
        for sub in ("node_modules/.bin", ".venv/Scripts", ".venv/bin", "venv/bin", "venv/Scripts"):
            for suffix in suffixes:
                path = Path(root) / sub / f"{command}{suffix}"
                if path.exists():
                    return str(path)
    return None


def merged_servers(config: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """内置目录 ← 用户配置 合并，返回可直接用的服务器定义列表。

    用户配置以 id 为键：既能覆盖内置（换命令/加参数/关掉），也能新增自定义 server
    （`command` 必填）。
    """
    config = config or {}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _CATALOG:
        override = config.get(item.id) or {}
        entry = {
            "id": item.id,
            "name": override.get("name") or item.name,
            "command": override.get("command") or item.command,
            "args": list(override.get("args") or item.args),
            "languages": list(override.get("languages") or item.languages),
            "root_markers": list(override.get("rootMarkers") or item.root_markers),
            "install_hint": item.install_hint,
            "initialization_options": override.get("initializationOptions") or item.initialization_options,
            "env": override.get("env") or {},
            "enabled": bool(override.get("enabled", True)),
            "builtin": True,
        }
        if item.id == "jdtls":
            # Paths remain user-owned; launch preparation never rewrites lsp.json.
            entry.update({
                "lombok_jar": override.get("lombokJar"),
                "lombok_enabled": override.get("lombokEnabled", True),
                "maven_repository": override.get("mavenRepository"),
                "isolate_workspace": override.get("isolateWorkspace", True),
            })
        out.append(entry)
        seen.add(item.id)
    for sid, raw in config.items():
        if sid in seen or not isinstance(raw, dict):
            continue
        command = str(raw.get("command") or "").strip()
        if not command:
            continue
        out.append(
            {
                "id": sid,
                "name": str(raw.get("name") or sid),
                "command": command,
                "args": list(raw.get("args") or []),
                "languages": list(raw.get("languages") or []),
                "root_markers": list(raw.get("rootMarkers") or []),
                "install_hint": "自定义服务器（来自 lsp.json）",
                "initialization_options": raw.get("initializationOptions") or {},
                "env": raw.get("env") or {},
                "enabled": bool(raw.get("enabled", True)),
                "builtin": False,
            }
        )
    return out


def detect(
    *,
    languages: dict[str, int] | None = None,
    present_files: set[str] | None = None,
    config: dict[str, dict[str, Any]] | None = None,
    roots: tuple[Path, ...] = (),
) -> list[dict[str, Any]]:
    """按项目实况给出"建议启用 + 是否装了"的清单。

    languages 来自符号索引（哪种语言有几个文件），present_files 是项目根下的关键文件名。
    """
    languages = languages or {}
    present = {name.lower() for name in (present_files or set())}
    present_names = {Path(name).name.lower() for name in present}
    out: list[dict[str, Any]] = []
    for entry in merged_servers(config):
        relevant = any(languages.get(lang, 0) > 0 for lang in entry["languages"])
        marker_hit = any(
            marker.lower() in present_names or any(name.endswith(marker.lower().lstrip("*")) for name in present_names)
            for marker in entry["root_markers"]
        )
        resolved = resolve_command(entry["command"], *roots)
        out.append(
            {
                "id": entry["id"],
                "name": entry["name"],
                "command": entry["command"],
                "args": entry["args"],
                "languages": entry["languages"],
                "root_markers": entry["root_markers"],
                "install_hint": entry["install_hint"],
                "enabled": entry["enabled"],
                "builtin": entry["builtin"],
                "detected": bool(relevant or marker_hit),
                "language_hits": {lang: languages.get(lang, 0) for lang in entry["languages"] if languages.get(lang)},
                "installed": bool(resolved),
                "resolved_command": resolved or "",
                "state": (
                    "unavailable" if not entry["enabled"]
                    else "installed" if resolved
                    else "missing"
                ),
            }
        )
    return out
