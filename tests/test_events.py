"""models/events.py 单测：归一化模型与消息转换。"""

from code_agent.models.events import (
    Event,
    EventType,
    ModelTurn,
    TokenUsage,
    ToolCall,
    assistant_message,
    tool_message,
    user_message,
)


def test_tool_call_has_id_and_defaults():
    tc = ToolCall(name="bash", arguments={"command": "echo hi"})
    assert tc.id.startswith("tc_")
    assert tc.arguments == {"command": "echo hi"}


def test_model_turn_tool_detection():
    turn = ModelTurn(text="ok")
    assert not turn.has_tool_calls
    turn2 = ModelTurn(tool_calls=[ToolCall(name="bash")], stop_reason="tool_use")
    assert turn2.has_tool_calls
    assert turn2.stop_reason == "tool_use"


def test_message_shapes():
    turn = ModelTurn(
        text="thinking",
        tool_calls=[ToolCall(id="t1", name="bash", arguments={"command": "ls"})],
    )
    msg = assistant_message(turn)
    assert msg["role"] == "assistant"
    assert msg["tool_calls"][0]["function"]["name"] == "bash"
    assert '"command": "ls"' in msg["tool_calls"][0]["function"]["arguments"] or \
           '"command":"ls"' in msg["tool_calls"][0]["function"]["arguments"]

    assert user_message("hi")["role"] == "user"
    assert tool_message("t1", "result")["tool_call_id"] == "t1"


def test_event_roundtrip_jsonl():
    ev = Event(seq=1, type=EventType.user_message, payload={"text": "hello"})
    line = ev.model_dump_json()
    ev2 = Event.model_validate_json(line)
    assert ev2 == ev


def test_usage_total():
    assert TokenUsage(input_tokens=10, output_tokens=5).total == 15
