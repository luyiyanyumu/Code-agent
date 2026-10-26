"""Literal failure locations remain visible through bounded context compaction."""

import pytest

from code_agent.context.compact import TOOL_OMIT_MARKER, compact_messages, messages_size_chars, prune_old_tool_results
from code_agent.context.diagnostics import failed_command_excerpt, is_failed_command_output


def collection_failure():
    return (
        "exit_code=2\n[stdout]\n================= ERRORS =================\n"
        + "importlib bootstrap and test collection trace\n" * 120
        + 'E     File "E:/project/code_agent/codeintel/file_search.py", line 55\n'
        + "E       _global_unused = None\nE       ^^^^^^^^^^^^^^\n"
        + "E   SyntaxError: invalid syntax\n"
        + "warning: unrelated library warning\n" * 100
        + "ERROR acceptance/test_search_delivery.py\nInterrupted: collection failed\n"
    )


@pytest.mark.parametrize("limit", [400, 900, 1200])
def test_middle_collection_root_is_retained_with_its_literal_location(limit):
    original = collection_failure()

    excerpt = failed_command_excerpt(original, limit)

    assert len(excerpt) <= limit
    assert excerpt.startswith("exit_code=2\n")
    assert 'File "E:/project/code_agent/codeintel/file_search.py", line 55' in excerpt
    assert "E       _global_unused = None" in excerpt
    assert "E   SyntaxError: invalid syntax" in excerpt
    assert excerpt.index("line 55") < excerpt.index("SyntaxError")
    assert "importlib bootstrap" not in excerpt
    assert "[失败诊断摘录]" in excerpt


def test_final_exception_groups_are_literal_and_not_inferred_from_summary():
    output = (
        "exit_code=1\n" + "routine progress\n" * 100
        + '  File "/project/service.py", line 42, in resolve\n'
        + "    raise ValueError('original failure')\nValueError: original failure\n"
        + "During handling of the above exception, another exception occurred:\n"
        + '  File "/project/routes.py", line 89, in handle\n'
        + "    raise RuntimeError('request failed')\nRuntimeError: request failed\n"
        + "collection summary\n" * 100
    )

    excerpt = failed_command_excerpt(output, 900)

    assert 'File "/project/service.py", line 42' in excerpt
    assert 'File "/project/routes.py", line 89' in excerpt
    assert "ValueError: original failure" in excerpt
    assert "RuntimeError: request failed" in excerpt
    assert len(excerpt) <= 900
    assert "caused by" not in excerpt


def test_assertion_and_filename_line_diagnostics_remain_visible():
    output = "exit_code=1\n" + "progress\n" * 300 + "test_api.py:44: AssertionError\nE   assert 400 == 200\n" + "warning\n" * 300
    excerpt = failed_command_excerpt(output, 600)
    assert "test_api.py:44: AssertionError" in excerpt
    assert "E   assert 400 == 200" in excerpt
    assert excerpt.startswith("exit_code=1\n")


@pytest.mark.parametrize("limit", [0, 1, 8, 31, 160, 900])
def test_limits_are_respected_and_repeated_excerpts_do_not_grow(limit):
    excerpt = failed_command_excerpt(collection_failure(), limit)
    assert len(excerpt) <= limit
    for _ in range(10):
        assert failed_command_excerpt(excerpt, limit) == excerpt


def test_very_long_exception_line_is_bounded_and_marked_as_an_excerpt():
    output = "exit_code=1\n" + "progress\n" * 100 + "ValueError: " + "message " * 1000
    excerpt = failed_command_excerpt(output, 900)
    assert len(excerpt) <= 900
    assert "ValueError:" in excerpt
    assert "[摘录]" in excerpt


def test_short_or_successful_results_are_not_invented_as_failures():
    assert failed_command_excerpt("exit_code=1\nValueError: bad") == "exit_code=1\nValueError: bad"
    assert not is_failed_command_output("exit_code=0\n12 passed")
    assert not is_failed_command_output('def error():\n    return "SyntaxError"')
    assert is_failed_command_output(collection_failure())


def test_history_pruning_retains_middle_failure_instead_of_only_collection_header_and_tail():
    messages = [{"role": "tool", "tool_call_id": "run", "content": collection_failure()}]
    result = prune_old_tool_results(messages)
    excerpt = result[0]["content"]
    assert "line 55" in excerpt and "SyntaxError: invalid syntax" in excerpt
    assert excerpt.count(TOOL_OMIT_MARKER) == 1
    for _ in range(20):
        assert prune_old_tool_results(result) == result
    assert messages[0]["content"] == collection_failure()


def test_full_compaction_keeps_original_failure_as_tool_data_and_stays_in_budget():
    messages = [
        {"role": "system", "content": "Trusted instructions."},
        {"role": "user", "content": "Repair the current implementation."},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "run", "type": "function", "function": {"name": "bash", "arguments": '{"command":"pytest"}'},
        }]},
        {"role": "tool", "tool_call_id": "run", "content": collection_failure()},
    ]

    result = compact_messages(messages, 900)

    assert messages_size_chars(message for message in result if message["role"] != "system") <= 900
    assert [message for message in result if message["role"] == "system"] == [messages[0]]
    tool = next(message for message in result if message["role"] == "tool")
    assert tool["tool_call_id"] == "run"
    assert "line 55" in tool["content"] and "SyntaxError: invalid syntax" in tool["content"]
    assert any(call["id"] == "run" for message in result for call in message.get("tool_calls", []))
