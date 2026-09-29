"""RunManager 测试：任务生命周期、事件缓冲、订阅补发、取消。"""

import asyncio

import pytest

from server_agent.agent.loop import Agent
from server_agent.agent.runs import RunManager, make_sse
from server_agent.llm import Message
from server_agent.llm.mock import MockLLM, tool_call
from server_agent.tools.registry import ToolRegistry


def build(factory=None):
    reg = ToolRegistry()

    @reg.tool(name="disk_usage")
    def _disk(path: str = "/") -> dict:
        """查磁盘。"""
        return {"mount": path, "percent": 97}

    def default_factory():
        llm = MockLLM([tool_call("disk_usage", {}), "根分区 97%"])
        return Agent(llm, reg, stream=False)

    return RunManager(factory or default_factory)


async def wait_done(mgr, run_id, timeout=3.0):
    run = mgr.get(run_id)
    deadline = asyncio.get_event_loop().time() + timeout
    while run.status == "running":
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"run 未在 {timeout}s 内结束：{run.status}")
        await asyncio.sleep(0.01)
    return run


async def test_run_completes_and_buffers_events():
    mgr = build()
    run = await mgr.start("磁盘满了吗")
    assert run.status == "running" and run.id.startswith("run_")
    await wait_done(mgr, run.id)
    assert run.status == "done"
    types = [e.type for e in run.events]
    assert types[0] == "start" and types[-1] == "end" and "tool_call" in types
    assert [e.seq for e in run.events] == list(range(1, len(run.events) + 1))
    summary = run.summary()
    assert summary["result"]["text"] == "根分区 97%" and summary["events"] == len(run.events)


async def test_subscribe_live_then_finish():
    mgr = build()
    run = await mgr.start("磁盘")
    seen = [ev.type async for ev in mgr.subscribe(run)]
    assert seen[0] == "start" and seen[-1] == "end"
    assert seen == [e.type for e in run.events]  # 与缓冲区一致，无重复无遗漏


async def test_subscribe_after_seq_replays_backlog():
    mgr = build()
    run = await mgr.start("磁盘")
    await wait_done(mgr, run.id)
    # 模拟断线重连：只补发 seq > 3 的事件
    replay = [e async for e in mgr.subscribe(run, after_seq=3)]
    assert [e.seq for e in replay] == list(range(4, len(run.events) + 1))


async def test_subscribe_late_while_running_gets_backlog_then_live():
    mgr = build()

    def slow_factory():
        import asyncio as aio

        class SlowLLM(MockLLM):
            async def chat(self, messages, tools=None, **opts):
                await aio.sleep(0.05)
                return await super().chat(messages, tools, **opts)

        return Agent(SlowLLM([tool_call("disk_usage", {}), "慢结论"]), ToolRegistry(), stream=False)

    mgr = build(slow_factory)
    run = await mgr.start("慢任务")
    await asyncio.sleep(0.01)  # 让 start 事件先落进缓冲区
    seqs = [e.seq async for e in mgr.subscribe(run)]
    assert seqs == sorted(set(seqs)) == list(range(1, len(seqs) + 1))  # 有序且不重复
    assert mgr.get(run.id).status == "done"


async def test_cancel_running_run():
    mgr = build()

    def slow_factory():
        class SlowLLM(MockLLM):
            async def chat(self, messages, tools=None, **opts):
                await asyncio.sleep(5)
                return await super().chat(messages, tools, **opts)

        return Agent(SlowLLM([]), ToolRegistry(), stream=False)

    mgr = build(slow_factory)
    run = await mgr.start("长任务")
    await asyncio.sleep(0.02)
    assert await mgr.cancel(run.id) is True
    assert run.status == "cancelled" and run.finished_at is not None
    assert await mgr.cancel(run.id) is False  # 已结束，无法再取消
    assert await mgr.cancel("run_nope") is False


async def test_error_in_agent_is_captured():
    reg = ToolRegistry()

    def bad_factory():
        llm = MockLLM(["ok"])
        llm.echo = False
        llm.script = []

        class Boom(MockLLM):
            async def chat(self, messages, tools=None, **opts):
                raise RuntimeError("模型炸了")

        return Agent(Boom([]), reg, stream=False)

    mgr = build(bad_factory)
    run = await mgr.start("会失败")
    await wait_done(mgr, run.id)
    assert run.status == "error" and "模型炸了" in run.error


async def test_eviction_keeps_running_and_limits_memory():
    mgr = build()
    from server_agent.agent import runs as runs_mod

    old = runs_mod.MAX_RUNS
    runs_mod.MAX_RUNS = 3
    try:
        ids = []
        for i in range(5):
            r = await mgr.start(f"任务{i}")
            ids.append(r.id)
            await wait_done(mgr, r.id)
        assert len(mgr.list()) == 3
        assert ids[-1] in mgr._runs and ids[0] not in mgr._runs
    finally:
        runs_mod.MAX_RUNS = old


def test_make_sse_format():
    from server_agent.agent.events import Event

    text = make_sse(Event("text", "run_x", 7, {"text": "你好"}))
    assert text.startswith("id: 7\nevent: text\ndata: ")
    assert text.endswith("\n\n") and '"你好"' in text
