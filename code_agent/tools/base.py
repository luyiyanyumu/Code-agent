"""Tool 协议与工具执行上下文。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from code_agent.models.approval import approval_parameters

if TYPE_CHECKING:  # 只为类型标注；运行时不引入依赖
    from code_agent.tools.fs_observation import FsObservation


@dataclass
class ToolContext:
    """一次工具执行的上下文。"""

    session_id: str
    workspace: Path                      # 工具默认工作区根目录
    user_id: str = "anonymous"
    extra: dict[str, Any] = field(default_factory=dict)
    #: 会话级「文件观察」记录（对齐 DSH 的 `dsh-fs-observation-policy`）。
    #:
    #: **None = 不启用读写观察策略**。这不是"忘了传"，而是刻意的可选语义：
    #: 该策略在 DSH 里本身就是可插拔的（省略它时写入仍是原子的，只是不要求先读）。
    #: 真实链路一定会传——`AgentLoop.run` 是 `ToolContext` 的唯一构造点，
    #: 它从 SessionRuntime 拿到 per-session 的记录并放进来。只有程序化直调
    #: （测试、脚本）会拿到 None，那种场景没有"模型基于陈旧内容覆盖"的风险。
    fs_observation: "FsObservation | None" = None


class ToolError(Exception):
    """工具执行失败（区别于拒绝执行）。"""


class Tool(ABC):
    """工具协议：name / description / parameters(JSON schema) / execute。

    description    给模型看的函数说明（保持英文，通用性最好）；
    description_zh 给界面看的中文介绍（「工具」页展示，不影响发给模型的 schema）。

    时间上限（timeout，对齐 DSH 的 `dsh-tool-call-timeout-policy`）：
    **每个工具声明自己的期限**，由 loop 在调用它时施加；没声明的一律不设限。
    「协作式」的准确含义——

    - 到点后 loop 只是**停止等待**这一次调用，并把一条超时结果喂回模型让
      它换路子；取消是**请求**，不是硬杀（不会去杀进程、拆线程、砸通道）；
    - 工具若忽略取消（或把活儿丢给了子进程/线程），底层工作**可能仍在继续**。
      这是**文档化的限制，不是待修的 bug**：本框架不做硬停，也就不保证它停了；
    - 到点只影响超时的那一次调用：同一轮里按时返回的调用完全不受影响；
    - 到点**不改变取消语义**：`CancelledError` 照旧原样上抛（用户点"停止"必须真停）。

    取值约定：正数 = 秒；`None`（默认）或 <= 0 = 不设限。`0` 刻意不解释成"立刻超时"，
    那只会把"没配好"变成一个必然失败的默认值。
    """

    name: str = ""
    description: str = ""
    description_zh: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    #: 本工具的协作式时间上限（秒）。None = 不设限（默认）。
    timeout: float | None = None

    #: 本工具是否会**写入**一个由 `arguments["path"]` 指定的文件。
    #:
    #: 用途：同一轮里的多个工具调用是并发执行的（`dispatch_all`），两个写同一个
    #: 文件的调用会互相覆盖，而且覆盖顺序不确定。声明了这一点，注册表就能按
    #: 解析后的绝对路径给它们加一把写租约（同路径串行，不同路径照旧并行）。
    #: False = 不参与租约（读工具、bash、联网工具等）。
    writes_path: bool = False

    # A waiting agent releases its scheduler slot. Its wait must not overlap
    # other same-turn tools which are still executing under that slot.
    suspends_session: bool = False

    def write_targets(self, arguments: dict[str, Any]) -> list[str]:
        """这一次调用会写哪些路径。默认实现：`writes_path` 为真时取 `arguments["path"]`。

        **一次写多个路径的工具要覆盖它**（例如 `edit_many` 一次改多个文件）：租约
        会为每个路径各取一把锁，而**只声明一个 `path` 的工具会让另一个文件裸奔**——
        那种"以为加了锁其实没加"的状态比完全不加锁更危险（read 的时候会以为有保护）。
        """
        if not self.writes_path:
            return []
        raw = arguments.get("path") if isinstance(arguments, dict) else None
        return [str(raw)] if raw else []

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> str:
        """执行工具，返回文本结果（喂回模型）。"""
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        """给模型看的 function 定义（不含中文介绍）。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": approval_parameters(self.parameters),
        }

    def summary(self) -> dict[str, Any]:
        """延迟加载时先暴露的摘要（不含完整 schema）。"""
        return {"name": self.name, "description": self.description}

    def intro_zh(self) -> str:
        """界面用的中文介绍；没写就退回英文描述。"""
        return self.description_zh or self.description
