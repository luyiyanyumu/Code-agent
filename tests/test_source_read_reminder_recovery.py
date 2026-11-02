"""Restoring a clipped receipt must restore its read-progress evidence too."""

import asyncio
import json

from code_agent.agent.source_context import SourceContext
from code_agent.context.compact import TOOL_OMIT_MARKER
from code_agent.models.events import ToolCall
from code_agent.tools.base import ToolContext
from code_agent.tools.builtin import ReadFileTool


def test_restored_receipt_prevents_same_source_windows_counting_as_new(workspace):
    source = "".join(f"value_{i:03d} = {i}  # verified source contents for this line\n" for i in range(150))
    (workspace / "current.py").write_bytes(source.encode("utf8"))
    context, calls = SourceContext(workspace), {}
    messages = [{"role": "user", "content": "Modify the current source and verify it."}]

    def read(**window):
        call = ToolCall(name="read_file", arguments={"path": "current.py", **window})
        before = context.begin(call)
        result = asyncio.run(ReadFileTool().execute(call.arguments, ToolContext("test", workspace)))
        context.capture(call, result, before)
        context.delivered(call.id, str(result))
        calls[call.id] = call
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{"id": call.id, "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}]},
            {"role": "tool", "tool_call_id": call.id, "content": str(result)},
        ])

    read()
    clipped = context.fit(messages, 900)
    assert any(TOOL_OMIT_MARKER in str(m.get("content", "")) for m in clipped)
    assert context.reminder(clipped, calls) is None

    restored = context.fit(messages, 20_000)
    assert any(m.get("content") == source for m in restored)
    assert context.reminder(restored, calls) is None

    hints = []
    for _ in range(3):
        read(offset=70, limit=3)
        hints.append(context.reminder(context.fit(messages, 20_000), calls))
    assert hints[:2] == [None, None]
    assert hints[-1] is not None and "3 轮" in hints[-1]
