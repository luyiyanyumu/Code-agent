"""Stopping an official-driver RPC must cancel its independent human waiter."""

import asyncio

import pytest

from code_agent.providers.offline import OfflineProvider
from test_permission_requests import (
    cleanup, decisions, drain, next_request, runtime_factory, write_turn,
)


@pytest.mark.parametrize("action", ["cancel", "interrupt_current_turn"])
def test_stop_cancels_an_approval_waiting_in_a_separate_harness_rpc(runtime_factory, action):
    runtime, manager = runtime_factory([
        write_turn("stopped-rpc.txt"), OfflineProvider.text_turn("must not request again"),
    ])
    queue, events = runtime.subscribe(), []

    async def scenario():
        task = asyncio.create_task(runtime.submit("write stopped-rpc.txt after approval"))
        try:
            request = await next_request(queue, events)
            getattr(runtime, action)()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            await cleanup(task)
        drain(queue, events)
        assert not (runtime.workspace / "stopped-rpc.txt").exists()
        assert runtime.router.resolve_default().calls == 1
        assert runtime.pending_approval() is None
        assert not manager.approval_repo.pending_for_session(runtime.session_id)
        assert decisions(events, request["approval_id"])[-1] == {
            "approval_id": request["approval_id"], "tool": "write_file",
            "approved": False, "status": "cancelled",
        }
        assert not runtime.busy and runtime.phase == ""
        assert not runtime.decide_approval(request["approval_id"], True)

    asyncio.run(scenario())
