"""能力「制作与上传」（M11）：让插件 / 技能 / MCP 配置可以从页面直接落盘。

此前这三样都只能 SSH 到服务器手工建文件——能用，但分发与维护门槛高。
这里把「写文件」收成带校验的 API，前端页面直接调用。

安全边界（全部 admin-only，且只写白名单位置）：
- 插件 zip：标准库 zipfile 解包，**防 zip-slip**（逐条 resolve 校验）、
  大小/条目数上限、顶层文件白名单（plugin.json / prompt.md / tools.py / skills/**）。
  代码式 tools.py 仍受 M10 双重开关约束，上传本身不改变加载策略。
- 技能：只写 skills/<name>/SKILL.md，frontmatter 由表单字段统一生成
  （用户贴的正文若带 frontmatter 会被剥掉，避免两套来源打架）。
- MCP：mcp.json 整份改写前先逐 server 校验（command 必填、字段白名单），
  校验不过整份不写——坏配置不能把下次启动搞挂。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any

#: 目录名 / 技能名 / MCP server 名共用一条命名规则（也是路径安全的一部分）
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: 插件包顶层允许出现的文件（skills/ 下的任意深度都允许）
_PLUGIN_TOP_FILES = {"plugin.json", "prompt.md", "tools.py"}
PLUGIN_SKILLS_DIR = "skills"

MAX_ZIP_BYTES = 5 * 1024 * 1024          # zip 原始大小
MAX_ENTRIES = 200                        # 条目数
MAX_TOTAL_UNCOMPRESSED = 8 * 1024 * 1024  # 解压后总大小
MAX_SKILL_BYTES = 512 * 1024             # 单个技能正文
MAX_RESOURCE_BYTES = 256 * 1024          # 单个技能附带资源文件

#: mcp.json 里允许出现的字段（多余字段拒绝——拼错字段静默生效比报错更糟）
MCP_SERVER_KEYS = {"command", "args", "env", "trusted", "enabled"}


class AuthoringError(ValueError):
    """制作/上传相关校验失败。message 直接给前端展示。"""


# ======================================================================
# 插件 zip 上传
# ======================================================================
def _clean_entry(raw: str) -> str | None:
    """规范化 zip 条目路径；不合法返回 None（目录条目/空路径）。"""
    name = raw.replace("\\", "/").strip()
    if not name or name.endswith("/"):
        return None
    # 统一剥掉开头 ./ 与 /，拒绝绝对路径与 ..
    while name.startswith("./"):
        name = name[2:]
    name = name.lstrip("/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise AuthoringError(f"zip 条目含非法路径（.. 或绝对路径）：{raw}")
    if any(p.startswith(".") for p in parts):
        raise AuthoringError(f"zip 条目含隐藏文件/目录：{raw}")
    return "/".join(parts)


def _strip_common_top(contents: dict[str, bytes]) -> dict[str, bytes]:
    """GitHub 打包的 zip 常带一层 <repo>-<ver>/ 顶层目录：全部条目同源就剥掉。"""
    names = list(contents)
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(tops) == 1 and names and all("/" in n for n in names):
        return {n.split("/", 1)[1]: b for n, b in contents.items()}
    return contents


def extract_plugin_zip(
    data: bytes,
    plugins_root: Path,
    *,
    zip_name: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """把插件 zip 解到 plugins/<name>/。返回 {"name", "path", "files"}。

    任何问题抛 AuthoringError（message 可直接给用户看），**失败不留半成品**。
    """
    plugins_root = Path(plugins_root)
    if len(data) > MAX_ZIP_BYTES:
        raise AuthoringError(f"zip 超过大小上限（{MAX_ZIP_BYTES // 1024 // 1024} MB）")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos: list[tuple[str, zipfile.ZipInfo]] = []
            for info in zf.infolist():
                cleaned = _clean_entry(info.filename)
                if cleaned:
                    infos.append((cleaned, info))
            if not infos:
                raise AuthoringError("zip 里没有文件")
            if len(infos) > MAX_ENTRIES:
                raise AuthoringError(f"zip 条目数超过上限（{MAX_ENTRIES}）")
            total = sum(i.file_size for _, i in infos)
            if total > MAX_TOTAL_UNCOMPRESSED:
                raise AuthoringError(
                    f"解压后总大小超过上限（{MAX_TOTAL_UNCOMPRESSED // 1024 // 1024} MB）"
                )
            # 在 with 块内一次性读出（总量已被上限约束）
            contents: dict[str, bytes] = {cleaned: zf.read(info) for cleaned, info in infos}
    except zipfile.BadZipFile as exc:
        raise AuthoringError(f"不是合法的 zip 文件：{exc}") from exc

    contents = _strip_common_top(contents)

    names = list(contents)

    if "plugin.json" not in names:
        raise AuthoringError(
            "zip 根目录必须包含 plugin.json（清单是唯一必需文件，格式见页面「制作规范」）"
        )
    for n in names:
        top = n.split("/", 1)[0]
        if top not in _PLUGIN_TOP_FILES and top != PLUGIN_SKILLS_DIR:
            raise AuthoringError(
                f"zip 含清单之外的顶层内容「{top}/」——只允许 plugin.json / prompt.md / "
                f"tools.py / skills/（多余内容请放进插件内嵌目录或删掉）"
            )

    # 目标目录名：优先清单里的 name，其次 zip 文件名
    dir_name = ""
    try:
        raw_manifest = json.loads(contents["plugin.json"].decode("utf-8"))
        if isinstance(raw_manifest, dict):
            dir_name = str(raw_manifest.get("name") or "")
    except (json.JSONDecodeError, UnicodeDecodeError):
        dir_name = ""  # 清单坏了也让 parse_manifest 在扫描时报，上传先按文件名落位
    dir_name = dir_name or Path(zip_name or "plugin").stem
    dir_name = dir_name.strip()
    if not NAME_RE.match(dir_name):
        raise AuthoringError(
            f"插件名「{dir_name}」不合法：需以字母/数字开头，只含字母数字点下划线连字符，≤64 字符"
        )

    dest = plugins_root / dir_name
    if dest.exists():
        if not overwrite:
            raise AuthoringError(
                f"插件目录 {dir_name}/ 已存在。要覆盖请勾选「覆盖同名插件」"
            )
        _move_to_trash(dest, plugins_root)
    dest.mkdir(parents=True)

    for cleaned in names:
        target = (dest / cleaned).resolve()
        if dest.resolve() not in target.parents:
            _move_to_trash(dest, plugins_root)  # 失败不留半成品（旧目录进回收站）
            raise AuthoringError(f"zip 条目越界：{cleaned}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[cleaned])

    return {"name": dir_name, "path": str(dest), "files": len(names)}


def _trash_dir(plugins_root: Path) -> Path:
    return plugins_root / ".trash"


def _move_to_trash(path: Path, root: Path) -> str:
    """把 path 移入 root/.trash/（带时间戳后缀），返回回收站内的名字。

    用 rename 而不是递归删除：删除是危险操作（误删不可恢复），移动则可找回；
    同时天然避开 Windows 上各类删除守卫（回收站策略 / 安全删除拦截）。
    """
    resolved = Path(path).resolve()
    if Path(root).resolve() not in resolved.parents:
        raise AuthoringError(f"拒绝移动 {resolved}：不在插件目录内")
    trash = _trash_dir(root)
    trash.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = trash / f"{resolved.name}-{stamp}"
    while target.exists():  # 同秒多次操作时避免覆盖
        target = trash / f"{resolved.name}-{stamp}-{os.urandom(2).hex()}"
    resolved.rename(target)
    return target.name


def delete_plugin_dir(plugins_root: Path, plugin_path: Path) -> dict[str, Any]:
    """「删除」插件：整个目录移入 plugins/.trash/（可手动找回）。"""
    trashed_as = _move_to_trash(plugin_path, plugins_root)
    return {
        "trashed_as": trashed_as,
        "trash_path": str(_trash_dir(plugins_root) / trashed_as),
    }


# ======================================================================
# 技能上传
# ======================================================================
_FRONTMATTER_RE = re.compile(r"\s*---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _clean_resource_path(raw: str) -> str:
    """校验并规范化技能附带资源路径（相对技能目录），返回 / 分隔的相对路径。"""
    name = str(raw or "").replace("\\", "/").strip()
    while name.startswith("./"):
        name = name[2:]
    name = name.lstrip("/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        raise AuthoringError(f"资源路径不合法：{raw}")
    if any(p == ".." for p in parts):
        raise AuthoringError(f"资源路径含非法 ..：{raw}")
    if any(p.startswith(".") for p in parts):
        raise AuthoringError(f"资源路径含隐藏文件/目录：{raw}")
    if parts[-1].upper() == "SKILL.MD":
        raise AuthoringError("SKILL.md 由表单生成，不能作为附带资源")
    return "/".join(parts)


def write_skill_file(
    skills_root: Path,
    name: str,
    description: str,
    content: str,
    resources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """写 skills/<name>/SKILL.md（+ 可选附带资源），frontmatter 统一生成。

    resources: [{path: "scripts/x.py", content: "…"}]
               或 [{path: "assets/i.png", content_b64: "…"}]（二进制用 base64）。
    返回 {"name", "path", "files"}。校验全部通过才落盘，失败不留半成品。
    """
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise AuthoringError(
            f"技能名「{name}」不合法：需以字母/数字开头，只含字母数字点下划线连字符，≤64 字符"
        )
    description = str(description or "").strip().replace("\n", " ")
    if not description:
        raise AuthoringError("description 不能为空——它是进 system prompt 目录的一句话简介")
    body = str(content or "").strip()
    if not body:
        raise AuthoringError("技能正文不能为空")
    if len(body.encode("utf-8")) > MAX_SKILL_BYTES:
        raise AuthoringError(f"技能正文超过大小上限（{MAX_SKILL_BYTES // 1024} KB）")
    # 贴进来的内容若自带 frontmatter，剥掉——name/description 以表单为准
    m = _FRONTMATTER_RE.match(body)
    if m:
        body = m.group(2).strip()

    # 先校验所有资源（路径 + 大小），全部通过再写盘
    parsed: list[tuple[str, bytes]] = []
    for res in resources or []:
        rpath = _clean_resource_path(str(res.get("path") or ""))
        if res.get("content") is not None:
            data = str(res["content"]).encode("utf-8")
        else:
            b64 = res.get("content_b64")
            if not b64:
                raise AuthoringError(f"资源 {rpath} 缺少 content 或 content_b64")
            try:
                data = base64.b64decode(str(b64), validate=True)
            except Exception as exc:
                raise AuthoringError(f"资源 {rpath} 的 content_b64 不是合法 base64") from exc
        if len(data) > MAX_RESOURCE_BYTES:
            raise AuthoringError(
                f"资源 {rpath} 超过大小上限（{MAX_RESOURCE_BYTES // 1024} KB）"
            )
        parsed.append((rpath, data))

    d = Path(skills_root) / name
    d.mkdir(parents=True, exist_ok=True)
    doc = d / "SKILL.md"
    doc.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")

    files = ["SKILL.md"]
    for rpath, data in parsed:
        target = (d / rpath).resolve()
        if d.resolve() not in target.parents:
            raise AuthoringError(f"资源越界：{rpath}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append(rpath)
    return {"name": name, "path": str(doc), "files": files}


def extract_skill_zip(
    data: bytes,
    skills_root: Path,
    *,
    zip_name: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """把技能 zip **整体**解到 skills/<name>/。返回 {"name", "path", "files"}。

    技能包 = 技能目录整体打包：SKILL.md 在 zip 根，或所有文件在同一个顶层
    目录下（自动剥掉）；可带 scripts/、references/、assets/ 等任意子文件。
    任何问题抛 AuthoringError；覆盖同名技能时旧目录移入 .trash（可找回）。
    """
    skills_root = Path(skills_root)
    if len(data) > MAX_ZIP_BYTES:
        raise AuthoringError(f"zip 超过大小上限（{MAX_ZIP_BYTES // 1024 // 1024} MB）")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos: list[tuple[str, zipfile.ZipInfo]] = []
            for info in zf.infolist():
                cleaned = _clean_entry(info.filename)
                if cleaned:
                    infos.append((cleaned, info))
            if not infos:
                raise AuthoringError("zip 里没有文件")
            if len(infos) > MAX_ENTRIES:
                raise AuthoringError(f"zip 条目数超过上限（{MAX_ENTRIES}）")
            total = sum(i.file_size for _, i in infos)
            if total > MAX_TOTAL_UNCOMPRESSED:
                raise AuthoringError(
                    f"解压后总大小超过上限（{MAX_TOTAL_UNCOMPRESSED // 1024 // 1024} MB）"
                )
            contents: dict[str, bytes] = {cleaned: zf.read(info) for cleaned, info in infos}
    except zipfile.BadZipFile as exc:
        raise AuthoringError(f"不是合法的 zip 文件：{exc}") from exc

    names = list(contents)

    # 定位技能根目录：SKILL.md 所在目录即根（zip 根或统一顶层目录）
    skill_docs = [n for n in names if n.rsplit("/", 1)[-1] == "SKILL.md"]
    if not skill_docs:
        raise AuthoringError(
            "zip 里必须包含 SKILL.md——技能包就是技能目录（SKILL.md + 可选 "
            "scripts/、references/、assets/）整体打包"
        )
    skill_doc = skill_docs[0]
    base = skill_doc.rpartition("/")[0]  # "" 表示 SKILL.md 就在 zip 根
    prefix = f"{base}/" if base else ""
    if not all(n.startswith(prefix) for n in names):
        raise AuthoringError(
            f"zip 内容必须在技能根目录内（一个 zip 只能打包一个技能，含 {skill_doc}）"
        )
    contents = {n[len(prefix) :]: b for n, b in contents.items()}
    names = list(contents)

    # 技能名：frontmatter name > 技能根目录名 > zip 文件名
    skill_name = ""
    try:
        text = contents["SKILL.md"].decode("utf-8", errors="replace")
        m = _FRONTMATTER_RE.match(text)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    if k.strip().lower() == "name":
                        skill_name = v.strip()
                        break
    except Exception:
        skill_name = ""
    skill_name = skill_name or base or Path(zip_name or "skill").stem
    skill_name = skill_name.strip()
    if not NAME_RE.match(skill_name):
        raise AuthoringError(
            f"技能名「{skill_name}」不合法：需以字母/数字开头，只含字母数字点下划线连字符，≤64 字符"
        )

    dest = skills_root / skill_name
    if dest.exists():
        if not overwrite:
            raise AuthoringError(f"技能目录 {skill_name}/ 已存在。要覆盖请勾选「覆盖同名技能」")
        _move_to_trash(dest, skills_root)
    dest.mkdir(parents=True)

    for cleaned in names:
        target = (dest / cleaned).resolve()
        if dest.resolve() not in target.parents:
            _move_to_trash(dest, skills_root)  # 失败不留半成品（旧目录进回收站）
            raise AuthoringError(f"zip 条目越界：{cleaned}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[cleaned])

    return {"name": skill_name, "path": str(dest), "files": len(names)}


# ======================================================================
# MCP 配置读写
# ======================================================================
def load_mcp_config(path: Path) -> dict[str, dict[str, Any]]:
    """读 mcp.json 的 mcpServers（不存在/坏 JSON 返回空，不抛——与 McpManager 行为一致）。"""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}


def normalize_mcp_servers(servers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """校验并规范化整份 server 配置。任何一条不合法就整份报错（坏配置不落盘）。"""
    out: dict[str, dict[str, Any]] = {}
    for name, cfg in (servers or {}).items():
        name = str(name).strip()
        if not NAME_RE.match(name):
            raise AuthoringError(
                f"MCP server 名「{name}」不合法：需以字母/数字开头，只含字母数字点下划线连字符"
            )
        if not isinstance(cfg, dict):
            raise AuthoringError(f"server {name} 的配置必须是对象")
        unknown = set(cfg) - MCP_SERVER_KEYS
        if unknown:
            raise AuthoringError(
                f"server {name} 含不认识的字段 {sorted(unknown)}——允许的字段：command / args / env / trusted"
            )
        command = str(cfg.get("command") or "").strip()
        if not command:
            raise AuthoringError(f"server {name} 缺少 command（要启动的可执行程序）")
        clean: dict[str, Any] = {"command": command}

        args = cfg.get("args")
        if args is not None:
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise AuthoringError(f"server {name} 的 args 必须是字符串数组")
            if args:
                clean["args"] = args
        env = cfg.get("env")
        if env is not None:
            if not isinstance(env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()
            ):
                raise AuthoringError(f"server {name} 的 env 必须是 字符串->字符串 的对象")
            if env:
                clean["env"] = env
        if cfg.get("trusted"):
            clean["trusted"] = True
        # M22：页面上的「启用/停用」就写这个字段（false 时不启动子进程）
        if cfg.get("enabled") is False:
            clean["enabled"] = False
        out[name] = clean
    return out


def save_mcp_config(path: Path, servers: dict[str, Any]) -> dict[str, Any]:
    """整份改写 mcp.json。返回写入后的 servers（供前端回显）。"""
    clean = normalize_mcp_servers(servers)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"mcpServers": clean}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return clean
