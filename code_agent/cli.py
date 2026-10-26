"""CLI 入口：python -m code_agent.cli

- --demo      离线冒烟：脚本化 offline provider 跑通 提问→调工具→回答
- --provider  offline | deepseek（默认 offline）
- 交互 REPL：每轮输入一个 prompt，bash 审批走控制台 y/n
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from code_agent.agent.loop import AgentLoop
from code_agent.config import load_env_file, transcripts_dir, workspace_root
from code_agent.jobs.registry import JobRegistry, notice_text
from code_agent.permissions.hooks import ApprovalRequest, PermissionManager
from code_agent.sandbox.persistent_shell import PersistentShells
from code_agent.providers import create_provider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.builtin import (
    BashPersistentTool,
    BashTool,
    ReadFileTool,
    WriteFileTool,
)
from code_agent.tools.fs_observation import FsObservation
from code_agent.tools.registry import ToolRegistry, apply_disabled


def build_registry(deferred: bool = False) -> ToolRegistry:
    from code_agent.config import artifacts_dir, builtin_skills_dir, experts_dir, skills_dir
    from code_agent.extensions import (
        ArtifactsStore,
        DeliverFileTool,
        ExpertsRegistry,
        LoadPackTool,
        PackSource,
        PresentTool,
        SkillsRegistry,
    )
    from code_agent.memory import SaveMemoryTool

    if deferred:
        from code_agent.tools.defer import DeferredToolRegistry

        reg: ToolRegistry = DeferredToolRegistry()
        reg.register_deferred(WriteFileTool())
    else:
        reg = ToolRegistry()
        reg.register(WriteFileTool())
    reg.register(BashTool())
    # M48 持久 shell：CLI 里尤其自然（终端本来就是有状态的）
    reg.register(BashPersistentTool())
    reg.register(ReadFileTool())
    from code_agent.tools.builtin.git_read_tools import GitReadTool

    reg.register(GitReadTool())
    reg.register(SaveMemoryTool())
    _artifacts = ArtifactsStore(artifacts_dir())
    reg.register(DeliverFileTool(_artifacts))
    # M43：交付已存在的文件（脚本产物 / 二进制 / 大文件）——CLI 里同样需要
    reg.register(PresentTool(_artifacts))
    reg.register(LoadPackTool(SkillsRegistry(
        skills_dir(), [PackSource(builtin_skills_dir(), "builtin")]
    )))
    reg.register(LoadPackTool(ExpertsRegistry(experts_dir())))
    # M43 运行时依赖位置：CLI 里尤其有用（"我该用哪只 python"）
    from code_agent.tools.builtin.env_tools import LoadWorkspaceDependenciesTool

    reg.register(LoadWorkspaceDependenciesTool())
    # M45 后台任务：CLI 也有任务表（长命令在终端里照样值得挂后台）
    from code_agent.tools.builtin.job_tools import build_job_tools

    for tool in build_job_tools():
        reg.register(tool)
    # M51 读图：CLI 同样注册（是否真的发给模型由 CODE_AGENT_VISION 决定）
    from code_agent.tools.builtin.image_tools import build_image_tools

    for tool in build_image_tools():
        reg.register(tool)
    # M19 联网工具（CLI 里同样常驻注册，开关由 SessionRuntime.web_enabled 决定）
    from code_agent.tools.builtin.web_tools import build_web_tools

    for tool in build_web_tools():
        reg.register(tool)
    # M20 技能商店（skill_list / skill_search / skill_install）
    from code_agent.tools.builtin.skill_tools import build_skill_tools

    for tool in build_skill_tools():
        reg.register(tool)
    return reg


def console_approver(req: ApprovalRequest) -> bool:
    print(f"\n[审批请求] 工具: {req.tool_name}")
    print(f"  参数: {req.command}")
    print(f"  操作目的（智能体说明）: {req.purpose or '智能体未提供操作目的'}")
    for effect in req.effects:
        print(f"  操作影响（智能体说明）: {effect}")
    print(f"  申请原因: {req.reason}")
    answer = input("  允许执行? (y/N): ").strip().lower()
    return answer in ("y", "yes")


def print_event(event) -> None:
    t, p = event.type.value, event.payload
    if t == "tool_use":
        call = p.get("call", {})
        print(f"  ⚙ 工具调用 {call.get('name')}({call.get('arguments')})")
    elif t == "tool_result":
        preview = (p.get("result") or "")[:300]
        print(f"  ⚙ 工具结果: {preview}")
    elif t == "approval_request":
        print(f"  ⏸ 需要审批: {p.get('tool')} - {p.get('reason')}")
    elif t == "error":
        print(f"  ✗ {p.get('message')}")


async def run_demo() -> None:
    print("=== Code-agent 离线冒烟（--demo）===")
    provider = create_provider("offline")
    ws = workspace_root()
    loop = AgentLoop(
        provider=provider,
        registry=build_registry(),
        permissions=PermissionManager(workspace_root=ws, approver=console_approver),
        transcript=TranscriptStore(transcripts_dir()),
        workspace=ws,
        on_event=print_event,
        # M41 文件观察：CLI 也挂上同一条策略（read 之后才能覆盖/编辑）
        fs_observation=FsObservation(),
        # M45 后台任务表：demo 是一次性的，给它一张自己的表即可
        jobs=JobRegistry(),
        # M48 持久 shell：demo 只有一条命令，给了也无妨（懒起，用不到就不 spawn）
        shells=PersistentShells(cwd=ws),
    )
    result = await loop.run("帮我执行 echo 演示一下工具调用")
    print(f"\n最终回答: {result.text}")
    print(f"轮数={result.turns} tokens={result.usage.total} session={loop.session_id}")
    print("轨迹文件:", transcripts_dir() / f"{loop.session_id}.jsonl")


async def run_repl(provider_name: str) -> None:
    from code_agent.context.prompt import build_history, memory_blocks
    from code_agent.memory.store import UserMemory, WorkspaceMemory
    from code_agent.models.events import new_id

    provider = create_provider(provider_name)
    ws = workspace_root()
    permissions = PermissionManager(workspace_root=ws, approver=console_approver)
    transcript = TranscriptStore(transcripts_dir())
    reg = build_registry()
    # M49：技能目录要进 system prompt。CLI 之前**没有**注入目录——模型看得见
    # `load_skill` 却不知道有哪些技能可加载，随包自带的 office 技能在 CLI 里等于不可见。
    # 目录只列 name + 一句话描述（几十 token），正文仍然按需 load_skill 展开。
    from code_agent.agent.loop import DEFAULT_SYSTEM_PROMPT
    from code_agent.config import builtin_skills_dir, skills_dir
    from code_agent.extensions import PackSource, SkillsRegistry

    skills = SkillsRegistry(
        skills_dir(), [PackSource(builtin_skills_dir(), "builtin")]
    )
    catalog = skills.catalog_text()
    system_prompt = DEFAULT_SYSTEM_PROMPT + (f"\n\n{catalog}" if catalog else "")
    session_id = new_id("s_")
    user_id = "cli-user"
    # M41 文件观察：**在循环外**建一次。REPL 每个 prompt 都重建 AgentLoop，
    # 记录挂在 loop 上的话"上一轮读过"到下一轮就没了，模型每轮都要重读。
    fs_obs = FsObservation()
    # M45 后台任务表同理：挂在循环外，否则每个 prompt 都把在跑的任务丢掉。
    jobs = JobRegistry()
    # M48 持久 shell 同理：**必须挂在循环外**，否则每个 prompt 都换一只 shell，
    # "cd / export 会保留"这句话当场变成谎话。
    shells = PersistentShells(cwd=ws)
    print(f"=== Code-agent REPL（provider={provider_name}, workspace={ws}）===")
    print(f"会话 {session_id}（进程内跨 prompt 保持上下文）| 输入 quit 退出")
    while True:
        try:
            prompt = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not prompt:
            continue
        if prompt.lower() in ("quit", "exit"):
            break
        history = build_history(transcript.replay(session_id))
        memory = memory_blocks(
            [{**record, "scope": "workspace", "label": "当前工作区"}
             for record in WorkspaceMemory(ws).active_records()],
            [{**record, "scope": "user", "label": "个人偏好"}
             for record in UserMemory(user_id).active_records()],
            query=prompt,
        )
        loop = AgentLoop(
            # 页面上一键禁用的工具在 CLI 里同样不生效（apply_disabled 是唯一入口）
            registry=apply_disabled(reg),
            permissions=permissions,
            transcript=transcript,
            session_id=session_id,
            workspace=ws,
            on_event=print_event,
            fs_observation=fs_obs,
            jobs=jobs,
            shells=shells,
            system_prompt=system_prompt,
            # CLI 没有"空闲唤醒"的地方（提示符之间没有事件循环），所以通知在
            # **下一条 prompt** 开头注入——语义与 Web 侧一致：没人收的结算不会丢。
            on_pending_notes=lambda: [notice_text(j) for j in jobs.drain_notices(session_id)],
        )
        try:
            result = await loop.run(prompt, user_id=user_id, history=history, memory=memory)
            print(f"\nagent> {result.text}")
        except Exception as exc:
            print(f"[错误] {exc}")
    print("bye.")


def main() -> None:
    load_env_file()  # 先吃 .env，命令行/真实环境变量优先级更高
    parser = argparse.ArgumentParser(prog="code-agent")
    parser.add_argument(
        "--provider", default="offline", choices=["offline", "deepseek", "openai", "openai_compat"]
    )
    parser.add_argument("--demo", action="store_true", help="离线冒烟模式")
    args = parser.parse_args()

    if args.demo:
        asyncio.run(run_demo())
    else:
        asyncio.run(run_repl(args.provider))


if __name__ == "__main__":
    main()
