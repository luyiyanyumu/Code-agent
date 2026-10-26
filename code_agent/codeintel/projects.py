"""项目服务（M18 代码模式 · P0）。

职责边界刻意收窄：
- **登记**：把"某个目录"登记成项目（打开既有仓库 / 在平台目录下新建）
- **浏览**：目录树（懒加载一层）与文件文本（可带行区间）
- **不解析符号、不建索引**——那是 P1 的 scanner/parsers/graph

安全模型（三条，缺一条就是事故）：
1. 项目根经 `paths.resolve_project_root` 白名单校验，用户的任意路径不能
   直接把平台数据目录或系统盘根登记成项目。
2. 所有项目内路径经 `paths.safe_child` 解析并判定，`..`/绝对路径/符号链接
   穿越一律拒绝。
3. 删除项目默认只删登记记录；只有 `origin='create'`（平台自己建的）且在
   `projects_root()` 之下，才允许连目录一起删。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import string
import time
from dataclasses import dataclass
from pathlib import Path

from code_agent.codeintel import paths as P
from code_agent.codeintel.uri import file_uri
from code_agent.codeintel.hashing import sha1_text
from code_agent.codeintel.text_encoding import decode_text, read_text_range
from code_agent.codeintel.vcs import RepoInfo, git_available, probe_repo
from code_agent.config import projects_root
from code_agent.storage.db import ProjectRepo

#: 单文件读取上限（不带行区间时）。超过就要求带 start/end，避免把 5MB 日志糊给前端
MAX_FILE_BYTES = 512 * 1024
#: 编辑器可用上限：超过这个体积在浏览器里编辑会明显掉帧，改为只读 + 提示
MAX_EDIT_BYTES = 2 * 1024 * 1024
#: 计算 sha1 / 行数时的硬上限：再大就只读区间、不统计总行数
MAX_HASH_BYTES = 64 * 1024 * 1024
#: 单次返回的最大行数
MAX_RANGE_LINES = 5000
#: 目录树单层最多返回条目数
MAX_TREE_ENTRIES = 2000

#: 扩展名 → 语言（用于高亮与（P1）解析器选择）
_LANG_BY_EXT: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".vue": "vue", ".svelte": "svelte",
    ".json": "json", ".jsonc": "json",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".less": "less",
    ".md": "markdown", ".markdown": "markdown",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell", ".bat": "bat", ".cmd": "bat",
    ".sql": "sql", ".xml": "xml", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".rb": "ruby", ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift", ".dart": "dart", ".lua": "lua", ".zig": "zig",
    ".tf": "terraform", ".dockerfile": "dockerfile", ".proto": "proto",
    ".txt": "text", ".log": "text", ".csv": "csv", ".env": "ini",
}

#: 视为"含密钥"的文件名模式：仍可在树里看到（用户有权看自己的文件），
#: 但 P1 的索引与发给模型的上下文会跳过它们（默认不外传）。
_SECRET_PATTERNS = (
    r"^\.env(\..+)?$", r"^\.npmrc$", r"^\.pypirc$", r"^\.netrc$",
    r"^id_(rsa|dsa|ecdsa|ed25519)(\..+)?$", r".*\.(pem|p12|pfx|key|keystore|jks)$",
    r"^credentials(\..+)?$", r"^\.git-credentials$", r"^secrets?\.(ya?ml|json|toml)$",
)
_SECRET_RE = re.compile("|".join(f"(?:{p})" for p in _SECRET_PATTERNS), re.IGNORECASE)


def _rmtree_force(path: Path) -> bool:
    """整目录删除，返回是否真的删干净了。

    Windows 上 git 的 loose object 带着只读属性，`shutil.rmtree(..., ignore_errors=True)`
    会静默跳过它们，留下一个 `.git/objects` 空壳——用户看到"项目没了"但磁盘上
    还躺着一堆目录。所以在失败时清掉只读位再删一次，并把最终结果如实返回。
    """
    def _retry(func, target, _exc):  # noqa: ANN001
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onerror=_retry)
    except OSError:
        pass
    return not path.exists()


# ---------------------------------------------------------------------------
# 目录选择器（浏览）用的小工具
# ---------------------------------------------------------------------------
#: 盘符根上没有任何价值、只有噪音的目录（Windows）
_SYSTEM_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "programdata", "recovery",
    "perflogs", "config.msi", "msocache", "intel", "amd", "nvidia",
}
_HIDDEN_DIR_NAMES = {
    "$recycle.bin", "$windows.~bt", "$windows.~ws", "system volume information",
    "$sysreset", "documents and settings", "all users", "default user",
}


def _dir_entry(entry: Path) -> dict:
    """列表里的一行目录：名称 + 修改时间 + 是不是工程（原生选择器也是这个信息量）。"""
    mtime = 0.0
    try:
        mtime = entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        pass
    looks_like_project = any(
        (entry / marker).exists()
        for marker in (".git", "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "pom.xml")
    )
    return {
        "name": entry.name,
        "path": str(entry),
        "mtime": mtime,
        "is_project": looks_like_project,
    }


def _breadcrumbs(current: Path) -> list[dict]:
    """面包屑：`C:\\` → `Users` → `dyh` → …（点任意一段即可跳回去）。"""
    parts = list(current.parts)
    out: list[dict] = []
    for index, part in enumerate(parts):
        name = part
        if index == 0:
            name = part if part.endswith("\\") or part == "/" else part + os.sep
            out.append({"name": name, "path": name})
            continue
        parent = out[-1]["path"]
        out.append({"name": name, "path": str(Path(parent) / part)})
    return out


def _drives() -> list[dict]:
    """可选根位置。Windows 上列出**所有逻辑盘符**（换盘不该靠手敲路径）。

    用 `GetLogicalDrives` 的位掩码而不是逐个 `exists("A:\\\\")`：后者在
    未插入的读卡器/断开的网络盘上会卡住（真机上表现为"浏览…"转圈）。
    """
    if os.name != "nt":
        return [{"name": "/", "path": "/", "kind": "root"}]
    mask = 0
    try:
        import ctypes

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 —— 非 Windows 或 API 不可用时退回探测
        mask = 0
    out: list[dict] = []
    for index, letter in enumerate(string.ascii_uppercase):
        if mask and not (mask >> index) & 1:
            continue
        root = f"{letter}:\\"
        if not mask and not os.path.exists(root):
            continue
        out.append({"name": root, "path": root, "kind": "drive"})
    return out


def _is_hidden_or_system(entry: Path, *, at_drive_root: bool) -> bool:
    """隐藏/系统目录判定（选择器里这些只有噪音）。"""
    name = (entry.name or "").lower()
    if entry.name.startswith("$") or name in _HIDDEN_DIR_NAMES:
        return True
    if at_drive_root and name in _SYSTEM_DIR_NAMES:
        return True
    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    hidden = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0)
    system = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0)
    return bool(attrs & (hidden | system))


def _default_browse_path(home: Path, projects_root: Path, roots: list[dict]) -> Path:
    """没给路径时的起点：优先"工作盘"，避免恰好落在盘符根的主目录上。"""
    if str(home) and home.is_dir() and home.parent != home:
        return home
    if projects_root.is_dir():
        return projects_root
    for root in roots:
        candidate = Path(str(root.get("path") or ""))
        if candidate.is_dir():
            return candidate
    return home


#: 项目名允许的字符（中英数、点、下划线、连字符、空格）；用于"新建项目"
_NAME_OK_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff ._-]{0,59}$")

#: 新建项目可选模板
TEMPLATES = ("empty", "python", "node")


def detect_language(path: str | Path) -> str:
    """按扩展名/文件名判定语言（判定不出返回 plaintext）。"""
    p = Path(path)
    name = p.name.lower()
    if name in ("dockerfile", "containerfile"):
        return "dockerfile"
    if name in ("makefile", "gnumakefile"):
        return "makefile"
    if name in (".gitignore", ".dockerignore", ".editorconfig"):
        return "ini"
    return _LANG_BY_EXT.get(p.suffix.lower(), "plaintext")


def is_secret_file(path: str | Path) -> bool:
    """文件名是否疑似含密钥（P1：不索引、不外传）。

    例外：`.env.example` / `*.sample` / `*.template` / `*.dist` 这类**模板文件**
    是给人抄的、本身不含真实凭据，而且正是最该让 AI 读的说明文件——误判成密钥
    会把它们从上下文里剔掉，属于"安全做过头"。
    """
    name = Path(path).name
    lowered = name.lower()
    if lowered.endswith((".example", ".sample", ".template", ".dist", ".example.local")):
        return False
    return bool(_SECRET_RE.match(name))


def clean_project_name(raw: str) -> str:
    """校验/清洗项目名（同时作为新建目录名）。"""
    if not isinstance(raw, str):
        raise P.ProjectPathError("项目名称必须是字符串")
    name = raw.strip()
    if not name:
        raise P.ProjectPathError("项目名称不能为空")
    if not _NAME_OK_RE.match(name):
        raise P.ProjectPathError("项目名只能包含中英文、数字、空格与 . _ -，且不超过 60 字符")
    if name in (".", ".."):
        raise P.ProjectPathError("项目名不合法")
    # Windows 会忽略尾部点，并把 CON/NUL 等解释为设备，而不是普通目录。
    if name.endswith(".") or name.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise P.ProjectPathError("项目名不能使用系统保留名称或以点结尾")
    return name


@dataclass(frozen=True)
class _CreatedDirectory:
    """只有 mkdir 成功的调用拥有清理权；目录被替换后即失去该权利。"""

    base: Path
    root: Path
    identity: tuple[int, int]

    def unchanged(self) -> bool:
        try:
            self.root.relative_to(self.base)
            if self.root == self.base or self.root.resolve() != self.root:
                return False
            if self.root.is_symlink() or getattr(self.root, "is_junction", lambda: False)():
                return False
            current = self.root.lstat()
            return stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == self.identity
        except (OSError, ValueError):
            return False

    def cleanup(self) -> bool:
        if not self.root.exists():
            return True
        # 递归删除前检查绝对路径边界及本次创建的目录身份，拒绝删除外部替换的目录。
        return self.unchanged() and _rmtree_force(self.root)


@dataclass(frozen=True)
class _CreateTarget:
    root: Path
    key: str
    created: _CreatedDirectory | None = None


def _sha1_of(path: Path, limit: int = MAX_HASH_BYTES) -> str:
    """文件指纹（文本口径）：与编辑器乐观锁、修改台账用同一套哈希。"""
    from code_agent.codeintel.hashing import sha1_file_text

    return sha1_file_text(path)


def _count_lines(path: Path, limit: int = MAX_HASH_BYTES) -> int:
    lines = 0
    read = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            lines += chunk.count(b"\n")
            read += len(chunk)
            if read >= limit:
                return lines
    return lines


class ProjectService:
    """项目登记 + 浏览；Git 快照只用于工作区元数据，不参与操作校验。"""

    def __init__(self, repo: ProjectRepo, *, roots: Path | None = None) -> None:
        self.repo = repo
        self._roots = roots
        self._git_views: dict[str, tuple[float, dict]] = {}
        self._git_available_cache: tuple[float, bool] | None = None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def roots(self) -> Path:
        return self._roots or projects_root()

    def get_owned(self, user_id: str, project_id: str) -> dict:
        """取项目并校验归属；不存在或不属于该用户一律 KeyError（不泄漏存在性）。"""
        row = self.repo.get(project_id)
        if row is None or row["user_id"] != user_id:
            raise KeyError("项目不存在或无权访问")
        return row

    def root_of(self, project: dict) -> Path:
        """项目根（每次重新校验目录仍在：用户在外部删了目录要立刻看得见）。"""
        root = Path(project["root_path"])
        if not root.is_dir():
            raise P.ProjectPathError(f"项目目录不可用：{project['root_path']}")
        return root

    async def _probe(self, root: str | Path) -> RepoInfo:
        try:
            return await probe_repo(root, with_status=True)
        except Exception as exc:  # 探测失败不能影响项目可用性
            return RepoInfo(git_available=await asyncio.to_thread(git_available),
                            error=f"{type(exc).__name__}: {exc}")

    async def view(
        self, project: dict, *, with_git: bool = False, repo_info: RepoInfo | None = None
    ) -> dict:
        """元数据立即返回；打开/创建或显式 with_git 才等待完整 Git 探测。

        工作区列表与详情不应为每个仓库启动五个 Git 子进程。完整 Git 面板
        仍自行读取最新状态；这里用 cached/stale 区分登记信息与实时探测。
        """
        info = repo_info
        root = project["root_path"]
        if info is None and with_git:
            info = await self._probe(root)
        now = time.monotonic()
        if info is not None:
            git = info.as_dict()
            git["changed"] = list(git["changed"])
            self._git_views[root] = (now, git)
            if len(self._git_views) > 128:
                self._git_views.pop(next(iter(self._git_views)))
            self._git_available_cache = (now, info.git_available)
            git = {**git, "changed": list(git["changed"]), "cached": False, "stale": False}
        elif root in self._git_views:
            sampled_at, cached = self._git_views[root]
            git = {**cached, "changed": list(cached["changed"]), "cached": True,
                   "stale": now - sampled_at >= 30.0}
        else:
            available = self._git_available_cache
            if available is None or now - available[0] >= 30.0:
                available = (now, await asyncio.to_thread(git_available))
                self._git_available_cache = available
            # 未读工作树时，dirty=None 表示未知，不能伪装成「没有改动」。
            git = {**RepoInfo(git_available=available[1], is_repo=project.get("git_mode") == "git",
                             branch=project.get("base_branch") or "").as_dict(),
                   "dirty": None, "cached": True, "stale": True}
        try:
            scan_summary = json.loads(project.get("scan_summary") or "{}")
        except json.JSONDecodeError:
            scan_summary = {}
        available = await asyncio.to_thread(Path(project["root_path"]).is_dir)
        return {
            "id": project["id"],
            "name": project["name"],
            "root_path": project["root_path"],
            "origin": project["origin"],
            "template": project.get("template") or "",
            "session_id": project.get("session_id") or "",
            "base_branch": project.get("base_branch") or "",
            "allow_direct_write": bool(project.get("allow_direct_write")),
            "git_mode": project.get("git_mode") or "",
            "last_scan_at": project.get("last_scan_at") or "",
            "scan_summary": scan_summary,
            "created_at": project.get("created_at") or "",
            "updated_at": project.get("updated_at") or "",
            "available": available,
            "git": git,
        }

    async def list_projects(self, user_id: str, *, with_git: bool = False) -> list[dict]:
        rows = await asyncio.to_thread(self.repo.list_for_user, user_id)
        slots = asyncio.Semaphore(4)

        async def render(row: dict) -> dict:
            async with slots:
                return await self.view(row, with_git=with_git)

        # 默认只装配轻量列表；显式完整探测仍保留顺序、最多同时四个项目。
        return list(await asyncio.gather(*(render(row) for row in rows)))

    # ------------------------------------------------------------------
    # 打开 / 新建 / 删除
    # ------------------------------------------------------------------
    async def open_project(self, user_id: str, raw_path: str | Path, name: str = "") -> dict:
        """打开既有目录：登记成项目（同一目录重复打开返回原项目）。"""
        if not isinstance(raw_path, (str, Path)) or not isinstance(name, str):
            raise P.ProjectPathError("路径和项目名必须是字符串")
        if any(ord(char) < 32 for char in str(raw_path) + name) or len(name) > 80:
            raise P.ProjectPathError("路径或项目名不合法")

        def location() -> tuple[Path, str]:
            root = P.resolve_project_root(raw_path)
            return root, P.root_key(root)

        root, key = await asyncio.to_thread(location)
        info = await self._probe(root)

        def register() -> dict:
            # Git 探测期间目录可能被删除或改成链接；提交前再次执行相同安全规则。
            if P.resolve_project_root(root) != root:
                raise P.ProjectPathError("项目目录位置已发生变化，请重新选择目录")
            return self.repo.register(
                user_id=user_id, name=name.strip() or root.name or "project",
                root_path=str(root), root_key=key, origin="open", template="",
                git_mode="git" if info.is_repo else "none",
                base_branch=info.branch if info.is_repo else "",
            )

        row = await asyncio.to_thread(register)
        return await self.view(row, repo_info=info)

    def _prepare_create(
        self, user_id: str, clean: str, template: str, location: str | Path = ""
    ) -> _CreateTarget:
        """磁盘和 SQLite 同步操作在线程中完成；不接管任何已有目录。"""
        default_base = self.roots().resolve()
        text = str(location).strip().strip('"').strip("'")
        default_location = not text
        if text:
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                raise P.ProjectPathError("项目位置必须是已有目录的绝对路径")
            base = candidate.resolve()
            if base == default_base:
                # 默认平台工程目录位于 agent_home 内，显式填写同一位置仍保留原创建例外。
                default_location = True
            else:
                base = P.resolve_project_root(candidate, for_creation_parent=True)
        else:
            base = default_base
        root = P.safe_child(base, clean)
        if root == base:
            raise P.ProjectPathError("项目目录必须位于所选位置下的独立子目录")
        if not default_location and root.is_dir():
            P.resolve_project_root(root)
        key = P.root_key(root)
        if self.repo.get_by_root(user_id, key) is not None:
            if not root.is_dir():
                raise P.ProjectPathError(f"已登记项目的目录不可用：{root}；请先移除旧登记")
            return _CreateTarget(root, key)
        # 自选父目录必须事先存在；不在验证后的竞争窗口中悄悄重建用户的父目录。
        if default_location:
            base.mkdir(parents=True, exist_ok=True)
        try:
            root.mkdir(exist_ok=False)
        except FileExistsError:
            raise P.ProjectPathError(f"目录已存在：{root}；请打开已有目录或使用其他项目名") from None
        stamp = root.lstat()
        created = _CreatedDirectory(base, root, (stamp.st_dev, stamp.st_ino))
        try:
            if not default_location:
                P.resolve_project_root(root)
            if not created.unchanged():
                raise P.ProjectPathError("新建项目目录已发生变化，请重新选择位置")
            self._scaffold(root, clean, template)
        except BaseException as exc:
            if not created.cleanup():
                raise P.ProjectPathError(f"创建未完成，目录已保留：{root}；请检查目录状态") from exc
            raise
        return _CreateTarget(root, key, created)

    async def create_project(
        self,
        user_id: str,
        name: str,
        template: str = "empty",
        *,
        git_init: bool = True,
        location: str | Path = "",
    ) -> dict:
        """在已有父目录下新建工程；location 为空时使用 projects_root()。"""
        clean = clean_project_name(name)
        if not isinstance(template, str) or not isinstance(git_init, bool):
            raise P.ProjectPathError("模板必须是字符串，git_init 必须是布尔值")
        if not isinstance(location, (str, Path)):
            raise P.ProjectPathError("项目位置必须是字符串")
        if len(str(location)) > 4096 or any(ord(char) < 32 for char in str(location)):
            raise P.ProjectPathError("项目位置不合法")
        tmpl = template.strip().lower() or "empty"
        if tmpl not in TEMPLATES:
            raise P.ProjectPathError(f"未知模板：{template}（可选：{', '.join(TEMPLATES)}）")

        preparation = asyncio.create_task(asyncio.to_thread(
            self._prepare_create, user_id, clean, tmpl, location
        ))
        try:
            target = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            # to_thread 取消不会停止线程；等骨架写入结束后再清理，避免后台继续写已删目录。
            target = await preparation
            if target.created:
                await asyncio.to_thread(target.created.cleanup)
            raise

        registered = False
        try:
            root = target.root
            if target.created and not await asyncio.to_thread(target.created.unchanged):
                raise P.ProjectPathError("新建项目目录已发生变化，请重新选择位置")
            if target.created and git_init and await asyncio.to_thread(git_available):
                info = await self._git_init(root)
            else:
                info = await self._probe(root)

            def register() -> dict:
                if not root.is_dir() or root.resolve() != root or (
                    target.created is not None and not target.created.unchanged()
                ):
                    raise P.ProjectPathError("项目目录在创建期间发生变化，请重新选择目录")
                return self.repo.register(
                    user_id=user_id, name=clean, root_path=str(root), root_key=target.key,
                    origin="create", template=tmpl, git_mode="git" if info.is_repo else "none",
                    base_branch=info.branch if info.is_repo else "",
                    # 复用途中登记被移除时，不给未由本次 mkdir 的目录重新授予创建/删除权。
                    existing_only=target.created is None,
                )

            registration = asyncio.create_task(asyncio.to_thread(register))
            try:
                row = await asyncio.shield(registration)
            except asyncio.CancelledError:
                # 登记已提交则保留目录，即使客户端在提交期间断开。
                await registration
                registered = True
                raise
            registered = True
            return await self.view(row, repo_info=info)
        except BaseException as exc:
            if target.created and not registered:
                if not await asyncio.to_thread(target.created.cleanup):
                    raise P.ProjectPathError(
                        f"创建未完成，目录已保留：{target.root}；请检查目录状态"
                    ) from exc
            raise

    async def _git_init(self, root: Path) -> RepoInfo:
        """git init + 初始提交（失败不致命：降级为"无版本控制"模式）。"""
        from code_agent.codeintel.vcs import run_git

        try:
            await run_git(root, ["init", "-q"])
            await run_git(root, ["add", "-A"])
            await run_git(
                root,
                ["-c", "user.name=Code-agent", "-c", "user.email=code-agent@local",
                 "commit", "-q", "-m", "chore: 初始化项目骨架（Code-agent 代码模式）"],
            )
        except Exception:  # noqa: BLE001 —— 初始化失败只影响"版本控制"能力，不影响项目可用
            pass
        return await self._probe(root)

    def _scaffold(self, root: Path, name: str, template: str) -> None:
        """仅在本次新建且通过位置边界校验的目录中写最小骨架。"""
        readme = root / "README.md"
        if not readme.exists():
            readme.write_text(f"# {name}\n\n由 Code-agent 代码模式创建。\n", encoding="utf-8")
        _gitignore = root / ".gitignore"
        if not _gitignore.exists():
            _gitignore.write_text(
                "__pycache__/\n*.py[cod]\n.venv/\nnode_modules/\ndist/\nbuild/\n.env\n",
                encoding="utf-8",
            )
        if template == "python":
            (root / "src").mkdir(exist_ok=True)
            (root / "tests").mkdir(exist_ok=True)
            if not (root / "pyproject.toml").exists():
                (root / "pyproject.toml").write_text(
                    f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
                    'requires-python = ">=3.11"\ndependencies = []\n\n'
                    "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\naddopts = \"-q\"\n",
                    encoding="utf-8",
                )
            main = root / "src" / "main.py"
            if not main.exists():
                main.write_text(
                    '"""入口：先跑通一个最小闭环，再往上加功能。"""\n\n\n'
                    'def main() -> str:\n    return "hello"\n\n\n'
                    'if __name__ == "__main__":\n    print(main())\n',
                    encoding="utf-8",
                )
            sample = root / "tests" / "test_main.py"
            if not sample.exists():
                sample.write_text(
                    "import sys\nfrom pathlib import Path\n\n"
                    "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / \"src\"))\n\n"
                    "from main import main  # noqa: E402\n\n\n"
                    "def test_main():\n    assert main() == \"hello\"\n",
                    encoding="utf-8",
                )
        elif template == "node":
            (root / "src").mkdir(exist_ok=True)
            if not (root / "package.json").exists():
                (root / "package.json").write_text(
                    json.dumps(
                        {
                            "name": re.sub(r"[^a-z0-9-]+", "-", name.lower()) or "app",
                            "version": "0.1.0",
                            "private": True,
                            "type": "module",
                            "main": "src/index.js",
                            "scripts": {"start": "node src/index.js"},
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            entry = root / "src" / "index.js"
            if not entry.exists():
                entry.write_text(
                    "export function main() {\n  return 'hello';\n}\n\n"
                    "if (import.meta.url === `file://${process.argv[1]}`) {\n"
                    "  console.log(main());\n}\n",
                    encoding="utf-8",
                )

    def delete_project(self, user_id: str, project_id: str, *, delete_files: bool = False) -> dict:
        """注销项目。delete_files 仅对平台自建且位于 projects_root 之下的项目生效。"""
        row = self.get_owned(user_id, project_id)
        removed_dir = False
        if delete_files:
            if row["origin"] != "create":
                raise P.ProjectPathError("只允许删除由本平台创建的项目目录；用户自己的仓库只能「移除」")
            root = Path(row["root_path"]).resolve()
            base = self.roots().resolve()
            try:
                root.relative_to(base)
            except ValueError:
                raise P.ProjectPathError("项目目录不在平台工程目录下，拒绝删除文件") from None
            if root != base and root.is_dir():
                removed_dir = _rmtree_force(root)
                if not removed_dir:
                    # 数据库记录先留着：报出来用户才有机会"关掉占用它的进程再删一次"，
                    # 比登记记录没了、目录还赖在磁盘上更好收拾。
                    raise P.ProjectPathError(f"项目目录没能完全删除（可能被占用）：{root}")
        self.repo.delete(project_id)
        return {"ok": True, "removed_dir": removed_dir}

    # ------------------------------------------------------------------
    # 浏览：目录树 / 文件文本 / 目录选择
    # ------------------------------------------------------------------
    def tree(self, project: dict, rel: str = "") -> dict:
        """列出某一层（懒加载）。返回 dirs/files，前端只展开当前层。"""
        root = self.root_of(project)
        rel_norm = P.normalize_rel(rel)
        target = P.safe_child(root, rel_norm)
        if not target.exists():
            raise P.ProjectPathError(f"目录不存在：{rel_norm or '.'}")
        if not target.is_dir():
            raise P.ProjectPathError(f"不是目录：{rel_norm}")

        dirs: list[dict] = []
        files: list[dict] = []
        truncated = False
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            raise P.ProjectPathError(f"目录不可读：{rel_norm}（{exc}）") from exc

        for entry in entries:
            if len(dirs) + len(files) >= MAX_TREE_ENTRIES:
                truncated = True
                break
            name = entry.name
            if entry.is_dir():
                # 隐藏目录（.idea/.vscode/.cache…）不进树；.git 等已在忽略名单里
                if name.startswith(".") or P.is_ignored_dir(name):
                    continue
                dirs.append({"name": name, "path": f"{rel_norm}/{name}".strip("/"), "kind": "dir"})
            else:
                # 隐藏文件保留：.env / .eslintrc / .gitignore 在工程里是重要信息，
                # 只是给疑似密钥的文件打上 secret 标记（P1 不索引、不进模型上下文）
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                files.append(
                    {
                        "name": name,
                        "path": f"{rel_norm}/{name}".strip("/"),
                        "kind": "file",
                        "size": stat.st_size,
                        "lang": detect_language(name),
                        "secret": is_secret_file(name),
                    }
                )
        return {
            "path": rel_norm,
            "dirs": dirs,
            "files": files,
            "truncated": truncated,
        }

    def read_file(self, project: dict, rel: str, start: int | None = None, end: int | None = None) -> dict:
        """读取文件文本；带 start/end 时按行切片（1-based，含端点）。

        不带区间的完整读取有 512KB 上限：一个 5MB 的日志文件既不该糊给前端，
        也不该进模型上下文（要看得给区间）。
        """
        root = self.root_of(project)
        rel_norm = P.normalize_rel(rel)
        if not rel_norm:
            raise P.ProjectPathError("缺少 path 参数")
        target = P.safe_child(root, rel_norm)
        if not target.is_file():
            raise P.ProjectPathError(f"文件不存在：{rel_norm}")

        size = target.stat().st_size
        with target.open("rb") as stream:
            head = stream.read(8192)
        if P.is_probably_binary(head):
            raise P.ProjectPathError(f"二进制文件不支持在编辑器里打开：{rel_norm}")

        wanted_range = start is not None or end is not None
        if not wanted_range and size > MAX_FILE_BYTES:
            raise P.ProjectPathError(
                f"文件过大（{size // 1024}KB > {MAX_FILE_BYTES // 1024}KB），请传 start/end 只读需要的行区间"
            )

        total_lines = _count_lines(target) if size <= MAX_HASH_BYTES else None
        if wanted_range:
            first = max(1, int(start or 1))
            last = int(end) if end is not None else (first + MAX_RANGE_LINES - 1)
            if last < first:
                raise P.ProjectPathError("end 不能小于 start")
            last = min(last, first + MAX_RANGE_LINES - 1)
            if total_lines is not None:
                last = min(last, max(total_lines, 1))
            decoded, line_count = read_text_range(target, first, last, inspection_limit=MAX_HASH_BYTES)
            return {
                "path": rel_norm,
                "lang": detect_language(target),
                "uri": file_uri(target),
                "text": decoded.text,
                "start_line": first,
                "end_line": first + max(line_count - 1, 0),
                "total_lines": total_lines,
                "truncated": total_lines is not None and last < total_lines,
                "size": size,
                "sha1": _sha1_of(target) if size <= MAX_HASH_BYTES else "",
                "secret": is_secret_file(target),
                "editable": size <= MAX_EDIT_BYTES and decoded.encoding == "utf-8",
                "range": True,
                **decoded.metadata(),
            }

        decoded = decode_text(target.read_bytes(), is_log=target.suffix.lower() == ".log")
        text = decoded.text
        return {
            "path": rel_norm,
            "lang": detect_language(target),
            # LSP 需要 file:// URI：由服务端生成并下发，前端原样回传（两边必须一致）
            "uri": file_uri(target),
            "text": text,
            "start_line": 1,
            "end_line": text.count("\n") + 1,
            "total_lines": total_lines,
            "truncated": False,
            "size": size,
            # The baseline belongs to the text returned to the editor. A
            # second disk read could hash an external writer's newer version.
            "sha1": sha1_text(text),
            "secret": is_secret_file(target),
            "editable": size <= MAX_EDIT_BYTES and decoded.encoding == "utf-8",
            "range": False,
            **decoded.metadata(),
        }

    def browse(self, raw: str = "", *, user_id: str = "", show_hidden: bool = False) -> dict:
        """目录选择器：只列目录，供「打开已有工作区」用。

        仅登录用户可用；`CODE_AGENT_CODE_BROWSE=0` 可整体关闭（强化部署时关掉，
        让用户直接输入路径）。

        三个"能选中自己仓库"的必要条件（早期版本只有第一条，用户只能看到一个 C:\\）：
        1. **盘符/快捷位置**：Windows 上列出所有逻辑盘符，外加主目录、平台工程目录与
           最近打开过的项目——换盘不该靠手敲路径；
        2. **默认起点合理**：没给路径时优先落在"平台工程目录所在的盘"（用户的工作盘），
           而不是恰好等于盘符根的 `Path.home()`；
        3. **隐藏与系统目录不凑热闹**：`$Recycle.Bin`、`Windows`、`ProgramData` 这些
           在选择器里只有噪音（`show_hidden=1` 可以显式打开，并如实报告过滤了多少个）。
        """
        import os

        if os.environ.get("CODE_AGENT_CODE_BROWSE", "1") == "0":
            raise P.ProjectPathError("目录浏览已关闭，请直接输入路径")

        roots = _drives()
        home = Path.home()
        text = str(raw or "").strip().strip('"')
        if not text:
            current = _default_browse_path(home, self.roots(), roots)
        else:
            current = Path(text).expanduser()
            if not current.is_dir():
                raise P.ProjectPathError(f"目录不存在：{text}")
            current = current.resolve()
        parent = current.parent
        if parent == current:
            parent = None

        at_drive_root = parent is None or parent == current
        dirs: list[dict] = []
        hidden_count = 0
        try:
            for entry in sorted(current.iterdir(), key=lambda p: p.name.lower()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith(".") or P.is_ignored_dir(entry.name):
                    continue
                if not show_hidden and _is_hidden_or_system(entry, at_drive_root=at_drive_root):
                    hidden_count += 1
                    continue
                dirs.append(_dir_entry(entry))
                if len(dirs) >= 500:
                    break
        except OSError as exc:
            raise P.ProjectPathError(f"目录不可读：{exc}") from exc

        has_project_markers = any(
            (current / marker).exists()
            for marker in (".git", "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "pom.xml")
        )
        return {
            "path": str(current),
            "parent": str(parent) if parent else "",
            "dirs": dirs,
            "roots": roots,
            "places": self._places(user_id, home),
            "breadcrumbs": _breadcrumbs(current),
            "quick": self._quick_locations(user_id, home, current),
            "hidden_count": hidden_count,
            "show_hidden": bool(show_hidden),
            "is_project": has_project_markers,
            "suggested_name": current.name,
        }

    def _places(self, user_id: str, home: Path) -> list[dict]:
        """左侧「快速访问」：标准文件夹 + 平台工程目录 + 最近项目（存在才给）。"""
        out: list[dict] = []
        seen: set[str] = set()

        def _add(name: str, path: Path | str, kind: str) -> None:
            text = str(path or "").strip()
            if not text:
                return
            key = text.rstrip("\\/").lower()
            if key in seen:
                return
            candidate = Path(text)
            if not candidate.is_dir():
                return
            seen.add(key)
            out.append({"name": name, "path": text, "kind": kind})

        _add(home.name or "个人文件夹", home, "home")
        for name, folder in (
            ("桌面", "Desktop"), ("下载", "Downloads"), ("文档", "Documents"),
            ("图片", "Pictures"), ("音乐", "Music"), ("视频", "Videos"),
        ):
            _add(name, home / folder, "folder")
            _add(name, home / "OneDrive" / folder, "folder")   # OneDrive 重定向过的机器
        _add("平台工程目录", self.roots(), "projects")
        if user_id:
            try:
                for row in self.repo.list_for_user(user_id)[:5]:
                    root = Path(row["root_path"])
                    _add(root.name or str(root), root, "recent")
            except Exception:  # noqa: BLE001 —— 快捷项拉不到不该让浏览失败
                pass
        return out[:20]

    def _quick_locations(self, user_id: str, home: Path, current: Path) -> list[dict]:
        """常用位置：主目录 + 平台工程目录 + 最近打开的项目（去重、最多 8 个）。"""
        out: list[dict] = []
        seen: set[str] = set()

        def _add(name: str, path: Path | str, kind: str) -> None:
            text = str(path or "").strip()
            if not text:
                return
            key = text.rstrip("\\/").lower()
            if key in seen:
                return
            seen.add(key)
            out.append({"name": name, "path": text, "kind": kind})

        _add("主目录", home, "home")
        _add("平台工程目录", self.roots(), "projects")
        if user_id:
            try:
                for row in self.repo.list_for_user(user_id)[:5]:
                    root = Path(row["root_path"])
                    _add(root.name or str(root), root, "recent")
                    _add("↰ 上级", root.parent, "recent-parent")
            except Exception:  # noqa: BLE001 —— 快捷项拉不到不该让浏览失败
                pass
        if current and str(current) not in seen:
            _add("当前目录的上级", current.parent if current.parent != current else current, "up")
        return out[:8]
