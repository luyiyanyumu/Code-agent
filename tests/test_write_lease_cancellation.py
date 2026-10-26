"""Cancellation must never release a different writer's path ownership."""

import asyncio

import pytest

from code_agent.tools.base import Tool, ToolContext
from code_agent.tools.registry import FilteredToolRegistry, ToolRegistry, WriteLeaseTable


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


async def drain(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_cancelled_waiter_cannot_unlock_current_holder_for_later_writer():
    async def scenario():
        table = WriteLeaseTable()
        first_entered, first_release, third_entered, third_release = (asyncio.Event() for _ in range(4))

        async def hold(entered, release):
            async with table.lease("shared"):
                entered.set()
                await release.wait()

        tasks = [asyncio.create_task(hold(first_entered, first_release))]
        try:
            await first_entered.wait()
            waiter = asyncio.create_task(hold(asyncio.Event(), asyncio.Event()))
            tasks.append(waiter)
            await until(lambda: table._refs.get("shared") == 2)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert table._refs == {"shared": 1}
            assert table._locks["shared"].locked(), "a cancelled waiter released the active writer's lock"
            tasks.append(asyncio.create_task(hold(third_entered, third_release)))
            await until(lambda: table._refs.get("shared") == 2)
            assert not third_entered.is_set()
            first_release.set()
            await asyncio.wait_for(third_entered.wait(), 2)
            third_release.set()
            await asyncio.gather(tasks[0], tasks[-1])
            assert table.size() == 0
            assert not table._refs
        finally:
            await drain(tasks)

    asyncio.run(scenario())


@pytest.mark.parametrize("available", [["a"], ["a", "b"]])
def test_cancelled_multi_path_waiter_releases_only_its_acquired_prefix(available):
    async def scenario():
        table = WriteLeaseTable()
        owner = table.lease("z")
        await owner.__aenter__()
        entered = asyncio.Event()

        async def acquire_many():
            async with table.lease_all([*available, "z"]):
                entered.set()

        waiter = asyncio.create_task(acquire_many())
        try:
            await until(lambda: table._refs.get("z") == 2)
            assert all(table._locks[key].locked() for key in available)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not entered.is_set()
            assert table._refs == {"z": 1}
            assert table.size() == 1
            assert table._locks["z"].locked(), "partial acquisition cleanup released someone else's final path"
            # Its earlier acquired paths must already be available, while z
            # remains exclusively owned by the original lease.
            async with table.lease_all(available):
                assert table._locks["z"].locked()
        finally:
            await drain([waiter])
            await owner.__aexit__(None, None, None)
        assert table.size() == 0
        assert not table._refs

    asyncio.run(scenario())


def test_cancellation_after_owner_handoff_wakes_next_waiter_and_cleans_table():
    async def scenario():
        table = WriteLeaseTable()
        owner = table.lease("shared")
        await owner.__aenter__()
        entered = []

        async def enter(tag):
            async with table.lease("shared"):
                entered.append(tag)

        cancelled = asyncio.create_task(enter("cancelled"))
        survivor = asyncio.create_task(enter("survivor"))
        try:
            await until(lambda: table._refs.get("shared") == 3)
            # release() wakes the first waiter; cancel before it can resume its
            # acquire coroutine. asyncio must forward that handoff itself.
            await owner.__aexit__(None, None, None)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            await asyncio.wait_for(survivor, 2)
            assert entered == ["survivor"]
            assert table.size() == 0
            assert not table._refs
        finally:
            await drain([cancelled, survivor])

    asyncio.run(scenario())


@pytest.mark.parametrize("dispatch_all", [False, True])
def test_filtered_view_cancelled_waiter_preserves_inner_registry_ownership(tmp_path, dispatch_all):
    async def scenario():
        registry = ToolRegistry()
        filtered = FilteredToolRegistry(registry, exclude=("unrelated",))
        ctx = ToolContext(session_id="lease-cancel", workspace=tmp_path)
        entered = {tag: asyncio.Event() for tag in ("first", "cancelled", "third")}
        release = {tag: asyncio.Event() for tag in entered}
        order = []

        class MemoryWriter(Tool):
            name = "memory_writer"
            writes_path = True

            async def execute(self, arguments, ctx):
                tag = arguments["tag"]
                order.append(f"{tag}:start")
                entered[tag].set()
                await release[tag].wait()
                order.append(f"{tag}:end")
                return "ok"

        registry.register(MemoryWriter())

        async def write(view, tag):
            args = {"path": "shared.txt", "tag": tag}
            if dispatch_all:
                return await view.dispatch_all([("memory_writer", args)], ctx)
            async with view.write_lease("memory_writer", args, ctx):
                return await view.dispatch("memory_writer", args, ctx)

        table = registry._write_lease
        assert filtered._lease_table() is table
        tasks = [asyncio.create_task(write(registry, "first"))]
        try:
            await entered["first"].wait()
            cancelled = asyncio.create_task(write(filtered, "cancelled"))
            tasks.append(cancelled)
            await until(lambda: list(table._refs.values()) == [2])
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            tasks.append(asyncio.create_task(write(filtered, "third")))
            await until(lambda: list(table._refs.values()) == [2])
            assert not entered["cancelled"].is_set()
            assert not entered["third"].is_set(), "filtered waiter cancellation bypassed the inner owner's lease"
            release["first"].set()
            await asyncio.wait_for(entered["third"].wait(), 2)
            release["third"].set()
            await asyncio.gather(tasks[0], tasks[-1])
            assert order == ["first:start", "first:end", "third:start", "third:end"]
            assert table.size() == 0
            assert not table._refs
            assert filtered._own_write_lease.size() == 0
        finally:
            await drain(tasks)

    asyncio.run(scenario())
