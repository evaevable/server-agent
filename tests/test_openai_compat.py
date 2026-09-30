"""用 httpx.MockTransport 模拟服务端：不联网也能测请求体、解析、流式拼接、重试。"""

import json

import httpx
import pytest

from server_agent.llm import LLMError, Message
from server_agent.llm import openai_compat
from server_agent.llm.openai_compat import OpenAICompatClient


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def fast(*_a, **_k):
        return None

    monkeypatch.setattr(openai_compat.asyncio, "sleep", fast)


def make_client(handler, **kw):
    return OpenAICompatClient("https://llm.test/v1/", "sk-test", "m1", transport=httpx.MockTransport(handler), **kw)


def ok_json(content="你好", tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": "m1", "choices": [{"message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


async def test_chat_request_body_and_parse():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ok_json())

    c = make_client(handler)
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    r = await c.chat([Message.system("s"), Message.user("u")], tools=tools, max_tokens=64)
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    b = seen["body"]
    assert b["model"] == "m1" and b["max_tokens"] == 64 and b["tool_choice"] == "auto"
    assert [m["role"] for m in b["messages"]] == ["system", "user"]
    assert r.message.content == "你好" and r.finish_reason == "stop" and r.usage.total_tokens == 7


async def test_chat_parses_tool_calls():
    tcs = [{"id": "call_1", "type": "function", "function": {"name": "disk_usage", "arguments": '{"path":"/"}'}}]
    c = make_client(lambda req: httpx.Response(200, json=ok_json(None, tcs, "tool_calls")))
    r = await c.chat([Message.user("磁盘")])
    assert r.finish_reason == "tool_calls"
    assert r.message.tool_calls[0].name == "disk_usage"
    assert r.message.tool_calls[0].parsed_arguments() == {"path": "/"}


async def test_retry_on_429_then_success():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(429, headers={"retry-after": "1"}, text="rate limited") if n["i"] < 3 else httpx.Response(200, json=ok_json())

    r = await make_client(handler, max_retries=2).chat([Message.user("x")])
    assert r.message.content == "你好" and n["i"] == 3


async def test_no_retry_on_401():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(LLMError) as ei:
        await make_client(handler).chat([Message.user("x")])
    assert ei.value.status == 401 and not ei.value.retryable and n["i"] == 1


async def test_retry_exhausted_on_503():
    with pytest.raises(LLMError) as ei:
        await make_client(lambda r: httpx.Response(503, text="busy"), max_retries=1).chat([Message.user("x")])
    assert ei.value.status == 503 and ei.value.retryable


def sse(*chunks):
    lines = [": keep-alive"] + [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks] + ["data: [DONE]"]
    return ("\n\n".join(lines) + "\n\n").encode()


async def test_stream_text_and_usage():
    body = sse(
        {"model": "m1", "choices": [{"delta": {"role": "assistant", "content": "磁盘"}}]},
        {"choices": [{"delta": {"content": "满了"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}},
    )
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    events = [e async for e in make_client(handler).stream([Message.user("x")])]
    assert seen["body"]["stream"] is True and seen["body"]["stream_options"] == {"include_usage": True}
    assert [e.text for e in events if e.type == "text"] == ["磁盘", "满了"]
    done = events[-1].response
    assert done.message.content == "磁盘满了" and done.finish_reason == "stop" and done.usage.total_tokens == 7


async def test_stream_assembles_tool_call_fragments():
    body = sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_9", "function": {"name": "disk_", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "usage", "arguments": '{"pa'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "/var"}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_10", "function": {"name": "host_info", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    c = make_client(lambda r: httpx.Response(200, content=body), stream_usage=False)
    events = [e async for e in c.stream([Message.user("x")])]
    r = events[-1].response
    assert r.finish_reason == "tool_calls" and r.message.content is None
    assert [(t.id, t.name) for t in r.message.tool_calls] == [("call_9", "disk_usage"), ("call_10", "host_info")]
    assert r.message.tool_calls[0].parsed_arguments() == {"path": "/var"}


async def test_stream_error_status_raises():
    with pytest.raises(LLMError) as ei:
        [e async for e in make_client(lambda r: httpx.Response(400, text="bad")).stream([Message.user("x")])]
    assert ei.value.status == 400


async def test_stream_reasoning_content_separated():
    body = sse(
        {"choices": [{"delta": {"reasoning_content": "先想想"}}]},
        {"choices": [{"delta": {"reasoning_content": "磁盘"}}]},
        {"choices": [{"delta": {"content": "用 df -h"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    events = [e async for e in make_client(lambda r: httpx.Response(200, content=body)).stream([Message.user("x")])]
    assert [(e.type, e.text) for e in events[:-1]] == [("reasoning", "先想想"), ("reasoning", "磁盘"), ("text", "用 df -h")]
    r = events[-1].response
    assert r.message.content == "用 df -h" and r.reasoning == "先想想磁盘"
    assert "reasoning" not in r.message.to_dict()  # 思考过程不回传给模型


async def test_vllm_reasoning_field_is_recognized():
    """vLLM 0.10+ 把思考内容放在 reasoning（不是 reasoning_content），流式与非流式都要认。"""
    body = sse(
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"delta": {"reasoning": "查根分区"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    events = [e async for e in make_client(lambda r: httpx.Response(200, content=body)).stream([Message.user("x")])]
    assert events[0].type == "reasoning" and events[0].text == "查根分区"
    assert events[-1].response.reasoning == "查根分区" and events[-1].response.message.content == "好"

    data = ok_json()
    data["choices"][0]["message"]["reasoning"] = "想一下"
    r = await make_client(lambda req: httpx.Response(200, json=data)).chat([Message.user("x")])
    assert r.reasoning == "想一下"


async def test_extra_body_merged_and_call_opts_win():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ok_json())

    c = make_client(handler, extra_body={"thinking": {"type": "disabled"}, "max_tokens": 10})
    await c.chat([Message.user("x")], max_tokens=99)
    assert seen["body"]["thinking"] == {"type": "disabled"} and seen["body"]["max_tokens"] == 99
