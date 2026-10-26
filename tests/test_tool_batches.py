"""Real effect ordering, prompt publication, deferral, and cancellation drain."""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from code_agent.agent.tool_batches import DEFERRED_PREFIX, execute_tool_batch
from code_agent.models.events import ToolCall


def call(name, identifier=None, **arguments):
    return ToolCall(id=identifier or name, name=name, arguments=arguments)


def tools(name):
    return SimpleNamespace(writes_path=name in {"write_file", "custom_write"},
                           suspends_session=name in {"wait_agents", "ask_user_question"})


def validation(tc):
    return tc.name in {"bash", "bash_persistent"} and tc.arguments.get("validation") is True


def test_independent_reads_are_parallel_and_publish_on_completion_in_ordered_slots():
    async def run():
        both_started = asyncio.Event()
        second_finished = asyncio.Event()
        started, published = [], []

        async def execute(tc):
            started.append(tc.id)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            if tc.id == "a":
                await second_finished.wait()
            else:
                second_finished.set()
            return tc.id

        result = await execute_tool_batch([call("read_file", "a"), call("read_file", "b")],
                                         get_tool=tools, execute=execute,
                                         on_result=lambda tc, value: published.append((tc.id, value)))
        assert published == [("b", "b"), ("a", "a")]
        return result

    assert asyncio.run(asyncio.wait_for(run(), 2)) == ["a", "b"]


@pytest.mark.parametrize("parallel", [False, True])
def test_native_writes_finish_before_real_project_test_runs(tmp_path, parallel):
    async def run():
        completed = []

        async def execute(tc):
            if tc.name == "read_file":
                completed.append(tc.id)
                return "initial source"
            if tc.name == "write_file":
                await asyncio.sleep(0)
                (tmp_path / tc.arguments["path"]).write_text(tc.arguments["content"], encoding="utf8")
                completed.append(tc.id)
                return "written"
            assert completed == ["read", "implementation", "tests"]
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pytest", "-q", "test_feature.py", "-o", "addopts=", cwd=tmp_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
            assert process.returncode == 0, output.decode(errors="replace")
            completed.append(tc.id)
            return f"exit_code={process.returncode}\n" + output.decode(errors="replace")

        calls = [call("read_file", "read"),
                 call("write_file", "implementation", path="feature.py", content="def answer():\n    return 42\n"),
                 call("write_file", "tests", path="test_feature.py", content="from feature import answer\ndef test_answer():\n    assert answer() == 42\n"),
                 call("bash", "verify", validation=True)]
        result = await execute_tool_batch(calls, get_tool=tools, execute=execute, parallel=parallel,
                                         is_validation=validation)
        assert completed == ["read", "implementation", "tests", "verify"]
        assert result[-1].startswith("exit_code=0") and "1 passed" in result[-1]

    asyncio.run(run())


@pytest.mark.parametrize("parallel", [False, True])
def test_unknown_shell_is_a_barrier_between_read_waves(parallel):
    async def run():
        state = []

        async def execute(tc):
            if tc.id == "before":
                state.append("before")
            elif tc.name == "bash":
                assert state == ["before"]
                await asyncio.sleep(0)
                state.append("shell")
            else:
                assert state == ["before", "shell"]
                state.append("after")
            return tc.id

        return await execute_tool_batch([call("read_file", "before"), call("bash", "shell"), call("read_file", "after")],
                                        get_tool=tools, execute=execute, parallel=parallel)

    assert asyncio.run(run()) == ["before", "shell", "after"]


@pytest.mark.parametrize("parallel", [False, True])
def test_session_suspension_waits_for_all_ordinary_tools(parallel):
    async def run():
        completed = []

        async def execute(tc):
            if tc.name in {"wait_agents", "ask_user_question"}:
                assert completed[:3] == ["read", "write", "tail"]
            completed.append(tc.id)
            return tc.id

        calls = [call("wait_agents", "wait"), call("read_file", "read"), call("write_file", "write"),
                 call("read_file", "tail"), call("ask_user_question", "ask")]
        results = await execute_tool_batch(calls, get_tool=tools, execute=execute, parallel=parallel)
        assert completed == ["read", "write", "tail", "wait", "ask"]
        return results

    assert asyncio.run(run()) == ["wait", "read", "write", "tail", "ask"]


@pytest.mark.parametrize("failure", ["[tool error] stale file", "[permission denied] write not authorized",
                                     "[edit_many] **部分失败** one edit missing", "exit_code=7\nwrite failed"])
@pytest.mark.parametrize("parallel", [False, True])
def test_failed_write_defers_only_identified_validation_and_keeps_recovery_reads_and_shells(failure, parallel):
    async def run():
        executed, deferred, published = [], [], []

        async def execute(tc):
            executed.append(tc.id)
            return failure if tc.id == "write" else "exit_code=0" if tc.name == "bash" else "recovery source"

        calls = [call("custom_write", "write"), call("bash", "verify", validation=True),
                 call("read_file", "recover"), call("bash", "repair", command="repair current source")]
        results = await execute_tool_batch(calls, get_tool=tools, execute=execute, parallel=parallel,
                                          is_validation=validation,
                                          on_deferred=lambda tc, value: deferred.append((tc.id, value)),
                                          on_result=lambda tc, value: published.append(tc.id))
        assert executed == ["write", "recover", "repair"]
        assert [identifier for identifier, _ in deferred] == ["verify"]
        assert deferred[0][1].startswith(DEFERRED_PREFIX) and "未执行" in deferred[0][1]
        assert results[1] == deferred[0][1] and not results[1].startswith("exit_code=")
        assert len(published) == len(calls) and published[:2] == ["write", "verify"]

    asyncio.run(run())


def test_native_edit_failure_is_a_write_even_without_single_path_metadata():
    async def run():
        calls = [call("edit_many", "edit"), call("bash", "verify", validation=True)]
        executed = []

        async def execute(tc):
            executed.append(tc.id)
            return "[tool error] exact anchor missing"

        result = await execute_tool_batch(calls, get_tool=lambda _: SimpleNamespace(writes_path=False),
                                         execute=execute, is_validation=validation)
        assert executed == ["edit"] and result[1].startswith(DEFERRED_PREFIX)

    asyncio.run(run())


@pytest.mark.parametrize("failed_tool,result", [("read_file", "[tool error] missing source"),
                                               ("bash", "exit_code=1\nactual failing test")])
def test_read_or_validation_failure_is_not_a_failed_write(failed_tool, result):
    async def run():
        executed = []

        async def execute(tc):
            executed.append(tc.id)
            return result if tc.id == "first" else "exit_code=0"

        output = await execute_tool_batch([call(failed_tool, "first"), call("bash", "second", validation=True)],
                                         get_tool=tools, execute=execute, is_validation=validation)
        assert executed == ["first", "second"] and output[-1] == "exit_code=0"

    asyncio.run(run())


def test_without_validation_predicate_unknown_shell_is_never_assumed_or_skipped():
    async def run():
        executed = []

        async def execute(tc):
            executed.append(tc.id)
            return "[tool error] write failed" if tc.id == "write" else "exit_code=0"

        await execute_tool_batch([call("write_file", "write"), call("bash", "unknown", validation=True)],
                                 get_tool=tools, execute=execute)
        assert executed == ["write", "unknown"]

    asyncio.run(run())


def test_async_hooks_publish_replacement_after_deferred_audit():
    async def run():
        hooks = []

        async def execute(tc):
            return "[permission denied] cannot write"

        async def deferred(tc, result):
            hooks.append(("audit", tc.id))

        async def publish(tc, result):
            hooks.append(("publish", tc.id))
            return result + "\npublished"

        output = await execute_tool_batch([call("write_file", "write"), call("bash", "test", validation=True)],
                                         get_tool=tools, execute=execute, is_validation=validation,
                                         on_result=publish, on_deferred=deferred)
        assert hooks == [("publish", "write"), ("audit", "test"), ("publish", "test")]
        assert all(result.endswith("published") for result in output)

    asyncio.run(run())


@pytest.mark.parametrize("self_cancel", [False, True])
def test_cancellation_drains_every_started_tool_before_returning(self_cancel):
    async def run():
        started, cleaned = [], []
        both_started = asyncio.Event()

        async def execute(tc):
            started.append(tc.id)
            if len(started) == 2:
                both_started.set()
            try:
                await both_started.wait()
                if self_cancel and tc.id == "a":
                    raise asyncio.CancelledError
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(tc.id)

        task = asyncio.create_task(execute_tool_batch([call("read_file", "a"), call("read_file", "b")],
                                                     get_tool=tools, execute=execute))
        await both_started.wait()
        if not self_cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sorted(cleaned) == ["a", "b"]
        assert not [pending for pending in asyncio.all_tasks() if pending is not asyncio.current_task() and not pending.done()]

    asyncio.run(asyncio.wait_for(run(), 2))


def test_publication_failure_cancels_and_drains_sibling_tools():
    async def run():
        both_started = asyncio.Event()
        cleaned = []
        started = []

        async def execute(tc):
            started.append(tc.id)
            if len(started) == 2:
                both_started.set()
            try:
                await both_started.wait()
                if tc.id == "a":
                    return "source"
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(tc.id)

        def publish(tc, result):
            raise ValueError("publishing failed")

        with pytest.raises(ValueError, match="publishing failed"):
            await execute_tool_batch([call("read_file", "a"), call("read_file", "b")],
                                     get_tool=tools, execute=execute, on_result=publish)
        assert sorted(cleaned) == ["a", "b"]

    asyncio.run(asyncio.wait_for(run(), 2))
