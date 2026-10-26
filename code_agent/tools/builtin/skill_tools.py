"""技能商店工具（M20）：让 agent **自己**把技能装进 Code-agent 的技能目录。

为什么需要它：技能目录（`~/.code-agent/skills/`）是 Code-agent 的扩展面，但此前只有
`load_skill`（把已有技能读进上下文），**没有任何"装进来"的能力**。于是用户说
"给自己装个技能"时，模型只能去 shell 里 `curl ... | bash`——在 Windows 上那条路
直接断（skillhub 的入口是 bash 脚本，`/` 管道在 cmd.exe 里也不成立，真机上表现为
`'head' is not recognized as an internal or external command`）。

这里把那条路换成**原生工具**：SkillHub 的本体其实是一个纯标准库的 Python 脚本
（`~/.skillhub/skills_store_cli.py`），所以直接用 Python 调它，不依赖 bash/curl：

| 工具 | 做什么 | 权限 |
| --- | --- | --- |
| `skill_list` | 列出本地已装技能（直接读目录，离线） | 只读，放行 |
| `skill_search` | 搜技能商店（`search --json`） | 只读，放行 |
| `skill_install` | 装到 Code-agent 的技能目录（`install --json --dir …`） | **要审批**（装软件是写操作） |

安装目标是 `skills_dir()`（即 `~/.code-agent/skills/`）——不是 `./skills`，
这是 skillhub 文档里明确要求的：装错地方等于没装（`--dir` 指错就"不会被识别"）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from code_agent.config import skills_dir
from code_agent.tools.base import Tool, ToolContext, ToolError

#: SkillHub CLI（纯标准库脚本，用当前解释器直接跑，不经过 bash 包装脚本）
CLI_CANDIDATES = (
    Path.home() / ".skillhub" / "skills_store_cli.py",
    Path(os.environ.get("SKILLHUB_HOME", "")) / "skills_store_cli.py",
)
#: 搜索要联网；安装要下载并解压，给宽一点
SEARCH_TIMEOUT = 60
INSTALL_TIMEOUT = 180
#: 喂回模型的输出上限
MAX_OUTPUT = 6000
MAX_ROWS = 20


def cli_path() -> Path | None:
    for candidate in CLI_CANDIDATES:
        if candidate and candidate.is_file():
            return candidate
    return None


def _missing_cli_hint() -> str:
    return (
        "本机没有 SkillHub CLI（找过 ~/.skillhub/skills_store_cli.py）。"
        "它的安装脚本是 bash 的（`curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/"
        "install/install.sh | bash`），Windows 上需要有 bash（Git Bash / WSL）才能跑；"
        "装好之后这个工具会直接调用其中的 Python 脚本，不再依赖 bash。"
    )


async def _run_cli(args: list[str], *, timeout: int) -> tuple[int, str, str]:
    """跑 CLI，返回 (返回码, stdout, stderr)。不经过 shell，参数按列表传。"""
    cli = cli_path()
    if cli is None:
        raise ToolError(_missing_cli_hint())
    cmd = [sys.executable or "python", str(cli), "--skip-self-upgrade", *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path.home()),
        )
    except OSError as exc:
        raise ToolError(f"启动 SkillHub CLI 失败：{exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        with __import__("contextlib").suppress(Exception):
            proc.kill()
        raise ToolError(f"SkillHub CLI 超时（{timeout}s）：可能是网络不通") from exc
    return (
        int(proc.returncode or 0),
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _parse_json(text: str) -> Any | None:
    """CLI 的 --json 输出里可能混着进度行：取第一段可解析的 JSON。"""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _row(item: dict[str, Any]) -> str:
    slug = str(item.get("slug") or item.get("id") or item.get("name") or "?")
    # 显示名优先 display_name/title，其次 name（CLI 的搜索结果里 name 就是显示名）
    name = str(item.get("display_name") or item.get("title") or item.get("name") or "")
    if name == slug:
        name = ""
    version = str(item.get("version") or "")
    desc = " ".join(str(item.get("description") or item.get("summary") or "").split())
    bits = [f"`{slug}`"]
    if name:
        bits.append(name)
    if version:
        bits.append(f"v{version}")
    line = " · ".join(bits)
    return f"- {line}" + (f"\n  {desc[:160]}" if desc else "")


def _split_slug(slug: str, namespace: str = "") -> tuple[str, str]:
    """把 `@命名空间/slug` 拆成 (slug, 命名空间)。

    **注意**：SkillHub CLI 的 install 接受两种写法，实测差别很大：
    - `install @ns/slug`（整串当一个参数）→ 真的装，落到 `skills/@ns/slug/`；
    - `install slug --namespace ns` → 退出码 0 但**什么都不装**（真机踩到）。
    所以默认保留整串，只在调用方显式给了 namespace 且 slug 不带 `@` 时才用 `--namespace`。
    """
    text = (slug or "").strip()
    ns = (namespace or "").strip()
    if text.startswith("@"):
        return text, ""            # 整串交给 CLI（这是能装上的写法）
    return text, ns


def skill_docs(root: Path | None = None) -> dict[str, Path]:
    """已装技能：名字 → SKILL.md 路径（复用 PackRegistry 的扫描规则，支持命名空间布局）。"""
    from code_agent.extensions.packs import iter_pack_docs

    base = root or skills_dir()
    docs: dict[str, Path] = {}
    for doc in iter_pack_docs(base, "SKILL.md"):
        docs[doc.parent.name] = doc
    return docs


class SkillListTool(Tool):
    """列出 Code-agent 技能目录里已装的技能（离线，直接读盘）。"""

    name = "skill_list"
    description = (
        "List the skills installed in this agent's skills directory "
        "(~/.code-agent/skills). Offline and read-only; use skill_search to find new ones."
    )
    description_zh = "列出本机已安装的技能（读 ~/.code-agent/skills，离线）。"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        base = skills_dir()
        if not base.is_dir():
            return f"技能目录不存在：{base}（可以用 skill_search + skill_install 装一个）"
        docs = skill_docs(base)
        if not docs:
            return f"技能目录是空的：{base}"
        lines = [f"已安装 {len(docs)} 个技能（`{base}`）："]
        for name, doc in list(docs.items())[:MAX_ROWS]:
            desc = ""
            try:
                head = doc.read_text(encoding="utf-8", errors="replace")[:600]
                for line in head.split("\n"):
                    if line.strip().lower().startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip("\"'")
                        break
                if not desc:
                    for line in head.split("\n"):
                        if line.strip() and not line.strip().startswith(("---", "#")):
                            desc = line.strip()
                            break
            except OSError:
                desc = ""
            # 命名空间布局（SkillHub 装的社区技能）额外标出所在子目录，方便定位
            rel = doc.parent.relative_to(base).as_posix()
            tag = f"（{rel}）" if rel != name else ""
            lines.append(f"- `{name}`{tag}" + (f"：{desc[:120]}" if desc else ""))
        if len(docs) > MAX_ROWS:
            lines.append(f"…另有 {len(docs) - MAX_ROWS} 个")
        lines.append("\n用 `load_skill <name>` 把它读进上下文即可使用。")
        return "\n".join(lines)


class SkillSearchTool(Tool):
    """搜技能商店（SkillHub）。"""

    name = "skill_search"
    description = (
        "Search the SkillHub store for installable skills. Returns slug, name, version and "
        "description for each hit; install one with skill_install."
    )
    description_zh = "搜技能商店（SkillHub），返回 slug / 名称 / 版本 / 说明。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "搜索词（必填）"}},
        "required": ["query"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能为空")
        code, out, err = await _run_cli(["search", "--json", *query.split()], timeout=SEARCH_TIMEOUT)
        payload = _parse_json(out)
        if code != 0 and payload is None:
            raise ToolError(f"搜索失败（退出码 {code}）：{(err or out).strip()[:300]}")
        items = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return (out or err)[:MAX_OUTPUT] or "没有结果"
        if not items:
            return f"没搜到「{query}」相关的技能。"
        body = "\n".join(_row(i) for i in items[:MAX_ROWS] if isinstance(i, dict))
        more = f"\n（共 {len(items)} 条，只列前 {MAX_ROWS} 条）" if len(items) > MAX_ROWS else ""
        return f"搜「{query}」命中 {len(items)} 个：\n{body}{more}\n\n用 skill_install 装其中一个（例如 `skill_install slug=<上面反引号里的值>`）。"


class SkillInstallTool(Tool):
    """把技能装进 Code-agent 的技能目录。"""

    name = "skill_install"
    description = (
        "Install a skill from the SkillHub store into this agent's own skills directory "
        "(~/.code-agent/skills), so the agent gains that capability. Requires approval."
    )
    description_zh = "把技能商店里的技能装到 Code-agent 自己的技能目录（需要审批）。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "技能 slug（skill_search 返回的反引号值，可带 @命名空间/）"},
            "namespace": {"type": "string", "description": "社区命名空间（可选；可写进 slug 的 @ns/ 前缀）"},
            "force": {"type": "boolean", "description": "已装过时覆盖重装（默认 false）"},
        },
        "required": ["slug"],
    }

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        raw_slug = str(arguments.get("slug") or "").strip()
        if not raw_slug:
            raise ToolError("slug 不能为空")
        slug, namespace = _split_slug(raw_slug, str(arguments.get("namespace") or ""))
        target = skills_dir()
        target.mkdir(parents=True, exist_ok=True)
        args = ["--dir", str(target), "install", "--json", slug]
        if namespace:
            args += ["--namespace", namespace]
        if arguments.get("force"):
            args.append("--force")
        code, out, err = await _run_cli(args, timeout=INSTALL_TIMEOUT)
        payload = _parse_json(out)
        combined = f"{out}\n{err}"
        if code != 0:
            # 已经装过不是失败：CLI 会说 "Target exists … use --force"，如实转述并给出办法
            if "target exists" in combined.lower():
                return (
                    f"`{raw_slug}` 已经装过了（目录里已有 {slug.split('/')[-1]}）。"
                    "要覆盖重装就再调一次并传 force=true；只想升级的话用 skillhub 的 upgrade。"
                )
            raise ToolError(f"安装 {raw_slug} 失败（退出码 {code}）：{(err or out).strip()[:400]}")

        # 不只看退出码：确认文件真的落在了 Code-agent 的技能目录里。
        # 实测两种失败都能被这一步抓住：
        #   · `install slug --namespace ns` → 退出码 0 但什么都没装；
        #   · 装到 `skills/@ns/slug/` 这种命名空间布局 → 一层目录扫不到（所以要按同一套
        #     扫描规则找：PackRegistry.iter_pack_docs 认两种布局）。
        docs = skill_docs(target)
        hit = next((n for n in docs if n == slug or slug.split("/")[-1] in n), "")
        name = str((payload or {}).get("name") or (payload or {}).get("slug") or slug) if isinstance(payload, dict) else slug
        lines = [f"已安装技能 `{raw_slug}`（命名空间 {namespace or '默认'}）到 {target}"]
        if hit:
            files = sorted(p.name for p in docs[hit].parent.iterdir())[:8]
            lines.append(f"落地目录：{docs[hit].parent}")
            lines.append(f"其中：{'、'.join(files)}")
        else:
            lines.append(
                f"注意：退出码是 0，但目录里没看到 {slug}"
                f"（当前已装：{'、'.join(list(docs)[:8]) or '无'}）。"
                f"下面附上 CLI 的输出，便于判断是「没找到这个技能」还是「装到了别处」："
            )
            lines.append((out or err).strip()[:400] or "(CLI 没有输出)")
        lines.append(f"\n接下来用 `load_skill {hit or name}` 把它的 SKILL.md 读进上下文就能用；"
                     "系统提示里的技能清单要下一条消息/新会话才会刷新。")
        return "\n".join(lines)


def build_skill_tools() -> list[Tool]:
    return [SkillListTool(), SkillSearchTool(), SkillInstallTool()]
