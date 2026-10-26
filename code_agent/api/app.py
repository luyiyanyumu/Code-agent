"""FastAPI 应用工厂（s06 sidecar）。

依赖通过 app.state 注入：
    db            Database
    users         UserRepo
    sessions_repo SessionRepo
    approvals     ApprovalRepo
    transcript    TranscriptStore
    router        ModelRouter
    registry      ToolRegistry
    sessions      SessionManager

默认本地单用户免登录，沿用已有管理员的数据身份。
CODE_AGENT_AUTH_ENABLED=true 可启用 JWT 认证；空数据库的初始管理员密码取
CODE_AGENT_ADMIN_PASSWORD（默认 admin123，启用认证的部署应显式配置）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from code_agent import __version__
from code_agent.agent.router import ModelRouter
from code_agent.api.static_web import mount_workbench
from code_agent.api.deps import CurrentUser, get_current_user, get_ws_user
from code_agent.api.code_api import mount_code_api
from code_agent.api.change_review_api import mount_change_review_api
from code_agent.api.runtime_api import mount_runtime_api
from code_agent.api.debug_api import mount_debug_api
from code_agent.api.native_dialog_api import mount_native_dialog_api
from code_agent.api.agent_settings_api import mount_agent_settings_api
from code_agent.api.agent_team_api import mount_agent_team_api
from code_agent.agent.project_team import ProjectTeam, TeamError
from code_agent.tools.builtin.agent_team_tools import register_agent_team_tools
from code_agent.codeintel.change_review import ChangeReviewService
from code_agent.codeintel.debug_runtime import DebugRuntime
from code_agent.api.security import create_token, hash_password, verify_password
from code_agent.automations import Scheduler, compute_initial_next_run
from code_agent.codeintel.projects import ProjectService
from code_agent.codeintel.service import CodeIntelService
from code_agent.config import (
    code_dir,
    artifacts_dir,
    auth_enabled,
    builtin_skills_dir,
    experts_dir,
    knowledge_dir,
    mcp_config_path,
    plugins_dir,
    skills_dir,
    transcripts_dir,
    workspace_root,
)
from code_agent.extensions import (
    ArtifactsStore,
    AuthoringError,
    DeliverFileTool,
    ExpertsRegistry,
    LoadPackTool,
    McpManager,
    PackSource,
    PluginRegistry,
    PresentTool,
    SkillsRegistry,
    build_catalog,
    delete_plugin_dir,
    extract_plugin_zip,
    extract_skill_zip,
    find_tool,
    load_mcp_config,
    normalize_mcp_servers,
    save_mcp_config,
    write_skill_file,
)
from code_agent.knowledge import (
    AREA_HUMAN,
    AREA_LLM,
    KbSearchTool,
    KbWriteTool,
    KnowledgeBase,
    compile_document,
)
from code_agent import settings
from code_agent.permissions.policy import AllowPolicy, DefaultPolicy
from code_agent.models.events import EventType
from code_agent.sessions.manager import (
    SessionBusy,
    SessionClosed,
    SessionManager,
    SessionRuntime,
)
from code_agent.storage.db import (
    ApprovalRepo,
    AuditRepo,
    AutomationRepo,
    BatchRepo,
    Database,
    ProjectRepo,
    SessionRepo,
    TaskRepo,
    UsageRepo,
    UserRepo,
    WorkspaceRepo,
)
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import BashPersistentTool, BashTool, ReadFileTool, WriteFileTool
from code_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def build_registry(
    artifacts_store: ArtifactsStore | None = None,
    knowledge: KnowledgeBase | None = None,
    code_intel: CodeIntelService | None = None,
) -> ToolRegistry:
    from code_agent.memory import SaveMemoryTool
    from code_agent.tools.builtin.code_tools import (
        CodeFeaturesTool,
        CodeGraphTool,
        CodeMapTool,
        CodeSearchTool,
        EditFileTool,
        EditManyTool,
        GlobTool,
        GrepTool,
        ListDirTool,
    )

    reg = ToolRegistry()
    from code_agent.tools.builtin.propose_edit import ProposeEditTool
    reg.register(ProposeEditTool(code_intel))
    reg.register(BashTool())
    # M48 持久 shell（对齐 dsh-tool-bash-persistent）：cd / export / 函数跨调用保留。
    # 刻意是独立工具：与一次性 bash 的状态语义完全不同，写进同一段描述容易让模型
    # 在"我以为还在那个目录"上栽跟头。
    reg.register(BashPersistentTool())
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    # M18 代码模式工具：定位（list_dir/grep/code_map）与精确编辑（edit_file）
    reg.register(ListDirTool())
    reg.register(GrepTool())
    # M43 按路径模式发现文件（对齐 dsh-tool-fs-search 的 glob）：
    # list_dir 只能一层层翻，grep 只能按内容找，"这棵树里有哪些 .py"以前没有工具能答
    reg.register(GlobTool())
    from code_agent.tools.builtin.git_read_tools import GitReadTool

    reg.register(GitReadTool())
    reg.register(CodeMapTool(code_intel))
    # M33 功能导航：按"能干什么"查（"X 功能在哪个板块"用这个，结构地图搜不到功能名）
    reg.register(CodeFeaturesTool(code_intel))
    # M34 检索层：融合检索（向量 + 关键词 + 图谱按需）与按需图谱（都只读）
    reg.register(CodeSearchTool(code_intel))
    reg.register(CodeGraphTool(code_intel))
    reg.register(EditFileTool(code_intel))
    # M52 一次多处精确替换（**DSH 没有这个工具**）：跨文件改名/批量补 docstring 时，
    # 用 edit_file 要发 N 轮工具调用，每一轮都要重新装配上下文 + 一次模型往返。
    # 两段式：先在内存里按顺序推演全部改动，全通过才落盘；落盘中途失败如实报。
    reg.register(EditManyTool(code_intel))
    reg.register(SaveMemoryTool())
    _artifacts = artifacts_store or ArtifactsStore(artifacts_dir())
    reg.register(DeliverFileTool(_artifacts))
    # M43 交付已存在的文件（对齐 dsh-tool-present）：deliver_file 只能收**文本内容**，
    # 脚本跑出来的 xlsx / PDF / 大图根本塞不进工具参数；present 只收路径。
    reg.register(PresentTool(_artifacts))
    reg.register(LoadPackTool(SkillsRegistry(skills_dir())))
    reg.register(LoadPackTool(ExpertsRegistry(experts_dir())))
    # M43 运行时依赖位置（对齐 dsh-tool-workspace-dependencies）：让模型用绝对路径调
    # python/node/pnpm，而不是靠 PATH 猜——猜错会污染全局环境或得到与 CI 不同的结果。
    from code_agent.tools.builtin.env_tools import LoadWorkspaceDependenciesTool

    reg.register(LoadWorkspaceDependenciesTool())
    # M45 后台任务（对齐 dsh-tool-jobs）：bash(run_in_background) 起的任务由这三个工具读取/停止。
    # 任务表本身挂在 SessionRuntime 上（会话级、进程内、不落库），工具通过 ctx.extra 拿。
    from code_agent.tools.builtin.job_tools import build_job_tools

    for tool in build_job_tools():
        reg.register(tool)
    # M51 读图（对齐 dsh-tool-fs 的 read_image）：把图片附到下一次模型请求。
    # 是否真的发出去由 CODE_AGENT_VISION 决定（默认关——默认 provider 是 deepseek，
    # 其 chat 接口不吃 image_url 块）；没开时工具会如实说明"像素没有发出去"。
    from code_agent.tools.builtin.image_tools import build_image_tools

    for tool in build_image_tools():
        reg.register(tool)
    if knowledge is not None:
        reg.register(KbSearchTool(knowledge))
        reg.register(KbWriteTool(knowledge))
    # M19 联网：工具常驻注册，是否暴露给模型由会话的「联网」开关决定
    # （关掉时用 FilteredToolRegistry 把 web_search/web_fetch 从 schema 里摘掉）
    from code_agent.tools.builtin.web_tools import build_web_tools

    for tool in build_web_tools():
        reg.register(tool)
    # M20 技能商店：让 agent 能自己把技能装进 Code-agent 的技能目录
    # （不再依赖 shell：skillhub 的入口是 bash 脚本，Windows 上那条路是断的）
    from code_agent.tools.builtin.skill_tools import build_skill_tools

    for tool in build_skill_tools():
        reg.register(tool)
    # M29 有序验收清单：模型自己声明"做完的标准"，门禁与收口自查都用它
    from code_agent.tools.builtin.acceptance_tools import build_acceptance_tools

    for tool in build_acceptance_tools():
        reg.register(tool)
    # M32 契约骨架：让 agent 自己能查"实现与契约对不对得上"
    from code_agent.tools.builtin.contract_tools import build_contract_tools

    for tool in build_contract_tools():
        reg.register(tool)
    # M33 功能描述：给没写 docstring 的符号补一句人话（按内容哈希缓存，代码改了自动失效）
    from code_agent.tools.builtin.brief_tools import build_brief_tools

    for tool in build_brief_tools(code_intel):
        reg.register(tool)
    # M30 任务清单：模型自己维护"这一轮做到第几步"，界面在输入框上方显示进度
    from code_agent.tools.builtin.todo_tools import build_todo_tools

    for tool in build_todo_tools():
        reg.register(tool)
    # M31 提问：模型停下来问人一句，答案作为普通工具结果喂回（阻塞式）
    from code_agent.tools.builtin.ask_tools import build_ask_tools

    for tool in build_ask_tools():
        reg.register(tool)
    return reg


def _clean_code_context(body: dict[str, Any]) -> dict[str, Any] | None:
    """清洗代码模式上下文（M18）。

    只放行白名单字段：path / start / end / symbol_id / node_id / test_files / importers。
    这是安全边界而不是格式美化——path 会被用于读取项目内文件，任意键都可能
    变成"让模型看工作区外的文件"的入口。
    """
    raw = body.get("code_context")
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    path = str(raw.get("path") or "").strip().replace("\\", "/")
    if path and ".." not in path and not path.startswith("/") and len(path) <= 400:
        out["path"] = path
    for key in ("start", "end"):
        try:
            value = int(raw.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[key] = value
    for key in ("symbol_id", "node_id"):
        value = str(raw.get(key) or "").strip()
        if value and len(value) <= 400:
            out[key] = value
    for key in ("test_files", "importers"):
        items = raw.get(key)
        if isinstance(items, list):
            cleaned = [str(i)[:300] for i in items[:20] if str(i).strip()]
            if cleaned:
                out[key] = cleaned
    hint = raw.get("map_hint")
    if isinstance(hint, str) and hint.strip():
        out["map_hint"] = hint.strip()[:400]
    return out or None


#: 附件上限（原始字节数）。base64 传输会膨胀 ~33%，30MB 原文对应 ~40MB 请求体
MAX_UPLOAD_BYTES = 30 * 1024 * 1024


def _clean_web_flag(body: dict[str, Any]) -> bool | None:
    """从请求体取「联网」开关（M19）。没给返回 None = 沿用会话设置。

    只认真正的布尔值：`"false"` 这种字符串如果被当成真值，会把"关掉联网"变成打开，
    而打开联网意味着会真的发外部请求——这种地方不做隐式转换（fail closed）。
    """
    if "web" not in body:
        return None
    value = body.get("web")
    if isinstance(value, bool):
        return value
    return None


def _clean_attachments(body: dict[str, Any]) -> list[dict[str, Any]] | None:
    """从请求体取附件清单并清洗。只收白名单字段——路径必须是 uploads/ 下的
    相对路径，否则模型会被诱导去读工作区外的文件（这是安全边界，不是格式美化）。"""
    items = body.get("attachments")
    if not isinstance(items, list) or not items:
        return None
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:120]
        path = str(it.get("path") or "").strip()
        if not name or not path:
            continue
        # 只放行 uploads/<单段文件名>，其余一律丢弃（fail closed）
        if "/" not in path and "\\" not in path:
            path = f"uploads/{path}"
        parts = path.replace("\\", "/").split("/")
        if len(parts) != 2 or parts[0] != "uploads" or not parts[1] or parts[1] in (".", ".."):
            continue
        entry: dict[str, Any] = {"name": name, "path": path}
        size = it.get("size")
        if isinstance(size, int) and size >= 0:
            entry["size"] = size
        out.append(entry)
        if len(out) >= 10:
            break
    return out or None


def create_app(provider_name: str | None = None) -> FastAPI:
    requires_auth = auth_enabled()
    # 插件先扫：它声明的 MCP 服务器要参与 MCP 连接，内嵌技能要并进技能目录
    plugins = PluginRegistry(plugins_dir())
    # MCP 信任工具名（lifespan 里填充）：重算放行名单时不能把它们丢掉
    mcp_trusted: set[str] = set()
    #: 代码式工具加载结果（插件 tools.py），默认关闭
    code_tools_state: dict[str, Any] = {"status": {}, "errors": [], "loaded": []}
    mcp = McpManager(mcp_config_path(), extra_servers=plugins.mcp_servers())

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        code_intel.bind_brief_loop(asyncio.get_running_loop())
        # MCP 连接器接入：连不上的 server 会被跳过，不阻塞启动
        for tool, trusted in await mcp.connect_all():
            registry.register(tool)
            if trusted:
                mcp_trusted.add(tool.name)
        # M10：插件代码工具（双重开关，默认不加载）
        code_tools_state["status"] = plugins.code_tools_status()
        loaded, errors = plugins.load_code_tools()
        code_tools_state["loaded"] = [t.name for t in loaded]
        code_tools_state["errors"] = errors
        for tool in loaded:
            registry.register(tool)
        _refresh_policies()
        # M5 s23：自动化调度器
        scheduler = Scheduler(automations_repo, manager.run_automation)
        scheduler.start()
        app.state.scheduler = scheduler
        # M18：代码模式 watcher（外部改动 → 索引自动跟上；只盯最近有人查看的项目）
        watch_task: asyncio.Task | None = None
        if os.environ.get("CODE_AGENT_CODE_WATCH", "1") != "0":
            watch_task = asyncio.create_task(_code_watch_loop())
        # M18 · P3：LSP 会话回收（空闲超时后杀进程，避免留下常驻语言服务器）
        lsp_task = asyncio.create_task(_lsp_reap_loop())
        # M34：顺手探一次本地向量端点（Ollama/LM Studio/vLLM），探到就启用融合检索的向量路。
        # **必须写在这里**：本项目用的是 `FastAPI(lifespan=...)`，Starlette 会忽略
        # `@app.on_event("startup")`——挂在 on_event 上会"注册成功但永远不执行"，
        # 症状是"配了本地模型却始终走关键词单路"，而且不报错。真机踩过一次，别改回去。
        await _autodetect_embedding()
        # Clear obsolete saved settings without changing current model or permissions.
        with contextlib.suppress(Exception):
            settings.legacy_model_catalog_purge()
        yield
        await project_team.aclose()
        from code_agent.codeintel import native_dialog
        await native_dialog.aclose()
        await app.state.ide_runtime.aclose()
        await app.state.ide_debug.close()
        change_reviews.close()
        lsp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await lsp_task
        with contextlib.suppress(Exception):
            await lsp_manager.stop_all()
        if watch_task is not None:
            watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watch_task
        await scheduler.stop()
        await code_intel.aclose()
        await mcp.stop_all()
        await router.aclose()
        await knowledge.aclose()

    app = FastAPI(title="Code-agent", version=__version__, lifespan=lifespan)
    app.state.auth_enabled = requires_auth

    db = Database(workspace_root().parent / "code_agent.db")
    users = UserRepo(db)
    sessions_repo = SessionRepo(db)
    workspaces_repo = WorkspaceRepo(db)
    projects_repo = ProjectRepo(db)
    approvals = ApprovalRepo(db)
    usage_repo = UsageRepo(db)
    audit_repo = AuditRepo(db)
    automations_repo = AutomationRepo(db)
    transcript = TranscriptStore(transcripts_dir())
    router = ModelRouter(provider_name=provider_name)
    artifacts_store = ArtifactsStore(artifacts_dir())
    knowledge = KnowledgeBase(knowledge_dir())

    async def _autodetect_embedding() -> None:
        """启动时探一次本地向量端点；结果留在 `app.state.embed_probe` 便于排查
        （"为什么没有向量检索"必须一眼能查到，而不是去猜日志）。"""
        try:
            from code_agent.retrieval.embed import LocalEmbedder, autodetect_local

            found = await autodetect_local()
            app.state.embed_probe = found
            if found.get("found"):
                # 显式构造：不走"写环境变量再重建"那条间接路（它依赖构造顺序，
                # 出问题时表现为"配了却没生效"，是最难查的一类问题）
                knowledge.embedder = LocalEmbedder(
                    base_url=str(found["base_url"]), model=str(found["model"])
                )
                logger.info(
                    "检索：已自动启用本地向量模型 %s（%s，%s 维）",
                    found["model"],
                    found["base_url"],
                    found.get("dim"),
                )
            else:
                logger.info("检索：未发现本地向量端点，走关键词单路（可用 CODE_AGENT_EMBED_BASE_URL 指定）")
        except Exception as exc:  # noqa: BLE001 —— 探测失败只意味着"关键词单路"，不影响启动
            app.state.embed_probe = {"found": False, "error": str(exc)[:200]}
            logger.warning("检索：本地向量探测失败（不影响启动）：%s", exc)
    # 代码情报先建：代码模式工具（code_map/edit_file）需要它，注册表也要带上它
    code_service = ProjectService(projects_repo)
    code_intel = CodeIntelService(projects_repo)
    change_reviews = ChangeReviewService(code_intel)
    code_intel.reviews = change_reviews
    registry = build_registry(artifacts_store, knowledge=knowledge, code_intel=code_intel)

    # M4/M10 扩展：技能（含插件内嵌）与专家目录注入 system prompt
    # M49 再加一层**随包自带**的技能（office 系列）：来源顺序 = 用户目录 → 自带 → 插件，
    # 于是用户能覆盖自带的，而插件覆盖不了平台的（packs.py 的既定优先级）。
    skills = SkillsRegistry(
        skills_dir(),
        [PackSource(builtin_skills_dir(), "builtin")] + plugins.skill_sources(),
    )
    experts = ExpertsRegistry(experts_dir())

    def _compose_suffix() -> str:
        return "\n\n".join(
            part
            for part in (skills.catalog_text(), experts.catalog_text(), plugins.prompt_suffix())
            if part
        )

    def _plugin_policies() -> dict[str, Any]:
        """插件 allowTools + MCP 信任工具 + 知识库工具 + 用户按工具覆盖的策略。

        基础：插件放行名单 / MCP 信任 / 知识库工具 → 免审批（AllowPolicy）。
        覆盖：用户在「工具」页设置的 tool_policies 优先——
          allow→AllowPolicy、approval→DefaultPolicy、policy→清除覆盖（回退内置策略）。
        """
        names = set(plugins.allow_tools()) | set(mcp_trusted) | {"kb_search", "kb_write"}
        policies: dict[str, Any] = {name: AllowPolicy() for name in sorted(names)}
        for tool, mode in settings.tool_policies().items():
            if mode == "allow":
                policies[tool] = AllowPolicy()
            elif mode == "approval":
                policies[tool] = DefaultPolicy()
            else:  # policy：清除覆盖，走内置策略
                policies.pop(tool, None)
        return policies

    def _apply_pack_disabled() -> None:
        """把"停用的技能/专家"推到两个 registry 上并重扫（M22）。

        与工具一键禁用同一语义：停用后不在 system prompt 的技能目录里，
        `load_skill` 也读不到——不是"列出来但点不动"。
        """
        off = settings.disabled_packs()
        skills.set_disabled({k.split(":", 1)[1] for k in off if k.startswith("skill:")})
        experts.set_disabled({k.split(":", 1)[1] for k in off if k.startswith("expert:")})

    def _refresh_policies() -> int:
        """把当前策略热推到 SessionManager（含已存在的会话运行时）。返回受影响会话数。"""
        return manager.apply_extensions(
            system_suffix=_compose_suffix(), extra_policies=_plugin_policies()
        )

    def _apply_plugin_changes() -> None:
        """插件目录变化后的统一收尾（M11：上传/删除与 reload 共用同一套热更新）。"""
        plugins.scan()
        skills.set_sources(plugins.skill_sources())
        _apply_pack_disabled()
        _refresh_policies()

    manager = SessionManager(
        transcript=transcript,
        session_repo=sessions_repo,
        approval_repo=approvals,
        router=router,
        registry=registry,
        system_suffix=_compose_suffix(),
        extra_policies=_plugin_policies(),
        usage_repo=usage_repo,
        audit_repo=audit_repo,
        knowledge=knowledge,
        workspace_repo=workspaces_repo,
        project_repo=projects_repo,
        code_intel=code_intel,
    )
    project_team = ProjectTeam(manager)
    manager.agent_team = project_team
    register_agent_team_tools(registry, project_team)
    # M18 代码模式：项目服务在代码情报之后（后者已建），这里只做 state 暴露
    app.state.projects = projects_repo
    app.state.code_service = code_service
    app.state.code_intel = code_intel
    # M33 批量补功能描述：模型从哪儿来由 API 层接上（codeintel 不依赖 agent/session 层）。
    # 用"每次都重新解析"的工厂而不是缓存实例：模型路由/档位可以在设置里改，改完立刻生效。
    if code_intel is not None:
        code_intel.set_brief_provider(lambda: manager.router.resolve_default() if manager else None)

    # M18 · P3：LSP 代理（诊断回调直接写进索引 → 地图角标）
    from code_agent.codeintel.lsp import LspManager

    lsp_manager = LspManager(
        projects_repo,
        intel=code_intel,
        documentation_path=code_dir() / 'lsp-documentation.sqlite3',
        on_diagnostics=lambda project, uri, items: code_intel.record_diagnostics(project, uri, items),
    )
    app.state.lsp = lsp_manager

    # M18 · P4：任务（隔离工作树 + Plan + 审批合并）+ 任务仓储
    from code_agent.codeintel.tasks import TaskService

    tasks_repo = TaskRepo(db)
    task_service = TaskService(
        projects=projects_repo, tasks=tasks_repo, intel=code_intel, sessions=manager
    )
    app.state.tasks = tasks_repo
    app.state.task_service = task_service

    # M18 · P5：并行批次（一个需求 → N 条互不重叠的车道 → 串行合并队列）
    from code_agent.codeintel.parallel import BatchService

    batches_repo = BatchRepo(db)
    batch_service = BatchService(
        project_repo=projects_repo,
        batches=batches_repo,
        tasks=tasks_repo,
        task_service=task_service,
        sessions=manager,
    )
    app.state.batches = batches_repo
    app.state.batch_service = batch_service

    async def _code_watch_loop() -> None:
        """后台 watcher：让"外部改动"也能自动进地图（AI 编辑/编辑器保存之外的第三条入口）。

        间隔与"近期窗口"都可用环境变量收紧；只处理最近有人查看的项目，
        没人看的时候这个循环几乎不做事。
        """
        interval = float(os.environ.get("CODE_AGENT_CODE_WATCH_INTERVAL", "4"))
        within = float(os.environ.get("CODE_AGENT_CODE_WATCH_WINDOW", "90"))
        while True:
            await asyncio.sleep(interval)
            try:
                changed = await code_intel.watch_once(within_s=within)
                if changed:
                    logger.info("代码模式索引已随磁盘更新：%s", ", ".join(changed))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— watcher 绝不能因单次异常退出
                logger.exception("代码模式 watcher 单轮异常（忽略）")

    async def _lsp_reap_loop() -> None:
        """回收空闲语言服务器：没人用的进程不该一直占着内存与文件句柄。"""
        interval = float(os.environ.get("CODE_AGENT_LSP_REAP_INTERVAL", "60"))
        while True:
            await asyncio.sleep(interval)
            try:
                reaped = await lsp_manager.reap_idle()
                if reaped:
                    logger.info("已回收空闲语言服务器：%s", ", ".join(reaped))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("LSP 回收任务单轮异常（忽略）")
    app.state.skills = skills
    app.state.experts = experts
    app.state.plugins = plugins
    app.state.mcp = mcp
    app.state.artifacts = artifacts_store
    app.state.knowledge = knowledge

    app.state.db = db
    app.state.users = users
    app.state.sessions_repo = sessions_repo
    app.state.workspaces_repo = workspaces_repo
    app.state.approvals = approvals
    app.state.usage_repo = usage_repo
    app.state.audit_repo = audit_repo
    app.state.automations_repo = automations_repo
    app.state.transcript = transcript
    app.state.router = router
    app.state.registry = registry
    app.state.sessions = manager

    # M18 代码模式接口（项目 / 目录树 / 文件 / 项目会话 / 目录选择 / 索引与地图 / LSP）
    mount_code_api(
        app,
        project_repo=projects_repo,
        manager=manager,
        service=code_service,
        intel=code_intel,
        lsp=lsp_manager,
        tasks=tasks_repo,
        task_service=task_service,
        batches=batches_repo,
        batch_service=batch_service,
    )
    mount_runtime_api(app, projects_repo)
    mount_agent_team_api(app, service=code_service, team=project_team)
    mount_agent_settings_api(app)
    mount_native_dialog_api(app)
    from code_agent.codeintel.java_debug import JavaDebugProvider, JAVA_DEBUG_COMMANDS

    async def _java_session(owner, project_id, root):
        project = code_service.get_owned(owner, project_id)
        if code_service.root_of(project).resolve() != root.resolve():
            raise ValueError("Java 调试工作区已变化，请重新打开项目")
        return await lsp_manager.session_for(project, "java", server_id="jdtls")

    async def _java_command(owner, project_id, root, command, arguments, timeout):
        if command not in JAVA_DEBUG_COMMANDS:
            raise ValueError("不支持此 Java 调试命令")
        session = await _java_session(owner, project_id, root)
        return await lsp_manager.request(session, "workspace/executeCommand", {
            "command": command, "arguments": arguments,
        }, timeout=timeout)

    async def _java_acquire(owner, project_id, root):
        session = await _java_session(owner, project_id, root)
        release = lsp_manager.subscribe(session, lambda _message: None)
        return release

    async def _java_release(release):
        release()

    debug_runtime = DebugRuntime(java_provider=JavaDebugProvider(
        _java_command, acquire=_java_acquire, release=_java_release,
    ))
    app.state.ide_debug = debug_runtime
    mount_debug_api(app, service=code_service, runtime=debug_runtime)
    app.state.change_reviews = change_reviews
    mount_change_review_api(app, service=code_service, reviews=change_reviews, intel=code_intel)

    def require_admin(user: CurrentUser) -> None:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 种子管理员：保留原用户 ID，免登录不会另建身份导致历史会话消失。
    admin_user = os.environ.get("CODE_AGENT_ADMIN_USER", "admin")
    if users.count() == 0:
        admin_pass = os.environ.get("CODE_AGENT_ADMIN_PASSWORD") or "admin123"
        users.create(admin_user, hash_password(admin_pass), role="admin")

    if not requires_auth:
        local_row = users.get_by_username(admin_user)
        if not local_row or local_row["disabled"] or local_row["role"] != "admin":
            local_row = db.query_one(
                "SELECT * FROM users WHERE role='admin' AND disabled=0 "
                "ORDER BY created_at, id LIMIT 1"
            )
        if not local_row:
            # 导入的数据库可能只有普通用户；建立一个固定可复用的本地管理员。
            local_name = admin_user
            while users.get_by_username(local_name):
                local_name += "_local"
            local_row = users.create(
                local_name, hash_password(os.environ.get("CODE_AGENT_ADMIN_PASSWORD") or "admin123"),
                role="admin",
            )
        app.state.local_user = CurrentUser(
            user_id=local_row["id"], username=local_row["username"], role=local_row["role"]
        )

    # ------------------------------------------------------------------
    # 健康检查（M7 部署：docker HEALTHCHECK / nginx upstream 探活）
    # ------------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": app.version}

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------
    @app.post("/api/v1/auth/login")
    def login(body: dict[str, Any]) -> dict:
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        user = users.get_by_username(username)
        if not user or user["disabled"] or not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = create_token(user["id"], user["username"], user["role"])
        return {"token": token, "user": _public_user(user)}

    @app.get("/api/v1/auth/me")
    def me(user: CurrentUser = Depends(get_current_user)) -> dict:
        row = users.get(user.user_id)
        if not row:
            raise HTTPException(status_code=401, detail="用户不存在")
        return _public_user(row)

    # ------------------------------------------------------------------
    # settings（M13 大模型配置）
    # ------------------------------------------------------------------
    def _model_settings_view() -> dict:
        """当前生效的大模型配置视图（api_key 打码，不回传完整 key）。

        M36：只有一个「默认模型」（三档已去掉）。
        """
        router = app.state.router
        cfg = settings.model_config(provider=router.provider_name)
        api_key = cfg.get("api_key") or ""
        from code_agent.providers.capacity import effective_model_capacity

        active_model = router.model_name()
        effective = effective_model_capacity(
            active_model, cfg["context_window"], cfg["max_output_tokens"]
        )
        return {
            "provider": router.provider_name,
            "base_url": cfg["base_url"],
            "model_default": cfg["model_default"],
            "proxy_mode": cfg["proxy_mode"],
            "api_key_set": bool(api_key),
            "api_key_hint": settings.mask_key(api_key),
            "active_model": active_model,
            **{field: cfg[field] for field in settings.MODEL_TOKEN_FIELDS},
            **{f"effective_{field}": effective[field] for field in settings.MODEL_TOKEN_FIELDS},
        }

    @app.get("/api/v1/settings/model")
    def get_model_settings(user: CurrentUser = Depends(get_current_user)) -> dict:
        return _model_settings_view()

    @app.put("/api/v1/settings/model")
    def put_model_settings(
        body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """保存大模型配置（admin），写 config.json 并热生效。api_key 留空 = 不修改。"""
        require_admin(user)
        from code_agent.agent.router import KNOWN_PROVIDERS

        provider = str(body.get("provider") or app.state.router.provider_name).strip()
        if provider not in KNOWN_PROVIDERS:
            raise HTTPException(
                status_code=422,
                detail=f"未知 provider：{provider}（可选：{', '.join(KNOWN_PROVIDERS)}）",
            )

        updates: dict[str, Any] = {"provider": provider}
        if "proxy_mode" in body:
            try:
                updates["proxy_mode"] = settings.normalize_proxy_mode(body["proxy_mode"])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        for key in ("base_url", "model_default"):
            val = body.get(key)
            if val is not None:
                updates[key] = str(val).strip()
        new_key = body.get("api_key")
        if new_key:
            updates["api_key"] = str(new_key).strip()
        for field in settings.MODEL_TOKEN_FIELDS:
            if field in body:
                updates[field] = body[field]

        previous = {key: settings.load().get(key) for key in updates}
        try:
            settings.save(updates)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            app.state.router.reload()
        except ValueError as exc:
            # Invalid budgets must never leave persisted settings ahead of the
            # live router, even when a provider rejects a reload.
            settings.save(previous)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _model_settings_view()

    # ------------------------------------------------------------------
    # permissions（M14：三档权限模式）
    # ------------------------------------------------------------------
    @app.get("/api/v1/settings/permission")
    def get_permission_settings(user: CurrentUser = Depends(get_current_user)) -> dict:
        """当前默认权限档位 + 可选档位清单（前端下拉直接渲染）。"""
        from code_agent.permissions.hooks import PERMISSION_LABELS, PERMISSION_MODES

        return {
            "mode": settings.permission_mode(),
            "modes": [
                {
                    "id": m,
                    "label": PERMISSION_LABELS[m][0],
                    "desc": PERMISSION_LABELS[m][1],
                }
                for m in PERMISSION_MODES
            ],
        }

    @app.put("/api/v1/settings/permission")
    def put_permission_settings(
        body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """保存默认权限档位（admin）。只影响**新建**会话，存量会话各自记着自己的档位。"""
        require_admin(user)
        try:
            settings.save_permission_mode(str(body.get("mode") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return get_permission_settings(user)

    # ------------------------------------------------------------------
    # model profiles（M14：多 provider 档案，一键切换）
    # ------------------------------------------------------------------
    @app.get("/api/v1/model-profiles")
    def get_model_profiles(user: CurrentUser = Depends(get_current_user)) -> list[dict]:
        return settings.list_profiles()

    @app.post("/api/v1/model-profiles")
    def add_model_profile(body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        require_admin(user)
        try:
            return settings.save_profile(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/v1/model-profiles/{pid}")
    def update_model_profile(
        pid: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        require_admin(user)
        previous_profiles = settings._read_profiles()
        try:
            view = settings.save_profile({**body, "id": pid})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # 编辑的是当前生效档案 → 立即应用（与激活等价）
        if view["active"]:
            try:
                _apply_profile(pid)
            except HTTPException:
                settings._write_profiles(previous_profiles)
                raise
        return view

    @app.delete("/api/v1/model-profiles/{pid}")
    def remove_model_profile(pid: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        require_admin(user)
        try:
            settings.delete_profile(pid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/v1/model-profiles/{pid}/activate")
    def activate_model_profile(pid: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        require_admin(user)
        try:
            _apply_profile(pid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _model_settings_view()

    @app.post("/api/v1/model-profiles/probe")
    async def probe_model_profiles(
        body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """探测 OpenAI 兼容后端的可用模型（GET {base_url}/models）。

        本地后端（Ollama / LM Studio / vLLM）的模型名不该靠猜——填完 base_url
        点一下就拉回来。管理员专属，避免被当成任意 URL 探测器。
        """
        import httpx
        from code_agent.providers.network import connection_error_detail, normalize_proxy_mode

        require_admin(user)
        base_url = (body.get("base_url") or "").strip().rstrip("/")
        api_key = (body.get("api_key") or "").strip()
        pid = str(body.get("profile_id") or "").strip()
        proxy_mode = body.get("proxy_mode")
        if pid:
            # 在某条档案里点「获取可用模型」：用**那条档案的**地址与已存密钥去问。
            # 密钥不回传前端，所以只能在后端补——否则远端网关的探测永远 401。
            store = settings._read_profiles()
            prof = settings._find_profile(store, pid)
            if prof is None:
                raise HTTPException(status_code=404, detail="档案不存在")
            base_url = base_url or str(prof.get("base_url") or "").strip().rstrip("/")
            if not base_url:
                base_url = {"deepseek": "https://api.deepseek.com", "openai": "https://api.openai.com/v1",
                            "openai_compat": "https://api.openai.com/v1"}.get(prof.get("provider"), "")
            if not api_key:
                api_key = str(prof.get("api_key") or "").strip()
            if proxy_mode is None:
                proxy_mode = prof.get("proxy_mode")
        if not base_url or (not pid and body.get("use_current") is True):
            # 没给档案也没给地址：用**当前生效配置**（含已保存的密钥）
            cfg = settings.model_config(provider=app.state.router.provider_name)
            base_url = base_url or str(cfg.get("base_url") or "").strip().rstrip("/")
            if not base_url:
                base_url = {"deepseek": "https://api.deepseek.com", "openai": "https://api.openai.com/v1",
                            "openai_compat": "https://api.openai.com/v1"}.get(cfg.get("provider"), "")
            if not api_key:
                api_key = str(cfg.get("api_key") or "").strip()
            if proxy_mode is None:
                proxy_mode = cfg.get("proxy_mode")
        try:
            proxy_mode = normalize_proxy_mode(proxy_mode)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="Base URL 必须以 http:// 或 https:// 开头")
        headers = {"Authorization": f"Bearer {api_key or 'not-needed'}"}
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=proxy_mode == "system") as client:
                resp = await client.get(f"{base_url}/models", headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"连接失败：{connection_error_detail(exc, base_url, proxy_mode)}"
            ) from exc
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502, detail=f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="返回内容不是 JSON") from exc
        raw = data.get("data") if isinstance(data, dict) else None
        if raw is None and isinstance(data, dict):
            raw = data.get("models")  # Ollama 原生 /api/tags 形状兜底
        models: list[str] = []
        for item in raw or []:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("name") or item.get("model")
                if mid:
                    models.append(str(mid))
            elif isinstance(item, str):
                models.append(item)
        # M36：探测要**列出全部**模型（网关后面可能挂着几十上百个：doubao/minimax/glm…），
        # 所以这里只去重+排序，不再按"够用就好"截断——上限放到 500 只是防爆内存。
        unique = sorted({m for m in models if m})
        return {"base_url": base_url, "models": unique[:500], "total": len(unique)}

    # ------------------------------------------------------------------
    # 每条档案自己的模型清单（M40：模型目录从全局搬到档案下面）
    # ------------------------------------------------------------------
    @app.put("/api/v1/model-profiles/{pid}/models")
    def put_profile_models(
        pid: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """整份替换某条档案的模型清单。清洗：空项丢弃、按 id 去重、限 200 条。"""
        require_admin(user)
        try:
            return settings.save_profile_models(pid, (body or {}).get("items"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/model-profiles/{pid}/models/reset")
    def reset_profile_models(pid: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        """恢复默认模型：清掉这条档案自定义的模型清单。"""
        require_admin(user)
        try:
            return settings.reset_profile_models(pid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _apply_profile(pid: str) -> None:
        """激活档案：写 config.json + router 热重载；router 校验失败则回写原值。"""
        prev = {k: settings.load().get(k) for k in
                ("provider", "base_url", "model_default", "api_key", "proxy_mode",
                 "context_window", "max_output_tokens")}
        prev_active = settings._read_profiles().get("active", "")
        try:
            settings.activate_profile(pid)
            app.state.router.reload()
        except ValueError as exc:
            settings.save(prev)
            store = settings._read_profiles()
            store["active"] = prev_active
            settings._write_profiles(store)
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    @app.post("/api/v1/sessions")
    def create_session(body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        if "loop_mode" in body:
            raise HTTPException(status_code=422, detail="循环模式设置已移除，请更新客户端")
        try:
            return manager.create(
                user.user_id,
                folder=str(body.get("folder") or ""),
                permission_mode=body.get("permission_mode") or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/sessions")
    def list_sessions(user: CurrentUser = Depends(get_current_user)) -> list[dict]:
        return sessions_repo.list_for_user(user.user_id)

    @app.get("/api/v1/sessions/{session_id}")
    def get_session(session_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            return manager.get_meta(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/close")
    def close_session(session_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            manager.close(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/v1/sessions/{session_id}/rename")
    def rename_session(
        session_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        try:
            return manager.rename(session_id, user.user_id, str(body.get("title") or ""))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/folder")
    def move_session_folder(
        session_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """把会话归入某个工作区分组（侧栏按工作区分文件夹展示）。空串 = 默认工作区。"""
        try:
            return manager.set_folder(session_id, user.user_id, str(body.get("folder") or ""))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/web")
    def web_capability(user: CurrentUser = Depends(get_current_user)) -> dict:
        """联网能力探测（M19）：前端据此决定「联网」开关能不能点、不能点为什么。

        只报"当前配置能不能用"，不泄露 key（`api_key` 只用来判断有没有）。
        """
        from code_agent.web import availability

        info = availability()
        return {
            **info,
            "tools": ["web_search", "web_fetch"],
            "how_to": "检索走 DeepSeek 的 Anthropic 兼容 Messages 接口 + 原生 web_search 工具；"
                      "端点用 CODE_AGENT_SEARCH_BASE_URL / config.json 的 search.base_url 配置，"
                      "key 复用 DEEPSEEK_API_KEY（或 config.json 的 search_api_key）。",
        }

    @app.post("/api/v1/sessions/{session_id}/web")
    def set_session_web(
        session_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """切换会话「联网」开关（M19）。关着时联网工具不出现在模型可见的 schema 里。"""
        try:
            return manager.set_web_enabled(
                session_id, user.user_id, bool(body.get("enabled"))
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/permission")
    def set_session_permission(
        session_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """切换会话权限档位（read-only / workspace-write / full-access），立即生效。"""
        try:
            return manager.set_permission_mode(
                session_id, user.user_id, str(body.get("mode") or "")
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/archive")
    def archive_session(session_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            manager.archive(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/v1/sessions/{session_id}/fork")
    def fork_session(session_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        try:
            return manager.fork(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/v1/sessions/{session_id}")
    def delete_session(session_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        """删除会话：连轨迹与用量一起清掉（与归档/退出的"保留可查"不同）。"""
        try:
            manager.delete(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/v1/sessions/{session_id}/context")
    def session_context_usage(
        session_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """当前会话的上下文占用估算（系统提示词 / 工具定义 / 对话消息三段）。"""
        try:
            return manager.context_usage(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # workspaces（M14：侧栏工作区分组）
    # ------------------------------------------------------------------
    @app.get("/api/v1/workspaces")
    def list_workspaces(user: CurrentUser = Depends(get_current_user)) -> list[dict]:
        """工作区清单（含会话数）。空工作区也在列表里——这是"新建工作区"能被看见的前提。"""
        return manager.list_workspaces(user.user_id)

    @app.post("/api/v1/workspaces")
    def create_workspace(
        body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """新建工作区（可以为空；会话靠「移到工作区」或分组标题上的 + 放进来）。"""
        try:
            return manager.create_workspace(user.user_id, str(body.get("name") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/v1/workspaces/{name}")
    def rename_workspace(
        name: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """重命名工作区：其下会话的 folder 同步改，两边不会走散。"""
        try:
            return manager.rename_workspace(user.user_id, name, str(body.get("name") or ""))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/v1/workspaces/{name}")
    def delete_workspace(name: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        """删除工作区（其下会话退回默认工作区，不删会话）。"""
        try:
            manager.delete_workspace(user.user_id, name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True}

    # ------------------------------------------------------------------
    # attachments（M14：任意格式文件上传到会话工作区）
    # ------------------------------------------------------------------
    @app.post("/api/v1/sessions/{session_id}/files")
    async def upload_session_file(
        session_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """上传任意格式附件到会话工作区的 uploads/ 目录（base64 JSON，与插件上传同约定）。

        不用 multipart：免新增 python-multipart 依赖；30MB 上限对"给 agent 看
        的素材"足够，更大就该放进工作区目录而不是聊天框。
        """
        import binascii
        from code_agent.extensions.artifacts import _safe_filename

        try:
            runtime = manager.get_runtime(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        name = _safe_filename(str(body.get("name") or "attachment.bin"))
        raw_b64 = str(body.get("content_b64") or "")
        try:
            data = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail="content_b64 不是合法的 base64") from exc
        if not data:
            raise HTTPException(status_code=422, detail="附件内容为空")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=422,
                detail=f"附件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限",
            )

        uploads = runtime.workspace / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        dest = uploads / name
        if dest.exists():
            # 同名不覆盖：加序号，避免两次上传互相吞掉
            stem, dot, ext = name.partition(".")
            for i in range(1, 1000):
                cand = uploads / (f"{stem}-{i}{dot}{ext}" if dot else f"{name}-{i}")
                if not cand.exists():
                    dest = cand
                    break
        dest.write_bytes(data)
        return {"name": dest.name, "path": f"uploads/{dest.name}", "size": len(data)}

    @app.get("/api/v1/sessions/{session_id}/files")
    def list_session_files(
        session_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> list[dict]:
        """列出会话已上传的附件（uploads/ 目录实况，重启后也不丢）。"""
        try:
            runtime = manager.get_runtime(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        root = runtime.workspace / "uploads"
        if not root.exists():
            return []
        out = []
        for f in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file():
                out.append({"name": f.name, "path": f"uploads/{f.name}", "size": f.stat().st_size})
        return out
    @app.get("/api/v1/sessions/{session_id}/approvals/pending")
    def pending_session_approval(
        session_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict | list:
        """当前挂起中的审批（前端刷新/重连后恢复审批弹窗用）。

        审批请求的实时事件不落盘（seq=0 扇出），刷新后前端拿不到
        approval_id 点不了按钮——以 approvals 表为准兜底。无挂起返回 {}。
        """
        try:
            manager.get_meta(session_id, user.user_id)
            runtime = manager.get_runtime(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return runtime.pending_approval() or {}

    @app.get("/api/v1/sessions/{session_id}/events")
    def list_events(
        session_id: str, after_seq: int = 0, user: CurrentUser = Depends(get_current_user)
    ) -> list[dict]:
        try:
            manager.get_meta(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [e.model_dump() for e in transcript.replay(session_id, after_seq=after_seq)]

    @app.post("/api/v1/sessions/{session_id}/prompt")
    async def prompt_session(
        session_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """REST 提交 prompt（无 WS 场景/测试用）。同步等待最终结果。"""
        try:
            runtime = manager.get_runtime(session_id, user.user_id)
            result = await runtime.submit(
                body.get("text") or "",
                _clean_attachments(body),
                code_context=_clean_code_context(body),
                web=_clean_web_flag(body),
            )
        except SessionBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TeamError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SessionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "text": result.text,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "usage": result.usage.model_dump(),
        }

    # ------------------------------------------------------------------
    # M29 检查点 / 回退 / 验收清单
    # ------------------------------------------------------------------
    @app.get("/api/v1/sessions/{session_id}/checkpoints")
    def list_checkpoints(
        session_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """本会话的成对检查点（对话位置 + 这一步的改动）。

        前端按 `anchor_seq` 把它们挂到对应那一行上：prompt 检查点的锚点是
        那条用户消息，段末检查点的锚点是最后一个事件。
        """
        try:
            runtime = manager.get_runtime(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"checkpoints": runtime.checkpoints(), "busy": runtime.busy}

    @app.post("/api/v1/sessions/{session_id}/rewind")
    def rewind_session(
        session_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """回退到某条检查点：撤文件改动 → 截轨迹 → 写回退记忆。

        回退本身会**落一条可回放的事件**（`rewind`），因为它是事实变更：
        刷新之后轨迹已经是截断的，没有这条事件用户会以为"刚才那段凭空消失了"。
        """
        checkpoint_id = str(body.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            raise HTTPException(status_code=422, detail="checkpoint_id 不能为空")
        try:
            runtime = manager.get_runtime(session_id, user.user_id)
            result = runtime.rewind(checkpoint_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SessionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        payload = {
            "text": result.get("report") or "",
            "rewind": True,
            "checkpoint": checkpoint_id,
            "keep_seq": result.get("keep_seq"),
            "reverted": len(result.get("reverted") or []),
            "failed": len(result.get("failed") or []),
        }
        # 用 assistant_message 而不是 session_meta：这份报告（撤了什么、哪些没撤成）
        # 是**事实**，刷新之后必须还在页面上。session_meta 会被前端当状态帧丢掉。
        runtime._emit_persisted(EventType.assistant_message, payload)  # noqa: SLF001（同包内落事件）
        return {
            "ok": True,
            "keep_seq": result.get("keep_seq"),
            "reverted": result.get("reverted") or [],
            "failed": result.get("failed") or [],
            "dropped_events": result.get("dropped_events") or 0,
            "report": result.get("report") or "",
            "note": result.get("note") or "",
        }

    @app.get("/api/v1/sessions/{session_id}/acceptance")
    def get_acceptance(
        session_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """有序验收清单 + 当前判定（进度 ρ=ℓ/n、第一个未满足项）。"""
        try:
            runtime = manager.get_runtime(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return runtime.acceptance()

    @app.put("/api/v1/sessions/{session_id}/acceptance")
    def put_acceptance(
        session_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """声明/覆盖有序验收清单（2~6 条，check 类型见 GET 的返回）。"""
        try:
            runtime = manager.get_runtime(session_id, user.user_id)
            return runtime.set_acceptance(body.get("items") or [], str(body.get("note") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/v1/sessions/{session_id}/acceptance")
    def delete_acceptance(
        session_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        try:
            runtime = manager.get_runtime(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": runtime.clear_acceptance()}

    # ------------------------------------------------------------------
    # M50 后台任务：给人看的面（只读）
    # ------------------------------------------------------------------
    @app.get("/api/v1/sessions/{session_id}/jobs")
    def list_session_jobs(
        session_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """本会话的后台任务清单（只读，**不含输出正文**）。

        为什么需要它：`jobs`（M45）落地时只有**模型**看得见（`job_list`/`job_output`），
        人这边一片空白——用户看到会话"在忙"却不知道在忙什么，后台任务于是变成黑盒。
        这个端点把名册摊开：有哪几个、什么状态、产出了多少字节、有没有因为内存上限丢块。

        **输出正文不给**：那是模型的通道（带消费游标与"哪段丢了"的说明），而且可能很大
        （环上限 256 KiB/任务）。要正文就让模型读，或去 `tool_results/` 拿落盘产物。
        """
        try:
            runtime = manager.get_runtime(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        jobs = runtime.jobs.snapshot(session_id)
        return {
            "jobs": jobs,
            "running": sum(1 for j in jobs if j["status"] == "running"),
        }

    # ------------------------------------------------------------------
    # M31 提问：回答模型挂起中的问题
    # ------------------------------------------------------------------
    @app.get("/api/v1/sessions/{session_id}/questions/pending")
    def pending_session_questions(
        session_id: str, user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """Restore only questions whose in-process answer waiter is still alive."""
        try:
            manager.get_meta(session_id, user.user_id)
            runtime = manager.get_runtime(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"questions": runtime.pending_questions()}

    @app.post("/api/v1/sessions/{session_id}/questions/{question_id}/answer")
    async def answer_question(
        session_id: str,
        question_id: str,
        body: dict[str, Any],
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """把用户的回答送达挂起中的提问。

        答案形状（前端按 DSH 的口径组装，服务端只做边界清洗）：
            {"answers": [{"id": "q1", "selected": ["A. ..."], "custom": "..."}]}
        放弃整组提问传 `{"dismiss": true}`：模型会收到"用户放弃了这组提问"。

        没命中挂起中的提问（服务重启 / 已超时 / 这一轮已被停止）→ **409**，
        而不是假装收下：答案丢了却回 200 是最难查的一类问题。
        """
        answers = body.get("answers")
        if body.get("dismiss"):
            payload: Any = False
        elif isinstance(answers, list):
            payload = [
                {
                    "id": str(a.get("id") or ""),
                    "selected": [str(s) for s in (a.get("selected") or []) if str(s).strip()],
                    **({"custom": str(a["custom"])[:4000]} if a.get("custom") else {}),
                }
                for a in answers
                if isinstance(a, dict)
            ]
        else:
            raise HTTPException(status_code=422, detail="answers 必须是数组（或传 dismiss=true 放弃）")

        try:
            runtime = manager.get_runtime(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not runtime.answer_question(question_id, payload):
            raise HTTPException(
                status_code=409,
                detail="这次提问已经结束了（超时、被停止，或服务重启过），重新发一条消息继续。",
            )
        return {"ok": True}

    # ------------------------------------------------------------------
    # artifacts（M4 s20 文件交付）
    # ------------------------------------------------------------------
    @app.get("/api/v1/sessions/{session_id}/artifacts")
    def list_artifacts(session_id: str, user: CurrentUser = Depends(get_current_user)) -> list[dict]:
        try:
            manager.get_meta(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return app.state.artifacts.list(session_id)

    @app.get("/api/v1/sessions/{session_id}/artifacts/{filename}")
    def get_artifact(session_id: str, filename: str, user: CurrentUser = Depends(get_current_user)):
        from fastapi.responses import FileResponse

        try:
            manager.get_meta(session_id, user.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        from code_agent.extensions.artifacts import _safe_filename

        path = app.state.artifacts.root / _safe_filename(session_id) / _safe_filename(filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="artifact 不存在")
        return FileResponse(path, filename=path.name)

    # ------------------------------------------------------------------
    # approvals
    # ------------------------------------------------------------------
    @app.post("/api/v1/approvals/{approval_id}/decide")
    async def decide_approval(
        approval_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        approved = body.get("approved")
        if not isinstance(approved, bool):
            raise HTTPException(status_code=422, detail="approved 必须是 true 或 false")
        try:
            hit = manager.decide_approval(user.user_id, approval_id, approved)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "delivered": hit}

    # ------------------------------------------------------------------
    # usage（M5 s21）
    # ------------------------------------------------------------------
    @app.get("/api/v1/usage/me")
    def my_usage(user: CurrentUser = Depends(get_current_user)) -> dict:
        return usage_repo.summary_for_user(user.user_id)

    # ------------------------------------------------------------------
    # audit（M5 s22，admin only）
    # ------------------------------------------------------------------
    @app.get("/api/v1/admin/audit")
    def audit_list(
        limit: int = 200, after_seq: int = 0, user: CurrentUser = Depends(get_current_user)
    ) -> list[dict]:
        require_admin(user)
        return audit_repo.list_entries(limit=limit, after_seq=after_seq)

    @app.get("/api/v1/admin/audit/verify")
    def audit_verify(user: CurrentUser = Depends(get_current_user)) -> dict:
        require_admin(user)
        return audit_repo.verify()

    # ------------------------------------------------------------------
    # tools / plugins（M10 工具入口）
    # ------------------------------------------------------------------
    def _catalog(can_edit: bool = False) -> dict:
        cat = build_catalog(
            registry=registry,
            skills=skills,
            experts=experts,
            plugins=plugins,
            mcp=mcp,
            extra_policies=manager.extra_policies,
            code_tool_status=code_tools_state["status"],
            code_tool_errors=code_tools_state["errors"],
        )
        cat["can_edit"] = can_edit  # 管理员可在页面上改工具审批策略
        return cat

    #: 工具级审批策略三档（后端为唯一真源，前端直接渲染）
    tool_policy_modes = [
        {"id": "allow", "label": "免审批", "desc": "该工具直接放行，不再人工确认"},
        {"id": "approval", "label": "需审批", "desc": "每次调用都需人工确认（保守）"},
        {"id": "policy", "label": "按参数判定", "desc": "用内置策略按参数决定（如 bash 黑/白名单）"},
    ]

    @app.get("/api/v1/permissions")
    def list_permissions(user: CurrentUser = Depends(get_current_user)) -> dict:
        """工具审批策略一览（普通用户只读，改需管理员）。"""
        overrides = settings.tool_policies()
        items = []
        for t in _catalog()["tools"]:
            items.append(
                {
                    "name": t["name"],
                    "source": t["source"],
                    "policy": t["policy"],
                    "policy_label": t["policy_label"],
                    "override": overrides.get(t["name"]),
                }
            )
        return {
            "tools": items,
            "modes": tool_policy_modes,
            "can_edit": user.role == "admin",
        }

    @app.put("/api/v1/permissions/{tool_name}")
    def set_permission(
        tool_name: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """设置某工具的审批策略（管理员）。mode=policy 清除覆盖、回退内置策略。"""
        require_admin(user)
        mode = (body.get("mode") or "").strip()
        if mode not in ("allow", "approval", "policy"):
            raise HTTPException(status_code=422, detail="mode 只能是 allow / approval / policy")
        if find_tool(_catalog(), tool_name) is None:
            raise HTTPException(status_code=404, detail=f"工具不存在: {tool_name}")
        try:
            settings.set_tool_policy(tool_name, mode)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        affected = _refresh_policies()  # 热生效到已存在的会话运行时
        return {
            "ok": True,
            "tool": tool_name,
            "mode": mode,
            "active_sessions_updated": affected,
        }

    @app.get("/api/v1/tools")
    def list_tools(user: CurrentUser = Depends(get_current_user)) -> dict:
        """统一能力清单：内置工具 / MCP 工具 / 插件工具 + 技能 / 专家 / 插件 / MCP 状态。

        每个工具带 `enabled`（M21）：禁用后模型看不到它、也调不动。
        """
        cat = _catalog(can_edit=user.role == "admin")
        off = set(settings.disabled_tools())
        for t in cat["tools"]:
            t["enabled"] = t["name"] not in off
            # 这个来源的工具能不能删/改：内置的只能禁用（代码自带），
            # MCP 的在 MCP 配置里改，插件的删插件即删工具
            t["editable"] = t["source"] == "mcp"
            t["deletable"] = t["source"] in ("mcp", "plugin")
            t["edit_hint"] = {
                "builtin": "内置工具：代码自带，只能禁用/启用",
                "mcp": "在「配置 MCP」里改命令行或删这一行",
                "plugin": "来自插件：删掉插件即删掉它的工具",
            }.get(t["source"], "")
        cat["disabled_tools"] = sorted(off)
        cat["summary"] = {**cat.get("summary", {}), "disabled": len(off)}
        return cat

    @app.post("/api/v1/tools/{tool_name}/enabled")
    def set_tool_enabled(
        tool_name: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """一键启用/禁用某个工具（管理员）。

        禁用 = 从模型可见的 schema 里摘掉 + 注册表层拒调（`FilteredToolRegistry`），
        比"改审批策略"更彻底：策略只管要不要人工确认，禁用是这个工具不存在。
        """
        require_admin(user)
        if find_tool(_catalog(), tool_name) is None:
            raise HTTPException(status_code=404, detail=f"工具不存在: {tool_name}")
        enabled = bool(body.get("enabled"))
        try:
            off = settings.set_tool_enabled(tool_name, enabled)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "tool": tool_name, "enabled": enabled, "disabled_tools": off}

    @app.post("/api/v1/tools/enabled")
    def set_tools_enabled(
        body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """批量启用/禁用（「一键禁用全部」/「全部启用」）。

        body: {"enabled": bool, "tools": [...]}；tools 省略 = 当前清单里的全部工具。
        """
        require_admin(user)
        enabled = bool(body.get("enabled"))
        names = body.get("tools")
        if not isinstance(names, list) or not names:
            if enabled:
                names = []          # 空 + enabled=True = 清空禁用清单
            else:
                names = [t["name"] for t in _catalog()["tools"]]
        known = {t["name"] for t in _catalog()["tools"]}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise HTTPException(status_code=404, detail=f"工具不存在: {', '.join(unknown[:5])}")
        off = settings.set_tools_enabled(list(names), enabled)
        return {"ok": True, "enabled": enabled, "count": len(names), "disabled_tools": off}

    @app.get("/api/v1/tools/{tool_name}")
    def get_tool(tool_name: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        item = find_tool(_catalog(), tool_name)
        if item is None:
            raise HTTPException(status_code=404, detail=f"工具不存在: {tool_name}")
        return item

    @app.get("/api/v1/plugins")
    def list_plugins(user: CurrentUser = Depends(get_current_user)) -> list[dict]:
        return [m.to_dict() for m in plugins.list()]

    @app.post("/api/v1/plugins/reload")
    def reload_plugins(user: CurrentUser = Depends(get_current_user)) -> dict:
        """重新扫描插件目录，并把技能目录 / prompt / 放行名单热更新到活跃会话。

        注意：MCP 服务器的新增或移除需要起停子进程，不在热更新范围内，需重启服务。
        """
        require_admin(user)
        before = {m.name for m in plugins.enabled()}
        plugins.scan()
        skills.set_sources(plugins.skill_sources())
        affected = manager.apply_extensions(
            system_suffix=_compose_suffix(), extra_policies=_plugin_policies()
        )
        after = {m.name for m in plugins.enabled()}
        mcp_declared = set(plugins.mcp_servers())
        return {
            "plugins": len(plugins.list()),
            "enabled": sorted(after),
            "added": sorted(after - before),
            "removed": sorted(before - after),
            "skills": skills.names(),
            "active_sessions_updated": affected,
            "mcp_restart_required": bool(mcp_declared - set(mcp.load_config())),
        }

    @app.post("/api/v1/plugins/{plugin_name}/toggle")
    def toggle_plugin(
        plugin_name: str,
        body: dict[str, Any],
        user: CurrentUser = Depends(get_current_user),
    ) -> dict:
        """启用/停用插件：写回 plugin.json，并热更新技能目录 / prompt / 放行名单。"""
        require_admin(user)
        try:
            manifest = plugins.set_enabled(plugin_name, bool(body.get("enabled", True)))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        skills.set_sources(plugins.skill_sources())
        affected = manager.apply_extensions(
            system_suffix=_compose_suffix(), extra_policies=_plugin_policies()
        )
        return {
            "plugin": manifest.to_dict(),
            "skills": skills.names(),
            "active_sessions_updated": affected,
            "mcp_restart_required": bool(plugins.mcp_servers()),
        }

    # ------------------------------------------------------------------
    # 能力制作与上传（M11）：插件 zip / 技能 / MCP 配置
    # ------------------------------------------------------------------
    @app.post("/api/v1/plugins/upload")
    def upload_plugin(body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        """上传插件包 zip（base64 走 JSON，避免引入 multipart 依赖）。

        服务端做防 zip-slip / 大小上限 / 顶层白名单校验，见 extensions/authoring.py。
        """
        require_admin(user)
        try:
            data = base64.b64decode(str(body.get("content_b64") or ""), validate=True)
            result = extract_plugin_zip(
                data,
                plugins_dir(),
                zip_name=str(body.get("filename") or ""),
                overwrite=bool(body.get("overwrite")),
            )
        except AuthoringError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _apply_plugin_changes()
        manifest = plugins.get(result["name"])
        return {
            "plugin": manifest.to_dict() if manifest else None,
            "files": result["files"],
            "skills": skills.names(),
        }

    @app.delete("/api/v1/plugins/{plugin_name}")
    def remove_plugin(
        plugin_name: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """删除插件目录并热更新（上传错了可以删掉重来）。"""
        require_admin(user)
        manifest = plugins.get(plugin_name)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"插件不存在: {plugin_name}")
        try:
            trashed = delete_plugin_dir(plugins_dir(), manifest.path)
        except AuthoringError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _apply_plugin_changes()
        return {
            "ok": True,
            "deleted": plugin_name,
            "skills": skills.names(),
            "trashed_as": trashed["trashed_as"],
            "note": "目录已移入 plugins/.trash/ 回收站，可手动找回或清理",
        }

    @app.post("/api/v1/skills/upload")
    def upload_skill(body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        """创建/覆盖一个本地技能（skills/<name>/SKILL.md），frontmatter 统一生成。"""
        require_admin(user)
        try:
            result = write_skill_file(
                skills_dir(),
                body.get("name"),
                body.get("description"),
                body.get("content"),
                resources=body.get("resources"),
            )
        except AuthoringError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        skills.rescan()
        _refresh_policies()
        return {"skill": result, "skills": skills.names()}

    @app.post("/api/v1/skills/import")
    def import_skill_zip(
        body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """整体导入技能包（zip）：技能目录打包，根含 SKILL.md，可带 scripts/、references/、assets/。"""
        require_admin(user)
        try:
            data = base64.b64decode(str(body.get("content_b64") or ""), validate=True)
            result = extract_skill_zip(
                data,
                skills_dir(),
                zip_name=str(body.get("filename") or ""),
                overwrite=bool(body.get("overwrite")),
            )
        except AuthoringError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        skills.rescan()
        _refresh_policies()
        return {"skill": result, "skills": skills.names()}

    @app.post("/api/v1/packs/{kind}/{name}/enabled")
    def set_pack_enabled(
        kind: str, name: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """启用/停用一个技能或专家（M22）。停用 = 不进技能目录、load_skill 也读不到。"""
        require_admin(user)
        if kind not in ("skill", "expert"):
            raise HTTPException(status_code=422, detail="kind 只能是 skill / expert")
        pack = skills if kind == "skill" else experts
        enabled = bool(body.get("enabled"))
        # 存在性判断要用**完整清单**（含已停用的）：停用的东西必须还能被启用回来。
        # 真机踩到：以前拿 names()（只有启用的）判断，于是"停用后再启用"直接 404。
        known = {i["name"] for i in pack.list()}
        if not enabled and name not in known:
            raise HTTPException(status_code=404, detail=f"{kind} 不存在：{name}")
        if enabled and name not in known:
            raise HTTPException(status_code=404, detail=f"{kind} 不存在：{name}")
        try:
            off = settings.set_pack_enabled(kind, name, enabled)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _apply_pack_disabled()
        return {
            "ok": True,
            "kind": kind,
            "name": name,
            "enabled": enabled,
            "disabled_packs": off,
            # 停用后 system prompt 的目录也变了：热推到活跃会话（否则模型还以为它可用）
            "active_sessions_updated": manager.apply_extensions(system_suffix=_compose_suffix()),
        }

    @app.delete("/api/v1/packs/{kind}/{name}")
    def delete_pack(
        kind: str, name: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """删除（卸载）一个技能或专家：整个目录移入 `.trash/`（可手动找回）。

        与插件删除同一套回收站约定——**不真删**，因为技能目录是用户的东西，
        而且删除是不可逆操作。插件带来的技能不在这里删（去停用/删除插件）。
        """
        require_admin(user)
        if kind not in ("skill", "expert"):
            raise HTTPException(status_code=422, detail="kind 只能是 skill / expert")
        pack = skills if kind == "skill" else experts
        path = pack.path_of(name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"{kind} 不存在：{name}")
        if pack.origin(name).startswith("plugin:"):
            raise HTTPException(
                status_code=422,
                detail=f"{name} 来自插件，请在「插件」里停用或删除该插件",
            )
        root = skills_dir() if kind == "skill" else experts_dir()
        trash = Path(root) / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = trash / f"{path.name}-{stamp}"
        try:
            shutil.move(str(path), str(target))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"移入回收站失败：{exc}") from exc
        pack.rescan()
        _refresh_policies()
        return {"ok": True, "kind": kind, "name": name, "moved_to": str(target), "trash": str(trash)}

    @app.post("/api/v1/mcp/{name}/enabled")
    def set_mcp_enabled(
        name: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """启用/停用一个 MCP server（M22）：写 mcp.json 的 enabled 字段。

        MCP 子进程是**启动时**连的，所以停用/启用都需要重启服务才真正生效——
        接口如实返回 restart_required，而不是假装已经生效。
        """
        require_admin(user)
        servers = load_mcp_config(mcp_config_path())
        if name not in servers:
            declared = sorted(plugins.mcp_servers().keys())
            if name in declared:
                raise HTTPException(
                    status_code=422, detail=f"{name} 由插件声明，请停用/删除该插件"
                )
            raise HTTPException(status_code=404, detail=f"MCP server 不存在：{name}")
        enabled = bool(body.get("enabled"))
        servers[name] = {**servers[name], "enabled": enabled}
        try:
            clean = normalize_mcp_servers(servers)
        except AuthoringError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        save_mcp_config(mcp_config_path(), clean)
        return {
            "ok": True,
            "name": name,
            "enabled": enabled,
            "restart_required": True,
            "note": "MCP 连接在启动时建立：重启服务后生效",
        }

    @app.delete("/api/v1/mcp/{name}")
    def delete_mcp(name: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        """删掉一个 MCP server（整行从 mcp.json 移除）。重启服务后断开。"""
        require_admin(user)
        servers = load_mcp_config(mcp_config_path())
        if name not in servers:
            if name in plugins.mcp_servers():
                raise HTTPException(status_code=422, detail=f"{name} 由插件声明，请删除该插件")
            raise HTTPException(status_code=404, detail=f"MCP server 不存在：{name}")
        servers.pop(name)
        try:
            clean = normalize_mcp_servers(servers)
        except AuthoringError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        save_mcp_config(mcp_config_path(), clean)
        return {"ok": True, "name": name, "restart_required": True, "remaining": sorted(clean)}

    @app.get("/api/v1/mcp/config")
    def get_mcp_config(user: CurrentUser = Depends(get_current_user)) -> dict:
        """MCP 配置：本地 mcp.json 可编辑部分 + 插件声明部分（只读，改插件清单去）。"""
        editable = load_mcp_config(mcp_config_path())
        from_plugins = {
            name: cfg for name, cfg in plugins.mcp_servers().items() if name not in editable
        }
        return {
            "path": str(mcp_config_path()),
            "servers": editable,
            "from_plugins": sorted(from_plugins),
            "connected": {s["name"]: s["connected"] for s in mcp.catalog()},
        }

    @app.put("/api/v1/mcp/config")
    def put_mcp_config(body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        """整份改写 mcp.json。逐 server 校验，坏配置整份不写。

        MCP 连接在启动时建立，改完需重启服务生效（接口如实返回 restart_required）。
        """
        require_admin(user)
        previous = normalize_mcp_servers(load_mcp_config(mcp_config_path()))
        try:
            clean = save_mcp_config(mcp_config_path(), body.get("servers") or {})
        except AuthoringError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "ok": True,
            "servers": clean,
            "restart_required": clean != previous,
            "note": "MCP 连接在服务启动时建立，重启服务后新配置才会连接",
        }

    # ------------------------------------------------------------------
    # knowledge（M12/M14 知识库 · LLM wiki）
    # ------------------------------------------------------------------
    @app.get("/api/v1/knowledge")
    def list_knowledge(
        origin: str | None = None, user: CurrentUser = Depends(get_current_user)
    ) -> list[dict]:
        """列文档。origin=human|llm 按区筛选；省略则两区合并（各自带 origin）。"""
        return knowledge.list_docs(origin)

    @app.get("/api/v1/knowledge/stats")
    def knowledge_stats(user: CurrentUser = Depends(get_current_user)) -> dict:
        return knowledge.stats()

    @app.get("/api/v1/knowledge/search")
    async def search_knowledge(
        q: str = "",
        top_k: int = 5,
        origin: str | None = None,
        user: CurrentUser = Depends(get_current_user),
    ) -> list[dict]:
        return await knowledge.search(q, top_k=top_k, origin=origin)

    @app.post("/api/v1/knowledge/{doc_id}/promote")
    def promote_knowledge(doc_id: str, user: CurrentUser = Depends(get_current_user)) -> dict:
        """采纳：把隔离区条目移入正式区（人工审核通过）。"""
        require_admin(user)
        doc = knowledge.promote(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="隔离区没有该文档")
        return doc

    @app.post("/api/v1/knowledge/{doc_id}/compile")
    async def compile_knowledge(
        doc_id: str, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        """用 LLM 把一篇资料编译成结构化 wiki 页（按主题拆页），产物进隔离区。"""
        require_admin(user)
        src = knowledge.get_doc(doc_id)
        if src is None:
            raise HTTPException(status_code=404, detail="知识文档不存在")
        try:
            provider = router.resolve_default()
            pages = await compile_document(src["content"], provider, title=src["title"])
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"LLM 编译失败：{exc}") from exc
        created = [
            knowledge.create(
                p["title"], p["content"], ["编译"], origin=AREA_LLM, source=f"compile:{doc_id}"
            )
            for p in pages
        ]
        return {
            "pages": len(created),
            "docs": [{"id": d["id"], "title": d["title"]} for d in created],
        }

    @app.get("/api/v1/knowledge/{doc_id}")
    def get_knowledge(
        doc_id: str, origin: str | None = None, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        doc = knowledge.get_doc(doc_id, origin)
        if doc is None:
            raise HTTPException(status_code=404, detail="知识文档不存在")
        return doc

    @app.post("/api/v1/knowledge")
    def create_knowledge(body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        require_admin(user)
        title = (body.get("title") or "").strip()
        content = (body.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=422, detail="content 必填")
        return knowledge.create(title or "未命名文档", content, body.get("tags") or [])

    @app.put("/api/v1/knowledge/{doc_id}")
    def update_knowledge(
        doc_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        require_admin(user)
        title = (body.get("title") or "").strip()
        content = (body.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=422, detail="content 必填")
        doc = knowledge.update(
            doc_id, title or "未命名文档", content, body.get("tags") or [], body.get("origin")
        )
        if doc is None:
            raise HTTPException(status_code=404, detail="知识文档不存在")
        return doc

    @app.delete("/api/v1/knowledge/{doc_id}")
    def delete_knowledge(
        doc_id: str, origin: str | None = None, user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        require_admin(user)
        if not knowledge.delete(doc_id, origin):
            raise HTTPException(status_code=404, detail="知识文档不存在")
        return {"ok": True}

    @app.post("/api/v1/knowledge/import")
    def import_knowledge(body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        """导入知识：本地目录扫描（{dir}）或文件上传（{files: [{filename, content_b64}]}）。"""
        require_admin(user)
        files = body.get("files")
        directory = (body.get("dir") or "").strip()
        if files:
            imported: list[str] = []
            errors: list[str] = []
            for f in files:
                filename = str(f.get("filename") or "").strip()
                try:
                    data = base64.b64decode(str(f.get("content_b64") or ""), validate=True)
                    doc = knowledge.import_file(
                        filename or "upload.md", data.decode("utf-8", errors="replace")
                    )
                    imported.append(doc["id"])
                except Exception as exc:
                    errors.append(f"{filename or '?'}: {exc}")
            return {"imported": len(imported), "docs": imported, "errors": errors}
        if directory:
            try:
                return knowledge.import_dir(directory)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise HTTPException(status_code=422, detail="需要提供 dir 或 files 之一")

    # ------------------------------------------------------------------
    # memory（记忆板块）
    # ------------------------------------------------------------------
    from code_agent.api.memory_api import mount_memory_api

    mount_memory_api(app, manager, sessions_repo, projects_repo)

    # ------------------------------------------------------------------
    # automations（M5 s23）
    # ------------------------------------------------------------------
    @app.post("/api/v1/automations")
    def create_automation(body: dict[str, Any], user: CurrentUser = Depends(get_current_user)) -> dict:
        name = (body.get("name") or "").strip()
        prompt = (body.get("prompt") or "").strip()
        schedule_type = body.get("schedule_type") or "recurring"
        rrule = body.get("rrule")
        scheduled_at = body.get("scheduled_at")
        if not name or not prompt:
            raise HTTPException(status_code=422, detail="name 和 prompt 必填")
        if schedule_type not in ("once", "recurring"):
            raise HTTPException(status_code=422, detail="schedule_type 只能是 once/recurring")
        if schedule_type == "once" and not scheduled_at:
            raise HTTPException(status_code=422, detail="once 型必须提供 scheduled_at")
        if schedule_type == "recurring":
            if not rrule:
                raise HTTPException(status_code=422, detail="recurring 型必须提供 rrule")
            # 先验证 rrule 可解析，再落库（否则 422 时残留脏数据）
            if compute_initial_next_run("recurring", rrule, None, _now_iso()) is None:
                raise HTTPException(
                    status_code=422,
                    detail="rrule 无法解析（支持 FREQ/INTERVAL/BYHOUR/BYMINUTE/BYDAY/BYMONTHDAY/BYMONTH）",
                )
        row = automations_repo.create(user.user_id, name, prompt, schedule_type, rrule, scheduled_at)
        if schedule_type == "once":
            # next_run_at 必须真正落库，调度器靠它判定到期
            automations_repo.set_next_run(row["id"], scheduled_at)
            row["next_run_at"] = scheduled_at
        else:
            next_at = compute_initial_next_run(schedule_type, rrule, None, row["created_at"])
            automations_repo.set_next_run(row["id"], next_at)
            row["next_run_at"] = next_at
        return row

    @app.get("/api/v1/automations")
    def list_automations(user: CurrentUser = Depends(get_current_user)) -> list[dict]:
        return automations_repo.list_for_user(user.user_id)

    @app.post("/api/v1/automations/{automation_id}/status")
    def set_automation_status(
        automation_id: str, body: dict[str, Any], user: CurrentUser = Depends(get_current_user)
    ) -> dict:
        row = automations_repo.get(automation_id)
        if row is None or row["user_id"] != user.user_id:
            raise HTTPException(status_code=404, detail="自动化不存在或无权访问")
        status = body.get("status")
        if status not in ("active", "paused"):
            raise HTTPException(status_code=422, detail="status 只能是 active/paused")
        if status == "active" and row["schedule_type"] == "recurring":
            next_at = compute_initial_next_run("recurring", row["rrule"], None, row["created_at"])
            automations_repo.mark_run(automation_id, next_at, status="active")
            if next_at is None:
                raise HTTPException(status_code=422, detail="rrule 无法解析，无法恢复")
            row["next_run_at"] = next_at
        else:
            automations_repo.update_status(automation_id, status)
        row["status"] = status
        return row

    # ------------------------------------------------------------------
    # WebSocket 流式通道
    # ------------------------------------------------------------------
    @app.websocket("/api/v1/sessions/{session_id}/stream")
    async def stream(websocket: WebSocket, session_id: str) -> None:
        user = get_ws_user(websocket)
        if user is None:
            await websocket.close(code=4401, reason="unauthorized")
            return
        try:
            runtime: SessionRuntime = manager.get_runtime(session_id, user.user_id)
        except KeyError:
            await websocket.close(code=4404, reason="session not found")
            return
        except SessionClosed:
            await websocket.close(code=4409, reason="session closed")
            return

        await websocket.accept()
        # 断线补齐：客户端带 after_seq 先补历史再进实时流
        after_seq = int(websocket.query_params.get("after_seq") or 0)
        for ev in transcript.replay(session_id, after_seq=after_seq):
            await websocket.send_json(ev.model_dump())
        await websocket.send_json(
            {
                "seq": 0,
                "type": "session_meta",
                "ts": 0,
                "payload": {
                    "session_id": session_id,
                    "model": runtime.model_name,
                    "busy": bool(runtime.busy),
                    "phase": runtime.phase if runtime.busy else "",
                    "pending_questions": runtime.pending_questions(),
                    "pending_approval": runtime.pending_approval(),
                    # 联网开关（M19）：前端输入框上的开关据此恢复状态
                    "web_enabled": bool(runtime.web_enabled),
                },
            }
        )

        queue = runtime.subscribe()
        try:
            task: Any = None
            # M6 超时兜底：客户端消息/事件等待都设上限，杜绝服务端无限挂起
            idle_timeout = float(os.environ.get("CODE_AGENT_WS_IDLE_TIMEOUT", "120"))
            last_pong = asyncio.get_event_loop().time()
            while True:
                # 两路等待：客户端消息 OR 运行时事件（带空闲超时）
                recv = asyncio.create_task(websocket.receive_json())
                ev_wait = asyncio.create_task(queue.get())
                try:
                    done, pending = await asyncio.wait(
                        {recv, ev_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=idle_timeout,
                    )
                finally:
                    for p in (recv, ev_wait):
                        if not p.done():
                            p.cancel()
                # 空闲超时：无事件也无客户端消息 -> 发心跳探活，避免静默挂死
                if not done:
                    now = asyncio.get_event_loop().time()
                    if now - last_pong > idle_timeout * 2:
                        # 长时间无任何交互，主动断开，释放订阅
                        await websocket.close(code=4400, reason="idle timeout")
                        return
                    continue

                if recv in done:
                    try:
                        msg = recv.result()
                    except WebSocketDisconnect:
                        break
                    mtype = msg.get("type")
                    if mtype == "prompt":
                        if runtime.busy:
                            await websocket.send_json(
                                {
                                    "seq": 0,
                                    "type": "error",
                                    "ts": 0,
                                    "payload": {
                                        "code": "busy",
                                        "message": "会话正在执行中：可能正在准备上下文、等待审批或模型输出。可在输入框右侧点「停止」中断当前任务。",
                                    },
                                }
                            )
                        else:
                            text = msg.get("text") or ""
                            task = asyncio.create_task(
                                runtime.submit(
                                    text,
                                    _clean_attachments(msg),
                                    code_context=_clean_code_context(msg),
                                    # 联网开关随消息下发（M19）：前端每次发消息都带当前状态，
                                    # 避免"刚点开关就发送"时后端还没收到设置的竞态
                                    web=_clean_web_flag(msg),
                                )
                            )
                    elif mtype == "cancel":
                        runtime.cancel()
                    elif mtype == "ping":
                        await websocket.send_json({"seq": 0, "type": "session_meta", "ts": 0,
                                                   "payload": {"pong": True, "busy": bool(runtime.busy),
                                                               "phase": runtime.phase if runtime.busy else ""}})
                        last_pong = asyncio.get_event_loop().time()
                if ev_wait in done:
                    ev: Event = ev_wait.result()
                    await websocket.send_json(ev.model_dump())
                    last_pong = asyncio.get_event_loop().time()
                # submit 任务异常兜底：不让异常静默丢在 task 里
                if task is not None and task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        await websocket.send_json(
                            {"seq": 0, "type": "error", "ts": 0, "payload": {"message": str(exc)}}
                        )
                        task = None
        except WebSocketDisconnect:
            pass
        finally:
            runtime.unsubscribe(queue)

    # All API and WebSocket routes must precede the optional SPA catch-all.
    mount_workbench(app)
    return app


def _public_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "created_at": row["created_at"],
    }
