"""LSP 代理（M18 · P3）：把语言服务器的能力接进编辑器和地图。

职责与边界：
- **只做代理**，不实现语言智能。补全/跳转/悬停由语言服务器回答，我们负责握手、
  文档同步（didOpen/didChange/didSave/didClose）、请求转发与通知扇出。
- **诊断进索引**：`textDocument/publishDiagnostics` 落到项目索引的 diagnostics 表，
  于是地图节点能显示"哪块编译不过"——这是 LSP 对"看框架不看实现"最大的增量。
- **失败可见**：命令找不到、握手超时、进程立刻退出，都要在状态接口里如实说明原因
  （含 stderr 尾部），而不是让前端表现为"补全没反应"。
- **资源有界**：同一 (项目, 工作树, 语言) 只起一个进程；空闲超时回收；
  总并发有上限，只回收没有活跃连接的空闲进程。

本模块不自行推断语言语义，也不直接写文件。跨文件和资源编辑建议交给前端预览，
文件创建、移动和删除由独立的 workspace_refactor 审查批次执行；服务器命令只允许
已接入编辑预览的白名单，Java 调试命令通过可信后端回调调用。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from code_agent.codeintel import lsp_catalog
from code_agent.codeintel.documentation_translation import DocumentationTranslator, needs_translation
from code_agent.codeintel.documentation_translation_store import DocumentationTranslationStore
from code_agent.codeintel.documentation_prewarm import DocumentationPrewarmer, apply_changes
from code_agent.codeintel.lsp_localization import localize_result, localize_notification, localize_error, restore_protocol_text
from code_agent.codeintel.interpreter import interpreter_info
from code_agent.codeintel.uri import normalize_uri, uri_to_path, uri_to_rel
from code_agent.extensions.jsonrpc_stdio import JsonRpcError, JsonRpcStdio

#: 空闲回收（秒）与并发上限
IDLE_TIMEOUT = float(os.environ.get("CODE_AGENT_LSP_IDLE", "600"))
MAX_SESSIONS = int(os.environ.get("CODE_AGENT_LSP_MAX", "3"))
#: initialize 超时：rust-analyzer/jdtls 首次索引会慢，给足
INIT_TIMEOUT = float(os.environ.get("CODE_AGENT_LSP_INIT_TIMEOUT", "45"))
DOCUMENTATION_RETRY_DELAY = 60.0


def notification_needs_translation(message: dict) -> bool:
    """Inspect only display prose, never protocol data, edits or identifiers."""
    method, params = message.get("method"), message.get("params") or {}
    text = []
    if method == "textDocument/publishDiagnostics":
        for diagnostic in params.get("diagnostics") or []:
            text.append(diagnostic.get("message", ""))
            text.extend(item.get("message", "") for item in diagnostic.get("relatedInformation") or [])
    elif method == "$/progress":
        value = params.get("value") or {}
        if isinstance(value, dict):
            text.extend([value.get("title", ""), value.get("message", "")])
    elif method == "code-agent/reviewEdit":
        text.append(params.get("label", ""))
    else:
        text.append(params.get("message", ""))
    return needs_translation(text, single_words=True)


def client_capabilities() -> dict[str, Any]:
    """Capabilities implemented by the editor/bridge, without dynamic registration."""
    return {
        "textDocument": {
            "synchronization": {"didSave": True, "willSave": False, "willSaveWaitUntil": False,
                                "dynamicRegistration": False},
            "completion": {
                "dynamicRegistration": False, "contextSupport": True,
                "completionItem": {"snippetSupport": True, "documentationFormat": ["markdown", "plaintext"],
                                   "insertReplaceSupport": True, "labelDetailsSupport": True,
                                   "commitCharactersSupport": True, "deprecatedSupport": True, "preselectSupport": True,
                                   "resolveSupport": {"properties": ["documentation", "detail", "additionalTextEdits"]}},
                "completionList": {"itemDefaults": ["commitCharacters", "editRange", "insertTextFormat", "insertTextMode", "data"]},
            },
            "hover": {"contentFormat": ["markdown", "plaintext"]},
            "signatureHelp": {"contextSupport": True, "signatureInformation": {
                "documentationFormat": ["markdown", "plaintext"],
                "parameterInformation": {"labelOffsetSupport": True}, "activeParameterSupport": True}},
            "definition": {"linkSupport": True}, "declaration": {"linkSupport": True},
            "typeDefinition": {"linkSupport": True}, "implementation": {"linkSupport": True},
            "references": {},
            "callHierarchy": {"dynamicRegistration": False},
            "typeHierarchy": {"dynamicRegistration": False},
            "codeLens": {"dynamicRegistration": False},
            "inlayHint": {"dynamicRegistration": False, "resolveSupport": {
                "properties": ["tooltip", "textEdits", "label.tooltip", "label.location"]}},
            "publishDiagnostics": {"relatedInformation": True, "versionSupport": True,
                                   "tagSupport": {"valueSet": [1, 2]}, "dataSupport": True},
            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
            "semanticTokens": {
                "dynamicRegistration": False,
                "requests": {"range": True, "full": True},
                "tokenTypes": ["namespace", "type", "class", "enum", "interface", "struct",
                               "typeParameter", "parameter", "variable", "property", "enumMember",
                               "event", "function", "method", "macro", "keyword", "modifier", "comment",
                               "string", "number", "regexp", "operator", "decorator",
                               "annotationMember", "record", "recordComponent"],
                "tokenModifiers": ["declaration", "definition", "readonly", "static", "deprecated",
                                   "abstract", "async", "modification", "documentation", "defaultLibrary",
                                   "public", "private", "protected", "native", "generic", "typeArgument",
                                   "importDeclaration", "constructor"],
                "formats": ["relative"], "overlappingTokenSupport": False,
                "multilineTokenSupport": False, "serverCancelSupport": True,
                "augmentsSyntaxTokens": True,
            },
            "codeAction": {"codeActionLiteralSupport": {"codeActionKind": {"valueSet": [
                "", "quickfix", "refactor", "refactor.extract", "refactor.inline", "refactor.rewrite",
                "source", "source.organizeImports", "source.fixAll"]}},
                "isPreferredSupport": True, "disabledSupport": True, "dataSupport": True,
                "resolveSupport": {"properties": ["edit"]}},
            "rename": {"prepareSupport": True}, "formatting": {}, "rangeFormatting": {},
        },
        "workspace": {"configuration": True, "workspaceFolders": True, "applyEdit": True,
                      "workspaceEdit": {"documentChanges": True, "resourceOperations": ["create", "rename", "delete"], "failureHandling": "transactional"},
                      "codeLens": {"refreshSupport": True}, "inlayHint": {"refreshSupport": True},
                      "symbol": {}},
        "general": {"positionEncodings": ["utf-16"]},
    }


#: 默认分析设置：语言服务器（尤其 pyright）默认往往"不做类型检查"，
#: 不显式下发就只会收到空诊断——地图上什么都看不到，用户以为坏了。
#: 用户可在 lsp.json 的 `initializationOptions` 里覆盖任意字段。
DEFAULT_SETTINGS: dict[str, Any] = {
    "java": {"referencesCodeLens": {"enabled": True}, "implementationsCodeLens": {"enabled": True}},
    "python": {
        "analysis": {
            "typeCheckingMode": "basic",
            "diagnosticMode": "openFilesOnly",
            "autoSearchPaths": True,
            "useLibraryCodeForTypes": True,
            "reportMissingImports": "warning",
            "reportUnusedVariable": "warning",
        }
    },
    "pyright": {
        "disableLanguageServices": False,
        "openFilesOnly": True,
    },
    "typescript": {
        "inlayHints": {"parameterNames": {"enabled": False}},
    },
}


def _deep_merge(base: dict, extra: dict) -> dict:
    """深合并（用户设置覆盖默认；不修改入参）。"""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _section_of(settings: dict, section: str) -> Any:
    node: Any = settings
    for part in (section or "").split("."):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node


def _root_uri(root: Path) -> str:
    from code_agent.codeintel.uri import file_uri

    return file_uri(root)


@dataclass
class LspSession:
    """一个语言服务器进程 + 文档状态。"""

    key: str
    server_id: str
    lang: str
    project_id: str
    tree_id: str
    root: Path
    command: str
    args: list[str]
    client: JsonRpcStdio
    capabilities: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, int] = field(default_factory=dict)   # 我们发出的原始 uri → version
    #: 归一化 uri → 我们发出的原始 uri。语言服务器回传的 uri 可能被它自己改写过
    #: （真机上 pyright 把小写盘符 + 编码冒号回传），必须靠归一化认领回去。
    uri_index: dict[str, str] = field(default_factory=dict)
    diagnostics: dict[str, list[dict]] = field(default_factory=dict)   # 原始 uri → items
    diagnostic_versions: dict[str, int | None] = field(default_factory=dict)
    closed_documents: set[str] = field(default_factory=set)
    listeners: set[Callable[[dict], None]] = field(default_factory=set)
    environment: dict[str, Any] = field(default_factory=dict)
    document_hashes: dict[str, str] = field(default_factory=dict)
    document_texts: dict[str, str] = field(default_factory=dict)
    foreground_documents: set[str] = field(default_factory=set)
    localization_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    notification_revisions: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    error: str = ""
    published: int = 0
    initialization_ms: float = 0.0
    #: 下发给语言服务器的设置（对应 workspace/configuration 的应答）
    settings: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def alive(self) -> bool:
        return self.client.alive

    def supports(self, capability: str) -> bool:
        value = self.capabilities.get(capability)
        return value is not None and value is not False  # An empty options object still declares support.

    def summary(self) -> dict:
        return {
            "key": self.key,
            "server": self.server_id,
            "environment": self.environment,
            "capabilities": self.capabilities,
            "initialization_ms": self.initialization_ms,
            "lang": self.lang,
            "alive": self.alive,
            "documents": len(self.documents),
            "diagnostics": sum(len(v) for v in self.diagnostics.values()),
            "published": self.published,
            # 语言服务器的 stderr 尾部：连不上/报错时这是唯一的线索
            "stderr": self.client.stderr_tail(limit=3),
            "error": self.error,
            "idle_s": round(time.monotonic() - self.last_used, 1),
        }


class LspManager:
    """语言服务器会话池。"""

    def __init__(
        self,
        projects: Any = None,
        *,
        intel: Any = None,
        config_path: Path | None = None,
        on_diagnostics: Callable[[dict, str, list[dict]], None] | None = None,
        documentation_path: Path | None = None,
    ) -> None:
        self.projects = projects
        self.intel = intel
        self._config_path = config_path
        self._sessions: dict[str, LspSession] = {}
        # Reserve capacity before starting a process. Slow initialize/stop calls
        # never hold the state lock, and concurrent callers share one startup.
        self._starting: dict[str, asyncio.Task[LspSession]] = {}
        self._starting_info: dict[str, dict[str, Any]] = {}
        self._stopping: dict[str, asyncio.Task[None]] = {}
        self._guard = asyncio.Lock()
        self._generation = 0
        self._closing = False
        self._shutdown: asyncio.Task[None] | None = None
        #: 最近一次失败原因（id → 原因），状态接口会显示出来
        self.failures: dict[str, str] = {}
        self._on_diagnostics = on_diagnostics
        # Reuse the configured description model without owning its connection
        # pool. Signatures and examples remain language-server evidence.
        def documentation_provider():
            factory = getattr(self.intel, "brief_provider_factory", None)
            return factory() if callable(factory) else None

        self.documentation_translator = DocumentationTranslator(
            documentation_provider, store=DocumentationTranslationStore(documentation_path))
        self.documentation_prewarm = DocumentationPrewarmer(self, documentation_path)
        self._documentation_jobs: dict[tuple[str, str], asyncio.Task] = {}
        self._documentation_progress: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------------
    # 配置与状态
    # ------------------------------------------------------------------
    def config(self) -> dict[str, dict[str, Any]]:
        return lsp_catalog.load_config(self._config_path)

    def servers(self, *, languages: dict[str, int] | None = None, present: set[str] | None = None,
                roots: tuple[Path, ...] = ()) -> list[dict]:
        return lsp_catalog.detect(
            languages=languages, present_files=present, config=self.config(), roots=roots
        )

    def status(self) -> dict:
        return {
            "sessions": [s.summary() for s in self._sessions.values()],
            "starting": [dict(value) for value in self._starting_info.values()],
            "failures": dict(self.failures),
            "max_sessions": MAX_SESSIONS,
            "idle_timeout": IDLE_TIMEOUT,
        }

    def documentation_status(self, project_id=None, tree_id=None) -> dict:
        return {"language": "zh-CN", "persistent": self.documentation_translator.store.path is not None,
                "cache": self.documentation_translator.stats(),
                "prewarm": self.documentation_prewarm.status(project_id, tree_id),
                "workspaces": [dict(value) for key, value in self._documentation_progress.items()
                               if (project_id is None or key[0] == project_id)
                               and (tree_id is None or key[1] == tree_id)],
                "workspace_running": any(not task.done() for key, task in self._documentation_jobs.items()
                                         if (project_id is None or key[0] == project_id)
                                         and (tree_id is None or key[1] == tree_id))}

    def start_documentation_prewarm(self, project: dict, *, tree_id="main") -> dict:
        key = (project["id"], tree_id)
        if key not in self._documentation_jobs or self._documentation_jobs[key].done():
            self._documentation_progress[key] = {"project_id": project["id"], "tree": tree_id,
                "status": "queued", "total_files": 0, "processed_files": 0, "unavailable_languages": []}
            self._documentation_jobs[key] = asyncio.create_task(self._prewarm_project(project, tree_id))
            self._documentation_jobs[key].add_done_callback(self._observe_task)
        return self.documentation_status(*key)

    async def _prewarm_project(self, project, tree_id):
        from code_agent.codeintel.scanner import walk_project
        from code_agent.codeintel.uri import file_uri

        root = Path(project["root_path"])
        walk = await asyncio.to_thread(walk_project, root)
        progress = self._documentation_progress[(project["id"], tree_id)]
        progress.update(status="running", total_files=len(walk.files), truncated=walk.truncated)
        # The current editor is already queued by didOpen. Stream the remaining
        # files one at a time; do not retain a project's source tree in memory.
        sessions = {}
        for entry in sorted(walk.files, key=lambda value: (value.lang, value.rel)):
            if self._closing:
                return
            if entry.skipped or entry.size > 2 * 1024 * 1024:
                progress["processed_files"] += 1
                continue
            if entry.lang not in sessions or sessions[entry.lang] is not None and not sessions[entry.lang].alive:
                definitions = await asyncio.to_thread(self.servers_for_language, entry.lang, project)
                installed = None
                for definition in definitions:
                    if await asyncio.to_thread(lsp_catalog.resolve_command, definition["command"], root, root.parent):
                        installed = definition
                        break
                try:
                    sessions[entry.lang] = await self.session_for(project, entry.lang, tree_id=tree_id,
                                                                 server_id=installed["id"]) if installed else None
                except (JsonRpcError, OSError):
                    sessions[entry.lang] = None
                if sessions[entry.lang] is None:
                    progress["unavailable_languages"].append(entry.lang)
            session = sessions[entry.lang]
            if session is None or not session.alive or not session.supports("hoverProvider"):
                progress["processed_files"] += 1
                continue
            uri = file_uri(root / entry.rel)
            uri = session.uri_index.get(normalize_uri(uri), uri)
            background = uri not in session.documents
            if background:
                try:
                    text = await asyncio.to_thread((root / entry.rel).read_text, encoding="utf-8", errors="replace")
                    await self.open_document(session, uri, text, lang_id=entry.lang, background=True)
                except (OSError, JsonRpcError):
                    continue
            else:
                self.documentation_prewarm.schedule(session, uri, session.document_texts.get(uri), force=True)
            while session.alive:
                state = self.documentation_prewarm.states.get((id(session), uri))
                if state is None or state["status"] in ("ready", "partial", "cancelled"):
                    break
                await asyncio.sleep(0.25)
            if background and uri not in session.foreground_documents:
                await self.close_document(session, uri)
            progress["processed_files"] += 1
        progress["status"] = "completed"

    async def servers_for_project(self, project: dict) -> list[dict]:
        """带上"项目里实际有哪些语言/关键文件"的探测结果。"""
        languages: dict[str, int] = {}
        present: set[str] = set()
        root = Path(project["root_path"])
        if self.intel is not None:
            try:
                index = self.intel.index(project["id"])
                for row in index.query_all(
                    "SELECT lang, COUNT(*) AS n FROM files WHERE skipped='' GROUP BY lang"
                ):
                    languages[row["lang"]] = int(row["n"])
                present = {
                    Path(r["path"]).name
                    for r in index.query_all("SELECT path FROM files WHERE path NOT LIKE '%/%'")
                }
            except Exception:
                pass
        if not present:
            with contextlib.suppress(OSError):
                present = {p.name for p in root.iterdir() if p.is_file()}
                languages = languages or {}
        # 探测是**同步 CPU 活儿**（Windows 上要遍历 PATH × PATHEXT 找命令，冷启动约 2-3 秒）。
        # 直接在这里跑会冻住整个事件循环（流式、轮询、其它请求全部排队），所以丢到线程里。
        return await asyncio.to_thread(
            self.servers, languages=languages, present=present, roots=(root, root.parent)
        )

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------
    async def session_for(
        self, project: dict, lang: str, *, tree_id: str = "main", server_id: str | None = None,
    ) -> LspSession:
        """取（或启动）某项目某语言的会话。失败抛 JsonRpcError（带原因）。"""
        generation = self._generation
        definitions = await asyncio.to_thread(self.servers_for_language, lang, project)
        if server_id:
            definitions = [e for e in definitions if e["id"] == server_id]
        if not definitions:
            raise JsonRpcError(f"没有为 {lang} 配置语言服务器" + (f"：{server_id} 未启用或语言不匹配" if server_id else ""))
        entry = definitions[0]
        key = f"{project['id']}:{tree_id}:{lang}:{entry['id']}"
        while True:
            retiring = None
            async with self._guard:
                if self._closing or generation != self._generation:
                    raise JsonRpcError("语言服务会话已关闭，请重新连接")
                existing = self._sessions.get(key)
                if existing is not None and existing.alive and not existing.error:
                    existing.touch()
                    return existing
                task = self._starting.get(key)
                if task is None:
                    if key in self._stopping:
                        retiring = self._stopping[key]
                    elif existing is not None:  # Dead processes still need pipe cleanup.
                        retiring = self._retire_locked(existing)
                    elif len(self._sessions) + len(self._starting) + len(self._stopping) >= MAX_SESSIONS:
                        # Let an already exiting process free its reservation first.
                        if self._stopping:
                            retiring = next(iter(self._stopping.values()))
                        else:
                            candidates = [s for s in self._sessions.values() if not s.listeners]
                            if not candidates:
                                raise JsonRpcError(
                                    f"语言服务并发上限 {MAX_SESSIONS} 已满；当前服务正在使用或启动中，请关闭其它代码工作区后重试"
                                )
                            retiring = self._retire_locked(min(candidates, key=lambda s: s.last_used))
                    else:
                        task = asyncio.create_task(self._start_reserved(project, lang, tree_id, key, entry, generation))
                        self._starting[key] = task
                        self._starting_info[key] = {
                            "key": key, "server": entry["id"], "lang": lang, "state": "starting",
                        }
                        # A disconnected caller must not leave an unobserved failure.
                        task.add_done_callback(lambda finished, session_key=key: self._start_finished(session_key, finished))
            if retiring is not None:
                await asyncio.shield(retiring)
                continue
            # One disconnected WebSocket cannot cancel another caller's startup.
            return await asyncio.shield(task)

    def _start_finished(self, key: str, task: asyncio.Task) -> None:
        # A task cancelled before its first execution never enters its finally
        # block. This callback also releases that reservation. State mutations
        # under _guard do not suspend, so a callback cannot interleave them.
        if self._starting.get(key) is task:
            self._starting.pop(key, None)
            self._starting_info.pop(key, None)
        self._observe_task(task)

    @staticmethod
    def _observe_task(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _start_reserved(
        self, project: dict, lang: str, tree_id: str, key: str, entry: dict, generation: int,
    ) -> LspSession:
        session = None
        task = asyncio.current_task()
        try:
            session = await self._start(project, lang, tree_id, key, entry)
            async with self._guard:
                if self._closing or generation != self._generation:
                    raise asyncio.CancelledError
                self._sessions[key] = session
                self._starting.pop(key, None)
                self._starting_info.pop(key, None)
            return session
        except BaseException:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.client.stop()
                if session.alive:
                    session.error = "初始化已取消，但语言服务进程未退出"
                    async with self._guard:
                        self._sessions[key] = session
            raise
        finally:
            async with self._guard:
                if self._starting.get(key) is task:
                    self._starting.pop(key, None)
                    self._starting_info.pop(key, None)

    def _retire_locked(self, session: LspSession) -> asyncio.Task[None]:
        """Remove a session, keeping its capacity occupied until its process exits."""
        self._sessions.pop(session.key, None)
        task = asyncio.create_task(self._retire(session))
        self._stopping[session.key] = task
        task.add_done_callback(self._observe_task)
        return task

    async def _retire(self, session: LspSession) -> None:
        failure = ""
        try:
            self.documentation_prewarm.cancel(session)
            for pending in list(session.localization_tasks.values()):
                pending.cancel()
            await asyncio.gather(*session.localization_tasks.values(), return_exceptions=True)
            session.localization_tasks.clear()
            try:
                await session.client.stop()
            except Exception as exc:
                failure = str(exc)
            if not session.alive:
                for uri in session.diagnostics:
                    self._sync_diagnostics(session, uri, include_current=False)
        finally:
            async with self._guard:
                if session.alive:
                    session.error = failure or "语言服务进程未退出"
                    self._sessions[session.key] = session
                if self._stopping.get(session.key) is asyncio.current_task():
                    self._stopping.pop(session.key, None)
        if session.alive:
            raise JsonRpcError("语言服务进程未退出，无法释放并发容量")

    async def _start(self, project: dict, lang: str, tree_id: str, key: str, entry: dict) -> LspSession:
        root = Path(project["root_path"])
        resolved = await asyncio.to_thread(lsp_catalog.resolve_command, entry["command"], root, root.parent)
        if not resolved:
            self.failures[entry["id"]] = f"未安装：{entry['install_hint'] or entry['command']}"
            raise JsonRpcError(
                f"语言服务器 {entry['name']} 未安装（{entry['command']}）。"
                f"安装方式：{entry['install_hint'] or '见 lsp.json'}"
            )
        launch_args = list(entry["args"])
        launch_command = resolved
        launch_environment: dict[str, Any] = {}
        if entry["id"] == "jdtls":
            from code_agent.codeintel.java_lsp_launch import jdtls_process_command, prepare_jdtls_launch

            launch_args, launch_environment = await asyncio.to_thread(
                prepare_jdtls_launch, project, tree_id, entry, resolved,
            )
            launch_command, launch_args = await asyncio.to_thread(jdtls_process_command, resolved, launch_args)
        client = JsonRpcStdio(
            launch_command,
            launch_args,
            framing="content-length",
            cwd=str(root),
            env=entry.get("env") or None,
            name=f"lsp:{entry['id']}",
        )
        session = LspSession(
            key=key, server_id=entry["id"], lang=lang, project_id=project["id"], tree_id=tree_id,
            root=root, command=launch_command, args=launch_args, client=client,
            environment=launch_environment,
        )
        session.settings = _deep_merge(DEFAULT_SETTINGS, entry.get("settings") or {})
        initial = entry.get("initialization_options") or {}
        if isinstance(initial.get("settings"), dict):
            session.settings = _deep_merge(session.settings, initial["settings"])
        if lang == "python":
            session.environment = await asyncio.to_thread(interpreter_info, root)
            session.settings = _deep_merge(session.settings, {
                "python": {"pythonPath": session.environment["python"]},
                "pylsp": {"plugins": {"jedi": {"environment": session.environment["python"]}}},
            })
        init_options = {"settings": session.settings}
        if entry.get("initialization_options"):
            init_options = _deep_merge(init_options, entry["initialization_options"])
        init_options["settings"] = session.settings
        if entry["id"] == "jdtls":
            from code_agent.codeintel.java_debug import debug_bundles
            bundles = await asyncio.to_thread(debug_bundles)
            configured = init_options.get("bundles") or []
            if not isinstance(configured, list) or any(not isinstance(bundle, str) for bundle in configured):
                raise JsonRpcError("Java initializationOptions.bundles 必须是路径列表")
            init_options["bundles"] = list(dict.fromkeys([*configured, *bundles]))
            session.environment["java_debug_bundles"] = bundles
        client.on_notification = lambda msg: self._on_notification(session, msg)
        client.on_request = lambda msg: self._on_server_request(session, msg)
        def closed() -> None:
            self.failures.setdefault(entry["id"], "服务器进程已退出")
            self.documentation_prewarm.cancel(session)
            for pending in list(session.localization_tasks.values()):
                pending.cancel()
            for uri in session.diagnostics:
                self._sync_diagnostics(session, uri, include_current=False)
        client.set_on_closed(closed)
        initialized_at = time.monotonic()
        try:
            await client.start()
            result = await client.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "clientInfo": {"name": "code-agent", "version": "0.10.0"},
                    "rootUri": _root_uri(root),
                    "workspaceFolders": [{"uri": _root_uri(root), "name": root.name}],
                    "capabilities": client_capabilities(),
                    "initializationOptions": init_options,
                },
                timeout=INIT_TIMEOUT,
            )
            session.capabilities = (result or {}).get("capabilities") or {}
            await client.notify("initialized", {})
            # 关键一步：显式下发分析设置。语言服务器（pyright 尤其）不收到设置
            # 时会按"最保守"的默认跑，结果是 publishDiagnostics 全是空数组——
            # 表现为"LSP 连上了但地图/编辑器一个错误都没有"。
            with contextlib.suppress(JsonRpcError):
                await client.notify("workspace/didChangeConfiguration", {"settings": session.settings})
            self.failures.pop(entry["id"], None)
            session.initialization_ms = round((time.monotonic() - initialized_at) * 1000, 1)
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                session.error = str(exc)
                self.failures[entry["id"]] = str(exc)
            with contextlib.suppress(Exception):
                await client.stop()
            if client.alive:
                # Failed cleanup must not silently release a live process slot.
                session.error = session.error or "初始化已取消，但语言服务进程未退出"
                async with self._guard:
                    self._sessions[key] = session
            if isinstance(exc, (JsonRpcError, OSError)):
                raise JsonRpcError(str(exc)) from exc
            raise
        return session

    def servers_for_language(self, lang: str, project: dict) -> list[dict]:
        root = Path(project["root_path"])
        entries = [
            e for e in lsp_catalog.merged_servers(self.config())
            if e["enabled"] and lang in e["languages"]
        ]
        # 优先用"已经装好的"服务器；都没装时列表顺序保持不变（错误信息里会给出安装提示）
        entries.sort(key=lambda e: 0 if lsp_catalog.resolve_command(e["command"], root, root.parent) else 1)
        return entries

    async def stop(self, key: str) -> None:
        # A manual stop must also stop the project harvester, otherwise its
        # next file would immediately recreate the server being stopped.
        jobs = []
        for identity, task in self._documentation_jobs.items():
            if key.startswith(f"{identity[0]}:{identity[1]}:") and not task.done():
                task.cancel()
                jobs.append(task)
                self._documentation_progress[identity]["status"] = "stopped"
        async with self._guard:
            starting = self._starting.get(key)
            if starting is not None and not starting.cancelling():
                starting.cancel()
            session = self._sessions.get(key)
            retiring = self._retire_locked(session) if session is not None else self._stopping.get(key)
        pending = [*jobs, *(task for task in (starting, retiring) if task is not None)]
        if pending:
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
        remaining = self._sessions.get(key)
        if remaining is not None and remaining.alive:
            raise JsonRpcError("语言服务进程未退出，请稍后重试关闭")

    async def stop_all(self) -> None:
        async with self._guard:
            task = self._shutdown
            if task is None:
                self._closing = True
                self._generation += 1
                starting = list(self._starting.values())
                for pending in starting:
                    if not pending.cancelling():
                        pending.cancel()
                for session in list(self._sessions.values()):
                    self._retire_locked(session)
                task = asyncio.create_task(self._finish_shutdown([*starting, *self._stopping.values()]))
                self._shutdown = task
                task.add_done_callback(self._observe_task)
        await asyncio.shield(task)

    async def _finish_shutdown(self, pending: list[asyncio.Task]) -> None:
        try:
            for job in self._documentation_jobs.values():
                job.cancel()
            await asyncio.gather(*self._documentation_jobs.values(), return_exceptions=True)
            self._documentation_jobs.clear()
            await self.documentation_prewarm.close()
            await self.documentation_translator.close()
            await asyncio.gather(*pending, return_exceptions=True)
            if any(session.alive for session in self._sessions.values()):
                raise JsonRpcError("部分语言服务进程未退出，请稍后重试关闭")
        finally:
            async with self._guard:
                if self._shutdown is asyncio.current_task():
                    self._shutdown = None
                    self._closing = False

    async def reap_idle(self, *, idle: float = IDLE_TIMEOUT) -> list[str]:
        """回收空闲会话（由 app 的后台任务周期调用）。"""
        now = time.monotonic()
        async with self._guard:
            idle_sessions = [s for s in self._sessions.values() if not s.listeners and now - s.last_used >= idle]
            pending = [self._retire_locked(session) for session in idle_sessions]
        if pending:
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
        return [session.key for session in idle_sessions]

    async def open_workspace(
        self,
        project: dict,
        *,
        lang: str | None = None,
        limit: int = 40,
        max_bytes: int = 65536,
        tree_id: str = "main",
    ) -> dict:
        """把项目里若干文件批量交给语言服务器，换取**全项目诊断**。

        为什么需要它：编辑器只在打开某个文件时才连 LSP，于是"没打开过的文件有错"
        在地图上是看不见的。而地图的价值恰恰是"哪块编译不过一眼看到"。
        这里按语言取前 limit 个文件 didOpen，诊断随后异步落进索引。

        代价有界：只处理最近有人查看的项目（调用方决定）、单文件 ≤max_bytes、
        每语言至多 limit 个；语言服务器本身有并发与空闲回收上限。
        """
        from code_agent.codeintel.scanner import walk_project
        from code_agent.codeintel.uri import file_uri

        index = self.intel.index(project["id"], tree_id) if self.intel is not None else None
        root = Path(project["root_path"])
        # 文件清单按**磁盘现状**取，而不是读索引：索引可能还没发现刚写入的文件，
        # 那会导致"新写的错误看不到诊断"（真机踩过：批量诊断只分析了旧文件）。
        # 走盘成本有界（忽略规则 + 上限），且顺带拿到语言与大小。
        entries: list[tuple[str, str, int]] = []
        try:
            walk = await asyncio.to_thread(walk_project, root)
            entries = [(e.rel, e.lang, e.size) for e in walk.files if not e.skipped]
        except Exception:
            entries = []
        if not entries and index is not None:
            rows = index.query_all(
                "SELECT path, lang, size FROM files WHERE skipped='' ORDER BY size ASC"
            )
            entries = [(r["path"], r["lang"], int(r["size"] or 0)) for r in rows]

        wanted = [e for e in entries if not lang or e[1] == lang]
        if not wanted:
            return {"opened": 0, "languages": [], "skipped": "项目里没有可分析的文件"}

        by_lang: dict[str, list[tuple[str, int]]] = {}
        for rel, entry_lang, size in wanted:
            if size > max_bytes:
                continue
            by_lang.setdefault(entry_lang, []).append((rel, size))

        opened: list[str] = []
        languages: list[dict] = []
        for entry_lang, files in sorted(by_lang.items(), key=lambda kv: -len(kv[1])):
            definitions = await asyncio.to_thread(self.servers_for_language, entry_lang, project)
            if not definitions:
                languages.append({"lang": entry_lang, "state": "no-server", "files": len(files)})
                continue
            resolved = await asyncio.to_thread(lsp_catalog.resolve_command, definitions[0]["command"], root, root.parent)
            if not resolved:
                languages.append(
                    {"lang": entry_lang, "state": "missing", "server": definitions[0]["id"],
                     "hint": definitions[0]["install_hint"], "files": len(files)}
                )
                continue
            try:
                session = await self.session_for(project, entry_lang, tree_id=tree_id)
            except (JsonRpcError, OSError) as exc:
                languages.append(
                    {"lang": entry_lang, "state": "failed", "server": definitions[0]["id"],
                     "error": str(exc), "files": len(files)}
                )
                continue
            count = 0
            for rel, _size in files[:limit]:
                uri = file_uri(root / rel)
                if uri in session.documents:
                    continue
                try:
                    text = await asyncio.to_thread((root / rel).read_text, encoding="utf-8", errors="replace")
                except OSError:
                    continue
                await self.open_document(session, uri, text, lang_id=entry_lang)
                opened.append(rel)
                count += 1
            languages.append(
                {"lang": entry_lang, "state": "open", "server": session.server_id, "files": count}
            )
        return {"opened": len(opened), "languages": languages, "files": opened[:200]}

    # ------------------------------------------------------------------
    # 文档同步与转发
    # ------------------------------------------------------------------
    async def open_document(self, session: LspSession, uri: str, text: str, *, lang_id: str = "", background=False) -> None:
        await self.handle_client_notification(session, {
            "method": "textDocument/didOpen", "params": {
                "textDocument": {
                    "uri": uri,
                    "languageId": lang_id or session.lang,
                    "version": 1,
                    "text": text,
                }
            },
        }, background=background)

    async def change_document(self, session: LspSession, uri: str, text: str) -> None:
        version = int(session.documents.get(uri, 0)) + 1
        # 全量同步（TextDocumentSyncKind.Full=1）：实现简单且不会因增量计算偏差
        # 让服务端与编辑器状态分叉。代价是每次改动整篇发送，对编辑单个文件可接受。
        await self.handle_client_notification(session, {"method": "textDocument/didChange", "params": {
            "textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]}})

    async def save_document(self, session: LspSession, uri: str, text: str = "") -> None:
        params: dict[str, Any] = {"textDocument": {"uri": uri}}
        if text:
            params["text"] = text
        await self.handle_client_notification(session, {"method": "textDocument/didSave", "params": params})

    async def close_document(self, session: LspSession, uri: str) -> None:
        with contextlib.suppress(JsonRpcError):
            await self.handle_client_notification(session, {"method": "textDocument/didClose",
                "params": {"textDocument": {"uri": uri}}})

    async def request(
        self, session: LspSession, method: str, params: Any, *, timeout: float = 20.0,
        on_request_id: Callable[[int], None] | None = None,
    ) -> Any:
        session.touch()
        return await session.client.request(method, params, timeout=timeout, on_request_id=on_request_id)

    async def notify(self, session: LspSession, method: str, params: Any) -> None:
        session.touch()
        await session.client.notify(method, params)

    # ------------------------------------------------------------------
    # 来自浏览器的消息（WS 桥只做转发，状态都在这里维护）
    # ------------------------------------------------------------------
    #: 需要我们自己记账的文档同步方法：转发之外还要更新 uri→version 与诊断缓存
    _DOC_SYNC = frozenset(
        {
            "textDocument/didOpen",
            "textDocument/didChange",
            "textDocument/didSave",
            "textDocument/didClose",
        }
    )

    def subscribe(self, session: LspSession, listener: Callable[[dict], None]) -> Callable[[], None]:
        """One listener per connection; disconnect cannot overwrite another subscriber."""
        session.listeners.add(listener)
        return lambda: session.listeners.discard(listener)

    def _publish(self, session: LspSession, message: dict) -> None:
        for listener in tuple(session.listeners):
            with contextlib.suppress(Exception):
                listener(message)

    def _sync_diagnostics(self, session: LspSession, uri: str, *, include_current: bool = True) -> None:
        """The index holds one aggregate per file, while editor markers remain per server."""
        if self._on_diagnostics is None:
            return
        candidates = [candidate for candidate in self._sessions.values() if candidate is not session]
        if include_current:
            candidates.append(session)  # Also works for a notification during initialization.
        normalized = normalize_uri(uri)
        combined = []
        for candidate in candidates:
            if not candidate.alive or (candidate.project_id, candidate.tree_id) != (session.project_id, session.tree_id):
                continue
            target = candidate.uri_index.get(normalized, uri)
            version = candidate.diagnostic_versions.get(target)
            if version is not None and version != candidate.documents.get(target):
                continue
            combined.extend(candidate.diagnostics.get(target, []))
        with contextlib.suppress(Exception):
            self._on_diagnostics(self.project_ref(session), uri, combined)

    def _document_uri(self, session: LspSession, uri: Any) -> str:
        if not isinstance(uri, str) or len(uri) > 8192:
            raise JsonRpcError("文档 URI 无效")
        parsed = urlparse(uri)
        path = uri_to_path(uri)
        if (parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment
                or path is None or not path.is_absolute() or uri_to_rel(uri, session.root) in (None, "", ".")):
            raise JsonRpcError("文档 URI 必须位于当前项目内")
        return session.uri_index.get(normalize_uri(uri), uri)

    def _check_uris(self, session: LspSession, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "uri":
                    self._document_uri(session, child)
                else:
                    self._check_uris(session, child)
        elif isinstance(value, list):
            for child in value:
                self._check_uris(session, child)

    @staticmethod
    def _version(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise JsonRpcError("文档版本必须是非负整数")
        return value

    def _check_workspace_edit(self, session: LspSession, edit: Any) -> dict:
        if not isinstance(edit, dict):
            raise JsonRpcError("WorkspaceEdit 无效")
        groups: list[list] = []
        changes = edit.get("changes", {})
        if not isinstance(changes, dict):
            raise JsonRpcError("WorkspaceEdit.changes 无效")
        for uri, edits in changes.items():
            self._document_uri(session, uri)
            groups.append(edits)
        document_changes = edit.get("documentChanges", [])
        if not isinstance(document_changes, list):
            raise JsonRpcError("WorkspaceEdit.documentChanges 无效")
        if len(changes) + len(document_changes) > 100:
            raise JsonRpcError("一次重构最多涉及 100 项文档操作")
        for change in document_changes:
            if not isinstance(change, dict):
                raise JsonRpcError("文档操作无效")
            if "kind" in change:
                kind = change["kind"]
                if kind not in ("create", "rename", "delete"):
                    raise JsonRpcError("不支持此文件操作")
                for key in (("oldUri", "newUri") if kind == "rename" else ("uri",)):
                    self._document_uri(session, change.get(key))
                options = change.get("options") or {}
                if not isinstance(options, dict) or any(options.get(key) for key in ("overwrite", "recursive", "ignoreIfExists", "ignoreIfNotExists")):
                    raise JsonRpcError("文件操作不支持覆盖、递归或忽略冲突")
                continue
            document = change.get("textDocument") or {}
            if not isinstance(document, dict):
                raise JsonRpcError("TextDocumentEdit 无效")
            uri = self._document_uri(session, document.get("uri"))
            version = document.get("version")
            if version is not None and (self._version(version) != session.documents.get(uri)):
                raise JsonRpcError("编辑建议的文档版本已过期")
            groups.append(change.get("edits"))
        if sum(len(g) for g in groups if isinstance(g, list)) > 10000:
            raise JsonRpcError("编辑建议数量过多")
        text_size = 0
        for edits in groups:
            if not isinstance(edits, list):
                raise JsonRpcError("TextEdit 列表无效")
            for item in edits:
                if not isinstance(item, dict) or not isinstance(item.get("newText"), str):
                    raise JsonRpcError("TextEdit 无效")
                span = item.get("range") or {}
                if not isinstance(span, dict):
                    raise JsonRpcError("TextEdit range 无效")
                text_size += len(item["newText"])
                if text_size > 4 * 1024 * 1024:
                    raise JsonRpcError("编辑建议内容过大")
                points = []
                for side in ("start", "end"):
                    pos = span.get(side) or {}
                    if not isinstance(pos, dict):
                        raise JsonRpcError("TextEdit position 无效")
                    points.append((self._version(pos.get("line")), self._version(pos.get("character"))))
                if points[0] > points[1]:
                    raise JsonRpcError("TextEdit range 无效")
        canonical = dict(edit)
        if "changes" in edit:
            canonical["changes"] = {}
            for uri, edits in changes.items():
                # Windows servers may lowercase/percent-encode the drive letter.
                canonical["changes"].setdefault(self._document_uri(session, uri), []).extend(edits)
        if "documentChanges" in edit:
            canonical["documentChanges"] = [
                ({**change, **{key: self._document_uri(session, change[key]) for key in
                    (("oldUri", "newUri") if change["kind"] == "rename" else ("uri",))}}
                if "kind" in change else {**change, "textDocument": {**change["textDocument"],
                    "uri": self._document_uri(session, change["textDocument"]["uri"])}})
                for change in document_changes
            ]
        return canonical

    async def handle_client_notification(self, session: LspSession, message: dict, *, background=False) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict) or method not in self._DOC_SYNC:
            raise JsonRpcError("不支持该客户端通知")
        if method in self._DOC_SYNC:
            document = params.get("textDocument") or {}
            if not isinstance(document, dict):
                raise JsonRpcError("textDocument 无效")
            uri = self._document_uri(session, document.get("uri"))
            params = {**params, "textDocument": {**document, "uri": uri}}
            normalized = normalize_uri(uri)
            if method == "textDocument/didOpen":
                version = self._version(document.get("version"))
                text = document.get("text")
                if not isinstance(text, str):
                    raise JsonRpcError("didOpen.text 无效")
                if not background:
                    session.foreground_documents.add(uri)
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                current = session.documents.get(uri)
                if current is not None:
                    if version < current or (version == current and digest != session.document_hashes.get(uri)):
                        raise JsonRpcError("didOpen 文档版本已过期或内容与版本不符")
                    if version == current:
                        session.document_texts[uri] = text
                        self.documentation_prewarm.schedule(session, uri, text)
                        session.touch()
                        return  # Reconnect replay already matches the persistent document.
                    method = "textDocument/didChange"
                    params = {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]}
                session.documents[uri] = version
                session.document_hashes[uri] = digest
                session.document_texts[uri] = text
                session.closed_documents.discard(normalized)
                session.uri_index[normalized] = uri
            elif method == "textDocument/didChange":
                version = self._version(document.get("version"))
                if uri not in session.documents or version <= session.documents[uri]:
                    raise JsonRpcError("didChange 需要已打开文档和递增版本")
                changes = params.get("contentChanges")
                if not isinstance(changes, list) or not changes or any(
                    not isinstance(c, dict) or not isinstance(c.get("text"), str) for c in changes
                ):
                    raise JsonRpcError("didChange.contentChanges 无效")
                session.documents[uri] = version
                snapshot = apply_changes(session.document_texts.get(uri), changes)
                self.documentation_prewarm.cancel(session, uri)
                if snapshot is not None:
                    session.document_texts[uri] = snapshot
                else:
                    session.document_texts.pop(uri, None)
                if all("range" not in c for c in changes):
                    session.document_hashes[uri] = hashlib.sha256(changes[-1]["text"].encode("utf-8")).hexdigest()
                else:
                    session.document_hashes.pop(uri, None)
            elif method == "textDocument/didClose":
                session.documents.pop(uri, None)
                session.document_hashes.pop(uri, None)
                session.document_texts.pop(uri, None)
                session.foreground_documents.discard(uri)
                self.documentation_prewarm.cancel(session, uri)
                session.uri_index.pop(normalized, None)
                session.diagnostics.pop(uri, None)
                session.diagnostic_versions.pop(uri, None)
                session.closed_documents.add(normalized)
                if len(session.closed_documents) > 1024:
                    session.closed_documents.pop()
                self._sync_diagnostics(session, uri)
        session.touch()
        await session.client.notify(method, params)
        if method in ("textDocument/didOpen", "textDocument/didChange", "textDocument/didSave"):
            self.documentation_prewarm.schedule(session, uri, session.document_texts.get(uri))

    async def handle_client_request(
        self, session: LspSession, message: dict, *, on_request_id: Callable[[int], None] | None = None,
    ) -> dict:
        """处理浏览器发来的请求，返回可直接下发的 JSON-RPC 响应信封。"""
        rid = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not method:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": "缺少 method"}}
        if method in ("initialize", "shutdown", "exit"):
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "该操作由编辑器审查流程管理"}}
        try:
            if method == "workspace/executeCommand":
                commands = (session.capabilities.get("executeCommandProvider") or {}).get("commands", [])
                arguments = params.get("arguments") if isinstance(params, dict) else None
                if (not isinstance(params, dict) or session.server_id != "jdtls" or params.get("command") != "java.edit.organizeImports"
                        or params["command"] not in commands or not isinstance(arguments, list) or len(arguments) != 1):
                    raise JsonRpcError("此命令尚未接入可预览的重构流程")
                self._document_uri(session, arguments[0])
            self._check_uris(session, params)
            uri = None
            if isinstance(params, dict) and "textDocument" in params:
                document = params["textDocument"]
                if not isinstance(document, dict):
                    raise JsonRpcError("textDocument 无效")
                uri = self._document_uri(session, document.get("uri"))
            timeout = float(message.get("timeout") or 20.0)
            if not 0 < timeout <= 60:
                raise ValueError("timeout 需在 0 到 60 秒之间")
        except (JsonRpcError, ValueError, TypeError, RecursionError) as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": str(exc)}}
        # 服务器不支持的能力直接回空结果：比报错更不容易在编辑器里弹出红色提示
        unsupported = {
            "textDocument/completion": "completionProvider",
            "textDocument/hover": "hoverProvider",
            "textDocument/definition": "definitionProvider",
            "textDocument/declaration": "declarationProvider",
            "textDocument/typeDefinition": "typeDefinitionProvider",
            "textDocument/implementation": "implementationProvider",
            "textDocument/prepareCallHierarchy": "callHierarchyProvider",
            "callHierarchy/incomingCalls": "callHierarchyProvider",
            "callHierarchy/outgoingCalls": "callHierarchyProvider",
            "textDocument/prepareTypeHierarchy": "typeHierarchyProvider",
            "typeHierarchy/supertypes": "typeHierarchyProvider",
            "typeHierarchy/subtypes": "typeHierarchyProvider",
            "textDocument/codeLens": "codeLensProvider",
            "codeLens/resolve": "codeLensProvider",
            "textDocument/inlayHint": "inlayHintProvider",
            "inlayHint/resolve": "inlayHintProvider",
            "textDocument/references": "referencesProvider",
            "textDocument/documentSymbol": "documentSymbolProvider",
            "textDocument/rename": "renameProvider",
            "textDocument/prepareRename": "renameProvider",
            "textDocument/signatureHelp": "signatureHelpProvider",
            "textDocument/codeAction": "codeActionProvider",
            "workspace/symbol": "workspaceSymbolProvider",
            "textDocument/formatting": "documentFormattingProvider",
            "textDocument/rangeFormatting": "documentRangeFormattingProvider",
            "textDocument/semanticTokens/full": "semanticTokensProvider",
            "textDocument/semanticTokens/range": "semanticTokensProvider",
        }.get(method)
        if unsupported and not session.supports(unsupported):
            return {"jsonrpc": "2.0", "id": rid, "result": None}
        if method in ("textDocument/semanticTokens/full", "textDocument/semanticTokens/range"):
            semantic = session.capabilities.get("semanticTokensProvider")
            mode = "range" if method.endswith("/range") else "full"
            if not isinstance(semantic, dict) or semantic.get(mode) is None or semantic.get(mode) is False:
                return {"jsonrpc": "2.0", "id": rid, "result": None}
        version = session.documents.get(uri) if uri else None
        params = restore_protocol_text(params)
        actual_id: int | None = None

        def assigned(value: int) -> None:
            nonlocal actual_id
            actual_id = value
            if on_request_id is not None:
                on_request_id(value)

        try:
            result = await self.request(session, method, params, timeout=timeout, on_request_id=assigned)
            if uri and session.documents.get(uri) != version:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32801, "message": "请求期间文档已变化"}}
            if method == "textDocument/rename" and result is not None:
                result = self._check_workspace_edit(session, result)
                await self._check_java_type_rename(session, params, result, timeout, on_request_id=assigned)
                if uri and session.documents.get(uri) != version:
                    raise JsonRpcError("请求期间文档已变化")
            if method == "workspace/executeCommand" and result is not None:
                result = self._check_workspace_edit(session, result)
            if method in ("textDocument/codeAction", "codeAction/resolve"):
                def canonical_action(action):
                    if isinstance(action, dict) and "edit" in action:
                        return {**action, "edit": self._check_workspace_edit(session, action["edit"])}
                    return action
                result = [canonical_action(action) for action in result] if isinstance(result, list) else canonical_action(result)
            result = await localize_result(self.documentation_translator, method, result,
                                           scope=f"{session.project_id}:{session.tree_id}:{session.server_id}")
            if uri and session.documents.get(uri) != version:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32801, "message": "翻译期间文档已变化"}}
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except asyncio.CancelledError:
            if actual_id is not None:
                with contextlib.suppress(Exception):
                    await session.client.notify("$/cancelRequest", {"id": actual_id})
            raise
        except JsonRpcError as exc:
            error = await localize_error(self.documentation_translator, {"code": -32000, "message": str(exc)})
            return {"jsonrpc": "2.0", "id": rid, "error": error}

    async def _check_java_type_rename(
        self, session: LspSession, params: dict, edit: dict, timeout: float,
        *, on_request_id: Callable[[int], None] | None = None,
    ) -> None:
        """Reject JDT's incomplete linked-resource type rename, using server symbols.

        JDT can omit RenameResourceChange while retaining declaration/reference
        text edits. Never invent a file move from Java source text: require the
        server to supply the move for a semantically identified primary type.
        """
        if session.server_id != "jdtls" or not session.supports("documentSymbolProvider"):
            return
        if (not isinstance(params, dict) or not isinstance(params.get("textDocument"), dict)
                or not isinstance(params["textDocument"].get("uri"), str)
                or not isinstance(params.get("position"), dict)):
            raise JsonRpcError("Java 重命名需要文件 URI 和符号位置")
        new_name = params.get("newName")
        if not isinstance(new_name, str):
            raise JsonRpcError("Java 重命名需要新的符号名称")
        groups: dict[str, list] = {}
        renamed: set[str] = set()
        for change in edit.get("documentChanges", []):
            if change.get("kind") == "rename":
                renamed.add(normalize_uri(change["oldUri"]))
            elif "textDocument" in change:
                groups.setdefault(normalize_uri(change["textDocument"]["uri"]), []).extend(change["edits"])
        for target, edits in edit.get("changes", {}).items():
            groups.setdefault(normalize_uri(target), []).extend(edits)
        groups = {target: edits for target, edits in groups.items()
                  if target not in renamed and (uri_to_path(target) or Path()).suffix == ".java"}
        if not groups:
            return

        def span(value):
            if not isinstance(value, dict):
                return None
            points = [value.get(key) for key in ("start", "end")]
            if any(not isinstance(point, dict) or any(type(point.get(key)) is not int or point[key] < 0
                       for key in ("line", "character")) for point in points):
                return None
            start, end = [(point["line"], point["character"]) for point in points]
            return (start, end) if start < end else None

        def overlaps(a, b):
            return bool(a and b and a[0] < b[1] and b[0] < a[1])

        versions = dict(session.documents)
        deadline = time.monotonic() + min(timeout, 5.0)

        async def query(method, arguments):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JsonRpcError("Java 类型重命名校验超时，未应用修改，请稍后重试")
            result = await self.request(session, method, arguments, timeout=remaining, on_request_id=on_request_id)
            if session.documents != versions:
                raise JsonRpcError("请求期间文档已变化")
            return result

        source = normalize_uri(params["textDocument"]["uri"])
        definitions: dict[str, list] = {}
        if session.supports("definitionProvider"):
            locations = await query("textDocument/definition", {
                "textDocument": params["textDocument"], "position": params["position"],
            })
            for location in (locations if isinstance(locations, list) else [locations])[:16]:
                if not isinstance(location, dict):
                    continue
                target = location.get("targetUri") or location.get("uri")
                if not isinstance(target, str) or uri_to_rel(target, session.root) is None:
                    continue
                definitions.setdefault(normalize_uri(target), []).append(span(
                    location.get("targetSelectionRange") or location.get("range")))
        candidates = list(dict.fromkeys([source, *definitions, *(
            target for target, edits in groups.items() if any(item.get("newText") == new_name for item in edits)
        )]))
        position = params.get("position") or {}
        point = (position.get("line"), position.get("character"))
        point_valid = all(type(value) is int and value >= 0 for value in point)
        for target in candidates[:16]:
            if target not in groups:
                continue
            symbols = await query("textDocument/documentSymbol", {"textDocument": {"uri": target}})
            for symbol in symbols if isinstance(symbols, list) else []:
                if not isinstance(symbol, dict) or symbol.get("kind") not in (5, 10, 11, 23):
                    continue
                if symbol.get("name") != uri_to_path(target).stem or symbol["name"] == new_name:
                    continue
                declaration = span(symbol.get("selectionRange"))
                if not declaration:
                    continue
                semantic_target = (target == source and point_valid and declaration[0] <= point < declaration[1]
                                   or any(overlaps(declaration, value) for value in definitions.get(target, [])))
                for item in groups[target]:
                    changed_range = span(item.get("range"))
                    if overlaps(declaration, changed_range) and (semantic_target or item.get("newText") == new_name):
                        raise JsonRpcError(
                            f"Java 服务器返回了不完整的类型重命名，缺少 {uri_to_path(target).name} 的文件移动。"
                            "当前工程资源关联方式暂不支持此操作，未应用任何修改。")

    # ------------------------------------------------------------------
    # 服务端通知/请求
    # ------------------------------------------------------------------
    def _on_notification(self, session: LspSession, message: dict) -> None:
        method = message.get("method")
        if method == "textDocument/publishDiagnostics":
            if not self._store_diagnostics(session, message.get("params") or {}):
                return
            params = message["params"]
            message = {**message, "params": {**params, "uri": self._document_uri(session, params["uri"])}}
        if method not in ("textDocument/publishDiagnostics", "$/progress", "window/showMessage", "window/logMessage", "code-agent/reviewEdit"):
            self._publish(session, message)
            return
        # Keep the stdio reader synchronous and responsive to cancellation.
        # Only the latest notification for a document/progress token may win.
        params = message.get("params") or {}
        uri = params.get("uri")
        key = f"{method}:{uri or params.get('token', '')}"
        revision = session.notification_revisions.get(key, 0) + 1
        session.notification_revisions[key] = revision
        old = session.localization_tasks.pop(key, None)
        if old:
            old.cancel()
        if (self.intel is None and isinstance(self.documentation_translator, DocumentationTranslator)
                and not self.documentation_translator.store.stats().get("entries")):
            # Embedded/offline managers without a model or any saved phrases
            # retain their immediate notification behavior.
            self._publish(session, message)
            return
        if method == "textDocument/publishDiagnostics" and not params.get("diagnostics"):
            self._publish(session, message)
            return
        if len(session.localization_tasks) >= 64 and key not in session.localization_tasks:
            self._publish(session, message)
            return
        version = session.documents.get(uri) if uri else None
        generation = self._generation

        async def localized():
            output = message

            def current():
                return (session.alive and self._generation == generation
                        and session.notification_revisions.get(key) == revision
                        and (not uri or session.documents.get(uri) == version))

            try:
                while current():
                    output = await localize_notification(self.documentation_translator, output,
                        scope=f"{session.project_id}:{session.tree_id}:{session.server_id}")
                    if not current():
                        return
                    if method == "textDocument/publishDiagnostics":
                        if not self._store_diagnostics(session, output.get("params") or {}):
                            return
                    self._publish(session, output)
                    if not notification_needs_translation(output):
                        return
                    # Busy/offline providers must not leave the last server
                    # notification untranslated forever; retry the remaining
                    # phrases without waiting for a fresh compiler event.
                    await asyncio.sleep(DOCUMENTATION_RETRY_DELAY)
            finally:
                if session.localization_tasks.get(key) is asyncio.current_task():
                    session.localization_tasks.pop(key, None)

        task = asyncio.create_task(localized())
        session.localization_tasks[key] = task
        task.add_done_callback(self._observe_task)

    def _store_diagnostics(self, session: LspSession, params: dict) -> bool:
        if not isinstance(params, dict):
            return False
        try:
            uri = self._document_uri(session, params.get("uri"))
            normalized = normalize_uri(uri)
            version = params.get("version")
            if version is not None:
                self._version(version)
                if version != session.documents.get(uri):
                    return False
            if normalized in session.closed_documents:
                return False
        except (JsonRpcError, ValueError, OSError):
            return False
        items = params.get("diagnostics") or []
        if not isinstance(items, list):
            return False
        # 认领回"我们发出的那个 uri"：语言服务器可能回传自己归一化过的形式
        uri = session.uri_index.get(normalize_uri(uri), uri)
        cleaned: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            span = item.get("range") or {}
            if not isinstance(span, dict):
                continue
            start = span.get("start") or {}
            end = span.get("end") or {}
            if not isinstance(start, dict) or not isinstance(end, dict):
                continue
            try:
                coords = [self._version(p.get(k, 0)) for p in (start, end) for k in ("line", "character")]
                severity = int(item.get("severity") or 1)
            except (JsonRpcError, TypeError, ValueError):
                continue
            cleaned.append(
                {
                    **item,  # Keep tags/data/relatedInformation for code actions and reconnect.
                    "line": coords[0] + 1,          # LSP 0-based → 1-based
                    "character": coords[1],
                    "end_line": coords[2] + 1,
                    "end_character": coords[3],
                    "severity": severity,
                    "message": str(item.get("message") or "")[:32000],
                    "source": str(item.get("source") or session.server_id)[:60],
                    "code": item.get("code", ""),
                }
            )
        session.diagnostics[uri] = cleaned
        session.diagnostic_versions[uri] = version
        session.published += 1
        import logging

        logging.getLogger(__name__).info(
            "LSP 诊断：%s → %s 共 %d 条", session.server_id, uri.split("/")[-1], len(cleaned)
        )
        self._sync_diagnostics(session, uri)
        return True

    def project_ref(self, session: LspSession) -> dict:
        """会话 → 项目行（把诊断写回索引/地图时需要）。"""
        if self.projects is not None:
            row = self.projects.get(session.project_id)
            if row is not None:
                return dict(row)
        return {"id": session.project_id, "root_path": str(session.root), "name": session.root.name}

    def _on_server_request(self, session: LspSession, message: dict) -> Any:
        """回应服务端发起的请求。LSP 要求客户端必须响应，否则对方会一直等。"""
        method = message.get("method") or ""
        if method == "workspace/configuration":
            # 必须按 section 返回真实设置：返回空对象等于告诉服务器"没配置"，
            # pyright 会退回默认（不做类型检查）→ 诊断永远是空的
            items = (message.get("params") or {}).get("items") or []
            return [_section_of(session.settings, str(item.get("section") or "")) for item in items]
        if method == "window/workDoneProgress/create":
            return None
        if method in ("workspace/codeLens/refresh", "workspace/inlayHint/refresh"):
            self._publish(session, {"jsonrpc": "2.0", "method": method, "params": {}})
            return None
        if method == "client/registerCapability" or method == "client/unregisterCapability":
            return None
        if method == "workspace/workspaceFolders":
            return [{"uri": _root_uri(session.root), "name": session.root.name}]
        if method == "workspace/applyEdit":
            params = message.get("params") or {}
            try:
                edit = self._check_workspace_edit(session, params.get("edit"))
            except (JsonRpcError, TypeError, AttributeError, ValueError) as exc:
                return {"applied": False, "failureReason": str(exc)}
            self._on_notification(session, {"jsonrpc": "2.0", "method": "code-agent/reviewEdit", "params": {
                "request_id": message.get("id"), "server": session.server_id,
                "label": str(params.get("label") or "语言服务编辑建议")[:200], "edit": edit,
            }})
            return {"applied": False, "failureReason": "编辑建议已交给用户审查，服务端未写入文件"}
        return None
