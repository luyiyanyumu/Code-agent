"""Order dependent tool effects while retaining parallel independent reads.

Every actual execution still goes through the caller's existing permission and
approval callback. This scheduler neither interprets shell commands nor grants
tools. A separate trusted predicate may identify a validation command for
deferral after an earlier native write failed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import inspect
import re
from typing import Any

from code_agent.models.events import ToolCall


DEFERRED_PREFIX = "[tool deferred]"
_SHELL = {"bash", "bash_persistent", "shell", "exec_command"}
_NATIVE_WRITES = {"write_file", "edit_file", "edit_many", "apply_patch", "replace_in_file"}
_FAILURE = re.compile(r"^\s*(?:\[tool error\]|\[permission denied\]|\[edit_many\][^\n]*部分失败)")
_EXIT = re.compile(r"^\s*exit_code=(-?\d+)\b")


def _write_failed(result: str) -> bool:
    if _FAILURE.match(result):
        return True
    code = _EXIT.match(result)
    return code is not None and int(code[1]) != 0


async def _resolved(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _cancel_and_drain(tasks: Sequence[asyncio.Task]) -> None:
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    if not tasks:
        return
    drained = asyncio.gather(*tasks, return_exceptions=True)
    interrupted = False
    # Additional cancellation must not cancel the tools' own finally/cleanup
    # blocks or leave them detached while the parent reports that it stopped.
    while not drained.done():
        try:
            await asyncio.shield(drained)
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError


async def execute_tool_batch(
    calls: Sequence[ToolCall], *,
    get_tool: Callable[[str], Any],
    execute: Callable[[ToolCall], Awaitable[str]],
    on_result: Callable[[ToolCall, str], Any] | None = None,
    parallel: bool = True,
    on_deferred: Callable[[ToolCall, str], Any] | None = None,
    is_validation: Callable[[ToolCall], bool] | None = None,
) -> list[str]:
    """Execute one model batch, publishing each completion in original slots.

    Native writes and shells are sequential barriers. Other ordinary calls may
    run together between barriers. Session-suspending calls always run after
    every ordinary call, regardless of their original position. Result hooks
    can be synchronous or async and may return a replacement published string.

    Only an explicitly identified validation shell is deferred after a failed
    write. Unknown or repair shells still execute, and a read/test failure does
    not masquerade as a write failure. No cancellation is converted to success.
    """
    metadata = []
    for call in calls:
        try:
            tool = get_tool(call.name)
        except Exception:
            # The execution callback owns the actual unavailable-tool error,
            # permission checks and audit trail. Classification grants nothing.
            tool = None
        writing = call.name in _NATIVE_WRITES or bool(getattr(tool, "writes_path", False))
        metadata.append((bool(getattr(tool, "suspends_session", False)), writing, call.name in _SHELL))

    results = [""] * len(calls)
    tasks: list[asyncio.Task] = []
    pending: list[asyncio.Task] = []
    failed_write = False

    async def publish(index: int, result: str) -> None:
        published = await _resolved(on_result(calls[index], result)) if on_result is not None else None
        results[index] = published if isinstance(published, str) else result

    async def run(index: int) -> None:
        nonlocal failed_write
        call = calls[index]
        _, writing, shell = metadata[index]
        if failed_write and shell and is_validation is not None and is_validation(call):
            result = (DEFERRED_PREFIX + " 验证未执行：本轮前序文件写入失败，先修正写入后再运行此验证；"
                      "这不是测试失败或测试通过。")
            if on_deferred is not None:
                await _resolved(on_deferred(call, result))
            await publish(index, result)
            return
        try:
            result = await _resolved(execute(call))
        except Exception as exc:
            # Cancellation is a BaseException and deliberately escapes. Normal
            # unexpected callback failures retain the ordinary tool-error form.
            result = f"[tool error] {type(exc).__name__}: {exc}"
        if not isinstance(result, str):
            result = f"[tool error] invalid tool result: {type(result).__name__}"
        if writing and _write_failed(result):
            failed_write = True
        await publish(index, result)

    def start(index: int) -> asyncio.Task:
        task = asyncio.create_task(run(index))
        tasks.append(task)
        return task

    async def flush() -> None:
        if pending:
            await asyncio.gather(*pending)
            pending.clear()

    try:
        for index, (suspends, writing, shell) in enumerate(metadata):
            if suspends:
                continue
            if not parallel or writing or shell:
                await flush()
                await start(index)
            else:
                pending.append(start(index))
        await flush()
        for index, (suspends, _, _) in enumerate(metadata):
            if suspends:
                await start(index)
        return results
    finally:
        await _cancel_and_drain(tasks)
