"""Repair only a provably extra terminal JSON closer, never tool content."""

import json
import logging

import pytest

from code_agent.providers.openai_compat import OpenAICompatProvider, _parse_args


def test_real_batch_shape_with_extra_closer_after_approval_is_repaired_without_content_changes(caplog):
    arguments = {
        "edits": [
            {"path": "src/search.py", "old_string": "def search(query):\n    return []",
             "new_string": "def search(query, whole_word=False):\n    return find(query)"},
            {"path": "src/api.py", "old_string": 'return search(query="[x]")',
             "new_string": 'return search(query="[x]", whole_word=whole_word)'},
        ],
        "_approval": {"purpose": "Implement whole-word search", "effects": ["Change search and API", "Run tests"]},
    }
    valid = json.dumps(arguments)
    raw = valid[:-1] + "]}"  # Same structural defect as the real edit_many call.
    with caplog.at_level(logging.INFO, logger="code_agent.providers.openai_compat"):
        assert _parse_args(raw) == arguments
    assert len(caplog.records) == 1
    assert f"position {len(valid) - 1}" in caplog.text
    assert "src/search.py" not in caplog.text and "whole_word" not in caplog.text
    assert "_approval" not in caplog.text and raw not in caplog.text


@pytest.mark.parametrize("extra", ["]", "}"])
def test_extra_closer_after_complete_object_preserves_nested_strings_and_escapes(extra):
    arguments = {
        "old_string": 'const value = {"list": ["]}", "\\\\", "escaped \\\" quote"]};\n',
        "new_string": "嵌套文本 ] } { [ 与换行\n以及引号 \"、反斜线 \\ 保持原样",
        "command": 'python -c "print(\'[x] } \\\\ ]\')"',
        "nested": {"items": [{"literal": '"}]"'}, 1, True, None]},
    }
    raw = json.dumps(arguments, ensure_ascii=False) + extra + " \r\n"
    assert _parse_args(raw) == arguments


@pytest.mark.parametrize("raw", [
    '{"command": "echo hello"',                  # Missing closing object.
    '{"command": "unterminated } ]',             # Missing string terminator.
    r'{"command": "bad\q"}',                    # Invalid escape.
    '{"a": 1}{"b": 2}',                         # Two adjacent objects.
    '{"a": [1,]}',                              # Expected closer, missing value.
    '{"a": 1,}',                                # Expected closer, trailing comma.
    '{"a": [1 2]}',                             # Missing comma between values.
    '{"a": 1}]]',                               # More than one change needed.
    '{"a": 1}] "other"',                        # Non-structural suffix.
    '{"a": ]1}',                                # Error before a nonterminal value.
    '{"a": [1, 2}}',                            # Missing array closer, not an extra closer.
    '[{"a": 1}]]',                              # Repair would produce an array, not arguments.
])
def test_other_invalid_argument_shapes_are_not_repaired_or_logged(raw, caplog):
    with caplog.at_level(logging.INFO, logger="code_agent.providers.openai_compat"):
        assert _parse_args(raw) == {"_raw": raw}
    assert not caplog.records


@pytest.mark.parametrize("arguments", [{"edits": []}, {"command": "printf '}]'"}, [1, 2], "string", 2, None])
def test_valid_json_keeps_existing_object_only_contract(arguments, caplog):
    raw = json.dumps(arguments)
    assert _parse_args(raw) == (arguments if isinstance(arguments, dict) else {"_raw": raw})
    assert not caplog.records


def test_dictionary_arguments_remain_the_same_object():
    arguments = {"command": "echo unchanged"}
    assert _parse_args(arguments) is arguments


@pytest.mark.parametrize("streamed", [False, True])
def test_streamed_and_complete_tool_calls_use_the_same_narrow_argument_parser(streamed):
    arguments = {"path": "src/a.py", "old_string": "return []", "new_string": "return [1]"}
    raw = json.dumps(arguments)[:-1] + "]}"
    slot = ({"id": "edit_1", "name": "edit_file", "arguments": raw} if streamed else
            {"id": "edit_1", "function": {"name": "edit_file", "arguments": raw}})
    call = OpenAICompatProvider._build_tool_call(slot, raw=not streamed)
    assert call.id == "edit_1" and call.name == "edit_file"
    assert call.arguments == arguments
