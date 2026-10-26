"""Read capabilities and saved evidence must match the actual session tools."""

import asyncio

import pytest

from code_agent.agent.loop import AgentLoop
from code_agent.context.images import ImageInbox
from code_agent.permissions.hooks import PermissionManager
from code_agent.providers.offline import OfflineProvider
from code_agent.storage.jsonl import TranscriptStore
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin.env_tools import LoadWorkspaceDependenciesTool
from code_agent.tools.builtin.image_tools import ReadImageTool
from code_agent.tools.builtin.tools_builtin import BashTool, ReadFileTool
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry


@pytest.mark.parametrize("with_bash", [False, True])
def test_dependency_guidance_uses_session_capabilities(workspace, with_bash):
    available = {"load_workspace_dependencies", "delegate_agent", "read_agent_execution"}
    if with_bash:
        available.add("bash")
    ctx = ToolContext("s", workspace, extra={"available_tools": available})
    text = asyncio.run(LoadWorkspaceDependenciesTool().execute({}, ctx))
    assert "用 `bash` 调用" in text if with_bash else "委派给执行节点" in text
    if not with_bash:
        assert "当前会话没有 bash" in text
        assert "用 `bash` 调用" not in text


def test_image_fallback_does_not_send_lead_to_unavailable_shell(workspace, monkeypatch):
    monkeypatch.delenv("CODE_AGENT_VISION", raising=False)
    (workspace / "image.gif").write_bytes(b"GIF89a\x01\x00\x01\x00\x00\x00\x00")
    ctx = ToolContext("s", workspace, extra={
        "available_tools": {"read_image", "delegate_agent"}, "images": ImageInbox(),
    })
    text = asyncio.run(ReadImageTool().execute({"path": "image.gif"}, ctx))
    assert "图片字节没有发给模型" in text
    assert "委派给具备执行能力的节点" in text
    assert "用 bash" not in text


@pytest.mark.parametrize("size", [6000, 100_000])
def test_persisted_tool_evidence_keeps_budgeted_result_and_pointer(agent_home, workspace, size):
    from code_agent.context.prompt import build_history
    from code_agent.config import tool_results_dir

    content = "BEGIN\n" + "x" * size + "\nFINAL DIAGNOSTIC: actual failure\n"
    (workspace / "output.txt").write_bytes(content.encode("utf-8"))
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    provider = OfflineProvider(script=[
        OfflineProvider.tool_call_turn("read_file", {"path": "output.txt", "max_bytes": size + 500}),
        OfflineProvider.text_turn("已读取"),
    ])
    loop = AgentLoop(provider=provider, registry=registry,
        permissions=PermissionManager(workspace_root=workspace, mode="read-only"),
        transcript=TranscriptStore(workspace.parent / "transcripts"), workspace=workspace)
    asyncio.run(loop.run("读取已有输出"))
    events = loop.transcript.replay(loop.session_id)
    payload = next(e.payload for e in events if e.type.value == "tool_result")
    assert payload["result_truncated"] is False
    assert payload["result_total_chars"] == len(payload["result"])
    if size == 6000:
        assert payload["result"] == content
    else:
        assert "全文已存盘" in payload["result"]
        assert "输出预算" in payload["result"]
        saved = list(tool_results_dir().rglob("*.txt"))
        assert any(path.read_text(encoding="utf-8") == content for path in saved)
    replay = next(m for m in build_history(events) if m["role"] == "tool")
    assert replay["content"] == payload["result"]


def test_tool_context_exposes_filtered_registry_not_global_capabilities(workspace):
    registry = ToolRegistry()
    registry.register(BashTool())
    registry.register(ReadFileTool())
    filtered = FilteredToolRegistry(registry, exclude=("bash",))
    loop = AgentLoop(provider=OfflineProvider(), registry=filtered,
        permissions=PermissionManager(workspace_root=workspace),
        transcript=TranscriptStore(workspace.parent / "transcripts"), workspace=workspace)
    assert loop._tool_extra()["available_tools"] == frozenset({"read_file"})
