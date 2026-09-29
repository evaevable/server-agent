import pytest

from server_agent.llm import LLMError, Message
from server_agent.llm.mock import MockLLM, text, tool_call


async def test_script_replay_in_order_and_records_calls():
    llm = MockLLM([tool_call("disk_usage", {"path": "/"}), "磁盘正常"])
    r1 = await llm.chat([Message.user("看看磁盘")], tools=[{"x": 1}])
    assert r1.finish_reason == "tool_calls" and r1.message.tool_calls[0].name == "disk_usage"
    r2 = await llm.chat([Message.user("看看磁盘"), r1.message, Message.tool(r1.message.tool_calls[0].id, "10%")])
    assert r2.message.content == "磁盘正常"
    assert len(llm.calls) == 2 and llm.calls[0]["tools"] == [{"x": 1}]
    assert llm.calls[1]["messages"][-1].role == "tool"


async def test_script_exhausted_raises():
    llm = MockLLM(["only one"])
    await llm.chat([Message.user("a")])
    with pytest.raises(LLMError):
        await llm.chat([Message.user("b")])


async def test_callable_script_item_sees_messages():
    llm = MockLLM([lambda msgs: f"收到 {len(msgs)} 条"])
    assert (await llm.chat([Message.system("s"), Message.user("u")])).message.content == "收到 2 条"


async def test_echo_and_stream_chunks():
    llm = MockLLM(chunk_size=2)
    events = [e async for e in llm.stream([Message.user("abc")])]
    assert "".join(e.text for e in events if e.type == "text") == "（mock）你说的是：abc"
    assert events[-1].type == "done" and events[-1].response.model == "mock"


def test_calls_are_snapshots():
    import asyncio

    llm = MockLLM([text("a")])
    msgs = [Message.user("x")]
    asyncio.run(llm.chat(msgs))
    msgs.append(Message.user("y"))
    assert len(llm.calls[0]["messages"]) == 1
