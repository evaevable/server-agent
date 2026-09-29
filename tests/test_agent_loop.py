"""Agent 循环测试：全部使用 MockLLM 剧本，确定、免费、离线。"""

import json

import pytest

from server_agent.agent import Agent, AgentResult
from server_agent.agent.loop import MAX_STEPS_NOTE, NO_TOOL_NOTE, REPEAT_NOTE
from server_agent.llm import Message
from server_agent.llm.mock import MockLLM, text, tool_call
from server_agent.tools.registry import ToolRegistry


def make_tools() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="disk_usage")
    def _disk(path: str = "/") -> dict:
        """查磁盘。"""
        if path == "/missing":
            from server_agent.tools.registry import ToolError
            raise ToolError("路径不存在: /missing")
        return {"mount": path, "percent": 97}

    @reg.tool(name="top_processes")
    def _top(limit: int = 3) -> dict:
        """查进程。"""
        return {"count": limit}

    return reg


def fake_clock(step=0.001):
    t = [0.0]

    def clock():
        t[0] += step
        return t[0]

    return clock


async def collect(agent, question):
    return [ev async for ev in agent.run(question)]


async def test_two_step_run_feeds_tool_result_back():
    llm = MockLLM([tool_call("disk_usage", {"path": "/"}), "根分区 97%，建议清理 /var/log"])
    agent = Agent(llm, make_tools(), clock=fake_clock())
    events = await collect(agent, "磁盘为什么满了")

    types = [e.type for e in events]
    # 文本事件可能被拆成多片（流式逐字），先折叠连续的 text
    collapsed = [t for i, t in enumerate(types) if t != "text" or (i == 0 or types[i - 1] != "text")]
    assert collapsed == ["start", "step", "tool_call", "tool_result", "step", "text", "report", "end"]
    assert events[-2].data["parsed"] is False   # 本轮结论不是 JSON，报告解析失败（第 07 章）
    assert "".join(e.data["text"] for e in events if e.type == "text") == "根分区 97%，建议清理 /var/log"
    assert events[0].data["tools"] == ["disk_usage", "top_processes"]
    end = events[-1].data
    assert end["text"] == "根分区 97%，建议清理 /var/log"
    assert end["steps"] == 2 and end["tool_calls"] == 1 and end["stopped"] == "final"
    # 第二次调用模型时，历史里必须带上 tool 消息
    second = llm.calls[1]["messages"]
    assert [m.role for m in second] == ["system", "user", "assistant", "tool"]
    assert json.loads(second[-1].content)["percent"] == 97 and second[-1].tool_call_id == "call_1"
    # 排查阶段的每次请求都带上了工具 Schema；最后的「改写为 JSON」请求不需要工具
    assert all(len(c["tools"]) == 2 for c in llm.calls[:2])
    assert llm.calls[2]["tools"] is None


async def test_tool_error_is_fed_back_and_model_recovers():
    llm = MockLLM([tool_call("disk_usage", {"path": "/missing"}), "该路径不存在，我改查根目录"])
    agent = Agent(llm, make_tools(), clock=fake_clock())
    events = await collect(agent, "查一下 /missing")

    tr = next(e for e in events if e.type == "tool_result")
    assert tr.data["ok"] is False and "路径不存在" in tr.data["content"]
    assert llm.calls[1]["messages"][-1].content == tr.data["content"]  # 错误原样回喂
    assert events[-1].data["text"].startswith("该路径不存在")


async def test_repeated_call_is_skipped_with_note():
    llm = MockLLM([
        tool_call("disk_usage", {"path": "/"}),
        tool_call("disk_usage", {"path": "/"}),   # 完全相同的调用
        tool_call("disk_usage", {"path": "/"}),   # 再来一次
        "根分区 97%",
    ])
    agent = Agent(llm, make_tools(), clock=fake_clock())
    events = await collect(agent, "反复查磁盘")

    results = [e.data for e in events if e.type == "tool_result"]
    assert len(results) == 3
    assert results[0]["ok"] and "skipped" not in results[0]
    assert [r.get("skipped") for r in results[1:]] == ["repeat", "repeat"]
    assert REPEAT_NOTE.format(name="disk_usage")[:20] in results[1]["content"]
    assert events[-1].data["tool_calls"] == 3  # 计数含被跳过的那次
    assert events[-1].data["text"] == "根分区 97%"


async def test_parallel_tool_calls():
    from server_agent.llm.base import ChatResponse, ToolCall
    from server_agent.llm.mock import text as _text

    both = ChatResponse(Message.assistant(None, [
        ToolCall("c1", "disk_usage", '{"path": "/"}'),
        ToolCall("c2", "top_processes", '{"limit": 1}'),
    ]), finish_reason="tool_calls")
    llm = MockLLM([both, _text("都查完了")])
    agent = Agent(llm, make_tools(), clock=fake_clock())
    events = await collect(agent, "一起查")

    calls = [e.data["name"] for e in events if e.type == "tool_call"]
    results = [e.data["name"] for e in events if e.type == "tool_result"]
    assert calls == ["disk_usage", "top_processes"] and results == calls
    assert events[-1].data["tool_calls"] == 2 and events[-1].data["steps"] == 2


async def test_max_steps_stops_and_asks_for_conclusion():
    llm = MockLLM([tool_call("disk_usage", {"path": f"/p{i}"}) for i in range(10)])
    agent = Agent(llm, make_tools(), max_steps=3, clock=fake_clock())
    events = await collect(agent, "永远查不完")

    assert len(llm.calls) == 3
    assert MAX_STEPS_NOTE.format(n=3) in llm.calls[-1]["messages"][-1].content
    assert events[-1].data["stopped"] == "max_steps" and events[-1].data["steps"] == 3


async def test_timeout_breaks_before_next_step():
    clock = fake_clock(step=1.0)  # 每次读表都前进 1 秒
    llm = MockLLM([tool_call("disk_usage", {"path": "/"})] * 5)
    agent = Agent(llm, make_tools(), max_steps=5, timeout=2.0, clock=clock)
    events = await collect(agent, "慢查询")

    errors = [e.data["message"] for e in events if e.type == "error"]
    assert len(errors) == 1 and "超时" in errors[0]
    assert events[-1].data["stopped"] == "timeout"


async def test_empty_answer_gets_reminder_then_finishes():
    llm = MockLLM(["", "结论：一切正常"])
    events = await collect(Agent(llm, make_tools(), clock=fake_clock()), "空回答")
    assert llm.calls[1]["messages"][-1].content == NO_TOOL_NOTE
    assert events[-1].data["text"] == "结论：一切正常" and events[-1].data["stopped"] == "final"


async def test_bad_json_arguments_told_to_model():
    llm = MockLLM([tool_call("disk_usage", {}), "我重新调用"])
    llm.script[0].message.tool_calls[0].arguments = "{not json"
    events = await collect(Agent(llm, make_tools(), clock=fake_clock()), "坏参数")
    tr = next(e for e in events if e.type == "tool_result")
    assert tr.data["skipped"] == "bad_json" and "合法 JSON" in tr.data["content"]
    assert "合法 JSON" in llm.calls[1]["messages"][-1].content
    assert events[-1].data["steps"] == 2


async def test_length_finish_reason_notes_truncation():
    from server_agent.llm.base import ChatResponse

    llm = MockLLM([ChatResponse(Message.assistant("很长的回答被切"), finish_reason="length")])
    events = await collect(Agent(llm, make_tools(), clock=fake_clock()), "长回答")
    end = events[-1].data
    assert end["stopped"] == "length" and "截断" in end["text"]


async def test_llm_error_is_reported_not_raised():
    llm = MockLLM([])  # 剧本为空且不开 echo -> 调用即 LLMError
    llm.echo = False
    events = await collect(Agent(llm, make_tools(), clock=fake_clock()), "会失败")
    assert events[-1].data["stopped"] == "error"
    assert events[-1].data["text"] == "（模型调用失败）"
    assert any(e.type == "error" for e in events)


async def test_run_sync_returns_result_and_no_stream_mode():
    llm = MockLLM([tool_call("disk_usage", {}), "查完了"])
    r = await Agent(llm, make_tools(), stream=False, clock=fake_clock()).run_sync("不流式")
    assert isinstance(r, AgentResult) and r.text == "查完了" and r.stopped == "final"
    # 消息历史：system + user + assistant(工具调用) + tool(结果) + assistant(结论)
    assert r.usage.total_tokens > 0
    assert [m.role for m in r.messages] == ["system", "user", "assistant", "tool", "assistant"]


async def test_history_is_prepended():
    llm = MockLLM(["基于历史的回答"])
    agent = Agent(llm, make_tools(), clock=fake_clock())
    await agent.run_sync("第二个问题", history=[Message.user("第一个问题"), Message.assistant("第一个回答")])
    roles = [m.role for m in llm.calls[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_events_are_json_serializable():
    import asyncio

    llm = MockLLM([tool_call("disk_usage", {}), "ok"])
    events = asyncio.run(collect(Agent(llm, make_tools(), clock=fake_clock()), "序列化"))

    class FakeUsage:
        pass

    for ev in events:
        if ev.type == "end":
            ev.data["usage"] = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        json.dumps(ev.to_dict(), ensure_ascii=False)


async def test_context_compression_emits_event_and_fits_budget():
    """工具结果把历史撑爆时，Agent 应先压缩再请求模型（并发出 context 事件）。"""
    from server_agent.llm.base import ChatResponse, ToolCall

    huge = "字" * 20000

    class BigTool(ToolRegistry):
        pass

    reg = ToolRegistry()

    @reg.tool(name="big_tool", max_chars=100000)
    def _big(i: int = 0) -> str:
        """返回很大的结果。"""
        return f"{i}:" + huge

    script = []
    for i in range(3):
        # 参数必须不同：完全相同的调用会被第 04 章的重复检测拦下
        script.append(ChatResponse(Message.assistant(None, [ToolCall(f"c{i}", "big_tool", f'{{"i": {i}}}')]),
                                   finish_reason="tool_calls"))
    script.append("查完了")

    agent = Agent(MockLLM(script), reg, stream=False, timeout=99,
                  clock=fake_clock())
    agent.budget = __import__("server_agent.memory.context", fromlist=["ContextBudget"]).ContextBudget(
        max_tokens=12000, reserve_output=1000, keep_recent_tool_msgs=1)
    events = await collect(agent, "用大工具查三遍")

    ctx_events = [e for e in events if e.type == "context"]
    assert ctx_events, "超预算时应发出 context 事件"
    # 单条工具结果本身就超预算时，会走到「连最近几条也压」的兜底（stage 带 +protected 后缀）
    assert ctx_events[0].data["stage"].startswith(("tail_head", "summarize", "drop"))
    assert ctx_events[0].data["after_tokens"] <= ctx_events[0].data["before_tokens"]
    # 第二次请求给模型的历史必须已经被压过
    second_call_tokens = __import__("server_agent.memory.context", fromlist=["messages_tokens"]).messages_tokens(
        llm_messages(agent))
    assert second_call_tokens <= agent.budget.available
    assert events[-1].data["stopped"] == "final"


def llm_messages(agent):
    """取最近一次请求给模型的消息（MockLLM 记录了 calls）。"""
    return agent.llm.calls[-1]["messages"]
