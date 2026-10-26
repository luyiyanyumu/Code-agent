"""代码情报服务（M18 · P1）：扫描编排 + 地图装配 + 增量 delta + 保真度校验。

三条"必须做对"的工程约束：

1. **单飞（single-flight）**：同一个项目的扫描同时只能跑一个。并发扫描会让
   两个线程对同一棵索引交错写入（version/changes 序列错乱），而扫描本身
   并不需要并行——用户点两次「扫描」不该产生两次全量工作。
2. **增量按指纹**：`(size, mtime)` 未变 → 跳过（连哈希都不算）；
   指纹变了但 sha1 相同（如 `git checkout` 回同内容）→ 只更新 stat，不产生变更；
   sha1 变了 → 重解析该文件。扫描 I/O 与"改动量"成正比，而不是与仓库大小成正比。
3. **扫描不阻塞事件循环**：文件 IO 与解析都放进 `asyncio.to_thread`。

保真度（fidelity）：`verify()` 会**重新解析全部文件但只比较、不写入**，返回
"索引与磁盘一致的比例"。它是"地图完全映射代码"这一承诺的可测形式——
不是靠感觉说同步了，而是能给出一个数。刻意不写入，否则它就变成"自己证明自己"。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import stat
import tempfile
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from typing import Any

from code_agent.codeintel import graph
from code_agent.codeintel.decisions import DecisionStore
from code_agent.codeintel.hashing import sha1_text
from code_agent.codeintel.patches import PatchStore
from code_agent.codeintel.paths import safe_child, root_key
from code_agent.codeintel.parsers import parse_file, is_strategy_current
from code_agent.codeintel.projects import MAX_EDIT_BYTES, MAX_FILE_BYTES, detect_language
from code_agent.codeintel.scanner import FileEntry, walk_project
from code_agent.codeintel.store import (
    META_ROOT_KEY,
    META_TREE,
    MAX_SYMBOLS_PER_FILE,
    SymbolIndex,
    file_id,
    now_iso,
)
from code_agent.config import code_dir


def _now_iso() -> str:
    """后台任务进度用的时间戳（UTC，秒级）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: /map 一次返回的符号上限（超过则截断并标记，前端提示"用搜索/筛选缩小范围"）
MAX_MAP_SYMBOLS = 4000
#: /map 一次返回的边上限
MAX_MAP_EDGES = 6000
#: delta 一次返回的变更上限
MAX_DELTA_CHANGES = 800
BUSINESS_MODULE_CACHE_TTL = 2.0
BRIEF_RETRY_COOLDOWN = 60.0
BRIEF_MODEL_TIMEOUT = 45.0


def _sha1_file(path: Path, *, limit: int = 8 * 1024 * 1024) -> str:
    """索引的增量指纹：**字节口径**（要能发现换行符/编码这类真实改动）。"""
    from code_agent.codeintel.hashing import sha1_file_bytes

    return sha1_file_bytes(path)


def _count_loc(path: Path, *, limit: int = 8 * 1024 * 1024) -> int:
    lines = 0
    read = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk or read >= limit:
                break
            lines += chunk.count(b"\n")
            read += len(chunk)
    return lines + 1


class FileSaveConflict(ValueError):
    def __init__(self, current_sha1: str, expected_sha1: str, message: str = "文件已被其他改动更新（AI 或外部工具）：请先对比再保存"):
        super().__init__(message)
        self.current_sha1, self.expected_sha1 = current_sha1, expected_sha1

    @property
    def detail(self) -> dict:
        return {"message": str(self), "current_sha1": self.current_sha1, "expected_sha1": self.expected_sha1}


def _save_snapshot(target: Path) -> tuple[bytes, tuple, int]:
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在：{target.name}")
    with target.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_EDIT_BYTES:
            raise ValueError("编辑器只支持不超过 2MB 的文本文件")
        data = stream.read(MAX_EDIT_BYTES + 1)
        after = os.fstat(stream.fileno())
    stamp = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if stamp(before) != stamp(after):
        raise FileSaveConflict("", "", "读取期间文件发生变化，请刷新后重试")
    if len(data) > MAX_EDIT_BYTES or b"\0" in data:
        raise ValueError("编辑器只支持不超过 2MB 的文本文件")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("文件不是有效 UTF-8 编码，当前仅支持只读查看；请先使用外部编辑器确认并转换编码后再保存") from exc
    return data, stamp(after), stat.S_IMODE(after.st_mode)


def _editor_text(data: bytes | str) -> str:
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    return text.replace("\r\n", "\n").replace("\r", "\n")


class CodeIntelService:
    """索引/地图的所有权所有者。线程安全的最小实现：按项目加锁 + 单飞标记。"""

    def __init__(self, projects: Any) -> None:
        self.projects = projects
        self._indexes: dict[str, SymbolIndex] = {}
        self._patch_stores: dict[str, PatchStore] = {}
        self._decision_stores: dict[str, DecisionStore] = {}
        #: 功能导航的证据表（符号→测试文件）按 change_seq 缓存：算一次要读全部测试文件
        self._protection_cache: dict[str, tuple[int, dict, dict]] = {}
        #: 功能导航第一屏按索引版本缓存：为了"箭头不许骗人"要读上百个文件解析调用，
        #: 每切一次页签重算一遍不值（索引一变 change_seq 就变，缓存自动失效）
        self._nav_cache: dict[str, tuple[tuple, dict]] = {}
        #: 摘要在构建前查缓存；每棵索引只保留最新版本，不随对话轮次增长。
        self._session_context_cache: dict[str, tuple[SymbolIndex, tuple[int, int], tuple[str, str], str]] = {}
        self._session_context_guard = threading.Lock()
        #: 功能描述（模型补的"它是干什么的"）按仓库根路径定位，进程内复用
        self._brief_stores: dict[str, Any] = {}
        self._business_parse_cache: dict[tuple[str, str, str], list[dict]] = {}
        self._business_module_cache: dict[tuple[str, str, str], tuple[float, int, int, dict]] = {}
        #: 补描述的后台任务状态（项目 id → 进度）：界面按它显示"正在补 40/123"
        self._fill_jobs: dict[str, dict] = {}
        self._brief_loop: asyncio.AbstractEventLoop | None = None
        self._brief_futures: dict[str, Future] = {}
        self._brief_checked: dict[str, tuple] = {}
        self._brief_revision: dict[str, tuple[tuple, int]] = {}
        self._brief_epoch = uuid.uuid4().hex
        self._brief_generations: dict[str, int] = {}
        self._active_brief_writes: dict[str, int] = {}
        #: 检索 chunk 库（项目 id → RetrievalStore 或 False 表示不可用）
        self._chunk_stores: dict[str, Any] = {}
        #: "怎么拿模型"由 API 层注入（codeintel 不依赖 agent/session 那一层）
        self.brief_provider_factory: Any = None
        self._guard = threading.RLock()
        self._scanning: set[str] = set()
        self._scan_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="code-index")
        self._scan_futures: dict[str, Future] = {}
        self._refresh_jobs: dict[str, dict] = {}
        self._scan_cache: dict[str, dict] = {}
        self._file_locks: dict[str, threading.Lock] = {}
        self._save_condition = threading.Condition(self._guard)
        self._active_saves: dict[str, int] = {}
        self._close_guard = threading.Lock()
        self._closing_projects: set[str] = set()
        self._closing_keys: set[str] = set()
        self._closing_all = False
        self._closed = False
        #: 最近被访问的项目 → 时间戳。watcher 只盯这些，避免后台把全库项目一直走盘。
        self._used: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 索引句柄
    # ------------------------------------------------------------------
    def index_path(self, project_id: str, tree_id: str = "main") -> Path:
        safe_tree = "".join(ch for ch in (tree_id or "main") if ch.isalnum() or ch in "-_") or "main"
        return code_dir() / project_id / f"index-{safe_tree}.db"

    def index(self, project_id: str, tree_id: str = "main") -> SymbolIndex:
        key = f"{project_id}:{tree_id}"
        with self._guard:
            if self._closed:
                raise RuntimeError("代码情报服务已关闭")
            handle = self._indexes.get(key)
            if handle is None:
                handle = SymbolIndex(self.index_path(project_id, tree_id))
                handle.set_meta(META_TREE, tree_id or "main")
                self._indexes[key] = handle
            return handle

    def patches(self, project_id: str) -> PatchStore:
        """项目的修改台账（单例，与索引同生命周期）。"""
        key = f"{project_id}:patches"
        with self._guard:
            handle = self._patch_stores.get(key)
            if handle is None:
                handle = PatchStore(code_dir() / project_id / "patches.db")
                self._patch_stores[key] = handle
            return handle

    def close(self, project_id: str | None = None) -> None:
        """Drain owned workers before closing their SQLite connections."""
        with self._close_guard:
            with self._save_condition:
                if self._closed:
                    return
                if project_id is None:
                    self._closing_all = True
                else:
                    self._closing_projects.add(project_id)
                self._cancel_brief_jobs(project_id)
                self._save_condition.wait_for(lambda: not any(
                    count for pid, count in self._active_saves.items() if project_id is None or pid == project_id
                ) and not any(count for key, count in self._active_brief_writes.items()
                              if project_id is None or self._fill_jobs.get(key, {}).get("project_id") == project_id))
                futures = [future for key, future in self._scan_futures.items()
                           if project_id is None or key.startswith(f"{project_id}:")]
            for future in futures:
                with contextlib.suppress(Exception):
                    future.result()
            if project_id is None:
                self._scan_executor.shutdown(wait=True)
            self._close_handles(project_id)
            with self._guard:
                if project_id is None:
                    self._closed = True
                else:
                    self._closing_projects.discard(project_id)

    async def aclose(self) -> None:
        with self._guard:
            pending = list(self._brief_futures.values())
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(*(asyncio.wrap_future(future) for future in pending), return_exceptions=True)
        await asyncio.to_thread(self.close)

    def _close_handles(self, project_id: str | None = None) -> None:
        with self._guard:
            if project_id is None:
                self._closed = True
            keys = [k for k in self._indexes if project_id is None or k.startswith(f"{project_id}:")]
            for key in keys:
                handle = self._indexes.pop(key)
                try:
                    handle.close()
                except Exception:
                    pass
                self._forget_session_context(handle)
            pkeys = [
                k for k in self._patch_stores if project_id is None or k.startswith(f"{project_id}:")
            ]
            for key in pkeys:
                try:
                    self._patch_stores.pop(key).close()
                except Exception:
                    pass
            dkeys = [
                k for k in self._decision_stores
                if project_id is None or k == project_id or k.startswith(f"{project_id}:")
            ]
            for key in dkeys:
                self._decision_stores.pop(key, None)
            if project_id is None:
                self._business_parse_cache.clear()
                self._business_module_cache.clear()

    def close_tree(self, project_id: str, tree_id: str) -> None:
        """关闭某棵树的索引句柄（任务工作树用完后调用）。"""
        key = f"{project_id}:{tree_id}"
        with self._close_guard:
            with self._save_condition:
                self._closing_keys.add(key)
                self._cancel_brief_jobs(project_id, tree_id)
                self._save_condition.wait_for(lambda: not any(count for brief_key, count in self._active_brief_writes.items()
                    if self._fill_jobs.get(brief_key, {}).get("project_id") == project_id
                    and self._fill_jobs.get(brief_key, {}).get("tree") == tree_id))
                if tree_id == "main":
                    self._save_condition.wait_for(lambda: not self._active_saves.get(project_id))
                future = self._scan_futures.get(key)
            if future:
                with contextlib.suppress(Exception):
                    future.result()
            with self._guard:
                handle = self._indexes.pop(key, None)
                self._closing_keys.discard(key)
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.close()
                self._forget_session_context(handle)

    def _forget_session_context(self, index: SymbolIndex) -> None:
        with self._session_context_guard:
            for key in [key for key, entry in self._session_context_cache.items() if entry[0] is index]:
                self._session_context_cache.pop(key, None)

    async def save_file(self, project: dict, rel: str, content: str, expected_sha1: str = "", session_id: str = "") -> dict:
        # Cancellation of a disconnected HTTP caller must not discard a
        # refresh after the disk write has already committed.
        operation = asyncio.create_task(asyncio.to_thread(self._save_file_sync, project, rel, content, expected_sha1, session_id))
        operation.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return await asyncio.shield(operation)

    def _save_file_sync(self, project: dict, rel: str, content: str, expected_sha1: str, session_id: str) -> dict:
        pid = project["id"]
        root = Path(project["root_path"])
        target = safe_child(root, rel)
        data = content.encode("utf-8")
        after = _editor_text(content)
        if len(data) > MAX_EDIT_BYTES or b"\0" in data:
            raise ValueError("编辑器只支持不超过 2MB 的文本文件")
        with self._save_condition:
            if self._closing_all or self._closed or pid in self._closing_projects or f"{pid}:main" in self._closing_keys:
                raise RuntimeError("代码情报服务正在关闭")
            self._active_saves[pid] = self._active_saves.get(pid, 0) + 1
            lock = self._file_locks.setdefault(root_key(target), threading.Lock())
        try:
            with lock:
                snapshot = _save_snapshot(target)
                before = _editor_text(snapshot[0])
                current_sha1 = sha1_text(before)
                if expected_sha1 and expected_sha1 != current_sha1:
                    raise FileSaveConflict(current_sha1, expected_sha1)
                if before == after:
                    return {"ok": True, "changed": False, "path": rel, "sha1": current_sha1,
                            "index_refresh": self.refresh_status(pid)}
                fd, name = tempfile.mkstemp(prefix=f".{target.name}.editor-", suffix=".tmp", dir=target.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, snapshot[2])
                    try:
                        unchanged_path = safe_child(root, rel) == target
                        current = _save_snapshot(target)
                    except FileNotFoundError as exc:
                        raise FileSaveConflict("", expected_sha1, "写入前文件已被删除，请刷新后对比") from exc
                    if not unchanged_path or current != snapshot:
                        raise FileSaveConflict(sha1_text(_editor_text(current[0])), expected_sha1, "写入前文件已被其他改动更新，请刷新后对比")
                    os.replace(temporary, target)
                finally:
                    with contextlib.suppress(OSError):
                        temporary.unlink()
                patch_id, patch_error = "", ""
                try:
                    record = self.patches(pid).record(rel=rel, session_id=session_id, tool="editor_save", before=before, after=after)
                    patch_id = record.get("id", "")
                except Exception as exc:
                    patch_error = str(exc)[:300]
                refresh, _ = self._queue_scan(project, "main", False, allow_closing=True)
                with self._guard:
                    cached = dict(self._scan_cache.get(f"{pid}:main", {}))
                return {**cached, "ok": True, "changed": True, "path": rel, "sha1": sha1_text(after),
                        "lines": after.count("\n") + 1, "patch_id": patch_id, "patch_error": patch_error,
                        "scanning": refresh["pending"], "index_refresh": refresh}
        finally:
            with self._save_condition:
                self._active_saves[pid] -= 1
                self._save_condition.notify_all()

    # ------------------------------------------------------------------
    # 工作区 → 项目（工具侧用：会话工作区就是项目根）
    # ------------------------------------------------------------------
    def project_for_workspace(self, workspace: Any, user_id: str = "") -> dict | None:
        """按工作区路径反查项目。

        工具拿不到 project_id（它只知道自己的工作区），而代码模式会话的工作区
        **就是**项目根目录，所以用归一化路径反查即可。这也是天然的权限边界：
        能在这个工作区里执行，就说明会话本身已经被授权过。
        """
        if self.projects is None:
            return None
        try:
            key = root_key(workspace)
        except Exception:
            return None
        row = None
        if hasattr(self.projects, "find_by_root"):
            row = self.projects.find_by_root(key)
        if row is not None and user_id and row.get("user_id") not in ("", user_id):
            return None
        return row

    # ------------------------------------------------------------------
    # 修改台账（P2）
    # ------------------------------------------------------------------
    def record_patch(
        self,
        *,
        workspace: Any,
        rel: str,
        session_id: str,
        tool: str,
        before: str,
        after: str,
        old_string: str = "",
        new_string: str = "",
        replace_all: bool = False,
        tree_id: str = "main",
        note: str = "",
    ) -> dict:
        project = self.project_for_workspace(workspace)
        if project is None:
            return {}
        return self.patches(project["id"]).record(
            rel=rel, session_id=session_id, tool=tool, before=before, after=after,
            old_string=old_string, new_string=new_string, replace_all=replace_all,
            tree_id=tree_id, note=note,
        )

    def list_patches(self, project: dict, *, limit: int = 50, path: str | None = None) -> dict:
        store = self.patches(project["id"])
        return {"patches": store.list(limit=limit, path=path), "counts": store.counts()}
    def patch_detail(self, project: dict, patch_id: str) -> dict:
        store = self.patches(project["id"])
        row = store.get(patch_id)
        if row is None:
            raise KeyError(f"改动记录不存在：{patch_id}")
        return PatchStore.detail_view(row)

    def revert_patch(self, project: dict, patch_id: str) -> dict:
        """回滚一次改动。

        安全前提：文件当前内容必须仍等于该次改动**之后**的内容。
        否则说明之后又被改过（另一个会话 / 人工 / 别的工具），直接写回会静默
        覆盖别人的工作——这种情况必须拒绝，让人来判断。
        """
        store = self.patches(project["id"])
        row = store.get(patch_id)
        if row is None:
            raise KeyError(f"改动记录不存在：{patch_id}")
        if row.get("reverted_at"):
            raise ValueError(f"该改动已于 {row['reverted_at']} 回滚过，无需重复操作")

        root = Path(project["root_path"])
        target = safe_child(root, row["path"])
        if not target.is_file():
            raise ValueError(f"文件不存在：{row['path']}")
        current = target.read_text(encoding="utf-8", errors="replace")
        if sha1_text(current) != row["after_sha1"]:
            raise ValueError(
                f"{row['path']} 在这次改动之后又被修改过，拒绝自动回滚"
                "（避免覆盖别人的改动）。请先查看当前内容再决定。"
            )

        before_text = row.get("before_text") or ""
        if before_text:
            restored = before_text
        else:
            # 超大文件没存全文：反向替换（唯一性校验同样不能省）
            old = row.get("old_string") or ""
            new = row.get("new_string") or ""
            if not new or new not in current:
                raise ValueError("该改动未保存全文，且无法在文件中定位其片段，需人工处理")
            restored = current.replace(new, old, -1 if row.get("replace_all") else 1)

        target.write_text(restored, encoding="utf-8")
        store.mark_reverted(patch_id)
        # 回滚本身也是一次改动：记一条新记录（历史里能看到"谁在什么时候回滚了什么"）
        store.record(
            rel=row["path"], session_id=row.get("session_id") or "", tool="revert_patch",
            before=current, after=restored, note=f"回滚 {patch_id}",
        )
        return {"ok": True, "path": row["path"], "patch_id": patch_id, "reverted": True}

    # ------------------------------------------------------------------
    # P6：影响面 / 架构门禁 / 决策线程 / 改动溯源
    # ------------------------------------------------------------------
    def decisions(self, project_id: str) -> DecisionStore:
        """项目的决策线程（单例，与索引同生命周期）。"""
        with self._guard:
            handle = self._decision_stores.get(project_id)
            if handle is None:
                handle = DecisionStore(project_id)
                self._decision_stores[project_id] = handle
            return handle

    def architecture(self, project: dict) -> dict:
        """读架构规则；文件坏了就如实报错，而不是当空规则放行。"""
        from code_agent.codeintel import architecture as arch

        errors: list[str] = []
        try:
            rules = arch.load_rules(project["id"])
        except arch.ArchitectureError as exc:
            rules, errors = {}, [str(exc)]
        if rules:
            errors = arch.validate_rules(rules) or errors
        return {"rules": rules, "errors": errors, "path": str(arch.rules_path(project["id"])),
                "layers": [str(l.get("name")) for l in (rules.get("layers") or []) if isinstance(l, dict)]}

    def save_architecture(self, project: dict, rules: dict) -> dict:
        from code_agent.codeintel import architecture as arch

        arch.save_rules(project["id"], rules)
        return self.architecture(project)

    def impact(
        self,
        project: dict,
        *,
        tree_id: str = "main",
        symbol_ids: list[str] | None = None,
        files: list[str] | None = None,
        depth: int = 3,
    ) -> dict:
        """影响面 + 关联测试 + 风险分级（界面与门禁共用同一份事实）。"""
        from code_agent.codeintel import architecture as arch, impact as impact_mod

        index = self.index(project["id"], tree_id)
        rules = (self.architecture(project).get("rules") or {})
        rel_files = [str(f).replace("\\", "/").strip("/") for f in (files or []) if str(f).strip()]
        result = impact_mod.analyze(
            index, symbol_ids=symbol_ids, files=rel_files, depth=depth,
            protected=arch.protected_by_rules(rules, rel_files) if rules else [],
            cross_layer=self._cross_layer_signals(index, rules, rel_files) if rules else [],
        )
        result["tree"] = tree_id
        return result

    def _cross_layer_signals(self, index: Any, rules: dict, files: list[str]) -> list[dict]:
        """层次违规也进风险理由（可能规则里只声明了 warn 或没配 public_api）。"""
        from code_agent.codeintel import architecture as arch

        return arch.layer_edges(index, rules, files=files)

    def gate(
        self,
        project: dict,
        *,
        tree_id: str = "main",
        files: list[str],
        changed_symbols: list[dict] | None = None,
    ) -> dict:
        """架构门禁：只在有规则时才有话说；没规则 = 不拦（也不假装通过了什么）。"""
        from code_agent.codeintel import architecture as arch

        index = self.index(project["id"], tree_id)
        rules = self.architecture(project).get("rules") or {}
        result = arch.evaluate(index, rules, files=files, changed_symbols=changed_symbols or [])
        result["tree"] = tree_id
        return result

    async def provenance(
        self,
        project: dict,
        *,
        tree_id: str = "main",
        path: str = "",
        symbol_id: str = "",
        tasks: Any = None,
        limit: int = 80,
    ) -> dict:
        """改动溯源时间线：台账 + 任务/批次 + 决策 + git 提交。"""
        from code_agent.codeintel import provenance as prov

        return await prov.timeline(
            project=project, index=self, tasks=tasks, decisions=self.decisions(project["id"]),
            path=path, symbol_id=symbol_id, tree_id=tree_id, limit=limit,
        )

    # ------------------------------------------------------------------
    # P7：测试即规格（行为卡片 / 覆盖率）与图即文档
    # ------------------------------------------------------------------
    def _read_text(self, project: dict, rel: str) -> str:
        """读一个项目内文件（只走边界校验）。"""
        path = safe_child(Path(project["root_path"]), rel)
        if not path.is_file():
            raise ValueError(f"文件不存在：{rel}")
        return path.read_text(encoding="utf-8", errors="replace")

    def _protection(
        self, project: dict, index: Any, tree_id: str
    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """"谁被测试守着"的两张表，按索引 change_seq 缓存。

        为什么必须缓存：算一次要读全部测试文件（本仓 ~1MB），而功能导航的每一层都是
        一次请求——不缓存的话点一下就重读一遍，越点越慢。索引一变（change_seq 变），
        缓存立刻失效，不会拿旧证据糊弄。
        """
        from code_agent.codeintel import features as features_mod

        key = f"{project['id']}:{tree_id}"
        seq = int(index.max_change_seq() or 0)
        with self._guard:
            hit = self._protection_cache.get(key)
            if hit and hit[0] == seq:
                return hit[1], hit[2]
        by_symbol, by_file = features_mod.protection_index(
            index, read_text=lambda rel: self._read_text(project, rel)
        )
        with self._guard:
            self._protection_cache[key] = (seq, by_symbol, by_file)
        return by_symbol, by_file

    def function_logic(self, project: dict, symbol_id: str, *, tree_id: str = "main") -> dict:
        """一个函数的实现步骤 + 调用关系（P8：让地图不止有框架，还有实现逻辑）。"""
        from code_agent.codeintel import logic

        return logic.function_logic(
            self.index(project["id"], tree_id),
            read_text=lambda rel: self._read_text(project, rel),
            symbol_id=symbol_id,
        )

    def symbol_purpose(self, project: dict, symbol_id: str, *, tree_id: str = "main") -> dict:
        """一个符号/文件/目录的"意义与作用"（P10/P12）。

        id 前缀决定问什么：`sym:` 符号 · `file:` 文件 · `mod:` 目录——
        地图上悬停哪个节点就用哪个 id，前端不需要分支。
        """
        from code_agent.codeintel import purpose

        index = self.index(project["id"], tree_id)
        out = purpose.describe_target(
            index,
            read_text=lambda rel: self._read_text(project, rel),
            target=symbol_id,
        )
        self._inject_brief(project, index, symbol_id, out, tree_id=tree_id)
        business = self.symbol_business(project, symbol_id, tree_id=tree_id)
        out["business"] = business.get("business")
        out["entity"] = business.get("entity")
        return out

    # ------------------------------------------------------------------
    # 功能描述（M33）：模型补的"它是干什么的"，按内容哈希失效
    # ------------------------------------------------------------------
    def _current_business_symbols(self, project: dict, rel: str, text: str | None = None) -> list[dict]:
        """Parse a bounded current snapshot without mutating the index on hover."""
        from code_agent.codeintel.store import symbol_hashes

        path = safe_child(Path(project["root_path"]), rel)
        if text is None:
            data, _, _ = _save_snapshot(path)
            text = _editor_text(data)
        else:
            if not isinstance(text, str) or len(text.encode("utf-8", errors="replace")) > MAX_EDIT_BYTES or "\0" in text:
                raise ValueError("功能说明只支持不超过 2MB 的文本文件")
            text = _editor_text(text)
        cache_key = (root_key(project["root_path"]), rel, sha1_text(text))
        with self._guard:
            cached = self._business_parse_cache.get(cache_key)
        if cached is not None:
            return cached
        parsed = parse_file(rel, detect_language(path), text)
        if parsed.get("error"):
            return []
        rows = []
        for symbol in parsed.get("symbols") or []:
            row = {**symbol, "path": rel}
            row["content_hash"], row["body_hash"] = symbol_hashes(symbol, text)
            rows.append(row)
        with self._guard:
            self._business_parse_cache[cache_key] = rows
            while len(self._business_parse_cache) > 32:
                self._business_parse_cache.pop(next(iter(self._business_parse_cache)))
        return rows

    def _business_row(self, project: dict, target: str, tree_id: str, *, refresh: bool = False) -> dict | None:
        """Version symbols from current source; version files/modules from content."""
        if target.startswith("sym:"):
            indexed = self.index(project["id"], tree_id).symbol(target)
            if indexed is None:
                return None
            path = safe_child(Path(project["root_path"]), str(indexed["path"]))
            if not path.is_file() or path.stat().st_size > MAX_EDIT_BYTES:
                return None
            rows = self._current_business_symbols(project, str(indexed["path"]))
            matches = [row for row in rows if row.get("id") == target]
            return matches[0] if len(matches) == 1 else None
        if target.startswith(("file:", "mod:")):
            prefix, rel = target.split(":", 1)
            path = safe_child(Path(project["root_path"]), rel)
            rel = path.relative_to(Path(project["root_path"]).resolve()).as_posix()
            if rel == ".":
                rel = ""
            if prefix == "file":
                if not path.is_file() or path.stat().st_size > MAX_EDIT_BYTES:
                    return None
                data, _, _ = _save_snapshot(path)
                digest = hashlib.sha1(data).hexdigest()
            else:
                if not path.is_dir():
                    return None
                cache_key = (root_key(project["root_path"]), tree_id, rel)
                seq = int(self.index(project["id"], tree_id).max_change_seq() or 0)
                stamp = path.stat().st_mtime_ns
                now = monotonic()
                with self._guard:
                    cached = self._business_module_cache.get(cache_key)
                if not refresh and cached and cached[1:3] == (seq, stamp) and now - cached[0] < BUSINESS_MODULE_CACHE_TTL:
                    return cached[3]
                # Same ignored/sensitive-file policy as the index. Inventory changes
                # also invalidate a module record, even before a scan completes.
                entries = walk_project(path).files
                digest = sha1_text("\n".join(sorted(
                    f"{entry.rel}:{_sha1_file(Path(entry.abs))}" for entry in entries if not entry.skipped
                )))
            row = {"id": f"{prefix}:{rel}", "kind": "file" if prefix == "file" else "module",
                   "name": path.name or project.get("name") or rel, "path": rel, "line": 1,
                   "content_hash": digest, "body_hash": digest}
            if prefix == "file":
                indexed = self.index(project["id"], tree_id).file_row(rel) or {}
                if indexed.get("sha1") == digest:
                    row["doc"] = indexed.get("module_doc") or ""
            if prefix == "mod":
                with self._guard:
                    self._business_module_cache[cache_key] = (now, seq, stamp, row)
                    while len(self._business_module_cache) > 64:
                        self._business_module_cache.pop(next(iter(self._business_module_cache)))
            return row
        return None

    def symbol_business(self, project: dict, target: str, *, tree_id: str = "main") -> dict:
        from code_agent.codeintel.business import business_for, entity_of

        row = self._business_row(project, str(target), tree_id)
        if row is None:
            return {"available": False, "reason": "当前代码中没有这个准确声明或节点", "entity": None, "business": None}
        return {"available": True, "entity": entity_of(row),
                "business": business_for(row, self.briefs(project, tree_id).get(str(row["id"])))}

    def resolve_symbol_business(self, project: dict, *, path: str, line: int, column: int,
                                name: str = "", text: str | None = None, tree_id: str = "main") -> dict:
        from code_agent.codeintel.business import business_for, declaration_at, entity_of

        if isinstance(line, bool) or isinstance(column, bool) or not isinstance(line, int) or not isinstance(column, int) or line < 1 or column < 1:
            raise ValueError("line 和 column 必须是从 1 开始的整数")
        target = safe_child(Path(project["root_path"]), path)
        rel = target.relative_to(Path(project["root_path"]).resolve()).as_posix()
        rows = self._current_business_symbols(project, rel, text)
        row = declaration_at(rows, line, column, str(name or ""))
        if row is None:
            return {"available": False, "reason": "位置未精确命中声明名称；引用需要语言服务器定位声明", "entity": None, "business": None}
        return {"available": True, "entity": entity_of(row),
                "business": business_for(row, self.briefs(project, tree_id).get(str(row["id"])))}

    def write_symbol_business(self, project: dict, target: str, payload: dict, *, tree_id: str = "main") -> dict:
        from code_agent.codeintel.business import business_for, entity_of, validated_fields, version_of

        row = self._business_row(project, str(target), tree_id, refresh=True)
        if row is None:
            raise ValueError("当前代码中没有这个准确声明或节点，请先刷新索引")
        expected = str(payload.get("version") or "")
        if expected and expected != version_of(row):
            raise FileSaveConflict(version_of(row), expected, "代码已变化，请刷新功能说明后再保存")
        fields = validated_fields(payload)
        # Evidence for a manual edit is the current exact code version, not a
        # client-supplied claim that the model or author confirmed anything.
        fields["evidence"] = [f"{row.get('path') or '.'}:{row.get('line') or 1} 人工编辑"]
        item = self._brief_store(project, tree_id).write(
            symbol_id=str(row["id"]), text=fields["business_meaning"],
            content_hash=str(row.get("content_hash") or ""), body_hash=str(row.get("body_hash") or ""),
            path_rel=str(row.get("path") or ""), name=str(row.get("name") or ""), model="human", business=fields,
        )
        with self._guard:
            self._nav_cache.pop(f"{project['id']}:{tree_id}", None)
        return {"available": True, "entity": entity_of(row), "business": business_for(row, item)}

    def _brief_store(self, project: dict, tree_id: str = "main") -> Any:
        from code_agent.codeintel.briefs import BriefStore, root_key_of

        key = root_key_of(project["root_path"])
        if tree_id != "main":
            key += "-" + hashlib.sha1(str(tree_id).encode("utf-8")).hexdigest()[:12]
        with self._guard:
            store = self._brief_stores.get(key)
            if store is None:
                store = BriefStore(key)
                self._brief_stores[key] = store
        return store

    def briefs(self, project: dict, tree_id: str = "main") -> dict[str, dict]:
        """这个仓库当前生效的描述：符号 id → 最后一条（是否过期由内容哈希判定）。"""
        return self._brief_store(project, tree_id).latest()

    def _inject_brief(self, project: dict, index: Any, symbol_id: str, out: dict, *, tree_id: str = "main") -> None:
        """Keep legacy brief metadata; functionality is displayed once in business.

        Technical purpose/facts remain source-derived, rather than repeating a
        model description as though it were a second technical fact.
        """
        from code_agent.codeintel.briefs import MAX_TEXT

        if not symbol_id.startswith("sym:"):
            return
        row = index.symbol(symbol_id)
        if row is None or str(row.get("doc") or "").strip() or (out.get("documentation") or {}).get("text"):
            return
        brief = (self.briefs(project, tree_id)).get(symbol_id)
        if not brief:
            return
        stored = str(brief.get("content_hash") or "")
        stored_body = str(brief.get("body_hash") or "")
        current = str(row.get("content_hash") or "")
        current_body = str(row.get("body_hash") or "")
        fresh = True
        if stored and stored != current:
            fresh = False
        if stored_body and stored_body != current_body:
            fresh = False
        if not (stored or stored_body):
            fresh = False
        if not fresh:
            # 代码改过：这条描述不再采用（也不删，留着让人知道它过期了）
            out["brief_stale"] = True
            return
        text = str(brief.get("text") or "")[:MAX_TEXT]
        from code_agent.codeintel.business import source_of

        origin = source_of(brief)
        out["brief"] = {
            "text": text,
            "model": str(brief.get("model") or ""),
            "ts": str(brief.get("ts") or ""),
            "source": origin,
        }

    def feature_brief_write(
        self,
        project: dict,
        *,
        symbol_id: str,
        text: str,
        model: str = "",
        session_id: str = "",
        tree_id: str = "main",
    ) -> dict:
        """写入/覆盖一条功能描述：哈希由**服务端**从索引取，调用方不需要自己算。"""
        row = self._business_row(project, str(symbol_id), tree_id)
        if row is None:
            raise ValueError(f"符号不存在：{symbol_id}")
        item = self._brief_store(project, tree_id).write(
            symbol_id=str(symbol_id),
            text=text,
            content_hash=str(row.get("content_hash") or ""),
            body_hash=str(row.get("body_hash") or ""),
            path_rel=str(row.get("path") or ""),
            name=str(row.get("name") or ""),
            model=model,
            session_id=session_id,
        )
        with self._guard:
            self._nav_cache.pop(f"{project['id']}:{tree_id}", None)
        return item

    def feature_brief_forget(self, project: dict, *, symbol_id: str, tree_id: str = "main") -> bool:
        removed = self._brief_store(project, tree_id).forget(str(symbol_id))
        if removed:
            with self._guard:
                self._nav_cache.clear()
        return removed

    def feature_brief_missing(
        self, project: dict, *, tree_id: str = "main", limit: int = 30
    ) -> dict:
        """还缺描述的**顶层功能**（作者没写 docstring，也没有生效的模型描述）。

        给 code-agent 自己用：先看清单，再逐条补——而不是让地图一直挂着「无说明」。
        """
        from code_agent.codeintel import features as features_mod

        nav = self.features(project, tree_id=tree_id, limit=0)
        missing: list[dict] = []

        def walk(domain: dict) -> None:
            for node in domain.get("features") or []:
                if node.get("brief_missing"):
                    missing.append(
                        {
                            "id": node["id"],
                            "name": node.get("name") or "",
                            "path": node.get("path") or "",
                            "line": node.get("line") or 0,
                            "kind": node.get("kind") or "",
                            "domain": domain.get("name") or "",
                        }
                    )
            for child in domain.get("children") or []:
                walk(child)

        for domain in nav.get("domains") or []:
            walk(domain)
        total = len(missing)
        return {
            "total": total,
            "items": missing[: max(1, int(limit))],
            "truncated": total > limit,
            "method": "作者没写 docstring、且没有生效的模型描述（过期的不算）",
        }

    def feature_briefs(self, project: dict, *, tree_id: str = "main") -> dict:
        """这个仓库已有的描述（含过期的）：给界面与工具看。"""
        index = self.index(project["id"], tree_id)
        latest = self.briefs(project, tree_id)
        items: list[dict] = []
        stale = 0
        for sid, item in sorted(latest.items()):
            row = index.symbol(sid) if sid.startswith("sym:") else self._business_row(project, sid, tree_id)
            current = str((row or {}).get("content_hash") or "")
            current_body = str((row or {}).get("body_hash") or "")
            fresh = bool(current or current_body)
            if str(item.get("content_hash") or "") and str(item.get("content_hash")) != current:
                fresh = False
            if str(item.get("body_hash") or "") and str(item.get("body_hash")) != current_body:
                fresh = False
            if not fresh:
                stale += 1
            items.append(
                {
                    "symbol_id": sid,
                    "text": item.get("text", ""),
                    "model": item.get("model", ""),
                    "ts": item.get("ts", ""),
                    "path": item.get("path", ""),
                    "name": item.get("name", ""),
                    "fresh": fresh,
                    "missing_symbol": row is None,
                }
            )
        return {
            "count": len(items),
            "stale": stale,
            "path": str(self._brief_store(project, tree_id).path),
            "items": items,
        }

    # ------------------------------------------------------------------
    # 功能描述：源码变动后自动增量补，同一份描述供地图与编辑器使用
    # ------------------------------------------------------------------
    def set_brief_provider(self, factory: Any) -> None:
        """注入"怎么拿到模型"（由 API 层在启动时接上模型路由）。

        服务层不 import 路由：这样单测可以直接塞一个假 provider，
        也避免 codeintel 依赖 agent/session 那一层。
        """
        self.brief_provider_factory = factory
        with self._guard:
            self._brief_checked.clear()
            for state in self._fill_jobs.values():
                state["retry_after"] = 0.0

    def bind_brief_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Keep provider connections on the server loop, including sync GET callers."""
        with self._guard:
            if self._brief_loop is None or not self._brief_loop.is_running() or self._brief_loop.is_closed():
                self._brief_loop = loop

    def _cancel_brief_jobs(self, project_id: str | None = None, tree_id: str | None = None) -> None:
        """Invalidate late callbacks permanently, even if a provider ignores cancel."""
        with self._guard:
            futures = []
            for key, job in self._fill_jobs.items():
                if project_id is not None and job.get("project_id") != project_id:
                    continue
                if tree_id is not None and job.get("tree") != tree_id:
                    continue
                self._brief_generations[key] = self._brief_generations.get(key, 0) + 1
                job.update({"running": False, "state": "waiting", "note": "项目已关闭，补描述已停止"})
                future = self._brief_futures.pop(key, None)
                if future:
                    futures.append(future)
        for future in futures:
            future.cancel()

    def _assert_brief_generation(self, key: str, generation: int | None) -> None:
        if generation is not None and self._brief_generations.get(key) != generation:
            raise ValueError("项目已关闭，旧补描述结果已丢弃")

    def _brief_key(self, project: dict, tree_id: str = "main") -> str:
        return self._brief_store(project, tree_id).root_key

    def _brief_signature(self, project: dict, tree_id: str = "main") -> tuple:
        index = self.index(project["id"], tree_id)
        stamp = self._brief_store(project, tree_id).stamp()
        with index.read_snapshot() as version:
            return (project["id"], tree_id, *version, stamp)

    def _brief_fill_state(self, project: dict, tree_id: str = "main") -> dict:
        with self._guard:
            state = self._fill_jobs.get(self._brief_key(project, tree_id))
            return dict(state) if state else {"running": False}

    def brief_fill_status(self, project: dict, tree_id: str = "main", *, cached_if_busy: bool = False) -> dict:
        """补描述的进度（界面按它显示"正在补 40/123"）。"""
        state = self._brief_fill_state(project, tree_id)
        key = self._brief_key(project, tree_id)
        # One integer token also covers manual writes/deletes, author comments,
        # and batches written while the longer model job is still running.
        busy = cached_if_busy and self.refresh_status(project["id"], tree_id)["pending"]
        token = None if busy else self._brief_signature(project, tree_id)
        with self._guard:
            previous, revision = self._brief_revision.get(key, (None, 0))
            if token is not None and token != previous:
                revision += 1
                self._brief_revision[key] = (token, revision)
        return {
            "running": bool(state.get("running")),
            "total": int(state.get("total") or 0),
            "done": int(state.get("done") or 0),
            "written": int(state.get("written") or 0),
            "failed": int(state.get("failed") or 0),
            "skipped": int(state.get("skipped") or 0),
            "started_at": state.get("started_at") or "",
            "finished_at": state.get("finished_at") or "",
            "auto": bool(state.get("auto")),
            "error": state.get("error") or "",
            "note": state.get("note") or "",
            "state": str(state.get("state") or "idle"),
            "revision": revision,
            "revision_epoch": self._brief_epoch,
            "coverage_known": bool(state.get("coverage_known")),
            "coverage_total": int(state.get("coverage_total") or 0),
            "described": int(state.get("described") or 0),
            "reused": int(state.get("reused") or 0),
            "added": int(state.get("added") or 0),
            "stale": int(state.get("stale") or 0),
            "target_counts": dict(state.get("target_counts") or {}),
            "missing": int(state.get("missing") or 0),
            "retry_in": max(0, int(float(state.get("retry_after") or 0) - monotonic())),
            "tree": tree_id,
        }

    def _collect_targets(self, project: dict, *, limit: int, tree_id: str = "main", generation: int | None = None) -> list[Any]:
        """All evidenced declarations, files and modules share one fill inventory.

        This runs in a worker thread. Unsupported or failed parses are excluded;
        no name-only meaning is invented for a word that has no declaration.
        """
        from code_agent.codeintel import brief_fill
        from code_agent.codeintel.business import business_for

        key = self._brief_key(project, tree_id)
        with self._guard:
            self._assert_brief_generation(key, generation)
            index = self.index(project["id"], tree_id)
        files = index.file_rows()
        rows = [row for row in index.all_symbols()
                if row.get("confidence") != "low" and not (files.get(row["path"]) or {}).get("error")]
        eligible = {rel: row for rel, row in files.items()
                    if not row.get("skipped") and not row.get("error") and row.get("sha1")
                    and int(row.get("size") or 0) <= MAX_EDIT_BYTES and row.get("lang") not in {"", "unknown"}}
        texts: dict[str, str] = {}

        def read_text(rel: str) -> str:
            if rel not in texts:
                texts[rel] = self._read_text(project, rel)
            return texts[rel]

        for rel, file in sorted(eligible.items()):
            rows.append({"id": f"file:{rel}", "name": Path(rel).name, "kind": "file", "path": rel,
                         "line": 1, "end_line": 60, "doc": file.get("module_doc") or "",
                         "content_hash": file["sha1"], "body_hash": file["sha1"]})
        module_files: dict[str, list[str]] = {}
        module_digests: dict[str, list[str]] = {}
        for path, file in files.items():
            if file.get("skipped"):
                continue
            parents = ["", *(parent.as_posix() for parent in Path(path).parents if parent.as_posix() != ".")]
            for rel in parents:
                prefix = rel + "/" if rel else ""
                module_digests.setdefault(rel, []).append(f"{path[len(prefix):]}:{file.get('sha1') or ''}")
                if path in eligible:
                    module_files.setdefault(rel, []).append(path)
        for rel, children in sorted(module_files.items()):
            digest = sha1_text("\n".join(sorted(module_digests[rel])))
            rows.append({"id": f"mod:{rel}", "name": Path(rel).name if rel else project.get("name", "根目录"),
                         "kind": "module", "path": rel, "line": 1, "source_files": sorted(children)[:4],
                         "content_hash": digest, "body_hash": digest})
        latest = self.briefs(project, tree_id)
        targets: list[Any] = []
        described = 0
        supported = 0
        added = stale = 0
        target_counts: dict[str, int] = {}
        for row in rows:
            if not (row.get("content_hash") or row.get("body_hash")):
                continue
            value = business_for(row, latest.get(str(row["id"])))
            if value["business_meaning"] and value["status"] in {"current", "unreviewed"}:
                # Indexed hashes and author comments are enough to reuse a
                # current explanation. Do not read unchanged source files.
                supported += 1
                described += 1
                continue
            if row.get("kind") == "module":
                evidence = []
                for child in row["source_files"]:
                    try:
                        evidence.append(f"文件 {child}:\n" + "\n".join(read_text(child).splitlines()[:15]))
                    except (OSError, ValueError):
                        continue
                source = "\n".join(evidence)
            else:
                source = str(row.get("source") or brief_fill.source_segment(read_text, row))
            if not source.strip():
                continue
            supported += 1
            stale += int(value["status"] == "stale")
            added += int(value["status"] != "stale")
            kind = str(row.get("kind") or "unknown")
            target_counts[kind] = target_counts.get(kind, 0) + 1
            targets.append(
                brief_fill.Target(
                    id=str(row["id"]),
                    name=str(row.get("name") or ""),
                    kind=str(row.get("kind") or ""),
                    path=str(row.get("path") or ""),
                    line=int(row.get("line") or 0),
                    signature=str(row.get("signature") or ""),
                    source=source,
                    content_hash=str(row.get("content_hash") or ""),
                    body_hash=str(row.get("body_hash") or ""),
                )
            )
        with self._guard:
            self._assert_brief_generation(key, generation)
            state = self._fill_jobs.get(self._brief_key(project, tree_id))
            if state is not None:
                state.update({"coverage_known": bool(supported), "coverage_total": supported,
                              "described": described, "missing": len(targets),
                              "reused": described, "added": added, "stale": stale, "target_counts": target_counts})
        return targets[:limit] if limit > 0 else targets

    async def _brief_fill_worker(
        self, project: dict, *, limit: int = 0, auto: bool = False, tree_id: str = "main", signature: tuple, generation: int
    ) -> None:
        from code_agent.codeintel import brief_fill
        from code_agent.codeintel.business import is_current, source_of

        store = self._brief_store(project, tree_id)
        key = self._brief_key(project, tree_id)

        def write(target: Any, text: str) -> bool:
            with self._guard:
                self._assert_brief_generation(key, generation)
                if self._closed or self._closing_all or project["id"] in self._closing_projects or f"{project['id']}:{tree_id}" in self._closing_keys:
                    raise ValueError("项目已关闭，停止补功能描述")
                self._active_brief_writes[key] = self._active_brief_writes.get(key, 0) + 1
            try:
                row = self._business_row(project, target.id, tree_id, refresh=True)
                if row is None or not is_current({"content_hash": target.content_hash, "body_hash": target.body_hash}, row):
                    raise brief_fill.TargetChanged("源码已变化，旧结果已丢弃，等待下一次增量补描述")
                before = store.latest().get(target.id)
                item = store.write(
                    symbol_id=target.id,
                    text=text,
                    content_hash=target.content_hash,
                    body_hash=target.body_hash,
                    path_rel=target.path,
                    name=target.name,
                    model=str(state.get("model") or ""),
                    business={"business_meaning": text, "evidence": [f"{target.path or '.'}:{target.line or 1} 当前源码片段"]},
                )
            finally:
                with self._save_condition:
                    self._active_brief_writes[key] -= 1
                    self._save_condition.notify_all()
            if before and item.get("id") == before.get("id") and source_of(item) == "human":
                return False
            self._invalidate_nav(project["id"])
            return True

        def progress(update: dict) -> None:
            with self._guard:
                current = self._fill_jobs.get(key)
                if current and self._brief_generations.get(key) == generation:
                    current.update(update)
                    current["described"] = int(state.get("initial_described") or 0) + int(update.get("written") or 0)
                    current["missing"] = max(0, int(current.get("coverage_total") or 0) - current["described"])

        state = self._brief_fill_state(project, tree_id)
        try:
            targets = await asyncio.to_thread(self._collect_targets, project, limit=limit, tree_id=tree_id, generation=generation)
            state = self._brief_fill_state(project, tree_id)
            state["initial_described"] = state.get("described", 0)
            initial_inventory = {name: state.get(name) for name in ("reused", "added", "stale", "target_counts")}
            with self._guard:
                self._assert_brief_generation(key, generation)
                self._fill_jobs[key].update({"state": "running", "total": len(targets)})
            if not targets:
                with self._guard:
                    self._assert_brief_generation(key, generation)
                    self._fill_jobs[key].update({"state": "complete" if state.get("coverage_known") else "waiting",
                                                "running": False, "finished_at": _now_iso(),
                                                "note": "功能描述已齐" if state.get("coverage_known") else "暂无已索引且可确认的代码实体"})
                    self._brief_checked[key] = signature
                return
            provider = None
            if self.brief_provider_factory is not None:
                provider = self.brief_provider_factory()
            if str(getattr(provider, "name", "")) == "offline":
                provider = None
            state["model"] = str(getattr(provider, "model", "") or "")
            with self._guard:
                self._assert_brief_generation(key, generation)
                self._fill_jobs[key]["model"] = state["model"]
            result = await brief_fill.fill(
                targets, provider=provider, write=write, on_progress=progress, threaded_write=True,
                timeout=BRIEF_MODEL_TIMEOUT,
                should_continue=lambda: self._brief_signature(project, tree_id)[:4] == signature[:4],
            )
            # Reconcile coverage from current code and any manual descriptions
            # entered while this request was in flight, without another model call.
            await asyncio.to_thread(self._collect_targets, project, limit=0, tree_id=tree_id, generation=generation)
            if self._brief_signature(project, tree_id)[:4] != signature[:4]:
                result.source_changed = True
                result.aborted = "源码已变化，等待增量更新"
            view = result.view()
            with self._guard:
                job = self._fill_jobs.get(key)
                if job is not None and self._brief_generations.get(key) == generation:
                    incomplete = bool(job.get("missing"))
                    job.update(
                        {
                            "running": False,
                            "finished_at": _now_iso(),
                            "written": view["written"],
                            "failed": view["failed"],
                            "skipped": view["skipped"],
                            "done": view["written"] + view["failed"] + view["skipped"],
                            "error": "; ".join(view["errors"][:2]) if incomplete else "",
                            "note": view["aborted"] or ("重试后功能描述已补全" if view["errors"] and not incomplete else ""),
                            "model": view["model"],
                            "state": "waiting" if view["aborted"] else ("error" if incomplete else "complete"),
                            "retry_after": monotonic() + BRIEF_RETRY_COOLDOWN if incomplete and not view["source_changed"] else 0.0,
                            **initial_inventory,
                        }
                    )
                    if not incomplete:
                        current_signature = self._brief_signature(project, tree_id)
                        if current_signature[:4] == signature[:4]:
                            self._brief_checked[key] = current_signature
        except asyncio.CancelledError:
            with self._guard:
                job = self._fill_jobs.get(key)
                if job and self._brief_generations.get(key) == generation:
                    job.update({"running": False, "state": "waiting", "note": "补描述已暂停", "finished_at": _now_iso()})
            raise
        except Exception as exc:  # noqa: BLE001 —— 后台任务不能静默死掉
            with self._guard:
                job = self._fill_jobs.get(key)
                if job is not None and self._brief_generations.get(key) == generation:
                    job.update(
                        {"running": False, "state": "error", "finished_at": _now_iso(), "error": str(exc)[:300],
                         "retry_after": monotonic() + BRIEF_RETRY_COOLDOWN}
                    )
        finally:
            with self._guard:
                if self._brief_generations.get(key) == generation:
                    self._brief_futures.pop(key, None)

    def _invalidate_nav(self, project_id: str) -> None:
        with self._guard:
            for key in [k for k in self._nav_cache if k.startswith(f"{project_id}:")]:
                self._nav_cache.pop(key, None)

    def retrieval_store(self, project: dict) -> Any:
        """这个项目的检索 chunk 库（`code/<pid>/retrieval.db`）。

        按项目分库：符号 id（`sym:路径#名字`）只在项目内有意义，混库会让两个仓库的
        同名文件互相串味。**向量库是全局的**（按内容哈希），内容相同就复用。
        """
        pid = str(project.get("id") or "")
        if not pid:
            return None
        with self._guard:
            store = self._chunk_stores.get(pid)
        if store is None:
            from code_agent.retrieval.store import chunk_store

            store = chunk_store(f"code-{pid}") or False
            with self._guard:
                self._chunk_stores[pid] = store
        return store or None

    def start_brief_fill(
        self, project: dict, *, limit: int = 0, auto: bool = False, tree_id: str = "main"
    ) -> dict:
        """启动一次补描述（**不阻塞**）：返回"开没开起来 + 一共要补多少"。

        Collection is background work, including when a sync GET schedules it.
        """
        try:
            self.bind_brief_loop(asyncio.get_running_loop())
        except RuntimeError:
            pass
        key = self._brief_key(project, tree_id)
        signature = self._brief_signature(project, tree_id)
        with self._guard:
            state = self._fill_jobs.get(key, {})
            if state.get("running"):
                return {"started": False, "reason": "正在自动补功能描述", **self.brief_fill_status(project, tree_id)}
            if self._closed or self._closing_all or project["id"] in self._closing_projects or f"{project['id']}:{tree_id}" in self._closing_keys:
                return {"started": False, "reason": "项目正在关闭"}
            if auto and self._brief_checked.get(key) == signature:
                return {"started": False, "reason": "当前代码的功能描述已检查", **self.brief_fill_status(project, tree_id)}
            if auto and float(state.get("retry_after") or 0) > monotonic():
                return {"started": False, "reason": "等待自动重试功能描述", **self.brief_fill_status(project, tree_id)}
            loop = self._brief_loop
            if loop is None or loop.is_closed() or not loop.is_running():
                return {"started": False, "reason": "等待服务启动后自动补功能描述", **self.brief_fill_status(project, tree_id)}
            self._fill_jobs[key] = {
                "running": True,
                "state": "queued",
                "total": 0,
                "done": 0,
                "written": 0,
                "failed": 0,
                "skipped": 0,
                "started_at": _now_iso(),
                "finished_at": "",
                "auto": bool(auto),
                "error": "",
                "note": "",
                "model": "",
                "project_id": project["id"],
                "tree": tree_id,
                "coverage_known": False,
                "coverage_total": 0,
                "described": 0,
                "missing": 0,
            }
            generation = self._brief_generations.get(key, 0) + 1
            self._brief_generations[key] = generation
            future = asyncio.run_coroutine_threadsafe(
                self._brief_fill_worker(dict(project), limit=limit, auto=auto, tree_id=tree_id, signature=signature, generation=generation), loop)
            self._brief_futures[key] = future
        return {"started": True, "total": 0, "auto": bool(auto), "state": "queued"}

    def maybe_autofill_briefs(self, project: dict, *, tree_id: str = "main") -> dict:
        """Automatically inspect new content; old one-shot markers are ignored.

        Reads only schedule once per completed index signature. Failed providers
        get a bounded cooldown instead of a model request on every status poll.
        """
        return self.start_brief_fill(project, auto=True, tree_id=tree_id)

    def behaviors(
        self,
        project: dict,
        *,
        tree_id: str = "main",
        path: str = "",
        module: str = "",
        target: str = "",
        limit: int = 0,
    ) -> dict:
        """行为卡片：每条测试函数的意图、断言数、它盯着谁。"""
        from code_agent.codeintel import spec

        index = self.index(project["id"], tree_id)
        return spec.behaviors(
            index,
            read_text=lambda rel: self._read_text(project, rel),
            path=path,
            module=module,
            target=target,
            limit=limit or spec.MAX_BEHAVIORS,
        )

    def coverage(self, project: dict, *, tree_id: str = "main") -> dict:
        """覆盖率三态（直接 / 文件级 / 无）+ 未覆盖的公共符号。"""
        from code_agent.codeintel import spec

        return spec.coverage(
            self.index(project["id"], tree_id),
            read_text=lambda rel: self._read_text(project, rel),
        )

    def features(self, project: dict, *, tree_id: str = "main", limit: int = 0) -> dict:
        """功能导航第一屏（M33）：能力域 → 功能（更深的层由 `feature_children` 按需给）。"""
        from code_agent.codeintel import features as features_mod

        index = self.index(project["id"], tree_id)
        token = self._brief_signature(project, tree_id)
        cache_key = f"{project['id']}:{tree_id}"
        if not limit:
            with self._guard:
                hit = self._nav_cache.get(cache_key)
            if hit and hit[0] == token:
                return hit[1]
        by_symbol, by_file = self._protection(project, index, tree_id)
        nav = features_mod.build_nav(
            index,
            read_text=lambda rel: self._read_text(project, rel),
            per_domain=limit or features_mod.MAX_FEATURES_PER_DOMAIN,
            by_symbol=by_symbol,
            by_file=by_file,
            briefs=self.briefs(project, tree_id),
        )
        self._attach_nav_business(project, index, nav, tree_id)
        if not limit:
            with self._guard:
                self._nav_cache[cache_key] = (token, nav)
        return nav

    def _attach_nav_business(self, project: dict, index: Any, payload: dict, tree_id: str) -> None:
        """Attach module/file shared records without rescanning for every map node."""
        from code_agent.codeintel.business import business_for
        from code_agent.codeintel.features import doc_label

        items = self.briefs(project, tree_id)
        files = index.file_rows()

        def visit(node: dict) -> None:
            sid = str(node.get("id") or "")
            row = None
            if sid.startswith("fdomain:"):
                rel = sid[len("fdomain:"):]
                rel = "" if rel == "根目录" else rel
                prefix = rel + "/" if rel else ""
                digest = sha1_text("\n".join(sorted(
                    f"{path[len(prefix):]}:{file.get('sha1') or ''}" for path, file in files.items()
                    if path.startswith(prefix) and not file.get("skipped")
                )))
                row = {"id": f"mod:{rel}", "content_hash": digest, "body_hash": digest}
            elif sid.startswith("file:"):
                file = files.get(sid[len("file:"):]) or {}
                digest = str(file.get("sha1") or "")
                row = {"id": sid, "content_hash": digest, "body_hash": digest, "doc": file.get("module_doc") or ""}
            if row:
                business = business_for(row, items.get(row["id"]))
                node["business"] = business
                if business["status"] in {"current", "unreviewed"} and business["business_meaning"]:
                    node["label"] = doc_label(business["business_meaning"]) or node.get("label") or node.get("name")
                    node["label_from"] = "business"
            for field in ("domains", "features", "children"):
                for child in node.get(field) or []:
                    visit(child)

        visit(payload)

    def feature_children(
        self,
        project: dict,
        *,
        node_id: str,
        tree_id: str = "main",
        visited: tuple[str, ...] = (),
    ) -> dict:
        """点开一个功能节点时的下一层（可以一直拆到原子步骤）。"""
        from code_agent.codeintel import features as features_mod

        index = self.index(project["id"], tree_id)
        by_symbol, by_file = self._protection(project, index, tree_id)
        payload = features_mod.children_of(
            index,
            read_text=lambda rel: self._read_text(project, rel),
            node_id=node_id,
            visited=visited,
            by_symbol=by_symbol,
            by_file=by_file,
            briefs=self.briefs(project, tree_id),
        )
        self._attach_nav_business(project, index, payload, tree_id)
        return payload

    # ------------------------------------------------------------------
    # P7：证据角标（archify 的 Verified Source Beacon）
    # ------------------------------------------------------------------
    def evidence(self, project: dict, *, tree_id: str = "main") -> dict:
        """每个节点上有多少**已记录的证据**（改动台账 + 决策线程）。

        为什么只算这两样：它们是"平台自己记下来的事实"，可以逐条点开看。
        git 提交要看 diff（在溯源里），测试覆盖是另一层语义（在覆盖率里）——
        混成一个数字就会变成"看起来很权威的假指标"。
        角标是**只读视图**：不改变任何代码与索引，也（按 archify 的 Canonical Clean
        规则）不进任何导出物。
        """
        index = self.index(project["id"], tree_id)
        paths = index.file_rows()
        patch_counts = self.patches(project["id"]).counts_by_path()
        summary = self.decisions(project["id"]).summary()
        targets = {str(k): int((v or {}).get("total") or 0) for k, v in (summary.get("targets") or {}).items()}

        by_path: dict[str, dict] = {}
        for rel in set(patch_counts) | {str(t).removeprefix("file:") for t in targets if str(t).startswith("file:")}:
            if rel not in paths:
                continue          # 已经被删掉的文件没必要给角标
            by_path[rel] = {
                "patches": int(patch_counts.get(rel, 0)),
                "decisions": int(targets.get(f"file:{rel}", 0)),
            }
        by_path = {rel: item for rel, item in by_path.items() if item["patches"] or item["decisions"]}

        by_symbol = {
            target.removeprefix("sym:"): count
            for target, count in targets.items()
            if target.startswith("sym:") and count
        }
        by_module = {
            target.removeprefix("mod:"): count
            for target, count in targets.items()
            if target.startswith("mod:") and count
        }
        total = sum(int(item["patches"]) + int(item["decisions"]) for item in by_path.values())
        total += sum(by_symbol.values()) + sum(by_module.values())
        return {
            "paths": by_path,
            "symbols": by_symbol,
            "modules": by_module,
            "totals": {
                "patches": sum(int(v) for v in patch_counts.values()),
                "decisions": int(summary.get("total") or 0),
                "nodes": len(by_path) + len(by_symbol) + len(by_module),
                "items": total,
            },
            "basis": "只数改动台账（未回滚的精确编辑）与决策线程；提交与测试分别在溯源/覆盖率里",
            "viewer_only": True,
        }

    # ------------------------------------------------------------------
    # 访问标记与后台 watcher
    # ------------------------------------------------------------------
    def touch(self, project_id: str) -> None:
        """标记"这个项目刚刚有人在看"。watcher 与 ensure_fresh 都以此为准。"""
        import time

        self._used[project_id] = time.monotonic()

    def recent_projects(self, *, within_s: float = 60.0) -> list[str]:
        """最近 in-flight 的项目。超过窗口的自动清掉（不再走盘）。"""
        import time

        now = time.monotonic()
        stale = [pid for pid, ts in self._used.items() if now - ts > within_s]
        for pid in stale:
            self._used.pop(pid, None)
        return sorted(self._used, key=lambda pid: self._used[pid], reverse=True)

    async def watch_once(self, *, within_s: float = 60.0, limit: int = 3) -> list[str]:
        """对"最近被查看"的项目做一次新鲜度检查，过期就增量重扫。

        为什么由服务端做、而不是让前端每几秒带着 ensure=1 拉地图：
        前端那样做等于把一次全量走盘塞进 HTTP 请求（184 个文件约 0.5s），
        既阻塞请求线程又随并发放大。放在这里只有一个后台任务在做，
        且**只在有人看的时候**做（within_s 之外自动停），代价有界。
        """
        changed: list[str] = []
        for pid in self.recent_projects(within_s=within_s)[:limit]:
            if self.projects is None:
                break
            row = self.projects.get(pid)
            if row is None:
                continue
            try:
                fresh = await asyncio.to_thread(self.ensure_fresh, row)
            except Exception:
                continue
            if fresh:
                continue
            try:
                await self.scan(row)
                changed.append(pid)
            except Exception:
                continue
        return changed

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self, project: dict, tree_id: str = "main", *, cached_if_busy: bool = False) -> dict:
        key = f"{project['id']}:{tree_id}"
        refresh = self.refresh_status(project["id"], tree_id)
        if cached_if_busy and refresh["pending"]:
            with self._guard:
                cached = dict(self._scan_cache.get(key, {}))
            # Counts are the last completed/read snapshot, never a partial
            # SQLite transaction. Cold progress explicitly leaves them unknown.
            if not cached:
                cached = {"version": None, "change_seq": None, "last_scan_at": "", "indexed": None,
                          "files": None, "symbols": None, "edges": None, "kinds": {}, "scan": {},
                          "root": project["root_path"]}
            return {**cached, "scanning": True, "index_refresh": refresh, "status_partial": True}
        index = self.index(project["id"], tree_id)
        counts = index.counts()
        summary = index.scan_summary()
        refresh = self.refresh_status(project["id"], tree_id)
        body = {
            "version": index.version(),
            "change_seq": index.max_change_seq(),
            "last_scan_at": index.get_meta("last_scan_at") or "",
            "scanning": refresh["pending"],
            "index_refresh": refresh,
            "indexed": bool(counts["files"] or counts["symbols"]),
            "files": counts["files"],
            "symbols": counts["symbols"],
            "edges": counts["edges"],
            "kinds": index.kind_counts(),
            "scan": summary,
            "root": project["root_path"],
        }
        with self._guard:
            self._scan_cache[key] = {name: value for name, value in body.items() if name not in {"scanning", "index_refresh"}}
        return body

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    async def scan(self, project: dict, *, full: bool = False, tree_id: str = "main") -> dict:
        """扫描项目。full=True 先清空索引（换分支/大批量外部改动后用）。"""
        self.bind_brief_loop(asyncio.get_running_loop())
        key = f"{project['id']}:{tree_id}"
        with self._guard:
            busy = key in self._scan_futures
        if busy:
            body = await asyncio.to_thread(self.status, project, tree_id, cached_if_busy=True)
            return {"status": "busy", **body}
        _, future = self._queue_scan(project, tree_id, full)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(wrapped)

    def refresh_status(self, project_id: str, tree_id: str = "main") -> dict:
        with self._guard:
            job = self._refresh_jobs.get(f"{project_id}:{tree_id}", {})
            state = str(job.get("state") or "idle")
            return {"state": state, "pending": state in {"queued", "running"},
                    "requested_generation": int(job.get("requested_generation") or 0),
                    "completed_generation": int(job.get("completed_generation") or 0),
                    "error": str(job.get("error") or ""), "queued_at": job.get("queued_at") or "",
                    "started_at": job.get("started_at") or "", "finished_at": job.get("finished_at") or ""}

    def request_refresh(self, project: dict, *, tree_id: str = "main") -> dict:
        """Mark committed disk changes dirty without waiting for project indexing."""
        return self._queue_scan(project, tree_id, False)[0]

    def _queue_scan(self, project: dict, tree_id: str, full: bool, *, allow_closing: bool = False) -> tuple[dict, Future]:
        key = f"{project['id']}:{tree_id}"
        with self._guard:
            closing = self._closing_all or project["id"] in self._closing_projects or key in self._closing_keys
            if self._closed or (closing and not allow_closing):
                raise RuntimeError("代码情报服务正在关闭")
            job = self._refresh_jobs.setdefault(key, {"requested_generation": 0, "completed_generation": 0})
            job["requested_generation"] += 1
            job.update({"project": dict(project), "full": bool(job.get("full") or full),
                        "queued_at": _now_iso(), "error": ""})
            future = self._scan_futures.get(key)
            if future is None:
                job["state"] = "queued"
                self._scanning.add(key)
                future = self._scan_executor.submit(self._refresh_worker, key, tree_id)
                self._scan_futures[key] = future
            return self.refresh_status(project["id"], tree_id), future

    def _refresh_worker(self, key: str, tree_id: str) -> dict:
        while True:
            with self._guard:
                job = self._refresh_jobs[key]
                generation, project, full = job["requested_generation"], job["project"], job.get("full", False)
                job.update({"state": "running", "full": False, "started_at": _now_iso()})
            result, failure = None, None
            try:
                result = self._scan_sync(project, full, tree_id)
            except Exception as exc:
                failure = exc
            with self._guard:
                job = self._refresh_jobs[key]
                errors = (result or {}).get("scan", {}).get("errors", [])
                if not failure and not errors:
                    job["completed_generation"] = generation
                job["error"] = str(failure)[:500] if failure else (f"{len(errors)} 个文件解析失败，请查看扫描详情" if errors else "")
                if job["requested_generation"] > generation:
                    # A save during this scan may have arrived after its file
                    # was visited. A second pass reads the latest disk version.
                    continue
                job.update({"state": "error" if failure or errors else "done", "finished_at": _now_iso()})
                self._scanning.discard(key)
                self._scan_futures.pop(key, None)
                if result is not None:
                    result.update({"scanning": False, "index_refresh": self.refresh_status(project["id"], tree_id)})
            if failure:
                raise failure
            if result is not None and self.brief_provider_factory is not None:
                result["briefs_autofill"] = self.maybe_autofill_briefs(project, tree_id=tree_id)
            return result or {"status": "done"}

    def _scan_sync(self, project: dict, full: bool, tree_id: str) -> dict:
        raw_root = str(project.get("root_path") or "").strip()
        # 空串/相对路径一律拒绝：`Path("")` 是 `Path(".")`，会把扫描对象悄悄换成
        # "服务进程的当前目录"（平台源码自己）——这个错误曾经真实发生过。
        if not raw_root or not Path(raw_root).is_absolute():
            raise ValueError(f"项目根必须是绝对路径：{raw_root or '（空）'}")
        root = Path(raw_root)
        if not root.is_dir():
            raise ValueError(f"项目目录不可用：{root}")
        index = self.index(project["id"], tree_id)
        if full:
            index.reset()

        walk = walk_project(root)
        known = {entry.rel for entry in walk.files}
        existing = index.file_rows()
        # 新增目标文件也会改变旧文件的依赖解析；只看 importer 指纹会一直漏边。
        import_inventory_changed = any(
            rel.endswith(graph.IMPORT_TARGET_EXTS)
            for rel in known.symmetric_difference(existing)
        )
        refresh_old_imports = index.get_meta("imports_revision") != graph.IMPORTS_REVISION

        parsed_files = 0
        reused = 0
        touched = 0
        removed = 0
        errors: list[dict] = []
        externals = 0
        capped_files: list[str] = []
        import_edges_refreshed = 0

        def refresh_imports(entry: FileEntry) -> None:
            nonlocal import_edges_refreshed
            if not (import_inventory_changed or refresh_old_imports) or entry.lang not in {
                "python", "javascript", "typescript", "vue", "svelte", "java"
            }:
                return
            text = Path(entry.abs).read_text(encoding="utf-8", errors="replace")
            parsed = parse_file(entry.rel, entry.lang, text)
            if parsed.get("error"):
                errors.append({"path": entry.rel, "error": parsed["error"]})
                return
            edges, _ = graph.import_edges(entry.rel, parsed.get("imports", []), known)
            if index.replace_import_edges(entry.rel, edges):
                import_edges_refreshed += 1

        for entry in walk.files:
            row = existing.get(entry.rel)
            try:
                if entry.skipped:
                    if row is None or row["sha1"] == "" or row["skipped"] != entry.skipped:
                        index.mark_file(
                            rel=entry.rel, lang=entry.lang, size=entry.size, mtime=entry.mtime,
                            sha1="", skipped=entry.skipped,
                        )
                    continue

                # 指纹命中：连哈希都不算（增量扫描的关键路径）
                if (
                    row is not None
                    and not row["skipped"]
                    and is_strategy_current(entry.lang, str(row.get("strategy") or ""))
                    and abs(float(row["mtime"] or 0) - entry.mtime) < 1e-6
                    and int(row["size"] or 0) == entry.size
                ):
                    refresh_imports(entry)
                    if row.get("error"):
                        errors.append({"path": entry.rel, "error": row["error"]})
                    reused += 1
                    continue

                sha1 = _sha1_file(Path(entry.abs))
                if row is not None and row["sha1"] == sha1 and not row["skipped"] and is_strategy_current(entry.lang, str(row.get("strategy") or "")):
                    refresh_imports(entry)
                    if row.get("error"):
                        errors.append({"path": entry.rel, "error": row["error"]})
                    index.touch_file(entry.rel, entry.size, entry.mtime)
                    touched += 1
                    continue

                text = Path(entry.abs).read_text(encoding="utf-8", errors="replace")
                if len(text.encode("utf-8", errors="replace")) > MAX_FILE_BYTES * 4:
                    text = text[: MAX_FILE_BYTES * 2]
                parsed = parse_file(entry.rel, entry.lang, text)
                edges, external_count = graph.import_edges(entry.rel, parsed.get("imports", []), known)
                externals += external_count
                if len(parsed.get("symbols") or []) > MAX_SYMBOLS_PER_FILE:
                    capped_files.append(entry.rel)
                index.apply_parse(
                    rel=entry.rel, lang=entry.lang, loc=_count_loc(Path(entry.abs)),
                    size=entry.size, mtime=entry.mtime, sha1=sha1, parsed=parsed, edges=edges,
                    source=text,
                )
                parsed_files += 1
                if parsed.get("error"):
                    errors.append({"path": entry.rel, "error": parsed["error"]})
            except Exception as exc:  # noqa: BLE001 —— 单文件失败不能让整次扫描失败
                errors.append({"path": entry.rel, "error": f"{type(exc).__name__}: {exc}"})

        missing = [rel for rel in existing if rel not in known]
        if missing:
            index.remove_files(missing)
            removed = len(missing)

        cross_items, cross_changed = graph.rebuild_cross_edges(index)
        summary = walk.summary()
        summary.update(
            {
                "mode": "full" if full else "incremental",
                "parsed": parsed_files,
                "reused": reused,
                "touched": touched,
                "removed": removed,
                "externals": externals,
                "cross_edges": len(cross_items),
                "import_edges_refreshed": import_edges_refreshed,
                "errors": errors[:50],
                # 单文件符号被截断的清单（生成代码/minified）：地图会据此提示"这里不全"
                "symbols_capped": capped_files[:20],
                "symbols_capped_count": len(capped_files),
                "finished_at": now_iso(),
            }
        )
        index.set_meta(META_ROOT_KEY, project.get("root_key") or "")
        if not errors:
            index.set_meta("imports_revision", graph.IMPORTS_REVISION)
        index.set_scan_summary(summary)
        if cross_changed:
            index._bump()  # 跨文件边变了，客户端需要知道图变了
        self.projects.set_scan(project["id"], json.dumps(summary, ensure_ascii=False))
        return {"status": "done", **self.status(project, tree_id), "changes": parsed_files + removed}

    def ensure_fresh(self, project: dict, tree_id: str = "main", *, max_age_s: float = 2.0) -> bool:
        """便宜的新鲜度探测：只比 (size, mtime)，命中就认为索引可用。

        为什么不做常驻 watcher：常驻线程要在每个项目上轮询整个目录，
        而代码模式页面**本来就是按需打开的** —— 打开时校验一次、页面停留时
        低频轮询 `change_seq`，成本远低于守护线程，失效场景也更少。
        """
        import time

        index = self.index(project["id"], tree_id)
        last = index.get_meta("last_scan_at")
        if not last:
            return False
        rows = index.file_rows()
        if any(not row.get("skipped") and not is_strategy_current(str(row.get("lang") or ""), str(row.get("strategy") or "")) for row in rows.values()):
            return False
        try:
            from datetime import datetime

            age = time.time() - datetime.fromisoformat(last).timestamp()
        except ValueError:
            age = max_age_s + 1
        if age < max_age_s:
            return True

        root = Path(project["root_path"])
        if not root.is_dir():
            return False
        walk = walk_project(root)
        seen = {entry.rel for entry in walk.files}
        if seen != set(rows):
            return False
        for entry in walk.files:
            row = rows.get(entry.rel)
            if row is None:
                return False
            if not row.get("skipped") and not is_strategy_current(entry.lang, str(row.get("strategy") or "")):
                return False
            if row["skipped"] or entry.skipped:
                if row["skipped"] != entry.skipped:
                    return False
                continue
            if abs(float(row["mtime"] or 0) - entry.mtime) > 1e-6 or int(row["size"] or 0) != entry.size:
                return False
        index.set_meta("last_scan_at", now_iso())  # 校验通过：续期，避免每次请求都全量比对
        return True

    async def ensure_indexed(self, project: dict, tree_id: str = "main") -> dict:
        """打开项目时调用：没有索引或已失效就扫一遍。"""
        status = self.status(project, tree_id)
        if status["scanning"]:
            return status
        if not status["indexed"] or not self.ensure_fresh(project, tree_id):
            await self.scan(project, tree_id=tree_id)
        return self.status(project, tree_id)

    # ------------------------------------------------------------------
    # 地图
    # ------------------------------------------------------------------
    def map_payload(
        self,
        project: dict,
        *,
        tree_id: str = "main",
        depth: int = 3,
        module: str | None = None,
        kinds: list[str] | None = None,
        with_edges: bool = True,
    ) -> dict:
        """装配地图。depth 控制"画到第几层语义"，是体积控制的主要旋钮。"""
        index = self.index(project["id"], tree_id)
        files = index.file_rows()
        symbols = index.all_symbols(kinds=kinds, limit=MAX_MAP_SYMBOLS + 1)
        truncated_symbols = len(symbols) > MAX_MAP_SYMBOLS
        symbols = symbols[:MAX_MAP_SYMBOLS]

        by_path: dict[str, list[dict]] = {}
        for row in symbols:
            by_path.setdefault(row["path"], []).append(graph.symbol_summary(row))
        for items in by_path.values():
            items.sort(key=lambda s: (s["line"], s["name"]))

        structure = graph.build_structure(
            files, by_path, depth=depth, module=module, root_name=project["name"],
            # 有类型过滤时只保留"含这种符号"的文件：否则过滤后树还是一模一样，
            # 用户会以为没生效（只有展开每个文件才看得到差别）。
            only_paths=set(by_path) if kinds else None,
        )
        edges: dict[str, list] = {"imports": [], "inherits": [], "referenced_by": []}
        edges_truncated = False
        if with_edges:
            for kind in edges:
                rows = index.edges(kind=kind, limit=MAX_MAP_EDGES + 1)
                if len(rows) > MAX_MAP_EDGES:
                    rows = rows[:MAX_MAP_EDGES]
                    edges_truncated = True
                edges[kind] = [
                    {"from": r["src"], "to": r["dst"], "kind": r["kind"], "count": int(r["count"] or 1)}
                    for r in rows
                ]

        summary = index.scan_summary()
        counts = index.counts()
        warnings: list[str] = []
        if truncated_symbols:
            warnings.append(f"符号超过 {MAX_MAP_SYMBOLS} 个，地图已截断：请用搜索或按模块筛选")
        if edges_truncated:
            warnings.append(f"关系边超过 {MAX_MAP_EDGES} 条，已截断：依赖图仅显示部分")
        if summary.get("errors"):
            warnings.append(f"{len(summary['errors'])} 个文件解析失败（已退化为仅文件节点）")
        if summary.get("skipped"):
            skipped = summary["skipped"]
            detail = "、".join(f"{k} {v}" for k, v in skipped.items())
            warnings.append(f"跳过 {detail}（疑似密钥/二进制/超大文件不参与解析）")
        if summary.get("symbols_capped_count"):
            warnings.append(
                f"{summary['symbols_capped_count']} 个文件的符号数超过 "
                f"{MAX_SYMBOLS_PER_FILE}（生成代码？）已截断，地图上这些文件不完整"
            )
        if not files:
            warnings.append("索引为空：请先执行一次扫描")

        return {
            "schema_version": 1,
            "version": index.version(),
            "change_seq": index.max_change_seq(),
            "project": {
                "id": project["id"],
                "name": project["name"],
                "root": project["root_path"],
                "tree_id": tree_id,
            },
            "generated_at": now_iso(),
            "index_refresh": self.refresh_status(project["id"], tree_id),
            "scan": summary,
            "structure": structure,
            "modules": graph.build_modules(index),
            "entrypoints": graph.build_entrypoints(index),
            "routes": graph.build_routes(index),
            "commands": graph.build_commands(index),
            "edges": edges,
            "stats": {
                "files": counts["files"],
                "symbols": counts["symbols"],
                "edges": counts["edges"],
                # 全量构成（项目是什么样），不是本次过滤后的构成——前端图例要的是前者
                "kinds": index.kind_counts(),
                # 本次响应的过滤条件：UI 要能显示"当前按 class 过滤 / 只到 depth=2"
                "filter": {"depth": depth, "module": module or "", "kinds": kinds or []},
                # 诊断概览（详情走 /diagnostics：消息体不该塞进地图载荷）
                "diagnostics": index.diagnostic_summary()["counts"],
                "truncated": truncated_symbols or edges_truncated,
            },
            "warnings": warnings,
        }

    def delta(self, project: dict, after_seq: int, *, tree_id: str = "main") -> dict:
        """自 after_seq 起的变更（地图实时更新用）。

        返回**节点载荷**而不仅是 id：前端可以把增量直接贴到树上，
        不必为了几个符号的变化重拉整张地图。
        """
        index = self.index(project["id"], tree_id)
        rows = index.changes_since(after_seq, limit=MAX_DELTA_CHANGES + 1)
        truncated = len(rows) > MAX_DELTA_CHANGES
        rows = rows[:MAX_DELTA_CHANGES]

        added_ids: list[str] = []
        updated_ids: list[str] = []
        removed_ids: list[str] = []
        files_changed: set[str] = set()
        edges_dirty = False
        for row in rows:
            kind = row["kind"]
            if row["path"]:
                files_changed.add(row["path"])
            if kind == "sym_added":
                added_ids.append(row["node_id"])
            elif kind == "sym_updated":
                updated_ids.append(row["node_id"])
            elif kind in ("sym_removed", "file_removed"):
                removed_ids.append(row["node_id"])
            # 文件级增删改都可能改变 import 边（乃至跨文件边）：
            # 客户端此时应重拉边集，而不是靠猜"哪条边没了"
            if kind in ("index_reset", "file_added", "file_removed", "file_updated"):
                edges_dirty = True

        removed_set = set(removed_ids)
        payload_nodes = [
            graph.symbol_summary(row)
            for row in index.symbols_by_ids([i for i in (added_ids + updated_ids) if i not in removed_set])
        ]
        nodes_by_id = {node["id"]: node for node in payload_nodes}
        return {
            "version": index.version(),
            "from_seq": after_seq,
            "to_seq": index.max_change_seq(),
            "truncated": truncated,
            "edges_dirty": edges_dirty or truncated,
            "added": [nodes_by_id[i] for i in added_ids if i in nodes_by_id],
            "updated": [nodes_by_id[i] for i in updated_ids if i in nodes_by_id],
            "removed": sorted(removed_set),
            "files": sorted(files_changed),
            "changes": [
                {"kind": r["kind"], "node_id": r["node_id"], "path": r["path"], "ts": r["ts"]} for r in rows
            ],
        }

    def symbol_detail(self, project: dict, sid: str, *, tree_id: str = "main") -> dict:
        index = self.index(project["id"], tree_id)
        row = index.symbol(sid)
        if row is None:
            raise KeyError(f"符号不存在：{sid}")
        detail = graph.symbol_detail(row)
        rel = row["path"]
        detail["importers"] = [
            e["src"] for e in index.edges(kind="referenced_by", dst=sid)
        ] + [e["src"] for e in index.edges(kind="imports", dst=file_id(rel))]
        detail["members"] = [
            graph.symbol_summary(child)
            for child in index.query_all("SELECT * FROM symbols WHERE parent=? ORDER BY line", (sid,))
        ]
        detail["importers"] = sorted(set(detail["importers"]))[:40]
        detail["file"] = {
            "path": rel,
            "sha1": (index.file_row(rel) or {}).get("sha1", ""),
            "lang": (index.file_row(rel) or {}).get("lang", ""),
        }
        return detail

    def text_slice(self, project: dict, rel: str, start: int, end: int) -> str:
        """按行区间取源码（符号详情里给模型看的实现片段，走项目边界校验）。"""
        root = Path(project["root_path"])
        path = safe_child(root, rel)
        if not path.is_file():
            raise ValueError(f"文件不存在：{rel}")
        first = max(1, int(start))
        last = max(first, int(end))
        out: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh, start=1):
                if idx < first:
                    continue
                if idx > last:
                    break
                out.append(line.rstrip("\n").rstrip("\r"))
        return "\n".join(out)

    def digest(self, project: dict, *, tree_id: str = "main", max_chars: int = 6000) -> str:
        return graph.build_digest(self.index(project["id"], tree_id), project, max_chars=max_chars)

    # ------------------------------------------------------------------
    # 代码模式上下文（P2）：会话级规程+地图 / 消息级结构坐标
    # ------------------------------------------------------------------
    def session_context(self, project: dict, *, tree_id: str = "main") -> tuple[int, str]:
        """返回 (索引版本, 会话级附加文本)，仅索引或项目元数据变化时重建。"""
        from code_agent.codeintel import context as ctx_mod

        project = dict(project)
        tree_id = tree_id or "main"
        key = f"{project['id']}:{tree_id}"
        # 摘要只读取项目名和根路径；扫描时间等状态元数据不应使缓存失效。
        project_key = (str(project.get("name")), str(project.get("root_path")))
        index = self.index(project["id"], tree_id)
        # 同一索引的并发请求只构建一次，并让版本号与正文来自同一快照。
        # 不在此处持服务 _guard：关闭路径持 _guard 后取索引锁，反向取锁会死锁。
        with index.read_snapshot() as revision:
            with self._session_context_guard:
                cached = self._session_context_cache.get(key)
            if cached is not None and cached[:3] == (index, revision, project_key):
                return revision[0], cached[3]
            text = ctx_mod.session_suffix(index, project)
            with self._session_context_guard:
                self._session_context_cache[key] = (index, revision, project_key, text)
            return revision[0], text

    def message_context(
        self, project: dict, code_context: dict | None, *, tree_id: str = "main"
    ) -> str:
        if not code_context:
            return ""
        from code_agent.codeintel import context as ctx_mod

        try:
            return ctx_mod.message_block(self, project, code_context, tree_id=tree_id)
        except Exception:
            # 上下文块只是"锦上添花"：拼不出来不能影响这一轮对话
            return ""

    # ------------------------------------------------------------------
    # 诊断（M18 · P3）：语言服务器 → 索引 → 地图角标
    # ------------------------------------------------------------------
    def record_diagnostics(
        self, project: dict, uri: str, items: list[dict], *, tree_id: str = "main"
    ) -> int:
        """把一次 publishDiagnostics 落进索引。

        uri 是 file:// 形式（由服务端下发给前端、前端原样回传）；越界或非项目内的
        URI 直接丢弃——诊断里带着源码位置与消息，不能让任意路径写进索引。
        """
        from code_agent.codeintel.uri import uri_to_rel

        rel = uri_to_rel(uri, project["root_path"])
        if rel is None:
            return 0
        index = self.index(project["id"], tree_id)
        attribute = self._attributor(index, project, rel)
        enriched: list[dict] = []
        for item in items:
            line = int(item.get("line") or 1)
            enriched.append({**item, "symbol_id": attribute(line)})
        return index.set_diagnostics(rel=rel, uri=uri, items=enriched)

    def _attributor(self, index: SymbolIndex, project: dict, rel: str):
        """返回 line → symbol_id 的归属函数。

        索引里还没有这个文件的符号时（刚写完、扫描还没跑到），**当场解析一次**：
        否则诊断会"挂在文件上但挂不到类/函数上"，地图角标就少了最重要的一层信息。
        按需解析出的符号 id 与扫描器之后写入的完全一致（id 由路径 + 限定名决定）。
        """
        rows = index.symbols_for(rel)
        if rows:
            return lambda line: index.symbol_at(rel, line)

        spans: list[tuple[int, int, str]] = []
        try:
            path = safe_child(Path(project["root_path"]), rel)
            text = path.read_text(encoding="utf-8", errors="replace")
            parsed = parse_file(rel, detect_language(path), text)
            spans = [
                (int(s.get("line") or 0), int(s.get("end_line") or s.get("line") or 0), s["id"])
                for s in parsed.get("symbols", [])
                if s.get("line")
            ]
        except Exception:  # noqa: BLE001 —— 归属失败不该影响诊断落库
            spans = []

        def _attribute(line: int) -> str:
            best: tuple[int, int, str] | None = None
            for start, end, sid in spans:
                if start <= line <= end and (best is None or (end - start) < (best[1] - best[0])):
                    best = (start, end, sid)
            return best[2] if best else ""

        return _attribute

    def diagnostics(
        self,
        project: dict,
        *,
        tree_id: str = "main",
        path: str | None = None,
        severity: int | None = None,
        limit: int = 200,
        details: bool = False,
    ) -> dict:
        index = self.index(project["id"], tree_id)
        summary = index.diagnostic_summary()
        payload = {
            "seq": summary["seq"],
            "counts": summary["counts"],
            "files": summary["files"],
            "symbols": summary["symbols"],
            "items": [],
        }
        limit = max(1, min(int(limit), 1000))
        if details:
            rows = index.diagnostics(path=path, severity=severity, limit=limit + 1)
            payload["items"] = [
                {
                    **row,
                    "column": max(0, int(row.get("character") or 0)) + 1,
                    "end_column": max(0, int(row.get("end_character") or 0)) + 1,
                }
                for row in rows[:limit]
            ]
            severity_name = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(severity)
            filtered_counts = summary["files"].get(path, {}) if path else summary["counts"]
            payload["total"] = (
                int(filtered_counts.get(severity_name, 0)) if severity_name else sum(filtered_counts.values())
            )
            payload["truncated"] = len(rows) > limit
        if path:
            payload["files_detail"] = {
                path: payload["items"] if details else index.diagnostics(path=path, severity=severity, limit=limit)
            }
        return payload

    # ------------------------------------------------------------------
    # 保真度校验
    # ------------------------------------------------------------------
    def verify(self, project: dict, *, tree_id: str = "main", limit_files: int = 4000) -> dict:
        """重解析磁盘文件并与索引比对（**不写入**），返回保真度。

        这是"地图完全映射代码"的可测形式：drift 为 0 时 fidelity = 1.0。
        只比较、不写入是刻意的——会自我修复的校验器证明不了任何事。
        """
        index = self.index(project["id"], tree_id)
        root = Path(project["root_path"])
        walk = walk_project(root)
        rows = index.file_rows()
        checked = 0
        drift: list[dict] = []

        for entry in walk.files:
            if entry.skipped:
                continue
            row = rows.get(entry.rel)
            if row is None:
                drift.append({"path": entry.rel, "reason": "索引缺失"})
                continue
            if checked >= limit_files:
                break
            checked += 1
            try:
                sha1 = _sha1_file(Path(entry.abs))
            except OSError as exc:
                drift.append({"path": entry.rel, "reason": f"读取失败：{exc}"})
                continue
            if sha1 != row["sha1"]:
                drift.append({"path": entry.rel, "reason": "内容已变化（索引过期）"})
                continue
            # 内容一致：再抽查符号集合是否与磁盘一致（解析一次，比对 id 集合）
            text = Path(entry.abs).read_text(encoding="utf-8", errors="replace")
            parsed = parse_file(entry.rel, entry.lang, text)
            disk_ids = {s["id"] for s in parsed.get("symbols", [])}
            index_ids = {s["id"] for s in index.symbols_for(entry.rel)}
            if disk_ids != index_ids:
                missing = len(disk_ids - index_ids)
                stale = len(index_ids - disk_ids)
                drift.append(
                    {"path": entry.rel, "reason": f"符号不一致（磁盘多 {missing} / 索引多 {stale}）"}
                )

        absent = [rel for rel in rows if rel not in {e.rel for e in walk.files}]
        for rel in absent[:200]:
            drift.append({"path": rel, "reason": "文件已不存在"})
        total = checked + len(absent)
        fidelity = 1.0 if total == 0 else max(0.0, (total - len(drift)) / total)
        return {
            "ok": not drift,
            "fidelity": round(fidelity, 4),
            "checked": checked,
            "drift": drift[:100],
            "drift_count": len(drift),
            "version": index.version(),
        }
