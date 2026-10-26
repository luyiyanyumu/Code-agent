"""路径安全边界（M18 代码模式的第一道门）。

为什么单独一个模块：代码模式的工作区是**用户的真实仓库**，一个越界写就
不是"回答错了"而是"删了别人的代码"。所以边界判定集中在纯函数里，可以被
单测直接钉住，而不是散在各个接口里靠自觉。

三条规则：
1. 项目根必须是"看起来就该是项目"的目录——拒绝文件系统根、家目录本身、
   平台数据目录（~/.code-agent）、以及操作系统目录。
2. 相对路径一律 resolve 后判定：`..`、绝对路径、符号链接穿越都会被挡；
   判定结果必须真的落在项目根之下。
3. 忽略规则只有一处（`IGNORE_DIRS`），树接口与 P1 的全量扫描共用，
   否则"树里看不到、扫描却算进去了"会让地图和文件树长期不一致。
"""

from __future__ import annotations

import os
from pathlib import Path

from code_agent.config import agent_home


class ProjectPathError(ValueError):
    """项目路径不合法（对用户可见的 422 文案）。"""


#: 永不进入文件树与索引的目录名
IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn",
        "node_modules", "bower_components",
        ".venv", "venv", "env", ".tox", "__pypackages__",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
        "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit",
        "coverage", "htmlcov", ".nyc_output",
        ".idea", ".vscode", ".vs",
        ".tmp", ".trash", "site-packages",
    }
)

#: 操作系统目录（永远不允许作为项目根）
_SYSTEM_DIR_NAMES: frozenset[str] = frozenset(
    {
        "windows", "program files", "program files (x86)", "programdata",
        "system32", "syswow64", "perflogs", "recovery", "msocache",
        "etc", "usr", "bin", "sbin", "boot", "dev", "proc", "sys", "lib", "lib64",
        "var", "opt", "root", "srv", "run", "tmp",
    }
)


def root_key(path: str | Path) -> str:
    """路径归一化键：绝对 + 小写 + 正斜杠。

    Windows 上 `E:\\proj` 与 `e:/proj/` 是同一个目录，但字符串不等；
    项目唯一性判定必须走归一化键，否则会冒出两个指向同一仓库的"项目"。
    """
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:  # 极少数不可解析路径（超长/权限）——按原样归一化
        resolved = resolved.absolute()
    return str(resolved).replace("\\", "/").rstrip("/").lower()


def resolve_project_root(
    raw: str | Path, *, allow_agent_home: bool = False, for_creation_parent: bool = False
) -> Path:
    """校验并返回项目根（resolve 后的绝对路径）。不合法抛 ProjectPathError。

    allow_agent_home 仅供内部/测试使用；用户接口一律走默认 False。
    for_creation_parent 仅用于新建项目的容器目录：盘根和主目录可容纳独立
    子项目，但系统目录与平台数据边界仍生效；最终子目录需按普通项目根校验。
    """
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        raise ProjectPathError("路径不能为空")

    path = Path(text).expanduser()
    if not path.exists():
        raise ProjectPathError(f"路径不存在：{text}")
    if not path.is_dir():
        raise ProjectPathError(f"不是目录：{text}")
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ProjectPathError(f"路径无法解析：{text}（{exc}）") from exc

    # 文件系统根：`C:\` 或 `/`（parent 等于自己）
    if not for_creation_parent and resolved.parent == resolved:
        raise ProjectPathError("不能把文件系统根目录作为项目根")

    home = Path.home().resolve()
    if not for_creation_parent and resolved == home:
        raise ProjectPathError("不能把用户主目录本身作为项目根，请指向具体的工程目录")

    if not allow_agent_home:
        # 注意顺序：ProjectPathError 是 ValueError 的子类，判定必须放在 try 之外，
        # 否则 `raise` 会被自己的 `except ValueError` 吞掉（曾把这条边界静默失效）。
        inside_home = True
        try:
            resolved.relative_to(agent_home().resolve())
        except ValueError:
            inside_home = False
        if inside_home:
            raise ProjectPathError("不能把 Code-agent 的数据目录作为项目根")

    # 系统目录：只在"正好是那个目录本身"时拒绝（避免误伤 C:\Users\x\etc 这类正常工程）
    if resolved.name.strip().lower() in _SYSTEM_DIR_NAMES and resolved.parent == resolved.parent.parent:
        raise ProjectPathError(f"系统目录不允许作为项目根：{resolved}")

    return resolved


def safe_child(root: str | Path, raw: str | Path) -> Path:
    """把项目内相对路径解析成绝对路径；越界抛 ProjectPathError。

    只做边界判定，不检查存在性（新建文件也需要解析路径）。
    """
    base = Path(root).resolve()
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return base
    candidate = Path(text)
    if candidate.is_absolute():
        raise ProjectPathError("只接受项目内的相对路径")
    target = (base / candidate)
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise ProjectPathError(f"路径无法解析：{raw}（{exc}）") from exc
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ProjectPathError(f"路径越出项目范围：{raw}") from None
    return resolved


def normalize_rel(raw: str | Path) -> str:
    """把相对路径归一成 `a/b/c` 形式（空串表示项目根）。"""
    text = str(raw or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    parts = [p for p in text.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ProjectPathError("相对路径不允许包含 ..")
    return "/".join(parts)


def relative_of(root: str | Path, path: str | Path) -> str:
    """返回 path 相对 root 的归一化相对路径（用于回给前端）。"""
    base = Path(root).resolve()
    target = Path(path).resolve()
    try:
        rel = target.relative_to(base)
    except ValueError:
        raise ProjectPathError(f"路径越出项目范围：{path}") from None
    return normalize_rel(str(rel))


def is_ignored_dir(name: str) -> bool:
    """目录名是否在忽略名单里（大小写不敏感：Windows 上 .Git 也是 .git）。"""
    return name.strip().lower() in IGNORE_DIRS


def is_probably_binary(blob: bytes) -> bool:
    """二进制探测：NUL 字节是文本文件里几乎不会出现的信号。"""
    return b"\x00" in blob


def path_is_within(candidate: str | Path, base: str | Path) -> bool:
    """candidate 是否在 base 之内（resolve 后判定，防符号链接穿越）。"""
    try:
        Path(candidate).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def same_path(a: str | Path, b: str | Path) -> bool:
    """同一目录判定（跨平台大小写差异走 normcase）。"""
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return str(a) == str(b)
