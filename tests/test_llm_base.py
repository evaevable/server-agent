import pytest

from server_agent.llm import Message, ToolCall, Usage


def test_message_to_dict_roles():
    assert Message.user("hi").to_dict() == {"role": "user", "content": "hi"}
    tc = ToolCall(id="c1", name="disk_usage", arguments='{"path": "/"}')
    d = Message.assistant(None, [tc]).to_dict()
    assert d["content"] is None
    assert d["tool_calls"][0] == {"id": "c1", "type": "function", "function": {"name": "disk_usage", "arguments": '{"path": "/"}'}}
    assert Message.tool("c1", "ok").to_dict() == {"role": "tool", "content": "ok", "tool_call_id": "c1"}


def test_tool_call_arguments_parsing():
    assert ToolCall("1", "f", '{"a": 1}').parsed_arguments() == {"a": 1}
    assert ToolCall("1", "f", "").parsed_arguments() == {}
    with pytest.raises(ValueError):
        ToolCall("1", "f", "{bad").parsed_arguments()
    with pytest.raises(ValueError):
        ToolCall("1", "f", "[1,2]").parsed_arguments()


def test_usage_add():
    assert Usage(1, 2, 3) + Usage(10, 20, 30) == Usage(11, 22, 33)
