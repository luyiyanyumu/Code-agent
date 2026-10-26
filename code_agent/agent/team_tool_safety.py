"""Conservative project-file effect classification for team execution.

``read`` means no direct project-file mutation: trusted coordination and
platform-state tools may still update their own records. This classification
does not grant permissions or interpret arbitrary shell commands.
"""

from __future__ import annotations

import sys
from typing import Any, Literal


TeamToolKind = Literal["write", "shell", "read", "opaque"]

# Match the actual built-in class, not a name, namespace prefix or subclass
# that can replace execute(). Merely calling a plugin "read_file" is not proof
# that it is read-only. Modules are already loaded when a tool is instantiated.
_TRUSTED_READ_TOOLS: dict[str, tuple[str, str]] = {
    "read_file": ("tools.builtin.tools_builtin", "ReadFileTool"),
    "read_image": ("tools.builtin.image_tools", "ReadImageTool"),
    "list_dir": ("tools.builtin.code_tools", "ListDirTool"),
    "grep": ("tools.builtin.code_tools", "GrepTool"),
    "glob": ("tools.builtin.code_tools", "GlobTool"),
    "code_map": ("tools.builtin.code_tools", "CodeMapTool"),
    "code_features": ("tools.builtin.code_tools", "CodeFeaturesTool"),
    "code_search": ("tools.builtin.code_tools", "CodeSearchTool"),
    "code_graph": ("tools.builtin.code_tools", "CodeGraphTool"),
    "git_read": ("tools.builtin.git_read_tools", "GitReadTool"),
    "contract_check": ("tools.builtin.contract_tools", "ContractCheckTool"),
    "load_workspace_dependencies": ("tools.builtin.env_tools", "LoadWorkspaceDependenciesTool"),
    "skill_list": ("tools.builtin.skill_tools", "SkillListTool"),
    "load_skill": ("extensions.packs", "LoadPackTool"),
    "load_expert": ("extensions.packs", "LoadPackTool"),
    "tool_search": ("tools.defer", "ToolSearchTool"),
    "web_search": ("tools.builtin.web_tools", "WebSearchTool"),
    "web_fetch": ("tools.builtin.web_tools", "WebFetchTool"),
    "kb_search": ("knowledge.tools", "KbSearchTool"),
    "ask_user_question": ("tools.builtin.ask_tools", "AskUserQuestionTool"),
    "todo_write": ("tools.builtin.todo_tools", "TodoWriteTool"),
    "acceptance_set": ("tools.builtin.acceptance_tools", "AcceptanceSetTool"),
    "acceptance_check": ("tools.builtin.acceptance_tools", "AcceptanceCheckTool"),
    "feature_brief": ("tools.builtin.brief_tools", "FeatureBriefTool"),
    "propose_edit": ("tools.builtin.propose_edit", "ProposeEditTool"),
    "job_output": ("tools.builtin.job_tools", "JobOutputTool"),
    "job_list": ("tools.builtin.job_tools", "JobListTool"),
    "job_kill": ("tools.builtin.job_tools", "JobKillTool"),
    "team_list": ("tools.builtin.agent_team_tools", "TeamListTool"),
    "delegate_agent": ("tools.builtin.agent_team_tools", "DelegateAgentTool"),
    "send_agent_message": ("tools.builtin.agent_team_tools", "SendAgentMessageTool"),
    "followup_agent": ("tools.builtin.agent_team_tools", "FollowupAgentTool"),
    "interrupt_agent": ("tools.builtin.agent_team_tools", "InterruptAgentTool"),
    "wait_agents": ("tools.builtin.agent_team_tools", "WaitAgentsTool"),
    "stop_agent": ("tools.builtin.agent_team_tools", "StopAgentTool"),
    "audit_agent": ("tools.builtin.agent_team_tools", "AuditAgentTool"),
    "read_agent_execution": ("tools.builtin.agent_team_tools", "ReadAgentExecutionTool"),
}


def classify_team_tool(name: str, tool: Any, arguments: dict[str, Any]) -> TeamToolKind:
    """Identify declared writes/shells first; unknown effects stay opaque."""
    if name in {"bash", "bash_persistent"}:
        return "shell"
    if getattr(tool, "writes_path", False):
        return "write"
    targets = getattr(tool, "write_targets", None)
    if callable(targets):
        try:
            if targets(arguments):
                return "write"
        except Exception:
            return "opaque"
    expected = _TRUSTED_READ_TOOLS.get(name)
    if expected is None:
        return "opaque"
    module_name, class_name = expected
    module_name = "code_agent." + module_name
    cls = type(tool)
    module = sys.modules.get(module_name)
    if cls.__module__ != module_name or cls is not getattr(module, class_name, None):
        return "opaque"
    # An instance-level replacement has the same class identity but not the
    # implementation whose effects were reviewed for this allowlist.
    if "execute" in getattr(tool, "__dict__", {}):
        return "opaque"
    return "read"
