"""目录扫描（M18 · P1）：把"文件清单 + 指纹"交给索引层。

只做三件事：走目录、按忽略规则筛、给每个文件留下 (size, mtime, lang) 指纹与
跳过原因。**不解析、不哈希**——哈希只在指纹变化时才做（增量扫描的全部意义：
一次扫描的 I/O 应当与"改动量"成正比，而不是与"仓库大小"成正比）。

跳过原因（与地图语义一致）：
- secret    疑似密钥文件：不进索引、不进模型上下文（文件树里仍然可见）
- too_large 超过单文件解析上限：只留文件节点
- binary    二进制：只留文件节点
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from code_agent.codeintel import paths as P
from code_agent.codeintel.projects import detect_language, is_secret_file
from code_agent.codeintel.parsers import FILE_ONLY_LANGS, PARSERS

#: 单次扫描的文件数上限（超过即截断，避免一个 monorepo 把内存/时间吃光）
MAX_FILES = 20000
#: 单文件解析上限：超过只留文件节点，不进内容解析
MAX_PARSE_BYTES = 512 * 1024
#: 需要嗅探二进制时才读的头部大小
SNIFF_BYTES = 8192
#: Git inventory discovery is bounded and never invokes a shell or network.
GIT_IGNORE_TIMEOUT = 10.0
#: 已知文本语言（无需嗅探，省掉每个文件一次读盘）。
#: **必须剔除 plaintext**：它是"扩展名认不出来"的兜底值，正好是最需要嗅探的一批
#: 文件（.png/.zip/.so…）。把兜底当已知，二进制探测就等于没写。
_TEXT_LANGS = (frozenset(PARSERS) | FILE_ONLY_LANGS) - {"plaintext"}


@dataclass
class FileEntry:
    rel: str
    abs: str
    size: int
    mtime: float
    lang: str
    skipped: str = ""          # "" | secret | too_large | binary

    @property
    def parseable(self) -> bool:
        return self.skipped == ""


@dataclass
class ScanResult:
    files: list[FileEntry] = field(default_factory=list)
    dirs: int = 0
    total_bytes: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    duration_ms: int = 0

    def summary(self) -> dict:
        langs: dict[str, dict] = {}
        for entry in self.files:
            if entry.skipped:
                continue
            bucket = langs.setdefault(entry.lang, {"name": entry.lang, "files": 0, "bytes": 0})
            bucket["files"] += 1
            bucket["bytes"] += entry.size
        ordered = sorted(langs.values(), key=lambda b: b["files"], reverse=True)
        return {
            "files": sum(1 for f in self.files if not f.skipped),
            "dirs": self.dirs,
            "bytes": self.total_bytes,
            "truncated": self.truncated,
            "skipped": dict(self.skipped),
            "languages": ordered,
            "duration_ms": self.duration_ms,
        }


def _sniff_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return P.is_probably_binary(fh.read(SNIFF_BYTES))
    except OSError:
        return True  # 读不了就当作不可解析，别让扫描崩在单个文件上


def _git_ignored_paths(root: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Ask Git for ignored paths once, including tracked build copies.

    Git supplies its actual nested/parent ignore semantics, worktree metadata,
    escaped names and negations. ``--directory`` collapses untracked ignored
    trees before Python descends into them. A second *batch* query folds tracked
    ignored files into ignored ancestors where Git confirms the whole directory
    is excluded. No subprocess is started per file or visited directory.

    Paths are relative to ``root`` even when it is a repository subdirectory;
    module version checks therefore inherit the same rules as repository scans.
    Outside Git, or when Git is unavailable, the existing static policy applies.
    """
    empty: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
    if not any((parent / ".git").exists() for parent in (root, *root.parents)):
        return empty
    kwargs = {"capture_output": True, "timeout": GIT_IGNORE_TIMEOUT,
              "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
    try:
        inventory = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), "ls-files", "--cached", "--others", "--ignored",
             "--exclude-standard", "--directory", "-z", "--", "."], **kwargs,
        )
        if inventory.returncode != 0:
            return empty
        paths = {os.fsdecode(value) for value in inventory.stdout.split(b"\0") if value}
        directories = {path.rstrip("/") for path in paths if path.endswith("/")}
        files = paths - {path for path in paths if path.endswith("/")}
        ancestors = {parent.as_posix() for path in files for parent in PurePosixPath(path).parents
                     if parent.as_posix() != "."}
        if ancestors:
            payload = b"\0".join(os.fsencode(path) for path in sorted(ancestors)) + b"\0"
            matched = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--no-index", "--stdin", "-z"],
                input=payload, **kwargs,
            )
            if matched.returncode in {0, 1}:
                directories.update(os.fsdecode(value).rstrip("/") for value in matched.stdout.split(b"\0") if value)
        return frozenset(files), frozenset(directories)
    except (OSError, subprocess.TimeoutExpired):
        return empty


def walk_project(root: Path, *, max_files: int = MAX_FILES, max_bytes: int = MAX_PARSE_BYTES) -> ScanResult:
    """遍历项目，返回文件清单（相对路径一律用 / 分隔）。"""
    import time

    started = time.monotonic()
    result = ScanResult()
    root = Path(root)
    ignored_files, ignored_dirs = _git_ignored_paths(root)

    def _walk(directory: Path, rel_dir: str) -> None:
        if result.truncated:
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        result.dirs += 1
        for entry in entries:
            if result.truncated:
                return
            name = entry.name
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if name.startswith(".") or P.is_ignored_dir(name) or child_rel in ignored_dirs:
                    continue
                _walk(Path(entry.path), child_rel)
                continue
            if child_rel in ignored_files:
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            lang = detect_language(name)
            skipped = ""
            if is_secret_file(name):
                skipped = "secret"
            elif stat.st_size > max_bytes:
                skipped = "too_large"
            elif lang not in _TEXT_LANGS and _sniff_binary(Path(entry.path)):
                skipped = "binary"
            if skipped:
                result.skipped[skipped] = result.skipped.get(skipped, 0) + 1
            elif len(result.files) >= max_files:
                result.truncated = True
                return
            result.files.append(
                FileEntry(
                    rel=child_rel,
                    abs=str(Path(entry.path)),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    lang=lang,
                    skipped=skipped,
                )
            )
            result.total_bytes += stat.st_size

    _walk(root, "")
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result
