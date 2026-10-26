"""权限策略引擎（s04）：先划边界，再给自由。

三态判定（设计文档 §7.2 第 1 层）：
- allow          直接放行
- deny           命中黑名单，直接拒绝
- need_approval  转人机审批（CLI 内 y/n，M2 换 Web 弹窗，异步落 approvals 表）

M1 策略：
- bash:      黑名单模式命中 -> deny；白名单前缀命中 -> allow；
             其余 -> need_approval（保守默认）
- write_file: 目标路径越出工作区 -> need_approval
- read_file:  放行（只读）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from code_agent.tools.base import ToolContext


class Decision(str, Enum):
    allow = "allow"
    deny = "deny"
    need_approval = "need_approval"


@dataclass
class PolicyResult:
    decision: Decision
    reason: str = ""


class BashPolicy:
    """bash 命令策略：黑名单 > 白名单 > 默认审批。"""

    # 命中即拒绝（M1 集合，M5 审计沙盒阶段扩充并配合容器隔离）
    DENY_PATTERNS: list[tuple[str, str]] = [
        (r"\bsudo\b", "使用 sudo"),
        (r"\bsu\b\s", "切换用户"),
        (r"rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+[/~]", "递归强制删除根/家目录"),
        (r"\brm\s+-rf?\s+/", "删除根路径"),
        (r"\bmkfs\b", "格式化文件系统"),
        (r"\b(shutdown|reboot|halt|poweroff)\b", "关机/重启"),
        (r"\bdd\b\s+if=.*of=/dev/", "裸设备写入"),
        (r">\s*/dev/sd[a-z]", "覆写磁盘设备"),
        (r"\bchmod\s+(-R\s+)?777\s+/", "全开权限"),
        (r"\bcurl\b[^|]*\|\s*(ba|z)?sh", "管道执行远程脚本"),
        (r"\bwget\b[^|]*\|\s*(ba|z)?sh", "管道执行远程脚本"),
        (r"\b:\(\)\s*\{.*\};\s*:", "fork bomb"),
        (r"\bcrontab\b", "修改计划任务"),
        (r"\bssh\b", "远程连接（防外联跳板）"),
        (r"\bscp\b", "远程拷贝（防数据外发）"),
        (r"\bnc\b|\bnetcat\b", "网络工具（防反弹 shell）"),
    ]

    # 命中即放行的安全前缀（只读/无副作用命令）
    ALLOW_PREFIXES: tuple[str, ...] = (
        "echo", "ls", "dir", "pwd", "cat", "head", "tail", "wc",
        "find", "grep", "rg", "which", "where", "whoami", "date",
        "python", "pip", "git status", "git log", "git diff",
        "type ", "more ",
    )

    def evaluate(self, arguments: dict[str, Any], ctx: ToolContext) -> PolicyResult:
        command = (arguments.get("command") or "").strip()
        if not command:
            return PolicyResult(Decision.deny, "空命令")
        lowered = command.lower()
        for pattern, label in self.DENY_PATTERNS:
            if re.search(pattern, lowered):
                return PolicyResult(Decision.deny, f"命中黑名单：{label}")
        for prefix in self.ALLOW_PREFIXES:
            if lowered.startswith(prefix):
                return PolicyResult(Decision.allow, f"白名单前缀 {prefix.strip()!r}")
        return PolicyResult(Decision.need_approval, "命令不在白名单内，需人工确认")


class WriteFilePolicy:
    """write_file / edit_file 策略：工作区内放行，越界**直接拒绝**。

    越界为什么是 `deny` 而不是 `need_approval`：工具自己有一条硬边界
    （`_WorkspaceBound.resolve_write`——`resolve()` 之后必须落在工作区内，越界抛
    `ToolError`）。也就是说"人工点一下放行"这条路**根本走不通**：批了之后工具照样拒。
    以前这里返回 need_approval，真实体验是——用户被叫来点一次确认，
    然后拿到一个错误：既打扰了人，又没有给出任何可能性。

    硬边界是刻意的纵深防御（M6）：即使某条策略漏配，工具本身也不放行越界读写。
    既然工具已经不可能越界写，策略层就不该再提供一条通往同一个死路的"审批"岔路；
    两边口径统一成"越界 = 拒"，理由写清楚，让模型去改路径。
    """

    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root).resolve()

    def evaluate(self, arguments: dict[str, Any], ctx: ToolContext) -> PolicyResult:
        raw = arguments.get("path") or ""
        if not raw:
            return PolicyResult(Decision.deny, "path 为空")
        p = Path(raw)
        resolved = (p if p.is_absolute() else (ctx.workspace / p)).resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError:
            return PolicyResult(
                Decision.deny,
                f"目标 {resolved} 在工作区 {self.workspace_root} 之外：写入/编辑有硬边界，"
                "不接受越界写入（请改到工作区内的路径；工作区外的文件由人来动）",
            )
        return PolicyResult(Decision.allow, "工作区内写入")


class ReadOnlyPolicy:
    """read_file 策略：M1 只读放行。"""

    def evaluate(self, arguments: dict[str, Any], ctx: ToolContext) -> PolicyResult:
        return PolicyResult(Decision.allow, "只读操作")


class AllowPolicy:
    """信任来源直通（如 trusted MCP server）。"""

    def evaluate(self, arguments: dict[str, Any], ctx: ToolContext) -> PolicyResult:
        return PolicyResult(Decision.allow, "信任来源")


#: 契约文件后缀（与 `code_agent.contract.parse.SUFFIX` 一致；这里自带一份是为了
#: 权限层不依赖契约层的 import——策略是最底层，不该被上层模块拖住）
CONTRACT_SUFFIX = ".CONTRACT.md"


class SessionStatePolicy:
    """会话级状态工具：只写**平台自己的**状态，不碰用户仓库。

    这类工具（验收清单、任务清单的判据落盘）写的是 `~/.code-agent/...` 下的进度与
    判据记录：不写用户仓库、不起子进程、不发网络请求。它们走 DefaultPolicy 会被
    一律拦下等人工确认——真机踩到：**无人值守/严格模式下这类调用永远失败**
    （`[permission denied] 人工审批未通过`），于是任务清单每次都写不进去。

    与 AllowPolicy 的分工很清楚：AllowPolicy 是"这个来源我信任"，本策略是
    "这一档风险不值得打断人"。它**不覆盖**任何会落到用户目录/起进程的操作——
    那些仍然走各自的策略（write_file / bash）。

    也不把它算作只读：只读档位下这类工具仍被拒（"只读 = 不产生任何写入"这条
    语义不破例，见 PermissionManager._is_read_only）。
    """

    def evaluate(self, arguments: dict[str, Any], ctx: ToolContext) -> PolicyResult:
        return PolicyResult(Decision.allow, "仅写平台自身的会话状态（不碰用户仓库）")


class ContractGuardPolicy:
    """契约闸门（R7）：**契约文件对 AI 只读**。

    阶段 0 实验里这个闸门没被触发过（4 次实现零违例），但那是因为 prompt 里明确禁止了——
    **prompt 不是闸门**。R7 是范式的命门：只要 AI 能动契约，它就会用"改契约"的方式
    让自己通过检查，这和"改测试凑通过"是同一个病（Janus Labs 的 BHV-001 量的就是这类行为）。

    设计取舍：
    - 命中即 **deny**（不是 need_approval）：这里不该给"点一下放行"的顺手通道，
      契约变更要走人的显式动作（人自己改文件，或先在设置里关掉这个闸门）。
    - 只拦**写入**：读契约永远是自由的（`read_file` 不受影响）。
    - 只认路径：`*.CONTRACT.md`。不去猜"这次改动语义上算不算契约变更"——
      猜错一次就等于闸门失效，而路径判定是确定的。
    """

    def __init__(self, inner: Any = None, *, protect_evidence: bool = False):
        self._inner = inner
        self.protect_evidence = protect_evidence

    def evaluate(self, arguments: dict[str, Any], ctx: ToolContext) -> PolicyResult:
        rel = str(arguments.get("path") or arguments.get("file") or "").strip().replace("\\", "/")
        name = rel.rsplit("/", 1)[-1]
        if name.endswith(CONTRACT_SUFFIX):
            return PolicyResult(
                Decision.deny,
                f"`{name}` 是契约文件：形状/义务/数据/拓扑是人写的设计，AI 只改函数体（R7）。"
                "需要改契约时请由人修改，或先在设置里关掉契约闸门。",
            )
        if self.protect_evidence and is_evidence_path(rel):
            return PolicyResult(
                Decision.deny,
                f"`{rel}` 是证据文件（测试）：不许为了让检查通过而改证据（R6）。"
                "证据代表需求；改断言等于把需求改成现状。缺证据时应当如实报告。",
            )
        if self._inner is None:
            return PolicyResult(Decision.allow, "契约闸门：非契约文件，按默认放行")
        return self._inner.evaluate(arguments, ctx)


class BatchWriteFilePolicy:
    """Validate every ``edit_many`` target before approving any of the batch."""

    def __init__(self, workspace_root: Path, *, contract_guard: bool = False):
        self._path_policy = WriteFilePolicy(workspace_root)
        if contract_guard:
            self._path_policy = ContractGuardPolicy(self._path_policy)

    def evaluate(self, arguments: dict[str, Any], ctx: ToolContext) -> PolicyResult:
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not edits:
            return PolicyResult(Decision.deny, "edits 必须是非空数组")
        for index, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                return PolicyResult(Decision.deny, f"第 {index} 处编辑必须是对象")
            path = edit.get("path")
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                return PolicyResult(Decision.deny, f"第 {index} 处编辑的 path 必须是有效的非空字符串")
            if not all(isinstance(edit.get(key), str) for key in ("old_string", "new_string")):
                return PolicyResult(Decision.deny, f"第 {index} 处编辑必须提供字符串 old_string 和 new_string")
            if "replace_all" in edit and not isinstance(edit["replace_all"], bool):
                return PolicyResult(Decision.deny, f"第 {index} 处编辑的 replace_all 必须是布尔值")
            try:
                result = self._path_policy.evaluate({"path": path.strip()}, ctx)
            except (OSError, TypeError, ValueError):
                return PolicyResult(Decision.deny, f"第 {index} 处编辑的 path 无效")
            if result.decision != Decision.allow:
                return PolicyResult(result.decision, f"第 {index} 处编辑：{result.reason}")
        return PolicyResult(Decision.allow, "全部目标均为工作区内可写文件")


def is_evidence_path(rel: str) -> bool:
    """判断路径是不是"证据文件"（测试）。

    口径刻意保守：只认 `tests/` 目录下、`test_*.py` / `*_test.py` / `conftest.py`。
    其它文件（包括 fixture 数据）不拦——闸门宁窄勿宽，宽了会挡住正常的开发。
    """
    if not rel:
        return False
    parts = [p for p in rel.split("/") if p]
    if len(parts) < 2:
        return False
    name = parts[-1]
    if not name.endswith(".py"):
        return False
    if "tests" not in parts[:-1]:
        return False
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


class DefaultPolicy:
    """未配置策略的工具：一律需审批（保守默认）。"""

    def evaluate(self, arguments: dict[str, Any], ctx: ToolContext) -> PolicyResult:
        return PolicyResult(Decision.need_approval, f"工具无专用策略，默认人工确认")


# ----------------------------------------------------------------------
# 策略 → 界面语义（「工具」页的「该工具走什么权限」）
# ----------------------------------------------------------------------
#: 策略类 → (kind, 中文说明)。
#:
#: kind 的取值与前端 `tool_policy_modes` 同一套词汇：
#:   allow    免审批（这条链路不会弹审批框）
#:   policy   按参数/路径判定（可能放行也可能审批）
#:   approval 每次都要人工确认
#:
#: 为什么把这张表放在策略类旁边：它是**策略的含义**，不是界面的装饰。
#: 此前这份文案长在 `extensions/catalog.py` 里且只覆盖 3 个工具（bash / read_file /
#: write_file），而 PermissionManager 实际注册了 20+ 个——于是「工具」页对十几个
#: **免审批**的工具显示「需人工审批」。文案与策略分居两处，就一定会漂。
POLICY_KINDS: dict[type, tuple[str, str]] = {
    AllowPolicy: ("allow", "免审批放行（显式声明的信任来源）"),
    ReadOnlyPolicy: ("allow", "免审批放行（只读，不产生任何写入）"),
    SessionStatePolicy: ("allow", "免审批（只写平台自身的会话状态）"),
    BashPolicy: ("policy", "按命令判定（黑名单 / 白名单 / 审批）"),
    WriteFilePolicy: ("policy", "工作区内放行，越界直接拒绝"),
    BatchWriteFilePolicy: ("policy", "逐项校验全部目标路径，任一目标违规即拒绝整批编辑"),
    ContractGuardPolicy: ("policy", "契约文件对 AI 只读（命中即拒）"),
    DefaultPolicy: ("approval", "默认策略（需人工审批）"),
}


def describe_policy(policy: Any) -> tuple[str, str]:
    """给出一个策略（实例或类）在界面上该怎么显示。未知策略按最保守的「需审批」。"""
    cls = policy if isinstance(policy, type) else type(policy)
    return POLICY_KINDS.get(cls, POLICY_KINDS[DefaultPolicy])
