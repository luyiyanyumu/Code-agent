"""权限钩子与审批流（s04）。

PermissionManager 是 agent loop 唯一的安全入口：
    decision = await permissions.evaluate(tool_name, arguments, ctx)
    if decision.decision == need_approval:
        ok = await permissions.request_approval(...)   # approver 回调

approver 是异步回调：(ApprovalRequest) -> bool
- M1 CLI：控制台 y/n
- M2 Web：写 approvals 表 + WS 推送，等用户点按钮
- 测试：脚本化返回值
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from code_agent.models.events import new_id
from code_agent.models.approval import APPROVAL_ARGUMENT, approval_details, executable_arguments
from code_agent.permissions.policy import (
    AllowPolicy,
    BashPolicy,
    BatchWriteFilePolicy,
    ContractGuardPolicy,
    Decision,
    DefaultPolicy,
    PolicyResult,
    ReadOnlyPolicy,
    SessionStatePolicy,
    WriteFilePolicy,
    describe_policy,
)
from code_agent.tools.base import ToolContext

Approver = Callable[["ApprovalRequest"], Awaitable[bool]]

#: 权限档位（与 harness 的 read-only / workspace-write / full-access 对齐）
PERMISSION_MODES = ("read-only", "workspace-write", "full-access")

#: 档位展示信息（后端是唯一真源，前端下拉直接渲染，避免两边文案漂移）
PERMISSION_LABELS: dict[str, tuple[str, str]] = {
    "read-only": ("只读", "默认只读；交互会话可为写入与命令申请单次授权"),
    "workspace-write": ("工作区写入", "工作区内自由读写，越界写与命令执行需审批"),
    "full-access": ("完全权限", "免审批直通，agent 全自动执行（谨慎使用）"),
}

# ----------------------------------------------------------------------
# 内置工具的权限策略登记表 —— **唯一真源**
# ----------------------------------------------------------------------
# 为什么要有这张表：策略真相此前散在三处，而且互相矛盾：
#   1. 本文件 `PermissionManager.__init__` 里的 `self._policies`（运行时事实）
#   2. 本文件的 `READ_ONLY_TOOLS` 白名单（只读档放行名单）
#   3. `extensions/catalog.py` 的 `_BUILTIN_POLICY_LABELS`（「工具」页文案）
# 第 3 处只覆盖 3 个工具，而这里实际注册了 27 个 → 「工具」页对十几个**免审批**的
# 工具显示「需人工审批」。同时 `skill_list` / `skill_search` 的 docstring 承诺
# 「只读，放行」，实际落到 DefaultPolicy，每次都要人工点一下（无人值守下直接失败）。
# 更糟的是白名单里长出了 6 个**没有任何类实现**的工具名（list_files / get_file_info /
# search_files / list_artifacts / read_artifact / knowledge_search），其中
# knowledge_search 连测试都在依赖——它读起来像"这个能力已被覆盖"，其实什么也没覆盖。
#
# 现在：运行时策略、「工具」页文案、只读档放行名单**全部从这张表派生**，并由
# `tests/test_permission_policy_consistency.py` 钉住一致性——手工维护的平行清单
# 就是这类漂移的温床，索性不要平行清单。
#
# 每行 = (工具名, 策略键, 只读档是否放行)。
#   策略键 → 策略类：见下面的 `_POLICY_CLASSES`
#   策略类 → 界面 (kind, 文案)：见 `policy.POLICY_KINDS`
#   只读档放行：只对"确实不产生写入"的工具为 True（含纯函数工具，见 §M30/M31 的取舍）
#
# 注：`glob` 在第三波落地（M43）：这里加一行 `("glob", "readonly", True)` 即可，
# 运行时/文案/只读名单会一起生效——这正是把三处收敛成一张表的好处。
BUILTIN_TOOL_POLICIES: tuple[tuple[str, str, bool], ...] = (
    # —— 变更工作区：按路径/命令判定 ——
    ("bash", "bash", False),
    # 持久 shell（M48）：同样是"执行命令"，所以走**同一套** BashPolicy
    # （黑名单拦截 / 白名单放行 / 其余转审批）——换个工具名不能换个策略。
    ("bash_persistent", "bash", False),
    ("read_file", "readonly", True),
    # 读图（M51）：只读——它只读文件与图片字节，不产生任何写入
    ("read_image", "readonly", True),
    ("write_file", "writefile", False),
    ("edit_file", "writefile", False),
    # 批量工具的路径位于 edits[*].path，不能套用顶层 path 的单文件参数形状。
    ("edit_many", "batch_writefile", False),
    # 仅保存平台的待审提案，不写项目；人工应用入口不作为工具暴露。
    ("propose_edit", "allow", True),
    # —— 代码情报：纯只读 ——
    ("list_dir", "readonly", True),
    ("grep", "readonly", True),
    ("glob", "readonly", True),
    ("git_read", "readonly", True),
    ("code_map", "readonly", True),
    ("code_features", "readonly", True),
    ("code_search", "readonly", True),
    ("code_graph", "readonly", True),
    # 契约检查只读契约 + 实现 + tests/，不产生写入（此前错挂成 session_state，
    # 导致只读档下"检查契约"这种纯读操作反而被拒）
    ("contract_check", "readonly", True),
    # —— 扩展面：读进来是只读；装进来是写操作 ——
    ("load_skill", "readonly", True),
    ("load_expert", "readonly", True),
    ("skill_list", "readonly", True),
    ("skill_search", "readonly", True),
    ("skill_install", "default", False),
    # —— 知识库：读为只读；写只落平台数据目录 ——
    ("kb_search", "readonly", True),
    ("kb_write", "session_state", False),
    # —— 联网：会话级「联网」开关（M19）才是闸门，开着时不再叠加人工审批 ——
    # 开关默认关（sessions.web_enabled DEFAULT 0），关着时工具根本不在模型的 schema 里，
    # 所以"打开开关"本身已是用户的显式授权。需要逐次确认的部署可在「工具」页把这两个
    # 工具改成「需审批」（settings.tool_policies 覆盖优先）。
    ("web_search", "readonly", True),
    ("web_fetch", "readonly", True),
    # —— 平台自身状态 / 无副作用的纯函数：不碰用户仓库，不该等人点一下 ——
    ("todo_write", "allow", True),
    ("ask_user_question", "allow", True),
    ("acceptance_set", "session_state", False),
    ("acceptance_check", "session_state", False),
    ("feature_brief", "allow", False),
    # save_memory / deliver_file / present 会落盘（用户记忆目录、工作区 memory/、
    # 交付物目录），所以只读档拒绝；但它们写的是平台自己的数据，默认不打断人。
    ("save_memory", "allow", False),
    ("deliver_file", "allow", False),
    ("present", "allow", False),
    # 运行时依赖位置：只报告路径、不执行任何程序、不写任何东西 → 纯只读
    ("load_workspace_dependencies", "readonly", True),
    # —— 后台任务（M45）：读输出/列任务只读；停任务会杀进程，只读档拒绝 ——
    ("job_output", "readonly", True),
    ("job_list", "readonly", True),
    ("job_kill", "allow", False),
    # Coordination writes platform metadata only. Actual worker actions use
    # their inherited permission policies, including Lead's direct execution.
    ("team_list", "readonly", True),
    ("read_agent_execution", "readonly", True),
    ("delegate_agent", "allow", True),
    ("send_agent_message", "allow", True),
    ("followup_agent", "allow", True),
    ("interrupt_agent", "allow", True),
    ("wait_agents", "readonly", True),
    ("stop_agent", "allow", True),
    ("audit_agent", "allow", True),
)

#: 策略键 → 策略类。单文件/批量写策略需要工作区根，装配时单独处理。
_POLICY_CLASSES: dict[str, type] = {
    "readonly": ReadOnlyPolicy,
    "allow": AllowPolicy,
    "session_state": SessionStatePolicy,
    "default": DefaultPolicy,
    "bash": BashPolicy,
    "writefile": WriteFilePolicy,
    "batch_writefile": BatchWriteFilePolicy,
}

#: 只读档放行的工具名。**派生**而非手工维护——手工维护正是它长出 6 个幽灵工具名的原因。
READ_ONLY_TOOLS = frozenset(name for name, _key, read_only in BUILTIN_TOOL_POLICIES if read_only)

#: 工具名 → 界面展示的 (kind, 文案)。「工具」页直接消费，与运行时同源。
BUILTIN_POLICY_KINDS: dict[str, tuple[str, str]] = {
    name: describe_policy(_POLICY_CLASSES[key]) for name, key, _read_only in BUILTIN_TOOL_POLICIES
}


def _build_policies(workspace_root: Any, *, contract_guard: bool) -> dict[str, Any]:
    """按登记表装配策略表。工作区缺失时跳过需要路径的策略（落到 DefaultPolicy）。"""
    policies: dict[str, Any] = {}
    for name, key, _read_only in BUILTIN_TOOL_POLICIES:
        if key in ("writefile", "batch_writefile"):
            if workspace_root is None:
                continue
            policies[name] = (
                BatchWriteFilePolicy(Path(workspace_root), contract_guard=contract_guard)
                if key == "batch_writefile" else WriteFilePolicy(Path(workspace_root))
            )
        else:
            policies[name] = _POLICY_CLASSES[key]()
    # M32 契约闸门（R7）：契约文件对 AI 只读。**包在写策略外面**，
    # 所以它只收紧"写契约"，工作区内的正常写入不受影响。
    if contract_guard:
        for tool in ("write_file", "edit_file"):
            if tool in policies:
                policies[tool] = ContractGuardPolicy(policies[tool])
    return policies


@dataclass
class ApprovalRequest:
    approval_id: str = field(default_factory=lambda: new_id("ap_"))
    session_id: str = ""
    user_id: str = "anonymous"
    tool_name: str = ""
    command: str = ""
    reason: str = ""
    purpose: str = ""
    effects: list[str] = field(default_factory=list)


class PermissionDenied(Exception):
    """deny 后抛给 loop 转成 tool_result 的错误。"""


class PermissionManager:
    def __init__(
        self,
        workspace_root=None,
        approver: Approver | None = None,
        mode: str = "workspace-write",
        contract_guard: bool = False,
        *,
        read_only_approval: bool = False,
        mode_provider: Callable[[], str] | None = None,
    ):
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.approver = approver
        self.mode = mode if mode in PERMISSION_MODES else "workspace-write"
        # Opt in only for an interactive runtime. CLI/strict automation retain
        # fail-closed read-only behavior, including when no approver exists.
        self.read_only_approval = bool(read_only_approval)
        self.mode_provider = mode_provider
        # 策略表从登记表装配（见文件头 `BUILTIN_TOOL_POLICIES`）——运行时策略、
        # 「工具」页文案、只读档放行名单共用同一个真源，不再各写一份。
        self._policies: dict[str, Any] = _build_policies(
            self.workspace_root, contract_guard=contract_guard
        )
        self._default = DefaultPolicy()
        # 审批请求历史（M2 落 approvals 表）
        self.history: list[dict[str, Any]] = []

    def policy_kind(self, tool_name: str) -> tuple[str, str]:
        """该工具走什么权限，返回 (kind, 可读说明)。『工具』页直接消费。

        kind: allow（免审批）/ policy（按参数判定）/ approval（需人工审批）。
        与 `evaluate` 读的是**同一个** `self._policies`，所以界面文案不会再和运行时
        打架（此前两者分居两个文件，界面把十几个免审批工具标成了「需人工审批」）。
        """
        return describe_policy(self._policies.get(tool_name, self._default))

    def builtin_policy_kinds(self) -> dict[str, tuple[str, str]]:
        """整张策略表的界面语义（含契约闸门等运行时包装后的真实结果）。"""
        return {name: self.policy_kind(name) for name in BUILTIN_POLICY_KINDS}

    def register_policy(self, tool_name: str, policy: Any) -> None:
        """注册自定义工具的策略（无策略走 DefaultPolicy）。"""
        self._policies[tool_name] = policy

    async def evaluate(self, tool_name: str, arguments: dict[str, Any], ctx: ToolContext) -> PolicyResult:
        """Read the current mode for each call; an interactive grant is never cached."""
        arguments = executable_arguments(arguments)
        try:
            mode = self.mode_provider() if self.mode_provider is not None else self.mode
        except Exception:
            return PolicyResult(Decision.deny, "无法读取当前会话权限，已拒绝本次操作，请重新确认权限设置")
        if mode not in PERMISSION_MODES:
            return PolicyResult(Decision.deny, "当前会话权限档位无效，已拒绝本次操作")
        if mode == "full-access":
            # 黑名单仍生效吗？不——full-access 的语义就是"信任 agent 全自动"，
            # 用户显式选择了免审批档；再拦就名不副实。
            return PolicyResult(Decision.allow, "完全权限模式：免审批直通")
        result = self._policies.get(tool_name, self._default).evaluate(arguments, ctx)
        # A user approval cannot override a hard policy boundary: blacklisted
        # commands, out-of-workspace paths and protected contracts stay denied.
        if result.decision == Decision.deny:
            return result
        if mode == "read-only" and not self._is_read_only(tool_name):
            if self.read_only_approval and self.approver is not None:
                return PolicyResult(
                    Decision.need_approval,
                    "当前会话为只读模式，此操作需要单次授权。批准仅允许本次操作，会话保持只读。",
                )
            return PolicyResult(Decision.deny, "只读模式：仅允许读取类操作")
        return result

    def _is_read_only(self, tool_name: str) -> bool:
        """只读档判定：命中依赖放行名单，或策略本身就是 ReadOnlyPolicy。

        名单里的名字来自 `BUILTIN_TOOL_POLICIES` 的第三列（派生，不手工维护）。
        名单覆盖的是**策略类型不是 ReadOnlyPolicy 但确实不产生写入**的工具
        （如 `todo_write` / `ask_user_question` 这两个纯函数）。

        扩展/MCP/插件工具没有内置策略，走 DefaultPolicy → 不在名单里 → 不自动只读放行。
        交互模式须单次批准；其他情况下保持拒绝。要声明其只读能力得显式
        `register_policy`，**不能靠起个像只读工具的名字混进来**——白名单按名字放行
        曾经就是这个口子：里面躺着 6 个没有任何实现的名字。
        """
        if tool_name in READ_ONLY_TOOLS:
            return True
        return isinstance(self._policies.get(tool_name), ReadOnlyPolicy)

    async def request_approval(
        self, tool_name: str, arguments: dict[str, Any], ctx: ToolContext, reason: str,
        *, purpose: str = "", effects: list[str] | None = None,
    ) -> bool:
        """发起人机审批。无 approver 视为拒绝（fail closed）。"""
        explanation = approval_details(arguments.get(APPROVAL_ARGUMENT))
        if purpose or effects is not None:
            explanation = approval_details({"purpose": purpose, "effects": effects})
        arguments = executable_arguments(arguments)
        req = ApprovalRequest(
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            tool_name=tool_name,
            command=json.dumps(arguments, ensure_ascii=False, indent=2),
            reason=reason,
            purpose=explanation["purpose"], effects=explanation["effects"],
        )
        record: dict[str, Any] = {
            "approval_id": req.approval_id,
            "tool": tool_name,
            "command": req.command,
            "reason": reason,
            **explanation,
            "status": "pending",
        }
        self.history.append(record)

        if self.approver is None:
            record["status"] = "denied"
            record["detail"] = "无审批通道，fail closed"
            return False

        try:
            ok = await self.approver(req)
        except Exception as exc:  # 审批通道故障也 fail closed
            record["status"] = "denied"
            record["detail"] = f"审批通道异常: {exc}"
            return False
        record["status"] = "approved" if ok else "denied"
        return ok
